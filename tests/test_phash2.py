"""A134 netsentinel.vision.phash2(指纹内核 v2 · 256bit 分块 pHash)测试。

纯离线、确定性(合成图固定随机源、断言零墙钟)、只写 tmp_path。覆盖:

- ``phash256``:64 位小写 hex 格式与决定性;≥8 组同图变换(0.5x / 0.8x
  缩放、亮度 ±10%)距离小;≥8 组异图构图(纯色 / 横竖条纹 / 粗细棋盘 /
  块噪声×2 / 对角渐变 / 同心环)距离大;象限结构(只改右下象限 → 仅末段
  16 hex 变化);PNG/BMP 格式无关;文件不存在 / 目录 / 损坏 / 截断 /
  Pillow 缺失(monkeypatch)一律中文 ValueError;
- ``to64``:16 位 hex、精确切片口径(每象限段前 4 hex)、同图稳定、异图
  距离同步大、与 A43 ``phash`` "近似而非全等"(确定性反例)、非 64 位
  入参拒绝;
- ``hamming_hex``:自距 0、已知向量、256bit 与 64bit(A43 哈希)等长
  互通、长度不等 / 非法 hex 中文报错、大小写与空白容忍;
- **红线 31 基准**::func:`test_v7_bench_separability_margin` 以确定性
  构造图断言"同图最大距离 < 异图最小距离"(8 组同图 × 80 组异图交叉),
  操作于确定性数据,不依赖墙钟;
- ``kernel_selfcheck``:零 IO 自检返回正裕量且两次调用一致(供 A138)。

区分度实测口径(2026-10 校准,确定性图):同图最大 4bit / 异图最小
90bit(256bit 域),阈值断言留 ≥4 倍余量。
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path
from typing import Any

import pytest

PIL = pytest.importorskip("PIL", reason="需要 Pillow 才能测试 256bit 指纹")
from PIL import Image, ImageDraw, ImageEnhance  # noqa: E402

from netsentinel.intel.phash import hamming as hamming43
from netsentinel.intel.phash import phash as phash43
from netsentinel.vision.phash2 import (
    hamming_hex,
    kernel_selfcheck,
    phash256,
    to64,
)

_RESAMPLE = Image.Resampling.LANCZOS

#: 断言阈值(实测同图最大 4 / 异图最小 90,均留足余量)。
_SAME_MAX_256 = 24    # 同图变换(缩放/亮度)汉明距离上限(256bit 域)
_DIFF_MIN_256 = 64    # 异图构图汉明距离下限(256bit 域)
_SAME_MAX_64 = 12     # 同图变换 to64 视图距离上限(64bit 域)
_DIFF_MIN_64 = 12     # 异图 to64 视图距离下限(64bit 域)


# ---------------------------------------------------------------------------
# 造图辅助:同图照片族(8 变体)与异图构图族(≥8 种)
# ---------------------------------------------------------------------------


def _photo(size: int, variant: int = 0) -> Image.Image:
    """有低频结构的灰度"照片":平滑双向渐变 + 三块几何形状 + 少量散点。

    峰值 210(亮度 ×1.10 后 231 < 255,不触发截断);variant 平移形状 /
    改变相位,8 个变体互不相同但组内(同 variant 的缩放 / 亮度版)近重复。
    """
    rng = random.Random(20261002 + variant)
    img = Image.new("L", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            base = 96 + 48 * math.sin((x / size) * 2 * math.pi + 0.7 + variant) \
                + 32 * math.cos((y / size) * 2 * math.pi + 1.9)
            px[x, y] = max(0, min(210, int(base)))
    draw = ImageDraw.Draw(img)
    ox = (variant % 4) * size * 0.08
    oy = (variant % 3) * size * 0.06
    draw.ellipse((size * 0.15 + ox, size * 0.20 + oy,
                  size * 0.55 + ox, size * 0.70 + oy), fill=210)
    draw.rectangle((size * 0.55 - ox, size * 0.10,
                    size * 0.90 - ox, size * 0.45), fill=40)
    draw.ellipse((size * 0.50, size * 0.55 + oy,
                  size * 0.95, size * 0.95 + oy), fill=150)
    for _ in range(size):  # 少量散点噪声,避免完全可预测
        px[rng.randrange(size), rng.randrange(size)] = rng.randrange(256)
    return img


def _solid(size: int, value: int) -> Image.Image:
    return Image.new("L", (size, size), value)


def _stripes(size: int, vertical: bool, period: int = 4) -> Image.Image:
    img = Image.new("L", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            k = (x if vertical else y) // period
            px[x, y] = 40 if k % 2 else 216
    return img


def _checker(size: int, cell: int) -> Image.Image:
    img = Image.new("L", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            px[x, y] = 30 if ((x // cell + y // cell) % 2) else 225
    return img


def _blocky_noise(size: int, seed: int) -> Image.Image:
    """8x8 大块随机灰度噪声(固定随机源,与平滑照片构图完全不同)。"""
    rng = random.Random(seed)
    img = Image.new("L", (size, size))
    px = img.load()
    block = max(1, size // 8)
    for by in range(0, size, block):
        for bx in range(0, size, block):
            value = rng.randrange(256)
            for y in range(by, min(by + block, size)):
                for x in range(bx, min(bx + block, size)):
                    px[x, y] = value
    return img


def _diag_gradient(size: int) -> Image.Image:
    img = Image.new("L", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            px[x, y] = int(176 * ((x + y) / (2 * size)) + 40)
    return img


def _rings(size: int) -> Image.Image:
    """同心圆环:径向高频构图,与双向渐变照片区分明显。"""
    img = Image.new("L", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            r = math.hypot(x / size - 0.5, y / size - 0.5)
            px[x, y] = max(0, min(255, int(128 + 100 * math.sin(6 * math.pi * r))))
    return img


def _compositions(size: int = 200) -> list[Image.Image]:
    """10 种互不相同的异图构图(纯色×2 / 条纹×2 / 棋盘×2 / 噪声×2 / 渐变 / 圆环)。"""
    return [
        _solid(size, 0),
        _solid(size, 200),
        _stripes(size, vertical=False),
        _stripes(size, vertical=True),
        _checker(size, cell=4),
        _checker(size, cell=8),
        _blocky_noise(size, 7),
        _blocky_noise(size, 99),
        _diag_gradient(size),
        _rings(size),
    ]


def _save(img: Image.Image, path: Path) -> str:
    img.save(path, format="PNG")
    return str(path)


def _variants(img: Image.Image) -> dict[str, Image.Image]:
    """同图变换族:0.5x / 0.8x 缩放与亮度 ±10%。"""
    return {
        "scale0.5x": img.resize((100, 100), _RESAMPLE),
        "scale0.8x": img.resize((160, 160), _RESAMPLE),
        "bright+10%": ImageEnhance.Brightness(img).enhance(1.10),
        "bright-10%": ImageEnhance.Brightness(img).enhance(0.90),
    }


# ---------------------------------------------------------------------------
# phash256:格式 / 决定性 / 同图不变性(≥8 组)/ 异图区分度(≥8 组)
# ---------------------------------------------------------------------------


def test_phash256_format_and_deterministic(tmp_path: Any) -> None:
    """phash256 返回 64 位小写 hex,同文件两次调用结果逐位一致。"""
    path = _save(_photo(200, 0), tmp_path / "photo.png")
    h1 = phash256(path)
    h2 = phash256(path)
    assert h1 == h2
    assert len(h1) == 64
    assert h1 == h1.lower()
    int(h1, 16)  # 可解析为十六进制


@pytest.mark.parametrize("variant", range(8))
def test_same_image_variants_stay_close(tmp_path: Any, variant: int) -> None:
    """8 组同图变换:缩放 0.5x / 0.8x 与亮度 ±10% 后距离 ≤ 24(256bit 域)。

    中位阈值 DCT 哈希对缩放 / 亮度线性缩放在数学上近不变,实测最大 4bit,
    断言上限留 6 倍余量。
    """
    base = _photo(200, variant)
    h0 = phash256(_save(base, tmp_path / f"v{variant}_base.png"))
    for name, img in _variants(base).items():
        h = phash256(_save(img, tmp_path / f"v{variant}_{name}.png"))
        d = hamming_hex(h0, h)
        assert d <= _SAME_MAX_256, f"variant={variant} {name} 距离 {d} > {_SAME_MAX_256}"


def test_different_compositions_far(tmp_path: Any) -> None:
    """≥8 组异图构图(10 种)与照片基图距离全部 ≥ 64(实测最小 118)。"""
    h0 = phash256(_save(_photo(200, 0), tmp_path / "base.png"))
    for i, comp in enumerate(_compositions()):
        h = phash256(_save(comp, tmp_path / f"comp{i}.png"))
        d = hamming_hex(h0, h)
        assert d >= _DIFF_MIN_256, f"comp{i} 距离 {d} < {_DIFF_MIN_256}"


def test_different_photo_variants_are_not_near_duplicates(tmp_path: Any) -> None:
    """形状平移 / 相位不同的两张照片不是近重复:距离显著大于同图阈值。"""
    ha = phash256(_save(_photo(200, 1), tmp_path / "p1.png"))
    hb = phash256(_save(_photo(200, 6), tmp_path / "p6.png"))
    assert hamming_hex(ha, hb) >= _SAME_MAX_256


# ---------------------------------------------------------------------------
# phash256:象限结构与格式无关
# ---------------------------------------------------------------------------


def _quadrant_img() -> Image.Image:
    """32x32 图:四象限各有独立图案(尺寸恰为 32,LANCZOS 同尺寸重采样
    为恒等映射,象限间无滤波渗漏,可精确断言分块结构)。"""
    img = Image.new("L", (32, 32))
    px = img.load()
    palette = [(10, 60), (90, 140), (170, 220), (240, 30)]
    for y in range(32):
        for x in range(32):
            q = (2 if y >= 16 else 0) + (1 if x >= 16 else 0)
            dark, bright = palette[q]
            px[x, y] = dark if (x // 4 + y // 4) % 2 else bright
    return img


def test_quadrant_segments_isolated(tmp_path: Any) -> None:
    """分块结构:只改右下象限 → 前三段(左上/右上/左下)逐字符不变,
    仅末段变化——256bit 按光栅序 4 段拼接的口径直接可观察。"""
    base = _quadrant_img()
    modified = _quadrant_img()
    pm = modified.load()
    for y in range(16, 32):  # 反转右下象限亮度
        for x in range(16, 32):
            pm[x, y] = 255 - pm[x, y]
    ha = phash256(_save(base, tmp_path / "q_base.png"))
    hb = phash256(_save(modified, tmp_path / "q_mod.png"))
    assert ha[0:16] == hb[0:16]   # 左上象限不变
    assert ha[16:32] == hb[16:32]  # 右上象限不变
    assert ha[32:48] == hb[32:48]  # 左下象限不变
    assert ha[48:] != hb[48:]      # 仅右下象限变化


def test_format_independence_png_bmp(tmp_path: Any) -> None:
    """同一像素内容存 PNG 与 BMP(均无损):哈希逐位一致(解码无关性)。"""
    img = _photo(32, 3)
    png = tmp_path / "f.png"
    bmp = tmp_path / "f.bmp"
    img.save(png, format="PNG")
    img.save(bmp, format="BMP")
    assert phash256(str(png)) == phash256(str(bmp))


# ---------------------------------------------------------------------------
# phash256:错误分支(不存在 / 目录 / 损坏 / 截断 / Pillow 缺失)
# ---------------------------------------------------------------------------


def test_phash256_missing_file_raises(tmp_path: Any) -> None:
    """文件不存在 → 中文 ValueError。"""
    with pytest.raises(ValueError, match="图片文件不存在"):
        phash256(str(tmp_path / "nope.png"))


def test_phash256_directory_raises(tmp_path: Any) -> None:
    """路径是目录而非文件 → 中文 ValueError。"""
    with pytest.raises(ValueError, match="图片文件不存在"):
        phash256(str(tmp_path))


def test_phash256_corrupted_file_raises(tmp_path: Any) -> None:
    """垃圾字节与截断 PNG(魔数后截断)均 → 中文 ValueError(无法解码)。"""
    garbage = tmp_path / "garbage.png"
    garbage.write_bytes(b"this is not an image \x00\x01\x02")
    with pytest.raises(ValueError, match="无法解码图片文件"):
        phash256(str(garbage))
    ok = tmp_path / "ok.png"
    _save(_photo(64, 0), ok)
    truncated = tmp_path / "truncated.png"
    truncated.write_bytes(ok.read_bytes()[:40])
    with pytest.raises(ValueError, match="无法解码图片文件"):
        phash256(str(truncated))


def test_phash256_pil_missing_raises(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Pillow 缺失分支:sys.modules["PIL"] = None → 直接中文 ValueError。

    V2 内核**无 aHash 降级路径**(与 A43 不同):256bit 口径没有降级等价物,
    缺 Pillow 一律明确报错。
    """
    path = _save(_photo(64, 0), tmp_path / "pic.png")
    monkeypatch.setitem(sys.modules, "PIL", None)
    with pytest.raises(ValueError, match="未安装 Pillow"):
        phash256(path)


