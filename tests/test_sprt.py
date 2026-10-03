"""A125 netsentinel.decision.sprt 序贯概率比检验(SPRT)测试。

纯数学 / 纯构造数据,离线、无 IO / 网络。覆盖(CONTRACTS-V7 §2 A125):

- LLR 手算对照(3 组精确值 + 极端证据值,逐项写出推导);
- A/B 边界值(默认 ln(19)/-ln(19),自定义 α/β);
- 边界判决**含等值**(≥A / ≤B,白盒对 _decide 精确赋值验证);
- 高置信序列早停:全 0.97 → 第 6 张停 nsfw(每张 LLR 上限 ln(1.8)≈0.588,
  故该参数化下 nsfw 早停下限即 6 张;任务示例"2 张停"仅在可调常量
  p0=0.1/p1=0.9 的对称假设下可达,见 test_adjustable_constants_*);
  全 0.02 → 第 2 张停 clean;
- 摇摆序列(0.99/0.5 交替)LLR 上下振荡但始终在 (B, A) 内 → 用满预算;
- 吸收态:终判后 update/decide_sequence 不再消耗样本、LLR 不动;
- clamp:[0.01, 0.99] 钳制对 0/1/越界/负值一致;
- 非法输入(非数值 / NaN / α/β/p0/p1 越界)中文 ValueError;
- LLR 单调性与零点(p≈0.73);
- next_images:不确定度升序 + ensemble 过滤(rank_for_vlm 语义)、判停截断、
  预算上限(cfg.vlm_max_images_per_site / max_send 覆盖)、cfg α/β 接线、
  空输入 / 非 ensemble 输入;
- 常量可调:p0/p1 构造覆盖(对称假设下 2 张即停,且不动模块全局);
- 校准假设 docstring 锁定(校准 / use_sprt / kernel_wire 关键词);
- kernel_selfcheck(A138 约定字段);
- test_v7_bench_early_stop:20 张序列第 4 张停,N<20(操作计数断言,零墙钟,
  红线 31);
- e-process/anytime 内核(A160+):MixtureSPRT 闭式 e-值精确对照、
  **鞅性质双重验证**(全路径枚举 E[E_n]=1 精确成立 + 固定种子模拟)、
  Ville anytime 越阈率 ≤ α+松弛、H0 下误判率模拟、三态判决/方向/对称性、
  吸收态冻结、对数域数值稳定(极端计数 lgamma 有限、exp 溢出守护 → inf)、
  clamp 一致;Howard stitching 半径精确值/分段行为、anytime CI 形状/钳制、
  流式与批式置信序列一致、**覆盖率模拟**(任意时刻真值在界内比例 ≥1-δ-松弛)、
  决策带判停(纯函数 + 序列驱动)、参数校验中文 ValueError;
  Wald SPRT 向后兼容锁定(边界/早停/常量/自检不变)。
"""
from __future__ import annotations

import math
import random

import pytest

from netsentinel.contracts import Config, ImageEvidence, ImageScore
from netsentinel.decision import sprt as sprt_mod
from netsentinel.decision.sprt import (
    CLAMP_HI,
    CLAMP_LO,
    P0,
    P1,
    SPRT,
    ConfidenceSequence,
    MixtureSPRT,
    anytime_ci,
    ci_band_stop,
    ci_band_stop_sequence,
    confidence_sequence,
    kernel_selfcheck,
    log_evalue,
    next_images,
    stitched_radius,
)

# 手算基准(推导见各用例;与模块实现同公式的独立笔算/高精度对照):
LN18 = math.log(1.8)          # 0.5877866649021191
LN02 = math.log(0.2)          # -1.6094379124341003
LN19 = math.log(19.0)         # 2.9444389791664403


def llr_paper(p: float) -> float:
    """手算 LLR:直接用 ln(1.8)/ln(0.2) 常数(与模块实现路径独立)。"""
    return p * LN18 + (1 - p) * LN02


def make_score(prob: float, model: str = "ensemble", name: str = "") -> ImageScore:
    """构造一条 ImageScore(ensemble 为主,stub 用于过滤测试)。"""
    tag = name or f"img_{prob}_{model}"
    return ImageScore(
        image=ImageEvidence(
            path=f"/tmp/{tag}.png",
            url=f"https://example.test/{tag}.png",
            source_page="https://example.test/",
        ),
        model=model,
        nsfw_prob=prob,
    )


# ---------------------------------------------------------------------------
# LLR 手算对照(3 组精确值)
# ---------------------------------------------------------------------------


def test_llr_hand_computed_three_groups() -> None:
    """三组手算精确值:LLR(0.5)/LLR(0.9)/LLR(0.97)。

    - LLR(0.5) = 0.5·ln(1.8) + 0.5·ln(0.2) = 0.5·ln(0.36) ≈ -0.5108256238;
    - LLR(0.9) = 0.9·ln(1.8) + 0.1·ln(0.2) ≈ +0.3680642072;
    - LLR(0.97)= 0.97·ln(1.8) + 0.03·ln(0.2) ≈ +0.5218699276。
    """
    s = SPRT()
    assert s.llr(0.5) == pytest.approx(-0.5108256237659907)
    assert s.llr(0.9) == pytest.approx(0.36806420716849714)
    assert s.llr(0.97) == pytest.approx(0.5218699275820324)
    # 与独立笔算路径逐点一致
    for p in (0.5, 0.9, 0.97):
        assert s.llr(p) == pytest.approx(llr_paper(p))


def test_llr_hand_computed_evidence_extremes() -> None:
    """极端证据手算:LLR(0.02) ≈ -1.5654934209、LLR(0.99) ≈ +0.5658144191。

    H1 点(p=0.9)给正证据、H0 点(p=0.5)给负证据是该参数化的固有形状:
    想给 H1 攒证据,p 必须明显高于零点 ≈0.73。
    """
    s = SPRT()
    assert s.llr(0.02) == pytest.approx(-1.565493420887376)
    assert s.llr(0.99) == pytest.approx(0.5658144191287569)
    assert s.llr(0.9) > 0.0
    assert s.llr(0.5) < 0.0


