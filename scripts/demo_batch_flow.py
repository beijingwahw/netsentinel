# -*- coding: utf-8 -*-
"""净网哨兵(NetSentinel)V6 批量流程离线演示脚本 —— A120(V6 的门面)。

在完全离线的环境中演示 V6 批量案件流水线的**全链路十步**:

    ① bulk 清单加载(A107:TXT 8 条 URL,# 注释 + 1 条故意拒绝项)
    ② plan_scan 扫描规划(A107 + A103 canonical:同站镜像归并去重)
    ③ batch_scan 批量扫描(A108:真扫本地,chromium 可用则真截图)
    ④ group_and_enqueue 归组入列(A104/A105/A108:8 个 URL → 3 个案件组)
    ⑤ 合并证据包(A11 组级打包 + A109 merge_bundles 跨包并证演示)
    ⑥ BatchTUI 脚本化(A111:io 注入 groups → attest×3(Y)→ ready → queue-batch)
    ⑦ run_batch dry_run 顺序执行(A112 + A113:注入/真链,逐条人工门提示)
    ⑧ batch_report 结案报告(A114:HTML 生成路径)
    ⑨ telemetry.snapshot() 尾节(V5 可观测性)
    ⑩ 收尾:全程零外呼、零真实提交;真实批量指引(batch_tui 声明 →
       batchflow --resume --exec)

演示站点设计(为什么是"三个域名 + 本机改写"):
    离线环境无法解析真实域名,而 canonical(A103)按可注册域归并时**只看
    host 不看路径**——若三个"域名"仅以 127.0.0.1 的路径区分,8 个 URL 会
    塌缩成 1 个组,演示不出"8 URL → 3 组"。因此本脚本用三个 IANA 保留域名
    a.example / b.example / c.example(RFC 2606,公网永不解析)作为站点标识,
    并在**抓取注入层**把这三个演示域名改写到 127.0.0.1 上的三个本地静态服务:

    - 改写发生在 fetcher/browser 的 allow_network 安全闸门**之前**,闸门
      看到的目标永远是 127.0.0.1(cfg.allow_network 全程保持默认 False);
    - 除这三个演示域名外,任何 URL 原样透传给闸门(闸门照常拦截);
    - 演示域名自身永不尝试 DNS 解析,零外呼承诺不受影响。

    每个域名含 index.html + second_page.html + 2 张 nsfw_hi PNG + 1 张
    normal PNG;a、b 另有 /mirror/ 镜像路径变体——共 8 个入口 URL。三个
    站点的图片用不同颜色生成(make_png),sha256 互不相同,避免 group_linker
    因图片指纹重叠把三个组误并成一个(真实镜像站则正好相反:同图即同伙)。

安全红线(与 CONTRACTS-V6 §0 一致):
    24. 批量举报仍是"逐条人工门":本脚本仅 dry_run(run_batch 内部对每条
        恒以 auto_confirm=False 调用执行器),不存在任何真实提交路径;
    25. 批量确认声明(留痕):TUI attest 的"我已逐站人工核实(Y/N)"由脚本
        注入 Y 应答(离线演示;生产必须真人逐组操作),声明落 sqlite+审计;
    26. 批量频控不放宽:演示用真实 RateLimiter(dry_run 不记账、不等待)。

兄弟模块未就位时,对应环节打印"模块未就位"并跳过(退出码仍 0),不栈崩溃。

用法:python scripts/demo_batch_flow.py
退出码:0=演示完成(含环节级跳过);1=准备阶段失败(临时目录/本地服务)。
"""
from __future__ import annotations

import contextlib
import dataclasses
import gc
import importlib
import importlib.util
import io
import shutil
import struct
import sys
import tempfile
import threading
import unicodedata
import zlib
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit, urlunsplit

#: 项目根目录(脚本位于 <root>/scripts/)
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from netsentinel import telemetry  # noqa: E402
from netsentinel.contracts import Config  # noqa: E402

#: 判定结果的中文标签(演示打印用)
_VERDICT_CN = {"clean": "无风险", "suspect": "疑似", "nsfw": "高置信色情"}

#: 演示域名规格:(域名, 是否带镜像路径变体, 2 张 nsfw 颜色, 1 张 normal 颜色)
#: 颜色互不相同 → PNG 字节互不相同 → sha256 互不相同(防组间团伙误并)。
_SITE_SPECS: list[tuple[str, bool, list[tuple[int, int, int]], tuple[int, int, int]]] = [
    ("a.example", True, [(166, 28, 60), (198, 60, 92)], (92, 120, 168)),
    ("b.example", True, [(22, 102, 74), (52, 138, 98)], (168, 142, 52)),
    ("c.example", False, [(94, 44, 150), (122, 78, 176)], (52, 128, 128)),
]

#: nsfw / normal 演示图尺寸(均 ≥ cfg.min_image_px=200,保证参与判定)
_NSFW_PX = (320, 240)
_NORMAL_PX = (240, 200)

#: TUI 声明的演示审核人(离线演示;生产必须真人)
_DEMO_REVIEWER = "演示审核员"


# ---------------------------------------------------------------------------
# 环节级容错:模块缺失 / 单环节异常 → 打印并跳过,退出码保持 0
# ---------------------------------------------------------------------------
class _ModuleMissing(RuntimeError):
    """兄弟模块未就位(中文消息),由 _stage 捕获后跳过对应环节。"""


def _need(module_name: str, attr: str = "") -> Any:
    """惰性导入兄弟模块(或其属性);未就位抛 :class:`_ModuleMissing`(中文)。"""
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise _ModuleMissing(f"模块 {module_name} 未就位({exc})") from exc
    if attr:
        try:
            return getattr(module, attr)
        except AttributeError as exc:
            raise _ModuleMissing(f"模块 {module_name} 未提供 {attr}") from exc
    return module


@contextlib.contextmanager
def _stage_ctx(
    index: str, title: str, skipped: list[str]
) -> Iterator[None]:
    """环节上下文管理器:打印环节头;模块缺失/异常 → 中文提示并入跳过清单。"""
    print()
    print(f"{index} {title}")
    print("─" * 60)
    try:
        yield
    except _ModuleMissing as exc:
        print(f"  ▶ 模块未就位,跳过本环节:{exc}")
        skipped.append(f"{index} {title}")
    except Exception as exc:  # noqa: BLE001 - 环节级兜底:演示不栈崩溃
        print(f"  ▶ 环节失败,已跳过:{type(exc).__name__}:{exc}")
        skipped.append(f"{index} {title}")
    else:
        print("  ▶ 环节完成。")


