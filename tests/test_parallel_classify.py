"""A166:netsentinel.vision.parallel_classify 单元测试(全离线 + 一例真进程)。

覆盖(契约 §2 A166 行 + 红线 35 / 37):

- 线程模式:保序 / 单条异常容错(0 分 + 中文 error)/ 空列表 / 注入
  fake executor 计数与不关闭;
- **峰值并发证明**:Barrier 计数法——假分类器等齐 workers 个并发才放行,
  若线程池实为串行则 Barrier 超时破裂 → error 分,断言全果即证真并行;
- 进程模式:经模块级纯函数 ``_proc_classify((name, path, url))`` 在子进程
  按名构造(红线 35:payload 恒为 tuple[str, str, str],**分类器实例绝不
  进 submit 参数**,源码级 + 运行时双重断言);保序重组(复用原始
  evidence 对象);子进程异常 → error dict 容错;注入 executor 被忽略;
  进程池构造失败 → 中文 RuntimeError;
- 红线 37:缺省线程池 / 进程池 workers ≤ 物理核数(全档 × reserve);
- 遥测:``classify.parallel`` 计时、``classify.errors`` 按失败条数累计;
- 真进程可用性:skin 分类器对 2 张 tmp PNG 真子进程打分(不可用环境 skip)。

并行态确定性:核数 monkeypatch 固定 8(mid→4 / low→2 / high reserve=1→7);
A164 ``ops.concurrency`` 双态(在场走委托 / 强制缺席走内置兜底)均覆盖。
"""
from __future__ import annotations

import inspect
import multiprocessing as mp
import os
import struct
import sys
import threading
import zlib
from concurrent.futures import Future
from pathlib import Path

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, ImageScore
from netsentinel.vision import parallel_classify as pc
from netsentinel.vision.stub_classifier import StubClassifier

#: 固定核数(autouse 夹具注入;三档期望:low 2 / mid 4 / high(reserve=1) 7)
CORES = 8


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """每用例隔离:核数探测固定 8、环境核数钩子清空。"""
    monkeypatch.setattr(os, "cpu_count", lambda: CORES)
    monkeypatch.delenv("NETSENTINEL_FAKE_CORES", raising=False)


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


def _ev(path: str, url: str | None = None) -> ImageEvidence:
    return ImageEvidence(
        path=path,
        url=url or f"https://img.example.test/{os.path.basename(path)}",
        source_page="https://example.test/page",
    )


def _evs(n: int, prefix: str = "img") -> list[ImageEvidence]:
    return [_ev(f"data/{prefix}_{i:03d}.jpg") for i in range(n)]


# -- 假执行器 / 假分类器 -----------------------------------------------------
class _RecordingExecutor:
    """同步假线程执行器:记录 submit 次数/任务,不自动关闭。"""

    def __init__(self) -> None:
        self.submit_calls = 0
        self.shutdown_calls = 0
        self.tasks: list[tuple[object, tuple]] = []

    def submit(self, fn, /, *args, **kwargs):
        self.submit_calls += 1
        self.tasks.append((fn, args))
        fut: Future = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001 - 同步执行,异常入 future
            fut.set_exception(exc)
        return fut

    def shutdown(self, wait: bool = True) -> None:
        self.shutdown_calls += 1


class _MapClassifier:
    """按 basename 尾部数字给分的假分类器(保序验证)。"""

    name = "map"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def classify(self, img: ImageEvidence) -> ImageScore:
        self.calls.append(img.path)
        k = int(os.path.basename(img.path).rsplit("_", 1)[1].split(".")[0])
        return ImageScore(image=img, model=self.name, nsfw_prob=k / 1000.0, scores={"k": k})


class _BoomClassifier:
    """basename 含 'bad' 时抛异常的假分类器(容错验证)。"""

    name = "boom"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def classify(self, img: ImageEvidence) -> ImageScore:
        self.calls.append(img.path)
        if "bad" in os.path.basename(img.path):
            raise RuntimeError(f"故意失败:{img.path}")
        return ImageScore(image=img, model=self.name, nsfw_prob=0.5, scores={})


