# -*- coding: utf-8 -*-
"""NetSentinel 预处理随机化平滑认证评测(A227,对标 Cohen et al. 2019
"Certified Adversarial Robustness via Randomized Smoothing";A239 噪声
LUT 加速 + 认证半径金标门禁)。

**动机(V10.4 波报告点名)**:对黑盒 VLM API( glm / nudenet / clip ...),
唯一可部署的认证防御就是预处理随机化平滑——推理前对图片叠加 σ 高斯噪声,
取 K 个噪声变体的多数投票为平滑预测;Cohen et al. 的定理给出**数学下界**:
在置信水平 1−α 下,任何 L2 扰动 ≤ r 的攻击都无法改变平滑预测。对
NetSentinel 的意义:半自动举报合规需要"检测率 ≥ x% 有统计担保"的证据,
本基准把该定理落成可复现的离线评测(经验认证准确率@σ / 平均认证半径 /
逐图半径分布 / ABSTAIN 计数),报告 JSON + markdown 双输出(结构对齐
``benchmarks/adversarial.py`` 与 ``benchmarks/phash_redteam.py`` 惯例)。

**管线**(全程离线、零网络、零第三方依赖——Pillow 为可选依赖,缺席时以
退出码 1 结束,不产出无效报告):

1. **平滑分类器包装**(:class:`SmoothedClassifier`,基分类器注入):每图
   生成 K 个 σ 高斯噪声变体——灰度 ``convert("L")`` 后叠加确定性
   N(0, σ²) 偏移。A239 起噪声引擎改为**分位数 LUT 批量方案**
   (:func:`_noisy_gray`,σ ≤ :data:`FAST_SIGMA_MAX` 时
   ``randbytes → bytes.translate → ImageChops.add`` 全 C 级路径,实测
   约 45× 加速;σ=0 变体与原图逐字节一致;论证见该函数 docstring),
   旧逐像素 float64 实现保留为 :func:`_noisy_gray_reference`(σ 超限时
   的回退路径 + 等价性测试金标),变体文件名保留原 stem(stub 桩规则
   不断链);K 个变体经基分类器批量评分(``classify_batch`` 优先,协议
   不支持回退逐张——对齐 adversarial 的 ``_classify_all`` 形态),按阈值
   0.5 二值化后**多数投票**得平滑预测与经验计数 ``(n=K, k_top1)``
   (二分类下 k_top2 = n − k_top1);
2. **Clopper-Pearson 精确置信界**:p_lower = k_top1 的 CP 精确下界、
   p_upper = k_top2 的 CP 精确上界(尾概率在对数空间用 ``math.lgamma``
   精确求和 + 二分求根;与 ``netsentinel/decision/conformal.py`` 的
   ``_binom_upper_tail``/``_clopper_pearson_lower`` 同款算法——后者为
   下划线私有接口、非公开契约,跨包引用会随重构静默断裂,故本地实现
   同款内核并在 docstring 标注来源,口径与已审计实现一致);
3. **Cohen 认证半径**(闭式下界,纯 stdlib)::

       r ≥ σ/2 · (Φ⁻¹(p_lower) − Φ⁻¹(p_upper))

   Φ⁻¹ 为标准正态分位数,用 **Acklam 有理近似** 加一步 Halley 打磨
   (``math.erfc`` 亦属 stdlib):原始 Acklam 最大绝对误差 < 1.15e-9,
   打磨后实测 < 1e-12(测试以已知分位点锁定 < 1e-9,远严于 1e-4 要求);
4. **ABSTAIN 语义(不硬造半径)**:无证书时 status="abstain"、radius=None,
   只计 ABSTAIN 数,绝不输出 0 半径冒充证书。判定口径见下。

**判定口径的数学说明(重要,评审必读)**:任务口径"p_lower+p_upper>1 时
给证书"在**精确** Clopper-Pearson 界下有一个必须直面的恒等式:二分类中
k_top1 + k_top2 = n,而 CP 界满足互补对称性 ``U(k) = 1 − L(n−k)``(本模块
docstring 内证明),于是 p_upper = 1 − p_lower **精确成立**,故
p_lower + p_upper ≡ 1——"严格大于 1"在任何计数组合下都不可达(包括
全票 k=K);同理可证多分类下 k_top1+k_top2 ≤ n 时和恒 ≤ 1。因此本模块
采用 Cohen 原始代码的判据 **certified ⟺ p_lower > p_upper**(等价于
Φ⁻¹(p_lower) − Φ⁻¹(p_upper) > 0,即认证半径 > 0;二分类下等价于
p_lower > 1/2 的多数显著性)。该判据与任务的两条锁定测试完全一致:

- 全票 k_top1=K:p_lower = (α/2)^(1/K)(闭式),p_upper = 1 − p_lower,
  半径 = σ/2·(Φ⁻¹(p_lower) − Φ⁻¹(1−p_lower)) = **σ/2·2Φ⁻¹(p_lower)**
  (Φ⁻¹ 反对称性),正是任务要求的手算闭式;
- k ≈ K/2:p_lower < 1/2 < p_upper → ABSTAIN(半径 None);
- ABSTAIN ⟺ p_lower ≤ p_upper ⟺ p_lower + p_upper ≤ 1(等号含入
  ABSTAIN——二分类下和恒为 1,非退化判定由 p_lower 与 1/2 的比较承载)。

**评测流程**:语料 × σ 网格 × K——默认语料为**确定性合成图集**(生成器
与金标惯例对齐 phash_redteam:同种子字节级一致、文件名即金标,不触碰
``benchmarks/corpus``;``--corpus`` 可改用 labels.json 标注语料,口径对齐
adversarial)。逐 (σ, 图) 产出平滑预测 / 计数 / CP 界 / 半径,聚合出
**经验认证准确率@σ**(平滑预测正确且半径>0 的比例——注意 σ=0 时半径
恒 0,认证准确率必为 0,这是"零噪声给零半径"的数学诚实而非缺陷)、
平均认证半径(仅对 radius>0 求均值)、逐图半径分布;正类口径对齐
run_benchmark 默认:positive={"nsfw"},borderline 计负类。

**认证半径金标门禁(A239,惯例对齐 adversarial 的单点/曲线金标)**:
复用 A226/A208 的"指纹键 / 容差 / 退出码三态"模式拦截半径回归——

- **指纹键** = sha256(分类器注册名 + 语料指纹 + σ 网格 + K + α +
  :data:`GOLDEN_STATS_VERSION`):合成语料指纹 = 生成器版本+张数+种子;
  标注语料指纹 = ``文件名:sha256`` 集合 + labels.json sha256(内容变化 /
  增删 / 重命名 / 标注变化都换键)。噪声种子不进指纹:stub 全票口径下
  指标是 (σ, K, α) 的闭式函数、与种子无关;真实分类器票分裂时种子
  漂移由容差与 ``--missing-baseline`` 兜底;
- **指标** = 各 σ 的 ``certified_acc`` 与 ``avg_radius``,容差 =
  ``max(10%·|基线|, 0.02)``(对齐曲线金标的固定小容差形态;认证率是
  小语料比例、半径是闭式量,均无推断层 CI 可借);``avg_radius`` 允许
  None(σ=0 或全 ABSTAIN),None↔数值互变即违例;
- **金标文件**(默认 ``benchmarks/out/certify_golden.json``,
  ``--golden`` 可覆盖):``{fingerprint: {"<σ>/<指标>": {value, tol}}}``,
  整体重建式写出,**不含时间戳**——同结果两次重建逐字节一致(与报告
  确定性红线同源);``--update-golden`` 重建并打印中文 diff 摘要,
  ``--gate`` 逐指标比对 ``|当前 − 基线| > 容差`` → 违例清单 → **退出码
  2**;指纹未命中 = 缺基线,``--missing-baseline warn|fail``(默认 warn
  跳过;fail 按违例处理,退出码 2);金标文件损坏 / 结构非法 / 版本不
  兼容 → **退出码 1**(对齐 adversarial 的 GoldenError=1 惯例)。

**确定性红线**:同种子任何次运行,``certify_report.json`` /
``certify_report.md`` **逐字节一致**——因此报告刻意不含时间戳
(adversarial 的 generated_at 惯例在此让位于逐字节可复现;运行时刻属
日志信息,不进确定性产物)。噪声变体字节同样确定:同
``(seed, 图身份, σ, j)`` 恒得同一变体字节流;唯一例外是 ``--gate`` /
``--update-golden`` 模式会在报告中追加 ``gate`` 段(含金标文件路径,
随调用方变化)——门禁模式面向 CI 比对而非归档,逐字节确定性承诺仅对
默认(无门禁)模式生效。

安全红线:本模块只读语料目录,噪声变体全部落临时目录并自动清理;证书
只是"平滑预测在 L2 扰动下不变的**数学下界**证据",任何模型(含 glm)
的分数都只是特征,最终判定与举报须人工确认,证书不得作为自动处置依据。

命令行(退出码 0/1/2 三态,A239 起对齐 adversarial 金标惯例:0 成功
——含门禁通过 / 缺基线 warn 跳过 / 金标重建;1 可预期错误——Pillow 缺失 /
分类器不可用 / 语料问题 / 参数非法 / **金标文件输入错误**(坏 JSON /
结构非法 / 版本不兼容);2 **金标门禁回归违例**;argparse 用法错误仍按
标准行为 SystemExit 2)::

    python benchmarks/certify_bench.py                       # 合成语料 + 默认网格
    python benchmarks/certify_bench.py --sigma 4,8,16 --k 50
    python benchmarks/certify_bench.py --corpus benchmarks/corpus   # labels.json 语料
    python benchmarks/certify_bench.py --out <目录>
    python benchmarks/certify_bench.py --update-golden       # 重建半径金标基线(打印 diff)
    python benchmarks/certify_bench.py --gate                # 金标门禁:回归 → 退出码 2
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import logging
import math
import os
import random
import sys
import tempfile
import types
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 直接以脚本运行(python benchmarks/certify_bench.py)时,保证项目根在
# sys.path 上,使 netsentinel / benchmarks 包可导入;经包导入(tests)时为空操作。
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from netsentinel import telemetry  # noqa: E402
from netsentinel.contracts import Config, ImageEvidence  # noqa: E402
from netsentinel.vision.classifier_base import get_classifier  # noqa: E402
from benchmarks.run_benchmark import BenchmarkError, load_corpus  # noqa: E402

__all__ = [
    "CertifyError",
    "CertifyGoldenError",
    "DEFAULT_ALPHA",
    "DEFAULT_CORPUS_COUNT",
    "DEFAULT_GOLDEN_PATH",
    "DEFAULT_K",
    "DEFAULT_SEED",
    "DEFAULT_SIGMAS",
    "DECIDE_THRESHOLD",
    "FAST_SIGMA_MAX",
    "GATE_METRICS",
    "GOLDEN_SCHEMA",
    "GOLDEN_STATS_VERSION",
    "MISSING_BASELINE_POLICIES",
    "POSITIVE_LABELS",
    "REPORT_SCHEMA",
    "SYNTH_KINDS",
    "SYNTH_SIZES",
    "SYNTH_VERSION",
    "MAX_K",
    "MAX_CORPUS_COUNT",
    "CERTIFY_RADIUS_FORMULA",
    "TOL_MIN",
    "TOL_VALUE_RATIO",
    "binom_upper_tail",
    "build_golden_entry",
    "certified_radius",
    "clopper_pearson_bounds",
    "compute_fingerprint",
    "evaluate_gate",
    "load_golden",
    "metric_tolerance",
    "norm_ppf",
    "save_golden",
    "variant_seed",
    "make_synthetic_corpus",
    "SmoothedClassifier",
    "run",
    "render_markdown",
    "main",
]

logger = logging.getLogger(__name__)

#: GLM 等在线分类器未配置时的环境变量提示名(与 adversarial / A37 一致)。
ENV_GLM_API_KEY = "NETSENTINEL_GLM_API_KEY"

#: 平滑投票的默认采样数 K(Cohen 论文用 10 万做认证;A239 的 LUT 批量
#: 噪声下 K=100 的变体生成是毫秒级,全票场景仍拿 (α/2)^(1/K) ≈ 0.964
#: 的 CP 下界,演示与回归锁定都够用;--k 可调)。
DEFAULT_K = 100

#: K 上限(防 CLI 误给天文数字把纯 Python 逐像素噪声拖到不可用)。
MAX_K = 2000

#: 认证的统计误差预算 α(CP 置信水平 = 1−α = 95%;Cohen 原文用 0.001,
#: 此处取更常规的 0.05 并允许 --alpha 覆盖)。
DEFAULT_ALPHA = 0.05

#: 默认 σ 网格(灰度级;半径量纲 = L2 像素差,同一 σ 网格下可跨 σ 比较)。
DEFAULT_SIGMAS: tuple[float, ...] = (8.0, 16.0, 32.0)

#: 噪声采样全局种子(固定 → 报告逐字节可复现)。
DEFAULT_SEED = 20261043

#: 合成语料默认张数与上限。
DEFAULT_CORPUS_COUNT = 12
MAX_CORPUS_COUNT = 64

#: 合成语料生成器版本(构图/命名口径变化时递增,进报告 method 段)。
SYNTH_VERSION = "cert-syn1"

#: 合成语料构图轮换(文件名关键词即金标:nsfw_hi→nsfw、nsfw_mid→borderline、
#: 其余→clean;与 stub 桩打分规则同源,全链认证率因此可手算)。
SYNTH_KINDS: tuple[str, ...] = ("nsfw_hi", "nsfw_mid", "normal", "normal")

#: 合成语料尺寸轮换(小图控制变体落盘与评分成本;A239 后噪声生成已是
#: C 级 LUT 路径,尺寸不再是噪声耗时瓶颈)。
SYNTH_SIZES: tuple[tuple[int, int], ...] = ((48, 32), (64, 48))

#: 基分类器分数 → 二值类的判决阈值(nsfw_prob ≥ 阈值 → "nsfw")。
DECIDE_THRESHOLD = 0.5

#: 正类口径(对齐 run_benchmark 默认 non-strict:positive={"nsfw"},
#: borderline 计负类——预测 "nsfw" 对 borderline 图记"不正确")。
POSITIVE_LABELS: frozenset[str] = frozenset({"nsfw"})

#: 报告 JSON 结构版本。
REPORT_SCHEMA = "netsentinel-certify-report/1"

#: Cohen 认证半径公式(报告 method 段原样呈现)。
CERTIFY_RADIUS_FORMULA = "r >= sigma/2 * (Phi_inv(p_lower) - Phi_inv(p_upper))"

# ---------------------------------------------------------------------------
# 噪声 LUT 批量引擎(A239):σ 快速路径上限 / LUT 版本
# ---------------------------------------------------------------------------

#: 噪声快速路径的 σ 上限:偏移编码为无符号字节 ``d + 128``(d ∈ [-128, 127]),
#: LUT 最深分位 Φ⁻¹(0.5/256) ≈ −2.8944,不饱和的条件是
#: σ·2.8944 ≤ 127 即 σ ≤ 43.88;取 40 留裕量(边界 σ 下最深偏移 ±116)。
#: σ 超限时 :func:`_noisy_gray` 回退 :func:`_noisy_gray_reference`(逐像素
#: float64 参考路径,与 A227 旧实现逐字节一致)。
FAST_SIGMA_MAX = 40.0

#: LUT 引擎版本(量化口径变化时递增;进报告 method.noise 描述)。
NOISE_LUT_VERSION = "lut-quant1"

#: 浮点比较容差。
_EPS = 1e-12

# ---------------------------------------------------------------------------
# 认证半径金标门禁(A239,惯例对齐 adversarial 的金标三态)
# ---------------------------------------------------------------------------

#: 金标统计口径版本(指标定义 / 容差规则变化时递增,进指纹键)。
GOLDEN_STATS_VERSION = "a239-1"

#: 金标文件结构版本(不兼容演进时递增;读取端见 :func:`load_golden` 预检)。
GOLDEN_SCHEMA = "netsentinel-certify-golden/1"

#: 金标文件默认路径(benchmarks/out/certify_golden.json;--golden 可覆盖)。
DEFAULT_GOLDEN_PATH = Path(__file__).resolve().parent / "out" / "certify_golden.json"

#: 指纹未命中金标(分类器/语料/σ 网格/K/α/统计版本已变化)时的处置策略。
MISSING_BASELINE_POLICIES: frozenset[str] = frozenset({"warn", "fail"})

#: 金标门禁指标:各 σ 的经验认证准确率与平均认证半径(其余统计量只报不拦)。
GATE_METRICS: tuple[str, ...] = ("certified_acc", "avg_radius")

#: 容差中"基线相对比例"分支:tol = max(TOL_VALUE_RATIO·|基线|, TOL_MIN)。
TOL_VALUE_RATIO = 0.10

#: 容差固定下限(半径量纲 = L2 像素差,0.02 灰度级已小于任何可解释漂移)。
TOL_MIN = 0.02

#: 容差规则文本(报告 / CLI 输出原样呈现)。
_TOL_RULE_TEXT = "max(10%·|基线|, 0.02)"


class CertifyError(RuntimeError):
    """认证评测流程中可预期的错误(中文消息;CLI 捕获后以退出码 1 结束)。"""


class CertifyGoldenError(CertifyError):
    """半径金标文件输入错误(损坏 / 坏 JSON / 结构非法 / 版本不兼容)。

    对应 CLI 退出码 1(对齐 adversarial 的 ``AdversarialGoldenError`` /
    ops/audit_verify 的"输入错误"惯例);作为 :class:`CertifyError` 子类,
    既有 ``except CertifyError`` 兜底仍可捕获。
    """

    exit_code = 1


# ---------------------------------------------------------------------------
# Pillow 惰性加载(与 adversarial / phash_redteam 同款策略)
# ---------------------------------------------------------------------------


def _load_pil():
    """惰性导入 ``PIL.Image`` 与 ``PIL.ImageChops``;缺失 / 半初始化返回 None。

    先导入父包 ``PIL`` 再导子模块:测试以 ``sys.modules["PIL"] = None``
    屏蔽 Pillow 时,若只导子模块会命中缓存而漏检,先导父包才能可靠感知
    "不可用"。A239 起噪声快速路径需要 ``ImageChops.add`` 做 C 级混合,
    一并惰性加载并挂在返回的命名空间上。
    """
    try:
        importlib.import_module("PIL")
        image = importlib.import_module("PIL.Image")
        chops = importlib.import_module("PIL.ImageChops")
    except Exception:  # None 注入 / 未安装 / 损坏的 PIL 一律按"未安装"处理
        return None
    return types.SimpleNamespace(Image=image, ImageChops=chops)


def _sha256_of(path: Path) -> str:
    """流式计算文件 sha256(变体证据哈希 / 图身份派生种子用)。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clamp255(value: float) -> int:
    """灰度值截断到 [0, 255](噪声点运算的边界处理,phash _atk_noise 同款)。"""
    return 0 if value < 0 else (255 if value > 255 else int(value))


