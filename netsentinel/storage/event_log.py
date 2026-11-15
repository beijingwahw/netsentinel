"""事件账本 · append-only 事件溯源存储(NetSentinel · A213)。

对标 event sourcing 的"事实不可变,状态只是投影":复核队列的每一次状态变更
(pending → approved/rejected → submitted、批注提权)都以**事件**的形式先落盘
到本账本,队列库里的 entries 行只是事件的投影(snapshot)。有了这份账本,
任何时刻都能离线**重放**(:mod:`netsentinel.storage.replay`)重建每条条目
的完整操作时间线,并与队列现状对账——audit.jsonl 只是"日志",删了就没了;
本账本是"事实",删改会被数据库本身拒绝。

与统一存储底座 :class:`~netsentinel.storage.kernel.SQLiteKernel` 的关系:
本模块**组合**内核(连接策略 WAL + busy_timeout=5000 + 单连接 + 全局锁,
线程安全;版本化迁移按需使用,这里只建一张表无需版本号),遵循其惯例而不
改动它。

append-only 的三层保证:

1. **API 层**:本类只暴露 ``append`` / ``append_many`` / ``iter_events`` /
   ``query`` / ``latest_seq`` 五个数据面方法,没有任何 UPDATE / DELETE 路径;
2. **数据库层**:``BEFORE UPDATE`` / ``BEFORE DELETE`` 触发器对 events 表
   一律 ``RAISE(ABORT, 中文消息)``——绕过本类、拿裸 sqlite3 连接直改也会被
   拒绝(想篡改必须先 DROP TRIGGER,而触发器被 DROP 这件事本身就会让重放
   对账发现事实与投影的分歧);
3. **重放层**::func:`netsentinel.storage.replay.replay_review_queue` 把账本
   重建的投影与队列现状逐条对账,账本被动过(哪怕重建触发器后改回去的
   payload)都会以"状态不一致 / 字段不一致"的差异形式暴露。

崩溃一致性(先账本后状态):写入方(:class:`~netsentinel.decision.review_queue.ReviewQueue`
双写模式)保证事件先于状态变更**提交**落盘;两步之间崩溃时,账本会比状态
多一条"已记未生效"事件——这是事件溯源的标准语义(事件存在但投影可能滞后),
由重放对账如实报告为"事件超前",而不是数据错误。

事件类型(先覆盖复核域,常量见模块底部导出):

- ``entry_added``           入列(site_url/verdict/note/priority_weight 快照)
- ``entry_approved``        人工确认 pending → approved
- ``entry_rejected``        人工驳回 pending → rejected
- ``entry_marked_submitted`` approved → submitted 回写
- ``entry_annotated``       批注提权加项(priority_weight 改写)

表结构(spec 对齐)::

    events(seq INTEGER PRIMARY KEY, ts TEXT, event_type TEXT,
           entry_id INTEGER, actor TEXT, payload_json TEXT)

seq 为 rowid 别名,提交成功的事件天然连续(1..N,无洞不重,并发以内核锁 +
WAL 忙等串行化;回滚的 INSERT 不占号)。

损坏安全:打开非 SQLite 文件 / 账本字节损坏时抛中文 :class:`EventLogError`
(fail-safe,绝不把损坏数据当正常事件吐出);锁等待(BUSY)类错误原样上抛,
不误报为损坏。

用法::

    from netsentinel.storage.event_log import EventLog
    with EventLog("data/review_events.db") as log:
        seq = log.append("entry_added", 1, actor="张三",
                         payload={"site_url": "http://x.test"})
        for ev in log.query(entry_id=1):
            print(ev.seq, ev.event_type, ev.payload)

零第三方依赖(仅标准库 sqlite3 / json / threading)。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import now_iso
from netsentinel.storage.kernel import BUSY_TIMEOUT_MS, SQLiteKernel

logger = logging.getLogger(__name__)

__all__ = [
    "BUSY_TIMEOUT_MS",
    "EVENT_ENTRY_ADDED",
    "EVENT_ENTRY_ANNOTATED",
    "EVENT_ENTRY_APPROVED",
    "EVENT_ENTRY_MARKED_SUBMITTED",
    "EVENT_ENTRY_REJECTED",
    "REVIEW_EVENT_TYPES",
    "Event",
    "EventLog",
    "EventLogError",
]

logger = logging.getLogger(__name__)

#: 入列事件:一条新条目进入待复核(site_url/verdict/note 等初始快照入 payload)。
EVENT_ENTRY_ADDED = "entry_added"

#: 人工确认事件:pending → approved(进入举报流程的唯一入口)。
EVENT_ENTRY_APPROVED = "entry_approved"

#: 人工驳回事件:pending → rejected(终态)。
EVENT_ENTRY_REJECTED = "entry_rejected"

#: 提交回写事件:approved → submitted(举报提交完成)。
EVENT_ENTRY_MARKED_SUBMITTED = "entry_marked_submitted"

#: 批注事件:改写提权加项 priority_weight(非状态迁移,只影响 triage 排序)。
EVENT_ENTRY_ANNOTATED = "entry_annotated"

#: 复核域事件类型全集(replay 可应用;账本本身不限制,留给未来域扩展)。
REVIEW_EVENT_TYPES: tuple[str, ...] = (
    EVENT_ENTRY_ADDED,
    EVENT_ENTRY_APPROVED,
    EVENT_ENTRY_REJECTED,
    EVENT_ENTRY_MARKED_SUBMITTED,
    EVENT_ENTRY_ANNOTATED,
)

_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY,
    ts           TEXT    NOT NULL,
    event_type   TEXT    NOT NULL,
    entry_id     INTEGER NOT NULL,
    actor        TEXT    NOT NULL DEFAULT '',
    payload_json TEXT    NOT NULL DEFAULT '{}'
)
"""

