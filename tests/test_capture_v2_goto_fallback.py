# -*- coding: utf-8 -*-
"""V10.24 席位2:capture goto 超时优雅降级(v2 引擎)——fake playwright,零真实浏览器、零网络。

v2 同款两态验收:
- ② goto 超时(重试用尽)→ urllib 退化采样成功:HTML/图片证据/线索在,
  截图缺席如实(退化无截图、无滚动/懒加载触发),计 ``capture_v2.degraded_pages``;
- ③ 退化也失败 → 记 ``capture_v2.errors`` 页级错误,批量继续其余页,scan 不崩;
- 主线(无超时)零变化:不触发降级计数。
"""
from __future__ import annotations

import pathlib

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence
from netsentinel.crawler import browser as browser_mod
from netsentinel.crawler import capture_v2 as capture_v2_module
from netsentinel.crawler.capture_v2 import capture_page_v2, capture_pages_v2


class _PWTimeout(RuntimeError):
    """playwright 风格超时:真实库的 TimeoutError 并非内置类型,类名对齐判据。"""

    pass


_PWTimeout.__name__ = "TimeoutError"

TIMEOUT_MSG = "Page.goto: Timeout 15000ms exceeded"

SLOW_URL = "http://127.0.0.1:8000/slow"
OK_URL = "http://127.0.0.1:8000/ok"

LAZY_HTML = (
    '<html><body><h1>成人电影</h1>'
    '<img src="/img/first.png"><img data-src="/img/lazy1.jpg">'
    "</body></html>"
)
EXPECTED_ABS = [
    "http://127.0.0.1:8000/img/first.png",
    "http://127.0.0.1:8000/img/lazy1.jpg",
]


class _FakeMouse:
    def __init__(self) -> None:
        self.moves: list[tuple[int, int]] = []

    def move(self, x: int, y: int) -> None:
        self.moves.append((x, y))

    def wheel(self, delta_x: int, delta_y: int) -> None:
        pass


class _FakeV2Page:
    """v2 全流程 page 替身:goto 对剧本 URL 抛超时;滚动/截图/content 可用。"""

    def __init__(self, html_by_url: dict[str, str], events: list, timeout_urls: set[str]):
        self._html_by_url = html_by_url
        self._events = events
        self._timeout_urls = timeout_urls
        self._url = ""
        self._idx = 0
        self.mouse = _FakeMouse()
        self.viewport_size = {"width": 1000, "height": 500}

    def goto(self, url, timeout=None):  # noqa: ANN001 - playwright 签名
        self._url = url
        self._events.append(("goto", url))
        if url in self._timeout_urls:
            raise _PWTimeout(TIMEOUT_MSG)

    def wait_for_load_state(self, state, timeout=None):  # noqa: ANN001
        pass

    def locator(self, selector: str):
        assert selector == "img"
        return self

    def count(self) -> int:
        return 2  # 每轮观测 <img> 数恒定 → 两轮即判稳提前退出

    def wait_for_timeout(self, ms: int) -> None:
        self._idx += 1

    def evaluate(self, script: str) -> None:
        self._events.append(("scroll-top", self._url))

    def screenshot(self, path=None, full_page=False):  # noqa: ANN001
        pathlib.Path(path).write_bytes(b"\x89PNG-fake")
        self._events.append(("shot", path))

    def content(self) -> str:
        return self._html_by_url.get(self._url, "<html></html>")

    def close(self):
        self._events.append(("close-page", self._url))


class _FakeV2Browser:
    def __init__(self, html_by_url, events, timeout_urls):
        self._html_by_url = html_by_url
        self._events = events
        self._timeout_urls = timeout_urls

    def new_page(self) -> _FakeV2Page:
        return _FakeV2Page(self._html_by_url, self._events, self._timeout_urls)

    def close(self):
        self._events.append(("close-browser",))


class _FakeChromium:
    def __init__(self, browser, events):
        self._browser = browser
        self._events = events

    def launch(self, headless=True):  # noqa: ANN001
        self._events.append(("launch",))
        return self._browser


class _FakePW:
    def __init__(self, browser, events):
        self.chromium = _FakeChromium(browser, events)

    def stop(self):
        pass


class _FakePWContext:
    """sync_playwright() 返回值替身:.start() → _FakePW(单页/会话两路通用)。"""

    def __init__(self, pw: _FakePW):
        self._pw = pw

    def start(self) -> _FakePW:
        return self._pw


def _install_fake(monkeypatch, html_by_url, timeout_urls=frozenset()):
    events: list = []
    browser = _FakeV2Browser(html_by_url, events, timeout_urls)
    pw = _FakePW(browser, events)
    monkeypatch.setattr(
        browser_mod, "_import_sync_playwright", lambda: (lambda: _FakePWContext(pw))
    )
    monkeypatch.setattr(capture_v2_module, "DEFAULT_SCROLL_PAUSE_S", 0.0)  # 测试提速
    return events, browser


@pytest.fixture()
def cfg(tmp_path) -> Config:
    return Config(evidence_dir=str(tmp_path / "ev"), data_dir=str(tmp_path / "data"))


@pytest.fixture()
def tel():
    """隔离全局遥测:用例前置清零、退出还原。"""
    telemetry.reset()
    yield telemetry
    telemetry.reset()


def _fake_download(rec):
    def download(urls, source_page, dest_dir, cfg):  # noqa: ANN001
        rec.append((list(urls), source_page, dest_dir))
        return [
            ImageEvidence(path=str(pathlib.Path(dest_dir) / f"img_{i}.png"), url=u,
                          source_page=source_page)
            for i, u in enumerate(urls)
        ]
    return download


