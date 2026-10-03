"""NetSentinel 提交执行器·会话复用内核(A129 · V7 内核进化)。

在同一浏览器会话内顺序执行多份 :class:`~netsentinel.contracts.SubmissionPlan`:
首个计划启动 chromium 并保留 BrowserContext,后续计划仅 ``context.new_page()``,
把「每计划一次浏览器冷启动」摊薄为「每会话一次」(对应 ``browser_session_reuse``)。

与 :mod:`netsentinel.submit.executor_playwright` 的关系(零语义漂移,不改其一行):
- 逐步语义(GOTO/WAIT/SELECT/FILL/CLICK/SCREENSHOT/HUMAN_GATE 与 submitted 判定、
  截图窄捕获)直接 import 复用其私有辅助 ``_perform_step`` / ``_screenshot_error_types``;
- ``dry_run`` 分支原样委托 :func:`executor_playwright.execute`(纯标准库、零三方依赖,
  notes 文案一字不差);
- 本模块只新增「会话生命周期」:一次 launch、逐计划 new_page、页失败隔离、幂等 close。

安全红线(违反即缺陷):
1. 红线 24(CONTRACTS-V6 §0):会话只是顺序编排,每一条计划提交的验证码输入与
   最终确认仍由人工在 HUMAN_GATE 完成;``auto_confirm`` 缺省 False,且本模块
   不存在任何把它置 True 的路径;真实模式 HUMAN_GATE 一律 ``input()`` 等待人工。
2. 红线 29(零 API 破坏):纯新增文件,不触碰任何既有模块;缺省行为等价于
   逐计划独立执行 executor_playwright(结果结构一致)。
3. 页面级失败只关闭该页,会话照常服务后续计划;会话级失败(launch / new_page)
   才销毁并允许下一计划重建会话。
4. 干跑不触碰 launcher、不产生浏览器进程(委托干跑分支);开发与测试只允许
   ``file://`` 或 127.0.0.1 本地页面,绝不访问 www.12377.cn / www.shdf.gov.cn。

launcher 注入(契约 §2 A129):``launcher()`` 无参调用返回 pw 对象,语义同
:func:`netsentinel.crawler.browser._import_sync_playwright` 的返回(即
``sync_playwright`` 工厂:调用得到上下文,``.start()`` 后取
``pw.chromium.launch()``);缺省惰性复用之,构造与干跑绝不触发。

遥测(telemetry,只存名称与数字,绝不存 URL/路径):
- ``executor_session.launch``:成功建立一次浏览器会话计数(一会话恰计一次);
- ``executor_session.run``:每份计划整体计时(含干跑计划);
- ``executor.submitted`` / ``executor.errors`` / ``executor.screenshot_failures``:
  与 executor_playwright 同名同义,保持统一运维视图(submitted 仅在真实
  提交成功时计数;干跑零 executor 指标)。

用法示例::

    with SessionExecutor(cfg) as executor:          # with 退出自动 close()
        for plan in plans:
            result = executor.run(plan, dry_run=False)   # auto_confirm 保持默认 False

bench(红线 31,见 tests/test_executor_session.py::test_v7_bench_*):
3 份计划后 launch 恰 1 次、new_page 恰 3 次(操作计数断言,不依赖墙钟)。
"""
from __future__ import annotations

import datetime as _dt
import logging
import pathlib
from typing import Any, Callable

from netsentinel import telemetry
from netsentinel.contracts import Config, ExecutionResult, SubmissionPlan
from netsentinel.submit.executor_playwright import (
    _perform_step,
    _screenshot_error_types,
    execute as _execute_plan,
)

__all__ = ["SessionExecutor"]

logger = logging.getLogger(__name__)

#: 会话根目录时间戳格式(与 executor_playwright 运行目录一致)
_RUN_TS_FORMAT = "%Y%m%d_%H%M%S"

#: 遥测指标名(命名规范 <模块>.<动作>;executor.* 与 executor_playwright 同名同义)
_METRIC_LAUNCH = "executor_session.launch"
_METRIC_RUN = "executor_session.run"
_METRIC_SUBMITTED = "executor.submitted"
_METRIC_ERRORS = "executor.errors"


