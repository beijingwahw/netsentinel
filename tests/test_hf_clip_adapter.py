"""A07 —— HFCLIPClassifier(HuggingFace CLIP NSFW 分类器适配器)测试。

全部离线:绝不安装/导入真实 transformers,绝不下载模型。
通过注入 FakePipeline 驱动分类逻辑;Pillow 缺失的环境里额外注入一个
最小假 PIL(仅 open/convert),使图片读取路径同样可测。
"""
from __future__ import annotations

import errno
import logging
import pathlib
import struct
import sys
import threading
import time
import types
import zlib

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence

# 兄弟模块 classifier_base(A05)并行开发中,可能尚未就位:importorskip 容错。
classifier_base = pytest.importorskip("netsentinel.vision.classifier_base")
clip_mod = pytest.importorskip("netsentinel.vision.hf_clip_adapter")
HFCLIPClassifier = clip_mod.HFCLIPClassifier


# ---------------------------------------------------------------------------
# 测试素材
# ---------------------------------------------------------------------------

def write_png(path, rgb: tuple[int, int, int] = (255, 0, 0),
              width: int = 1, height: int = 1) -> pathlib.Path:
    """用 stdlib zlib+struct 手写一个真实的小 PNG(默认 1x1 纯红)。"""
    path = pathlib.Path(path)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data)))

    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    png = (b"\x89PNG\r\n\x1a\n" + ihdr
           + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    path.write_bytes(png)
    return path


def make_evidence(path) -> ImageEvidence:
    """由本地文件路径构造一条受检图片证据。"""
    return ImageEvidence(
        path=str(path),
        url="http://127.0.0.1/img/x.png",
        source_page="http://127.0.0.1/",
    )


class FakePipeline:
    """transformers pipeline 替身:返回构造时给定的结果或抛出给定异常。"""

    def __init__(self, results: list[dict] | None = None, error: Exception | None = None):
        self.results = list(results) if results is not None else []
        self.error = error
        self.calls = 0

    def __call__(self, image):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.results


def _real_pil_available() -> bool:
    try:
        import PIL.Image  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.fixture
def ensure_pil(monkeypatch):
    """保证 classify() 内的 ``from PIL import Image`` 可用。

    真实 Pillow 存在则直接使用(顺便真实验证手写 PNG 的正确性);
    缺失时向 sys.modules 注入最小假 PIL(open + convert("RGB"))。
    """
    if _real_pil_available():
        return "real"

    class _UnidentifiedImageError(OSError):
        pass

    class _FakeImageFile:
        def __init__(self, path: str):
            self.path = path

        def convert(self, mode: str):
            if mode != "RGB":
                raise ValueError(f"假 PIL 不支持的转换模式: {mode}")
            return self

    def _open(path):
        p = pathlib.Path(path)
        if not p.is_file():
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(path))
        if not p.read_bytes().startswith(b"\x89PNG"):
            raise _UnidentifiedImageError(f"cannot identify image file {str(path)!r}")
        return _FakeImageFile(str(path))

    fake_pil = types.ModuleType("PIL")
    fake_image = types.ModuleType("PIL.Image")
    fake_image.open = _open
    fake_image.UnidentifiedImageError = _UnidentifiedImageError
    fake_pil.Image = fake_image
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", fake_image)
    return "fake"


# ---------------------------------------------------------------------------
# 注册与基础行为
# ---------------------------------------------------------------------------

def test_registered_as_clip(monkeypatch):
    """模块导入时已把 HFCLIPClassifier 注册为 "clip"。

    无 transformers 时工厂实例化会抛出本适配器的中文安装提示,恰好证明
    注册表里是 HFCLIPClassifier(若未注册,工厂应抛 ValueError 而非 ImportError)。
    """
    assert issubclass(HFCLIPClassifier, classifier_base.NsfwClassifier)
    assert HFCLIPClassifier.name == "clip"
    monkeypatch.setitem(sys.modules, "transformers", None)
    with pytest.raises(ImportError, match=r"未安装 transformers/torch"):
        classifier_base.get_classifier("clip", Config())


