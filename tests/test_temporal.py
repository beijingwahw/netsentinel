"""netsentinel.intel.temporal 时序内核测试(Kleinberg 爆发检测 + TTL 建议)。

纯离线、固定种子、确定性断言(pytest.approx 处理浮点)。覆盖:

- Kleinberg 经典性质:均匀流零 burst;密集段产生恰覆盖该段的 burst
  (level ≥ 1,weight > 0);多层数值更高层级;孤立短间隔被 γ 抑制;
- 对数域数值稳定:尺度不变(全体时间戳同乘常数,level/weight 不变)、
  大时间跨度(1e310 小时)不溢出、大流(数千事件)成本有限;
- 确定性:同输入两次调用输出与操作计数逐项相等;
- burst_factor:空列表 / 区间内按权重饱和单调 / 区间外半衰期衰减 /
  钳制 [0, 1];
- suggest_ttl:factor 单调不增、上下限钳制、factor 越界钳制、
  与 ops.adaptive 的 [6, 720] 同口径、非法参数中文 ValueError;
- 操作计数不变量:dp_cells == gaps×levels、dp_trans_evals ==
  (gaps−1)×levels²(对事件数线性)、kernel_selfcheck 自检通过;
- kleinberg_bursts_nested(A241 层级嵌套输出):与扁平输出共用状态路径、
  两层数据的树形结构与嵌套性质、单层退化与扁平输出逐项相等、固定
  种子多簇流的父子区间嵌套 / level 逐级 +1 / 扁平 level == 子树最大、
  stats 与扁平口径一致仅多 nodes、非法参数中文 ValueError;
- calibrate_params(A241 参数标定工具):三站点无 burst 场景的命中 /
  浪费手算对照、burst 场景的网格分化与帕累托支配(暴力复核)、
  shrink=0.8 行与 suggest_ttl 链逐位一致、无真值流的诚实降级、
  确定性与输入不被改动、非法参数中文 ValueError。
"""
from __future__ import annotations

import math
import random
from itertools import pairwise
from typing import Any

import pytest

from netsentinel.intel import temporal
from netsentinel.intel.temporal import (
    CALIB_DEFAULT_HALFLIVES_H,
    CALIB_DEFAULT_SHRINKS,
    MAX_TTL_H,
    MIN_TTL_H,
    burst_factor,
    calibrate_params,
    kernel_selfcheck,
    kleinberg_bursts,
    kleinberg_bursts_nested,
    suggest_ttl,
)
from netsentinel.ops import adaptive

# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def _uniform(start: float, step: float, count: int) -> list[float]:
    """等间隔事件流:start 起 count 个、每 step 小时一个。"""
    return [start + step * i for i in range(count)]


def _assert_chinese_value_error(excinfo: pytest.ExceptionInfo[Any]) -> None:
    """断言异常消息含中文字符(项目惯例:面向运维的中文报错)。"""
    assert any("\u4e00" <= ch <= "\u9fff" for ch in str(excinfo.value))


# ---------------------------------------------------------------------------
# Kleinberg 经典性质
# ---------------------------------------------------------------------------


def test_uniform_stream_yields_no_bursts() -> None:
    """均匀间隔 → 无 burst:升档每间隔净亏 ln s − λ0Δ(s−1) < 0。"""
    assert kleinberg_bursts(_uniform(0.0, 1.0, 720)) == []
    assert kleinberg_bursts(_uniform(0.0, 24.0, 200)) == []


def test_dense_cluster_single_burst_covering_cluster() -> None:
    """稀疏背景 + 密集段 → 恰一个 burst,区间覆盖密集段且远窄于全窗。"""
    background = _uniform(0.0, 24.0, 10)  # 0..216h,每 24h 一事件
    cluster = [300.0 + 0.1 * i for i in range(30)]  # 30 事件挤进 [300, 302.9]
    bursts = kleinberg_bursts(background + cluster)
    assert len(bursts) == 1
    only = bursts[0]
    assert only["start"] == pytest.approx(300.0)
    assert only["end"] == pytest.approx(302.9)
    assert only["level"] >= 1  # 经典两状态:level 恰为 1
    assert only["level"] == 1
    assert only["weight"] > 10.0  # 29 个间隔 × ≈0.68 自然对数单位
    assert math.isfinite(only["weight"])
    assert only["end"] - only["start"] < 30.0  # 定位到密集段,不是全窗


def test_multilevel_automaton_detects_higher_level() -> None:
    """可扩展 k 层:levels=3 时超密簇的最优路径升到第 2 层。"""
    background = _uniform(0.0, 1.0, 720)
    cluster = [360.0 + 0.5 * i / 47 for i in range(48)]  # 48 事件挤进 0.5h
    bursts = kleinberg_bursts(background + cluster, levels=3)
    assert len(bursts) == 1
    assert bursts[0]["level"] == 2  # 密度对比足以支撑两层爬升
    assert bursts[0]["weight"] > 0.0
    assert bursts[0]["start"] <= 360.5 <= bursts[0]["end"]


def test_isolated_dense_gap_suppressed_by_gamma() -> None:
    """γ 调节作用:孤立短间隔的节省(≈0.68)摊不平大 γ 的爬升代价。"""
    events = [0.0, 100.0, 100.5, 200.0]  # 唯一短间隔 0.5h
    assert kleinberg_bursts(events, gamma=2.0) == []  # 爬升 2·ln2 ≈ 1.39 > 0.68
    relaxed = kleinberg_bursts(events, gamma=0.1)  # 爬升 0.069 < 0.68 → 放行
    assert len(relaxed) == 1
    assert relaxed[0]["start"] == pytest.approx(100.0)
    assert relaxed[0]["end"] == pytest.approx(100.5)


