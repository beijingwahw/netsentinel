"""NetSentinel(净网哨兵)分布式追踪内核(netsentinel.telemetry_trace,A198)。

对标 **W3C Trace Context / OpenTelemetry 渐进式采纳**的零依赖追踪内核:用
纯标准库 :mod:`contextvars` 把 trace_id(16 hex)/ span_id(8 hex)沿调用链
传播,让 scan 链路(``fetch → vlm → fusion``)可以"一链一 trace、一段一
span"地贯通。OpenTelemetry 缺席时自动退化为**零依赖本港实现**(默认路径);
环境装有 opentelemetry 时惰性桥接同步(缺失零影响,绝无顶层 import)。

用法示例::

    from netsentinel import telemetry_trace as tt

    with tt.new_trace() as trace_id:              # 新 trace(16 hex)
        with tt.span("扫描.抓取", attrs={"stage": "fetch"}):
            ...                                    # 嵌套即父子 span
            with tt.span("抓取.翻页"):
                ...
        with tt.span("扫描.融合"):
            ...

        tt.export_trace_json()                     # 当前 trace 的嵌套 span 树

    @tt.traced("扫描.单页")                         # 装饰器等价于 span()
    def scan_one(url, cfg): ...

    # 线程边界:显式快照/恢复对,不做任何隐式 magic
    snap = tt.propagate()
    def worker():
        tt.restore(snap)                           # 子线程接入同一条 trace
        with tt.span("扫描.子线程"): ...

核心 API(:func:`new_trace` / :func:`span` / :func:`traced` / :func:`propagate`
/ :func:`restore` / :func:`export_trace_json` / :func:`drain_to_sink`
/ :func:`configure` / :func:`reset`):

- ``new_trace()``:上下文管理器,生成新 trace_id 并进入其上下文,``yield``
  trace_id;离开时恢复原上下文(可嵌套,内层生效期间覆盖外层);
- ``span(name, *, attrs=None)``:上下文管理器,生成 span_id(8 hex),parent
  自动取当前 span(嵌套成树);时长用 :func:`time.perf_counter` 计;体内抛
  异常时**记录 error 后原样重抛,绝不吞**;无显式 trace 时自动开一条隐式
  根 trace(渐进式采纳,离开时恢复);
- ``@traced(name)``:装饰器形态的 :func:`span`;
- ``propagate()`` / ``restore(snapshot)``:跨线程显式传播对——
  :class:`TraceSnapshot` 冻结 dataclass(trace_id + 当前 span_id),
  在新线程里 :func:`restore` 后继续挂到同一条 trace 树上;不做任何
  隐式线程注入;
- ``export_trace_json(trace_id=None)``:返回当前 trace(或指定 id)的
  **嵌套 span 树**——``{"trace_id", "started_ts", "spans": [...]}``,
  节点含 ``name/span_id/parent_span_id/start_ts/duration_ms/error/attrs/
  children``,中文 name 原样保留,可直接 :func:`json.dumps`;
- ``configure(*, audit_sink=None, id_factory=None, on_evict=None)``:
  模块级注入点(默认全 None 零副作用);
- ``reset()``:清空 trace 注册表与 OTel 探测缓存(仅测试使用)。

落点双写(span 结束时,顺序固定):

1. ``telemetry.observe(name, duration秒)``——同名 timer 进
   :mod:`netsentinel.telemetry` 环形样本(该模块只读使用,不改其源);
   另计 ``trace.span`` / ``trace.span.error`` / 桥接与审计的安全失效计数;
2. 可选 ``audit_sink(payload)`` 回调——payload 恒为
   ``{"event": "span", "trace_id", "span_id", "parent", "name",
   "duration_ms", "error"}`` 七键;本模块**不直接 import logging_util**
   (避免耦合),由调用方注入,例如::

       audit = JsonlAuditLogger("data/audit.jsonl")
       tt.configure(audit_sink=lambda rec: audit.log_event(**rec))

   audit_sink 抛出的任何异常都被安全吞掉并计数(``trace.audit.error``),
   绝不拖垮被追踪业务。

长窗口转存(:func:`drain_to_sink` + ``on_evict``,A231 新增):

- ``drain_to_sink(*, sink=None)``:把注册表**全部** trace(含将被逐出的)
  逐条以 ``{"event": "trace_export", "trace_id", "spans": 嵌套树, "ts"}``
  四键载荷写入 sink(sink 参数优先,缺省用已注入的 ``audit_sink``),
  **随后从注册表移除**——防重复转存与内存无界增长;无任何可用 sink 时
  立即返回 0 且**不动注册表**(绝不无谓丢数据);转存与移除在注册表
  锁的同一保护域内完成快照/删除(与 :func:`_append_span` 同锁域,sink
  回调在锁外调用以防重入死锁);成功计数 ``trace.drain.exported``,
  单条 sink 异常安全吞掉并计数 ``trace.drain.error`` 后继续下一条;
- ``configure(on_evict=...)``:MAX_TRACES 逐出回调——逐出发生前捕获
  同款四键载荷交给回调(给长窗口转存**最后一次机会**,数据在删除前
  已定格);回调在锁外调用、异常安全吞掉并计数 ``trace.evict.error``;
  默认 None = 逐出行为与现状逐字节一致。

OTel 桥接(可选、薄、惰性):

- 每个 span 开始时才探测 ``opentelemetry.trace``(顶层零 import,缺席或
  探测异常一律返回 None → 纯本港路径,为**默认且被测试覆盖的路径**);
- 探测成功后用 ``tracer.start_as_current_span(name, attributes=...)``
  同步开/关 OTel span(异常 span 会把异常信息传给 OTel ``__exit__``);
- 桥接的任何异常都安全失效(计数 ``trace.otel.error``),被追踪业务
  永远不受影响;opentelemetry **不进入必装依赖**(pyproject 不动)。

内存边界(与 telemetry 环形截断同哲学):

- 全局注册表最多保留 :data:`MAX_TRACES`(64)条 trace,超出按插入序
  逐出最旧(当前活跃 trace 恒为最新,不会被逐出);``on_evict`` 已注入
  时,逐出前先把该 trace 的完整导出载荷交给回调(长窗口转存最后机会);
- 单条 trace 最多记录 :data:`MAX_SPANS_PER_TRACE`(4096)个 span,超出
  只是不再进树(timer 双写照常),并计数 ``trace.span.dropped``;
- 长跑窗口不足(A203 交付报告建议):调用方定期
  :func:`drain_to_sink` 可把注册表整体转存审计 JSONL 后清空,逐出
  前的尾巴由 ``on_evict`` 兜底。

部署注记(A203 交付报告收录 · 多进程行为说明,仅文档零代码)::

    trace 注册表(:data:`_TRACES`)是**进程内内存态**,不跨进程共享。webui
    以 ``uvicorn --workers N(N > 1)`` 多进程部署时,``/trace`` 在线查询
    会按负载均衡落到任意 worker——目标 trace 若建在另一 worker,本
    worker 查无此 trace,返回**空树**(``{"trace_id": …, "started_ts":
    None, "spans": []}``,HTTP 200 不报错;即 :func:`export_trace_json`
    对未知/已逐出 trace_id 的既有口径)——这是多进程分摊语义而非数据
    丢失。建议任选其一:

    1. 保持单进程(``workers=1``):注册表即全量视图,查询恒命中;
    2. 或按 trace_id 前缀做路由粘性(同一 trace 的查询恒落同一 worker);
    3. 或不依赖在线查询,改依赖 batchflow 长窗口 :func:`drain_to_sink`
       转存的审计 JSONL(A231 已建)做事后检索——转存发生在产生该
       trace 的进程内,不受多 worker 分摊影响。

红线遵守:

- **零运行时第三方依赖**:仅标准库 + ``netsentinel`` 包内只读引用
  (telemetry);opentelemetry 仅惰性探测桥接、缺失零影响;
- **红线 17(遥测只存名称与数字)**:``attrs`` 由调用方保证不含
  URL/密钥/路径等内容字段,本模块不添加也不检查内容;
- **纯新增模块,不接线主流程**:orchestrator/service 的 scan 链路接线
  属后续工作(见模块尾"接线路径"注释);
- 不 import logging_util / 不改 telemetry.py / 不写任何文件。

线程模型::mod:`contextvars` 每线程独立上下文——各线程天然各自独立
trace_id(测试覆盖);跨线程贯通**必须**显式 :func:`propagate` →
:func:`restore`;注册表自身由 ``threading.Lock`` 保护,多线程挂 span 到
同一条 trace 安全。
"""
from __future__ import annotations

