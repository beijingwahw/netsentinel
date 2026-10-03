"""NetSentinel(净网哨兵)并发扫描池(ops.pool,A58)。

对 watchlist 多站点做**有界并行扫描**:与 A39 ``ops.scheduler.run_once``
(逐项顺序巡查)共用同一套约定——指纹记忆、礼貌间隔、待复核通知——
但用 ``concurrent.futures.ThreadPoolExecutor`` 把单项扫描并行化:

- 有界并发:同一时刻最多 ``workers`` 个站点在扫(线程池 max_workers);
- 全局礼貌间隔:停顿发生在**提交侧串行循环**里(每提交一个任务后
  ``sleep(1.0 + jitter*0.5)``),任务本身并行,因此任意时刻相邻两次
  真实扫描的开始时间仍被礼貌间隔隔开;
- 指纹去重(首版"先扫后记"语义,见 :func:`run_pool` 文档);
- 失败隔离:单个 URL 扫描抛异常只计入 ``failed`` / ``errors``,绝不影响
  其余目标,也不中断整池。

安全红线(必须体现在代码里):
1. **只扫 ``items`` 列表中的目标**(去重保序后),本模块没有任何自动
   扩展目标的入口——不做链接发现、不读 watchlist 之外的任何来源;
2. 并发上限 ``workers`` 有界生效(缺省线程池 ``max_workers=workers``);
3. 单点失败不影响整体(见上"失败隔离")。

线程池与进程池注意事项(重要):
- 本模块**默认线程池**。如需真多进程,自行注入
  ``concurrent.futures.ProcessPoolExecutor``:Windows 下 spawn 启动方式
  要求 ① 程序入口必须有 ``if __name__ == "__main__":`` 守卫;② 传入的
  ``cfg`` / ``run_scan`` / ``memory`` 以及任务返回值必须**可 pickle**
  (本模块的工作函数 ``_scan_one`` 是模块级顶层函数、结果为 NamedTuple,
  均可 pickle;lambda / 局部闭包 fake 不可 pickle,测试注入时请注意);
  ③ sqlite 记忆库被多进程同时写时依赖 sqlite 自身文件锁,建议每进程
  独立连接(SiteMemory 即如此)。
- **Playwright sync API 线程安全注意事项**:sync API 的事件循环绑定
  创建它的线程,``Playwright`` / ``Browser`` / ``Page`` 等 sync 对象
  **严禁跨线程共享**。并发池中每个工作线程必须各自独立实例化
  (线程局部存储),这是缺省线程池形态下 crawler 层的既定约定;本模块
  自身不持有、不传递任何 Playwright 对象。

A203 trace 贯通(telemetry_trace 第三波接线,只增不改既有语义):

- ``run_pool(..., propagate_trace=True)``(默认开)时,提交循环在**提交侧**
  拍 ``telemetry_trace.propagate()`` 快照;**仅当快照确有活跃 trace_id** 才
  提交包装任务 :func:`_scan_one_traced`(worker 首行 ``restore`` 接回同一条
  trace 树);调用方无活跃 trace 或开关关闭时提交 :func:`_scan_one` **原样**,
  零开销零行为变化;
- 本模块自身不开任何 span(worker 内的 span 由 scan 实现或调用方自行开,
  挂到 restore 后的上下文即自动成树);
- 进程池注入形态下快照(dataclass)可 pickle,但 trace 注册表每进程独立,
  树不跨进程汇合(如需汇合由调用方自行导出/合并)。

A212 AIMD 自适应背压(可选注入,默认 None=现状静态背压逐字节不变):

- ``run_pool(..., aimd=AIMDController(...))`` 注入时,提交侧背压决策改由
  ``aimd.should_pause(pending)`` 驱动(信号驱动乘性减/加性增的动态并发
  窗口,见 :mod:`netsentinel.ops.aimd`),任务完成(含失败)经模块级
  :class:`_TimedScanAdapter` 把墙钟时长喂给 ``aimd.on_task_done``;
- 停顿**动作与时长完全不变**:无论静态阈值还是 AIMD 判定,多停一轮的
  时长恒为 ``PAUSE_BASE_S + PAUSE_JITTER_S × jitter``(红线 35:AIMD 只
  调节本地并发窗口,只增不减对外礼貌);AIMD 判"不停"时也**绝不跳过**
  每提交一次的常规礼貌停顿;
- 不注入 ``aimd``(默认 None)时,背压走原静态阈值分支,行为与 A212 前
  逐字节一致;trace 贯通路径(``_scan_one_traced`` 包装)不受 AIMD 注入
  影响——时长适配器包装的是 ``scan`` 可调用物,提交物与快照逻辑原样。

A236 窗口→executor 重建闭环 + 进程路径 worker 时长回传(可选,默认关闭):

- ``run_pool(..., rebuild_executor=True)``(**仅自建线程池形态**生效;注入
  executor 归调用方所有,该开关被忽略并告警)时,提交按**窗口分片**推进:
  分片大小 ``N = max(1, 当前 permits)``;提交间检查窗口相比**建池时刻**的
  收缩,收缩越过 :data:`REBUILD_FACTOR`(减半)→ 当前分片**提前收尾并
  排空**已提交任务,分片边界以 ``min(permits, 原始档位 workers)`` 重建
  executor——重建后 max_workers **恒 ≤ 原始档位 workers**(红线 47/37:
  并发窗口 cap 永不越档);窗口回升(≥ 建池窗口 × 因子)同样经重建扩容,
  但同样不越原始档位(cap 语义;乘性滞回防抖)。重建成本诚实计量
  (``pool.rebuild.count`` / ``pool.rebuild.duration`` / 失败
  ``pool.rebuild.failure``);工厂抛异常 → 回退**原池**继续,绝不中断整批。
  **红线 35:重建只发生在分片边界(已排空),期间不新增/不跳过/不缩短
  任何礼貌或背压停顿**——停顿序列只由提交侧产生,与重建零耦合;
- 进程池形态(stdlib ``ProcessPoolExecutor`` 注入)+ ``aimd``:A212 的
  :class:`_TimedScanAdapter` 会随 pickle 把控制器复制进子进程,父进程
  窗口看不到完成回调——本形态改提交模块级纯函数 :func:`_scan_one_timed`:
  worker 侧就地计时(纯 ``time.monotonic``,**不 pickle 控制器**,A166
  红线:进程池只传纯函数),时长连同结局经返回值 :class:`_TimedOutcome`
  回传,父进程聚合时喂 ``aimd.on_task_done``(失败任务的时长照实回传:
  异常就地转数据,错误文案与线程形态 ``str(exc)`` 一致;外层 summary
  形状逐字节不变)。线程池形态仍走 A212 适配器,零变化。

测试约定:``run_scan`` / ``memory`` / ``executor`` / ``sleep`` / ``jitter``
均可注入,通知经模块级 ``_default_notify``(monkeypatch 替换);全部离线、
零真实 sleep、零网络。
"""
from __future__ import annotations

