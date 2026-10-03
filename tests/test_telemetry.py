"""telemetry 轻量遥测核心测试(V5 · 项目负责人)。"""
from __future__ import annotations

import json
import threading

from netsentinel import telemetry


def setup_function(_fn):
    telemetry.reset()


def test_inc_accumulates():
    telemetry.inc("fetch.page")
    telemetry.inc("fetch.page")
    telemetry.inc("fetch.page", 3)
    assert telemetry.snapshot()["counters"]["fetch.page"] == 5.0


def test_gauge_overwrites():
    telemetry.gauge("queue.pending", 3)
    telemetry.gauge("queue.pending", 7)
    assert telemetry.snapshot()["gauges"]["queue.pending"] == 7.0


def test_timer_context_records_seconds():
    with telemetry.timer("op.x"):
        pass
    stats = telemetry.snapshot()["timers"]["op.x"]
    assert stats["count"] == 1 and stats["avg_ms"] >= 0.0


def test_timer_records_even_on_exception():
    try:
        with telemetry.timer("op.fail"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert telemetry.snapshot()["timers"]["op.fail"]["count"] == 1


def test_timed_decorator(monkeypatch):
    calls = []
    monkeypatch.setattr(telemetry, "observe", lambda n, s: calls.append((n, s)))

    @telemetry.timed("dec.fn")
    def fn():
        return 42

    assert fn() == 42 and calls and calls[0][0] == "dec.fn"


def test_observe_trims_to_max_samples():
    for _ in range(telemetry.MAX_SAMPLES + 50):
        telemetry.observe("hot.loop", 0.001)
    assert telemetry.snapshot()["timers"]["hot.loop"]["count"] == telemetry.MAX_SAMPLES


def test_snapshot_stats_known_samples():
    for s in (0.010, 0.020, 0.030, 0.040, 0.100):
        telemetry.observe("known", s)
    st = telemetry.snapshot()["timers"]["known"]
    assert st["count"] == 5
    assert st["avg_ms"] == 40.0          # (10+20+30+40+100)/5 ms
    assert st["max_ms"] == 100.0
    assert 90.0 <= st["p95_ms"] <= 100.0


def test_snapshot_empty_and_ts():
    snap = telemetry.snapshot()
    assert snap["counters"] == {} and snap["gauges"] == {} and snap["timers"] == {}
    assert "T" in snap["ts"]


def test_export_jsonl_roundtrip(tmp_path):
    telemetry.inc("e", 1)
    path = tmp_path / "nested" / "m.jsonl"
    snap = telemetry.export_jsonl(str(path))
    assert path.is_file()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[-1])["counters"]["e"] == 1.0 == snap["counters"]["e"]


def test_thread_safety():
    def worker():
        for _ in range(200):
            telemetry.inc("race")
            telemetry.observe("race.t", 0.0001)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert telemetry.snapshot()["counters"]["race"] == 1600.0


def test_reset_clears_all():
    telemetry.inc("a")
    telemetry.gauge("b", 1)
    telemetry.observe("c", 1.0)
    telemetry.reset()
    snap = telemetry.snapshot()
    assert not (snap["counters"] or snap["gauges"] or snap["timers"])