import contextvars
import datetime as _dt
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from importlib import import_module
from typing import Any, Callable, Iterator, Mapping

from netsentinel import telemetry

__all__ = [
    "TRACE_ID_HEX_LEN",
    "SPAN_ID_HEX_LEN",
    "MAX_TRACES",
    "MAX_SPANS_PER_TRACE",
    "SpanRecord",
    "TraceSnapshot",
    "new_trace",
    "span",
    "traced",
    "propagate",
    "restore",
    "current_trace_id",
    "current_span_id",
    "export_trace_json",
    "drain_to_sink",
    "configure",
    "reset",
]

#: trace_id 十六进制字符数(W3C 风格缩短口径:64 bit → 16 hex)
TRACE_ID_HEX_LEN = 16

#: span_id 十六进制字符数(32 bit → 8 hex)
SPAN_ID_HEX_LEN = 8

#: 注册表最多保留的 trace 条数(超出按插入序逐出最旧;防内存无界)
MAX_TRACES = 64

#: 单条 trace 最多记录的 span 数(超出不再进树,timer 双写照常)
MAX_SPANS_PER_TRACE = 4096

#: id 工厂签名:入参为随机字节数,返回 2×nbytes 个小写 hex 字符
IdFactory = Callable[[int], str]

#: 审计回调签名:入参为七键 span 载荷 dict,返回值被忽略
AuditSink = Callable[[dict[str, Any]], None]

