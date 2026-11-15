# -*- coding: utf-8 -*-
"""V10.24 席位2:capture goto 超时优雅降级(v1 引擎)——fake playwright,零真实浏览器、零网络。

三级降级链验收口径:
① goto 瞬态超时重试 1 次(V5 既有,``capture.transient_retry``);
② 重试用尽 → 整页退回 urllib 退化采样(``capture.degraded_pages``,
   screenshot_path="" 如实缺席,HTML/图片证据/文本线索照常);
③ urllib 也失败 → 记页级采样错误(``capture.errors``):批量路径继续其余页
   (scan 不崩、样本数变少如实呈现),单页路径维持异常上抛(fail-fast);
④ 主线(无超时)零变化:无降级计数、行为与 V10.23 前逐字节一致。
"""
from __future__ import annotations

import pathlib

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence
from netsentinel.crawler import browser as browser_mod


# ---------------------------------------------------------------------------
# fake playwright 桩(参照 test_browser_session 同款形态;goto 按剧本抛超时)
# ---------------------------------------------------------------------------

class _PWTimeout(RuntimeError):
    """playwright 风格超时:真实库的 TimeoutError 并非内置类型,类名对齐判据。"""

    pass


_PWTimeout.__name__ = "TimeoutError"

TIMEOUT_MSG = "Page.goto: Timeout 15000ms exceeded"

SLOW_URL = "http://127.0.0.1:8000/slow"
OK_URL = "http://127.0.0.1:8000/ok"

HTML_OK = (
    '<html><body><h1>裸聊 porn</h1>'
    '<img src="/img/a.png"><img data-src="/img/b.jpg">'
    "</body></html>"
)
EXPECTED_ABS = [
    "http://127.0.0.1:8000/img/a.png",
    "http://127.0.0.1:8000/img/b.jpg",
]


class _FakePage:
    """playwright page 替身:goto 对剧本 URL 每次都抛超时(模拟慢站重试用尽)。"""

    def __init__(self, html_by_url: dict[str, str], events: list, timeout_urls: set[str]):
        self._html_by_url = html_by_url
        self._events = events
        self._timeout_urls = timeout_urls
        self._url = ""

    def goto(self, url, timeout=None):  # noqa: ANN001 - playwright 签名
        self._url = url
        self._events.append(("goto", url))
        if url in self._timeout_urls:
            raise _PWTimeout(TIMEOUT_MSG)

    def wait_for_load_state(self, state, timeout=None):  # noqa: ANN001
        pass

    def screenshot(self, path=None, full_page=False):  # noqa: ANN001
        pathlib.Path(path).write_bytes(b"\x89PNG-fake")
        self._events.append(("shot", path))

    def content(self) -> str:
        return self._html_by_url.get(self._url, "<html></html>")

    def close(self):
        self._events.append(("close-page", self._url))


class _FakeBrowser:
    def __init__(self, html_by_url, events, timeout_urls):
        self._html_by_url = html_by_url
        self._events = events
        self._timeout_urls = timeout_urls

    def new_page(self) -> _FakePage:
        return _FakePage(self._html_by_url, self._events, self._timeout_urls)

    def close(self):
        self._events.append(("close-browser",))


class _FakePW:
    def __init__(self, browser, events):
        self.chromium = _Chromium(browser, events)

    def stop(self):
        pass


class _Chromium:
    def __init__(self, browser, events):
        self._browser = browser
        self._events = events

    def launch(self, headless=True):  # noqa: ANN001
        self._events.append(("launch",))
        return self._browser


class _FakePWContext:
    """sync_playwright() 返回值替身:.start() → _FakePW(单页/会话两路通用)。"""

    def __init__(self, pw: _FakePW):
        self._pw = pw

    def start(self) -> _FakePW:
        return self._pw


def _install_fake(monkeypatch, html_by_url, timeout_urls=frozenset()):
    events: list = []
    browser = _FakeBrowser(html_by_url, events, timeout_urls)
    pw = _FakePW(browser, events)
    monkeypatch.setattr(
        browser_mod, "_import_sync_playwright", lambda: (lambda: _FakePWContext(pw))
    )
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
# ② goto 超时 → urllib 退化 → 采样成功(截图缺席如实)
# ---------------------------------------------------------------------------