#: entry_id 过滤索引(entry_id, seq):按条目取事件时间线免全表扫描。
_EVENTS_ENTRY_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_events_entry ON events(entry_id, seq)"
)

#: event_type 过滤索引(event_type, seq)。
_EVENTS_TYPE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, seq)"
)

#: append-only 触发器:数据库层拒绝任何 UPDATE / DELETE(中文 ABORT 消息)。
#: 注:DROP TRIGGER / DROP TABLE 无法用触发器拦截(SQLite 限制),那属于
#: "绕过防线"的蓄意行为,由重放对账兜底检出。
_NO_UPDATE_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, '事件账本只允许追加:events 表禁止 UPDATE');
END
"""

_NO_DELETE_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, '事件账本只允许追加:events 表禁止 DELETE');
END
"""


class EventLogError(ValueError):
    """事件账本输入/损坏错误(中文消息):文件非 SQLite、字节损坏、
    payload 非法 JSON(疑似篡改)、参数非法等。"""


@dataclass(frozen=True)
class Event:
    """账本中的一条不可变事件(行转 dataclass,payload 已解析为 dict)。

    ``payload`` 是事件负载(自定义键值,JSON 往返);``actor`` 是操作署名
    (可为空串,表示未署名/系统动作);``ts`` 为落盘时间(本地时区 ISO8601)。
    """

    seq: int
    ts: str
    event_type: str
    entry_id: int
    actor: str
    payload: dict[str, Any]


def _is_lock_error(exc: sqlite3.Error) -> bool:
    """区分"锁等待"与"损坏":BUSY/LOCKED 原样上抛,其余 DatabaseError
    按损坏处理(中文报错)。"""
    return isinstance(exc, sqlite3.OperationalError) and (
        "locked" in str(exc) or "busy" in str(exc)
    )


#: 事件落盘 INSERT(单条与批量共用同一语句,参数顺序与列序一致)。
_INSERT_EVENT_SQL = (
    "INSERT INTO events (ts, event_type, entry_id, actor, payload_json)"
    " VALUES (?, ?, ?, ?, ?)"
)


