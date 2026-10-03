"""A167 负载护栏(netsentinel.ops.load_guard)单元测试。

全部离线:``os.times()`` / 墙钟 / 睡眠一律注入 fake(脚本序列 + 手算
利用率),零真实 sleep、零网络。覆盖契约 V9 §3 A167 与红线 37:

- 三档手算利用率:0.5(不让)/ 0.95(让)/ 1.0(让);
- 首采样本不足 → False;阈值严格大于(恰好等于不让);
- ``maybe_yield`` 过载调 ``sleep(0.05)``(计数+时长),不过载不调;
- interval 内复用缓存样本(不消耗新 times 样本);
- 墙钟注入参与分母;时钟回退不崩;
- 并发调用安全(锁外睡眠,无死锁,不留学生线程)。
"""
from __future__ import annotations

import math
import os
import threading
import time

import pytest

from netsentinel.ops.load_guard import (
    DEFAULT_INTERVAL_S,
    DEFAULT_THRESHOLD,
    YIELD_SLEEP_S,
    LoadGuard,
)

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class ScriptedTimes:
    """按脚本顺序吐出 times 元组 (user, system, children_user, children_system, elapsed)。

    脚本耗尽后重复最后一个样本;线程安全地记录调用次数。
    """

    def __init__(self, samples: list[tuple[float, float, float, float, float]]) -> None:
        self.samples = [tuple(s) for s in samples]
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self) -> tuple[float, ...]:
        with self._lock:
            self.calls += 1
            return self.samples[min(self.calls, len(self.samples)) - 1]


class SteppingTimes:
    """每次调用 user 前进 ``step_user`` 秒(并发压测:利用率恒为 step/step_wall)。"""

    def __init__(self, start_user: float = 0.0, step_user: float = 2.0) -> None:
        self._user = float(start_user)
        self._step = float(step_user)
        self._lock = threading.Lock()

    def __call__(self) -> tuple[float, float, float, float, float]:
        with self._lock:
            self._user += self._step
            return (self._user, 0.0, 0.0, 0.0, 0.0)


class FakeClock:
    """手动推进的假墙钟(线程安全)。"""

    def __init__(self, start: float = 1000.0) -> None:
        self._value = float(start)
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._value

    def advance(self, dt: float) -> None:
        with self._lock:
            self._value += float(dt)


class SteppingClock:
    """每次被调用自动前进 ``step`` 秒(并发压测:每次采样都已过 interval)。"""

    def __init__(self, start: float = 0.0, step: float = 1.0) -> None:
        self._value = float(start)
        self._step = float(step)
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            self._value += self._step
            return self._value


class SleepRecorder:
    """记录每次睡眠的时长(可携带非阻塞探针验证锁外执行)。"""

    def __init__(self) -> None:
        self.durations: list[float] = []
        self._lock = threading.Lock()

    def __call__(self, seconds: float) -> None:
        with self._lock:
            self.durations.append(float(seconds))

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.durations)


def pump(guard: LoadGuard, clock: FakeClock, dt: float = 1.0) -> bool:
    """推进一个采样窗口(dt > interval_s)并返回该窗口的节流判定。"""
    clock.advance(dt)
    return guard.should_throttle()


# ---------------------------------------------------------------------------
# 缺省装配与惰性初始化
# ---------------------------------------------------------------------------
def test_defaults_and_lazy_init() -> None:
    guard = LoadGuard()
    assert guard.interval_s == DEFAULT_INTERVAL_S == 0.5
    assert guard.threshold == DEFAULT_THRESHOLD == 0.92
    assert YIELD_SLEEP_S == 0.05
    # 缺省装配:os.times / time.sleep / time.monotonic;构造时不采样
    assert guard._times_fn is os.times
    assert guard._sleep_fn is time.sleep
    assert guard._clock_fn is time.monotonic
    assert guard._last_wall is None  # 惰性:首次调用才建基线


def test_default_guard_first_call_offline_false() -> None:
    guard = LoadGuard()
    assert guard.should_throttle() is False  # 真实 os.times 首采:样本不足
    assert guard.last_utilization() is None


