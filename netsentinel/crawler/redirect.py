"""重定向链追踪(A47)。

色情/违法站点常借短链服务、跳板域名与多级 302 隐藏真实落地页;本模块把
一条重定向链逐跳展开,返回完整访问序列(含起点),供证据图谱(intel.graph)
与 URL 情报分析使用。

每跳判定顺序(至多一次 HEAD 请求 + 至多一次页面抓取):
1. 对当前 URL 发出**不自动跟随重定向**的 HEAD 请求(:func:`_head`),取
   status 与 Location 头——``fetcher.fetch_page`` 在 3xx 时响应体为空,
   拿不到 Location,故本模块自带不跟随的探测;
2. ``3xx + Location`` → ``urljoin`` 绝对化后进入下一跳;
3. ``2xx`` → 抓取页面 HTML,检查 ``<meta http-equiv="refresh">`` 与顶层
   ``location.href=...`` / ``location.replace(...)`` 跳转,命中则继续;
4. 其余情况(无 Location 的 3xx、4xx/5xx、连接失败、无可识别跳转)终止。

安全与健壮性:
- HEAD 与页面抓取走同一 ``allow_network`` 闸门:惰性复用
  ``crawler.fetcher`` 的闸门函数与 ``NetworkDisabledError``(importlib
  导入,不复制语义);fetcher 未就位时本模块实现同语义的本地闸门。
  策略性拦截向上冒泡,绝不在中途吞掉;
- 跳数(链内相邻跳转次数)不超过 ``cfg.redirect_max_hops``;出现重复
  URL(环路)立即截断并告警;
- 每到达一个新 URL 时向可选注入的图谱对象(A46 EvidenceGraph 形态,
  ``graph=None`` 时无任何效果)登记站点节点与 redirect 边,登记过程
  try/except 全容错,图谱缺失或抛错都不影响链追踪主流程;
- 页面抓取(含注入的 fetch)失败时链在当前 URL 优雅终止,不抛出;
- 遥测(V5):每一跳探测计 ``telemetry.timer("redirect.hop")``;每条链
  完成时计 ``redirect.chain`` 一次,并把链内跳数累加进 ``redirect.hops``;
- 仅使用 Python 标准库。

用法示例::

    from netsentinel.contracts import Config
    from netsentinel.crawler.redirect import trace_redirects, final_url

    chain = trace_redirects("http://127.0.0.1:8000/hop1", Config())
    final_url(chain)  # 重定向链的最终落地页
"""
from __future__ import annotations

import html as _html
import importlib
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from netsentinel import telemetry
from netsentinel.contracts import Config

__all__ = ["trace_redirects", "final_url", "chain_summary"]

logger = logging.getLogger(__name__)

#: 可注入的页面抓取函数签名:fetch(url, cfg) -> (status, html, final_url)
FetchPageFn = Callable[[str, Config], tuple[int, str, str]]

_FETCHER_MODULE = "netsentinel.crawler.fetcher"

#: 默认策略下放行的本机 host(与 fetcher 同语义)
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})

#: fetcher 缺失时的本地兜底 User-Agent(正常情况惰性复用 fetcher.USER_AGENT)
_FALLBACK_USER_AGENT = "NetSentinel/0.1"

#: <meta ...> 标签及其 http-equiv/content 属性的识别(允许任意属性顺序/引号风格)
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_META_HTTP_EQUIV_RE = re.compile(r"""http-equiv\s*=\s*['"]?\s*refresh\b""", re.IGNORECASE)
_META_CONTENT_RE = re.compile(r"""content\s*=\s*(['"])(.*?)\1""", re.IGNORECASE | re.DOTALL)

#: 顶层 JS 跳转:location.href="..." / location.replace('...')(双单引号都认)
_JS_LOCATION_RE = re.compile(
    r"""(?:(?:window|document)\.)?location\.(?:href\s*=|replace\s*\()\s*(['"])([^'"]+)\1""",
    re.IGNORECASE,
)

