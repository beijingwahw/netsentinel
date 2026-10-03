"""分布式追踪内核测试(netsentinel.telemetry_trace,A198;W3C 风格零依赖追踪)。

覆盖::

- id 形态:trace_id 16 hex / span_id 8 hex,默认工厂互异;
- 嵌套 span 树:parent 链、中文 name 保留、``export_trace_json`` 结构
  (根/children/时间戳/attrs)与 JSON 可序列化;
- 异常 span:记录 ``"类型: 消息"`` 后**原样重抛**;timer 双写照常;
- 落点双写:``telemetry.observe`` 同名 timer + ``trace.span`` 计数;
  audit_sink 七键载荷精确(含 error/parent)、抛异常安全吞掉并计数、
  默认无 sink 零副作用;
- ``@traced`` 装饰器:透传返回值/保留函数名/异常同样重抛;
- 并发隔离:每线程 contextvars 各自独立 trace_id(显式 new_trace 与
  隐式根 trace 两种形态);
- 线程边界:``propagate``/``restore`` 快照对接,子线程 span 挂回同一
  trace 树、parent 为快照时刻的 span;restore 不影响主线程上下文;
- 确定性:id 工厂注入固定序列 → trace/span id 逐字符可预期;
- OTel 桥接:缺席(默认)探测 None 且缓存、import 失败零影响;
  在场(伪 tracer 注入)时开/关/异常同步;桥接自身异常安全失效;
- ``configure`` 类型校验、``reset`` 清注册表与探测缓存、
  ``MAX_TRACES`` 逐出与 ``MAX_SPANS_PER_TRACE`` 截断;
- 红线:源码全部 import 仅标准库 + netsentinel(无 opentelemetry /
  logging_util 任何形式的 import 语句)。

全部测试离线,不发起网络请求,不依赖墙钟时长(只断言 >= 0)。
"""
from __future__ import annotations

import ast
import inspect
import json
import re
import sys
import threading
import types
from pathlib import Path

import pytest

from netsentinel import telemetry
from netsentinel import telemetry_trace as tt


@pytest.fixture(autouse=True)
def _isolate():
    """测试隔离:清 telemetry 指标、清 trace 注册表/OTel 缓存、还原默认配置。"""
    telemetry.reset()
    tt.reset()
    tt.configure()  # 回默认:audit_sink=None(零副作用)+ 默认 id 工厂
    yield
    tt.reset()
    tt.configure()


# ---------------------------------------------------------------------------
# id 形态与默认工厂
# ---------------------------------------------------------------------------
def test_default_id_shapes_and_uniqueness():
    """默认工厂:trace_id 恒 16 hex、span_id 恒 8 hex,且各自互异。"""
    trace_ids = []
    span_ids = []
    for _ in range(4):
        with tt.new_trace() as tid:
            trace_ids.append(tid)
            with tt.span("形态.段") as rec:
                span_ids.append(rec.span_id)
    assert all(re.fullmatch(r"[0-9a-f]{16}", t) for t in trace_ids)
    assert all(re.fullmatch(r"[0-9a-f]{8}", s) for s in span_ids)
    assert len(set(trace_ids)) == 4 and len(set(span_ids)) == 4


# ---------------------------------------------------------------------------
# 嵌套 span 树与导出
# ---------------------------------------------------------------------------
def test_nested_span_tree_parent_chain_and_export():
    """三层嵌套:parent 链逐级正确,导出为嵌套树(中文 name/时间戳保留)。"""
    with tt.new_trace() as tid:
        with tt.span("扫描.抓取", attrs={"stage": "fetch"}) as root:
            with tt.span("抓取.翻页") as mid:
                with tt.span("抓取.解析") as leaf:
                    pass
        tree = tt.export_trace_json()

    assert tree["trace_id"] == tid
    assert isinstance(tree["started_ts"], str) and "T" in tree["started_ts"]
    assert len(tree["spans"]) == 1, "同 trace 内首个 span 应为唯一根"
    r = tree["spans"][0]
    assert r["name"] == "扫描.抓取"
    assert r["span_id"] == root.span_id
    assert r["parent_span_id"] is None
    assert r["attrs"] == {"stage": "fetch"}
    assert r["duration_ms"] is not None and r["duration_ms"] >= 0.0
    assert r["error"] is None
    assert len(r["children"]) == 1
    m = r["children"][0]
    assert m["name"] == "抓取.翻页" and m["span_id"] == mid.span_id
    assert m["parent_span_id"] == root.span_id
    assert len(m["children"]) == 1
    lf = m["children"][0]
    assert lf["name"] == "抓取.解析" and lf["span_id"] == leaf.span_id
    assert lf["parent_span_id"] == mid.span_id and lf["children"] == []
    # 时间戳:ISO 形态且根不晚于叶
    assert lf["start_ts"] >= r["start_ts"]