def test_trivial_inputs_return_empty() -> None:
    """空 / 单事件 / 全同刻事件 → 无间隔可言,返回空列表。"""
    assert kleinberg_bursts([]) == []
    assert kleinberg_bursts([42.0]) == []
    assert kleinberg_bursts([7.0, 7.0, 7.0]) == []


def test_duplicate_timestamps_deduped() -> None:
    """重复时间戳合并(零间隔会让 ln Δ 发散),操作计数按去重后口径。"""
    stats: dict[str, int | float] = {}
    assert kleinberg_bursts([5.0, 5.0, 6.0, 7.0], stats=stats) == []
    assert stats["events_in"] == 4
    assert stats["events_used"] == 3
    assert stats["gaps"] == 2


def test_unsorted_input_equals_sorted() -> None:
    """输入乱序不影响结果:内部规范化为去重升序(固定种子打乱)。"""
    events = _uniform(0.0, 3.0, 50) + [100.0 + 0.2 * i for i in range(25)]
    shuffled = list(events)
    random.Random(20261003).shuffle(shuffled)
    assert kleinberg_bursts(shuffled) == kleinberg_bursts(events)


def test_burst_record_structure() -> None:
    """输出结构:恰含 start/end/level/weight 四键,end > start,按 start 升序。"""
    events = _uniform(0.0, 12.0, 30) + [500.0 + 0.3 * i for i in range(20)]
    bursts = kleinberg_bursts(events, levels=3)
    assert bursts, "密集段必须至少产生一个 burst"
    for b in bursts:
        assert set(b) == {"start", "end", "level", "weight"}
        assert isinstance(b["start"], float) and isinstance(b["end"], float)
        assert isinstance(b["level"], int) and isinstance(b["weight"], float)
        assert b["end"] > b["start"]
        assert b["level"] >= 1
        assert b["weight"] > 0.0
    starts = [b["start"] for b in bursts]
    assert starts == sorted(starts)


# ---------------------------------------------------------------------------
# 对数域数值稳定
# ---------------------------------------------------------------------------


def test_scale_invariance() -> None:
    """尺度不变:全体时间戳同乘 1e6,区间随之缩放、level/weight 不变。"""
    small = _uniform(0.0, 24.0, 10) + [300.0 + 0.1 * i for i in range(30)]
    big = [t * 1e6 for t in small]
    bursts_s = kleinberg_bursts(small)
    bursts_b = kleinberg_bursts(big)
    assert len(bursts_s) == len(bursts_b) == 1
    assert bursts_b[0]["start"] == pytest.approx(bursts_s[0]["start"] * 1e6, rel=1e-9)
    assert bursts_b[0]["end"] == pytest.approx(bursts_s[0]["end"] * 1e6, rel=1e-9)
    assert bursts_b[0]["level"] == bursts_s[0]["level"]
    assert bursts_b[0]["weight"] == pytest.approx(bursts_s[0]["weight"], rel=1e-9)


def test_huge_span_does_not_overflow() -> None:
    """大时间跨度(1e308 小时,贴近 float 上限):n/T 已是次正规量级,
    先除后取对数必丢精度,对数域组装仍稳定。"""
    events = _uniform(0.0, 1.0, 20) + [1e308]  # 起始 1h 密集 + 巨大空窗
    stats: dict[str, int | float] = {}
    bursts = kleinberg_bursts(events, stats=stats)
    assert len(bursts) == 1
    assert bursts[0]["start"] == pytest.approx(0.0)
    assert bursts[0]["end"] == pytest.approx(19.0)
    assert math.isfinite(bursts[0]["weight"])
    assert math.isfinite(float(stats["cost_base"]))
    assert math.isfinite(float(stats["cost_opt"]))


def test_large_stream_costs_finite() -> None:
    """数千事件大流:成本口径全程有限(lgamma 路径;float(n!) 会溢出)。"""
    stats: dict[str, int | float] = {}
    assert kleinberg_bursts(_uniform(0.0, 1.0, 5000), stats=stats) == []
    assert stats["events_used"] == 5000
    assert math.isfinite(float(stats["cost_base"]))
    assert math.isfinite(float(stats["cost_opt"]))
    # lgamma(n+1) 远超 float 指数域仍有限——这是对数域防溢出的直接证据
    assert math.lgamma(5001.0) > 709.0 * 3  # ≈ 37563,naive factorial 必溢出


# ---------------------------------------------------------------------------
# 确定性
# ---------------------------------------------------------------------------


def test_deterministic_repeat_calls() -> None:
    """同输入两次调用:burst 列表与操作计数逐项相等(纯函数)。"""
    events = _uniform(0.0, 6.0, 80) + [400.0 + 0.25 * i for i in range(40)]
    stats_a: dict[str, int | float] = {}
    stats_b: dict[str, int | float] = {}
    bursts_a = kleinberg_bursts(events, stats=stats_a)
    bursts_b = kleinberg_bursts(events, stats=stats_b)
    assert bursts_a == bursts_b
    assert stats_a == stats_b


# ---------------------------------------------------------------------------
# burst_factor
# ---------------------------------------------------------------------------


def test_burst_factor_empty_bursts_is_zero() -> None:
    """空输入(None / 空列表)→ 强度 0。"""
    assert burst_factor(None, 100.0) == 0.0
    assert burst_factor([], 100.0) == 0.0


