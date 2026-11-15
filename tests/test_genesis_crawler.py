# -*- coding: utf-8 -*-
"""创世纪 G1 升级波测试:crawler 领地(v2 会话复用 / browser_session 原语 / fetcher host 贯穿)。

覆盖口径(全程离线:fake playwright 替身,零真实浏览器、零网络外呼):
- ``capture_v2.capture_pages_v2``:N 页只 launch 一次(fake playwright 计数)、
  goto 瞬态超时只重建 page 不重启浏览器、fail-fast 会话回收、安全闸门逐页生效、
  空列表零副作用、缺 playwright 逐页退化(与逐页 ``capture_page_v2`` 等价);
- ``browser.browser_session``:会话原语生命周期(产出/回收/异常回收/缺依赖产出 None);
- ``capture_page_v2`` 冷启动语义不变(逐次调用各自 launch);
- ``fetcher``:host 贯穿消重后,同一 URL 的 ``_url_host`` 解析次数降为 1,
  安全闸门与 robots 拒绝语义逐项保持。
"""
from __future__ import annotations

import hashlib
import pathlib
import urllib.request
import urllib.robotparser

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence
from netsentinel.crawler import browser as browser_mod
from netsentinel.crawler import capture_v2 as capture_v2_mod
from netsentinel.crawler import fetcher as fetcher_mod
from netsentinel.crawler.browser import browser_session
from netsentinel.crawler.capture_v2 import capture_page_v2, capture_pages_v2
from netsentinel.crawler.fetcher import (
    NetworkDisabledError,
    RobotsDeniedError,
    download_images,
    fetch_page,
)


# ---------------------------------------------------------------------------
# 公共夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def cfg(tmp_path: pathlib.Path) -> Config:
    return Config(
        evidence_dir=str(tmp_path / "ev"),
        fetch_timeout_s=5.0,
        fetch_delay_s=0.0,
    )


@pytest.fixture()
def tel():
    """隔离全局遥测:前置清零、退出还原。"""
    telemetry.reset()
    yield telemetry
    telemetry.reset()


@pytest.fixture()
def fast_scroll(monkeypatch: pytest.MonkeyPatch) -> None:
    """滚动暂停归零(纯提速;不影响判稳逻辑)。"""
    monkeypatch.setattr(capture_v2_mod, "DEFAULT_SCROLL_PAUSE_S", 0.0)


# ---------------------------------------------------------------------------
# fake playwright 替身(驱动 v2 会话复用全流程)
# ---------------------------------------------------------------------------


class _Mouse:
    """记录移动/滚轮事件的假鼠标。"""

    def __init__(self) -> None:
        self.moves: list[tuple[int, int]] = []
        self.wheels: list[tuple[int, int]] = []

    def move(self, x: int, y: int) -> None:
        self.moves.append((x, y))

    def wheel(self, dx: int, dy: int) -> None:
        self.wheels.append((dx, dy))


class _FakeV2Page:
    """playwright Page 替身:goto/滚动/截图/取 HTML 全流程离线可驱动。"""

    def __init__(self, browser: "_FakeV2Browser") -> None:
        self._browser = browser
        self._round = 0
        self._url = ""
        self.mouse = _Mouse()
        self.viewport_size = {"width": 1000, "height": 500}
        self.goto_urls: list[str] = []
        self.eval_calls: list[str] = []
        self.closed = False

    def goto(self, url: str, timeout: float | None = None) -> None:
        self._url = url
        self.goto_urls.append(url)
        if self._browser.first_goto_error is not None:
            exc, self._browser.first_goto_error = (
                self._browser.first_goto_error, None,
            )
            raise exc
        if url in self._browser.fail_urls:
            raise RuntimeError("boom:v2 页面打不开")

    def wait_for_load_state(self, state: str, timeout: float | None = None) -> None:
        return

    def locator(self, selector: str) -> "_FakeV2Page":
        assert selector == "img"
        return self

    def count(self) -> int:
        counts = self._browser.counts
        return counts[min(self._round, len(counts) - 1)]

    def wait_for_timeout(self, ms: int) -> None:
        self._round += 1  # 每等待一轮,观测值推进一格

    def evaluate(self, script: str) -> None:
        self.eval_calls.append(script)

    def screenshot(self, path: str = "", full_page: bool = False) -> None:
        pathlib.Path(path).write_bytes(b"\x89PNG-fake-v2")
        self._browser.shot_paths.append(path)

    def content(self) -> str:
        return self._browser.html_by_url.get(self._url, "<html><body></body></html>")

    def close(self) -> None:
        self.closed = True
        self._browser.closed_page_urls.append(self._url)