# ---------------------------------------------------------------------------
# 边界 A/B 与含等值判决
# ---------------------------------------------------------------------------


def test_boundaries_default_and_custom() -> None:
    """默认 α=β=0.05:A=ln(19)≈2.9444、B=-ln(19);自定义 (0.1,0.2):A=ln(8)、B=ln(2/9)。"""
    s = SPRT()
    assert s.upper == pytest.approx(LN19)
    assert s.lower == pytest.approx(-LN19)
    c = SPRT(0.1, 0.2)
    assert c.upper == pytest.approx(math.log(8.0))      # ln((1-0.2)/0.1)
    assert c.lower == pytest.approx(math.log(2.0 / 9.0))  # ln(0.2/(1-0.1))
    assert c.alpha == 0.1 and c.beta == 0.2


def test_boundary_decision_inclusive_equality() -> None:
    """边界判决含等值:total_llr 恰等于 A → nsfw、恰等于 B → clean。

    白盒把 total_llr 精确赋到边界上(浮点可精确达成),验证 ≥/≤ 含等值;
    再退 1 个 ulp 必须仍为 continue(严格内侧不停)。
    """
    s = SPRT()
    s.total_llr = s.upper
    assert s._decide() == "nsfw"
    s.total_llr = math.nextafter(s.upper, -math.inf)
    assert s._decide() == "continue"
    s.total_llr = s.lower
    assert s._decide() == "clean"
    s.total_llr = math.nextafter(s.lower, math.inf)
    assert s._decide() == "continue"


# ---------------------------------------------------------------------------
# 早停 / 摇摆 / 吸收态
# ---------------------------------------------------------------------------


def test_high_confidence_nsfw_early_stop() -> None:
    """全 0.97 序列:第 6 张停 nsfw(5 张时累计 2.6093 < A,6 张 3.1312 ≥ A)。

    手算:每张 LLR(0.97)≈0.52187;
    5×0.52187=2.60935 < ln(19)=2.94444 < 6×0.52187=3.13122。
    另注:单张证据上限 = ln(1.8)≈0.58789(p→1 时),故 p0=0.5/p1=0.9/
    α=β=0.05 参数化下任何序列判 nsfw 最少 6 张(ceil(ln19/ln1.8))。
    """
    verdict, n_used = SPRT().decide_sequence([0.97] * 20)
    assert verdict == "nsfw"
    assert n_used == 6
    assert n_used < 20  # 早停:省下 14 张送审
    s = SPRT()
    assert s.decide_sequence([0.97] * 5) == ("continue", 5)
    assert s.total_llr == pytest.approx(2.609349637910162)


def test_high_confidence_clean_early_stop() -> None:
    """全 0.02 序列:第 2 张停 clean(1 张 -1.5655 > B,2 张 -3.1310 ≤ B)。"""
    verdict, n_used = SPRT().decide_sequence([0.02] * 20)
    assert verdict == "clean"
    assert n_used == 2
    s = SPRT()
    assert s.decide_sequence([0.02]) == ("continue", 1)
    assert s.total_llr == pytest.approx(-1.565493420887376)


def test_swinging_sequence_uses_full_budget() -> None:
    """摇摆序列(0.99/0.5 交替×10):LLR 逐张上下振荡但始终在 (B, A) 内。

    每对净证据 = LLR(0.99)+LLR(0.5) ≈ 0.5658-0.5108 = +0.0550,
    20 张累计仅 ≈0.5499,离两侧边界都远 → 用满 20 张仍 continue。
    """
    probs = [0.99, 0.5] * 10
    verdict, n_used = SPRT().decide_sequence(probs)
    assert verdict == "continue"
    assert n_used == 20  # 用满:摇摆证据不足以早停
    s = SPRT()
    s.decide_sequence(probs)
    assert s.total_llr == pytest.approx(0.5498879536276613)


def test_all_half_stops_clean_at_six() -> None:
    """全 0.5(最不确定)序列:证据恒为 LLR(0.5)≈-0.5108,第 6 张停 clean。

    5×(-0.51083)=-2.55413 > B=-2.94444;6×=-3.06495 ≤ B。
    """
    verdict, n_used = SPRT().decide_sequence([0.5] * 20)
    assert (verdict, n_used) == ("clean", 6)


def test_absorbing_state() -> None:
    """吸收态:终判后继续 update 直接返回终态,n / total_llr 均冻结。

    decide_sequence 对已吸收实例返回 (终态, 0),不再消耗任何样本。
    """
    s = SPRT()
    assert s.update(0.02) == "continue"
    assert s.update(0.02) == "clean"
    frozen_llr, frozen_n = s.total_llr, s.n
    for _ in range(10):
        assert s.update(0.99) == "clean"  # 吸收:反方向证据也改不了终态
    assert s.n == frozen_n == 2
    assert s.total_llr == frozen_llr
    assert frozen_llr == pytest.approx(-3.130986841774752)  # 2×LLR(0.02)
    assert s.decide_sequence([0.99] * 50) == ("clean", 0)


# ---------------------------------------------------------------------------
# clamp 与非法输入
# ---------------------------------------------------------------------------


def test_clamp_bounds() -> None:
    """clamp [0.01, 0.99]:0/负值/越界与 0.01 等价,1/超大与 0.99 等价。"""
    assert CLAMP_LO == 0.01 and CLAMP_HI == 0.99
    s = SPRT()
    assert s.llr(0.0) == s.llr(0.01) == s.llr(-5.0)
    assert s.llr(1.0) == s.llr(0.99) == s.llr(7.5)
    a, b = SPRT(), SPRT()
    a.update(0.0)
    b.update(0.01)
    assert a.total_llr == b.total_llr
    c, d = SPRT(), SPRT()
    c.update(1.0)
    d.update(0.99)
    assert c.total_llr == d.total_llr


