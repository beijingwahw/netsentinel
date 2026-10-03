"""NetSentinel 网页/图片抓取器(A02)。

仅使用标准库(urllib)完成页面抓取与图片下载。

用法示例::

    from netsentinel.contracts import Config
    from netsentinel.crawler.fetcher import fetch_page

    status, html, final_url = fetch_page("http://127.0.0.1:8000/page", Config())
    # status=0 表示连接层失败;HTTP 4xx/5xx 以状态码返回而不抛出

安全设计(对应团队契约 §0 红线):
- ``allow_network=False``(默认)时,只有目标 host 为 127.0.0.1 / localhost
  才允许发起请求,其余一律在发出前被安全闸门拦截(NetworkDisabledError);
- ``respect_robots=True`` 时,真实外网目标先经 urllib.robotparser 校验,
  本机地址(开发/测试服务)跳过 robots 检查。

V5 升级(行为兼容,仅增强):
- 幂等 GET 的连接类错误(URLError 超时/连接重置)按指数退避重试,最多
  :data:`RETRY_MAX_ATTEMPTS` 次尝试;HTTP 4xx/5xx 与非连接类错误不重试;
- robots.txt 校验结果按站点(netloc)进程内 TTL 缓存
  (:data:`ROBOTS_CACHE_TTL_S`),避免逐页抓取时重复拉取;
- 批量下载时同一 host 连续失败 :data:`HOST_FAILURE_LIMIT` 次即提前放弃
  本批该 host 的剩余 URL(记 warning 与 telemetry 计数);
- 关键路径接入 ``netsentinel.telemetry``(fetch.page / fetch.image /
  fetch.retry / fetch.errors / fetch.host_abandoned)。
"""
from __future__ import annotations

import hashlib
import logging
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser  # 供 _check_robots 动态使用/便于测试打桩
from pathlib import Path
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence

__all__ = [
    "NetworkDisabledError",
    "RobotsDeniedError",
    "fetch_page",
    "download_images",
]

logger = logging.getLogger(__name__)

#: 对外声明的 User-Agent(契约固定值)
USER_AGENT = "NetSentinel/0.1"

#: HTML 响应体读取上限(2MB,超出截断)
MAX_HTML_BYTES = 2 * 1024 * 1024

#: URL 无扩展名时落盘使用的默认扩展名
DEFAULT_IMAGE_EXT = ".jpg"

#: 视为图片的 URL 路径后缀(响应头缺失 Content-Type 时的放行依据)
_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico"})

#: 默认策略下放行的本机 host
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})


def _is_loopback_host(host: str) -> bool:
    """127.0.0.0/8 全段与 ::1 均为本机回环,放行(不离开本机)。"""
    import ipaddress

    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False

#: 幂等 GET 的最大尝试次数 = 1 次原始请求 + 最多 2 次重试(V5)
RETRY_MAX_ATTEMPTS = 3

#: 重试指数退避基数(秒):第 1 次重试约 0.5s,第 2 次约 1.0s(V5)
RETRY_BACKOFF_BASE_S = 0.5

#: 退避随机抖动上限比例(0.25 = 在退避值上最多再加 25% 抖动;V5)
RETRY_BACKOFF_JITTER = 0.25

#: robots.txt 结果进程内缓存有效期(秒;V5)
ROBOTS_CACHE_TTL_S = 60.0

#: 批量下载时同一 host 连续失败达到该次数即提前放弃本批该 host(V5)
HOST_FAILURE_LIMIT = 3

#: 视为"连接类错误"而允许重试的底层原因类型(超时 / 连接被重置或中断)。
#: 连接被拒绝(ConnectionRefusedError)是确定性失败、DNS 解析失败
#: (socket.gaierror)重试通常无益,均不在其列,保持单次尝试的既有行为。
_RETRYABLE_REASONS: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
)

#: robots 缓存:netloc(小写)-> (写入时刻 time.monotonic,已成功 read 的 parser)
_ROBOTS_CACHE: dict[str, tuple[float, urllib.robotparser.RobotFileParser]] = {}
_ROBOTS_CACHE_LOCK = threading.Lock()


class NetworkDisabledError(RuntimeError):
    """默认安全策略下仅放行本机地址(127.0.0.1 / localhost)的网络请求。"""


class RobotsDeniedError(RuntimeError):
    """目标站点 robots.txt 禁止抓取,已按礼貌抓取策略中止请求。"""


