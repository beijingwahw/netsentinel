"""A130 调度内核(netsentinel.ops.sched_kernel)单元测试。

全部离线:被测对象是纯函数(零 IO、零时钟),同输入同输出;
优先级用**手算对照**(因子逐项展开),轮选用确定性构造数据上的
**精确结果**断言(红线 31:禁依赖墙钟)。遥测计数遵循全仓惯例:
``telemetry.reset()`` 后取 ``snapshot()["counters"]`` 比对。

V10 升级覆盖(EDF+aging 防饿死 + 变成本预算背包):

- aging 第四因子:默认权重 0 **位级等价**(浮点精确 ==,非近似)、
  aging 单调性(更老站点不落后于新站点)、一周封顶、权重可配与脏值防御;
- 变成本背包:逐项成本 = pages × min_interval_s,价值密度贪心 +
  装不下跳过继续;**等成本退化一致性**(与测试内复刻的旧算法逐项相等)、
  预算硬上界不变量、零成本 / 无穷成本边界、脏 pages 防御;
- 操作计数不变量:变成本与自定义权重下仍每项恰一次 priority 调用。
"""
from __future__ import annotations

import math

import pytest

from netsentinel import telemetry
from netsentinel.ops import sched_kernel
from netsentinel.ops.sched_kernel import (
    AGING_WINDOW_H,
    STALENESS_WINDOW_H,
    W_AGING,
    W_RISK,
    W_STALENESS,
    W_VOLATILITY,
    kernel_selfcheck,
    priority,
    select_round,
)


# ---------------------------------------------------------------------------
# 构造助手:确定性造数,禁止随机
# ---------------------------------------------------------------------------
def item(
    url: str,
    vol: float = 0.0,
    risk: float = 0.0,
    stale_h: float = 0.0,
    age_h: float | None = None,
    pages: float | None = None,
) -> dict:
    """确定性候选项:基础四字段全显式;age_h / pages 传 None 即**不含该键**
    (专用于"缺字段"路径;V10 新字段默认缺席,与旧口径调用零差异)。"""
    it = {
        "url": url,
        "volatility": vol,
        "url_risk": risk,
        "staleness_h": stale_h,
    }
    if age_h is not None:
        it["age_h"] = age_h
    if pages is not None:
        it["pages"] = pages
    return it


def ten_items() -> list[dict]:
    """10 项确定性候选:优先级两两不同,降序恰为 e,i,d,h,c,g,b,f,a,e0。

    构造:volatility=i/10、url_risk=(9-i)/10、staleness_h=i*24(i=0..9),
    priority(i) = 0.045*i + 0.035*(9-i) + 0.2*min(i/7, 1) 关于 i 严格递增,
    故降序 = i=9,8,7,6,5,4,3,2,1,0(即 site9 … site0)。
    """
    return [
        item(
            f"https://site{i}.example",
            vol=i / 10,
            risk=(9 - i) / 10,
            stale_h=i * 24,
        )
        for i in range(10)
    ]


# ---------------------------------------------------------------------------
# 权重常量
# ---------------------------------------------------------------------------
class TestConstants:
    def test_weights_values_and_sum(self) -> None:
        """契约口径:0.45 / 0.35 / 0.20,且和为 1(满分可解析到 1.0)。"""
        assert W_VOLATILITY == 0.45
        assert W_RISK == 0.35
        assert W_STALENESS == 0.20
        assert W_VOLATILITY + W_RISK + W_STALENESS == pytest.approx(1.0)
        assert STALENESS_WINDOW_H == 168  # 一周

    def test_aging_constants_default_off(self) -> None:
        """V10 aging 因子:默认权重 0.0(完全关闭),归一化窗口同为一周。"""
        assert W_AGING == 0.0
        assert AGING_WINDOW_H == 168
        # 默认四权重和仍为 1(aging 关闭不改变归一化口径)
        assert W_VOLATILITY + W_RISK + W_STALENESS + W_AGING == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# priority:三因子手算对照