def test_sequential_spans_are_siblings_under_same_trace():
    """同一 trace 内的并列 span:各自为根,互为兄弟,不串父子。"""
    with tt.new_trace() as tid:
        with tt.span("并行段.a"):
            pass
        with tt.span("并行段.b"):
            pass
    tree = tt.export_trace_json(tid)
    assert [s["name"] for s in tree["spans"]] == ["并行段.a", "并行段.b"]
    assert all(s["parent_span_id"] is None for s in tree["spans"])
    assert all(s["children"] == [] for s in tree["spans"])


def test_export_json_serializable_and_chinese_preserved():
    """导出树可直接 json.dumps;中文名 ensure_ascii=False 原样保留。"""
    with tt.new_trace():
        with tt.span("扫描.融合", attrs={"stage": "fusion", "attempt": 2}):
            with tt.span("融合.仲裁"):
                pass
        tree = tt.export_trace_json()  # trace 上下文内导出当前树
    text = json.dumps(tree, ensure_ascii=False)
    assert "扫描.融合" in text and "融合.仲裁" in text
    assert json.loads(text) == tree
    node_keys = set(tree["spans"][0])
    assert node_keys == {
        "name",
        "span_id",
        "parent_span_id",
        "start_ts",
        "duration_ms",
        "error",
        "attrs",
        "children",
    }


def test_export_empty_without_trace_or_unknown_id():
    """无当前 trace / 未知 trace_id:返回空树结构而非报错。"""
    assert tt.current_trace_id() is None and tt.current_span_id() is None
    assert tt.export_trace_json() == {
        "trace_id": None,
        "started_ts": None,
        "spans": [],
    }
    unknown = tt.export_trace_json("deadbeefdeadbeef")
    assert unknown == {"trace_id": "deadbeefdeadbeef", "started_ts": None, "spans": []}


# ---------------------------------------------------------------------------
# 异常 span:记录后重抛
# ---------------------------------------------------------------------------
def test_span_error_recorded_and_reraised():
    """体内异常:span 记 error 且向上原样重抛;timer 双写仍发生。"""
    telemetry.reset()
    with pytest.raises(RuntimeError, match="数据源失败"):
        with tt.span("失败.段"):
            raise RuntimeError("数据源失败")

    nodes = [n for n in _all_nodes() if n["name"] == "失败.段"]
    assert len(nodes) == 1
    assert nodes[0]["error"] == "RuntimeError: 数据源失败"
    assert nodes[0]["duration_ms"] >= 0.0
    timers = telemetry.snapshot()["timers"]
    assert timers["失败.段"]["count"] == 1
    assert telemetry.snapshot()["counters"]["trace.span.error"] == 1


# ---------------------------------------------------------------------------
# 落点双写:telemetry timer + audit_sink
# ---------------------------------------------------------------------------
def test_telemetry_dual_write_observe_and_counters():
    """span 结束写同名 timer(秒样本)并计 trace.span。"""
    telemetry.reset()
    with tt.new_trace():
        with tt.span("双写.计时"):
            pass
        with tt.span("双写.计时"):
            pass
    stats = telemetry.snapshot()["timers"]["双写.计时"]
    assert stats["count"] == 2 and stats["avg_ms"] >= 0.0
    assert telemetry.snapshot()["counters"]["trace.span"] == 2


def test_audit_sink_payload_exact_keys_and_values():
    """audit_sink 收到恰七键载荷;确定性 id 工厂下取值逐字段可预期。"""
    seq = iter(range(1, 64))

    def fixed(nbytes: int) -> str:
        return f"{next(seq):0{2 * nbytes}x}"

    seen: list[dict] = []
    tt.configure(audit_sink=seen.append, id_factory=fixed)

    with tt.new_trace() as tid:
        with tt.span("审计.段") as rec:
            pass

    assert len(seen) == 1
    payload = seen[0]
    assert set(payload) == {
        "event",
        "trace_id",
        "span_id",
        "parent",
        "name",
        "duration_ms",
        "error",
    }
    assert payload["event"] == "span"
    assert payload["trace_id"] == tid == "0000000000000001"
    assert payload["span_id"] == rec.span_id == "00000002"
    assert payload["parent"] is None
    assert payload["name"] == "审计.段"
    assert isinstance(payload["duration_ms"], float) and payload["duration_ms"] >= 0.0
    assert payload["error"] is None


