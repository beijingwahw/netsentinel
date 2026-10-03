"""指纹内核 v2:256bit 分块感知哈希(V7 · A134,见 CONTRACTS-V7.md §2 / 红线 29、31)。

A43(``netsentinel.intel.phash``)的 64bit pHash 对**整幅** 32x32 灰度做一次
全局 DCT、取左上 8x8 低频——空间信息被压平,近重复识别可靠但精细区分度
有限。本内核升级为**分块**指纹:

- 32x32 灰度 → 按 2x2 划成 4 个 16x16 象限(光栅序:左上 → 右上 →
  左下 → 右下)→ 每象限独立做 2D DCT-II(可分离余弦基,仅 stdlib
  ``math``)→ 取左上 8x8 低频系数,以"除 DC 外 63 个系数的中位数"为
  阈值(与 A43 同款口径)→ 每象限 64bit,共 **256bit**;
- :func:`phash256` 返回 64 位小写十六进制;:func:`hamming_hex` 同时支持
  256bit 与 64bit(A43)哈希的等长比较;:func:`to64` 把 256bit 映射回
  64bit 视图,便于接入既有 64bit 生态(``PhashRegistry`` / LSH)。

**口径修正说明**(docstring 明示,契约原文与 256bit 总长矛盾):契约 §2
A134 行写"每块取 4x4 低频 → 4×16bit"——4×16 = 64bit,与"256bit pHash"
自相矛盾;实施按修正口径:**4 块 × 每块 8x8 = 64 系数(阈值 = 除 DC 外
63 个系数的中位)→ 每块 64bit,共 256bit**。

``to64`` 与 A43 ``phash`` 的语义对齐(近似而非全等):两者都是"DCT 低频 +
除 DC 中位阈值"的 64bit 指纹,对缩放/亮度线性变化同样稳定,近重复图的
距离都小;但 A43 在整幅 32x32 上做一次全局 DCT,``to64`` 取的是四个 16x16
象限 DCT 各自**最高 16bit**(即每象限 8x8 系数的前两行 u=0,1,共 16 个
系数)的拼接——数学上不是同一变换,故 ``to64(x) == phash(x)`` 并不成立,
两者距离只是强相关:用于"粗筛同 64bit 生态"时安全,**不可**当作逐位相等
的替代品。

亮度不变性:象限内整体亮度线性缩放时该象限全部系数同比例缩放、符号不变,
除 DC 中位阈值随之缩放,比较结果不变——这正是近重复识别想要的不变性
(与 A43 同理)。分块的代价是象限边界重采样可能有 1~2bit 抖动,换来的是
"能量在画面哪个位置"的空间布局信息(A43 全局哈希完全丢弃)。

红线遵守(V7 §0):

- **红线 29(零 API 破坏)**:纯新增文件,不改 A43 ``intel/phash.py``
  (只读参考其 DCT 基表思路)、不注册任何名字、旧调用方零感知;
- **红线 31(基准可复现)**:``tests/test_phash2.py`` 的
  ``test_v7_bench_separability_margin`` 以确定性构造图(≥8 组同图变换 +
  ≥8 组异构图)断言**同图最大汉明距离 < 异图最小汉明距离**,不依赖墙钟;
  :func:`kernel_selfcheck` 另以零 IO 的解析合成图复算同一裕量,供
  A138 kernel_bench 总控调用。

Pillow 惰性加载:缺失/损坏时 :func:`phash256` 直接抛中文 ValueError
(V2 内核不做 aHash 降级——256bit 口径无降级等价物);图片文件不存在 /
无法解码同样抛中文 ValueError。计算耗时记
``telemetry.timer("phash2.compute")``(与 A43 的 ``phash.compute``
计数体系并存,互不干扰)。

**指纹盲区修复(V12 · A219,红队基准 A218 实测点名:镜像翻转与中心裁剪
全档零召回、旋转 3° 即破 256bit——p256@32=0.02)**:在既有
phash256/64bit 口径**零改动**的前提下,本模块新增三个互补的不变性指纹
(全部纯 PIL + stdlib,位宽/语义与旧哈希并存互不影响):

- :func:`mirror_hash`(64bit):镜像不变规范形——图与其水平/垂直翻转
  (含 180° 复合,即翻转群完整轨道)的全局 64bit DCT 哈希取字典序最小
  规范形。flip 召回 0 → 1.0 的机理:翻转只改变哈希,不改变轨道,
  规范形在轨道上严格恒定;
- :func:`ring_hash`(32bit):旋转不变——中心方形裁剪(去掉旋转白角)
  → 极坐标采样(PIL MESH+BILINEAR 重采样,过采样后 BOX 归并,角度桶
  32 × 半径带 4)→ 各半径带一维 DCT 能量签名(旋转 ≈ 角向循环平移,
  低频能量近似不变);
- :func:`pyramid_hash`(108bit)与 :func:`pyramid_distance`:多尺度
  32/16/8 三层低频 DCT 串联,查询语义取三层汉明最小值(抗缩放式
  裁剪/zoom 的机理:zoom 后内容的能量分布在某一尺度的低频三角上最
  接近原图)。裁剪召回是**部分恢复而非全档 1.0**(诚实量化见
  ``tests/test_phash_invariant.py``)。

**原理性边界深化(V14 · A228,针对 A219 交付报告点名的两项边界)**:
A219 实测点名 ① ring 的 DCT 能量签名只在 ~1.3 桶角向平移内近似不变(±15°
召回 0.19);② 照片族构图裁剪全档低频失配(裁剪仅部分恢复)。本模块在
既有五哈希**零改动**的前提下新增两个互补指纹(纯 PIL + stdlib):

- :func:`dft_ring_hash`(32bit):复用 ring 的极坐标采样管线,但沿角度轴
  取 **DFT 复数模长**(k=1..8)而非 DCT 能量——角向平移(=旋转)在 DFT
  模上**严格**不变(循环移位定理,证明概要见 docstring),±15° 实测召回
  1.000(ring 0.188);角向重采样伪影用幅度比退化保护置零(径向构图);
- :func:`tile_hash`(768bit = 12 tile × 64bit)与 :func:`tile_any_match`:
  重叠子窗口指纹——全图 + 3×3 重叠半图 tile(4×4 网格上的 2×2 滑窗,
  50% 重叠)+ 中心裁剪尺度梯两级(1/√2、1/(2√2))。查询语义 = 任一
  tile 对命中即候选(12×12 对取最小汉明距离);对抗中心裁剪的机理:
  裁剪掉的只是边缘 tile,中心内容在尺度梯的某一级上以相近尺度保留。
  tile 位宽取 64bit 而非任务草案的 8bit:实测 8/15bit tile 在 144 对
  任一命中语义下异图最小距塌缩到 0(易撞),64bit 时异图最小距 12、
  工作距 8 留双向裕量(权衡实测见 ``tests/test_phash_invariant.py``)。

本模块无任何网络行为(红线 12:哈希与图片数据仅本地处理)。

用法示例::

    from netsentinel.vision.phash2 import phash256, hamming_hex, to64

    h1 = phash256("data/pic_a.png")       # 64 位 hex(256bit)
    h2 = phash256("data/pic_a_small.png") # 同图缩放版
    hamming_hex(h1, h2)                   # 小(近重复)
    to64(h1)                              # 16 位 hex 视图,接入 64bit 生态
"""
from __future__ import annotations

import importlib
import logging
import math
import statistics
from pathlib import Path
from typing import Any

from netsentinel import telemetry

__all__ = [
    "DFT_RING_SUGGESTED_MAX_DISTANCE",
    "MIRROR_SUGGESTED_MAX_DISTANCE",
    "PYRAMID_SUGGESTED_MAX_DISTANCE",
    "RING_SUGGESTED_MAX_DISTANCE",
    "TILE_SUGGESTED_MAX_DISTANCE",
    "dft_ring_hash",
    "hamming_hex",
    "kernel_selfcheck",
    "mirror_hash",
    "phash256",
    "pyramid_distance",
    "pyramid_hash",
    "ring_hash",
    "tile_any_match",
    "tile_hash",
    "to64",
]

logger = logging.getLogger(__name__)