# ---------------------------------------------------------------------------
class TestPriority:
    def test_hand_computed_three_factors(self) -> None:
        """手算:0.45*0.8 + 0.35*0.5 + 0.20*(42/168) = 0.36+0.175+0.05 = 0.585。"""
        got = priority(item("u", vol=0.8, risk=0.5, stale_h=42))
        assert got == pytest.approx(0.585)

    def test_all_max_is_one(self) -> None:
        """三因子全满:0.45 + 0.35 + 0.20 = 1.0(168h 已是"完全陈旧")。"""
        assert priority(item("u", vol=1.0, risk=1.0, stale_h=168)) == pytest.approx(1.0)

    def test_all_zero_is_zero(self) -> None:
        assert priority(item("u", vol=0.0, risk=0.0, stale_h=0.0)) == 0.0

    def test_single_factor_volatility(self) -> None:
        """只有波动度:1.0 × 0.45(精确:浮点乘 1 不引入误差)。"""
        assert priority(item("u", vol=1.0)) == 0.45

    def test_single_factor_risk(self) -> None:
        assert priority(item("u", risk=1.0)) == 0.35

    def test_single_factor_staleness(self) -> None:
        """只有陈旧度:一周(168h)→ 恰 0.20;半周(84h)→ 0.10。"""
        assert priority(item("u", stale_h=STALENESS_WINDOW_H)) == 0.20
        assert priority(item("u", stale_h=84)) == pytest.approx(0.10)

    def test_staleness_capped_at_one_week(self) -> None:
        """超过 168h 封顶:两周年与一周年同分(线性外推会到 0.40,错误)。"""
        assert priority(item("u", stale_h=168)) == priority(item("u", stale_h=336))

    def test_missing_fields_default_zero(self) -> None:
        """缺字段按 0:空 dict / 只有 url / 缺任一因子都不抛错。"""
        assert priority({}) == 0.0
        assert priority({"url": "x"}) == 0.0
        assert priority({"volatility": 1.0}) == 0.45
        assert priority({"url_risk": 1.0}) == 0.35
        assert priority({"staleness_h": 168}) == 0.20

    def test_none_and_dirty_values_tolerated(self) -> None:
        """None / 非数脏值按 0,不抛 TypeError。"""
        assert priority({"volatility": None, "url_risk": None, "staleness_h": None}) == 0.0
        assert priority({"volatility": "high", "url_risk": [], "staleness_h": "1w"}) == 0.0

    def test_out_of_range_clamped(self) -> None:
        """越界钳制:volatility=2→1、risk=-0.5→0、负陈旧→0、NaN→0。"""
        assert priority(item("u", vol=2.0)) == 0.45
        assert priority(item("u", risk=-0.5)) == 0.0
        assert priority(item("u", stale_h=-72)) == 0.0
        assert priority({"volatility": math.nan}) == 0.0

    def test_url_not_part_of_scoring(self) -> None:
        """url 仅透传不参与打分:除 url 外全同的两项同分。"""
        assert priority(item("https://a.example", 0.5, 0.5, 24)) == priority(
            item("https://b.example", 0.5, 0.5, 24)
        )

    def test_range_property_grid(self) -> None:
        """确定性网格性质检验:任意合法组合恒在 [0, 1] 且与手算公式一致。"""
        for vol in (0.0, 0.1, 0.25, 0.5, 0.9, 1.0):
            for risk in (0.0, 0.2, 0.6, 1.0):
                for stale in (0, 12, 84, 168, 500):
                    got = priority(item("u", vol, risk, stale))
                    manual = (
                        W_VOLATILITY * vol
                        + W_RISK * risk
                        + W_STALENESS * min(stale / STALENESS_WINDOW_H, 1.0)
                    )
                    assert got == pytest.approx(manual)
                    assert 0.0 <= got <= 1.0


