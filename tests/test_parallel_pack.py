"""A171:netsentinel.evidence.parallel_pack 单元测试(全离线 + 一例真集成)。

覆盖(契约 §2 A171 行 + 红线 35 / 37):

- 基本语义:空列表短路(不建池不计时)/ 保序 / 注入 builder 收到
  ``(report, cfg)`` 原对 / 注入 executor 逐包 submit 且不被关闭;
- 容错:单包失败 → None 占位 + 中文 warning + ``parallel_pack.failures``
  逐包计数;全失败仍等长;取结果超时同样 None 占位(超时可调,测试注入);
- **同 host 串行化(host→锁字典)**:fake builder 记录并发进入峰值——
  同 host 两包并发峰值 = 1(锁串行);不同 host 两包 Barrier(2) 必须
  等齐才放行(峰值 = 2,证明线程池真的并行,峰值 1 归因于锁而非串行池);
- 执行器三级解析:注入 / A164 在场委托 ``thread_pool(cfg, label="pack")`` /
  A164 缺席内置公式兜底 ThreadPool(mid@cores8 → 4,前缀 "pack");
- 红线 37:全档 workers ≤ 物理核数(委托态与兜底态);单批总量上限
  ``MAX_BATCH`` 中文 ValueError;缺省池随 with 关闭;
- 遥测:``parallel_pack.all`` 计时 / ``parallel_pack.bundles`` 按成功数 /
  ``parallel_pack.failures`` 按失败数;
- 缺省 builder 惰性解析 A11 ``packager.build_bundle``(monkeypatch 替身
  可被捕获);A11 缺席 → 中文 RuntimeError(原异常保留 ``__cause__``);
- 真集成:2 个 tmp 站点报告走真 ``build_bundle``(importorskip),zip /
  manifest / sha256 落盘且保序对位。

并行态确定性:核数 monkeypatch 固定 8(mid→4 / low→2 / high reserve=1→7);
A164 ``ops.concurrency`` 双态(在场走委托 / 强制缺席走内置兜底)均覆盖。
A214 追加:并行产物签名接线——默认 hmac-sha256 配置零签名(现状回归)、
ed25519 下注入 builder 的未签名产物补签且独立验签通过(zip 同步刷新)、
缺省 build_bundle 产物不重复签(恰一次)、补签失败只降级绝不成 None 占位。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
import threading
import time
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pytest

from netsentinel import telemetry
from netsentinel.contracts import (
    Config,
    EvidenceBundle,
    ImageEvidence,
    ImageScore,
    PageSample,
    SiteReport,
    Verdict,
)
from netsentinel.evidence import parallel_pack as pp
from netsentinel.security import ed25519
from netsentinel.security.bundle_sign import BundleSigner

#: 固定核数(autouse 夹具注入;三档期望:low 2 / mid 4 / high(reserve=1) 7)
CORES = 8

#: 同 host 串行性证明的观测窗口(秒):无锁时两线程几乎必然重叠
OVERLAP_WINDOW_S = 0.2

#: Barrier 等齐超时(秒):不同 host 若无真并发即破裂
BARRIER_TIMEOUT_S = 20.0

#: A214 确定性测试种子(与 test_packager.SEED_A/SEED_B 同源,便于复算公钥)
SEED_A = bytes.fromhex("11" * 32)
SEED_B = bytes.fromhex("22" * 32)


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """每用例隔离:核数探测固定 8、环境核数钩子清空、签名密钥环境清空。"""
    monkeypatch.setattr("os.cpu_count", lambda: CORES)
    monkeypatch.delenv("NETSENTINEL_FAKE_CORES", raising=False)
    monkeypatch.delenv("NETSENTINEL_SIGN_KEY", raising=False)
    monkeypatch.delenv("NETSENTINEL_ED25519_SEED_HEX", raising=False)


def make_cfg(tier: str = "mid", reserve: int = 1) -> Config:
    cfg = Config()
    cfg.concurrency_tier = tier
    cfg.cpu_reserve = reserve
    return cfg


def force_a164_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制 A164 ops.concurrency 缺席(内置兜底路径)。"""
    import netsentinel.ops as ops_pkg

    monkeypatch.setitem(sys.modules, "netsentinel.ops.concurrency", None)
    monkeypatch.delattr(ops_pkg, "concurrency", raising=False)


def a164_present() -> bool:
    """A164 是否在场(在场用例据此跳过/切换断言)。"""
    try:
        from netsentinel.ops import concurrency  # noqa: F401
    except Exception:
        return False
    return True


def make_report(url: str) -> SiteReport:
    return SiteReport(
        site_url=url,
        pages=[PageSample(url=url)],
        verdict=Verdict.SUSPECT,
        agg_nsw_prob=0.66,
        nsw_image_count=2,
    )


