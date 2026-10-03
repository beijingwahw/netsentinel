"""A06 测试:Nudenet 适配器 —— 全部离线,注入 FakeDetector,绝不下载模型。

- 不 import 真实 nudenet 库(环境未安装);惰性导入路径用 monkeypatch 往
  sys.modules 注入假 nudenet 模块来验证 ImportError 提示文案与构造逻辑。
- classifier_base(A05)并行开发中:未就位时仅注册相关用例跳过
  (pytest.importorskip 容错),注入 detector 的用例不依赖基类。
"""
from __future__ import annotations

import logging
import sys
import threading
import time
import types
from typing import Any

import pytest

pytest.importorskip("netsentinel.vision.nudenet_adapter")  # 自身模块导入失败时跳过

from netsentinel import telemetry  # noqa: E402
from netsentinel.contracts import Config, ImageEvidence, ImageScore  # noqa: E402
from netsentinel.vision.nudenet_adapter import (  # noqa: E402
    EXPOSED_FALLBACK_WEIGHT,
    LABEL_WEIGHTS,
    NEUTRAL_WEIGHT,
    NudeNetClassifier,
)


class FakeDetector:
    """离线替身:detect(path) 按 path 返回预置 detections,或抛出预置异常。"""

    def __init__(
        self,
        by_path: dict[str, list[dict[str, Any]]] | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.by_path = dict(by_path or {})
        self.exc = exc
        self.calls: list[str] = []

    def detect(self, path: str) -> list[dict[str, Any]]:
        self.calls.append(path)
        if self.exc is not None:
            raise self.exc
        return self.by_path.get(path, [])


def make_img(path: str = "C:/tmp/dl/a.png") -> ImageEvidence:
    return ImageEvidence(
        path=path,
        url="http://127.0.0.1/img/a.png",
        source_page="http://127.0.0.1/page1",
    )


def make_classifier(detections: list[dict[str, Any]], path: str = "C:/tmp/dl/a.png") -> NudeNetClassifier:
    detector = FakeDetector(by_path={path: detections})
    return NudeNetClassifier(detector=detector)


# ---------------------------------------------------------------------------
# 权重表与标签兼容
# ---------------------------------------------------------------------------

def test_label_weights_contract() -> None:
    assert LABEL_WEIGHTS["EXPOSED_BREAST_F"] == pytest.approx(0.93)
    assert LABEL_WEIGHTS["EXPOSED_BREAST_M"] == pytest.approx(0.93)
    assert LABEL_WEIGHTS["EXPOSED_GENITALIA_F"] == pytest.approx(0.97)
    assert LABEL_WEIGHTS["EXPOSED_GENITALIA_M"] == pytest.approx(0.97)
    assert LABEL_WEIGHTS["EXPOSED_ANUS"] == pytest.approx(0.98)
    # 分支兼容:新旧标签同权
    assert LABEL_WEIGHTS["EXPOSED_BUTTOCKS"] == pytest.approx(0.85)
    assert LABEL_WEIGHTS["EXPOSED_GLUTEUS"] == pytest.approx(0.85)


def test_buttocks_and_gluteus_same_score() -> None:
    old = make_classifier([{"class": "EXPOSED_BUTTOCKS", "score": 0.8}]).classify(make_img())
    new = make_classifier([{"class": "EXPOSED_GLUTEUS", "score": 0.8}]).classify(make_img())
    expected = 0.85 * (0.5 + 0.5 * 0.8)  # 0.765
    assert old.nsfw_prob == pytest.approx(expected)
    assert new.nsfw_prob == pytest.approx(expected)


def test_old_label_field_name_supported() -> None:
    """旧版字段名 label(而非 class)也能解析。"""
    result = make_classifier([{"label": "EXPOSED_BREAST_M", "score": 1.0}]).classify(make_img())
    assert result.nsfw_prob == pytest.approx(0.93 * 1.0)
    assert result.scores["detections"][0]["label"] == "EXPOSED_BREAST_M"


# ---------------------------------------------------------------------------
# classify 打分行为
# ---------------------------------------------------------------------------

def test_exposed_label_scores_high() -> None:
    result = make_classifier([{"class": "EXPOSED_GENITALIA_F", "score": 0.99}]).classify(make_img())
    assert result.nsfw_prob == pytest.approx(0.97 * (0.5 + 0.5 * 0.99))
    assert result.nsfw_prob > 0.9
    assert result.model == "nudenet"
    assert result.image.path == "C:/tmp/dl/a.png"
    assert result.scores["detections"] == [{"label": "EXPOSED_GENITALIA_F", "score": 0.99}]


def test_covered_label_scores_low() -> None:
    result = make_classifier([{"class": "COVERED_BREAST", "score": 0.95}]).classify(make_img())
    assert result.nsfw_prob == pytest.approx(0.05 * (0.5 + 0.5 * 0.95))
    assert result.nsfw_prob < 0.10


def test_multiple_detections_take_max() -> None:
    result = make_classifier(
        [
            {"class": "EXPOSED_BREAST_F", "score": 0.8},    # 0.93*0.90 = 0.837
            {"class": "COVERED_GENITALIA_F", "score": 0.99},  # 0.05*0.995 ≈ 0.05
            {"class": "EXPOSED_ANUS", "score": 0.6},        # 0.98*0.80 = 0.784
        ]
    ).classify(make_img())
    assert result.nsfw_prob == pytest.approx(0.837)  # 取最大,不是均值
    assert len(result.scores["detections"]) == 3


def test_no_detections_minimal_prob() -> None:
    result = make_classifier([]).classify(make_img())
    assert result.nsfw_prob == pytest.approx(0.01)
    assert result.scores["detections"] == []


def test_unknown_label_low_weight() -> None:
    result = make_classifier([{"class": "SOMETHING_WEIRD", "score": 1.0}]).classify(make_img())
    assert result.nsfw_prob == pytest.approx(0.05)


def test_other_exposed_prefix_fallback() -> None:
    result = make_classifier([{"class": "EXPOSED_ELBOW", "score": 1.0}]).classify(make_img())
    assert result.nsfw_prob == pytest.approx(EXPOSED_FALLBACK_WEIGHT)


def test_neutral_body_parts() -> None:
    for label in ("BELLY", "BUTTOCKS", "ARMPITS", "FEET"):
        result = make_classifier([{"class": label, "score": 1.0}]).classify(make_img())
        assert result.nsfw_prob == pytest.approx(NEUTRAL_WEIGHT)


def test_detections_trimmed_to_ten() -> None:
    many = [{"class": f"COVERED_PART_{i}", "score": 0.5} for i in range(12)]
    result = make_classifier(many).classify(make_img())
    assert len(result.scores["detections"]) == 10
    assert result.nsfw_prob == pytest.approx(0.05 * 0.75)


def test_detector_error_returns_zero_and_warns(caplog: Any) -> None:
    detector = FakeDetector(exc=RuntimeError("boom"))
    clf = NudeNetClassifier(detector=detector)
    with caplog.at_level(logging.WARNING):
        result = clf.classify(make_img())
    assert result.nsfw_prob == 0.0
    assert "boom" in result.scores["error"]
    assert result.model == "nudenet"
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


def test_detector_receives_image_path() -> None:
    detector = FakeDetector(by_path={"C:/tmp/dl/x.png": [{"class": "BELLY", "score": 0.9}]})
    clf = NudeNetClassifier(detector=detector)
    clf.classify(make_img(path="C:/tmp/dl/x.png"))
    assert detector.calls == ["C:/tmp/dl/x.png"]


# ---------------------------------------------------------------------------
# 惰性导入路径(不依赖真实 nudenet 库)
# ---------------------------------------------------------------------------

def test_missing_nudenet_raises_import_error_with_hint(monkeypatch: Any) -> None:
    """sys.modules 注入 None 强制 import 失败,验证中文安装提示。"""
    monkeypatch.setitem(sys.modules, "nudenet", None)
    with pytest.raises(ImportError, match="未安装 nudenet"):
        NudeNetClassifier()


def test_constructor_lazily_builds_real_detector(monkeypatch: Any) -> None:
    """注入假 nudenet 模块:无 detector 时走 from nudenet import NudeDetector。"""

    class FakeNudeDetector:
        def detect(self, path: str) -> list[dict[str, Any]]:
            return [{"class": "EXPOSED_ANUS", "score": 1.0}]

    fake_module = types.ModuleType("nudenet")
    fake_module.NudeDetector = FakeNudeDetector  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nudenet", fake_module)

    clf = NudeNetClassifier()
    assert isinstance(clf._detector, FakeNudeDetector)
    result = clf.classify(make_img())
    assert result.nsfw_prob == pytest.approx(0.98 * 1.0)


def test_module_import_triggers_no_model_download() -> None:
    """模块已成功 import(见文件顶部 importorskip)且未连带 import nudenet。"""
    import netsentinel.vision.nudenet_adapter as mod

    assert mod.NudeNetClassifier.name == "nudenet"
    assert "nudenet" not in sys.modules  # 惰性导入:import 模块本身不触碰三方库


# ---------------------------------------------------------------------------
# 注册表(依赖 A05 的 classifier_base;未就位则跳过)
# ---------------------------------------------------------------------------

def test_registered_in_classifier_registry(monkeypatch: Any) -> None:
    classifier_base = pytest.importorskip("netsentinel.vision.classifier_base")
    # 避免真实构造 NudeDetector 时下载模型:注入假 nudenet 模块
    fake_module = types.ModuleType("nudenet")
    fake_module.NudeDetector = FakeDetector  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nudenet", fake_module)

    clf = classifier_base.get_classifier("nudenet", Config())
    assert isinstance(clf, NudeNetClassifier)
    assert clf.name == "nudenet"


# ---------------------------------------------------------------------------
# V5 升级:线程安全(串行推理)/ 批量推理协议 / 遥测
# ---------------------------------------------------------------------------


class _ThreadProbeDetector:
    """V5 替身:用独立探针锁记录推理并发峰值,验证共享实例被串行调用。"""

    def __init__(self, by_path: dict[str, list[dict[str, Any]]]) -> None:
        self.by_path = dict(by_path)
        self.calls: list[str] = []
        self.active = 0
        self.peak = 0
        self._probe = threading.Lock()

    def detect(self, path: str) -> list[dict[str, Any]]:
        with self._probe:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append(path)
        time.sleep(0.02)  # 拉长推理窗口:无锁时多线程必然交叠
        with self._probe:
            self.active -= 1
        return self.by_path.get(path, [])


class _BatchDetector:
    """V5 替身:提供 detect_batch(批量协议),记录单图/批量调用序列。"""

    def __init__(
        self,
        by_path: dict[str, list[dict[str, Any]]],
        batch_error: Exception | None = None,
        batch_result_override: Any = None,
    ) -> None:
        self.by_path = dict(by_path)
        self.batch_error = batch_error
        self.batch_result_override = batch_result_override
        self.detect_calls: list[str] = []
        self.batch_calls: list[list[str]] = []

    def detect(self, path: str) -> list[dict[str, Any]]:
        self.detect_calls.append(path)
        return self.by_path.get(path, [])

    def detect_batch(self, paths: list[str]) -> list[list[dict[str, Any]]]:
        self.batch_calls.append(list(paths))
        if self.batch_error is not None:
            raise self.batch_error
        if self.batch_result_override is not None:
            return self.batch_result_override
        return [self.by_path.get(p, []) for p in paths]


def test_v5_concurrent_classify_serializes_detector_calls() -> None:
    """多线程 classify:模型侧要求串行推理 → 检测器并发峰值必须为 1。"""
    paths = [f"C:/tmp/dl/t{i}.png" for i in range(4)]
    probe = _ThreadProbeDetector(
        {p: [{"class": "EXPOSED_GENITALIA_F", "score": 0.9}] for p in paths}
    )
    clf = NudeNetClassifier(detector=probe)
    results: dict[str, ImageScore] = {}

    def worker(path: str) -> None:
        results[path] = clf.classify(make_img(path=path))

    threads = [threading.Thread(target=worker, args=(p,)) for p in paths]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert probe.peak == 1  # 推理全程串行,无并发交叠
    assert sorted(probe.calls) == sorted(paths)
    assert len(results) == 4
    for score in results.values():
        assert score.nsfw_prob == pytest.approx(0.97 * (0.5 + 0.5 * 0.9))


def test_v5_classify_batch_uses_detect_batch_once() -> None:
    """检测器带 detect_batch(底层库批量协议):一次传入全部路径,结果保序。"""
    paths = [f"C:/tmp/dl/b{i}.png" for i in range(3)]
    detector = _BatchDetector(
        {
            paths[0]: [{"class": "EXPOSED_BREAST_F", "score": 1.0}],
            paths[1]: [],  # 无检测框 → 兜底 0.01
            paths[2]: [{"label": "EXPOSED_ANUS", "score": 0.5}],  # 旧字段名
        }
    )
    clf = NudeNetClassifier(detector=detector)
    scores = clf.classify_batch([make_img(p) for p in paths])
    assert detector.batch_calls == [paths]  # 单次批量调用,全路径按输入顺序
    assert detector.detect_calls == []      # 未退化为逐张
    assert [s.nsfw_prob for s in scores] == pytest.approx(
        [0.93 * 1.0, 0.01, 0.98 * (0.5 + 0.5 * 0.5)]
    )
    assert scores[1].scores["detections"] == []
    assert scores[2].scores["detections"][0]["label"] == "EXPOSED_ANUS"


def test_v5_classify_batch_per_image_when_protocol_lacks_batch() -> None:
    """注入协议只有单图 detect(既有 FakeDetector):批量保持逐张、按序调用。"""
    paths = [f"C:/tmp/dl/f{i}.png" for i in range(3)]
    detector = FakeDetector(
        by_path={p: [{"class": "BELLY", "score": 0.9}] for p in paths}
    )
    clf = NudeNetClassifier(detector=detector)
    scores = clf.classify_batch([make_img(p) for p in paths])
    assert detector.calls == paths  # 调用序列:逐张、与输入同序
    assert all(s.nsfw_prob == pytest.approx(0.10 * (0.5 + 0.5 * 0.9)) for s in scores)


@pytest.mark.parametrize("scenario", ["raise", "short_result"])
def test_v5_classify_batch_falls_back_on_bad_batch_result(scenario: str) -> None:
    """detect_batch 抛错/返回形状不符:回退逐张,整批结果仍完整。"""
    paths = [f"C:/tmp/dl/x{i}.png" for i in range(3)]
    kwargs: dict[str, Any] = {}
    if scenario == "raise":
        kwargs["batch_error"] = RuntimeError("batch boom")
    else:
        kwargs["batch_result_override"] = []  # 长度与输入不符
    detector = _BatchDetector(
        {p: [{"class": "EXPOSED_ANUS", "score": 1.0}] for p in paths}, **kwargs
    )
    clf = NudeNetClassifier(detector=detector)
    scores = clf.classify_batch([make_img(p) for p in paths])
    assert len(detector.batch_calls) == 1  # 批量先试了一次
    assert detector.detect_calls == paths  # 再回退逐张
    assert [s.nsfw_prob for s in scores] == pytest.approx([0.98] * 3)


def test_v5_telemetry_records_infer_timer_and_error_counter() -> None:
    """推理计时 nudenet.infer、失败计数 vision.errors 均落入遥测。"""
    telemetry.reset()
    try:
        ok = make_classifier([{"class": "BELLY", "score": 0.5}])
        ok.classify(make_img())
        assert telemetry.snapshot()["timers"]["nudenet.infer"]["count"] >= 1

        bad = NudeNetClassifier(detector=FakeDetector(exc=RuntimeError("boom")))
        bad.classify(make_img())
        assert telemetry.snapshot()["counters"]["vision.errors"] >= 1
    finally:
        telemetry.reset()
