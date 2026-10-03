# -*- coding: utf-8 -*-
"""NetSentinel(净网哨兵)三档并发基准(benchmarks.tier_bench,A173)。

V9「CPU 自适应三档并发」的**量化证明工具**:用 **Barrier 计数法**在线程池
上实测各档位的并发峰值,并与 CONTRACTS-V9 §1 权威公式的理论 workers 逐档
对照——**峰值=Barrier 实测并发度,确定性计数非墙钟**(红线 31:不测耗时、
不与任何钟表读数比较,同机同参数两次运行结论恒一致,报告逐字节可复现)。

核心 API(契约 §2 A173 行):

- :func:`peak_concurrency(task_fn, n, workers, timeout_s) <peak_concurrency>`:
  在 ``workers`` 个线程的线程池里跑 ``n`` 个 ``task_fn``。task_fn 约定签名
  ``task_fn(barrier, timeout_s)``,**进入时在共享 Barrier(workers 方)上
  等待**:

  - 全部 ``workers`` 方同时到达 → Barrier 放行,即证明恰有 ``workers`` 个
    任务并发在跑(峰值=workers,确定性到达计数);
  - 部分等待超时(n < workers、或个别任务抛异常没到达)→ 栏破,按**实际
    安全到达数**计峰值——计数器只统计真正进入 Barrier 等待的任务,异常
    任务不计入,不虚报;

  每次等待都带 ``timeout_s`` 兜底(超时保护:破栏必返,不挂死),线程池
  退出必 ``shutdown(wait=True)``,不留孤儿线程(红线 37)。基准自身提供
  符合约定的标准任务 :func:`_barrier_task`;调用方也可注入自定义 task_fn
  (如故障注入:让个别任务抛异常,验证峰值按安全到达计数)。
- :func:`run(out_dir, cores) <run>`:三档(low / mid / high)各跑一次
  :func:`peak_concurrency` → 返回表 ``{tier: {"workers_expected", "peak"}}``,
  并落盘中文报告 ``tier_report.md`` + ``tier_report.json``;``cores`` 显式
  注入优先,缺省自动探测(A163 ``detect``);
- :func:`main`:CLI(``--out`` / ``--cores``),三档实测峰值与理论全部一致 →
  退出码 0,存在不一致 → 2。

三档理论值来源:**惰性优先** A163 ``netsentinel.ops.cpu_profile.tier_workers``
(兄弟模块只读、可能缺席),缺席时按 §1 内置公式兜底,二者逐档一致:

===== ======== ======== ===================
N     low      mid      high(reserve=1)
===== ======== ======== ===================
4     1        2        3
8     2        4        7
16    4        8        15
32    8        16       31
===== ======== ======== ===================

红线关联:

- 红线 31(基准可复现):全程操作计数口径(Barrier 到达计数),零墙钟
  断言——本模块不 import 任何计时/基准库,报告不含时间戳,同参数两次
  运行产出逐字节一致;
- 红线 35(压榨边界):本基准只度量**本地线程池**的并发度,不触碰任何
  对外网络频控(fetch_delay_s / 引擎限速 / 举报频控与档位无关,一概
  不放宽);
- 红线 37(资源治理):任务总量有上限(每档 n=workers×2,两轮循环
  Barrier)、每次 Barrier 等待有超时兜底、线程池退出必关闭,实测峰值
  恒 ≤ 核数。

命令行::

    python benchmarks/tier_bench.py --out benchmarks/out --cores 8
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 直接以脚本运行(python benchmarks/tier_bench.py)时,保证项目根在
# sys.path 上,使 netsentinel 各模块可导入;经包导入(tests)时为空操作。
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "TIERS",
    "peak_concurrency",
    "render_markdown",
    "run",
    "main",
]

#: 每次 Barrier 等待的超时兜底秒数(仅安全网:正常放行是确定性计数,与耗时无关)
DEFAULT_TIMEOUT_S = 10.0

#: 合法的并发档位(契约 §1 权威三档:low / mid / high)
TIERS = ("low", "mid", "high")

#: 核数探测兜底值(§1:``os.cpu_count()`` 返回 None 时按 2)
FALLBACK_CORES = 2

#: 高档保留核心数(与 ``Config.cpu_reserve`` 缺省一致;low/mid 不受影响)
DEFAULT_RESERVE = 1

#: 报告表头的档位中文注记
_TIER_TEXT = {
    "low": "low(低·省电)",
    "mid": "mid(中·默认)",
    "high": "high(高·压榨)",
}

#: task_fn 约定签名:进入时在共享 Barrier(workers 方)上等待
_TaskFn = Callable[["_CountingBarrier", float], Any]


# ---------------------------------------------------------------------------
# Barrier 计数法核心:到达计数代理 + 标准任务
# ---------------------------------------------------------------------------
class _CountingBarrier:
    """带**到达计数**的 Barrier 代理(红线 31:计数口径,非墙钟)。

    包装 ``threading.Barrier(parties)``,在每次 :meth:`wait` 前后维护
    「当前正在 Barrier 上等待的任务数」(``_waiting``)与「历史同时等待
    峰值」(``_peak``)——峰值即实测并发度:

    - 全部 ``parties`` 方同时到达 → 底层 Barrier 放行,此刻等待数恰为
      ``parties``,证明恰有这么多任务并发;
    - 部分等待超时 → 底层栏破,等待数停在**实际安全到达数**上(没到达
      的、抛异常的任务从不进入计数,不虚报)。

    计数只在自持锁内做增减,绝不跨越底层等待持锁;``threading.Barrier``
    本身可循环复用(放行后自动复位),支持 n > workers 的多轮任务。
    """

    __slots__ = ("_barrier", "_lock", "_waiting", "_peak")

    def __init__(self, parties: int) -> None:
        self._barrier = threading.Barrier(parties)
        self._lock = threading.Lock()
        self._waiting = 0
        self._peak = 0

    @property
    def parties(self) -> int:
        """Barrier 方数(= 目标并发度 workers)。"""
        return self._barrier.parties

    @property
    def peak(self) -> int:
        """历史同时等待峰值(= 实测并发度;加锁快照,任务全部结束后为定值)。"""
        with self._lock:
            return self._peak

    def wait(self, timeout: float | None = None) -> int:
        """到达并等待:进入时计数 +1 并刷新峰值,底层等待返回/破栏后 -1。

        :param timeout: 超时兜底秒数(None 表示不限;基准链路恒传有限值,
            保证部分到达时必破栏返回、不挂死)。
        :return: 底层 ``Barrier.wait`` 的到达序号(0..parties-1)。
        :raises threading.BrokenBarrierError: 部分等待超时,或栏已被此前
            的破栏打破(与原生 Barrier 语义一致;是否吞掉由 task_fn 决定)。
        """
        with self._lock:
            self._waiting += 1
            if self._waiting > self._peak:
                self._peak = self._waiting
        try:
            return self._barrier.wait(timeout)
        finally:
            with self._lock:
                self._waiting -= 1


def _barrier_task(barrier: _CountingBarrier, timeout_s: float) -> int:
    """标准基准任务(契约约定的 canonical 形态):进入即在共享 Barrier 上等待。

    - 全部 workers 方到达 → Barrier 放行,证明恰有 workers 并发;
    - 部分等待超时 → ``BrokenBarrierError`` 在任务内**就地吞掉**按 0 返回
      (破栏不算任务错误:峰值由 :class:`_CountingBarrier` 的到达计数负责,
      不需要任务自己报数)。
    """
    try:
        barrier.wait(timeout_s)
    except threading.BrokenBarrierError:
        pass
    return 0


# ---------------------------------------------------------------------------
# peak_concurrency:线程池跑 n 个 task_fn,返回 Barrier 到达计数峰值
# ---------------------------------------------------------------------------
def _validate_bench_args(
    task_fn: Any, n: Any, workers: Any, timeout_s: Any
) -> tuple[int, int]:
    """入参校验(容错取向:能折成合法值就折,折不动抛中文 ValueError)。"""
    if not callable(task_fn):
        raise ValueError(f"task_fn={task_fn!r} 无效:必须是可调用对象")
    try:
        n_i, w_i = int(n), int(workers)
    except (TypeError, ValueError):
        raise ValueError(
            f"基准参数非法:n={n!r}、workers={workers!r},都必须是 ≥1 的整数"
        ) from None
    if n_i < 1:
        raise ValueError(f"任务数 n={n!r} 无效:必须是 ≥1 的整数")
    if w_i < 1:
        raise ValueError(f"线程数 workers={workers!r} 无效:必须是 ≥1 的整数")
    try:
        t = float(timeout_s)
    except (TypeError, ValueError):
        raise ValueError(f"超时 timeout_s={timeout_s!r} 无效:必须是 >0 的秒数") from None
    if not t > 0.0:  # 含 NaN:NaN>0 为 False,一并拒绝
        raise ValueError(f"超时 timeout_s={timeout_s!r} 无效:必须是 >0 的秒数")
    return n_i, w_i


def peak_concurrency(
    task_fn: _TaskFn,
    n: int,
    workers: int,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> int:
    """在 ``workers`` 线程的线程池上跑 ``n`` 个 ``task_fn``,返回实测并发峰值。

    **Barrier 计数法**(确定性计数,非墙钟):

    1. 建一个 ``workers`` 方的共享 :class:`_CountingBarrier`(到达计数
       代理),线程池 ``max_workers=workers``;
    2. 逐个提交 ``n`` 个包装任务:调用 ``task_fn(barrier, timeout_s)``——
       按约定,任务进入时在共享 Barrier 上等待;
    3. 全部 ``workers`` 方同时到达 → Barrier 放行,恰证 ``workers`` 并发,
       峰值=workers;部分等待超时(任务不足 / 个别任务抛异常没到达)→
       栏破,峰值=**实际安全到达数**(异常任务不进入计数,不虚报);
    4. ``shutdown(wait=True)`` 收池(每次等待都有 ``timeout_s`` 兜底,任务
       必然全部终止,不挂死、不留孤儿线程,红线 37)。

    task_fn 的异常被收入 future 不向上抛(调用 :func:`_barrier_task`
    时破栏已就地吞掉;自定义 task_fn 抛出的异常同样不干扰计数、不挂死
    本函数)。线程池真实并发度受 ``max_workers=workers`` 钳制,故峰值
    恒 ≤ workers(红线 37 的执行侧对应)。

    :param task_fn: 符约定的任务函数 ``task_fn(barrier, timeout_s)``;
        基准自身提供标准实现 :func:`_barrier_task`,调用方可注入自定义
        (如故障注入)。
    :param n: 要跑的任务总数(≥1;n < workers 时 Barrier 必部分超时,
        峰值=实际到达数)。
    :param workers: 线程池线程数 = Barrier 方数(≥1)。
    :param timeout_s: 每次 Barrier 等待的超时兜底秒数(>0;缺省 10.0)。
    :return: 实测并发峰值(``_CountingBarrier`` 到达计数,恒 ≥0;标准
        任务下确定性等于 workers)。
    :raises ValueError: 入参非法(中文报错)。

    用法示例::

        peak = peak_concurrency(_barrier_task, n=8, workers=4)  # → 4
    """
    n_i, w_i = _validate_bench_args(task_fn, n, workers, timeout_s)
    barrier = _CountingBarrier(w_i)

    def _run() -> Any:
        return task_fn(barrier, timeout_s)

    executor = ThreadPoolExecutor(max_workers=w_i, thread_name_prefix="ns-tier-bench")
    try:
        for _ in range(n_i):
            executor.submit(_run)  # 异常留在 future 里:峰值只按安全到达计数
    finally:
        executor.shutdown(wait=True)  # 超时兜底保证每个任务必然终止,不挂死
    return barrier.peak


# ---------------------------------------------------------------------------
# 三档理论值(惰性优先 A163 cpu_profile,缺席内置 §1 公式兜底——同款)
# ---------------------------------------------------------------------------
def _validate_tier(tier: Any) -> str:
    """校验并发档位;非法值抛中文 ValueError(先于任何 A163 委托)。"""
    if tier in TIERS:
        return tier  # type: ignore[return-value]
    raise ValueError(f"并发档位 {tier!r} 无效:必须为 low / mid / high 三档之一")


def _detect_cores() -> int:
    """当前逻辑核数:优先 A163 ``cpu_profile.detect()["cores"]``(惰性导入,
    兄弟可能缺席),缺席或异常回退 ``os.cpu_count()``,再回退 2(§1)。"""
    try:
        from netsentinel.ops import cpu_profile  # 惰性:A163 并行开发,可能缺席
    except ImportError:
        cpu_profile = None  # type: ignore[assignment]
    if cpu_profile is not None:
        try:
            cores = int((cpu_profile.detect() or {}).get("cores", 0) or 0)
            if cores > 0:
                return cores
        except Exception:  # noqa: BLE001 - 探测失败仅降级,不阻断基准
            pass
    return os.cpu_count() or FALLBACK_CORES


def _resolve_cores(cores: int | None) -> int:
    """解析核数:``None`` → 自动探测(:func:`_detect_cores`);显式值必须
    是 ≥1 的整数(注入用,非法抛中文 ValueError)。"""
    if cores is None:
        return _detect_cores()
    try:
        n = int(cores)
    except (TypeError, ValueError):
        raise ValueError(f"核数 cores={cores!r} 无效:必须是 ≥1 的整数(注入显式值)") from None
    if n < 1:
        raise ValueError(f"核数 cores={cores!r} 无效:必须是 ≥1 的整数(注入显式值)")
    return n


def _builtin_tier_workers(tier: str, *, reserve: int, cores: int) -> int:
    """契约 §1 内置兜底公式(与 A163 ``tier_workers`` 逐条一致):

    low=``max(1, N//4)``、mid=``max(1, N//2)``、high=``max(1, N-reserve)``。
    """
    r = max(0, int(reserve))
    if tier == "low":
        return max(1, cores // 4)
    if tier == "mid":
        return max(1, cores // 2)
    return max(1, cores - r)  # high


def _tier_workers(tier: str, *, cores: int) -> int:
    """三档理论 workers(A163 优先,缺席兜底;红线 37:结果恒 1 ≤ w ≤ cores)。"""
    tier = _validate_tier(tier)
    try:
        from netsentinel.ops import cpu_profile  # 惰性:A163 并行开发,可能缺席
    except ImportError:
        cpu_profile = None  # type: ignore[assignment]
    if cpu_profile is not None:
        fn = getattr(cpu_profile, "tier_workers", None)
        if callable(fn):
            try:
                return max(1, int(fn(tier, reserve=DEFAULT_RESERVE, cores=cores)))
            except Exception:  # noqa: BLE001 - 兄弟异常仅降级到内置公式
                pass
    return _builtin_tier_workers(tier, reserve=DEFAULT_RESERVE, cores=cores)


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------
def render_markdown(payload: dict[str, Any]) -> str:
    """把 run 的 payload 渲染为中文 Markdown 报告(单遍拼接,无外部依赖)。"""
    tiers = payload["tiers"]
    lines: list[str] = []
    lines.append("# NetSentinel 三档并发基准报告(A173)")
    lines.append("")
    lines.append(f"- 核数(cores):{payload['cores']}(来源:{payload['cores_source']})")
    lines.append("- 任务总量:每档 n=workers×2(两轮循环 Barrier,红线 37 总量与超时上限)")
    lines.append(
        "- 方法:**峰值=Barrier 实测并发度,确定性计数非墙钟**——workers 个任务"
        "同时在共享 Barrier 上到达才放行,放行即证明恰有 workers 并发"
    )
    lines.append("")
    lines.append("## 一、三档对照表")
    lines.append("")
    lines.append("| 档位 | 理论 workers | 实测峰值(Barrier) | 判定 |")
    lines.append("| --- | --- | --- | --- |")
    for tier in TIERS:
        row = tiers[tier]
        ok = row["peak"] == row["workers_expected"]
        lines.append(
            "| {} | {} | {} | {} |".format(
                _TIER_TEXT[tier], row["workers_expected"], row["peak"], "一致" if ok else "不一致"
            )
        )
    lines.append("")
    lines.append("## 二、口径说明")
    lines.append("")
    lines.append("- 峰值=Barrier 实测并发度,确定性计数非墙钟(红线 31);")
    lines.append(
        "- 三档公式(§1):low=max(1, N//4)、mid=max(1, N//2)、high=max(1, N-reserve),"
        f"本报告 reserve={DEFAULT_RESERVE};"
    )
    lines.append("- 部分等待超时按实际安全到达数计,异常任务不计入峰值(不虚报);")
    lines.append(
        "- 红线 35:本基准只度量本地线程池,不放宽任何对外频控;红线 37:实测峰值"
        "恒 ≤ 核数,线程池退出必关闭,不留孤儿线程。"
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主流程与 CLI
# ---------------------------------------------------------------------------
def run(out_dir: str | Path = "benchmarks/out", *, cores: int | None = None) -> dict:
    """三档各跑一次 :func:`peak_concurrency`,落盘报告,返回三档对照表。

    流程:解析核数(``cores`` 显式注入优先,缺省 ``detect`` 自动探测)→
    对 low / mid / high 三档分别换算理论 workers(§1 公式)→ 各跑
    ``peak_concurrency(_barrier_task, n=workers×2, workers)``(标准任务,
    两轮循环 Barrier)→ 返回表 ``{tier: {"workers_expected", "peak"}}``
    (恰三键 low / mid / high,每行恰 ``workers_expected`` / ``peak`` 两键)。

    产出 ``out_dir/tier_report.md``(中文对照表 + 口径说明)与
    ``out_dir/tier_report.json``(同源 payload);目录不存在则创建。报告
    不含时间戳,同参数两次运行逐字节一致(红线 31)。

    :param out_dir: 报告输出目录(缺省 ``benchmarks/out``,契约 A173)。
    :param cores: 显式注入核数(≥1 整数);``None`` 自动探测。
    :return: 三档对照表(见上)。
    :raises ValueError: 档位 / 核数非法(中文报错)。
    :raises OSError: 报告落盘失败。
    """
    resolved = _resolve_cores(cores)
    table: dict[str, dict[str, int]] = {}
    for tier in TIERS:
        w = _tier_workers(tier, cores=resolved)
        table[tier] = {
            "workers_expected": w,
            "peak": peak_concurrency(_barrier_task, n=w * 2, workers=w),
        }
    payload = {
        "benchmark": "tier_bench",
        "cores": resolved,
        "cores_source": "inject" if cores is not None else "detect",
        "method": "barrier-count",
        "note": "峰值=Barrier 实测并发度,确定性计数非墙钟",
        "tiers": table,
    }

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "tier_report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out / "tier_report.md").write_text(render_markdown(payload), encoding="utf-8")
    return table


def _ensure_utf8_stdio() -> None:
    """Windows 控制台编码非 UTF-8 时切换标准流编码,避免中文输出报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if (
                stream is not None
                and stream.encoding
                and stream.encoding.lower() not in ("utf-8", "utf8")
            ):
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 重新配置失败不影响主流程
            pass


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    返回码:0 三档实测峰值与理论 workers 全部一致;2 存在不一致档位,
    或流程可预期错误(中文提示到 stderr)。
    """
    _ensure_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="python benchmarks/tier_bench.py",
        description=(
            "NetSentinel 三档并发基准(A173):Barrier 计数法实测三档线程池"
            "并发峰值,与理论 workers 对照,产出 tier_report.md / "
            "tier_report.json(全程离线,确定性计数非墙钟)"
        ),
    )
    parser.add_argument(
        "--out", default="benchmarks/out", help="报告输出目录(默认 benchmarks/out)"
    )
    parser.add_argument(
        "--cores", type=int, default=None, help="显式注入核数(缺省自动探测 detect)"
    )
    args = parser.parse_args(argv)

    try:
        table = run(args.out, cores=args.cores)
    except (ValueError, OSError) as exc:
        print(f"错误:{exc}", file=sys.stderr)
        return 2

    parts = " · ".join(
        "{} {}/{}".format(tier, table[tier]["workers_expected"], table[tier]["peak"])
        for tier in TIERS
    )
    print(f"三档并发基准(cores={_resolve_cores(args.cores)},理论/实测):{parts}")
    out_dir = Path(args.out)
    print(f"报告已写出:{out_dir / 'tier_report.md'} 与 {out_dir / 'tier_report.json'}")
    if any(row["peak"] != row["workers_expected"] for row in table.values()):
        print("结论:存在实测峰值与理论不一致的档位 —— 退出码 2")
        return 2
    print("结论:三档实测峰值与理论 workers 全部一致 —— 退出码 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
