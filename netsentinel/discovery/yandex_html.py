"""内置 Yandex 网页检索引擎(V10.23"内置 yandex 内核";V10.25 内置 Yandex 浏览器)。

不依赖官方付费 XML API:直接读取 Yandex 网页检索结果页
(``https://yandex.com/search/?text=<query>``)并解析自然结果链接。

双通道传输:
- **browser**(优先):真实浏览器渲染后取 DOM——**V10.25 起通道优先级为
  Yandex Browser(同厂指纹,winget/官网安装后自动检测)→ Chrome → Edge →
  Playwright 自带 Chromium**;浏览器通道用**原生 UA**(不覆盖为采集 UA);
  **会话持久化**缺省启用(``<data>/browser_profiles/yandex_html``,
  人工验证通过后的 cookie 跨查询/跨运行保留;env 可关);
- **http**(兜底):纯 urllib 拉取(NetSentinel UA 公开身份);Yandex 对
  高频/数据中心 IP 会出验证码,低频 + 礼貌间隔(流水线
  ``discovery_query_delay_s``,缺省 3s)下家用网络通常可用。

安全与合规(与 XML 引擎同纪律):
- 仅在 ``cfg.discovery_online=True`` 时外呼(红线 28);
- **绝不识别/解析/绕过任何验证码与反爬**(红线 1):检测到验证码页即
  中文报错停止;有头模式(``NETSENTINEL_YANDEX_HTML_HEADED=1``)进入
  **人工验证码门**——窗口保持可见,轮询等待运营者亲手完成(最长 180s),
  机器只等待绝不代填;
- 只读公开检索结果,不模拟登录、不点击、不翻页;
- 结果解析纯标准库(``html.parser``),零新依赖;
- telemetry 只记名称与数字;查询词不入日志。

SERP 链接口径:收集 ``<a href>`` → 解码 Yandex 跳转包装
(``/clck/jsredir?...&url=<encoded>``)→ 剔除 Yandex 自有域 → http(s) 直链
去重保序(自然排名序)→ 截断到 limit。域过滤与 canonical 去重的最终
口径由流水线(discover)统一执行,引擎只做显而易见的自域剔除。
"""
from __future__ import annotations

import logging
import os
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlsplit

from netsentinel.contracts import Config
from netsentinel.discovery.engines import DiscoveryConfigError, SearchEngine, SearchHit

__all__ = [
    "YandexHtmlEngine",
    "YANDEX_HTML_BASE",
    "USER_AGENT",
    "parse_serp_links",
    "wait_human_captcha",
    "detect_yandex_browser",
    "browser_launch_candidates",
]

logger = logging.getLogger(__name__)

#: 网页检索端点(可经 cfg 附加属性 yandex_html_base / 测试注入覆盖)。
YANDEX_HTML_BASE = "https://yandex.com/search/"

#: 对外 UA(与 yandex.py 的 XML 引擎同一取值副本;修改须同步,见该文件注释)。
USER_AGENT = "NetSentinel/0.17"

#: Yandex 自有域后缀(引擎级自域剔除;流水线 DEFAULT_EXCLUDE_DOMAINS 另有总口径)。
_YANDEX_HOST_SUFFIXES = (
    "yandex.com",
    "yandex.ru",
    "yandex.net",
    "yastatic.net",
    "ya.ru",
    "moikrug.ru",
)

#: 验证码/反爬页面特征(命中即停,绝不绕过)。
_CAPTCHA_MARKERS = (
    "showcaptcha",
    "smartcaptcha",
    "ucheck",
    "checkcaptcha",
    "технические работы",
)

#: 验证码判定时,自然结果链接数的上限(低于此数且命中特征才判验证码,
#: 避免普通页面偶含 "captcha" 字样误伤)。
_CAPTCHA_MAX_LINKS = 3


def _http_get(url: str, timeout: float) -> tuple[int, str]:
    """GET 并返回 (status, body);连接层错误转中文 RuntimeError(同 XML 引擎)。"""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200) or 200), resp.read().decode(
                "utf-8", errors="replace"
            )
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"搜索外呼连接失败(DNS 解析失败/断网/超时):{getattr(exc, 'reason', exc)}"
        ) from exc


