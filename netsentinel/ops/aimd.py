"""NetSentinel(净网哨兵)AIMD 自适应并发调速器(netsentinel.ops.aimd,A212)。

对标自适应拥塞控制的 **AIMD 范式**(Additive Increase / Multiplicative
Decrease,TCP 拥塞窗口的同族思想),把 ``ops.pool`` 的**静态阈值背压**
(积压 > workers×2 一律多停一轮)升级为**信号驱动的动态并发窗口**:

- **乘性减(MD)**:过载信号(进程 CPU 利用率超阈 / EWMA 任务时延超
  基线倍数)→ ``permits = max(floor, permits // 2)``——窗口砍半、快速退避;
- **加性增(AI)**:平稳每 ``calm_round`` 个任务完成 → ``permits += 1``,
  饱和在 ``cap``——线性试探回升,不冲回过载;
- **地板 ``floor ≥ 1``**:窗口永不归零——再重的过载也保留至少 1 个许可,
  调速器自身不可能把扫描池饿死(进步性红线);
- **上限 ``cap``**:调用方注入的档位 workers(三档公式换算,恒 ≤ 物理核数,
  红线 37)——窗口扩张**永不越过**档位并发上限,AIMD 只会在档位之内
  收紧,绝不会突破档位去"压榨"。

安全红线(必须体现在代码与用法里):

1. **红线 35(压榨边界)**:本调速器只调节**本地计算 / 本地回环 IO 的
   并发窗口**(提交侧背压停顿的触发阈值)。对外网络的礼貌间隔、引擎
   限速、举报频控一概不放宽:在 ``ops.pool`` 的集成形态下,乘性减只会
   **增加**提交侧停顿(更礼貌),加性增最多回到"每提交一次礼貌停顿"的
   现状基线——停顿时长恒由 pool 的固定常量 ``PAUSE_BASE_S +
   PAUSE_JITTER_S × jitter`` 组成,本模块不持有、不计算任何停顿时长;
2. **红线 37(资源治理)**:``cap`` 必须由调用方注入档位 workers
   (≤ 物理核数)。内核自身**不探测核数**(纯逻辑、确定性、可注入),
   只做 ``cap ≥ 1`` / ``floor ≥ 1`` / ``floor ≤ cap`` 的构造校验;
3. **失败隔离**:``on_task_done`` / ``on_signal`` 的脏输入(负数 / NaN /
   inf / 非数)一律按"无信号"忽略,绝不抛错拖垮扫描池。

纯逻辑内核(零 IO、零网络、零隐藏时钟):

- 时钟可注入(``clock_fn``,缺省 ``time.monotonic``):池集成形态经
  :meth:`AIMDController.now` 读取,测试注入脚本时钟即得**确定性时长**;
- 信号可注入:利用率来自 ``ops.load_guard.LoadGuard.last_utilization()``
  读数口(样本不足的 ``None`` 按"平静"处理),任务时长来自池 worker 的
  真实墙钟差分;
- 同一脚本输入永远得到同一状态转移(见 :func:`kernel_selfcheck`),
  供 benchmarks/kernel_bench 与回归测试对账。

acquire/release 语义映射(池集成视角):

- **acquire 侧(节流决策)**:提交循环在提交下一个任务前调
  ``should_pause(pending)``,``pending``(已提交未完成数)≥ ``permits``
  → 建议多停一轮礼貌间隔(等价于"许可已用尽");
- **release 侧(完成回调)**:任务结束(含失败)由计时适配器调
  ``on_task_done(duration_s)``——先更新 EWMA 并做时延过载判定,再记
  平稳完成计数驱动加性增。

线程安全:内部一把 ``threading.Lock`` 保护全部簿记(窗口/计数/EWMA),
锁内零 IO、零睡眠——多 worker 并发回调安全,且绝不长期占锁
(沿用 :class:`ops.load_guard.LoadGuard` 的锁纪律)。

多进程注意:``__getstate__``/``__setstate__`` 重建锁使控制器可 pickle
(进程池注入形态提交不炸),但各进程反序列化后状态独立——父进程窗口
看不到子进程的完成回调,进程池形态下窗口退化为初始值(仍有界于 cap,
安全;如需汇合由调用方自行回传计数)。

用法示例(线程池形态,与 ops.pool 集成)::

    from netsentinel.ops.aimd import AIMDController
    from netsentinel.ops.concurrency import io_workers
    from netsentinel.ops.load_guard import LoadGuard
    from netsentinel.ops.pool import run_pool

    guard = LoadGuard()                                   # A167 利用率读数
    aimd = AIMDController(io_workers(cfg))                # cap = 档位 workers(≤ 核数)
    run_pool(cfg, items, workers=io_workers(cfg), aimd=aimd)  # 注入即启用
    # 旁路喂利用率信号(高档本地计算循环里):
    if guard.should_throttle():
        aimd.on_signal(guard.last_utilization())          # 过载 → 窗口减半

可观测性:每次乘性减累加 ``aimd.decrease``、每次真实加性增累加
``aimd.increase``,窗口变化记 gauge ``aimd.permits``(只存数字,红线 17/23)。
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Callable

from netsentinel import telemetry

__all__ = [
    "DEFAULT_ALPHA",
    "DEFAULT_CALM_ROUND",
    "DEFAULT_LATENCY_FACTOR",
    "DEFAULT_UTIL_THRESHOLD",
    "AIMDController",
    "kernel_selfcheck",
]

logger = logging.getLogger(__name__)

#: 利用率过载阈值(与 ops.load_guard.DEFAULT_THRESHOLD 同款口径:严格大于才减窗)
DEFAULT_UTIL_THRESHOLD: float = 0.92

#: EWMA 平滑系数(标准指数加权移动平均:ewma = (1-α)·ewma + α·新样本)
DEFAULT_ALPHA: float = 0.25

#: 加性增周期:平稳完成每该数量个任务,窗口 +1(线性试探回升)
DEFAULT_CALM_ROUND: int = 4

#: 时延过载倍数:EWMA 任务时延严格大于 baseline_s × 该倍数 → 乘性减
DEFAULT_LATENCY_FACTOR: float = 2.0

#: 墙钟函数类型:返回单调递增秒数(可注入脚本时钟)
_ClockFn = Callable[[], float]


def _positive_finite(value: Any, name: str) -> float:
    """把入参规范成**正的有限浮点数**;非数 / 非有限 / ≤ 0 抛中文 ValueError。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须为正的有限数,收到:{value!r}") from None
    if not math.isfinite(num) or num <= 0.0:
        raise ValueError(f"{name} 必须为正的有限数,收到:{value!r}")
    return num