# ---------------------------------------------------------------------------
# hamming_hex:自距 / 已知向量 / 互通 / 容错 / 报错
# ---------------------------------------------------------------------------


def test_hamming_hex_self_distance_zero(tmp_path: Any) -> None:
    """自距为 0:256bit 哈希、64bit 哈希、真实图片哈希三种口径。"""
    h256 = phash256(_save(_photo(64, 0), tmp_path / "a.png"))
    assert hamming_hex(h256, h256) == 0
    assert hamming_hex("0" * 64, "0" * 64) == 0
    assert hamming_hex("f" * 16, "f" * 16) == 0


def test_hamming_hex_known_vectors() -> None:
    """已知向量:全 1 vs 全 0(256bit 域 = 256;64bit 域 = 64)、单半字节翻转。"""
    assert hamming_hex("f" * 64, "0" * 64) == 256
    assert hamming_hex("f" * 16, "0" * 16) == 64
    assert hamming_hex("00", "01") == 1
    assert hamming_hex("ff", "00") == 8
    assert hamming_hex("f0f0", "0f0f") == 16


def test_hamming_hex_length_mismatch_raises() -> None:
    """长度不等(256bit vs 64bit)→ 中文 ValueError。"""
    with pytest.raises(ValueError, match="长度不一致"):
        hamming_hex("f" * 64, "f" * 16)
    with pytest.raises(ValueError, match="长度不一致"):
        hamming_hex("ff", "fff")