#: 图谱写边时依次尝试的方法名(A46 契约未固定公开写边方法,按常见形态容错)
_EDGE_METHOD_NAMES = ("add_edge", "link", "connect")


# ---------------------------------------------------------------------------
# fetcher 惰性复用(闸门 / 异常 / UA / 默认抓取)
# ---------------------------------------------------------------------------


def _load_fetcher_module() -> Any | None:
    """惰性导入 A02 的 fetcher 模块;未就位时返回 None(不抛出)。"""
    try:
        return importlib.import_module(_FETCHER_MODULE)
    except ImportError:
        logger.debug("fetcher 模块未就位,redirect 使用本地同语义实现:%s", _FETCHER_MODULE)
        return None


def _load_default_fetcher() -> FetchPageFn:
    """惰性加载 fetcher.fetch_page;未就位时抛中文 RuntimeError(同 site_map)。"""
    module = _load_fetcher_module()
    if module is None:
        raise RuntimeError(
            f"fetcher 模块未就位:无法导入 {_FETCHER_MODULE}(A02 负责的抓取模块)"
        )
    fetch = getattr(module, "fetch_page", None)
    if not callable(fetch):
        raise RuntimeError(f"fetcher 模块未就位:{_FETCHER_MODULE} 中缺少 fetch_page 函数")
    return fetch


class _NetworkDisabledFallback(RuntimeError):
    """fetcher 缺失时的本地同语义兜底异常(仅离线开发环境可能出现)。"""


def _network_disabled_error_type() -> type[BaseException]:
    """优先复用 fetcher.NetworkDisabledError;缺失时用本地兜底异常。"""
    module = _load_fetcher_module()
    exc_type = getattr(module, "NetworkDisabledError", None) if module is not None else None
    if isinstance(exc_type, type) and issubclass(exc_type, BaseException):
        return exc_type
    return _NetworkDisabledFallback


def _gate_network_access(url: str, cfg: Config) -> None:
    """allow_network 安全闸门:优先复用 fetcher 的闸门判断(importlib,不复制)。

    fetcher 未就位时,本模块实现同语义:仅 127.0.0.1 / localhost 放行,
    其余在发出请求前抛 NetworkDisabledError(或本地兜底异常)。
    """
    module = _load_fetcher_module()
    gate = getattr(module, "_gate_network_access", None) if module is not None else None
    if callable(gate):
        gate(url, cfg)
        return
    host = _url_host(url)
    if not (cfg.allow_network or host in _LOCAL_HOSTS):
        raise _network_disabled_error_type()(
            f"默认安全策略禁止访问非本机地址:已拦截 {url}(host={host or '未知'})。"
            "如确需追踪该真实站点的重定向链,请在配置中将 allow_network 设为 True 后重试。"
        )


def _user_agent() -> str:
    """对外 UA:惰性复用 fetcher.USER_AGENT,缺失时用契约固定值兜底。"""
    module = _load_fetcher_module()
    ua = getattr(module, "USER_AGENT", None) if module is not None else None
    return ua if isinstance(ua, str) and ua else _FALLBACK_USER_AGENT