def test_single_page_goto_timeout_falls_back_to_urllib(tmp_path, monkeypatch, cfg, tel):
    """单页:两次 goto 均超时(重试用尽)→ urllib 退化成功——HTML/图片证据/线索在,
    截图缺席如实呈现;计 capture.degraded_pages,页本身不算错误。"""
    events, _ = _install_fake(monkeypatch, {SLOW_URL: "<html>不该被浏览器取到</html>"},
                              timeout_urls={SLOW_URL})
    fetch_calls: list[str] = []

    def fake_fetch(url, cfg_):  # noqa: ANN001
        fetch_calls.append(url)
        return 200, HTML_OK, url

    sample = browser_mod.capture_page(
        SLOW_URL, cfg, fetch_page=fake_fetch, download=_fake_download([])
    )

    # 第①②级轨迹:goto 两次(原始+重试)均超时,urllib 退化被调用一次
    assert [e for e in events if e == ("goto", SLOW_URL)] == [("goto", SLOW_URL)] * 2
    assert fetch_calls == [SLOW_URL]
    # 退化采样:无截图(如实缺席),HTML 提取的图片证据与文本线索照常
    assert sample.screenshot_path == ""
    assert pathlib.Path(sample.image_evidences[0].path).name  # 证据对象在
    assert sample.text_hint_hits  # "裸聊"/"porn" 命中
    counters = telemetry.snapshot()["counters"]
    assert counters.get("capture.degraded_pages") == 1.0
    assert counters.get("capture.transient_retry") == 1.0
    assert "capture.errors" not in counters  # 该页最终成功,不算错误
    assert "capture.screenshot" not in counters  # 没有截图计数


def test_batch_goto_timeout_falls_back_to_urllib(tmp_path, monkeypatch, cfg, tel):
    """批量会话路径同款②级:慢页退回 urllib 成功,其余页走浏览器,均产出样本。"""
    events, _ = _install_fake(
        monkeypatch,
        {SLOW_URL: "<html>browser-should-not-read</html>", OK_URL: HTML_OK},
        timeout_urls={SLOW_URL},
    )
    downloads: list = []

    def fake_fetch(url, cfg_):  # noqa: ANN001
        return 200, HTML_OK, url

    samples = browser_mod.capture_pages(
        [SLOW_URL, OK_URL], cfg, fetch_page=fake_fetch, download=_fake_download(downloads)
    )

    assert [s.url for s in samples] == [SLOW_URL, OK_URL]
    assert samples[0].screenshot_path == ""  # 慢页:退化,无截图
    assert samples[1].screenshot_path and pathlib.Path(samples[1].screenshot_path).is_file()
    # 两条下载都有图片链接(慢页来自 urllib HTML,好页来自浏览器 content)
    assert [d[0] for d in downloads] == [EXPECTED_ABS, EXPECTED_ABS]
    counters = telemetry.snapshot()["counters"]
    assert counters.get("capture.degraded_pages") == 1.0
    assert counters.get("capture.transient_retry") == 1.0
    assert counters.get("capture.screenshot") == 1.0  # 仅好页截图
    assert "capture.errors" not in counters


# ---------------------------------------------------------------------------
# ③ 两者都败 → 页级错误记录 + 其余页继续 + scan 不崩
# ---------------------------------------------------------------------------

def test_single_page_both_fail_raises_after_telemetry(tmp_path, monkeypatch, cfg, tel):
    """单页③级:goto 超时且 urllib 也失败 → 原样上抛超时(单页无"其余页",
    维持 fail-fast 传播语义),capture.errors / degraded_pages 如实计数。"""
    events, _ = _install_fake(monkeypatch, {SLOW_URL: "<html>x</html>"},
                              timeout_urls={SLOW_URL})

    def dead_fetch(url, cfg_):  # noqa: ANN001
        return 0, "", url  # urllib 连接失败(status=0)

    with pytest.raises(RuntimeError, match="Page.goto: Timeout 15000ms exceeded"):
        browser_mod.capture_page(SLOW_URL, cfg, fetch_page=dead_fetch, download=_fake_download([]))

    counters = telemetry.snapshot()["counters"]
    assert counters.get("capture.errors") == 1.0
    assert counters.get("capture.degraded_pages") == 1.0
    assert counters.get("capture.transient_retry") == 1.0


