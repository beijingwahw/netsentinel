# -*- coding: utf-8 -*-
"""NetSentinel 离线端到端测试(stub 桩分类器链路)—— A19。

覆盖三块内容,全部离线、只访问 127.0.0.1:

1. ``scripts/make_png.py``:纯标准库 PNG 编码器的字节级正确性(签名/块结构/
   CRC/滤波字节/像素数据)与 CLI 行为;
2. ``tests/fixtures/demo_site``:静态夹具完整性(页面引用的图片齐全、
   尺寸为 200x200、站点内互链可供 BFS);
3. 端到端闭环:本地 http.server 服务演示站点 → site_map 链接发现 →
   capture_page 逐页捕获(真浏览器不可用时自动退化为无截图模式)→
   stub 分类 → ensemble 集成 → assess 判定 → ReviewQueue 入列(pending)→
   packager 证据 zip → plan_12377 举报计划 → executor dry_run 干跑。

安全红线:
- 全程仅访问 127.0.0.1,绝不出现 www.12377.cn / www.shdf.gov.cn 的真实访问;
- 举报计划不得包含自动填写验证码的步骤,人工门(HUMAN_GATE)必须存在;
- dry_run 不启动浏览器、不提交任何举报(submitted 必须为 False);
- 兄弟模块未就位时按团队契约用 ``pytest.importorskip`` 跳过(skip 是可接受结局)。
"""
from __future__ import annotations

import dataclasses
import functools
import importlib.util
import struct
import subprocess
import sys
import threading
import zlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import pytest

from netsentinel.contracts import Config, SiteReport, StepAction, Verdict

# ---------------------------------------------------------------------------
# 路径与依赖清单
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "demo_site"
MAKE_PNG_PATH = ROOT / "scripts" / "make_png.py"

#: 端到端核心链路依赖的兄弟模块(扫描 → 判定 → 复核队列)
CORE_SIBLING_MODULES: list[str] = [
    "netsentinel.config",
    "netsentinel.crawler.fetcher",
    "netsentinel.crawler.browser",
    "netsentinel.crawler.site_map",
    "netsentinel.vision.stub_classifier",
    "netsentinel.vision.ensemble",
    "netsentinel.decision.verdict",
    "netsentinel.decision.review_queue",
]

#: 证据打包与举报(提交)侧依赖的兄弟模块
SUBMIT_SIBLING_MODULES: list[str] = [
    "netsentinel.evidence.packager",
    "netsentinel.submit.portal_12377",
    "netsentinel.submit.executor_playwright",
]

#: 演示站点里应被桩分类器判为高置信(nsfw_hi 关键词)的图片
NSFW_IMAGE_NAMES = ["nsfw_hi_1.png", "nsfw_hi_2.png", "nsfw_hi_3.png", "nsfw_hi_4.png"]
#: 演示站点里的正常图片
NORMAL_IMAGE_NAMES = ["normal_1.png"]
#: 全部夹具图片(200x200,由 scripts/make_png.py 生成)
ALL_IMAGE_NAMES = NSFW_IMAGE_NAMES + NORMAL_IMAGE_NAMES


def _load_make_png() -> Any:
    """按文件路径加载 scripts/make_png.py(scripts 目录不是包,无法常规 import)。"""
    spec = importlib.util.spec_from_file_location("netsentinel_scripts_make_png", MAKE_PNG_PATH)
    assert spec is not None and spec.loader is not None, "无法加载 scripts/make_png.py"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 1. PNG 编码器单元测试
# ---------------------------------------------------------------------------

