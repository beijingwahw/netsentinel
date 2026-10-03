"""每提供方礼貌限速桶(V4 · A74)。

多平台并发调用视觉模型时按"提供方"独立限速(RPM 意识),防止触发平台
429 或封禁:

- 令牌桶:每桶容量 2(允许小幅突发),补充速率 ``rpm / 60`` 每秒;
- ``acquire(provider) -> float``:阻塞式取令牌,返回本次为等到令牌而
  等待的秒数(0 = 立即获得);真实路径用 ``time.sleep`` 按缺口分段等待;
- ``try_acquire(provider) -> bool``:非阻塞,适合"可跳过"的场景;
- ``state(provider) -> {"tokens": float, "rpm": int}``:只读观测,不消费;
- 未注册的提供方以 ``DEFAULT_RPM`` 动态建桶(线程安全)。

时钟注入:``clock`` 需具备 ``time.monotonic`` 语义(返回单调递增秒);
若注入对象额外提供可调用的 ``sleep(seconds)``(如测试用 FakeClock),
阻塞等待将改为推进该模拟时钟,从而离线、零真实 sleep 可测。
纯标准库实现(threading/time),无第三方依赖。

V5 升级:``acquire`` 锁粒度细化——临界区内只保留 refill / 令牌判定 / 扣减与
rpm 快照捕获,等待时长计算、日志与 sleep 全部移出锁外,睡醒后回到循环
双检(等待期间令牌被并发线程取走属正常竞争);每次**真实等待过**的取令牌
计一次 ``telemetry.inc("throttle.waited")``(多段等待只计一次,立即获得不计)。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from netsentinel import telemetry

__all__ = ["DEFAULT_RPM", "BUCKET_CAPACITY", "ProviderThrottle"]

logger = logging.getLogger(__name__)

#: 未在 rpm_hints 登记的提供方使用的默认限速(次/分钟)
DEFAULT_RPM = 60

#: 每桶容量(令牌数):允许 2 次突发后进入匀速补充
BUCKET_CAPACITY = 2.0

#: 令牌判定浮点容差
_EPS = 1e-9


class _Bucket:
    """单个提供方的令牌桶(内部类,始终在 ProviderThrottle 的锁内访问)。"""

    __slots__ = ("rpm", "rate", "tokens", "last")

    def __init__(self, rpm: int, now: float) -> None:
        self.rpm = int(rpm)
        self.rate = rpm / 60.0  # 令牌/秒
        self.tokens = BUCKET_CAPACITY
        self.last = float(now)

    def refill(self, now: float) -> None:
        """按距上次补充的 elapsed 秒补充令牌,超出容量封顶。"""
        elapsed = now - self.last
        if elapsed <= 0.0:  # 单调时钟不应回退,防御性跳过
            return
        self.tokens = min(BUCKET_CAPACITY, self.tokens + elapsed * self.rate)
        self.last = now


class ProviderThrottle:
    """每提供方限速器:多平台并发场景共享一个实例即可。

    用法::

        throttle = ProviderThrottle({"glm": 60, "openai": 30})
        waited = throttle.acquire("glm")   # 阻塞等到令牌,返回等待秒数
        if throttle.try_acquire("openai"):  # 非阻塞,失败可跳过
            ...
        throttle.state("glm")               # 观测,不消费
    """

    def __init__(
        self,
        rpm_hints: dict[str, int] | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """构造限速器。

        :param rpm_hints: 提供方 → 每分钟调用上限提示;未登记的提供方用
            ``DEFAULT_RPM``。非法值(<1)回退默认并告警。
        :param clock: 可注入时钟,``time.monotonic`` 语义(无参调用返回
            单调递增秒);缺省用 ``time.monotonic``。若该对象附带可调用的
            ``sleep(seconds)``,阻塞等待将用它推进模拟时间(离线测试)。
        """
        self._rpm_hints: dict[str, int] = {}
        for name, rpm in dict(rpm_hints or {}).items():
            rpm = int(rpm)
            if rpm < 1:
                logger.warning(
                    "提供方 %s 的 rpm 提示非法(%d<1),回退默认 %d",
                    name, rpm, DEFAULT_RPM,
                )
                rpm = DEFAULT_RPM
            self._rpm_hints[str(name)] = rpm

        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}

        if clock is None:
            self._clock = time.monotonic
            self._sleep = time.sleep
        else:
            self._clock = clock
            sleeper = getattr(clock, "sleep", None)
            self._sleep = sleeper if callable(sleeper) else time.sleep

    # -- 内部 ---------------------------------------------------------------

    def _bucket_for(self, provider: str) -> _Bucket:
        """取(或按 DEFAULT_RPM 动态创建)提供方的桶。调用方必须已持锁。"""
        bucket = self._buckets.get(provider)
        if bucket is None:
            rpm = self._rpm_hints.get(provider, DEFAULT_RPM)
            bucket = _Bucket(rpm, self._clock())
            self._buckets[provider] = bucket
            logger.debug("为提供方 %s 动态创建限速桶(RPM=%d)", provider, rpm)
        return bucket

    def _refilled(self, provider: str) -> _Bucket:
        """持锁取桶并按当前时钟补充令牌。"""
        bucket = self._bucket_for(provider)
        bucket.refill(self._clock())
        return bucket

    # -- 对外 API -----------------------------------------------------------

    def try_acquire(self, provider: str) -> bool:
        """非阻塞取一个令牌:立即获得返回 True,不足返回 False(不等待)。"""
        provider = str(provider)
        with self._lock:
            bucket = self._refilled(provider)
            if bucket.tokens >= 1.0 - _EPS:
                bucket.tokens -= 1.0
                return True
            return False

    def acquire(self, provider: str) -> float:
        """阻塞取一个令牌,返回本次等待的秒数(0 = 立即)。

        实现循环(V5 锁粒度细化):临界区内只做 refill / 判定 / 扣减并捕获
        (等待秒数, rpm) 快照;等待计算、日志与 sleep 均在锁外执行,睡醒后
        回到循环双检——等待期间令牌可能被并发线程取走,属正常竞争。

        可观测性:本次取令牌**真实等待过**(累计等待 > 0)时计一次
        ``telemetry.inc("throttle.waited")``;立即获得与 ``try_acquire``
        均不计。
        """
        provider = str(provider)
        waited = 0.0
        while True:
            with self._lock:
                bucket = self._refilled(provider)
                if bucket.tokens >= 1.0 - _EPS:
                    bucket.tokens -= 1.0
                    acquired = True
                else:
                    acquired = False
                    # 差多少令牌 → 还需多少秒(缺口 ≤ 1,故单次等待 ≤ 1/rate)
                    wait_s = (1.0 - bucket.tokens) / bucket.rate
                    rpm = bucket.rpm
            if acquired:
                break
            logger.debug(
                "提供方 %s 触发礼貌限速,等待 %.3f 秒(RPM=%d)",
                provider, wait_s, rpm,
            )
            self._sleep(wait_s)
            waited += wait_s
        if waited > 0.0:
            telemetry.inc("throttle.waited")  # 仅真实等待时计一次(多段等待不重复计)
        return waited

    def state(self, provider: str) -> dict[str, float]:
        """只读观测该提供方桶状态(先按当前时钟补充,但不消费令牌)。"""
        provider = str(provider)
        with self._lock:
            bucket = self._refilled(provider)
            return {"tokens": round(bucket.tokens, 6), "rpm": bucket.rpm}