def make_reports(n: int, prefix: str = "site") -> list[SiteReport]:
    return [make_report(f"https://{prefix}{i}.example.test/") for i in range(n)]


def fake_bundle(report: SiteReport) -> EvidenceBundle:
    return EvidenceBundle(
        site_url=report.site_url,
        dir_path=f"data/evidence/{report.site_url}",
        manifest_path=f"data/evidence/{report.site_url}/manifest.json",
        zip_path=f"data/evidence/{report.site_url}.zip",
    )


def map_builder(**kwargs):
    """保序可用的假 builder:按 site_url 生成 bundle;url 含 'bad' 时抛异常。"""
    delay = float(kwargs.get("delay", 0.0))
    calls: list[str] = []

    def _build(report: SiteReport, cfg: Config) -> EvidenceBundle:
        calls.append(report.site_url)
        if delay:
            time.sleep(delay)
        if "bad" in report.site_url:
            raise RuntimeError(f"故意失败:{report.site_url}")
        return fake_bundle(report)

    _build.calls = calls  # type: ignore[attr-defined]
    return _build


class _RecordingExecutor:
    """同步假线程执行器:记录 submit 次数/任务,不自动关闭。"""

    def __init__(self) -> None:
        self.submit_calls = 0
        self.shutdown_calls = 0

    def submit(self, fn, /, *args, **kwargs):
        self.submit_calls += 1
        fut: Future = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001 - 同步执行,异常入 future
            fut.set_exception(exc)
        return fut

    def shutdown(self, wait: bool = True) -> None:
        self.shutdown_calls += 1


def make_fake_pool_class() -> type:
    """造一个同步假 ThreadPoolExecutor 类(记录构造参数、主线程内执行)。"""

    class FakeThreadPool:
        instances: list["FakeThreadPool"] = []

        def __init__(self, max_workers=None, thread_name_prefix="", **kw) -> None:
            self.max_workers = max_workers
            self.prefix = thread_name_prefix
            self.shutdown_calls = 0
            FakeThreadPool.instances.append(self)

        def submit(self, fn, /, *args, **kwargs):
            fut: Future = Future()
            try:
                fut.set_result(fn(*args, **kwargs))
            except Exception as exc:  # noqa: BLE001
                fut.set_exception(exc)
            return fut

        def shutdown(self, wait: bool = True) -> None:
            self.shutdown_calls += 1

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.shutdown(wait=True)
            return False

    return FakeThreadPool


