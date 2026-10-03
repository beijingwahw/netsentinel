# -*- coding: utf-8 -*-
"""netsentinel.crawler.redirect 单元测试(A47)。

全部离线:仅访问 127.0.0.1 上的本地 http.server(随机端口);
跨 host 用例借助 localhost 别名(同为闸门放行的本机地址),不发起任何
真实外网请求,不触碰举报门户。
"""
from __future__ import annotations

import functools
import logging
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("netsentinel.crawler.fetcher")

from netsentinel.contracts import Config  # noqa: E402
from netsentinel import telemetry  # noqa: E402
from netsentinel.crawler.fetcher import NetworkDisabledError  # noqa: E402
from netsentinel.crawler.redirect import (  # noqa: E402
    _js_location_target,
    _meta_refresh_target,
    chain_summary,
    final_url,
    trace_redirects,
)

LOGGER_NAME = "netsentinel.crawler.redirect"

#: 路由表取值:(HTTP 状态, Location 头, GET 响应体)
Route = tuple[int, str, str]


# ---------------------------------------------------------------------------
# 本地 HTTP 服务(127.0.0.1 随机端口;GET/HEAD 走同一路由)
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """按路由表应答:3xx 附 Location,200 返回 HTML;HEAD 只发头不发体。"""

    def __init__(
        self, *args: object, routes: dict[str, Route] | None = None, **kwargs: object
    ) -> None:
        self._routes: dict[str, Route] = routes or {}
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return  # 静默访问日志,避免污染 pytest 输出

    def do_GET(self) -> None:  # noqa: N802
        self._serve(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(head_only=True)

    def _serve(self, head_only: bool) -> None:
        route = self._routes.get(urllib.parse.urlsplit(self.path).path)
        if route is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status, location, body = route
        data = body.encode("utf-8")
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head_only and data:
            self.wfile.write(data)


class RedirectServer:
    """运行在 127.0.0.1 随机端口的后台线程路由服务。"""

    def __init__(self, routes: dict[str, Route]) -> None:
        self.routes = routes  # 暴露给用例,便于动态补路由(如需要端口号的绝对 Location)
        handler = functools.partial(_Handler, routes=routes)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    @property
    def local_base(self) -> str:
        """同一服务的 localhost 形式(与 127.0.0.1 构成跨 host 跳转,闸门同样放行)。"""
        return f"http://localhost:{self._httpd.server_address[1]}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def _build_routes() -> dict[str, Route]:
    routes: dict[str, Route] = {
        "/hop1": (301, "/hop2", ""),
        "/hop2": (301, "/final.html", ""),
        "/final.html": (200, "", "<html><body><h1>落地页</h1></body></html>"),
        "/meta.html": (
            200,
            "",
            '<html><head><meta http-equiv="refresh" content="0;url=/final.html">'
            "</head></html>",
        ),
        "/js-href.html": (
            200,
            "",
            "<html><script>location.href='/final.html';</script></html>",
        ),
        "/js-replace.html": (
            200,
            "",
            '<html><script>window.location.replace("/final.html");</script></html>',
        ),
        "/js-bad.html": (
            200,
            "",
            "<html><script>location.href='javascript:void(0)';</script></html>",
        ),
        "/plain.html": (200, "", "<html><body><p>普通页面,无跳转</p></body></html>"),
        "/loop-a": (301, "/loop-b", ""),
        "/loop-b": (301, "/loop-a", ""),
        "/self-meta.html": (
            200,
            "",
            '<meta http-equiv="refresh" content="0; url=/self-meta.html">',
        ),
    }
    # 无限前进链:endless/0 → endless/1 → ...(远超任何测试用 max_hops)
    for i in range(64):
        routes[f"/endless/{i}"] = (302, f"/endless/{i + 1}", "")
    return routes


@pytest.fixture()
def server():
    srv = RedirectServer(_build_routes())
    yield srv
    srv.close()


def make_cfg(**overrides: object) -> Config:
    """安全默认配置(禁网、无延迟),按需覆盖字段。"""
    values: dict[str, object] = {
        "allow_network": False,
        "fetch_timeout_s": 5.0,
        "fetch_delay_s": 0.0,  # 测试不等待
        "redirect_max_hops": 5,
    }
    values.update(overrides)
    return Config(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# HTTP 3xx 链
# ---------------------------------------------------------------------------


def test_301_301_200_full_chain(server: RedirectServer) -> None:
    """两级 301 后落地 200:链完整展开(含起点),相对 Location 被绝对化。"""
    start = f"{server.base}/hop1"
    chain = trace_redirects(start, make_cfg())
    assert chain == [start, f"{server.base}/hop2", f"{server.base}/final.html"]
    assert final_url(chain) == f"{server.base}/final.html"


def test_no_redirect_single_element(server: RedirectServer) -> None:
    """200 且无 meta/JS 跳转:链只有起点一个元素。"""
    url = f"{server.base}/plain.html"
    assert trace_redirects(url, make_cfg()) == [url]


# ---------------------------------------------------------------------------
# 页面级跳转(meta refresh / JS location)
# ---------------------------------------------------------------------------


def test_meta_refresh_followed(server: RedirectServer) -> None:
    """meta refresh 页(默认 fetcher 抓取 HTML)→ 跳到目标页。"""
    start = f"{server.base}/meta.html"
    assert trace_redirects(start, make_cfg()) == [start, f"{server.base}/final.html"]


def test_js_location_href_followed(server: RedirectServer) -> None:
    """location.href='...'(单引号)→ 跳。"""
    start = f"{server.base}/js-href.html"
    assert trace_redirects(start, make_cfg()) == [start, f"{server.base}/final.html"]


def test_js_location_replace_followed(server: RedirectServer) -> None:
    """window.location.replace("...")(双引号 + replace)→ 跳。"""
    start = f"{server.base}/js-replace.html"
    assert trace_redirects(start, make_cfg()) == [start, f"{server.base}/final.html"]


def test_js_javascript_scheme_not_followed(server: RedirectServer) -> None:
    """JS 目标为 javascript: 伪协议时不跟进,链停在当前页。"""
    url = f"{server.base}/js-bad.html"
    assert trace_redirects(url, make_cfg()) == [url]


def test_injected_fetch_used_for_html_inspection(server: RedirectServer) -> None:
    """注入的 fetch 被用于 200 页面的 HTML 检查(不必依赖 fetcher)。"""
    start = f"{server.base}/meta.html"
    calls: list[str] = []

    def fake_fetch(url: str, cfg: Config) -> tuple[int, str, str]:
        calls.append(url)
        page = '<meta http-equiv="refresh" content="0;url=/from-fetch.html">'
        return 200, page, url

    chain = trace_redirects(start, make_cfg(), fetch=fake_fetch)
    assert calls == [start]
    assert chain == [start, f"{server.base}/from-fetch.html"]


# ---------------------------------------------------------------------------
# 环路与跳数上限
# ---------------------------------------------------------------------------


def test_loop_truncated_with_warning(server: RedirectServer, caplog: pytest.LogCaptureFixture) -> None:
    """环路 a→b→a:截断为 [a, b] 并记 warning。"""
    a, b = f"{server.base}/loop-a", f"{server.base}/loop-b"
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        chain = trace_redirects(a, make_cfg())
    assert chain == [a, b]
    assert any("环路" in rec.message for rec in caplog.records)


def test_self_referential_meta_loop_truncated(
    server: RedirectServer, caplog: pytest.LogCaptureFixture
) -> None:
    """meta refresh 指向自身:同样按重复 URL 截断。"""
    url = f"{server.base}/self-meta.html"
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        chain = trace_redirects(url, make_cfg())
    assert chain == [url]
    assert any("环路" in rec.message for rec in caplog.records)


@pytest.mark.parametrize(
    "max_hops, expected_len",
    [(3, 4), (1, 2), (0, 1), (40, 41)],
)
def test_max_hops_cap(
    server: RedirectServer,
    caplog: pytest.LogCaptureFixture,
    max_hops: int,
    expected_len: int,
) -> None:
    """无限前进链按 redirect_max_hops 截断:链长 = 跳数 + 1;达上限记 warning。"""
    start = f"{server.base}/endless/0"
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        chain = trace_redirects(start, make_cfg(redirect_max_hops=max_hops))
    assert len(chain) == expected_len
    assert chain[0] == start
    assert chain[-1] == f"{server.base}/endless/{expected_len - 1}"
    if max_hops > 0:
        assert any("上限" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# allow_network 安全闸门
# ---------------------------------------------------------------------------


def test_allow_network_false_rejects_external_before_request() -> None:
    """allow_network=False:外网 URL 在发出请求前被拒(NetworkDisabledError 语义)。"""
    url = "https://example.com/short-link"
    with pytest.raises(NetworkDisabledError) as excinfo:
        trace_redirects(url, make_cfg())  # allow_network=False(安全默认)
    message = str(excinfo.value)
    assert url in message
    assert "allow_network" in message


def test_fetcher_missing_local_gate_same_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetcher 未就位时本地闸门同语义:外网仍被拒(RuntimeError 子类,中文提示)。"""
    monkeypatch.setitem(sys.modules, "netsentinel.crawler.fetcher", None)
    with pytest.raises(RuntimeError, match="allow_network"):
        trace_redirects("https://example.com/x", make_cfg())


def test_fetcher_missing_still_traces_local_3xx(
    server: RedirectServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fetcher 未就位:纯 3xx 链仍可完整追踪(200 页的 HTML 检查优雅跳过)。"""
    monkeypatch.setitem(sys.modules, "netsentinel.crawler.fetcher", None)
    start = f"{server.base}/hop1"
    chain = trace_redirects(start, make_cfg())
    assert chain == [start, f"{server.base}/hop2", f"{server.base}/final.html"]


# ---------------------------------------------------------------------------
# 图谱写入(A46 形态注入;未传/异常均不影响主流程)
# ---------------------------------------------------------------------------


class FakeGraph:
    """记录调用的图谱替身(对齐 A46 EvidenceGraph 的 add_site / add_edge 形态)。"""

    def __init__(self) -> None:
        self.sites: list[str] = []
        self.edges: list[tuple[str, str, str, float]] = []

    def add_site(self, site: str) -> None:
        self.sites.append(site)

    def add_edge(self, src: str, dst: str, kind: str = "?", weight: float = 0.0) -> None:
        self.edges.append((src, dst, kind, weight))


def test_graph_receives_sites_and_redirect_edges(server: RedirectServer) -> None:
    """跨 host 跳转(127.0.0.1 → localhost):登记两个站点节点 + 一条 redirect 边。"""
    # 绝对 Location 需要端口,故动态补路由
    server.routes["/cross-out"] = (301, f"{server.local_base}/final.html", "")
    graph = FakeGraph()
    start = f"{server.base}/cross-out"
    chain = trace_redirects(start, make_cfg(), graph=graph)
    assert chain == [start, f"{server.local_base}/final.html"]
    assert graph.sites == ["127.0.0.1", "localhost"]
    assert graph.edges == [("127.0.0.1", "localhost", "redirect", 1)]


def test_graph_none_is_noop(server: RedirectServer) -> None:
    """graph 缺省 None:无任何效果、不报错(A46 未接线时的惰性行为)。"""
    chain = trace_redirects(f"{server.base}/hop1", make_cfg())
    assert len(chain) == 3


def test_same_host_hop_no_self_edge(server: RedirectServer) -> None:
    """同 host 的路径级跳转:每个新 URL 到达都登记一次站点,但不产生自环边。"""
    graph = FakeGraph()
    start = f"{server.base}/hop1"
    chain = trace_redirects(start, make_cfg(), graph=graph)
    assert len(chain) == 3
    assert graph.sites == ["127.0.0.1"] * len(chain)  # 去重由图谱节点主键负责
    assert graph.edges == []


class ExplodingGraph:
    """所有写入都失败的图谱替身:验证容错(绝不影响链追踪)。"""

    def add_site(self, site: str) -> None:
        raise RuntimeError(f"模拟图谱写失败:{site}")

    def add_edge(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("模拟图谱写失败")


def test_graph_failures_are_tolerated(server: RedirectServer) -> None:
    """图谱写入抛异常:链追踪照常完成。"""
    chain = trace_redirects(f"{server.base}/hop1", make_cfg(), graph=ExplodingGraph())
    assert chain == [
        f"{server.base}/hop1",
        f"{server.base}/hop2",
        f"{server.base}/final.html",
    ]


# ---------------------------------------------------------------------------
# final_url / chain_summary
# ---------------------------------------------------------------------------


def test_final_url_and_chain_summary() -> None:
    """final_url 取末元素(空链中文报错);chain_summary 为中文"共 N 跳:A → B → C"。"""
    chain = ["http://a.test/s", "http://b.test/m", "http://c.test/f"]
    assert final_url(chain) == "http://c.test/f"
    summary = chain_summary(chain)
    assert summary.startswith("共 2 跳")
    assert "http://a.test/s → http://b.test/m → http://c.test/f" in summary
    assert chain_summary([chain[0]]) == "共 0 跳:http://a.test/s"
    with pytest.raises(ValueError, match="为空"):
        final_url([])


# ---------------------------------------------------------------------------
# 解析辅助函数(meta refresh / JS location 的写法兼容)
# ---------------------------------------------------------------------------


def test_meta_refresh_variants() -> None:
    """属性顺序颠倒、单双引号、大小写、延迟秒数、url= 带引号均能识别。"""
    cases = [
        '<meta http-equiv="refresh" content="0;url=/a">',
        "<meta http-equiv='refresh' content='5; URL=\"/b\"'>",
        "<META HTTP-EQUIV='Refresh' CONTENT=\"0; url='/c'\">",
        '<meta content="0;url=/d" http-equiv="refresh">',
        '<meta name="desc" content="x"><meta http-equiv="refresh" content="0;url=/e">',
    ]
    assert [_meta_refresh_target(c) for c in cases] == ["/a", "/b", "/c", "/d", "/e"]


def test_meta_refresh_negative() -> None:
    """非 refresh 的 meta、只有延迟秒数、无 meta 均视为不跳转。"""
    assert _meta_refresh_target('<meta http-equiv="content-type" content="text/html">') is None
    assert _meta_refresh_target('<meta http-equiv="refresh" content="30">') is None
    assert _meta_refresh_target("<p>没有 meta</p>") is None


def test_js_location_variants() -> None:
    """location.href / location.replace,单双引号与大小写都认。"""
    assert _js_location_target("location.href='/a'") == "/a"
    assert _js_location_target('window.location.href = "/b";') == "/b"
    assert _js_location_target("location.replace('/c')") == "/c"
    assert _js_location_target('LOCATION.REPLACE("/d")') == "/d"
    assert _js_location_target("var x = 1;") is None


# ---------------------------------------------------------------------------
# V5 升级:每跳计时与链长遥测(redirect.hop / redirect.chain / redirect.hops)
# ---------------------------------------------------------------------------


def test_v5_hop_timer_records_each_probe(server: RedirectServer) -> None:
    """两级 301 后落地:共 3 次 hop 探测,各记一条 redirect.hop 计时样本。"""
    telemetry.reset()
    start = f"{server.base}/hop1"
    chain = trace_redirects(start, make_cfg())
    assert chain == [start, f"{server.base}/hop2", f"{server.base}/final.html"]

    snapshot = telemetry.snapshot()
    assert snapshot["timers"]["redirect.hop"]["count"] == 3  # 每跳探测一次
    assert snapshot["counters"]["redirect.chain"] == 1
    assert snapshot["counters"]["redirect.hops"] == 2


def test_v5_chain_and_hops_accumulate(server: RedirectServer) -> None:
    """链计数与跳数累加:两次追踪(2 跳 + 0 跳)合计 chain=2、hops=2。"""
    telemetry.reset()
    trace_redirects(f"{server.base}/hop1", make_cfg())
    trace_redirects(f"{server.base}/plain.html", make_cfg())
    counters = telemetry.snapshot()["counters"]
    assert counters["redirect.chain"] == 2
    assert counters["redirect.hops"] == 2


def test_v5_loop_chain_still_records_hop_stats(server: RedirectServer) -> None:
    """环路截断的链同样完成遥测登记(1 次探测后截断,跳数为 1)。"""
    telemetry.reset()
    a = f"{server.base}/loop-a"
    chain = trace_redirects(a, make_cfg())
    assert chain == [a, f"{server.base}/loop-b"]
    snapshot = telemetry.snapshot()
    assert snapshot["timers"]["redirect.hop"]["count"] == 2  # loop-a 与 loop-b 各探测一次
    assert snapshot["counters"]["redirect.chain"] == 1
    assert snapshot["counters"]["redirect.hops"] == 1
