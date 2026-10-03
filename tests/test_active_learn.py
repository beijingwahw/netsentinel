"""A44 netsentinel.intel.active_learn 主动学习反馈环测试。

纯离线、零网络、零真实 VLM 调用。覆盖:
- record / snapshot / count 基础行为与参数校验(中文 ValueError);
- jsonl 持久化往返(tmp_path)、脏行跳过、防御性拷贝;
- 样本 <20 返回空建议(契约"返回空并说明",行为由本文件锁定);
- 三条建议规则各自触发 / 不触发(构造 approve-reject 分布);
- 建议只含声明字段 {"param","current","suggested","reason","n"} 且方向合理
  (上调 ≥ current、下调有 0.3 下限、nsfw_threshold 建议值=驳回段中位数);
- 建议绝不修改 cfg(只产出建议红线);
- rank_for_vlm:不确定度 |p-0.5| 升序、只取 ensemble、预算截断、空输入。
"""
from __future__ import annotations

import json
import statistics
from dataclasses import replace

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, ImageScore, Verdict
from netsentinel.intel.active_learn import (
    MIN_SAMPLES,
    REVIEW_FLOOR,
    ReviewFeedback,
    rank_for_vlm,
)

# ---------------------------------------------------------------------------
# 造数工具
# ---------------------------------------------------------------------------


def _fill(fb: ReviewFeedback, rows: list[tuple[str, str, float, int]]) -> None:
    """按 (verdict, action, agg, nsw_count) 批量记录。"""
    for verdict, action, agg, nsw in rows:
        fb.record(verdict, action, agg, nsw)


def _rows(rejected_nsfw: list[tuple[float, int]], suspect: list[str]) -> list[tuple]:
    """拼装复核样本:rejected_nsfw = (agg, nsw_count) 的 NSFW 驳回;
    suspect = ["approve"|"reject"] 的疑似条目;不足 MIN_SAMPLES 用 clean 补齐。"""
    rows = [("nsfw", "reject", agg, cnt) for agg, cnt in rejected_nsfw]
    rows += [("suspect", act, 0.55, 2) for act in suspect]
    while len(rows) < MIN_SAMPLES:
        rows.append(("clean", "approve", 0.05, 0))
    return rows


def _score(prob: float, model: str = "ensemble", name: str = "img.jpg") -> ImageScore:
    ev = ImageEvidence(
        path=f"data/evidence/{name}", url=f"https://x.example/{name}", source_page="https://x.example/"
    )
    return ImageScore(image=ev, model=model, nsfw_prob=prob)


# 组合场景:20 条样本同时满足三条规则。
# - 6 条被驳回的 NSFW:agg 中位数 0.75 < 0.9(规则①),nsw_count 中位数 2 < 3(规则②);
# - 14 条疑似:11 条确认(11/14 ≈ 0.786 > 0.6,规则③)。
_COMBINED_REJECTED = [(0.70, 1), (0.72, 2), (0.74, 2), (0.76, 2), (0.78, 3), (0.80, 3)]
_COMBINED_SUSPECT = ["approve"] * 11 + ["reject"] * 3


# ---------------------------------------------------------------------------
# record / snapshot / count
# ---------------------------------------------------------------------------


def test_initial_state_is_empty() -> None:
    """新实例:count=0,snapshot 为空列表。"""
    fb = ReviewFeedback()
    assert fb.count() == 0
    assert fb.snapshot() == []
    assert fb.jsonl_path == ""


def test_record_builds_snapshot_and_count() -> None:
    """record 三条:count=3;snapshot 字段恰为四个声明键,值归一化保留。"""
    fb = ReviewFeedback()
    fb.record("nsfw", "reject", 0.75, 2)
    fb.record("suspect", "approve", 0.55, 1)
    fb.record("clean", "approve", 0.05, 0)
    assert fb.count() == 3
    snap = fb.snapshot()
    assert snap == [
        {"verdict": "nsfw", "action": "reject", "agg": 0.75, "nsw_count": 2},
        {"verdict": "suspect", "action": "approve", "agg": 0.55, "nsw_count": 1},
        {"verdict": "clean", "action": "approve", "agg": 0.05, "nsw_count": 0},
    ]


