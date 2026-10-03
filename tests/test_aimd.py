"""A212 AIMD 自适应并发调速器(netsentinel.ops.aimd)单元测试。

全部离线、零真实 sleep、零网络:时钟注入脚本钟、利用率信号注入脚本值、
任务时长手算对照;数学性质(AIMD 范式)逐点断言:

- 乘性减:超载(利用率严格超阈 / EWMA 时延超基线倍数)→ 窗口砍半,
  ``max(floor, permits//2)`` **永不穿地板**(floor≥1 进步性);
- 加性增:平稳每 ``calm_round`` 个完成 +1,**恰饱和在 cap** 不虚计增长;
- EWMA:``ewma = (1-α)·ewma + α·x`` 手算对照(α=0.25/0.5/1.0),
  首样本播种、基线注入、零基线跳过时延判定;
- 确定性:同一脚本 → 同一状态序列(注入时钟/信号可复现);
- 并发安全:多线程 on_task_done/on_signal 混跑,窗口恒在 [floor, cap],
  操作计数(decreases)与真返回值逐次对账;
- pickle:进程池注入形态状态可迁移、锁重建后可继续演进;
- LoadGuard 读数口:last_utilization() → on_signal() 消费接线
  (None 平静 / 超阈减窗 / 低载不动)。

A224 防闪烁加固(CONTRACTS-V13 §2 工程清理,A212 自查建议):凡断言
涉及时长 / EWMA / 基线播种的用例,控制器一律注入**脚本钟**
(:class:`SteppingClock` / :class:`FakeClock`),不依赖缺省
``time.monotonic``——即使未来内核把时长计量改接 ``now()``,这些用例
也不会随机器负载漂移(A214/A217 曾各观测到一次负载相关闪烁);
全部语义断言原样保留,仅时钟来源收紧为确定性脚本。

红线对照:cap 由调用方注入档位 workers(本文件固定小值,不依赖核数,
保持确定性);内核不持有停顿时长(AIMD 只调窗口,礼貌间隔在 pool)。
"""
from __future__ import annotations

import math
import pickle
import threading

import pytest

from netsentinel import telemetry
from netsentinel.ops.aimd import (
    DEFAULT_ALPHA,
    DEFAULT_CALM_ROUND,
    DEFAULT_LATENCY_FACTOR,
    DEFAULT_UTIL_THRESHOLD,
    AIMDController,
    kernel_selfcheck,
)
from netsentinel.ops.load_guard import LoadGuard

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class SteppingClock:
    """每次被调用前进 ``step`` 秒的脚本钟(线程安全、确定性)。"""

    def __init__(self, start: float = 0.0, step: float = 1.0) -> None:
        self._value = float(start)
        self._step = float(step)
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            self._value += self._step
            return self._value


class ScriptedTimes:
    """按脚本吐出 times 五元组(与 test_load_guard 同款,自包含拷贝)。"""

    def __init__(self, samples: list[tuple[float, ...]]) -> None:
        self.samples = [tuple(s) for s in samples]
        self.calls = 0

    def __call__(self) -> tuple[float, ...]:
        self.calls += 1
        return self.samples[min(self.calls, len(self.samples)) - 1]


class FakeClock:
    """手动推进的假墙钟。"""

    def __init__(self, start: float = 100.0) -> None:
        self._value = float(start)

    def __call__(self) -> float:
        return self._value

    def advance(self, dt: float) -> None:
        self._value += float(dt)