def test_burst_factor_inside_burst_monotone_in_weight() -> None:
    """区间内:强度 = w/(w+1),对权重严格递增且天然落在 (0, 1)。"""
    values = []
    for w in (0.5, 2.0, 9.0):
        f = burst_factor([{"start": 10.0, "end": 20.0, "level": 1, "weight": w}], 15.0)
        assert 0.0 < f < 1.0
        values.append(f)
    assert values == sorted(values)
    assert len(set(values)) == 3  # 严格递增


def test_burst_factor_boundaries_inside() -> None:
    """区间边界:start 与 end 时刻均按"区间内"取满强度。"""
    b = [{"start": 10.0, "end": 20.0, "level": 1, "weight": 3.0}]
    assert burst_factor(b, 10.0) == pytest.approx(0.75)
    assert burst_factor(b, 20.0) == pytest.approx(0.75)
    assert burst_factor(b, 9.99) == 0.0  # 尚未开始


def test_burst_factor_decays_with_halflife_after_end() -> None:
    """区间外:默认半衰期 24h,每过一个半衰期强度减半,久远归零。"""
    b = [{"start": 10.0, "end": 20.0, "level": 1, "weight": 3.0}]
    full = 0.75
    assert burst_factor(b, 20.0 + 24.0) == pytest.approx(full * 0.5)
    assert burst_factor(b, 20.0 + 48.0) == pytest.approx(full * 0.25)
    assert burst_factor(b, 20.0 + 24.0 * 60) < 1e-9  # 60 个半衰期后实际归零
    # 半衰期可调:12h 时 24h 后衰减到 1/4
    assert burst_factor(b, 44.0, decay_halflife_h=12.0) == pytest.approx(full * 0.25)


def test_burst_factor_inf_weight_saturates_to_one() -> None:
    """权重 inf:特判饱和到 1(inf/(inf+1) 是 nan,不得外泄)。"""
    b = [{"start": 10.0, "end": 20.0, "level": 1, "weight": math.inf}]
    assert burst_factor(b, 15.0) == 1.0
    huge = [{"start": 10.0, "end": 20.0, "level": 1, "weight": 1e300}]
    assert burst_factor(huge, 15.0) == 1.0


def test_burst_factor_multiple_bursts_takes_max() -> None:
    """多 burst 并存:取贡献最大者;结束后只看衰减残量。"""
    bursts = [
        {"start": 10.0, "end": 20.0, "level": 1, "weight": 1.0},  # 强度 0.5
        {"start": 50.0, "end": 60.0, "level": 1, "weight": 9.0},  # 强度 0.9
    ]
    assert burst_factor(bursts, 15.0) == pytest.approx(0.5)
    assert burst_factor(bursts, 55.0) == pytest.approx(0.9)
    # 两个都结束后取衰减后较大者:近末的强 burst(0.9·2^-1)压过远末的弱 burst
    assert burst_factor(bursts, 84.0) == pytest.approx(0.45, rel=1e-6)


def test_burst_factor_always_in_unit_range() -> None:
    """任何输入组合结果都钳制在 [0, 1]。"""
    b = [{"start": 0.0, "end": 1000.0, "level": 2, "weight": 1e18}]
    assert 0.0 <= burst_factor(b, 500.0) <= 1.0
    assert burst_factor([{"start": 0.0, "end": 1.0, "level": 1, "weight": -5.0}], 0.5) == 0.0


# ---------------------------------------------------------------------------
# suggest_ttl
# ---------------------------------------------------------------------------


def test_suggest_ttl_zero_factor_keeps_base() -> None:
    """稳态(factor=0)→ 建议 TTL 恰为基准,与既有默认行为一致。"""
    assert suggest_ttl(72, 0.0) == pytest.approx(72.0)
    assert suggest_ttl(72, 0) == pytest.approx(72.0)


def test_suggest_ttl_full_factor_shrinks_to_fifth() -> None:
    """最强爆发(factor=1)→ 收缩到基准的 1/5(base·(1−0.8))。"""
    assert suggest_ttl(72, 1.0) == pytest.approx(14.4)
    assert suggest_ttl(100, 1.0) == pytest.approx(20.0)


def test_suggest_ttl_monotone_non_increasing_in_factor() -> None:
    """单调性:factor 每增不减 TTL(含钳制区)。"""
    ttls = [suggest_ttl(300.0, f / 20.0) for f in range(21)]
    assert all(a >= b for a, b in pairwise(ttls))
    # 落在钳制区内的基准同样单调(300·0.2=60 > min=6,全程不触界)
    ttls_mid = [suggest_ttl(100.0, f / 20.0) for f in range(21)]
    assert all(a >= b for a, b in pairwise(ttls_mid))
    assert ttls_mid[0] > ttls_mid[-1]  # 且确实收缩了


def test_suggest_ttl_clamped_to_bounds() -> None:
    """钳制边界:超上限压回 720,低于下限抬到 6,中间原样。"""
    assert suggest_ttl(5000.0, 1.0) == 720.0  # 1000 → 压回 720
    assert suggest_ttl(5.0, 0.0) == 6.0  # 5 → 抬到 6
    assert suggest_ttl(1000.0, 0.5) == pytest.approx(600.0)  # 中间不触界
    assert suggest_ttl(6.0, 1.0) == pytest.approx(6.0)  # 6·0.2=1.2 → 抬回 6
    assert suggest_ttl(720.0, 0.0) == 720.0  # 基准恰为上限


def test_suggest_ttl_factor_out_of_range_clamped() -> None:
    """factor 越界(负 / 超 1)防御性钳制到 [0, 1]。"""
    assert suggest_ttl(72, 7.5) == pytest.approx(suggest_ttl(72, 1.0))
    assert suggest_ttl(72, -3.0) == pytest.approx(suggest_ttl(72, 0.0))