# ---------------------------------------------------------------------------
# V10 aging 因子:EDF+aging 防饿死(默认权重 0,位级等价)
# ---------------------------------------------------------------------------
class TestAgingPriority:
    def test_default_weights_age_bitwise_irrelevant(self) -> None:
        """默认权重 0:任取 age_h(含封顶前后 / 超窗 / 脏值),打分与
        **不含该键**的结果位级相等(浮点精确 ==,非 approx)。"""
        for age in (0, 1, 24, 84, 167, 168, 169, 336, 500, 9999, -72, math.inf):
            without = priority(item("u", 0.8, 0.5, 42))
            with_age = priority(item("u", 0.8, 0.5, 42, age_h=age))
            assert with_age == without, f"age_h={age!r} 改变了默认口径打分"
        assert priority({"age_h": 500}) == 0.0
        assert priority({"age_h": None}) == 0.0
        assert priority({"age_h": "1w"}) == 0.0  # 脏值按 0,不抛错

    def test_aging_manual_computation(self) -> None:
        """开启 aging:手算 = 三因子基线 + 0.10 × min(84/168, 1) = 0.635。"""
        base = priority(item("u", 0.8, 0.5, 42))
        got = priority(item("u", 0.8, 0.5, 42, age_h=84), weights={"aging": 0.10})
        assert base == pytest.approx(0.585)
        assert got == pytest.approx(base + 0.10 * 0.5)
        assert got == pytest.approx(0.635)

    def test_aging_monotone_nondecreasing(self) -> None:
        """防饿死单调性:同等条件下,更老的站点打分**不低于**更新的站点;
        封顶前严格递增,一周饱和后持平。"""
        weights = {"aging": 0.15}
        scores = [
            priority(item(f"u{age}", 0.3, 0.3, 24, age_h=age), weights=weights)
            for age in (0, 1, 24, 84, 167, 168, 169, 336, 10000)
        ]
        assert all(a <= b for a, b in zip(scores, scores[1:]))  # 单调不减
        assert scores[0] < scores[1] < scores[2] < scores[4]  # 封顶前严格增
        assert scores[5] == scores[6] == scores[8]  # 168h 起饱和持平

    def test_aging_saturation_exactly_one_week(self) -> None:
        """aging 一周封顶:168h 与任意超窗值同分,半窗恰为权重之半。"""
        weights = {"aging": 0.20}
        assert priority(item("u", age_h=168), weights=weights) == pytest.approx(0.20)
        assert priority(item("u", age_h=84), weights=weights) == pytest.approx(0.10)
        assert priority(item("u", age_h=10**6), weights=weights) == pytest.approx(0.20)

    def test_aging_prevents_starvation(self) -> None:
        """EDF+aging 场景:高波动新站 vs 低波动老站——默认口径老站被碾压,
        开启 aging 后饿满一周的老站反超(防饿死),且分数仍在 [0, 1]。"""
        new = item("new", vol=0.9, age_h=0)
        old = item("old", vol=0.1, age_h=500)
        assert priority(new) > priority(old)  # 旧口径:老站饿死
        weights = {"aging": 0.6}
        # 老:0.45*0.1 + 0.6*1 = 0.645 > 新:0.45*0.9 = 0.405
        assert priority(old, weights=weights) > priority(new, weights=weights)
        assert 0.0 <= priority(old, weights=weights) <= 1.0

    def test_weights_reconfigurable_and_clamped(self) -> None:
        """权重可配:逐键覆盖生效;权重和 > 1 由 [0, 1] 末端钳制兜底。"""
        only_risk = {"volatility": 0.0, "url_risk": 1.0}
        assert priority(item("u", vol=1.0, risk=1.0), weights=only_risk) == pytest.approx(1.0)
        assert priority(item("u", vol=1.0, risk=0.0), weights=only_risk) == 0.0
        over = {"aging": 1.0}  # 四权重和 2.0,全满时钳回 1.0
        assert priority(item("u", 1.0, 1.0, 168, age_h=168), weights=over) == 1.0

    def test_dirty_weights_tolerated(self) -> None:
        """脏权重防御:非数 / NaN / 未知键 / 非 mapping 一律不炸,
        有效的覆盖照常生效(与 _factor01 宁缺勿错一致)。"""
        assert priority(item("u", vol=1.0), weights={"volatility": "high"}) == 0.0
        assert priority(item("u", vol=1.0), weights={"volatility": math.nan}) == 0.0
        base = priority(item("u", 0.8, 0.5, 42))  # 0.585
        assert priority(item("u", 0.8, 0.5, 42), weights={"no_such_factor": 0.9}) == pytest.approx(base)
        assert priority(item("u", 0.8, 0.5, 42), weights=42) == pytest.approx(base)

    def test_negative_weight_clamped_to_zero(self) -> None:
        """负权重会破坏单调性(越老越靠后)→ 钳制为 0,绝不生效。"""
        assert priority(item("u", vol=1.0), weights={"volatility": -2.0}) == 0.0

    def test_pages_do_not_affect_priority(self) -> None:
        """pages 只作成本因子,不参与打分:带 pages 与不带同分。"""
        assert priority(item("u", 0.5, pages=9)) == priority(item("u", 0.5))
        assert priority({"pages": 100}) == 0.0


