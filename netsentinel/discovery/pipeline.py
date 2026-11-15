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
from netsentinel.storage.kernel import connect as sqlite_connect

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


def _aware_local(dt: _dt.datetime) -> _dt.datetime:
    """naive 时间戳按本地时区 aware 化;已带 tzinfo 原样返回(V10.18)。

    写入侧 :meth:`_DiscoveryCache.put` 一律写 ``now().astimezone()``(带
    tz),但历史版本/外部工具可能写入 naive ISO 串(无 tz 后缀)。读取比较
    统一 aware 化:naive 按本地时区 attach——与写入侧本地墙钟口径一致,
    且天然规避 naive 减法跨夏令时边界少算 1 小时的退化;aware 条目的
    判定结果与旧式 ``now(created.tzinfo)`` 写法逐秒等价。
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_dt.datetime.now().astimezone().tzinfo)
    return dt


class _DiscoveryCache:
    """查询结果缓存(sqlite,TTL 小时),键=(engine, query, limit)。"""

    def __init__(self, db_path: str, ttl_h: int) -> None:
        self._ttl_h = int(ttl_h)
        self._lock = threading.Lock()
        path = Path(db_path)
        if str(path.parent):
            path.parent.mkdir(parents=True, exist_ok=True)
        # V10.21:连接统一经 storage.kernel.connect 工厂(全仓唯一
        # sqlite3.connect 入口);V10.18 手补的 WAL + busy_timeout PRAGMA
        # 随之收敛进工厂(口径不变:多终端并行跑 discovery 命令访问同一
        # 缓存文件时不抛 SQLITE_BUSY)。
        self._conn = sqlite_connect(str(path))
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
            # 时区防御(V10.18):naive 串按本地时区 aware 化后再与 aware
            # 当前时刻比较,统一 aware-vs-aware;NULL/畸形 created_at 按
            # 缓存未命中处理(TypeError 一并兜住,触发刷新重查)。
            created_dt = _aware_local(_dt.datetime.fromisoformat(created))
            if (_dt.datetime.now().astimezone() - created_dt).total_seconds() > self._ttl_h * 3600:
                return None
        except (TypeError, ValueError):
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
            except (DiscoveryConfigError, RuntimeError, OSError) as exc:
                # OSError 兜底:URLError(DNS 失败/断网)是 OSError 子类,
                # 引擎层(yandex._http_get)已转中文 RuntimeError,这里再兜住
                # 任何未转换的原始 OSError(含第三方/自定义引擎)——单查询
                # 失败只记 errors,绝不击穿整轮 discover。
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
        lines.append(f"# 引擎:{engine_name} | 生成:{_dt.datetime.now().astimezone().isoformat(timespec='seconds')}\n")
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
    parser.add_argument("--engine", default=None,
                        help="yandex(默认,官方 XML API)/ yandex-html(内置网页内核,免凭据)"
                             " / searxng / mock")
    parser.add_argument("--transport", default=None, choices=["browser", "http"],
                        help="yandex-html 专用传输通道:browser(Playwright 渲染,auto 在席即优)"
                             " / http(纯 urllib);缺省 auto")
    parser.add_argument("--template", action="append", default=None,
                        help="查询模板,可多次给出,支持 {kw} 占位符(默认原样查询)")
    parser.add_argument("--template-file", default=None,
                        help="查询模板文件(.txt,每行一模板,{kw} 占位符,# 注释;"
                             "与 --template 叠加去重;缺省 data/keywords/templates.txt 可自建)")
    parser.add_argument("--out", default="leads.txt", help="线索输出文件(bulk_intake 兼容)")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--offline-check", action="store_true",
                        help="只做装载/配置检查,不发任何查询(零外呼)")
    args = parser.parse_args(argv)

    from netsentinel.config import load_config

    cfg = load_config(args.config)
    if args.engine:
        cfg.discovery_engine = args.engine
    if args.transport:
        # 附加属性注入(V10.23):yandex-html 引擎构造时读取,其他引擎忽略。
        cfg.yandex_html_transport = args.transport

    from netsentinel.discovery.keywords import load_keywords, load_templates

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

    # V10.20:--template-file 模板文件装载(可选;与 --template 内联模板叠加
    # 去重,文件模板在前);两者都缺省时 templates 保持 None = 原样查询,
    # 主线行为零变化。装载在 --offline-check 之前完成,离线检查同样校验模板。
    templates: list[str] | None = list(args.template or [])
    if args.template_file:
        try:
            file_templates = load_templates(args.template_file)
        except ValueError as exc:
            print(f"错误:{exc}")
            return 2
        templates = file_templates + [t for t in templates if t not in file_templates]
    if not templates:
        templates = None

    print(f"关键词 {len(words)} 个;引擎 {cfg.discovery_engine};"
          f"discovery_online={'开' if cfg.discovery_online else '关'}")
    if templates is not None:
        print(f"查询模板 {len(templates)} 个(--template-file/--template)")
    if args.offline_check:
        print("离线检查完成:未发出任何查询(线索发现需 discovery_online: true)")
        return 0

    try:
        result = discover(words, cfg, templates=templates)
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
