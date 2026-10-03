"""观测内核·指标导出(V7 · A137;见 CONTRACTS-V7.md §2 A137 行 / 红线 29、31)。

把 :mod:`netsentinel.telemetry`(冻结,V5)的 ``snapshot()`` 汇总视图转成
**纯标准库**的可序列化文本,供运营者把进程内指标接入任意外部观测体系:

- :func:`to_prometheus`:快照 → Prometheus 文本 exposition 格式(0.0.4 语法);
- :func:`diff_snaps`:两份快照的**简明增量 diff**(新增计数器 / 计数器增量 /
  仪表变化 / 计时器变化,四键恒存在);
- :func:`render_metrics`:``telemetry.snapshot()`` → ``to_prometheus`` 直通,
  service 挂 ``/metrics`` 端点的说明见其 docstring(**不改 service**,红线 29);
- :func:`parse_prometheus`:极简解析器(指标名 → 值;bench 往返与消费方校验用);
- :func:`kernel_selfcheck`:离线可复现微基准(A138 基准总控统一调用)。

格式规则(:func:`to_prometheus`)
--------------------------------

- 计数器 → ``<name>_total <v>`` 一条样本行,``# TYPE`` 为 ``counter``;
- 仪表   → ``<name> <v>``,``# TYPE`` 为 ``gauge``;
- 计时器 → ``<name>_count`` / ``<name>_avg_ms`` / ``<name>_p95_ms`` /
  ``<name>_max_ms`` 四条序列,``# TYPE`` 全部为 ``gauge``——``_count`` 是
  环形缓冲内的**当前样本数**,受 telemetry :data:`~netsentinel.telemetry.MAX_SAMPLES`
  截断、非严格单调,标 counter 会被 Prometheus 误判"计数器重置",故标 gauge;
- 每个指标族(同名样本组)前恰好一行 ``# HELP`` + 一行 ``# TYPE``;
- 名称清洗(:func:`clean_name`):``[a-zA-Z0-9_:]`` 之外的字节一律换 ``_``;
  首字符为数字时补前缀 ``ns_``(exposition 指标名不允许数字开头);空名回退
  ``ns_unnamed``;``# HELP`` 文本内保留**原始名**(反斜杠/换行按规则转义),
  运营者可据此回溯清洗前的遥测键;
- 值格式:整值浮点压成整数文本(``5.0`` → ``5``),其余用 ``repr``
  (最短往返表示);负数/科学计数法文本均为合法 exposition 值;
- 节序固定 counters → gauges → timers,节内指标名**排序**输出,
  同一快照恒得同一文本(逐字节确定,便于 diff 与测试);
- 空快照 → 仅注释头(无任何指标行);缺失的节视同空节;
- 输出以换行符结束(exposition 格式要求)。

diff 语义(:func:`diff_snaps`)
------------------------------

telemetry 的计数器**只增不删**(名字一旦出现不会被移除,除非 ``reset()``
全清),故 b 中消失的计数器不属于任何键;仪表/计时器的新增与消失用
``None`` 哨兵表示(见该函数 docstring)。

红线遵守(V7 §0):

- **红线 29(零 API 破坏)**:本模块为**纯新增文件**;telemetry.py 冻结不改、
  service 不改——``/metrics`` 挂载是运营者在 service 之外的自由扩展,
  见 :func:`render_metrics` docstring;
- **红线 31(基准可复现)**::func:`kernel_selfcheck` 与
  ``tests/test_telemetry_export.py::test_v7_bench_export_parse_roundtrip``
  以**指标行数 == 计数器 + 仪表 + 4×计时器**与**(名称, 值) 往返逐对相等**
  的操作计数断言证明往返零失真,不依赖墙钟。

线程安全:全部函数为纯函数(只读入参、新建返回值),无共享可变状态。

仅使用标准库;离线运行,不发起任何网络请求。
"""
from __future__ import annotations

import re
from typing import Any

from netsentinel import telemetry

__all__ = [
    "clean_name",
    "to_prometheus",
    "diff_snaps",
    "render_metrics",
    "parse_prometheus",
    "kernel_selfcheck",
]


#: 指标名字节白名单之外 → ``_``(Prometheus 指标名规则:[a-zA-Z0-9_:])
_BAD_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_:]")

#: 解析器接受的指标名(Prometheus 规则:首字符不允许数字)
_VALID_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")

