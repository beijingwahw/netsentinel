"""A05 单元测试:netsentinel.vision.classifier_base / stub_classifier(离线)。

覆盖点:
- stub 规则三分支(nsfw_hi / nsfw_mid / 其他);
- classify_batch 的长度与顺序保持;
- register_classifier / get_classifier 往返;
- 未知名抛 ValueError(中文消息列出已注册名);
- 重复注册记录 warning 且 get 返回新(覆盖后)类的实例。

V5 新增(test_v5_* 前缀):注册表并发读写安全、get_classifier 失败遥测计数、
惰性导入失败记忆(每进程只尝试一次)、stub.classify 遥测计数。
"""
from __future__ import annotations

import importlib
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, ImageScore
from netsentinel.vision import classifier_base as cb
from netsentinel.vision.classifier_base import (
    NsfwClassifier,
    get_classifier,
    register_classifier,
)
from netsentinel.vision.stub_classifier import StubClassifier


def _evidence(filename: str) -> ImageEvidence:
    """构造 ImageEvidence;桩分类器只读路径字符串,无需真实文件。"""
    return ImageEvidence(
        path=f"data/img/{filename}",
        url=f"https://example.invalid/{filename}",
        source_page="https://example.invalid/index.html",
    )


@pytest.fixture(autouse=True)
def _restore_registry():
    """快照/恢复模块级注册表,避免测试内注册互相污染或影响其他测试模块。"""
    saved = dict(cb._REGISTRY)
    yield
    cb._REGISTRY.clear()
    cb._REGISTRY.update(saved)


# ---------------------------------------------------------------------------
# 规则三分支
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("filename", "expected_prob", "expected_hint"),
    [
        ("page1_NSFW_HI_a.jpg", 0.97, "nsfw_hi"),
        ("photo_nsfw_hi.jpg", 0.97, "nsfw_hi"),
        ("thumb_NSFW_MID_b.png", 0.72, "nsfw_mid"),
        ("hero_nsfw_mid.webp", 0.72, "nsfw_mid"),
        ("logo.png", 0.02, "none"),
        ("avatar.JPG", 0.02, "none"),
    ],
    ids=["hi-upper", "hi", "mid-upper", "mid", "plain", "plain-upper-ext"],
)
def test_stub_rule_branches(filename: str, expected_prob: float, expected_hint: str) -> None:
    stub = StubClassifier()
    img = _evidence(filename)
    score = stub.classify(img)
    assert isinstance(score, ImageScore)
    assert score.image is img
    assert score.model == "stub"
    assert score.nsfw_prob == pytest.approx(expected_prob)
    assert score.scores == {"hint": expected_hint}


def test_stub_hi_takes_priority_in_order() -> None:
    """nsfw_hi 与 nsfw_mid 同时出现时按 hi 分支(先命中先返回)。"""
    score = StubClassifier().classify(_evidence("both_nsfw_hi_and_nsfw_mid.jpg"))
    assert score.nsfw_prob == pytest.approx(0.97)
    assert score.scores["hint"] == "nsfw_hi"


# ---------------------------------------------------------------------------
# classify_batch
# ---------------------------------------------------------------------------

def test_classify_batch_length_and_order() -> None:
    stub = StubClassifier()
    imgs = [
        _evidence("01_nsfw_hi.jpg"),
        _evidence("02_plain.jpg"),
        _evidence("03_nsfw_mid.jpg"),
        _evidence("04_plain.png"),
    ]
    scores = stub.classify_batch(imgs)
    assert len(scores) == len(imgs)
    # 顺序与输入一一对应
    assert [s.image.path for s in scores] == [i.path for i in imgs]
    assert [s.nsfw_prob for s in scores] == pytest.approx([0.97, 0.02, 0.72, 0.02])
    # 默认实现等价于逐张 classify
    assert scores == [stub.classify(img) for img in imgs]


def test_classify_batch_empty() -> None:
    assert StubClassifier().classify_batch([]) == []


# ---------------------------------------------------------------------------
# 注册表与工厂
# ---------------------------------------------------------------------------

class _DemoClassifier(NsfwClassifier):
    """用于往返验证的最小实现。"""

    name = "_a05_demo"

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg

    def classify(self, img: ImageEvidence) -> ImageScore:
        return ImageScore(image=img, model=self.name, scores={"demo": True}, nsfw_prob=0.5)