def test_audit_sink_parent_and_error_fields():
    """嵌套 + 异常:内层载荷 parent=外层 span_id;异常载荷 error 非空。"""
    seen = []
    tt.configure(audit_sink=seen.append)
    with tt.new_trace():
        with tt.span("审计.外层") as outer:
            with tt.span("审计.内层"):
                pass
        with pytest.raises(ValueError, match="bad"):
            with tt.span("审计.炸层"):
                raise ValueError("bad")

    by_name = {p["name"]: p for p in seen}
    assert set(by_name) == {"审计.外层", "审计.内层", "审计.炸层"}
    assert by_name["审计.内层"]["parent"] == outer.span_id
    assert by_name["审计.外层"]["parent"] is None
    assert by_name["审计.炸层"]["error"] == "ValueError: bad"
    assert by_name["审计.外层"]["error"] is None


def test_audit_sink_exception_swallowed():
    """audit_sink 抛异常:被追踪业务不受影响,计数 trace.audit.error。"""
    telemetry.reset()

    def bad_sink(_payload):
        raise ValueError("sink 坏了")

    tt.configure(audit_sink=bad_sink)
    with tt.new_trace():
        with tt.span("审计.失败回执"):
            pass  # 不应向上抛
    assert telemetry.snapshot()["counters"]["trace.audit.error"] == 1
    assert telemetry.snapshot()["timers"]["审计.失败回执"]["count"] == 1


def test_default_no_audit_sink_zero_side_effects():
    """默认 configure():无 sink,span 全链路正常、零审计副作用。"""
    telemetry.reset()
    tt.configure()
    with tt.new_trace():
        with tt.span("默认.段"):
            pass
    counters = telemetry.snapshot()["counters"]
    assert counters["trace.span"] == 1
    assert "trace.audit.error" not in counters  # 无 sink → 不存在审计失败路径


# ---------------------------------------------------------------------------
# traced 装饰器
# ---------------------------------------------------------------------------
def test_traced_decorator_passthrough_and_metadata():
    """@traced:返回值透传、functools.wraps 保留元数据、span 入树。"""

    @tt.traced("装饰.扫描")
    def scan(url, *, depth=1):
        return (url, depth)

    assert scan("https://a.example.com/", depth=2) == ("https://a.example.com/", 2)
    assert scan.__name__ == "scan"
    names = [n["name"] for n in _all_nodes()]
    assert names == ["装饰.扫描"]


def test_traced_decorator_reraises_and_records():
    """@traced 包裹的函数异常:记录 error 后重抛。"""
    telemetry.reset()

    @tt.traced("装饰.失败")
    def boom():
        raise KeyError("缺配置")

    with pytest.raises(KeyError):
        boom()
    nodes = [n for n in _all_nodes() if n["name"] == "装饰.失败"]
    assert nodes[0]["error"] == "KeyError: '缺配置'"
    assert telemetry.snapshot()["counters"]["trace.span.error"] == 1


# ---------------------------------------------------------------------------
# 并发隔离与线程边界
# ---------------------------------------------------------------------------
def test_concurrent_threads_each_own_trace():
    """8 线程各自 new_trace:trace_id 全互异,各自树只含自己的 span。"""
    results = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        with tt.new_trace() as tid:
            with tt.span(f"并发.段.{i}"):
                with tt.span(f"并发.子段.{i}"):
                    pass
            tree = tt.export_trace_json()
        with lock:
            results.append((tid, tree))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 8
    tids = [tid for tid, _ in results]
    assert len(set(tids)) == 8, "各线程 trace_id 必须互异(contextvars 线程隔离)"
    for tid, tree in results:
        assert tree["trace_id"] == tid
        root = tree["spans"][0]
        assert root["name"].startswith("并发.段.") and root["parent_span_id"] is None
        assert [c["name"] for c in root["children"]][0].startswith("并发.子段.")
    # 注册表恰 8 条 trace
    assert len(tt._TRACES) == 8
    assert telemetry.snapshot()["counters"]["trace.span"] == 16