# ---------------------------------------------------------------------------
# 标准正态分位数 Φ⁻¹:Acklam 有理近似 + 一步 Halley 打磨(纯 stdlib)
# ---------------------------------------------------------------------------


def norm_ppf(p: float) -> float:
    """标准正态分布的分位数函数 Φ⁻¹(p)(Acklam 有理近似,纯 stdlib)。

    精度(docstring 锁定,测试断言):原始 Acklam 有理近似在整个
    (0, 1) 上的最大绝对误差 **< 1.15e-9**(Acklam 2004 实测界);本实现
    再做一步 Halley 打磨(Φ 用 stdlib ``math.erfc``:Φ(x) = erfc(−x/√2)/2,
    pdf φ(x) = exp(−x²/2)/√(2π)),把误差压到浮点舍入量级,实测 < 1e-12;
    测试以已知分位点(0.975 → 1.959963984540054 等)锁定 < 1e-9,
    远严于任务要求的 1e-4。

    - 定义域 (0, 1) 开区间:p ≤ 0 或 p ≥ 1、NaN → ValueError(中文);
    - p = 0.5 → 恰为 0.0;反对称性 Φ⁻¹(1−p) = −Φ⁻¹(p) 在浮点下保持
      ~1e-15(打磨步以各自的 Φ 残差为零点,不做对称捷径)。
    """
    if isinstance(p, bool) or not isinstance(p, (int, float)):
        raise ValueError(f"norm_ppf:p 必须为数值,收到 {p!r}")
    p = float(p)
    if math.isnan(p) or p <= 0.0 or p >= 1.0:
        raise ValueError(f"norm_ppf:p 必须严格落在 (0, 1) 开区间,收到 {p!r}")

    # --- Acklam 有理近似(系数为 Peter Acklam 2004 公布的标准系数) ---
    a = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
    b = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00)
    p_low, p_high = 0.02425, 1.0 - 0.02425

    def _horner(coefs: tuple[float, ...], v: float) -> float:
        """Horner 求值:((c0·v + c1)·v + …)·v + c_last。"""
        total = coefs[0]
        for coef in coefs[1:]:
            total = total * v + coef
        return total

    if p < p_low:  # 下尾
        q = math.sqrt(-2.0 * math.log(p))
        x = _horner(c, q) / (_horner(d, q) * q + 1.0)
    elif p <= p_high:  # 中央区
        q = p - 0.5
        r = q * q
        x = _horner(a, r) * q / (_horner(b, r) * r + 1.0)
    else:  # 上尾(对称性)
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -_horner(c, q) / (_horner(d, q) * q + 1.0)

    # --- 一步 Halley 打磨:解 Φ(x) − p = 0(stdlib math.erfc 提供 Φ) ---
    def _cdf(value: float) -> float:
        return 0.5 * math.erfc(-value / math.sqrt(2.0))

    err = _cdf(x) - p
    pdf = math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)
    if pdf > 0.0:  # |x| 极大时 pdf 下溢,保留 Acklam 原值(< 1.15e-9 已达标)
        u = err / pdf
        x = x - u / (1.0 + 0.5 * x * u)
    return x


# ---------------------------------------------------------------------------
# 精确二项尾与 Clopper-Pearson 界
# (对数空间 lgamma 内核与 netsentinel/decision/conformal.py 的
#  _binom_upper_tail / _clopper_pearson_lower 同款;该函数为下划线私有
#  接口、非公开契约,跨包引用会随重构静默断裂,故本地实现同款算法并在此
#  标注来源,数值口径与已审计实现一致)
# ---------------------------------------------------------------------------


def _sig(x: float) -> float:
    """规整到 12 位有效数字:清除二分尾部浮点噪声,保证输出确定性
    (conformal._sig 同款)。"""
    return float(f"{x:.12g}")