def make_fake_pool_class() -> type:
    """造一个同步假 ProcessPoolExecutor 类(记录 payload、主进程内执行)。"""

    class FakeProcessPool:
        instances: list["FakeProcessPool"] = []

        def __init__(self, max_workers=None, mp_context=None, **kw) -> None:
            self.max_workers = max_workers
            self.mp_context = mp_context
            self.submitted: list[tuple[object, tuple]] = []
            self.shutdown_calls = 0
            self.shutdown_kwargs: dict = {}
            FakeProcessPool.instances.append(self)

        def submit(self, fn, /, *args):
            self.submitted.append((fn, args))
            fut: Future = Future()
            try:
                fut.set_result(fn(*args))
            except Exception as exc:  # noqa: BLE001
                fut.set_exception(exc)
            return fut

        def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
            self.shutdown_calls += 1
            self.shutdown_kwargs = {"wait": wait, "cancel_futures": cancel_futures}

    return FakeProcessPool


def _write_solid_png(path, rgb: tuple[int, int, int], w: int = 48, h: int = 48) -> str:
    """落盘一张纯色 8bit RGB PNG(filter 0;stdlib 可解,无需 Pillow)。"""
    raw = (b"\x00" + bytes(rgb) * w) * h

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    Path(path).write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    return str(path)


# ---------------------------------------------------------------------------
# 线程模式(默认)
# ---------------------------------------------------------------------------
def test_thread_mode_preserves_order_with_default_pool() -> None:
    """缺省线程池:12 条保序,概率与输入逐位对齐。"""
    clf = _MapClassifier()
    results = pc.classify_parallel(clf, _evs(12), make_cfg())
    assert [s.scores["k"] for s in results] == list(range(12))
    assert [s.nsfw_prob for s in results] == [k / 1000.0 for k in range(12)]
    assert all(s.model == "map" for s in results)
    assert all(s.image.path.startswith("data/img_") for s in results)


def test_thread_mode_injected_executor_preserves_order() -> None:
    """注入 fake executor:同样保序,且逐条都被 submit。"""
    rec = _RecordingExecutor()
    clf = _MapClassifier()
    evs = _evs(7, "inj")
    results = pc.classify_parallel(clf, evs, make_cfg(), executor=rec)
    assert [s.scores["k"] for s in results] == list(range(7))
    assert rec.submit_calls == 7


def test_thread_mode_single_item_exception_tolerated() -> None:
    """单条异常 → 该条 0 分 + 中文 error,其余条目与整批不受影响。"""
    clf = _BoomClassifier()
    evs = [
        _ev("data/ok_0.jpg"),
        _ev("data/bad_1.jpg"),
        _ev("data/ok_2.jpg"),
        _ev("data/bad_3.jpg"),
        _ev("data/ok_4.jpg"),
        _ev("data/ok_5.jpg"),
    ]
    results = pc.classify_parallel(clf, evs, make_cfg())
    assert len(results) == 6
    for i, s in enumerate(results):
        assert s.image is evs[i]  # 保序对位
        if i in (1, 3):
            assert s.nsfw_prob == 0.0
            assert "分类失败" in s.scores["error"]
            assert "故意失败" in s.scores["error"]
        else:
            assert s.nsfw_prob == 0.5
            assert "error" not in s.scores
    assert clf.calls  # 真实被调用过


def test_thread_mode_all_fail_still_full_length() -> None:
    """全部失败:仍返回等长列表,全部 0 分 + error(不抛出)。"""
    clf = _BoomClassifier()
    evs = [_ev(f"data/bad_{i}.jpg") for i in range(4)]
    results = pc.classify_parallel(clf, evs, make_cfg())
    assert len(results) == 4
    assert all(s.nsfw_prob == 0.0 for s in results)
    assert all("error" in s.scores for s in results)


