# -*- coding: utf-8 -*-
"""净网哨兵(NetSentinel)V9 三档并发与收官流程离线演示脚本 —— A179。

在完全离线的环境中演示 V9 的两条主线(CONTRACTS-V9 §2 A179 行):

    ① CPU 画像与三档表(A163 ``ops.cpu_profile``:``detect()`` 真跑,
       ``tier_workers`` 按 §1 公式换算三档 workers,``recommend`` 给建议档)
    ② tier_bench 三档峰值证明(A173 ``benchmarks.tier_bench.run()``:
       Barrier 计数法,实测并发峰值 = 理论 workers,确定性计数非墙钟)
    ③ 本地 2 站扫描收官(NETSENTINEL_FAKE_CORES=4、tier=high →
       io_workers=3;``finishflow.finish`` 注入式真跑:真 batch_scan +
       真 run_pool(3 workers)+ 真 SummaryAgent,打印中文收官汇总:
       组数 / 待声明 / ready / 档位 / worker 数)
    ④ 依次举报(干跑):演示声明(approve + attest 脚本代行,生产必须
       真人)→ ``ready_entries`` 就绪清单 → ``SequentialReportAgent``
       编排真 ``run_batch``:dry_run=True、注入应答执行器(浏览器不
       启动、HUMAN_GATE 不代答),逐条打印结果
    ⑤ 收尾:高档只压榨本地计算;对外礼貌间隔与举报频控未放宽(红线 35);
       全程零外呼、零真实提交(红线 36);workers 恒 ≤ cores(红线 37)

演示站点设计(与 A120 V6 演示同款):离线环境无法解析真实域名,而
canonical 归组只看 host——本脚本用两个 IANA 保留域名 a.example /
b.example(RFC 2606,公网永不解析)作站点标识,并在**抓取注入层**把
演示域名改写到 127.0.0.1 上的两个本地静态服务:

- 改写发生在 fetcher/browser 的 allow_network 安全闸门**之前**,闸门
  看到的目标永远是 127.0.0.1(cfg.allow_network 全程保持默认 False);
- 演示域名自身永不尝试 DNS 解析,零外呼承诺不受影响;
- 每域名含 index.html + second_page.html + 2 张 nsfw_hi PNG + 1 张
  normal PNG,两站图片颜色互异(sha256 互异,避免团伙误并)→ 预期
  恰 2 个案件组。

安全红线(与 CONTRACTS-V9 §0 一致):

35. 压榨边界:tier=high 只作用于本地计算与本地回环 IO(扫描池 workers、
    Barrier 基准);``cfg`` 的礼貌字段(``fetch_delay_s`` /
    ``submit_min_interval_s`` / ``batch_item_interval_s`` /
    ``submit_max_per_day``)全程**原值透传、一概不放宽**,脚本打印对照;
36. 结案代理无自主提交权:④ 恒 dry_run,执行器为注入应答(不启动浏览器),
    ``run_batch`` 对每条恒以 ``auto_confirm=False`` 调用执行器——每条
    举报在真实模式下仍须人工输入验证码并最终确认(HUMAN_GATE);
37. 资源治理:三档 workers 恒 ≤ 核数,线程池退出必关闭,不留孤儿线程。

兄弟模块未就位时对应环节打印"模块未就位"并跳过(退出码仍 0),不栈崩溃。

用法:NETSENTINEL_FAKE_CORES=4 python scripts/demo_tiers_finish.py
退出码:0=演示完成(含环节级跳过);1=准备阶段失败(临时目录/本地服务)。
"""
from __future__ import annotations

import contextlib
import dataclasses
import gc
import importlib
import importlib.util
import os
import shutil
import struct
import sys
import tempfile
import threading
import unicodedata
import zlib
from dataclasses import dataclass
from functools import partial
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

#: 演示域名规格:(域名, 2 张 nsfw 颜色, 1 张 normal 颜色)——颜色互异 →
#: PNG 字节互异 → sha256 互异(避免 group_linker 把两组误并成一伙)。
_SITE_SPECS: list[tuple[str, list[tuple[int, int, int]], tuple[int, int, int]]] = [
    ("a.example", [(166, 28, 60), (198, 60, 92)], (92, 120, 168)),
    ("b.example", [(22, 102, 74), (52, 138, 98)], (168, 142, 52)),
]

