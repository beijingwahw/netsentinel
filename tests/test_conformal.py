"""A45 netsentinel.decision.conformal 共形式精度担保测试。

纯数据构造、离线、无 IO / 网络。覆盖(CONTRACTS-V3 §3 A45):
- 完美分离校准集 → 阈值落在真假边界、经验精度 1.0、valid=True;
- 含噪集(假高分嵌在中段)→ 达标最大前缀在噪声之前截断、精度达标;
- 无法达标(噪声在最顶)→ 锁定语义:不存在达标前缀 ⇒ valid=False、threshold=None;
- 并列分值整组进出:组边界担保,不会给出"选进半组"的不诚实阈值;
- 阈值随目标精度单调(目标越高阈值不降);
- n<30 降级(valid=False、threshold=None、无数字背书),n=30 边界放行;
- 全负样本降级;target 非法(∉(0.5,1))抛 ValueError;
- apply 选集与降序排序、阈值 None 全拒、边界等值入选;
- merge_reports 含"前提 / 样本 / 95% 或目标值"等关键词,extra_note 追加;
- 脏数据(非二元组 / 非数值分值 / 非 bool 标签)防御式 ValueError。

V5 新增(test_v5_* 前缀):conformal.fit 计时与 conformal.degraded 降级计数、
"扫描不提前 break、取最大达标前缀"的回归锁定(精度先跌破目标后回升)。

V10 新增(test_v10_* 前缀,Conformal Risk Control / Learn-then-Test 升级):
- 确定性:同输入两次拟合结果 dict 完全相等(纯浮点、无随机源);
- Clopper-Pearson 精确 (1-δ) 下界:全真前缀闭式 δ^(1/m)、CP 下界 ≤ 点估计、
  下界处尾概率恰等于 δ(测试内用 math.comb 直构整数组合数做**独立**参考实现,
  与被测 lgamma 对数空间路径交叉验证);
- Learn-then-Test + Bonferroni:校正水平 = δ/候选数;认证通过的前缀其精确
  二项 p 值必 ≤ 校正水平、CP 下界必 ≥ 目标;更深一层候选必不通过(取的是
  "最深通过前缀");并列分组只认组边界(认证永不切半组);
- 行为向后兼容锁定:生效 threshold 与 δ 无关、恒等于既有经验选择;
  LTT 认证结果(ltt_*)仅以新增字段建议式随行;
- δ 参数:非法值 ValueError;降级结果统计字段一律为 None。
  注:"真率 1.0 时 CP 下界 = δ^(1/n) < 1"(有限样本上宣称下界=1 不成立,
  那不是 Clopper-Pearson;点估计才 =1.0)——按诚实统计断言。
"""
from __future__ import annotations

import math

import pytest

from netsentinel import telemetry
from netsentinel.decision.conformal import (
    CAVEAT,
    MIN_CALIBRATION_N,
    apply,
    fit_threshold,
    merge_reports,
)


# ---------------------------------------------------------------------------
# 辅助构造
# ---------------------------------------------------------------------------


def span(
    start: float, step: float, count: int, label: bool
) -> list[tuple[float, bool]]:
    """等差生成 (分值, 标签) 校准条目;round(10) 让浮点值干净可读。"""
    return [(round(start + step * i, 10), label) for i in range(count)]