import logging
import pathlib
import random as _random
import time
from concurrent.futures import Executor, Future, ProcessPoolExecutor, ThreadPoolExecutor
from typing import Any, Callable, NamedTuple

from netsentinel import telemetry
from netsentinel import telemetry_trace as tt
from netsentinel.contracts import Config
from netsentinel.ops.aimd import AIMDController

__all__ = ["run_pool"]

logger = logging.getLogger(__name__)

#: 相邻两次任务提交之间的基础停顿(秒),叠加 0~0.5s 随机抖动(礼貌抓取,
#: 与 A39 scheduler 的 PAUSE_BASE_S / PAUSE_JITTER_S 同款)
PAUSE_BASE_S = 1.0
PAUSE_JITTER_S = 0.5

#: 缺省并发上限(线程数);仅影响缺省线程池的 max_workers
DEFAULT_WORKERS = 2

#: 站点指纹记忆的 TTL(小时)与落盘文件名(相对 cfg.data_dir)——
#: 与 A39 scheduler 完全同款(MEMORY_TTL_HOURS=72 / site_memory.db)
MEMORY_TTL_HOURS = 72
MEMORY_DB_NAME = "site_memory.db"

#: 背压系数(V5):提交侧已提交未完成的任务数超过 ``workers × 该系数`` 时,
#: 提交循环在提交下一个任务前**多停一轮**礼貌间隔,给消费侧留出赶上时间。
#: 量化:默认 workers=2 → 阈值 4;积压 ≤ 4 时行为与旧版完全一致(零额外
#: 停顿);积压超过阈值后每提交一个任务多停 ``1.0 + jitter*0.5`` 秒。
BACKLOG_FACTOR = 2

#: A236 executor 重建触发系数(**乘性滞回**,防窗口抖动导致重建风暴):
#: 窗口收缩到 ≤ 建池窗口÷该系数(如减半)→ 触发收缩重建;窗口回升到
#: ≥ 建池窗口×该系数 → 触发扩容重建。重建后 max_workers 恒为
#: ``min(当前窗口, 原始档位 workers)``——绝不越原始档位(红线 47/37),
#: 也绝不小于 1(floor 语义在 executor 侧的同款投影)。
REBUILD_FACTOR = 2


# ---------------------------------------------------------------------------
# 单任务结果(模块级 + NamedTuple:可 pickle,兼容进程池形态)
# ---------------------------------------------------------------------------
class _TaskOutcome(NamedTuple):
    """一个站点任务的产出(由工作线程返回,主线程聚合)。"""

    status: str              # "done"(已扫描)/ "skipped"(指纹未变)
    verdict: str = ""        # status="done" 时:verdict.value
    needs_review: bool = False
    agg: float = 0.0


class _TimedOutcome(NamedTuple):
    """worker 侧计时回传壳(A236,进程池形态专用):纯数据,可 pickle。

    - ``outcome`` 非 None:任务成功,为 :class:`_TaskOutcome` 原样;
    - ``error`` 非空:worker 侧异常**就地转数据**(不跨进程上抛),
      文案 = ``str(原异常)``——父进程照旧计入 ``failed``,与线程形态
      ``fut.result()`` 上抛后取 ``str(exc)`` 完全一致;
    - ``duration_s``:worker 侧 ``time.monotonic`` 差分只包 ``scan``
      调用本身(与线程侧 :class:`_TimedScanAdapter` 同口径),失败任务
      照实携带(墙钟消耗同样是真实负载信号)。
    """

    outcome: "_TaskOutcome | None" = None
    duration_s: float = 0.0
    error: str = ""


# ---------------------------------------------------------------------------
# 缺省实现工厂(模块级,便于测试 monkeypatch;内部惰性导入兄弟模块)
# ---------------------------------------------------------------------------
def _default_run_scan(url: str, cfg: Config) -> Any:
    """缺省扫描实现:惰性导入编排器 run_scan(返回 SiteReport 或鸭子等价物)。"""
    try:
        from netsentinel.pipeline import orchestrator
    except ImportError as exc:
        raise RuntimeError(
            f"模块 netsentinel.pipeline.orchestrator 未就位:{exc}"
        ) from exc
    return orchestrator.run_scan(url, cfg)


def _default_sleep(seconds: float) -> None:
    """缺省停顿实现(测试注入 fake 以避免真实等待)。"""
    time.sleep(seconds)


def _default_jitter() -> float:
    """缺省抖动随机源:返回 [0, 1) 内一个浮点。"""
    return _random.random()


def _default_executor_factory(max_workers: int) -> ThreadPoolExecutor:
    """A236 重建工厂:按目标档位新建线程池(模块级,便于测试记录/替换)。

    只被 :func:`run_pool` 的**自建线程池**分支传入 :func:`_run_batch`;
    重建后 max_workers 由调用方钳制在 ``min(permits, 原始档位)`` ≤ 档位
    (红线 47/37),此处仅做 ≥ 1 防御。
    """
    return ThreadPoolExecutor(max_workers=max(1, int(max_workers)))