# ---------------------------------------------------------------------------
# select_round:预算选取
# ---------------------------------------------------------------------------
class TestSelectRound:
    def test_budget4_min1_picks_top4_in_priority_order(self) -> None:
        """红线 31 核心口径:10 项 / budget=4.0 / 间隔 1.0 → 恰选 4 项最高优先,
        且返回按 priority 降序(site9 > site8 > site7 > site6)。"""
        picked = select_round(ten_items(), 4.0)
        assert [p["url"] for p in picked] == [
            "https://site9.example",
            "https://site8.example",
            "https://site7.example",
            "https://site6.example",
        ]

    def test_budget_zero_and_negative_return_empty(self) -> None:
        items = ten_items()
        assert select_round(items, 0.0) == []
        assert select_round(items, -1.0) == []

    def test_budget_below_one_interval_return_empty(self) -> None:
        """预算不足一项(0.5 < 间隔 1.0)→ 空。"""
        assert select_round(ten_items(), 0.5) == []
        assert select_round(ten_items(), 0.999) == []

    def test_budget_fractional_floor(self) -> None:
        """非整预算向下取整:3.5 / 1.0 → 3 项(装不下第 4 项)。"""
        picked = select_round(ten_items(), 3.5)
        assert len(picked) == 3
        assert [p["url"] for p in picked] == [
            "https://site9.example",
            "https://site8.example",
            "https://site7.example",
        ]

    def test_custom_min_interval(self) -> None:
        """每项成本 = min_interval_s:6.0/2.0 → 3 项;6.0/1.5 → 4 项。"""
        assert len(select_round(ten_items(), 6.0, min_interval_s=2.0)) == 3
        assert len(select_round(ten_items(), 6.0, min_interval_s=1.5)) == 4

    def test_float_representation_defense(self) -> None:
        """二进制浮点防御:0.3/0.1 名义 3 项,朴素 floor(2.999...) 会错选 2。"""
        picked = select_round(ten_items(), 0.3, min_interval_s=0.1)
        assert len(picked) == 3

    def test_min_interval_nonpositive_raises(self) -> None:
        """间隔为 0 / 负属非法配置(零成本会装下一切)→ ValueError 中文消息。"""
        with pytest.raises(ValueError, match="礼貌间隔"):
            select_round(ten_items(), 10.0, min_interval_s=0.0)
        with pytest.raises(ValueError, match="必须为正数"):
            select_round(ten_items(), 10.0, min_interval_s=-1.0)

    def test_empty_and_none_items(self) -> None:
        assert select_round([], 4.0) == []
        assert select_round(None, 4.0) == []  # type: ignore[arg-type]

    def test_missing_fields_items_selectable(self) -> None:
        """字段缺失的项不炸:全按 0 分参与排序(仍可被预算选中)。"""
        items = [{"url": "plain"}, item("top", vol=1.0), {}]
        picked = select_round(items, 2.0)
        assert len(picked) == 2
        assert picked[0] is items[1]  # 唯一有分数的 "top" 排最前
        assert picked[1] in (items[0], items[2])  # 两个 0 分项其一

    def test_equal_priority_stable_original_order(self) -> None:
        """等优先级稳定性:同分项保持 items 原顺序(reverse 排序不翻动平局)。"""
        items = [item(f"u{i}") for i in range(6)]  # 全 0 分
        picked = select_round(items, 3.0)
        assert [p["url"] for p in picked] == ["u0", "u1", "u2"]

    def test_fewer_items_than_budget_selects_all(self) -> None:
        """候选少于预算容量 → 全选,且不越界造项。"""
        items = [item("low", vol=0.1), item("high", vol=0.9)]
        picked = select_round(items, 100.0)
        assert [p["url"] for p in picked] == ["high", "low"]

    def test_input_not_mutated(self) -> None:
        """纯函数:调用后输入列表顺序与各项内容原样(返回的是原对象引用)。"""
        items = ten_items()
        before = [dict(it) for it in items]
        order_before = [id(it) for it in items]
        picked = select_round(items, 4.0)
        assert [id(it) for it in items] == order_before  # 列表顺序未动
        assert [dict(it) for it in items] == before  # 各项内容未动
        assert all(any(p is orig for orig in items) for p in picked)  # 引用透传


