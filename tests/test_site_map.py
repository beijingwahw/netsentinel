"""site_map.discover_links 的离线单元测试 —— A04。

全部用例通过注入 fake fetch_page(闭包构造 HTML)离线运行,绝不发起真实网络请求;
cfg.fetch_delay_s=0,测试不产生真实休眠。
"""
from __future__ import annotations

import logging
import sys

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.crawler.site_map import discover_links

START = "http://site.test/"

# 两层小站点:起点 → a/b/c.html → a1/a2/b1
PAGES_TREE: dict[str, tuple[int, str] | Exception] = {
    START: (200, '<html><a href="/a">A</a><a href="/b">B</a><a href="c.html">C</a></html>'),
    "http://site.test/a": (200, '<a href="/a1">A1</a><a href="/a2">A2</a>'),
    "http://site.test/b": (200, '<a href="/b1">B1</a>'),
    "http://site.test/c.html": (200, "<p>没有链接</p>"),
    "http://site.test/a1": (200, "<p>a1</p>"),
    "http://site.test/a2": (200, "<p>a2</p>"),
    "http://site.test/b1": (200, "<p>b1</p>"),
}

EXPECTED_FULL = [
    START,
    "http://site.test/a",
    "http://site.test/b",
    "http://site.test/c.html",
    "http://site.test/a1",
    "http://site.test/a2",
    "http://site.test/b1",
]


def make_cfg(max_pages: int = 10) -> Config:
    """测试配置:fetch_delay_s=0,避免真实休眠。"""
    return Config(max_pages=max_pages, fetch_delay_s=0)


def make_fetch(pages: dict[str, tuple[int, str] | Exception]):
    """构造可注入的 fake fetch_page,并记录调用顺序。

    pages 取值: (status, html) 正常返回;Exception 模拟抓取异常;
    未登记的 url 一律返回 404(顺便覆盖未知页面的健壮性)。
    """
    calls: list[str] = []

    def fake_fetch(url: str, cfg: Config) -> tuple[int, str, str]:
        calls.append(url)
        entry = pages.get(url)
        if isinstance(entry, Exception):
            raise entry
        if entry is None:
            return 404, "", url
        status, html = entry
        return status, html, url

    return fake_fetch, calls


def test_bfs_two_level_order() -> None:
    """两层结构按 BFS 顺序收录:起点 → 第一层(a, b, c.html)→ 第二层(a1, a2, b1)。"""
    fetch, calls = make_fetch(PAGES_TREE)
    result = discover_links(START, make_cfg(max_pages=10), fetch_page=fetch)
    assert result == EXPECTED_FULL
    # 抓取调用顺序同样是 BFS,且每个页面只抓一次
    assert calls == EXPECTED_FULL


@pytest.mark.parametrize(
    "max_pages,expected",
    [
        (1, [START]),
        (2, [START, "http://site.test/a"]),
        (3, [START, "http://site.test/a", "http://site.test/b"]),
        (7, EXPECTED_FULL),
    ],
)
def test_max_pages_cap(max_pages: int, expected: list[str]) -> None:
    """返回列表长度 <= max_pages(含起点),达到上限后不再抓取。"""
    fetch, calls = make_fetch(PAGES_TREE)
    result = discover_links(START, make_cfg(max_pages=max_pages), fetch_page=fetch)
    assert result == expected
    assert len(result) <= max_pages
    assert calls == expected  # 上限之外的页面没有被请求