def test_invalid_inputs_raise() -> None:
    """非法输入中文 ValueError:非数值 / NaN 概率;α/β ∉(0,0.5);p0≥p1 或出界。"""
    s = SPRT()
    for bad in ("abc", None, object()):
        with pytest.raises(ValueError, match="必须是数字"):
            s.update(bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="NaN"):
        s.update(float("nan"))
    for alpha, beta in ((0.0, 0.05), (0.5, 0.05), (0.05, 0.6), (-0.1, 0.05)):
        with pytest.raises(ValueError, match="错误率"):
            SPRT(alpha, beta)
    with pytest.raises(ValueError, match="p0/p1"):
        SPRT(0.05, 0.05, p0=0.9, p1=0.9)
    with pytest.raises(ValueError, match="p0/p1"):
        SPRT(0.05, 0.05, p0=0.0, p1=0.9)
    with pytest.raises(ValueError, match="p0/p1"):
        SPRT(0.05, 0.05, p0=0.8, p1=0.3)


def test_llr_monotone_and_neutral_point() -> None:
    """LLR 随 p 严格单调递增;零点在 0.73 与 0.74 之间(p≈0.7324)。"""
    s = SPRT()
    grid = [i / 100 for i in range(1, 100)]  # 0.01..0.99
    values = [s.llr(p) for p in grid]
    assert all(x < y for x, y in zip(values, values[1:]))
    assert s.llr(0.73) < 0.0 < s.llr(0.74)


# ---------------------------------------------------------------------------
# decide_sequence 杂项与状态跟踪
# ---------------------------------------------------------------------------


def test_decide_sequence_empty_and_generator() -> None:
    """空序列 → ("continue", 0);生成器输入照常逐个消费(第 6 张停)。"""
    assert SPRT().decide_sequence([]) == ("continue", 0)
    verdict, n_used = SPRT().decide_sequence(p for p in [0.97] * 6)
    assert (verdict, n_used) == ("nsfw", 6)


def test_state_and_n_tracking() -> None:
    """update 返回值恒在三种状态内;n 逐张 +1;state() 与最近返回一致。"""
    s = SPRT()
    for i, p in enumerate([0.5, 0.62, 0.75, 0.02, 0.02], start=1):
        out = s.update(p)
        assert out in ("continue", "nsfw", "clean")
        assert s.n == i
        assert s.state() == out
    assert s.state() in ("continue", "nsfw", "clean")


# ---------------------------------------------------------------------------
# next_images:排序 / 截断 / 预算 / cfg 接线
# ---------------------------------------------------------------------------


def test_next_images_ordering_and_truncation() -> None:
    """不确定度升序送审 + 判停截断:rank_for_vlm 语义(ensemble、|p-0.5| 升序)。

    候选(ensemble):0.99, 0.52, 0.02, 0.48, 0.02, 0.02 → 送审序
    0.52, 0.48, 0.02, 0.02, 0.02, 0.99(并列按原顺序稳定排)。
    累计 LLR:-0.4669 → -1.0217 → -2.5871 → -4.1526 ≤ B ⇒ 第 4 张停 clean;
    第 5 张 0.02 与高置信 0.99 均不再送审(省预算)。
    """
    candidates = [
        make_score(0.99, name="high"),
        make_score(0.52, name="a"),
        make_score(0.02, name="b1"),
        make_score(0.48, name="b"),
        make_score(0.02, name="b2"),
        make_score(0.02, name="b3"),
    ]
    sent, s = next_images(candidates, Config())
    assert [sc.nsfw_prob for sc in sent] == [0.52, 0.48, 0.02, 0.02]
    assert s.state() == "clean"
    assert s.n == 4
    assert len(sent) < len(candidates)
    # 返回的是入参对象引用(rank_for_vlm 语义),且高置信图从未被送审
    assert sent[0] is candidates[1] and sent[1] is candidates[3]
    assert all(sc is not candidates[0] for sc in sent)
    # 中途累计值手算对照
    assert s.total_llr == pytest.approx(-4.152638089306734)


def test_next_images_budget_cap_and_max_send() -> None:
    """预算上限:默认 cfg.vlm_max_images_per_site=8;max_send 覆盖;≤0 全不送。

    候选全为 0.76(零点 ≈0.7324 之上,单张 LLR≈+0.0605):送审序即 20 张
    0.76,累计 8×0.0605≈0.484 远未到 A ⇒ 不早停,取满默认预算 8 张。
    """
    candidates = [make_score(0.76, name=f"w{i}") for i in range(20)]
    sent, s = next_images(candidates, Config())
    assert len(sent) == 8  # 不判停 → 取满默认预算 8
    assert s.state() == "continue"
    assert s.n == 8
    assert s.total_llr == pytest.approx(8 * 0.06045276634342645)
    sent3, s3 = next_images(candidates, Config(), max_send=3)
    assert [sc.nsfw_prob for sc in sent3] == [0.76, 0.76, 0.76]
    assert s3.n == 3
    sent0, s0 = next_images(candidates, Config(), max_send=0)
    assert sent0 == [] and s0.n == 0 and s0.state() == "continue"


def test_next_images_uses_cfg_alpha_beta() -> None:
    """cfg 接线:alpha/beta 取自 cfg(0.2/0.2 → B=ln(0.25)≈-1.3863)。

    LLR(0.02)≈-1.5655 ≤ B ⇒ 第 1 张即停 clean(默认 0.05/0.05 时需 2 张)。
    """
    cfg = Config(sprt_alpha=0.2, sprt_beta=0.2)
    candidates = [make_score(0.02, name=f"c{i}") for i in range(5)]
    sent, s = next_images(candidates, cfg)
    assert len(sent) == 1
    assert s.state() == "clean"
    assert s.alpha == 0.2 and s.beta == 0.2
    assert s.upper == pytest.approx(math.log(4.0))
    assert s.lower == pytest.approx(-math.log(4.0))