def test_classify_batch_default_loop(tmp_path, ensure_pil):
    """classify_batch 继承基类默认循环实现,逐张调用 classify。"""
    png_a = write_png(tmp_path / "a.png")
    png_b = write_png(tmp_path / "b.png", rgb=(0, 255, 0))
    pipe = FakePipeline(results=[{"label": "nsfw", "score": 0.6}])
    clf = HFCLIPClassifier(pipeline=pipe)
    scores = clf.classify_batch([make_evidence(png_a), make_evidence(png_b)])
    assert len(scores) == 2
    assert [s.nsfw_prob for s in scores] == pytest.approx([0.6, 0.6])
    assert pipe.calls == 2


# ---------------------------------------------------------------------------
# 标签 -> 概率映射
# ---------------------------------------------------------------------------

def test_nsw_label_maps_to_score(tmp_path, ensure_pil):
    """输出含 nsfw 标签:nsfw_prob 直接取其 score,raw 结果原样透传。"""
    png = write_png(tmp_path / "c.png")
    results = [{"label": "nsfw", "score": 0.93}, {"label": "sfw", "score": 0.07}]
    pipe = FakePipeline(results=results)
    clf = HFCLIPClassifier(cfg=None, pipeline=pipe)
    score = clf.classify(make_evidence(png))
    assert score.nsfw_prob == pytest.approx(0.93)
    assert score.model == "clip"
    assert score.image.path == str(png)
    assert score.scores["raw"] == results
    assert pipe.calls == 1


def test_sfw_only_maps_to_one_minus(tmp_path, ensure_pil):
    """输出缺 nsfw 标签、只有 sfw:nsfw_prob = 1 - sfw_score。"""
    png = write_png(tmp_path / "d.png")
    pipe = FakePipeline(results=[{"label": "sfw", "score": 0.25}])
    clf = HFCLIPClassifier(pipeline=pipe)
    score = clf.classify(make_evidence(png))
    assert score.nsfw_prob == pytest.approx(0.75)
    assert pipe.calls == 1


def test_unknown_labels_conservative_zero(tmp_path, ensure_pil):
    """输出既无 nsfw 也无 sfw 标签:保守记 0 分(绝不误报)。"""
    png = write_png(tmp_path / "e.png")
    pipe = FakePipeline(results=[{"label": "cat", "score": 0.9}])
    clf = HFCLIPClassifier(pipeline=pipe)
    score = clf.classify(make_evidence(png))
    assert score.nsfw_prob == 0.0
    assert pipe.calls == 1


# ---------------------------------------------------------------------------
# 失败路径:异常 -> 0 分 + error + warning
# ---------------------------------------------------------------------------

def test_pipeline_exception_returns_zero(tmp_path, ensure_pil, caplog):
    """pipeline 抛异常:nsfw_prob=0.0,scores 记录 error。"""
    png = write_png(tmp_path / "f.png")
    pipe = FakePipeline(error=RuntimeError("推理崩溃"))
    clf = HFCLIPClassifier(pipeline=pipe)
    with caplog.at_level(logging.WARNING, logger="netsentinel.vision.hf_clip_adapter"):
        score = clf.classify(make_evidence(png))
    assert score.nsfw_prob == 0.0
    assert "推理崩溃" in score.scores["error"]
    assert pipe.calls == 1
    assert any("CLIP" in rec.getMessage() for rec in caplog.records)


def test_missing_file_returns_zero_with_warning(tmp_path, ensure_pil, caplog):
    """文件不存在:0 分 + error + warning 日志,且不调用 pipeline。"""
    pipe = FakePipeline(results=[{"label": "nsfw", "score": 0.99}])
    clf = HFCLIPClassifier(pipeline=pipe)
    with caplog.at_level(logging.WARNING, logger="netsentinel.vision.hf_clip_adapter"):
        score = clf.classify(make_evidence(tmp_path / "missing.png"))
    assert score.nsfw_prob == 0.0
    assert "error" in score.scores
    assert pipe.calls == 0
    assert any("CLIP" in rec.getMessage() for rec in caplog.records)


def test_corrupt_file_returns_zero(tmp_path, ensure_pil):
    """文件损坏(非 PNG 内容):0 分 + error,pipeline 不被调用。"""
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"this is definitely not a png image")
    pipe = FakePipeline(results=[{"label": "nsfw", "score": 0.99}])
    clf = HFCLIPClassifier(pipeline=pipe)
    score = clf.classify(make_evidence(bad))
    assert score.nsfw_prob == 0.0
    assert "error" in score.scores
    assert pipe.calls == 0


