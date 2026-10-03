"""netsentinel.decision.abstain 单元测试(分歧驱动的选择性弃权 + 曲线)。

覆盖:
* 契约:AbstainDecision 恰为四字段契约且 frozen;默认阈值 0.35 对齐
  vision/arbiter.DISAGREE_GAP(只读对标);
* decide:高分歧 → 弃权、一致高分/低分 → 不弃权;两种分歧度量数学正确性
  (极差/总体标准差手算对照);闭边界(分歧恰等于阈值 → 弃权);
* 边界:单成员分歧恒 0 永不弃权(含阈值 0)、空输入不弃权;
* batch_decide:保序、与逐项 decide 一致、纯函数、确定性;
* sweep_disagreement_threshold:经典三点曲线手算对照、端点(阈 0=全覆盖、
  阈 ∞=零覆盖)、覆盖率随阈值单调不增(由构造保证)、乱序网格自动升序、
  二值化阈值可配、映射/列表输入等价、单成员永不进弃权集;
* to_triage_hint:键集、弃权加权(priority_weight=分歧度)、非弃权恒 0、
  p_nsfw 透传与校验、docstring 说明接线方式;
* 防御式校验:分数/阈值/度量/真值/网格非法一律中文 ValueError。

全部离线、纯内存、固定数据(确定性:同输入必同输出)。
"""
from __future__ import annotations

import dataclasses
import math

import pytest

from netsentinel.decision.abstain import (
    DEFAULT_ABSTAIN_THRESHOLD,
    DEFAULT_BINARIZE,
    METRIC_RANGE,
    METRIC_STD,
    AbstainDecision,
    batch_decide,
    decide,
    disagreement_range,
    disagreement_std,
    sweep_disagreement_threshold,
    to_triage_hint,
)
from netsentinel.vision.arbiter import DISAGREE_GAP

# ---------------------------------------------------------------------------
# 经典校准集(手算基准):极差度量、二值化 0.5
#   A=[0.9,0.95] d=0.05 mean=0.925 pred=1 label=1 对
#   B=[0.1,0.12] d=0.02 mean=0.11  pred=0 label=0 对
#   C=[0.9,0.10] d=0.80 mean=0.50  pred=1 label=1 对
#   D=[0.8,0.10] d=0.70 mean=0.45  pred=0 label=1 错(唯一误判)
# ---------------------------------------------------------------------------
CAL = [[0.9, 0.95], [0.1, 0.12], [0.9, 0.1], [0.8, 0.1]]
CAL_LABELS = [1, 0, 1, 1]


# ---------------------------------------------------------------------------
# 契约与常量
# ---------------------------------------------------------------------------


class TestContract:
    def test_decision_fields_exactly_four(self):
        names = [f.name for f in dataclasses.fields(AbstainDecision)]
        assert names == ["abstain", "disagreement", "threshold", "reason"]

    def test_decision_is_frozen(self):
        d = decide([0.9, 0.1])
        with pytest.raises(dataclasses.FrozenInstanceError):
            d.abstain = False  # type: ignore[misc]

    def test_default_threshold_aligned_with_arbiter_gap(self):
        # 只读对标:默认阈值与 arbiter 分歧阈值同值同语义(spread>=gap 升级)。
        assert DEFAULT_ABSTAIN_THRESHOLD == DISAGREE_GAP == 0.35
        assert DEFAULT_BINARIZE == 0.5
        assert METRIC_RANGE == "range" and METRIC_STD == "std"


# ---------------------------------------------------------------------------
# 分歧度量数学正确性(手算对照)
# ---------------------------------------------------------------------------


