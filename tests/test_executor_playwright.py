"""A15 计划执行器(submit/executor_playwright)测试。

安全规则:
- 全部离线:本地页面只用 ``file://`` 指向 tests/fixtures/mini_form.html,
  绝不访问 www.12377.cn / www.shdf.gov.cn 或任何真实门户。
- 干跑用例不依赖 playwright(纯标准库路径,CI 可测);
- 真实用例 ``pytest.importorskip("playwright")``,chromium 启动失败时 skip;
- HUMAN_GATE 交互路径用 monkeypatch 替换 ``input``,不在测试里挂起等待;
- Step/Plan 直接用 contracts 数据类构造,不依赖 A12(form_models)。
"""
from __future__ import annotations

import pathlib

import pytest

from netsentinel.contracts import (
    Config,
    ExecutionResult,
    Portal,
    Step,
    StepAction,
    SubmissionPayload,
    SubmissionPlan,
)
from netsentinel import telemetry
from netsentinel.submit import executor_playwright
from netsentinel.submit.executor_playwright import (
    _perform_step,
    _screenshot_error_types,
    execute,
)

FIXTURES_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"
MINI_FORM_URI = (FIXTURES_DIR / "mini_form.html").resolve().as_uri()
#: 本地模拟门户目录:旧版页(契约 id)+ V13 改版演练页(id 全换、aria/label 保留)。
MOCK_PORTALS_DIR = pathlib.Path(__file__).resolve().parent / "mock_portals"
OLD_MOCK_URI = (MOCK_PORTALS_DIR / "12377_mock.html").resolve().as_uri()
REDESIGN_URI = (MOCK_PORTALS_DIR / "12377_mock_redesigned.html").resolve().as_uri()

_DESC = "该站点存在大量疑似色情图片,经辅助系统初筛并人工核实,附证据包。"


def _make_plan(steps: list[Step], portal: Portal = Portal.P12377) -> SubmissionPlan:
    """用 contracts 数据类直接构造计划(不依赖 A12 form_models)。"""
    payload = SubmissionPayload(
        portal=portal,
        site_url="http://example.invalid/site",
        category="色情低俗信息",
        description=_DESC,
        evidence_zip="data/evidence/example.zip",
        reporter_name="测试举报人",
        reporter_phone="13800000000",
    )
    return SubmissionPlan(
        portal=portal,
        entry_url=MINI_FORM_URI,
        payload=payload,
        steps=steps,
    )


def _standard_13_steps() -> list[Step]:
    """13 步计划:标准 12 步序列 + 1 个可跳过的选填字段。"""
    return [
        Step(StepAction.GOTO, "打开本地模拟举报页面", value=MINI_FORM_URI),
        Step(StepAction.WAIT, "等待页面加载", value="0.1"),
        Step(StepAction.SELECT, "选择信息类型", selector="#report-type", value="色情低俗信息"),
        Step(StepAction.FILL, "填写举报链接", selector="#report-url", value="http://example.invalid/site"),
        Step(StepAction.FILL, "填写具体描述", selector="#report-desc", value=_DESC),
        Step(StepAction.FILL, "填写举报人姓名", selector="#report-name", value="测试举报人"),
        Step(StepAction.FILL, "填写备用联系方式(选填)", selector="#report-phone2", value="", meta={"skippable": True}),
        Step(StepAction.FILL, "填写联系电话", selector="#report-phone", value="13800000000"),
        Step(StepAction.SCREENSHOT, "截图:填写完成"),
        Step(StepAction.HUMAN_GATE, "人工核对信息、上传证据包并输入验证码"),
        Step(StepAction.CLICK, "点击提交按钮", selector="#report-submit", meta={"submit": True}),
        Step(StepAction.WAIT, "等待提交结果", value="0.2"),
        Step(StepAction.SCREENSHOT, "截图:提交结果"),
    ]


# ---------------------------------------------------------------------------
# 干跑(不依赖 playwright)
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_standard_13_step_plan(self, tmp_path: pathlib.Path) -> None:
        cfg = Config(data_dir=str(tmp_path))
        result = execute(_make_plan(_standard_13_steps()), cfg, dry_run=True)

        assert result.ok is True
        assert result.submitted is False
        assert result.stopped_at == ""
        assert result.portal == "12377"
        # 首行横幅 + 13 条步骤记录
        assert result.notes[0] == "DRY-RUN:未驱动浏览器"
        assert len(result.notes) == 14
        assert all("[DRY]" in line for line in result.notes[1:])
        assert result.notes[1].startswith("[DRY] 1. goto")
        # 干跑不截图、不中止
        assert result.screenshots == []
        # 缺省 out_dir = <data_dir>/runs/<ts>,自动创建
        runs_dir = tmp_path / "runs"
        assert runs_dir.is_dir()
        assert len(list(runs_dir.iterdir())) == 1

    def test_dry_run_default_taken_from_config(self, tmp_path: pathlib.Path) -> None:
        cfg = Config(data_dir=str(tmp_path), dry_run_default=True)
        plan = _make_plan([Step(StepAction.GOTO, "打开页面", value=MINI_FORM_URI)])
        result = execute(plan, cfg)  # dry_run=None → 取 cfg.dry_run_default
        assert result.notes[0] == "DRY-RUN:未驱动浏览器"
        assert result.ok is True
        assert result.submitted is False

    def test_explicit_out_dir_created(self, tmp_path: pathlib.Path) -> None:
        cfg = Config(data_dir=str(tmp_path))
        out_dir = tmp_path / "custom" / "run01"
        plan = _make_plan([Step(StepAction.GOTO, "打开页面", value=MINI_FORM_URI)])
        execute(plan, cfg, dry_run=True, out_dir=str(out_dir))
        assert out_dir.is_dir()


# ---------------------------------------------------------------------------
# 真实模式(需要 playwright;chromium 缺失时 skip)
# ---------------------------------------------------------------------------

def _launch_skip(result) -> None:
    """chromium 启动失败的统一处理:skip 而非报错。"""
    if result.stopped_at == "browser.launch":
        pytest.skip("chromium 未安装")