# ---------------------------------------------------------------------------
# 中文表格与输出辅助(与 demo_stub_scan / A78 同款,纯标准库)
# ---------------------------------------------------------------------------
def _ensure_utf8_stdio() -> None:
    """Windows 管道/终端非 UTF-8 时切换标准输出编码,避免中文打印报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and stream.encoding and stream.encoding.lower() not in (
                "utf-8",
                "utf8",
            ):
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 重新配置失败不影响主流程
            pass


def _disp_width(text: str) -> int:
    """中文等东亚宽字符按 2 列计的显示宽度。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(text))


def _pad(text: Any, width: int) -> str:
    """按显示宽度右补空格,用于中文表格对齐。"""
    text = str(text)
    return text + " " * max(0, width - _disp_width(text))


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    """渲染一张按显示宽度对齐的中文表格(无第三方依赖)。"""
    widths = [_disp_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _disp_width(str(cell)))
    lines = [
        "  ".join(_pad(h, widths[i]) for i, h in enumerate(headers)).rstrip(),
        "  ".join("-" * w for w in widths),
    ]
    for row in rows:
        lines.append(
            "  ".join(_pad(str(c), widths[i]) for i, c in enumerate(row)).rstrip()
        )
    return "\n".join(lines)


def _print_telemetry_tail() -> None:
    """⑨ telemetry.snapshot() 中文尾节:计数器/仪表 + 各阶段耗时(次数/平均/p95/最大)。"""
    snap = telemetry.snapshot()
    print()
    print("── 遥测快照(telemetry.snapshot,V5 可观测)──────────")
    counters = snap.get("counters") or {}
    gauges = snap.get("gauges") or {}
    timers = snap.get("timers") or {}
    if not (counters or gauges or timers):
        print("  本进程尚未记录任何遥测指标。")
        return
    if counters or gauges:
        rows = [[name, f"{value:g}"] for name, value in {**counters, **gauges}.items()]
        print("  计数器 / 仪表:")
        print(_table(["指标", "值"], rows))
    if timers:
        rows = [
            [
                name,
                f"{stats['count']}",
                f"{stats['avg_ms']:.1f}",
                f"{stats['p95_ms']:.1f}",
                f"{stats['max_ms']:.1f}",
            ]
            for name, stats in timers.items()
        ]
        print("  耗时统计(毫秒):")
        print(_table(["阶段(timer)", "次数", "平均", "p95", "最大"], rows))


# ---------------------------------------------------------------------------
# 纯标准库 PNG 生成(复用 scripts/make_png.py;加载失败时用同语义内联兜底)
# ---------------------------------------------------------------------------
def _load_make_png() -> Callable[[int, int, tuple[int, int, int]], bytes]:
    """按路径复用 scripts/make_png.py 的 ``make_png``;失败时用内联同语义实现。"""
    make_png_path = ROOT / "scripts" / "make_png.py"
    try:
        spec = importlib.util.spec_from_file_location("netsentinel_demo_make_png", make_png_path)
        if spec is not None and spec.loader is not None:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            fn: Callable[[int, int, tuple[int, int, int]], bytes] = module.make_png
            fn(*_NSFW_PX, (1, 2, 3))  # 探测一次,确认可调用
            return fn
    except Exception:  # noqa: BLE001 - 任何加载失败都走内联兜底
        pass

    def _fallback_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
        def chunk(tag: bytes, data: bytes) -> bytes:
            crc = zlib.crc32(tag + data) & 0xFFFFFFFF
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        scanline = b"\x00" + bytes(rgb) * width
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(scanline * height, 9))
            + chunk(b"IEND", b"")
        )

    return _fallback_png


def _png_size(path: Path) -> tuple[int, int]:
    """读取 PNG IHDR 宽高(仅标准库);非 PNG / 失败返回 (0, 0)。"""
    try:
        with open(path, "rb") as fh:
            header = fh.read(24)
    except OSError:
        return (0, 0)
    if (
        len(header) >= 24
        and header[:8] == b"\x89PNG\r\n\x1a\n"
        and header[12:16] == b"IHDR"
    ):
        width, height = struct.unpack(">II", header[16:24])
        return (int(width), int(height))
    return (0, 0)


# ---------------------------------------------------------------------------
# 演示站点:目录构建 + 本机静态服务(全部绑定 127.0.0.1)
# ---------------------------------------------------------------------------
class _QuietHandler(SimpleHTTPRequestHandler):
    """静态文件服务:关闭访问日志,避免污染演示输出。"""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


@dataclass
class _Site:
    """一个演示"域名":本机端口 + 目录 + 入口 URL 清单。"""

    name: str  # 演示域名(a.example)
    port: int
    server: ThreadingHTTPServer
    thread: threading.Thread
    urls: list[str]  # 入口 URL(主站 / 第二页 / 镜像)

    @property
    def base(self) -> str:
        return f"http://{self.name}:{self.port}"


_PAGE_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>演示站点 {name}{suffix}</title>
</head>
<body>
  <h1>演示站点 {name}{suffix}(NetSentinel V6 批量流程离线演示夹具)</h1>
  <p>
    本页面对应本机 127.0.0.1 上的临时静态服务,不对应任何真实网站。
    页面中的"情色"字样用于触发文本线索;图片文件名中的 nsfw_hi /
    normal 关键词供桩分类器(stub)按契约打分。
  </p>
  <img src="{p}nsfw_hi_{tag}1.png" alt="演示图 1">
  <img src="{p}nsfw_hi_{tag}2.png" alt="演示图 2">
  <img src="{p}normal_{tag}.png" alt="正常演示图">
  <p><a href="{back}">{back_text}</a></p>
