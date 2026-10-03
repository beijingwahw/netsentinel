# -*- coding: utf-8 -*-
"""A169 收官 CLI(finishflow)单元测试:全部离线、全注入。

覆盖:finish 全链 fake(tier_once → io_workers → 扫描 → 结案汇总器 → 中文
收官汇总 capsys → outcome dict 结构)、workers 透传(注入 scan 关键字 /
缺省经 pool_runner 包装透传给 run_pool)、红线 35(cfg 原对象只读透传、
fetch_delay_s 不变、--tier 覆盖不改礼貌参数)、执行顺序、空输入、
scan 失败计数、outcome 形态兼容(dataclass/映射);run_report 干跑默认与
显式 dry_run=False、items/runner 注入、--resume 续批合并(portal 补齐)、
批次已完新建、空清单零值、兄弟缺失中文 RuntimeError、就绪清单缺省
(ready_entries 惰性)、批次不存在 ValueError;CLI main 的互斥校验 /
模式分派 / --tier 覆盖与非法值 / 空清单短路 / 错误码 2 与 3 /
python -m 子进程入口;V12 收官 trace 树落盘(真实内核 span 树落盘 /
无 trace_id 零行为 / 导出异常只告警计数不中断 / 模块缺席整体跳过 /
多站点同站合并目录);V13 批次成本归集收官步(vlm_cost.jsonl 存在时
runs/<批次>/cost.jsonl 永远写 + 聚合行结构 / 账本不存在零行为 / 空账本
照写 / 伪 pyarrow 验证 parquet 写入调用 / parquet 失败降级 jsonl /
归集整体失败绝不中断 / run_id 缺省生成与目录名安全化);A232 批预算
哨兵检查(cfg 附加属性 batch_cost_budget:getattr 缺省 None 零行为 /
超限 result 标记 + 中文醒目告警 + finishflow.budget.exceeded 计数 /
未超限零噪音 / 无账本跳过 / 非法配置与模块缺席只告警绝不中断 /
超限后已产出文件与举报批次库状态原样——哨兵不是执行器)。

零网络、零真实门户、零真实提交;兄弟全 fake 或 monkeypatch sys.modules。
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import re
import subprocess
import sys
import types
from typing import Any

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.finishflow import finish, main, run_report

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fake 组件(离线注入)
# ---------------------------------------------------------------------------
class FakeScan:
    """批量扫描替身:记录 (urls, cfg, workers),返回预置 reports/summary。"""

    def __init__(self, reports=None, errors=None, done=None, failed=None,
                 skipped=0) -> None:
        self.reports = dict(reports or {})
        self.errors = list(errors or [])
        self.done = done
        self.failed = failed
        self.skipped = int(skipped)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, urls, cfg, **kwargs):
        self.calls.append(
            {"urls": list(urls), "cfg": cfg, "kwargs": dict(kwargs)}
        )
        reports = {u: self.reports.get(u, f"report:{u}") for u in urls}
        summary: dict[str, Any] = {
            "errors": list(self.errors),
            "skipped": self.skipped,
        }
        if self.done is not None:
            summary["done"] = self.done
        if self.failed is not None:
            summary["failed"] = self.failed
        return {"reports": reports, "summary": summary}


@dataclasses.dataclass
class FakeOutcome:
    """A168 SummaryOutcome 鸭子替身(契约字段全量)。"""

    groups: list[str]
    report_md: str = "out/run_summary.md"
    report_html: str = "out/run_summary.html"
    ready_count: int = 2
    attest_pending: int = 1
    workers: int = 4
    tier: str = "high"
    next_steps: list[str] = dataclasses.field(
        default_factory=lambda: ["① batch_tui 逐组声明", "② finishflow --report"]
    )


class FakeSummaryAgent:
    """结案汇总器替身:记录 (scan_result, cfg),返回预置 outcome。"""

    def __init__(self, outcome=None) -> None:
        self.outcome = outcome if outcome is not None else FakeOutcome(
            groups=["g1", "g2"]
        )
        self.calls: list[dict[str, Any]] = []

    def run(self, scan_result, cfg):
        self.calls.append({"scan_result": scan_result, "cfg": cfg})
        return self.outcome


class FakeSequentialRunner:
    """A174 SequentialReportAgent 替身:记录 items/kwargs,返回预置摘要。"""

    def __init__(self, result=None) -> None:
        self.result = dict(
            result
            or {
                "submitted": 1,
                "failed": 0,
                "total": 2,
                "rate_limited": False,
                "paused": False,
                "results": [],
                "report_path": "out/batch_report.md",
            }
        )
        self.calls: list[dict[str, Any]] = []

    def run(self, items, cfg, **kwargs):
        self.calls.append(
            {"items": [dict(i) for i in items], "cfg": cfg, "kwargs": dict(kwargs)}
        )
        return dict(self.result)


class FakeReady:
    """A110 ready_entries 替身:记录 cfg,返回预置就绪条目。"""

    def __init__(self, items=None) -> None:
        self.items = [dict(i) for i in (items or [])]
        self.calls: list[Any] = []

    def __call__(self, cfg, **kwargs):
        self.calls.append(cfg)
        return [dict(i) for i in self.items]


def _install_module(monkeypatch, name: str, **attrs) -> types.ModuleType:
    """把带属性的假模块塞进 sys.modules(惰性导入的兄弟注入点)。"""
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def _cfg(tmp_path, **kw) -> Config:
    """离线 Config:data_dir/db_path 全指向 tmp,避免触碰仓库 data/。"""
    return Config(
        data_dir=str(tmp_path / "data"),
        db_path=str(tmp_path / "q.db"),
        audit_path=str(tmp_path / "audit.jsonl"),
        **kw,
    )


def _finish_deps(monkeypatch, *, workers: int = 3) -> list[str]:
    """装上 finish 的惰性兄弟:A165 tier_once + A164 io_workers(记录顺序)。"""
    order: list[str] = []

    def tier_once(cfg):
        order.append("tier_once")

    def io_workers(cfg):
        order.append("io_workers")
        return workers

    _install_module(
        monkeypatch, "netsentinel.ops.tier_state", tier_once=tier_once
    )
    _install_module(
        monkeypatch, "netsentinel.ops.concurrency", io_workers=io_workers
    )
    return order


def _write_txt(tmp_path, lines: list[str], name: str = "leads.txt") -> str:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _ready(entry_id, group, **extra) -> dict[str, Any]:
    """标准就绪条目(A110 五键)。"""
    item = {
        "entry_id": entry_id,
        "group_name": group,
        "site_url": f"https://{group}/",
        "evidence_zip": f"out/e{entry_id}.zip",
        "portal": "12377",
    }
    item.update(extra)
    return item


# ---------------------------------------------------------------------------
# finish:全链 fake
# ---------------------------------------------------------------------------
class TestFinish:
    def test_full_chain_fake_returns_outcome_dict(self, tmp_path, monkeypatch):
        """全链 fake:outcome 字段 + 扫描计数齐备,scan/summary 各调一次。"""
        _finish_deps(monkeypatch, workers=3)
        scan = FakeScan()
        agent = FakeSummaryAgent()
        cfg = _cfg(tmp_path)
        result = finish(["https://a.com/", "https://b.com/"], cfg,
                        scan=scan, summary=agent)
        assert result["sites"] == 2
        assert result["scanned"] == 2
        assert result["failed"] == 0
        assert result["groups"] == 2
        assert result["attest_pending"] == 1
        assert result["ready_count"] == 2
        assert result["tier"] == "high"          # outcome 的 tier 优先
        assert result["workers"] == 4            # outcome 的 workers 优先
        assert result["report_md"] == "out/run_summary.md"
        assert result["next_steps"] == FakeOutcome(groups=[]).next_steps
        # scan:收到全部 URL + workers(io_workers 结果);cfg 原对象透传
        assert len(scan.calls) == 1
        assert scan.calls[0]["urls"] == ["https://a.com/", "https://b.com/"]
        assert scan.calls[0]["kwargs"]["workers"] == 3
        assert scan.calls[0]["cfg"] is cfg
        # summary:收到 scan_result 与同一 cfg
        assert len(agent.calls) == 1
        assert set(agent.calls[0]["scan_result"]["reports"]) == {
            "https://a.com/", "https://b.com/"
        }
        assert agent.calls[0]["cfg"] is cfg

    def test_summary_printed_chinese(self, tmp_path, monkeypatch, capsys):
        """中文收官汇总八要素 + 红线 35/36 提示 + 缺省下一步指引。"""
        _finish_deps(monkeypatch)
        finish(["https://a.com/"], _cfg(tmp_path), scan=FakeScan(),
                summary=FakeSummaryAgent(
                    FakeOutcome(groups=["g1"], next_steps=[])
                ))
        out = capsys.readouterr().out
        for token in ("站点", "扫描成功", "扫描失败", "案件组数", "待声明组",
                      "就绪条目", "并发档位", "worker"):
            assert token in out, f"收官汇总缺少字段:{token}"
        assert "红线 35" in out and "红线 36" in out
        assert "下一步" in out
        assert "python -m netsentinel.cli.batch_tui" in out
        assert "python -m netsentinel.finishflow --report" in out

    def test_workers_passthrough_to_injected_scan(self, tmp_path, monkeypatch):
        """A164 io_workers 的结果以 workers= 关键字透传给注入 scan。"""
        _finish_deps(monkeypatch, workers=5)
        scan = FakeScan()
        finish(["https://a.com/"], _cfg(tmp_path), scan=scan,
                summary=FakeSummaryAgent())
        assert scan.calls[0]["kwargs"]["workers"] == 5

    def test_politeness_passthrough_redline35(self, tmp_path, monkeypatch):
        """红线 35:cfg 只读透传同一对象,fetch_delay_s 等礼貌参数不变。"""
        _finish_deps(monkeypatch)
        cfg = _cfg(tmp_path, fetch_delay_s=2.5, concurrency_tier="low")
        scan = FakeScan()
        agent = FakeSummaryAgent()
        finish(["https://a.com/"], cfg, scan=scan, summary=agent)
        assert scan.calls[0]["cfg"] is cfg
        assert agent.calls[0]["cfg"] is cfg
        assert cfg.fetch_delay_s == 2.5
        assert cfg.concurrency_tier == "low"  # finish 不改档位,只读

    def test_default_path_wires_workers_into_run_pool(
        self, tmp_path, monkeypatch
    ):
        """缺省路径:真 batch_scan + 假 ops.pool → run_pool 收到 workers=N。"""
        _finish_deps(monkeypatch, workers=3)
        pool_calls: list[dict[str, Any]] = []

        def fake_run_pool(cfg_, items, **kwargs):
            pool_calls.append({"items": list(items), "kwargs": dict(kwargs)})
            n = len(items)
            return {"total": n, "done": n, "failed": 0, "skipped": 0,
                    "results": {}, "errors": []}

        _install_module(
            monkeypatch, "netsentinel.ops.pool", run_pool=fake_run_pool
        )
        result = finish(["https://a.com/"], _cfg(tmp_path),
                        summary=FakeSummaryAgent())
        assert len(pool_calls) == 1
        assert pool_calls[0]["items"] == ["https://a.com/"]
        assert pool_calls[0]["kwargs"]["workers"] == 3
        assert callable(pool_calls[0]["kwargs"]["run_scan"])
        assert result["scanned"] == 1 and result["sites"] == 1

    def test_execution_order_tier_workers_scan_summary(
        self, tmp_path, monkeypatch
    ):
        """顺序:tier_once → io_workers → scan → summary(档位先于并发)。"""
        order = _finish_deps(monkeypatch)

        def scan(urls, cfg, **kwargs):
            order.append("scan")
            return {"reports": {u: f"r:{u}" for u in urls}, "summary": {}}

        class OrderedSummary:
            def run(self, scan_result, cfg):
                order.append("summary")
                return {"groups": 1}

        finish(["https://a.com/"], _cfg(tmp_path), scan=scan,
                summary=OrderedSummary())
        assert order == ["tier_once", "io_workers", "scan", "summary"]

    def test_sibling_tier_state_missing_chinese(self, tmp_path, monkeypatch):
        """A165 缺失 → 中文 RuntimeError(模块未就位)。"""
        monkeypatch.setitem(sys.modules, "netsentinel.ops.tier_state", None)
        with pytest.raises(RuntimeError, match="未就位"):
            finish(["https://a.com/"], _cfg(tmp_path), scan=FakeScan(),
                    summary=FakeSummaryAgent())

    def test_sibling_concurrency_missing_chinese(self, tmp_path, monkeypatch):
        """A164 缺失 → 中文 RuntimeError。"""
        _install_module(monkeypatch, "netsentinel.ops.tier_state", tier_once=lambda cfg: None)
        monkeypatch.setitem(sys.modules, "netsentinel.ops.concurrency", None)
        with pytest.raises(RuntimeError, match="未就位"):
            finish(["https://a.com/"], _cfg(tmp_path), scan=FakeScan(),
                    summary=FakeSummaryAgent())

    def test_sibling_summary_agent_missing_chinese(self, tmp_path, monkeypatch):
        """A168 缺席(未注入 summary)→ 中文 RuntimeError。"""
        _finish_deps(monkeypatch)
        monkeypatch.setitem(sys.modules, "netsentinel.agent.summary_agent", None)
        with pytest.raises(RuntimeError, match="未就位"):
            finish(["https://a.com/"], _cfg(tmp_path), scan=FakeScan())

    def test_sibling_batch_scan_missing_chinese(self, tmp_path, monkeypatch):
        """A108 缺失(未注入 scan)→ 中文 RuntimeError。"""
        _finish_deps(monkeypatch)
        monkeypatch.setitem(sys.modules, "netsentinel.ops.batch_scan", None)
        with pytest.raises(RuntimeError, match="未就位"):
            finish(["https://a.com/"], _cfg(tmp_path),
                    summary=FakeSummaryAgent())

    def test_empty_urls_zero_counts(self, tmp_path, monkeypatch, capsys):
        """空 URL 列表:全零 outcome,不异常。"""
        _finish_deps(monkeypatch)
        scan = FakeScan()
        result = finish([], _cfg(tmp_path), scan=scan,
                        summary=FakeSummaryAgent(FakeOutcome(groups=[])))
        assert result["sites"] == 0
        assert result["scanned"] == 0 and result["failed"] == 0
        assert result["groups"] == 0
        assert scan.calls[0]["urls"] == []

    def test_outcome_mapping_fallback_and_default_hints(
        self, tmp_path, monkeypatch, capsys
    ):
        """outcome 为普通映射且缺 tier/workers → 回退 cfg 档位与 io_workers;
        无 next_steps → 打印缺省两步指引。"""
        _finish_deps(monkeypatch, workers=2)
        result = finish(["https://a.com/"], _cfg(tmp_path), scan=FakeScan(),
                        summary=FakeSummaryAgent({"groups": 3, "attest_pending": 2,
                                                  "ready_count": 1}))
        assert result["groups"] == 3
        assert result["tier"] == "mid"      # cfg.concurrency_tier 默认值
        assert result["workers"] == 2       # 回退 io_workers 结果
        assert "next_steps" not in result
        out = capsys.readouterr().out
        assert "python -m netsentinel.cli.batch_tui" in out
        assert "python -m netsentinel.finishflow --report" in out

    def test_real_tier_state_and_concurrency_wiring(self, tmp_path):
        """真 A165/A164(已落地)对接:tier_once(cfg)/io_workers(cfg) 签名吻合,
        workers 经关键字到达注入 scan(≥1,且 cfg 只读不被改写)。"""
        cfg = _cfg(tmp_path)
        tier_before = cfg.concurrency_tier
        scan = FakeScan()
        finish(["https://a.com/"], cfg, scan=scan, summary=FakeSummaryAgent())
        workers = scan.calls[0]["kwargs"]["workers"]
        assert isinstance(workers, int) and workers >= 1
        assert cfg.concurrency_tier == tier_before  # tier_once 不改写 cfg

    def test_scan_failed_counted_from_errors(self, tmp_path, monkeypatch, capsys):
        """pool 摘要缺 failed 键 → 回退 errors 长度;失败数入表。"""
        _finish_deps(monkeypatch)
        scan = FakeScan(errors=[("https://a.com/", "连接超时")])
        result = finish(["https://a.com/"], _cfg(tmp_path), scan=scan,
                        summary=FakeSummaryAgent())
        assert result["failed"] == 1
        assert "扫描失败: 1" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# run_report:举报准备(默认干跑)
# ---------------------------------------------------------------------------
class TestRunReport:
    def test_dry_run_default_runner_receives_items_and_state(
        self, tmp_path, capsys
    ):
        """干跑默认:runner 收到 items + state(绑定新批次),报告路径带回。"""
        cfg = _cfg(tmp_path)
        runner = FakeSequentialRunner()
        report = run_report(cfg, items=[_ready(101, "g1"), _ready(102, "g2")],
                            runner=runner)
        assert len(runner.calls) == 1
        call = runner.calls[0]
        assert [i["entry_id"] for i in call["items"]] == [101, 102]
        assert call["cfg"] is cfg
        assert call["kwargs"]["dry_run"] is True   # cfg.dry_run_default 默认 True
        state = call["kwargs"]["state"]
        assert state.batch_id == 1                 # 新建批次 1
        assert callable(state.mark) and callable(state.summary)
        assert report["report_path"] == "out/batch_report.md"
        assert report["result"]["batch_id"] == 1
        assert "新建批次" in capsys.readouterr().out
        # 批次确实落账(A113 真库,tmp db)
        from netsentinel.submit.batch_state import BatchState

        st = BatchState(cfg.db_path)
        try:
            assert st.summary(1)["total"] == 2
        finally:
            st.close()

    def test_explicit_dry_run_false_real_mode(self, tmp_path, capsys):
        """显式 dry_run=False:runner 收到 False,打印真实执行 + 红线 36。"""
        runner = FakeSequentialRunner()
        run_report(_cfg(tmp_path), items=[_ready(1, "g1")], runner=runner,
                   dry_run=False)
        assert runner.calls[0]["kwargs"]["dry_run"] is False
        out = capsys.readouterr().out
        assert "真实执行模式" in out
        assert "红线 36" in out

    def test_dry_run_none_follows_cfg_default(self, tmp_path):
        """dry_run=None → 取 cfg.dry_run_default(False 时即真实模式)。"""
        runner = FakeSequentialRunner()
        run_report(_cfg(tmp_path, dry_run_default=False),
                   items=[_ready(1, "g1")], runner=runner)
        assert runner.calls[0]["kwargs"]["dry_run"] is False

    def test_resume_unfinished_merges_portal(self, tmp_path, capsys):
        """--resume 续批:未完条目沿用批次,就绪清单补 portal/执行细节。"""
        from netsentinel.submit.batch_state import BatchState

        cfg = _cfg(tmp_path)
        st = BatchState(cfg.db_path)
        try:
            bid = st.new_batch(
                [{"entry_id": 1, "group_name": "g1"},
                 {"entry_id": 2, "group_name": "g2"}]
            )
        finally:
            st.close()
        runner = FakeSequentialRunner()
        report = run_report(
            cfg, items=[_ready(1, "g1", portal="shdf", site_url="https://g1/x")],
            runner=runner, batch_id=bid,
        )
        items = runner.calls[0]["items"]
        assert [i["entry_id"] for i in items] == [1, 2]
        by_id = {i["entry_id"]: i for i in items}
        assert by_id[1]["portal"] == "shdf"       # 就绪条目补齐覆盖
        assert by_id[1]["site_url"] == "https://g1/x"
        assert by_id[2]["portal"] == "12377"      # 双缺 → 缺省门户
        assert report["result"]["batch_id"] == bid
        assert "续批批次" in capsys.readouterr().out

    def test_resume_completed_batch_creates_new(self, tmp_path, capsys):
        """批次已完(无未完条目)→ 按就绪清单新建批次再执行。"""
        from netsentinel.submit.batch_state import BatchState

        cfg = _cfg(tmp_path)
        st = BatchState(cfg.db_path)
        try:
            bid = st.new_batch([{"entry_id": 9, "group_name": "old"}])
            st.bound(bid).mark(9, "submitted")    # 全部终态 → resume 为空
        finally:
            st.close()
        runner = FakeSequentialRunner()
        report = run_report(cfg, items=[_ready(11, "g1")], runner=runner,
                            batch_id=bid)
        assert [i["entry_id"] for i in runner.calls[0]["items"]] == [11]
        assert report["result"]["batch_id"] == bid + 1   # 新批次
        assert "新建批次" in capsys.readouterr().out

    def test_idle_empty_items_zero_result(self, tmp_path, capsys):
        """就绪清单为空:零值摘要、不建批、不触碰 runner。"""
        runner = FakeSequentialRunner()
        report = run_report(_cfg(tmp_path), items=[], runner=runner)
        assert runner.calls == []
        assert report["report_path"] is None
        assert report["result"]["submitted"] == 0
        assert report["result"]["note"]
        out = capsys.readouterr().out
        assert "无待举报条目" in out
        from netsentinel.submit.batch_state import BatchState

        st = BatchState(str(tmp_path / "q.db"))
        try:
            assert st.list_batches() == []       # 未新建任何批次
        finally:
            st.close()

    def test_runner_module_missing_chinese(self, tmp_path, monkeypatch):
        """A174 缺失(未注入 runner)且有条目 → 中文 RuntimeError。"""
        monkeypatch.setitem(
            sys.modules, "netsentinel.agent.sequential_report", None
        )
        with pytest.raises(RuntimeError, match="未就位"):
            run_report(_cfg(tmp_path), items=[_ready(1, "g1")])

    def test_ready_entries_used_when_items_none(self, tmp_path, monkeypatch):
        """items=None → 惰性 A110 ready_entries(注入假模块取条目)。"""
        ready = FakeReady(items=[_ready(5, "g1")])
        _install_module(
            monkeypatch, "netsentinel.decision.batch_review",
            ready_entries=ready,
        )
        runner = FakeSequentialRunner()
        run_report(_cfg(tmp_path), runner=runner)
        assert len(ready.calls) == 1
        assert [i["entry_id"] for i in runner.calls[0]["items"]] == [5]

    def test_runner_plain_callable(self, tmp_path):
        """runner 为普通可调用(无 .run)→ 直接调用同样成立。"""
        calls: list[dict[str, Any]] = []

        def runner(items, cfg, **kwargs):
            calls.append({"items": list(items), "kwargs": dict(kwargs)})
            return {"submitted": 0, "failed": 0, "report_path": "out/x.md"}

        report = run_report(_cfg(tmp_path), items=[_ready(1, "g1")],
                            runner=runner)
        assert calls[0]["kwargs"]["dry_run"] is True
        assert report["report_path"] == "out/x.md"

    def test_resume_nonexistent_batch_valueerror(self, tmp_path):
        """续批批次不存在 → A113 中文 ValueError 原样上抛。"""
        with pytest.raises(ValueError, match="批次"):
            run_report(_cfg(tmp_path), items=[_ready(1, "g1")],
                       runner=FakeSequentialRunner(), batch_id=999)


# ---------------------------------------------------------------------------
# CLI main:互斥 / 分派 / --tier / 错误码
# ---------------------------------------------------------------------------
class TestMain:
    def test_input_and_report_conflict_returns_2(self, capsys):
        """--input 与 --report 互斥 → 返回 2,中文提示。"""
        assert main(["--input", "x.txt", "--report"]) == 2
        assert "互斥" in capsys.readouterr().out

    def test_no_mode_returns_2(self, capsys):
        """既无 --input 也无 --report → 返回 2。"""
        assert main([]) == 2
        assert "需要 --input" in capsys.readouterr().out

    def test_exec_without_report_returns_2(self, capsys):
        """--exec 仅 --report 模式有效 → 返回 2。"""
        assert main(["--exec"]) == 2
        assert "--exec 仅在 --report" in capsys.readouterr().out

    def test_resume_without_report_returns_2(self, capsys):
        """--resume 仅 --report 模式有效 → 返回 2。"""
        assert main(["--resume", "3"]) == 2
        assert "--resume 仅在 --report" in capsys.readouterr().out

    def test_dry_run_exec_mutex_returns_2(self, capsys):
        """--dry-run 与 --exec 互斥 → 返回 2。"""
        assert main(["--report", "--dry-run", "--exec"]) == 2
        assert "互斥" in capsys.readouterr().out

    def test_report_dispatch_dry_run_default(self, tmp_path, monkeypatch):
        """--report 默认干跑:run_report(batch_id=None, dry_run=True)。"""
        seen: list[dict[str, Any]] = []

        def fake_run_report(cfg, **kwargs):
            seen.append(kwargs)
            return {"result": {}, "report_path": None}

        monkeypatch.setattr("netsentinel.finishflow.run_report", fake_run_report)
        assert main(["--report"]) == 0
        assert seen[0]["batch_id"] is None
        assert seen[0]["dry_run"] is True

    def test_report_dispatch_resume_exec_real_mode(self, tmp_path, monkeypatch):
        """--report --resume 7 --exec:续批 + 真实执行(dry_run=False)。"""
        seen: list[dict[str, Any]] = []

        def fake_run_report(cfg, **kwargs):
            seen.append(kwargs)
            return {"result": {}, "report_path": None}

        monkeypatch.setattr("netsentinel.finishflow.run_report", fake_run_report)
        assert main(["--report", "--resume", "7", "--exec"]) == 0
        assert seen[0]["batch_id"] == 7
        assert seen[0]["dry_run"] is False

    def test_tier_override_applies_to_cfg(self, tmp_path, monkeypatch, capsys):
        """--tier high:覆盖 cfg.concurrency_tier 再跑,礼貌参数不动(红线 35)。"""
        seen: list[dict[str, Any]] = []

        def fake_finish(urls, cfg):
            seen.append({"urls": list(urls), "cfg": cfg})

        monkeypatch.setattr("netsentinel.finishflow.finish", fake_finish)
        path = _write_txt(tmp_path, ["https://a.com/"])
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            f"data_dir: {(tmp_path / 'data').as_posix()}\n"
            f"db_path: {(tmp_path / 'q.db').as_posix()}\n"
            "fetch_delay_s: 2.5\n",
            encoding="utf-8",
        )
        assert main(["--input", path, "--config", str(cfg_path), "--tier", "high"]) == 0
        assert seen[0]["cfg"].concurrency_tier == "high"
        assert seen[0]["cfg"].fetch_delay_s == 2.5   # 礼貌参数原样保留
        assert seen[0]["urls"] == ["https://a.com/"]
        out = capsys.readouterr().out
        assert "已覆盖并发档位:high" in out
        assert "红线 35" in out

    def test_tier_none_keeps_cfg_default(self, tmp_path, monkeypatch):
        """不给 --tier:cfg.concurrency_tier 保持缺省 mid。"""
        seen: list[dict[str, Any]] = []

        def fake_finish(urls, cfg):
            seen.append({"cfg": cfg})

        monkeypatch.setattr("netsentinel.finishflow.finish", fake_finish)
        path = _write_txt(tmp_path, ["https://a.com/"])
        assert main(["--input", path]) == 0
        assert seen[0]["cfg"].concurrency_tier == "mid"

    def test_tier_invalid_choice_exits_2(self, tmp_path):
        """--tier 非法值 → argparse 退出码 2。"""
        path = _write_txt(tmp_path, ["https://a.com/"])
        with pytest.raises(SystemExit) as exc:
            main(["--input", path, "--tier", "ultra"])
        assert exc.value.code == 2

    def test_empty_input_file_short_circuits(self, tmp_path, monkeypatch, capsys):
        """清单零合法 URL:直接返回 0,不触发 finish。"""
        def boom(urls, cfg):  # noqa: ANN001 - 任何调用都是违规
            raise AssertionError("空清单不得触发收官流程")

        monkeypatch.setattr("netsentinel.finishflow.finish", boom)
        path = _write_txt(tmp_path, ["not a url at all"])
        assert main(["--input", path]) == 0
        assert "清单为空" in capsys.readouterr().out

    def test_missing_input_file_returns_2(self, tmp_path, capsys):
        """清单文件不存在 → A107 中文 ValueError → 返回 2。"""
        assert main(["--input", str(tmp_path / "nope.txt")]) == 2
        assert "错误" in capsys.readouterr().out

    def test_runtime_error_returns_3(self, tmp_path, monkeypatch, capsys):
        """兄弟模块未就位(RuntimeError)→ 返回 3,中文消息。"""
        def fake_finish(urls, cfg):  # noqa: ANN001
            raise RuntimeError("模块 netsentinel.ops.tier_state 未就位:boom")

        monkeypatch.setattr("netsentinel.finishflow.finish", fake_finish)
        path = _write_txt(tmp_path, ["https://a.com/"])
        assert main(["--input", path]) == 3
        assert "未就位" in capsys.readouterr().out

    def test_report_sibling_missing_returns_3(self, tmp_path, monkeypatch, capsys):
        """报告模式兄弟缺失(有条目但 A174 未就位)→ 返回 3。"""
        monkeypatch.setitem(
            sys.modules, "netsentinel.agent.sequential_report", None
        )
        _install_module(
            monkeypatch, "netsentinel.decision.batch_review",
            ready_entries=FakeReady(items=[_ready(1, "g1")]),
        )
        assert main(["--report"]) == 3
        assert "未就位" in capsys.readouterr().out

    def test_module_entry_subprocess(self):
        """python -m netsentinel.finishflow 真入口:无参数 → 退出码 2 + 中文。"""
        proc = subprocess.run(
            [sys.executable, "-m", "netsentinel.finishflow"],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
        assert proc.returncode == 2
        assert "需要 --input" in (proc.stdout + proc.stderr)


# ---------------------------------------------------------------------------
# V12:收官流程 trace 树落盘(intel["trace_id"] → data/runs/<站点>/trace.json)
# ---------------------------------------------------------------------------
class FakeTraceReport:
    """携带 intel 的报告替身(intel["trace_id"] 由调用方注入)。"""

    def __init__(self, site_url: str, intel: dict[str, Any] | None = None) -> None:
        self.site_url = site_url
        self.intel = dict(intel or {})


class TestFinishTraceExport:
    def test_trace_json_written_per_site_real_kernel(
        self, tmp_path, monkeypatch, capsys
    ):
        """真实 telemetry_trace 内核:报告携带 trace_id → 落盘
        data/runs/<站点canonical键>/trace.json,内容为 export_trace_json
        的 span 树;result 携带路径、汇总打印 trace 行、saved 计数 +1。"""
        tt = pytest.importorskip("netsentinel.telemetry_trace")
        tt.reset()
        try:
            with tt.new_trace() as tid, tt.span("scan.fetch"):
                pass  # 留一个根 span:落盘内容应为可校验的 span 树
            _finish_deps(monkeypatch)
            cfg = _cfg(tmp_path)
            url = "https://www.a.com/x"
            scan = FakeScan(reports={url: FakeTraceReport(url, {"trace_id": tid})})
            base_saved = telemetry.snapshot()["counters"].get(
                "finishflow.trace_export.saved", 0.0
            )
            result = finish([url], cfg, scan=scan, summary=FakeSummaryAgent())
        finally:
            tt.reset()

        from netsentinel.intel.canonical import canonical_key

        expected = (
            pathlib.Path(cfg.data_dir) / "runs" / canonical_key(url) / "trace.json"
        )
        assert canonical_key(url) == "a.com"  # 站点标识 = canonical 键
        assert result["trace_json_paths"] == [str(expected)]
        assert expected.is_file()
        tree = json.loads(expected.read_text(encoding="utf-8"))
        assert tree["trace_id"] == tid
        assert [n["name"] for n in tree["spans"]] == ["scan.fetch"]
        assert telemetry.snapshot()["counters"].get(
            "finishflow.trace_export.saved", 0.0
        ) == base_saved + 1.0
        out = capsys.readouterr().out
        assert "trace 树" in out and "trace.json" in out

    def test_no_trace_id_zero_behavior_change(self, tmp_path, monkeypatch, capsys):
        """无 trace_id(= trace_enabled 默认关的现状):零行为变化——不写
        任何文件、result 无 trace 键、输出无 trace 行、saved/failed 零增量。"""
        _finish_deps(monkeypatch)
        cfg = _cfg(tmp_path)
        snap0 = telemetry.snapshot()["counters"]
        base_saved = snap0.get("finishflow.trace_export.saved", 0.0)
        base_failed = snap0.get("finishflow.trace_export.failed", 0.0)

        result = finish(["https://a.com/"], cfg, scan=FakeScan(),
                        summary=FakeSummaryAgent())

        assert "trace_json_paths" not in result        # outcome 形态与旧版一致
        assert not (pathlib.Path(cfg.data_dir) / "runs").exists()
        counters = telemetry.snapshot()["counters"]
        assert counters.get("finishflow.trace_export.saved", 0.0) == base_saved
        assert counters.get("finishflow.trace_export.failed", 0.0) == base_failed
        assert "trace 树" not in capsys.readouterr().out

    def test_export_failure_warns_and_never_interrupts(
        self, tmp_path, monkeypatch, caplog
    ):
        """导出函数抛异常:中文 warning + finishflow.trace_export.failed 计数;
        收官流程照常完成(绝不中断),不产生任何文件。"""
        seen: list[str] = []

        def fake_export(tid):
            seen.append(tid)
            raise RuntimeError("导出器爆炸")

        mod = types.ModuleType("netsentinel.telemetry_trace")
        mod.export_trace_json = fake_export
        monkeypatch.setitem(sys.modules, "netsentinel.telemetry_trace", mod)
        _finish_deps(monkeypatch)
        cfg = _cfg(tmp_path)
        url = "https://a.com/"
        scan = FakeScan(reports={url: FakeTraceReport(url, {"trace_id": "deadbeefdeadbeef"})})
        base_failed = telemetry.snapshot()["counters"].get(
            "finishflow.trace_export.failed", 0.0
        )

        with caplog.at_level("WARNING", logger="netsentinel.finishflow"):
            result = finish([url], cfg, scan=scan, summary=FakeSummaryAgent())

        assert seen == ["deadbeefdeadbeef"]            # 按报告携带的 trace_id 调用
        assert result["sites"] == 1 and result["scanned"] == 1  # 收官照常完成
        assert "trace_json_paths" not in result
        assert telemetry.snapshot()["counters"].get(
            "finishflow.trace_export.failed", 0.0
        ) == base_failed + 1.0
        assert "trace 树落盘失败" in caplog.text       # 中文 warning
        assert "不中断收官" in caplog.text
        assert not (pathlib.Path(cfg.data_dir) / "runs").exists()

    def test_trace_module_missing_skips_whole_batch(
        self, tmp_path, monkeypatch, caplog
    ):
        """telemetry_trace 缺席:整批跳过(按待导出份数计 failed),
        中文 warning、收官流程不中断。"""
        monkeypatch.setitem(sys.modules, "netsentinel.telemetry_trace", None)
        _finish_deps(monkeypatch)
        cfg = _cfg(tmp_path)
        urls = ["https://a.com/", "https://b.org/"]
        reports = {u: FakeTraceReport(u, {"trace_id": u}) for u in urls}
        base_failed = telemetry.snapshot()["counters"].get(
            "finishflow.trace_export.failed", 0.0
        )

        with caplog.at_level("WARNING", logger="netsentinel.finishflow"):
            result = finish(urls, cfg, scan=FakeScan(reports=reports),
                            summary=FakeSummaryAgent())

        assert result["sites"] == 2 and result["scanned"] == 2   # 收官照常
        assert "trace_json_paths" not in result
        assert telemetry.snapshot()["counters"].get(
            "finishflow.trace_export.failed", 0.0
        ) == base_failed + 2.0
        assert "trace 树导出模块未就位" in caplog.text

    def test_multiple_sites_same_site_merged_and_chinese_preserved(
        self, tmp_path, monkeypatch
    ):
        """注入假导出器:两站点各写一份 trace.json(span 树结构 + 中文 name
        原样落盘);同 canonical 站的两 URL 写同一目录(后写覆盖,路径去重);
        saved 计数按成功写出次数(含覆盖写)。"""
        seen: list[str] = []

        def fake_export(tid):
            seen.append(tid)
            return {
                "trace_id": tid,
                "started_ts": None,
                "spans": [{
                    "name": "扫描.抓取", "span_id": "s1", "parent_span_id": None,
                    "start_ts": "t0", "duration_ms": 1.5, "error": None,
                    "attrs": {"stage": "fetch"}, "children": [],
                }],
            }

        mod = types.ModuleType("netsentinel.telemetry_trace")
        mod.export_trace_json = fake_export
        monkeypatch.setitem(sys.modules, "netsentinel.telemetry_trace", mod)
        _finish_deps(monkeypatch)
        cfg = _cfg(tmp_path)
        urls = ["https://www.a.com/1", "https://a.com/2", "https://b.org/"]
        reports = {u: FakeTraceReport(u, {"trace_id": u}) for u in urls}
        base_saved = telemetry.snapshot()["counters"].get(
            "finishflow.trace_export.saved", 0.0
        )

        result = finish(urls, cfg, scan=FakeScan(reports=reports),
                        summary=FakeSummaryAgent())

        assert seen == urls                          # 每份报告各导出一次
        runs = pathlib.Path(cfg.data_dir) / "runs"
        a_dir = runs / "a.com"                       # www. 归并:同 canonical 站
        b_dir = runs / "b.org"
        assert result["trace_json_paths"] == [str(a_dir / "trace.json"),
                                              str(b_dir / "trace.json")]
        a_tree = json.loads((a_dir / "trace.json").read_text(encoding="utf-8"))
        assert a_tree["trace_id"] == "https://a.com/2"   # 后写覆盖以最新扫描为准
        b_tree = json.loads((b_dir / "trace.json").read_text(encoding="utf-8"))
        assert b_tree["trace_id"] == "https://b.org/"
        for tree in (a_tree, b_tree):
            assert tree["spans"][0]["name"] == "扫描.抓取"  # 中文 name 原样
            assert tree["spans"][0]["children"] == []
        assert (a_dir / "trace.json").read_text(encoding="utf-8").find("扫描.抓取") > 0
        assert telemetry.snapshot()["counters"].get(
            "finishflow.trace_export.saved", 0.0
        ) == base_saved + 3.0                        # 三次写出(含一次覆盖)


# ---------------------------------------------------------------------------
# V13:批次成本归集收官步(vlm_cost.jsonl → runs/<批次>/cost.jsonl(+parquet))
# ---------------------------------------------------------------------------
def _seed_cost_ledger(data_dir: pathlib.Path, rows: list[dict]) -> pathlib.Path:
    """在 <data_dir>/vlm_cost.jsonl 落账(新旧形态混合;空列表 = 空账本文件)。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    ledger = data_dir / "vlm_cost.jsonl"
    with ledger.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return ledger


