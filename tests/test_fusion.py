"""A29 netsentinel.decision.fusion 可解释特征融合引擎测试。

纯数据构造、离线、无 IO。覆盖:
- logit 融合数值(与手算精确/近似对照,手算用测试侧独立 sig() 实现);
- page_vlm 缺分 → 按 0.5 中性计且权重减半;
- 特征缺失(空 dict)→ 权重剔除后归一化;全缺 → fused == 图像原值;
- 只升不降:fused_final >= agg,verdict 档位与 needs_review 单调不降,
  辅助全零也不能把图像 NSFW 洗白;
- NSFW 档的图像张数闸门(nsw_image_count 不足时最高 SUSPECT);
- 就地更新并返回同一对象(id 相等),agg_nsw_prob 保持图像原值。

V5 新增(test_v5_* 前缀):sigmoid 极端值(±inf / 超大 |x|)稳定性、
fusion.fuse 计时与 fusion.escalated 升级计数、单遍乘积重构后归一化路径的
数值恒等(bias + Σcontrib ≈ logit(raw_prob))。

V12 新增(test_v12_* 前缀,A216):fuse_reliable 的 opt-in 贝叶斯路径
(bayes_tracker 注入)——默认路径快照逐字节一致(weight_model 键不出现)、
贝叶斯权重精确手算(17/24 : 7/24 → agg = 5/8)、未知成员中性回退、
贝叶斯优先于 Brier、空 tracker 等权回退。
"""
from __future__ import annotations

import math

import pytest

from netsentinel import telemetry
from netsentinel.contracts import (
    Config,
    ImageEvidence,
    ImageScore,
    PageSample,
    SiteReport,
    Verdict,
)
from netsentinel.decision.fusion import BIAS, WEIGHTS, _norm, _sigmoid, fuse
from netsentinel.decision.fusion_reliable import fuse_reliable
from netsentinel.decision.reliability import (
    BayesianReliabilityTracker,
    ReliabilityTracker,
)
from netsentinel.decision.verdict import assess

SITE = "https://example.test/"


def sig(x: float) -> float:
    """测试侧独立实现的 sigmoid(手算对照用,不复用被测代码)。"""
    return 1.0 / (1.0 + math.exp(-x))


# ---------------------------------------------------------------------------
# 辅助工厂(与 test_verdict.py 同款,仅内存数据,不落盘)
# ---------------------------------------------------------------------------


def make_evidence(
    name: str = "img.png", width: int = 250, height: int = 250
) -> ImageEvidence:
    return ImageEvidence(
        path=f"data/evidence/{name}",
        url=f"{SITE}images/{name}",
        source_page=SITE,
        width=width,
        height=height,
    )


def make_scores(probs: list[float]) -> list[ImageScore]:
    return [
        ImageScore(
            image=make_evidence(f"img_{i}.png"),
            model="ensemble",
            nsfw_prob=p,
        )
        for i, p in enumerate(probs)
    ]


def make_report(probs: list[float], cfg: Config | None = None) -> SiteReport:
    """用 v1 判定公式构造图像侧结论(真实链路,agg/count/verdict 均由 assess 给出)。"""
    pages = [PageSample(url=SITE, screenshot_path="data/shots/home.png")]
    return assess(SITE, pages, make_scores(probs), cfg or Config())


def zero_aux() -> tuple[dict, dict, dict]:
    """三路辅助特征全在席、全零风险。"""
    return {"risk": 0.0, "explain": []}, {"risk": 0.0}, {"page_nsfw_prob": 0.0}


def high_aux() -> tuple[dict, dict, dict]:
    """三路辅助特征全在席、最高风险。"""
    return {"risk": 1.0}, {"risk": 1.0}, {"page_nsfw_prob": 1.0}


# ---------------------------------------------------------------------------
# 基础纯函数
# ---------------------------------------------------------------------------


def test_sigmoid_basics() -> None:
    """sigmoid(0)=0.5、单调、两端饱和且大负数不溢出。"""
    assert _sigmoid(0.0) == pytest.approx(0.5)
    assert _sigmoid(1.0) > _sigmoid(0.0) > _sigmoid(-1.0)
    assert _sigmoid(10.0) == pytest.approx(1.0, abs=1e-4)
    assert _sigmoid(-10.0) == pytest.approx(0.0, abs=1e-4)  # 不抛 OverflowError


def test_norm_clamps_to_unit_interval() -> None:
    """_norm 把任意数值夹紧到 [0,1]。"""
    assert _norm(-0.5) == 0.0
    assert _norm(1.5) == 1.0
    assert _norm(0.42) == 0.42


# ---------------------------------------------------------------------------
# 场景一:纯净站 → 融合分低,CLEAN 不变
# ---------------------------------------------------------------------------


