# -*- coding: utf-8 -*-
"""A170(并入 A174)顺序举报代理 SequentialReportAgent 单元测试:全离线、全注入。

覆盖(契约 V9 §2 A170 行 + §0 红线 36):

- run_batch 编排缝:items / cfg / state / dry_run / stop 透传、auto_confirm
  恒不出现在引擎 kwargs(红线 36 运行时缝)、返回形状与 submitted 镜像、
  cfg 缺失中文 ValueError、run 处覆盖构造处、注入 executor 优先;
- on_item 逐条进度回调:每条前触发(index/total/item,1 起序)、先于执行器、
  异常吞没不中断、混合门户分派包装缺省计划器、未启用时不传计划器包装;
- SessionExecutor 路径:executor_cls 实例化恰 1 次 / run N 次 / close 恰 1 次、
  包装器只透传 dry_run(auto_confirm 保持执行器缺省 False)、
  browser_session_reuse 开→惰性会话类 / 关→不传 executor、
  引擎异常时 finally 仍 close、close 自身异常吞没;
- 报告渲染:batch_id 传入→A114 渲染到 cfg.data_dir/finish_report_{id}.html
  并回填 report_path、无 batch_id 不渲染、渲染失败降级 None、真渲染器落盘;
- 遥测:seq_report.run 计数、seq_report.submitted 仪表;
- 红线 36 静态守卫:源码无 auto_confirm=True 字样、不触碰复核队列 / 声明、
  模块 docstring 声明红线;
- 真 A112 run_batch 集成(离线):干跑逐条、会话复用包装、stop 暂停时
  on_item 不触发。

零 playwright、零网络、零真实门户;runner / executor / executor_cls /
renderer / 门户计划器 / 频控间隔全 fake。
"""
from __future__ import annotations

import pathlib
import sys
import types
from pathlib import Path
from typing import Any

import pytest

import netsentinel.agent.sequential_report as seq
from netsentinel import telemetry
from netsentinel.agent.sequential_report import SequentialReportAgent
from netsentinel.contracts import Config, ExecutionResult


# ---------------------------------------------------------------------------
# Fake 组件(离线注入)
# ---------------------------------------------------------------------------
class FakeState:
    """A113 鸭子状态器替身:记录全部 mark 调用 (entry_id, status, error)。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, str, str]] = []

    def mark(self, entry_id: Any, status: str, error: str = "") -> None:
        self.calls.append((entry_id, status, error))

    def of(self, entry_id: Any) -> list[tuple[str, str]]:
        return [(s, e) for eid, s, e in self.calls if eid == entry_id]


def _zero_summary(**extra: Any) -> dict[str, Any]:
    """A112 零值摘要形状(测试基线)。"""
    base = {
        "submitted": 0,
        "failed": 0,
        "rate_limited": False,
        "paused": False,
        "results": [],
        "note": "",
    }
    base.update(extra)
    return base


class RecRunner:
    """引擎替身:只记录 (items, cfg, kwargs) 并返回预置摘要(可抛异常)。"""

    def __init__(self, summary: dict[str, Any] | None = None, exc: BaseException | None = None) -> None:
        self.summary = dict(summary or _zero_summary())
        self.exc = exc
        self.calls: list[dict[str, Any]] = []

    def __call__(self, items: Any, cfg: Any, **kw: Any) -> dict[str, Any]:
        self.calls.append(
            {"items": list(items), "cfg": cfg, "kwargs": dict(kw)}
        )
        if self.exc is not None:
            raise self.exc
        return dict(self.summary)


class LoopRunner:
    """模拟 A112 run_batch 顺序循环的引擎替身:逐条 计划器→执行器。

    每条按 portal 分派 ``plan_12377`` / ``plan_shdf`` 包装(kwargs 缺席用
    stub 计划),再以 ``executor(plan, cfg, auto_confirm=False, dry_run=...)``
    调用(与 A112 真实调用形状一致);stop 为真即暂停返回。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.exec_plans: list[Any] = []
        self.planner_hits: list[tuple[str, Any]] = []
        self.paused = False

    def __call__(self, items: Any, cfg: Any, **kw: Any) -> dict[str, Any]:
        self.calls.append({"items": list(items), "cfg": cfg, "kwargs": dict(kw)})
        executor = kw.get("executor")
        submitted = 0
        for item in items:
            if kw.get("stop") and kw["stop"]():
                self.paused = True
                break
            portal = str(item.get("portal", "12377"))
            key = "plan_12377" if portal == "12377" else "plan_shdf"
            planner = kw.get(key)
            plan = planner(item, cfg) if planner is not None else {"stub": item.get("entry_id")}
            self.exec_plans.append(plan)
            if executor is None:
                result = ExecutionResult(
                    portal=portal, ok=True, submitted=not bool(kw.get("dry_run"))
                )
            else:
                result = executor(plan, cfg, auto_confirm=False, dry_run=kw.get("dry_run"))
            if getattr(result, "submitted", False):
                submitted += 1
        return _zero_summary(submitted=submitted, paused=self.paused)


