# -*- coding: utf-8 -*-
"""批量顺序提交引擎(NetSentinel V6 · A112)。

把一批已通过人工复核与批量确认声明的举报条目(通常来自 A110
``BatchReview.ready_entries`` / A111 TUI ``queue-batch``)**逐条顺序**送入执行器:
每一条都要独立经过 频控前置 → 计划构造 → 执行器(人工门)→ 状态/频控/审计
落账。批量只是"顺序编排",绝不改变单条提交的安全语义。

安全红线(CONTRACTS-V6 §0,违反即缺陷):
24. **批量举报仍是"逐条人工门"**:``executor`` 恒以 ``auto_confirm=False``
    调用,本模块不存在任何把 ``auto_confirm`` 置 ``True`` 的代码路径;验证码
    输入与最终确认始终由人工在执行器 HUMAN_GATE 步骤完成(dry_run 除外,
    干跑连浏览器都不启动)。
25. **批量确认声明(留痕)**:声明核验在上游 A110/A111 收口,``run_batch``
    收到的 items 即"已声明组"的条目;本模块不再重复校验,但每条提交成功的
    审计事件(``batch_submit``)都会落盘留痕。
26. **批量模式频控不得放宽**:每条提交前强制过 ``RateLimiter.can_submit``
    (缺省最小间隔取 ``max(batch_item_interval_s, submit_min_interval_s)``、
    每日上限 ``submit_max_per_day``);额度/间隔不通过时**整体挂起返回**
    (不 sleep 死等),状态可续批;单批条数超过 ``cfg.batch_max_items``
    直接抛中文 ``ValueError`` 拒绝运行。

每条的处理顺序(契约 §4 A112):
① ``stop()`` 回调为 True → 暂停返回(``paused=True``,已完成结果保留);
② ``state.mark(entry_id, "running")``;③ 频控不通过 → 该条 ``rate_limited``、
整体挂起返回;④ ``planner(entry, cfg)`` 构造计划;⑤ ``executor(plan, cfg,
auto_confirm=False, dry_run=...)``;⑥ ``submitted`` → ``state.mark(submitted)``
+ ``rate.record()`` + 审计;⑦ 异常/失败 → ``state.mark(failed, 中文原因)``
继续下一条;⑧ 条间 ``sleep(cfg.batch_item_interval_s)`` 仅真实模式执行。

全部依赖(executor / 12377 / shdf 计划器 / RateLimiter / 审计日志器)均
惰性导入或由调用方注入,测试可完全离线(零 playwright、零网络)。

用法示例(fake 注入,离线)::

    from netsentinel.submit.batch_submit import run_batch

    summary = run_batch(items, cfg, executor=fake_exec, rate=fake_rate,
                        state=fake_state, dry_run=True)
    # summary == {"submitted": n, "failed": n, "rate_limited": bool,
    #             "paused": bool, "results": [...], "note": "中文说明"}
"""
from __future__ import annotations

import importlib
import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config, Portal

__all__ = ["run_batch", "SUPPORTED_PORTALS"]

logger = logging.getLogger(__name__)

#: 支持的举报门户取值(与 contracts.Portal 对齐)
SUPPORTED_PORTALS: frozenset[str] = frozenset({Portal.P12377.value, Portal.SHDF.value})

#: 每条 item 必须携带的字段(缺一即中文 ValueError)
_REQUIRED_KEYS: tuple[str, ...] = ("entry_id", "group_name", "portal")

#: 提交成功的审计事件名(JsonlAuditLogger 自动附 ts/event)
_AUDIT_EVENT = "batch_submit"

# 遥测指标名(V5 规范 <模块>.<动作>;只记名称与数字,不记 URL 内容)
_METRIC_RUN = "batch.run"
_METRIC_SUBMITTED = "batch.submitted"
_METRIC_FAILED = "batch.failed"
_METRIC_RATE_LIMITED = "batch.rate_limited"
_METRIC_PAUSED = "batch.paused"


# ---------------------------------------------------------------------------
# 惰性加载与纯辅助
# ---------------------------------------------------------------------------


def _load(module_name: str) -> Any:
    """惰性导入兄弟模块;缺失时抛中文 RuntimeError(指明模块名)。"""
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(f"模块 {module_name} 未就位:{exc}") from exc