# ---------------------------------------------------------------------------
# 不跟随重定向的 HEAD 探测
# ---------------------------------------------------------------------------


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁用 urllib 的自动跟随:让 3xx 以 HTTPError 形态露出 status+Location。"""

    def redirect_request(  # noqa: D102
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        return None


_NO_REDIRECT_OPENER: urllib.request.OpenerDirector | None = None


def _no_redirect_opener() -> urllib.request.OpenerDirector:
    """构建(并缓存)不自动跟随重定向的 opener。"""
    global _NO_REDIRECT_OPENER
    if _NO_REDIRECT_OPENER is None:
        _NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)
    return _NO_REDIRECT_OPENER


def _head(url: str, cfg: Config) -> tuple[int, str]:
    """对 url 发出不跟随重定向的 HEAD 请求,返回 ``(status, Location 头)``。

    - 与 fetch_page 走同一 allow_network 闸门(策略拦截向上冒泡);
    - 3xx 因禁用自动跟随而以 HTTPError 返回:取出 code 与 Location,不抛出;
    - 连接层失败(URLError)返回 ``(0, "")``;其余意外异常原样抛出。
    """
    _gate_network_access(url, cfg)
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": _user_agent()})
    try:
        with _no_redirect_opener().open(request, timeout=cfg.fetch_timeout_s) as resp:
            status = int(getattr(resp, "status", 0) or 0)
            location = resp.headers.get("Location") or ""
            return status, location
    except urllib.error.HTTPError as exc:
        headers = getattr(exc, "headers", None) or getattr(exc, "hdrs", None)
        location = (headers.get("Location") or "") if headers is not None else ""
        return int(exc.code), location
    except urllib.error.URLError as exc:
        logger.warning("HEAD 请求连接失败,重定向链在此终止:%s(%s)", url, exc)
        return 0, ""


# ---------------------------------------------------------------------------
# 页面级跳转解析(meta refresh / JS location)
# ---------------------------------------------------------------------------


def _meta_refresh_target(page_html: str) -> str | None:
    """从 HTML 中提取 meta refresh 的目标 URL;无则返回 None。

    兼容属性任意顺序、单双引号、大小写、``0;url=...`` / ``5; URL='...'`` 等
    常见写法;content 只有延迟秒数(无 url=)视为不跳转。
    """
    for tag_match in _META_TAG_RE.finditer(page_html):
        tag = tag_match.group(0)
        if not _META_HTTP_EQUIV_RE.search(tag):
            continue
        content_match = _META_CONTENT_RE.search(tag)
        if content_match is None:
            continue
        value = content_match.group(2).strip()
        parts = value.split(";", 1)
        if len(parts) != 2:
            continue
        target = parts[1].strip()
        if target[:4].lower() != "url=":
            continue
        target = target[4:].strip().strip("'\"")
        if target:
            return _html.unescape(target)
    return None


def _js_location_target(page_html: str) -> str | None:
    """从 HTML 中提取顶层 JS 跳转目标(``location.href=`` / ``location.replace()``)。"""
    match = _JS_LOCATION_RE.search(page_html)
    if match is None:
        return None
    target = match.group(2).strip()
    return _html.unescape(target) if target else None


# ---------------------------------------------------------------------------
# 跳跳链核心
# ---------------------------------------------------------------------------


def _url_host(url: str) -> str:
    """解析 URL 的 hostname(小写;解析失败返回空串)。"""
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _normalize_url(url: str) -> str:
    """去掉 URL 的 # 片段,其余保持原样(用于链内 URL 去重/环路判定)。"""
    parts = urllib.parse.urlsplit(url.strip())
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _http_target(url: str, raw_target: str) -> str | None:
    """把跳转目标相对 url 绝对化并去片段;非 http(s) 目标返回 None(不跟进)。"""
    try:
        absolute = _normalize_url(urllib.parse.urljoin(url, raw_target.strip()))
    except ValueError:
        logger.debug("跳转目标无法解析为 URL,不跟进:%r(来自 %s)", raw_target, url)
        return None
    if urllib.parse.urlsplit(absolute).scheme in ("http", "https"):
        return absolute
    logger.debug("跳转目标非 http(s),不跟进:%r(来自 %s)", raw_target, url)
    return None


def _fetch_html(get_fetch: Callable[[], FetchPageFn], url: str, cfg: Config) -> str:
    """抓取页面 HTML:策略性拦截(NetworkDisabled 语义)向上冒泡,普通失败返回空串。"""
    try:
        _status, page_html, _final = get_fetch()(url, cfg)
        return page_html or ""
    except Exception as exc:
        if isinstance(exc, _network_disabled_error_type()):
            raise
        logger.debug("页面抓取失败,重定向链在当前 URL 终止:%s(%s)", url, exc)
        return ""