_GRID = 32        # 整图统一缩放尺寸(与 A43 一致)
_QUAD = 16        # 象限边长(_GRID 一分为二)
_LOW = 8          # 每象限 DCT 只取左上 8x8 低频系数
_QUADS = 4        # 2x2 分块 → 4 象限
_HEX256_LEN = 64  # 256bit → 64 个十六进制字符
_HEX64_LEN = 16   # to64 视图 → 16 个十六进制字符
_HEX_DIGITS = frozenset("0123456789abcdef")

#: 预计算余弦基表(与 A43 同思路,规模改为象限 16):``_COS_TABLE[u][x] =
#: cos((2x+1)·u·π / (2·16))``,u ∈ [0, 8),x ∈ [0, 16)。2D DCT 可分离,
#: 行/列两个方向共用该表。
_COS_TABLE: tuple[tuple[float, ...], ...] = tuple(
    tuple(
        math.cos((2 * x + 1) * u * math.pi / (2 * _QUAD))
        for x in range(_QUAD)
    )
    for u in range(_LOW)
)

#: 三个不变性指纹(A219)的位宽与工作距常量(召回/误报的实测依据见
#: tests/test_phash_invariant.py 的矩阵断言与 docstring):
_MIRROR_HEX_LEN = 16    # mirror_hash:64bit → 16 hex
_RING_HEX_LEN = 8       # ring_hash:32bit → 8 hex
_RING_ANGLES = 32       # 极坐标角度桶数
_RING_BANDS = 4         # 极坐标半径带数
_RING_OVER_ANGLES = 128  # 极坐标过采样角度(MESH 细分,抗走样)
_RING_OVER_RADII = 32   # 极坐标过采样半径
_RING_R_OUT = 0.45      # 采样外径(相对中心方形边长;内缩避开旋转白角)
_RING_R_IN = 0.10       # 采样内径(避开圆心角度退化区)
_PYRAMID_LEVELS = (32, 16, 8)  # 多尺度三层(网格边长)
_PYRAMID_K = 6          # 每层取 k×k 低频三角系数(36bit/层)
_PYRAMID_HEX_LEN = 27   # 108bit(3×36)→ 27 hex(每层 9 hex)

#: A228 两个新指纹的位宽与几何常量(实测依据见 tests/test_phash_invariant.py):
_DFT_RING_K = 8         # dft_ring:每半径带取的角向 DFT 模长个数(k=1..8)
_DFT_RING_HEX_LEN = 8   # dft_ring:32bit(4 带 × 8bit)→ 8 hex,与 ring 同位宽
_DFT_DEGEN_RATIO = 0.06  # 角向退化保护:带内最大交流幅度 / |直流| ≤ 该值的带视为
#                          “角向无结构”整体置零。标定:极坐标重采样伪影在角向
#                          均匀内容上注入 ≤3.9% 的虚假角向结构(rings_broad 实测
#                          3.3%/3.9%/0.4%/0.2%),真实角向结构 ≥11.9%(cloud,
#                          语料最弱),6% 居中留双向裕量。
_TILE_CANON = 128       # tile:统一缩放的规范边长(4×4 网格 → 每格 32px)
_TILE_GRID_OFFSETS = (0, 1, 2)  # S2 滑窗偏移(单位 = 1 格):4×4 网格上 2×2 滑窗
#                                # 共 3×3 = 9 个重叠 tile,相邻重叠 50%
_TILE_LADDER = (2 ** -0.5, 2 ** -1.5)  # 中心裁剪尺度梯两级(≈0.707/0.354,√2 步进:
#                                       # 任意中心裁剪边长 ∈ [0.354, 1] 与某级相差 ≤×1.19)
_TILE_K = 8             # 每 tile 8x8 低频 DCT → 64bit(位宽权衡实测见 tile_hash docstring)
_TILE_COUNT = 12        # 1 全图 + 9 重叠 S2 + 2 中心梯级
_TILE_HEX_LEN = 192     # 12 × 64bit → 192 hex(每 tile 段 16 hex)
_TILE_SEG_HEX = 16      # 每 tile 段的 hex 字符数

#: mirror_hash 建议工作距(64bit 域):实测翻转距离恒 0,异构图(不同
#: 构图族)最小距 ≥ 20(合成语料)/ 16(红队语料异构对),12 居中留裕量。
MIRROR_SUGGESTED_MAX_DISTANCE = 12

#: ring_hash 建议工作距(32bit 域):实测 ±3° 距离 ≤ 6、±8° 距离 ≤ 10,
#: 异构图(角向有信息的构图)最小距 ≥ 8,6 为零误报上沿(±3/±8 合并
#: 召回 0.78,阈值 8 会把个别异构对拉进候选)。
RING_SUGGESTED_MAX_DISTANCE = 6

#: pyramid_distance 建议工作距(36bit 层域):实测中心裁剪 10%~30% 距离
#: 2~18,异构图最小距 ≥ 12(双语料一致),11 为零误报上沿。
PYRAMID_SUGGESTED_MAX_DISTANCE = 11

#: dft_ring_hash 建议工作距(32bit 域):实测 ±3/±8/±15° 全档距离 ≤ 2,
#: 良性 JPEG/缩放最大距 2,异构图(角向有信息的 7 构图,21 对)最小距 4
#: ——3 为零误报上沿(photo2×cross_wavy / cloud×quad 等平滑构图对在 4bit
#: 相邻,勿放宽)。
DFT_RING_SUGGESTED_MAX_DISTANCE = 3

#: tile_any_match 建议工作距(64bit tile 域):实测中心裁剪 10%/20%/30%
#: 召回 0.375/0.750/0.875(距离 0~31),良性 JPEG/缩放最大距 6,异构图
#: (8 构图 28 对)最小距 12——8 在两侧各留 ≥2bit 裕量;放宽到 12 即触碰
#: 异图对,勿放大使用。
TILE_SUGGESTED_MAX_DISTANCE = 8

#: 余弦基表缓存(新哈希用,按 (n, k) 记忆化;旧 ``_COS_TABLE`` 不变)。
_COS_TABLE_CACHE: dict[tuple[int, int], tuple[tuple[float, ...], ...]] = {}

#: 单色图退化保护(A219 新哈希):常量矩阵的 DCT 交流系数在浮点上是
#: ~1e-10 级残差(数学应为 0),直接过中位阈值会让"噪声符号"决定哈希
#: 位。相对峰值(1e-9·max)以下的系数先归零,单色图即得 docstring 所述
#: 的确定退化值;真实图像的系数量级 ~1e+2,不受影响。
_DEGEN_EPS = 1e-9


# ---------------------------------------------------------------------------
# Pillow 惰性加载(与 A43 intel/phash.py 同款策略)
# ---------------------------------------------------------------------------


def _load_pil():
    """惰性导入 ``PIL.Image``;缺失 / 半初始化一律返回 None。

    先导入父包 ``PIL`` 再导入 ``PIL.Image``:测试以 ``sys.modules["PIL"] = None``
    屏蔽 Pillow 时,若只导 ``PIL.Image`` 会命中缓存中的子模块而漏检,
    先导父包才能可靠感知"不可用"。
    """
    try:
        importlib.import_module("PIL")
        return importlib.import_module("PIL.Image")
    except Exception:  # None 注入 / 未安装 / 损坏的 PIL 均按"未安装"处理
        return None


def _normalize_hex(value: object, what: str) -> str:
    """校验并归一化十六进制哈希串(去空白、转小写);非法抛中文 ValueError。"""
    h = str(value or "").strip().lower()
    if not h or any(ch not in _HEX_DIGITS for ch in h):
        raise ValueError(f"非法{what}(需为非空十六进制字符串):{value!r}")
    return h


# ---------------------------------------------------------------------------
# 像素读取与 256bit 哈希计算
# ---------------------------------------------------------------------------