def test_clean_site_stays_clean_and_returns_same_object() -> None:
    """图像 agg 0.02 + 三路辅助全零:融合分 ~0.124,仍 CLEAN、免复核。

    手算:z = -2.0 + 2.2*0.02 = -1.956;fused = sigmoid(-1.956) ≈ 0.1239。
    """
    cfg = Config()
    report = make_report([0.02])
    assert report.verdict is Verdict.CLEAN

    url_feat, text_feat, page_vlm = zero_aux()
    ret = fuse(report, url_feat, text_feat, page_vlm, cfg)

    assert ret is report  # 就地更新,返回同一对象
    assert id(ret) == id(report)
    fusion = report.intel["fusion"]
    assert fusion["prob"] == pytest.approx(sig(-1.956), abs=1e-4)
    assert fusion["prob"] < cfg.review_threshold
    assert report.verdict is Verdict.CLEAN
    assert report.needs_review is False
    # 图像侧字段保持原值不动(fusion 单列)
    assert report.agg_nsw_prob == pytest.approx(0.02)
    assert report.nsw_image_count == 0


# ---------------------------------------------------------------------------
# 场景二:辅助特征加强复核倾向(只能往复核方向推)
# ---------------------------------------------------------------------------


def test_high_aux_upgrades_clean_to_suspect() -> None:
    """图像弱信号(agg 0.2,原 CLEAN)+ URL/文本/页面全高风险 → 升 SUSPECT 复核。

    手算:z = -2.0 + 2.2*0.2 + 1.2 + 0.35 + 0.45 = 0.44;fused ≈ 0.6083 >= 0.50。
    """
    cfg = Config()
    report = make_report([0.20])
    assert report.verdict is Verdict.CLEAN

    url_feat, text_feat, page_vlm = high_aux()
    fuse(report, url_feat, text_feat, page_vlm, cfg)

    fusion = report.intel["fusion"]
    assert fusion["prob"] == pytest.approx(sig(0.44), abs=1e-4)
    assert report.verdict is Verdict.SUSPECT
    assert report.needs_review is True


def test_suspect_prob_rises_with_high_aux() -> None:
    """图像 SUSPECT(agg 0.72)+ 辅助高风险:融合概率上升但未过 NSFW 线。

    手算:z = -2.0 + 2.2*0.72 + 2.0 = 1.584;fused ≈ 0.8298(> agg 0.72,< 0.90)。
    """
    cfg = Config()
    report = make_report([0.72, 0.72, 0.72])
    assert report.verdict is Verdict.SUSPECT

    url_feat, text_feat, page_vlm = high_aux()
    fuse(report, url_feat, text_feat, page_vlm, cfg)

    fusion = report.intel["fusion"]
    assert fusion["prob"] == pytest.approx(sig(1.584), abs=1e-4)
    assert fusion["prob"] > report.agg_nsw_prob  # 融合概率上升
    assert report.verdict is Verdict.SUSPECT
    assert report.agg_nsw_prob == pytest.approx(0.72)  # agg 保持图像原值


def test_nsfw_locked_to_suspect_when_image_count_insufficient() -> None:
    """图像 agg 0.95 但达标张数 2 < min_nsw_images:即使融合分过线也锁 SUSPECT。

    手算:z = -2.0 + 2.2*0.95 + 2.0 = 2.09;fused ≈ 0.8900;
    fused_final = max(0.8900, 0.95) = 0.95 >= 0.90,但 count 2 < 3 → 非 NSFW。
    """
    cfg = Config()
    report = make_report([0.95, 0.95])
    assert report.verdict is Verdict.SUSPECT
    assert report.nsw_image_count == 2

    url_feat, text_feat, page_vlm = high_aux()
    fuse(report, url_feat, text_feat, page_vlm, cfg)

    fusion = report.intel["fusion"]
    assert fusion["raw_prob"] == pytest.approx(sig(2.09), abs=1e-4)
    assert fusion["prob"] == pytest.approx(0.95)  # 只升不降:最终分不低于图像 agg
    assert fusion["prob"] >= cfg.nsfw_threshold  # 分数过线……
    assert report.verdict is Verdict.SUSPECT  # ……但图像张数不足,锁 SUSPECT
    assert report.needs_review is True


