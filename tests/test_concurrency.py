"""A164:netsentinel.ops.concurrency 单元测试(全离线 + 一例真进程)。

覆盖(契约 §2 A164 行 + §1 三档公式 + 红线 37):

- 三档 workers 参数化(monkeypatch 核数探测 = 8:low→2 / mid→4 / high→7,
  reserve=0 全核、单核退化恒 ≥1、cpu_count 为 None 回退 2);
- ``cpu_workers == min(io_workers, cores)``:超核档位被钳回物理核(红线 37);
- ``workers_for`` 分派(io / cpu / 非法 kind);
- ``thread_pool`` 上下文:4 任务全果、线程名前缀 label(缺省 "ns")、
  退出(含 with 体抛异常)必 shutdown、退出后 submit 拒绝;
- ``process_pool``:构造失败(mp 不可用环境)→ 中文 RuntimeError 守卫提示
  (原异常保留为 __cause__,不自动吞);max_workers/mp_context/关闭接线;
  可用环境(显式注入 spawn mp_context)真进程跑 2 任务加法;
- 非法档 → 中文 ValueError(各入口一致透传);
- 红线 37 专项:全档 × reserve 组合 workers ≤ cores。

并行态确定性:兄弟模块 A163 ``netsentinel.ops.cpu_profile``(并行开发中)
经 autouse 夹具强制**缺席**(sys.modules 置 None),走 §1 内置公式兜底;
委托路径用注入 fake cpu_profile 的用例覆盖——两种并行态下结论一致。
"""
from __future__ import annotations

import multiprocessing as mp
import os
import sys
import threading
import types
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import pytest

import netsentinel.ops
from netsentinel.contracts import Config
from netsentinel.ops import concurrency

#: 固定核数(autouse 夹具注入;三档期望:low 2 / mid 4 / high 7)
CORES = 8


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """每用例隔离:A163 cpu_profile 强制缺席 + 核数探测固定为 8。

    缺席强制需双管齐下:``sys.modules`` 置 None **且** ``netsentinel.ops``
    包属性置 None——真实 cpu_profile 若已被同仓其他用例导入,包属性会
    在 ``from netsentinel.ops import cpu_profile`` 的 getattr 环节胜出,
    仅改 sys.modules 不够(全仓回归实测)。
    """
    monkeypatch.setitem(sys.modules, "netsentinel.ops.cpu_profile", None)
    monkeypatch.setattr(netsentinel.ops, "cpu_profile", None, raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: CORES)


def make_cfg(tier: str = "mid", reserve: int = 1) -> Config:
    cfg = Config()
    cfg.concurrency_tier = tier
    cfg.cpu_reserve = reserve
    return cfg


def install_fake_cpu_profile(
    monkeypatch: pytest.MonkeyPatch,
    *,
    detect_cores: int = CORES,
    tier_workers: object | None = None,
) -> list[tuple[str, int, int]]:
    """注入 fake A163 cpu_profile(模块级 detect + tier_workers),记录调用。"""
    calls: list[tuple[str, int, int]] = []
    module = types.ModuleType("netsentinel.ops.cpu_profile")
    module.detect = lambda: {  # type: ignore[attr-defined]
        "cores": detect_cores, "arch": "x86_64", "platform": "win32", "psutil": False,
    }

    def fake_tier_workers(tier: str, *, reserve: int = 1, cores: int | None = None) -> int:
        calls.append((tier, reserve, int(cores or 0)))
        if callable(tier_workers):
            return int(tier_workers(tier, reserve=reserve, cores=cores))  # type: ignore[misc]
        return concurrency._builtin_tier_workers(tier, reserve=reserve, cores=cores)

    module.tier_workers = fake_tier_workers  # type: ignore[attr-defined]
    # 双管齐下注入:sys.modules + ops 包属性(理由见 isolated 夹具文档)
    monkeypatch.setitem(sys.modules, "netsentinel.ops.cpu_profile", module)
    monkeypatch.setattr(netsentinel.ops, "cpu_profile", module, raising=False)
    return calls


def _proc_add(a: int, b: int) -> tuple[int, int]:
    """真进程池任务:模块级纯函数(可按引用 pickle,红线 35 形态)。

    返回 ``(a+b, 子进程 pid)``——pid 供测试证明确在子进程执行。
    """
    return int(a) + int(b), os.getpid()