def test_empty_evidence_list_returns_empty_both_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空列表 → [](线程/进程两态);且不建进程池。"""
    fake = make_fake_pool_class()
    monkeypatch.setattr(pc, "ProcessPoolExecutor", fake)
    assert pc.classify_parallel(_MapClassifier(), [], make_cfg()) == []
    assert pc.classify_parallel(
        _MapClassifier(), [], make_cfg(), use_processes=True
    ) == []
    assert fake.instances == []  # 空批短路,进程池根本未创建


def test_injected_executor_not_closed_and_counted() -> None:
    """注入执行器:submit 计数 = 条数;shutdown 不被调用(归属调用方)。"""
    rec = _RecordingExecutor()
    pc.classify_parallel(_MapClassifier(), _evs(5, "own"), make_cfg(), executor=rec)
    assert rec.submit_calls == 5
    assert rec.shutdown_calls == 0  # 不关闭
    rec2 = _RecordingExecutor()
    pc.classify_parallel(_MapClassifier(), [], make_cfg(), executor=rec2)
    assert rec2.submit_calls == 0  # 空批不提交


def test_error_telemetry_counts_failures() -> None:
    """classify.errors 按失败条数累计(3 条失败 → +3)。"""
    telemetry.reset()
    evs = [_ev(f"data/bad_{i}.jpg") for i in range(3)] + [
        _ev(f"data/ok_{i}.jpg") for i in range(4)
    ]
    pc.classify_parallel(_BoomClassifier(), evs, make_cfg())
    counters = telemetry.snapshot()["counters"]
    assert counters.get("classify.errors") == 3.0


def test_timer_telemetry_recorded() -> None:
    """整批计时入 classify.parallel。"""
    telemetry.reset()
    pc.classify_parallel(_MapClassifier(), _evs(3, "tm"), make_cfg())
    assert "classify.parallel" in telemetry.snapshot()["timers"]


def test_peak_concurrency_barrier_proof() -> None:
    """峰值并发证明:Barrier(workers)计数法。

    cores=8 / mid → workers=4;12 个任务的假分类器必须**等齐 4 个并发**
    才放行(Barrier 两轮复用)。若缺省线程池实为串行或并发 < 4,Barrier
    20s 超时破裂 → BrokenBarrierError → error 分;断言 12 条全果无 error,
    即证线程池真的按 workers 并行。
    """
    workers = pc._io_workers(make_cfg())
    assert workers == 4  # cores=8 → mid=4(前提自证)
    barrier = threading.Barrier(workers, timeout=20)

    class _BarrierClassifier:
        name = "barrier"

        def classify(self, img: ImageEvidence) -> ImageScore:
            barrier.wait()  # 等齐 workers 个并发才返回
            return ImageScore(image=img, model=self.name, nsfw_prob=0.25)

    evs = _evs(workers * 3, "peak")
    results = pc.classify_parallel(_BarrierClassifier(), evs, make_cfg())
    assert len(results) == workers * 3
    errors = [s.scores.get("error") for s in results if "error" in s.scores]
    assert errors == [], f"存在未达并发度而失败的条目:{errors}"
    assert all(s.nsfw_prob == 0.25 for s in results)


def test_fallback_thread_pool_when_a164_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A164 缺席:兜底 ThreadPoolExecutor(io_workers 内置公式,label=classify)。"""
    force_a164_absent(monkeypatch)
    created: list[dict] = []

    class _RecordingThreadPool:
        def __init__(self, max_workers=None, thread_name_prefix="", **kw) -> None:
            self.max_workers = max_workers
            created.append(
                {"max_workers": max_workers, "prefix": thread_name_prefix}
            )
            self._shutdown = False

        def submit(self, fn, /, *args):
            fut: Future = Future()
            fut.set_result(fn(*args))
            return fut

        def shutdown(self, wait: bool = True) -> None:
            self._shutdown = True

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.shutdown(wait=True)
            return False

    monkeypatch.setattr(pc, "ThreadPoolExecutor", _RecordingThreadPool)
    results = pc.classify_parallel(_MapClassifier(), _evs(3, "fb"), make_cfg())
    assert [s.scores["k"] for s in results] == [0, 1, 2]
    assert created == [{"max_workers": 4, "prefix": "classify"}]  # mid@cores8