# ---------------------------------------------------------------------------
# 依赖缺失时的中文安装提示(不真装 transformers / Pillow)
# ---------------------------------------------------------------------------

def test_import_error_hint_when_transformers_missing(monkeypatch):
    """transformers 缺失(sys.modules 置 None 阻断):构造时抛中文提示。"""
    monkeypatch.setitem(sys.modules, "transformers", None)
    with pytest.raises(ImportError, match=r"未安装 transformers/torch.*netsentinel\[clip\]"):
        HFCLIPClassifier()


def test_import_error_hint_when_pil_missing(tmp_path, ensure_pil, monkeypatch):
    """Pillow 缺失(sys.modules 置 None 阻断):classify 时抛中文提示。"""
    png = write_png(tmp_path / "g.png")
    clf = HFCLIPClassifier(pipeline=FakePipeline(results=[{"label": "sfw", "score": 0.5}]))
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setitem(sys.modules, "PIL.Image", None)
    with pytest.raises(ImportError, match="未安装 Pillow"):
        clf.classify(make_evidence(png))


# ---------------------------------------------------------------------------
# V5 升级:线程安全(串行推理)/ 批量逐张协议 / 遥测
# ---------------------------------------------------------------------------


class _ThreadProbePipeline:
    """V5 替身:用独立探针锁记录推理并发峰值,验证共享 pipeline 被串行调用。"""

    def __init__(self, results: list[dict]):
        self.results = list(results)
        self.calls = 0
        self.active = 0
        self.peak = 0
        self._probe = threading.Lock()

    def __call__(self, image):
        with self._probe:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.02)  # 拉长推理窗口:无锁时多线程必然交叠
        with self._probe:
            self.active -= 1
        return self.results


class _SequencePipeline:
    """V5 替身:第 n 次调用返回第 n 组结果,锁定批量"逐张、保序"语义。"""

    def __init__(self, probs: list[float]):
        self.probs = list(probs)
        self.calls = 0

    def __call__(self, image):
        prob = self.probs[min(self.calls, len(self.probs) - 1)]
        self.calls += 1
        return [{"label": "nsfw", "score": prob}]


def test_v5_concurrent_classify_serializes_pipeline_calls(tmp_path, ensure_pil):
    """多线程 classify:模型侧要求串行推理 → pipeline 并发峰值必须为 1。"""
    pngs = [write_png(tmp_path / f"t{i}.png", rgb=(i * 60 % 256, 0, 0)) for i in range(4)]
    probe = _ThreadProbePipeline([{"label": "nsfw", "score": 0.5}])
    clf = HFCLIPClassifier(pipeline=probe)
    results = {}

    def worker(path):
        results[str(path)] = clf.classify(make_evidence(path))

    threads = [threading.Thread(target=worker, args=(p,)) for p in pngs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert probe.peak == 1  # 推理全程串行,无并发交叠
    assert probe.calls == 4
    assert len(results) == 4
    assert all(s.nsfw_prob == pytest.approx(0.5) for s in results.values())


def test_v5_classify_batch_is_per_image_in_order(tmp_path, ensure_pil):
    """注入协议为单图 __call__(image):批量保持逐张,结果与输入一一保序。"""
    pngs = [write_png(tmp_path / f"s{i}.png") for i in range(3)]
    pipe = _SequencePipeline([0.1, 0.2, 0.3])
    clf = HFCLIPClassifier(pipeline=pipe)
    scores = clf.classify_batch([make_evidence(p) for p in pngs])
    assert pipe.calls == 3  # 逐张:每张恰好一次推理
    assert [s.nsfw_prob for s in scores] == pytest.approx([0.1, 0.2, 0.3])


def test_v5_telemetry_records_infer_timer_and_error_counter(tmp_path, ensure_pil):
    """推理计时 clip.infer、失败计数 vision.errors 均落入遥测。"""
    telemetry.reset()
    try:
        png = write_png(tmp_path / "tel.png")
        clf = HFCLIPClassifier(
            pipeline=FakePipeline(results=[{"label": "nsfw", "score": 0.7}])
        )
        clf.classify(make_evidence(png))
        assert telemetry.snapshot()["timers"]["clip.infer"]["count"] >= 1

        clf.classify(make_evidence(tmp_path / "missing.png"))  # 失败路径
        assert telemetry.snapshot()["counters"]["vision.errors"] >= 1
    finally:
        telemetry.reset()