class FakeExecutor:
    """普通执行器替身:捕获 (plan, auto_confirm, dry_run),恒提交成功。"""

    def __init__(self, outcome: str = "submitted") -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    def __call__(self, plan: Any, cfg: Any, *, auto_confirm: bool = None, dry_run: bool = None, **kw: Any) -> Any:
        self.calls.append(
            {"plan": plan, "cfg": cfg, "auto_confirm": auto_confirm, "dry_run": dry_run}
        )
        submitted = self.outcome == "submitted" and not bool(dry_run)
        return ExecutionResult(portal="12377", ok=True, submitted=submitted)

    @property
    def n(self) -> int:
        return len(self.calls)


class FakeSession:
    """V7 SessionExecutor 替身类(作 executor_cls 用):记录实例化/run/close。"""

    made: list["FakeSession"] = []

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self.runs: list[dict[str, Any]] = []
        self.closed = 0
        self.close_exc: BaseException | None = None
        FakeSession.made.append(self)

    def run(self, plan: Any, *, auto_confirm: bool = False, dry_run: bool | None = None) -> Any:
        self.runs.append({"plan": plan, "auto_confirm": auto_confirm, "dry_run": dry_run})
        return ExecutionResult(portal="12377", ok=True, submitted=not bool(dry_run))

    def close(self) -> None:
        self.closed += 1
        if self.close_exc is not None:
            raise self.close_exc


class BoomSession:
    """不应被实例化的哨兵类:实例化即 AssertionError(验证优先级/开关)。"""

    def __init__(self, cfg: Any) -> None:
        raise AssertionError("BoomSession 不应被实例化")


class FakeRenderer:
    """A114 渲染器替身:记录调用并落一个占位 HTML(可抛异常)。"""

    def __init__(self, exc: BaseException | None = None) -> None:
        self.exc = exc
        self.calls: list[dict[str, Any]] = []

    def __call__(self, batch_id: Any, summary: Any, out_path: str) -> str:
        self.calls.append(
            {"batch_id": batch_id, "summary": summary, "out_path": out_path}
        )
        if self.exc is not None:
            raise self.exc
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("<html>fake</html>", encoding="utf-8")
        return "<html>fake</html>"


def _item(entry_id: Any, portal: str = "12377", group: str = "g1") -> dict[str, Any]:
    """标准举报条目(A112 三必填键 + 执行细节)。"""
    return {
        "entry_id": entry_id,
        "group_name": group,
        "portal": portal,
        "site_url": "http://x.example/",
        "evidence_zip": "z.zip",
    }


def _ns_cfg(tmp_path: pathlib.Path, *, reuse: bool = False, data_dir: str | None = None) -> types.SimpleNamespace:
    """鸭子配置替身(runner fake 路径用;只带本代理读取的字段)。"""
    return types.SimpleNamespace(
        data_dir=data_dir if data_dir is not None else str(tmp_path / "data"),
        browser_session_reuse=reuse,
    )


def _real_cfg(tmp_path: pathlib.Path, **kw: Any) -> Config:
    """离线真 Config:data/audit/db 全指向 tmp;间隔 0(真 run_batch 集成用)。"""
    kw.setdefault("browser_session_reuse", False)
    return Config(
        data_dir=str(tmp_path / "data"),
        audit_path=str(tmp_path / "audit.jsonl"),
        db_path=str(tmp_path / "q.db"),
        dry_run_default=True,
        batch_item_interval_s=0,
        submit_min_interval_s=0,
        **kw,
    )