def test_next_images_empty_and_non_ensemble() -> None:
    """空输入 / 无 ensemble 条目 → ([], 全新 SPRT);stub 条目不参与排序送审。"""
    sent, s = next_images([], Config())
    assert sent == [] and s.n == 0 and s.state() == "continue"
    stubs = [make_score(0.5, model="stub", name=f"st{i}") for i in range(3)]
    sent2, s2 = next_images(stubs, Config())
    assert sent2 == [] and s2.n == 0
    mixed = stubs + [make_score(0.02, name="ens")]
    sent3, s3 = next_images(mixed, Config())
    assert len(sent3) == 1 and sent3[0].model == "ensemble"
    assert all(sc.model == "ensemble" for sc in sent3)


# ---------------------------------------------------------------------------
# 常量可调 / 文档锁定 / 自检 / 基准
# ---------------------------------------------------------------------------


def test_adjustable_constants_two_image_stop() -> None:
    """常量可调:对称假设 p0=0.1/p1=0.9 下每张证据 = (2p-1)·ln(9)。

    LLR(0.97)=0.94·ln9≈2.0654:1 张 2.0654 < A=ln19,2 张 4.1308 ≥ A
    ⇒ 全 0.97 第 2 张停 nsfw;全 0.02(每张 -2.1093)第 2 张停 clean。
    构造覆盖不影响模块全局默认 P0/P1。
    """
    s = SPRT(0.05, 0.05, p0=0.1, p1=0.9)
    assert s.llr(0.97) == pytest.approx(0.94 * math.log(9.0))
    assert s.decide_sequence([0.97] * 20) == ("nsfw", 2)
    assert SPRT(0.05, 0.05, p0=0.1, p1=0.9).decide_sequence([0.02] * 20) == (
        "clean",
        2,
    )
    assert (P0, P1) == (0.5, 0.9)  # 模块全局常量未被覆盖污染


def test_calibration_assumption_documented() -> None:
    """校准假设必须写在 docstring:校准概率 / use_sprt 默认关 / kernel_wire 装配。"""
    mod_doc = sprt_mod.__doc__ or ""
    assert "校准" in mod_doc and "use_sprt" in mod_doc and "kernel_wire" in mod_doc
    assert "校准" in (SPRT.__doc__ or "")
    assert "校准" in (next_images.__doc__ or "") and "演示" in (next_images.__doc__ or "")
    assert Config().use_sprt is False  # 默认关:旧调用方零感知(红线 29)


def test_kernel_selfcheck_shape() -> None:
    """kernel_selfcheck 返回 A138 约定字段,value(早停送审数)< baseline。"""
    report = kernel_selfcheck()
    assert report["name"] == "sprt"
    assert {"name", "metric", "value", "baseline"} <= set(report)
    assert report["value"] == 2 < report["baseline"] == 20
    assert report["verdict"] == "clean"
    assert kernel_selfcheck() == report  # 确定性:两次调用完全一致


def test_v7_bench_early_stop() -> None:
    """V7 基准(红线 31):20 张序列第 4 张停,N<20,操作计数断言零墙钟。

    序列 [0.52, 0.48] + [0.02]×18:累计 LLR -0.4669 → -1.0217 →
    -2.5871 → -4.1526 ≤ B ⇒ N=4;无 SPRT 基线需送满 20 张,
    省 16 张送审(80% 配额)。next_images(max_send=20)同序截断至 4。
    """
    probs = [0.52, 0.48] + [0.02] * 18
    verdict, n_used = SPRT().decide_sequence(probs)
    assert verdict == "clean"
    assert n_used == 4
    assert n_used < 20
    assert 20 - n_used == 16  # 省下的 VLM 送审张数

    candidates = [make_score(p, name=f"b{i}") for i, p in enumerate(probs)]
    sent, s = next_images(candidates, Config(), max_send=20)
    assert len(sent) == n_used == s.n
    assert s.state() == "clean"
    # 未送审的 16 张从未被消耗
    assert all(sc not in sent for sc in candidates[4:])


# ===========================================================================
# A160+ e-process / anytime 内核:MixtureSPRT(混合 SPRT)
# 对标 Ramdas et al. 2023(safe anytime-valid inference)与 Robbins
# mixture-SPRT:E_n 是 H0 下期望 1 的非负上鞅,Ville ⇒ P(∃n: E_n≥1/α)≤α。
# ===========================================================================


def test_mixture_log_evalue_closed_form_exact() -> None:
    """闭式 e-值精确对照:E₁=1(先验均值=p0 时)、E₂(s=0)=1.5。

    - n=1、Jeffreys(a=b=0.5)、p0=0.5:预测分布 m(1)=a/(a+b)=0.5=p0,
      故 E₁=1(x=0/1 皆然);Beta(3,1) 与 p0=0.75 同理(log≈0,浮点尾差);
    - n=2、s=0、Jeffreys、p0=0.5:m=B(0.5,2.5)/B(0.5,0.5)=0.375,
      原似然 0.25 ⇒ E=1.5,log E=ln(1.5)≈0.4054651081;
    - n=5、s=3:与测试内独立 lgamma 链(另写一遍 lnB 公式)逐位一致。
    """
    assert log_evalue(1, 1) == pytest.approx(0.0, abs=1e-12)
    assert log_evalue(0, 1) == pytest.approx(0.0, abs=1e-12)
    assert log_evalue(1, 1, p0=0.75, prior_a=3, prior_b=1) == pytest.approx(
        0.0, abs=1e-12
    )
    assert log_evalue(0, 1, p0=0.75, prior_a=3, prior_b=1) == pytest.approx(
        0.0, abs=1e-12
    )
    assert log_evalue(0, 2) == pytest.approx(math.log(1.5))
    assert log_evalue(2, 2) == pytest.approx(math.log(1.5))  # Jeffreys 对称
    # 独立重推:log E = lnB(a+s, b+n-s) − lnB(a,b) − s·ln p0 − (n−s)·ln(1−p0)
    a, b, p0, s, n = 0.5, 0.5, 0.5, 3.0, 5
    lnb_num = (
        math.lgamma(a + s) + math.lgamma(b + n - s) - math.lgamma(a + b + n)
    )
    lnb_prior = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    expect = (
        lnb_num - lnb_prior - s * math.log(p0) - (n - s) * math.log1p(-p0)
    )
    assert log_evalue(s, n) == pytest.approx(expect, rel=1e-13)
    # 先验均值≠p0 时 E₁ 期望仍为 1,但单点不再恒 1(区分于上组)
    assert log_evalue(1, 1, p0=0.5, prior_a=3, prior_b=1) == pytest.approx(
        math.log(0.75 / 0.5)
    )
    with pytest.raises(ValueError, match="p0"):
        log_evalue(0, 2, p0=0.0)