def test_threads_without_new_trace_get_implicit_independent_traces():
    """无线程传参的裸 span:每线程隐式根 trace,互不串线。"""
    results = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        with tt.span(f"隐式.段.{i}") as rec:
            snapshot = (
                tt.current_trace_id(),
                tt.current_span_id(),
                tt.export_trace_json(),
            )
        with lock:
            results.append((rec, *snapshot))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    tids = [tid for _, tid, _, _ in results]
    assert len(set(tids)) == 4 and all(tid is not None for tid in tids)
    for rec, tid, cur_span, tree in results:
        assert tid == tree["trace_id"]
        assert cur_span == rec.span_id  # 体内当前 span 即自己
        root = tree["spans"][0]
        assert root["span_id"] == rec.span_id and root["parent_span_id"] is None


def test_propagate_restore_crosses_thread_boundary():
    """propagate/restore:子线程 span 挂回主 trace,parent=快照时刻 span。"""
    out: dict[str, object] = {}

    def worker(snap: tt.TraceSnapshot) -> None:
        tt.restore(snap)
        out["worker_trace"] = tt.current_trace_id()
        with tt.span("子线程.抓取") as rec:
            out["worker_parent"] = tt.current_span_id()
        out["worker_span"] = rec.span_id

    with tt.new_trace() as tid:
        with tt.span("主线程.整链") as root:
            snap = tt.propagate()
            assert (snap.trace_id, snap.span_id) == (tid, root.span_id)
            t = threading.Thread(target=worker, args=(snap,))
            t.start()
            t.join()
            # restore 只改子线程上下文,主线程当前 span 不受影响
            assert tt.current_span_id() == root.span_id
        out["tid"] = tid
        out["root_id"] = root.span_id

    tree = tt.export_trace_json(out["tid"])
    assert tree["trace_id"] == out["tid"]
    root_node = tree["spans"][0]
    assert root_node["span_id"] == out["root_id"]
    assert out["worker_trace"] == out["tid"]  # 子线程接回了同一条 trace
    child = root_node["children"][0]
    assert child["name"] == "子线程.抓取"
    assert child["span_id"] == out["worker_span"]
    assert child["parent_span_id"] == out["root_id"]
    assert len(root_node["children"]) == 1


def test_restore_rejects_non_snapshot():
    """restore 类型防御:非 TraceSnapshot 抛 TypeError(中文消息)。"""
    with pytest.raises(TypeError, match="TraceSnapshot"):
        tt.restore(("不是快照", None))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 确定性 id 工厂注入
# ---------------------------------------------------------------------------
def test_id_factory_injection_deterministic_ids():
    """固定序列工厂:trace/span id 逐字符确定,嵌套 parent 链亦可预期。"""
    seq = iter(range(1, 64))

    def fixed(nbytes: int) -> str:
        return f"{next(seq):0{2 * nbytes}x}"

    tt.configure(id_factory=fixed)
    with tt.new_trace() as tid:
        assert tid == "0000000000000001"
        with tt.span("确定.外") as outer:
            assert outer.span_id == "00000002"
            with tt.span("确定.内") as inner:
                assert inner.span_id == "00000003"
                assert inner.parent_span_id == "00000002"

    tree = tt.export_trace_json("0000000000000001")
    assert tree["spans"][0]["span_id"] == "00000002"
    assert tree["spans"][0]["children"][0]["span_id"] == "00000003"


def test_configure_type_validation():
    """configure:非可调用 audit_sink / id_factory 抛 TypeError。"""
    with pytest.raises(TypeError, match="audit_sink"):
        tt.configure(audit_sink=123)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="id_factory"):
        tt.configure(id_factory="not-callable")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# OTel 桥接:缺席(默认)/ import 失败 / 在场同步 / 桥接自护
# ---------------------------------------------------------------------------
def test_otel_absent_probe_returns_none_and_is_cached(monkeypatch):
    """缺席路径(默认):探测 None;结果缓存,第二次不再 import。"""
    calls: list[str] = []

    def no_otel(name: str):
        calls.append(name)
        raise ImportError(f"环境中没有 {name}")

    monkeypatch.setattr(tt, "import_module", no_otel)
    monkeypatch.setattr(tt, "_OTEL_TRACER", tt._UNPROBED)
    assert tt._otel_tracer() is None
    assert tt._otel_tracer() is None
    assert calls == ["opentelemetry.trace"], "探测应恰好一次后被缓存"