def binom_upper_tail(k: int, n: int, p: float) -> float:
    """精确计算二项上尾概率 P(Bin(n, p) ≥ k)(CP 求根共用,纯 stdlib)。

    对数空间逐项求和:log C(n, j) 经 ``math.lgamma`` 计算(组合数大时
    天文级,直接乘会溢出;lgamma 全程对数无溢出),概率空间累加无非负
    相消。与 ``netsentinel/decision/conformal.py`` 的 ``_binom_upper_tail``
    同款算法(私有接口,本地镜像并注明)。
    """
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    log_p = math.log(p)
    log_q = math.log1p(-p)
    lgam_n1 = math.lgamma(n + 1)
    total = 0.0
    for j in range(k, n + 1):
        log_term = (
            lgam_n1
            - math.lgamma(j + 1)
            - math.lgamma(n - j + 1)
            + j * log_p
            + (n - j) * log_q
        )
        total += math.exp(log_term)  # 极小项下溢为 0.0,对和的贡献确为 ~0
    return min(1.0, total)


def _cp_lower(k: int, n: int, tail: float) -> float:
    """Clopper-Pearson 单侧下界:解尾方程 P(Bin(n, p) ≥ k) = tail。

    尾概率对 p 单调递增(随机单调性),二分必收敛;k = 0 → 0.0(下界贴
    零);k = n → 闭式 tail^(1/n)。返回**偏保守一侧**的 lo(尾概率恒
    < tail),绝不虚高(conformal._clopper_pearson_lower 同款口径)。
    """
    if n <= 0 or k <= 0:
        return 0.0
    if k >= n:
        return _sig(tail ** (1.0 / n))
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if binom_upper_tail(k, n, mid) < tail:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-16:
            break
    return _sig(lo)


def clopper_pearson_bounds(k: int, n: int, alpha: float) -> tuple[float, float]:
    """Clopper-Pearson 精确双侧 (1−α) 置信区间(二项占比 k/n,纯 stdlib)。

    - 下界解 P(Bin(n, p) ≥ k) = α/2;上界解 P(Bin(n, p) ≤ k) = α/2,
      后者等价于 P(Bin(n, p) ≥ k+1) = 1 − α/2。由互补恒等式(证明见
      :func:`certified_radius` docstring)上界可精确表为
      ``U(k) = 1 − L(n−k)``,复用同一个下界求根器,数值口径自洽;
    - 闭式边界:k = 0 → (0, 1 − (α/2)^(1/n));k = n → ((α/2)^(1/n), 1);
    - n ≤ 0、k 越界、α 不在 (0, 0.5) → ValueError(中文)。
    """
    if n <= 0:
        raise ValueError(f"clopper_pearson_bounds:样本数必须为正,收到 n={n}")
    if not 0 <= k <= n:
        raise ValueError(f"clopper_pearson_bounds:成功数 k={k} 超出 [0, {n}]")
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not (
        0.0 < float(alpha) < 0.5
    ):
        raise ValueError(
            f"clopper_pearson_bounds:α 必须严格落在 (0, 0.5) 开区间,收到 {alpha!r}"
        )
    tail = float(alpha) / 2.0
    lower = _cp_lower(k, n, tail)
    upper = 1.0 - _cp_lower(n - k, n, tail)  # U(k) = 1 − L(n−k),见上
    return lower, min(1.0, upper)


def certified_radius(
    n: int,
    k_top1: int,
    k_top2: int,
    sigma: float,
    alpha: float = DEFAULT_ALPHA,
) -> dict[str, Any]:
    """Cohen 认证半径(闭式下界):由经验计数 (n, k_top1, k_top2) 定证书。

    数学(Levine & Feizi / Cohen et al. 定理的 plug-in 形式)::

        p_lower = CP 下界(k_top1, n, α)      # 平滑后 top1 类概率下界
        p_upper = CP 上界(k_top2, n, α)      # 平滑后次类概率上界
        certified ⟺ p_lower > p_upper        # ⟺ Φ⁻¹(p_lower) > Φ⁻¹(p_upper)
        r ≥ σ/2 · (Φ⁻¹(p_lower) − Φ⁻¹(p_upper))

    **为什么不用字面的 "p_lower + p_upper > 1"**:CP 界满足互补对称性
    U(k) = 1 − L(n−k)——对 T(k,p)=P(Bin(n,p)≥k) 与 u=L(n−k) 有
    T(k+1, 1−u) = P(Bin(n,u) ≤ n−k−1) = 1 − T(n−k, u) = 1−α/2,故
    U(k) = 1−L(n−k) 精确成立。于是**二分类(k_top1+k_top2=n)下恒有
    p_upper = 1 − p_lower、p_lower+p_upper ≡ 1**,"和严格大于 1"在任何
    计数下不可达(多分类 k_top1+k_top2≤n 时同理可证和 ≤ 1)。因此采用
    Cohen 原始判据 p_lower > p_upper(等价半径 > 0;二分类下等价
    p_lower > 1/2),任务的两条锁定口径在其下精确成立:

    - 全票 k_top1=n:p_lower=(α/2)^(1/n),p_upper=1−p_lower,
      r = σ/2·(Φ⁻¹(p_lower) − Φ⁻¹(1−p_lower)) = σ/2·2Φ⁻¹(p_lower)
      = σ·Φ⁻¹(p_lower)(Φ⁻¹ 反对称);
    - k ≈ n/2:p_lower < 1/2 < p_upper → ABSTAIN;
    - ABSTAIN ⟺ p_lower ≤ p_upper ⟺ p_lower + p_upper ≤ 1(等号含入
      ABSTAIN,不硬造半径——输出 radius=None)。

    σ = 0:半径恒 0.0(零噪声给零半径,数学诚实);n=1 时全票也不足以
    过半显著((α/2)^1 < 1/2)→ ABSTAIN。非法计数 / σ<0 / α 越界 →
    ValueError(中文)。
    """
    if isinstance(sigma, bool) or not isinstance(sigma, (int, float)) or not (
        float(sigma) >= 0.0
    ):
        raise ValueError(f"certified_radius:sigma 必须为非负数值,收到 {sigma!r}")
    if n < 1:
        raise ValueError(f"certified_radius:采样数 n 必须为正,收到 n={n}")
    if not 0 <= k_top1 <= n or not 0 <= k_top2 <= n:
        raise ValueError(
            f"certified_radius:计数越界 k_top1={k_top1}, k_top2={k_top2}(n={n})"
        )
    if k_top1 < k_top2:
        raise ValueError(
            f"certified_radius:k_top1={k_top1} 不得小于 k_top2={k_top2}"
            "(top1 计数应居多数)"
        )
    if k_top1 + k_top2 > n:
        raise ValueError(
            f"certified_radius:k_top1+k_top2={k_top1 + k_top2} 超出 n={n}"
        )
    sigma = float(sigma)
    p_lower = clopper_pearson_bounds(k_top1, n, float(alpha))[0]  # top1 概率下界
    p_upper = clopper_pearson_bounds(k_top2, n, float(alpha))[1]  # 次类概率上界
    if p_lower > p_upper + _EPS:
        z_lower = norm_ppf(p_lower)
        z_upper = norm_ppf(p_upper)
        radius = sigma / 2.0 * (z_lower - z_upper)
        status = "certified"
    else:
        radius = None  # ABSTAIN:无证书,绝不硬造半径
        status = "abstain"
    return {
        "n": n,
        "k_top1": k_top1,
        "k_top2": k_top2,
        "sigma": sigma,
        "alpha": float(alpha),
        "p_lower": p_lower,
        "p_upper": p_upper,
        "p_lower_plus_p_upper": _sig(p_lower + p_upper),
        "binary_complement": k_top1 + k_top2 == n,
        "radius": radius,
        "status": status,
    }


# ---------------------------------------------------------------------------
# 确定性噪声变体(A239:分位数 LUT 批量引擎;参考路径保留;种子派生与调用顺序无关)
# ---------------------------------------------------------------------------


def variant_seed(seed: int, image_key: str, sigma: float, j: int) -> int:
    """派生单个噪声变体的确定性种子(sha256 → 31bit,与调用顺序无关)。

    ``image_key`` 为图片身份键(合成语料用图序、标注语料用文件内容
    sha256 前缀):同 ``(seed, image_key, sigma, j)`` 恒得同一种子 → 同
    输出字节;不同 j / 不同 σ / 不同图互不干扰。phash_redteam 的
    ``variant_seed`` 用算术派生,这里因为 image_key 是字符串改用哈希
    派生,确定性语义相同。
    """
    basis = f"{seed}|{image_key}|{sigma:.6g}|{j}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(basis).digest()[:8], "big") % (2**31)


#: σ → 256 项偏移 LUT 的进程级缓存(LUT 只依赖 σ,可安全复用;构建
#: 仅需 256 次 norm_ppf 调用,首次用到某 σ 时构建一次)。
_NOISE_LUT_CACHE: dict[float, bytes] = {}


def _noise_lut(sigma: float) -> bytes:
    """构建 / 取缓存的 σ 噪声偏移 LUT(256 项,``bytes.translate`` 输入端)。

    **分层分位数映射(inverse-CDF stratification)**:对索引字节
    b ∈ [0, 256),取 u = (b + 0.5)/256(该字节层带的中心分位),偏移

        d(b) = round(Φ⁻¹(u) · σ)   (Φ⁻¹ 复用本模块 Acklam+Halley 内核)

    编码为无符号字节 ``d + 128``。于是**均匀随机索引字节 → 严格按正态
    分位分层的整数偏移**:索引流均匀 ⇔ 偏移分布恰为 N(0, σ²) 的 256 层
    分位离散化(均值精确 0、标准差 ≈ σ,实测 σ=16 时 std=15.955;A227
    旧实现经 ``round(gauss)`` 得到的同样是整数偏移的离散正态,两者口径
    同族)。确定性:同 σ 恒得同一 LUT(norm_ppf 纯函数),缓存不影响
    语义;σ=0 时全表 128(零偏移直通)。
    """
    cached = _NOISE_LUT_CACHE.get(sigma)
    if cached is not None:
        return cached
    table = bytearray(256)
    for b in range(256):
        d = round(norm_ppf((b + 0.5) / 256.0) * sigma)
        # 编码域 [-128, 127];FAST_SIGMA_MAX 内保证不饱和(见常量注释)
        if d < -128:
            d = -128
        elif d > 127:
            d = 127
        table[b] = d + 128
    lut = bytes(table)
    _NOISE_LUT_CACHE[sigma] = lut
    return lut