# ---------------------------------------------------------------------------
# 基本语义:空批 / 保序 / builder 注入 / executor 注入
# ---------------------------------------------------------------------------
def test_empty_reports_returns_empty_without_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空列表 → [](不建池、不计时、不解析 builder)。"""
    fake = make_fake_pool_class()
    monkeypatch.setattr(pp, "ThreadPoolExecutor", fake)
    telemetry.reset()
    assert pp.pack_all([], make_cfg()) == []
    assert fake.instances == []  # 空批短路,池根本未创建
    assert telemetry.snapshot()["timers"] == {}  # 不计时
    # builder 也不该被解析:让缺省解析必然抛错,若被触捡即失败
    monkeypatch.setitem(sys.modules, "netsentinel.evidence.packager", None)
    assert pp.pack_all([], make_cfg()) == []


def test_pack_all_preserves_order_with_default_pool() -> None:
    """缺省线程池:8 个不同 host 报告保序,逐位对齐输入。"""
    reports = make_reports(8)
    results = pp.pack_all(reports, make_cfg(), builder=map_builder())
    assert [b.site_url for b in results] == [r.site_url for r in reports]
    assert all(b is not None for b in results)
    assert all(isinstance(b, EvidenceBundle) for b in results)


def test_injected_builder_receives_report_and_cfg_pairs() -> None:
    """注入 builder:每个 (report, cfg) 原对送达(调用次序随线程调度,
    结果保序另行断言),zip_path 可区分。"""
    builder = map_builder()
    seen_cfg: list[Config] = []
    inner = builder

    def _wrap(report: SiteReport, cfg: Config) -> EvidenceBundle:
        seen_cfg.append(cfg)
        return inner(report, cfg)

    cfg = make_cfg()
    reports = [make_report(f"https://inj{i}.example.test/") for i in range(5)]
    results = pp.pack_all(reports, cfg, builder=_wrap)
    assert len(builder.calls) == 5
    assert sorted(builder.calls) == sorted(r.site_url for r in reports)  # 全覆盖
    assert all(c is cfg for c in seen_cfg)  # cfg 原对象逐次送达
    assert [b.zip_path for b in results] == [
        f"data/evidence/{r.site_url}.zip" for r in reports
    ]


def test_injected_executor_used_and_not_closed() -> None:
    """注入 executor:逐包 submit、用完不关闭(归属调用方)。"""
    rec = _RecordingExecutor()
    reports = [make_report(f"https://own{i}.example.test/") for i in range(6)]
    results = pp.pack_all(reports, make_cfg(), builder=map_builder(), executor=rec)
    assert rec.submit_calls == 6
    assert rec.shutdown_calls == 0  # 不关闭
    assert all(b is not None for b in results)


# ---------------------------------------------------------------------------
# 容错:单包失败 / 全失败 / 取结果超时
# ---------------------------------------------------------------------------
def test_single_failure_becomes_none_placeholder(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """单包失败 → 该位 None 占位 + 中文 warning + failures +1;其余照常。"""
    telemetry.reset()
    caplog.set_level(logging.WARNING, logger=pp.__name__)
    reports = [
        make_report("https://ok-0.example.test/"),
        make_report("https://bad-1.example.test/"),
        make_report("https://ok-2.example.test/"),
    ]
    results = pp.pack_all(reports, make_cfg(), builder=map_builder())
    assert len(results) == 3
    assert results[0] is not None and results[1] is None and results[2] is not None
    assert results[0].site_url == reports[0].site_url  # 保序不塌陷
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("证据包构建失败" in w and "None 占位" in w for w in warnings)
    assert any("bad-1.example.test" in w for w in warnings)  # 定位到站点
    assert telemetry.snapshot()["counters"].get("parallel_pack.failures") == 1.0


def test_all_fail_still_full_length() -> None:
    """全部失败:仍返回等长全 None 列表(不抛出),failures 按包数累计。"""
    telemetry.reset()
    reports = [make_report(f"https://bad-{i}.example.test/") for i in range(4)]
    results = pp.pack_all(reports, make_cfg(), builder=map_builder())
    assert results == [None, None, None, None]
    assert telemetry.snapshot()["counters"].get("parallel_pack.failures") == 4.0


def test_result_timeout_becomes_none_placeholder(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """取结果超时(红线 37 超时上限):None 占位 + 中文 warning(含异常类型)。"""

    class _HangingExecutor:
        def __init__(self) -> None:
            self.submits = 0

        def submit(self, fn, /, *args, **kwargs):
            self.submits += 1
            return Future()  # 永不完成的 future → 必然超时

        def shutdown(self, wait: bool = True) -> None:
            pass

    monkeypatch.setattr(pp, "RESULT_TIMEOUT_S", 0.15)
    caplog.set_level(logging.WARNING, logger=pp.__name__)
    telemetry.reset()
    ex = _HangingExecutor()
    results = pp.pack_all(
        [make_report("https://slow.example.test/")],
        make_cfg(),
        builder=map_builder(),
        executor=ex,
    )
    assert ex.submits == 1
    assert results == [None]
    assert telemetry.snapshot()["counters"].get("parallel_pack.failures") == 1.0
    warnings = [r.getMessage() for r in caplog.records]
    assert any("TimeoutError" in w and "证据包构建失败" in w for w in warnings)


# ---------------------------------------------------------------------------
# 同 host 串行化(host→锁字典)/ 不同 host 真并发(Barrier 计数法)
# ---------------------------------------------------------------------------
class _PeakBuilder:
    """记录并发进入峰值的假 builder(可选入睡窗口 / Barrier 等齐)。"""

    def __init__(self, *, delay: float = 0.0, barrier: threading.Barrier | None = None):
        self.entered = 0
        self.peak = 0
        self.barrier = barrier
        self.delay = delay
        self._guard = threading.Lock()

    def __call__(self, report: SiteReport, cfg: Config) -> EvidenceBundle:
        with self._guard:
            self.entered += 1
            self.peak = max(self.peak, self.entered)
        if self.barrier is not None:
            self.barrier.wait()  # 不同 host:等齐 N 个并发才放行
        if self.delay:
            time.sleep(self.delay)  # 同 host:无锁时两线程几乎必然重叠
        with self._guard:
            self.entered -= 1
        return fake_bundle(report)


def test_same_host_two_packs_serialized_peak_one() -> None:
    """同 safe_host 两包:并发进入峰值 = 1(锁串行);结果照常全果。

    前提自证:注入的真实线程池确有 2 个 worker——若无锁,两包在
    0.2s 观察窗口内几乎必然重叠(峰值 2);峰值恰为 1 只能归因于
    host 锁串行化。
    """
    ex = ThreadPoolExecutor(max_workers=2)
    try:
        assert ex._max_workers == 2  # 池确有并行能力(前提自证)
        builder = _PeakBuilder(delay=OVERLAP_WINDOW_S)
        reports = [
            make_report("https://same.example.net/a"),
            make_report("https://same.example.net/b"),  # 同 host → 同锁
        ]
        results = pp.pack_all(reports, make_cfg(), builder=builder, executor=ex)
        assert builder.peak == 1, "同 host 构建未被串行化(检测到目录名竞争风险)"
        assert all(b is not None for b in results)
        assert [b.site_url for b in results] == [r.site_url for r in reports]
    finally:
        ex.shutdown(wait=True)


def test_different_hosts_two_packs_parallel_peak_two() -> None:
    """不同 host 两包:Barrier(2) 必须等齐才放行 → 峰值 = 2(真并发)。

    若线程池实为串行(或被全局锁误伤),Barrier 20s 超时破裂 →
    BrokenBarrierError → None 占位,断言全果即证不同 host 真并行。
    """
    barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT_S)
    ex = ThreadPoolExecutor(max_workers=2)
    try:
        builder = _PeakBuilder(barrier=barrier)
        reports = [
            make_report("https://alpha.example.test/"),
            make_report("https://beta.example.test/"),  # 不同 host → 各自锁
        ]
        results = pp.pack_all(reports, make_cfg(), builder=builder, executor=ex)
        assert all(b is not None for b in results), "不同 host 未并行(Barrier 破裂)"
        assert builder.peak == 2  # 两个 builder 同时在场
    finally:
        ex.shutdown(wait=True)


def test_host_lock_registry_keyed_by_safe_host() -> None:
    """锁注册表:同 host(含大小写/端口差异)同锁;不同 host / unknown 各自独立。"""
    lock_a1 = pp._host_lock("https://HostA.Example.NET/x")
    lock_a2 = pp._host_lock("http://hosta.example.net:8080/y")  # 同 host 同锁
    lock_b = pp._host_lock("https://hostb.example.net/")
    lock_u1 = pp._host_lock("not-a-url")
    lock_u2 = pp._host_lock("::://")
    assert lock_a1 is lock_a2
    assert lock_a1 is not lock_b
    assert lock_u1 is lock_u2  # 取不到主机名统一 unknown 键
    assert lock_u1 is not lock_a1
    # 键与 A11 packager._safe_host 逐字符一致(锁键=目录前缀,冻结模块对照)
    packager = pytest.importorskip("netsentinel.evidence.packager")
    for url in [
        "https://HostA.Example.NET/x",
        "http://hosta.example.net:8080/y",
        "https://hostb.example.net/",
        "not-a-url",
        "ftp://127.0.0.1/pub",
    ]:
        assert pp._safe_host(url) == packager._safe_host(url)


# ---------------------------------------------------------------------------
# 执行器三级解析:A164 委托 / 缺席兜底 / 非法档位
# ---------------------------------------------------------------------------
def test_default_pool_delegates_to_a164_thread_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A164 在场:缺省线程池走 thread_pool(cfg, label="pack")。"""
    if not a164_present():
        pytest.skip("A164 ops.concurrency 缺席(并行开发期)")
    from netsentinel.ops import concurrency

    captured: list[dict] = []
    original = concurrency.thread_pool

    def spy(cfg, *, label=concurrency.DEFAULT_LABEL, **kw):
        captured.append({"label": label, "cfg": cfg})
        return original(cfg, label=label, **kw)

    monkeypatch.setattr(concurrency, "thread_pool", spy)
    cfg = make_cfg()
    pp.pack_all(make_reports(3), cfg, builder=map_builder())
    assert captured == [{"label": "pack", "cfg": cfg}]


