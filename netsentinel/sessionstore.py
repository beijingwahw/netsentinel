# -*- coding: utf-8 -*-
"""会话累积库 —— 运行结果按网站名字(可注册域)分类持久累积(NetSentinel · 会话累积席)。

需求落点(与会话流程 :mod:`netsentinel.sessionflow` 配套):

1. **自动按网站名字分类累积**:每次扫描产出的站点报告经
   :meth:`SessionStore.accumulate_report` 归入"同 canonical(可注册域)一组"
   的累积记录(www / 子域 / 端口变体都算同站,host 记入 aliases);
2. **结束运行存档**:SQLite 累积库本身就是延续基座;:meth:`SessionStore.archive`
   另写一份人类可读的 JSON 快照(存档),含全部组与扫描事件统计;
3. **下次运行继续**:重开同一 ``<data_dir>/session/accumulation.db`` 即在
   上一次基础上继续累积(scan_count 递增、verdict 取最严重档、agg 取最大、
   aliases/site_urls 并集),历史明细落 ``scan_events``(append-only);
4. **确认举报时按网站名字举报**:由 sessionflow 取 ``pending_groups()``
   (未举报组)逐组汇总展示,举报完成经 :meth:`mark_reported` 收档。

安全口径(与全仓红线一致):

- 本模块**只做本地累积与存档**,不扫描、不联网、不产生任何提交动作;
- 组名口径与 A104 ``case_group`` / A110 ``resolve_group_name`` 完全一致
  (canonical 可注册域名),因此累积库的组名能直接对上复核队列 note 里的
  ``[组:组名]`` 标记;A103 ``intel.canonical`` 惰性导入,缺席时用内置同语义
  兜底(多段后缀表 com.cn/net.cn/org.cn/gov.cn/edu.cn/ac.cn + 通用 TLD);
- 连接策略与 review_queue 等存储模块同一:WAL + busy_timeout 5000,可重复
  打开同一库、支持 ``with`` 上下文。

用法示例::

    from netsentinel.sessionstore import SessionStore

    store = SessionStore("data/session/accumulation.db")
    store.accumulate_report(report)          # 判定/agg/别名自动并入该站组
    groups = store.pending_groups()          # 未举报组(按严重度排序)
    store.mark_reported([g.name for g in groups])
    store.archive("data/session/archive.json")
"""
from __future__ import annotations

import importlib
import ipaddress
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from netsentinel import telemetry
from netsentinel.contracts import now_iso
from netsentinel.storage.kernel import connect as sqlite_connect

__all__ = ["SiteGroupAccum", "SessionStore"]

logger = logging.getLogger(__name__)

#: SQLite 忙等上限(毫秒):与 review_queue / batch_review 同一策略。
BUSY_TIMEOUT_MS: int = 5000

#: 判定档位(clean < suspect < nsfw);未知档按 clean 记(取不到值时保守留旧)。
_VERDICT_RANK: dict[str, int] = {"clean": 0, "suspect": 1, "nsfw": 2}

#: A103 canonical 模块名(惰性导入目标;缺席走内置兜底)。
_CANONICAL_MODULE = "netsentinel.intel.canonical"

#: 内置兜底:常见多段后缀表(与 A104 case_group 内置表一致,禁增删)。
_MULTI_SUFFIXES: frozenset[str] = frozenset(
    {"com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn"}
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS session_groups (
    name         TEXT PRIMARY KEY,
    aliases      TEXT    NOT NULL DEFAULT '[]',
    site_urls    TEXT    NOT NULL DEFAULT '[]',
    scan_count   INTEGER NOT NULL DEFAULT 0,
    verdict      TEXT    NOT NULL DEFAULT 'clean',
    last_verdict TEXT    NOT NULL DEFAULT 'clean',
    agg_max      REAL    NOT NULL DEFAULT 0.0,
    last_agg     REAL    NOT NULL DEFAULT 0.0,
    nsw_total    INTEGER NOT NULL DEFAULT 0,
    first_seen   TEXT    NOT NULL DEFAULT '',
    last_seen    TEXT    NOT NULL DEFAULT '',
    reported     INTEGER NOT NULL DEFAULT 0,
    reported_at  TEXT    NOT NULL DEFAULT ''
)
"""

_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS scan_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_name TEXT NOT NULL,
    site_url   TEXT NOT NULL,
    verdict    TEXT NOT NULL DEFAULT '',
    agg        REAL NOT NULL DEFAULT 0.0,
    ts         TEXT NOT NULL DEFAULT ''
)
"""

_EVENTS_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_scan_events_group ON scan_events(group_name, id)"
)