def test_upgrade_to_nsfw_possible_when_count_sufficient() -> None:
    """达标张数足够且融合分过 NSFW 线时可升 NSFW(辅助加强复核的上限方向)。

    自定义阈值 nsfw=0.75 / 计数线 0.5:4 张 0.6 → assess SUSPECT;
    手算:z = -2.0 + 2.2*0.6 + 2.0 = 1.32;fused ≈ 0.7892 >= 0.75 且 count 4 → NSFW。
    """
    cfg = Config(nsfw_threshold=0.75, prob_count_line=0.5)
    report = make_report([0.60, 0.60, 0.60, 0.60], cfg)
    assert report.verdict is Verdict.SUSPECT
    assert report.nsw_image_count == 4

    url_feat, text_feat, page_vlm = high_aux()
    fuse(report, url_feat, text_feat, page_vlm, cfg)

    fusion = report.intel["fusion"]
    assert fusion["prob"] == pytest.approx(sig(1.32), abs=1e-4)
    assert report.verdict is Verdict.NSFW
    assert report.needs_review is True

    # 对照:同样阈值但图像只有 1 张(count 1 < 3)→ 即使融合分过线也锁 SUSPECT
    report2 = make_report([0.60], cfg)
    url2, text2, page2 = high_aux()
    fuse(report2, url2, text2, page2, cfg)
    assert report2.intel["fusion"]["prob"] >= cfg.nsfw_threshold
    assert report2.verdict is Verdict.SUSPECT


# ---------------------------------------------------------------------------
# 场景三:只升不降 —— 图像 NSFW 不能被辅助特征洗白
# ---------------------------------------------------------------------------


def test_nsfw_with_zero_aux_stays_nsfw() -> None:
    """图像 NSFW(agg 0.95)+ 辅助全零:raw fused 仅 ~0.522,但最终分与档位不降。

    手算:z = -2.0 + 2.2*0.95 = 0.09;fused ≈ 0.5224;
    fused_final = max(0.5224, 0.95) = 0.95 → 仍 NSFW。
    """
    cfg = Config()
    report = make_report([0.95, 0.95, 0.95, 0.95])
    assert report.verdict is Verdict.NSFW

    url_feat, text_feat, page_vlm = zero_aux()
    fuse(report, url_feat, text_feat, page_vlm, cfg)

    fusion = report.intel["fusion"]
    assert fusion["raw_prob"] == pytest.approx(sig(0.09), abs=1e-4)
    assert fusion["raw_prob"] < report.agg_nsw_prob
    assert fusion["prob"] == pytest.approx(0.95)  # 只升不降
    assert report.verdict is Verdict.NSFW
    assert report.needs_review is True
    assert report.agg_nsw_prob == pytest.approx(0.95)


def test_verdict_never_downgraded_even_with_extreme_config() -> None:
    """极端配置下重算档位为 CLEAN,也必须钳制回原 NSFW(档位只升不降)。"""
    cfg = Config(nsfw_threshold=0.99, review_threshold=0.98)
    report = SiteReport(
        site_url=SITE,
        agg_nsw_prob=0.95,
        nsw_image_count=4,
        verdict=Verdict.NSFW,
        needs_review=True,
    )
    url_feat, text_feat, page_vlm = zero_aux()
    fuse(report, url_feat, text_feat, page_vlm, cfg)

    assert report.intel["fusion"]["prob"] == pytest.approx(0.95)
    assert 0.95 < cfg.review_threshold  # 若无钳制将重算为 CLEAN
    assert report.verdict is Verdict.NSFW  # 钳制生效
    assert report.needs_review is True


def test_needs_review_never_turned_off() -> None:
    """needs_review 单向:原 True 即使重算档位为 CLEAN 也不被翻 False。"""
    cfg = Config()
    report = SiteReport(
        site_url=SITE,
        agg_nsw_prob=0.02,
        nsw_image_count=0,
        verdict=Verdict.CLEAN,
        needs_review=True,  # 外部已标记复核
    )
    url_feat, text_feat, page_vlm = zero_aux()
    fuse(report, url_feat, text_feat, page_vlm, cfg)

    assert report.verdict is Verdict.CLEAN
    assert report.needs_review is True  # 恒 True 保留原 True


# ---------------------------------------------------------------------------
# page_vlm 缺分:按 0.5 中性计且权重减半
# ---------------------------------------------------------------------------


def test_page_vlm_none_score_halves_weight() -> None:
    """page_nsfw_prob=None:特征按 0.5 计,权重 1.2 → 0.6;contrib 精确手算对照。

    手算(agg 0.7,url 0.8,text 0.6):
    z = -2.0 + 2.2*0.7 + 0.6*0.5 + 0.35*0.8 + 0.45*0.6 = 0.39;fused ≈ 0.5963。
    """
    cfg = Config()
    report = make_report([0.70, 0.70, 0.70])
    url_feat: dict = {"risk": 0.8}
    text_feat: dict = {"risk": 0.6}
    page_vlm: dict = {"model": "glm", "page_nsfw_prob": None}

    fuse(report, url_feat, text_feat, page_vlm, cfg)

    fusion = report.intel["fusion"]
    # 精确断言(数值均为 4 位小数内的干净值)
    assert fusion["weights_used"] == {
        "image": 2.2,
        "page_vlm": 0.6,  # 1.2 减半
        "url": 0.35,
        "text": 0.45,
    }
    assert fusion["contrib"] == {
        "image": 1.54,   # 2.2 * 0.7
        "page_vlm": 0.3,  # 0.6 * 0.5(半权重 × 中性分)
        "url": 0.28,     # 0.35 * 0.8
        "text": 0.27,    # 0.45 * 0.6
    }
    assert fusion["raw_prob"] == pytest.approx(sig(0.39), abs=1e-4)
    assert fusion["prob"] == pytest.approx(0.7)  # fused 0.5963 < agg 0.7 → 取 agg