# ---------------------------------------------------------------------------
# 首采:样本不足 → False
# ---------------------------------------------------------------------------
def test_first_sample_insufficient_false() -> None:
    times = ScriptedTimes([(3.0, 1.0, 0.0, 0.0, 0.0)])
    clock = FakeClock(100.0)
    guard = LoadGuard(times_fn=times, clock_fn=clock)
    assert times.calls == 0  # 构造不采样
    assert guard.should_throttle() is False  # 首采只建基线
    assert times.calls == 1


def test_first_sample_last_utilization_none() -> None:
    guard = LoadGuard(
        times_fn=ScriptedTimes([(9.9, 9.9, 0.0, 0.0, 0.0)]),
        clock_fn=FakeClock(),
    )
    guard.should_throttle()
    assert guard.last_utilization() is None  # 没有差分就没有利用率


# ---------------------------------------------------------------------------
# 三档手算利用率:0.5 / 0.95 / 1.0(墙钟差分均为 1.0s)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("cpu_second", "expected_util", "expected_throttle"),
    [
        pytest.param(0.5, 0.5, False, id="low-0.50"),
        pytest.param(0.95, 0.95, True, id="high-0.95"),
        pytest.param(1.0, 1.0, True, id="full-1.00"),
    ],
)
def test_tier_utilization_hand_computed(
    cpu_second: float, expected_util: float, expected_throttle: bool
) -> None:
    times = ScriptedTimes(
        [(0.0, 0.0, 0.0, 0.0, 0.0), (cpu_second, 0.0, 0.0, 0.0, 0.0)]
    )
    clock = FakeClock(100.0)
    guard = LoadGuard(times_fn=times, clock_fn=clock)
    assert guard.should_throttle() is False  # 首采
    assert pump(guard, clock, dt=1.0) is expected_throttle
    assert guard.last_utilization() == pytest.approx(expected_util)
    assert times.calls == 2


# ---------------------------------------------------------------------------
# 阈值边界:严格大于才让位
# ---------------------------------------------------------------------------
def test_threshold_exact_boundary_no_throttle() -> None:
    # Δcpu=0.92、Δwall=1.0 → 利用率恰等于缺省阈值 0.92 → 不让位
    times = ScriptedTimes([(0.0, 0.0, 0.0, 0.0, 0.0), (0.92, 0.0, 0.0, 0.0, 0.0)])
    clock = FakeClock(100.0)
    guard = LoadGuard(times_fn=times, clock_fn=clock)
    guard.should_throttle()
    assert pump(guard, clock) is False
    assert guard.last_utilization() == pytest.approx(0.92)


def test_custom_threshold_boundaries() -> None:
    # threshold=0.5:利用率恰 0.5 不让(严格大于)
    clock = FakeClock(100.0)
    guard = LoadGuard(
        threshold=0.5,
        times_fn=ScriptedTimes([(0.0, 0.0, 0.0, 0.0, 0.0), (0.5, 0.0, 0.0, 0.0, 0.0)]),
        clock_fn=clock,
    )
    guard.should_throttle()
    assert pump(guard, clock) is False

    # threshold=0.5:利用率 0.6 > 0.5 → 让
    clock2 = FakeClock(100.0)
    guard2 = LoadGuard(
        threshold=0.5,
        times_fn=ScriptedTimes([(0.0, 0.0, 0.0, 0.0, 0.0), (0.6, 0.0, 0.0, 0.0, 0.0)]),
        clock_fn=clock2,
    )
    guard2.should_throttle()
    assert pump(guard2, clock2) is True

    # threshold=1.0:利用率恰 1.0(单核打满)仍不让——严格大于
    clock3 = FakeClock(100.0)
    guard3 = LoadGuard(
        threshold=1.0,
        times_fn=ScriptedTimes([(0.0, 0.0, 0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0, 0.0)]),
        clock_fn=clock3,
    )
    guard3.should_throttle()
    assert pump(guard3, clock3) is False


# ---------------------------------------------------------------------------
# maybe_yield:过载调 sleep(0.05),不过载/样本不足不调
# ---------------------------------------------------------------------------
def overloaded_guard(sleep_fn) -> tuple[LoadGuard, FakeClock]:
    """持续过载的护栏:两个窗口利用率均为 2.0(CPU 差分 2.0 / 墙钟 1.0)。"""
    times = ScriptedTimes(
        [
            (0.0, 0.0, 0.0, 0.0, 0.0),
            (2.0, 0.0, 0.0, 0.0, 0.0),
            (4.0, 0.0, 0.0, 0.0, 0.0),
        ]
    )
    clock = FakeClock(100.0)
    guard = LoadGuard(times_fn=times, clock_fn=clock, sleep_fn=sleep_fn)
    guard.should_throttle()  # 建基线
    return guard, clock