def test_record_accepts_verdict_enum_and_normalizes() -> None:
    """容忍 Verdict 枚举(取 .value);record 返回归一化条目副本。"""
    fb = ReviewFeedback()
    entry = fb.record(Verdict.NSFW, "Reject", 0.9, 3)
    assert entry == {"verdict": "nsfw", "action": "reject", "agg": 0.9, "nsw_count": 3}
    assert fb.snapshot()[0]["verdict"] == "nsfw"


def test_record_rounds_agg_to_4dp() -> None:
    """agg 统一保留 4 位小数(与 as_dict 精度一致)。"""
    fb = ReviewFeedback()
    fb.record("nsfw", "reject", 0.123456, 1)
    assert fb.snapshot()[0]["agg"] == 0.1235


@pytest.mark.parametrize("action", ["yes", "", "APPROVE?"])
def test_record_invalid_action_raises_cn(action: str) -> None:
    """非法 action → ValueError 中文,且不产生任何条目。"""
    fb = ReviewFeedback()
    with pytest.raises(ValueError, match="approve"):
        fb.record("nsfw", action, 0.9, 3)
    assert fb.count() == 0


@pytest.mark.parametrize("verdict", ["porno", "", "NSFW!"])
def test_record_invalid_verdict_raises_cn(verdict: str) -> None:
    """非法 verdict → ValueError 中文(列出合法取值)。"""
    fb = ReviewFeedback()
    with pytest.raises(ValueError, match="verdict"):
        fb.record(verdict, "reject", 0.9, 3)
    assert fb.count() == 0


def test_record_non_numeric_agg_raises() -> None:
    """agg 非数字 → ValueError 中文。"""
    fb = ReviewFeedback()
    with pytest.raises(ValueError, match="agg"):
        fb.record("nsfw", "reject", "高", 3)  # type: ignore[arg-type]


def test_snapshot_is_defensive_copy() -> None:
    """修改 snapshot 返回值不影响内部状态。"""
    fb = ReviewFeedback()
    fb.record("nsfw", "reject", 0.75, 2)
    snap = fb.snapshot()
    snap.clear()
    snap2 = fb.snapshot()
    snap2[0]["agg"] = 99.0
    assert fb.count() == 1
    assert fb.snapshot() == [{"verdict": "nsfw", "action": "reject", "agg": 0.75, "nsw_count": 2}]


# ---------------------------------------------------------------------------
# jsonl 持久化往返
# ---------------------------------------------------------------------------


def test_jsonl_roundtrip(tmp_path) -> None:
    """record 写 jsonl → 新实例读回:条数与内容一致。"""
    path = str(tmp_path / "feedback.jsonl")
    fb = ReviewFeedback(path)
    _fill(fb, [("nsfw", "reject", 0.75, 2), ("suspect", "approve", 0.55, 1)])
    reloaded = ReviewFeedback(path)
    assert reloaded.count() == 2
    assert reloaded.snapshot() == fb.snapshot()


def test_jsonl_accumulates_across_instances(tmp_path) -> None:
    """两个实例先后写同一文件 → 第三个实例读回全部 3 条(顺序保持)。"""
    path = str(tmp_path / "feedback.jsonl")
    ReviewFeedback(path).record("nsfw", "reject", 0.8, 2)
    ReviewFeedback(path).record("suspect", "approve", 0.6, 1)
    ReviewFeedback(path).record("clean", "approve", 0.1, 0)
    fb = ReviewFeedback(path)
    assert fb.count() == 3
    assert [e["verdict"] for e in fb.snapshot()] == ["nsfw", "suspect", "clean"]


def test_jsonl_skips_corrupt_lines(tmp_path) -> None:
    """空行 / 非 JSON / 缺字段行被跳过,只载入合法条目。"""
    path = tmp_path / "feedback.jsonl"
    valid = json.dumps(
        {"verdict": "nsfw", "action": "reject", "agg": 0.75, "nsw_count": 2},
        ensure_ascii=False,
    )
    path.write_text(
        "\n不是 json\n" + valid + "\n" + json.dumps({"verdict": "nsfw"}) + "\n",
        encoding="utf-8",
    )
    fb = ReviewFeedback(str(path))
    assert fb.count() == 1
    assert fb.snapshot()[0]["agg"] == 0.75


