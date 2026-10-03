"""SQLite 人工复核队列 —— "机器初筛、人工拍板"红线的技术载体(NetSentinel · A10)。

机器初筛(suspect / nsfw)只负责把站点放进队列;是否举报由人在这条队列上逐条拍板:

    pending ──人工确认──> approved ──举报提交完成──> submitted
      └─────人工驳回────> rejected

任何未经人工确认的迁移(例如 pending 直接 submitted)都会被拒绝并抛出
ValueError(中文消息)。本模块只依赖标准库 sqlite3,可独立 CLI 运行:

    python -m netsentinel.decision.review_queue --db data/review_queue.db list
    python -m netsentinel.decision.review_queue show 1
    python -m netsentinel.decision.review_queue approve 1 --note "已人工核实"
    python -m netsentinel.decision.review_queue list --wide          # 完整显示超宽 URL

V5 升级(契约 CONTRACTS-V5.md):连接一律 ``PRAGMA journal_mode=WAL`` +
``busy_timeout=5000``(读写并发 / 跨进程锁等待);entries(status, id) 复合
索引让 ``list(status)`` 免全表扫描,旧库打开时自动补建(CREATE INDEX IF NOT
EXISTS);CLI list 表格默认截断超宽 URL(终端友好,只影响显示不影响数据),
``--wide`` 完整显示;关键动作接入 ``netsentinel.telemetry``
(queue.add / queue.approve / queue.reject / queue.submit 计数,
summary 后回写 queue.total 仪表)。

复核分诊(决策论 triage,默认关闭、显式启用):``list`` 新增仅关键字参数
``sort`` —— 默认 ``sort="fifo"``(id 升序,与历史行为完全一致);显式传
``sort="triage"`` 时接入 ``netsentinel.decision.triage`` 的信息价值排序
(同分按 id 升序 FIFO 平局决断,翻案率从本库复核历史只读聚合)。排序**只影响
返回顺序**,不改变任何条目数据,更不触碰状态机 / 四眼复核 / 人工门语义。
CLI 对应 ``list --sort triage``。

分歧弃权提权消费(默认 0 = 现状):``add`` 新增仅关键字参数
``priority_weight``(非负有限数值,默认 0.0——不传时入库行为与旧版完全
一致),``annotate`` 可在入列后补记/改写。权重只作 ``list(sort="triage")``
的提权加项(经 ``triage.sort_entries(boost_by_url=...)``,同站点多条取
最大值),**不触碰状态机**:pending → approved/rejected → submitted 的
迁移约束、四眼复核与人工门语义一概不变。旧库打开时自动补建
``priority_weight`` 列(``ALTER TABLE ADD COLUMN ... DEFAULT 0.0``,老数据
补 0,行为不变)。典型上游:orchestrator 的分歧弃权接线
(decision.abstain,机器"这题我不答"的直送人工信号)。

事件账本双写(A213 · 事件溯源,默认关闭):``__init__`` 新增仅关键字参数
``event_log``(默认 ``None`` = 现状,**不启用时零事件、零额外 SQL,行为
逐字节不变**)。显式传入 :class:`~netsentinel.storage.event_log.EventLog`
后,``add / approve / reject / mark_submitted / annotate`` 在每次状态变更
**提交前**先把事件 append 进账本(事件类型 ``entry_added`` /
``entry_approved`` / ``entry_rejected`` / ``entry_marked_submitted`` /
``entry_annotated``,可选 ``actor=`` 署名)——**先账本后状态**(崩溃一致性
红线):两步之间进程崩溃时,账本会多一条"已记未生效"事件,而绝不会出现
"状态已变、账本无据";前者由 :func:`netsentinel.storage.replay.replay_review_queue`
对账并如实报告为"事件超前",重放语义按"事件存在但状态可能滞后"处理。
四眼 / 状态机校验逻辑零改动:非法迁移在校验阶段即抛 ValueError,不会产生
任何事件;账本 append 失败时本次状态写入整体回滚(账本与状态都不动)。
"""
from __future__ import annotations

