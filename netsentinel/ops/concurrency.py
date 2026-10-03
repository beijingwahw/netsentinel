"""NetSentinel(净网哨兵)统一并发执行器工厂(ops.concurrency,A164)。

V9「CPU 自适应三档并发」的**执行侧收口**:按 ``cfg.concurrency_tier`` /
``cfg.cpu_reserve``(契约 §1 权威三档公式)解析出 workers 数,并产出
成对的线程池 / 进程池**上下文管理器**——退出必关闭,不留孤儿线程/进程
(红线 37:失控不得留孤儿进程/线程)。

核心 API(契约 §2 A164 行):

- :func:`io_workers(cfg) <io_workers>`:IO 型(网络抓取 / VLM 请求等待)
  workers,= A163 ``cpu_profile.tier_workers(tier, reserve=cpu_reserve)``;
- :func:`cpu_workers(cfg) <cpu_workers>`:进程池 workers,
  = ``min(io_workers, cores)``——**进程池永不超过物理核数**(红线 37);
- :func:`thread_pool(cfg, label) <thread_pool>`:线程池上下文管理器
  (``max_workers=io_workers``、线程名前缀 ``label``,退出必 shutdown);
- :func:`process_pool(cfg, label, mp_context) <process_pool>`:进程池
  上下文管理器(``max_workers=cpu_workers``,Windows spawn 兼容);
- :func:`workers_for(cfg, kind) <workers_for>`:按用途分派上述两者。

设计要点:

1. **惰性依赖 A163**:``netsentinel.ops.cpu_profile``(并行开发 A163,
   可能就位也可能缺席)一律惰性导入;缺席或不可调用时按契约 §1 的
   **内置公式兜底**(与 ``cpu_profile.tier_workers`` 同一条公式,二者
   结论恒一致)。核数探测同理:优先 ``cpu_profile.detect()["cores"]``,
   缺席回退 ``os.cpu_count()``,再回退 2(§1);
2. **tier 非法 → 中文 ValueError**:本模块自行校验(先于任何委托),
   报错信息给出三档取值,不依赖兄弟模块的报错文案;
3. **Windows spawn 兼容(重要)**:进程池仅用于模块级纯函数的本地计算
   (红线 35,不 pickle 分类器实例 / 连接);``spawn`` 启动会在子进程内
   重新导入 ``__main__``,因此 :func:`process_pool` **只应在调用方处于
   ``if __name__ == "__main__":`` 守卫内的程序入口使用,或由测试显式
   注入 ``mp_context``**;构造失败(如无守卫环境下的 spawn 异常)统一
   转成中文 :class:`RuntimeError` 提示(保留原始异常为 ``__cause__``,
   **不自动吞**),绝不静默降级;
4. **退出必关闭**:两个工厂都用 ``try/finally`` 保证 ``shutdown(wait=True)``,
   with 体抛异常同样关闭,不留孤儿;

测试约定:``tests/test_concurrency.py`` 全离线(核数 monkeypatch 固定,
A163 双态——缺席走内置公式 / 注入 fake 走委托路径,结论一致);真进程
用例显式注入 ``mp_context=spawn`` 并只提交模块级纯函数。
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from netsentinel.contracts import Config

__all__ = [
    "TIERS",
    "io_workers",
    "cpu_workers",
    "workers_for",
    "thread_pool",
    "process_pool",
]

logger = logging.getLogger(__name__)

#: 合法的并发档位(契约 §1 权威三档:low / mid / high)
TIERS = ("low", "mid", "high")

#: 核数探测兜底值(§1:``os.cpu_count()`` 返回 None 时按 2)
FALLBACK_CORES = 2

#: 线程池缺省名前缀(与线程名前缀 ``thread_name_prefix`` 同值)
DEFAULT_LABEL = "ns"


# ---------------------------------------------------------------------------
# 核数探测与档位公式(惰性优先 A163,缺席内置兜底——同 §1)
# ---------------------------------------------------------------------------
def _cores() -> int:
    """当前逻辑核数:优先 A163 ``cpu_profile.detect()["cores"]``(惰性导入,
    兄弟可能缺席),缺席或异常回退 ``os.cpu_count()``,再回退 2(§1)。

    返回恒 ≥ 1,供 :func:`cpu_workers` 的红线 37 钳制使用。
    """
    try:
        from netsentinel.ops import cpu_profile  # 惰性:A163 并行开发,可能缺席
    except ImportError:
        cpu_profile = None  # type: ignore[assignment]
    if cpu_profile is not None:
        try:
            info = cpu_profile.detect() or {}
            cores = int(info.get("cores", 0) or 0)
            if cores > 0:
                return cores
        except Exception as exc:  # noqa: BLE001 - 探测失败仅降级,不阻断
            logger.warning("cpu_profile.detect() 调用失败,核数回退 os.cpu_count():%s", exc)
    return os.cpu_count() or FALLBACK_CORES


def _validate_tier(tier: Any) -> str:
    """校验并发档位;非法值抛中文 ValueError(先于任何 A163 委托)。"""
    if tier not in TIERS:
        raise ValueError(
            f"并发档位 {tier!r} 无效:必须为 low / mid / high 三档之一"
            "(对应 cores//4 / cores//2 / cores-reserve 个 workers)"
        )
    return tier  # type: ignore[return-value]


def _builtin_tier_workers(tier: str, *, reserve: int = 1, cores: int | None = None) -> int:
    """契约 §1 内置兜底公式(与 A163 ``tier_workers`` 逐条一致):

    - low:max(1, N//4) —— 后台/省电;
    - mid:max(1, N//2) —— 默认;
    - high:max(1, N - reserve) —— 最大限度压榨(reserve=0 即全核)。
    """
    n = int(cores) if cores and int(cores) > 0 else (os.cpu_count() or FALLBACK_CORES)
    try:
        r = max(0, int(reserve))
    except (TypeError, ValueError):
        r = 1
    if tier == "low":
        return max(1, n // 4)
    if tier == "mid":
        return max(1, n // 2)
    return max(1, n - r)  # high


def _tier_workers_fn() -> Callable[..., int] | None:
    """惰性取 A163 ``cpu_profile.tier_workers``;缺席或不可调用返回 None。"""
    try:
        from netsentinel.ops import cpu_profile  # 惰性:A163 并行开发,可能缺席
    except ImportError:
        return None
    fn = getattr(cpu_profile, "tier_workers", None)
    return fn if callable(fn) else None


def _reserve_of(cfg: Config) -> int:
    """读 ``cfg.cpu_reserve``(高档保留核数);读不到/非法按缺省 1。"""
    try:
        return int(getattr(cfg, "cpu_reserve", 1))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1


# ---------------------------------------------------------------------------
# workers 解析
# ---------------------------------------------------------------------------
def io_workers(cfg: Config) -> int:
    """IO 型 workers 数(线程池规模):= A163 ``tier_workers(tier, reserve)``。

    委托时统一由本模块探测核数并经 ``cores=`` 传入(单一核数来源,
    保证与 :func:`cpu_workers` 的钳制基准一致);A163 缺席或调用异常时
    按 :func:`_builtin_tier_workers`(§1 同款公式)兜底。tier 非法抛
    中文 ValueError。
    """
    tier = _validate_tier(getattr(cfg, "concurrency_tier", "mid"))
    reserve = _reserve_of(cfg)
    fn = _tier_workers_fn()
    if fn is not None:
        try:
            return max(1, int(fn(tier, reserve=reserve, cores=_cores())))
        except Exception as exc:  # noqa: BLE001 - 兄弟异常仅降级到内置公式
            logger.warning(
                "cpu_profile.tier_workers 调用失败,按 §1 内置公式兜底:%s", exc
            )
    return _builtin_tier_workers(tier, reserve=reserve, cores=_cores())


def cpu_workers(cfg: Config) -> int:
    """进程池 workers 数:``min(io_workers(cfg), cores)``——**进程池永不超过
    物理核数**(红线 37:任一执行器 workers ≤ cpu 核数)。tier 非法抛
    中文 ValueError(经 :func:`io_workers` 校验透传)。
    """
    n = io_workers(cfg)
    cores = _cores()
    if n > cores:
        logger.warning(
            "档位 workers=%d 超过物理核数 %d,进程池按红线 37 钳制为 %d",
            n, cores, cores,
        )
        return cores
    return n


def workers_for(cfg: Config, kind: str = "io") -> int:
    """按用途分派 workers 数:``kind="io"`` → :func:`io_workers`(线程池),
    ``kind="cpu"`` → :func:`cpu_workers`(进程池);其他取值抛中文
    ValueError。
    """
    if kind == "io":
        return io_workers(cfg)
    if kind == "cpu":
        return cpu_workers(cfg)
    raise ValueError(
        f"并发类型 kind={kind!r} 无效:必须为 'io'(线程池)或 'cpu'(进程池)"
    )


# ---------------------------------------------------------------------------
# 执行器工厂(上下文管理器:退出必关闭,不留孤儿)
# ---------------------------------------------------------------------------
@contextmanager
def thread_pool(cfg: Config, *, label: str = DEFAULT_LABEL) -> Iterator[ThreadPoolExecutor]:
    """线程池上下文管理器:``ThreadPoolExecutor(max_workers=io_workers(cfg),
    thread_name_prefix=label)``。

    - 规模 = :func:`io_workers`(IO 型:抓取 / VLM 等待,**不受红线 35
      放宽的礼貌频控影响**,那些在提交侧节奏里);
    - 线程名前缀 ``label``(缺省 ``"ns"``)便于日志归因;
    - 退出(含 with 体抛异常)**必 shutdown(wait=True)**,不留孤儿线程
      (红线 37)。

    用法::

        with thread_pool(cfg, label="scan") as pool:
            futs = [pool.submit(fn, url) for url in urls]
            results = [f.result() for f in futs]
    """
    n = io_workers(cfg)
    executor = ThreadPoolExecutor(max_workers=n, thread_name_prefix=label)
    logger.debug("线程池 %s 创建:max_workers=%d", label, n)
    try:
        yield executor
    finally:
        executor.shutdown(wait=True)


@contextmanager
def process_pool(
    cfg: Config, *, label: str = DEFAULT_LABEL, mp_context: Any = None
) -> Iterator[ProcessPoolExecutor]:
    """进程池上下文管理器:``ProcessPoolExecutor(max_workers=cpu_workers(cfg),
    mp_context=mp_context)``(规模永不超过物理核,红线 37)。

    **Windows spawn 兼容(必读)**:``spawn`` 启动会在子进程内重新导入
    ``__main__``,因此本工厂**仅当调用方处于 ``if __name__ == "__main__":``
    守卫内的程序入口时可用**,或由测试显式注入 ``mp_context``(如
    ``multiprocessing.get_context("spawn")``)。提交的任务必须是**模块级
    纯函数**(红线 35:本地计算,不 pickle 分类器实例 / 连接)。

    构造失败(如无守卫环境下 spawn 反复导入 ``__main__`` 失败)统一转成
    中文 :class:`RuntimeError` 提示并保留原始异常(``__cause__``),
    **不自动吞、不静默降级**;退出(含 with 体抛异常)必
    ``shutdown(wait=True)``,不留孤儿进程(红线 37)。

    :param cfg: 全局配置(取 ``concurrency_tier`` / ``cpu_reserve``)。
    :param label: 池标签(日志归因;进程本身无线程名前缀概念)。
    :param mp_context: 可选 ``multiprocessing`` 上下文(测试显式注入以
        固定 spawn / fork 语义);None 用平台缺省(Windows 即 spawn)。
    """
    n = cpu_workers(cfg)
    try:
        executor = ProcessPoolExecutor(max_workers=n, mp_context=mp_context)
    except Exception as exc:  # noqa: BLE001 - 统一转中文 RuntimeError(不吞:原异常入 __cause__)
        raise RuntimeError(
            f"进程池 {label!r} 创建失败(max_workers={n}):Windows spawn 要求"
            "程序入口位于 if __name__ == '__main__': 守卫内,或由测试显式注入"
            f"mp_context;请检查入口守卫与任务函数可 pickle 性。原始错误:{exc}"
        ) from exc
    logger.debug("进程池 %s 创建:max_workers=%d", label, n)
    try:
        yield executor
    finally:
        executor.shutdown(wait=True)
