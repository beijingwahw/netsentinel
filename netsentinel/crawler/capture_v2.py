"""netsentinel.crawler.capture_v2 —— V2 增强采集引擎(滚动触发懒加载后再采样)。

针对违规站点普遍使用懒加载/无限滚动"藏图"的场景,V2 在 v1
(``netsentinel.crawler.browser.capture_page``)的基础上增加滚动采样阶段:

    goto(url) → 按 ``scroll_plan`` 逐轮 ``page.mouse.wheel`` 大步长下滚 + 暂停 →
    每轮统计 DOM 中 ``<img>`` 数量,按 ``images_stable`` 判稳提前退出 →
    ``window.scrollTo(0, 0)`` 回滚顶部 → networkidle 容错等待 →
    整页截图(命名同 v1 规则:safe_host + 时间戳)→ ``page.content()`` 取 HTML。

图片链接提取(ImgExtractor)、文本线索、安全闸门、退化路径**全部复用** A03 的
``netsentinel.crawler.browser``(importlib 导入,不复制其实现)。

安全红线(与 v1 完全一致):
- ``allow_network=False`` 为默认:真实浏览器导航仅放行 127.0.0.1 / localhost / ::1,
  真实导航前先校验(复用 browser 的闸门函数);
- 本模块绝不访问 www.12377.cn / www.shdf.gov.cn,也不处理任何验证码;
- 未安装 playwright 时退化为 urllib 仅抓 HTML(v1 同款语义),screenshot_path 留空。

判稳语义(明确定义,供离线单测):
- ``images_stable(prev, cur, rounds_left)``:单轮判据,返回
  ``cur == prev or rounds_left <= 0``——本轮数量与上一轮相同视为趋稳;
  轮次预算用尽则强制判稳(不再等待)。数量减少同样算"有变化",不判稳。
- 调用方(``_scroll_until_stable``)在此基础上跟踪"连续相同":需连续
  ``MIN_STABLE_ROUNDS`` 轮判稳才提前退出,以规避偶发的一轮延迟出图。

V5 升级(见 CONTRACTS-V5.md,行为与 API 兼容不变):
- playwright ``launch``/``goto`` 瞬态超时自动重试 1 次(复用 browser 的
  ``_open_page_with_retry``,ImportError 等其余异常不重试);
- 截图命名复用 browser 的 ``_screenshot_path``(同秒碰撞安全,追加序号);
- 滚动轮数完全由模块常量驱动(:data:`DEFAULT_SCROLL_ROUNDS` /
  :data:`DEFAULT_SCROLL_PAUSE_S`),视口回退宽高常量化;
- 遥测:``capture_v2.page`` 计时、``capture_v2.scroll_rounds`` gauge
  (本轮实际滚动轮数)、``capture_v2.screenshot`` / ``capture_v2.degraded`` /
  ``capture_v2.errors`` 计数,慢路径(> :data:`SLOW_CAPTURE_V2_WARN_S`)WARNING。

V10.24 升级(goto 超时优雅降级,与 v1 browser 同款口径):页级 goto 超时
三级降级链——① 瞬态重试 1 次(V5 既有)→ ② 重试用尽整页退回 urllib 退化
采样(复用 ``browser._shoot_with_goto_fallback``,计
``capture_v2.degraded_pages``;退化采样**无截图、无滚动/懒加载触发**,
``screenshot_path=""`` 如实呈现)→ ③ urllib 也失败(非 200/抛错)记
``capture_v2.errors`` 页级采样错误:批量路径继续其余页(页级失败绝不杀
整扫,样本数变少如实呈现),单页路径维持异常上抛。非超时类异常不进
降级链,行为不变。

用法示例::

    from netsentinel.contracts import Config
    from netsentinel.crawler.capture_v2 import capture_page_v2, capture_pages_v2, scroll_plan

    plan = scroll_plan(rounds=4, pause_s=0.5)      # 纯函数,离线可测
    cfg = Config(evidence_dir="./evidence")
    sample = capture_page_v2("http://127.0.0.1:8080/lazy.html", cfg)
    samples = capture_pages_v2(["http://127.0.0.1:8080/p1", "http://127.0.0.1:8080/p2"], cfg)
    # 批量口一次浏览器会话逐页复用(创世纪 G1);单页口维持逐次冷启动
"""
from __future__ import annotations

import importlib
import logging
import os
import time
from typing import Any, Callable, Iterator

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, PageSample