import argparse
import logging
import math
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import SiteReport, now_iso
from netsentinel.decision.triage import (
    SORT_FIFO,
    SORT_TRIAGE,
    OverturnStats,
    sort_entries,
)
from netsentinel.storage.event_log import (
    EVENT_ENTRY_ADDED,
    EVENT_ENTRY_ANNOTATED,
    EVENT_ENTRY_APPROVED,
    EVENT_ENTRY_MARKED_SUBMITTED,
    EVENT_ENTRY_REJECTED,
    EventLog,
)

logger = logging.getLogger(__name__)

#: 合法状态全集(状态机:pending → approved → submitted;pending → rejected)。
VALID_STATUSES: tuple[str, ...] = ("pending", "approved", "rejected", "submitted")

#: 状态中文名(CLI 展示用)。
STATUS_CN: dict[str, str] = {
    "pending": "待复核",
    "approved": "已确认",
    "rejected": "已驳回",
    "submitted": "已提交",
}

#: 判定中文名(CLI 展示用)。
VERDICT_CN: dict[str, str] = {
    "clean": "无风险",
    "suspect": "疑似",
    "nsfw": "高置信色情",
}

#: 默认库路径,与 contracts.Config.db_path 保持一致。
DEFAULT_DB_PATH = "data/review_queue.db"

#: SQLite 忙等上限(毫秒):跨进程并发写时的锁等待窗口(V5 统一)。
BUSY_TIMEOUT_MS: int = 5000

#: CLI list 表格单列默认最大显示宽度(CJK 按双宽计):超宽 URL/路径截断,
#: 终端友好;``--wide`` 可完整显示。仅影响显示,不影响库中数据。
MAX_CELL_DISPLAY_WIDTH: int = 48