# ---------------------------------------------------------------------------
# io_workers:三档参数化(§1 公式,monkeypatch detect cores=8)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "tier,expected",
    [("low", 2), ("mid", 4), ("high", 7)],
)
def test_io_workers_three_tiers_cores8(tier: str, expected: int) -> None:
    """cores=8、reserve=1:low=8//4=2、mid=8//2=4、high=8-1=7。"""
    assert concurrency.io_workers(make_cfg(tier=tier, reserve=1)) == expected


def test_io_workers_high_reserve_zero_full_cores() -> None:
    """reserve=0 即全核压榨:high → 8(§1)。"""
    assert concurrency.io_workers(make_cfg(tier="high", reserve=0)) == 8


def test_io_workers_cpu_count_none_fallback_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """os.cpu_count() 返回 None → 核数回退 2(§1):mid=1、high=1。"""
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert concurrency.io_workers(make_cfg(tier="mid")) == 1  # max(1, 2//2)
    assert concurrency.io_workers(make_cfg(tier="high", reserve=1)) == 1  # max(1, 2-1)


def test_io_workers_min_one_on_single_core(monkeypatch: pytest.MonkeyPatch) -> None:
    """单核退化:三档都恒 ≥1(§1 的 max(1, ...) 下限)。"""
    monkeypatch.setattr(os, "cpu_count", lambda: 1)
    for tier in concurrency.TIERS:
        assert concurrency.io_workers(make_cfg(tier=tier)) == 1


# ---------------------------------------------------------------------------
# A163 委托与兜底(惰性双态)
# ---------------------------------------------------------------------------
def test_io_workers_delegates_to_cpu_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """A163 就位时:tier/reserve/cores 透传给 tier_workers(cores 统一注入)。"""
    calls = install_fake_cpu_profile(monkeypatch, detect_cores=16)
    assert concurrency.io_workers(make_cfg(tier="high", reserve=2)) == 14  # 16-2
    assert calls == [("high", 2, 16)]


def test_cores_prefers_cpu_profile_detect(monkeypatch: pytest.MonkeyPatch) -> None:
    """核数探测优先 A163 detect():detect=16 胜过 os.cpu_count=8。"""
    install_fake_cpu_profile(monkeypatch, detect_cores=16)
    assert concurrency._cores() == 16
    assert concurrency.io_workers(make_cfg(tier="mid")) == 8  # 16//2,而非 8//2


def test_io_workers_falls_back_when_profile_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A163 tier_workers 抛异常 → §1 内置公式兜底(不放大失败)。"""
    install_fake_cpu_profile(monkeypatch, tier_workers=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert concurrency.io_workers(make_cfg(tier="mid", reserve=1)) == 4


def test_io_workers_absent_profile_uses_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A163 缺席(autouse 已强制)→ 内置 §1 公式,三档与 §1 逐条一致。"""
    assert concurrency.io_workers(make_cfg(tier="low", reserve=1)) == 2
    assert concurrency.io_workers(make_cfg(tier="mid", reserve=1)) == 4
    assert concurrency.io_workers(make_cfg(tier="high", reserve=3)) == 5


# ---------------------------------------------------------------------------
# cpu_workers:min 钳制(红线 37)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "tier,io_expected",
    [("low", 2), ("mid", 4), ("high", 7)],
)
def test_cpu_workers_equals_io_when_below_cores(tier: str, io_expected: int) -> None:
    """cores=8 下三档 io 均 < cores → cpu_workers == io_workers。"""
    cfg = make_cfg(tier=tier, reserve=1)
    assert concurrency.cpu_workers(cfg) == io_expected == concurrency.io_workers(cfg)


def test_cpu_workers_clamped_to_cores_redline37(monkeypatch: pytest.MonkeyPatch) -> None:
    """档位公式超核(注入 fake 返回 99)→ cpu_workers=min(99, 8)=8。"""
    install_fake_cpu_profile(monkeypatch, tier_workers=lambda *a, **k: 99)
    cfg = make_cfg(tier="high")
    assert concurrency.io_workers(cfg) == 99
    assert concurrency.cpu_workers(cfg) == 8  # 红线 37:进程池永不超过物理核


def test_redline37_all_tiers_workers_never_exceed_cores() -> None:
    """红线 37 专项:全档 × reserve 组合,io/cpu workers 恒 ≤ cores。"""
    for tier in concurrency.TIERS:
        for reserve in (0, 1, 3):
            cfg = make_cfg(tier=tier, reserve=reserve)
            assert concurrency.io_workers(cfg) <= CORES
            assert concurrency.cpu_workers(cfg) <= CORES
    # reserve=0 的 high 恰好压满全核(边界等于,不超)
    assert concurrency.cpu_workers(make_cfg(tier="high", reserve=0)) == CORES