def _noisy_gray(pil: Any, gray: Any, sigma: float, vseed: int) -> Any:
    """对灰度图叠加确定性 N(0, σ²) 噪声,返回新图(不改入参)。

    **A239 批量 LUT 引擎**(σ ≤ :data:`FAST_SIGMA_MAX`):全 C 级管线——

    1. ``random.Random(vseed).randbytes(w·h)`` 一次调用批量取均匀索引
       字节(每像素一个,与参考路径同一 ``vseed`` 派生体系);
    2. ``bytes.translate(_noise_lut(σ))`` 把索引字节逐个映射为分层分位
       高斯偏移(编码为 ``d + 128``);
    3. ``Image.frombytes`` 落噪声图 + ``ImageChops.add(gray, 噪声, 1,
       -128)`` 做 ``clamp(v + d)`` 的 C 级混合(输出像素仅依赖同位置
       输入像素,保持 PIL 点运算语义)。

    实测 64×48 图约 **45×** 加速(1046µs → 23µs/张,Python 3.14.6 /
    Pillow 12.3.0);σ=0 时 LUT 全 128 → 输出与输入**逐字节一致**
    (零噪声退化语义精确保持)。

    **与 A227 旧实现(逐像素 ``rng.gauss`` float64 流)的一致性论证**
    (评审必读,任务允许的"同分布新种子体系"路线):

    - 逐字节等价 + ≥5× 加速在纯 Python 下**不可同时达成**:CPython
      3.14 实测旧内核 331ns/像素(gauss 调用本身 194ns),5× 预算
      66ns/像素低于单次 ``round``+加法的解释器开销;即便用
      ``randbytes`` 批量复现同一 MT 字流(已验证可逐位复现
      ``random()``/``gauss`` 序列)再按对展开 Box-Muller,数学函数的
      逐像素 Python 调用仍使总成本高于旧内核(实测 0.86×)。
      故取任务书明确允许的回退方案:同分布(分层分位离散化)新种子
      体系,并在此注明;
    - 分布等价:偏移均值精确 0、std ≈ σ(量化到 1 灰度级,≤ 40σ 范围
      不饱和),认证半径公式只消费 σ 的解析值,不受量化影响;
    - 旧实现保留为 :func:`_noisy_gray_reference`(σ > :data:`FAST_
      SIGMA_MAX` 的回退路径 + 等价性测试的金标对照);测试锁定:σ=0
      新旧输出与原图三方逐字节一致、σ 超限走参考路径逐字节一致、
      同种子字节确定、大样本均值/方差逼近 (0, σ²)。
    """
    if sigma > FAST_SIGMA_MAX:
        return _noisy_gray_reference(pil, gray, sigma, vseed)
    rng = random.Random(vseed)
    width, height = gray.size
    noise_offsets = rng.randbytes(width * height).translate(_noise_lut(sigma))
    noise_image = pil.Image.frombytes("L", (width, height), noise_offsets)
    return pil.ImageChops.add(gray, noise_image, 1, -128)


def _noisy_gray_reference(pil: Any, gray: Any, sigma: float, vseed: int) -> Any:
    """A227 旧噪声内核(逐像素 ``rng.gauss`` float64 + putdata 落图)。

    保留目的:①σ 超出 :data:`FAST_SIGMA_MAX`(偏移编码域 [-128, 127]
    不再无饱和)时的确定性回退;②等价性测试的金标对照(σ=0 与快速
    路径逐字节一致)。噪声由 ``random.Random(vseed)`` 确定性逐像素采样
    (phash_redteam._atk_noise 同款内核);σ=0 时 gauss 采样恒为 0 →
    输出与输入逐像素一致。兼容 Pillow 12+ 的 getdata 弃用。
    """
    rng = random.Random(vseed)
    getter = getattr(gray, "get_flattened_data", None) or gray.getdata
    noisy = pil.Image.new("L", gray.size)
    noisy.putdata([
        _clamp255(int(round(v + rng.gauss(0.0, sigma)))) for v in getter()
    ])
    return noisy


def _classify_all(classifier: Any, evidences: list[ImageEvidence]) -> list[float]:
    """对全部证据评分,返回与输入同序的 nsfw_prob 列表。

    对齐 adversarial 的 ``_classify_all`` 形态:优先走分类器协议的批量
    接口 ``classify_batch``(整图 K 个变体合并一次调用),协议不支持或
    返回长度不符时回退逐张,结果不变。
    """
    batch_fn = getattr(classifier, "classify_batch", None)
    if callable(batch_fn):
        batch = batch_fn(evidences)
        if isinstance(batch, list) and len(batch) == len(evidences):
            return [float(score.nsfw_prob) for score in batch]
        logger.warning(
            "classify_batch 返回长度不符(期望 %d),回退逐张 classify",
            len(evidences),
        )
    return [float(classifier.classify(ev).nsfw_prob) for ev in evidences]


class SmoothedClassifier:
    """随机化平滑分类器包装(基分类器注入,不依赖注册表)。

    对每张输入图:生成 K 个 σ 高斯噪声变体(确定性种子派生,文件名保留
    原 stem → stub 桩规则不断链)→ 基分类器批量评分 → 分数 ≥
    ``threshold`` 记 "nsfw" 票否则 "clean" 票 → 多数投票得平滑预测与
    计数 ``(n=K, k_top1)``(二分类:k_top2 = K − k_top1)→ CP 界 →
    Cohen 认证半径 / ABSTAIN(:func:`certified_radius`)。

    平票(K 为偶数且各半)按保守方向判 "clean",且此时 p_lower 必
    < 1/2 → 必然 ABSTAIN,平票裁决不影响认证语义。σ=0 时全部变体与
    输入逐像素一致 → 平滑预测 ≡ 基分类器预测(退化情形)。
    """

    def __init__(
        self,
        base: Any,
        *,
        sigma: float,
        k: int = DEFAULT_K,
        seed: int = DEFAULT_SEED,
        alpha: float = DEFAULT_ALPHA,
        threshold: float = DECIDE_THRESHOLD,
    ) -> None:
        if isinstance(sigma, bool) or not isinstance(sigma, (int, float)) or float(sigma) < 0.0:
            raise ValueError(f"sigma 必须为非负数值,收到 {sigma!r}")
        if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= MAX_K:
            raise ValueError(f"k 必须为 1~{MAX_K} 的整数,收到 {k!r}")
        if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not (
            0.0 < float(alpha) < 0.5
        ):
            raise ValueError(f"alpha 必须严格落在 (0, 0.5) 开区间,收到 {alpha!r}")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not (
            0.0 < float(threshold) < 1.0
        ):
            raise ValueError(f"threshold 必须严格落在 (0, 1) 开区间,收到 {threshold!r}")
        self.base = base
        self.sigma = float(sigma)
        self.k = k
        self.seed = seed
        self.alpha = float(alpha)
        self.threshold = float(threshold)

    def classify_smoothed(
        self,
        image_path: str | Path,
        variant_dir: str | Path,
        *,
        image_key: str | None = None,
    ) -> dict[str, Any]:
        """对单张图执行"K 变体 → 投票 → 认证",返回一行结果 dict。

        - ``variant_dir``:变体 PNG 落盘目录(调用方管理生命周期,本方法
          只写入 ``<stem>__smooth_<j>.png``,不清理);变体必须落盘——
          基分类器协议以 ``ImageEvidence.path`` 为输入;
        - ``image_key``:图片身份键(缺省取文件内容 sha256 前 16 位),
          参与种子派生 → 同图同参数必得同变体字节;
        - 返回键:file / sigma / n / top_class / k_top1 / k_top2 /
          p_lower / p_upper / radius(None=ABSTAIN)/ status /
          votes_nsfw;Pillow 缺失 / 解码失败 → :class:`CertifyError`。
        """
        pil = _load_pil()
        if pil is None:
            raise CertifyError(
                "未安装 Pillow,无法生成噪声变体(pip install Pillow 后重试;"
                "本基准以退出码 1 结束,不产出无效报告)"
            )
        src = Path(image_path)
        try:
            with pil.Image.open(src) as opened:
                opened.load()
                gray = opened.convert("L")
        except (OSError, ValueError) as exc:
            raise CertifyError(f"无法解码图片:{src}({exc})") from exc

        key = image_key if image_key is not None else _sha256_of(src)[:16]
        out = Path(variant_dir)
        out.mkdir(parents=True, exist_ok=True)
        stem = src.stem
        variant_paths: list[Path] = []
        for j in range(self.k):
            vseed = variant_seed(self.seed, key, self.sigma, j)
            noisy = _noisy_gray(pil, gray, self.sigma, vseed)
            path = out / f"{stem}__smooth_{j:03d}.png"
            noisy.save(path, format="PNG", compress_level=1)
            variant_paths.append(path)

        evidences = [
            ImageEvidence(
                path=str(path),
                url=f"file://{path}#smooth:{self.sigma:g}:{j}",
                source_page=f"certify://{stem}",
                sha256=_sha256_of(path),
                width=gray.size[0],
                height=gray.size[1],
            )
            for j, path in enumerate(variant_paths)
        ]
        probs = _classify_all(self.base, evidences)
        votes_nsfw = sum(1 for prob in probs if prob >= self.threshold)
        votes_clean = self.k - votes_nsfw
        if votes_nsfw > votes_clean:
            top_class = "nsfw"
        else:
            # 次多数或平票:平票按保守方向判 clean(此时必 ABSTAIN,见类 docstring)
            top_class = "clean"
        k_top1 = max(votes_nsfw, votes_clean)
        k_top2 = self.k - k_top1

        verdict = certified_radius(self.k, k_top1, k_top2, self.sigma, self.alpha)
        return {
            "file": src.name,
            "sigma": self.sigma,
            "n": self.k,
            "top_class": top_class,
            "k_top1": k_top1,
            "k_top2": k_top2,
            "votes_nsfw": votes_nsfw,
            "p_lower": verdict["p_lower"],
            "p_upper": verdict["p_upper"],
            "radius": verdict["radius"],
            "status": verdict["status"],
        }


# ---------------------------------------------------------------------------
# 合成语料(确定性生成器 + 文件名金标,惯例对齐 phash_redteam)
# ---------------------------------------------------------------------------


