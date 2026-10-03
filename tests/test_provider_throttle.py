"""tests/test_provider_throttle.py —— A74 每提供方限速桶单元测试。

离线:注入 FakeClock(自带 sleep 推进模拟时间),全程零真实 sleep、
零外呼、零密钥;多线程用例以"容量 + 速率×时长 ≥ 次数"的令牌守恒
上界验证并发 acquire 不超速。
"""
from __future__ import annotations

import logging
import threading
import time

import pytest

from netsentinel.vision.provider_throttle import (
    BUCKET_CAPACITY,
    DEFAULT_RPM,
    ProviderThrottle,
)


# ---------------------------------------------------------------------------
# 测试工具
# ---------------------------------------------------------------------------

class FakeClock:
    """单调假时钟:``()`` 返回当前秒;advance/sleep 显式推进(线程安全)。

    作为 ``clock`` 注入 ProviderThrottle 后,阻塞等待走 ``sleep(seconds)``
    直接推进自身,因此测试不会真实停顿。
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._now

    @property
    def now(self) -> float:
        return self()

    def advance(self, seconds: float) -> None:
        with self._lock:
            if seconds > 0.0:
                self._now += seconds

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


# ---------------------------------------------------------------------------
# 常量与基本行为
# ---------------------------------------------------------------------------

def test_constants() -> None:
    """契约常量:默认 60 RPM,桶容量 2。"""
    assert DEFAULT_RPM == 60
    assert BUCKET_CAPACITY == 2.0


def test_first_acquires_immediate_then_try_acquire_false() -> None:
    """首个(及容量内第二个)立即获得;连续打满容量后 try_acquire 失败。"""
    throttle = ProviderThrottle({"glm": 60}, clock=FakeClock())
    assert throttle.acquire("glm") == 0.0  # 首个立即
    assert throttle.acquire("glm") == 0.0  # 容量 2,第二个仍立即
    assert throttle.try_acquire("glm") is False  # 打满后非阻塞失败


def test_try_acquire_success_within_capacity() -> None:
    """容量内 try_acquire 成功且真实扣减令牌。"""
    throttle = ProviderThrottle({"openai": 30}, clock=FakeClock())
    assert throttle.try_acquire("openai") is True
    assert throttle.try_acquire("openai") is True
    assert throttle.try_acquire("openai") is False


# ---------------------------------------------------------------------------
# 阻塞等待(时钟注入模拟)
# ---------------------------------------------------------------------------

def test_blocking_acquire_waits_with_fake_clock() -> None:
    """打满后阻塞 acquire:FakeClock 推进,返回等待秒 > 0(60RPM → 1.0s)。"""
    clock = FakeClock()
    throttle = ProviderThrottle({"glm": 60}, clock=clock)
    throttle.acquire("glm")
    throttle.acquire("glm")
    waited = throttle.acquire("glm")
    assert waited > 0.0
    assert waited == pytest.approx(1.0)  # 差 1 个令牌,速率 1/秒
    assert clock.now == pytest.approx(1.0)  # 等待由 FakeClock 推进模拟


def test_acquire_after_partial_refill_waits_remainder() -> None:
    """部分回填后阻塞等待只补剩余缺口(60RPM 回填 0.25 → 再等 0.75)。"""
    clock = FakeClock()
    throttle = ProviderThrottle({"glm": 60}, clock=clock)
    throttle.acquire("glm")
    throttle.acquire("glm")
    clock.advance(0.25)  # 回填 0.25 个令牌
    assert throttle.acquire("glm") == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# 速率正确性(state 观测)
# ---------------------------------------------------------------------------

def test_refill_rate_60rpm_via_state() -> None:
    """60 RPM → 每秒回 1 令牌;超出容量封顶;state 断言基于 clock 推进。"""
    clock = FakeClock()
    throttle = ProviderThrottle(clock=clock)  # 未登记提供方用 DEFAULT_RPM
    assert throttle.state("openai") == {"tokens": pytest.approx(2.0), "rpm": 60}
    throttle.acquire("openai")
    throttle.acquire("openai")
    assert throttle.state("openai")["tokens"] == pytest.approx(0.0, abs=1e-6)
    clock.advance(0.5)
    assert throttle.state("openai")["tokens"] == pytest.approx(0.5, abs=1e-6)
    clock.advance(0.5)
    assert throttle.state("openai")["tokens"] == pytest.approx(1.0, abs=1e-6)
    clock.advance(30.0)  # 远超容量,封顶在 2
    assert throttle.state("openai")["tokens"] == pytest.approx(2.0)


def test_state_does_not_consume() -> None:
    """state 只观测不消费:反复调用不改变令牌,后续取令牌不受影响。"""
    throttle = ProviderThrottle({"a": 120}, clock=FakeClock())
    throttle.try_acquire("a")
    before = throttle.state("a")["tokens"]
    for _ in range(5):
        assert throttle.state("a")["tokens"] == pytest.approx(before)
    assert throttle.try_acquire("a") is True  # 剩余令牌仍可取
    assert throttle.try_acquire("a") is False


# ---------------------------------------------------------------------------
# rpm_hints 差异化与未知提供方
# ---------------------------------------------------------------------------

def test_rpm_hints_differentiate_rates() -> None:
    """两家速率不同:60RPM 每令牌等 1.0s,120RPM 每令牌等 0.5s。"""
    slow = ProviderThrottle({"p": 60}, clock=FakeClock())
    fast = ProviderThrottle({"p": 120}, clock=FakeClock())
    for throttle in (slow, fast):
        throttle.acquire("p")
        throttle.acquire("p")
    assert slow.state("p")["rpm"] == 60
    assert fast.state("p")["rpm"] == 120
    assert slow.acquire("p") == pytest.approx(1.0)
    assert fast.acquire("p") == pytest.approx(0.5)


def test_unknown_provider_dynamically_uses_default_rpm() -> None:
    """未注册提供方动态建桶,RPM 取默认 60,行为与登记提供方一致。"""
    throttle = ProviderThrottle({"known": 30}, clock=FakeClock())
    st = throttle.state("brand_new")  # 触发动态建桶
    assert st["rpm"] == DEFAULT_RPM
    assert st["tokens"] == pytest.approx(BUCKET_CAPACITY)
    assert throttle.try_acquire("brand_new") is True


def test_invalid_rpm_falls_back_with_warning(caplog) -> None:
    """rpm 提示 <1 视为非法:回退默认并记录中文告警。"""
    caplog.set_level(logging.WARNING, logger="netsentinel.vision.provider_throttle")
    throttle = ProviderThrottle({"bad": 0, "neg": -5}, clock=FakeClock())
    assert throttle.state("bad")["rpm"] == DEFAULT_RPM
    assert throttle.state("neg")["rpm"] == DEFAULT_RPM
    assert any("回退默认" in rec.getMessage() for rec in caplog.records)


def test_default_clock_wiring_no_real_sleep() -> None:
    """缺省时钟走 time.monotonic/time.sleep:容量内取令牌不触发任何等待。"""
    throttle = ProviderThrottle()
    assert throttle.acquire("glm") == 0.0
    assert throttle.try_acquire("glm") is True
    st = throttle.state("glm")
    assert st["rpm"] == DEFAULT_RPM
    assert 0.0 <= st["tokens"] < 0.01  # 真实时钟下两次消费间仅有微小回填


# ---------------------------------------------------------------------------
# 并发(多线程 acquire 不超速)
# ---------------------------------------------------------------------------

def test_concurrent_acquire_respects_rate() -> None:
    """12 线程并发 acquire:模拟时长 ≥ (N-容量)/速率,零等待次数 ≤ 容量。"""
    clock = FakeClock()
    rpm = 60
    throttle = ProviderThrottle({"glm": rpm}, clock=clock)
    n = 12
    results: list[float] = []
    results_lock = threading.Lock()

    def worker() -> None:
        waited = throttle.acquire("glm")
        with results_lock:
            results.append(waited)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)
    assert not any(th.is_alive() for th in threads)
    assert len(results) == n
    assert all(w >= 0.0 for w in results)
    # 容量 2:至多 2 次零等待(突发)
    assert sum(1 for w in results if w == 0.0) <= BUCKET_CAPACITY
    # 令牌守恒:N 次 acquire ≤ 容量 + 速率×模拟时长 → 时长 ≥ (N-2)/rate
    min_elapsed = (n - BUCKET_CAPACITY) / (rpm / 60.0)
    assert clock.now >= min_elapsed - 1e-6
    # 所有等待均由 FakeClock.sleep 推进:总等待 == 模拟时长
    assert clock.now == pytest.approx(sum(results), abs=1e-6)


def test_concurrent_dynamic_bucket_created_once() -> None:
    """多线程同时首次访问同一未知提供方:只建一个桶,无异常。"""
    throttle = ProviderThrottle(clock=FakeClock())
    workers = 8
    barrier = threading.Barrier(workers)

    def worker() -> None:
        barrier.wait()
        throttle.try_acquire("newbie")

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)
    assert not any(th.is_alive() for th in threads)
    assert len(throttle._buckets) == 1  # 白盒:动态建桶恰好一次
    assert throttle.state("newbie")["rpm"] == DEFAULT_RPM


# ---------------------------------------------------------------------------
# V5:锁粒度细化(等待在锁外)+ throttle.waited 遥测(仅真实等待时)
# ---------------------------------------------------------------------------


class HalfSleepClock(FakeClock):
    """每次 sleep 只推进请求时长的一半:单线程也 deterministic 地多段等待。"""

    def __init__(self, start: float = 0.0) -> None:
        super().__init__(start)
        self.sleep_calls = 0

    def sleep(self, seconds: float) -> None:
        self.sleep_calls += 1
        self.advance(seconds / 2.0)


def test_v5_immediate_acquire_never_counts_waited() -> None:
    """容量内立即获得(含 try_acquire)不计 throttle.waited。"""
    from netsentinel import telemetry

    telemetry.reset()
    try:
        throttle = ProviderThrottle({"glm": 60}, clock=FakeClock())
        assert throttle.acquire("glm") == 0.0
        assert throttle.acquire("glm") == 0.0
        assert throttle.try_acquire("glm") is False
        assert telemetry.snapshot()["counters"].get("throttle.waited", 0.0) == 0.0
    finally:
        telemetry.reset()


def test_v5_waited_acquire_counts_exactly_one() -> None:
    """真实等待一次的 acquire 计一次;再次等待再计一次。"""
    from netsentinel import telemetry

    telemetry.reset()
    try:
        clock = FakeClock()
        throttle = ProviderThrottle({"glm": 60}, clock=clock)
        throttle.acquire("glm")
        throttle.acquire("glm")
        assert throttle.acquire("glm") == pytest.approx(1.0)  # 真实等待(FakeClock 推进)
        counters = telemetry.snapshot()["counters"]
        assert counters.get("throttle.waited") == 1.0
        assert throttle.acquire("glm") == pytest.approx(1.0)
        assert telemetry.snapshot()["counters"].get("throttle.waited") == 2.0
    finally:
        telemetry.reset()


def test_v5_multi_segment_wait_counts_once_per_acquire() -> None:
    """多段等待(时钟只走一半 → 反复补检)只计一次 throttle.waited。"""
    from netsentinel import telemetry

    telemetry.reset()
    try:
        clock = HalfSleepClock()
        throttle = ProviderThrottle({"p": 60}, clock=clock)
        throttle.acquire("p")
        throttle.acquire("p")
        waited = throttle.acquire("p")
        # 每段等待按请求秒数累计(半速时钟下几何级数:Σ 1+0.5+0.25+… ≈ 2.0),
        # 而时钟实际只推进 ≈1.0 秒(缺口 1 令牌 @1/秒)
        assert waited == pytest.approx(2.0, abs=1e-6)
        assert clock.now == pytest.approx(1.0, abs=1e-6)
        assert clock.sleep_calls >= 5                   # 确实经历了多段等待
        assert telemetry.snapshot()["counters"].get("throttle.waited") == 1.0
    finally:
        telemetry.reset()


class BlockingSleepClock(FakeClock):
    """sleep 进入即置位 entered,并阻塞到 release:用于观测锁是否被睡眠持有。"""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def sleep(self, seconds: float) -> None:
        self.entered.set()
        assert self.release.wait(timeout=10), "测试未释放 sleep"
        self.advance(seconds)


def test_v5_sleep_happens_outside_lock() -> None:
    """锁粒度:线程 A 在 acquire 中沉睡时,其他线程仍能 try_acquire/state。"""
    from netsentinel import telemetry

    telemetry.reset()
    clock = BlockingSleepClock()
    throttle = ProviderThrottle({"p": 60}, clock=clock)
    throttle.acquire("p")
    throttle.acquire("p")  # tokens 打空

    result: dict[str, float] = {}

    def blocked_worker() -> None:
        result["waited"] = throttle.acquire("p")

    worker = threading.Thread(target=blocked_worker)
    worker.start()
    try:
        assert clock.entered.wait(timeout=5.0), "acquire 未进入等待"
        # A 正在 clock.sleep 中沉睡:此刻锁必须空闲,否则下面两行会卡死至超时
        assert throttle.try_acquire("p") is False
        assert throttle.state("p")["rpm"] == 60
    finally:
        clock.release.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert result["waited"] == pytest.approx(1.0)
    assert telemetry.snapshot()["counters"].get("throttle.waited") == 1.0
    telemetry.reset()


def test_v5_default_clock_still_wired() -> None:
    """质量:缺省时钟仍为 time.monotonic / time.sleep(注入语义不动)。"""
    throttle = ProviderThrottle()
    assert throttle._clock is time.monotonic
    assert throttle._sleep is time.sleep
