"""A123 识别内核·肤色启发式(V7):``SkinHeuristicClassifier``(注册名 "skin")。

纯启发式离线粗筛内核 —— 零网络、零模型权重、零三方硬依赖:

- **Pillow 惰性加载**:安装了 Pillow 时可解 JPEG/WEBP/GIF 等任意格式;缺失时
  退回**纯 stdlib zlib 的最小 PNG 像素读取器** :func:`read_png_rgb`
  (IHDR/IDAT/IEND、非隔行、8bit,色型 0 灰度 / 2 RGB / 4 灰度+Alpha / 6 RGBA,
  扫描线滤波 0-None/1-Sub/2-Up/3-Average/4-Paeth 全支持;逐块 CRC 校验);
  其余格式(JPEG/WEBP 等)在缺 PIL 时直接 ``scores.error``;
- **特征**(像素采样至 ≤64x64 网格后计算,分辨率无关、结果确定):
  1. ``skin_ratio``:BT.601 YCbCr 变换后 Cb∈[77,127] 且 Cr∈[133,173] 的采样
     像素占比(Chai & Ngan 经典肤色簇阈值);
  2. ``max_blob``:8x8 粗网格上"肤色像素过半"的格再做 4-连通最大连通块,
     占 64 格比例 —— 迭代 flood fill(显式栈、无递归),每格至多入栈一次,
     **操作计数 ≤ 64**,经实例属性 ``last_flood_ops`` 导出供 bench 断言;
  3. ``edge_density``:采样灰度图上水平/垂直梯度 > 40 的像素占比;
- **组合公式**(常数模块级导出,便于调参与复算)::

      nsfw_prob = clamp(1.5*skin_ratio + 1.2*max_blob + 0.3*edge_density - 0.35, 0, 0.98)

**局限(重要)**:这是纯色调/结构启发式,不是学习到的分类模型——

- 人体艺术、沙滩照、泳装照等大面积裸露肤色但**非色情**的图片必然误报;
- 暖色调墙面/木地板/沙漠等场景色调也可能落入肤色簇造成误报;
- 重滤镜、暗光、低饱和的真实违规图可能漏报。
  因此本内核**仅作离线粗筛信号**,不得单独作为处置/举报依据,
  应与文本/页面级证据融合后由人复核。

bench(红线 31,确定性构造数据 + 操作计数,禁墙钟)::

    kernel_selfcheck()  # 8 张肤色椭圆主体图 vs 8 张风景/文本图的均值差(>0.2)

配套断言见 tests/test_heuristic_kernel.py:均值差 > 0.2、阈值分类零错分
(线性可分)、flood fill 操作计数 ≤ 64(全肤格恰 = 64)。

用法示例::

    from netsentinel.contracts import Config, ImageEvidence
    from netsentinel.vision.heuristic_kernel import SkinHeuristicClassifier

    clf = SkinHeuristicClassifier(Config())
    score = clf.classify(ImageEvidence(path="dl/a.png", url="...", source_page="..."))
    score.model == "skin" and 0.0 <= score.nsfw_prob <= 0.98

坏文件/缺解码器一律容错:``nsfw_prob=0.0`` + ``scores={"error": 原因}``,
绝不抛出。小图(宽与高均 < ``cfg.min_image_px``)照常计算,仅在 scores
标记 ``small=True``(口径与 decision.verdict 的候选过滤一致:任一边达标即非小图)。

模块导入时自动以 "skin" 注册到 classifier_base 注册表(基座缺席时静默跳过)。
仅使用标准库 + 可选 Pillow;离线运行,不发起任何网络请求。
"""
from __future__ import annotations

import logging
import struct
import zlib
from pathlib import Path
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, ImageScore

__all__ = [
    "SkinHeuristicClassifier",
    "read_png_rgb",
    "PngDecodeError",
    "kernel_selfcheck",
    "synthetic_skin_pixels",
    "synthetic_clean_pixels",
    "CB_MIN",
    "CB_MAX",
    "CR_MIN",
    "CR_MAX",
    "W_SKIN_RATIO",
    "W_MAX_BLOB",
    "W_EDGE_DENSITY",
    "BIAS",
    "PROB_CAP",
    "GRID",
    "EDGE_THRESHOLD",
    "SAMPLE_MAX",
]

