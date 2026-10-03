"""tests/test_ensemble.py —— A08 ensemble_scores 单元测试。

离线:仅构造 ImageEvidence / ImageScore,不需要真实图片文件,也不访问网络。

V5 新增(test_v5_* 前缀):ensemble.images 遥测计数、单遍分组合并后的语义
回归锁定(分组/首现顺序/首条 image 引用/缺失成员/重复后值覆盖)。
"""
from __future__ import annotations

import pytest

from netsentinel import telemetry
from netsentinel.contracts import ImageEvidence, ImageScore
from netsentinel.vision.ensemble import ensemble_scores


def make_evidence(path: str) -> ImageEvidence:
    """构造无需真实落盘文件的图片证据。"""
    return ImageEvidence(
        path=path,
        url=f"https://example.invalid/{path}",
        source_page="https://example.invalid/index.html",
        sha256="0" * 64,
        width=800,
        height=600,
    )


def make_score(path: str, model: str, prob: float) -> ImageScore:
    """快捷构造一条成员评分。"""
    return ImageScore(image=make_evidence(path), model=model, nsfw_prob=prob)


# ---------------------------------------------------------------------------
# 分组与加权平均
# ---------------------------------------------------------------------------

def test_two_models_two_images_weighted_average() -> None:
    """两模型两图:分组正确、权重 2:1 的加权平均数值精确。"""
    img_a = make_evidence("a.jpg")
    scores = [
        ImageScore(image=img_a, model="m1", nsfw_prob=0.9),
        ImageScore(image=make_evidence("b.jpg"), model="m1", nsfw_prob=0.5),
        ImageScore(image=img_a, model="m2", nsfw_prob=0.7),
        ImageScore(image=make_evidence("b.jpg"), model="m2", nsfw_prob=0.3),
    ]
    result = ensemble_scores(scores, weights={"m1": 2.0, "m2": 1.0})

    assert len(result) == 2
    assert [r.image.path for r in result] == ["a.jpg", "b.jpg"]
    assert all(r.model == "ensemble" for r in result)
    # (0.9*2 + 0.7*1) / 3 与 (0.5*2 + 0.3*1) / 3
    assert result[0].nsfw_prob == pytest.approx((0.9 * 2 + 0.7) / 3)
    assert result[1].nsfw_prob == pytest.approx((0.5 * 2 + 0.3) / 3)
    assert result[0].scores["members"] == {"m1": 0.9, "m2": 0.7}
    assert result[1].scores["members"] == {"m1": 0.5, "m2": 0.3}
    assert "missing" not in result[0].scores
    assert "missing" not in result[1].scores


def test_image_reference_is_first_entry_for_that_path() -> None:
    """集成条目的 image 必须是该图第一条输入的 image 引用(同一对象)。"""
    img_a = make_evidence("a.jpg")
    scores = [
        ImageScore(image=img_a, model="m1", nsfw_prob=0.9),
        ImageScore(image=make_evidence("a.jpg"), model="m2", nsfw_prob=0.7),
    ]
    result = ensemble_scores(scores)
    assert result[0].image is img_a


# ---------------------------------------------------------------------------
# 缺失成员
# ---------------------------------------------------------------------------

def test_missing_member_averages_existing_and_records_missing() -> None:
    """某模型没给某图打分:只用现有成员平均,并记录 missing。"""
    scores = [
        make_score("a.jpg", "m1", 0.9),
        make_score("a.jpg", "m2", 0.7),
        make_score("b.jpg", "m1", 0.6),
    ]
    result = ensemble_scores(scores)  # 等权

    assert len(result) == 2
    full, partial = result
    assert full.image.path == "a.jpg"
    assert "missing" not in full.scores
    assert full.nsfw_prob == pytest.approx(0.8)

    assert partial.image.path == "b.jpg"
    assert partial.scores["members"] == {"m1": 0.6}
    assert partial.scores["missing"] == ["m2"]
    assert partial.nsfw_prob == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# 权重兜底
# ---------------------------------------------------------------------------