def _install_portals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """把假门户计划器模块塞进 sys.modules(run_batch / 本代理均惰性导入)。"""
    hits: list[tuple[str, Any]] = []

    def plan_12377(entry: Any, cfg: Any) -> dict[str, Any]:
        hits.append(("12377", entry))
        return {"portal": "12377", "entry_id": entry.get("entry_id")}

    def plan_shdf(entry: Any, cfg: Any) -> dict[str, Any]:
        hits.append(("shdf", entry))
        return {"portal": "shdf", "entry_id": entry.get("entry_id")}

    for name, fn in (
        ("netsentinel.submit.portal_12377", plan_12377),
        ("netsentinel.submit.portal_shdf", plan_shdf),
    ):
        mod = types.ModuleType(name)
        setattr(mod, "plan_12377" if name.endswith("12377") else "plan_shdf", fn)
        monkeypatch.setitem(sys.modules, name, mod)
    return hits


@pytest.fixture(autouse=True)
def _isolated_fakes(monkeypatch: pytest.MonkeyPatch):
    """每用例:装上假门户计划器(on_item 包装会惰性解析缺省计划器)、
    清空 FakeSession 类级登记,避免跨用例泄漏。"""
    _install_portals(monkeypatch)
    FakeSession.made.clear()
    yield
    FakeSession.made.clear()