__all__ = [
    "BUSY_TIMEOUT_MS",
    "DEFAULT_DB_PATH",
    "MAX_CELL_DISPLAY_WIDTH",
    "STATUS_CN",
    "VALID_STATUSES",
    "VERDICT_CN",
    "Entry",
    "ReviewQueue",
    "main",
]

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    site_url     TEXT    NOT NULL,
    verdict      TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',
    evidence_zip TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL DEFAULT '',
    updated_at   TEXT    NOT NULL DEFAULT '',
    note         TEXT    NOT NULL DEFAULT '',
    priority_weight REAL NOT NULL DEFAULT 0.0
)
"""

#: status 复合索引(status, id):list(status) 等值过滤走索引且天然按 id
#: 有序(免全表扫描 + 免临时排序);IF NOT EXISTS 保证旧库打开时补建。
_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_entries_status ON entries(status, id)"
)

#: 旧库补建 priority_weight 列的 DDL(仅当 PRAGMA table_info 探测到缺失时执行)。
_PRIORITY_WEIGHT_COLUMN_SQL = (
    "ALTER TABLE entries ADD COLUMN priority_weight REAL NOT NULL DEFAULT 0.0"
)


@dataclass
class Entry:
    """复核队列中的一条记录(字段与 entries 表一一对应)。

    注:site_url / verdict 提供空串默认值仅为满足 dataclass 的默认值排序
    规则;正常使用时总是显式传参。``priority_weight`` 为分歧弃权提权加项
    (默认 0.0 = 不提权;只影响 triage 排序,不影响状态机)。
    """

    id: int = 0
    site_url: str = ""
    verdict: str = ""
    status: str = "pending"
    evidence_zip: str = ""
    created_at: str = ""
    updated_at: str = ""
    note: str = ""
    priority_weight: float = 0.0


class ReviewQueue:
    """基于 SQLite 的人工复核队列。

    自动创建父目录与 entries 表(IF NOT EXISTS),可重复打开同一数据库。
    支持 ``with`` 上下文;写操作即时 commit。

    :param event_log: 事件账本(A213 双写,默认 ``None`` = 现状零事件)。
        启用后 ``add / approve / reject / mark_submitted / annotate`` 在状态
        变更**提交前**先 append 事件(先账本后状态,崩溃一致性);本类只
        持有引用,**不拥有其生命周期**(close 不会关闭账本,由调用方管理)。
    """

    def __init__(
        self, db_path: str | Path, *, event_log: EventLog | None = None
    ) -> None:
        self.db_path = str(db_path)
        #: 事件账本(None = 不启用双写,行为与旧版逐字节一致)。
        self._event_log = event_log
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        # V5:一律 WAL + 忙等 5s(读写并发 / 跨进程锁等待),
        # 与 vlm_cache 等存储模块同一连接策略。
        self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA_SQL)
        # V5:status 索引旧库补建(IF NOT EXISTS,老数据打开即享受索引)。
        self._conn.execute(_INDEX_SQL)
        # 分歧弃权提权:priority_weight 列旧库补建(缺列才 ALTER,老数据补 0)。
        columns = {
            str(row[1]) for row in self._conn.execute("PRAGMA table_info(entries)")
        }
        if "priority_weight" not in columns:
            self._conn.execute(_PRIORITY_WEIGHT_COLUMN_SQL)
        self._conn.commit()
        logger.debug("复核队列已就绪:%s", self.db_path)

    # ------------------------------------------------------------------
    # 基础读写
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_priority_weight(value: Any) -> float:
        """校验提权加项:非负有限数值(bool 拒绝),非法抛中文 ValueError。"""
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError(f"提权权重必须是有限数值:{value!r}")
        number = float(value)
        if number < 0.0:
            raise ValueError(f"提权权重必须是非负数值:{value!r}")
        return number

    def _emit(
        self,
        event_type: str,
        entry_id: int,
        *,
        actor: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        """把事件追加进账本(未启用 = 零操作,不发一条 SQL)。

        调用时机契约:**必须在状态变更提交之前**(先账本后状态);调用方
        负责"事件 append 失败 → 状态写入回滚"的配对处理。
        """
        if self._event_log is None:
            return
        self._event_log.append(event_type, entry_id, actor=actor, payload=payload)

    def add(
        self,
        report: SiteReport,
        evidence_zip: str = "",
        *,
        note: str = "",
        priority_weight: float = 0.0,
        actor: str = "",
    ) -> int:
        """把一份站点报告加入队列(初始 pending),返回新条目 id。

        :param note: 备注留痕(V6 批量流程用它携带 ``[组:案件组名]`` 标记,
            供 batch_tui/batch_review 识别分组;空串保持 v1 行为)。
        :param priority_weight: 分歧弃权提权加项(非负有限数值,默认 0.0 =
            不提权,入库行为与旧版完全一致)。典型来源:decision.abstain 的
            ``to_triage_hint(...)["priority_weight"]``;只影响后续
            ``list(sort="triage")`` 的排序,不触碰状态机。
        :param actor: 操作署名(仅写入事件账本,不影响 entries 表;启用账本
            时留痕"谁把站点放进队列")。
        :raises ValueError: priority_weight 非法时(中文消息)。
        """
        verdict = getattr(report.verdict, "value", None)
        if verdict is None:  # 容忍鸭子类型:直接给了字符串
            verdict = str(report.verdict)
        weight = self._validate_priority_weight(priority_weight)
        ts = now_iso()
        cur = self._conn.execute(
            "INSERT INTO entries (site_url, verdict, status, evidence_zip,"
            " created_at, updated_at, note, priority_weight)"
            " VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)",
            (report.site_url, verdict, evidence_zip, ts, ts, str(note or ""), weight),
        )
        entry_id = int(cur.lastrowid or 0)
        if self._event_log is None:
            # 现状路径:SQL 语句序列与旧版完全一致(INSERT → commit;
            # lastrowid 在 commit 前读取,游标属性不受影响)。
            self._conn.commit()
        else:
            # 先账本后状态:entry_added 事件先行落盘提交,随后才提交
            # entries 写入;两步之间崩溃 = 账本多一条未生效事件(重放
            # 对账按"事件超前"报告),绝不会出现状态已变而账本无据。
            try:
                self._emit(
                    EVENT_ENTRY_ADDED,
                    entry_id,
                    actor=actor,
                    payload={
                        "site_url": report.site_url,
                        "verdict": verdict,
                        "evidence_zip": evidence_zip,
                        "note": str(note or ""),
                        "priority_weight": weight,
                        "status": "pending",
                    },
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        logger.info(
            "入列待复核:id=%s site=%s verdict=%s", entry_id, report.site_url, verdict
        )
        telemetry.inc("queue.add")
        return entry_id

    def list(
        self, status: str | None = None, *, sort: str = SORT_FIFO
    ) -> list[Entry]:
        """按 id 升序列出条目;给定 status 时只返回该状态的条目。

        :param sort: 返回顺序 —— ``"fifo"``(默认:id 升序,历史行为完全
            不变)或 ``"triage"``(decision.triage 决策论分诊:按信息价值
            优先级降序,同分按 id 升序 FIFO 平局决断;翻案率估计器从本库
            entries 复核历史只读聚合,不改 schema)。排序只影响返回顺序,
            不改变任何条目数据,也不触碰状态机 / 四眼复核 / 人工门语义。
            triage 排序同时消费本库的 ``priority_weight`` 提权加项
            (同站点多条取最大值,经 ``sort_entries(boost_by_url=...)``)。
        :raises ValueError: sort 取值非法时(中文消息)。
        """
        if status is None:
            rows = self._conn.execute(
                "SELECT * FROM entries ORDER BY id ASC"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM entries WHERE status = ? ORDER BY id ASC", (status,)
            ).fetchall()
        entries = [self._row_to_entry(r) for r in rows]
        if sort == SORT_FIFO:
            return entries
        if sort == SORT_TRIAGE:
            stats = OverturnStats.from_sqlite(self._conn)
            # 分歧弃权提权:priority_weight > 0 的站点聚合为 boost 映射
            # (同站点多条取最大值);全 0 时传 None,排序口径与旧版一致。
            boosts: dict[str, float] = {}
            for e in entries:
                weight = float(getattr(e, "priority_weight", 0.0) or 0.0)
                if weight > 0.0:
                    boosts[e.site_url] = max(boosts.get(e.site_url, 0.0), weight)
            return sort_entries(
                entries, overturn_stats=stats, boost_by_url=(boosts or None)
            )
        raise ValueError(
            f"未知的排序方式:{sort}(仅支持 {SORT_FIFO} / {SORT_TRIAGE})"
        )

    def get(self, id: int) -> Entry | None:
        """按 id 取单条;不存在返回 None。"""
        row = self._conn.execute(
            "SELECT * FROM entries WHERE id = ?", (id,)
        ).fetchone()
        return self._row_to_entry(row) if row is not None else None

    def summary(self) -> dict[str, int]:
        """各状态条目计数(四种状态键始终齐全)。

        统计完成后回写 ``telemetry.gauge("queue.total", 总数)``,
        供运营面板观察队列规模(V5 可观测)。
        """
        counts = {s: 0 for s in VALID_STATUSES}
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM entries GROUP BY status"
        ).fetchall()
        for row in rows:
            counts[row["status"]] = int(row["n"])
        telemetry.gauge("queue.total", sum(counts.values()))
        return counts

    def annotate(
        self, id: int, *, priority_weight: float, actor: str = ""
    ) -> Entry:
        """批注/改写一条条目的提权加项(分歧弃权信号的后续补充入口)。

        只写 ``priority_weight`` 与 ``updated_at`` 两个非状态字段,
        **不触碰状态机**:status / note / verdict 原样保留,任何状态
        (pending / approved / rejected / submitted)的条目都可批注——
        提权只是排序提示,不是状态迁移,更不是人工确认。返回更新后的条目。

        :param actor: 操作署名(仅写入事件账本,不影响 entries 表)。
        :raises ValueError: 条目不存在或 priority_weight 非法时(中文消息)。
        """
        weight = self._validate_priority_weight(priority_weight)
        row = self._conn.execute(
            "SELECT * FROM entries WHERE id = ?", (id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"复核条目不存在:id={id}")
        previous_weight = float(row["priority_weight"] or 0.0)
        ts = now_iso()
        self._conn.execute(
            "UPDATE entries SET priority_weight = ?, updated_at = ? WHERE id = ?",
            (weight, ts, id),
        )
        if self._event_log is None:
            self._conn.commit()
        else:
            # 先账本后状态:entry_annotated 事件先落盘(payload 记录改写
            # 前后的权重,重放可重建最终提权值),再提交 entries 写入。
            try:
                self._emit(
                    EVENT_ENTRY_ANNOTATED,
                    id,
                    actor=actor,
                    payload={
                        "priority_weight": weight,
                        "previous_weight": previous_weight,
                    },
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        entry = self._row_to_entry(row)
        entry.priority_weight = weight
        entry.updated_at = ts
        telemetry.inc("queue.annotate")
        logger.info(
            "复核条目 %s 批注提权加项 %g(状态与备注不变,仍是 %s)",
            id,
            weight,
            STATUS_CN.get(entry.status, entry.status),
        )
        return entry

    # ------------------------------------------------------------------
    # 状态机(人工拍板)
    # ------------------------------------------------------------------

    def approve(self, id: int, note: str = "", *, actor: str = "") -> Entry:
        """人工确认:pending → approved(进入举报流程的唯一入口)。

        :param actor: 操作署名(仅写入事件账本;四眼复核的审核人姓名适合
            经此留痕,不影响 entries 表与校验逻辑)。
        """
        entry = self._transition(
            id,
            ("pending",),
            "approved",
            "人工确认",
            note,
            event_type=EVENT_ENTRY_APPROVED,
            actor=actor,
        )
        telemetry.inc("queue.approve")
        return entry

    def reject(self, id: int, note: str = "", *, actor: str = "") -> Entry:
        """人工驳回:pending → rejected(驳回后不可再确认或提交)。

        :param actor: 操作署名(仅写入事件账本)。
        """
        entry = self._transition(
            id,
            ("pending",),
            "rejected",
            "人工驳回",
            note,
            event_type=EVENT_ENTRY_REJECTED,
            actor=actor,
        )
        telemetry.inc("queue.reject")
        return entry

    def mark_submitted(self, id: int, *, actor: str = "") -> Entry:
        """举报提交完成后回写:approved → submitted。

        :param actor: 操作署名(仅写入事件账本)。
        """
        entry = self._transition(
            id,
            ("approved",),
            "submitted",
            "标记已提交",
            None,
            event_type=EVENT_ENTRY_MARKED_SUBMITTED,
            actor=actor,
        )
        telemetry.inc("queue.submit")
        return entry

    def _transition(
        self,
        entry_id: int,
        allowed_from: tuple[str, ...],
        to_status: str,
        action_cn: str,
        note: str | None,
        *,
        event_type: str = "",
        actor: str = "",
    ) -> Entry:
        row = self._conn.execute(
            "SELECT * FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"复核条目不存在:id={entry_id}")
        current = row["status"]
        if current not in allowed_from:
            allowed = " 或 ".join(
                f"{STATUS_CN.get(s, s)}({s})" for s in allowed_from
            )
            raise ValueError(
                f"复核条目 {entry_id} 当前状态为 "
                f"{STATUS_CN.get(current, current)}({current}),"
                f"只有 {allowed} 状态的条目才能{action_cn}"
            )
        ts = now_iso()
        # 传入空备注时保留原备注,不静默清空。
        new_note = note if note else (row["note"] or "")
        self._conn.execute(
            "UPDATE entries SET status = ?, updated_at = ?, note = ? WHERE id = ?",
            (to_status, ts, new_note, entry_id),
        )
        if self._event_log is None:
            self._conn.commit()
        else:
            # 先账本后状态:状态迁移事件(payload 记 from/to 状态与最终备注)
            # 先行落盘提交,随后才提交 entries 写入;两步之间崩溃 = 账本多
            # 一条未生效事件(重放对账按"事件超前"报告,给出准确 seq)。
            # 状态机校验(上方)在事件产生之前完成:非法迁移零事件。
            try:
                self._emit(
                    event_type,
                    entry_id,
                    actor=actor,
                    payload={
                        "from_status": current,
                        "to_status": to_status,
                        "note": new_note,
                    },
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        logger.info("复核条目 %s:%s(%s → %s)", entry_id, action_cn, current, to_status)
        entry = self._row_to_entry(row)
        entry.status = to_status
        entry.updated_at = ts
        entry.note = new_note
        return entry

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> Entry:
        return Entry(
            id=int(row["id"]),
            site_url=row["site_url"] or "",
            verdict=row["verdict"] or "",
            status=row["status"] or "pending",
            evidence_zip=row["evidence_zip"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            note=row["note"] or "",
            priority_weight=float(row["priority_weight"] or 0.0),
        )

    def close(self) -> None:
        """关闭底层连接(幂等容忍)。"""
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover - 关闭异常无需上抛
            logger.debug("关闭复核队列连接时出现异常", exc_info=True)

    def __enter__(self) -> ReviewQueue:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# CLI:python -m netsentinel.decision.review_queue
# ---------------------------------------------------------------------------

def _display_width(text: str) -> int:
    """近似显示宽度:CJK / 全角字符按 2 列计,其余按 1 列。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _status_label(status: str) -> str:
    return f"{STATUS_CN.get(status, '未知')}({status})"


