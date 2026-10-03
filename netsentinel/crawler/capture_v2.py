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

用法示例::

    from netsentinel.contracts import Config
    from netsentinel.crawler.capture_v2 import capture_page_v2, scroll_plan

    plan = scroll_plan(rounds=4, pause_s=0.5)      # 纯函数,离线可测
    cfg = Config(evidence_dir="./evidence")
    sample = capture_page_v2("http://127.0.0.1:8080/lazy.html", cfg)
"""
from __future__ import annotations

import importlib
import logging
import os
import time
from typing import Any, Callable

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, PageSample

logger = logging.getLogger(__name__)

__all__ = [
    "scroll_plan",
    "images_stable",
    "capture_page_v2",
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


def _shoot_with_playwright_v2(
    sync_playwright: Callable[..., object], url: str, cfg: Config
) -> tuple[str, str]:
    """无头 chromium 滚动采样并整页截图,返回 ``(截图路径, 页面 HTML)``。

    V5:launch/goto 瞬态超时自动重试 1 次(复用 browser._open_page_with_retry);
    截图命名复用 browser._screenshot_path(同 v1 规则 + 同秒碰撞安全);
    实际滚动轮数记 gauge ``capture_v2.scroll_rounds``、成功截图计数
    ``capture_v2.screenshot``。
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
            page.wait_for_load_state("networkidle", timeout=cfg.fetch_timeout_s * 1000)
        except Exception:  # noqa: BLE001 - networkidle 超时/失败按契约忽略,继续截图
            logger.warning("等待 networkidle 超时或失败,忽略并继续:%s", url)

        page.screenshot(path=shot_path, full_page=True)
        telemetry.inc("capture_v2.screenshot")
        html = page.content()
        logger.info(
            "capture_v2 滚动采样完成(逐轮 <img> 数量:%s),整页截图:%s", counts, shot_path
        )
        return shot_path, html
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                logger.warning("关闭 chromium 实例失败(忽略)")
        pw.stop()


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------

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
    """
    browser_mod = _load_browser_module()
    started = time.perf_counter()
    try:
        with telemetry.timer("capture_v2.page"):
            # 1) 取页面 HTML:优先 playwright(滚动采样 + 截图),未安装走退化路径。
            sync_playwright = browser_mod._import_sync_playwright()
            screenshot_path = ""
            if sync_playwright is not None:
                browser_mod._ensure_local_navigation(url, cfg)  # 安全闸门,同 v1
                screenshot_path, html = _shoot_with_playwright_v2(sync_playwright, url, cfg)
            else:
                telemetry.inc("capture_v2.degraded")
                logger.info(
                    "未安装 playwright,capture_v2 进入退化模式:仅抓取 HTML,不生成浏览器截图:%s", url
                )
                if fetch_page is None:
                    fetch_page = browser_mod._load_fetcher().fetch_page
                _status, html, _final_url = fetch_page(url, cfg)

            # 2) 图片链接提取(复用 A03 的 ImgExtractor 绝对化/去重实现)与下载。
            image_urls = browser_mod._extract_image_urls(html, url)[: max(0, cfg.max_images_per_page)]
            evidences: list[ImageEvidence] = []
            if image_urls:
                dest_dir = os.path.join(cfg.evidence_dir, f"{browser_mod._safe_host(url)}_imgs")
                os.makedirs(dest_dir, exist_ok=True)
                if download is None:
                    download = browser_mod._load_fetcher().download_images
                logger.info(
                    "capture_v2 页面 %s 提取到 %d 个图片链接(上限 %d),开始下载到 %s",
                    url, len(image_urls), cfg.max_images_per_page, dest_dir,
                )
                evidences = list(download(image_urls, url, dest_dir, cfg))
            else:
                logger.info("capture_v2 页面 %s 未提取到可下载图片链接", url)

            # 3) 文本线索命中(复用 A03 关键词表)。
            text_hint_hits = browser_mod._match_text_hints(html)
            if text_hint_hits:
                logger.info("capture_v2 页面 %s 命中文本线索:%s", url, ", ".join(text_hint_hits))

            sample = PageSample(
                url=url,
                screenshot_path=screenshot_path,
                image_evidences=evidences,
                text_hint_hits=text_hint_hits,
            )
        return sample
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