def test_span_flow_unaffected_when_otel_import_fails(monkeypatch):
    """import 失败路径:嵌套 + 异常 span 全部照常,零 import 错误外泄。"""
    telemetry.reset()

    def no_otel(_name):
        raise ImportError("no opentelemetry")

    monkeypatch.setattr(tt, "import_module", no_otel)
    monkeypatch.setattr(tt, "_OTEL_TRACER", tt._UNPROBED)

    with tt.new_trace() as tid:
        with tt.span("无桥.段", attrs={"stage": "fetch"}):
            with tt.span("无桥.子"):
                pass
        with pytest.raises(RuntimeError):
            with tt.span("无桥.炸"):
                raise RuntimeError("x")

    tree = tt.export_trace_json(tid)
    root = tree["spans"][0]
    assert root["name"] == "无桥.段" and root["children"][0]["name"] == "无桥.子"
    sibling = tree["spans"][1]  # 同层并列 span,不是 root 的孩子
    assert sibling["name"] == "无桥.炸"
    assert sibling["error"] == "RuntimeError: x"
    assert sibling["parent_span_id"] is None
    counters = telemetry.snapshot()["counters"]
    assert counters["trace.span"] == 3 and counters["trace.span.error"] == 1
    assert "trace.otel.error" not in counters  # 探测失败按缺席处理,不算桥接错误


def test_otel_bridge_syncs_when_present(monkeypatch):
    """在场路径(伪 tracer 注入):开/关/异常同步,attrs 透传。"""
    log: list[tuple] = []

    class _FakeSpan:
        def __init__(self, name: str):
            self._name = name

        def __enter__(self):
            log.append(("enter", self._name))
            return self

        def __exit__(self, exc_type, exc, tb):
            log.append(("exit", self._name, exc_type.__name__ if exc_type else None))

    class _FakeTracer:
        def start_as_current_span(self, name, attributes=None):
            log.append(("start", name, attributes))
            return _FakeSpan(name)

    fake_mod = types.ModuleType("opentelemetry.trace")
    fake_mod.get_tracer = lambda *a, **k: _FakeTracer()
    monkeypatch.setattr(tt, "import_module", lambda name: fake_mod)
    monkeypatch.setattr(tt, "_OTEL_TRACER", tt._UNPROBED)

    with tt.new_trace():
        with tt.span("桥接.成功", attrs={"stage": "vlm"}):
            pass
        with pytest.raises(RuntimeError):
            with tt.span("桥接.失败"):
                raise RuntimeError("boom")

    assert ("start", "桥接.成功", {"stage": "vlm"}) in log
    assert ("enter", "桥接.成功") in log
    assert ("exit", "桥接.成功", None) in log
    assert ("exit", "桥接.失败", "RuntimeError") in log, "异常信息必须传给 OTel __exit__"


def test_otel_bridge_failures_never_break_tracing(monkeypatch):
    """桥接自身异常:开/关 span 抛错都安全失效(计数),业务照常。"""
    telemetry.reset()

    class _EnterBoom:
        def start_as_current_span(self, name, attributes=None):
            raise ValueError("otel 开 span 失败")

    class _ExitBoomSpan:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            raise ValueError("otel 关 span 失败")

    class _ExitBoomTracer:
        def start_as_current_span(self, name, attributes=None):
            return _ExitBoomSpan()

    fake_mod = types.ModuleType("opentelemetry.trace")

    def switch_tracer(tracer):
        fake_mod.get_tracer = lambda *a, **k: tracer

    monkeypatch.setattr(tt, "import_module", lambda name: fake_mod)
    monkeypatch.setattr(tt, "_OTEL_TRACER", tt._UNPROBED)

    switch_tracer(_EnterBoom())
    with tt.new_trace():
        with tt.span("桥接.开失败"):
            pass  # 不应受桥接异常影响

    switch_tracer(_ExitBoomTracer())
    monkeypatch.setattr(tt, "_OTEL_TRACER", tt._UNPROBED)  # 换 tracer 后重新探测
    with tt.span("桥接.关失败"):
        pass

    names = [n["name"] for n in _all_nodes()]
    assert "桥接.开失败" in names and "桥接.关失败" in names
    assert telemetry.snapshot()["counters"]["trace.otel.error"] >= 2