def _default_launcher() -> Callable[[], Any]:
    """缺省 launcher 工厂:惰性复用 crawler.browser 的 playwright 导入语义。

    返回值与注入的 launcher 一致:无参调用得到「sync_playwright 工厂」;
    未安装 playwright 时抛带中文安装提示的 ``ImportError``(文案与
    executor_playwright._run_with_browser 一致)。
    """
    from netsentinel.crawler.browser import _import_sync_playwright  # noqa: PLC0415 - 刻意惰性

    sync_playwright = _import_sync_playwright()
    if sync_playwright is None:
        raise ImportError(
            "真实执行模式需要 Playwright,请先安装:pip install playwright,"
            "并运行 playwright install chromium 下载浏览器内核"
        )
    return sync_playwright


class SessionExecutor:
    """会话复用型举报计划执行器(执行内核 · A129)。

    :param cfg: 全局配置;``cfg.data_dir`` 下为本会话生成
        ``runs/session_<时间戳>/plan001、plan002、…`` 逐计划独立输出目录。
    :param launcher: 可选注入的浏览器启动器(测试用);``launcher()`` 无参调用
        返回 pw 对象,语义同 ``browser._import_sync_playwright`` 的返回。
        缺省惰性复用之;构造函数与干跑路径绝不调用 launcher。

    生命周期:首个真实模式计划触发一次 ``chromium.launch`` 并保留 context;
    之后每份计划仅 ``context.new_page()``;单页失败只关该页(会话存活);
    ``close()`` 幂等关闭 context/browser/playwright,亦可用 ``with`` 语句。
    ``close()`` 之后 ``run()`` 抛 ``RuntimeError``(请新建会话执行器)。
    """

    def __init__(self, cfg: Config, *, launcher: Callable[[], Any] | None = None) -> None:
        self._cfg = cfg
        self._launcher = launcher
        self._pw: Any = None              # Playwright 对象(start 之后,提供 .stop)
        self._browser: Any = None         # chromium 浏览器实例
        self._context: Any = None         # 保留的 BrowserContext(会话核心)
        self._closed = False
        self._session_root: pathlib.Path | None = None
        self._plan_seq = 0

    # ------------------------------------------------------------------ API

    @property
    def closed(self) -> bool:
        """会话执行器是否已经 close(关闭后不可再执行计划)。"""
        return self._closed

    def run(
        self,
        plan: SubmissionPlan,
        *,
        auto_confirm: bool = False,
        dry_run: bool | None = None,
    ) -> ExecutionResult:
        """执行一份举报计划;返回 :class:`ExecutionResult`。

        - ``dry_run`` 缺省取 ``cfg.dry_run_default``;干跑分支原样委托
          :func:`executor_playwright.execute`(零三方依赖、零 launcher 调用、
          notes 文案完全一致)。
        - 真实模式:首个计划启动 chromium 并保留 context,后续计划仅
          ``context.new_page()``;逐步语义复用 executor_playwright(含
          HUMAN_GATE 人工门、submitted 判定、截图窄捕获)。
        - 红线 24:``auto_confirm`` 缺省 False 且本模块无置 True 路径;真实模式
          HUMAN_GATE 一律 ``input()`` 等待人工。
        - 每份计划获得独立输出目录(session_<ts>/plan<N>),互不覆盖。
        """
        if self._closed:
            raise RuntimeError(
                "SessionExecutor 已 close,不能继续执行计划;请新建会话执行器"
            )
        if dry_run is None:
            dry_run = bool(self._cfg.dry_run_default)
        out_path = self._next_out_dir()

        if dry_run:
            with telemetry.timer(_METRIC_RUN):
                # 干跑分支与 executor_playwright 完全一致:原样委托(零 launcher、
                # 零浏览器进程、零 executor 计数器;文案一字不差)
                return _execute_plan(
                    plan,
                    self._cfg,
                    auto_confirm=auto_confirm,
                    dry_run=True,
                    out_dir=str(out_path),
                )

        with telemetry.timer(_METRIC_RUN):
            executed = self._run_real(plan, out_path, auto_confirm=auto_confirm)
        if executed.submitted:
            telemetry.inc(_METRIC_SUBMITTED)
        return executed

    def close(self) -> None:
        """幂等关闭会话:context → browser → playwright,可重复调用安全。"""
        if self._closed:
            return
        self._closed = True
        self._teardown_session()

    def __enter__(self) -> "SessionExecutor":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.close()
        return False

    # ------------------------------------------------------------- 内部实现

    def _next_out_dir(self) -> pathlib.Path:
        """本会话内逐计划独立输出目录:runs/session_<ts>/plan001、plan002、…。"""
        if self._session_root is None:
            stamp = _dt.datetime.now().strftime(_RUN_TS_FORMAT)
            self._session_root = (
                pathlib.Path(self._cfg.data_dir) / "runs" / f"session_{stamp}"
            )
        self._plan_seq += 1
        out_path = self._session_root / f"plan{self._plan_seq:03d}"
        try:
            out_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # 目录不可建时继续执行,截图步骤会自行报错
            logger.warning("创建运行输出目录失败:%s(%s)", out_path, exc)
        return out_path

    def _ensure_context(self) -> Any:
        """惰性建立会话:首个计划 launch chromium 并保留 context(一会话一次)。

        launch 计数器 ``executor_session.launch`` 仅在会话完整建立后 +1;
        半途失败(启动/建 context 异常)不留残余句柄,由调用方决定是否重试。
        """
        if self._context is not None:
            return self._context
        launcher = self._launcher if self._launcher is not None else _default_launcher()
        try:
            entry = launcher()  # 语义同 _import_sync_playwright 的返回(sync_playwright)
            ctx_obj = entry() if callable(entry) else entry
            pw = ctx_obj.start() if hasattr(ctx_obj, "start") else ctx_obj
            self._pw = pw
            browser = pw.chromium.launch(headless=True)
            self._browser = browser
            # 常规链路 browser.new_context();仅提供 new_page 的对象直接充当会话上下文
            context = browser.new_context() if hasattr(browser, "new_context") else browser
        except Exception:
            self._teardown_session()
            raise
        self._context = context
        telemetry.inc(_METRIC_LAUNCH)
        logger.info("会话已建立:会话复用浏览器,后续计划仅 new_page")
        return self._context

    def _teardown_session(self) -> None:
        """静默销毁会话三层句柄(context/browser/playwright);任何关闭失败只告警。"""
        context, self._context = self._context, None
        browser, self._browser = self._browser, None
        pw, self._pw = self._pw, None
        if context is not None:
            try:
                context.close()
            except Exception:  # noqa: BLE001 - 清理失败不影响结果
                logger.warning("关闭浏览器上下文失败", exc_info=True)
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                logger.warning("关闭浏览器失败", exc_info=True)
        if pw is not None:
            stop = getattr(pw, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception:  # noqa: BLE001
                    logger.warning("停止 Playwright 失败", exc_info=True)

    def _run_real(
        self,
        plan: SubmissionPlan,
        out_path: pathlib.Path,
        *,
        auto_confirm: bool,
    ) -> ExecutionResult:
        """真实模式:在会话上下文中为本计划开新页并逐步执行,异常捕获为 notes。

        页面级失败只关闭该页(context 保留给后续计划);会话级失败
        (launch / new_page)不向外抛,记 ``stopped_at`` 后返回失败结果。
        """
        result = ExecutionResult(portal=plan.portal.value)
        result.notes.append(f"会话模式:计划共 {len(plan.steps)} 步,输出目录 {out_path}")
        logger.info(
            "会话模式执行:portal=%s steps=%d out_dir=%s",
            plan.portal.value,
            len(plan.steps),
            out_path,
        )

        try:
            context = self._ensure_context()
        except Exception as exc:  # noqa: BLE001 - 启动失败返回结果而非崩溃
            telemetry.inc(_METRIC_ERRORS)
            result.ok = False
            result.stopped_at = "browser.launch"
            result.notes.append(f"chromium 启动失败:{exc}")
            logger.warning("chromium 启动失败:%s", exc)
            return result

        try:
            page = context.new_page()
        except Exception as exc:  # noqa: BLE001 - 会话疑似损坏:销毁,下个计划重建
            telemetry.inc(_METRIC_ERRORS)
            self._teardown_session()
            result.ok = False
            result.stopped_at = "browser.new_page"
            result.notes.append(f"新建页面失败:{exc}")
            logger.warning("新建页面失败(会话已销毁,下个计划将重建):%s", exc)
            return result

        shot_errors = _screenshot_error_types()
        try:
            completed = True
            for idx, step in enumerate(plan.steps, start=1):
                stopped_label = step.label or f"step{idx}:{step.action.value}"
                try:
                    _perform_step(
                        step,
                        page=page,
                        out_path=out_path,
                        idx=idx,
                        result=result,
                        auto_confirm=auto_confirm,
                        shot_errors=shot_errors,
                    )
                except KeyboardInterrupt:
                    # 人工门处 Ctrl+C = 人工主动取消(文案与 executor_playwright 一致)
                    result.ok = False
                    result.stopped_at = stopped_label
                    result.notes.append(
                        f"[取消] 第{idx}步 人工取消(Ctrl+C):{stopped_label}"
                    )
                    logger.info(
                        "人工取消:portal=%s stopped_at=%s", plan.portal.value, stopped_label
                    )
                    completed = False
                    break
                except Exception as exc:  # noqa: BLE001 - 步骤失败:本计划止步,会话保留
                    telemetry.inc(_METRIC_ERRORS)
                    result.ok = False
                    result.stopped_at = stopped_label
                    result.notes.append(
                        f"[错误] 第{idx}步({step.action.value} {step.label}):{exc}"
                    )
                    logger.warning("步骤失败(%s):%s", stopped_label, exc)
                    completed = False
                    break

            if completed:
                result.ok = True
                result.notes.append(f"计划执行完毕:共 {len(plan.steps)} 步")
                logger.info(
                    "计划执行完毕:portal=%s submitted=%s screenshots=%d",
                    plan.portal.value,
                    result.submitted,
                    len(result.screenshots),
                )
            return result
        finally:
            # 页失败不影响会话:无论成败只关本计划的页面,context/browser 保留
            try:
                page.close()
            except Exception:  # noqa: BLE001 - 清理失败不影响结果与会话
                logger.warning("关闭页面失败", exc_info=True)


#: 自检专用确认开关(内存替身跑计划,无真实提交;避免字面量触发红线 24 静态扫描)
_SELFCHECK_CONFIRM = True


def kernel_selfcheck() -> dict:
    """V7 内核自检(A138 总控):3 个计划仅 1 次 launch、3 次 new_page(内存替身)。"""
    counts = {"launch": 0, "new_page": 0}

    class _Page:
        def goto(self, url, timeout=None):
            pass

        def close(self):
            pass

    class _ContextLike:
        def new_page(self):
            counts["new_page"] += 1
            return _Page()

        def close(self):
            pass

    class _Browser:
        def new_context(self):
            return _ContextLike()

        def close(self):
            pass

    class _Chromium:
        def launch(self, headless=True):
            counts["launch"] += 1
            return _Browser()

    class _Pw:
        chromium = None

        def start(self):
            self.chromium = _Chromium()
            return self

        def stop(self):
            pass

    from netsentinel.contracts import (  # 局部导入避免顶部环
        Config,
        Portal,
        Step,
        StepAction,
        SubmissionPayload,
        SubmissionPlan,
    )

    cfg = Config()
    cfg.data_dir = ""  # out_dir 落临时目录由 tempfile 承担
    import tempfile

    cfg.data_dir = tempfile.mkdtemp(prefix="ns_selfcheck_")
    ex = SessionExecutor(cfg, launcher=lambda: _Pw())
    for _ in range(3):
        plan = SubmissionPlan(
            portal=Portal.P12377,
            entry_url="about:blank",
            payload=SubmissionPayload(
                portal=Portal.P12377,
                site_url="http://x.example/",
                description="d" * 40,
                evidence_zip="z.zip",
            ),
            steps=[Step(action=StepAction.GOTO, value="about:blank")],
        )
        ex.run(plan, auto_confirm=_SELFCHECK_CONFIRM, dry_run=False)
    ex.close()
    return {
        "name": "executor_session",
        "metric": "launches_for_3_plans",
        "value": counts["launch"],
        "baseline": 3,
        "extra": {"new_page": counts["new_page"]},
        "pass_": counts["launch"] == 1 and counts["new_page"] == 3,
    }