def test_batch_both_fail_page_error_scan_survives(tmp_path, monkeypatch, cfg, tel):
    """批量③级(V10.23 实弹事故场景):慢页 goto 超时 + urllib 也挂 →
    该页记页级采样错误,scan 不崩,其余页照常采样,样本数变少如实呈现。"""
    events, _ = _install_fake(
        monkeypatch,
        {SLOW_URL: "<html>browser-should-not-read</html>", OK_URL: HTML_OK},
        timeout_urls={SLOW_URL},
    )
    downloads: list = []

    def fake_fetch(url, cfg_):  # noqa: ANN001
        if url == SLOW_URL:
            return 0, "", url  # 慢站连 urllib 也拿不到
        return 200, HTML_OK, url

    samples = browser_mod.capture_pages(
        [SLOW_URL, OK_URL], cfg, fetch_page=fake_fetch, download=_fake_download(downloads)
    )

    # 慢页被记为页级错误后跳过;好页继续采样——整扫未崩,样本数 1/2 如实
    assert [s.url for s in samples] == [OK_URL]
    assert pathlib.Path(samples[0].screenshot_path).is_file()
    assert samples[0].text_hint_hits
    # 慢页完整走完三级:goto 两次(重试用尽)后 urllib 也失败
    assert [e for e in events if e == ("goto", SLOW_URL)] == [("goto", SLOW_URL)] * 2
    assert ("goto", OK_URL) in events
    assert [d[0] for d in downloads] == [EXPECTED_ABS]  # 仅好页下载
    counters = telemetry.snapshot()["counters"]
    assert counters.get("capture.errors") == 1.0
    assert counters.get("capture.degraded_pages") == 1.0
    assert counters.get("capture.screenshot") == 1.0
    # 会话照常回收(慢页失败不泄漏浏览器会话)
    assert ("close-browser",) in events


# ---------------------------------------------------------------------------
# ④ 主线(无超时)零变化
# ---------------------------------------------------------------------------

def test_batch_mainline_unchanged_no_degradation(tmp_path, monkeypatch, cfg, tel):
    """正常路径:不触发降级计数与重试,行为与 V10.23 前一致(全页截图+线索)。"""
    events, _ = _install_fake(monkeypatch, {OK_URL: HTML_OK})
    samples = browser_mod.capture_pages(
        [OK_URL, OK_URL], cfg, download=_fake_download([])
    )
    assert len(samples) == 2
    assert all(pathlib.Path(s.screenshot_path).is_file() for s in samples)
    counters = telemetry.snapshot()["counters"]
    assert "capture.degraded_pages" not in counters
    assert "capture.transient_retry" not in counters
    assert "capture.errors" not in counters
    assert counters.get("capture.screenshot") == 2.0


def test_batch_non_timeout_error_still_fails_fast(tmp_path, monkeypatch, cfg, tel):
    """非超时类异常不进降级链:原样上抛(fail-fast 语义与 V5 以来不变)。"""
    # goto 抛非超时 RuntimeError:借 _FakePage 剧本外直接改桩行为
    events, browser = _install_fake(monkeypatch, {OK_URL: HTML_OK})

    def boom_goto(url, timeout=None):  # noqa: ANN001
        raise RuntimeError("chromium 崩溃:页面打不开")

    original_new_page = browser.new_page

    def new_page_boom():
        page = original_new_page()
        page.goto = boom_goto
        return page

    browser.new_page = new_page_boom

    with pytest.raises(RuntimeError, match="页面打不开"):
        browser_mod.capture_pages([OK_URL], cfg, download=_fake_download([]))

    counters = telemetry.snapshot()["counters"]
    assert counters.get("capture.errors") == 1.0
    assert "capture.degraded_pages" not in counters
    assert ("close-browser",) in events
