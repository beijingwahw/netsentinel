"""A58 并发扫描池(netsentinel.ops.pool)单元测试。

全部离线:run_scan / memory / sleep / jitter / executor 一律注入 fake,
通知经 monkeypatch 替换模块级 ``_default_notify``;零网络、零真实 sleep、
零真实记忆库(不触发缺省 SiteMemory 落盘)。
"""
from __future__ import annotations

import logging
import pathlib
import threading
import time
from concurrent.futures import Executor, Future, ProcessPoolExecutor, ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, Callable

import pytest

import netsentinel.ops.pool as pool
from netsentinel import telemetry
from netsentinel.contracts import Config, Verdict
from netsentinel.ops.aimd import AIMDController
from netsentinel.ops.pool import run_pool

SITE_A = "https://site-a.example.com/"
SITE_B = "https://site-b.example.com/list"
SITE_C = "https://site-c.example.com/home"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class FakeScan:
    """路由式 fake 扫描:按 URL 返回预设报告或抛错;记录调用与并发峰值。"""

    def __init__(
        self,
        reports: dict[str, Any] | None = None,
        errors: tuple[str, ...] = (),
        delay_s: float = 0.0,
    ) -> None:
        self.reports = dict(reports or {})
        self.errors = set(errors)
        self.delay_s = delay_s
        self.calls: list[str] = []
        self.max_concurrency = 0
        self._lock = threading.Lock()
        self._live = 0

    def __call__(self, url: str, cfg: Config) -> Any:
        with self._lock:
            self.calls.append(url)
            self._live += 1
            self.max_concurrency = max(self.max_concurrency, self._live)
        try:
            if self.delay_s:
                time.sleep(self.delay_s)
            if url in self.errors:
                raise RuntimeError(f"模拟扫描失败:{url}")
            report = self.reports.get(url)
            if report is None:
                raise AssertionError(f"fake run_scan 未预设该 URL 的报告:{url}")
            return report
        finally:
            with self._lock:
                self._live -= 1


