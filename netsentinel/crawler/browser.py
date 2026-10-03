"""netsentinel.crawler.browser —— 单页采样(v1 采集引擎)。

对单个 URL 做:整页截图(playwright,缺依赖时退化为仅抓 HTML)→ 图片链接
提取(绝对化/去重/去 data:)→ 下载为图片证据 → 文本线索命中。

V5 升级:launch/goto 瞬态超时自动重试 1 次;截图命名同秒碰撞安全;超长属性
防护;telemetry(capture.page/degraded/screenshot/errors/transient_retry)。

安全红线:``allow_network=False`` 时浏览器仅允许导航本机地址(127.0.0.0/8
回环段与 ::1;外网一律拒绝)。图片下载复用 fetcher 的安全闸门。

用法示例::

    from netsentinel.contracts import Config
    from netsentinel.crawler.browser import capture_page

    sample = capture_page("http://127.0.0.1:8000/", Config())
    sample.screenshot_path, sample.image_evidences, sample.text_hint_hits
"""
from __future__ import annotations

import datetime
import importlib
import logging
import os
import re
import time
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, PageSample

__all__ = [
    "BROWSER_OPEN_ATTEMPTS",
    "MAX_ATTR_BYTES",
    "SLOW_CAPTURE_WARN_S",
    "TEXT_HINTS",
    "ImgExtractor",
    "capture_page",
]

logger = logging.getLogger(__name__)

#: 浏览器打开页面的总尝试次数(1 次原始 + 1 次瞬态重试)
BROWSER_OPEN_ATTEMPTS = 2

#: 单个 HTML 属性的长度上限(超过即跳过,防恶意页内存放大)
MAX_ATTR_BYTES = 2 * 1024 * 1024

#: 慢采样告警阈值(秒)
SLOW_CAPTURE_WARN_S = 1.0

#: 文本线索词表(小写子串匹配;记录命中原文)
TEXT_HINTS = ["色情", "成人电影", "裸聊", " AV ", "情色", "porn", "sex video", "nude"]

#: 截图名时间戳格式(HHMMSS)
_SHOT_TS_FORMAT = "%H%M%S"

#: 默认策略下放行的本机 host(另经 :func:`_is_loopback_host` 放行 127/8 与 ::1)
_LOCAL_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_loopback_host(host: str) -> bool:
    """127.0.0.0/8 全段与 ::1 均为本机回环,放行(不离开本机)。"""
    import ipaddress

    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _ensure_local_navigation(url: str, cfg: Config) -> None:
    """安全红线:allow_network=False 时,真实浏览器只允许导航到本机地址。"""
    if cfg.allow_network:
        return
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host not in _LOCAL_HOSTS and not _is_loopback_host(host):
        raise RuntimeError(
            f"allow_network=False:仅允许访问本机地址(127.0.0.1/localhost 回环),拒绝导航:{url}"
        )


def _safe_host(url: str) -> str:
    """host:port 的安全文件名形态(非 [a-zA-Z0-9.-] 一律换 ``_``)。"""
    netloc = (urlsplit(url).netloc or "unknown").lower()
    return re.sub(r"[^a-zA-Z0-9.-]", "_", netloc)