# ---------------------------------------------------------------------------
# workers_for 分派
# ---------------------------------------------------------------------------
def test_workers_for_dispatch() -> None:
    """默认与显式 kind='io' 同 io_workers;'cpu' 同 cpu_workers。"""
    cfg = make_cfg(tier="high", reserve=1)
    assert concurrency.workers_for(cfg) == 7
    assert concurrency.workers_for(cfg, kind="io") == 7
    assert concurrency.workers_for(cfg, kind="cpu") == 7


def test_workers_for_io_cpu_divergence(monkeypatch: pytest.MonkeyPatch) -> None:
    """超核档位下 io 与 cpu 分化:io=99、cpu 被钳回 cores=8。"""
    install_fake_cpu_profile(monkeypatch, tier_workers=lambda *a, **k: 99)
    cfg = make_cfg(tier="mid")
    assert concurrency.workers_for(cfg, "io") == 99
    assert concurrency.workers_for(cfg, "cpu") == 8


def test_workers_for_invalid_kind() -> None:
    """非法 kind → 中文 ValueError。"""
    with pytest.raises(ValueError, match="'io'"):
        concurrency.workers_for(make_cfg(), kind="gpu")


# ---------------------------------------------------------------------------
# 非法档位:各入口一致中文 ValueError
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["turbo", "", "MID", "高", None])
def test_invalid_tier_value_error_chinese(bad: object) -> None:
    """非法档位 → ValueError,报错含三档取值说明。"""
    with pytest.raises(ValueError, match="low / mid / high"):
        concurrency.io_workers(make_cfg(tier=bad))  # type: ignore[arg-type]


def test_invalid_tier_propagates_through_all_entries() -> None:
    """非法档经 cpu_workers / workers_for / 两池工厂一致透传 ValueError。"""
    cfg = make_cfg(tier="ultra")
    with pytest.raises(ValueError, match="low / mid / high"):
        concurrency.cpu_workers(cfg)
    with pytest.raises(ValueError, match="low / mid / high"):
        concurrency.workers_for(cfg, kind="io")
    with pytest.raises(ValueError, match="low / mid / high"):
        with concurrency.thread_pool(cfg):
            pass
    with pytest.raises(ValueError, match="low / mid / high"):
        with concurrency.process_pool(cfg):
            pass


# ---------------------------------------------------------------------------
# thread_pool:上下文 / 4 任务全果 / label 前缀 / 退出必关闭
# ---------------------------------------------------------------------------
def test_thread_pool_submits_four_tasks_all_results() -> None:
    """mid 档(cores=8 → workers=4)提交 4 任务全部完成且结果正确。"""
    with concurrency.thread_pool(make_cfg(tier="mid")) as pool:
        assert isinstance(pool, ThreadPoolExecutor)
        assert pool._max_workers == 4
        futures = [pool.submit(lambda i=i: i * i, i) for i in range(4)]
        assert sorted(f.result(timeout=10) for f in futures) == [0, 1, 4, 9]


def test_thread_pool_shuts_down_on_exit() -> None:
    """退出后必关闭:submit 拒绝(RuntimeError)、_shutdown=True。"""
    with concurrency.thread_pool(make_cfg()) as pool:
        assert pool._shutdown is False
    assert pool._shutdown is True
    with pytest.raises(RuntimeError, match="shutdown"):
        pool.submit(lambda: None)


def test_thread_pool_closes_even_when_body_raises() -> None:
    """with 体抛异常同样关闭(finally 兜底,不留孤儿线程,红线 37)。"""
    pool_ref: ThreadPoolExecutor | None = None
    with pytest.raises(ZeroDivisionError):
        with concurrency.thread_pool(make_cfg()) as pool:
            pool_ref = pool
            raise ZeroDivisionError("boom")
    assert pool_ref is not None and pool_ref._shutdown is True


def test_thread_pool_name_prefix_custom_label() -> None:
    """label 透传 thread_name_prefix:工作线程名以 "scan_" 开头。"""
    with concurrency.thread_pool(make_cfg(), label="scan") as pool:
        name = pool.submit(lambda: threading.current_thread().name).result(timeout=10)
    assert name.startswith("scan_")


