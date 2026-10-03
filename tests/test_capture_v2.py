"""netsentinel.crawler.capture_v2 的测试。

- 纯逻辑:scroll_plan / images_stable / _scroll_until_stable(离线,FakePage 注入);
- 提取复用:注入 fake fetch/download,懒加载 HTML(data-src)场景,
  断言 v2 与 v1(browser.capture_page)行为一致的部分;
- 浏览器:importorskip("playwright"),本地 http.server 起懒加载页
  (首屏 2 张 img,滚动到底时 JS 再插入 3 张),对比 v1 < 4 而 v2 >= 4;
  chromium 启动失败 → skip;
- 退化模式:monkeypatch sys.modules 屏蔽 playwright → screenshot_path == ""。

全程离线:所有外网 URL 只出现在字符串里,绝不发起真实请求。
"""
from __future__ import annotations

import base64
import os
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, PageSample
from netsentinel.crawler import capture_v2 as capture_v2_module
from netsentinel.crawler.capture_v2 import (
    DEFAULT_SCROLL_PAUSE_S,
    DEFAULT_SCROLL_ROUNDS,
    DEFAULT_VIEWPORT_HEIGHT,
    DEFAULT_VIEWPORT_WIDTH,
    MIN_STABLE_ROUNDS,
    SLOW_CAPTURE_V2_WARN_S,
    WHEEL_STEP_PX,
    _scroll_until_stable,
    _shoot_with_playwright_v2,
    capture_page_v2,
    images_stable,
    scroll_plan,
)

BASE_URL = "http://site.example/page.html"

# 懒加载字符串页面:data-src 形态的懒加载图 + 空 src + data: URI 干扰项。
LAZY_HTML = """<html><head><title>lazy page</title></head><body>
<h1>成人电影 免费试看</h1>
<img src="/img/first.png">
<img data-src="/img/lazy1.jpg">
<img data-src="/img/lazy2.jpg">
<img src="">
<img data-src="data:image/png;base64,AAAA">
</body></html>"""

# 与 v1 一致的期望提取结果(绝对化、去 data:、去空值,按出现顺序)。
EXPECTED_ABS = [
    "http://site.example/img/first.png",
    "http://site.example/img/lazy1.jpg",
    "http://site.example/img/lazy2.jpg",
]

# 1x1 透明 PNG,供本地 http 服务充当 <img> 资源(不触网)。
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

