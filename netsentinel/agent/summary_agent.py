# -*- coding: utf-8 -*-
"""结案代理(NetSentinel V9 · A168)—— 批量跑完后的分类汇总与举报准备。

依据 CONTRACTS-V9 §2 A168 与 §0 红线 36 实现:批量扫描
(A108 ``ops.batch_scan.batch_scan``)跑完之后,由本代理把
``{"reports": {url: SiteReport}, "summary": {...}}`` 收口成一份
:class:`SummaryOutcome`,给收官 CLI(A169 finishflow)打印中文汇总表、
给运营者看"下一步该做什么"。四个阶段,**全部只读统计、零提交路径**:

1. **分类**:惰性复用 A108 ``ops.batch_scan.group_and_enqueue``
   (归组 → 合并证据 → 每组入列一条 pending,红线 24 链路原样),
   也可注入 ``grouper`` 整体替换(鸭子返回 ``{"groups": [...], ...}``);
2. **汇总统计**:A117 ``intel.group_stats.stats``(或注入 ``stats_fn``)
   产出组数 / 判定分布 / 最大组等战况;
3. **收官报告**:A172 ``report.run_summary.render_run_summary``(惰性,
   或注入 ``renderer``)渲染 MD+HTML 双文件;兄弟模块缺席时**降级写最小
   Markdown 汇总**(本模块内置,仅标准库);
4. **举报准备**:按 A110 语义统计复核队列里 pending→approved 的条目,
   以 note 的 ``[组:名]`` 标记聚合——``ready_count`` = 已声明且已批准的
   条数,``attest_pending`` = 已批准但**未声明**的组名清单
   (``cfg.batch_require_attestation=False`` 时全部视为就绪)。本阶段
   **只统计、不激活任何东西**:库文件不存在直接返回零(不建库不建表),
   绝不写声明、绝不改条目状态、绝不触发提交。

红线 36(结案代理无自主提交权):本模块不编排举报执行链,源码中
不存在任何自动确认或绕过声明的路径;举报仍须运营者逐组声明(A111
batch_tui)→ 逐条人工门(A112 顺序批量链)完成。``next_steps`` 固定
给出这三步中文指引,任何阶段的降级提示都以中文备注**前置**在其中。

兄弟模块(A108/A117/A172/A110/A164)一律**惰性导入 + 鸭子注入**:
缺席或抛错只降级(空汇总 / 最小 MD / 零计数)并在 ``next_steps``
前置中文提示,绝不让收官流程中断。可观测性:``summary_agent.run``
计数与 ``summary_agent.groups`` 仪表(仅名称与数字)。

用法::

    from netsentinel.agent.summary_agent import SummaryAgent

    outcome = SummaryAgent().run(scan_result, cfg)
    outcome.groups, outcome.ready_count, outcome.attest_pending
    outcome.report_md, outcome.next_steps

仅使用标准库;Python 3.10+;零网络、零外呼、零提交。
"""
from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from netsentinel import telemetry

__all__ = [
    "DISABLED_NEXT_STEPS",
    "NEXT_STEPS",
    "SummaryAgent",
    "SummaryOutcome",
]

logger = logging.getLogger(__name__)

#: 配置关闭时的固定提示(仅此一条;最小 outcome 不做任何汇总与落盘)。
DISABLED_NEXT_STEPS: tuple[str, ...] = ("配置关闭",)

#: 正常收官后的固定中文后续步骤(红线 25 声明 → A169 收官举报 → 红线 24 人工门)。
NEXT_STEPS: tuple[str, ...] = (
    "第一步:运行 python -m netsentinel.cli.batch_tui 逐组完成证据核验与批量确认声明"
    "(红线 25:无声明的组不得提交)",
    "第二步:运行 python -m netsentinel.finishflow --report --resume 批次号 先干跑核对,"
    "确认无误后再加 --exec 进入真实批量举报",
    "第三步:举报执行阶段每一条仍须逐条人工门完成验证码输入与最终确认,"
    "频控与每日额度照常生效(红线 24/26)",
)

#: 收官报告固定声明句(与 A172 render_run_summary 的固定文案一致)。
REDLINE_NOTE = "本汇总由结案代理生成,举报须经逐组声明与逐条人工门"