# ---------------------------------------------------------------------------
# run_batch 编排缝(runner 注入)
# ---------------------------------------------------------------------------
class TestEngineOrchestration:
    def test_runner_receives_items_cfg_state_dry_run(self, tmp_path):
        """items / cfg / state / dry_run 原样透传给引擎。"""
        runner = RecRunner()
        cfg = _ns_cfg(tmp_path)
        st = FakeState()
        items = [_item(1), _item(2)]
        SequentialReportAgent(cfg).run(items, dry_run=False, state=st, runner=runner)
        call = runner.calls[0]
        assert call["items"] == items
        assert call["cfg"] is cfg
        assert call["kwargs"]["state"] is st
        assert call["kwargs"]["dry_run"] is False

    def test_dry_run_none_passthrough(self, tmp_path):
        """dry_run=None 原样透传(由 run_batch 落回 cfg.dry_run_default)。"""
        runner = RecRunner()
        SequentialReportAgent(_ns_cfg(tmp_path)).run([_item(1)], runner=runner)
        assert runner.calls[0]["kwargs"]["dry_run"] is None

    def test_dry_run_true_passthrough(self, tmp_path):
        runner = RecRunner()
        SequentialReportAgent(_ns_cfg(tmp_path)).run([_item(1)], dry_run=True, runner=runner)
        assert runner.calls[0]["kwargs"]["dry_run"] is True

    def test_auto_confirm_absent_from_engine_kwargs(self, tmp_path):
        """红线 36 运行时缝:引擎 kwargs 中恒不出现 auto_confirm。"""
        runner = RecRunner()
        SequentialReportAgent(_ns_cfg(tmp_path)).run([_item(1)], dry_run=False, runner=runner)
        assert "auto_confirm" not in runner.calls[0]["kwargs"]

    def test_return_shape_and_submitted_mirror(self, tmp_path):
        """返回 {"result", "report_path", "submitted"};submitted 镜像摘要。"""
        summary = _zero_summary(submitted=2, failed=1)
        runner = RecRunner(summary=summary)
        out = SequentialReportAgent(_ns_cfg(tmp_path)).run([_item(1), _item(2)], runner=runner)
        assert out["result"] == summary
        assert out["submitted"] == 2
        assert out["report_path"] is None

    def test_submitted_zero_when_engine_returns_odd_result(self, tmp_path):
        """引擎返回非映射 / 缺 submitted → submitted 取 0(鸭子容错)。"""
        out1 = SequentialReportAgent(_ns_cfg(tmp_path)).run([], runner=lambda *a, **k: None)
        assert out1["submitted"] == 0
        out2 = SequentialReportAgent(_ns_cfg(tmp_path)).run(
            [], runner=lambda *a, **k: {"submitted": "bad"}
        )
        assert out2["submitted"] == 0

    def test_cfg_missing_chinese_valueerror(self, tmp_path):
        """构造与 run 均未给 cfg → 中文 ValueError。"""
        with pytest.raises(ValueError, match="cfg"):
            SequentialReportAgent().run([_item(1)], runner=RecRunner())

    def test_run_cfg_overrides_init_cfg(self, tmp_path):
        """run 处 cfg 优先于构造处。"""
        runner = RecRunner()
        init_cfg, run_cfg = _ns_cfg(tmp_path), _ns_cfg(tmp_path, data_dir="other")
        SequentialReportAgent(init_cfg).run([_item(1)], run_cfg, runner=runner)
        assert runner.calls[0]["cfg"] is run_cfg

    def test_init_cfg_used_when_run_omits(self, tmp_path):
        runner = RecRunner()
        cfg = _ns_cfg(tmp_path)
        SequentialReportAgent(cfg).run([_item(1)], runner=runner)
        assert runner.calls[0]["cfg"] is cfg

    def test_state_from_constructor(self, tmp_path):
        """构造处 state 在 run 未覆盖时生效。"""
        runner = RecRunner()
        st = FakeState()
        SequentialReportAgent(_ns_cfg(tmp_path), state=st).run([_item(1)], runner=runner)
        assert runner.calls[0]["kwargs"]["state"] is st

    def test_stop_passthrough(self, tmp_path):
        """stop 回调透传(暂停语义由 run_batch 收口)。"""
        runner = RecRunner()
        stop = lambda: False  # noqa: E731 - 测试替身
        SequentialReportAgent(_ns_cfg(tmp_path)).run([_item(1)], stop=stop, runner=runner)
        assert runner.calls[0]["kwargs"]["stop"] is stop

    def test_engine_exception_propagates(self, tmp_path):
        """引擎异常原样上抛(不吞)。"""
        runner = RecRunner(exc=ValueError("条数超过上限"))
        with pytest.raises(ValueError, match="上限"):
            SequentialReportAgent(_ns_cfg(tmp_path)).run([_item(1)], runner=runner)

    def test_finishflow_call_form_compatible(self, tmp_path, monkeypatch):
        """A169 调用形态:SequentialReportAgent().run(items, cfg, dry_run=, state=)。"""
        runner = RecRunner(summary=_zero_summary(submitted=1))
        monkeypatch.setattr(seq, "_run_batch", runner)
        cfg = _ns_cfg(tmp_path)
        st = FakeState()
        out = SequentialReportAgent().run([_item(7)], cfg, dry_run=True, state=st)
        assert runner.calls[0]["cfg"] is cfg
        assert runner.calls[0]["kwargs"]["state"] is st
        assert out["submitted"] == 1

    def test_contract_a170_full_signature(self, tmp_path):
        """契约 A170 行全参形态:run(items, cfg, executor=, state=, dry_run=, on_item=)。"""
        runner = LoopRunner()
        ex = FakeExecutor()
        st = FakeState()
        seen: list[tuple[int, int, Any]] = []

        def on_item(index: int, total: int, item: Any) -> None:
            seen.append((index, total, item))

        out = SequentialReportAgent().run(
            [_item(1), _item(2)], _ns_cfg(tmp_path),
            executor=ex, state=st, dry_run=True, on_item=on_item, runner=runner,
        )
        assert ex.n == 2
        assert seen == [(1, 2, _item(1)), (2, 2, _item(2))]
        assert out["submitted"] == 0   # 干跑:执行成功但未真实提交