def test_fallback_pool_when_a164_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A164 缺席:兜底 ThreadPoolExecutor(io_workers 内置公式,label=pack)。"""
    force_a164_absent(monkeypatch)
    fake = make_fake_pool_class()
    monkeypatch.setattr(pp, "ThreadPoolExecutor", fake)
    telemetry.reset()
    results = pp.pack_all(
        make_reports(3), make_cfg(), builder=map_builder()
    )
    assert all(b is not None for b in results)
    assert fake.instances[0].max_workers == 4  # cores=8 → mid=4
    assert fake.instances[0].prefix == "pack"
    assert fake.instances[0].shutdown_calls == 1  # with 退出必关闭(红线 37)


def test_invalid_tier_raises_chinese_valueerror_both_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非法档位 → 中文 ValueError(A164 在场委托透传 / 缺席兜底同判)。"""
    cfg = make_cfg()
    cfg.concurrency_tier = "bogus"
    with pytest.raises(ValueError, match="档位"):
        pp.pack_all(make_reports(2), cfg, builder=map_builder())
    force_a164_absent(monkeypatch)
    with pytest.raises(ValueError, match="未知并发档位"):
        pp.pack_all(make_reports(2), cfg, builder=map_builder())


def test_invalid_tier_tolerated_with_injected_executor() -> None:
    """注入 executor:档位不被消费,非法值不阻断(校验只在自建池时发生)。"""
    cfg = make_cfg()
    cfg.concurrency_tier = "bogus"
    rec = _RecordingExecutor()
    results = pp.pack_all(
        make_reports(2), cfg, builder=map_builder(), executor=rec
    )
    assert rec.submit_calls == 2
    assert all(b is not None for b in results)


