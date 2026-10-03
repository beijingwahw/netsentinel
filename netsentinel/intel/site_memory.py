"""站点指纹记忆与重扫决策(A33)。

依据 CONTRACTS-V2.md §3 A33 条目:定期巡查同样站点会浪费预算,本模块
用一份轻量 SQLite 记忆库记住每个站点"上次收录时的内容指纹",下一轮
巡查前先问 ``should_rescan``——指纹未变且未过 TTL(默认 72 小时)的
站点整站跳过,连采集都省掉;站点内容变化、超过 TTL 或首次见到时才
重新扫描。

指纹素材 = 排序后的页面 URL 列表 + 排序后的全部图片 sha256
(无图时退化为仅 URL 列表),整体取 sha256 十六进制前 16 位。

只依赖标准库 sqlite3 / hashlib / threading / datetime;连接以
``check_same_thread=False`` 建立,统一启用 ``PRAGMA journal_mode=WAL`` 与
``busy_timeout=5000``(V5:多读者单写者并发更好、锁冲突时等待而非立刻
SQLITE_BUSY),全部读写经同一把 ``threading.Lock`` 串行化,可在多线程巡查
(scheduler)中安全共用;库文件损坏时告警并删除重建(记忆丢了只会多扫一轮,
不影响正确性——安全方向失效)。

用法示例::

    with SiteMemory("data/site_memory.db", ttl_hours=72) as mem:
        fp = mem.fingerprint(report)
        need, reason = mem.should_rescan("https://example.com/", fp)
        if need:
            mem.remember("https://example.com/", fp)

V10.4 追加**单次决策 TTL 覆写**(向后兼容):``should_rescan`` 增可选
``ttl_hours`` 参数——缺省 ``None`` 时沿用库级 ``self.ttl_hours``
(默认 72 小时,四分支语义与消息逐字节不变);显式传入则**仅对当次
决策**生效,库级 TTL 与既有调用方不受影响。这是给爆发检测联动的
装配缝:团伙扩张期把建议 TTL 收短、稳态回默认,详见 :mod:`netsentinel.intel.temporal`。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import sqlite3
import threading
from pathlib import Path

from netsentinel import telemetry
from netsentinel.contracts import SiteReport

__all__ = ["DEFAULT_DB_PATH", "BUSY_TIMEOUT_MS", "SiteMemory"]

logger = logging.getLogger(__name__)

#: 默认库路径(与 scheduler 的 <data_dir>/site_memory.db 约定一致)。
DEFAULT_DB_PATH = "data/site_memory.db"

#: 连接统一启用的忙等待上限(毫秒):并发写锁冲突时最多等这么久
BUSY_TIMEOUT_MS = 5000

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sites (
    site_url    TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT ''
)
"""


def _parse_ts(ts: str) -> _dt.datetime | None:
    """解析 ISO8601 时间戳;无时区信息按 UTC 对待;失败返回 None。"""
    if not ts:
        return None
    try:
        dt = _dt.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt


#: 时间戳解析缓存上限(满则整体清空,简单防内存无界;标准库实现)
_TS_CACHE_MAX = 1024
_TS_CACHE: dict[str, "_dt.datetime | None"] = {}
_TS_MISSING: object = object()  # 哨兵:区分"未缓存"与"缓存了 None"


def _parse_ts_cached(ts: str) -> _dt.datetime | None:
    """带进程内缓存的 :func:`_parse_ts`(V5 性能小函数)。

    巡查循环对同一站点反复 ``should_rescan`` 时,``updated_at`` 字符串
    高度重复(秒级精度),缓存让 ``fromisoformat`` 只对每个不同字符串执行
    一次。缓存只存"字符串 → 解析结果",键为库内时间戳、不含站点 URL 或
    任何密钥。满 :data:`_TS_CACHE_MAX` 条整体清空(简单截断,标准库、
    零新依赖);dict 读写原子性由 GIL 保证,并发下最坏情形是重复解析一次。
    """
    hit = _TS_CACHE.get(ts, _TS_MISSING)
    if hit is not _TS_MISSING:
        return hit  # type: ignore[return-value]  # 哨兵已排除"未缓存"
    if len(_TS_CACHE) >= _TS_CACHE_MAX:
        _TS_CACHE.clear()
    value = _parse_ts(ts)
    _TS_CACHE[ts] = value
    return value


