"""netsentinel.crawler.browser 的测试。

- 提取/线索逻辑:注入 fake fetch/download 或直接测 ImgExtractor,不启动浏览器;
- playwright 路径:threading + http.server 起本地页(仅 127.0.0.1),chromium 缺失时 skip;
- 全程离线:所有外网 URL 只出现在注入 fake 的字符串里,绝不发起真实请求。
"""
from __future__ import annotations

import base64
import datetime
import os
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, PageSample
from netsentinel.crawler.browser import (
    BROWSER_OPEN_ATTEMPTS,
    MAX_ATTR_BYTES,
    TEXT_HINTS,
    ImgExtractor,
    _is_transient_browser_error,
    _screenshot_path,
    _shoot_with_playwright,
    capture_page,
)

BASE_URL = "http://site.example/page.html"

# 含相对/绝对/data-src/data: URI/重复/空 src/协议相对等各形态的字符串页面。
PAGE_HTML = """<html><head><title>demo page</title></head><body>
<h1>免费成人电影在线看</h1>
<img src="/img/a.png">
<img src="http://cdn.example.com/b.jpg" data-src="/img/c.png">
<img data-src="data:image/png;base64,AAAA">
<img src="/img/a.png">
<img src="">
<img src="//cdn.example.com/d.webp">
<p>更多 Porn 与 NUDE 内容</p>
</body></html>"""

# 期望的绝对化图片链接(按出现顺序、去重、去 data: URI、跳过空值)。
EXPECTED_ABS = [
    "http://site.example/img/a.png",
    "http://cdn.example.com/b.jpg",
    "http://site.example/img/c.png",
    "http://cdn.example.com/d.webp",
]

# 1x1 透明 PNG,供本地 http 服务充当 <img> 资源(不触网)。
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

# 本地服务页面:图片必须全部指向本机,避免浏览器真的加载外网资源。
LOCAL_HTML = """<html><head><title>本地演示站</title></head><body>
<h1>成人电影 试看片段</h1>
<img src="/img/a.png">
<img src="/img/a.png">
<img data-src="/img/c.png">
<img data-src="data:image/png;base64,AAAA">
<img src="/img/b.png">
</body></html>"""


class _LocalHandler(BaseHTTPRequestHandler):
    """只在 127.0.0.1 上服务的极简静态处理器。"""

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定命名
        if self.path.startswith("/img/"):
            body, ctype = PNG_1PX, "image/png"
        else:
            body, ctype = LOCAL_HTML.encode("utf-8"), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return  # 静音访问日志,避免污染 pytest 输出


class _Recorder:
    """记录注入 fake 的调用参数。"""

    def __init__(self) -> None:
        self.fetch_calls: list[str] = []
        self.download_calls: list[tuple[list[str], str, str]] = []


def _make_fakes(
    html: str = PAGE_HTML,
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


@pytest.fixture()
def cfg(tmp_path: pathlib.Path) -> Config:
    return Config(evidence_dir=str(tmp_path / "evidence"), fetch_timeout_s=10.0)


@pytest.fixture()
def no_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    """屏蔽 playwright 导入(sys.modules 置 None 触发 ImportError),强制走退化路径。"""
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)


# ---------------------------------------------------------------------------
# 提取逻辑(不开浏览器)
# ---------------------------------------------------------------------------

def test_text_hints_constant_matches_contract() -> None:
    assert TEXT_HINTS == ["色情", "成人电影", "裸聊", " AV ", "情色", "porn", "sex video", "nude"]


def test_img_extractor_collects_src_and_data_src_in_document_order() -> None:
    ex = ImgExtractor()
    ex.feed(PAGE_HTML + '<img src="/img/x.gif"/>')
    ex.close()
    # 原始收集:不去重、不去 data:(那是 _extract_image_urls 的职责),空值跳过;
    # 同一标签内 src 与 data-src 按文档属性顺序;自闭合标签同样处理。
    assert ex.links == [
        "/img/a.png",
        "http://cdn.example.com/b.jpg",
        "/img/c.png",
        "data:image/png;base64,AAAA",
        "/img/a.png",
        "//cdn.example.com/d.webp",
        "/img/x.gif",
    ]