def test_link_filtering() -> None:
    """外 host / 子域 / 非 http(s) / javascript: / mailto: / 锚点被过滤;片段被去掉;<img> 不算链接。"""
    pages: dict[str, tuple[int, str] | Exception] = {
        START: (
            200,
            "\n".join(
                [
                    '<a href="http://other.example/foreign">外站</a>',
                    '<a href="//cdn.site.test/static">子域算外 host</a>',
                    '<a href="ftp://site.test/file.zip">非 http(s)</a>',
                    '<a href="javascript:alert(1)">脚本</a>',
                    '<a href="mailto:report@site.test">邮件</a>',
                    '<a href="#top">同页锚点</a>',
                    '<a href="/keep#section">带片段</a>',
                    '<a href="http://SITE.test/keep2">host 大小写不敏感</a>',
                    '<img src="/logo.png" />',
                    '<a href="/ok">正常</a>',
                ]
            ),
        ),
        "http://site.test/keep": (200, "<p>keep</p>"),
        "http://SITE.test/keep2": (200, "<p>keep2</p>"),
        "http://site.test/ok": (200, "<p>ok</p>"),
    }
    fetch, calls = make_fetch(pages)
    result = discover_links(START, make_cfg(max_pages=10), fetch_page=fetch)

    assert result[0] == START
    assert "http://site.test/keep" in result  # 片段 #section 已去掉
    assert "http://SITE.test/keep2" in result  # hostname 大小写不敏感,应保留
    assert "http://site.test/ok" in result

    joined = " ".join(result)
    assert "#" not in joined  # 任何结果 URL 都不带片段
    for bad in (
        "other.example",
        "cdn.site.test",
        "ftp://",
        "javascript:",
        "mailto:",
        "logo.png",  # <img src> 不应被当作链接
    ):
        assert bad not in joined, f"不应出现:{bad}"

    # 外 host 链接根本没有被抓取;锚点归一后即起点本身,不重复出现
    assert "http://other.example/foreign" not in calls
    assert result.count(START) == 1


def test_failed_pages_skipped_others_continue(caplog) -> None:
    """404 / 空 html / 抓取异常的页面被跳过,其余分支继续;失败页的链接不采纳,并记 debug 日志。"""
    pages: dict[str, tuple[int, str] | Exception] = {
        START: (
            200,
            '<a href="/bad404"></a><a href="/boom"></a><a href="/empty"></a>'
            '<a href="/blank"></a><a href="/good"></a>',
        ),
        # 404 但带 html:非 200 即使有内容也不解析其链接
        "http://site.test/bad404": (404, '<a href="/never1"></a>'),
        "http://site.test/boom": RuntimeError("模拟网络故障"),
        "http://site.test/empty": (200, ""),
        "http://site.test/blank": (200, "   \n\t "),
        "http://site.test/good": (200, '<a href="/good2"></a>'),
        "http://site.test/good2": (200, "<p>good2</p>"),
    }
    fetch, calls = make_fetch(pages)
    with caplog.at_level(logging.DEBUG, logger="netsentinel.crawler.site_map"):
        result = discover_links(START, make_cfg(max_pages=10), fetch_page=fetch)

    assert result == [START, "http://site.test/good", "http://site.test/good2"]
    # BFS 队列仍按顺序尝试每个分支,失败页占位但不进结果
    assert calls == [
        START,
        "http://site.test/bad404",
        "http://site.test/boom",
        "http://site.test/empty",
        "http://site.test/blank",
        "http://site.test/good",
        "http://site.test/good2",
    ]
    # 404 页面里的链接没有被提取/抓取
    assert "http://site.test/never1" not in calls
    # 失败被记为 debug 日志
    assert any("跳过" in rec.message for rec in caplog.records)


@pytest.mark.parametrize(
    "entry",
    [(500, ""), (404, "<html>gone</html>"), (200, "")],
)
def test_all_failed_returns_start(entry: tuple[int, str]) -> None:
    """起点(及一切页面)全部失败时,至少返回 [start_url]。"""
    fetch, calls = make_fetch({START: entry})
    result = discover_links(START, make_cfg(max_pages=5), fetch_page=fetch)
    assert result == [START]
    assert calls == [START]


def test_unknown_pages_default_404_returns_start() -> None:
    """fake 未登记任何页面(全部 404)时同样返回 [start_url]。"""
    fetch, calls = make_fetch({})
    result = discover_links(START, make_cfg(max_pages=5), fetch_page=fetch)
    assert result == [START]
    assert calls == [START]