def test_mixture_martingale_exact_enumeration() -> None:
    """鞅性质(确定性):全路径枚举 E_{H0}[E_n] = 1,精确到 1e-9。

    对 n=1,2,3,5 枚举全部 2^n 条硬标签路径,按 H0(p=p0)概率加权平均
    E_n——任何 Beta(a,b) 混合下每个 θ 的似然比都是期望 1 的鞅,Fubini 积
    分后仍鞅,故均值必须**恒等** 1(非近似)。覆盖 5 组 (p0, 先验) 参数。
    """
    combos = [
        (0.5, 0.5, 0.5),  # Jeffreys
        (0.5, 3.0, 1.0),  # 偏 nsfw 的混合
        (0.75, 3.0, 1.0),
        (0.5, 2.0, 2.0),
        (0.3, 4.0, 2.0),
    ]
    for p0, a, b in combos:
        for n in (1, 2, 3, 5):
            total = 0.0
            for mask in range(2**n):
                hits = bin(mask).count("1")
                h0_prob = (p0**hits) * ((1.0 - p0) ** (n - hits))
                total += h0_prob * math.exp(
                    log_evalue(hits, n, p0=p0, prior_a=a, prior_b=b)
                )
            assert total == pytest.approx(1.0, abs=1e-9), (p0, a, b, n)


def test_mixture_martingale_simulation_fixed_seed() -> None:
    """鞅性质(模拟):H0 真值下 E[E_n]≈1、E[log E_n]<0(Jensen)。

    固定种子 555、N=4000 条 n=20 的硬标签路径,Beta(2,2) 混合(方差小、
    均值估计稳;重尾的 Jeffreys 长程均值在有限样本下波动大,故模拟用紧
    先验,精确性已由枚举测试覆盖)。观测均值 0.899,断言带 [0.8, 1.2]。
    """
    rng = random.Random(555)
    n_paths, horizon = 4000, 20
    log_es = []
    for _ in range(n_paths):
        hits = sum(1 for _ in range(horizon) if rng.random() < 0.5)
        log_es.append(log_evalue(hits, horizon, prior_a=2.0, prior_b=2.0))
    mean_e = sum(math.exp(min(x, 700.0)) for x in log_es) / n_paths
    mean_log = sum(log_es) / n_paths
    assert 0.8 <= mean_e <= 1.2  # E[E_n]=1(鞅),采样波动带宽
    assert mean_log < 0.0  # E[log E_n] ≤ log E[E_n]=0(Jensen 严格)


def test_mixture_ville_anytime_crossing_simulation() -> None:
    """Ville anytime 担保(模拟):P(∃n≤40: E_n ≥ 1/α) ≤ α+松弛。

    固定种子 777、M=3000 条 H0 路径、逐 n 追踪 E_n 是否越过 1/0.05=20
    (即 MixtureSPRT 的拒 H0 阈值)。理论 ≤0.05,观测 0.0273;断言 ≤0.08
    (宽松带,容纳模拟噪声);≥0.005 保证本测试有牙(并非永不越阈)。
    """
    rng = random.Random(777)
    threshold = math.log(1.0 / 0.05)
    crossings = 0
    for _ in range(3000):
        s = 0.0
        crossed = False
        for n in range(1, 41):
            s += 1.0 if rng.random() < 0.5 else 0.0
            if log_evalue(s, n) >= threshold:
                crossed = True
                break
        crossings += crossed
    rate = crossings / 3000
    assert 0.005 <= rate <= 0.08


def test_mixture_false_verdict_rate_under_null() -> None:
    """anytime 误判率(模拟):H0 公平硬币下 MixtureSPRT 终判率 ≤ α+松弛。

    固定种子 31337、M=3000 条路径(每条至多 40 张,判停即断):任何时刻
    出 nsfw/clean 终判都是错误事件,anytime 担保其比例 ≤ α=0.05;观测
    0.0263,断言 ≤ 0.08。
    """
    rng = random.Random(31337)
    wrong = 0
    for _ in range(3000):
        m = MixtureSPRT(0.05)
        state = "continue"
        for _ in range(40):
            state = m.update(1.0 if rng.random() < 0.5 else 0.0)
            if state != "continue":
                break
        wrong += state != "continue"
    assert wrong / 3000 <= 0.08