def _apply_connection_pragmas(conn: sqlite3.Connection) -> None:
    """对新连接启用 WAL 日志与忙等待(幂等;WAL 失败时降级为默认日志)。"""
    try:
        conn.execute("PRAGMA journal_mode=WAL").fetchone()
    except sqlite3.DatabaseError:  # 个别文件系统不支持 WAL:沿用默认日志
        logger.debug("WAL 日志模式启用失败,沿用默认日志模式", exc_info=True)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")


class SiteMemory:
    """基于 SQLite 的站点指纹记忆:记住指纹,决定"要不要再扫一次"。

    - ``fingerprint(report)``:对报告内容取 16 位十六进制指纹(纯函数性质,
      与库状态无关,页序 / 图序无关);
    - ``remember(url, fp)``:UPSERT 指纹与更新时间;
    - ``should_rescan(url, fp)``:四分支决策——首见 / 未变未过期(跳过)/
      内容已变化 / 超过 TTL。

    线程安全:所有数据库访问都持 ``self._lock``;支持 ``with`` 上下文。
    """

    def __init__(self, db_path: str, ttl_hours: int = 72) -> None:
        self.db_path = str(db_path)
        self.ttl_hours = int(ttl_hours)
        self._lock = threading.Lock()
        parent = Path(self.db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._open()
        logger.debug("站点指纹记忆已就绪:%s(TTL=%sh)", self.db_path, self.ttl_hours)

    # ------------------------------------------------------------------
    # 连接与损坏重建
    # ------------------------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        """打开(或新建)数据库;文件损坏时删除重建为空库。

        每个新连接统一启用 WAL 日志与忙等待(:func:`_apply_connection_pragmas`,
        V5):WAL 允许读写并发、busy_timeout 让短暂锁冲突等待 5 秒而不是
        立刻抛 SQLITE_BUSY。
        """
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            _apply_connection_pragmas(conn)
            conn.execute(_SCHEMA_SQL)
            conn.commit()
            return conn
        except sqlite3.DatabaseError:
            logger.warning(
                "站点指纹记忆库损坏,已删除重建(旧记忆丢失,仅导致多扫一轮):%s",
                self.db_path,
                exc_info=True,
            )
            conn.close()
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(self.db_path + suffix).unlink(missing_ok=True)
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            _apply_connection_pragmas(conn)
            conn.execute(_SCHEMA_SQL)
            conn.commit()
            return conn

    # ------------------------------------------------------------------
    # 时间注入缝:测试用 monkeypatch 替换 _now 即可推进 / 回拨时钟
    # ------------------------------------------------------------------

    def _now(self) -> _dt.datetime:
        """当前时刻(本地时区,带时区信息;与 contracts.now_iso 同口径)。"""
        return _dt.datetime.now(_dt.timezone.utc).astimezone()

    def _now_iso(self) -> str:
        return self._now().isoformat(timespec="seconds")

    # ------------------------------------------------------------------
    # 指纹
    # ------------------------------------------------------------------

    def fingerprint(self, report: SiteReport) -> str:
        """计算站点内容指纹:sha256(排序后页面 URL 列表 + 排序后全部图片 sha256)。

        素材序列化规则(逐行 ``u:<url>`` / ``i:<sha>``,带段落标题,避免
        不同集合拼出相同字符串):先 ``pages:`` 段的排序去重 URL,再有图片时
        追加 ``images:`` 段的排序去重 sha256。图片 sha256 取自各页
        ``image_evidences`` 与 ``report.image_scores`` 的并集;无图时退化为
        仅 URL 列表。返回 sha256 hexdigest 前 16 位,对页序 / 图序不敏感。
        """
        page_urls = sorted({p.url for p in report.pages if p.url})
        shas: set[str] = set()
        for page in report.pages:
            for img in page.image_evidences:
                if img.sha256:
                    shas.add(img.sha256)
        for score in report.image_scores:
            sha = getattr(getattr(score, "image", None), "sha256", "")
            if sha:
                shas.add(sha)
        material = "pages:\n" + "\n".join(f"u:{u}" for u in page_urls)
        if shas:
            material += "\nimages:\n" + "\n".join(f"i:{s}" for s in sorted(shas))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    # ------------------------------------------------------------------
    # 记忆读写
    # ------------------------------------------------------------------

    def remember(self, site_url: str, fingerprint: str) -> None:
        """记住站点当前指纹(UPSERT:存在则更新指纹与 updated_at)。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO sites (site_url, fingerprint, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(site_url) DO UPDATE SET "
                "fingerprint = excluded.fingerprint, updated_at = excluded.updated_at",
                (site_url, fingerprint, self._now_iso()),
            )
            self._conn.commit()
        logger.debug("已记住站点指纹:%s -> %s", site_url, fingerprint)

    def last_fingerprint(self, site_url: str) -> str:
        """读回记忆库中该站点最近一次记住的指纹;无记录返回空串。

        (A39 scheduler 跨轮去重会用该读口;不提供时它退化为进程内兜底表。)
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT fingerprint FROM sites WHERE site_url = ?", (site_url,)
            ).fetchone()
        return str(row["fingerprint"]) if row is not None else ""

    def should_rescan(
        self,
        site_url: str,
        fingerprint: str,
        ttl_hours: float | None = None,
    ) -> tuple[bool, str]:
        """重扫决策,返回 (是否需要重扫, 中文原因)。

        四分支:

        1. 无记录 → ``(True, "首次收录")``;
        2. 指纹不同 → ``(True, "站点内容已变化")``;
        3. 指纹相同且距 updated_at 不足 ``effective_ttl`` →
           ``(False, "指纹未变化,距上次 N 小时…")``(并累加遥测计数
           ``site_memory.rescan_skip``,V5);
        4. 指纹相同但已过 TTL(含 updated_at 无法解析的旧脏数据,按过期
           处理,安全方向)→ ``(True, "超过 TTL 需复查")``。

        时间戳解析走进程内缓存(:func:`_parse_ts_cached`),同一
        updated_at 字符串不重复 fromisoformat。

        :param ttl_hours: 单次决策的 TTL 覆写(小时)。``None``(缺省)=
            沿用库级 ``self.ttl_hours``,消息与既有行为逐字节一致
            (向后兼容);显式传入则仅本次生效,经 ``int()`` 截断到整
            小时(小数 TTL 向零取整),负值视同立即过期(安全方向:
            多扫不漏扫)。指纹与四分支语义零改动。

        V10.4 联动装配点(**只读说明**;scheduler / adaptive / graph 均
        在本模块领地外,不在此处改动它们):调用方把图谱
        :mod:`netsentinel.intel.graph` 中与本站点相关的
        ``phash_near`` / ``redirect`` 边 ``created_at`` 时间戳(换算为
        小时序)喂给 :func:`netsentinel.intel.temporal.kleinberg_bursts`
        做爆发检测,经 :func:`~netsentinel.intel.temporal.burst_factor`
        取当前强度、 :func:`~netsentinel.intel.temporal.suggest_ttl`
        得建议 TTL 后,以 ``ttl_hours=建议值`` 传入本方法——团伙扩张期
        (边密集出现)自动缩短相关站点的重扫间隔,稳态维持基准 TTL
        省预算。红线:建议 TTL 只影响"指纹未变时跳过多久"这一档,
        **不绕过任何频控**——重扫频率仍受全局礼貌间隔与频控钳制约束。
        """
        effective_ttl = self.ttl_hours if ttl_hours is None else int(ttl_hours)
        with self._lock:
            row = self._conn.execute(
                "SELECT fingerprint, updated_at FROM sites WHERE site_url = ?",
                (site_url,),
            ).fetchone()
        if row is None:
            return True, "首次收录"
        if (row["fingerprint"] or "") != fingerprint:
            return True, "站点内容已变化"

        ts = _parse_ts_cached(row["updated_at"] or "")
        if ts is None:
            return True, "超过 TTL 需复查"
        age_hours = max(0.0, (self._now() - ts).total_seconds() / 3600.0)
        if age_hours >= effective_ttl:
            return True, "超过 TTL 需复查"
        telemetry.inc("site_memory.rescan_skip")
        return (
            False,
            f"指纹未变化,距上次 {age_hours:.1f} 小时,"
            f"未超 TTL({effective_ttl} 小时),本轮跳过",
        )

    # ------------------------------------------------------------------
    # 统计与生命周期
    # ------------------------------------------------------------------

    def stats(self) -> int:
        """记忆库当前记录的站点数(sites 表行数)。"""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM sites").fetchone()
        return int(row["n"])

    def close(self) -> None:
        """关闭底层连接(幂等容忍)。"""
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover - 关闭异常无需上抛
            logger.debug("关闭站点指纹记忆连接时出现异常", exc_info=True)

    def __enter__(self) -> SiteMemory:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