class TestMetricMath:
    def test_range_hand_computed(self):
        assert disagreement_range([0.1, 0.45]) == pytest.approx(0.35)
        assert disagreement_range([0.0, 0.35]) == pytest.approx(0.35)
        assert disagreement_range([0.9, 0.1]) == pytest.approx(0.8)
        assert disagreement_range([0.2, 0.5, 0.35]) == pytest.approx(0.3)

    def test_range_empty_and_single_are_zero(self):
        assert disagreement_range([]) == 0.0
        assert disagreement_range([0.7]) == 0.0

    def test_std_hand_computed(self):
        # [0.2, 0.4]: mean=0.3, 方差=(0.01+0.01)/2=0.01, std=0.1
        assert disagreement_std([0.2, 0.4]) == pytest.approx(0.1)
        # [0.0, 0.5, 1.0]: mean=0.5, 方差=(0.25+0.25+0)/3=1/6
        assert disagreement_std([0.0, 0.5, 1.0]) == pytest.approx(math.sqrt(1 / 6))
        # [0, 0, 1]: mean=1/3, 方差=((1/9)*2+(4/9))/3=2/9
        assert disagreement_std([0, 0, 1]) == pytest.approx(math.sqrt(2 / 9))

    def test_std_empty_and_single_are_zero(self):
        assert disagreement_std([]) == 0.0
        assert disagreement_std([0.7]) == 0.0

    def test_std_is_population_not_sample(self):
        # 总体口径除以 n:样本口径(n-1)会给 sqrt(0.02) ≈ 0.1414,手算区分。
        assert disagreement_std([0.2, 0.4]) == pytest.approx(0.1)
        assert disagreement_std([0.2, 0.4]) != pytest.approx(math.sqrt(0.02))


# ---------------------------------------------------------------------------
# decide:基本行为
# ---------------------------------------------------------------------------


class TestDecideBasic:
    def test_high_disagreement_abstains(self):
        d = decide([0.9, 0.1])
        assert d.abstain is True
        assert d.disagreement == pytest.approx(0.8)
        assert d.threshold == DEFAULT_ABSTAIN_THRESHOLD
        assert "弃权" in d.reason and "人工" in d.reason

    def test_consistent_high_scores_not_abstain(self):
        d = decide([0.92, 0.95, 0.9])
        assert d.abstain is False
        assert d.disagreement == pytest.approx(0.05)
        assert "自动判定" in d.reason

    def test_consistent_low_scores_not_abstain(self):
        d = decide([0.05, 0.08, 0.1])
        assert d.abstain is False
        assert d.disagreement == pytest.approx(0.05)

    def test_mapping_input_equals_list_input(self):
        assert decide({"stub": 0.9, "nudenet": 0.1}) == decide([0.9, 0.1])
        assert decide({"a": 0.2, "b": 0.25}) == decide([0.2, 0.25])

    def test_custom_threshold_echoed(self):
        d = decide([0.4, 0.6], threshold=0.5)
        assert d.threshold == 0.5
        assert d.abstain is False  # 极差 0.2 < 0.5

    def test_closed_boundary_disagreement_equal_threshold_abstains(self):
        # 闭边界(对齐 arbiter 的 spread >= gap):分歧恰等于阈值即弃权。
        assert decide([0.0, 0.35], threshold=0.35).abstain is True
        assert decide([0.0, 0.34], threshold=0.35).abstain is False

    def test_std_metric_abstain_behavior(self):
        # [0,0,1] 的总体标准差 sqrt(2/9) ≈ 0.4714:≥0.35 弃权、<0.5 不弃权。
        assert decide([0, 0, 1], metric=METRIC_STD, threshold=0.35).abstain is True
        assert decide([0, 0, 1], metric=METRIC_STD, threshold=0.5).abstain is False
        got = decide([0, 0, 1], metric=METRIC_STD, threshold=0.35)
        assert got.disagreement == pytest.approx(math.sqrt(2 / 9))

    def test_custom_callable_metric(self):
        assert decide([0.1, 0.9], metric=lambda v: 0.0).abstain is False
        assert decide([0.1, 0.9], metric=lambda v: 1.0).abstain is True


# ---------------------------------------------------------------------------
# decide:边界与确定性
# ---------------------------------------------------------------------------


class TestDecideBoundaries:
    def test_empty_input_never_abstains(self):
        d = decide([], threshold=0.35)
        assert d.abstain is False
        assert d.disagreement == 0.0
        assert "无成员分数" in d.reason

    def test_single_member_never_abstains_even_at_zero_threshold(self):
        # 单成员分歧恒 0,永不弃权——即使阈值取 0(d>=0 本会触发)也不弃权。
        for threshold in (0.0, 0.35, 1.0):
            d = decide([0.7], threshold=threshold)
            assert d.abstain is False
            assert d.disagreement == 0.0
            assert "永不弃权" in d.reason
        assert decide([0.7], metric=METRIC_STD).disagreement == 0.0

    def test_deterministic_same_input_same_output(self):
        for _ in range(3):
            assert decide([0.9, 0.1]) == decide([0.9, 0.1])
            assert decide([0, 0, 1], metric=METRIC_STD) == decide(
                [0, 0, 1], metric=METRIC_STD
            )