class _FakeV2Browser:
    """playwright Browser 替身:携带逐轮 <img> 数量剧本与一次 goto 瞬态异常。"""

    def __init__(
        self,
        html_by_url: dict[str, str],
        *,
        counts: list[int] | None = None,
        first_goto_error: BaseException | None = None,
        fail_urls: frozenset[str] = frozenset(),
    ) -> None:
        self.html_by_url = html_by_url
        self.counts = list(counts or [2, 2, 2])
        self.first_goto_error = first_goto_error
        self.fail_urls = fail_urls
        self.pages: list[_FakeV2Page] = []
        self.shot_paths: list[str] = []
        self.closed_page_urls: list[str] = []
        self.closed = False

    def new_page(self) -> _FakeV2Page:
        page = _FakeV2Page(self)
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.closed = True


class _FakeV2API:
    """sync_playwright 替身:每次 launch 产出一个新 _FakeV2Browser 并计数。"""

    def __init__(self, make_browser) -> None:
        self.launch_calls = 0
        self.stops = 0
        self.browsers: list[_FakeV2Browser] = []
        outer = self

        class _Chromium:
            def launch(self, headless: bool = True) -> _FakeV2Browser:
                outer.launch_calls += 1
                browser = make_browser()
                outer.browsers.append(browser)
                return browser

        self.chromium = _Chromium()

    def start(self) -> "_FakeV2API":
        return self

    def stop(self) -> None:
        self.stops += 1


class _FakePWCtx:
    """sync_playwright() 返回值替身:.start() → _FakeV2API。"""

    def __init__(self, api: _FakeV2API) -> None:
        self._api = api

    def start(self) -> _FakeV2API:
        return self._api


def _install_fake_playwright(monkeypatch: pytest.MonkeyPatch, api: _FakeV2API) -> None:
    monkeypatch.setattr(
        browser_mod, "_import_sync_playwright", lambda: (lambda: _FakePWCtx(api))
    )


_URLS = [
    "http://127.0.0.1:8000/p1",
    "http://127.0.0.1:8000/p2",
    "http://127.0.0.1:8000/p3",
]
_HTML_BY_URL = {
    "http://127.0.0.1:8000/p1": '<html><body><img src="/a.png"><img src="/a.png"></body></html>',
    "http://127.0.0.1:8000/p2": "<html><body>裸聊 porn</body></html>",
    "http://127.0.0.1:8000/p3": "<html><body><img data-src='/b.jpg'></body></html>",
}


def _fake_download(urls, source_page, dest_dir, cfg_):
    """注入的假下载:不触网,按 URL 顺序产出证据。"""
    return [
        ImageEvidence(path=str(pathlib.Path(dest_dir) / f"f{i}.png"), url=u, source_page=source_page)
        for i, u in enumerate(urls)
    ]


# ---------------------------------------------------------------------------
# capture_pages_v2:v2 引擎浏览器会话复用(fake playwright)
# ---------------------------------------------------------------------------