def test_default_instance_creates_no_file(tmp_path) -> None:
    """jsonl_path 为空:纯内存,不在磁盘产生任何文件。"""
    fb = ReviewFeedback()
    fb.record("nsfw", "reject", 0.75, 2)
    assert list(tmp_path.iterdir()) == []
    assert fb.count() == 1


def test_loaded_feedback_can_suggest(tmp_path) -> None:
    """从 jsonl 载入的样本同样能产出建议(反馈环跨批次闭环)。"""
    path = str(tmp_path / "feedback.jsonl")
    _fill(ReviewFeedback(path), _rows(_COMBINED_REJECTED, _COMBINED_SUSPECT))
    reloaded = ReviewFeedback(path)
    assert reloaded.count() == MIN_SAMPLES
    params = [s["param"] for s in reloaded.threshold_suggestions(Config())]
    assert set(params) == {"nsfw_threshold", "min_nsw_images", "review_threshold"}


# ---------------------------------------------------------------------------
# threshold_suggestions:样本不足
# ---------------------------------------------------------------------------


def test_min_samples_constant_is_20() -> None:
    """最少样本数常量锁定为 20(契约 §3 A44)。"""
    assert MIN_SAMPLES == 20


def test_insufficient_samples_return_empty() -> None:
    """19 条(哪怕分布极端)→ 空建议:小样本不给出误导性调整。"""
    fb = ReviewFeedback()
    _fill(fb, [("nsfw", "reject", 0.70, 1)] * (MIN_SAMPLES - 1))  # 不经 _rows 补齐
    assert fb.count() == MIN_SAMPLES - 1
    assert fb.threshold_suggestions(Config()) == []


def test_zero_samples_return_empty() -> None:
    """零样本 → 空建议。"""
    assert ReviewFeedback().threshold_suggestions(Config()) == []


# ---------------------------------------------------------------------------
# 规则①:被驳回 NSFW 的 agg 中位数 < nsfw_threshold
# ---------------------------------------------------------------------------


def test_rule1_fires_suggests_median() -> None:
    """驳回 NSFW 的 agg 中位数 0.75 < 0.9 → 建议 nsfw_threshold=0.75。"""
    fb = ReviewFeedback()
    _fill(fb, _rows(_COMBINED_REJECTED, []))  # 不放 suspect,规则③不参与
    (sug,) = [s for s in fb.threshold_suggestions(Config()) if s["param"] == "nsfw_threshold"]
    assert sug["current"] == pytest.approx(0.9)
    assert sug["suggested"] == pytest.approx(
        statistics.median([agg for agg, _ in _COMBINED_REJECTED])
    )
    assert sug["suggested"] == pytest.approx(0.75)
    assert sug["n"] == len(_COMBINED_REJECTED)
    assert "低分 NSFW 被人工驳回集中" in sug["reason"]


def test_rule1_not_firing_when_median_above_threshold() -> None:
    """驳回 NSFW 的 agg 中位数 ≥ 阈值(刚过线的低分段不存在)→ 无该建议。"""
    fb = ReviewFeedback()
    _fill(fb, _rows([(0.92, 4), (0.93, 4), (0.94, 4)], []))
    assert fb.count() == MIN_SAMPLES
    params = [s["param"] for s in fb.threshold_suggestions(Config())]
    assert "nsfw_threshold" not in params


def test_rule1_ignores_approved_nsfw() -> None:
    """被人工确认(approve)的 NSFW 低分样本不参与规则①。"""
    fb = ReviewFeedback()
    rows = [("nsfw", "approve", agg, cnt) for agg, cnt in _COMBINED_REJECTED]
    while len(rows) < MIN_SAMPLES:
        rows.append(("clean", "approve", 0.05, 0))
    _fill(fb, rows)
    assert fb.threshold_suggestions(Config()) == []


# ---------------------------------------------------------------------------
# 规则②:被驳回 NSFW 的 nsw_count 中位数 < min_nsw_images
# ---------------------------------------------------------------------------


def test_rule2_fires_suggests_plus_one() -> None:
    """驳回 NSFW 的 nsw_count 中位数 2 < 3 → 建议 min_nsw_images=4(上调)。"""
    fb = ReviewFeedback()
    _fill(fb, _rows(_COMBINED_REJECTED, []))
    (sug,) = [s for s in fb.threshold_suggestions(Config()) if s["param"] == "min_nsw_images"]
    assert sug["current"] == 3
    assert sug["suggested"] == 4  # 上调 1
    assert sug["suggested"] >= sug["current"]
    assert sug["n"] == len(_COMBINED_REJECTED)