def _default_memory(cfg: Config) -> Any:
    """缺省站点指纹记忆:独立路径 ``<data_dir>/site_memory.db``,TTL 72h。

    **与 A39 scheduler 同款路径**(读其代码确认:``pathlib.Path(cfg.data_dir)
    / "site_memory.db"``),两模块天然共享同一份记忆;惰性导入 A33
    ``netsentinel.intel.site_memory.SiteMemory``,未就位时抛中文
    RuntimeError,由 :func:`run_pool` 捕获后降级为"本轮不去重"。
    """
    try:
        from netsentinel.intel.site_memory import SiteMemory
    except ImportError as exc:
        raise RuntimeError(
            f"模块 netsentinel.intel.site_memory 未就位:{exc}"
        ) from exc
    db_path = str(pathlib.Path(cfg.data_dir) / MEMORY_DB_NAME)
    return SiteMemory(db_path, ttl_hours=MEMORY_TTL_HOURS)


def _default_notify(cfg: Config, event: str, text: str) -> bool:
    """缺省通知实现:惰性导入 notify.hub;单发、不重试、异常不外抛。"""
    try:
        from netsentinel.notify.hub import notify
    except ImportError as exc:
        logger.warning("通知模块 netsentinel.notify.hub 未就位,本次提醒未发送:%s", exc)
        return False
    try:
        return bool(notify(cfg, event, text))
    except Exception as exc:  # noqa: BLE001 - 通知失败绝不拖垮扫描池
        logger.warning("通知发送失败(单发不重试):%s", exc)
        return False


# ---------------------------------------------------------------------------
# 目标准入(只收 items 里的东西,绝不扩大范围)
# ---------------------------------------------------------------------------
def _dedupe_targets(items: list[str]) -> list[str]:
    """去重保序:按去除首尾空白后的字符串去重,保留首次出现顺序。"""
    seen: set[str] = set()
    targets: list[str] = []
    for raw in items:
        u = str(raw).strip()
        if u and u not in seen:
            seen.add(u)
            targets.append(u)
    return targets


def _is_http_url(url: str) -> bool:
    """目标准入形态检查:必须 http(s):// 开头(与 A39 watchlist 校验同款)。"""
    return url.lower().startswith(("http://", "https://"))


# ---------------------------------------------------------------------------
# 单站点任务(模块级顶层函数:可 pickle,兼容进程池注入形态)
# ---------------------------------------------------------------------------
def _scan_one(
    url: str, cfg: Config, scan: Callable[[str, Config], Any], memory: Any
) -> _TaskOutcome:
    """扫描单个站点并完成指纹记忆,返回 :class:`_TaskOutcome`。

    步骤(与 A39 run_once 的单项逻辑对齐,差异仅在"跳过"语义,见模块文档):

    1. 读记忆库中该站点上轮指纹 ``last_fingerprint(url)``(读不到按空串,
       即必扫方向);
    2. 调 ``scan(url, cfg)``——异常原样上抛,由聚合方按 ``failed`` 处理
       (失败隔离);
    3. 扫后 ``fp = memory.fingerprint(report)`` 并 ``remember(url, fp)``
       ("先扫后记":指纹只能来自扫描产出的报告);
    4. 首版跳过判定:上轮指纹非空且与本轮新算指纹相同 → 返回
       ``skipped``(注意:**扫描本身已经发生**,省的只是判定与通知,
       不是流量——该限制写明在 :func:`run_pool` 文档中);
    5. 否则抽取 verdict / needs_review / agg 返回 ``done``,由主线程
       聚合时决定是否 notify。
    """
    last_fp = ""
    if memory is not None:
        try:
            raw_last = memory.last_fingerprint(url)
        except Exception as exc:  # noqa: BLE001 - 读失败按必扫处理(安全方向)
            logger.warning("读取站点上轮指纹失败,按必扫处理:%s %s", url, exc)
            raw_last = ""
        last_fp = raw_last if isinstance(raw_last, str) else ""

    report = scan(url, cfg)  # 单项异常 → Future 上抛 → failed(不拖垮整池)

    if memory is not None:
        try:
            fp = str(memory.fingerprint(report) or "")
        except Exception as exc:  # noqa: BLE001 - 算不出指纹就不记忆
            logger.warning("计算站点指纹失败(本轮不记忆):%s %s", url, exc)
            fp = ""
        if fp:
            try:
                memory.remember(url, fp)
            except Exception as exc:  # noqa: BLE001 - 记忆失败不影响本轮结果
                logger.warning("写入站点指纹记忆失败(不影响本轮结果):%s %s", url, exc)
            if last_fp and fp == last_fp:
                logger.info("站点指纹与上轮一致,本轮计为跳过:%s", url)
                return _TaskOutcome(status="skipped")

    verdict_raw = getattr(report, "verdict", "")
    verdict = getattr(verdict_raw, "value", verdict_raw)
    needs_review = bool(getattr(report, "needs_review", False))
    try:
        agg = float(getattr(report, "agg_nsw_prob", 0.0) or 0.0)
    except (TypeError, ValueError):
        agg = 0.0
    return _TaskOutcome(
        status="done", verdict=str(verdict), needs_review=needs_review, agg=agg
    )


def _scan_one_traced(
    url: str,
    cfg: Config,
    scan: Callable[[str, Config], Any],
    memory: Any,
    snapshot: tt.TraceSnapshot,
) -> _TaskOutcome:
    """``_scan_one`` 的跨线程追踪包装(A203):worker **首行** restore 快照。

    - :func:`tt.restore` 后,本线程内后续开的 span 自动挂到快照时刻的
      trace 树(以快照 span 为父);
    - restore 失败(理论仅类型不符)按**无追踪上下文**继续执行原任务——
      失败隔离红线:扫描本体绝不因追踪而丢;
    - 模块级顶层函数 + 冻结 dataclass 入参,可 pickle(兼容进程池注入;
      注册表每进程独立,见模块文档);
    - 本函数不开 span,只负责把上下文接进来。
    """
    try:
        tt.restore(snapshot)
    except Exception as exc:  # noqa: BLE001 - 追踪恢复失败不影响扫描
        logger.warning("trace 快照恢复失败,本任务退化为无追踪执行:%s %s", url, exc)
    return _scan_one(url, cfg, scan, memory)


