# -*- coding: utf-8 -*-
"""顺序举报代理(NetSentinel V9 · A170,并入 A174 编号)—— 收官阶段的批量
举报**编排器**。

依据 CONTRACTS-V9 §2 A170 行与 §0 红线 36 实现:批量扫描与结案汇总
(A168 SummaryAgent)跑完、运营者逐组完成批量确认声明(A111 batch_tui,
红线 25 在上游收口)之后,由本代理把"已声明组"的条目**顺序**送入
A112 ``submit.batch_submit.run_batch`` 批量链:

- **只编排、不提交**:本代理对每一条举报没有任何自主决定权——
  ``run_batch`` 内部恒以 ``auto_confirm=False`` 调用执行器(其函数缺省值
  即 False,本模块调用时**不传该参数**,源码中也不存在任何把它置 True
  的字样,红线 36,静态断言守卫);验证码输入与最终确认始终由人工在
  执行器 HUMAN_GATE 步骤完成;
- **不建批、不声明**``batch_id`` / 批次状态器(``state``)由调用方
  (A169 finishflow 经 A113 ``BatchState.bound``)绑定,本代理不创建批次、
  不写声明、不触碰 A110 复核队列;
- **频控原样**红线 26/35:举报频控与每日额度全部沿用 V6 ``run_batch``
  链路,本模块不作任何放宽或绕过;
- **会话复用可选**``cfg.browser_session_reuse`` 为真(缺省)时惰性构造
  V7 ``SessionExecutor``:一次浏览器会话内逐条 ``.run(plan)``、收尾
  ``.close()``(构造后逐条执行,异常时 ``finally`` 兜底关闭);显式注入
  ``executor`` / ``executor_cls`` 优先于该缺省;两者都缺席且关闭复用时,
  不传 executor——``run_batch`` 落回缺省 ``executor_playwright.execute``。

可观测性与产物:

- 逐条进度:``on_item(index, total, item)`` 回调在**每条实际进入执行前**
  触发(经计划器包装实现;被 stop 暂停 / 频控挂起跳过的条目不回调),
  回调自身异常只告警吞没,绝不中断批量;
- 结案报告:``batch_id`` 参数传入时,结束后经 A114
  ``report.batch_report.render_batch_report`` 渲染到
  ``cfg.data_dir/finish_report_{batch_id}.html``(渲染器可注入 / 可
  monkeypatch 模块属性;渲染失败只降级告警,不影响批量结果);
- 返回 ``{"result": run_batch 摘要, "report_path": 路径或 None,
  "submitted": 提交成功条数}``;
- 遥测(只记名称与数字):``seq_report.run`` 计数、
  ``seq_report.submitted`` 仪表。

用法示例(fake 注入,离线)::

    from netsentinel.agent.sequential_report import SequentialReportAgent

    agent = SequentialReportAgent(cfg)
    out = agent.run(items, dry_run=True, state=st.bound(bid),
                    on_item=lambda i, n, item: print(f"{i}/{n} {item}"),
                    batch_id=bid)
    out["result"]["submitted"], out["report_path"], out["submitted"]

也兼容 A169 finishflow 的调用形态
``SequentialReportAgent().run(items, cfg, dry_run=..., state=...)``
(cfg / state / executor 既可在构造时给,也可在 run 时给,run 优先)。

兄弟模块(A112 run_batch / A114 batch_report / V7 SessionExecutor)一律
惰性导入 + 鸭子注入,测试可完全离线(零 playwright、零网络、零门户)。
仅使用标准库;Python 3.10+。
"""
from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from netsentinel import telemetry

__all__ = ["SequentialReportAgent", "REPORT_NAME_TEMPLATE"]

logger = logging.getLogger(__name__)

#: 结案报告文件名模板(落 cfg.data_dir 下;batch_id 由调用方绑定)
REPORT_NAME_TEMPLATE = "finish_report_{}.html"

#: 遥测指标名(V5 规范 <模块>.<动作>;只记名称与数字,不记 URL/内容)
_METRIC_RUN = "seq_report.run"
_METRIC_SUBMITTED = "seq_report.submitted"