logger = logging.getLogger(__name__)

try:  # 基座缺失时(并行开发期)静默降级为不注册,模块本身仍可独立使用
    from netsentinel.vision.classifier_base import NsfwClassifier, register_classifier
except ImportError:  # pragma: no cover - 仅并行开发期出现
    NsfwClassifier = object  # type: ignore[assignment,misc]
    register_classifier = None  # type: ignore[assignment]

# ---- 肤色簇阈值(BT.601 数字 YCbCr;Chai & Ngan 经典范围)--------------------
#: Cb 下/上界(闭区间)。
CB_MIN, CB_MAX = 77, 127
#: Cr 下/上界(闭区间)。
CR_MIN, CR_MAX = 133, 173

# ---- 组合公式系数(nsfw_prob = clamp(线性组合, 0, PROB_CAP))----------------
W_SKIN_RATIO: float = 1.5     # 肤色占比权重(主信号)
W_MAX_BLOB: float = 1.2       # 最大肤色连通块权重(聚集度,抑制零散暖色噪声)
W_EDGE_DENSITY: float = 0.3   # 边缘密度权重(高细节弱加分)
BIAS: float = -0.35           # 截距:白图/风景的零信号基线压到 0 分以下
PROB_CAP: float = 0.98        # 启发式不给满格置信(保留人复核空间)

# ---- 特征计算参数 -----------------------------------------------------------
#: 连通块粗网格边长(8x8=64 格;flood fill 操作计数上界即 64)。
GRID: int = 8
#: 灰度梯度边沿判定阈值(0-255)。
EDGE_THRESHOLD: int = 40
#: 特征采样网格上限(宽/高各自 ≤ 该值个采样点,步长 = max(1, 边 // 该值))。
SAMPLE_MAX: int = 64
#: 小图判定默认线(cfg.min_image_px 缺省时的回退值)。
_DEFAULT_MIN_PX: int = 200


class PngDecodeError(ValueError):
    """stdlib PNG 解码失败(魔数/约束/CRC/数据损坏)。"""


# ---------------------------------------------------------------------------
# 纯 stdlib 最小 PNG 像素读取器
# ---------------------------------------------------------------------------

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
#: 解码尺寸护栏:超出该像素总数的 IHDR 直接拒绝(防畸形头撑爆内存)。
_MAX_PIXELS = 64_000_000
#: PNG 色型 -> 每像素通道数(0 灰度 / 2 RGB / 4 灰度+Alpha / 6 RGBA)。
_PNG_CHANNELS: dict[int, int] = {0: 1, 2: 3, 4: 2, 6: 4}