logger = logging.getLogger(__name__)

__all__ = [
    "scroll_plan",
    "images_stable",
    "capture_page_v2",
    "capture_pages_v2",
    "capture_pages_v2_iter",
    "DEFAULT_SCROLL_ROUNDS",
    "DEFAULT_SCROLL_PAUSE_S",
    "WHEEL_STEP_PX",
    "MIN_STABLE_ROUNDS",
    "DEFAULT_VIEWPORT_WIDTH",
    "DEFAULT_VIEWPORT_HEIGHT",
]

#: 默认滚动轮数与每轮暂停秒数(契约 A35:最多 6 轮,每轮 0.6s)。
DEFAULT_SCROLL_ROUNDS = 6
DEFAULT_SCROLL_PAUSE_S = 0.6

#: 每轮滚轮事件的纵向步长(像素);取大步长以便单轮即可接近页面底部触发懒加载。
WHEEL_STEP_PX = 3000

#: 连续多少轮 <img> 数量不变才认定懒加载已稳定(单轮不变可能只是延迟出图)。
MIN_STABLE_ROUNDS = 2

#: 视口尺寸信息缺失时的回退宽高(用于把鼠标移到视口中心;常量化便于调优)。
DEFAULT_VIEWPORT_WIDTH = 1280
DEFAULT_VIEWPORT_HEIGHT = 720

#: 单页 capture_v2 耗时超过该秒数时输出 WARNING 日志(慢路径观测)。
SLOW_CAPTURE_V2_WARN_S = 1.0

FetchPageFn = Callable[[str, Config], tuple[int, str, str]]
DownloadFn = Callable[..., list[ImageEvidence]]


# ---------------------------------------------------------------------------
# 纯逻辑(离线可测)
# ---------------------------------------------------------------------------

def scroll_plan(
    rounds: int = DEFAULT_SCROLL_ROUNDS,
    pause_s: float = DEFAULT_SCROLL_PAUSE_S,
) -> list[float]:
    """构造滚动采样计划:返回每轮滚动后的暂停秒数序列(纯函数)。

    语义:
    - ``rounds`` 为滚动轮数,须 >= 0;``rounds == 0`` 返回空列表(不滚动直接采样);
    - ``pause_s`` 为每轮滚动后的等待秒数,须 >= 0;
    - 返回列表长度 == ``rounds``,每个元素均为 ``float(pause_s)``;
    - 参数为负抛 ``ValueError``(中文消息)。
    """
    if rounds < 0:
        raise ValueError(f"滚动轮数 rounds 不能为负:收到 {rounds}")
    if pause_s < 0:
        raise ValueError(f"每轮暂停秒数 pause_s 不能为负:收到 {pause_s}")
    return [float(pause_s)] * int(rounds)


def images_stable(prev_count: int, cur_count: int, rounds_left: int) -> bool:
    """懒加载图片数量是否"判稳"(纯函数,单轮判据)。

    语义(返回 ``cur_count == prev_count or rounds_left <= 0``):
    - ``cur_count == prev_count``:本轮 DOM 中 ``<img>`` 数量与上一轮相同,
      视为懒加载趋于稳定(连续相同由调用方按轮跟踪,见 ``_scroll_until_stable``);
    - ``rounds_left <= 0``:滚动轮次预算已用尽,强制判稳(返回 True 以便退出);
    - 数量增减(含 ``cur < prev``)一律视为"仍有变化",不判稳(除非轮次用尽)。
    """
    return cur_count == prev_count or rounds_left <= 0


# ---------------------------------------------------------------------------
# 对 A03 browser 模块的复用(不复制实现)
# ---------------------------------------------------------------------------

def _load_browser_module():
    """惰性导入 A03 的 netsentinel.crawler.browser,复用其提取/闸门/退化逻辑。"""
    try:
        return importlib.import_module("netsentinel.crawler.browser")
    except ImportError as exc:
        raise RuntimeError(
            "browser 模块未就位(缺少 netsentinel.crawler.browser),"
            "capture_v2 需复用其图片提取与安全闸门,无法继续"
        ) from exc


# ---------------------------------------------------------------------------
# 浏览器路径:滚动采样 + 整页截图
# ---------------------------------------------------------------------------

def _img_count(page: Any) -> int:
    """统计当前 DOM 中 <img> 元素数量(懒加载进度观测指标)。"""
    return int(page.locator("img").count())


