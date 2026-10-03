"""A129 会话复用执行内核(submit/executor_session)测试。

安全规则:
- 全部离线:真实模式一律注入 FakeLauncher/FakeContext/FakePage,绝不启动真
  chromium、绝不联网、绝不访问 www.12377.cn / www.shdf.gov.cn;
- 干跑用例零三方依赖(委托 executor_playwright 干跑分支,纯标准库);
- HUMAN_GATE 交互路径用 monkeypatch 替换 ``input``,绝不在测试里挂起等待;
- 红线 24(auto_confirm 缺省 False 且无置 True 路径)与红线 31(以操作计数
  断言代差,不依赖墙钟)均有专门用例锁定,以 ``test_v7_`` 前缀标识。
"""
from __future__ import annotations

import inspect
import pathlib

import pytest

from netsentinel import telemetry
from netsentinel.contracts import (
    Config,
    Portal,
    Step,
    StepAction,
    SubmissionPayload,
    SubmissionPlan,
)
from netsentinel.crawler import browser as browser_mod
from netsentinel.submit import executor_session
from netsentinel.submit.executor_session import SessionExecutor

FIXTURES_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"
MINI_FORM_URI = (FIXTURES_DIR / "mini_form.html").resolve().as_uri()

_DESC = "该站点存在大量疑似色情图片,经辅助系统初筛并人工核实,附证据包。"


# ---------------------------------------------------------------------------
# 计划构造助手(不依赖 form_models)
# ---------------------------------------------------------------------------

def _make_plan(steps: list[Step], portal: Portal = Portal.P12377) -> SubmissionPlan:
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


def _goto(label: str = "打开本地模拟举报页面") -> Step:
    return Step(StepAction.GOTO, label, value=MINI_FORM_URI)


def _shot(label: str = "截图:填写完成") -> Step:
    return Step(StepAction.SCREENSHOT, label)


def _gate(label: str = "人工核对信息、上传证据包并输入验证码") -> Step:
    return Step(StepAction.HUMAN_GATE, label)


# ---------------------------------------------------------------------------
# 离线伪造对象:launcher → pw → chromium.launch → browser → context → page
# ---------------------------------------------------------------------------

class FakePage:
    """离线假页面:记录 _perform_step 会触发的全部调用;错误可按方法注入。"""

    def __init__(self) -> None:
        self.goto_calls: list[dict] = []
        self.wait_calls: list[int] = []
        self.select_calls: list[tuple[str, str]] = []
        self.fill_calls: list[tuple[str, str]] = []
        self.click_calls: list[str] = []
        self.screenshot_calls: list[dict] = []
        self.close_calls = 0
        self.goto_error: BaseException | None = None
        self.fill_error: BaseException | None = None
        self.click_error: BaseException | None = None
        self.screenshot_error: BaseException | None = None

    def goto(self, url, timeout=None) -> None:  # noqa: ANN001
        self.goto_calls.append({"url": url, "timeout": timeout})
        if self.goto_error is not None:
            raise self.goto_error

    def wait_for_timeout(self, ms) -> None:  # noqa: ANN001
        self.wait_calls.append(ms)

    def select_option(self, selector, value) -> None:  # noqa: ANN001
        self.select_calls.append((selector, value))

    def fill(self, selector, value) -> None:  # noqa: ANN001
        if self.fill_error is not None:
            raise self.fill_error
        self.fill_calls.append((selector, value))

    def click(self, selector) -> None:  # noqa: ANN001
        if self.click_error is not None:
            raise self.click_error
        self.click_calls.append(selector)

    def screenshot(self, path=None, full_page=False) -> None:  # noqa: ANN001
        self.screenshot_calls.append({"path": path, "full_page": full_page})
        if self.screenshot_error is not None:
            raise self.screenshot_error
        pathlib.Path(path).write_bytes(b"fake-png")

    def close(self) -> None:
        self.close_calls += 1


