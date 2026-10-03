"""netsentinel.cli.review_tui 单元测试(A57 · 交互式终端复核 TUI)。

覆盖契约 §3 A57 与任务书要求的全部场景:
  * list 渲染:中文表头 + 🔴/🟡/🟢 ANSI 彩色徽章 + 聚合分 + 状态筛选;
  * show 详情:证据包提示"证据包路径,请人工核验内容"、四眼状态行;
  * approve 全流程:摘要 → 证据包提示 → "我已人工核实(Y/N)":
      - Y → queue.approve 被调用((fake queue 记录)→ "已批准";
      - N / 非法输入 / EOF → "已取消",绝不批准(红线);
      - 无 --reviewer / 空白姓名 → 中文提示,不进入确认问题;
  * reject:空理由拒绝并提示重试,有(带空格)理由成功;
  * help / 未知命令 / EOF 退出码 0;
  * 四眼:第一人后 awaiting_second、同人重复 ValueError 被捕获转中文
    错误且主循环不崩,第二人通过;
  * 真实 FourEyesQueue 集成(importorskip 保护):list→approve→同人拒绝
    →第二人通过;
  * main() argparse 包装(--db / --four-eyes)与 ``python -m`` 模块入口
    (subprocess + stdin=DEVNULL,零交互真实终端)。

全部离线,stdin/stdout 用 io.StringIO 注入,零外呼、零真实门户、零真实
VLM 调用。
"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# TUI 与真实队列的导入均受 importorskip 保护(缺失时跳过而非报错)。
pytest.importorskip("netsentinel.cli.review_tui")
import netsentinel.cli.review_tui as review_tui_mod  # noqa: E402
from netsentinel import telemetry  # noqa: E402
from netsentinel.cli.review_tui import (  # noqa: E402
    ANSI_GREEN,
    ANSI_RED,
    ANSI_RESET,
    ANSI_YELLOW,
    ReviewTUI,
    main,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 测试替身:可注入的 fake 队列(记录 approve/reject 调用)
# ---------------------------------------------------------------------------


def fake_entry(
    entry_id: int = 1,
    *,
    verdict: str = "nsfw",
    status: str = "pending",
    site: str = "http://tui.test/a",
    evidence: str = "data/evidence/1.zip",
    agg: float | None = 0.87,
) -> SimpleNamespace:
    """构造最小条目(鸭子类型;agg 走 agg_nsw_prob 字段)。"""
    return SimpleNamespace(
        id=entry_id,
        site_url=site,
        verdict=verdict,
        status=status,
        evidence_zip=evidence,
        created_at="2026-10-01T09:00:00+08:00",
        updated_at="2026-10-01T09:30:00+08:00",
        note="",
        agg_nsw_prob=agg,
    )


class FakeQueue:
    """结构兼容 FourEyesQueue 的记录型 fake(含可选四眼语义)。"""

    def __init__(self, entries=(), *, four_eyes: bool = False) -> None:
        self.entries: list[SimpleNamespace] = list(entries)
        self.four_eyes = four_eyes
        self.approvals: list[tuple[int, str]] = []
        self.rejects: list[tuple[int, str]] = []
        self.list_filters: list[str | None] = []

    # -- 读 ---------------------------------------------------------------

    def list(self, status: str | None = None) -> list[SimpleNamespace]:
        self.list_filters.append(status)
        if status is None:
            return list(self.entries)
        return [e for e in self.entries if e.status == status]

    def get(self, id: int) -> SimpleNamespace | None:
        return next((e for e in self.entries if e.id == id), None)

    def summary(self) -> dict[str, int]:
        counts = {s: 0 for s in ("pending", "approved", "rejected", "submitted")}
        for e in self.entries:
            counts[e.status] = counts.get(e.status, 0) + 1
        return counts

    def status(self, id: int) -> dict:
        entry = self.get(id)
        if entry is None:
            raise ValueError(f"复核条目不存在:id={id}")
        reviewers = [r for (i, r) in self.approvals if i == id]
        if entry.status in ("approved", "submitted"):
            state = "approved"
        elif entry.status == "pending" and reviewers:
            state = "awaiting_second"
        else:
            state = "none"
        return {"state": state, "reviewers": reviewers, "required": self.four_eyes}

    # -- 写 ---------------------------------------------------------------

    def approve(self, id: int, reviewer: str) -> dict:
        entry = self.get(id)
        if entry is None:
            raise ValueError(f"复核条目不存在:id={id}")
        if entry.status != "pending":
            raise ValueError(
                f"复核条目 {id} 当前状态为 {entry.status},"
                "只有 待复核(pending) 状态的条目才能人工确认"
            )
        name = reviewer.strip()
        existing = [r for (i, r) in self.approvals if i == id]
        if self.four_eyes:
            if name in existing:
                raise ValueError(f"同一审核人不能二次确认:{name}")
            self.approvals.append((id, name))
            if not existing:
                return {"state": "awaiting_second", "first": name, "note": "等待第二审核人确认"}
            entry.status = "approved"
            return {"state": "approved", "reviewers": existing + [name]}
        self.approvals.append((id, name))
        entry.status = "approved"
        return {"state": "approved", "reviewers": [name]}

    def reject(self, id: int, note: str = "") -> SimpleNamespace:
        entry = self.get(id)
        if entry is None:
            raise ValueError(f"复核条目不存在:id={id}")
        if entry.status != "pending":
            raise ValueError(
                f"复核条目 {id} 当前状态为 {entry.status},"
                "只有 待复核(pending) 状态的条目才能人工驳回"
            )
        entry.status = "rejected"
        entry.note = note
        self.rejects.append((id, note))
        return entry


def run_tui(
    script: str,
    queue: FakeQueue,
    *,
    required: bool = False,
    db_path: str = "data/fake-review.db",
) -> tuple[int, str]:
    """以注入 IO 跑一段命令脚本,返回 (退出码, 全部输出)。"""
    out = io.StringIO()
    tui = ReviewTUI(
        db_path,
        required,
        stdin=io.StringIO(script),
        stdout=out,
        queue=queue,
    )
    code = tui.run()
    return code, out.getvalue()


# ---------------------------------------------------------------------------
# list 渲染
# ---------------------------------------------------------------------------


class TestList:
    def test_renders_chinese_headers_and_colored_badges(self) -> None:
        queue = FakeQueue(
            [
                fake_entry(1, verdict="nsfw"),
                fake_entry(2, verdict="suspect", site="http://tui.test/b", agg=0.42),
                # clean 一般不入列,防御性渲染也要能画出来。
                fake_entry(3, verdict="clean", site="http://tui.test/c", agg=0.01),
            ]
        )
        code, out = run_tui("list\nquit\n", queue)
        assert code == 0
        # 中文表头齐全。
        for header in ("编号", "判定", "站点", "聚合分", "更新时间", "状态"):
            assert header in out
        # 🔴 nsfw 红色徽章(ANSI 颜色 + emoji + 判定文本)。
        assert ANSI_RED in out and "🔴" in out and "高置信色情(nsfw)" in out
        assert ANSI_RESET in out
        # 🟡 suspect 黄色徽章。
        assert ANSI_YELLOW in out and "🟡" in out and "疑似(suspect)" in out
        # 🟢 clean 绿色徽章(防御性)。
        assert ANSI_GREEN in out and "🟢" in out and "无风险(clean)" in out
        # 聚合分与站点列。
        assert "0.87" in out and "0.42" in out
        assert "http://tui.test/a" in out
        # 状态中文 + 统计行。
        assert "待复核(pending)" in out
        assert "统计(共 3 条)" in out

    def test_unknown_verdict_rended_defensively(self) -> None:
        queue = FakeQueue([fake_entry(9, verdict="weird")])
        code, out = run_tui("list\nquit\n", queue)
        assert code == 0
        assert "未知" in out and "weird" in out

    def test_status_filter_forwarded_and_applied(self) -> None:
        queue = FakeQueue(
            [
                fake_entry(1, verdict="nsfw"),
                fake_entry(2, verdict="suspect", status="approved", site="http://tui.test/b"),
            ]
        )
        code, out = run_tui("list --status pending\nquit\n", queue)
        assert code == 0
        assert queue.list_filters == ["pending"]
        assert "http://tui.test/a" in out
        assert "http://tui.test/b" not in out

    def test_empty_queue_message(self) -> None:
        code, out = run_tui("list\nquit\n", FakeQueue())
        assert code == 0
        assert "复核队列为空" in out

    def test_unknown_status_rejected(self) -> None:
        queue = FakeQueue()
        code, out = run_tui("list --status bogus\nquit\n", queue)
        assert code == 0
        assert "未知状态:bogus" in out
        assert queue.list_filters == []  # 非法状态在查询前被拦截

    def test_unknown_argument_rejected(self) -> None:
        code, out = run_tui("list --foo\nquit\n", FakeQueue())
        assert code == 0
        assert "不认识参数" in out and "--foo" in out


# ---------------------------------------------------------------------------
# show 详情
# ---------------------------------------------------------------------------


class TestShow:
    def test_detail_includes_evidence_hint_and_fields(self) -> None:
        queue = FakeQueue([fake_entry(1)])
        code, out = run_tui("show 1\nquit\n", queue)
        assert code == 0
        assert "证据包路径,请人工核验内容" in out
        assert "data/evidence/1.zip" in out
        assert "http://tui.test/a" in out
        assert "🔴" in out and "创建时间" in out and "备注" in out
        assert "四眼状态" in out  # fake 提供 status 能力

    def test_show_missing_entry(self) -> None:
        code, out = run_tui("show 404\nquit\n", FakeQueue())
        assert code == 0
        assert "错误:复核条目不存在:id=404" in out

    def test_show_non_integer_id_usage(self) -> None:
        code, out = run_tui("show abc\nquit\n", FakeQueue())
        assert code == 0
        assert "用法:show <编号>" in out


# ---------------------------------------------------------------------------
# approve:人工确认红线(Y 批准 / N 与非法输入绝不批准)
# ---------------------------------------------------------------------------


class TestApprove:
    def test_yes_flow_records_approval_and_prints_state(self) -> None:
        entry = fake_entry(1)
        queue = FakeQueue([entry])
        code, out = run_tui("approve 1 --reviewer 张三\nY\nquit\n", queue)
        assert code == 0
        # 流程文案:摘要 → 证据包提示 → 人工确认问题。
        assert "条目 1 摘要" in out
        assert "批准前请打开证据包人工核实" in out
        assert "我已人工核实(Y/N)" in out
        # 结果:已批准 + fake queue 记录了调用,条目状态翻转。
        assert "条目 1 已批准" in out and "张三" in out
        assert queue.approvals == [(1, "张三")]
        assert entry.status == "approved"

    def test_no_answer_never_approves(self) -> None:
        entry = fake_entry(1)
        queue = FakeQueue([entry])
        code, out = run_tui("approve 1 --reviewer 张三\nN\nquit\n", queue)
        assert code == 0
        assert "已取消" in out
        assert "已批准" not in out
        assert queue.approvals == []
        assert entry.status == "pending"

    def test_illegal_answer_never_approves(self) -> None:
        entry = fake_entry(1)
        queue = FakeQueue([entry])
        code, out = run_tui("approve 1 --reviewer 张三\n也许\nquit\n", queue)
        assert code == 0
        assert "已取消" in out and "已批准" not in out
        assert queue.approvals == []
        assert entry.status == "pending"

    def test_eof_at_confirmation_cancels_safely(self) -> None:
        entry = fake_entry(1)
        queue = FakeQueue([entry])
        # 确认问题时即 EOF:必须取消,且主循环随后正常退出。
        code, out = run_tui("approve 1 --reviewer 张三\n", queue)
        assert code == 0
        assert "已取消" in out and "已批准" not in out
        assert queue.approvals == [] and entry.status == "pending"

    def test_missing_reviewer_option_prints_hint(self) -> None:
        entry = fake_entry(1)
        queue = FakeQueue([entry])
        code, out = run_tui("approve 1\nquit\n", queue)
        assert code == 0
        assert "--reviewer" in out and "审核人" in out
        # 没有审核人就不该走到人工确认问题。
        assert "我已人工核实" not in out
        assert queue.approvals == [] and entry.status == "pending"

    def test_blank_reviewer_name_rejected(self) -> None:
        entry = fake_entry(1)
        queue = FakeQueue([entry])
        code, out = run_tui('approve 1 --reviewer "   "\nquit\n', queue)
        assert code == 0
        assert "--reviewer" in out
        assert queue.approvals == [] and entry.status == "pending"

    def test_reviewer_name_with_spaces_supported(self) -> None:
        entry = fake_entry(1)
        queue = FakeQueue([entry])
        code, out = run_tui('approve 1 --reviewer "张 三"\nY\nquit\n', queue)
        assert code == 0
        assert queue.approvals == [(1, "张 三")]
        assert entry.status == "approved"

    def test_missing_entry_fails_before_confirmation(self) -> None:
        queue = FakeQueue([fake_entry(1)])
        code, out = run_tui("approve 404 --reviewer 张三\nY\nquit\n", queue)
        assert code == 0
        assert "错误:复核条目不存在:id=404" in out
        assert "我已人工核实" not in out
        assert queue.approvals == []

    def test_non_pending_entry_error_does_not_crash_loop(self) -> None:
        queue = FakeQueue([fake_entry(1, status="rejected")])
        code, out = run_tui("approve 1 --reviewer 张三\nY\nlist\nquit\n", queue)
        assert code == 0
        assert "错误:" in out and "pending" in out
        assert queue.approvals == []
        # 主循环未崩:list 仍执行。
        assert "编号" in out

    def test_unknown_option_rejected(self) -> None:
        queue = FakeQueue([fake_entry(1)])
        code, out = run_tui("approve 1 --who 张三\nquit\n", queue)
        assert code == 0
        assert "不认识参数" in out
        assert queue.approvals == []


# ---------------------------------------------------------------------------
# reject:理由必填
# ---------------------------------------------------------------------------


class TestReject:
    def test_missing_note_rejected_with_retry_hint(self) -> None:
        entry = fake_entry(1)
        queue = FakeQueue([entry])
        code, out = run_tui("reject 1\nquit\n", queue)
        assert code == 0
        assert "理由" in out and "重试" in out
        assert queue.rejects == [] and entry.status == "pending"

    def test_note_flag_without_value_rejected(self) -> None:
        entry = fake_entry(1)
        queue = FakeQueue([entry])
        code, out = run_tui("reject 1 --note\nquit\n", queue)
        assert code == 0
        assert "--note" in out and "理由" in out
        assert queue.rejects == [] and entry.status == "pending"

    def test_blank_note_rejected(self) -> None:
        entry = fake_entry(1)
        queue = FakeQueue([entry])
        code, out = run_tui('reject 1 --note ""\nquit\n', queue)
        assert code == 0
        assert "理由" in out and "重试" in out
        assert queue.rejects == [] and entry.status == "pending"

    def test_note_with_spaces_succeeds(self) -> None:
        entry = fake_entry(1)
        queue = FakeQueue([entry])
        code, out = run_tui('reject 1 --note "证据不足 图片模糊"\nquit\n', queue)
        assert code == 0
        assert "条目 1 已驳回" in out
        assert "驳回理由:证据不足 图片模糊" in out
        assert queue.rejects == [(1, "证据不足 图片模糊")]
        assert entry.status == "rejected"

    def test_missing_entry_error_in_chinese(self) -> None:
        code, out = run_tui("reject 404 --note 理由\nquit\n", FakeQueue())
        assert code == 0
        assert "错误:复核条目不存在:id=404" in out


# ---------------------------------------------------------------------------
# help / 未知命令 / 退出码
# ---------------------------------------------------------------------------


class TestHelpAndMisc:
    def test_help_lists_all_commands(self) -> None:
        code, out = run_tui("help\nquit\n", FakeQueue())
        assert code == 0
        for keyword in ("list", "show", "approve", "reject", "quit", "--reviewer", "--note"):
            assert keyword in out
        assert "绝不自动批准" in out

    def test_unknown_command_message(self) -> None:
        code, out = run_tui("bogus\nquit\n", FakeQueue())
        assert code == 0
        assert "未知命令:bogus" in out

    def test_unbalanced_quotes_do_not_crash(self) -> None:
        code, out = run_tui('reject 1 --note "未闭合\nquit\n', FakeQueue())
        assert code == 0
        assert "命令解析失败" in out

    def test_eof_exits_zero(self) -> None:
        code, out = run_tui("list\n", FakeQueue())  # 无 quit,读尽即 EOF
        assert code == 0
        assert "EOF" in out

    def test_quit_exits_zero(self) -> None:
        code, out = run_tui("quit\n", FakeQueue())
        assert code == 0
        assert "再见" in out

    def test_banner_shows_four_eyes_mode(self) -> None:
        code, out = run_tui("quit\n", FakeQueue(), required=True)
        assert code == 0
        assert "四眼原则:已启用" in out
        code, out = run_tui("quit\n", FakeQueue(), required=False)
        assert code == 0
        assert "四眼原则:未启用" in out


# ---------------------------------------------------------------------------
# 四眼原则(fake 语义):awaiting_second → 同人拒绝 → 第二人通过
# ---------------------------------------------------------------------------


class TestFourEyesFake:
    def test_full_flow_and_same_reviewer_error_caught(self) -> None:
        entry = fake_entry(1)
        queue = FakeQueue([entry], four_eyes=True)
        script = (
            "approve 1 --reviewer 张三\nY\n"        # 第一人 → awaiting_second
            "approve 1 --reviewer 张三\nY\n"        # 同人重复 → ValueError 转中文
            "approve 1 --reviewer 李四\nY\n"        # 第二人 → approved
            "show 1\n"
            "quit\n"
        )
        code, out = run_tui(script, queue, required=True)
        assert code == 0
        # 第一人:等待第二审核人,单人绝不放行。
        assert "等待第二审核人(第一人:张三)" in out
        # 同人重复:ValueError 被捕获为中文错误,主循环不崩。
        assert "错误:同一审核人不能二次确认:张三" in out
        # 第二人:两人齐 → 已批准。
        assert "条目 1 已批准" in out
        assert queue.approvals == [(1, "张三"), (1, "李四")]
        assert entry.status == "approved"
        # show 里的四眼状态行显示双人完成。
        assert "双人确认完成" in out

    def test_first_person_alone_keeps_pending(self) -> None:
        entry = fake_entry(1)
        queue = FakeQueue([entry], four_eyes=True)
        code, out = run_tui("approve 1 --reviewer 张三\nY\nquit\n", queue, required=True)
        assert code == 0
        assert entry.status == "pending"  # 红线:单人到不了 approved
        assert queue.approvals == [(1, "张三")]
        assert "等待第二审核人" in out


# ---------------------------------------------------------------------------
# 真实 FourEyesQueue 集成(importorskip 保护)
# ---------------------------------------------------------------------------


class TestFourEyesRealIntegration:
    def test_scripted_full_flow_over_real_queue(self, tmp_path: Path) -> None:
        four_eyes_mod = pytest.importorskip("netsentinel.decision.four_eyes")
        contracts = pytest.importorskip("netsentinel.contracts")
        FourEyesQueue = four_eyes_mod.FourEyesQueue
        SiteReport, Verdict = contracts.SiteReport, contracts.Verdict

        db = tmp_path / "queues" / "tui.db"
        with FourEyesQueue(db, True) as seed:
            entry_id = seed.add(
                SiteReport(site_url="http://real.test/x", verdict=Verdict.NSFW),
                evidence_zip=str(tmp_path / "evidence.zip"),
            )
            other_id = seed.add(
                SiteReport(site_url="http://real.test/y", verdict=Verdict.SUSPECT)
            )

        out = io.StringIO()
        with FourEyesQueue(db, True) as queue:
            tui = ReviewTUI(
                str(db),
                True,
                stdin=io.StringIO(
                    "list\n"
                    f"approve {entry_id} --reviewer 张三\nY\n"   # awaiting_second
                    f"approve {entry_id} --reviewer 张三\nY\n"   # 同人 → 中文错误
                    f"approve {entry_id} --reviewer 李四\nY\n"   # 双人 → approved
                    f"reject {other_id} --note 证据不足\n"
                    "quit\n"
                ),
                stdout=out,
                queue=queue,
            )
            code = tui.run()
            assert code == 0
            assert queue.get(entry_id).status == "approved"
            assert queue.get(other_id).status == "rejected"
            assert queue.status(entry_id) == {
                "state": "approved",
                "reviewers": ["张三", "李四"],
                "required": True,
            }

        text = out.getvalue()
        assert "等待第二审核人(第一人:张三)" in text
        assert "错误:同一审核人不能二次确认:张三" in text
        assert "条目 " in text and "已批准" in text
        assert "条目 " in text and "已驳回" in text
        assert "证据包路径,请人工核验内容" in text
        # 单人绝不放行的中间态在输出中留痕(第一人确认后仍是待复核)。
        assert "待复核(pending)" in text

    def test_required_false_still_asks_confirmation(self, tmp_path: Path) -> None:
        four_eyes_mod = pytest.importorskip("netsentinel.decision.four_eyes")
        contracts = pytest.importorskip("netsentinel.contracts")
        db = tmp_path / "single.db"
        with four_eyes_mod.FourEyesQueue(db, False) as seed:
            entry_id = seed.add(
                contracts.SiteReport(
                    site_url="http://real.test/s", verdict=contracts.Verdict.NSFW
                )
            )
        out = io.StringIO()
        with four_eyes_mod.FourEyesQueue(db, False) as queue:
            tui = ReviewTUI(
                str(db),
                False,
                stdin=io.StringIO(
                    f"approve {entry_id} --reviewer 王五\nY\nquit\n"
                ),
                stdout=out,
                queue=queue,
            )
            code = tui.run()
            assert code == 0
            assert queue.get(entry_id).status == "approved"
        text = out.getvalue()
        # 四眼未启用也必须走 Y/N 人工确认。
        assert "我已人工核实(Y/N)" in text
        assert "条目 " in text and "已批准" in text


# ---------------------------------------------------------------------------
# main():argparse 包装与模块入口
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_with_db_and_four_eyes_flag(self, tmp_path: Path) -> None:
        db = tmp_path / "main.db"
        out = io.StringIO()
        code = main(
            ["--db", str(db), "--four-eyes"],
            stdin=io.StringIO("quit\n"),
            stdout=out,
        )
        assert code == 0
        text = out.getvalue()
        assert "净网哨兵" in text
        assert "四眼原则:已启用" in text
        assert str(db) in text
        assert db.exists()  # 队列已真实建库

    def test_main_without_four_eyes(self, tmp_path: Path) -> None:
        out = io.StringIO()
        code = main(
            ["--db", str(tmp_path / "m2.db")],
            stdin=io.StringIO("quit\n"),
            stdout=out,
        )
        assert code == 0
        assert "四眼原则:未启用" in out.getvalue()

    def test_main_help_exits_zero(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["--help"])
        assert excinfo.value.code == 0

    def test_module_entry_eof_exits_zero(self, tmp_path: Path) -> None:
        """``python -m netsentinel.cli.review_tui``:stdin=DEVNULL 即 EOF 退出。"""
        import os

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"  # 跨平台输出编码确定
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "netsentinel.cli.review_tui",
                "--db",
                str(tmp_path / "entry.db"),
                "--four-eyes",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            cwd=str(PROJECT_ROOT),
            env=env,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
        stdout = proc.stdout.decode("utf-8", "replace")
        assert "净网哨兵" in stdout
        assert "四眼原则:已启用" in stdout
        assert "EOF" in stdout


# ---------------------------------------------------------------------------
# V5 升级(A101):CJK 宽度 / ANSI 剥离 / 表格单遍宽度 / tui.* 遥测 /
# 人工确认红线锁定
# ---------------------------------------------------------------------------


class TestV5Upgrades:
    def test_v5_confirm_question_red_line_locked(self) -> None:
        # 红线:批准前的人工确认问题一字不动(仅 Y/y 通过)
        assert review_tui_mod._CONFIRM_QUESTION == "我已人工核实(Y/N)"

    def test_v5_display_width_cjk_aware_and_ansi_free(self) -> None:
        dw = review_tui_mod._display_width
        assert dw("中文") == 4                          # CJK 每字 2 列
        assert dw("abc") == 3                           # ASCII 每字 1 列
        assert dw(ANSI_RED + "🔴" + ANSI_RESET) == 2    # ANSI 不计宽、emoji 2 列
        assert dw("") == 0
        assert dw(review_tui_mod._pad("中", 5)) == 5    # 按可见宽度补空格后精确

    def test_v5_strip_ansi_variants(self) -> None:
        strip = review_tui_mod._strip_ansi
        assert strip("\x1b[31m红\x1b[0m") == "红"
        assert strip("plain") == "plain"
        assert strip("\x1b[") == ""                      # 截断的转义序列不抛
        assert strip("\x1b[1;31;40mXm") == "Xm"         # 复合 SGR 参数

    def test_v5_table_widths_computed_exactly_once(self, monkeypatch) -> None:
        # 单遍宽度计算:2 表头 + 2 行 × 2 列 = 6 次宽度计算,打印阶段不重算
        calls = {"n": 0}
        real = review_tui_mod._display_width

        def counting(text: str) -> int:
            calls["n"] += 1
            return real(text)

        monkeypatch.setattr(review_tui_mod, "_display_width", counting)
        out = io.StringIO()
        tui = ReviewTUI(
            "data/x.db",
            False,
            stdin=io.StringIO(""),
            stdout=out,
            queue=FakeQueue(),
        )
        tui._print_table(
            ["编号", "站点"], [["1", "http://a.test"], ["2", "http://b.test"]]
        )
        assert calls["n"] == 6

    def test_v5_approve_telemetry_success_only(self) -> None:
        telemetry.reset()
        entry = fake_entry(1)
        queue = FakeQueue([entry])
        script = (
            "approve 1 --reviewer 张三\nN\n"   # 取消:不计数
            "approve 1 --reviewer 张三\nY\n"   # 成功:tui.approve +1
            "quit\n"
        )
        code, out = run_tui(script, queue)
        assert code == 0
        counters = telemetry.snapshot()["counters"]
        assert counters.get("tui.approve") == 1.0
        assert counters.get("tui.reject", 0.0) == 0.0

    def test_v5_reject_telemetry_success_only(self) -> None:
        telemetry.reset()
        entry = fake_entry(1)
        queue = FakeQueue([entry])
        script = (
            "approve 1 --reviewer 张三\n也许\n"   # 非法回答:不计数
            "reject 1\n"                          # 缺理由:不计数
            "reject 404 --note 理由\n"            # 条目不存在:不计数
            "reject 1 --note 证据不足\n"          # 成功:tui.reject +1
            "quit\n"
        )
        code, out = run_tui(script, queue)
        assert code == 0
        counters = telemetry.snapshot()["counters"]
        assert counters.get("tui.reject") == 1.0
        assert counters.get("tui.approve", 0.0) == 0.0


# ---------------------------------------------------------------------------
# A234:事件账本接线(默认关 = 现状;开时复核动作自动双写;注入点断言)
# ---------------------------------------------------------------------------


class TestEventLedgerWiring:
    """event_log 参数:默认 None = 现状;注入后自建队列透传(A222 双写)。"""

    def _event_log(self, tmp_path: Path):
        storage_event_log = pytest.importorskip("netsentinel.storage.event_log")
        return storage_event_log.EventLog(tmp_path / "ledger.db")

    def test_default_off_is_status_quo(self, tmp_path: Path) -> None:
        """默认关:横幅无账本行,自建队列不接账本(现状快照)。"""
        out = io.StringIO()
        tui = ReviewTUI(
            tmp_path / "q.db", False, stdin=io.StringIO("quit\n"), stdout=out
        )
        try:
            assert tui.event_log is None
            assert tui._queue._event_log is None  # 注入点:未接线
            code = tui.run()
        finally:
            pass
        assert code == 0
        assert "事件账本" not in out.getvalue()

    def test_event_log_injected_into_owned_queue(self, tmp_path: Path) -> None:
        """注入点断言:event_log 透传给自建 FourEyesQueue,横幅如实展示。"""
        log = self._event_log(tmp_path)
        out = io.StringIO()
        tui = ReviewTUI(
            tmp_path / "q.db",
            True,
            stdin=io.StringIO("quit\n"),
            stdout=out,
            event_log=log,
        )
        try:
            assert tui.event_log is log
            assert tui._queue._event_log is log  # 注入点:同一实例透传
            assert tui.run() == 0
        finally:
            log.close()
        text = out.getvalue()
        assert "事件账本:已接线" in text
        assert str(tmp_path / "ledger.db") in text

    def test_event_log_ignored_for_injected_queue(self, tmp_path: Path) -> None:
        """注入 queue 时 event_log 不参与装配(fake 队列照常工作,横幅不误报)。"""
        log = self._event_log(tmp_path)
        entry = fake_entry(1)
        queue = FakeQueue([entry])
        out = io.StringIO()
        tui = ReviewTUI(
            tmp_path / "q.db",
            False,
            stdin=io.StringIO("quit\n"),
            stdout=out,
            queue=queue,
            event_log=log,
        )
        try:
            assert tui.run() == 0
            assert tui.event_log is log  # 引用保留,但注入队列不经它装配
        finally:
            log.close()
        assert "事件账本:已接线" not in out.getvalue()  # 不做无据声明

    def test_wired_tui_double_writes_review_actions(
        self, tmp_path: Path
    ) -> None:
        """接线开:双人 approve + reject 自动双写(先账本后状态,A222 语义)。"""
        from netsentinel.contracts import SiteReport, Verdict

        db = tmp_path / "wired.db"
        log = self._event_log(tmp_path)
        from netsentinel.decision.four_eyes import FourEyesQueue

        with FourEyesQueue(db, True, event_log=log) as seed_queue:
            entry_id = seed_queue.add(
                SiteReport(site_url="http://wire.test/a", verdict=Verdict.NSFW)
            )
            other_id = seed_queue.add(
                SiteReport(site_url="http://wire.test/b", verdict=Verdict.SUSPECT)
            )

        out = io.StringIO()
        tui = ReviewTUI(
            db,
            True,
            stdin=io.StringIO(
                f"approve {entry_id} --reviewer 张三\nY\n"
                f"approve {entry_id} --reviewer 李四\nY\n"
                f"reject {other_id} --note 证据不足\n"
                "quit\n"
            ),
            stdout=out,
            event_log=log,
        )
        try:
            assert tui.run() == 0
        finally:
            log.close()

        types = [
            e.event_type
            for e in self._iter_events(tmp_path / "ledger.db")
        ]
        assert types == [
            "entry_added",
            "entry_added",
            "four_eyes_awaiting_second",
            "four_eyes_approved",
            "entry_approved",
            "entry_rejected",
        ]
        # 状态投影与账本事实一致(离线重放可对账)。
        with FourEyesQueue(db, True) as q:
            assert q.get(entry_id).status == "approved"
            assert q.get(other_id).status == "rejected"

    @staticmethod
    def _iter_events(path: Path):
        from netsentinel.storage.event_log import EventLog

        with EventLog(path) as l:
            return l.iter_events()

    def test_main_event_ledger_flag_wires_and_closes(
        self, tmp_path: Path
    ) -> None:
        """CLI ``--event-ledger``:TUI 复核动作落账本;退出后账本可独立重开。"""
        from netsentinel.contracts import SiteReport, Verdict
        from netsentinel.decision.four_eyes import FourEyesQueue

        db = tmp_path / "main.db"
        ledger = tmp_path / "main-ledger.db"
        with FourEyesQueue(db, False) as seed:
            entry_id = seed.add(
                SiteReport(site_url="http://main.test", verdict=Verdict.NSFW)
            )
        out = io.StringIO()
        code = main(
            [
                "--db",
                str(db),
                "--event-ledger",
                str(ledger),
            ],
            stdin=io.StringIO(f"approve {entry_id} --reviewer 王五\nY\nquit\n"),
            stdout=out,
        )
        assert code == 0
        text = out.getvalue()
        assert "事件账本:已接线" in text
        assert str(ledger) in text
        assert ledger.exists()
        # seed 阶段 add 未接账本(历史数据,属"账本缺失"现状);接线后的
        # approve 动作才双写:账本里恰好一条 entry_approved(先账本后状态)。
        types = [e.event_type for e in self._iter_events(ledger)]
        assert types == ["entry_approved"]

    def test_main_without_ledger_flag_keeps_status_quo(
        self, tmp_path: Path
    ) -> None:
        """CLI 不带 ``--event-ledger``:不建账本文件、横幅无账本行(现状)。"""
        out = io.StringIO()
        code = main(
            ["--db", str(tmp_path / "m.db")],
            stdin=io.StringIO("quit\n"),
            stdout=out,
        )
        assert code == 0
        assert "事件账本" not in out.getvalue()
        assert not (tmp_path / "review_events.db").exists()