def _open_gray_rows(path: Path, size: int) -> list[list[float]]:
    """打开图片 → 灰度("L")→ 缩放到 size×size,返回逐行像素值(0~255)。

    未安装 Pillow、文件损坏 / 非图片内容时抛中文 ValueError(与 A43 同款
    容错面;V2 无 aHash 降级路径)。
    """
    pil_image = _load_pil()
    if pil_image is None:
        raise ValueError(
            "未安装 Pillow,无法解码图片计算 256bit 指纹(pip install Pillow 后启用)"
        )
    try:
        with pil_image.open(path) as im:
            resampling = getattr(pil_image, "Resampling", pil_image)
            gray = im.convert("L").resize((size, size), resampling.LANCZOS)
            # Pillow 12 起 getdata 弃用(14 移除),优先用 get_flattened_data
            getter = getattr(gray, "get_flattened_data", None) or gray.getdata
            data = list(getter())
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法解码图片文件:{path}({exc})") from exc
    return [
        [float(v) for v in data[row * size:(row + 1) * size]]
        for row in range(size)
    ]


def _quad_hash_bits(quad: list[list[float]]) -> int:
    """对一个 16x16 象限做 2D DCT-II,取左上 8x8 低频生成 64bit。

    变换可分离,分两趟完成(规模 16x16,双重循环开销可忽略):

    1. 行方向 ``G[y][v] = Σ_x p[y][x]·cos_v[x]``(v < 8);
    2. 列方向 ``F[u][v] = Σ_y G[y][v]·cos_u[y]``(u, v < 8)。

    阈值取 8x8 系数中"除 DC(左上角 [0][0])外 63 个"的中位数:象限整体
    亮度线性缩放时全部 AC 系数同比例缩放、符号不变,哈希几乎不动。位序:
    flat[0](DC)为该象限最高位,输出 64bit 整数。
    """
    g = [
        [sum(px * cx for px, cx in zip(quad[y], _COS_TABLE[v])) for v in range(_LOW)]
        for y in range(_QUAD)
    ]
    coeffs = [
        [sum(g[y][v] * cu_y for y, cu_y in enumerate(_COS_TABLE[u])) for v in range(_LOW)]
        for u in range(_LOW)
    ]
    flat = [coeffs[u][v] for u in range(_LOW) for v in range(_LOW)]
    median = statistics.median(flat[1:])  # 除 DC 外的中位阈值
    bits = 0
    for i, value in enumerate(flat):
        if value > median:
            bits |= 1 << (63 - i)
    return bits


def _dct256_hash(rows: list[list[float]]) -> str:
    """32x32 灰度矩阵 → 4 象限 × 64bit → 64 位小写十六进制。

    象限按光栅序拼接:左上(16 hex)→ 右上 → 左下 → 右下,故哈希的
    第 ``b`` 段 16 个字符恰对应第 ``b`` 个象限(象限结构可从哈希直接
    切片观察,``to64`` 也据此取每段最高 16bit)。
    """
    half = _GRID // 2
    parts: list[str] = []
    for by in (0, half):
        for bx in (0, half):
            quad = [row[bx:bx + half] for row in rows[by:by + half]]
            parts.append(f"{_quad_hash_bits(quad):016x}")
    return "".join(parts)


def phash256(path: str | Path) -> str:
    """计算图片 256bit 分块感知哈希,返回 64 位小写十六进制。

    流程:灰度 → 32x32 → 4 个 16x16 象限 → 各自 2D DCT 左上 8x8(除 DC
    中位阈值)→ 每象限 64bit × 4 = 256bit。对缩放 / 轻微亮度变化 / 重编码
    保持稳定,且保留"能量在四象限的空间布局",区分度高于 A43 的全局
    64bit 哈希。未安装 Pillow(本内核**无降级路径**)、文件不存在 / 损坏
    抛中文 ValueError。计算耗时记 ``telemetry.timer("phash2.compute")``。
    """
    src = Path(path)
    if not src.is_file():
        raise ValueError(f"图片文件不存在:{src}")
    with telemetry.timer("phash2.compute"):
        return _dct256_hash(_open_gray_rows(src, _GRID))


# ---------------------------------------------------------------------------
# 距离与 64bit 视图
# ---------------------------------------------------------------------------


def hamming_hex(a: str, b: str) -> int:
    """两个十六进制哈希的汉明距离(异或后置位计数)。

    同时支持 256bit(64 hex,本内核)与 64bit(16 hex,A43 ``phash``)
    哈希——**两侧长度必须相等**,长度不等或内容非法十六进制抛中文
    ValueError;大小写与首尾空白容忍(与 A43 ``hamming`` 同款容错面,
    便于 64bit 生态两侧互换使用)。
    """
    ha = _normalize_hex(a, "哈希 a")
    hb = _normalize_hex(b, "哈希 b")
    if len(ha) != len(hb):
        raise ValueError(
            f"哈希长度不一致,无法比较:{len(ha)} 位与 {len(hb)} 位"
        )
    return (int(ha, 16) ^ int(hb, 16)).bit_count()


def to64(hex256: str) -> str:
    """256bit 哈希 → 64bit 视图:取每象限段最高 16bit 拼接,返回 16 位 hex。

    切片口径:256bit 哈希按象限等分为 4 段各 16 个 hex 字符,``to64`` 取
    每段**前 4 个**字符(即该象限 64bit 的最高 16bit——按位序恰是 8x8
    系数中 u=0,1 两行共 16 个最低竖向频率系数),拼接成 64bit。

    与 A43 ``intel.phash.phash`` 的语义对齐(近似而非全等):同为"DCT
    低频 + 除 DC 中位阈值"的 64bit 指纹、近重复距离同样小,但 A43 是
    整幅 32x32 的全局 DCT,本函数是四个象限 DCT 的低频切片拼接,数学上
    不是同一变换——``to64(x) == phash(x)`` 一般不成立,只能当作强相关的
    **近似视图**(用于接入 64bit 生态的粗筛,不作逐位等价替换)。

    入参须恰为 64 位十六进制,否则抛中文 ValueError。
    """
    h = _normalize_hex(hex256, "256bit 哈希")
    if len(h) != _HEX256_LEN:
        raise ValueError(
            f"to64 需要 64 位十六进制(256bit)哈希,实际收到 {len(h)} 位:{hex256!r}"
        )
    seg = _HEX256_LEN // _QUADS  # 每象限段 16 个 hex 字符
    top = 4                      # 最高 16bit = 4 个 hex 字符
    return "".join(h[b * seg:b * seg + top] for b in range(_QUADS))


# ---------------------------------------------------------------------------
# A219 不变性指纹之一:mirror_hash(镜像不变规范形,64bit)
# ---------------------------------------------------------------------------


def _cos_table(n: int, k: int) -> tuple[tuple[float, ...], ...]:
    """取(或预计算并缓存)n 点 DCT 的前 k 个余弦基:``table[u][x] =
    cos((2x+1)·u·π / (2n))``。与旧 :data:`_COS_TABLE` 同思路,规模按需
    生成、按 ``(n, k)`` 记忆化,全程确定性。
    """
    key = (n, k)
    table = _COS_TABLE_CACHE.get(key)
    if table is None:
        table = tuple(
            tuple(math.cos((2 * x + 1) * u * math.pi / (2 * n)) for x in range(n))
            for u in range(k)
        )
        _COS_TABLE_CACHE[key] = table
    return table


def _dct_lowfreq_bits(rows: list[list[float]], k: int) -> int:
    """对 n×n 灰度矩阵做 2D DCT-II,取左上 k×k 低频三角生成 k² bit。

    与 :func:`netsentinel.intel.phash._dct_hash` 同款口径(可分离两趟 DCT +
    "除 DC 外中位阈值",亮度线性缩放不变),但三角规模 k 为参数:旧 64bit
    哈希即 ``k=8`` 特例;pyramid 层取 ``k=6``(只保留最低频 36 系数,对
    zoom 类裁剪最稳)。位序:flat[0](DC)为最高位,输出 k² bit 整数。
    """
    n = len(rows)
    table = _cos_table(n, k)
    g = [
        [sum(px * cx for px, cx in zip(rows[y], table[v])) for v in range(k)]
        for y in range(n)
    ]
    coeffs = [
        [sum(g[y][v] * cu for y, cu in enumerate(table[u])) for v in range(k)]
        for u in range(k)
    ]
    flat = [coeffs[u][v] for u in range(k) for v in range(k)]
    # 单色图退化保护:相对峰值以下的浮点残差归零(见 _DEGEN_EPS 注释),
    # 使常量矩阵的哈希取确定的退化值而非浮点噪声位。
    peak = max(abs(value) for value in flat)
    flat = [0.0 if abs(value) <= _DEGEN_EPS * peak else value for value in flat]
    median = statistics.median(flat[1:])  # 除 DC 外的中位阈值
    bits = 0
    for i, value in enumerate(flat):
        if value > median:
            bits |= 1 << (k * k - 1 - i)
    return bits


