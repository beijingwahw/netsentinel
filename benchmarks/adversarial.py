# -*- coding: utf-8 -*-
"""NetSentinel 对抗鲁棒性基准(A55,V3)。

量化"违规站常用的视觉规避手段"对识别分数的削弱程度:高斯模糊、8x8 马赛克、
中央 25% 面积遮挡、JPEG quality=30 重压缩。对标注语料(benchmarks/corpus,
复用 A37 的 labels.json)逐图生成四类扰动变体,比较"原始分 vs 扰动分",
统计每类扰动的**平均绝对降幅 / 最大降幅 / 降幅>0.2 的图片占比**,产出中文
报告 ``adversarial_report.md`` + ``adversarial_report.json``。**全程离线**:

- ``perturb(src_path, out_dir)``:PIL 惰性生成四类变体,文件名保留原 stem
  (如 ``nsfw_hi_001_blur.png``),stub 桩规则因此对变体打出与原图相同的分;
- ``run(corpus_dir, out_dir, classifier="stub")``:载入 labels.json(A37 的
  ``load_corpus``)→ 原始分 + 四变体分 → 降幅统计 → 写报告;扰动变体落在
  临时目录(运行结束自动清理,绝不写进语料目录);
- 分类器经 ``classifier_base.get_classifier`` 惰性获取,失败给中文指引;
  glm 等在线分类器需显式配置(vlm_online + 密钥)才允许评测,未配置时
  以退出码 2 结束,绝不静默产出无效报告(与 A37 口径一致);
- 未安装 Pillow 时给出中文提示并以退出码 2 结束。

命令行::

    python benchmarks/adversarial.py                          # 默认 stub + 自带语料
    python benchmarks/adversarial.py --corpus benchmarks/corpus --out benchmarks/out
    python benchmarks/adversarial.py --classifier glm         # 需显式配置,离线默认拒绝
    python benchmarks/adversarial.py --grid                   # 感知扰动预算曲线(ε 网格模式)
    python benchmarks/adversarial.py --update-golden          # 重建金标基线(打印 diff)
    python benchmarks/adversarial.py --gate                   # 金标门禁:回归 → 退出码 2
    python benchmarks/adversarial.py --update-curve-golden    # 重建感知曲线独立金标
    python benchmarks/adversarial.py --curve-gate             # 曲线金标门禁:回归 → 退出码 2

口径说明:降幅 = 原始 nsfw_prob − 扰动 nsfw_prob(带符号,扰动后分数更高记为
负);"平均绝对降幅"取 |降幅| 的均值(衡量总体漂移),"最大降幅"取带符号最大值
(衡量最坏掉分),"占比"按 降幅 > 0.2(显著掉分线)统计。

**stub 天然免疫声明**:stub 按文件名关键词打分,而扰动变体保留原 stem,因此
stub 场景下降幅恒为 0——这验证的是"扰动生成 → 评分 → 统计 → 报告"框架本身;
生产模型(glm/nudenet)会体现真实退化,建议接入后用同一命令重跑对比。

V5 升级(兼容性零破坏,降幅口径与报告数值不变):

- 性能:``perturb`` 源图只打开并解码一次,四变体复用同一内存图像;JPEG
  重压缩先于中央遮挡执行,遮挡原地进行,省一次整图拷贝;``run`` 的变体
  生成与评分两趟循环合并——整轮一次 ``classify_batch`` 批量评分
  (N 张语料的分类器调用从 5N 次降为 1 次,协议不支持时回退逐张);
- 可观测:``telemetry.timer("adversarial.run")`` 全程计时,图片数 / 变体数
  / 可预期失败分别计入 ``adversarial.images`` / ``adversarial.variants`` /
  ``adversarial.errors``。

V9 统计推断层(对标 RobustBench / HELM 的 CI 报告规范;兼容性零破坏,
既有指标字段与数值不变,只新增):

- 每类扰动的带符号平均降幅(``avg_drop``)附**配对 percentile bootstrap
  95% CI**(B=10000;逐图降幅即配对差,对其有放回重采样取均值,再取
  2.5%/97.5% 次序统计量;``random.Random(种子)`` 固定种子派生 → 同一
  数据任何次运行输出**逐位一致**,纯 stdlib);
- **符号翻转配对置换检验** p 值(双侧,原假设:平均降幅 = 0):n ≤ 12 时
  精确枚举全部 2^n 次符号翻转,n 更大时 9999 次蒙特卡洛(含 +1 校正,
  p 永不为 0);
- 降幅>0.2 占比附 **Wilson score 95% CI**(闭式公式,仅 ``math``;相对
  正态近似在 0/N 与 N/N 边界不塌缩为无信息区间);
- 报告新增 ``ci95`` / ``p_value`` / ``wilson_ci``(JSON 字段 + markdown
  新列):单点估计之外同时给出不确定度与显著性,避免小语料下过度解读。

V10.4 对抗基准金标回归门禁(对标 regression-based benchmark gates:模型 /
prompt / 预处理任一变更自动拦截鲁棒性回归;兼容性零破坏,``gate=False``
默认即现状纯报告,既有指标字段与语义不变,门禁为纯新增层):

- **指纹键** = ``fingerprint(classifier 注册名 + 语料文件名:sha256 集合 +
  labels.json sha256 + 扰动参数 + 统计版本号)``:语料 / 参数 / 分类器任一
  变化即新键 → 旧金标自然"缺基线",绝不用错误基线比对(避免误报);
- **金标文件**(默认 ``benchmarks/out/adversarial_golden.json``,``--golden``
  可覆盖):``{fingerprint: {"<扰动>/<指标>": {value, tol}}}`` 记录
  avg_drop / avg_abs_drop / drop_gt_0.2_ratio 每类扰动的基线值与容差;
- **容差** = ``max(|基线值| × 10%, 对应 CI 半宽)``——直接复用 V9 统计推断层
  的 CI 作为容差来源(avg_drop / avg_abs_drop 用 bootstrap ci95 半宽,
  drop_gt_0.2_ratio 用 Wilson 95% CI 半宽),统计上自洽:采样噪声天然落在
  CI 内,超出即真回归;
- ``--update-golden`` 显式重建(无确认直写,CLI 打印变更 diff 摘要);
  ``--gate`` 逐指标比对 ``|当前 − 基线| > 容差`` → 违例清单(中文,含指标名 /
  基线 / 当前 / 容差)→ **退出码 2**;指纹未命中金标 = "缺基线",行为可配
  (``--missing-baseline warn|fail``,默认 warn + 提示 update;fail 按违例处理);
- **退出码三态**(对齐 ops/audit_verify 惯例):0 正常(含 warn 跳过)/
  1 金标文件输入错误(损坏 / 坏 JSON / 结构非法)/ 2 回归违例(或既有
  Pillow 缺失、分类器不可用等可预期错误,语义不变)。

感知感知(perceptual-aware)扰动预算曲线(对标 2025-26 攻防报告从固定
L_p 参数转向 SSIM/感知度量约束的 ε 网格 + 鲁棒性曲线的范式;兼容性零破坏,
``grid=False`` 默认即现状单点报告,payload 不新增任何键):

- **扰动参数网格**:四类扰动各定义强度递增的 ε 网格——blur radius
  ``{1,2,3,5,8}``、mosaic 块 ``{4,8,16,24}`` px、occlude 面积
  ``{10,25,40,60}``%、jpeg quality ``{60,40,30,20,10}``(jpeg 的强度
  ε = 1 − q/100,使四类扰动的 ε 同向递增;单点默认参数均在网格内);
- **感知强度度量**:纯 stdlib SSIM(8x8 非重叠窗口、PIL ``convert("L")``
  灰度化、全程 float 数学、零 numpy;Pillow 缺失时中文报错)——每个变体附
  ``(perturb_kind, param, ssim)`` 三元组,横轴从"固定 L_p 参数"升级为
  "感知等强度";
- **曲线模式** ``run(grid=True)``:每类扰动输出 score-vs-ε 点列、
  score-vs-SSIM 曲线、鲁棒 AUC(平均降幅对 ε 的归一化梯形面积)与
  ``min_effective_attack``(首个 |平均降幅|>0.2 的最小 ε 及其 SSIM——
  直接指导 preprocess 反混淆参数下限);payload 新增 ``curves`` 段,
  markdown 报告新增曲线表;
- **与金标门禁正交**:GATE_METRICS / 指纹 / 容差只覆盖单点默认参数,
  ``curves`` 不进 stats、不进金标;``grid=True`` 时门禁行为与 ``grid=False``
  完全一致(见 :func:`run` docstring 与报告曲线段说明)。

V13 评测增强批:感知曲线**独立**金标门禁(单点金标零变动,兼容性零
破坏;对标 regression-based benchmark gates 对曲线指标的覆盖):

- **独立指纹键** = 单点指纹(:func:`_fingerprint_digest` 的 sha256)+
  ε 网格参数(:data:`PERTURB_GRIDS`)+ 曲线指标版本
  (:data:`CURVE_GOLDEN_STATS_VERSION`)——分类器 / 语料 / 单点扰动
  参数任一变化(单点指纹换键)、网格加密或曲线口径变化,曲线键即换,
  绝不用错误基线比对;
- **曲线金标文件**(默认 ``benchmarks/out/adversarial_curve_golden.json``,
  ``--curve-golden`` 可覆盖,独立于单点金标文件):每类扰动记
  ``<kind>/auc``(鲁棒 AUC)与 ``<kind>/min_effective_eps``(首个显著
  掉分的 ε;``value: null`` 表示网格内未跌破);
- **容差与 None 语义**:数值指标容差 = ``max(10%·|基线|, 0.05)`` 固定
  小容差(曲线指标无逐样本 CI 可依托,取固定小容差防过度敏感);
  ``min_effective_eps`` 的 None↔数值互变**即违例**——"未跌破 ↔ 已跌破"
  是曲线形态的质变,任何方向都须人工复核;
- ``--curve-gate`` / ``--update-curve-golden`` 与既有 ``--gate`` /
  ``--update-golden`` 并存互不干扰(两文件、两指纹、两段输出,缺基线
  处置共用 ``--missing-baseline``);违例 → 退出码 2,曲线金标文件
  损坏 → 退出码 1(对齐单点金标三态惯例);``run(grid=True,
  curve_gate=True)`` 返回与 :func:`evaluate_gate` 同形态的违例清单;
  开启曲线门禁而未开 ``--grid`` 时自动附带 ε 网格评测(曲线门禁的
  比对对象就是曲线,单点门禁行为仍不受影响)。

V13-2 评测增强批(曲线金标三项扩展;单点金标零变动,兼容性零破坏):

- **曲线形状指纹** ``shape_digest``:curves 段每类扰动新增 score-vs-ε 点列的
  规范化形状签名——逐 ε 段斜率符号序列量化为 hex("0"平坦 / "1"上升 /
  "2"下降,每段一位,长度 = 网格点数 − 1;见 :func:`curve_shape_digest`)。
  动机:鲁棒 AUC 把整条曲线坍缩成单个面积标量,``min_effective_eps`` 只看
  首个越阈点——"掉分质量在段间重新分配 / 局部峰平移"可以同时保持 AUC 与
  端点不变而完全逃过既有指标;逐段斜率符号序列对这类形状重排敏感。曲线
  门禁指标扩为 ``(auc, min_effective_eps, shape_digest)``(4 类 × 3 = 12 项),
  :data:`CURVE_GOLDEN_STATS_VERSION` 递增至 v13-2——旧曲线金标指纹全部
  失效,按既有"缺基线"机制处置;digest 不匹配 = 新违例
  ``kind="shape_changed"``,容差为离散的"允许斜率符号翻转段数"
  (:data:`CURVE_SHAPE_FLIP_TOL`,默认 0 严格);
- **曲线缺基线策略独立** ``--curve-missing-baseline``:缺省继承
  ``--missing-baseline`` 的值(既有行为),显式设置则曲线金标独立处置
  (单点与曲线可分别 warn / fail,见 :func:`run` 的 ``curve_missing_baseline``)。

安全红线:本模块只读本地语料,变体只写临时目录;任何模型(含 glm)的分数都
只是特征,最终判定与举报须人工确认。金标门禁只拦截"指标显著变差",不构成
自动处置依据。
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
# 直接以脚本运行(python benchmarks/adversarial.py)时,保证项目根在
# sys.path 上,使 netsentinel / benchmarks 包可导入;经包导入(tests)时为空操作。
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from netsentinel import telemetry  # noqa: E402
from netsentinel.contracts import Config, ImageEvidence, now_iso  # noqa: E402
from netsentinel.vision.classifier_base import get_classifier  # noqa: E402
from benchmarks.run_benchmark import BenchmarkError, load_corpus  # noqa: E402

__all__ = [
    "AdversarialError",
    "AdversarialGoldenError",
    "DROP_ALERT_LINE",
    "PERTURB_TYPES",
    "PERTURB_META",
    "PERTURB_GRIDS",
    "GRID_SSIM_WIN",
    "STATS_SEED",
    "BOOTSTRAP_B",
    "PERMUTATION_MC_N",
    "PERMUTATION_EXACT_MAX_N",
    "GATE_METRICS",
    "GOLDEN_SCHEMA",
    "GOLDEN_STATS_VERSION",
    "TOL_VALUE_RATIO",
    "DEFAULT_GOLDEN_PATH",
    "MISSING_BASELINE_POLICIES",
    "CURVE_GATE_METRICS",
    "CURVE_GOLDEN_SCHEMA",
    "CURVE_GOLDEN_STATS_VERSION",
    "CURVE_TOL_VALUE_RATIO",
    "CURVE_TOL_MIN",
    "CURVE_SHAPE_FLIP_TOL",
    "SHAPE_FLAT_TOL",
    "DEFAULT_CURVE_GOLDEN_PATH",
    "bootstrap_mean_ci95",
    "signflip_permutation_pvalue",
    "wilson_ci95",
    "compute_ssim",
    "robustness_auc",
    "min_effective_attack",
    "curve_shape_digest",
    "perturb",
    "run",
    "compute_fingerprint",
    "compute_curve_fingerprint",
    "metric_tolerance",
    "curve_metric_tolerance",
    "build_golden_entry",
    "build_curve_golden_entry",
    "load_golden",
    "load_curve_golden",
    "save_golden",
    "save_curve_golden",
    "evaluate_gate",
    "evaluate_curve_gate",
    "render_markdown",
    "main",
]

logger = logging.getLogger(__name__)

#: GLM 等在线分类器未配置时的环境变量提示名(与 A37 一致)。
ENV_GLM_API_KEY = "NETSENTINEL_GLM_API_KEY"

#: 四类扰动(顺序即变体生成顺序与报告列顺序)。
PERTURB_TYPES: tuple[str, ...] = ("blur", "mosaic", "occlude", "jpeg")

#: 感知扰动预算曲线:每类扰动的 ε 网格(**强度递增**排列;jpeg 以 quality
#: 表达,强度 ε = 1 − q/100,故网格按 quality 递减 = 强度递增)。单点默认
#: 参数(blur 3 / mosaic 8 / occlude 0.25 / jpeg 30)均包含在网格内,便于
#: 单点结果与曲线互相锚定。仅 grid=True 时使用,单点路径不读此表。
PERTURB_GRIDS: dict[str, tuple[float, ...]] = {
    "blur": (1, 2, 3, 5, 8),           # GaussianBlur radius(px)
    "mosaic": (4, 8, 16, 24),          # 马赛克块边长(px)
    "occlude": (0.10, 0.25, 0.40, 0.60),  # 中央遮挡面积占比
    "jpeg": (60, 40, 30, 20, 10),      # quality(强度 = 1 − q/100 递增)
}

#: ε(攻击强度)语义:曲线横轴与 AUC 归一化均用该强度,而非原始参数值。
_GRID_EPS_SEMANTICS: dict[str, str] = {
    "blur": "GaussianBlur radius(px)",
    "mosaic": "马赛克块边长(px)",
    "occlude": "中央遮挡面积占比",
    "jpeg": "1 − quality/100(重压缩强度)",
}

#: SSIM 感知相似度的窗口边长(px;非重叠窗口,窗内均值再全图平均)。
GRID_SSIM_WIN = 8

#: 扰动参数与中文名(报告表格与结论用);"grid" 为该类扰动的 ε 网格
#: (= :data:`PERTURB_GRIDS` 对应元组),"param_name" 为参数的机器可读名。
PERTURB_META: dict[str, dict[str, Any]] = {
    "blur": {
        "label": "高斯模糊",
        "suffix": "_blur.png",
        "param": "GaussianBlur radius=3",
        "param_name": "radius",
        "grid": PERTURB_GRIDS["blur"],
    },
    "mosaic": {
        "label": "马赛克",
        "suffix": "_mosaic.png",
        "param": "8x8 块(1/8 降采样 NEAREST 再放大回)",
        "param_name": "block_px",
        "grid": PERTURB_GRIDS["mosaic"],
    },
    "occlude": {
        "label": "中央遮挡",
        "suffix": "_occlude.png",
        "param": "中央 25% 面积黑块",
        "param_name": "area_ratio",
        "grid": PERTURB_GRIDS["occlude"],
    },
    "jpeg": {
        "label": "JPEG 重压缩",
        "suffix": "_jpeg.jpg",
        "param": "quality=30",
        "param_name": "quality",
        "grid": PERTURB_GRIDS["jpeg"],
    },
}

#: 显著掉分线:降幅超过该值计入"占比"统计。
DROP_ALERT_LINE = 0.2

_BLUR_RADIUS = 3            # 高斯模糊半径
_MOSAIC_BLOCK = 8           # 马赛克块尺寸(像素)
_OCCLUDE_AREA_RATIO = 0.25  # 遮挡面积占比(中央)
_JPEG_QUALITY = 30          # 重压缩质量

# --- V9 统计推断层参数(全部确定性:固定种子 → 报告逐位可复现) -------------

#: 统计推断全局种子(2026 · A55;按扰动类型 + 用途派生子种子,见
#: :func:`_kind_stats_seed`)。固定种子的意义:任何机器、任何次运行,
#: 同一份降幅数据得到同一份 CI / p 值——评测报告可复现是对标 RobustBench /
#: HELM 规范的硬前提。
STATS_SEED = 20260155

#: 配对 percentile bootstrap 重采样次数。
BOOTSTRAP_B = 10_000

#: 置换检验蒙特卡洛抽样次数(精确枚举不可行时)。
PERMUTATION_MC_N = 9_999

#: n 不超过该值时精确枚举全部 2^n 次符号翻转(n=12 → 4096 次,毫秒级)。
PERMUTATION_EXACT_MAX_N = 12

#: 标准正态 97.5% 分位数(Wilson 95% CI 用;常量内联,避免依赖 scipy)。
_Z95 = 1.959963984540054

#: 浮点比较容差(置换统计量 "≥" 判定防浮点噪声)。
_STAT_EPS = 1e-12

# --- V10.4 金标回归门禁参数(对标 regression-based benchmark gates) ------

#: 金标统计口径版本号:指标定义 / 容差规则 / 扰动参数任一语义变化时递增,
#: 使全部旧指纹失效(缺基线 → 提示重建),绝不用错误口径比对旧基线。
GOLDEN_STATS_VERSION = "v10.4-1"

#: 金标文件结构版本(不兼容演进时递增;读取端见 ``load_golden`` 预检)。
GOLDEN_SCHEMA = "netsentinel-adversarial-golden/1"

#: 纳入门禁的指标(每类扰动逐一比对):带符号平均降幅 / 平均绝对降幅 /
#: 降幅>0.2 占比。max_drop 依赖单图极端值,不作为门禁口径(报告仍保留)。
GATE_METRICS: tuple[str, ...] = ("avg_drop", "avg_abs_drop", "drop_gt_0.2_ratio")

#: 容差中"基线相对比例"分支:tol = max(TOL_VALUE_RATIO·|基线值|, CI 半宽)。
TOL_VALUE_RATIO = 0.10

#: 指纹未命中金标(分类器/语料/参数已变化)时的处置策略。
MISSING_BASELINE_POLICIES: frozenset[str] = frozenset({"warn", "fail"})

#: 金标文件默认路径(benchmarks/out/adversarial_golden.json;--golden 可覆盖)。
DEFAULT_GOLDEN_PATH = Path(__file__).resolve().parent / "out" / "adversarial_golden.json"

#: 容差规则的中文口径说明(写入金标 _tolerance_rule 与报告 payload)。
_TOL_RULE_TEXT = (
    "tol = max(10%·|基线值|, 对应 CI 半宽):avg_drop/avg_abs_drop 取配对 "
    f"bootstrap ci95 半宽,drop_gt_0.2_ratio 取 Wilson 95% CI 半宽(V9 统计"
    "推断层产出,固定种子确定性)"
)

# --- V13 感知曲线独立金标门禁参数(与单点金标完全独立的两套键/文件) -----

#: 曲线金标统计口径版本号:曲线指标定义(AUC 归一口径 / min_effective ε
#: 语义 / shape_digest 形状指纹口径)/ 曲线容差规则 / ε 网格参数任一变化时
#: 递增,使全部旧曲线指纹失效(缺基线 → 提示重建),绝不用错误口径比对旧
#: 基线。**独立于** :data:`GOLDEN_STATS_VERSION`:单点口径演进不牵连曲线
#: 基线,反之亦然。v13-2:曲线门禁指标扩入 shape_digest(旧曲线金标按缺
#: 基线处理)。
CURVE_GOLDEN_STATS_VERSION = "v13-2"

#: 曲线金标文件结构版本(不兼容演进时递增;读取端见 ``load_curve_golden``)。
CURVE_GOLDEN_SCHEMA = "netsentinel-adversarial-curve-golden/1"

#: 纳入曲线门禁的指标(每类扰动逐一比对):鲁棒 AUC、首个显著掉分强度
#: (min_effective_attack 的 ε;None = 网格内未跌破,与数值互变即违例)与
#: 曲线形状指纹(shape_digest;离散 hex,不匹配 → kind="shape_changed")。
CURVE_GATE_METRICS: tuple[str, ...] = ("auc", "min_effective_eps", "shape_digest")

#: 曲线容差中"基线相对比例"分支:tol = max(CURVE_TOL_VALUE_RATIO·|基线|, CURVE_TOL_MIN)。
CURVE_TOL_VALUE_RATIO = 0.10

#: 曲线容差固定小容差下限:曲线指标是 ε 网格上的聚合量,无逐样本 CI 可
#: 依托(V9 推断层只覆盖单点降幅),取固定小容差防过度敏感。
CURVE_TOL_MIN = 0.05

#: shape_digest 的离散容差:**允许斜率符号翻转的段数**(逐位比较)。digest
#: 是离散值,无数值容差可言——0(默认)= 严格精确匹配,任何一段斜率符号
#: 变化即 kind="shape_changed";1 = 允许单段翻转(局部一段的升/降翻转不
#: 拦截,适合对采样抖动更宽容的门禁)。写入金标的 ``tol`` 字段即此值,
#: 重建金标时生效(改常量 → --update-curve-golden 重跑)。
CURVE_SHAPE_FLIP_TOL = 0

#: shape_digest 判"平坦段"的斜率阈值(|slope| ≤ 该值记 "0")。曲线点列的
#: 降幅在 payload 中统一 round 4 位小数,真平坦段差值恰为 0;阈值取 1e-9
#: 仅为吸收浮点求和的次生噪声,不影响 1e-4 量级以上的真实升降判定。
SHAPE_FLAT_TOL = 1e-9

#: 曲线金标文件默认路径(独立于单点金标;--curve-golden 可覆盖)。
DEFAULT_CURVE_GOLDEN_PATH = Path(__file__).resolve().parent / "out" / "adversarial_curve_golden.json"

#: 曲线容差规则的中文口径说明(写入曲线金标 _tolerance_rule 与 payload)。
_CURVE_TOL_RULE_TEXT = (
    "曲线容差 = max(10%·|基线|, 0.05) 固定小容差;min_effective_eps 的 "
    "None↔数值互变即违例(未跌破↔已跌破为曲线形态质变,任何方向都拦截);"
    f"shape_digest 为离散形状指纹,容差 = 允许斜率符号翻转段数(默认 "
    f"{CURVE_SHAPE_FLIP_TOL} 严格),不匹配即 kind=shape_changed"
)


class AdversarialError(RuntimeError):
    """对抗基准流程中可预期的错误(中文消息;CLI 捕获后以退出码 2 结束)。"""


class AdversarialGoldenError(AdversarialError):
    """金标文件输入错误(损坏 / 坏 JSON / 结构非法 / 版本不兼容)。

    对应 CLI 退出码 1(对齐 ops/audit_verify 的"输入错误"惯例);作为
    :class:`AdversarialError` 子类,既有 ``except AdversarialError`` 兜底
    仍可捕获。
    """

    exit_code = 1


# ---------------------------------------------------------------------------
# Pillow 惰性加载(与 vision/preprocess.py、intel/phash.py 同款策略)
# ---------------------------------------------------------------------------


def _load_pil():
    """惰性导入 ``PIL.Image`` / ``PIL.ImageFilter``;缺失 / 半初始化返回 None。

    先导入父包 ``PIL`` 再导子模块:测试以 ``sys.modules["PIL"] = None`` 屏蔽
    Pillow 时,若只导子模块会命中缓存而漏检,先导父包才能可靠感知"不可用"。
    """
    try:
        importlib.import_module("PIL")
        image = importlib.import_module("PIL.Image")
        image_filter = importlib.import_module("PIL.ImageFilter")
    except Exception:  # None 注入 / 未安装 / 损坏的 PIL 一律按"未安装"处理
        return None
    return types.SimpleNamespace(Image=image, ImageFilter=image_filter)


def _sha256_of(path: Path) -> str:
    """流式计算文件 sha256(变体证据哈希,便于对账)。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 扰动变体生成
