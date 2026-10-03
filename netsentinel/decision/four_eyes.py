"""四眼原则(双人复核)队列 —— 高价值举报须两名不同审核人批准(NetSentinel V3 · A50)。

在 A10 人工复核队列(pending → approved → submitted / rejected)之上叠加一道
双人确认门:当 ``required=True`` 时,单人的 approve 只会留下第一审核人记录并
进入"等待第二审核人"状态;只有另一名**不同**审核人再次确认,底层条目才会
真正变为 approved。本模块采用组合模式复用 ReviewQueue(同一 SQLite 库文件,
条目表 entries 归底层管理,本模块不修改 A10 任何文件),并自建 approvals 表
记录每位审核人的确认痕迹(审计留痕):

    approvals(entry_id INTEGER, reviewer TEXT, acted_at TEXT,
              PRIMARY KEY(entry_id, reviewer))

安全红线(V3 契约 §0 第 11 条 + §3 A50):
  * 四眼复核只能增加审批环节:任何配置组合都不会削弱人工门;
  * required=True 时绝不允许单人把条目放行到 approved;
  * 同一审核人不能充当第二审核人(显式中文报错 + 主键双保险)。

用法::

    from netsentinel.decision.four_eyes import FourEyesQueue
    q = FourEyesQueue(cfg.db_path, cfg.four_eyes_required)
    entry_id = q.add(report)
    q.approve(entry_id, "张三")   # -> {"state": "awaiting_second", ...}
    q.approve(entry_id, "李四")   # -> {"state": "approved", ...}

驳回语义:reject 为终态(直通底层),approvals 留痕保留供审计,但条目状态
已变为 rejected,之后的任何 approve 都会收到底层"非待复核"的中文错误。

V5 升级(CONTRACTS-V5.md):approvals 自有连接与底层 ReviewQueue 采用同一
连接策略(``PRAGMA journal_mode=WAL`` + ``busy_timeout=5000``,共库跨进程
更稳);双人确认关键节点接入 ``netsentinel.telemetry``——第一审核人留痕计
``four_eyes.awaiting``,两眼齐(或未启用四眼的单人直通)计 ``four_eyes.approved``。

A222 升级(事件账本透传,默认关闭):``__init__`` 新增仅关键字参数 ``event_log``
(默认 ``None`` = 现状逐字节不变,零事件零额外 SQL)。显式传入
:class:`~netsentinel.storage.event_log.EventLog` 后:

- **透传底层**::class:`~netsentinel.decision.review_queue.ReviewQueue` 的 A213
  双写机制自动生效——``add / reject / mark_submitted`` 与双人齐后的底层确认
  都会先把 ``entry_*`` 事件落盘(先账本后状态),底层 approve 由本层传入
  ``actor=审核人`` 完成署名;
- **四眼域事件**:第一审核人留痕与双人确认完成是四眼自有的状态变更
  (approvals 表),由本层在写入留痕**之前**补发 ``four_eyes_awaiting_second``
  / ``four_eyes_approved`` 事件(payload 以 ``state`` / ``reviewers`` 携带双人
  语义,常量定义在本模块,不改 event_log 的常量集;重放器按未识别事件
  前向兼容,不影响对账);
- 四眼校验逻辑(同人拒二次、双人主键、状态机拦截)零改动:非法操作在校验
  阶段即抛 ValueError,不产生任何事件。
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from netsentinel import telemetry
from netsentinel.contracts import SiteReport, now_iso
from netsentinel.decision.review_queue import (
    BUSY_TIMEOUT_MS,
    STATUS_CN,
    Entry,
    ReviewQueue,
)
from netsentinel.storage.event_log import EventLog

logger = logging.getLogger(__name__)

__all__ = [
    "EVENT_FOUR_EYES_APPROVED",
    "EVENT_FOUR_EYES_AWAITING",
    "FOUR_EYES_EVENT_TYPES",
    "FourEyesQueue",
]

#: 四眼确认留痕表(与底层 entries 表同库不同表,互不侵入)。
_APPROVALS_SQL = """
CREATE TABLE IF NOT EXISTS approvals (
    entry_id  INTEGER NOT NULL,
    reviewer  TEXT    NOT NULL,
    acted_at  TEXT    NOT NULL,
    PRIMARY KEY (entry_id, reviewer)
)
"""

#: 四眼域事件:第一审核人留痕(A222)。条目在底层仍是 pending,等待第二
#: 审核人;payload 形如 ``{"state": "awaiting_second", "reviewer": ...,
#: "reviewers": [...]}``。常量定义在本模块(不改 event_log 的常量集),
#: 重放器按未识别事件前向兼容(不参与 entries 投影对账)。
EVENT_FOUR_EYES_AWAITING = "four_eyes_awaiting_second"

#: 四眼域事件:双人确认完成(A222)。先于底层 ``entry_approved`` 落盘,
#: payload 以 ``reviewers`` 列表携带双人署名(谁与谁共同放行)。
EVENT_FOUR_EYES_APPROVED = "four_eyes_approved"

#: 四眼域事件类型全集(payload 以 state / reviewer / reviewers 区分语义)。
FOUR_EYES_EVENT_TYPES: tuple[str, ...] = (
    EVENT_FOUR_EYES_AWAITING,
    EVENT_FOUR_EYES_APPROVED,
)


class FourEyesQueue:
    """四眼原则复核队列(组合 ReviewQueue,共库共条目)。

    ``required=False`` 时 approve 直通底层(单人确认),仍写 approvals 留痕;
    ``required=True`` 时执行状态机 none → awaiting_second → approved,
    单人绝不放行。list / get / reject / mark_submitted 等底层读操作直通委托。

    :param event_log: 事件账本(A222 透传,默认 ``None`` = 现状逐字节不变)。
        启用后:(1) 底层经 A213 双写自动落 ``entry_*`` 状态事件(先账本后
        状态,底层 approve 带上 ``actor=审核人`` 署名);(2) 四眼自有的留痕
        变更(第一审核人 / 双人齐)由本层在写 approvals 行**之前**先落
        四眼域事件(先账本后状态)。本类只持有引用,**不拥有其生命周期**
        (close 不会关闭账本,由调用方管理)。
    """

    def __init__(
        self,
        db_path: str | Path,
        required: bool,
        *,
        event_log: EventLog | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self.required = bool(required)
        #: 事件账本(None = 不启用透传,行为与旧版逐字节一致)。
        self._event_log = event_log
        # 组合而非修改:底层 ReviewQueue 负责建目录与 entries 表;
        # A222 透传:账本经底层 A213 双写机制落 entry_* 事件(先账本后状态)。
        self._queue = ReviewQueue(self.db_path, event_log=event_log)
        # 自有连接只管 approvals 表(即时 commit,与底层写纪律一致)。
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        # V5:与底层 ReviewQueue(A10)同一连接策略:WAL + 忙等 5s。
        self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_APPROVALS_SQL)
        self._conn.commit()
        logger.debug(
            "四眼复核队列已就绪:%s(required=%s)", self.db_path, self.required
        )

    # ------------------------------------------------------------------
    # 底层直通委托(只读与终态操作,不经过四眼逻辑)
    # ------------------------------------------------------------------

    def add(self, report: SiteReport, evidence_zip: str = "") -> int:
        """把站点报告加入底层队列(初始 pending),返回条目 id。"""
        return self._queue.add(report, evidence_zip=evidence_zip)

    def list(self, status: str | None = None) -> list[Entry]:
        """直通底层:按 id 升序列出条目(可按状态过滤)。"""
        return self._queue.list(status)

    def get(self, id: int) -> Entry | None:
        """直通底层:按 id 取单条;不存在返回 None。"""
        return self._queue.get(id)

    def summary(self) -> dict[str, int]:
        """直通底层:各状态条目计数。"""
        return self._queue.summary()

    def reject(self, id: int, note: str = "") -> Entry:
        """直通底层人工驳回:pending → rejected(终态,approvals 留痕保留)。"""
        return self._queue.reject(id, note=note)

    def mark_submitted(self, id: int) -> Entry:
        """直通底层:approved → submitted(举报提交完成回写)。"""
        return self._queue.mark_submitted(id)

    # ------------------------------------------------------------------
    # 四眼确认(核心状态机)
    # ------------------------------------------------------------------

    def approve(self, entry_id: int, reviewer: str) -> dict:
        """人工确认(四眼语义)。

        - 条目不存在 / 非 pending:透传底层中文 ValueError;
        - required=False:底层单人确认,返回 ``{"state": "approved", "reviewers": [reviewer]}``;
        - required=True 第一人:仅记录 approvals,条目保持 pending,
          返回 ``{"state": "awaiting_second", "first": reviewer,
          "note": "等待第二审核人确认"}``;
        - required=True 第二人(reviewer ≠ 第一人):记录 approvals 并底层
          确认,返回 ``{"state": "approved", "reviewers": [第一人, 第二人]}``;
        - 同人重复确认:抛 ValueError(中文)。
        """
        name = self._validate_reviewer(reviewer)
        if not self.required:
            # 直通底层:不存在/非 pending 的中文异常原样上抛。
            # A222:actor=审核人署名(仅入事件账本;未启用账本时零影响)。
            self._queue.approve(entry_id, actor=name)
            self._try_record(entry_id, name)
            logger.info("四眼复核未启用:条目 %s 由 %s 单人确认", entry_id, name)
            telemetry.inc("four_eyes.approved")
            return {"state": "approved", "reviewers": [name]}
        return self._confirm(entry_id, name, require_first=False)

    def second_approver(self, entry_id: int, reviewer: str) -> dict:
        """第二审核人显式确认入口(契约 §3 A50)。

        必须已有第一审核人记录且 reviewer 不同于第一人;否则中文 ValueError。
        两人齐 → 底层 approve,返回与 approve 第二人相同的字典。
        """
        name = self._validate_reviewer(reviewer)
        return self._confirm(entry_id, name, require_first=True)

    def status(self, entry_id: int) -> dict:
        """四眼状态查询:{"state": none|awaiting_second|approved,
        "reviewers": [...], "required": bool}。

        - approved / submitted 的条目视为四眼已完成(state="approved");
        - pending 且已有第一人留痕 → "awaiting_second";
        - 其余(无留痕,或已驳回——驳回为终态)→ "none",
          reviewers 保留历史留痕供审计。
        条目不存在时抛中文 ValueError(与底层一致)。
        """
        entry = self._queue.get(entry_id)
        if entry is None:
            raise ValueError(f"复核条目不存在:id={entry_id}")
        reviewers = self._reviewers(entry_id)
        if entry.status in ("approved", "submitted"):
            state = "approved"
        elif entry.status == "pending" and reviewers:
            state = "awaiting_second"
        else:
            state = "none"
        return {"state": state, "reviewers": reviewers, "required": self.required}

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _emit(
        self,
        event_type: str,
        entry_id: int,
        *,
        actor: str = "",
        payload: dict | None = None,
    ) -> None:
        """把四眼域事件追加进账本(未启用透传 = 零操作,不发一条 SQL)。

        调用时机契约(对齐 review_queue._emit 的 A213 形态):**必须在对应
        状态变更提交之前**(先账本后状态)——四眼域事件先行落盘,随后才写
        approvals 留痕 / 触发底层状态迁移。
        """
        if self._event_log is None:
            return
        self._event_log.append(event_type, entry_id, actor=actor, payload=payload)

    def _confirm(self, entry_id: int, name: str, *, require_first: bool) -> dict:
        """required=True 时的确认状态机(require_first 供 second_approver)。"""
        entry = self._queue.get(entry_id)
        if entry is None:
            # 与底层 _transition 完全一致的中文报错,保证透传语义。
            raise ValueError(f"复核条目不存在:id={entry_id}")
        # 先做底层状态校验:非 pending(含已驳回/已确认/已提交)一律按底层
        # 语义拒绝,不给四眼逻辑留绕过空间。
        self._ensure_pending(entry_id, entry)
        existing = self._reviewers(entry_id)
        if name in existing:
            raise ValueError(f"同一审核人不能二次确认:{name}")
        if require_first and not existing:
            raise ValueError(
                "尚无第一审核人确认记录:第二审核人不能先于第一审核人确认"
            )
        if not existing:
            # 第一审核人:只留痕,不放行(红线:单人绝不 approve)。
            # A222 先账本后状态:四眼域事件先行落盘,再写 approvals 留痕;
            # 事件 append 失败时留痕不写(账实都不动)。
            self._emit(
                EVENT_FOUR_EYES_AWAITING,
                entry_id,
                actor=name,
                payload={
                    "state": "awaiting_second",
                    "reviewer": name,
                    "reviewers": [name],
                },
            )
            self._record_approval(entry_id, name)
            telemetry.inc("four_eyes.awaiting")
            logger.info(
                "四眼复核:条目 %s 第一审核人 %s 已确认,等待第二审核人",
                entry_id,
                name,
            )
            return {
                "state": "awaiting_second",
                "first": name,
                "note": "等待第二审核人确认",
            }
        # 第二审核人:先账本后状态——四眼域事件(双人署名)先行落盘,
        # 再写留痕,最后底层确认(底层内部再按 A213 先落 entry_approved
        # 后提交 entries,署名 actor=第二审核人)。
        # 底层确认失败则回滚本次留痕保持账实一致(账本为 append-only
        # 事实,已落事件不回删,由重放对账如实呈现)。
        self._emit(
            EVENT_FOUR_EYES_APPROVED,
            entry_id,
            actor=name,
            payload={
                "state": "approved",
                "reviewer": name,
                "reviewers": existing + [name],
            },
        )
        self._record_approval(entry_id, name)
        try:
            self._queue.approve(entry_id, actor=name)
        except Exception:
            self._drop_approval(entry_id, name)
            raise
        reviewers = existing + [name]
        telemetry.inc("four_eyes.approved")
        logger.info(
            "四眼复核:条目 %s 双人确认完成(%s)", entry_id, "、".join(reviewers)
        )
        return {"state": "approved", "reviewers": reviewers}

    def _ensure_pending(self, entry_id: int, entry: Entry) -> None:
        """复刻底层 approve 的状态校验报错(第一人路径不能真调底层 approve)。"""
        if entry.status == "pending":
            return
        allowed = " 或 ".join(
            f"{STATUS_CN.get(s, s)}({s})" for s in ("pending",)
        )
        raise ValueError(
            f"复核条目 {entry_id} 当前状态为 "
            f"{STATUS_CN.get(entry.status, entry.status)}({entry.status}),"
            f"只有 {allowed} 状态的条目才能人工确认"
        )

    @staticmethod
    def _validate_reviewer(reviewer: str) -> str:
        """审核人姓名校验:空/空白拒绝(中文报错),两侧空白剥除后返回。"""
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError("审核人不能为空或全为空白:请提供有效的审核人姓名")
        return reviewer.strip()

    def _reviewers(self, entry_id: int) -> list[str]:
        """按确认顺序(rowid 即插入顺序)返回该条目的审核人列表。"""
        rows = self._conn.execute(
            "SELECT reviewer FROM approvals WHERE entry_id = ? ORDER BY rowid ASC",
            (entry_id,),
        ).fetchall()
        return [str(r["reviewer"]) for r in rows]

    def _record_approval(self, entry_id: int, reviewer: str) -> None:
        """写入一条确认留痕(主键 entry_id+reviewer 兜底防同人重复)。"""
        self._conn.execute(
            "INSERT INTO approvals (entry_id, reviewer, acted_at) VALUES (?, ?, ?)",
            (entry_id, reviewer, now_iso()),
        )
        self._conn.commit()

    def _try_record(self, entry_id: int, reviewer: str) -> None:
        """required=False 路径的留痕:已有同名留痕时仅告警不阻断(单人已获授权)。"""
        try:
            self._record_approval(entry_id, reviewer)
        except sqlite3.IntegrityError:  # pragma: no cover - 正常流程不可达
            logger.warning(
                "条目 %s 已有审核人 %s 的留痕,跳过重复记录", entry_id, reviewer
            )

    def _drop_approval(self, entry_id: int, reviewer: str) -> None:
        """删除一条留痕(仅用于底层确认失败后的回滚)。"""
        self._conn.execute(
            "DELETE FROM approvals WHERE entry_id = ? AND reviewer = ?",
            (entry_id, reviewer),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def close(self) -> None:
        """关闭自有连接与底层连接(幂等容忍)。"""
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover - 关闭异常无需上抛
            logger.debug("关闭四眼复核留痕连接时出现异常", exc_info=True)
        finally:
            self._queue.close()

    def __enter__(self) -> FourEyesQueue:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