</body>
</html>
"""


def _build_site_dirs(temp_root: Path, make_png: Callable[[int, int, tuple[int, int, int]], bytes]) -> Path:
    """生成三个演示域名的站点目录(各自独立 PNG,sha256 互不相同)。"""
    sites_root = temp_root / "sites"
    sites_root.mkdir(parents=True, exist_ok=True)
    for name, has_mirror, nsfw_colors, normal_color in _SITE_SPECS:
        tag = name[0]  # a / b / c
        site_dir = sites_root / name
        site_dir.mkdir(parents=True, exist_ok=True)
        for idx, color in enumerate(nsfw_colors, start=1):
            (site_dir / f"nsfw_hi_{tag}{idx}.png").write_bytes(
                make_png(_NSFW_PX[0], _NSFW_PX[1], color)
            )
        (site_dir / f"normal_{tag}.png").write_bytes(
            make_png(_NORMAL_PX[0], _NORMAL_PX[1], normal_color)
        )
        # 主站:index(→第二页)与 second_page(→回主页)
        (site_dir / "index.html").write_text(
            _PAGE_TMPL.format(name=name, suffix="", p="", tag=tag, back="second_page.html", back_text="进入第二页"),
            encoding="utf-8",
        )
        (site_dir / "second_page.html").write_text(
            _PAGE_TMPL.format(name=name, suffix=" · 第二页", p="", tag=tag, back="index.html", back_text="返回主页"),
            encoding="utf-8",
        )
        # 镜像路径变体(a、b):引用上级目录的同一批图片(真实镜像站的同图复用)
        if has_mirror:
            mirror_dir = site_dir / "mirror"
            mirror_dir.mkdir(parents=True, exist_ok=True)
            (mirror_dir / "index.html").write_text(
                _PAGE_TMPL.format(
                    name=name, suffix=" · 镜像", p="../", tag=tag,
                    back="second_page.html", back_text="进入镜像第二页",
                ),
                encoding="utf-8",
            )
            (mirror_dir / "second_page.html").write_text(
                _PAGE_TMPL.format(
                    name=name, suffix=" · 镜像第二页", p="../", tag=tag,
                    back="index.html", back_text="返回镜像主页",
                ),
                encoding="utf-8",
            )
    return sites_root


def _start_sites(sites_root: Path) -> list[_Site]:
    """为每个演示域名在 127.0.0.1 随机端口起一个静态服务。"""
    sites: list[_Site] = []
    for name, has_mirror, _colors, _normal in _SITE_SPECS:
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(_QuietHandler, directory=str(sites_root / name))
        )
        thread = threading.Thread(
            target=server.serve_forever, daemon=True, name=f"demo-http-{name}"
        )
        thread.start()
        port = int(server.server_address[1])
        urls = [f"http://{name}:{port}/", f"http://{name}:{port}/second_page.html"]
        if has_mirror:
            urls.append(f"http://{name}:{port}/mirror/")
        sites.append(_Site(name=name, port=port, server=server, thread=thread, urls=urls))
    return sites


def _stop_sites(sites: list[_Site]) -> None:
    """停掉全部演示静态服务(幂等容忍)。"""
    for site in sites:
        try:
            site.server.shutdown()
            site.server.server_close()
            site.thread.join(timeout=5)
        except Exception:  # noqa: BLE001 - 收尾清理失败不影响退出码
            pass


# ---------------------------------------------------------------------------
# 演示域名 → 127.0.0.1 的抓取改写(发生在 allow_network 闸门之前)
# ---------------------------------------------------------------------------
class _HostRewriter:
    """把演示域名(a.example 等)的 URL 改写为 127.0.0.1 同端口形式。

    - ``to_local``:演示域名 → http://127.0.0.1:PORT/…(供安全闸门/浏览器);
    - ``to_demo``:本机形式 → 演示域名形式(供报告/证据里的 URL 还原展示)。
    两个方向都只处理本演示已知的主机,其余 URL 原样返回(闸门照常生效)。
    """

    def __init__(self, sites: list[_Site]) -> None:
        self._hosts = {site.name for site in sites}
        self._local_prefixes = {
            f"http://127.0.0.1:{site.port}": f"http://{site.name}:{site.port}"
            for site in sites
        }

    def to_local(self, url: str) -> str:
        """演示域名 URL → 127.0.0.1 URL;非演示域名原样返回。"""
        try:
            parts = urlsplit(str(url))
            host = (parts.hostname or "").lower()
            port = f":{parts.port}" if parts.port else ""
        except ValueError:
            return str(url)
        if host not in self._hosts:
            return str(url)
        return urlunsplit(
            (parts.scheme or "http", f"127.0.0.1{port}", parts.path, parts.query, parts.fragment)
        )

    def to_demo(self, url: str) -> str:
        """127.0.0.1:演示端口 URL → 演示域名 URL;其余原样返回。"""
        text = str(url)
        for local, demo in self._local_prefixes.items():
            if text.startswith(local):
                return demo + text[len(local):]
        return text


# ---------------------------------------------------------------------------
# 扫描注入:保留文件名的下载器 + 改写代理的 fetch/capture(离线桩链路专用)
# ---------------------------------------------------------------------------
def _make_preserving_download(
    fetcher_module: Any, rewriter: _HostRewriter
) -> Callable[..., list[Any]]:
    """构造注入 capture_page 的图片下载函数(离线演示链路专用)。

    与 demo_stub_scan 同款:真实下载完成后 ① 把文件改回 URL 中的原始文件名
    (桩分类器按文件名关键词打分)② 从 PNG 头补全宽高(判定公式按
    min_image_px 过滤小图);另把证据里的 URL 还原为演示域名形式(报告可读)。
    不发起任何额外网络请求,重命名只发生在本地证据目录内。
    """

    def download(urls: list[str], source_page: str, dest_dir: str, cfg: Config) -> list[Any]:
        local_urls = [rewriter.to_local(u) for u in urls]
        evidences = fetcher_module.download_images(local_urls, source_page, dest_dir, cfg)
        adjusted: list[Any] = []
        for evidence in evidences:
            name = PurePosixPath(urlsplit(evidence.url).path).name
            target = Path(dest_dir) / (name or "image.bin")
            if name and Path(evidence.path) != target:
                Path(evidence.path).replace(target)
            width, height = _png_size(target)
            adjusted.append(
                dataclasses.replace(
                    evidence,
                    path=str(target),
                    url=rewriter.to_demo(evidence.url),
                    source_page=rewriter.to_demo(source_page),
                    width=width or int(evidence.width or 0),
                    height=height or int(evidence.height or 0),
                )
            )
        return adjusted

    return download


def _make_scan_injections(rewriter: _HostRewriter) -> dict[str, Any]:
    """构造传给 run_scan / batch_scan 的注入件(fetch_page / capture / run_scan)。"""
    fetcher_module = _need("netsentinel.crawler.fetcher")
    browser_module = _need("netsentinel.crawler.browser")
    orchestrator = _need("netsentinel.pipeline.orchestrator")
    download = _make_preserving_download(fetcher_module, rewriter)

    def fetch_page(url: str, cfg: Config) -> tuple[int, str, str]:
        status, html, final_url = fetcher_module.fetch_page(rewriter.to_local(url), cfg)
        # final_url 还原为演示域名形式:站点地图 BFS 以"与起点同 host"筛链接,
        # 若把 127.0.0.1 形式的最终 URL 交回去,相对链接会被解析到别的 host 而丢失。
        return status, html, rewriter.to_demo(final_url)

    def capture(url: str, cfg: Config, *, fetch_page: Any = None) -> Any:
        # 签名与 orchestrator.run_scan 的 CaptureFn 约定一致(fetch_page 可注入);
        # 对浏览器/闸门始终提供 127.0.0.1 形式,对报告还原演示域名形式。
        sample = browser_module.capture_page(
            rewriter.to_local(url), cfg, fetch_page=fetch_page, download=download
        )
        return dataclasses.replace(sample, url=str(url))

    def run_scan(url: str, cfg: Config) -> Any:
        return orchestrator.run_scan(url, cfg, fetch_page=fetch_page, capture=capture)

    return {"fetch_page": fetch_page, "capture": capture, "run_scan": run_scan}


def _chromium_launchable() -> bool:
    """探测 playwright + chromium 是否可用(本机探测,离线)。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001 - 未安装 playwright 视为不可用
        return False
    try:
        pw = sync_playwright().start()
    except Exception:  # noqa: BLE001
        return False
    try:
        browser = pw.chromium.launch(headless=True)
    except Exception:  # noqa: BLE001 - 浏览器二进制未下载等
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass
        return False
    try:
        browser.close()
    finally:
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass
    return True


