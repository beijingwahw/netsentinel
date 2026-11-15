"""replay 包公共辅助:常量 / 数据类 / 队列工厂注入接缝。

本文件由单文件 :mod:`netsentinel.storage.replay` 拆包而来(逐字搬运,
不改逻辑);子模块 :mod:`projection` / :mod:`reconcile` / :mod:`heal` /
:mod:`cli` 共享的状态机表、事件常量、报告数据类与跨 decision 注入接缝
集中在此,避免循环依赖。

decision↔storage 架构环解耦接缝(V? · 任务书):

- 原实现里 CLI 层有 4 处函数内惰性 ``import decision``(ReviewQueue × 3、
  FourEyesQueue × 1)。本文件提供**可注入工厂**(:func:`set_queue_factories`)
  作为解 storage→decision 架构环的接缝——测试 / 替代实现可注入自己的队列
  构造器,storage 包不再硬编码对 decision 的依赖;
- **未注入时保持现有惰性导入默认行为**(默认工厂内部仍是函数内
  ``from netsentinel.decision... import ...``,完全向后兼容,**不要求任何
  调用方现在改**);
- 工厂签名统一为 ``(queue_path: str) -> Any``(按路径构造队列对象),
  与原 CLI 各调用点 ``ReviewQueue(path)`` / ``FourEyesQueue(path, False)``
  的构造口径一一对应。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from netsentinel.storage.event_log import (
    EVENT_ENTRY_APPROVED,
    EVENT_ENTRY_MARKED_SUBMITTED,
    EVENT_ENTRY_REJECTED,
)

__all__ = [
    "AHEAD_ACTION_APPROVE",
    "AHEAD_ACTION_REJECT",
    "AHEAD_ACTION_SUBMIT",
    "AHEAD_ACTIONS_CN",
    "AHEAD_EVENT_ACTIONS",
    "APPROVAL_DIFF_INCONSISTENT",
    "APPROVAL_DIFF_LAGGING",
    "ApplyAheadOutcome",
    "AheadAction",
    "ApprovalDiff",
    "ApprovalHealAction",
    "EVENT_APPROVALS_HEALED",
    "EXIT_INPUT_ERROR",
    "EXIT_MISMATCH",
    "EXIT_OK",
    "EntryDiff",
    "FOUR_EYES_EVENT_APPROVED",
    "FOUR_EYES_EVENT_AWAITING",
    "FOUR_EYES_EVENT_TYPES",
    "HEAL_ACTOR",
    "HealApprovalsOutcome",
    "ProjectedApprovals",
    "ProjectedEntry",
    "ReplayReport",
    "StatusStep",
    "TimelineItem",
    "make_four_eyes_queue",
    "make_review_queue",
    "reset_queue_factories",
    "set_queue_factories",
]

#: 对账一致(账本与队列互相印证)。
EXIT_OK = 0

#: 输入错误(文件缺失 / 路径是目录 / 账本损坏到无法打开)。
EXIT_INPUT_ERROR = 1

#: 检出差异(事件超前 / 账本缺失 / 状态或字段不一致 / payload 非法)。
EXIT_MISMATCH = 2

#: 状态机可达表(只读复用状态机语义,零改动):from 状态可达的状态集合。
#: pending → approved/rejected;approved → submitted;终态 submitted/rejected
#: 不可再迁移。重放只拿它定位"首个分歧事件",不做任何校验拦截。
_REACHABLE: dict[str, frozenset[str]] = {
    "pending": frozenset({"approved", "rejected"}),
    "approved": frozenset({"submitted"}),
    "rejected": frozenset(),
    "submitted": frozenset(),
}

#: 对账参与比较的字段(时间戳豁免,见包 docstring)。
_RECONCILE_FIELDS: tuple[str, ...] = (
    "site_url",
    "verdict",
    "evidence_zip",
    "note",
    "priority_weight",
)

#: 差异明细中文列出的字段名(展示用)。
_FIELD_CN: dict[str, str] = {
    "site_url": "站点",
    "verdict": "判定",
    "evidence_zip": "证据包",
    "note": "备注",
    "priority_weight": "提权加项",
}

# ---------------------------------------------------------------------------
# 事件超前补齐(--apply-ahead)· 常量
# ---------------------------------------------------------------------------

#: 补齐动作令牌(注入回调的稳定契约;不暴露底层方法名,保持 storage 抽象)。
AHEAD_ACTION_APPROVE = "approve"
AHEAD_ACTION_REJECT = "reject"
AHEAD_ACTION_SUBMIT = "submit"

#: 动作令牌 → 中文名(打印用)。
AHEAD_ACTIONS_CN: dict[str, str] = {
    AHEAD_ACTION_APPROVE: "人工确认",
    AHEAD_ACTION_REJECT: "人工驳回",
    AHEAD_ACTION_SUBMIT: "提交回写",
}

#: 状态迁移事件 → (动作令牌, 合法起点, 将达状态)。只读复用底层状态机
#: 语义(pending → approved/rejected;approved → submitted),零第二套判例。
AHEAD_EVENT_ACTIONS: dict[str, tuple[str, str, str]] = {
    EVENT_ENTRY_APPROVED: (AHEAD_ACTION_APPROVE, "pending", "approved"),
    EVENT_ENTRY_REJECTED: (AHEAD_ACTION_REJECT, "pending", "rejected"),
    EVENT_ENTRY_MARKED_SUBMITTED: (AHEAD_ACTION_SUBMIT, "approved", "submitted"),
}

# ---------------------------------------------------------------------------
# 四眼留痕(approvals)投影对账 · 常量(A234)
# ---------------------------------------------------------------------------

#: 四眼域事件:第一审核人留痕。**本地声明**(字面值与
#: ``netsentinel.decision.four_eyes.EVENT_FOUR_EYES_AWAITING`` 一致,
#: 测试锁定防漂移)——storage 层不反向 import 决策层(依赖方向对齐 A213)。
FOUR_EYES_EVENT_AWAITING = "four_eyes_awaiting_second"

#: 四眼域事件:双人确认完成(本地声明,同上;payload 以 ``reviewers``
#: 列表携带 [第一人, 第二人])。
FOUR_EYES_EVENT_APPROVED = "four_eyes_approved"

#: 四眼域事件类型全集(重放 approvals 投影时识别;其余类型按未识别计数)。
FOUR_EYES_EVENT_TYPES: tuple[str, ...] = (
    FOUR_EYES_EVENT_AWAITING,
    FOUR_EYES_EVENT_APPROVED,
)

#: approvals 差异类型:账本事件超前(双人齐已记但表缺)——崩溃窗口语义。
APPROVAL_DIFF_LAGGING = "留痕滞后"

#: approvals 差异类型:留痕与账本事件互相不可解释(疑似外部改写)。
APPROVAL_DIFF_INCONSISTENT = "留痕不一致"

#: 留痕补写(``--heal-approvals``)回调的固定署名:进审计事件的 ``actor``
#: 恒为该值——与真人审核人的姓名署名**可区分**(防伪造留痕红线:任何后续
#: 审计都能看出该 approvals 行是重放补写、而非真人点击复核)。
HEAL_ACTOR = "replay-heal"

#: 留痕补写的审计事件类型(**本地声明**,重放器按未识别事件前向兼容:
#: entries 投影计 unknown、approvals 投影忽略——不影响任何对账结论)。
#: payload 形如 ``{"reviewer": 补写的审核人, "source_seq": 来源四眼事件 seq}``。
EVENT_APPROVALS_HEALED = "approvals_healed"


@dataclass(frozen=True)
class StatusStep:
    """一次状态迁移步(seq = 造成该状态的事件序号,status = 迁移后状态)。

    entry_added 的初始 pending 也是一步;status_chain 的末尾即重放最终状态。
    """

    seq: int
    status: str


@dataclass(frozen=True)
class TimelineItem:
    """操作时间线中的一步(seq / 时间 / 类型 / 署名 / 中文一句话摘要)。"""

    seq: int
    ts: str
    event_type: str
    actor: str
    summary: str


@dataclass
class ProjectedEntry:
    """重放重建的单条条目投影(最终字段值 + 状态链 + 操作时间线)。"""

    entry_id: int
    site_url: str = ""
    verdict: str = ""
    status: str = ""
    note: str = ""
    evidence_zip: str = ""
    priority_weight: float = 0.0
    created_at: str = ""
    timeline: list[TimelineItem] = field(default_factory=list)
    status_chain: list[StatusStep] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "site_url": self.site_url,
            "verdict": self.verdict,
            "status": self.status,
            "note": self.note,
            "evidence_zip": self.evidence_zip,
            "priority_weight": self.priority_weight,
            "created_at": self.created_at,
            "timeline": [
                {
                    "seq": t.seq,
                    "ts": t.ts,
                    "event_type": t.event_type,
                    "actor": t.actor,
                    "summary": t.summary,
                }
                for t in self.timeline
            ],
            "status_chain": [
                {"seq": s.seq, "status": s.status} for s in self.status_chain
            ],
        }


@dataclass
class EntryDiff:
    """一条对账差异(中文;kind 为差异类型的中文短语)。

    :ivar kind: 差异类型:"事件超前" / "账本缺失" / "状态不一致" / "字段不一致"。
    :ivar expected_status: 重放(账本)侧的期望最终状态;账本无事件时为
        中文占位"(账本无事件)"。
    :ivar actual_status: 队列(entries 表)的实际状态;条目不存在时为
        中文占位"(条目不存在)"。
    :ivar first_divergent_seq: 首个分歧事件 seq(账本缺失/无法定位时为 None)。
    :ivar detail: 中文差异说明(字段级差异列出每个字段的期望/实际值)。
    """

    entry_id: int
    kind: str
    expected_status: str
    actual_status: str
    first_divergent_seq: int | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "kind": self.kind,
            "expected_status": self.expected_status,
            "actual_status": self.actual_status,
            "first_divergent_seq": self.first_divergent_seq,
            "detail": self.detail,
        }


@dataclass
class ReplayReport:
    """重放对账报告(确定性:同一输入 → 同一报告,零墙钟字段)。"""

    ok: bool
    events_total: int
    entries_projected: int
    entries_actual: int
    unknown_events: int
    diffs: list[EntryDiff] = field(default_factory=list)
    projection: dict[int, ProjectedEntry] = field(default_factory=dict)
    #: 账本级错误(payload 非法 JSON / 读取失败——疑似篡改或损坏)。
    ledger_error: str = ""
    #: 四眼留痕(approvals)投影对账(A234,注入 four_eyes_factory 才启用):
    #: 重放条目数(0 = 未启用该段,向后兼容)。
    approvals_projected: int = 0
    #: approvals 差异独立清单(中文);**不影响** ok / exit_code(不阻塞
    #: entries 状态对账结论)。
    approval_diffs: list[ApprovalDiff] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """CLI 退出码:0 = 一致;2 = 检出差异 / 账本错误。

        四眼留痕(approvals)差异不参与判定(独立清单,不阻塞状态对账)。
        """
        return EXIT_OK if self.ok else EXIT_MISMATCH

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "events_total": self.events_total,
            "entries_projected": self.entries_projected,
            "entries_actual": self.entries_actual,
            "unknown_events": self.unknown_events,
            "ledger_error": self.ledger_error,
            "approvals_projected": self.approvals_projected,
            "approval_diffs": [d.to_dict() for d in self.approval_diffs],
            "diffs": [d.to_dict() for d in self.diffs],
            "projection": [
                self.projection[eid].to_dict() for eid in sorted(self.projection)
            ],
        }


@dataclass(frozen=True)
class AheadAction:
    """一条"事件超前"差异的补齐动作(确定性,中文可读)。

    :ivar entry_id: 条目编号。
    :ivar seq: 未生效事件的 seq(定位"第几条事件超前")。
    :ivar event_type: 事件类型(如 ``entry_approved``)。
    :ivar action: 补齐动作令牌(approve / reject / submit);不可自动补齐
        时为空串。
    :ivar from_status: 计划起点(对账时队列里的实际状态)。
    :ivar to_status: 将达状态(重放链上的目标步)。
    :ivar note: 重做须携带的备注(取自事件 payload——保证补齐后**字段级**
        对账也归零,不只是状态)。
    :ivar auto: ``False`` = 仅列出供人工裁决,不进入执行清单。
    :ivar reason: ``auto=False`` 时的中文原因;可自动补齐时为空串。
    """

    entry_id: int
    seq: int
    event_type: str
    action: str
    from_status: str
    to_status: str
    note: str
    auto: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "seq": self.seq,
            "event_type": self.event_type,
            "action": self.action,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "note": self.note,
            "auto": self.auto,
            "reason": self.reason,
        }


@dataclass
class ApplyAheadOutcome:
    """``--yes`` 逐条执行补齐的结果(已执行/幂等跳过/错误,如实报告)。"""

    executed: list[AheadAction] = field(default_factory=list)
    skipped: list[tuple[AheadAction, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: 校验失败或回调失败后是否中止了剩余条目(绝不半途 apply 的执行侧体现)。
    aborted: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "executed": [a.to_dict() for a in self.executed],
            "skipped": [
                {"action": a.to_dict(), "note": note} for a, note in self.skipped
            ],
            "errors": list(self.errors),
            "aborted": self.aborted,
        }


@dataclass(frozen=True)
class ProjectedApprovals:
    """单条条目的四眼留痕投影(重放 ``four_eyes_*`` 事件所得)。

    :ivar reviewers: 双人署名序列 ``[第一人, 第二人]``(required=False 单人
        直通时为 ``[审核人]``);取该条目**最后一条**四眼事件的 payload。
    :ivar seq: 造成本投影的四眼事件 seq(差异定位用)。
    """

    reviewers: tuple[str, ...]
    seq: int


@dataclass
class ApprovalDiff:
    """一条四眼留痕(approvals)对账差异(中文;独立清单,不阻塞状态对账)。

    :ivar kind: 差异类型:"留痕滞后"(事件超前,崩溃窗口语义)/
        "留痕不一致"(互相不可解释,疑似改写)。
    :ivar expected_reviewers: 重放(账本)侧的留痕序列。
    :ivar actual_reviewers: approvals 表侧的实际留痕序列。
    :ivar event_seq: 对应四眼事件的 seq(定位差异来源)。
    :ivar detail: 中文差异说明。
    """

    entry_id: int
    kind: str
    expected_reviewers: list[str]
    actual_reviewers: list[str]
    event_seq: int
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "kind": self.kind,
            "expected_reviewers": list(self.expected_reviewers),
            "actual_reviewers": list(self.actual_reviewers),
            "event_seq": self.event_seq,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ApprovalHealAction:
    """一条"留痕滞后"差异的留痕补写动作(确定性,中文可读)。

    :ivar entry_id: 条目编号。
    :ivar reviewer: 将补写的审核人署名(账本四眼事件事实侧的名字——
        approvals 行仍记真人名,补写动作本身另行署名 :data:`HEAL_ACTOR`)。
    :ivar event_seq: 来源四眼事件 seq(补写依据,审计事件回指)。
    :ivar event_ts: 来源事件 ts(补写行 acted_at 的取值——确定性零墙钟)。
    """

    entry_id: int
    reviewer: str
    event_seq: int
    event_ts: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "reviewer": self.reviewer,
            "event_seq": self.event_seq,
            "event_ts": self.event_ts,
        }


@dataclass
class HealApprovalsOutcome:
    """``--heal-approvals`` 逐行补写的结果(已补写/幂等跳过/错误,如实报告)。"""

    executed: list[ApprovalHealAction] = field(default_factory=list)
    skipped: list[tuple[ApprovalHealAction, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: 回调失败后是否中止了剩余行(绝不半途 apply 的执行侧体现)。
    aborted: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "executed": [a.to_dict() for a in self.executed],
            "skipped": [
                {"action": a.to_dict(), "note": note} for a, note in self.skipped
            ],
            "errors": list(self.errors),
            "aborted": self.aborted,
        }


# ---------------------------------------------------------------------------
# decision↔storage 架构环解耦:可注入队列工厂(接缝,默认行为不变)
# ---------------------------------------------------------------------------
#: 注入的复核队列工厂(``(queue_path: str) -> Any``);``None`` = 未注入,
#: 走默认惰性导入 decision.review_queue.ReviewQueue(向后兼容)。
_REVIEW_QUEUE_FACTORY: Callable[[str], Any] | None = None

#: 注入的四眼队列工厂(``(queue_path: str) -> Any``);``None`` = 未注入,
#: 走默认惰性导入 decision.four_eyes.FourEyesQueue(向后兼容)。
_FOUR_EYES_FACTORY: Callable[[str], Any] | None = None


def set_queue_factories(
    review_factory: Callable[[str], Any] | None = None,
    four_eyes_factory: Callable[[str], Any] | None = None,
) -> None:
    """注入替代队列工厂,解 storage→decision 架构环(**接缝,非必需**)。

    本接缝是 replay 包与 decision 包之间依赖方向的正式松动点:原实现里
    CLI 层 4 处函数内惰性 ``import decision``(ReviewQueue × 3 /
    FourEyesQueue × 1);注入后 :func:`make_review_queue` /
    :func:`make_four_eyes_queue` 改用注入的工厂构造队列对象,storage 包
    不再(经默认路径之外的任何路径)触碰 decision——测试可注入替身,
    替代实现(如内存队列 / 其他存储)可整体替换决策层对接。

    - 工厂签名:``(queue_path: str) -> Any``(按路径构造,返回带
      ``list()/close()`` 的复核队列或 ``status()/close()`` 的四眼队列);
    - **参数为 ``None`` 表示"该项不变"**(不注入 / 只改其一均可);
    - 未注入任何工厂时,默认行为与历史版本逐字节一致(默认工厂内部
      函数内惰性导入 decision,**不要求现有调用方做任何改动**);
    - 注入是进程级全局态:测试用毕请调 :func:`reset_queue_factories`
      复位,避免跨用例泄漏。
    """
    global _REVIEW_QUEUE_FACTORY, _FOUR_EYES_FACTORY
    if review_factory is not None:
        _REVIEW_QUEUE_FACTORY = review_factory
    if four_eyes_factory is not None:
        _FOUR_EYES_FACTORY = four_eyes_factory


def reset_queue_factories() -> None:
    """复位为默认(惰性导入 decision)工厂;测试隔离用。"""
    global _REVIEW_QUEUE_FACTORY, _FOUR_EYES_FACTORY
    _REVIEW_QUEUE_FACTORY = None
    _FOUR_EYES_FACTORY = None


def _default_review_queue_factory(queue_path: str) -> Any:
    """默认复核队列工厂:惰性导入 decision.review_queue(向后兼容口径)。"""
    from netsentinel.decision.review_queue import ReviewQueue  # 惰性:接缝默认实现

    return ReviewQueue(queue_path)


def _default_four_eyes_factory(queue_path: str) -> Any:
    """默认四眼队列工厂:惰性导入 decision.four_eyes(向后兼容口径)。

    ``required=False``:对账 / 补写只读使用 status/close,required 取值
    不影响读路径(与原 CLI 装配 ``FourEyesQueue(path, False)`` 一致)。
    """
    from netsentinel.decision.four_eyes import FourEyesQueue  # 惰性:接缝默认实现

    return FourEyesQueue(queue_path, False)


def make_review_queue(queue_path: str) -> Any:
    """按路径构造复核队列:注入工厂优先,缺省惰性导入 decision(接缝入口)。"""
    factory = _REVIEW_QUEUE_FACTORY or _default_review_queue_factory
    return factory(queue_path)


def make_four_eyes_queue(queue_path: str) -> Any:
    """按路径构造四眼队列:注入工厂优先,缺省惰性导入 decision(接缝入口)。"""
    factory = _FOUR_EYES_FACTORY or _default_four_eyes_factory
    return factory(queue_path)