def test_register_get_roundtrip() -> None:
    cfg = Config()
    register_classifier("_a05_demo", _DemoClassifier)
    inst = get_classifier("_a05_demo", cfg)
    assert type(inst) is _DemoClassifier
    assert isinstance(inst, NsfwClassifier)
    assert inst.cfg is cfg
    assert get_classifier("_a05_demo", cfg) is not inst  # 每次返回新实例
    score = inst.classify(_evidence("x.jpg"))
    assert score.model == "_a05_demo"
    assert score.nsfw_prob == pytest.approx(0.5)


def test_get_stub_via_factory() -> None:
    inst = get_classifier("stub", Config())
    assert isinstance(inst, StubClassifier)
    assert inst.classify(_evidence("a_nsfw_hi.jpg")).nsfw_prob == pytest.approx(0.97)


def test_get_stub_via_lazy_import() -> None:
    """注册表与 sys.modules 均无 stub 时,工厂应惰性导入 stub_classifier 完成自注册。

    注:importlib 对已导入模块有缓存,只有模块首次导入才会执行其模块级注册,
    因此这里同时从 sys.modules 移除以模拟"尚未导入"的真实惰性场景。
    """
    cb._REGISTRY.pop("stub", None)
    saved_module = sys.modules.pop("netsentinel.vision.stub_classifier", None)
    try:
        inst = get_classifier("stub", Config())
        assert isinstance(inst, NsfwClassifier)
        assert inst.name == "stub"
        assert inst.classify(_evidence("a_nsfw_hi.jpg")).nsfw_prob == pytest.approx(0.97)
    finally:
        if saved_module is not None:
            sys.modules["netsentinel.vision.stub_classifier"] = saved_module


def test_unknown_name_raises_valueerror() -> None:
    with pytest.raises(ValueError) as excinfo:
        get_classifier("no_such_classifier", Config())
    message = str(excinfo.value)
    assert "no_such_classifier" in message
    assert "stub" in message  # 中文消息应列出当前已注册名称


def test_lazy_adapter_import_or_valueerror() -> None:
    """nudenet/clip:适配器模块就位则惰性导入触发注册,未就位则 ValueError。"""
    cfg = Config()
    for name, module_path in cb._LAZY_IMPORT_MODULES.items():
        if name == "stub":
            continue
        try:
            importlib.import_module(module_path)
        except ImportError:
            with pytest.raises(ValueError, match=name):
                get_classifier(name, cfg)
        else:
            assert name in cb._REGISTRY


# ---------------------------------------------------------------------------
# 重复注册与输入校验
# ---------------------------------------------------------------------------

class _DupV1(NsfwClassifier):
    name = "_a05_dup"

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg

    def classify(self, img: ImageEvidence) -> ImageScore:
        return ImageScore(image=img, model=self.name, nsfw_prob=0.1)


class _DupV2(_DupV1):
    pass


def test_duplicate_registration_warns_and_overrides(caplog) -> None:
    register_classifier("_a05_dup", _DupV1)
    with caplog.at_level(logging.WARNING):
        register_classifier("_a05_dup", _DupV2)
    assert any(
        "_a05_dup" in record.getMessage() and "覆盖" in record.getMessage()
        for record in caplog.records
    )
    # get 返回覆盖后新类的实例
    inst = get_classifier("_a05_dup", Config())
    assert type(inst) is _DupV2
    assert isinstance(inst, _DupV1)


def test_register_classifier_validates_input() -> None:
    with pytest.raises(ValueError):
        register_classifier("", _DemoClassifier)  # 名称为空
    with pytest.raises(ValueError):
        register_classifier("_a05_bad", dict)  # type: ignore[arg-type]  # 不是子类

    class _Unnamed(NsfwClassifier):
        def classify(self, img: ImageEvidence) -> ImageScore:
            return ImageScore(image=img, model="x")

    with pytest.raises(ValueError):
        register_classifier("_a05_unnamed", _Unnamed)  # 缺少非空 name


def test_base_class_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        NsfwClassifier()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# V5 升级:并发安全(注册表锁)
# ---------------------------------------------------------------------------

_V5_WORKERS = 8
_V5_OPS_PER_WORKER = 25


