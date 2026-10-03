# -*- coding: utf-8 -*-
"""收官汇总报告渲染(NetSentinel V9 · A172)。

批量扫描(A108)跑完、结案代理(A168 ``SummaryAgent``)汇总之后,把整轮
运行战况渲染成**收官汇总报告**(Markdown + HTML 双件套),供运营者核对、
归档。与 ``report.html_report``(单站举报材料)、``report.batch_report``
(批量结案报告)同属公文风格,但**独立实现、互不依赖**;本模块只用
Python 标准库(html / os / datetime / pathlib),产物为零外部依赖的
离线文件(HTML 内联 CSS)。

核心入口 :func:`render_run_summary`:

- ``stats`` 鸭子形状(与契约 §2 A172 一致,**dict 或对象均可**,取值
  经 Mapping.get / getattr 双路容错,任何字段缺失都不抛错)::

      {"sites": int, "scanned": int, "failed": int,
       "verdict_dist": {"clean": n, "suspect": n, "nsfw": n},
       "groups": int | list,       # list → 取 len
       "tier": "low"|"mid"|"high", "workers": int, "cores": int(可选),
       "cost_est": float, "budget_used": float,
       "attest_pending": ["组名", ...], "ready_count": int,
       "wall_s": float(可选), "errors": [(url, 原因), ...](仅列前 10),
       "generated_at": "2026-10-02 09:30:00"(缺省取当前时间)}

- ``out_base`` 无扩展名输出基名,产物 ``{out_base}.md`` 与
  ``{out_base}.html``(父目录自动创建,UTF-8),返回两个路径字符串;
- Markdown 六段:一、总览表;二、判定分布表(clean/suspect/nsfw 固定
  三行 + 未知键追加);三、并发档位(档位/workers/cores;**高档**追加
  固定注记"本地计算全压榨;对外礼貌间隔与举报频控未放宽",红线 35);
  四、举报准备(ready 计数 + 待声明组清单);五、错误清单(前 10 条,
  超出注明总数);六、结论(固定红线声明句);
- HTML 与 Markdown 同结构,公文风内联 CSS(宋体系、A4 白纸、黑色细
  边框,打印友好),结论段**红字**加重;
- 所有动态插值一律 :func:`html.escape`(组名 / URL / 原因等可含
  ``<script>`` 之类特殊字符),Markdown 表格单元格转义竖线。

红线呼应(CONTRACTS-V9 §0 红线 36):结论段固定重申"本汇总由结案
代理生成;任何举报须经逐组人工声明与逐条人工门完成,代理无自主提交权"。
本模块只读 stats、只写报告文件,零网络、零外呼、零提交路径。

用法::

    from netsentinel.report.run_summary import render_run_summary

    md_path, html_path = render_run_summary(stats, "out/run_summary")
"""
from __future__ import annotations

import html as _html
import logging
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = ["CONCLUSION", "HIGH_TIER_NOTE", "VERDICT_CN", "render_run_summary"]

logger = logging.getLogger(__name__)

#: 文档抬头(Markdown 一级标题,也是 HTML <title> 与 <h1>)
DOC_TITLE = "收官汇总报告"

#: 结论段固定句(红线 36 口径:结案代理无自主提交权,逐字渲染)。
CONCLUSION = (
    "本汇总由结案代理生成;任何举报须经逐组人工声明与逐条人工门完成,"
    "代理无自主提交权。"
)

#: 高档(high)并发的固定注记(红线 35:压榨边界——只加速本地计算)。
HIGH_TIER_NOTE = "本地计算全压榨;对外礼貌间隔与举报频控未放宽"

#: verdict → 中文名(与 decision.review_queue / html_report 同口径)
VERDICT_CN: dict[str, str] = {
    "clean": "无风险",
    "suspect": "疑似",
    "nsfw": "高置信色情",
}

#: 判定分布固定三行(按既定顺序渲染;未知键追加在后)
_VERDICT_ORDER: tuple[str, ...] = ("clean", "suspect", "nsfw")

#: 错误清单最多展示条数(超出只注明总数)
MAX_ERRORS = 10

#: 生成时间格式(本地时区,精确到秒)
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"

#: CPU 核数取不到时的回退(契约 §1:os.cpu_count() 缺省回退 2)
_DEFAULT_CORES = 2