#: 模块级缝(测试可 monkeypatch 整体替换;缺省惰性解析兄弟模块):
#: A112 批量顺序提交引擎 run_batch
_run_batch: Callable[..., Any] | None = None
#: A114 批量结案报告渲染器 render_batch_report
_render_batch_report: Callable[..., str] | None = None
#: V7 会话复用执行器 SessionExecutor
_session_executor_cls: Any = None


# ---------------------------------------------------------------------------
# 惰性导入与解析辅助
# ---------------------------------------------------------------------------
def _lazy_import(module_name: str, attr: str) -> Any:
    """惰性导入 ``module_name.attr``;ImportError / 缺属性 → 中文 RuntimeError。"""
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(f"模块 {module_name} 未就位:{exc}") from exc
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise RuntimeError(f"模块 {module_name} 未提供 {attr}:{exc}") from exc


def _resolve_run_batch() -> Callable[..., Any]:
    """解析批量执行引擎:模块缝已注入则原样用,否则惰性取 A112 ``run_batch``。"""
    global _run_batch
    if _run_batch is None:
        _run_batch = _lazy_import("netsentinel.submit.batch_submit", "run_batch")
    return _run_batch


def _resolve_renderer() -> Callable[..., str]:
    """解析结案报告渲染器:缺省惰性取 A114 ``render_batch_report``。"""
    global _render_batch_report
    if _render_batch_report is None:
        _render_batch_report = _lazy_import(
            "netsentinel.report.batch_report", "render_batch_report"
        )
    return _render_batch_report


def _resolve_session_executor_cls() -> Any:
    """解析会话执行器类:缺省惰性取 V7 ``SessionExecutor``。"""
    global _session_executor_cls
    if _session_executor_cls is None:
        _session_executor_cls = _lazy_import(
            "netsentinel.submit.executor_session", "SessionExecutor"
        )
    return _session_executor_cls


def _submitted_of(result: Any) -> int:
    """从 run_batch 摘要鸭子取提交成功条数;缺失 / 非法 → 0。"""
    if not isinstance(result, Mapping):
        return 0
    try:
        return int(result.get("submitted") or 0)
    except (TypeError, ValueError):
        return 0


def _safe_on_item(on_item: Callable[..., Any], index: int, total: int, item: Any) -> None:
    """触发逐条进度回调;回调自身任何异常只告警吞没(进度钩子不得中断批量)。"""
    try:
        on_item(index, total, item)
    except Exception:  # noqa: BLE001 - 进度回调异常吞没,绝不阻断举报链
        logger.warning("on_item 进度回调异常已吞没(index=%d)", index, exc_info=True)


def _bind_session(inst: Any) -> Callable[..., Any]:
    """把会话实例包装成 run_batch 的执行器形状 ``(plan, cfg, **kw)``。

    只透传 ``dry_run``;``auto_confirm`` 刻意**不转发**——会话执行器的
    参数缺省值即 False(人工门,红线 36),本包装不存在置 True 的路径。
    """
    def executor(plan: Any, cfg_: Any, **kw: Any) -> Any:
        return inst.run(plan, dry_run=kw.get("dry_run"))

    return executor


def _safe_close(inst: Any) -> None:
    """收尾关闭会话实例;close 自身异常只告警(结果与关闭互不影响)。"""
    try:
        inst.close()
    except Exception:  # noqa: BLE001 - 关闭兜底,绝不吞掉批量主结果
        logger.warning("关闭会话执行器失败(已忽略)", exc_info=True)


def _wrap_planner(
    portal_key: str,
    on_item: Callable[..., Any],
    box: dict[str, int],
    total: int,
) -> Callable[[Any, Any], Any]:
    """包装单个门户计划器:每条构造计划前触发 ``on_item``(红线 36 的进度钩子)。

    真实计划器首次调用时惰性解析并缓存;条目计数与门户无关地按实际进入
    执行的顺序递增(stop 暂停 / 频控挂起时 run_batch 不会调到计划器,
    因此被跳过的条目不产生回调)。
    """
    module_name, attr = (
        ("netsentinel.submit.portal_12377", "plan_12377")
        if portal_key == "12377"
        else ("netsentinel.submit.portal_shdf", "plan_shdf")
    )
    base: Callable[[Any, Any], Any] | None = None

    def planner(entry: Any, cfg_: Any) -> Any:
        nonlocal base
        box["index"] += 1
        _safe_on_item(on_item, box["index"], total, entry)
        if base is None:
            base = _lazy_import(module_name, attr)
        return base(entry, cfg_)

    return planner