class TestMakePng:
    """make_png 的字节级正确性:可直接被任何 PNG 解码器读取。"""

    @staticmethod
    def _parse_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
        """按 PNG 规范逐块解析,同时校验每块的 CRC32;格式错误直接断言失败。"""
        assert data[:8] == b"\x89PNG\r\n\x1a\n", "PNG 签名错误"
        chunks: list[tuple[bytes, bytes]] = []
        pos = 8
        while pos < len(data):
            assert pos + 8 <= len(data), "块头被截断"
            (length,) = struct.unpack(">I", data[pos : pos + 4])
            tag = data[pos + 4 : pos + 8]
            payload = data[pos + 8 : pos + 8 + length]
            assert len(payload) == length, "块数据被截断"
            crc_bytes = data[pos + 8 + length : pos + 12 + length]
            assert len(crc_bytes) == 4, "块 CRC 被截断"
            expected_crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
            assert struct.unpack(">I", crc_bytes)[0] == expected_crc, f"块 {tag!r} CRC 校验失败"
            chunks.append((tag, payload))
            pos += 12 + length
        return chunks

    def test_bytes_structure(self) -> None:
        """IHDR/IDAT/IEND 三块齐全,位深 8、真彩色、无隔行,像素可解压还原。"""
        make_png = _load_make_png().make_png
        width, height, rgb = 7, 5, (0x12, 0x34, 0x56)
        data = make_png(width, height, rgb)

        chunks = self._parse_chunks(data)
        tags = [tag for tag, _ in chunks]
        assert tags == [b"IHDR", b"IDAT", b"IEND"], f"块顺序应固定,实际为 {tags}"

        ihdr_w, ihdr_h, depth, color_type, compression, filter_method, interlace = (
            struct.unpack(">IIBBBBB", chunks[0][1])
        )
        assert (ihdr_w, ihdr_h) == (width, height), "IHDR 宽高不符"
        assert (depth, color_type, compression, filter_method, interlace) == (8, 2, 0, 0, 0)

        raw = zlib.decompress(chunks[1][1])
        stride = 1 + 3 * width  # 滤波字节 + RGB 像素
        assert len(raw) == height * stride, "解压后像素字节数不符"
        expected_row = b"\x00" + bytes(rgb) * width
        for row_index in range(height):
            row = raw[row_index * stride : (row_index + 1) * stride]
            assert row == expected_row, f"第 {row_index} 行应为 filter=0 的纯色扫描线"

        assert chunks[-1][1] == b"", "IEND 数据段必须为空"

    def test_distinct_inputs_distinct_bytes(self) -> None:
        """不同颜色/尺寸应产出不同字节(夹具图片内容互不相同)。"""
        make_png = _load_make_png().make_png
        assert make_png(4, 4, (1, 2, 3)) != make_png(4, 4, (1, 2, 4))
        assert make_png(4, 4, (1, 2, 3)) != make_png(5, 4, (1, 2, 3))

    def test_invalid_arguments_raise(self) -> None:
        """非法宽高/颜色必须抛 ValueError(中文消息)。"""
        make_png = _load_make_png().make_png
        for bad_size in (0, -3, 2.5, "10", True):
            with pytest.raises(ValueError):
                make_png(bad_size, 10, (0, 0, 0))  # type: ignore[arg-type]
            with pytest.raises(ValueError):
                make_png(10, bad_size, (0, 0, 0))  # type: ignore[arg-type]
        for bad_rgb in ((0, 0), (0, 0, 0, 0), (0, 0, 256), (-1, 0, 0), (0, 0, 1.5), "050"):
            with pytest.raises(ValueError):
                make_png(3, 3, bad_rgb)  # type: ignore[arg-type]

    def test_cli_roundtrip(self, tmp_path: Path) -> None:
        """CLI:python scripts/make_png W H RRGGBB out.png 产出可解析的 PNG。"""
        out_path = tmp_path / "nested" / "pic.png"
        proc = subprocess.run(
            [sys.executable, str(MAKE_PNG_PATH), "6", "4", "fF8800", str(out_path)],
            capture_output=True,
            text=True,
            errors="replace",
            cwd=str(ROOT),
            timeout=60,
        )
        assert proc.returncode == 0, f"CLI 应成功,stderr={proc.stderr}"
        assert out_path.is_file(), "CLI 应落盘输出文件"
        header = out_path.read_bytes()[:24]
        assert header[:8] == b"\x89PNG\r\n\x1a\n"
        assert header[12:16] == b"IHDR"
        width, height = struct.unpack(">II", header[16:24])
        assert (width, height) == (6, 4), "CLI 参数 W/H 应写入 IHDR"

    def test_cli_rejects_bad_usage(self, tmp_path: Path) -> None:
        """CLI:非法颜色 / 非正尺寸应以退出码 2 结束且不落盘。"""
        out_path = tmp_path / "bad.png"
        for args in (
            ["6", "4", "XYZ12A", str(out_path)],   # 非十六进制颜色
            ["6", "4", "12345", str(out_path)],    # 颜色长度错误
            ["0", "4", "112233", str(out_path)],   # 非正宽度
        ):
            proc = subprocess.run(
                [sys.executable, str(MAKE_PNG_PATH), *args],
                capture_output=True,
                text=True,
                errors="replace",
                cwd=str(ROOT),
                timeout=60,
            )
            assert proc.returncode == 2, f"参数 {args!r} 应以退出码 2 拒绝"
            assert not out_path.exists(), "失败时不应产生输出文件"