# 本地懒加载演示页:首屏 2 张图;onscroll 滚动接近底部时 JS 再插入 3 张。
# 页面主体高 2400px(默认视口 720px),WHEEL_STEP_PX=3000 的第一轮滚动即可触底。
LAZY_LOCAL_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>懒加载演示页</title></head>
<body style="margin:0">
<h1>本地懒加载演示 成人电影</h1>
<div style="height:2400px">
<img src="/img/a.png">
<img src="/img/b.png">
</div>
<div id="late" style="display:none"></div>
<script>
var inserted = false;
window.addEventListener("scroll", function () {
    if (inserted) { return; }
    var nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 200;
    if (nearBottom) {
        inserted = true;
        var holder = document.getElementById("late");
        ["c", "d", "e"].forEach(function (n) {
            var im = document.createElement("img");
            im.src = "/img/late_" + n + ".png";
            holder.appendChild(im);
        });
        holder.style.display = "block";
    }
});
</script>
</body></html>"""


class _LazyHandler(BaseHTTPRequestHandler):
    """只在 127.0.0.1 上服务的极简懒加载页处理器。"""

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定命名
        if self.path.startswith("/img/"):
            body, ctype = PNG_1PX, "image/png"
        else:
            body, ctype = LAZY_LOCAL_HTML.encode("utf-8"), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return  # 静音访问日志,避免污染 pytest 输出


# ---------------------------------------------------------------------------
# 公共夹具与辅助
# ---------------------------------------------------------------------------

@pytest.fixture()
def cfg(tmp_path: pathlib.Path) -> Config:
    return Config(
        evidence_dir=str(tmp_path / "evidence"),
        fetch_timeout_s=15.0,
        fetch_delay_s=0.0,
    )


@pytest.fixture()
def no_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    """屏蔽 playwright 导入(sys.modules 置 None 触发 ImportError),强制退化路径。"""
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)


def _require_browser():
    """复用 A03 模块的用例前置:并行开发中 browser 未就位时跳过。"""
    return pytest.importorskip("netsentinel.crawler.browser")


class _FakeMouse:
    """记录滚轮/移动事件的假鼠标。"""

    def __init__(self) -> None:
        self.wheel_events: list[tuple[int, int]] = []
        self.moves: list[tuple[int, int]] = []

    def move(self, x: int, y: int) -> None:
        self.moves.append((x, y))

    def wheel(self, delta_x: int, delta_y: int) -> None:
        self.wheel_events.append((delta_x, delta_y))


class _FakePage:
    """离线驱动 _scroll_until_stable 的假页面:按序吐出每轮 img 数量。"""

    def __init__(self, counts: list[int]) -> None:
        self._counts = list(counts)
        self._idx = 0
        self.mouse = _FakeMouse()
        self.wait_ms: list[int] = []

    def locator(self, selector: str) -> "_FakePage":
        assert selector == "img"
        return self

    def count(self) -> int:
        return self._counts[min(self._idx, len(self._counts) - 1)]

    def wait_for_timeout(self, ms: int) -> None:
        self.wait_ms.append(ms)
        self._idx += 1  # 每等待一轮,观测值推进一格


class _Recorder:
    """记录注入 fake 的调用参数。"""

    def __init__(self) -> None:
        self.fetch_calls: list[str] = []
        self.download_calls: list[tuple[list[str], str, str]] = []


def _make_fakes(
    html: str = LAZY_HTML,
) -> tuple[_Recorder, Callable[..., object], Callable[..., object]]:
    rec = _Recorder()

    def fake_fetch_page(url: str, cfg: Config) -> tuple[int, str, str]:
        rec.fetch_calls.append(url)
        return 200, html, url

    def fake_download(
        urls: list[str], source_page: str, dest_dir: str, cfg: Config
    ) -> list[ImageEvidence]:
        rec.download_calls.append((list(urls), source_page, dest_dir))
        return [
            ImageEvidence(path=os.path.join(dest_dir, f"img_{i}.png"), url=u, source_page=source_page)
            for i, u in enumerate(urls)
        ]

    return rec, fake_fetch_page, fake_download


def _skip_if_no_chromium() -> None:
    """预检 chromium 二进制是否可用(缺浏览器时按契约 skip,不算失败)。"""
    from playwright.sync_api import sync_playwright

    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    except Exception as exc:  # noqa: BLE001 - 环境缺 chromium 等问题一律跳过
        pytest.skip(f"chromium 未安装:{exc}")
    else:
        browser.close()
        pw.stop()


# ---------------------------------------------------------------------------
# 纯逻辑:scroll_plan
# ---------------------------------------------------------------------------

def test_scroll_plan_defaults_match_contract() -> None:
    assert DEFAULT_SCROLL_ROUNDS == 6
    assert DEFAULT_SCROLL_PAUSE_S == 0.6
    plan = scroll_plan()
    assert len(plan) == 6
    assert all(p == 0.6 for p in plan)
    assert all(isinstance(p, float) for p in plan)


def test_scroll_plan_custom_rounds_and_pause() -> None:
    assert scroll_plan(3, 0.25) == [0.25, 0.25, 0.25]
    # int 暂停秒也会归一为 float
    plan = scroll_plan(2, 1)
    assert plan == [1.0, 1.0]
    assert all(isinstance(p, float) for p in plan)


def test_scroll_plan_zero_rounds_empty() -> None:
    assert scroll_plan(0, 0.6) == []


@pytest.mark.parametrize("rounds, pause_s", [(-1, 0.6), (-3, 0.0), (6, -0.1)])
def test_scroll_plan_rejects_negative_arguments(rounds: int, pause_s: float) -> None:
    with pytest.raises(ValueError, match="不能为负"):
        scroll_plan(rounds, pause_s)


# ---------------------------------------------------------------------------
# 纯逻辑:images_stable
# ---------------------------------------------------------------------------

def test_images_stable_equal_counts_is_stable() -> None:
    # 本轮数量与上一轮相同 → 判稳(连续性由调用方跟踪)
    assert images_stable(3, 3, 5) is True
    assert images_stable(0, 0, 6) is True  # 无图页面同样判稳


def test_images_stable_growth_is_unstable() -> None:
    assert images_stable(2, 5, 4) is False
    assert images_stable(2, 3, 1) is False


def test_images_stable_shrink_counts_as_change() -> None:
    # 数量减少同样算"有变化",不判稳(除非轮次用尽)
    assert images_stable(5, 4, 2) is False


def test_images_stable_rounds_exhausted_forces_stable() -> None:
    # 轮次预算用尽(含负余轮)→ 强制判稳退出
    assert images_stable(3, 4, 0) is True
    assert images_stable(3, 4, -1) is True


# ---------------------------------------------------------------------------
# 纯逻辑:_scroll_until_stable(FakePage 离线驱动)
# ---------------------------------------------------------------------------

def test_scroll_loop_waits_for_two_consecutive_equal_rounds() -> None:
    """懒加载典型曲线 2 → 5 → 5 → 5:变化后需连续 MIN_STABLE_ROUNDS 轮不变才退出。"""
    page = _FakePage([2, 5, 5, 5, 5])
    counts = _scroll_until_stable(page, scroll_plan())
    assert counts == [5, 5, 5]
    assert len(page.mouse.wheel_events) == 3
    assert page.wait_ms == [600, 600, 600]
    assert page.mouse.wheel_events == [(0, WHEEL_STEP_PX)] * 3


def test_scroll_loop_static_page_exits_after_min_rounds() -> None:
    """静态页 2 → 2 → 2:两轮不变即提前退出,不耗尽整个计划。"""
    page = _FakePage([2, 2, 2, 2, 2, 2, 2])
    counts = _scroll_until_stable(page, scroll_plan())
    assert counts == [2, 2]
    assert len(page.mouse.wheel_events) == MIN_STABLE_ROUNDS


def test_scroll_loop_resets_streak_when_count_changes_again() -> None:
    """连续计数中途被变化打断后须重新累计:2,2(1 轮稳)→ 5(打断)→ 5,5(稳)。"""
    page = _FakePage([2, 2, 5, 5, 5])
    counts = _scroll_until_stable(page, scroll_plan())
    assert counts == [2, 5, 5, 5]
    assert len(page.mouse.wheel_events) == 4


def test_scroll_loop_exhausts_plan_when_count_keeps_changing() -> None:
    """无限滚动页:每轮都在变,则滚满整个计划(默认 6 轮)。"""
    page = _FakePage([1, 2, 3, 4, 5, 6, 7])
    counts = _scroll_until_stable(page, scroll_plan())
    assert counts == [2, 3, 4, 5, 6, 7]
    assert len(page.mouse.wheel_events) == 6


def test_scroll_loop_empty_plan_does_nothing() -> None:
    page = _FakePage([4])
    assert _scroll_until_stable(page, scroll_plan(0)) == []
    assert page.mouse.wheel_events == []
    assert page.wait_ms == []


# ---------------------------------------------------------------------------
# 提取复用 + 退化模式(fake fetch/download 注入,屏蔽 playwright)
# ---------------------------------------------------------------------------

def test_degraded_extraction_matches_v1(cfg: Config, no_playwright: None) -> None:
    """退化模式下 v2 与 v1 的图片提取行为一致(data-src 懒加载场景)。"""
    _require_browser()
    from netsentinel.crawler.browser import capture_page

    rec, fake_fetch, fake_download = _make_fakes()
    v2 = capture_page_v2(BASE_URL, cfg, fetch_page=fake_fetch, download=fake_download)
    v1 = capture_page(BASE_URL, cfg, fetch_page=fake_fetch, download=fake_download)

    assert isinstance(v2, PageSample)
    assert v2.url == BASE_URL
    assert v2.screenshot_path == ""  # 退化模式:无浏览器截图
    assert rec.fetch_calls == [BASE_URL, BASE_URL]

    expected = EXPECTED_ABS
    assert [e.url for e in v2.image_evidences] == expected
    assert [e.url for e in v1.image_evidences] == expected  # 与 v1 行为一致
    assert all(e.source_page == BASE_URL for e in v2.image_evidences)
    assert v2.text_hint_hits == ["成人电影"]

    urls, source_page, dest_dir = rec.download_calls[0]
    assert urls == expected
    assert source_page == BASE_URL
    assert dest_dir == os.path.join(cfg.evidence_dir, "site.example_imgs")
    assert os.path.isdir(dest_dir)


def test_degraded_respects_max_images_cap(cfg: Config, no_playwright: None) -> None:
    _require_browser()
    cfg.max_images_per_page = 2
    rec, fake_fetch, fake_download = _make_fakes()
    sample = capture_page_v2(BASE_URL, cfg, fetch_page=fake_fetch, download=fake_download)

    urls, _, _ = rec.download_calls[0]
    assert urls == EXPECTED_ABS[:2]
    assert [e.url for e in sample.image_evidences] == EXPECTED_ABS[:2]


def test_degraded_no_images_skips_download(cfg: Config, no_playwright: None) -> None:
    _require_browser()
    rec, fake_fetch, fake_download = _make_fakes(html="<html><body><p>纯文字页</p></body></html>")
    sample = capture_page_v2(BASE_URL, cfg, fetch_page=fake_fetch, download=fake_download)

    assert rec.download_calls == []
    assert sample.image_evidences == []
    assert sample.screenshot_path == ""
    assert sample.text_hint_hits == []


def test_missing_browser_module_raises_chinese_runtime_error(
    cfg: Config, no_playwright: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """browser 模块不可导入时抛中文 RuntimeError(并行开发容错)。"""
    monkeypatch.setitem(sys.modules, "netsentinel.crawler.browser", None)
    with pytest.raises(RuntimeError, match="browser 模块未就位"):
        capture_page_v2(BASE_URL, cfg)


def test_degraded_missing_fetcher_raises_chinese_runtime_error(
    cfg: Config, no_playwright: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未注入 fake 且 fetcher 不可导入时,退化为复用 v1 的中文 RuntimeError。"""
    monkeypatch.setitem(sys.modules, "netsentinel.crawler.fetcher", None)
    with pytest.raises(RuntimeError, match="fetcher 模块未就位"):
        capture_page_v2(BASE_URL, cfg)


