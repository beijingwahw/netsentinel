"""创世纪升级波 G5(submit 域)新增测试。

覆盖两处升级对象:
1. 双执行器步骤编排共享(V17 · G5):executor_playwright 与 executor_session
   原先逐字重复的步骤循环提取为共享私有 ``_run_plan_steps``——
   - 静态锁定:两模块持有同一函数对象(零拷贝,防回归为复制粘贴);
   - 运行时锁定:两执行器真实模式均经由共享 helper(离线替身证实);
   - 行为一致:干跑逐 Step notes 对比、真实模式步骤级 notes 全等对比;
2. 频控窗口判断流式聚合(can_submit 不再物化时间戳列表):
   - 旧格式状态文件兼容:手写旧版整文件 JSON / 旧 JSONL(含乱序与裸 ISO 行)
     → 间隔与当日计数两口径正确;
   - 乱序落盘语义锁定:最近一次取全局最大,防未来"只读尾部"捷径放宽频控;
   - can_submit 只读不落盘;长历史文件聚合正确。

安全规则:全部离线——真实模式注入内存替身(sync_playwright / launcher),
绝不启动 chromium、绝不联网、绝不访问 www.12377.cn / www.shdf.gov.cn;
HUMAN_GATE 走干跑(跳过记录)或 auto_confirm=True 测试模式,不做真实交互。
"""
from __future__ import annotations

import json
import pathlib
import sys
import types
from datetime import datetime, timedelta

import pytest

from netsentinel.contracts import (
    Config,
    Portal,
    Step,
    StepAction,
    SubmissionPayload,
    SubmissionPlan,
)
from netsentinel.submit import executor_playwright, executor_session
from netsentinel.submit.executor_session import SessionExecutor
from netsentinel.submit.rate_limit import RateLimiter

FIXTURES_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"
MINI_FORM_URI = (FIXTURES_DIR / "mini_form.html").resolve().as_uri()

_DESC = "该站点存在大量疑似色情图片,经辅助系统初筛并人工核实,附证据包。"


# ---------------------------------------------------------------------------
# 计划构造(与既有执行器测试同口径,不依赖 form_models)
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
        portal=portal, entry_url=MINI_FORM_URI, payload=payload, steps=steps
    )


def _parity_steps() -> list[Step]:
    """13 步标准计划(覆盖全部步骤类型:含选填跳过、人工门、提交、截图)。"""
    return [
        Step(StepAction.GOTO, "打开本地模拟举报页面", value=MINI_FORM_URI),
        Step(StepAction.WAIT, "等待页面加载", value="0.1"),
        Step(StepAction.SELECT, "选择信息类型", selector="#report-type",
             value="色情低俗信息"),
        Step(StepAction.FILL, "填写举报链接", selector="#report-url",
             value="http://example.invalid/site"),
        Step(StepAction.FILL, "填写具体描述", selector="#report-desc", value=_DESC),
        Step(StepAction.FILL, "填写举报人姓名", selector="#report-name",
             value="测试举报人"),
        Step(StepAction.FILL, "填写备用联系方式(选填)", selector="#report-phone2",
             value="", meta={"skippable": True}),
        Step(StepAction.FILL, "填写联系电话", selector="#report-phone",
             value="13800000000"),
        Step(StepAction.SCREENSHOT, "截图:填写完成"),
        Step(StepAction.HUMAN_GATE, "人工核对信息、上传证据包并输入验证码"),
        Step(StepAction.CLICK, "点击提交按钮", selector="#report-submit",
             meta={"submit": True}),
        Step(StepAction.WAIT, "等待提交结果", value="0.2"),
        Step(StepAction.SCREENSHOT, "截图:提交结果"),
    ]


# ---------------------------------------------------------------------------
# 离线替身:记录型页面 + 会话 launcher 链 + 注入式假 playwright 模块
# ---------------------------------------------------------------------------