def _next_hop(
    url: str, cfg: Config, get_fetch: Callable[[], FetchPageFn]
) -> tuple[str | None, str]:
    """探测 url 的下一跳,返回 ``(下一跳 URL 或 None, 判定依据)``。"""
    status, location = _head(url, cfg)
    if 300 <= status < 400:
        if location.strip():
            target = _http_target(url, location)
            if target is not None:
                return target, f"HTTP {status} Location"
        logger.debug("HTTP %s 未提供可跟进的 Location 头,链终止:%s", status, url)
        return None, ""
    if 200 <= status < 300:
        page_html = _fetch_html(get_fetch, url, cfg)
        if page_html.strip():
            meta_target = _meta_refresh_target(page_html)
            if meta_target is not None:
                target = _http_target(url, meta_target)
                if target is not None:
                    return target, "meta refresh"
            js_target = _js_location_target(page_html)
            if js_target is not None:
                target = _http_target(url, js_target)
                if target is not None:
                    return target, "JS location"
        return None, ""
    logger.debug("HEAD 状态 %s 不可跟进,链终止:%s", status, url)
    return None, ""


# ---------------------------------------------------------------------------
# 图谱写入(A46 EvidenceGraph 形态,可选注入,全程容错)
# ---------------------------------------------------------------------------


def _graph_add_site(graph: Any, url: str, host: str) -> None:
    """登记站点节点:约定按 host 归一;签名不符时退回完整 URL 重试(容错)。"""
    add_site = getattr(graph, "add_site", None)
    if not callable(add_site):
        logger.debug("图谱对象缺少 add_site 方法,跳过站点登记:%r", type(graph).__name__)
        return
    try:
        add_site(host)
        return
    except TypeError:
        logger.debug("add_site(host) 签名不符,改用完整 URL 重试", exc_info=True)
    except Exception:
        logger.debug("图谱 add_site 调用失败,已容错跳过(host=%s)", host, exc_info=True)
        return
    try:
        add_site(url)  # 兼容 A46 契约 add_site(url) 形态
    except Exception:
        logger.debug("图谱 add_site(url) 亦失败,已容错跳过(url=%s)", url, exc_info=True)


def _try_edge_call(method: Callable[..., Any], src: str, dst: str) -> bool:
    """按 kwarg/位置参数等常见签名尝试写边;任一成功返回 True。"""
    attempts = (
        lambda: method(src, dst, kind="redirect", weight=1),
        lambda: method(src, dst, "redirect", 1),
        lambda: method(src, dst, "redirect"),
        lambda: method(src, dst),
    )
    for attempt in attempts:
        try:
            attempt()
            return True
        except TypeError:
            continue
        except Exception:
            logger.debug("图谱 redirect 边写入失败,已容错跳过", exc_info=True)
            return False
    return False


def _graph_add_redirect_edge(graph: Any, src_host: str, dst_host: str) -> None:
    """补一条 redirect 边;方法名/签名按常见形态逐个尝试(容错)。"""
    for name in _EDGE_METHOD_NAMES:
        method = getattr(graph, name, None)
        if callable(method) and _try_edge_call(method, src_host, dst_host):
            return
    logger.debug(
        "图谱对象未提供可用的边写入方法,跳过 redirect 边:%s -> %s", src_host, dst_host
    )


def _graph_note_arrival(graph: Any, url: str, prev_url: str | None) -> None:
    """到达新 URL 时登记图谱节点(以及来自上一跳的 redirect 边);全程容错。

    - 起点只登记站点节点(无前驱边);
    - 同 host 的路径级跳转不产生站点图自环,只对跨 host 跳转记 redirect 边。
    """
    if graph is None:
        return
    host = _url_host(url)
    if not host:
        return
    _graph_add_site(graph, url, host)
    if prev_url is None:
        return
    prev_host = _url_host(prev_url)
    if not prev_host or prev_host == host:
        return
    _graph_add_redirect_edge(graph, prev_host, host)


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------