def _looks_like_captcha(url: str, html: str) -> bool:
    """验证码页判定:URL 特征或(页面特征 + 自然结果几乎为零)。"""
    lowered_url = (url or "").lower()
    if any(m in lowered_url for m in ("showcaptcha", "checkcaptcha", "smartcaptcha")):
        return True
    lowered = (html or "").lower()
    if "captcha" not in lowered:
        return False
    hrefs = _LinkCollector()
    try:
        hrefs.feed(html)
    except Exception:  # noqa: BLE001
        pass
    external = [
        h
        for h in hrefs.hrefs
        if h.startswith(("http://", "https://")) and not _is_yandex_host(h)
    ]
    return len(external) <= _CAPTCHA_MAX_LINKS


def wait_human_captcha(
    page: Any,
    max_wait_s: float = 180.0,
    *,
    poll_s: float = 1.5,
    sleeper: Callable[[float], None] | None = None,
) -> bool:
    """有头模式的**人工验证码门**:弹出窗口由运营者亲手完成验证。

    红线 1 的正解形态——系统绝不识别/代填/绕过验证码,只在可见窗口里
    **等待真人完成**:轮询页面直到验证码消失(运营者提交后 Yandex 会
    跳回原结果页)。返回 True=已解除;False=超时仍驻留(调用方如实报错)。
    """
    import time as _time

    sleep = sleeper or _time.sleep
    deadline = _time.monotonic() + max(1.0, float(max_wait_s))
    print(
        "\n[人工验证码门] Yandex 要求人机验证:请在刚打开的浏览器窗口里"
        "\n               亲手完成验证(系统绝不代填,红线 1)。"
        f"\n               完成后自动继续,最长等待 {int(max_wait_s)} 秒…\n",
        flush=True,
    )
    while _time.monotonic() < deadline:
        sleep(poll_s)
        try:
            if not _looks_like_captcha(page.url, page.content()):
                return True
        except Exception:  # noqa: BLE001 - 页面跳转中的瞬时错误按未解除处理
            continue
    return False


def detect_yandex_browser() -> str | None:
    """检测系统已安装的 Yandex Browser(内置浏览器通道,V10.25)。

    优先级:环境变量 ``NETSENTINEL_YANDEX_BROWSER_PATH``(显式指定)→
    Windows 标准安装路径三处;未安装返回 None(通道列表跳过)。
    """
    env = os.environ.get("NETSENTINEL_YANDEX_BROWSER_PATH", "").strip()
    if env:
        return env if Path(env).is_file() else None
    local = os.environ.get("LOCALAPPDATA", "")
    candidates: list[Path] = []
    if local:
        candidates.append(Path(local) / "Yandex" / "YandexBrowser" / "Application" / "browser.exe")
    candidates.extend(
        [
            Path("C:/Program Files (x86)/Yandex/YandexBrowser/Application/browser.exe"),
            Path("C:/Program Files/Yandex/YandexBrowser/Application/browser.exe"),
        ]
    )
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return None


def _profile_dir() -> Path | None:
    """浏览器会话持久化目录(<data>/browser_profiles/yandex_html)。

    价值:人工验证码通过后的 cookie 跨查询/跨运行保留(Yandex 对已验证
    会话显著宽容);目录随 ``data/`` 不入库。``NETSENTINEL_YANDEX_HTML_PROFILE=0``
    可关闭(无痕语义,验证码需逐轮人工)。
    """
    if os.environ.get("NETSENTINEL_YANDEX_HTML_PROFILE", "").strip() == "0":
        return None
    base = os.environ.get("NETSENTINEL_DATA_DIR", "").strip() or "data"
    return Path(base) / "browser_profiles" / "yandex_html"


def browser_launch_candidates() -> list[dict[str, Any]]:
    """浏览器通道候选(V10.25 起含内置 Yandex Browser,按优先级)。

    Yandex Browser(同厂指纹,对 Yandex 检索最自然)→ 系统 Chrome →
    系统 Edge → Playwright 自带 Chromium;前两者缺席自动跳过。
    """
    candidates: list[dict[str, Any]] = []
    yandex_exe = detect_yandex_browser()
    if yandex_exe:
        candidates.append({"executable_path": yandex_exe})
    candidates.extend([{"channel": "chrome"}, {"channel": "msedge"}, {}])
    return candidates