def run_threads(target, n: int = 8) -> None:
    """起 n 线程并限时汇合(卡死即失败,与 test_load_guard 同款)。"""
    threads = [threading.Thread(target=target) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
        assert not t.is_alive(), "并发调用卡死(疑似锁内 IO/死锁)"


# ---------------------------------------------------------------------------
# 缺省装配与构造校验
# ---------------------------------------------------------------------------
def test_defaults_and_initial_state() -> None:
    ctl = AIMDController(4)
    assert ctl.cap == 4
    assert ctl.floor == 1
    assert ctl.calm_round == DEFAULT_CALM_ROUND == 4
    assert ctl.alpha == DEFAULT_ALPHA == 0.25
    assert ctl.util_threshold == DEFAULT_UTIL_THRESHOLD == 0.92  # 与 LoadGuard 同款
    assert ctl.latency_factor == DEFAULT_LATENCY_FACTOR == 2.0
    assert ctl.permits == 4  # 乐观起步:初始窗口 = cap
    st = ctl.state()
    assert st["calm_count"] == 0
    assert st["ewma_s"] is None and st["baseline_s"] is None
    assert st["decreases"] == st["increases"] == st["samples"] == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"floor": 0},                                   # 地板 < 1(进步性红线)
        {"floor": -2},
        {"floor": 5},                                   # 地板穿 cap
        {"calm_round": 0},                              # 加性增周期 < 1
        {"alpha": 0.0},                                 # α 不落在 (0,1]
        {"alpha": 1.2},
        {"alpha": math.nan},
        {"util_threshold": 0.0},                        # 阈值非正
        {"util_threshold": math.inf},
        {"latency_factor": 0.0},                        # 倍数非正
        {"latency_factor": -1.0},
        {"baseline_s": 0.0},                            # 基线非正
        {"baseline_s": -3.0},
        {"baseline_s": math.nan},
    ],
    ids=lambda d: ",".join(f"{k}={v}" for k, v in d.items()),
)
def test_invalid_params_rejected(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        AIMDController(4, **kwargs)


@pytest.mark.parametrize("bad_cap", [0, -1, "x", None])
def test_invalid_cap_rejected(bad_cap) -> None:
    with pytest.raises(ValueError, match="cap"):
        AIMDController(bad_cap)


def test_alpha_one_means_last_sample() -> None:
    """α=1.0(上界合法):EWMA 退化为"最新样本"(无平滑)。"""
    ctl = AIMDController(
        4, alpha=1.0, baseline_s=10.0, calm_round=100, clock_fn=FakeClock(0.0)
    )
    ctl.on_task_done(2.0)
    ctl.on_task_done(6.0)
    assert ctl.state()["ewma_s"] == 6.0


# ---------------------------------------------------------------------------
# 乘性减:超载砍半,永不穿地板
# ---------------------------------------------------------------------------
def test_util_overload_halving_sequence() -> None:
    """cap=10 连续过载:10 → 5 → 2 → 1 → 1(floor=1 钳住,窗口永不归零)。"""
    ctl = AIMDController(10, calm_round=100)
    seq = []
    for _ in range(5):
        assert ctl.on_signal(0.99) is True  # 每次过载信号都返回 True
        seq.append(ctl.permits)
    assert seq == [5, 2, 1, 1, 1]


def test_halving_never_breaks_custom_floor() -> None:
    """floor=3:10 → 5 → 3 → 3(5//2=2 与 3//2=1 均被地板钳回 3)。"""
    ctl = AIMDController(10, floor=3, calm_round=100)
    seq = []
    for _ in range(4):
        ctl.on_signal(0.99)
        seq.append(ctl.permits)
    assert seq == [5, 3, 3, 3]
    assert ctl.permits >= ctl.floor  # 不变式:窗口 ≥ 地板


def test_util_signal_strict_threshold_boundaries() -> None:
    ctl = AIMDController(4, calm_round=100)
    assert ctl.on_signal(0.92) is False  # 恰等于阈值:严格大于才减
    assert ctl.on_signal(0.9199) is False
    assert ctl.permits == 4
    assert ctl.on_signal(0.93) is True   # 严格超阈 → 4 → 2
    assert ctl.permits == 2
    assert ctl.on_signal(2.0) is True    # 多核叠加 >1 同样按过载
    assert ctl.permits == 1


@pytest.mark.parametrize(
    "dirty", [None, float("nan"), float("inf"), float("-inf"), -0.5, "高负载"]
)
def test_util_signal_dirty_input_calm(dirty) -> None:
    """脏读数(None/非有限/负数/非数)按平静处理,不动窗口、不抛错。"""
    ctl = AIMDController(4, calm_round=100)
    assert ctl.on_signal(dirty) is False
    assert ctl.permits == 4
    assert ctl.state()["decreases"] == 0


def test_decrease_resets_calm_counter() -> None:
    """经典 AIMD:拥塞事件后加性增周期重起(calm 清零)。"""
    ctl = AIMDController(
        8, floor=1, calm_round=3, clock_fn=SteppingClock(start=0.0, step=1.0)
    )
    ctl.on_task_done(1.0)  # 平静 1/3(基线播种 1.0,ewma=1.0 未越 2.0)
    assert ctl.state()["calm_count"] == 1
    ctl.on_signal(0.99)  # 过载减窗:8 → 4,calm 清零
    assert ctl.permits == 4
    assert ctl.state()["calm_count"] == 0
    ctl.on_task_done(1.0)
    ctl.on_task_done(1.0)  # 2/3
    assert ctl.permits == 4  # 差一个平稳完成,不 +1
    ctl.on_task_done(1.0)  # 3/3 → 5
    assert ctl.permits == 5


# ---------------------------------------------------------------------------
# 加性增:每 calm_round 个平稳完成 +1,恰饱和在 cap
# ---------------------------------------------------------------------------
def test_additive_increase_saturates_at_cap() -> None:
    """floor=1/cap=6/calm_round=1:6 次平稳完成恰回到 cap,第 7 次不越界。"""
    ctl = AIMDController(
        6, floor=1, calm_round=1, baseline_s=1.0, clock_fn=SteppingClock()
    )
    ctl.on_signal(0.99)
    ctl.on_signal(0.99)
    ctl.on_signal(0.99)  # 6 → 3 → 1
    assert ctl.permits == 1
    seq = []
    for _ in range(7):
        ctl.on_task_done(1.0)
        seq.append(ctl.permits)
    assert seq == [2, 3, 4, 5, 6, 6, 6]  # 恰饱和,继续完成不再增长
    assert ctl.state()["increases"] == 5  # 1→6 共 5 次真实增长,饱和不虚计


def test_additive_increase_needs_calm_round_completions() -> None:
    """calm_round=4:每 4 个平稳完成恰 +1(3 个不动、第 4 个动)。"""
    ctl = AIMDController(
        4, floor=1, calm_round=4, baseline_s=1.0, clock_fn=SteppingClock()
    )
    ctl.on_signal(0.99)
    ctl.on_signal(0.99)  # 4 → 2 → 1(第二次减半被地板钳住)
    assert ctl.permits == 1
    for _ in range(3):
        ctl.on_task_done(1.0)
    assert ctl.permits == 1  # 3/4:差一个平稳完成,不 +1
    ctl.on_task_done(1.0)  # 第 4 个 → 2
    assert ctl.permits == 2
    assert ctl.state()["calm_count"] == 0  # 触发后计数清零重起周期


# ---------------------------------------------------------------------------
# EWMA:手算对照(可配 α)
# ---------------------------------------------------------------------------
def test_ewma_hand_computed_alpha_025_seeded_baseline() -> None:
    """α=0.25,时长 1.0/3.0/3.0 → ewma 恰为 1.0/1.5/1.875(手算);

    基线未注入 → 首样本播种 baseline=1.0;2.0 倍阈未越 → 全程平稳。
    """
    ctl = AIMDController(4, calm_round=100, clock_fn=SteppingClock())
    ctl.on_task_done(1.0)
    assert ctl.state()["ewma_s"] == 1.0
    assert ctl.state()["baseline_s"] == 1.0  # 首样本播种
    ctl.on_task_done(3.0)
    assert ctl.state()["ewma_s"] == pytest.approx(1.5)  # 0.75×1+0.25×3
    ctl.on_task_done(3.0)
    assert ctl.state()["ewma_s"] == pytest.approx(1.875)  # 0.75×1.5+0.25×3
    assert ctl.permits == 4  # 1.875 < 1.0×2.0 → 无减窗
    assert ctl.state()["samples"] == 3  # 操作计数:有效完成恰记 3 样


def test_ewma_hand_computed_alpha_05_latency_spike() -> None:
    """α=0.5,注入基线 2.0:时长 2/4/10 → ewma 2.0/3.0/6.5;6.5 > 4.0 减窗。"""
    ctl = AIMDController(
        4,
        alpha=0.5,
        latency_factor=2.0,
        baseline_s=2.0,
        calm_round=100,
        clock_fn=SteppingClock(),
    )
    assert ctl.on_task_done(2.0) is False
    assert ctl.state()["ewma_s"] == 2.0
    assert ctl.on_task_done(4.0) is False
    assert ctl.state()["ewma_s"] == 3.0  # 0.5×2+0.5×4
    assert ctl.on_task_done(10.0) is True  # 0.5×3+0.5×10=6.5 > 2.0×2.0 → MD
    assert ctl.state()["ewma_s"] == 6.5
    assert ctl.permits == 2  # 4 → 2
    # 回落:0.5×6.5+0.5×1=3.75 ≤ 4.0 → 平稳
    assert ctl.on_task_done(1.0) is False
    assert ctl.state()["ewma_s"] == 3.75


def test_latency_exact_boundary_no_decrease() -> None:
    """时延判定同样严格大于:ewma 恰等于 baseline×factor 不减窗。"""
    ctl = AIMDController(
        4, baseline_s=2.0, latency_factor=2.0, calm_round=100, clock_fn=SteppingClock()
    )
    ctl.on_task_done(2.0)
    ctl.on_task_done(6.0)
    # 手算(α=0.25):0.75×2.0+0.25×6.0 = 3.0 < 4.0 → 平稳
    assert ctl.state()["ewma_s"] == 3.0
    assert ctl.permits == 4
    ctl2 = AIMDController(
        4, alpha=1.0, baseline_s=2.0, latency_factor=2.0, calm_round=100,
        clock_fn=SteppingClock(),
    )
    ctl2.on_task_done(2.0)
    assert ctl2.on_task_done(4.0) is False  # α=1:ewma=4.0 恰等于阈值 → 不减
    assert ctl2.on_task_done(4.1) is True  # 略越 → 减


def test_zero_baseline_skips_latency_check() -> None:
    """零基线(首样本时长 0)无法定义倍数:时延判定跳过,窗口只受利用率驱动。"""
    ctl = AIMDController(
        4, latency_factor=2.0, calm_round=100, clock_fn=SteppingClock()
    )
    ctl.on_task_done(0.0)
    assert ctl.state()["baseline_s"] == 0.0
    assert ctl.on_task_done(100.0) is False  # ewma=25 但基线 0 → 不判时延
    assert ctl.permits == 4


@pytest.mark.parametrize("dirty", [-1.0, float("nan"), float("inf"), None, "慢"])
def test_dirty_duration_ignored_entirely(dirty) -> None:
    """脏时长整体忽略:不更新 EWMA、不计平稳完成、不触发减窗。"""
    ctl = AIMDController(
        4, baseline_s=1.0, calm_round=2, clock_fn=SteppingClock()
    )
    assert ctl.on_task_done(dirty) is False
    st = ctl.state()
    assert st["samples"] == 0 and st["ewma_s"] is None and st["calm_count"] == 0
    assert ctl.permits == 4


# ---------------------------------------------------------------------------
# should_pause(acquire 侧节流决策)
# ---------------------------------------------------------------------------
def test_should_pause_boundaries() -> None:
    """pending ≥ permits → True(许可用尽);pending < permits → False。"""
    ctl = AIMDController(4, calm_round=100)
    assert ctl.should_pause(3) is False
    assert ctl.should_pause(4) is True   # 恰用尽
    assert ctl.should_pause(9) is True
    assert ctl.should_pause(0) is False  # 空转提交侧永不节流
    assert ctl.should_pause(-5) is False
    assert ctl.should_pause("堵塞") is False  # 脏输入按 0
    ctl.on_signal(0.99)  # 窗口 4 → 2
    assert ctl.should_pause(2) is True
    assert ctl.should_pause(1) is False


def test_floor_permits_keeps_submission_alive() -> None:
    """窗口压到地板 1:单飞在途(pending=1)才停,pending=0 照常提交——
    调速器不可能把扫描池饿死(进步性)。"""
    ctl = AIMDController(8, floor=1, calm_round=100)
    for _ in range(4):
        ctl.on_signal(0.99)
    assert ctl.permits == 1
    assert ctl.should_pause(0) is False
    assert ctl.should_pause(1) is True


# ---------------------------------------------------------------------------
# 确定性:注入时钟 + 同脚本重放
# ---------------------------------------------------------------------------
def test_injected_clock_drives_now() -> None:
    clock = SteppingClock(start=0.0, step=1.5)
    ctl = AIMDController(4, clock_fn=clock)
    assert ctl.now() == 1.5
    assert ctl.now() == 3.0
    assert ctl.now() == 4.5


def test_same_script_same_state_replay() -> None:
    """同一脚本(信号+时长)在两个控制器上重放 → 状态快照逐字段相等。"""

    def replay(ctl: AIMDController) -> dict:
        ctl.on_signal(0.99)
        ctl.on_task_done(1.0)
        ctl.on_task_done(3.0)
        ctl.on_signal(2.5)
        ctl.on_task_done(0.5)
        ctl.on_task_done(0.5)
        ctl.on_task_done(0.5)
        return ctl.state()

    a = AIMDController(8, floor=2, calm_round=2, clock_fn=FakeClock())
    b = AIMDController(8, floor=2, calm_round=2, clock_fn=FakeClock())
    assert replay(a) == replay(b)  # 浮点 EWMA 同运算序列 → 逐位相等


def test_state_snapshot_keys_stable() -> None:
    st = AIMDController(4).state()
    assert set(st) == {
        "permits", "cap", "floor", "calm_count", "ewma_s", "baseline_s",
        "decreases", "increases", "samples",
    }


# ---------------------------------------------------------------------------
# 并发安全(多线程 on_task_done / on_signal)
# ---------------------------------------------------------------------------
def test_concurrent_pure_calm_exact_growth() -> None:
    """纯平稳并发:8 线程 × 25 完成时长恒 1.0 → 窗口恰涨到 cap,
    真实加性增恰 cap-起点 次(锁内计数无丢失、饱和不虚计)。

    A224:注入**常量脚本钟**(FakeClock 只读、线程安全),时长计量
    不再挂接缺省单调钟,负载漂移零通道。"""
    ctl = AIMDController(
        32, calm_round=1, baseline_s=1.0, clock_fn=FakeClock(0.0)
    )
    ctl.on_signal(0.99)  # 32 → 16
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(25):
                assert ctl.on_task_done(1.0) is False
        except BaseException as exc:  # pragma: no cover - 记录后统一断言
            errors.append(exc)

    run_threads(worker)
    assert errors == []
    st = ctl.state()
    assert st["samples"] == 200  # 操作计数:每次有效完成恰记一样本
    assert st["increases"] == 16  # 16 → 32 恰 16 次真实增长
    assert ctl.permits == 32
    assert st["ewma_s"] == 1.0  # 全 1.0 样本:EWMA 精确保持 1.0


def test_concurrent_mixed_signals_invariants() -> None:
    """减窗/增窗混跑:窗口恒在 [floor, cap];decreases 计数与
    全部 True 返回逐次对账(锁内簿记零丢失)。A224:常量脚本钟(只读,
    线程安全),消除时长计量的真实时钟通道。"""
    ctl = AIMDController(
        16, floor=2, calm_round=1, baseline_s=1.0, clock_fn=FakeClock(0.0)
    )
    reactions: list[bool] = []
    lock = threading.Lock()
    errors: list[BaseException] = []

    def calm_worker() -> None:
        try:
            for _ in range(150):
                fired = ctl.on_task_done(1.0)
                with lock:
                    reactions.append(fired)
        except BaseException as exc:  # pragma: no cover - 记录后统一断言
            errors.append(exc)

    def signal_worker() -> None:
        try:
            for _ in range(60):
                fired = ctl.on_signal(0.99)
                with lock:
                    reactions.append(fired)
                assert ctl.on_signal(0.5) is False  # 低载信号永不减窗
        except BaseException as exc:  # pragma: no cover - 记录后统一断言
            errors.append(exc)

    threads = [threading.Thread(target=calm_worker) for _ in range(6)]
    threads += [threading.Thread(target=signal_worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
        assert not t.is_alive(), "并发混跑卡死"
    assert errors == []
    assert 2 <= ctl.permits <= 16  # 不变式:地板 ≤ 窗口 ≤ cap
    st = ctl.state()
    assert st["decreases"] == sum(1 for r in reactions if r)  # True 逐次对账
    assert st["samples"] == 6 * 150
    assert st["calm_count"] < ctl.calm_round  # 计数恒在周期界内


# ---------------------------------------------------------------------------
# pickle(进程池注入形态:状态可迁移,锁重建)
# ---------------------------------------------------------------------------
def test_pickle_roundtrip_preserves_state_and_evolution() -> None:
    """进程池注入形态:状态可迁移(含注入的脚本钟),锁重建后可继续演进。"""
    ctl = AIMDController(
        8, floor=2, calm_round=3, baseline_s=1.0, clock_fn=FakeClock(100.0)
    )
    ctl.on_signal(0.99)
    ctl.on_task_done(1.0)
    clone = pickle.loads(pickle.dumps(ctl))
    assert clone.permits == 4
    assert clone.state() == ctl.state()
    clone.on_task_done(1.0)  # 锁已重建:反序列化后可继续演进
    clone.on_task_done(1.0)  # 3/3 平稳 → +1
    assert clone.permits == 5
    assert clone.on_signal(0.99) is True
    assert clone.permits == 2


# ---------------------------------------------------------------------------
# LoadGuard 读数口 → on_signal 消费接线
# ---------------------------------------------------------------------------
def test_load_guard_readout_feeds_util_signal() -> None:
    """last_utilization() 读数直接可喂:None 平静 / 2.0 过载减窗 / 0.5 不动。"""
    times = ScriptedTimes(
        [
            (0.0, 0.0, 0.0, 0.0, 0.0),
            (2.0, 0.0, 0.0, 0.0, 0.0),   # Δcpu 2.0 / Δwall 1.0 → util 2.0
            (2.5, 0.0, 0.0, 0.0, 0.0),   # Δcpu 0.5 / Δwall 1.0 → util 0.5
        ]
    )
    clock = FakeClock(100.0)
    guard = LoadGuard(times_fn=times, clock_fn=clock)
    ctl = AIMDController(8, calm_round=100)

    guard.should_throttle()  # 首采建基线:last_utilization 为 None
    assert ctl.on_signal(guard.last_utilization()) is False  # None 按平静
    assert ctl.permits == 8

    clock.advance(1.0)  # util 2.0(多核叠加)> 0.92
    assert guard.should_throttle() is True
    assert ctl.on_signal(guard.last_utilization()) is True  # 8 → 4
    assert ctl.permits == 4

    clock.advance(1.0)  # util 0.5:平静
    assert guard.should_throttle() is False
    assert ctl.on_signal(guard.last_utilization()) is False
    assert ctl.permits == 4
    assert times.calls == 3  # 消费读数不触发额外采样


# ---------------------------------------------------------------------------
# 遥测(只存数字,红线 17/23)
# ---------------------------------------------------------------------------
def test_telemetry_counters_and_gauge() -> None:
    telemetry.reset()
    ctl = AIMDController(
        4, floor=1, calm_round=1, baseline_s=1.0, clock_fn=SteppingClock()
    )
    ctl.on_signal(0.99)  # 4 → 2
    ctl.on_task_done(1.0)  # 平稳 1/1 → 3
    ctl.on_task_done(1.0)  # → 4
    snap = telemetry.snapshot()
    assert snap["counters"]["aimd.decrease"] == 1
    assert snap["counters"]["aimd.increase"] == 2
    assert snap["gauges"]["aimd.permits"] == 4.0
    ctl.on_signal(0.99)  # 4 → 2
    assert telemetry.snapshot()["gauges"]["aimd.permits"] == 2.0


def test_saturation_does_not_count_fake_increase() -> None:
    telemetry.reset()
    ctl = AIMDController(
        2, calm_round=1, baseline_s=1.0, clock_fn=SteppingClock()
    )
    ctl.on_task_done(1.0)  # 平稳,但已饱和在 cap → 不虚计
    assert telemetry.snapshot()["counters"].get("aimd.increase", 0) == 0
    assert ctl.state()["increases"] == 0


# ---------------------------------------------------------------------------
# 内核自检(确定性脚本,四键口径与 sched_kernel 一致)
# ---------------------------------------------------------------------------
def test_kernel_selfcheck_deterministic_script() -> None:
    result = kernel_selfcheck()
    assert result["name"] == "aimd"
    assert result["metric"] == "regrow_to_cap_permits"
    assert result["value"] == result["baseline"] == 8
    assert result["halving_seq"] == result["halving_expected"] == [4, 2, 2, 2]
    assert result["regrow_seq"] == result["regrow_expected"]
    assert result["regrow_seq"][-1] == 8  # 恰饱和在 cap
    assert result["latency_spike_permits"] == 4  # EWMA 尖峰单独减半 8→4
    assert result["op_counts_invariant"] is True


def test_kernel_selfcheck_replay_deterministic() -> None:
    assert kernel_selfcheck() == kernel_selfcheck()