def _cost_ledger_rows() -> list[dict]:
    """收官归集测试用的账目:旧行(未标记批次)+ run-x 批两行(含无价提示)。"""
    ts = "2026-10-02T10:00:00+08:00"
    return [
        {"ts": ts, "provider": "openai", "model": "gpt-4o-mini",
         "images": 1000, "est_cost": 10.0},                    # 旧形态行
        {"ts": ts, "provider": "openai", "model": "gpt-4o-mini",
         "images": 500, "est_cost": 5.0, "run_id": "run-x",
         "tokens": 1024, "duration_s": 2.5},                   # 新形态行
        {"ts": ts, "provider": "together", "model": "Llama-4-Scout",
         "images": 300, "est_cost": None, "run_id": "run-x"},  # 无价提示
    ]


def _has_pyarrow() -> bool:
    try:
        import pyarrow  # noqa: F401
        return True
    except ImportError:
        return False


class TestFinishCostCloseout:
    def test_cost_jsonl_always_written_with_aggregates(
        self, tmp_path, monkeypatch, capsys
    ):
        """账本存在 → runs/<批次>/cost.jsonl 永远写(与 pyarrow 无关):meta +
        by=model 明细 + by=run_id 总账 + 两份 totals;result 携带路径;
        打印中文成本表;saved 计数 +1。"""
        _finish_deps(monkeypatch)
        cfg = _cfg(tmp_path)
        _seed_cost_ledger(tmp_path / "data", _cost_ledger_rows())
        base_saved = telemetry.snapshot()["counters"].get(
            "finishflow.cost_export.saved", 0.0
        )
        result = finish(["https://a.com/"], cfg, scan=FakeScan(),
                        summary=FakeSummaryAgent(), run_id="run-x")

        out_path = pathlib.Path(cfg.data_dir) / "runs" / "run-x" / "cost.jsonl"
        assert result["cost_jsonl"] == str(out_path)
        assert result["run_id"] == "run-x"
        assert out_path.is_file()
        lines = [json.loads(x) for x in out_path.read_text(encoding="utf-8").splitlines()]
        meta = lines[0]
        assert meta["kind"] == "meta"
        assert meta["run_id"] == "run-x"
        assert meta["ledger"] == "vlm_cost.jsonl"
        assert meta["records"] == 3                      # 有效账目行数
        model_rows = [l for l in lines if l.get("kind") == "agg" and l.get("by") == "model"]
        assert {(r["model"], r["calls"], r["est_cost"]) for r in model_rows} == {
            ("openai:gpt-4o-mini", 2, 15.0),
            ("together:Llama-4-Scout", 1, 0.0),
        }
        run_rows = {l["run_id"]: l for l in lines
                    if l.get("kind") == "agg" and l.get("by") == "run_id"}
        assert run_rows["run-x"]["calls"] == 2
        assert run_rows["(未标记批次)"]["calls"] == 1      # 旧行回退桶
        assert run_rows["(未标记批次)"]["est_cost"] == 10.0
        totals = [l for l in lines if l.get("kind") == "totals"]
        assert len(totals) == 2
        assert {t["by"] for t in totals} == {"model", "run_id"}
        assert telemetry.snapshot()["counters"].get(
            "finishflow.cost_export.saved", 0.0
        ) == base_saved + 1.0
        out = capsys.readouterr().out
        for token in ("批次成本汇总", "批次标识", "openai:gpt-4o-mini",
                      "调用", "费用", "合计", "总账(按批次)", "cost.jsonl"):
            assert token in out, f"成本表缺少:{token}"
        # pyarrow 为可选 extra:本机无 → 无 parquet;装有 analytics → 有
        if _has_pyarrow():
            assert result.get("cost_parquet", "").endswith("cost.parquet")
        else:
            assert "cost_parquet" not in result

    def test_cost_step_zero_behavior_without_ledger(
        self, tmp_path, monkeypatch, capsys
    ):
        """账本不存在 → 零行为:不建目录、result 无 cost 键、无成本打印、计数零增量。"""
        _finish_deps(monkeypatch)
        cfg = _cfg(tmp_path)
        snap0 = telemetry.snapshot()["counters"]
        result = finish(["https://a.com/"], cfg, scan=FakeScan(),
                        summary=FakeSummaryAgent())
        assert "cost_jsonl" not in result
        assert "cost_parquet" not in result
        assert "run_id" not in result
        assert not (pathlib.Path(cfg.data_dir) / "runs").exists()
        assert "成本" not in capsys.readouterr().out
        counters = telemetry.snapshot()["counters"]
        for name in ("finishflow.cost_export.saved",
                     "finishflow.cost_export.parquet",
                     "finishflow.cost_export.failed"):
            assert counters.get(name, 0.0) == snap0.get(name, 0.0)

    def test_cost_step_writes_even_for_empty_ledger(self, tmp_path, monkeypatch):
        """账本存在但零有效行 → cost.jsonl 照写(meta records=0 + 零值 totals)。"""
        _finish_deps(monkeypatch)
        cfg = _cfg(tmp_path)
        _seed_cost_ledger(tmp_path / "data", [])
        result = finish(["https://a.com/"], cfg, scan=FakeScan(),
                        summary=FakeSummaryAgent(), run_id="run-x")
        out_path = pathlib.Path(cfg.data_dir) / "runs" / "run-x" / "cost.jsonl"
        assert result["cost_jsonl"] == str(out_path)
        lines = [json.loads(x) for x in out_path.read_text(encoding="utf-8").splitlines()]
        assert lines[0]["kind"] == "meta" and lines[0]["records"] == 0
        assert all(l["kind"] != "agg" for l in lines)     # 无明细行
        assert [l for l in lines if l["kind"] == "totals"][0]["calls"] == 0

    def test_parquet_export_with_fake_pyarrow(self, tmp_path, monkeypatch):
        """伪 pyarrow 模块:聚合行经 from_pylist 建表、write_table 落
        runs/<批次>/cost.parquet;parquet 计数 +1;result 携带 cost_parquet。"""
        _finish_deps(monkeypatch)
        cfg = _cfg(tmp_path)
        _seed_cost_ledger(tmp_path / "data", _cost_ledger_rows())
        calls: dict[str, Any] = {}
        fake_pa = types.ModuleType("pyarrow")
        fake_pq = types.ModuleType("pyarrow.parquet")

        class _FakeTable:
            @staticmethod
            def from_pylist(rows):
                calls["rows"] = [dict(r) for r in rows]
                return "FAKE_TABLE"

        def _fake_write_table(table, path):
            calls["write"] = (table, str(path))

        fake_pa.Table = _FakeTable
        fake_pq.write_table = _fake_write_table
        fake_pa.parquet = fake_pq          # import pyarrow.parquet as pq 的属性路径
        monkeypatch.setitem(sys.modules, "pyarrow", fake_pa)
        monkeypatch.setitem(sys.modules, "pyarrow.parquet", fake_pq)
        base_parquet = telemetry.snapshot()["counters"].get(
            "finishflow.cost_export.parquet", 0.0
        )

        result = finish(["https://a.com/"], cfg, scan=FakeScan(),
                        summary=FakeSummaryAgent(), run_id="run-x")

        assert calls["write"][0] == "FAKE_TABLE"
        written = pathlib.PurePath(calls["write"][1])
        assert written.name == "cost.parquet" and written.parent.name == "run-x"
        by_tags = {r["by"] for r in calls["rows"]}
        assert {"model", "run_id", "model:totals", "run_id:totals"} == by_tags
        openai_row = next(r for r in calls["rows"]
                          if r.get("by") == "model"
                          and r.get("model") == "openai:gpt-4o-mini")
        assert openai_row["calls"] == 2 and openai_row["est_cost"] == 15.0
        assert result["cost_parquet"].endswith("cost.parquet")
        assert telemetry.snapshot()["counters"].get(
            "finishflow.cost_export.parquet", 0.0
        ) == base_parquet + 1.0

    def test_parquet_failure_degrades_to_jsonl(self, tmp_path, monkeypatch, caplog):
        """write_table 抛异常 → 中文 warning + failed 计数;已落盘 jsonl 不受影响;
        收官照常完成(无 cost_parquet 键,绝不中断)。"""
        _finish_deps(monkeypatch)
        cfg = _cfg(tmp_path)
        _seed_cost_ledger(tmp_path / "data", _cost_ledger_rows())
        fake_pa = types.ModuleType("pyarrow")
        fake_pq = types.ModuleType("pyarrow.parquet")

        class _FakeTable:
            @staticmethod
            def from_pylist(rows):
                return "FAKE_TABLE"

        def _boom_write_table(table, path):
            raise RuntimeError("parquet 写盘爆炸")

        fake_pa.Table = _FakeTable
        fake_pq.write_table = _boom_write_table
        fake_pa.parquet = fake_pq
        monkeypatch.setitem(sys.modules, "pyarrow", fake_pa)
        monkeypatch.setitem(sys.modules, "pyarrow.parquet", fake_pq)
        base_failed = telemetry.snapshot()["counters"].get(
            "finishflow.cost_export.failed", 0.0
        )

        with caplog.at_level("WARNING", logger="netsentinel.finishflow"):
            result = finish(["https://a.com/"], cfg, scan=FakeScan(),
                            summary=FakeSummaryAgent(), run_id="run-x")

        assert (pathlib.Path(cfg.data_dir) / "runs" / "run-x" / "cost.jsonl").is_file()
        assert result["sites"] == 1 and result["scanned"] == 1   # 收官照常完成
        assert "cost_jsonl" in result and "cost_parquet" not in result
        assert telemetry.snapshot()["counters"].get(
            "finishflow.cost_export.failed", 0.0
        ) == base_failed + 1.0
        assert "parquet 导出失败" in caplog.text
        assert "降级" in caplog.text and "不中断收官" in caplog.text

    def test_cost_closeout_failure_never_interrupts(self, tmp_path, monkeypatch, caplog):
        """归集整体失败(CostMeter 构造即炸)→ 中文 warning + failed 计数;
        收官照常返回完整 outcome,绝不中断。"""
        _finish_deps(monkeypatch)
        cfg = _cfg(tmp_path)
        _seed_cost_ledger(tmp_path / "data", _cost_ledger_rows())

        class _BoomMeter:
            def __init__(self, path):  # noqa: ANN001
                raise RuntimeError("账本打不开")

        mod = types.ModuleType("netsentinel.vision.cost_meter")
        mod.CostMeter = _BoomMeter
        monkeypatch.setitem(sys.modules, "netsentinel.vision.cost_meter", mod)
        base_failed = telemetry.snapshot()["counters"].get(
            "finishflow.cost_export.failed", 0.0
        )

        with caplog.at_level("WARNING", logger="netsentinel.finishflow"):
            result = finish(["https://a.com/"], cfg, scan=FakeScan(),
                            summary=FakeSummaryAgent(), run_id="run-x")

        assert result["sites"] == 1 and result["scanned"] == 1   # 收官不中断
        assert result["groups"] == 2                             # 汇总照常
        assert "cost_jsonl" not in result
        assert telemetry.snapshot()["counters"].get(
            "finishflow.cost_export.failed", 0.0
        ) == base_failed + 1.0
        assert "批次成本归集失败" in caplog.text
        assert "不中断收官" in caplog.text

    def test_run_id_default_generated_and_sanitized(self, tmp_path, monkeypatch):
        """不给 run_id → 本地时间缺省标识 run-YYYYMMDD-HHMMSS;显式标识经
        安全化作目录名(与站点目录同口径)。"""
        _finish_deps(monkeypatch)
        cfg = _cfg(tmp_path)
        _seed_cost_ledger(tmp_path / "data", _cost_ledger_rows())

        result = finish(["https://a.com/"], cfg, scan=FakeScan(),
                        summary=FakeSummaryAgent())
        assert re.fullmatch(r"run-\d{8}-\d{6}", result["run_id"])
        runs_dir = pathlib.Path(cfg.data_dir) / "runs"
        assert [p.name for p in runs_dir.iterdir()] == [result["run_id"]]

        result2 = finish(["https://a.com/"], cfg, scan=FakeScan(),
                         summary=FakeSummaryAgent(), run_id="run 2026/10")
        assert result2["run_id"] == "run 2026/10"          # 原样入 meta/result
        safe_dir = pathlib.Path(cfg.data_dir) / "runs" / "run_2026_10"
        assert (safe_dir / "cost.jsonl").is_file()          # 目录名已安全化
        assert result2["cost_jsonl"] == str(safe_dir / "cost.jsonl")