def _verdict_label(verdict: str) -> str:
    if not verdict:
        return "-"
    return f"{VERDICT_CN.get(verdict, '未知')}({verdict})"


def _truncate(text: str, width: int | None = MAX_CELL_DISPLAY_WIDTH) -> str:
    """把超宽单元格截断到 ``width`` 显示列(CJK 双宽计),尾部加省略号。

    ``width=None`` 时不截断(CLI ``--wide``);只改显示,不改数据。
    """
    if width is None or _display_width(text) <= width:
        return text
    kept: list[str] = []
    used = 0
    for ch in text:
        ch_width = 2 if ord(ch) > 0x2E7F else 1
        if used + ch_width > width - 1:  # 预留 1 列给省略号
            break
        kept.append(ch)
        used += ch_width
    return "".join(kept) + "…"


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [_display_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _display_width(cell))
    print("  ".join(_pad(h, widths[i]) for i, h in enumerate(headers)).rstrip())
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(_pad(c, widths[i]) for i, c in enumerate(row)).rstrip())


def _build_parser() -> argparse.ArgumentParser:
    # --db 在顶层与子命令两级都可用:顶层提供真实默认值,
    # 子命令级用 SUPPRESS,避免未传参时覆盖顶层已解析的值。
    db_top = argparse.ArgumentParser(add_help=False)
    db_top.add_argument(
        "--db", default=DEFAULT_DB_PATH, help="SQLite 队列数据库路径(默认:%(default)s)"
    )
    db_sub = argparse.ArgumentParser(add_help=False)
    db_sub.add_argument("--db", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(
        prog="python -m netsentinel.decision.review_queue",
        description="净网哨兵 · 人工复核队列(机器初筛,人工拍板)",
        parents=[db_top],
    )
    sub = parser.add_subparsers(
        dest="command", required=True, metavar="{list,show,approve,reject}"
    )

    p_list = sub.add_parser("list", help="列出复核条目", parents=[db_sub])
    p_list.add_argument(
        "--status", choices=VALID_STATUSES, default=None, help="只看指定状态"
    )
    p_list.add_argument(
        "--sort",
        choices=(SORT_FIFO, SORT_TRIAGE),
        default=SORT_FIFO,
        help=(
            "返回顺序:fifo=id 升序(默认,历史行为不变);"
            "triage=决策论分诊,按信息价值优先(同分按 id FIFO 平局,"
            "仅影响显示顺序,不改变状态机与人工门)"
        ),
    )
    p_list.add_argument(
        "--wide",
        action="store_true",
        help=f"完整显示超宽 URL/路径(默认每列截断到 {MAX_CELL_DISPLAY_WIDTH} 显示列)",
    )

    p_show = sub.add_parser("show", help="查看单个条目详情", parents=[db_sub])
    p_show.add_argument("id", type=int, help="条目编号")

    p_approve = sub.add_parser(
        "approve", help="人工确认(仅待复核条目)", parents=[db_sub]
    )
    p_approve.add_argument("id", type=int, help="条目编号")
    p_approve.add_argument("--note", default="", help="确认备注(可选)")

    p_reject = sub.add_parser(
        "reject", help="人工驳回(仅待复核条目)", parents=[db_sub]
    )
    p_reject.add_argument("id", type=int, help="条目编号")
    p_reject.add_argument("--note", default="", help="驳回原因(可选)")
    return parser


def _cmd_list(
    queue: ReviewQueue,
    status: str | None,
    *,
    wide: bool = False,
    sort: str = SORT_FIFO,
) -> int:
    entries = queue.list(status, sort=sort)
    if not entries:
        if status:
            print(f"按状态筛选 {_status_label(status)}:共 0 条。")
        else:
            print("复核队列为空,暂无条目。")
        return 0
    limit: int | None = None if wide else MAX_CELL_DISPLAY_WIDTH
    headers = ["编号", "站点", "判定", "状态", "证据包", "更新时间"]
    rows = [
        [
            str(e.id),
            _truncate(e.site_url, limit),
            _verdict_label(e.verdict),
            _status_label(e.status),
            _truncate(e.evidence_zip or "-", limit),
            e.updated_at or "-",
        ]
        for e in entries
    ]
    _print_table(headers, rows)
    stats = queue.summary()
    print()
    print(
        f"统计(共 {sum(stats.values())} 条):"
        f"待复核 {stats['pending']} · 已确认 {stats['approved']} · "
        f"已驳回 {stats['rejected']} · 已提交 {stats['submitted']}"
    )
    return 0


def _cmd_show(queue: ReviewQueue, entry_id: int) -> int:
    e = queue.get(entry_id)
    if e is None:
        raise ValueError(f"复核条目不存在:id={entry_id}")
    print(f"编号    : {e.id}")
    print(f"站点    : {e.site_url}")
    print(f"判定    : {_verdict_label(e.verdict)}")
    print(f"状态    : {_status_label(e.status)}")
    print(f"证据包  : {e.evidence_zip or '(尚未打包)'}")
    print(f"创建时间: {e.created_at or '-'}")
    print(f"更新时间: {e.updated_at or '-'}")
    print(f"备注    : {e.note or '-'}")
    return 0


def _cmd_approve(queue: ReviewQueue, entry_id: int, note: str) -> int:
    e = queue.approve(entry_id, note)
    print(f"条目 {e.id} 已人工确认:{_status_label('pending')} → {_status_label(e.status)}")
    if note:
        print(f"备注:{note}")
    print("该条目现在可以进入举报提交流程(提交时仍须通过人工门)。")
    return 0


def _cmd_reject(queue: ReviewQueue, entry_id: int, note: str) -> int:
    e = queue.reject(entry_id, note)
    print(f"条目 {e.id} 已人工驳回:{_status_label('pending')} → {_status_label(e.status)}")
    if note:
        print(f"备注:{note}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 入口:返回 0 成功;未知条目或非法状态迁移返回 1(中文错误)。"""
    args = _build_parser().parse_args(argv)
    queue = ReviewQueue(args.db)
    try:
        if args.command == "list":
            return _cmd_list(
                queue,
                args.status,
                wide=getattr(args, "wide", False),
                sort=getattr(args, "sort", SORT_FIFO),
            )
        if args.command == "show":
            return _cmd_show(queue, args.id)
        if args.command == "approve":
            return _cmd_approve(queue, args.id, args.note)
        if args.command == "reject":
            return _cmd_reject(queue, args.id, args.note)
        raise ValueError(f"未知子命令:{args.command}")  # pragma: no cover - argparse 已拦截
    except ValueError as exc:
        print(f"错误:{exc}", file=sys.stderr)
        return 1
    finally:
        queue.close()


if __name__ == "__main__":
    raise SystemExit(main())