def _scan_one_timed(
    url: str,
    cfg: Config,
    scan: Callable[[str, Config], Any],
    memory: Any,
    snapshot: tt.TraceSnapshot | None = None,
) -> _TimedOutcome:
    """``_scan_one`` 的 worker 侧计时形态(A236,进程池形态专用)。

    - 只包 ``scan`` 调用本身的墙钟(与线程侧 :class:`_TimedScanAdapter`
      同口径),用 ``time.monotonic`` 在 worker 进程内**就地计时**——
      **不 pickle 控制器**(A166 红线:进程池只传纯函数;且控制器随
      pickle 复制进子进程会让父窗口看不到完成,正是本形态要修的);
    - 计得时长连同结局经返回值 :class:`_TimedOutcome` 回传:成功 →
      (outcome, duration);失败 → (None, duration, str(异常))——
      异常**不跨进程上抛**,父进程按 ``error`` 字段照旧计入 ``failed``
      (文案与线程形态一致,见 :class:`_TimedOutcome` 文档);
    - ``snapshot`` 非 None 时先 restore(与 :func:`_scan_one_traced`
      同款语义:restore 失败退化为无追踪执行,绝不丢扫描);
    - 模块级顶层函数 + 纯数据入参/返回值,可 pickle;内部闭包
      ``_timed_scan`` 只活在 worker 进程内,不经 pickle。
    """
    durations: list[float] = []

    def _timed_scan(u: str, c: Config) -> Any:
        started = time.monotonic()
        try:
            return scan(u, c)
        finally:
            durations.append(max(0.0, time.monotonic() - started))

    if snapshot is not None:
        try:
            tt.restore(snapshot)
        except Exception as exc:  # noqa: BLE001 - 追踪恢复失败不影响扫描
            logger.warning("trace 快照恢复失败,本任务退化为无追踪执行:%s %s", url, exc)
    try:
        outcome = _scan_one(url, cfg, _timed_scan, memory)
    except Exception as exc:  # noqa: BLE001 - 异常就地转数据,不跨进程上抛
        return _TimedOutcome(
            outcome=None, duration_s=durations[0] if durations else 0.0,
            error=str(exc),
        )
    return _TimedOutcome(
        outcome=outcome, duration_s=durations[0] if durations else 0.0
    )


def _is_process_pool(executor: Executor) -> bool:
    """进程池形态探测(A236):仅识别 stdlib ``ProcessPoolExecutor``(含子类)。

    模块级函数,便于测试 monkeypatch 注入自定义判定(mock 执行器);判定
    仅影响 ``aimd`` 注入时的**时长回传通道选择**(适配器 vs 返回值),
    不影响任何提交/停顿/聚合语义。
    """
    return isinstance(executor, ProcessPoolExecutor)


def _feed_aimd_duration(aimd: AIMDController | None, duration_s: float) -> None:
    """父进程侧回喂 worker 时长(A236 进程路径聚合点);控制器异常绝不外抛。

    与线程侧 :class:`_TimedScanAdapter` 的故障隔离口径一致:计量失败只
    告警,绝不影响扫描结果的聚合。
    """
    if aimd is None:
        return
    try:
        aimd.on_task_done(duration_s)
    except Exception as exc:  # noqa: BLE001 - 计量失败绝不拖垮聚合
        logger.warning("AIMD on_task_done 记录失败(不影响扫描):%s", exc)


class _TimedScanAdapter:
    """AIMD 时长计量适配器(A212):包装 ``scan``,把每次调用的墙钟时长
    喂给控制器的 ``on_task_done``。

    - 时长取自控制器注入时钟(``aimd.now()`` 前后差分,负差钳 0)——
      测试注入脚本时钟即得**确定性时长**;
    - 扫描抛异常也照实计量(``finally`` 路径):失败任务的墙钟消耗同样
      是真实负载信号;``on_task_done`` 自身抛异常只告警,绝不影响扫描
      结果(AIMD 故障隔离);
    - 模块级类 + 双字段:``scan`` 与控制器均可 pickle 时整体可 pickle
      (兼容进程池注入形态;控制器状态每进程独立,见 ops.aimd 模块文档)。
    """

    __slots__ = ("_aimd", "_scan")

    def __init__(self, aimd: AIMDController, scan: Callable[[str, Config], Any]) -> None:
        self._aimd = aimd
        self._scan = scan

    def __call__(self, url: str, cfg: Config) -> Any:
        start = self._aimd.now()
        try:
            return self._scan(url, cfg)
        finally:
            duration = max(0.0, self._aimd.now() - start)
            try:
                self._aimd.on_task_done(duration)
            except Exception as exc:  # noqa: BLE001 - 计量失败绝不拖垮扫描
                logger.warning("AIMD on_task_done 记录失败(不影响扫描):%s", exc)


