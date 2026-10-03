# -*- coding: utf-8 -*-
"""A114 批量结案报告(batch_report.render_batch_report)单元测试。

全部离线、零外呼、零真实门户;文件落盘只写 ``tmp_path``。
"""
from __future__ import annotations

import html
from pathlib import Path

from netsentinel.report.batch_report import render_batch_report


# ---------------------------------------------------------------------------
# 夹具辅助
# ---------------------------------------------------------------------------

def _summary_base() -> dict:
    """A112 run_batch 形状的最小批次摘要(1 成功 / 1 失败 / 1 未执行)。"""
    return {
        "submitted": 2,
        "failed": 1,
        "rate_limited": False,
        "paused": False,
        "results": [
            {
                "entry_id": 101,
                "group_name": "example.com.cn(含 3 个关联站点)",
                "ok": True,
                "submitted": True,
                "error": "",
                "portal": "12377",
            },
            {
                "entry_id": 102,
                "group_name": "bad-site.net",
                "ok": False,
                "submitted": False,
                "error": "执行异常:TimeoutError:页面加载超时",
                "portal": "shdf",
            },
            {
                "entry_id": 103,
                "group_name": "dry-run.org",
                "ok": True,
                "submitted": False,
                "error": "",
                "portal": "12377",
            },
        ],
        "note": "",
    }


def _render(summary: dict, tmp_path: Path, name: str = "report.html") -> tuple[str, Path]:
    out = tmp_path / name
    doc = render_batch_report("BAT-20261002-01", summary, str(out))
    return doc, out


# ---------------------------------------------------------------------------
# 一、文档骨架与六区块标题(抬头 + 五个编号区块)
# ---------------------------------------------------------------------------

def test_doctype_charset_and_title(tmp_path: Path) -> None:
    doc, _ = _render(_summary_base(), tmp_path)
    assert doc.startswith("<!doctype html>")
    assert '<meta charset="utf-8">' in doc
    assert '<html lang="zh-CN">' in doc
    assert "<title>批量举报结案报告(人工核对稿)</title>" in doc


def test_six_block_headings(tmp_path: Path) -> None:
    """完整摘要(含声明与截图)→ 抬头 + 一至五共六个区块标题齐全。"""
    shot = tmp_path / "shot_101.png"
    shot.write_bytes(b"\x89PNG fake")
    summary = _summary_base()
    summary["attestations"] = [
        {"group_name": "example.com.cn", "reviewer": "张三", "ts": "2026-10-02 09:00:00"}
    ]
    summary["screenshots"] = [str(shot)]
    doc, _ = _render(summary, tmp_path)
    assert "<h1>批量举报结案报告(人工核对稿)</h1>" in doc
    assert "<h2>一、批次信息</h2>" in doc
    assert "<h2>二、逐条状态</h2>" in doc
    assert "<h2>三、声明清单</h2>" in doc
    assert "<h2>四、截图清单</h2>" in doc
    assert "<h2>五、结论</h2>" in doc


# ---------------------------------------------------------------------------
# 二、批次信息:批次号回显 / 计数 / 标志 / 生成时间 / 备注
# ---------------------------------------------------------------------------

def test_batch_id_echoed(tmp_path: Path) -> None:
    doc, _ = _render(_summary_base(), tmp_path)
    assert "BAT-20261002-01" in doc
    assert "<th>批次号</th>" in doc
    # 抬头 meta 行也带批次号
    assert "批次 BAT-20261002-01" in doc


def test_batch_info_counts_and_flags(tmp_path: Path) -> None:
    summary = _summary_base()
    summary["rate_limited"] = True
    summary["paused"] = True
    doc, _ = _render(summary, tmp_path)
    assert "<th>提交成功</th>" in doc and "2 条" in doc
    assert "<th>失败</th>" in doc and "1 条" in doc
    assert "<th>频控挂起</th>" in doc and ">是<" in doc
    assert "<th>人工暂停</th>" in doc and ">否<" not in doc.split("频控挂起")[1][:200]


def test_generated_at_override_and_default(tmp_path: Path) -> None:
    summary = _summary_base()
    summary["generated_at"] = "2026-10-02 09:30:00"
    doc, _ = _render(summary, tmp_path)
    assert "<th>生成时间</th>" in doc
    assert "2026-10-02 09:30:00" in doc
    # 未提供时回退当前时间(形如 YYYY-MM-DD HH:MM:SS)
    doc2, _ = _render(_summary_base(), tmp_path, name="report2.html")
    assert "生成时间" in doc2
    row = doc2.split("<th>生成时间</th>")[1].split("</td>")[0]
    assert len(row.split("<td>")[1]) == 19  # "2026-10-02 09:30:00" 定长


def test_note_row_only_when_present(tmp_path: Path) -> None:
    doc_without, _ = _render(_summary_base(), tmp_path, name="a.html")
    assert "备注" not in doc_without
    summary = _summary_base()
    summary["note"] = "额度/间隔限制,可续批:今日额度已用尽"
    doc_with, _ = _render(summary, tmp_path, name="b.html")
    assert "<th>备注</th>" in doc_with
    assert "额度/间隔限制,可续批" in doc_with


# ---------------------------------------------------------------------------
# 三、逐条状态表:中文状态映射 / 门户 / 空 results
# ---------------------------------------------------------------------------

