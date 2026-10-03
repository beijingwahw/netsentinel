"""NetSentinel(净网哨兵)进程负载护栏(netsentinel.ops.load_guard,A167)。

高档并发(契约 §1 ``high`` 档)最大限度压榨 CPU——但只作用于**本地计算
与本地回环 IO**(红线 35):对外网络的礼貌间隔(``fetch_delay_s``)、
引擎限速、举报频控(红线 26)一概不放宽,也与本模块无关。

本模块是红线 37 的"CPU 过载让位保护":在高档本地计算循环里埋一个
**让位点**,当进程 CPU 利用率超过阈值时短暂睡眠(0.05s),把机器让给
操作系统与其他任务,避免高档压榨把整机拖死。

采样口径(进程 CPU 利用率,纯标准库):

- CPU 时间:``os.times()`` 的 ``user + system``(本进程自身消耗,不含
  ``children_*`` 子进程部分);
- 墙钟:可注入的 ``clock_fn``(缺省 ``time.monotonic``);
- 利用率 = 相邻两次采样之间 ``(user+system)`` 差分 ÷ 墙钟差分。
  单线程进程该值落在 ``[0, 1]``;多线程并行时可能大于 1(多核叠加),
  同样按"超过阈值"处理。

节流判定(:meth:`LoadGuard.should_throttle`):

- **样本不足 → False**:首次调用只建立基线(没有差分可算);墙钟停滞
  或回退(距上次采样不足 ``interval_s``)时复用缓存判定,同样不会抛
  ``ZeroDivisionError``;
- 利用率**严格大于** ``threshold`` 才让位(恰好等于阈值不让位);
- 相邻两次**真实采样**至少间隔 ``interval_s``:间隔内的调用复用缓存
  样本,判定与 :meth:`LoadGuard.last_utilization` 保持稳定——高频探询
  本身不应推高 CPU 读数。

让位动作(:meth:`LoadGuard.maybe_yield`):判定为 True 时执行
``sleep(YIELD_SLEEP_S)``(0.05 秒,可注入 ``sleep_fn``)并返回 True;
False 时什么都不做、返回 False。

线程安全与可中断(红线 37):

- 内部一把 ``threading.Lock`` 只保护采样簿记(读 ``os.times()`` 与两次
  减法),**睡眠发生在锁外**——过载让位绝不长期占锁,不阻塞其他线程
  的判定与让位;
- 单次让位固定 0.05 秒、有界可中断;高档循环每次迭代重新判定,不存在
  无法中止的长睡眠,失控循环不会因此卡死。

高档循环的让位点(用法示例)::

    from netsentinel.ops.load_guard import LoadGuard

    guard = LoadGuard(interval_s=0.5)          # threshold 缺省 0.92
    for item in heavy_items:                   # 高档并行分类/打包等本地计算
        result = heavy_local_compute(item)     # 只压榨本地 CPU(红线 35)
        if guard.maybe_yield():                # ← 让位点:CPU>92% 时 sleep 0.05s
            log("进程负载 %.0f%%,让位 50ms", (guard.last_utilization() or 0) * 100)

全部依赖(``os.times`` / 墙钟 / 睡眠)均可注入:``times_fn``、
``clock_fn``、``sleep_fn``;测试零真实 sleep、零网络、完全离线。
构造函数**不做任何采样**(惰性),首次调用 :meth:`should_throttle`
才建立基线。
"""
from __future__ import annotations

import math
import os
import threading
import time
from typing import Callable, Sequence

__all__ = [
    "DEFAULT_INTERVAL_S",
    "DEFAULT_THRESHOLD",
    "YIELD_SLEEP_S",
    "LoadGuard",
]

#: 缺省采样间隔(秒):两次真实采样之间的最小墙钟距离
DEFAULT_INTERVAL_S: float = 0.5

#: 缺省节流阈值:进程 CPU 利用率严格大于该值才让位
DEFAULT_THRESHOLD: float = 0.92

#: 单次让位的睡眠时长(秒):有界、可中断(红线 37)
YIELD_SLEEP_S: float = 0.05

#: times 采样函数类型:返回 (user, system, children_user, children_system, elapsed)
_TimesFn = Callable[[], Sequence[float]]

#: 墙钟函数类型:返回单调递增的秒数
_ClockFn = Callable[[], float]

#: 睡眠函数类型:接受秒数
_SleepFn = Callable[[float], None]