# ---------------------------------------------------------------------------
# reset / 内存边界
# ---------------------------------------------------------------------------
def test_reset_clears_registry_and_probe_cache(monkeypatch):
    """reset:清空 trace 注册表与 OTel 探测缓存(测试隔离钩子)。"""

    def no_otel(name):
        raise ImportError(f"环境中没有 {name}")

    monkeypatch.setattr(tt, "import_module", no_otel)
    monkeypatch.setattr(tt, "_OTEL_TRACER", tt._UNPROBED)
    assert tt._otel_tracer() is None
    assert tt._OTEL_TRACER is None  # 探测结果(缺席)已缓存

    with tt.new_trace() as tid:
        with tt.span("重置前.段"):
            pass
    assert tt.export_trace_json(tid)["spans"]

    tt.reset()
    assert tt.export_trace_json(tid)["spans"] == []
    assert tt._OTEL_TRACER is tt._UNPROBED


def test_max_traces_eviction_keeps_newest():
    """注册表超 MAX_TRACES:按插入序逐出最旧,最新 trace 仍完整可导出。"""
    tids = []
    for i in range(tt.MAX_TRACES + 10):
        with tt.new_trace() as tid:
            with tt.span(f"逐出.段.{i}"):
                pass
        tids.append(tid)
    assert len(tt._TRACES) == tt.MAX_TRACES
    assert tt.export_trace_json(tids[0])["spans"] == [], "最旧 trace 应被逐出"
    newest = tt.export_trace_json(tids[-1])
    assert newest["trace_id"] == tids[-1]
    assert newest["spans"][0]["name"] == f"逐出.段.{tt.MAX_TRACES + 9}"


def test_max_spans_per_trace_cap(monkeypatch):
    """单 trace span 数上限:超限不进树(timer 双写照常)并计数 dropped。"""
    telemetry.reset()
    monkeypatch.setattr(tt, "MAX_SPANS_PER_TRACE", 2)
    with tt.new_trace() as tid:
        for i in range(3):
            with tt.span(f"截断.段.{i}"):
                pass
    tree = tt.export_trace_json(tid)
    roots = tree["spans"]
    assert len(roots) == 2, "第 3 个 span 应因上限被丢弃"
    assert telemetry.snapshot()["counters"]["trace.span.dropped"] == 1
    assert telemetry.snapshot()["counters"]["trace.span"] == 3
    assert telemetry.snapshot()["timers"]["截断.段.2"]["count"] == 1


# ---------------------------------------------------------------------------
# 长窗口转存(A231):drain_to_sink + configure(on_evict)
# ---------------------------------------------------------------------------
def test_drain_to_sink_empties_registry_with_exact_four_key_payload():
    """drain:逐 trace 写恰四键 trace_export 载荷(嵌套树与 export 同口径)
    后注册表清空;已转存 trace 再 export 得空树。"""
    seen: list[dict] = []
    tt.configure(audit_sink=seen.append)
    with tt.new_trace() as tid1:
        with tt.span("转存.外层", attrs={"stage": "fetch"}) as outer:
            with tt.span("转存.内层"):
                pass
    with tt.new_trace() as tid2:
        with tt.span("转存.另条"):
            pass

    drained = tt.drain_to_sink()

    assert drained == 2
    assert len(tt._TRACES) == 0, "转存后注册表应清空(防重复转存与内存增长)"
    assert tt.export_trace_json(tid1) == {
        "trace_id": tid1, "started_ts": None, "spans": [],
    }
    exports = [p for p in seen if p["event"] == "trace_export"]
    assert [p["trace_id"] for p in exports] == [tid1, tid2]  # 按插入序
    for payload in exports:
        assert set(payload) == {"event", "trace_id", "spans", "ts"}
        assert payload["event"] == "trace_export"
        assert isinstance(payload["ts"], str) and "T" in payload["ts"]
    root = exports[0]["spans"][0]
    assert root["name"] == "转存.外层"
    assert root["span_id"] == outer.span_id
    assert root["children"][0]["name"] == "转存.内层"  # 树结构原样进载荷
    assert json.dumps(exports[0], ensure_ascii=False)  # 可直接序列化


def test_drain_to_sink_repeated_calls_idempotent():
    """重复 drain 幂等:第二次 0 条、sink 不再收到 trace_export 载荷。"""
    seen: list[dict] = []
    tt.configure(audit_sink=seen.append)
    with tt.new_trace():
        with tt.span("幂等.段"):
            pass
    assert tt.drain_to_sink() == 1
    assert tt.drain_to_sink() == 0
    exports = [p for p in seen if p["event"] == "trace_export"]
    assert len(exports) == 1, "同一 trace 只转存一次(span 事件照常,不属转存)"


