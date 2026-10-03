# -*- coding: utf-8 -*-
"""netsentinel.crawler.fetcher 单元测试(A02)。

全部离线:仅访问 127.0.0.1 上的本地 http.server(随机端口),
文件只写入 pytest 的 tmp_path,不触碰任何真实外网或举报门户。
"""
from __future__ import annotations

import functools
import hashlib
import logging
import socket
import struct
import threading
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.crawler import fetcher
from netsentinel.crawler.fetcher import (
    NetworkDisabledError,
    RobotsDeniedError,
    download_images,
    fetch_page,
)


# ---------------------------------------------------------------------------
# 测试素材:用标准库构造最小合法 PNG(1x1 像素),避免依赖外网或二进制夹具
# ---------------------------------------------------------------------------

def _build_png(rgb: bytes) -> bytes:
    """构造 1x1 RGB 的最小合法 PNG 字节串。"""
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 宽1 高1 8bit 真彩色
    idat = zlib.compress(b"\x00" + rgb)  # 滤镜字节 + 单像素
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


PNG_A = _build_png(b"\xff\x00\x00")  # 红色像素
PNG_B = _build_png(b"\x00\x00\xff")  # 蓝色像素
PNG_A_SHA256 = hashlib.sha256(PNG_A).hexdigest()
PNG_B_SHA256 = hashlib.sha256(PNG_B).hexdigest()

INDEX_HTML = "<html><body><h1>NetSentinel 测试页</h1></body></html>"


# ---------------------------------------------------------------------------
# 本地 HTTP 服务(threading + http.server,host 固定 127.0.0.1)
# ---------------------------------------------------------------------------

class _Handler(SimpleHTTPRequestHandler):
    """静态文件服务(目录由 partial 注入),附带两条测试专用路由。"""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return  # 静默访问日志,避免污染 pytest 输出

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/echo-ua":
            body = (self.headers.get("User-Agent") or "").encode("ascii")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/no-type.png":
            # 故意不发送 Content-Type,也不发 Content-Length(HTTP/1.0 连接关闭即结束)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(PNG_B)
            return
        super().do_GET()