def _validate_and_serialize(
    event_type: str,
    entry_id: int,
    *,
    actor: str = "",
    payload: dict[str, Any] | None = None,
) -> tuple[str, int, str, str]:
    """校验并序列化一条事件参数 → ``(event_type, entry_id, actor, payload_json)``。

    单条 :meth:`EventLog.append` 与批量 :meth:`EventLog.append_many` 共用
    同一套校验判例(参数类型 / payload JSON 规范化,中文报错、检查顺序
    完全一致)——批量口不引入任何第二套判例。
    """
    if not isinstance(event_type, str) or not event_type.strip():
        raise EventLogError(
            f"事件类型必须是非空字符串:{event_type!r}"
        )
    if isinstance(entry_id, bool) or not isinstance(entry_id, int):
        raise EventLogError(f"条目编号必须是整数:{entry_id!r}")
    if not isinstance(actor, str):
        raise EventLogError(f"操作署名必须是字符串:{actor!r}")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise EventLogError(
            f"事件负载必须是 dict 或 None:{type(payload).__name__}"
        )
    try:
        # 确定性序列化:键排序 + 紧凑分隔符,同一 payload 字节级稳定。
        payload_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise EventLogError(f"事件负载无法 JSON 序列化:{exc}") from exc
    return event_type, int(entry_id), actor, payload_json