def _paeth_predictor(a: int, b: int, c: int) -> int:
    """PNG 滤波 4(Paeth)预测子:a=左 b=上 c=左上。"""
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def read_png_rgb(path: str | Path) -> tuple[int, int, list[list[tuple[int, int, int]]]]:
    """最小 PNG 像素读取器(纯 stdlib zlib,无 Pillow 依赖)。

    支持范围:IHDR/IDAT/IEND 块结构、非隔行(interlace=0)、8bit、
    色型 0 灰度 / 2 RGB / 4 灰度+Alpha / 6 RGBA(Alpha 通道丢弃);
    扫描线滤波 0~4 全支持;逐块 CRC 校验;多 IDAT 块拼接。
    调色板(3)与 16bit 等其余形态抛 :class:`PngDecodeError`。

    返回 ``(width, height, rows)``,其中 ``rows[y][x] == (r, g, b)``。
    """
    data = Path(path).read_bytes()
    if len(data) < 8 or data[:8] != _PNG_SIGNATURE:
        raise PngDecodeError("非 PNG 魔数(未安装 Pillow 时无法解码 JPEG/WEBP 等格式)")

    pos = 8
    ihdr: bytes | None = None
    idat = bytearray()
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        ctype = data[pos + 4 : pos + 8]
        end = pos + 12 + length
        if end > len(data):
            raise PngDecodeError(f"PNG 块 {ctype!r} 声明长度越界(文件截断)")
        body = data[pos + 8 : pos + 8 + length]
        (crc_stored,) = struct.unpack(">I", data[pos + 8 + length : end])
        if zlib.crc32(data[pos + 4 : pos + 8 + length]) & 0xFFFFFFFF != crc_stored:
            raise PngDecodeError(f"PNG 块 {ctype!r} CRC 校验失败(数据损坏)")
        if ctype == b"IHDR":
            ihdr = bytes(body)
        elif ctype == b"IDAT":
            idat += body
        elif ctype == b"IEND":
            break
        pos = end
    if ihdr is None or len(ihdr) != 13:
        raise PngDecodeError("缺少合法 IHDR 块")
    if not idat:
        raise PngDecodeError("缺少 IDAT 像素数据")

    width, height, depth, color_type, compression, filter_method, interlace = (
        struct.unpack(">IIBBBBB", ihdr)
    )
    if width < 1 or height < 1 or width * height > _MAX_PIXELS:
        raise PngDecodeError(f"IHDR 尺寸非法:{width}x{height}")
    if depth != 8:
        raise PngDecodeError(f"仅支持 8bit PNG,实际 bit_depth={depth}")
    if color_type not in _PNG_CHANNELS:
        raise PngDecodeError(f"不支持 PNG 色型 color_type={color_type}(仅 0/2/4/6)")
    if compression != 0 or filter_method != 0 or interlace != 0:
        raise PngDecodeError("仅支持标准压缩/滤波的非隔行(Adam7 隔行不支持)")

    channels = _PNG_CHANNELS[color_type]
    stride = width * channels
    expected = height * (1 + stride)
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise PngDecodeError(f"IDAT zlib 解压失败:{exc}") from exc
    if len(raw) < expected:
        raise PngDecodeError(f"IDAT 数据不足:期望 {expected} 字节,实际 {len(raw)}")

    # 逐扫描线反滤波(filter 0~4),输出连续 RGB 字节流之前的原始通道流。
    bpp = channels
    unfiltred = bytearray()
    prev = bytearray(stride)
    p = 0
    for _ in range(height):
        ftype = raw[p]
        p += 1
        line = bytearray(raw[p : p + stride])
        p += stride
        if ftype == 0:  # None
            pass
        elif ftype == 1:  # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:  # Average
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:  # Paeth
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth_predictor(a, b, c)) & 0xFF
        else:
            raise PngDecodeError(f"未知扫描线滤波类型:{ftype}")
        unfiltred += line
        prev = line

    # 通道流 → RGB 三元组行(灰度按 (v,v,v) 展开,Alpha 一律丢弃)。
    rows: list[list[tuple[int, int, int]]] = []
    k = 0
    for _ in range(height):
        row: list[tuple[int, int, int]] = []
        for _ in range(width):
            if color_type == 2:  # RGB
                row.append((unfiltred[k], unfiltred[k + 1], unfiltred[k + 2]))
                k += 3
            elif color_type == 6:  # RGBA
                row.append((unfiltred[k], unfiltred[k + 1], unfiltred[k + 2]))
                k += 4
            elif color_type == 0:  # 灰度
                v = unfiltred[k]
                row.append((v, v, v))
                k += 1
            else:  # 灰度 + Alpha
                v = unfiltred[k]
                row.append((v, v, v))
                k += 2
        rows.append(row)
    return width, height, rows


def _try_pil():
    """惰性加载 Pillow(返回 ``PIL.Image`` 模块);缺失/坏安装返回 None。

    与 preprocess._load_pil 同口径:同时导入 ``PIL.Image`` 与 ``PIL``,
    保证 ``sys.modules["PIL"] = None`` 的测试替身能可靠模拟"未安装"。
    """
    try:
        import importlib

        image = importlib.import_module("PIL.Image")
        importlib.import_module("PIL")
    except Exception:  # None/缺失/半初始化的 PIL 一律按"未安装"处理
        return None
    return image