def test_v2_session_reuse_single_launch_for_all_pages(
    cfg: Config, tel: object, fast_scroll: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """核心:N 页只 launch 一次;逐页 new_page/滚动/截图;会话统一回收。"""
    api = _FakeV2API(lambda: _FakeV2Browser(_HTML_BY_URL))
    _install_fake_playwright(monkeypatch, api)

    samples = capture_pages_v2(list(_URLS), cfg, download=_fake_download)

    assert [s.url for s in samples] == _URLS
    assert api.launch_calls == 1  # 一次冷启动,整批复用
    assert api.stops == 1
    (browser,) = api.browsers
    assert browser.closed  # 会话回收
    assert [p.goto_urls for p in browser.pages] == [[u] for u in _URLS]
    assert browser.closed_page_urls == _URLS  # 每页采样完即关
    assert len(browser.shot_paths) == 3 and all(pathlib.Path(p).is_file() for p in browser.shot_paths)

    # 每页采样口径与单页 v2 一致:滚动判稳(2 轮即稳)、鼠标居中、回滚顶部、v2 命名
    for page in browser.pages:
        assert len(page.mouse.wheels) == 2
        assert page.mouse.moves == [(500, 250)]
        assert page.eval_calls == ["window.scrollTo(0, 0)"]
    for sample, shot in zip(samples, browser.shot_paths):
        assert sample.screenshot_path == shot
        assert pathlib.Path(shot).name.startswith("127.0.0.1_8000_v2_")

    # 证据/线索口径与单页 v2 一致
    assert [e.url for e in samples[0].image_evidences] == [
        "http://127.0.0.1:8000/a.png"
    ]
    assert [e.url for e in samples[2].image_evidences] == [
        "http://127.0.0.1:8000/b.jpg"
    ]
    assert samples[1].text_hint_hits  # "裸聊"/"porn" 命中

    snap = telemetry.snapshot()
    assert snap["counters"].get("capture.session_reuse") == 1.0
    assert snap["counters"].get("capture_v2.session_reuse") == 1.0
    assert snap["counters"].get("capture_v2.screenshot") == 3.0
    assert snap["timers"]["capture_v2.page"]["count"] == 3


def test_v2_session_reuse_transient_goto_retry_keeps_browser(
    cfg: Config, tel: object, fast_scroll: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """goto 瞬态超时只重建 page,不重启浏览器(与 v1 会话复用同口径)。"""
    err = TimeoutError("goto timed out")
    api = _FakeV2API(lambda: _FakeV2Browser(_HTML_BY_URL, first_goto_error=err))
    _install_fake_playwright(monkeypatch, api)

    samples = capture_pages_v2(_URLS[:1], cfg, download=_fake_download)

    assert api.launch_calls == 1  # 重试不重启浏览器
    (browser,) = api.browsers
    assert len(browser.pages) == 2  # 重试新建了 page
    assert browser.pages[0].closed  # 失败 page 先关防句柄泄漏
    assert samples[0].screenshot_path  # 重试后采样成功
    assert telemetry.snapshot()["counters"].get("capture.transient_retry") == 1.0


def test_v2_session_fail_fast_cleans_up(
    cfg: Config, tel: object, fast_scroll: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单页非瞬态失败原样上抛(fail-fast),已开 page 与会话仍统一回收。"""
    api = _FakeV2API(
        lambda: _FakeV2Browser(
            _HTML_BY_URL, fail_urls=frozenset({"http://127.0.0.1:8000/p2"})
        )
    )
    _install_fake_playwright(monkeypatch, api)

    with pytest.raises(RuntimeError, match="v2 页面打不开"):
        capture_pages_v2(list(_URLS), cfg, download=_fake_download)

    (browser,) = api.browsers
    assert browser.closed and api.stops == 1
    assert browser.closed_page_urls == [
        "http://127.0.0.1:8000/p1",  # 首页已正常采样并关闭
        "http://127.0.0.1:8000/p2",  # 失败 page 也已关闭
    ]
    assert telemetry.snapshot()["counters"].get("capture_v2.errors") == 1.0


def test_v2_session_gate_blocks_external_url(
    cfg: Config, tel: object, fast_scroll: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """安全红线:allow_network=False 时逐页闸门生效,外网拒绝且会话回收。"""
    api = _FakeV2API(lambda: _FakeV2Browser({}))
    _install_fake_playwright(monkeypatch, api)

    with pytest.raises(RuntimeError, match="仅允许访问本机地址"):
        capture_pages_v2(["http://evil.example/x"], cfg, download=_fake_download)

    (browser,) = api.browsers
    assert browser.closed and api.stops == 1


def test_v2_empty_urls_no_session(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空列表零副作用:不触发 playwright 探测、不建会话,返回空表。"""

    def _forbidden() -> object:
        raise AssertionError("空列表不应探测 playwright")

    monkeypatch.setattr(browser_mod, "_import_sync_playwright", _forbidden)
    assert capture_pages_v2([], cfg) == []


def test_v2_degraded_without_playwright_matches_per_page(
    cfg: Config, tel: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺 playwright:逐页退化,行为与逐页调用 capture_page_v2 完全一致。"""
    monkeypatch.setattr(browser_mod, "_import_sync_playwright", lambda: None)

    fetch_calls: list[str] = []

    def fake_fetch(url: str, cfg_: Config) -> tuple[int, str, str]:
        fetch_calls.append(url)
        return 200, _HTML_BY_URL[url], url

    samples = capture_pages_v2(
        list(_URLS), cfg, fetch_page=fake_fetch, download=_fake_download
    )

    assert fetch_calls == _URLS  # 每页各自走一次退化抓取
    assert [s.url for s in samples] == _URLS
    assert all(s.screenshot_path == "" for s in samples)  # 退化:无截图
    assert [e.url for e in samples[0].image_evidences] == ["http://127.0.0.1:8000/a.png"]
    assert samples[1].text_hint_hits
    snap = telemetry.snapshot()
    assert snap["counters"].get("capture_v2.degraded") == 3.0
    assert snap["counters"].get("capture_v2.session_reuse") is None


def test_v2_cold_start_single_page_still_launches_per_call(
    cfg: Config, tel: object, fast_scroll: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单页口语义不变:两次 capture_page_v2 各自冷启动(各 launch/stop 一次)。"""
    api = _FakeV2API(lambda: _FakeV2Browser(_HTML_BY_URL))
    _install_fake_playwright(monkeypatch, api)

    s1 = capture_page_v2(_URLS[0], cfg, download=_fake_download)
    s2 = capture_page_v2(_URLS[1], cfg, download=_fake_download)

    assert api.launch_calls == 2 and api.stops == 2  # 冷启动不共享会话
    assert all(b.closed for b in api.browsers)
    assert [s.url for s in (s1, s2)] == _URLS[:2]
    assert s2.text_hint_hits and not s1.text_hint_hits


# ---------------------------------------------------------------------------
# browser.browser_session 会话原语
# ---------------------------------------------------------------------------


def test_browser_session_primitive_lifecycle_and_reuse_counter(
    cfg: Config, tel: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """原语:产出 browser、计 capture.session_reuse、退出时 close+stop 各一次。"""
    api = _FakeV2API(lambda: _FakeV2Browser({}))
    _install_fake_playwright(monkeypatch, api)

    with browser_session(cfg) as browser:
        assert browser is api.browsers[0]
        assert telemetry.snapshot()["counters"].get("capture.session_reuse") == 1.0

    assert api.browsers[0].closed and api.stops == 1


def test_browser_session_cleans_up_on_body_exception(
    cfg: Config, tel: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """原语:with 体内抛异常时,会话仍在 finally 中回收后原样上抛。"""
    api = _FakeV2API(lambda: _FakeV2Browser({}))
    _install_fake_playwright(monkeypatch, api)

    with pytest.raises(ValueError, match="boom"):
        with browser_session(cfg):
            raise ValueError("boom")

    assert api.browsers[0].closed and api.stops == 1


def test_browser_session_yields_none_without_playwright(
    cfg: Config, tel: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """原语:缺 playwright 产出 None(不抛出),由调用方自行走退化路径。"""
    monkeypatch.setattr(browser_mod, "_import_sync_playwright", lambda: None)
    with browser_session(cfg) as browser:
        assert browser is None
    assert telemetry.snapshot()["counters"].get("capture.session_reuse") is None


# ---------------------------------------------------------------------------
# fetcher:host 贯穿消重(解析计数)+ 闸门/robots 语义保持
# ---------------------------------------------------------------------------


class _FakeResp:
    """fetcher 的假 HTTP 响应:上下文管理器 + headers.get + read/geturl。"""

    def __init__(self, data: bytes, url: str) -> None:
        self._data = data
        self._url = url
        self.status = 200
        self.headers = {"Content-Type": "image/png"}

    def read(self, n: int = -1) -> bytes:
        return self._data[:n] if n is not None and n >= 0 else self._data

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


_PNG = b"\x89PNG-fake-bytes"


@pytest.fixture()
def offline_urlopen(monkeypatch: pytest.MonkeyPatch) -> None:
    """打桩 _urlopen_with_retry:返回本地假响应,零外呼、零退避等待。"""
    monkeypatch.setattr(
        fetcher_mod, "_urlopen_with_retry",
        lambda request, timeout: _FakeResp(_PNG, request.full_url),
    )


@pytest.fixture()
def clean_robots_cache():
    """隔离模块级 robots 缓存(进入/退出各清一次)。"""
    fetcher_mod._ROBOTS_CACHE.clear()
    yield
    fetcher_mod._ROBOTS_CACHE.clear()


def test_download_images_parses_host_once_per_unique_url(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, offline_urlopen: None
) -> None:
    """G1 消重:每个唯一 URL 的 _url_host 解析次数 = 1(循环解析贯穿闸门/robots)。"""
    calls: list[str] = []
    real = fetcher_mod._url_host

    def counting(url: str) -> str:
        calls.append(url)
        return real(url)

    monkeypatch.setattr(fetcher_mod, "_url_host", counting)

    urls = [
        "http://127.0.0.1:8000/a.png",
        "http://127.0.0.1:8000/b.png",
        "http://127.0.0.1:8000/a.png",  # 重复 URL:跳过,不产生解析
    ]
    evidences = download_images(
        urls, "http://127.0.0.1:8000/page.html", str(tmp_path / "imgs"),
        Config(fetch_delay_s=0.0),
    )

    assert len(evidences) == 2  # 去重后 2 个唯一 URL 各一张
    assert calls == [
        "http://127.0.0.1:8000/a.png",
        "http://127.0.0.1:8000/b.png",
    ]  # 每个唯一 URL 恰好解析一次(升级前为 3 次:循环+闸门+robots)
    digest = hashlib.sha256(_PNG).hexdigest()
    assert evidences[0].path.endswith(f"{digest[:16]}.png")


def test_fetch_page_parses_host_once(
    monkeypatch: pytest.MonkeyPatch, offline_urlopen: None
) -> None:
    """fetch_page 同样受益:闸门解析的 host 贯穿给 robots(1 次,升级前 2 次)。"""
    calls: list[str] = []
    real = fetcher_mod._url_host

    def counting(url: str) -> str:
        calls.append(url)
        return real(url)

    monkeypatch.setattr(fetcher_mod, "_url_host", counting)

    status, html, final = fetch_page("http://127.0.0.1:8000/x", Config())

    assert status == 200 and html and final == "http://127.0.0.1:8000/x"
    assert calls == ["http://127.0.0.1:8000/x"]


def test_host_threading_keeps_gate_and_robots_semantics(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    offline_urlopen: None,
    clean_robots_cache: None,
) -> None:
    """消重后安全语义逐项保持:本机闸门照拦、外网 robots 拒绝照抛(双路径)。"""
    # 1) allow_network=False:外网图片仍被闸门拦截(策略错误向上冒泡)
    with pytest.raises(NetworkDisabledError):
        download_images(
            ["https://example.com/pic.png"], "https://example.com/",
            str(tmp_path / "imgs"), Config(),
        )

    # 2) allow_network=True + robots 全禁:fetch_page 与 download_images 均拒绝
    class _DenyAllParser:
        def set_url(self, url: str) -> None:
            return None

        def parse(self, lines: list[str]) -> None:
            return None

        def can_fetch(self, useragent: str, url: str) -> bool:
            return False

    monkeypatch.setattr(urllib.robotparser, "RobotFileParser", _DenyAllParser)
    # robots.txt 现由 fetcher 侧带超时 urlopen 拉取后 parse 逐行喂入(不再
    # 走 RobotFileParser.read),这里同样打桩 urlopen,保持零真实外呼。
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResp(
            b"User-agent: *\nDisallow: /", request.full_url
        ),
    )
    cfg = Config(allow_network=True)
    with pytest.raises(RobotsDeniedError):
        fetch_page("https://example.com/page", cfg)
    with pytest.raises(RobotsDeniedError):
        download_images(
            ["https://example.com/pic.png"], "https://example.com/",
            str(tmp_path / "imgs2"), cfg,
        )

    # 3) 本机地址不受 robots 影响(host 贯穿不改变本地短路判定)
    status, _html, _final = fetch_page("http://127.0.0.1:8000/ok", Config())
    assert status == 200