# ---------------------------------------------------------------------------
# playwright 路径(本地 127.0.0.1 服务;缺 chromium 则 skip)
# ---------------------------------------------------------------------------

def test_playwright_guard_blocks_nonlocal_navigation_v2(cfg: Config) -> None:
    """安全红线:allow_network=False 时浏览器只允许导航本机地址(闸门同 v1)。"""
    pytest.importorskip("playwright")
    _require_browser()
    cfg.allow_network = False
    with pytest.raises(RuntimeError, match="仅允许访问本机地址"):
        capture_page_v2("https://example.com/page.html", cfg)


def test_capture_v2_beats_v1_on_lazy_local_page(cfg: Config) -> None:
    """本地懒加载页:v1 只拿到首屏 2 张,v2 滚动后拿到 >= 4 张。"""
    pytest.importorskip("playwright")
    _require_browser()
    from netsentinel.crawler.browser import capture_page

    _skip_if_no_chromium()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _LazyHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{port}/lazy.html"
        # 均使用默认 download = fetcher.download_images(真实下载本机 1px PNG)
        v1_sample = capture_page(url, cfg)
        v2_sample = capture_page_v2(url, cfg)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    v1_urls = [e.url for e in v1_sample.image_evidences]
    v2_urls = [e.url for e in v2_sample.image_evidences]

    # 对比断言:v1 不滚动拿不到懒加载图(< 4),v2 滚动后 >= 4
    assert len(v1_urls) < 4
    assert len(v1_urls) == 2  # 首屏 a.png / b.png
    assert len(v2_urls) >= 4
    assert any("late_c" in u for u in v2_urls)
    assert set(v1_urls) <= set(v2_urls)  # v1 拿到的 v2 全都拿得到

    # 整页截图已落盘,命名同 v1 规则:<safe_host>_<HHMMSS>.png
    assert v2_sample.screenshot_path.endswith(".png")
    assert os.path.isfile(v2_sample.screenshot_path)
    name = os.path.basename(v2_sample.screenshot_path)
    assert name.startswith(f"127.0.0.1_{port}_")
    assert len(name.rsplit("_", 1)[-1].split(".")[0]) == 6  # HHMMSS

    assert v2_sample.url == url
    assert all(e.source_page == url for e in v2_sample.image_evidences)
    assert v2_sample.text_hint_hits == ["成人电影"]


