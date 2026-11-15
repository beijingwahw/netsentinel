# -*- coding: utf-8 -*-
"""V10.35(P2-7/8/9)无人值守韧性测试:优雅退出 + 排空 + 遥测落盘。

全部离线:扫描/停顿/抖动注入 fake,信号只在主线程测试内注册-交还;
零网络、零真实 sleep、零真实记忆库。
"""
from __future__ import annotations

import json
import logging
import pathlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest

import netsentinel.ops.pool as pool
from netsentinel import telemetry
from netsentinel.contracts import Config, Verdict
from netsentinel.ops.graceful import GracefulExit, graceful_exit
from netsentinel.ops.pool import run_pool

SITE_A = "https://site-a.example.com/"
SITE_B = "https://site-b.example.com/list"
SITE_C = "https://site-c.example.com/home"
SITE_D = "https://site-d.example.com/"


# ---------------------------------------------------------------------------
# fakes(与 test_pool.py 同款,独立复制以保持两文件互不依赖)
# ---------------------------------------------------------------------------
class FakeScan:
    """记录调用的 fake 扫描(可注入延迟,模拟在飞任务)。"""

    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.calls: list[str] = []

    def __call__(self, url: str, cfg: Config) -> Any:
        self.calls.append(url)
        if self.delay_s:
            time.sleep(self.delay_s)
        return SimpleNamespace(
            site_url=url, verdict=Verdict.CLEAN, agg_nsw_prob=0.02, needs_review=False
        )


class FakeSleep:
    """记录式停顿(不真睡)。"""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(float(seconds))


