"""A168 ``agent.summary_agent`` 单元测试(全 fake 注入,离线/零外呼)。

覆盖契约 §2 A168 与红线 36 要点:

- 正常 outcome 全字段(groups 摘要行 / 报告路径 / ready_count /
  attest_pending / workers / tier / next_steps);
- 举报准备:pending→approved 条目按 ``[组:名]`` 聚合,ready_count =
  已声明已批准数,attest_pending = 已批准未声明组名(首见序);
  ``batch_require_attestation=False`` 全放行;库文件缺失零副作用;
- ``summary_agent_enabled=False`` → 最小 outcome(仅 tier/workers/
  ``["配置关闭"]``,不调 grouper、不落盘);
- 收官报告:A172 兄弟缺席 / 渲染失败 → 最小 MD 降级稿(含红线 36
  声明句),注入 renderer 时透传 stats 并采用其返回路径;
- 注入生效:grouper 收到 cfg/reports/packager、stats_fn 收到组对象、
  renderer 收到 A172 口径 stats;
- 兄弟缺失优雅降级:分类 / 统计 / 举报准备 / 渲染任一阶段抛错均只
  前置中文"注意:"提示,固定三步中文指引仍在尾部;
- 红线 36 源码断言:无 run_batch / executor / plan_ / auto_confirm,
  无 ``.attest(`` 代答调用;
- 保序:groups 摘要行保持分组结果顺序;
- telemetry:summary_agent.run 计数与 summary_agent.groups 仪表。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from netsentinel import telemetry
from netsentinel.agent import summary_agent as ns
from netsentinel.agent.summary_agent import (
    DISABLED_NEXT_STEPS,
    NEXT_STEPS,
    REDLINE_NOTE,
    SummaryAgent,
    SummaryOutcome,
)
from netsentinel.contracts import Config, SiteReport, Verdict
from netsentinel.decision.batch_review import BatchReview
from netsentinel.decision.review_queue import ReviewQueue

#: A110 声明文本合法样例(必须含"人工核实")。
VALID_TEXT = "我已逐站人工核实全部证据,同意批量举报"


# ---------------------------------------------------------------------------
# 公共 fake
# ---------------------------------------------------------------------------
@dataclass
class FakeGroup:
    """A104 CaseGroup 的鸭子替身(字段按契约同名同义)。"""

    name: str
    aliases: list[str] = field(default_factory=list)
    site_urls: list[str] = field(default_factory=list)
    agg_max: float = 0.0
    verdict: str = "nsfw"
    entry_ids: list[int] = field(default_factory=list)


class RecordingGrouper:
    """整体替换 A108 分类阶段的 fake:记录调用并返回固定分组结果。"""

    def __init__(
        self,
        groups: list[Any] | None = None,
        *,
        enqueued: int | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.groups = list(groups or [])
        self.enqueued = len(self.groups) if enqueued is None else enqueued
        self.exc = exc
        self.calls: list[dict[str, Any]] = []

    def __call__(self, cfg: Any, *, reports=None, packager=None) -> dict:
        self.calls.append(
            {"cfg": cfg, "reports": dict(reports or {}), "packager": packager}
        )
        if self.exc is not None:
            raise self.exc
        return {"groups": list(self.groups), "enqueued": self.enqueued, "skipped_clean": 0}


class RecordingStats:
    """替换 A117 group_stats.stats 的 fake:记录组对象并返回固定战况。"""

    def __init__(
        self,
        *,
        exc: Exception | None = None,
        result: dict | None = None,
    ) -> None:
        self.exc = exc
        self.result = result
        self.calls: list[list[Any]] = []

    def __call__(self, groups: Any) -> dict:
        self.calls.append(list(groups))
        if self.exc is not None:
            raise self.exc
        if self.result is not None:
            return dict(self.result)
        return {
            "group_count": len(groups),
            "verdict_dist": {"clean": 0, "suspect": 0, "nsfw": len(groups)},
        }


class FakeRenderer:
    """替换 A172 render_run_summary 的 fake:记录 stats 并真的写两个文件。"""

    def __init__(self, *, exc: Exception | None = None) -> None:
        self.exc = exc
        self.calls: list[dict[str, Any]] = []

    def __call__(self, stats: dict, out_base: str) -> tuple[str, str]:
        self.calls.append({"stats": dict(stats), "out_base": str(out_base)})
        if self.exc is not None:
            raise self.exc
        md = Path(str(out_base) + ".md")
        html = Path(str(out_base) + ".html")
        for path in (md, html):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fake-report", encoding="utf-8")
        return str(md), str(html)


class MarkerPackager:
    """透传给分类阶段的证据打包 fake(仅做身份标记)。"""

    def __call__(self, report: Any, cfg: Any) -> str:
        return "evidence/fake.zip"


def make_cfg(tmp_path: Path, **overrides: Any) -> Config:
    """指向 tmp_path 的测试配置(db 缺省指向不存在的库,零副作用)。"""
    kwargs: dict[str, Any] = {
        "data_dir": str(tmp_path / "data"),
        "db_path": str(tmp_path / "queue.db"),
    }
    kwargs.update(overrides)
    return Config(**kwargs)


def make_groups() -> list[FakeGroup]:
    """三个典型组:多站 nsfw / 单站 suspect / 单站 clean。"""
    return [
        FakeGroup(
            name="团伙A",
            aliases=["a.com", "mirror.a.com"],
            site_urls=["https://a.com/1", "https://a.com/2", "https://mirror.a.com/"],
            agg_max=0.95,
            verdict="nsfw",
        ),
        FakeGroup(
            name="团伙B",
            aliases=["b.com"],
            site_urls=["https://b.com/"],
            agg_max=0.70,
            verdict="suspect",
        ),
        FakeGroup(
            name="干净组",
            aliases=["c.com"],
            site_urls=["https://c.com/"],
            agg_max=0.10,
            verdict="clean",
        ),
    ]


def make_scan_result(n: int = 2, summary: dict | None = None) -> dict:
    """构造 A108 batch_scan 形状的扫描结果(报告用轻量对象占位)。"""
    reports = {f"https://s{i}.example.com/": object() for i in range(n)}
    base = {"total": n, "done": n, "failed": 0, "skipped": 0, "results": {}, "errors": []}
    if summary:
        base.update(summary)
    return {"reports": reports, "summary": base}


def seed_queue(
    db_path: str | Path,
    *,
    approved: list[tuple[str, str]] | None = None,
    pending: list[tuple[str, str]] | None = None,
    rejected: list[tuple[str, str]] | None = None,
    attested: list[str] | None = None,
) -> None:
    """在临时库里种入复核条目与声明(真实 A10/A110 链路,只写 tmp_path)。

    approved/pending/rejected 均为 ``(url, 组名)``;note 写 ``[组:名]`` 标记
    (与 A108 归组入列口径一致);attested 为已声明组名清单。
    """
    queue = ReviewQueue(db_path)
    for url, group in approved or []:
        eid = queue.add(
            SiteReport(site_url=url, verdict=Verdict.NSFW), "e.zip", note=f"[组:{group}]"
        )
        queue.approve(eid)
    for url, group in pending or []:
        queue.add(
            SiteReport(site_url=url, verdict=Verdict.NSFW), "e.zip", note=f"[组:{group}]"
        )
    for url, group in rejected or []:
        eid = queue.add(
            SiteReport(site_url=url, verdict=Verdict.NSFW), "e.zip", note=f"[组:{group}]"
        )
        queue.reject(eid)
    queue.close()
    if attested:
        review = BatchReview(db_path)
        for name in attested:
            review.attest(name, items=1, reviewer="张三", text=VALID_TEXT)
        review.close()


def run_agent(
    cfg: Config,
    *,
    scan_result: dict | None = None,
    grouper: Any = None,
    packager: Any = None,
    stats_fn: Any = None,
    renderer: Any = None,
) -> SummaryOutcome:
    """统一入口:跑一次结案代理(缺省注入最小 fake 集)。"""
    if scan_result is None:
        scan_result = make_scan_result()
    if grouper is None and getattr(cfg, "summary_agent_enabled", True):
        grouper = RecordingGrouper(make_groups())
    return SummaryAgent().run(
        scan_result, cfg, grouper=grouper, packager=packager,
        stats_fn=stats_fn, renderer=renderer,
    )


def block_module(monkeypatch, *module_names: str) -> None:
    """把 ns._lazy_import 对指定兄弟模块的调用钉死为"未就位"(其余放行)。

    用于确定性地走降级分支——不依赖兄弟模块在仓库中是否真实存在
    (并行开发期间 A163–A175 可能随时就位)。
    """
    real_import = ns._lazy_import
    blocked = tuple(module_names)

    def guarded_import(module_name: str, attr: str) -> Any:
        if module_name in blocked:
            raise RuntimeError(f"模块 {module_name} 未就位:测试屏蔽")
        return real_import(module_name, attr)

    monkeypatch.setattr(ns, "_lazy_import", guarded_import)


# ---------------------------------------------------------------------------
# 正常 outcome:全字段
# ---------------------------------------------------------------------------
class TestNormalOutcome:
    def test_all_fields_present_and_correct(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        grouper = RecordingGrouper(make_groups())
        renderer = FakeRenderer()
        outcome = run_agent(cfg, grouper=grouper, stats_fn=RecordingStats(), renderer=renderer)

        assert isinstance(outcome, SummaryOutcome)
        assert [row["name"] for row in outcome.groups] == ["团伙A", "团伙B", "干净组"]
        assert outcome.groups[0] == {
            "name": "团伙A", "sites": 2, "urls": 3, "verdict": "nsfw", "agg_max": 0.95,
        }
        assert outcome.report_md and outcome.report_html
        assert Path(outcome.report_md).is_file()
        assert Path(outcome.report_html).is_file()
        assert outcome.ready_count == 0  # 库文件不存在 → 零就绪
        assert outcome.attest_pending == []
        assert outcome.workers >= 1
        assert outcome.tier == "mid"
        assert outcome.next_steps == list(NEXT_STEPS)  # 无降级 → 恰好固定三步

    def test_groups_rows_preserve_input_order(self, tmp_path: Path) -> None:
        names = ["组4", "组2", "组9", "组1"]
        grouper = RecordingGrouper(
            [FakeGroup(name=n, aliases=[f"{n}.com"], site_urls=[f"https://{n}.com/"]) for n in names]
        )
        outcome = run_agent(make_cfg(tmp_path), grouper=grouper, renderer=FakeRenderer())
        assert [row["name"] for row in outcome.groups] == names

    def test_group_row_fields_and_host_dedup(self, tmp_path: Path) -> None:
        group = FakeGroup(
            name="大小写组",
            aliases=["A.COM", "a.com", "b.com"],  # 大小写与重复 → 去重后 2 站
            site_urls=["https://a.com/", "https://a.com/x", "https://b.com/"],
            agg_max=0.88,
            verdict="nsfw",
        )
        outcome = run_agent(
            make_cfg(tmp_path), grouper=RecordingGrouper([group]), renderer=FakeRenderer()
        )
        assert outcome.groups[0]["sites"] == 2
        assert outcome.groups[0]["urls"] == 3
        assert outcome.groups[0]["verdict"] == "nsfw"
        assert outcome.groups[0]["agg_max"] == 0.88

    def test_group_name_fallbacks(self, tmp_path: Path) -> None:
        no_name = FakeGroup(name="", aliases=[], site_urls=["https://x.com/"])
        nothing = FakeGroup(name="", aliases=[], site_urls=[])
        outcome = run_agent(
            make_cfg(tmp_path), grouper=RecordingGrouper([no_name, nothing]),
            renderer=FakeRenderer(),
        )
        assert outcome.groups[0]["name"] == "https://x.com/"
        assert outcome.groups[1]["name"] == "未命名组"

    def test_verdict_enum_normalized(self, tmp_path: Path) -> None:
        group = FakeGroup(name="枚举组", verdict=Verdict.SUSPECT)
        outcome = run_agent(
            make_cfg(tmp_path), grouper=RecordingGrouper([group]), renderer=FakeRenderer()
        )
        assert outcome.groups[0]["verdict"] == "suspect"

    def test_tier_reflected_from_cfg(self, tmp_path: Path) -> None:
        outcome = run_agent(
            make_cfg(tmp_path, concurrency_tier="high"), renderer=FakeRenderer()
        )
        assert outcome.tier == "high"


# ---------------------------------------------------------------------------
# 举报准备:ready_count / attest_pending(A110 语义只读统计)
# ---------------------------------------------------------------------------
class TestReadiness:
    def test_ready_count_and_attest_pending(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        seed_queue(
            cfg.db_path,
            approved=[("https://a.com/", "A"), ("https://a2.com/", "A"), ("https://b.com/", "B")],
            pending=[("https://b2.com/", "B")],   # 未确认:不计入任何统计
            rejected=[("https://c.com/", "C")],   # 已驳回:同上
            attested=["A"],
        )
        outcome = run_agent(cfg, renderer=FakeRenderer())
        assert outcome.ready_count == 2  # A 组两条:已声明 + 已批准
        assert outcome.attest_pending == ["B"]  # B 组已批准未声明

    def test_attest_pending_first_seen_order(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        seed_queue(
            cfg.db_path,
            approved=[
                ("https://b1.com/", "B"),
                ("https://a1.com/", "A"),
                ("https://c1.com/", "C"),
                ("https://b2.com/", "B"),  # B 组第二条:不重复列名
            ],
            attested=["A"],
        )
        outcome = run_agent(cfg, renderer=FakeRenderer())
        assert outcome.attest_pending == ["B", "C"]
        assert outcome.ready_count == 1

    def test_require_attestation_false_all_ready(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path, batch_require_attestation=False)
        seed_queue(
            cfg.db_path,
            approved=[("https://a.com/", "A"), ("https://b.com/", "B")],
        )
        outcome = run_agent(cfg, renderer=FakeRenderer())
        assert outcome.ready_count == 2  # 声明门关闭:全部 approved 视为就绪
        assert outcome.attest_pending == []

    def test_missing_db_zero_and_no_side_effect(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)  # queue.db 不存在
        outcome = run_agent(cfg, renderer=FakeRenderer())
        assert outcome.ready_count == 0
        assert outcome.attest_pending == []
        assert not Path(cfg.db_path).exists()  # 不激活任何东西:不建库
        assert not Path(cfg.db_path).parent.joinpath("queue.db-wal").exists()

    def test_all_attested_ready_pending_empty(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        seed_queue(
            cfg.db_path,
            approved=[("https://a.com/", "A"), ("https://b.com/", "B")],
            attested=["A", "B"],
        )
        outcome = run_agent(cfg, renderer=FakeRenderer())
        assert outcome.ready_count == 2
        assert outcome.attest_pending == []


# ---------------------------------------------------------------------------
# 配置关闭:最小 outcome
# ---------------------------------------------------------------------------
class TestDisabled:
    def test_minimal_outcome(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path, summary_agent_enabled=False)
        grouper = RecordingGrouper(make_groups())
        stats_fn = RecordingStats()
        renderer = FakeRenderer()
        outcome = run_agent(
            cfg, grouper=grouper, stats_fn=stats_fn, renderer=renderer
        )
        assert grouper.calls == []  # 关闭时不做任何汇总
        assert stats_fn.calls == []
        assert renderer.calls == []
        assert outcome.groups == []
        assert outcome.report_md == ""
        assert outcome.report_html == ""
        assert outcome.ready_count == 0
        assert outcome.attest_pending == []
        assert outcome.workers >= 1
        assert outcome.tier == "mid"
        assert outcome.next_steps == ["配置关闭"]
        assert not Path(cfg.data_dir).exists()  # 不落任何文件

    def test_disabled_reflects_tier(self, tmp_path: Path) -> None:
        cfg = make_cfg(
            tmp_path, summary_agent_enabled=False, concurrency_tier="high"
        )
        outcome = SummaryAgent().run(make_scan_result(), cfg)
        assert outcome.tier == "high"
        assert outcome.next_steps == list(DISABLED_NEXT_STEPS)


# ---------------------------------------------------------------------------
# 收官报告:A172 注入 / 缺席最小稿 / 渲染失败
# ---------------------------------------------------------------------------
class TestReport:
    def test_renderer_injection_receives_contract_stats(self, tmp_path: Path) -> None:
        renderer = FakeRenderer()
        run_agent(make_cfg(tmp_path), renderer=renderer)
        assert len(renderer.calls) == 1
        stats = renderer.calls[0]["stats"]
        assert set(stats) >= {
            "sites", "scanned", "failed", "verdict_dist", "groups", "tier",
            "workers", "cost_est", "budget_used", "telemetry_top",
            "attest_pending", "ready_count",
        }
        assert stats["sites"] == 2
        assert stats["tier"] == "mid"
        assert stats["workers"] >= 1

    def test_renderer_paths_adopted(self, tmp_path: Path) -> None:
        renderer = FakeRenderer()
        outcome = run_agent(make_cfg(tmp_path), renderer=renderer)
        assert outcome.report_md == renderer.calls[0]["out_base"] + ".md"
        assert outcome.report_html == renderer.calls[0]["out_base"] + ".html"

    def test_minimal_md_when_run_summary_absent(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # 钉死 A172 为未就位(不依赖并行开发期间该模块是否已在仓库中)
        block_module(monkeypatch, "netsentinel.report.run_summary")
        outcome = run_agent(make_cfg(tmp_path))  # 无 renderer
        assert outcome.report_html == ""
        assert outcome.report_md.endswith("run_summary.md")
        assert Path(outcome.report_md).is_file()
        text = Path(outcome.report_md).read_text(encoding="utf-8")
        assert "收官汇总" in text
        assert REDLINE_NOTE in text  # 红线 36 固定声明句
        assert outcome.next_steps[0].startswith("注意:")
        assert "未就位" in outcome.next_steps[0]
        assert outcome.next_steps[1:] == list(NEXT_STEPS)

    def test_minimal_md_contains_readiness_section(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        block_module(monkeypatch, "netsentinel.report.run_summary")
        cfg = make_cfg(tmp_path)
        seed_queue(
            cfg.db_path,
            approved=[("https://b.com/", "B"), ("https://a.com/", "A")],
            attested=["A"],
        )
        outcome = run_agent(cfg)  # 无 renderer → 最小稿
        text = Path(outcome.report_md).read_text(encoding="utf-8")
        assert "[组:B] 1 条" in text  # 已确认条目按组聚合
        assert "[组:A] 1 条" in text
        assert "已声明已批准 1 条" in text
        assert "B" in text  # 待声明组可见

    def test_renderer_failure_falls_back_minimal(self, tmp_path: Path) -> None:
        renderer = FakeRenderer(exc=RuntimeError("模块 netsentinel.report.run_summary 未就位:测试"))
        outcome = run_agent(make_cfg(tmp_path), renderer=renderer)
        assert outcome.report_html == ""
        assert Path(outcome.report_md).is_file()  # 最小稿兜底
        assert outcome.next_steps[0].startswith("注意:")
        assert "最小 Markdown" in outcome.next_steps[0]


# ---------------------------------------------------------------------------
# 注入生效:grouper / packager / stats_fn
# ---------------------------------------------------------------------------
class TestInjection:
    def test_grouper_receives_cfg_reports_packager(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path)
        grouper = RecordingGrouper(make_groups())
        packager = MarkerPackager()
        scan_result = make_scan_result(n=3)
        SummaryAgent().run(scan_result, cfg, grouper=grouper, packager=packager,
                           renderer=FakeRenderer())
        assert len(grouper.calls) == 1
        assert grouper.calls[0]["cfg"] is cfg
        assert grouper.calls[0]["reports"] == scan_result["reports"]
        assert grouper.calls[0]["packager"] is packager  # 原对象透传

    def test_stats_fn_receives_group_objects(self, tmp_path: Path) -> None:
        groups = make_groups()
        stats_fn = RecordingStats(
            result={"group_count": 3, "verdict_dist": {"clean": 1, "suspect": 1, "nsfw": 1}}
        )
        renderer = FakeRenderer()
        outcome = run_agent(
            make_cfg(tmp_path), grouper=RecordingGrouper(groups),
            stats_fn=stats_fn, renderer=renderer,
        )
        assert stats_fn.calls[0] == groups  # 组对象原样(保序)
        assert renderer.calls[0]["stats"]["verdict_dist"] == {
            "clean": 1, "suspect": 1, "nsfw": 1,
        }
        assert len(outcome.groups) == 3

    def test_scan_summary_mapped_to_render_stats(self, tmp_path: Path) -> None:
        renderer = FakeRenderer()
        scan_result = make_scan_result(n=2, summary={"scanned": 5, "failed": 2})
        SummaryAgent().run(scan_result, make_cfg(tmp_path),
                           grouper=RecordingGrouper([]), renderer=renderer)
        stats = renderer.calls[0]["stats"]
        assert stats["sites"] == 2  # 捕获报告数
        assert stats["scanned"] == 5
        assert stats["failed"] == 2

    def test_scan_summary_done_fallback_for_scanned(self, tmp_path: Path) -> None:
        renderer = FakeRenderer()
        scan_result = make_scan_result(n=2, summary={"done": 7, "failed": 1})
        SummaryAgent().run(scan_result, make_cfg(tmp_path),
                           grouper=RecordingGrouper([]), renderer=renderer)
        stats = renderer.calls[0]["stats"]
        assert stats["scanned"] == 7  # pool 口径 done → scanned
        assert stats["failed"] == 1

    def test_empty_scan_result_tolerated(self, tmp_path: Path) -> None:
        grouper = RecordingGrouper([])
        renderer = FakeRenderer()
        outcome = SummaryAgent().run({}, make_cfg(tmp_path), grouper=grouper, renderer=renderer)
        assert grouper.calls[0]["reports"] == {}
        assert renderer.calls[0]["stats"]["sites"] == 0
        assert renderer.calls[0]["stats"]["scanned"] == 0
        assert outcome.groups == []

    def test_none_scan_result_tolerated(self, tmp_path: Path) -> None:
        grouper = RecordingGrouper([])
        outcome = SummaryAgent().run(None, make_cfg(tmp_path), grouper=grouper,
                                     renderer=FakeRenderer())  # type: ignore[arg-type]
        assert grouper.calls[0]["reports"] == {}
        assert outcome.groups == []


# ---------------------------------------------------------------------------
# 兄弟缺失优雅降级(中文 next_steps)
# ---------------------------------------------------------------------------
class TestGracefulDegradation:
    def test_grouper_failure_degrades_with_chinese_note(self, tmp_path: Path) -> None:
        grouper = RecordingGrouper(exc=RuntimeError("模块 netsentinel.intel.case_group 未就位:No module"))
        renderer = FakeRenderer()
        outcome = run_agent(make_cfg(tmp_path), grouper=grouper, renderer=renderer)
        assert outcome.groups == []
        assert outcome.next_steps[0].startswith("注意:")
        assert "分类" in outcome.next_steps[0]
        assert outcome.next_steps[-3:] == list(NEXT_STEPS)  # 固定三步仍在尾部
        assert renderer.calls[0]["stats"]["groups"] == []

    def test_stats_failure_degrades(self, tmp_path: Path) -> None:
        renderer = FakeRenderer()
        outcome = run_agent(
            make_cfg(tmp_path),
            stats_fn=RecordingStats(exc=RuntimeError("模块 netsentinel.intel.group_stats 未就位")),
            renderer=renderer,
        )
        assert len(outcome.groups) == 3  # 分组结果不受统计降级影响
        assert renderer.calls[0]["stats"]["verdict_dist"] == {}
        assert outcome.next_steps[0].startswith("注意:")
        assert "A117" in outcome.next_steps[0]

    def test_all_siblings_missing_full_degradation(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """全部兄弟模块缺失:outcome 仍产出,四个降级提示 + 固定三步。"""
        block_module(
            monkeypatch,
            "netsentinel.ops.batch_scan",
            "netsentinel.intel.group_stats",
            "netsentinel.decision.review_queue",
            "netsentinel.decision.batch_review",
            "netsentinel.report.run_summary",
        )
        cfg = make_cfg(tmp_path)
        seed_queue(cfg.db_path, approved=[("https://b.com/", "B")])  # 触发举报准备阶段
        outcome = SummaryAgent().run(make_scan_result(), cfg)
        assert isinstance(outcome, SummaryOutcome)
        assert outcome.groups == []
        assert outcome.ready_count == 0
        assert outcome.attest_pending == []
        assert outcome.report_html == ""
        assert Path(outcome.report_md).is_file()  # 内置最小稿仍落盘
        notes = outcome.next_steps[:-3]
        assert len(notes) == 4  # 分类 / 统计 / 举报准备 / 渲染
        assert all(note.startswith("注意:") for note in notes)
        assert outcome.next_steps[-3:] == list(NEXT_STEPS)

    def test_notes_are_chinese(self, tmp_path: Path) -> None:
        grouper = RecordingGrouper(exc=RuntimeError("boom"))
        outcome = run_agent(make_cfg(tmp_path), grouper=grouper, renderer=FakeRenderer())
        merged = "".join(outcome.next_steps)
        assert "注意" in merged and "降级" in merged
        assert all(step for step in outcome.next_steps)


# ---------------------------------------------------------------------------
# 红线 36:零提交路径(源码断言)
# ---------------------------------------------------------------------------
class TestRedline36Source:
    def test_no_submission_paths_in_source(self) -> None:
        src = Path(ns.__file__).read_text(encoding="utf-8")
        for forbidden in ("run_batch", "executor", "plan_", "auto_confirm"):
            assert forbidden not in src, f"红线 36 违例:源码出现 {forbidden!r}"
        assert not re.search(r"\.attest\s*\(", src), "红线 36 违例:源码存在声明代答调用"

    def test_no_submit_chain_imports_in_source(self) -> None:
        src = Path(ns.__file__).read_text(encoding="utf-8")
        assert "netsentinel.submit" not in src  # 不 import 举报提交链
        assert "sequential_report" not in src   # 不编排 A174 顺序举报代理


# ---------------------------------------------------------------------------
# workers / tier(契约 §1 公式,本地兜底路径钉死)
# ---------------------------------------------------------------------------
class TestWorkers:
    def test_tier_formula_local_fallback(self, tmp_path: Path, monkeypatch) -> None:
        block_module(monkeypatch, "netsentinel.ops.concurrency")
        monkeypatch.setattr(os, "cpu_count", lambda: 8)

        def workers_of(**overrides: Any) -> int:
            cfg = make_cfg(tmp_path, summary_agent_enabled=False, **overrides)
            return SummaryAgent().run({}, cfg).workers

        assert workers_of(concurrency_tier="low") == 2    # max(1, 8//4)
        assert workers_of() == 4                          # mid: max(1, 8//2)
        assert workers_of(concurrency_tier="high") == 7   # 8 - cpu_reserve(1)
        assert workers_of(concurrency_tier="high", cpu_reserve=0) == 8  # 全核压榨

    def test_high_never_exceeds_cores(self, tmp_path: Path, monkeypatch) -> None:
        """红线 37:任意档位 workers ≤ 核数(reserve≥0 钳制)。"""
        block_module(monkeypatch, "netsentinel.ops.concurrency")
        monkeypatch.setattr(os, "cpu_count", lambda: 8)
        for tier in ("low", "mid", "high"):
            cfg = make_cfg(
                tmp_path, summary_agent_enabled=False,
                concurrency_tier=tier, cpu_reserve=0,
            )
            assert SummaryAgent().run({}, cfg).workers <= 8

    def test_single_core_minimum_one_worker(self, tmp_path: Path, monkeypatch) -> None:
        block_module(monkeypatch, "netsentinel.ops.concurrency")
        monkeypatch.setattr(os, "cpu_count", lambda: 1)
        cfg = make_cfg(tmp_path, summary_agent_enabled=False, concurrency_tier="high")
        assert SummaryAgent().run({}, cfg).workers == 1  # max(1, 1-0)


# ---------------------------------------------------------------------------
# telemetry
# ---------------------------------------------------------------------------
class TestTelemetry:
    def test_inc_run_and_gauge_groups(self, tmp_path: Path) -> None:
        telemetry.reset()
        run_agent(make_cfg(tmp_path), renderer=FakeRenderer())
        snap = telemetry.snapshot()
        assert snap["counters"].get("summary_agent.run") == 1
        assert snap["gauges"].get("summary_agent.groups") == 3

    def test_gauge_groups_zero_when_disabled(self, tmp_path: Path) -> None:
        telemetry.reset()
        cfg = make_cfg(tmp_path, summary_agent_enabled=False)
        SummaryAgent().run({}, cfg)
        snap = telemetry.snapshot()
        assert snap["counters"].get("summary_agent.run") == 1  # run 仍计数
        assert snap["gauges"].get("summary_agent.groups") == 0

    def test_gauge_groups_zero_on_grouper_failure(self, tmp_path: Path) -> None:
        telemetry.reset()
        run_agent(
            make_cfg(tmp_path),
            grouper=RecordingGrouper(exc=RuntimeError("未就位")),
            renderer=FakeRenderer(),
        )
        assert telemetry.snapshot()["gauges"].get("summary_agent.groups") == 0


# ---------------------------------------------------------------------------
# next_steps 固定中文序列
# ---------------------------------------------------------------------------
class TestNextSteps:
    def test_fixed_sequence_content(self) -> None:
        assert len(NEXT_STEPS) == 3
        assert "batch_tui" in NEXT_STEPS[0] and "声明" in NEXT_STEPS[0]
        assert "finishflow --report --resume" in NEXT_STEPS[1]
        assert "人工门" in NEXT_STEPS[2]
        assert DISABLED_NEXT_STEPS == ("配置关闭",)

    def test_outcome_next_steps_ends_with_fixed_sequence(self, tmp_path: Path) -> None:
        grouper = RecordingGrouper(exc=RuntimeError("boom"))
        outcome = run_agent(make_cfg(tmp_path), grouper=grouper, renderer=FakeRenderer())
        assert outcome.next_steps[-3:] == list(NEXT_STEPS)
        assert all(isinstance(step, str) and step for step in outcome.next_steps)