#: 内联 CSS:简洁公文风格(宋体系、A4 白纸、黑色细边框,打印友好;
#: 结论段 .redline 红字加重,与 batch_report 同族但独立维护)
_CSS = """body{margin:0;padding:24px 12px;background:#f5f5f5;
  font-family:"SimSun","Songti SC","Noto Serif CJK SC",serif;color:#1a1a1a;}
.sheet{max-width:900px;margin:0 auto;background:#fff;padding:44px 52px;
  border:1px solid #999;box-shadow:0 2px 8px rgba(0,0,0,.08);}
h1{font-size:22px;text-align:center;letter-spacing:2px;margin:0 0 6px;}
.doc-meta{text-align:center;color:#666;font-size:13px;margin:0 0 26px;}
h2{font-size:16px;border-left:4px solid #8b1e3f;padding-left:8px;
  margin:26px 0 10px;font-weight:700;}
table.info{width:100%;border-collapse:collapse;font-size:14px;margin:0 0 6px;}
table.info th,table.info td{border:1px solid #999;padding:6px 10px;
  text-align:left;word-break:break-all;}
table.info th{width:170px;background:#f2f2f2;font-weight:600;white-space:nowrap;}
table.grid{width:100%;border-collapse:collapse;font-size:13px;}
table.grid th,table.grid td{border:1px solid #999;padding:5px 8px;
  text-align:left;word-break:break-all;}
table.grid th{background:#f2f2f2;font-weight:600;white-space:nowrap;}
table.grid td.num{text-align:center;white-space:nowrap;}
p.note{font-size:13px;color:#555;line-height:1.8;margin:6px 0;}
p.decl{font-size:14px;line-height:2;text-indent:2em;margin:6px 0;}
p.redline{color:#c00000;font-weight:700;}
p.muted{font-size:13px;color:#555;line-height:1.8;}
p.foot{margin-top:34px;padding-top:10px;border-top:1px dashed #bbb;
  font-size:12px;color:#777;text-align:center;}
@media print{body{background:#fff;padding:0}.sheet{border:none;box-shadow:none}}"""


# ---------------------------------------------------------------------------
# 取值 / 格式化辅助(全部容错,任何字段缺失都不抛错)
# ---------------------------------------------------------------------------

def _esc(value: Any) -> str:
    """转义任意插值为安全 HTML 文本(html.escape,属性/正文通用)。"""
    return _html.escape(str(value), quote=True)


def _md_cell(value: Any) -> str:
    """Markdown 表格单元格文本:竖线转义,防动态文本撑破表格。"""
    return str(value).replace("|", "\\|")


def _field(stats: Any, name: str, default: Any = None) -> Any:
    """从 stats(dict 或鸭子对象)容错取字段;None / 缺失回退默认值。"""
    if isinstance(stats, Mapping):
        val = stats.get(name, default)
    else:
        val = getattr(stats, name, default)
    return default if val is None else val


def _as_int(value: Any, default: int = 0) -> int:
    """宽容整数转换(鸭子值可能是 None / 浮点 / 字符串);失败回退默认。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _as_float(value: Any, default: float = 0.0) -> float:
    """宽容浮点转换;失败回退默认。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _mapping(value: Any) -> dict:
    """容错取映射字段;非映射(含缺失)→ 空 dict。"""
    return dict(value) if isinstance(value, Mapping) else {}


def _groups_count(value: Any) -> int:
    """组数:groups 为 list/tuple/set → len;否则按整数宽容转换。"""
    if isinstance(value, (list, tuple, set, frozenset)):
        return len(value)
    return _as_int(value)


def _cores_of(stats: Any) -> int:
    """CPU 核数:stats.cores 优先;缺失 / 非法 → os.cpu_count()(回退 2)。"""
    cores = _as_int(_field(stats, "cores"), 0)
    if cores >= 1:
        return cores
    return os.cpu_count() or _DEFAULT_CORES


def _verdict_rows(dist: Mapping[str, Any]) -> list[tuple[str, int]]:
    """判定分布 → 展示行 [(中文(键), 数量)]:固定三行在前,未知键追加。"""
    rows: list[tuple[str, int]] = []
    for key in _VERDICT_ORDER:
        label = f"{VERDICT_CN[key]}({key})"
        rows.append((label, _as_int(dist.get(key))))
    for key, val in dist.items():
        if key in _VERDICT_ORDER:
            continue
        rows.append((f"{key}(其他)", _as_int(val)))
    return rows


