"""A123 netsentinel.vision.heuristic_kernel(识别内核·肤色启发式)测试。

全部离线、确定性(合成图零随机源、断言零墙钟),只写 tmp_path。覆盖:

- 纯 stdlib PNG 读取器:色型 0 灰度 / 2 RGB / 4 灰度+Alpha / 6 RGBA、
  扫描线滤波 0-4 轮换往返一致、隔行/16bit/坏 CRC/截断一律拒绝;
- 容错:文件不存在、缺 PIL 时的非 PNG(JPEG 魔数)→ nsfw_prob=0 + scores.error;
- PIL 路径:真 JPEG 可解、同 PNG 双路径(PIL vs stdlib)结果逐位一致;
- 特征与公式:skin_ratio/max_blob/edge_density 取值范围、小图 small 标记、
  min_image_px 配置口径、组合公式精确复算、满肤图钳制 0.98;
- 注册:get_classifier("skin") 命中本内核;
- bench(红线 31,操作计数/确定性数据,禁墙钟):
  * test_v7_bench_linear_separability —— 8 张肤色椭圆主体图 vs 8 张风景/文本图
    (纯 stdlib 构造、强制无 PIL 解码),均值差 > 0.2 且阈值分类零错分;
  * test_v7_bench_flood_fill_ops —— flood fill 操作计数 ≤ 64,
    全肤格恰 64、空格 0;
- kernel_selfcheck:确定性自检返回均值差 > baseline。
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

import pytest

from netsentinel.contracts import Config, ImageEvidence
from netsentinel.vision import heuristic_kernel as hk
from netsentinel.vision.heuristic_kernel import (
    SkinHeuristicClassifier,
    kernel_selfcheck,
    read_png_rgb,
    synthetic_clean_pixels,
    synthetic_skin_pixels,
)

try:  # 可选 Pillow:仅在个别用例使用(缺席时对应用例跳过)
    from PIL import Image as PilImage

    HAS_PIL = True
except ImportError:  # pragma: no cover - 环境无 Pillow
    PilImage = None  # type: ignore[assignment]
    HAS_PIL = False


# ---------------------------------------------------------------------------
# 纯 stdlib PNG 写入器(测试专用;与解码器互为逆运算)
# ---------------------------------------------------------------------------

_CT_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}


def _chunk(ctype: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + ctype
        + data
        + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
    )


def _encode_scanlines(
    rows: list[list[tuple[int, int, int]]], color_type: int, filter_cycle: bool
) -> bytes:
    """把 RGB 三元组行编码为 PNG 原始扫描线流(可选滤波 0-4 轮换)。"""
    bpp = _CT_CHANNELS[color_type]
    raw_rows: list[bytes] = []
    for row in rows:
        buf = bytearray()
        for (r, g, b) in row:
            if color_type == 0:  # 灰度:调用方传 (v, v, v),取 v
                buf.append(r)
            elif color_type == 2:
                buf += bytes((r, g, b))
            elif color_type == 4:
                buf += bytes((r, 255))
            else:  # 6
                buf += bytes((r, g, b, 255))
        raw_rows.append(bytes(buf))

    out = bytearray()
    prev = bytes(len(raw_rows[0]))
    for idx, line in enumerate(raw_rows):
        ftype = (idx % 5) if filter_cycle else 0
        out.append(ftype)
        enc = bytearray(line)
        for i in range(len(line)):
            left = line[i - bpp] if i >= bpp else 0
            if ftype == 0:
                v = 0
            elif ftype == 1:  # Sub
                v = left
            elif ftype == 2:  # Up
                v = prev[i]
            elif ftype == 3:  # Average
                v = (left + prev[i]) >> 1
            else:  # Paeth
                v = hk._paeth_predictor(left, prev[i], prev[i - bpp] if i >= bpp else 0)
            enc[i] = (line[i] - v) & 0xFF
        out += enc
        prev = line
    return bytes(out)


def _write_png(
    path,
    rows: list[list[tuple[int, int, int]]],
    *,
    color_type: int = 2,
    filter_cycle: bool = False,
    bit_depth: int = 8,
    interlace: int = 0,
) -> str:
    """落盘一张 8bit 非隔行 PNG(bit_depth/interlace 可故意写非法值测拒绝)。"""
    h, w = len(rows), len(rows[0])
    ihdr = struct.pack(">IIBBBBB", w, h, bit_depth, color_type, 0, 0, interlace)
    raw = _encode_scanlines(rows, color_type, filter_cycle)
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )
    Path(path).write_bytes(payload)
    return str(path)


def _ev(path) -> ImageEvidence:
    return ImageEvidence(
        path=str(path), url="http://127.0.0.1/img/x.png", source_page="http://127.0.0.1/"
    )


def _ellipse_rows(
    w: int,
    h: int,
    rx: float,
    ry: float,
    tone: tuple[int, int, int],
    bg: tuple[int, int, int] = (30, 40, 60),
    cx: float = 0.5,
    cy: float = 0.5,
) -> list[list[tuple[int, int, int]]]:
    """确定性合成:深蓝底 + 居中肤色椭圆(测试本地构造器)。"""
    inv_rx2, inv_ry2 = 1.0 / (rx * rx), 1.0 / (ry * ry)
    rows = []
    for y in range(h):
        dy = y / h - cy
        rows.append(
            [
                tone if ((x / w - cx) ** 2) * inv_rx2 + dy * dy * inv_ry2 <= 1.0 else bg
                for x in range(w)
            ]
        )
    return rows


ROWS_2X3 = [
    [(224, 172, 150), (30, 40, 60), (255, 255, 255)],
    [(0, 0, 0), (90, 140, 215), (55, 115, 65)],
]


# ---------------------------------------------------------------------------
# 注册与工厂
# ---------------------------------------------------------------------------


def test_v7_registry_name_skin() -> None:
    """模块导入即注册 "skin";工厂按名取到本类,实例名一致。"""
    from netsentinel.vision import classifier_base

    assert "skin" in classifier_base._REGISTRY
    clf = classifier_base.get_classifier("skin", Config())
    assert isinstance(clf, SkinHeuristicClassifier)
    assert clf.name == "skin"


# ---------------------------------------------------------------------------
# 纯 stdlib PNG 读取器
# ---------------------------------------------------------------------------


def test_v7_png_decoder_rgb(tmp_path) -> None:
    out = read_png_rgb(_write_png(tmp_path / "rgb.png", ROWS_2X3, color_type=2))
    assert out[0] == 3 and out[1] == 2
    assert out[2] == ROWS_2X3


def test_v7_png_decoder_rgba(tmp_path) -> None:
    out = read_png_rgb(_write_png(tmp_path / "rgba.png", ROWS_2X3, color_type=6))
    assert (out[0], out[1]) == (3, 2)
    assert out[2] == ROWS_2X3  # Alpha 通道被丢弃,RGB 原样


def test_v7_png_decoder_gray(tmp_path) -> None:
    rows = [[(100, 100, 100), (0, 0, 0), (255, 255, 255)]]
    out = read_png_rgb(_write_png(tmp_path / "gray.png", rows, color_type=0))
    assert out[2] == rows  # 灰度按 (v, v, v) 展开


def test_v7_png_decoder_gray_alpha(tmp_path) -> None:
    rows = [[(77, 77, 77), (200, 200, 200)]]
    out = read_png_rgb(_write_png(tmp_path / "graya.png", rows, color_type=4))
    assert out[2] == rows


def test_v7_png_filter_cycle_roundtrip(tmp_path) -> None:
    """滤波 0-4 轮换编码 vs 全 0 编码:解码结果与原始像素逐位一致。"""
    w, h, rows = synthetic_skin_pixels(0)
    plain = read_png_rgb(_write_png(tmp_path / "f0.png", rows, color_type=2))
    cycled = read_png_rgb(
        _write_png(tmp_path / "f4.png", rows, color_type=2, filter_cycle=True)
    )
    assert plain[2] == rows and cycled[2] == rows
    assert (plain[0], plain[1]) == (w, h)


def test_v7_png_interlaced_rejected(tmp_path, monkeypatch) -> None:
    """隔行(Adam7)不支持:解码抛 PngDecodeError,classify 容错为 error 分。"""
    p = _write_png(
        tmp_path / "inter.png", [[(10, 10, 10)] * 4] * 4, color_type=2, interlace=1
    )
    with pytest.raises(hk.PngDecodeError):
        read_png_rgb(p)
    monkeypatch.setattr(hk, "_try_pil", lambda: None)
    score = SkinHeuristicClassifier().classify(_ev(p))
    assert score.nsfw_prob == 0.0 and "error" in score.scores


def test_v7_png_bitdepth16_rejected(tmp_path) -> None:
    p = _write_png(
        tmp_path / "d16.png", [[(10, 10, 10)] * 4] * 4, color_type=2, bit_depth=16
    )
    with pytest.raises(hk.PngDecodeError, match="8bit"):
        read_png_rgb(p)


def test_v7_png_bad_crc_rejected(tmp_path) -> None:
    """IDAT 数据区翻转一字节 → CRC 校验失败拒绝。"""
    p = Path(_write_png(tmp_path / "crc.png", ROWS_2X3, color_type=2))
    data = bytearray(p.read_bytes())
    i = data.index(b"IDAT") + 4 + 6  # IDAT 数据区内某字节
    data[i] ^= 0xFF
    p.write_bytes(bytes(data))
    with pytest.raises(hk.PngDecodeError, match="CRC"):
        read_png_rgb(p)


def test_v7_png_truncated_rejected(tmp_path) -> None:
    """IDAT 中途截断 → 块越界/数据不足拒绝。"""
    payload = Path(
        _write_png(tmp_path / "trunc.png", _ellipse_rows(64, 64, 0.4, 0.4, (224, 172, 150)))
    ).read_bytes()
    i = payload.index(b"IDAT")
    cut = payload[: i + 4 + max(1, (len(payload) - i - 16) // 2)]
    (tmp_path / "trunc2.png").write_bytes(cut)
    with pytest.raises(hk.PngDecodeError):
        read_png_rgb(tmp_path / "trunc2.png")


# ---------------------------------------------------------------------------
# 容错分支
# ---------------------------------------------------------------------------


def test_v7_missing_file_error_score(tmp_path) -> None:
    score = SkinHeuristicClassifier().classify(_ev(tmp_path / "none.png"))
    assert score.model == "skin"
    assert score.nsfw_prob == 0.0
    assert "error" in score.scores


def test_v7_non_png_without_pil_errors(tmp_path, monkeypatch) -> None:
    """缺 PIL + JPEG 魔数:直接 error 分(不崩溃、不猜测)。"""
    jpg = tmp_path / "fake.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64)
    monkeypatch.setitem(sys.modules, "PIL", None)  # 模拟未安装(双导入口径下可靠)
    score = SkinHeuristicClassifier().classify(_ev(jpg))
    assert score.nsfw_prob == 0.0
    assert "error" in score.scores
    assert "PNG" in score.scores["error"] or "Pillow" in score.scores["error"]


# ---------------------------------------------------------------------------
# PIL 路径与双路径一致性
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_PIL, reason="需要 Pillow 才能测试 JPEG 路径")
def test_v7_jpeg_with_pil(tmp_path) -> None:
    """装了 PIL 时 JPEG 可解:打分成功且三特征齐全。"""
    jpg = tmp_path / "skin.jpg"
    im = PilImage.new("RGB", (260, 210), (30, 40, 60))
    px = im.load()
    for y in range(210):
        for x in range(260):
            if ((x - 130) / 104.0) ** 2 + ((y - 105) / 94.0) ** 2 <= 1.0:
                px[x, y] = (224, 172, 150)
    im.save(jpg, format="JPEG", quality=95)
    score = SkinHeuristicClassifier().classify(_ev(jpg))
    assert "error" not in score.scores
    assert score.nsfw_prob > 0.5
    assert set(score.scores) == {"skin_ratio", "max_blob", "edge_density", "small"}


def test_v7_stdlib_png_classify_features(tmp_path, monkeypatch) -> None:
    """强制无 PIL:纯 stdlib 解码 PNG 并打分(零三方依赖链路)。"""
    monkeypatch.setattr(hk, "_try_pil", lambda: None)
    w, h, rows = synthetic_skin_pixels(0)
    p = _write_png(tmp_path / "skin0.png", rows, color_type=2)
    score = SkinHeuristicClassifier().classify(_ev(p))
    assert score.model == "skin"
    assert set(score.scores) == {"skin_ratio", "max_blob", "edge_density", "small"}
    assert score.nsfw_prob > 0.5
    assert score.scores["small"] is False  # 240x240 ≥ 默认 min_image_px=200


@pytest.mark.skipif(not HAS_PIL, reason="需要 Pillow 才能对比双路径")
def test_v7_pil_and_stdlib_agree(tmp_path, monkeypatch) -> None:
    """同一 PNG:PIL 路径与 stdlib 路径给出逐位一致的特征与总分(确定性)。"""
    w, h, rows = synthetic_clean_pixels(1)  # 文本图:边缘特征非平凡
    p = _write_png(tmp_path / "text.png", rows, color_type=2)
    via_pil = SkinHeuristicClassifier().classify(_ev(p))
    monkeypatch.setattr(hk, "_try_pil", lambda: None)
    via_std = SkinHeuristicClassifier().classify(_ev(p))
    assert via_pil.nsfw_prob == via_std.nsfw_prob
    assert via_pil.scores == via_std.scores


# ---------------------------------------------------------------------------
# 特征语义 / 小图 / 公式
# ---------------------------------------------------------------------------


def test_v7_skin_vs_clean_feature_ranges(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(hk, "_try_pil", lambda: None)
    clf = SkinHeuristicClassifier()
    sp = _write_png(tmp_path / "s.png", list(synthetic_skin_pixels(3)[2]), color_type=2)
    s = clf.classify(_ev(sp)).scores
    assert 0.40 < s["skin_ratio"] < 0.70  # 椭圆面积占比 π·rx·ry
    assert 0.40 <= s["max_blob"] <= 0.85
    assert 0.0 <= s["edge_density"] < 0.20
    for k, gen in ((0, synthetic_clean_pixels), (1, synthetic_clean_pixels)):
        cp = _write_png(tmp_path / f"c{k}.png", list(gen(k)[2]), color_type=2)
        c = clf.classify(_ev(cp)).scores
        assert c["skin_ratio"] == 0.0  # 风景/文本零肤色像素
        assert c["max_blob"] == 0.0
        assert 0.0 <= c["edge_density"] < 0.45


def test_v7_small_image_flag(tmp_path, monkeypatch) -> None:
    """小图(< min_image_px)照常计算,仅标记 small=True。"""
    monkeypatch.setattr(hk, "_try_pil", lambda: None)
    clf = SkinHeuristicClassifier()
    small_p = _write_png(
        tmp_path / "small.png", _ellipse_rows(64, 64, 0.40, 0.45, (224, 172, 150)), color_type=2
    )
    s = clf.classify(_ev(small_p))
    assert s.scores["small"] is True  # 64 < 200(宽与高均低于判定线)
    assert s.nsfw_prob > 0.5  # 照算
    big_p = _write_png(
        tmp_path / "big.png", _ellipse_rows(240, 240, 0.40, 0.45, (224, 172, 150)), color_type=2
    )
    assert clf.classify(_ev(big_p)).scores["small"] is False


def test_v7_min_image_px_from_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(hk, "_try_pil", lambda: None)
    p = _write_png(
        tmp_path / "s64.png", _ellipse_rows(64, 64, 0.40, 0.45, (224, 172, 150)), color_type=2
    )
    assert SkinHeuristicClassifier().min_image_px == 200
    assert SkinHeuristicClassifier(None).min_image_px == 200
    clf = SkinHeuristicClassifier(Config(min_image_px=64))
    assert clf.min_image_px == 64
    assert clf.classify(_ev(p)).scores["small"] is False  # 达标线降到 64 → 非小图


def test_v7_prob_formula_exact(tmp_path, monkeypatch) -> None:
    """中等信号图(不触发钳制):返回分与公式复算逐位相等。"""
    monkeypatch.setattr(hk, "_try_pil", lambda: None)
    p = _write_png(
        tmp_path / "mid.png",
        _ellipse_rows(240, 240, 0.20, 0.22, (224, 172, 150)),
        color_type=2,
    )
    score = SkinHeuristicClassifier().classify(_ev(p))
    s = score.scores
    expected = min(
        max(
            hk.W_SKIN_RATIO * s["skin_ratio"]
            + hk.W_MAX_BLOB * s["max_blob"]
            + hk.W_EDGE_DENSITY * s["edge_density"]
            + hk.BIAS,
            0.0,
        ),
        hk.PROB_CAP,
    )
    assert score.nsfw_prob == expected
    assert 0.0 < score.nsfw_prob < hk.PROB_CAP  # 确认双侧均未钳制


def test_v7_prob_cap_098(tmp_path, monkeypatch) -> None:
    """满幅肤色:原始分 2.35 被钳到 0.98(启发式不给满格置信)。"""
    monkeypatch.setattr(hk, "_try_pil", lambda: None)
    rows = [[(224, 172, 150)] * 64 for _ in range(64)]
    p = _write_png(tmp_path / "full.png", rows, color_type=2)
    score = SkinHeuristicClassifier().classify(_ev(p))
    assert score.nsfw_prob == hk.PROB_CAP == 0.98


def test_v7_classify_batch_order(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(hk, "_try_pil", lambda: None)
    clean = _write_png(
        tmp_path / "c.png", [[(90, 140, 215)] * 32] * 32, color_type=2
    )
    skin = _write_png(
        tmp_path / "s.png", _ellipse_rows(64, 64, 0.42, 0.46, (210, 160, 140)), color_type=2
    )
    clf = SkinHeuristicClassifier()
    scores = clf.classify_batch([_ev(clean), _ev(skin)])
    assert [s.model for s in scores] == ["skin", "skin"]  # 顺序保持
    assert scores[0].nsfw_prob < scores[1].nsfw_prob


# ---------------------------------------------------------------------------
# bench(红线 31:操作计数 / 确定性构造数据,零墙钟)
# ---------------------------------------------------------------------------


def _bench_corpus(tmp_path):
    """落盘 8 张肤色椭圆图 + 8 张风景/文本图(纯 stdlib 构造)。"""
    monkeypatch_files = []
    for k in range(8):
        w, h, rows = synthetic_skin_pixels(k)
        monkeypatch_files.append(_write_png(tmp_path / f"skin_{k}.png", rows, color_type=2))
    for k in range(8):
        w, h, rows = synthetic_clean_pixels(k)
        monkeypatch_files.append(_write_png(tmp_path / f"clean_{k}.png", rows, color_type=2))
    return monkeypatch_files[:8], monkeypatch_files[8:]


def test_v7_bench_linear_separability(tmp_path, monkeypatch) -> None:
    """8 肤色图 vs 8 风景/文本图(强制无 PIL,纯 stdlib 链路):

    均值差 > 0.2(契约),且以 max(clean)/min(skin) 中点为阈值分类零错分
    —— 两侧线性可分。全程确定性合成数据,无墙钟。
    """
    monkeypatch.setattr(hk, "_try_pil", lambda: None)
    clf = SkinHeuristicClassifier()
    skin_files, clean_files = _bench_corpus(tmp_path)
    assert len(skin_files) >= 8 and len(clean_files) >= 8
    skin_probs = [clf.classify(_ev(p)).nsfw_prob for p in skin_files]
    clean_probs = [clf.classify(_ev(p)).nsfw_prob for p in clean_files]
    mean_gap = sum(skin_probs) / len(skin_probs) - sum(clean_probs) / len(clean_probs)
    assert mean_gap > 0.2, f"均值差不足:{mean_gap:.4f}"
    threshold = (max(clean_probs) + min(skin_probs)) / 2.0
    errors = sum(1 for p in clean_probs if p > threshold) + sum(
        1 for p in skin_probs if p <= threshold
    )
    assert errors == 0, f"阈值 {threshold:.4f} 下存在错分"
    assert min(skin_probs) > 0.5 and max(clean_probs) < 0.1  # 量化间隔


def test_v7_bench_flood_fill_ops(tmp_path, monkeypatch) -> None:
    """flood fill 操作计数:任意图 ≤ 64(每格至多访问一次);

    满肤图恰 64(单连通块全格),纯背景图恰 0 —— 计数确定性,禁墙钟。
    """
    monkeypatch.setattr(hk, "_try_pil", lambda: None)
    clf = SkinHeuristicClassifier()
    skin_files, clean_files = _bench_corpus(tmp_path)
    for p in skin_files + clean_files:
        clf.classify(_ev(p))
        assert 0 <= clf.last_flood_ops <= hk.GRID * hk.GRID
    full = _write_png(
        tmp_path / "full.png", [[(224, 172, 150)] * 64 for _ in range(64)], color_type=2
    )
    clf.classify(_ev(full))
    assert clf.last_flood_ops == 64  # 8x8 全肤单块,每格恰一次出栈
    bg = _write_png(
        tmp_path / "bg.png", [[(90, 140, 215)] * 64 for _ in range(64)], color_type=2
    )
    clf.classify(_ev(bg))
    assert clf.last_flood_ops == 0


# ---------------------------------------------------------------------------
# kernel_selfcheck(A138 总控入口)
# ---------------------------------------------------------------------------


def test_v7_kernel_selfcheck() -> None:
    d = kernel_selfcheck()
    assert set(d) >= {"name", "metric", "value", "baseline"}
    assert d["name"] == "skin"
    assert d["baseline"] >= 0.2
    assert d["value"] > d["baseline"], f"自检均值差不足:{d}"
    assert kernel_selfcheck() == d  # 确定性:两次调用完全一致
