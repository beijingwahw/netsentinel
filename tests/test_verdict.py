"""A09 netsentinel.decision.verdict 判定公式测试。

纯数据构造,离线,不做任何文件 IO;依据 CONTRACTS.md §4:
candidates(尺寸过滤)→ agg(max)→ count(计数线)→ verdict → needs_review。

V5 新增(test_v5_* 前缀):verdict.assess 计时与 verdict.<档位> 计数遥测、
单遍 candidates 扫描的语义回归锁定(含负分候选的 max 语义)。
"""
from __future__ import annotations

import pytest

from netsentinel import telemetry
from netsentinel.contracts import (
    Config,
    ImageEvidence,
    ImageScore,
    PageSample,
    Verdict,
)
from netsentinel.decision.verdict import assess

SITE = "https://example.test/"


# ---------------------------------------------------------------------------
# 辅助工厂
# ---------------------------------------------------------------------------


def make_evidence(
    name: str = "img.png", width: int = 250, height: int = 250
) -> ImageEvidence:
    """构造一张 250x250 的受检图片证据(仅内存数据,不落盘)。"""
    return ImageEvidence(
        path=f"data/evidence/{name}",
        url=f"{SITE}images/{name}",
        source_page=SITE,
        width=width,
        height=height,
    )


def make_score(
    prob: float,
    width: int = 250,
    height: int = 250,
    idx: int = 0,
    model: str = "ensemble",
) -> ImageScore:
    """构造一条 ensemble 模型评分。"""
    return ImageScore(
        image=make_evidence(f"img_{idx}.png", width, height),
        model=model,
        nsfw_prob=prob,
    )


def make_scores(
    probs: list[float], width: int = 250, height: int = 250
) -> list[ImageScore]:
    return [
        make_score(p, width=width, height=height, idx=i)
        for i, p in enumerate(probs)
    ]


def make_page() -> PageSample:
    return PageSample(url=SITE, screenshot_path="data/shots/home.png")


# ---------------------------------------------------------------------------
# 基本判定分支
# ---------------------------------------------------------------------------


def test_nsfw_when_high_score_and_enough_images() -> None:
    """4 张 0.95、250px:agg 达标且张数达标 → NSFW(仍需人工复核)。"""
    cfg = Config()
    report = assess(SITE, [make_page()], make_scores([0.95, 0.95, 0.95, 0.95]), cfg)
    assert report.verdict is Verdict.NSFW
    assert report.needs_review is True
    assert report.agg_nsw_prob == pytest.approx(0.95)
    assert report.nsw_image_count == 4


def test_high_score_but_too_few_images_is_suspect() -> None:
    """2 张 0.95:agg 达标但张数不足 min_nsw_images → SUSPECT。"""
    cfg = Config()
    report = assess(SITE, [make_page()], make_scores([0.95, 0.95]), cfg)
    assert report.verdict is Verdict.SUSPECT
    assert report.needs_review is True
    assert report.agg_nsw_prob == pytest.approx(0.95)
    assert report.nsw_image_count == 2


def test_mid_score_between_thresholds_is_suspect() -> None:
    """agg 低于 nsfw_threshold 但 >= review_threshold → SUSPECT。"""
    cfg = Config()
    report = assess(SITE, [make_page()], make_scores([0.60, 0.55, 0.58]), cfg)
    assert report.verdict is Verdict.SUSPECT
    assert report.needs_review is True
    assert report.agg_nsw_prob == pytest.approx(0.60)
    # 0.6 < prob_count_line(0.8),不计入达标张数
    assert report.nsw_image_count == 0


def test_all_low_scores_is_clean() -> None:
    """全低分 → CLEAN,且 needs_review=False。"""
    cfg = Config()
    report = assess(SITE, [make_page()], make_scores([0.20, 0.10, 0.05, 0.30]), cfg)
    assert report.verdict is Verdict.CLEAN
    assert report.needs_review is False
    assert report.agg_nsw_prob == pytest.approx(0.30)
    assert report.nsw_image_count == 0