def _error_rows(errors: Any) -> tuple[list[tuple[str, str]], int]:
    """错误清单 → (前 MAX_ERRORS 行, 总数)。

    每项容错三种形状:``(url, 原因)`` 二元组 / ``{"url","reason"}`` 映射 /
    任意单值(整体当 URL);非列表输入按空处理。
    """
    if not isinstance(errors, (list, tuple)):
        return [], 0
    rows: list[tuple[str, str]] = []
    for item in errors:
        if isinstance(item, Mapping):
            url = item.get("url", "")
            reason = item.get("reason", item.get("error", ""))
        elif isinstance(item, (list, tuple)):
            url = item[0] if len(item) >= 1 else ""
            reason = item[1] if len(item) >= 2 else ""
        else:
            url, reason = item, ""
        rows.append((str(url), str(reason)))
    return rows[:MAX_ERRORS], len(rows)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def render_run_summary(stats: dict[str, Any], out_base: str) -> tuple[str, str]:
    """渲染收官汇总报告(Markdown + HTML 双件),写盘并返回两个路径。

    :param stats:    整轮运行战况(dict 或鸭子对象,``getattr`` 容错),
        键见模块 docstring;任何字段缺失 / 类型不符都不抛错,数值按 0、
        文本按占位符渲染(空 stats 亦可完整出稿)。
    :param out_base: 输出基名(无扩展名);产物 ``{out_base}.md`` 与
        ``{out_base}.html``,父目录自动创建,UTF-8 写出。
    :return: ``(md_path, html_path)`` 两个落盘路径字符串。

    所有动态插值在 HTML 中均经 :func:`html.escape`,在 Markdown 中转义
    竖线;本函数只写这两个报告文件,零网络、零提交动作(红线 36)。
    """
    stats = stats if isinstance(stats, Mapping) else (stats if stats is not None else {})
    sites = _as_int(_field(stats, "sites"))
    scanned = _as_int(_field(stats, "scanned"))
    failed = _as_int(_field(stats, "failed"))
    dist = _mapping(_field(stats, "verdict_dist", {}))
    groups = _groups_count(_field(stats, "groups"))
    tier = str(_field(stats, "tier", "mid") or "mid").strip() or "mid"
    workers = _as_int(_field(stats, "workers"))
    cores = _cores_of(stats)
    cost_est = _as_float(_field(stats, "cost_est"))
    budget_used = _as_float(_field(stats, "budget_used"))
    attest_raw = _field(stats, "attest_pending", [])
    attest_pending = (
        [str(name) for name in attest_raw if str(name).strip()]
        if isinstance(attest_raw, (list, tuple))
        else []
    )
    ready_count = _as_int(_field(stats, "ready_count"))
    wall_raw = _field(stats, "wall_s")
    wall_text = (
        f"{_as_float(wall_raw):.1f} 秒"
        if isinstance(wall_raw, (int, float)) and not isinstance(wall_raw, bool)
        else "—"
    )
    errors, error_total = _error_rows(_field(stats, "errors", []))
    generated_at = str(
        _field(stats, "generated_at") or datetime.now().strftime(_TS_FORMAT)
    )
    high_note = tier.lower() == "high"

    # ---------------- Markdown ----------------
    dist_rows = _verdict_rows(dist)
    md: list[str] = [
        f"# {DOC_TITLE}",
        "",
        f"- 生成时间:{generated_at}",
        f"- 并发档位:{tier}(workers={workers},cores={cores})",
        "",
        "## 一、总览",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 站点数 | {sites} |",
        f"| 已扫描 | {scanned} |",
        f"| 失败 | {failed} |",
        f"| 案件组数 | {groups} |",
        f"| 预估成本(元) | {cost_est:.2f} |",
        f"| 预算已用(元) | {budget_used:.2f} |",
        f"| 总耗时 | {wall_text} |",
        f"| 生成时间 | {_md_cell(generated_at)} |",
        "",
        "## 二、判定分布",
        "",
        "| 判定 | 数量 |",
        "| --- | --- |",
    ]
    md.extend(f"| {_md_cell(label)} | {count} |" for label, count in dist_rows)
    md.extend(
        [
            "",
            "## 三、并发档位",
            "",
            "| 项目 | 数值 |",
            "| --- | --- |",
            f"| 档位 | {_md_cell(tier)} |",
            f"| workers | {workers} |",
            f"| CPU 核数(cores) | {cores} |",
        ]
    )
    if high_note:
        md.extend(
            ["", f"> 注:{HIGH_TIER_NOTE}(红线 35:压榨边界只及本地计算)。"]
        )
    md.extend(
        [
            "",
            "## 四、举报准备",
            "",
            f"- 已声明已批准(ready):{ready_count} 条",
            "- 待声明组:"
            + (
                f"{len(attest_pending)} 个——"
                + "、".join(_md_cell(name) for name in attest_pending)
                if attest_pending
                else "无(全部已声明或无已确认条目)"
            ),
            "",
            "## 五、错误清单",
            "",
        ]
    )
    if errors:
        md.extend(
            [
                "| 序号 | 站点 | 原因 |",
                "| --- | --- | --- |",
            ]
        )
        md.extend(
            f"| {idx} | {_md_cell(url) or '—'} | {_md_cell(reason) or '—'} |"
            for idx, (url, reason) in enumerate(errors, start=1)
        )
        if error_total > MAX_ERRORS:
            md.append(f"| …… | (共 {error_total} 条,仅列前 {MAX_ERRORS} 条) | |")
    else:
        md.append("无错误记录。")
    md.extend(
        [
            "",
            "## 六、结论",
            "",
            f"> **{CONCLUSION}**",
            "",
        ]
    )
    md_text = "\n".join(md)

    # ---------------- HTML ----------------
    dist_html = "".join(
        f'\n      <tr><td>{_esc(label)}</td><td class="num">{count}</td></tr>'
        for label, count in dist_rows
    )
    high_note_html = (
        f'\n  <p class="note">注:{_esc(HIGH_TIER_NOTE)}'
        "(红线 35:压榨边界只及本地计算)。</p>"
        if high_note
        else ""
    )
    if errors:
        err_body = "".join(
            f'\n      <tr><td class="num">{idx}</td>'
            f"<td>{_esc(url or '—')}</td><td>{_esc(reason or '—')}</td></tr>"
            for idx, (url, reason) in enumerate(errors, start=1)
        )
        overflow = (
            f'\n  <p class="muted">(共 {error_total} 条错误,'
            f"仅列前 {MAX_ERRORS} 条;其余见运行日志。)</p>"
            if error_total > MAX_ERRORS
            else ""
        )
        errors_html = (
            '<table class="grid">\n      <tr><th>序号</th><th>站点</th>'
            "<th>原因</th></tr>" + err_body + "\n    </table>" + overflow
        )
    else:
        errors_html = '<p class="muted">无错误记录。</p>'

    doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(DOC_TITLE)}</title>