def test_thread_pool_name_prefix_default_ns() -> None:
    """缺省 label="ns":工作线程名以 "ns_" 开头。"""
    with concurrency.thread_pool(make_cfg()) as pool:
        name = pool.submit(lambda: threading.current_thread().name).result(timeout=10)
    assert name.startswith("ns_")


def test_thread_pool_scale_follows_tier() -> None:
    """线程池规模跟随档位:low/mid/high → 2/4/7(cores=8)。"""
    for tier, expected in (("low", 2), ("mid", 4), ("high", 7)):
        with concurrency.thread_pool(make_cfg(tier=tier)) as pool:
            assert pool._max_workers == expected


# ---------------------------------------------------------------------------
# process_pool:守卫提示 / 接线 / 真进程
# ---------------------------------------------------------------------------
def test_process_pool_construction_failure_chinese_runtimeerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mp 不可用环境(构造抛错)→ 中文 RuntimeError 守卫提示,不自动吞。"""

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("子进程反复导入 __main__ 失败")

    monkeypatch.setattr(concurrency, "ProcessPoolExecutor", boom)
    with pytest.raises(RuntimeError, match="if __name__") as excinfo:
        with concurrency.process_pool(make_cfg(), label="cls"):
            pass
    assert "mp_context" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, OSError)  # 原异常保留(不吞)


def test_process_pool_wires_max_workers_and_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """接线:max_workers=cpu_workers、mp_context 透传、退出必 shutdown。"""
    captured: dict[str, object] = {}

    class FakeProcessPool:
        def __init__(
            self, max_workers: int | None = None, mp_context: object = None, **kw: object
        ) -> None:
            captured["max_workers"] = max_workers
            captured["mp_context"] = mp_context
            self._shutdown = False

        def shutdown(self, wait: bool = True) -> None:
            self._shutdown = True

        def submit(self, fn: object, *a: object) -> object:
            class _F:
                def result(self, timeout: object = None) -> object:
                    return fn(*a)  # type: ignore[misc]

            return _F()

    monkeypatch.setattr(concurrency, "ProcessPoolExecutor", FakeProcessPool)
    ctx = mp.get_context("spawn")
    cfg = make_cfg(tier="mid", reserve=1)
    with concurrency.process_pool(cfg, label="cls", mp_context=ctx) as pool:
        assert pool.submit(lambda: 41 + 1).result() == 42
    assert captured["max_workers"] == concurrency.cpu_workers(cfg) == 4
    assert captured["mp_context"] is ctx
    assert pool._shutdown is True


def test_process_pool_real_two_tasks_spawn() -> None:
    """可用环境(显式注入 spawn mp_context):2 任务真进程加法。

    Windows spawn 兼容形态——测试经 mp_context 显式注入;任务为模块级
    纯函数(红线 35);pid 断言证明确在子进程执行。
    """
    ctx = mp.get_context("spawn")
    cfg = make_cfg(tier="mid", reserve=1)  # cores=8 → cpu_workers=4
    parent_pid = os.getpid()
    with concurrency.process_pool(cfg, label="ns-test", mp_context=ctx) as pool:
        assert isinstance(pool, ProcessPoolExecutor)
        assert pool._max_workers == 4
        s1, p1 = pool.submit(_proc_add, 2, 3).result(timeout=60)
        s2, p2 = pool.submit(_proc_add, 10, 20).result(timeout=60)
    assert (s1, s2) == (5, 30)
    assert p1 != parent_pid and p2 != parent_pid  # 真子进程,非主进程内执行
    # 退出必关闭,不留孤儿进程(红线 37):关闭后 submit 拒绝
    with pytest.raises(RuntimeError, match="shutdown"):
        pool.submit(_proc_add, 1, 1)


def test_process_pool_never_exceeds_cores(monkeypatch: pytest.MonkeyPatch) -> None:
    """红线 37:超核档位下进程池规模被钳到 cores=8(接线层生效)。"""
    install_fake_cpu_profile(monkeypatch, tier_workers=lambda *a, **k: 99)
    captured: dict[str, object] = {}

    class FakeProcessPool:
        def __init__(self, max_workers: int | None = None, **kw: object) -> None:
            captured["max_workers"] = max_workers
            self._shutdown = False

        def shutdown(self, wait: bool = True) -> None:
            self._shutdown = True

    monkeypatch.setattr(concurrency, "ProcessPoolExecutor", FakeProcessPool)
    with concurrency.process_pool(make_cfg(tier="high")):
        pass
    assert captured["max_workers"] == 8