def test_drain_to_sink_without_sink_keeps_registry_intact():
    """无可用 sink(模块未注入且未传参):返回 0 且不动注册表(零变化)。"""
    telemetry.reset()
    with tt.new_trace() as tid:
        with tt.span("无汇.段"):
            pass
    assert tt.drain_to_sink() == 0
    assert tid in tt._TRACES and len(tt._TRACES) == 1
    assert tt.export_trace_json(tid)["spans"], "数据仍在,绝不无谓丢弃"
    counters = telemetry.snapshot()["counters"]
    assert "trace.drain.exported" not in counters
    assert "trace.drain.error" not in counters


def test_drain_to_sink_explicit_sink_param_takes_priority():
    """sink 参数优先:trace_export 只走显式 sink,模块注入的 sink 不收。"""
    module_seen: list[dict] = []
    explicit: list[dict] = []
    tt.configure(audit_sink=module_seen.append)
    with tt.new_trace() as tid:
        with tt.span("显式.段"):
            pass
    drained = tt.drain_to_sink(sink=explicit.append)
    assert drained == 1
    assert [p["trace_id"] for p in explicit] == [tid]
    assert [p["event"] for p in module_seen] == ["span"]  # 只有 span 事件


def test_drain_to_sink_sink_failure_counted_and_continues():
    """sink 逐条异常:安全吞掉计数 trace.drain.error,其余 trace 照常转存;
    已领走的 trace 无论成败均出账(转存 = 一次出账)。"""
    telemetry.reset()
    good: list[dict] = []

    def flaky(payload: dict) -> None:
        if payload["spans"] and payload["spans"][0]["name"] == "转存.炸条":
            raise ValueError("sink 坏了")
        good.append(payload)

    tt.configure(audit_sink=flaky)
    with tt.new_trace():
        with tt.span("转存.好条"):
            pass
    with tt.new_trace():
        with tt.span("转存.炸条"):
            pass
    drained = tt.drain_to_sink()
    assert drained == 1
    assert [p["spans"][0]["name"] for p in good] == ["转存.好条"]
    assert telemetry.snapshot()["counters"]["trace.drain.error"] == 1
    assert len(tt._TRACES) == 0, "炸条也已领走出账(内存不因 sink 故障增长)"


def test_on_evict_fires_on_overflow_before_data_loss():
    """on_evict:溢出逐出前触发,收四键载荷(含完整 span 树)——被逐出的
    最旧 trace 数据经回调拿到最后转存机会。"""
    evicted: list[dict] = []
    tt.configure(on_evict=evicted.append)
    oldest_span_id = ""
    for i in range(tt.MAX_TRACES + 1):
        with tt.new_trace():
            with tt.span(f"逐出钩.{i}") as rec:
                if i == 0:
                    oldest_span_id = rec.span_id
    assert len(evicted) == 1, "恰最旧一条被逐出"
    payload = evicted[0]
    assert set(payload) == {"event", "trace_id", "spans", "ts"}
    assert payload["event"] == "trace_export"
    assert payload["spans"][0]["name"] == "逐出钩.0"
    assert payload["spans"][0]["span_id"] == oldest_span_id
    assert len(tt._TRACES) == tt.MAX_TRACES  # 逐出本体照常收敛


def test_on_evict_failure_swallowed_and_counted():
    """on_evict 回调抛异常:安全吞掉计数 trace.evict.error,逐出本体不受影响。"""
    telemetry.reset()

    def bad(_payload: dict) -> None:
        raise RuntimeError("逐出回调坏了")

    tt.configure(on_evict=bad)
    for i in range(tt.MAX_TRACES + 1):
        with tt.new_trace():
            with tt.span(f"逐出错.{i}"):
                pass
    assert len(tt._TRACES) == tt.MAX_TRACES
    assert telemetry.snapshot()["counters"]["trace.evict.error"] == 1


def test_on_evict_default_none_eviction_byte_identical():
    """默认不注入 on_evict:逐出零回调零计数(与 A198 现状逐字节一致)。"""
    telemetry.reset()
    tids = []
    for i in range(tt.MAX_TRACES + 3):
        with tt.new_trace() as tid:
            with tt.span(f"默认逐出.{i}"):
                pass
        tids.append(tid)
    counters = telemetry.snapshot()["counters"]
    assert "trace.evict.error" not in counters
    assert tt.export_trace_json(tids[0])["spans"] == []
    assert len(tt._TRACES) == tt.MAX_TRACES