class FakeContext:
    """离线假 BrowserContext:new_page 逐页记录,可注入逐页配置钩子。"""

    def __init__(self, page_hook=None) -> None:  # noqa: ANN001
        self.pages: list[FakePage] = []
        self.close_calls = 0
        self._page_hook = page_hook

    def new_page(self) -> FakePage:
        page = FakePage()
        if self._page_hook is not None:
            self._page_hook(page, len(self.pages) + 1)
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.close_calls += 1


class FakeBrowser:
    """离线假浏览器:new_context 记录;launch_error 为一次性启动失败。"""

    def __init__(self, page_hook=None) -> None:  # noqa: ANN001
        self.launch_count = 0
        self.launch_error: BaseException | None = None
        self.contexts: list[FakeContext] = []
        self.close_calls = 0
        self.chromium = FakeChromium(self)
        self._page_hook = page_hook

    def new_context(self) -> FakeContext:
        context = FakeContext(self._page_hook)
        self.contexts.append(context)
        return context

    def close(self) -> None:
        self.close_calls += 1


class FakeChromium:
    """离线假 chromium:记录 launch 参数(含失败尝试)。"""

    def __init__(self, browser: FakeBrowser) -> None:
        self._browser = browser
        self.launch_args: list[dict] = []

    def launch(self, headless=True) -> FakeBrowser:  # noqa: ANN001
        self.launch_args.append({"headless": headless})
        err = self._browser.launch_error
        if err is not None:
            self._browser.launch_error = None  # 一次性错误,失败即消费
            raise err
        self._browser.launch_count += 1
        return self._browser


class FakePlaywright:
    """离线假 Playwright 对象(.start() 之后的那层)。"""

    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium
        self.start_calls = 0
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


class _FakePWContext:
    """sync_playwright() 的返回:.start() 取得 Playwright 对象。"""

    def __init__(self, pw: FakePlaywright) -> None:
        self._pw = pw

    def start(self) -> FakePlaywright:
        self._pw.start_calls += 1
        return self._pw


class _FakeSyncPlaywright:
    """语义同 playwright.sync_api.sync_playwright:调用返回带 start() 的上下文。"""

    def __init__(self, pw: FakePlaywright) -> None:
        self._pw = pw

    def __call__(self) -> _FakePWContext:
        return _FakePWContext(self._pw)


class FakeLauncher:
    """注入用 launcher:无参调用返回 pw 对象(同 _import_sync_playwright 返回语义)。"""

    def __init__(self, page_hook=None) -> None:  # noqa: ANN001
        self.browser = FakeBrowser(page_hook)
        self.calls = 0
        self.last_pw: FakePlaywright | None = None

    def __call__(self) -> _FakeSyncPlaywright:
        self.calls += 1
        pw = FakePlaywright(self.browser.chromium)
        self.last_pw = pw
        return _FakeSyncPlaywright(pw)


def _fail_page_2_goto() -> FakeLauncher:
    """第 2 个计划所用页面在 goto 时崩溃(页失败隔离场景)。"""

    def hook(page: FakePage, index: int) -> None:
        if index == 2:
            page.goto_error = RuntimeError("页面导航崩溃")

    return FakeLauncher(page_hook=hook)


# ---------------------------------------------------------------------------
# 干跑分支:与 executor_playwright 完全一致(委托),零 launcher
# ---------------------------------------------------------------------------