def test_capture_page_degraded_mode_extraction(cfg: Config, no_playwright: None) -> None:
    """退化模式(fake fetch/download):绝对化、去重、去 data:、截图为空、hint 命中。"""
    rec, fake_fetch, fake_download = _make_fakes()
    sample = capture_page(BASE_URL, cfg, fetch_page=fake_fetch, download=fake_download)

    assert isinstance(sample, PageSample)
    assert sample.url == BASE_URL
    assert rec.fetch_calls == [BASE_URL]
    # 退化模式:无浏览器截图
    assert sample.screenshot_path == ""

    assert len(rec.download_calls) == 1
    urls, source_page, dest_dir = rec.download_calls[0]
    assert urls == EXPECTED_ABS
    assert source_page == BASE_URL
    assert dest_dir == os.path.join(cfg.evidence_dir, "site.example_imgs")
    assert os.path.isdir(dest_dir)  # dest 目录由 capture_page 负责创建

    assert [e.url for e in sample.image_evidences] == EXPECTED_ABS
    assert all(e.source_page == BASE_URL for e in sample.image_evidences)
    # 命中记录的是 TEXT_HINTS 原文常量(小写化匹配后命中)
    assert sample.text_hint_hits == ["成人电影", "porn", "nude"]


def test_capture_page_respects_max_images_cap(cfg: Config, no_playwright: None) -> None:
    cfg.max_images_per_page = 2
    rec, fake_fetch, fake_download = _make_fakes()
    sample = capture_page(BASE_URL, cfg, fetch_page=fake_fetch, download=fake_download)

    urls, _, _ = rec.download_calls[0]
    assert urls == EXPECTED_ABS[:2]  # 截断且保持顺序
    assert [e.url for e in sample.image_evidences] == EXPECTED_ABS[:2]


def test_capture_page_no_images_skips_download(cfg: Config, no_playwright: None) -> None:
    rec, fake_fetch, fake_download = _make_fakes(html="<html><body><p>纯文字页</p></body></html>")
    sample = capture_page(BASE_URL, cfg, fetch_page=fake_fetch, download=fake_download)

    assert rec.download_calls == []
    assert sample.image_evidences == []
    assert sample.screenshot_path == ""
    assert sample.text_hint_hits == []


def test_text_hint_matching_case_and_spacing(cfg: Config, no_playwright: None) -> None:
    """大小写不敏感;" AV " 需两侧空格才命中,AVATAR 不算。"""
    html = "<html><body>porn Porn NUDE nuDe 情色 情色 裸聊 watch AV now AVATAR</body></html>"
    rec, fake_fetch, fake_download = _make_fakes(html=html)
    sample = capture_page(BASE_URL, cfg, fetch_page=fake_fetch, download=fake_download)

    assert sample.text_hint_hits == ["裸聊", " AV ", "情色", "porn", "nude"]  # 按 TEXT_HINTS 表序