# ---------------------------------------------------------------------------
# A232:批预算哨兵检查(cfg.batch_cost_budget;哨兵不是执行器)
# ---------------------------------------------------------------------------
def _budget_ledger_rows() -> list[dict]:
    """预算检查用账目:run-x 批 5.0 元(已定价)+ 1 次无价提示;另有他批。"""
    ts = "2026-10-03T10:00:00+08:00"
    return [
        {"ts": ts, "provider": "openai", "model": "gpt-4o-mini",
         "images": 1000, "est_cost": 10.0},                    # 未标记批(不计入)
        {"ts": ts, "provider": "openai", "model": "gpt-4o-mini",
         "images": 500, "est_cost": 5.0, "run_id": "run-x"},   # 本批 5.0 元
        {"ts": ts, "provider": "together", "model": "Llama-4-Scout",
         "images": 300, "est_cost": None, "run_id": "run-x"},  # 本批无价提示
    ]


class TestFinishBudgetSentinel:
    def test_budget_exceeded_marks_result_alerts_and_keeps_files(
        self, tmp_path, monkeypatch, capsys
    ):
        """超限:result 标记 budget_exceeded + 明细;中文醒目告警;exceeded 计数;
        **不删文件**:cost.jsonl 原样在盘;**不改队列**:批次库零批次。"""
        _finish_deps(monkeypatch)
        cfg = _cfg(tmp_path)
        cfg.batch_cost_budget = 4.5     # 附加实例属性(getattr 动态读取口径)
        _seed_cost_ledger(tmp_path / "data", _budget_ledger_rows())
        base = telemetry.snapshot()["counters"].get(
            "finishflow.budget.exceeded", 0.0
        )

        result = finish(["https://a.com/"], cfg, scan=FakeScan(),
                        summary=FakeSummaryAgent(), run_id="run-x")

        assert result["budget_exceeded"] is True
        assert result["budget"]["run_id"] == "run-x"
        assert result["budget"]["spent"] == 5.0
        assert result["budget"]["max_cost"] == 4.5
        assert result["budget"]["unpriced_calls"] == 1
        assert result["budget"]["calls"] == 2
        assert "超批预算" in result["budget"]["advice"]
        assert telemetry.snapshot()["counters"].get(
            "finishflow.budget.exceeded", 0.0
        ) == base + 1.0
        out = capsys.readouterr().out
        for token in ("批预算超限告警", "本批费用", "5.00", "4.50",
                      "处置建议", "哨兵不是执行器"):
            assert token in out, f"预算告警缺少:{token}"
        # 红线:绝不删除已产出文件——成本账快照原样在盘
        cost_jsonl = pathlib.Path(cfg.data_dir) / "runs" / "run-x" / "cost.jsonl"
        assert cost_jsonl.is_file()
        assert result["cost_jsonl"] == str(cost_jsonl)
        # 红线:绝不改队列状态——收官流程本就不触碰举报批次库(仍为空)
        from netsentinel.submit.batch_state import BatchState

        st = BatchState(cfg.db_path)
        try:
            assert st.list_batches() == []
        finally:
            st.close()

    def test_budget_within_limit_silent_no_marking(
        self, tmp_path, monkeypatch, capsys
    ):
        """未超限:零噪音——不标记 result、不打印告警、计数零增量。"""
        _finish_deps(monkeypatch)
        cfg = _cfg(tmp_path)
        cfg.batch_cost_budget = 100.0
        _seed_cost_ledger(tmp_path / "data", _budget_ledger_rows())
        base = telemetry.snapshot()["counters"].get(
            "finishflow.budget.exceeded", 0.0
        )

        result = finish(["https://a.com/"], cfg, scan=FakeScan(),
                        summary=FakeSummaryAgent(), run_id="run-x")

        assert "budget_exceeded" not in result and "budget" not in result
        assert telemetry.snapshot()["counters"].get(
            "finishflow.budget.exceeded", 0.0
        ) == base
        assert "批预算超限告警" not in capsys.readouterr().out

    def test_budget_absent_zero_behavior(self, tmp_path, monkeypatch, capsys):
        """batch_cost_budget 缺省未配置(getattr None)→ 零行为:不检查、不计数。"""
        _finish_deps(monkeypatch)
        cfg = _cfg(tmp_path)
        assert not hasattr(cfg, "batch_cost_budget") or cfg.batch_cost_budget is None
        _seed_cost_ledger(tmp_path / "data", _budget_ledger_rows())
        snap0 = telemetry.snapshot()["counters"]

        result = finish(["https://a.com/"], cfg, scan=FakeScan(),
                        summary=FakeSummaryAgent(), run_id="run-x")

        assert "budget_exceeded" not in result and "budget" not in result
        counters = telemetry.snapshot()["counters"]
        assert counters.get("finishflow.budget.exceeded", 0.0) == snap0.get(
            "finishflow.budget.exceeded", 0.0
        )
        assert "批预算超限告警" not in capsys.readouterr().out

    def test_budget_without_ledger_skips_silently(
        self, tmp_path, monkeypatch, capsys
    ):
        """配置了预算但账本不存在(无 VLM 记账)→ 无账可查,零行为跳过。"""
        _finish_deps(monkeypatch)
        cfg = _cfg(tmp_path)
        cfg.batch_cost_budget = 0.01
        snap0 = telemetry.snapshot()["counters"]

        result = finish(["https://a.com/"], cfg, scan=FakeScan(),
                        summary=FakeSummaryAgent(), run_id="run-x")

        assert "budget_exceeded" not in result
        assert telemetry.snapshot()["counters"].get(
            "finishflow.budget.exceeded", 0.0
        ) == snap0.get("finishflow.budget.exceeded", 0.0)
        assert "批预算超限告警" not in capsys.readouterr().out

    def test_budget_invalid_config_warns_never_interrupts(
        self, tmp_path, monkeypatch, caplog
    ):
        """预算配置非法(负数 / 非数值)→ 中文 warning + 跳过检查,收官不中断。"""
        import logging

        _finish_deps(monkeypatch)
        cfg = _cfg(tmp_path)
        cfg.batch_cost_budget = -5
        _seed_cost_ledger(tmp_path / "data", _budget_ledger_rows())

        with caplog.at_level(logging.WARNING, logger="netsentinel.finishflow"):
            result = finish(["https://a.com/"], cfg, scan=FakeScan(),
                            summary=FakeSummaryAgent(), run_id="run-x")

        assert result["sites"] == 1 and result["scanned"] == 1   # 收官照常
        assert "budget_exceeded" not in result
        assert "批预算配置" in caplog.text and "不中断收官" in caplog.text

    def test_budget_module_missing_never_interrupts(
        self, tmp_path, monkeypatch, caplog
    ):
        """cost_meter 模块未就位 → 预算检查失败只中文 warning,收官绝不中断。"""
        import logging

        _finish_deps(monkeypatch)
        cfg = _cfg(tmp_path)
        cfg.batch_cost_budget = 4.5
        _seed_cost_ledger(tmp_path / "data", _budget_ledger_rows())
        monkeypatch.setitem(sys.modules, "netsentinel.vision.cost_meter", None)

        with caplog.at_level(logging.WARNING, logger="netsentinel.finishflow"):
            result = finish(["https://a.com/"], cfg, scan=FakeScan(),
                            summary=FakeSummaryAgent(), run_id="run-x")

        assert result["sites"] == 1 and result["groups"] == 2    # 收官照常
        assert "budget_exceeded" not in result                   # 未判定即不标记
        assert "批预算检查失败" in caplog.text and "不中断收官" in caplog.text

    def test_budget_default_run_id_zero_spent_not_over(
        self, tmp_path, monkeypatch
    ):
        """不给 run_id:缺省批次标识查账为零花费 → 不超限、不标记
        (⑤ 归集与 ⑥ 预算共用同一缺省标识,不二次生成)。"""
        _finish_deps(monkeypatch)
        cfg = _cfg(tmp_path)
        cfg.batch_cost_budget = 1.0
        _seed_cost_ledger(tmp_path / "data", _budget_ledger_rows())

        result = finish(["https://a.com/"], cfg, scan=FakeScan(),
                        summary=FakeSummaryAgent())

        assert re.fullmatch(r"run-\d{8}-\d{6}", result["run_id"])  # ⑤⑥ 同一标识
        assert "budget_exceeded" not in result   # 缺省标识无账目 → 0 元未超
