"""netsentinel.cli.batch_tui 单元测试(V6 · A111 · 交互式批量复核 TUI)。

覆盖契约 §4 A111 与任务书要求的全部场景:
  * groups 分组总览:按备注“[组:名]”聚合 pending/approved、判定 ANSI 徽章
    (🔴/🟡/🟢)、agg 最大列、已声明 ✓/✗、rejected/submitted/未分组排除、
    声明模块缺失时降级为“?”;
  * show 组内逐条:站点/判定/agg/证据包路径 + “请人工核验内容”;
  * attest 全流程:show → “批准前请逐站打开证据包核实” → “我已逐站人工核实
    (Y/N)”:Y 落声明(文案固定含“人工核实”,组名/条数/审核人正确)、
    N / 乱码 / EOF 一律取消不落库;缺 --reviewer / 值缺失 / 未知组中文报错;
    声明层文案校验 ValueError 被捕获且主循环不崩;
  * ready:注入 ready_entries 打印待批量清单 + 固定中文提示
    “批量提交需在 CLI/TUI 外经 run_batch 逐条人工门执行”;空清单;
    A110 模块缺失中文错误(sys.modules 毒化,确定性跳过真实 import);
  * queue-batch:Y 只打印 dry_run 演练示例与真实命令行、**不执行**
    (注入带计数哨兵断言调用数恒为 0,红线 24);N 取消;--portal 覆盖
    (12377|shdf,非法值中文报错);空 ready 不发确认问题;
  * 未知命令中文提示、EOF 退出码 0;
  * main() argparse 包装(--db)与 ``python -m netsentinel.cli.batch_tui``
    模块入口(subprocess + stdin=DEVNULL,零交互真实终端);
  * 真实 SQLite 集成(importorskip 保护 A110):入列 → 标组 → approve →
    groups(✗)→ attest Y 落库 → groups(✓)→ ready → queue-batch 演练。

全部离线,stdin/stdout 用 io.StringIO 注入,零外呼、零真实门户、零真实提交。
"""
from __future__ import annotations

import io
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# TUI 本体与真实模块的导入均受 importorskip 保护(缺失时跳过而非报错)。
pytest.importorskip("netsentinel.cli.batch_tui")
from netsentinel.cli.batch_tui import BatchTUI, main  # noqa: E402
from netsentinel.contracts import Config  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: 人工确认问题(红线 25,一字不可改)。
CONFIRM_QUESTION = "我已逐站人工核实(Y/N)"

#: ready 固定提示(任务书原文)。
READY_HINT = "批量提交需在 CLI/TUI 外经 run_batch 逐条人工门执行"


# ---------------------------------------------------------------------------
# 测试替身:fake 队列 / fake 声明层 / 执行哨兵
# ---------------------------------------------------------------------------


def group_entry(
    entry_id: int,
    *,
    group: str,
    verdict: str = "nsfw",
    status: str = "pending",
    site: str | None = None,
    evidence: str = "data/evidence/g.zip",
    agg: float | None = 0.5,
    note: str | None = None,
) -> SimpleNamespace:
    """构造带“[组:名]”备注标记的最小条目(鸭子类型;agg 走 agg_nsw_prob)。"""
    return SimpleNamespace(
        id=entry_id,
        site_url=site if site is not None else f"http://{group}/p{entry_id}",
        verdict=verdict,
        status=status,
        evidence_zip=evidence,
        created_at="2026-10-02T09:00:00+08:00",
        updated_at="2026-10-02T09:30:00+08:00",
        note=note if note is not None else f"[组:{group}] 合并证据包",
        agg_nsw_prob=agg,
    )


class FakeQueue:
    """结构兼容 ReviewQueue 的记录型 fake(仅 list/get;生命周期归调用方)。"""

    def __init__(self, entries=()) -> None:
        self.entries: list[SimpleNamespace] = list(entries)
        self.list_filters: list[str | None] = []

    def list(self, status: str | None = None) -> list[SimpleNamespace]:
        self.list_filters.append(status)
        if status is None:
            return list(self.entries)
        return [e for e in self.entries if getattr(e, "status", "") == status]

    def get(self, id: int) -> SimpleNamespace | None:
        return next((e for e in self.entries if e.id == id), None)

    def close(self) -> None:  # pragma: no cover - 注入队列不应被 TUI 关闭
        raise AssertionError("注入队列的生命周期归调用方,TUI 不应关闭它")