#: 收官报告落盘基名(相对 cfg.data_dir):<基名>.md / <基名>.html。
REPORT_BASENAME = "run_summary"

#: 判定档位 → 严重度(group_stats 同口径;未知按 suspect,安全方向)。
_VERDICT_ORDER: dict[str, int] = {"clean": 0, "suspect": 1, "nsfw": 2}

#: workers 计算缺省保留核心数(与 contracts.Config.cpu_reserve 缺省一致)。
_DEFAULT_RESERVE = 1

#: 分档 workers 计算缺省核心数(os.cpu_count() 取不到时的回退,契约 §1)。
_DEFAULT_CORES = 2

#: 注入函数类型速记(仅供标注;运行时全部鸭子调用)。
_GrouperFn = Callable[..., Any]
_StatsFn = Callable[..., Any]
_RendererFn = Callable[..., Any]
_PackagerFn = Callable[..., Any]


# ---------------------------------------------------------------------------
# SummaryOutcome:结案代理的最终产物
# ---------------------------------------------------------------------------
@dataclass
class SummaryOutcome:
    """结案代理输出(字段与契约 §2 A168 一一对应)。

    - ``groups``:案件组摘要行 ``list[dict]``(键 ``name``/``sites``/``urls``
      /``verdict``/``agg_max``,保持分组结果顺序);
    - ``report_md`` / ``report_html``:收官报告两文件路径(A172 双件套;
      兄弟缺席降级时 html 为空串、md 为内置最小稿);
    - ``ready_count``:已声明且已批准(pending→approved)的条目数;
    - ``attest_pending``:已批准但尚未完成人工核实声明的组名清单
      (按队列 id 首见顺序;声明门关闭时为空);
    - ``workers`` / ``tier``:本次批量运行的并发档位与实际 worker 数;
    - ``next_steps``:固定中文后续步骤(降级提示前置其上)。
    """

    groups: list[dict] = field(default_factory=list)
    report_md: str = ""
    report_html: str = ""
    ready_count: int = 0
    attest_pending: list[str] = field(default_factory=list)
    workers: int = 1
    tier: str = "mid"
    next_steps: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 惰性导入辅助(兄弟模块并行开发中,未就位抛中文 RuntimeError)
# ---------------------------------------------------------------------------
def _lazy_import(module_name: str, attr: str) -> Any:
    """惰性导入 ``module_name.attr``;ImportError/缺属性 → 中文 RuntimeError。"""
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(f"模块 {module_name} 未就位:{exc}") from exc
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise RuntimeError(f"模块 {module_name} 未提供 {attr}:{exc}") from exc