def test_suggest_ttl_returns_float() -> None:
    """返回类型恒为 float(即便入参是 int)。"""
    assert isinstance(suggest_ttl(72, 0), float)
    assert isinstance(suggest_ttl(72, 1), float)


def test_suggest_ttl_bounds_match_adaptive_contract() -> None:
    """默认上下限与 ops.adaptive 的 [6, 720] 硬界同口径(同一频控观)。"""
    assert MIN_TTL_H == adaptive.MIN_INTERVAL_H == 6
    assert MAX_TTL_H == adaptive.MAX_INTERVAL_H == 720
    assert temporal.TTL_SHRINK == 0.8


def test_suggest_ttl_invalid_args_raise_chinese() -> None:
    """非法参数:base ≤ 0、min_h ≤ 0、min > max 均抛中文 ValueError。"""
    with pytest.raises(ValueError) as ei:
        suggest_ttl(0, 0.5)
    _assert_chinese_value_error(ei)
    with pytest.raises(ValueError):
        suggest_ttl(-72, 0.5)
    with pytest.raises(ValueError):
        suggest_ttl(72, 0.5, min_h=0, max_h=10)
    with pytest.raises(ValueError):
        suggest_ttl(72, 0.5, min_h=100, max_h=50)
    with pytest.raises(ValueError):
        suggest_ttl(72, 0.5, min_h=-6, max_h=720)


# ---------------------------------------------------------------------------
# burst_factor 参数校验
# ---------------------------------------------------------------------------


def test_burst_factor_invalid_args_raise_chinese() -> None:
    """非法参数:非正半衰期、非有限 now 抛中文 ValueError。"""
    with pytest.raises(ValueError) as ei:
        burst_factor([], 100.0, decay_halflife_h=0.0)
    _assert_chinese_value_error(ei)
    with pytest.raises(ValueError):
        burst_factor([], 100.0, decay_halflife_h=-24.0)
    with pytest.raises(ValueError):
        burst_factor([], math.inf)
    with pytest.raises(ValueError):
        burst_factor([], math.nan)


# ---------------------------------------------------------------------------
# kleinberg_bursts 参数校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"s": 1},          # s=1:所有层速率相同,模型退化
        {"s": 0.5},        # s<1:速率递减,语义颠倒
        {"gamma": 0},      # 爬升零代价:任何微小起伏都成 burst
        {"gamma": -1},     # 负惩罚无意义
        {"levels": 1},     # 只有基态:无爆发可言
        {"levels": 2.5},   # 层数须为整数
    ],
)
def test_kleinberg_invalid_params_raise_chinese(kwargs: dict[str, Any]) -> None:
    """模型参数越界 → 中文 ValueError(防误配)。"""
    with pytest.raises(ValueError) as ei:
        kleinberg_bursts([1.0, 2.0, 3.0], **kwargs)
    _assert_chinese_value_error(ei)


@pytest.mark.parametrize("bad", [math.inf, -math.inf, math.nan])
def test_kleinberg_nonfinite_events_raise(bad: float) -> None:
    """时间戳非有限 → 拒算(排序与对数都会失真)。"""
    with pytest.raises(ValueError):
        kleinberg_bursts([1.0, bad, 3.0])


def test_kleinberg_overflowing_level_combination_rejected() -> None:
    """ln n + (levels−1)·ln s 超出 float 指数域 → 直接拒算而非溢出 inf。"""
    with pytest.raises(ValueError) as ei:
        kleinberg_bursts([float(i) for i in range(10)], s=1e300, levels=3)
    _assert_chinese_value_error(ei)


# ---------------------------------------------------------------------------
# 操作计数不变量与离线自检
# ---------------------------------------------------------------------------


def test_operation_count_invariants() -> None:
    """核心不变量:dp_cells == gaps×levels、dp_trans == (gaps−1)×levels²。"""
    events = _uniform(0.0, 8.0, 60) + [600.0 + 0.2 * i for i in range(30)]
    for levels in (2, 3, 4):
        stats: dict[str, int | float] = {}
        kleinberg_bursts(events, levels=levels, stats=stats)
        gaps = stats["events_used"] - 1
        assert stats["gaps"] == gaps
        assert stats["levels"] == levels
        assert stats["dp_cells"] == gaps * levels
        assert stats["dp_trans_evals"] == (gaps - 1) * levels * levels
        assert stats["events_used"] <= stats["events_in"]


def test_operation_counts_scale_linearly_with_events() -> None:
    """事件数翻倍 DP 单元格数线性翻倍(远优于穷举 levels^gaps)。"""
    small: dict[str, int | float] = {}
    big: dict[str, int | float] = {}
    kleinberg_bursts(_uniform(0.0, 1.0, 500), stats=small)
    kleinberg_bursts(_uniform(0.0, 1.0, 1000), stats=big)
    ratio = big["dp_cells"] / small["dp_cells"]
    assert pytest.approx(ratio, rel=1e-3) == (999 / 499)  # 线性外推
    assert big["dp_cells"] < 2 ** int(big["gaps"])  # 穷举口径为天文数字


def test_kernel_selfcheck_passes_and_is_deterministic() -> None:
    """kernel_selfcheck:四键口径齐备、value == baseline、两次调用一致。"""
    first = kernel_selfcheck()
    second = kernel_selfcheck()
    assert first == second
    assert first["name"] == "temporal.kleinberg"
    assert first["value"] == first["baseline"]
    assert first["value"] > 0 and first["uniform_stream_cells"] > 0


# ---------------------------------------------------------------------------
# kleinberg_bursts_nested:层级嵌套输出(A241;与扁平输出共用同一条状态路径)
# ---------------------------------------------------------------------------


