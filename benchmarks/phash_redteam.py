# -*- coding: utf-8 -*-
"""NetSentinel pHash 指纹对抗鲁棒性红队基准(V12 · 防御侧回应)。

**动机与对标**:2026 年黑盒哈希攻击研究(LLM 引导的 pHash/PDQ/PhotoDNA
规避)表明,违规站重传图片时常叠加轻量几何 / 光度 / 重编码扰动以规避
感知指纹。本基准站在**防御侧**回应:用 9 个标准攻击族(缩放 / 中心裁剪
/ 旋转 / 翻转 / 亮度 / 对比度 / JPEG 重压缩 / 高斯噪声 / 水印式半透明
覆盖)的确定性参数网格,量化自研指纹(``intel.phash.phash`` 64bit 与
``vision.phash2.phash256`` 256bit)与两套检索索引(``LSHIndex`` 单表
分带 / ``MultiTableLSH`` 多表 multi-probe)在扰动下的**召回损失**,
为 V12 多表 LSH 与金字塔哈希升级提供可复现数据(世界前沿调研 TOP7
第 4 项)。

**评测管线**(全程离线、零网络、零第三方依赖——Pillow 为可选依赖,
缺席时以退出码 2 结束,不产出无效报告):

1. **合成基线图集**:确定性生成器(种子派生的矩形 / 渐变 / 纹理 /
   条纹 / 同心环 / 照片式构图,纯 Pillow 像素运算,同种子字节级一致)
   ——不依赖也不写入 ``benchmarks/corpus``(语料目录只读);
2. **指纹入库**:每张基线图计算 phash64 + phash256,phash64 同时灌入
   ``LSHIndex(bands=4)`` 与 ``MultiTableLSH(K=8, B=16, p=2)`` 两索引;
3. **攻击变体**:每图 × 每攻击族 × 每参数(共 9 族 32 档)生成扰动
   变体(JPEG 族即"以该质量落盘再解码",其余为 PIL 确定性变换;噪声
   用 ``random.Random(种子)`` 逐像素高斯点运算,种子确定 → 字节确定);
4. **召回评测**:对每个变体查询"其源图是否在距离 d 内被找回"——
   暴力全扫真值(直接汉明距离 ≤ d)vs LSH 索引召回,d∈{8,12,16}
   (64bit)/ {32,48,64}(256bit,位宽 4 倍同比例放大);
5. **产出**:攻击族×指纹×d 召回矩阵、最脆弱攻击 Top3、
   min_effective_attack(首个召回跌破 0.9 的攻击强度)、单表 vs 多表
   索引对比数据点;报告 JSON + markdown 双输出(结构对齐
   ``benchmarks/adversarial.py`` 的 A55 惯例)。

**统计层**(对齐 adversarial 的 V9 统计推断规范):召回是二项占比,
每个门禁指标附 Wilson score 95% CI(复用 ``adversarial.wilson_ci95``,
兄弟模块只读引用);全部随机源固定种子,同一数据任何次运行输出逐位
一致。

**金标回归门禁**(``--gate``,对齐 adversarial V10.4 金标惯例):

- **指纹键** = sha256(指纹算法集 + 攻击族版本与参数网格 + 语料指纹
  (合成参数 + 逐文件 sha256)+ 距离网格 + 统计版本号):任一变化即新键,
  旧金标自然缺基线,绝不用错误基线比对;
- **容差** = ``max(--tol-ratio·|基线值|, Wilson 95% CI 半宽)``,比例
  可配(默认 10%);
- ``--update-golden`` 重建基线(打印 diff 摘要);``--gate`` 逐指标比对,
  |当前−基线| > 容差 → 中文违例清单 + 退出码 2;
- **退出码三态**(对齐 ops/audit_verify 惯例):0 正常(含缺基线 warn
  跳过)/ 1 金标文件输入错误(损坏 / 坏 JSON / 结构非法)/ 2 回归违例,
  或 Pillow 缺失等可预期错误。

安全红线:对抗扰动**仅作用于本地合成图**(服务防御评测,不触碰任何
真实证据);全程离线零网络;语料与变体全部落在临时目录,运行结束自动
清理;报告只写调用方指定的输出目录。召回数字只用于改进指纹与索引
配置,不构成任何自动处置依据。

命令行::

    python benchmarks/phash_redteam.py --out <目录>              # 纯报告
    python benchmarks/phash_redteam.py --out <目录> --count 8 --seed 20261033
    python benchmarks/phash_redteam.py --out <目录> --update-golden  # 重建金标
    python benchmarks/phash_redteam.py --out <目录> --gate           # 门禁比对
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import json
import logging
import math
import random
import sys
import tempfile
import types
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 直接以脚本运行(python benchmarks/phash_redteam.py)时,保证项目根在
# sys.path 上,使 netsentinel / benchmarks 包可导入;经包导入(tests)时为空操作。
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from netsentinel import telemetry  # noqa: E402
from netsentinel.contracts import now_iso  # noqa: E402
from netsentinel.intel.phash import hamming, phash  # noqa: E402
from netsentinel.intel.phash_lsh import (  # noqa: E402
    DEFAULT_BANDS,
    DEFAULT_KEY_BITS,
    DEFAULT_PROBE_DEPTH,
    DEFAULT_TABLES,
    LSHIndex,
    MultiTableLSH,
)
from netsentinel.vision.phash2 import hamming_hex, phash256  # noqa: E402
from benchmarks.adversarial import wilson_ci95  # noqa: E402  兄弟只读(统计层对齐)

__all__ = [
    "ATTACK_FAMILIES",
    "ATTACK_META",
    "ATTACK_VERSION",
    "CORPUS_GENERATOR_VERSION",
    "CORPUS_KINDS",
    "CORPUS_SIZES",
    "DEFAULT_CORPUS_COUNT",
    "DEFAULT_GOLDEN_PATH",
    "DEFAULT_SEED",
    "DISTANCES256",
    "DISTANCES64",
    "GATE_METRICS",
    "GOLDEN_SCHEMA",
    "GOLDEN_STATS_VERSION",
    "IDENTITY_FAMILY",
    "MISSING_BASELINE_POLICIES",
    "RECALL_FLOOR",
    "RedteamError",
    "RedteamGoldenError",
    "TOL_VALUE_RATIO",
    "apply_attack",
    "build_golden_entry",
    "compute_fingerprint",
    "evaluate_gate",
    "generate_variants",
    "load_golden",
    "make_synthetic_corpus",
    "main",
    "metric_tolerance",
    "render_markdown",
    "run",
    "save_golden",
    "variant_seed",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 攻击族注册表(顺序即报告行序;参数网格按"强度递增"排列,
# min_effective_attack 沿该序找首个召回跌破 0.9 的档位)
# ---------------------------------------------------------------------------

#: 攻击族版本号:任何攻击实现 / 参数网格语义变化时递增 → 金标指纹整体
#: 轮换(缺基线 → 提示重建),绝不用旧口径比对新数据。
ATTACK_VERSION = "rt1"

#: 合成语料生成器版本(构图 / 尺寸 / 命名口径变化时递增,同上轮换指纹)。
#: syn2:初版 gradient/texture/stripes/rings 构图在 32x32 降采样后 DCT 交流
#: 系数趋零(中位阈值两侧随机翻转,恒等外全部攻击距离虚高),换成六类
#: "降采样后仍保有强低频结构"的构图(经验筛选,逐类以轻攻击距离 ≈ 0 验证)。
CORPUS_GENERATOR_VERSION = "syn2"

#: 恒等族(无攻击:原图字节重存),用于"无攻击召回恒 = 1.0"的数学自检。
IDENTITY_FAMILY = "none"

#: 9 个攻击族(报告行序)。
ATTACK_FAMILIES: tuple[str, ...] = (
    "resize",
    "crop",
    "rotate",
    "flip",
    "brightness",
    "contrast",
    "jpeg",
    "noise",
    "watermark",
)

#: 各族中文名 / 参数单位 / 参数网格(网格按强度递增排序)。
ATTACK_META: dict[str, dict[str, Any]] = {
    IDENTITY_FAMILY: {
        "label": "无攻击(恒等重存)",
        "unit": "—",
        "params": ("identity",),
        "desc": "原图字节原样复制重存(控制组:任何 d 下召回必须 = 1.0)",
    },
    "resize": {
        "label": "缩放",
        "unit": "倍",
        "params": ("1.25x", "0.75x", "1.5x", "0.5x", "2.0x"),
        "desc": "LANCZOS 重采样到指定倍率(指纹内部统一再缩到 32x32)",
    },
    "crop": {
        "label": "中心裁剪",
        "unit": "面积占比",
        "params": ("0.1", "0.2", "0.3"),
        "desc": "中心裁剪指定面积占比后回填放大到原尺寸(丢边缘信息)",
    },
    "rotate": {
        "label": "旋转",
        "unit": "度",
        "params": ("3", "-3", "8", "-8", "15", "-15"),
        "desc": "绕中心旋转,白边(255)填充",
    },
    "flip": {
        "label": "镜像翻转",
        "unit": "方向",
        "params": ("h", "v"),
        "desc": "水平 / 垂直镜像(几何结构反转,对 DCT 指纹属强攻击)",
    },
    "brightness": {
        "label": "亮度",
        "unit": "系数",
        "params": ("1.2", "0.8", "1.4", "0.6"),
        "desc": "亮度线性缩放 ±20% / ±40%(LUT 点运算,越界截断)",
    },
    "contrast": {
        "label": "对比度",
        "unit": "系数",
        "params": ("1.2", "0.8", "1.4", "0.6"),
        "desc": "绕 128 灰心的对比度缩放 ±20% / ±40%(LUT 点运算)",
    },
    "jpeg": {
        "label": "JPEG 重压缩",
        "unit": "质量",
        "params": ("60", "30", "10"),
        "desc": "以指定 quality 落盘 JPEG 再解码(块效应即攻击本身)",
    },
    "noise": {
        "label": "高斯噪声",
        "unit": "σ",
        "params": ("8", "16", "32"),
        "desc": "逐像素加性高斯噪声(random.Random 固定种子点运算)",
    },
    "watermark": {
        "label": "水印覆盖",
        "unit": "α",
        "params": ("0.35", "0.6"),
        "desc": "右下约 20% 面积半透明白色覆盖(45%x45% 区域 α 混合)",
    },
}

#: 全部参与评测的族(恒等族在最前,控制组先行)。
ALL_FAMILIES: tuple[str, ...] = (IDENTITY_FAMILY, *ATTACK_FAMILIES)

# --- 评测口径常量 -----------------------------------------------------------

#: 64bit 指纹的距离网格(对齐 PhashRegistry.find_similar 的 d=8 日常工作点,
#: 与 V12 多表 LSH 验收的 d=12/16 高距区)。
DISTANCES64: tuple[int, ...] = (8, 12, 16)

#: 256bit 指纹的距离网格(位宽 4 倍,同比例放大:d/64 = D/256)。
DISTANCES256: tuple[int, ...] = (32, 48, 64)

#: min_effective_attack 的召回地板:族内参数沿强度序首个召回 < 0.9 的档位。
RECALL_FLOOR = 0.9

#: 合成语料缺省张数与种子(确定性:同参数字节级一致)。
DEFAULT_CORPUS_COUNT = 8
DEFAULT_SEED = 20261033

#: 语料构图轮换表(索引取模循环)。六类构型刻意覆盖不同频率结构:
#: 照片式 / 随机形状(平滑底 + 几何边缘)、随机矩形 / 块状马赛克(硬边
#: 块结构)、16x16 / 8x8 格点噪声上采样(平滑随机"云纹")——均满足
#: "LANCZOS 降采样到 32x32 后低频交流系数仍远大于零"(否则中位阈值
#: 两侧随机翻转,指纹退化,轻攻击即产生虚高距离)。
CORPUS_KINDS: tuple[str, ...] = (
    "photo",    # 照片式:平滑双向渐变 + 固定圆 / 矩形几何形状
    "rects",    # 种子随机矩形拼贴
    "shapes",   # 平滑双向正弦底 + 种子随机椭圆 / 矩形(第二照片式构图)
    "cloud16", # 16x16 随机格点 LANCZOS 上采样(平滑随机云纹)
    "mosaic",   # 8x8 块整数哈希马赛克(硬边块纹理)
    "cloud8",  # 8x8 随机格点 BICUBIC 上采样(更粗尺度的云纹)
)
CORPUS_SIZES: tuple[tuple[int, int], ...] = ((256, 256), (320, 240))

# --- 金标门禁常量(对齐 adversarial V10.4 惯例) -----------------------------

#: 金标统计口径版本号(指标定义 / 容差规则 / 攻击参数任一语义变化时递增)。
GOLDEN_STATS_VERSION = "v12-rt-1"

#: 金标文件结构版本(不兼容演进时递增;读取端见 load_golden 预检)。
GOLDEN_SCHEMA = "netsentinel-phash-redteam-golden/1"

#: 纳入门禁的指标(每攻击族逐一比对):64bit 两个距离档 + 256bit 主档
#: + 单表/多表索引在 d8(工作点)与 d16(高距区)的召回。
GATE_METRICS: tuple[str, ...] = (
    "recall_phash64_d8",
    "recall_phash64_d16",
    "recall_phash256_d32",
    "recall_lsh_single_d8",
    "recall_lsh_multi_d8",
    "recall_lsh_multi_d16",
)

#: 容差中"基线相对比例"分支的缺省比例(CLI --tol-ratio 可覆盖)。
TOL_VALUE_RATIO = 0.10

#: 指纹未命中金标时的处置策略(对齐 adversarial)。
MISSING_BASELINE_POLICIES: frozenset[str] = frozenset({"warn", "fail"})

#: 金标文件默认路径(--golden 可覆盖)。
DEFAULT_GOLDEN_PATH = Path(__file__).resolve().parent / "out" / "phash_redteam_golden.json"

#: 浮点比较容差(违例判定 "≥" 防浮点噪声)。
_STAT_EPS = 1e-12

#: 容差规则中文口径(写入金标 _tolerance_rule 与报告)。
_TOL_RULE_TEXT = (
    "tol = max(--tol-ratio·|基线值|, Wilson 95% CI 半宽):CI 由 V9 统计层"
    "wilson_ci95(闭式,固定种子确定性)产出;默认比例 10%"
)


class RedteamError(RuntimeError):
    """红队基准流程中可预期的错误(中文消息;CLI 捕获后以退出码 2 结束)。"""


class RedteamGoldenError(RedteamError):
    """金标文件输入错误(损坏 / 坏 JSON / 结构非法 / 版本不兼容)。

    对应 CLI 退出码 1(对齐 ops/audit_verify 的"输入错误"惯例)。
    """

    exit_code = 1


# ---------------------------------------------------------------------------
# Pillow 惰性加载(与 adversarial / intel.phash 同款策略)
# ---------------------------------------------------------------------------


def _load_pil():
    """惰性导入 ``PIL.Image`` / ``PIL.ImageDraw``;缺失 / 半初始化返回 None。

    先导入父包 ``PIL`` 再导子模块:测试以 ``sys.modules["PIL"] = None``
    屏蔽 Pillow 时,若只导子模块会命中缓存而漏检,先导父包才能可靠感知
    "不可用"。
    """
    try:
        importlib.import_module("PIL")
        image = importlib.import_module("PIL.Image")
        draw = importlib.import_module("PIL.ImageDraw")
    except Exception:  # None 注入 / 未安装 / 损坏的 PIL 均按"未安装"处理
        return None
    return types.SimpleNamespace(Image=image, ImageDraw=draw)


def _sha256_of(path: Path) -> str:
    """流式计算文件 sha256(语料指纹 / 变体对账用)。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clamp(value: int) -> int:
    """灰度值截断到 [0, 255](LUT / 噪声点运算共用的边界处理)。"""
    return 0 if value < 0 else (255 if value > 255 else value)