def test_mixture_decisions_direction_and_symmetry() -> None:
    """三态判决与方向:强 nsfw/clean 流各第 9 张停;p0=0.5+Jeffreys 对称。

    [0.95]×60:E₉=log(20.99)≥ln20 ⇒ nsfw;[0.05]×60 完全对称(先验对称)
    ⇒ 同样第 9 张停 clean;全 0.5(恰为 H0)200 张不判停且 E_n<1
    (混合先验在原假设路径上只亏不赚——Jensen 亏损)。
    """
    m = MixtureSPRT(0.05)
    assert m.upper == pytest.approx(math.log(20.0))  # ln(1/α)
    assert m.decide_sequence([0.95] * 60) == ("nsfw", 9)
    assert m.s == pytest.approx(9 * 0.95) and m.n == 9
    assert m.s / m.n > m.p0  # 方向读出:运行均值在 p0 上方 ⇒ nsfw
    m2 = MixtureSPRT(0.05)
    assert m2.decide_sequence([0.05] * 60) == ("clean", 9)  # 对称早停
    assert m2.total_log_e == pytest.approx(m.total_log_e)  # 对称:log E 相同
    m3 = MixtureSPRT(0.05)
    assert m3.decide_sequence([0.5] * 200) == ("continue", 200)  # 原假设不拒
    assert 0.0 < m3.evalue() < 1.0


def test_mixture_update_matches_closed_form_and_clamp() -> None:
    """update 路径 = 闭式重算(无增量漂移);clamp 与 Wald SPRT 同规。"""
    m = MixtureSPRT(0.05)
    for p in (0.97, 0.97, 0.97):
        m.update(p)
    assert m.total_log_e == pytest.approx(log_evalue(3 * 0.97, 3))
    lo_pair = (MixtureSPRT(0.05), MixtureSPRT(0.05))
    lo_pair[0].update(0.0)
    lo_pair[1].update(0.01)
    assert lo_pair[0].total_log_e == lo_pair[1].total_log_e
    hi_pair = (MixtureSPRT(0.05), MixtureSPRT(0.05))
    hi_pair[0].update(1.0)
    hi_pair[1].update(0.99)
    assert hi_pair[0].total_log_e == hi_pair[1].total_log_e
    m4 = MixtureSPRT(0.05)
    for bad in ("abc", None, object()):
        with pytest.raises(ValueError, match="必须是数字"):
            m4.update(bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="NaN"):
        m4.update(float("nan"))


def test_mixture_absorbing_state() -> None:
    """吸收态:终判后 n/s/total_log_e 全冻结,decide_sequence 不再消耗。"""
    m = MixtureSPRT(0.05)
    assert m.decide_sequence([0.05] * 9) == ("clean", 9)
    frozen = (m.n, m.s, m.total_log_e, m.state())
    for _ in range(10):
        assert m.update(0.99) == "clean"  # 反方向证据改不了终态
    assert (m.n, m.s, m.total_log_e, m.state()) == frozen
    assert m.decide_sequence([0.99] * 50) == ("clean", 0)
    assert m.evalue() == pytest.approx(math.exp(m.total_log_e))


def test_mixture_validation_errors() -> None:
    """非法参数中文 ValueError:α/p0/先验越界;log_evalue 的 s/n 越界。"""
    for alpha in (0.0, 0.5, 0.7, -0.1):
        with pytest.raises(ValueError, match="alpha"):
            MixtureSPRT(alpha)
    for p0 in (0.0, 1.0, -0.2, 1.5):
        with pytest.raises(ValueError, match="p0"):
            MixtureSPRT(0.05, p0=p0)
    for a, b in ((0.0, 0.5), (0.5, -1.0), (float("nan"), 0.5)):
        with pytest.raises(ValueError, match="prior"):
            MixtureSPRT(0.05, prior_a=a, prior_b=b)
    with pytest.raises(ValueError, match="无效"):
        log_evalue(3.0, 2)  # s > n
    with pytest.raises(ValueError, match="无效"):
        log_evalue(-0.5, 2)  # s < 0
    with pytest.raises(ValueError, match="无效"):
        log_evalue(0.0, -1)  # n < 0
    with pytest.raises(ValueError, match="必须是数字"):
        log_evalue("x", 2)  # type: ignore[arg-type]


def test_mixture_log_domain_extremes() -> None:
    """对数域数值稳定:极端计数下 log e 有限;exp 溢出守护返回 inf。

    n=2000 全 NSFW:log E≈1381.9(>ln 最大 double≈709.78)仍为有限数
    (lgamma 闭式无幂连乘);evalue() 不抛 OverflowError 而给 inf;
    n=10⁴、s=9900 的 log E≈6366.6 同样有限。 Wald SPRT 的 exp 路径
    在此量级早已炸(对数域累计是 e-process 的核心工程点)。
    """
    assert math.isfinite(log_evalue(2000.0, 2000))
    assert math.isfinite(log_evalue(9900.0, 10000))
    assert log_evalue(9900.0, 10000) == pytest.approx(6366.625071306027)
    assert log_evalue(2000.0, 2000) == pytest.approx(1381.9214824471971)
    m = MixtureSPRT(0.05)
    m.n, m.s = 2000, 2000.0  # 白盒注入极端聚合(与旧测试直改 total_llr 同法)
    m.total_log_e = m.log_evalue()
    assert math.isfinite(m.total_log_e)
    assert m.evalue() == math.inf  # 溢出守护:不抛 OverflowError
    m2 = MixtureSPRT(0.05)
    m2.n, m2.s = 2000, 0.0  # 全 clean 对称极端
    m2.total_log_e = m2.log_evalue()
    assert math.isfinite(m2.total_log_e) and m2.evalue() == math.inf
    assert math.isfinite(log_evalue(100.0, 10000))  # 深负侧也不下溢炸 NaN


# ===========================================================================
# A160+ anytime 置信序列:Howard 线性 stitching(Hoeffding-U 型)
# ===========================================================================


