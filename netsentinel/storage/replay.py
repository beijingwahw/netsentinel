"""事件账本重放与对账 —— 复核状态的离线重建(NetSentinel · A213)。

对标 event sourcing + saga 的"重放校验"::class:`~netsentinel.storage.event_log.EventLog`
里的 append-only 事件流是**事实**,复核队列 entries 表只是**投影**。本模块把
事实重放成投影(每条条目的最终状态 + 完整操作时间线),再与队列现状逐条
对账:

- **一致** → OK 报告(退出码 0):账本与状态互相印证,任何历史时刻可重建;
- **不一致** → 中文差异清单(退出码 2):每条差异给出 ``entry_id`` /
  期望状态(重放)/ 实际状态(队列)/ 首个分歧事件 seq,三类典型成因——

  1. ``事件超前``:账本事件已落盘但状态未生效(先账本后状态的**崩溃窗口**,
     A213 双写的预期语义:事件存在但状态可能滞后——不算数据损坏,重放如实
     报告并给出未生效事件的准确 seq,修复动作是重做该操作或人工裁决);
  2. ``账本缺失``:队列有条目但账本无任何事件(账本启用前的历史数据,或
     账本被整段删改);
  3. ``状态不一致 / 字段不一致``:重放结果与队列现状都无法互相解释(payload
     被篡改、或绕过双写直改了 entries 表)。

纯函数核心 :func:`replay_review_queue` 不直接依赖 decision 包:队列经
``queue_factory``(零参可调用,返回带 ``list()`` / ``close()`` 的队列对象)
注入,storage 层不反向 import 决策层(CLI 入口内惰性导入默认工厂)。

CLI(对齐 :mod:`netsentinel.ops.audit_verify` 惯例)::

    python -m netsentinel.storage.replay data/review_events.db data/review_queue.db
    python -m netsentinel.storage.replay 账本.db 队列.db --json [报告路径]

退出码:0 = 一致;1 = 输入错误(文件缺失/路径是目录/账本损坏到无法打开);
2 = 检出差异(含账本 payload 非法 JSON——疑似篡改)。

事件超前补齐(A222 · ``--apply-ahead``,半自动裁决)::

    python -m netsentinel.storage.replay 账本.db 队列.db --apply-ahead          # 预览
    python -m netsentinel.storage.replay 账本.db 队列.db --apply-ahead --yes    # 补齐

"事件超前"不是数据损坏(先账本后状态的崩溃窗口,A213 双写的预期语义),
但差异需要消除:``--apply-ahead`` 检出后给出**逐条重做**的补齐动作清单
(entry_id / 事件 / 将达状态,重做备注取自事件 payload 以保证字段对账归零)。
红线:**绝不静默自动改状态**——

- 不带 ``--yes``:仅打印将执行的动作清单(含"不可自动补齐,需人工裁决"
  项),不改动任何状态,退出码 0;
- 带 ``--yes``:先复跑对账打印差异,再打印全量动作清单(先全量校验后
  逐条执行,任何校验失败绝不半途 apply),经注入的 queue apply 回调逐条
  重做(幂等:执行前先查当前状态,已到重放链目标步则跳过并标注),执行
  后自动复跑对账验证归零;无超前差异 → 无动作退出 0。
- 退出码:0 = 预览 / 补齐后归零 / 无差异无动作;2 = 补齐失败(回调失败、
  校验失败、或复跑对账差异未归零);1 沿用输入错误语义。

补齐回调**注入式**(本模块不 import decision 包,对齐 A213 的依赖方向,
CLI 入口惰性导入默认实现):默认回调用**不接账本**的队列连接重做
``approve / reject / mark_submitted``——事件早已在账本,补的只是 entries
状态投影,重做不得重复记账。

交互确认(A234 · ``-i/--interactive``)::

    python -m netsentinel.storage.replay 账本.db 队列.db --apply-ahead -i

``--yes`` 之外的第三种确认口径:全量动作清单**先打印**,随后终端逐条
``y/n`` 确认(:func:`input` 阻塞,对齐 HUMAN_GATE 的 input 惯例)——仅
``y/Y`` 执行该条,``n`` / 空回车 / 乱码 / EOF 一律跳过并如实标注(红线:
交互确认绝不静默自动执行);非 TTY 环境中文拒绝并提示改用 ``--yes``,
``--yes`` 与 ``--interactive`` 互斥;执行后同样复跑对账。跳过留下的差异
如实保留(退出码按"差异是否归零"判定,与 ``--yes`` 同口径)。

四眼留痕(approvals)投影对账(A234,注入式启用)::

``replay_review_queue`` 新增仅关键字参数 ``four_eyes_factory``(默认
``None`` = 现状,不启用该段,向后兼容)。注入后重放 ``four_eyes_*``
事件重建 approvals 投影(``{entry_id: [第一人, 第二人]}``),与队列
``status()`` 读出的实际留痕对账:事件超前(账本已记双人齐但 approvals
表缺)→ ``留痕滞后`` 差异(中文,**不阻塞** entries 状态对账结论与退出
码,独立清单);非前缀缺失的互相不可解释 → ``留痕不一致``。事件类型
常量本地声明(字面值与 ``decision.four_eyes`` 一致),storage 层依旧不
反向 import 决策层;CLI 以 ``--four-eyes`` 开关装配默认工厂。

留痕滞后补齐(``--heal-approvals``,独立开关 · A234 报告建议的落地)::

    python -m netsentinel.storage.replay 账本.db 队列.db --apply-ahead --heal-approvals         # 预览(两清单)
    python -m netsentinel.storage.replay 账本.db 队列.db --apply-ahead --yes --heal-approvals   # 补齐 + 补写

对账能报"留痕滞后"但修复此前仍是人工:``--heal-approvals`` 把补写留痕
纳入半自动裁决,是**独立于 entries 状态补齐的第二条确认链**:

- 独立开关(默认关):只加 ``--yes`` 不加 ``--heal-approvals`` 时 approvals
  **一字不动**(两清单两确认,绝不捆绑);开关蕴含 ``--four-eyes`` 的
  approvals 投影对账,且仅在与 ``--apply-ahead`` 搭配时有效;
- dry(不带 ``--yes`` / ``-i``):打印将补写的 approvals 行(entry_id /
  将补 reviewer / 来源事件 seq),零改动;
- ``--yes --heal-approvals``(或 ``-i`` 逐条 y):经注入回调逐行补写——
  幂等预检(留痕已有该 reviewer 则跳过并标注),每行**先在账本补记
  ``approvals_healed`` 审计事件、署名 ``actor="replay-heal"``**(与真人
  复核署名可区分——防伪造留痕:后续审计恒能看出该行是重放补写而非
  真人点击),再写 approvals 行(``acted_at`` 取来源事件 ts,确定性零
  墙钟);单写人路径(事件只带一名 reviewer)自然只补一人;补写后复跑
  approvals 对账验证归零(独立清单,仍不影响状态对账退出码;回调失败
  与 entries 补齐同口径——中止剩余、如实报告、退出 2);
- 无滞后差异 → approvals 无动作;账本校验失败(疑似篡改)→ 一切自动
  补齐整体拒绝(不按可疑事实补写留痕)。

时间戳字段(created_at / updated_at)**不参与对账**:双写时事件与条目各取
一次秒级时钟,合法跨秒差不代表事实分歧(文档化豁免,避免误报)。

零第三方依赖;全部离线;确定性(同一账本+同一队列 → 同一报告,不含墙钟)。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from netsentinel.storage.event_log import (
    EVENT_ENTRY_ADDED,
    EVENT_ENTRY_ANNOTATED,
    EVENT_ENTRY_APPROVED,
    EVENT_ENTRY_MARKED_SUBMITTED,
    EVENT_ENTRY_REJECTED,
    Event,
    EventLog,
    EventLogError,
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
    "build_approvals_projection",
    "build_projection",
    "execute_apply_ahead",
    "execute_heal_approvals",
    "main",
    "plan_apply_ahead",
    "plan_heal_approvals",
    "reconcile_approvals",
    "replay_review_queue",
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

#: 对账参与比较的字段(时间戳豁免,见模块 docstring)。
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
# 纯函数:事件流 → 投影
# ---------------------------------------------------------------------------


def _can_reach(source: str, target: str) -> bool:
    """状态机可达判定(含自反):source 沿合法迁移能否到达 target。

    只读使用状态机语义(零改动);重放拿它定位"链上第一个不可能出现在
    实际状态之前的步",从而给出首个分歧事件 seq。
    """
    if source == target:
        return True
    frontier = {source}
    seen = {source}
    while frontier:
        nxt: set[str] = set()
        for s in frontier:
            for t in _REACHABLE.get(s, frozenset()):
                if t == target:
                    return True
                if t not in seen:
                    seen.add(t)
                    nxt.add(t)
        frontier = nxt
    return False


def build_projection(
    events: list[Event],
) -> tuple[dict[int, ProjectedEntry], int]:
    """把事件流按 seq 顺序重放为投影(``{entry_id: ProjectedEntry}``)。

    纯函数、确定性:同一事件列表恒得同一投影。语义(与双写侧一一对应):

    - ``entry_added``:建立条目快照(site_url/verdict/evidence_zip/note/
      priority_weight),状态入链为 pending;
    - ``entry_approved`` / ``entry_rejected`` / ``entry_marked_submitted``:
      状态按 payload 的 ``to_status`` 入链;payload 带非空 ``note`` 时改写
      备注(与队列"空备注保留原备注"语义一致);
    - ``entry_annotated``:改写提权加项(非状态迁移,不入状态链);
    - 其余类型:计入返回的 unknown 计数,不应用(前向兼容:新域事件不炸老
      重放器)。

    宽容重放(不校验迁移合法性):账本被动过(重复迁移 / 缺 entry_added)
    时照常应用,让**对账**去暴露事实与投影的分歧——重放器绝不当校验器,
    以免与队列状态机语义产生第二套判例(红线:状态机语义零改动)。
    """
    entries: dict[int, ProjectedEntry] = {}
    unknown = 0
    for ev in events:
        etype = ev.event_type
        payload = ev.payload or {}
        if etype == EVENT_ENTRY_ADDED:
            p = entries.setdefault(
                ev.entry_id, ProjectedEntry(entry_id=ev.entry_id)
            )
            p.site_url = str(payload.get("site_url") or "")
            p.verdict = str(payload.get("verdict") or "")
            p.evidence_zip = str(payload.get("evidence_zip") or "")
            p.note = str(payload.get("note") or "")
            p.priority_weight = float(payload.get("priority_weight") or 0.0)
            if not p.created_at:
                p.created_at = ev.ts
            to_status = str(payload.get("status") or "pending")
            p.status = to_status
            p.status_chain.append(StatusStep(ev.seq, to_status))
            p.timeline.append(
                TimelineItem(
                    ev.seq,
                    ev.ts,
                    etype,
                    ev.actor,
                    f"入列待复核:{p.site_url or '(未知站点)'}"
                    f"(判定 {p.verdict or '-'})",
                )
            )
        elif etype in (
            EVENT_ENTRY_APPROVED,
            EVENT_ENTRY_REJECTED,
            EVENT_ENTRY_MARKED_SUBMITTED,
        ):
            p = entries.setdefault(
                ev.entry_id, ProjectedEntry(entry_id=ev.entry_id)
            )
            if not p.created_at:
                p.created_at = ev.ts
            from_status = str(payload.get("from_status") or "")
            to_status = str(payload.get("to_status") or "")
            note = str(payload.get("note") or "")
            if note:
                p.note = note
            p.status = to_status
            p.status_chain.append(StatusStep(ev.seq, to_status))
            action = {
                EVENT_ENTRY_APPROVED: "人工确认",
                EVENT_ENTRY_REJECTED: "人工驳回",
                EVENT_ENTRY_MARKED_SUBMITTED: "提交回写",
            }[etype]
            p.timeline.append(
                TimelineItem(
                    ev.seq,
                    ev.ts,
                    etype,
                    ev.actor,
                    f"{action}:{from_status or '?'} → {to_status or '?'}",
                )
            )
        elif etype == EVENT_ENTRY_ANNOTATED:
            p = entries.setdefault(
                ev.entry_id, ProjectedEntry(entry_id=ev.entry_id)
            )
            if not p.created_at:
                p.created_at = ev.ts
            previous = payload.get("previous_weight")
            weight = float(payload.get("priority_weight") or 0.0)
            p.priority_weight = weight
            prev_text = f"{previous}" if previous is not None else "?"
            p.timeline.append(
                TimelineItem(
                    ev.seq,
                    ev.ts,
                    etype,
                    ev.actor,
                    f"批注提权:{prev_text} → {weight}",
                )
            )
        else:
            unknown += 1
    return entries, unknown


# ---------------------------------------------------------------------------
# 对账
# ---------------------------------------------------------------------------


def _diff_field_mismatch(
    proj: ProjectedEntry, actual: Any
) -> EntryDiff | None:
    """字段级对账(状态一致时才做):时间戳豁免,只比五个事实字段。

    首个分歧 seq 取"造成不一致取值的最早事件"——按各字段最后写入事件
    seq 取最小(字段值由最后一次写入决定,但分歧从最早的可疑写入开始)。
    """
    mismatched: list[str] = []
    seqs: list[int] = []
    parts: list[str] = []
    for name in _RECONCILE_FIELDS:
        expected = getattr(proj, name)
        got = getattr(actual, name, None)
        if name == "priority_weight":
            try:
                same = float(got or 0.0) == float(expected)
            except (TypeError, ValueError):
                same = False
        else:
            same = str(got or "") == str(expected)
        if not same:
            mismatched.append(name)
            parts.append(
                f"{_FIELD_CN.get(name, name)}(期望 {expected!r},实际 {got!r})"
            )
    if not mismatched:
        return None
    # 各差异字段的"写入来源事件"seq:重放侧由时间线回溯(每种事件类型
    # 可能写入的字段集);取最小值 = 最早的可疑写入。
    writable_by_event: tuple[tuple[str, tuple[str, ...]], ...] = (
        (EVENT_ENTRY_ADDED, _RECONCILE_FIELDS),
        (EVENT_ENTRY_ANNOTATED, ("priority_weight",)),
        (EVENT_ENTRY_APPROVED, ("note",)),
        (EVENT_ENTRY_REJECTED, ("note",)),
    )
    for t in proj.timeline:
        if any(
            t.event_type == etype and any(m in fields for m in mismatched)
            for etype, fields in writable_by_event
        ):
            seqs.append(t.seq)
    divergent = min(seqs) if seqs else None
    return EntryDiff(
        entry_id=proj.entry_id,
        kind="字段不一致",
        expected_status=proj.status,
        actual_status=str(getattr(actual, "status", "")),
        first_divergent_seq=divergent,
        detail="账本与队列的字段取值对不上(疑似 payload 被篡改或条目被外部改写):"
        + ";".join(parts),
    )


def _diff_status(
    proj: ProjectedEntry, actual: Any
) -> EntryDiff | None:
    """状态对账:三岔——一致(返回 None)/ 事件超前 / 状态不一致。"""
    actual_status = str(getattr(actual, "status", "") or "")
    chain = proj.status_chain
    if not chain:
        # 账本里只有非状态事件(如仅批注):状态无从重放,交给字段对账。
        return None
    if chain[-1].status == actual_status:
        return None  # 状态一致,字段差异由 _diff_field_mismatch 负责
    # 实际状态出现在链上(较早位置)→ 其后事件已落盘未生效 = 事件超前。
    idx = max(
        (i for i, step in enumerate(chain) if step.status == actual_status),
        default=None,
    )
    if idx is not None and idx < len(chain) - 1:
        ahead = chain[idx + 1]
        return EntryDiff(
            entry_id=proj.entry_id,
            kind="事件超前",
            expected_status=proj.status,
            actual_status=actual_status,
            first_divergent_seq=ahead.seq,
            detail=(
                "账本事件已落盘但状态未生效(第 "
                f"{ahead.seq} 条事件起):先账本后状态的崩溃窗口,"
                "重做该操作或人工裁决即可,不属于数据损坏"
            ),
        )
    # 实际状态不在链上:链上第一个"不可能抵达实际状态"的步 = 首个分歧。
    for step in chain:
        if not _can_reach(step.status, actual_status):
            return EntryDiff(
                entry_id=proj.entry_id,
                kind="状态不一致",
                expected_status=proj.status,
                actual_status=actual_status,
                first_divergent_seq=step.seq,
                detail=(
                    f"重放状态 {proj.status} 与队列实际状态 {actual_status} "
                    "互相不可解释(疑似 payload 被篡改、事件被删除,"
                    "或状态被绕过双写直改)"
                ),
            )
    return EntryDiff(
        entry_id=proj.entry_id,
        kind="状态不一致",
        expected_status=proj.status,
        actual_status=actual_status,
        first_divergent_seq=None,
        detail=(
            f"队列实际状态 {actual_status} 在账本事件中无任何记载"
            "(疑似该状态迁移的事件被删除,或状态被绕过双写直改)"
        ),
    )


def _reconcile(
    projection: dict[int, ProjectedEntry], actual: dict[int, Any]
) -> list[EntryDiff]:
    """投影 × 队列现状 → 差异清单(按 entry_id 升序,每条目至多一条)。"""
    diffs: list[EntryDiff] = []
    for entry_id in sorted(set(projection) | set(actual)):
        proj = projection.get(entry_id)
        act = actual.get(entry_id)
        if proj is not None and act is None:
            diffs.append(
                EntryDiff(
                    entry_id=entry_id,
                    kind="事件超前",
                    expected_status=proj.status,
                    actual_status="(条目不存在)",
                    first_divergent_seq=(
                        proj.timeline[0].seq if proj.timeline else None
                    ),
                    detail=(
                        "账本已记录该条目(第 "
                        f"{proj.timeline[0].seq if proj.timeline else '?'} 条事件起)"
                        "但队列无此条目:入列事件落盘后、条目写入提交前崩溃,"
                        "或条目被外部删除"
                    ),
                )
            )
            continue
        if proj is None and act is not None:
            diffs.append(
                EntryDiff(
                    entry_id=entry_id,
                    kind="账本缺失",
                    expected_status="(账本无事件)",
                    actual_status=str(getattr(act, "status", "")),
                    first_divergent_seq=None,
                    detail=(
                        "队列有此条目但账本无任何事件:账本启用前的历史数据,"
                        "或该条目的事件被整段删改"
                    ),
                )
            )
            continue
        assert proj is not None and act is not None
        diff = _diff_status(proj, act) or _diff_field_mismatch(proj, act)
        if diff is not None:
            diffs.append(diff)
    return diffs


# ---------------------------------------------------------------------------
# 四眼留痕(approvals)投影对账(A234):纯函数重放 + 纯函数对账
# ---------------------------------------------------------------------------


def build_approvals_projection(
    events: Iterable[Event],
) -> dict[int, ProjectedApprovals]:
    """把 ``four_eyes_*`` 事件重放为 approvals 投影(纯函数,确定性)。

    每条条目取**最后一条**四眼域事件的 payload(事件按 seq 升序遍历,
    后写覆盖先写):``reviewers`` 列表即双人署名序列 ``[第一人, 第二人]``,
    缺失时回退单键 ``reviewer``。其余事件类型不参与(与
    :func:`build_projection` 的未识别前向兼容口径一致——本函数不重复计数
    unknown,计数归 :func:`build_projection`)。

    宽容重放(不校验"awaiting 先于 approved"等时序):账本被动过时由
    **对账**(:func:`reconcile_approvals`)暴露事实与留痕的分歧,重放器
    绝不当校验器(零第二套判例)。
    """
    projection: dict[int, ProjectedApprovals] = {}
    for ev in events:
        if ev.event_type not in FOUR_EYES_EVENT_TYPES:
            continue
        payload = ev.payload or {}
        reviewers = [str(r) for r in (payload.get("reviewers") or [])]
        if not reviewers:
            single = str(payload.get("reviewer") or "").strip()
            if single:
                reviewers = [single]
        projection[ev.entry_id] = ProjectedApprovals(
            reviewers=tuple(reviewers), seq=ev.seq
        )
    return projection


def reconcile_approvals(
    projection: dict[int, ProjectedApprovals], four_eyes_queue: Any
) -> list[ApprovalDiff]:
    """approvals 投影 × 队列实际留痕 → 差异清单(独立,按 entry_id 升序)。

    只做**投影 → 实际**单向对账:required=False 的单人直通路径写 approvals
    但不发四眼事件(设计如此),反向对账会把合法现状误报为差异。差异两类:

    - ``留痕滞后``:实际留痕是期望序列的**前缀且更短**(账本已记双人齐
      但表缺)——双人齐路径"先账本后留痕"的崩溃窗口语义,补写留痕或
      人工裁决即可,不属于数据损坏;
    - ``留痕不一致``:其余不匹配(名字不同 / 实际多出)——留痕与账本
      事件互相不可解释,疑似外部改写,需人工裁决。

    :param four_eyes_queue: 注入的四眼队列(鸭子契约:``status(entry_id)``
        返回 ``{"reviewers": [...]}``;条目不存在抛 ``ValueError`` 视为
        无留痕——"条目不存在"本身已由 entries 对账报告,此处不重复)。
    """
    status_fn = getattr(four_eyes_queue, "status", None)
    if not callable(status_fn):
        raise ValueError(
            "注入的 four_eyes 队列缺少 status(entry_id) 能力,无法对账 approvals 投影"
        )
    diffs: list[ApprovalDiff] = []
    for entry_id in sorted(projection):
        proj = projection[entry_id]
        expected = list(proj.reviewers)
        actual: list[str] = []
        try:
            st = status_fn(entry_id)
        except ValueError:
            st = None  # 条目不存在:entries 对账已报告,此处按无留痕处理
        if isinstance(st, dict):
            actual = [str(r) for r in (st.get("reviewers") or [])]
        if actual == expected:
            continue
        expected_text = "、".join(expected) if expected else "(无)"
        actual_text = "、".join(actual) if actual else "(无)"
        if actual == expected[: len(actual)]:
            diffs.append(
                ApprovalDiff(
                    entry_id=entry_id,
                    kind=APPROVAL_DIFF_LAGGING,
                    expected_reviewers=expected,
                    actual_reviewers=actual,
                    event_seq=proj.seq,
                    detail=(
                        f"账本已记录四眼确认(第 {proj.seq} 条事件,应留痕 "
                        f"{expected_text})但 approvals 表仅 {actual_text}:"
                        "双人齐路径先账本后留痕的崩溃窗口(写入留痕前终止),"
                        "属事件超前而非数据损坏,补写留痕或人工裁决即可;"
                        "本差异不阻塞 entries 状态对账"
                    ),
                )
            )
        else:
            diffs.append(
                ApprovalDiff(
                    entry_id=entry_id,
                    kind=APPROVAL_DIFF_INCONSISTENT,
                    expected_reviewers=expected,
                    actual_reviewers=actual,
                    event_seq=proj.seq,
                    detail=(
                        f"approvals 表留痕与账本四眼事件互相不可解释"
                        f"(期望 {expected_text},实际 {actual_text}):"
                        "疑似留痕被外部改写或四眼事件被删改,需人工裁决"
                    ),
                )
            )
    return diffs


# ---------------------------------------------------------------------------
# 事件超前补齐(--apply-ahead):纯函数计划 + 纯函数执行
# ---------------------------------------------------------------------------


def plan_apply_ahead(
    report: ReplayReport, events: Iterable[Event]
) -> tuple[list[AheadAction], list[str]]:
    """从对账报告规划"事件超前"差异的补齐动作清单(纯函数,确定性)。

    只处理 ``kind == "事件超前"`` 的差异(账本缺失/状态不一致属于篡改或
    历史数据,补状态只会掩盖问题,**绝不**自动重做)。对每处差异沿重放
    状态链从"队列当前状态"走到"重放最终状态",每个未生效的状态步生成
    一条动作(事件 → 动作映射见 :data:`AHEAD_EVENT_ACTIONS`,备注取自事件
    payload,保证补齐后字段级对账也归零)。

    :param report: 对账报告(通常来自 :func:`replay_review_queue`)。
    :param events: 账本全量事件(用于按 seq 取事件的类型与 payload)。
    :return: ``(动作清单, 校验错误清单)``。校验错误非空 = 账本事实与底层
        状态机互相矛盾(疑似篡改,如 pending 直迁 submitted 的事件)——
        调用方在 ``--yes`` 模式下必须**整体拒绝执行**(绝不半途 apply),
        涉及的条目以 ``auto=False`` 列出供人工裁决;对应动作与不可自动
        补齐的条目(队列无此条目等)同样以 ``auto=False`` 列出但**不**阻断
        其他条目的补齐。
    """
    events_by_seq = {ev.seq: ev for ev in events}
    actions: list[AheadAction] = []
    validation_errors: list[str] = []
    for d in report.diffs:
        if d.kind != "事件超前":
            continue
        proj = report.projection.get(d.entry_id)
        if proj is None:  # pragma: no cover - _reconcile 不会产出该组合
            actions.append(
                AheadAction(
                    d.entry_id, d.first_divergent_seq or 0, "", "", "",
                    d.expected_status, "", False,
                    "重放投影缺失,无法定位补齐起点,需人工裁决",
                )
            )
            continue
        chain = proj.status_chain
        if d.actual_status == "(条目不存在)":
            # 入列事件已落盘但条目行缺失:entries 是 AUTOINCREMENT 主键,
            # 重做 add 无法保证取回同一 id,自动补齐反而制造新差异。
            first_ev = (
                events_by_seq.get(d.first_divergent_seq)
                if d.first_divergent_seq is not None
                else None
            )
            actions.append(
                AheadAction(
                    d.entry_id, d.first_divergent_seq or 0,
                    first_ev.event_type if first_ev is not None else "",
                    "", "(条目不存在)", d.expected_status, "", False,
                    "队列无此条目:入列事件已落盘但条目行缺失,"
                    "无法自动补齐(需人工裁决)",
                )
            )
            continue
        idx = max(
            (i for i, step in enumerate(chain) if step.status == d.actual_status),
            default=None,
        )
        if idx is None:  # pragma: no cover - _diff_status 判"事件超前"时必在链上
            actions.append(
                AheadAction(
                    d.entry_id, d.first_divergent_seq or 0, "", "", "",
                    d.expected_status, "", False,
                    f"实际状态 {d.actual_status} 不在重放链上,"
                    "无法定位补齐起点,需人工裁决",
                )
            )
            continue
        from_status = d.actual_status
        for step in chain[idx + 1:]:
            ev = events_by_seq.get(step.seq)
            spec = AHEAD_EVENT_ACTIONS.get(ev.event_type) if ev is not None else None
            if spec is None:
                # 链上出现不可重做的状态步(如重复入列步 → pending)。
                actions.append(
                    AheadAction(
                        d.entry_id, step.seq,
                        ev.event_type if ev is not None else "",
                        "", from_status, step.status, "", False,
                        f"第 {step.seq} 条事件不是可重做的状态迁移"
                        f"(→ {step.status}),需人工裁决",
                    )
                )
                break  # 后续步依赖本步,整条停止规划
            action, legal_from, to_status = spec
            note = str((ev.payload or {}).get("note") or "") if ev is not None else ""
            if from_status != legal_from:
                # 宽容重放应用了底层状态机不可能批准的迁移 → 疑似篡改:
                # 校验失败,--yes 模式整体拒绝执行(绝不半途 apply)。
                validation_errors.append(
                    f"条目 {d.entry_id} 第 {step.seq} 条事件要求 "
                    f"{legal_from} → {to_status},与重放链起点 "
                    f"{from_status} 不符(疑似账本被动过):拒绝自动补齐"
                )
                actions.append(
                    AheadAction(
                        d.entry_id, step.seq, ev.event_type, action,
                        from_status, to_status, note, False,
                        "校验失败:事件迁移与底层状态机不符,"
                        "已整体拒绝自动补齐(需人工裁决)",
                    )
                )
                break
            actions.append(
                AheadAction(
                    d.entry_id, step.seq, ev.event_type, action,
                    from_status, to_status, note, True, "",
                )
            )
            from_status = to_status
    return actions, validation_errors


def execute_apply_ahead(
    actions: Iterable[AheadAction],
    queue_factory: Callable[[], Any],
    apply_callback: Callable[[int, str, str], Any],
) -> ApplyAheadOutcome:
    """逐条执行补齐动作(纯函数核心,幂等,绝不半途 apply)。

    前置契约:调用方已完成**全量**校验(先全量 dry 列表再逐条执行);
    本函数只执行 ``auto=True`` 的动作。对每条动作:

    1. 幂等预检:经 ``queue_factory`` 重开队列读条目**当前**状态——
       已达目标步(状态已在重放链上)→ 跳过并中文标注,不报错;
       条目已不存在 → 跳过并标注;
    2. 当前状态与计划起点不符(对账后被并发改动)→ 中止剩余条目并如实
       报告已执行条数;
    3. 调用注入的 ``apply_callback(entry_id, action, note)`` 重做;回调抛错
       → 中止剩余条目,**已执行条目如实保留在 ``executed`` 中**。

    :param queue_factory: 零参可调用,返回队列对象(需 ``list()``,用毕由
        本函数关闭)——与 :func:`replay_review_queue` 同一注入契约。
    :param apply_callback: 注入的补齐回调(storage 不 import decision 层)。
    """
    outcome = ApplyAheadOutcome()
    auto = [a for a in actions if a.auto]
    if not auto:
        return outcome
    status_queue = queue_factory()
    try:
        for act in auto:
            # 幂等预检:每条动作前重读当前状态(看见其他连接已提交的变更)。
            entries = {int(e.id): e for e in status_queue.list()}
            entry = entries.get(act.entry_id)
            if entry is None:
                outcome.skipped.append((act, "条目已不存在,跳过"))
                continue
            current = str(getattr(entry, "status", "") or "")
            if current == act.to_status:
                outcome.skipped.append(
                    (act, f"状态已在重放链上(现为 {current}),幂等跳过")
                )
                continue
            if current != act.from_status:
                outcome.aborted = True
                outcome.errors.append(
                    f"条目 {act.entry_id} 当前状态 {current} 与计划起点 "
                    f"{act.from_status} 不符(对账后被并发改动):中止剩余 "
                    f"补齐,已执行 {len(outcome.executed)} 条,其余未执行"
                )
                break
            try:
                apply_callback(act.entry_id, act.action, act.note)
            except Exception as exc:  # noqa: BLE001 - 回调任何失败都要如实报告
                outcome.aborted = True
                outcome.errors.append(
                    f"条目 {act.entry_id} 补齐回调失败:{exc}——中止剩余补齐,"
                    f"已执行 {len(outcome.executed)} 条,其余未执行"
                )
                break
            outcome.executed.append(act)
    finally:
        try:
            status_queue.close()
        except Exception:  # noqa: BLE001, S110 - 不因清理失败丢执行结果
            pass
    return outcome


# ---------------------------------------------------------------------------
# 留痕滞后补齐(--heal-approvals):纯函数计划 + 纯函数执行
# ---------------------------------------------------------------------------


def plan_heal_approvals(
    report: ReplayReport, events: Iterable[Event]
) -> list[ApprovalHealAction]:
    """从对账报告规划"留痕滞后"差异的留痕补写清单(纯函数,确定性)。

    只处理 :data:`APPROVAL_DIFF_LAGGING` 差异(``留痕不一致`` 属疑似外部
    改写,补写只会掩盖问题,**绝不**自动补):每处差异补写期望留痕序列中
    实际留痕(前缀)之后缺失的审核人——双人齐路径补第二人,单写人路径
    (事件只带一名 reviewer)自然只补一人。

    :param report: 对账报告(``approval_diffs`` 来自注入 ``four_eyes_factory``
        的 :func:`replay_review_queue`)。
    :param events: 账本全量事件(按 seq 取来源事件的 ts 作为补写行的
        ``acted_at``——确定性,零墙钟;seq 查不到时防御性置空串)。
    :return: 补写动作清单(按差异的 entry_id 升序、reviewer 顺序)。
    """
    events_by_seq = {ev.seq: ev for ev in events}
    actions: list[ApprovalHealAction] = []
    for d in report.approval_diffs:
        if d.kind != APPROVAL_DIFF_LAGGING:
            continue
        source = events_by_seq.get(d.event_seq)
        for reviewer in d.expected_reviewers[len(d.actual_reviewers):]:
            actions.append(
                ApprovalHealAction(
                    entry_id=d.entry_id,
                    reviewer=reviewer,
                    event_seq=d.event_seq,
                    event_ts=str(source.ts) if source is not None else "",
                )
            )
    return actions


def execute_heal_approvals(
    actions: Iterable[ApprovalHealAction],
    four_eyes_factory: Callable[[], Any],
    heal_callback: Callable[[int, str, int, str], Any],
) -> HealApprovalsOutcome:
    """逐行补写 approvals 留痕(纯函数核心,幂等,绝不半途 apply)。

    前置契约:调用方已完成**独立显式确认**(``--yes --heal-approvals`` 或
    交互 y——绝不与 entries 状态补齐共用同一默认动作)。对每条动作:

    1. 幂等预检:经 ``four_eyes_factory`` 重开四眼队列读当前留痕——该
       reviewer 已在留痕中 → 跳过并中文标注,不报错;条目不存在 → 跳过
       并标注(entries 对账已报告,此处不重复);
    2. 调用注入的 ``heal_callback(entry_id, reviewer, event_seq, event_ts)``
       补写(默认实现先补记 ``actor="replay-heal"`` 审计事件再写 approvals
       行,见 :func:`_default_heal_callback`);回调抛错 → 中止剩余行,
       **已补写条目如实保留在 ``executed`` 中**。

    :param four_eyes_factory: 零参可调用,返回带 ``status(entry_id)`` /
        ``close()`` 的四眼队列对象(与 :func:`replay_review_queue` 同一
        注入契约;``None`` 视为契约违规,中文报错)。
    :param heal_callback: 注入的补写回调(storage 不 import decision 层)。
    """
    if four_eyes_factory is None:
        raise ValueError(
            "留痕补写需要注入 four_eyes_factory(读当前留痕做幂等预检),当前为 None"
        )
    outcome = HealApprovalsOutcome()
    todo = list(actions)
    if not todo:
        return outcome
    status_queue = four_eyes_factory()
    try:
        for act in todo:
            try:
                st = status_queue.status(act.entry_id)
            except ValueError:
                outcome.skipped.append((act, "条目已不存在,跳过"))
                continue
            current = (
                [str(r) for r in (st.get("reviewers") or [])]
                if isinstance(st, dict)
                else []
            )
            if act.reviewer in current:
                outcome.skipped.append(
                    (act, f"留痕已有 {act.reviewer}(现为 {len(current)} 人),幂等跳过")
                )
                continue
            try:
                heal_callback(act.entry_id, act.reviewer, act.event_seq, act.event_ts)
            except Exception as exc:  # noqa: BLE001 - 回调任何失败都要如实报告
                outcome.aborted = True
                outcome.errors.append(
                    f"条目 {act.entry_id} 留痕补写回调失败:{exc}——中止剩余补写,"
                    f"已补写 {len(outcome.executed)} 行,其余未执行"
                )
                break
            outcome.executed.append(act)
    finally:
        try:
            status_queue.close()
        except Exception:  # noqa: BLE001, S110 - 不因清理失败丢执行结果
            pass
    return outcome


# ---------------------------------------------------------------------------
# 主入口(纯函数)+ CLI
# ---------------------------------------------------------------------------


def replay_review_queue(
    event_log: EventLog,
    queue_factory: Callable[[], Any],
    *,
    four_eyes_factory: Callable[[], Any] | None = None,
) -> ReplayReport:
    """重放账本重建复核投影,并与队列现状对账(确定性,零墙钟)。

    :param event_log: 事件账本(只读使用:仅 iter_events)。
    :param queue_factory: 零参可调用,返回队列对象(需 ``list()`` 供对账、
        ``close()`` 由本函数在用毕后调用)——注入而非直接依赖 decision 包,
        storage 层不反向 import 决策层。
    :param four_eyes_factory: 四眼队列工厂(A234,默认 ``None`` = 不启用
        approvals 投影对账段,行为与旧版一致,向后兼容)。注入时返回带
        ``status(entry_id)`` / ``close()`` 的队列对象,本函数重放
        ``four_eyes_*`` 事件重建留痕投影并对账,差异落入
        :attr:`ReplayReport.approval_diffs`(独立清单,**不影响** ok /
        退出码——不阻塞 entries 状态对账)。
    :return: :class:`ReplayReport`(账本损坏/payload 非法 JSON 时**不抛**,
        落入 ``ledger_error`` 并计为差异,退出码语义 2)。
    """
    try:
        events = event_log.iter_events()
        projection, unknown = build_projection(events)
        ledger_error = ""
    except EventLogError as exc:
        return ReplayReport(
            ok=False,
            events_total=0,
            entries_projected=0,
            entries_actual=0,
            unknown_events=0,
            diffs=[],
            projection={},
            ledger_error=str(exc),
        )
    queue = queue_factory()
    try:
        entries = queue.list()
    finally:
        try:
            queue.close()
        except Exception:  # noqa: BLE001, S110 - 对账不因清理失败而丢报告
            pass
    actual = {int(e.id): e for e in entries}
    diffs = _reconcile(projection, actual)
    # 四眼留痕(approvals)投影对账:注入工厂才启用(默认关 = 现状)。
    approval_diffs: list[ApprovalDiff] = []
    approvals_projected = 0
    if four_eyes_factory is not None:
        approvals_projection = build_approvals_projection(events)
        approvals_projected = len(approvals_projection)
        four_eyes_queue = four_eyes_factory()
        try:
            approval_diffs = reconcile_approvals(
                approvals_projection, four_eyes_queue
            )
        finally:
            try:
                four_eyes_queue.close()
            except Exception:  # noqa: BLE001, S110 - 同上,清理失败不丢报告
                pass
    ok = not diffs and not ledger_error
    return ReplayReport(
        ok=ok,
        events_total=len(events),
        entries_projected=len(projection),
        entries_actual=len(actual),
        unknown_events=unknown,
        diffs=diffs,
        projection=projection,
        ledger_error=ledger_error,
        approvals_projected=approvals_projected,
        approval_diffs=approval_diffs,
    )


def _echo_approval_diffs_lines(report: Any, emit: Any) -> None:
    """四眼留痕(approvals)差异的中文清单行(人读报告与 apply-ahead 共用)。

    独立清单:**不阻塞** entries 状态对账结论与退出码(留痕滞后属事件
    超前的崩溃窗口语义,修复动作是补写留痕或人工裁决)。
    """
    if not report.approval_diffs:
        if getattr(report, "approvals_projected", 0):
            emit(
                f"四眼留痕对账(approvals 投影,{report.approvals_projected} 条):"
                "一致(独立清单,不影响上方状态对账结论)"
            )
        return
    emit(
        f"四眼留痕对账(approvals 投影,{report.approvals_projected} 条):"
        f"检出差异 {len(report.approval_diffs)} 处(独立清单,不影响上方状态对账结论)"
    )
    for d in report.approval_diffs:
        expected_text = "、".join(d.expected_reviewers) or "(无)"
        actual_text = "、".join(d.actual_reviewers) or "(无)"
        emit(
            f"  - 条目 {d.entry_id}[{d.kind}] 应留痕 {expected_text} / "
            f"实际 {actual_text} / 对应事件 第 {d.event_seq} 条"
        )
        emit(f"    {d.detail}")


def _print_human_report(report: ReplayReport, ledger_path: str, queue_path: str) -> None:
    """打印中文人读报告(对齐 audit_verify 的报告版式)。"""
    bar = "=" * 60
    print(bar)
    print("NetSentinel 事件账本重放对账报告(复核状态可重建性核验)")
    print(bar)
    print(f"事件账本:{ledger_path}")
    print(f"复核队列:{queue_path}")
    print(
        f"事件总数 {report.events_total} | 重放条目 {report.entries_projected} | "
        f"队列条目 {report.entries_actual}"
        + (f" | 未识别事件 {report.unknown_events}(不参与重放)" if report.unknown_events else "")
    )
    if report.ledger_error:
        print(f"账本错误:{report.ledger_error}")
    if not report.diffs and not report.ledger_error:
        print("对账结论:一致 —— 账本重放投影与队列现状完全互相印证,退出码 0")
        _echo_approval_diffs_lines(report, print)
        return
    total = len(report.diffs) + (1 if report.ledger_error else 0)
    print(f"对账结论:检出差异 {total} 处,退出码 {report.exit_code}")
    for d in report.diffs:
        seq_text = (
            f"第 {d.first_divergent_seq} 条" if d.first_divergent_seq is not None else "无对应事件"
        )
        print(
            f"  - 条目 {d.entry_id}[{d.kind}] 期望 {d.expected_status} / "
            f"实际 {d.actual_status} / 首个分歧事件 {seq_text}"
        )
        print(f"    {d.detail}")
    _echo_approval_diffs_lines(report, print)


def _default_apply_callback(
    queue_path: str,
) -> Callable[[int, str, str], Any]:
    """构造默认补齐回调(惰性导入 decision.review_queue,依赖方向对齐 A213)。

    回调重做 approve / reject / mark_submitted 时使用**不接事件账本**的队列
    连接:事件早已在账本(这正是"事件超前"的前提),补的只是 entries 状态
    投影——重做不得重复记账。
    """
    from netsentinel.decision.review_queue import ReviewQueue

    def _apply(entry_id: int, action: str, note: str = "") -> Any:
        with ReviewQueue(queue_path) as rq:
            if action == AHEAD_ACTION_APPROVE:
                return rq.approve(entry_id, note)
            if action == AHEAD_ACTION_REJECT:
                return rq.reject(entry_id, note)
            if action == AHEAD_ACTION_SUBMIT:
                return rq.mark_submitted(entry_id)
            raise ValueError(f"未知的补齐动作:{action}")

    return _apply


def _default_heal_callback(
    log: EventLog, queue_path: str
) -> Callable[[int, str, int, str], Any]:
    """构造默认留痕补写回调(先账本后留痕;sqlite3 直写,不 import decision)。

    每行补写两步(**先账本后状态**,与 A213/A222 崩溃窗口语义同构):

    1. 账本补记 :data:`EVENT_APPROVALS_HEALED` 审计事件,``actor`` 恒为
       :data:`HEAL_ACTOR`(``"replay-heal"``)——与真人复核署名可区分
       (防伪造留痕:后续审计恒能看出该行是重放补写而非真人点击);
    2. 直写 approvals 表补留痕行(``INSERT OR IGNORE`` 主键兜底幂等:
       预检后仍撞主键说明并发已补,不视为错误);``acted_at`` 取来源
       四眼事件的 ts(确定性,零墙钟)。

    本回调**不经** :class:`~netsentinel.decision.four_eyes.FourEyesQueue`
    的 approve 路径重做:四眼域事实事件早已在账(这正是"留痕滞后"的
    前提),重做 approve 会再走一遍校验/记账;这里只补缺失的投影行,
    storage 层依旧不反向 import 决策层(依赖方向对齐 A213)。
    """
    import sqlite3  # 标准库;惰性导入,对齐本模块 CLI 的惰性装配惯例

    def _heal(entry_id: int, reviewer: str, seq: int, ts: str) -> None:
        # 先账本:补写动作的审计事件(署名 replay-heal,与真人可区分)。
        log.append(
            EVENT_APPROVALS_HEALED,
            entry_id,
            actor=HEAL_ACTOR,
            payload={"reviewer": reviewer, "source_seq": seq},
        )
        # 后状态:补写 approvals 留痕行(acted_at 取来源事件 ts,确定性)。
        con = sqlite3.connect(queue_path)
        try:
            con.execute(
                "INSERT OR IGNORE INTO approvals"
                " (entry_id, reviewer, acted_at) VALUES (?, ?, ?)",
                (entry_id, reviewer, ts),
            )
            con.commit()
        finally:
            con.close()

    return _heal


def _echo_action(act: AheadAction) -> str:
    """单条补齐动作的中文一行(条目/事件/将达状态——任务书要求的口径)。"""
    action_cn = AHEAD_ACTIONS_CN.get(act.action, act.action or "-")
    note_text = f"(携带备注:{act.note})" if act.note else ""
    return (
        f"  - 条目 {act.entry_id} · 重做{action_cn}({act.event_type or '?'},"
        f"第 {act.seq} 条事件):{act.from_status} → {act.to_status}{note_text}"
    )


def _stdin_is_tty() -> bool:
    """stdin 是否为真实终端(交互确认的可用性前置;测试可 monkeypatch)。

    读取失败(注入流无 isatty 等)按"非 TTY"处理——宁可拒绝交互也不
    静默把非终端输入当人工确认(红线:交互确认绝不静默自动执行)。
    """
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _interactive_prompt(act: AheadAction) -> str:
    """逐条确认的中文提问(对齐 HUMAN_GATE 的 input 提问惯例)。"""
    action_cn = AHEAD_ACTIONS_CN.get(act.action, act.action or "-")
    return (
        f"确认重做?条目 {act.entry_id} · {action_cn}:"
        f"{act.from_status} → {act.to_status} [y/n]:"
    )


def _echo_heal_action(act: ApprovalHealAction) -> str:
    """单条留痕补写动作的中文一行(entry/将补 reviewer/来源事件 seq——任务口径)。"""
    return (
        f"  - 条目 {act.entry_id} · 补写留痕 {act.reviewer}"
        f"(来源事件 第 {act.event_seq} 条,acted_at 取事件 ts)"
    )


def _heal_prompt(act: ApprovalHealAction) -> str:
    """留痕补写逐条确认的中文提问(独立于 entries 重做的确认链)。"""
    return (
        f"确认补写留痕?条目 {act.entry_id} · 补 {act.reviewer}"
        f"(来源事件 第 {act.event_seq} 条,署名 replay-heal)[y/n]:"
    )


def _run_apply_ahead(
    log: EventLog,
    ledger_path: str,
    queue_path: str,
    *,
    confirm: bool,
    apply_callback: Callable[[int, str, str], Any] | None,
    json_path: str | None,
    interactive: bool = False,
    ask: Callable[[str], str] | None = None,
    four_eyes_factory: Callable[[], Any] | None = None,
    heal_approvals: bool = False,
    heal_callback: Callable[[int, str, int, str], Any] | None = None,
) -> int:
    """``--apply-ahead`` 入口:对账 → 打印差异与两份清单 → (可选)逐条重做/补写 → 复跑验证。

    红线:不带 ``--yes`` / ``-i`` 绝不改任何状态;执行前必须**先**打印将执行
    的动作清单再逐条执行(先全量校验,任何校验失败绝不半途 apply)。

    确认口径(A234):``confirm=True``(--yes 批量确认)/ ``interactive=True``
    (``-i`` 终端逐条 y/n,经注入的 ``ask`` 提问,默认 :func:`input`;非 y
    一律跳过并如实标注)/ 两者皆无 = 预览。``four_eyes_factory`` 透传给
    执行前后的对账(approvals 投影差异进独立清单,不影响退出码)。

    留痕补齐(``heal_approvals=True``,独立开关):approvals 留痕的补写是
    **第二条确认链**——必须 ``--heal-approvals`` 与 ``--yes`` / 交互 y
    **同时**成立才执行;只加 ``--yes`` 不加 ``--heal-approvals`` 时
    approvals 一字不动(两清单两确认,绝不与 entries 状态补齐捆绑)。
    """
    # 惰性导入:解耦 storage 与 decision(CLI 层才装配默认工厂与回调)。
    from netsentinel.decision.review_queue import ReviewQueue

    def queue_factory() -> Any:
        return ReviewQueue(queue_path)

    ask_fn = ask if ask is not None else input
    callback = apply_callback or _default_apply_callback(queue_path)
    machine = json_path == "-"
    staged: dict[str, Any] = {
        "mode": (
            "interactive" if interactive else ("execute" if confirm else "dry-run")
        ),
        "ledger": ledger_path,
        "queue": queue_path,
        "heal_approvals": bool(heal_approvals),
    }

    def echo(text: str) -> None:
        if not machine:
            print(text)

    bar = "=" * 60
    mode_line = (
        "· 交互确认(--interactive),逐条 y/n"
        if interactive
        else ("· 已确认(--yes),将逐条重做" if confirm else "· 预览(未加 --yes)")
    )
    if heal_approvals:
        mode_line += ";留痕补齐(--heal-approvals)已开启"
    echo(bar)
    echo(f"NetSentinel 事件超前补齐(--apply-ahead){mode_line}")
    echo(bar)
    echo(f"事件账本:{ledger_path}")
    echo(f"复核队列:{queue_path}")

    # 执行前再次对账(本次调用的最新口径),打印差异。
    before = replay_review_queue(
        log, queue_factory, four_eyes_factory=four_eyes_factory
    )
    staged["before"] = before.to_dict()
    ahead_total = sum(1 for d in before.diffs if d.kind == "事件超前")
    lagging_total = sum(
        1 for d in before.approval_diffs if d.kind == APPROVAL_DIFF_LAGGING
    )
    if before.ledger_error:
        echo(f"账本错误:{before.ledger_error}")
        echo("对账结论:账本损坏,拒绝补齐(先修复账本再执行),退出码 2")
        staged["exit_code"] = EXIT_MISMATCH
        staged["errors"] = ["账本损坏,拒绝补齐"]
        if machine:
            print(json.dumps(staged, ensure_ascii=False, indent=2))
        return EXIT_MISMATCH
    echo(f"执行前对账:差异 {len(before.diffs)} 处,其中事件超前 {ahead_total} 处")
    for d in before.diffs:
        echo(
            f"  - 条目 {d.entry_id}[{d.kind}] 期望 {d.expected_status} / "
            f"实际 {d.actual_status}"
        )
    _echo_approval_diffs_lines(before, echo)

    # 全量计划(先全量 dry 列表,再谈执行):entries 重做 + approvals 补写两清单。
    actions, validation_errors = plan_apply_ahead(before, log.iter_events())
    staged["actions"] = [a.to_dict() for a in actions]
    staged["validation_errors"] = list(validation_errors)
    heal_actions: list[ApprovalHealAction] = (
        plan_heal_approvals(before, log.iter_events()) if heal_approvals else []
    )
    staged["heal_actions"] = [a.to_dict() for a in heal_actions]
    if not actions and not heal_actions:
        if lagging_total and not heal_approvals:
            echo(
                f"检出留痕滞后 {lagging_total} 处:未加 --heal-approvals,"
                "本次不动 approvals 留痕(独立确认口径,不与状态补齐捆绑)"
            )
        if heal_approvals:
            echo(
                "未检出事件超前差异与留痕滞后差异,无动作"
                "(其他差异需另行人工裁决),退出码 0"
            )
        else:
            echo("未检出事件超前差异,无动作(其他差异需另行人工裁决),退出码 0")
        staged["exit_code"] = EXIT_OK
        if machine:
            print(json.dumps(staged, ensure_ascii=False, indent=2))
        return EXIT_OK

    auto = [a for a in actions if a.auto]
    manual = [a for a in actions if not a.auto]
    if actions:
        echo(f"将执行的动作清单(共 {len(auto)} 条):")
        for act in auto:
            echo(_echo_action(act))
        if manual:
            echo(f"不可自动补齐(需人工裁决,共 {len(manual)} 条,不执行):")
            for act in manual:
                echo(f"  - 条目 {act.entry_id} · {act.reason}")
        if validation_errors:
            for err in validation_errors:
                echo(f"校验失败:{err}")
    else:
        echo("未检出事件超前差异:entries 状态无补齐动作")

    # ---- 第二清单:approvals 留痕补写(独立开关;未开启时仅提示,绝不执行)----
    if heal_approvals:
        if heal_actions:
            echo(f"将补写的留痕清单(--heal-approvals,共 {len(heal_actions)} 行):")
            for ha in heal_actions:
                echo(_echo_heal_action(ha))
            echo(
                "  每行先补记审计事件 approvals_healed"
                "(actor=replay-heal,与真人复核署名可区分),再写 approvals 留痕"
            )
        else:
            echo("未检出留痕滞后差异:approvals 留痕无补写动作")
    elif lagging_total:
        echo(
            f"检出留痕滞后 {lagging_total} 处:未加 --heal-approvals,"
            "本次不动 approvals 留痕(独立确认口径,不与状态补齐捆绑)"
        )

    executing = confirm or interactive
    if not executing:
        echo("未加 --yes:以上清单仅为预览,不会改动任何状态;确认无误后追加 --yes 执行。")
        echo("对账结论:预览完成,未做任何改动,退出码 0")
        staged["exit_code"] = EXIT_OK
        if machine:
            print(json.dumps(staged, ensure_ascii=False, indent=2))
        return EXIT_OK

    # 执行口径(--yes / -i):任何校验失败 → 整体拒绝(绝不半途 apply;
    # 账本疑似被篡改时留痕补写同门拒绝——不按可疑事实补写留痕)。
    if validation_errors:
        echo("校验失败:已整体拒绝执行任何补齐(绝不半途 apply),退出码 2")
        staged["exit_code"] = EXIT_MISMATCH
        staged["executed"] = []
        staged["errors"] = list(validation_errors)
        if machine:
            print(json.dumps(staged, ensure_ascii=False, indent=2))
        return EXIT_MISMATCH

    errors_collected: list[str] = []

    # ---- 执行段一:entries 状态重做(确认链一:--yes / -i 逐条 y/n)----
    outcome = ApplyAheadOutcome()
    entries_reported = False
    if actions and auto:
        if interactive:
            # 逐条 y/n 确认(全量清单已在上方打印):仅 y/Y 执行该条;n / 空回车 /
            # 乱码 / EOF 一律跳过并如实标注(红线:绝不静默自动执行)。
            confirmed: list[AheadAction] = []
            declined: list[tuple[AheadAction, str]] = []
            for act in auto:
                try:
                    answer = ask_fn(_interactive_prompt(act)).strip()
                except EOFError:
                    answer = ""
                if answer in ("y", "Y"):
                    confirmed.append(act)
                else:
                    declined.append((act, f"人工未确认(回答 {answer or 'EOF/空'}),跳过"))
            echo(
                f"交互确认结果:同意执行 {len(confirmed)} 条 / 人工跳过 {len(declined)} 条"
            )
            outcome = execute_apply_ahead(confirmed, queue_factory, callback)
            outcome.skipped.extend(declined)
        else:
            # 逐条重做(幂等预检 + 回调注入),已执行条目如实报告。
            outcome = execute_apply_ahead(auto, queue_factory, callback)
        entries_reported = True
    elif actions:
        echo(
            "无可自动补齐的动作(全部需人工裁决):entries 状态未做任何改动,"
            "差异仍在,退出码 2"
        )
        errors_collected.append("无可自动补齐的动作,差异仍在(需人工裁决)")

    if entries_reported:
        staged["executed"] = [a.to_dict() for a in outcome.executed]
        staged["skipped"] = [
            {"action": a.to_dict(), "note": note} for a, note in outcome.skipped
        ]
        errors_collected.extend(outcome.errors)
        echo(f"逐条执行结果:已执行 {len(outcome.executed)} 条,"
             f"跳过 {len(outcome.skipped)} 条")
        for act in outcome.executed:
            echo(f"  - 已执行:条目 {act.entry_id}:{act.from_status} → {act.to_status}")
        for act, note_text in outcome.skipped:
            echo(f"  - 跳过:条目 {act.entry_id}({note_text})")
        for err in outcome.errors:
            echo(f"  - 错误:{err}")

    # ---- 执行段二:approvals 留痕补写(确认链二:--heal-approvals × --yes/交互 y)----
    heal_outcome = HealApprovalsOutcome()
    if heal_approvals and heal_actions:
        heal_todo = heal_actions
        declined_heal: list[tuple[ApprovalHealAction, str]] = []
        if interactive:
            # 逐行 y/n 确认(补写清单已在上方打印):仅 y/Y 补写该行,其余
            # 一律跳过并如实标注(红线:绝不静默自动补写留痕)。
            confirmed_heal: list[ApprovalHealAction] = []
            for ha in heal_actions:
                try:
                    answer = ask_fn(_heal_prompt(ha)).strip()
                except EOFError:
                    answer = ""
                if answer in ("y", "Y"):
                    confirmed_heal.append(ha)
                else:
                    declined_heal.append(
                        (ha, f"人工未确认(回答 {answer or 'EOF/空'}),跳过")
                    )
            echo(
                f"留痕补写交互确认:同意 {len(confirmed_heal)} 行 / "
                f"人工跳过 {len(declined_heal)} 行"
            )
            heal_todo = confirmed_heal
        if heal_todo:
            heal_cb = heal_callback or _default_heal_callback(log, queue_path)
            heal_outcome = execute_heal_approvals(
                heal_todo, four_eyes_factory, heal_cb
            )
        heal_outcome.skipped.extend(declined_heal)  # 人工跳过的行如实进清单
        echo(
            f"留痕补写结果:已补写 {len(heal_outcome.executed)} 行,"
            f"跳过 {len(heal_outcome.skipped)} 行"
        )
        for ha in heal_outcome.executed:
            echo(
                f"  - 已补写:条目 {ha.entry_id} 补 {ha.reviewer}"
                f"(来源事件 第 {ha.event_seq} 条,署名 replay-heal)"
            )
        for ha, note_text in heal_outcome.skipped:
            echo(f"  - 跳过:条目 {ha.entry_id}({note_text})")
        for err in heal_outcome.errors:
            echo(f"  - 错误:{err}")
        staged["heal_executed"] = [a.to_dict() for a in heal_outcome.executed]
        staged["heal_skipped"] = [
            {"action": a.to_dict(), "note": note} for a, note in heal_outcome.skipped
        ]
        errors_collected.extend(heal_outcome.errors)

    # 执行后自动复跑对账,验证归零。
    after = replay_review_queue(
        log, queue_factory, four_eyes_factory=four_eyes_factory
    )
    staged["after"] = after.to_dict()
    staged["errors"] = list(errors_collected)
    if heal_approvals and heal_actions:
        if heal_outcome.ok and not after.approval_diffs:
            echo(
                "留痕补写后复跑 approvals 对账:留痕差异归零"
                "(独立清单,不影响状态对账退出码)"
            )
        else:
            echo(
                f"留痕补写后复跑 approvals 对账:留痕差异未归零"
                f"(剩余 {len(after.approval_diffs)} 处,独立清单)"
            )
    if outcome.ok and heal_outcome.ok and after.ok:
        echo("补齐后复跑对账:差异归零,账本与队列重新互相印证,退出码 0")
        staged["exit_code"] = EXIT_OK
        rc = EXIT_OK
    else:
        echo(f"补齐后复跑对账:差异未归零(剩余 {len(after.diffs)} 处),退出码 2")
        for d in after.diffs:
            echo(
                f"  - 条目 {d.entry_id}[{d.kind}] 期望 {d.expected_status} / "
                f"实际 {d.actual_status}"
            )
        staged["exit_code"] = EXIT_MISMATCH
        rc = EXIT_MISMATCH
    _echo_approval_diffs_lines(after, echo)
    if machine:
        print(json.dumps(staged, ensure_ascii=False, indent=2))
    elif json_path:
        out = pathlib.Path(json_path)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(staged, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"JSON 报告已写入:{out}")
        except OSError as exc:
            print(f"JSON 报告写入失败:{exc}", file=sys.stderr)
            return EXIT_INPUT_ERROR
    return rc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m netsentinel.storage.replay",
        description=(
            "事件账本重放对账:重放 append-only 事件流重建复核条目投影"
            "(最终状态+操作时间线),并与队列 SQLite 现状逐条对账,"
            "输出中文差异清单(事件超前/账本缺失/状态或字段不一致)"
        ),
    )
    parser.add_argument(
        "ledger_path",
        help="事件账本 SQLite 路径(EventLog 落盘格式)",
    )
    parser.add_argument(
        "queue_path",
        help="复核队列 SQLite 路径(review_queue 的 entries 表,现状投影)",
    )
    parser.add_argument(
        "--json",
        nargs="?",
        const="-",
        default=None,
        metavar="报告路径",
        help=(
            "输出机器可读 JSON 报告:不带值打印到 stdout(替代人读报告);"
            "带路径则写入该文件,stdout 仍打印人读报告"
        ),
    )
    parser.add_argument(
        "--apply-ahead",
        action="store_true",
        help=(
            "事件超前补齐(半自动裁决):对检出的'事件超前'差异给出逐条"
            "重做清单;不带 --yes 仅打印清单不改动任何状态(退出码 0),"
            "带 --yes 先复跑对账打印差异、再全量校验后逐条重做并复跑"
            "对账验证归零"
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "显式人工确认:与 --apply-ahead 搭配才有效,确认执行补齐清单"
            "(绝不静默自动改状态)"
        ),
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help=(
            "交互确认(A234):与 --apply-ahead 搭配才有效,全量清单先打印,"
            "终端逐条 y/n 确认(仅 y 执行,n/空/乱码/EOF 一律跳过并如实标注);"
            "非交互终端(非 TTY)拒绝执行,请改用 --yes"
        ),
    )
    parser.add_argument(
        "--four-eyes",
        action="store_true",
        help=(
            "四眼留痕(approvals)投影对账(A234):重放 four_eyes_* 事件与"
            " approvals 表对账,'留痕滞后'差异独立列出,不影响状态对账退出码"
        ),
    )
    parser.add_argument(
        "--heal-approvals",
        action="store_true",
        help=(
            "四眼留痕(approvals)滞后补写(独立开关,默认关):对'留痕滞后'"
            "差异给出逐行补写清单;须与 --apply-ahead 搭配,并经 --yes 或"
            "交互 y **独立确认**才执行——只加 --yes 不加本开关时 approvals"
            " 一字不动(两清单两确认,绝不与状态补齐捆绑);补写行先补记"
            " actor=replay-heal 审计事件(与真人复核署名可区分);本开关"
            "蕴含 --four-eyes 的投影对账"
        ),
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    apply_callback: Callable[[int, str, str], Any] | None = None,
    ask: Callable[[str], str] | None = None,
    heal_callback: Callable[[int, str, int, str], Any] | None = None,
) -> int:
    """CLI 入口:``<账本.db> <队列.db> [--json [报告路径]] [--apply-ahead [--yes|-i]]``。

    - 一致 → 中文报告,0;
    - 检出差异(事件超前 / 账本缺失 / 状态或字段不一致 / payload 非法)→ 2;
    - 输入错误(账本或队列文件缺失 / 路径是目录 / 账本损坏 / ``--yes`` 或
      ``-i`` 未搭配 ``--apply-ahead`` / ``--yes`` 与 ``-i`` 同给 / ``-i``
      于非 TTY 或 stdout 机器模式 / ``--four-eyes`` 队列缺 approvals
      能力 / ``--heal-approvals`` 未搭配 ``--apply-ahead``)→ 1;
    - ``--apply-ahead`` 不带 ``--yes``/``-i`` → 仅打印动作清单,0;
    - ``--apply-ahead --yes`` / ``--apply-ahead -i`` → 补齐后复跑对账归零
      0;校验失败 / 回调失败 / 差异未归零(含交互跳过留下的差异)2;
      无超前差异无动作 0;
    - ``--four-eyes`` 追加 approvals 投影对账(独立清单,不改退出码);
    - ``--heal-approvals`` 追加留痕补写清单并(**独立确认后**)逐行补写,
      补写后复跑 approvals 对账归零(独立清单,不改状态对账退出码;回调
      失败退出 2);只加 ``--yes`` 不加 ``--heal-approvals`` 时 approvals
      一字不动;
    - 参数缺失由 argparse 报错(SystemExit 2)。

    :param apply_callback: 测试注入的补齐回调(默认 ``None`` = CLI 惰性装配
        不接账本的 ReviewQueue 实现,见 :func:`_default_apply_callback`)。
    :param ask: 测试注入的交互提问函数(``-i`` 模式;默认 ``None`` = 内建
        ``input``,阻塞等待人工——对齐 HUMAN_GATE 的 input 惯例)。
    :param heal_callback: 测试注入的留痕补写回调(默认 ``None`` = CLI 惰性
        装配先账本后留痕的 sqlite3 直写实现,见 :func:`_default_heal_callback`)。
    """
    args = _build_parser().parse_args(argv)
    ledger = pathlib.Path(args.ledger_path)
    queue_p = pathlib.Path(args.queue_path)
    if args.yes and not args.apply_ahead:
        print("输入错误:--yes 仅在与 --apply-ahead 搭配时有效(人工确认补齐清单)")
        return EXIT_INPUT_ERROR
    if args.heal_approvals and not args.apply_ahead:
        print(
            "输入错误:--heal-approvals 仅在与 --apply-ahead 搭配时有效"
            "(四眼留痕补写的独立确认开关,与 --yes 分别确认)"
        )
        return EXIT_INPUT_ERROR
    if args.interactive and not args.apply_ahead:
        print(
            "输入错误:-i/--interactive 仅在与 --apply-ahead 搭配时有效"
            "(逐条交互确认补齐清单)"
        )
        return EXIT_INPUT_ERROR
    if args.yes and args.interactive:
        print(
            "输入错误:--yes 与 -i/--interactive 互斥"
            "(批量确认与逐条确认只能二选一)"
        )
        return EXIT_INPUT_ERROR
    if args.interactive and args.json == "-":
        print(
            "输入错误:-i/--interactive 与 --json(stdout)互斥"
            "(交互提示会混入机器输出);无终端环境请改用 --apply-ahead --yes"
        )
        return EXIT_INPUT_ERROR
    if args.interactive and not _stdin_is_tty():
        print(
            "输入错误:当前 stdin 不是交互终端(非 TTY),无法逐条确认;"
            "请在真实终端运行,或改用 --apply-ahead --yes"
        )
        return EXIT_INPUT_ERROR
    if not ledger.exists():
        print(f"输入错误:事件账本不存在:{ledger}")
        return EXIT_INPUT_ERROR
    if ledger.is_dir():
        print(f"输入错误:事件账本路径是目录,应为 SQLite 文件:{ledger}")
        return EXIT_INPUT_ERROR
    if not queue_p.exists():
        print(f"输入错误:复核队列不存在:{queue_p}")
        return EXIT_INPUT_ERROR
    if queue_p.is_dir():
        print(f"输入错误:复核队列路径是目录,应为 SQLite 文件:{queue_p}")
        return EXIT_INPUT_ERROR
    try:
        log = EventLog(ledger)
    except EventLogError as exc:
        print(f"输入错误:{exc}")
        return EXIT_INPUT_ERROR

    # 四眼留痕(approvals)投影对账工厂(CLI 层惰性装配,依赖方向对齐 A213;
    # 只读使用:status/close,required 取值不影响读路径)。--heal-approvals
    # 蕴含 --four-eyes:留痕补写以 approvals 投影对账为前置(检出滞后才有得补)。
    four_eyes_factory: Callable[[], Any] | None = None
    if args.four_eyes or args.heal_approvals:
        from netsentinel.decision.four_eyes import FourEyesQueue

        _queue_path_str = str(queue_p)

        def _four_eyes_factory() -> Any:
            return FourEyesQueue(_queue_path_str, False)

        four_eyes_factory = _four_eyes_factory

    try:
        if args.apply_ahead:
            # 补齐入口:先对账打印差异与两份清单;--yes / -i 才逐条重做并复跑验证;
            # 留痕补写走独立开关 --heal-approvals(第二条确认链,绝不捆绑)。
            return _run_apply_ahead(
                log,
                str(ledger),
                str(queue_p),
                confirm=bool(args.yes),
                apply_callback=apply_callback,
                json_path=args.json,
                interactive=bool(args.interactive),
                ask=ask,
                four_eyes_factory=four_eyes_factory,
                heal_approvals=bool(args.heal_approvals),
                heal_callback=heal_callback,
            )

        # 惰性导入:解耦 storage 与 decision(CLI 层才装配默认队列工厂)。
        from netsentinel.decision.review_queue import ReviewQueue

        report = replay_review_queue(
            log,
            lambda: ReviewQueue(str(queue_p)),
            four_eyes_factory=four_eyes_factory,
        )
    finally:
        log.close()

    if args.json == "-":
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return report.exit_code

    _print_human_report(report, str(ledger), str(queue_p))
    if args.json:
        out = pathlib.Path(args.json)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"JSON 报告写入失败:{exc}", file=sys.stderr)
            return EXIT_INPUT_ERROR
        print(f"JSON 报告已写入:{out}")
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    raise SystemExit(main())
