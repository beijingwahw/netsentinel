"""A59 自适应重扫(netsentinel.ops.adaptive)单元测试。

全部离线:被测对象是纯函数,"当前时间"一律显式注入(不注入 now 的
分支用上下夹逼断言),零网络、零真实 sleep、零时钟抖动依赖;
确定性(同输入同输出)单独成组验证。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from netsentinel import telemetry
from netsentinel.ops.adaptive import (
    MAX_INTERVAL_H,
    MIN_INTERVAL_H,
    next_run,
    plan_row,
    suggest_interval_hours,
    volatility,
)

BASE = 72  # 与 cfg.adaptive_base_interval_h 默认值一致

# 档位速查(便于下面选历史序列):
#   ["a", "b"]                 -> vol=1.0   高波动
#   ["a", "a", "b"]            -> vol=0.5   高波动(>=0.5 含边界)
#   ["a"] * 10 + ["b"]         -> vol=0.1   低波动(<=0.1 含边界,11 期)
#   ["a", "a", "a", "b", "b"]  -> vol=0.25  中间档
#   ["a", "a", "a"]            -> vol=0.0   低波动(3 期,可退避)


# ---------------------------------------------------------------------------
# volatility:变化频率
# ---------------------------------------------------------------------------
class TestVolatility:
    def test_all_same_is_zero(self) -> None:
        assert volatility(["fp1", "fp1", "fp1", "fp1"]) == 0.0

    def test_all_changed_is_one(self) -> None:
        assert volatility(["a", "b", "c", "d"]) == 1.0

    def test_mixed_half(self) -> None:
        # 3 期 1 变:1 / (3 - 1) = 0.5
        assert volatility(["a", "a", "b"]) == 0.5

    def test_mixed_two_thirds(self) -> None:
        assert volatility(["a", "b", "b", "c"]) == pytest.approx(2 / 3)

    def test_empty_and_none_are_zero(self) -> None:
        assert volatility([]) == 0.0
        assert volatility(None) == 0.0  # type: ignore[arg-type]

    def test_single_element_is_zero(self) -> None:
        assert volatility(["only"]) == 0.0

    def test_returns_float_in_unit_range(self) -> None:
        for history in ([], ["a"], ["a", "b"], ["a"] * 50 + ["b"]):
            v = volatility(history)
            assert isinstance(v, float)
            assert 0.0 <= v <= 1.0

    def test_none_fingerprint_entries_treated_as_empty_string(self) -> None:
        # 脏数据防御:None 与 "" 视为同一指纹
        assert volatility([None, "", "x"]) == 0.5  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# suggest_interval_hours:分档与钳制
# ---------------------------------------------------------------------------
class TestSuggestIntervalHours:
    # -- 历史不足:保守返回 base ------------------------------------------
    def test_insufficient_history_returns_base(self) -> None:
        assert suggest_interval_hours([], BASE) == BASE
        assert suggest_interval_hours(None, BASE) == BASE  # type: ignore[arg-type]
        assert suggest_interval_hours(["fp"], BASE) == BASE

    def test_two_periods_low_volatility_still_base(self) -> None:
        # 仅 2 期且未变(vol=0.0):低波动退避要求 >= 3 期,不满足 -> base
        assert suggest_interval_hours(["a", "a"], BASE) == BASE

    # -- 高波动:加密到 base // 2 ------------------------------------------
    def test_high_volatility_halves(self) -> None:
        assert suggest_interval_hours(["a", "b"], BASE) == BASE // 2

    def test_high_volatility_boundary_inclusive(self) -> None:
        # vol 恰为 0.5 也算高波动
        assert suggest_interval_hours(["a", "a", "b"], BASE) == BASE // 2

    def test_high_volatility_floor_clamped_to_six(self) -> None:
        # base=4 -> 4//2=2,钳到下限 6;base=10 -> 5,钳到 6
        assert suggest_interval_hours(["a", "b"], 4) == MIN_INTERVAL_H == 6
        assert suggest_interval_hours(["a", "b"], 10) == 6
        assert suggest_interval_hours(["a", "b"], 12) == 6  # 12//2=6 恰达下限

    # -- 低波动:退避到 base * 2 -------------------------------------------
    def test_low_volatility_doubles(self) -> None:
        assert suggest_interval_hours(["a", "a", "a", "a"], 100) == 200

    def test_low_volatility_boundary_inclusive(self) -> None:
        # 11 期恰 1 变:vol = 1/10 = 0.1,含边界 -> 退避
        assert suggest_interval_hours(["a"] * 10 + ["b"], 100) == 200

    def test_low_volatility_ceiling_clamped_to_720(self) -> None:
        assert suggest_interval_hours(["a", "a", "a"], 500) == MAX_INTERVAL_H == 720
        assert suggest_interval_hours(["a", "a", "a"], 480) == 720

    # -- 中间档:维持 base --------------------------------------------------
    def test_middle_volatility_keeps_base(self) -> None:
        # vol = 1/4 = 0.25,介于 (0.1, 0.5) 开区间
        assert suggest_interval_hours(["a", "a", "a", "b", "b"], BASE) == BASE

    def test_result_type_is_int(self) -> None:
        for history in ([], ["a", "b"], ["a", "a", "a"]):
            assert isinstance(suggest_interval_hours(history, 72), int)

    # -- 参数校验 -----------------------------------------------------------
    def test_non_positive_base_raises_chinese_value_error(self) -> None:
        for bad in (0, -1, -100):
            with pytest.raises(ValueError, match="必须为正"):
                suggest_interval_hours(["a", "b"], bad)


# ---------------------------------------------------------------------------
# next_run:下次运行时刻
# ---------------------------------------------------------------------------
class TestNextRun:
    def test_empty_schedule_equals_now_plus_interval(self) -> None:
        now = datetime(2026, 10, 1, 8, 0, 0)
        assert next_run([], 12, now=now) == datetime(2026, 10, 1, 20, 0, 0)

    def test_empty_schedule_defaults_to_wallclock_now(self) -> None:
        t0 = datetime.now()
        got = next_run([], 6)
        t1 = datetime.now()
        assert got.tzinfo is None
        assert t0 + timedelta(hours=6) <= got <= t1 + timedelta(hours=6)

    def test_multiple_times_take_maximum(self) -> None:
        d1 = datetime(2026, 9, 1, 0, 0)
        d2 = datetime(2026, 9, 5, 12, 30)
        d3 = datetime(2026, 9, 10, 6, 0)
        # 乱序给入,取最近的 d3 起算
        assert next_run([d1, d3, d2], 25) == datetime(2026, 9, 11, 7, 0)

    def test_interval_crosses_day_boundary(self) -> None:
        d = datetime(2026, 10, 1, 23, 0)
        assert next_run([d], 6) == datetime(2026, 10, 2, 5, 0)

    def test_none_schedule_and_none_entries_fall_back_to_now(self) -> None:
        now = datetime(2026, 10, 1, 0, 0)
        assert next_run(None, 3, now=now) == datetime(2026, 10, 1, 3, 0)
        assert next_run([None, None], 3, now=now) == datetime(2026, 10, 1, 3, 0)  # type: ignore[list-item]

    def test_aware_schedule_input_tolerated_and_naive_output(self) -> None:
        aware = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        naive_max = datetime(2026, 1, 2, 0, 0)
        got = next_run([aware, naive_max], 24)  # 混用不抛 TypeError
        assert got == datetime(2026, 1, 3, 0, 0)
        assert got.tzinfo is None

    def test_aware_only_schedule_converted_to_local_naive(self) -> None:
        aware = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        now = datetime(2025, 12, 1, 0, 0)
        got = next_run([aware], 24, now=now)
        expected = aware.astimezone().replace(tzinfo=None) + timedelta(hours=24)
        assert got == expected
        assert got.tzinfo is None

    def test_aware_now_param_converted_to_local_naive(self) -> None:
        aware_now = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
        got = next_run([], 6, now=aware_now)
        expected = aware_now.astimezone().replace(tzinfo=None) + timedelta(hours=6)
        assert got == expected
        assert got.tzinfo is None


# ---------------------------------------------------------------------------
# plan_row:调度行组装
# ---------------------------------------------------------------------------
class TestPlanRow:
    def test_row_structure_and_field_types(self) -> None:
        last = datetime(2026, 10, 1, 0, 0)
        row = plan_row("https://example.com/", ["a", "b", "c"], BASE, last_run=last)
        assert set(row) == {"url", "volatility", "interval_h", "next_run"}
        assert row["url"] == "https://example.com/"
        assert isinstance(row["volatility"], float)
        assert isinstance(row["interval_h"], int)
        assert isinstance(row["next_run"], str)

    def test_next_run_is_iso_of_last_run_plus_interval(self) -> None:
        last = datetime(2026, 10, 1, 8, 0)
        row = plan_row("https://example.com/", ["a", "b", "c"], 24, last_run=last)
        assert row["interval_h"] == 12  # 高波动:24//2
        assert datetime.fromisoformat(row["next_run"]) == datetime(
            2026, 10, 1, 20, 0
        )

    def test_no_last_run_gives_none_next_run(self) -> None:
        row = plan_row("https://example.com/", ["a", "b"], BASE)
        assert row["next_run"] is None
        assert row["interval_h"] == BASE // 2  # 其余字段照常计算

    def test_no_history_conservative_row(self) -> None:
        row = plan_row("https://example.com/", [], BASE)
        assert row["volatility"] == 0.0
        assert row["interval_h"] == BASE
        assert row["next_run"] is None

    def test_tiers_flow_into_row(self) -> None:
        last = datetime(2026, 1, 1, 0, 0)
        high = plan_row("u", ["a", "b"], BASE, last_run=last)
        low = plan_row("u", ["a", "a", "a"], BASE, last_run=last)
        mid = plan_row("u", ["a", "a", "a", "b", "b"], BASE, last_run=last)
        assert high["interval_h"] == 36
        assert low["interval_h"] == 144
        assert mid["interval_h"] == 72

    def test_invalid_base_propagates_value_error(self) -> None:
        with pytest.raises(ValueError, match="必须为正"):
            plan_row("https://example.com/", ["a", "b"], 0)


# ---------------------------------------------------------------------------
# 确定性:同输入同输出
# ---------------------------------------------------------------------------
class TestDeterminism:
    def test_volatility_stable(self) -> None:
        history = ["a", "b", "b", "c", "c", "c"]
        assert volatility(history) == volatility(history)

    def test_suggest_interval_stable(self) -> None:
        for history in ([], ["a"], ["a", "b"], ["a", "a", "a"], ["a", "a", "b"]):
            first = suggest_interval_hours(history, BASE)
            for _ in range(3):
                assert suggest_interval_hours(history, BASE) == first

    def test_next_run_stable_with_injected_now(self) -> None:
        schedule = [datetime(2026, 1, 1, 0, 0), datetime(2026, 2, 1, 12, 0)]
        now = datetime(2026, 3, 1, 0, 0)
        assert next_run(schedule, 48, now=now) == next_run(schedule, 48, now=now)

    def test_plan_row_stable(self) -> None:
        kwargs = {
            "url": "https://example.com/",
            "history": ["a", "b", "c"],
            "base_h": BASE,
            "last_run": datetime(2026, 10, 1, 0, 0),
        }
        assert plan_row(**kwargs) == plan_row(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# V5 升级:volatility 单遍实现的等价性锁定 + adaptive.suggest 遥测计数
# ---------------------------------------------------------------------------
def test_v5_volatility_single_pass_large_history_exact() -> None:
    """单遍实现对长序列的精确值锁定(5000 期:全变 / 全不变 / 末端一变)。"""
    n = 5000
    assert volatility(["a", "b"] * (n // 2)) == 1.0
    assert volatility(["a"] * n) == 0.0
    assert volatility(["a"] * (n - 1) + ["b"]) == pytest.approx(1 / (n - 1))
    # None 规范化在单遍路径中同样生效
    assert volatility([None] * 3 + ["x"]) == pytest.approx(1 / 3)  # type: ignore[list-item]


def test_v5_volatility_docstring_examples_hold() -> None:
    """模块 docstring 中的用法示例数值正确(示例即契约)。"""
    assert volatility(["fp1", "fp1", "fp2"]) == 0.5
    assert suggest_interval_hours(["fp1", "fp2"], 72) == 36


def test_v5_suggest_interval_telemetry_counter() -> None:
    """每次成功建议累加 adaptive.suggest;校验失败与 volatility 不计数。"""
    telemetry.reset()
    suggest_interval_hours(["a", "b"], BASE)   # 高波动档
    suggest_interval_hours(["a", "a", "a"], BASE)  # 低波动档
    suggest_interval_hours([], BASE)           # 历史不足档
    volatility(["a", "b"])                     # 不经过建议入口,不计数
    with pytest.raises(ValueError):
        suggest_interval_hours(["a", "b"], 0)  # 校验失败:未产生建议,不计数
    assert telemetry.snapshot()["counters"]["adaptive.suggest"] == 3