def test_nested_trivial_inputs_return_empty() -> None:
    """空 / 单事件 / 全同刻 / 均匀流 → 无间隔或无 burst,返回空森林。"""
    assert kleinberg_bursts_nested([]) == []
    assert kleinberg_bursts_nested([42.0]) == []
    assert kleinberg_bursts_nested([7.0, 7.0, 7.0]) == []
    assert kleinberg_bursts_nested(_uniform(0.0, 1.0, 720)) == []


def test_nested_single_level_degenerates_to_flat_output() -> None:
    """单层退化(levels=2):剥掉 children 键后与扁平输出**逐项相等**(含 weight 逐位)。"""
    events = [float(i) for i in range(0, 100, 10)] + [300 + 0.1 * i for i in range(30)]
    flat = kleinberg_bursts(events)
    tree = kleinberg_bursts_nested(events)
    assert flat, "密集段必须至少产生一个 burst"
    assert len(tree) == len(flat)
    for node in tree:
        assert node["children"] == []  # 两状态无嵌套层可言
    stripped = [{k: v for k, v in node.items() if k != "children"} for node in tree]
    assert stripped == flat  # 同公式同求和顺序 → 浮点逐位一致


def test_nested_two_level_tree_structure() -> None:
    """两层数据(levels=3 超密簇):顶层 level 1 节点内嵌 level 2 子节点。"""
    background = _uniform(0.0, 1.0, 720)
    cluster = [360.0 + 0.5 * i / 47 for i in range(48)]  # 48 事件挤进 0.5h
    events = sorted(background + cluster)
    flat = kleinberg_bursts(events, levels=3)
    tree = kleinberg_bursts_nested(events, levels=3)
    assert len(flat) == 1 and flat[0]["level"] == 2  # 扁平口径:最高层 2
    assert len(tree) == 1
    top = tree[0]
    assert set(top) == {"level", "start", "end", "weight", "children"}
    assert top["level"] == 1  # 树形口径:阈值层,顶层恒 1
    assert len(top["children"]) == 1
    child = top["children"][0]
    assert set(child) == {"level", "start", "end", "weight", "children"}
    assert child["level"] == 2 and child["children"] == []
    # 嵌套性质(Kleinberg 2002):子区间必然落在父区间内
    assert top["start"] <= child["start"] and child["end"] <= top["end"]
    assert child["end"] - child["start"] > 0.0
    # 顶层节点与扁平 burst 同界同权重(同一状态路径、同一公式)
    assert (top["start"], top["end"]) == (flat[0]["start"], flat[0]["end"])
    assert top["weight"] == flat[0]["weight"]
    assert top["weight"] > 0.0 and child["weight"] > 0.0  # 各层 weight ≥ γ·ln s > 0
    # 扁平 level(段内最高状态)== 以该节点为根的子树最大 level
    assert flat[0]["level"] == max(top["level"], child["level"]) == 2


def _clustered_stream(seed: int) -> list[float]:
    """确定性伪随机流:稀疏背景 + 三个密度不一的簇(供 levels=4 通用性质测试)。"""
    rng = random.Random(seed)
    events = [0.0]
    for _ in range(40):
        events.append(events[-1] + rng.uniform(6.0, 30.0))
    for _ in range(3):
        anchor = events[-1] + rng.uniform(20.0, 80.0)
        count = rng.randint(8, 40)
        density = rng.choice([0.05, 0.2, 1.0])
        for i in range(count):
            events.append(anchor + density * i * (1.0 + 0.01 * rng.random()))
    return events


def test_nested_forest_invariants_on_fixed_seed_streams() -> None:
    """固定种子多簇流(levels=4):树形嵌套性质 + 与扁平输出的一致性全量断言。"""
    for seed in range(12):
        events = _clustered_stream(seed)
        flat = kleinberg_bursts(events, levels=4)
        tree = kleinberg_bursts_nested(events, levels=4)
        assert len(tree) == len(flat), f"顶层区间数应与扁平输出一致(seed={seed})"

        def walk(node: dict, depth: int) -> int:
            """断言节点结构并返回子树最大 level。"""
            assert set(node) == {"level", "start", "end", "weight", "children"}
            assert node["level"] == depth + 1  # 顶层 1,子层逐级 +1
            assert node["weight"] > 0.0
            top = node["level"]
            for child in node["children"]:
                # 嵌套性质:level q 区间必然嵌套于 ≤ q−1 区间内(允许同界)
                assert child["start"] >= node["start"], f"子区间起点越出父区间(seed={seed})"
                assert child["end"] <= node["end"], f"子区间终点越出父区间(seed={seed})"
                top = max(top, walk(child, depth + 1))
            return top

        for fb, node in zip(flat, tree):
            # 顶层节点与扁平 burst 同界同权重(逐位);扁平 level == 子树最大 level
            assert (node["start"], node["end"], node["weight"]) == (
                fb["start"],
                fb["end"],
                fb["weight"],
            ), f"顶层节点应与扁平 burst 同界同权重(seed={seed})"
            assert fb["level"] == walk(node, 0), f"扁平 level 应等于子树最大 level(seed={seed})"