def _scroll_until_stable(page: Any, plan: list[float]) -> list[int]:
    """按计划逐轮下滚并暂停,返回每轮观测到的 ``<img>`` 数量序列。

    逐轮执行 ``page.mouse.wheel(0, WHEEL_STEP_PX)`` → 等待 → 统计数量,并按
    ``images_stable`` 单轮判据跟踪"连续相同"轮数:

    - 连续 ``MIN_STABLE_ROUNDS`` 轮数量不变 → 判稳提前退出;
    - 数量发生变化(增或减)→ 重置连续计数继续滚动;
    - 计划用尽(最后一轮 ``rounds_left <= 0`` 强制判稳)→ 自然退出。
    """
    counts: list[int] = []
    prev = _img_count(page)
    same_streak = 0
    for idx, pause_s in enumerate(plan):
        page.mouse.wheel(0, WHEEL_STEP_PX)
        page.wait_for_timeout(int(round(pause_s * 1000)))
        cur = _img_count(page)
        counts.append(cur)
        rounds_left = len(plan) - idx - 1
        if images_stable(prev, cur, rounds_left):
            if rounds_left <= 0:
                logger.debug("滚动轮次已用尽,停止滚动(逐轮数量:%s)", counts)
                break
            same_streak += 1
            if same_streak >= MIN_STABLE_ROUNDS:
                logger.info(
                    "<img> 数量连续 %d 轮不变,判定懒加载稳定,提前结束滚动:%s",
                    same_streak, counts,
                )
                break
        else:
            same_streak = 0
        prev = cur
    return counts


def _capture_loaded_page_v2(
    page: Any, shot_path: str, url: str, cfg: Config
) -> tuple[str, str]:
    """在**已完成 goto** 的 page 上执行 v2 滚动采样与整页截图(冷启动/会话复用共用核心)。

    步骤与口径(V5 以来保持不变):鼠标移到视口中心 → 按常量驱动计划滚动
    判稳(gauge ``capture_v2.scroll_rounds``)→ 回滚顶部 → networkidle 容错
    等待 → 整页截图(计数 ``capture_v2.screenshot``)→ ``page.content()``。
    不负责 page 的创建与关闭(由各调用方按各自生命周期管理)。
    """
    # 先把鼠标移到视口中心,避免悬停在页头固定元素上拦截滚轮事件。
    try:
        size = page.viewport_size or {}
        page.mouse.move(
            int(size.get("width", DEFAULT_VIEWPORT_WIDTH)) // 2,
            int(size.get("height", DEFAULT_VIEWPORT_HEIGHT)) // 2,
        )
    except Exception:  # noqa: BLE001 - 鼠标定位失败不影响滚动采样
        logger.debug("移动鼠标到视口中心失败(忽略)")

    # 滚动计划完全由模块常量驱动(V5 常量化,便于整体调优)。
    counts = _scroll_until_stable(
        page,
        scroll_plan(rounds=DEFAULT_SCROLL_ROUNDS, pause_s=DEFAULT_SCROLL_PAUSE_S),
    )
    telemetry.gauge("capture_v2.scroll_rounds", len(counts))

    # 回滚顶部:让整页截图从页面顶端开始取景(与人工浏览视角一致)。
    page.evaluate("window.scrollTo(0, 0)")
    try:
        # int() 取整对齐 v1 写法(browser._shoot_page_in_browser):playwright
        # 的毫秒超时形参应传整数,浮点乘积在部分版本会告警/拒收。
        page.wait_for_load_state(
            "networkidle", timeout=int(cfg.fetch_timeout_s * 1000)
        )
    except Exception:  # noqa: BLE001 - networkidle 超时/失败按契约忽略,继续截图
        logger.warning("等待 networkidle 超时或失败,忽略并继续:%s", url)

    page.screenshot(path=shot_path, full_page=True)
    telemetry.inc("capture_v2.screenshot")
    html = page.content()
    logger.info(
        "capture_v2 滚动采样完成(逐轮 <img> 数量:%s),整页截图:%s", counts, shot_path
    )
    return shot_path, html