def _mirror_bits(rows: list[list[float]]) -> int:
    """翻转群轨道规范形:{原图, 水平翻, 垂直翻, 180°}四个 64bit 哈希取最小。

    任务口径为"图与其水平/垂直翻转三者",但 {恒等, H, V} 对翻转不封闭
    (H∘V = 180° 旋转不在集合内,min 会随代表元漂移),必须补上复合元
    构成 Klein 四元群的完整轨道,规范形才在轨道上**严格**恒定——
    ``mirror(任意翻转版) == mirror(原图)`` 逐位成立(flip 召回 = 1.0
    的数学保证)。等长小写 hex 的字典序与整数值序一致,取整型 min 即
    取字典序最小规范形。
    """
    hflip = [row[::-1] for row in rows]
    vflip = rows[::-1]
    both = [row[::-1] for row in rows[::-1]]
    return min(_dct_lowfreq_bits(m, _LOW) for m in (rows, hflip, vflip, both))


def mirror_hash(path: str | Path) -> str:
    """计算图片**镜像不变**感知哈希(64bit 规范形),返回 16 位小写 hex。

    流程:灰度 → 32x32 → 全局 DCT 64bit(与 A43 ``phash`` 同款算法,自
    包实现、不跨模块依赖)→ 对 {原图, 水平翻转, 垂直翻转, 180°} 四个
    轨道元的哈希取最小规范形。对水平/垂直镜像**严格不变**(距离恒 0),
    对缩放 / 亮度线性变化 / 重编码与 A43 同等稳定;代价是规范形丢弃了
    "原图是否翻转"这一信息(异图若互为翻转关系则不可分,属设计取舍)。

    退化语义:单色图 DCT 交流系数全零,任意灰度 >0 的单色图哈希恒为
    ``8000000000000000``(全零灰度恒为全 0),不同灰度的单色图之间距离
    恒 0——无结构可辨,入库场景应改用字节 sha256 去重。未安装 Pillow、
    文件不存在 / 损坏抛中文 ValueError(与 :func:`phash256` 同款容错面);
    计算耗时记 ``telemetry.timer("phash2.mirror_compute")``。
    """
    src = Path(path)
    if not src.is_file():
        raise ValueError(f"图片文件不存在:{src}")
    with telemetry.timer("phash2.mirror_compute"):
        return f"{_mirror_bits(_open_gray_rows(src, _GRID)):016x}"


# ---------------------------------------------------------------------------
# A219 不变性指纹之二:ring_hash(旋转不变,32bit)
# ---------------------------------------------------------------------------


def _polar_band_rows(src: Path) -> list[list[float]]:
    """中心方形裁剪 → 极坐标采样 → 4 个半径带 × 32 角度桶的均值矩阵。

    采样管线(全部 PIL 重采样,确定性):

    1. 灰度 → 取中心 min(w,h)×min(h,w) 方形(旋转攻击的白边填充集中在
       四角,方形内裁即天然规避大部分;内缩采样外径进一步留边);
    2. MESH 逆映射把 128 角 × 32 半径的极坐标网格逐 cell 映回源图四边形
       (BILINEAR 重采样,cell 级面积覆盖,抗走样);
    3. BOX 归并到 32 角 × 4 带(每 cell = 过采样 4×8 块的真实面积均值,
       行 0 = 最外带)。旋转 θ ≈ 角向信号循环平移 θ/360°·32 桶。
    """
    pil_image = _load_pil()
    if pil_image is None:
        raise ValueError(
            "未安装 Pillow,无法解码图片计算旋转不变指纹(pip install Pillow 后启用)"
        )
    try:
        with pil_image.open(src) as im:
            gray = im.convert("L")
            width, height = gray.size
            side = min(width, height)
            x0, y0 = (width - side) // 2, (height - side) // 2
            square = gray.crop((x0, y0, x0 + side, y0 + side))
            cx = cy = side / 2.0
            r_out, r_in = side * _RING_R_OUT, side * _RING_R_IN
            na, nr = _RING_OVER_ANGLES, _RING_OVER_RADII
            mesh: list[tuple[tuple[int, int, int, int], tuple[float, ...]]] = []
            for j in range(nr):  # 行 0 = 最外带,半径向圆心线性递减
                r1 = r_out - (r_out - r_in) * (j / nr)
                r2 = r_out - (r_out - r_in) * ((j + 1) / nr)
                for i in range(na):
                    a1 = 2 * math.pi * (i / na)
                    a2 = 2 * math.pi * ((i + 1) / na)
                    mesh.append(
                        (
                            (i, j, i + 1, j + 1),
                            (
                                cx + r1 * math.cos(a1), cy + r1 * math.sin(a1),
                                cx + r1 * math.cos(a2), cy + r1 * math.sin(a2),
                                cx + r2 * math.cos(a2), cy + r2 * math.sin(a2),
                                cx + r2 * math.cos(a1), cy + r2 * math.sin(a1),
                            ),
                        )
                    )
            resampling = getattr(pil_image, "Resampling", pil_image)
            transform = getattr(pil_image, "Transform", pil_image)
            polar = square.transform(
                (na, nr),
                transform.MESH,
                mesh,
                resample=resampling.BILINEAR,
            )
            small = polar.resize(
                (_RING_ANGLES, _RING_BANDS), resample=resampling.BOX
            )
            getter = getattr(small, "get_flattened_data", None) or small.getdata
            data = list(getter())
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法解码图片文件:{src}({exc})") from exc
    return [
        [float(v) for v in data[row * _RING_ANGLES:(row + 1) * _RING_ANGLES]]
        for row in range(_RING_BANDS)
    ]


def _ring_bits_from_bands(bands: list[list[float]]) -> int:
    """4×32 角向矩阵 → 32bit:各带一维 DCT 能量(k=1..8)中位阈值签名。

    每半径带取 32 点角向轮廓的一维 DCT-II,交流能量 ``E_k = C_k²``
    (k=1..8 共 8 个,直流 k=0 是带均值、对旋转平凡不变但无角向信息,
    不参与);阈值 = 同 8 个能量的中位数 → 每带恰 8bit,4 带共 32bit。
    旋转把轮廓平移 Δ 桶(3° ≈ 0.27 桶),DCT 低频能量对小幅平移近似
    不变(严格不变需 DFT 模,任务口径为 DCT 近似)——这是 ±3°/±8°
    召回的机理,也是 ±15°(1.3 桶)开始失守的边界。
    """
    bits = 0
    for band_index, band in enumerate(bands):
        table = _cos_table(len(band), 9)
        coeffs = [sum(p * c for p, c in zip(band, table[k])) for k in range(9)]
        # 单色图退化保护(见 _DEGEN_EPS):角向常量带的交流系数是浮点
        # 残差级,须以**直流幅度**(带均值 × 点数)为参照判退化——以
        # 交流自身峰值为参照会被噪声的相对起伏骗过(残差间也互有大小)。
        if max(abs(c) for c in coeffs[1:]) <= _DEGEN_EPS * abs(coeffs[0]):
            continue  # 角向常量带:能量全零,该带 8bit 全 0
        energies = [c * c for c in coeffs[1:]]
        median = statistics.median(energies)
        for i, energy in enumerate(energies):
            if energy > median:
                bits |= 1 << (31 - (band_index * 8 + i))
    return bits


