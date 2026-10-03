"""A227/A239 单元测试:预处理随机化平滑认证评测(全程离线,只写 tmp_path)。

覆盖点(数学层纯 stdlib,零 PIL 依赖;管线层依赖 Pillow):
- Φ⁻¹(Acklam + Halley):已知分位点精度(< 1e-9,远严于 1e-4 要求)、
  反对称性、p=0.5 → 0、定义域外 / NaN / bool → ValueError;
- 精确二项尾:闭式边界(k≤0→1、k>n→0、p=0/1 端点)与手算值
  P(Bin(10, 1/2) ≥ 7) = 176/1024;
- Clopper-Pearson 界:k=0 / k=n 闭式((α/2)^(1/n) 与 1−(α/2)^(1/n))、
  根性质(边界代入尾方程 ≈ α/2)、互补恒等式 U(k) = 1 − L(n−k)
  (二分类认证判据退化的数学根源);
- Cohen 认证半径:全票手算对照(半径 = σ/2·2Φ⁻¹(p_lower) = σ·Φ⁻¹(p_lower),
  p_lower = (α/2)^(1/K))、无证书区(k≈K/2 → ABSTAIN、radius=None)、
  半径随 k_top1 单调、σ=0 → 半径恰 0、参数非法 ValueError;
- 噪声 LUT 批量引擎(A239):σ=0 快速路径与参考实现、原图三方逐字节
  一致;σ 超限回退参考路径逐字节一致;同种子字节确定、换种子变化;
  LUT 对称 / 有界 / σ=0 全零;大样本均值≈0、std≈σ(同分布论证);
  参考实现(旧逐像素 gauss 内核)自身确定性与敏感性;加速比 ≥5×
  (固定图固定种子计时对照,宽松下界防 CI 抖动);
- 平滑分类器(基分类器注入):stub 全票(K 票同值,stub 按文件名)→
  k_top1=K + 闭式半径;构造分裂票分类器(按变体序号 j 给分)→ 多数
  投票计数精确、9/10 认证 / 5/5 平票 ABSTAIN(保守判 clean);σ=0 退化
  = 基分类器(全部变体字节一致,平滑预测 ≡ 基预测);同种子变体字节
  确定、换种子变体确实变化;构造参数非法 / PIL 屏蔽 → 中文异常;
- 合成语料:同 (count, seed) 逐字节一致、文件名即金标、count 非法
  ValueError、PIL 屏蔽 → CertifyError;
- run() 全链:labels.json 标注语料(stub)→ 认证率可手算(k=K 全票,
  认证准确率 = 正确预测占比,borderline 计负类);混合场景(nsfw_hi 全票
  认证 / nsfw_mid 平票 ABSTAIN / normal 全票认证)→ 认证准确率 3/4、
  ABSTAIN 计数与半径分布精确;语料目录只读不被写入;报告结构(JSON
  schema / stats / details / method 口径段)与 markdown 关键段;
  确定性:同种子两次运行 JSON+MD 逐字节一致(报告无时间戳);
- 半径金标门禁(A239):指纹键 = 分类器+语料+σ 网格+K+α+统计版本
  (逐分量敏感);容差闭式 max(10%·|基线|, 0.02);金标往返(重建 →
  门禁通过,重建幂等无时间戳);构造回归违例(超容差 / None↔数值互变 /
  指标缺基线)→ CLI 退出码 2;坏金标文件(坏 JSON / 版本不兼容 / 结构
  非法)→ 退出码 1;指纹敏感(换 K / α → 缺基线:warn 跳过 0 / fail 2);
  evaluate_gate 单元对照;
- CLI:成功路径退出码 0 + stdout 摘要;可预期错误(分类器未注册 /
  语料缺 labels.json / glm 未配置 / PIL 屏蔽 / σ 网格含负 / k=0)→
  退出码 1 + 中文 stderr。
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

import pytest

from benchmarks.certify_bench import (
    DEFAULT_ALPHA,
    DEFAULT_SEED,
    FAST_SIGMA_MAX,
    CertifyError,
    CertifyGoldenError,
    binom_upper_tail,
    build_golden_entry,
    certified_radius,
    clopper_pearson_bounds,
    evaluate_gate,
    load_golden,
    main,
    make_synthetic_corpus,
    metric_tolerance,
    norm_ppf,
    run,
    save_golden,
    variant_seed,
    SmoothedClassifier,
    _fingerprint_digest,
    _load_pil,
    _noise_lut,
    _noisy_gray,
    _noisy_gray_reference,
)
from netsentinel.contracts import Config, ImageEvidence, ImageScore
from netsentinel.vision.stub_classifier import StubClassifier
from benchmarks.run_benchmark import write_png

# 噪声变体 / 全链评测依赖 Pillow;环境缺失时整文件跳出
# (数学层测试随之跳过,与 test_adversarial 的 importorskip 惯例一致)。
PIL = pytest.importorskip("PIL", reason="需要 Pillow 才能测试平滑认证噪声变体")
from PIL import Image as PILImage  # noqa: E402

# ---------------------------------------------------------------------------
# Φ⁻¹(数学层)
# ---------------------------------------------------------------------------


def test_norm_ppf_known_quantiles() -> None:
    """已知分位点精度:Acklam+Halley 实测 < 1e-9(锁定值,远严于 1e-4)。"""
    known = {
        0.5: 0.0,
        0.975: 1.959963984540054,
        0.995: 2.5758293035489004,
        0.001: -3.090232306167813,
        0.8413447460685429: 1.0,   # Φ(1) 的反函数
        0.0001: -3.719016485455709,
    }
    for p, expected in known.items():
        assert norm_ppf(p) == pytest.approx(expected, abs=1e-9), f"p={p}"


def test_norm_ppf_precision_far_below_1e4_line() -> None:
    """任务要求的精度线:全区间(含深尾)与已知分位点差 < 1e-4 的冗余断言。"""
    for p, expected in ((0.975, 1.959963984540054), (0.001, -3.090232306167813)):
        assert abs(norm_ppf(p) - expected) < 1e-4
    assert norm_ppf(0.5) == 0.0


def test_norm_ppf_antisymmetry() -> None:
    """反对称性 Φ⁻¹(1−p) = −Φ⁻¹(p)(半径闭式 σ/2·2Φ⁻¹(p_lower) 依赖它)。"""
    for p in (0.01, 0.1, 0.30849710781899997, 0.691502892181, 0.975):
        assert norm_ppf(1.0 - p) == pytest.approx(-norm_ppf(p), abs=1e-9)


def test_norm_ppf_domain_errors() -> None:
    """定义域 (0,1) 之外 / NaN / 非数值 → ValueError(中文)。"""
    for bad in (0.0, 1.0, -0.1, 1.5, float("nan")):
        with pytest.raises(ValueError, match="norm_ppf"):
            norm_ppf(bad)
    with pytest.raises(ValueError):
        norm_ppf(True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        norm_ppf("0.5")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 精确二项尾与 Clopper-Pearson 界(数学层)
# ---------------------------------------------------------------------------


def test_binom_upper_tail_closed_edges() -> None:
    """闭式边界:k≤0 → 1;k>n → 0;p=0(k≥1)→ 0;p=1 → 1;k=n → p^n。"""
    assert binom_upper_tail(0, 10, 0.3) == 1.0
    assert binom_upper_tail(-1, 10, 0.3) == 1.0
    assert binom_upper_tail(11, 10, 0.3) == 0.0
    assert binom_upper_tail(3, 10, 0.0) == 0.0
    assert binom_upper_tail(3, 10, 1.0) == 1.0
    assert binom_upper_tail(10, 10, 0.7) == pytest.approx(0.7**10)


def test_binom_upper_tail_hand_value() -> None:
    """手算对照:P(Bin(10, 1/2) ≥ 7) = (120+45+10+1)/1024 = 0.171875。"""
    assert binom_upper_tail(7, 10, 0.5) == pytest.approx(176 / 1024, abs=1e-15)


def test_clopper_pearson_closed_forms() -> None:
    """k=0 / k=n 的闭式:下界 (α/2)^(1/n)、上界 1−(α/2)^(1/n)(手算锁定)。"""
    alpha = 0.05
    tail = alpha / 2.0
    lo, hi = clopper_pearson_bounds(0, 10, alpha)
    assert lo == 0.0
    assert hi == pytest.approx(1.0 - tail ** 0.1, abs=1e-12)
    lo, hi = clopper_pearson_bounds(10, 10, alpha)
    assert lo == pytest.approx(tail ** 0.1, abs=1e-12)
    assert hi == 1.0


def test_clopper_pearson_root_property() -> None:
    """根性质:下界代入 P(Bin(n, ·) ≥ k) ≈ α/2;上界由互补恒等式保证。"""
    alpha = 0.05
    for k, n in ((6, 10), (55, 100), (9, 10)):
        lo, _ = clopper_pearson_bounds(k, n, alpha)
        assert binom_upper_tail(k, n, lo) == pytest.approx(alpha / 2.0, abs=1e-9)


def test_clopper_pearson_complement_identity() -> None:
    """互补恒等式 U(k) = 1 − L(n−k)(二分类下 p_lower+p_upper≡1 的根源)。"""
    alpha = 0.05
    for k, n in ((0, 10), (3, 10), (6, 12), (10, 10)):
        lo_a, _ = clopper_pearson_bounds(k, n, alpha)
        _, hi_b = clopper_pearson_bounds(n - k, n, alpha)
        assert lo_a + hi_b == pytest.approx(1.0, abs=1e-12), f"k={k}, n={n}"


def test_clopper_pearson_validation() -> None:
    """n≤0 / k 越界 / α 越界 → ValueError(中文)。"""
    with pytest.raises(ValueError, match="clopper_pearson_bounds"):
        clopper_pearson_bounds(1, 0, 0.05)
    with pytest.raises(ValueError, match="clopper_pearson_bounds"):
        clopper_pearson_bounds(11, 10, 0.05)
    with pytest.raises(ValueError, match="clopper_pearson_bounds"):
        clopper_pearson_bounds(5, 10, 0.6)
    with pytest.raises(ValueError, match="clopper_pearson_bounds"):
        clopper_pearson_bounds(5, 10, 0.0)


# ---------------------------------------------------------------------------
# Cohen 认证半径(数学层)
# ---------------------------------------------------------------------------


def test_certified_radius_unanimous_closed_form() -> None:
    """全票手算对照:k=K 全对 → p_lower=(α/2)^(1/K),半径 = σ/2·2Φ⁻¹(p_lower)。"""
    k_total, sigma, alpha = 10, 16.0, 0.05
    result = certified_radius(k_total, k_total, 0, sigma, alpha)
    p_lower_closed = (alpha / 2.0) ** (1.0 / k_total)
    assert result["status"] == "certified"
    assert result["p_lower"] == pytest.approx(p_lower_closed, abs=1e-12)
    assert result["p_upper"] == pytest.approx(1.0 - p_lower_closed, abs=1e-12)  # 互补
    # 任务口径的闭式:σ/2·(Φ⁻¹(p_lower) − Φ⁻¹(p_upper)) = σ/2·2Φ⁻¹(p_lower)
    radius_closed = sigma / 2.0 * 2.0 * norm_ppf(p_lower_closed)
    assert result["radius"] == pytest.approx(radius_closed, rel=1e-9)
    assert result["radius"] > 0.0
    assert result["binary_complement"] is True


def test_certified_radius_abstain_near_half() -> None:
    """无证书区:k ≈ K/2(平票 / 微弱多数)→ ABSTAIN,不硬造半径。"""
    for k_total, k_top1 in ((10, 5), (10, 6), (20, 10), (20, 12)):
        result = certified_radius(k_total, k_top1, k_total - k_top1, 8.0)
        assert result["status"] == "abstain", (k_total, k_top1)
        assert result["radius"] is None
        assert result["p_lower"] < 0.5 < result["p_upper"]


def test_certified_radius_monotone_in_top1() -> None:
    """半径随 k_top1 单调不减,全票最大;半数 ABSTAIN(不参与半径序列)。"""
    radii: list[float] = []
    for k_top1 in range(11, 21):
        result = certified_radius(20, k_top1, 20 - k_top1, 10.0)
        if result["status"] == "certified":
            radii.append(result["radius"])
    assert len(radii) >= 2
    assert all(a < b for a, b in zip(radii, radii[1:]))
    assert radii[-1] == pytest.approx(10.0 * norm_ppf(0.025 ** (1 / 20)), rel=1e-9)
    assert certified_radius(20, 10, 10, 10.0)["radius"] is None


def test_certified_radius_sigma_zero() -> None:
    """σ=0:全票仍 certified(多数显著),半径恰 0.0(零噪声给零半径)。"""
    result = certified_radius(10, 10, 0, 0.0)
    assert result["status"] == "certified"
    assert result["radius"] == 0.0


def test_certified_radius_multiclass_counts_abstain() -> None:
    """k_top1+k_top2<n(类票分裂)时口径仍成立:弱多数 → ABSTAIN。"""
    result = certified_radius(10, 6, 2, 8.0)  # 其余 2 票给第三类
    assert result["status"] == "abstain"
    assert result["radius"] is None
    assert result["binary_complement"] is False
    assert result["p_lower"] + result["p_upper"] < 1.0


def test_certified_radius_validation() -> None:
    """σ<0 / n<1 / 计数越界 / k_top1<k_top2 / k1+k2>n → ValueError(中文)。"""
    with pytest.raises(ValueError, match="sigma"):
        certified_radius(10, 10, 0, -0.1)
    with pytest.raises(ValueError, match="n"):
        certified_radius(0, 0, 0, 1.0)
    with pytest.raises(ValueError, match="越界"):
        certified_radius(10, 11, 0, 1.0)
    with pytest.raises(ValueError, match="不得小于"):
        certified_radius(10, 3, 5, 1.0)
    with pytest.raises(ValueError, match="超出"):
        certified_radius(10, 8, 5, 1.0)


def test_variant_seed_deterministic_and_distinct() -> None:
    """种子派生:同参数恒同值、不同 (j, σ, 图) 互异、值域 [0, 2^31)。"""
    assert variant_seed(7, "imgA", 8.0, 3) == variant_seed(7, "imgA", 8.0, 3)
    seeds = {
        variant_seed(7, "imgA", 8.0, j) for j in range(10)
    } | {
        variant_seed(7, "imgA", 16.0, 0),
        variant_seed(7, "imgB", 8.0, 0),
        variant_seed(8, "imgA", 8.0, 0),
    }
    assert len(seeds) == 13
    assert all(0 <= s < 2**31 for s in seeds)


# ---------------------------------------------------------------------------
# 噪声 LUT 批量引擎(A239):等价性 / 回退 / 确定性 / 分布 / 加速比
# ---------------------------------------------------------------------------


def _make_gray(width: int, height: int, seed: int = 4242) -> PILImage.Image:
    """确定性灰度测试图(棋盘 + 渐变 + 随机块,覆盖 0/255 截断边界)。"""
    import random as _random

    rng = _random.Random(seed)
    image = PILImage.new("L", (width, height))
    image.putdata(
        [
            (x * 255 // max(1, width - 1)) if (x + y) % 3 == 0 else rng.randrange(256)
            for y in range(height)
            for x in range(width)
        ]
    )
    return image


def test_noisy_gray_sigma0_fast_equals_reference_and_source() -> None:
    """等价性红线:σ=0 时快速路径 / 参考实现 / 原图三方逐字节一致。

    零噪声退化语义(σ=0 变体 ≡ 原图)是平滑认证的数学基础,LUT 全 128
    直通 + ImageChops 精确整数算术保证逐字节等价;含奇数像素尺寸(5×3,
    LUT 方案无成对采样约束)。
    """
    pil = _load_pil()
    for width, height in ((24, 16), (5, 3), (64, 48)):
        gray = _make_gray(width, height, seed=width * 100 + height)
        src = gray.tobytes()
        for vseed in (1, 4242):
            fast = _noisy_gray(pil, gray, 0.0, vseed)
            ref = _noisy_gray_reference(pil, gray, 0.0, vseed)
            assert fast.tobytes() == src, (width, height, vseed)
            assert ref.tobytes() == src


def test_noisy_gray_sigma0_noise_values_are_zero() -> None:
    """σ=0 的 LUT 全 128(零偏移编码),任意图任意种子输出恒等原图。"""
    pil = _load_pil()
    lut = _noise_lut(0.0)
    assert lut == bytes([128]) * 256
    gray = _make_gray(13, 7, seed=7)
    for vseed in (1, 999, 2**31 - 1):
        assert _noisy_gray(pil, gray, 0.0, vseed).tobytes() == gray.tobytes()


def test_noisy_gray_large_sigma_falls_back_to_reference() -> None:
    """σ > FAST_SIGMA_MAX 回退参考路径:与 _noisy_gray_reference 逐字节一致。

    偏移编码域 [-128, 127] 在大 σ 下饱和,快速 LUT 不再无偏;回退到
    逐像素 float64 参考实现(与 A227 旧实现逐字节一致),确定性保持。
    """
    pil = _load_pil()
    gray = _make_gray(24, 16, seed=99)
    for sigma in (FAST_SIGMA_MAX + 0.5, 64.0):
        vseed = variant_seed(5, "big", sigma, 3)
        fast = _noisy_gray(pil, gray, sigma, vseed)
        ref = _noisy_gray_reference(pil, gray, sigma, vseed)
        assert fast.tobytes() == ref.tobytes(), sigma
        again = _noisy_gray(pil, gray, sigma, vseed)
        assert again.tobytes() == ref.tobytes()  # 回退路径同样字节确定


def test_noisy_gray_fast_path_deterministic_and_sensitive() -> None:
    """快速路径:同 (σ, vseed) 恒同字节;换 vseed / 换 σ 输出确实变化。"""
    pil = _load_pil()
    gray = _make_gray(32, 20, seed=5)
    first = _noisy_gray(pil, gray, 16.0, 77).tobytes()
    assert _noisy_gray(pil, gray, 16.0, 77).tobytes() == first  # 确定
    assert _noisy_gray(pil, gray, 16.0, 78).tobytes() != first  # 换种子
    assert _noisy_gray(pil, gray, 8.0, 77).tobytes() != first   # 换 σ
    # 非全零噪声(快速路径确实在加噪声)
    assert first != gray.tobytes()


def test_noise_lut_symmetric_bounded_and_stratified() -> None:
    """LUT 结构性质:对称(d(b)+d(255−b)=256,正态零均值映射)、有界
    (编码域内)、分层分位(σ=16 的最深分位 |d|≈Φ⁻¹(0.5/256)·σ≈92)。"""
    for sigma in (0.0, 4.0, 8.0, 16.0, 32.0, 40.0, 12.5):
        lut = _noise_lut(sigma)
        assert len(lut) == 256
        assert all(lut[b] + lut[255 - b] == 256 for b in range(256)), sigma
        assert all(0 <= v <= 255 for v in lut)
    lut16 = _noise_lut(16.0)
    # 最深分位:索引 0 的分位 u=0.5/256 → Φ⁻¹(u)·σ ≈ −92.6(不饱和证据)
    assert lut16[0] - 128 == pytest.approx(norm_ppf(0.5 / 256) * 16.0, abs=0.5)


def test_noisy_gray_distribution_matches_sigma() -> None:
    """同分布论证:均匀索引字节 → LUT 偏移,大样本均值≈0(4 倍标准误内)、
    std≈σ(相对误差 <1%)——分层分位映射的分布保真证据。"""
    import random as _random

    sample = _random.Random(20261043).randbytes(300_000)
    for sigma in (4.0, 16.0, 32.0):
        lut = _noise_lut(sigma)
        offsets = [lut[b] - 128 for b in sample]
        mean = statistics.fmean(offsets)
        std = statistics.pstdev(offsets)
        # 均值阈值取 4 倍标准误(σ/√N):固定种子下确定性成立且不因
        # 采样波动误报
        assert abs(mean) <= 4.0 * sigma / len(sample) ** 0.5, (sigma, mean)
        assert std == pytest.approx(sigma, rel=0.01), (sigma, std)


def test_noisy_gray_reference_deterministic_and_sensitive() -> None:
    """参考实现(A227 旧内核)自身:同种子同字节、换种子变化、σ=0 直通。"""
    pil = _load_pil()
    gray = _make_gray(24, 16, seed=3)
    first = _noisy_gray_reference(pil, gray, 12.0, 42).tobytes()
    assert _noisy_gray_reference(pil, gray, 12.0, 42).tobytes() == first
    assert _noisy_gray_reference(pil, gray, 12.0, 43).tobytes() != first
    assert _noisy_gray_reference(pil, gray, 0.0, 42).tobytes() == gray.tobytes()


def test_noisy_gray_speedup_at_least_5x() -> None:
    """加速比断言(宽松下界 ≥5×,实测约 40×+):固定图固定种子计时对照。

    防抖动:参考 / 快速各跑多轮取每调用最优均值(排除 JIT 预热与后台
    噪声);下界 5× 与实测加速比之间留有约一个数量级裕量,CI 抖动不致
    翻车。图取 64×48(合成语料最大档),σ=16(默认网格中位)。
    """
    pil = _load_pil()
    gray = _make_gray(64, 48, seed=2026)
    sigma, vseed = 16.0, 77

    def best_per_call(fn, calls: int, rounds: int = 4) -> float:
        best = float("inf")
        for _ in range(rounds):
            start = time.perf_counter()
            for _ in range(calls):
                fn()
            best = min(best, (time.perf_counter() - start) / calls)
        return best

    t_ref = best_per_call(lambda: _noisy_gray_reference(pil, gray, sigma, vseed), 8)
    t_fast = best_per_call(lambda: _noisy_gray(pil, gray, sigma, vseed), 64)
    assert t_fast < t_ref, "快速路径不应慢于参考实现"
    assert t_ref / t_fast >= 5.0, f"加速比不足:{t_ref / t_fast:.2f}×(<5×)"


# ---------------------------------------------------------------------------
# 平滑分类器包装(基分类器注入)
# ---------------------------------------------------------------------------


def _tiny_png(path: Path, width: int = 24, height: int = 16) -> Path:
    """生成小测试 PNG(手写图惯例:PIL 直接构造,棋盘纹理)。"""
    rows = []
    for y in range(height):
        for x in range(width):
            v = 200 if ((x // 3) + (y // 3)) % 2 else 40
            rows.append((v, 128, 255 - v))
    PILImage.frombytes("RGB", (width, height), bytes(b for px in rows for b in px)).save(
        path, format="PNG"
    )
    return path


def test_smoothed_classifier_unanimous_stub(tmp_path: Path) -> None:
    """stub 基分类器(按文件名打分,变体保留 stem)→ K 票同值 + 闭式半径。"""
    image = _tiny_png(tmp_path / "shot_nsfw_hi_001.png")
    smoothed = SmoothedClassifier(StubClassifier(Config()), sigma=4.0, k=10)
    row = smoothed.classify_smoothed(image, tmp_path / "v1")
    assert row["top_class"] == "nsfw"
    assert row["n"] == 10
    assert row["k_top1"] == 10
    assert row["k_top2"] == 0
    assert row["votes_nsfw"] == 10
    assert row["status"] == "certified"
    assert row["radius"] == pytest.approx(4.0 * norm_ppf(0.025**0.1), rel=1e-9)


class _SplitVoteClassifier:
    """构造分裂票的假分类器:按变体序号 j 打分(多数投票可精确手算)。

    - ``mode="nine_of_ten"``:j<9 → 0.9(nsfw),j=9 → 0.1 → 9/1 票;
    - ``mode="alternating"``:j 偶 → 0.9,j 奇 → 0.1 → K 偶数时 5/5 平票。
    """

    name = "splitvote"

    def __init__(self, mode: str = "nine_of_ten") -> None:
        self.mode = mode

    def classify(self, img: ImageEvidence) -> ImageScore:
        name = Path(img.path).name
        j = int(name.rsplit("__smooth_", 1)[1][:3])
        if self.mode == "nine_of_ten":
            prob = 0.1 if j == 9 else 0.9
        else:
            prob = 0.9 if j % 2 == 0 else 0.1
        return ImageScore(image=img, model=self.name, nsfw_prob=prob)


def test_smoothed_classifier_majority_vote_split(tmp_path: Path) -> None:
    """9/10 票 → 计数精确且与 certified_radius 一致;5/5 平票 → ABSTAIN。"""
    image = _tiny_png(tmp_path / "probe_normal_001.png")

    row = SmoothedClassifier(_SplitVoteClassifier(), sigma=6.0, k=10).classify_smoothed(
        image, tmp_path / "v9"
    )
    assert row["votes_nsfw"] == 9
    assert row["top_class"] == "nsfw"
    assert row["k_top1"] == 9 and row["k_top2"] == 1
    assert row["status"] == "certified"
    expected = certified_radius(10, 9, 1, 6.0)["radius"]
    assert row["radius"] == pytest.approx(expected, rel=1e-12)

    tie = SmoothedClassifier(
        _SplitVoteClassifier(mode="alternating"), sigma=6.0, k=10
    ).classify_smoothed(image, tmp_path / "v5")
    assert tie["votes_nsfw"] == 5
    assert tie["top_class"] == "clean"  # 平票保守方向
    assert tie["status"] == "abstain"
    assert tie["radius"] is None  # 不硬造半径


def test_smoothed_classifier_sigma_zero_degenerate(tmp_path: Path) -> None:
    """σ=0 退化 = 基分类器:变体逐字节一致,平滑预测 ≡ 基预测,半径 0。"""
    stub = StubClassifier(Config())
    for name, expected_class in (
        ("shot_nsfw_hi_001.png", "nsfw"),
        ("shot_normal_001.png", "clean"),
    ):
        image = _tiny_png(tmp_path / name)
        base_prob = stub.classify(
            ImageEvidence(path=str(image), url="benchmark://x", source_page="benchmark://x")
        ).nsfw_prob
        row = SmoothedClassifier(stub, sigma=0.0, k=6).classify_smoothed(
            image, tmp_path / f"zero_{expected_class}"
        )
        assert row["top_class"] == expected_class
        assert row["k_top1"] == 6
        # 基分类器对原图的二值判决与平滑预测一致(σ=0 退化语义)
        assert (base_prob >= 0.5) == (expected_class == "nsfw")
        assert row["radius"] == 0.0
        assert row["status"] == "certified"  # 多数显著,但半径为 0(不计认证准确)
    # σ=0 的 K 个变体彼此逐字节一致(零噪声)
    variants = sorted((tmp_path / "zero_nsfw").glob("shot_nsfw_hi_001__smooth_*.png"))
    assert len(variants) == 6
    assert len({v.read_bytes() for v in variants}) == 1


def test_smoothed_classifier_noise_deterministic_bytes(tmp_path: Path) -> None:
    """同种子同输出字节;换种子噪声确实不同;σ>0 变体与 σ=0 变体不同。"""
    image = _tiny_png(tmp_path / "probe_nsfw_mid_001.png")
    smoothed = SmoothedClassifier(StubClassifier(Config()), sigma=12.0, k=5)
    smoothed.classify_smoothed(image, tmp_path / "a", image_key="K1")
    smoothed.classify_smoothed(image, tmp_path / "b", image_key="K1")
    name = "probe_nsfw_mid_001__smooth_000.png"
    assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()

    SmoothedClassifier(
        StubClassifier(Config()), sigma=12.0, k=5, seed=DEFAULT_SEED + 1
    ).classify_smoothed(image, tmp_path / "c", image_key="K1")
    assert (tmp_path / "a" / name).read_bytes() != (tmp_path / "c" / name).read_bytes()

    SmoothedClassifier(StubClassifier(Config()), sigma=0.0, k=5).classify_smoothed(
        image, tmp_path / "z", image_key="K1"
    )
    assert (tmp_path / "a" / name).read_bytes() != (tmp_path / "z" / name).read_bytes()


def test_smoothed_classifier_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """构造参数非法 → ValueError(中文);Pillow 屏蔽 → CertifyError(中文)。"""
    stub = StubClassifier(Config())
    with pytest.raises(ValueError, match="sigma"):
        SmoothedClassifier(stub, sigma=-1.0)
    with pytest.raises(ValueError, match="k"):
        SmoothedClassifier(stub, sigma=1.0, k=0)
    with pytest.raises(ValueError, match="alpha"):
        SmoothedClassifier(stub, sigma=1.0, alpha=0.9)
    with pytest.raises(ValueError, match="threshold"):
        SmoothedClassifier(stub, sigma=1.0, threshold=1.0)

    image = _tiny_png(tmp_path / "probe_nsfw_hi_001.png")
    monkeypatch.setitem(sys.modules, "PIL", None)
    with pytest.raises(CertifyError, match="Pillow"):
        SmoothedClassifier(stub, sigma=4.0, k=2).classify_smoothed(
            image, tmp_path / "blocked"
        )


def test_smoothed_classifier_rejects_undecodable(tmp_path: Path) -> None:
    """坏图(非 PNG 字节)→ CertifyError(中文)。"""
    bad = tmp_path / "bad_nsfw_hi_001.png"
    bad.write_bytes(b"definitely not a png")
    with pytest.raises(CertifyError, match="无法解码"):
        SmoothedClassifier(StubClassifier(Config()), sigma=4.0, k=2).classify_smoothed(
            bad, tmp_path / "v"
        )


# ---------------------------------------------------------------------------
# 合成语料(phash_redteam 生成器惯例)
# ---------------------------------------------------------------------------


def test_make_synthetic_corpus_deterministic_and_labeled(tmp_path: Path) -> None:
    """同 (count, seed) 逐字节一致;文件名即金标;count 非法 ValueError。"""
    first = make_synthetic_corpus(tmp_path / "c1", count=6, seed=11)
    second = make_synthetic_corpus(tmp_path / "c2", count=6, seed=11)
    assert [item["name"] for item in first] == [item["name"] for item in second]
    for a, b in zip(first, second):
        assert Path(a["path"]).read_bytes() == Path(b["path"]).read_bytes()
    # 图间互异(构图/调色随图序变化)
    assert len({Path(i["path"]).read_bytes() for i in first}) == 6
    by_kind = {item["kind"]: item["label"] for item in first}
    assert by_kind["nsfw_hi"] == "nsfw"
    assert by_kind["nsfw_mid"] == "borderline"
    assert by_kind["normal"] == "clean"
    for bad in (0, -3, 65):
        with pytest.raises(ValueError, match="count"):
            make_synthetic_corpus(tmp_path / f"c{bad}", count=bad)


# ---------------------------------------------------------------------------
# run() 全链(stub 满票可手算 / 混合 ABSTAIN / 报告结构 / 确定性)
# ---------------------------------------------------------------------------


def _make_labeled_corpus(root: Path) -> Path:
    """labels.json 小语料(adversarial 手写 PNG 惯例):2 hi / 1 mid / 3 normal。"""
    corpus = root / "corpus"
    corpus.mkdir(parents=True)
    labels = {
        "nsfw_hi_001.png": "nsfw",
        "nsfw_hi_002.png": "nsfw",
        "nsfw_mid_001.png": "borderline",
        "normal_001.png": "clean",
        "normal_002.png": "clean",
        "normal_003.png": "clean",
    }
    for name in labels:
        write_png(corpus / name, 40, 30, (0x40, 0x80, 0xC0))
    (corpus / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return corpus


def test_run_labeled_corpus_stub_certified_rate_hand_check(tmp_path: Path) -> None:
    """stub 全链:全票认证,认证准确率 = 正确预测占比 5/6,半径 = 闭式值。"""
    corpus = _make_labeled_corpus(tmp_path)
    before = sorted(p.name for p in corpus.iterdir())
    payload = run(tmp_path / "out", corpus_dir=corpus, k=12, sigmas=(12.0,))
    assert sorted(p.name for p in corpus.iterdir()) == before  # 语料只读

    stats = payload["stats"][0]
    assert stats["sigma"] == 12.0 and stats["n"] == 6
    assert stats["abstain"] == 0
    assert stats["smoothed_correct"] == 5   # borderline 预测 nsfw → 不正确
    assert stats["smoothed_acc"] == pytest.approx(5 / 6, abs=1e-4)
    assert stats["certified_correct"] == 5
    assert stats["certified_acc"] == pytest.approx(5 / 6, abs=1e-4)  # 经验认证准确率@σ
    assert stats["certified_wrong"] == 1   # 诚实性指标:认证但预测错误
    closed_radius = 12.0 * norm_ppf((DEFAULT_ALPHA / 2) ** (1 / 12))
    assert stats["avg_radius"] == pytest.approx(closed_radius, abs=1e-3)
    assert stats["radius_min"] == stats["radius_max"] == pytest.approx(closed_radius, abs=1e-3)
    assert stats["radius_distribution"] == [
        pytest.approx(closed_radius, abs=1e-3)
    ] * 6
    for row in payload["details"]:
        assert row["n"] == 12 and row["k_top1"] == 12 and row["k_top2"] == 0
        assert row["status"] == "certified"
        assert row["certified_correct"] == (row["label"] != "borderline")


class _MixedVoteClassifier:
    """混合票分类器:nsfw_hi → 全票 0.9;nsfw_mid → 奇偶交替(平票 ABSTAIN);
    其余 → 全票 0.02。认证率可精确手算。"""

    name = "mixedvote"

    def classify(self, img: ImageEvidence) -> ImageScore:
        name = Path(img.path).name
        if "nsfw_hi" in name:
            prob = 0.9
        elif "nsfw_mid" in name:
            j = int(name.rsplit("__smooth_", 1)[1][:3])
            prob = 0.9 if j % 2 == 0 else 0.1
        else:
            prob = 0.02
        return ImageScore(image=img, model=self.name, nsfw_prob=prob)


def test_run_mixed_certified_and_abstain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """混合场景:hi 全票认证 / mid 平票 ABSTAIN / normal 全票认证 → 3/4 认证。"""
    from benchmarks import certify_bench as cb

    corpus = _make_labeled_corpus(tmp_path)
    monkeypatch.setattr(cb, "_build_classifier", lambda name, cfg: _MixedVoteClassifier())
    payload = run(tmp_path / "out", corpus_dir=corpus, k=10, sigmas=(6.0,))

    stats = payload["stats"][0]
    assert stats["abstain"] == 1
    # 语料:nsfw_hi×2(nsfw,认证正确)、nsfw_mid×1(borderline,ABSTAIN)、normal×3(clean,认证正确)
    assert stats["certified_correct"] == 5
    assert stats["certified_acc"] == pytest.approx(5 / 6, abs=1e-4)
    by_file = {row["file"]: row for row in payload["details"]}
    assert by_file["nsfw_mid_001.png"]["status"] == "abstain"
    assert by_file["nsfw_mid_001.png"]["radius"] is None
    assert by_file["nsfw_mid_001.png"]["top_class"] == "clean"  # 平票保守
    for name in ("nsfw_hi_001.png", "normal_002.png"):
        assert by_file[name]["status"] == "certified"
        assert by_file[name]["radius"] > 0.0
    # 半径均值只对 radius>0 的图计算(ABSTAIN 不硬造半径)
    radii = [r["radius"] for r in payload["details"] if r["radius"] is not None]
    assert stats["avg_radius"] == pytest.approx(sum(radii) / len(radii), abs=1e-3)
    assert len(stats["radius_distribution"]) == len(radii)


def test_run_sigma_grid_rows_and_payload_structure(tmp_path: Path) -> None:
    """σ 网格:逐 (σ, 图) 一行、半径随 σ 线性放大(stub 全票下 r ∝ σ)。"""
    payload = run(tmp_path / "out", synthetic_count=4, k=8, sigmas=(4.0, 8.0, 0.0))
    assert payload["schema"] == "netsentinel-certify-report/1"
    assert payload["sigmas"] == [4.0, 8.0, 0.0]
    assert [row["sigma"] for row in payload["stats"]] == [4.0, 8.0, 0.0]
    assert len(payload["details"]) == 4 * 3
    by_sigma = {row["sigma"]: row for row in payload["stats"]}
    assert by_sigma[8.0]["avg_radius"] == pytest.approx(
        2 * by_sigma[4.0]["avg_radius"], rel=1e-3
    )
    # σ=0:平滑预测仍正确(退化 = 基分类器),但半径恒 0 → 认证准确率 0,
    # 且 radius>0 的集合为空 → 平均半径/分布均为 None/空(不硬造数字)
    assert by_sigma[0.0]["smoothed_acc"] == by_sigma[4.0]["smoothed_acc"]
    assert by_sigma[0.0]["certified_acc"] == 0.0
    assert by_sigma[0.0]["avg_radius"] is None
    assert by_sigma[0.0]["radius_distribution"] == []
    # 报告口径段(评审可直接读到的数学说明)
    method = payload["method"]
    assert method["k"] == 8 and method["alpha"] == DEFAULT_ALPHA
    assert method["seed"] == DEFAULT_SEED
    assert "Phi_inv" in method["formula"]
    assert "Acklam" in method["ppf"]
    assert "Clopper-Pearson" in method["cp"]
    assert "p_lower > p_upper" in method["certify_rule"]
    assert method["abstain"].startswith("无证书时")


def test_run_reports_written_and_structure(tmp_path: Path) -> None:
    """报告双输出:JSON 可解析、markdown 含关键段;输出目录只有两份报告。"""
    payload = run(tmp_path / "out", synthetic_count=3, k=6, sigmas=(5.0,))
    out = tmp_path / "out"
    assert sorted(p.name for p in out.iterdir()) == [
        "certify_report.json",
        "certify_report.md",
    ]
    data = json.loads((out / "certify_report.json").read_text(encoding="utf-8"))
    assert data == payload
    text = (out / "certify_report.md").read_text(encoding="utf-8")
    assert "# NetSentinel 预处理随机化平滑认证评测报告(A227)" in text
    assert "认证准确率@σ" in text
    assert "r >= sigma/2" in text
    assert "ABSTAIN" in text and "CERTIFIED" in text
    assert "逐图认证明细" in text
    assert "人工确认" in text  # 红线声明
    assert "cs_nsfw_hi_000.png" in text  # 合成语料文件名进明细表


def test_run_byte_deterministic_same_seed(tmp_path: Path) -> None:
    """确定性红线:同种子两次运行 JSON+MD 逐字节一致(报告无时间戳)。"""
    run(tmp_path / "o1", synthetic_count=4, k=8, sigmas=(6.0, 10.0), seed=99)
    run(tmp_path / "o2", synthetic_count=4, k=8, sigmas=(6.0, 10.0), seed=99)
    for name in ("certify_report.json", "certify_report.md"):
        assert (tmp_path / "o1" / name).read_bytes() == (tmp_path / "o2" / name).read_bytes()
    # 换种子 → 噪声不同 → 报告字节不同(变体 sha 与文本内容不进报告,
    # 但 stub 场景下计数字段不变;此处断言换种子不抛错且报告仍可重建)
    run(tmp_path / "o3", synthetic_count=4, k=8, sigmas=(6.0, 10.0), seed=100)
    assert (tmp_path / "o3" / "certify_report.json").exists()


def test_run_validation_errors(tmp_path: Path) -> None:
    """空 σ 网格 / σ 负值 / k=0 / alpha 越界 → CertifyError(中文)。"""
    with pytest.raises(CertifyError, match="σ 网格为空"):
        run(tmp_path / "out", sigmas=())
    with pytest.raises(CertifyError, match="σ 网格含负值"):
        run(tmp_path / "out", sigmas=(1.0, -2.0))
    with pytest.raises(CertifyError, match="k"):
        run(tmp_path / "out", k=0)
    with pytest.raises(CertifyError, match="alpha"):
        run(tmp_path / "out", alpha=0.7)


def test_run_glm_unconfigured_rejected(tmp_path: Path) -> None:
    """glm 未配置(vlm_online=False)→ 拒绝评测(CertifyError,中文)。"""
    with pytest.raises(CertifyError, match="vlm_online"):
        run(tmp_path / "out", classifier="glm", k=4)


# ---------------------------------------------------------------------------
# 半径金标门禁(A239):指纹 / 容差 / 读写 / evaluate_gate / 往返
# ---------------------------------------------------------------------------


def test_fingerprint_digest_component_sensitivity() -> None:
    """指纹键逐分量敏感:分类器 / 语料 / σ 网格 / K / α / 统计版本任一
    变化即换键;同分量恒同键。"""
    identity = {"source": "synthetic", "generator": "cert-syn1", "count": 4, "seed": 1}
    base = _fingerprint_digest("stub", identity, (4.0, 8.0), 20, 0.05)
    assert base == _fingerprint_digest("stub", identity, (4.0, 8.0), 20, 0.05)
    assert _fingerprint_digest("glm", identity, (4.0, 8.0), 20, 0.05) != base
    assert _fingerprint_digest("stub", {**identity, "count": 5}, (4.0, 8.0), 20, 0.05) != base
    assert _fingerprint_digest("stub", identity, (4.0, 8.0, 16.0), 20, 0.05) != base
    assert _fingerprint_digest("stub", identity, (4.0, 8.0), 22, 0.05) != base  # 换 K
    assert _fingerprint_digest("stub", identity, (4.0, 8.0), 20, 0.01) != base  # 换 α
    # 噪声种子不进键(stub 全票口径下指标与种子无关,见指纹 docstring)
    assert _fingerprint_digest("stub", {**identity, "seed": 1}, (4.0, 8.0), 20, 0.05) == base


def test_metric_tolerance_closed_form() -> None:
    """容差闭式:max(10%·|基线|, 0.02);基线≈0 时取下限 0.02;非法 → ValueError。"""
    assert metric_tolerance(7.683) == pytest.approx(0.7683, abs=1e-12)
    assert metric_tolerance(0.75) == pytest.approx(0.075, abs=1e-12)
    assert metric_tolerance(0.1) == pytest.approx(0.02, abs=1e-12)   # 比例分支 < 下限
    assert metric_tolerance(0.0) == 0.02
    assert metric_tolerance(-3.0) == pytest.approx(0.3, abs=1e-12)
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError, match="metric_tolerance"):
            metric_tolerance(bad)


def test_evaluate_gate_unit_semantics() -> None:
    """evaluate_gate 单元:容差内通过 / 超容差违例 / 指标缺基线 / None↔数值互变。"""
    stats = [
        {"sigma": 8.0, "certified_acc": 0.75, "avg_radius": 7.683},
        {"sigma": 0.0, "certified_acc": 0.0, "avg_radius": None},
    ]
    golden = {
        "8/certified_acc": {"value": 0.75, "tol": 0.075},
        "8/avg_radius": {"value": 7.683, "tol": 0.7683},
        "0/certified_acc": {"value": 0.0, "tol": 0.02},
        "0/avg_radius": {"value": None, "tol": None},
    }
    assert evaluate_gate(stats, golden) == []  # 完全一致 + 双 None
    # 容差边界内(|Δ| = tol)不违例;边界外违例
    within = dict(golden, **{"8/certified_acc": {"value": 0.6751, "tol": 0.075}})
    assert evaluate_gate(stats, within) == []
    beyond = dict(golden, **{"8/avg_radius": {"value": 8.46, "tol": 0.7683}})
    violations = evaluate_gate(stats, beyond)
    assert [v["kind"] for v in violations] == ["metric_violation"]
    assert violations[0]["metric"] == "8/avg_radius"
    assert "超出容差" in violations[0]["message"]
    # 指标缺基线(fail-safe)与 None↔数值互变
    missing = {k: v for k, v in golden.items() if k != "0/certified_acc"}
    kinds = {v["kind"] for v in evaluate_gate(stats, missing)}
    assert "missing_metric" in kinds
    flipped = dict(golden, **{"0/avg_radius": {"value": 1.0, "tol": 0.1}})
    kinds = {v["kind"] for v in evaluate_gate(stats, flipped)}
    assert kinds == {"type_change"}


def test_build_golden_entry_shapes() -> None:
    """build_golden_entry:σ 键格式(:g)、数值基线含容差、None 基线 tol 同 None。"""
    stats = [
        {"sigma": 8.0, "certified_acc": 0.75, "avg_radius": 7.683},
        {"sigma": 12.5, "certified_acc": 0.0, "avg_radius": None},
    ]
    entry = build_golden_entry(stats)
    assert set(entry) == {
        "8/certified_acc", "8/avg_radius", "12.5/certified_acc", "12.5/avg_radius",
    }
    assert entry["8/certified_acc"] == {"value": 0.75, "tol": 0.075}
    assert entry["12.5/avg_radius"] == {"value": None, "tol": None}


def test_save_load_golden_roundtrip_deterministic(tmp_path: Path) -> None:
    """save/load 往返:无时间戳 → 同内容两次重建逐字节一致;结构校验错误集。"""
    entry = build_golden_entry(
        [{"sigma": 8.0, "certified_acc": 0.75, "avg_radius": 7.683}]
    )
    save_golden(tmp_path / "g1.json", "f" * 64, entry, classifier_name="stub")
    save_golden(tmp_path / "g2.json", "f" * 64, entry, classifier_name="stub")
    first = (tmp_path / "g1.json").read_bytes()
    assert first == (tmp_path / "g2.json").read_bytes()  # 重建幂等(无时间戳)
    raw = load_golden(tmp_path / "g1.json")
    assert raw["f" * 64] == entry
    assert raw["_schema"] == "netsentinel-certify-golden/1"
    assert load_golden(tmp_path / "absent.json") == {}  # 不存在 → 空金标
    # 坏 JSON / 顶层非对象 / 版本不兼容 / 指标表结构非法 / {value,tol} 形态非法
    bad_json = tmp_path / "bad1.json"
    bad_json.write_text("{broken", encoding="utf-8")
    with pytest.raises(CertifyGoldenError, match="无法解析"):
        load_golden(bad_json)
    bad_top = tmp_path / "bad2.json"
    bad_top.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(CertifyGoldenError, match="顶层必须是对象"):
        load_golden(bad_top)
    bad_schema = tmp_path / "bad3.json"
    bad_schema.write_text(
        json.dumps({"_schema": "other/9", "f" * 64: entry}), encoding="utf-8"
    )
    with pytest.raises(CertifyGoldenError, match="版本不兼容"):
        load_golden(bad_schema)
    bad_entry = tmp_path / "bad4.json"
    bad_entry.write_text(json.dumps({"f" * 64: {}}), encoding="utf-8")
    with pytest.raises(CertifyGoldenError, match="指标表缺失或为空"):
        load_golden(bad_entry)
    bad_spec = tmp_path / "bad5.json"
    bad_spec.write_text(json.dumps({"f" * 64: {"8/x": {"value": None, "tol": 0.1}}}), encoding="utf-8")
    with pytest.raises(CertifyGoldenError, match="应为"):
        load_golden(bad_spec)


def test_gate_roundtrip_update_then_pass(tmp_path: Path) -> None:
    """金标往返:--update-golden 重建 → payload.gate.status=updated;随后
    --gate 同参数复跑 → pass,报告含 gate 段;门禁模式下确定性仍保持。"""
    golden = tmp_path / "g" / "certify_golden.json"  # 父目录不存在 → 自动创建
    first = run(
        tmp_path / "o1", k=20, synthetic_count=4, sigmas=(4.0, 8.0),
        update_golden=True, golden_path=golden,
    )
    assert first["gate"]["mode"] == "update"
    assert first["gate"]["status"] == "updated"
    assert first["gate"]["metrics_recorded"] == 4  # 2σ × 2 指标
    assert first["gate"]["metrics_total"] == 4
    assert golden.exists()
    second = run(
        tmp_path / "o2", k=20, synthetic_count=4, sigmas=(4.0, 8.0),
        gate=True, golden_path=golden,
    )
    assert second["gate"]["mode"] == "check"
    assert second["gate"]["status"] == "pass"
    assert second["gate"]["violations"] == []
    assert second["gate"]["fingerprint"] == first["gate"]["fingerprint"]
    # 报告(JSON + markdown)带门禁段;markdown 渲染关键行
    text = (tmp_path / "o2" / "certify_report.md").read_text(encoding="utf-8")
    assert "半径金标门禁" in text
    assert "门禁**通过**" in text
    data = json.loads((tmp_path / "o2" / "certify_report.json").read_text(encoding="utf-8"))
    assert data["gate"]["status"] == "pass"
    # 同门禁配置复跑逐字节一致(门禁段不含时间戳)
    run(tmp_path / "o3", k=20, synthetic_count=4, sigmas=(4.0, 8.0),
        gate=True, golden_path=golden)
    assert (tmp_path / "o2" / "certify_report.json").read_bytes() == (
        tmp_path / "o3" / "certify_report.json").read_bytes()


def test_gate_regression_constructed_violations(tmp_path: Path) -> None:
    """构造回归违例:篡改基线(超容差 / None↔数值互变 / 指标缺基线)→
    payload.gate.violations 齐全;CLI 退出码 2 + 中文 stderr。"""
    golden = tmp_path / "g.json"
    run(tmp_path / "o", k=20, synthetic_count=4, sigmas=(4.0, 8.0),
        update_golden=True, golden_path=golden)
    raw = json.loads(golden.read_text(encoding="utf-8"))
    fp = next(k for k in raw if not k.startswith("_"))
    raw[fp]["8/certified_acc"]["value"] = 0.95          # 偏差 0.2 > 容差 0.095
    raw[fp]["8/avg_radius"] = {"value": None, "tol": None}  # None↔数值互变
    del raw[fp]["4/avg_radius"]                          # 指标缺基线(fail-safe)
    golden.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    payload = run(tmp_path / "o2", k=20, synthetic_count=4, sigmas=(4.0, 8.0),
                  gate=True, golden_path=golden)
    kinds = [v["kind"] for v in payload["gate"]["violations"]]
    assert "metric_violation" in kinds
    assert "type_change" in kinds
    assert "missing_metric" in kinds
    assert payload["gate"]["status"] == "violations"
    # CLI 裁决:退出码 2 + 中文违例详情(stderr)
    code = main([
        "--out", str(tmp_path / "o3"), "--k", "20", "--count", "4",
        "--sigma", "4,8", "--gate", "--golden", str(golden),
    ])
    assert code == 2


# ---------------------------------------------------------------------------
# CLI(退出码 0/1)
# ---------------------------------------------------------------------------


def test_cli_success_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """成功路径:退出码 0、stdout 摘要含认证准确率与半径、报告落盘。"""
    code = main(
        ["--out", str(tmp_path / "o"), "--k", "6", "--count", "3", "--sigma", "4,8"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "平滑认证评测完成" in out
    assert "认证准确率@σ" in out
    assert "平均认证半径" in out
    assert "报告已写出" in out
    assert (tmp_path / "o" / "certify_report.json").exists()
    assert (tmp_path / "o" / "certify_report.md").exists()


def test_cli_labeled_corpus_and_grid(tmp_path: Path) -> None:
    """--corpus(labels.json 口径)与 --sigma 解析:退出码 0,σ 行齐全。"""
    corpus = _make_labeled_corpus(tmp_path)
    code = main(
        ["--out", str(tmp_path / "o"), "--corpus", str(corpus), "--k", "6",
         "--sigma", "3,9"]
    )
    assert code == 0
    payload = json.loads((tmp_path / "o" / "certify_report.json").read_text(encoding="utf-8"))
    assert payload["corpus"]["source"] == "labeled"
    assert payload["corpus"]["total"] == 6
    assert [row["sigma"] for row in payload["stats"]] == [3.0, 9.0]


def test_cli_expected_errors_exit_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """可预期错误 → 退出码 1 + 中文 stderr(任务口径的 0/1 两态)。"""
    # 分类器未注册
    code = main(["--out", str(tmp_path / "o1"), "--classifier", "nope", "--k", "4"])
    assert code == 1
    assert "无法创建分类器" in capsys.readouterr().err
    # 语料缺 labels.json
    empty = tmp_path / "empty_corpus"
    empty.mkdir()
    code = main(["--out", str(tmp_path / "o2"), "--corpus", str(empty), "--k", "4"])
    assert code == 1
    assert "错误" in capsys.readouterr().err
    # glm 未配置
    code = main(["--out", str(tmp_path / "o3"), "--classifier", "glm", "--k", "4"])
    assert code == 1
    assert "vlm_online" in capsys.readouterr().err
    # σ 网格含负值 / k=0("=-4,8" 形式绕开 argparse 的负数参数歧义)
    code = main(["--out", str(tmp_path / "o4"), "--sigma=-4,8", "--k", "4"])
    assert code == 1
    code = main(["--out", str(tmp_path / "o5"), "--k", "0"])
    assert code == 1
    for name in ("o1", "o2", "o3", "o4", "o5"):
        assert not (tmp_path / name).exists()  # 失败路径绝不产出报告


def test_cli_pil_blocked_exit_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """无 Pillow 分支:monkeypatch 屏蔽 PIL 后 CLI 以退出码 1 结束(中文)。"""
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setitem(sys.modules, "PIL.Image", None)
    code = main(["--out", str(tmp_path / "o"), "--k", "4", "--count", "2"])
    assert code == 1
    assert "Pillow" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# CLI 半径金标门禁(A239):退出码三态 0(通过/重建/缺基线 warn)/ 1(坏文件)/ 2(违例)
# ---------------------------------------------------------------------------


def test_cli_gate_roundtrip_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 金标往返:--update-golden 重建(退出码 0 + diff 摘要)→
    --gate 复跑通过(退出码 0 + 门禁通过摘要)。"""
    golden = tmp_path / "g.json"
    code = main([
        "--out", str(tmp_path / "o1"), "--k", "20", "--count", "4",
        "--sigma", "4,8", "--update-golden", "--golden", str(golden),
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "半径金标已重建" in out
    assert "新增基线(4 项指标" in out
    code = main([
        "--out", str(tmp_path / "o2"), "--k", "20", "--count", "4",
        "--sigma", "4,8", "--gate", "--golden", str(golden),
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "半径金标门禁通过" in out
    assert "全部在容差内" in out


def test_cli_gate_bad_golden_file_exit_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """坏金标文件(坏 JSON / 版本不兼容 / 结构非法)→ 退出码 1 + 中文 stderr。"""
    # 坏 JSON
    golden = tmp_path / "bad.json"
    golden.write_text("{not json", encoding="utf-8")
    code = main([
        "--out", str(tmp_path / "o"), "--k", "8", "--count", "2",
        "--gate", "--golden", str(golden),
    ])
    assert code == 1
    assert "金标文件无法解析" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()  # 失败路径绝不产出报告
    # 版本不兼容
    golden.write_text(
        json.dumps({"_schema": "netsentinel-certify-golden/9", "f": {}}), encoding="utf-8"
    )
    code = main([
        "--out", str(tmp_path / "o2"), "--k", "8", "--count", "2",
        "--gate", "--golden", str(golden),
    ])
    assert code == 1
    assert "版本不兼容" in capsys.readouterr().err
    # 指标形态非法(value=None 但 tol 数值)
    golden.write_text(
        json.dumps({"f" * 64: {"8/avg_radius": {"value": None, "tol": 0.1}}}),
        encoding="utf-8",
    )
    code = main([
        "--out", str(tmp_path / "o3"), "--k", "8", "--count", "2",
        "--gate", "--golden", str(golden),
    ])
    assert code == 1
    assert "结构非法" in capsys.readouterr().err


def test_cli_gate_fingerprint_sensitivity_missing_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """指纹敏感:换 K / 换 α → 指纹换键 → 缺基线;warn 跳过退出码 0,
    --missing-baseline fail 按违例处理退出码 2。"""
    golden = tmp_path / "g.json"
    main([
        "--out", str(tmp_path / "o0"), "--k", "20", "--count", "3",
        "--sigma", "4,8", "--update-golden", "--golden", str(golden),
    ])
    capsys.readouterr()
    # 换 K:指纹未命中 → 默认 warn 跳过(退出码 0 + 中文警告)
    code = main([
        "--out", str(tmp_path / "o1"), "--k", "24", "--count", "3",
        "--sigma", "4,8", "--gate", "--golden", str(golden),
    ])
    assert code == 0
    captured = capsys.readouterr()
    assert "缺基线" in captured.err
    assert "已按警告跳过" in captured.out
    # 换 α:同样缺基线 → fail 策略下退出码 2
    code = main([
        "--out", str(tmp_path / "o2"), "--k", "20", "--count", "3",
        "--sigma", "4,8", "--alpha", "0.01", "--gate", "--missing-baseline", "fail",
        "--golden", str(golden),
    ])
    assert code == 2
    assert "missing_baseline=fail" in capsys.readouterr().err
    # 原参数复跑仍通过(指纹命中,退出码 0)
    code = main([
        "--out", str(tmp_path / "o3"), "--k", "20", "--count", "3",
        "--sigma", "4,8", "--gate", "--golden", str(golden),
    ])
    assert code == 0
    assert "半径金标门禁通过" in capsys.readouterr().out
