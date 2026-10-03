# -*- coding: utf-8 -*-
"""净网哨兵(NetSentinel)离线端到端演示脚本 —— A19。

在完全离线的环境中演示"抽样 → 图像识别 → 站点判定 → 人工复核队列 →
举报计划生成 → 干跑(dry_run)"的完整闭环:

1. 把 tests/fixtures/demo_site 拷贝到临时目录,用 threading + http.server
   在 127.0.0.1 的随机端口起一个静态服务(绝无真实网络访问);
2. load_config() 加载默认配置(allow_network=False 仅放行本机地址);
3. 调用兄弟模块 pipeline.orchestrator.run_scan 完成扫描(惰性导入,
   任一兄弟模块未就位时打印缺失清单并以退出码 2 结束);
4. 打印 SiteReport 中文摘要(判定 / 聚合分 / 达标图数 / 页面数);
5. needs_review 时打印证据包 zip 路径与复核队列条目号;
6. 用 portal_12377.plan_12377 生成举报计划,并以 dry_run 干跑展示前几步。

安全红线(与团队契约一致):
- 全程只访问 127.0.0.1,绝不访问 www.12377.cn / www.shdf.gov.cn;
- 验证码只能由人工门(HUMAN_GATE)处理,本脚本绝不自动填写;
- dry_run 不启动浏览器、不点击提交,即"未提交任何举报"。

用法:python scripts/demo_stub_scan.py
退出码:0=演示成功;2=兄弟模块未就位;1=其他错误。
"""
from __future__ import annotations

import dataclasses
import importlib
import shutil
import struct
import sys
import tempfile
import threading
import unicodedata
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

#: 项目根目录(脚本位于 <root>/scripts/)
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from netsentinel import telemetry  # noqa: E402
from netsentinel.contracts import Config, PageSample, SiteReport  # noqa: E402

#: 演示站点静态夹具目录
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "demo_site"

#: 演示依赖的兄弟模块(模块名 → 中文角色说明);任一缺失都以退出码 2 结束
REQUIRED_SIBLING_MODULES: list[tuple[str, str]] = [
    ("netsentinel.config", "配置加载(A01)"),
    ("netsentinel.crawler.fetcher", "网页/图片抓取(A02)"),
    ("netsentinel.crawler.browser", "页面捕获(A03)"),
    ("netsentinel.crawler.site_map", "站点地图 BFS(A04)"),
    ("netsentinel.vision.stub_classifier", "桩分类器(A05)"),
    ("netsentinel.vision.ensemble", "集成评分(A08)"),
    ("netsentinel.decision.verdict", "站点判定(A09)"),
    ("netsentinel.decision.review_queue", "人工复核队列(A10)"),
    ("netsentinel.evidence.packager", "证据打包(A11)"),
    ("netsentinel.submit.portal_12377", "12377 举报计划(A13)"),
    ("netsentinel.submit.executor_playwright", "举报执行器(A15)"),
    ("netsentinel.pipeline.orchestrator", "扫描流水线(A18)"),
]