def test_hamming_hex_invalid_hex_raises() -> None:
    """非法十六进制 / 空串 → 中文 ValueError。"""
    with pytest.raises(ValueError, match="非法哈希"):
        hamming_hex("zzzz", "0000")
    with pytest.raises(ValueError, match="非法哈希"):
        hamming_hex("", "0000")
    with pytest.raises(ValueError, match="非法哈希"):
        hamming_hex("0x10", "0000")


def test_hamming_hex_case_and_whitespace_tolerated() -> None:
    """大小写与首尾空白容忍:归一后与规范形式结果一致。"""
    assert hamming_hex("  FF  ", "ff") == hamming_hex("ff", "ff") == 0
    assert hamming_hex("ABCDEF", "000000") == 17  # 0xABCDEF = 2+3+2+3+3+4 个置位


def test_hamming_hex_interops_with_a43_64bit(tmp_path: Any) -> None:
    """与 A43 生态互通:对两张真实图的 64bit pHash,
    hamming_hex 与 intel.phash.hamming 结果一致。"""
    ha = phash43(_save(_photo(128, 0), tmp_path / "a.png"))
    hb = phash43(_save(_photo(128, 5), tmp_path / "b.png"))
    assert hamming_hex(ha, hb) == hamming43(ha, hb)


# ---------------------------------------------------------------------------
# to64:格式 / 切片口径 / 同图稳定 / 异图同步大 / 与 A43 近似关系 / 入参校验
# ---------------------------------------------------------------------------