def _portal_text(value: Any) -> str:
    """portal 字段归一为展示/分派用的纯字符串。

    ``Portal`` 枚举取 ``.value``;其余取 ``str().strip().lower()``。
    """
    if isinstance(value, Portal):
        return value.value
    return str(value).strip().lower()


def _pick_planner(portal_text: str, plan_12377: Any, plan_shdf: Any) -> Any:
    """按门户取计划器;未知门户抛中文 ValueError(按单条失败处理)。"""
    if portal_text == Portal.P12377.value:
        return plan_12377
    if portal_text == Portal.SHDF.value:
        return plan_shdf
    raise ValueError(
        f"不支持的举报门户:{portal_text!r}(仅支持 {'/'.join(sorted(SUPPORTED_PORTALS))})"
    )


def _validate_items(items: Any, cfg: Config) -> None:
    """前置校验:items 为列表、每条含必填字段、条数不超过单批上限。

    - 非列表(含 str/bytes/dict)→ 中文 ValueError;
    - 某条不是映射或缺 entry_id/group_name/portal → 中文 ValueError 指明序号;
    - ``len(items) > cfg.batch_max_items`` → 中文 ValueError(红线 26)。
    - 空列表**不**在此抛错:由 :func:`run_batch` 直接返回零值摘要。
    """
    if isinstance(items, (str, bytes)) or not isinstance(items, (list, tuple)):
        raise ValueError(
            f"items 必须是举报条目列表(list),当前类型是 {type(items).__name__}"
        )
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            raise ValueError(
                f"第 {idx} 条举报条目必须是字典/映射,当前类型是 {type(item).__name__}"
            )
        missing = [key for key in _REQUIRED_KEYS if item.get(key) in (None, "")]
        if missing:
            raise ValueError(
                f"第 {idx} 条举报条目缺少必填字段:{'、'.join(missing)}"
                "(每条需含 entry_id / group_name / portal)"
            )
    if len(items) > int(cfg.batch_max_items):
        raise ValueError(
            f"批量条数 {len(items)} 超过单批上限 {cfg.batch_max_items}:"
            "红线 26,批量模式不得放宽频控与额度约束,请拆分批次或调高"
            " batch_max_items(上限 50)后分批执行"
        )


def _result_row(
    entry_id: Any,
    group_name: Any,
    portal_text: str,
    *,
    ok: bool,
    submitted: bool,
    error: str,
) -> dict[str, Any]:
    """构造返回摘要中的单条结果行(字段与顺序固定,供 A114 报告渲染)。"""
    return {
        "entry_id": entry_id,
        "group_name": group_name,
        "ok": ok,
        "submitted": submitted,
        "error": error,
        "portal": portal_text,
    }