def test_maybe_yield_over_calls_sleep_005() -> None:
    rec = SleepRecorder()
    guard, clock = overloaded_guard(rec)
    clock.advance(1.0)  # 利用率 2.0 > 0.92
    assert guard.maybe_yield() is True
    assert rec.durations == [0.05]
    assert rec.durations[0] == YIELD_SLEEP_S


def test_maybe_yield_under_no_sleep() -> None:
    rec = SleepRecorder()
    times = ScriptedTimes([(0.0, 0.0, 0.0, 0.0, 0.0), (0.2, 0.0, 0.0, 0.0, 0.0)])
    clock = FakeClock(100.0)
    guard = LoadGuard(times_fn=times, clock_fn=clock, sleep_fn=rec)
    guard.should_throttle()
    clock.advance(1.0)  # 利用率 0.2 < 0.92
    assert guard.maybe_yield() is False
    assert rec.durations == []


def test_maybe_yield_insufficient_first_sample_no_sleep() -> None:
    rec = SleepRecorder()
    guard = LoadGuard(
        times_fn=ScriptedTimes([(5.0, 5.0, 0.0, 0.0, 0.0)]),
        clock_fn=FakeClock(),
        sleep_fn=rec,
    )
    assert guard.maybe_yield() is False  # 首采样本不足
    assert rec.durations == []


def test_should_throttle_never_sleeps() -> None:
    rec = SleepRecorder()
    guard, clock = overloaded_guard(rec)
    clock.advance(1.0)
    assert guard.should_throttle() is True  # 持续过载
    clock.advance(1.0)
    assert guard.should_throttle() is True
    assert rec.durations == []  # 判定本身绝不睡眠


def test_maybe_yield_sleeps_outside_lock() -> None:
    """红线 37:睡眠必须发生在内部锁之外(单线程非阻塞探针,确定性)。"""
    probed: list[tuple[bool, float]] = []

    def probe_sleep(seconds: float) -> None:
        acquired = guard._lock.acquire(blocking=False)
        if acquired:
            guard._lock.release()
        probed.append((acquired, seconds))

    guard, clock = overloaded_guard(probe_sleep)
    clock.advance(1.0)
    assert guard.maybe_yield() is True
    assert probed == [(True, 0.05)]  # 锁可被非阻塞获取 → 睡眠在锁外


# ---------------------------------------------------------------------------
# interval 缓存与到期重采样
# ---------------------------------------------------------------------------
def test_interval_reuses_cached_sample() -> None:
    times = ScriptedTimes(
        [(0.0, 0.0, 0.0, 0.0, 0.0), (0.5, 0.0, 0.0, 0.0, 0.0), (9.0, 0.0, 0.0, 0.0, 0.0)]
    )
    clock = FakeClock(100.0)
    guard = LoadGuard(times_fn=times, clock_fn=clock)
    guard.should_throttle()  # 基线
    assert pump(guard, clock, dt=1.0) is False  # 窗口 1:利用率 0.5
    assert times.calls == 2
    for _ in range(2):  # interval(0.5s)内(累计 0.2/0.4)反复调用:复用缓存样本
        clock.advance(0.2)
        assert guard.should_throttle() is False
        assert guard.last_utilization() == pytest.approx(0.5)
    assert guard.should_throttle() is False  # 墙钟停滞:同样复用
    assert times.calls == 2  # 未消耗新样本(第 3 个样本未被动用)


def test_interval_exact_boundary_resamples() -> None:
    times = ScriptedTimes(
        [(0.0, 0.0, 0.0, 0.0, 0.0), (0.5, 0.0, 0.0, 0.0, 0.0), (2.5, 0.0, 0.0, 0.0, 0.0)]
    )
    clock = FakeClock(100.0)
    guard = LoadGuard(times_fn=times, clock_fn=clock)
    guard.should_throttle()
    pump(guard, clock, dt=1.0)  # 窗口 1:0.5
    clock.advance(0.5)  # 恰好到达 interval → 视为到期,立即重采样
    assert guard.should_throttle() is True  # Δcpu=2.0 / Δwall=0.5 = 4.0
    assert guard.last_utilization() == pytest.approx(4.0)
    assert times.calls == 3


