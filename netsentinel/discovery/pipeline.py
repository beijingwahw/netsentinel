"""发现流水线与 CLI(V6.5):关键词 → 搜索引擎 → 去重线索清单。

输出与 ``ops.bulk_intake.load_bulk`` 完全兼容的 .txt 线索文件,可直接喂给
``python -m netsentinel.batchflow --input leads.txt`` 进入 V6 批量流水线。

红线 27/28 在此执行:
- 发现的 URL 只是线索,本模块不做任何判定/处置,输出文件头部写明用途;
- 关键词全部来自调用方;``discovery_online=False`` 时非 mock 引擎直接拒发;
- 查询间隔(默认 ≥3s,配置下限 1s)与单轮上限由 cfg 强制。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from netsentinel.contracts import Config
from netsentinel.discovery.engines import (
    DiscoveryConfigError,
    SearchEngine,
    SearchHit,
)

__all__ = ["discover", "write_leads", "main"]

logger = logging.getLogger(__name__)

#: 默认排除域(搜索引擎自身与常见基础设施域,运营者可追加)
DEFAULT_EXCLUDE_DOMAINS = frozenset(
    {
        "yandex.com",
        "yandex.ru",
        "ya.ru",
        "searx.github.io",
        "github.com",
        "google.com",
        "bing.com",
    }
)

LEADS_HEADER = "# NetSentinel 线索清单(搜索引擎发现):仅待筛查线索,非判定结论;\n# 须经理完整扫描→人工复核→声明→举报流程。\n"


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


class _DiscoveryCache:
    """查询结果缓存(sqlite,TTL 小时),键=(engine, query, limit)。"""

    def __init__(self, db_path: str, ttl_h: int) -> None:
        self._ttl_h = int(ttl_h)
        self._lock = threading.Lock()
        path = Path(db_path)
        if str(path.parent):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS queries ("
            "engine TEXT, query TEXT, limit_n INTEGER, urls TEXT, created_at TEXT,"
            " PRIMARY KEY(engine, query, limit_n))"
        )
        self._conn.commit()

    def get(self, engine: str, query: str, limit: int) -> list[str] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT urls, created_at FROM queries WHERE engine=? AND query=? AND limit_n=?",
                (engine, query, limit),
            ).fetchone()
        if not row:
            return None
        urls_raw, created = row
        try:
            created_dt = _dt.datetime.fromisoformat(created)
            if (_dt.datetime.now(created_dt.tzinfo) - created_dt).total_seconds() > self._ttl_h * 3600:
                return None
        except ValueError:
            return None
        return [u for u in (urls_raw or "").splitlines() if u]

    def put(self, engine: str, query: str, limit: int, urls: list[str]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO queries(engine, query, limit_n, urls, created_at)"
                " VALUES(?,?,?,?,?) ON CONFLICT(engine, query, limit_n)"
                " DO UPDATE SET urls=excluded.urls, created_at=excluded.created_at",
                (engine, query, limit, "\n".join(urls), _dt.datetime.now().astimezone().isoformat(timespec="seconds")),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def discover(
    keywords: list[str],
    cfg: Config,
    *,
    engine: SearchEngine | None = None,
    templates: list[str] | None = None,
    queries: list[str] | None = None,
    sleep: Callable[[float], None] | None = None,
    cache: _DiscoveryCache | None = None,
    exclude_domains: set[str] | frozenset[str] | None = None,
) -> dict:
    """执行一轮线索发现,返回线索清单与统计(不做任何判定)。

    :param keywords: 运营者自定义关键词(红线 28:调用方提供);
    :param engine: 注入引擎;缺省按 ``cfg.discovery_engine`` 构造;
    :param queries: 直接给查询(与 keywords 二选一,给出则忽略 keywords/templates);
    :param sleep: 查询间隔注入(测试用),缺省 time.sleep;
    :param cache: 结果缓存注入;缺省惰性按 cfg.discovery_cache_db 构造
        (``cache_db=""`` 或 ttl<=0 时禁用);
    :return: ``{"urls": [...], "queries": n, "cache_hits": n, "errors": [(query, 中文原因)]}``
    """
    from netsentinel import telemetry
    from netsentinel.discovery.keywords import expand_queries

    if queries is None:
        queries = expand_queries(keywords, templates)
    if not queries:
        return {"urls": [], "queries": 0, "cache_hits": 0, "errors": []}

    if engine is None:
        from netsentinel.discovery.engines import get_engine

        engine = get_engine(cfg.discovery_engine, cfg)

    sleeper = sleep or time.sleep
    excludes = set(DEFAULT_EXCLUDE_DOMAINS) | set(exclude_domains or [])

    if cache is None:
        ttl = int(getattr(cfg, "discovery_cache_ttl_h", 0) or 0)
        cache = (
            _DiscoveryCache(cfg.discovery_cache_db, ttl) if cfg.discovery_cache_db and ttl > 0 else None
        )
    own_cache = cache is not None

    urls: list[str] = []
    seen_canonical: set[str] = set()
    errors: list[tuple[str, str]] = []
    cache_hits = 0
    max_per = max(1, int(cfg.discovery_max_per_query))
    max_total = max(1, int(cfg.discovery_max_total))
    delay = float(cfg.discovery_query_delay_s)

    from netsentinel.intel.canonical import canonical_key  # 惰性(A103)

    for idx, q in enumerate(queries):
        if len(urls) >= max_total:
            logger.info("达到单轮线索上限 %d,剩余查询跳过", max_total)
            break
        cached = cache.get(engine.name, q, max_per) if cache else None
        if cached is not None:
            cache_hits += 1
            hits = cached
            telemetry.inc("discovery.cache_hit")
        else:
            if idx > 0:
                sleeper(delay)
            try:
                with telemetry.timer("discovery.search"):
                    found = engine.search(q, max_per)
                hits = [h.url for h in found]
            except (DiscoveryConfigError, RuntimeError) as exc:
                errors.append((q, str(exc)))
                telemetry.inc("discovery.errors")
                continue
            if cache:
                cache.put(engine.name, q, max_per, hits)
        for u in hits:
            if len(urls) >= max_total:
                break
            host = _host_of(u)
            if not host or host in excludes or any(host.endswith("." + d) for d in excludes):
                continue
            ck = canonical_key(u)
            if not ck or ck in seen_canonical:
                continue
            seen_canonical.add(ck)
            urls.append(u)

    if own_cache and cache is not None:
        cache.close()

    telemetry.inc("discovery.queries", len(queries))
    telemetry.inc("discovery.leads", len(urls))
    logger.info(
        "发现完成:查询 %d 次 / 线索 %d 条 / 缓存命中 %d / 失败 %d 条",
        len(queries), len(urls), cache_hits, len(errors),
    )
    return {"urls": urls, "queries": len(queries), "cache_hits": cache_hits, "errors": errors}


def write_leads(urls: list[str], path: str, *, engine_name: str = "") -> int:
    """写线索 .txt(带用途头注释,兼容 bulk_intake.load_bulk);返回条数。"""
    target = Path(path)
    if str(target.parent):
        target.parent.mkdir(parents=True, exist_ok=True)
    lines = [LEADS_HEADER]
    if engine_name:
        lines.append(f"# 引擎:{engine_name} | 生成:{_dt.datetime.now().isoformat(timespec='seconds')}\n")
    lines.extend(u + "\n" for u in urls)
    target.write_text("".join(lines), encoding="utf-8")
    return len(urls)


def main(argv: list[str] | None = None) -> int:
    """CLI:``python -m netsentinel.discovery --keywords-file k.txt [--engine yandex] --out leads.txt``"""
    parser = argparse.ArgumentParser(
        prog="python -m netsentinel.discovery",
        description="搜索引擎线索发现(优先 Yandex;自定义关键词;仅产出待筛查线索)",
    )
    parser.add_argument("--keywords-file", help="关键词文件(.txt/.yaml)或以逗号分隔的词表")
    parser.add_argument("--keywords", help="内联关键词,逗号分隔(与 --keywords-file 二选一)")
    parser.add_argument("--engine", default=None, help="yandex(默认)/ searxng / mock")
    parser.add_argument("--template", action="append", default=None,
                        help="查询模板,可多次给出,支持 {kw} 占位符(默认原样查询)")
    parser.add_argument("--out", default="leads.txt", help="线索输出文件(bulk_intake 兼容)")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--offline-check", action="store_true",
                        help="只做装载/配置检查,不发任何查询(零外呼)")
    args = parser.parse_args(argv)

    from netsentinel.config import load_config

    cfg = load_config(args.config)
    if args.engine:
        cfg.discovery_engine = args.engine

    from netsentinel.discovery.keywords import load_keywords

    try:
        if args.keywords:
            words = load_keywords([w.strip() for w in args.keywords.split(",")])
        elif args.keywords_file:
            p = Path(args.keywords_file)
            words = load_keywords(p if p.suffix.lower() in (".txt", ".yaml", ".yml") else str(p))
        else:
            print("错误:请用 --keywords 或 --keywords-file 提供自定义关键词(不内置任何词表)")
            return 2
    except ValueError as exc:
        print(f"错误:{exc}")
        return 2

    print(f"关键词 {len(words)} 个;引擎 {cfg.discovery_engine};"
          f"discovery_online={'开' if cfg.discovery_online else '关'}")
    if args.offline_check:
        print("离线检查完成:未发出任何查询(线索发现需 discovery_online: true)")
        return 0

    try:
        result = discover(words, cfg, templates=args.template)
    except (DiscoveryConfigError, RuntimeError) as exc:
        print(f"错误:{exc}")
        return 1
    n = write_leads(result["urls"], args.out, engine_name=cfg.discovery_engine)
    print(f"发现完成:查询 {result['queries']} 次,线索 {n} 条 → {args.out}")
    for q, reason in result["errors"][:5]:
        print(f"  失败:{q} —— {reason}")
    print("下一步:python -m netsentinel.batchflow --input " + args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