# ---------------------------------------------------------------------------
# V5 升级:瞬态重试 / 滚动轮数 gauge / 常量驱动 / 遥测
# ---------------------------------------------------------------------------


@pytest.fixture()
def tel():
    """隔离全局遥测:用例前置清零、退出还原(避免污染其他用例的指标断言)。"""
    telemetry.reset()
    yield telemetry
    telemetry.reset()


class _FakeV2Page:
    """完整 playwright Page 替身:驱动 _shoot_with_playwright_v2 全流程(离线)。"""

    def __init__(self, browser: "_FakeV2Browser") -> None:
        self._browser = browser
        self._idx = 0
        self.mouse = _FakeMouse()
        self.viewport_size = {"width": 1000, "height": 500}
        self.eval_calls: list[str] = []

    def goto(self, url: str, timeout: float | None = None) -> None:
        self._browser.pages_goto.append(url)
        if self._browser.goto_error is not None:
            exc, self._browser.goto_error = self._browser.goto_error, None
            raise exc

    def wait_for_load_state(self, state: str, timeout: float | None = None) -> None:
        return

    def locator(self, selector: str) -> "_FakeV2Page":
        assert selector == "img"
        return self

    def count(self) -> int:
        counts = self._browser.counts
        return counts[min(self._idx, len(counts) - 1)]

    def wait_for_timeout(self, ms: int) -> None:
        self._idx += 1  # 每等待一轮,观测值推进一格

    def evaluate(self, script: str) -> None:
        self.eval_calls.append(script)

    def screenshot(self, path: str = "", full_page: bool = False) -> None:
        self._browser.screenshot_paths.append(path)
        with open(path, "wb") as fh:
            fh.write(PNG_1PX)

    def content(self) -> str:
        return LAZY_HTML