#: 逐出回调签名:入参为四键 trace_export 载荷 dict(与 drain 同款),返回值被忽略
EvictCallback = Callable[[dict[str, Any]], None]

_TRACE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "netsentinel_trace_id", default=None
)
_CURRENT_SPAN: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "netsentinel_current_span", default=None,
)

#: 注册表锁:保护 _TRACES 的建/逐出/追加/读取(记录字段定稿也走这把锁)
_LOCK = threading.Lock()

#: trace_id → trace 簿记(插入序即建序,供 MAX_TRACES 逐出最旧)
_TRACES: dict[str, "_TraceRecord"] = {}

#: 模块级注入点(configure 写入;默认 None 零副作用)
_AUDIT_SINK: AuditSink | None = None
_ID_FACTORY: IdFactory = secrets.token_hex
_ON_EVICT: EvictCallback | None = None

#: OTel 探测哨兵:未探测 / 探测成功(tracer)/ 探测失败(None)
_UNPROBED = object()
_OTEL_TRACER: Any = _UNPROBED


# ---------------------------------------------------------------------------
# dataclass 契约
# ---------------------------------------------------------------------------


@dataclass
class SpanRecord:
    """单个 span 的账目(开始即登记,结束时回填 duration/error)。

    ``duration_ms``/``error`` 在 span 进行中为 ``None``;``error`` 形如
    ``"RuntimeError: 数据源失败"``(类型名 + 消息,与 logging_util 的
    ``_degrade_reason``、merkle 降级原因同款格式)。
    ``attrs`` 由调用方保证不含内容字段(红线 17)。
    """

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    start_ts: str
    attrs: dict[str, Any] = field(default_factory=dict)
    duration_ms: float | None = None
    error: str | None = None


@dataclass(frozen=True)
class _TraceRecord:
    """一条 trace 的簿记:span 按开始序排列(嵌套树的稳定遍历序)。"""

    trace_id: str
    started_ts: str
    spans: list[SpanRecord] = field(default_factory=list)