def test_nested_stats_share_dp_counters_with_flat() -> None:
    """操作计数扩展:nested 与 flat 共用 DP,共享键取值全等,仅多 nodes 一键。"""
    events = _uniform(0.0, 1.0, 720) + [360.0 + 0.5 * i / 47 for i in range(48)]
    flat_stats: dict[str, int | float] = {}
    nested_stats: dict[str, int | float] = {}
    flat = kleinberg_bursts(events, levels=3, stats=flat_stats)
    tree = kleinberg_bursts_nested(events, levels=3, stats=nested_stats)
    assert set(nested_stats) - set(flat_stats) == {"nodes"}
    for key in flat_stats:
        assert nested_stats[key] == flat_stats[key], f"共享键 {key} 应取值一致"
    # 核心不变量对 nested 同样成立(同一条 DP)
    assert nested_stats["dp_cells"] == nested_stats["gaps"] * nested_stats["levels"]
    assert nested_stats["dp_trans_evals"] == (nested_stats["gaps"] - 1) * nested_stats["levels"] ** 2
    assert nested_stats["bursts"] == len(tree) == len(flat) == 1
    assert nested_stats["nodes"] == 2  # 顶层 1 + 嵌套 level 2 子节点 1
    # 单层退化为森林无嵌套:nodes == bursts
    single_stats: dict[str, int | float] = {}
    single = kleinberg_bursts_nested(
        [float(i) for i in range(0, 100, 10)] + [300 + 0.1 * i for i in range(30)],
        stats=single_stats,
    )
    assert single_stats["nodes"] == single_stats["bursts"] == len(single)


def test_nested_deterministic_repeat_calls() -> None:
    """同输入两次调用:树形输出与操作计数逐项相等(纯函数)。"""
    events = _uniform(0.0, 8.0, 60) + [600.0 + 0.2 * i for i in range(30)]
    stats_a: dict[str, int | float] = {}
    stats_b: dict[str, int | float] = {}
    tree_a = kleinberg_bursts_nested(events, levels=4, stats=stats_a)
    tree_b = kleinberg_bursts_nested(events, levels=4, stats=stats_b)
    assert tree_a == tree_b
    assert stats_a == stats_b


def test_nested_scale_invariance() -> None:
    """尺度不变(与扁平口径同纪律):全体时间戳同乘 1e6,区间缩放、level/weight 不变。"""
    small = sorted(_uniform(0.0, 1.0, 720) + [360.0 + 0.5 * i / 47 for i in range(48)])
    big = [t * 1e6 for t in small]
    tree_s = kleinberg_bursts_nested(small, levels=3)
    tree_b = kleinberg_bursts_nested(big, levels=3)

    def shape(nodes: list[dict]) -> tuple:
        return tuple(
            (n["level"], round(n["weight"], 6), len(n["children"]), shape(n["children"]))
            for n in nodes
        )

    assert len(tree_s) == len(tree_b) == 1
    assert tree_b[0]["start"] == pytest.approx(tree_s[0]["start"] * 1e6, rel=1e-9)
    assert tree_b[0]["end"] == pytest.approx(tree_s[0]["end"] * 1e6, rel=1e-9)
    assert shape(tree_b) == shape(tree_s)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"s": 1},
        {"s": 0.5},
        {"gamma": 0},
        {"gamma": -1},
        {"levels": 1},
        {"levels": 2.5},
    ],
)
def test_nested_invalid_params_raise_chinese(kwargs: dict[str, Any]) -> None:
    """模型参数越界 → 中文 ValueError(与扁平入口同校验、同消息)。"""
    with pytest.raises(ValueError) as ei:
        kleinberg_bursts_nested([1.0, 2.0, 3.0], **kwargs)
    _assert_chinese_value_error(ei)


@pytest.mark.parametrize("bad", [math.inf, -math.inf, math.nan])
def test_nested_nonfinite_events_raise(bad: float) -> None:
    """时间戳非有限 → 拒算(共用事件规范化)。"""
    with pytest.raises(ValueError):
        kleinberg_bursts_nested([1.0, bad, 3.0])


def test_nested_overflowing_level_combination_rejected() -> None:
    """ln n + (levels−1)·ln s 超 float 指数域 → 直接拒算(共用入口校验)。"""
    with pytest.raises(ValueError) as ei:
        kleinberg_bursts_nested([float(i) for i in range(10)], s=1e300, levels=3)
    _assert_chinese_value_error(ei)


def test_new_public_names_are_exported() -> None:
    """__all__ 收录新入口(旧名零改动、新增并存)。"""
    assert "kleinberg_bursts_nested" in temporal.__all__
    assert "calibrate_params" in temporal.__all__
    assert "CALIB_DEFAULT_HALFLIVES_H" in temporal.__all__
    assert "CALIB_DEFAULT_SHRINKS" in temporal.__all__
    assert CALIB_DEFAULT_HALFLIVES_H == (6.0, 12.0, 24.0, 48.0, 96.0)
    assert CALIB_DEFAULT_SHRINKS == (0.5, 0.8, 0.9)
    # 旧入口一个不少(零改动红线)
    for name in ("kleinberg_bursts", "burst_factor", "suggest_ttl", "kernel_selfcheck"):
        assert name in temporal.__all__


# ---------------------------------------------------------------------------
# calibrate_params:半衰期 × 收缩系数网格标定(A241;A207 交付后续)
# ---------------------------------------------------------------------------