class FakeReview:
    """结构兼容 A110 BatchReview 的记录型 fake(文案校验同真实语义)。"""

    def __init__(self, attested=()) -> None:
        self._attested = set(attested)
        self.attestations: list[dict] = []
        self.calls = 0

    def attest(self, group_name: str, items: int, reviewer: str, text: str):
        self.calls += 1
        if "人工核实" not in text:
            raise ValueError("声明文本必须包含“人工核实”四字,已拒绝记录")
        self._attested.add(group_name)
        record = {
            "group_name": group_name,
            "items": items,
            "reviewer": reviewer,
            "text": text,
        }
        self.attestations.append(record)
        return SimpleNamespace(ts="2026-10-02T10:00:00+08:00", **record)

    def is_attested(self, group_name: str) -> bool:
        return group_name in self._attested


class StrictFakeReview(FakeReview):
    """声明层文案校验永远拒绝的 fake(模拟 A110 ValueError 上抛路径)。"""

    def attest(self, group_name: str, items: int, reviewer: str, text: str):
        self.calls += 1
        raise ValueError("声明文本必须包含“人工核实”四字,已拒绝记录")


class TripwireExecutor:
    """红线 24 哨兵:若 TUI 试图执行任何批量提交即刻失败并计数。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args: object, **kwargs: object) -> None:
        self.calls += 1
        raise AssertionError("红线 24:批量复核 TUI 不得调用任何执行器/run_batch")


def make_tui(
    entries=(),
    review: FakeReview | None = None,
    script: str = "",
) -> tuple[BatchTUI, io.StringIO, FakeReview]:
    """组装注入 io/queue/review 的 BatchTUI(零真实终端、零真实 SQLite)。"""
    buf = io.StringIO()
    queue = FakeQueue(entries)
    if review is None:
        review = FakeReview()
    tui = BatchTUI(
        "unused_batch_tui.db",
        Config(),
        stdin=io.StringIO(script),
        stdout=buf,
        queue=queue,
        review=review,
    )
    return tui, buf, review


def poison_batch_review(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 A110 模块毒化为 sys.modules None → import 必失败(确定性,离线)。"""
    monkeypatch.setitem(sys.modules, "netsentinel.decision.batch_review", None)


# ---------------------------------------------------------------------------
# help / 横幅 / 退出
# ---------------------------------------------------------------------------


def test_help_lists_commands_and_redlines() -> None:
    tui, buf, _ = make_tui(script="help\nquit\n")
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    for cmd in ("groups", "show <组名>", "attest <组名> --reviewer <名字>",
                "ready", "queue-batch"):
        assert cmd in out
    assert "我已逐站人工核实(Y/N)" in out
    assert "不执行真实提交" in out
    assert "逐条人工门" in out


def test_banner_and_quit_exit_zero() -> None:
    tui, buf, _ = make_tui(script="quit\n")
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    assert "批量复核 TUI" in out
    assert "unused_batch_tui.db" in out
    assert "再见" in out


def test_unknown_command_chinese_then_continue() -> None:
    tui, buf, _ = make_tui(script="frobnicate\nquit\n")
    rc = tui.run()
    assert rc == 0
    assert "未知命令:frobnicate" in buf.getvalue()
    assert "help" in buf.getvalue()


def test_eof_exits_zero() -> None:
    tui, buf, _ = make_tui(script="")
    rc = tui.run()
    assert rc == 0
    assert "收到输入结束(EOF)" in buf.getvalue()


# ---------------------------------------------------------------------------
# groups:分组总览
# ---------------------------------------------------------------------------


def test_groups_table_badges_and_attest_marks() -> None:
    entries = [
        group_entry(1, group="attested.com", verdict="nsfw", agg=0.91),
        group_entry(2, group="attested.com", verdict="nsfw", agg=0.77, status="approved"),
        group_entry(3, group="fresh.com", verdict="suspect", agg=0.42),
    ]
    tui, buf, _ = make_tui(entries, review=FakeReview(attested={"attested.com"}),
                           script="groups\nquit\n")
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    for header in ("组名", "条数", "判定", "agg 最大", "已声明"):
        assert header in out
    assert "attested.com" in out and "fresh.com" in out
    assert "🔴 高置信色情(nsfw)" in out  # attested.com 组判定取最严重档
    assert "🟡 疑似(suspect)" in out
    assert "✓ 已声明" in out and "✗ 未声明" in out
    assert "共 2 组 · 3 条" in out