class TestDryRunDelegation:
    def test_banner_and_step_notes(self, tmp_path: pathlib.Path) -> None:
        cfg = Config(data_dir=str(tmp_path))
        executor = SessionExecutor(cfg)
        plan = _make_plan([_goto(), Step(StepAction.WAIT, "等待页面加载", value="0.1")])
        result = executor.run(plan, dry_run=True)

        assert result.ok is True
        assert result.submitted is False
        assert result.stopped_at == ""
        assert result.portal == "12377"
        assert result.notes[0] == "DRY-RUN:未驱动浏览器"
        assert len(result.notes) == 3  # 横幅 + 2 条步骤记录
        assert result.notes[1].startswith("[DRY] 1. goto")
        assert result.notes[2].startswith("[DRY] 2. wait")
        assert result.screenshots == []

    def test_default_taken_from_config(self, tmp_path: pathlib.Path) -> None:
        cfg = Config(data_dir=str(tmp_path), dry_run_default=True)
        executor = SessionExecutor(cfg)
        result = executor.run(_make_plan([_goto()]))  # dry_run=None → cfg 缺省
        assert result.notes[0] == "DRY-RUN:未驱动浏览器"
        assert result.ok is True

    def test_human_gate_recorded_as_skipped(self, tmp_path: pathlib.Path) -> None:
        cfg = Config(data_dir=str(tmp_path))
        executor = SessionExecutor(cfg)
        result = executor.run(_make_plan([_gate()]), dry_run=True)
        assert any("干跑跳过" in line and "等待人工确认的环节未执行" in line
                   for line in result.notes)

    def test_zero_launcher_calls(self, tmp_path: pathlib.Path) -> None:
        """干跑零 launcher:3 份干跑计划后不触发任何 launch/launcher 调用。"""
        telemetry.reset()
        launcher = FakeLauncher()
        cfg = Config(data_dir=str(tmp_path), dry_run_default=True)
        executor = SessionExecutor(cfg, launcher=launcher)
        for _ in range(3):
            executor.run(_make_plan([_goto(), _shot()]))
        assert launcher.calls == 0
        assert launcher.browser.chromium.launch_args == []
        assert launcher.browser.launch_count == 0
        assert "executor_session.launch" not in telemetry.snapshot()["counters"]

    def test_out_dirs_counted_per_plan(self, tmp_path: pathlib.Path) -> None:
        """每 plan 独立 out_dir 计数:同一会话下 plan001/plan002 互不覆盖。"""
        cfg = Config(data_dir=str(tmp_path))
        executor = SessionExecutor(cfg)
        executor.run(_make_plan([_goto()]), dry_run=True)
        executor.run(_make_plan([_goto()]), dry_run=True)

        runs_dir = tmp_path / "runs"
        session_dirs = list(runs_dir.iterdir())
        assert len(session_dirs) == 1  # 一个执行器 = 一个会话根目录
        assert sorted(p.name for p in session_dirs[0].iterdir()) == ["plan001", "plan002"]

    def test_launcher_untouched_until_real_plan(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缺省 launcher 惰性:构造与干跑都不触发(仅真实首个计划触发)。"""
        telemetry.reset()
        import_calls = {"n": 0}

        def fake_import():  # noqa: ANN202
            import_calls["n"] += 1
            return _FakeSyncPlaywright(FakePlaywright(FakeBrowser().chromium))

        monkeypatch.setattr(browser_mod, "_import_sync_playwright", fake_import)
        cfg = Config(data_dir=str(tmp_path), dry_run_default=True)
        executor = SessionExecutor(cfg)  # 不注入 launcher → 缺省惰性路径
        assert import_calls["n"] == 0  # 构造不触发
        # 干跑同样不触发缺省 launcher
        result = executor.run(_make_plan([_goto()]))
        assert result.notes[0] == "DRY-RUN:未驱动浏览器"
        assert import_calls["n"] == 0


# ---------------------------------------------------------------------------
# 逐步语义抽查(复用 executor_playwright 的 _perform_step)
# ---------------------------------------------------------------------------

class TestStepSemantics:
    def test_full_plan_ok_submitted_screenshot_saved(
        self, tmp_path: pathlib.Path
    ) -> None:
        launcher = FakeLauncher()
        cfg = Config(data_dir=str(tmp_path))
        executor = SessionExecutor(cfg, launcher=launcher)
        plan = _make_plan([
            _goto(),
            Step(StepAction.FILL, "填写举报链接", selector="#report-url",
                 value="http://example.invalid/site"),
            Step(StepAction.FILL, "填写备用联系方式(选填)", selector="#report-phone2",
                 value="", meta={"skippable": True}),
            _gate(),
            Step(StepAction.CLICK, "点击提交按钮", selector="#report-submit",
                 meta={"submit": True}),
            _shot("截图:提交结果"),
        ])
        result = executor.run(plan, auto_confirm=True, dry_run=False)

        assert result.ok is True
        assert result.submitted is True
        assert result.stopped_at == ""
        assert "会话模式" in result.notes[0]
        page = launcher.browser.contexts[0].pages[0]
        assert page.goto_calls[0]["url"] == MINI_FORM_URI
        assert page.goto_calls[0]["timeout"] == 10000
        assert page.fill_calls == [("#report-url", "http://example.invalid/site")]
        # 提交后的整页截图已落盘并列入清单
        assert len(result.screenshots) == 1
        shot = pathlib.Path(result.screenshots[0])
        assert shot.name == "step06_screenshot.png"
        assert shot.is_file() and shot.stat().st_size > 0
        assert page.screenshot_calls[0]["full_page"] is True
        assert any("自动确认(测试模式)" in line for line in result.notes)
        executor.close()

    def test_fill_skippable_empty_value_skipped(self, tmp_path: pathlib.Path) -> None:
        launcher = FakeLauncher()
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        step = Step(StepAction.FILL, "选填字段", selector="#phone2",
                    value="", meta={"skippable": True})
        result = executor.run(_make_plan([step]), dry_run=False)

        assert result.ok is True
        page = launcher.browser.contexts[0].pages[0]
        assert page.fill_calls == []  # 空且可跳过 → 不调用 fill
        assert any("[跳过]" in line and "可选字段为空" in line
                   for line in result.notes)

    def test_fill_empty_without_skippable_still_fills(
        self, tmp_path: pathlib.Path
    ) -> None:
        launcher = FakeLauncher()
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        step = Step(StepAction.FILL, "必填字段", selector="#desc", value="")
        result = executor.run(_make_plan([step]), dry_run=False)

        assert result.ok is True
        page = launcher.browser.contexts[0].pages[0]
        assert page.fill_calls == [("#desc", "")]

    def test_wait_and_select_recorded(self, tmp_path: pathlib.Path) -> None:
        launcher = FakeLauncher()
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        steps = [
            Step(StepAction.WAIT, "等待页面加载", value="0.2"),
            Step(StepAction.SELECT, "选择信息类型", selector="#report-type",
                 value="色情低俗信息"),
        ]
        result = executor.run(_make_plan(steps), dry_run=False)

        assert result.ok is True
        page = launcher.browser.contexts[0].pages[0]
        assert page.wait_calls == [200]  # int(float("0.2") * 1000)
        assert page.select_calls == [("#report-type", "色情低俗信息")]

    def test_click_submit_selector_sets_submitted(self, tmp_path: pathlib.Path) -> None:
        launcher = FakeLauncher()
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        step = Step(StepAction.CLICK, "点击提交按钮", selector="#report-submit")
        result = executor.run(_make_plan([step]), dry_run=False)

        assert result.submitted is True
        assert any("[提交]" in line and "已点击提交按钮" in line
                   for line in result.notes)

    def test_click_meta_submit_sets_submitted(self, tmp_path: pathlib.Path) -> None:
        launcher = FakeLauncher()
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        step = Step(StepAction.CLICK, "自定义提交", selector="#anything",
                    meta={"submit": True})
        result = executor.run(_make_plan([step]), dry_run=False)
        assert result.submitted is True

    def test_plain_click_not_submitted(self, tmp_path: pathlib.Path) -> None:
        launcher = FakeLauncher()
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        step = Step(StepAction.CLICK, "展开更多信息", selector="#more")
        result = executor.run(_make_plan([step]), dry_run=False)

        assert result.submitted is False
        assert result.ok is True
        assert any("[OK] 第1步 click" in line for line in result.notes)

    def test_screenshot_io_failure_tolerated(self, tmp_path: pathlib.Path) -> None:
        """截图保存失败窄捕获:不入清单、不中断后续提交(OSError 路径)。"""
        telemetry.reset()
        launcher = FakeLauncher(page_hook=lambda p, i: setattr(
            p, "screenshot_error", OSError("disk full")))
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        steps = [_shot(), Step(StepAction.CLICK, "点击提交按钮",
                               selector="#report-submit")]
        result = executor.run(_make_plan(steps), dry_run=False)

        assert result.ok is True  # 截图失败不中断
        assert result.screenshots == []
        assert result.submitted is True  # 后续提交照常执行
        assert any("[截图失败]" in line for line in result.notes)
        assert telemetry.snapshot()["counters"].get("executor.screenshot_failures") == 1.0

    def test_step_failure_marks_failed_and_stops(self, tmp_path: pathlib.Path) -> None:
        telemetry.reset()
        launcher = FakeLauncher(page_hook=lambda p, i: setattr(
            p, "fill_error", RuntimeError("元素不可见")))
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        steps = [
            _goto(),
            Step(StepAction.FILL, "填写举报链接", selector="#report-url", value="x"),
            Step(StepAction.CLICK, "点击提交按钮", selector="#report-submit"),
        ]
        result = executor.run(_make_plan(steps), dry_run=False)

        assert result.ok is False
        assert result.stopped_at == "填写举报链接"
        assert result.submitted is False  # 中止于 fill,后续 CLICK 未执行
        assert any("[错误]" in line and "元素不可见" in line for line in result.notes)
        page = launcher.browser.contexts[0].pages[0]
        assert page.click_calls == []
        assert page.close_calls == 1  # 失败页照常关闭,会话保留

    def test_submitted_not_undone_by_later_step_failure(
        self, tmp_path: pathlib.Path
    ) -> None:
        """提交按钮点击成功后,后续步骤异常不撤销 submitted(同构语义)。"""
        launcher = FakeLauncher(page_hook=lambda p, i: setattr(
            p, "fill_error", RuntimeError("校验失败")))
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        steps = [
            Step(StepAction.CLICK, "点击提交按钮", selector="#report-submit"),
            Step(StepAction.FILL, "填写补充说明", selector="#extra", value="y"),
        ]
        result = executor.run(_make_plan(steps), dry_run=False)

        assert result.ok is False
        assert result.submitted is True

    def test_keyboardinterrupt_in_step_is_cancel(self, tmp_path: pathlib.Path) -> None:
        launcher = FakeLauncher(page_hook=lambda p, i: setattr(
            p, "click_error", KeyboardInterrupt()))
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        steps = [_goto(), Step(StepAction.CLICK, "点击提交按钮", selector="#report-submit")]
        result = executor.run(_make_plan(steps), dry_run=False)

        assert result.ok is False
        assert result.stopped_at == "点击提交按钮"
        assert any("[取消]" in line and "人工取消(Ctrl+C)" in line
                   for line in result.notes)


# ---------------------------------------------------------------------------
# 会话生命周期:一次 launch、逐计划 new_page、页失败隔离、幂等 close
# ---------------------------------------------------------------------------

class TestSessionLifecycle:
    def test_v7_bench_session_reuse_launch_once_new_page_three(
        self, tmp_path: pathlib.Path
    ) -> None:
        """红线 31 bench:3 个 plan 后 launch 恰 1 次、new_page 恰 3 次(操作计数)。"""
        telemetry.reset()
        launcher = FakeLauncher()
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        results = [executor.run(_make_plan([_goto(), _shot()]), dry_run=False)
                   for _ in range(3)]

        assert all(r.ok for r in results)
        # launch 恰 1 次(launcher 调用、chromium.launch 尝试、成功计数三者一致)
        assert launcher.calls == 1
        assert len(launcher.browser.chromium.launch_args) == 1
        assert launcher.browser.chromium.launch_args[0] == {"headless": True}
        assert launcher.browser.launch_count == 1
        assert len(launcher.browser.contexts) == 1
        # new_page 恰 3 次,每页各关一次
        context = launcher.browser.contexts[0]
        assert len(context.pages) == 3
        assert all(page.close_calls == 1 for page in context.pages)
        # 遥测与会话事实一致:成功会话 launch 计数恰 1
        assert telemetry.snapshot()["counters"].get("executor_session.launch") == 1.0
        executor.close()

    def test_page_failure_isolation_second_plan_third_continues(
        self, tmp_path: pathlib.Path
    ) -> None:
        """页失败不影响会话:第 2 个 plan goto 抛错→该条 failed,第 3 个仍执行。"""
        launcher = _fail_page_2_goto()
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        r1 = executor.run(_make_plan([_goto("打开页面")]), dry_run=False)
        r2 = executor.run(_make_plan([_goto("打开页面")]), dry_run=False)
        r3 = executor.run(_make_plan([_goto("打开页面")]), dry_run=False)

        assert r1.ok is True
        assert r2.ok is False
        assert r2.stopped_at == "打开页面"
        assert any("[错误]" in line and "页面导航崩溃" in line for line in r2.notes)
        assert r2.submitted is False
        assert r3.ok is True  # 会话未死,第 3 个计划照常执行
        # 会话事实:仍只 launch 1 次、共 new_page 3 次;崩溃页已关闭但 context 未关
        assert launcher.browser.launch_count == 1
        context = launcher.browser.contexts[0]
        assert len(context.pages) == 3
        assert context.pages[1].close_calls == 1
        assert len(context.pages[1].goto_calls) == 1  # 崩溃页只尝试过一次导航
        assert len(context.pages[2].goto_calls) == 1  # 第 3 个计划照常导航
        assert context.close_calls == 0
        executor.close()
        assert context.close_calls == 1

    def test_launch_failure_result_then_retry_next_plan(
        self, tmp_path: pathlib.Path
    ) -> None:
        """chromium 启动失败:该计划 failed(stopped_at=browser.launch),下个计划重试。"""
        telemetry.reset()
        launcher = FakeLauncher()
        launcher.browser.launch_error = RuntimeError("chromium missing")
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        r1 = executor.run(_make_plan([_goto()]), dry_run=False)

        assert r1.ok is False
        assert r1.stopped_at == "browser.launch"
        assert any("chromium 启动失败" in line for line in r1.notes)
        assert launcher.browser.contexts == []  # 失败不留残余句柄

        r2 = executor.run(_make_plan([_goto()]), dry_run=False)
        assert r2.ok is True  # 会话失败后允许重建
        assert len(launcher.browser.chromium.launch_args) == 2  # 两次尝试
        assert launcher.browser.launch_count == 1
        snap = telemetry.snapshot()["counters"]
        assert snap.get("executor_session.launch") == 1.0  # 仅成功会话计数
        assert snap.get("executor.errors") == 1.0
        executor.close()

    def test_run_after_close_raises(self, tmp_path: pathlib.Path) -> None:
        launcher = FakeLauncher()
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        executor.run(_make_plan([_goto()]), dry_run=False)
        executor.close()
        assert executor.closed is True
        with pytest.raises(RuntimeError, match="已 close"):
            executor.run(_make_plan([_goto()]), dry_run=False)
        with pytest.raises(RuntimeError, match="已 close"):
            executor.run(_make_plan([_goto()]), dry_run=True)  # 干跑同样不可用

    def test_close_idempotent(self, tmp_path: pathlib.Path) -> None:
        launcher = FakeLauncher()
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        executor.run(_make_plan([_goto()]), dry_run=False)
        executor.close()
        executor.close()  # 幂等:重复 close 不再触碰任何句柄

        context = launcher.browser.contexts[0]
        assert context.close_calls == 1
        assert launcher.browser.close_calls == 1
        assert launcher.last_pw is not None
        assert launcher.last_pw.stop_calls == 1
        assert launcher.last_pw.start_calls == 1  # start 恰一次(会话只建立一次)

    def test_with_statement_closes_on_exit(self, tmp_path: pathlib.Path) -> None:
        launcher = FakeLauncher()
        with SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher) as ex:
            result = ex.run(_make_plan([_goto()]), dry_run=False)
        assert result.ok is True
        assert ex.closed is True
        assert launcher.browser.contexts[0].close_calls == 1
        assert launcher.browser.close_calls == 1

    def test_out_dirs_independent_real_mode(self, tmp_path: pathlib.Path) -> None:
        """真实模式逐计划独立目录:两份计划的截图分别落在 plan001/plan002。"""
        launcher = FakeLauncher()
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        r1 = executor.run(_make_plan([_shot()]), dry_run=False)
        r2 = executor.run(_make_plan([_shot()]), dry_run=False)

        assert [pathlib.Path(p).parent.name for p in r1.screenshots + r2.screenshots] \
            == ["plan001", "plan002"]
        assert all(pathlib.Path(p).is_file() for p in r1.screenshots + r2.screenshots)
        executor.close()


# ---------------------------------------------------------------------------
# HUMAN_GATE 与红线 24:每条计划仍是逐条人工门
# ---------------------------------------------------------------------------

class TestHumanGateRedLines:
    def test_auto_confirm_test_mode_note(self, tmp_path: pathlib.Path) -> None:
        """auto_confirm=True(测试模式):只记 notes,不交互。"""
        launcher = FakeLauncher()
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        result = executor.run(_make_plan([_gate()]), auto_confirm=True, dry_run=False)
        assert result.ok is True
        assert any("自动确认(测试模式)" in line for line in result.notes)

    def test_v7_redline_human_gate_real_mode_waits_input(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """红线 24:不传 auto_confirm(缺省 False)时,HUMAN_GATE 必须 input() 等待。"""
        prompts: list[str] = []
        monkeypatch.setattr(
            "builtins.input", lambda prompt="": prompts.append(prompt) or ""
        )
        launcher = FakeLauncher()
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        result = executor.run(_make_plan([_goto(), _gate()]), dry_run=False)

        assert result.ok is True
        assert len(prompts) == 1
        assert prompts[0].startswith("【人工确认】")
        assert "人工核对信息" in prompts[0]
        assert "Ctrl+C 取消" in prompts[0]
        assert any("已人工确认" in line for line in result.notes)

    def test_human_gate_ctrl_c_cancels(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """人工门处 Ctrl+C = 人工主动取消:该计划 failed,后续步骤不执行。"""
        def boom(prompt: str = "") -> str:
            raise KeyboardInterrupt()

        monkeypatch.setattr("builtins.input", boom)
        launcher = FakeLauncher()
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        steps = [_goto(), _gate(), Step(StepAction.CLICK, "点击提交按钮",
                                        selector="#report-submit")]
        result = executor.run(_make_plan(steps), dry_run=False)

        assert result.ok is False
        assert result.stopped_at == "人工核对信息、上传证据包并输入验证码"
        assert result.submitted is False
        assert any("[取消]" in line for line in result.notes)
        page = launcher.browser.contexts[0].pages[0]
        assert page.click_calls == []  # 取消后绝不继续提交

    def test_v7_redline_auto_confirm_default_false_no_true_path(self) -> None:
        """红线 24 静态锁定:run 缺省 False,且源码不存在任何 auto_confirm=True。"""
        signature = inspect.signature(SessionExecutor.run)
        assert signature.parameters["auto_confirm"].default is False
        source = inspect.getsource(executor_session)
        assert "auto_confirm=True" not in source


# ---------------------------------------------------------------------------
# 遥测:launch 恰一次、run 每 plan、submitted/errors 计数
# ---------------------------------------------------------------------------

class TestTelemetry:
    def test_v7_telemetry_launch_once_run_per_plan(
        self, tmp_path: pathlib.Path
    ) -> None:
        telemetry.reset()
        launcher = FakeLauncher()
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        for _ in range(3):
            executor.run(_make_plan([_goto()]), dry_run=False)
        executor.close()

        snap = telemetry.snapshot()
        assert snap["timers"]["executor_session.run"]["count"] == 3  # 每 plan 一次
        assert snap["counters"].get("executor_session.launch") == 1.0  # 恰一次

    def test_run_timer_counts_dry_run_plans(self, tmp_path: pathlib.Path) -> None:
        telemetry.reset()
        executor = SessionExecutor(Config(data_dir=str(tmp_path)))
        executor.run(_make_plan([_goto()]), dry_run=True)
        executor.run(_make_plan([_goto()]), dry_run=True)

        snap = telemetry.snapshot()
        assert snap["timers"]["executor_session.run"]["count"] == 2
        # 干跑零浏览器语义不变:无会话 launch 计数、零 executor.* 计数器
        assert "executor_session.launch" not in snap["counters"]
        assert not any(name.startswith("executor.") for name in snap["counters"])

    def test_submitted_counter_only_when_submitted(
        self, tmp_path: pathlib.Path
    ) -> None:
        telemetry.reset()
        launcher = FakeLauncher()
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        executor.run(_make_plan([_goto()]), dry_run=False)  # 未提交
        snap = telemetry.snapshot()
        assert "executor.submitted" not in snap["counters"]

        executor.run(_make_plan([Step(StepAction.CLICK, "点击提交按钮",
                                      selector="#report-submit")]), dry_run=False)
        assert telemetry.snapshot()["counters"].get("executor.submitted") == 1.0

    def test_step_error_counted(self, tmp_path: pathlib.Path) -> None:
        telemetry.reset()
        launcher = _fail_page_2_goto()
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        executor.run(_make_plan([_goto("打开页面")]), dry_run=False)  # 成功
        executor.run(_make_plan([_goto("打开页面")]), dry_run=False)  # 页失败
        assert telemetry.snapshot()["counters"].get("executor.errors") == 1.0


# ---------------------------------------------------------------------------
# 缺省 launcher:惰性复用 browser._import_sync_playwright
# ---------------------------------------------------------------------------

class TestDefaultLauncher:
    def test_reuses_browser_import_lazily(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import_calls = {"n": 0}
        fake_browser = FakeBrowser()

        def fake_import():  # noqa: ANN202
            import_calls["n"] += 1
            return _FakeSyncPlaywright(FakePlaywright(fake_browser.chromium))

        monkeypatch.setattr(browser_mod, "_import_sync_playwright", fake_import)
        executor = SessionExecutor(Config(data_dir=str(tmp_path)))
        assert import_calls["n"] == 0  # 构造不触发

        result = executor.run(_make_plan([_goto()]), dry_run=False)
        assert import_calls["n"] == 1  # 首个真实计划恰触发一次
        assert result.ok is True
        assert fake_browser.launch_count == 1
        executor.close()

    def test_missing_playwright_fails_gracefully(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(browser_mod, "_import_sync_playwright", lambda: None)
        executor = SessionExecutor(Config(data_dir=str(tmp_path)))
        result = executor.run(_make_plan([_goto()]), dry_run=False)

        assert result.ok is False
        assert result.stopped_at == "browser.launch"
        assert any("请先安装" in line and "playwright" in line for line in result.notes)