def _browser_get(url: str, timeout: float) -> tuple[int, str]:
    """浏览器渲染后返回 (200, 渲染后 HTML)。

    - 通道优先级:内置 Yandex Browser → Chrome → Edge → 自带 Chromium
      (V10.25:Yandex Browser 经 winget/官网安装后自动检测接入;亦可用
      ``NETSENTINEL_YANDEX_BROWSER_PATH`` 显式指定可执行文件);
    - **原生 UA**:浏览器通道不覆盖 User-Agent(用浏览器自身 UA)——
      V10.23 曾覆盖为 NetSentinel/0.17,真浏览器配机器人 UA 反而更易被
      识别;HTTP 通道仍声明 NetSentinel UA(采集身份礼貌公开);
    - **会话持久化**:缺省启用 :func:`_profile_dir`(验证码通过后的
      cookie 跨查询/跨运行保留);``NETSENTINEL_YANDEX_HTML_PROFILE=0`` 关闭;
    - **验证码红线不变**:检测到验证码一律不绕过;有头模式
      (``NETSENTINEL_YANDEX_HTML_HEADED=1``)进入 :func:`wait_human_captcha`
      人工门(真人窗口内完成,机器只等待);无头模式即停报错。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise DiscoveryConfigError(
            "yandex-html 浏览器通道需要 Playwright:"
            "pip install -e \".[browser]\" && playwright install chromium"
            "(或改用 --transport http 纯 HTTP 通道)"
        ) from exc

    headed = os.environ.get("NETSENTINEL_YANDEX_HTML_HEADED", "").strip() in ("1", "true", "yes")
    profile = _profile_dir()
    last_error: Exception | None = None
    for launch_kwargs in browser_launch_candidates():
        with sync_playwright() as pw:
            context = None
            browser = None
            try:
                if profile is not None:
                    profile.mkdir(parents=True, exist_ok=True)
                    context = pw.chromium.launch_persistent_context(
                        str(profile),
                        headless=not headed,
                        locale="zh-CN",
                        **launch_kwargs,
                    )
                    page = context.pages[0] if context.pages else context.new_page()
                else:
                    browser = pw.chromium.launch(headless=not headed, **launch_kwargs)
                    page = browser.new_page(locale="zh-CN")
            except Exception as exc:  # noqa: BLE001 - 该内核缺席试下一个
                last_error = exc
                continue
            try:
                page.goto(url, timeout=int(timeout * 1000))
                page.wait_for_load_state("domcontentloaded", timeout=int(timeout * 1000))
                page.wait_for_timeout(1200)  # 结果区脚本跳转收敛(固定短等,无交互)
                html = page.content()
                if headed and _looks_like_captcha(page.url, html):
                    wait_s = float(
                        os.environ.get("NETSENTINEL_YANDEX_HTML_CAPTCHA_WAIT_S", "300").strip()
                        or 300
                    )
                    if not wait_human_captcha(page, wait_s):
                        raise RuntimeError(
                            f"验证码人工门超时({int(wait_s)} 秒内未完成):请在窗口内亲手完成验证后重试,"
                            "或放慢节奏/改用官方 XML API(红线 1:系统绝不代填验证码)"
                        )
                    html = page.content()  # 运营者完成验证后的结果页
                return 200, html
            finally:
                try:
                    if context is not None:
                        context.close()
                    if browser is not None:
                        browser.close()
                except Exception:  # noqa: BLE001 - 关闭失败不掩盖取回的内容
                    pass
    raise RuntimeError(
        "yandex-html 浏览器通道启动失败"
        f"(YandexBrowser/chrome/msedge/chromium 均不可用):{last_error}"
    )


def _is_yandex_host(url: str) -> bool:
    """URL 主机是否 Yandex 自有域(大小写不归一由 urlsplit 处理)。"""
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return True  # 无主机的一律不是可扫描线索
    return any(
        host == suffix or host.endswith("." + suffix) for suffix in _YANDEX_HOST_SUFFIXES
    )


def _decode_yandex_redirect(url: str) -> str:
    """解码 Yandex 跳转包装链接(clck/jsredir 的 url 参数)。

    形如 ``https://yandex.com/clck/jsredir?...&url=https%3A%2F%2F目标``;
    非包装链接原样返回。
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if "clck" not in (host + parts.path):
        return url
    for chunk in parts.query.split("&"):
        if chunk.lower().startswith(("url=", "u=")):
            target = unquote(chunk.split("=", 1)[1])
            if target.startswith(("http://", "https://")):
                return target
    return url