@dataclass(frozen=True)
class TraceSnapshot:
    """跨线程传播快照(:func:`propagate` 产出 / :func:`restore` 消费)。

    显式 API、无隐式 magic:主线程 ``propagate()`` 拿到快照,交给工作线程
    ``restore()`` 后,工作线程内开的 span 就挂到同一条 trace 树、以快照
    时刻的 span 为父。
    """

    trace_id: str | None
    span_id: str | None


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """当前本地时区 ISO 时间戳(毫秒精度;与 telemetry.snapshot 同风格)。"""
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .astimezone()
        .isoformat(timespec="milliseconds")
    )


def _make_id(nbytes: int) -> str:
    """经注入工厂生成 id:nbytes 字节 → 2×nbytes 个 hex 字符。"""
    return _ID_FACTORY(nbytes)


def _register_trace(trace_id: str) -> None:
    """登记 trace(已存在则幂等返回);超 :data:`MAX_TRACES` 逐出最旧。

    ``on_evict`` 已注入时:逐出条目的完整导出载荷(嵌套 span 树)在
    **删除前**于锁内定格,锁外交给回调——给长窗口转存最后一次机会,
    同时避免回调重入本模块锁造成死锁;回调异常安全吞掉并计数
    ``trace.evict.error``,绝不影响注册/逐出本体。
    """
    evicted: list[tuple[str, str, list[SpanRecord]]] = []
    with _LOCK:
        if trace_id in _TRACES:
            return
        _TRACES[trace_id] = _TraceRecord(trace_id=trace_id, started_ts=_now_iso())
        while len(_TRACES) > MAX_TRACES:
            for oldest in _TRACES:  # dict 保插入序:首个即最旧
                if oldest != trace_id:
                    tr = _TRACES.pop(oldest)
                    if _ON_EVICT is not None:
                        # 删除前捕获载荷数据(spans 快照),锁外回调
                        evicted.append((tr.trace_id, tr.started_ts, list(tr.spans)))
                    break
            else:  # 只剩自己(理论不可达,防御 MAX_TRACES<1 的误配置)
                break
    for tid, started_ts, records in evicted:
        try:
            _ON_EVICT(_trace_export_payload(tid, started_ts, records))
        except Exception:  # noqa: BLE001 - 逐出回调失效绝不拖垮追踪本体
            telemetry.inc("trace.evict.error")


def _append_span(trace_id: str, record: SpanRecord) -> bool:
    """把 span 账目按开始序追加进 trace;超上限返回 False(调用方计数)。"""
    with _LOCK:
        tr = _TRACES.get(trace_id)
        if tr is None:  # propagate 自被逐出/未知 trace 的按需重建
            tr = _TraceRecord(trace_id=trace_id, started_ts=_now_iso())
            _TRACES[trace_id] = tr
        if len(tr.spans) >= MAX_SPANS_PER_TRACE:
            return False
        tr.spans.append(record)
        return True


def _finalize(record: SpanRecord, duration_ms: float, error: str | None) -> None:
    """锁内回填 span 终态(避免导出方读到撕裂的半更新记录)。"""
    with _LOCK:
        record.duration_ms = duration_ms
        record.error = error


def _emit(record: SpanRecord) -> None:
    """span 结束的落点双写:telemetry timer → 可选 audit_sink。

    任何一侧的异常都安全失效并计数,绝不向上抛进被追踪业务。
    """
    duration_s = (record.duration_ms or 0.0) / 1000.0
    telemetry.observe(record.name, duration_s)
    telemetry.inc("trace.span")
    if record.error is not None:
        telemetry.inc("trace.span.error")
    sink = _AUDIT_SINK
    if sink is None:
        return
    payload = {
        "event": "span",
        "trace_id": record.trace_id,
        "span_id": record.span_id,
        "parent": record.parent_span_id,
        "name": record.name,
        "duration_ms": record.duration_ms,
        "error": record.error,
    }
    try:
        sink(payload)
    except Exception:  # noqa: BLE001 - 审计旁路失效绝不拖垮业务
        telemetry.inc("trace.audit.error")