def test_groups_excludes_rejected_submitted_and_ungrouped() -> None:
    entries = [
        group_entry(1, group="gone.com", status="rejected"),
        group_entry(2, group="sent.com", status="submitted"),
        group_entry(3, group="misc", status="pending", note="普通单站,无组标记"),
        group_entry(4, group="keep.com", status="pending"),
    ]
    tui, buf, _ = make_tui(entries, script="groups\nquit\n")
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    assert "keep.com" in out
    assert "gone.com" not in out and "sent.com" not in out
    assert "共 1 组 · 1 条" in out
    assert "未带组标记 1 条" in out


def test_groups_empty_message() -> None:
    tui, buf, _ = make_tui(script="groups\nquit\n")
    rc = tui.run()
    assert rc == 0
    assert "当前没有可批量分组的条目" in buf.getvalue()


def test_groups_agg_max_column() -> None:
    entries = [
        group_entry(1, group="agg.com", agg=0.31),
        group_entry(2, group="agg.com", agg=0.87, status="approved"),
    ]
    tui, buf, _ = make_tui(entries, script="groups\nquit\n")
    rc = tui.run()
    assert rc == 0
    assert "0.87" in buf.getvalue()  # 组内 agg 取最大


def test_groups_degrades_when_review_module_missing(monkeypatch) -> None:
    poison_batch_review(monkeypatch)
    entries = [group_entry(1, group="x.com")]
    buf = io.StringIO()
    tui = BatchTUI(
        "unused.db",
        Config(),
        stdin=io.StringIO("groups\nquit\n"),
        stdout=buf,
        queue=FakeQueue(entries),
        review=None,
    )
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    assert "?(声明模块未安装)" in out
    assert "A110" in out and "不可用" in out


# ---------------------------------------------------------------------------
# show:组内逐条明细
# ---------------------------------------------------------------------------


def test_show_group_detail() -> None:
    entries = [
        group_entry(1, group="evil.com", site="http://a.evil.com/x",
                    evidence="data/evidence/evil_a.zip", agg=0.92),
        group_entry(2, group="evil.com", site="http://b.evil.com/y",
                    evidence="data/evidence/evil_b.zip", agg=0.55, status="approved"),
    ]
    tui, buf, _ = make_tui(entries, script="show evil.com\nquit\n")
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    assert "── 组 evil.com(共 2 条)──" in out
    assert "http://a.evil.com/x" in out and "http://b.evil.com/y" in out
    assert "🔴 高置信色情(nsfw)" in out
    assert "0.92" in out and "0.55" in out
    assert "data/evidence/evil_a.zip" in out
    assert "请人工核验内容" in out


def test_show_unknown_group() -> None:
    tui, buf, _ = make_tui([group_entry(1, group="real.com")],
                           script="show nope.com\nquit\n")
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    assert "未找到组:nope.com" in out
    assert "groups" in out


def test_show_usage_error() -> None:
    tui, buf, _ = make_tui(script="show\nquit\n")
    rc = tui.run()
    assert rc == 0
    assert "用法:show <组名>" in buf.getvalue()


# ---------------------------------------------------------------------------
# attest:批量确认声明全流程(红线 25)
# ---------------------------------------------------------------------------


def test_attest_yes_records_declaration() -> None:
    entries = [
        group_entry(1, group="evil.com", evidence="data/evidence/e1.zip"),
        group_entry(2, group="evil.com", status="approved",
                    evidence="data/evidence/e2.zip"),
    ]
    tui, buf, review = make_tui(entries, script="attest evil.com --reviewer 张三\nY\nquit\n")
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    # 流程三步:show 明细 → 核验提示 → 确认问题。
    assert "── 组 evil.com(共 2 条)──" in out
    assert "data/evidence/e1.zip" in out
    assert "批准前请逐站打开证据包核实" in out
    assert CONFIRM_QUESTION in out
    assert "声明已记录" in out
    assert "审核人:张三" in out
    # 落库内容:组名 / 条数 / 审核人 / 固定文案含“人工核实”。
    assert review.calls == 1
    assert len(review.attestations) == 1
    rec = review.attestations[0]
    assert rec["group_name"] == "evil.com"
    assert rec["items"] == 2
    assert rec["reviewer"] == "张三"
    assert "人工核实" in rec["text"]
    assert review.is_attested("evil.com") is True