def test_page_vlm_score_key_absent_behaves_like_none() -> None:
    """page_vlm 在席但缺 page_nsfw_prob 键:与 None 等价(中性 + 减半)。"""
    cfg = Config()
    report = make_report([0.70, 0.70, 0.70])
    fuse(report, {"risk": 0.8}, {"risk": 0.6}, {"model": "glm"}, cfg)
    fusion = report.intel["fusion"]
    assert fusion["weights_used"]["page_vlm"] == 0.6
    assert fusion["contrib"]["page_vlm"] == 0.3


def test_halved_contrib_is_exactly_half_of_valid_neutral() -> None:
    """对照实验:同一场景下,缺分(权重减半)的 contrib 恰为有效 0.5 分的一半。"""
    cfg = Config()

    r_none = make_report([0.70, 0.70, 0.70])
    fuse(r_none, {"risk": 0.8}, {"risk": 0.6}, {"page_nsfw_prob": None}, cfg)

    r_valid = make_report([0.70, 0.70, 0.70])
    fuse(r_valid, {"risk": 0.8}, {"risk": 0.6}, {"page_nsfw_prob": 0.5}, cfg)

    c_none = r_none.intel["fusion"]["contrib"]
    c_valid = r_valid.intel["fusion"]["contrib"]
    w_none = r_none.intel["fusion"]["weights_used"]
    w_valid = r_valid.intel["fusion"]["weights_used"]
    assert c_none["page_vlm"] == pytest.approx(c_valid["page_vlm"] / 2)
    assert w_none["page_vlm"] == pytest.approx(w_valid["page_vlm"] / 2)
    # 其他特征的贡献不受 page_vlm 减半影响
    assert c_none["image"] == c_valid["image"]
    assert c_none["url"] == c_valid["url"]


# ---------------------------------------------------------------------------
# 特征缺失:权重剔除后归一化;全缺 → fused == 图像原值
# ---------------------------------------------------------------------------


def test_all_aux_missing_fused_equals_agg() -> None:
    """三路辅助全为空 dict:融合无意义,fused(含 raw_prob)等于图像原值。"""
    cfg = Config()
    report = make_report([0.72, 0.72, 0.72])
    assert report.verdict is Verdict.SUSPECT

    fuse(report, {}, {}, {}, cfg)

    fusion = report.intel["fusion"]
    assert fusion["prob"] == pytest.approx(0.72)
    assert fusion["raw_prob"] == pytest.approx(0.72)
    assert fusion["contrib"] == {"image": 0.72, "page_vlm": 0.0, "url": 0.0, "text": 0.0}
    assert fusion["weights_used"] == {
        "image": 1.0,
        "page_vlm": 0.0,
        "url": 0.0,
        "text": 0.0,
    }
    assert "无辅助特征" in fusion.get("note", "")
    assert report.verdict is Verdict.SUSPECT  # 档位不变
    assert report.needs_review is True


def test_all_aux_missing_with_none_page_vlm_same_result() -> None:
    """page_vlm 传 None 同样按缺失处理:全缺 → fused == agg。"""
    cfg = Config()
    report = make_report([0.30])
    fuse(report, {}, {}, None, cfg)  # type: ignore[arg-type]
    fusion = report.intel["fusion"]
    assert fusion["prob"] == pytest.approx(0.30)
    assert report.verdict is Verdict.CLEAN


def test_missing_page_vlm_renormalizes_remaining_weights() -> None:
    """仅 page_vlm 缺失:image/url/text 权重按原始总权重 4.2 归一化。

    手算(agg 0.6,url 1.0,text 1.0):
    剩余权重和 = 2.2+0.35+0.45 = 3.0,scale = 4.2/3.0 = 1.4;
    w = {image 3.08, url 0.49, text 0.63};
    z = -2.0 + 3.08*0.6 + 0.49 + 0.63 = 0.968;fused ≈ 0.7247。
    """
    cfg = Config()
    report = make_report([0.60])

    fuse(report, {"risk": 1.0}, {"risk": 1.0}, {}, cfg)

    fusion = report.intel["fusion"]
    assert fusion["weights_used"] == {
        "image": 3.08,   # 2.2 * 1.4
        "page_vlm": 0.0,  # 缺失剔除
        "url": 0.49,     # 0.35 * 1.4
        "text": 0.63,    # 0.45 * 1.4
    }
    assert fusion["contrib"] == {
        "image": 1.848,  # 3.08 * 0.6
        "page_vlm": 0.0,
        "url": 0.49,     # 0.49 * 1.0
        "text": 0.63,    # 0.63 * 1.0
    }
    assert fusion["raw_prob"] == pytest.approx(sig(0.968), abs=1e-4)
    assert fusion["prob"] > report.agg_nsw_prob  # 0.7247 > 0.6