def _verdict_text(value: Any) -> str:
    """判定值(Verdict 枚举 / 字符串)→ 合法档位小写串;未知返回空串。"""
    raw = getattr(value, "value", value)
    text = str(raw if raw is not None else "").strip().lower()
    return text if text in _VERDICT_RANK else ""


def _worst(left: str, right: str) -> str:
    """两档位取更严重者(空档不参赛;双空返回 'clean')。"""
    if left and (not right or _VERDICT_RANK[left] >= _VERDICT_RANK[right]):
        return left or "clean"
    return right or left or "clean"


# ---------------------------------------------------------------------------
# canonical:惰性 A103 + 内置同语义兜底(口径与 A104 case_group 一致)
# ---------------------------------------------------------------------------
def _load_canonical() -> Any | None:
    """惰性导入 A103 canonical;任何异常按未就位(返回 None 走兜底)。"""
    try:
        return importlib.import_module(_CANONICAL_MODULE)
    except Exception as exc:  # noqa: BLE001 - 守卫宽:任何异常按未就位
        logger.debug("canonical(A103)未就位,累积分组启用内置兜底:%s", exc)
        return None


def _is_ip_literal(host: str) -> bool:
    """host 是否为 IP 字面量(快路径:普通域名直接短路)。"""
    if ":" not in host and not host[:1].isdigit():
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _builtin_canonical_key(url: str) -> str:
    """内置兜底 canonical_key:多段后缀取末三段,否则末两段;IP 直连取 IP。"""
    try:
        host = urlsplit(str(url or "").strip()).hostname
    except ValueError:
        return ""
    host = (host or "").rstrip(".")
    if not host:
        return ""
    if _is_ip_literal(host):
        return host
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI_SUFFIXES:
        return ".".join(labels[-3:])
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return labels[-1]


def _host_of(url: str) -> str:
    """提取 URL host(小写、去端口、去尾点);失败返回空串。"""
    try:
        host = urlsplit(str(url or "").strip()).hostname
    except ValueError:
        host = ""
    return (host or "").rstrip(".").lower()


# ---------------------------------------------------------------------------
# 组累积记录(字段与 session_groups 表一一对应)
# ---------------------------------------------------------------------------
@dataclass
class SiteGroupAccum:
    """一个网站(canonical 组)的跨会话累积记录。"""

    name: str = ""
    aliases: list[str] = field(default_factory=list)
    site_urls: list[str] = field(default_factory=list)
    scan_count: int = 0
    verdict: str = "clean"       # 历次最严重档(只升不降)
    last_verdict: str = "clean"  # 最近一次档位
    agg_max: float = 0.0         # 历次聚合分最大值
    last_agg: float = 0.0        # 最近一次聚合分
    nsw_total: int = 0           # 历次达标图计数之和(累积规模感)
    first_seen: str = ""
    last_seen: str = ""
    reported: int = 0            # 0=待举报(累积中) 1=已完成举报
    reported_at: str = ""

    def sort_key(self) -> tuple[int, float, int, str]:
        """待举报组排序键:(严重档, agg_max, 扫描次数)降序,组名升序兜底。"""
        return (
            -_VERDICT_RANK.get(self.verdict, 0),
            -self.agg_max,
            -self.scan_count,
            self.name,
        )

    def as_dict(self) -> dict[str, Any]:
        """存档快照行(aliases/site_urls 原样列表;不携带报告正文)。"""
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "site_urls": list(self.site_urls),
            "scan_count": self.scan_count,
            "verdict": self.verdict,
            "last_verdict": self.last_verdict,
            "agg_max": round(self.agg_max, 4),
            "last_agg": round(self.last_agg, 4),
            "nsw_total": self.nsw_total,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "reported": bool(self.reported),
            "reported_at": self.reported_at,
        }