class FakeMemory:
    """A33 SiteMemory 契约 fake(首见即记,永不跳过)。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def fingerprint(self, report: Any) -> str:
        return "fp:" + str(getattr(report, "site_url", ""))

    def last_fingerprint(self, url: str) -> str | None:
        return self.store.get(url)

    def remember(self, url: str, fingerprint: str) -> None:
        self.store[url] = fingerprint


def make_cfg(tmp_path: pathlib.Path, **kw: Any) -> Config:
    """落盘路径全部收进 tmp_path 的 Config。"""
    data = tmp_path / "data"
    return Config(
        watchlist_path=str(tmp_path / "watchlist.yaml"),
        data_dir=str(data),
        db_path=str(data / "review_queue.db"),
        audit_path=str(data / "audit.jsonl"),
        log_path=str(data / "logs" / "netsentinel.log"),
        **kw,
    )


def no_jitter() -> float:
    return 0.0


@pytest.fixture
def single_thread_executor() -> Any:
    ex = ThreadPoolExecutor(max_workers=1)
    yield ex
    ex.shutdown(wait=True)


@pytest.fixture(autouse=True)
def _reset_telemetry() -> Any:
    telemetry.reset()
    yield
    telemetry.reset()


# ---------------------------------------------------------------------------
# GracefulExit 单元
# ---------------------------------------------------------------------------
def test_graceful_initial_state_not_requested() -> None:
    ex = GracefulExit()
    assert ex.requested is False
    assert ex.should_stop() is False
    assert ex.installed is False


def test_graceful_first_request_sets_flag_and_telemetry() -> None:
    ex = GracefulExit()
    ex.request("test")
    assert ex.requested is True
    assert ex.should_stop() is True
    assert telemetry.snapshot()["counters"].get("graceful.signal") == 1.0
    assert telemetry.snapshot()["counters"].get("graceful.requested") == 1.0


def test_graceful_second_request_raises_keyboard_interrupt() -> None:
    ex = GracefulExit()
    ex.request("first")
    with pytest.raises(KeyboardInterrupt):
        ex.request("second")
    # 两次信号都计数;requested 保持置位
    assert telemetry.snapshot()["counters"].get("graceful.signal") == 2.0
    assert ex.requested is True


def test_graceful_context_manager_installs_and_restores_in_main_thread() -> None:
    import signal as _signal

    before_int = _signal.getsignal(_signal.SIGINT)
    with graceful_exit() as ex:
        assert ex.installed is True
        assert _signal.getsignal(_signal.SIGINT) is not before_int
    assert _signal.getsignal(_signal.SIGINT) is before_int


def test_graceful_context_no_signal_install_opt_out() -> None:
    with graceful_exit(install_signals=False) as ex:
        assert ex.installed is False
        ex.request("internal")
        assert ex.should_stop() is True


def test_graceful_non_main_thread_install_degrades_to_flag_only() -> None:
    result: dict[str, Any] = {}

    def _worker() -> None:
        ex = GracefulExit()
        ex.install()
        result["installed"] = ex.installed
        ex.request("thread")
        result["requested"] = ex.requested
        ex.restore()

    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    assert result["installed"] is False  # 非主线程:注册降级
    assert result["requested"] is True   # 标志位照常可用


def test_graceful_signal_handler_wired_end_to_end() -> None:
    """主线程直接调处理器等价路径:request → stop;再 request → 硬中断。"""
    ex = GracefulExit().install()
    try:
        ex._handle_signal(2, None)  # SIGINT 编号
        assert ex.should_stop() is True
        with pytest.raises(KeyboardInterrupt):
            ex._handle_signal(2, None)
    finally:
        ex.restore()


# ---------------------------------------------------------------------------
# run_pool stop_check 排空
# ---------------------------------------------------------------------------
def test_run_pool_without_stop_check_summary_shape_unchanged(
    tmp_path, single_thread_executor
) -> None:
    cfg = make_cfg(tmp_path)
    scan, memory, sleeper = FakeScan(), FakeMemory(), FakeSleep()
    summary = run_pool(
        cfg, [SITE_A], run_scan=scan, memory=memory,
        executor=single_thread_executor, sleep=sleeper, jitter=no_jitter,
    )
    # 缺省(不注入 stop_check):摘要形状与旧版逐字节一致
    assert "interrupted" not in summary
    assert "not_submitted" not in summary
    assert summary["done"] == 1


def test_run_pool_stop_before_any_submit_skips_all(
    tmp_path, single_thread_executor
) -> None:
    cfg = make_cfg(tmp_path)
    scan, memory, sleeper = FakeScan(), FakeMemory(), FakeSleep()
    summary = run_pool(
        cfg, [SITE_A, SITE_B, SITE_C], run_scan=scan, memory=memory,
        executor=single_thread_executor, sleep=sleeper, jitter=no_jitter,
        stop_check=lambda: True,  # 开局即停
    )
    assert scan.calls == []                       # 零提交零扫描
    assert summary["interrupted"] is True
    assert summary["not_submitted"] == [SITE_A, SITE_B, SITE_C]
    # 恒等式扩式:done + failed + skipped + len(not_submitted) == total
    assert summary["done"] + summary["failed"] + summary["skipped"] + len(
        summary["not_submitted"]
    ) == summary["total"]


class StopAfterFirstPause(FakeSleep):
    """首个提交后停顿时刻置停机标志(模拟操作者在首站在飞时按下 Ctrl-C)。"""

    def __init__(self) -> None:
        super().__init__()
        self.stopped = False

    def __call__(self, seconds: float) -> None:
        super().__call__(seconds)
        self.stopped = True


def test_run_pool_stop_after_first_submit_drains_inflight(
    tmp_path, single_thread_executor
) -> None:
    """首个站提交后置停:在飞站**必须排空完成**(不弃单),其余不提交。"""
    cfg = make_cfg(tmp_path)
    scan, memory = FakeScan(), FakeMemory()
    sleeper = StopAfterFirstPause()
    summary = run_pool(
        cfg, [SITE_A, SITE_B, SITE_C], run_scan=scan, memory=memory,
        executor=single_thread_executor, sleep=sleeper, jitter=no_jitter,
        stop_check=lambda: sleeper.stopped,
    )
    assert scan.calls == [SITE_A]                 # 在飞站排空,后两站不提交
    assert summary["done"] == 1                   # 排空的站正常计 done
    assert summary["interrupted"] is True
    assert summary["not_submitted"] == [SITE_B, SITE_C]
    assert summary["done"] + summary["failed"] + summary["skipped"] + len(
        summary["not_submitted"]
    ) == summary["total"]
    # 停机计数入遥测(2 = 未提交数)
    assert telemetry.snapshot()["counters"].get("pool.graceful.not_submitted") == 2.0


def test_run_pool_stop_with_form_rejected_target_keeps_identity(
    tmp_path, single_thread_executor
) -> None:
    """形态不合法目标计入 failed(已"处理"),不进 not_submitted。"""
    cfg = make_cfg(tmp_path)
    scan, memory = FakeScan(), FakeMemory()
    sleeper = StopAfterFirstPause()
    summary = run_pool(
        cfg, ["not-a-url", SITE_A, SITE_B], run_scan=scan, memory=memory,
        executor=single_thread_executor, sleep=sleeper, jitter=no_jitter,
        stop_check=lambda: sleeper.stopped,
    )
    # not-a-url 在 stop 前已被拒(提交循环顺序处理),failed=1;
    # SITE_A 提交并排空;SITE_B 停机不提交
    assert summary["failed"] == 1
    assert summary["done"] == 1
    assert summary["not_submitted"] == [SITE_B]
    assert summary["done"] + summary["failed"] + summary["skipped"] + len(
        summary["not_submitted"]
    ) == summary["total"]


def test_run_pool_graceful_exit_object_end_to_end(
    tmp_path, single_thread_executor
) -> None:
    """graceful_exit 协调器 + run_pool stop_check 全链(不注册真信号)。"""
    cfg = make_cfg(tmp_path)
    scan, memory, sleeper = FakeScan(), FakeMemory(), FakeSleep()
    with graceful_exit(install_signals=False) as ex:
        assert ex.should_stop() is False
        summary_a = run_pool(
            cfg, [SITE_A], run_scan=scan, memory=memory,
            executor=single_thread_executor, sleep=sleeper, jitter=no_jitter,
            stop_check=ex.should_stop,
        )
        assert "interrupted" not in summary_a   # 未请求:正常跑完

        ex.request("operator ctrl-c")
        summary_b = run_pool(
            cfg, [SITE_B, SITE_C], run_scan=scan, memory=memory,
            executor=single_thread_executor, sleep=sleeper, jitter=no_jitter,
            stop_check=ex.should_stop,
        )
        assert summary_b["interrupted"] is True
        assert summary_b["not_submitted"] == [SITE_B, SITE_C]
    # 协调器退出后标志位保留(再次进入批量的调用方自行新建协调器)
    assert ex.requested is True


# ---------------------------------------------------------------------------
# 遥测批末落盘(P2-7b)
# ---------------------------------------------------------------------------
def test_run_pool_telemetry_export_appends_jsonl(tmp_path) -> None:
    """cfg.telemetry_jsonl_path 配置时,批末追加一行快照 JSON。"""
    out = tmp_path / "tel" / "telemetry.jsonl"
    cfg = make_cfg(tmp_path, telemetry_jsonl_path=str(out))
    scan, memory, sleeper = FakeScan(), FakeMemory(), FakeSleep()
    with ThreadPoolExecutor(max_workers=1) as ex:
        run_pool(
            cfg, [SITE_A], run_scan=scan, memory=memory,
            executor=ex, sleep=sleeper, jitter=no_jitter,
        )
    assert out.exists()
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    snap = json.loads(lines[0])
    assert "counters" in snap and "gauges" in snap and "timers" in snap
    assert snap["counters"].get("pool.done") == 1.0
    # telemetry.export 计数在快照写盘**之后**累加 → 落在下一批的快照里
    # 每批一行:再跑一批追加第二行(第二行携带上一批的 export 计数)
    with ThreadPoolExecutor(max_workers=1) as ex:
        run_pool(
            cfg, [SITE_B], run_scan=scan, memory=memory,
            executor=ex, sleep=sleeper, jitter=no_jitter,
        )
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["counters"].get("telemetry.export") == 1.0


def test_run_pool_telemetry_export_off_by_default(tmp_path) -> None:
    """未配置路径:零落盘、零 export 计数(与旧行为逐字节一致)。"""
    cfg = make_cfg(tmp_path)  # telemetry_jsonl_path 缺省 ""
    scan, memory, sleeper = FakeScan(), FakeMemory(), FakeSleep()
    with ThreadPoolExecutor(max_workers=1) as ex:
        run_pool(
            cfg, [SITE_A], run_scan=scan, memory=memory,
            executor=ex, sleep=sleeper, jitter=no_jitter,
        )
    assert telemetry.snapshot()["counters"].get("telemetry.export") is None


def test_run_pool_telemetry_export_failure_does_not_break_batch(
    tmp_path, caplog
) -> None:
    """落盘失败只告警 + failed 计数,摘要照常返回。"""
    bad = tmp_path / "tel.jsonl"
    # 用目录占位文件路径制造 open 失败(路径是个目录)
    bad.mkdir()
    cfg = make_cfg(tmp_path, telemetry_jsonl_path=str(bad))
    scan, memory, sleeper = FakeScan(), FakeMemory(), FakeSleep()
    with ThreadPoolExecutor(max_workers=1) as ex:
        summary = run_pool(
            cfg, [SITE_A], run_scan=scan, memory=memory,
            executor=ex, sleep=sleeper, jitter=no_jitter,
        )
    assert summary["done"] == 1  # 批次结果不受观测失败影响
    assert telemetry.snapshot()["counters"].get("telemetry.export.failed") == 1.0