class _FakeV2Browser:
    """playwright Browser 替身:携带每轮 <img> 数量剧本与一次 goto 异常。"""

    def __init__(self, counts: list[int], goto_error: BaseException | None = None) -> None:
        self.counts = list(counts)
        self.goto_error = goto_error
        self.pages_goto: list[str] = []
        self.screenshot_paths: list[str] = []
        self.pages: list[_FakeV2Page] = []
        self.closed = False

    def new_page(self) -> _FakeV2Page:
        page = _FakeV2Page(self)
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.closed = True


class _FakeV2PlaywrightAPI:
    """sync_playwright 替身:chromium.launch 按剧本返回 _FakeV2Browser 实例。"""

    def __init__(self, launches: list[_FakeV2Browser]) -> None:
        self._launches = list(launches)
        self.launch_calls = 0
        self.stops = 0
        outer = self

        class _Chromium:
            def launch(self, headless: bool = True) -> "_FakeV2Browser":
                outer.launch_calls += 1
                if outer._launches:
                    return outer._launches.pop(0)
                return _FakeV2Browser(counts=[1, 1, 1])

        self.chromium = _Chromium()

    def start(self) -> "_FakeV2PlaywrightAPI":
        return self

    def stop(self) -> None:
        self.stops += 1


def test_v5_transient_goto_timeout_retried_once(cfg: Config, tel: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """v2 浏览器路径:goto 超时类异常自动重试 1 次(复用 browser 的重试助手)。"""
    monkeypatch.setattr(capture_v2_module, "DEFAULT_SCROLL_PAUSE_S", 0.0)  # 测试提速
    dead = _FakeV2Browser(counts=[1, 1, 1], goto_error=TimeoutError("goto timed out"))
    ok = _FakeV2Browser(counts=[2, 2, 2])
    api = _FakeV2PlaywrightAPI([dead, ok])

    shot, html = _shoot_with_playwright_v2(lambda: api, "http://127.0.0.1:8000/lazy.html", cfg)

    assert api.launch_calls == 2 and api.stops == 1
    assert dead.closed is True  # 重试前关闭失败实例
    assert os.path.isfile(shot)
    assert os.path.basename(shot).startswith("127.0.0.1_8000_")  # 命名复用 v1 规则
    assert html == LAZY_HTML
    counters = telemetry.snapshot()["counters"]
    assert counters.get("capture.transient_retry") == 1.0
    assert counters.get("capture_v2.screenshot") == 1.0


def test_v5_capture_v2_scroll_rounds_gauge_and_mouse_center(
    cfg: Config, tel: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """滚动轮数记 gauge;鼠标先移到视口中心(1000x500 → 500,250);回滚顶部。"""
    monkeypatch.setattr(capture_v2_module, "DEFAULT_SCROLL_PAUSE_S", 0.0)
    browser = _FakeV2Browser(counts=[3, 3, 3])  # 首轮即稳:两轮后提前退出
    api = _FakeV2PlaywrightAPI([browser])

    shot, _html = _shoot_with_playwright_v2(lambda: api, "http://127.0.0.1:8000/lazy.html", cfg)

    page = browser.pages[0]
    assert len(page.mouse.wheel_events) == MIN_STABLE_ROUNDS  # 2 轮即稳,提前退出
    assert page.mouse.moves == [(500, 250)]  # 视口中心(回退常量不触发)
    assert page.eval_calls == ["window.scrollTo(0, 0)"]  # 回滚顶部
    assert os.path.isfile(shot)
    snap = telemetry.snapshot()
    assert snap["gauges"].get("capture_v2.scroll_rounds") == 2.0
    assert snap["counters"].get("capture_v2.screenshot") == 1.0


def test_v5_scroll_plan_constants_drive_browser_path(
    cfg: Config, tel: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """常量化:浏览器路径的滚动计划由模块常量 DEFAULT_SCROLL_ROUNDS/PAUSE_S 驱动。"""
    monkeypatch.setattr(capture_v2_module, "DEFAULT_SCROLL_ROUNDS", 3)
    monkeypatch.setattr(capture_v2_module, "DEFAULT_SCROLL_PAUSE_S", 0.0)
    browser = _FakeV2Browser(counts=[1, 2, 3, 4, 5])  # 每轮都在变 → 滚满计划
    api = _FakeV2PlaywrightAPI([browser])

    _shoot_with_playwright_v2(lambda: api, "http://127.0.0.1:8000/lazy.html", cfg)

    page = browser.pages[0]
    assert len(page.mouse.wheel_events) == 3  # 计划轮数 3(而非默认 6)
    assert telemetry.snapshot()["gauges"].get("capture_v2.scroll_rounds") == 3.0
    # 视口回退常量已常量化(1280x720),与实现一致
    assert (DEFAULT_VIEWPORT_WIDTH, DEFAULT_VIEWPORT_HEIGHT) == (1280, 720)
    assert SLOW_CAPTURE_V2_WARN_S == 1.0


def test_v5_capture_v2_degraded_telemetry(cfg: Config, no_playwright: None, tel: object) -> None:
    """退化模式遥测:capture_v2.degraded 计数 + capture_v2.page 计时。"""
    rec, fake_fetch, fake_download = _make_fakes()
    capture_page_v2(BASE_URL, cfg, fetch_page=fake_fetch, download=fake_download)

    snap = telemetry.snapshot()
    assert snap["counters"].get("capture_v2.degraded") == 1.0
    assert "capture_v2.screenshot" not in snap["counters"]
    assert snap["timers"]["capture_v2.page"]["count"] == 1


def test_v5_capture_v2_counts_errors_and_times_failures(
    cfg: Config, no_playwright: None, tel: object
) -> None:
    """异常路径:capture_v2.errors 计数、capture_v2.page 计时(失败同样计时)。"""

    def boom(url: str, cfg_: Config) -> tuple[int, str, str]:
        raise RuntimeError("v2 抓取失败")

    with pytest.raises(RuntimeError, match="v2 抓取失败"):
        capture_page_v2(BASE_URL, cfg, fetch_page=boom)

    snap = telemetry.snapshot()
    assert snap["counters"].get("capture_v2.errors") == 1.0
    assert snap["timers"]["capture_v2.page"]["count"] == 1


def test_v5_capture_v2_real_browser_telemetry(cfg: Config, tel: object) -> None:
    """真实 chromium 端到端:scroll_rounds gauge、screenshot 计数、page 计时。"""
    pytest.importorskip("playwright")
    _require_browser()
    _skip_if_no_chromium()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _LazyHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        rec, fake_fetch, fake_download = _make_fakes()
        url = f"http://127.0.0.1:{port}/lazy.html"
        sample = capture_page_v2(url, cfg, fetch_page=fake_fetch, download=fake_download)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert sample.screenshot_path and os.path.isfile(sample.screenshot_path)
    snap = telemetry.snapshot()
    assert snap["counters"].get("capture_v2.screenshot") == 1.0
    assert snap["gauges"].get("capture_v2.scroll_rounds", 0.0) >= 1.0  # 至少滚动并观测过一轮
    assert snap["timers"]["capture_v2.page"]["count"] == 1
