# -*- coding: utf-8 -*-
"""sessionflow(会话累积总流程)单元测试 —— 全离线注入,零外呼、零浏览器。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from netsentinel.contracts import Config, SiteReport, Verdict
from netsentinel.decision.batch_review import BatchReview, REQUIRED_PHRASE
from netsentinel.decision.review_queue import ReviewQueue
from netsentinel.sessionflow import report_all, run_session, session_status
from netsentinel.sessionstore import SessionStore
from netsentinel.submit.batch_state import BatchState


def _cfg(tmp_path) -> Config:
    return Config(
        data_dir=str(tmp_path / "data"),
        db_path=str(tmp_path / "data" / "review_queue.db"),
        audit_path=str(tmp_path / "data" / "audit.jsonl"),
        evidence_dir=str(tmp_path / "data" / "evidence"),
    )


def _report(url: str, verdict: Verdict, agg: float, nsw: int = 3) -> SiteReport:
    return SiteReport(
        site_url=url,
        verdict=verdict,
        agg_nsw_prob=agg,
        nsw_image_count=nsw,
        needs_review=verdict is not Verdict.CLEAN,
    )


# ---------------------------------------------------------------------------
# run_session:扫描 → 累积 → 存档(注入 scanner/grouping/plan,零外呼)
# ---------------------------------------------------------------------------
def _fake_plan(valid, cfg):
    return {"to_scan": list(valid), "skipped_unchanged": 0, "duplicate_urls": 0}


def _make_grouping(out_groups):
    def _grouping(cfg, *, reports=None):
        return {"groups": list(out_groups), "enqueued": len(out_groups), "skipped_clean": 0}

    return _grouping


def test_run_session_accumulates_and_archives(tmp_path, capsys):
    """扫描结果按网站名字累积,结束自动存档;空报告也刷新存档。"""
    cfg = _cfg(tmp_path)
    reports = {
        "https://www.a.com/": _report("https://www.a.com/", Verdict.NSFW, 0.95),
        "https://b.com/": _report("https://b.com/", Verdict.SUSPECT, 0.60),
    }

    def scanner(urls, cfg_):
        return {"reports": dict(reports), "summary": {}}

    result = run_session(
        cfg,
        urls=list(reports),
        plan_fn=_fake_plan,
        scanner=scanner,
        grouping=_make_grouping(["g1", "g2"]),
    )
    assert result["scanned"] == 2
    assert result["groups"] == 2
    assert result["store_stats"] == {
        "groups": 2, "pending": 2, "reported": 0, "scans": 2,
    }
    archive = Path(result["archive_path"])
    assert archive.is_file()
    data = json.loads(archive.read_text("utf-8"))
    assert [g["name"] for g in data["pending_groups"]] == ["a.com", "b.com"]
    assert "会话扫描摘要" in capsys.readouterr().out


def test_run_session_continues_across_runs(tmp_path):
    """第二次运行在同一累积库上继续:scan_count 递增、组数不重复。"""
    cfg = _cfg(tmp_path)
    first = {
        "https://www.a.com/": _report("https://www.a.com/", Verdict.SUSPECT, 0.60),
    }

    def scan_first(urls, cfg_):
        return {"reports": dict(first), "summary": {}}

    run_session(
        cfg, urls=list(first), plan_fn=_fake_plan,
        scanner=scan_first, grouping=_make_grouping(["g"]),
    )
    second = {
        "https://img.a.com/": _report("https://img.a.com/", Verdict.NSFW, 0.95),
    }

    def scan_second(urls, cfg_):
        return {"reports": dict(second), "summary": {}}

    result = run_session(
        cfg, urls=list(second), plan_fn=_fake_plan,
        scanner=scan_second, grouping=_make_grouping(["g"]),
    )
    assert result["store_stats"]["groups"] == 1  # 同站继续累积,不重复建组
    with SessionStore(str(Path(cfg.data_dir) / "session" / "accumulation.db")) as st:
        g = st.get_group("a.com")
        assert g.scan_count == 2
        assert g.verdict == "nsfw"
        assert set(g.aliases) == {"www.a.com", "img.a.com"}


def test_run_session_input_validation(tmp_path):
    """--input 与 --urls 二选一:都空/都给均抛中文 ValueError。"""
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError):
        run_session(cfg)
    with pytest.raises(ValueError):
        run_session(cfg, input_path="x.txt", urls=["https://a.com/"])


def test_session_status_prints_groups(tmp_path, capsys):
    """--status:打印累积现状并返回快照。"""
    cfg = _cfg(tmp_path)
    with SessionStore(str(Path(cfg.data_dir) / "session" / "accumulation.db")) as st:
        st.accumulate("https://a.com/", "nsfw", 0.9)
    snap = session_status(cfg)
    out = capsys.readouterr().out
    assert snap["stats"]["groups"] == 1
    assert "a.com" in out and "待举报" in out


# ---------------------------------------------------------------------------
# report_all:确认举报(注入 prompt / submit;真实队列、声明台账、批次状态)
# ---------------------------------------------------------------------------
class _FakeBatchStateFactory:
    """记录 new_batch 调用的状态器工厂(真实 BatchState,离线 sqlite)。"""

    def __init__(self, db_path):
        self._db = db_path
        self.batches: list[list] = []

    def __call__(self, db_path):
        outer = self

        class _Wrapped(BatchState):
            def new_batch(self, items, note=""):
                outer.batches.append(list(items))
                return super().new_batch(items, note=note)

        return _Wrapped(db_path)


def _setup_report_fixture(tmp_path):
    """累积两组(nsfw 在前)+ 复核队列 pending 条目(note 带组标记)。"""
    cfg = _cfg(tmp_path)
    with SessionStore(str(Path(cfg.data_dir) / "session" / "accumulation.db")) as st:
        st.accumulate("https://www.a.com/", "nsfw", 0.95)
        st.accumulate("https://b.com/", "suspect", 0.60)
    queue = ReviewQueue(cfg.db_path)
    queue.add(
        _report("https://www.a.com/", Verdict.NSFW, 0.95),
        "data/evidence/a.zip",
        note="[组:a.com]",
    )
    queue.add(
        _report("https://b.com/", Verdict.SUSPECT, 0.60),
        "data/evidence/b.zip",
        note="[组:b.com]",
    )
    return cfg, queue


def test_report_all_confirmed_reports_group_by_site(tmp_path, capsys):
    """确认 y → 批准 pending + 逐组声明 + 按网站名排序提交 + 收档 + 存档。"""
    cfg, queue = _setup_report_fixture(tmp_path)
    captured: dict = {}

    def fake_prompt(text):
        captured.setdefault("prompts", []).append(text)
        return "y"

    def fake_run_batch(items, cfg_, *, state=None, dry_run=None):
        captured["items"] = list(items)
        captured["dry_run"] = dry_run
        return {
            "submitted": len(items),
            "failed": 0,
            "rate_limited": False,
            "paused": False,
            "results": [
                {
                    "entry_id": i["entry_id"],
                    "group_name": i["group_name"],
                    "ok": True,
                    "submitted": True,
                    "error": "",
                    "portal": "12377",
                }
                for i in items
            ],
            "note": "",
        }

    summary = report_all(
        cfg,
        reviewer="张三",
        prompt=fake_prompt,
        queue=queue,
        state_cls=_FakeBatchStateFactory(cfg.db_path),
        submit=fake_run_batch,
    )

    assert summary["confirmed"] is True
    assert summary["groups_total"] == 2
    assert summary["groups_reported"] == 2
    assert summary["submitted"] == 2
    assert summary["approved"] == 2
    # 按网站名字分组排序:nsfw 的 a.com 组排在 suspect 的 b.com 之前
    assert [i["group_name"] for i in captured["items"]] == ["a.com", "b.com"]
    # 缺省跟随 cfg.dry_run_default(默认 True 干跑)
    assert captured["dry_run"] is True
    # 复核队列:pending 全部 → approved → submitted(真实提交口径回写)
    assert len(queue.list(status="submitted")) == 2
    assert len(queue.list(status="pending")) == 0
    # 红线 25:逐组批量声明留痕(审核人 + "人工核实")
    with BatchReview(cfg.db_path) as review:
        atts = {a.group_name: a for a in review.list_attestations()}
        assert set(atts) == {"a.com", "b.com"}
        assert atts["a.com"].reviewer == "张三"
        assert REQUIRED_PHRASE in atts["a.com"].text
    # 累积库收档 + 存档刷新
    with SessionStore(str(Path(cfg.data_dir) / "session" / "accumulation.db")) as st:
        assert st.pending_groups() == []
        assert st.stats()["reported"] == 2
    assert Path(summary["archive_path"]).is_file()
    out = capsys.readouterr().out
    assert "红线 24" in out
    # 确认提示语必须包含"人工核实"(红线 25 的显式自述要求)
    confirm_prompts = [t for t in captured.get("prompts", []) if "确认" in t]
    assert confirm_prompts and "人工核实" in confirm_prompts[0]


def test_report_all_abort_without_confirm_changes_nothing(tmp_path):
    """确认非 y:零状态变更 —— 不批准、不声明、不提交、不收档。"""
    cfg, queue = _setup_report_fixture(tmp_path)

    def refusing_submit(items, cfg_, **kw):  # pragma: no cover - 不应被调用
        raise AssertionError("未确认时绝不能提交")

    summary = report_all(
        cfg,
        reviewer="张三",
        prompt=lambda prompt_text: "n",
        queue=queue,
        state_cls=_FakeBatchStateFactory(cfg.db_path),
        submit=refusing_submit,
    )
    assert summary["confirmed"] is False
    assert summary["submitted"] == 0
    assert len(queue.list(status="pending")) == 2       # 未批准
    assert len(queue.list(status="approved")) == 0
    with BatchReview(cfg.db_path) as review:
        assert review.list_attestations() == []          # 未声明
    with SessionStore(str(Path(cfg.data_dir) / "session" / "accumulation.db")) as st:
        assert len(st.pending_groups()) == 2             # 未收档


def test_report_all_missing_reviewer_aborts(tmp_path):
    """审核人为空且交互输入也为空:中止(举报确认必须留痕审核人)。"""
    cfg, queue = _setup_report_fixture(tmp_path)

    def refusing_submit(items, cfg_, **kw):  # pragma: no cover - 不应被调用
        raise AssertionError("无审核人时绝不能提交")

    summary = report_all(
        cfg,
        prompt=lambda prompt_text: "",
        queue=queue,
        state_cls=_FakeBatchStateFactory(cfg.db_path),
        submit=refusing_submit,
    )
    assert summary["confirmed"] is False
    assert len(queue.list(status="pending")) == 2


def test_report_all_empty_store_zero_summary(tmp_path):
    """累积库为空:零值摘要返回,不进入确认交互。"""
    cfg = _cfg(tmp_path)

    def exploding_prompt(text):  # pragma: no cover - 不应被调用
        raise AssertionError("空库不应进入确认交互")

    summary = report_all(cfg, prompt=exploding_prompt)
    assert summary["groups_total"] == 0
    assert Path(summary["archive_path"]).is_file()


def test_report_all_excludes_reported_and_foreign_groups(tmp_path):
    """已举报组与其他流程条目不进入本轮;条目全部终态的组自动收档。"""
    cfg = _cfg(tmp_path)
    with SessionStore(str(Path(cfg.data_dir) / "session" / "accumulation.db")) as st:
        st.accumulate("https://done.com/", "nsfw", 0.9)
        st.mark_reported(["done.com"])                     # 已举报组
        st.accumulate("https://pending.com/", "suspect", 0.6)
    queue = ReviewQueue(cfg.db_path)
    # 已举报组的条目(approved)与无关组条目:都不应进入提交
    queue.add(_report("https://done.com/", Verdict.NSFW, 0.9), "z.zip", note="[组:done.com]")
    queue.approve(1, actor="前置")
    queue.add(_report("https://other.com/", Verdict.SUSPECT, 0.6), "o.zip", note="[组:other.com]")
    queue.approve(2, actor="前置")

    captured: dict = {}

    def fake_run_batch(items, cfg_, *, state=None, dry_run=None):
        captured["items"] = list(items)
        return {
            "submitted": len(items), "failed": 0, "rate_limited": False,
            "paused": False,
            "results": [
                {"entry_id": i["entry_id"], "group_name": i["group_name"],
                 "ok": True, "submitted": True, "error": "", "portal": "12377"}
                for i in items
            ],
            "note": "",
        }

    summary = report_all(
        cfg, reviewer="李四", prompt=lambda t: "y", queue=queue,
        state_cls=_FakeBatchStateFactory(cfg.db_path), submit=fake_run_batch,
    )
    # pending.com 组在队列中没有条目:无可执行组,不进入确认与提交
    assert summary["confirmed"] is False
    assert summary["groups_total"] == 0
    assert captured == {}                        # 没有任何提交发生
    # done.com 组条目状态保持 approved(未被触碰)
    assert queue.get(1).status == "approved"
    assert queue.get(2).status == "approved"


def test_report_all_rate_limited_marks_only_completed(tmp_path):
    """频控挂起:已提交条目回写 submitted,未完成组留待下次续跑。"""
    cfg = _cfg(tmp_path)
    with SessionStore(str(Path(cfg.data_dir) / "session" / "accumulation.db")) as st:
        st.accumulate("https://a.com/", "nsfw", 0.95)
        st.accumulate("https://b.com/", "suspect", 0.60)
    queue = ReviewQueue(cfg.db_path)
    queue.add(_report("https://www.a.com/", Verdict.NSFW, 0.95), "a.zip", note="[组:a.com]")
    queue.add(_report("https://b.com/", Verdict.SUSPECT, 0.60), "b.zip", note="[组:b.com]")

    def partial_run_batch(items, cfg_, *, state=None, dry_run=None):
        # 第一批(a.com)成功;返回 rate_limited=True 挂起整体
        return {
            "submitted": 1, "failed": 0, "rate_limited": True, "paused": False,
            "results": [
                {"entry_id": items[0]["entry_id"], "group_name": items[0]["group_name"],
                 "ok": True, "submitted": True, "error": "", "portal": "12377"},
            ],
            "note": "额度/间隔限制,可续批",
        }

    summary = report_all(
        cfg, reviewer="王五", prompt=lambda t: "y", queue=queue,
        state_cls=_FakeBatchStateFactory(cfg.db_path), submit=partial_run_batch,
    )
    assert summary["rate_limited"] is True
    assert summary["submitted"] == 1
    # a.com 完成 → submitted + 收档;b.com 条目已批准但未提交,组保持待举报
    assert queue.get(1).status == "submitted"
    assert queue.get(2).status == "approved"
    with SessionStore(str(Path(cfg.data_dir) / "session" / "accumulation.db")) as st:
        assert [g.name for g in st.pending_groups()] == ["b.com"]


# ---------------------------------------------------------------------------
# 红线静态断言(项目惯例:源码级守卫)
# ---------------------------------------------------------------------------
def test_source_has_no_auto_confirm_true():
    """红线 24:sessionflow 源码不得出现任何 auto_confirm=True 路径。"""
    source = Path(__import__("netsentinel.sessionflow", fromlist=["x"]).__file__).read_text(
        "utf-8"
    )
    assert "auto_confirm=True" not in source


def test_source_report_path_wired_into_archive():
    """report_all 摘要必须携带存档路径(结束运行对结果存档)。"""
    source = Path(__import__("netsentinel.sessionflow", fromlist=["x"]).__file__).read_text(
        "utf-8"
    )
    assert '"archive_path"' in source