def _url_host(url: str) -> str:
    """解析 URL 的 hostname(小写;解析失败返回空串)。"""
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _gate_network_access(url: str, cfg: Config) -> str:
    """安全闸门:allow_network=False 时仅放行本机地址,返回目标 host。

    非本机地址直接抛 :class:`NetworkDisabledError`,不会发出任何真实请求。
    """
    host = _url_host(url)
    if cfg.allow_network or host in _LOCAL_HOSTS or _is_loopback_host(host):
        return host
    raise NetworkDisabledError(
        f"默认安全策略禁止访问非本机地址:已拦截 {url}(host={host or '未知'})。"
        "如确需抓取该真实站点,请在配置中将 allow_network 设为 True 后重试。"
    )


def _is_retryable_connection_error(exc: BaseException) -> bool:
    """判断异常是否为可安全重试的连接类错误(URLError 超时/连接重置)。

    - HTTPError(4xx/5xx 应用层应答)与 URLError 以外的异常一律不可重试;
    - 仅当 ``exc.reason`` 属于 :data:`_RETRYABLE_REASONS`(超时/连接重置族)
      时才视为瞬时故障,值得按指数退避重试。
    """
    if isinstance(exc, urllib.error.HTTPError):
        return False  # HTTP 4xx/5xx 是服务器明确应答,重试无益且不礼貌
    if not isinstance(exc, urllib.error.URLError):
        return False
    return isinstance(exc.reason, _RETRYABLE_REASONS)


def _backoff_delay(attempt: int) -> float:
    """计算第 ``attempt`` 次重试(从 1 计)前的指数退避秒数(含随机抖动)。

    第 1 次重试约 ``RETRY_BACKOFF_BASE_S`` 秒,之后逐次翻倍,并叠加不超过
    :data:`RETRY_BACKOFF_JITTER` 比例的随机抖动以防惊群。
    """
    base = RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1))
    return base + base * RETRY_BACKOFF_JITTER * random.random()


def _sleep_backoff(attempt: int) -> None:
    """重试前按指数退避休眠(独立函数便于测试打桩,避免真实等待)。"""
    time.sleep(_backoff_delay(attempt))


def _urlopen_with_retry(request: urllib.request.Request, timeout: float) -> Any:
    """打开 request 并返回响应对象;仅对连接类错误做指数退避重试(V5)。

    - 最多 :data:`RETRY_MAX_ATTEMPTS` 次尝试(1 次原始 + 最多 2 次重试),
      请求为幂等 GET,重试安全;
    - HTTPError(4xx/5xx)与非连接类 URLError(如 DNS 失败、连接被拒绝)
      不重试,原样抛出,由调用方按既有语义处理;
    - 每次真正重试前记 ``telemetry.inc("fetch.retry")`` 并退避休眠。
    """
    for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.URLError as exc:  # 含子类 HTTPError
            if not _is_retryable_connection_error(exc) or attempt >= RETRY_MAX_ATTEMPTS:
                raise
            telemetry.inc("fetch.retry")
            logger.info(
                "连接类错误(第 %d/%d 次尝试),指数退避后重试:%s(%s)",
                attempt,
                RETRY_MAX_ATTEMPTS,
                request.full_url,
                exc,
            )
            _sleep_backoff(attempt)
    raise AssertionError("重试循环不可能正常退出")  # pragma: no cover


def _robots_parser_cached(robots_url: str) -> urllib.robotparser.RobotFileParser | None:
    """取 robots parser:命中未过期的进程内缓存则复用,否则拉取并缓存(V5)。

    - 缓存键为 robots_url 的 netloc(小写,含端口),同一站点在
      :data:`ROBOTS_CACHE_TTL_S` 内只拉取一次,避免逐页抓取时的重复请求;
    - robots.txt 自身获取失败(网络抖动)按"允许"处理并留痕,且**不缓存**,
      下次校验仍会尝试重新拉取;
    - 返回 None 表示读取失败(调用方按允许放行)。
    """
    key = urllib.parse.urlsplit(robots_url).netloc.lower()
    now = time.monotonic()
    with _ROBOTS_CACHE_LOCK:
        entry = _ROBOTS_CACHE.get(key)
        if entry is not None and now - entry[0] < ROBOTS_CACHE_TTL_S:
            return entry[1]
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)
    try:
        parser.read()
    except (urllib.error.URLError, OSError) as exc:
        # robots.txt 自身获取失败(如网络抖动)时按"允许"处理并留痕
        logger.warning("robots.txt 获取失败,按允许处理:%s(%s)", robots_url, exc)
        return None
    with _ROBOTS_CACHE_LOCK:
        _ROBOTS_CACHE[key] = (time.monotonic(), parser)
    return parser