def test_all_zero_weights_fall_back_to_equal_weights() -> None:
    """权重全 0:按等权兜底 (0.9 + 0.7) / 2 = 0.8。"""
    scores = [
        make_score("a.jpg", "m1", 0.9),
        make_score("a.jpg", "m2", 0.7),
    ]
    result = ensemble_scores(scores, weights={"m1": 0.0, "m2": 0.0})
    assert len(result) == 1
    assert result[0].nsfw_prob == pytest.approx((0.9 + 0.7) / 2)


def test_unknown_model_in_weights_gets_default_one() -> None:
    """weights 里没提到的模型按 1.0 计。"""
    scores = [
        make_score("a.jpg", "m1", 0.9),
        make_score("a.jpg", "m2", 0.7),
    ]
    result = ensemble_scores(scores, weights={"other": 5.0})
    assert result[0].nsfw_prob == pytest.approx((0.9 + 0.7) / 2)


# ---------------------------------------------------------------------------
# 边界:空输入 / 单成员透传
# ---------------------------------------------------------------------------

def test_empty_input_returns_empty_list() -> None:
    assert ensemble_scores([]) == []
    assert ensemble_scores([], weights={"m1": 2.0}) == []


def test_single_member_passthrough() -> None:
    """单图单成员:分值直接透传(含带权重的情形)。"""
    result = ensemble_scores([make_score("only.jpg", "stub", 0.42)])
    assert len(result) == 1
    assert result[0].model == "ensemble"
    assert result[0].nsfw_prob == pytest.approx(0.42)
    assert result[0].scores["members"] == {"stub": 0.42}
    assert "missing" not in result[0].scores

    weighted = ensemble_scores(
        [make_score("only.jpg", "stub", 0.42)], weights={"stub": 3.0}
    )
    assert weighted[0].nsfw_prob == pytest.approx(0.42)


# ---------------------------------------------------------------------------
# 顺序
# ---------------------------------------------------------------------------

def test_output_preserves_first_seen_order() -> None:
    """输出顺序 = 图片首现顺序,而非模型顺序。"""
    scores = [
        make_score("c.jpg", "m1", 0.1),
        make_score("a.jpg", "m1", 0.2),
        make_score("c.jpg", "m2", 0.3),
        make_score("b.jpg", "m1", 0.4),
    ]
    result = ensemble_scores(scores)
    assert [r.image.path for r in result] == ["c.jpg", "a.jpg", "b.jpg"]


# ---------------------------------------------------------------------------
# 数值截断(clamp)
# ---------------------------------------------------------------------------

def test_clamp_to_unit_interval() -> None:
    """非法输入 1.2 -> 1.0;负值 -> 0.0;两成员均值越界同样截断。"""
    high = ensemble_scores([make_score("hi.jpg", "m1", 1.2)])
    assert high[0].nsfw_prob == 1.0

    low = ensemble_scores([make_score("lo.jpg", "m1", -0.3)])
    assert low[0].nsfw_prob == 0.0

    mean_over = ensemble_scores(
        [make_score("x.jpg", "m1", 1.2), make_score("x.jpg", "m2", 1.1)]
    )
    assert mean_over[0].nsfw_prob == 1.0  # (1.2 + 1.1) / 2 = 1.15 -> 1.0


# ---------------------------------------------------------------------------
# 附加防御性行为
# ---------------------------------------------------------------------------

def test_duplicate_member_score_last_value_wins() -> None:
    """同一模型对同一图重复评分:后值覆盖前值。"""
    scores = [
        make_score("a.jpg", "m1", 0.9),
        make_score("a.jpg", "m1", 0.5),
        make_score("a.jpg", "m2", 0.7),
    ]
    result = ensemble_scores(scores)
    assert result[0].scores["members"] == {"m1": 0.5, "m2": 0.7}
    assert result[0].nsfw_prob == pytest.approx(0.6)