def test_utilization_updates_across_windows() -> None:
    times = ScriptedTimes(
        [(0.0, 0.0, 0.0, 0.0, 0.0), (0.5, 0.0, 0.0, 0.0, 0.0), (1.5, 0.0, 0.0, 0.0, 0.0)]
    )
    clock = FakeClock(100.0)
    guard = LoadGuard(times_fn=times, clock_fn=clock)
    guard.should_throttle()
    assert pump(guard, clock) is False  # 窗口 1:0.5 → 不让
    assert guard.last_utilization() == pytest.approx(0.5)
    assert pump(guard, clock) is True  # 窗口 2:Δcpu=1.0 / Δwall=1.0 = 1.0 → 让
    assert guard.last_utilization() == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 墙钟注入:分母参与手算;时钟回退不崩
# ---------------------------------------------------------------------------
def test_wall_clock_injection_drives_denominator() -> None:
    # Δcpu 恒为 1.0:墙钟差分 2.0s → 0.5(不让);1.0s → 1.0(让)
    times = ScriptedTimes(
        [(0.0, 0.0, 0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0, 0.0), (2.0, 0.0, 0.0, 0.0, 0.0)]
    )
    clock = FakeClock(200.0)
    guard = LoadGuard(times_fn=times, clock_fn=clock)
    guard.should_throttle()
    assert pump(guard, clock, dt=2.0) is False
    assert guard.last_utilization() == pytest.approx(0.5)
    assert pump(guard, clock, dt=1.0) is True
    assert guard.last_utilization() == pytest.approx(1.0)


def test_backward_or_frozen_clock_no_crash() -> None:
    """墙钟回退/停滞:距上次采样 < interval → 复用缓存,不消耗新样本、不崩。"""
    times = ScriptedTimes(
        [(0.0, 0.0, 0.0, 0.0, 0.0), (0.5, 0.0, 0.0, 0.0, 0.0), (9.0, 0.0, 0.0, 0.0, 0.0)]
    )
    clock = FakeClock(100.0)
    guard = LoadGuard(times_fn=times, clock_fn=clock)
    guard.should_throttle()  # 基线 @100.0
    clock.advance(1.0)
    assert guard.should_throttle() is False  # 窗口 1 @101.0:利用率 0.5
    clock.advance(-2.0)  # 回退到 99.0:距上次采样为负 < interval → 复用缓存
    assert guard.should_throttle() is False
    assert guard.last_utilization() == pytest.approx(0.5)
    assert guard.should_throttle() is False  # 停滞:仍在 interval 内
    assert times.calls == 2  # 未消耗新样本(9.0 未被动用)


def test_backward_clock_then_recovery_recalc() -> None:
    """墙钟恢复推进并跨过 interval 后,基于新样本重算利用率。"""
    times = ScriptedTimes(
        [(0.0, 0.0, 0.0, 0.0, 0.0), (0.5, 0.0, 0.0, 0.0, 0.0), (1.1, 0.0, 0.0, 0.0, 0.0)]
    )
    clock = FakeClock(100.0)
    guard = LoadGuard(times_fn=times, clock_fn=clock)
    guard.should_throttle()  # 基线 @100.0(user=0.0)
    clock.advance(1.0)
    assert guard.should_throttle() is False  # 窗口 1 @101.0:user=0.5 → 0.5
    clock.advance(-5.0)  # 回退到 96.0:复用,不崩
    assert guard.should_throttle() is False
    clock.advance(5.6)  # 101.6:距上次采样 0.6 ≥ 0.5 → 重采样
    assert guard.should_throttle() is True  # Δcpu=0.6 / Δwall=0.6 = 1.0
    assert guard.last_utilization() == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 并发安全(红线 37:不死者线程、锁外睡眠)