def test_attest_no_cancels_without_recording() -> None:
    entries = [group_entry(1, group="evil.com")]
    tui, buf, review = make_tui(entries, script="attest evil.com --reviewer 张三\nN\nquit\n")
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    assert CONFIRM_QUESTION in out
    assert "已取消" in out
    assert review.calls == 0
    assert review.is_attested("evil.com") is False


def test_attest_garbage_answer_cancels() -> None:
    entries = [group_entry(1, group="evil.com")]
    tui, buf, review = make_tui(entries, script="attest evil.com --reviewer 张三\n嗯??\nquit\n")
    rc = tui.run()
    assert rc == 0
    assert "已取消" in buf.getvalue()
    assert review.calls == 0


def test_attest_eof_at_question_cancels() -> None:
    entries = [group_entry(1, group="evil.com")]
    tui, buf, review = make_tui(entries, script="attest evil.com --reviewer 李四\n")
    rc = tui.run()  # 问题处读尽 → 取消;随后主循环 EOF 退出
    out = buf.getvalue()
    assert rc == 0
    assert CONFIRM_QUESTION in out
    assert "已取消" in out
    assert "收到输入结束(EOF)" in out
    assert review.calls == 0


def test_attest_missing_reviewer_option() -> None:
    entries = [group_entry(1, group="evil.com")]
    tui, buf, review = make_tui(entries, script="attest evil.com\nquit\n")
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    assert "缺少 --reviewer" in out
    assert "例如 attest example.com --reviewer 张三" in out
    assert CONFIRM_QUESTION not in out  # 未进入确认问题
    assert review.calls == 0


def test_attest_reviewer_without_value() -> None:
    entries = [group_entry(1, group="evil.com")]
    tui, buf, review = make_tui(entries, script="attest evil.com --reviewer\nquit\n")
    rc = tui.run()
    assert rc == 0
    assert "--reviewer 需要审核人姓名" in buf.getvalue()
    assert review.calls == 0


def test_attest_unknown_group() -> None:
    tui, buf, review = make_tui([group_entry(1, group="real.com")],
                                script="attest nope.com --reviewer 张三\nquit\n")
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    assert "未找到组:nope.com" in out
    assert CONFIRM_QUESTION not in out
    assert review.calls == 0


def test_attest_review_layer_validation_error_keeps_loop_alive() -> None:
    entries = [group_entry(1, group="evil.com")]
    tui, buf, review = make_tui(
        entries,
        review=StrictFakeReview(),
        script="attest evil.com --reviewer 张三\nY\nquit\n",
    )
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    assert "错误:声明文本必须包含“人工核实”四字" in out
    assert "声明已记录" not in out
    assert "再见" in out  # 主循环未崩,后续命令照常执行
    assert review.calls == 1  # 声明层确曾收到请求并被其校验拒绝


def test_attest_module_missing_chinese_error(monkeypatch) -> None:
    poison_batch_review(monkeypatch)
    entries = [group_entry(1, group="evil.com")]
    buf = io.StringIO()
    tui = BatchTUI(
        "unused.db",
        Config(),
        stdin=io.StringIO("attest evil.com --reviewer 张三\nY\nquit\n"),
        stdout=buf,
        queue=FakeQueue(entries),
        review=None,
    )
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    assert "不可用,无法记录声明" in out
    assert "声明已记录" not in out


# ---------------------------------------------------------------------------
# ready:待批量清单
# ---------------------------------------------------------------------------