# ---------------------------------------------------------------------------
# decide:防御式校验
# ---------------------------------------------------------------------------


class TestDecideValidation:
    @pytest.mark.parametrize(
        "scores",
        [
            [float("nan"), 0.2],
            [0.1, float("inf")],
            [True, 0.2],  # bool 不应被当作分数
            ["0.9", 0.1],
            "abc",
            0.5,  # 非可迭代 / 非映射
        ],
    )
    def test_bad_scores_raise_chinese(self, scores):
        with pytest.raises(ValueError, match="成员分数"):
            decide(scores)

    def test_bad_mapping_value_raises(self):
        with pytest.raises(ValueError, match="成员分数"):
            decide({"stub": 0.1, "nudenet": float("nan")})

    @pytest.mark.parametrize(
        "threshold", [-0.1, float("nan"), float("inf"), True, "0.35"]
    )
    def test_bad_threshold_raises_chinese(self, threshold):
        with pytest.raises(ValueError, match="弃权阈值"):
            decide([0.1, 0.2], threshold=threshold)

    def test_unknown_metric_raises_chinese(self):
        with pytest.raises(ValueError, match="分歧度量"):
            decide([0.1, 0.2], metric="mad")

    def test_callable_metric_nan_output_raises(self):
        with pytest.raises(ValueError, match="分歧度量"):
            decide([0.1, 0.2], metric=lambda v: float("nan"))


# ---------------------------------------------------------------------------
# batch_decide:保序 / 一致性 / 确定性
# ---------------------------------------------------------------------------


class TestBatchDecide:
    def test_order_preserved(self):
        out = batch_decide([[0.9, 0.1], [0.1, 0.12], [0.4, 0.5]])
        assert [d.abstain for d in out] == [True, False, False]

    def test_equals_per_item_decide(self):
        xs = [[0.9, 0.1], [0.1, 0.12], [0.0, 0.34], [0.6, 0.2]]
        assert batch_decide(xs) == [decide(x) for x in xs]

    def test_mixed_list_and_mapping_inputs(self):
        out = batch_decide([{"a": 0.9, "b": 0.1}, [0.2, 0.25]])
        assert out == [decide({"a": 0.9, "b": 0.1}), decide([0.2, 0.25])]
        assert [d.abstain for d in out] == [True, False]

    def test_threshold_and_metric_forwarded(self):
        # 极差 0.4 / 0.8 均低于宽松阈值 0.9 → 全不弃权(阈值确实被转发)。
        xs = [[0.8, 0.4], [0.9, 0.1]]
        assert [d.abstain for d in batch_decide(xs, threshold=0.9)] == [False, False]
        std_out = batch_decide(xs, metric=METRIC_STD, threshold=0.5)
        assert std_out == [
            decide(x, metric=METRIC_STD, threshold=0.5) for x in xs
        ]

    def test_empty_input_returns_empty_list(self):
        assert batch_decide([]) == []

    def test_deterministic_and_pure(self):
        xs = [[0.9, 0.1], [0.1, 0.12], [0.45, 0.8]]
        snapshot = [list(x) for x in xs]
        first = batch_decide(xs)
        for _ in range(3):
            assert batch_decide(xs) == first
        assert xs == snapshot  # 纯函数:不改输入


# ---------------------------------------------------------------------------
# sweep:risk-coverage 曲线(手算对照 / 端点 / 单调性)
# ---------------------------------------------------------------------------