<style>
{_CSS}
</style>
</head>
<body>
<div class="sheet">
  <h1>{_esc(DOC_TITLE)}</h1>
  <p class="doc-meta">净网哨兵 NetSentinel · 离线生成 · 仅供人工核对与归档</p>

  <h2>一、总览</h2>
  <table class="info">
    <tr><th>站点数</th><td>{sites}</td></tr>
    <tr><th>已扫描</th><td>{scanned}</td></tr>
    <tr><th>失败</th><td>{failed}</td></tr>
    <tr><th>案件组数</th><td>{groups}</td></tr>
    <tr><th>预估成本(元)</th><td>{cost_est:.2f}</td></tr>
    <tr><th>预算已用(元)</th><td>{budget_used:.2f}</td></tr>
    <tr><th>总耗时</th><td>{_esc(wall_text)}</td></tr>
    <tr><th>生成时间</th><td>{_esc(generated_at)}</td></tr>
  </table>

  <h2>二、判定分布</h2>
  <table class="grid">
      <tr><th>判定</th><th>数量</th></tr>{dist_html}
    </table>

  <h2>三、并发档位</h2>
  <table class="info">
    <tr><th>档位</th><td>{_esc(tier)}</td></tr>
    <tr><th>workers</th><td>{workers}</td></tr>
    <tr><th>CPU 核数(cores)</th><td>{cores}</td></tr>
  </table>{high_note_html}

  <h2>四、举报准备</h2>
  <table class="info">
    <tr><th>已声明已批准(ready)</th><td>{ready_count} 条</td></tr>
    <tr><th>待声明组</th><td>{_esc("、".join(attest_pending) if attest_pending else "无(全部已声明或无已确认条目)")}</td></tr>
  </table>

  <h2>五、错误清单</h2>
  {errors_html}

  <h2>六、结论</h2>
  <p class="decl redline">{_esc(CONCLUSION)}</p>

  <p class="foot">本报告为整轮运行的收官留痕与核对材料;任何举报以逐组人工声明并经逐条人工门确认后的内容为准。</p>
</div>
</body>
</html>
"""

    base = str(out_base)
    md_path = Path(base + ".md")
    html_path = Path(base + ".html")
    for target in (md_path, html_path):
        target.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(md_text, encoding="utf-8")
    html_path.write_text(doc, encoding="utf-8")
    logger.info(
        "收官汇总报告已写出:%s 与 %s(%d 组 / %d 站点)",
        md_path, html_path, groups, sites,
    )
    return str(md_path), str(html_path)
