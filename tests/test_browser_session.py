# -*- coding: utf-8 -*-
"""browser.capture_pages 会话复用测试 —— fake playwright,零真实浏览器、零网络。"""
from __future__ import annotations

import pathlib

import pytest

from netsentinel.contracts import Config
from netsentinel.crawler import browser as browser_mod


def _cfg(tmp_path) -> Config:
    return Config(
        evidence_dir=str(tmp_path / "ev"),
        data_dir=str(tmp_path / "data"),
    )


class _FakePage:
    """playwright page 替身:记录 goto/close,截图写盘,content 返回剧本 HTML。"""

    def __init__(self, html_by_url: dict[str, str], events: list, fail_urls: set[str]):
        self._html_by_url = html_by_url
        self._events = events
        self._fail_urls = fail_urls
        self._url = ""

    def goto(self, url, timeout=None):  # noqa: ANN001 - playwright 签名
        self._url = url
        self._events.append(("goto", url))
        if url in self._fail_urls:
            raise RuntimeError("boom:页面打不开")

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
    def __init__(self, html_by_url, events, fail_urls):
        self._html_by_url = html_by_url
        self._events = events
        self._fail_urls = fail_urls
        self.pages: list[_FakePage] = []

    def new_page(self) -> _FakePage:
        page = _FakePage(self._html_by_url, self._events, self._fail_urls)
        self.pages.append(page)
        return page

    def close(self):
        self._events.append(("close-browser",))


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser, events):
        self._browser = browser
        self._events = events

    def launch(self, headless=True):  # noqa: ANN001
        self._events.append(("launch",))
        return self._browser


class _FakePW:
    def __init__(self, browser, events):
        self.chromium = _FakeChromium(browser, events)
        self._events = events

    def stop(self):
        self._events.append(("stop",))


class _FakePWContext:
    """sync_playwright() 返回值替身:.start() → _FakePW。"""

    def __init__(self, pw: _FakePW):
        self._pw = pw

    def start(self) -> _FakePW:
        return self._pw


def _install_fake(monkeypatch, html_by_url, fail_urls=frozenset()):
    events: list = []
    browser = _FakeBrowser(html_by_url, events, fail_urls)
    pw = _FakePW(browser, events)
    monkeypatch.setattr(
        browser_mod, "_import_sync_playwright", lambda: (lambda: _FakePWContext(pw))
    )
    return events, browser


_URLS = ["http://127.0.0.1:8000/a", "http://127.0.0.1:8000/b"]
_HTML = {
    "http://127.0.0.1:8000/a": '<html><img src="/x.png"></html>',
    "http://127.0.0.1:8000/b": "<html>裸聊 porn</html>",
}


def test_session_reuse_single_launch_for_all_pages(tmp_path, monkeypatch):
    """N 页只 launch 一次;逐页 goto/截图;全部 page 与浏览器会话统一回收。"""
    events, browser = _install_fake(monkeypatch, _HTML)
    cfg = _cfg(tmp_path)
    samples = browser_mod.capture_pages(
        list(_URLS), cfg,
        download=lambda urls, src, dest, c: [],
    )
    assert [s.url for s in samples] == _URLS
    assert events.count(("launch",)) == 1  # 核心:一次冷启动
    assert [e for e in events if e[0] == "goto"] == [("goto", u) for u in _URLS]
    # 每页都截图落盘且 page 关闭;浏览器 close 与 playwright stop 各一次
    assert all(pathlib.Path(s.screenshot_path).is_file() for s in samples)
    assert [e for e in events if e[0] == "close-page"] == [
        ("close-page", u) for u in _URLS
    ]
    assert ("close-browser",) in events and ("stop",) in events
    # 文本线索与图片提取口径不变
    assert samples[1].text_hint_hits  # "裸聊"/"porn" 命中


def test_session_fail_fast_cleans_up(tmp_path, monkeypatch):
    """单页失败原样上抛(fail-fast),已开 page/浏览器/会话仍在 finally 回收。"""
    events, browser = _install_fake(
        monkeypatch, _HTML, fail_urls={"http://127.0.0.1:8000/b"}
    )
    cfg = _cfg(tmp_path)
    with pytest.raises(RuntimeError, match="页面打不开"):
        browser_mod.capture_pages(
            list(_URLS), cfg,
            download=lambda urls, src, dest, c: [],
        )
    assert ("close-page", "http://127.0.0.1:8000/a") in events  # 首页已关
    assert ("close-browser",) in events and ("stop",) in events


def test_fallback_without_playwright_matches_per_page(tmp_path, monkeypatch):
    """playwright 缺席:逐页退回 capture_page(退化口径),行为与逐页调用一致。"""
    monkeypatch.setattr(browser_mod, "_import_sync_playwright", lambda: None)

    def fake_fetch(url, cfg_):
        return (200, _HTML.get(url, ""), url)

    cfg = _cfg(tmp_path)
    samples = browser_mod.capture_pages(
        list(_URLS), cfg, fetch_page=fake_fetch,
        download=lambda urls, src, dest, c: [],
    )
    assert len(samples) == 2
    assert all(s.screenshot_path == "" for s in samples)  # 退化:无截图
    assert samples[1].text_hint_hits


def test_local_gate_still_enforced_per_page(tmp_path, monkeypatch):
    """安全红线:allow_network=False 时逐页闸门生效,外网 URL 拒绝并回收会话。"""
    events, _ = _install_fake(monkeypatch, _HTML)
    cfg = _cfg(tmp_path)  # allow_network 默认 False
    with pytest.raises(RuntimeError, match="仅允许访问本机地址"):
        browser_mod.capture_pages(
            ["http://evil.example/x"], cfg,
            download=lambda urls, src, dest, c: [],
        )
    assert ("close-browser",) in events and ("stop",) in events


def test_empty_urls_short_circuit(tmp_path):
    """空列表零副作用:不启动 playwright、不建目录,返回空表。"""
    assert browser_mod.capture_pages([], _cfg(tmp_path)) == []