# ---------------------------------------------------------------------------
# on_item 逐条进度回调
# ---------------------------------------------------------------------------
class TestOnItemCallback:
    def test_fires_per_item_with_index_and_total(self, tmp_path):
        """每条前触发 on_item(index, total, item);index 1 起序。"""
        runner = LoopRunner()
        seen: list[tuple[int, int, Any]] = []
        items = [_item(1), _item(2), _item(3)]
        SequentialReportAgent(_ns_cfg(tmp_path)).run(
            items, dry_run=True, on_item=lambda i, n, it: seen.append((i, n, it)),
            runner=runner,
        )
        assert seen == [(1, 3, items[0]), (2, 3, items[1]), (3, 3, items[2])]

    def test_fires_before_executor_sees_plan(self, tmp_path):
        """on_item 先于该条执行(顺序敏感:进度在前、执行在后)。"""
        runner = LoopRunner()
        order: list[str] = []

        def on_item(index: int, total: int, item: Any) -> None:
            order.append(f"on:{item['entry_id']}")

        ex = FakeExecutor()

        def spy(plan, cfg, **kw):
            order.append(f"exec:{plan['entry_id']}")
            return ex(plan, cfg, **kw)

        SequentialReportAgent(_ns_cfg(tmp_path)).run(
            [_item("a"), _item("b")], dry_run=True, on_item=on_item,
            runner=runner, executor=spy,
        )
        assert order == ["on:a", "exec:a", "on:b", "exec:b"]

    def test_exception_in_callback_swallowed(self, tmp_path):
        """回调异常吞没:批量继续、后续回调照常、结果照常返回。"""
        runner = LoopRunner()
        seen: list[int] = []

        def on_item(index: int, total: int, item: Any) -> None:
            seen.append(index)
            if index == 2:
                raise RuntimeError("进度钩子炸了")

        out = SequentialReportAgent(_ns_cfg(tmp_path)).run(
            [_item(1), _item(2), _item(3)], dry_run=True,
            on_item=on_item, runner=runner,
        )
        assert seen == [1, 2, 3]
        assert len(runner.exec_plans) == 3     # 第 2 条未被中断
        assert out["result"]["paused"] is False

    def test_mixed_portals_dispatch_wrapped_planners(self, tmp_path, monkeypatch):
        """混合门户经包装计划器分派到(假)缺省门户计划器,回调 1 起序不乱。"""
        hits = _install_portals(monkeypatch)
        runner = LoopRunner()
        seen: list[tuple[int, Any]] = []
        items = [_item(1, "12377"), _item(2, "shdf"), _item(3, "12377")]
        SequentialReportAgent(_ns_cfg(tmp_path)).run(
            items, dry_run=True,
            on_item=lambda i, n, it: seen.append((i, it["entry_id"])),
            runner=runner,
        )
        assert [p[0] for p in hits] == ["12377", "shdf", "12377"]
        assert [p[1]["entry_id"] for p in hits] == [1, 2, 3]
        assert seen == [(1, 1), (2, 2), (3, 3)]

    def test_disabled_when_none_no_planner_kwargs(self, tmp_path):
        """on_item 未启用:引擎 kwargs 不含计划器包装(纯净透传)。"""
        runner = RecRunner()
        SequentialReportAgent(_ns_cfg(tmp_path)).run([_item(1)], runner=runner)
        kwargs = runner.calls[0]["kwargs"]
        assert "plan_12377" not in kwargs
        assert "plan_shdf" not in kwargs


