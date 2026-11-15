"""G4 创世纪升级波测试(netsentinel.ops.pool 未完成计数 O(1) 化)。

覆盖两件事:

1. **计数一致性**:_PendingGauge 读数与 futures 实际完成数在并发提交/
   完成交错下逐检查点一致(fake 工作函数/scan 以事件控制完成时机);
2. **背压口径不变**:受控交错场景下,静态背压与 AIMD 两条路径的停顿
   触发时机与旧口径(逐个扫 ``futures`` 判 ``fut.done()`` 计数)逐次
   同判——停顿次数、时长、遥测计数全部对解析预期断言。

全部离线:run_scan / memory / sleep / jitter / aimd 时钟一律注入;
真实线程池仅承载受控 fake scan(事件挂起/放行 + 看门狗兜底放行),
零网络、零真实礼貌 sleep(内部以微秒级事件同步,与既有测试同款)、
零真实记忆库(不触发缺省 SiteMemory 落盘)。
"""
from __future__ import annotations

import pathlib
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, Callable

import pytest

import netsentinel.ops.pool as pool
from netsentinel import telemetry
from netsentinel.contracts import Config, Verdict
from netsentinel.ops.aimd import AIMDController
from netsentinel.ops.pool import run_pool


# ---------------------------------------------------------------------------
# fakes(与 test_pool.py 同款口径,本文件独立持有)
# ---------------------------------------------------------------------------
class FakeMemory:
    """A33 SiteMemory 契约 fake:记住每站点指纹(纯内存,零落盘)。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def fingerprint(self, report: Any) -> str:
        return "fp:" + str(getattr(report, "site_url", ""))

    def remember(self, site_url: str, fp: str) -> None:
        self.store[site_url] = fp

    def last_fingerprint(self, site_url: str) -> str:
        return self.store.get(site_url, "")


def make_report(url: str, *, needs_review: bool = False) -> SimpleNamespace:
    """鸭子类型的 SiteReport fake(池只读 site_url/verdict/agg/needs_review)。"""
    return SimpleNamespace(
        site_url=url, verdict=Verdict.CLEAN, agg_nsw_prob=0.05,
        needs_review=needs_review,
    )


def make_cfg(tmp_path: pathlib.Path) -> Config:
    """把所有落盘路径收进 tmp_path 的 Config(本测试不应触发任何落盘)。"""
    data = tmp_path / "data"
    return Config(
        watchlist_path=str(tmp_path / "watchlist.yaml"),
        data_dir=str(data),
        db_path=str(data / "review_queue.db"),
        audit_path=str(data / "audit.jsonl"),
        log_path=str(data / "logs" / "netsentinel.log"),
    )


def fixed_jitter() -> float:
    """固定抖动源:停顿时长恒为 1.0 + 0.5*0.4 = 1.2s(仅记录,不真睡)。"""
    return 0.4


class SteppingClock:
    """每次被调用前进 step 秒的脚本钟:AIMD 时长(两次 now 差)恒定。"""

    def __init__(self, step: float = 1.0) -> None:
        self._value = 0.0
        self._step = float(step)

    def __call__(self) -> float:
        self._value += self._step
        return self._value


class _BlockingFn:
    """挂在事件上的 fake 工作函数:set(started) → wait(release) → 返回。"""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self) -> int:
        self.started.set()
        self.release.wait(timeout=15)
        return 42


def _track(
    gauge: pool._PendingGauge,
    ex: ThreadPoolExecutor,
    fn: Callable[[], Any],
) -> tuple[Future, threading.Event]:
    """提交 fn 并登记进 gauge;再挂一个**晚于 gauge 回调**的标记回调。

    done 回调按挂接顺序在同一完成线程内串行执行——标记置位时该笔的
    −1 必已执行,检查点读数无竞态(可做精确断言而非轮询)。
    """
    fut = ex.submit(fn)
    gauge.track(fut)
    marker = threading.Event()
    fut.add_done_callback(lambda _f, m=marker: m.set())
    return fut, marker


# ---------------------------------------------------------------------------
# G4-1:计数器与 futures 实际完成数在交错下逐检查点一致
# ---------------------------------------------------------------------------
def test_g4_gauge_tracks_completions_exact_under_interleaving():
    """交错提交/完成:读数与"已提交 − 已完成"逐检查点精确一致。"""
    ex = ThreadPoolExecutor(max_workers=2)
    gauge = pool._PendingGauge()
    try:
        fns = [_BlockingFn() for _ in range(8)]
        markers: list[threading.Event] = []
        for fn in fns:
            _, marker = _track(gauge, ex, fn)
            markers.append(marker)
        assert gauge.value == 8  # 全部挂起:零完成

        for i in (0, 1):  # 放行前两个(恰占满 2 个 worker,其余排队)
            fns[i].release.set()
            assert markers[i].wait(timeout=15)
        assert gauge.value == 8 - 2  # 检查点:已完成恰 2(标记晚于 −1)

        late = [_BlockingFn() for _ in range(4)]  # 交错:边完成边再提交
        for fn in late:
            _, marker = _track(gauge, ex, fn)
            markers.append(marker)
        assert gauge.value == 12 - 2  # 新提交未被放行:读数恰为差值

        for fn in fns[2:] + late:
            fn.release.set()
        for m in markers:
            assert m.wait(timeout=15)
        assert gauge.value == 0  # 全部完成:计数精确归零(零丢失)
    finally:
        ex.shutdown(wait=True)
    assert gauge.value == 0  # 排空后仍是 0(shutdown 语义下回调全跑完)


def test_g4_gauge_no_lost_updates_under_thread_contention():
    """高并发完成/提交交错:计数零丢失(锁保护正确性的压力验证)。

    4 个 worker × 240 个微任务真实并发完成;任何一次 +1/−1 丢失更新
    都会让终局读数偏离 0(裸 int 读-改-写在 GIL 下并非原子)。
    """
    ex = ThreadPoolExecutor(max_workers=4)
    gauge = pool._PendingGauge()
    done = threading.Semaphore(0)
    total = 240
    try:
        for i in range(total):
            fut = ex.submit(time.sleep, 0.0003 if i % 4 == 0 else 0.0)
            gauge.track(fut)
            fut.add_done_callback(lambda _f: done.release())
        for _ in range(total):
            assert done.acquire(timeout=15)
        assert gauge.value == 0
    finally:
        ex.shutdown(wait=True)
    assert gauge.value == 0


# ---------------------------------------------------------------------------
# G4-2:背压触发时机与旧口径一致(run_pool 级受控交错场景)
# ---------------------------------------------------------------------------
class ScriptedPause:
    """记录每次停顿时长;第 N 次停顿时执行预设动作(放行/等待)。"""

    def __init__(self, actions: dict[int, Callable[[], None]] | None = None) -> None:
        self.calls: list[float] = []
        self._actions = dict(actions or {})

    def __call__(self, seconds: float) -> None:
        self.calls.append(float(seconds))
        act = self._actions.get(len(self.calls))
        if act is not None:
            act()


def _interleave_world(
    n_items: int, prefix: str
) -> tuple[list[str], Callable[[str, Config], Any],
           dict[str, threading.Event], dict[str, threading.Event]]:
    """受控交错世界:fake scan 按 URL 挂事件(started/release),绝不真睡。"""
    items = [f"https://{prefix}{i}.example.com/" for i in range(n_items)]
    reports = {u: make_report(u) for u in items}
    started = {u: threading.Event() for u in items}
    release = {u: threading.Event() for u in items}

    def scan(url: str, cfg: Config) -> Any:
        started[url].set()
        release[url].wait(timeout=15)
        return reports[url]

    return items, scan, started, release


def _release_all(release: dict[str, threading.Event]) -> None:
    for ev in release.values():
        ev.set()


def test_g4_static_backpressure_timing_matches_legacy_reads(tmp_path):
    """静态背压触发时机与旧口径一致(受控交错 + 解析停顿序列)。

    workers=1(阈值 2×1=2)、6 目标、单线程真实执行器;第②次停顿时
    放行 u0 并等到 u1 开扫——worker 串行 ⇒ u0 已 set_result 且 done
    回调全部执行完,新旧口径此刻同读"未完成 1";其余目标一律挂起,
    此后提交期间再无完成。逐次读数(= 旧口径逐个 ``fut.done()`` 扫描
    在同一受控世界下的确定值):

    - 提交 #1..#4 前读数 0/1/1/2(≤ 2,零背压);
    - 提交 #5/#6 前读数 3/4(> 2,各多停一轮);
    - 末次停顿放行全部 → 聚合排空:共 8 次停顿(6 常规 + 2 背压),
      全部 1.2s——与旧实现的停顿序列逐字节一致。
    """
    cfg = make_cfg(tmp_path)
    items, scan, started, release = _interleave_world(6, "g4s-")
    u0, u1 = items[0], items[1]

    def act_release_u0_wait_u1() -> None:
        release[u0].set()
        assert started[u1].wait(timeout=15)

    def act_release_rest() -> None:
        for u in items[1:]:
            release[u].set()

    pause = ScriptedPause({2: act_release_u0_wait_u1, 8: act_release_rest})
    watchdog = threading.Timer(5.0, lambda: _release_all(release))
    watchdog.start()
    telemetry.reset()
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        summary = run_pool(
            cfg, items, run_scan=scan, memory=FakeMemory(),
            executor=ex, workers=1, sleep=pause, jitter=fixed_jitter,
        )
    finally:
        _release_all(release)
        watchdog.cancel()
        ex.shutdown(wait=True)
    assert summary["done"] == 6 and summary["failed"] == 0
    assert len(pause.calls) == 8  # 6 常规 + 2 背压(与旧口径逐字节一致)
    assert all(s == pytest.approx(1.2) for s in pause.calls)
    assert telemetry.snapshot()["counters"]["pool.backpressure"] == 2


def test_g4_aimd_pending_reads_match_legacy_full_scan(tmp_path):
    """AIMD 路径读数与旧口径(全量扫 futures)逐次同判:停顿序列一致。

    cap=2 恒窗(注入步进钟 → 每任务时长恰 1.0 = 基线,无时延减窗;
    calm_round=100 → 5 个完成不触发加性增),即
    ``should_pause(pending) = pending ≥ 2``。5 目标、单线程真实执行器;
    第②次停顿放行 u0 并等 u1 开扫(此后提交期间再无完成),逐次读数
    (= 旧口径全量扫描在同一受控世界下的确定值):

    - 提交 #1..#3 前读数 0/1/1(< 2,零背压);
    - 提交 #4/#5 前读数 2/3(≥ 2,各多停一轮);
    - 末次停顿放行全部 → 共 7 次停顿(5 常规 + 2 背压)、终局 permits==2、
      时长样本恰 5(每任务一次;O(1) 读数路径不双计不漏计)。
    """
    cfg = make_cfg(tmp_path)
    items, scan, started, release = _interleave_world(5, "g4a-")
    u0, u1 = items[0], items[1]
    ctl = AIMDController(2, calm_round=100, clock_fn=SteppingClock(1.0))

    def act_release_u0_wait_u1() -> None:
        release[u0].set()
        assert started[u1].wait(timeout=15)

    def act_release_rest() -> None:
        for u in items[1:]:
            release[u].set()

    pause = ScriptedPause({2: act_release_u0_wait_u1, 7: act_release_rest})
    watchdog = threading.Timer(5.0, lambda: _release_all(release))
    watchdog.start()
    telemetry.reset()
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        summary = run_pool(
            cfg, items, run_scan=scan, memory=FakeMemory(),
            executor=ex, workers=1, sleep=pause, jitter=fixed_jitter,
            aimd=ctl,
        )
    finally:
        _release_all(release)
        watchdog.cancel()
        ex.shutdown(wait=True)
    assert summary["done"] == 5 and summary["failed"] == 0
    assert len(pause.calls) == 7  # 5 常规 + 2 背压(与旧口径逐字节一致)
    assert all(s == pytest.approx(1.2) for s in pause.calls)
    assert telemetry.snapshot()["counters"]["pool.backpressure"] == 2
    assert ctl.permits == 2  # 恒窗:读数路径不影响窗口演化
    assert ctl.state()["samples"] == 5  # 每任务恰一次时长回喂


def test_g4_politeness_constants_and_pause_sites_untouched():
    """红线 35 源级断言(与 A236 同款):礼貌常量零触碰;停顿表达式恰两处,
    本波未新增任何停顿调用;O(1) 计数不引入真实睡眠。"""
    import inspect

    assert pool.PAUSE_BASE_S == 1.0
    assert pool.PAUSE_JITTER_S == 0.5
    src = inspect.getsource(pool)
    assert src.count("pause(PAUSE_BASE_S + PAUSE_JITTER_S * jitter_fn())") == 2
    assert src.count("time.sleep(") == 1  # 仅 _default_sleep(可注入替身)