def test_to64_format_and_known_slicing() -> None:
    """to64 返回 16 位小写 hex,且精确取每象限段(16 hex)的前 4 个字符:
    即每象限 64bit 的最高 16bit(8x8 系数中 u=0,1 两行)。"""
    h = "0123456789abcdef" * 4
    out = to64(h)
    assert out == "0123" * 4
    assert len(out) == 16
    assert to64(h.upper()) == out  # 大写容忍


def test_to64_stable_across_calls_and_inputs(tmp_path: Any) -> None:
    """to64 决定性:同哈希两次调用一致;不同图给不同 to64。"""
    h = phash256(_save(_photo(128, 0), tmp_path / "a.png"))
    assert to64(h) == to64(h)
    h2 = phash256(_save(_rings(128), tmp_path / "b.png"))
    assert to64(h) != to64(h2)


@pytest.mark.parametrize("variant", range(8))
def test_to64_same_image_stays_close(tmp_path: Any, variant: int) -> None:
    """8 组同图变换的 to64 视图距离 ≤ 12(实测最大 1;近似而非全等的
    64bit 视图同样具备近重复稳定性)。"""
    base = _photo(200, variant)
    t0 = to64(phash256(_save(base, tmp_path / f"t{variant}_base.png")))
    for name, img in _variants(base).items():
        t = to64(phash256(_save(img, tmp_path / f"t{variant}_{name}.png")))
        d = hamming_hex(t0, t)
        assert d <= _SAME_MAX_64, f"variant={variant} {name} to64 距离 {d}"