class ImgExtractor(HTMLParser):
    """从 HTML 中按出现顺序收集 <img> 的 src 与 data-src 属性值(原始值,不做绝对化)。

    - 同一标签内多属性按文档属性顺序;
    - 空值跳过;``data:`` URI 原样保留(交由 :func:`_extract_image_urls` 丢弃);
    - 单属性超过 :data:`MAX_ATTR_BYTES` 跳过并告警(防内存放大)。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "img":
            return
        for name, value in attrs:
            if name not in ("src", "data-src") or not value:
                continue
            if len(value) > MAX_ATTR_BYTES:
                logger.warning(
                    "ImgExtractor 跳过超长属性(%s,%d 字符,上限 %d)",
                    name, len(value), MAX_ATTR_BYTES,
                )
                continue
            self.links.append(value)


def _extract_image_urls(html: str, base_url: str) -> list[str]:
    """绝对化 + 去重(保序)+ 去空 + 去内嵌 data: URI 的图片链接列表。"""
    extractor = ImgExtractor()
    try:
        extractor.feed(html)
        extractor.close()
    except Exception as exc:  # noqa: BLE001 - 恶意 HTML 不应中断采样
        logger.warning("图片链接提取中止(%s),使用已收集部分", exc)
    out: list[str] = []
    seen: set[str] = set()
    for raw in extractor.links:
        if raw.startswith("data:"):
            continue
        absolute = urljoin(base_url, raw)
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append(absolute)
    return out


def _match_text_hints(html: str) -> list[str]:
    """按 TEXT_HINTS 表序返回命中的原文常量(小写子串匹配)。"""
    lowered = (html or "").lower()
    return [hint for hint in TEXT_HINTS if hint.lower() in lowered]


def _load_fetcher() -> Any:
    """惰性加载 fetcher;未就位抛中文 RuntimeError(并行开发容错)。"""
    try:
        return importlib.import_module("netsentinel.crawler.fetcher")
    except ImportError as exc:
        raise RuntimeError(
            f"fetcher 模块未就位,退化采样无法获取 HTML:{exc}"
        ) from exc


def _import_sync_playwright() -> Callable[[], Any] | None:
    """惰性导入 playwright.sync_api.sync_playwright;缺依赖返回 None(由调用方走退化)。"""
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415 - 刻意惰性

        return sync_playwright
    except ImportError:
        return None


def _is_transient_browser_error(exc: BaseException) -> bool:
    """瞬态判据:异常类(含 MRO)名为 TimeoutError 才可安全重试(导航幂等只读)。"""
    return any(cls.__name__ == "TimeoutError" for cls in type(exc).__mro__)


def _open_page_with_retry(pw: Any, url: str, cfg: Config) -> tuple[Any, Any]:
    """launch + new_page + goto,瞬态超时重试 1 次;失败实例先关闭防句柄泄漏。"""
    last_exc: BaseException | None = None
    for attempt in range(1, BROWSER_OPEN_ATTEMPTS + 1):
        browser = None
        try:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=int(cfg.fetch_timeout_s * 1000))
            return browser, page
        except Exception as exc:  # noqa: BLE001 - 统一分诊:瞬态重试,其余上抛
            if browser is not None:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001 - 关闭失败不影响主流程
                    pass
            if _is_transient_browser_error(exc) and attempt < BROWSER_OPEN_ATTEMPTS:
                telemetry.inc("capture.transient_retry")
                logger.info("浏览器打开页面瞬态超时,自动重试(第 %d 次):%s", attempt, url)
                last_exc = exc
                continue
            raise
    raise last_exc if last_exc is not None else RuntimeError("浏览器打开页面失败")  # pragma: no cover


def _screenshot_path(
    evidence_dir: str, url: str, ts: datetime.datetime | None = None, *, tag: str = ""
) -> str:
    """整页截图路径:[safe_host][_tag]_<HHMMSS>.png;同秒同 stem 追加 _1/_2 序号防覆盖。

    :param tag: 可选引擎标记(如 capture_v2 传 "v2"),避免与 v1 同秒互撞。
    """
    stamp = (ts or datetime.datetime.now()).strftime(_SHOT_TS_FORMAT)
    stem = f"{_safe_host(url)}_{tag}_{stamp}" if tag else f"{_safe_host(url)}_{stamp}"
    candidate = os.path.join(evidence_dir, f"{stem}.png")
    if not os.path.exists(candidate):
        return candidate
    seq = 1
    while True:
        candidate = os.path.join(evidence_dir, f"{stem}_{seq}.png")
        if not os.path.exists(candidate):
            return candidate
        seq += 1


def _shoot_with_playwright(
    pw_factory: Callable[[], Any], url: str, cfg: Config
) -> tuple[str, str]:
    """真实浏览器路径:goto(瞬态重试)→ networkidle 容错等待 → 整页截图 → 取 HTML。

    :return: (screenshot_path, html)
    """
    os.makedirs(cfg.evidence_dir, exist_ok=True)
    ctx = pw_factory()
    pw = ctx.start() if hasattr(ctx, "start") else ctx
    browser = None
    try:
        browser, page = _open_page_with_retry(pw, url, cfg)
        try:
            page.wait_for_load_state(
                "networkidle", timeout=int(cfg.fetch_timeout_s * 1000)
            )
        except Exception as exc:  # noqa: BLE001 - networkidle 常超时,容错继续
            logger.debug("networkidle 等待超时(忽略):%s", exc)
        shot = _screenshot_path(cfg.evidence_dir, url)
        page.screenshot(path=shot, full_page=True)
        telemetry.inc("capture.screenshot")
        return shot, page.content()
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
        stop = getattr(pw, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:  # noqa: BLE001
                pass


def _fetch_html(url: str, cfg: Config, fetch_page: Callable[..., Any] | None) -> str:
    """退化路径取 HTML:注入优先,否则 fetcher.fetch_page;非 200 视为空。"""
    if fetch_page is not None:
        status, html, _final = fetch_page(url, cfg)
        return html if status == 200 else ""
    fetcher = _load_fetcher()
    status, html, _final = fetcher.fetch_page(url, cfg)
    return html if status == 200 else ""


def capture_page(
    url: str,
    cfg: Config,
    *,
    fetch_page: Callable[..., Any] | None = None,
    download: Callable[..., Any] | None = None,
) -> PageSample:
    """单页采样:截图 + 图片证据下载 + 文本线索命中。

    - playwright 可用:导航前过本机闸门,整页截图,html=page.content();
    - playwright 缺失:退化模式(urllib 抓 HTML,截图留空,记 capture.degraded);
    - 图片链接绝对化/去重/去 data: 后截取前 ``cfg.max_images_per_page`` 张,
      经 ``download``(缺省 fetcher.download_images)落盘为 ImageEvidence;
    - 无图片时跳过下载;任何异常记 capture.errors 后原样上抛。
    """
    start = time.perf_counter()
    try:
        with telemetry.timer("capture.page"):
            sync_playwright = _import_sync_playwright()
            if sync_playwright is not None:
                _ensure_local_navigation(url, cfg)
                screenshot_path, html = _shoot_with_playwright(sync_playwright, url, cfg)
            else:
                telemetry.inc("capture.degraded")
                logger.info("未安装 playwright,退化模式采样(无截图):%s", url)
                screenshot_path, html = "", _fetch_html(url, cfg, fetch_page)

            image_urls = _extract_image_urls(html, url)[: max(0, cfg.max_images_per_page)]
            evidences: list[ImageEvidence] = []
            if image_urls:
                dest_dir = os.path.join(cfg.evidence_dir, f"{_safe_host(url)}_imgs")
                os.makedirs(dest_dir, exist_ok=True)
                dl = download
                if dl is None:
                    dl = _load_fetcher().download_images
                evidences = list(dl(image_urls, url, dest_dir, cfg))

            hints = _match_text_hints(html)
        elapsed = time.perf_counter() - start
        if elapsed > SLOW_CAPTURE_WARN_S:
            logger.warning("慢采样(%.1fs > %.1fs):%s", elapsed, SLOW_CAPTURE_WARN_S, url)
        return PageSample(
            url=url,
            screenshot_path=screenshot_path,
            image_evidences=evidences,
            text_hint_hits=hints,
        )
    except Exception:
        telemetry.inc("capture.errors")
        raise