# ---------------------------------------------------------------------------
# 红线 37:workers ≤ cores;总量上限
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "tier,reserve", [("low", 1), ("mid", 1), ("high", 1), ("high", 0)]
)
def test_default_workers_never_exceed_cores(tier: str, reserve: int) -> None:
    """全档 × reserve:缺省线程池 workers ≤ 物理核数(A164 委托态)。"""
    cfg = make_cfg(tier, reserve)
    workers = pp._io_workers(cfg)
    assert 1 <= workers <= CORES
    if (tier, reserve) == ("high", 0):
        assert workers == CORES  # 压榨上限恰好全核,不越界


@pytest.mark.parametrize(
    "tier,reserve", [("low", 1), ("mid", 1), ("high", 1), ("high", 0)]
)
def test_fallback_workers_never_exceed_cores(
    monkeypatch: pytest.MonkeyPatch, tier: str, reserve: int
) -> None:
    """A164 缺席兜底公式同样 ≤ cores(红线 37,§1 逐条)。"""
    force_a164_absent(monkeypatch)
    cfg = make_cfg(tier, reserve)
    assert pp._io_workers(cfg) <= CORES
    assert pp._fallback_tier_workers(tier, reserve=reserve, cores=CORES) <= CORES


def test_fallback_tier_workers_formula() -> None:
    """内置三档公式逐条(§1):cores 注入,确定性无环境依赖。"""
    f = pp._fallback_tier_workers
    assert f("low", cores=4) == 1  # max(1, 4//4)
    assert f("mid", cores=4) == 2  # 4//2
    assert f("high", reserve=1, cores=4) == 3  # 4-1
    assert f("high", reserve=0, cores=4) == 4  # 全核
    assert (
        f("low", cores=1) == f("mid", cores=1) == f("high", reserve=0, cores=1) == 1
    )
    assert f("low", cores=2) == 1  # 2//4=0 → 兜 1
    assert f("mid", cores=8) == 4
    with pytest.raises(ValueError, match="未知并发档位"):
        f("fast", cores=8)


def test_max_batch_guard() -> None:
    """单批总量上限(红线 37):超过 MAX_BATCH → 中文 ValueError。"""
    huge = [make_report(f"https://huge{i}.example.test/") for i in range(pp.MAX_BATCH + 1)]
    with pytest.raises(ValueError, match="超过上限"):
        pp.pack_all(huge, make_cfg(), builder=map_builder())


# ---------------------------------------------------------------------------
# 遥测与缺省 builder 解析
# ---------------------------------------------------------------------------
def test_telemetry_timer_and_counters() -> None:
    """parallel_pack.all 计时;bundles 按成功数(3 成 1 败 → 3.0/1.0)。"""
    telemetry.reset()
    reports = [
        make_report("https://t-0.example.test/"),
        make_report("https://t-1.example.test/"),
        make_report("https://bad-2.example.test/"),
        make_report("https://t-3.example.test/"),
    ]
    pp.pack_all(reports, make_cfg(), builder=map_builder())
    snap = telemetry.snapshot()
    assert "parallel_pack.all" in snap["timers"]
    assert snap["counters"].get("parallel_pack.bundles") == 3.0
    assert snap["counters"].get("parallel_pack.failures") == 1.0


def test_default_builder_is_lazy_packager_build_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺省 builder 惰性解析 A11 build_bundle:monkeypatch 替身可被捕获。"""
    packager = pytest.importorskip("netsentinel.evidence.packager")
    calls: list[str] = []

    def spy(report: SiteReport, cfg: Config) -> EvidenceBundle:
        calls.append(report.site_url)
        return fake_bundle(report)

    monkeypatch.setattr(packager, "build_bundle", spy)
    reports = [make_report(f"https://lazy{i}.example.test/") for i in range(3)]
    results = pp.pack_all(reports, make_cfg())  # 不注入 builder
    assert len(calls) == 3
    assert sorted(calls) == sorted(r.site_url for r in reports)  # 全覆盖
    assert all(b is not None for b in results)


def test_default_builder_missing_packager_chinese_runtimeerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A11 缺席(极端并行态):缺省 builder 解析 → 中文 RuntimeError,
    原异常保留 __cause__(不静默降级)。"""
    import netsentinel.evidence as ev_pkg

    monkeypatch.setitem(sys.modules, "netsentinel.evidence.packager", None)
    monkeypatch.delattr(ev_pkg, "packager", raising=False)
    with pytest.raises(RuntimeError, match="packager") as excinfo:
        pp.pack_all([make_report("https://x.example.test/")], make_cfg())
    assert "builder" in str(excinfo.value)  # 给出注入 builder= 的修复提示
    assert isinstance(excinfo.value.__cause__, ImportError)  # 不吞原异常