class _LinkCollector(HTMLParser):
    """标准库 HTML 链接收集器(只取 <a href>,忽略其余标签)。"""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self.hrefs.append(value.strip())


def parse_serp_links(html_text: str, limit: int) -> list[SearchHit]:
    """从 SERP HTML 解析自然结果线索(纯函数,离线可测)。

    口径:``<a href>`` → 解码 Yandex 跳转包装 → 剔除自域/非 http(s) →
    去重保序(自然排名序)→ 截断 limit;验证码页抛中文 RuntimeError。
    """
    collector = _LinkCollector()
    try:
        collector.feed(html_text or "")
    except Exception:  # noqa: BLE001 - 坏 HTML 按无链接处理,由上限外逻辑报"零结果"
        pass

    seen: set[str] = set()
    hits: list[SearchHit] = []
    for href in collector.hrefs:
        if href.startswith(("javascript:", "mailto:", "tel:", "#", "/")):
            continue
        target = _decode_yandex_redirect(href)
        if not target.startswith(("http://", "https://")):
            continue
        if _is_yandex_host(target):
            continue
        if target in seen:
            continue
        seen.add(target)
        hits.append(SearchHit.from_url(target, engine="yandex-html"))
        if len(hits) >= max(1, int(limit)):
            break

    lowered = (html_text or "").lower()
    if (
        len(hits) <= _CAPTCHA_MAX_LINKS
        and lowered
        and any(marker in lowered for marker in _CAPTCHA_MARKERS)
    ):
        raise RuntimeError(
            "Yandex 返回了验证码/反爬页面:按红线 1 绝不自动识别或绕过;"
            "请放慢查询节奏(discovery_query_delay_s)、改用浏览器通道"
            "(--transport browser)或官方 XML API(凭据接入),由运营者决策"
        )
    return hits


class YandexHtmlEngine(SearchEngine):
    """内置 Yandex 网页引擎(双通道;离线/未授权时构造或调用即拒)。"""

    name = "yandex-html"

    def __init__(
        self,
        cfg: Config,
        *,
        transport: Callable[[str, float], tuple[int, str]] | None = None,
        base: str = "",
        mode: str = "",
    ) -> None:
        self.cfg = cfg
        self._base = (
            base
            or (getattr(cfg, "yandex_html_base", "") or "").strip()
            or os.environ.get("NETSENTINEL_YANDEX_HTML_BASE", "")
            or YANDEX_HTML_BASE
        ).rstrip("/")
        mode = (mode or getattr(cfg, "yandex_html_transport", "") or "auto").strip().lower()
        if mode not in ("auto", "browser", "http"):
            raise DiscoveryConfigError(
                f"yandex-html 传输通道应为 auto/browser/http,得到:{mode!r}"
            )
        self._mode = mode
        self._transport = transport  # 测试注入优先;缺省按通道选择

    # -- 协议实现 ---------------------------------------------------------
    def search(self, query: str, limit: int) -> list[SearchHit]:
        if not getattr(self.cfg, "discovery_online", False):
            raise DiscoveryConfigError(
                "yandex-html 发现层未开启:需在配置中显式设置 discovery_online: true"
                "(搜索外呼须运营者显式授权,默认零外呼)"
            )
        url = f"{self._base}/?text={quote(query)}"
        fetch = self._transport or self._select_fetch()
        status, body = fetch(url, 30.0)
        if status != 200:
            raise RuntimeError(
                f"Yandex 网页检索返回 HTTP {status}"
                "(可能被限流:请放慢节奏或改用官方 XML API)"
            )
        hits = parse_serp_links(body, limit)
        if not hits:
            logger.warning("yandex-html:查询 %d 字返回 0 条自然结果(可能为空页)", len(query))
        return hits

    # -- 内部 -------------------------------------------------------------
    def _select_fetch(self) -> Callable[[str, float], tuple[int, str]]:
        """按通道选择传输:auto=浏览器可用则浏览器,否则纯 HTTP。"""
        if self._mode == "http":
            return _http_get
        if self._mode == "browser":
            return _browser_get
        # auto:Playwright 在席用浏览器,缺席退 HTTP(不弹安装错误,给中文提示)
        try:
            import playwright  # noqa: F401

            return _browser_get
        except ImportError:
            logger.info("yandex-html:Playwright 缺席,退纯 HTTP 通道(浏览器口径更稳)")
            return _http_get
