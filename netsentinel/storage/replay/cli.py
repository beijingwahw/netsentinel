"""replay CLI:中文人读报告 / 机器 JSON 报告 / --apply-ahead / --heal-approvals。

由单文件 :mod:`netsentinel.storage.replay` 拆包而来(逐字搬运,不改逻辑):
``main`` 与参数解析、人读/机器报告打印、默认补齐与留痕补写回调、交互
确认辅助集中在本模块。纯函数核心见 :mod:`netsentinel.storage.replay.projection`
/ :mod:`netsentinel.storage.replay.reconcile` / :mod:`netsentinel.storage.replay.heal`。

decision 依赖经 ``_common.set_queue_factories`` 注入接缝装配(未注入时
默认工厂内部惰性导入 decision,行为与拆包前逐字节一致)。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections.abc import Callable
from typing import Any

from netsentinel.storage.event_log import Event, EventLog, EventLogError
from netsentinel.storage.replay._common import (
    AHEAD_ACTION_APPROVE,
    AHEAD_ACTION_REJECT,
    AHEAD_ACTION_SUBMIT,
    AHEAD_ACTIONS_CN,
    APPROVAL_DIFF_LAGGING,
    EVENT_APPROVALS_HEALED,
    EXIT_INPUT_ERROR,
    EXIT_MISMATCH,
    EXIT_OK,
    HEAL_ACTOR,
    AheadAction,
    ApprovalHealAction,
    ApplyAheadOutcome,
    HealApprovalsOutcome,
    ReplayReport,
    make_four_eyes_queue,
    make_review_queue,
)
from netsentinel.storage.replay.heal import (
    execute_apply_ahead,
    execute_heal_approvals,
    plan_apply_ahead,
    plan_heal_approvals,
)
from netsentinel.storage.replay.reconcile import replay_review_queue

__all__ = ["main"]


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
    """构造默认补齐回调(经注入接缝构造队列,依赖方向对齐 A213)。

    回调重做 approve / reject / mark_submitted 时使用**不接事件账本**的队列
    连接:事件早已在账本(这正是"事件超前"的前提),补的只是 entries 状态
    投影——重做不得重复记账。

    队列构造经 :func:`_common.make_review_queue`(可注入工厂;未注入时
    默认惰性导入 decision.review_queue,与拆包前行为一致)。
    """
    def _apply(entry_id: int, action: str, note: str = "") -> Any:
        with make_review_queue(queue_path) as rq:
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
    from netsentinel.storage.kernel import connect as sqlite_connect  # 全仓唯一连接入口

    def _heal(entry_id: int, reviewer: str, seq: int, ts: str) -> None:
        # 先账本:补写动作的审计事件(署名 replay-heal,与真人可区分)。
        log.append(
            EVENT_APPROVALS_HEALED,
            entry_id,
            actor=HEAL_ACTOR,
            payload={"reviewer": reviewer, "source_seq": seq},
        )
        # 后状态:补写 approvals 留痕行(acted_at 取来源事件 ts,确定性)。
        # V10.18 连接卫生 → V10.21 收敛:连接经 storage.kernel.connect
        # 工厂建立(WAL + busy_timeout 由工厂内置,与全仓 SQLite 连接
        # 策略一致;此前该直连缺 PRAGMA,与同库其他写连接并发时忙等缺失)。
        con = sqlite_connect(queue_path)
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
    # 队列工厂经注入接缝构造(未注入时默认惰性导入 decision.review_queue,
    # 与拆包前行为一致——解 storage→decision 架构环的接缝见 _common)。
    def queue_factory() -> Any:
        return make_review_queue(queue_path)

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
    # 拆包兼容:经包命名空间动态解析 _stdin_is_tty——测试历史注入点为
    # monkeypatch 包属性(``replay_module._stdin_is_tty``),直接引用本模块
    # 局部名会绕开补丁。
    from netsentinel.storage import replay as _replay_pkg  # 惰性:动态查找

    if args.interactive and not _replay_pkg._stdin_is_tty():
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

    # 四眼留痕(approvals)投影对账工厂(CLI 层装配;经注入接缝构造,
    # 未注入时默认惰性导入 decision.four_eyes,依赖方向对齐 A213;
    # 只读使用:status/close,required 取值不影响读路径)。--heal-approvals
    # 蕴含 --four-eyes:留痕补写以 approvals 投影对账为前置(检出滞后才有得补)。
    four_eyes_factory: Callable[[], Any] | None = None
    if args.four_eyes or args.heal_approvals:
        _queue_path_str = str(queue_p)

        def _four_eyes_factory() -> Any:
            return make_four_eyes_queue(_queue_path_str)

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

        # 复核队列工厂(经注入接缝构造,未注入时默认惰性导入
        # decision.review_queue,与拆包前行为一致)。
        report = replay_review_queue(
            log,
            lambda: make_review_queue(str(queue_p)),
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