def test_calibrate_hand_computed_hits_and_waste() -> None:
    """三站点无 burst 场景,命中 / 浪费逐项手算对照(无 burst → 网格各行同值)。

    站点 A:事件 [0,24,...,480](均匀,无 burst),真值 [250, 460],基准
    TTL=100h → 重扫恰在 100/200/300/400(500 > 480 停);真值 250 的窗口
    (0,250] 含 100、200 → 命中;真值 460 的窗口 (250,460] 含 300、400 → 命中。
    站点 B:事件 [0,50,...,200],真值 [100] → 重扫 [100,200];真值恰落在
    重扫时刻上(窗口 (0,100] 右闭含 100)→ 命中。
    站点 C:事件 [0,50,100],真值 [99] → 重扫 [100];窗口 (0,99] 不含 100
    → 未命中(TTL 到得比真值晚一步)。
    合计:真值 4、命中 3、重扫 7、浪费 4;全网格一致 → 15 行全在前沿。
    """
    site_a = [float(i) for i in range(0, 481, 24)]
    site_b = [0.0, 50.0, 100.0, 150.0, 200.0]
    site_c = [0.0, 50.0, 100.0]
    for events in (site_a, site_b, site_c):
        assert kleinberg_bursts(events) == []  # 均匀流无 burst,factor 恒 0
    report = calibrate_params([site_a, site_b, site_c], [[250.0, 460.0], [100.0], [99.0]], base_ttl_h=100.0)
    assert report["sites"] == report["sites_scored"] == 3
    assert report["sites_without_truth"] == 0
    assert report["truths"] == 4
    assert report["combos"] == 15
    assert len(report["grid"]) == 15
    for row in report["grid"]:
        assert set(row) == {"half_life_h", "shrink", "hits", "truths", "hit_rate", "rescans", "waste", "rescan_times"}
        assert row["hits"] == 3
        assert row["truths"] == 4
        assert row["hit_rate"] == pytest.approx(0.75)
        assert row["rescans"] == 7
        assert row["waste"] == 4
        assert row["rescan_times"] == [
            [100.0, 200.0, 300.0, 400.0],
            [100.0, 200.0],
            [100.0],
        ]
    # 全行并列(命中 / 浪费同值)→ 互不严格支配 → 帕累托前沿即全网格
    assert len(report["pareto"]) == 15
    assert "帕累托" in report["advice"]
    assert "无单一最优" in report["advice"]  # 并列时诚实呈现,不假装收敛


def _burst_calibration_scenario() -> tuple[list[float], list[list[float]]]:
    """burst 分化场景:360 个整点背景 + [380,381] 密集簇,真值 [405, 490]。

    第二个真值落在爆发衰减尾流内:半衰期 6h 时 400h 处强度已衰减到
    ~0.10,下一跳 ≈ 490.6h 迟于真值 → 未命中;半衰期 ≥ 12h 时下一跳
    落在 (405, 490] 内 → 命中。收缩越狠(0.9)重扫越密 → 浪费越多。
    """
    events = sorted(_uniform(0.0, 1.0, 360) + [380.0 + i / 29.0 for i in range(30)])
    return events, [[405.0, 490.0]]


def test_calibrate_grid_differentiates_and_pareto_dominance() -> None:
    """burst 场景:网格按参数分化;帕累托前沿与支配关系暴力复核。"""
    events, truths = _burst_calibration_scenario()
    bursts = kleinberg_bursts(events)
    assert len(bursts) == 1 and bursts[0]["start"] == pytest.approx(380.0)  # 场景自检
    report = calibrate_params([events], truths, base_ttl_h=100.0)

    by_combo = {(row["half_life_h"], row["shrink"]): row for row in report["grid"]}
    assert set(by_combo) == {(h, sh) for h in CALIB_DEFAULT_HALFLIVES_H for sh in CALIB_DEFAULT_SHRINKS}
    # 半衰期 6h:衰减过快,真值 490 前无重扫 → 只命中 405 一个
    for sh in CALIB_DEFAULT_SHRINKS:
        assert by_combo[(6.0, sh)]["hits"] == 1
        assert by_combo[(6.0, sh)]["waste"] == 3
    # 半衰期 12h:下一跳 ≈ 484 ≤ 490 → 双命中且浪费最少
    assert by_combo[(12.0, 0.5)]["hits"] == 2 and by_combo[(12.0, 0.5)]["waste"] == 3
    # 半衰期 96h + 高收缩:双命中但重扫更密 → 浪费 4,被 (12, 0.5) 支配
    assert by_combo[(96.0, 0.9)]["hits"] == 2 and by_combo[(96.0, 0.9)]["waste"] == 4
    assert by_combo[(96.0, 0.8)]["waste"] == 4

    # 暴力复核支配定义:a 命中 ≥ b 且浪费 ≤ b 且至少一项严格
    def dominates(a: dict, b: dict) -> bool:
        return (a["hits"] >= b["hits"] and a["waste"] <= b["waste"]) and (
            a["hits"] > b["hits"] or a["waste"] < b["waste"]
        )

    expected_front = {
        combo for combo, row in by_combo.items() if not any(dominates(o, row) for o in by_combo.values())
    }
    actual_front = {(row["half_life_h"], row["shrink"]) for row in report["pareto"]}
    assert actual_front == expected_front
    assert (6.0, 0.5) not in actual_front  # 命中少的组合不在前沿
    assert (96.0, 0.9) not in actual_front  # 同命中浪费多 → 被支配
    assert (24.0, 0.8) in actual_front  # 缺省组合恰在前沿上
    # 建议诚实呈现权衡(无单一最优),并给出缺省组合位置
    assert "帕累托" in report["advice"]
    assert "无单一最优" in report["advice"]
    assert "缺省组合" in report["advice"]