# ---------------------------------------------------------------------------
# 真集成:真 build_bundle × 2 站点(依赖缺席 importorskip)
# ---------------------------------------------------------------------------
def test_real_build_bundle_integration_two_sites(tmp_path: Path) -> None:
    """真 A11 build_bundle 集成:2 个 tmp 站点(不同 host)→ 双包全果,
    zip/manifest/sha256 落盘、目录互异、保序对位。"""
    pytest.importorskip("netsentinel.evidence.packager")
    cfg = make_cfg()
    cfg.evidence_dir = str(tmp_path / "evidence")

    shot = tmp_path / "shot_alpha.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"alpha-shot" * 8)
    img = tmp_path / "img_alpha_0.jpg"
    img.write_bytes(b"jpeg-bytes" * 12)

    report_a = SiteReport(
        site_url="https://alpha-real.example.net/",
        pages=[
            PageSample(
                url="https://alpha-real.example.net/index.html",
                screenshot_path=str(shot),
                image_evidences=[
                    ImageEvidence(
                        path=str(img),
                        url="https://alpha-real.example.net/i/0.jpg",
                        source_page="https://alpha-real.example.net/index.html",
                    )
                ],
            )
        ],
        image_scores=[
            ImageScore(
                image=ImageEvidence(
                    path=str(img), url="u", source_page="p"
                ),
                model="ensemble",
                nsfw_prob=0.9123,
            )
        ],
        agg_nsw_prob=0.9123,
        nsw_image_count=1,
        verdict=Verdict.NSFW,
    )
    report_b = SiteReport(  # 无证据文件站点:packager 仍应成包
        site_url="https://beta-real.example.net/",
        pages=[PageSample(url="https://beta-real.example.net/")],
        verdict=Verdict.CLEAN,
    )

    results = pp.pack_all([report_a, report_b], cfg)
    assert len(results) == 2
    assert all(b is not None for b in results)
    bundle_a, bundle_b = results
    assert bundle_a.site_url == report_a.site_url  # 保序对位
    assert bundle_b.site_url == report_b.site_url
    assert bundle_a.dir_path != bundle_b.dir_path  # 不同 host 目录互异

    zip_a = Path(bundle_a.zip_path)
    assert zip_a.exists() and zip_a.suffix == ".zip"
    manifest = json.loads(
        Path(bundle_a.manifest_path).read_text(encoding="utf-8")
    )
    assert manifest["report"]["site_url"] == report_a.site_url
    files = manifest["files"]
    assert {f["role"] for f in files} == {"screenshot", "image"}
    assert all(len(f["sha256"]) == 64 for f in files)  # sha256 已逐文件登记
    with zipfile.ZipFile(zip_a) as zf:
        names = zf.namelist()
    assert any(n.endswith("manifest.json") for n in names)
    assert any(n.endswith("summary.md") for n in names)

    zip_b = Path(bundle_b.zip_path)  # 零证据站点照样成包(A11 语义)
    assert zip_b.exists()
    telemetry_snap = telemetry.snapshot()["counters"]
    assert telemetry_snap.get("parallel_pack.bundles", 0) >= 2  # 本批已计数