# ---------------------------------------------------------------------------
# 小图过滤
# ---------------------------------------------------------------------------


def test_small_images_excluded_from_agg_and_count() -> None:
    """150px 高分小图不参与判定:agg=0、count=0 → CLEAN。"""
    cfg = Config()
    report = assess(
        SITE,
        [make_page()],
        make_scores([0.99, 0.99, 0.99, 0.99], width=150, height=150),
        cfg,
    )
    assert report.verdict is Verdict.CLEAN
    assert report.needs_review is False
    assert report.agg_nsw_prob == 0.0
    assert report.nsw_image_count == 0


def test_small_image_high_score_does_not_leak_into_agg() -> None:
    """混合场景:agg 只来自合格尺寸图片,小图高分不抬升 agg。"""
    cfg = Config()
    ens = make_scores([0.99], width=150, height=150) + make_scores([0.30])
    report = assess(SITE, [make_page()], ens, cfg)
    assert report.agg_nsw_prob == pytest.approx(0.30)
    assert report.nsw_image_count == 0
    assert report.verdict is Verdict.CLEAN


def test_size_filter_accepts_one_dimension_at_least() -> None:
    """过滤条件是"或":宽 150 但高 300 的图仍参与判定。"""
    cfg = Config()
    ens = make_scores([0.90], width=150, height=300) + make_scores(
        [0.99], width=150, height=150
    )
    report = assess(SITE, [make_page()], ens, cfg)
    assert report.agg_nsw_prob == pytest.approx(0.90)
    assert report.nsw_image_count == 1
    assert report.verdict is Verdict.SUSPECT


def test_image_size_exactly_min_px_counts() -> None:
    """尺寸恰好等于 min_image_px(>=)的图片计入候选。"""
    cfg = Config()
    report = assess(
        SITE,
        [make_page()],
        make_scores([0.95, 0.95, 0.95, 0.95], width=cfg.min_image_px, height=cfg.min_image_px),
        cfg,
    )
    assert report.verdict is Verdict.NSFW
    assert report.nsw_image_count == 4


# ---------------------------------------------------------------------------
# 空输入
# ---------------------------------------------------------------------------


def test_empty_ensemble_is_clean() -> None:
    """空 ensemble → CLEAN、agg=0、count=0。"""
    cfg = Config()
    report = assess(SITE, [], [], cfg)
    assert report.verdict is Verdict.CLEAN
    assert report.needs_review is False
    assert report.agg_nsw_prob == 0.0
    assert report.nsw_image_count == 0
    assert report.image_scores == []


# ---------------------------------------------------------------------------
# 边界:恰好等于阈值(>=)
# ---------------------------------------------------------------------------


def test_boundary_agg_equals_nsfw_threshold_is_nsfw() -> None:
    """agg 恰好等于 nsfw_threshold(0.90)且张数达标 → NSFW。"""
    cfg = Config()
    report = assess(SITE, [make_page()], make_scores([0.90, 0.90, 0.90]), cfg)
    assert report.verdict is Verdict.NSFW
    assert report.needs_review is True
    assert report.agg_nsw_prob == pytest.approx(cfg.nsfw_threshold)


def test_boundary_just_below_nsfw_threshold_is_suspect() -> None:
    """agg 0.89 略低于 0.90,即使张数达标也只是 SUSPECT。"""
    cfg = Config()
    report = assess(SITE, [make_page()], make_scores([0.89, 0.89, 0.89, 0.89]), cfg)
    assert report.verdict is Verdict.SUSPECT


def test_boundary_prob_equals_count_line_counts() -> None:
    """单图分恰好等于 prob_count_line(0.80)即计入 nsw_image_count(>=)。"""
    cfg = Config(nsfw_threshold=0.80)
    report = assess(SITE, [make_page()], make_scores([0.80, 0.80, 0.80]), cfg)
    assert report.nsw_image_count == 3
    assert report.verdict is Verdict.NSFW