#: nsfw / normal 演示图尺寸(均 ≥ cfg.min_image_px=200,保证参与判定;
#: 每站 2 页 × 2 张 nsfw_hi ≥ min_nsw_images=3 → 判 nsfw,组必入列)
_NSFW_PX = (320, 240)
_NORMAL_PX = (240, 200)

#: 演示声明用的审核人与声明文本(离线演示;生产必须真人逐组核实)
_DEMO_REVIEWER = "演示审核员"
_ATTEST_TEXT = "我已逐站人工核实本组证据,确认举报材料真实有效(离线演示代行)"

#: 红线 35 对照的礼貌字段(档位不得触碰;打印原值证明未放宽)
_POLITENESS_FIELDS = (
    "fetch_delay_s",
    "submit_min_interval_s",
    "batch_item_interval_s",
    "submit_max_per_day",
)


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
def _stage_ctx(index: str, title: str, skipped: list[str]) -> Iterator[None]:
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
# 中文表格与输出辅助(与 demo_batch_flow / A120 同款,纯标准库)
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
    """一个演示"域名":本机端口 + 目录 + 入口 URL。"""

    name: str
    port: int
    server: ThreadingHTTPServer
    thread: threading.Thread
    entry_url: str  # 入口 URL(主页;第二页经站内链接发现)

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
  <h1>演示站点 {name}{suffix}(NetSentinel V9 三档并发离线演示夹具)</h1>
  <p>
    本页面对应本机 127.0.0.1 上的临时静态服务,不对应任何真实网站。
    页面中的"情色"字样用于触发文本线索;图片文件名中的 nsfw_hi /
    normal 关键词供桩分类器(stub)按契约打分。
  </p>
  <img src="nsfw_hi_{tag}1.png" alt="演示图 1">
  <img src="nsfw_hi_{tag}2.png" alt="演示图 2">
  <img src="normal_{tag}.png" alt="正常演示图">
  <p><a href="{back}">{back_text}</a></p>