def test_ready_prints_items_and_hint() -> None:
    items = [
        {"entry_id": 5, "group_name": "evil.com",
         "site_urls": ["http://a.evil.com/x", "http://b.evil.com/y"], "portal": 12377},
        {"entry_id": 9, "group_name": "bad.net",
         "site_urls": ["http://bad.net/z"], "portal": 12377},
    ]
    captured: list[tuple[object, object, object]] = []

    def fake_ready(cfg, *, queue=None, review=None):
        captured.append((cfg, queue, review))
        return items

    tui, buf, _ = make_tui(script="ready\nquit\n")
    tui._ready_entries_fn = fake_ready
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    assert "待批量清单(共 2 条)" in out
    assert "evil.com" in out and "bad.net" in out
    assert "http://a.evil.com/x" in out
    assert "12377" in out
    assert READY_HINT in out
    assert "(本 TUI 不提交)" in out
    # ready_entries 收到 TUI 的 cfg / queue / review。
    assert len(captured) == 1
    cfg, queue, review = captured[0]
    assert isinstance(cfg, Config)
    assert isinstance(queue, FakeQueue)
    assert isinstance(review, FakeReview)


def test_ready_empty_message() -> None:
    tui, buf, _ = make_tui(script="ready\nquit\n")
    tui._ready_entries_fn = lambda cfg, *, queue=None, review=None: []
    rc = tui.run()
    assert rc == 0
    assert "当前没有待批量条目" in buf.getvalue()


def test_ready_module_missing_chinese_error(monkeypatch) -> None:
    poison_batch_review(monkeypatch)
    tui, buf, _ = make_tui(script="ready\nquit\n")  # FakeReview 无 ready_entries
    assert tui._ready_entries_fn is None
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    assert "不可用,无法生成 ready 条目" in out
    assert "待批量清单(共" not in out


# ---------------------------------------------------------------------------
# queue-batch:批量队列预览(只演练 / 打印命令,绝不执行 —— 红线 24)
# ---------------------------------------------------------------------------


def _ready_items() -> list[dict]:
    return [
        {"entry_id": 5, "group_name": "evil.com",
         "site_urls": ["http://a.evil.com/x"], "portal": 12377},
        {"entry_id": 9, "group_name": "bad.net",
         "site_urls": ["http://bad.net/z"], "portal": 12377},
    ]


def test_queue_batch_yes_dry_run_only_never_executes() -> None:
    tui, buf, _ = make_tui(script="queue-batch\nY\nquit\n")
    tui._ready_entries_fn = lambda cfg, *, queue=None, review=None: _ready_items()
    trip = TripwireExecutor()
    tui._run_batch_hook = trip  # 红线 24 哨兵:TUI 任何路径都不得调用它
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    assert "确认将依次批量举报 2 条,每条仍需人工输入验证码(Y/N)" in out
    # 只打印:dry_run 演练示例 + 真实命令行。
    assert "dry_run 演练调用示例" in out
    assert "from netsentinel.submit.batch_submit import run_batch" in out
    assert "run_batch(items, cfg, dry_run=True)" in out
    assert "python -m netsentinel.batchflow --resume <批次号> --exec" in out
    assert "不执行任何提交" in out
    assert "绝不代填验证码" in out
    # 核心:零执行。
    assert trip.calls == 0


def test_queue_batch_no_cancels() -> None:
    tui, buf, _ = make_tui(script="queue-batch\nN\nquit\n")
    tui._ready_entries_fn = lambda cfg, *, queue=None, review=None: _ready_items()
    trip = TripwireExecutor()
    tui._run_batch_hook = trip
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    assert "确认将依次批量举报 2 条" in out
    assert "已取消:未确认" in out
    assert "dry_run" not in out
    assert "run_batch" not in out
    assert trip.calls == 0


def test_queue_batch_portal_override() -> None:
    tui, buf, _ = make_tui(script="queue-batch --portal shdf\nY\nquit\n")
    tui._ready_entries_fn = lambda cfg, *, queue=None, review=None: _ready_items()
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    assert "门户:shdf" in out          # 命令说明段带门户
    assert out.count("shdf") >= 2      # 预览表格“门户”列同样覆盖


def test_queue_batch_default_portal_12377() -> None:
    tui, buf, _ = make_tui(script="queue-batch\nY\nquit\n")
    tui._ready_entries_fn = lambda cfg, *, queue=None, review=None: _ready_items()
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    assert "门户:12377" in out