def _max_blob(cells: list[list[bool]]) -> tuple[float, int]:
    """8x8 布尔格上最大 4-连通"肤区块"占比(迭代 flood fill)。

    显式栈实现、入栈即标记已访问 —— 每格至多入栈/出栈一次,
    因此操作计数(出栈次数)上界恰为格子总数 64(供 bench 断言)。

    返回 ``(最大块格数 / 64, 操作计数)``。
    """
    ops = 0
    best = 0
    visited = [[False] * GRID for _ in range(GRID)]
    for j in range(GRID):
        for i in range(GRID):
            if not cells[j][i] or visited[j][i]:
                continue
            visited[j][i] = True
            stack = [(j, i)]
            size = 0
            while stack:
                y, x = stack.pop()
                ops += 1
                size += 1
                for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    ny, nx = y + dy, x + dx
                    if (
                        0 <= ny < GRID
                        and 0 <= nx < GRID
                        and cells[ny][nx]
                        and not visited[ny][nx]
                    ):
                        visited[ny][nx] = True
                        stack.append((ny, nx))
            if size > best:
                best = size
    return best / float(GRID * GRID), ops


def _combine(skin_ratio: float, max_blob: float, edge_density: float) -> float:
    """启发式线性组合 + 双侧钳制(见模块 docstring 公式)。"""
    raw = (
        W_SKIN_RATIO * skin_ratio
        + W_MAX_BLOB * max_blob
        + W_EDGE_DENSITY * edge_density
        + BIAS
    )
    return min(max(raw, 0.0), PROB_CAP)


# ---------------------------------------------------------------------------
# 合成语料(确定性,无随机源;供 bench / 自检 / A138 kernel_bench / A140 演示)
# ---------------------------------------------------------------------------

#: 4 个经典人体肤色采样(Cb/Cb 均落在肤色簇内,离边界 >10 有余量)。
_SKIN_TONES: tuple[tuple[int, int, int], ...] = (
    (224, 172, 150),
    (210, 160, 140),
    (230, 185, 165),
    (195, 145, 120),
)
#: 肤色图的深蓝背景(Cb≈140 / Cr≈121,远离肤色簇)。
_SKIN_BACKGROUND = (30, 40, 60)


def synthetic_skin_pixels(index: int) -> tuple[int, int, list[list[tuple[int, int, int]]]]:
    """第 index(0..7)张合成"肤色椭圆占主体"图像(确定性,无随机源)。

    240x240、深蓝底、居中(±2%)人体色调椭圆;色调在 4 个肤色采样间轮换,
    半径随 index 在 0.36~0.42 / 0.40~0.45 间变化,8 张互不相同。
    """
    w = h = 240
    tone = _SKIN_TONES[index % len(_SKIN_TONES)]
    rx = 0.36 + 0.02 * (index % 4)
    ry = 0.40 + 0.025 * (index % 3)
    cx = 0.5 + 0.02 * ((index % 3) - 1)
    cy = 0.5 + 0.02 * ((index % 2) - 0.5)
    inv_rx2 = 1.0 / (rx * rx)
    inv_ry2 = 1.0 / (ry * ry)
    rows: list[list[tuple[int, int, int]]] = []
    for y in range(h):
        dy = y / h - cy
        row: list[tuple[int, int, int]] = []
        for x in range(w):
            dx = x / w - cx
            row.append(
                tone if dx * dx * inv_rx2 + dy * dy * inv_ry2 <= 1.0 else _SKIN_BACKGROUND
            )
        rows.append(row)
    return w, h, rows


def synthetic_clean_pixels(index: int) -> tuple[int, int, list[list[tuple[int, int, int]]]]:
    """第 index(0..7)张合成"风景 / 纯文本结构"图像(确定性,无随机源)。

    偶数下标:天空渐变 + 草地 + 深绿树影(风景);奇数下标:白底黑字
    伪文本行(结构)。全部像素的 Cb/Cr 均不在肤色簇内 → 三特征严格为 0。
    """
    w, h = 256, 200
    rows: list[list[tuple[int, int, int]]] = []
    if index % 2 == 0:
        # 风景:冷色天空渐变、绿草地、深绿树影(避开一切暖黄色调)。
        horizon = int(h * 0.62) + 4 * (index % 3)
        trees = ((0.12, 10, 30), (0.38, 14, 40), (0.70, 8, 26))
        for y in range(h):
            row: list[tuple[int, int, int]] = []
            if y < horizon:
                t = y / max(1, horizon - 1)
                row = [  # (90,140,215) → (170,200,235)
                    (
                        int(90 + (170 - 90) * t),
                        int(140 + (200 - 140) * t),
                        int(215 + (235 - 215) * t),
                    )
                ] * w
            else:
                t = (y - horizon) / max(1, h - 1 - horizon)
                row = [  # (55,115,65) → (85,145,90)
                    (
                        int(55 + (85 - 55) * t),
                        int(115 + (145 - 115) * t),
                        int(65 + (90 - 65) * t),
                    )
                ] * w
            for tx, tw, th in trees:  # 从地平线向下延伸的深绿"树影"矩形
                x0 = int(tx * w) + 3 * (index % 4)
                y0 = horizon - th
                if y0 <= y and 0 <= x0 < w:
                    for x in range(max(0, x0), min(w, x0 + tw)):
                        row[x] = (30, 90, 40)
            rows.append(row)
    else:
        # 纯文本结构:白底 + 8 行伪文本黑块(乘法散列伪随机,确定性)。
        for y in range(h):
            row = [(255, 255, 255)] * w
            for j in range(8):
                y0 = int(h * (0.10 + 0.105 * j))
                if y0 <= y < y0 + 9:
                    for x in range(w):
                        if (((x * 2654435761) ^ (y * 40503)) & 0xFF) % 8 < 3:
                            row[x] = (20, 20, 20)
            rows.append(row)
    return w, h, rows


