"""事件流 → 投影重建(纯函数,确定性)。

由单文件 :mod:`netsentinel.storage.replay` 拆包而来(逐字搬运,不改逻辑):
entries 投影(:func:`build_projection`)与四眼 approvals 投影
(:func:`build_approvals_projection`)的重建逻辑集中在本模块;对账逻辑见
:mod:`netsentinel.storage.replay.reconcile`,CLI 见
:mod:`netsentinel.storage.replay.cli`。
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from netsentinel.storage.event_log import (
    EVENT_ENTRY_ADDED,
    EVENT_ENTRY_ANNOTATED,
    EVENT_ENTRY_APPROVED,
    EVENT_ENTRY_MARKED_SUBMITTED,
    EVENT_ENTRY_REJECTED,
    Event,
)
from netsentinel.storage.replay._common import (
    FOUR_EYES_EVENT_TYPES,
    ProjectedApprovals,
    ProjectedEntry,
    StatusStep,
    TimelineItem,
)

__all__ = ["build_approvals_projection", "build_projection"]


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
    **对账**(:func:`netsentinel.storage.replay.reconcile.reconcile_approvals`)
    暴露事实与留痕的分歧,重放器绝不当校验器(零第二套判例)。
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