# ---------------------------------------------------------------------------
# V10 select_round × weights:aging 开关在轮选里的表现
# ---------------------------------------------------------------------------
class TestSelectRoundWeights:
    def test_weights_thread_through_select_round(self) -> None:
        """select_round 透传 weights:同等因子下老站排前(aging 开);
        默认(aging 关)平局稳定保持原顺序——一个开关即得两代行为。"""
        items = [
            item("new", 0.5, 0.5, 24, age_h=2),
            item("old", 0.5, 0.5, 24, age_h=120),
        ]
        assert [p["url"] for p in select_round(items, 2.0)] == ["new", "old"]
        aged = select_round(items, 2.0, weights={"aging": 0.3})
        assert [p["url"] for p in aged] == ["old", "new"]  # 老站不落后于新站
        assert aged[0] is items[1]

    def test_aging_starvation_overtake_in_round(self) -> None:
        """预算只够一项:默认选高波动新站,开启 aging 后选饿满一周的老站。"""
        items = [item("new", 0.9), item("old", 0.2, age_h=200)]
        assert [p["url"] for p in select_round(items, 1.0)] == ["new"]
        picked = select_round(items, 1.0, weights={"aging": 0.8})
        # 老:0.45*0.2 + 0.8*1 = 0.89 > 新:0.45*0.9 = 0.405
        assert [p["url"] for p in picked] == ["old"]


# ---------------------------------------------------------------------------
# V10 变成本背包:逐项成本 pages × min_interval_s
# ---------------------------------------------------------------------------
def legacy_equal_cost_round(items: list[dict], budget: float, cost: float) -> list[dict]:
    """旧算法(等成本 + 装不下即停)的测试内复刻,仅用于退化一致性对照。

    与 V7 实现**逐行同构**:priority 降序稳定排序 → 依次装入恒定成本
    cost,预算不足即提前终止(容差 1e-9 与内核 _EPS 一致)。
    """
    ranked = sorted(items, key=priority, reverse=True)
    remaining = float(budget)
    picked: list[dict] = []
    for it in ranked:
        if remaining + 1e-9 < cost:
            break
        picked.append(it)
        remaining -= cost
    return picked


