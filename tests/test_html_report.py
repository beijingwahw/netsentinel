"""A34 单文件 HTML 举报材料(人工核对稿)渲染测试。

全部离线:纯标准库造最小合法 PNG 夹具,只写 tmp_path,断言六个区块标题、
base64 内嵌 / 超限列名分支、签名栏空位、声明文案、manifest 读取回退、
落盘 UTF-8 与 html.escape 转义。
V5:内嵌图片单次 stat(读取调用计数,不用墙钟)与 telemetry(report.render)。
"""
from __future__ import annotations

import json
import pathlib
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

from netsentinel import telemetry
from netsentinel.contracts import EvidenceBundle
from netsentinel.report.html_report import render_report

#: 六个区块标题(与 render_report 输出一一对应)
SECTION_HEADINGS = (
    "一、基本信息",
    "二、识别摘要",
    "三、风险要点",
    "四、证据图片",
    "五、声明",
    "六、人工核对签名栏",
)


def _mini_png(width: int = 6, height: int = 4, rgb: tuple = (140, 30, 63)) -> bytes:
    """最小合法纯色 PNG(8 位 RGB、filter=0、单 IDAT),纯标准库实现。"""

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


def _make_entry(**overrides) -> SimpleNamespace:
    """伪造一条复核条目(鸭子类型,字段与 decision.review_queue.Entry 对齐)。"""
    fields = dict(
        id=7,
        site_url="http://bad.example/spot",
        verdict="nsfw",
        status="approved",
        evidence_zip="data/evidence/bad.example_20261001_100000.zip",
        created_at="2026-10-01T10:00:00+08:00",
        updated_at="2026-10-01T11:00:00+08:00",
        note="已人工核实",
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _sample_report_dict(images: list[str], with_intel: bool = True) -> dict:
    """伪造 SiteReport.as_dict() 形状的报告 dict。"""
    report: dict = {
        "site_url": "http://bad.example/spot",
        "pages": [
            {
                "url": "http://bad.example/spot",
                "screenshot_path": "",
                "images": images,
                "text_hint_hits": [],
            }
        ],
        "image_scores": [],
        "agg_nsw_prob": 0.9312,
        "nsw_image_count": 4,
        "verdict": "nsfw",
        "needs_review": True,
        "created_at": "2026-10-01T09:30:00+08:00",
    }
    if with_intel:
        report["intel"] = {
            "url": {
                "risk": 0.35,
                "explain": ["域名使用可疑顶级域 .xyz", "URL 含多级危险词子域"],
            },
            "text": {
                "risk": 0.8,
                "explain": ["页面命中中文色情关键词 12 个"],
            },
            "page_vlm": {"page_nsfw_prob": 0.82, "elements": []},
            "fusion": {
                "prob": 0.9600,
                "contrib": {"image": 2.2, "url": 0.12, "text": 0.2},
                "rule": "只升不降",
            },
        }
    return report


def test_full_render_sections_images_signature(tmp_path: Path) -> None:
    """完整渲染:六区块标题、单图内嵌、超限列名、签名栏三空位、声明文案。"""
    small = tmp_path / "small.png"          # <60KB,应内嵌
    small.write_bytes(_mini_png())
    big = tmp_path / "big.png"              # 真实存在但 >60KB,只列名
    big.write_bytes(b"\x89PNG" + b"\x00" * (60 * 1024 + 512))
    missing = tmp_path / "missing.png"      # 不存在的假 path,走"见 zip"分支

    html = render_report(
        _make_entry(),
        bundle=None,
        report_dict=_sample_report_dict([str(small), str(big), str(missing)]),
    )

    # 文档骨架与标题
    assert "<!doctype html>" in html.lower()
    assert '<meta charset="utf-8">' in html
    assert "网络有害信息举报材料(人工核对稿)" in html

    # 六个区块标题齐全
    for heading in SECTION_HEADINGS:
        assert heading in html

    # 区块一:基本信息(编号 / 站点 / 判定中文 / 证据包路径 / 复核备注)
    assert "http://bad.example/spot" in html
    assert "高置信色情" in html
    assert "data/evidence/bad.example_20261001_100000.zip" in html
    assert "已人工核实" in html

    # 区块二:识别摘要(agg / 达标图片数 / 页面数)
    assert "0.9312" in html
    assert "4" in html
    assert "1" in html

    # 区块三:风险要点 = url 2 条 + text 1 条 + 融合判定 1 条 + 融合贡献 1 条 = 5 条
    assert html.count('<li class="risk">') == 5
    assert "【链接特征】域名使用可疑顶级域 .xyz" in html
    assert "【文本特征】页面命中中文色情关键词 12 个" in html
    assert "【融合贡献】图像模型 +2.20" in html

    # 区块四:仅 small.png 内嵌;big / missing 只出现在"见 zip"列表
    assert html.count('src="data:image/png;base64,') == 1
    assert "其余见证据包 zip" in html
    assert "big.png" in html
    assert "missing.png" in html

    # 区块五:声明含"虚假举报"法律风险提示与人工核实确认
    assert "虚假举报" in html
    assert "人工核实" in html

    # 区块六:签名栏三个下划线空位(举报人 / 联系方式 / 日期)
    assert html.count('class="sign-blank"') == 3
    assert "举报人" in html
    assert "联系方式" in html
    assert "日期" in html


def test_no_intel_shows_placeholder(tmp_path: Path) -> None:
    """report_dict 无 intel:风险要点显示"未启用融合分析"。"""
    html = render_report(
        _make_entry(),
        bundle=None,
        report_dict=_sample_report_dict([], with_intel=False),
    )
    assert "未启用融合分析" in html
    assert '<li class="risk">' not in html
    # 无图片可嵌时也不抛错
    assert "证据包 zip" in html


def test_report_dict_from_manifest(tmp_path: Path) -> None:
    """report_dict 缺省时从 bundle 目录的 manifest.json 读回 "report" 键。"""
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    small = bundle_dir / "small.png"
    small.write_bytes(_mini_png())
    # manifest 里的图片路径是相对名,应以 bundle 证据目录为基准解析
    report = _sample_report_dict(["small.png"])
    (bundle_dir / "manifest.json").write_text(
        json.dumps({"report": report, "files": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    bundle = EvidenceBundle(
        site_url="http://bad.example/spot",
        dir_path=str(bundle_dir),
        manifest_path=str(bundle_dir / "manifest.json"),
        zip_path=str(bundle_dir) + ".zip",
    )

    html = render_report(_make_entry(), bundle=bundle, report_dict=None)

    assert "0.9312" in html                       # 识别摘要来自 manifest
    assert "【链接特征】" in html                  # 风险要点来自 manifest 的 intel
    assert 'src="data:image/png;base64,' in html  # 相对路径按 bundle 目录解析内嵌


def test_manifest_corrupt_falls_back(tmp_path: Path) -> None:
    """manifest 损坏(非法 JSON)时按缺失处理,不抛错。"""
    bundle_dir = tmp_path / "broken"
    bundle_dir.mkdir()
    (bundle_dir / "manifest.json").write_text("{不是合法 JSON", encoding="utf-8")
    bundle = EvidenceBundle(
        site_url="http://bad.example/spot",
        dir_path=str(bundle_dir),
        manifest_path=str(bundle_dir / "manifest.json"),
        zip_path="",
    )

    html = render_report(_make_entry(), bundle=bundle, report_dict=None)

    assert "未启用融合分析" in html
    assert "—" in html  # 识别摘要缺省占位


def test_out_path_written_utf8(tmp_path: Path) -> None:
    """out_path 落盘:父目录自动创建,UTF-8 中文完整,内容与返回值一致。"""
    out = tmp_path / "reports" / "举报材料_人工核对稿.html"
    html = render_report(
        _make_entry(),
        bundle=None,
        report_dict=_sample_report_dict([], with_intel=False),
        out_path=str(out),
    )
    assert out.is_file()
    on_disk = out.read_text(encoding="utf-8")
    assert on_disk == html
    assert "网络有害信息举报材料(人工核对稿)" in on_disk
    assert "高置信色情" in on_disk  # 中文未被写成转义字节


def test_escapes_site_url(tmp_path: Path) -> None:
    """站点 URL 含 <script> 时必须被转义,不得产生可执行脚本标签。"""
    entry = _make_entry(site_url='http://bad.example/<script>alert(1)</script>')
    html = render_report(entry, bundle=None, report_dict=None)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "alert(1)" in html  # 文本内容保留,仅标签被转义


# ---------------------------------------------------------------------------
# V5:telemetry 与单次 stat(读取调用计数,不用墙钟)
# ---------------------------------------------------------------------------


def test_v5_render_telemetry(tmp_path: Path) -> None:
    """V5 可观测:render_report 整体耗时记 timer("report.render")。"""
    telemetry.reset()
    html = render_report(
        _make_entry(),
        bundle=None,
        report_dict=_sample_report_dict([], with_intel=False),
    )
    snap = telemetry.snapshot()
    assert snap["timers"]["report.render"]["count"] == 1
    assert "<!doctype html>" in html.lower()


def test_v5_image_embed_single_stat_per_file(tmp_path: Path, monkeypatch) -> None:
    """V5 性能:每张候选图的 stat 系统调用恰 **1 次**(旧实现 is_file+stat 为 2 次)。

    读取调用计数:小图恰好读 1 次;大图 / 缺失图零读取、只列文件名。
    """
    small = tmp_path / "small.png"
    small.write_bytes(_mini_png())
    big = tmp_path / "big.png"
    big.write_bytes(b"\x89PNG" + b"\x00" * (60 * 1024 + 1))
    missing = tmp_path / "missing.png"

    stat_calls: dict[str, int] = {}
    read_calls: dict[str, int] = {}
    real_stat = pathlib.Path.stat
    real_read = pathlib.Path.read_bytes

    def counting_stat(self, *args, **kwargs):
        stat_calls[str(self)] = stat_calls.get(str(self), 0) + 1
        return real_stat(self, *args, **kwargs)

    def counting_read(self, *args, **kwargs):
        read_calls[str(self)] = read_calls.get(str(self), 0) + 1
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "stat", counting_stat)
    monkeypatch.setattr(pathlib.Path, "read_bytes", counting_read)
    html = render_report(
        _make_entry(),
        bundle=None,
        report_dict=_sample_report_dict([str(small), str(big), str(missing)]),
    )
    monkeypatch.undo()  # 计数到此为止

    # 每文件单次 stat:存在性 + 常规文件 + 大小一次拿全
    assert stat_calls[str(small)] == 1
    assert stat_calls[str(big)] == 1
    assert stat_calls[str(missing)] == 1
    # 只有小图被真正读取一次;大图先 stat 判大小即放弃,缺失图 stat 即失败
    assert read_calls[str(small)] == 1
    assert str(big) not in read_calls
    assert str(missing) not in read_calls
    assert html.count('src="data:image/png;base64,') == 1
    assert "big.png" in html and "missing.png" in html  # 只列名