# ---------------------------------------------------------------------------
# SequentialReportAgent:顺序举报代理本体
# ---------------------------------------------------------------------------
class SequentialReportAgent:
    """顺序举报代理(A170,并入 A174):收官阶段的批量举报**编排器**。

    :meth:`run` 把已声明组的条目一次交给 A112 ``run_batch`` 顺序逐条执行
    (恒人工门、频控原样,红线 36);本代理自身**无自主提交权**——不创建
    批次、不写声明、不改条目状态,一切提交仍走"逐组声明 → 逐条人工门"
    的人工链路。

    :param cfg: 全局配置(也可在 :meth:`run` 处再给,run 处优先);缺省
        执行器策略读 ``cfg.browser_session_reuse``。
    :param executor: 注入的执行器(最高优先;``(plan, cfg, **kw)`` 形状,
        run_batch 以 ``auto_confirm=False`` / ``dry_run`` 关键字调用)。
    :param executor_cls: 注入的会话执行器**类**(次优先;实例化一次、逐条
        ``.run(plan, dry_run=...)``、``finally`` 收尾 ``.close()``)。
    :param state: 调用方绑定的批次状态器(``mark(entry_id, status, error="")``
        鸭子;A169 经 A113 ``BatchState.bound(batch_id)`` 注入)。
    :param runner: 批量执行引擎缝(缺省惰性 A112 ``run_batch``;测试注入)。
    :param renderer: 结案报告渲染器缝(缺省惰性 A114
        ``render_batch_report(batch_id, summary, out_path)``;测试注入)。
    """

    def __init__(
        self,
        cfg: Any = None,
        *,
        executor: Callable[..., Any] | None = None,
        executor_cls: Any = None,
        state: Any = None,
        runner: Callable[..., Any] | None = None,
        renderer: Callable[..., str] | None = None,
    ) -> None:
        self._cfg = cfg
        self._executor = executor
        self._executor_cls = executor_cls
        self._state = state
        self._runner = runner
        self._renderer = renderer

    # ------------------------------------------------------------------ API
    def run(
        self,
        items: list[dict[str, Any]],
        cfg: Any = None,
        *,
        executor: Callable[..., Any] | None = None,
        executor_cls: Any = None,
        state: Any = None,
        dry_run: bool | None = None,
        on_item: Callable[[int, int, Any], None] | None = None,
        stop: Callable[[], bool] | None = None,
        batch_id: Any = None,
        runner: Callable[..., Any] | None = None,
        renderer: Callable[..., str] | None = None,
    ) -> dict[str, Any]:
        """顺序编排一批举报条目,返回 ``{"result", "report_path", "submitted"}``。

        :param items: 待提交条目列表(每条至少含 ``entry_id`` / ``group_name``
            / ``portal``;校验与单批上限由 A112 ``run_batch`` 收口,中文
            ``ValueError`` 原样上抛)。
        :param cfg: 全局配置(run 处优先于构造处;两处都没有 → 中文
            ``ValueError``)。读取 ``browser_session_reuse`` / ``data_dir`` /
            ``dry_run_default``(经 run_batch)。
        :param executor / executor_cls / state: 同构造参数(run 处优先);
            三者优先级:executor 注入 > executor_cls > cfg 复用缺省。
        :param dry_run: 干跑覆盖,``None`` 原样透传(run_batch 落回
            ``cfg.dry_run_default``);本模块不在此处改动任何提交语义。
        :param on_item: 逐条进度回调 ``(index, total, item)``(1 起序,
            每条实际进入执行前触发;异常吞没;``None`` 不启用,也不向
            run_batch 传计划器包装)。
        :param stop: 暂停回调(透传 run_batch:每条开始前为真即暂停返回,
            已完成结果保留可续批)。
        :param batch_id: 批次号(调用方绑定):非 ``None`` 时结束后渲染
            A114 结案报告到 ``cfg.data_dir/finish_report_{batch_id}.html``,
            路径放入返回值 ``report_path``;渲染失败只降级告警。
        :param runner / renderer: 引擎 / 渲染器注入(run 处优先于构造处)。
        :return: ``{"result": run_batch 批次摘要, "report_path": 报告路径
            或 None, "submitted": 提交成功条数}``。
        :raises ValueError: cfg 两处均未提供(中文)。
        :raises RuntimeError: 缺省依赖(A112 / A114 / V7 SessionExecutor)
            未就位(中文,指明模块名)。

        安全(红线 36):调用 ``run_batch`` 时**不传** ``auto_confirm``
        (其函数缺省即 False,逐条 HUMAN_GATE 人工门);源码无任何置
        True 的路径;本代理不建批、不声明、不绕过 attest,声明核验
        由上游 A110/A111 人工链路完成。
        """
        effective_cfg = cfg if cfg is not None else self._cfg
        if effective_cfg is None:
            raise ValueError(
                "缺少全局配置 cfg:请在 SequentialReportAgent(cfg) 构造时"
                " 或 run(items, cfg) 调用时提供"
            )
        effective_state = state if state is not None else self._state
        effective_executor = executor if executor is not None else self._executor
        effective_cls = executor_cls if executor_cls is not None else self._executor_cls
        effective_runner = runner if runner is not None else self._runner
        effective_renderer = renderer if renderer is not None else self._renderer

        telemetry.inc(_METRIC_RUN)

        # ---- 引擎参数组装:恒不传 auto_confirm(其缺省即 False,红线 36)----
        kwargs: dict[str, Any] = {"state": effective_state, "dry_run": dry_run}
        session_inst: Any = None
        if effective_executor is not None:
            kwargs["executor"] = effective_executor
        else:
            cls = effective_cls
            if cls is None and bool(
                getattr(effective_cfg, "browser_session_reuse", False)
            ):
                cls = _resolve_session_executor_cls()
            if cls is not None:
                session_inst = cls(effective_cfg)
                kwargs["executor"] = _bind_session(session_inst)
                logger.info("会话执行器已构造:批量执行复用一次浏览器会话")
        if stop is not None:
            kwargs["stop"] = stop
        if on_item is not None:
            total = len(items) if isinstance(items, (list, tuple)) else 0
            box = {"index": 0}
            kwargs["plan_12377"] = _wrap_planner("12377", on_item, box, total)
            kwargs["plan_shdf"] = _wrap_planner("shdf", on_item, box, total)

        # ---- 顺序编排:一次 run_batch 逐条执行;会话 finally 收尾 ----
        run_fn = effective_runner if effective_runner is not None else _resolve_run_batch()
        logger.info(
            "顺序举报编排开始:items=%d dry_run=%s batch_id=%s",
            len(items) if isinstance(items, (list, tuple)) else -1,
            dry_run, batch_id,
        )
        try:
            result = run_fn(items, effective_cfg, **kwargs)
        finally:
            if session_inst is not None:
                _safe_close(session_inst)

        submitted = _submitted_of(result)
        telemetry.gauge(_METRIC_SUBMITTED, submitted)
        logger.info(
            "顺序举报编排结束:submitted=%d batch_id=%s", submitted, batch_id
        )
        report_path = (
            self._render_report(effective_renderer, batch_id, result, effective_cfg)
            if batch_id is not None
            else None
        )
        return {"result": result, "report_path": report_path, "submitted": submitted}

    # ------------------------------------------------------------- 内部实现
    @staticmethod
    def _render_report(
        renderer: Callable[..., str] | None,
        batch_id: Any,
        summary: Any,
        cfg: Any,
    ) -> str | None:
        """渲染 A114 结案报告并返回落盘路径;失败降级为 None(只告警)。

        报告只是留痕核对材料(A114 自身不触发任何提交);批量结果永远
        先于渲染返回,渲染异常绝不吞掉已完成的举报摘要。
        """
        render_fn = renderer if renderer is not None else _resolve_renderer()
        data_dir = str(getattr(cfg, "data_dir", "data") or "data")
        out_path = Path(data_dir) / REPORT_NAME_TEMPLATE.format(batch_id)
        try:
            render_fn(batch_id, summary, str(out_path))
        except Exception as exc:  # noqa: BLE001 - 渲染失败降级,不影响批量结果
            logger.warning("结案报告渲染失败(已降级为无报告):%s", exc, exc_info=True)
            return None
        logger.info("顺序举报结案报告已写出:%s", out_path)
        return str(out_path)