class TestRealMode:
    def test_fill_gate_submit_screenshot(self, tmp_path: pathlib.Path) -> None:
        pytest.importorskip("playwright")
        cfg = Config(data_dir=str(tmp_path))
        steps = [
            Step(StepAction.GOTO, "打开本地模拟表单", value=MINI_FORM_URI, timeout_s=15.0),
            Step(StepAction.FILL, "填写举报链接", selector="#report-url", value="http://example.invalid/site"),
            Step(StepAction.FILL, "填写具体描述", selector="#report-desc", value=_DESC),
            Step(StepAction.HUMAN_GATE, "人工核对信息并输入验证码"),
            Step(StepAction.CLICK, "点击提交按钮", selector="#report-submit", meta={"submit": True}),
            Step(StepAction.WAIT, "等待提交结果", value="0.2"),
            Step(StepAction.SCREENSHOT, "截图:提交结果"),
        ]
        out_dir = tmp_path / "run"
        result = execute(
            _make_plan(steps), cfg,
            auto_confirm=True, dry_run=False, headless=True, out_dir=str(out_dir),
        )
        _launch_skip(result)

        assert result.ok is True
        assert result.submitted is True
        assert result.stopped_at == ""
        assert result.portal == "12377"
        # 提交后的整页截图已落盘
        assert len(result.screenshots) == 1
        shot = pathlib.Path(result.screenshots[0])
        assert shot.name == "step07_screenshot.png"
        assert shot.is_file() and shot.stat().st_size > 0
        # 测试模式下的人工门只记录,不交互
        assert any("自动确认(测试模式)" in line for line in result.notes)

    def test_human_gate_waits_for_human_input(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """红线验证:未开 auto_confirm 时,HUMAN_GATE 必须调用 input() 等待人工。"""
        pytest.importorskip("playwright")
        cfg = Config(data_dir=str(tmp_path))
        prompts: list[str] = []
        monkeypatch.setattr(
            "builtins.input", lambda prompt="": prompts.append(prompt) or ""
        )
        steps = [
            Step(StepAction.GOTO, "打开本地模拟表单", value=MINI_FORM_URI, timeout_s=15.0),
            Step(StepAction.HUMAN_GATE, "人工核对信息、上传证据包并输入验证码"),
            Step(StepAction.SCREENSHOT, "截图:人工门通过后"),
        ]
        result = execute(
            _make_plan(steps), cfg,
            auto_confirm=False, dry_run=False, out_dir=str(tmp_path / "run"),
        )
        _launch_skip(result)

        assert result.ok is True
        assert result.submitted is False
        assert len(prompts) == 1
        assert prompts[0].startswith("【人工确认】")
        assert "人工核对信息" in prompts[0]
        assert any("已人工确认" in line for line in result.notes)

    def test_goto_missing_file_stops_with_stopped_at(self, tmp_path: pathlib.Path) -> None:
        pytest.importorskip("playwright")
        cfg = Config(data_dir=str(tmp_path))
        steps = [
            Step(
                StepAction.GOTO, "打不开的页面",
                value="file:///__no_such_dir__/%s.html" % ("missing" * 8),
                timeout_s=5.0,
            ),
            Step(StepAction.CLICK, "不应执行到的提交", selector="#report-submit", meta={"submit": True}),
        ]
        result = execute(
            _make_plan(steps), cfg, dry_run=False, out_dir=str(tmp_path / "run")
        )
        _launch_skip(result)

        assert result.ok is False
        assert result.stopped_at != ""
        assert result.stopped_at == "打不开的页面"
        assert result.submitted is False  # 后续 CLICK 未执行
        assert any("[错误]" in line for line in result.notes)


# ---------------------------------------------------------------------------
# V5 升级:可观测性(telemetry)+ 截图 IO 失败不中断(窄捕获)
# ---------------------------------------------------------------------------
class TestV5Telemetry:
    def test_v5_real_mode_timer_and_submitted_counter(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """真实模式整体计时 executor.run;submitted=True 计 executor.submitted。"""
        telemetry.reset()
        seen: dict[str, object] = {}

        def fake_run(plan, result, out_path, *, auto_confirm, headless):
            seen["called"] = True
            result.ok = True
            result.submitted = True
            return result

        monkeypatch.setattr(executor_playwright, "_run_with_browser", fake_run)
        cfg = Config(data_dir=str(tmp_path))
        plan = _make_plan([Step(StepAction.GOTO, "打开页面", value=MINI_FORM_URI)])
        result = execute(plan, cfg, dry_run=False)

        assert seen["called"] is True
        assert result.submitted is True
        snap = telemetry.snapshot()
        assert snap["timers"]["executor.run"]["count"] == 1
        assert snap["counters"].get("executor.submitted") == 1.0

    def test_v5_submitted_counter_only_when_submitted(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """submitted=False 时绝不计 executor.submitted(仍计时)。"""
        telemetry.reset()

        def fake_run(plan, result, out_path, *, auto_confirm, headless):
            result.ok = True
            result.submitted = False
            return result

        monkeypatch.setattr(executor_playwright, "_run_with_browser", fake_run)
        cfg = Config(data_dir=str(tmp_path))
        plan = _make_plan([Step(StepAction.GOTO, "打开页面", value=MINI_FORM_URI)])
        result = execute(plan, cfg, dry_run=False)

        assert result.submitted is False
        snap = telemetry.snapshot()
        assert snap["timers"]["executor.run"]["count"] == 1
        assert "executor.submitted" not in snap["counters"]

    def test_v5_dry_run_emits_no_executor_metrics(self, tmp_path: pathlib.Path) -> None:
        """干跑纯文本路径不产生任何 executor 指标(零三方依赖红线锁定)。"""
        telemetry.reset()
        cfg = Config(data_dir=str(tmp_path))
        execute(_make_plan(_standard_13_steps()), cfg, dry_run=True)

        snap = telemetry.snapshot()
        assert "executor.run" not in snap["timers"]
        assert not any(name.startswith("executor.") for name in snap["counters"])


class _FakePage:
    """离线假页面:只实现 _perform_step 用到的方法,截图可注入异常。"""

    def __init__(self, screenshot_error: BaseException | None = None) -> None:
        self.screenshot_error = screenshot_error
        self.screenshot_calls: list[dict] = []

    def screenshot(self, path=None, full_page=False) -> None:  # noqa: ANN001
        self.screenshot_calls.append({"path": path, "full_page": full_page})
        if self.screenshot_error is not None:
            raise self.screenshot_error
        pathlib.Path(path).write_bytes(b"fake-png")

    def goto(self, *args, **kwargs) -> None:  # noqa: ANN001
        raise AssertionError("本用例不应驱动导航")


class TestV5ScreenshotTolerance:
    def test_v5_screenshot_io_failure_does_not_abort(self, tmp_path: pathlib.Path) -> None:
        """截图保存失败(OSError)窄捕获:warning + notes 记录,不抛出不中断。"""
        telemetry.reset()
        page = _FakePage(screenshot_error=OSError("disk full"))
        result = ExecutionResult(portal="12377")
        step = Step(StepAction.SCREENSHOT, "截图:填写完成")

        _perform_step(
            step, page=page, out_path=tmp_path, idx=3, result=result, auto_confirm=False
        )

        assert result.screenshots == []  # 失败的截图不入清单
        assert any("截图失败" in line for line in result.notes)
        assert telemetry.snapshot()["counters"].get("executor.screenshot_failures") == 1.0

    def test_v5_screenshot_playwright_error_tolerated(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Playwright Error(截图超时/IO)同样不中断(装了 playwright 才有意义)。"""
        pw_error = pytest.importorskip("playwright").sync_api.Error
        telemetry.reset()
        page = _FakePage(screenshot_error=pw_error("Page.screenshot: Timeout"))
        result = ExecutionResult(portal="12377")
        step = Step(StepAction.SCREENSHOT, "截图:提交结果")

        shot_errors = _screenshot_error_types()
        _perform_step(
            step, page=page, out_path=tmp_path, idx=7, result=result,
            auto_confirm=True, shot_errors=shot_errors,
        )

        assert result.screenshots == []
        assert any("截图失败" in line for line in result.notes)
        assert telemetry.snapshot()["counters"].get("executor.screenshot_failures") == 1.0

    def test_v5_screenshot_unexpected_error_still_propagates(
        self, tmp_path: pathlib.Path
    ) -> None:
        """窄捕获证据:窄捕获范围之外的异常(如 RuntimeError)照常向上抛出。"""
        page = _FakePage(screenshot_error=RuntimeError("代码缺陷,非 IO 失败"))
        result = ExecutionResult(portal="12377")
        step = Step(StepAction.SCREENSHOT, "截图")

        with pytest.raises(RuntimeError, match="代码缺陷"):
            _perform_step(
                step, page=page, out_path=tmp_path, idx=1, result=result,
                auto_confirm=False, shot_errors=_screenshot_error_types(),
            )

    def test_v5_screenshot_error_types_always_include_oserror(self) -> None:
        types = _screenshot_error_types()
        assert OSError in types
        # playwright 可导入时应一并覆盖其 Error 基类
        try:
            from playwright.sync_api import Error as _PwError
        except ImportError:  # pragma: no cover - 环境相关
            pytest.skip("playwright 未安装")
        assert _PwError in types

    def test_v5_screenshot_success_still_recorded(self, tmp_path: pathlib.Path) -> None:
        """正常截图路径不受窄捕获影响:落盘 + 入 screenshots 清单。"""
        page = _FakePage()
        result = ExecutionResult(portal="12377")
        step = Step(StepAction.SCREENSHOT, "截图")

        _perform_step(
            step, page=page, out_path=tmp_path, idx=2, result=result, auto_confirm=False
        )

        assert len(result.screenshots) == 1
        assert pathlib.Path(result.screenshots[0]).is_file()
        assert any("[截图]" in line for line in result.notes)


# ---------------------------------------------------------------------------
# 反脆弱事件驱动等待:wait_until 条件 + 分级兜底链(全部离线,mock page)
# ---------------------------------------------------------------------------
class _WaitMockPage:
    """离线假页面:记录等待类调用序,可注入各条件的失败行为。

    - ``networkidle_errors`` / ``selector_errors``:按次消费的异常队列,
      队列耗尽(或为空)即视为该调用成功 —— 可模拟"首败重试成功";
    - ``content_texts``:轮询 page.content() 依次返回的文本(耗尽后重复末项)。
    """

    def __init__(
        self,
        *,
        networkidle_errors: list[BaseException] | None = None,
        selector_errors: list[BaseException] | None = None,
        content_texts: list[str] | None = None,
    ) -> None:
        self.calls: list[tuple] = []
        self.networkidle_errors = list(networkidle_errors or [])
        self.selector_errors = list(selector_errors or [])
        self.content_texts = list(content_texts or [""])
        self._content_i = 0

    def wait_for_load_state(self, state=None, timeout=None) -> None:  # noqa: ANN001
        self.calls.append(("load_state", state, timeout))
        if self.networkidle_errors:
            raise self.networkidle_errors.pop(0)

    def wait_for_selector(self, selector=None, state=None, timeout=None) -> None:  # noqa: ANN001
        self.calls.append(("selector", selector, state, timeout))
        if self.selector_errors:
            raise self.selector_errors.pop(0)

    def content(self) -> str:
        self.calls.append(("content",))
        text = self.content_texts[min(self._content_i, len(self.content_texts) - 1)]
        self._content_i += 1
        return text

    def wait_for_timeout(self, ms) -> None:  # noqa: ANN001
        self.calls.append(("sleep", ms))


class _NoTouchPage:
    """任何页面方法调用都视为违规:HUMAN_GATE 分支绝不驱动浏览器的证据桩。"""

    def __getattr__(self, name: str):  # noqa: ANN202
        def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError(f"HUMAN_GATE 期间不应调用 page.{name}")

        return forbidden


class TestEventDrivenWait:
    """wait_until 事件驱动等待:退避重试、分级兜底链、审计 notes、兼容锁定。"""

    def _wait_step(
        self,
        *,
        meta: dict | None = None,
        selector: str = "",
        value: str = "",
        timeout_s: float = 10.0,
    ) -> Step:
        return Step(
            StepAction.WAIT, "等待页面加载",
            selector=selector, value=value, timeout_s=timeout_s,
            meta=meta or {},
        )

    def _run(self, step: Step, page, tmp_path: pathlib.Path) -> ExecutionResult:  # noqa: ANN001
        result = ExecutionResult(portal="12377")
        _perform_step(
            step, page=page, out_path=tmp_path, idx=2,
            result=result, auto_confirm=False,
        )
        return result

    def test_legacy_wait_without_wait_until_unchanged(self, tmp_path: pathlib.Path) -> None:
        """无 wait_until → 固定盲睡,调用与 notes 与历史行为逐字一致。"""
        page = _WaitMockPage()
        result = self._run(self._wait_step(value="1.5"), page, tmp_path)
        assert page.calls == [("sleep", 1500)]
        assert result.notes == ["[OK] 第2步 wait 等待页面加载"]

    def test_unknown_wait_until_key_rejected_before_any_page_call(
        self, tmp_path: pathlib.Path
    ) -> None:
        """未知条件键在驱动页面前即被拒绝(错误计划必须暴露,绝不静默降级)。"""
        page = _WaitMockPage()
        step = self._wait_step(meta={"wait_until": {"bogus": 1}})
        with pytest.raises(ValueError, match="未知 wait_until 条件键"):
            self._run(step, page, tmp_path)
        assert page.calls == []

    def test_fallback_chain_networkidle_timeout_selector_then_blind_sleep(
        self, tmp_path: pathlib.Path
    ) -> None:
        """分级兜底链调用序:networkidle 超时 → 退避重试 → selector → 盲睡。"""
        page = _WaitMockPage(
            networkidle_errors=[RuntimeError("netidle t1"), RuntimeError("netidle t2")],
            selector_errors=[RuntimeError("selector t")],
        )
        step = self._wait_step(
            meta={"wait_until": {"requests_idle": 0.05}},
            selector="#report-url", value="0.3", timeout_s=0.05,
        )
        result = self._run(step, page, tmp_path)

        assert [c[0] for c in page.calls] == [
            "load_state", "load_state", "selector", "sleep",
        ]
        # 指数退避:重试超时 = 首次 × 2
        assert page.calls[0] == ("load_state", "networkidle", 50)
        assert page.calls[1] == ("load_state", "networkidle", 100)
        # 降级 selector:step.selector + state="visible" + step.timeout_s
        assert page.calls[2] == ("selector", "#report-url", "visible", 50)
        # 最终兜底:盲睡 value 秒(旧语义延续)
        assert page.calls[3] == ("sleep", 300)
        # 每一级都写入 notes(审计可见)
        assert any("条件 requests_idle 超时" in n for n in result.notes)
        assert any("降级 selector_visible 失败" in n for n in result.notes)
        assert any("兜底盲睡 0.3s" in n for n in result.notes)

    def test_requests_idle_backoff_retry_succeeds(self, tmp_path: pathlib.Path) -> None:
        """首败后指数退避重试一次即成功:不进入兜底链,重试细节入 notes。"""
        page = _WaitMockPage(networkidle_errors=[RuntimeError("netidle t1")])
        step = self._wait_step(meta={"wait_until": {"requests_idle": 0.05}})
        result = self._run(step, page, tmp_path)

        assert [c[0] for c in page.calls] == ["load_state", "load_state"]
        assert page.calls[1][2] == 2 * page.calls[0][2]  # 退避 ×2
        assert any(
            "条件 requests_idle 满足" in n and "退避重试成功" in n for n in result.notes
        )
        assert not any("兜底" in n for n in result.notes)

    def test_selector_visible_condition_satisfied(self, tmp_path: pathlib.Path) -> None:
        page = _WaitMockPage()  # selector_errors 空 → 立即成功
        step = self._wait_step(
            meta={"wait_until": {"selector_visible": "#report-submit"}}, timeout_s=3.0
        )
        result = self._run(step, page, tmp_path)

        assert page.calls == [("selector", "#report-submit", "visible", 3000)]
        assert any("条件 selector_visible 满足" in n for n in result.notes)
        assert not any(c[0] == "sleep" for c in page.calls)

    def test_text_present_polling_until_text_appears(
        self, tmp_path: pathlib.Path
    ) -> None:
        """text_present 轮询 page.content(),间隔退避,文本出现即通过。"""
        page = _WaitMockPage(content_texts=["", "", "举报已受理"])
        step = self._wait_step(
            meta={"wait_until": {"text_present": "举报已受理"}}, timeout_s=2.0
        )
        result = self._run(step, page, tmp_path)

        content_calls = [c for c in page.calls if c[0] == "content"]
        assert len(content_calls) == 3  # 未出现 → 退避 → 再轮询 → 命中
        assert not any(c[0] == "sleep" for c in page.calls)
        assert any("条件 text_present 满足" in n for n in result.notes)

    def test_combined_conditions_any_one_satisfies(self, tmp_path: pathlib.Path) -> None:
        """多条件组合(Any-of):requests_idle 失败后 text_present 命中即通过。"""
        page = _WaitMockPage(
            networkidle_errors=[RuntimeError("t1"), RuntimeError("t2")],
            content_texts=["举报已受理"],
        )
        step = self._wait_step(
            meta={"wait_until": {"requests_idle": 0.02, "text_present": "举报已受理"}},
            timeout_s=1.0,
        )
        result = self._run(step, page, tmp_path)

        assert [c[0] for c in page.calls] == ["load_state", "load_state", "content"]
        assert any("条件 requests_idle 超时" in n for n in result.notes)
        assert any("条件 text_present 满足" in n for n in result.notes)

    def test_declared_selector_visible_failure_skips_same_level_fallback(
        self, tmp_path: pathlib.Path
    ) -> None:
        """selector_visible 已作为声明条件尝试失败 → 不重复同级降级,直接盲睡。"""
        page = _WaitMockPage(
            selector_errors=[RuntimeError("s1"), RuntimeError("s2")]
        )
        step = self._wait_step(
            meta={"wait_until": {"selector_visible": "#report-url"}},
            selector="#report-url", value="0.1", timeout_s=0.05,
        )
        result = self._run(step, page, tmp_path)

        assert [c[0] for c in page.calls] == ["selector", "selector", "sleep"]
        assert any("跳过同级降级" in n for n in result.notes)
        assert any("兜底盲睡 0.1s" in n for n in result.notes)

    def test_no_fallback_selector_sleeps_directly(self, tmp_path: pathlib.Path) -> None:
        """无可用降级选择器:记录后直接进入盲睡兜底。"""
        page = _WaitMockPage(
            networkidle_errors=[RuntimeError("t1"), RuntimeError("t2")]
        )
        step = self._wait_step(
            meta={"wait_until": {"requests_idle": 0.02}}, value="0.1"
        )
        result = self._run(step, page, tmp_path)

        assert [c[0] for c in page.calls] == ["load_state", "load_state", "sleep"]
        assert any("无可用降级选择器" in n for n in result.notes)

    def test_fallback_selector_visible_success(self, tmp_path: pathlib.Path) -> None:
        """networkidle 失败后降级 selector_visible 成功:不盲睡,降级结果入 notes。"""
        page = _WaitMockPage(
            networkidle_errors=[RuntimeError("t1"), RuntimeError("t2")]
        )
        step = self._wait_step(
            meta={"wait_until": {"requests_idle": 0.02}},
            selector="#form-loaded", value="9", timeout_s=0.05,
        )
        result = self._run(step, page, tmp_path)

        assert [c[0] for c in page.calls] == ["load_state", "load_state", "selector"]
        assert any("降级 selector_visible 成功" in n for n in result.notes)
        assert not any(c[0] == "sleep" for c in page.calls)

    def test_dry_run_notes_identical_with_and_without_wait_until(
        self, tmp_path: pathlib.Path
    ) -> None:
        """干跑路径行为不变:wait_until 只存在于 meta,notes 与旧计划逐字一致。"""
        cfg = Config(data_dir=str(tmp_path))
        plain = _make_plan([
            Step(StepAction.GOTO, "打开页面", value=MINI_FORM_URI),
            Step(StepAction.WAIT, "等待页面加载", value="0.5"),
        ])
        eventful = _make_plan([
            Step(StepAction.GOTO, "打开页面", value=MINI_FORM_URI),
            Step(
                StepAction.WAIT, "等待页面加载", value="0.5",
                meta={"wait_until": {"requests_idle": 1}},
            ),
        ])
        r_plain = execute(plain, cfg, dry_run=True, out_dir=str(tmp_path / "a"))
        r_event = execute(eventful, cfg, dry_run=True, out_dir=str(tmp_path / "b"))

        assert r_plain.notes == r_event.notes
        assert r_event.notes[2] == "[DRY] 2. wait 等待页面加载 0.5"
        assert r_event.ok is True
        assert r_event.submitted is False


class TestHumanGateRegression:
    """红线回归(重点断言):HUMAN_GATE 人工门语义不变,且绝不驱动页面。"""

    def test_human_gate_calls_input_and_never_touches_page(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prompts: list[str] = []
        monkeypatch.setattr(
            "builtins.input", lambda prompt="": prompts.append(prompt) or ""
        )
        result = ExecutionResult(portal="12377")
        step = Step(
            StepAction.HUMAN_GATE, "人工核对信息、上传证据包并输入验证码",
            selector="#report-captcha", text="验证码仅限人工输入",
        )

        _perform_step(
            step, page=_NoTouchPage(), out_path=tmp_path, idx=10,
            result=result, auto_confirm=False,
        )

        assert len(prompts) == 1
        assert prompts[0].startswith("【人工确认】")
        assert "人工核对信息" in prompts[0]
        assert any("已人工确认" in n for n in result.notes)

    def test_human_gate_auto_confirm_test_mode_never_inputs(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inputs: list[str] = []
        monkeypatch.setattr(
            "builtins.input", lambda prompt="": inputs.append(prompt) or ""
        )
        result = ExecutionResult(portal="12377")

        _perform_step(
            Step(StepAction.HUMAN_GATE, "人工门"), page=_NoTouchPage(),
            out_path=tmp_path, idx=10, result=result, auto_confirm=True,
        )

        assert inputs == []
        assert any("自动确认(测试模式)" in n for n in result.notes)


# ---------------------------------------------------------------------------
# 自愈选择器候选链(V13):逐候选探测、胜者缓存、审计 notes、v1 兼容
# ---------------------------------------------------------------------------
class _FakeLocator:
    """离线假定位器:count() 决定命中,fill/select_option/click 记录进页面。"""

    def __init__(self, page: "_ChainMockPage", hit: bool, desc: str) -> None:
        self._page = page
        self._hit = hit
        self.desc = desc

    def count(self) -> int:
        return 1 if self._hit else 0

    @property
    def first(self) -> "_FakeLocator":
        return self

    def fill(self, value: str) -> None:
        self._page.actions.append(("locator.fill", self.desc, value))

    def select_option(self, value: str) -> None:
        self._page.actions.append(("locator.select", self.desc, value))

    def click(self) -> None:
        self._page.actions.append(("locator.click", self.desc, None))


class _ChainMockPage:
    """离线假页面:支持全部候选类型探测与 css/locator 双路动作,记录调用序。"""

    def __init__(
        self,
        *,
        css_hits: tuple[str, ...] = (),
        role_hits: tuple[tuple[str, str], ...] = (),
        label_hits: tuple[str, ...] = (),
        placeholder_hits: tuple[str, ...] = (),
        text_hits: tuple[str, ...] = (),
    ) -> None:
        self.css_hits = set(css_hits)
        self.role_hits = set(role_hits)
        self.label_hits = set(label_hits)
        self.placeholder_hits = set(placeholder_hits)
        self.text_hits = set(text_hits)
        self.probes: list[tuple] = []
        self.actions: list[tuple] = []

    def query_selector(self, css: str):
        self.probes.append(("css", css))
        return object() if css in self.css_hits else None

    def get_by_role(self, role: str, name: str | None = None) -> _FakeLocator:
        key = (role, name or "")
        self.probes.append(("role", role, name or ""))
        return _FakeLocator(self, key in self.role_hits, f"role={role}/{name}")

    def get_by_label(self, label: str) -> _FakeLocator:
        self.probes.append(("label", label))
        return _FakeLocator(self, label in self.label_hits, f"label={label}")

    def get_by_placeholder(self, placeholder: str) -> _FakeLocator:
        self.probes.append(("placeholder", placeholder))
        return _FakeLocator(
            self, placeholder in self.placeholder_hits, f"placeholder={placeholder}"
        )

    def get_by_text(self, text: str) -> _FakeLocator:
        self.probes.append(("text", text))
        return _FakeLocator(self, text in self.text_hits, f"text={text}")

    def fill(self, selector: str, value: str) -> None:
        self.actions.append(("page.fill", selector, value))

    def select_option(self, selector: str, value: str) -> None:
        self.actions.append(("page.select", selector, value))

    def click(self, selector: str) -> None:
        self.actions.append(("page.click", selector, None))


class _LabelOnlyPage:
    """只有 label 定位能力的最小假页面:验证 get_by_role 缺失时的链式回退。"""

    def __init__(self, label_hits: tuple[str, ...]) -> None:
        self.label_hits = set(label_hits)
        self.probes: list[tuple] = []
        self.actions: list[tuple] = []

    def query_selector(self, css: str):
        self.probes.append(("css", css))
        return None

    def get_by_label(self, label: str) -> _FakeLocator:
        self.probes.append(("label", label))
        return _FakeLocator(self, label in self.label_hits, f"label={label}")

    def fill(self, selector: str, value: str) -> None:
        self.actions.append(("page.fill", selector, value))

    def select_option(self, selector: str, value: str) -> None:
        self.actions.append(("page.select", selector, value))

    def click(self, selector: str) -> None:
        self.actions.append(("page.click", selector, None))


class TestSelfHealingSelectorChain:
    """候选链解析:回退命中、审计 notes、胜者缓存、全失败中止、v1 直通。"""

    URL_CHAIN = [
        {"css": "#report-url"},
        {"role": "textbox", "name": "举报链接"},
        {"label": "举报链接"},
    ]

    @pytest.fixture(autouse=True)
    def _clear_winner_cache(self) -> None:
        executor_playwright._SELECTOR_WINNERS.clear()

    def _fill(self, chain: list | None, value: str = "http://example.invalid/site") -> Step:
        meta = {"selector_chain": chain} if chain is not None else {}
        return Step(
            StepAction.FILL, "填写举报链接",
            selector="#report-url", value=value, meta=meta,
        )

    def _run(self, step: Step, page, tmp_path: pathlib.Path) -> ExecutionResult:  # noqa: ANN001
        result = ExecutionResult(portal="12377")
        _perform_step(
            step, page=page, out_path=tmp_path, idx=4,
            result=result, auto_confirm=False,
        )
        return result

    def test_v1_no_chain_fast_path_zero_probe(self, tmp_path: pathlib.Path) -> None:
        """v1 兼容锁定:无候选链 → 直接 page.fill(主选择器),零探测、notes 逐字不变。"""
        page = _ChainMockPage()
        result = self._run(self._fill(None), page, tmp_path)
        assert page.probes == []
        assert page.actions == [("page.fill", "#report-url", "http://example.invalid/site")]
        assert result.notes == ["[OK] 第4步 fill 填写举报链接"]

    def test_single_candidate_chain_also_fast_path(self, tmp_path: pathlib.Path) -> None:
        """链长为 1(解析层归一化的 v1 形态)同样直通,不探测。"""
        page = _ChainMockPage(css_hits=("#report-url",))
        result = self._run(self._fill([{"css": "#report-url"}]), page, tmp_path)
        assert page.probes == []
        assert page.actions == [("page.fill", "#report-url", "http://example.invalid/site")]
        assert result.notes == ["[OK] 第4步 fill 填写举报链接"]

    def test_fallback_to_label_records_winner_index_and_type(
        self, tmp_path: pathlib.Path
    ) -> None:
        """主 css 未命中 → label 候选回退:胜者索引 + 候选类型进 notes(审计可见)。"""
        telemetry.reset()
        page = _ChainMockPage(label_hits=("举报链接",))
        result = self._run(self._fill(self.URL_CHAIN), page, tmp_path)

        assert [p[0] for p in page.probes] == ["css", "role", "label"]
        assert page.actions == [("locator.fill", "label=举报链接", "http://example.invalid/site")]
        assert any(
            "[自愈]" in n and "候选#3" in n and "label=举报链接" in n for n in result.notes
        )
        assert any("已回退" in n for n in result.notes)
        assert result.notes[-1] == "[OK] 第4步 fill 填写举报链接"
        assert telemetry.snapshot()["counters"].get("executor.selfheal") == 1.0

    def test_primary_hit_on_multichain_records_note_without_metric(
        self, tmp_path: pathlib.Path
    ) -> None:
        """链长 ≥2 但主候选命中:仍写审计 note(胜者索引 0),不计自愈指标。"""
        telemetry.reset()
        page = _ChainMockPage(css_hits=("#report-url",))
        result = self._run(self._fill(self.URL_CHAIN), page, tmp_path)

        assert page.actions == [("page.fill", "#report-url", "http://example.invalid/site")]
        assert any("候选#1" in n and "css=#report-url" in n for n in result.notes)
        assert "executor.selfheal" not in telemetry.snapshot()["counters"]

    def test_select_via_role_locator(self, tmp_path: pathlib.Path) -> None:
        """SELECT 步骤候选链回退:role+name 命中 → locator.select_option。"""
        page = _ChainMockPage(role_hits=(("combobox", "举报类型"),))
        step = Step(
            StepAction.SELECT, "选择信息类型",
            selector="#report-type", value="色情低俗信息",
            meta={"selector_chain": [
                {"css": "#report-type"},
                {"role": "combobox", "name": "举报类型"},
            ]},
        )
        result = ExecutionResult(portal="12377")
        _perform_step(
            step, page=page, out_path=tmp_path, idx=3,
            result=result, auto_confirm=False,
        )
        assert page.actions == [
            ("locator.select", "role=combobox/举报类型", "色情低俗信息")
        ]
        assert result.notes[-1] == "[OK] 第3步 select 选择信息类型"

    def test_click_via_text_locator(self, tmp_path: pathlib.Path) -> None:
        """CLICK 步骤候选链回退:text 候选命中 → locator.click,提交置位不受影响。"""
        page = _ChainMockPage(text_hits=("提交举报",))
        step = Step(
            StepAction.CLICK, "点击提交",
            selector="#report-submit",
            meta={"submit": True, "selector_chain": [
                {"css": "#report-submit"},
                {"role": "button", "name": "提交举报"},
                {"text": "提交举报"},
            ]},
        )
        result = ExecutionResult(portal="12377")
        _perform_step(
            step, page=page, out_path=tmp_path, idx=15,
            result=result, auto_confirm=False,
        )
        assert page.actions == [("locator.click", "text=提交举报", None)]
        assert result.submitted is True  # meta.submit 语义与候选链正交

    def test_placeholder_candidate_probe(self, tmp_path: pathlib.Path) -> None:
        page = _ChainMockPage(placeholder_hits=("要举报的站点链接",))
        chain = [
            {"css": "#report-url"},
            {"placeholder": "要举报的站点链接"},
        ]
        result = self._run(self._fill(chain), page, tmp_path)
        assert page.actions == [
            ("locator.fill", "placeholder=要举报的站点链接", "http://example.invalid/site")
        ]
        assert any("候选#2" in n and "placeholder=" in n for n in result.notes)

    def test_role_candidate_skipped_when_get_by_role_missing(
        self, tmp_path: pathlib.Path
    ) -> None:
        """页面(旧版驱动/测试桩)不具备 get_by_role:role 候选按未命中跳过,
        链上 label 候选自然兜底。"""
        page = _LabelOnlyPage(label_hits=("举报链接",))
        self._run(self._fill(self.URL_CHAIN), page, tmp_path)
        assert [p[0] for p in page.probes] == ["css", "label"]
        assert page.actions == [
            ("locator.fill", "label=举报链接", "http://example.invalid/site")
        ]

    def test_all_candidates_miss_aborts_with_chinese_error(
        self, tmp_path: pathlib.Path
    ) -> None:
        """全候选失败:抛中文 LookupError(不静默),由上层记 [错误] 并 stopped_at。"""
        page = _ChainMockPage()  # 全部未命中
        with pytest.raises(LookupError, match="候选链全部未命中") as excinfo:
            self._run(self._fill(self.URL_CHAIN), page, tmp_path)
        message = str(excinfo.value)
        assert "css=#report-url" in message and "label=举报链接" in message
        assert page.actions == []  # 未产生任何填写动作

    def test_winner_cache_reused_on_second_resolution(
        self, tmp_path: pathlib.Path
    ) -> None:
        """胜者缓存:第二次解析同字段直接试胜者候选(不再从链首扫起)。"""
        page = _ChainMockPage(label_hits=("举报链接",))
        self._run(self._fill(self.URL_CHAIN), page, tmp_path)
        assert [p[0] for p in page.probes] == ["css", "role", "label"]
        assert executor_playwright._SELECTOR_WINNERS == {"#report-url": 2}

        second = self._run(self._fill(self.URL_CHAIN), page, tmp_path)
        # 第二次只探测了缓存胜者(label),没有重扫 css/role
        assert [p[0] for p in page.probes] == ["css", "role", "label", "label"]
        assert any("缓存胜者" in n and "候选#3" in n for n in second.notes)
        assert page.actions[1] == ("locator.fill", "label=举报链接", "http://example.invalid/site")

    def test_stale_winner_invalidated_and_rescanned(
        self, tmp_path: pathlib.Path
    ) -> None:
        """缓存胜者失效(页面又改版):弹出缓存 → 全链重扫 → 新胜者回写。"""
        page = _ChainMockPage(label_hits=("举报链接",))
        self._run(self._fill(self.URL_CHAIN), page, tmp_path)
        assert executor_playwright._SELECTOR_WINNERS == {"#report-url": 2}

        # 改版:label 消失,placeholder 出现
        page.label_hits = set()
        page.placeholder_hits = {"要举报的站点链接"}
        chain = [
            {"css": "#report-url"},
            {"role": "textbox", "name": "举报链接"},
            {"label": "举报链接"},
            {"placeholder": "要举报的站点链接"},
        ]
        result = self._run(self._fill(chain), page, tmp_path)
        assert any("重新全链探测" in n for n in result.notes)
        assert any("候选#4" in n and "placeholder=" in n for n in result.notes)
        assert executor_playwright._SELECTOR_WINNERS == {"#report-url": 3}

    def test_skippable_empty_value_skips_before_resolution(
        self, tmp_path: pathlib.Path
    ) -> None:
        """空值 skippable 跳过优先于候选链解析:不产生任何探测调用。"""
        page = _ChainMockPage()
        step = Step(
            StepAction.FILL, "填写举报人姓名",
            selector="#report-name", value="", meta={"skippable": True, "selector_chain": [
                {"css": "#report-name"}, {"label": "举报人姓名"},
            ]},
        )
        result = self._run(step, page, tmp_path)
        assert page.probes == [] and page.actions == []
        assert any("[跳过]" in n for n in result.notes)

    def test_dry_run_notes_identical_with_and_without_chains(
        self, tmp_path: pathlib.Path
    ) -> None:
        """干跑路径行为不变:候选链只存在于 meta,不进入干跑 notes。"""
        cfg = Config(data_dir=str(tmp_path))
        plain = _make_plan([
            Step(StepAction.FILL, "填写举报链接", selector="#report-url", value="http://x"),
        ])
        chained = _make_plan([
            Step(
                StepAction.FILL, "填写举报链接", selector="#report-url", value="http://x",
                meta={"selector_chain": self.URL_CHAIN},
            ),
        ])
        r_plain = execute(plain, cfg, dry_run=True, out_dir=str(tmp_path / "a"))
        r_chain = execute(chained, cfg, dry_run=True, out_dir=str(tmp_path / "b"))
        assert r_plain.notes == r_chain.notes
        assert r_chain.notes[-1] == "[DRY] 1. fill 填写举报链接 #report-url"
        assert not any("自愈" in n for n in r_chain.notes)


class TestSelfHealingRedesignDrill:
    """门户改版演练(真浏览器):id 全换的改版页上候选链回退命中、v1 兼容锁定。"""

    def _redesign_chain_steps(self) -> list[Step]:
        """针对改版演练页的链式计划:主 css 指契约 id(改版页已不存在),
        兜底候选用 aria/label 语义(改版页保留)。"""
        return [
            Step(StepAction.GOTO, "打开改版后的本地模拟表单", value=REDESIGN_URI, timeout_s=15.0),
            Step(
                StepAction.SELECT, "选择信息类型", selector="#report-type",
                value="色情低俗信息",
                meta={"selector_chain": [
                    {"css": "#report-type"},
                    {"role": "combobox", "name": "举报类型"},
                    {"label": "举报类型"},
                ]},
            ),
            Step(
                StepAction.FILL, "填写举报链接", selector="#report-url",
                value="http://example.invalid/site",
                meta={"selector_chain": [
                    {"css": "#report-url"},
                    {"role": "textbox", "name": "举报链接"},
                    {"label": "举报链接"},
                ]},
            ),
            Step(
                StepAction.FILL, "填写具体描述", selector="#report-desc", value=_DESC,
                meta={"selector_chain": [
                    {"css": "#report-desc"},
                    {"role": "textbox", "name": "具体描述"},
                    {"label": "具体描述"},
                ]},
            ),
            Step(StepAction.HUMAN_GATE, "人工核对信息并输入验证码"),
            Step(
                StepAction.CLICK, "点击提交按钮", selector="#report-submit",
                meta={"submit": True, "selector_chain": [
                    {"css": "#report-submit"},
                    {"role": "button", "name": "提交举报"},
                    {"text": "提交举报"},
                ]},
            ),
            Step(StepAction.WAIT, "等待提交结果", value="0.2"),
            Step(StepAction.SCREENSHOT, "截图:提交结果"),
        ]

    def test_v2_chain_plan_survives_portal_redesign(
        self, tmp_path: pathlib.Path
    ) -> None:
        """改版演练核心:id 全换后,v2 候选链计划经 aria/label 回退全流程受理。"""
        pytest.importorskip("playwright")
        cfg = Config(data_dir=str(tmp_path))
        result = execute(
            _make_plan(self._redesign_chain_steps()), cfg,
            auto_confirm=True, dry_run=False, headless=True,
            out_dir=str(tmp_path / "run"),
        )
        _launch_skip(result)

        assert result.ok is True, f"notes={result.notes}"
        assert result.submitted is True
        assert result.stopped_at == ""
        # 每个自动步骤都回退命中,且审计 notes 可见(胜者索引 + 候选类型)
        heal_notes = [n for n in result.notes if "[自愈]" in n]
        assert len(heal_notes) == 4, f"4 个自动步骤应各自记录回退:notes={result.notes}"
        assert all("已回退" in n for n in heal_notes)
        assert any("候选#2" in n and "role+name=" in n for n in heal_notes)
        assert result.screenshots, "改版页同样应留下截图佐证"

    def test_v1_single_selector_plan_still_works_on_old_mock_page(
        self, tmp_path: pathlib.Path
    ) -> None:
        """v1 兼容:单值选择器计划在旧版契约 id 页面上照常工作,零自愈 notes。"""
        pytest.importorskip("playwright")
        cfg = Config(data_dir=str(tmp_path))
        steps = [
            Step(StepAction.GOTO, "打开本地模拟表单", value=OLD_MOCK_URI, timeout_s=15.0),
            Step(StepAction.SELECT, "选择信息类型", selector="#report-type", value="色情低俗信息"),
            Step(StepAction.FILL, "填写举报链接", selector="#report-url", value="http://example.invalid/site"),
            Step(StepAction.FILL, "填写具体描述", selector="#report-desc", value=_DESC),
            Step(StepAction.HUMAN_GATE, "人工核对信息并输入验证码"),
            Step(StepAction.CLICK, "点击提交按钮", selector="#report-submit", meta={"submit": True}),
            Step(StepAction.WAIT, "等待提交结果", value="0.2"),
            Step(StepAction.SCREENSHOT, "截图:提交结果"),
        ]
        result = execute(
            _make_plan(steps), cfg,
            auto_confirm=True, dry_run=False, headless=True,
            out_dir=str(tmp_path / "run"),
        )
        _launch_skip(result)

        assert result.ok is True, f"notes={result.notes}"
        assert result.submitted is True
        assert not any("[自愈]" in n for n in result.notes)  # 快速路径零探测零 notes

    def test_v1_plan_breaks_on_redesigned_page_proving_need_for_chains(
        self, tmp_path: pathlib.Path
    ) -> None:
        """对照组:v1 单值选择器在改版页上确实中止(stopped_at)—— 自愈是
        真实需要,不是多余机制;中止不静默([错误] notes 可见)。"""
        pytest.importorskip("playwright")
        cfg = Config(data_dir=str(tmp_path))
        steps = [
            Step(StepAction.GOTO, "打开改版后的本地模拟表单", value=REDESIGN_URI, timeout_s=15.0),
            Step(StepAction.FILL, "填写举报链接", selector="#report-url", value="http://x", timeout_s=2.0),
        ]
        result = execute(
            _make_plan(steps), cfg,
            dry_run=False, headless=True, out_dir=str(tmp_path / "run"),
        )
        _launch_skip(result)

        assert result.ok is False
        assert result.stopped_at == "填写举报链接"
        assert any("[错误]" in n for n in result.notes)