# ---------------------------------------------------------------------------
# 分类器本体
# ---------------------------------------------------------------------------


class SkinHeuristicClassifier(NsfwClassifier):
    """肤色启发式分类器(YCbCr 肤色占比 + 连通块 + 边缘密度,离线粗筛)。

    构造后每次 :meth:`classify` 为纯函数式的确定性计算(同图同分),
    并把最近一次 flood fill 的操作计数留在 ``last_flood_ops``(bench 用)。
    """

    name = "skin"

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg
        try:
            self.min_image_px = int(getattr(cfg, "min_image_px", _DEFAULT_MIN_PX))
        except (TypeError, ValueError):
            self.min_image_px = _DEFAULT_MIN_PX
        #: 最近一次成功 classify 的 flood fill 操作计数(8x8 格上界 64;-1=未算)。
        self.last_flood_ops: int = -1

    # -- 像素加载 -----------------------------------------------------------

    def _load_pixels(
        self, path: str
    ) -> tuple[int, int, list[list[tuple[int, int, int]]]]:
        """读图返回 ``(w, h, rows[y][x]=(r,g,b))``;优先 Pillow,退回 stdlib PNG。"""
        pil = _try_pil()
        if pil is not None:
            with pil.open(path) as im:
                rgb = im.convert("RGB")
                width, height = rgb.size
                buf = rgb.tobytes()  # 行主序 RGB 字节流(Pillow 各版本稳定 API)
            rows = [
                [
                    (
                        buf[(y * width + x) * 3],
                        buf[(y * width + x) * 3 + 1],
                        buf[(y * width + x) * 3 + 2],
                    )
                    for x in range(width)
                ]
                for y in range(height)
            ]
            return width, height, rows
        return read_png_rgb(path)

    # -- 特征 ---------------------------------------------------------------

    def _features(
        self, width: int, height: int, rows: list[list[tuple[int, int, int]]]
    ) -> dict[str, float]:
        """三特征计算(采样 → 肤色掩码 → 8x8 连通块 → 边缘密度)。"""
        step_x = max(1, width // SAMPLE_MAX)
        step_y = max(1, height // SAMPLE_MAX)
        n_cols = len(range(0, width, step_x))
        n_rows = len(range(0, height, step_y))

        skin_grid = [[False] * n_cols for _ in range(n_rows)]
        gray = [[0] * n_cols for _ in range(n_rows)]
        n_skin = 0
        total = 0
        for j, y in enumerate(range(0, height, step_y)):
            row = rows[y]
            for i, x in enumerate(range(0, width, step_x)):
                r, g, b = row[x]
                gray[j][i] = (r * 299 + g * 587 + b * 114) // 1000
                cb = 128.0 - 0.168736 * r - 0.331264 * g + 0.5 * b
                cr = 128.0 + 0.5 * r - 0.418688 * g - 0.081312 * b
                if CB_MIN <= cb <= CB_MAX and CR_MIN <= cr <= CR_MAX:
                    skin_grid[j][i] = True
                    n_skin += 1
                total += 1
        skin_ratio = n_skin / float(total)

        # 8x8 粗网格:格内肤色采样像素过半(>= 1/2)才算肤格。
        counts = [[0] * GRID for _ in range(GRID)]
        totals = [[0] * GRID for _ in range(GRID)]
        for j in range(n_rows):
            cj = j * GRID // n_rows
            for i in range(n_cols):
                ci = i * GRID // n_cols
                totals[cj][ci] += 1
                if skin_grid[j][i]:
                    counts[cj][ci] += 1
        cells = [
            [totals[cj][ci] > 0 and counts[cj][ci] * 2 >= totals[cj][ci] for ci in range(GRID)]
            for cj in range(GRID)
        ]
        max_blob, ops = _max_blob(cells)
        self.last_flood_ops = ops

        edges = 0
        for j in range(n_rows):
            for i in range(n_cols):
                v = gray[j][i]
                if i > 0 and abs(v - gray[j][i - 1]) > EDGE_THRESHOLD:
                    edges += 1
                elif j > 0 and abs(v - gray[j - 1][i]) > EDGE_THRESHOLD:
                    edges += 1
        edge_density = edges / float(total)

        return {
            "skin_ratio": skin_ratio,
            "max_blob": max_blob,
            "edge_density": edge_density,
        }

    # -- 契约入口 -----------------------------------------------------------

    def classify(self, img: ImageEvidence) -> ImageScore:
        """对单张图片打肤色启发式分;坏文件/缺解码器容错为 0 分 + error。"""
        try:
            width, height, rows = self._load_pixels(img.path)
            feats = self._features(width, height, rows)
            # 小图口径与 decision.verdict 一致:宽与高均低于 min_image_px 才算小图;
            # 照常计算打分,仅在 scores 标记 small=True。
            small = width < self.min_image_px and height < self.min_image_px
            prob = _combine(
                feats["skin_ratio"], feats["max_blob"], feats["edge_density"]
            )
        except Exception as exc:  # 坏文件/缺 Pillow 且非 PNG/截断 —— 一律容错
            telemetry.inc("skin.error")
            logger.warning("skin 启发式解码/计算失败:%s(%s)", img.path, exc)
            return ImageScore(
                image=img,
                model=self.name,
                scores={"error": str(exc)[:200]},
                nsfw_prob=0.0,
            )
        telemetry.inc("skin.classify")
        scores: dict[str, Any] = dict(feats)
        scores["small"] = small
        logger.debug(
            "skin 打分:path=%s prob=%.4f skin_ratio=%.4f max_blob=%.4f edge=%.4f",
            img.path,
            prob,
            feats["skin_ratio"],
            feats["max_blob"],
            feats["edge_density"],
        )
        return ImageScore(
            image=img,
            model=self.name,
            scores=scores,
            nsfw_prob=prob,
        )


# ---------------------------------------------------------------------------
# 自检(供 A138 kernel_bench 总控;确定性,无墙钟、无文件 IO)
# ---------------------------------------------------------------------------


def kernel_selfcheck() -> dict[str, Any]:
    """离线确定性自检:合成肤色图 vs 风景/文本图的 nsfw_prob 均值差。

    返回 ``{"name", "metric", "value", "baseline"}``,value 为两侧均值差
    (契约要求 > 0.2);两侧各 8 张内置合成图,零随机源、零文件 IO。
    """
    clf = SkinHeuristicClassifier()
    skin_probs: list[float] = []
    clean_probs: list[float] = []
    for k in range(8):
        w, h, rows = synthetic_skin_pixels(k)
        f = clf._features(w, h, rows)
        skin_probs.append(_combine(f["skin_ratio"], f["max_blob"], f["edge_density"]))
        w, h, rows = synthetic_clean_pixels(k)
        f = clf._features(w, h, rows)
        clean_probs.append(_combine(f["skin_ratio"], f["max_blob"], f["edge_density"]))
    gap = sum(skin_probs) / len(skin_probs) - sum(clean_probs) / len(clean_probs)
    return {
        "name": "skin",
        "metric": "synthetic_skin_vs_scenery_mean_gap",
        "value": round(gap, 4),
        "baseline": 0.2,
    }


if register_classifier is not None:  # 基座缺席时静默跳过(并行开发期)
    register_classifier("skin", SkinHeuristicClassifier)
