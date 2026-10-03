# -*- coding: utf-8 -*-
"""A172 收官汇总报告(report.run_summary.render_run_summary)单元测试。

全部离线、零外呼、零真实门户;文件落盘只写 ``tmp_path``。
覆盖:双件落盘与返回路径、全字段渲染、鸭子容错(空 dict / 对象 /
缺字段)、判定分布表、并发档位段(高档注记)、举报准备、错误清单
截断、escape 注入、深目录自动创建、固定结论句(红线 36 口径)。
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from netsentinel.report.run_summary import (
    CONCLUSION,
    HIGH_TIER_NOTE,
    MAX_ERRORS,
    VERDICT_CN,
    render_run_summary,
)


# ---------------------------------------------------------------------------
# 夹具辅助
# ---------------------------------------------------------------------------

def _full_stats() -> dict:
    """契约 §2 A172 形状的全字段战况(高档档位,触发高档注记)。"""
    return {
        "sites": 12,
        "scanned": 11,
        "failed": 1,
        "verdict_dist": {"clean": 5, "suspect": 4, "nsfw": 3},
        "groups": 4,
        "tier": "high",
        "workers": 3,
        "cores": 4,
        "cost_est": 1.25,
        "budget_used": 0.5,
        "attest_pending": ["example.com.cn(含 3 个关联站点)", "bad-site.net"],
        "ready_count": 6,
        "wall_s": 123.4,
        "errors": [
            ("https://down.example.com/", "连接超时"),
            ("https://blocked.example.org/", "HTTP 403 拒绝访问"),
        ],
        "generated_at": "2026-10-02 09:30:00",
    }


def _render(stats: dict, tmp_path: Path, name: str = "run_summary") -> tuple[str, str, str, str]:
    """渲染到 tmp_path 下,返回 (md_path, html_path, md_text, html_text)。"""
    md_path, html_path = render_run_summary(stats, str(tmp_path / name))
    return (
        md_path,
        html_path,
        Path(md_path).read_text(encoding="utf-8"),
        Path(html_path).read_text(encoding="utf-8"),
    )


@dataclass
class _DuckStats:
    """stats 的鸭子对象替身(走 getattr 路径;字段与 _full_stats 同值)。"""

    sites: int = 12
    scanned: int = 11
    failed: int = 1
    verdict_dist: dict = field(
        default_factory=lambda: {"clean": 5, "suspect": 4, "nsfw": 3}
    )
    groups: int = 4
    tier: str = "high"
    workers: int = 3
    cores: int = 4
    cost_est: float = 1.25
    budget_used: float = 0.5
    attest_pending: list = field(
        default_factory=lambda: ["example.com.cn(含 3 个关联站点)", "bad-site.net"]
    )
    ready_count: int = 6
    wall_s: float = 123.4
    errors: list = field(
        default_factory=lambda: [
            ("https://down.example.com/", "连接超时"),
            ("https://blocked.example.org/", "HTTP 403 拒绝访问"),
        ]
    )
    generated_at: str = "2026-10-02 09:30:00"


# ---------------------------------------------------------------------------
# 落盘 / 返回路径
# ---------------------------------------------------------------------------

def test_both_products_written_and_returned(tmp_path: Path) -> None:
    """两产物落盘,返回路径与 {out_base}.md/.html 一一对应。"""
    base = tmp_path / "out" / "run_summary"
    md_path, html_path = render_run_summary(_full_stats(), str(base))
    assert md_path == str(tmp_path / "out" / "run_summary.md")
    assert html_path == str(tmp_path / "out" / "run_summary.html")
    assert Path(md_path).is_file()
    assert Path(html_path).is_file()
    assert Path(md_path).read_text(encoding="utf-8").startswith("# 收官汇总报告")


def test_deep_out_base_auto_mkdir(tmp_path: Path) -> None:
    """out_base 深目录(多级不存在)自动创建,双件照常落盘。"""
    base = tmp_path / "a" / "b" / "c" / "d" / "run"
    md_path, html_path = render_run_summary({"sites": 1}, str(base))
    assert Path(md_path).is_file()
    assert Path(html_path).is_file()
    assert (tmp_path / "a" / "b" / "c" / "d").is_dir()


def test_written_utf8_chinese(tmp_path: Path) -> None:
    """中文内容按 UTF-8 落盘(读回无乱码、无异常)。"""
    _, _, md_text, html_text = _render(_full_stats(), tmp_path)
    assert "收官汇总报告" in md_text
    assert "收官汇总报告" in html_text


# ---------------------------------------------------------------------------
# 全字段渲染 / 鸭子容错
# ---------------------------------------------------------------------------

def test_full_stats_overview_rendered(tmp_path: Path) -> None:
    """总览表:站点 / 扫描 / 失败 / 组数 / 成本 / 预算 / 耗时全量渲染。"""
    _, _, md_text, html_text = _render(_full_stats(), tmp_path)
    for text in (md_text, html_text):
        assert "12" in text  # sites
        assert "11" in text  # scanned
        assert "案件组数" in text and "4" in text  # groups
        assert "1.25" in text  # cost_est
        assert "0.50" in text  # budget_used
        assert "123.4" in text  # wall_s
        assert "2026-10-02 09:30:00" in text  # generated_at
    assert "| 站点数 | 12 |" in md_text
    assert "| 已扫描 | 11 |" in md_text
    assert "| 失败 | 1 |" in md_text
    assert "| 总耗时 | 123.4 秒 |" in md_text


def test_duck_object_stats_via_getattr(tmp_path: Path) -> None:
    """stats 为鸭子对象(getattr 路径)与 dict 渲染等价。"""
    md1, _, md_obj, _ = _render(_DuckStats(), tmp_path, name="duck")
    _, _, md_dict, _ = _render(_full_stats(), tmp_path, name="dict")
    assert md_obj == md_dict
    assert Path(md1).is_file()


def test_empty_dict_renders(tmp_path: Path) -> None:
    """空 dict 也能完整出稿:数值缺省 0,不抛错。"""
    md_path, html_path = render_run_summary({}, str(tmp_path / "empty"))
    md_text = Path(md_path).read_text(encoding="utf-8")
    html_text = Path(html_path).read_text(encoding="utf-8")
    assert CONCLUSION in md_text
    assert CONCLUSION in html_text
    assert "| 站点数 | 0 |" in md_text
    assert "| 档位 | mid |" in md_text  # tier 缺省 mid
    assert "无错误记录" in md_text


def test_missing_numeric_fields_default_zero(tmp_path: Path) -> None:
    """只给部分字段:其余数值一律 0,结构完整。"""
    _, _, md_text, html_text = _render({"sites": 7}, tmp_path)
    assert "| 站点数 | 7 |" in md_text
    assert "| 已扫描 | 0 |" in md_text
    assert "| 失败 | 0 |" in md_text
    assert "| 案件组数 | 0 |" in md_text
    assert "| 预估成本(元) | 0.00 |" in md_text
    assert "| 总耗时 | — |" in md_text  # wall_s 缺失占位
    assert ">0</td>" in html_text  # 数值缺省 0 在 HTML 表格中同样渲染


def test_none_stats_tolerated(tmp_path: Path) -> None:
    """stats=None 容错为空汇总(收官链不因空输入中断)。"""
    md_path, html_path = render_run_summary(None, str(tmp_path / "none"))  # type: ignore[arg-type]
    assert CONCLUSION in Path(md_path).read_text(encoding="utf-8")
    assert CONCLUSION in Path(html_path).read_text(encoding="utf-8")


def test_groups_list_counted_as_len(tmp_path: Path) -> None:
    """groups 传 list(A168 组摘要行形状)→ 按 len 计数。"""
    _, _, md_text, _ = _render(
        {"groups": [{"name": "g1"}, {"name": "g2"}, {"name": "g3"}]}, tmp_path
    )
    assert "| 案件组数 | 3 |" in md_text


# ---------------------------------------------------------------------------
# 判定分布表
# ---------------------------------------------------------------------------

def test_verdict_dist_fixed_three_rows(tmp_path: Path) -> None:
    """分布表固定三行:clean/suspect/nsfw 中文名 + 数量。"""
    _, _, md_text, html_text = _render(_full_stats(), tmp_path)
    for text in (md_text, html_text):
        assert f"{VERDICT_CN['clean']}(clean)" in text
        assert f"{VERDICT_CN['suspect']}(suspect)" in text
        assert f"{VERDICT_CN['nsfw']}(nsfw)" in text
    assert f"| {VERDICT_CN['clean']}(clean) | 5 |" in md_text
    assert f"| {VERDICT_CN['suspect']}(suspect) | 4 |" in md_text
    assert f"| {VERDICT_CN['nsfw']}(nsfw) | 3 |" in md_text


def test_verdict_dist_missing_keys_default_zero(tmp_path: Path) -> None:
    """分布缺键 → 对应行数量 0(表恒三行起步)。"""
    _, _, md_text, _ = _render({"verdict_dist": {"nsfw": 2}}, tmp_path)
    assert f"| {VERDICT_CN['clean']}(clean) | 0 |" in md_text
    assert f"| {VERDICT_CN['suspect']}(suspect) | 0 |" in md_text
    assert f"| {VERDICT_CN['nsfw']}(nsfw) | 2 |" in md_text


def test_verdict_dist_non_mapping_tolerated(tmp_path: Path) -> None:
    """verdict_dist 非映射(脏数据)→ 按空分布渲染,不抛错。"""
    _, _, md_text, _ = _render({"verdict_dist": "oops"}, tmp_path)
    assert f"| {VERDICT_CN['clean']}(clean) | 0 |" in md_text


def test_verdict_dist_extra_key_appended(tmp_path: Path) -> None:
    """未知判定键追加在固定三行之后(不丢数据)。"""
    _, _, md_text, _ = _render(
        {"verdict_dist": {"clean": 1, "video_nsfw": 7}}, tmp_path
    )
    assert "video_nsfw(其他) | 7 |" in md_text


# ---------------------------------------------------------------------------
# 并发档位段
# ---------------------------------------------------------------------------

def test_tier_section_fields(tmp_path: Path) -> None:
    """档位段:档位 / workers / cores 三项齐备。"""
    _, _, md_text, html_text = _render(
        {"tier": "mid", "workers": 8, "cores": 16}, tmp_path
    )
    for text in (md_text, html_text):
        assert "并发档位" in text
        assert "mid" in text
        assert "16" in text
    assert "| 档位 | mid |" in md_text
    assert "| workers | 8 |" in md_text
    assert "| CPU 核数(cores) | 16 |" in md_text


def test_high_tier_note_present(tmp_path: Path) -> None:
    """高档(high):固定注记"本地计算全压榨;对外礼貌间隔与举报频控未放宽"。"""
    _, _, md_text, html_text = _render({"tier": "high", "workers": 3, "cores": 4}, tmp_path)
    assert HIGH_TIER_NOTE in md_text
    assert HIGH_TIER_NOTE in html_text
    assert "红线 35" in md_text  # 注记锚定压榨边界红线


def test_low_mid_tier_note_absent(tmp_path: Path) -> None:
    """低 / 中档:不出现高档注记。"""
    for tier in ("low", "mid"):
        _, _, md_text, html_text = _render({"tier": tier}, tmp_path, name=f"r-{tier}")
        assert HIGH_TIER_NOTE not in md_text
        assert HIGH_TIER_NOTE not in html_text


# ---------------------------------------------------------------------------
# 举报准备 / 错误清单
# ---------------------------------------------------------------------------

def test_attest_pending_and_ready_rendered(tmp_path: Path) -> None:
    """举报准备:ready 计数与待声明组名逐个渲染。"""
    _, _, md_text, html_text = _render(_full_stats(), tmp_path)
    for text in (md_text, html_text):
        assert "已声明已批准(ready)" in text
        assert "6 条" in text
        assert "example.com.cn(含 3 个关联站点)" in text
        assert "bad-site.net" in text
    assert "待声明组:2 个——" in md_text


def test_attest_pending_empty_placeholder(tmp_path: Path) -> None:
    """无待声明组:占位文案"无(全部已声明或无已确认条目)"。"""
    _, _, md_text, html_text = _render({"ready_count": 3}, tmp_path)
    for text in (md_text, html_text):
        assert "无(全部已声明或无已确认条目)" in text


def test_errors_all_listed(tmp_path: Path) -> None:
    """错误清单:URL 与原因逐条渲染(MD 表 + HTML 表)。"""
    _, _, md_text, html_text = _render(_full_stats(), tmp_path)
    for text in (md_text, html_text):
        assert "https://down.example.com/" in text
        assert "连接超时" in text
        assert "https://blocked.example.org/" in text
        assert "HTTP 403 拒绝访问" in text


def test_errors_truncated_to_ten(tmp_path: Path) -> None:
    """错误超过 10 条只列前 10,并注明总数。"""
    errors = [(f"https://s{i:02d}.example.com/", f"原因{i}") for i in range(15)]
    _, _, md_text, html_text = _render({"errors": errors}, tmp_path)
    assert "https://s00.example.com/" in md_text
    assert "https://s09.example.com/" in md_text
    assert "https://s10.example.com/" not in md_text  # 第 11 条起不渲染
    assert "共 15 条" in md_text and f"前 {MAX_ERRORS} 条" in md_text
    assert "https://s10.example.com/" not in html_text
    assert "共 15 条" in html_text


def test_error_item_shapes_tolerated(tmp_path: Path) -> None:
    """错误项三种形状容错:二元组 / {"url","reason"} 映射 / 单值字符串。"""
    errors = [
        ("https://a.example.com/", "超时"),
        {"url": "https://b.example.com/", "reason": "403"},
        "https://c.example.com/",
    ]
    _, _, md_text, _ = _render({"errors": errors}, tmp_path)
    assert "https://a.example.com/" in md_text and "超时" in md_text
    assert "https://b.example.com/" in md_text and "403" in md_text
    assert "https://c.example.com/" in md_text


def test_no_errors_placeholder(tmp_path: Path) -> None:
    """无错误:清单段显示"无错误记录"。"""
    _, _, md_text, html_text = _render({}, tmp_path)
    assert "无错误记录" in md_text
    assert "无错误记录" in html_text


# ---------------------------------------------------------------------------
# 固定结论段(红线 36 口径)
# ---------------------------------------------------------------------------

def test_conclusion_fixed_sentence_md_and_html(tmp_path: Path) -> None:
    """结论段固定句逐字出现(红线 36:代理无自主提交权)。"""
    _, _, md_text, html_text = _render(_full_stats(), tmp_path)
    assert CONCLUSION in md_text
    assert CONCLUSION in html_text
    assert CONCLUSION == (
        "本汇总由结案代理生成;任何举报须经逐组人工声明与逐条人工门完成,"
        "代理无自主提交权。"
    )


def test_conclusion_red_in_html(tmp_path: Path) -> None:
    """HTML 结论段红字:redline 样式类包裹固定句。"""
    _, html_path, _, html_text = _render(_full_stats(), tmp_path)
    assert '<p class="decl redline">' in html_text
    assert "p.redline" in html_text and "color:#c00000" in html_text
    start = html_text.index('<p class="decl redline">')
    assert CONCLUSION in html_text[start : start + 200]


# ---------------------------------------------------------------------------
# HTML 结构 / escape / 时间
# ---------------------------------------------------------------------------

def test_html_doctype_title_inline_css(tmp_path: Path) -> None:
    """HTML:doctype、zh-CN、内联 CSS、公文风抬头。"""
    _, _, _, html_text = _render(_full_stats(), tmp_path)
    assert html_text.startswith("<!doctype html>")
    assert '<html lang="zh-CN">' in html_text
    assert "<title>收官汇总报告</title>" in html_text
    assert "<style>" in html_text  # 内联 CSS
    assert "SimSun" in html_text  # 宋体系公文风
    assert "净网哨兵 NetSentinel" in html_text


def test_html_six_section_headings(tmp_path: Path) -> None:
    """HTML 六段结构与 MD 同构:总览/分布/档位/举报准备/错误/结论。"""
    _, _, md_text, html_text = _render(_full_stats(), tmp_path)
    for heading in (
        "一、总览",
        "二、判定分布",
        "三、并发档位",
        "四、举报准备",
        "五、错误清单",
        "六、结论",
    ):
        assert f"## {heading}" in md_text
        assert f"<h2>{heading}</h2>" in html_text


def test_escapes_script_injection(tmp_path: Path) -> None:
    """script 注入(URL/组名/原因/时间)在 HTML 中全量转义。"""
    stats = {
        "tier": "<script>alert(1)</script>",
        "generated_at": '<img src=x onerror="alert(2)">',
        "attest_pending": ["<script>组</script>"],
        "errors": [("<script>alert(3)</script>", "原因<b>加粗</b>")],
    }
    _, _, md_text, html_text = _render(stats, tmp_path)
    assert "&lt;script&gt;" in html_text
    assert "&lt;img src=x onerror=&quot;alert(2)&quot;&gt;" in html_text
    assert "<script>" not in html_text  # 原样标签零残留
    assert "<b>加粗</b>" not in html_text
    assert "&lt;b&gt;加粗&lt;/b&gt;" in html_text
    # MD 不做 HTML 转义,但表格竖线被转义、内容原样保留
    assert "<script>alert(3)</script>" in md_text


def test_md_pipe_in_cell_escaped(tmp_path: Path) -> None:
    """MD 表格单元格内的竖线被转义,不撑破表格。"""
    _, _, md_text, _ = _render(
        {"attest_pending": ["组|名"], "errors": [("https://a/|b", "原因|x")]}, tmp_path
    )
    assert "组\\|名" in md_text
    assert "https://a/\\|b" in md_text


def test_generated_at_override_and_default(tmp_path: Path) -> None:
    """生成时间:显式 given 覆盖;缺失回退当前本地时间(今日前缀)。"""
    _, _, md_text, _ = _render({"generated_at": "2026-01-01 00:00:00"}, tmp_path)
    assert "2026-01-01 00:00:00" in md_text
    _, _, md_text2, _ = _render({}, tmp_path, name="t2")
    assert datetime.now().strftime("%Y-%m-%d") in md_text2


def test_cores_fallback_when_missing(tmp_path: Path) -> None:
    """cores 缺失回退 os.cpu_count()(取不到按 2),恒为正整数。"""
    import os

    _, _, md_text, _ = _render({"workers": 2}, tmp_path)
    expected = os.cpu_count() or 2
    assert f"| CPU 核数(cores) | {expected} |" in md_text


def test_worker_duck_partial_object(tmp_path: Path) -> None:
    """部分字段的鸭子对象:缺省渲染零值 + 固定结构完整。"""
    duck = SimpleNamespace(sites=3, tier="high", workers=2, cores=4)
    md_path, html_path = render_run_summary(duck, str(tmp_path / "partial"))  # type: ignore[arg-type]
    md_text = Path(md_path).read_text(encoding="utf-8")
    assert Path(html_path).is_file()
    assert "| 站点数 | 3 |" in md_text
    assert "| 已扫描 | 0 |" in md_text
    assert HIGH_TIER_NOTE in md_text
    assert CONCLUSION in md_text


def test_html_escape_via_stdlib_consistency(tmp_path: Path) -> None:
    """转义口径与标准库 html.escape 一致(quote=True)。"""
    stats = {"attest_pending": ['a"b&c']}
    _, _, _, html_text = _render(stats, tmp_path)
    assert html.escape('a"b&c', quote=True) in html_text