# ---------------------------------------------------------------------------
# 2. 演示站点静态夹具
# ---------------------------------------------------------------------------

class TestDemoSiteFixtures:
    """tests/fixtures/demo_site 的完整性:端到端与演示脚本都依赖它。"""

    def test_index_page_references_all_images(self) -> None:
        index = FIXTURE_DIR / "index.html"
        assert index.is_file(), "缺少 tests/fixtures/demo_site/index.html"
        html = index.read_text(encoding="utf-8")
        assert "<title>演示站点</title>" in html, "首页标题必须为「演示站点」"
        for name in ALL_IMAGE_NAMES:
            assert f'src="{name}"' in html, f"首页必须以 <img src> 引用 {name}"
        assert html.count("<img") >= len(ALL_IMAGE_NAMES)

    def test_image_files_are_200x200_png(self) -> None:
        for name in ALL_IMAGE_NAMES:
            path = FIXTURE_DIR / name
            assert path.is_file(), f"缺少夹具图片 {name}(请用 scripts/make_png.py 生成)"
            header = path.read_bytes()[:24]
            assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{name} 不是合法 PNG"
            assert header[12:16] == b"IHDR"
            width, height = struct.unpack(">II", header[16:24])
            assert (width, height) == (200, 200), f"{name} 应为 200x200,实际 {width}x{height}"

    def test_second_page_links_back(self) -> None:
        second = FIXTURE_DIR / "second_page.html"
        assert second.is_file(), "缺少 tests/fixtures/demo_site/second_page.html"
        html = second.read_text(encoding="utf-8")
        assert 'href="index.html"' in html, "第二页应链接回首页(供 site_map BFS)"


# ---------------------------------------------------------------------------
# 3. 端到端:本机服务 + 扫描 + 判定 + 队列(+ 证据包与举报干跑)
# ---------------------------------------------------------------------------

class _QuietHandler(SimpleHTTPRequestHandler):
    """静态文件服务:关闭访问日志,避免污染 pytest 输出。"""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


