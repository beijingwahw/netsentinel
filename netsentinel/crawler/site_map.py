"""站点链接发现(同 host 广度优先 BFS)—— A04。

职责:从起点 URL 出发,借助可注入的 fetch_page 抓取页面,用标准库
html.parser 提取 ``<a href>``,归一化过滤后做同 host 的广度优先遍历,
产出待抽样页面 URL 列表(供 pipeline.orchestrator 逐页 capture)。

用法示例(离线注入 fake 抓取函数)::

    from netsentinel.contracts import Config
    from netsentinel.crawler.site_map import discover_links

    pages = {"http://site.test/": (200, '<a href="/about">关于</a>')}
    def fake_fetch(url, cfg):
        status, html = pages.get(url, (404, ""))
        return status, html, url

    discover_links("http://site.test/", Config(max_pages=3, fetch_delay_s=0),
                   fetch_page=fake_fetch)
    # -> ['http://site.test/', 'http://site.test/about']

设计要点:
- 本模块自身不发起任何网络请求;默认抓取函数惰性导入
  ``netsentinel.crawler.fetcher.fetch_page``(A02 提供),未就位时抛中文 RuntimeError。
- 单页失败(非 200 / 空 HTML / 抓取异常)只记 debug 日志并继续,不影响其他分支。
- 每次成功抓取后按 ``cfg.fetch_delay_s`` 休眠(礼貌抓取);测试注入 fake 时可置 0。
- BFS 的"已抓/已入队"合并为同一个 seen 集合判定:链接在入队前即去重,
  任何 URL 至多入队一次、至多抓取一次(与结果列表共用同一去重依据)。
- 遥测(V5):新链接入队计 ``sitemap.discovered``;单页抓取失败/不可用
  计 ``sitemap.errors``。
- 仅使用 Python 标准库。
"""
from __future__ import annotations

import importlib
import logging
import time
from collections import deque
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

from netsentinel import telemetry
from netsentinel.contracts import Config

__all__ = ["discover_links"]

logger = logging.getLogger(__name__)

# 可注入的抓取函数签名:fetch_page(url, cfg) -> (status, html, final_url)
FetchPageFn = Callable[[str, Config], tuple[int, str, str]]

_FETCHER_MODULE = "netsentinel.crawler.fetcher"


class _AnchorExtractor(HTMLParser):
    """按文档顺序收集页面中所有 ``<a href="...">`` 的值(标准库 html.parser)。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # HTMLParser 已将标签名/属性名转为小写;自闭合 <a/> 会经由
        # 默认 handle_startendtag 转调本方法。
        if tag != "a":
            return
        for attr_name, attr_value in attrs:
            if attr_name == "href" and attr_value and attr_value.strip():
                self.hrefs.append(attr_value.strip())


def _extract_hrefs(html: str) -> list[str]:
    """从 HTML 中提取 <a href> 列表;解析中断时保留已提取部分。"""
    parser = _AnchorExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        logger.debug("HTML 解析中断,使用已提取到的链接", exc_info=True)
    return parser.hrefs


def _strip_fragment(url: str) -> str:
    """去掉 URL 的 # 片段,其余部分保持原样。"""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _same_site_link(raw_href: str, page_url: str, start_host: str) -> str | None:
    """把 href 相对 page_url 绝对化并过滤;不合规返回 None。

    规则:scheme 必须是 http/https;hostname 与起点相同(大小写不敏感);去掉片段。
    单遍解析(V5):直接用已拆出的 parts 重组去片段 URL,避免二次 urlsplit。
    """
    absolute = urljoin(page_url, raw_href)
    parts = urlsplit(absolute)
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if not host or host != start_host:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _load_default_fetcher() -> FetchPageFn:
    """惰性加载 A02 的 fetcher.fetch_page;未就位时抛中文 RuntimeError。"""
    try:
        module = importlib.import_module(_FETCHER_MODULE)
    except ImportError as exc:
        raise RuntimeError(
            f"fetcher 模块未就位:无法导入 {_FETCHER_MODULE}(A02 负责的抓取模块)"
        ) from exc
    fetch = getattr(module, "fetch_page", None)
    if not callable(fetch):
        raise RuntimeError(
            f"fetcher 模块未就位:{_FETCHER_MODULE} 中缺少 fetch_page 函数"
        )
    return fetch


def discover_links(
    start_url: str,
    cfg: Config,
    fetch_page: FetchPageFn | None = None,
) -> list[str]:
    """从 start_url 出发做同 host 广度优先链接发现。

    - 返回按 BFS 顺序排列的成功页面 URL 列表(含起点),长度 <= cfg.max_pages;
    - 只解析 status==200 且 html 非空(纯空白视为空)的页面;
    - 仅保留 http/https 且与起点 hostname 相同(大小写不敏感)的链接,去片段,按完整 URL 去重;
    - 单页失败(含抓取异常)记 debug 日志后继续,不占用成功页配额;
    - 每次成功抓取后按 cfg.fetch_delay_s 休眠(测试可传 0);
    - 若没有任何页面成功,至少返回 ``[start_url]``;
    - 遥测(V5):每个通过过滤且未见过的新链接入队时计
      ``sitemap.discovered``;单页抓取异常或不可用(非 200 / 空 HTML)
      计 ``sitemap.errors``。
    """
    fetch = fetch_page if fetch_page is not None else _load_default_fetcher()

    start_host = (urlsplit(start_url).hostname or "").lower()
    origin = _strip_fragment(start_url)

    seen: set[str] = {origin}
    queue: deque[str] = deque([origin])
    result: list[str] = []

    while queue and len(result) < cfg.max_pages:
        url = queue.popleft()
        try:
            status, html, final_url = fetch(url, cfg)
        except Exception as exc:  # 单页异常不终止巡爬
            telemetry.inc("sitemap.errors")
            logger.debug("页面抓取异常,跳过:%s(%s)", url, exc)
            continue

        if status != 200 or not html.strip():
            telemetry.inc("sitemap.errors")
            logger.debug(
                "页面不可用(status=%s, html_len=%d),跳过:%s", status, len(html), url
            )
            continue

        result.append(url)
        logger.debug("页面收录:%s(final_url=%s)", url, final_url)

        # 以最终 URL(重定向后)为基准解析相对链接;seen 同时承担
        # "已抓 / 已入队"两类判定,链接入队前即完成去重
        base = final_url or url
        for raw_href in _extract_hrefs(html):
            link = _same_site_link(raw_href, base, start_host)
            if link is None or link in seen:
                continue
            seen.add(link)
            queue.append(link)
            telemetry.inc("sitemap.discovered")

        time.sleep(max(0.0, float(cfg.fetch_delay_s)))  # 礼貌休眠;测试传 0

    if not result:
        logger.debug("没有任何页面抓取成功,至少返回起点:%s", start_url)
        return [start_url]

    logger.info(
        "链接发现完成:起点 %s,成功收录 %d 页(上限 %d)",
        start_url,
        len(result),
        cfg.max_pages,
    )
    return result