# ---------------------------------------------------------------------------


def _write_perturb_variant(
    pil: Any,
    im: Any,
    out_path: Path,
    kind: str,
    *,
    blur_radius: int = _BLUR_RADIUS,
    mosaic_block: int = _MOSAIC_BLOCK,
    occlude_ratio: float = _OCCLUDE_AREA_RATIO,
    jpeg_quality: int = _JPEG_QUALITY,
) -> Path:
    """生成**单类**扰动变体并落盘(参数化内核;单点与网格模式共用)。

    不传额外参数即模块默认常量——单点模式(:func:`perturb`)的输出与历史
    版本逐字节一致(occlude 在副本上粘贴,不再原地污染共享源图,像素结果
    相同)。参数越界 → ValueError(中文);写出失败 → OSError/ValueError
    由调用方包装为 :class:`AdversarialError`。
    """
    if kind == "blur":
        if blur_radius < 0:
            raise ValueError(f"blur_radius 必须非负,收到 {blur_radius}")
        im.filter(pil.ImageFilter.GaussianBlur(radius=blur_radius)).save(
            out_path, format="PNG"
        )
    elif kind == "mosaic":
        if mosaic_block < 1:
            raise ValueError(f"mosaic_block 必须为正整数,收到 {mosaic_block}")
        width, height = im.size
        small_w = max(1, width // mosaic_block)
        small_h = max(1, height // mosaic_block)
        mosaic = im.resize((small_w, small_h), pil.Image.NEAREST)
        mosaic = mosaic.resize((width, height), pil.Image.NEAREST)
        mosaic.save(out_path, format="PNG")
    elif kind == "occlude":
        if not 0.0 < occlude_ratio <= 1.0:
            raise ValueError(f"occlude_ratio 必须在 (0, 1] 内,收到 {occlude_ratio}")
        width, height = im.size
        canvas = im.copy()  # 副本上粘贴:源图保持纯净,供其余变体/网格复用
        side = int(round((width * height * occlude_ratio) ** 0.5))
        side = max(1, min(side, width, height))
        x0 = (width - side) // 2
        y0 = (height - side) // 2
        canvas.paste((0, 0, 0), (x0, y0, x0 + side, y0 + side))
        canvas.save(out_path, format="PNG")
    elif kind == "jpeg":
        if not 1 <= jpeg_quality <= 95:
            raise ValueError(f"jpeg_quality 必须在 [1, 95] 内,收到 {jpeg_quality}")
        im.save(out_path, format="JPEG", quality=jpeg_quality)
    else:
        raise ValueError(f"未知扰动类型 {kind!r}(允许:{list(PERTURB_TYPES)})")
    return out_path


def perturb(src_path: str | os.PathLike[str], out_dir: str | os.PathLike[str]) -> list[str]:
    """对单张图片生成四类视觉规避变体,返回新文件路径列表(顺序同 PERTURB_TYPES)。

    - ``<stem>_blur.png``:GaussianBlur(radius=3);
    - ``<stem>_mosaic.png``:8x8 块马赛克——缩到 ``(w//8, h//8)``(NEAREST)
      再放大回原尺寸(NEAREST),得到硬边块状图;
    - ``<stem>_occlude.png``:中央 25% **面积**黑块(边长 = √(0.25·w·h),
      居中粘贴纯黑矩形);
    - ``<stem>_jpeg.jpg``:以 quality=30 重压缩。

    统一转 RGB 后处理(保证滤镜 / JPEG 编码对任意源模式可用);所有变体保持
    原始宽高。文件名保留原 stem,故 stub 桩规则对变体打出与原图相同的分。
    V5 性能:源图只**打开并解码一次**,四个变体全部复用同一内存图像(经
    :func:`_write_perturb_variant` 参数化内核,默认参数 = 模块常量,输出与
    历史版本逐字节一致)。
    未安装 Pillow / 解码失败 → :class:`AdversarialError`(中文)。
    """
    pil = _load_pil()
    if pil is None:
        raise AdversarialError(
            "未安装 Pillow,无法生成对抗扰动变体(pip install Pillow 后重试;"
            "本工具以退出码 2 结束,不产出无效报告)"
        )

    src = Path(src_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    try:
        with pil.Image.open(src) as opened:
            opened.load()
            im = opened.convert("RGB")
    except (OSError, ValueError) as exc:
        raise AdversarialError(f"无法解码图片,扰动跳过:{src}({exc})") from exc

    stem = src.stem
    try:
        paths = [
            _write_perturb_variant(
                pil, im, out / f"{stem}{PERTURB_META[kind]['suffix']}", kind
            )
            for kind in PERTURB_TYPES
        ]
    except (OSError, ValueError) as exc:
        raise AdversarialError(f"扰动变体写出失败:{src}({exc})") from exc

    logger.debug("扰动变体生成完毕:%s → %d 个变体", src.name, len(paths))
    return [str(path) for path in paths]


# ---------------------------------------------------------------------------
# 网格扰动变体(感知扰动预算曲线):单类 + 单参数,文件名保留原 stem
# ---------------------------------------------------------------------------


def _grid_param_tag(kind: str, param: float) -> str:
    """网格参数 → 文件名标签(确定性;occlude 用百分数整数,避免小数点)。"""
    if kind == "occlude":
        return str(int(round(float(param) * 100)))
    return str(int(param))


def _grid_strength(kind: str, param: float) -> float:
    """网格参数 → 攻击强度 ε(jpeg 取 1 − q/100,使四类扰动的 ε 同向递增)。"""
    if kind == "jpeg":
        return 1.0 - float(param) / 100.0
    return float(param)


def _perturb_grid_variant(
    src_path: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    kind: str,
    param: float,
) -> Path:
    """生成"单类扰动 + 单个网格参数"的变体并落盘(grid 模式专用)。

    - 文件名 ``<stem>_<kind>_<参数标签>.png/.jpg``:保留原 stem(stub 关键词
      不断链),参数标签确定性可重建;
    - 参数按扰动类型映射到 :func:`_write_perturb_variant` 的对应形参,
      越界 / 未知类型 → ValueError(中文);解码 / 写出失败 →
      :class:`AdversarialError`(中文);
    - 返回变体路径(父目录自动创建)。
    """
    if kind not in PERTURB_TYPES:
        raise ValueError(f"未知扰动类型 {kind!r}(允许:{list(PERTURB_TYPES)})")
    kwargs: dict[str, Any] = {}
    if kind == "blur":
        kwargs["blur_radius"] = int(param)
    elif kind == "mosaic":
        kwargs["mosaic_block"] = int(param)
    elif kind == "occlude":
        kwargs["occlude_ratio"] = float(param)
    else:  # jpeg(上面已排除未知类型)
        kwargs["jpeg_quality"] = int(param)

    pil = _load_pil()
    if pil is None:
        raise AdversarialError(
            "未安装 Pillow,无法生成网格扰动变体(pip install Pillow 后重试;"
            "本工具以退出码 2 结束,不产出无效报告)"
        )
    src = Path(src_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    try:
        with pil.Image.open(src) as opened:
            opened.load()
            im = opened.convert("RGB")
    except (OSError, ValueError) as exc:
        raise AdversarialError(f"无法解码图片,扰动跳过:{src}({exc})") from exc

    extension = ".jpg" if kind == "jpeg" else ".png"
    target = out / f"{src.stem}_{kind}_{_grid_param_tag(kind, param)}{extension}"
    try:
        _write_perturb_variant(pil, im, target, kind, **kwargs)
    except (OSError, ValueError) as exc:
        raise AdversarialError(f"网格扰动变体写出失败:{src}({exc})") from exc
    return target


# ---------------------------------------------------------------------------
# 分类器解析(惰性 + 中文指引 + glm 配置预检,口径对齐 A37)
# ---------------------------------------------------------------------------


def _preflight_online_classifier(classifier_name: str, cfg: Config) -> None:
    """在线分类器(glm)评测前配置预检:未配置直接拒绝,避免静默产出无效报告。"""
    if classifier_name != "glm":
        return
    reasons: list[str] = []
    if not cfg.vlm_online:
        reasons.append("cfg.vlm_online=False(默认安全态:图像数据不出本机)")
    api_key = cfg.glm_api_key or os.environ.get(ENV_GLM_API_KEY, "")
    if not api_key:
        reasons.append(f"未配置 glm_api_key,环境变量 {ENV_GLM_API_KEY} 也为空")
    if reasons:
        raise AdversarialError(
            "GLM 对抗鲁棒性评测未配置就绪:" + ";".join(reasons)
            + "。离线验证框架请改用 --classifier stub;确需在线评测,请在配置中"
            "设置 vlm_online: true 并提供 glm_api_key(红线:开启后扰动变体仅"
            "发往 glm_base_url,且 GLM 结果只是特征,最终判定与举报仍须人工确认)。"
        )


def _build_classifier(classifier_name: str, cfg: Config):
    """经工厂惰性获取分类器;失败抛 :class:`AdversarialError`(中文指引)。"""
    _preflight_online_classifier(classifier_name, cfg)
    try:
        return get_classifier(classifier_name, cfg)
    except ValueError as exc:
        raise AdversarialError(
            f"无法创建分类器 '{classifier_name}':{exc}\n"
            "提示:离线验证框架请用 --classifier stub(开箱即用);"
            "glm 需配置 vlm_online 与 glm_api_key;"
            "nudenet / clip 需安装对应可选依赖(pip install '.[vision]' / '.[clip]')。"
        ) from exc


# ---------------------------------------------------------------------------
# V9 统计推断层:配对 bootstrap CI / 符号翻转置换检验 / Wilson CI
# (全部纯 stdlib:random.Random 固定种子 + math 闭式公式,零第三方依赖)
# ---------------------------------------------------------------------------


def _kind_stats_seed(kind: str, offset: int) -> int:
    """按扰动类型与用途派生确定性子种子(与调用顺序无关)。

    offset 语义:1 → bootstrap CI;2 → 置换检验。同一 (kind, offset)
    恒得同一种子,因此同一份降幅数据在任何机器、任何次运行下产出逐位一致
    的推断结果;不同扰动类型之间互不影响。
    """
    return (STATS_SEED + PERTURB_TYPES.index(kind) * 1009 + offset) % (2**31)


def bootstrap_mean_ci95(
    values: list[float],
    *,
    b: int = BOOTSTRAP_B,
    seed: int = STATS_SEED,
) -> tuple[float, float]:
    """配对 percentile bootstrap 95% CI(对 ``values`` 均值的区间估计)。

    ``values`` 是逐图**配对**降幅(同一张图的原始分 − 扰动分),配对结构
    已在差值中消去图片间基线差异;对其做有放回重采样 ``b`` 次、每次取
    均值,再取 2.5% / 97.5% 次序统计量(最近秩法,序号 ``⌊q·(b−1)⌋``)。

    - ``random.Random(seed)`` 固定种子 → 完全确定性,同数据同种子必得
      同区间(评测报告可复现);
    - 全零输入 → 区间收缩为 ``(0.0, 0.0)``(覆盖真值 0);
    - 空 ``values`` → ValueError(中文)。

    B=10000 次 × n 个样本在纯 Python 下为毫秒~秒级,无需 numpy。
    """
    n = len(values)
    if n == 0:
        raise ValueError("bootstrap_mean_ci95:输入为空,无法计算置信区间")
    rng = random.Random(seed)
    means: list[float] = [sum(rng.choices(values, k=n)) / n for _ in range(b)]
    means.sort()
    return means[int(0.025 * (b - 1))], means[int(0.975 * (b - 1))]


def signflip_permutation_pvalue(
    values: list[float],
    *,
    n_mc: int = PERMUTATION_MC_N,
    seed: int = STATS_SEED,
    exact_max_n: int = PERMUTATION_EXACT_MAX_N,
) -> tuple[float, str]:
    """符号翻转**配对**置换检验(双侧,原假设 H0:平均降幅 = 0)。

    检验统计量 = 带符号降幅之和(等价于均值,n 固定);置换方式为随机
    翻转各降幅符号——这是配对差情形下自然的精确置换族(对称性来自配对
    结构,不假设分布形状)。

    - n ≤ ``exact_max_n``:精确枚举全部 2^n 次符号翻转,p = 满足
      |T_perm| ≥ |T_obs| 的置换占比(观测组合本身在枚举中,无需校正);
    - n 更大:``n_mc`` 次蒙特卡洛抽样,p = (满足数 + 1) / (n_mc + 1),
      +1 校正保证 p 永不为 0 且期望无偏;
    - 边界行为:全零降幅 → p 恰为 1(零效应不误报显著);恒正降幅 →
      观测统计量是置换族最大值,p 极小(强效应检出)。

    返回 ``(p_value, method)``,method ∈ {"exact", "monte_carlo"}。
    """
    n = len(values)
    if n == 0:
        raise ValueError("signflip_permutation_pvalue:输入为空,无法检验")
    obs = abs(sum(values))
    if n <= exact_max_n:
        total = 1 << n
        count = 0
        for mask in range(total):
            flipped = 0.0
            for i in range(n):
                flipped += values[i] if (mask >> i) & 1 else -values[i]
            if abs(flipped) >= obs - _STAT_EPS:
                count += 1
        return count / total, "exact"
    rng = random.Random(seed)
    count = 0
    for _ in range(n_mc):
        flipped = 0.0
        for v in values:
            flipped += v if rng.getrandbits(1) else -v
        if abs(flipped) >= obs - _STAT_EPS:
            count += 1
    return (count + 1) / (n_mc + 1), "monte_carlo"


def wilson_ci95(k: int, n: int, *, z: float = _Z95) -> tuple[float, float]:
    """Wilson score 95% CI(二项占比 k/n 的闭式区间,纯 ``math``)。

    center ± half,其中 center = (p + z²/2n) / (1 + z²/n),
    half = z/(1 + z²/n) · √(p(1−p)/n + z²/4n²),p = k/n。

    相对 Wald 正态近似,Wilson 在小样本与极端占比下性质正确:

    - k=0 → 恰为 ``(0, z²/(n+z²))``(下界贴 0,上界仍给信息量);
    - k=n → 恰为 ``(n/(n+z²), 1)``(上界贴 1,不越界);
    - 任何 0 ≤ k ≤ n 区间都在 [0, 1] 内且覆盖点估计 k/n 的邻域。

    n ≤ 0 或 k 越界 → ValueError(中文)。
    """
    if n <= 0:
        raise ValueError(f"wilson_ci95:样本数必须为正,收到 n={n}")
    if not 0 <= k <= n:
        raise ValueError(f"wilson_ci95:成功数 k={k} 超出 [0, {n}]")
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return max(0.0, center - half), min(1.0, center + half)


# ---------------------------------------------------------------------------
# 感知感知(perceptual-aware)扰动预算曲线:SSIM / 鲁棒 AUC / min_effective
# (全部纯 stdlib:float 数学,零 numpy;Pillow 惰性,仅灰度化与像素读取用)
# ---------------------------------------------------------------------------


def _flattened_pixels(gray_image: Any) -> list[int]:
    """灰度图 → 行展平的像素值序列(旧 Pillow ``getdata``,新 Pillow
    ``get_flattened_data``;按可用性择一,消除弃用告警且跨版本兼容)。"""
    flattened = getattr(gray_image, "get_flattened_data", None)
    if callable(flattened):
        return list(flattened())
    return list(gray_image.getdata())


def compute_ssim(
    reference: Any,
    compared: Any,
    *,
    win: int = GRID_SSIM_WIN,
    data_range: float = 255.0,
    k1: float = 0.01,
    k2: float = 0.03,
) -> float:
    """SSIM(结构相似度)纯 stdlib 实现:8x8 非重叠窗口 + 全域 float 数学。

    对标 2025-26 攻防报告"以感知度量约束扰动预算"的范式:固定 L_p 参数
    (radius / quality / …)只刻画操作强度,不刻画**人眼感知强度**;SSIM 把
    每个变体映射到 [0, 1] 的感知相似度(1 = 与原图逐像素一致),使四类
    扰动可在同一感知标尺上比较。

    - 输入:图片路径或 PIL Image 对象(路径先 ``PIL.Image.open`` 再统一
      ``convert("L")`` 灰度化——L = (299R + 587G + 114B)/1000);
    - 窗口:边长 ``win`` 的非重叠窗口(不足一个整窗的边缘不计;任一维度
      小于 ``win`` 时窗口收缩到 ``min(宽, 高)``,小图也能给出定义良好的
      值),每窗按标准 SSIM 公式(均值 / 方差 / 协方差,C1=(k1·L)²,
      C2=(k2·L)²)取值后全图平均;
    - 恒等输入 → 恰为 1.0(浮点上分子分母逐项相等);常量图窗口方差为 0,
      由 C1/C2 正则项保持公式有定义(闭式可手算,测试对照用);
    - 尺寸不一致 / 窗口非法 → ValueError(中文);未安装 Pillow →
      :class:`AdversarialError`(中文,CLI 层转退出码 2)。
    """
    if win < 1:
        raise ValueError(f"compute_ssim:窗口边长必须为正,收到 win={win}")
    pil = _load_pil()
    if pil is None:
        raise AdversarialError(
            "未安装 Pillow,无法计算 SSIM 感知相似度(需要 PIL 的 "
            "convert('L') 灰度化与像素读取;pip install Pillow 后重试)"
        )

    def _to_gray(item: Any) -> Any:
        if hasattr(item, "convert") and (
            hasattr(item, "getdata") or hasattr(item, "get_flattened_data")
        ):
            return item.convert("L")
        with pil.Image.open(item) as opened:
            opened.load()
            return opened.convert("L")

    gray_a = _to_gray(reference)
    gray_b = _to_gray(compared)
    if gray_a.size != gray_b.size:
        raise ValueError(
            f"compute_ssim:两图尺寸不一致 {gray_a.size} vs {gray_b.size},无法逐窗比较"
        )
    width, height = gray_a.size
    if width < 1 or height < 1:
        raise ValueError("compute_ssim:图像尺寸为空,无法计算 SSIM")

    win_eff = min(win, width, height)
    pixels_a = _flattened_pixels(gray_a)
    pixels_b = _flattened_pixels(gray_b)
    c1 = (k1 * data_range) ** 2
    c2 = (k2 * data_range) ** 2
    total = 0.0
    windows = 0
    for y0 in range(0, height - win_eff + 1, win_eff):
        for x0 in range(0, width - win_eff + 1, win_eff):
            sum_x = 0.0
            sum_y = 0.0
            sum_xx = 0.0
            sum_yy = 0.0
            sum_xy = 0.0
            for row in range(win_eff):
                base = (y0 + row) * width + x0
                for col in range(win_eff):
                    va = float(pixels_a[base + col])
                    vb = float(pixels_b[base + col])
                    sum_x += va
                    sum_y += vb
                    sum_xx += va * va
                    sum_yy += vb * vb
                    sum_xy += va * vb
            n = float(win_eff * win_eff)
            mean_x = sum_x / n
            mean_y = sum_y / n
            # 方差 / 协方差的一阶公式在常量窗口下可为 -1e-10 量级的浮点噪声,
            # 截断到 0 保持 SSIM 语义(方差非负)。
            var_x = max(sum_xx / n - mean_x * mean_x, 0.0)
            var_y = max(sum_yy / n - mean_y * mean_y, 0.0)
            cov_xy = sum_xy / n - mean_x * mean_y
            numerator = (2.0 * mean_x * mean_y + c1) * (2.0 * cov_xy + c2)
            denominator = (mean_x * mean_x + mean_y * mean_y + c1) * (var_x + var_y + c2)
            total += numerator / denominator
            windows += 1
    return total / windows


def robustness_auc(eps_values: list[float], drops: list[float]) -> float:
    """鲁棒 AUC:平均降幅对 ε 的归一化梯形面积(ε 严格升序)。

    AUC = Σᵢ (dropᵢ + dropᵢ₊₁)/2 · (εᵢ₊₁ − εᵢ) / (ε_max − ε_min)
    ——即 drop-ε 曲线下面积按 ε 跨度归一,量纲 = "该扰动族在网格范围内的
    平均掉分":越大 = 分类器对该扰动族整体越不鲁棒。降幅带符号(与单点
    ``avg_drop`` 口径一致,扰动后分数反升会抵减面积)。

    - ε 序列必须**严格递增**(同长度、非空,否则 ValueError 中文);
    - 单点输入(无法构成区间)→ 0.0;
    - 纯 float 数学,确定性。
    """
    n = len(eps_values)
    if n == 0 or n != len(drops):
        raise ValueError(
            f"robustness_auc:ε 序列与降幅序列须等长且非空(收到 {n} 与 {len(drops)})"
        )
    for i in range(1, n):
        if not eps_values[i] > eps_values[i - 1]:
            raise ValueError(
                "robustness_auc:ε 序列必须严格递增"
                f"(位置 {i}:{eps_values[i - 1]} → {eps_values[i]})"
            )
    if n == 1:
        return 0.0
    area = 0.0
    for i in range(n - 1):
        area += (drops[i] + drops[i + 1]) / 2.0 * (eps_values[i + 1] - eps_values[i])
    return area / (eps_values[-1] - eps_values[0])


def min_effective_attack(
    points: list[dict[str, Any]],
    *,
    threshold: float = DROP_ALERT_LINE,
) -> dict[str, Any] | None:
    """ε 升序扫描,返回**首个 |平均降幅| > threshold** 的网格点(无则 None)。

    - 输入 ``points`` 为曲线点列(任意顺序,内部按 ``eps`` 升序排序扫描),
      每点至少含 ``param / eps / avg_drop``,SSIM 取 ``avg_ssim``(可缺省 →
      None);阈值默认 :data:`DROP_ALERT_LINE`(0.2,与单点"占比"口径同线);
    - 返回 ``{"param", "eps", "ssim", "avg_drop"}``:最小有效攻击强度及其
      感知相似度——"再弱一档已安全、这档开始显著掉分"的分界,直接指导
      preprocess 反混淆参数的下限选择(反混淆至少须覆盖该强度);
    - 判定用严格大于:降幅恰等于阈值(如 jpeg q=60 恰掉 0.2)不计入——
      与单点 ``drop_gt_0.2_ratio`` 的 ``> 0.2`` 口径一致。
    """
    for point in sorted(points, key=lambda item: float(item["eps"])):
        if abs(float(point["avg_drop"])) > threshold:
            return {
                "param": point["param"],
                "eps": point["eps"],
                "ssim": point.get("avg_ssim"),
                "avg_drop": point["avg_drop"],
            }
    return None


def curve_shape_digest(
    eps_values: list[float],
    drops: list[float],
    *,
    flat_tol: float = SHAPE_FLAT_TOL,
) -> str:
    """score-vs-ε 点列的规范化形状签名:逐段斜率符号序列量化为 hex。

    编码:第 i 段斜率 = (dropᵢ₊₁ − dropᵢ)/(εᵢ₊₁ − εᵢ),符号量化为一位
    hex——"0" 平坦(|slope| ≤ ``flat_tol``)/ "1" 上升 / "2" 下降;整条曲线
    的指纹 = 按段拼接的 hex 串(长度 = 网格点数 − 1,如 blur 5 点 → 4 位)。
    纯 float 数学、零随机源 → 同一点列任何次运行指纹逐位一致;单调不降曲线
    恒为 "1…1",全零曲线恒为 "0…0"。

    **平移敏感性(设计动机)**:鲁棒 AUC 是整条曲线对 ε 的归一化梯形积分
    ——单个标量;"min_effective_eps" 只看首个 |降幅|>0.2 的越阈点。把掉分
    质量在段间**重新分配**(局部峰/谷沿 ε 轴平移、形状重排)可以在 AUC 与
    端点完全不变的前提下彻底改变曲线形状:例如 drops [0.05, 0.45, 0.15,
    0.15, 0.05] 与 [0.05, 0.93, 0.02, 0.036, 0.05](ε 网格 [1,2,3,5,8])
    的 AUC 同为 1.15/7、首越阈点同为 ε=2,但逐段斜率符号 "1202" →
    "1211"(两段翻转)——本指纹恰好捕获这类既有数值指标的盲区。垂直方向
    的整条平移会改变 AUC 本身,由数值容差拦截;水平方向(ε 轴)的平移则
    同时移动越阈点,由 min_effective_eps 拦截——本指纹专攻**形状重排**
    这一残余盲区。

    - ε 序列必须**严格递增**(同长度、非空,否则 ValueError 中文,与
      :func:`robustness_auc` 同款校验);``flat_tol`` 负数 → ValueError;
    - 单点输入(无段)→ 空串(仅理论边界:网格恒 ≥ 4 点);
    - 容差语义(见 :data:`CURVE_SHAPE_FLIP_TOL`):两条指纹逐位比较,
      "允许 k 段斜率符号翻转" = 至多 k 个位置不同;网格不同的指纹不会
      相互比对(网格在曲线指纹键中)。
    """
    n = len(eps_values)
    if n == 0 or n != len(drops):
        raise ValueError(
            f"curve_shape_digest:ε 序列与降幅序列须等长且非空(收到 {n} 与 {len(drops)})"
        )
    if flat_tol < 0:
        raise ValueError(f"curve_shape_digest:flat_tol 必须非负,收到 {flat_tol}")
    for i in range(1, n):
        if not eps_values[i] > eps_values[i - 1]:
            raise ValueError(
                "curve_shape_digest:ε 序列必须严格递增"
                f"(位置 {i}:{eps_values[i - 1]} → {eps_values[i]})"
            )
    symbols: list[str] = []
    for i in range(n - 1):
        slope = (drops[i + 1] - drops[i]) / (eps_values[i + 1] - eps_values[i])
        if slope > flat_tol:
            symbols.append("1")  # 上升
        elif slope < -flat_tol:
            symbols.append("2")  # 下降
        else:
            symbols.append("0")  # 平坦
    return "".join(symbols)


def _shape_flip_count(baseline: str, current: str) -> int | None:
    """两条形状指纹的斜率符号翻转段数(逐位比较);长度不一致 → None。

    网格不同的曲线指纹不会相互比对(ε 网格在曲线指纹键中),长度不一致
    属理论不可达的防御分支,由调用方按违例处理。
    """
    if len(baseline) != len(current):
        return None
    return sum(1 for a, b in zip(baseline, current) if a != b)



# ---------------------------------------------------------------------------
# V10.4 金标回归门禁:指纹 / 容差 / 金标读写 / 逐指标比对
# (纯 stdlib:hashlib + json;容差直接复用 V9 统计推断层的 CI 半宽)
# ---------------------------------------------------------------------------


def _perturb_signature() -> dict[str, Any]:
    """扰动参数指纹分量(金标键的一部分:扰动口径变 → 全部指纹轮换)。"""
    return {
        "types": list(PERTURB_TYPES),
        "blur_radius": _BLUR_RADIUS,
        "mosaic_block": _MOSAIC_BLOCK,
        "occlude_area_ratio": _OCCLUDE_AREA_RATIO,
        "jpeg_quality": _JPEG_QUALITY,
        "drop_alert_line": DROP_ALERT_LINE,
    }


def _fingerprint_digest(
    classifier_name: str,
    pairs: list[tuple[ImageEvidence, str]],
    labels_sha256: str,
) -> str:
    """按"分类器注册名 + 语料文件名:sha256 集合 + 扰动参数 + 统计版本"取指纹。

    - ``文件名:sha256`` 成对纳入:内容变化、文件增删、重命名都会换键
      (stub 按文件名打分,重命名本身就是评分口径变化,必须换键);
    - labels.json 自身 sha256 一并纳入(标注分布属于评测口径);
    - 规范化 JSON(sort_keys + 紧凑分隔符)再 sha256,任何机器逐位一致。
    """
    basis = {
        "classifier": classifier_name,
        "corpus": sorted(f"{Path(ev.path).name}:{ev.sha256}" for ev, _ in pairs),
        "labels_sha256": labels_sha256,
        "perturb": _perturb_signature(),
        "stats_version": GOLDEN_STATS_VERSION,
    }
    canonical = json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_fingerprint(classifier_name: str, corpus_dir: str | os.PathLike[str]) -> str:
    """对外便捷入口:装载语料并计算金标指纹键(语料非法透传 BenchmarkError)。"""
    pairs = load_corpus(corpus_dir)
    return _fingerprint_digest(
        classifier_name, pairs, _sha256_of(Path(corpus_dir) / "labels.json")
    )


def metric_tolerance(metric: str, value: float, stats_row: dict[str, Any]) -> float:
    """计算单个指标的容差:``max(|value| × TOL_VALUE_RATIO, 对应 CI 半宽)``。

    - ``avg_drop`` / ``avg_abs_drop`` → V9 配对 bootstrap ``ci95`` 半宽;
    - ``drop_gt_0.2_ratio`` → Wilson 95% CI 半宽。

    统计上自洽:重采样 / 小语料噪声天然落在 CI 半宽内(不误报),系统性
    退化则超出(不漏报);CI 收缩为 [0,0] 时(stub 确定性全零降幅)容差
    退化为 10%·|基线|,确定性数据下任何漂移都被拦截。
    非门禁指标名 → ValueError(中文)。
    """
    if metric not in GATE_METRICS:
        raise ValueError(f"metric_tolerance:非门禁指标 {metric!r}(允许:{list(GATE_METRICS)})")
    bounds = stats_row["wilson_ci"] if metric == "drop_gt_0.2_ratio" else stats_row["ci95"]
    ci_half = (float(bounds[1]) - float(bounds[0])) / 2.0
    return max(abs(float(value)) * TOL_VALUE_RATIO, ci_half)


def build_golden_entry(stats: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """由基准 payload 的 stats 构建单指纹金标条目 ``{"<扰动>/<指标>": {value, tol}}``。"""
    entry: dict[str, dict[str, float]] = {}
    for row in stats:
        for metric in GATE_METRICS:
            value = float(row[metric])
            entry[f"{row['type']}/{metric}"] = {
                "value": round(value, 4),
                "tol": round(metric_tolerance(metric, value, row), 6),
            }
    return entry


def load_golden(path: str | os.PathLike[str]) -> dict[str, Any]:
    """读金标文件并做结构校验;文件不存在 → ``{}``(视作空金标,缺基线路径)。

    损坏 / 坏 JSON / 顶层非对象 / 版本不兼容 / 指标表结构非法 →
    :class:`AdversarialGoldenError`(中文;CLI 退出码 1)。
    下划线开头的键为元数据(``_schema`` 等),不参与指纹比对。
    """
    golden = Path(path)
    if not golden.exists():
        return {}
    try:
        raw = json.loads(golden.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdversarialGoldenError(
            f"金标文件无法解析:{golden}({exc});请修复或删除后用 --update-golden 重建"
        ) from exc
    if not isinstance(raw, dict):
        raise AdversarialGoldenError(
            f"金标文件顶层必须是对象(fingerprint → 指标表),当前为 {type(raw).__name__}:{golden}"
        )
    schema = raw.get("_schema")
    if schema is not None and schema != GOLDEN_SCHEMA:
        raise AdversarialGoldenError(
            f"金标文件版本不兼容:期望 {GOLDEN_SCHEMA},实际 {schema!r};请 --update-golden 重建"
        )
    for key, entry in raw.items():
        if key.startswith("_"):
            continue
        if not isinstance(entry, dict) or not entry:
            raise AdversarialGoldenError(
                f"金标文件结构非法:指纹 {key[:8]}… 的指标表缺失或为空(应为 "
                '"<扰动>/<指标>": {value, tol})'
            )
        for metric, spec in entry.items():
            if (
                not isinstance(metric, str)
                or "/" not in metric
                or not isinstance(spec, dict)
                or set(spec) != {"value", "tol"}
                or not isinstance(spec["value"], (int, float))
                or isinstance(spec["value"], bool)
                or not isinstance(spec["tol"], (int, float))
                or isinstance(spec["tol"], bool)
                or float(spec["tol"]) < 0.0
            ):
                raise AdversarialGoldenError(
                    f"金标文件结构非法:指纹 {key[:8]}… 的指标 {metric!r} 应为 "
                    "{value: 数值, tol: 非负数值}"
                )
    return raw


def save_golden(
    path: str | os.PathLike[str],
    fingerprint: str,
    entry: dict[str, dict[str, float]],
    *,
    classifier_name: str | None = None,
    corpus_files: int | None = None,
) -> None:
    """写出金标文件(整体重建:只保留本次指纹的条目,父目录自动创建)。

    元数据(``_`` 前缀键)记录结构版本 / 统计版本 / 容差规则 / 重建依据,
    ``sort_keys=True`` 使输出逐字节稳定(指纹为十六进制,恒排在 ``_`` 键后)。
    """
    data: dict[str, Any] = {
        "_schema": GOLDEN_SCHEMA,
        "_stats_version": GOLDEN_STATS_VERSION,
        "_tolerance_rule": _TOL_RULE_TEXT,
        "_updated_at": now_iso(),
        "_basis": {
            "classifier": classifier_name,
            "corpus_files": corpus_files,
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

    - 比对对象:每类扰动 × :data:`GATE_METRICS`(4 类 × 3 指标 = 12 项);
    - 基线在金标中缺失该指标键(统计版本过旧)→ 按"指标缺基线"违例,
      宁可拦截也不静默跳过(fail-safe 方向);
    - 违例项含指标名 / 基线 / 当前 / 容差 / 偏差五要素,中文 message 直印。
    """
    violations: list[dict[str, Any]] = []
    for row in stats:
        for metric in GATE_METRICS:
            key = f"{row['type']}/{metric}"
            spec = golden_entry.get(key)
            current = float(row[metric])
            if spec is None:
                violations.append(
                    {
                        "kind": "missing_metric",
                        "metric": key,
                        "perturb": row["type"],
                        "metric_name": metric,
                        "baseline": None,
                        "current": current,
                        "tol": None,
                        "delta": None,
                        "message": (
                            f"{row['label']}({row['type']})·{metric}:金标缺该指标基线"
                            f"(统计版本过旧?),请 --update-golden 重建"
                        ),
                    }
                )
                continue
            baseline = float(spec["value"])
            tol = float(spec["tol"])
            delta = abs(current - baseline)
            if delta > tol + _STAT_EPS:
                violations.append(
                    {
                        "kind": "metric_violation",
                        "metric": key,
                        "perturb": row["type"],
                        "metric_name": metric,
                        "baseline": baseline,
                        "current": current,
                        "tol": tol,
                        "delta": round(delta, 6),
                        "message": (
                            f"{row['label']}({row['type']})·{metric}:基线 {baseline:.4f}"
                            f" → 当前 {current:.4f},偏差 {delta:.4f} 超出容差 {tol:.4f}"
                        ),
                    }
                )
    return violations


def _missing_baseline_violation(
    fingerprint: str,
    golden: Path,
    *,
    rebuild_flag: str = "--update-golden",
    detail: str = "分类器/语料/扰动参数或统计版本已变化",
) -> dict[str, Any]:
    """构造"指纹缺基线"违例(missing_baseline=fail 时按回归违例处理)。

    ``rebuild_flag`` / ``detail`` 供曲线金标门禁复用本形态(单点默认值
    与历史版本逐字节一致)。
    """
    return {
        "kind": "missing_baseline",
        "metric": None,
        "perturb": None,
        "metric_name": None,
        "baseline": None,
        "current": None,
        "tol": None,
        "delta": None,
        "fingerprint": fingerprint,
        "message": (
            f"指纹 {fingerprint[:12]}… 在金标 {golden} 中无基线({detail}),"
            f"missing_baseline=fail 按违例处理;"
            f"确认变更合理后请先 {rebuild_flag} 重建金标"
        ),
    }


def _update_diff_lines(
    old_raw: dict[str, Any], new_fingerprint: str, new_entry: dict[str, dict[str, float]]
) -> list[str]:
    """--update-golden 的变更 diff 摘要(中文行,CLI 直印留痕)。"""
    lines: list[str] = []
    old_fps = [key for key in old_raw if not key.startswith("_")]
    if new_fingerprint in old_raw:
        old_entry = old_raw[new_fingerprint]
        changed: list[str] = []
        unchanged = 0
        for key, spec in new_entry.items():
            old_spec = old_entry.get(key)
            if old_spec is None:
                changed.append(f"{key}:新增(值 {float(spec['value']):.4f})")
            elif abs(float(spec["value"]) - float(old_spec["value"])) > _STAT_EPS:
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
            "容差=max(10%·|基线|, CI 半宽))"
        )
    for gone in (key for key in old_fps if key != new_fingerprint):
        lines.append(
            f"指纹 {gone[:12]}…:移除(与当前分类器/语料/参数不再匹配,金标按本次结果整体重建)"
        )
    return lines


def _gate_section(
    payload: dict[str, Any],
    *,
    pairs: list[tuple[ImageEvidence, str]],
    corpus_dir: str | os.PathLike[str],
    classifier_name: str,
    golden_path: str | os.PathLike[str],
    update_golden: bool,
    missing_baseline: str,
) -> dict[str, Any]:
    """执行金标门禁并把结果挂到 ``payload["gate"]``(由 :func:`run` 调用)。

    - ``update_golden=True``:重建优先于比对(先建基线,随后可直接 ``--gate``
      复跑);打印性 diff 摘要存入 ``update_diff``;
    - 比对模式:指纹命中 → 逐指标 :func:`evaluate_gate`;未命中 → 缺基线,
      warn(默认)记 warnings 不拦截,fail 记违例(退出码 2)。
    """
    if missing_baseline not in MISSING_BASELINE_POLICIES:
        raise AdversarialError(
            f"missing_baseline 取值非法:{missing_baseline!r}"
            f"(允许:{sorted(MISSING_BASELINE_POLICIES)})"
        )
    golden = Path(golden_path)
    fingerprint = _fingerprint_digest(
        classifier_name, pairs, _sha256_of(Path(corpus_dir) / "labels.json")
    )
    raw = load_golden(golden)  # 不存在 → {};损坏 → AdversarialGoldenError(退出码 1)

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
            corpus_files=len(pairs),
        )
        telemetry.inc("adversarial.golden.updates")
        section.update({"status": "updated", "metrics_recorded": len(entry), "update_diff": diff})
        return section

    entry = raw.get(fingerprint)
    if entry is None:
        warning = (
            f"指纹 {fingerprint[:12]}… 在金标 {golden} 中缺基线"
            f"(分类器/语料/扰动参数或统计版本已变化),本次按警告跳过比对;"
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
        telemetry.inc("adversarial.gate.violations", amount=len(violations))
    section["status"] = "violations" if violations else "pass"
    section["violations"] = violations
    return section


# ---------------------------------------------------------------------------
# V13 感知曲线独立金标门禁:曲线指纹 / 容差 / 读写 / 逐指标比对
# (与单点金标完全独立:独立键、独立文件、独立容差规则;None↔数值即违例)
# ---------------------------------------------------------------------------


def _grid_signature() -> dict[str, Any]:
    """ε 网格参数指纹分量(曲线金标键的一部分:网格加密/换挡即换键)。"""
    return {kind: list(PERTURB_GRIDS[kind]) for kind in PERTURB_TYPES}


def _curve_fingerprint_digest(
    classifier_name: str,
    pairs: list[tuple[ImageEvidence, str]],
    labels_sha256: str,
) -> str:
    """曲线金标指纹 = **单点指纹** + ε 网格参数 + 曲线指标版本。

    - 直接内嵌 :func:`_fingerprint_digest` 的单点 sha256:分类器 / 语料
      (文件名:sha256)/ 标注 / 单点扰动参数 / 单点统计版本任一变化,
      单点指纹换键 → 曲线键随之换键(曲线评测口径与单点同源);
    - 叠加 ε 网格与 :data:`CURVE_GOLDEN_STATS_VERSION`:网格或曲线口径
      变化也换键,而**单点**统计版本演进不影响曲线键的独立语义;
    - 规范化 JSON(sort_keys + 紧凑分隔符)再 sha256,逐位一致。
    """
    basis = {
        "fingerprint": _fingerprint_digest(classifier_name, pairs, labels_sha256),
        "grid": _grid_signature(),
        "curve_stats_version": CURVE_GOLDEN_STATS_VERSION,
    }
    canonical = json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_curve_fingerprint(classifier_name: str, corpus_dir: str | os.PathLike[str]) -> str:
    """对外便捷入口:装载语料并计算曲线金标指纹键(语料非法透传 BenchmarkError)。"""
    pairs = load_corpus(corpus_dir)
    return _curve_fingerprint_digest(
        classifier_name, pairs, _sha256_of(Path(corpus_dir) / "labels.json")
    )


def curve_metric_tolerance(baseline: float) -> float:
    """曲线指标容差 = ``max(10%·|基线|, 0.05)`` 固定小容差。

    与单点容差(``max(10%·|基线|, CI 半宽)``)的差异:曲线指标(AUC /
    min_effective ε)是 ε 网格上的聚合量,V9 推断层不产出其 CI,故取
    固定小容差 0.05 下限——既容纳网格量化的离散噪声,又不放过系统性
    偏移。基线非法(非有限数值)→ ValueError(中文)。
    """
    value = float(baseline)
    if not math.isfinite(value):
        raise ValueError(f"curve_metric_tolerance:基线必须为有限数值,收到 {baseline!r}")
    return max(abs(value) * CURVE_TOL_VALUE_RATIO, CURVE_TOL_MIN)


def build_curve_golden_entry(curves: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """由 ``payload["curves"]`` 构建曲线金标条目 ``{"<扰动>/<指标>": {value, tol}}``。

    指标:每类扰动的 ``auc``、``min_effective_eps``(首个显著掉分点的
    ε;网格内未跌破 → ``{"value": None, "tol": None}``——None 无数值容差
    可言,互变语义由 :func:`evaluate_curve_gate` 单独裁决)与
    ``shape_digest``(形状指纹 hex;``tol`` = 允许斜率符号翻转段数,即
    :data:`CURVE_SHAPE_FLIP_TOL`,默认 0 严格)。
    """
    entry: dict[str, dict[str, Any]] = {}
    for kind in PERTURB_TYPES:
        curve = curves["kinds"][kind]
        auc = float(curve["auc"])
        entry[f"{kind}/auc"] = {
            "value": round(auc, 4),
            "tol": round(curve_metric_tolerance(auc), 6),
        }
        attack = curve["min_effective_attack"]
        if attack is None:
            entry[f"{kind}/min_effective_eps"] = {"value": None, "tol": None}
        else:
            eps = float(attack["eps"])
            entry[f"{kind}/min_effective_eps"] = {
                "value": round(eps, 6),
                "tol": round(curve_metric_tolerance(eps), 6),
            }
        entry[f"{kind}/shape_digest"] = {
            "value": curve["shape_digest"],
            "tol": int(CURVE_SHAPE_FLIP_TOL),
        }
    return entry


def _curve_spec_valid(metric: str, spec: Any) -> bool:
    """曲线金标指标条目结构校验:``{value, tol}`` 且按指标名分派口径——

    - ``*/shape_digest``:value 为非空 hex 符号串(字符 ∈ {0,1,2}),tol 为
      非负非 bool 整数(允许斜率符号翻转段数);
    - 其余(auc / min_effective_eps):tol=None 当且仅当 value=None(None
      基线无容差概念);数值基线的 tol 须为非负非 bool 实数。
    """
    if not isinstance(spec, dict) or set(spec) != {"value", "tol"}:
        return False
    value, tol = spec["value"], spec["tol"]
    if metric.endswith("/shape_digest"):
        return (
            isinstance(value, str)
            and bool(value)
            and all(ch in "012" for ch in value)
            and isinstance(tol, int)
            and not isinstance(tol, bool)
            and tol >= 0
        )
    if value is None:
        return tol is None
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isinstance(tol, (int, float))
        and not isinstance(tol, bool)
        and float(tol) >= 0.0
    )


def load_curve_golden(path: str | os.PathLike[str]) -> dict[str, Any]:
    """读曲线金标文件并做结构校验;文件不存在 → ``{}``(空金标,缺基线路径)。

    与单点 :func:`load_golden` 的差异:指标值允许 ``None``
    (min_effective 未跌破语义)且 **tol 与 value 必须同为 None 或同为
    数值**(None 无容差概念);``*/shape_digest`` 的 value 为形状指纹
    hex 符号串(字符 ∈ {0,1,2})、tol 为非负整数(允许翻转段数);其余
    (坏 JSON / 顶层非对象 / 版本不兼容 / 指标键无 ``/`` / tol 负数)
    同样抛 :class:`AdversarialGoldenError`(中文;CLI 退出码 1)。
    ``_`` 前缀键为元数据,不参与指纹比对。
    """
    golden = Path(path)
    if not golden.exists():
        return {}
    try:
        raw = json.loads(golden.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdversarialGoldenError(
            f"曲线金标文件无法解析:{golden}({exc});请修复或删除后用 "
            "--update-curve-golden 重建"
        ) from exc
    if not isinstance(raw, dict):
        raise AdversarialGoldenError(
            f"曲线金标文件顶层必须是对象(fingerprint → 曲线指标表),当前为 "
            f"{type(raw).__name__}:{golden}"
        )
    schema = raw.get("_schema")
    if schema is not None and schema != CURVE_GOLDEN_SCHEMA:
        raise AdversarialGoldenError(
            f"曲线金标文件版本不兼容:期望 {CURVE_GOLDEN_SCHEMA},实际 {schema!r};"
            "请 --update-curve-golden 重建"
        )
    for key, entry in raw.items():
        if key.startswith("_"):
            continue
        if not isinstance(entry, dict) or not entry:
            raise AdversarialGoldenError(
                f"曲线金标文件结构非法:指纹 {key[:8]}… 的指标表缺失或为空(应为 "
                '"<扰动>/<auc|min_effective_eps|shape_digest>": {value, tol})'
            )
        for metric, spec in entry.items():
            if (
                not isinstance(metric, str)
                or "/" not in metric
                or not _curve_spec_valid(metric, spec)
            ):
                raise AdversarialGoldenError(
                    f"曲线金标文件结构非法:指纹 {key[:8]}… 的指标 {metric!r} 应为 "
                    "{value: 数值|None, tol: 非负数值|None}(tol=None 当且仅当 "
                    "value=None);shape_digest 为 {value: '012' 组成的 hex 串, "
                    "tol: 非负整数(允许斜率符号翻转段数)"
                )
    return raw


def save_curve_golden(
    path: str | os.PathLike[str],
    fingerprint: str,
    entry: dict[str, dict[str, Any]],
    *,
    classifier_name: str | None = None,
    corpus_files: int | None = None,
) -> None:
    """写出曲线金标文件(整体重建;与单点金标互不触碰对方文件)。

    元数据(``_`` 前缀键)记录结构版本 / 曲线统计版本 / 曲线容差规则 /
    ε 网格 / 重建依据;``sort_keys=True`` 使输出逐字节稳定。
    """
    data: dict[str, Any] = {
        "_schema": CURVE_GOLDEN_SCHEMA,
        "_stats_version": CURVE_GOLDEN_STATS_VERSION,
        "_tolerance_rule": _CURVE_TOL_RULE_TEXT,
        "_updated_at": now_iso(),
        "_basis": {
            "classifier": classifier_name,
            "corpus_files": corpus_files,
            "metrics": list(CURVE_GATE_METRICS),
            "grid": _grid_signature(),
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


def evaluate_curve_gate(
    curves_kinds: dict[str, Any], golden_entry: dict[str, Any]
) -> list[dict[str, Any]]:
    """曲线指标逐项比对,返回违例清单(与 :func:`evaluate_gate` 同形态)。

    - 比对对象:每类扰动 × :data:`CURVE_GATE_METRICS`(4 类 × 3 指标
      = 12 项);``curves_kinds`` 即 ``payload["curves"]["kinds"]``;
    - ``auc``:数值比对 ``|当前 − 基线| > 容差``(容差 =
      ``max(10%·|基线|, 0.05)``,同单点的浮点 ε 防抖);
    - ``min_effective_eps``:**None↔数值互变即违例**(kind="null_flip"):
      基线 None → 当前检出 = 鲁棒性下降方向;基线数值 → 当前 None =
      曲线形态变化(变好也须人工复核,门禁只报不处置);两侧同为数值时
      按容差比对,同为 None 则通过;
    - ``shape_digest``:离散形状指纹——精确匹配通过,否则统计**斜率符号
      翻转段数**(逐位比较),超过容差 ``tol``(允许翻转段数,默认 0 严格)
      即违例(kind="shape_changed",拦截"曲线平移/重排但 AUC 与端点不变"
      的盲区);基线缺该键或当前载荷缺该字段 → "指标缺基线"违例
      (kind="missing_metric"),宁可拦截也不静默跳过;
    - 违例项含指标名 / 基线 / 当前 / 容差 / 偏差五要素,中文 message 直印。
    """
    violations: list[dict[str, Any]] = []
    # 按 PERTURB_TYPES 顺序遍历 curves_kinds 中实际存在的扰动类型
    # (完整运行恒为四类;单元测试可传入部分类型的手造 kinds)。
    for kind in (k for k in PERTURB_TYPES if k in curves_kinds):
        curve = curves_kinds[kind]
        label = curve["label"]
        current_auc = float(curve["auc"])
        attack = curve["min_effective_attack"]
        current_eps = None if attack is None else float(attack["eps"])

        # auc:纯数值比对
        key = f"{kind}/auc"
        spec = golden_entry.get(key)
        if spec is None:
            violations.append(
                {
                    "kind": "missing_metric",
                    "metric": key,
                    "perturb": kind,
                    "metric_name": "auc",
                    "baseline": None,
                    "current": current_auc,
                    "tol": None,
                    "delta": None,
                    "message": (
                        f"{label}({kind})·auc:曲线金标缺该指标基线"
                        "(曲线统计版本过旧?),请 --update-curve-golden 重建"
                    ),
                }
            )
        else:
            baseline = float(spec["value"])
            tol = float(spec["tol"])
            delta = abs(current_auc - baseline)
            if delta > tol + _STAT_EPS:
                violations.append(
                    {
                        "kind": "metric_violation",
                        "metric": key,
                        "perturb": kind,
                        "metric_name": "auc",
                        "baseline": baseline,
                        "current": current_auc,
                        "tol": tol,
                        "delta": round(delta, 6),
                        "message": (
                            f"{label}({kind})·鲁棒AUC:基线 {baseline:.4f} → "
                            f"当前 {current_auc:.4f},偏差 {delta:.4f} 超出容差 {tol:.4f}"
                            f"(容差=max(10%·|基线|, 0.05))"
                        ),
                    }
                )

        # min_effective_eps:None↔数值互变即违例;数值对按容差比对
        key = f"{kind}/min_effective_eps"
        spec = golden_entry.get(key)
        baseline_eps = None if spec is None else spec["value"]
        if spec is None:
            violations.append(
                {
                    "kind": "missing_metric",
                    "metric": key,
                    "perturb": kind,
                    "metric_name": "min_effective_eps",
                    "baseline": None,
                    "current": current_eps,
                    "tol": None,
                    "delta": None,
                    "message": (
                        f"{label}({kind})·min_effective_eps:曲线金标缺该指标基线"
                        "(曲线统计版本过旧?),请 --update-curve-golden 重建"
                    ),
                }
            )
        elif baseline_eps is None and current_eps is None:
            pass  # 两侧均未跌破:通过(shape_digest 比对仍须继续,不可 continue)
        elif baseline_eps is None or current_eps is None:
            if baseline_eps is None:
                message = (
                    f"{label}({kind})·min_effective_eps:基线网格内未跌破显著掉分线,"
                    f"当前在 ε={current_eps:.4f} 检出(鲁棒性下降方向)——"
                    "None↔数值互变即违例"
                )
            else:
                message = (
                    f"{label}({kind})·min_effective_eps:基线在 ε={baseline_eps:.4f} "
                    "检出显著掉分,当前网格内未检出(曲线形态变化,变好也须人工"
                    "复核)——None↔数值互变即违例"
                )
            violations.append(
                {
                    "kind": "null_flip",
                    "metric": key,
                    "perturb": kind,
                    "metric_name": "min_effective_eps",
                    "baseline": baseline_eps,
                    "current": current_eps,
                    "tol": None,
                    "delta": None,
                    "message": message,
                }
            )
        else:
            tol = float(spec["tol"])
            delta = abs(current_eps - float(baseline_eps))
            if delta > tol + _STAT_EPS:
                violations.append(
                    {
                        "kind": "metric_violation",
                        "metric": key,
                        "perturb": kind,
                        "metric_name": "min_effective_eps",
                        "baseline": float(baseline_eps),
                        "current": current_eps,
                        "tol": tol,
                        "delta": round(delta, 6),
                        "message": (
                            f"{label}({kind})·首个显著掉分ε:基线 {baseline_eps:.4f}"
                            f" → 当前 {current_eps:.4f},偏差 {delta:.4f} 超出容差 "
                            f"{tol:.4f}(容差=max(10%·|基线|, 0.05))"
                        ),
                    }
                )

        # shape_digest:离散形状指纹——精确匹配,或允许 ≤tol 段斜率符号翻转
        key = f"{kind}/shape_digest"
        spec = golden_entry.get(key)
        current_digest = curve.get("shape_digest")
        if spec is None or current_digest is None:
            violations.append(
                {
                    "kind": "missing_metric",
                    "metric": key,
                    "perturb": kind,
                    "metric_name": "shape_digest",
                    "baseline": None if spec is None else spec["value"],
                    "current": current_digest,
                    "tol": None,
                    "delta": None,
                    "message": (
                        f"{label}({kind})·shape_digest:"
                        + (
                            "曲线金标缺该指标基线(曲线统计版本过旧?)"
                            if spec is None
                            else "当前曲线载荷缺形状指纹字段(统计版本过旧?)"
                        )
                        + ",请 --update-curve-golden 重建"
                    ),
                }
            )
            continue
        baseline_digest = str(spec["value"])
        allowed_flips = int(spec["tol"])
        if baseline_digest != current_digest:
            flips = _shape_flip_count(baseline_digest, str(current_digest))
            if flips is None:
                violations.append(
                    {
                        "kind": "shape_changed",
                        "metric": key,
                        "perturb": kind,
                        "metric_name": "shape_digest",
                        "baseline": baseline_digest,
                        "current": current_digest,
                        "tol": allowed_flips,
                        "delta": None,
                        "message": (
                            f"{label}({kind})·shape_digest:曲线形状指纹基线 "
                            f"{baseline_digest} → 当前 {current_digest},段数不一致"
                            "(ε 网格应在指纹键中一致,理论不可达)——形状已变化"
                        ),
                    }
                )
            elif flips > allowed_flips:
                violations.append(
                    {
                        "kind": "shape_changed",
                        "metric": key,
                        "perturb": kind,
                        "metric_name": "shape_digest",
                        "baseline": baseline_digest,
                        "current": current_digest,
                        "tol": allowed_flips,
                        "delta": flips,
                        "message": (
                            f"{label}({kind})·shape_digest:曲线形状指纹基线 "
                            f"{baseline_digest} → 当前 {current_digest},{flips} 段"
                            f"斜率符号翻转超出容差 {allowed_flips} 段——拦截\"曲线"
                            "平移/重排但 AUC 与端点不变\"的形状盲区"
                        ),
                    }
                )
    return violations


def _curve_update_diff_lines(
    old_raw: dict[str, Any], new_fingerprint: str, new_entry: dict[str, dict[str, Any]]
) -> list[str]:
    """--update-curve-golden 的变更 diff 摘要(中文行,CLI 直印留痕)。"""
    lines: list[str] = []
    old_fps = [key for key in old_raw if not key.startswith("_")]
    if new_fingerprint in old_raw:
        old_entry = old_raw[new_fingerprint]
        changed: list[str] = []
        unchanged = 0
        for key, spec in new_entry.items():
            old_spec = old_entry.get(key)
            if old_spec is None:
                changed.append(
                    f"{key}:新增(值 {_fmt_curve_value(spec['value'])})"
                )
            elif _curve_value_changed(old_spec["value"], spec["value"]):
                changed.append(
                    f"{key}:{_fmt_curve_value(old_spec['value'])} → "
                    f"{_fmt_curve_value(spec['value'])}"
                )
            else:
                unchanged += 1
        removed = [key for key in old_entry if key not in new_entry]
        lines.append(
            f"指纹 {new_fingerprint[:12]}…:曲线基线刷新({unchanged} 项持平,"
            f"{len(changed)} 项变化,{len(removed)} 项移除)"
        )
        lines.extend(f"  变更 {line}" for line in changed)
        lines.extend(f"  移除 {key}(当前曲线统计不再产出该指标)" for key in removed)
    else:
        lines.append(
            f"指纹 {new_fingerprint[:12]}…:新增曲线基线({len(new_entry)} 项指标,"
            "容差=max(10%·|基线|, 0.05);min_effective None↔数值互变即违例;"
            "shape_digest 离散精确匹配)"
        )
    for gone in (key for key in old_fps if key != new_fingerprint):
        lines.append(
            f"指纹 {gone[:12]}…:移除(与当前分类器/语料/网格/曲线口径不再匹配,"
            "曲线金标按本次结果整体重建)"
        )
    return lines


def _fmt_curve_value(value: Any) -> str:
    """曲线金标 diff 摘要用的值格式化(None 直译为"未跌破";形状指纹 hex 原样)。"""
    if value is None:
        return "未跌破(None)"
    if isinstance(value, str):
        return value
    return f"{float(value):.4f}"


def _curve_value_changed(old: Any, new: Any) -> bool:
    """曲线基线值是否实质变化(None↔数值、字符串指纹不等与数值漂移都算,
    数值带浮点 ε 防抖;shape_digest 等字符串值按精确比较)。"""
    if old is None or new is None:
        return old is not new
    if isinstance(old, str) or isinstance(new, str):
        return old != new
    return abs(float(old) - float(new)) > _STAT_EPS


def _curve_gate_section(
    payload: dict[str, Any],
    *,
    pairs: list[tuple[ImageEvidence, str]],
    corpus_dir: str | os.PathLike[str],
    classifier_name: str,
    golden_path: str | os.PathLike[str],
    update_golden: bool,
    missing_baseline: str,
) -> dict[str, Any]:
    """执行曲线金标门禁并把结果挂到 ``payload["curve_gate"]``(由 :func:`run` 调用)。

    结构与单点 :func:`_gate_section` 同构:``mode`` ∈ ``update | check``、
    ``status`` ∈ ``updated | pass | violations | no_baseline``;两门禁读写
    **各自的金标文件与指纹键**,互不触碰、互不干扰。``missing_baseline``
    由 :func:`run` 解析后传入(曲线独立策略缺省继承单点值);比对指标为
    :data:`CURVE_GATE_METRICS`(auc / min_effective_eps / shape_digest)。
    """
    if missing_baseline not in MISSING_BASELINE_POLICIES:
        raise AdversarialError(
            f"missing_baseline 取值非法:{missing_baseline!r}"
            f"(允许:{sorted(MISSING_BASELINE_POLICIES)})"
        )
    golden = Path(golden_path)
    fingerprint = _curve_fingerprint_digest(
        classifier_name, pairs, _sha256_of(Path(corpus_dir) / "labels.json")
    )
    raw = load_curve_golden(golden)  # 不存在 → {};损坏 → AdversarialGoldenError(退出码 1)

    section: dict[str, Any] = {
        "mode": "update" if update_golden else "check",
        "golden_path": str(golden),
        "fingerprint": fingerprint,
        "missing_baseline": missing_baseline,
        "tolerance_rule": _CURVE_TOL_RULE_TEXT,
        "metrics_total": len(payload["curves"]["kinds"]) * len(CURVE_GATE_METRICS),
    }

    if update_golden:
        entry = build_curve_golden_entry(payload["curves"])
        diff = _curve_update_diff_lines(raw, fingerprint, entry)
        save_curve_golden(
            golden,
            fingerprint,
            entry,
            classifier_name=classifier_name,
            corpus_files=len(pairs),
        )
        telemetry.inc("adversarial.curve_golden.updates")
        section.update(
            {"status": "updated", "metrics_recorded": len(entry), "update_diff": diff}
        )
        return section

    entry = raw.get(fingerprint)
    if entry is None:
        warning = (
            f"指纹 {fingerprint[:12]}… 在曲线金标 {golden} 中缺基线"
            "(分类器/语料/网格参数或曲线统计版本已变化),本次按警告跳过比对;"
            "确认变更合理后请 --update-curve-golden 重建曲线金标"
        )
        if missing_baseline == "fail":
            section["status"] = "violations"
            section["violations"] = [
                _missing_baseline_violation(
                    fingerprint,
                    golden,
                    rebuild_flag="--update-curve-golden",
                    detail="分类器/语料/网格参数或曲线统计版本已变化",
                )
            ]
        else:
            section["status"] = "no_baseline"
            section["warnings"] = [warning]
        return section

    violations = evaluate_curve_gate(payload["curves"]["kinds"], entry)
    if violations:
        telemetry.inc("adversarial.curve_gate.violations", amount=len(violations))
    section["status"] = "violations" if violations else "pass"
    section["violations"] = violations
    return section


# ---------------------------------------------------------------------------
# 主流程与统计
# ---------------------------------------------------------------------------


def _classify_all(classifier: Any, evidences: list[ImageEvidence]) -> list[float]:
    """对全部证据(原图 + 变体)评分,返回与输入同序的 nsfw_prob 列表。

    V5 性能:优先走分类器协议的批量接口 ``classify_batch``——整轮基准从
    ``5N`` 次逐张 ``classify`` 合并为**一次**批量调用(glm / multi_provider
    等批量实现可合并传输);分类器未提供批量接口或返回长度不符时回退逐张,
    结果不变。
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


def _grid_curves_section(
    classifier: Any,
    pairs: list[tuple[ImageEvidence, str]],
    base_probs: list[float],
) -> dict[str, Any]:
    """执行 ε 网格评测并构建 ``payload["curves"]`` 段(``grid=True`` 时调用)。

    流程(全程确定性,无随机源;变体落临时目录,运行结束自动清理):

    - 逐图 × 逐扰动类型 × 逐 ε 网格点生成变体(:func:`_perturb_grid_variant`,
      文件名保留原 stem → stub 关键词不断链),整轮一次 ``_classify_all``
      批量评分(与单点路径同一协议);
    - 每个变体计算 SSIM(:func:`compute_ssim`,原图 vs 变体),即
      ``(perturb_kind, param, ssim)`` 三元组;
    - 按 (类型, 参数) 聚合出 score-vs-ε 点列(平均分 / 平均降幅 / 平均
      SSIM + 逐变体明细)、score-vs-SSIM 曲线(SSIM 降序)、鲁棒 AUC
      (:func:`robustness_auc`)与 ``min_effective_attack``
      (:func:`min_effective_attack`)。

    **与金标门禁正交**:本段只读单点评分产出的 ``base_probs``,不触碰
    stats / details / gate 的任何字段;GATE_METRICS 与指纹均只覆盖单点默认
    参数,curves 不进 stats、不进金标——``grid=True`` 不改变门禁行为。
    """
    with tempfile.TemporaryDirectory(prefix="netsentinel_adversarial_grid_") as tmp:
        variants_root = Path(tmp) / "grid"
        records: list[tuple[str, float, int, Path]] = []
        for index, (evidence, _label) in enumerate(pairs):
            for kind in PERTURB_TYPES:
                for param in PERTURB_GRIDS[kind]:
                    records.append(
                        (
                            kind,
                            param,
                            index,
                            _perturb_grid_variant(
                                evidence.path, variants_root, kind, param
                            ),
                        )
                    )
        evidences = [
            ImageEvidence(
                path=str(path),
                url=(
                    f"{pairs[index][0].url}#adversarial-grid:{kind}"
                    f":{_grid_param_tag(kind, param)}"
                ),
                source_page=pairs[index][0].source_page,
                sha256=_sha256_of(path),
                # 网格变体同样保持原始宽高(理由同单点路径),沿用嗅探值。
                width=pairs[index][0].width,
                height=pairs[index][0].height,
            )
            for kind, param, index, path in records
        ]
        probs = _classify_all(classifier, evidences)
        ssims = [
            compute_ssim(pairs[index][0].path, path)
            for kind, param, index, path in records
        ]
    telemetry.inc("adversarial.grid_variants", amount=len(records))

    # (文件名, 变体分, 降幅, SSIM)——聚合用未舍入值,落盘前统一 round。
    grouped: dict[tuple[str, float], list[tuple[str, float, float, float]]] = {}
    for (kind, param, index, path), prob, ssim_value in zip(records, probs, ssims):
        drop = base_probs[index] - prob
        grouped.setdefault((kind, param), []).append(
            (Path(pairs[index][0].path).name, prob, drop, ssim_value)
        )

    total = len(pairs)
    kinds_payload: dict[str, Any] = {}
    for kind in PERTURB_TYPES:
        meta = PERTURB_META[kind]
        points: list[dict[str, Any]] = []
        eps_values: list[float] = []
        avg_drops: list[float] = []
        for param in PERTURB_GRIDS[kind]:
            rows = grouped[(kind, param)]
            eps = round(_grid_strength(kind, param), 6)
            avg_drop = sum(row[2] for row in rows) / total
            avg_score = sum(row[1] for row in rows) / total
            avg_ssim = sum(row[3] for row in rows) / total
            eps_values.append(eps)
            avg_drops.append(avg_drop)
            points.append(
                {
                    "param": param,
                    "eps": eps,
                    "n": total,
                    "avg_score": round(avg_score, 4),
                    "avg_drop": round(avg_drop, 4),
                    "avg_ssim": round(avg_ssim, 4),
                    # 每变体附 (perturb_kind, param, ssim) 三元组(含文件名/分/降幅)。
                    "variants": [
                        {
                            "file": file,
                            "perturb_kind": kind,
                            "param": param,
                            "ssim": round(ssim_value, 4),
                            "prob": round(prob, 4),
                            "drop": round(drop, 4),
                        }
                        for file, prob, drop, ssim_value in rows
                    ],
                }
            )
        by_ssim = sorted(points, key=lambda point: point["avg_ssim"], reverse=True)
        kinds_payload[kind] = {
            "label": meta["label"],
            "param_name": meta["param_name"],
            "grid": list(PERTURB_GRIDS[kind]),
            "points": points,
            "score_vs_ssim": [
                [point["avg_ssim"], point["avg_score"]] for point in by_ssim
            ],
            "auc": round(robustness_auc(eps_values, avg_drops), 6),
            "min_effective_attack": min_effective_attack(points),
            # V13-2 形状指纹:逐 ε 段斜率符号序列(0 平坦/1 升/2 降),
            # 曲线金标的第三个门禁指标(拦截"平移/重排但 AUC 与端点不变")。
            "shape_digest": curve_shape_digest(eps_values, avg_drops),
        }

    return {
        "mode": "grid",
        "note": (
            "感知感知(perceptual-aware)扰动预算曲线:对标 2025-26 攻防报告"
            "从固定 L_p 参数转向 SSIM 约束 ε 网格的范式。本段与单点金标门禁正交:"
            "GATE_METRICS / 指纹 / 容差只覆盖单点默认参数,curves 不进 stats、"
            "不进单点金标,grid=True 不改变单点门禁行为;V13 起曲线指标另设"
            "独立金标键与独立文件(--curve-gate / --update-curve-golden)。"
        ),
        "ssim": {
            "window": GRID_SSIM_WIN,
            "grayscale": "PIL convert('L')",
            "data_range": 255,
            "implementation": "纯 stdlib float 数学(无 numpy),非重叠窗口 SSIM 均值",
        },
        "eps_semantics": dict(_GRID_EPS_SEMANTICS),
        "auc_definition": (
            "鲁棒 AUC = Σ (drop_i + drop_{i+1})/2 · Δε / (ε_max − ε_min)"
            "(梯形法则,ε 严格升序;降幅带符号,值越大 = 该扰动族整体掉分越重)"
        ),
        "min_effective_attack_definition": (
            f"ε 升序扫描首个 |平均降幅| > {DROP_ALERT_LINE} 的最小 ε(附该点"
            "平均 SSIM;None = 网格内无显著掉分)——指导 preprocess 反混淆参数"
            "至少覆盖该强度"
        ),
        "shape_digest_definition": (
            "曲线形状指纹:逐 ε 段斜率符号量化为 hex(0 平坦 / 1 上升 / "
            "2 下降,每段一位,长度 = 网格点数 − 1)。AUC 与 min_effective_eps"
            "对\"掉分质量段间重分配 / 局部峰平移\"不敏感(可同值),形状指纹"
            "恰好补此盲区;V13-2 起纳入曲线金标第三指标"
            f"(容差 = 允许斜率符号翻转段数,默认 {CURVE_SHAPE_FLIP_TOL} 严格)"
        ),
        "kinds": kinds_payload,
    }


def run(
    corpus_dir: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    classifier: str = "stub",
    *,
    gate: bool = False,
    golden_path: str | os.PathLike[str] | None = None,
    update_golden: bool = False,
    missing_baseline: str = "warn",
    grid: bool = False,
    curve_gate: bool = False,
    curve_golden_path: str | os.PathLike[str] | None = None,
    update_curve_golden: bool = False,
    curve_missing_baseline: str | None = None,
) -> dict[str, Any]:
    """跑一次对抗鲁棒性基准:扰动生成 → 评分 → 降幅统计 → 写报告。

    - 载入 ``corpus_dir/labels.json``(复用 A37 的 ``load_corpus``,标注取值
      ``nsfw | borderline | clean``),逐图取原始分;
    - 每图用 :func:`perturb` 生成四类变体到**临时目录**(运行结束自动清理,
      语料目录只读不被写入),逐一评分;
    - 统计每类扰动:平均绝对降幅(|降幅| 均值)、最大降幅(带符号最大值)、
      降幅>0.2 的图片占比;V9 起另附统计推断:平均降幅的配对 percentile
      bootstrap 95% CI(``ci95``)、符号翻转置换检验 p 值(``p_value``)、
      占比的 Wilson 95% CI(``wilson_ci``)——固定种子,同数据必得同结果;
    - V5 性能:变体生成与评分两趟循环合并——单趟循环建好"原图 + 四变体"
      的证据清单后**一次**批量评分(分类器协议支持时;否则回退逐张),
      N 张语料的分类器调用次数从 5N 次降为 1 次;
    - V5 可观测:``telemetry.timer("adversarial.run")`` 全程计时,图片数与
      变体数计入 ``adversarial.images`` / ``adversarial.variants``;
    - 写 ``out_dir/adversarial_report.md``(中文)+ ``adversarial_report.json``,
      返回写入 json 的同一份 payload(便于测试与上层复用);
    - 未安装 Pillow / 分类器不可用 / 语料问题 → 中文异常(CLI 层转退出码 2)。

    V10.4 金标门禁(全部默认关闭,``gate=False`` 即现状纯报告,payload 不
    新增任何键):

    - ``gate=True``:按指纹键比对金标基线,结果挂 ``payload["gate"]``
      (``status`` ∈ ``pass | violations | no_baseline | updated``,
      ``violations`` 为中文违例清单,含指标名/基线/当前/容差);
    - ``update_golden=True``:重建优先于比对,金标整体重写为本次结果
      (``--update-golden`` 语义,无确认直写,diff 摘要存 ``update_diff``);
    - ``golden_path``:金标路径,缺省 :data:`DEFAULT_GOLDEN_PATH`;
    - ``missing_baseline``:指纹未命中金标时 ``"warn"``(默认,记 warning
      不拦截)或 ``"fail"``(按回归违例处理);
    - 金标文件损坏 / 坏 JSON → :class:`AdversarialGoldenError`(CLI 层转
      退出码 1);违例判定本身只产出清单,退出码由 CLI 层裁决(违例 → 2)。

    感知扰动预算曲线(默认关闭,``grid=False`` 即现状单点报告,payload 不
    新增任何键):

    - ``grid=True``:在单点评测之外,按 :data:`PERTURB_GRIDS` 的 ε 网格逐
      (类型, 参数) 生成变体、批量评分并计算每变体 SSIM,产出
      ``payload["curves"]`` 段——每类扰动含 score-vs-ε 点列、score-vs-SSIM
      曲线、鲁棒 AUC(:func:`robustness_auc`)与 ``min_effective_attack``
      (:func:`min_effective_attack`);markdown 报告追加曲线表;
    - **与金标门禁正交**:``curves`` 不进 stats、不进金标,GATE_METRICS /
      指纹 / 容差只覆盖单点默认参数——``grid=True`` 时门禁的比对对象、
      指纹与行为和 ``grid=False`` 完全一致,两模式可自由组合;
    - 网格变体同样落临时目录自动清理;Pillow 缺失时同单点路径(中文异常,
      CLI 退出码 2)。

    V13 感知曲线独立金标门禁(默认关闭,单点金标零变动;与 ``--gate`` /
    ``--update-golden`` 并存互不干扰):

    - ``curve_gate=True`` / ``update_curve_golden=True``:按**独立指纹键**
      (单点指纹 + ε 网格 + :data:`CURVE_GOLDEN_STATS_VERSION`)比对曲线
      金标,结果挂 ``payload["curve_gate"]``(结构同单点 ``gate`` 段:
      ``status`` ∈ ``pass | violations | no_baseline | updated``,
      ``violations`` 为与 :func:`evaluate_gate` 同形态的中文清单);
    - 指标:每类扰动的鲁棒 AUC 与 ``min_effective_eps``(首个显著掉分
      ε);容差 = ``max(10%·|基线|, 0.05)``,**None↔数值互变即违例**
      (kind="null_flip",两个方向都拦截);
    - ``curve_golden_path``:曲线金标路径,缺省 :data:`DEFAULT_CURVE_GOLDEN_PATH`
      (独立文件,单点 ``--update-golden`` 不触碰它,反之亦然);
      ``missing_baseline`` 与单点门禁共用同一策略参数;
    - 曲线门禁的比对对象是曲线:开启任一曲线金标开关而未开 ``grid`` 时
      **自动附带** ε 网格评测(payload 相应出现 ``curves`` 段);单点
      ``gate`` 的行为与指纹不因此改变;
    - 曲线金标文件损坏 / 坏 JSON → :class:`AdversarialGoldenError`
      (CLI 层转退出码 1);违例判定只产出清单,退出码由 CLI 层裁决。

    V13-2 评测增强批(曲线金标三项扩展;单点金标零变动):

    - 曲线门禁指标扩为 ``(auc, min_effective_eps, shape_digest)``(4 类
      × 3 = 12 项):每类扰动的 curves 段新增 ``shape_digest`` 形状指纹
      (:func:`curve_shape_digest`),不匹配且斜率符号翻转段数超容差 →
      违例 kind="shape_changed";曲线统计版本递增至 v13-2,旧曲线金标
      指纹全部失效(按缺基线机制处置);
    - ``curve_missing_baseline``:曲线金标缺基线处置的**独立**策略参数,
      ``None``(默认)继承 ``missing_baseline`` 的值(既有行为零变动),
      显式传 ``"warn" | "fail"`` 则曲线独立于单点——单点 fail + 曲线 warn
      (或反之)可自由组合,非法取值由 :func:`_curve_gate_section` 校验
      (:class:`AdversarialError`,中文)。
    """
    if _load_pil() is None:
        raise AdversarialError(
            "未安装 Pillow,无法运行对抗鲁棒性基准(pip install Pillow 后重试;"
            "本工具以退出码 2 结束,不产出无效报告)"
        )

    cfg = Config()
    clf = _build_classifier(classifier, cfg)
    pairs = load_corpus(corpus_dir)
    if not pairs:
        raise AdversarialError(
            f"语料为空:{corpus_dir}(请检查 labels.json,或先用 "
            "`python benchmarks/run_benchmark.py --make-corpus` 重建语料)"
        )
    telemetry.inc("adversarial.images", amount=len(pairs))
    telemetry.inc("adversarial.variants", amount=len(pairs) * len(PERTURB_TYPES))

    with telemetry.timer("adversarial.run"):
        details: list[dict[str, Any]] = []
        drops: dict[str, list[float]] = {kind: [] for kind in PERTURB_TYPES}
        label_counts: dict[str, int] = {}

        # 单趟循环:逐图生成四变体并连同原图登记进评分清单
        # (kind=None 表示原图;base 恒先于其变体登记,drop 计算依赖该顺序)。
        entries: list[tuple[dict[str, Any], str | None, ImageEvidence]] = []
        with tempfile.TemporaryDirectory(prefix="netsentinel_adversarial_") as tmp:
            variants_root = Path(tmp)
            for evidence, label in pairs:
                label_counts[label] = label_counts.get(label, 0) + 1
                filename = Path(evidence.path).name
                row: dict[str, Any] = {
                    "file": filename,
                    "label": label,
                    "base_prob": 0.0,
                    "variants": {},
                }
                details.append(row)
                entries.append((row, None, evidence))
                for kind, variant_path in zip(PERTURB_TYPES, perturb(evidence.path, variants_root)):
                    path = Path(variant_path)
                    entries.append(
                        (
                            row,
                            kind,
                            ImageEvidence(
                                path=str(path),
                                url=f"{evidence.url}#adversarial:{kind}",
                                source_page=evidence.source_page,
                                sha256=_sha256_of(path),
                                # 四类扰动均保持原始宽高(马赛克放大回原尺寸、
                                # 遮挡仅粘贴、模糊/JPEG 编码不变尺寸),直接
                                # 沿用语料嗅探值即可。
                                width=evidence.width,
                                height=evidence.height,
                            ),
                        )
                    )

            # 合并评分:整轮一次批量(协议支持时),而非逐图逐变体各调一次。
            evidences = [entry[2] for entry in entries]
            probs = _classify_all(clf, evidences)
            base_prob = 0.0
            base_probs: list[float] = []  # 未舍入原始分(grid 曲线降幅用)
            for (row, kind, evidence_item), prob in zip(entries, probs):
                if kind is None:
                    base_prob = prob
                    base_probs.append(prob)
                    row["base_prob"] = round(prob, 4)
                    continue
                drop = base_prob - prob
                drops[kind].append(drop)
                row["variants"][kind] = {
                    "file": Path(evidence_item.path).name,
                    "prob": round(prob, 4),
                    "drop": round(drop, 4),
                }

        stats: list[dict[str, Any]] = []
        total = len(details)
        for kind in PERTURB_TYPES:
            values = drops[kind]
            meta = PERTURB_META[kind]
            # V9 统计推断层:配对 bootstrap CI / 符号翻转置换检验 / Wilson CI
            # (按扰动类型派生固定子种子 → 两次运行结果逐位一致)。
            ci_lo, ci_hi = bootstrap_mean_ci95(values, seed=_kind_stats_seed(kind, 1))
            p_value, p_method = signflip_permutation_pvalue(
                values, seed=_kind_stats_seed(kind, 2)
            )
            alerts = sum(1 for v in values if v > DROP_ALERT_LINE)
            w_lo, w_hi = wilson_ci95(alerts, total)
            stats.append(
                {
                    "type": kind,
                    "label": meta["label"],
                    "suffix": meta["suffix"],
                    "param": meta["param"],
                    "n": total,
                    "avg_abs_drop": round(sum(abs(v) for v in values) / total, 4),
                    "avg_drop": round(sum(values) / total, 4),
                    "max_drop": round(max(values), 4),
                    "drop_gt_0.2_ratio": round(alerts / total, 4),
                    # --- V9 新增字段(既有字段语义与数值不变) ---
                    "ci95": [round(ci_lo, 4), round(ci_hi, 4)],
                    "ci95_method": (
                        f"paired percentile bootstrap B={BOOTSTRAP_B}, "
                        f"seed=derived({STATS_SEED})"
                    ),
                    "p_value": round(p_value, 6),
                    "p_value_method": (
                        "sign-flip paired permutation (two-sided, "
                        + (
                            f"exact 2^{total} enumeration"
                            if p_method == "exact"
                            else f"monte carlo n={PERMUTATION_MC_N}, +1 corrected"
                        )
                        + ")"
                    ),
                    "wilson_ci": [round(w_lo, 4), round(w_hi, 4)],
                    "wilson_ci_method": "Wilson score interval, closed-form, z=1.96",
                }
            )

        payload: dict[str, Any] = {
            "generated_at": now_iso(),
            "classifier": clf.name or classifier,
            "classifier_name": classifier,
            "corpus": {
                "dir": str(Path(corpus_dir)),
                "total": total,
                "labels": dict(sorted(label_counts.items())),
            },
            "drop_alert_line": DROP_ALERT_LINE,
            "inference": {
                "seed": STATS_SEED,
                "bootstrap_b": BOOTSTRAP_B,
                "permutation_mc_n": PERMUTATION_MC_N,
                "permutation_exact_max_n": PERMUTATION_EXACT_MAX_N,
                "notes": (
                    "ci95:avg_drop(带符号平均降幅)的配对 percentile bootstrap "
                    f"95% CI(B={BOOTSTRAP_B},固定种子 {STATS_SEED} 按扰动类型"
                    "派生子种子,输出逐位确定);p_value:符号翻转配对置换检验"
                    f"(双侧,H0:平均降幅=0;n≤{PERMUTATION_EXACT_MAX_N} 精确枚举,"
                    f"否则 {PERMUTATION_MC_N} 次蒙特卡洛 +1 校正);wilson_ci:"
                    "降幅>0.2 占比的 Wilson score 95% CI(闭式,0/N 与 N/N "
                    "边界不塌缩)。规范对标 RobustBench / HELM 的 CI 报告。"
                ),
            },
            "variants_note": (
                "扰动变体生成于临时目录并在运行结束后自动清理;语料目录只读,"
                "未写入任何文件。变体文件名保留原 stem,可随时确定性重建。"
            ),
            "stub_immunity_note": (
                "stub 按文件名规则打分,对视觉扰动天然免疫——本报告用于验证框架;"
                "生产模型(glm/nudenet)会体现真实退化,建议接入后重跑。"
            ),
            "stats": stats,
            "details": details,
        }

        # 感知扰动预算曲线(纯新增层:不开启时 payload 不含 "curves" 键,
        # 既有指标字段与语义零变动;与金标门禁正交,见 _grid_curves_section)。
        # 曲线金标门禁的比对对象就是曲线 → 任一曲线金标开关自动附带网格评测。
        if grid or curve_gate or update_curve_golden:
            payload["curves"] = _grid_curves_section(clf, pairs, base_probs)

        # V10.4 金标门禁(纯新增层:不开启时 payload 不含 "gate" 键,
        # 既有指标字段与语义零变动)。
        if gate or update_golden:
            payload["gate"] = _gate_section(
                payload,
                pairs=pairs,
                corpus_dir=corpus_dir,
                classifier_name=classifier,
                golden_path=golden_path if golden_path is not None else DEFAULT_GOLDEN_PATH,
                update_golden=update_golden,
                missing_baseline=missing_baseline,
            )

        # V13 感知曲线独立金标门禁(纯新增层:不开启时 payload 不含
        # "curve_gate" 键;独立指纹/独立文件,与单点金标互不干扰)。
        # V13-2:curve_missing_baseline 缺省(None)继承单点策略值。
        if curve_gate or update_curve_golden:
            payload["curve_gate"] = _curve_gate_section(
                payload,
                pairs=pairs,
                corpus_dir=corpus_dir,
                classifier_name=classifier,
                golden_path=(
                    curve_golden_path
                    if curve_golden_path is not None
                    else DEFAULT_CURVE_GOLDEN_PATH
                ),
                update_golden=update_curve_golden,
                missing_baseline=(
                    missing_baseline
                    if curve_missing_baseline is None
                    else curve_missing_baseline
                ),
            )

        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "adversarial_report.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (out / "adversarial_report.md").write_text(render_markdown(payload), encoding="utf-8")
        logger.info(
            "对抗鲁棒性基准完成:classifier=%s total=%d", payload["classifier"], total
        )
    return payload


def _r4(value: float) -> str:
    """报告用的 4 位小数格式化。"""
    return f"{float(value):.4f}"


def _render_curves_lines(payload: dict[str, Any], curves: dict[str, Any]) -> list[str]:
    """把 ``payload["curves"]`` 渲染为中文 Markdown 曲线表(grid 模式专用)。

    每类扰动一张"参数 / ε(强度)/ 平均分 / 平均降幅 / 平均 SSIM"点列表,
    附鲁棒 AUC 与首个显著掉分强度(min_effective_attack);SSIM 口径、ε
    语义与"和金标门禁正交"的声明写进段首注脚。
    """
    lines: list[str] = []
    lines.append("## 感知扰动预算曲线(ε 网格 · grid 模式)")
    lines.append("")
    lines.append(f"- {curves['note']}")
    eps_desc = "、".join(
        f"{kind} = {desc}" for kind, desc in curves["eps_semantics"].items()
    )
    ssim_meta = curves["ssim"]
    lines.append(
        f"- SSIM 口径:{ssim_meta['window']}x{ssim_meta['window']} 非重叠窗口、"
        f"{ssim_meta['grayscale']} 灰度化、{ssim_meta['implementation']};"
        f"恒等图 SSIM = 1.0。ε(攻击强度)语义:{eps_desc}。"
    )
    lines.append(
        f"- {curves['auc_definition']};{curves['min_effective_attack_definition']}。"
    )
    lines.append("")
    for kind in PERTURB_TYPES:
        curve = curves["kinds"][kind]
        grid_desc = "/".join(str(param) for param in curve["grid"])
        lines.append(
            f"### {curve['label']}({kind}):{curve['param_name']} 网格 [{grid_desc}]"
        )
        lines.append("")
        lines.append("| 参数 | ε(强度) | 平均分 | 平均降幅 | 平均 SSIM |")
        lines.append("| ---: | ---: | ---: | ---: | ---: |")
        for point in curve["points"]:
            lines.append(
                "| {} | {} | {} | {} | {} |".format(
                    point["param"],
                    _r4(point["eps"]),
                    _r4(point["avg_score"]),
                    _r4(point["avg_drop"]),
                    _r4(point["avg_ssim"]),
                )
            )
        attack = curve["min_effective_attack"]
        if attack is None:
            lines.append("")
            lines.append(
                f"- 鲁棒 AUC = {_r4(curve['auc'])};网格内无 |平均降幅| > "
                f"{payload['drop_alert_line']} 的攻击强度(min_effective_attack = None)。"
            )
        else:
            lines.append("")
            lines.append(
                "- 鲁棒 AUC = {};首个显著掉分:ε = {}({} = {},平均 SSIM = {},"
                "平均降幅 {})——preprocess 反混淆参数至少应覆盖该强度。".format(
                    _r4(curve["auc"]),
                    _r4(attack["eps"]),
                    curve["param_name"],
                    attack["param"],
                    _r4(attack["ssim"]),
                    _r4(attack["avg_drop"]),
                )
            )
        if "shape_digest" in curve:  # V13-2 形状指纹(老 payload 无此键则跳过)
            lines.append(
                f"- 形状指纹 shape_digest = `{curve['shape_digest']}`"
                "(逐 ε 段斜率符号:0 平坦 / 1 上升 / 2 下降;纳入曲线金标第三指标)"
            )
        lines.append("")
    return lines


def render_markdown(payload: dict[str, Any]) -> str:
    """把基准 payload 渲染为中文 Markdown 报告(单文件,无外部依赖)。"""
    corpus = payload["corpus"]
    lines: list[str] = []
    lines.append("# NetSentinel 对抗鲁棒性基准报告(A55)")
    lines.append("")
    lines.append(f"- 生成时间:{payload['generated_at']}")
    lines.append(f"- 分类器:`{payload['classifier']}`")
    labels_desc = "、".join(f"{k} {v} 张" for k, v in corpus["labels"].items())
    lines.append(
        f"- 语料:`{corpus['dir']}`,共 {corpus['total']} 张({labels_desc};"
        "复用 A37 标注语料,labels.json 只读)"
    )
    lines.append(
        "- 扰动口径:降幅 = 原始 nsfw_prob − 扰动 nsfw_prob(带符号);"
        f"显著掉分线 = 降幅 > {payload['drop_alert_line']}"
    )
    lines.append(f"- {payload['variants_note']}")
    lines.append("")

    lines.append("## 一、扰动统计总表")
    lines.append("")
    # V9 统计推断列:老 payload(无 ci95)渲染为旧表,新 payload 自动加列。
    has_inference = bool(payload["stats"]) and "ci95" in payload["stats"][0]
    header = "| 扰动类型 | 变体文件 | 扰动参数 | 平均绝对降幅 | 最大降幅 | 降幅>0.2 占比 |"
    aligns = "| --- | --- | --- | ---: | ---: | ---: |"
    if has_inference:
        header += " 平均降幅 95% CI | 置换 p 值 | 占比 Wilson 95% CI |"
        aligns += " ---: | ---: | ---: |"
    lines.append(header)
    lines.append(aligns)
    for row in payload["stats"]:
        ratio = float(row["drop_gt_0.2_ratio"])
        cells = "| {} | `<stem>{}` | {} | {} | {} | {:.1%} |".format(
            row["label"],
            row["suffix"],
            row["param"],
            _r4(row["avg_abs_drop"]),
            _r4(row["max_drop"]),
            ratio,
        )
        if has_inference:
            ci_lo, ci_hi = row["ci95"]
            w_lo, w_hi = row["wilson_ci"]
            cells += " [{}, {}] | {} | [{}, {}] |".format(
                _r4(ci_lo),
                _r4(ci_hi),
                f"{float(row['p_value']):.4f}",
                _r4(w_lo),
                _r4(w_hi),
            )
        lines.append(cells)
    lines.append("")
    lines.append(
        "- 平均绝对降幅:|降幅| 的均值,衡量该扰动造成的总体分数漂移;"
        "最大降幅:带符号最大值(为负表示扰动后分数反而更高);"
        "占比:降幅 > 0.2 的图片比例,越高说明该扰动越是识别盲区。"
    )
    if has_inference:
        lines.append(
            "- 平均降幅 95% CI:带符号平均降幅(avg_drop)的配对 percentile "
            f"bootstrap 置信区间(B={BOOTSTRAP_B},固定种子,结果逐位可复现);"
            "区间含 0 表示在 5% 水平上无法排除“扰动无系统性影响”。"
        )
        lines.append(
            "- 置换 p 值:符号翻转配对置换检验(双侧,原假设:平均降幅 = 0;"
            f"n ≤ {PERMUTATION_EXACT_MAX_N} 精确枚举全部符号翻转,否则 "
            f"{PERMUTATION_MC_N} 次蒙特卡洛并作 +1 校正);p < 0.05 视为降幅"
            "显著非零。全零降幅时 p = 1(零效应不误报)。"
        )
        lines.append(
            "- 占比 Wilson 95% CI:降幅>0.2 占比的 Wilson score 置信区间"
            "(闭式公式);相对正态近似,它在 0/N 与 N/N 边界不塌缩为无信息区间。"
        )
    lines.append("")

    lines.append("## 二、逐图分数矩阵")
    lines.append("")
    header = ["文件", "标注", "原始"] + [PERTURB_META[k]["label"] for k in PERTURB_TYPES]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for row in payload["details"]:
        cells = [row["file"], row["label"], _r4(row["base_prob"])]
        cells += [_r4(row["variants"][kind]["prob"]) for kind in PERTURB_TYPES]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

    # 感知扰动预算曲线(grid 模式;老 payload / 单点 payload 无 "curves" 键,
    # 本段整体不渲染——既有报告结构零变动)。
    curves = payload.get("curves")
    if curves is not None:
        lines.extend(_render_curves_lines(payload, curves))

    lines.append("## 三、结论")
    lines.append("")
    lines.append(
        f"- **{payload['stub_immunity_note']}** stub 分数来自文件名关键词"
        "(nsfw_hi→0.97 / nsfw_mid→0.72 / 其他→0.02),而扰动变体保留原 stem,"
        "故 stub 场景下四类降幅恒为 0——上表数字仅用于验证"
        "\"扰动生成 → 评分 → 统计 → 报告\"链路,不代表真实抗规避能力。"
    )
    if payload["classifier"] != "stub":
        lines.append(
            f"- 本次评测分类器为 `{payload['classifier']}`:降幅统计反映真实"
            "视觉退化,可与 stub 基线对比定位盲区;某类扰动占比偏高时,建议在"
            "preprocess(反混淆)/ 复核流程中加针对性反制,并扩充对应训练样本。"
        )
    else:
        lines.append(
            "- 接入生产模型后请用同一命令重跑(`--classifier glm` 或 "
            "`--classifier nudenet`),与本 stub 基线对照,即可量化"
            "模糊/马赛克/遮挡/重压缩四类规避手段的真实掉分。"
        )
    lines.append("- 语料为程序生成的合成图片,不含真实违规内容;指标不可外推到真实业务分布。")
    lines.append(
        "- 按契约红线,任何模型(含 glm)的结果都只是特征:最终判定与举报须人工确认;"
        "对抗基准结论只用于改进检测配置,不得作为自动处置依据。"
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
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

    返回码(三态,对齐 ops/audit_verify 惯例;单点与曲线金标共用):

    - 0:成功(纯报告 / 金标重建 / 门禁通过 / 缺基线 warn 跳过);
    - 1:金标文件输入错误(损坏 / 坏 JSON / 结构非法 / 版本不兼容;
      单点与曲线金标文件同理);
    - 2:金标门禁回归违例(单点或曲线任一检出即 2),或 Pillow 缺失 /
      分类器不可用 / 语料问题等既有可预期错误(语义不变)。
    """
    _ensure_utf8_stdio()
    default_corpus = str(Path(__file__).resolve().parent / "corpus")
    default_out = str(Path(__file__).resolve().parent / "out")
    parser = argparse.ArgumentParser(
        prog="python benchmarks/adversarial.py",
        description=(
            "NetSentinel 对抗鲁棒性基准:对标注语料生成模糊/马赛克/遮挡/重压缩"
            "四类扰动变体,统计识别分数降幅(全程离线;glm 等在线分类器需显式配置)"
        ),
    )
    parser.add_argument(
        "--corpus", default=default_corpus, help=f"标注语料目录(默认 {default_corpus})"
    )
    parser.add_argument(
        "--out", default=default_out, help=f"报告输出目录(默认 {default_out})"
    )
    parser.add_argument(
        "--classifier",
        default="stub",
        help="分类器注册名(默认 stub,离线可用;glm 需配置 vlm_online 与密钥)",
    )
    grid_group = parser.add_argument_group("感知扰动预算曲线(ε 网格)")
    grid_group.add_argument(
        "--grid",
        action="store_true",
        help="开启感知扰动预算曲线模式:四类扰动按 ε 网格逐点评测,输出 "
        "score-vs-ε 点列 / score-vs-SSIM 曲线 / 鲁棒 AUC / 首个显著掉分强度"
        "(payload 新增 curves 段;与金标门禁正交,GATE_METRICS 不变)",
    )
    gate_group = parser.add_argument_group("金标回归门禁(V10.4)")
    gate_group.add_argument(
        "--gate",
        action="store_true",
        help="开启金标门禁:逐指标比对基线,|当前−基线|>容差 → 违例清单 + 退出码 2",
    )
    gate_group.add_argument(
        "--update-golden",
        action="store_true",
        help="重建金标基线(无确认直写,打印变更 diff 摘要;优先于 --gate)",
    )
    gate_group.add_argument(
        "--golden",
        default=str(DEFAULT_GOLDEN_PATH),
        help=f"金标文件路径(默认 {DEFAULT_GOLDEN_PATH})",
    )
    gate_group.add_argument(
        "--missing-baseline",
        choices=sorted(MISSING_BASELINE_POLICIES),
        default="warn",
        help="指纹未命中金标(分类器/语料/参数已变化)时:warn 跳过并提示(默认),"
        "或 fail 按回归违例处理(退出码 2;单点金标门禁用;曲线门禁缺省继承本值)",
    )
    curve_group = parser.add_argument_group("感知曲线金标门禁(V13 评测增强批)")
    curve_group.add_argument(
        "--curve-gate",
        action="store_true",
        help="开启感知曲线独立金标门禁:逐类比对鲁棒 AUC、首个显著掉分 ε 与"
        "曲线形状指纹 shape_digest,数值指标超 max(10%%·|基线|, 0.05)、"
        "None↔数值互变或形状指纹斜率翻转段数超容差 → 违例清单 + 退出码 2"
        "(与 --gate 并存互不干扰;未开 --grid 时自动附带 ε 网格评测)",
    )
    curve_group.add_argument(
        "--update-curve-golden",
        action="store_true",
        help="重建感知曲线独立金标基线(无确认直写,打印变更 diff 摘要;"
        "优先于 --curve-gate;不触碰单点金标文件)",
    )
    curve_group.add_argument(
        "--curve-golden",
        default=str(DEFAULT_CURVE_GOLDEN_PATH),
        help=f"曲线金标文件路径(默认 {DEFAULT_CURVE_GOLDEN_PATH})",
    )
    curve_group.add_argument(
        "--curve-missing-baseline",
        choices=sorted(MISSING_BASELINE_POLICIES),
        default=None,
        help="曲线金标缺基线处置:缺省继承 --missing-baseline 的值(既有行为);"
        "显式设置则曲线门禁独立于单点(单点 fail + 曲线 warn 可自由组合)",
    )
    args = parser.parse_args(argv)

    try:
        payload = run(
            args.corpus,
            args.out,
            classifier=args.classifier,
            gate=args.gate,
            golden_path=args.golden,
            update_golden=args.update_golden,
            missing_baseline=args.missing_baseline,
            grid=args.grid,
            curve_gate=args.curve_gate,
            curve_golden_path=args.curve_golden,
            update_curve_golden=args.update_curve_golden,
            curve_missing_baseline=args.curve_missing_baseline,
        )
    except AdversarialError as exc:
        # 金标输入错误(子类 exit_code=1)与既有可预期错误(2)分流。
        telemetry.inc("adversarial.errors")
        print(f"错误:{exc}", file=sys.stderr)
        return int(getattr(exc, "exit_code", 2))
    except BenchmarkError as exc:
        telemetry.inc("adversarial.errors")
        print(f"错误:{exc}", file=sys.stderr)
        return 2

    print(
        f"对抗鲁棒性基准完成:分类器={payload['classifier']} "
        f"语料={payload['corpus']['total']}张"
    )
    for row in payload["stats"]:
        summary = "  {:<6}({})平均绝对降幅 {} 最大降幅 {} 降幅>0.2 占比 {:.1%}".format(
            row["type"],
            row["label"],
            _r4(row["avg_abs_drop"]),
            _r4(row["max_drop"]),
            float(row["drop_gt_0.2_ratio"]),
        )
        if "ci95" in row:  # V9 统计推断摘要(老 payload 无此字段时回退旧行)
            ci_lo, ci_hi = row["ci95"]
            w_lo, w_hi = row["wilson_ci"]
            summary += (
                f" 平均降幅CI95[{_r4(ci_lo)},{_r4(ci_hi)}]"
                f" 置换p={float(row['p_value']):.4f}"
                f" 占比WilsonCI[{_r4(w_lo)},{_r4(w_hi)}]"
            )
        print(summary)
    # --- 感知扰动预算曲线摘要(grid 模式;单点模式不打印本段) ---
    curves = payload.get("curves")
    if curves is not None:
        print("感知扰动预算曲线(grid 模式):")
        for kind in PERTURB_TYPES:
            curve = curves["kinds"][kind]
            attack = curve["min_effective_attack"]
            if attack is None:
                tail = "网格内无显著掉分(|平均降幅| ≤ 0.2)"
            else:
                tail = (
                    f"首个显著掉分 ε={_r4(attack['eps'])}"
                    f"(SSIM={_r4(attack['ssim'])},平均降幅 {_r4(attack['avg_drop'])})"
                )
            print(
                "  {:<6}({})鲁棒AUC={} {}".format(
                    kind, curve["label"], _r4(curve["auc"]), tail
                )
            )
    out_dir = Path(args.out)
    print(f"报告已写出:{out_dir / 'adversarial_report.md'} 与 {out_dir / 'adversarial_report.json'}")

    # --- V10.4 金标门禁结果输出与退出码裁决 ---
    exit_code = 0
    gate_info = payload.get("gate")
    if gate_info is not None:
        fp_display = gate_info["fingerprint"][:12] + "…"
        if gate_info["mode"] == "update":
            print(
                f"金标已重建:{gate_info['golden_path']}(指纹 {fp_display},"
                f"记录 {gate_info['metrics_recorded']} 项指标基线,"
                "容差=max(10%·|基线|, CI 半宽))"
            )
            for line in gate_info["update_diff"]:
                print(f"  {line}")
        elif gate_info["status"] == "pass":
            print(
                f"金标门禁通过:指纹 {fp_display},"
                f"{gate_info['metrics_total']} 项指标全部在容差内"
                "(容差=max(10%·|基线|, CI 半宽))—— 退出码 0"
            )
        elif gate_info["status"] == "no_baseline":
            for line in gate_info["warnings"]:
                print(f"警告:{line}", file=sys.stderr)
            print("金标门禁:缺基线,已按警告跳过 —— 退出码 0")
        else:  # violations(含缺基线 fail 与指标违例)
            for violation in gate_info["violations"]:
                print(f"金标违例:{violation['message']}", file=sys.stderr)
            print(
                f"金标门禁:检出 {len(gate_info['violations'])} 项回归违例 —— 退出码 2",
                file=sys.stderr,
            )
            exit_code = 2

    # --- V13 感知曲线独立金标门禁结果输出(与单点门禁各自独立裁决) ---
    curve_info = payload.get("curve_gate")
    if curve_info is not None:
        fp_display = curve_info["fingerprint"][:12] + "…"
        if curve_info["mode"] == "update":
            print(
                f"曲线金标已重建:{curve_info['golden_path']}(指纹 {fp_display},"
                f"记录 {curve_info['metrics_recorded']} 项曲线指标基线,"
                "容差=max(10%·|基线|, 0.05);min_effective None↔数值互变即违例;"
                "shape_digest 离散精确匹配"
                f"(默认允许 {CURVE_SHAPE_FLIP_TOL} 段斜率翻转))"
            )
            for line in curve_info["update_diff"]:
                print(f"  {line}")
        elif curve_info["status"] == "pass":
            print(
                f"曲线金标门禁通过:指纹 {fp_display},"
                f"{curve_info['metrics_total']} 项曲线指标全部在容差内"
                "(容差=max(10%·|基线|, 0.05);None↔数值互变即违例;"
                "shape_digest 斜率翻转段数容差)—— 退出码 0"
            )
        elif curve_info["status"] == "no_baseline":
            for line in curve_info["warnings"]:
                print(f"警告:{line}", file=sys.stderr)
            print("曲线金标门禁:缺基线,已按警告跳过 —— 退出码 0")
        else:  # violations(含缺基线 fail 与曲线指标违例)
            for violation in curve_info["violations"]:
                print(f"曲线金标违例:{violation['message']}", file=sys.stderr)
            print(
                f"曲线金标门禁:检出 {len(curve_info['violations'])} 项曲线回归违例"
                " —— 退出码 2",
                file=sys.stderr,
            )
            exit_code = 2
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