def test_halved_page_plus_missing_url_text_renormalizes() -> None:
    """减半与归一化叠加:page 中性(0.6)+ url/text 缺失。

    手算(agg 0.8):有效权重和 = 2.2+0.6 = 2.8,scale = 4.2/2.8 = 1.5;
    w = {image 3.3, page_vlm 0.9};z = -2.0 + 3.3*0.8 + 0.9*0.5 = 1.09。
    """
    cfg = Config()
    report = make_report([0.80])

    fuse(report, {}, {}, {"page_nsfw_prob": None}, cfg)

    fusion = report.intel["fusion"]
    assert fusion["weights_used"]["image"] == pytest.approx(3.3)
    assert fusion["weights_used"]["page_vlm"] == pytest.approx(0.9)
    assert fusion["weights_used"]["url"] == 0.0
    assert fusion["weights_used"]["text"] == 0.0
    assert fusion["contrib"]["image"] == pytest.approx(2.64)
    assert fusion["contrib"]["page_vlm"] == pytest.approx(0.45)
    assert fusion["raw_prob"] == pytest.approx(sig(1.09), abs=1e-4)
    assert fusion["prob"] == pytest.approx(0.8)  # fused 0.7484 < agg 0.8 → 取 agg


# ---------------------------------------------------------------------------
# 值域夹紧与非法值兜底(提示注入防御:非法值按缺失/保守处理)
# ---------------------------------------------------------------------------


def test_page_prob_out_of_range_clamped_and_risk_none_falls_back() -> None:
    """page 分 1.7 夹紧为 1.0;url 在席但 risk=None 按 0.0 计(不剔除、不归一化误伤)。

    手算(agg 0.3,url 0.0,text 缺失,page 1.0):
    有效权重和 = 2.2+1.2+0.35 = 3.75,scale = 4.2/3.75 = 1.12;
    z = -2.0 + 2.464*0.3 + 1.344*1.0 + 0 = 0.0832;fused ≈ 0.5208 >= 0.5 → SUSPECT。
    """
    cfg = Config()
    report = make_report([0.30])
    assert report.verdict is Verdict.CLEAN

    fuse(report, {"risk": None}, {}, {"page_nsfw_prob": 1.7}, cfg)

    fusion = report.intel["fusion"]
    assert fusion["weights_used"] == {
        "image": 2.464,   # 2.2 * 1.12
        "page_vlm": 1.344,  # 1.2 * 1.2(全权重 × 归一化)
        "url": 0.392,     # 0.35 * 1.12(在席但风险按 0)
        "text": 0.0,
    }
    assert fusion["contrib"] == {
        "image": 0.7392,   # 2.464 * 0.3
        "page_vlm": 1.344,  # 1.344 * 1.0(1.7 已夹紧)
        "url": 0.0,
        "text": 0.0,
    }
    assert fusion["raw_prob"] == pytest.approx(sig(0.0832), abs=1e-4)
    assert report.verdict is Verdict.SUSPECT  # 复核倾向增强


def test_page_prob_negative_clamped_to_zero() -> None:
    """page 分 -0.5 夹紧为 0.0,contrib 为 0,不产生负向拉扯。"""
    cfg = Config()
    report = make_report([0.30])
    fuse(report, {}, {}, {"page_nsfw_prob": -0.5}, cfg)
    fusion = report.intel["fusion"]
    assert fusion["contrib"]["page_vlm"] == 0.0
    assert report.verdict is Verdict.CLEAN


# ---------------------------------------------------------------------------
# 边界:无图像证据 + 高辅助风险
# ---------------------------------------------------------------------------


def test_empty_image_evidence_with_high_aux_is_suspect_not_nsfw() -> None:
    """空 ensemble(agg 0)+ 辅助全高:z = 0 → fused 恰 0.5 → SUSPECT,绝不 NSFW。"""
    cfg = Config()
    pages = [PageSample(url=SITE)]
    report = assess(SITE, pages, [], cfg)
    assert report.verdict is Verdict.CLEAN

    url_feat, text_feat, page_vlm = high_aux()
    fuse(report, url_feat, text_feat, page_vlm, cfg)

    fusion = report.intel["fusion"]
    assert fusion["prob"] == pytest.approx(0.5)  # sig(-2.0 + 2.0) = sig(0) = 0.5
    assert report.verdict is Verdict.SUSPECT  # count 0 < 3 → 最高 SUSPECT
    assert report.needs_review is True


# ---------------------------------------------------------------------------
# intel 结构契约
# ---------------------------------------------------------------------------