# ---------------------------------------------------------------------------
# OTel 惰性桥接(薄:探测一次 → 开/关 span;缺席/异常一律零影响)
# ---------------------------------------------------------------------------


def _otel_tracer() -> Any:
    """惰性探测 opentelemetry tracer;缺失/任何异常 → None(默认零依赖路径)。

    结果缓存(缺失也缓存),避免每个 span 重复付出 import 代价;
    :func:`reset` 清缓存以便测试重新探测。
    """
    global _OTEL_TRACER
    if _OTEL_TRACER is not _UNPROBED:
        return _OTEL_TRACER
    try:
        otel_trace = import_module("opentelemetry.trace")
        _OTEL_TRACER = otel_trace.get_tracer("netsentinel.telemetry_trace")
    except Exception:  # noqa: BLE001 - 未安装/坏环境/坏 provider 均按缺席处理
        _OTEL_TRACER = None
    return _OTEL_TRACER


def _otel_enter(name: str, attrs: Mapping[str, Any]) -> Any:
    """桥接开 span:返回 OTel span 上下文管理器;不可用/异常返回 None。"""
    tracer = _otel_tracer()
    if tracer is None:
        return None
    try:
        cm = tracer.start_as_current_span(name, attributes=dict(attrs) or None)
        cm.__enter__()
        return cm
    except Exception:  # noqa: BLE001 - 桥接安全失效
        telemetry.inc("trace.otel.error")
        return None


def _otel_exit(cm: Any, exc_info: tuple[Any, Any, Any]) -> None:
    """桥接关 span:把异常信息传给 OTel(其内部负责 record_exception/状态)。"""
    if cm is None:
        return
    exc_type, exc, _tb = exc_info
    try:
        cm.__exit__(exc_type, exc, _tb)
    except Exception:  # noqa: BLE001 - 桥接安全失效
        telemetry.inc("trace.otel.error")


# ---------------------------------------------------------------------------
# 公开 API:new_trace / span / traced
# ---------------------------------------------------------------------------