#: 空遥测名清洗后的回退名(仍以 ns_ 前缀保持来源可辨)
_EMPTY_NAME_FALLBACK = "ns_unnamed"

#: 计时器四指标:(snapshot 统计字段, 导出后缀, HELP 说明)
_TIMER_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("count", "count", "计时样本数(环形缓冲内)"),
    ("avg_ms", "avg_ms", "计时均值(毫秒)"),
    ("p95_ms", "p95_ms", "计时 p95(毫秒)"),
    ("max_ms", "max_ms", "计时最大值(毫秒)"),
)

#: 导出头(空快照时唯一输出;ts 存在时另起一行注释)
_HEADER_LINE = "# NetSentinel telemetry export(V7 A137 观测内核)"


# ---------------------------------------------------------------------------
# 名称清洗与值格式化
# ---------------------------------------------------------------------------


def clean_name(name: str) -> str:
    """遥测名 → 合法 exposition 指标名(不含 counter 的 ``_total`` 后缀)。

    规则(与模块 docstring 一致)::

        clean_name("fetch.page")   -> "fetch_page"
        clean_name("queue-depth")  -> "queue_depth"
        clean_name("9lives")       -> "ns_9lives"      # 首字符数字 → 补 ns_
        clean_name("a:b.c")        -> "a_b_c"          # 冒号合法,保留
        clean_name("空间")         -> "__"             # 非 ASCII 一律换 _
        clean_name("")             -> "ns_unnamed"     # 空名回退
    """
    cleaned = _BAD_NAME_CHARS.sub("_", str(name))
    if not cleaned:
        return _EMPTY_NAME_FALLBACK
    if cleaned[0].isdigit():
        cleaned = "ns_" + cleaned
    return cleaned


def _fmt_value(value: Any) -> str:
    """值 → exposition 文本:整值浮点压成整数,其余取 ``repr``(最短往返)。"""
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return repr(number)


def _escape_help(text: str) -> str:
    """HELP 文本转义(exposition 规则:反斜杠加倍、换行转义;防止破坏行结构)。"""
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _emit_family(lines: list[str], metric: str, mtype: str, help_text: str, value: Any) -> None:
    """一个指标族 = 一行 HELP + 一行 TYPE + 一条样本行(本内核每族恰一条样本)。"""
    lines.append(f"# HELP {metric} {help_text}")
    lines.append(f"# TYPE {metric} {mtype}")
    lines.append(f"{metric} {_fmt_value(value)}")


# ---------------------------------------------------------------------------
# to_prometheus
# ---------------------------------------------------------------------------


def to_prometheus(snapshot: dict) -> str:
    """把 :func:`netsentinel.telemetry.snapshot` 形状的 dict 渲染为 exposition 文本。

    入参为普通 dict(只读,不修改),容忍缺 ``ts`` / 缺任意节;空快照仅输出
    注释头。输出确定性:节序 counters → gauges → timers,节内按原始名排序。

    示例(输入 ``{"counters": {"fetch.page": 5}, "gauges": {"queue.pending": 7},
    "timers": {"scan.duration": {"count": 3, "avg_ms": 16.667, "p95_ms": 20.0,
    "max_ms": 20.0}}}``)::

        # NetSentinel telemetry export(V7 A137 观测内核)
        # HELP fetch_page_total 计数器 "fetch.page"
        # TYPE fetch_page_total counter
        fetch_page_total 5
        # HELP queue_pending 仪表 "queue.pending"
        # TYPE queue_pending gauge
        queue_pending 7
        # HELP scan_duration_count 计时样本数(环形缓冲内) "scan.duration"
        # TYPE scan_duration_count gauge
        scan_duration_count 3
        ... avg_ms / p95_ms / max_ms 同理 ...
    """
    lines: list[str] = [_HEADER_LINE]
    ts = snapshot.get("ts")
    if ts:
        lines.append(f"# 快照时间戳 ts={_escape_help(str(ts))}")

    for orig, value in sorted((snapshot.get("counters") or {}).items()):
        metric = clean_name(orig) + "_total"
        _emit_family(lines, metric, "counter", f'计数器 "{_escape_help(orig)}"', value)

    for orig, value in sorted((snapshot.get("gauges") or {}).items()):
        metric = clean_name(orig)
        _emit_family(lines, metric, "gauge", f'仪表 "{_escape_help(orig)}"', value)

    for orig, stats in sorted((snapshot.get("timers") or {}).items()):
        base = clean_name(orig)
        for field, suffix, label in _TIMER_FIELDS:
            metric = f"{base}_{suffix}"
            value = stats.get(field, 0) if isinstance(stats, dict) else 0
            _emit_family(
                lines, metric, "gauge", f'{label} "{_escape_help(orig)}"', value
            )

    return "\n".join(lines) + "\n"