# ---------------------------------------------------------------------------
# 池主体:提交(串行 + 礼貌间隔)→ 聚合(失败隔离);A236 分片/重建编排
# ---------------------------------------------------------------------------
def _run_shard(
    cfg: Config,
    shard: list[str],
    *,
    executor: Executor,
    scan: Callable[[str, Config], Any],
    memory: Any,
    pause: Callable[[float], None],
    jitter_fn: Callable[[], float],
    backlog_limit: int,
    propagate_trace: bool,
    aimd: AIMDController | None,
    timed_mode: bool,
    summary: dict[str, Any],
    should_end_shard: Callable[[], bool] | None,
) -> int:
    """在给定 executor 上提交并聚合**一个分片**,返回实际处理的目标数。

    提交侧与聚合侧逻辑与 A58/V5/A203/A212 完全同款(语义逐字节保持):
    串行提交 + 每提交一次礼貌停顿 + 背压停顿 + trace 快照包装;聚合按
    提交顺序取结果,单项异常只计数。A236 仅新增两处:

    - ``should_end_shard``(重建闭环专用,默认 None=不启用):**提交间**
      检查窗口收缩,越阈则当前分片提前收尾(已提交任务在随后的聚合中
      **全部排空**);首目标不检查,保证每个分片至少处理 1 个目标
      (进度性:防零提交死循环),返回值按**处理数**(含形态被拒目标)
      计,同样保证推进;
    - ``timed_mode``(进程池形态,见 :func:`_is_process_pool`):提交
      :func:`_scan_one_timed`,聚合时拆 :class:`_TimedOutcome` 回壳、把
      worker 时长喂 ``aimd.on_task_done``(失败任务照实回传);线程形态
      仍提交 :func:`_scan_one` / :func:`_scan_one_traced` 原样。
    """
    futures: list[tuple[str, Future[Any]]] = []
    processed = 0
    for url in shard:
        # A236 重建闭环:提交间检查窗口收缩(首目标不检查,见函数文档)
        if futures and should_end_shard is not None and should_end_shard():
            logger.debug(
                "窗口收缩越过重建阈值,当前分片提前收尾(已提交 %d 个,排空后判定重建)",
                len(futures),
            )
            break
        processed += 1
        if not _is_http_url(url):
            summary["failed"] += 1
            telemetry.inc("pool.failed")
            summary["errors"].append(
                (url, "目标 URL 必须以 http:// 或 https:// 开头,已拒绝扫描")
            )
            logger.warning("目标形态不合法,未提交扫描(不扩大范围原则):%s", url)
            continue
        # 背压:默认(aimd=None)沿用静态阈值——已提交未完成的任务数超过
        # workers×BACKLOG_FACTOR → 多停一轮;注入 aimd(A212)时同一停顿
        # 动作改由 aimd.should_pause(pending) 驱动(动态并发窗口,红线 35:
        # 停顿时长恒为固定礼貌常量,AIMD 只增不减对外礼貌)
        pending = sum(1 for _, fut in futures if not fut.done())
        if aimd is not None:
            overload = aimd.should_pause(pending)
        else:
            overload = pending > backlog_limit
        if overload:
            telemetry.inc("pool.backpressure")
            if aimd is not None:
                logger.info(
                    "任务积压 %d 达到 AIMD 窗口 %d,提交侧多停一轮(背压)",
                    pending, aimd.permits,
                )
            else:
                logger.info(
                    "任务积压 %d 超过阈值 %d,提交侧多停一轮(背压)",
                    pending, backlog_limit,
                )
            pause(PAUSE_BASE_S + PAUSE_JITTER_S * jitter_fn())
        # A203 trace 贯通:仅在确有活跃 trace 快照时提交带 restore 的包装任务;
        # 无快照(开关关闭 / 调用方无 trace)提交 _scan_one 原样,零行为变化。
        # A236 进程形态(timed_mode):提交 _scan_one_timed(计时经返回值
        # 回传),快照逻辑同款(快照作为第 5 个可选参数随纯函数入子进程)。
        task: Callable[..., Any] = _scan_one
        task_args: tuple[Any, ...] = (url, cfg, scan, memory)
        snap = tt.propagate() if propagate_trace else None
        if timed_mode:
            task = _scan_one_timed
            if snap is not None and snap.trace_id is not None:
                task_args = (url, cfg, scan, memory, snap)
        elif snap is not None and snap.trace_id is not None:
            task = _scan_one_traced
            task_args = (url, cfg, scan, memory, snap)
        futures.append((url, executor.submit(task, *task_args)))
        pause(PAUSE_BASE_S + PAUSE_JITTER_S * jitter_fn())

    def _fail(url: str, message: str) -> None:
        summary["failed"] += 1
        telemetry.inc("pool.failed")
        summary["errors"].append((url, message))
        logger.warning("单项扫描失败,继续其余目标:%s %s", url, message)

    # 聚合侧:按提交顺序取结果;单项异常只计数(不设 Future 超时)。
    # A236 timed_mode:拆 _TimedOutcome 回壳,时长喂 aimd(失败也照实
    # 回传);裸 _TaskOutcome(极端 mock 形态)按原样消费、不喂时长(防御)。
    for url, fut in futures:
        try:
            outcome: Any = fut.result()
        except Exception as exc:  # noqa: BLE001 - 单点失败不影响整体(安全红线)
            _fail(url, str(exc))
            continue
        if timed_mode and isinstance(outcome, _TimedOutcome):
            _feed_aimd_duration(aimd, outcome.duration_s)
            if outcome.error:
                _fail(url, outcome.error)
                continue
            outcome = outcome.outcome
        if outcome is None:
            _fail(url, "worker 未回传任务结局(进程回传壳缺结局)")
            continue
        if outcome.status == "skipped":
            summary["skipped"] += 1
            telemetry.inc("pool.skipped")
            continue
        summary["done"] += 1
        telemetry.inc("pool.done")
        summary["results"][url] = outcome.verdict
        if outcome.needs_review:
            text = f"{url} verdict={outcome.verdict} agg={outcome.agg:.2f}"
            try:
                sent = bool(_default_notify(cfg, "pending_review", text))
            except Exception as exc:  # noqa: BLE001 - 通知异常不拖垮扫描池
                logger.warning("通知发送异常(单发不重试):%s", exc)
                sent = False
            if sent:
                logger.info("已发送待复核提醒:%s", text)
            else:
                logger.info("待复核提醒未能发出(单发不重试):%s", text)
    return processed