def test_default_thread_pool_delegates_to_a164_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A164 在场:缺省线程池走 thread_pool(cfg, label="classify")。"""
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
    pc.classify_parallel(_MapClassifier(), _evs(4, "del"), cfg)
    assert captured == [{"label": "classify", "cfg": cfg}]


def test_invalid_tier_raises_chinese_valueerror_both_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非法档位 → 中文 ValueError(A164 在场透传 / 缺席兜底同判)。"""
    cfg = make_cfg()
    cfg.concurrency_tier = "bogus"
    with pytest.raises(ValueError, match="档位"):
        pc.classify_parallel(_MapClassifier(), _evs(2), cfg)
    force_a164_absent(monkeypatch)
    with pytest.raises(ValueError, match="未知并发档位"):
        pc.classify_parallel(_MapClassifier(), _evs(2), cfg)


def test_fallback_tier_workers_formula() -> None:
    """内置三档公式逐条(§1):cores 注入,确定性无环境依赖。"""
    f = pc._fallback_tier_workers
    assert f("low", cores=4) == 1  # max(1, 4//4)
    assert f("mid", cores=4) == 2  # 4//2
    assert f("high", reserve=1, cores=4) == 3  # 4-1
    assert f("high", reserve=0, cores=4) == 4  # 全核
    assert f("low", cores=1) == f("mid", cores=1) == f("high", reserve=0, cores=1) == 1
    assert f("low", cores=2) == 1  # 2//4=0 → 兜 1
    assert f("mid", cores=8) == 4
    with pytest.raises(ValueError, match="未知并发档位"):
        f("fast", cores=8)


# ---------------------------------------------------------------------------
# 红线 37:workers ≤ cores(缺省执行器,注入显式值除外)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tier,reserve", [("low", 1), ("mid", 1), ("high", 1), ("high", 0)])
def test_default_workers_never_exceed_cores(tier: str, reserve: int) -> None:
    """全档 × reserve:io_workers / cpu_workers 均 ≤ 物理核数(A164 在场)。"""
    cfg = make_cfg(tier, reserve)
    assert pc._io_workers(cfg) <= CORES
    assert pc._cpu_workers(cfg) <= CORES


@pytest.mark.parametrize("tier,reserve", [("low", 1), ("mid", 1), ("high", 1), ("high", 0)])
def test_default_workers_never_exceed_cores_fallback(
    monkeypatch: pytest.MonkeyPatch, tier: str, reserve: int
) -> None:
    """A164 缺席兜底同样 ≤ cores;进程池额外被 min(·, cores) 钳制。"""
    force_a164_absent(monkeypatch)
    cfg = make_cfg(tier, reserve)
    assert pc._fallback_tier_workers(tier, reserve=reserve, cores=CORES) <= CORES
    assert pc._cpu_workers(cfg) <= CORES
    # 高档 reserve=0 恰好等于全核(压榨上限,不越界)
    if (tier, reserve) == ("high", 0):
        assert pc._cpu_workers(cfg) == CORES


def test_max_batch_guard() -> None:
    """单批总量上限(红线 37):超过 MAX_BATCH → 中文 ValueError。"""
    huge = _evs(pc.MAX_BATCH + 1, "huge")
    with pytest.raises(ValueError, match="超过上限"):
        pc.classify_parallel(_MapClassifier(), huge, make_cfg())