class _V5ThreadA(NsfwClassifier):
    name = "_v5_thread_a"

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg

    def classify(self, img: ImageEvidence) -> ImageScore:
        return ImageScore(image=img, model=self.name, nsfw_prob=0.1)


class _V5ThreadB(_V5ThreadA):
    name = "_v5_thread_b"


def _run_concurrently(fn, workers: int = _V5_WORKERS) -> list:
    """用栅栏同时放行多个线程执行 fn(worker_id),收集返回值/异常。"""
    barrier = threading.Barrier(workers)

    def wrapped(worker_id: int):
        barrier.wait(timeout=10.0)  # 最大化同时起跑的交错概率
        return fn(worker_id)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(wrapped, i) for i in range(workers)]
        return [fut.result(timeout=30.0) for fut in futures]


def test_v5_concurrent_register_distinct_names_thread_safe() -> None:
    """多线程并发注册互不覆盖:全部写入可见,注册过程零异常。"""

    def register_many(worker_id: int) -> int:
        count = 0
        for i in range(_V5_OPS_PER_WORKER):
            register_classifier(f"_v5_reg_{worker_id}_{i}", _V5ThreadA)
            count += 1
        return count

    results = _run_concurrently(register_many)
    assert results == [_V5_OPS_PER_WORKER] * _V5_WORKERS
    # 无丢失、无覆盖:200 个名字全部可解析为合法实例。
    for worker_id in range(_V5_WORKERS):
        for i in range(_V5_OPS_PER_WORKER):
            assert isinstance(
                get_classifier(f"_v5_reg_{worker_id}_{i}", Config()), _V5ThreadA
            )


def test_v5_concurrent_register_same_name_no_corruption() -> None:
    """多线程竞写同一注册名:最终恰好一个生效类,读取方拿到的一定是二者之一。"""

    def overwrite(worker_id: int) -> None:
        cls = _V5ThreadA if worker_id % 2 == 0 else _V5ThreadB
        for _ in range(_V5_OPS_PER_WORKER):
            register_classifier("_v5_contended", cls)

    _run_concurrently(overwrite)
    inst = get_classifier("_v5_contended", Config())
    assert type(inst) in (_V5ThreadA, _V5ThreadB)


def test_v5_concurrent_get_classifier_thread_safe() -> None:
    """多线程并发 get:全部返回合法 stub 实例,且每次调用都是新实例。"""

    def get_many(_: int) -> int:
        kept: list[object] = []  # 持强引用,防止对象回收后 id() 地址复用
        for _ in range(_V5_OPS_PER_WORKER):
            inst = get_classifier("stub", Config())
            assert isinstance(inst, StubClassifier)
            kept.append(inst)
        return len({id(o) for o in kept})

    results = _run_concurrently(get_many)
    assert results == [_V5_OPS_PER_WORKER] * _V5_WORKERS  # 无一复用实例


def test_v5_concurrent_mixed_register_and_get() -> None:
    """写者不断注册新名,读者不断取既有名:读者全程零异常、结果合法。"""
    reader_failures: list[BaseException] = []

    def writer(worker_id: int) -> int:
        for i in range(_V5_OPS_PER_WORKER):
            register_classifier(f"_v5_mix_{worker_id}_{i}", _V5ThreadA)
        return _V5_OPS_PER_WORKER

    def reader(worker_id: int) -> int:
        del worker_id
        for _ in range(_V5_OPS_PER_WORKER):
            try:
                inst = get_classifier("stub", Config())
                assert isinstance(inst, StubClassifier)
            except BaseException as exc:  # noqa: BLE001 - 收集后统一断言
                reader_failures.append(exc)
        return _V5_OPS_PER_WORKER

    barrier = threading.Barrier(_V5_WORKERS)
    half = _V5_WORKERS // 2

    def wrapped(worker_id: int) -> int:
        barrier.wait(timeout=10.0)
        return writer(worker_id) if worker_id % 2 == 0 else reader(worker_id)

    with ThreadPoolExecutor(max_workers=_V5_WORKERS) as pool:
        futures = [pool.submit(wrapped, i) for i in range(_V5_WORKERS)]
        assert [fut.result(timeout=30.0) for fut in futures] == [
            _V5_OPS_PER_WORKER
        ] * _V5_WORKERS
    assert half >= 1 and reader_failures == []