def _run_rebuilding(
    cfg: Config,
    targets: list[str],
    *,
    executor: Executor,
    executor_factory: Callable[[int], Executor],
    original_workers: int,
    scan: Callable[[str, Config], Any],
    memory: Any,
    pause: Callable[[float], None],
    jitter_fn: Callable[[], float],
    backlog_limit: int,
    propagate_trace: bool,
    aimd: AIMDController,
    summary: dict[str, Any],
) -> None:
    """A236 窗口→executor 重建闭环(就地填充 ``summary``,不返回)。

    - 分片大小 ``N = max(1, aimd.permits)``(窗口即并发度,一批恰好
      一个窗口量的提交,分片边界天然是重建检查点);
    - **提交间**检查:``permits × REBUILD_FACTOR ≤ 建池窗口``(收缩越过
      减半阈)→ 当前分片提前收尾(已提交任务**全部排空**)再进入重建
      判定;
    - 重建判定(**乘性滞回**):收缩(permits×factor ≤ build)或回升
      (permits ≥ build×factor)且目标档位 ≠ 当前档位 → 以
      ``max(1, min(permits, 原始档位 workers))`` 重建 executor——
      **恒 ≤ 原始档位 workers**(红线 47/37:并发窗口 cap 永不越档,
      窗口恢复也不越 cap);``min`` 已含 permits,窗口小于档位时收窄到
      窗口,窗口大于档位时钳在档位;
    - **先建新池再关旧池**:旧池在分片边界已排空,``shutdown(wait=True)``
      即时返回;工厂抛异常 → 计 ``pool.rebuild.failure``,回退**原池**
      继续(原池原封不动,绝不中断整批);
    - 重建成本诚实计量:``pool.rebuild.duration`` 计时(含工厂构造与
      旧池关闭,含失败尝试)、``pool.rebuild.count`` 计成功次数;
    - **红线 35:重建期间礼貌间隔/背压序列零触碰**——本函数不含任何
      ``pause(...)`` 调用,停顿只发生在 :func:`_run_shard` 的提交侧,
      重建只发生在分片边界(不新增、不跳过、不缩短任何停顿);
    - 工厂建出的每个池归本函数所有:批次结束(含异常)``finally`` 必
      关闭,不养孤儿线程;批次目标全部提交后不再重建(只剩成本)。
    """
    current: Executor = executor
    current_workers = original_workers
    created: list[Executor] = []
    build_permits = aimd.permits
    idx = 0
    try:
        while idx < len(targets):
            shard_size = max(1, aimd.permits)  # 分片大小 N = 当前 permits 的函数
            logger.debug(
                "A236 重建闭环分片:剩余 %d,窗口 %d → 本分片至多 %d 个",
                len(targets) - idx, aimd.permits, shard_size,
            )
            processed = _run_shard(
                cfg, targets[idx: idx + shard_size],
                executor=current, scan=scan, memory=memory,
                pause=pause, jitter_fn=jitter_fn, backlog_limit=backlog_limit,
                propagate_trace=propagate_trace, aimd=aimd, timed_mode=False,
                summary=summary,
                # 提交间检查:窗口相比建池时收缩越过 1/REBUILD_FACTOR → 提前收尾
                should_end_shard=lambda: aimd.permits * REBUILD_FACTOR <= build_permits,
            )
            idx += processed
            if idx >= len(targets):
                break  # 已无剩余目标:重建只剩成本,不做
            permits = aimd.permits
            desired = max(1, min(permits, original_workers))  # cap:恒 ≤ 原始档位
            shrink = permits * REBUILD_FACTOR <= build_permits
            expand = permits >= build_permits * REBUILD_FACTOR
            if (not (shrink or expand)) or desired == current_workers:
                continue
            with telemetry.timer("pool.rebuild.duration"):  # 诚实计量(含失败尝试)
                try:
                    fresh = executor_factory(desired)  # 先建新池:失败时原池原封不动
                except Exception as exc:  # noqa: BLE001 - 回退原池,绝不中断整批
                    telemetry.inc("pool.rebuild.failure")
                    logger.warning(
                        "executor 重建失败(max_workers=%d),回退原池继续(不中断):%s",
                        desired, exc,
                    )
                    continue
                created.append(fresh)
                try:
                    current.shutdown(wait=True)  # 分片已排空,关闭即时
                except Exception as exc:  # noqa: BLE001 - 旧池关闭失败不影响新池
                    logger.warning("旧 executor 关闭失败(不影响重建):%s", exc)
            telemetry.inc("pool.rebuild.count")
            logger.info(
                "executor 重建:max_workers %d → %d(当前窗口 %d,原始档位 %d,cap 语义)",
                current_workers, desired, permits, original_workers,
            )
            current = fresh
            current_workers = desired
            build_permits = permits
    finally:
        # 工厂建出的池归本函数所有:批次结束(含异常)必关闭,不养孤儿线程
        for built_executor in created:
            try:
                built_executor.shutdown(wait=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("重建 executor 关闭失败:%s", exc)


def _run_batch(
    cfg: Config,
    targets: list[str],
    *,
    executor: Executor,
    scan: Callable[[str, Config], Any],
    memory: Any,
    pause: Callable[[float], None],
    jitter_fn: Callable[[], float],
    workers: int = DEFAULT_WORKERS,
    propagate_trace: bool = True,
    aimd: AIMDController | None = None,
    rebuild_executor: bool = False,
    executor_factory: Callable[[int], Executor] | None = None,
    executor_workers: int | None = None,
) -> dict[str, Any]:
    """在给定 executor 上跑完整批目标,返回汇总字典(纯聚合,不建线程池)。

    ``workers`` 仅用于计算背压阈值(:data:`BACKLOG_FACTOR`),不用于收窄
    注入方 executor 的并发;``propagate_trace`` 见 :func:`run_pool`;
    ``aimd``(A212,默认 None)注入时背压决策改由 ``aimd.should_pause``
    驱动、任务完成时长经 :class:`_TimedScanAdapter` 回喂(线程形态)或
    :func:`_scan_one_timed` 返回值回传(进程形态,见模块文档)。

    A236 重建闭环(默认 False=单分片跑完,与 A212 行为逐字节一致):
    ``rebuild_executor=True`` 且 ``executor_factory`` 与 ``aimd`` 均注入时,
    批内按窗口分片提交、分片边界按 :data:`REBUILD_FACTOR` 滞回判定重建
    (:func:`_run_rebuilding`);``executor_factory`` / ``executor_workers``
    由 :func:`run_pool` 的自建线程池分支透传,直接调用方(测试)亦可
    自行注入记录式工厂。
    """
    summary: dict[str, Any] = {
        "total": len(targets),
        "done": 0,
        "failed": 0,
        "skipped": 0,
        "results": {},
        "errors": [],
    }
    backlog_limit = max(1, int(workers)) * BACKLOG_FACTOR
    # A236 进程形态判定:aimd 注入 + stdlib ProcessPoolExecutor(含子类)
    # → 时长改走 worker 返回值回传(不 pickle 控制器),不再包线程侧适配器
    timed_mode = aimd is not None and _is_process_pool(executor)
    if aimd is not None and not timed_mode:
        scan = _TimedScanAdapter(aimd, scan)  # 完成回调:时长喂 on_task_done
        logger.info(
            "并发扫描池开始:目标 %d 个(AIMD 窗口:permits=%d, cap=%d, floor=%d)",
            len(targets), aimd.permits, aimd.cap, aimd.floor,
        )
    elif timed_mode:
        logger.info(
            "并发扫描池开始:目标 %d 个(AIMD 窗口:permits=%d, cap=%d, floor=%d;"
            "进程池形态:worker 计时经返回值回传,父进程聚合喂 on_task_done)",
            len(targets), aimd.permits, aimd.cap, aimd.floor,
        )
    else:
        logger.info("并发扫描池开始:目标 %d 个(背压阈值 %d)", len(targets), backlog_limit)

    rebuild_active = (
        rebuild_executor and aimd is not None and executor_factory is not None
    )
    if rebuild_executor and not rebuild_active:
        logger.debug(
            "executor 重建闭环未启用(需同时注入 aimd 与 executor_factory;"
            "注入 executor 归调用方所有,run_pool 不会重建/关闭),本批单分片跑完"
        )
    if rebuild_active:
        _run_rebuilding(
            cfg, targets, executor=executor, executor_factory=executor_factory,
            original_workers=max(
                1, int(executor_workers if executor_workers is not None else workers)
            ),
            scan=scan, memory=memory, pause=pause, jitter_fn=jitter_fn,
            backlog_limit=backlog_limit, propagate_trace=propagate_trace,
            aimd=aimd, summary=summary,
        )
    else:
        _run_shard(
            cfg, targets, executor=executor, scan=scan, memory=memory,
            pause=pause, jitter_fn=jitter_fn, backlog_limit=backlog_limit,
            propagate_trace=propagate_trace, aimd=aimd, timed_mode=timed_mode,
            summary=summary, should_end_shard=None,
        )

    logger.info(
        "并发扫描池结束:总计 %d,完成 %d,指纹未变跳过 %d,失败 %d",
        summary["total"], summary["done"], summary["skipped"], summary["failed"],
    )
    return summary


def run_pool(
    cfg: Config,
    items: list[str],
    *,
    run_scan: Callable[[str, Config], Any] | None = None,
    memory: Any | None = None,
    executor: Executor | None = None,
    workers: int = DEFAULT_WORKERS,
    sleep: Callable[[float], None] | None = None,
    jitter: Callable[[], float] | None = None,
    propagate_trace: bool = True,
    aimd: AIMDController | None = None,
    rebuild_executor: bool = False,
) -> dict[str, Any]:
    """并发扫描一批目标 URL,返回本轮汇总。

    流程:

    1. **去重保序**:`items`` 去除首尾空白后按首次出现顺序去重(``total``
       为去重后的目标数);非 http(s) 形态的目标不提交扫描,直接计入
       ``failed`` 与 ``errors``(目标准入检查,绝不扩大扫描范围);
    2. **有界并发**:缺省 ``ThreadPoolExecutor(max_workers=workers)``,
       同一时刻至多 ``workers`` 个站点在扫;**注入 executor 时 ``workers``
       仅用于背压阈值计算**,不强制收窄注入方的并发(调用方自负其责);
    3. **全局礼貌间隔**:提交循环内每提交一个任务即停顿
       ``1.0 + jitter()*0.5`` 秒(提交侧串行,任务并行);
    4. **提交侧背压(V5,默认)**:已提交未完成的任务数超过
       ``workers × BACKLOG_FACTOR``(默认 2×workers)时,提交下一个任务前
       **多停一轮**同款礼貌间隔,给消费侧留出赶上时间;积压不超过阈值时
       行为与旧版完全一致(零额外停顿)。停顿函数与抖动源均可注入量化;
       **AIMD 形态(A212)**:注入 ``aimd`` 控制器时,该停顿决策改由
       ``aimd.should_pause(pending)`` 驱动(积压达到动态窗口即停,窗口随
       过载信号乘性减、随平稳完成加性增,见 :mod:`netsentinel.ops.aimd`),
       多停一轮的时长与静态形态**完全同款**——AIMD 只调节本地并发窗口,
       绝不缩短对外礼貌间隔(红线 35);
    5. **指纹去重(首版"先扫后记"语义)**:站点新指纹只能从本轮扫描产出
       的报告计算,扫描前**无法**与记忆比对,因此首版跳过逻辑为——
       仍执行扫描,扫后 ``memory.fingerprint(report)`` 与上轮
       ``memory.last_fingerprint(url)`` 比较:相同 → 计入 ``skipped``
       (不 notify、不进 ``results``;注意扫描流量已发生,省的是判定与
       通知,不是抓取);不同或首见 → 计入 ``done`` 并 ``remember``;
    6. **失败隔离**:单个 URL 扫描抛异常只计入 ``failed`` / ``errors``,
       不设 Future 超时、不中断其余目标;
    7. ``needs_review`` 的报告经 ``_default_notify`` 单发 ``pending_review``
       提醒(惰性导入 notify.hub,失败仅告警;skipped 一律不 notify)。

    :param cfg: 全局配置(memory 缺省路径取 ``cfg.data_dir``)。
    :param items: 目标 URL 列表(只扫其中去重后的条目,不扩大范围)。
    :param run_scan: 注入的扫描函数,签名 ``(url, cfg) -> report``;
        缺省惰性导入 ``pipeline.orchestrator.run_scan``。
    :param memory: 注入的站点指纹记忆(须实现 last_fingerprint /
        fingerprint / remember,A33 SiteMemory API);缺省惰性构造
        ``SiteMemory(<data_dir>/site_memory.db, ttl_hours=72)``——
        与 A39 scheduler 同款路径,两模块共享同一份记忆;不可用时
        降级为"不去重、不记忆"。
    :param executor: 注入的并发执行器(须提供 ``submit``);缺省新建
        线程池并由本函数负责关闭;注入的 executor 归调用方所有,
        本函数**不会** shut it down。如注入 ``ProcessPoolExecutor``,
        见模块文档的 Windows spawn / pickle 注意事项。
    :param workers: 缺省线程池的并发上限(有界);注入 executor 时不再
        用于收窄并发,但仍作为**背压阈值**(workers × :data:`BACKLOG_FACTOR`)
        参与提交侧节奏控制(V5)。
    :param sleep: 注入的停顿函数;缺省 ``time.sleep``。
    :param jitter: 注入的 [0,1) 随机源;缺省 ``random.random``。
    :param propagate_trace: A203 trace 贯通开关(默认开):提交侧对每个目标
        拍 ``telemetry_trace.propagate()`` 快照,**仅在快照确有活跃 trace_id**
        时提交 :func:`_scan_one_traced`(worker 首行 restore 接回同一条
        trace 树);调用方无活跃 trace 或显式传 False 时提交 :func:`_scan_one`
        原样——零开销、零行为变化(既有调用方不受任何影响)。
    :param aimd: A212 AIMD 调速器(``netsentinel.ops.aimd.AIMDController``,
        默认 None=现状静态背压):注入时背压决策改由 ``aimd.should_pause``
        驱动,任务完成(含失败)的墙钟时长回喂 ``aimd.on_task_done`` 驱动
        窗口自适应——**线程池形态**经 :class:`_TimedScanAdapter` 在 worker
        内回喂;**进程池形态**(注入 stdlib ``ProcessPoolExecutor``)经
        :func:`_scan_one_timed` 在 worker 侧计时、返回值回传,由本函数在
        父进程聚合时回喂(外层 summary 形状两种形态完全一致)。窗口上限
        由构造方的档位 workers 决定(≤ 物理核数,红线 37);对外礼貌间隔
        不受任何影响(红线 35,见模块文档)。
    :param rebuild_executor: A236 窗口→executor 重建闭环开关(默认 False=
        现状单池跑完,行为逐字节不变):True 时**仅对 run_pool 自建的
        线程池**生效——按当前 AIMD 窗口分片提交,窗口相比建池时刻减半
        (或回升翻倍)则排空当前分片后以 ``min(permits, workers)`` 重建
        线程池,恒不超过原始档位 workers(红线 47/37);重建期间礼貌/
        背压停顿零触碰(红线 35),成本记 ``pool.rebuild.count`` /
        ``pool.rebuild.duration``(失败计 ``pool.rebuild.failure`` 并回退
        原池,不中断)。需同时注入 ``aimd``(无窗口信号则单分片跑完,
        行为与现状一致);**注入 executor 时本开关被忽略并告警**(注入
        池归调用方所有,本函数绝不重建/关闭)。
    :return: ``{"total", "done", "failed", "skipped",
        "results": {url: verdict.value}, "errors": [(url, 错误信息), ...]}``,
        且恒有 ``done + failed + skipped == total``。

    可观测性(V5):整批耗时记 ``telemetry.timer("pool.total")``,按结局累加
    ``pool.done`` / ``pool.failed`` / ``pool.skipped`` 三个计数器,触发背压
    停顿时累加 ``pool.backpressure``;A236 重建闭环另计
    ``pool.rebuild.count`` / ``pool.rebuild.duration`` / ``pool.rebuild.failure``
    (只存名称与数字,不涉及站点内容)。

    用法示例::

        summary = run_pool(cfg, ["https://a.example.com/"], workers=2)
        assert summary["done"] + summary["failed"] + summary["skipped"] == 1
    """
    with telemetry.timer("pool.total"):
        scan = run_scan if run_scan is not None else _default_run_scan
        pause = sleep if sleep is not None else _default_sleep
        jitter_fn = jitter if jitter is not None else _default_jitter

        targets = _dedupe_targets(items)

        if memory is None:
            try:
                memory = _default_memory(cfg)
            except Exception as exc:  # noqa: BLE001 - 记忆库不可用只影响去重
                logger.warning("站点指纹记忆不可用,本轮不去重(全部重扫):%s", exc)
                memory = None

        if executor is not None:
            if rebuild_executor:
                # A236:注入池归调用方所有——绝不重建/关闭,开关忽略并告警
                logger.warning(
                    "rebuild_executor=True 但已注入 executor(归调用方所有),"
                    "重建闭环不启用:run_pool 绝不重建/关闭调用方的池"
                )
            logger.debug(
                "使用注入的 executor(%s);workers=%d 仅作背压阈值,不强制收窄",
                type(executor).__name__, workers,
            )
            return _run_batch(
                cfg, targets, executor=executor, scan=scan, memory=memory,
                pause=pause, jitter_fn=jitter_fn, workers=workers,
                propagate_trace=propagate_trace, aimd=aimd,
            )

        n_workers = max(1, int(workers))
        if not rebuild_executor:
            with ThreadPoolExecutor(max_workers=n_workers) as own:
                return _run_batch(
                    cfg, targets, executor=own, scan=scan, memory=memory,
                    pause=pause, jitter_fn=jitter_fn, workers=n_workers,
                    propagate_trace=propagate_trace, aimd=aimd,
                )
        # A236 重建闭环:自建初始池(归本函数,finally 必关);批内重建由
        # _run_batch/_run_rebuilding 经 executor_factory 就地完成(工厂建出
        # 的池同样必关,不养孤儿线程)
        own = ThreadPoolExecutor(max_workers=n_workers)
        try:
            return _run_batch(
                cfg, targets, executor=own, scan=scan, memory=memory,
                pause=pause, jitter_fn=jitter_fn, workers=n_workers,
                propagate_trace=propagate_trace, aimd=aimd,
                rebuild_executor=True, executor_workers=n_workers,
                executor_factory=_default_executor_factory,
            )
        finally:
            own.shutdown(wait=True)