def ring_hash(path: str | Path) -> str:
    """计算图片**旋转不变**感知哈希(32bit),返回 8 位小写 hex。

    流程:中心方形裁剪 → 极坐标采样(PIL 重采样,32 角度桶 × 4 半径带)
    → 各带 DCT 能量签名。对 ±3°/±8° 旋转实测召回 ≥ 0.75(合成语料
    0.84 / 红队语料 0.97,``tests/test_phash_invariant.py``),对缩放 /
    重编码 / 噪声稳定;镜像水平翻会改变角向轮廓方向(能量近守恒,召回
    部分),垂直翻则严格不变——镜像场景由 :func:`mirror_hash` 负责。

    退化语义:单色图 / 角向无结构图(辐射对称、纯径向渐变)的各带能量
    全零,哈希恒为 ``00000000``——旋转不变指纹**数学上不可能**区分互为
    旋转等价的构图,此类图不要用 ring 单独判定(与主指纹联用)。未安装
    Pillow、文件不存在 / 损坏抛中文 ValueError;计算耗时记
    ``telemetry.timer("phash2.ring_compute")``。
    """
    src = Path(path)
    if not src.is_file():
        raise ValueError(f"图片文件不存在:{src}")
    with telemetry.timer("phash2.ring_compute"):
        return f"{_ring_bits_from_bands(_polar_band_rows(src)):08x}"


# ---------------------------------------------------------------------------
# A219 不变性指纹之三:pyramid_hash(多尺度,108bit)+ pyramid_distance
# ---------------------------------------------------------------------------


def _pyramid_level_bits(src: Path) -> list[int]:
    """一次解码,按 32/16/8 三层 LANCZOS 缩放各取 k×k 低频三角(36bit/层)。

    抗缩放式裁剪/zoom 的机理:中心裁剪 + 回填放大 = zoom,内容的能量
    分布在频率域整体搬迁;三层尺度下,总有一层的低频三角与原图某层的
    低频三角"最接近"(配合 :func:`pyramid_distance` 的三层取 min 查询
    语义)。注意这是**部分恢复**:结构主导的照片裁 10% 面积后三层都不
    再对齐(实测召回 ~0.4 档位),纹理主导图恢复更好——诚实数字见
    ``tests/test_phash_invariant.py``。
    """
    pil_image = _load_pil()
    if pil_image is None:
        raise ValueError(
            "未安装 Pillow,无法解码图片计算多尺度指纹(pip install Pillow 后启用)"
        )
    try:
        with pil_image.open(src) as im:
            gray = im.convert("L")
            resampling = getattr(pil_image, "Resampling", pil_image)
            levels: list[int] = []
            for size in _PYRAMID_LEVELS:
                small = gray.resize((size, size), resample=resampling.LANCZOS)
                getter = getattr(small, "get_flattened_data", None) or small.getdata
                data = list(getter())
                rows = [
                    [float(v) for v in data[row * size:(row + 1) * size]]
                    for row in range(size)
                ]
                levels.append(_dct_lowfreq_bits(rows, _PYRAMID_K))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法解码图片文件:{src}({exc})") from exc
    return levels


def pyramid_hash(path: str | Path) -> str:
    """计算图片**多尺度**感知哈希(108bit = 3 层 × 36bit),返回 27 位 hex。

    流程:灰度 → 32/16/8 三层 LANCZOS 缩放 → 各层 6x6 低频 DCT 三角
    (除 DC 中位阈值,亮度线性缩放不变)→ 三层 36bit 串联(每层 9 个
    hex,粗层在尾部,切片口径与 :func:`pyramid_distance` 一致)。对缩
    放 / 重编码 / 噪声稳定,对中心裁剪(zoom)部分恢复,对旋转 ±8° 内
    辅助召回;查询必须用 :func:`pyramid_distance`(三层取 min),不要
    用全长汉明(会退化为最细层单尺度)。

    退化语义:与 :func:`mirror_hash` 同理,单色图各层交流系数全零,
    灰度 >0 的单色图哈希恒为 ``800000000000000...0``(每层最高位为 1),
    全零灰度恒为全 0。未安装 Pillow、文件不存在 / 损坏抛中文
    ValueError;计算耗时记 ``telemetry.timer("phash2.pyramid_compute")``。
    """
    src = Path(path)
    if not src.is_file():
        raise ValueError(f"图片文件不存在:{src}")
    with telemetry.timer("phash2.pyramid_compute"):
        return "".join(f"{bits:09x}" for bits in _pyramid_level_bits(src))


def pyramid_distance(a: str, b: str) -> int:
    """两个 108bit 多尺度哈希的查询距离:**三层汉明取最小**(0~36)。

    入参须各为 27 位十六进制(每层 9 hex = 36bit),非法长度 / 非法
    十六进制抛中文 ValueError(大小写与首尾空白容忍)。距离语义:zoom /
    裁剪攻击下"哪一层最像"事先未知,取三层最小值作为查询距离——代价
    是随机异图的期望距离从 54/108 降到约 27+ ,工作距
    :data:`PYRAMID_SUGGESTED_MAX_DISTANCE`(11)按实测异图最小距 12
    留零误报上沿校准,不要放大使用。
    """
    ha = _normalize_hex(a, "金字塔哈希 a")
    hb = _normalize_hex(b, "金字塔哈希 b")
    if len(ha) != _PYRAMID_HEX_LEN or len(hb) != _PYRAMID_HEX_LEN:
        raise ValueError(
            f"pyramid_distance 需要 {_PYRAMID_HEX_LEN} 位十六进制(108bit)哈希,"
            f"实际收到 {len(ha)} 位与 {len(hb)} 位"
        )
    seg = _PYRAMID_HEX_LEN // len(_PYRAMID_LEVELS)  # 每层 9 hex
    return min(
        hamming_hex(ha[i * seg:(i + 1) * seg], hb[i * seg:(i + 1) * seg])
        for i in range(len(_PYRAMID_LEVELS))
    )


# ---------------------------------------------------------------------------
# A228 不变性指纹之四:dft_ring_hash(严格旋转不变,32bit)
# ---------------------------------------------------------------------------

#: 角向 DFT 旋转因子表(模块加载时确定性预计算,规模 8×32 开销可忽略):
#: ``_DFT_TWIDDLE[k] = (cos_table, sin_table)``,``cos_table[i] = cos(2πki/N)``,
#: ``sin_table[i] = sin(2πki/N)``,N = :data:`_RING_ANGLES`。实信号 DFT 的
#: 复数系数按 ``X_k = Σ x_i·cos − i·Σ x_i·sin`` 展开,模长平方即两分量平方和。
_DFT_TWIDDLE: dict[int, tuple[tuple[float, ...], tuple[float, ...]]] = {
    k: (
        tuple(
            math.cos(2 * math.pi * k * i / _RING_ANGLES)
            for i in range(_RING_ANGLES)
        ),
        tuple(
            math.sin(2 * math.pi * k * i / _RING_ANGLES)
            for i in range(_RING_ANGLES)
        ),
    )
    for k in range(1, _DFT_RING_K + 1)
}


def _dft_ring_bits_from_bands(bands: list[list[float]]) -> int:
    """4×32 角向矩阵 → 32bit:各带 DFT 复数模长平方(k=1..8)中位阈值签名。

    每半径带对 32 点角向轮廓做一维 DFT,取 k=1..8 的模长平方 ``|X_k|²``
    (直流 k=0 是带均值、对旋转平凡不变但无角向信息,不参与;k>8 靠近
    Nyquist 的分量对重采样误差最敏感,弃用)。阈值 = 同 8 个模长平方的
    中位数 → 每带恰 8bit,4 带共 32bit——与 :func:`_ring_bits_from_bands`
    的 DCT 能量签名同构(同位宽、同带内中位阈值口径),便于逐角度对照。

    量化方案论证(相对"幅度归一化量化"):① 与 ring 同构,对照实验口径
    一致;② 带内中位阈值对亮度线性缩放严格不变(全带模长同比例缩放);
    ③ 8 个互异值上的中位阈值使每带恰 4 置位(签名熵最大化),无需引入
    全局标定参数;固定阈值的幅度归一化方案在"某频占优"的构图上会把次频
    压进噪声区,位不稳。

    退化保护(幅度比口径,标定见 :data:`_DFT_DEGEN_RATIO`):极坐标重采样
    伪影会在角向均匀内容上注入 2~4% 的虚假角向"结构",其模长随变换漂移、
    决定哈希位后不稳(rings_broad 旋转距离实测 10~20);低于阈值幅度的带
    整体置零,真实角向结构(≥12%)不受影响。
    """
    bits = 0
    for band_index, band in enumerate(bands):
        dc = sum(band)
        squares: list[float] = []
        for k in range(1, _DFT_RING_K + 1):
            cos_table, sin_table = _DFT_TWIDDLE[k]
            re = sum(p * c for p, c in zip(band, cos_table))
            im = sum(p * s for p, s in zip(band, sin_table))
            squares.append(re * re + im * im)
        if math.sqrt(max(squares)) <= _DFT_DEGEN_RATIO * abs(dc):
            continue  # 角向无结构带(含直流为 0 的全黑带):该带 8bit 全 0
        median = statistics.median(squares)
        for i, square in enumerate(squares):
            if square > median:
                bits |= 1 << (31 - (band_index * _DFT_RING_K + i))
    return bits