# ---------------------------------------------------------------------------
# V5 升级:可观测性(classifier.miss / stub.classify)
# ---------------------------------------------------------------------------

def _counter(name: str) -> float:
    return telemetry.snapshot()["counters"].get(name, 0.0)


def test_v5_miss_counter_on_unknown_name() -> None:
    """未知名的失败路径累计 classifier.miss;成功路径不累计。"""
    before = _counter("classifier.miss")
    inst = get_classifier("stub", Config())  # 命中注册表:不应计数
    assert isinstance(inst, StubClassifier)
    assert _counter("classifier.miss") == before

    with pytest.raises(ValueError):
        get_classifier("no_such_v5_classifier", Config())  # 转交后仍 ValueError
    assert _counter("classifier.miss") == before + 1.0


def test_v5_miss_counter_when_gateway_module_absent(monkeypatch) -> None:
    """multi_provider 未就位(导入失败)分支:同样计入 classifier.miss。"""
    monkeypatch.setitem(sys.modules, "netsentinel.vision.multi_provider", None)
    monkeypatch.delattr(
        __import__("netsentinel.vision", fromlist=["multi_provider"]),
        "multi_provider",
        raising=False,
    )
    before = _counter("classifier.miss")
    with pytest.raises(ValueError, match="no_such_v5_gw"):
        get_classifier("no_such_v5_gw", Config())
    assert _counter("classifier.miss") == before + 1.0


def test_v5_stub_classify_counter() -> None:
    """stub.classify 逐次计数:classify +1,classify_batch 按张数累计。"""
    stub = StubClassifier()
    before = _counter("stub.classify")
    stub.classify(_evidence("v5_plain.jpg"))
    assert _counter("stub.classify") == before + 1.0

    stub.classify_batch([_evidence(f"v5_{i}.jpg") for i in range(3)])
    assert _counter("stub.classify") == before + 4.0


# ---------------------------------------------------------------------------
# V5 升级:性能(惰性导入失败记忆——每进程只尝试一次)
# ---------------------------------------------------------------------------

def test_v5_lazy_import_failure_memoized(monkeypatch) -> None:
    """适配器模块导入失败后记忆:第二次 miss 不再重试 importlib,仍走转交报错。"""
    module_path = cb._LAZY_IMPORT_MODULES["nudenet"]
    monkeypatch.setitem(sys.modules, module_path, None)  # 强制 ImportError
    cb._REGISTRY.pop("nudenet", None)  # 模拟"尚未导入注册"(autouse fixture 会恢复)
    calls: list[str] = []
    real_import = importlib.import_module

    def counting_import(module_name: str):
        calls.append(module_name)
        return real_import(module_name)

    monkeypatch.setattr(importlib, "import_module", counting_import)
    try:
        with pytest.raises(ValueError):
            cb.get_classifier("nudenet", Config())
        assert module_path in calls  # 首次 miss 确实尝试了 importlib
        assert module_path in cb._LAZY_IMPORT_FAILED  # 失败已被记忆

        calls.clear()
        with pytest.raises(ValueError):
            cb.get_classifier("nudenet", Config())
        assert module_path not in calls  # 第二次不再重试 importlib
    finally:
        cb._LAZY_IMPORT_FAILED.discard(module_path)  # 不污染后续测试


def test_v5_successful_lazy_import_not_memoized_as_failure() -> None:
    """导入成功路径不写失败记忆:stub 的注册表+sys.modules 双摘除后仍可惰性恢复。

    注:重新导入会生成新的 StubClassifier 类对象,故按既有用例口径断言
    NsfwClassifier + name(与 test_get_stub_via_lazy_import 一致)。
    """
    cb._REGISTRY.pop("stub", None)
    saved_module = sys.modules.pop("netsentinel.vision.stub_classifier", None)
    try:
        inst = get_classifier("stub", Config())
        assert isinstance(inst, NsfwClassifier)
        assert inst.name == "stub"
        assert inst.classify(_evidence("a_nsfw_hi.jpg")).nsfw_prob == pytest.approx(0.97)
        assert "netsentinel.vision.stub_classifier" not in cb._LAZY_IMPORT_FAILED
    finally:
        if saved_module is not None:
            sys.modules["netsentinel.vision.stub_classifier"] = saved_module