# ---------------------------------------------------------------------------
# SessionExecutor 路径(executor_cls / browser_session_reuse)
# ---------------------------------------------------------------------------
class TestSessionExecutorPath:
    def test_cls_instantiated_once_run_n_close_once(self, tmp_path):
        """executor_cls:实例化恰 1 次、逐条 run N 次、close 恰 1 次。"""
        runner = LoopRunner()
        SequentialReportAgent(_ns_cfg(tmp_path), executor_cls=FakeSession).run(
            [_item(1), _item(2), _item(3)], dry_run=True, runner=runner,
        )
        assert len(FakeSession.made) == 1
        inst = FakeSession.made[0]
        assert len(inst.runs) == 3
        assert inst.closed == 1

    def test_wrapper_drops_auto_confirm_keeps_default_false(self, tmp_path):
        """包装器只透传 dry_run:会话 run 收到的 auto_confirm 保持缺省 False。"""
        runner = LoopRunner()
        SequentialReportAgent(_ns_cfg(tmp_path), executor_cls=FakeSession).run(
            [_item(1), _item(2)], dry_run=True, runner=runner,
        )
        for call in FakeSession.made[0].runs:
            assert call["auto_confirm"] is False

    def test_wrapper_forwards_dry_run(self, tmp_path):
        runner = LoopRunner()
        SequentialReportAgent(_ns_cfg(tmp_path), executor_cls=FakeSession).run(
            [_item(1)], dry_run=False, runner=runner,
        )
        assert FakeSession.made[0].runs[0]["dry_run"] is False

    def test_reuse_true_resolves_default_session_cls(self, tmp_path, monkeypatch):
        """browser_session_reuse=True → 惰性解析会话类并走包装路径。"""
        monkeypatch.setattr(seq, "_session_executor_cls", FakeSession)
        runner = LoopRunner()
        cfg = _ns_cfg(tmp_path, reuse=True)
        out = SequentialReportAgent(cfg).run([_item(1), _item(2)], dry_run=True, runner=runner)
        assert len(FakeSession.made) == 1
        assert FakeSession.made[0].closed == 1
        assert out["submitted"] == 0   # dry_run=True → 未真实提交

    def test_reuse_false_no_executor_kw(self, tmp_path, monkeypatch):
        """复用关闭:不传 executor(run_batch 落回缺省 executor_playwright)。"""
        monkeypatch.setattr(seq, "_session_executor_cls", BoomSession)
        runner = RecRunner()
        SequentialReportAgent(_ns_cfg(tmp_path, reuse=False)).run([_item(1)], runner=runner)
        assert "executor" not in runner.calls[0]["kwargs"]

    def test_reuse_attr_missing_defaults_off(self, tmp_path, monkeypatch):
        """鸭子 cfg 缺 browser_session_reuse 字段 → 按关闭处理。"""
        monkeypatch.setattr(seq, "_session_executor_cls", BoomSession)
        runner = RecRunner()
        cfg = types.SimpleNamespace(data_dir=str(tmp_path))
        SequentialReportAgent(cfg).run([_item(1)], runner=runner)
        assert "executor" not in runner.calls[0]["kwargs"]

    def test_injected_executor_beats_cls_and_default(self, tmp_path, monkeypatch):
        """注入 executor 最高优先:executor_cls 与复用缺省都不触发。"""
        monkeypatch.setattr(seq, "_session_executor_cls", BoomSession)
        runner = LoopRunner()
        ex = FakeExecutor()
        cfg = _ns_cfg(tmp_path, reuse=True)
        SequentialReportAgent(cfg, executor_cls=BoomSession).run(
            [_item(1)], dry_run=True, executor=ex, runner=runner,
        )
        assert ex.n == 1
        assert runner.calls[0]["kwargs"]["executor"] is ex

    def test_run_executor_cls_overrides_constructor(self, tmp_path):
        """run 处 executor_cls 优先于构造处。"""
        runner = LoopRunner()
        SequentialReportAgent(_ns_cfg(tmp_path), executor_cls=BoomSession).run(
            [_item(1)], dry_run=True, executor_cls=FakeSession, runner=runner,
        )
        assert len(FakeSession.made) == 1

    def test_close_still_called_when_engine_raises(self, tmp_path):
        """引擎异常时 finally 兜底:会话仍 close(恰 1 次),异常原样上抛。"""
        runner = RecRunner(exc=RuntimeError("引擎炸了"))
        with pytest.raises(RuntimeError, match="引擎炸了"):
            SequentialReportAgent(_ns_cfg(tmp_path), executor_cls=FakeSession).run(
                [_item(1)], dry_run=True, runner=runner,
            )
        assert len(FakeSession.made) == 1
        assert FakeSession.made[0].closed == 1

    def test_close_failure_swallowed(self, tmp_path):
        """close 自身异常只告警:批量结果照常返回。"""
        runner = LoopRunner()

        class BadCloseSession(FakeSession):
            def close(self) -> None:
                super().close()
                raise RuntimeError("close 炸了")

        out = SequentialReportAgent(_ns_cfg(tmp_path), executor_cls=BadCloseSession).run(
            [_item(1)], dry_run=True, runner=runner,
        )
        assert out["submitted"] == 0
        assert BadCloseSession.made[-1].closed == 1