def dft_ring_hash(path: str | Path) -> str:
    """计算图片**严格旋转不变**感知哈希(DFT 模长签名,32bit),返回 8 位小写 hex。

    流程:复用 :func:`ring_hash` 的极坐标采样管线(中心方形裁剪 → PIL
    MESH 极坐标重采样 → 32 角度桶 × 4 半径带)→ 各带沿角度轴做 **DFT 取
    复数模长**(k=1..8)→ 带内中位阈值签名(见
    :func:`_dft_ring_bits_from_bands`)。

    **严格旋转不变的证明概要**:设某半径带的角向轮廓为 ``x_i``(i = 0..31,
    对应角度 2πi/32)。图像旋转 θ 后内容角向平移,``x'_i = x((i−Δ) mod 32)``,
    其中 Δ = θ·32/360°(可为分数桶)。由 DFT 循环移位定理,整数 Δ 时
    ``X'_k = X_k·e^(−i2πkΔ/32)``,故 ``|X'_k| = |X_k|`` 与 Δ 无关;分数 Δ
    时,连续角向轮廓 g 的傅里叶级数满足 ``G^(θ)_k = G_k·e^(−ikθ)``(连续
    平移定理),模长严格不变,而带限轮廓(角向频率 < 16)的 32 点 DFT 与
    连续系数成比例,模长仍严格不变。仅有的误差源是超出带限的分量的混叠
    与极坐标重采样噪声——均为二阶效应。对照 :func:`ring_hash`:DCT 基对
    平移不封闭,平移把每个 DCT 系数的能量**泄漏到全部系数**(±15° ≈ 1.33
    桶时泄漏已过半),这是 ring 在 ±15° 失守(实测召回 0.188)的根因;DFT
    模长无此泄漏,同 32bit 预算下 ±3/±8/±15° 实测召回 1.000(最差距 2)。

    对缩放 / 重编码 / 噪声与 ring 同等稳定(实测良性 JPEG/缩放最大距 2 ≤
    工作距 3)。镜像会翻转角向轮廓方向,但模长谱不变,故对水平/垂直翻转
    **严格不变**——方向信息(顺/逆时针结构)被丢弃属设计取舍。

    退化语义:单色图 / 角向无结构图(辐射对称、纯径向渐变)各带经退化
    保护置零,哈希恒为 ``00000000``(任意灰度含全黑);径向构图(rings_broad
    类)与其余构图最小距实测 ≥ 12。旋转不变指纹**数学上不可能**区分互为
    旋转等价的构图,此类图不要用本哈希单独判定(候选非判定,与主指纹
    联用)。未安装 Pillow、文件不存在 / 损坏抛中文 ValueError(与
    :func:`ring_hash` 同款容错面);计算耗时记
    ``telemetry.timer("phash2.dft_ring_compute")``。
    """
    src = Path(path)
    if not src.is_file():
        raise ValueError(f"图片文件不存在:{src}")
    with telemetry.timer("phash2.dft_ring_compute"):
        return f"{_dft_ring_bits_from_bands(_polar_band_rows(src)):08x}"


# ---------------------------------------------------------------------------
# A228 不变性指纹之五:tile_hash(重叠子窗口,768bit)+ tile_any_match
# ---------------------------------------------------------------------------


def _tile_bits_list(src: Path) -> list[int]:
    """一次解码 → 128 规范形 → 12 个 tile 各 64bit 低频 DCT(确定性)。

    tile 几何(顺序即哈希段序,切片口径与 :func:`tile_any_match` 一致):

    1. 段 0:S1 全图(128×128);
    2. 段 1~9:S2 重叠半图 tile——4×4 网格(每格 32px)上的 2×2 滑窗,
       步长 1 格,共 3×3 个 64×64 tile(相邻重叠 50%),光栅序;
    3. 段 10~11:中心裁剪尺度梯两级,边长 ≈ 0.707 / 0.354(√2 步进)。

    每 tile LANCZOS 缩放到 32×32 后做 8×8 低频 DCT(除 DC 中位阈值,
    :func:`_dct_lowfreq_bits` 的 k=8 特例)→ 64bit。
    """
    pil_image = _load_pil()
    if pil_image is None:
        raise ValueError(
            "未安装 Pillow,无法解码图片计算子窗口指纹(pip install Pillow 后启用)"
        )
    try:
        with pil_image.open(src) as im:
            gray = im.convert("L")
            resampling = getattr(pil_image, "Resampling", pil_image)
            base = gray.resize((_TILE_CANON, _TILE_CANON), resample=resampling.LANCZOS)
            cell = _TILE_CANON // 4
            windows = [base]
            for oy in _TILE_GRID_OFFSETS:
                for ox in _TILE_GRID_OFFSETS:
                    windows.append(
                        base.crop((ox * cell, oy * cell,
                                   (ox + 2) * cell, (oy + 2) * cell))
                    )
            for side_fraction in _TILE_LADDER:
                side = max(1, int(round(_TILE_CANON * side_fraction)))
                offset = (_TILE_CANON - side) // 2
                windows.append(
                    base.crop((offset, offset, offset + side, offset + side))
                )
            bits: list[int] = []
            for window in windows:
                small = window.resize((_GRID, _GRID), resample=resampling.LANCZOS)
                getter = getattr(small, "get_flattened_data", None) or small.getdata
                data = list(getter())
                rows = [
                    [float(v) for v in data[row * _GRID:(row + 1) * _GRID]]
                    for row in range(_GRID)
                ]
                bits.append(_dct_lowfreq_bits(rows, _TILE_K))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法解码图片文件:{src}({exc})") from exc
    return bits


def tile_hash(path: str | Path) -> str:
    """计算图片**重叠子窗口**感知哈希(12 tile × 64bit = 768bit),返回 192 位小写 hex。

    流程:灰度 → 128×128 规范形 → 12 个 tile(S1 全图 + 9 个重叠半图
    tile + 2 级中心裁剪尺度梯,几何见 :func:`_tile_bits_list`)→ 每 tile
    32×32 低频 DCT 64bit → 光栅序串联,每 tile 恒 16 个 hex 字符。

    **抗中心裁剪机理**:裁剪攻击(丢边缘 + 回填放大)销毁的是边缘 tile,
    中心内容以放大后的尺度保留——尺度梯两级(≈0.707/0.354)保证任意
    中心裁剪(边长 ∈ [0.354, 1])的内容与某一级 tile 的尺度相差 ≤×1.19,
    重叠半图 tile 再提供位置容差。实测裁剪 10%/20%/30% 召回
    0.375/0.750/0.875(pyramid 0.250/0.125/0.125),照片族构图从全档
    零召回中恢复(photo 裁剪 30% 距 10 ≤ 工作距 8)。

    **tile 位宽权衡(64bit 而非任务草案的 8bit,实测依据)**:任一命中
    语义下两图需比较 12×12 = 144 个 tile 对、取最小,误报距离随对数增多
    而塌缩——8bit/15bit tile(k=3/4)实测异图最小距 **0**(小 tile 信息量
    不足,144 对内必撞),0.25 边长的梯级同理;64bit tile(8×8 低频
    DCT,与 :func:`phash256` 象限同口径)实测异图最小距 12,工作距 8
    在"良性最大距 6 / 异图最小距 12"之间留双向 ≥2bit 裕量,是本语料下
    能同时容纳"任一命中语义 + 裁剪召回"的最小位宽。

    查询语义(红线 48:**候选非判定**):必须用 :func:`tile_any_match`
    (144 对取最小),任一 tile 对命中即候选——宽进严出,候选集应再经
    主指纹 / 人工复核收敛,不要据单 tile 命中直接处置。

    退化语义:单色图每 tile 交流系数全零(相对峰值归零保护)→ 灰度 >0
    的单色图哈希恒为 ``8000000000000000`` × 12 段(全零灰度恒为全 0),
    两个不同灰度的单色图距离为 0——无结构可辨,入库场景应改用字节
    sha256 去重;极小图(边长 <16px)上采样到规范形后趋常量,同样退化。
    未安装 Pillow、文件不存在 / 损坏抛中文 ValueError;计算耗时记
    ``telemetry.timer("phash2.tile_compute")``。
    """
    src = Path(path)
    if not src.is_file():
        raise ValueError(f"图片文件不存在:{src}")
    with telemetry.timer("phash2.tile_compute"):
        return "".join(f"{bits:016x}" for bits in _tile_bits_list(src))


