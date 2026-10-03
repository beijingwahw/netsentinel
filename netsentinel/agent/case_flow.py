# -*- coding: utf-8 -*-
"""案件主流程编排(NetSentinel V3 · A41):run_scan → plan → apply → 复核入列。

与 CONTRACTS-V3 §3 A41 一致::

    run_case(url, cfg, **deps) -> SiteReport
        = run_scan(惰性接 orchestrator,稳定层不动)
        → plan_investigation(GLM 办案规划,走 vlm_cache 预算)
        → apply_plan(执行侦查动作,verdict/needs_review 只升不降)
        → (可选第二轮:首轮 confidence < 0.5 且首轮确有动作,总轮数 ≤ 2)
        → 升级时 build_bundle + queue.add + 审计(全部惰性复用,不复制实现)

入列去重语义(重要,避免生产环境重复入列):

- 真实 ``orchestrator.run_scan`` 在 ``needs_review`` 时已自行入列;
- run_case 仅在**案件侦查升级**了结论(verdict 档位升高,或 needs_review
  由 False 变 True)时才重新打包并追加一条复核记录(证据更强,值得人再看一眼);
- 首轮已入列且本轮无升级 → 不重复入列,只记审计。

安全红线(V3 §0):
- 13:所有 VLM 调用(含本流程的案件规划)走 vlm_cache 预算,不绕过;
- 11/7:侦查只收集证据,入列只是"待人工拍板",任何环节都不跳过人工门,
  更不自动举报;离线 / 预算尽时优雅降级为"只扫描、不侦查"。

兄弟模块(pipeline.orchestrator / evidence.packager / decision.review_queue /
logging_util)一律惰性导入;run_case 依赖的结构性兄弟缺失时抛中文
``RuntimeError`` 指明模块(plan/apply 内部的兄弟缺失则按 notes 跳过,见
case_agent 模块说明)。

V5:整体流程接入 ``netsentinel.telemetry``(case.total 计时、case.rounds_total /
case.actions_total gauge、case.escalated 升级计数;只记名称与数字)。

用法示例::

    from netsentinel.agent.case_flow import run_case

    report = run_case("http://localhost/a", cfg)   # 离线时自动降级为只扫描
    print(report.verdict, report.intel["case_agent"]["rounds_total"])
"""
from __future__ import annotations

import importlib
import logging
from typing import Any, Callable

from netsentinel import telemetry
from netsentinel.contracts import Config, SiteReport, Verdict

from netsentinel.agent.case_agent import apply_plan, plan_investigation

__all__ = ["run_case", "MAX_PLAN_ROUNDS", "SECOND_ROUND_CONFIDENCE"]

logger = logging.getLogger(__name__)

#: 案件规划+执行的最大轮数(首轮恒有;第二轮看置信度门槛)
MAX_PLAN_ROUNDS = 2

#: 触发第二轮侦查的置信度门槛(首轮 confidence < 0.5 且首轮有动作)
SECOND_ROUND_CONFIDENCE = 0.5

#: 注入的规划函数签名:(report, cfg) -> 计划 dict
PlannerFn = Callable[[SiteReport, Config], dict]

#: 注入的扫描函数签名:(url, cfg) -> SiteReport
ScanFn = Callable[[str, Config], SiteReport]


def _load(module_name: str) -> Any:
    """惰性导入兄弟模块;缺失时抛中文 RuntimeError(指明模块,契约 A41)。"""
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(f"模块 {module_name} 未就位:{exc}") from exc


# ---------------------------------------------------------------------------
# 缺省工厂(模块级,便于测试 monkeypatch 稳定替换)
# ---------------------------------------------------------------------------


def _default_run_scan(url: str, cfg: Config) -> SiteReport:
    """缺省扫描:复用 orchestrator.run_scan(稳定层,不改不抄)。"""
    orchestrator = _load("netsentinel.pipeline.orchestrator")
    return orchestrator.run_scan(url, cfg)


def _default_build_bundle(report: SiteReport, cfg: Config) -> Any:
    """缺省证据包:复用 evidence.packager.build_bundle。"""
    packager = _load("netsentinel.evidence.packager")
    return packager.build_bundle(report, cfg)


def _default_queue(cfg: Config) -> Any:
    """缺省复核队列:复用 decision.review_queue.ReviewQueue。"""
    review_queue = _load("netsentinel.decision.review_queue")
    return review_queue.ReviewQueue(cfg.db_path)


