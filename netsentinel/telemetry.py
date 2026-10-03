"""NetSentinel 轻量遥测核心(V5 · 项目负责人)。

零依赖、线程安全的进程内指标:计数器(counter)/ 仪表(gauge)/ 计时样本(timer)。

任何模块可自由接入::

    from netsentinel import telemetry

    telemetry.inc("fetch.page")                       # 计数
    telemetry.gauge("queue.pending", 3)               # 瞬时值
    with telemetry.timer("scan.duration"):            # 计时(秒)
        ...
    telemetry.snapshot()                              # 汇总视图

不接入的模块不受任何影响。设计约束:

- 只存**名称与数字**,绝不存 URL/密钥/路径等内容字段(红线 17 由调用方保证);
- 单进程内存态,采样环形截断(:data:`MAX_SAMPLES`),不产生后台线程;
- ``reset()`` 仅供测试使用。
"""
from __future__ import annotations

import datetime as _dt
import json
import threading
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator

__all__ = [
    "MAX_SAMPLES",
    "inc",
    "gauge",
    "observe",
    "timer",
    "timed",
    "snapshot",
    "reset",
    "export_jsonl",
]

#: 每个计时指标保留的最近样本数(环形截断,防内存无界)
MAX_SAMPLES = 256

_LOCK = threading.Lock()
_COUNTERS: dict[str, float] = {}
_GAUGES: dict[str, float] = {}
_TIMERS: dict[str, list[float]] = {}


def inc(name: str, amount: float = 1.0) -> None:
    """计数器累加(负数递减亦合法)。"""
    with _LOCK:
        _COUNTERS[name] = _COUNTERS.get(name, 0.0) + float(amount)


def gauge(name: str, value: float) -> None:
    """记录瞬时值(后值覆盖前值)。"""
    with _LOCK:
        _GAUGES[name] = float(value)


def observe(name: str, seconds: float) -> None:
    """手动提交一个计时样本(秒);配合 :func:`timer` 通常无需直接调用。"""
    with _LOCK:
        samples = _TIMERS.setdefault(name, [])
        samples.append(float(seconds))
        if len(samples) > MAX_SAMPLES:
            del samples[: len(samples) - MAX_SAMPLES]


@contextmanager
def timer(name: str) -> Iterator[None]:
    """计时上下文管理器::

        with telemetry.timer("packager.duration"):
            build_bundle(...)
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        observe(name, time.perf_counter() - start)


def timed(name: str) -> Callable[[Callable], Callable]:
    """计时装饰器::

        @telemetry.timed("fetch.page")
        def fetch_page(url, cfg): ...
    """

    def deco(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with timer(name):
                return func(*args, **kwargs)

        return wrapper

    return deco


def _stats(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {"count": 0, "avg_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
    ordered = sorted(samples)
    p95_index = max(0, min(len(ordered) - 1, round(0.95 * len(ordered)) - 1))
    return {
        "count": len(samples),
        "avg_ms": round(sum(samples) / len(samples) * 1000, 3),
        "p95_ms": round(ordered[p95_index] * 1000, 3),
        "max_ms": round(ordered[-1] * 1000, 3),
    }


def snapshot() -> dict[str, Any]:
    """当前全部指标的只读汇总(计数器原值、计时器统计为毫秒)。"""
    with _LOCK:
        return {
            "ts": _dt.datetime.now(_dt.timezone.utc)
            .astimezone()
            .isoformat(timespec="seconds"),
            "counters": dict(_COUNTERS),
            "gauges": dict(_GAUGES),
            "timers": {name: _stats(list(s)) for name, s in _TIMERS.items()},
        }


def reset() -> None:
    """清空全部指标(仅测试使用)。"""
    with _LOCK:
        _COUNTERS.clear()
        _GAUGES.clear()
        _TIMERS.clear()


def export_jsonl(path: str) -> dict[str, Any]:
    """把 :func:`snapshot` 追加为一行 JSON(自动建目录);返回该快照。"""
    snap = snapshot()
    target = Path(path)
    if str(target.parent):
        target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(snap, ensure_ascii=False) + "\n")
    return snap