def test_queue_batch_invalid_portal() -> None:
    tui, buf, _ = make_tui(script="queue-batch --portal foo\nquit\n")
    tui._ready_entries_fn = lambda cfg, *, queue=None, review=None: _ready_items()
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    assert "未知门户:foo" in out
    assert "12377 / shdf" in out


def test_queue_batch_empty_ready_skips_question() -> None:
    tui, buf, _ = make_tui(script="queue-batch\nY\nquit\n")
    tui._ready_entries_fn = lambda cfg, *, queue=None, review=None: []
    trip = TripwireExecutor()
    tui._run_batch_hook = trip
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    assert "无待批量条目" in out
    assert "确认将依次批量举报" not in out  # 空清单不发确认问题
    assert trip.calls == 0


def test_queue_batch_missing_ready_module(monkeypatch) -> None:
    poison_batch_review(monkeypatch)
    tui, buf, _ = make_tui(script="queue-batch\nY\nquit\n")
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    assert "不可用,无法生成 ready 条目" in out
    assert "确认将依次批量举报" not in out


# ---------------------------------------------------------------------------
# main() 包装与 python -m 模块入口
# ---------------------------------------------------------------------------


def test_main_wrapper_creates_real_sqlite_db(tmp_path) -> None:
    db = tmp_path / "main_tui.db"
    buf = io.StringIO()
    rc = main(["--db", str(db)], stdin=io.StringIO("groups\nquit\n"), stdout=buf)
    out = buf.getvalue()
    assert rc == 0
    assert "批量复核 TUI" in out
    assert "当前没有可批量分组的条目" in out
    assert db.exists()


def test_python_m_module_entry_eof_exits_zero(tmp_path) -> None:
    db = tmp_path / "module_entry.db"
    proc = subprocess.run(
        [sys.executable, "-m", "netsentinel.cli.batch_tui", "--db", str(db)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(PROJECT_ROOT),
        timeout=90,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    out = proc.stdout.decode("utf-8", errors="replace")
    assert "批量复核 TUI" in out
    assert "EOF" in out
    assert db.exists()


# ---------------------------------------------------------------------------
# 真实 SQLite 集成(A110 合入后自动生效;缺失时 importorskip 跳过)
# ---------------------------------------------------------------------------


def test_real_sqlite_batch_flow(tmp_path) -> None:
    pytest.importorskip("netsentinel.decision.batch_review")
    from netsentinel.contracts import SiteReport, Verdict
    from netsentinel.decision.review_queue import ReviewQueue

    db = str(tmp_path / "batch_flow.db")
    queue = ReviewQueue(db)
    ids = []
    for i, url in enumerate(("http://demo.com/a", "http://demo.com/b"), start=1):
        report = SiteReport(site_url=url, verdict=Verdict.NSFW, agg_nsw_prob=0.93)
        ids.append(queue.add(report, evidence_zip=f"data/evidence/demo{i}.zip"))
    # A108 group_and_enqueue 语义:组名入 note 前缀“[组:名]”(测试直写库模拟)。
    conn = sqlite3.connect(db)
    for entry_id in ids:
        conn.execute(
            "UPDATE entries SET note = ? WHERE id = ?",
            ("[组:demo.com] 合并证据包", entry_id),
        )
    conn.commit()
    conn.close()
    for entry_id in ids:
        queue.approve(entry_id)

    cfg = Config(db_path=db)
    buf = io.StringIO()
    script = (
        "groups\n"
        "attest demo.com --reviewer 张三\n"
        "Y\n"
        "groups\n"
        "ready\n"
        "queue-batch\n"
        "Y\n"
        "quit\n"
    )
    tui = BatchTUI(db, cfg, stdin=io.StringIO(script), stdout=buf, queue=queue)
    rc = tui.run()
    out = buf.getvalue()
    assert rc == 0
    assert "demo.com" in out
    assert "✗ 未声明" in out          # 声明前
    assert CONFIRM_QUESTION in out
    assert "声明已记录" in out
    assert "✓ 已声明" in out          # 声明后
    assert READY_HINT in out
    assert "dry_run=True" in out      # queue-batch 只演练
    # 声明确已落库(真实 BatchReview 另开连接核验)。
    from netsentinel.decision.batch_review import BatchReview

    assert BatchReview(db).is_attested("demo.com") is True
    queue.close()