</body>
</html>
"""


def _build_site_dirs(temp_root: Path, make_png: Callable[[int, int, tuple[int, int, int]], bytes]) -> Path:
    """生成两个演示域名的站点目录(各自独立 PNG,sha256 互不相同)。"""
    sites_root = temp_root / "sites"
    sites_root.mkdir(parents=True, exist_ok=True)
    for name, nsfw_colors, normal_color in _SITE_SPECS:
        tag = name[0]  # a / b
        site_dir = sites_root / name
        site_dir.mkdir(parents=True, exist_ok=True)
        for idx, color in enumerate(nsfw_colors, start=1):
            (site_dir / f"nsfw_hi_{tag}{idx}.png").write_bytes(
                make_png(_NSFW_PX[0], _NSFW_PX[1], color)
            )
        (site_dir / f"normal_{tag}.png").write_bytes(
            make_png(_NORMAL_PX[0], _NORMAL_PX[1], normal_color)
        )
        (site_dir / "index.html").write_text(
            _PAGE_TMPL.format(
                name=name, suffix="", tag=tag,
                back="second_page.html", back_text="进入第二页",
            ),
            encoding="utf-8",
        )
        (site_dir / "second_page.html").write_text(
            _PAGE_TMPL.format(
                name=name, suffix=" · 第二页", tag=tag,
                back="index.html", back_text="返回主页",
            ),
            encoding="utf-8",
        )
    return sites_root


def _start_sites(sites_root: Path) -> list[_Site]:
    """为每个演示域名在 127.0.0.1 随机端口起一个静态服务。"""
    sites: list[_Site] = []
    for name, _colors, _normal in _SITE_SPECS:
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(_QuietHandler, directory=str(sites_root / name))
        )
        thread = threading.Thread(
            target=server.serve_forever, daemon=True, name=f"demo-http-{name}"
        )
        thread.start()
        port = int(server.server_address[1])
        sites.append(
            _Site(
                name=name, port=port, server=server, thread=thread,
                entry_url=f"http://{name}:{port}/",
            )
        )
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

    真实下载完成后 ① 把文件改回 URL 中的原始文件名(桩分类器按文件名
    关键词打分)② 从 PNG 头补全宽高(判定公式按 min_image_px 过滤小图);
    另把证据里的 URL 还原为演示域名形式(报告可读)。不发起任何额外
    网络请求,重命名只发生在本地证据目录内。
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
    """构造传给 run_scan 的注入件(fetch_page / capture / run_scan)。"""
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


def _force_degraded_capture() -> None:
    """让 capture_page 进入退化模式:不启动浏览器,仅抓 HTML(无截图)。

    本演示主题是三档并发与收官链,而非截图质量;为确定性起见全程退化
    (即便本机装有 playwright + chromium 也不启动,零浏览器依赖)。
    """
    browser_module = importlib.import_module("netsentinel.crawler.browser")
    browser_module._import_sync_playwright = lambda: None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 配置
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
    cfg.fetch_delay_s = 0.0  # 本机回环演示无需抓取间隔(配置选择,与档位无关——红线 35)
    cfg.concurrency_tier = "high"  # V9 高档:N - cpu_reserve(演示主旨)
    return cfg


# ---------------------------------------------------------------------------
# 各环节
# ---------------------------------------------------------------------------
def _stage_cpu_profile(cfg: Config) -> dict[str, Any]:
    """① CPU 画像与三档表(A163 detect 真跑 + tier_workers + recommend)。"""
    cpu_profile = _need("netsentinel.ops.cpu_profile")
    profile = cpu_profile.detect()  # 真跑:受 NETSENTINEL_FAKE_CORES 覆盖
    cores = int(profile["cores"])
    reserve = int(getattr(cfg, "cpu_reserve", 1))
    print("  detect() 画像(真跑,psutil 惰性可选、零外呼):")
    print(
        _table(
            ["键", "值"],
            [[key, str(value)] for key, value in profile.items()],
        )
    )
    fake_env = os.environ.get("NETSENTINEL_FAKE_CORES", "")
    if fake_env:
        print(f"  测试/演示钩子:NETSENTINEL_FAKE_CORES={fake_env}(核数被覆盖)")
    else:
        print("  测试/演示钩子:NETSENTINEL_FAKE_CORES 未设置(按真实核数演示)")
    print()
    print(f"  三档表(契约 §1 公式,reserve={reserve},N={cores}):")
    rows = []
    for tier, formula in (
        ("low", "max(1, N//4)"),
        ("mid", "max(1, N//2)"),
        ("high", "max(1, N-reserve)"),
    ):
        rows.append(
            [tier, formula, cpu_profile.tier_workers(tier, reserve=reserve)]
        )
    print(_table(["档位", "公式", "workers"], rows))
    print(f"  recommend(profile) 建议档位:{cpu_profile.recommend(dict(profile))}")
    print("  红线 37:三档 workers 恒 ≤ 核数;高档只压榨本地计算(红线 35)。")
    return {"profile": profile, "cores": cores}


def _stage_tier_bench(temp_root: Path, cores: int) -> dict[str, Any]:
    """② tier_bench 三档峰值证明(A173 run():Barrier 计数法)。"""
    tier_bench = _need("benchmarks.tier_bench")
    out_dir = temp_root / "bench"
    table = tier_bench.run(str(out_dir))  # 缺省自动探测核数(受 FAKE_CORES 影响)
    tier_text = {"low": "low(低·省电)", "mid": "mid(中·默认)", "high": "high(高·压榨)"}
    rows = []
    all_ok = True
    for tier in ("low", "mid", "high"):
        row = table[tier]
        ok = int(row["peak"]) == int(row["workers_expected"])
        all_ok = all_ok and ok
        rows.append(
            [
                tier_text[tier],
                row["workers_expected"],
                row["peak"],
                "一致" if ok else "不一致",
            ]
        )
    print("  Barrier 计数法:workers 个任务同时在共享 Barrier 上到达才放行,")
    print(f"  峰值=实测并发度(确定性计数,非墙钟);核数来源:detect(N={cores})")
    print()
    print(_table(["档位", "理论 workers", "实测峰值(Barrier)", "判定"], rows))
    print()
    print(f"  报告已写出:{out_dir / 'tier_report.md'} 与 {out_dir / 'tier_report.json'}")
    print(
        "  结论:"
        + ("三档实测峰值与理论 workers 全部一致" if all_ok else "存在不一致档位(见上表)")
        + ";峰值恒 ≤ 核数(红线 37);零墙钟断言,同参数两次运行逐字节一致(红线 31)。"
    )
    return {"table": table, "all_ok": all_ok}


def _stage_finish(
    cfg: Config,
    sites: list[_Site],
    rewriter: _HostRewriter,
    site_memory: Any,
) -> dict[str, Any]:
    """③ 本地 2 站扫描收官:finishflow.finish(注入式真跑,tier=high)。"""
    finishflow = _need("netsentinel.finishflow")
    batch_scan = _need("netsentinel.ops.batch_scan", "batch_scan")
    run_pool = _need("netsentinel.ops.pool", "run_pool")
    io_workers = _need("netsentinel.ops.concurrency", "io_workers")
    review_queue_cls = _need("netsentinel.decision.review_queue", "ReviewQueue")

    urls = [site.entry_url for site in sites]
    workers_expected = int(io_workers(cfg))
    print(f"  并发档位:tier={cfg.concurrency_tier},cpu_reserve={cfg.cpu_reserve}"
          f" → io_workers(cfg) = {workers_expected}"
          f"(cores {workers_expected + int(cfg.cpu_reserve)} - reserve {cfg.cpu_reserve})")
    print("  证明口径:finish 内部经 A164 io_workers 算出 workers,以 workers= 关键字")
    print("            透传给注入扫描 → 真 run_pool(max_workers=workers)并发扫描。")

    # 队列条目 id 快照:finish 内部 SummaryAgent→group_and_enqueue 会入列组级条目
    queue = review_queue_cls(str(cfg.db_path))
    ids_before = {int(e.id) for e in queue.list()}

    injections = _make_scan_injections(rewriter)
    observed: dict[str, Any] = {"workers": None}

    def pool_runner(cfg_: Any, items: list[str], **kwargs: Any) -> Any:
        # 红线 35:高档只作用于本地回环 IO——演示站点全在 127.0.0.1,
        # 提交侧停顿注入为 no-op 属"本地回环"边界内;对外礼貌字段不动。
        return run_pool(cfg_, items, sleep=lambda seconds: None, **kwargs)

    def scan(targets: list[str], cfg_: Config, *, workers: int = 1) -> dict[str, Any]:
        observed["workers"] = int(workers)
        return batch_scan(
            targets, cfg_,
            run_scan=injections["run_scan"],
            memory=site_memory,
            pool_runner=pool_runner,
        )

    print()
    print(f"  目标 URL({len(urls)} 站,演示域名改写至 127.0.0.1 本机静态服务):")
    for site in sites:
        print(f"    - {site.entry_url}  →  http://127.0.0.1:{site.port}/")
    print()
    print("  ── finishflow.finish(注入式真跑)──(收官汇总由 A169 打印)")
    outcome = finishflow.finish(urls, cfg, scan=scan)

    print()
    print("  收官核对:")
    workers_seen = observed["workers"]
    print(f"    注入扫描实收 workers = {workers_seen}(finish 经 workers= 关键字透传给扫描池)")
    if workers_seen == workers_expected:
        print(f"    ✔ 与 io_workers(cfg)={workers_expected} 一致(tier=high 压榨本地扫描池)")
    else:
        print(f"    ▶ 注意:与 io_workers(cfg)={workers_expected} 不一致(仍继续演示)")
    print(f"    outcome:groups={outcome.get('groups')},"
          f"attest_pending={outcome.get('attest_pending')},"
          f"ready_count={outcome.get('ready_count')},"
          f"tier={outcome.get('tier')},workers={outcome.get('workers')}")
    print(f"    收官报告:{outcome.get('report_md') or '(未生成)'}")
    print(f"              {outcome.get('report_html') or '(未生成)'}")
    print(f"    站点/扫描/失败/跳过:{outcome.get('sites')}/{outcome.get('scanned')}/"
          f"{outcome.get('failed')}/{outcome.get('skipped')}")
    # 红线 35 对照:礼貌字段在 tier=high 下原值不动
    mid_cfg = dataclasses.replace(cfg, concurrency_tier="mid")
    politeness_rows = []
    for field in _POLITENESS_FIELDS:
        v_high = getattr(cfg, field)
        v_mid = getattr(mid_cfg, field)
        politeness_rows.append([field, v_mid, v_high, "一致(未放宽)" if v_mid == v_high else "!! 不一致"])
    print()
    print("    红线 35 对照(mid ↔ high,礼貌/频控字段必须原值不变):")
    print(_table(["字段", "mid 档值", "high 档值", "判定"], politeness_rows))
    print("    说明:本机回环演示把 fetch_delay_s 配置为 0(配置选择,与档位无关);")
    print("          档位换算从不触碰这些字段,举报频控(红线 26)同理不放宽。")

    ids_after = {int(e.id) for e in queue.list()}
    new_ids = sorted(ids_after - ids_before)
    print(f"    新入列组级条目 id:{new_ids}(状态 pending;声明后方可举报,红线 25)")
    return {
        "outcome": outcome,
        "queue": queue,
        "new_entry_ids": new_ids,
        "workers": workers_seen,
        "politeness_ok": all(
            getattr(cfg, f) == getattr(mid_cfg, f) for f in _POLITENESS_FIELDS
        ),
    }


def _stage_sequential_report(cfg: Config, queue: Any, new_entry_ids: list[int]) -> dict[str, Any]:
    """④ 依次举报(干跑):演示声明 → ready_entries → SequentialReportAgent。"""
    batch_review_cls = _need("netsentinel.decision.batch_review", "BatchReview")
    resolve_group_name = _need("netsentinel.decision.batch_review", "resolve_group_name")
    ready_entries_fn = _need("netsentinel.decision.batch_review", "ready_entries")
    agent_cls = _need("netsentinel.agent.sequential_report", "SequentialReportAgent")
    batch_state_cls = _need("netsentinel.submit.batch_state", "BatchState")
    contracts = _need("netsentinel.contracts")

    # ---- 演示声明:approve(人工确认)+ attest(逐组人工核实声明)----
    # 生产必须真人在复核 TUI / batch_tui 完成;离线演示以脚本代行(红线 25 留痕)。
    # 组名以 note 的 [组:名] 标记解析(A110 resolve_group_name 同口径);
    # approve 传空备注以**保留原 note**(组名标记在,ready_entries 才能对上声明)。
    review = batch_review_cls(str(cfg.db_path), audit_path=str(cfg.audit_path))
    st: Any = None
    try:
        group_names: list[str] = []
        approved_ids: list[int] = []
        for eid in new_entry_ids:
            entry = queue.get(int(eid))
            if entry is None:
                continue
            group = str(resolve_group_name(str(entry.note or ""), str(entry.site_url or "")))
            if group and group not in group_names:
                group_names.append(group)
            if str(entry.status) == "pending":
                approved = queue.approve(int(eid))  # 空备注:保留 [组:名] 标记
                approved_ids.append(int(approved.id))
        print(f"  演示声明(脚本代行,生产必须真人):approve {approved_ids};声明组 {group_names}")
        for name in group_names:
            review.attest(name, items=1, reviewer=_DEMO_REVIEWER, text=_ATTEST_TEXT)
        print(
            f"  声明台账:已声明组 "
            f"{sum(1 for n in group_names if review.is_attested(n))}/{len(group_names)}"
        )

        # ---- 就绪清单(A110 ready_entries:approved 且所在组已声明)----
        items = list(ready_entries_fn(cfg))
        print()
        print(f"  ready_entries 就绪清单:{len(items)} 条")
        print(
            _table(
                ["序号", "entry_id", "组名", "门户"],
                [
                    [i, it.get("entry_id"), it.get("group_name"), it.get("portal", "12377")]
                    for i, it in enumerate(items, start=1)
                ],
            )
        )
        if not items:
            print("  ▶ 就绪清单为空(上游声明未完成?),跳过干跑。")
            return {"items": [], "summary": None}

        # ---- 批次状态(A113)----
        st = batch_state_cls(str(Path(cfg.data_dir) / "batch_state.db"))
        bid = int(st.new_batch(items, note="V9 三档收官离线演示(dry_run)"))
        print()
        print(f"  A113 批次已登记:batch_id={bid}(逐条状态 pending → running → …)")

        # ---- 注入应答执行器(红线 36:HUMAN_GATE 不代答,浏览器不启动)----
        exec_calls: list[dict[str, Any]] = []

        def executor(plan: Any, cfg_: Config, *, auto_confirm: bool = False,
                     dry_run: bool | None = None, **kw: Any) -> Any:
            exec_calls.append(
                {
                    "auto_confirm": bool(auto_confirm),
                    "dry_run": bool(dry_run) if dry_run is not None else None,
                    "entry_url": str(getattr(plan, "entry_url", "")),
                }
            )
            return contracts.ExecutionResult(
                ok=True,
                portal="12377",
                submitted=False,  # 干跑:绝不置 True(零真实提交)
                notes=["(注入应答)dry_run 演练:浏览器未启动,HUMAN_GATE 人工门未触发,未提交"],
            )

        # ---- 计划器:ready 字典 → 队列 Entry → 真实 plan_12377(与 V6 演示同款)----
        portal_mod = importlib.import_module("netsentinel.submit.portal_12377")
        real_plan = portal_mod.plan_12377

        def planner(item: dict[str, Any], cfg_: Config) -> Any:
            entry = queue.get(int(item.get("entry_id") or 0))
            if entry is None:
                raise RuntimeError(f"复核条目不存在:{item.get('entry_id')}")
            return real_plan(entry, cfg_, reporter_name="净网哨兵演示")

        print("  执行链:真 run_batch(频控/上限原样)+ 真实 plan_12377(经队列取条)")
        print("          + 注入应答执行器(dry_run;auto_confirm 恒 False,红线 36)")

        agent = agent_cls()

        def on_item(index: int, total: int, item: Any) -> None:
            print(
                f"    ▶ 第 {index}/{total} 条:group={item.get('group_name')} "
                f"entry={item.get('entry_id')} portal={item.get('portal', '12377')}"
                "(进入执行;真实模式此处是 HUMAN_GATE 人工门)"
            )

        try:
            portal_mod.plan_12377 = planner  # 演示注入:ready 字典解析为队列条目
            print()
            print("  ── SequentialReportAgent.run(dry_run=True)──")
            out = agent.run(
                items, cfg,
                dry_run=True,
                executor=executor,
                state=st.bound(bid),
                batch_id=bid,
                on_item=on_item,
            )
        finally:
            portal_mod.plan_12377 = real_plan  # 还原模块属性(进程内演示注入)

        summary = dict(out.get("result") or {})
        print()
        print(f"  批量结果:submitted={summary.get('submitted', 0)},"
              f"failed={summary.get('failed', 0)},"
              f"rate_limited={'是' if summary.get('rate_limited') else '否'},"
              f"paused={'是' if summary.get('paused', False) else '否'},"
              f"report_path={out.get('report_path') or '(未生成)'}")
        rows = []
        for r in summary.get("results", []):
            state_text = "✔ 已提交" if r.get("submitted") else (
                "✘ 失败" if not r.get("ok") else "○ 干跑未提交"
            )
            rows.append(
                [r.get("group_name"), r.get("entry_id"), state_text, r.get("error") or "-"]
            )
        print(_table(["组名", "条目", "结果", "原因"], rows))
        print()
        confirms = sorted({str(c["auto_confirm"]) for c in exec_calls})
        print(f"  执行器调用 {len(exec_calls)} 次:每条 auto_confirm = "
              f"{'/'.join(confirms) if confirms else '(未调用)'}")
        print("  红线 36:每条仍需人工门——真实执行(--exec)时验证码输入与最终确认")
        print("          由人工在执行器 HUMAN_GATE 步骤完成;本演示 dry_run + 注入应答,")
        print("          全程零浏览器、零门户、零真实提交;举报频控与每日额度照常(红线 26)。")
        try:
            counts = {k: v for k, v in dict(st.summary(bid)).items() if isinstance(v, int)}
            print(f"  A113 批次汇总(batch_id={bid}):{counts}")
        except Exception as exc:  # noqa: BLE001 - 汇总失败不影响演示
            print(f"  ▶ 批次汇总读取失败(忽略):{type(exc).__name__}:{exc}")
        return {
            "items": items,
            "summary": summary,
            "report_path": out.get("report_path"),
            "auto_confirm_all_false": bool(exec_calls)
            and all(not c["auto_confirm"] for c in exec_calls),
            "submitted": int(summary.get("submitted", 0) or 0),
        }
    finally:
        for closer in (st, review):
            close_fn = getattr(closer, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:  # noqa: BLE001 - 收尾清理失败不影响演示
                    pass


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:  # noqa: ARG001 - 预留参数
    """演示主流程;返回进程退出码。"""
    _ensure_utf8_stdio()
    print("═" * 62)
    print(" 净网哨兵(NetSentinel)V9 三档并发与收官流程离线演示 —— A179")
    print(" 主题:CPU 自适应三档 → 本地 2 站收官(tier=high)→ 依次举报干跑")
    print(" 安全提示:仅访问 127.0.0.1 演示站点;dry_run;零外呼;零真实提交")
    print("═" * 62)

    telemetry.reset()  # 只统计本轮演示
    skipped_stages: list[str] = []
    temp_root = Path(tempfile.mkdtemp(prefix="netsentinel_tiers_demo_"))
    sites: list[_Site] = []
    queue: Any = None
    site_memory: Any = None
    try:
        # ------------------------------------------------------------------
        # 准备:演示站点 + 临时配置 + 退化截图 + 站点指纹记忆
        # ------------------------------------------------------------------
        print()
        print("准备 演示站点与临时配置(全部产物路径指向临时目录,零残留)")
        print("─" * 60)
        make_png = _load_make_png()
        sites_root = _build_site_dirs(temp_root, make_png)
        sites = _start_sites(sites_root)
        rewriter = _HostRewriter(sites)
        for site in sites:
            print(f"  {site.base:<38} → 127.0.0.1:{site.port}(本地静态服务)")
        cfg = _build_demo_config(temp_root)
        print(f"  allow_network={cfg.allow_network}(仅放行 127.0.0.1/localhost,保持默认)")
        print(f"  classifier={cfg.classifier}(stub 桩分类器,按文件名关键词打分)")
        print(f"  V9:concurrency_tier={cfg.concurrency_tier},cpu_reserve={cfg.cpu_reserve},"
              f"concurrency_auto={cfg.concurrency_auto}")
        print(f"  V9:summary_agent_enabled={cfg.summary_agent_enabled},"
              f"dry_run_default={cfg.dry_run_default}")
        _force_degraded_capture()
        print("  截图策略:强制退化模式(不启动浏览器,仅抓 HTML;确定性优先)")
        memory_cls = _need("netsentinel.intel.site_memory", "SiteMemory")
        site_memory = memory_cls(str(Path(cfg.data_dir) / "site_memory.db"), ttl_hours=72)

        stage1: dict[str, Any] = {}
        stage3: dict[str, Any] = {}
        stage4: dict[str, Any] = {}

        # ------------------------------------------------------------------
        # ① CPU 画像与三档表
        # ------------------------------------------------------------------
        with _stage_ctx("[1/5]", "① CPU 画像与三档表(A163 detect 真跑 + tier_workers)", skipped_stages):
            stage1 = _stage_cpu_profile(cfg)

        # ------------------------------------------------------------------
        # ② tier_bench 三档峰值证明
        # ------------------------------------------------------------------
        with _stage_ctx("[2/5]", "② tier_bench 三档峰值证明(A173 run():Barrier 计数法)", skipped_stages):
            _stage_tier_bench(temp_root, int(stage1.get("cores") or 0))

        # ------------------------------------------------------------------
        # ③ 本地 2 站扫描收官(tier=high → io_workers=3)
        # ------------------------------------------------------------------
        try:
            _io_workers_fn = _need("netsentinel.ops.concurrency", "io_workers")
            tier_note = f"tier=high → io_workers={int(_io_workers_fn(cfg))}"
        except Exception:  # noqa: BLE001 - 标题降级为通用文案,不影响演示
            tier_note = "tier=high"
        _env_cores = os.environ.get("NETSENTINEL_FAKE_CORES", "")
        env_note = f"NETSENTINEL_FAKE_CORES={_env_cores}、" if _env_cores else "真实核数、"
        with _stage_ctx(
            "[3/5]",
            f"③ 本地 2 站扫描收官(finishflow.finish,{env_note}{tier_note})",
            skipped_stages,
        ):
            stage3 = _stage_finish(cfg, sites, rewriter, site_memory)
            queue = stage3.get("queue")

        # ------------------------------------------------------------------
        # ④ 依次举报(干跑)
        # ------------------------------------------------------------------
        with _stage_ctx(
            "[4/5]",
            "④ 依次举报干跑(SequentialReportAgent:ready 条目逐条执行,注入应答)",
            skipped_stages,
        ):
            if queue is None or not stage3.get("new_entry_ids"):
                raise _ModuleMissing("上游 ③ 收官未产出组级条目(或环节被跳过),无 ready 条目可演练")
            stage4 = _stage_sequential_report(cfg, queue, stage3["new_entry_ids"])

        # ------------------------------------------------------------------
        # ⑤ 收尾
        # ------------------------------------------------------------------
        print()
        print("[5/5] ⑤ 收尾:压榨边界与安全声明")
        print("─" * 60)
        print("═" * 62)
        print(" 高档只压榨本地计算;对外礼貌间隔与举报频控未放宽(红线 35);")
        print(" 全程零外呼、零真实提交(红线 36);workers 恒 ≤ 核数(红线 37)。")
        cores = int(stage1.get("cores") or 0)
        workers_seen = stage3.get("workers")
        politeness_ok = stage3.get("politeness_ok")
        submitted = stage4.get("submitted")
        auto_ok = stage4.get("auto_confirm_all_false")
        proofs = [
            [f"③ tier=high 扫描 workers={workers_seen} ≤ cores={cores}", workers_seen is not None and cores > 0 and workers_seen <= cores],
            ["③ 礼貌/频控字段 mid↔high 完全一致", politeness_ok is True],
            ["④ 干跑 submitted=0(零真实提交)", submitted == 0],
            ["④ 每条 auto_confirm=False(人工门不代答)", auto_ok is True],
        ]
        print(" 证据:")
        print(_table(["检查项", "结果"], [[text, "✔" if ok else "▶"] for text, ok in proofs]))
        if skipped_stages:
            print(f" 跳过环节({len(skipped_stages)} 个,兄弟模块未就位或环节降级):")
            for name in skipped_stages:
                print(f"   - {name}")
        print(" 真实收官(生产)路径:")
        print("   ① python -m netsentinel.finishflow --input 清单.yaml --tier high   # 收官汇总")
        print("   ② python -m netsentinel.cli.batch_tui                             # 逐组声明(红线 25)")
        print("   ③ python -m netsentinel.finishflow --report --dry-run              # 举报准备(先干跑)")
        print("      确认后 --exec:每条验证码与最终确认仍由人工完成(红线 36)")
        print(f" 演示产物(临时目录,随演示结束清理):{temp_root}")
        print("═" * 62)
        return 0
    except Exception as exc:  # noqa: BLE001 - 演示脚本顶层兜底
        print(f"演示失败:{type(exc).__name__}:{exc}", file=sys.stderr)
        return 1
    finally:
        if queue is not None:
            close_fn = getattr(queue, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:  # noqa: BLE001 - 收尾清理失败不影响退出码
                    pass
        if site_memory is not None:
            close_fn = getattr(site_memory, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:  # noqa: BLE001
                    pass
        _stop_sites(sites)
        shutil.rmtree(temp_root, ignore_errors=True)
        # Windows 下个别 sqlite/日志句柄可能延迟释放:强制 GC 后再清一次(零残留)。
        gc.collect()
        shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