def _check_robots(url: str, cfg: Config) -> None:
    """robots.txt 校验:仅针对真实外网目标;本机地址一律跳过。

    只会在安全闸门放行之后才会执行,因此本函数内的 robots.txt 读取
    只可能发生在 allow_network=True 且目标为外网时。校验结果经进程内
    TTL 缓存(:func:`_robots_parser_cached`)避免同站重复拉取。
    """
    if not cfg.respect_robots:
        return
    host = _url_host(url)
    if not host or host in _LOCAL_HOSTS or _is_loopback_host(host):
        return  # 本机开发/测试服务不适用 robots
    parts = urllib.parse.urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    parser = _robots_parser_cached(robots_url)
    if parser is None:
        return  # robots.txt 获取失败已留痕,按允许处理
    if not parser.can_fetch(USER_AGENT, url):
        raise RobotsDeniedError(f"目标站点 robots.txt 禁止抓取:{url},已中止请求。")


def _url_suffix(url: str) -> str:
    """取 URL 路径的文件扩展名(小写,含点;无扩展名返回空串)。"""
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    return Path(path).suffix.lower()


def fetch_page(url: str, cfg: Config) -> tuple[int, str, str]:
    """抓取单个页面,返回 ``(status, html, final_url)``。

    - User-Agent 固定为 ``NetSentinel/0.1``,超时取 ``cfg.fetch_timeout_s``;
    - 响应体最多读取 2MB,超出部分直接截断;
    - HTTP 错误码(HTTPError)不抛出:返回 ``(错误码, "", 最终 URL)``,
      且**不重试**;连接层错误中的超时/连接重置按指数退避重试(<= 2 次,
      见 :func:`_urlopen_with_retry`),重试耗尽仍失败才返回 0;
    - 连接层错误(URLError,如拒绝连接/DNS 失败)返回 ``(0, "", 原始 URL)``;
    - 其余意外异常按契约原样抛出;
    - ``allow_network=False`` 时非本机地址抛 :class:`NetworkDisabledError`;
      外网目标在 ``respect_robots=True`` 且被 robots 禁止时抛
      :class:`RobotsDeniedError`;
    - 遥测:``fetch.page`` 计时(含重试)、``fetch.page`` /
      ``fetch.page.<status>`` 计数、连接失败再计 ``fetch.errors``。
    """
    _gate_network_access(url, cfg)
    _check_robots(url, cfg)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with telemetry.timer("fetch.page"):
        try:
            with _urlopen_with_retry(request, cfg.fetch_timeout_s) as resp:
                status = int(getattr(resp, "status", 0) or 0)
                final_url = resp.geturl() or url
                data = resp.read(MAX_HTML_BYTES + 1)
                html = data[:MAX_HTML_BYTES].decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            logger.info("页面返回 HTTP 错误 %s:%s", exc.code, url)
            status, html, final_url = int(exc.code), "", str(getattr(exc, "url", "") or url)
        except urllib.error.URLError as exc:
            logger.warning("页面抓取连接失败:%s(%s)", url, exc)
            status, html, final_url = 0, "", url
    telemetry.inc("fetch.page")
    telemetry.inc(f"fetch.page.{status}")
    if status == 0:
        telemetry.inc("fetch.errors")
    return status, html, final_url


