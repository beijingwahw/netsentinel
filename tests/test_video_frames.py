"""A48 video_frames 测试(离线,只写 tmp_path)。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.vision import video_frames

PIL = pytest.importorskip("PIL", reason="需要 Pillow 才能测试 GIF 帧采样")
from PIL import Image  # noqa: E402

# 12 种互不相同的纯色,支撑 n=1/4/12 三档多帧 GIF(帧色不同)
_COLORS = [
    (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0),
    (255, 0, 255), (0, 255, 255), (128, 0, 0), (0, 128, 0),
    (0, 0, 128), (128, 128, 0), (128, 0, 128), (0, 128, 128),
]


def _make_gif(path, n: int) -> None:
    """生成 n 帧纯色 GIF(每帧颜色不同)。"""
    frames = [Image.new("RGB", (40, 30), _COLORS[i % len(_COLORS)]) for i in range(n)]
    if n == 1:
        frames[0].save(path, format="GIF")
    else:
        frames[0].save(
            path, format="GIF", save_all=True, append_images=frames[1:], duration=80, loop=0
        )


def _cfg(max_frames: int = 6) -> Config:
    cfg = Config()
    cfg.video_max_frames = max_frames
    return cfg


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# GIF 采样:n=1 / 4 / 12
# ---------------------------------------------------------------------------

def test_single_frame_gif(tmp_path):
    src = tmp_path / "anim1.gif"
    _make_gif(src, 1)
    out = tmp_path / "out"
    evs = video_frames.sample_frames(str(src), _cfg(), str(out))
    assert len(evs) == 1  # min(1, 6)
    ev = evs[0]
    assert "anim1" in ev.path and "frame" in _basename(ev.path)
    assert _basename(ev.path) == "anim1_frame00.png"
    assert ev.width == 40 and ev.height == 30
    assert len(ev.sha256) == 64 and int(ev.sha256, 16) >= 0  # 合法 hex
    assert ev.url.endswith("#frame:0")
    assert ev.source_page == ""
    from pathlib import Path
    assert Path(ev.path).is_file()


def test_four_frames_all_taken(tmp_path):
    src = tmp_path / "nsfw_hi_anim4.gif"
    _make_gif(src, 4)
    out = tmp_path / "out"
    evs = video_frames.sample_frames(str(src), _cfg(), str(out))
    assert len(evs) == 4  # min(4, 6) → 全取
    names = [_basename(e.path) for e in evs]
    assert names == ["nsfw_hi_anim4_frame00.png", "nsfw_hi_anim4_frame01.png",
                     "nsfw_hi_anim4_frame02.png", "nsfw_hi_anim4_frame03.png"]
    for i, ev in enumerate(evs):
        assert "nsfw_hi" in ev.path  # 原关键词保留,stub 规则链路可用
        assert ev.url.endswith(f"#frame:{i}")
        assert ev.width == 40 and ev.height == 30 and len(ev.sha256) == 64
        from pathlib import Path
        assert Path(ev.path).is_file()
    assert len({e.sha256 for e in evs}) == 4  # 帧色不同 → 内容不同


def test_twelve_frames_uniform_sampled(tmp_path):
    src = tmp_path / "anim12.gif"
    _make_gif(src, 12)
    out = tmp_path / "out"
    evs = video_frames.sample_frames(str(src), _cfg(6), str(out))
    assert len(evs) == 6  # min(12, 6) → 均匀采样
    # round(i*(12-1)/(6-1)) → 原始帧号 [0, 2, 4, 7, 9, 11]
    names = [_basename(e.path) for e in evs]
    assert names == ["anim12_frame00.png", "anim12_frame02.png", "anim12_frame04.png",
                     "anim12_frame07.png", "anim12_frame09.png", "anim12_frame11.png"]
    for idx, ev in zip([0, 2, 4, 7, 9, 11], evs):
        assert ev.url.endswith(f"#frame:{idx}")


def test_max_frames_config_override(tmp_path):
    src = tmp_path / "anim12b.gif"
    _make_gif(src, 12)
    evs = video_frames.sample_frames(src, _cfg(3), tmp_path / "out")
    assert len(evs) == 3
    assert _basename(evs[0].path) == "anim12b_frame00.png"
    assert _basename(evs[-1].path) == "anim12b_frame11.png"  # 首尾帧必采


def test_frame_colors_distinct(tmp_path):
    src = tmp_path / "colors4.gif"
    _make_gif(src, 4)
    evs = video_frames.sample_frames(src, _cfg(), tmp_path / "out")
    pixels = set()
    for ev in evs:
        with Image.open(ev.path) as im:
            pixels.add(im.getpixel((5, 5)))
    assert len(pixels) == 4  # 采样帧确为互不相同的内容


def test_uppercase_gif_extension(tmp_path):
    src = tmp_path / "UPPER.GIF"
    _make_gif(src, 2)
    evs = video_frames.sample_frames(src, _cfg(), tmp_path / "out")
    assert len(evs) == 2
    assert "UPPER" in evs[0].path


def test_url_and_source_page_kwargs(tmp_path):
    src = tmp_path / "linked.gif"
    _make_gif(src, 2)
    evs = video_frames.sample_frames(
        src, _cfg(), tmp_path / "out",
        url="http://127.0.0.1/img/linked.gif", source_page="http://127.0.0.1/gallery",
    )
    assert [e.url for e in evs] == [
        "http://127.0.0.1/img/linked.gif#frame:0",
        "http://127.0.0.1/img/linked.gif#frame:1",
    ]
    assert all(e.source_page == "http://127.0.0.1/gallery" for e in evs)


def test_outdir_autocreated_nested(tmp_path):
    src = tmp_path / "nested_dir.gif"
    _make_gif(src, 3)
    out = tmp_path / "deep" / "nested" / "frames"
    evs = video_frames.sample_frames(src, _cfg(), out)
    assert evs and out.is_dir()


def test_zero_max_frames_returns_empty(tmp_path):
    src = tmp_path / "zero.gif"
    _make_gif(src, 4)
    assert video_frames.sample_frames(src, _cfg(0), tmp_path / "out") == []


# ---------------------------------------------------------------------------
# 降级分支:视频容器 / 文件不存在 / 无 PIL / 损坏 GIF
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ext", ["mp4", "mov", "mkv", "webm"])
def test_video_containers_return_empty_with_hint(tmp_path, ext, caplog):
    src = tmp_path / f"clip.{ext}"
    src.write_bytes(b"\x00\x00\x00\x18ftypmp42......")  # 假视频文件
    with caplog.at_level(logging.INFO, logger="netsentinel.vision.video_frames"):
        evs = video_frames.sample_frames(src, _cfg(), tmp_path / "out")
    assert evs == []  # 不抛异常
    assert any("ffmpeg" in r.message and "GIF" in r.message for r in caplog.records)


def test_missing_file_returns_empty(tmp_path):
    assert video_frames.sample_frames(
        tmp_path / "ghost.gif", _cfg(), tmp_path / "out") == []


def test_unsupported_extension_returns_empty(tmp_path):
    src = tmp_path / "photo.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert video_frames.sample_frames(src, _cfg(), tmp_path / "out") == []


def test_no_pil_returns_empty(tmp_path, monkeypatch):
    src = tmp_path / "nopil.gif"
    _make_gif(src, 4)  # 先落盘真实 GIF,再屏蔽 PIL
    monkeypatch.setitem(sys.modules, "PIL", None)
    assert video_frames.sample_frames(src, _cfg(), tmp_path / "out") == []


def test_corrupt_gif_returns_empty(tmp_path):
    src = tmp_path / "broken.gif"
    src.write_bytes(b"GIF89a\x01\x00\x01\x00\x00 garbage not a gif")
    assert video_frames.sample_frames(src, _cfg(), tmp_path / "out") == []


# ---------------------------------------------------------------------------
# is_supported
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("a.gif", True), ("a.GIF", True), ("a.Gif", True),
    ("a.mp4", True), ("a.MOV", True), ("a.mov", True),
    ("a.mkv", True), ("a.webm", True), ("a.WebM", True),
    ("a.png", False), ("a.jpg", False), ("a.txt", False), ("noext", False),
])
def test_is_supported(name, expected):
    assert video_frames.is_supported(name) is expected


# ---------------------------------------------------------------------------
# V5 升级:遥测 / 单遍编码哈希 / 半途产物清理
# ---------------------------------------------------------------------------


def test_v5_telemetry_counts_sampled_frames(tmp_path):
    """按实际采样帧数累加 video.frames;坏文件/跳过路径不计。"""
    telemetry.reset()
    try:
        full = tmp_path / "full.gif"
        _make_gif(full, 4)
        sampled = tmp_path / "sampled.gif"
        _make_gif(sampled, 12)
        broken = tmp_path / "broken.gif"
        broken.write_bytes(b"GIF89a garbage not a gif")

        assert len(video_frames.sample_frames(full, _cfg(), tmp_path / "o1")) == 4
        assert len(video_frames.sample_frames(sampled, _cfg(6), tmp_path / "o2")) == 6
        assert video_frames.sample_frames(broken, _cfg(), tmp_path / "o3") == []
        assert telemetry.snapshot()["counters"]["video.frames"] == 10  # 4+6,坏文件 0
    finally:
        telemetry.reset()


def test_v5_frame_sha256_matches_bytes_on_disk(tmp_path):
    """单遍落盘:证据 sha256 与磁盘字节一致(哈希来自同一份内存字节)。"""
    import hashlib

    src = tmp_path / "hash.gif"
    _make_gif(src, 2)
    evs = video_frames.sample_frames(src, _cfg(), tmp_path / "out")
    assert len(evs) == 2
    for ev in evs:
        assert ev.sha256 == hashlib.sha256(Path(ev.path).read_bytes()).hexdigest()


def test_v5_partial_write_failure_cleans_halfway_frames(tmp_path, monkeypatch, caplog):
    """第 3 帧落盘失败:整体返回 [],并清理已写的帧,不留孤儿证据。"""
    src = tmp_path / "flaky.gif"
    _make_gif(src, 4)  # 先在补丁外生成好 GIF
    out = tmp_path / "out"
    real_save = Image.Image.save
    state = {"n": 0}

    def flaky_save(self, fp, format=None, **kwargs):
        state["n"] += 1
        if state["n"] == 3:
            raise OSError("模拟落盘中途失败")
        return real_save(self, fp, format, **kwargs)

    monkeypatch.setattr(Image.Image, "save", flaky_save)
    with caplog.at_level(logging.WARNING, logger="netsentinel.vision.video_frames"):
        evs = video_frames.sample_frames(src, _cfg(), out)
    assert evs == []
    assert list(out.glob("*_frame*.png")) == []  # 前 2 帧已写也被清掉
    assert any("GIF" in r.getMessage() for r in caplog.records)