# ---------------------------------------------------------------------------
def run_threads(target, n: int = 8) -> None:
    threads = [threading.Thread(target=target) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
        assert not t.is_alive(), "并发调用卡死(疑似锁内睡眠/死锁)"


def test_concurrent_should_throttle_threadsafe() -> None:
    # 每次采样墙钟 +1.0s、CPU +2.0s → 除基线调用外利用率恒为 2.0(过载)
    guard = LoadGuard(times_fn=SteppingTimes(step_user=2.0), clock_fn=SteppingClock(step=1.0))
    results: list[bool] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            for _ in range(25):
                value = guard.should_throttle()
                assert isinstance(value, bool)
                with lock:
                    results.append(value)
        except BaseException as exc:  # pragma: no cover - 记录后统一断言
            errors.append(exc)

    run_threads(worker)
    assert errors == []
    assert len(results) == 8 * 25
    # 基线调用(全局第一次)返回 False,其余全部过载 True
    assert results.count(False) == 1
    assert guard.last_utilization() == pytest.approx(2.0)


def test_concurrent_maybe_yield_no_deadlock_and_counts() -> None:
    rec = SleepRecorder()
    guard = LoadGuard(
        times_fn=SteppingTimes(step_user=2.0),
        clock_fn=SteppingClock(step=1.0),
        sleep_fn=rec,
    )
    true_returns = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(20):
            if guard.maybe_yield():
                with lock:
                    true_returns.append(True)

    run_threads(worker)
    # 每个返回 True 的让位恰好对应一次 sleep(0.05);无死锁、无遗漏
    assert rec.count == len(true_returns) == 20 * 8 - 1  # 基线那次为 False
    assert set(rec.durations) == {YIELD_SLEEP_S}


# ---------------------------------------------------------------------------
# 参数校验(中文 ValueError)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad_interval", [0.0, -0.5, math.inf, math.nan])
def test_invalid_interval_rejected(bad_interval: float) -> None:
    with pytest.raises(ValueError, match="interval_s"):
        LoadGuard(interval_s=bad_interval)


@pytest.mark.parametrize("bad_threshold", [0.0, -1.0, math.inf, math.nan])
def test_invalid_threshold_rejected(bad_threshold: float) -> None:
    with pytest.raises(ValueError, match="threshold"):
        LoadGuard(threshold=bad_threshold)


# ---------------------------------------------------------------------------
# A212:AIMD 消费口(last_utilization 读数直喂 ops.aimd,护栏语义零改动)
# ---------------------------------------------------------------------------
def test_last_utilization_feeds_aimd_controller() -> None:
    """读数口接线:last_utilization() 的三种读数(None/过载/低载)经
    AIMDController.on_signal 消费——None 与低载平静,超阈减半窗口;
    消费读数不触发新采样、不影响 should_throttle 判定口径。"""
    from netsentinel.ops.aimd import AIMDController

    times = ScriptedTimes(
        [
            (0.0, 0.0, 0.0, 0.0, 0.0),
            (2.0, 0.0, 0.0, 0.0, 0.0),  # 窗口 1:Δcpu 2.0 / Δwall 1.0 → 2.0
            (2.5, 0.0, 0.0, 0.0, 0.0),  # 窗口 2:Δcpu 0.5 / Δwall 1.0 → 0.5
            (4.5, 0.0, 0.0, 0.0, 0.0),  # 窗口 3:Δcpu 2.0 / Δwall 1.0 → 2.0
        ]
    )
    clock = FakeClock(100.0)
    guard = LoadGuard(times_fn=times, clock_fn=clock)
    aimd = AIMDController(8, calm_round=100)

    guard.should_throttle()  # 首采建基线
    assert aimd.on_signal(guard.last_utilization()) is False  # None 按平静
    assert aimd.permits == 8

    clock.advance(1.0)  # util 2.0 > 0.92(与护栏让位同款阈值)
    assert guard.should_throttle() is True  # 护栏自身判定不受消费方影响
    assert aimd.on_signal(guard.last_utilization()) is True  # 8 → 4
    assert aimd.permits == 4

    clock.advance(1.0)  # util 0.5:平静
    assert guard.should_throttle() is False
    assert aimd.on_signal(guard.last_utilization()) is False
    assert aimd.permits == 4

    clock.advance(1.0)  # util 2.0:再次过载 → 4 → 2
    assert guard.should_throttle() is True  # 先到期重采样(last_utilization 只读缓存)
    assert aimd.on_signal(guard.last_utilization()) is True
    assert aimd.permits == 2
    assert times.calls == 4  # 消费读数零额外采样(interval 内复用缓存)