def parse_prometheus(text: str) -> dict[str, float]:
    """极简 exposition 解析器:指标名 → 浮点值(bench 往返与消费方校验用)。

    忽略注释行(``#`` 开头)与空行;样本行必须恰为 ``<name> <value>``
    两个词,且 name 匹配 ``^[a-zA-Z_:][a-zA-Z0-9_:]*$``、value 可被
    :class:`float` 解析,否则抛 :class:`ValueError`(中文消息)。
    重名样本以最后一次出现为准(本内核不产生重名,出现即输入有重复)。
    """
    metrics: dict[str, float] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2 or not _VALID_METRIC_NAME.match(parts[0]):
            raise ValueError(f"第 {lineno} 行不是合法样本行:{raw!r}")
        try:
            metrics[parts[0]] = float(parts[1])
        except ValueError as exc:  # pragma: no cover - float 已过词法检查
            raise ValueError(f"第 {lineno} 行数值不可解析:{parts[1]!r}") from exc
    return metrics


# ---------------------------------------------------------------------------
# diff_snaps
# ---------------------------------------------------------------------------


def _timer_view(stats: Any) -> dict[str, Any]:
    """计时器统计的规范四字段视图(忽略多余键,保证比较口径稳定)。"""
    if not isinstance(stats, dict):
        return {field: 0 for field, _, _ in _TIMER_FIELDS}
    return {field: stats.get(field, 0) for field, _, _ in _TIMER_FIELDS}


def diff_snaps(a: dict, b: dict) -> dict:
    """两份 snapshot 的简明增量对比(**b 相对 a**;入参只读,不修改)。

    返回恒含四键(结构可直接 ``json.dumps``)::

        {
          "counters_added": {name: b 中的值},            # b 新出现的计数器
          "counters_delta": {name: b 值 - a 值},         # 两侧都在的计数器增量
                                                          # (零增量剔除;负增量
                                                          #  = telemetry.inc 负数)
          "gauges_changed": {name: {"from": x, "to": y}}, # 值有变的仪表;新增 →
                                                          # from=None,消失 → to=None
          "timers_changed": {name: {"from": s, "to": s}}, # count/avg_ms/p95_ms/max_ms
                                                          # 任一字段变化;缺侧为 None,
                                                          # s 为四字段视图 dict
        }

    说明:telemetry 语义里计数器只增不删,名字不会被移除(唯一例外是
    ``reset()`` 全清),故 b 中消失的计数器不属于任何键;仪表/计时器则用
    ``None`` 哨兵显式表达增删。各键内按名称排序,输出确定。
    """
    ca = a.get("counters") or {}
    cb = b.get("counters") or {}
    counters_added = {name: float(cb[name]) for name in sorted(set(cb) - set(ca))}
    counters_delta: dict[str, float] = {}
    for name in sorted(set(ca) & set(cb)):
        delta = float(cb[name]) - float(ca[name])
        if delta != 0.0:
            counters_delta[name] = delta

    ga = a.get("gauges") or {}
    gb = b.get("gauges") or {}
    gauges_changed: dict[str, dict[str, Any]] = {}
    for name in sorted(set(ga) | set(gb)):
        old, new = ga.get(name), gb.get(name)
        if old != new:
            gauges_changed[name] = {
                "from": None if old is None else float(old),
                "to": None if new is None else float(new),
            }

    ta = a.get("timers") or {}
    tb = b.get("timers") or {}
    timers_changed: dict[str, dict[str, Any]] = {}
    for name in sorted(set(ta) | set(tb)):
        old = _timer_view(ta[name]) if name in ta else None
        new = _timer_view(tb[name]) if name in tb else None
        if old != new:
            timers_changed[name] = {"from": old, "to": new}

    return {
        "counters_added": counters_added,
        "counters_delta": counters_delta,
        "gauges_changed": gauges_changed,
        "timers_changed": timers_changed,
    }