def _force_degraded_capture() -> None:
    """让 capture_page 进入退化模式:不启动浏览器,仅抓 HTML(无截图)。"""
    browser_module = importlib.import_module("netsentinel.crawler.browser")
    browser_module._import_sync_playwright = lambda: None  # type: ignore[attr-defined]


class _InlineFuture:
    """已完成的最小 Future 鸭子对象(供 _InlineExecutor 返回)。"""

    def __init__(self, value: Any, exc: BaseException | None) -> None:
        self._value = value
        self._exc = exc

    def done(self) -> bool:
        return True

    def result(self, timeout: float | None = None) -> Any:
        if self._exc is not None:
            raise self._exc
        return self._value


class _InlineExecutor:
    """同步串行执行器:submit 即在**当前线程**执行(演示用)。

    目的:批量扫描仍走真实 ops.pool.run_pool(提交循环 / 背压 / 聚合全部
    真实),但把扫描任务留在主线程执行——playwright 同步 API 在主线程最稳;
    同时天然串行,避免多线程截图与 sqlite 写入竞争。生产环境请用缺省
    ThreadPoolExecutor 并发池。
    """

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> _InlineFuture:
        try:
            return _InlineFuture(fn(*args, **kwargs), None)
        except BaseException as exc:  # noqa: BLE001 - 与 Future.result 语义一致
            return _InlineFuture(None, exc)

    def shutdown(self, wait: bool = True) -> None:  # noqa: ARG002 - 鸭子接口
        return None


def _make_pool_runner() -> Callable[..., dict[str, Any]]:
    """构造传给 batch_scan 的并发池运行器:真 run_pool + 主线程串行 + 零停顿。"""
    pool_module = _need("netsentinel.ops.pool")

    def runner(cfg: Any, items: list[str], **kwargs: Any) -> dict[str, Any]:
        return pool_module.run_pool(
            cfg,
            items,
            executor=_InlineExecutor(),
            workers=1,
            sleep=lambda seconds: None,  # 本机演示无需礼貌停顿
            **kwargs,
        )

    return runner


# ---------------------------------------------------------------------------
# 配置与队列
# ---------------------------------------------------------------------------
def _build_demo_config(temp_root: Path) -> Config:
    """load_config() 出默认配置后,把所有产物路径改指临时目录(演示零残留)。"""
    config_module = _need("netsentinel.config")
    cfg = config_module.load_config()
    data_dir = temp_root / "data"
    cfg.data_dir = str(data_dir)
    cfg.evidence_dir = str(data_dir / "evidence")
    cfg.db_path = str(data_dir / "review_queue.db")
    cfg.audit_path = str(data_dir / "audit.jsonl")
    cfg.log_path = str(data_dir / "logs" / "netsentinel.log")
    cfg.fetch_delay_s = 0.0  # 本机演示无需礼貌间隔
    return cfg


def _snapshot_ids(queue: Any) -> set[int]:
    """复核队列当前全部条目 id 快照(用于识别新增的组级条目)。"""
    return {int(e.id) for e in queue.list()}


def _canonical_of(url: str) -> str:
    """URL → canonical 主名(A103,惰性;未就位回退 host)。"""
    try:
        canonical = importlib.import_module("netsentinel.intel.canonical")
        return str(canonical.canonical_name(url))
    except Exception:  # noqa: BLE001 - 演示兜底:取 host
        try:
            return str(urlsplit(url).hostname or url)
        except ValueError:
            return str(url)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _run_tui_session(cfg: Config, queue: Any, review: Any, group_names: list[str]) -> None:
    """⑥ BatchTUI 脚本化:io 注入回放 groups → attest×3(Y)→ ready → queue-batch(Y)→ quit。"""
    batch_tui_cls = _need("netsentinel.cli.batch_tui", "BatchTUI")

    script_lines = ["groups"]
    for name in group_names:
        script_lines += [f"attest {name} --reviewer {_DEMO_REVIEWER}", "Y"]
    script_lines += ["ready", "queue-batch", "Y", "quit"]
    script_text = "\n".join(script_lines) + "\n"

    print("  脚本输入(io 注入,逐行喂给 TUI 的 stdin):")
    for line in script_lines:
        print(f"    > {line}")
    print()
    print("  ── TUI 会话回放(stdout 捕获后回显)──")

    buffer = io.StringIO()
    tui = batch_tui_cls(
        cfg.db_path, cfg, stdin=io.StringIO(script_text), stdout=buffer, queue=queue, review=review
    )
    rc = tui.run()
    transcript = buffer.getvalue()
    # 回显整理:提示符/问答补换行,并在"(Y/N):"后标注脚本应答,便于阅读。
    transcript = transcript.replace("批量复核> ", "\n批量复核> ").lstrip("\n")
    transcript = transcript.replace("(Y/N): ", "(Y/N)【脚本应答:Y】\n")
    for line in transcript.splitlines():
        print(f"  │ {line}")
    print()
    print(f"  TUI 退出码:{rc}(红线 24:本 TUI 绝不执行真实提交,queue-batch 只演练并打印真实命令)")