def _shoot_with_playwright_v2(
    sync_playwright: Callable[..., object], url: str, cfg: Config
) -> tuple[str, str]:
    """冷启动路径:自起 playwright 会话 + launch,滚动采样并整页截图,返回 ``(截图路径, 页面 HTML)``。

    V5:launch/goto 瞬态超时自动重试 1 次(复用 browser._open_page_with_retry);
    截图命名复用 browser._screenshot_path(同 v1 规则 + 同秒碰撞安全);
    实际滚动轮数记 gauge ``capture_v2.scroll_rounds``、成功截图计数
    ``capture_v2.screenshot``。

    批量场景(同站多页)优先用 :func:`capture_pages_v2`——一次会话逐页
    复用,免每页冷启动;本函数保留单页冷启动语义不变。
    """
    browser_mod = _load_browser_module()
    os.makedirs(cfg.evidence_dir, exist_ok=True)
    # 截图命名复用 v1 规则并加 "v2" 标记:<safe_host>_v2_<HHMMSS>.png(避免与 v1 同秒互撞;
    # 同引擎内部同秒冲突仍自动追加序号)
    shot_path = browser_mod._screenshot_path(cfg.evidence_dir, url, tag="v2")

    pw = sync_playwright().start()
    browser = None
    try:
        browser, page = browser_mod._open_page_with_retry(pw, url, cfg)
        return _capture_loaded_page_v2(page, shot_path, url, cfg)
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                logger.warning("关闭 chromium 实例失败(忽略)")
        pw.stop()


def _shoot_page_v2_in_browser(
    browser_mod: Any, browser: Any, url: str, cfg: Config
) -> tuple[str, str]:
    """会话复用路径:在**已打开**的 browser 上完成单页 v2 滚动采样。

    与 :func:`_shoot_with_playwright_v2` 的差异仅在建页方式:复用
    ``browser._new_page_with_retry``(瞬态超时只重建 page,不重启浏览器),
    采样完 page 即关(浏览器留给会话里的下一页)——与 v1 的
    ``browser._shoot_page_in_browser`` 同一生命周期口径。
    """
    os.makedirs(cfg.evidence_dir, exist_ok=True)
    shot_path = browser_mod._screenshot_path(cfg.evidence_dir, url, tag="v2")
    page = None
    try:
        page = browser_mod._new_page_with_retry(browser, url, cfg)
        return _capture_loaded_page_v2(page, shot_path, url, cfg)
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                logger.debug("关闭 v2 采样 page 失败(忽略)")


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------

def _collect_evidences_v2(
    browser_mod: Any,
    url: str,
    cfg: Config,
    html: str,
    download: DownloadFn | None,
) -> list[ImageEvidence]:
    """从 HTML 提取图片链接(截取上限)并落盘为图片证据(无图返回空表)。

    与 v1 ``browser._collect_evidences`` 同口径,差异仅在日志措辞带 v2 标识;
    供单页 :func:`capture_page_v2` 与批量 :func:`capture_pages_v2` 共用。
    """
    image_urls = browser_mod._extract_image_urls(html, url)[: max(0, cfg.max_images_per_page)]
    evidences: list[ImageEvidence] = []
    if not image_urls:
        logger.info("capture_v2 页面 %s 未提取到可下载图片链接", url)
        return evidences
    dest_dir = os.path.join(cfg.evidence_dir, f"{browser_mod._safe_host(url)}_imgs")
    os.makedirs(dest_dir, exist_ok=True)
    dl = download
    if dl is None:
        dl = browser_mod._load_fetcher().download_images
    logger.info(
        "capture_v2 页面 %s 提取到 %d 个图片链接(上限 %d),开始下载到 %s",
        url, len(image_urls), cfg.max_images_per_page, dest_dir,
    )
    return list(dl(image_urls, url, dest_dir, cfg))