class LocalServer:
    """运行在 127.0.0.1 随机端口的后台线程 HTTP 服务。"""

    def __init__(self, directory: str) -> None:
        handler = functools.partial(_Handler, directory=directory)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def _write_static_files(root: Path) -> None:
    (root / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (root / "img.png").write_bytes(PNG_A)
    (root / "note.txt").write_text("这不是图片,只是普通文本。", encoding="utf-8")
    (root / "big.png").write_bytes(PNG_A + b"\x00" * (2 * 1024 * 1024))  # 约 2MB,超过 1MB 上限
    (root / "big.html").write_text("x" * (3 * 1024 * 1024), encoding="utf-8")  # 超过 2MB 读取上限


@pytest.fixture()
def server(tmp_path: Path) -> LocalServer:
    _write_static_files(tmp_path)
    srv = LocalServer(str(tmp_path))
    yield srv
    srv.close()


def make_cfg(**overrides: object) -> Config:
    """默认安全配置(禁网、无延迟),按需覆盖字段。"""
    values: dict[str, object] = {
        "allow_network": False,
        "fetch_timeout_s": 5.0,
        "fetch_delay_s": 0.0,  # 测试不等待
        "respect_robots": True,
    }
    values.update(overrides)
    return Config(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# fetch_page
# ---------------------------------------------------------------------------

def test_fetch_page_success(server: LocalServer) -> None:
    """本机 200:返回状态码、HTML 内容与最终 URL。"""
    url = f"{server.base_url}/index.html"
    status, html, final_url = fetch_page(url, make_cfg())
    assert status == 200
    assert html == INDEX_HTML
    assert final_url == url


def test_fetch_page_sends_declared_user_agent(server: LocalServer) -> None:
    """请求头 User-Agent 必须是契约规定的 NetSentinel/0.1。"""
    status, html, _ = fetch_page(f"{server.base_url}/echo-ua", make_cfg())
    assert status == 200
    assert html.strip() == "NetSentinel/0.1"


def test_fetch_page_404_returns_status_without_raising(server: LocalServer) -> None:
    """HTTP 404 不抛异常:status=404,html 为空。"""
    url = f"{server.base_url}/missing.html"
    status, html, final_url = fetch_page(url, make_cfg())
    assert status == 404
    assert html == ""
    assert final_url == url


def test_fetch_page_truncates_over_2mb(server: LocalServer) -> None:
    """超过 2MB 的响应体被截断,不多读、不报错。"""
    status, html, _ = fetch_page(f"{server.base_url}/big.html", make_cfg())
    assert status == 200
    assert len(html) == 2 * 1024 * 1024
    assert html == "x" * (2 * 1024 * 1024)


def test_fetch_page_connection_refused_returns_zero(tmp_path: Path) -> None:
    """连接层失败(URLError):返回 (0, "", 原始 URL),不抛出。"""
    _write_static_files(tmp_path)
    srv = LocalServer(str(tmp_path))
    url = f"{srv.base_url}/index.html"
    srv.close()  # 关闭服务制造"连接被拒绝"
    status, html, final_url = fetch_page(url, make_cfg())
    assert status == 0
    assert html == ""
    assert final_url == url


def test_fetch_page_blocked_for_external_when_network_disabled() -> None:
    """allow_network=False 时外网 URL 必须在发出请求前被拦截。"""
    url = "https://example.com/some/page"
    with pytest.raises(NetworkDisabledError) as excinfo:
        fetch_page(url, make_cfg())  # allow_network=False
    message = str(excinfo.value)
    assert url in message  # 消息含被拒 URL
    assert "allow_network" in message  # 消息含开启方式提示


# ---------------------------------------------------------------------------
# robots 策略(离线打桩,绝不真实外网请求)
# ---------------------------------------------------------------------------

class _DenyAllParser:
    """替身:任何 URL 都判为 robots 禁止。"""

    def set_url(self, url: str) -> None:
        self.url = url

    def read(self) -> None:
        return None

    def can_fetch(self, useragent: str, url: str) -> bool:
        return False


def test_fetch_page_robots_denied_for_real_external(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实外网目标被 robots 禁止时抛 RobotsDeniedError(打桩,无真实请求)。"""
    monkeypatch.setattr(urllib.robotparser, "RobotFileParser", _DenyAllParser)
    with pytest.raises(RobotsDeniedError):
        fetch_page("https://example.com/page", make_cfg(allow_network=True))


def test_robots_check_skipped_for_local(server: LocalServer, monkeypatch: pytest.MonkeyPatch) -> None:
    """本机地址不适用 robots:即便 robots 全部禁止,本机抓取仍成功。"""
    monkeypatch.setattr(urllib.robotparser, "RobotFileParser", _DenyAllParser)
    status, html, _ = fetch_page(f"{server.base_url}/index.html", make_cfg())
    assert status == 200
    assert "NetSentinel 测试页" in html


# ---------------------------------------------------------------------------
# download_images
# ---------------------------------------------------------------------------

def test_download_images_success_dedup_and_skip_non_image(server: LocalServer, tmp_path: Path) -> None:
    """本机 PNG 下载成功、sha256/文件名正确、按 URL 去重、非图片 Content-Type 跳过。"""
    page = f"{server.base_url}/index.html"
    png_url = f"{server.base_url}/img.png"
    txt_url = f"{server.base_url}/note.txt"
    dest = str(tmp_path / "images")

    evidences = download_images([png_url, txt_url, png_url, txt_url, png_url], page, dest, make_cfg())

    assert len(evidences) == 1  # 去重后仅一张图片;文本被 Content-Type 拒绝
    ev = evidences[0]
    assert ev.url == png_url
    assert ev.source_page == page
    assert ev.sha256 == PNG_A_SHA256
    assert ev.width == 0 and ev.height == 0  # 宽高由后续模块补充,此处恒 0
    saved = Path(ev.path)
    assert saved.parent == Path(dest)
    assert saved.name == PNG_A_SHA256[:16] + ".png"  # sha256 前 16 位 + 保留原扩展名
    assert saved.read_bytes() == PNG_A


def test_download_images_missing_content_type_with_image_suffix(server: LocalServer, tmp_path: Path) -> None:
    """响应头缺失 Content-Type 时,URL 后缀为图片扩展名则放行;且保持顺序。"""
    page = f"{server.base_url}/index.html"
    plain_url = f"{server.base_url}/img.png"  # 服务端会带 image/png
    notype_url = f"{server.base_url}/no-type.png"  # 无 Content-Type,后缀 .png
    dest = str(tmp_path / "images")

    evidences = download_images([plain_url, notype_url], page, dest, make_cfg())

    assert [e.url for e in evidences] == [plain_url, notype_url]  # 顺序保持
    assert evidences[0].sha256 == PNG_A_SHA256
    assert evidences[1].sha256 == PNG_B_SHA256
    assert Path(evidences[1].path).name == PNG_B_SHA256[:16] + ".png"
    assert Path(evidences[1].path).read_bytes() == PNG_B


def test_download_images_skips_oversized(server: LocalServer, tmp_path: Path) -> None:
    """超过 max_image_mb 的图片跳过,同批正常大小图片不受影响。"""
    page = f"{server.base_url}/index.html"
    dest = str(tmp_path / "images")
    cfg = make_cfg(max_image_mb=1)  # 上限 1MB

    evidences = download_images(
        [f"{server.base_url}/big.png", f"{server.base_url}/img.png"], page, dest, cfg
    )

    assert [e.url for e in evidences] == [f"{server.base_url}/img.png"]
    assert Path(evidences[0].path).read_bytes() == PNG_A


def test_download_images_zero_limit_skips_everything(server: LocalServer, tmp_path: Path) -> None:
    """max_image_mb=0:任何非空文件都超限,应全部跳过且不抛错。"""
    cfg = make_cfg(max_image_mb=0)
    evidences = download_images(
        [f"{server.base_url}/img.png"], f"{server.base_url}/index.html",
        str(tmp_path / "images"), cfg,
    )
    assert evidences == []


def test_download_images_gate_blocks_external(tmp_path: Path) -> None:
    """allow_network=False 时,图片下载同样执行安全闸门。"""
    with pytest.raises(NetworkDisabledError):
        download_images(
            ["https://example.com/pic.png"], "https://example.com/",
            str(tmp_path / "images"), make_cfg(),
        )


# ---------------------------------------------------------------------------
# V5 升级:连接类错误指数退避重试 / robots TTL 缓存 / 同 host 连续失败提前放弃 / 遥测
# 全部离线:urlopen 与退避休眠均打桩,零外呼、零真实等待。
# ---------------------------------------------------------------------------


class _FakeResponse:
    """urllib 响应替身:支持 with 上下文 / status / geturl / read / headers.get。"""

    def __init__(
        self,
        url: str,
        body: bytes,
        status: int = 200,
        content_type: str | None = "text/html; charset=utf-8",
    ) -> None:
        self.status = status
        self._url = url
        self._body = body
        self.headers: dict[str, str] = {} if content_type is None else {"Content-Type": content_type}

    def geturl(self) -> str:
        return self._url

    def read(self, amount: int = -1) -> bytes:
        if amount is None or amount < 0 or amount >= len(self._body):
            return self._body
        return self._body[:amount]

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


@pytest.fixture()
def no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """打桩退避休眠:不真实等待,记录每次重试的 attempt 序号。"""
    attempts: list[int] = []
    monkeypatch.setattr(fetcher, "_sleep_backoff", attempts.append)
    return attempts


def test_v5_backoff_delay_exponential_with_jitter() -> None:
    """退避时长按指数增长并带随机抖动:0.5s 档 [0.5, 0.625],1.0s 档 [1.0, 1.25]。"""
    for _ in range(20):
        first = fetcher._backoff_delay(1)
        second = fetcher._backoff_delay(2)
        assert 0.5 <= first <= 0.5 * (1 + fetcher.RETRY_BACKOFF_JITTER)
        assert 1.0 <= second <= 1.0 * (1 + fetcher.RETRY_BACKOFF_JITTER)
        assert second > first  # 指数翻倍严格大于上一档(含抖动上界)


def test_v5_retryable_error_classification() -> None:
    """仅 URLError 包裹的超时/连接重置族可重试;HTTP 错误、拒绝连接、DNS 失败不可重试。"""
    retryable = [
        urllib.error.URLError(TimeoutError("timed out")),
        urllib.error.URLError(ConnectionResetError("reset by peer")),
        urllib.error.URLError(ConnectionAbortedError()),
        urllib.error.URLError(BrokenPipeError()),
    ]
    not_retryable = [
        urllib.error.URLError(ConnectionRefusedError("refused")),
        urllib.error.URLError(socket.gaierror(8, "name resolution failed")),
        urllib.error.HTTPError("http://x/", 503, "Service Unavailable", None, None),
        RuntimeError("与 URLError 无关的异常"),
    ]
    for exc in retryable:
        assert fetcher._is_retryable_connection_error(exc) is True, exc
    for exc in not_retryable:
        assert fetcher._is_retryable_connection_error(exc) is False, exc


def test_v5_fetch_page_retries_connection_errors_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, no_backoff_sleep: list[int]
) -> None:
    """超时/连接重置各失败一次后第三次成功:结果正常返回,共尝试 3 次、退避 2 次。"""
    telemetry.reset()
    url = "http://127.0.0.1:65535/page"
    calls: list[str] = []

    def flaky_urlopen(request: object, timeout: float) -> _FakeResponse:
        calls.append(getattr(request, "full_url", ""))
        if len(calls) == 1:
            raise urllib.error.URLError(TimeoutError("timed out"))
        if len(calls) == 2:
            raise urllib.error.URLError(ConnectionResetError("reset by peer"))
        return _FakeResponse(url, b"ok")

    monkeypatch.setattr(urllib.request, "urlopen", flaky_urlopen)
    status, html, final_url = fetch_page(url, make_cfg())

    assert (status, html, final_url) == (200, "ok", url)
    assert calls == [url] * 3  # 1 次原始 + 2 次重试
    assert no_backoff_sleep == [1, 2]  # 退避档位:第 1、2 次重试
    counters = telemetry.snapshot()["counters"]
    assert counters.get("fetch.retry") == 2
    assert counters.get("fetch.page") == 1
    assert counters.get("fetch.page.200") == 1


def test_v5_fetch_page_gives_up_after_retry_budget(
    monkeypatch: pytest.MonkeyPatch, no_backoff_sleep: list[int]
) -> None:
    """持续超时:重试预算(<= 2 次)用尽后返回 (0, "", url),不抛出。"""
    telemetry.reset()
    url = "http://127.0.0.1:65535/page"
    calls: list[str] = []

    def always_timeout(request: object, timeout: float) -> _FakeResponse:
        calls.append(getattr(request, "full_url", ""))
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(urllib.request, "urlopen", always_timeout)
    status, html, final_url = fetch_page(url, make_cfg())

    assert (status, html, final_url) == (0, "", url)
    assert len(calls) == fetcher.RETRY_MAX_ATTEMPTS  # 最多 3 次尝试
    assert no_backoff_sleep == [1, 2]
    counters = telemetry.snapshot()["counters"]
    assert counters.get("fetch.retry") == 2
    assert counters.get("fetch.errors") == 1
    assert counters.get("fetch.page.0") == 1


def test_v5_fetch_page_http_error_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 5xx 是应用层应答:不重试(单次尝试),按既有语义返回错误码。"""
    telemetry.reset()
    url = "http://127.0.0.1:65535/page"
    calls: list[str] = []

    def always_503(request: object, timeout: float) -> _FakeResponse:
        calls.append(getattr(request, "full_url", ""))
        raise urllib.error.HTTPError(url, 503, "Service Unavailable", None, None)

    monkeypatch.setattr(urllib.request, "urlopen", always_503)
    status, html, final_url = fetch_page(url, make_cfg())

    assert (status, html, final_url) == (503, "", url)
    assert len(calls) == 1  # HTTP 4xx/5xx 绝不重试
    assert "fetch.retry" not in telemetry.snapshot()["counters"]


def test_v5_fetch_page_connection_refused_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """连接被拒绝属确定性失败:保持单次尝试的既有行为,直接返回 0。"""
    url = "http://127.0.0.1:65535/page"
    calls: list[str] = []

    def refused(request: object, timeout: float) -> _FakeResponse:
        calls.append(getattr(request, "full_url", ""))
        raise urllib.error.URLError(ConnectionRefusedError("connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    status, html, final_url = fetch_page(url, make_cfg())

    assert (status, html, final_url) == (0, "", url)
    assert len(calls) == 1


class _CountingAllowParser:
    """替身:read 成功、can_fetch 一律允许;按类属性统计真实拉取次数。"""

    reads = 0

    def set_url(self, url: str) -> None:
        self.url = url

    def read(self) -> None:
        type(self).reads += 1

    def can_fetch(self, useragent: str, url: str) -> bool:
        return True


@pytest.fixture()
def clean_robots_cache() -> None:
    """隔离模块级 robots 缓存:进入/退出各清一次,避免跨用例污染。"""
    fetcher._ROBOTS_CACHE.clear()
    yield
    fetcher._ROBOTS_CACHE.clear()


def test_v5_robots_cached_per_site_within_ttl(
    monkeypatch: pytest.MonkeyPatch, clean_robots_cache: None
) -> None:
    """同站点(同 netloc)60s 内只拉一次 robots.txt;不同站点互不共享;TTL 过期重拉。"""
    _CountingAllowParser.reads = 0
    monkeypatch.setattr(urllib.robotparser, "RobotFileParser", _CountingAllowParser)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse("http://x/", b"page"),
    )
    cfg = make_cfg(allow_network=True)  # 仅经打桩路径,无真实外呼

    fetch_page("http://cache-a.example/page1", cfg)
    fetch_page("http://cache-a.example/page2", cfg)  # 命中缓存:不再拉取
    assert _CountingAllowParser.reads == 1

    fetch_page("http://cache-b.example/page", cfg)  # 不同站点:独立拉取
    assert _CountingAllowParser.reads == 2

    monkeypatch.setattr(fetcher, "ROBOTS_CACHE_TTL_S", 0.0)  # TTL 归零即刻过期
    fetch_page("http://cache-a.example/page3", cfg)
    assert _CountingAllowParser.reads == 3


def test_v5_robots_read_failure_not_cached(
    monkeypatch: pytest.MonkeyPatch, clean_robots_cache: None
) -> None:
    """robots.txt 拉取失败按"允许"处理且不缓存:下次校验会再次尝试拉取。"""

    class _FlakyRobotsParser(_CountingAllowParser):
        def read(self) -> None:
            type(self).reads += 1
            raise urllib.error.URLError("robots.txt 不可达")

    _FlakyRobotsParser.reads = 0
    monkeypatch.setattr(urllib.robotparser, "RobotFileParser", _FlakyRobotsParser)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse("http://x/", b"page"),
    )
    cfg = make_cfg(allow_network=True)

    assert fetch_page("http://flaky.example/page1", cfg)[0] == 200  # 失败按允许放行
    assert fetch_page("http://flaky.example/page2", cfg)[0] == 200
    assert _FlakyRobotsParser.reads == 2  # 未缓存:两次都真实尝试了拉取
    assert fetcher._ROBOTS_CACHE == {}  # 失败结果绝不进入缓存


def test_v5_download_images_abandons_host_after_consecutive_failures(
    monkeypatch: pytest.MonkeyPatch,
    no_backoff_sleep: list[int],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """同一 host 连续失败 3 次:提前放弃本批该 host 剩余 URL,其他 host 不受影响。"""
    telemetry.reset()
    calls: list[str] = []

    def host_flaky_urlopen(request: object, timeout: float) -> _FakeResponse:
        target = getattr(request, "full_url", "")
        calls.append(target)
        if urllib.parse.urlsplit(target).hostname == "127.0.0.1":
            raise urllib.error.URLError(TimeoutError("timed out"))
        return _FakeResponse(target, PNG_B, content_type="image/png")

    monkeypatch.setattr(urllib.request, "urlopen", host_flaky_urlopen)
    urls = [
        "http://127.0.0.1:65535/a.png",
        "http://127.0.0.1:65535/b.png",
        "http://127.0.0.1:65535/c.png",
        "http://127.0.0.1:65535/d.png",  # 放弃后同 host 剩余项:不再尝试
        "http://localhost:65535/ok.png",  # 其他 host:不受影响
    ]
    with caplog.at_level(logging.WARNING, logger="netsentinel.crawler.fetcher"):
        evidences = download_images(urls, "http://127.0.0.1:65535/", str(tmp_path / "img"), make_cfg())

    assert [e.url for e in evidences] == ["http://localhost:65535/ok.png"]
    # 前 3 个 URL 各耗尽 3 次尝试(9 次);第 4 个从未发出;localhost 1 次
    assert len(calls) == 3 * fetcher.RETRY_MAX_ATTEMPTS + 1
    assert "http://127.0.0.1:65535/d.png" not in calls
    assert any("提前放弃" in rec.message for rec in caplog.records)

    counters = telemetry.snapshot()["counters"]
    assert counters.get("fetch.host_abandoned") == 1
    assert counters.get("fetch.errors") == 3  # 3 个最终失败的 URL
    assert counters.get("fetch.retry") == 6   # 每个失败 URL 重试 2 次


def test_v5_download_images_success_resets_failure_streak(
    monkeypatch: pytest.MonkeyPatch, no_backoff_sleep: list[int], tmp_path: Path
) -> None:
    """连续失败计数可被成功应答重置:失败-失败-成功-失败-失败-成功 不触发放弃。"""
    telemetry.reset()
    calls: list[str] = []

    def name_flaky_urlopen(request: object, timeout: float) -> _FakeResponse:
        target = getattr(request, "full_url", "")
        calls.append(target)
        if "/fail" in target:
            raise urllib.error.URLError(TimeoutError("timed out"))
        return _FakeResponse(target, PNG_B, content_type="image/png")

    monkeypatch.setattr(urllib.request, "urlopen", name_flaky_urlopen)
    base = "http://127.0.0.1:65535"
    urls = [f"{base}/fail1.png", f"{base}/fail2.png", f"{base}/ok1.png",
            f"{base}/fail3.png", f"{base}/fail4.png", f"{base}/ok2.png"]

    evidences = download_images(urls, base + "/", str(tmp_path / "img"), make_cfg())

    assert [e.url for e in evidences] == [f"{base}/ok1.png", f"{base}/ok2.png"]
    assert telemetry.snapshot()["counters"].get("fetch.host_abandoned", 0) == 0
    assert telemetry.snapshot()["counters"].get("fetch.errors") == 4


def test_v5_fetch_page_telemetry_timer_and_counters(server: LocalServer) -> None:
    """fetch_page 记录 fetch.page 计时与按状态计数(本地 server 实路径)。"""
    telemetry.reset()
    fetch_page(f"{server.base_url}/index.html", make_cfg())
    snapshot = telemetry.snapshot()
    assert snapshot["timers"]["fetch.page"]["count"] == 1
    assert snapshot["counters"]["fetch.page"] == 1
    assert snapshot["counters"]["fetch.page.200"] == 1


def test_v5_download_images_telemetry_timer(server: LocalServer, tmp_path: Path) -> None:
    """每张图片的下载(含落盘)整体计入 fetch.image 计时与计数。"""
    telemetry.reset()
    download_images(
        [f"{server.base_url}/img.png"], f"{server.base_url}/index.html",
        str(tmp_path / "images"), make_cfg(),
    )
    snapshot = telemetry.snapshot()
    assert snapshot["timers"]["fetch.image"]["count"] == 1
    assert snapshot["counters"]["fetch.image"] == 1