def tile_any_match(a: str, b: str) -> int:
    """两个 768bit 子窗口哈希的查询距离:**12×12 tile 对汉明取最小**(0~64)。

    入参须各为 192 位十六进制(12 段 × 每 tile 16 hex = 64bit),非法长度 /
    非法十六进制抛中文 ValueError(大小写与首尾空白容忍)。距离语义:裁剪 /
    zoom 攻击下"哪些 tile 幸存、以什么尺度幸存"事先未知,取全部 tile 对的
    最小汉明距离作为查询距离——这是**宽召回**的候选语义(任一 tile 对命中
    即候选,红线 48:候选非判定),代价是随机异图的期望最小距从 ~32 降到
    ~12(144 对取最小的次序统计),工作距 :data:`TILE_SUGGESTED_MAX_DISTANCE`
    (8)按实测异图最小距 12 留零误报上沿校准,不要放大使用。
    """
    ha = _normalize_hex(a, "子窗口哈希 a")
    hb = _normalize_hex(b, "子窗口哈希 b")
    if len(ha) != _TILE_HEX_LEN or len(hb) != _TILE_HEX_LEN:
        raise ValueError(
            f"tile_any_match 需要 {_TILE_HEX_LEN} 位十六进制(768bit)哈希,"
            f"实际收到 {len(ha)} 位与 {len(hb)} 位"
        )
    tiles_a = [ha[i * _TILE_SEG_HEX:(i + 1) * _TILE_SEG_HEX] for i in range(_TILE_COUNT)]
    tiles_b = [hb[i * _TILE_SEG_HEX:(i + 1) * _TILE_SEG_HEX] for i in range(_TILE_COUNT)]
    return min(
        hamming_hex(ta, tb) for ta in tiles_a for tb in tiles_b
    )


# ---------------------------------------------------------------------------
# 自检(供 A138 kernel_bench 总控;确定性,无墙钟、无文件 IO、无 PIL)
# ---------------------------------------------------------------------------