def test_rule2_not_firing_when_median_meets_minimum() -> None:
    """nsw_count 中位数 ≥ min_nsw_images(少量达标图问题不存在)→ 无该建议。"""
    fb = ReviewFeedback()
    _fill(fb, _rows([(0.92, 3), (0.93, 4), (0.94, 5)], []))
    params = [s["param"] for s in fb.threshold_suggestions(Config())]
    assert "min_nsw_images" not in params


def test_rule2_respects_custom_min_nsw_images() -> None:
    """自定义 cfg.min_nsw_images=6:中位数 4 < 6 触发,建议 7。"""
    fb = ReviewFeedback()
    _fill(fb, _rows([(0.95, 3), (0.96, 4), (0.97, 5)], []))
    cfg = replace(Config(), min_nsw_images=6)
    (sug,) = [s for s in fb.threshold_suggestions(cfg) if s["param"] == "min_nsw_images"]
    assert sug == {
        "param": "min_nsw_images",
        "current": 6,
        "suggested": 7,
        "reason": sug["reason"],
        "n": 3,
    }


# ---------------------------------------------------------------------------
# 规则③:疑似条目人工确认占比 > 0.6 → review_threshold 下调 0.05(≥0.3)
# ---------------------------------------------------------------------------


def test_rule3_fires_suggests_step_down() -> None:
    """确认占比 15/20 = 0.75 > 0.6 → 建议 review_threshold 0.5 → 0.45。"""
    fb = ReviewFeedback()
    _fill(fb, _rows([], ["approve"] * 15 + ["reject"] * 5))
    (sug,) = fb.threshold_suggestions(Config())
    assert sug["param"] == "review_threshold"
    assert sug["current"] == pytest.approx(0.5)
    assert sug["suggested"] == pytest.approx(0.45)  # 下调 0.05
    assert sug["suggested"] < sug["current"]
    assert sug["suggested"] >= REVIEW_FLOOR
    assert sug["n"] == 20


def test_rule3_boundary_ratio_06_does_not_fire() -> None:
    """确认占比恰为 0.6(严格大于才触发)→ 无建议。"""
    fb = ReviewFeedback()
    _fill(fb, _rows([], ["approve"] * 6 + ["reject"] * 4))
    assert fb.threshold_suggestions(Config()) == []


def test_rule3_clamped_at_floor() -> None:
    """review_threshold=0.32 → 建议 0.3(下限钳制,不再降)。"""
    fb = ReviewFeedback()
    _fill(fb, _rows([], ["approve"] * 15 + ["reject"] * 5))
    cfg = replace(Config(), review_threshold=0.32)
    (sug,) = fb.threshold_suggestions(cfg)
    assert sug["suggested"] == pytest.approx(0.3)
    assert sug["suggested"] >= REVIEW_FLOOR


def test_rule3_no_op_at_floor_not_emitted() -> None:
    """review_threshold 已是 0.3:下调被下限钳成原值(无实际调整)→ 不出建议。"""
    fb = ReviewFeedback()
    _fill(fb, _rows([], ["approve"] * 15 + ["reject"] * 5))
    cfg = replace(Config(), review_threshold=0.3)
    assert fb.threshold_suggestions(cfg) == []


# ---------------------------------------------------------------------------
# 组合场景:字段声明与方向性断言
# ---------------------------------------------------------------------------


def test_combined_scenario_three_suggestions() -> None:
    """20 条组合分布 → 恰好三条建议,参数与方向全部锁定。"""
    fb = ReviewFeedback()
    _fill(fb, _rows(_COMBINED_REJECTED, _COMBINED_SUSPECT))
    assert fb.count() == MIN_SAMPLES
    sugs = fb.threshold_suggestions(Config())
    assert [s["param"] for s in sugs] == [
        "nsfw_threshold",
        "min_nsw_images",
        "review_threshold",
    ]
    by_param = {s["param"]: s for s in sugs}
    # nsfw_threshold:建议值 = 被驳回 NSFW 的 agg 中位数(向驳回集中段对齐)
    assert by_param["nsfw_threshold"]["suggested"] == pytest.approx(0.75)
    # min_nsw_images:上调 1
    assert by_param["min_nsw_images"]["suggested"] == 4
    # review_threshold:下调 0.05 且不低于 0.3
    assert by_param["review_threshold"]["suggested"] == pytest.approx(0.45)
    assert by_param["review_threshold"]["suggested"] >= REVIEW_FLOOR