# ---------------------------------------------------------------------------
# 进程模式(红线 35:不 pickle 实例;payload 恒 tuple[str, str, str])
# ---------------------------------------------------------------------------
def test_proc_payload_is_plain_string_triple_and_source_never_submits_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """红线 35 双重断言:运行时 payload 全为 (str, str, str);源码层
    submit 只提交 ``_proc_classify, payload``,分类器实例绝不入参。"""
    fake = make_fake_pool_class()
    monkeypatch.setattr(pc, "ProcessPoolExecutor", fake)
    clf = _MapClassifier()  # 实例:断言它从未被送进 submit
    evs = _evs(4, "pp")
    pc.classify_parallel(clf, evs, make_cfg(), use_processes=True)
    pool = fake.instances[0]
    assert len(pool.submitted) == 4
    for fn, args in pool.submitted:
        assert fn is pc._proc_classify  # 任务是模块级纯函数
        assert len(args) == 1
        payload = args[0]
        assert isinstance(payload, tuple)
        assert len(payload) == 3
        assert all(isinstance(x, str) for x in payload)  # 纯字符串三元组
        assert clf not in payload and clf is not payload
        assert clf not in args
    # 源码断言:进程路径唯一 submit 形态为 submit(_proc_classify, payload)
    src = inspect.getsource(pc._classify_processes)
    assert "pool.submit(_proc_classify, payload)" in src
    assert "submit(classifier" not in src
    # 契约签名:payload 注解为 tuple[str, str, str]
    ann = inspect.signature(pc._proc_classify).parameters["payload"].annotation
    assert str(ann) == "tuple[str, str, str]"


def test_process_mode_preserves_order_and_reuses_original_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """进程模式保序;重组时 image 复用主进程原始 evidence 对象;
    用完必 shutdown(cancel_futures,红线 37 不留排队任务)。"""
    fake = make_fake_pool_class()
    monkeypatch.setattr(pc, "ProcessPoolExecutor", fake)
    paths = [
        "data/a_nsfw_hi.png",
        "data/b_clean.png",
        "data/c_nsfw_mid.png",
        "data/d_clean.png",
        "data/e_nsfw_hi.png",
    ]
    evs = [_ev(p) for p in paths]
    results = pc.classify_parallel(
        StubClassifier(make_cfg()), evs, make_cfg(), use_processes=True
    )
    expected = [0.97, 0.02, 0.72, 0.02, 0.97]  # stub 按文件名关键词
    assert [s.nsfw_prob for s in results] == expected
    assert [s.image.path for s in results] == paths
    for i, s in enumerate(results):
        assert s.image is evs[i]  # 原始对象(含 source_page 等完整字段)
        assert s.model == "stub"
    pool = fake.instances[0]
    assert pool.shutdown_calls == 1
    assert pool.shutdown_kwargs.get("cancel_futures") is True


def test_process_mode_ignores_injected_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """use_processes=True:注入的 executor 被完全忽略(契约语义)。"""
    fake = make_fake_pool_class()
    monkeypatch.setattr(pc, "ProcessPoolExecutor", fake)
    rec = _RecordingExecutor()
    pc.classify_parallel(
        _MapClassifier(), _evs(3, "ig"), make_cfg(), executor=rec, use_processes=True
    )
    assert rec.submit_calls == 0  # 线程执行器未被使用
    assert len(fake.instances[0].submitted) == 3  # 进程池承担全部任务


def test_process_mode_pool_construction_failure_chinese_runtimeerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """构造失败(无守卫环境等)→ 中文 RuntimeError,原异常保留 __cause__。"""
    def boom(*args, **kwargs):
        raise OSError("子进程反复导入 __main__ 失败")

    monkeypatch.setattr(pc, "ProcessPoolExecutor", boom)
    with pytest.raises(RuntimeError, match="进程池") as excinfo:
        pc.classify_parallel(
            _MapClassifier(), _evs(2), make_cfg(), use_processes=True
        )
    assert "if __name__" in str(excinfo.value)  # 守卫修复提示
    assert isinstance(excinfo.value.__cause__, OSError)  # 不吞原异常