class TestSweepCurve:
    def test_classic_three_point_curve_hand_computed(self):
        # 手算(见模块顶部基准):阈 0 全覆盖、误判 D/4=0.25;阈 0.65 只留
        # A/B(分歧≤0.35)覆盖率 2/4、零误判;阈 ∞ 零覆盖、风险按 0.0 报。
        points = sweep_disagreement_threshold(
            CAL, CAL_LABELS, thresholds=[0.0, 0.65, float("inf")]
        )
        assert points[0] == (0.0, pytest.approx(1.0), pytest.approx(0.25))
        assert points[1] == (0.65, pytest.approx(0.5), pytest.approx(0.0))
        assert points[2] == (float("inf"), pytest.approx(0.0), pytest.approx(0.0))

    def test_endpoints_zero_full_coverage_infinite_zero_coverage(self):
        points = dict(
            (t, (c, r))
            for t, c, r in sweep_disagreement_threshold(
                CAL, CAL_LABELS, thresholds=[0.0, float("inf"), 1.5]
            )
        )
        assert points[0.0][0] == pytest.approx(1.0)  # 阈 0 → 全覆盖
        assert points[float("inf")][0] == pytest.approx(0.0)  # 阈 ∞ → 零覆盖
        assert points[float("inf")][1] == pytest.approx(0.0)  # 零覆盖风险按 0
        assert points[1.5][0] == pytest.approx(0.0)  # 大有限阈值同样全弃权

    def test_coverage_monotone_non_increasing_by_construction(self):
        grid = [0.0, 0.25, 0.5, 0.65, 0.75, 1.0, float("inf")]
        points = sweep_disagreement_threshold(CAL, CAL_LABELS, thresholds=grid)
        coverages = [c for _t, c, _r in points]
        assert all(
            coverages[i] >= coverages[i + 1] for i in range(len(coverages) - 1)
        ), f"覆盖率随阈值升序非单调不增:{coverages}"
        # 手算逐点对照(允许平台期:0.5/0.5/0.5 与 0.0/0.0)。
        assert coverages == pytest.approx([1.0, 0.75, 0.5, 0.5, 0.5, 0.0, 0.0])

    def test_full_grid_hand_computed_risk_values(self):
        points = sweep_disagreement_threshold(
            CAL, CAL_LABELS, thresholds=[0.0, 0.25, 0.5]
        )
        # t=0.25:保留 {A,B,D},唯一误判 D 仍在 → 风险 1/3(弃权去掉的是
        # 本就判对的 C,风险不必然单调降——只对覆盖率断言单调)。
        assert points[0] == (0.0, pytest.approx(1.0), pytest.approx(0.25))
        assert points[1] == (0.25, pytest.approx(0.75), pytest.approx(1 / 3))
        assert points[2] == (0.5, pytest.approx(0.5), pytest.approx(0.0))

    def test_unsorted_grid_returned_ascending(self):
        points = sweep_disagreement_threshold(
            CAL, CAL_LABELS, thresholds=[float("inf"), 0.65, 0.0]
        )
        assert [t for t, _c, _r in points] == [0.0, 0.65, float("inf")]

    def test_binarize_threshold_configurable(self):
        # 二值化降到 0.4:D 的均值 0.45 改判 1 → 全覆盖点上误判清零。
        points = sweep_disagreement_threshold(
            CAL, CAL_LABELS, thresholds=[0.0], binarize=0.4
        )
        assert points[0] == (0.0, pytest.approx(1.0), pytest.approx(0.0))

    def test_bool_labels_accepted(self):
        got = sweep_disagreement_threshold(
            [[0.9, 0.95], [0.1, 0.12]], [True, False], thresholds=[0.0]
        )
        assert got == [(0.0, pytest.approx(1.0), pytest.approx(0.0))]

    def test_mapping_input_curve_equals_list_input(self):
        kwargs = dict(thresholds=[0.0, 0.65, float("inf")])
        by_map = sweep_disagreement_threshold(
            [{"a": 0.9, "b": 0.1}], [1], **kwargs
        )
        by_list = sweep_disagreement_threshold([[0.9, 0.1]], [1], **kwargs)
        assert by_map == by_list

    def test_single_member_kept_until_threshold_above_one(self):
        # 单成员分歧恒 0:阈 ≤ 1(容忍上限 ≥ 0)时始终自动判定,永不进弃权集。
        points = sweep_disagreement_threshold(
            [[0.7]], [1], thresholds=[0.0, 1.0, float("inf")]
        )
        assert points[0] == (0.0, pytest.approx(1.0), pytest.approx(0.0))
        assert points[1] == (1.0, pytest.approx(1.0), pytest.approx(0.0))
        assert points[2] == (float("inf"), pytest.approx(0.0), pytest.approx(0.0))

    def test_default_grid_covers_endpoints_and_monotone(self):
        points = sweep_disagreement_threshold(CAL, CAL_LABELS)
        assert points[0][0] == 0.0 and points[0][1] == pytest.approx(1.0)
        assert points[-1][0] == float("inf") and points[-1][1] == pytest.approx(0.0)
        coverages = [c for _t, c, _r in points]
        assert all(
            coverages[i] >= coverages[i + 1] for i in range(len(coverages) - 1)
        )

    def test_deterministic_same_input_same_output(self):
        first = sweep_disagreement_threshold(CAL, CAL_LABELS)
        for _ in range(3):
            assert sweep_disagreement_threshold(CAL, CAL_LABELS) == first