def test_suggestions_contain_only_declared_fields() -> None:
    """每条建议只含声明字段 {"param","current","suggested","reason","n"}。"""
    fb = ReviewFeedback()
    _fill(fb, _rows(_COMBINED_REJECTED, _COMBINED_SUSPECT))
    for sug in fb.threshold_suggestions(Config()):
        assert set(sug) == {"param", "current", "suggested", "reason", "n"}
        assert isinstance(sug["reason"], str) and sug["reason"]
        assert sug["n"] > 0
        # 红线:文案必须写明"仅为建议",不自动改生产阈值
        assert "仅为建议" in sug["reason"]


def test_clean_data_yields_no_suggestions() -> None:
    """干净数据(无误报、确认率不超线)→ 空建议列表。"""
    fb = ReviewFeedback()
    rows = [("nsfw", "approve", 0.95, 5)] * 5
    rows += [("suspect", "approve", 0.55, 2)] * 5 + [("suspect", "reject", 0.52, 1)] * 5
    rows += [("clean", "approve", 0.05, 0)] * 5
    _fill(fb, rows)
    assert fb.threshold_suggestions(Config()) == []


def test_suggestions_do_not_mutate_cfg() -> None:
    """红线:产建议绝不修改 cfg(只读分析,重复调用结果一致)。"""
    fb = ReviewFeedback()
    _fill(fb, _rows(_COMBINED_REJECTED, _COMBINED_SUSPECT))
    cfg = Config()
    before = (cfg.nsfw_threshold, cfg.min_nsw_images, cfg.review_threshold)
    first = fb.threshold_suggestions(cfg)
    second = fb.threshold_suggestions(cfg)
    assert (cfg.nsfw_threshold, cfg.min_nsw_images, cfg.review_threshold) == before
    assert first == second
    assert first is not second  # 每次返回新列表,不共享内部可变状态


# ---------------------------------------------------------------------------
# rank_for_vlm:不确定度排序与预算截断
# ---------------------------------------------------------------------------


def test_rank_empty_input_returns_empty() -> None:
    """空输入 → 空列表。"""
    assert rank_for_vlm([], budget=3) == []


@pytest.mark.parametrize("budget", [0, -1, -100])
def test_rank_non_positive_budget_returns_empty(budget: int) -> None:
    """预算 ≤0 → 空列表(费用保护)。"""
    scores = [_score(0.5), _score(0.9)]
    assert rank_for_vlm(scores, budget=budget) == []


def test_rank_orders_by_uncertainty_ascending() -> None:
    """|p-0.5| 升序:最接近 0.5 的排最前(边缘优先)。"""
    scores = [_score(0.9), _score(0.5), _score(0.42), _score(0.12)]
    ranked = rank_for_vlm(scores, budget=10)
    assert [s.nsfw_prob for s in ranked] == [0.5, 0.42, 0.12, 0.9]
    uncertainties = [abs(s.nsfw_prob - 0.5) for s in ranked]
    assert uncertainties == sorted(uncertainties)


def test_rank_truncates_to_budget() -> None:
    """预算 2:只保留不确定度最小的前 2 张。"""
    scores = [_score(0.9), _score(0.5), _score(0.42), _score(0.12)]
    ranked = rank_for_vlm(scores, budget=2)
    assert [s.nsfw_prob for s in ranked] == [0.5, 0.42]


def test_rank_uses_only_ensemble_scores() -> None:
    """只取 model="ensemble":stub / vlm-arbiter 条目即使更边缘也不参与。"""
    scores = [
        _score(0.51, model="stub", name="stub.jpg"),          # 不确定度 0.01,若参与将排第一
        _score(0.49, model="vlm-arbiter", name="arb.jpg"),    # 同上
        _score(0.5, model="ensemble", name="e1.jpg"),
        _score(0.8, model="ensemble", name="e2.jpg"),
    ]
    ranked = rank_for_vlm(scores, budget=10)
    assert [(s.model, s.nsfw_prob) for s in ranked] == [
        ("ensemble", 0.5),
        ("ensemble", 0.8),
    ]