def test_stitched_radius_exact_values_and_epochs() -> None:
    """半径精确值与分段行为:手算 4 点 + 段内单调 + 有界性。

    - ε(1)=sqrt(ln(4·1/(δ/2))/2)=sqrt(ln(160)/2)≈1.5930(k=0 段);
    - ε(2)=sqrt(ln(4·2/(δ/4))/4)=sqrt(ln(640)/4)≈1.2710;
    - ε(4)≈0.9904(k=2 段)、ε(1000)≈0.0973(k=9 段,m=1024);
    - 同段内(n=5..8 同属 m=8 段)ε∝1/√n 递减;跨段 4→5 不升(本参数下);
    - n=1..5000 × δ∈{0.01,0.05,0.2}:ε 有限且 ≤2(宽度有工程上界)。
    """
    assert stitched_radius(1) == pytest.approx(math.sqrt(math.log(160.0) / 2.0))
    assert stitched_radius(2) == pytest.approx(math.sqrt(math.log(640.0) / 4.0))
    assert stitched_radius(4) == pytest.approx(0.9904394565970204)
    assert stitched_radius(1000) == pytest.approx(0.09730908854375508)
    seg = [stitched_radius(n) for n in range(5, 9)]  # 同段(k=3, m=8)
    assert all(x > y for x, y in zip(seg, seg[1:]))
    assert stitched_radius(5) < stitched_radius(4)  # 跨段不升
    assert stitched_radius(100) < stitched_radius(10) < stitched_radius(2)
    for delta in (0.01, 0.05, 0.2):
        radii = [stitched_radius(n, delta) for n in range(1, 5001)]
        assert all(math.isfinite(r) and 0.0 < r <= 2.0 for r in radii)
    with pytest.raises(ValueError, match="n="):
        stitched_radius(0)
    with pytest.raises(ValueError, match="n="):
        stitched_radius(2.5)
    for delta in (0.0, 1.0, 1.5, -0.1):
        with pytest.raises(ValueError, match="delta"):
            stitched_radius(10, delta)


def test_anytime_ci_shape_clipping_and_order() -> None:
    """anytime CI:中心±半径、[0,1] 钳制、随 s 单调、n 增大收紧。"""
    lo, hi = anytime_ci(10, 10.0)  # 全 NSFW:上端钳到 1
    r = stitched_radius(10, 0.05)
    assert hi == 1.0
    assert lo == pytest.approx(1.0 - r)
    lo0, hi0 = anytime_ci(10, 0.0)  # 全 clean:下端钳到 0
    assert lo0 == 0.0 and hi0 == pytest.approx(r)
    mid_lo, mid_hi = anytime_ci(100, 50.0)
    assert (mid_lo + mid_hi) / 2 == pytest.approx(0.5)  # 中心=运行均值
    assert mid_hi - mid_lo == pytest.approx(2 * stitched_radius(100))
    assert mid_hi - mid_lo < hi0 - lo0  # n 大 ⇒ 区间更窄
    a = anytime_ci(30, 15.0)
    b = anytime_ci(30, 21.0)
    assert b[0] > a[0] and b[1] > a[1]  # s 升 ⇒ 区间整体右移(单调)
    with pytest.raises(ValueError, match="s="):
        anytime_ci(10, 10.5)  # s > n
    with pytest.raises(ValueError, match="s="):
        anytime_ci(10, -0.1)
    with pytest.raises(ValueError, match="delta"):
        anytime_ci(10, 5.0, 0.0)


def test_confidence_sequence_stream_and_batch_equal() -> None:
    """流式 ConfidenceSequence 与批式 confidence_sequence 逐步一致。

    含:钳制一致性(0/1 与 0.01/0.99 同区间)、n=0 无信息态
    ((0,1)、半径 inf、mean 抛错)、区间随样本数不增宽。
    """
    probs = [0.9, 0.1, 0.0, 1.0, 0.5, 0.33]
    batch = confidence_sequence(probs)
    assert len(batch) == len(probs)
    cs = ConfidenceSequence()
    assert cs.interval() == (0.0, 1.0)  # n=0:无信息
    assert cs.radius() == math.inf
    with pytest.raises(ValueError, match="尚无样本"):
        cs.mean()
    streamed = [cs.update(p) for p in probs]
    assert streamed == batch
    assert cs.n == 6 and cs.sum == pytest.approx(0.9 + 0.1 + 0.01 + 0.99 + 0.5 + 0.33)
    assert cs.mean() == pytest.approx(cs.sum / 6)
    # clamp 一致:边界值与钳制值产生同一区间
    cs_a = ConfidenceSequence()
    cs_b = ConfidenceSequence()
    cs_a.update(0.0)
    cs_b.update(0.01)
    assert cs_a.interval() == cs_b.interval()
    # 宽度非增:区间受 [0,1] 钳制时同宽,改验未钳制半径收紧(ε∝1/√n)
    widths = [hi - lo for _, (lo, hi) in enumerate(batch)]
    assert all(w <= 2.0 for w in widths)
    assert cs.radius() == pytest.approx(stitched_radius(6))
    assert stitched_radius(6) < stitched_radius(1)
    with pytest.raises(ValueError, match="delta"):
        ConfidenceSequence(delta=1.5)
    with pytest.raises(ValueError, match="必须是数字"):
        confidence_sequence([0.5, "x"])  # type: ignore[list-item]


def test_ci_anytime_coverage_simulation() -> None:
    """覆盖率模拟:任意时刻(全时段联合)真值在界内比例 ≥ 1-δ-松弛。

    δ=0.1、p∈{0.2,0.8}(固定种子)、M=1500 条路径、逐 n=1..64 联合检查
    真值是否始终落界内——stitching 的核心担保就是**联合**覆盖(盯任意一
    步都不许漏);观测 1.0,断言 ≥0.9(宽松)。另验固定 n=64、δ=0.05、
    p=0.5 的单点覆盖 ≥0.93(观测 1.0)。
    """
    for p, seed in ((0.2, 4242), (0.8, 4250)):
        rng = random.Random(seed)
        covered = 0
        for _ in range(1500):
            s = 0.0
            ok = True
            for n in range(1, 65):
                s += 1.0 if rng.random() < p else 0.0
                lo, hi = anytime_ci(n, s, 0.1)
                if not lo <= p <= hi:
                    ok = False
                    break
            covered += ok
        assert covered / 1500 >= 0.9  # 1-δ-0.1 宽松下限
    rng = random.Random(99)
    hits = 0
    for _ in range(1500):
        s = sum(1.0 for _ in range(64) if rng.random() < 0.5)
        lo, hi = anytime_ci(64, s, 0.05)
        hits += lo <= 0.5 <= hi
    assert hits / 1500 >= 0.93