# ---------------------------------------------------------------------------
# sweep:防御式校验
# ---------------------------------------------------------------------------


class TestSweepValidation:
    def test_length_mismatch_raises_chinese(self):
        with pytest.raises(ValueError, match="长度不一致"):
            sweep_disagreement_threshold([[0.1, 0.2]], [0, 1])

    @pytest.mark.parametrize("label", [2, -1, 0.5, "1", None])
    def test_bad_label_raises_chinese(self, label):
        with pytest.raises(ValueError, match="人工真值"):
            sweep_disagreement_threshold([[0.1, 0.2]], [label])

    def test_empty_member_group_rejected(self):
        # 校准集样本无成员分即无预测,宁拒不猜。
        with pytest.raises(ValueError, match="为空"):
            sweep_disagreement_threshold([[]], [0])

    def test_nan_score_raises(self):
        with pytest.raises(ValueError, match="成员分数"):
            sweep_disagreement_threshold([[float("nan"), 0.2]], [1])

    @pytest.mark.parametrize("bad", [-0.1, float("nan"), -float("inf"), True, "0.5"])
    def test_bad_grid_item_raises_chinese(self, bad):
        with pytest.raises(ValueError, match="阈值网格"):
            sweep_disagreement_threshold(
                [[0.1, 0.2]], [1], thresholds=[0.0, bad]
            )

    @pytest.mark.parametrize("bad", [float("nan"), 1.5, -0.1, True])
    def test_bad_binarize_raises_chinese(self, bad):
        with pytest.raises(ValueError, match="二值化"):
            sweep_disagreement_threshold([[0.1, 0.2]], [1], binarize=bad)

    def test_empty_inputs_return_empty_curve(self):
        assert sweep_disagreement_threshold([], []) == []
        assert sweep_disagreement_threshold([[0.1, 0.2]], [1], thresholds=[]) == []


# ---------------------------------------------------------------------------
# to_triage_hint:triage 集成提示(不改 triage.py)
# ---------------------------------------------------------------------------


class TestTriageHint:
    def test_keys_and_values_for_abstain(self):
        d = decide([0.9, 0.1])
        hint = to_triage_hint(d, p_nsfw=0.42)
        assert set(hint) == {
            "source",
            "abstain",
            "disagreement",
            "threshold",
            "p_nsfw",
            "priority_weight",
            "reason",
        }
        assert hint["source"] == "decision.abstain"
        assert hint["abstain"] is True
        assert hint["disagreement"] == pytest.approx(0.8)
        assert hint["threshold"] == DEFAULT_ABSTAIN_THRESHOLD
        assert hint["p_nsfw"] == pytest.approx(0.42)
        # 弃权样本:priority_weight = 分歧度本身(越大越先复核)。
        assert hint["priority_weight"] == pytest.approx(0.8)
        assert hint["reason"] == d.reason

    def test_non_abstain_weight_zero_and_p_nsfw_none(self):
        hint = to_triage_hint(decide([0.1, 0.12]))
        assert hint["abstain"] is False
        assert hint["priority_weight"] == 0.0
        assert hint["p_nsfw"] is None

    def test_single_member_hint_never_weighted(self):
        # 单成员永不弃权 → 恒不进加权复核路径。
        hint = to_triage_hint(decide([0.7], threshold=0.0))
        assert hint["abstain"] is False and hint["priority_weight"] == 0.0

    @pytest.mark.parametrize("bad", [1.5, -0.1, float("nan"), True])
    def test_bad_p_nsfw_raises_chinese(self, bad):
        with pytest.raises(ValueError, match="p_nsfw"):
            to_triage_hint(decide([0.9, 0.1]), p_nsfw=bad)

    def test_docstring_documents_wiring(self):
        # 红线:接线方式必须在 docstring 说明(实际接线属后续任务)。
        doc = to_triage_hint.__doc__ or ""
        assert "triage" in doc and "p_nsfw" in doc and "priority" in doc