def test_configure_on_evict_type_validation():
    """configure:非可调用 on_evict 抛 TypeError(与其余注入点同口径)。"""
    with pytest.raises(TypeError, match="on_evict"):
        tt.configure(on_evict=123)  # type: ignore[arg-type]


def test_drain_concurrent_with_span_appends_thread_safe():
    """并发安全:多线程同时建 trace/挂 span 与 drain 交错——零异常、每条
    trace 最终都转存到 sink、载荷只含本批 trace、末次 drain 后注册表清空
    (drain 领取与 _append_span 同锁域,两条 drainer 不会重复领同一条)。"""
    made: list[str] = []
    guard = threading.Lock()
    errors: list[BaseException] = []
    received_ids: list[str] = []
    draining = threading.Event()

    def sink(payload: dict) -> None:
        received_ids.append(payload["trace_id"])

    def producer(k: int) -> None:
        try:
            for i in range(10):
                with tt.new_trace() as tid:
                    with tt.span(f"并发转存.{k}.{i}"):
                        pass
                with guard:
                    made.append(tid)
        except BaseException as exc:  # noqa: BLE001 - 记录后由断言判失败
            errors.append(exc)

    def drainer() -> None:
        try:
            while draining.is_set():
                tt.drain_to_sink(sink=sink)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    producers = [threading.Thread(target=producer, args=(k,)) for k in range(4)]
    draining.set()
    drainers = [threading.Thread(target=drainer) for _ in range(2)]
    for t in [*drainers, *producers]:
        t.start()
    for t in producers:
        t.join()
    draining.clear()
    for t in drainers:
        t.join()

    assert errors == []
    tt.drain_to_sink(sink=sink)  # 末次兜底:清空注册表
    assert len(tt._TRACES) == 0
    assert sorted(set(received_ids)) == sorted(made), "每条 trace 最终都转存到"
    assert set(received_ids) <= set(made), "载荷只含本批 trace(无串账)"


# ---------------------------------------------------------------------------
# 红线:源码 import 只允许标准库 + netsentinel
# ---------------------------------------------------------------------------
def test_source_imports_stdlib_and_netsentinel_only():
    """AST 全树断言:任何位置都无 opentelemetry / logging_util 的 import 语句。"""
    src = Path(inspect.getfile(tt)).read_text(encoding="utf-8")
    stdlib = set(sys.stdlib_module_names)
    offenders = []
    for node in ast.walk(ast.parse(src)):
        roots: set[str] = set()
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            if not node.level:
                roots = {(node.module or "").split(".")[0]}
        for root in roots:
            if root not in stdlib and root != "netsentinel":
                offenders.append(root)
    assert not offenders, f"发现非 stdlib/netsentinel 的 import:{sorted(set(offenders))}"


# ---------------------------------------------------------------------------
# 测试辅助:从注册表导出全部节点(供跨隐式 trace 的断言)
# ---------------------------------------------------------------------------
def _all_exported_roots():
    """全部 trace 的根节点展开(测试辅助)。"""
    nodes = []
    for tr in list(tt._TRACES):
        nodes.extend(tt.export_trace_json(tr)["spans"])
    return nodes


def _all_nodes():
    """全部 trace 的全部节点(深度优先;测试辅助)。"""
    flat: list[dict] = []

    def walk(node):
        flat.append(node)
        for child in node["children"]:
            walk(child)

    for root in _all_exported_roots():
        walk(root)
    return flat


# ---------------------------------------------------------------------------
# A203 部署注记:模块 docstring 必须收录多 worker 空树行为(防回归删除)
# ---------------------------------------------------------------------------


def test_docstring_documents_multi_worker_empty_tree_deployment_note():
    """部署注记防删除:docstring 须说明 uvicorn workers>1 的空树行为与三条建议。

    背景(A203 交付报告):trace 注册表是进程内内存态,uvicorn workers>1 时
    /trace 查询可能落到无该 trace 的 worker,返回空树(HTTP 200,不报错)。
    本模块只加文档与注释、零代码行为变化——该断言防止注记被误删。
    """
    doc = tt.__doc__ or ""
    assert "部署注记" in doc
    # 行为:多进程部署 / 空树 / 200 不报错(分摊语义而非数据丢失)
    assert "uvicorn" in doc
    assert "workers" in doc
    assert "空树" in doc
    # 建议:保持单进程 / 按 trace_id 前缀路由 / 依赖 drain_to_sink 转存的审计 JSONL
    assert "单进程" in doc
    assert "trace_id" in doc and "路由" in doc
    assert "drain_to_sink" in doc
