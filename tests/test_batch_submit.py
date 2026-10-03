# -*- coding: utf-8 -*-
"""A112 批量顺序提交引擎(run_batch)单元测试:全部离线、全注入。

覆盖:三条顺序全提交(state/rate/审计三重落账)、中途失败继续、频控首条即拒
与中途额度耗尽两种挂起、stop 注入暂停、超上限/缺字段/非列表 ValueError、
dry_run 透传与条间 sleep、auto_confirm 恒 False(运行时捕获 + 源码静态守卫,
红线 24)、审计事件字段、空 items 零值、12377/shdf 计划器分派、缺省惰性
依赖(executor/planner/RateLimiter)构造参数(红线 26)。

零 playwright、零网络、零真实门户;sleep/rate/state/executor/planner 全 fake。
"""
from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

import netsentinel.submit.batch_submit as batch_submit
from netsentinel.contracts import Config, ExecutionResult, Portal
from netsentinel.submit.batch_submit import run_batch


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
        return [(status, error) for eid, status, error in self.calls if eid == entry_id]


class FakeRate:
    """频控器替身:allow 序列按 can_submit 调用次序消费,耗尽后重复末值。"""

    def __init__(
        self,
        allow: list[bool] | None = None,
        reason: str = "距上次提交仅 10 秒,小于最小间隔 90 秒",
    ) -> None:
        self.allow = list(allow) if allow is not None else None
        self.reason = reason
        self.can_calls = 0
        self.records = 0

    def can_submit(self) -> tuple[bool, str]:
        self.can_calls += 1
        if self.allow is None:
            return (True, "")
        idx = min(self.can_calls - 1, len(self.allow) - 1)
        return (True, "") if self.allow[idx] else (False, self.reason)

    def record(self) -> None:
        self.records += 1


class FakeExecutor:
    """执行器替身:捕获调用参数;outcome 支持 submitted/ok/失败步骤名/异常。"""

    def __init__(self, outcomes: list[Any] | None = None) -> None:
        self.outcomes = list(outcomes or [])
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, plan: Any, cfg: Config, *, auto_confirm: bool = None, dry_run: bool = None, **kw: Any
    ) -> Any:
        self.calls.append(
            {"plan": plan, "cfg": cfg, "auto_confirm": auto_confirm, "dry_run": dry_run}
        )
        outcome = self.outcomes.pop(0) if self.outcomes else "submitted"
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "submitted":
            return ExecutionResult(portal="12377", ok=True, submitted=True)
        if outcome == "ok":  # 执行成功但未真实提交(干跑语义)
            return ExecutionResult(portal="12377", ok=True, submitted=False)
        stopped = outcome if isinstance(outcome, str) else str(outcome)
        return ExecutionResult(portal="12377", ok=False, submitted=False, stopped_at=stopped)

    @property
    def n(self) -> int:
        return len(self.calls)


class FakePlanner:
    """计划器替身:记录 (entry, cfg);exc 非空则抛出。"""

    def __init__(self, name: str = "plan", exc: BaseException | None = None) -> None:
        self.name = name
        self.exc = exc
        self.calls: list[tuple[Any, Config]] = []

    def __call__(self, entry: Any, cfg: Config) -> Any:
        self.calls.append((entry, cfg))
        if self.exc is not None:
            raise self.exc
        return {"planner": self.name, "entry_id": entry.get("entry_id")}

    @property
    def n(self) -> int:
        return len(self.calls)


class SleepRecorder:
    """sleep 替身:记录每次等待秒数。"""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def stop_at(n: int):
    """stop 回调替身:第 n 次检查起返回 True。"""
    count = 0

    def _stop() -> bool:
        nonlocal count
        count += 1
        return count >= n

    return _stop


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def make_cfg(tmp_path: pathlib.Path, **overrides: Any) -> Config:
    base: dict[str, Any] = dict(
        data_dir=str(tmp_path / "data"),
        audit_path=str(tmp_path / "audit" / "audit.jsonl"),
        dry_run_default=True,
        batch_max_items=20,
        batch_item_interval_s=90,
        submit_min_interval_s=60,
        submit_max_per_day=5,
    )
    base.update(overrides)
    return Config(**base)


def make_items(n: int, portal: Any = "12377") -> list[dict[str, Any]]:
    return [
        {"entry_id": i, "group_name": f"案件组{i}", "portal": portal}
        for i in range(1, n + 1)
    ]