def trace_redirects(
    url: str,
    cfg: Config,
    *,
    fetch: FetchPageFn | None = None,
    graph: Any = None,
) -> list[str]:
    """展开 url 的完整重定向链,返回访问序列(含起点)。

    - 每跳依次判定:HTTP 3xx Location 头 → html ``<meta refresh>`` → 顶层
      ``location.href=`` / ``location.replace(...)``,均无则终止;
    - ``fetch`` 可注入页面抓取函数(签名同 fetcher.fetch_page),缺省惰性
      复用 fetcher.fetch_page(仅在需要检查 200 页面 HTML 时才解析/调用);
    - 跳数不超过 ``cfg.redirect_max_hops``;重复 URL(环路)截断并告警;
    - 每到达一个新 URL 时向 ``graph``(A46 EvidenceGraph 形态,可选注入)
      登记站点节点与跨 host 的 redirect 边;图谱未传(None)或写入异常
      均不影响追踪;
    - 遥测(V5):每跳探测计 ``redirect.hop`` 计时;链完成计
      ``redirect.chain`` 并把跳数累加进 ``redirect.hops``;
    - ``allow_network=False`` 时,非本机地址在发出请求前抛
      ``fetcher.NetworkDisabledError``(fetcher 缺失时为本地同语义异常),
      策略性拦截同样适用于链中途出现的跨 host 外网跳转。
    """
    resolved_fetch: FetchPageFn | None = None

    def get_fetch() -> FetchPageFn:
        nonlocal resolved_fetch
        if fetch is not None:
            return fetch
        if resolved_fetch is None:
            resolved_fetch = _load_default_fetcher()
        return resolved_fetch

    max_hops = max(0, int(getattr(cfg, "redirect_max_hops", 5) or 0))

    start = _normalize_url(url)
    chain: list[str] = [start]
    seen: set[str] = {start}
    _graph_note_arrival(graph, start, None)
    logger.debug("重定向链追踪开始:%s(最大跳数 %d)", start, max_hops)

    hit_cap = False
    for _ in range(max_hops):
        current = chain[-1]
        with telemetry.timer("redirect.hop"):  # 每跳探测单独计时(V5)
            next_url, via = _next_hop(current, cfg, get_fetch)
        if next_url is None:
            break
        if next_url in seen:  # set 判定:重复 URL(环路)O(1) 截断
            logger.warning("重定向链出现环路:%s 已访问过,追踪截断。", next_url)
            break
        chain.append(next_url)
        seen.add(next_url)
        logger.debug("跳转(%s):%s -> %s", via or "未知", current, next_url)
        _graph_note_arrival(graph, next_url, current)
        if cfg.fetch_delay_s > 0:
            time.sleep(max(0.0, float(cfg.fetch_delay_s)))  # 礼貌休眠;测试传 0
    else:
        hit_cap = True  # 用满全部跳数配额:链可能仍被截断

    # 链长统计接遥测(V5):chain 计数给出链条数,hops 累加给出平均跳数分母
    telemetry.inc("redirect.chain")
    telemetry.inc("redirect.hops", amount=float(len(chain) - 1))

    if hit_cap and max_hops > 0:
        logger.warning(
            "重定向链达到最大跳数上限(%d),链条可能被截断:%s",
            max_hops,
            chain_summary(chain),
        )
    else:
        logger.info("重定向链追踪完成:%s", chain_summary(chain))
    return chain


def final_url(chain: list[str]) -> str:
    """返回重定向链的最终 URL(末元素);空链抛中文 ValueError。"""
    if not chain:
        raise ValueError("重定向链为空,无法确定最终 URL。")
    return chain[-1]


def chain_summary(chain: list[str]) -> str:
    """重定向链的中文摘要,例如 ``共 2 跳:http://a → http://b → http://c``。"""
    if not chain:
        return "空重定向链"
    return f"共 {max(0, len(chain) - 1)} 跳:" + " → ".join(chain)
