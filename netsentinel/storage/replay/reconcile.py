"""投影 × 队列现状 → 对账(纯函数,确定性)。

由单文件 :mod:`netsentinel.storage.replay` 拆包而来(逐字搬运,不改逻辑):
entries 状态/字段对账(``_diff_status`` / ``_diff_field_mismatch`` /
``_reconcile``)、四眼 approvals 对账(:func:`reconcile_approvals`)与
主入口 :func:`replay_review_queue` 集中在本模块;投影重建见
:mod:`netsentinel.storage.replay.projection`,补齐执行见
:mod:`netsentinel.storage.replay.heal`。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from netsentinel.storage.event_log import (
    EVENT_ENTRY_ADDED,
    EVENT_ENTRY_ANNOTATED,
    EVENT_ENTRY_APPROVED,
    EVENT_ENTRY_REJECTED,
    Event,
    EventLog,
    EventLogError,
)
from netsentinel.storage.replay._common import (
    _FIELD_CN,
    _REACHABLE,
    _RECONCILE_FIELDS,
    APPROVAL_DIFF_INCONSISTENT,
    APPROVAL_DIFF_LAGGING,
    ApprovalDiff,
    EntryDiff,
    ProjectedApprovals,
    ProjectedEntry,
    ReplayReport,
)
from netsentinel.storage.replay.projection import (
    build_approvals_projection,
    build_projection,
)

__all__ = ["reconcile_approvals", "replay_review_queue"]


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
                        "或条目被外部删除。"
                        "处置指引:该条目需人工重建或重扫——entries 主键为 "
                        "AUTOINCREMENT,自动重做入列无法保证取回同一 "
                        "entry_id(自动补齐反而制造新差异),故重放不自动重建;"
                        "请人工核对账本 entry_added 事件的 payload"
                        "(站点/判定/证据包/备注),确认需保留后重扫该站点入列,"
                        "或由人工裁决放弃该编号(账本事件原样留痕)"
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
# 主入口(纯函数)
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