# ---------------------------------------------------------------------------
# ② goto 超时 → urllib 退化 → 采样成功(截图/懒加载缺席如实)
# ---------------------------------------------------------------------------

def test_v2_single_page_goto_timeout_falls_back_to_urllib(
    tmp_path, monkeypatch, cfg, tel
):
    """v2 单页:两次 goto 均超时 → urllib 退化成功——退化无截图、无滚动触发,
    HTML/图片证据/线索照常;计 capture_v2.degraded_pages。"""
    events, _ = _install_fake(monkeypatch, {SLOW_URL: "<html>不该被浏览器取到</html>"},
                              timeout_urls={SLOW_URL})
    fetch_calls: list[str] = []

    def fake_fetch(url, cfg_):  # noqa: ANN001
        fetch_calls.append(url)
        return 200, LAZY_HTML, url

    sample = capture_page_v2(
        SLOW_URL, cfg, fetch_page=fake_fetch, download=_fake_download([])
    )

    assert [e for e in events if e == ("goto", SLOW_URL)] == [("goto", SLOW_URL)] * 2
    assert fetch_calls == [SLOW_URL]
    assert sample.screenshot_path == ""  # 退化:截图缺席如实
    assert sample.text_hint_hits == ["成人电影"]  # urllib HTML 的线索照常
    counters = telemetry.snapshot()["counters"]
    assert counters.get("capture_v2.degraded_pages") == 1.0
    assert counters.get("capture.transient_retry") == 1.0  # 第①级:重试了一次
    assert "capture_v2.errors" not in counters
    assert "capture_v2.screenshot" not in counters


def test_v2_batch_goto_timeout_falls_back_to_urllib(tmp_path, monkeypatch, cfg, tel):
    """v2 批量会话路径②级:慢页退化成功,其余页走浏览器滚动采样,均产出样本。"""
    events, _ = _install_fake(
        monkeypatch,
        {SLOW_URL: "<html>browser-should-not-read</html>", OK_URL: LAZY_HTML},
        timeout_urls={SLOW_URL},
    )
    downloads: list = []

    def fake_fetch(url, cfg_):  # noqa: ANN001
        return 200, LAZY_HTML, url

    samples = capture_pages_v2(
        [SLOW_URL, OK_URL], cfg, fetch_page=fake_fetch, download=_fake_download(downloads)
    )

    assert [s.url for s in samples] == [SLOW_URL, OK_URL]
    assert samples[0].screenshot_path == ""  # 慢页:退化,无截图
    assert samples[1].screenshot_path and pathlib.Path(samples[1].screenshot_path).is_file()
    assert [d[0] for d in downloads] == [EXPECTED_ABS, EXPECTED_ABS]
    counters = telemetry.snapshot()["counters"]
    assert counters.get("capture_v2.degraded_pages") == 1.0
    assert counters.get("capture.transient_retry") == 1.0
    assert counters.get("capture_v2.screenshot") == 1.0
    assert "capture_v2.errors" not in counters


# ---------------------------------------------------------------------------
# ③ 两者都败 → 页级错误记录 + 其余页继续 + scan 不崩
# ---------------------------------------------------------------------------

def test_v2_batch_both_fail_page_error_scan_survives(tmp_path, monkeypatch, cfg, tel):
    """v2 批量③级:慢页 goto 超时 + urllib 也挂 → 记 capture_v2.errors 页级错误,
    继续其余页,scan 不崩,样本数变少如实呈现。"""
    events, _ = _install_fake(
        monkeypatch,
        {SLOW_URL: "<html>browser-should-not-read</html>", OK_URL: LAZY_HTML},
        timeout_urls={SLOW_URL},
    )
    downloads: list = []

    def fake_fetch(url, cfg_):  # noqa: ANN001
        if url == SLOW_URL:
            return 0, "", url  # 慢站连 urllib 也拿不到
        return 200, LAZY_HTML, url

    samples = capture_pages_v2(
        [SLOW_URL, OK_URL], cfg, fetch_page=fake_fetch, download=_fake_download(downloads)
    )

    assert [s.url for s in samples] == [OK_URL]  # 慢页跳过,样本数 1/2 如实
    assert pathlib.Path(samples[0].screenshot_path).is_file()
    assert samples[0].text_hint_hits == ["成人电影"]
    assert [e for e in events if e == ("goto", SLOW_URL)] == [("goto", SLOW_URL)] * 2
    assert ("goto", OK_URL) in events
    assert [d[0] for d in downloads] == [EXPECTED_ABS]
    counters = telemetry.snapshot()["counters"]
    assert counters.get("capture_v2.errors") == 1.0
    assert counters.get("capture_v2.degraded_pages") == 1.0
    assert counters.get("capture_v2.screenshot") == 1.0
    assert ("close-browser",) in events  # 会话照常回收


# ---------------------------------------------------------------------------
# 主线(无超时)零变化
# ---------------------------------------------------------------------------

def test_v2_batch_mainline_unchanged_no_degradation(tmp_path, monkeypatch, cfg, tel):
    """v2 正常路径:不触发降级/重试计数,滚动采样与截图行为不变。"""
    events, _ = _install_fake(monkeypatch, {OK_URL: LAZY_HTML})
    samples = capture_pages_v2([OK_URL], cfg, download=_fake_download([]))

    assert len(samples) == 1
    assert pathlib.Path(samples[0].screenshot_path).is_file()
    counters = telemetry.snapshot()["counters"]
    assert "capture_v2.degraded_pages" not in counters
    assert "capture.transient_retry" not in counters
    assert "capture_v2.errors" not in counters
    assert counters.get("capture_v2.screenshot") == 1.0
    assert telemetry.snapshot()["gauges"].get("capture_v2.scroll_rounds") == 2.0