# ---------------------------------------------------------------------------
# 结案报告渲染(A114 batch_report)
# ---------------------------------------------------------------------------
class TestReportRendering:
    def test_batch_id_renders_report_to_data_dir(self, tmp_path):
        """batch_id 传入 → 渲染到 cfg.data_dir/finish_report_{id}.html 并回填路径。"""
        runner = RecRunner(summary=_zero_summary(submitted=1))
        renderer = FakeRenderer()
        cfg = _ns_cfg(tmp_path)
        out = SequentialReportAgent(cfg).run(
            [_item(1)], dry_run=False, batch_id=42, runner=runner, renderer=renderer,
        )
        expected = str(Path(cfg.data_dir) / "finish_report_42.html")
        assert len(renderer.calls) == 1
        call = renderer.calls[0]
        assert call["batch_id"] == 42
        assert call["summary"] == out["result"]      # 摘要原样交给渲染器
        assert call["out_path"] == expected
        assert out["report_path"] == expected

    def test_no_batch_id_no_render(self, tmp_path):
        runner = RecRunner()
        renderer = FakeRenderer()
        out = SequentialReportAgent(_ns_cfg(tmp_path)).run(
            [_item(1)], runner=runner, renderer=renderer,
        )
        assert renderer.calls == []
        assert out["report_path"] is None

    def test_render_failure_degrades_to_none(self, tmp_path):
        """渲染失败只降级告警:批量结果照常返回,report_path=None。"""
        runner = RecRunner(summary=_zero_summary(submitted=1))
        renderer = FakeRenderer(exc=OSError("磁盘满了"))
        out = SequentialReportAgent(_ns_cfg(tmp_path)).run(
            [_item(1)], batch_id=7, runner=runner, renderer=renderer,
        )
        assert out["submitted"] == 1
        assert out["report_path"] is None

    def test_renderer_from_constructor(self, tmp_path):
        runner = RecRunner()
        renderer = FakeRenderer()
        SequentialReportAgent(_ns_cfg(tmp_path), renderer=renderer).run(
            [_item(1)], batch_id=3, runner=runner,
        )
        assert len(renderer.calls) == 1

    def test_real_a114_renderer_writes_html(self, tmp_path):
        """真 A114 渲染器(纯标准库):HTML 落盘、含批次号与固定结论。"""
        cfg = _real_cfg(tmp_path)
        runner = RecRunner(summary=_zero_summary(submitted=1))
        out = SequentialReportAgent(cfg).run(
            [_item(1)], dry_run=True, batch_id="BAT-1", runner=runner,
        )
        path = Path(cfg.data_dir) / "finish_report_BAT-1.html"
        assert out["report_path"] == str(path)
        assert path.is_file()
        text = path.read_text(encoding="utf-8")
        assert "批量举报结案报告" in text
        assert "BAT-1" in text
        assert "每一条均经" in text and "人工门" in text   # 红线 36 固定结论


# ---------------------------------------------------------------------------
# 遥测
# ---------------------------------------------------------------------------
class TestTelemetry:
    def test_run_counter_and_submitted_gauge(self, tmp_path):
        telemetry.reset()
        runner = RecRunner(summary=_zero_summary(submitted=2))
        SequentialReportAgent(_ns_cfg(tmp_path)).run([_item(1), _item(2)], runner=runner)
        snap = telemetry.snapshot()
        assert snap["counters"].get("seq_report.run") == 1.0
        assert snap["gauges"].get("seq_report.submitted") == 2.0

    def test_gauge_zero_on_empty_batch(self, tmp_path):
        telemetry.reset()
        SequentialReportAgent(_ns_cfg(tmp_path)).run([], runner=RecRunner())
        snap = telemetry.snapshot()
        assert snap["gauges"].get("seq_report.submitted") == 0.0


# ---------------------------------------------------------------------------
# 红线 36:源码静态守卫
# ---------------------------------------------------------------------------
class TestRedline36:
    SOURCE = pathlib.Path(seq.__file__).read_text(encoding="utf-8")

    def test_no_auto_confirm_true_literal(self):
        """静态守卫:源码不得出现把 auto_confirm 置 True 的任何字样。"""
        assert "auto_confirm=True" not in self.SOURCE
        assert "auto_confirm = True" not in self.SOURCE

    def test_no_review_queue_or_attestation_touch(self):
        """本代理不触碰复核队列 / 声明链(声明由上游 A110/A111 人工完成)。"""
        assert "netsentinel.decision" not in self.SOURCE
        assert "is_attested" not in self.SOURCE
        assert "BatchReview" not in self.SOURCE

    def test_docstring_declares_redline_36(self):
        """契约要求:模块 docstring 明示红线 36(无自主提交权)。"""
        assert "红线 36" in seq.__doc__
        assert "红线 36" in SequentialReportAgent.__doc__

    def test_engine_never_receives_auto_confirm_any_path(self, tmp_path):
        """任意注入组合(注入 executor / 会话类 / on_item / dry_run)下,
        引擎 kwargs 均无 auto_confirm(红线 36 运行时全覆盖)。"""
        runner = LoopRunner()
        agent = SequentialReportAgent(
            _ns_cfg(tmp_path, reuse=True), executor_cls=FakeSession
        )
        agent.run(
            [_item(1), _item(2, "shdf")], dry_run=True,
            on_item=lambda *a: None, runner=runner,
        )
        assert "auto_confirm" not in runner.calls[0]["kwargs"]
        for call in FakeSession.made[0].runs:
            assert call["auto_confirm"] is False