# ---------------------------------------------------------------------------
# SessionStore 主类
# ---------------------------------------------------------------------------
class SessionStore:
    """按网站名字(canonical)持久累积扫描结果的 SQLite 存储。

    - 两张表:``session_groups``(一组一行,UPSERT 累积)与 ``scan_events``
      (append-only 扫描明细,重放历史);
    - 同一库可反复打开(下次运行继续累积);WAL + 忙等 5s;
    - 全部方法纯本地,零网络、零提交动作。
    """

    def __init__(self, db_path: str | Path, *, canonical: Any | None = None) -> None:
        self.db_path = str(db_path)
        self._canonical = canonical if canonical is not None else _load_canonical()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite_connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        # V10.21:连接统一经 storage.kernel.connect 工厂(全仓唯一
        # sqlite3.connect 入口);WAL + 忙等 5s 由工厂内置,口径不变。
        self._conn.execute(_SCHEMA_SQL)
        self._conn.execute(_EVENTS_SQL)
        self._conn.execute(_EVENTS_INDEX_SQL)
        self._conn.commit()
        logger.debug("会话累积库已就绪:%s", self.db_path)

    # ------------------------------------------------------------------
    # canonical 解析(A103 优先,内置兜底)
    # ------------------------------------------------------------------
    def _group_name(self, url: str) -> str:
        """URL → canonical 组名(A103 优先;解析失败回退 host,再退原串截断)。"""
        if self._canonical is not None:
            try:
                name = str(self._canonical.canonical_name(url) or "")
                if name:
                    return name
            except Exception:  # noqa: BLE001 - 单条解析异常按兜底处理
                logger.debug("canonical_name(%r) 异常,按内置兜底处理", url)
        key = _builtin_canonical_key(url)
        if key:
            return key
        host = _host_of(url)
        return host or str(url or "").strip()[:60]

    # ------------------------------------------------------------------
    # 累积(核心写路径)
    # ------------------------------------------------------------------
    def accumulate(
        self,
        site_url: str,
        verdict: str,
        agg: float,
        *,
        nsw_count: int = 0,
    ) -> SiteGroupAccum:
        """把一次扫描结论并入对应网站组,返回并库后的累积记录。

        - 同 canonical 组:scan_count +1,verdict 取历史最严重档,
          agg_max 取历史最大,aliases/site_urls 并集去重(保首见序),
          nsw_total 累加,first_seen/last_seen 维护;
        - 新组:插入首行(first_seen = last_seen = 当前时间);
        - 同时向 ``scan_events`` 追加一条明细(append-only,历史可重放)。
        """
        name = self._group_name(site_url)
        ts = now_iso()
        v = _verdict_text(verdict)
        try:
            agg_f = float(agg)
        except (TypeError, ValueError):
            agg_f = 0.0
        try:
            nsw_i = int(nsw_count)
        except (TypeError, ValueError):
            nsw_i = 0

        host = _host_of(site_url)
        row = self._conn.execute(
            "SELECT * FROM session_groups WHERE name = ?", (name,)
        ).fetchone()

        if row is None:
            aliases = [host] if host else []
            urls = [site_url] if site_url else []
            group = SiteGroupAccum(
                name=name,
                aliases=aliases,
                site_urls=urls,
                scan_count=1,
                verdict=v or "clean",
                last_verdict=v or "clean",
                agg_max=agg_f,
                last_agg=agg_f,
                nsw_total=nsw_i,
                first_seen=ts,
                last_seen=ts,
            )
            self._conn.execute(
                """
                INSERT INTO session_groups
                    (name, aliases, site_urls, scan_count, verdict, last_verdict,
                     agg_max, last_agg, nsw_total, first_seen, last_seen,
                     reported, reported_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '')
                """,
                (
                    group.name,
                    json.dumps(group.aliases, ensure_ascii=False),
                    json.dumps(group.site_urls, ensure_ascii=False),
                    group.scan_count,
                    group.verdict,
                    group.last_verdict,
                    group.agg_max,
                    group.last_agg,
                    group.nsw_total,
                    group.first_seen,
                    group.last_seen,
                ),
            )
            telemetry.inc("sessionstore.group_created")
        else:
            group = self._row_to_group(row)
            group.scan_count += 1
            if v:
                group.verdict = _worst(group.verdict, v)
                group.last_verdict = v
            group.agg_max = max(group.agg_max, agg_f)
            group.last_agg = agg_f
            group.nsw_total += nsw_i
            group.last_seen = ts
            if host and host not in group.aliases:
                group.aliases.append(host)
            if site_url and site_url not in group.site_urls:
                group.site_urls.append(site_url)
            self._conn.execute(
                """
                UPDATE session_groups SET
                    aliases = ?, site_urls = ?, scan_count = ?, verdict = ?,
                    last_verdict = ?, agg_max = ?, last_agg = ?, nsw_total = ?,
                    last_seen = ?
                WHERE name = ?
                """,
                (
                    json.dumps(group.aliases, ensure_ascii=False),
                    json.dumps(group.site_urls, ensure_ascii=False),
                    group.scan_count,
                    group.verdict,
                    group.last_verdict,
                    group.agg_max,
                    group.last_agg,
                    group.nsw_total,
                    group.last_seen,
                    group.name,
                ),
            )

        self._conn.execute(
            """
            INSERT INTO scan_events (group_name, site_url, verdict, agg, ts)
            VALUES (?, ?, ?, ?, ?)
            """,
            (group.name, str(site_url or ""), v or "", agg_f, ts),
        )
        self._conn.commit()
        telemetry.inc("sessionstore.accumulated")
        logger.debug(
            "累积入组:%s(第 %d 次扫描,verdict=%s agg=%.4f)",
            group.name, group.scan_count, group.last_verdict, group.last_agg,
        )
        return group

    def accumulate_report(self, report: Any) -> SiteGroupAccum:
        """鸭子读取站点报告(SiteReport)并累积(getattr 容错,离线纯本地)。"""
        verdict = _verdict_text(getattr(report, "verdict", ""))
        try:
            agg = float(getattr(report, "agg_nsw_prob", 0.0) or 0.0)
        except (TypeError, ValueError):
            agg = 0.0
        try:
            nsw = int(getattr(report, "nsw_image_count", 0) or 0)
        except (TypeError, ValueError):
            nsw = 0
        return self.accumulate(
            str(getattr(report, "site_url", "") or ""),
            verdict,
            agg,
            nsw_count=nsw,
        )

    # ------------------------------------------------------------------
    # 读路径
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_group(row: sqlite3.Row) -> SiteGroupAccum:
        """session_groups 行 → SiteGroupAccum(aliases/site_urls 反序列化)。"""
        try:
            aliases = [str(a) for a in json.loads(row["aliases"] or "[]")]
        except (json.JSONDecodeError, TypeError):
            aliases = []
        try:
            urls = [str(u) for u in json.loads(row["site_urls"] or "[]")]
        except (json.JSONDecodeError, TypeError):
            urls = []
        return SiteGroupAccum(
            name=str(row["name"] or ""),
            aliases=aliases,
            site_urls=urls,
            scan_count=int(row["scan_count"] or 0),
            verdict=str(row["verdict"] or "clean"),
            last_verdict=str(row["last_verdict"] or "clean"),
            agg_max=float(row["agg_max"] or 0.0),
            last_agg=float(row["last_agg"] or 0.0),
            nsw_total=int(row["nsw_total"] or 0),
            first_seen=str(row["first_seen"] or ""),
            last_seen=str(row["last_seen"] or ""),
            reported=int(row["reported"] or 0),
            reported_at=str(row["reported_at"] or ""),
        )

    def get_group(self, name: str) -> SiteGroupAccum | None:
        """取一个组;不存在返回 None。"""
        row = self._conn.execute(
            "SELECT * FROM session_groups WHERE name = ?", (str(name or ""),)
        ).fetchone()
        return None if row is None else self._row_to_group(row)

    def list_groups(self, *, include_reported: bool = True) -> list[SiteGroupAccum]:
        """全部组,按 (严重档, agg_max, 扫描次数) 降序(组名升序兜底)。"""
        rows = self._conn.execute("SELECT * FROM session_groups").fetchall()
        groups = [self._row_to_group(r) for r in rows]
        if not include_reported:
            groups = [g for g in groups if not g.reported]
        groups.sort(key=SiteGroupAccum.sort_key)
        return groups

    def pending_groups(self) -> list[SiteGroupAccum]:
        """待举报组(reported=0),排序同 :meth:`list_groups`。"""
        return self.list_groups(include_reported=False)

    def group_events(self, name: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """一个组的扫描明细(最新在前,默认最近 20 条)。"""
        rows = self._conn.execute(
            "SELECT group_name, site_url, verdict, agg, ts FROM scan_events"
            " WHERE group_name = ? ORDER BY id DESC LIMIT ?",
            (str(name or ""), int(limit)),
        ).fetchall()
        return [
            {
                "site_url": str(r["site_url"] or ""),
                "verdict": str(r["verdict"] or ""),
                "agg": float(r["agg"] or 0.0),
                "ts": str(r["ts"] or ""),
            }
            for r in rows
        ]

    def stats(self) -> dict[str, int]:
        """累积库规模统计:组数 / 待举报 / 已举报 / 扫描事件总数。"""
        groups = self._conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(reported), 0) AS r"
            " FROM session_groups"
        ).fetchone()
        events = self._conn.execute("SELECT COUNT(*) AS n FROM scan_events").fetchone()
        total = int(groups["n"] or 0)
        reported = int(groups["r"] or 0)
        return {
            "groups": total,
            "pending": total - reported,
            "reported": reported,
            "scans": int(events["n"] or 0),
        }

    # ------------------------------------------------------------------
    # 举报收档
    # ------------------------------------------------------------------
    def mark_reported(self, names: list[str], *, ts: str | None = None) -> int:
        """把若干组标记为已完成举报(reported=1 + 时间戳);返回更新条数。

        已是 reported 的组再次标记只刷新时间戳(幂等);空名列表零操作。
        """
        stamp = ts or now_iso()
        changed = 0
        for raw in names or []:
            name = str(raw or "").strip()
            if not name:
                continue
            cur = self._conn.execute(
                "UPDATE session_groups SET reported = 1, reported_at = ?"
                " WHERE name = ?",
                (stamp, name),
            )
            changed += cur.rowcount
        self._conn.commit()
        if changed:
            telemetry.inc("sessionstore.reported", changed)
        return changed

    # ------------------------------------------------------------------
    # 存档(结束运行 / 举报收官时调用)
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """当前累积快照(存档内容;含全部组与统计,不携带报告正文)。"""
        groups = self.list_groups()
        return {
            "archived_at": now_iso(),
            "stats": self.stats(),
            "pending_groups": [g.as_dict() for g in groups if not g.reported],
            "reported_groups": [g.as_dict() for g in groups if g.reported],
        }

    def archive(self, path: str | Path) -> str:
        """把 :meth:`snapshot` 写成 JSON 存档文件(临时文件 + 原子替换)。"""
        from netsentinel.atomic import replace_with_retry  # 惰性:兄弟微工具

        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(self.snapshot(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            # Windows 上旧档可能正被 webui/杀毒软件短暂持有:有限重试后放弃
            # (POSIX 行为与裸 os.replace 一致);失败清理残留临时文件。
            replace_with_retry(tmp, out)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        telemetry.inc("sessionstore.archived")
        logger.info("会话累积已存档:%s", out)
        return str(out)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def close(self) -> None:
        """关闭连接(幂等容忍)。"""
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover - 关闭异常无需上抛
            logger.debug("关闭会话累积库连接时出现异常", exc_info=True)

    def __enter__(self) -> "SessionStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