class EventLog:
    """append-only 事件账本(SQLite WAL,线程安全,组合 SQLiteKernel)。

    本类**没有任何 UPDATE / DELETE 路径**:事件一经 ``append`` 落盘即为
    事实,数据库触发器会拒绝绕过本类的直接改写(见模块 docstring 的三层
    append-only 保证)。支持 ``with`` 上下文;写入路径两条:单条 ``append``
    即时 commit(单条 INSERT 自成事务,原子:要么整条在,要么整条不在,
    不存在半条事件);批量 :meth:`append_many` 整批单事务一次 commit
    (任一条非法整批拒绝,见其 docstring)。
    """

    def __init__(self, path: str | Path) -> None:
        self.db_path = str(path)
        try:
            # 统一底座:WAL + busy_timeout=5000 + 单连接 + 全局锁,
            # 既有库打开零迁移(kernel 不做任何 DDL,除非显式 migrate)。
            self._kernel = SQLiteKernel(self.db_path)
            self._kernel.execute(_EVENTS_SQL)
            self._kernel.execute(_EVENTS_ENTRY_INDEX_SQL)
            self._kernel.execute(_EVENTS_TYPE_INDEX_SQL)
            self._kernel.execute(_NO_UPDATE_TRIGGER_SQL)
            self._kernel.execute(_NO_DELETE_TRIGGER_SQL)
        except sqlite3.DatabaseError as exc:
            if _is_lock_error(exc):
                raise
            raise EventLogError(
                f"事件账本损坏或不是 SQLite 数据库:{self.db_path}"
                f"(原始错误:{exc})"
            ) from exc
        logger.debug("事件账本已就绪:%s", self.db_path)

    # ------------------------------------------------------------------
    # 写入(唯一数据面写入口;append-only)
    # ------------------------------------------------------------------

    def append(
        self,
        event_type: str,
        entry_id: int,
        *,
        actor: str = "",
        payload: dict[str, Any] | None = None,
    ) -> int:
        """原子追加一条事件,返回其 seq(账本内单调,1 起无洞不重)。

        单条 INSERT + 即时 commit 自成事务:并发追加由内核锁(同实例)与
        WAL 忙等(跨连接)串行化,提交成功的事件 seq 连续;进程崩溃时本条
        要么完整落盘要么完全不存在,不存在"半条事件"。

        :param event_type: 事件类型(非空字符串;复核域常量见
            :data:`REVIEW_EVENT_TYPES`,但不强制白名单——账本面向多域扩展)。
        :param entry_id: 关联条目编号(整数;bool 拒绝)。
        :param actor: 操作署名(默认空串=未署名;必须为字符串)。
        :param payload: 事件负载 dict(JSON 规范化落盘:键排序 + 紧凑分隔,
            确定性);``None`` 视为 ``{}``。
        :raises EventLogError: 参数非法 / payload 不可 JSON 序列化(中文消息)。
        """
        event_type, entry_id, actor, payload_json = _validate_and_serialize(
            event_type, entry_id, actor=actor, payload=payload
        )
        try:
            cur = self._kernel.execute(
                _INSERT_EVENT_SQL,
                (now_iso(), event_type, entry_id, actor, payload_json),
            )
        except sqlite3.DatabaseError as exc:
            if _is_lock_error(exc):
                raise
            raise EventLogError(
                f"事件账本写入失败(疑似损坏):{exc}"
            ) from exc
        seq = int(cur.lastrowid or 0)
        telemetry.inc("storage.event_log.append")
        return seq

    def append_many(self, events: Iterable[Mapping[str, Any]]) -> list[int]:
        """原子批量追加一批事件(整批单事务、一次 commit),返回各事件 seq
        (按入参顺序,账本内连续无洞)。

        语义 = 逐条 :meth:`append` 的**原子化**:

        - **任一条非法整批拒绝**:先对整批做与 ``append`` 完全一致的参数
          校验与 payload 序列化(:func:`_validate_and_serialize`,零第二套
          判例),校验阶段零 DB 触碰;任何一条非法 → 中文
          :class:`EventLogError`(含条目序号),整批一条都不落库;
        - **单事务一次 commit**:通过锁内 ``BEGIN IMMEDIATE`` 起事务、
          连续 INSERT、提交前校验本批 seq 逐条连续(不变式"1..N 无洞
          不重"),全部成功才 commit;任何一步失败整批回滚(回滚的 INSERT
          不占号,seq 连续性保持,后续追加不受影响);
        - 并发与崩溃语义与 ``append`` 同源:同实例由内核锁串行化、跨连接
          由 WAL 忙等串行化;进程崩溃时整批要么完整落盘要么完全不存在。

        事务实现沿用内核惯例(:meth:`netsentinel.storage.kernel.SQLiteKernel.migrate`
        的 ``BEGIN IMMEDIATE`` 单事务与 :class:`~netsentinel.storage.kernel.Repo`
        的锁内直执同构),不改内核文件;语句仍经内核 ``_exec`` 计数,
        可观测性保持。

        :param events: 事件规格可迭代对象(一次性消费);每项是含
            ``event_type`` / ``entry_id``(必填)与 ``actor`` / ``payload``
            (可选,缺省与 ``append`` 一致)的映射;非映射项 / 含未知键 /
            缺必填键 → 整批拒绝(中文报错)。
        :raises EventLogError: 任一条参数非法 / payload 不可 JSON 序列化 /
            seq 不连续 / 写入失败(疑似损坏);锁等待(BUSY)类错误原样上抛。
        """
        prepared: list[tuple[str, int, str, str, str]] = []
        for idx, item in enumerate(events, start=1):
            if not isinstance(item, Mapping):
                raise EventLogError(
                    f"第 {idx} 条事件规格必须是映射(dict):{type(item).__name__}"
                )
            unknown_keys = sorted(set(item) - {"event_type", "entry_id", "actor", "payload"})
            if unknown_keys:
                raise EventLogError(
                    f"第 {idx} 条事件规格含未知键:{unknown_keys}"
                    "(仅允许 event_type / entry_id / actor / payload)"
                )
            if "event_type" not in item or "entry_id" not in item:
                raise EventLogError(
                    f"第 {idx} 条事件规格缺少必填键:event_type / entry_id"
                )
            event_type, entry_id, actor, payload_json = _validate_and_serialize(
                item["event_type"],
                item["entry_id"],
                actor=item.get("actor", ""),
                payload=item.get("payload"),
            )
            prepared.append((now_iso(), event_type, entry_id, actor, payload_json))
        if not prepared:
            return []
        k = self._kernel
        conn = k._conn
        seqs: list[int] = []
        with k._lock:
            began = False
            try:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                    began = True
                for ts, event_type, entry_id, actor, payload_json in prepared:
                    cur = k._exec(_INSERT_EVENT_SQL, (ts, event_type, entry_id, actor, payload_json))
                    seqs.append(int(cur.lastrowid or 0))
                # seq 连续性校验(提交前):单事务内 rowid 应逐条 +1 连续分配,
                # 违反即底层异常——整批回滚拒绝,绝不让有洞/重复批次落库。
                first = seqs[0]
                for offset, seq in enumerate(seqs):
                    if seq != first + offset:
                        raise EventLogError(
                            f"批量追加分配到的 seq 不连续(起点 {first},"
                            f"实际序列 {seqs}):已整批回滚,seq 无洞不重的"
                            "账本不变式保持"
                        )
                conn.commit()
            except sqlite3.DatabaseError as exc:
                if began:
                    conn.rollback()
                if _is_lock_error(exc):
                    raise
                raise EventLogError(
                    f"事件账本批量写入失败(疑似损坏):{exc}"
                ) from exc
            except BaseException:
                if began:
                    conn.rollback()
                raise
        telemetry.inc("storage.event_log.append_many")
        return seqs

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def iter_events(self) -> list[Event]:
        """按 seq 升序返回全部事件(重放入口;大账本请用 :meth:`query`
        带过滤,避免一次性物化)。"""
        return self.query()

    def query(
        self,
        *,
        entry_id: int | None = None,
        event_type: str | None = None,
        seq_from: int | None = None,
        seq_to: int | None = None,
    ) -> list[Event]:
        """按条件过滤事件(全部条件可组合,结果按 seq 升序)。

        :param entry_id: 只取该条目的事件。
        :param event_type: 只取该类型的事件。
        :param seq_from: seq 下界(含);:param seq_to: seq 上界(含)。
        :raises EventLogError: 参数类型非法、``seq_from > seq_to``、
            账本损坏 / payload 非法 JSON(中文消息)。
        """
        sql = "SELECT seq, ts, event_type, entry_id, actor, payload_json FROM events"
        conds: list[str] = []
        params: list[Any] = []
        if entry_id is not None:
            if isinstance(entry_id, bool) or not isinstance(entry_id, int):
                raise EventLogError(f"条目编号过滤值必须是整数:{entry_id!r}")
            conds.append("entry_id = ?")
            params.append(int(entry_id))
        if event_type is not None:
            if not isinstance(event_type, str) or not event_type:
                raise EventLogError(
                    f"事件类型过滤值必须是非空字符串:{event_type!r}"
                )
            conds.append("event_type = ?")
            params.append(event_type)
        if seq_from is not None:
            if isinstance(seq_from, bool) or not isinstance(seq_from, int):
                raise EventLogError(f"seq 下界必须是整数:{seq_from!r}")
            conds.append("seq >= ?")
            params.append(int(seq_from))
        if seq_to is not None:
            if isinstance(seq_to, bool) or not isinstance(seq_to, int):
                raise EventLogError(f"seq 上界必须是整数:{seq_to!r}")
            conds.append("seq <= ?")
            params.append(int(seq_to))
        if (
            seq_from is not None
            and seq_to is not None
            and int(seq_from) > int(seq_to)
        ):
            raise EventLogError(
                f"seq 范围非法:下界 {seq_from} 大于上界 {seq_to}"
            )
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY seq ASC"
        try:
            rows = self._kernel.query(sql, tuple(params))
        except sqlite3.DatabaseError as exc:
            if _is_lock_error(exc):
                raise
            raise EventLogError(
                f"事件账本读取失败(疑似损坏):{exc}"
            ) from exc
        return [self._row_to_event(r) for r in rows]

    def latest_seq(self) -> int:
        """当前最大 seq(空账本返回 0;用于断言 seq 连续性)。"""
        try:
            rows = self._kernel.query(
                "SELECT COALESCE(MAX(seq), 0) AS s FROM events"
            )
        except sqlite3.DatabaseError as exc:
            if _is_lock_error(exc):
                raise
            raise EventLogError(
                f"事件账本读取失败(疑似损坏):{exc}"
            ) from exc
        return int(rows[0]["s"]) if rows else 0

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_event(row: dict[str, Any]) -> Event:
        """行转 :class:`Event`;payload 非法 JSON / 非对象 → 中文报错
        (fail-safe:疑似篡改的事件绝不静默吞掉)。"""
        raw = row["payload_json"]
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise EventLogError(
                f"第 {row['seq']} 条事件的负载不是合法 JSON(疑似被篡改):{exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise EventLogError(
                f"第 {row['seq']} 条事件的负载不是 JSON 对象(疑似被篡改):{raw!r}"
            )
        return Event(
            seq=int(row["seq"]),
            ts=str(row["ts"] or ""),
            event_type=str(row["event_type"] or ""),
            entry_id=int(row["entry_id"]),
            actor=str(row["actor"] or ""),
            payload=payload,
        )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def close(self) -> None:
        """关闭底层连接(幂等容忍;不负责关闭使用方持有的其他资源)。"""
        self._kernel.close()

    def __enter__(self) -> EventLog:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