def _resampling(pil_image):
    """取当前 Pillow 版本的 LANCZOS 重采样枚举(兼容 Resampling 拆分前后)。"""
    resampling = getattr(pil_image, "Resampling", pil_image)
    return resampling.LANCZOS


# ---------------------------------------------------------------------------
# 合成基线图集(确定性生成器:种子矩形 / 渐变 / 纹理图)
# ---------------------------------------------------------------------------


def _synthetic_image(pil, index: int, width: int, height: int, seed: int):
    """按索引构造确定性灰度("L")合成图(零随机源的构图用解析公式,
    随机构图用 ``random.Random(seed, index)`` 派生流)。

    构图覆盖六类频率结构(见 :data:`CORPUS_KINDS`)——保证攻击族的召回
    结论不依赖单一纹理结构;全部构图经验证"降采样到 32x32 后低频交流
    系数远离零",轻攻击(缩放 / 重编码 / 亮度)指纹距离 ≈ 0。
    """
    image = pil.Image
    kind = CORPUS_KINDS[index % len(CORPUS_KINDS)]

    if kind == "photo":
        # 照片式:平滑双向正弦渐变打底(相位随图序轮换,保证同名构图在
        # 大语料中不重复 → 指纹互异),再叠几何形状。
        phase_x = 0.7 + 0.61 * (index // len(CORPUS_KINDS))
        phase_y = 1.9 + 0.47 * (index // len(CORPUS_KINDS))
        base = image.new("L", (width, height))
        flat = [
            _clamp(int(round(
                96 + 48 * _sin01(2.0, x / width, phase_x)
                + 32 * _cos01(2.0, y / height, phase_y)
            )))
            for y in range(height)
            for x in range(width)
        ]
        base.putdata(flat)
        draw = pil.ImageDraw.Draw(base)
        shift = 0.03 * (index % 3)
        cx, cy = int(width * (0.35 + shift)), int(height * 0.45)
        r = int(0.24 * min(width, height))
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=210)
        draw.rectangle(
            (int(width * 0.55), int(height * (0.1 + shift)), int(width * 0.9), int(height * 0.45)),
            fill=40,
        )
        cx, cy = int(width * 0.72), int(height * (0.75 - shift))
        r = int(0.22 * min(width, height))
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=150)
        return base

    if kind == "rects":
        # 种子随机矩形拼贴:每图独立随机流(种子 × 图序派生),同种子必一致。
        rng = random.Random(seed * 1_000_003 + index)
        base = image.new("L", (width, height), 128)
        draw = pil.ImageDraw.Draw(base)
        for _ in range(12):
            rw = int(width * (0.08 + 0.22 * rng.random()))
            rh = int(height * (0.08 + 0.22 * rng.random()))
            x0 = rng.randrange(max(1, width - rw))
            y0 = rng.randrange(max(1, height - rh))
            draw.rectangle((x0, y0, x0 + rw, y0 + rh), fill=rng.randrange(30, 226))
        return base

    if kind == "shapes":
        # 第二照片式构图:双向正弦底 + 5 个种子随机椭圆 / 矩形。
        rng = random.Random(seed * 7717 + index)
        base = image.new("L", (width, height))
        base.putdata([
            _clamp(int(round(
                110 + 50 * _sin01(1.5, x / width, -0.4)
                + 35 * _sin01(2.5, y / height, 2.2)
            )))
            for y in range(height)
            for x in range(width)
        ])
        draw = pil.ImageDraw.Draw(base)
        for _ in range(5):
            r = int(min(width, height) * (0.08 + 0.14 * rng.random()))
            cx = rng.randrange(r, max(r + 1, width - r))
            cy = rng.randrange(r, max(r + 1, height - r))
            if rng.getrandbits(1):
                draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=rng.randrange(30, 226))
            else:
                rw = int(width * (0.1 + 0.2 * rng.random()))
                rh = int(height * (0.1 + 0.2 * rng.random()))
                x0 = rng.randrange(max(1, width - rw))
                y0 = rng.randrange(max(1, height - rh))
                draw.rectangle((x0, y0, x0 + rw, y0 + rh), fill=rng.randrange(30, 226))
        return base

    if kind == "cloud16":
        # 平滑随机云纹:16x16 随机格点 LANCZOS 上采样(类 Perlin 低通噪声,
        # 频率结构接近自然图像的平滑随机成分)。
        rng = random.Random(seed * 1_000_003 + index)
        lattice = image.new("L", (16, 16))
        lattice.putdata([rng.randrange(0, 256) for _ in range(16 * 16)])
        return lattice.resize((width, height), _resampling(image))

    if kind == "mosaic":
        # 8x8 块整数哈希马赛克:无随机源、逐块确定性(硬边块纹理)。
        block = max(1, width // 8)
        salt = index * 83492791 + seed * 69069
        base = image.new("L", (width, height))
        base.putdata([
            ((x // block) * 73856093 ^ (y // block) * 19349663 ^ salt) & 0xFF
            for y in range(height)
            for x in range(width)
        ])
        return base

    # kind == "cloud8":8x8 随机格点 BICUBIC 上采样(更粗尺度云纹)。
    rng = random.Random(seed * 2_000_003 + index)
    lattice = image.new("L", (8, 8))
    lattice.putdata([rng.randrange(0, 256) for _ in range(8 * 8)])
    resampling = getattr(image, "Resampling", image)
    return lattice.resize((width, height), resampling.BICUBIC)


def _sin01(cycles: float, t: float, phase: float) -> float:
    """合成构图用正弦分量(频率 × 归一化坐标 + 相位)。"""
    return math.sin(2 * math.pi * cycles * t + phase)


def _cos01(cycles: float, t: float, phase: float) -> float:
    """合成构图用余弦分量(频率 × 归一化坐标 + 相位)。"""
    return math.cos(2 * math.pi * cycles * t + phase)


def make_synthetic_corpus(
    out_dir: str | Path, count: int = DEFAULT_CORPUS_COUNT, seed: int = DEFAULT_SEED
) -> list[dict[str, Any]]:
    """确定性生成合成基线图集(PNG 落盘),返回清单(按图序)。

    每张:构图按 ``CORPUS_KINDS`` 轮换、尺寸按 ``CORPUS_SIZES`` 交替、
    文件名 ``rt_img_{index:03d}.png``;PNG 以 ``compress_level=1`` 落盘
    (高熵纹理图快一个数量级,同参数字节级可复现)。同 ``(count, seed)``
    任何次运行产物逐字节一致(确定性测试据此断言)。
    count 非法(非正整数 / 超过 128)抛中文 ValueError。
    """
    if not isinstance(count, int) or isinstance(count, bool) or count < 1 or count > 128:
        raise ValueError(f"count 无效:须为 1~128 的整数,得到 {count!r}")
    pil = _load_pil()
    if pil is None:
        raise RedteamError(
            "未安装 Pillow,无法生成合成基线图集(pip install Pillow 后重试;"
            "本基准以退出码 2 结束,不产出无效报告)"
        )
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    for index in range(count):
        width, height = CORPUS_SIZES[index % len(CORPUS_SIZES)]
        kind = CORPUS_KINDS[index % len(CORPUS_KINDS)]
        image = _synthetic_image(pil, index, width, height, seed)
        name = f"rt_img_{index:03d}.png"
        path = out / name
        image.save(path, format="PNG", compress_level=1)
        manifest.append(
            {
                "index": index,
                "name": name,
                "path": str(path),
                "kind": kind,
                "width": width,
                "height": height,
            }
        )
    logger.debug("合成基线图集生成完毕:%s(%d 张,seed=%d)", out, count, seed)
    return manifest


def corpus_digest(manifest: list[dict[str, Any]]) -> str:
    """语料指纹:按文件名排序的 ``名字:sha256`` 串接后取 sha256。"""
    pairs = sorted(
        f"{item['name']}:{_sha256_of(Path(item['path']))}" for item in manifest
    )
    return hashlib.sha256("|".join(pairs).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 攻击族实现(PIL 确定性变换;JPEG 族为"落盘再解码"的字节级攻击)
# ---------------------------------------------------------------------------


def variant_seed(seed: int, image_index: int, family: str, param: str) -> int:
    """派生单个变体的确定性种子(与调用顺序无关,只依赖四个参数)。

    同 ``(seed, image_index, family, param)`` 恒得同一种子;噪声族据此
    逐像素采样,故同种子 → 同输出字节。非法族 / 参数抛中文 ValueError。
    """
    try:
        family_rank = ALL_FAMILIES.index(family)
        param_rank = ATTACK_META[family]["params"].index(param)
    except (ValueError, KeyError) as exc:
        raise ValueError(
            f"非法攻击族/参数:{family!r}/{param!r}"
            f"(允许族:{list(ALL_FAMILIES)})"
        ) from exc
    return (
        seed * 1_000_003
        + image_index * 7919
        + family_rank * 104_729
        + param_rank * 15_485_863
    ) % (2**31)


def _atk_resize(pil, im, param: str, seed: int):
    factor = float(param[:-1])
    width, height = im.size
    target = (max(1, int(round(width * factor))), max(1, int(round(height * factor))))
    return im.resize(target, _resampling(pil.Image))


def _atk_crop(pil, im, param: str, seed: int):
    frac = float(param)
    width, height = im.size
    side = max(1, int(round((width * height * frac) ** 0.5)))
    side = min(side, width, height)
    x0, y0 = (width - side) // 2, (height - side) // 2
    return im.crop((x0, y0, x0 + side, y0 + side)).resize(
        (width, height), _resampling(pil.Image)
    )


def _atk_rotate(pil, im, param: str, seed: int):
    resampling = getattr(pil.Image, "Resampling", pil.Image)
    return im.rotate(
        float(param), resample=resampling.BICUBIC, expand=False, fillcolor=255
    )


def _atk_flip(pil, im, param: str, seed: int):
    image = pil.Image
    transpose = getattr(image, "Transpose", image)
    if param == "h":
        return im.transpose(transpose.FLIP_LEFT_RIGHT)
    return im.transpose(transpose.FLIP_TOP_BOTTOM)


def _atk_brightness(pil, im, param: str, seed: int):
    factor = float(param)
    return im.point([_clamp(int(round(v * factor))) for v in range(256)])


def _atk_contrast(pil, im, param: str, seed: int):
    factor = float(param)
    return im.point([_clamp(int(round((v - 128) * factor + 128))) for v in range(256)])


def _atk_jpeg(pil, im, param: str, seed: int):
    # JPEG 族的攻击即"以该质量编码再解码"的往返本身(块效应 / 振铃),
    # 内存 BytesIO 往返等价于落盘 .jpg 再读,且不产生中间文件。
    quality = int(float(param))
    buffer = io.BytesIO()
    im.save(buffer, format="JPEG", quality=quality)
    buffer.seek(0)
    with pil.Image.open(buffer) as reopened:
        reopened.load()
        return reopened.copy()


def _atk_noise(pil, im, param: str, seed: int):
    sigma = float(param)
    rng = random.Random(seed)
    width, height = im.size
    noisy = pil.Image.new("L", (width, height))
    # Pillow 12 起 getdata 弃用(14 移除),优先用 get_flattened_data。
    getter = getattr(im, "get_flattened_data", None) or im.getdata
    noisy.putdata([
        _clamp(int(round(v + rng.gauss(0.0, sigma))))
        for v in getter()
    ])
    return noisy


def _atk_watermark(pil, im, param: str, seed: int):
    alpha = float(param)
    width, height = im.size
    rw = max(1, int(round(width * 0.45)))   # 0.45 x 0.45 ≈ 20% 面积,右下角
    rh = max(1, int(round(height * 0.45)))
    x0, y0 = width - rw, height - rh
    lut = [_clamp(int(round(v * (1.0 - alpha) + 255.0 * alpha))) for v in range(256)]
    marked = im.copy()
    marked.paste(im.crop((x0, y0, width, height)).point(lut), (x0, y0))
    return marked


_ATTACK_IMPL = {
    "resize": _atk_resize,
    "crop": _atk_crop,
    "rotate": _atk_rotate,
    "flip": _atk_flip,
    "brightness": _atk_brightness,
    "contrast": _atk_contrast,
    "jpeg": _atk_jpeg,
    "noise": _atk_noise,
    "watermark": _atk_watermark,
}


def apply_attack(im, family: str, param: str, seed: int = 0):
    """对 PIL 图像施加指定攻击变换,返回新图(不修改入参)。

    - ``family`` 须为 :data:`ATTACK_FAMILIES` 之一(恒等族不经此函数,
      由 :func:`generate_variants` 直接字节复制);``param`` 须在该族
      网格内;非法输入抛中文 ValueError;
    - 全部变换确定性:同 ``(im, family, param, seed)`` 必得同输出(噪声
      族由 ``random.Random(seed)`` 逐像素采样保证);
    - JPEG 族返回"以该质量编码再解码"后的图像(攻击即重压缩往返)。
    """
    impl = _ATTACK_IMPL.get(family)
    if impl is None:
        raise ValueError(
            f"非法攻击族:{family!r}(允许:{list(ATTACK_FAMILIES)})"
        )
    if family not in ATTACK_META or param not in ATTACK_META[family]["params"]:
        raise ValueError(
            f"非法攻击参数:{family!r}/{param!r}"
            f"(允许:{list(ATTACK_META[family]['params']) if family in ATTACK_META else '族非法'})"
        )
    pil = _load_pil()
    if pil is None:
        raise RedteamError(
            "未安装 Pillow,无法施加攻击变换(pip install Pillow 后重试)"
        )
    try:
        return impl(pil, im, param, seed)
    except (TypeError, ValueError, OSError) as exc:
        raise ValueError(f"攻击变换失败:{family}/{param}({exc})") from exc


def generate_variants(
    src_path: str | Path,
    out_dir: str | Path,
    *,
    seed: int = DEFAULT_SEED,
    image_index: int = 0,
    families: tuple[str, ...] | None = None,
    include_identity: bool = True,
) -> list[dict[str, Any]]:
    """对单张基线图生成全部攻击变体(落盘 PNG),返回变体记录列表。

    - 恒等变体(含 ``include_identity=True`` 时):原图**字节原样复制**
      (文件名 ``<stem>__none_identity.png``),保证"无攻击距离 = 0"的
      控制组语义;
    - 每攻击族 × 每参数:``apply_attack`` 变换后以 PNG(compress_level=1)
      落盘,文件名 ``<stem>__<族>_<参数>.png``;变体种子由
      :func:`variant_seed` 派生(同种子 → 同输出字节);
    - ``families`` 可限定参与的攻击族(测试用;缺省全部 9 族);
    - 变体只写入 ``out_dir``(调用方给临时目录),源图只读不被触碰。
    """
    pil = _load_pil()
    if pil is None:
        raise RedteamError(
            "未安装 Pillow,无法生成攻击变体(pip install Pillow 后重试;"
            "本基准以退出码 2 结束,不产出无效报告)"
        )
    src = Path(src_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = src.stem

    try:
        with pil.Image.open(src) as opened:
            opened.load()
            im = opened.convert("L")
    except (OSError, ValueError) as exc:
        raise RedteamError(f"无法解码基线图:{src}({exc})") from exc

    chosen = tuple(ATTACK_FAMILIES) if families is None else tuple(families)
    records: list[dict[str, Any]] = []
    if include_identity:
        identity_path = out / f"{stem}__{IDENTITY_FAMILY}_identity.png"
        identity_path.write_bytes(src.read_bytes())
        records.append(
            {
                "family": IDENTITY_FAMILY,
                "param": "identity",
                "path": str(identity_path),
                "seed": variant_seed(seed, image_index, IDENTITY_FAMILY, "identity"),
            }
        )
    for family in chosen:
        for param in ATTACK_META[family]["params"]:
            vseed = variant_seed(seed, image_index, family, param)
            variant = apply_attack(im, family, param, seed=vseed)
            path = out / f"{stem}__{family}_{param}.png"
            variant.save(path, format="PNG", compress_level=1)
            records.append(
                {"family": family, "param": param, "path": str(path), "seed": vseed}
            )
    return records


# ---------------------------------------------------------------------------
# 金标门禁:指纹 / 容差 / 金标读写 / 逐指标比对(对齐 adversarial V10.4)
# ---------------------------------------------------------------------------


def _fingerprint_digest(count: int, seed: int, digest: str) -> str:
    """金标指纹键:指纹算法 + 索引规格 + 攻击族版本与网格 + 语料指纹 +
    距离网格 + 统计版本,规范化 JSON 后取 sha256(任何机器逐位一致)。"""
    basis = {
        "fingerprints": ["phash64:intel.phash", "phash256:vision.phash2"],
        "indexes": {
            "single": f"LSHIndex(bands={DEFAULT_BANDS})",
            "multi": (
                f"MultiTableLSH(K={DEFAULT_TABLES},B={DEFAULT_KEY_BITS},"
                f"p={DEFAULT_PROBE_DEPTH})"
            ),
        },
        "attacks": {
            "version": ATTACK_VERSION,
            "families": {family: list(ATTACK_META[family]["params"]) for family in ALL_FAMILIES},
        },
        "distances": {"phash64": list(DISTANCES64), "phash256": list(DISTANCES256)},
        "corpus": {
            "generator": CORPUS_GENERATOR_VERSION,
            "count": count,
            "seed": seed,
            "digest": digest,
        },
        "stats_version": GOLDEN_STATS_VERSION,
    }
    canonical = json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_fingerprint(
    count: int = DEFAULT_CORPUS_COUNT, seed: int = DEFAULT_SEED
) -> str:
    """对外便捷入口:重建合成语料并计算金标指纹键(临时目录,零残留)。"""
    with tempfile.TemporaryDirectory(prefix="netsentinel_phash_redteam_fp_") as tmp:
        manifest = make_synthetic_corpus(Path(tmp) / "corpus", count, seed)
        return _fingerprint_digest(count, seed, corpus_digest(manifest))


def _gate_metric_value(row: dict[str, Any], metric: str) -> float:
    """从攻击族统计行提取门禁指标值(非法指标名抛中文 ValueError)。"""
    try:
        if metric.startswith("recall_phash64_d"):
            return float(row["recall"]["phash64"][metric.rsplit("_d", 1)[1]])
        if metric.startswith("recall_phash256_d"):
            return float(row["recall"]["phash256"][metric.rsplit("_d", 1)[1]])
        if metric.startswith("recall_lsh_single_d"):
            return float(row["recall"]["lsh_single"][metric.rsplit("_d", 1)[1]])
        if metric.startswith("recall_lsh_multi_d"):
            return float(row["recall"]["lsh_multi"][metric.rsplit("_d", 1)[1]])
    except (KeyError, TypeError) as exc:
        raise ValueError(f"门禁指标提取失败:{metric!r}({exc})") from exc
    raise ValueError(f"metric_tolerance/取值:非门禁指标 {metric!r}(允许:{list(GATE_METRICS)})")


def metric_tolerance(
    metric: str,
    value: float,
    wilson_bounds: list[float] | tuple[float, float],
    *,
    tol_ratio: float = TOL_VALUE_RATIO,
) -> float:
    """单指标容差:``max(tol_ratio·|value|, Wilson 95% CI 半宽)``。

    与 adversarial 的统计自洽口径一致:采样噪声天然落在 CI 半宽内
    (不误报),系统性退化则超出(不漏报);本基准全确定性,容差实际
    由 ``tol_ratio`` 主导(默认 10%)。非法指标名抛中文 ValueError。
    """
    if metric not in GATE_METRICS:
        raise ValueError(f"metric_tolerance:非门禁指标 {metric!r}(允许:{list(GATE_METRICS)})")
    if tol_ratio < 0:
        raise ValueError(f"tol_ratio 无效:须为非负数,得到 {tol_ratio!r}")
    ci_half = (float(wilson_bounds[1]) - float(wilson_bounds[0])) / 2.0
    return max(abs(float(value)) * float(tol_ratio), ci_half)


def build_golden_entry(
    families: list[dict[str, Any]], *, tol_ratio: float = TOL_VALUE_RATIO
) -> dict[str, dict[str, float]]:
    """由基准 payload 的族统计行构建金标条目 ``{"<族>/<指标>": {value, tol}}``。"""
    entry: dict[str, dict[str, float]] = {}
    for row in families:
        for metric in GATE_METRICS:
            value = _gate_metric_value(row, metric)
            wilson = row["wilson95"][metric]
            entry[f"{row['type']}/{metric}"] = {
                "value": round(value, 4),
                "tol": round(metric_tolerance(metric, value, wilson, tol_ratio=tol_ratio), 6),
            }
    return entry


def load_golden(path: str | Path) -> dict[str, Any]:
    """读金标文件并做结构校验;文件不存在 → ``{}``(空金标,缺基线路径)。

    损坏 / 坏 JSON / 顶层非对象 / 版本不兼容 / 指标表结构非法 →
    :class:`RedteamGoldenError`(中文;CLI 退出码 1)。下划线开头键为
    元数据,不参与指纹比对(口径对齐 adversarial.load_golden)。
    """
    golden = Path(path)
    if not golden.exists():
        return {}
    try:
        raw = json.loads(golden.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RedteamGoldenError(
            f"金标文件无法解析:{golden}({exc});请修复或删除后用 --update-golden 重建"
        ) from exc
    if not isinstance(raw, dict):
        raise RedteamGoldenError(
            f"金标文件顶层必须是对象(fingerprint → 指标表),当前为 {type(raw).__name__}:{golden}"
        )
    schema = raw.get("_schema")
    if schema is not None and schema != GOLDEN_SCHEMA:
        raise RedteamGoldenError(
            f"金标文件版本不兼容:期望 {GOLDEN_SCHEMA},实际 {schema!r};请 --update-golden 重建"
        ) from None
    for key, entry in raw.items():
        if key.startswith("_"):
            continue
        if not isinstance(entry, dict) or not entry:
            raise RedteamGoldenError(
                f"金标文件结构非法:指纹 {key[:8]}… 的指标表缺失或为空"
                "(应为 \"<族>/<指标>\": {value, tol})"
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
                raise RedteamGoldenError(
                    f"金标文件结构非法:指纹 {key[:8]}… 的指标 {metric!r} 应为 "
                    "{value: 数值, tol: 非负数值}"
                )
    return raw


def save_golden(
    path: str | Path,
    fingerprint: str,
    entry: dict[str, dict[str, float]],
    *,
    count: int | None = None,
    seed: int | None = None,
    tol_ratio: float = TOL_VALUE_RATIO,
) -> None:
    """写出金标文件(整体重建:只保留本次指纹的条目;父目录自动创建)。"""
    data: dict[str, Any] = {
        "_schema": GOLDEN_SCHEMA,
        "_stats_version": GOLDEN_STATS_VERSION,
        "_tolerance_rule": _TOL_RULE_TEXT,
        "_updated_at": now_iso(),
        "_basis": {
            "corpus_count": count,
            "corpus_seed": seed,
            "tol_ratio": tol_ratio,
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
    families: list[dict[str, Any]], golden_entry: dict[str, Any]
) -> list[dict[str, Any]]:
    """逐指标比对 ``|当前 − 基线| > 容差``,返回中文违例清单。

    基线缺该指标键(统计版本过旧)→ 按"指标缺基线"违例,宁可拦截也不
    静默跳过(fail-safe 方向);违例项含指标名 / 基线 / 当前 / 容差 /
    偏差五要素,CLI 直印 message。
    """
    violations: list[dict[str, Any]] = []
    for row in families:
        for metric in GATE_METRICS:
            key = f"{row['type']}/{metric}"
            spec = golden_entry.get(key)
            current = _gate_metric_value(row, metric)
            if spec is None:
                violations.append(
                    {
                        "kind": "missing_metric",
                        "metric": key,
                        "family": row["type"],
                        "metric_name": metric,
                        "baseline": None,
                        "current": current,
                        "tol": None,
                        "delta": None,
                        "message": (
                            f"{row['label']}({row['type']})·{metric}:金标缺该指标基线"
                            "(统计版本过旧?),请 --update-golden 重建"
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
                        "family": row["type"],
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


def _missing_baseline_violation(fingerprint: str, golden: Path) -> dict[str, Any]:
    """构造"指纹缺基线"违例(missing_baseline=fail 时按回归违例处理)。"""
    return {
        "kind": "missing_baseline",
        "metric": None,
        "family": None,
        "metric_name": None,
        "baseline": None,
        "current": None,
        "tol": None,
        "delta": None,
        "fingerprint": fingerprint,
        "message": (
            f"指纹 {fingerprint[:12]}… 在金标 {golden} 中无基线(指纹算法/攻击族"
            "版本/语料或统计版本已变化),missing_baseline=fail 按违例处理;"
            "确认变更合理后请先 --update-golden 重建金标"
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
            "容差=max(--tol-ratio·|基线|, Wilson CI 半宽))"
        )
    for gone in (key for key in old_fps if key != new_fingerprint):
        lines.append(
            f"指纹 {gone[:12]}…:移除(与当前指纹算法/攻击族/语料不再匹配,"
            "金标按本次结果整体重建)"
        )
    return lines


def _gate_section(
    payload: dict[str, Any],
    *,
    fingerprint: str,
    golden_path: str | Path,
    update_golden: bool,
    missing_baseline: str,
    tol_ratio: float,
) -> dict[str, Any]:
    """执行金标门禁并把结果挂到 ``payload["gate"]``(由 :func:`run` 调用)。"""
    if missing_baseline not in MISSING_BASELINE_POLICIES:
        raise RedteamError(
            f"missing_baseline 取值非法:{missing_baseline!r}"
            f"(允许:{sorted(MISSING_BASELINE_POLICIES)})"
        )
    golden = Path(golden_path)
    raw = load_golden(golden)  # 不存在 → {};损坏 → RedteamGoldenError(退出码 1)

    section: dict[str, Any] = {
        "mode": "update" if update_golden else "check",
        "golden_path": str(golden),
        "fingerprint": fingerprint,
        "missing_baseline": missing_baseline,
        "tolerance_rule": _TOL_RULE_TEXT,
        "metrics_total": len(payload["families"]) * len(GATE_METRICS),
    }

    if update_golden:
        entry = build_golden_entry(payload["families"], tol_ratio=tol_ratio)
        diff = _update_diff_lines(raw, fingerprint, entry)
        save_golden(
            golden,
            fingerprint,
            entry,
            count=payload["corpus"]["count"],
            seed=payload["corpus"]["seed"],
            tol_ratio=tol_ratio,
        )
        telemetry.inc("phash_redteam.golden.updates")
        section.update(
            {"status": "updated", "metrics_recorded": len(entry), "update_diff": diff}
        )
        return section

    entry = raw.get(fingerprint)
    if entry is None:
        warning = (
            f"指纹 {fingerprint[:12]}… 在金标 {golden} 中缺基线(指纹算法/攻击族"
            "版本/语料或统计版本已变化),本次按警告跳过比对;确认变更合理后"
            "请 --update-golden 重建金标"
        )
        if missing_baseline == "fail":
            section["status"] = "violations"
            section["violations"] = [_missing_baseline_violation(fingerprint, golden)]
        else:
            section["status"] = "no_baseline"
            section["warnings"] = [warning]
        return section

    violations = evaluate_gate(payload["families"], entry)
    if violations:
        telemetry.inc("phash_redteam.gate.violations", len(violations))
    section["status"] = "violations" if violations else "pass"
    section["violations"] = violations
    return section


# ---------------------------------------------------------------------------
# 评测主流程
# ---------------------------------------------------------------------------


def _rate(hits: int, total: int) -> float:
    """命中率(保留 4 位);total 为 0 时返回 0.0(防御,正常不触发)。"""
    return round(hits / total, 4) if total else 0.0


def _family_row(family: str, records: list[dict[str, Any]], count: int) -> dict[str, Any]:
    """把一个攻击族的逐变体记录聚合为统计行(参数格 × 族均值 + Wilson CI)。"""
    meta = ATTACK_META[family]
    params = list(meta["params"])
    cells: dict[str, dict[str, dict[str, float]]] = {}
    # 整数命中计数:fp 维度 → 距离档 → 命中数(参数格与族均值共用,无二次舍入)。
    hits: dict[str, dict[str, dict[str, int]]] = {}

    for param in params:
        bucket = [r for r in records if r["family"] == family and r["param"] == param]
        param_hits: dict[str, dict[str, int]] = {
            "phash64": {
                str(d): sum(1 for r in bucket if r["dist64"] <= d) for d in DISTANCES64
            },
            "phash256": {
                str(D): sum(1 for r in bucket if r["dist256"] <= D) for D in DISTANCES256
            },
            "lsh_single": {
                str(d): sum(
                    1 for r in bucket if r["lsh_single_in_d16"] and r["dist64"] <= d
                )
                for d in DISTANCES64
            },
            "lsh_multi": {
                str(d): sum(
                    1 for r in bucket if r["lsh_multi_in_d16"] and r["dist64"] <= d
                )
                for d in DISTANCES64
            },
        }
        hits[param] = param_hits
        cells[param] = {
            fp: {d_key: _rate(k, count) for d_key, k in per_d.items()}
            for fp, per_d in param_hits.items()
        }

    n_total = count * len(params)
    recall = {
        fp: {
            d_key: _rate(sum(hits[param][fp][d_key] for param in params), n_total)
            for d_key in hits[params[0]][fp]
        }
        for fp in hits[params[0]]
    }

    # Wilson 95% CI(门禁指标逐项;召回为二项占比,Wilson 在 0/N 与
    # N/N 边界不塌缩——复用 adversarial 的 V9 统计层,固定闭式公式)。
    wilson95: dict[str, list[float]] = {}
    for metric in GATE_METRICS:
        value = _gate_metric_value({"recall": recall, "type": family}, metric)
        k = int(round(value * n_total))
        lo, hi = wilson_ci95(k, n_total)
        wilson95[metric] = [round(lo, 4), round(hi, 4)]

    # min_effective_attack:参数沿强度递增序,首个召回 < RECALL_FLOOR 的档位
    # (主工作点 phash64@d8 暴力真值 / 多表 LSH@d8 两个口径各报一份)。
    def _first_drop(fp: str, d_key: str) -> dict[str, Any] | None:
        for param in params:
            value = cells[param][fp][d_key]
            if value < RECALL_FLOOR:
                return {"param": param, "recall": value}
        return None

    # 最脆弱格(族内所有 参数×指纹×距离 的最小召回,Top3 排序依据)。
    worst: dict[str, Any] | None = None
    for param in params:
        for fp, per_d in cells[param].items():
            for d_key, value in per_d.items():
                if worst is None or value < worst["recall"]:
                    worst = {
                        "recall": value,
                        "param": param,
                        "metric": f"{fp}@d{d_key}",
                    }

    return {
        "type": family,
        "label": meta["label"],
        "unit": meta["unit"],
        "desc": meta["desc"],
        "params": params,
        "n_images": count,
        "n_variants": n_total,
        "cells": cells,
        "recall": recall,
        "wilson95": wilson95,
        "min_effective_attack": {
            "brute_d8": _first_drop("phash64", "8"),
            "lsh_mt_d8": _first_drop("lsh_multi", "8"),
        },
        "min_cell": worst,
    }


def run(
    out_dir: str | Path,
    *,
    count: int = DEFAULT_CORPUS_COUNT,
    seed: int = DEFAULT_SEED,
    gate: bool = False,
    golden_path: str | Path | None = None,
    update_golden: bool = False,
    missing_baseline: str = "warn",
    tol_ratio: float = TOL_VALUE_RATIO,
) -> dict[str, Any]:
    """跑一次 pHash 红队基准:合成语料 → 指纹入库 → 攻击变体 → 召回矩阵 → 报告。

    流程(全程临时目录,零残留;语料 / 变体不写仓库目录):

    1. 确定性生成 ``count`` 张合成基线图(构图 / 尺寸轮换,同种子字节级
       一致),计算 phash64 + phash256;phash64 灌入 ``LSHIndex(bands=4)``
       与 ``MultiTableLSH()``(V12 默认参数)两索引;
    2. 每图生成恒等重存 + 9 攻击族 × 32 档参数的变体,逐变体计算两套
       指纹并查询两索引(查询一次 max_distance=16,逐 d 档由距离过滤
       派生——候选集只依赖桶键,d ≤ 16 档语义等价于分别查询);
    3. 聚合:攻击族 × 指纹 × d 召回矩阵(含 Wilson 95% CI)、最脆弱
       攻击 Top3(按族内最小召回格升序)、min_effective_attack(首个
       召回 < 0.9 的攻击强度)、单表 vs 多表索引在全攻击变体上的召回
       对比;LSH 召回以暴力全扫真值对账(结构性 ≤);
    4. 写 ``out_dir/phash_redteam_report.md``(中文)+
       ``phash_redteam_report.json``,返回同一份 payload;
    5. ``gate=True`` / ``update_golden=True`` 时执行金标门禁(见模块
       docstring);未安装 Pillow → :class:`RedteamError`(CLI 层转
       退出码 2),金标文件损坏 → :class:`RedteamGoldenError`(退出码 1)。
    """
    pil = _load_pil()
    if pil is None:
        raise RedteamError(
            "未安装 Pillow,无法运行 pHash 红队基准(pip install Pillow 后重试;"
            "本基准以退出码 2 结束,不产出无效报告)"
        )
    if not isinstance(count, int) or isinstance(count, bool) or count < 1 or count > 128:
        raise RedteamError(f"count 无效:须为 1~128 的整数,得到 {count!r}")
    if tol_ratio < 0:
        raise RedteamError(f"tol_ratio 无效:须为非负数,得到 {tol_ratio!r}")

    telemetry.inc("phash_redteam.images", count)
    with telemetry.timer("phash_redteam.run"):
        with tempfile.TemporaryDirectory(prefix="netsentinel_phash_redteam_") as tmp:
            root = Path(tmp)
            manifest = make_synthetic_corpus(root / "corpus", count, seed)
            digest = corpus_digest(manifest)

            base64_hashes = [phash(item["path"]) for item in manifest]
            base256_hashes = [phash256(item["path"]) for item in manifest]

            single = LSHIndex(bands=DEFAULT_BANDS)
            multi = MultiTableLSH()
            for index_i, hex64 in enumerate(base64_hashes):
                single.insert(hex64, index_i)
                multi.insert(hex64, index_i)

            # 基图间可分性(上下文:异图最小距 vs 攻击后同图距的间隔)。
            pair64 = [
                hamming(base64_hashes[i], base64_hashes[j])
                for i in range(count)
                for j in range(i + 1, count)
            ]
            pair256 = [
                hamming_hex(base256_hashes[i], base256_hashes[j])
                for i in range(count)
                for j in range(i + 1, count)
            ]

            records: list[dict[str, Any]] = []
            max_d = max(DISTANCES64)
            for image_index, item in enumerate(manifest):
                variants = generate_variants(
                    item["path"],
                    root / "variants",
                    seed=seed,
                    image_index=image_index,
                )
                telemetry.inc(
                    "phash_redteam.variants", len(variants)
                )
                for variant in variants:
                    query64 = phash(variant["path"])
                    query256 = phash256(variant["path"])
                    dist64 = hamming(query64, base64_hashes[image_index])
                    dist256 = hamming_hex(query256, base256_hashes[image_index])
                    in_single = image_index in single.query(query64, max_distance=max_d)
                    in_multi = image_index in multi.query(query64, max_distance=max_d)
                    records.append(
                        {
                            "image": image_index,
                            "kind": item["kind"],
                            "family": variant["family"],
                            "param": variant["param"],
                            "dist64": dist64,
                            "dist256": dist256,
                            "lsh_single_in_d16": bool(in_single),
                            "lsh_multi_in_d16": bool(in_multi),
                        }
                    )

        families = [
            _family_row(family, records, count) for family in ALL_FAMILIES
        ]

        # 索引对比:全部攻击变体(排除恒等控制组)在逐 d 档的召回,
        # 多表相对单表的优势可量化(Δ = 多表 − 单表)。
        attacked = [r for r in records if r["family"] != IDENTITY_FAMILY]
        n_attacked = len(attacked)
        index_compare: dict[str, Any] = {"d_values": list(DISTANCES64), "n_variants": n_attacked}
        for d in DISTANCES64:
            brute_k = sum(1 for r in attacked if r["dist64"] <= d)
            single_k = sum(1 for r in attacked if r["lsh_single_in_d16"] and r["dist64"] <= d)
            multi_k = sum(1 for r in attacked if r["lsh_multi_in_d16"] and r["dist64"] <= d)
            index_compare[str(d)] = {
                "brute": _rate(brute_k, n_attacked),
                "lsh_single": _rate(single_k, n_attacked),
                "lsh_multi": _rate(multi_k, n_attacked),
                "mt_minus_single": round(_rate(multi_k, n_attacked) - _rate(single_k, n_attacked), 4),
            }

        # 最脆弱攻击 Top3(按族内最小召回格升序;恒等控制组不参与)。
        ranked = sorted(
            (row for row in families if row["type"] != IDENTITY_FAMILY),
            key=lambda row: (row["min_cell"]["recall"], row["type"]),
        )
        top3 = [
            {
                "type": row["type"],
                "label": row["label"],
                "min_recall": row["min_cell"]["recall"],
                "worst_param": row["min_cell"]["param"],
                "worst_metric": row["min_cell"]["metric"],
            }
            for row in ranked[:3]
        ]

        payload: dict[str, Any] = {
            "generated_at": now_iso(),
            "benchmark": "phash_redteam",
            "corpus": {
                "mode": "synthetic",
                "generator": CORPUS_GENERATOR_VERSION,
                "count": count,
                "seed": seed,
                "kinds": list(CORPUS_KINDS),
                "sizes": [list(size) for size in CORPUS_SIZES],
                "digest": digest,
                "separability": {
                    "phash64_min_pair_dist": min(pair64) if pair64 else None,
                    "phash256_min_pair_dist": min(pair256) if pair256 else None,
                },
            },
            "attacks": {
                "version": ATTACK_VERSION,
                "families": {
                    family: {
                        "label": ATTACK_META[family]["label"],
                        "params": list(ATTACK_META[family]["params"]),
                    }
                    for family in ALL_FAMILIES
                },
                "total_variants_per_image": 1 + sum(
                    len(ATTACK_META[family]["params"]) for family in ATTACK_FAMILIES
                ),
            },
            "fingerprints": {
                "phash64": "netsentinel.intel.phash.phash(64bit DCT)",
                "phash256": "netsentinel.vision.phash2.phash256(256bit 分块 DCT)",
                "indexes": {
                    "single": f"LSHIndex(bands={DEFAULT_BANDS})",
                    "multi": (
                        f"MultiTableLSH(K={DEFAULT_TABLES},B={DEFAULT_KEY_BITS},"
                        f"p={DEFAULT_PROBE_DEPTH})"
                    ),
                    "note": "两索引均以 phash64 灌库;LSH 召回以暴力全扫为真值对账",
                },
            },
            "distances": {
                "phash64": list(DISTANCES64),
                "phash256": list(DISTANCES256),
            },
            "primary_operating_point": {
                "fingerprint": "phash64",
                "d": 8,
                "recall_floor": RECALL_FLOOR,
                "note": "min_effective_attack 与 Top3 脆弱度的工作点口径之一",
            },
            "families": families,
            "index_compare": index_compare,
            "top3_fragile": top3,
            "inference": {
                "wilson_ci_method": "Wilson score interval, closed-form, z=1.96(复用 adversarial V9 统计层)",
                "notes": (
                    "召回为二项占比,每个门禁指标附 Wilson 95% CI;全部随机源"
                    "固定种子,同一 (count, seed) 任何次运行输出逐位一致。"
                ),
            },
            "variants_note": (
                "合成基线图与攻击变体均生成于临时目录并在运行结束后自动清理;"
                "报告只写入调用方指定的输出目录,仓库语料目录零写入。"
            ),
            "details": records,
        }

        if gate or update_golden:
            payload["gate"] = _gate_section(
                payload,
                fingerprint=_fingerprint_digest(count, seed, digest),
                golden_path=golden_path if golden_path is not None else DEFAULT_GOLDEN_PATH,
                update_golden=update_golden,
                missing_baseline=missing_baseline,
                tol_ratio=tol_ratio,
            )

        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "phash_redteam_report.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (out / "phash_redteam_report.md").write_text(
            render_markdown(payload), encoding="utf-8"
        )
        logger.info(
            "pHash 红队基准完成:count=%d variants=%d top3=%s",
            count, len(records), ",".join(item["type"] for item in top3),
        )
    return payload


# ---------------------------------------------------------------------------
# 报告渲染(JSON payload → 中文 markdown,单遍字符串累加)
# ---------------------------------------------------------------------------


def _r4(value: float) -> str:
    """报告用的 4 位小数格式化。"""
    return f"{float(value):.4f}"


def render_markdown(payload: dict[str, Any]) -> str:
    """把基准 payload 渲染为中文 Markdown 报告(单文件,无外部依赖)。"""
    corpus = payload["corpus"]
    lines: list[str] = []
    lines.append("# NetSentinel pHash 指纹对抗鲁棒性红队基准报告(V12)")
    lines.append("")
    lines.append(f"- 生成时间:{payload['generated_at']}")
    lines.append(
        f"- 基线图集:合成生成器 `{corpus['generator']}` × {corpus['count']} 张"
        f"(seed={corpus['seed']},构图 {len(corpus['kinds'])} 类轮换 / 尺寸 2 档交替,"
        f"语料指纹 `{corpus['digest'][:12]}…`)"
    )
    sep = corpus["separability"]
    if sep["phash64_min_pair_dist"] is not None:
        lines.append(
            f"- 异图可分性(基图两两最小距):phash64 = {sep['phash64_min_pair_dist']} bit,"
            f" phash256 = {sep['phash256_min_pair_dist']} bit(召回损失的参照间隔)"
        )
    lines.append(
        f"- 攻击族版本:`{payload['attacks']['version']}`,9 族 × 32 档参数,"
        f"每图 {payload['attacks']['total_variants_per_image']} 个变体"
        "(含恒等控制组)"
    )
    lines.append(
        "- 距离网格:phash64 @ d∈"
        f"{tuple(payload['distances']['phash64'])}(日常工作点 d=8;"
        "V12 多表 LSH 高距区 d=12/16);phash256 @ "
        f"{tuple(payload['distances']['phash256'])}(位宽 4 倍同比例)"
    )
    lines.append(
        "- 召回口径:变体指纹与**其源图**基线指纹的距离 ≤ d,且(索引列)"
        "源图在候选命中内——暴力全扫真值 vs LSH 索引召回,索引列结构性 ≤ 真值列"
    )
    lines.append(f"- {payload['variants_note']}")
    lines.append("")

    # ---- 一、召回矩阵 ----
    lines.append("## 一、召回矩阵(攻击族 × 指纹 × d)")
    lines.append("")
    lines.append("### 1.1 逐参数格(暴力全扫真值)")
    lines.append("")
    header = (
        "| 攻击族 | 参数 | " + " | ".join(
            f"p64@{d}" for d in payload["distances"]["phash64"]
        ) + " | " + " | ".join(
            f"p256@{d}" for d in payload["distances"]["phash256"]
        ) + " |"
    )
    lines.append(header)
    lines.append(
        "| --- | --- | " + " | ".join(["---:"] * (
            len(payload["distances"]["phash64"]) + len(payload["distances"]["phash256"])
        )) + " |"
    )
    for row in payload["families"]:
        for param in row["params"]:
            cell = row["cells"][param]
            cells = [row["label"] if param == row["params"][0] else "", param]
            cells += [_r4(cell["phash64"][str(d)]) for d in payload["distances"]["phash64"]]
            cells += [_r4(cell["phash256"][str(d)]) for d in payload["distances"]["phash256"]]
            lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

    lines.append("### 1.2 逐参数格(LSH 索引召回,基于 phash64)")
    lines.append("")
    lines.append(
        "| 攻击族 | 参数 | " + " | ".join(
            f"单表@{d}" for d in payload["distances"]["phash64"]
        ) + " | " + " | ".join(
            f"多表@{d}" for d in payload["distances"]["phash64"]
        ) + " |"
    )
    lines.append(
        "| --- | --- | " + " | ".join(["---:"] * (2 * len(payload["distances"]["phash64"]))) + " |"
    )
    for row in payload["families"]:
        for param in row["params"]:
            cell = row["cells"][param]
            cells = [row["label"] if param == row["params"][0] else "", param]
            cells += [_r4(cell["lsh_single"][str(d)]) for d in payload["distances"]["phash64"]]
            cells += [_r4(cell["lsh_multi"][str(d)]) for d in payload["distances"]["phash64"]]
            lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

    lines.append("### 1.3 族均值(全部参数档平均;phash64@8 附 Wilson 95% CI)")
    lines.append("")
    lines.append(
        "| 攻击族 | 变体数 | p64@8 [CI95] | p64@12 | p64@16 | p256@32 | "
        "单表@8 | 多表@8 | 多表@16 |"
    )
    lines.append("| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in payload["families"]:
        recall = row["recall"]
        w_lo, w_hi = row["wilson95"]["recall_phash64_d8"]
        lines.append(
            "| {} | {} | {} [{}, {}] | {} | {} | {} | {} | {} | {} |".format(
                row["label"],
                row["n_variants"],
                _r4(recall["phash64"]["8"]),
                _r4(w_lo),
                _r4(w_hi),
                _r4(recall["phash64"]["12"]),
                _r4(recall["phash64"]["16"]),
                _r4(recall["phash256"]["32"]),
                _r4(recall["lsh_single"]["8"]),
                _r4(recall["lsh_multi"]["8"]),
                _r4(recall["lsh_multi"]["16"]),
            )
        )
    lines.append("")

    # ---- 二、最脆弱攻击 Top3 ----
    lines.append("## 二、最脆弱攻击 Top3(族内最小召回格升序)")
    lines.append("")
    lines.append("| # | 攻击族 | 最弱格召回 | 最弱参数 | 最弱口径 |")
    lines.append("| ---: | --- | ---: | --- | --- |")
    for rank, item in enumerate(payload["top3_fragile"], start=1):
        lines.append(
            f"| {rank} | {item['label']}({item['type']}) | {_r4(item['min_recall'])}"
            f" | {item['worst_param']} | {item['worst_metric']} |"
        )
    lines.append("")

    # ---- 三、min_effective_attack ----
    floor = payload["primary_operating_point"]["recall_floor"]
    lines.append(
        f"## 三、min_effective_attack(首个召回 < {floor} 的攻击强度,参数按强度递增序)"
    )
    lines.append("")
    lines.append("| 攻击族 | 暴力真值 phash64@d8 | 多表 LSH@d8 |")
    lines.append("| --- | --- | --- |")
    for row in payload["families"]:
        brute = row["min_effective_attack"]["brute_d8"]
        mt = row["min_effective_attack"]["lsh_mt_d8"]
        brute_text = (
            f"`{brute['param']}`(召回 {_r4(brute['recall'])})" if brute else "未跌破(全档 ≥ 0.9)"
        )
        mt_text = (
            f"`{mt['param']}`(召回 {_r4(mt['recall'])})" if mt else "未跌破(全档 ≥ 0.9)"
        )
        lines.append(f"| {row['label']}({row['type']}) | {brute_text} | {mt_text} |")
    lines.append("")

    # ---- 四、索引对比 ----
    lines.append("## 四、索引对比:单表 vs 多表(全部攻击变体聚合,基于 phash64)")
    lines.append("")
    cmp_data = payload["index_compare"]
    lines.append(f"变体总数:{cmp_data['n_variants']}(恒等控制组除外)。")
    lines.append("")
    lines.append("| d | 暴力真值 | 单表 LSH | 多表 LSH | 多表−单表 Δ |")
    lines.append("| ---: | ---: | ---: | ---: | ---: |")
    for d in cmp_data["d_values"]:
        row = cmp_data[str(d)]
        lines.append(
            f"| {d} | {_r4(row['brute'])} | {_r4(row['lsh_single'])}"
            f" | {_r4(row['lsh_multi'])} | {_r4(row['mt_minus_single'])} |"
        )
    lines.append("")
    lines.append(
        "- 单表分带 LSH 在 d ≥ bands(默认 4)后召回断崖(每带各翻一位即全毁),"
        "多表 multi-probe(V12)把高距区召回拉回——Δ 列即其可量化优势;"
        "召回升级的代价刻度见 compare_calls / probe_calls 操作计数(phash_lsh)。"
    )
    lines.append("")

    # ---- 五、逐变体明细 ----
    lines.append("## 五、逐变体明细(距离 + 索引候选命中)")
    lines.append("")
    lines.append("| 图 | 构图 | 攻击族 | 参数 | d(p64) | d(p256) | 单表@16 | 多表@16 |")
    lines.append("| ---: | --- | --- | --- | ---: | ---: | --- | --- |")
    for record in payload["details"]:
        lines.append(
            "| {} | {} | {} | {} | {} | {} | {} | {} |".format(
                record["image"],
                record["kind"],
                record["family"],
                record["param"],
                record["dist64"],
                record["dist256"],
                "√" if record["lsh_single_in_d16"] else "×",
                "√" if record["lsh_multi_in_d16"] else "×",
            )
        )
    lines.append("")

    # ---- 六、口径与结论 ----
    lines.append("## 六、口径与结论")
    lines.append("")
    lines.append(
        "- 恒等控制组(无攻击重存)在任何指纹 × 距离档的召回必须 = 1.0000"
        "(数学自检;偏离即评测管线自身有 bug)。"
    )
    lines.append(
        "- 攻击变体只作用于**本地合成图**(服务防御评测);本基准全程离线零网络,"
        "结论只用于改进指纹与索引配置(如 V12 多表 LSH 参数、金字塔哈希分层),"
        "不构成任何自动处置依据。"
    )
    lines.append(
        "- 翻转类攻击对 DCT 指纹属结构性强攻击(镜像改变奇次频率系数符号,"
        "约半数位翻转):若业务需要抗翻转,应在入库时补镜像指纹(后续建议)。"
    )
    lines.append(
        f"- 统计层:召回为二项占比,{payload['inference']['wilson_ci_method']};"
        "金标门禁容差 = max(--tol-ratio·|基线|, Wilson CI 半宽)。"
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

    返回码(三态,对齐 ops/audit_verify 惯例):

    - 0:成功(纯报告 / 金标重建 / 门禁通过 / 缺基线 warn 跳过);
    - 1:金标文件输入错误(损坏 / 坏 JSON / 结构非法 / 版本不兼容);
    - 2:金标门禁回归违例,或 Pillow 缺失 / 参数非法等可预期错误。
    """
    _ensure_utf8_stdio()
    default_out = str(Path(__file__).resolve().parent / "out")
    parser = argparse.ArgumentParser(
        prog="python benchmarks/phash_redteam.py",
        description=(
            "NetSentinel pHash 指纹对抗鲁棒性红队基准:9 攻击族参数网格量化"
            " phash64/phash256 与单表/多表 LSH 的扰动召回损失(全程离线,"
            "合成语料,报告 JSON+markdown)"
        ),
    )
    parser.add_argument(
        "--out", default=default_out, help=f"报告输出目录(默认 {default_out})"
    )
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_CORPUS_COUNT,
        help=f"合成基线图张数 1~128(默认 {DEFAULT_CORPUS_COUNT},确定性生成)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"合成语料与攻击噪声种子(默认 {DEFAULT_SEED})",
    )
    gate_group = parser.add_argument_group("金标回归门禁")
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
        help="指纹未命中金标(指纹算法/攻击族/语料已变化)时:warn 跳过并提示"
        "(默认),或 fail 按回归违例处理(退出码 2)",
    )
    gate_group.add_argument(
        "--tol-ratio",
        type=float,
        default=TOL_VALUE_RATIO,
        help=f"容差的基线相对比例分支(默认 {TOL_VALUE_RATIO};容差 = max(比例·|基线|, Wilson CI 半宽))",
    )
    args = parser.parse_args(argv)

    try:
        payload = run(
            args.out,
            count=args.count,
            seed=args.seed,
            gate=args.gate,
            golden_path=args.golden,
            update_golden=args.update_golden,
            missing_baseline=args.missing_baseline,
            tol_ratio=args.tol_ratio,
        )
    except RedteamError as exc:
        # 金标输入错误(子类 exit_code=1)与可预期错误(2)分流。
        telemetry.inc("phash_redteam.errors")
        print(f"错误:{exc}", file=sys.stderr)
        return int(getattr(exc, "exit_code", 2))

    print(
        f"pHash 红队基准完成:语料={payload['corpus']['count']} 张(合成,seed="
        f"{payload['corpus']['seed']}) 变体={len(payload['details'])} 个"
    )
    none_row = next(
        row for row in payload["families"] if row["type"] == IDENTITY_FAMILY
    )
    print(
        "  控制组(无攻击)召回:p64@8={:.4f} p256@32={:.4f}(必须为 1.0)".format(
            none_row["recall"]["phash64"]["8"],
            none_row["recall"]["phash256"]["32"],
        )
    )
    for item in payload["top3_fragile"]:
        print(
            "  最脆弱Top3:{}({}) 最弱格 {} @ {}({})".format(
                item["label"], item["type"], _r4(item["min_recall"]),
                item["worst_param"], item["worst_metric"],
            )
        )
    cmp_data = payload["index_compare"]
    for d in cmp_data["d_values"]:
        row = cmp_data[str(d)]
        print(
            "  索引对比 d={}:暴力 {} 单表 {} 多表 {} Δ{}".format(
                d, _r4(row["brute"]), _r4(row["lsh_single"]),
                _r4(row["lsh_multi"]), _r4(row["mt_minus_single"]),
            )
        )
    out_dir = Path(args.out)
    print(
        f"报告已写出:{out_dir / 'phash_redteam_report.md'}"
        f" 与 {out_dir / 'phash_redteam_report.json'}"
    )

    # --- 金标门禁结果输出与退出码裁决(对齐 adversarial CLI) ---
    gate_info = payload.get("gate")
    if gate_info is not None:
        fp_display = gate_info["fingerprint"][:12] + "…"
        if gate_info["mode"] == "update":
            print(
                f"金标已重建:{gate_info['golden_path']}(指纹 {fp_display},"
                f"记录 {gate_info['metrics_recorded']} 项指标基线,"
                "容差=max(--tol-ratio·|基线|, Wilson CI 半宽))"
            )
            for line in gate_info["update_diff"]:
                print(f"  {line}")
        elif gate_info["status"] == "pass":
            print(
                f"金标门禁通过:指纹 {fp_display},"
                f"{gate_info['metrics_total']} 项指标全部在容差内"
                " —— 退出码 0"
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
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