class TestVariableCost:
    def test_density_greedy_prefers_cheap_high_value(self) -> None:
        """价值密度贪心:满分 4 页大站(密度 0.25)装不下 3 预算被跳过,
        密度次之的 2 页中站 + 1 页小站被装入——旧"提前终止"会得空列表。"""
        items = [
            item("big", 1.0, 1.0, 168, pages=4),  # value 1.0, cost 4, 密度 0.25
            item("mid", 0.5, 0.5, pages=2),       # value 0.4, cost 2, 密度 0.2
            item("small", 0.3, pages=1),          # value 0.135, cost 1, 密度 0.135
        ]
        picked = select_round(items, 3.0)
        assert [p["url"] for p in picked] == ["mid", "small"]
        assert legacy_equal_cost_round(items, 3.0, 1.0) != picked  # 旧口径确实不同

    def test_continue_after_unfit_fills_budget(self) -> None:
        """装不下则 continue:密度最高的 3 页项(1/3)装不进 2.5 预算,
        跳过后仍用 2 页 + 半页项把预算装满(硬上界 2.5 不破)。"""
        items = [
            item("big", 1.0, 1.0, 168, pages=3),  # 密度 1/3 最高,但 3 > 2.5
            item("mid", 0.5, 0.5, pages=2),       # value 0.4, cost 2
            item("tiny", 0.1, pages=0.5),         # value 0.045, cost 0.5
        ]
        picked = select_round(items, 2.5)
        assert [p["url"] for p in picked] == ["mid", "tiny"]
        total = sum(p["pages"] * 1.0 for p in picked)
        assert total == pytest.approx(2.5)  # 预算恰好装满,一分不超(± 容差)

    def test_highest_raw_priority_can_lose_to_density(self) -> None:
        """贪心价值密度 ≠ 优先级序:最高优先级的 10 页大站可能落选。"""
        items = [
            item("huge", 1.0, 1.0, 168, pages=10),
            item("quick", 0.5, pages=1),
        ]
        assert priority(items[0]) > priority(items[1])  # 优先级反转的成因确认
        assert [p["url"] for p in select_round(items, 2.0)] == ["quick"]

    @pytest.mark.parametrize("pages_const", [None, 1, 2.5, 7])
    @pytest.mark.parametrize(
        ("budget", "min_interval"),
        [
            (0.5, 1.0),
            (2.5, 1.0),
            (4.0, 1.0),
            (5.5, 1.0),
            (100.0, 1.0),
            (0.3, 0.1),  # 浮点表示防御场景
            (6.0, 1.5),
            (6.0, 2.0),
        ],
    )
    def test_equal_cost_degeneration_matches_legacy(
        self, pages_const: float | None, budget: float, min_interval: float
    ) -> None:
        """**等成本退化一致性(核心)**:成本恒定时(pages 全缺 / 全 1 /
        全 2.5 / 全 7),变成本新路径与测试内复刻的旧算法**逐项相等**。"""
        items = ten_items()
        if pages_const is not None:
            items = [dict(it, pages=pages_const) for it in items]
        cost = (pages_const or 1) * min_interval
        assert select_round(items, budget, min_interval_s=min_interval) == (
            legacy_equal_cost_round(items, budget, cost)
        )

    @pytest.mark.parametrize("budget", [0.0, 0.5, 1.0, 2.5, 3.0, 7.5])
    @pytest.mark.parametrize("min_interval", [0.5, 1.0, 2.0])
    def test_budget_hard_upper_bound_invariant(
        self, budget: float, min_interval: float
    ) -> None:
        """预算硬上界不变量:任意场景网格下,选中项成本之和 ≤ 预算 + 容差。"""
        items = [
            item(f"u{i}", vol=i / 10, risk=(7 - i) / 10, pages=i % 4)  # 含零成本页
            for i in range(8)
        ]
        picked = select_round(items, budget, min_interval_s=min_interval)
        total = sum(p["pages"] * min_interval for p in picked)
        assert total <= budget + 1e-6
        assert all(p["pages"] * min_interval <= budget + 1e-6 for p in picked)

    def test_zero_cost_items_are_free(self) -> None:
        """零成本边界:pages=0(及负数脏值钳 0)恒可入选,连零预算也白拿、
        排在一切正成本项之前;负预算则一分不花(零成本也不选)。"""
        items = [
            item("free", 0.0, pages=0),
            item("neg", 0.0, pages=-3),
            item("paid", 0.9, pages=1),
        ]
        assert [p["url"] for p in select_round(items, 0.0)] == ["free", "neg"]
        assert select_round(items, -0.5) == []
        assert [p["url"] for p in select_round(items, 1.0)] == ["free", "neg", "paid"]

    def test_huge_and_infinite_cost_never_fit(self) -> None:
        """极大成本边界:超大页数 / inf 页数任何有限预算都装不下,自然跳过。"""
        items = [
            item("huge", 1.0, pages=10**9),
            item("inf", 1.0, pages=math.inf),
            item("ok", 0.1, pages=1),
        ]
        assert [p["url"] for p in select_round(items, 10.0**6)] == ["ok"]
        assert select_round([items[1]], 1e308) == []  # 单只无穷成本项 → 空

    def test_dirty_pages_fall_back_to_one_page(self) -> None:
        """pages 脏值(None / 非数 / NaN)按 1 页计 → 与缺键的等成本
        旧口径选取结果一致。"""
        clean = [item("a", 0.5), item("b", 0.2)]
        expected = [p["url"] for p in select_round(clean, 1.0)]
        assert expected == ["a"]
        dirty = [dict(clean[0], pages=None), dict(clean[1], pages="many")]
        nan_like = [dict(clean[0], pages=math.nan), dict(clean[1], pages=[])]
        assert [p["url"] for p in select_round(dirty, 1.0)] == expected
        assert [p["url"] for p in select_round(nan_like, 1.0)] == expected

    def test_fractional_pages_supported(self) -> None:
        """非整数页数按比例计成本:预算 2.5 装 5 个半页项(第 6 项装不下)。"""
        items = [item(f"u{i}", vol=0.5, pages=0.5) for i in range(6)]
        assert len(select_round(items, 2.5)) == 5

    def test_return_order_is_non_increasing_density(self) -> None:
        """返回序 = 贪心决策序:选中项的价值密度单调不增(稳定平局除外)。"""
        items = [item(f"u{i}", vol=(9 - i) / 10, pages=(i % 3) + 1) for i in range(9)]
        picked = select_round(items, 6.0)
        assert picked  # 确定性构造下必非空
        densities = [priority(p) / (p["pages"] * 1.0) for p in picked]
        assert densities == sorted(densities, reverse=True)

    def test_single_pass_scoring_with_costs_and_weights(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """操作计数不变量(V10):逐项成本 + 自定义权重下仍**每项恰一次**
        priority 调用;预算、页数、权重变化均不触发任何一次重扫。"""
        calls = {"n": 0}
        real = sched_kernel.priority

        def counting(it: dict, *, weights: dict | None = None) -> float:
            calls["n"] += 1
            return real(it, weights=weights)

        monkeypatch.setattr(sched_kernel, "priority", counting)
        items = [item(f"u{i}", vol=i / 8, pages=i + 1) for i in range(8)]
        assert len(select_round(items, 3.0)) >= 1
        assert calls["n"] == 8  # 单遍打分:每项恰一次
        calls["n"] = 0
        select_round(items, 100.0, weights={"aging": 0.2})
        assert calls["n"] == 8  # 预算翻数十倍 + aging 开启,打分次数不变

    def test_input_not_mutated_and_deterministic(self) -> None:
        """纯函数不变量延伸:pages / age_h / weights 路径同样不改输入、
        返回原对象引用、两次调用结果完全一致。"""
        items = [
            item("a", 0.9, 0.1, 24, age_h=30, pages=2),
            item("b", 0.1, 0.2, 100, age_h=300, pages=0.5),
        ]
        snapshot = [dict(it) for it in items]
        first = select_round(items, 1.5, weights={"aging": 0.4})
        assert [dict(it) for it in items] == snapshot  # 内容未动
        assert select_round(items, 1.5, weights={"aging": 0.4}) == first  # 确定性
        assert all(any(p is orig for orig in items) for p in first)  # 引用透传


# ---------------------------------------------------------------------------
# 可观测性
# ---------------------------------------------------------------------------
class TestTelemetry:
    def test_round_counter_increments(self) -> None:
        """每轮决策 +1(空轮也算);非法参数抛错不计数。"""
        telemetry.reset()
        select_round(ten_items(), 4.0)
        select_round([], 4.0)
        select_round(ten_items(), 0.0)  # 预算不足 → 空轮仍计数
        with pytest.raises(ValueError):
            select_round(ten_items(), 4.0, min_interval_s=0.0)
        assert telemetry.snapshot()["counters"]["sched_kernel.round"] == 3


# ---------------------------------------------------------------------------
# V7 内核自检(A138 基准总控对接)
# ---------------------------------------------------------------------------
class TestSelfcheck:
    def test_kernel_selfcheck_deterministic(self) -> None:
        """自检主四键口径不变(value=baseline=4);V10 附加两键(变成本
        退化一致 / aging 默认位级等价)同为确定性结果;两次调用完全一致。"""
        first = kernel_selfcheck()
        assert set(first) == {
            "name",
            "metric",
            "value",
            "baseline",
            "varcost_degenerate_top4",
            "aging_default_weight_equivalent",
        }
        assert first["name"] == "sched_kernel"
        assert first["value"] == first["baseline"] == 4
        assert first["varcost_degenerate_top4"] == 4  # pages=1 等成本退化仍选 4
        assert first["aging_default_weight_equivalent"] is True  # 位级等价铁证
        assert kernel_selfcheck() == first


# ---------------------------------------------------------------------------
# V7 基准(红线 31:操作计数 / 确定性数据精确结果,禁墙钟)
# ---------------------------------------------------------------------------
class TestV7Bench:
    def test_v7_bench_top4_of_10_exact(self) -> None:
        """确定性数据精确结果:10 项 budget=4 恰选 4 项,与全排序前 4 逐项一致。

        代差来源:旧顺序巡查(scheduler.run_once)按 watchlist 原序逐项扫,
        预算下先扫到谁全凭列表顺序;新内核在等预算下**精确锁定位次最高的
        4 项**(与全量排序切片完全相等,可复现、不依赖任何计时)。
        """
        items = ten_items()
        picked = select_round(items, 4.0)
        full_ranking = sorted(items, key=priority, reverse=True)
        assert picked == full_ranking[:4]  # 与"全排序取前 4"逐项相等
        assert len(picked) == 4  # floor(4.0/1.0) = 4
        # 选中的恰是全体优先级中最高的 4 个值(降序)
        top4_values = sorted((priority(it) for it in items), reverse=True)[:4]
        assert [priority(p) for p in picked] == pytest.approx(top4_values)
        # 未入选的 6 项优先级无一高于入选最低者
        picked_min = min(priority(p) for p in picked)
        assert all(priority(it) <= picked_min for it in items if it not in picked)

    def test_v7_bench_single_pass_ranking_operation_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """操作计数断言:排序打分每项恰一次(10 项 → 10 次 priority 调用),
        装入阶段零重扫;预算从 4 变 8 不增加任何一次打分。"""
        calls = {"n": 0}
        real = sched_kernel.priority

        def counting(it: dict) -> float:
            calls["n"] += 1
            return real(it)

        monkeypatch.setattr(sched_kernel, "priority", counting)
        picked4 = select_round(ten_items(), 4.0)
        assert calls["n"] == 10  # 单遍打分:每项恰一次
        calls["n"] = 0
        picked8 = select_round(ten_items(), 8.0)
        assert calls["n"] == 10  # 预算翻倍,打分次数不变(不随预算重扫)
        assert len(picked4) == 4 and len(picked8) == 8