def test_intel_structure_and_explain_fields() -> None:
    """intel 结构:intel = {url, text, page_vlm, fusion};fusion 五要素齐全。"""
    cfg = Config()
    report = make_report([0.70, 0.70, 0.70])
    url_feat: dict = {"risk": 0.8}
    text_feat: dict = {"risk": 0.6}
    page_vlm: dict = {"page_nsfw_prob": 0.4, "elements": [], "model": "glm"}

    fuse(report, url_feat, text_feat, page_vlm, cfg)

    intel = report.intel
    assert set(intel.keys()) == {"url", "text", "page_vlm", "fusion"}
    assert intel["url"] is url_feat  # 原文引用透传
    assert intel["text"] is text_feat
    assert intel["page_vlm"] is page_vlm

    fusion = intel["fusion"]
    for key in ("prob", "raw_prob", "contrib", "weights_used", "rule"):
        assert key in fusion
    assert set(fusion["contrib"].keys()) == {"image", "page_vlm", "url", "text"}
    assert set(fusion["weights_used"].keys()) == {"image", "page_vlm", "url", "text"}
    assert fusion["rule"] == "只升不降:辅助特征仅加强复核,不降低图像判定"
    assert isinstance(fusion["prob"], float) and 0.0 <= fusion["prob"] <= 1.0

    # as_dict() 在 intel 非空时输出
    d = report.as_dict()
    assert d["intel"]["fusion"]["prob"] == fusion["prob"]


def test_contrib_sums_reconstruct_logit() -> None:
    """可解释性自洽:bias + Σcontrib ≈ logit(raw_prob)(4 位小数舍入容差内)。

    手算(agg 0.72,page 0.9,url 0.4,text 0.5,全在席):
    z = -2.0 + 2.2*0.72 + 1.2*0.9 + 0.35*0.4 + 0.45*0.5 = 1.029;
    """
    cfg = Config()
    report = make_report([0.72, 0.72, 0.72])
    fuse(report, {"risk": 0.4}, {"risk": 0.5}, {"page_nsfw_prob": 0.9}, cfg)

    fusion = report.intel["fusion"]
    expected_z = BIAS + (
        WEIGHTS["image"] * 0.72
        + WEIGHTS["page_vlm"] * 0.9
        + WEIGHTS["url"] * 0.4
        + WEIGHTS["text"] * 0.5
    )
    assert expected_z == pytest.approx(1.029, abs=1e-9)
    assert fusion["raw_prob"] == pytest.approx(sig(expected_z), abs=1e-4)
    assert sum(fusion["contrib"].values()) == pytest.approx(expected_z - BIAS, abs=1e-3)
    # 融合概率介于图像 agg 与 1 之间,且不低于 agg
    assert fusion["prob"] >= report.agg_nsw_prob


# ---------------------------------------------------------------------------
# V5:sigmoid 极端值稳定性 / 融合遥测 / 单遍重构后的数值恒等
# ---------------------------------------------------------------------------


def test_v5_sigmoid_extreme_inputs_stable() -> None:
    """sigmoid 极端输入复查锁定:±inf 与超大 |x| 均下溢饱和,绝不 OverflowError。"""
    assert _sigmoid(float("inf")) == 1.0
    assert _sigmoid(float("-inf")) == 0.0
    assert _sigmoid(1e300) == 1.0  # exp(-1e300) 下溢为 0.0,不抛异常
    assert _sigmoid(-1e300) == 0.0  # 负分支:exp(-1e300) 下溢为 0.0
    assert _sigmoid(800.0) == 1.0  # 远超 exp 上界 ~709 的正数也安全
    assert _sigmoid(-800.0) == 0.0
    assert _sigmoid(0.0) == pytest.approx(0.5)  # 极端值复查不破坏原点


def test_v5_fuse_telemetry_timer_and_escalation() -> None:
    """fusion.fuse 每次调用计时;fusion.escalated 仅在档位被抬高时 +1。"""
    counters = telemetry.snapshot()["counters"]
    base_esc = counters.get("fusion.escalated", 0.0)
    base_runs = telemetry.snapshot()["timers"].get("fusion.fuse", {}).get("count", 0)

    # CLEAN + 辅助全零:仍 CLEAN → 不升级
    r1 = make_report([0.02])
    fuse(r1, *zero_aux(), Config())
    assert telemetry.snapshot()["counters"].get("fusion.escalated", 0.0) == base_esc

    # NSFW + 辅助全零:raw fused 更低但档位钳制保持 NSFW → 不升级
    r2 = make_report([0.95, 0.95, 0.95, 0.95])
    fuse(r2, *zero_aux(), Config())
    assert telemetry.snapshot()["counters"].get("fusion.escalated", 0.0) == base_esc

    # CLEAN + 辅助全高:升 SUSPECT → +1
    r3 = make_report([0.20])
    fuse(r3, *high_aux(), Config())
    assert r3.verdict is Verdict.SUSPECT
    assert telemetry.snapshot()["counters"].get("fusion.escalated", 0.0) == base_esc + 1.0

    assert telemetry.snapshot()["timers"]["fusion.fuse"]["count"] >= base_runs + 3