def has_cjk(text: str) -> bool:
    """断言文案是中文(担保/说明不允许只给英文或空串)。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


# ---------------------------------------------------------------------------
# fit_threshold:有效担保
# ---------------------------------------------------------------------------


def test_fit_perfect_separation_valid() -> None:
    """20 真(0.80~0.99)+ 20 假(0.30~0.49)完全可分,target=0.99。

    最大达标前缀恰为 20 条全真(k=21 时 20/21≈0.952<0.99):
    阈值落在真假边界(=最低真分 0.80,高于最高假分 0.49),精度 1.0。
    """
    trues = span(0.80, 0.01, 20, True)
    falses = span(0.30, 0.01, 20, False)
    fit = fit_threshold(trues + falses, 0.99)

    assert fit["valid"] is True
    assert fit["n"] == 40
    assert fit["threshold"] == pytest.approx(min(s for s, _ in trues))
    assert fit["threshold"] > max(s for s, _ in falses)
    assert fit["empirical_precision"] == pytest.approx(1.0)
    assert "1.00" in fit["guarantee"] and "0.99" in fit["guarantee"]
    assert has_cjk(fit["guarantee"])
    assert fit["caveat"] == CAVEAT
    assert fit["target_precision"] == 0.99


def test_fit_noisy_middle_truncates_before_noise() -> None:
    """含噪集:1 条假高分(0.75)嵌在 18 条真分(0.82~0.99)之下。

    k=19 时 18/19≈0.947<0.95 ⇒ 达标最大前缀停在 k=18:
    阈值 = 最低真分 0.82 > 噪声分 0.75,名单在噪声之前截断。n=30 恰在放行边界。
    """
    calib = (
        span(0.82, 0.01, 18, True)
        + [(0.75, False)]
        + span(0.20, 0.01, 11, False)
    )
    fit = fit_threshold(calib, 0.95)

    assert fit["valid"] is True
    assert fit["n"] == 30
    assert fit["threshold"] == pytest.approx(0.82)
    assert fit["threshold"] > 0.75  # 噪声(假高分)被截在名单之外
    assert fit["empirical_precision"] >= 0.95
    assert fit["empirical_precision"] == pytest.approx(1.0)


def test_fit_tie_scores_move_as_whole_groups() -> None:
    """并列分值整组进出:0.80 处 1 真 3 假的同分组。

    若按"逐条前缀"贪心可停在组内第 2 条(k=22,21/22≈0.9545≥0.95),
    但部署时同分不可区分 ⇒ 担保只认组边界(整组入后 21/24=0.875<0.95):
    阈值退到 0.81,选集恰为 20 条真,精度 1.0(诚实优先于 recall)。
    """
    calib = (
        span(0.81, 0.01, 20, True)
        + [(0.80, True), (0.80, False), (0.80, False), (0.80, False)]
        + span(0.30, 0.01, 7, False)
    )
    fit = fit_threshold(calib, 0.95)

    assert fit["valid"] is True
    assert fit["threshold"] == pytest.approx(0.81)
    assert fit["empirical_precision"] == pytest.approx(1.0)
    # 同分组整组拒绝:0.80 不入选
    out = apply([("tie", 0.80), ("true", 0.99)], fit["threshold"])
    assert [sid for sid, _ in out["selected"]] == ["true"]


def test_fit_all_positive_calibration() -> None:
    """全正样本(n≥30):任意前缀精度 1.0,最大前缀=全集,阈值=最低分。"""
    calib = span(0.50, 0.01, 35, True)
    fit = fit_threshold(calib, 0.95)

    assert fit["valid"] is True
    assert fit["threshold"] == pytest.approx(0.50)
    assert fit["empirical_precision"] == pytest.approx(1.0)


def test_fit_threshold_monotone_in_target() -> None:
    """阈值单调性:目标精度越高,拟合阈值不减(更严的门槛不会更松)。

    30 真(0.70~0.99)+ 15 假(0.30~0.44):手算各目标下的达标最大前缀
    依次为 k=30/31/33/37/45,阈值 0.70/0.44/0.42/0.38/0.30 单调不增。
    """
    calib = span(0.70, 0.01, 30, True) + span(0.30, 0.01, 15, False)
    targets = [0.99, 0.95, 0.9, 0.8, 0.6]

    fits = [fit_threshold(calib, t) for t in targets]
    assert all(f["valid"] for f in fits)
    thresholds = [f["threshold"] for f in fits]
    assert thresholds == sorted(thresholds, reverse=True)  # 目标降序 ⇒ 阈值不增
    assert thresholds[0] == pytest.approx(0.70)  # 0.99:只收 30 条真
    assert thresholds[-1] == pytest.approx(0.30)  # 0.6:全集入选
    for f, t in zip(fits, targets):
        assert f["empirical_precision"] >= t  # 每档经验精度均达标


# ---------------------------------------------------------------------------
# fit_threshold:降级与拒绝
# ---------------------------------------------------------------------------


def test_fit_unattainable_noise_on_top_degrades() -> None:
    """无法达标(锁定语义):噪声(假)占最高分 0.999。

    含噪声前缀精度上限 18/19≈0.947<0.95,后续只降 ⇒ 不存在达标前缀:
    valid=False、threshold=None、empirical_precision=None,guarantee 只讲
    原因,不做任何数字背书。
    """
    calib = (
        [(0.999, False)]
        + span(0.80, 0.01, 18, True)
        + span(0.20, 0.01, 11, False)
    )
    fit = fit_threshold(calib, 0.95)

    assert fit["n"] == 30  # 样本量本身够,纯粹是"无法达标"降级
    assert fit["valid"] is False
    assert fit["threshold"] is None
    assert fit["empirical_precision"] is None
    assert "不存在" in fit["guarantee"]
    assert has_cjk(fit["guarantee"])


def test_fit_small_sample_degrades() -> None:
    """n<30 降级:哪怕完美可分也不给担保;29 条同样降级,30 条边界放行。"""
    tiny = span(0.95, -0.05, 5, True) + span(0.40, -0.05, 5, False)
    assert len(tiny) == 10
    fit = fit_threshold(tiny, 0.95)

    assert fit["valid"] is False
    assert fit["threshold"] is None
    assert fit["empirical_precision"] is None
    assert fit["n"] == 10
    assert str(MIN_CALIBRATION_N) in fit["guarantee"]  # 中文说明点名样本量门槛
    assert has_cjk(fit["guarantee"])

    n29 = span(0.85, 0.01, 15, True) + span(0.30, 0.01, 14, False)
    assert len(n29) == 29
    fit29 = fit_threshold(n29, 0.95)
    assert fit29["valid"] is False
    assert fit29["threshold"] is None


def test_fit_all_negative_degrades() -> None:
    """全负样本(n≥30):精度无从定义 → 降级,不输出阈值。"""
    calib = span(0.10, 0.02, 35, False)
    fit = fit_threshold(calib, 0.95)

    assert fit["n"] == 35
    assert fit["valid"] is False
    assert fit["threshold"] is None
    assert fit["empirical_precision"] is None
    assert "真实违规" in fit["guarantee"]


def test_fit_invalid_target_raises() -> None:
    """target 必须严格落在 (0.5, 1):边界 0.5 / 1.0 与越界值均 ValueError。"""
    calib = span(0.80, 0.01, 20, True) + span(0.30, 0.01, 15, False)
    for bad in (0.5, 1.0, 0.0, 1.5, -0.2, float("nan"), "0.95"):
        with pytest.raises(ValueError):
            fit_threshold(calib, bad)  # type: ignore[arg-type]

    # 合法开区间内的取值不因参数校验而失败(空集走样本量降级)
    degraded = fit_threshold([], 0.51)
    assert degraded["valid"] is False and degraded["threshold"] is None


def test_fit_malformed_entries_raise() -> None:
    """脏数据防御:非二元组 / 非数值分值 / 非 bool 标签一律 ValueError(中文)。"""
    with pytest.raises(ValueError, match="分值"):
        fit_threshold([(0.9, True), ("bad", False)], 0.95)
    with pytest.raises(ValueError, match="标签"):
        fit_threshold([(0.9, "yes")], 0.95)
    with pytest.raises(ValueError, match="二元组"):
        fit_threshold([(0.9, True), [0.8, True, 0]], 0.95)
    with pytest.raises(ValueError, match="二元组"):
        fit_threshold([0.9], 0.95)


# ---------------------------------------------------------------------------
# apply:选集与排序
# ---------------------------------------------------------------------------


def test_apply_selects_desc_and_boundary_inclusive() -> None:
    """分值≥阈值入选、按分值降序输出;恰等于阈值的条目入选(闭边界)。"""
    scores = [("d", 0.50), ("a", 0.99), ("c", 0.70), ("b", 0.85), ("e", 0.75)]
    out = apply(scores, 0.75)

    assert out["selected"] == [("a", 0.99), ("b", 0.85), ("e", 0.75)]
    assert out["rejected"] == 2
    assert has_cjk(out["expected_precision_note"])
    assert "人工" in out["expected_precision_note"]  # 提醒复核不可省略


def test_apply_none_threshold_selects_nothing() -> None:
    """threshold=None(拟合降级):宁可不报警,也不输出无担保的名单。"""
    out = apply([("a", 0.99), ("b", 0.10)], None)

    assert out["selected"] == []
    assert out["rejected"] == 2
    assert "无有效阈值" in out["expected_precision_note"]
    assert has_cjk(out["expected_precision_note"])


def test_apply_all_below_threshold() -> None:
    out = apply([("a", 0.10), ("b", 0.20)], 0.82)
    assert out["selected"] == []
    assert out["rejected"] == 2


def test_apply_malformed_raises() -> None:
    with pytest.raises(ValueError, match="分值"):
        apply([("a", "x")], 0.5)
    with pytest.raises(ValueError, match="二元组"):
        apply([("a",)], 0.5)


# ---------------------------------------------------------------------------
# 端到端 + merge_reports
# ---------------------------------------------------------------------------


def test_end_to_end_fit_then_apply_rejects_noise() -> None:
    """拟合 → 应用:嵌中段噪声(0.75)不入名单,真分入选且降序。"""
    calib = (
        span(0.82, 0.01, 18, True)
        + [(0.75, False)]
        + span(0.20, 0.01, 11, False)
    )
    fit = fit_threshold(calib, 0.95)
    out = apply(
        [("t1", 0.91), ("noise", 0.75), ("t2", 0.83), ("low", 0.20)],
        fit["threshold"],
    )

    assert [sid for sid, _ in out["selected"]] == ["t1", "t2"]
    assert out["rejected"] == 2


def test_merge_reports_contains_premise_sample_and_target() -> None:
    """报告必须含:前提、样本量、目标值(95% 或 0.95)、免责;附注追加。"""
    calib = (
        span(0.82, 0.01, 18, True)
        + [(0.75, False)]
        + span(0.20, 0.01, 11, False)
    )
    fit = fit_threshold(calib, 0.95)
    text = merge_reports(fit)

    assert "前提" in text
    assert "样本" in text
    assert "95%" in text or "0.95" in text
    assert "免" in text  # 免责声明在场
    assert "0.82" in text  # 阈值可见
    assert has_cjk(text)

    extra = "校准集来源:2026-09 第 38 周人工核验 30 例"
    assert extra in merge_reports(fit, extra)


def test_merge_reports_on_degraded_fit_stays_honest() -> None:
    """降级结果同样能成文:明说无有效阈值,前提与免责不缺席,不编数字。"""
    fit = fit_threshold(span(0.95, -0.05, 5, True) + span(0.40, -0.05, 5, False), 0.95)
    text = merge_reports(fit, "等待人工核验样本积累")

    assert "无" in text and "前提" in text and "免" in text
    assert str(MIN_CALIBRATION_N) in text
    assert "等待人工核验样本积累" in text
    # 容错:空 dict 也能生成说明而不是抛异常
    assert has_cjk(merge_reports({}))


# ---------------------------------------------------------------------------
# V5:遥测(conformal.fit 计时 / conformal.degraded 计数)与扫描语义锁定
# ---------------------------------------------------------------------------


def test_v5_telemetry_fit_timer_and_degraded_counter() -> None:
    """每次拟合计时;conformal.degraded 只在降级(如小样本)时累计,
    有效担保的拟合不计数。"""
    valid_calib = (
        span(0.82, 0.01, 18, True)
        + [(0.75, False)]
        + span(0.20, 0.01, 11, False)
    )
    counters = telemetry.snapshot()["counters"]
    base_deg = counters.get("conformal.degraded", 0.0)
    base_runs = telemetry.snapshot()["timers"].get("conformal.fit", {}).get("count", 0)

    fit = fit_threshold(valid_calib, 0.95)
    assert fit["valid"] is True
    assert (
        telemetry.snapshot()["counters"].get("conformal.degraded", 0.0) == base_deg
    )  # 有效拟合不计数

    tiny = fit_threshold(
        span(0.95, -0.05, 5, True) + span(0.40, -0.05, 5, False), 0.95
    )
    assert tiny["valid"] is False
    assert (
        telemetry.snapshot()["counters"].get("conformal.degraded", 0.0)
        == base_deg + 1.0
    )

    assert telemetry.snapshot()["timers"]["conformal.fit"]["count"] >= base_runs + 2


def test_v5_scan_takes_largest_qualifying_prefix_after_dip() -> None:
    """V5 扫描语义锁定:精度先跌破目标(17/18≈0.944<0.95)后随正样本回升,
    贪心"首个达标前缀"会停在 k=17,正确语义取**最大**达标前缀 k=41
    (40/41≈0.9756≥0.95),阈值=最低分 0.60——不提前 break 的回归锁。"""
    calib = (
        span(1.00, -0.01, 17, True)   # 1.00 ~ 0.84,k=17 时精度 1.0
        + [(0.83, False)]              # k=18:17/18≈0.9444 < 0.95(跌破)
        + span(0.82, -0.01, 23, True)  # 0.82 ~ 0.60,k=41:40/41≈0.9756(回升)
    )
    assert len(calib) == 41
    fit = fit_threshold(calib, 0.95)

    assert fit["valid"] is True
    assert fit["threshold"] == pytest.approx(0.60)  # 最大前缀的最后一条
    assert fit["empirical_precision"] == pytest.approx(40 / 41, abs=1e-4)
    # 对照:若贪心停在 k=17,阈值应为 0.84——断言显式排除该错误实现
    assert fit["threshold"] != pytest.approx(0.84)


# ---------------------------------------------------------------------------
# V10:有限样本统计担保(Clopper-Pearson 精确下界 + Learn-then-Test 校正)
# ---------------------------------------------------------------------------


def ref_binom_upper_tail(k: int, m: int, p: float) -> float:
    """独立参考实现:math.comb 精确整数组合数直接求和 P(Bin(m,p) ≥ k)。

    与被测模块的 lgamma 对数空间路径互为独立实现,交叉验证精确性;
    m ≤ 200 时整数组合数 ~1e59,float 换算无溢出。
    """
    return float(
        sum(math.comb(m, j) * p**j * (1.0 - p) ** (m - j) for j in range(k, m + 1))
    )


#: V10 主数据集 A:170 真(0.830~0.999)+ 30 假(0.500~0.529),n=200,目标 0.9。
#: 既有经验选择:k=188(170/188≈0.904≥0.9,阈值 0.512);
#: Bonferroni(δ=0.05,|Λ|=200)校正水平 2.5e-4 下最深通过前缀 k=174
#: (p≈7.7e-5 通过)、k=175(p≈2.8e-4 不通过)→ 认证阈值 0.526。
CALIB_170_30 = span(0.830, 0.001, 170, True) + span(0.500, 0.001, 30, False)

#: V10 数据集 B:160 真单值 + 0.70 处并列组(1 真 9 假),n=170,目标 0.9。
#: 组边界只有 160 与 170 两个"整组"深度;脏并列组边界 k=161/170
#: (p≈0.027 ≫ 校正水平)不通过,认证停在干净前缀 160(阈值 0.800)。
CALIB_TIE_DIRTY = span(0.800, 0.001, 160, True) + [(0.70, True)] + [(0.70, False)] * 9


def test_v10_deterministic_same_input_same_output() -> None:
    """确定性:固定输入 → 固定输出(两次调用结果 dict 逐键相等,含全部浮点字段)。"""
    for calib, target, delta in (
        (CALIB_170_30, 0.9, 0.05),
        (CALIB_170_30, 0.9, 0.01),
        (CALIB_TIE_DIRTY, 0.9, 0.05),
        (span(0.500, 0.003, 160, True), 0.9, 0.05),
    ):
        assert fit_threshold(calib, target, delta) == fit_threshold(calib, target, delta)


def test_v10_cp_lower_bound_closed_form_all_true() -> None:
    """全真前缀(k=m):CP (1-δ) 下界有闭式 δ^(1/m);p 值 = 目标^m。

    注意诚实口径:真率 1.0 是**点估计**;有限样本上 CP 下界 = 0.05^(1/160)
    ≈ 0.9815 < 1(宣称"下界=1"在统计上不成立,恰是本升级要纠正的过度承诺)。
    160 条全真、目标 0.9:整个前缀通过 Bonferroni 校正 → selection_mode=ltt,
    此时数学事实"CP 下界 ≥ 目标"必须成立。
    """
    calib = span(0.500, 0.003, 160, True)
    fit = fit_threshold(calib, 0.9)

    assert fit["valid"] is True
    assert fit["selection_mode"] == "ltt"
    assert fit["threshold"] == pytest.approx(0.500)
    assert fit["empirical_precision"] == pytest.approx(1.0)  # 点估计 =1
    assert fit["lower_bound"] == pytest.approx(0.05 ** (1.0 / 160), rel=1e-9)
    assert fit["lower_bound"] < 1.0  # 有限样本下界恒 <1(诚实优先)
    assert fit["lower_bound"] >= 0.9 - 1e-9  # 通过校正 ⟹ 下界 ≥ 目标
    assert fit["p_value"] == pytest.approx(0.9**160, rel=1e-9)
    assert fit["p_value"] <= fit["corrected_alpha"]
    assert fit["corrected_alpha"] == pytest.approx(0.05 / 160, rel=1e-9)
    assert fit["n_candidates"] == 160
    assert fit["confidence"] == pytest.approx(0.95)
    assert fit["ltt_threshold"] == fit["threshold"]  # 认证的就是生效选择


def test_v10_cp_lower_bound_leq_point_estimate_and_root_property() -> None:
    """统计性质:CP 下界 ≤ 点估计;下界处精确尾概率 = δ(定义验证);p 值交叉验证。

    用 math.comb 直构整数组合数的独立求和验证被测 lgamma 路径的精确性,
    三组覆盖 k=m / k<m / 混合噪声场景。
    """
    fits = [
        fit_threshold(CALIB_170_30, 0.9),  # 生效前缀 k=170, m=188
        fit_threshold(span(0.70, 0.01, 30, True) + span(0.30, 0.01, 15, False), 0.6),
        fit_threshold(span(0.80, 0.01, 20, True) + span(0.30, 0.01, 20, False), 0.99),
        fit_threshold(CALIB_TIE_DIRTY, 0.9),  # 生效前缀 k=161, m=170
    ]
    sel = [(170, 188, 0.9), (30, 45, 0.6), (20, 20, 0.99), (161, 170, 0.9)]
    for fit, (k, m, target) in zip(fits, sel):
        prec = fit["empirical_precision"]
        lb = fit["lower_bound"]
        assert 0.0 < lb <= prec + 1e-12  # CP 下界 ≤ 点估计
        assert fit["p_value"] == pytest.approx(
            ref_binom_upper_tail(k, m, target), rel=1e-9
        )
        if k < m:  # 下界定义:P(Bin(m, lb) ≥ k) = δ(k=m 时已由闭式用例覆盖)
            assert ref_binom_upper_tail(k, m, lb) == pytest.approx(0.05, abs=1e-6)
        assert fit["corrected_alpha"] == pytest.approx(0.05 / fit["n_candidates"], rel=1e-9)


def test_v10_ltt_advisory_certifies_conservative_threshold() -> None:
    """LTT 认证层:生效阈值保持既有经验值(向后兼容),认证阈值以建议随行。

    CALIB_170_30 / 目标 0.9:经验最深前缀 k=188 本身 p≈0.48 ≫ 校正水平
    (未通过)→ mode=ltt_advisory;最深**通过**前缀 k=174(p≈7.7e-5),
    更深一档 k=175(p≈2.8e-4)必不通过——"最深通过前缀"的两侧锁定。
    """
    fit = fit_threshold(CALIB_170_30, 0.9)

    assert fit["valid"] is True
    assert fit["threshold"] == pytest.approx(0.512)  # 既有经验语义,逐字节不变
    assert fit["empirical_precision"] == pytest.approx(170 / 188, abs=1e-4)
    assert fit["selection_mode"] == "ltt_advisory"
    assert fit["ltt_threshold"] == pytest.approx(0.526)
    assert fit["ltt_prefix_size"] == 174
    assert fit["ltt_threshold"] > fit["threshold"]  # 认证阈值只可能更保守
    assert fit["n_candidates"] == 200
    assert fit["corrected_alpha"] == pytest.approx(0.05 / 200, rel=1e-9)

    # 认证前缀的统计性质:从 ltt_threshold 反推前缀,独立重算精确二项检验
    k_sel = sum(1 for s, lab in CALIB_170_30 if s >= fit["ltt_threshold"] and lab)
    m_sel = sum(1 for s, _ in CALIB_170_30 if s >= fit["ltt_threshold"])
    assert (k_sel, m_sel) == (170, 174)
    assert ref_binom_upper_tail(k_sel, m_sel, 0.9) <= fit["corrected_alpha"]
    # 更深一档(组边界 175)必不通过 → 选中的确是最深通过前缀
    assert ref_binom_upper_tail(170, 175, 0.9) > fit["corrected_alpha"]
    # 认证前缀的 CP 下界:定义验证 + 必 ≥ 目标(通过校正的数学推论)
    assert ref_binom_upper_tail(170, 174, fit["ltt_lower_bound"]) == pytest.approx(
        0.05, abs=1e-6
    )
    assert fit["ltt_lower_bound"] >= 0.9 - 1e-9
    # 生效前缀如实标注未通过:文案点名校正检验与建议,不粉饰
    assert "Bonferroni" in fit["guarantee"] and "建议" in fit["guarantee"]
    assert has_cjk(fit["guarantee"])


def test_v10_ltt_mode_when_empirical_max_certified() -> None:
    """经验最深前缀本身通过校正 → selection_mode=ltt,生效选择即被认证。

    160 真单值 + 0.70 并列组(1 真 3 假):经验最深前缀 = 全集 k=164
    (161/164≈0.982≥0.9),其 p≈3.68e-5 ≤ 0.05/161 → 认证通过。
    """
    calib = span(0.800, 0.001, 160, True) + [
        (0.70, True),
        (0.70, False),
        (0.70, False),
        (0.70, False),
    ]
    fit = fit_threshold(calib, 0.9)

    assert fit["selection_mode"] == "ltt"
    assert fit["n_candidates"] == 161  # 160 单值组 + 1 并列组:候选只认组边界
    assert fit["threshold"] == pytest.approx(0.70)
    assert fit["ltt_threshold"] == fit["threshold"]
    assert fit["ltt_prefix_size"] == 164
    assert fit["corrected_alpha"] == pytest.approx(0.05 / 161, rel=1e-9)
    assert fit["p_value"] == pytest.approx(ref_binom_upper_tail(161, 164, 0.9), rel=1e-9)
    assert fit["p_value"] <= fit["corrected_alpha"]
    assert fit["lower_bound"] >= 0.9 - 1e-9  # 认证 ⟹ CP 下界 ≥ 目标


def test_v10_certification_never_splits_tie_group() -> None:
    """并列分组语义:认证前缀只落在组边界,绝不"切半组"。

    脏并列组(0.70 处 1 真 9 假):组边界 m=170 的 p≈0.027 ≫ 校正水平 →
    认证停在干净前缀 m=160(阈值 0.800,0.70 整组排除);生效阈值仍为
    既有经验值 0.70(向后兼容,不变)。
    """
    fit = fit_threshold(CALIB_TIE_DIRTY, 0.9)

    assert fit["valid"] is True
    assert fit["threshold"] == pytest.approx(0.70)  # 既有经验语义:161/170≈0.947
    assert fit["selection_mode"] == "ltt_advisory"
    assert fit["ltt_threshold"] == pytest.approx(0.800)  # 干净前缀的组边界
    assert fit["ltt_prefix_size"] == 160
    assert fit["n_candidates"] == 161
    # 组边界 170 不通过、干净边界 160 通过(两侧锁定)
    assert ref_binom_upper_tail(161, 170, 0.9) > fit["corrected_alpha"]
    assert ref_binom_upper_tail(160, 160, 0.9) <= fit["corrected_alpha"]
    # apply 视角:认证阈值下 0.70 整组(含组内真样本)全排除,不切半组
    out = apply([("tie", 0.70), ("clean", 0.800)], fit["ltt_threshold"])
    assert [sid for sid, _ in out["selected"]] == ["clean"]


def test_v10_stricter_delta_shrinks_certified_prefix() -> None:
    """δ 可配:更严的误差预算 → 校正水平更小 → 认证前缀只可能更浅(更保守)。

    生效 threshold 与 δ 无关(行为向后兼容的一部分);confidence=1-δ 回显。
    """
    loose = fit_threshold(CALIB_170_30, 0.9, 0.05)
    strict = fit_threshold(CALIB_170_30, 0.9, 0.01)

    assert loose["corrected_alpha"] == pytest.approx(0.05 / 200, rel=1e-9)
    assert strict["corrected_alpha"] == pytest.approx(0.01 / 200, rel=1e-9)
    assert strict["corrected_alpha"] < loose["corrected_alpha"]
    assert strict["ltt_threshold"] >= loose["ltt_threshold"]  # 更严只会更保守
    assert strict["ltt_prefix_size"] <= loose["ltt_prefix_size"]
    assert strict["confidence"] == pytest.approx(0.99)
    assert loose["confidence"] == pytest.approx(0.95)
    # δ 不改变生效阈值与经验精度(红线:行为向后兼容)
    for fit in (loose, strict):
        assert fit["threshold"] == pytest.approx(0.512)
        assert fit["empirical_precision"] == pytest.approx(170 / 188, abs=1e-4)


def test_v10_invalid_delta_raises_and_degraded_carries_none() -> None:
    """delta 非法(∉(0,0.5) 开区间 / 非数值 / bool)→ ValueError(中文);
    降级结果统计字段一律 None:没做过检验就不给数字。"""
    calib = span(0.80, 0.01, 20, True) + span(0.30, 0.01, 15, False)
    for bad in (0.0, 0.5, 1.0, -0.1, 0.75, float("nan"), "0.05", True):
        with pytest.raises(ValueError, match="delta"):
            fit_threshold(calib, 0.95, bad)  # type: ignore[arg-type]

    fit = fit_threshold(span(0.95, -0.05, 5, True), 0.95)
    assert fit["valid"] is False
    assert fit["selection_mode"] == "degraded"
    assert fit["confidence"] == pytest.approx(0.95)
    for key in ("lower_bound", "p_value", "corrected_alpha", "n_candidates",
                "ltt_threshold", "ltt_prefix_size", "ltt_lower_bound"):
        assert fit[key] is None
    assert fit["delta"] == 0.05

    strict_small = fit_threshold(span(0.95, -0.05, 5, True), 0.95, 0.02)
    assert strict_small["confidence"] == pytest.approx(0.98)


def test_v10_merge_reports_shows_statistical_lines() -> None:
    """报告层:有效结果带"统计背书 / Learn-then-Test"行,建议阈值可见;
    降级结果不出现统计背书行(无数字可写,绝不编造)。"""
    fit = fit_threshold(CALIB_170_30, 0.9)
    text = merge_reports(fit)
    assert "统计背书" in text and "Clopper-Pearson" in text
    assert "Learn-then-Test" in text
    assert "95%" in text
    assert "统计认证建议" in text and "0.526" in text
    assert has_cjk(text)

    degraded_text = merge_reports(
        fit_threshold(span(0.95, -0.05, 5, True), 0.95)
    )
    assert "统计背书" not in degraded_text
    assert "前提" in degraded_text and "免" in degraded_text


def test_v10_legacy_keys_superset_and_values_unchanged() -> None:
    """API 兼容:新增字段只增不改——旧键全部在场,且 CALIB_170_30 上
    旧语义字段(threshold/empirical_precision/valid/n/guarantee 前缀)
    与升级前经验公式可手算值一致。"""
    fit = fit_threshold(CALIB_170_30, 0.9)
    legacy_keys = {
        "threshold", "guarantee", "empirical_precision", "n", "valid",
        "caveat", "target_precision",
    }
    assert legacy_keys <= set(fit)
    assert fit["n"] == 200 and fit["valid"] is True
    assert fit["caveat"] == CAVEAT
    assert fit["target_precision"] == 0.9
    base = f"分值≥{fit['threshold']} 的名单经验精度 {fit['empirical_precision']:.2f} ≥ 目标 {fit['target_precision']}"
    assert fit["guarantee"].startswith(base)  # 旧担保语句原样保留,统计说明只追加