def test_ci_band_stop_decisions() -> None:
    """决策带判停:纯函数三态 + 序列驱动早停(60 张流第 30 张停)。

    默认退化带 [0.5, 0.5](硬币面阈值):n=30、s=1.5(均值 0.05)时
    hi≈0.497<0.5 ⇒ clean;s=28.5 时 lo≈0.503>0.5 ⇒ nsfw;s=15 横跨
    0.5 ⇒ continue。加宽成 [0.3, 0.7] 后同流恒 continue(灰区不下判);
    序列驱动 mid-stream 截断:n_used=30<60;max_n 限额;空序列零消耗。
    """
    assert ci_band_stop(30, 1.5) == "clean"
    assert ci_band_stop(30, 28.5) == "nsfw"
    assert ci_band_stop(30, 15.0) == "continue"
    assert ci_band_stop(1, 1.0) == "continue"  # n=1 半径≈1.59:必横跨
    # 加宽决策带 [0.3, 0.7]:灰区更宽 ⇒ 判 nsfw 需要更多证据(n=30 时
    # lo≈0.503<0.7 仍 continue;n=200、s=190 时 lo≈0.749>0.7 才 nsfw);
    # 中心 0.5 的流在宽带下恒 continue(灰区不下判)。
    assert ci_band_stop(30, 28.5, clean_below=0.3, nsfw_above=0.7) == "continue"
    assert ci_band_stop(200, 190.0, clean_below=0.3, nsfw_above=0.7) == "nsfw"
    assert ci_band_stop(30, 15.0, clean_below=0.3, nsfw_above=0.7) == "continue"
    assert ci_band_stop_sequence([0.05] * 60) == ("clean", 30)
    assert ci_band_stop_sequence([0.95] * 60) == ("nsfw", 30)
    verdict, used = ci_band_stop_sequence(
        [0.5] * 60, clean_below=0.3, nsfw_above=0.7
    )
    assert (verdict, used) == ("continue", 60)  # 灰区永不判停
    assert ci_band_stop_sequence([0.02] * 10, max_n=4) == ("continue", 4)
    assert ci_band_stop_sequence([]) == ("continue", 0)
    early = ci_band_stop_sequence([0.05] * 60)
    assert early[1] == 30 < 60  # 早停:后 30 张不送审(省预算同 SPRT)


def test_ci_band_stop_validation() -> None:
    """决策带/参数校验:带越界、左>右、坏 δ、n<1、max_n<1 均中文报错。"""
    with pytest.raises(ValueError, match="决策带"):
        ci_band_stop(30, 15.0, clean_below=0.7, nsfw_above=0.3)  # 左>右
    with pytest.raises(ValueError, match="决策带"):
        ci_band_stop(30, 15.0, clean_below=-0.1)
    with pytest.raises(ValueError, match="决策带"):
        ci_band_stop(30, 15.0, nsfw_above=1.2)
    with pytest.raises(ValueError, match="delta"):
        ci_band_stop(30, 15.0, 0.0)
    with pytest.raises(ValueError, match="n="):
        ci_band_stop(0, 0.0)
    with pytest.raises(ValueError, match="n="):
        ci_band_stop_sequence([0.5], max_n=0)


# ===========================================================================
# 向后兼容锁定:Wald SPRT 语义一字未动(A160+ 只做纯增量)
# ===========================================================================


def test_backward_compat_wald_sprt_untouched() -> None:
    """Wald SPRT 原语义锁定:边界/早停/常量/自检与 V7 完全一致。"""
    s = SPRT()
    assert s.upper == pytest.approx(LN19) and s.lower == pytest.approx(-LN19)
    assert SPRT().decide_sequence([0.97] * 20) == ("nsfw", 6)
    assert SPRT().decide_sequence([0.02] * 20) == ("clean", 2)
    assert SPRT().decide_sequence([0.52, 0.48] + [0.02] * 18) == ("clean", 4)
    assert (P0, P1, CLAMP_LO, CLAMP_HI) == (0.5, 0.9, 0.01, 0.99)
    assert kernel_selfcheck()["value"] == 2
    # 旧导出一项不少(__all__ 只增不减),SPRT 仍是 Wald 参数化(带 p1)
    for name in (
        "P0", "P1", "CLAMP_LO", "CLAMP_HI", "STATES", "SPRT",
        "next_images", "kernel_selfcheck",
    ):
        assert name in sprt_mod.__all__
    assert isinstance(SPRT(0.05, 0.05, p0=0.1, p1=0.9), SPRT)
    assert not isinstance(SPRT(), MixtureSPRT)  # 两个内核相互独立
    for name in (
        "MixtureSPRT", "log_evalue", "stitched_radius", "anytime_ci",
        "confidence_sequence", "ConfidenceSequence", "ci_band_stop",
        "ci_band_stop_sequence",
    ):
        assert name in sprt_mod.__all__  # 新内核全部导出


def test_anytime_docs_locked() -> None:
    """文档锁定:e-process/anytime/校准/stitching 关键词必须在 docstring。"""
    mod_doc = sprt_mod.__doc__ or ""
    assert "e-值" in mod_doc and "anytime" in mod_doc and "stitching" in mod_doc
    assert "校准" in (MixtureSPRT.__doc__ or "")
    assert "e-值" in (MixtureSPRT.__doc__ or "")
    assert "anytime" in (ConfidenceSequence.__doc__ or "")
    assert "Hoeffding" in (stitched_radius.__doc__ or "")
    assert "决策带" in (ci_band_stop.__doc__ or "")