def test_duplicate_links_fetched_once() -> None:
    """同一目标的多种写法(绝对/相对/带重复)归一去重后只抓一次。"""
    pages: dict[str, tuple[int, str] | Exception] = {
        START: (
            200,
            '<a href="/dup"></a><a href="/dup"></a>'
            '<a href="dup"></a><a href="http://site.test/dup"></a>',
        ),
        "http://site.test/dup": (200, "<p>dup</p>"),
    }
    fetch, calls = make_fetch(pages)
    result = discover_links(START, make_cfg(), fetch_page=fetch)
    assert result == [START, "http://site.test/dup"]
    assert calls.count("http://site.test/dup") == 1


def test_default_fetcher_not_ready(monkeypatch) -> None:
    """未注入 fetch_page 且 fetcher 模块不可导入时,抛中文 RuntimeError。"""
    # 强制 importlib 视该模块为已屏蔽(None 即 ImportError),无论 A02 文件是否就位
    monkeypatch.setitem(sys.modules, "netsentinel.crawler.fetcher", None)
    with pytest.raises(RuntimeError, match="fetcher 模块未就位"):
        discover_links(START, make_cfg())


# ---------------------------------------------------------------------------
# V5 升级:入队去重合并判定 / sitemap.discovered 与 sitemap.errors 遥测
# ---------------------------------------------------------------------------


def test_v5_seen_merges_fetched_and_queued() -> None:
    """已抓/已入队合并判定:多个页面先后发现同一链接,该链接只入队/抓取一次。"""
    pages: dict[str, tuple[int, str] | Exception] = {
        START: (200, '<a href="/a"></a><a href="/b"></a>'),
        "http://site.test/a": (200, '<a href="/shared"></a><a href="/shared"></a>'),
        "http://site.test/b": (200, '<a href="/shared"></a>'),
        "http://site.test/shared": (200, "<p>shared</p>"),
    }
    fetch, calls = make_fetch(pages)
    result = discover_links(START, make_cfg(max_pages=10), fetch_page=fetch)

    assert result == [START, "http://site.test/a", "http://site.test/b", "http://site.test/shared"]
    assert calls.count("http://site.test/shared") == 1  # 入队前即去重,只抓一次
    # 起点 #锚点 归一后即自身,不重复入队
    pages[START] = (200, '<a href="#top"></a><a href="/a"></a>')
    fetch2, calls2 = make_fetch(pages)
    result2 = discover_links(START, make_cfg(max_pages=10), fetch_page=fetch2)
    assert calls2.count(START) == 1
    assert result2.count(START) == 1


def test_v5_discovered_telemetry_counts_unique_enqueue() -> None:
    """每个通过过滤且未见过的新链接入队时计一次 sitemap.discovered。"""
    telemetry.reset()
    fetch, _calls = make_fetch(PAGES_TREE)
    result = discover_links(START, make_cfg(max_pages=10), fetch_page=fetch)
    assert result == EXPECTED_FULL
    # 新链接共 6 个:a, b, c.html, a1, a2, b1(起点不计,重复发现不计)
    assert telemetry.snapshot()["counters"]["sitemap.discovered"] == 6


def test_v5_fetch_failures_counted() -> None:
    """抓取异常与非 200/空 HTML 均计入 sitemap.errors;成功页不计。"""
    telemetry.reset()
    pages: dict[str, tuple[int, str] | Exception] = {
        START: (200, '<a href="/x404"></a><a href="/xboom"></a><a href="/xempty"></a><a href="/ok"></a>'),
        "http://site.test/x404": (404, "<html>gone</html>"),
        "http://site.test/xboom": RuntimeError("模拟网络故障"),
        "http://site.test/xempty": (200, ""),
        "http://site.test/ok": (200, "<p>ok</p>"),
    }
    fetch, _calls = make_fetch(pages)
    discover_links(START, make_cfg(max_pages=10), fetch_page=fetch)
    assert telemetry.snapshot()["counters"]["sitemap.errors"] == 3