class _RecordingPage:
    """离线假页面:记录全部被驱动的方法调用;截图真实写盘以走成功路径。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def goto(self, url, timeout=None) -> None:  # noqa: ANN001
        self.calls.append(("goto", url, timeout))

    def wait_for_timeout(self, ms) -> None:  # noqa: ANN001
        self.calls.append(("sleep", ms))

    def select_option(self, selector, value) -> None:  # noqa: ANN001
        self.calls.append(("select", selector, value))

    def fill(self, selector, value) -> None:  # noqa: ANN001
        self.calls.append(("fill", selector, value))

    def click(self, selector) -> None:  # noqa: ANN001
        self.calls.append(("click", selector))

    def screenshot(self, path=None, full_page=False) -> None:  # noqa: ANN001
        self.calls.append(("screenshot", pathlib.Path(path).name, full_page))
        pathlib.Path(path).write_bytes(b"fake-png")

    def close(self) -> None:
        self.calls.append(("close",))


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[_RecordingPage] = []

    def new_page(self) -> _RecordingPage:
        page = _RecordingPage()
        self.pages.append(page)
        return page

    def close(self) -> None:
        pass


class _FakeBrowser:
    """双路径假浏览器:executor_playwright 走 ``new_page``(无 context 层),
    SessionExecutor 走 ``new_context().new_page()``;两条路径的页面都记录在册。"""

    def __init__(self) -> None:
        self.context = _FakeContext()
        self.direct_pages: list[_RecordingPage] = []

    def new_context(self) -> _FakeContext:
        return self.context

    def new_page(self) -> _RecordingPage:
        page = _RecordingPage()
        self.direct_pages.append(page)
        return page

    def close(self) -> None:
        pass


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser) -> None:
        self._browser = browser

    def launch(self, headless=True) -> _FakeBrowser:  # noqa: ANN001
        return self._browser


class _FakePlaywright:
    def __init__(self, browser: _FakeBrowser) -> None:
        self.chromium = _FakeChromium(browser)

    def stop(self) -> None:
        pass


class _FakePwContext:
    """``sync_playwright()`` 的返回:.start() 取得 Playwright 对象。"""

    def __init__(self, pw: _FakePlaywright) -> None:
        self._pw = pw

    def start(self) -> _FakePlaywright:
        return self._pw


class _FakeSyncPlaywright:
    """语义同 playwright.sync_api.sync_playwright:调用返回带 start() 的上下文。"""

    def __init__(self, pw: _FakePlaywright) -> None:
        self._pw = pw

    def __call__(self) -> _FakePwContext:
        return _FakePwContext(self._pw)


class FakeLauncher:
    """SessionExecutor 注入用 launcher(全离线,单 browser/单 context)。"""

    def __init__(self) -> None:
        self.browser = _FakeBrowser()

    def __call__(self) -> _FakeSyncPlaywright:
        return _FakeSyncPlaywright(_FakePlaywright(self.browser))


def _install_fake_playwright(monkeypatch: pytest.MonkeyPatch, launcher: FakeLauncher):
    """把离线假 ``playwright.sync_api`` 注入 sys.modules(monkeypatch 自动还原)。

    使 ``executor_playwright._run_with_browser`` 的惰性导入拿到替身:
    绝不启动真实 chromium、绝不联网(真装了 playwright 的环境同样被覆盖)。
    """
    class _Error(Exception):
        """替身 Playwright Error 基类(截图窄捕获类型获取用)。"""

    fake_api = types.ModuleType("playwright.sync_api")
    fake_api.Error = _Error
    fake_api.sync_playwright = _FakeSyncPlaywright(_FakePlaywright(launcher.browser))
    fake_pkg = types.ModuleType("playwright")
    fake_pkg.sync_api = fake_api
    monkeypatch.setitem(sys.modules, "playwright", fake_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_api)


# ---------------------------------------------------------------------------
# 1. 共享步骤编排:同一 helper、两执行器均经由、行为一致
# ---------------------------------------------------------------------------

class TestSharedStepOrchestration:
    def test_genesis_same_helper_object(self) -> None:
        """共享锁定:两执行器持有同一个 _run_plan_steps 函数对象(零拷贝)。"""
        assert executor_session._run_plan_steps is executor_playwright._run_plan_steps
        assert callable(executor_playwright._run_plan_steps)

    def test_genesis_session_real_mode_routes_through_helper(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """会话执行器真实模式:步骤编排经共享 helper(auto_confirm 原样透传)。"""
        calls: list[tuple] = []
        original = executor_session._run_plan_steps

        def spy(plan, **kwargs):  # noqa: ANN202
            calls.append((plan, kwargs.get("auto_confirm")))
            return original(plan, **kwargs)

        monkeypatch.setattr(executor_session, "_run_plan_steps", spy)
        launcher = FakeLauncher()
        executor = SessionExecutor(Config(data_dir=str(tmp_path)), launcher=launcher)
        plan = _make_plan([
            Step(StepAction.GOTO, "打开页面", value=MINI_FORM_URI),
            Step(StepAction.FILL, "填写举报链接", selector="#report-url", value="x"),
        ])
        result = executor.run(plan, dry_run=False)  # auto_confirm 缺省 False
        executor.close()

        assert result.ok is True
        assert calls == [(plan, False)]

    def test_genesis_playwright_real_mode_routes_through_helper(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """executor_playwright 真实模式(注入假 playwright):同样经共享 helper。"""
        calls: list[tuple] = []
        original = executor_playwright._run_plan_steps

        def spy(plan, **kwargs):  # noqa: ANN202
            calls.append((plan, kwargs.get("auto_confirm")))
            return original(plan, **kwargs)

        monkeypatch.setattr(executor_playwright, "_run_plan_steps", spy)
        launcher = FakeLauncher()
        _install_fake_playwright(monkeypatch, launcher)
        plan = _make_plan([
            Step(StepAction.GOTO, "打开页面", value=MINI_FORM_URI),
            Step(StepAction.FILL, "填写举报链接", selector="#report-url", value="x"),
        ])
        result = executor_playwright.execute(
            plan, Config(data_dir=str(tmp_path)),
            dry_run=False, out_dir=str(tmp_path / "run"),
        )
        assert result.ok is True
        assert calls == [(plan, False)]


class TestExecutorStepParity:
    """共享 helper 后两执行器步骤行为一致(任务点名:dry_run 逐 Step notes 对比)。"""

    def test_genesis_dry_run_notes_step_by_step_identical(
        self, tmp_path: pathlib.Path
    ) -> None:
        """干跑逐 Step notes 对比:两执行器逐条全等(含横幅、跳过与人工门文案)。"""
        plan = _make_plan(_parity_steps())
        r_pw = executor_playwright.execute(
            plan, Config(data_dir=str(tmp_path)), dry_run=True,
            out_dir=str(tmp_path / "a"),
        )
        r_se = SessionExecutor(Config(data_dir=str(tmp_path))).run(plan, dry_run=True)

        assert r_se.notes == r_pw.notes                       # 逐条全等
        assert len(r_se.notes) == len(plan.steps) + 1         # 横幅 + 每 Step 一条
        for i, step in enumerate(plan.steps, start=1):
            assert r_se.notes[i] == r_pw.notes[i]
            assert r_se.notes[i].startswith(f"[DRY] {i}. {step.action.value}")
        # 人工门在干跑下同样只记"跳过",两执行器文案一致(已由全等覆盖)
        gate_idx = [i for i, s in enumerate(plan.steps, start=1)
                    if s.action is StepAction.HUMAN_GATE][0]
        assert "干跑跳过" in r_se.notes[gate_idx]
        assert "干跑跳过" in r_pw.notes[gate_idx]
        assert r_se.ok is True and r_se.submitted is False

    def test_genesis_real_mode_step_notes_identical(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """真实模式步骤级 notes 全等:首行模式声明之外逐字一致(共享编排证据)。"""
        plan = _make_plan(_parity_steps())
        # executor_playwright:注入离线假 playwright(零真浏览器、零外呼)
        pw_launcher = FakeLauncher()
        _install_fake_playwright(monkeypatch, pw_launcher)
        r_pw = executor_playwright.execute(
            plan, Config(data_dir=str(tmp_path)),
            auto_confirm=True, dry_run=False, out_dir=str(tmp_path / "pw"),
        )
        # SessionExecutor:注入同构离线 launcher
        se_launcher = FakeLauncher()
        r_se = SessionExecutor(
            Config(data_dir=str(tmp_path)), launcher=se_launcher
        ).run(plan, auto_confirm=True, dry_run=False)

        assert r_pw.ok is True and r_se.ok is True
        assert r_pw.submitted is True and r_se.submitted is True
        assert r_pw.notes[0].startswith("真实模式")
        assert r_se.notes[0].startswith("会话模式")
        # 首行之外逐字一致:全部 [OK]/[跳过]/[截图]/[人工门]/[提交]/收尾 notes 同源
        assert r_se.notes[1:] == r_pw.notes[1:]
        assert r_se.notes[-1] == "计划执行完毕:共 13 步"
        # 截图文件名一致(路径生成同源;目录不同属会话生命周期差异)
        assert [pathlib.Path(p).name for p in r_se.screenshots] == \
               [pathlib.Path(p).name for p in r_pw.screenshots]
        # 页面驱动行为一致:两执行器各自页面收到相同的步骤调用序;
        # 会话执行器仅多一条收尾 page.close()(会话生命周期差异,非步骤差异)
        se_page_calls = se_launcher.browser.context.pages[0].calls
        pw_page_calls = pw_launcher.browser.direct_pages[0].calls
        assert se_page_calls[:-1] == pw_page_calls
        assert se_page_calls[-1] == ("close",)


# ---------------------------------------------------------------------------
# 2. 频控:旧格式兼容 + 流式窗口聚合(can_submit 不物化列表)
# ---------------------------------------------------------------------------

class TestRateLimitLegacyCompat:
    """旧格式状态文件(手写旧 JSONL / 旧版整文件 JSON)→ can_submit 正确判断。"""

    def test_genesis_legacy_dict_interval_and_daily_window(
        self, tmp_path: pathlib.Path
    ) -> None:
        """手写旧版整文件 JSON:间隔与当日计数两口径均正确(跨日不计入当日)。"""
        p = tmp_path / "rl.json"
        y1 = datetime(2026, 10, 12, 9, 0, 0)
        y2 = datetime(2026, 10, 12, 10, 0, 0)
        d1 = datetime(2026, 10, 13, 9, 0, 0)
        d2 = datetime(2026, 10, 13, 10, 0, 0)
        p.write_text(
            json.dumps(
                {"timestamps": [t.isoformat() for t in (y1, y2, d1, d2)]}, indent=2
            ),
            encoding="utf-8",
        )

        rl = RateLimiter(str(p), min_interval_s=60, max_per_day=3)
        # 最近一次是 d2:距 30s → 间隔拒绝
        ok, reason = rl.can_submit(d2 + timedelta(seconds=30))
        assert ok is False and "间隔" in reason
        # 间隔充足、当日 2 次 < 上限 3 → 放行
        assert rl.can_submit(d2 + timedelta(minutes=30)) == (True, "")

        # 同一文件、上限 2:当日恰 2 次(昨日的 y1/y2 不计入)→ 上限拒绝
        rl_cap = RateLimiter(str(p), min_interval_s=60, max_per_day=2)
        ok, reason = rl_cap.can_submit(d2 + timedelta(minutes=30))
        assert ok is False and ("每日" in reason or "上限" in reason)
        # 跨天清零:次日放行
        assert rl_cap.can_submit(datetime(2026, 10, 14, 9, 0, 0)) == (True, "")

    def test_genesis_handwritten_old_jsonl_out_of_order(
        self, tmp_path: pathlib.Path
    ) -> None:
        """手写旧 JSONL(乱序 + 裸 ISO 行混排):最近一次/当日计数仍按全局口径。

        乱序语义锁定:最新时间戳写在文件首行(非尾部)时,"只读尾部"的捷径
        会把最近一次算小、放宽频控——本用例锁定正确实现必须全局聚合。
        """
        p = tmp_path / "rl.json"
        y = datetime(2026, 10, 12, 9, 0, 0)
        d1 = datetime(2026, 10, 13, 9, 0, 0)
        d2 = datetime(2026, 10, 13, 10, 0, 0)
        p.write_text(
            "\n".join([
                json.dumps(d2.isoformat()),   # 最新记录写在最前(乱序)
                json.dumps(y.isoformat()),    # 昨日(JSONL 行)
                d1.isoformat(),               # 当日(裸 ISO 手写行)
            ]) + "\n",
            encoding="utf-8",
        )
        rl = RateLimiter(str(p), min_interval_s=60, max_per_day=5)
        # 最近一次仍为 d2(虽然它不在文件尾部)
        assert rl.can_submit(d2 + timedelta(seconds=30))[0] is False
        assert rl.can_submit(d2 + timedelta(minutes=2)) == (True, "")
        # 当日计数 = d1 + d2 共 2 次(裸 ISO 行同样计入)
        rl_cap = RateLimiter(str(p), min_interval_s=60, max_per_day=2)
        ok, reason = rl_cap.can_submit(d2 + timedelta(minutes=5))
        assert ok is False and "2" in reason

    def test_genesis_can_submit_never_rewrites_state(
        self, tmp_path: pathlib.Path
    ) -> None:
        """can_submit 纯只读:任意判定结果都不改状态文件字节。"""
        p = tmp_path / "rl.json"
        p.write_text(
            json.dumps(datetime(2026, 10, 13, 9, 0, 0).isoformat()) + "\n",
            encoding="utf-8",
        )
        before = p.read_bytes()
        rl = RateLimiter(str(p), 60, 5)
        rl.can_submit(datetime(2026, 10, 13, 9, 0, 1))   # 拒绝路径
        rl.can_submit(datetime(2026, 10, 14, 9, 0, 0))   # 放行路径
        assert p.read_bytes() == before

    def test_genesis_large_history_streaming_window(
        self, tmp_path: pathlib.Path
    ) -> None:
        """长历史(500 行单日)流式聚合:当日计数不误收旧日,间隔按全局最近一次。"""
        p = tmp_path / "rl.json"
        base = datetime(2026, 9, 1, 8, 0, 0)
        with p.open("w", encoding="utf-8") as fh:
            for i in range(500):
                fh.write(json.dumps((base + timedelta(minutes=i)).isoformat()) + "\n")
        last = base + timedelta(minutes=499)

        rl = RateLimiter(str(p), min_interval_s=60, max_per_day=5)
        assert rl.can_submit(last + timedelta(seconds=1))[0] is False   # 间隔拒绝
        # 9 月 1 日的 500 条不计入 10 月 13 日当日;且早已超过间隔 → 放行
        assert rl.can_submit(datetime(2026, 10, 13, 9, 0, 0)) == (True, "")
