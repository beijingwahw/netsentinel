"""netsentinel.storage.replay 单元测试(A213 · 重放对账 + A222 · --apply-ahead)。

覆盖:完整生命周期重放一致(投影/时间线/状态链)、崩溃窗口"事件超前"
(append 后状态写失败,给出准确 seq)、payload 篡改检出(状态/字段/非法
JSON 三路)、账本缺失、绕过双写直改状态检出、未识别事件前向兼容、
queue_factory 生命周期、CLI(退出码 0/1/2 与 --json)、确定性。

A222 追加(--apply-ahead 半自动裁决):计划纯函数(单步/多步/条目不存在/
伪造迁移整体拒绝)、执行纯函数(竞态幂等跳过 / 回调失败中止且已执行条目
如实报告)、CLI 全矩阵(dry 仅打印不改状态、--yes 补齐后复跑对账归零、
幂等复跑 no-op、无差异 no-op、校验失败绝不半途、退出码 0/1/2)、
四眼队列(A222 透传)崩溃窗口的端到端补齐。

A234 追加:``-i/--interactive`` 交互确认(全量清单先打印、mock y/n 序列、
乱码/EOF 一律跳过并如实标注、非 TTY 中文拒绝、与 --yes/--json 互斥、
执行后复跑对账);approvals 投影对账(``four_eyes_factory`` 注入式启用:
双人齐一致 / 崩溃"留痕滞后"检出 / 外部改写"留痕不一致" / 未注入不启用 /
纯函数重放 last-write-wins / 本地常量与 decision 层防漂移 / CLI --four-eyes)。

A245 收官追加(--heal-approvals 留痕补写,独立开关 · 独立确认链):
计划纯函数(只补"留痕滞后"、前缀对齐补缺、单写人路径只补一人)、执行
纯函数(幂等跳过 / 条目缺失跳过 / 回调失败中止且已执行如实报告)、CLI
全矩阵(dry 打印两清单零改动、--yes+--heal-approvals 补写后 approvals
对账归零、审计事件署名 actor="replay-heal" 与真人可区分、无滞后 no-op、
**不捆绑**(仅 --yes 时 approvals 一字不动)、-i 逐条 y/n 独立确认、
开关未搭配 --apply-ahead 输入错误 1)。

全部离线,只写 tmp_path,零第三方依赖;时间戳 monkeypatch 固定,报告确定性。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from netsentinel import telemetry
from netsentinel.contracts import SiteReport, Verdict
from netsentinel.decision import review_queue as review_queue_module
from netsentinel.storage import event_log as event_log_module
from netsentinel.storage import replay as replay_module
from netsentinel.storage.event_log import (
    EVENT_ENTRY_ADDED,
    EVENT_ENTRY_APPROVED,
    EVENT_ENTRY_MARKED_SUBMITTED,
    Event,
    EventLog,
)
from netsentinel.storage.replay import (
    APPROVAL_DIFF_INCONSISTENT,
    APPROVAL_DIFF_LAGGING,
    EVENT_APPROVALS_HEALED,
    EXIT_INPUT_ERROR,
    EXIT_MISMATCH,
    EXIT_OK,
    FOUR_EYES_EVENT_APPROVED,
    FOUR_EYES_EVENT_AWAITING,
    HEAL_ACTOR,
    ApprovalDiff,
    ApprovalHealAction,
    ProjectedEntry,
    ReplayReport,
    build_approvals_projection,
    execute_apply_ahead,
    execute_heal_approvals,
    plan_apply_ahead,
    plan_heal_approvals,
    reconcile_approvals,
    replay_review_queue,
)

FIXED_TS = "2026-01-01T08:00:00+08:00"


def make_report(url: str = "http://site.test/page") -> SiteReport:
    return SiteReport(
        site_url=url, verdict=Verdict.SUSPECT, needs_review=True
    )


@pytest.fixture()
def fixed_clock(monkeypatch) -> None:
    """固定双写两侧时钟,报告确定性(时间戳不参与对账,但参与报告内容)。"""
    monkeypatch.setattr(event_log_module, "now_iso", lambda: FIXED_TS)
    monkeypatch.setattr(review_queue_module, "now_iso", lambda: FIXED_TS)


@pytest.fixture()
def env(tmp_path: Path, fixed_clock) -> tuple[Path, Path]:
    """返回 (队列库路径, 账本库路径);由各测试自行开连接。"""
    return tmp_path / "q.db", tmp_path / "e.db"


def open_pair(qdb: Path, edb: Path):
    log = EventLog(edb)
    queue = review_queue_module.ReviewQueue(qdb, event_log=log)
    return log, queue


def replay(qdb: Path, edb: Path):
    log = EventLog(edb)
    try:
        return replay_review_queue(
            log, lambda: review_queue_module.ReviewQueue(qdb)
        )
    finally:
        log.close()


# ---------------------------------------------------------------------------
# 完整生命周期:重放一致
# ---------------------------------------------------------------------------


class TestLifecycleConsistency:
    def test_full_lifecycle_ok(self, env):
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        e1 = q.add(make_report("http://a.test"), actor="机器初筛")
        e2 = q.add(
            make_report("http://b.test"), priority_weight=1.5, actor="orch"
        )
        q.annotate(e2, priority_weight=2.5, actor="张三")
        q.approve(e1, "已人工核实", actor="李四")
        q.mark_submitted(e1, actor="系统")
        q.reject(e2, "误报", actor="王五")
        q.close()
        log.close()

        report = replay(qdb, edb)
        assert report.ok is True
        assert report.exit_code == EXIT_OK
        assert report.diffs == []
        assert report.ledger_error == ""
        assert report.events_total == 6
        assert report.entries_projected == report.entries_actual == 2
        assert report.unknown_events == 0

        p1: ProjectedEntry = report.projection[e1]
        p2: ProjectedEntry = report.projection[e2]
        assert p1.status == "submitted"
        assert p1.site_url == "http://a.test"
        assert p1.note == "已人工核实"
        assert p2.status == "rejected"
        assert p2.priority_weight == 2.5
        # 状态链:pending → approved → submitted / pending → rejected。
        assert [(s.seq, s.status) for s in p1.status_chain] == [
            (1, "pending"),
            (4, "approved"),
            (5, "submitted"),
        ]
        assert [(s.seq, s.status) for s in p2.status_chain] == [
            (2, "pending"),
            (6, "rejected"),
        ]

    def test_timeline_is_complete_audit_trail(self, env):
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        e1 = q.add(make_report(), actor="张三")
        q.approve(e1, "ok", actor="李四")
        q.mark_submitted(e1, actor="系统")
        q.close()
        log.close()

        report = replay(qdb, edb)
        tl = report.projection[e1].timeline
        assert [t.seq for t in tl] == [1, 2, 3]
        assert [t.actor for t in tl] == ["张三", "李四", "系统"]
        assert [t.ts for t in tl] == [FIXED_TS] * 3
        assert tl[0].summary.startswith("入列待复核:")
        assert "人工确认" in tl[1].summary and "pending → approved" in tl[1].summary
        assert "提交回写" in tl[2].summary

    def test_annotate_rebuilds_final_weight(self, env):
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        e1 = q.add(make_report(), priority_weight=1.0)
        q.annotate(e1, priority_weight=3.25, actor="张三")
        q.annotate(e1, priority_weight=0.5, actor="李四")
        q.close()
        log.close()
        report = replay(qdb, edb)
        assert report.ok is True
        assert report.projection[e1].priority_weight == 0.5
        assert len(report.projection[e1].timeline) == 3  # added + 两次批注

    def test_pending_only_ledger_ok(self, env):
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        q.add(make_report())
        q.close()
        log.close()
        report = replay(qdb, edb)
        assert report.ok is True and report.events_total == 1


# ---------------------------------------------------------------------------
# 崩溃窗口:账本多一条未生效事件 → "事件超前" + 准确 seq
# ---------------------------------------------------------------------------


class TestCrashWindow:
    def _crash_after_append(self, log: EventLog, monkeypatch) -> None:
        """模拟崩溃:事件 append 成功落盘后、状态提交前进程终止。"""
        real_append = log.append

        def append_then_die(*args, **kwargs):
            seq = real_append(*args, **kwargs)
            raise RuntimeError(f"模拟崩溃:事件 seq={seq} 已落盘,状态未提交")

        monkeypatch.setattr(log, "append", append_then_die)

    def test_approve_crash_reports_event_ahead_with_exact_seq(
        self, env, monkeypatch
    ):
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        e1 = q.add(make_report(), actor="张三")
        self._crash_after_append(log, monkeypatch)
        with pytest.raises(RuntimeError, match="模拟崩溃"):
            q.approve(e1, "已核实", actor="李四")
        q.close()
        log.close()

        report = replay(qdb, edb)
        assert report.ok is False and report.exit_code == EXIT_MISMATCH
        assert len(report.diffs) == 1
        d = report.diffs[0]
        approve_seq = EventLog(edb).query(
            event_type=EVENT_ENTRY_APPROVED
        )[0].seq
        assert d.entry_id == e1
        assert d.kind == "事件超前"
        assert d.expected_status == "approved"
        assert d.actual_status == "pending"
        assert d.first_divergent_seq == approve_seq  # 准确 seq
        assert "崩溃" in d.detail

    def test_add_crash_reports_missing_entry(self, env, monkeypatch):
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        self._crash_after_append(log, monkeypatch)
        with pytest.raises(RuntimeError, match="模拟崩溃"):
            q.add(make_report("http://c.test"))
        q.close()
        log.close()

        report = replay(qdb, edb)
        assert report.ok is False
        d = report.diffs[0]
        assert d.kind == "事件超前"
        assert d.actual_status == "(条目不存在)"
        assert d.first_divergent_seq == 1  # 该条目首条(entry_added)事件

    def test_submit_crash_chain_ahead(self, env, monkeypatch):
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        e1 = q.add(make_report())
        q.approve(e1)
        self._crash_after_append(log, monkeypatch)
        with pytest.raises(RuntimeError):
            q.mark_submitted(e1)
        q.close()
        log.close()

        report = replay(qdb, edb)
        d = report.diffs[0]
        assert d.kind == "事件超前"
        assert (d.expected_status, d.actual_status) == ("submitted", "approved")
        assert d.first_divergent_seq == 3


# ---------------------------------------------------------------------------
# 篡改检出
# ---------------------------------------------------------------------------


def _tamper(edb: Path, sql: str, params: tuple = ()) -> None:
    """绕过 append-only 触发器直改事件(模拟拿到 DBA 权限的攻击者)。"""
    con = sqlite3.connect(edb)
    con.execute("DROP TRIGGER events_no_update")
    con.execute(sql, params)
    con.commit()
    con.close()


class TestTamperDetection:
    def test_to_status_tamper_detected(self, env):
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        e1 = q.add(make_report(), actor="张三")
        q.approve(e1, actor="李四")
        q.close()
        log.close()
        # 把 approve 事件的 to_status 改成 rejected:重放 rejected,实际 approved。
        _tamper(
            edb,
            "UPDATE events SET payload_json = ? WHERE event_type = ?",
            (
                '{"actor":"","from_status":"pending","note":"","to_status":"rejected"}',
                EVENT_ENTRY_APPROVED,
            ),
        )
        report = replay(qdb, edb)
        assert report.ok is False and report.exit_code == EXIT_MISMATCH
        d = report.diffs[0]
        assert d.entry_id == e1
        assert d.kind == "状态不一致"
        assert d.expected_status == "rejected"
        assert d.actual_status == "approved"
        assert d.first_divergent_seq == 2  # 指向被篡改的那条事件

    def test_field_tamper_detected(self, env):
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        q.add(make_report("http://real.test"))
        q.close()
        log.close()
        _tamper(
            edb,
            "UPDATE events SET payload_json = ? WHERE event_type = 'entry_added'",
            (
                (
                    '{"actor":"","evidence_zip":"","note":"",'
                    '"priority_weight":0.0,"site_url":"http://fake.test",'
                    '"status":"pending","verdict":"suspect"}'
                ),
            ),
        )
        report = replay(qdb, edb)
        assert report.ok is False
        d = report.diffs[0]
        assert d.kind == "字段不一致"
        assert "站点" in d.detail and "fake.test" in d.detail
        assert d.first_divergent_seq == 1

    def test_invalid_payload_json_reported_not_raised(self, env):
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        q.add(make_report())
        q.close()
        log.close()
        _tamper(edb, "UPDATE events SET payload_json = '{broken'")
        report = replay(qdb, edb)
        # 账本级错误不抛异常,落入报告(退出码语义 2)。
        assert report.ok is False and report.exit_code == EXIT_MISMATCH
        assert report.ledger_error and "篡改" in report.ledger_error
        assert report.diffs == []

    def test_ledger_missing_entry_reported(self, env):
        # 账本启用前的历史数据:队列有条目,账本空。
        qdb, edb = env
        q = review_queue_module.ReviewQueue(qdb)  # 不启用账本(现状)
        q.add(make_report("http://legacy.test"))
        q.close()
        report = replay(qdb, edb)
        assert report.ok is False
        d = report.diffs[0]
        assert d.kind == "账本缺失"
        assert d.expected_status == "(账本无事件)"
        assert d.actual_status == "pending"
        assert d.first_divergent_seq is None

    def test_bypass_dual_write_change_detected(self, env):
        # 绕过双写直改 entries 表(直改 SQL / 未接线队列操作)→ 对账检出。
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        e1 = q.add(make_report())
        q.close()
        log.close()
        plain = review_queue_module.ReviewQueue(qdb)  # 无账本的连接
        plain.approve(e1)
        plain.close()
        report = replay(qdb, edb)
        assert report.ok is False
        d = report.diffs[0]
        assert d.kind == "状态不一致"
        assert d.actual_status == "approved"


# ---------------------------------------------------------------------------
# 前向兼容与工厂契约
# ---------------------------------------------------------------------------


class TestFactoryAndCompat:
    def test_unknown_event_type_counted_not_failed(self, env):
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        e1 = q.add(make_report())
        q.close()
        log.append("future_domain_event", e1, payload={"x": 1})  # 未来域事件
        log.close()
        report = replay(qdb, edb)
        assert report.ok is True  # 未识别事件不判差异(前向兼容)
        assert report.unknown_events == 1
        assert report.events_total == 2

    def test_queue_factory_is_closed_after_use(self, env):
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        q.add(make_report())
        q.close()
        log.close()

        class SpyQueue:
            def __init__(self):
                from netsentinel.decision.review_queue import ReviewQueue

                self.inner = ReviewQueue(qdb)
                self.closed = False

            def list(self):
                return self.inner.list()

            def close(self):
                self.closed = True
                self.inner.close()

        spy = SpyQueue()
        log = EventLog(edb)
        try:
            report = replay_review_queue(log, lambda: spy)
        finally:
            log.close()
        assert report.ok is True
        assert spy.closed is True

    def test_empty_ledger_empty_queue_ok(self, env):
        qdb, edb = env
        log = EventLog(edb)
        log.close()
        review_queue_module.ReviewQueue(qdb).close()  # 建库即可
        report = replay(qdb, edb)
        assert report.ok is True and report.events_total == 0


# ---------------------------------------------------------------------------
# 确定性
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_scenario_same_report(self, tmp_path: Path, monkeypatch):
        def run(tag: str) -> dict:
            monkeypatch.setattr(event_log_module, "now_iso", lambda: FIXED_TS)
            monkeypatch.setattr(
                review_queue_module, "now_iso", lambda: FIXED_TS
            )
            qdb = tmp_path / f"{tag}-q.db"
            edb = tmp_path / f"{tag}-e.db"
            log, q = open_pair(qdb, edb)
            e1 = q.add(make_report("http://a.test"), actor="张三")
            q.approve(e1, "ok", actor="李四")
            q.close()
            log.close()
            return replay(qdb, edb).to_dict()

        assert run("x") == run("y")


# ---------------------------------------------------------------------------
# CLI:python -m netsentinel.storage.replay(对齐 audit_verify 惯例)
# ---------------------------------------------------------------------------


class TestCli:
    def test_ok_exit_0(self, env, capsys):
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        q.add(make_report())
        q.close()
        log.close()
        rc = replay_module.main([str(edb), str(qdb)])
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "一致" in out and "重放对账报告" in out

    def test_mismatch_exit_2_with_chinese_diff(self, env, capsys):
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        q.add(make_report())  # 有账本
        q.close()
        log.close()
        plain = review_queue_module.ReviewQueue(qdb)
        plain.approve(1)
        plain.close()
        rc = replay_module.main([str(edb), str(qdb)])
        assert rc == EXIT_MISMATCH
        out = capsys.readouterr().out
        assert "状态不一致" in out and "条目 1" in out and "首个分歧" in out

    def test_missing_ledger_exit_1(self, env, capsys):
        qdb, edb = env
        assert not edb.exists()
        assert not qdb.exists()
        rc = replay_module.main([str(edb), str(qdb)])
        # 队列也不存在 → 输入错误 1(不自动建库)。
        assert rc == EXIT_INPUT_ERROR
        assert "输入错误" in capsys.readouterr().out

    def test_missing_queue_exit_1(self, env, capsys):
        qdb, edb = env
        log = EventLog(edb)
        log.close()
        assert not qdb.exists()
        rc = replay_module.main([str(edb), str(qdb)])
        assert rc == EXIT_INPUT_ERROR
        assert "复核队列不存在" in capsys.readouterr().out

    def test_corrupt_ledger_exit_1(self, env, capsys):
        qdb, edb = env
        edb.write_bytes(b"garbage" * 16)
        review_queue_module.ReviewQueue(qdb).close()
        rc = replay_module.main([str(edb), str(qdb)])
        assert rc == EXIT_INPUT_ERROR
        assert "输入错误" in capsys.readouterr().out

    def test_json_dash_prints_machine_report(self, env, capsys):
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        q.add(make_report())
        q.close()
        log.close()
        rc = replay_module.main([str(edb), str(qdb), "--json"])
        assert rc == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True and payload["exit_code"] == 0
        assert payload["projection"][0]["status"] == "pending"

    def test_json_path_written(self, env, capsys, tmp_path):
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        q.add(make_report())
        q.close()
        log.close()
        out_path = tmp_path / "reports" / "r.json"
        rc = replay_module.main([str(edb), str(qdb), "--json", str(out_path)])
        assert rc == EXIT_OK
        assert out_path.exists()
        assert "JSON 报告已写入" in capsys.readouterr().out
        assert json.loads(out_path.read_text(encoding="utf-8"))["ok"] is True


# ---------------------------------------------------------------------------
# 遥测
# ---------------------------------------------------------------------------


class TestTelemetry:
    def test_append_counter_tracked(self, env):
        qdb, edb = env
        telemetry.reset()
        log, q = open_pair(qdb, edb)
        q.add(make_report())
        q.close()
        log.close()
        snap = telemetry.snapshot()
        assert snap["counters"].get("storage.event_log.append", 0) >= 1
        telemetry.reset()


# ---------------------------------------------------------------------------
# A222:--apply-ahead 半自动裁决(计划纯函数)
# ---------------------------------------------------------------------------


def _crash_on(log: EventLog, monkeypatch, event_type: str) -> None:
    """模拟崩溃窗口:指定类型的事件 append 成功落盘后立刻抛错(状态未提交)。"""
    real = log.append

    def crash(event_type_arg: str, entry_id: int, **kwargs) -> int:
        if event_type_arg == event_type:
            seq = real(event_type_arg, entry_id, **kwargs)
            raise RuntimeError(
                f"模拟崩溃:事件 seq={seq} 已落盘,状态未提交"
            )
        return real(event_type_arg, entry_id, **kwargs)

    monkeypatch.setattr(log, "append", crash)


class TestApplyAheadPlan:
    def test_plan_single_step_with_note(self, env, monkeypatch):
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        e1 = q.add(make_report())
        _crash_on(log, monkeypatch, EVENT_ENTRY_APPROVED)
        with pytest.raises(RuntimeError, match="模拟崩溃"):
            q.approve(e1, "已核实", actor="李四")
        q.close()
        log.close()

        report = replay(qdb, edb)
        with EventLog(edb) as l2:
            actions, errors = plan_apply_ahead(report, l2.iter_events())
        assert errors == []
        assert len(actions) == 1
        a = actions[0]
        assert a.auto is True
        assert (
            a.entry_id, a.action, a.from_status, a.to_status, a.note
        ) == (e1, "approve", "pending", "approved", "已核实")
        assert a.event_type == EVENT_ENTRY_APPROVED
        assert a.seq == 2  # 指向未生效的那条事件

    def test_plan_multi_step_chain(self, env):
        """重放链多步超前(实际 pending、账本已到 submitted)→ 逐步计划。"""
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        e1 = q.add(make_report())
        q.close()
        log.close()
        # 链上合法的两条事实(pending→approved→submitted),状态停在 pending。
        with EventLog(edb) as l2:
            l2.append(
                EVENT_ENTRY_APPROVED, e1, actor="李四",
                payload={"from_status": "pending", "to_status": "approved",
                         "note": "ok"},
            )
            l2.append(
                EVENT_ENTRY_MARKED_SUBMITTED, e1, actor="系统",
                payload={"from_status": "approved", "to_status": "submitted",
                         "note": "ok"},
            )
        report = replay(qdb, edb)
        assert report.diffs[0].kind == "事件超前"
        with EventLog(edb) as l3:
            actions, errors = plan_apply_ahead(report, l3.iter_events())
        assert errors == []
        assert [
            (a.action, a.from_status, a.to_status) for a in actions
        ] == [
            ("approve", "pending", "approved"),
            ("submit", "approved", "submitted"),
        ]
        assert all(a.auto for a in actions)

    def test_plan_missing_entry_manual_only(self, env, monkeypatch):
        """入列崩溃(条目不存在)→ 仅列出,不可自动补齐。"""
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        _crash_on(log, monkeypatch, EVENT_ENTRY_ADDED)
        with pytest.raises(RuntimeError, match="模拟崩溃"):
            q.add(make_report("http://c.test"))
        q.close()
        log.close()

        report = replay(qdb, edb)
        with EventLog(edb) as l2:
            actions, errors = plan_apply_ahead(report, l2.iter_events())
        assert errors == []
        assert len(actions) == 1
        assert actions[0].auto is False
        assert "无法自动补齐" in actions[0].reason

    def test_plan_forged_transition_refuses(self, env):
        """伪造迁移(pending 直迁 submitted)→ 校验失败,计划标注整体拒绝。"""
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        e1 = q.add(make_report())
        q.close()
        log.close()
        with EventLog(edb) as l2:
            l2.append(
                EVENT_ENTRY_MARKED_SUBMITTED, e1, actor="伪造",
                payload={"from_status": "pending", "to_status": "submitted",
                         "note": ""},
            )
        report = replay(qdb, edb)
        assert report.diffs[0].kind == "事件超前"
        with EventLog(edb) as l3:
            actions, errors = plan_apply_ahead(report, l3.iter_events())
        assert len(errors) == 1
        assert "拒绝自动补齐" in errors[0]
        assert actions[0].auto is False
        assert "校验失败" in actions[0].reason


# ---------------------------------------------------------------------------
# A222:--apply-ahead 执行纯函数(幂等 / 回调失败)
# ---------------------------------------------------------------------------


class TestApplyAheadExecute:
    def _one_ahead(self, env, monkeypatch):
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        e1 = q.add(make_report())
        _crash_on(log, monkeypatch, EVENT_ENTRY_APPROVED)
        with pytest.raises(RuntimeError, match="模拟崩溃"):
            q.approve(e1, "已核实")
        q.close()
        log.close()
        return qdb, edb, e1

    def test_race_skip_is_idempotent_and_reconciles(self, env, monkeypatch):
        """竞态仿真:对账后、逐条预检前条目已被补齐 → 跳过并标注,仍归零。"""
        qdb, edb, e1 = self._one_ahead(env, monkeypatch)
        report = replay(qdb, edb)
        with EventLog(edb) as l2:
            actions, errors = plan_apply_ahead(report, l2.iter_events())
        assert errors == [] and len(actions) == 1

        class RaceFactory:
            """status 队列建立前,另一条连接已把条目补到重放链目标(竞态)。
            补齐方式与默认回调同构:带上事件 payload 的备注,不接账本。"""

            def __init__(self) -> None:
                self.armed = False

            def __call__(self):
                rq = review_queue_module.ReviewQueue(qdb)
                if not self.armed:
                    rq.approve(e1, actions[0].note)
                    self.armed = True
                return rq

        def must_not_run(entry_id, action, note=""):
            raise AssertionError("幂等跳过后不应再调用补齐回调")

        outcome = execute_apply_ahead(actions, RaceFactory(), must_not_run)
        assert outcome.ok is True
        assert outcome.executed == []
        assert len(outcome.skipped) == 1
        assert "状态已在重放链上" in outcome.skipped[0][1]
        # 另一路径已补齐:复跑对账同样归零。
        assert replay(qdb, edb).ok is True

    def test_callback_failure_aborts_rest_and_reports_executed(
        self, env, monkeypatch
    ):
        """回调失败:中止剩余;已执行条目如实保留,未执行条目不动。"""
        qdb, edb, e1 = self._one_ahead(env, monkeypatch)
        # 再造两条同样的超前差异(共 3 条)。
        log = EventLog(edb)
        q = review_queue_module.ReviewQueue(qdb, event_log=log)
        _crash_on(log, monkeypatch, EVENT_ENTRY_APPROVED)
        e2 = q.add(make_report("http://b.test"))
        with pytest.raises(RuntimeError, match="模拟崩溃"):
            q.approve(e2)
        e3 = q.add(make_report("http://c.test"))
        with pytest.raises(RuntimeError, match="模拟崩溃"):
            q.approve(e3)
        q.close()
        log.close()

        report = replay(qdb, edb)
        with EventLog(edb) as l2:
            actions, errors = plan_apply_ahead(report, l2.iter_events())
        assert errors == []
        auto = [a for a in actions if a.auto]
        assert [a.entry_id for a in auto] == [e1, e2, e3]

        calls: list[int] = []

        def flaky(entry_id, action, note=""):
            calls.append(entry_id)
            if entry_id == e1:  # 第一条成功(真实补齐,不接账本)
                with review_queue_module.ReviewQueue(qdb) as rq:
                    return rq.approve(entry_id, note)
            raise ValueError(f"注入失败:条目 {entry_id}")

        outcome = execute_apply_ahead(
            auto, lambda: review_queue_module.ReviewQueue(qdb), flaky
        )
        assert outcome.aborted is True
        assert outcome.ok is False
        assert calls == [e1, e2]  # 第三条未被尝试(中止,不半途继续)
        assert [a.entry_id for a in outcome.executed] == [e1]  # 如实报告
        assert any("已执行 1 条" in e for e in outcome.errors)
        with review_queue_module.ReviewQueue(qdb) as rq:
            assert rq.get(e1).status == "approved"
            assert rq.get(e2).status == "pending"
            assert rq.get(e3).status == "pending"


# ---------------------------------------------------------------------------
# A222:--apply-ahead CLI 全矩阵(退出码 0/1/2)
# ---------------------------------------------------------------------------


class TestApplyAheadCli:
    def _ahead(self, env, monkeypatch, url="http://a.test"):
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        e1 = q.add(make_report(url), actor="机器初筛")
        _crash_on(log, monkeypatch, EVENT_ENTRY_APPROVED)
        with pytest.raises(RuntimeError, match="模拟崩溃"):
            q.approve(e1, "已人工核实", actor="李四")
        q.close()
        log.close()
        return qdb, edb, e1

    def test_dry_run_prints_list_and_changes_nothing(
        self, env, monkeypatch, capsys
    ):
        """无 --yes:仅打印动作清单(entry/事件/将达状态),退出码 0 且零改动。"""
        qdb, edb, e1 = self._ahead(env, monkeypatch)
        rc = replay_module.main([str(edb), str(qdb), "--apply-ahead"])
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "将执行的动作清单" in out
        assert "条目 1" in out
        assert "entry_approved" in out
        assert "pending → approved" in out
        assert "已人工核实" in out  # 事件备注随清单展示
        assert "--yes" in out and "预览" in out
        # 红线:预览绝不改状态。
        with review_queue_module.ReviewQueue(qdb) as rq:
            assert rq.get(e1).status == "pending"

    def test_yes_applies_and_reconciles_to_zero(
        self, env, monkeypatch, capsys
    ):
        """--yes:执行前打印差异与清单 → 逐条重做 → 复跑对账归零,退出码 0。"""
        qdb, edb, e1 = self._ahead(env, monkeypatch)
        rc = replay_module.main([str(edb), str(qdb), "--apply-ahead", "--yes"])
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "执行前对账" in out  # 执行前再次对账打印差异
        assert "将执行的动作清单" in out  # 执行前先打印清单
        assert "已执行:条目 1" in out
        assert "差异归零" in out
        with review_queue_module.ReviewQueue(qdb) as rq:
            entry = rq.get(e1)
            assert entry.status == "approved"
            assert entry.note == "已人工核实"  # 备注取自事件 payload
        report = replay(qdb, edb)
        assert report.ok is True and report.diffs == []

    def test_yes_twice_second_run_noop(self, env, monkeypatch, capsys):
        """幂等:补齐后再次 --yes → 未检出超前差异,无动作退出 0。"""
        qdb, edb, _ = self._ahead(env, monkeypatch)
        assert (
            replay_module.main([str(edb), str(qdb), "--apply-ahead", "--yes"])
            == EXIT_OK
        )
        capsys.readouterr()
        rc = replay_module.main([str(edb), str(qdb), "--apply-ahead", "--yes"])
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "未检出事件超前差异" in out
        assert "无动作" in out

    def test_yes_consistent_state_noop(self, env, capsys):
        """--yes 但无超前差异 → 无动作退出 0。"""
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        q.add(make_report())
        q.close()
        log.close()
        rc = replay_module.main([str(edb), str(qdb), "--apply-ahead", "--yes"])
        assert rc == EXIT_OK
        assert "未检出事件超前差异" in capsys.readouterr().out

    def test_yes_callback_failure_exit_2_reports_honestly(
        self, env, monkeypatch, capsys
    ):
        """CLI 回调失败:退出 2;已执行条目如实报告,未执行条目不动。"""
        qdb, edb, e1 = self._ahead(env, monkeypatch)
        log = EventLog(edb)
        q = review_queue_module.ReviewQueue(qdb, event_log=log)
        _crash_on(log, monkeypatch, EVENT_ENTRY_APPROVED)
        e2 = q.add(make_report("http://b.test"))
        with pytest.raises(RuntimeError, match="模拟崩溃"):
            q.approve(e2)
        e3 = q.add(make_report("http://c.test"))
        with pytest.raises(RuntimeError, match="模拟崩溃"):
            q.approve(e3)
        q.close()
        log.close()

        def flaky(entry_id, action, note=""):
            if entry_id == e1:
                with review_queue_module.ReviewQueue(qdb) as rq:
                    return rq.approve(entry_id, note)
            raise ValueError(f"注入失败:条目 {entry_id}")

        rc = replay_module.main(
            [str(edb), str(qdb), "--apply-ahead", "--yes"],
            apply_callback=flaky,
        )
        assert rc == EXIT_MISMATCH
        out = capsys.readouterr().out
        assert "已执行 1 条" in out
        assert "中止剩余补齐" in out
        assert "差异未归零" in out
        with review_queue_module.ReviewQueue(qdb) as rq:
            assert rq.get(e1).status == "approved"
            assert rq.get(e2).status == "pending"
            assert rq.get(e3).status == "pending"

    def test_yes_validation_failure_refuses_everything(self, env, capsys):
        """绝不半途:一条伪造迁移毒化全量校验 → 全部拒绝(含合法超前条目)。"""
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        good = q.add(make_report("http://good.test"))
        forged = q.add(make_report("http://forged.test"))
        q.close()
        log.close()
        # good:链上合法的超前事实;forged:pending 直迁 submitted(状态机不容)。
        with EventLog(edb) as l2:
            l2.append(
                EVENT_ENTRY_APPROVED, good, actor="李四",
                payload={"from_status": "pending", "to_status": "approved",
                         "note": ""},
            )
            l2.append(
                EVENT_ENTRY_MARKED_SUBMITTED, forged, actor="伪造",
                payload={"from_status": "pending", "to_status": "submitted",
                         "note": ""},
            )
        rc = replay_module.main([str(edb), str(qdb), "--apply-ahead", "--yes"])
        assert rc == EXIT_MISMATCH
        out = capsys.readouterr().out
        assert "校验失败" in out
        assert "整体拒绝" in out
        # 合法的超前条目也未被补齐(先全量校验,绝不半途 apply)。
        with review_queue_module.ReviewQueue(qdb) as rq:
            assert rq.get(good).status == "pending"
            assert rq.get(forged).status == "pending"

    def test_yes_without_apply_ahead_exit_1(self, env, capsys):
        """--yes 单独使用 → 输入错误 1(必须与 --apply-ahead 搭配)。"""
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        q.add(make_report())
        q.close()
        log.close()
        rc = replay_module.main([str(edb), str(qdb), "--yes"])
        assert rc == EXIT_INPUT_ERROR
        assert "--yes 仅在与 --apply-ahead" in capsys.readouterr().out

    def test_missing_ledger_exit_1(self, env, capsys):
        """输入错误沿用惯例:账本缺失 → 1(即便带 --apply-ahead --yes)。"""
        qdb, edb = env
        rc = replay_module.main([str(edb), str(qdb), "--apply-ahead", "--yes"])
        assert rc == EXIT_INPUT_ERROR
        assert "输入错误" in capsys.readouterr().out

    def test_missing_entry_manual_only_paths(self, env, monkeypatch, capsys):
        """入列崩溃(条目不存在):dry 列出不可自动补齐;--yes 无动作退出 2。"""
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        _crash_on(log, monkeypatch, EVENT_ENTRY_ADDED)
        with pytest.raises(RuntimeError, match="模拟崩溃"):
            q.add(make_report("http://c.test"))
        q.close()
        log.close()

        rc = replay_module.main([str(edb), str(qdb), "--apply-ahead"])
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "不可自动补齐" in out
        assert "无法自动补齐" in out

        rc = replay_module.main([str(edb), str(qdb), "--apply-ahead", "--yes"])
        assert rc == EXIT_MISMATCH  # 无可自动补齐:差异仍在,如实退出 2
        assert "无可自动补齐" in capsys.readouterr().out

    def test_apply_ahead_json_dash_machine_report(
        self, env, monkeypatch, capsys
    ):
        """--apply-ahead --json:stdout 输出机器可读结果(dry-run 口径)。"""
        qdb, edb, _ = self._ahead(env, monkeypatch)
        rc = replay_module.main([str(edb), str(qdb), "--apply-ahead", "--json"])
        assert rc == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "dry-run"
        assert payload["exit_code"] == EXIT_OK
        assert len(payload["actions"]) == 1
        assert payload["actions"][0]["action"] == "approve"
        assert payload["before"]["ok"] is False

    def test_yes_multi_step_chain_restores_full_state(
        self, env, monkeypatch, capsys
    ):
        """多步超前:--yes 沿重放链逐条重做(approve → submit),终态 submitted。"""
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        e1 = q.add(make_report("http://chain.test"))
        _crash_on(log, monkeypatch, EVENT_ENTRY_APPROVED)
        with pytest.raises(RuntimeError, match="模拟崩溃"):
            q.approve(e1, "链上备注")
        q.close()
        log.close()
        # approve 已在账(崩溃残留);再补一条链上合法的 submitted 事实。
        with EventLog(edb) as l2:
            l2.append(
                EVENT_ENTRY_MARKED_SUBMITTED, e1, actor="系统",
                payload={"from_status": "approved", "to_status": "submitted",
                         "note": "链上备注"},
            )
        rc = replay_module.main([str(edb), str(qdb), "--apply-ahead", "--yes"])
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "已执行:条目 1:pending → approved" in out
        assert "已执行:条目 1:approved → submitted" in out
        with review_queue_module.ReviewQueue(qdb) as rq:
            assert rq.get(e1).status == "submitted"
        assert replay(qdb, edb).ok is True


# ---------------------------------------------------------------------------
# A222:四眼队列(事件账本透传)× --apply-ahead 端到端
# ---------------------------------------------------------------------------


class TestApplyAheadFourEyes:
    def test_four_eyes_pair_crash_recovered_by_apply_ahead(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """四眼双人齐的崩溃窗口由 --apply-ahead 补齐,账实归零。"""
        pytest.importorskip("netsentinel.decision.four_eyes")
        from netsentinel.decision.four_eyes import FourEyesQueue

        qdb = tmp_path / "fq.db"
        edb = tmp_path / "fe.db"
        log = EventLog(edb)
        q = FourEyesQueue(qdb, True, event_log=log)
        entry_id = q.add(make_report("http://four.test"))
        q.approve(entry_id, "张三")
        _crash_on(log, monkeypatch, EVENT_ENTRY_APPROVED)
        with pytest.raises(RuntimeError, match="模拟崩溃"):
            q.approve(entry_id, "李四")
        q.close()
        log.close()

        rc = replay_module.main([str(edb), str(qdb), "--apply-ahead", "--yes"])
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "差异归零" in out
        # 状态投影已补齐;账本事实(含四眼域事件)原样保留。
        with FourEyesQueue(qdb, True) as q2:
            assert q2.get(entry_id).status == "approved"
        with EventLog(edb) as l2:
            types = [e.event_type for e in l2.iter_events()]
        assert types == [
            "entry_added",
            "four_eyes_awaiting_second",
            "four_eyes_approved",
            "entry_approved",  # 补齐前已在账:重做未重复记账
        ]
        assert replay(qdb, edb).ok is True


# ---------------------------------------------------------------------------
# A234:-i/--interactive 交互确认(全量清单先打印;逐条 y/n;非 TTY 拒绝)
# ---------------------------------------------------------------------------


class TestApplyAheadInteractive:
    def _two_ahead(self, env, monkeypatch):
        """造两条独立的 approve 崩溃窗口(两个条目都停在 pending)。"""
        qdb, edb = env
        log, q = open_pair(qdb, edb)
        e1 = q.add(make_report("http://i1.test"), actor="机器初筛")
        e2 = q.add(make_report("http://i2.test"), actor="机器初筛")
        _crash_on(log, monkeypatch, EVENT_ENTRY_APPROVED)
        with pytest.raises(RuntimeError, match="模拟崩溃"):
            q.approve(e1, "已核实一", actor="李四")
        with pytest.raises(RuntimeError, match="模拟崩溃"):
            q.approve(e2, "已核实二", actor="王五")
        q.close()
        log.close()
        return qdb, edb, e1, e2

    @staticmethod
    def _fake_tty(monkeypatch) -> None:
        monkeypatch.setattr(replay_module, "_stdin_is_tty", lambda: True)

    def test_all_yes_applies_and_reconciles_to_zero(
        self, env, monkeypatch, capsys
    ):
        """逐条全 y:清单先打印 → 逐条执行 → 复跑对账归零,退出码 0。"""
        qdb, edb, e1, e2 = self._two_ahead(env, monkeypatch)
        self._fake_tty(monkeypatch)
        prompts: list[str] = []
        answers = iter(["y", "y"])

        def echoing_ask(prompt: str) -> str:
            prompts.append(prompt)
            print(prompt)  # 模拟内建 input() 的提示回显(无换行)
            return next(answers)

        rc = replay_module.main(
            [str(edb), str(qdb), "--apply-ahead", "-i"],
            ask=echoing_ask,
        )
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        # 全量清单先打印,确认提问在其后(红线:绝不静默执行)。
        assert out.index("将执行的动作清单") < out.index("确认重做?")
        assert "交互确认(--interactive)" in out
        assert "交互确认结果:同意执行 2 条 / 人工跳过 0 条" in out
        assert "已执行:条目 1:pending → approved" in out
        assert "已执行:条目 2:pending → approved" in out
        assert "差异归零" in out
        # 提问内容含条目定位与状态迁移(逐条口径)。
        assert len(prompts) == 2
        assert "条目 1" in prompts[0] and "[y/n]" in prompts[0]
        with review_queue_module.ReviewQueue(qdb) as rq:
            assert rq.get(e1).note == "已核实一"  # 备注取自事件 payload
            assert rq.get(e2).status == "approved"
        assert replay(qdb, edb).ok is True

    def test_mixed_y_n_skips_declined_and_reports_honestly(
        self, env, monkeypatch, capsys
    ):
        """y/n 混合:n 的条目跳过并如实标注,差异未归零如实退出 2。"""
        qdb, edb, e1, e2 = self._two_ahead(env, monkeypatch)
        self._fake_tty(monkeypatch)
        answers = iter(["y", "n"])
        rc = replay_module.main(
            [str(edb), str(qdb), "--apply-ahead", "--interactive"],
            ask=lambda p: next(answers),
        )
        assert rc == EXIT_MISMATCH  # 跳过留下的差异仍在(与 --yes 同口径)
        out = capsys.readouterr().out
        assert "交互确认结果:同意执行 1 条 / 人工跳过 1 条" in out
        assert "已执行:条目 1:pending → approved" in out
        assert "跳过:条目 2(人工未确认(回答 n),跳过)" in out
        assert "差异未归零" in out
        with review_queue_module.ReviewQueue(qdb) as rq:
            assert rq.get(e1).status == "approved"
            assert rq.get(e2).status == "pending"  # 跳过 = 状态不动

    def test_invalid_answer_and_eof_never_execute(
        self, env, monkeypatch, capsys
    ):
        """乱码 / EOF 一律视为未确认(绝不静默执行),全部跳过。"""
        qdb, edb, e1, e2 = self._two_ahead(env, monkeypatch)
        self._fake_tty(monkeypatch)

        def weird_ask(prompt: str) -> str:
            if "条目 1" in prompt:
                return "maybe"  # 乱码:跳过
            raise EOFError  # EOF:跳过

        rc = replay_module.main(
            [str(edb), str(qdb), "--apply-ahead", "-i"], ask=weird_ask
        )
        assert rc == EXIT_MISMATCH
        out = capsys.readouterr().out
        assert "已执行 0 条" in out
        assert "跳过:条目 1(人工未确认(回答 maybe),跳过)" in out
        assert "跳过:条目 2(人工未确认(回答 EOF/空),跳过)" in out
        with review_queue_module.ReviewQueue(qdb) as rq:
            assert rq.get(e1).status == "pending"
            assert rq.get(e2).status == "pending"

    def test_non_tty_refused_with_chinese_hint(
        self, env, monkeypatch, capsys
    ):
        """非 TTY:中文拒绝并提示 --yes,退出码 1,零改动。"""
        qdb, edb, e1, _ = self._two_ahead(env, monkeypatch)
        monkeypatch.setattr(replay_module, "_stdin_is_tty", lambda: False)
        called = []

        def must_not_ask(prompt: str) -> str:
            called.append(prompt)
            raise AssertionError("非 TTY 不应进入逐条确认")

        rc = replay_module.main(
            [str(edb), str(qdb), "--apply-ahead", "-i"], ask=must_not_ask
        )
        assert rc == EXIT_INPUT_ERROR
        out = capsys.readouterr().out
        assert "非 TTY" in out and "--yes" in out and "输入错误" in out
        assert called == []
        with review_queue_module.ReviewQueue(qdb) as rq:
            assert rq.get(e1).status == "pending"  # 红线:拒绝即零改动

    def test_interactive_requires_apply_ahead(self, env, capsys):
        qdb, edb = env
        rc = replay_module.main([str(edb), str(qdb), "-i"])
        assert rc == EXIT_INPUT_ERROR
        assert "--interactive 仅在与 --apply-ahead 搭配" in capsys.readouterr().out

    def test_interactive_conflicts_with_yes(self, env, capsys):
        qdb, edb = env
        rc = replay_module.main([str(edb), str(qdb), "--apply-ahead", "-i", "--yes"])
        assert rc == EXIT_INPUT_ERROR
        assert "互斥" in capsys.readouterr().out

    def test_interactive_conflicts_with_json_stdout(self, env, capsys):
        """交互提示不得混入 stdout 机器输出:与 --json(stdout)互斥。"""
        qdb, edb = env
        rc = replay_module.main(
            [str(edb), str(qdb), "--apply-ahead", "-i", "--json"]
        )
        assert rc == EXIT_INPUT_ERROR
        out = capsys.readouterr().out
        assert "互斥" in out and "--json" in out

    def test_interactive_json_file_path_allowed(
        self, env, monkeypatch, capsys, tmp_path
    ):
        """--json <路径>(stdout 仍人读)与 -i 兼容:报告落文件、stdout 不混。"""
        qdb, edb, e1, _ = self._two_ahead(env, monkeypatch)
        self._fake_tty(monkeypatch)
        out_path = tmp_path / "r.json"

        def echoing_ask(prompt: str) -> str:
            print(prompt)  # 模拟 input() 回显
            return "y"

        rc = replay_module.main(
            [str(edb), str(qdb), "--apply-ahead", "-i", "--json", str(out_path)],
            ask=echoing_ask,
        )
        assert rc == EXIT_OK
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        assert payload["mode"] == "interactive"
        assert len(payload["executed"]) == 2  # 两处超前均确认执行
        out = capsys.readouterr().out
        assert "JSON 报告已写入" in out
        assert "确认重做?" in out  # stdout 仍为人读交互(机器报告在文件)
        with review_queue_module.ReviewQueue(qdb) as rq:
            assert rq.get(e1).status == "approved"


# ---------------------------------------------------------------------------
# A234:approvals 投影对账(注入 four_eyes_factory 才启用;独立清单)
# ---------------------------------------------------------------------------


class TestApprovalsReplay:
    @staticmethod
    def _four_eyes_env(tmp_path: Path, monkeypatch):
        """四眼队列 + 账本;返回 (qdb, edb, entry_id)。"""
        from netsentinel.decision.four_eyes import FourEyesQueue

        monkeypatch.setattr(event_log_module, "now_iso", lambda: FIXED_TS)
        monkeypatch.setattr(review_queue_module, "now_iso", lambda: FIXED_TS)
        qdb = tmp_path / "fq.db"
        edb = tmp_path / "fe.db"
        log = EventLog(edb)
        q = FourEyesQueue(qdb, True, event_log=log)
        entry_id = q.add(make_report("http://four.test"))
        return qdb, edb, entry_id, q, log

    def replay_four_eyes(self, qdb: Path, edb: Path):
        from netsentinel.decision.four_eyes import FourEyesQueue

        log = EventLog(edb)
        try:
            return replay_review_queue(
                log,
                lambda: review_queue_module.ReviewQueue(qdb),
                four_eyes_factory=lambda: FourEyesQueue(qdb, True),
            )
        finally:
            log.close()

    def test_local_constants_match_decision_layer(self):
        """防漂移:本地四眼事件常量与 decision.four_eyes 字面值一致。"""
        four_eyes_mod = pytest.importorskip("netsentinel.decision.four_eyes")
        assert FOUR_EYES_EVENT_AWAITING == four_eyes_mod.EVENT_FOUR_EYES_AWAITING
        assert FOUR_EYES_EVENT_APPROVED == four_eyes_mod.EVENT_FOUR_EYES_APPROVED
        assert set(replay_module.FOUR_EYES_EVENT_TYPES) == set(
            four_eyes_mod.FOUR_EYES_EVENT_TYPES
        )

    def test_build_approvals_projection_last_write_wins(self):
        """纯函数:四眼事件按序覆盖(last-write-wins),非四眼事件忽略。"""
        from netsentinel.storage.event_log import Event

        events = [
            Event(1, FIXED_TS, EVENT_ENTRY_ADDED, 1, "机器", {}),
            Event(2, FIXED_TS, FOUR_EYES_EVENT_AWAITING, 1, "张三",
                  {"state": "awaiting_second", "reviewer": "张三",
                   "reviewers": ["张三"]}),
            Event(3, FIXED_TS, "future_domain_event", 1, "x", {"y": 1}),
            Event(4, FIXED_TS, FOUR_EYES_EVENT_APPROVED, 1, "李四",
                  {"state": "approved", "reviewer": "李四",
                   "reviewers": ["张三", "李四"]}),
            Event(5, FIXED_TS, FOUR_EYES_EVENT_APPROVED, 2, "王五",
                  {"state": "approved", "reviewer": "王五"}),  # 无 reviewers 回退
        ]
        proj = build_approvals_projection(events)
        assert proj[1].reviewers == ("张三", "李四")
        assert proj[1].seq == 4  # 最后一条四眼事件
        assert proj[2].reviewers == ("王五",)  # 回退单键 reviewer
        assert proj[2].seq == 5

    def test_reconcile_approvals_requires_status_capability(self):
        """注入的队列缺 status 能力 → 中文 ValueError(契约显式失败)。"""
        from netsentinel.storage.replay import ProjectedApprovals

        with pytest.raises(ValueError, match="status"):
            reconcile_approvals({1: ProjectedApprovals(("张三",), 2)}, object())

    def test_pair_complete_consistent(self, tmp_path: Path, monkeypatch):
        """双人齐:approvals 投影一致(独立清单为空,不影响 ok)。"""
        qdb, edb, entry_id, q, log = self._four_eyes_env(tmp_path, monkeypatch)
        q.approve(entry_id, "张三")
        q.approve(entry_id, "李四")
        q.close()
        log.close()

        report = self.replay_four_eyes(qdb, edb)
        assert report.ok is True
        assert report.exit_code == EXIT_OK
        assert report.approvals_projected == 1
        assert report.approval_diffs == []
        assert report.diffs == []

    def test_crash_lagging_detected_without_blocking(self, tmp_path, monkeypatch):
        """双人齐崩溃窗口:账本有四人齐、approvals 缺第二人 → 留痕滞后
        (独立清单,不阻塞状态对账:ok / 退出码不受影响)。"""
        qdb, edb, entry_id, q, log = self._four_eyes_env(tmp_path, monkeypatch)
        q.approve(entry_id, "张三")  # 真实第一人:事件 + 留痕齐全
        q.close()
        # 模拟崩溃:four_eyes_approved 事件已落盘,但 approvals 留痕与底层
        # approve 都未发生(先账本后留痕的崩溃窗口,A222 语义)。
        log.append(
            FOUR_EYES_EVENT_APPROVED,
            entry_id,
            actor="李四",
            payload={
                "state": "approved",
                "reviewer": "李四",
                "reviewers": ["张三", "李四"],
            },
        )
        log.close()

        report = self.replay_four_eyes(qdb, edb)
        assert report.diffs == []  # entries 状态对账:仍一致(pending)
        assert report.ok is True and report.exit_code == EXIT_OK  # 不阻塞
        assert report.approvals_projected == 1
        assert len(report.approval_diffs) == 1
        d = report.approval_diffs[0]
        assert d.entry_id == entry_id
        assert d.kind == APPROVAL_DIFF_LAGGING
        assert d.expected_reviewers == ["张三", "李四"]
        assert d.actual_reviewers == ["张三"]
        assert d.event_seq == 3  # 指向 four_eyes_approved 那条事件
        assert "崩溃窗口" in d.detail and "不阻塞" in d.detail
        # to_dict 口径(机器可读报告)。
        assert report.to_dict()["approval_diffs"][0]["kind"] == "留痕滞后"

    def test_not_enabled_without_factory(self, tmp_path: Path, monkeypatch):
        """仅注入 ReviewQueue(默认)→ approvals 段不启用(向后兼容)。"""
        qdb, edb, entry_id, q, log = self._four_eyes_env(tmp_path, monkeypatch)
        q.approve(entry_id, "张三")
        q.close()
        log.append(
            FOUR_EYES_EVENT_APPROVED,
            entry_id,
            actor="李四",
            payload={"state": "approved", "reviewer": "李四",
                     "reviewers": ["张三", "李四"]},
        )
        log.close()

        report = replay(qdb, edb)  # 无 four_eyes_factory
        assert report.approvals_projected == 0
        assert report.approval_diffs == []
        assert report.ok is True

    def test_tampered_approvals_inconsistent(self, tmp_path, monkeypatch):
        """approvals 被外部改写(多出第三人行)→ 留痕不一致。"""
        qdb, edb, entry_id, q, log = self._four_eyes_env(tmp_path, monkeypatch)
        q.approve(entry_id, "张三")
        q.close()
        log.close()
        # 绕过四眼层直改 approvals 表(拿到 DBA 权限的攻击者)。
        con = sqlite3.connect(qdb)
        con.execute(
            "INSERT INTO approvals (entry_id, reviewer, acted_at)"
            " VALUES (?, ?, ?)",
            (entry_id, "王五", FIXED_TS),
        )
        con.commit()
        con.close()

        report = self.replay_four_eyes(qdb, edb)
        assert len(report.approval_diffs) == 1
        d = report.approval_diffs[0]
        assert d.kind == APPROVAL_DIFF_INCONSISTENT
        assert d.expected_reviewers == ["张三"]
        assert d.actual_reviewers == ["张三", "王五"]
        assert "互相不可解释" in d.detail

    def test_cli_four_eyes_flag_lists_lagging_section(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """CLI ``--four-eyes``:人读报告含独立留痕清单;退出码不受其影响。"""
        qdb, edb, entry_id, q, log = self._four_eyes_env(tmp_path, monkeypatch)
        q.approve(entry_id, "张三")
        q.close()
        log.append(
            FOUR_EYES_EVENT_APPROVED,
            entry_id,
            actor="李四",
            payload={"state": "approved", "reviewer": "李四",
                     "reviewers": ["张三", "李四"]},
        )
        log.close()

        rc = replay_module.main([str(edb), str(qdb), "--four-eyes"])
        assert rc == EXIT_OK  # 留痕滞后不阻塞状态对账
        out = capsys.readouterr().out
        assert "四眼留痕对账" in out
        assert "留痕滞后" in out
        assert "不影响上方状态对账结论" in out
        assert "张三、李四" in out

    def test_cli_four_eyes_json_includes_approval_fields(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """CLI ``--four-eyes --json``:机器报告含 approvals_projected / 差异。"""
        qdb, edb, entry_id, q, log = self._four_eyes_env(tmp_path, monkeypatch)
        q.approve(entry_id, "张三")
        q.approve(entry_id, "李四")
        q.close()
        log.close()

        rc = replay_module.main([str(edb), str(qdb), "--four-eyes", "--json"])
        assert rc == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["approvals_projected"] == 1
        assert payload["approval_diffs"] == []
        assert payload["ok"] is True

    def test_cli_without_four_eyes_flag_keeps_status_quo(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """CLI 不带 ``--four-eyes``(默认):机器报告 approvals 段为零值。"""
        qdb, edb, entry_id, q, log = self._four_eyes_env(tmp_path, monkeypatch)
        q.approve(entry_id, "张三")
        q.approve(entry_id, "李四")
        q.close()
        log.close()

        rc = replay_module.main([str(edb), str(qdb), "--json"])
        assert rc == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["approvals_projected"] == 0
        assert payload["approval_diffs"] == []
        assert "四眼留痕对账" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# A245 收官:--heal-approvals 留痕补写(独立开关 · 独立确认链 · 两清单两确认)
# ---------------------------------------------------------------------------


class TestHealApprovals:
    """双人齐崩溃恢复后 approvals 仍缺第二人(A234 报告)的半自动补写。"""

    @staticmethod
    def _lagging_env(tmp_path: Path, monkeypatch):
        """双人齐崩溃窗口:four_eyes_approved 已落账、第二人留痕未写。

        返回 (qdb, edb, entry_id):账本 [entry_added, awaiting(张三),
        approved(张三、李四)],approvals [张三],entries 仍 pending
        (状态对账一致——留痕滞后是独立清单)。
        """
        from netsentinel.decision.four_eyes import FourEyesQueue

        monkeypatch.setattr(event_log_module, "now_iso", lambda: FIXED_TS)
        monkeypatch.setattr(review_queue_module, "now_iso", lambda: FIXED_TS)
        qdb = tmp_path / "hq.db"
        edb = tmp_path / "he.db"
        log = EventLog(edb)
        q = FourEyesQueue(qdb, True, event_log=log)
        entry_id = q.add(make_report("http://heal.test"))
        q.approve(entry_id, "张三")
        # 模拟崩溃:第二人路径 _emit(四眼事件)已落盘,但留痕写入前终止。
        real_record = q._record_approval

        def crash_record(eid, reviewer):
            if reviewer == "李四":
                raise RuntimeError(f"模拟崩溃:{reviewer} 留痕未写")
            return real_record(eid, reviewer)

        monkeypatch.setattr(q, "_record_approval", crash_record)
        with pytest.raises(RuntimeError, match="模拟崩溃"):
            q.approve(entry_id, "李四")
        q.close()
        log.close()
        return qdb, edb, entry_id

    @staticmethod
    def _complete_pair_env(tmp_path: Path, monkeypatch):
        """双人齐完整路径(无任何差异):账本 / 留痕 / 状态互相印证。"""
        from netsentinel.decision.four_eyes import FourEyesQueue

        monkeypatch.setattr(event_log_module, "now_iso", lambda: FIXED_TS)
        monkeypatch.setattr(review_queue_module, "now_iso", lambda: FIXED_TS)
        qdb = tmp_path / "pq.db"
        edb = tmp_path / "pe.db"
        log = EventLog(edb)
        q = FourEyesQueue(qdb, True, event_log=log)
        entry_id = q.add(make_report("http://pair.test"))
        q.approve(entry_id, "张三")
        q.approve(entry_id, "李四")
        q.close()
        log.close()
        return qdb, edb, entry_id

    @staticmethod
    def _replay_four_eyes(qdb: Path, edb: Path):
        from netsentinel.decision.four_eyes import FourEyesQueue

        log = EventLog(edb)
        try:
            return replay_review_queue(
                log,
                lambda: review_queue_module.ReviewQueue(qdb),
                four_eyes_factory=lambda: FourEyesQueue(qdb, True),
            )
        finally:
            log.close()

    @staticmethod
    def _approvals(qdb: Path) -> list[str]:
        con = sqlite3.connect(qdb)
        try:
            rows = con.execute(
                "SELECT reviewer FROM approvals ORDER BY rowid ASC"
            ).fetchall()
        finally:
            con.close()
        return [str(r[0]) for r in rows]

    @staticmethod
    def _healed_events(edb: Path) -> list[Event]:
        with EventLog(edb) as l2:
            return [e for e in l2.iter_events() if e.event_type == EVENT_APPROVALS_HEALED]

    # ---- 计划纯函数 ----

    def test_plan_only_lagging_prefix_missing_with_source_ts(self):
        """只补"留痕滞后"(不一致绝不自动补);前缀对齐补缺;ts 取来源事件。"""
        report = ReplayReport(
            ok=True,
            events_total=0,
            entries_projected=0,
            entries_actual=0,
            unknown_events=0,
            approval_diffs=[
                ApprovalDiff(1, APPROVAL_DIFF_LAGGING, ["张三", "李四"], ["张三"], 3, ""),
                ApprovalDiff(2, APPROVAL_DIFF_INCONSISTENT, ["王五"], ["赵六"], 5, ""),
                ApprovalDiff(3, APPROVAL_DIFF_LAGGING, ["钱七"], [], 7, ""),
            ],
        )
        events = [
            Event(3, FIXED_TS, FOUR_EYES_EVENT_APPROVED, 1, "李四",
                  {"reviewers": ["张三", "李四"]}),
            Event(5, FIXED_TS, FOUR_EYES_EVENT_APPROVED, 2, "x", {}),
            Event(7, FIXED_TS, FOUR_EYES_EVENT_APPROVED, 3, "钱七",
                  {"reviewer": "钱七"}),  # 单写人路径:仅一名 reviewer
        ]
        actions = plan_heal_approvals(report, events)
        assert [(a.entry_id, a.reviewer, a.event_seq) for a in actions] == [
            (1, "李四", 3),  # 双人齐:补第二人
            (3, "钱七", 7),  # 单写人:只补一人
        ]
        assert actions[0].event_ts == FIXED_TS  # acted_at 取来源事件 ts

    def test_plan_empty_without_lagging(self):
        report = ReplayReport(
            ok=True, events_total=0, entries_projected=0, entries_actual=0,
            unknown_events=0,
        )
        assert plan_heal_approvals(report, []) == []

    # ---- 执行纯函数(注入 stub,无 DB)----

    class _StubFEQueue:
        """四眼队列 stub:status/close 鸭子契约(注入式测试,零 IO)。"""

        def __init__(self, reviewers_by_entry: dict[int, list[str]]) -> None:
            self._m = reviewers_by_entry

        def status(self, entry_id: int) -> dict:
            if entry_id not in self._m:
                raise ValueError(f"复核条目不存在:id={entry_id}")
            return {"state": "none", "reviewers": list(self._m[entry_id]),
                    "required": True}

        def close(self) -> None:
            pass

    def test_execute_requires_factory(self):
        with pytest.raises(ValueError, match="four_eyes_factory"):
            execute_heal_approvals([], None, lambda *a: None)

    def test_execute_idempotent_skip_and_missing_entry(self):
        """幂等预检:留痕已有该 reviewer → 跳过;条目不存在 → 跳过。"""
        stub = self._StubFEQueue({1: ["张三"]})

        def must_not_run(*args):
            raise AssertionError("幂等跳过后不应再调用补写回调")

        outcome = execute_heal_approvals(
            [ApprovalHealAction(1, "张三", 2, FIXED_TS)],
            lambda: stub,
            must_not_run,
        )
        assert outcome.ok is True and outcome.executed == []
        assert len(outcome.skipped) == 1
        assert "幂等跳过" in outcome.skipped[0][1]

        outcome2 = execute_heal_approvals(
            [ApprovalHealAction(9, "王五", 2, FIXED_TS)],
            lambda: self._StubFEQueue({}),
            must_not_run,
        )
        assert outcome2.executed == []
        assert "条目已不存在" in outcome2.skipped[0][1]

    def test_execute_callback_failure_aborts_and_reports_executed(self):
        """回调失败:中止剩余;已补写条目如实保留,未补写条目不动。"""
        stub = self._StubFEQueue({1: [], 2: []})
        calls: list[int] = []

        def flaky(entry_id, reviewer, seq, ts):
            calls.append(entry_id)
            if entry_id == 1:
                return None
            raise ValueError(f"注入失败:条目 {entry_id}")

        outcome = execute_heal_approvals(
            [
                ApprovalHealAction(1, "张三", 2, FIXED_TS),
                ApprovalHealAction(2, "李四", 4, FIXED_TS),
            ],
            lambda: stub,
            flaky,
        )
        assert outcome.aborted is True and outcome.ok is False
        assert calls == [1, 2]  # 第二条失败后中止,无第三条被尝试
        assert [a.entry_id for a in outcome.executed] == [1]  # 如实报告
        assert any("中止剩余补写" in e for e in outcome.errors)

    # ---- CLI 全矩阵 ----

    def test_dry_prints_heal_list_and_changes_nothing(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """dry:打印将补写的 approvals 行(entry/将补 reviewer/来源事件 seq),零改动。"""
        qdb, edb, entry_id = self._lagging_env(tmp_path, monkeypatch)
        rc = replay_module.main([str(edb), str(qdb), "--apply-ahead", "--heal-approvals"])
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "留痕补齐(--heal-approvals)已开启" in out
        assert "将补写的留痕清单(--heal-approvals,共 1 行)" in out
        assert f"条目 {entry_id}" in out and "李四" in out
        assert "第 3 条" in out  # 来源事件 seq(four_eyes_approved)
        assert "replay-heal" in out
        assert "预览" in out and "--yes" in out
        # 红线:预览绝不写留痕、绝不补记审计事件。
        assert self._approvals(qdb) == ["张三"]
        assert self._healed_events(edb) == []

    def test_yes_heals_and_approvals_reconcile_to_zero(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """--yes + --heal-approvals:逐行补写后复跑 approvals 对账归零。"""
        qdb, edb, entry_id = self._lagging_env(tmp_path, monkeypatch)
        rc = replay_module.main(
            [str(edb), str(qdb), "--apply-ahead", "--yes", "--heal-approvals"]
        )
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert f"已补写:条目 {entry_id} 补 李四" in out
        assert "留痕补写后复跑 approvals 对账:留痕差异归零" in out
        assert self._approvals(qdb) == ["张三", "李四"]
        report = self._replay_four_eyes(qdb, edb)
        assert report.ok is True and report.diffs == []
        assert report.approval_diffs == []  # 留痕对账归零
        # acted_at 取来源事件 ts(确定性,零墙钟)。
        con = sqlite3.connect(qdb)
        try:
            acted = con.execute(
                "SELECT acted_at FROM approvals WHERE reviewer = '李四'"
            ).fetchone()[0]
        finally:
            con.close()
        assert acted == FIXED_TS

    def test_heal_actor_signature_distinguishable_from_human(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """署名红线:补写动作的审计事件 actor="replay-heal",与真人复核可区分。"""
        qdb, edb, entry_id = self._lagging_env(tmp_path, monkeypatch)
        assert (
            replay_module.main(
                [str(edb), str(qdb), "--apply-ahead", "--yes", "--heal-approvals"]
            )
            == EXIT_OK
        )
        healed = self._healed_events(edb)
        assert len(healed) == 1
        ev = healed[0]
        assert ev.actor == HEAL_ACTOR == "replay-heal"  # 防伪造留痕的机器署名
        assert ev.entry_id == entry_id
        assert ev.payload["reviewer"] == "李四"
        assert ev.payload["source_seq"] == 3
        # 对照:四眼域真人事件的 actor 是审核人姓名,与补写署名不同源。
        with EventLog(edb) as l2:
            human = [
                e for e in l2.iter_events() if e.event_type == FOUR_EYES_EVENT_APPROVED
            ]
        assert human[0].actor == "李四"

    def test_no_lagging_is_noop(self, tmp_path: Path, monkeypatch, capsys):
        """无滞后差异:--yes --heal-approvals 也是 no-op(零补写零审计事件)。"""
        qdb, edb, entry_id = self._complete_pair_env(tmp_path, monkeypatch)
        rc = replay_module.main(
            [str(edb), str(qdb), "--apply-ahead", "--yes", "--heal-approvals"]
        )
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "未检出事件超前差异与留痕滞后差异" in out
        assert "无动作" in out
        assert self._approvals(qdb) == ["张三", "李四"]
        assert self._healed_events(edb) == []

    def test_yes_without_heal_flag_leaves_approvals_untouched(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """不捆绑红线:对账已报留痕滞后(--four-eyes),仅 --yes(不加
        --heal-approvals)→ approvals 一字不动,仅提示独立确认口径。"""
        qdb, edb, entry_id = self._lagging_env(tmp_path, monkeypatch)
        rc = replay_module.main(
            [str(edb), str(qdb), "--apply-ahead", "--yes", "--four-eyes"]
        )
        assert rc == EXIT_OK  # 留痕滞后不阻塞状态对账(独立清单,退出码 0)
        out = capsys.readouterr().out
        assert "留痕滞后" in out  # 对账如实报告差异
        assert "未加 --heal-approvals" in out
        assert "不动 approvals 留痕" in out
        assert self._approvals(qdb) == ["张三"]
        assert self._healed_events(edb) == []

    def test_dry_heal_flag_without_yes_changes_nothing(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """不捆绑红线(反向):--heal-approvals 不加 --yes → 仅预览,零改动。"""
        qdb, edb, entry_id = self._lagging_env(tmp_path, monkeypatch)
        rc = replay_module.main([str(edb), str(qdb), "--apply-ahead", "--heal-approvals"])
        assert rc == EXIT_OK
        assert self._approvals(qdb) == ["张三"]
        assert self._healed_events(edb) == []

    def test_yes_heals_entries_first_then_heal_completes_both(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """entries 超前 + 留痕滞后同场:仅 --yes 只补 entries(approvals 不动);
        追加 --heal-approvals 的第二次运行才补写留痕(两清单两确认)。"""
        from netsentinel.decision.four_eyes import FourEyesQueue

        monkeypatch.setattr(event_log_module, "now_iso", lambda: FIXED_TS)
        monkeypatch.setattr(review_queue_module, "now_iso", lambda: FIXED_TS)
        qdb = tmp_path / "bq.db"
        edb = tmp_path / "be.db"
        log = EventLog(edb)
        q = FourEyesQueue(qdb, True, event_log=log)
        entry_id = q.add(make_report("http://both.test"))
        q.approve(entry_id, "张三")

        def silent_record(eid, reviewer):  # 留痕静默丢失(滞后根源)
            return None

        monkeypatch.setattr(q, "_record_approval", silent_record)
        _crash_on(log, monkeypatch, EVENT_ENTRY_APPROVED)  # entries 状态也超前
        with pytest.raises(RuntimeError, match="模拟崩溃"):
            q.approve(entry_id, "李四")
        q.close()
        log.close()

        # 第一次:仅 --yes → entries 补齐,approvals 一字不动。
        rc = replay_module.main([str(edb), str(qdb), "--apply-ahead", "--yes"])
        assert rc == EXIT_OK
        assert "差异归零" in capsys.readouterr().out
        with FourEyesQueue(qdb, True) as q2:
            assert q2.get(entry_id).status == "approved"
        assert self._approvals(qdb) == ["张三"]
        assert self._healed_events(edb) == []

        # 第二次:--yes --heal-approvals → 留痕补写,approvals 对账归零。
        rc = replay_module.main(
            [str(edb), str(qdb), "--apply-ahead", "--yes", "--heal-approvals"]
        )
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert f"已补写:条目 {entry_id} 补 李四" in out
        assert self._approvals(qdb) == ["张三", "李四"]
        assert self._replay_four_eyes(qdb, edb).approval_diffs == []

    def test_single_reviewer_event_heals_exactly_one_row(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """单写人路径:事件只带一名 reviewer → 仅补一人(不凭空补第二人)。"""
        from netsentinel.decision.four_eyes import FourEyesQueue

        monkeypatch.setattr(event_log_module, "now_iso", lambda: FIXED_TS)
        monkeypatch.setattr(review_queue_module, "now_iso", lambda: FIXED_TS)
        qdb = tmp_path / "sq.db"
        edb = tmp_path / "se.db"
        log = EventLog(edb)
        q = FourEyesQueue(qdb, True, event_log=log)
        entry_id = q.add(make_report("http://single.test"))
        q.close()
        log.append(
            FOUR_EYES_EVENT_APPROVED,
            entry_id,
            actor="王五",
            payload={"state": "approved", "reviewer": "王五"},  # 无 reviewers 列表
        )
        log.close()

        rc = replay_module.main(
            [str(edb), str(qdb), "--apply-ahead", "--yes", "--heal-approvals"]
        )
        assert rc == EXIT_OK
        assert self._approvals(qdb) == ["王五"]  # 只补一人
        assert self._replay_four_eyes(qdb, edb).approval_diffs == []

    def test_interactive_heal_yes_writes_no_skips(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """交互 y 也是合法独立确认:逐行 y → 补写归零(清单先打印)。"""
        qdb, edb, entry_id = self._lagging_env(tmp_path, monkeypatch)
        monkeypatch.setattr(replay_module, "_stdin_is_tty", lambda: True)
        prompts: list[str] = []

        def echoing_ask(prompt: str) -> str:
            prompts.append(prompt)
            print(prompt)
            return "y"

        rc = replay_module.main(
            [str(edb), str(qdb), "--apply-ahead", "-i", "--heal-approvals"],
            ask=echoing_ask,
        )
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert out.index("将补写的留痕清单") < out.index("确认补写留痕?")  # 清单先打印
        assert "留痕补写交互确认:同意 1 行 / 人工跳过 0 行" in out
        assert "留痕差异归零" in out
        assert f"条目 {entry_id}" in prompts[0] and "[y/n]" in prompts[0]
        assert self._approvals(qdb) == ["张三", "李四"]

    def test_interactive_heal_declined_leaves_approvals_untouched(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """交互 n:该行跳过并如实标注,留痕不动(独立清单不改退出码)。"""
        qdb, edb, entry_id = self._lagging_env(tmp_path, monkeypatch)
        monkeypatch.setattr(replay_module, "_stdin_is_tty", lambda: True)

        def decline(prompt: str) -> str:
            return "n"

        rc = replay_module.main(
            [str(edb), str(qdb), "--apply-ahead", "-i", "--heal-approvals"],
            ask=decline,
        )
        assert rc == EXIT_OK  # entries 一致;留痕差异不阻塞(独立清单)
        out = capsys.readouterr().out
        assert "留痕补写交互确认:同意 0 行 / 人工跳过 1 行" in out
        assert f"跳过:条目 {entry_id}(人工未确认(回答 n),跳过)" in out
        assert "留痕差异未归零" in out
        assert self._approvals(qdb) == ["张三"]
        assert self._healed_events(edb) == []

    def test_heal_flag_requires_apply_ahead_exit_1(self, tmp_path: Path, capsys):
        """--heal-approvals 未搭配 --apply-ahead → 输入错误 1(对齐 --yes 惯例)。"""
        rc = replay_module.main(
            [str(tmp_path / "e.db"), str(tmp_path / "q.db"), "--heal-approvals"]
        )
        assert rc == EXIT_INPUT_ERROR
        assert "--heal-approvals 仅在与 --apply-ahead 搭配" in capsys.readouterr().out

    def test_heal_json_dash_machine_report(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """--heal-approvals --json:机器报告含补写清单(dry-run 口径)。"""
        qdb, edb, entry_id = self._lagging_env(tmp_path, monkeypatch)
        rc = replay_module.main(
            [str(edb), str(qdb), "--apply-ahead", "--heal-approvals", "--json"]
        )
        assert rc == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "dry-run"
        assert payload["heal_approvals"] is True
        assert payload["before"]["approval_diffs"][0]["kind"] == "留痕滞后"
        assert len(payload["heal_actions"]) == 1
        assert payload["heal_actions"][0]["reviewer"] == "李四"
        assert payload["heal_actions"][0]["event_seq"] == 3