#: 判定结果的中文标签
_VERDICT_CN = {"clean": "无风险", "suspect": "疑似", "nsfw": "高置信色情"}


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
    """中文等东亚宽字符按 2 列计的显示宽度(纯标准库近似,与 A78 演示同款)。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    """按显示宽度右补空格,用于中文表格对齐。"""
    text = str(text)
    return text + " " * max(0, width - _disp_width(text))


def _table(headers: list[str], rows: list[list[str]]) -> str:
    """渲染一张按显示宽度对齐的中文表格(无第三方依赖,与 A78 演示同款)。"""
    widths = [_disp_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _disp_width(str(cell)))
    lines = [
        "  ".join(_pad(h, widths[i]) for i, h in enumerate(headers)).rstrip(),
        "  ".join("-" * w for w in widths),
    ]
    for row in rows:
        lines.append("  ".join(_pad(str(c), widths[i]) for i, c in enumerate(row)).rstrip())
    return "\n".join(lines)


def _print_telemetry_tail() -> None:
    """打印 telemetry.snapshot() 中文尾节(V5 可观测性演示)。

    展示本进程累计的关键计数(计数器/仪表)与各阶段耗时统计
    (次数 / 平均 / p95 / 最大,单位毫秒);零依赖、只含名称与数字。
    """
    snap = telemetry.snapshot()
    print()
    print("── 遥测快照(telemetry.snapshot,V5 可观测性)──────────")
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


class _QuietHandler(SimpleHTTPRequestHandler):
    """静态文件服务:关闭访问日志,避免污染演示输出。"""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


def check_sibling_modules() -> list[str]:
    """逐一导入演示依赖的兄弟模块,返回未就位模块的中文描述列表。"""
    missing: list[str] = []
    for name, role in REQUIRED_SIBLING_MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - 并行开发中未就位/依赖缺失均视为未就位
            missing.append(f"{name} —— {role}(导入失败:{exc})")
    return missing


def _png_size(path: Path) -> tuple[int, int]:
    """读取 PNG IHDR 中的宽高(仅标准库);非 PNG 或读取失败返回 (0, 0)。"""
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


def _make_preserving_download(fetcher_module: object) -> object:
    """构造注入 capture_page 的图片下载函数(离线桩链路专用)。

    背景:fetcher.download_images 按契约以 sha256 前缀落盘,而桩分类器
    依据文件名关键词(nsfw_hi/nsfw_mid)打分;为了让离线演示/端到端测试
    能走真实下载路径同时又让桩分类器可判读,这里在真实下载完成后:
    1. 把文件改回 URL 中的原始文件名(仅本机夹具,文件名全局唯一);
    2. 从 PNG 头补全 width/height(判定公式需要按 min_image_px 过滤小图)。
    本函数不发起任何额外网络请求,重命名只发生在本地证据目录内。
    """

    def download(
        urls: list[str],
        source_page: str,
        dest_dir: str,
        cfg: Config,
    ) -> list:
        evidences = fetcher_module.download_images(urls, source_page, dest_dir, cfg)  # type: ignore[attr-defined]
        adjusted: list = []
        for evidence in evidences:
            name = PurePosixPath(urlsplit(evidence.url).path).name
            target = Path(dest_dir) / (name or "image.bin")
            if name and Path(evidence.path) != target:
                Path(evidence.path).replace(target)
            width, height = _png_size(target)
            adjusted.append(
                dataclasses.replace(evidence, path=str(target), width=width, height=height)
            )
        return adjusted

    return download


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


def _make_capture() -> object:
    """构造传给 run_scan 的页面捕获函数(注入保留文件名的下载器)。"""
    fetcher_module = importlib.import_module("netsentinel.crawler.fetcher")
    browser_module = importlib.import_module("netsentinel.crawler.browser")
    download = _make_preserving_download(fetcher_module)

    def capture(
        url: str,
        cfg: Config,
        *,
        fetch_page: object = None,
    ) -> PageSample:
        # 签名与 pipeline.orchestrator.run_scan 的 CaptureFn 约定一致
        # (支持 fetch_page 关键字注入);download 固定注入保留文件名版本。
        return browser_module.capture_page(  # type: ignore[attr-defined]
            url, cfg, fetch_page=fetch_page, download=download
        )

    return capture


def _print_report_summary(report: SiteReport) -> None:
    """打印 SiteReport 中文摘要。"""
    verdict = getattr(report.verdict, "value", str(report.verdict))
    print()
    print("── 站点判定摘要(SiteReport)──────────────────────────")
    print(f"  判定结果 : {_VERDICT_CN.get(verdict, verdict)}({verdict})")
    print(f"  聚合分   : {report.agg_nsw_prob:.4f}(候选图中最高 ensemble 分)")
    print(f"  达标图数 : {report.nsw_image_count} 张(单图分 ≥ 计数线)")
    print(f"  抽样页面 : {len(report.pages)} 个")
    for page in report.pages:
        hints = "、".join(page.text_hint_hits) if page.text_hint_hits else "无"
        print(
            f"    - {page.url}"
            f"(图片 {len(page.image_evidences)} 张,文本线索:{hints})"
        )
    print(f"  需人工复核: {'是(NSFW 也必须人工确认后才允许举报)' if report.needs_review else '否'}")


def _print_plan_preview(plan: object, limit: int = 6) -> None:
    """按 playbook 风格打印举报计划的前几步。"""
    steps = getattr(plan, "steps", [])
    total = len(steps)
    portal = getattr(getattr(plan, "portal", ""), "value", "")
    entry_url = getattr(plan, "entry_url", "")
    print()
    print("── 举报计划(playbook 预览,仅展示前几步)──────────────")
    print(f"  渠道:{portal} | 入口:{entry_url} | 共 {total} 步")
    for index, step in enumerate(steps[:limit], start=1):
        action = getattr(getattr(step, "action", ""), "value", "")
        detail = step.label or step.text or step.selector or step.value  # type: ignore[attr-defined]
        print(f"    步骤 {index}/{total} [{action}] {detail}")
    if total > limit:
        print(f"    ……(其余 {total - limit} 步略,含人工门与提交确认)")
    gate_steps = [s for s in steps if getattr(getattr(s, "action", ""), "value", "") == "human_gate"]
    if gate_steps:
        print(f"    人工门:{len(gate_steps)} 处 —— 验证码与最终提交只会由人工完成")


def _find_pending_entry(db_path: str, site_url: str):  # noqa: ANN202 - 兄弟 Entry 类型,鸭子返回
    """从复核队列里找回本次扫描产生的 pending 条目(取同站点最后一条)。"""
    review_queue = importlib.import_module("netsentinel.decision.review_queue")
    queue = review_queue.ReviewQueue(db_path)
    try:
        entries = [e for e in queue.list(status="pending") if e.site_url == site_url]
        return entries[-1] if entries else None
    finally:
        queue.close()


def _start_local_site(site_dir: Path) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    """在 127.0.0.1 随机端口起静态服务,返回 (server, 线程, 首页 URL)。"""
    handler = partial(_QuietHandler, directory=str(site_dir))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="demo-http")
    thread.start()
    index_url = f"http://127.0.0.1:{server.server_address[1]}/index.html"
    return server, thread, index_url


def _build_demo_config(temp_root: Path) -> Config:
    """load_config() 出默认配置后,把所有产物路径改指临时目录(演示零残留)。"""
    config_module = importlib.import_module("netsentinel.config")
    cfg = config_module.load_config()
    data_dir = temp_root / "data"
    cfg.data_dir = str(data_dir)
    cfg.evidence_dir = str(data_dir / "evidence")
    cfg.db_path = str(data_dir / "review_queue.db")
    cfg.audit_path = str(data_dir / "audit.jsonl")
    cfg.log_path = str(data_dir / "logs" / "netsentinel.log")
    cfg.fetch_delay_s = 0.0  # 本机演示无需礼貌间隔
    return cfg


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001 - 预留参数
    """演示主流程;返回进程退出码。"""
    _ensure_utf8_stdio()
    print("═" * 62)
    print(" 净网哨兵(NetSentinel)离线端到端演示 —— 桩分类器链路")
    print(" 安全提示:仅访问 127.0.0.1 演示站点;dry_run;不提交任何举报")
    print("═" * 62)

    # ④(前置)兄弟模块就位检查:缺失则打印清单并以退出码 2 结束
    missing = check_sibling_modules()
    if missing:
        print()
        print("演示无法启动:以下兄弟模块尚未就位 ——")
        for item in missing:
            print(f"  - {item}")
        print()
        print("并行开发仍在进行,请等待对应代理完成后再运行本演示。")
        return 2

    temp_root = Path(tempfile.mkdtemp(prefix="netsentinel_demo_"))
    server: ThreadingHTTPServer | None = None
    server_thread: threading.Thread | None = None
    # V5 可观测:遥测从演示起点清零,尾节的 snapshot 只含本轮数据
    telemetry.reset()
    try:
        # ① 临时目录拷贝演示站点 → ② 本机静态服务(127.0.0.1 随机端口)
        site_dir = temp_root / "demo_site"
        shutil.copytree(FIXTURE_DIR, site_dir)
        server, server_thread, index_url = _start_local_site(site_dir)
        print(f"[1/6] 演示站点已就绪:{index_url}(目录:{site_dir})")

        # ③ 加载默认配置(allow_network=False 只放行本机)
        cfg = _build_demo_config(temp_root)
        print(f"[2/6] 配置加载完成:allow_network={cfg.allow_network}(仅放行本机),分类器={cfg.classifier}")

        # 真浏览器探测:chromium 可用则截图,否则退化为仅抓 HTML
        if _chromium_launchable():
            print("[3/6] 检测到 playwright + chromium:页面将进行真实整页截图")
        else:
            _force_degraded_capture()
            print("[3/6] 未检测到可用 chromium:capture_page 自动退化为无截图模式(仅抓 HTML)")

        # ④ run_scan(兄弟惰性导入已在上方检查,这里直接取用)
        orchestrator = importlib.import_module("netsentinel.pipeline.orchestrator")
        print("[4/6] 开始扫描:链接发现 → 逐页捕获 → 桩分类器打分 → 集成 → 判定 ……")
        with telemetry.timer("demo.scan"):
            report = orchestrator.run_scan(index_url, cfg, capture=_make_capture())
        telemetry.gauge("demo.pages", len(report.pages))
        telemetry.gauge(
            "demo.images", sum(len(page.image_evidences) for page in report.pages)
        )

        # ⑤ SiteReport 中文摘要
        _print_report_summary(report)

        # ⑥ needs_review:证据包 zip 与复核队列条目
        entry = None
        if report.needs_review:
            entry = _find_pending_entry(cfg.db_path, index_url)
            if entry is not None:
                print()
                print("── 人工复核(机器初筛,人工拍板)────────────────────────")
                print(f"  复核队列条目编号:{entry.id}(状态:{entry.status})")
                print(f"  证据包 zip     :{entry.evidence_zip or '(未记录)'}")
                print("  后续:人工确认(approve)后才能进入举报提交流程")
            else:
                print()
                print("警告:needs_review=True 但未在复核队列中找到对应 pending 条目。")

        # ⑦ plan_12377 + executor dry_run 展示举报计划
        if entry is not None:
            portal_12377 = importlib.import_module("netsentinel.submit.portal_12377")
            executor = importlib.import_module("netsentinel.submit.executor_playwright")
            plan = portal_12377.plan_12377(entry, cfg, reporter_name="净网哨兵演示")
            _print_plan_preview(plan)
            with telemetry.timer("demo.plan_dryrun"):
                result = executor.execute(
                    plan, cfg, dry_run=True, out_dir=str(temp_root / "runs")
                )
            print()
            print("── 举报执行(dry_run 干跑)────────────────────────────")
            print(f"  执行结果:ok={result.ok},已提交={result.submitted}")
            print(f"  步骤记录:{len(result.notes)} 条(dry_run 不启动浏览器、不真实提交)")
            if result.notes:
                print(f"  示例记录:{result.notes[0]}")

        # ⑧ V5 遥测尾节:关键计数与各阶段耗时 p95(中文表格)
        _print_telemetry_tail()

        # ⑨ 收尾
        print()
        print("═" * 62)
        print(" 演示完成:全程未访问真实网络,未提交任何举报")
        print(f" 演示产物(临时目录,随演示结束清理):{temp_root}")
        print("═" * 62)
        return 0
    except Exception as exc:  # noqa: BLE001 - 演示脚本顶层兜底,避免栈溢出式报错
        print(f"演示失败:{type(exc).__name__}:{exc}", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=5)
        shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