class AIMDController:
    """AIMD 自适应并发窗口控制器(纯逻辑内核,时钟/信号可注入,A212)。

    状态机(全部转移在内部锁内完成,同一脚本输入 → 同一状态序列):

    ========== ============================== ==============================
    事件        条件                            转移
    ========== ============================== ==============================
    on_signal   util 严格 > util_threshold      permits=max(floor,permits//2),
                                                 平静计数清零(减窗)
    on_task_    ewma 严格 > baseline×factor      同上(时延过载减窗)
    done        其余(平稳完成)                  每 calm_round 个:permits
                                                 =min(cap,permits+1)
    ========== ============================== ==============================

    :param cap: 窗口上限(档位 workers,恒 ≥ 1;红线 37:由调用方保证
        ≤ 物理核数——本内核不探测核数以保持确定性)。初始窗口 = cap
        (乐观起步:先按档位全速跑,过载再退避)。
    :param floor: 窗口地板(≥ 1 且 ≤ cap):乘性减的钳制下界,保证
        永远至少 1 个许可(调速器不可能饿死扫描池)。
    :param calm_round: 加性增周期(≥ 1):平稳完成每该数量个任务窗口 +1。
    :param alpha: EWMA 平滑系数,取值 (0, 1](1 即"最新样本",无平滑)。
    :param util_threshold: 利用率过载阈值(正有限;严格大于才减窗,
        缺省 0.92 与 :class:`ops.load_guard.LoadGuard` 同款)。
    :param latency_factor: 时延过载倍数(正有限;EWMA 严格大于
        ``baseline_s × latency_factor`` 才减窗,缺省 2.0)。
    :param baseline_s: 时延基线(秒);``None`` 时由**首个有效样本**播种
        (确定性:同一脚本 → 同一基线)。零基线无法定义倍数,时延判定
        自动跳过(只保留利用率信号路径)。
    :param clock_fn: 墙钟函数(缺省 ``time.monotonic``),经 :meth:`now`
        暴露给计时适配器;注入脚本时钟即得确定性时长。
    :raises ValueError: 任一参数越界(中文消息,含参数名与实际值)。
    """

    __slots__ = (
        "cap",
        "floor",
        "calm_round",
        "alpha",
        "util_threshold",
        "latency_factor",
        "_baseline_s",
        "_clock_fn",
        "_lock",
        "_permits",
        "_calm",
        "_ewma_s",
        "_decreases",
        "_increases",
        "_samples",
    )

    def __init__(
        self,
        cap: int,
        *,
        floor: int = 1,
        calm_round: int = DEFAULT_CALM_ROUND,
        alpha: float = DEFAULT_ALPHA,
        util_threshold: float = DEFAULT_UTIL_THRESHOLD,
        latency_factor: float = DEFAULT_LATENCY_FACTOR,
        baseline_s: float | None = None,
        clock_fn: _ClockFn | None = None,
    ) -> None:
        try:
            cap_i = int(cap)
        except (TypeError, ValueError):
            raise ValueError(f"cap 必须为正整数(档位 workers),收到:{cap!r}") from None
        try:
            floor_i = int(floor)
        except (TypeError, ValueError):
            raise ValueError(f"floor 必须为不小于 1 的整数,收到:{floor!r}") from None
        try:
            calm_i = int(calm_round)
        except (TypeError, ValueError):
            raise ValueError(
                f"calm_round 必须为不小于 1 的整数,收到:{calm_round!r}"
            ) from None
        if cap_i < 1:
            raise ValueError(f"cap 必须为正整数(档位 workers),收到:{cap!r}")
        if floor_i < 1:
            raise ValueError(f"floor 必须为不小于 1 的整数(进步性红线),收到:{floor!r}")
        if floor_i > cap_i:
            raise ValueError(
                f"floor({floor_i}) 不得超过 cap({cap_i}):地板必须落在窗口界内"
            )
        if calm_i < 1:
            raise ValueError(f"calm_round 必须为不小于 1 的整数,收到:{calm_round!r}")
        self.cap = cap_i
        self.floor = floor_i
        self.calm_round = calm_i
        self.alpha = _positive_finite(alpha, "alpha")
        if self.alpha > 1.0:
            raise ValueError(f"alpha 必须落在 (0, 1],收到:{alpha!r}")
        self.util_threshold = _positive_finite(util_threshold, "util_threshold")
        self.latency_factor = _positive_finite(latency_factor, "latency_factor")
        self._baseline_s = (
            None if baseline_s is None else _positive_finite(baseline_s, "baseline_s")
        )
        self._clock_fn: _ClockFn = clock_fn or time.monotonic
        self._lock = threading.Lock()
        self._permits = cap_i  # 乐观起步:满窗开场,过载信号再乘性退避
        self._calm = 0
        self._ewma_s: float | None = None
        self._decreases = 0
        self._increases = 0
        self._samples = 0

    # ------------------------------------------------------------------
    # 内部:乘性减 / 加性增(调用方必须已持有 self._lock)
    # ------------------------------------------------------------------
    def _decrease_locked(self, reason: str) -> None:
        """乘性减:``permits = max(floor, permits // 2)``,平静计数清零。

        已在地板时窗口不变,但过载反应仍计数(``aimd.decrease``)——
        遥测反映的是**信号次数**,不是窗口变化次数。
        """
        new_permits = max(self.floor, self._permits // 2)
        self._calm = 0  # 经典 AIMD:每次拥塞事件后重起加性增周期
        self._permits = new_permits
        self._decreases += 1
        telemetry.inc("aimd.decrease")
        telemetry.gauge("aimd.permits", float(new_permits))
        logger.debug("AIMD 乘性减(%s):permits → %d(floor=%d)", reason, new_permits, self.floor)

    def _increase_locked(self) -> bool:
        """加性增:平静计数满 ``calm_round`` → ``permits = min(cap, permits+1)``。

        返回是否发生**真实**增长(已饱和在 cap 时不计 ``aimd.increase``)。
        """
        self._calm += 1
        if self._calm < self.calm_round:
            return False
        self._calm = 0
        new_permits = min(self.cap, self._permits + 1)
        if new_permits == self._permits:
            return False  # 饱和在 cap:窗口不动,也不伪造一次增长
        self._permits = new_permits
        self._increases += 1
        telemetry.inc("aimd.increase")
        telemetry.gauge("aimd.permits", float(new_permits))
        logger.debug("AIMD 加性增:permits → %d(cap=%d)", new_permits, self.cap)
        return True

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------
    @property
    def permits(self) -> int:
        """当前并发窗口(许可数),恒有 ``floor ≤ permits ≤ cap``(线程安全只读)。"""
        with self._lock:
            return self._permits

    def now(self) -> float:
        """读注入时钟(缺省 ``time.monotonic``):计时适配器用它取任务起止。

        不加锁(时钟函数须线程安全,标准库单调钟满足);测试注入脚本
        时钟即得**确定性时长**——两次 :meth:`now` 的差即被记录的任务时长。
        """
        return float(self._clock_fn())

    def state(self) -> dict[str, Any]:
        """状态快照(观测/自检用,浅拷贝):窗口三件套 + EWMA + 操作计数。"""
        with self._lock:
            return {
                "permits": self._permits,
                "cap": self.cap,
                "floor": self.floor,
                "calm_count": self._calm,
                "ewma_s": self._ewma_s,
                "baseline_s": self._baseline_s,
                "decreases": self._decreases,
                "increases": self._increases,
                "samples": self._samples,
            }

    def on_signal(self, util: float | None) -> bool:
        """外部利用率信号(消费 :meth:`ops.load_guard.LoadGuard.last_utilization`
        读数),过载则乘性减。

        - ``util`` **严格大于** ``util_threshold`` → 减窗(返回 ``True``);
          恰等于阈值不减(与 LoadGuard 的"严格大于才让位"同款口径);
        - ``None``(样本不足)/ 负数 / 非有限数 / 非数 → 按**平静**处理
          (返回 ``False``,不动窗口)——脏读数绝不误杀;
        - 重复过载信号每次都减(调用方按采样节奏喂,缺省 LoadGuard 的
          ``interval_s=0.5s`` 就是天然限频);已在地板时窗口不变仍计一次
          过载反应(见 :meth:`_decrease_locked`)。

        线程安全;本方法零 IO、零睡眠,绝不抛错(脏输入静默降级)。
        """
        if util is None:
            return False
        try:
            value = float(util)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(value) or value < 0.0:
            return False
        if value <= self.util_threshold:
            return False
        with self._lock:
            self._decrease_locked(f"util={value:.3f}>{self.util_threshold}")
        return True

    def on_task_done(self, duration_s: float) -> bool:
        """任务完成回调(release 侧):更新 EWMA → 时延过载判定 → 平稳计数。

        顺序与语义(全在锁内,确定性):

        1. **脏时长防御**:负数 / NaN / inf / 非数 → 整体忽略(返回
           ``False``,不更新 EWMA、不计平稳完成);
        2. **EWMA 更新**:首个有效样本直接播种;此后
           ``ewma = (1-α)·ewma + α·duration``;``baseline_s`` 未注入时由
           首个有效样本播种(零基线 → 时延判定自动跳过);
        3. **时延过载判定**:``ewma > baseline × latency_factor`` → 乘性减
           (返回 ``True``,平静计数清零)——**先判过载再计平稳**,拥塞
           事件不当"平稳完成"记账;
        4. **平稳计数**:每 ``calm_round`` 个平稳完成窗口 +1 至 cap
           (:meth:`_increase_locked`,返回 ``False``)。

        线程安全(多 worker 并发回调);失败任务同样喂本方法(墙钟时长
        照实计量),由池侧计时适配器保证。
        """
        try:
            duration = float(duration_s)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(duration) or duration < 0.0:
            return False
        with self._lock:
            if self._ewma_s is None:
                self._ewma_s = duration
            else:
                self._ewma_s = (1.0 - self.alpha) * self._ewma_s + self.alpha * duration
            if self._baseline_s is None:
                self._baseline_s = duration
            self._samples += 1
            if (
                self._baseline_s > 0.0
                and self._ewma_s > self._baseline_s * self.latency_factor
            ):
                self._decrease_locked(
                    f"ewma={self._ewma_s:.3f}>baseline×{self.latency_factor}"
                )
                return True
            self._increase_locked()
            return False

    def should_pause(self, pending: int) -> bool:
        """提交侧节流决策(acquire 侧):``pending ≥ permits`` → 建议多停一轮。

        - ``pending`` 为当前**已提交未完成**的任务数(池提交循环的 O(F)
          扫描结果,与静态背压同款口径);
        - ``pending < permits`` → ``False``(许可未用尽,照常提交);
        - ``pending ≤ 0`` 恒 ``False``(``floor ≥ 1`` 保证窗口永不为 0,
          空转提交侧永不被节流);脏输入(非整数)按 0 处理。
        """
        try:
            count = int(pending)
        except (TypeError, ValueError):
            return False
        if count <= 0:
            return False
        with self._lock:
            return count >= self._permits

    # ------------------------------------------------------------------
    # pickle 支持(进程池注入形态:提交不炸;各进程状态独立,见模块文档)
    # ------------------------------------------------------------------
    def __getstate__(self) -> dict[str, Any]:
        """序列化状态(锁不可 pickle,排除后由 __setstate__ 重建)。"""
        with self._lock:
            return {
                "cap": self.cap,
                "floor": self.floor,
                "calm_round": self.calm_round,
                "alpha": self.alpha,
                "util_threshold": self.util_threshold,
                "latency_factor": self.latency_factor,
                "baseline_s": self._baseline_s,
                "clock_fn": self._clock_fn,
                "permits": self._permits,
                "calm": self._calm,
                "ewma_s": self._ewma_s,
                "decreases": self._decreases,
                "increases": self._increases,
                "samples": self._samples,
            }

    def __setstate__(self, state: dict[str, Any]) -> None:
        """反序列化:恢复簿记并**重建锁**(此后状态可继续演进)。"""
        self.cap = state["cap"]
        self.floor = state["floor"]
        self.calm_round = state["calm_round"]
        self.alpha = state["alpha"]
        self.util_threshold = state["util_threshold"]
        self.latency_factor = state["latency_factor"]
        self._baseline_s = state["baseline_s"]
        self._clock_fn = state["clock_fn"]
        self._permits = state["permits"]
        self._calm = state["calm"]
        self._ewma_s = state["ewma_s"]
        self._decreases = state["decreases"]
        self._increases = state["increases"]
        self._samples = state["samples"]
        self._lock = threading.Lock()

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        with self._lock:
            return (
                f"AIMDController(permits={self._permits}, cap={self.cap}, "
                f"floor={self.floor})"
            )


# ---------------------------------------------------------------------------
# A212 内核自检(确定性脚本;口径与 sched_kernel.kernel_selfcheck 四键一致)
# ---------------------------------------------------------------------------
def kernel_selfcheck() -> dict[str, Any]:
    """确定性自检:固定脚本下的窗口序列逐点比对(零 IO、零真实时钟)。

    脚本(cap=8, floor=2, calm_round=2, baseline=1.0, α=0.25, 倍数=2.0):

    - 连续 4 次利用率过载 → 窗口序列 ``[4, 2, 2, 2]``(乘性减,floor 钳住);
    - 12 个平稳完成(时长 1.0 / 3.5 / 1.0×10)→ 窗口自 2 线性回升
      ``[2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8]`` 并**恰饱和在 cap=8**
      (首个完成仅平静计数 1/2,窗口未动;每 2 个平稳完成 +1);
    - 时延尖峰(1.0 → 10.0,EWMA=3.25 > 1.0×2)→ 单独乘性减 8→4;
    - 操作计数不变量:EWMA 更新次数 == 有效完成数、
      真实加性增次数 == 窗口净增长量(饱和不虚计)。

    主四键(``name/metric/value/baseline``)与 V7 sched_kernel 同口径,
    附加键收入 ``extra`` 留档(benchmarks/kernel_bench 未注册本模块,
    供 A212 单测与后续基准总控调用)。
    """
    ctl = AIMDController(8, floor=2, calm_round=2, baseline_s=1.0)
    halving: list[int] = []
    for _ in range(4):
        ctl.on_signal(0.99)
        halving.append(ctl.permits)

    regrown: list[int] = []
    ctl.on_task_done(1.0)  # ewma=1.0(=基线,未越 2.0)→ 平稳 1/2
    regrown.append(ctl.permits)
    ctl.on_task_done(3.5)  # ewma=1.625(仍未越)→ 平稳 2/2 → +1
    regrown.append(ctl.permits)
    for _ in range(10):
        ctl.on_task_done(1.0)
        regrown.append(ctl.permits)

    spike = AIMDController(8, floor=2, calm_round=2, baseline_s=1.0)
    spike.on_task_done(1.0)  # ewma=1.0
    spike.on_task_done(10.0)  # ewma=0.75×1.0+0.25×10=3.25 > 2.0 → 乘性减
    spike_permits = spike.permits

    st = ctl.state()
    # 操作计数不变量:12 个有效完成 == EWMA 样本数;窗口自 2 涨到 8
    # 恰 6 次真实加性增(4 次利用率减窗不触碰增长计数)
    ops_ok = (
        st["samples"] == 12
        and st["increases"] == 6
        and st["decreases"] == 4
        and spike.state()["decreases"] == 1
    )
    return {
        "name": "aimd",
        "metric": "regrow_to_cap_permits",
        "value": ctl.permits,
        "baseline": 8,
        "halving_seq": halving,
        "halving_expected": [4, 2, 2, 2],
        "regrow_seq": regrown,
        "regrow_expected": [2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8],
        "latency_spike_permits": spike_permits,
        "op_counts_invariant": ops_ok,
    }