def _download_one(url: str, source_page: str, dest: Path, cfg: Config) -> ImageEvidence | None:
    """下载单张图片并落盘;不满足条件的资源返回 None(由调用方跳过)。

    安全闸门与 robots 校验同 :func:`fetch_page`;连接类错误同样走指数退避
    重试(:func:`_urlopen_with_retry`);整体耗时记 ``telemetry.timer("fetch.image")``。
    """
    _gate_network_access(url, cfg)
    _check_robots(url, cfg)
    limit_bytes = max(0, int(cfg.max_image_mb)) * 1024 * 1024
    suffix = _url_suffix(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    telemetry.inc("fetch.image")
    with telemetry.timer("fetch.image"):
        with _urlopen_with_retry(request, cfg.fetch_timeout_s) as resp:
            status = int(getattr(resp, "status", 0) or 0)
            if status >= 400:
                logger.debug("图片响应状态异常 HTTP %s,跳过:%s", status, url)
                return None
            content_type = resp.headers.get("Content-Type")
            if content_type is not None:
                if not content_type.split(";")[0].strip().lower().startswith("image/"):
                    logger.debug("Content-Type 非 image/*(%s),跳过:%s", content_type, url)
                    return None
            elif suffix not in _IMAGE_EXTS:
                logger.debug("响应缺少 Content-Type 且 URL 后缀非图片(%s),跳过:%s", suffix, url)
                return None
            data = resp.read(limit_bytes + 1)
            if len(data) > limit_bytes:
                logger.debug("图片超过大小上限 %d MB,跳过:%s", cfg.max_image_mb, url)
                return None
        digest = hashlib.sha256(data).hexdigest()
        path = dest / f"{digest[:16]}{suffix or DEFAULT_IMAGE_EXT}"
        path.write_bytes(data)
    logger.debug("图片已保存:%s <- %s", path, url)
    return ImageEvidence(
        path=str(path),
        url=url,
        source_page=source_page,
        sha256=digest,
        width=0,
        height=0,
    )


def download_images(
    urls: list[str],
    source_page: str,
    dest_dir: str,
    cfg: Config,
) -> list[ImageEvidence]:
    """按顺序下载图片并落盘,返回成功项的 :class:`ImageEvidence` 列表。

    - 保持输入顺序,按 URL 字符串去重(仅保留首次出现);
    - 同样执行 allow_network 安全闸门(非本机且禁网时抛
      :class:`NetworkDisabledError`,属于策略错误,不视为单张失败);
    - 单文件超过 ``cfg.max_image_mb``、或 Content-Type 非 ``image/*`` 时跳过
      (响应头缺失 Content-Type 时,URL 后缀为图片扩展名才放行);
    - 落盘目录 ``dest_dir`` 不存在时自动创建;文件名 = sha256 前 16 位 +
      原扩展名(无扩展名默认 ``.jpg``);宽高填 0;
    - 单张失败仅记 debug 日志并继续;相邻两次请求之间 sleep
      ``cfg.fetch_delay_s`` 秒(测试传 0 即不等待);
    - 同一 host **连续失败** :data:`HOST_FAILURE_LIMIT` 次(仅统计网络层异常,
      策略性跳过不算失败)即提前放弃本批该 host 的剩余 URL:记 warning 与
      ``telemetry.inc("fetch.host_abandoned")``,其他 host 不受影响(V5)。
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    results: list[ImageEvidence] = []
    seen: set[str] = set()
    requested = False  # 是否已经发起过至少一次真实请求
    consecutive_failures: dict[str, int] = {}  # host -> 当前连续失败次数
    abandoned_hosts: set[str] = set()  # 本批已提前放弃的 host
    for index, url in enumerate(urls):
        if url in seen:
            logger.debug("跳过重复图片 URL:%s", url)
            continue
        seen.add(url)
        host = _url_host(url)
        if host in abandoned_hosts:
            logger.debug("host 已因连续失败被放弃,跳过本批剩余 URL:%s(host=%s)", url, host)
            continue
        if requested and cfg.fetch_delay_s > 0:
            time.sleep(cfg.fetch_delay_s)
        try:
            evidence = _download_one(url, source_page, dest, cfg)
        except (NetworkDisabledError, RobotsDeniedError):
            # 策略性拦截必须向上冒泡,不能被"单张失败继续"吞掉
            raise
        except Exception as exc:  # 单张失败:记录后继续
            logger.debug("下载图片失败,已跳过:%s(%s)", url, exc)
            telemetry.inc("fetch.errors")
            failures = consecutive_failures.get(host, 0) + 1
            consecutive_failures[host] = failures
            if failures >= HOST_FAILURE_LIMIT:
                abandoned_hosts.add(host)
                remaining = sum(1 for other in urls[index + 1 :] if _url_host(other) == host)
                telemetry.inc("fetch.host_abandoned")
                logger.warning(
                    "同一 host 连续失败 %d 次,提前放弃本批该 host 的剩余 %d 个 URL:host=%s",
                    HOST_FAILURE_LIMIT,
                    remaining,
                    host,
                )
            continue
        requested = True
        # 任何拿到应答的结果(含策略性跳过)都说明 host 可达,重置连续失败计数
        consecutive_failures[host] = 0
        if evidence is not None:
            results.append(evidence)
    return results