def _capture_page_v2_common(
    browser_mod: Any,
    url: str,
    cfg: Config,
    get_html: Callable[[str], tuple[str, str]],
    download: DownloadFn | None,
) -> PageSample:
    """单页 V2 采样公共骨架(冷启动 / 会话复用 / 退化三路共用)。

    ``get_html(url)`` 负责(并仅负责)取 ``(截图路径, HTML)``——含安全闸门;
    本函数包裹与 :func:`capture_page_v2` 原版逐项一致的口径:整页计时
    ``capture_v2.page``(异常路径同样计时)、图片证据、文本线索、异常计数
    ``capture_v2.errors``、慢路径 WARNING。
    """
    started = time.perf_counter()
    try:
        with telemetry.timer("capture_v2.page"):
            # 1) 取页面 HTML + 截图(playwright 滚动采样 / 退化仅抓 HTML)。
            screenshot_path, html = get_html(url)

            # 2) 图片链接提取(复用 A03 的 ImgExtractor 绝对化/去重实现)与下载。
            evidences = _collect_evidences_v2(browser_mod, url, cfg, html, download)

            # 3) 文本线索命中(复用 A03 关键词表)。
            text_hint_hits = browser_mod._match_text_hints(html)
            if text_hint_hits:
                logger.info("capture_v2 页面 %s 命中文本线索:%s", url, ", ".join(text_hint_hits))

        return PageSample(
            url=url,
            screenshot_path=screenshot_path,
            image_evidences=evidences,
            text_hint_hits=text_hint_hits,
        )
    except Exception:
        telemetry.inc("capture_v2.errors")
        raise
    finally:
        elapsed = time.perf_counter() - started
        if elapsed > SLOW_CAPTURE_V2_WARN_S:
            logger.warning(
                "capture_page_v2 慢路径:耗时 %.0f ms(阈值 %.1f s):%s",
                elapsed * 1000, SLOW_CAPTURE_V2_WARN_S, url,
            )


def capture_page_v2(
    url: str,
    cfg: Config,
    *,
    fetch_page: FetchPageFn | None = None,
    download: DownloadFn | None = None,
) -> PageSample:
    """V2 页面抽样:滚动触发懒加载后再采样,返回 :class:`PageSample`。

    与 v1(``browser.capture_page``)的差异仅在于浏览器路径多了滚动采样阶段;
    图片提取(ImgExtractor + 绝对化/去重)、文本线索命中、安全闸门、
    退化路径(无 playwright → urllib 仅抓 HTML,``screenshot_path=""``)
    均复用 A03 的 ``netsentinel.crawler.browser``,行为与 v1 保持一致。

    Args:
        url: 目标页面地址(allow_network=False 时仅放行本机)。
        cfg: 全局配置(evidence_dir / fetch_timeout_s / max_images_per_page 等)。
        fetch_page: 可注入抓取函数 ``(url, cfg) -> (status, html, final_url)``,
            退化模式默认用 fetcher.fetch_page。
        download: 可注入下载函数 ``(urls, source_page, dest_dir, cfg) -> list[ImageEvidence]``,
            缺省用 fetcher.download_images。

    Returns:
        PageSample:url、整页截图路径(退化模式为空串)、图片证据、文本线索命中。

    遥测(V5):整页计时 ``capture_v2.page``(异常路径同样计时);实际滚动轮数
    gauge ``capture_v2.scroll_rounds``;退化模式计数 ``capture_v2.degraded``;
    任何异常抛出前计数 ``capture_v2.errors``;耗时超过
    :data:`SLOW_CAPTURE_V2_WARN_S` 输出 WARNING 日志。
    V10.24:goto 超时重试用尽退回 urllib 退化采样时计
    ``capture_v2.degraded_pages``(与 v1 的 ``capture.degraded_pages`` 同名后缀)。

    批量场景(同站多页)优先用 :func:`capture_pages_v2`——一次浏览器会话
    逐页复用,免每页冷启动;本函数保留单页独立会话语义不变。
    """
    browser_mod = _load_browser_module()
    sync_playwright = browser_mod._import_sync_playwright()

    def get_html(u: str) -> tuple[str, str]:
        if sync_playwright is not None:
            browser_mod._ensure_local_navigation(u, cfg)  # 安全闸门,同 v1
            # V10.24:goto 超时三级降级链(重试用尽 → urllib 退化 → 页级错误),
            # 退化计 capture_v2.degraded_pages(口径与 v1 的 capture.degraded_pages 同名后缀)。
            return browser_mod._shoot_with_goto_fallback(
                u,
                cfg,
                lambda page_url: _shoot_with_playwright_v2(sync_playwright, page_url, cfg),
                fetch_page,
                degraded_metric="capture_v2.degraded_pages",
            )
        telemetry.inc("capture_v2.degraded")
        logger.info(
            "未安装 playwright,capture_v2 进入退化模式:仅抓取 HTML,不生成浏览器截图:%s", u
        )
        fetch = fetch_page if fetch_page is not None else browser_mod._load_fetcher().fetch_page
        _status, html, _final_url = fetch(u, cfg)
        return "", html

    return _capture_page_v2_common(browser_mod, url, cfg, get_html, download)


