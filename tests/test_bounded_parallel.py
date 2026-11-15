"""P0-2 受界并发送审测试(全部离线、零外呼)。

锁定四组语义:

1. **workers 解析**:`vlm_parallel_workers` 0=自动档(≤6)、1=串行、
   2~16 显式、>16 钳制、垃圾值自动档兜底;
2. **资格检测**:覆写 ``classify_batch`` 的分类器(与一切注入替身)
   走原串行路径,零并行计数;仅"沿用基类默认批量"的分类器并行;
3. **并行正确性**:保序返回、结果与串行逐项相等、并发受界
   (实测峰值 ≤ cap 且 ≥ 2)、单图异常按 A166 容错为 error 分
   (整批不炸、其余条目不受影响);
4. **预算原子性(并发放行的前提)**:VlmCache.spend_one 在多线程竞争下
   恰好放行 daily_limit 次、其余全部 VlmBudgetExceeded——绝不超支。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, ImageScore
from netsentinel.vision.bounded_parallel import (
    ABS_MAX_WORKERS,
    DEFAULT_MAX_WORKERS,
    classify_bounded,
    resolve_workers,
)
from netsentinel.vision.classifier_base import NsfwClassifier

SITE = "http://localhost/a"


def _ev(path: str, *, width: int = 300, height: int = 300, sha: str = "") -> ImageEvidence:
    return ImageEvidence(path=path, url=f"http://img/{path}", source_page=SITE,
                         sha256=sha, width=width, height=height)


# ---------------------------------------------------------------------------
# 1. workers 解析
# ---------------------------------------------------------------------------
def test_resolve_workers_auto_default_bounded() -> None:
    """缺省 0 = 自动档:≥2 且 ≤ DEFAULT_MAX_WORKERS(20 核 mid 档 → 6)。"""
    w = resolve_workers(Config())
    assert 2 <= w <= DEFAULT_MAX_WORKERS


def test_resolve_workers_serial_and_explicit() -> None:
    cfg = Config()
    cfg.vlm_parallel_workers = 1
    assert resolve_workers(cfg) == 1  # 强制串行
    cfg.vlm_parallel_workers = 3
    assert resolve_workers(cfg) == 3
    cfg.vlm_parallel_workers = 99
    assert resolve_workers(cfg) == ABS_MAX_WORKERS  # 钳制
    cfg.vlm_parallel_workers = 0
    assert 2 <= resolve_workers(cfg) <= DEFAULT_MAX_WORKERS  # 0 = 自动档


def test_resolve_workers_garbage_falls_back_to_auto() -> None:
    cfg = Config()
    cfg.vlm_parallel_workers = "banana"  # type: ignore[assignment]
    assert 2 <= resolve_workers(cfg) <= DEFAULT_MAX_WORKERS


# ---------------------------------------------------------------------------
# 2. 资格检测:覆写 classify_batch → 串行;默认批量 → 并行
# ---------------------------------------------------------------------------
class SleepyDefault(NsfwClassifier):
    """沿用基类默认 classify_batch 的替身:记录并发峰值与调用顺序。"""

    name = "sleepy_default"

    def __init__(self, prob: float = 0.03, delay: float = 0.05) -> None:
        self.prob = prob
        self.delay = delay
        self.calls = 0
        self.max_concurrency = 0
        self._live = 0
        self._lock = threading.Lock()

    def classify(self, img: ImageEvidence) -> ImageScore:
        with self._lock:
            self.calls += 1
            self._live += 1
            self.max_concurrency = max(self.max_concurrency, self._live)
        time.sleep(self.delay)
        with self._lock:
            self._live -= 1
        return ImageScore(image=img, model=self.name, nsfw_prob=self.prob)


class CustomBatch(SleepyDefault):
    """覆写 classify_batch(原生批量语义,如 failover/nudenet)。"""

    def classify_batch(self, imgs: list[ImageEvidence]) -> list[ImageScore]:
        return [self.classify(i) for i in imgs]  # 自管批量:绝不替它并行


def _parallel_batches_delta() -> float:
    return telemetry.snapshot()["counters"].get("vlm.parallel.batches", 0.0)


def test_custom_batch_classifier_stays_serial() -> None:
    cfg = Config()
    imgs = [_ev(f"i{i}.jpg", sha=chr(ord("a") + i) * 64) for i in range(4)]
    clf = CustomBatch(delay=0.02)
    base = _parallel_batches_delta()
    out = classify_bounded(clf, imgs, cfg)
    assert [s.image.path for s in out] == [i.path for i in imgs]
    assert clf.max_concurrency == 1  # 串行:从未并发
    assert _parallel_batches_delta() == base  # 零并行计数(零扰动)


def test_injected_fake_protocol_stays_serial() -> None:
    """协议替身(自带 classify_batch,非 NsfwClassifier 子类)→ 串行。"""

    class FakeProtocol:
        name = "fake"

        def __init__(self) -> None:
            self.calls = 0

        def classify(self, img: ImageEvidence) -> ImageScore:
            self.calls += 1
            return ImageScore(image=img, model="fake", nsfw_prob=0.01)

        def classify_batch(self, imgs: list[ImageEvidence]) -> list[ImageScore]:
            return [self.classify(i) for i in imgs]

    clf = FakeProtocol()
    imgs = [_ev(f"p{i}.jpg", sha=chr(ord("a") + i) * 64) for i in range(3)]
    base = _parallel_batches_delta()
    out = classify_bounded(clf, imgs, Config())  # type: ignore[arg-type]
    assert clf.calls == 3
    assert _parallel_batches_delta() == base


def test_single_item_never_pools() -> None:
    clf = SleepyDefault()
    base = _parallel_batches_delta()
    out = classify_bounded(clf, [_ev("only.jpg", sha="a" * 64)], Config())
    assert len(out) == 1
    assert _parallel_batches_delta() == base  # 单张开池纯开销 → 串行


def test_serial_knob_disables_pooling() -> None:
    cfg = Config()
    cfg.vlm_parallel_workers = 1
    clf = SleepyDefault(delay=0.02)
    imgs = [_ev(f"s{i}.jpg", sha=chr(ord("a") + i) * 64) for i in range(4)]
    base = _parallel_batches_delta()
    classify_bounded(clf, imgs, cfg)
    assert clf.max_concurrency == 1  # 强制串行
    assert _parallel_batches_delta() == base


# ---------------------------------------------------------------------------
# 3. 并行正确性
# ---------------------------------------------------------------------------
def test_parallel_order_preserved_and_results_equal_serial() -> None:
    imgs = [_ev(f"e{i}.jpg", sha=chr(ord("a") + i) * 64) for i in range(7)]
    serial = SleepyDefault(prob=0.03, delay=0.0)
    expected = serial.classify_batch(imgs)

    cfg = Config()
    cfg.vlm_parallel_workers = 4
    par = SleepyDefault(prob=0.03, delay=0.05)
    base = _parallel_batches_delta()
    got = classify_bounded(par, imgs, cfg)

    assert [(s.image.path, s.nsfw_prob) for s in got] == \
           [(s.image.path, s.nsfw_prob) for s in expected]  # 保序且逐项相等
    assert par.max_concurrency >= 2  # 确实并发了
    assert par.max_concurrency <= 4  # 受界
    assert _parallel_batches_delta() == base + 1


def test_parallel_respects_cap() -> None:
    cfg = Config()
    cfg.vlm_parallel_workers = 2
    clf = SleepyDefault(delay=0.06)
    imgs = [_ev(f"c{i}.jpg", sha=chr(ord("a") + i) * 64) for i in range(6)]
    classify_bounded(clf, imgs, cfg)
    assert 2 <= clf.max_concurrency <= 2  # 峰值恰为 cap


def test_parallel_is_faster_than_serial_wall() -> None:
    """6 张 × 80ms 串行 ≥480ms;受界并发(自动档 ≥2)应显著快于串行。"""
    imgs = [_ev(f"w{i}.jpg", sha=chr(ord("a") + i) * 64) for i in range(6)]
    t0 = time.perf_counter()
    classify_bounded(SleepyDefault(delay=0.08), imgs, Config())
    parallel_wall = time.perf_counter() - t0
    assert parallel_wall < 0.08 * 6 * 0.75  # 至少省 25%(宽松阈值防抖动)


class FlakyOnce(NsfwClassifier):
    """沿用默认批量;第 fail_at 张抛异常(其余正常)。"""

    name = "flaky_once"

    def __init__(self, fail_at: int) -> None:
        self.fail_at = fail_at
        self.i = 0

    def classify(self, img: ImageEvidence) -> ImageScore:
        i = self.i
        self.i += 1
        if i == self.fail_at:
            raise RuntimeError("模拟单图识别异常(HTTP 429 余额耗尽)")
        return ImageScore(image=img, model=self.name, nsfw_prob=0.02)


def test_parallel_single_item_exception_degrades_in_band() -> None:
    """A166 语义:单图异常 → error 分(nsfw_prob=0 + 中文 error),整批不炸。"""
    imgs = [_ev(f"f{i}.jpg", sha=chr(ord("a") + i) * 64) for i in range(4)]
    cfg = Config()
    cfg.vlm_parallel_workers = 3
    out = classify_bounded(FlakyOnce(fail_at=1), imgs, cfg)
    assert len(out) == 4  # 整批完整
    assert out[1].nsfw_prob == 0.0 and "error" in out[1].scores
    assert out[0].nsfw_prob == 0.02 and out[2].nsfw_prob == 0.02
    # V10.24 收货口语义:errored 计数由编排层 _count_image_failures 承接


def test_parallel_empty_and_tiny_inputs() -> None:
    clf = SleepyDefault()
    assert classify_bounded(clf, [], Config()) == []
    out = classify_bounded(clf, [_ev("t.jpg", sha="a" * 64)], Config())
    assert len(out) == 1


# ---------------------------------------------------------------------------
# 4. 预算原子性(并发放行的前提):并发 spend_one 绝不超支
# ---------------------------------------------------------------------------
def test_vlm_cache_budget_atomic_under_threads(tmp_path) -> None:
    from netsentinel.vision.vlm_cache import VlmBudgetExceeded, VlmCache

    cache = VlmCache(str(tmp_path / "budget.db"), daily_limit=5)
    ok_count = []
    denied: list[Exception] = []
    lock = threading.Lock()

    def spend() -> None:
        try:
            cache.spend_one()
            with lock:
                ok_count.append(1)
        except VlmBudgetExceeded as exc:
            with lock:
                denied.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(spend) for _ in range(20)]
        for f in futures:
            f.result()

    assert len(ok_count) == 5  # 恰好放行 limit 次
    assert len(denied) == 15  # 其余全部拒绝
    assert cache.budget_state()["used"] == 5
    cache.close()


# ---------------------------------------------------------------------------
# 5. run_scan 集成:默认批量分类器在整扫里确实并行,判定与串行一致
# ---------------------------------------------------------------------------
def test_run_scan_parallel_and_serial_verdicts_identical(
    monkeypatch: pytest.MonkeyPatch, cfg_fixture
) -> None:
    import netsentinel.pipeline.orchestrator as orchestrator
    from netsentinel.contracts import PageSample

    monkeypatch.setattr(
        orchestrator, "_default_discover", lambda url, c, fetch_page=None: [url]
    )
    monkeypatch.setattr(
        orchestrator, "_default_build_bundle",
        lambda report, c: type("B", (), {"zip_path": "fake/bundle.zip"})(),
    )

    class _Q:
        calls: list[Any] = []

        def add(self, report, zip_path, **kw):
            _Q.calls.append(1)
            return 1

        def close(self):
            pass

    monkeypatch.setattr(orchestrator, "_default_queue", lambda db_path: _Q())

    class _A:
        def log_event(self, event, **fields):
            pass

    monkeypatch.setattr(orchestrator, "_default_audit_logger", lambda p: _A())

    evidences = [_ev(f"big{i}.jpg", width=300, height=300,
                     sha=chr(ord("a") + i) * 64) for i in range(5)]

    def capture(evidences):
        class Cap:
            def __call__(self, url, cfg, *, fetch_page=None):
                return PageSample(url=url, screenshot_path="",
                                  image_evidences=list(evidences))
        return Cap()

    base = _parallel_batches_delta()
    par_clf = SleepyDefault(prob=0.72, delay=0.05)
    report_par = orchestrator.run_scan(
        SITE, cfg_fixture, capture=capture(evidences), classifier=par_clf
    )
    assert _parallel_batches_delta() == base + 1  # 整扫内确实并行了
    assert par_clf.max_concurrency >= 2

    cfg_ser = Config(
        data_dir=cfg_fixture.data_dir, evidence_dir=cfg_fixture.evidence_dir,
        db_path=cfg_fixture.db_path, audit_path=cfg_fixture.audit_path,
        log_path=cfg_fixture.log_path,
    )
    cfg_ser.use_fusion = False
    cfg_ser.ensemble_members = []
    cfg_ser.vlm_parallel_workers = 1
    ser_clf = SleepyDefault(prob=0.72, delay=0.0)
    report_ser = orchestrator.run_scan(
        SITE, cfg_ser, capture=capture(evidences), classifier=ser_clf
    )
    assert report_ser.verdict is report_par.verdict  # 判定一致
    assert report_ser.agg_nsw_prob == report_par.agg_nsw_prob
    assert [s.nsfw_prob for s in report_ser.image_scores] == \
           [s.nsfw_prob for s in report_par.image_scores]


@pytest.fixture
def cfg_fixture(tmp_path) -> Config:
    cfg = Config(
        data_dir=str(tmp_path / "data"),
        evidence_dir=str(tmp_path / "evidence"),
        db_path=str(tmp_path / "data" / "review_queue.db"),
        audit_path=str(tmp_path / "data" / "audit.jsonl"),
        log_path=str(tmp_path / "data" / "logs" / "netsentinel.log"),
    )
    cfg.use_fusion = False
    cfg.ensemble_members = []
    return cfg