def test_process_mode_subprocess_error_dict_tolerated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子进程按名构造失败 → error dict 容错为 0 分 + 中文 error,逐条计数。"""
    fake = make_fake_pool_class()
    monkeypatch.setattr(pc, "ProcessPoolExecutor", fake)

    class _MissingClassifier:
        name = "__definitely_missing__"

    telemetry.reset()
    evs = _evs(2, "miss")
    results = pc.classify_parallel(
        _MissingClassifier(), evs, make_cfg(), use_processes=True
    )
    assert len(results) == 2
    for i, s in enumerate(results):
        assert s.image is evs[i]
        assert s.nsfw_prob == 0.0
        assert "子进程分类失败" in s.scores["error"]
    assert telemetry.snapshot()["counters"].get("classify.errors") == 2.0


def test_process_mode_workers_wired_to_cpu_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """进程池 max_workers = cpu_workers(cfg)(红线 37 接线):high/reserve=0
    → 全核 8;mid → 4;且各档 ≤ cores。"""
    fake = make_fake_pool_class()
    monkeypatch.setattr(pc, "ProcessPoolExecutor", fake)
    for tier, reserve, expected in [("high", 0, 8), ("mid", 1, 4), ("low", 1, 2)]:
        cfg = make_cfg(tier, reserve)
        pc.classify_parallel(
            StubClassifier(cfg), _evs(1, f"w{tier}"), cfg, use_processes=True
        )
        pool = fake.instances[-1]
        assert pool.max_workers == pc._cpu_workers(cfg) == expected
        assert pool.max_workers <= CORES


def test_proc_classify_returns_plain_dict_shape() -> None:
    """纯函数直连:stub 名 → dict{model, nsfw_prob, scores, path, url}。"""
    data = pc._proc_classify(("stub", "img/a_nsfw_hi.png", "https://x/y.png"))
    assert isinstance(data, dict) and "error" not in data
    assert data["model"] == "stub"
    assert data["nsfw_prob"] == 0.97
    assert data["path"] == "img/a_nsfw_hi.png"
    assert data["url"] == "https://x/y.png"
    assert isinstance(data["scores"], dict)


def test_proc_classify_bad_name_returns_error_dict() -> None:
    """纯函数直连:按名构造失败 → 不抛,返回 error dict(中文)。"""
    data = pc._proc_classify(("__definitely_missing__", "p.png", "u"))
    assert "error" in data
    assert "子进程分类失败" in data["error"]
    assert data["path"] == "p.png" and data["url"] == "u"


def test_process_mode_real_skin_two_pngs_spawn(tmp_path: Path) -> None:
    """真进程可用性(显式 spawn 语义):skin 分类器对 2 张 tmp PNG 打分。

    - 不 pickle 实例:主进程实例仅取 ``name``;子进程按名重建(红线 35);
    - 不可用环境(进程池创建失败等)→ skip 而非 fail;
    - 纯肤色图分 > 纯风景(蓝)图分,顺序与输入对齐。
    """
    skin_path = _write_solid_png(tmp_path / "solid_skin.png", (230, 170, 150))
    blue_path = _write_solid_png(tmp_path / "solid_blue.png", (30, 60, 200))
    evs = [_ev(skin_path), _ev(blue_path)]
    cfg = make_cfg()  # cores=8 → cpu_workers=4
    assert cfg.classifier == "stub"
    try:
        from netsentinel.vision.heuristic_kernel import SkinHeuristicClassifier

        clf = SkinHeuristicClassifier(cfg)
    except Exception as exc:  # noqa: BLE001 - 依赖缺席按不可用环境跳过
        pytest.skip(f"skin 分类器不可用:{exc}")
    try:
        results = pc.classify_parallel(clf, evs, cfg, use_processes=True)
    except RuntimeError as exc:
        pytest.skip(f"真进程池不可用(环境限制):{exc}")
    assert len(results) == 2
    assert [s.image.path for s in results] == [skin_path, blue_path]  # 保序
    for s in results:
        assert s.model == "skin"
        assert "error" not in s.scores
        assert 0.0 <= s.nsfw_prob <= 1.0
    assert results[0].nsfw_prob > results[1].nsfw_prob  # 肤色图 > 蓝图