def test_ensemble_entries_in_input_are_ignored() -> None:
    """输入里已有的 ensemble 条目不参与聚合,避免重复计数。"""
    scores = [
        make_score("a.jpg", "m1", 0.9),
        make_score("a.jpg", "ensemble", 0.99),
    ]
    result = ensemble_scores(scores)
    assert len(result) == 1
    assert result[0].nsfw_prob == pytest.approx(0.9)
    assert result[0].scores["members"] == {"m1": 0.9}

    # 只有 ensemble 条目时输出为空。
    assert ensemble_scores([make_score("a.jpg", "ensemble", 0.9)]) == []


# ---------------------------------------------------------------------------
# V5:遥测计数(ensemble.images)
# ---------------------------------------------------------------------------

def test_v5_telemetry_images_counter_counts_outputs() -> None:
    """ensemble.images 按输出图片数累计(3 张图 → +3,含缺失成员情形)。"""
    before = telemetry.snapshot()["counters"].get("ensemble.images", 0.0)
    scores = [
        make_score("a.jpg", "m1", 0.9),
        make_score("a.jpg", "m2", 0.7),
        make_score("b.jpg", "m1", 0.4),  # b 缺 m2
        make_score("c.jpg", "m2", 0.1),
    ]
    result = ensemble_scores(scores)
    after = telemetry.snapshot()["counters"].get("ensemble.images", 0.0)
    assert len(result) == 3
    assert after == before + 3.0


# ---------------------------------------------------------------------------
# V5:单遍分组合并的语义回归锁定(改写循环后行为不变)
# ---------------------------------------------------------------------------

def test_v5_single_pass_grouping_semantics_unchanged() -> None:
    """混合场景一次锁全:分组、首现顺序、首条 image 引用、重复后值覆盖、
    ensemble 条目过滤、缺失成员清单。"""
    img_c_first = make_evidence("c.jpg")
    scores = [
        ImageScore(image=img_c_first, model="m1", nsfw_prob=0.1),
        ImageScore(image=make_evidence("a.jpg"), model="m1", nsfw_prob=0.9),
        ImageScore(image=make_evidence("c.jpg"), model="m1", nsfw_prob=0.5),  # c 的 m1 重复
        ImageScore(image=make_evidence("c.jpg"), model="m2", nsfw_prob=0.3),
        ImageScore(image=make_evidence("b.jpg"), model="m2", nsfw_prob=0.4),
        ImageScore(image=make_evidence("a.jpg"), model="ensemble", nsfw_prob=0.99),  # 过滤
    ]
    result = ensemble_scores(scores)

    # 首现顺序:c → a → b(而非模型顺序)
    assert [r.image.path for r in result] == ["c.jpg", "a.jpg", "b.jpg"]
    # c 的 image 是其第一条输入的引用
    assert result[0].image is img_c_first
    # 重复评分后值覆盖:m1 取 0.5
    assert result[0].scores["members"] == {"m1": 0.5, "m2": 0.3}
    assert result[0].nsfw_prob == pytest.approx((0.5 + 0.3) / 2)
    # a 缺 m2;b 缺 m1
    assert result[1].scores["members"] == {"m1": 0.9}
    assert result[1].scores["missing"] == ["m2"]
    assert result[2].scores["members"] == {"m2": 0.4}
    assert result[2].scores["missing"] == ["m1"]
    # ensemble 条目被过滤,未参与 a 的聚合
    assert result[1].nsfw_prob == pytest.approx(0.9)


def test_v5_weighted_average_after_single_pass_merge() -> None:
    """合并循环后加权路径数值仍精确(2:1 权重、越界截断保持)。"""
    scores = [
        make_score("x.jpg", "m1", 0.9),
        make_score("x.jpg", "m2", 0.3),
        make_score("y.jpg", "m1", 1.2),
        make_score("y.jpg", "m2", 0.8),
    ]
    result = ensemble_scores(scores, weights={"m1": 2.0, "m2": 1.0})
    assert result[0].nsfw_prob == pytest.approx((0.9 * 2 + 0.3) / 3)
    # (1.2*2 + 0.8)/3 ≈ 1.0667 → 截断到 1.0
    assert result[1].nsfw_prob == 1.0