def test_v5_renormalized_contrib_reconstructs_logit() -> None:
    """V5 单遍乘积重构后在**归一化路径**上的数值恒等:bias + Σcontrib
    ≈ logit(raw_prob)(4 位小数舍入容差内),与旧双遍实现逐位一致。

    手算(agg 0.6,url 1.0,text 1.0,page 缺失):
    剩余权重和 3.0,scale = 4.2/3.0 = 1.4;z = -2.0 + 3.08*0.6 + 0.49 + 0.63 = 0.968。
    """
    cfg = Config()
    report = make_report([0.60])
    fuse(report, {"risk": 1.0}, {"risk": 1.0}, {}, cfg)

    fusion = report.intel["fusion"]
    z_expected = BIAS + (
        round(WEIGHTS["image"] * 1.4, 4) * 0.6
        + round(WEIGHTS["url"] * 1.4, 4)
        + round(WEIGHTS["text"] * 1.4, 4)
    )
    assert z_expected == pytest.approx(0.968, abs=1e-9)
    assert fusion["raw_prob"] == pytest.approx(sig(z_expected), abs=1e-4)
    assert BIAS + sum(fusion["contrib"].values()) == pytest.approx(z_expected, abs=1e-3)
    assert fusion["prob"] > report.agg_nsw_prob  # 0.7247 > 0.6,只升方向不变


# ===========================================================================
# V12(A216):fuse_reliable 的 opt-in 贝叶斯路径(bayes_tracker 注入)
# ===========================================================================


def bayes_two_member() -> BayesianReliabilityTracker:
    """无遗忘双成员 tracker:glm 10 对 / stub 10 错。

    手算:mean_raw = 11/12 / 1/12;池 = 11/22 = 0.5;κ=10 → λ = 10/20 = 0.5;
    θ*(glm) = 0.5·(11/12) + 0.5·0.5 = 17/24,θ*(stub) = 7/24(Σ 恰为 1,
    归一权重即 17/24 : 7/24)。
    """
    tracker = BayesianReliabilityTracker(half_life=None)
    for i in range(10):
        tracker.record("glm", True, ts=float(i))
        tracker.record("stub", False, ts=float(i))
    return tracker


def bayes_report(extra: list[ImageScore] | None = None) -> SiteReport:
    """glm 0.8 / stub 0.2 双成员报告(直构,不依赖 assess / ensemble)。"""
    scores = [
        ImageScore(image=make_evidence("a.png"), model="glm:glm-5.3", nsfw_prob=0.8),
        ImageScore(image=make_evidence("a.png"), model="stub", nsfw_prob=0.2),
    ]
    scores.extend(extra or [])
    return SiteReport(
        site_url=SITE,
        image_scores=scores,
        agg_nsw_prob=0.5,
        nsw_image_count=0,
        verdict=Verdict.CLEAN,
        needs_review=False,
    )


def bayes_zero_aux() -> tuple[dict, dict, dict]:
    """三路辅助在席、全零风险(与 reliability 测试的 zero_aux 同款)。"""
    return {"risk": 0.0, "explain": []}, {"risk": 0.0}, {"page_nsfw_prob": 0.0}


def dominance_brier_tracker() -> ReliabilityTracker:
    """Brier 对照:6 对 0 错 vs 6 错 → 权重 21/22 : 1/22。"""
    tracker = ReliabilityTracker()
    for _ in range(6):
        tracker.record("glm", 1.0, True)
        tracker.record("stub", 1.0, False)
    return tracker


def test_v12_default_path_snapshot_identical_with_explicit_none() -> None:
    """opt-in 缺省语义:bayes_tracker=None(显式)与不传(缺省)输出逐字节一致。

    Brier 主路径(21/22 : 1/22):agg = 17/22,intel 键集与 V7 完全相同
    ——**无 weight_model 键**(新增键只在贝叶斯路径出现,默认路径形状不变)。
    """
    cfg = Config()
    r1 = bayes_report()
    r2 = bayes_report()
    fuse_reliable(r1, *bayes_zero_aux(), cfg, tracker=dominance_brier_tracker())
    fuse_reliable(
        r2, *bayes_zero_aux(), cfg,
        tracker=dominance_brier_tracker(), bayes_tracker=None,  # 显式 None
    )
    assert r1.intel["fusion"] == r2.intel["fusion"]  # 整 dict 逐字节相等
    fusion = r1.intel["fusion"]
    assert set(fusion) == {
        "prob", "raw_prob", "contrib", "weights_used",
        "rule", "agg_reliable", "member_weights",
    }  # V7 七键,无 weight_model
    assert fusion["member_weights"] == {"glm": 0.9545, "stub": 0.0455}
    assert fusion["agg_reliable"] == pytest.approx(17.0 / 22.0, abs=1e-4)