@contextmanager
def new_trace() -> Iterator[str]:
    """进入一条新 trace:生成 16 hex trace_id,``yield`` 该 id。

    嵌套使用时内层 ``new_trace`` 在其作用域内覆盖外层,离开时逐一恢复
    原 contextvar(token 复位);同时把当前 span 清空——新 trace 内第一个
    span 即根 span(parent 为 None)。
    """
    trace_id = _make_id(TRACE_ID_HEX_LEN // 2)
    _register_trace(trace_id)
    trace_token = _TRACE_ID.set(trace_id)
    span_token = _CURRENT_SPAN.set(None)
    try:
        yield trace_id
    finally:
        _CURRENT_SPAN.reset(span_token)
        _TRACE_ID.reset(trace_token)


@contextmanager
def span(
    name: str, *, attrs: Mapping[str, Any] | None = None
) -> Iterator[SpanRecord]:
    """开一个 span 并挂到当前 trace(无显式 trace 时自动开隐式根 trace)。

    - **传播**:span_id 为 8 hex;parent 自动取进入时的当前 span
      (嵌套 ``with`` 即父子树);体内经由 :func:`traced`/线程内继续嵌套
      均按同一规则延伸;
    - **计时**:``time.perf_counter`` 差值,毫秒保留 3 位小数;
    - **异常**:体内抛异常时记录 ``"类型: 消息"`` 到 span 账目与审计载荷,
      然后**原样重抛,绝不吞**;
    - **双写**:结束时 :func:`telemetry.observe`(同名 timer,秒)+
      可选 ``audit_sink``(见 :func:`configure`);OTel 在场则同步桥接;
    - ``yield`` 该 span 的 :class:`SpanRecord`(可读 ``span_id`` 做关联,
      请勿外部改写字段)。

    ``attrs`` 只存调用方给的标签键值(红线 17:不得放 URL/密钥/路径等
    内容字段),导出时原样进入 ``attrs`` 节点。
    """
    safe_name = str(name)
    trace_id = _TRACE_ID.get()
    implicit_token: contextvars.Token | None = None
    if trace_id is None:
        # 渐进式采纳:没有显式 new_trace() 也能直接 span(自动隐式根 trace)
        trace_id = _make_id(TRACE_ID_HEX_LEN // 2)
        implicit_token = _TRACE_ID.set(trace_id)
    _register_trace(trace_id)
    parent_span_id = _CURRENT_SPAN.get()
    span_id = _make_id(SPAN_ID_HEX_LEN // 2)
    record = SpanRecord(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        name=safe_name,
        start_ts=_now_iso(),
        attrs=dict(attrs) if attrs else {},
    )
    appended = _append_span(trace_id, record)
    if not appended:
        telemetry.inc("trace.span.dropped")
    span_token = _CURRENT_SPAN.set(span_id) if appended else None
    start_perf = time.perf_counter()
    otel_cm = _otel_enter(safe_name, record.attrs)
    error_text: str | None = None
    exc_info: tuple[Any, Any, Any] = (None, None, None)
    try:
        yield record
    except BaseException as exc:  # 记录后原样重抛,绝不吞异常
        error_text = f"{type(exc).__name__}: {exc}"
        exc_info = (type(exc), exc, exc.__traceback__)
        raise
    finally:
        if span_token is not None:
            _CURRENT_SPAN.reset(span_token)
        if implicit_token is not None:
            _TRACE_ID.reset(implicit_token)
        duration_ms = round((time.perf_counter() - start_perf) * 1000.0, 3)
        _finalize(record, duration_ms, error_text)
        _otel_exit(otel_cm, exc_info)
        _emit(record)


def traced(
    name: str, *, attrs: Mapping[str, Any] | None = None
) -> Callable[[Callable], Callable]:
    """装饰器形态的 :func:`span`(与 ``telemetry.timed`` 同风格)::

        @telemetry_trace.traced("扫描.单页")
        def scan_one(url, cfg): ...

    被装饰函数抛异常同样"记录后重抛";返回值原样透传。
    """

    def deco(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with span(name, attrs=attrs):
                return func(*args, **kwargs)

        return wrapper

    return deco


# ---------------------------------------------------------------------------
# 线程边界:显式 propagate / restore 对
# ---------------------------------------------------------------------------


def propagate() -> TraceSnapshot:
    """拍下当前 (trace_id, 当前 span_id) 快照,供其他线程 :func:`restore`。

    这是**唯一的**跨线程传播机制:本模块绝不隐式把 context 注入别的线程;
    每个新线程默认从空上下文起步(独立 trace),要接回主 trace 就显式
    传快照。
    """
    return TraceSnapshot(trace_id=_TRACE_ID.get(), span_id=_CURRENT_SPAN.get())


def restore(snapshot: TraceSnapshot) -> None:
    """在当前线程恢复 :func:`propagate` 拍下的快照(接入同一条 trace)。"""
    if not isinstance(snapshot, TraceSnapshot):
        raise TypeError(f"snapshot 应为 TraceSnapshot,当前为 {type(snapshot).__name__}")
    _TRACE_ID.set(snapshot.trace_id)
    _CURRENT_SPAN.set(snapshot.span_id)


def current_trace_id() -> str | None:
    """当前上下文的 trace_id(无 trace 时 None;供 orchestrator 接线用)。"""
    return _TRACE_ID.get()


def current_span_id() -> str | None:
    """当前上下文的 span_id(无活跃 span 时 None)。"""
    return _CURRENT_SPAN.get()


# ---------------------------------------------------------------------------
# 导出 / 配置 / 测试钩子
# ---------------------------------------------------------------------------


def _tree_from_records(
    tid: str | None, started_ts: str | None, records: list[SpanRecord]
) -> dict[str, Any]:
    """span 账目列表(开始序)→ 嵌套 span 树(结构见 :func:`export_trace_json`)。

    父 id 找不到对应节点(如父 span 属于被逐出/已转存的 trace)时该 span
    上浮为根;纯函数,不触碰注册表(:func:`export_trace_json` /
    :func:`drain_to_sink` / 逐出回调共用同一棵树口径)。
    """
    nodes: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for rec in records:
        node: dict[str, Any] = {
            "name": rec.name,
            "span_id": rec.span_id,
            "parent_span_id": rec.parent_span_id,
            "start_ts": rec.start_ts,
            "duration_ms": rec.duration_ms,
            "error": rec.error,
            "attrs": dict(rec.attrs),
            "children": [],
        }
        nodes[rec.span_id] = node
        ordered.append(node)
    roots: list[dict[str, Any]] = []
    for rec, node in zip(records, ordered):
        parent = rec.parent_span_id
        if parent is not None and parent in nodes:
            nodes[parent]["children"].append(node)
        else:
            roots.append(node)
    return {"trace_id": tid, "started_ts": started_ts, "spans": roots}


def _trace_export_payload(
    trace_id: str, started_ts: str, records: list[SpanRecord]
) -> dict[str, Any]:
    """单条 trace 的长窗口转存载荷(恒四键,可直接 :func:`json.dumps`)::

        {"event": "trace_export", "trace_id": "…16hex…",
         "spans": [...嵌套树,与 export_trace_json 同口径...],
         "ts": "…ISO…"}
    """
    tree = _tree_from_records(trace_id, started_ts, records)
    return {
        "event": "trace_export",
        "trace_id": trace_id,
        "spans": tree["spans"],
        "ts": _now_iso(),
    }


def export_trace_json(trace_id: str | None = None) -> dict[str, Any]:
    """当前 trace(或指定 id)的嵌套 span 树,可直接 :func:`json.dumps`。

    返回结构::

        {
          "trace_id": "…16hex…",       # 当前无 trace 且未指定时为 None
          "started_ts": "…ISO…",        # trace 首个登记时刻
          "spans": [                     # 根 span 数组(开始序),嵌套如下
            {"name": "扫描.抓取",        # 中文 name 原样保留
             "span_id": "…8hex…",
             "parent_span_id": None,
             "start_ts": "…ISO…",
             "duration_ms": 12.345,     # 进行中的 span 为 None
             "error": None,             # 异常 span 为 "类型: 消息"
             "attrs": {...},            # 调用方标签原样
             "children": [ ... ]},      # 子 span(开始序)
          ]
        }

    父 id 找不到对应节点(如父 span 属于被逐出 trace)时该 span 上浮为根;
    未知/已逐出 trace_id 返回空树(``spans: []``)而非报错。
    """
    tid = _TRACE_ID.get() if trace_id is None else trace_id
    with _LOCK:
        tr = _TRACES.get(tid) if tid is not None else None
        if tr is None:
            return {"trace_id": tid, "started_ts": None, "spans": []}
        started_ts = tr.started_ts
        records = list(tr.spans)
    return _tree_from_records(tid, started_ts, records)


def drain_to_sink(*, sink: AuditSink | None = None) -> int:
    """把注册表**全部** trace 转存到审计 sink 后清空注册表(长窗口转存)。

    - **零副作用门槛**:``sink`` 未给且模块级 ``audit_sink`` 也未注入 →
      立即返回 0,**不动注册表**(绝不无谓丢数据,现状零变化);
    - **载荷**:逐 trace 一条 ``{"event": "trace_export", "trace_id",
      "spans": 嵌套树, "ts"}`` 四键(与 :func:`export_trace_json` 同一
      树口径,含将被 :data:`MAX_TRACES` 逐出的全部存量);
    - **移除防重**:注册表快照与清空在 :data:`_LOCK` 同一锁域内一次完成
      (与 :func:`_append_span` 同锁域,并发安全);sink 回调在**锁外**
      逐条调用(防回调重入本模块锁造成死锁),单条异常安全吞掉、计数
      ``trace.drain.error`` 后继续下一条——已领走的 trace 无论转存成败
      均不再回注册表(转存语义 = 一次出账,防重复转存与内存增长);
    - 成功计数 ``trace.drain.exported``;返回成功转存的 trace 条数。

    典型接线(长跑批次收官,如 batchflow)::

        audit = JsonlAuditLogger(cfg.audit_path)
        drained = tt.drain_to_sink(sink=lambda p: audit.log_event(**p))
    """
    target = sink if sink is not None else _AUDIT_SINK
    if target is None:
        return 0
    with _LOCK:  # 领取全部 trace(快照 + 清空原子完成,并发 append 不丢)
        if not _TRACES:
            return 0
        claimed: list[tuple[str, str, list[SpanRecord]]] = [
            (tid, tr.started_ts, list(tr.spans)) for tid, tr in _TRACES.items()
        ]
        _TRACES.clear()
    drained = 0
    for tid, started_ts, records in claimed:
        try:
            target(_trace_export_payload(tid, started_ts, records))
        except Exception:  # noqa: BLE001 - 转存失败绝不中断主流程
            telemetry.inc("trace.drain.error")
            continue
        drained += 1
    if drained:
        telemetry.inc("trace.drain.exported", drained)
    return drained


def configure(
    *,
    audit_sink: AuditSink | None = None,
    id_factory: IdFactory | None = None,
    on_evict: EvictCallback | None = None,
) -> None:
    """模块级注入点(默认全 None 零副作用;每次调用整体生效,传 None 即清除)。

    - ``audit_sink``:span 结束回调,收七键载荷(见模块 docstring 示例,
      典型接 ``JsonlAuditLogger.log_event``;本模块不直接 import logging_util);
    - ``id_factory``:``(nbytes) -> 2×nbytes 个 hex 字符``,默认
      ``secrets.token_hex``;测试注入固定序列即得确定性 trace/span id;
    - ``on_evict``::data:`MAX_TRACES` 逐出回调,收 :func:`drain_to_sink`
      同款四键 trace_export 载荷(逐出条目的完整 span 树在删除前定格,
      给长窗口转存最后一次机会);默认 None = 逐出行为与现状逐字节一致。
    """
    global _AUDIT_SINK, _ID_FACTORY, _ON_EVICT
    if audit_sink is not None and not callable(audit_sink):
        raise TypeError(f"audit_sink 需为可调用,当前为 {type(audit_sink).__name__}")
    if id_factory is not None and not callable(id_factory):
        raise TypeError(f"id_factory 需为可调用,当前为 {type(id_factory).__name__}")
    if on_evict is not None and not callable(on_evict):
        raise TypeError(f"on_evict 需为可调用,当前为 {type(on_evict).__name__}")
    _AUDIT_SINK = audit_sink
    _ON_EVICT = on_evict
    if id_factory is not None:
        _ID_FACTORY = id_factory
    else:
        _ID_FACTORY = secrets.token_hex


def reset() -> None:
    """清空 trace 注册表与 OTel 探测缓存(仅测试使用;不动 configure 配置)。"""
    global _OTEL_TRACER
    with _LOCK:
        _TRACES.clear()
    _OTEL_TRACER = _UNPROBED


# ---------------------------------------------------------------------------
# 后续接线路径(备忘,不改主流程——接线属后续工作)
#
# 1. orchestrator(service 之外的编排层)在 scan 入口:
#        with telemetry_trace.new_trace() as trace_id:
#            audit.log_event("scan_trace_started", trace_id=trace_id)
#            ... 现有 fetch→vlm→fusion 链 ...
# 2. 链上各段换成 @traced 或 with span(...)(fetch/vlm/fusion 三个服务模块
#    各包一层,attrs 只放 stage 等标签,不放内容字段);
# 3. 线程池任务用 propagate()/restore() 快照对接(ops/pool 提交处统一注入);
# 4. service 侧如需透出:export_trace_json(trace_id) 直接可 dumps 进
#    JsonlAuditLogger(log_event(**{"event": "trace_export", **tree}))。
# ---------------------------------------------------------------------------