class LoadGuard:
    """进程 CPU 负载护栏:高档本地计算循环的让位点(契约 V9 §3 A167)。

    用 ``os.times()`` 差分估算**本进程** CPU 利用率,超过 ``threshold``
    时 :meth:`maybe_yield` 睡眠 ``YIELD_SLEEP_S`` 秒让位。纯标准库、
    线程安全;``times_fn`` / ``clock_fn`` / ``sleep_fn`` 均可注入。

    :param interval_s: 相邻两次真实采样的最小墙钟间隔(秒,必须为正的
        有限数);间隔内的调用复用缓存样本。
    :param threshold: 节流阈值,利用率**严格大于**该值才让位(必须为正
        的有限数;缺省 0.92)。
    :param times_fn: CPU 时间采样函数,缺省 :func:`os.times`(只取
        ``user + system``)。
    :param sleep_fn: 睡眠函数,缺省 :func:`time.sleep`。
    :param clock_fn: 墙钟函数,缺省 :func:`time.monotonic`。
    :raises ValueError: ``interval_s`` / ``threshold`` 非正或非有限数。
    """

    __slots__ = (
        "interval_s",
        "threshold",
        "_clock_fn",
        "_lock",
        "_sleep_fn",
        "_times_fn",
        "_last_cpu",
        "_last_wall",
        "_utilization",
    )

    def __init__(
        self,
        interval_s: float = DEFAULT_INTERVAL_S,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        times_fn: _TimesFn | None = None,
        sleep_fn: _SleepFn | None = None,
        clock_fn: _ClockFn | None = None,
    ) -> None:
        interval_s = float(interval_s)
        threshold = float(threshold)
        if not math.isfinite(interval_s) or interval_s <= 0.0:
            raise ValueError(f"interval_s 必须为正的有限秒数,收到:{interval_s!r}")
        if not math.isfinite(threshold) or threshold <= 0.0:
            raise ValueError(f"threshold 必须为正的有限利用率阈值,收到:{threshold!r}")
        self.interval_s = interval_s
        self.threshold = threshold
        self._times_fn: _TimesFn = times_fn or os.times
        self._sleep_fn: _SleepFn = sleep_fn or time.sleep
        self._clock_fn: _ClockFn = clock_fn or time.monotonic
        # 红线 37:锁只护采样簿记,绝不跨睡眠持有(见 maybe_yield)。
        self._lock = threading.Lock()
        self._last_wall: float | None = None
        self._last_cpu: float | None = None
        self._utilization: float | None = None

    # ------------------------------------------------------------------
    # 内部:采样(必须在持有 self._lock 时调用)
    # ------------------------------------------------------------------
    def _sample_locked(self) -> float | None:
        """按需采一次样并返回最新利用率;样本不足时返回 ``None``。

        - 首次调用:只建立基线(user+system 与墙钟),无差分 → ``None``;
        - 距上次真实采样不足 ``interval_s``(含墙钟回退/停滞):复用缓存
          利用率,不消耗新的 times 样本;
        - 到期:取新样本,利用率 = Δ(user+system) / Δ墙钟(钳到 ≥ 0,
          防御 times 读数回退)。
        """
        now = float(self._clock_fn())
        if self._last_wall is None:
            first = self._times_fn()
            self._last_wall = now
            self._last_cpu = float(first[0]) + float(first[1])
            return None  # 首采:只有基线,样本不足
        if now - self._last_wall < self.interval_s:
            return self._utilization  # interval 内:复用缓存样本
        sample = self._times_fn()
        wall_dt = now - self._last_wall
        cpu_now = float(sample[0]) + float(sample[1])
        # 到期分支必有 wall_dt >= interval_s > 0;此判断是防御未来重构的
        # 硬护栏,保证任何时钟异常都不产生除零。
        if wall_dt <= 0.0:  # pragma: no cover - 不可达防御分支
            return self._utilization
        cpu_dt = cpu_now - self._last_cpu  # 基线分支已保证非 None
        self._last_wall = now
        self._last_cpu = cpu_now
        self._utilization = max(0.0, cpu_dt / wall_dt)
        return self._utilization

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------
    def should_throttle(self) -> bool:
        """判定当前是否应让位。

        - 样本不足(首采只建基线 / interval 内无新样本且尚无缓存)→
          ``False``;
        - 最新利用率**严格大于** ``threshold`` → ``True``(恰好等于阈值
          不让位);
        - 其余(含利用率 ≤ 阈值)→ ``False``。

        线程安全;间隔内的重复调用复用缓存样本,判定稳定。本方法自身
        **绝不睡眠**。
        """
        with self._lock:
            util = self._sample_locked()
        return util is not None and util > self.threshold

    def maybe_yield(self) -> bool:
        """高档循环的让位点:过载则睡眠 ``YIELD_SLEEP_S`` 秒并返回 True。

        等价于 ``should_throttle()`` 为 True 时 ``sleep(0.05)`` 后返回
        ``True``;否则不睡眠、返回 ``False``。睡眠发生在内部锁**之外**,
        让位期间不阻塞其他线程的判定(红线 37:让位保护必须可中断、
        有界,单次固定 0.05 秒)。
        """
        if not self.should_throttle():
            return False
        self._sleep_fn(YIELD_SLEEP_S)
        return True

    def last_utilization(self) -> float | None:
        """最近一次**算出**的进程 CPU 利用率;尚无差分样本时为 ``None``。

        interval 内复用缓存,因此该值与 :meth:`should_throttle` 的判定
        口径一致。

        A212 读数口:本方法是 AIMD 调速器(:class:`ops.aimd.AIMDController`)
        的利用率信号来源——调用方以 ``aimd.on_signal(guard.last_utilization())``
        消费,``None``(样本不足)按"平静"处理、超阈值触发乘性减;本方法
        只读缓存、不采样不睡眠,语义与 A167 完全一致(零改动)。
        """
        with self._lock:
            return self._utilization