def test_rank_no_ensemble_returns_empty() -> None:
    """列表中没有 ensemble 条目 → 空列表(调用方应传已并入集成分的列表)。"""
    scores = [_score(0.5, model="stub"), _score(0.7, model="clip")]
    assert rank_for_vlm(scores, budget=3) == []


def test_rank_ties_keep_input_order() -> None:
    """不确定度相同(0.25 与 0.75 都是 0.25,二进制下严格相等)→ 稳定排序保持输入顺序。"""
    scores = [_score(0.25, name="a.jpg"), _score(0.75, name="b.jpg"), _score(0.5, name="c.jpg")]
    ranked = rank_for_vlm(scores, budget=3)
    assert [s.image.path.split("/")[-1] for s in ranked] == ["c.jpg", "a.jpg", "b.jpg"]


def test_rank_returns_same_objects_and_does_not_mutate_input() -> None:
    """返回元素是入参对象引用;入参列表顺序不被重排。"""
    scores = [_score(0.9, name="a.jpg"), _score(0.5, name="b.jpg"), _score(0.4, name="c.jpg")]
    ranked = rank_for_vlm(scores, budget=3)
    assert ranked[0] is scores[1]
    assert ranked[1] is scores[2]
    assert ranked[2] is scores[0]
    assert [s.image.path.split("/")[-1] for s in scores] == ["a.jpg", "b.jpg", "c.jpg"]


# ---------------------------------------------------------------------------
# V5 升级:threshold_suggestions 统计单遍完成 + telemetry 计数
# (learn.suggestions 带建议数量;样本不足的空建议不计数)
# ---------------------------------------------------------------------------


def test_v5_threshold_suggestions_single_pass_over_entries() -> None:
    """性能锁定:三条规则共用一次遍历——对 _entries 的迭代恰为 1 次。

    旧实现对 entries 做多次列表扫描(rejected_nsfw 推导、suspect 推导、
    approved 再推导);V5 改为单遍收集全部统计量。
    """
    fb = ReviewFeedback()
    _fill(fb, _rows(_COMBINED_REJECTED, _COMBINED_SUSPECT))

    iter_calls = {"n": 0}

    class _CountingList(list):
        def __iter__(self):  # type: ignore[override]
            iter_calls["n"] += 1
            return super().__iter__()

    fb._entries = _CountingList(fb.snapshot())

    sugs = fb.threshold_suggestions(Config())
    assert [s["param"] for s in sugs] == [
        "nsfw_threshold",
        "min_nsw_images",
        "review_threshold",
    ]  # 单遍优化不改变建议内容与顺序
    assert iter_calls["n"] == 1


def test_v5_telemetry_counts_suggestions() -> None:
    """可观测:组合分布产出 3 条建议 → learn.suggestions 计 3。"""
    telemetry.reset()
    fb = ReviewFeedback()
    _fill(fb, _rows(_COMBINED_REJECTED, _COMBINED_SUSPECT))
    fb.threshold_suggestions(Config())
    assert telemetry.snapshot()["counters"]["learn.suggestions"] == 3

    # 再次调用累计(计数器语义,建议数量逐次累加)。
    fb.threshold_suggestions(Config())
    assert telemetry.snapshot()["counters"]["learn.suggestions"] == 6


def test_v5_telemetry_single_rule_counts_one() -> None:
    """只触发规则③ → learn.suggestions 计 1(数量与产出的建议条数一致)。"""
    telemetry.reset()
    fb = ReviewFeedback()
    _fill(fb, _rows([], ["approve"] * 15 + ["reject"] * 5))
    sugs = fb.threshold_suggestions(Config())
    assert len(sugs) == 1
    assert telemetry.snapshot()["counters"]["learn.suggestions"] == 1


def test_v5_telemetry_no_counter_for_insufficient_samples() -> None:
    """样本不足的空建议不产生 learn.suggestions 计数(快照保持干净)。"""
    telemetry.reset()
    fb = ReviewFeedback()
    _fill(fb, [("nsfw", "reject", 0.70, 1)] * (MIN_SAMPLES - 1))
    assert fb.threshold_suggestions(Config()) == []
    assert "learn.suggestions" not in telemetry.snapshot()["counters"]