# ---------------------------------------------------------------------------
# 真 A112 run_batch 集成(仍全离线:假门户 / 假执行器 / tmp 数据目录)
# ---------------------------------------------------------------------------
class TestRealRunBatchIntegration:
    def test_dry_run_two_items_end_to_end(self, tmp_path, monkeypatch):
        """干跑真链路:逐条 running→skipped,submitted=0,状态落账。"""
        _install_portals(monkeypatch)
        cfg = _real_cfg(tmp_path)
        ex = FakeExecutor()
        st = FakeState()
        seen: list[int] = []
        out = SequentialReportAgent(cfg).run(
            [_item(1), _item(2)], executor=ex, state=st, dry_run=True,
            on_item=lambda i, n, it: seen.append(i),
        )
        assert out["submitted"] == 0
        assert ex.n == 2
        assert seen == [1, 2]
        assert st.of(1) == [("running", ""), ("skipped", "干跑/未真实提交")]
        assert out["result"]["results"][0]["ok"] is True

    def test_session_reuse_path_with_real_engine(self, tmp_path, monkeypatch):
        """会话复用 + 真引擎:1 个会话实例服务 2 条、提交成功、close 收尾。"""
        _install_portals(monkeypatch)
        monkeypatch.setattr(seq, "_session_executor_cls", FakeSession)
        cfg = _real_cfg(tmp_path, browser_session_reuse=True)
        st = FakeState()
        out = SequentialReportAgent(cfg).run(
            [_item(1), _item(2)], state=st, dry_run=False,
        )
        assert out["submitted"] == 2
        assert st.of(2)[-1] == ("submitted", "")
        assert len(FakeSession.made) == 1
        assert len(FakeSession.made[0].runs) == 2
        assert FakeSession.made[0].closed == 1
        # 真引擎恒以 auto_confirm=False 调用执行器(包装器丢弃后仍是缺省 False)
        for call in FakeSession.made[0].runs:
            assert call["auto_confirm"] is False

    def test_stop_halts_before_first_item_no_on_item(self, tmp_path, monkeypatch):
        """stop 立即为真:paused 返回、on_item 不触发(条目未进入执行)。"""
        _install_portals(monkeypatch)
        cfg = _real_cfg(tmp_path)
        seen: list[int] = []
        out = SequentialReportAgent(cfg).run(
            [_item(1), _item(2)], dry_run=True, stop=lambda: True,
            on_item=lambda i, n, it: seen.append(i),
        )
        assert out["result"]["paused"] is True
        assert seen == []
        assert out["result"]["results"] == []

    def test_rate_limit_halts_and_skips_later_callbacks(self, tmp_path, monkeypatch):
        """频控挂起(红线 26):后续条目不执行、不回调,摘要如实挂起。"""
        _install_portals(monkeypatch)
        cfg = _real_cfg(tmp_path)

        class OneShotRate:
            """首条放行、之后拒绝的频控替身(runner 闭包注入真 run_batch)。"""

            def __init__(self) -> None:
                self.n = 0

            def can_submit(self) -> tuple[bool, str]:
                self.n += 1
                if self.n > 1:
                    return False, "距上次提交仅 0 秒"
                return True, ""

            def record(self) -> None:
                pass

        from netsentinel.submit.batch_submit import run_batch

        rate = OneShotRate()

        def runner(items: Any, cfg_: Any, **kw: Any) -> dict[str, Any]:
            return run_batch(items, cfg_, rate=rate, **kw)

        seen: list[int] = []
        out = SequentialReportAgent(cfg).run(
            [_item(1), _item(2)], dry_run=False, executor=FakeExecutor(),
            on_item=lambda i, n, it: seen.append(i), runner=runner,
        )
        assert out["result"]["rate_limited"] is True
        assert out["result"]["submitted"] == 1
        assert seen == [1]        # 第 2 条被频控挡住,不回调