def make_deps(outcomes: list[Any] | None = None, allow: list[bool] | None = None) -> dict[str, Any]:
    """一套标准 fake 依赖(默认 dry_run=True,后续可按用例覆盖)。"""
    return {
        "executor": FakeExecutor(outcomes),
        "plan_12377": FakePlanner("p12377"),
        "plan_shdf": FakePlanner("pshdf"),
        "rate": FakeRate(allow),
        "state": FakeState(),
        "dry_run": True,
        "sleep": SleepRecorder(),
    }


def kw(deps: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """把 make_deps 的结果转成 run_batch 关键字参数,并支持覆盖。"""
    kwargs = dict(deps)
    kwargs.update(overrides)
    return kwargs


def read_audit(cfg: Config) -> list[dict[str, Any]]:
    path = pathlib.Path(cfg.audit_path)
    assert path.exists(), f"审计文件未生成:{path}"
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ---------------------------------------------------------------------------
# 前置校验(红线 26 / 结构校验)
# ---------------------------------------------------------------------------


def test_empty_items_returns_zero_summary(tmp_path: pathlib.Path) -> None:
    """空 items:返回零值摘要,不调用执行器,也不构造任何落盘依赖。"""
    cfg = make_cfg(tmp_path)
    deps = make_deps()
    summary = run_batch([], cfg, **kw(deps))
    assert summary["submitted"] == 0
    assert summary["failed"] == 0
    assert summary["rate_limited"] is False
    assert summary["paused"] is False
    assert summary["results"] == []
    assert deps["executor"].n == 0
    # 惰性依赖未构造:不建 data 目录、不写审计
    assert not (tmp_path / "data").exists()
    assert not pathlib.Path(cfg.audit_path).exists()


def test_items_over_limit_raises_value_error(tmp_path: pathlib.Path) -> None:
    """条数超过 batch_max_items → 中文 ValueError(红线 26),绝不执行。"""
    cfg = make_cfg(tmp_path, batch_max_items=2)
    deps = make_deps()
    with pytest.raises(ValueError) as ei:
        run_batch(make_items(3), cfg, **kw(deps))
    msg = str(ei.value)
    assert "3" in msg and "上限" in msg and "2" in msg
    assert deps["executor"].n == 0


def test_items_not_a_list_raises(tmp_path: pathlib.Path) -> None:
    """items 传字符串/字典 → 中文 ValueError。"""
    cfg = make_cfg(tmp_path)
    for bad in ("not-a-list", {"entry_id": 1}):
        with pytest.raises(ValueError, match="列表"):
            run_batch(bad, cfg, executor=FakeExecutor(), dry_run=True)  # type: ignore[arg-type]


def test_item_missing_required_key_raises(tmp_path: pathlib.Path) -> None:
    """缺 group_name / portal 等必填字段 → 中文 ValueError 指明序号与字段。"""
    cfg = make_cfg(tmp_path)
    items = [
        {"entry_id": 1, "group_name": "组A", "portal": "12377"},
        {"entry_id": 2, "portal": "12377"},
    ]
    with pytest.raises(ValueError) as ei:
        run_batch(items, cfg, executor=FakeExecutor(), dry_run=True)
    assert "第 2 条" in str(ei.value) and "group_name" in str(ei.value)


def test_item_missing_portal_raises(tmp_path: pathlib.Path) -> None:
    """缺 portal 字段同样拒绝(空串视同缺失)。"""
    cfg = make_cfg(tmp_path)
    items = [{"entry_id": 1, "group_name": "组A", "portal": ""}]
    with pytest.raises(ValueError, match="portal"):
        run_batch(items, cfg, executor=FakeExecutor(), dry_run=True)


def test_item_not_mapping_raises(tmp_path: pathlib.Path) -> None:
    """条目不是字典(如整数)→ 中文 ValueError。"""
    cfg = make_cfg(tmp_path)
    with pytest.raises(ValueError, match="映射"):
        run_batch([1, 2], cfg, executor=FakeExecutor(), dry_run=True)  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# 顺序执行主流程
# ---------------------------------------------------------------------------


def test_three_items_all_submitted(tmp_path: pathlib.Path) -> None:
    """三条顺序执行全提交:state 三次 running→submitted、rate.record 三次、审计三行。"""
    cfg = make_cfg(tmp_path)
    deps = make_deps(["submitted"] * 3)
    summary = run_batch(make_items(3), cfg, **kw(deps))
    assert summary["submitted"] == 3
    assert summary["failed"] == 0
    assert summary["rate_limited"] is False
    assert summary["paused"] is False
    assert summary["note"] == ""
    assert [r["entry_id"] for r in summary["results"]] == [1, 2, 3]
    assert all(r["ok"] and r["submitted"] and r["error"] == "" for r in summary["results"])
    state: FakeState = deps["state"]
    for eid in (1, 2, 3):
        assert [s for s, _ in state.of(eid)] == ["running", "submitted"]
    rate: FakeRate = deps["rate"]
    assert rate.records == 3 and rate.can_calls == 3
    events = read_audit(cfg)
    assert len(events) == 3
    assert [e["entry_id"] for e in events] == [1, 2, 3]


def test_second_item_fails_third_continues(tmp_path: pathlib.Path) -> None:
    """第二条执行失败(执行器返回失败)→ 记中文原因后继续第三条。"""
    cfg = make_cfg(tmp_path)
    deps = make_deps(["submitted", "验证码人工门", "submitted"])
    summary = run_batch(make_items(3), cfg, **kw(deps))
    assert summary["submitted"] == 2
    assert summary["failed"] == 1
    assert summary["rate_limited"] is False and summary["paused"] is False
    row = summary["results"][1]
    assert row["ok"] is False and row["submitted"] is False
    assert "执行失败" in row["error"] and "验证码人工门" in row["error"]
    state: FakeState = deps["state"]
    assert [s for s, _ in state.of(2)] == ["running", "failed"]
    assert "执行失败" in state.of(2)[1][1]
    assert [s for s, _ in state.of(3)] == ["running", "submitted"]
    assert deps["executor"].n == 3


def test_executor_exception_fails_item_and_continues(tmp_path: pathlib.Path) -> None:
    """执行器抛异常 → 该条 failed(中文"执行异常"),后续条目继续。"""
    cfg = make_cfg(tmp_path)
    deps = make_deps([ValueError("浏览器启动失败"), "submitted"])
    summary = run_batch(make_items(2), cfg, **kw(deps))
    assert summary["failed"] == 1 and summary["submitted"] == 1
    assert "执行异常" in summary["results"][0]["error"]
    assert "浏览器启动失败" in summary["results"][0]["error"]
    assert summary["results"][1]["submitted"] is True


def test_executor_returning_none_fails_item(tmp_path: pathlib.Path) -> None:
    """执行器返回 None(异常实现)→ 该条按失败处理,不崩批。"""
    cfg = make_cfg(tmp_path)

    def none_executor(plan: Any, cfg: Config, *, auto_confirm: bool, dry_run: bool) -> Any:
        return None

    summary = run_batch(make_items(1), cfg, executor=none_executor,
                        plan_12377=FakePlanner(), plan_shdf=FakePlanner(),
                        rate=FakeRate(), state=FakeState(), dry_run=True,
                        sleep=SleepRecorder())
    assert summary["failed"] == 1
    assert "执行器未返回" in summary["results"][0]["error"]


def test_planner_exception_fails_item_and_continues(tmp_path: pathlib.Path) -> None:
    """计划器抛异常 → 该条 failed,执行器未被该条调用,后续继续。"""
    cfg = make_cfg(tmp_path)
    deps = make_deps(["submitted"])
    deps["plan_12377"] = FakePlanner("bad", exc=RuntimeError("表单模型未就位"))
    summary = run_batch(make_items(1), cfg, **kw(deps))
    assert summary["failed"] == 1 and summary["submitted"] == 0
    assert "执行异常" in summary["results"][0]["error"]
    assert "表单模型未就位" in summary["results"][0]["error"]
    assert deps["executor"].n == 0


def test_ok_not_submitted_marks_skipped(tmp_path: pathlib.Path) -> None:
    """干跑语义(ok=True、submitted=False)→ 该条 skipped,不算失败。"""
    cfg = make_cfg(tmp_path)
    deps = make_deps(["ok"])
    summary = run_batch(make_items(1), cfg, **kw(deps))
    assert summary["submitted"] == 0 and summary["failed"] == 0
    row = summary["results"][0]
    assert row["ok"] is True and row["submitted"] is False and row["error"] == ""
    state: FakeState = deps["state"]
    assert state.of(1) == [("running", ""), ("skipped", "干跑/未真实提交")]
    # 未真实提交:不记频控、不写审计
    assert deps["rate"].records == 0
    assert not pathlib.Path(cfg.audit_path).exists()


# ---------------------------------------------------------------------------
# 频控挂起(红线 26)
# ---------------------------------------------------------------------------


def test_rate_denied_on_first_suspends_whole_batch(tmp_path: pathlib.Path) -> None:
    """频控第一条即拒 → 该条 rate_limited,整体挂起返回,后续条目未执行。"""
    cfg = make_cfg(tmp_path)
    deps = make_deps(allow=[False])
    summary = run_batch(make_items(3), cfg, **kw(deps))
    assert summary["rate_limited"] is True
    assert summary["paused"] is False
    assert summary["submitted"] == 0 and summary["failed"] == 0
    assert len(summary["results"]) == 1
    row = summary["results"][0]
    assert row["ok"] is False and row["submitted"] is False
    assert "额度/间隔限制" in row["error"] and "可续批" in row["error"]
    assert "可续批" in summary["note"]
    assert deps["executor"].n == 0
    assert deps["rate"].can_calls == 1
    state: FakeState = deps["state"]
    assert [s for s, _ in state.of(1)] == ["running", "rate_limited"]
    assert "间隔" in state.of(1)[1][1]
    # 未执行的条目没有任何状态记录
    assert state.of(2) == [] and state.of(3) == []


def test_rate_exhausted_midway_suspends(tmp_path: pathlib.Path) -> None:
    """额度中途耗尽(第二条被拒)→ 已提交 1 条后挂起,可续批(真实模式)。"""
    cfg = make_cfg(tmp_path)
    deps = make_deps(["submitted"], allow=[True, False])
    summary = run_batch(make_items(3), cfg, **kw(deps, dry_run=False))
    assert summary["submitted"] == 1
    assert summary["rate_limited"] is True
    assert summary["paused"] is False
    assert len(summary["results"]) == 2
    assert summary["results"][0]["submitted"] is True
    assert summary["results"][1]["ok"] is False
    state: FakeState = deps["state"]
    assert [s for s, _ in state.of(1)] == ["running", "submitted"]
    assert [s for s, _ in state.of(2)] == ["running", "rate_limited"]
    assert deps["rate"].records == 1
    assert deps["executor"].n == 1
    # 真实模式:仅在第一条后等了一次条间间隔,挂起后不再等待
    assert deps["sleep"].calls == [90.0]


def test_rate_reason_recorded_in_state_error(tmp_path: pathlib.Path) -> None:
    """频控拒绝的中文原因完整透传到状态 error 与结果 error。"""
    cfg = make_cfg(tmp_path)
    state = FakeState()
    rate = FakeRate(allow=[False], reason="当日(本地时区)已提交 5 次,达到每日上限 5 次,请明日再提交")
    summary = run_batch(make_items(1), cfg, executor=FakeExecutor(),
                        plan_12377=FakePlanner(), plan_shdf=FakePlanner(),
                        rate=rate, state=state, dry_run=True, sleep=SleepRecorder())
    assert summary["rate_limited"] is True
    assert "每日上限" in summary["results"][0]["error"]
    assert "每日上限" in state.of(1)[1][1]


# ---------------------------------------------------------------------------
# stop 暂停
# ---------------------------------------------------------------------------


def test_stop_before_first_pauses_with_zero_done(tmp_path: pathlib.Path) -> None:
    """stop 第一条前触发 → 立即暂停,零执行,已保留空结果。"""
    cfg = make_cfg(tmp_path)
    deps = make_deps()
    summary = run_batch(make_items(2), cfg, stop=stop_at(1), **kw(deps))
    assert summary["paused"] is True
    assert summary["rate_limited"] is False
    assert summary["submitted"] == 0 and summary["failed"] == 0
    assert summary["results"] == []
    assert "暂停" in summary["note"]
    assert deps["executor"].n == 0
    assert deps["state"].calls == []


def test_stop_before_third_preserves_completed(tmp_path: pathlib.Path) -> None:
    """stop 第三条前触发 → 前两条结果完整保留,第三条零痕迹。"""
    cfg = make_cfg(tmp_path)
    deps = make_deps(["submitted", "submitted", "submitted"])
    summary = run_batch(make_items(3), cfg, stop=stop_at(3), **kw(deps))
    assert summary["paused"] is True
    assert summary["submitted"] == 2
    assert [r["entry_id"] for r in summary["results"]] == [1, 2]
    assert deps["executor"].n == 2
    state: FakeState = deps["state"]
    assert state.of(3) == []


# ---------------------------------------------------------------------------
# dry_run / sleep(红线 24 的 dry 侧与红线 26 的条间间隔)
# ---------------------------------------------------------------------------


def test_dry_run_param_overrides_cfg_no_sleep(tmp_path: pathlib.Path) -> None:
    """dry_run=True 显式覆盖 cfg.dry_run_default=False → 透传执行器且零 sleep。"""
    cfg = make_cfg(tmp_path, dry_run_default=False)
    deps = make_deps(["submitted"] * 2)
    summary = run_batch(make_items(2), cfg, **kw(deps))
    assert summary["submitted"] == 2
    for call in deps["executor"].calls:
        assert call["dry_run"] is True
    assert deps["sleep"].calls == []


def test_dry_run_none_follows_cfg_default(tmp_path: pathlib.Path) -> None:
    """dry_run=None → 取 cfg.dry_run_default(True:干跑透传、无 sleep)。"""
    cfg = make_cfg(tmp_path, dry_run_default=True)
    deps = make_deps(["submitted"])
    summary = run_batch(make_items(1), cfg, **kw(deps, dry_run=None))
    assert summary["submitted"] == 1
    assert deps["executor"].calls[0]["dry_run"] is True
    assert deps["sleep"].calls == []


def test_real_mode_sleeps_interval_between_items(tmp_path: pathlib.Path) -> None:
    """真实模式:条间等待 batch_item_interval_s,共 n-1 次,末条后不等。"""
    cfg = make_cfg(tmp_path, dry_run_default=False, batch_item_interval_s=7,
                   submit_min_interval_s=5)
    deps = make_deps(["submitted"] * 3)
    summary = run_batch(make_items(3), cfg, **kw(deps, dry_run=False))
    assert summary["submitted"] == 3
    assert deps["sleep"].calls == [7.0, 7.0]
    for call in deps["executor"].calls:
        assert call["dry_run"] is False


def test_single_item_real_mode_never_sleeps(tmp_path: pathlib.Path) -> None:
    """真实模式单条:无"条间",不 sleep。"""
    cfg = make_cfg(tmp_path, dry_run_default=False)
    deps = make_deps(["submitted"])
    summary = run_batch(make_items(1), cfg, **kw(deps, dry_run=False))
    assert summary["submitted"] == 1
    assert deps["sleep"].calls == []


def test_real_mode_forced_when_cfg_default_dry(tmp_path: pathlib.Path) -> None:
    """cfg 默认干跑但批量层强制 dry_run=False → 执行器收到 False 且 sleep 生效。"""
    cfg = make_cfg(tmp_path, dry_run_default=True, batch_item_interval_s=11)
    deps = make_deps(["submitted"] * 2)
    summary = run_batch(make_items(2), cfg, **kw(deps, dry_run=False))
    assert summary["submitted"] == 2
    assert all(c["dry_run"] is False for c in deps["executor"].calls)
    assert deps["sleep"].calls == [11.0]


# ---------------------------------------------------------------------------
# 红线 24:auto_confirm 恒 False
# ---------------------------------------------------------------------------


def test_auto_confirm_always_false_runtime(tmp_path: pathlib.Path) -> None:
    """任何路径(干跑/真实/失败后续)下执行器收到的 auto_confirm 恒为 False。"""
    cfg = make_cfg(tmp_path, dry_run_default=False)
    deps = make_deps(["submitted", "失败步骤", "submitted"])
    run_batch(make_items(3), cfg, **kw(deps, dry_run=False))
    assert deps["executor"].n == 3
    for call in deps["executor"].calls:
        assert call["auto_confirm"] is False


def test_auto_confirm_true_absent_from_source() -> None:
    """静态守卫:batch_submit.py 源码中不得出现把 auto_confirm 置 True 的字样(红线 24)。"""
    source = pathlib.Path(batch_submit.__file__).read_text(encoding="utf-8")
    assert "auto_confirm=True" not in source
    assert "auto_confirm = True" not in source


# ---------------------------------------------------------------------------
# 审计留痕
# ---------------------------------------------------------------------------


def test_audit_event_fields(tmp_path: pathlib.Path) -> None:
    """提交成功的审计事件 batch_submit 字段齐全(含 auto_confirm=False 留痕)。"""
    cfg = make_cfg(tmp_path)
    deps = make_deps(["submitted"])
    run_batch(make_items(1), cfg, **kw(deps))
    events = read_audit(cfg)
    assert len(events) == 1
    ev = events[0]
    assert ev["event"] == "batch_submit"
    assert ev["entry_id"] == 1
    assert ev["group_name"] == "案件组1"
    assert ev["portal"] == "12377"
    assert ev["dry_run"] is True
    assert ev["auto_confirm"] is False
    assert ev["index"] == 1 and ev["total"] == 1
    assert "ts" in ev and ev["ts"]


def test_no_audit_written_when_paused_before_any_submit(tmp_path: pathlib.Path) -> None:
    """暂停场景下未产生提交 → 不写审计文件。"""
    cfg = make_cfg(tmp_path)
    deps = make_deps(["submitted"])
    run_batch(make_items(1), cfg, stop=stop_at(1), **kw(deps))
    assert not pathlib.Path(cfg.audit_path).exists()


# ---------------------------------------------------------------------------
# 计划器分派
# ---------------------------------------------------------------------------


def test_planner_dispatch_by_portal(tmp_path: pathlib.Path) -> None:
    """portal=12377 → 12377 计划器;portal=shdf → shdf 计划器;条目原样透传。"""
    cfg = make_cfg(tmp_path)
    p12377, pshdf = FakePlanner("p12377"), FakePlanner("pshdf")
    executor = FakeExecutor(["submitted", "submitted"])
    items = [
        {"entry_id": 1, "group_name": "组A", "portal": "12377"},
        {"entry_id": 2, "group_name": "组B", "portal": "shdf"},
    ]
    summary = run_batch(items, cfg, executor=executor, plan_12377=p12377,
                        plan_shdf=pshdf, rate=FakeRate(), state=FakeState(),
                        dry_run=True, sleep=SleepRecorder())
    assert p12377.n == 1 and pshdf.n == 1
    assert p12377.calls[0][0] is items[0]
    assert pshdf.calls[0][0] is items[1]
    assert p12377.calls[0][1] is cfg and pshdf.calls[0][1] is cfg
    # 执行器拿到的正是对应计划器返回的计划对象
    assert executor.calls[0]["plan"]["planner"] == "p12377"
    assert executor.calls[1]["plan"]["planner"] == "pshdf"
    assert [r["portal"] for r in summary["results"]] == ["12377", "shdf"]


def test_portal_enum_values_accepted(tmp_path: pathlib.Path) -> None:
    """portal 字段也接受 Portal 枚举,结果行归一为纯字符串值。"""
    cfg = make_cfg(tmp_path)
    pshdf = FakePlanner("pshdf")
    items = [{"entry_id": 9, "group_name": "组枚举", "portal": Portal.SHDF}]
    summary = run_batch(items, cfg, executor=FakeExecutor(["submitted"]),
                        plan_12377=FakePlanner("p12377"), plan_shdf=pshdf,
                        rate=FakeRate(), state=FakeState(), dry_run=True,
                        sleep=SleepRecorder())
    assert pshdf.n == 1
    assert summary["results"][0]["portal"] == "shdf"


def test_unknown_portal_fails_item_batch_continues(tmp_path: pathlib.Path) -> None:
    """未知门户 → 该条中文失败("不支持的举报门户"),后续条目继续提交。"""
    cfg = make_cfg(tmp_path)
    deps = make_deps(["submitted"])
    items = [
        {"entry_id": 1, "group_name": "组A", "portal": "gov"},
        {"entry_id": 2, "group_name": "组B", "portal": "12377"},
    ]
    summary = run_batch(items, cfg, **kw(deps))
    assert summary["failed"] == 1 and summary["submitted"] == 1
    assert "不支持的举报门户" in summary["results"][0]["error"]
    assert summary["results"][1]["submitted"] is True
    assert deps["executor"].n == 1  # 仅第二条到了执行器


# ---------------------------------------------------------------------------
# 状态器与结果结构
# ---------------------------------------------------------------------------


def test_state_none_is_fine(tmp_path: pathlib.Path) -> None:
    """state=None:不落批次状态也能正常完成批量。"""
    cfg = make_cfg(tmp_path)
    deps = make_deps(["submitted"] * 2)
    summary = run_batch(make_items(2), cfg, **kw(deps, state=None))
    assert summary["submitted"] == 2


def test_result_row_schema(tmp_path: pathlib.Path) -> None:
    """结果行字段恰为 entry_id/group_name/ok/submitted/error/portal 六项。"""
    cfg = make_cfg(tmp_path)
    deps = make_deps(["submitted"])
    summary = run_batch(make_items(1), cfg, **kw(deps))
    assert set(summary["results"][0]) == {"entry_id", "group_name", "ok", "submitted", "error", "portal"}
    assert set(summary) == {"submitted", "failed", "rate_limited", "paused", "results", "note"}


def test_state_mark_order_running_then_terminal(tmp_path: pathlib.Path) -> None:
    """每条状态轨迹恒为 running → 终态(submitted/failed)。"""
    cfg = make_cfg(tmp_path)
    state = FakeState()
    items = make_items(3)
    items[1]["portal"] = "x"  # 第二条门户非法 → failed
    run_batch(items, cfg, executor=FakeExecutor(["submitted", "submitted"]),
              plan_12377=FakePlanner(), plan_shdf=FakePlanner(),
              rate=FakeRate(), state=state, dry_run=True, sleep=SleepRecorder())
    assert [s for s, _ in state.of(1)] == ["running", "submitted"]
    assert [s for s, _ in state.of(2)] == ["running", "failed"]
    assert [s for s, _ in state.of(3)] == ["running", "submitted"]


# ---------------------------------------------------------------------------
# 缺省惰性依赖(monkeypatch 真实模块属性,离线)
# ---------------------------------------------------------------------------


def test_default_executor_and_planners_resolved_lazily(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不注入 executor/planner 时惰性取真实模块属性(此处替换为离线 fake)。"""
    cfg = make_cfg(tmp_path)
    fake_exec = FakeExecutor(["submitted"])
    p12377, pshdf = FakePlanner("real12377"), FakePlanner("realshdf")
    monkeypatch.setattr("netsentinel.submit.executor_playwright.execute", fake_exec)
    monkeypatch.setattr("netsentinel.submit.portal_12377.plan_12377", p12377)
    monkeypatch.setattr("netsentinel.submit.portal_shdf.plan_shdf", pshdf)
    summary = run_batch(make_items(1), cfg, rate=FakeRate(), state=FakeState(),
                        dry_run=True, sleep=SleepRecorder())
    assert summary["submitted"] == 1
    assert fake_exec.n == 1 and fake_exec.calls[0]["auto_confirm"] is False
    assert p12377.n == 1 and pshdf.n == 0


def test_default_rate_limiter_uses_stricter_interval(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺省频控器:路径 data_dir/rate_limit.json,最小间隔取两者更严者,日额度照常(红线 26)。"""
    created: list[dict[str, Any]] = []

    class SpyRateLimiter:
        def __init__(self, path: str, min_interval_s: int, max_per_day: int) -> None:
            created.append(
                {"path": pathlib.Path(path), "min": min_interval_s, "max": max_per_day}
            )

        def can_submit(self) -> tuple[bool, str]:
            return (True, "")

        def record(self) -> None:
            pass

    monkeypatch.setattr("netsentinel.submit.rate_limit.RateLimiter", SpyRateLimiter)
    cfg = make_cfg(tmp_path, batch_item_interval_s=90, submit_min_interval_s=120,
                   submit_max_per_day=3)
    summary = run_batch(make_items(1), cfg, executor=FakeExecutor(["submitted"]),
                        plan_12377=FakePlanner(), plan_shdf=FakePlanner(),
                        state=FakeState(), dry_run=True, sleep=SleepRecorder())
    assert summary["submitted"] == 1
    assert len(created) == 1
    assert created[0]["path"] == tmp_path / "data" / "rate_limit.json"
    assert created[0]["min"] == 120  # max(90, 120):批量频控不得放宽
    assert created[0]["max"] == 3