def test_status_cn_mapping(tmp_path: Path) -> None:
    doc, _ = _render(_summary_base(), tmp_path)
    assert "已提交✔" in doc
    assert "失败✘" in doc
    assert "未执行" in doc
    # 失败原因中文回显
    assert "执行异常:TimeoutError:页面加载超时" in doc
    # 表头六列齐全
    for head in ("序号", "组名", "条目", "门户", "结果", "原因"):
        assert f"<th>{head}</th>" in doc
    # 条目号与组名回显
    assert ">101<" in doc
    assert "example.com.cn(含 3 个关联站点)" in doc


def test_portal_cn_display(tmp_path: Path) -> None:
    summary = _summary_base()
    summary["results"][2]["portal"] = "unknown-gov"
    doc, _ = _render(summary, tmp_path)
    assert "12377(中央网信办)" in doc
    assert "shdf(扫黄打非)" in doc
    # 未知门户原样回显,不抛错
    assert "unknown-gov" in doc


def test_empty_results_shows_placeholder(tmp_path: Path) -> None:
    summary = _summary_base()
    summary["results"] = []
    doc, _ = _render(summary, tmp_path)
    assert "无条目" in doc
    assert "<th>序号</th>" in doc  # 表头仍在
    assert "已提交✔" not in doc


# ---------------------------------------------------------------------------
# 四、可选区块:声明清单 / 截图清单(缺失或为空 → 整段隐藏)
# ---------------------------------------------------------------------------

def test_optional_sections_hidden_when_missing_or_empty(tmp_path: Path) -> None:
    doc, _ = _render(_summary_base(), tmp_path)  # 无 attestations / screenshots 键
    assert "三、声明清单" not in doc
    assert "四、截图清单" not in doc
    assert "五、结论" in doc  # 结论编号不受可选段影响
    summary = _summary_base()
    summary["attestations"] = []
    summary["screenshots"] = []
    doc2, _ = _render(summary, tmp_path, name="empty.html")
    assert "三、声明清单" not in doc2
    assert "四、截图清单" not in doc2


def test_attestation_table_rows(tmp_path: Path) -> None:
    summary = _summary_base()
    summary["attestations"] = [
        {"group_name": "example.com.cn", "reviewer": "张三", "ts": "2026-10-02 09:00:00"},
        {"group_name": "bad-site.net", "reviewer": "李四", "ts": "2026-10-02 09:05:00"},
    ]
    doc, _ = _render(summary, tmp_path)
    assert "<th>组名</th>" in doc and "<th>审核人</th>" in doc and "<th>声明时间</th>" in doc
    assert "张三" in doc and "李四" in doc
    assert "2026-10-02 09:05:00" in doc


def test_screenshot_existing_and_missing(tmp_path: Path) -> None:
    shot = tmp_path / "sub" / "shot_ok.png"
    shot.parent.mkdir()
    shot.write_bytes(b"\x89PNG fake")
    missing = tmp_path / "gone.png"
    summary = _summary_base()
    summary["screenshots"] = [str(shot), str(missing)]
    doc, _ = _render(summary, tmp_path)
    assert str(shot) in doc and str(missing) in doc
    assert "已存档" in doc
    assert "文件缺失" in doc
    # 顺序稳定:已存档行在缺失行之前
    assert doc.index("已存档") < doc.index("文件缺失")


# ---------------------------------------------------------------------------
# 五、结论段固定文案
# ---------------------------------------------------------------------------

def test_conclusion_fixed_sentences(tmp_path: Path) -> None:
    doc, _ = _render(_summary_base(), tmp_path)
    assert "本次批量执行每一条均经人工门完成验证码输入与最终确认" in doc
    assert "虚假举报承担法律责任" in doc


def test_rate_limited_and_paused_conclusion_notes(tmp_path: Path) -> None:
    summary = _summary_base()
    summary["rate_limited"] = True
    summary["paused"] = True
    doc, _ = _render(summary, tmp_path)
    assert "额度/间隔限制挂起" in doc
    assert "凭批次号续批" in doc


# ---------------------------------------------------------------------------
# 六、转义与落盘
# ---------------------------------------------------------------------------

def test_escapes_script_injection(tmp_path: Path) -> None:
    evil = '<script>alert("xss")</script>'
    summary = _summary_base()
    summary["results"][0]["group_name"] = evil
    summary["results"][1]["error"] = f"执行异常:{evil}"
    summary["attestations"] = [{"group_name": evil, "reviewer": "王&五", "ts": "t"}]
    doc, _ = _render(summary, tmp_path)
    assert html.escape(evil, quote=True) in doc  # 转义形态可寻
    assert "<script>alert" not in doc  # 原始注入串绝不出现在 HTML 正文
    assert "王&amp;五" in doc
    # 批次号同样转义
    out = tmp_path / "evil.html"
    doc2 = render_batch_report('<b>"BAD"', summary, str(out))
    assert html.escape('<b>"BAD"', quote=True) in doc2


def test_written_utf8_and_mkdir(tmp_path: Path) -> None:
    out = tmp_path / "深层" / "目录" / "BAT-01.html"
    doc = render_batch_report("BAT-X", _summary_base(), str(out))
    assert out.is_file()
    assert out.parent.is_dir()  # 父目录自动创建
    assert out.read_text(encoding="utf-8") == doc  # 落盘内容与返回值一致
    assert out.read_bytes().decode("utf-8").startswith("<!doctype html>")


def test_summary_type_guard(tmp_path: Path) -> None:
    out = tmp_path / "bad.html"
    try:
        render_batch_report("BAT-X", "not-a-dict", str(out))  # type: ignore[arg-type]
    except TypeError as exc:
        assert "summary" in str(exc)
    else:  # pragma: no cover - 守卫必须抛错
        raise AssertionError("summary 非 dict 应抛 TypeError")
    assert not out.exists()  # 抛错前不落盘