def test_boundary_agg_equals_review_threshold_is_suspect() -> None:
    """agg 恰好等于 review_threshold(0.50)→ SUSPECT;略低 0.49 → CLEAN。"""
    cfg = Config()
    report = assess(SITE, [make_page()], make_scores([0.50]), cfg)
    assert report.verdict is Verdict.SUSPECT
    assert report.needs_review is True

    report_low = assess(SITE, [make_page()], make_scores([0.49]), cfg)
    assert report_low.verdict is Verdict.CLEAN
    assert report_low.needs_review is False


# ---------------------------------------------------------------------------
# 配置与字段透传
# ---------------------------------------------------------------------------


def test_custom_config_thresholds_respected() -> None:
    """自定义阈值生效:2 张 0.75 在收紧后的配置下判 NSFW。"""
    cfg = Config(
        nsfw_threshold=0.70,
        review_threshold=0.30,
        prob_count_line=0.60,
        min_nsw_images=2,
    )
    report = assess(SITE, [make_page()], make_scores([0.75, 0.75]), cfg)
    assert report.verdict is Verdict.NSFW
    assert report.nsw_image_count == 2


def test_report_carries_inputs_and_metadata() -> None:
    """SiteReport 透传 site_url / pages / image_scores,并带时间戳。"""
    cfg = Config()
    pages = [make_page()]
    ens = make_scores([0.95, 0.95, 0.95, 0.95])
    report = assess(SITE, pages, ens, cfg)
    assert report.site_url == SITE
    assert report.pages is pages
    assert report.image_scores is ens
    assert isinstance(report.created_at, str) and report.created_at


# ---------------------------------------------------------------------------
# V5:遥测(verdict.assess 计时 / verdict.<档位> 计数)与单遍扫描语义锁定
# ---------------------------------------------------------------------------


def test_v5_telemetry_counts_verdict_tiers_and_timer() -> None:
    """每次 assess 计时一次,并按判定档位累计 verdict.clean / verdict.nsfw。"""
    counters = telemetry.snapshot()["counters"]
    base_clean = counters.get("verdict.clean", 0.0)
    base_nsfw = counters.get("verdict.nsfw", 0.0)
    base_suspect = counters.get("verdict.suspect", 0.0)
    base_runs = telemetry.snapshot()["timers"].get("verdict.assess", {}).get("count", 0)

    assess(SITE, [], [], Config())                                # → clean
    assess(SITE, [make_page()], make_scores([0.95, 0.95, 0.95, 0.95]), Config())  # → nsfw

    counters = telemetry.snapshot()["counters"]
    assert counters.get("verdict.clean", 0.0) == base_clean + 1.0
    assert counters.get("verdict.nsfw", 0.0) == base_nsfw + 1.0
    assert counters.get("verdict.suspect", 0.0) == base_suspect  # 未产生 suspect
    assert telemetry.snapshot()["timers"]["verdict.assess"]["count"] >= base_runs + 2


def test_v5_single_pass_candidates_semantics_locked() -> None:
    """V5 单遍扫描语义锁定:过滤(或)、agg(保序 max)、count(闭下限)与
    旧"建列表 + max + sum"三趟实现逐位一致——特别是负分候选时
    ``max(..., default=0.0)`` 取负最大值而非 0 下限。"""
    cfg = Config()
    ens = (
        make_scores([0.99], width=150, height=150)  # 小图:排除,高分不泄入
        + make_scores([-0.20])  # 候选,负分
        + make_scores([0.30])  # 候选
    )
    report = assess(SITE, [make_page()], ens, cfg)
    assert report.agg_nsw_prob == pytest.approx(0.30)  # max(-0.20, 0.30)
    assert report.nsw_image_count == 0  # 均低于计数线 0.80
    assert report.verdict is Verdict.CLEAN
    assert report.needs_review is False

    # 候选全为负分:agg 为负最大值(-0.20),单遍实现的 None 哨兵不注入 0 下限
    report_neg = assess(SITE, [make_page()], make_scores([-0.20, -0.50]), cfg)
    assert report_neg.agg_nsw_prob == pytest.approx(-0.20)
    assert report_neg.nsw_image_count == 0
    assert report_neg.verdict is Verdict.CLEAN
