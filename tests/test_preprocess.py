"""A26 preprocess 测试(离线,只写 tmp_path)。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from netsentinel import telemetry
from netsentinel.contracts import ImageEvidence
from netsentinel.vision import preprocess

PIL = pytest.importorskip("PIL", reason="需要 Pillow 才能测试变体派生")
from PIL import Image  # noqa: E402


def _make_png(path, w, h, rgb=(200, 30, 30)):
    Image.new("RGB", (w, h), rgb).save(path, format="PNG")


def _evidence(path):
    return ImageEvidence(path=str(path), url="http://127.0.0.1/img/x.png",
                         source_page="http://127.0.0.1/")


def test_missing_file_returns_empty(tmp_path):
    assert preprocess.derive_variants(
        _evidence(tmp_path / "none.png"), str(tmp_path / "out")) == []


def test_no_pil_returns_empty(tmp_path, monkeypatch):
    src = tmp_path / "small.png"
    _make_png(src, 200, 200)  # 先落盘,再屏蔽 PIL
    monkeypatch.setitem(sys.modules, "PIL", None)
    assert preprocess.derive_variants(_evidence(src), str(tmp_path / "out")) == []


def test_small_image_upscaled(tmp_path):
    src = tmp_path / "nsfw_hi_small.png"
    _make_png(src, 200, 200)
    out = tmp_path / "out"
    variants = preprocess.derive_variants(_evidence(src), str(out))
    assert len(variants) == 1
    v = variants[0]
    assert v.width == 400 and v.height == 400
    assert v.path.endswith("nsfw_hi_small_up2x.png")
    assert "nsfw_hi" in v.path  # 原关键词保留,stub 链路可用
    assert v.url.endswith("#variant:up2x")
    assert len(v.sha256) == 64


def test_large_image_split_2x2(tmp_path):
    src = tmp_path / "nsfw_hi_grid.png"
    _make_png(src, 800, 800)
    variants = preprocess.derive_variants(_evidence(src), str(tmp_path / "out"))
    assert len(variants) == 4
    names = sorted(v.path.split("\\")[-1] if "\\" in v.path else v.path.split("/")[-1]
                   for v in variants)
    assert any("grid_r0c0" in n for n in names)
    for v in variants:
        assert v.width >= 399 and v.height >= 399  # 切块约 400x400


def test_midsize_image_no_variants(tmp_path):
    src = tmp_path / "normal_mid.png"
    _make_png(src, 450, 450)
    assert preprocess.derive_variants(_evidence(src), str(tmp_path / "out")) == []


def test_corrupt_file_returns_empty(tmp_path):
    src = tmp_path / "bad.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n garbage")
    assert preprocess.derive_variants(_evidence(src), str(tmp_path / "out")) == []


def test_outdir_autocreated(tmp_path):
    src = tmp_path / "s.png"
    _make_png(src, 100, 100)
    out = tmp_path / "nested" / "out"
    variants = preprocess.derive_variants(_evidence(src), str(out))
    assert variants and out.is_dir()


def test_sha256_matches_file(tmp_path):
    import hashlib
    src = tmp_path / "s.png"
    _make_png(src, 120, 90)
    v = preprocess.derive_variants(_evidence(src), str(tmp_path / "out"))[0]
    assert v.sha256 == hashlib.sha256(open(v.path, "rb").read()).hexdigest()


# ---------------------------------------------------------------------------
# V5 升级:单次打开复用 / 单遍编码哈希 / 遥测
# ---------------------------------------------------------------------------


def test_v5_source_image_opened_exactly_once(tmp_path, monkeypatch):
    """Image.open 单次打开复用:任一分支(含派生 4 变体的 2x2 拼图)只解码一次。"""
    real_open = Image.open
    opens: list[str] = []

    def counting_open(fp, *args, **kwargs):
        opens.append(str(fp))
        return real_open(fp, *args, **kwargs)

    monkeypatch.setattr(Image, "open", counting_open)

    grid = tmp_path / "g.png"
    _make_png(grid, 800, 800)
    assert len(preprocess.derive_variants(_evidence(grid), str(tmp_path / "o1"))) == 4
    assert opens == [str(grid)]  # 4 个切块变体共用同一次解码

    small = tmp_path / "s.png"
    _make_png(small, 100, 100)
    assert len(preprocess.derive_variants(_evidence(small), str(tmp_path / "o2"))) == 1
    assert opens == [str(grid), str(small)]  # 每张源图各打开一次


def test_v5_variant_written_bytes_match_evidence_sha256(tmp_path):
    """单遍落盘:证据 sha256 与磁盘字节一致(哈希来自同一份内存字节)。"""
    import hashlib
    src = tmp_path / "grid.png"
    _make_png(src, 700, 700)
    variants = preprocess.derive_variants(_evidence(src), str(tmp_path / "out"))
    assert len(variants) == 4
    for v in variants:
        assert v.sha256 == hashlib.sha256(Path(v.path).read_bytes()).hexdigest()


def test_v5_telemetry_counts_derived_variants(tmp_path):
    """每次派生按实际变体数累加 preprocess.variants(中间尺寸计 0)。"""
    telemetry.reset()
    try:
        small = tmp_path / "a.png"
        _make_png(small, 120, 120)
        grid = tmp_path / "b.png"
        _make_png(grid, 700, 700)
        mid = tmp_path / "c.png"
        _make_png(mid, 450, 450)
        preprocess.derive_variants(_evidence(small), str(tmp_path / "o"))
        preprocess.derive_variants(_evidence(grid), str(tmp_path / "o"))
        preprocess.derive_variants(_evidence(mid), str(tmp_path / "o"))
        assert telemetry.snapshot()["counters"]["preprocess.variants"] == 5  # 1+4+0
    finally:
        telemetry.reset()