def _run_batch_stage(
    cfg: Config,
    queue: Any,
    review: Any,
    temp_root: Path,
) -> dict[str, Any] | None:
    """⑦ run_batch dry_run 顺序执行:真实计划器/执行器(dry_run),A113 批次状态留痕。"""
    run_batch = _need("netsentinel.submit.batch_submit", "run_batch")
    ready_entries = _need("netsentinel.decision.batch_review", "ready_entries")

    items = list(ready_entries(cfg, queue=queue, review=review))
    if not items:
        print("  ▶ 待批量清单为空(声明/确认未完成?),跳过批量执行。")
        return None
    print(f"  待批量清单(ready_entries,approved 且已声明):{len(items)} 条")
    print(
        _table(
            ["序号", "条目", "组名", "门户"],
            [
                [i, it.get("entry_id"), it.get("group_name"), it.get("portal", "12377")]
                for i, it in enumerate(items, start=1)
            ],
        )
    )
    print(f"  批量参数:batch_max_items={cfg.batch_max_items},"
          f"batch_item_interval_s={cfg.batch_item_interval_s}(≥ submit_min_interval_s="
          f"{cfg.submit_min_interval_s},红线 26);dry_run 跳过条间等待")

    # A113 批次状态(未就位 → 不落状态继续演示)
    state: Any = None
    batch_id: Any = "-(未落批次)"
    state_summary: dict[str, Any] | None = None
    batch_state_obj: Any = None
    try:
        batch_state_cls = _need("netsentinel.submit.batch_state", "BatchState")
        batch_state_obj = batch_state_cls(str(Path(cfg.data_dir) / "batch_state.db"))
        batch_id = batch_state_obj.new_batch(items, note="V6 批量流程离线演示(dry_run)")
        state = batch_state_obj.bound(int(batch_id))
        print(f"  A113 批次已登记:batch_id={batch_id}(逐条状态 pending → running → …)")
    except _ModuleMissing as exc:
        print(f"  ▶ 模块未就位,跳过批次状态:{exc}")

    # 执行器与计划器:优先真链(12377 计划 + executor dry_run 干跑);
    # 相关模块未就位时退化为 fake(任务书允许:注入 fake executor 或 dry_run 真跑)。
    runs_dir = temp_root / "runs"
    try:
        plan_12377 = _need("netsentinel.submit.portal_12377", "plan_12377")

        def planner(item: dict[str, Any], cfg_d: Config) -> Any:
            entry = queue.get(int(item.get("entry_id") or 0))
            if entry is None:
                raise RuntimeError(f"复核条目不存在:{item.get('entry_id')}")
            return plan_12377(entry, cfg_d, reporter_name="净网哨兵演示")

        executor_mod = _need("netsentinel.submit.executor_playwright")

        def executor(plan: Any, cfg_d: Config, *, auto_confirm: bool = False,
                     dry_run: bool | None = None, **kw: Any) -> Any:
            return executor_mod.execute(
                plan, cfg_d, auto_confirm=auto_confirm, dry_run=dry_run,
                out_dir=str(runs_dir), **kw
            )

        print("  执行链:真实 plan_12377 + executor_playwright(dry_run:不启动浏览器、不提交)")
    except _ModuleMissing as exc:
        print(f"  ▶ 真实执行链模块未就位({exc}),退化为 fake 注入(离线演练语义)")
        contracts = _need("netsentinel.contracts")

        def planner(item: dict[str, Any], cfg_d: Config) -> Any:  # type: ignore[misc]
            return item

        def executor(plan: Any, cfg_d: Config, *, auto_confirm: bool = False,  # type: ignore[misc]
                     dry_run: bool | None = None, **kw: Any) -> Any:
            return contracts.ExecutionResult(
                ok=True,
                portal="12377",
                notes=["(fake)dry_run 演练:未打开浏览器,人工门(HUMAN_GATE)未触发,未提交"],
            )

    with telemetry.timer("demo.run_batch"):
        summary = run_batch(
            items, cfg, dry_run=True, state=state,
            executor=executor, plan_12377=planner,
        )
    print()
    print(f"  批量结果:提交成功 {summary.get('submitted', 0)} 条,"
          f"失败 {summary.get('failed', 0)} 条,"
          f"频控挂起={'是' if summary.get('rate_limited') else '否'},"
          f"暂停={'是' if summary.get('paused', False) else '否'}")
    rows = [
        [
            r.get("group_name"),
            r.get("entry_id"),
            "✔ 已提交" if r.get("submitted") else ("✘ 失败" if not r.get("ok") else "○ 未提交(dry_run)"),
            r.get("error") or "-",
        ]
        for r in summary.get("results", [])
    ]
    print(_table(["组名", "条目", "结果", "原因"], rows))
    print()
    print("  红线 24 提示:批量只是顺序编排——真实执行时**每一条**的验证码输入与")
    print("  最终确认仍由人工在执行器 HUMAN_GATE 完成;run_batch 对每条恒以")
    print("  auto_confirm=False 调用执行器,本演示全程 dry_run,零真实提交。")
    if state is not None and batch_state_obj is not None:
        try:
            state_summary = dict(batch_state_obj.summary(int(batch_id)))
            counts = {k: v for k, v in state_summary.items() if isinstance(v, int)}
            print()
            print(f"  A113 批次汇总(batch_id={batch_id}):{counts}")
        except Exception as exc:  # noqa: BLE001 - 汇总失败不影响演示
            print(f"  ▶ 批次汇总读取失败(忽略):{type(exc).__name__}:{exc}")
    return {"summary": summary, "batch_id": batch_id, "state_summary": state_summary,
            "batch_state": batch_state_obj}


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001 - 预留参数
    """演示主流程;返回进程退出码。"""
    _ensure_utf8_stdio()
    print("═" * 62)
    print(" 净网哨兵(NetSentinel)V6 批量流程离线演示 —— A120")
    print(" 主题:大批量筛选 → 归纳同名 → 依次批量举报(全程人工门)")
    print(" 安全提示:仅访问 127.0.0.1 演示站点;dry_run;零真实提交")
    print("═" * 62)

    telemetry.reset()  # 尾节 snapshot 只含本轮演示数据
    skipped_stages: list[str] = []
    temp_root = Path(tempfile.mkdtemp(prefix="netsentinel_batch_demo_"))
    sites: list[_Site] = []
    queue: Any = None
    review: Any = None
    batch_state_obj: Any = None
    exit_code = 0
    try:
        # ------------------------------------------------------------------
        # 准备 1/3:演示站点(3 域名 × 主站+第二页,a/b 另有镜像路径,共 8 URL)
        # ------------------------------------------------------------------
        print()
        print("准备 1/3 演示站点:3 个保留域名 × (index + second_page"
              " + 2 张 nsfw_hi PNG + 1 张 normal),a/b 各含 /mirror/ 镜像变体")
        print("─" * 60)
        make_png = _load_make_png()
        sites_root = _build_site_dirs(temp_root, make_png)
        sites = _start_sites(sites_root)
        all_urls: list[str] = [u for site in sites for u in site.urls]
        rewriter = _HostRewriter(sites)
        for site in sites:
            print(f"  {site.base:<38} → 127.0.0.1:{site.port}(本地静态服务)")
        print("  图片:每域名单独生成(颜色互异 → sha256 互异,避免组间团伙误并)")
        print(f"  入口 URL 共 {len(all_urls)} 个:{len(all_urls)} URL → 预期 3 个案件组")

        # ------------------------------------------------------------------
        # 准备 2/3:临时配置(allow_network 默认 False 仅放行本机)
        # ------------------------------------------------------------------
        print()
        print("准备 2/3 临时配置(全部产物路径指向临时目录,演示零残留)")
        print("─" * 60)
        cfg = _build_demo_config(temp_root)
        print(f"  allow_network={cfg.allow_network}(仅放行 127.0.0.1/localhost,保持默认)")
        print(f"  classifier={cfg.classifier}(stub 桩分类器,按文件名关键词打分)")
        print(f"  fetch_delay_s={cfg.fetch_delay_s}(本机演示无需礼貌间隔)")
        print(f"  V6:group_merge_phash_overlap={cfg.group_merge_phash_overlap},"
              f"group_merge_template={cfg.group_merge_template}")
        print(f"  V6:batch_max_items={cfg.batch_max_items},"
              f"batch_item_interval_s={cfg.batch_item_interval_s},"
              f"batch_require_attestation={cfg.batch_require_attestation}")
        print(f"  V6:dry_run_default={cfg.dry_run_default}(默认干跑,不驱动浏览器提交)")

        # ------------------------------------------------------------------
        # 准备 3/3:浏览器探测(chromium 可用则真截图,否则自动退化)
        # ------------------------------------------------------------------
        print()
        print("准备 3/3 浏览器探测")
        print("─" * 60)
        if _chromium_launchable():
            print("  检测到 playwright + chromium:页面将进行真实整页截图")
        else:
            _force_degraded_capture()
            print("  未检测到可用 chromium:capture_page 自动退化为无截图模式(仅抓 HTML)")

        # 共享队列/声明台账(全程复用同一连接;WAL)
        try:
            queue = _need("netsentinel.decision.review_queue", "ReviewQueue")(cfg.db_path)
            review = _need("netsentinel.decision.batch_review", "BatchReview")(
                cfg.db_path, audit_path=cfg.audit_path
            )
        except _ModuleMissing as exc:
            print(f"  ▶ 队列/声明模块未就位:{exc}(依赖队列的环节将跳过)")
            skipped_stages.append("准备(复核队列/声明台账)")

        # 共享站点指纹记忆(A39):由演示自持并在收尾关闭,避免惰性构造的
        # sqlite 连接不关闭 → Windows 清理临时目录时句柄残留(零残留承诺)。
        site_memory: Any = None
        try:
            memory_cls = _need("netsentinel.intel.site_memory", "SiteMemory")
            site_memory = memory_cls(
                str(Path(cfg.data_dir) / "site_memory.db"), ttl_hours=72
            )
        except _ModuleMissing as exc:
            print(f"  ▶ 站点指纹记忆模块未就位(规划/扫描不做未变跳过):{exc}")

        # ①-⑧ 的跨环节状态
        valid: list[str] = []            # ① 清单加载产物
        scan_reports: dict[str, Any] = {}  # ③ 批量扫描捕获的报告
        groups: list[Any] = []           # ④ 案件组
        group_entry_ids: set[int] = set()  # ④ 组级入列条目 id
        batch_outcome: dict[str, Any] | None = None  # ⑦ run_batch 产物

        # ------------------------------------------------------------------
        # ① bulk 清单加载
        # ------------------------------------------------------------------
        bulk_path = temp_root / "urls.txt"
        lines = ["# NetSentinel V6 批量流程离线演示清单(# 开头为注释行)"]
        for site in sites:
            lines.append(f"http://{site.name}:{site.port}/")
            lines.append(f"http://{site.name}:{site.port}/second_page.html")
            if any(u.endswith("/mirror/") for u in site.urls):
                lines.append(f"# {site.name} 镜像路径变体")
                lines.append(f"http://{site.name}:{site.port}/mirror/")
        lines.append("# 故意写入的不支持协议,演示拒绝留痕")
        lines.append("ftp://demo.invalid/x")
        bulk_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with _stage_ctx("[1/10]", "① bulk 清单加载(A107 load_bulk:TXT + 注释 + 拒绝留痕)", skipped_stages):
            load_bulk = _need("netsentinel.ops.bulk_intake", "load_bulk")
            valid, rejected = load_bulk(str(bulk_path))
            print(f"  清单文件:{bulk_path}")
            print(f"  加载完成:合法 URL {len(valid)} 个,拒绝 {len(rejected)} 条")
            for raw, reason in rejected:
                print(f"    拒绝:{raw} —— {reason}")
            for u in valid:
                print(f"    合法:{u}")
            if len(valid) != len(all_urls):
                print(f"  ▶ 注意:合法条数 {len(valid)} 与预期入口数 {len(all_urls)} 不一致(继续演示)")

        # ------------------------------------------------------------------
        # ② plan_scan 扫描规划(canonical 同站归并)
        # ------------------------------------------------------------------
        with _stage_ctx("[2/10]", "② plan_scan 扫描规划(A103 canonical:同站镜像归并去重)", skipped_stages):
            if not valid:
                raise _ModuleMissing("上游 ① 清单加载被跳过,当前没有可规划的 URL")
            plan_scan = _need("netsentinel.ops.bulk_intake", "plan_scan")
            plan = plan_scan(valid, cfg, memory=site_memory)
            print(
                f"  规划结果:待扫 {len(plan['to_scan'])} 个,"
                f"指纹未变跳过 {plan['skipped_unchanged']} 个,"
                f"同站重复 {plan['duplicate_urls']} 条"
            )
            for u in plan["to_scan"]:
                print(f"    待扫:{u}(_canonical = {_canonical_of(u)})")
            print("  说明:second_page 与 /mirror/ 路径变体按可注册域归并计入\"同站重复\"——")
            print("        生产环境只扫 to_scan 即可省流量;本演示为完整展示\"镜像归并\",")
            print("        下一步 batch_scan 改扫全部 8 条(每条产出独立报告供分组)。")

        # ------------------------------------------------------------------
        # ③ batch_scan 批量扫描(真扫本地)
        # ------------------------------------------------------------------
        with _stage_ctx("[3/10]", "③ batch_scan 批量扫描(A108 + A58 run_pool:真扫本地演示站点)", skipped_stages):
            batch_scan = _need("netsentinel.ops.batch_scan", "batch_scan")
            injections = _make_scan_injections(rewriter)
            with telemetry.timer("demo.batch_scan"):
                scanned = batch_scan(
                    all_urls, cfg,
                    run_scan=injections["run_scan"],
                    pool_runner=_make_pool_runner(),
                    memory=site_memory,
                )
            scan_reports.update(scanned.get("reports") or {})
            summary = scanned.get("summary") or {}
            print(
                f"  并发池汇总:total={summary.get('total')} done={summary.get('done')}"
                f" skipped={summary.get('skipped')} failed={summary.get('failed')}"
            )
            rows = [
                [u, _VERDICT_CN.get(v, str(v)) + f"({v})"]
                for u, v in (summary.get("results") or {}).items()
            ]
            print(_table(["URL", "判定"], rows))
            for u, err in summary.get("errors") or []:
                print(f"    失败:{u} —— {err}")
            print(f"  捕获 SiteReport {len(scan_reports)} 份(供归组;失败 URL 不产报告)")

        # ------------------------------------------------------------------
        # ④ group_and_enqueue 归组入列 + 分组表
        # ------------------------------------------------------------------
        with _stage_ctx("[4/10]", "④ group_and_enqueue(A104 canonical 归组 + A105 团伙归并 + A11 组级打包入列)", skipped_stages):
            if queue is None:
                raise _ModuleMissing("复核队列不可用(准备阶段未就位)")
            group_and_enqueue = _need("netsentinel.ops.batch_scan", "group_and_enqueue")
            ids_before = _snapshot_ids(queue)
            grouped = group_and_enqueue(cfg, reports=scan_reports, queue=queue)
            groups = list(grouped.get("groups") or [])
            group_entry_ids = _snapshot_ids(queue) - ids_before
            print(f"  {len(all_urls)} 个 URL 归纳为 {len(groups)} 个案件组"
                  f"(入列 {grouped.get('enqueued', 0)} 条,clean 跳过 {grouped.get('skipped_clean', 0)} 组)")
            rows = [
                [
                    getattr(g, "name", "?"),
                    _VERDICT_CN.get(str(getattr(g, "verdict", "")), str(getattr(g, "verdict", "")))
                    + f"({getattr(g, 'verdict', '')})",
                    f"{float(getattr(g, 'agg_max', 0.0) or 0.0):.2f}",
                    len(getattr(g, "site_urls", []) or []),
                    len(getattr(g, "aliases", []) or []),
                    len(getattr(g, "image_sha_set", []) or []),
                ]
                for g in groups
            ]
            print()
            print(_table(["组名(canonical)", "判定", "agg 最大", "URL 数", "域名数", "图片指纹"], rows))
            print()
            for g in groups:
                print(f"  组 {getattr(g, 'name', '?')}(成员 URL):")
                for u in getattr(g, "site_urls", []) or []:
                    print(f"    - {u}")
            print("  说明:同域下的路径变体(second_page / mirror)按 canonical 归并进同组;")
            print("        组间图片指纹重叠率低于阈值 0.3,无团伙并组(三站图片各自独立)。")
            print(f"  新入列组级条目:{sorted(group_entry_ids)}(状态 pending;组名前缀 [组:名]"
                  f"同时写入证据包 manifest 的 intel.queue_note 留痕)")

        # ------------------------------------------------------------------
        # ⑤ 合并证据包路径(A11 组级打包 + A109 merge_bundles 演示)
        # ------------------------------------------------------------------
        with _stage_ctx("[5/10]", "⑤ 合并证据包(A108 内置 A11 组级打包;A109 merge_bundles 跨包并证演示)", skipped_stages):
            if queue is None:
                raise _ModuleMissing("复核队列不可用(准备阶段未就位)")
            all_entries = queue.list()
            group_entry_by_id = {int(e.id): e for e in all_entries if int(e.id) in group_entry_ids}
            # 组级条目证据包 = A108 对合并报告(A11)打的组级 zip
            for gid in sorted(group_entry_ids):
                entry = group_entry_by_id.get(gid)
                zip_path = getattr(entry, "evidence_zip", "") if entry is not None else ""
                name = _canonical_of(getattr(entry, "site_url", "")) if entry is not None else "?"
                print(f"  案件组 {name:<12} 组级证据包:{zip_path or '(未记录)'}")
            # A109:把第一个组的"成员单站证据包"合并成一份跨包并证的大包
            merge_bundles = _need("netsentinel.evidence.merge_bundles", "merge_bundles")
            if groups:
                first = groups[0]
                first_name = getattr(first, "name", "")
                member_urls = set(getattr(first, "site_urls", []) or [])
                sub_bundles = [
                    SimpleNamespace(
                        site_url=e.site_url,
                        dir_path=e.evidence_zip[:-4],
                        manifest_path=str(Path(e.evidence_zip[:-4]) / "manifest.json"),
                        zip_path=e.evidence_zip,
                    )
                    for e in all_entries
                    if int(e.id) not in group_entry_ids
                    and e.site_url in member_urls
                    and getattr(e, "evidence_zip", "")
                ]
                if sub_bundles:
                    merged = merge_bundles(
                        sub_bundles, cfg.evidence_dir,
                        title=f"案件组 {first_name} 合并证据包(A109 演示)",
                    )
                    print()
                    print(f"  A109 跨包并证:组 {first_name} 的 {len(sub_bundles)} 个单站证据包")
                    print(f"    合并产物:zip={getattr(merged, 'zip_path', '')}")
                    print(f"              目录={getattr(merged, 'dir_path', '')}(manifest + 中文 summary.md)")
                else:
                    print("  ▶ 该组没有可合并的单站子包(跳过 A109 演示)")

        # ------------------------------------------------------------------
        # ⑥ BatchTUI 脚本化(approve + attest + ready + queue-batch)
        # ------------------------------------------------------------------
        with _stage_ctx("[6/10]", "⑥ BatchTUI 脚本化(A111:groups → attest×3(Y)→ ready → queue-batch(Y))", skipped_stages):
            if queue is None or review is None:
                raise _ModuleMissing("复核队列/声明台账不可用(准备阶段未就位)")
            group_names = [str(getattr(g, "name", "")) for g in groups]
            # 前置:机器初筛的组级条目需人工 approve(演示以脚本代行,生产必须真人)
            approved_ids: list[int] = []
            for gid in sorted(group_entry_ids):
                entry = queue.get(int(gid))
                if entry is not None and str(entry.status) == "pending":
                    approved = queue.approve(
                        int(gid),
                        note=f"[组:{_canonical_of(entry.site_url)}] 批量演示:人工确认(机器初筛,人工拍板)",
                    )
                    approved_ids.append(int(approved.id))
            print(f"  前置 approve:组级条目 {approved_ids} 已人工确认"
                  f"(演示以脚本代行;生产须在复核 TUI 逐条人工批准)")
            print(f"  当前声明台账:已声明组 "
                  f"{sum(1 for n in group_names if review.is_attested(n))}/{len(group_names)}")
            if not group_names:
                print("  ▶ 无案件组(上游环节被跳过?),TUI 演示从略")
            else:
                _run_tui_session(cfg, queue, review, group_names)

        # ------------------------------------------------------------------
        # ⑦ run_batch dry_run 顺序执行
        # ------------------------------------------------------------------
        with _stage_ctx("[7/10]", "⑦ run_batch dry_run(A112 顺序逐条 + A113 批次状态;每条仍需人工门)", skipped_stages):
            if queue is None or review is None:
                raise _ModuleMissing("复核队列/声明台账不可用(准备阶段未就位)")
            batch_outcome = _run_batch_stage(cfg, queue, review, temp_root)
            if batch_outcome is not None:
                batch_state_obj = batch_outcome.get("batch_state")

        # ------------------------------------------------------------------
        # ⑧ batch_report 结案报告
        # ------------------------------------------------------------------
        with _stage_ctx("[8/10]", "⑧ batch_report 结案报告(A114:HTML 公文风,含逐条状态与声明清单)", skipped_stages):
            if queue is None or review is None:
                raise _ModuleMissing("复核队列/声明台账不可用(准备阶段未就位)")
            render = _need("netsentinel.report.batch_report", "render_batch_report")
            if batch_outcome is not None:
                # 直接复用 ⑦ run_batch 的真实摘要(含 submitted/failed/results/note)
                summary = dict(batch_outcome.get("summary") or {})
                summary["note"] = summary.get("note") or "V6 批量流程离线演示(dry_run)"
                batch_id_text = str(batch_outcome.get("batch_id") or "V6-DEMO")
            else:
                ready_entries = _need("netsentinel.decision.batch_review", "ready_entries")
                items = list(ready_entries(cfg, queue=queue, review=review))
                summary = {
                    "submitted": 0,
                    "failed": 0,
                    "rate_limited": False,
                    "paused": False,
                    "results": [
                        {
                            "entry_id": it.get("entry_id"),
                            "group_name": it.get("group_name"),
                            "ok": True,
                            "submitted": False,
                            "error": "",
                            "portal": it.get("portal", "12377"),
                        }
                        for it in items
                    ],
                    "note": "V6 批量流程离线演示(dry_run)",
                }
                batch_id_text = "V6-DEMO"
            summary["attestations"] = [
                {"group_name": a.group_name, "reviewer": a.reviewer, "ts": a.ts}
                for a in review.list_attestations()
            ]
            out_path = temp_root / "batch_report.html"
            html_text = render(batch_id_text, summary, str(out_path))
            print(f"  结案报告已生成:{out_path}({len(html_text)} 字符 HTML;批次号 {batch_id_text})")
            print("  报告内容:批次信息 / 逐条状态表 / 声明清单 / 结论段"
                  "(含\"每条均经人工门完成验证码\"声明)")

        # ------------------------------------------------------------------
        # ⑨ 遥测尾节
        # ------------------------------------------------------------------
        with _stage_ctx("[9/10]", "⑨ telemetry.snapshot() 尾节(V5 可观测性)", skipped_stages):
            _print_telemetry_tail()

        # ------------------------------------------------------------------
        # ⑩ 收尾
        # ------------------------------------------------------------------
        print()
        print("[10/10] ⑩ 收尾:零外呼 / 零真实提交声明 + 真实批量指引")
        print("─" * 60)
        print("═" * 62)
        print(" 演示完成:全程零外呼(仅访问本机 127.0.0.1)、零真实提交(dry_run)")
        if skipped_stages:
            print(f" 跳过环节({len(skipped_stages)} 个,兄弟模块未就位或环节降级):")
            for name in skipped_stages:
                print(f"   - {name}")
        print(" 真实批量(生产)路径:")
        print("   ① python -m netsentinel.cli.batch_tui    # groups → attest 逐组声明(红线 25)")
        print("   ② python -m netsentinel.batchflow --resume <批次号> --exec")
        print("      # 逐条人工门执行:每条验证码与最终确认均由人工完成(红线 24)")
        print(f" 演示产物(临时目录,随演示结束清理):{temp_root}")
        print("═" * 62)
        return exit_code
    except Exception as exc:  # noqa: BLE001 - 演示脚本顶层兜底,避免栈溢出式报错
        print(f"演示失败:{type(exc).__name__}:{exc}", file=sys.stderr)
        return 1
    finally:
        for closer in (queue, review, batch_state_obj, site_memory):
            close_fn = getattr(closer, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:  # noqa: BLE001 - 收尾清理失败不影响退出码
                    pass
        _stop_sites(sites)
        shutil.rmtree(temp_root, ignore_errors=True)
        # Windows 下个别 sqlite/日志句柄可能延迟释放:强制 GC 后再清一次(零残留)。
        gc.collect()
        shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