def _as_int(value: Any, default: int = 0) -> int:
    """宽容整数转换(duck 值可能是 None / 浮点 / 字符串)。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    """宽容浮点转换(duck 值可能是 None / 字符串)。"""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 档位与 workers(契约 §1 权威公式;优先复用 A164,缺席本地同义兜底)
# ---------------------------------------------------------------------------
def _tier_of(cfg: Any) -> str:
    """读并发档位(cfg.concurrency_tier,缺省 mid;空白回退 mid)。"""
    tier = str(getattr(cfg, "concurrency_tier", "mid") or "mid").strip().lower()
    return tier or "mid"


def _local_tier_workers(tier: str, cores: int, reserve: int) -> int:
    """契约 §1 公式的本地镜像:low=cores//4,mid=cores//2,high=cores-reserve。

    仅在兄弟模块 ops.concurrency(A164)未就位时兜底使用;未知档位按 mid
    处理(汇总展示用途,不替 A163 做校验)。恒有结果 ≥1 且 ≤ cores
    (reserve 钳到 ≥0,红线 37)。
    """
    if reserve < 0:
        reserve = 0
    if cores < 1:
        cores = _DEFAULT_CORES
    if tier == "low":
        return max(1, cores // 4)
    if tier == "high":
        return max(1, min(cores, cores - reserve))
    return max(1, cores // 2)


def _workers_for(cfg: Any, tier: str) -> int:
    """解析该档位的 worker 数:优先 A164 ``ops.concurrency.io_workers``。

    兄弟缺席 / 抛错 / 返回非法值时回退本地 §1 公式
    (``os.cpu_count()`` 取不到按 2,``cpu_reserve`` 取不到按 1)。
    """
    try:
        io_workers = _lazy_import("netsentinel.ops.concurrency", "io_workers")
        workers = _as_int(io_workers(cfg), 0)
        if workers >= 1:
            return workers
    except Exception as exc:  # noqa: BLE001 - 兄弟缺席按本地公式兜底
        logger.debug("ops.concurrency(A164)未就位,按契约 §1 公式本地计算:%s", exc)
    cores = os.cpu_count() or _DEFAULT_CORES
    reserve = _as_int(getattr(cfg, "cpu_reserve", _DEFAULT_RESERVE), _DEFAULT_RESERVE)
    return _local_tier_workers(tier, cores, reserve)


# ---------------------------------------------------------------------------
# 组摘要行(鸭子取值,口径与 A117 group_stats 一致)
# ---------------------------------------------------------------------------
def _seq_of(obj: Any, attr: str) -> list[str]:
    """容错读取对象的序列属性(list/tuple/set);缺失 / 其他类型 → []。"""
    value = getattr(obj, attr, None)
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [str(item) for item in value]


def _group_name(group: Any) -> str:
    """组名;缺名回退首个站点 URL,再退"未命名组"(与 A108 同口径)。"""
    name = str(getattr(group, "name", "") or "").strip()
    if name:
        return name
    urls = _seq_of(group, "site_urls")
    return urls[0] if urls else "未命名组"


def _hosts_of(group: Any) -> list[str]:
    """组的去重别名域(小写、保首见顺序;空值跳过)——"站点数"口径。"""
    hosts: list[str] = []
    seen: set[str] = set()
    for item in _seq_of(group, "aliases"):
        host = item.strip().lower()
        if host and host not in seen:
            seen.add(host)
            hosts.append(host)
    return hosts


def _verdict_str(value: Any) -> str:
    """判定值(枚举 / 字符串)→ 小写字符串;未知 / 空回退原样文本。"""
    raw = getattr(value, "value", value)
    text = str(raw if raw is not None else "").strip()
    return text.lower() if text.lower() in _VERDICT_ORDER else text


def _group_row(group: Any) -> dict:
    """组 → 摘要行 ``{name, sites, urls, verdict, agg_max}``(保输入序)。"""
    return {
        "name": _group_name(group),
        "sites": len(_hosts_of(group)),
        "urls": len(_seq_of(group, "site_urls")),
        "verdict": _verdict_str(getattr(group, "verdict", "")),
        "agg_max": _as_float(getattr(group, "agg_max", 0.0)),
    }


# ---------------------------------------------------------------------------
# 举报准备:pending→approved 条目按 [组:名] 聚合(只统计,不激活任何东西)
# ---------------------------------------------------------------------------
def _readiness_counts(cfg: Any) -> tuple[int, list[str], dict[str, int]]:
    """统计复核队列的举报就绪情况,返回 ``(ready_count, attest_pending, 按组计数)``。

    A110 语义的只读镜像(不调用其 ready_entries——那会构造队列写入侧对象;
    这里只做同口径统计):

    - 只看 ``approved``(pending→approved 已人工确认)条目,按 id 升序;
    - 组名解析复用 A110 ``resolve_group_name``(note 的 ``[组:名]`` 标记,
      无标记回退 canonical/host);
    - ``cfg.batch_require_attestation=True``(缺省)时:已声明组
      (A110 ``BatchReview.is_attested``)的条目计入 ``ready_count``,
      未声明组名进入 ``attest_pending``;为 False 时全部 approved 视为就绪;
    - **不激活任何东西**:``cfg.db_path`` 库文件不存在直接返回零
      (不创建目录 / 库 / 表),绝不写声明、不改状态、不触发提交。
    """
    require = bool(getattr(cfg, "batch_require_attestation", True))
    db_path = str(getattr(cfg, "db_path", "data/review_queue.db") or "")
    if not db_path or not Path(db_path).is_file():
        return 0, [], {}

    queue_cls = _lazy_import("netsentinel.decision.review_queue", "ReviewQueue")
    review_cls = _lazy_import("netsentinel.decision.batch_review", "BatchReview")
    resolve_group_name = _lazy_import(
        "netsentinel.decision.batch_review", "resolve_group_name"
    )
    queue = queue_cls(db_path)
    review = review_cls(db_path)
    try:
        approved = list(queue.list("approved"))
        ready = 0
        pending_names: list[str] = []
        by_group: dict[str, int] = {}
        for entry in approved:
            note = str(getattr(entry, "note", "") or "")
            site_url = str(getattr(entry, "site_url", "") or "")
            group = str(resolve_group_name(note, site_url) or "").strip() or "未命名组"
            by_group[group] = by_group.get(group, 0) + 1
            if require and not review.is_attested(group):
                if group not in pending_names:
                    pending_names.append(group)
            else:
                ready += 1
        return ready, pending_names, by_group
    finally:
        # 只关闭本函数自己打开的对象;关闭异常不影响统计结果。
        for obj in (queue, review):
            close = getattr(obj, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - 关闭兜底,绝不阻断收官
                    logger.debug("关闭举报准备统计对象时出现异常", exc_info=True)


# ---------------------------------------------------------------------------
# 收官报告:渲染 stats 组装 + 兄弟缺席时的最小 MD 降级稿
# ---------------------------------------------------------------------------
def _render_stats(
    *,
    reports: Mapping[str, Any],
    summary: Mapping[str, Any],
    rows: list[dict],
    stats: Mapping[str, Any],
    ready_count: int,
    attest_pending: list[str],
    tier: str,
    workers: int,
) -> dict[str, Any]:
    """组装 A172 ``render_run_summary`` 的入参(键与契约 §2 A172 一致)。

    ``sites`` = 捕获报告数;``scanned`` 优先取扫描摘要的 ``scanned`` /
    ``done``(A58 pool 口径),缺失回退报告数;``failed`` 同理;
    ``telemetry_top`` 取当前计数器 Top5(仅名称与数字)。
    """
    counters = telemetry.snapshot().get("counters") or {}
    telemetry_top = [
        {"name": name, "value": value}
        for name, value in sorted(
            counters.items(), key=lambda kv: (-float(kv[1]), kv[0])
        )[:5]
    ]
    sites = len(reports)
    scanned = summary.get("scanned")
    if scanned is None:
        scanned = summary.get("done", sites)
    verdict_dist = stats.get("verdict_dist")
    return {
        "sites": sites,
        "scanned": _as_int(scanned, sites),
        "failed": _as_int(summary.get("failed", 0), 0),
        "verdict_dist": dict(verdict_dist) if isinstance(verdict_dist, Mapping) else {},
        "groups": rows,
        "tier": tier,
        "workers": workers,
        "cost_est": _as_float(summary.get("cost_est", 0.0), 0.0),
        "budget_used": _as_float(summary.get("budget_used", 0.0), 0.0),
        "telemetry_top": telemetry_top,
        "attest_pending": list(attest_pending),
        "ready_count": int(ready_count),
    }


def _write_minimal_md(
    render_stats: Mapping[str, Any],
    by_group: Mapping[str, int],
    next_steps: list[str],
    out_base: Path,
) -> str:
    """收官报告的**最小降级稿**(A172 缺席 / 渲染失败时兜底,仅标准库)。

    只写一个 Markdown 文件(``<out_base>.md``,父目录自动创建),
    内容:站点 / 组数 / 判定分布 / 档位 / 举报准备(按组计数与待声明组)
    + 固定红线声明句 + 后续步骤。返回落盘路径。
    """
    verdict_dist = render_stats.get("verdict_dist") or {}
    verdict_text = "、".join(f"{k} {v}" for k, v in verdict_dist.items()) or "无数据"
    attest_pending = list(render_stats.get("attest_pending") or [])
    by_group_text = (
        "、".join(f"[组:{g}] {n} 条" for g, n in by_group.items()) or "无已确认条目"
    )
    lines = [
        "# 净网哨兵 · 收官汇总(最小稿)",
        "",
        f"- 站点数:{render_stats.get('sites', 0)}"
        f"(扫描 {render_stats.get('scanned', 0)},失败 {render_stats.get('failed', 0)})",
        f"- 案件组数:{len(render_stats.get('groups') or [])};判定分布:{verdict_text}",
        f"- 并发档位:{render_stats.get('tier', 'mid')}"
        f"(workers={render_stats.get('workers', 1)})",
        f"- 举报准备:已声明已批准 {render_stats.get('ready_count', 0)} 条;"
        f"已确认条目按组:{by_group_text}",
        "- 待声明组:"
        + ("、".join(attest_pending) if attest_pending else "无(全部已声明或无已确认条目)"),
        "",
        f"> {REDLINE_NOTE}(红线 36:结案代理无自主提交权)。",
        "",
        "## 后续步骤",
    ]
    lines.extend(f"- {step}" for step in next_steps)
    md_path = Path(str(out_base) + ".md")
    if str(md_path.parent):
        md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("收官报告最小稿已写出:%s", md_path)
    return str(md_path)


# ---------------------------------------------------------------------------
# SummaryAgent:结案代理本体
# ---------------------------------------------------------------------------
class SummaryAgent:
    """结案代理(A168):批量跑完后的分类汇总 + 举报**准备**(红线 36)。

    :meth:`run` 把批量扫描结果收口成 :class:`SummaryOutcome`:
    分类(A108 group_and_enqueue 惰性 / 注入)→ 汇总统计(A117)→
    收官报告(A172 双文件 / 缺席最小 MD)→ 举报准备(A110 语义只读统计)。
    本代理**无自主提交权**:不编排举报执行链、不写声明、不改条目状态,
    一切提交仍走"逐组声明 → 逐条人工门"的人工链路。
    """

    def run(
        self,
        scan_result: dict,
        cfg: Any,
        *,
        grouper: _GrouperFn | None = None,
        packager: _PackagerFn | None = None,
        stats_fn: _StatsFn | None = None,
        renderer: _RendererFn | None = None,
    ) -> SummaryOutcome:
        """汇总一批扫描结果,返回 :class:`SummaryOutcome`。

        :param scan_result: A108 ``batch_scan`` 返回值
            ``{"reports": {url: SiteReport}, "summary": {...}}``;缺键 / 空
            按空批量处理,不报错。
        :param cfg: 全局配置(读 ``summary_agent_enabled`` /
            ``concurrency_tier`` / ``cpu_reserve`` / ``batch_require_attestation``
            / ``db_path`` / ``data_dir``;全部鸭子容错)。
        :param grouper: 整体替换分类阶段的函数,鸭子签名同 A108
            ``grouper(cfg, reports=..., packager=...) -> {"groups": [...],
            "enqueued": n, "skipped_clean": m}``(测试离线注入)。
        :param packager: 证据打包器,透传给 A108 分类阶段(可注入)。
        :param stats_fn: 替换 A117 ``group_stats.stats`` 的统计函数。
        :param renderer: 替换 A172 ``render_run_summary(stats, out_base)``
            的渲染函数(返回 ``(md_path, html_path)``)。
        :return: 结案产物;``cfg.summary_agent_enabled=False`` 时返回
            最小 outcome(仅 tier / workers / ``["配置关闭"]``)。

        各阶段兄弟缺失 / 注入抛错一律**优雅降级**:对应字段取空值、
        中文提示前置到 ``next_steps``,绝不中断收官流程;任何阶段都
        不触发提交动作(红线 36)。
        """
        telemetry.inc("summary_agent.run")
        tier = _tier_of(cfg)
        workers = _workers_for(cfg, tier)

        # ---- 配置关闭:最小 outcome,不做任何汇总与落盘 ----
        if not bool(getattr(cfg, "summary_agent_enabled", True)):
            telemetry.gauge("summary_agent.groups", 0)
            logger.info("结案代理已配置关闭(summary_agent_enabled=False)")
            return SummaryOutcome(
                workers=workers,
                tier=tier,
                next_steps=list(DISABLED_NEXT_STEPS),
            )

        reports: dict[str, Any] = {}
        summary: Mapping[str, Any] = {}
        if isinstance(scan_result, Mapping):
            raw_reports = scan_result.get("reports")
            if isinstance(raw_reports, Mapping):
                reports = {str(k): v for k, v in raw_reports.items()}
            raw_summary = scan_result.get("summary")
            if isinstance(raw_summary, Mapping):
                summary = raw_summary
        notes: list[str] = []  # 降级提示(中文,前置在固定步骤之上)

        # ---- 阶段一:分类(A108 group_and_enqueue 惰性 / 注入)----
        group_objs: list[Any] = []
        try:
            if grouper is not None:
                result = grouper(cfg, reports=reports, packager=packager)
            else:
                group_and_enqueue = _lazy_import(
                    "netsentinel.ops.batch_scan", "group_and_enqueue"
                )
                result = group_and_enqueue(cfg, reports=reports, packager=packager)
            if isinstance(result, Mapping):
                group_objs = list(result.get("groups") or [])
                enqueued = _as_int(result.get("enqueued"), 0)
                skipped_clean = _as_int(result.get("skipped_clean"), 0)
                logger.info(
                    "分类完成:%d 组(入列 %d,clean 跳过 %d)",
                    len(group_objs), enqueued, skipped_clean,
                )
        except Exception as exc:  # noqa: BLE001 - 兄弟缺失优雅降级
            notes.append(
                f"注意:分类阶段(A108 归组入列)未完成已降级为空汇总({exc});"
                "请先完成批量扫描与归组,再运行结案代理"
            )
            logger.warning("结案代理分类阶段降级", exc_info=True)

        rows = [_group_row(group) for group in group_objs]

        # ---- 阶段二:汇总统计(A117 group_stats / 注入)----
        stats: dict[str, Any] = {}
        try:
            stats_call = stats_fn
            if stats_call is None:
                stats_call = _lazy_import("netsentinel.intel.group_stats", "stats")
            result_stats = stats_call(group_objs)
            if isinstance(result_stats, Mapping):
                stats = dict(result_stats)
        except Exception as exc:  # noqa: BLE001 - 兄弟缺失优雅降级
            notes.append(f"注意:汇总统计(A117 group_stats)未就位已降级({exc})")
            logger.warning("结案代理统计阶段降级", exc_info=True)

        # ---- 阶段三:举报准备(A110 语义,只统计不激活)----
        ready_count = 0
        attest_pending: list[str] = []
        by_group: dict[str, int] = {}
        try:
            ready_count, attest_pending, by_group = _readiness_counts(cfg)
        except Exception as exc:  # noqa: BLE001 - 队列不可读优雅降级
            notes.append(f"注意:举报准备统计不可用已降级为空({exc})")
            logger.warning("结案代理举报准备阶段降级", exc_info=True)
        if attest_pending:
            logger.info(
                "举报准备:已声明已批准 %d 条;待声明组 %d 个:%s",
                ready_count, len(attest_pending), "、".join(attest_pending),
            )

        # ---- 阶段四:收官报告(A172 双文件 / 缺席最小 MD)----
        render_stats = _render_stats(
            reports=reports,
            summary=summary,
            rows=rows,
            stats=stats,
            ready_count=ready_count,
            attest_pending=attest_pending,
            tier=tier,
            workers=workers,
        )
        data_dir = str(getattr(cfg, "data_dir", "data") or "data")
        out_base = Path(data_dir) / REPORT_BASENAME
        report_md = ""
        report_html = ""
        try:
            render_call = renderer
            if render_call is None:
                render_call = _lazy_import(
                    "netsentinel.report.run_summary", "render_run_summary"
                )
            md_path, html_path = render_call(render_stats, str(out_base))
            report_md = str(md_path)
            report_html = str(html_path)
        except Exception as exc:  # noqa: BLE001 - 兄弟缺失写最小 MD
            notes.append(
                f"注意:收官报告渲染(A172 run_summary)未就位,已写最小 Markdown 汇总({exc})"
            )
            logger.warning("结案代理收官报告降级为最小稿", exc_info=True)
            report_md = _write_minimal_md(render_stats, by_group, notes + list(NEXT_STEPS), out_base)
            report_html = ""

        next_steps = notes + list(NEXT_STEPS)
        return self._finish(
            rows, report_md, report_html, ready_count,
            attest_pending, workers, tier, next_steps,
        )

    @staticmethod
    def _finish(
        rows: list[dict],
        report_md: str,
        report_html: str,
        ready_count: int,
        attest_pending: list[str],
        workers: int,
        tier: str,
        next_steps: list[str],
    ) -> SummaryOutcome:
        """收口:记录 groups 仪表并组装 :class:`SummaryOutcome`。"""
        telemetry.gauge("summary_agent.groups", len(rows))
        return SummaryOutcome(
            groups=rows,
            report_md=report_md,
            report_html=report_html,
            ready_count=int(ready_count),
            attest_pending=list(attest_pending),
            workers=int(workers),
            tier=str(tier),
            next_steps=list(next_steps),
        )