class FakeMemory:
    """A33 SiteMemory 契约 fake:记住每站点指纹,供第二轮跳过判断。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.remembered: list[tuple[str, str]] = []
        self.fingerprint_calls: list[str] = []
        self.last_calls: list[str] = []

    def fingerprint(self, report: Any) -> str:
        self.fingerprint_calls.append(str(getattr(report, "site_url", "")))
        return "fp:" + str(getattr(report, "site_url", ""))

    def remember(self, site_url: str, fp: str) -> None:
        self.store[site_url] = fp
        self.remembered.append((site_url, fp))

    def last_fingerprint(self, site_url: str) -> str:
        self.last_calls.append(site_url)
        return self.store.get(site_url, "")


class FakeSleep:
    """记录每次停顿时长,绝不真实等待。"""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(float(seconds))


def make_report(
    url: str,
    *,
    verdict: Verdict = Verdict.NSFW,
    agg: float = 0.97,
    needs_review: bool = True,
) -> SimpleNamespace:
    """鸭子类型的 SiteReport fake(池只读 site_url/verdict/agg/needs_review)。"""
    return SimpleNamespace(
        site_url=url, verdict=verdict, agg_nsw_prob=agg, needs_review=needs_review
    )


def make_cfg(tmp_path: pathlib.Path) -> Config:
    """把所有落盘路径都收进 tmp_path 的 Config(本测试不应触发任何落盘)。"""
    data = tmp_path / "data"
    return Config(
        watchlist_path=str(tmp_path / "watchlist.yaml"),
        data_dir=str(data),
        db_path=str(data / "review_queue.db"),
        audit_path=str(data / "audit.jsonl"),
        log_path=str(data / "logs" / "netsentinel.log"),
    )


@pytest.fixture
def single_thread_executor() -> Any:
    """注入的单线程执行器:验证 max_workers=1 时功能无并发错误。"""
    ex = ThreadPoolExecutor(max_workers=1)
    yield ex
    ex.shutdown(wait=True)


def fixed_jitter() -> float:
    """固定抖动源:停顿时长恒为 1.0 + 0.5*0.4 = 1.2s(仅记录,不真睡)。"""
    return 0.4


def recorder_notify(ok: bool = True) -> tuple[list[tuple[str, str]], Callable[..., bool]]:
    """构造记录式通知 fake(经 monkeypatch 挂到 pool._default_notify)。"""
    calls: list[tuple[str, str]] = []

    def _notify(cfg: Any, event: str, text: str) -> bool:
        calls.append((event, text))
        return ok

    return calls, _notify


# ---------------------------------------------------------------------------
# 全成功 / 失败隔离
# ---------------------------------------------------------------------------
def test_all_success_results_complete(tmp_path, single_thread_executor):
    cfg = make_cfg(tmp_path)
    scan = FakeScan(
        reports={
            SITE_A: make_report(SITE_A, verdict=Verdict.NSFW, needs_review=True),
            SITE_B: make_report(SITE_B, verdict=Verdict.CLEAN, agg=0.05, needs_review=False),
            SITE_C: make_report(SITE_C, verdict=Verdict.SUSPECT, agg=0.66, needs_review=True),
        }
    )
    memory, sleeper = FakeMemory(), FakeSleep()
    summary = run_pool(
        cfg,
        [SITE_A, SITE_B, SITE_C],
        run_scan=scan,
        memory=memory,
        executor=single_thread_executor,
        sleep=sleeper,
        jitter=fixed_jitter,
    )
    assert summary["total"] == 3
    assert summary["done"] == 3
    assert summary["failed"] == 0
    assert summary["skipped"] == 0
    assert summary["errors"] == []
    assert summary["results"] == {
        SITE_A: "nsfw", SITE_B: "clean", SITE_C: "suspect"
    }
    # 恒等式:done + failed + skipped == total
    assert summary["done"] + summary["failed"] + summary["skipped"] == summary["total"]
    # 先扫后记:三个站点都已记住指纹
    assert sorted(url for url, _ in memory.remembered) == [SITE_A, SITE_B, SITE_C]


def test_single_failure_isolated_rest_complete(tmp_path, single_thread_executor):
    cfg = make_cfg(tmp_path)
    scan = FakeScan(
        reports={
            SITE_A: make_report(SITE_A),
            SITE_C: make_report(SITE_C, verdict=Verdict.CLEAN, needs_review=False),
        },
        errors=(SITE_B,),
    )
    memory = FakeMemory()
    summary = run_pool(
        cfg,
        [SITE_A, SITE_B, SITE_C],
        run_scan=scan,
        memory=memory,
        executor=single_thread_executor,
        sleep=FakeSleep(),
        jitter=fixed_jitter,
    )
    assert summary["total"] == 3
    assert summary["done"] == 2
    assert summary["failed"] == 1
    assert summary["skipped"] == 0
    assert summary["results"] == {SITE_A: "nsfw", SITE_C: "clean"}
    assert len(summary["errors"]) == 1
    failed_url, err_text = summary["errors"][0]
    assert failed_url == SITE_B
    assert "模拟扫描失败" in err_text
    # 失败站点未写记忆,成功站点照常记忆
    assert sorted(url for url, _ in memory.remembered) == [SITE_A, SITE_C]


# ---------------------------------------------------------------------------
# skipped 语义(先扫后记:同指纹第二轮 → skipped,不 notify)
# ---------------------------------------------------------------------------
def test_skipped_on_same_fingerprint_second_round(
    tmp_path, single_thread_executor, monkeypatch
):
    cfg = make_cfg(tmp_path)
    reports = {
        SITE_A: make_report(SITE_A, verdict=Verdict.NSFW, needs_review=True),
        SITE_B: make_report(SITE_B, verdict=Verdict.NSFW, needs_review=True),
    }
    scan = FakeScan(reports=reports)
    memory = FakeMemory()
    notify_calls, notify_fn = recorder_notify()
    monkeypatch.setattr(pool, "_default_notify", notify_fn)

    first = run_pool(
        cfg, [SITE_A, SITE_B], run_scan=scan, memory=memory,
        executor=single_thread_executor, sleep=FakeSleep(), jitter=fixed_jitter,
    )
    assert first["done"] == 2 and first["skipped"] == 0
    assert len(notify_calls) == 2  # 首轮 needs_review 均已提醒

    notify_calls.clear()
    second = run_pool(
        cfg, [SITE_A, SITE_B], run_scan=scan, memory=memory,
        executor=single_thread_executor, sleep=FakeSleep(), jitter=fixed_jitter,
    )
    # 第二轮:fake memory 记忆的指纹与本轮新算一致 → skipped,不 notify、不进 results
    assert second["skipped"] == 2
    assert second["done"] == 0
    assert second["results"] == {}
    assert second["failed"] == 0
    assert notify_calls == []
    # 扫描本身仍发生(先扫后记:省的是判定与通知,不是抓取)
    assert len(scan.calls) == 4


# ---------------------------------------------------------------------------
# 全局礼貌间隔:提交侧串行,每个目标至少一次停顿
# ---------------------------------------------------------------------------
def test_polite_sleep_at_least_once_per_item(tmp_path, single_thread_executor):
    cfg = make_cfg(tmp_path)
    items = [SITE_A, SITE_B, SITE_C]
    scan = FakeScan(reports={u: make_report(u) for u in items})
    sleeper = FakeSleep()
    run_pool(
        cfg, items, run_scan=scan, memory=FakeMemory(),
        executor=single_thread_executor, sleep=sleeper, jitter=fixed_jitter,
    )
    assert len(sleeper.calls) >= len(items)
    for seconds in sleeper.calls:
        assert 1.0 <= seconds <= 1.5  # base 1.0 + jitter*0.5 ∈ [1.0, 1.5)
        assert seconds == pytest.approx(1.2)  # 固定抖动 0.4 → 恒 1.2


# ---------------------------------------------------------------------------
# 注入 max_workers=1 执行器:功能正确且零并发
# ---------------------------------------------------------------------------
def test_injected_single_thread_executor_functional(tmp_path, single_thread_executor):
    cfg = make_cfg(tmp_path)
    scan = FakeScan(
        reports={u: make_report(u, verdict=Verdict.CLEAN, needs_review=False)
                 for u in (SITE_A, SITE_B, SITE_C)},
        errors=(SITE_B,),
    )
    summary = run_pool(
        cfg, [SITE_A, SITE_B, SITE_C], run_scan=scan, memory=FakeMemory(),
        executor=single_thread_executor, sleep=FakeSleep(), jitter=fixed_jitter,
    )
    assert summary["done"] == 2 and summary["failed"] == 1
    assert scan.max_concurrency == 1  # 单线程执行器下确实无并发
    # 注入的 executor 归调用方所有:run_pool 不得将其关闭
    assert single_thread_executor.submit(lambda: 42).result() == 42


def test_default_executor_bounded_by_workers(tmp_path):
    """缺省线程池形态:并发峰值受 workers 有界约束。"""
    cfg = make_cfg(tmp_path)
    items = [f"https://n{i}.example.com/" for i in range(6)]
    scan = FakeScan(
        reports={u: make_report(u, verdict=Verdict.CLEAN, needs_review=False)
                 for u in items},
        delay_s=0.02,
    )
    summary = run_pool(
        cfg, items, run_scan=scan, memory=FakeMemory(),
        workers=2, sleep=FakeSleep(), jitter=fixed_jitter,
    )
    assert summary["done"] == 6
    assert 1 <= scan.max_concurrency <= 2  # 有界并发(安全红线)


# ---------------------------------------------------------------------------
# items 去重(保序)
# ---------------------------------------------------------------------------
def test_items_deduped_order_preserved(tmp_path, single_thread_executor):
    cfg = make_cfg(tmp_path)
    scan = FakeScan(
        reports={
            SITE_A: make_report(SITE_A),
            SITE_B: make_report(SITE_B, verdict=Verdict.CLEAN, needs_review=False),
        }
    )
    sleeper = FakeSleep()
    summary = run_pool(
        cfg,
        [SITE_A, "  " + SITE_A + "  ", SITE_B, SITE_A],  # 含空白变体与重复
        run_scan=scan,
        memory=FakeMemory(),
        executor=single_thread_executor,
        sleep=sleeper,
        jitter=fixed_jitter,
    )
    assert summary["total"] == 2
    assert summary["done"] == 2
    assert summary["results"] == {SITE_A: "nsfw", SITE_B: "clean"}
    assert scan.calls == [SITE_A, SITE_B]  # 去重 + 保序,只扫一次
    assert len(sleeper.calls) == 2


def test_invalid_url_rejected_without_scan(tmp_path, single_thread_executor):
    """目标准入:非 http(s) 形态不提交扫描,计入 failed(绝不扩大范围)。"""
    cfg = make_cfg(tmp_path)
    scan = FakeScan(reports={SITE_A: make_report(SITE_A)})
    summary = run_pool(
        cfg, [SITE_A, "ftp://bad.example.com/x"], run_scan=scan, memory=FakeMemory(),
        executor=single_thread_executor, sleep=FakeSleep(), jitter=fixed_jitter,
    )
    assert summary["total"] == 2
    assert summary["done"] == 1
    assert summary["failed"] == 1
    assert scan.calls == [SITE_A]
    assert "http" in summary["errors"][0][1]


# ---------------------------------------------------------------------------
# notify:needs_review 触发、CLEAN 不触发、通知异常被忽略
# ---------------------------------------------------------------------------
def test_notify_called_for_needs_review_not_for_clean(
    tmp_path, single_thread_executor, monkeypatch
):
    cfg = make_cfg(tmp_path)
    scan = FakeScan(
        reports={
            SITE_A: make_report(SITE_A, verdict=Verdict.NSFW, needs_review=True),
            SITE_B: make_report(SITE_B, verdict=Verdict.CLEAN, needs_review=False),
        }
    )
    notify_calls, notify_fn = recorder_notify()
    monkeypatch.setattr(pool, "_default_notify", notify_fn)
    run_pool(
        cfg, [SITE_A, SITE_B], run_scan=scan, memory=FakeMemory(),
        executor=single_thread_executor, sleep=FakeSleep(), jitter=fixed_jitter,
    )
    assert len(notify_calls) == 1  # 只有 needs_review 的 A 触发
    event, text = notify_calls[0]
    assert event == "pending_review"
    assert SITE_A in text and "nsfw" in text


def test_notify_failure_ignored(tmp_path, single_thread_executor, monkeypatch):
    cfg = make_cfg(tmp_path)

    def _boom(cfg: Any, event: str, text: str) -> bool:
        raise RuntimeError("通知通道炸了")

    monkeypatch.setattr(pool, "_default_notify", _boom)
    summary = run_pool(
        cfg, [SITE_A], run_scan=FakeScan(reports={SITE_A: make_report(SITE_A)}),
        memory=FakeMemory(), executor=single_thread_executor,
        sleep=FakeSleep(), jitter=fixed_jitter,
    )
    # 通知异常不影响扫描结果
    assert summary["done"] == 1 and summary["failed"] == 0
    assert summary["results"] == {SITE_A: "nsfw"}


# ---------------------------------------------------------------------------
# 空 items:summary 全 0
# ---------------------------------------------------------------------------
def test_empty_items_all_zero(tmp_path, single_thread_executor):
    cfg = make_cfg(tmp_path)
    scan, sleeper = FakeScan(), FakeSleep()
    summary = run_pool(
        cfg, [], run_scan=scan, memory=FakeMemory(),
        executor=single_thread_executor, sleep=sleeper, jitter=fixed_jitter,
    )
    assert summary == {
        "total": 0, "done": 0, "failed": 0, "skipped": 0,
        "results": {}, "errors": [],
    }
    assert scan.calls == []
    assert sleeper.calls == []


# ---------------------------------------------------------------------------
# V5 升级:提交侧背压 + 遥测(pool.total timer / done / failed / skipped)
# ---------------------------------------------------------------------------
def test_v5_no_backpressure_when_backlog_within_limit(tmp_path, single_thread_executor):
    """积压不超过 2×workers 时零额外停顿:停顿次数恰等于目标数。"""
    cfg = make_cfg(tmp_path)
    items = [SITE_A, SITE_B, SITE_C]
    scan = FakeScan(reports={u: make_report(u) for u in items})
    sleeper = FakeSleep()
    run_pool(
        cfg, items, run_scan=scan, memory=FakeMemory(),
        executor=single_thread_executor, workers=2,  # 阈值 2×2=4,积压峰 2
        sleep=sleeper, jitter=fixed_jitter,
    )
    assert len(sleeper.calls) == len(items)  # 只有每目标一次的礼貌停顿


def test_v5_backpressure_extra_pause_when_backlog_exceeds_limit(tmp_path):
    """积压 > 2×workers:每个越界提交前多停一轮(可量化、可注入)。"""
    cfg = make_cfg(tmp_path)
    release = threading.Event()
    items = [f"https://bp{i}.example.com/" for i in range(5)]
    reports = {
        u: make_report(u, verdict=Verdict.CLEAN, needs_review=False) for u in items
    }

    def blocking_scan(url: str, cfg: Config) -> Any:
        release.wait(timeout=10)  # 全部任务挂起,人为制造最大积压
        return reports[url]

    sleeper = FakeSleep()
    watchdog = threading.Timer(1.0, release.set)  # 兜底放行,防测试挂死
    watchdog.start()
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        summary = run_pool(
            cfg, items, run_scan=blocking_scan, memory=FakeMemory(),
            executor=ex, workers=1,  # 阈值 2×1=2
            sleep=sleeper, jitter=fixed_jitter,
        )
    finally:
        release.set()
        watchdog.cancel()
        ex.shutdown(wait=True)

    assert summary["done"] == 5
    # 提交 #1/#2/#3 前积压 0/1/2(不超过 2)→ 无背压;
    # 提交 #4/#5 前积压 3/4(> 2)→ 各多停一轮:5 常规 + 2 背压 = 7
    assert len(sleeper.calls) == 7
    assert all(s == pytest.approx(1.2) for s in sleeper.calls)  # 背压停顿同款时长


def test_v5_pool_telemetry_total_timer_and_counters(tmp_path, single_thread_executor):
    """pool.total 计时一次;done/failed 计数与 summary 对账。"""
    cfg = make_cfg(tmp_path)
    scan = FakeScan(
        reports={
            SITE_A: make_report(SITE_A),
            SITE_B: make_report(SITE_B, verdict=Verdict.CLEAN, needs_review=False),
        },
        errors=(SITE_C,),
    )
    telemetry.reset()
    summary = run_pool(
        cfg, [SITE_A, SITE_B, SITE_C], run_scan=scan, memory=FakeMemory(),
        executor=single_thread_executor, sleep=FakeSleep(), jitter=fixed_jitter,
    )
    assert (summary["done"], summary["failed"], summary["skipped"]) == (2, 1, 0)
    snap = telemetry.snapshot()
    assert snap["counters"]["pool.done"] == 2
    assert snap["counters"]["pool.failed"] == 1
    assert snap["counters"].get("pool.skipped", 0) == 0
    assert snap["timers"]["pool.total"]["count"] == 1


def test_v5_pool_telemetry_skip_and_backpressure_counters(
    tmp_path, single_thread_executor
):
    """第二轮全 skipped:pool.skipped 计数;积压不越界时无 backpressure 计数。"""
    cfg = make_cfg(tmp_path)
    reports = {SITE_A: make_report(SITE_A), SITE_B: make_report(SITE_B)}
    scan = FakeScan(reports=reports)
    memory = FakeMemory()
    telemetry.reset()
    run_pool(
        cfg, [SITE_A, SITE_B], run_scan=scan, memory=memory,
        executor=single_thread_executor, sleep=FakeSleep(), jitter=fixed_jitter,
    )
    telemetry.reset()
    second = run_pool(
        cfg, [SITE_A, SITE_B], run_scan=scan, memory=memory,
        executor=single_thread_executor, sleep=FakeSleep(), jitter=fixed_jitter,
    )
    assert second["skipped"] == 2
    snap = telemetry.snapshot()
    assert snap["counters"]["pool.skipped"] == 2
    assert snap["counters"].get("pool.done", 0) == 0
    assert snap["counters"].get("pool.backpressure", 0) == 0  # 积压未越界


# ---------------------------------------------------------------------------
# A203:trace 贯通回归(propagate 快照默认开,不影响池既有语义)
# ---------------------------------------------------------------------------
def test_a203_trace_on_backpressure_pauses_unchanged(tmp_path):
    """活跃 trace 下背压停顿序列与无 trace 完全一致(5 常规 + 2 背压 = 7)。

    propagate/restore 只带上下文,不得改变提交侧节奏(礼貌/背压红线)。
    """
    from netsentinel import telemetry_trace as tt

    cfg = make_cfg(tmp_path)
    release = threading.Event()
    items = [f"https://tr{i}.example.com/" for i in range(5)]
    reports = {
        u: make_report(u, verdict=Verdict.CLEAN, needs_review=False) for u in items
    }

    def blocking_scan(url: str, cfg: Config) -> Any:
        release.wait(timeout=10)  # 人为制造最大积压(与 V5 背压用例同款)
        return reports[url]

    sleeper = FakeSleep()
    watchdog = threading.Timer(1.0, release.set)
    watchdog.start()
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        with tt.new_trace():
            summary = run_pool(
                cfg, items, run_scan=blocking_scan, memory=FakeMemory(),
                executor=ex, workers=1,  # 阈值 2×1=2
                sleep=sleeper, jitter=fixed_jitter,
            )
    finally:
        release.set()
        watchdog.cancel()
        ex.shutdown(wait=True)

    assert summary["done"] == 5
    # 与 test_v5_backpressure... 无 trace 基准逐毫秒一致:零额外/缺失停顿
    assert len(sleeper.calls) == 7
    assert all(s == pytest.approx(1.2) for s in sleeper.calls)


def test_a203_trace_on_injected_executor_not_shutdown(tmp_path):
    """trace 开启 + 注入 executor:run_pool 结束后 executor 仍归调用方可用。"""
    from netsentinel import telemetry_trace as tt

    cfg = make_cfg(tmp_path)
    scan = FakeScan(reports={SITE_A: make_report(SITE_A, needs_review=False)})

    def traced_scan(url: str, cfg_: Config) -> Any:
        with tt.span("池.站点", attrs={"stage": "scan"}):
            return scan(url, cfg_)

    ex = ThreadPoolExecutor(max_workers=1)
    try:
        with tt.new_trace() as tid:
            summary = run_pool(
                cfg, [SITE_A], run_scan=traced_scan, memory=FakeMemory(),
                executor=ex, sleep=FakeSleep(), jitter=fixed_jitter,
            )
        assert summary["done"] == 1
        # worker 内 span 挂到主 trace(以提交侧快照为父,成树)
        tree = tt.export_trace_json(tid)
        names = {n["name"] for n in _walk_names(tree["spans"])}
        assert "池.站点" in names
    finally:
        assert ex.submit(lambda: 42).result() == 42  # 未被 run_pool 关闭
        ex.shutdown(wait=True)


def _walk_names(spans: list[Any]) -> Any:
    """深度优先展开嵌套 span 树,逐个产出节点。"""
    for node in spans:
        yield node
        yield from _walk_names(node["children"])


# ---------------------------------------------------------------------------
# A212:AIMD 自适应背压(注入 aimd 才启用;默认 None=现状静态背压逐字节)
# ---------------------------------------------------------------------------
class SyncExecutor:
    """submit 即同步执行并返回已完成 Future:背压确定化为零积压。"""

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        fut: Future = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - 与真实 Future 同语义
            fut.set_exception(exc)
        return fut

    def shutdown(self, wait: bool = True) -> None:  # pragma: no cover
        return None


class SteppingClock:
    """每次被调用前进 1.0 秒的脚本钟:任务时长(两次 now 差)恒 1.0s。"""

    def __init__(self, step: float = 1.0) -> None:
        self._value = 0.0
        self._step = float(step)

    def __call__(self) -> float:
        self._value += self._step
        return self._value


class SignalingSleep:
    """记录停顿时长,并在脚本指定的第 N 次停顿时注入利用率信号。"""

    def __init__(self, aimd: AIMDController, signals: dict[int, float]) -> None:
        self.calls: list[float] = []
        self._aimd = aimd
        self._signals = dict(signals)

    def __call__(self, seconds: float) -> None:
        self.calls.append(float(seconds))
        util = self._signals.pop(len(self.calls), None)
        if util is not None:
            self._aimd.on_signal(util)


def run_blocking_backlog(
    tmp_path: pathlib.Path,
    *,
    sleep: Callable[[float], None],
    extra_kwargs: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Callable[[float], None]]:
    """确定性最大积压场景(与 V5/A203 背压用例同款):单线程执行器 +
    全部任务挂起至放行;提交侧各轮 pending 恒为 0/1/2/3/4。"""
    cfg = make_cfg(tmp_path)
    release = threading.Event()
    items = [f"https://a212-{i}.example.com/" for i in range(5)]
    reports = {
        u: make_report(u, verdict=Verdict.CLEAN, needs_review=False) for u in items
    }

    def blocking_scan(url: str, cfg: Config) -> Any:
        release.wait(timeout=10)
        return reports[url]

    watchdog = threading.Timer(1.0, release.set)
    watchdog.start()
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        kwargs: dict[str, Any] = dict(
            run_scan=blocking_scan, memory=FakeMemory(), executor=ex,
            workers=1, sleep=sleep, jitter=fixed_jitter,
        )
        kwargs.update(extra_kwargs or {})
        summary = run_pool(cfg, items, **kwargs)
    finally:
        release.set()
        watchdog.cancel()
        ex.shutdown(wait=True)
    return summary, sleep


def test_a212_default_none_matches_status_quo_millisecond_exact(tmp_path):
    """不注入=现状:完全不传 aimd 与显式 aimd=None 的停顿序列逐毫秒一致,
    且等于 V5 静态基准(5 常规 + 2 背压 = 7 次 1.2s)。"""
    summary_a, sleeper_a = run_blocking_backlog(tmp_path, sleep=FakeSleep())
    assert summary_a["done"] == 5
    summary_b, sleeper_b = run_blocking_backlog(
        tmp_path, sleep=FakeSleep(), extra_kwargs={"aimd": None}
    )
    assert summary_b["done"] == 5
    assert sleeper_a.calls == sleeper_b.calls  # 逐毫秒一致(值级全等)
    assert sleeper_a.calls == [pytest.approx(1.2)] * 7  # 与 A203/V5 基准同款


def test_a212_aimd_pause_sequence_matches_prediction(tmp_path):
    """注入信号下背压停顿序列符合 AIMD 预测(窗口演化逐点手算)。

    cap=8/calm_round=100(提交期无完成 → 无加性增);信号注入于第 1/2 次
    停顿(各 0.99 → 窗口 8→4→2):
    提交 #1:pending=0 < 8 → 提交;礼貌①(信号 → 4)
    提交 #2:pending=1 < 4 → 提交;礼貌②(信号 → 2)
    提交 #3:pending=2 ≥ 2 → 背压③ + 礼貌④
    提交 #4:pending=3 ≥ 2 → 背压⑤ + 礼貌⑥
    提交 #5:pending=4 ≥ 2 → 背压⑦ + 礼貌⑧
    共 8 次停顿(5 常规 + 3 背压),时长全部 1.2s;终局窗口 2。
    """
    ctl = AIMDController(8, calm_round=100)
    sleeper = SignalingSleep(ctl, {1: 0.99, 2: 0.99})
    telemetry.reset()
    summary, sleeper = run_blocking_backlog(
        tmp_path, sleep=sleeper, extra_kwargs={"aimd": ctl}
    )
    assert summary["done"] == 5 and summary["failed"] == 0
    assert len(sleeper.calls) == 8  # AIMD 预测:5 常规 + 3 背压
    assert all(s == pytest.approx(1.2) for s in sleeper.calls)  # 背压同款时长
    assert telemetry.snapshot()["counters"]["pool.backpressure"] == 3
    assert ctl.permits == 2  # 提交期窗口 8→4→2;完成回调不计增(calm 未满)


def test_a212_aimd_at_floor_never_removes_polite_pauses(tmp_path):
    """窗口压到地板(permits=1)也只增不减礼貌:同步执行器零积压 →
    停顿恰为每目标一次礼貌停顿(AIMD 判停永不跳过常规礼貌间隔)。"""
    cfg = make_cfg(tmp_path)
    ctl = AIMDController(1, floor=1)  # cap=1:窗口恒 1(最大收缩)
    assert ctl.permits == 1
    items = [SITE_A, SITE_B, SITE_C]
    sleeper = FakeSleep()
    telemetry.reset()
    summary = run_pool(
        cfg, items, run_scan=FakeScan(reports={u: make_report(u) for u in items}),
        memory=FakeMemory(), executor=SyncExecutor(), sleep=sleeper,
        jitter=fixed_jitter, aimd=ctl,
    )
    assert summary["done"] == 3
    assert sleeper.calls == [pytest.approx(1.2)] * 3  # 恰每目标一次,零缺失
    assert telemetry.snapshot()["counters"].get("pool.backpressure", 0) == 0


def test_a212_completions_feed_controller_deterministic_clock(tmp_path):
    """任务完成回喂:脚本钟步进 1.0s → 每任务时长恰 1.0、EWMA 恒 1.0,
    窗口按 calm_round 线性回升并饱和(确定性注入时钟全链对账)。"""
    cfg = make_cfg(tmp_path)
    ctl = AIMDController(4, calm_round=2, clock_fn=SteppingClock(1.0))
    assert ctl.on_signal(0.99) is True  # 预置过载:窗口 4 → 2
    items = [f"https://clk{i}.example.com/" for i in range(6)]
    sleeper = FakeSleep()
    summary = run_pool(
        cfg, items, run_scan=FakeScan(reports={u: make_report(u) for u in items}),
        memory=FakeMemory(), executor=SyncExecutor(), sleep=sleeper,
        jitter=fixed_jitter, aimd=ctl,
    )
    assert summary["done"] == 6
    assert sleeper.calls == [pytest.approx(1.2)] * 6  # permits≥2 > pending(0)
    st = ctl.state()
    assert st["samples"] == 6  # 每任务恰一次完成回调
    assert st["ewma_s"] == 1.0 and st["baseline_s"] == 1.0  # 时长恒 1.0(手算)
    assert st["increases"] == 2  # 完成第 2/4 个 → 2→3→4;第 6 个饱和不虚计
    assert ctl.permits == 4  # 线性回升恰饱和在 cap


def test_a212_aimd_composes_with_trace_propagation(tmp_path):
    """AIMD 注入不动 trace 贯通路径:活跃 trace 下提交物仍是
    _scan_one_traced,worker 挂同一棵树,时长照喂 on_task_done。"""
    from netsentinel import telemetry_trace as tt

    cfg = make_cfg(tmp_path)

    class RecordingExecutor:
        """记录提交物函数名,转交真实单线程池执行。"""

        def __init__(self) -> None:
            self._ex = ThreadPoolExecutor(max_workers=1)
            self.submitted: list[str] = []

        def submit(self, fn, *args, **kwargs):
            self.submitted.append(getattr(fn, "__name__", str(fn)))
            return self._ex.submit(fn, *args, **kwargs)

        def shutdown(self):
            self._ex.shutdown(wait=True)

    ex = RecordingExecutor()
    ctl = AIMDController(4, calm_round=100)
    seen: list[str | None] = []

    def traced_scan(url: str, cfg_: Config) -> Any:
        seen.append(tt.current_trace_id())
        with tt.span("池.站点", attrs={"stage": "scan"}):
            return make_report(url, verdict=Verdict.CLEAN, needs_review=False)

    try:
        with tt.new_trace() as tid:
            summary = run_pool(
                cfg, [SITE_A], run_scan=traced_scan, memory=FakeMemory(),
                executor=ex, sleep=FakeSleep(), jitter=fixed_jitter, aimd=ctl,
            )
    finally:
        ex.shutdown()

    assert summary["done"] == 1
    assert ex.submitted == ["_scan_one_traced"]  # A203 提交物原样
    assert seen == [tid]  # worker 首行 restore:与主线程同一条 trace
    assert ctl.state()["samples"] == 1  # 时长回喂不因 trace 包装丢失


# ---------------------------------------------------------------------------
# A236:窗口→executor 重建闭环 + 进程路径 worker 时长回传
# ---------------------------------------------------------------------------
class RecordingSyncExecutor(SyncExecutor):
    """记录提交目标与关闭次数的同步执行器(重建闭环的确定性观测点)。"""

    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[str] = []
        self.shutdown_calls = 0

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        if args:
            self.submitted.append(str(args[0]))
        return super().submit(fn, *args, **kwargs)

    def shutdown(self, wait: bool = True) -> None:
        self.shutdown_calls += 1


class FakeSyncProcessPool(ProcessPoolExecutor):
    """同步假进程池:通过**真实 isinstance 探测**,submit 就地执行(零子进程)。

    跳过 ``ProcessPoolExecutor.__init__``(不建管理线程/队列、不 spawn),
    仅初始化 ``Executor`` 基类(与 A166 测试的 fake ProcessPoolExecutor
    同款思路,但保留真实类型以驱动 :func:`pool._is_process_pool` 分支);
    提交物仍是模块级纯函数(红线 35:不 pickle 实例)。
    """

    def __init__(self) -> None:
        Executor.__init__(self)  # 测试专用:跳过进程池基建,不留孤儿
        self.submitted_names: list[str] = []

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
        self.submitted_names.append(getattr(fn, "__name__", str(fn)))
        fut: Future = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - 与真实 Future 同语义
            fut.set_exception(exc)
        return fut

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        return None


def test_a236_rebuild_loop_shrink_uses_min_permits_and_workers(tmp_path):
    """注入信号触发减窗:分片提前收尾→排空→以 min(permits, workers) 重建。

    手算(cap=8, calm_round=100 → 提交期无加性增;信号注入于第 1/2 次停顿,
    窗口 8→4→2;原始档位 workers=4):
    分片①[t1]:t1 提交(同步执行器零积压)→ pause① 减窗 8→4
      (4×2 ≤ 8 越阈)→ 提前收尾排空;边界 min(4,4)=4 == 当前 4 → 不重建;
    分片②[t2](size=4,越阈即收尾):pause② 减窗 4→2 → 边界
      min(2,4)=2 ≠ 4 → 重建#1(max_workers=2);
    分片③[t3,t4](size=2)、分片④[t5,t6]:无越阈、无扩容 → 不重建。
    全程恰 6 次礼貌停顿(每目标一次;红线 35:重建零触碰停顿)、零背压、
    重建计数 1、时长计时 1 次;旧池(初始执行器)在重建点被关闭,
    新池承接 t3..t6,批次结束被 finally 关闭。
    """
    cfg = make_cfg(tmp_path)
    items = [f"https://a236-s{i}.example.com/" for i in range(6)]
    ctl = AIMDController(8, calm_round=100, clock_fn=SteppingClock(1.0))
    sleeper = SignalingSleep(ctl, {1: 0.99, 2: 0.99})
    initial = RecordingSyncExecutor()
    built_sizes: list[int] = []
    built_pools: list[RecordingSyncExecutor] = []

    def factory(n: int) -> RecordingSyncExecutor:
        built_sizes.append(int(n))
        ex = RecordingSyncExecutor()
        built_pools.append(ex)
        return ex

    telemetry.reset()
    summary = pool._run_batch(
        cfg, items, executor=initial,
        scan=FakeScan(reports={u: make_report(u) for u in items}),
        memory=FakeMemory(), pause=sleeper, jitter_fn=fixed_jitter, workers=4,
        aimd=ctl, rebuild_executor=True, executor_factory=factory,
        executor_workers=4,
    )
    assert summary["done"] == 6 and summary["failed"] == 0
    assert summary["results"] == {u: "nsfw" for u in items}
    assert built_sizes == [2]  # min(permits=2, 原始档位 workers=4)
    assert sleeper.calls == [pytest.approx(1.2)] * 6  # 重建不新增/不跳过停顿
    snap = telemetry.snapshot()
    assert snap["counters"]["pool.rebuild.count"] == 1
    assert snap["timers"]["pool.rebuild.duration"]["count"] == 1
    assert snap["counters"].get("pool.backpressure", 0) == 0
    assert initial.submitted == items[:2]  # 旧池只承接重建前的目标
    assert initial.shutdown_calls == 1  # 重建点关闭(分片已排空,即时)
    assert built_pools[0].submitted == items[2:]  # 新池承接其余目标
    assert built_pools[0].shutdown_calls == 1  # 批次结束 finally 关闭


def test_a236_rebuild_expand_capped_at_original_workers(tmp_path):
    """窗口回升经重建扩容,但恒 ≤ 原始档位 workers(cap 语义,红线 47/37)。

    手算(cap=8, calm_round=1, SteppingClock(1.0) → 每任务时长恰 1.0=基线,
    完成即 +1 许可;信号注入于第 1/2 次停顿):
    分片①[t1]:完成(已饱和 8 不增)→ pause① 8→4 → 越阈收尾;边界
      min(4,4)=4 == 4 → 不重建;分片②[t2]:完成 4→5 → pause② 5//2=2 →
      边界 min(2,4)=2 ≠ 4 → 重建#1(2);分片③[t3,t4](build=2):完成
      2→3→4;边界 4 ≥ 2×2 → 扩容重建#2(4);分片④[t5..t8]:完成
      4→5→…→8;边界 8 ≥ 4×2 但 min(8,4)=4 == 当前 4 → **不重建
      (窗口恢复不越原始档位)**;分片⑤[t9,t10]:饱和无操作。
    工厂请求恰 [2, 4];全程 10 次礼貌停顿、零背压、重建计数 2。
    """
    cfg = make_cfg(tmp_path)
    items = [f"https://a236-x{i}.example.com/" for i in range(10)]
    ctl = AIMDController(8, calm_round=1, clock_fn=SteppingClock(1.0))
    sleeper = SignalingSleep(ctl, {1: 0.99, 2: 0.99})
    initial = RecordingSyncExecutor()
    built_sizes: list[int] = []

    def factory(n: int) -> RecordingSyncExecutor:
        built_sizes.append(int(n))
        return RecordingSyncExecutor()

    telemetry.reset()
    summary = pool._run_batch(
        cfg, items, executor=initial,
        scan=FakeScan(reports={u: make_report(u) for u in items}),
        memory=FakeMemory(), pause=sleeper, jitter_fn=fixed_jitter, workers=4,
        aimd=ctl, rebuild_executor=True, executor_factory=factory,
        executor_workers=4,
    )
    assert summary["done"] == 10 and summary["failed"] == 0
    assert built_sizes == [2, 4]
    assert all(1 <= n <= 4 for n in built_sizes)  # 恒 ≤ 原始档位(红线 47/37)
    assert ctl.permits == 8  # 窗口已回升到 cap(父窗口看得到完成)
    assert sleeper.calls == [pytest.approx(1.2)] * 10  # 红线 35:重建零触碰
    assert telemetry.snapshot()["counters"]["pool.rebuild.count"] == 2


def test_a236_rebuild_without_shrink_pause_sequence_unchanged(tmp_path):
    """无减窗信号时重建闭环零触发:停顿序列与 rebuild_executor=False
    逐毫秒一致(分片边界保持背压/礼貌序列的"无信号"基线)。"""
    def run_once(flag: bool) -> tuple[dict[str, Any], SignalingSleep]:
        cfg = make_cfg(tmp_path)
        release = threading.Event()
        items = [f"https://a236-i{i}.example.com/" for i in range(5)]
        reports = {
            u: make_report(u, verdict=Verdict.CLEAN, needs_review=False)
            for u in items
        }

        def blocking_scan(url: str, cfg_: Config) -> Any:
            release.wait(timeout=10)
            return reports[url]

        ctl = AIMDController(8, calm_round=100)
        sleeper = SignalingSleep(ctl, {})  # 无信号 → 窗口恒 8,永不越阈
        watchdog = threading.Timer(1.0, release.set)
        watchdog.start()
        telemetry.reset()
        try:
            summary = run_pool(
                cfg, items, run_scan=blocking_scan, memory=FakeMemory(),
                workers=1, sleep=sleeper, jitter=fixed_jitter,
                aimd=ctl, rebuild_executor=flag,
            )
        finally:
            release.set()
            watchdog.cancel()
        return summary, sleeper

    summary_a, sleeper_a = run_once(False)
    summary_b, sleeper_b = run_once(True)
    assert summary_a["done"] == 5 and summary_b["done"] == 5
    assert sleeper_a.calls == sleeper_b.calls  # 值级全等(逐毫秒一致)
    assert sleeper_a.calls == [pytest.approx(1.2)] * 5
    assert telemetry.snapshot()["counters"].get("pool.rebuild.count", 0) == 0


def test_a236_rebuild_end_to_end_blocking_backlog(tmp_path):
    """run_pool 自建池端到端:阻塞积压 + 双信号减窗 8→4→2 → 真线程池重建为 2。

    手算(workers=4 自建池;任务挂起至 1.0s 看门狗放行;信号注入于
    第 1/2 次停顿):分片①[t1](pause① 8→4,越阈收尾→排空 t1)→边界
    min(4,4)=4 == 4 不重建;分片②[t2](pause② 4→2)→边界 min(2,4)=2 ≠ 4
    → 重建#1(真 ThreadPoolExecutor);分片③[t3,t4](build=2,无越阈)、
    分片④[t5]。恰 5 次礼貌停顿、零背压(每分片排空后 pending 归零,
    背压序列在分片边界保持)、重建计数 1、5 目标全部完成。
    """
    cfg = make_cfg(tmp_path)
    release = threading.Event()
    items = [f"https://a236-e{i}.example.com/" for i in range(5)]
    reports = {
        u: make_report(u, verdict=Verdict.CLEAN, needs_review=False)
        for u in items
    }

    def blocking_scan(url: str, cfg_: Config) -> Any:
        release.wait(timeout=10)
        return reports[url]

    ctl = AIMDController(8, calm_round=100)
    sleeper = SignalingSleep(ctl, {1: 0.99, 2: 0.99})
    watchdog = threading.Timer(1.0, release.set)
    watchdog.start()
    telemetry.reset()
    try:
        summary = run_pool(
            cfg, items, run_scan=blocking_scan, memory=FakeMemory(),
            workers=4, sleep=sleeper, jitter=fixed_jitter,
            aimd=ctl, rebuild_executor=True,
        )
    finally:
        release.set()
        watchdog.cancel()
    assert summary["done"] == 5 and summary["failed"] == 0
    assert sleeper.calls == [pytest.approx(1.2)] * 5
    snap = telemetry.snapshot()
    assert snap["counters"]["pool.rebuild.count"] == 1
    assert snap["counters"].get("pool.backpressure", 0) == 0


def test_a236_rebuild_failure_falls_back_to_original_pool(tmp_path):
    """重建工厂抛异常:回退原池继续,绝不中断整批;失败诚实计数。"""
    cfg = make_cfg(tmp_path)
    items = [f"https://a236-f{i}.example.com/" for i in range(6)]
    ctl = AIMDController(8, calm_round=100, clock_fn=SteppingClock(1.0))
    sleeper = SignalingSleep(ctl, {1: 0.99, 2: 0.99})
    initial = RecordingSyncExecutor()
    requests: list[int] = []

    def boom(n: int) -> RecordingSyncExecutor:
        requests.append(int(n))
        raise RuntimeError(f"模拟线程池构造失败:max_workers={n}")

    telemetry.reset()
    summary = pool._run_batch(
        cfg, items, executor=initial,
        scan=FakeScan(reports={u: make_report(u) for u in items}),
        memory=FakeMemory(), pause=sleeper, jitter_fn=fixed_jitter, workers=4,
        aimd=ctl, rebuild_executor=True, executor_factory=boom,
        executor_workers=4,
    )
    assert summary["done"] == 6 and summary["failed"] == 0
    # 分片①边界 desired=4==4 不触发;其后每个越阈边界各试一次(共 4 次)
    assert requests == [2, 2, 2, 2]
    snap = telemetry.snapshot()
    assert snap["counters"].get("pool.rebuild.count", 0) == 0
    assert snap["counters"]["pool.rebuild.failure"] == 4
    assert initial.submitted == items  # 全部目标仍在原池完成(不中断)
    assert initial.shutdown_calls == 0  # 原池未被关闭(回退继续使用)
    assert sleeper.calls == [pytest.approx(1.2)] * 6


def test_a236_rebuild_flag_ignored_for_injected_executor(tmp_path, caplog):
    """注入 executor 归调用方所有:重建开关被忽略(告警),池不被重建/关闭。"""
    cfg = make_cfg(tmp_path)
    items = [SITE_A, SITE_B]
    ctl = AIMDController(8, calm_round=100)
    ex = RecordingSyncExecutor()
    telemetry.reset()
    with caplog.at_level(logging.WARNING, logger="netsentinel.ops.pool"):
        summary = run_pool(
            cfg, items, run_scan=FakeScan(reports={u: make_report(u) for u in items}),
            memory=FakeMemory(), executor=ex, workers=4,
            sleep=FakeSleep(), jitter=fixed_jitter, aimd=ctl,
            rebuild_executor=True,
        )
    assert summary["done"] == 2
    assert ex.shutdown_calls == 0  # 归调用方:绝不被关闭
    assert telemetry.snapshot()["counters"].get("pool.rebuild.count", 0) == 0
    assert any("rebuild_executor" in r.message for r in caplog.records)
    assert ex.submit(lambda: 42).result() == 42  # 池仍归调用方可用


def test_a236_rebuild_without_aimd_is_status_quo(tmp_path):
    """rebuild_executor=True 但未注入 aimd:无窗口信号 → 单分片跑完、零重建,
    停顿序列与 False 完全一致(不注入 aimd=现状快照;向后兼容)。"""
    cfg = make_cfg(tmp_path)
    items = [SITE_A, SITE_B, SITE_C]
    scan = FakeScan(reports={u: make_report(u) for u in items})
    sleeper_off, sleeper_on = FakeSleep(), FakeSleep()
    telemetry.reset()
    summary_off = run_pool(
        cfg, items, run_scan=scan, memory=FakeMemory(), workers=2,
        sleep=sleeper_off, jitter=fixed_jitter, rebuild_executor=False,
    )
    summary_on = run_pool(
        cfg, items, run_scan=scan, memory=FakeMemory(), workers=2,
        sleep=sleeper_on, jitter=fixed_jitter, rebuild_executor=True,
    )
    assert summary_on == summary_off
    assert summary_on["done"] == 3
    assert sleeper_on.calls == sleeper_off.calls  # 停顿序列逐毫秒一致
    assert telemetry.snapshot()["counters"].get("pool.rebuild.count", 0) == 0


def test_a236_politeness_constants_and_pause_sites_untouched():
    """红线 35 源级断言:礼貌常量零触碰;停顿只出自同款表达式且恰两处
    (背压 + 常规),重建/回传路径不引入任何新的停顿调用;模块内除缺省
    sleep 实现外无其他真实睡眠。"""
    import inspect

    assert pool.PAUSE_BASE_S == 1.0
    assert pool.PAUSE_JITTER_S == 0.5
    src = inspect.getsource(pool)
    assert src.count("pause(PAUSE_BASE_S + PAUSE_JITTER_S * jitter_fn())") == 2
    assert src.count("time.sleep(") == 1  # 仅 _default_sleep(可注入替身)


def test_a236_process_pool_duration_return_feeds_parent_aimd(tmp_path):
    """进程形态(真 isinstance 探测):提交物是 _scan_one_timed 纯函数,
    worker 时长经返回值回传,父进程恰每任务一次喂 on_task_done(无
    _TimedScanAdapter 双计);外层 summary 形状与线程形态逐字节一致。

    latency_factor 巨大 → 微秒级真实时长的抖动永不触发时延减窗,
    断言只依赖计数(确定性);预置减窗 4→2 后由回传驱动加性增回升。"""
    cfg = make_cfg(tmp_path)
    items = [SITE_A, SITE_B, SITE_C]
    scan = FakeScan(reports={u: make_report(u) for u in items})

    ctl_p = AIMDController(4, calm_round=1, latency_factor=1e9)
    assert ctl_p.on_signal(0.99) is True  # 4 → 2
    ex = FakeSyncProcessPool()
    summary_p = run_pool(
        cfg, items, run_scan=scan, memory=FakeMemory(), executor=ex,
        sleep=FakeSleep(), jitter=fixed_jitter, aimd=ctl_p,
    )
    assert summary_p == {
        "total": 3, "done": 3, "failed": 0, "skipped": 0,
        "results": {SITE_A: "nsfw", SITE_B: "nsfw", SITE_C: "nsfw"},
        "errors": [],
    }
    assert ex.submitted_names == ["_scan_one_timed"] * 3  # 进程路径提交物
    st_p = ctl_p.state()
    assert st_p["samples"] == 3  # 每任务恰一次(适配器未启用 → 无双计)
    assert st_p["increases"] == 2  # 2→3→4;第 3 个饱和不虚计
    assert ctl_p.permits == 4  # 父窗口看得到子进程完成(回传汇合)

    # 线程形态(现状)同一场景:外层形状与窗口演化完全一致
    ctl_t = AIMDController(4, calm_round=1, latency_factor=1e9)
    assert ctl_t.on_signal(0.99) is True
    summary_t = run_pool(
        cfg, items, run_scan=scan, memory=FakeMemory(), executor=SyncExecutor(),
        sleep=FakeSleep(), jitter=fixed_jitter, aimd=ctl_t,
    )
    assert summary_t == summary_p  # 外层返回形状与现状逐字节一致
    st_t = ctl_t.state()
    assert (st_t["samples"], st_t["increases"]) == (st_p["samples"], st_p["increases"])
    assert ctl_t.permits == ctl_p.permits


def test_a236_mock_process_pool_fabricated_duration_feeds_aimd(
    tmp_path, monkeypatch
):
    """mock 进程池执行器(经 _is_process_pool 探测注入):预设时长回传 →
    aimd 计数精确;失败任务时长照实回传;裸结局防御分支不喂时长。"""
    cfg = make_cfg(tmp_path)

    class FabricatingExecutor:
        """submit 即弹出一个预设返回值(完全绕开真实工作函数)。"""

        def __init__(self, scripted: list[Any]) -> None:
            self.scripted = list(scripted)

        def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
            fut: Future = Future()
            fut.set_result(self.scripted.pop(0))
            return fut

        def shutdown(self, wait: bool = True) -> None:
            return None

    scripted = [
        pool._TimedOutcome(
            outcome=pool._TaskOutcome(
                status="done", verdict="nsfw", needs_review=False, agg=0.9
            ),
            duration_s=1.0,
        ),
        pool._TimedOutcome(outcome=None, duration_s=0.5, error="子进程分类炸了"),
        pool._TaskOutcome(status="skipped"),  # 裸结局(防御分支:不喂时长)
    ]
    ex = FabricatingExecutor(scripted)
    monkeypatch.setattr(pool, "_is_process_pool", lambda e: True)
    ctl = AIMDController(4, calm_round=2, clock_fn=SteppingClock(1.0))
    telemetry.reset()
    summary = run_pool(
        cfg, [SITE_A, SITE_B, SITE_C], run_scan=FakeScan(),
        memory=FakeMemory(), executor=ex, sleep=FakeSleep(),
        jitter=fixed_jitter, aimd=ctl,
    )
    assert (summary["done"], summary["failed"], summary["skipped"]) == (1, 1, 1)
    assert summary["errors"] == [(SITE_B, "子进程分类炸了")]
    st = ctl.state()
    assert st["samples"] == 2  # 1.0 与 0.5 各一次;裸结局不喂
    assert st["ewma_s"] == pytest.approx(0.75 * 1.0 + 0.25 * 0.5)
    assert st["baseline_s"] == 1.0


def test_a236_process_worker_error_returned_as_data_matches_thread_form(tmp_path):
    """worker 侧异常就地转数据回传:failed 计数/错误文案与线程形态完全
    一致;失败任务的时长同样照实回传(两形态均计)。"""
    cfg = make_cfg(tmp_path)
    scan = FakeScan(reports={SITE_A: make_report(SITE_A)}, errors=(SITE_B,))

    ctl_p = AIMDController(4, calm_round=100, latency_factor=1e9)
    summary_p = run_pool(
        cfg, [SITE_A, SITE_B], run_scan=scan, memory=FakeMemory(),
        executor=FakeSyncProcessPool(), sleep=FakeSleep(), jitter=fixed_jitter,
        aimd=ctl_p,
    )
    ctl_t = AIMDController(4, calm_round=100, latency_factor=1e9)
    summary_t = run_pool(
        cfg, [SITE_A, SITE_B], run_scan=scan, memory=FakeMemory(),
        executor=SyncExecutor(), sleep=FakeSleep(), jitter=fixed_jitter,
        aimd=ctl_t,
    )
    assert summary_p == summary_t  # 逐字节一致(含 errors 文案)
    assert summary_p["failed"] == 1 and summary_p["done"] == 1
    assert summary_p["errors"] == [(SITE_B, "模拟扫描失败:" + SITE_B)]
    assert ctl_p.state()["samples"] == 2  # 失败任务时长也照实回传
    assert ctl_t.state()["samples"] == 2


def test_a236_process_timed_mode_composes_with_trace(tmp_path, monkeypatch):
    """进程形态 + 活跃 trace:提交物是 _scan_one_timed 且携带快照
    (url, cfg, scan, memory, snapshot 共 5 参),worker 首行 restore
    接回同一棵 trace 树,时长回传不丢。"""
    from netsentinel import telemetry_trace as tt

    cfg = make_cfg(tmp_path)

    class RecordingProcExecutor:
        def __init__(self) -> None:
            self.submitted: list[tuple[str, int]] = []

        def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
            self.submitted.append((getattr(fn, "__name__", str(fn)), len(args)))
            fut: Future = Future()
            try:
                fut.set_result(fn(*args, **kwargs))
            except BaseException as exc:  # noqa: BLE001
                fut.set_exception(exc)
            return fut

        def shutdown(self, wait: bool = True) -> None:
            return None

    monkeypatch.setattr(pool, "_is_process_pool", lambda e: True)
    ex = RecordingProcExecutor()
    ctl = AIMDController(4, calm_round=100, latency_factor=1e9)
    seen: list[str | None] = []

    def traced_scan(url: str, cfg_: Config) -> Any:
        seen.append(tt.current_trace_id())
        return make_report(url, verdict=Verdict.CLEAN, needs_review=False)

    with tt.new_trace() as tid:
        summary = run_pool(
            cfg, [SITE_A], run_scan=traced_scan, memory=FakeMemory(),
            executor=ex, sleep=FakeSleep(), jitter=fixed_jitter, aimd=ctl,
        )
    assert summary["done"] == 1
    assert ex.submitted == [("_scan_one_timed", 5)]  # 快照随纯函数入子进程
    assert seen == [tid]  # worker 首行 restore:与主线程同一条 trace
    assert ctl.state()["samples"] == 1  # 时长回传不因 trace 包装丢失