def capture_pages_v2(
    urls: list[str],
    cfg: Config,
    *,
    fetch_page: FetchPageFn | None = None,
    download: DownloadFn | None = None,
) -> list[PageSample]:
    """批量 V2 采样(会话复用,创世纪 G1):**一次** Playwright 启动 + 浏览器
    launch,逐页 ``new_page`` 滚动采样,全部完成后统一关闭。

    - playwright 可用:复用 ``browser.browser_session`` 会话原语;导航前逐页
      过本机闸门;每页采样口径(滚动判稳 / 整页截图 / 图片证据 / 文本线索 /
      ``capture_v2.page`` 计时 / ``capture_v2.errors`` 计数 / 慢路径告警)与
      逐页调用 :func:`capture_page_v2` 逐项一致;goto 瞬态超时自动重试 1 次
      (只重建 page,不重启浏览器,复用 ``browser._new_page_with_retry``);
      goto 超时走三级降级链(``browser._shoot_with_goto_fallback``,计
      ``capture_v2.degraded_pages``,无截图/无懒加载):退化也失败则**记
      页级采样错误(``capture_v2.errors``)并继续其余页**(页级失败绝不杀
      整扫,样本数变少如实呈现,V10.24);非超时类异常仍原样上抛
      (fail-fast,与逐页调用的传播语义相同),会话在原语 finally 中统一回收;
    - playwright 缺失 / 空列表:逐页退回 :func:`capture_page_v2`(各自走
      退化模式),行为与逐页调用完全一致;
    - 安全不变:``allow_network=False`` 闸门逐页生效,外网一律拒绝。

    性能背景:此前 v2 批量(orchestrator 逐页调 ``capture_page_v2``)每页
    各做一次 ``sync_playwright().start()`` + ``chromium.launch()`` + close +
    stop(冷启动 0.5~2s/页);本函数把会话成本摊到整站(与 v1 的
    ``browser.capture_pages`` 同款收益,V10.13 对齐)。

    P1-3 起内核为 :func:`capture_pages_v2_iter`(逐页流式产出),本函数仅
    物化为 list——list 语义与流式化之前逐字节一致。
    """
    return list(
        capture_pages_v2_iter(urls, cfg, fetch_page=fetch_page, download=download)
    )


def capture_pages_v2_iter(
    urls: list[str],
    cfg: Config,
    *,
    fetch_page: FetchPageFn | None = None,
    download: DownloadFn | None = None,
) -> Iterator[PageSample]:
    """流式批量 V2 采样(P1-3 生产者内核):逐页即时产出,会话随耗尽/提前关闭回收。

    与 :func:`capture_pages_v2` 同一内核(滚动判稳 / 三级降级 / 页级容错 /
    闸门语义一致);供编排器站内流水线在生产者侧逐页消费。
    """
    if not urls:
        return
    browser_mod = _load_browser_module()

    with browser_mod.browser_session(cfg) as browser:
        if browser is None:  # 缺 playwright:逐页退化,语义与逐页调用一致
            for u in urls:
                yield capture_page_v2(u, cfg, fetch_page=fetch_page, download=download)
            return
        telemetry.inc("capture_v2.session_reuse")
        logger.info("capture_v2 批量采样会话复用:%d 页共用一次浏览器启动", len(urls))

        def shoot_shared(u: str) -> tuple[str, str]:
            browser_mod._ensure_local_navigation(u, cfg)  # 安全闸门,同 v1
            # V10.24:goto 超时三级降级链(口径与 v1 批量一致)。
            return browser_mod._shoot_with_goto_fallback(
                u,
                cfg,
                lambda page_url: _shoot_page_v2_in_browser(
                    browser_mod, browser, page_url, cfg
                ),
                fetch_page,
                degraded_metric="capture_v2.degraded_pages",
            )

        for u in urls:
            try:
                yield _capture_page_v2_common(browser_mod, u, cfg, shoot_shared, download)
            except Exception as exc:
                # 注意:capture_v2.errors 已在 _capture_page_v2_common 内计数,此处不重复。
                if browser_mod._is_transient_browser_error(exc):
                    # 第③级落地:goto 超时且 urllib 退化也未果 → 页级错误,
                    # 不杀整扫,继续其余页(样本数变少如实呈现)。
                    logger.error(
                        "capture_v2 页面 %s goto 超时且 urllib 退化采样均失败,"
                        "记页级采样错误,继续其余页(%s)", u, exc,
                    )
                    continue
                raise