def _synthetic_rows(kind: int) -> list[list[float]]:
    """构造确定性 32x32 灰度矩阵(解析公式,零随机源、零 IO)。

    kind 0 为"照片式"构图(平滑双向渐变 + 圆 / 矩形几何形状);1~8 为
    互不相同的构图:纯色 / 横条纹 / 竖条纹 / 细棋盘 / 纵向渐变 / 同心环 /
    对角渐变 / 4x4 块噪声。``kernel_selfcheck`` 用 0 的亮度变体做同图侧、
    0 vs 1~8 做异图侧。
    """
    n = _GRID
    rows: list[list[float]] = []
    for y in range(n):
        row: list[float] = []
        for x in range(n):
            u, v = x / n, y / n
            if kind == 0:  # 照片式:平滑双向渐变 + 三块几何形状(峰值 210)
                val = 96 + 48 * math.sin(2 * math.pi * u + 0.7) \
                    + 32 * math.cos(2 * math.pi * v + 1.9)
                if (u - 0.35) ** 2 + (v - 0.45) ** 2 < 0.06:
                    val = 210.0
                elif 0.55 < u < 0.9 and 0.1 < v < 0.45:
                    val = 40.0
                elif (u - 0.72) ** 2 + (v - 0.75) ** 2 < 0.05:
                    val = 150.0
            elif kind == 1:
                val = 128.0                                   # 纯色
            elif kind == 2:
                val = 40.0 if (y // 4) % 2 else 216.0          # 横条纹
            elif kind == 3:
                val = 40.0 if (x // 4) % 2 else 216.0          # 竖条纹
            elif kind == 4:
                val = 30.0 if ((x // 4 + y // 4) % 2) else 225.0  # 细棋盘
            elif kind == 5:
                val = 176.0 * v + 40.0                        # 纵向渐变
            elif kind == 6:
                r = math.hypot(u - 0.5, v - 0.5)              # 同心圆环
                val = 128 + 100 * math.sin(6 * math.pi * r)
            elif kind == 7:
                val = 176.0 * (u + v) / 2 + 40.0              # 对角渐变
            else:  # kind >= 8:确定性 4x4 块噪声(解析伪随机,无随机源)
                fract = abs(math.sin(12.9898 * (x // 4) + 78.233 * (y // 4) + kind * 37.7))
                val = 255.0 * (fract * fract * fract)
            row.append(val)
        rows.append(row)
    return rows


def _ring_selfcheck_profiles(
    shifts_deg: tuple[float, ...] = (0, 3, -3, 8, -8),
) -> tuple[list[list[list[float]]], list[list[list[float]]]]:
    """ring/dft 自检的确定性角向轮廓组(同侧族 / 异侧族,零随机源)。

    同侧族 = 同一频率构成(1/2/5 次正弦混合,各带振幅递减模拟半径带)
    的 4 带轮廓组做 **解析角度偏移**采样(``shifts_deg`` 换算为 32 桶的
    分数平移——与真实旋转对角向信号的作用完全同型,比整桶平移温和且
    更贴近验收口径);异侧族 = 频率构成不同的 4 带轮廓组(3/4/7 次与
    2/6 次混合)。能量签名对小幅角偏移近似不变、对频率构成敏感,
    margin = 异侧最小距 − 同侧最大距(实测 16 − 8 = 8)。

    A228 起 ``shifts_deg`` 可参数化:ring 自检沿用默认 ±3/±8,dft 自检
    传入 ±15/±25(模长谱对任意分数偏移不变,验证区更宽)。
    """
    n = _RING_ANGLES

    def _profile(
        freqs: list[int], amps: list[float], delta: float = 0.0
    ) -> list[float]:
        return [
            128.0 + sum(
                a * math.sin(2 * math.pi * f * ((i / n) - delta) + 0.3 * j)
                for j, (f, a) in enumerate(zip(freqs, amps))
            )
            for i in range(n)
        ]

    def _group(freqs: list[int], amps: list[float], delta: float = 0.0):
        return [
            _profile(freqs, [a * (1.0 - 0.18 * band) for a in amps], delta)
            for band in range(_RING_BANDS)
        ]

    freqs, amps = [1, 2, 5], [50.0, 30.0, 15.0]
    same = [
        _group(freqs, amps, math.radians(deg) / (2 * math.pi))
        for deg in shifts_deg
    ]
    diff = [
        _group([3, 4, 7], [45.0, 32.0, 18.0]),
        _group([2, 6], [55.0, 22.0]),
    ]
    return same, diff


def _tile_bits_from_rows(rows: list[list[float]]) -> list[int]:
    """矩阵级 tile 位列表(自检用,零 IO / 零 PIL)。

    与 :func:`_tile_bits_list` 的"128 规范形 + LANCZOS"路径在数学上同型
    (tile 几何同比例、每 tile 同 k=8 低频 DCT;差异仅在重采样核,与
    pyramid 自检的"逐层均值粗化"同款近似口径):32x32 矩阵上直接切
    tile——S1 全图、S2 = 2×2 格滑窗(步长 1 格 = 8px)共 9 个 16×16、
    中心梯级按 :data:`_TILE_LADDER` 比例取整边长。
    """
    n = len(rows)
    cell = n // 4
    mats = [rows]
    for oy in _TILE_GRID_OFFSETS:
        for ox in _TILE_GRID_OFFSETS:
            mats.append(
                [r[ox * cell:(ox + 2) * cell] for r in rows[oy * cell:(oy + 2) * cell]]
            )
    for side_fraction in _TILE_LADDER:
        side = max(1, int(round(n * side_fraction)))
        offset = (n - side) // 2
        mats.append([r[offset:offset + side] for r in rows[offset:offset + side]])
    return [_dct_lowfreq_bits(m, _TILE_K) for m in mats]


def kernel_selfcheck() -> dict[str, Any]:
    """离线确定性自检:同图变换最大距 vs 异图最小距的区分度裕量。

    同图侧 = 照片式构图(kind 0)的亮度 ×1.1 / ×0.9(中位阈值 DCT 哈希
    对正线性缩放在数学上不变)与整数量化抖动;异图侧 = kind 0 vs 8 种
    不同构图。返回 ``{"name", "metric", "value", "baseline"}``,value 为
    ``min(异图距离) - max(同图距离)``(须 > 0 才有区分度);全程零
    随机源、零文件 IO、不依赖 Pillow / 墙钟。

    A219 附加键(旧四键口径不变,供 A138 kernel_bench 收入 ``extra``):三个
    不变性指纹各自的同型裕量——

    - ``mirror_margin_bits``:翻转轨道规范形在 {恒等, H, V, 180°} 上
      距离恒 0(同侧最大 = 0)vs 异构图规范形最小距(64bit 域);
    - ``ring_margin_bits``:合成角向轮廓 ±1 桶循环移位(旋转近似)最大
      距 vs 不同频率构成轮廓最小距(32bit 域);
    - ``pyramid_margin_bits``:亮度 ×1.1/×0.9(不变)与异构图 32/16/8
      三层取 min 距离的裕量(36bit 层域)。

    A228 附加键(旧七键口径不变,继续追加):

    - ``dft_ring_margin_bits``:角向轮廓 ±8/±15/±25° 分数偏移(DFT 模长
      的严格不变区)最大距 vs 不同频率构成轮廓最小距(32bit 域);
    - ``tile_margin_bits``:亮度 ×1.1/×0.9(中位阈值 DCT 不变)同侧
      最大距 vs 异构图 12 tile 任一命中最小距(64bit tile 域,矩阵级
      同型口径见 :func:`_tile_bits_from_rows`)。
    """
    base = _synthetic_rows(0)
    base_hash = _dct256_hash(base)
    same_hashes = [
        base_hash,
        _dct256_hash([[p * 1.1 for p in row] for row in base]),
        _dct256_hash([[p * 0.9 for p in row] for row in base]),
        _dct256_hash([[float(int(round(p))) for p in row] for row in base]),
    ]
    max_same = max(hamming_hex(base_hash, h) for h in same_hashes[1:])
    cross = [
        hamming_hex(base_hash, _dct256_hash(_synthetic_rows(kind)))
        for kind in range(1, 9)
    ]
    margin = min(cross) - max_same

    # --- 附加键一:mirror(轨道封闭性 0 距 vs 异构图最小距) ---
    mirror_base = _mirror_bits(base)
    mirror_orbit_max = max(
        hamming_hex(f"{mirror_base:016x}", f"{_mirror_bits(variant):016x}")
        for variant in (
            [row[::-1] for row in base],        # 水平翻
            base[::-1],                          # 垂直翻
            [row[::-1] for row in base[::-1]],   # 180°
            [[p * 1.1 for p in row] for row in base],  # 亮度不变性
        )
    )
    mirror_cross = [
        hamming_hex(f"{mirror_base:016x}", f"{_mirror_bits(_synthetic_rows(k)):016x}")
        for k in range(1, 9)
    ]
    mirror_margin = min(mirror_cross) - mirror_orbit_max

    # --- 附加键二:ring(循环移位同侧 vs 频率异侧) ---
    same_groups, diff_groups = _ring_selfcheck_profiles()
    ring_ref = _ring_bits_from_bands(same_groups[0])
    ring_same_max = max(
        hamming_hex(f"{ring_ref:08x}", f"{_ring_bits_from_bands(group):08x}")
        for group in same_groups[1:]
    )
    ring_cross = [
        hamming_hex(f"{ring_ref:08x}", f"{_ring_bits_from_bands(group):08x}")
        for group in diff_groups
    ]
    ring_margin = min(ring_cross) - ring_same_max

    # --- 附加键三:pyramid(亮度不变 vs 异图,三层取 min) ---
    def _pyr_hex(rows: list[list[float]]) -> str:
        # 自检在 32x32 矩阵上只算最细层 + 其半采样层(粗化即典型层差),
        # 与 _pyramid_level_bits 的"逐层 LANCZOS"口径在数学上同型。
        half = [[(rows[2 * y][2 * x] + rows[2 * y][2 * x + 1]
                  + rows[2 * y + 1][2 * x] + rows[2 * y + 1][2 * x + 1]) / 4.0
                 for x in range(_GRID // 2)] for y in range(_GRID // 2)]
        quarter = [[(half[2 * y][2 * x] + half[2 * y][2 * x + 1]
                     + half[2 * y + 1][2 * x] + half[2 * y + 1][2 * x + 1]) / 4.0
                    for x in range(_GRID // 4)] for y in range(_GRID // 4)]
        return "".join(
            f"{_dct_lowfreq_bits(r, _PYRAMID_K):09x}" for r in (rows, half, quarter)
        )

    pyr_base = _pyr_hex(base)
    pyr_same_max = max(
        pyramid_distance(pyr_base, _pyr_hex([[p * f for p in row] for row in base]))
        for f in (1.1, 0.9)
    )
    pyr_cross = [
        pyramid_distance(pyr_base, _pyr_hex(_synthetic_rows(k)))
        for k in range(1, 9)
    ]
    pyramid_margin = min(pyr_cross) - pyr_same_max

    # --- 附加键四:dft_ring(分数角偏移 ±8/±15/±25° vs 频率异侧) ---
    dft_same_groups, dft_diff_groups = _ring_selfcheck_profiles(
        shifts_deg=(0, 8, -8, 15, -15, 25, -25)
    )
    dft_ref = _dft_ring_bits_from_bands(dft_same_groups[0])
    dft_same_max = max(
        hamming_hex(f"{dft_ref:08x}", f"{_dft_ring_bits_from_bands(group):08x}")
        for group in dft_same_groups[1:]
    )
    dft_cross = [
        hamming_hex(f"{dft_ref:08x}", f"{_dft_ring_bits_from_bands(group):08x}")
        for group in dft_diff_groups
    ]
    dft_ring_margin = min(dft_cross) - dft_same_max

    # --- 附加键五:tile(亮度不变 vs 异图,12 tile 任一命中最小距) ---
    def _tile_hex(rows: list[list[float]]) -> str:
        return "".join(f"{bits:016x}" for bits in _tile_bits_from_rows(rows))

    tile_base = _tile_hex(base)
    tile_same_max = max(
        tile_any_match(tile_base, _tile_hex([[p * f for p in row] for row in base]))
        for f in (1.1, 0.9)
    )
    tile_cross = [
        tile_any_match(tile_base, _tile_hex(_synthetic_rows(k)))
        for k in range(1, 9)
    ]
    tile_margin = min(tile_cross) - tile_same_max

    return {
        "name": "phash2",
        "metric": "same_max_lt_diff_min_margin_bits",
        "value": round(margin, 4),
        "baseline": 0,
        "mirror_margin_bits": round(mirror_margin, 4),
        "ring_margin_bits": round(ring_margin, 4),
        "pyramid_margin_bits": round(pyramid_margin, 4),
        "dft_ring_margin_bits": round(dft_ring_margin, 4),
        "tile_margin_bits": round(tile_margin, 4),
    }