def test_missing_fetcher_raises_chinese_runtime_error(
    cfg: Config, no_playwright: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未注入 fake 且 fetcher 模块不可导入时,抛中文 RuntimeError(并行开发容错)。"""
    monkeypatch.setitem(sys.modules, "netsentinel.crawler.fetcher", None)  # 强制 ImportError
    with pytest.raises(RuntimeError, match="fetcher 模块未就位"):
        capture_page(BASE_URL, cfg)


# ---------------------------------------------------------------------------
# playwright 路径(本地 127.0.0.1 服务;缺 chromium 则 skip)
# ---------------------------------------------------------------------------

def test_playwright_guard_blocks_nonlocal_navigation(cfg: Config) -> None:
    """安全红线:allow_network=False 时浏览器只允许导航本机地址。"""
    pytest.importorskip("playwright")
    cfg.allow_network = False
    with pytest.raises(RuntimeError, match="仅允许访问本机地址"):
        capture_page("https://example.com/page.html", cfg)


def test_capture_page_playwright_local_server(cfg: Config) -> None:
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    # 预检 chromium 二进制是否可用(缺浏览器时按契约 skip,不算失败)。
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    except Exception as exc:  # noqa: BLE001 - 环境缺 chromium 等问题一律跳过
        pytest.skip(f"chromium 未安装:{exc}")
    else:
        browser.close()
        pw.stop()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        rec, fake_fetch, fake_download = _make_fakes()
        url = f"http://127.0.0.1:{port}/index.html"
        sample = capture_page(url, cfg, fetch_page=fake_fetch, download=fake_download)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert isinstance(sample, PageSample)
    assert sample.url == url
    assert rec.fetch_calls == []  # playwright 路径不应调用退化抓取

    # 整页截图已落盘,文件名 <safe_host>_<HHMMSS>.png
    assert sample.screenshot_path.endswith(".png")
    assert os.path.isfile(sample.screenshot_path)
    name = os.path.basename(sample.screenshot_path)
    assert name.startswith("127.0.0.1_")
    assert len(name.rsplit("_", 1)[-1].split(".")[0]) == 6  # HHMMSS

    expected = [
        f"http://127.0.0.1:{port}/img/a.png",
        f"http://127.0.0.1:{port}/img/c.png",
        f"http://127.0.0.1:{port}/img/b.png",
    ]
    urls, source_page, dest_dir = rec.download_calls[0]
    assert urls == expected  # 顺序、去重(第二个 a.png)、data: 丢弃
    assert source_page == url
    assert os.path.basename(dest_dir) == f"127.0.0.1_{port}_imgs"  # 端口中的 : 被安全化为 _
    assert [e.url for e in sample.image_evidences] == expected
    assert sample.text_hint_hits == ["成人电影"]


# ---------------------------------------------------------------------------
# V5 升级:瞬态重试 / 截图命名碰撞安全 / 遥测 / 超长属性防护
# ---------------------------------------------------------------------------


@pytest.fixture()
def tel():
    """隔离全局遥测:用例前置清零、退出还原(避免污染其他用例的指标断言)。"""
    telemetry.reset()
    yield telemetry
    telemetry.reset()


class _FakeTargetPage:
    """playwright Page 替身:记录 goto,截图落盘真实 PNG 字节。"""

    def __init__(self, browser: "_FakeTargetBrowser") -> None:
        self._browser = browser
        self.goto_calls: list[str] = []

    def goto(self, url: str, timeout: float | None = None) -> None:
        self.goto_calls.append(url)
        self._browser._raise_scripted_goto_error()

    def wait_for_load_state(self, state: str, timeout: float | None = None) -> None:
        return  # networkidle 立即就绪

    def screenshot(self, path: str, full_page: bool = False) -> None:
        self._browser.screenshot_paths.append(path)
        with open(path, "wb") as fh:
            fh.write(PNG_1PX)

    def content(self) -> str:
        return self._browser.html


class _FakeTargetBrowser:
    """playwright Browser 替身:goto 阶段可按剧本抛一次异常。"""

    def __init__(self, html: str = LOCAL_HTML, goto_errors: list[BaseException] | None = None) -> None:
        self.html = html
        self._goto_errors = list(goto_errors or [])
        self.pages: list[_FakeTargetPage] = []
        self.screenshot_paths: list[str] = []
        self.closed = False

    def _raise_scripted_goto_error(self) -> None:
        if self._goto_errors:
            raise self._goto_errors.pop(0)

    def new_page(self) -> _FakeTargetPage:
        page = _FakeTargetPage(self)
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.closed = True


class _FakePlaywrightAPI:
    """sync_playwright 替身:() → .start() → .chromium.launch 按剧本返回实例或抛异常。"""

    def __init__(self, launches: list[BaseException | _FakeTargetBrowser]) -> None:
        self._launches = list(launches)
        self.launch_calls = 0
        self.stops = 0
        outer = self

        class _Chromium:
            def launch(self, headless: bool = True) -> "_FakeTargetBrowser":
                return outer._launch()

        self.chromium = _Chromium()

    def _launch(self) -> "_FakeTargetBrowser":
        self.launch_calls += 1
        item = self._launches.pop(0) if self._launches else _FakeTargetBrowser()
        if isinstance(item, BaseException):
            raise item
        return item

    def start(self) -> "_FakePlaywrightAPI":
        return self

    def stop(self) -> None:
        self.stops += 1


def test_v5_is_transient_browser_error_matches_timeout_only() -> None:
    """瞬态判据:超时类(含子类/MRO)可重试;ImportError/业务异常不匹配。"""

    class PlaywrightLikeTimeout(Exception):
        pass

    PlaywrightLikeTimeout.__name__ = "TimeoutError"
    assert _is_transient_browser_error(TimeoutError("t")) is True
    assert _is_transient_browser_error(PlaywrightLikeTimeout("pw")) is True  # MRO 名匹配
    assert _is_transient_browser_error(ImportError("playwright 缺失")) is False
    assert _is_transient_browser_error(RuntimeError("boom")) is False
    assert _is_transient_browser_error(AssertionError("x")) is False


def test_v5_transient_launch_timeout_retried_once(cfg: Config, tel: object) -> None:
    """launch 阶段超时类异常自动重试 1 次:第二次成功并产出截图与 HTML。"""
    ok = _FakeTargetBrowser()
    api = _FakePlaywrightAPI([TimeoutError("chromium launch timed out"), ok])
    shot, html = _shoot_with_playwright(lambda: api, "http://127.0.0.1:8000/x.html", cfg)

    assert api.launch_calls == BROWSER_OPEN_ATTEMPTS == 2
    assert api.stops == 1
    assert os.path.isfile(shot) and shot.endswith(".png")
    assert html == LOCAL_HTML
    counters = telemetry.snapshot()["counters"]
    assert counters.get("capture.transient_retry") == 1.0
    assert counters.get("capture.screenshot") == 1.0


def test_v5_transient_goto_timeout_retried_once(cfg: Config, tel: object) -> None:
    """goto 阶段超时类异常同样重试 1 次:失败实例先关闭,新实例完成导航。"""
    dead = _FakeTargetBrowser(goto_errors=[TimeoutError("net::ERR_TIMED_OUT")])
    ok = _FakeTargetBrowser()
    api = _FakePlaywrightAPI([dead, ok])
    shot, _html = _shoot_with_playwright(lambda: api, "http://127.0.0.1:8000/y.html", cfg)

    assert api.launch_calls == 2
    assert dead.closed is True  # 重试前关闭泄漏的实例
    assert dead.pages[0].goto_calls and ok.pages[0].goto_calls
    assert os.path.isfile(shot)
    assert telemetry.snapshot()["counters"].get("capture.transient_retry") == 1.0


def test_v5_non_transient_launch_error_not_retried(cfg: Config, tel: object) -> None:
    """非超时类异常(launch 崩溃)不重试:首次即原样抛出。"""
    api = _FakePlaywrightAPI([RuntimeError("chromium 二进制损坏")])
    with pytest.raises(RuntimeError, match="chromium 二进制损坏"):
        _shoot_with_playwright(lambda: api, "http://127.0.0.1:8000/z.html", cfg)

    assert api.launch_calls == 1
    assert "capture.transient_retry" not in telemetry.snapshot()["counters"]


def test_v5_transient_timeout_exhausted_after_one_retry(cfg: Config, tel: object) -> None:
    """连续两次超时:只重试 1 次,重试仍失败则原样抛出(不无限重试)。"""
    api = _FakePlaywrightAPI([TimeoutError("t1"), TimeoutError("t2")])
    with pytest.raises(TimeoutError):
        _shoot_with_playwright(lambda: api, "http://127.0.0.1:8000/w.html", cfg)

    assert api.launch_calls == 2
    assert telemetry.snapshot()["counters"].get("capture.transient_retry") == 1.0


def test_v5_screenshot_path_collision_safe(cfg: Config) -> None:
    """同秒同主机多次截图:文件名追加 _1/_2 序号;不同主机互不冲突。"""
    os.makedirs(cfg.evidence_dir, exist_ok=True)  # 生产路径由 _shoot_with_playwright 负责建目录
    ts = datetime.datetime(2026, 1, 1, 12, 30, 5)
    url = "http://127.0.0.1:8000/page.html"

    p1 = _screenshot_path(cfg.evidence_dir, url, ts)
    assert os.path.basename(p1) == "127.0.0.1_8000_123005.png"  # 无冲突:命名同 v1
    pathlib.Path(p1).write_bytes(PNG_1PX)

    p2 = _screenshot_path(cfg.evidence_dir, url, ts)
    assert os.path.basename(p2) == "127.0.0.1_8000_123005_1.png"
    pathlib.Path(p2).write_bytes(PNG_1PX)

    p3 = _screenshot_path(cfg.evidence_dir, url, ts)
    assert os.path.basename(p3) == "127.0.0.1_8000_123005_2.png"

    other = _screenshot_path(cfg.evidence_dir, "http://localhost:9000/x", ts)
    assert os.path.basename(other) == "localhost_9000_123005.png"  # 不同主机(含端口)不冲突


def test_v5_img_extractor_skips_oversized_attribute() -> None:
    """单个属性超过 2MB 上限时跳过(防内存放大);正常属性不受影响。"""
    assert MAX_ATTR_BYTES == 2 * 1024 * 1024
    huge = "http://evil.example/" + "a" * MAX_ATTR_BYTES
    html = f'<img src="/ok.png"><img data-src="{huge}"><img src="/ok2.png">'

    ex = ImgExtractor()
    ex.feed(html)
    ex.close()

    assert ex.links == ["/ok.png", "/ok2.png"]  # 超长属性被跳过,不影响其余收集
    # 恰好等于上限的属性仍正常收集(边界:仅"超过"才跳过)
    edge = ImgExtractor()
    edge.feed(f'<img src="{"b" * MAX_ATTR_BYTES}">')
    edge.close()
    assert len(edge.links) == 1


def test_v5_capture_page_degraded_emits_telemetry(cfg: Config, no_playwright: None, tel: object) -> None:
    """退化模式遥测:capture.degraded + capture.page 计时;无 screenshot 计数。"""
    rec, fake_fetch, fake_download = _make_fakes()
    capture_page(BASE_URL, cfg, fetch_page=fake_fetch, download=fake_download)

    snap = telemetry.snapshot()
    assert snap["counters"].get("capture.degraded") == 1.0
    assert "capture.screenshot" not in snap["counters"]
    assert snap["timers"]["capture.page"]["count"] == 1


def test_v5_capture_page_counts_errors_and_times_failures(
    cfg: Config, no_playwright: None, tel: object
) -> None:
    """异常路径:capture.errors 计数、capture.page 计时(失败同样计时)、异常原样抛出。"""

    def boom(url: str, cfg_: Config) -> tuple[int, str, str]:
        raise RuntimeError("抓取失败")

    with pytest.raises(RuntimeError, match="抓取失败"):
        capture_page(BASE_URL, cfg, fetch_page=boom)

    snap = telemetry.snapshot()
    assert snap["counters"].get("capture.errors") == 1.0
    assert snap["timers"]["capture.page"]["count"] == 1


def test_v5_capture_page_real_browser_telemetry(cfg: Config, tel: object) -> None:
    """真实 chromium 端到端:capture.screenshot 计数 + capture.page 计时同步落地。"""
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    except Exception as exc:  # noqa: BLE001 - 环境缺 chromium 等问题一律跳过
        pytest.skip(f"chromium 未安装:{exc}")
    else:
        browser.close()
        pw.stop()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        rec, fake_fetch, fake_download = _make_fakes()
        url = f"http://127.0.0.1:{port}/index.html"
        sample = capture_page(url, cfg, fetch_page=fake_fetch, download=fake_download)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert sample.screenshot_path and os.path.isfile(sample.screenshot_path)
    snap = telemetry.snapshot()
    assert snap["counters"].get("capture.screenshot") == 1.0
    assert "capture.degraded" not in snap["counters"]
    assert snap["timers"]["capture.page"]["count"] == 1