# ---------------------------------------------------------------------------
# render_metrics:service 挂 /metrics 的直通入口
# ---------------------------------------------------------------------------


def render_metrics() -> str:
    """``telemetry.snapshot()`` → :func:`to_prometheus` 直通(即时渲染,无缓存)。

    **service 挂载说明(不改 service,红线 29——本函数只是取数直通,
    挂载由运营者在 service 之外自由完成)**::

        # service/app.py 的 create_app(cfg) 返回 FastAPI 实例且已 __all__ 导出,
        # 运营者可在自己的启动脚本/独立模块里追加路由,零侵入:
        from fastapi import Response

        from netsentinel.telemetry_export import render_metrics
        from service.app import create_app

        app = create_app(cfg)

        @app.get("/metrics")
        def metrics() -> Response:
            return Response(
                render_metrics(),
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )

    要点:

    - ``/metrics`` 只读本函数输出,**不含 URL/密钥/路径**等内容字段
      (telemetry 只存名称与数字,红线 17);
    - 端点应同样遵守 service 安全红线:仅本机回环访问、无鉴权即不暴露
      到不可信网络;
    - 若用非 FastAPI 的静态方案,可周期性把 ``render_metrics()`` 写入文件
      由任意 HTTP 静态服务暴露(输出逐字节确定,便于增量同步)。
    """
    return to_prometheus(telemetry.snapshot())


# ---------------------------------------------------------------------------
# 离线自检(A138 基准总控调用;红线 31:纯计数断言,不依赖墙钟)
# ---------------------------------------------------------------------------


def kernel_selfcheck() -> dict[str, Any]:
    """离线可复现微基准:导出 → 解析 往返保真率。

    构造 40 计数器 + 30 仪表 + 20 计时器 = **150 条指标样本**的确定性快照
    (值全部取二进制可精确表示的 0.5 步长,无浮点噪声),导出后用
    :func:`parse_prometheus` 解析,断言:

    - 非注释行数 == 40 + 30 + 4×20 == 150(操作计数);
    - 解析恢复的 ``(名称, 值)`` 与期望**逐对完全相等**(往返零失真)。

    返回 ``{"name", "metric", "value", "baseline"}``:value = 正确恢复的
    样本对数 ÷ 导出样本总数,baseline = 1.0(理想往返)。
    """
    n_counters, n_gauges, n_timers = 40, 30, 20
    snap: dict[str, Any] = {
        "ts": "2026-10-02T00:00:00+08:00",
        "counters": {f"selfcheck.counter.{i:02d}": i * 0.5 for i in range(n_counters)},
        "gauges": {f"selfcheck.gauge.{i:02d}": (i % 7) * 0.5 for i in range(n_gauges)},
        "timers": {
            f"selfcheck.timer.{i:02d}": {
                "count": i % 5,
                "avg_ms": (i % 9) * 0.5,
                "p95_ms": (i % 11) * 0.5,
                "max_ms": (i % 13) * 0.5,
            }
            for i in range(n_timers)
        },
    }
    expected: dict[str, float] = {}
    for i in range(n_counters):
        expected[f"selfcheck_counter_{i:02d}_total"] = i * 0.5
    for i in range(n_gauges):
        expected[f"selfcheck_gauge_{i:02d}"] = (i % 7) * 0.5
    for i in range(n_timers):
        for field, suffix, _label in _TIMER_FIELDS:
            expected[f"selfcheck_timer_{i:02d}_{suffix}"] = float(
                snap["timers"][f"selfcheck.timer.{i:02d}"][field]
            )

    text = to_prometheus(snap)
    sample_lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert len(sample_lines) == n_counters + n_gauges + 4 * n_timers == 150, (
        f"指标行数 {len(sample_lines)} != 150(往返前提被破坏)"
    )

    parsed = parse_prometheus(text)
    assert len(parsed) == len(expected), "解析恢复的指标个数与导出不一致"
    recovered = sum(1 for name, value in expected.items() if parsed.get(name) == value)
    assert recovered == len(expected), "存在往返失真的 (名称, 值) 对"
    return {
        "name": "telemetry_export",
        "metric": "roundtrip_pair_recovery_ratio",
        "value": round(recovered / len(expected), 6),
        "baseline": 1.0,
    }