@pytest.fixture()
def demo_site_url() -> Any:
    """在 127.0.0.1 随机端口服务演示站点,yield 首页 URL,teardown 关停。"""
    handler = functools.partial(_QuietHandler, directory=str(FIXTURE_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="e2e-http")
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/index.html"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _scan_config(tmp_path: Path) -> Config:
    """构造端到端配置:所有产物只写 tmp_path,本机演示无抓取间隔。"""
    cfg = Config()
    data_dir = tmp_path / "data"
    cfg.data_dir = str(data_dir)
    cfg.evidence_dir = str(data_dir / "evidence")
    cfg.db_path = str(data_dir / "review_queue.db")
    cfg.audit_path = str(data_dir / "audit.jsonl")
    cfg.log_path = str(data_dir / "logs" / "netsentinel.log")
    cfg.fetch_delay_s = 0.0
    return cfg


def _png_size(path: Path) -> tuple[int, int]:
    """读取 PNG IHDR 宽高(仅标准库);非 PNG/读取失败返回 (0, 0)。"""
    try:
        with open(path, "rb") as fh:
            header = fh.read(24)
    except OSError:
        return (0, 0)
    if len(header) >= 24 and header[:8] == b"\x89PNG\r\n\x1a\n" and header[12:16] == b"IHDR":
        width, height = struct.unpack(">II", header[16:24])
        return (int(width), int(height))
    return (0, 0)


def _make_preserving_download(fetcher_module: Any) -> Any:
    """构造注入 capture_page 的下载函数(离线桩链路专用)。

    fetcher.download_images 按契约以 sha256 前缀落盘、宽高置 0;而桩分类器
    按文件名关键词打分、判定公式按 min_image_px 过滤小图。这里在真实下载
    完成后:1) 把文件改回 URL 原始文件名(夹具文件名全局唯一);
    2) 从 PNG 头补全宽高。不发起任何额外网络请求。
    """

    def download(
        urls: list[str],
        source_page: str,
        dest_dir: str,
        cfg: Config,
    ) -> list[Any]:
        evidences = fetcher_module.download_images(urls, source_page, dest_dir, cfg)
        adjusted: list[Any] = []
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


@functools.lru_cache(maxsize=1)
def _chromium_launchable() -> bool:
    """探测 playwright + chromium 是否可用(进程内缓存,只探测一次)。"""
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


def _maybe_force_degraded(monkeypatch: pytest.MonkeyPatch, mods: dict[str, Any]) -> bool:
    """chromium 不可用时强制 capture_page 走无截图退化模式,返回是否退化。"""
    if _chromium_launchable():
        return False
    monkeypatch.setattr(
        mods["netsentinel.crawler.browser"], "_import_sync_playwright", lambda: None
    )
    return True


def _run_stub_scan(
    mods: dict[str, Any],
    cfg: Config,
    start_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> SiteReport:
    """按契约拼装 run_scan 的核心流水线:发现 → 捕获 → 分类 → 集成 → 判定。

    页面捕获注入"保留原始文件名 + 补全 PNG 宽高"的下载器,使离线桩链路
    (stub 按文件名打分、assess 按 min_image_px 过滤)得以走真实下载路径。
    """
    # 真浏览器不可用(未安装 playwright/chromium)→ 退化模式,不强求截图
    degraded = _maybe_force_degraded(monkeypatch, mods)

    urls = mods["netsentinel.crawler.site_map"].discover_links(start_url, cfg)
    assert start_url in urls, "链接发现结果必须包含起点页"
    assert all(urlsplit(u).hostname in ("127.0.0.1", "localhost") for u in urls), (
        "离线测试只允许访问本机地址"
    )

    download = _make_preserving_download(mods["netsentinel.crawler.fetcher"])
    pages = [
        mods["netsentinel.crawler.browser"].capture_page(u, cfg, download=download)
        for u in urls
    ]
    images = [img for page in pages for img in page.image_evidences]
    assert len(images) == len(ALL_IMAGE_NAMES), (
        f"首页应下载到全部 {len(ALL_IMAGE_NAMES)} 张夹具图片,实际 {len(images)} 张"
        f"(退化模式={degraded})"
    )

    # 桩分类器(cfg.ensemble_members 默认 ["stub"],此处直接实例化 stub)
    classifier = mods["netsentinel.vision.stub_classifier"].StubClassifier(cfg)
    member_scores = classifier.classify_batch(images)
    ensemble = mods["netsentinel.vision.ensemble"].ensemble_scores(member_scores)
    return mods["netsentinel.decision.verdict"].assess(start_url, pages, ensemble, cfg)


def test_e2e_scan_verdict_and_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    demo_site_url: str,
) -> None:
    """端到端(核心链路):扫描演示站点 → NSFW → needs_review → 队列 pending。"""
    mods = {name: pytest.importorskip(name) for name in CORE_SIBLING_MODULES}
    cfg = _scan_config(tmp_path)

    report = _run_stub_scan(mods, cfg, demo_site_url, monkeypatch)

    assert report.verdict is Verdict.NSFW, (
        f"4 张 nsfw_hi 夹具图应判 NSFW,实际 {report.verdict}(agg={report.agg_nsw_prob})"
    )
    assert report.needs_review is True, "NSFW 同样必须人工复核"
    assert report.agg_nsw_prob > 0.9, f"聚合分应 > 0.9,实际 {report.agg_nsw_prob}"
    assert report.nsw_image_count >= cfg.min_nsw_images, (
        f"达标图数应 ≥ {cfg.min_nsw_images},实际 {report.nsw_image_count}"
    )
    assert len(report.pages) >= 2, "BFS 应发现首页与第二页"

    queue_cls = mods["netsentinel.decision.review_queue"].ReviewQueue
    queue = queue_cls(cfg.db_path)
    try:
        entry_id = queue.add(report, evidence_zip="")
        entry = queue.get(entry_id)
    finally:
        queue.close()
    assert entry is not None, "入列后应能按 id 读回"
    assert entry.status == "pending", f"新条目状态应为 pending,实际 {entry.status}"
    assert entry.site_url == demo_site_url
    assert entry.verdict == Verdict.NSFW.value


def test_e2e_evidence_bundle_and_dry_run_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    demo_site_url: str,
) -> None:
    """端到端(提交侧):证据 zip → 队列记录 → plan_12377 → dry_run 执行。"""
    mods = {
        name: pytest.importorskip(name)
        for name in CORE_SIBLING_MODULES + SUBMIT_SIBLING_MODULES
    }
    cfg = _scan_config(tmp_path)

    report = _run_stub_scan(mods, cfg, demo_site_url, monkeypatch)
    assert report.verdict is Verdict.NSFW and report.needs_review

    # 证据包:目录 + manifest + zip,zip 文件必须真实存在
    bundle = mods["netsentinel.evidence.packager"].build_bundle(report, cfg)
    assert bundle.zip_path, "证据包必须产出 zip 路径"
    assert Path(bundle.zip_path).is_file(), f"证据 zip 应存在:{bundle.zip_path}"
    assert Path(bundle.manifest_path).is_file(), "manifest.json 应存在"

    # 复核队列:pending 记录并携带证据 zip
    queue_cls = mods["netsentinel.decision.review_queue"].ReviewQueue
    queue = queue_cls(cfg.db_path)
    try:
        entry_id = queue.add(report, evidence_zip=bundle.zip_path)
        entry = queue.get(entry_id)
    finally:
        queue.close()
    assert entry is not None and entry.status == "pending"
    assert entry.evidence_zip == bundle.zip_path

    # 举报计划:12377 渠道;不得自动填写验证码;必须有人工门
    plan = mods["netsentinel.submit.portal_12377"].plan_12377(
        entry, cfg, reporter_name="净网哨兵离线测试"
    )
    assert plan.portal.value == "12377"
    captcha_fill = [
        s for s in plan.steps if s.action is StepAction.FILL and "captcha" in s.selector
    ]
    assert not captcha_fill, "红线:验证码只能由人工门输入,计划不得包含自动填写验证码步骤"
    assert any(s.action is StepAction.HUMAN_GATE for s in plan.steps), "计划必须包含人工门"
    assert plan.payload.evidence_zip == bundle.zip_path, "计划应携带证据包 zip"

    # 干跑执行:不启动浏览器、不真实提交
    result = mods["netsentinel.submit.executor_playwright"].execute(
        plan, cfg, dry_run=True, out_dir=str(tmp_path / "runs")
    )
    assert result.ok is True, f"dry_run 应成功,notes={result.notes}"
    assert result.submitted is False, "红线:dry_run 绝不真实提交举报"