def _default_audit_logger(path: str) -> Any:
    """缺省审计日志:复用 logging_util.JsonlAuditLogger。"""
    logging_util = _load("netsentinel.logging_util")
    return logging_util.JsonlAuditLogger(path)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

_VERDICT_ORDER: dict[str, int] = {
    Verdict.CLEAN.value: 0,
    Verdict.SUSPECT.value: 1,
    Verdict.NSFW.value: 2,
}


def _verdict_rank(value: Any) -> int:
    """判定档位 → 0/1/2;未知取值按 0(保守)。"""
    text = value.value if isinstance(value, Verdict) else str(value)
    return _VERDICT_ORDER.get(text.strip().lower(), 0)


def _plan_confidence(plan: dict) -> float:
    """容错读取计划置信度;缺失 / 非法按 0.0(低置信,倾向于补一轮)。"""
    raw = plan.get("confidence")
    if isinstance(raw, bool) or raw is None:
        return 0.0
    try:
        return min(1.0, max(0.0, float(raw)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _actions_count(report: SiteReport) -> int:
    """统计 case_agent 各轮计划的动作总数(审计观测用)。"""
    intel = getattr(report, "intel", None)
    if not isinstance(intel, dict):
        return 0
    node = intel.get("case_agent")
    if not isinstance(node, dict):
        return 0
    rounds = node.get("rounds")
    if not isinstance(rounds, list):
        return 0
    total = 0
    for record in rounds:
        if isinstance(record, dict) and isinstance(record.get("actions"), list):
            total += len(record["actions"])
    return total


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def _run_case_once(
    url: str,
    cfg: Config,
    *,
    run_scan: ScanFn | None = None,
    planner: PlannerFn | None = None,
    client: Any | None = None,
    rescan: Callable[[str, Config], Any] | None = None,
    recheck: Callable[[str, Config], Any] | None = None,
    build_bundle: Callable[[SiteReport, Config], Any] | None = None,
    queue: Any | None = None,
    audit_logger: Any | None = None,
) -> SiteReport:
    """案件流程主体实现(参数与安全语义见 :func:`run_case`)。"""
    logger.info("案件流程开始:%s(规划轮数上限 %d)", url, MAX_PLAN_ROUNDS)

    # 1) 稳定层扫描:run_scan 不变(它自身在 needs_review 时已入列 + 审计)。
    scan = run_scan if run_scan is not None else _default_run_scan
    report = scan(url, cfg)
    initial_rank = _verdict_rank(report.verdict)
    initial_review = bool(report.needs_review)

    # 2) 规划 → 执行,≤ MAX_PLAN_ROUNDS 轮;第二轮门槛见 SECOND_ROUND_CONFIDENCE。
    make_plan: PlannerFn = planner if planner is not None else (
        lambda r, c: plan_investigation(r, c, client=client)
    )
    rounds = 0
    previous: dict | None = None
    while rounds < MAX_PLAN_ROUNDS:
        if rounds >= 1 and previous is not None:
            if (
                bool(previous.get("offline"))
                or not previous.get("actions")
                or _plan_confidence(previous) >= SECOND_ROUND_CONFIDENCE
            ):
                break  # 首轮已高置信 / 无动作 / 离线:不再追加侦查轮
        try:
            plan = make_plan(report, cfg)
        except Exception as exc:  # noqa: BLE001 - 规划异常按"无动作"降级,不阻断流程
            telemetry.inc("case.errors")
            logger.warning("案件规划异常,本轮按无动作处理:%s", exc)
            plan = {"offline": False, "error": f"案件规划异常:{exc}", "actions": []}
        if not isinstance(plan, dict):
            logger.warning("案件规划返回结构非法,本轮按无动作处理")
            plan = {"offline": False, "error": "案件规划返回结构非法", "actions": []}

        report = apply_plan(plan, report, cfg, rescan=rescan, recheck=recheck)
        previous = plan
        rounds += 1

    # 3) 升级判定 + 重新入列(去重:首轮已入列且无升级时不重复入列)。
    final_rank = _verdict_rank(report.verdict)
    escalated = final_rank > initial_rank or (report.needs_review and not initial_review)
    entry_id: int | None = None
    bundle_zip = ""
    if report.needs_review and escalated:
        bundle_fn = build_bundle if build_bundle is not None else _default_build_bundle
        bundle = bundle_fn(report, cfg)
        bundle_zip = str(getattr(bundle, "zip_path", "") or "")
        review_queue = queue if queue is not None else _default_queue(cfg)
        entry_id = int(review_queue.add(report, bundle_zip))
        telemetry.inc("case.escalated")
        logger.info(
            "案件侦查升级(%s → %s),已重新入列待人工复核:id=%s zip=%s",
            initial_rank, final_rank, entry_id, bundle_zip,
        )
    elif report.needs_review:
        logger.info(
            "站点仍需人工复核;首轮扫描已入列且本轮无升级,不重复入列(%s)", url
        )

    # 4) 审计(无论是否入列都记一条 "case" 事件,含轮次与升级标记)。
    actions_total = _actions_count(report)
    telemetry.gauge("case.rounds_total", rounds)
    telemetry.gauge("case.actions_total", actions_total)
    audit = audit_logger if audit_logger is not None else _default_audit_logger(cfg.audit_path)
    audit.log_event(
        "case",
        site=url,
        verdict=getattr(report.verdict, "value", str(report.verdict)),
        agg=round(float(report.agg_nsw_prob), 4),
        needs_review=bool(report.needs_review),
        rounds=rounds,
        actions=actions_total,
        escalated=bool(escalated),
        entry_id=entry_id,
        zip=bundle_zip or None,
    )
    logger.info(
        "案件流程结束:%s rounds=%d escalated=%s verdict=%s agg=%.4f",
        url, rounds, escalated,
        getattr(report.verdict, "value", report.verdict), report.agg_nsw_prob,
    )
    return report


def run_case(
    url: str,
    cfg: Config,
    *,
    run_scan: ScanFn | None = None,
    planner: PlannerFn | None = None,
    client: Any | None = None,
    rescan: Callable[[str, Config], Any] | None = None,
    recheck: Callable[[str, Config], Any] | None = None,
    build_bundle: Callable[[SiteReport, Config], Any] | None = None,
    queue: Any | None = None,
    audit_logger: Any | None = None,
) -> SiteReport:
    """对单个站点跑一轮完整的"侦探式"案件流程,返回站点报告(同一对象累积更新)。

    流程:run_scan → (plan → apply)×≤2 轮 → 升级时重新入列 + 审计。

    :param url: 站点 URL。
    :param cfg: 全局配置。
    :param run_scan: 注入的扫描函数;缺省惰性调 ``orchestrator.run_scan``
        (兄弟缺失抛中文 RuntimeError)。
    :param planner: 注入的规划函数 ``(report, cfg) -> 计划 dict``;缺省
        ``case_agent.plan_investigation``(client 透传)。规划离线 / 预算尽 /
        解析失败时优雅降级:不执行动作,仅写 intel 记录。
    :param client: 透传给 :func:`plan_investigation` 的 GLM 客户端(测试注入点)。
    :param rescan / recheck: 透传给 :func:`apply_plan` 的侦查回调(测试注入点)。
    :param build_bundle: 注入的证据包构建函数;缺省 ``packager.build_bundle``。
    :param queue: 注入的复核队列(须有 ``add(report, zip) -> id``);
        缺省 ``ReviewQueue(cfg.db_path)``。
    :param audit_logger: 注入的审计日志器(须有 ``log_event(event, **fields)``);
        缺省 ``JsonlAuditLogger(cfg.audit_path)``。
    :return: 站点报告(intel["case_agent"] 记录各轮侦查;升级时已重新入列)。
    :raises RuntimeError: 结构性兄弟模块(orchestrator / packager /
        review_queue / logging_util)未就位,中文消息指明模块。

    V5 可观测:整体耗时记 ``telemetry.timer("case.total")``,轮数 / 动作数记
    gauge ``case.rounds_total`` / ``case.actions_total``,升级入列计
    ``case.escalated``(只记名称与数字,不记 URL 内容)。
    """
    with telemetry.timer("case.total"):
        return _run_case_once(
            url,
            cfg,
            run_scan=run_scan,
            planner=planner,
            client=client,
            rescan=rescan,
            recheck=recheck,
            build_bundle=build_bundle,
            queue=queue,
            audit_logger=audit_logger,
        )