def test_to64_different_images_far(tmp_path: Any) -> None:
    """异图 to64 距离同步大:照片 vs 10 种构图全部 ≥ 12(实测最小 25)。"""
    t0 = to64(phash256(_save(_photo(200, 0), tmp_path / "base.png")))
    for i, comp in enumerate(_compositions()):
        t = to64(phash256(_save(comp, tmp_path / f"comp{i}.png")))
        d = hamming_hex(t0, t)
        assert d >= _DIFF_MIN_64, f"comp{i} to64 距离 {d} < {_DIFF_MIN_64}"


def test_to64_semantics_approximate_not_equal_to_a43(tmp_path: Any) -> None:
    """与 A43 phash 的语义对齐是"近似而非全等":三张确定性图中至少一张
    (实测三张全部)``to64(x) != phash(x)``——两者是不同变换,只保证距离
    强相关,不保证逐位相等。"""
    images = [_photo(200, 0), _stripes(200, vertical=False), _rings(200)]
    pairs = []
    for i, img in enumerate(images):
        path = _save(img, tmp_path / f"a43_{i}.png")
        pairs.append((to64(phash256(path)), phash43(path)))
    assert any(t != p for t, p in pairs), "to64 与 A43 phash 不应逐位全等"


@pytest.mark.parametrize(
    "bad",
    ["f" * 16, "f" * 63, "f" * 65, "", "xyz" * 21 + "x"],
)
def test_to64_rejects_non_64_hex(bad: str) -> None:
    """非 64 位十六进制入参(16/63/65 位、空、非法字符)→ 中文 ValueError。"""
    with pytest.raises(ValueError):
        to64(bad)


# ---------------------------------------------------------------------------
# 红线 31 基准(确定性数据,零墙钟)+ kernel_selfcheck(供 A138)
# ---------------------------------------------------------------------------


def test_v7_bench_separability_margin(tmp_path: Any) -> None:
    """红线 31 基准:同图最大距离 < 异图最小距离(区分度断言)。

    确定性构造数据:8 组同图(各 4 种变换 = 32 个同图距离)× 8 张照片
    vs 10 种构图(80 个异图距离),断言:

    - 同图侧最大值 ≤ 24(256bit 域,实测 4);
    - 异图侧最小值 ≥ 64(实测 90);
    - **同图最大 < 异图最小**(存在正区分度裕量,实测裕量 86)。

    全程固定随机源、无墙钟读数,离线可复现。
    """
    same_distances: list[int] = []
    cross_distances: list[int] = []
    comp_paths = [
        _save(comp, tmp_path / f"bench_comp{i}.png")
        for i, comp in enumerate(_compositions())
    ]
    comp_hashes = [phash256(p) for p in comp_paths]
    for variant in range(8):  # 8 组同图变换
        base = _photo(200, variant)
        h0 = phash256(_save(base, tmp_path / f"bench_v{variant}.png"))
        for img in _variants(base).values():
            h = phash256(_save(img, tmp_path / f"bench_v{variant}_x.png"))
            same_distances.append(hamming_hex(h0, h))
        for hc in comp_hashes:  # 每张照片 vs 10 种构图
            cross_distances.append(hamming_hex(h0, hc))
    assert len(same_distances) == 32   # 8 组 × 4 变换
    assert len(cross_distances) == 80  # 8 照片 × 10 构图
    same_max = max(same_distances)
    cross_min = min(cross_distances)
    assert same_max <= _SAME_MAX_256
    assert cross_min >= _DIFF_MIN_256
    assert same_max < cross_min, (
        f"区分度失效:同图最大 {same_max} ≥ 异图最小 {cross_min}"
    )


