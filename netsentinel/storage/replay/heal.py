"""事件超前补齐 / 留痕滞后补写(纯函数计划 + 纯函数执行)。

由单文件 :mod:`netsentinel.storage.replay` 拆包而来(逐字搬运,不改逻辑):
``--apply-ahead`` 的计划与执行(:func:`plan_apply_ahead` /
:func:`execute_apply_ahead`)与 ``--heal-approvals`` 的计划与执行
(:func:`plan_heal_approvals` / :func:`execute_heal_approvals`)集中在本
模块;CLI 装配见 :mod:`netsentinel.storage.replay.cli`。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from netsentinel.storage.event_log import Event
from netsentinel.storage.replay._common import (
    AHEAD_EVENT_ACTIONS,
    APPROVAL_DIFF_LAGGING,
    AheadAction,
    ApprovalHealAction,
    ApplyAheadOutcome,
    HealApprovalsOutcome,
    ReplayReport,
)

__all__ = [
    "execute_apply_ahead",
    "execute_heal_approvals",
    "plan_apply_ahead",
    "plan_heal_approvals",
]


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

    :param report: 对账报告(通常来自
        :func:`netsentinel.storage.replay.replay_review_queue`)。
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
                    "无法自动补齐(AUTOINCREMENT 主键无法保证取回同一 id)。"
                    "处置指引:该条目需人工重建或重扫——核对账本事件的 "
                    "payload(站点/判定/证据包/备注)后重扫该站点入列,"
                    "或人工裁决放弃该编号(需人工裁决)",
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
        本函数关闭)——与 :func:`netsentinel.storage.replay.replay_review_queue`
        同一注入契约。
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
        的 :func:`netsentinel.storage.replay.replay_review_queue`)。
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
       行,见 ``cli._default_heal_callback``);回调抛错 → 中止剩余行,
       **已补写条目如实保留在 ``executed`` 中**。

    :param four_eyes_factory: 零参可调用,返回带 ``status(entry_id)`` /
        ``close()`` 的四眼队列对象(与
        :func:`netsentinel.storage.replay.replay_review_queue` 同一注入
        契约;``None`` 视为契约违规,中文报错)。
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