def _mark_state(state: Any, entry_id: Any, status: str, error: str = "") -> None:
    """鸭子访问批次状态器:``state.mark(entry_id, status, error="")``。

    ``state`` 为 ``None`` 时静默跳过;mark 自身异常只告警不中断——提交证据
    已由频控时间戳与审计日志双重留痕,状态器故障不应阻断批量主流程。
    batch_id 绑定与 resume 语义由调用方(A113 适配层)保证。
    """
    if state is None:
        return
    try:
        if error:
            state.mark(entry_id, status, error=error)
        else:
            state.mark(entry_id, status)
    except Exception:  # pragma: no cover - 状态器实现问题,防御性兜底
        logger.warning(
            "批次状态记录失败(entry_id=%s status=%s)", entry_id, status, exc_info=True
        )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def run_batch(
    items: list[dict[str, Any]],
    cfg: Config,
    *,
    executor: Callable[..., Any] | None = None,
    plan_12377: Callable[[Any, Config], Any] | None = None,
    plan_shdf: Callable[[Any, Config], Any] | None = None,
    rate: Any | None = None,
    state: Any | None = None,
    dry_run: bool | None = None,
    stop: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """顺序逐条执行批量举报,返回中文可读的批次摘要。

    :param items: 待提交条目列表,每条至少含 ``entry_id`` / ``group_name`` /
        ``portal``("12377" / "shdf",也接受 :class:`Portal` 枚举);条目上
        其余字段(如 site_url / verdict / evidence_zip)由计划器自行取用,
        本模块不裁剪。空列表返回零值摘要;条数超过 ``cfg.batch_max_items``
        抛中文 ``ValueError``(红线 26)。
    :param cfg: 全局配置。
    :param executor: 注入的执行器;缺省惰性取
        ``submit.executor_playwright.execute``(签名 ``execute(plan, cfg, *,
        auto_confirm, dry_run, ...)``)。
    :param plan_12377 / plan_shdf: 注入的两门户计划器 ``(entry, cfg) -> plan``;
        缺省惰性取 ``portal_12377.plan_12377`` / ``portal_shdf.plan_shdf``,
        按条目 ``portal`` 字段分派。
    :param rate: 注入的频控器(须有 ``can_submit() -> (bool, 中文原因)`` 与
        ``record()``);缺省惰性构造 ``RateLimiter(cfg.data_dir +
        "/rate_limit.json", max(cfg.batch_item_interval_s,
        cfg.submit_min_interval_s), cfg.submit_max_per_day)``——批量间隔与
        单条最小间隔取更严者,每日额度照常生效(红线 26)。
    :param state: 注入的批次状态器(鸭子接口 ``mark(entry_id, status,
        error="")``,状态 ∈ running/submitted/failed/skipped/rate_limited);
        缺省 ``None`` 表示不落批次状态。
    :param dry_run: 干跑覆盖;``None`` 时取 ``cfg.dry_run_default``。干跑跳过
        条间 sleep。
    :param stop: 暂停回调,每条开始前检查一次;返回 True 即暂停返回
        (``paused=True``,已完成结果保留,可续批)。
    :param sleep: 条间等待函数(测试注入;真实模式条间等待
        ``cfg.batch_item_interval_s`` 秒)。
    :return: ``{"submitted": n, "failed": n, "rate_limited": bool, "paused": bool,
        "results": [{entry_id, group_name, ok, submitted, error, portal}], "note": 中文说明}``。
        频控不通过时**整体挂起**(不 sleep 死等),``note`` 说明"额度/间隔限制,
        可续批";``results`` 只含已处理的条目。

    安全:``executor`` 恒以 ``auto_confirm=False`` 调用(红线 24,本模块
    不存在任何置 True 的路径);单条计划器/执行器异常只记该条失败并继续
    下一条,绝不中断整批。
    """
    _validate_items(items, cfg)
    if not items:
        # 空批次:无事可做,返回零值摘要(不构造任何惰性依赖、不落盘)
        return {
            "submitted": 0,
            "failed": 0,
            "rate_limited": False,
            "paused": False,
            "results": [],
            "note": "",
        }

    effective_dry_run: bool = bool(cfg.dry_run_default) if dry_run is None else bool(dry_run)

    # 惰性依赖:校验通过后才解析,纯校验失败不触碰磁盘/兄弟模块
    if executor is None:
        executor = _load("netsentinel.submit.executor_playwright").execute
    if plan_12377 is None:
        plan_12377 = _load("netsentinel.submit.portal_12377").plan_12377
    if plan_shdf is None:
        plan_shdf = _load("netsentinel.submit.portal_shdf").plan_shdf
    if rate is None:
        rate_module = _load("netsentinel.submit.rate_limit")
        rate = rate_module.RateLimiter(
            f"{cfg.data_dir}/rate_limit.json",
            max(int(cfg.batch_item_interval_s), int(cfg.submit_min_interval_s)),
            int(cfg.submit_max_per_day),
        )

    audit_logger: Any = None

    def _audit() -> Any:
        """惰性构造审计日志器(首次提交成功才落盘;复用 cfg.audit_path)。"""
        nonlocal audit_logger
        if audit_logger is None:
            logging_util = _load("netsentinel.logging_util")
            audit_logger = logging_util.JsonlAuditLogger(cfg.audit_path)
        return audit_logger

    submitted_n = 0
    failed_n = 0
    rate_limited = False
    paused = False
    note = ""
    results: list[dict[str, Any]] = []
    total = len(items)

    with telemetry.timer(_METRIC_RUN):
        for index, item in enumerate(items, start=1):
            # ① 暂停检查(每条开始前;Ctrl-C 语义由调用方转接为 stop 回调)
            if stop is not None and stop():
                paused = True
                note = (
                    f"批量在第 {index} 条前暂停(stop 回调触发),"
                    f"已完成 {len(results)} 条,可续批"
                )
                telemetry.inc(_METRIC_PAUSED)
                logger.info("批量暂停:index=%d done=%d", index, len(results))
                break

            entry_id = item.get("entry_id")
            group_name = item.get("group_name")
            portal_text = _portal_text(item.get("portal"))

            # ② 状态 → running
            _mark_state(state, entry_id, "running")

            # ③ 频控前置:不通过 → 该条 rate_limited,整体挂起返回(不 sleep 死等)
            allowed, reason = rate.can_submit()
            if not allowed:
                why = f"额度/间隔限制,可续批:{reason}"
                _mark_state(state, entry_id, "rate_limited", error=why)
                rate_limited = True
                note = why
                telemetry.inc(_METRIC_RATE_LIMITED)
                results.append(
                    _result_row(
                        entry_id,
                        group_name,
                        portal_text,
                        ok=False,
                        submitted=False,
                        error=why,
                    )
                )
                logger.warning("频控挂起:index=%d reason=%s", index, reason)
                break

            # ④⑤ 计划构造 + 执行器(auto_confirm 恒 False——红线 24)
            try:
                planner = _pick_planner(portal_text, plan_12377, plan_shdf)
                plan = planner(item, cfg)
                result = executor(plan, cfg, auto_confirm=False, dry_run=effective_dry_run)
            except Exception as exc:  # ⑦ 单条异常 → 失败并继续下一条
                error = f"执行异常:{type(exc).__name__}:{exc}"
                _mark_state(state, entry_id, "failed", error=error)
                failed_n += 1
                telemetry.inc(_METRIC_FAILED)
                results.append(
                    _result_row(
                        entry_id,
                        group_name,
                        portal_text,
                        ok=False,
                        submitted=False,
                        error=error,
                    )
                )
                logger.warning("批量单条异常:index=%d entry=%s(%s)", index, entry_id, exc)
            else:
                if result is None:
                    error = "执行失败:执行器未返回任何结果"
                    _mark_state(state, entry_id, "failed", error=error)
                    failed_n += 1
                    telemetry.inc(_METRIC_FAILED)
                    results.append(
                        _result_row(
                            entry_id, group_name, portal_text,
                            ok=False, submitted=False, error=error,
                        )
                    )
                elif getattr(result, "submitted", False):
                    # ⑥ 提交成功:状态 → submitted + 频控记账 + 审计留痕
                    _mark_state(state, entry_id, "submitted")
                    rate.record()
                    _audit().log_event(
                        _AUDIT_EVENT,
                        entry_id=entry_id,
                        group_name=group_name,
                        portal=portal_text,
                        dry_run=effective_dry_run,
                        auto_confirm=False,
                        index=index,
                        total=total,
                    )
                    submitted_n += 1
                    telemetry.inc(_METRIC_SUBMITTED)
                    results.append(
                        _result_row(
                            entry_id, group_name, portal_text,
                            ok=True, submitted=True, error="",
                        )
                    )
                elif getattr(result, "ok", False):
                    # 执行成功但未真实提交(典型:干跑)→ 该条跳过,不算失败
                    _mark_state(state, entry_id, "skipped", error="干跑/未真实提交")
                    results.append(
                        _result_row(
                            entry_id, group_name, portal_text,
                            ok=True, submitted=False, error="",
                        )
                    )
                else:
                    stopped = getattr(result, "stopped_at", "") or "未知步骤"
                    error = f"执行失败:{stopped}"
                    _mark_state(state, entry_id, "failed", error=error)
                    failed_n += 1
                    telemetry.inc(_METRIC_FAILED)
                    results.append(
                        _result_row(
                            entry_id, group_name, portal_text,
                            ok=False, submitted=False, error=error,
                        )
                    )

            # ⑧ 条间等待:仅真实模式且还有下一条(dry_run 跳过;末条后不等)
            if not effective_dry_run and index < total:
                sleep(float(cfg.batch_item_interval_s))

    summary = {
        "submitted": submitted_n,
        "failed": failed_n,
        "rate_limited": rate_limited,
        "paused": paused,
        "results": results,
        "note": note,
    }
    logger.info(
        "批量结束:submitted=%d failed=%d rate_limited=%s paused=%s",
        submitted_n,
        failed_n,
        rate_limited,
        paused,
    )
    return summary