def test_kernel_selfcheck_positive_deterministic_margin() -> None:
    """kernel_selfcheck(零 IO / 零随机源):裕量 > baseline 0 且两次一致,
    字段齐全(供 A138 kernel_bench 总控调用)。"""
    first = kernel_selfcheck()
    assert first == kernel_selfcheck()  # 决定性
    assert first["name"] == "phash2"
    assert first["metric"] == "same_max_lt_diff_min_margin_bits"
    assert first["value"] > first["baseline"]
    assert first["baseline"] == 0


# ---------------------------------------------------------------------------
# A219 不变性指纹族:mirror / ring / pyramid 基础面
# (召回 / 误报 / 退化语义的验收矩阵见 tests/test_phash_invariant.py)
# ---------------------------------------------------------------------------


def test_invariant_hash_formats_and_deterministic(tmp_path: Any) -> None:
    """三新哈希:位宽口径(16/8/27 hex)、小写、同文件两次逐位一致。"""
    path = _save(_photo(200, 0), tmp_path / "inv.png")
    from netsentinel.vision.phash2 import (
        mirror_hash,
        pyramid_hash,
        ring_hash,
    )

    for func, width in ((mirror_hash, 16), (ring_hash, 8), (pyramid_hash, 27)):
        h1 = func(path)
        h2 = func(path)
        assert h1 == h2, f"{func.__name__} 同文件两次不一致"
        assert len(h1) == width
        assert h1 == h1.lower()
        int(h1, 16)


def test_kernel_selfcheck_invariant_extra_keys() -> None:
    """A219 附加键:三个不变性指纹的同型裕量为正、口径稳定、旧四键不动。"""
    from netsentinel.vision.phash2 import kernel_selfcheck as check

    first = check()
    for key in ("mirror_margin_bits", "ring_margin_bits", "pyramid_margin_bits"):
        assert first[key] > 0, f"{key} 须为正(同图变换 < 异图)"
    # 旧键口径不受附加键影响(A138 kernel_bench 的四键消费者零感知)
    assert set(first) >= {"name", "metric", "value", "baseline"}


# ---------------------------------------------------------------------------
# A228 新增指纹:dft_ring / tile 基础面
# (旋转 / 裁剪召回与误报的验收矩阵见 tests/test_phash_invariant.py)
# ---------------------------------------------------------------------------


def test_a228_hash_formats_and_deterministic(tmp_path: Any) -> None:
    """两新哈希:位宽口径(dft_ring 8 hex / tile 192 hex)、小写、
    同文件两次逐位一致(全确定性:解析变换 + 预计算三角表,零随机源)。"""
    path = _save(_photo(200, 0), tmp_path / "a228.png")
    from netsentinel.vision.phash2 import dft_ring_hash, tile_hash

    for func, width in ((dft_ring_hash, 8), (tile_hash, 192)):
        h1 = func(path)
        h2 = func(path)
        assert h1 == h2, f"{func.__name__} 同文件两次不一致"
        assert len(h1) == width
        assert h1 == h1.lower()
        int(h1, 16)


def test_kernel_selfcheck_a228_extra_keys() -> None:
    """A228 附加键:dft_ring / tile 的同型裕量为正;A219 七键口径不动
    (旧键消费者零感知,新增只追加)。"""
    from netsentinel.vision.phash2 import kernel_selfcheck as check

    first = check()
    for key in ("dft_ring_margin_bits", "tile_margin_bits"):
        assert first[key] > 0, f"{key} 须为正(同图变换 < 异图)"
    assert set(first) >= {
        "name", "metric", "value", "baseline",
        "mirror_margin_bits", "ring_margin_bits", "pyramid_margin_bits",
    }