# ---------------------------------------------------------------------------
# A214:并行产物签名(复用 A205 packager.sign_bundle 公开口;默认零签名)
# ---------------------------------------------------------------------------
class _RealLayoutBuilder:
    """落盘 packager 约定布局(dir/manifest/zip 同父同名)但**不签名**的
    builder(模拟"注入的构建器绕过 A205 签名"的并行产物)。"""

    def __init__(self, root: Path, *, fail_keyword: str = "bad") -> None:
        self.root = root
        self.fail_keyword = fail_keyword
        self.calls = 0

    def __call__(self, report: SiteReport, cfg: Config) -> EvidenceBundle:
        self.calls += 1
        if self.fail_keyword in report.site_url:
            raise RuntimeError(f"故意失败:{report.site_url}")
        bdir = self.root / f"site_{self.calls:03d}"
        bdir.mkdir(parents=True, exist_ok=True)
        data = f"EVIDENCE-{report.site_url}".encode("utf-8")
        (bdir / "ev.png").write_bytes(data)
        manifest = {
            "report": {"site_url": report.site_url},
            "files": [
                {
                    "path": "ev.png",
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                    "role": "image",
                }
            ],
        }
        (bdir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        zip_path = self.root / f"{bdir.name}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in sorted(bdir.iterdir()):
                if item.is_file():
                    zf.write(item, f"{bdir.name}/{item.name}")
        return EvidenceBundle(
            site_url=report.site_url,
            dir_path=str(bdir),
            manifest_path=str(bdir / "manifest.json"),
            zip_path=str(zip_path),
        )


def _ed25519_cfg(tmp_path: Path) -> Config:
    """ed25519 显式配置(data_dir 隔离到 tmp,计数器/密钥不落项目目录)。"""
    cfg = make_cfg()
    cfg.data_dir = str(tmp_path / "data")
    cfg.bundle_sign_algo = "ed25519"
    cfg.ed25519_seed_hex = SEED_A.hex()
    return cfg


def test_a214_default_cfg_products_stay_unsigned_and_ordered(
    tmp_path: Path,
) -> None:
    """默认 hmac-sha256 配置 = 现状零签名:注入 builder 产物原样未签名、
    保序不变、零 bundle.sign.* / parallel_pack.sign_* 计数。"""
    telemetry.reset()
    try:
        builder = _RealLayoutBuilder(tmp_path / "e")
        reports = make_reports(3)
        results = pp.pack_all(reports, make_cfg(), builder=builder)

        assert [b.site_url for b in results] == [r.site_url for r in reports]  # 保序
        for bundle in results:
            manifest = json.loads(
                Path(bundle.manifest_path).read_text(encoding="utf-8")
            )
            assert "signature" not in manifest  # 现状零签名行为
        snap = telemetry.snapshot()["counters"]
        assert not [k for k in snap if k.startswith("bundle.sign.")]
        assert not [k for k in snap if k.startswith("parallel_pack.sign")]
    finally:
        telemetry.reset()


def test_a214_ed25519_policy_signs_injected_products_keeps_order_and_placeholders(
    tmp_path: Path,
) -> None:
    """ed25519 下注入 builder 的未签名产物补签:manifest 含签名块、独立验签
    通过、zip 同步刷新;混合失败批保序 + None 占位语义不变(签名绝不成
    None 占位)。"""
    telemetry.reset()
    try:
        builder = _RealLayoutBuilder(tmp_path / "e")
        reports = [
            make_report("https://ok-0.example.test/"),
            make_report("https://bad-1.example.test/"),  # 构建失败 → None 占位
            make_report("https://ok-2.example.test/"),
        ]
        results = pp.pack_all(reports, _ed25519_cfg(tmp_path), builder=builder)

        # 保序 + 失败占位语义回归(签名步绝不扰动)
        assert [b.site_url if b is not None else None for b in results] == [
            reports[0].site_url,
            None,
            reports[2].site_url,
        ]
        assert results[1] is None

        for bundle in results[0::2]:  # 两个成功包均已补签
            manifest = json.loads(
                Path(bundle.manifest_path).read_text(encoding="utf-8")
            )
            sig = manifest["signature"]
            assert sig["algo"] == "ed25519"
            assert sig["public_key"] == ed25519.public_key(SEED_A).hex()
            assert sig["files_hashed"] == 1
            ok, msg = BundleSigner(
                data_dir=str(tmp_path / "v"), algo="ed25519", ed25519_seed=SEED_B
            ).verify(bundle.dir_path)
            assert ok is True and msg.startswith("校验通过:1 个文件")
            # zip 已刷新:zip 内即已签名 manifest(目录与 zip 口径一致)
            with zipfile.ZipFile(bundle.zip_path) as zf:
                prefix = Path(bundle.dir_path).name
                zipped = json.loads(zf.read(f"{prefix}/manifest.json").decode())
            assert zipped["signature"] == sig

        snap = telemetry.snapshot()["counters"]
        assert snap["bundle.sign.ed25519"] == 2  # 两个成功包各补签一次
        assert snap.get("parallel_pack.failures") == 1.0  # 构建失败照常占位
        assert not [k for k in snap if k.startswith("parallel_pack.sign")]
    finally:
        telemetry.reset()


def test_a214_ed25519_real_builder_signed_once_no_churn(tmp_path: Path) -> None:
    """缺省 build_bundle 产物已在 A205 内 zip 前签名:本模块不重复签——
    签名块恰一份、目录与 zip 的 manifest 逐字节一致(零补签扰动)。"""
    pytest.importorskip("netsentinel.evidence.packager")
    telemetry.reset()
    try:
        cfg = _ed25519_cfg(tmp_path)
        cfg.evidence_dir = str(tmp_path / "evidence")
        results = pp.pack_all(make_reports(2), cfg)  # 缺省 builder

        assert all(b is not None for b in results)
        for bundle in results:
            on_disk = Path(bundle.manifest_path).read_bytes()
            with zipfile.ZipFile(bundle.zip_path) as zf:
                prefix = Path(bundle.dir_path).name
                in_zip = zf.read(f"{prefix}/manifest.json")
            assert in_zip == on_disk  # 无补签扰动:zip 内 manifest 与盘上逐字节一致
            manifest = json.loads(on_disk.decode("utf-8"))
            assert manifest["signature"]["algo"] == "ed25519"

        # 每包恰签名一次(build_bundle 内),补签步零重复
        assert telemetry.snapshot()["counters"]["bundle.sign.ed25519"] == 2
        assert "bundle.sign.skipped" not in telemetry.snapshot()["counters"]
    finally:
        telemetry.reset()


def test_a214_sign_failure_degrades_but_never_drops_bundles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """补签失败(签名器抛错):成功包原样保留(绝不成 None 占位)、保序、
    manifest 回滚未签名、telemetry 记 bundle.sign.skipped。"""
    monkeypatch.setattr(
        BundleSigner,
        "sign_manifest",
        lambda self, bundle_dir: (_ for _ in ()).throw(RuntimeError("模拟签名失败")),
    )
    telemetry.reset()
    try:
        builder = _RealLayoutBuilder(tmp_path / "e")
        reports = make_reports(3)
        results = pp.pack_all(reports, _ed25519_cfg(tmp_path), builder=builder)

        assert all(b is not None for b in results)  # 签名失败绝不丢包
        assert [b.site_url for b in results] == [r.site_url for r in reports]
        for bundle in results:
            manifest = json.loads(
                Path(bundle.manifest_path).read_text(encoding="utf-8")
            )
            assert "signature" not in manifest  # 回滚干净
        snap = telemetry.snapshot()["counters"]
        assert snap["bundle.sign.skipped"] == 3  # A205 单一实现内部计数
        assert snap.get("parallel_pack.bundles") == 3.0  # 成功包数不受影响
        assert not [k for k in snap if k.startswith("parallel_pack.sign")]
    finally:
        telemetry.reset()


# ---------------------------------------------------------------------------
# A224:补签步消费 packager 公开口(写口缺席降级路径回归)
# ---------------------------------------------------------------------------
def test_a224_resign_consumes_public_write_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """monkeypatch packager.sign_bundle/zip_bundle 为记录替身(透传真实现):
    补签步经**公开名**消费(每成功包一次)、zip 刷新同走公开口;
    V14(A235)起私有别名已移除,替身不被任何别名旁路。"""
    from netsentinel.evidence import packager as pkg

    sign_calls: list[str] = []
    zip_calls: list[str] = []
    real_sign, real_zip = pkg.sign_bundle, pkg.zip_bundle

    def _sign_spy(bundle_dir, manifest_path, cfg):
        sign_calls.append(str(manifest_path))
        return real_sign(bundle_dir, manifest_path, cfg)

    def _zip_spy(bundle_dir, zip_path):
        zip_calls.append(str(zip_path))
        return real_zip(bundle_dir, zip_path)

    monkeypatch.setattr(pkg, "sign_bundle", _sign_spy)
    monkeypatch.setattr(pkg, "zip_bundle", _zip_spy)
    telemetry.reset()
    try:
        builder = _RealLayoutBuilder(tmp_path / "e")
        results = pp.pack_all(make_reports(2), _ed25519_cfg(tmp_path), builder=builder)

        assert all(b is not None for b in results)
        assert len(sign_calls) == 2  # 每成功包恰一次(公开口)
        assert set(sign_calls) == {b.manifest_path for b in results}
        assert len(zip_calls) == 2  # zip 刷新同走公开口
        for bundle in results:
            manifest = json.loads(
                Path(bundle.manifest_path).read_text(encoding="utf-8")
            )
            assert manifest["signature"]["algo"] == "ed25519"
        # V14(A235):一代兼容别名已移除,替身无别名旁路
        assert not hasattr(pkg, "_sign_bundle")
        assert not hasattr(pkg, "_zip_bundle")
    finally:
        telemetry.reset()


def test_a224_public_write_ports_absent_degrades(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """极端并行态回归:packager 缺公开写口(部分写入)→ 中文告警 +
    parallel_pack.sign_skipped 按包计数,产物照常返回且保持未签名
    (降级路径与 A214 原口径一致,仅符号名换公开口)。"""
    caplog.set_level(logging.WARNING, logger="netsentinel.evidence.parallel_pack")
    from netsentinel.evidence import packager as pkg

    monkeypatch.delattr(pkg, "sign_bundle")
    monkeypatch.delattr(pkg, "zip_bundle")
    telemetry.reset()
    try:
        builder = _RealLayoutBuilder(tmp_path / "e")
        results = pp.pack_all(make_reports(2), _ed25519_cfg(tmp_path), builder=builder)

        assert all(b is not None for b in results)  # 绝不因签名丢包
        for bundle in results:
            manifest = json.loads(
                Path(bundle.manifest_path).read_text(encoding="utf-8")
            )
            assert "signature" not in manifest  # 保持未签名
        snap = telemetry.snapshot()["counters"]
        assert snap["parallel_pack.sign_skipped"] == 2  # 按包计数(两成功包)
        assert any("sign_bundle/zip_bundle 公开写口" in r.getMessage() for r in caplog.records)
    finally:
        telemetry.reset()