def test_calibrate_default_shrink_row_matches_suggest_ttl_chain() -> None:
    """shrink=0.8 的行:重扫时刻序列与 burst_factor + suggest_ttl 直算链逐位一致。"""
    events, truths = _burst_calibration_scenario()
    bursts = kleinberg_bursts(events)
    report = calibrate_params([events], truths, base_ttl_h=100.0)
    horizon = max(events[-1], truths[0][-1])  # t_hi = max(末事件, 末真值)
    start = min(events[0], truths[0][0])  # t_lo = min(首事件, 首真值)
    for hl in CALIB_DEFAULT_HALFLIVES_H:
        row = next(r for r in report["grid"] if r["shrink"] == 0.8 and r["half_life_h"] == hl)
        cur, expected = start, []
        while True:
            factor = burst_factor(bursts, cur, decay_halflife_h=hl)
            nxt = cur + suggest_ttl(100.0, factor)
            if nxt > horizon:
                break
            expected.append(nxt)
            cur = nxt
        assert row["rescan_times"][0] == expected, f"shrink=0.8 行应与 suggest_ttl 链逐位一致(hl={hl})"


def test_calibrate_no_truths_degrades_honestly() -> None:
    """无真值流:命中恒 0、只按浪费取舍;建议明说无法标定及时性(诚实降级)。"""
    events = _uniform(0.0, 24.0, 30)
    report = calibrate_params([events], [[]], base_ttl_h=100.0)
    assert report["truths"] == 0
    assert report["sites_without_truth"] == 1
    for row in report["grid"]:
        assert row["hits"] == 0
        assert row["hit_rate"] == 0.0
        assert row["waste"] == row["rescans"]  # 无命中 → 全部重扫都算浪费
    min_waste = min(row["waste"] for row in report["grid"])
    assert {row["waste"] for row in report["pareto"]} == {min_waste}
    assert "真值" in report["advice"]
    assert "诚实降级" in report["advice"]
    # 极端情形:连事件流都没有 → 同样诚实说明,而非抛错或假装给出最优
    empty = calibrate_params([], [])
    assert empty["sites"] == empty["sites_scored"] == 0
    assert empty["combos"] == 15
    assert "无法标定" in empty["advice"]


def test_calibrate_single_combo_grid_consistent_with_default() -> None:
    """单组合网格(half_lives=(24,), shrinks=(0.8,))与缺省网格中同行完全一致。"""
    events, truths = _burst_calibration_scenario()
    full = calibrate_params([events], truths, base_ttl_h=100.0)
    one = calibrate_params([events], truths, base_ttl_h=100.0, half_lives=(24.0,), shrinks=(0.8,))
    assert one["combos"] == 1 and len(one["grid"]) == 1
    row24 = next(r for r in full["grid"] if r["half_life_h"] == 24.0 and r["shrink"] == 0.8)
    assert one["grid"][0] == row24
    assert one["pareto"] == [dict(row24)]  # 单行无人支配 → 自成前沿


def test_calibrate_deterministic_and_inputs_untouched() -> None:
    """纯函数:两次调用输出逐项相等;入参列表不被原地改动(防御性拷贝)。"""
    events, truths = _burst_calibration_scenario()
    streams = [list(events)]
    outcomes = [list(truths[0])]
    first = calibrate_params(streams, outcomes, base_ttl_h=100.0)
    second = calibrate_params(streams, outcomes, base_ttl_h=100.0)
    assert first == second
    assert streams == [list(events)]
    assert outcomes == [list(truths[0])]
    # 帕累托行是网格行的值拷贝,改它不污染网格(出参互不别名)
    first["pareto"][0]["hits"] = -1
    assert all(row["hits"] >= 0 for row in first["grid"])


@pytest.mark.parametrize(
    ("streams", "outcomes", "kwargs"),
    [
        ([[1.0, 2.0]], [], {}),                                   # 流数与真值数不匹配
        ([[1.0, 2.0]], [[]], {"base_ttl_h": 0}),                  # 基准 TTL 非正
        ([[1.0, 2.0]], [[]], {"base_ttl_h": -72}),                # 基准 TTL 为负
        ([[1.0, 2.0]], [[]], {"min_h": 0}),                       # 下限非正
        ([[1.0, 2.0]], [[]], {"min_h": 100, "max_h": 50}),        # 下限高于上限
        ([[1.0, 2.0]], [[]], {"half_lives": ()}),                 # 半衰期网格为空
        ([[1.0, 2.0]], [[]], {"shrinks": ()}),                    # 收缩网格为空
        ([[1.0, 2.0]], [[]], {"half_lives": (0.0,)}),             # 半衰期非正
        ([[1.0, 2.0]], [[]], {"half_lives": (-6.0,)}),            # 半衰期为负
        ([[1.0, 2.0]], [[]], {"half_lives": (math.inf,)}),        # 半衰期非有限
        ([[1.0, 2.0]], [[]], {"shrinks": (0.0,)}),                # 收缩系数非正
        ([[1.0, 2.0]], [[]], {"shrinks": (1.0,)}),                # 收缩系数触上界(须落在开区间)
        ([[1.0, 2.0]], [[]], {"shrinks": (1.5,)}),                # 收缩系数越上界
        ([[1.0, 2.0]], [[]], {"shrinks": (math.nan,)}),           # 收缩系数非有限
        ([[1.0, 2.0]], [[math.inf]], {}),                         # 真值时点非有限
        ([[1.0, 2.0]], [[math.nan]], {}),                         # 真值时点 NaN
        ([[1.0, math.inf]], [[]], {}),                            # 事件非有限(检测器口径透传)
        ([[0.0, 1e9]], [[]], {}),                                 # 仿真窗口/下限超安全上限
    ],
)
def test_calibrate_invalid_args_raise_chinese(
    streams: list[list[float]],
    outcomes: list[list[float]],
    kwargs: dict[str, Any],
) -> None:
    """非法输入 → 中文 ValueError(长度不匹配 / 网格越界 / 非有限 / 窗口失控)。"""
    with pytest.raises(ValueError) as ei:
        calibrate_params(streams, outcomes, **kwargs)
    _assert_chinese_value_error(ei)