def test_v12_bayes_weights_exact_hand_computed() -> None:
    """贝叶斯注入精确手算:17/24 : 7/24 → agg = 0.8·17/24+0.2·7/24 = 5/8。

    z = -2.0 + 2.2·(5/8) = -0.625 → raw = sigmoid(-0.625) ≈ 0.3489;
    只升不降:prob = max(0.3489, 0.625) = 0.625;intel 多出
    weight_model = "bayes"。
    """
    cfg = Config()
    report = bayes_report()
    fuse_reliable(report, *bayes_zero_aux(), cfg, bayes_tracker=bayes_two_member())
    fusion = report.intel["fusion"]

    assert fusion["weight_model"] == "bayes"  # opt-in 标记键
    assert fusion["member_weights"] == {"glm": 0.7083, "stub": 0.2917}  # 17/24 : 7/24
    assert fusion["agg_reliable"] == pytest.approx(0.625, abs=1e-4)  # 5/8
    assert fusion["contrib"]["image"] == pytest.approx(1.375, abs=1e-4)  # 2.2·0.625
    assert fusion["raw_prob"] == pytest.approx(sig(-0.625), abs=1e-4)
    assert fusion["prob"] == pytest.approx(0.625, abs=1e-4)  # 只升不降下限
    assert report.verdict is Verdict.SUSPECT  # 0.625 >= 0.50,自 CLEAN 升档
    assert report.needs_review is True


def test_v12_bayes_unknown_member_neutral_fallback() -> None:
    """tracker 不含的成员按已知权重均值(0.5)参与:nudenet 不清零不垄断。

    手算:agg = (17/24·0.8 + 7/24·0.2 + 0.5·0.6) / (17/24+7/24+0.5)
    = 0.925/1.5 ≈ 0.6167。
    """
    cfg = Config()
    extra = [
        ImageScore(image=make_evidence("b.png"), model="nudenet", nsfw_prob=0.6),
    ]
    report = bayes_report(extra)
    fuse_reliable(report, *bayes_zero_aux(), cfg, bayes_tracker=bayes_two_member())
    fusion = report.intel["fusion"]

    assert fusion["agg_reliable"] == pytest.approx(0.925 / 1.5, abs=1e-4)
    mw = fusion["member_weights"]
    assert set(mw) == {"glm", "stub", "nudenet"}
    assert mw["nudenet"] == pytest.approx(1.0 / 3.0, abs=1e-4)  # 0.5/1.5
    assert mw["glm"] == pytest.approx(17.0 / 36.0, abs=1e-4)  # (17/24)/1.5
    assert mw["stub"] == pytest.approx(7.0 / 36.0, abs=1e-4)
    assert sum(mw.values()) == pytest.approx(1.0, abs=1e-4)


def test_v12_bayes_precedence_over_brier_tracker() -> None:
    """双 tracker 同传:贝叶斯优先——member_weights 为 17/24 口径而非 21/22。"""
    cfg = Config()
    report = bayes_report()
    fuse_reliable(
        report, *bayes_zero_aux(), cfg,
        tracker=dominance_brier_tracker(), bayes_tracker=bayes_two_member(),
    )
    fusion = report.intel["fusion"]
    assert fusion["member_weights"] == {"glm": 0.7083, "stub": 0.2917}  # 贝叶斯口径
    assert fusion["agg_reliable"] == pytest.approx(0.625, abs=1e-4)
    assert fusion["weight_model"] == "bayes"

    # 对照:仅 Brier(同数据)→ 21/22 口径,两路径数值确实不同
    report2 = bayes_report()
    fuse_reliable(report2, *bayes_zero_aux(), cfg, tracker=dominance_brier_tracker())
    f2 = report2.intel["fusion"]
    assert f2["member_weights"] == {"glm": 0.9545, "stub": 0.0455}
    assert "weight_model" not in f2
    assert f2["agg_reliable"] != pytest.approx(0.625, abs=1e-3)


def test_v12_bayes_empty_tracker_equal_weights() -> None:
    """空贝叶斯 tracker:无可用权重 → 全体等权回退(与旧行为一致),
    但 weight_model 标记仍为 "bayes"(opt-in 路径可见)。"""
    cfg = Config()
    report = bayes_report()
    fuse_reliable(report, *bayes_zero_aux(), cfg, bayes_tracker=BayesianReliabilityTracker())
    fusion = report.intel["fusion"]
    assert fusion["weight_model"] == "bayes"
    assert fusion["member_weights"] == {"glm": 0.5, "stub": 0.5}
    assert fusion["agg_reliable"] == pytest.approx(0.5, abs=1e-4)  # 等权 = 均值