def _synthetic_image(pil: Any, index: int, width: int, height: int, seed: int):
    """按图序构造确定性 RGB 合成图(构图轮换:渐变 / 条纹 / 棋盘 / 种子矩形)。

    构图只需"图间字节互异且可复现",无频率结构要求(平滑认证看的是分类
    器输出,不是指纹);同 ``(index, seed)`` 任何次运行逐字节一致。
    """
    rng = random.Random(seed * 1_000_003 + index)
    kind = index % 4
    base_r = 40 + (index * 37) % 180
    base_g = 60 + rng.randrange(120)
    base_b = 90 + (index * 53) % 140
    if kind == 3:
        # 种子随机色块:8x8 格点随机色,块内填充(块状纹理,零随机顺序依赖)
        block = 8
        lattice = [
            (rng.randrange(256), rng.randrange(256), rng.randrange(256))
            for _ in range(((width + block - 1) // block) * ((height + block - 1) // block))
        ]
    pixels: list[tuple[int, int, int]] = []
    for y in range(height):
        for x in range(width):
            if kind == 0:  # 双向渐变
                pixel = (
                    (base_r + x * 255 // max(1, width - 1)) & 0xFF,
                    (base_g + y * 255 // max(1, height - 1)) & 0xFF,
                    base_b,
                )
            elif kind == 1:  # 竖条纹
                flag = (x // 4) % 2
                pixel = (base_r if flag else 255 - base_r, base_g, base_b if flag else 255 - base_b)
            elif kind == 2:  # 棋盘
                cell = 4
                flag = ((x // cell) + (y // cell)) % 2
                pixel = (
                    (base_r, base_g, base_b) if flag
                    else (255 - base_r, 255 - base_g, 255 - base_b)
                )
            else:  # 种子随机色块(块内同色)
                block = 8
                cols = (width + block - 1) // block
                pixel = lattice[(y // block) * cols + (x // block)]
            pixels.append((pixel[0] & 0xFF, pixel[1] & 0xFF, pixel[2] & 0xFF))
    image = pil.Image.new("RGB", (width, height))
    image.putdata(pixels)
    return image


def make_synthetic_corpus(
    out_dir: str | Path,
    count: int = DEFAULT_CORPUS_COUNT,
    seed: int = DEFAULT_SEED,
) -> list[dict[str, Any]]:
    """确定性生成合成语料(PNG 落盘),返回清单(按图序,含金标 label)。

    - 文件名即金标:``cs_nsfw_hi_XXX.png`` → label "nsfw"、
      ``cs_nsfw_mid_XXX.png`` → "borderline"、``cs_normal_XXX.png`` →
      "clean"(与 stub 桩打分规则同源 → 全链认证率可手算);
    - 构图按 ``SYNTH_KINDS`` 轮换、尺寸按 ``SYNTH_SIZES`` 交替,PNG 以
      ``compress_level=1`` 落盘;同 ``(count, seed)`` 任何次运行产物逐字节
      一致(确定性测试据此断言);
    - count 非法(非正整数 / 超过 :data:`MAX_CORPUS_COUNT`)→ ValueError
      (中文)。
    """
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count < 1
        or count > MAX_CORPUS_COUNT
    ):
        raise ValueError(f"count 无效:须为 1~{MAX_CORPUS_COUNT} 的整数,得到 {count!r}")
    pil = _load_pil()
    if pil is None:
        raise CertifyError(
            "未安装 Pillow,无法生成合成语料(pip install Pillow 后重试;"
            "本基准以退出码 1 结束,不产出无效报告)"
        )
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    for index in range(count):
        width, height = SYNTH_SIZES[index % len(SYNTH_SIZES)]
        kind = SYNTH_KINDS[index % len(SYNTH_KINDS)]
        label = {"nsfw_hi": "nsfw", "nsfw_mid": "borderline"}.get(kind, "clean")
        name = f"cs_{kind}_{index:03d}.png"
        path = out / name
        _synthetic_image(pil, index, width, height, seed).save(
            path, format="PNG", compress_level=1
        )
        manifest.append(
            {
                "index": index,
                "name": name,
                "path": str(path),
                "label": label,
                "kind": kind,
                "width": width,
                "height": height,
            }
        )
    logger.debug("合成语料生成完毕:%s(%d 张,seed=%d)", out, count, seed)
    return manifest


# ---------------------------------------------------------------------------
# 分类器解析(惰性 + 中文指引 + glm 配置预检,口径对齐 adversarial)
# ---------------------------------------------------------------------------


def _build_classifier(classifier_name: str, cfg: Config):
    """经工厂惰性获取分类器;失败抛 :class:`CertifyError`(中文指引)。

    glm 等在线分类器需显式配置(vlm_online + 密钥)才允许评测,未配置
    直接拒绝——绝不静默产出无效报告(与 adversarial 口径一致)。
    """
    if classifier_name == "glm":
        reasons: list[str] = []
        if not cfg.vlm_online:
            reasons.append("cfg.vlm_online=False(默认安全态:图像数据不出本机)")
        api_key = cfg.glm_api_key or os.environ.get(ENV_GLM_API_KEY, "")
        if not api_key:
            reasons.append(f"未配置 glm_api_key,环境变量 {ENV_GLM_API_KEY} 也为空")
        if reasons:
            raise CertifyError(
                "GLM 认证评测未配置就绪:" + ";".join(reasons)
                + "。离线验证框架请改用 --classifier stub;确需在线评测,请在"
                "配置中设置 vlm_online: true 并提供 glm_api_key(红线:平滑"
                "认证只是数学下界证据,最终判定与举报仍须人工确认)。"
            )
    try:
        return get_classifier(classifier_name, cfg)
    except ValueError as exc:
        raise CertifyError(
            f"无法创建分类器 '{classifier_name}':{exc}\n"
            "提示:离线验证框架请用 --classifier stub(开箱即用);"
            "glm 需配置 vlm_online 与 glm_api_key;"
            "nudenet / clip 需安装对应可选依赖(pip install '.[vision]' / '.[clip]')。"
        ) from exc


# ---------------------------------------------------------------------------
# 认证半径金标门禁(A239):指纹键 / 容差 / 读写 / 逐指标比对
# (惯例对齐 adversarial 的单点金标:三态退出码 + fail-safe 缺基线处置)
# ---------------------------------------------------------------------------


def _fingerprint_digest(
    classifier_name: str,
    corpus_identity: dict[str, Any],
    sigmas: tuple[float, ...],
    k: int,
    alpha: float,
) -> str:
    """按"分类器注册名 + 语料指纹 + σ 网格 + K + α + 统计版本"取指纹。

    - 语料指纹:合成语料 = 生成器版本 + 张数 + 种子;标注语料 =
      ``文件名:sha256`` 集合 + labels.json 自身 sha256(内容变化、增删、
      重命名、标注调整都换键);
    - 噪声种子不进键:stub 全票口径下各指标是 (σ, K, α) 的闭式函数、与
      种子无关;真实分类器票分裂时种子漂移由容差兜底;
    - 规范化 JSON(sort_keys + 紧凑分隔符)再 sha256,任何机器逐位一致。
    """
    basis = {
        "classifier": classifier_name,
        "corpus": corpus_identity,
        "sigmas": [float(s) for s in sigmas],
        "k": int(k),
        "alpha": float(alpha),
        "stats_version": GOLDEN_STATS_VERSION,
    }
    canonical = json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_fingerprint(
    classifier_name: str,
    corpus_dir: str | os.PathLike[str],
    *,
    sigmas: tuple[float, ...] | list[float] = DEFAULT_SIGMAS,
    k: int = DEFAULT_K,
    alpha: float = DEFAULT_ALPHA,
) -> str:
    """对外便捷入口:装载标注语料并计算半径金标指纹键(语料非法透传
    :class:`BenchmarkError`,与 adversarial 的同名惯例一致)。"""
    pairs = load_corpus(corpus_dir)
    identity = {
        "source": "labeled",
        "files": sorted(f"{Path(ev.path).name}:{ev.sha256}" for ev, _ in pairs),
        "labels_sha256": _sha256_of(Path(corpus_dir) / "labels.json"),
    }
    return _fingerprint_digest(classifier_name, identity, tuple(sigmas), k, alpha)


def metric_tolerance(baseline: float) -> float:
    """单个半径金标指标的容差 = ``max(10%·|基线|, 0.02)``。

    对齐 adversarial 曲线金标的固定小容差形态:认证率 / 平均半径都是
    stub 确定性下的闭式量,无推断层 CI 可借;10% 比例分支容纳半径随
    K 的合法微调,0.02 下限拦住基线≈0 时的任何漂移。基线非法(非有限
    数值)→ ValueError(中文)。
    """
    value = float(baseline)
    if not math.isfinite(value):
        raise ValueError(f"metric_tolerance:基线必须为有限数值,收到 {baseline!r}")
    return max(abs(value) * TOL_VALUE_RATIO, TOL_MIN)


def build_golden_entry(stats: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """由 payload 的 stats 构建金标条目 ``{"<σ:g>/<指标>": {value, tol}}``。

    ``avg_radius`` 允许 None(σ=0 或全 ABSTAIN 无证书):记
    ``{"value": None, "tol": None}``,互变语义由 :func:`evaluate_gate`
    单独裁决(与 adversarial 曲线金标的 min_effective_eps 同款)。
    """
    entry: dict[str, dict[str, Any]] = {}
    for row in stats:
        key_sigma = f"{float(row['sigma']):g}"
        acc = float(row["certified_acc"])
        entry[f"{key_sigma}/certified_acc"] = {
            "value": round(acc, 4),
            "tol": round(metric_tolerance(acc), 6),
        }
        radius = row["avg_radius"]
        if radius is None:
            entry[f"{key_sigma}/avg_radius"] = {"value": None, "tol": None}
        else:
            radius = float(radius)
            entry[f"{key_sigma}/avg_radius"] = {
                "value": round(radius, 4),
                "tol": round(metric_tolerance(radius), 6),
            }
    return entry


def _golden_spec_valid(spec: Any) -> bool:
    """金标指标条目结构校验:``{value, tol}`` 且 tol=None 当且仅当
    value=None(None 基线无容差概念);数值基线的 tol 须为非负非 bool 实数。"""
    if not isinstance(spec, dict) or set(spec) != {"value", "tol"}:
        return False
    value, tol = spec["value"], spec["tol"]
    if value is None:
        return tol is None
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isinstance(tol, (int, float))
        and not isinstance(tol, bool)
        and float(tol) >= 0.0
    )


def load_golden(path: str | os.PathLike[str]) -> dict[str, Any]:
    """读半径金标文件并做结构校验;文件不存在 → ``{}``(空金标,缺基线路径)。

    损坏 / 坏 JSON / 顶层非对象 / 版本不兼容 / 指标表结构非法 /
    ``{value, tol}`` 形态非法 → :class:`CertifyGoldenError`(中文;CLI
    退出码 1)。``_`` 前缀键为元数据,不参与指纹比对。
    """
    golden = Path(path)
    if not golden.exists():
        return {}
    try:
        raw = json.loads(golden.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CertifyGoldenError(
            f"半径金标文件无法解析:{golden}({exc});请修复或删除后用"
            " --update-golden 重建"
        ) from exc
    if not isinstance(raw, dict):
        raise CertifyGoldenError(
            f"半径金标文件顶层必须是对象(fingerprint → 指标表),当前为"
            f" {type(raw).__name__}:{golden}"
        )
    schema = raw.get("_schema")
    if schema is not None and schema != GOLDEN_SCHEMA:
        raise CertifyGoldenError(
            f"半径金标文件版本不兼容:期望 {GOLDEN_SCHEMA},实际 {schema!r};"
            "请 --update-golden 重建"
        )
    for key, entry in raw.items():
        if key.startswith("_"):
            continue
        if not isinstance(entry, dict) or not entry:
            raise CertifyGoldenError(
                f"半径金标文件结构非法:指纹 {key[:8]}… 的指标表缺失或为空"
                '(应为 "<σ>/<certified_acc|avg_radius>": {value, tol})'
            )
        for metric, spec in entry.items():
            if (
                not isinstance(metric, str)
                or "/" not in metric
                or not _golden_spec_valid(spec)
            ):
                raise CertifyGoldenError(
                    f"半径金标文件结构非法:指纹 {key[:8]}… 的指标 {metric!r} "
                    "应为 {value: 数值或 None, tol: 非负数值或 None}"
                    "(tol=None 当且仅当 value=None)"
                )
    return raw


def save_golden(
    path: str | os.PathLike[str],
    fingerprint: str,
    entry: dict[str, dict[str, Any]],
    *,
    classifier_name: str | None = None,
    corpus_desc: str | None = None,
) -> None:
    """写出半径金标文件(整体重建:只保留本次指纹的条目,父目录自动创建)。

    元数据(``_`` 前缀键)记录结构版本 / 统计版本 / 容差规则 / 重建依据;
    **刻意不含时间戳**——同结果两次重建逐字节一致(与报告确定性红线
    同源,区别于 adversarial 金标的 ``_updated_at`` 惯例),
    ``sort_keys=True`` 使指纹(十六进制)恒排在 ``_`` 键后,输出逐字节
    稳定。
    """
    data = {
        "_schema": GOLDEN_SCHEMA,
        "_stats_version": GOLDEN_STATS_VERSION,
        "_tolerance_rule": _TOL_RULE_TEXT,
        "_basis": {
            "classifier": classifier_name,
            "corpus": corpus_desc,
            "metrics": list(GATE_METRICS),
        },
        fingerprint: entry,
    }
    golden = Path(path)
    if golden.parent and not golden.parent.exists():
        golden.parent.mkdir(parents=True, exist_ok=True)
    golden.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def evaluate_gate(
    stats: list[dict[str, Any]], golden_entry: dict[str, Any]
) -> list[dict[str, Any]]:
    """逐指标比对 ``|当前 − 基线| > 容差``,返回违例清单(中文 message)。

    - 比对对象:每个 σ × :data:`GATE_METRICS`(认证准确率 + 平均半径);
    - 基线缺该指标键(统计版本过旧)→ 按"指标缺基线"违例,宁可拦截
      也不静默跳过(fail-safe 方向);
    - ``avg_radius`` 的 None ↔ 数值互变(σ=0 行为退化 / 恢复)按
      "类型互变"违例——None 无数值容差可言(对齐曲线金标惯例);
    - 违例项含指标名 / 基线 / 当前 / 容差 / 偏差五要素,中文 message 直印。
    """
    violations: list[dict[str, Any]] = []
    for row in stats:
        key_sigma = f"{float(row['sigma']):g}"
        for metric in GATE_METRICS:
            key = f"{key_sigma}/{metric}"
            spec = golden_entry.get(key)
            current = row[metric]
            if spec is None:
                violations.append(
                    {
                        "kind": "missing_metric",
                        "metric": key,
                        "sigma": row["sigma"],
                        "metric_name": metric,
                        "baseline": None,
                        "current": current,
                        "tol": None,
                        "delta": None,
                        "message": (
                            f"σ={key_sigma}·{metric}:金标缺该指标基线"
                            "(统计版本过旧?),请 --update-golden 重建"
                        ),
                    }
                )
                continue
            baseline = spec["value"]
            if (baseline is None) != (current is None):
                violations.append(
                    {
                        "kind": "type_change",
                        "metric": key,
                        "sigma": row["sigma"],
                        "metric_name": metric,
                        "baseline": baseline,
                        "current": current,
                        "tol": None,
                        "delta": None,
                        "message": (
                            f"σ={key_sigma}·{metric}:基线 {baseline} ↔ 当前"
                            f" {current} 类型互变(数值↔None),按违例处理;"
                            "确认变更合理后请 --update-golden 重建金标"
                        ),
                    }
                )
                continue
            if baseline is None and current is None:
                continue  # 双 None:同为无证书,天然一致
            baseline_f = float(baseline)
            current_f = float(current)
            tol = float(spec["tol"])
            delta = abs(current_f - baseline_f)
            if delta > tol + _EPS:
                violations.append(
                    {
                        "kind": "metric_violation",
                        "metric": key,
                        "sigma": row["sigma"],
                        "metric_name": metric,
                        "baseline": baseline_f,
                        "current": current_f,
                        "tol": tol,
                        "delta": round(delta, 6),
                        "message": (
                            f"σ={key_sigma}·{metric}:基线 {baseline_f:.4f} →"
                            f" 当前 {current_f:.4f},偏差 {delta:.4f} 超出容差"
                            f" {tol:.4f}"
                        ),
                    }
                )
    return violations


def _missing_baseline_violation(fingerprint: str, golden: Path) -> dict[str, Any]:
    """构造"指纹缺基线"违例(missing_baseline=fail 时按回归违例处理)。"""
    return {
        "kind": "missing_baseline",
        "metric": None,
        "sigma": None,
        "metric_name": None,
        "baseline": None,
        "current": None,
        "tol": None,
        "delta": None,
        "fingerprint": fingerprint,
        "message": (
            f"指纹 {fingerprint[:12]}… 在金标 {golden} 中无基线(分类器/"
            "语料/σ网格/K/α或统计版本已变化),missing_baseline=fail 按违例"
            "处理;确认变更合理后请先 --update-golden 重建金标"
        ),
    }


def _update_diff_lines(
    old_raw: dict[str, Any], new_fingerprint: str, new_entry: dict[str, Any]
) -> list[str]:
    """--update-golden 的变更 diff 摘要(中文行,CLI 直印留痕,对齐 adversarial)。"""
    lines: list[str] = []
    old_fps = [key for key in old_raw if not key.startswith("_")]
    if new_fingerprint in old_raw:
        old_entry = old_raw[new_fingerprint]
        changed: list[str] = []
        unchanged = 0
        for key, spec in new_entry.items():
            old_spec = old_entry.get(key)
            if old_spec is None:
                changed.append(f"{key}:新增(值 {spec['value']})")
            elif old_spec["value"] is None or spec["value"] is None:
                if old_spec["value"] != spec["value"]:
                    changed.append(f"{key}:{old_spec['value']} → {spec['value']}")
                else:
                    unchanged += 1
            elif abs(float(spec["value"]) - float(old_spec["value"])) > _EPS:
                changed.append(
                    f"{key}:{float(old_spec['value']):.4f} → {float(spec['value']):.4f}"
                )
            else:
                unchanged += 1
        removed = [key for key in old_entry if key not in new_entry]
        lines.append(
            f"指纹 {new_fingerprint[:12]}…:基线刷新({unchanged} 项持平,"
            f"{len(changed)} 项变化,{len(removed)} 项移除)"
        )
        lines.extend(f"  变更 {line}" for line in changed)
        lines.extend(f"  移除 {key}(当前统计不再产出该指标)" for key in removed)
    else:
        lines.append(
            f"指纹 {new_fingerprint[:12]}…:新增基线({len(new_entry)} 项指标,"
            f"容差={_TOL_RULE_TEXT})"
        )
    for gone in (key for key in old_fps if key != new_fingerprint):
        lines.append(
            f"指纹 {gone[:12]}…:移除(与当前分类器/语料/参数不再匹配,"
            "金标按本次结果整体重建)"
        )
    return lines


def _gate_section(
    payload: dict[str, Any],
    *,
    classifier_name: str,
    corpus_identity: dict[str, Any],
    sigmas: tuple[float, ...],
    k: int,
    alpha: float,
    golden_path: str | os.PathLike[str],
    update_golden: bool,
    missing_baseline: str,
) -> dict[str, Any]:
    """执行半径金标门禁并把结果挂到 ``payload["gate"]``(由 :func:`run` 调用)。

    - ``update_golden=True``:重建优先于比对(先建基线,随后可直接
      ``--gate`` 复跑);中文 diff 摘要存入 ``update_diff``;
    - 比对模式:指纹命中 → 逐指标 :func:`evaluate_gate`;未命中 → 缺基线,
      warn(默认)记 warnings 不拦截,fail 记违例(退出码 2)。
    """
    if missing_baseline not in MISSING_BASELINE_POLICIES:
        raise CertifyError(
            f"missing_baseline 取值非法:{missing_baseline!r}"
            f"(允许:{sorted(MISSING_BASELINE_POLICIES)})"
        )
    golden = Path(golden_path)
    fingerprint = _fingerprint_digest(classifier_name, corpus_identity, sigmas, k, alpha)
    raw = load_golden(golden)  # 不存在 → {};损坏 → CertifyGoldenError(退出码 1)

    section: dict[str, Any] = {
        "mode": "update" if update_golden else "check",
        "golden_path": str(golden),
        "fingerprint": fingerprint,
        "missing_baseline": missing_baseline,
        "tolerance_rule": _TOL_RULE_TEXT,
        "metrics_total": len(payload["stats"]) * len(GATE_METRICS),
    }

    if update_golden:
        entry = build_golden_entry(payload["stats"])
        diff = _update_diff_lines(raw, fingerprint, entry)
        save_golden(
            golden,
            fingerprint,
            entry,
            classifier_name=classifier_name,
            corpus_desc=corpus_identity.get("source", None),
        )
        telemetry.inc("certify.golden.updates")
        section.update(
            {"status": "updated", "metrics_recorded": len(entry), "update_diff": diff}
        )
        return section

    entry = raw.get(fingerprint)
    if entry is None:
        warning = (
            f"指纹 {fingerprint[:12]}… 在金标 {golden} 中缺基线"
            "(分类器/语料/σ网格/K/α或统计版本已变化),本次按警告跳过比对;"
            "确认变更合理后请 --update-golden 重建金标"
        )
        if missing_baseline == "fail":
            section["status"] = "violations"
            section["violations"] = [_missing_baseline_violation(fingerprint, golden)]
        else:
            section["status"] = "no_baseline"
            section["warnings"] = [warning]
        return section

    violations = evaluate_gate(payload["stats"], entry)
    if violations:
        telemetry.inc("certify.gate.violations", amount=len(violations))
    section["status"] = "violations" if violations else "pass"
    section["violations"] = violations
    return section


# ---------------------------------------------------------------------------
# 主评测流程:语料 × σ 网格 × K → 报告(JSON + markdown)
# ---------------------------------------------------------------------------


def run(
    out_dir: str | os.PathLike[str],
    *,
    corpus_dir: str | os.PathLike[str] | None = None,
    classifier: str = "stub",
    sigmas: tuple[float, ...] | list[float] = DEFAULT_SIGMAS,
    k: int = DEFAULT_K,
    seed: int = DEFAULT_SEED,
    alpha: float = DEFAULT_ALPHA,
    synthetic_count: int = DEFAULT_CORPUS_COUNT,
    gate: bool = False,
    golden_path: str | os.PathLike[str] = DEFAULT_GOLDEN_PATH,
    update_golden: bool = False,
    missing_baseline: str = "warn",
) -> dict[str, Any]:
    """跑一次平滑认证评测:噪声变体 → 多数投票 → CP 界 → 认证半径 → 报告。

    - 语料:``corpus_dir=None`(默认)→ 确定性合成语料(:func:
      ``make_synthetic_corpus``,``synthetic_count`` 张,文件名即金标);
      给定目录 → 复用 adversarial 的 ``load_corpus``(labels.json 标注,
      取值 ``nsfw | borderline | clean``,目录只读);
    - 逐 (σ, 图) 调 :meth:`SmoothedClassifier.classify_smoothed`(变体落
      临时目录按 σ 分子目录,运行结束自动清理);正类口径
      positive={"nsfw"}、borderline 计负类(对齐 run_benchmark 默认);
    - 每 σ 聚合:平滑准确率 / **经验认证准确率@σ**(平滑预测正确且半径
      >0 的比例;σ=0 时半径恒 0 → 认证准确率必为 0)/ ABSTAIN 计数 /
      认证但预测错误计数(诚实性指标)/ 平均认证半径(仅对半径>0 求均
      值,无证书时 None)/ 半径 min/中位/max 与逐图半径分布;
    - ``gate`` / ``update_golden``:执行半径金标门禁(:func:`_gate_section`
      挂到 ``payload["gate"]``;指纹 = 分类器+语料+σ 网格+K+α+统计版本,
      指标 = 各 σ 的 certified_acc 与 avg_radius,容差 =
      max(10%·|基线|, 0.02));``missing_baseline`` 决定指纹未命中金标时
      warn 跳过还是按违例处理;门禁段含金标路径,故逐字节确定性承诺仅
      对默认(无门禁)模式生效;
    - 写 ``out_dir/certify_report.json`` + ``certify_report.md``,返回写入
      json 的同一份 payload;**报告不含时间戳**——同种子任何次运行输出
      逐字节一致(确定性红线优先于 adversarial 的 generated_at 惯例);
    - Pillow 缺失 / 分类器不可用 / 语料问题 → :class:`CertifyError` /
      :class:`BenchmarkError`(CLI 层转退出码 1;金标文件输入错误为
      :class:`CertifyGoldenError`,同退出码 1;门禁违例由 CLI 裁决为
      退出码 2)。
    """
    sigmas = tuple(float(s) for s in sigmas)
    if not sigmas:
        raise CertifyError("σ 网格为空:至少提供一个非负 σ")
    if any(s < 0.0 for s in sigmas):
        raise CertifyError(f"σ 网格含负值:{list(sigmas)}(σ 必须非负)")
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= MAX_K:
        raise CertifyError(f"k 必须为 1~{MAX_K} 的整数,收到 {k!r}")
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not (
        0.0 < float(alpha) < 0.5
    ):
        raise CertifyError(f"alpha 必须严格落在 (0, 0.5) 开区间,收到 {alpha!r}")
    if _load_pil() is None:
        raise CertifyError(
            "未安装 Pillow,无法运行平滑认证评测(pip install Pillow 后重试;"
            "本基准以退出码 1 结束,不产出无效报告)"
        )

    cfg = Config()
    clf = _build_classifier(classifier, cfg)
    positive_desc = "/".join(sorted(POSITIVE_LABELS))

    with tempfile.TemporaryDirectory(prefix="netsentinel_certify_") as tmp:
        if corpus_dir is None:
            manifest = make_synthetic_corpus(Path(tmp) / "corpus", synthetic_count, seed)
            pairs = [
                (
                    ImageEvidence(
                        path=item["path"],
                        url=f"benchmark://certify/{item['name']}",
                        source_page="benchmark://certify",
                        sha256=_sha256_of(Path(item["path"])),
                        width=item["width"],
                        height=item["height"],
                    ),
                    item["label"],
                )
                for item in manifest
            ]
            corpus_info: dict[str, Any] = {
                "source": "synthetic",
                "generator": SYNTH_VERSION,
                "count": synthetic_count,
                "seed": seed,
            }
        else:
            pairs = load_corpus(corpus_dir)
            corpus_info = {
                "source": "labeled",
                "dir": str(Path(corpus_dir)),
                "loader": "benchmarks.run_benchmark.load_corpus (labels.json)",
            }
        if not pairs:
            raise CertifyError(
                f"语料为空:{corpus_dir}(请检查 labels.json,或调整 --count)"
            )
        telemetry.inc("certify.images", amount=len(pairs))
        telemetry.inc("certify.variants", amount=len(pairs) * len(sigmas) * k)

        with telemetry.timer("certify.run"):
            rows: list[dict[str, Any]] = []
            label_counts: dict[str, int] = {}
            for index, (evidence, label) in enumerate(pairs):
                label_counts[label] = label_counts.get(label, 0) + 1
                for sigma in sigmas:
                    smoothed = SmoothedClassifier(
                        clf, sigma=sigma, k=k, seed=seed, alpha=alpha
                    )
                    row = smoothed.classify_smoothed(
                        evidence.path,
                        Path(tmp) / f"sigma_{sigma:g}".replace(".", "_"),
                        image_key=(
                            f"{index:04d}" if corpus_dir is None else evidence.sha256[:16]
                        ),
                    )
                    correct = (row["top_class"] == "nsfw") == (label in POSITIVE_LABELS)
                    radius = row["radius"]
                    row.update(
                        {
                            "label": label,
                            "correct": correct,
                            "certified_correct": bool(correct and radius is not None and radius > 0.0),
                            "p_lower": round(row["p_lower"], 6),
                            "p_upper": round(row["p_upper"], 6),
                            "radius": None if radius is None else round(radius, 4),
                        }
                    )
                    rows.append(row)
                    if row["status"] == "abstain":
                        telemetry.inc("certify.abstain")

            total = len(pairs)
            stats: list[dict[str, Any]] = []
            for sigma in sigmas:
                sigma_rows = [row for row in rows if row["sigma"] == sigma]
                certified_rows = [
                    row for row in sigma_rows if row["radius"] is not None and row["radius"] > 0.0
                ]
                radii = sorted(float(row["radius"]) for row in certified_rows)
                abstain = sum(1 for row in sigma_rows if row["status"] == "abstain")
                certified_wrong = sum(1 for row in certified_rows if not row["correct"])
                mid = radii[len(radii) // 2] if radii else None
                stats.append(
                    {
                        "sigma": sigma,
                        "n": total,
                        "smoothed_correct": sum(1 for row in sigma_rows if row["correct"]),
                        "smoothed_acc": round(
                            sum(1 for row in sigma_rows if row["correct"]) / total, 4
                        ),
                        "certified_correct": sum(
                            1 for row in sigma_rows if row["certified_correct"]
                        ),
                        "certified_acc": round(
                            sum(1 for row in sigma_rows if row["certified_correct"]) / total,
                            4,
                        ),
                        "certified_wrong": certified_wrong,
                        "abstain": abstain,
                        "avg_radius": (
                            round(sum(radii) / len(radii), 4) if radii else None
                        ),
                        "radius_min": radii[0] if radii else None,
                        "radius_median": mid,
                        "radius_max": radii[-1] if radii else None,
                        # 逐图认证半径分布(升序;ABSTAIN 不在其中——不硬造半径)
                        "radius_distribution": [round(r, 4) for r in radii],
                    }
                )

            payload: dict[str, Any] = {
                "schema": REPORT_SCHEMA,
                "classifier": getattr(clf, "name", None) or classifier,
                "classifier_name": classifier,
                "corpus": {
                    **corpus_info,
                    "total": total,
                    "labels": dict(sorted(label_counts.items())),
                },
                "positive_class": {
                    "labels": sorted(POSITIVE_LABELS),
                    "note": f"正类 = {positive_desc};borderline 计负类(对齐 run_benchmark 默认 non-strict)",
                },
                "method": {
                    "k": k,
                    "alpha": alpha,
                    "confidence": round(1.0 - float(alpha), 4),
                    "seed": seed,
                    "threshold": DECIDE_THRESHOLD,
                    "noise": (
                        f"灰度 convert('L') 后叠加确定性 N(0, σ²) 偏移(引擎 "
                        f"{NOISE_LUT_VERSION}:σ≤{FAST_SIGMA_MAX:g} 走分位数 LUT 批量路径"
                        "(randbytes→translate→ImageChops 全 C 级,约 45×加速),"
                        "σ 超限回退逐像素 float64 参考路径;同 (seed,图,σ,j) 恒得同字节,"
                        "σ=0 变体与原图逐字节一致)"
                    ),
                    "vote": "多数投票(K 个变体分数 ≥ 0.5 记 nsfw 票;平票保守判 clean 且必 ABSTAIN)",
                    "cp": (
                        "Clopper-Pearson 精确双侧 (1−α) 界:对数空间 lgamma 二项尾"
                        "+二分求根(与 netsentinel/decision/conformal.py 的私有内核"
                        "同款算法,本地实现并注明)"
                    ),
                    "ppf": (
                        "Φ⁻¹ = Acklam 有理近似 + 一步 Halley 打磨(stdlib math.erfc);"
                        "原始 Acklam 最大误差 < 1.15e-9,打磨后实测 < 1e-12"
                    ),
                    "formula": CERTIFY_RADIUS_FORMULA,
                    "certify_rule": (
                        "certified ⟺ p_lower > p_upper(等价半径>0;二分类 CP 互补"
                        "恒等式 U(k)=1−L(n−k) 使 p_upper=1−p_lower、p_lower+p_upper≡1,"
                        "字面 '>1' 不可达,故取 Cohen 原始判据,ABSTAIN ⟺ p_lower≤p_upper)"
                    ),
                    "abstain": "无证书时 status=abstain、radius=None,只计数绝不硬造半径",
                    "certified_acc": "经验认证准确率@σ = 平滑预测正确且半径>0 的比例(σ=0 时半径恒 0 → 必为 0)",
                },
                "sigmas": list(sigmas),
                "stats": stats,
                "details": rows,
                "corpus_note": (
                    "语料为程序生成的合成图片或本地标注语料,不含真实违规内容;"
                    "指标不可外推到真实业务分布。"
                ),
                "stub_immunity_note": (
                    "stub 按文件名规则打分且变体保留原 stem → 全部变体同分、"
                    "k_top1=K,本报告用于验证“噪声 → 投票 → CP 界 → 半径 → 报告”"
                    "链路;生产模型(glm/nudenet)下票会分裂,认证率与半径会真实变化。"
                ),
                "determinism_note": (
                    "同种子任何次运行本报告逐字节一致(刻意不含时间戳);"
                    "噪声种子由 (seed, 图身份, σ, j) 哈希派生,与调用顺序无关。"
                ),
            }

            # --- 半径金标门禁(A239):重建优先于比对,结果挂 payload["gate"] ---
            if gate or update_golden:
                if corpus_dir is None:
                    # 合成语料指纹 = 生成器版本 + 张数 + 种子(corpus_info 即身份,
                    # 不含机器相关的路径字段)
                    corpus_identity = dict(corpus_info)
                else:
                    # 标注语料指纹 = 文件名:sha256 集合 + labels.json sha(adversarial 惯例)
                    corpus_identity = {
                        "source": "labeled",
                        "files": sorted(
                            f"{Path(ev.path).name}:{ev.sha256}" for ev, _ in pairs
                        ),
                        "labels_sha256": _sha256_of(
                            Path(corpus_dir) / "labels.json"
                        ),
                    }
                payload["gate"] = _gate_section(
                    payload,
                    classifier_name=classifier,
                    corpus_identity=corpus_identity,
                    sigmas=sigmas,
                    k=k,
                    alpha=alpha,
                    golden_path=golden_path,
                    update_golden=update_golden,
                    missing_baseline=missing_baseline,
                )

            out = Path(out_dir)
            out.mkdir(parents=True, exist_ok=True)
            (out / "certify_report.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (out / "certify_report.md").write_text(
                render_markdown(payload), encoding="utf-8"
            )
    logger.info(
        "平滑认证评测完成:classifier=%s total=%d sigmas=%s",
        payload["classifier"], total, list(sigmas),
    )
    return payload


def _fmt(value: Any) -> str:
    """报告用的数值格式化(None → "-",float → 4 位小数)。"""
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_markdown(payload: dict[str, Any]) -> str:
    """把评测 payload 渲染为中文 Markdown 报告(单文件,无外部依赖)。"""
    method = payload["method"]
    corpus = payload["corpus"]
    lines: list[str] = []
    lines.append("# NetSentinel 预处理随机化平滑认证评测报告(A227)")
    lines.append("")
    lines.append(f"- 分类器:`{payload['classifier']}`(平滑包装 K={method['k']},"
                 f"判决阈值 {method['threshold']})")
    if corpus["source"] == "synthetic":
        lines.append(
            f"- 语料:确定性合成图集(生成器 {corpus['generator']},"
            f"{corpus['count']} 张,seed={corpus['seed']});文件名即金标"
        )
    else:
        lines.append(f"- 语料:标注语料 `{corpus['dir']}`(labels.json,只读)")
    labels_desc = "、".join(f"{k} {v} 张" for k, v in corpus["labels"].items())
    lines.append(f"  共 {corpus['total']} 张({labels_desc})")
    lines.append(f"- σ 网格:{payload['sigmas']}(灰度级;半径量纲 = L2 像素差)")
    lines.append(
        f"- 认证口径:置信水平 {method['confidence'] * 100:g}%(α={method['alpha']});"
        f"{method['formula']};{method['ppf'].split(';')[0]}"
    )
    lines.append(f"- 判定:{method['certify_rule']}")
    lines.append(f"- ABSTAIN:{method['abstain']}")
    lines.append("")

    lines.append("## 一、认证统计总表")
    lines.append("")
    lines.append(
        "| σ | 平滑准确率 | 认证准确率@σ | 认证但预测错误 | ABSTAIN |"
        " 平均认证半径 | 半径 min/中位/max |"
    )
    lines.append("| ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in payload["stats"]:
        lines.append(
            "| {} | {:.1%} | {:.1%} | {} | {} | {} | {}/{}/{} |".format(
                row["sigma"],
                float(row["smoothed_acc"]),
                float(row["certified_acc"]),
                row["certified_wrong"],
                row["abstain"],
                _fmt(row["avg_radius"]),
                _fmt(row["radius_min"]),
                _fmt(row["radius_median"]),
                _fmt(row["radius_max"]),
            )
        )
    lines.append("")
    lines.append(
        "- 平滑准确率:多数投票预测的正确率(正类 = nsfw,borderline 计负类);"
        "认证准确率@σ:预测正确**且**认证半径 > 0 的比例——半自动举报合规"
        "可引用的“数学下界检测率证据”(置信水平见上)。"
    )
    lines.append(
        "- 平均认证半径仅对 radius>0 的图求均值;ABSTAIN 行图不硬造半径;"
        "σ=0 时半径恒 0,认证准确率必为 0(零噪声给零半径,数学诚实)。"
    )
    lines.append("")

    lines.append("## 二、逐图认证明细(按 σ 分组)")
    lines.append("")
    for row in payload["stats"]:
        sigma = row["sigma"]
        lines.append(f"### σ = {sigma}")
        lines.append("")
        lines.append(
            "| 文件 | 金标 | 平滑预测 | 正确 | k_top1 | k_top2 | p_lower |"
            " p_upper | 认证半径 | 状态 |"
        )
        lines.append("| --- | --- | --- | :-: | ---: | ---: | ---: | ---: | ---: | --- |")
        for detail in payload["details"]:
            if detail["sigma"] != sigma:
                continue
            lines.append(
                "| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                    detail["file"],
                    detail["label"],
                    detail["top_class"],
                    "是" if detail["correct"] else "否",
                    detail["k_top1"],
                    detail["k_top2"],
                    f"{detail['p_lower']:.6f}",
                    f"{detail['p_upper']:.6f}",
                    _fmt(detail["radius"]),
                    "CERTIFIED" if detail["status"] == "certified" else "ABSTAIN",
                )
            )
        dist = row["radius_distribution"]
        lines.append("")
        lines.append(
            f"- 半径分布(升序,{len(dist)} 项):{dist if dist else '(全部 ABSTAIN 或半径为 0,无分布)'}"
        )
        lines.append("")

    lines.append("## 三、结论")
    lines.append("")
    lines.append(f"- **{payload['stub_immunity_note']}**")
    best = max(payload["stats"], key=lambda row: float(row["certified_acc"]))
    lines.append(
        f"- 网格内最优认证工作点:σ={best['sigma']},认证准确率@σ="
        f"{float(best['certified_acc']):.1%},平均认证半径 {_fmt(best['avg_radius'])}"
        "(以更大 σ 换更大半径会压低平滑准确率,实际部署按合规所需下界选点)。"
    )
    lines.append(f"- {payload['corpus_note']}")
    lines.append(
        "- 按契约红线,认证半径只是“平滑预测在 L2 扰动 ≤ r 内不变”的数学下界"
        "证据:它不证明语义正确,任何模型(含 glm)的分数都只是特征,最终判定"
        "与举报须人工确认,证书不得作为自动处置依据。"
    )
    lines.append(f"- {payload['determinism_note']}")
    lines.append("")

    # --- 半径金标门禁段(仅 --gate/--update-golden 模式;A239) ---
    gate_info = payload.get("gate")
    if gate_info is not None:
        lines.append("## 四、半径金标门禁(A239)")
        lines.append("")
        fp_display = gate_info["fingerprint"][:12] + "…"
        if gate_info["mode"] == "update":
            lines.append(
                f"- 模式:**重建基线**——金标 `{gate_info['golden_path']}` 已按指纹"
                f" {fp_display} 整体重建,记录 {gate_info['metrics_recorded']} 项指标"
                f"(容差={gate_info['tolerance_rule']},无时间戳 → 重建幂等)。"
            )
            for line in gate_info["update_diff"]:
                lines.append(f"  - {line}")
        elif gate_info["status"] == "pass":
            lines.append(
                f"- 模式:比对——指纹 {fp_display} 命中,门禁**通过**:"
                f"{gate_info['metrics_total']} 项指标全部在容差内"
                f"(容差={gate_info['tolerance_rule']})。"
            )
        elif gate_info["status"] == "no_baseline":
            lines.append(
                f"- 模式:比对——指纹 {fp_display} 缺基线,按警告跳过"
                f"(missing_baseline={gate_info['missing_baseline']})。"
            )
            for line in gate_info["warnings"]:
                lines.append(f"  - {line}")
        else:  # violations(缺基线 fail / 指标违例 / 类型互变 / 指标缺基线)
            lines.append(
                f"- 模式:比对——检出 **{len(gate_info['violations'])} 项回归违例**:"
            )
            for violation in gate_info["violations"]:
                lines.append(f"  - {violation['message']}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI(退出码 0/1:0 成功;1 可预期错误——Pillow 缺失 / 分类器不可用 / 语料问题)
# ---------------------------------------------------------------------------


def _ensure_utf8_stdio() -> None:
    """Windows 控制台编码非 UTF-8 时切换标准流编码,避免中文输出报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if (
                stream is not None
                and stream.encoding
                and stream.encoding.lower() not in ("utf-8", "utf8")
            ):
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 重新配置失败不影响主流程
            pass


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    返回码(0/1/2 三态,A239 起对齐 adversarial 金标惯例):

    - 0:成功(纯报告 / 金标重建 / 门禁通过 / 缺基线 warn 跳过);
    - 1:可预期错误——Pillow 缺失 / 分类器不可用 / 语料问题 / 参数非法 /
      **金标文件输入错误**(坏 JSON / 结构非法 / 版本不兼容;保持本基准
      既有可预期错误的 0/1 语义不变,已有测试锁定);
    - 2:**金标门禁回归违例**(指标超容差 / None↔数值互变 / 缺基线
      fail / 指标缺基线)。

    (argparse 用法错误仍按标准行为 SystemExit 2。)
    """
    _ensure_utf8_stdio()
    default_out = str(Path(__file__).resolve().parent / "out")
    parser = argparse.ArgumentParser(
        prog="python benchmarks/certify_bench.py",
        description=(
            "NetSentinel 预处理随机化平滑认证评测:σ 高斯噪声 K 变体多数投票"
            "+ Clopper-Pearson 界 + Cohen 认证半径,产出经验认证准确率@σ 与"
            "半径分布(全程离线;glm 等在线分类器需显式配置)"
        ),
    )
    parser.add_argument(
        "--out", default=default_out, help=f"报告输出目录(默认 {default_out})"
    )
    parser.add_argument(
        "--corpus",
        default=None,
        help="标注语料目录(labels.json 口径,只读);缺省用确定性合成语料",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_CORPUS_COUNT,
        help=f"合成语料张数(仅 --corpus 缺省时生效,默认 {DEFAULT_CORPUS_COUNT})",
    )
    parser.add_argument(
        "--sigma",
        default=",".join(f"{s:g}" for s in DEFAULT_SIGMAS),
        help=f"σ 网格,逗号分隔的非负灰度级(默认 {DEFAULT_SIGMAS})",
    )
    parser.add_argument(
        "--k", type=int, default=DEFAULT_K, help=f"每图噪声变体数(默认 {DEFAULT_K})"
    )
    parser.add_argument(
        "--alpha",
        type=float,
        default=DEFAULT_ALPHA,
        help=f"统计误差预算 α,CP 置信水平 = 1−α(默认 {DEFAULT_ALPHA})",
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, help=f"噪声种子(默认 {DEFAULT_SEED})"
    )
    parser.add_argument(
        "--classifier",
        default="stub",
        help="分类器注册名(默认 stub,离线可用;glm 需配置 vlm_online 与密钥)",
    )
    gate_group = parser.add_argument_group("半径金标门禁(A239)")
    gate_group.add_argument(
        "--gate",
        action="store_true",
        help="开启半径金标门禁:逐 σ 比对认证准确率与平均半径基线,"
        "|当前−基线|>max(10%%·|基线|, 0.02) 或 None↔数值互变 → 违例清单 + 退出码 2",
    )
    gate_group.add_argument(
        "--update-golden",
        action="store_true",
        help="重建半径金标基线(无确认直写,打印变更 diff 摘要;优先于 --gate;"
        "金标文件无时间戳,同结果重建幂等)",
    )
    gate_group.add_argument(
        "--golden",
        default=str(DEFAULT_GOLDEN_PATH),
        help=f"半径金标文件路径(默认 {DEFAULT_GOLDEN_PATH})",
    )
    gate_group.add_argument(
        "--missing-baseline",
        choices=sorted(MISSING_BASELINE_POLICIES),
        default="warn",
        help="指纹未命中金标(分类器/语料/σ网格/K/α已变化)时:warn 跳过并提示"
        "(默认),或 fail 按回归违例处理(退出码 2)",
    )
    args = parser.parse_args(argv)

    try:
        sigmas = tuple(float(item) for item in str(args.sigma).split(",") if item.strip())
        payload = run(
            args.out,
            corpus_dir=args.corpus,
            classifier=args.classifier,
            sigmas=sigmas,
            k=args.k,
            seed=args.seed,
            alpha=args.alpha,
            synthetic_count=args.count,
            gate=args.gate,
            golden_path=args.golden,
            update_golden=args.update_golden,
            missing_baseline=args.missing_baseline,
        )
    except (CertifyError, BenchmarkError) as exc:
        # 金标文件输入错误(子类 exit_code=1)与既有可预期错误(1)同码,
        # 读取属性以保持与 adversarial 的分流惯例一致、便于后续扩展。
        telemetry.inc("certify.errors")
        print(f"错误:{exc}", file=sys.stderr)
        return int(getattr(exc, "exit_code", 1))

    print(
        f"平滑认证评测完成:分类器={payload['classifier']} "
        f"语料={payload['corpus']['total']}张 K={payload['method']['k']} "
        f"α={payload['method']['alpha']}"
    )
    for row in payload["stats"]:
        print(
            "  σ={:<6g} 平滑准确率 {:.1%} 认证准确率@σ {:.1%} 平均认证半径 {}"
            " ABSTAIN {}".format(
                row["sigma"],
                float(row["smoothed_acc"]),
                float(row["certified_acc"]),
                _fmt(row["avg_radius"]),
                row["abstain"],
            )
        )
    out_dir = Path(args.out)
    print(f"报告已写出:{out_dir / 'certify_report.md'} 与 {out_dir / 'certify_report.json'}")

    # --- A239 半径金标门禁结果输出与退出码裁决(对齐 adversarial 惯例) ---
    exit_code = 0
    gate_info = payload.get("gate")
    if gate_info is not None:
        fp_display = gate_info["fingerprint"][:12] + "…"
        if gate_info["mode"] == "update":
            print(
                f"半径金标已重建:{gate_info['golden_path']}(指纹 {fp_display},"
                f"记录 {gate_info['metrics_recorded']} 项指标基线,"
                f"容差={gate_info['tolerance_rule']};无时间戳,重建幂等)"
            )
            for line in gate_info["update_diff"]:
                print(f"  {line}")
        elif gate_info["status"] == "pass":
            print(
                f"半径金标门禁通过:指纹 {fp_display},"
                f"{gate_info['metrics_total']} 项指标全部在容差内"
                f"(容差={gate_info['tolerance_rule']})—— 退出码 0"
            )
        elif gate_info["status"] == "no_baseline":
            for line in gate_info["warnings"]:
                print(f"警告:{line}", file=sys.stderr)
            print("半径金标门禁:缺基线,已按警告跳过 —— 退出码 0")
        else:  # violations(缺基线 fail / 指标违例 / 类型互变 / 指标缺基线)
            for violation in gate_info["violations"]:
                print(f"金标违例:{violation['message']}", file=sys.stderr)
            print(
                f"半径金标门禁:检出 {len(gate_info['violations'])} 项回归违例"
                " —— 退出码 2",
                file=sys.stderr,
            )
            exit_code = 2
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
