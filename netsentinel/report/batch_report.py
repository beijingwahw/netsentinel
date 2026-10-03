# -*- coding: utf-8 -*-
"""批量举报结案报告渲染(NetSentinel V6 · A114)。

一批举报(A112 ``run_batch`` 顺序逐条执行)干完之后,把批次摘要渲染成
**单文件 HTML 结案报告(人工核对稿)**供运营者核对、归档。与
``report.html_report``(单站举报材料)同属公文风格,但**独立实现、
互不依赖**;本模块只用 Python 标准库(html / pathlib / datetime),
产物为零外部依赖的离线 HTML(内联 CSS)。

核心入口 :func:`render_batch_report`:

- ``summary`` 鸭子形状 = A112 ``run_batch`` 返回值 + 可选扩展::

      {"submitted": int, "failed": int, "rate_limited": bool, "paused": bool,
       "results": [{"entry_id", "group_name", "ok", "submitted", "error", "portal"}],
       "note": str,
       # 可选扩展:
       "attestations": [{"group_name", "reviewer", "ts"}],   # 声明清单
       "screenshots": ["截图路径", ...],                      # 提交成功截图
       "generated_at": "2026-10-02 09:30:00"}                # 覆盖生成时间

- 五个区块:一、批次信息(批次号/生成时间/提交成功/失败/频控挂起/人工暂停,
  有备注时追加备注行);二、逐条状态表(序号/组名/条目/门户/结果中文/原因,
  结果 ∈ 已提交✔ / 失败✘ / 未执行,空列表显示"无条目");三、声明清单
  (有则表格:组名/审核人/声明时间);四、截图清单(有则逐路径核对存在性,
  存在→已存档、缺失→文件缺失);五、结论(固定含"本次批量执行每一条均经
  人工门完成验证码输入与最终确认"与"虚假举报承担法律责任"提示);
- 可选区块(三/四)在 ``attestations`` / ``screenshots`` 缺失或为空时
  **整段隐藏**;
- 全部动态插值一律 :func:`html.escape`(组名/原因等可含 ``<script>``
  之类特殊字符);``out_path`` 写盘(父目录自动创建,UTF-8)并返回
  HTML 字符串。

红线呼应(CONTRACTS-V6 §0):本报告只是**留痕与核对材料**,结论段固定
重申每条均经人工门完成验证码输入与最终确认(红线 24),并提示虚假举报的
法律责任;报告自身不触发任何提交动作。

用法::

    from netsentinel.report.batch_report import render_batch_report

    html_text = render_batch_report("BAT-20261002-01", summary,
                                    out_path="结案报告/BAT-01.html")
"""
from __future__ import annotations

import html as _html
import logging
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = ["render_batch_report"]

logger = logging.getLogger(__name__)

#: 文档抬头(也是 <title> 与 <h1>)
DOC_TITLE = "批量举报结案报告(人工核对稿)"

#: 逐条结果的中文状态(与契约 §4 A114 口径一致)
STATUS_SUBMITTED = "已提交✔"
STATUS_FAILED = "失败✘"
STATUS_SKIPPED = "未执行"

#: 门户取值 → 中文展示(与 __main__ / contracts 的门户叫法对齐;未知原样展示)
PORTAL_CN: dict[str, str] = {
    "12377": "12377(中央网信办)",
    "shdf": "shdf(扫黄打非)",
}

#: 截图存在性标注
SCREENSHOT_OK = "已存档"
SCREENSHOT_MISSING = "文件缺失"

#: 空逐条表占位文案
EMPTY_RESULTS_TEXT = "无条目"

#: 生成时间格式(本地时区,精确到秒)
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 内联 CSS:简洁公文风格(宋体系、A4 白纸、黑色细边框,打印友好)
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
table.info th{width:150px;background:#f2f2f2;font-weight:600;white-space:nowrap;}
table.grid{width:100%;border-collapse:collapse;font-size:13px;}
table.grid th,table.grid td{border:1px solid #999;padding:5px 8px;
  text-align:left;word-break:break-all;}
table.grid th{background:#f2f2f2;font-weight:600;white-space:nowrap;}
table.grid td.num{text-align:center;white-space:nowrap;}
td.ok{color:#1a6b3c;font-weight:600;}td.bad{color:#8b1e3f;font-weight:600;}
p.decl{font-size:14px;line-height:2;text-indent:2em;margin:6px 0;}
p.foot{margin-top:34px;padding-top:10px;border-top:1px dashed #bbb;
  font-size:12px;color:#777;text-align:center;}
@media print{body{background:#fff;padding:0}.sheet{border:none;box-shadow:none}}"""


# ---------------------------------------------------------------------------
# 取值 / 格式化辅助(全部容错,任何字段缺失都不抛错)
# ---------------------------------------------------------------------------

def _esc(value: Any) -> str:
    """转义任意插值为安全 HTML 文本(html.escape,属性/正文通用)。"""
    return _html.escape(str(value), quote=True)


def _text(value: Any, default: str = "—") -> str:
    """取字符串字段;None / 空白 / 缺失回退占位符"—"。"""
    if value is None:
        return default
    text = str(value)
    return text if text.strip() else default


def _count_cn(value: Any) -> str:
    """计数值格式化:整数(或整型浮点)→ ``N 条``;缺失 → ``—``。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    return f"{int(value)} 条"


def _flag_cn(value: Any) -> str:
    """布尔标志格式化:True→是 / False→否 / 缺失→—。"""
    if value is None:
        return "—"
    return "是" if value else "否"


def _portal_cn(value: Any) -> str:
    """门户取值 → 中文展示;未知门户原样展示(不抛错)。"""
    text = _text(value)
    return PORTAL_CN.get(text, text)


def _status_cn(row: Mapping[str, Any]) -> tuple[str, str]:
    """单条结果 → (中文状态, CSS class)。

    映射规则(与 A112 结果行语义对应):
    - ``submitted`` 为真 → ``已提交✔``(绿);
    - 否则 ``ok`` 为假(失败/频控挂起等)→ ``失败✘``(红),原因列说明细节;
    - 其余(执行成功但未真实提交,典型为干跑)→ ``未执行``。
    """
    if row.get("submitted"):
        return STATUS_SUBMITTED, "ok"
    if not row.get("ok"):
        return STATUS_FAILED, "bad"
    return STATUS_SKIPPED, ""


def _rows(value: Any) -> list[Mapping[str, Any]]:
    """把 summary里的列表字段容错成"映射行"列表(非列表/非映射项按空行处理)。"""
    if not isinstance(value, (list, tuple)):
        return []
    out: list[Mapping[str, Any]] = []
    for item in value:
        out.append(item if isinstance(item, Mapping) else {})
    return out


# ---------------------------------------------------------------------------
# 区块渲染
# ---------------------------------------------------------------------------

def _render_results(results: Any) -> str:
    """区块二:逐条状态表(序号/组名/条目/门户/结果/原因);空 → "无条目"。"""
    rows = _rows(results)
    if not rows:
        return (
            '<table class="grid">\n      <tr><th>序号</th><th>组名</th>'
            "<th>条目</th><th>门户</th><th>结果</th><th>原因</th></tr>\n      "
            f'<tr><td colspan="6">{_esc(EMPTY_RESULTS_TEXT)}</td></tr>\n    </table>'
        )
    body: list[str] = []
    for idx, row in enumerate(rows, start=1):
        status, cls = _status_cn(row)
        status_html = f'<td class="{cls}">{_esc(status)}</td>' if cls else f"<td>{_esc(status)}</td>"
        body.append(
            f'<tr><td class="num">{idx}</td>'
            f"<td>{_esc(_text(row.get('group_name')))}</td>"
            f"<td>{_esc(_text(row.get('entry_id')))}</td>"
            f"<td>{_esc(_portal_cn(row.get('portal')))}</td>"
            f"{status_html}"
            f"<td>{_esc(_text(row.get('error'), default=''))}</td></tr>"
        )
    return (
        '<table class="grid">\n      <tr><th>序号</th><th>组名</th>'
        "<th>条目</th><th>门户</th><th>结果</th><th>原因</th></tr>\n      "
        + "\n      ".join(body)
        + "\n    </table>"
    )


def _render_attestations(attestations: Any) -> str:
    """区块三:声明清单表(组名/审核人/声明时间);空列表返回空串(整段隐藏)。"""
    rows = _rows(attestations)
    if not rows:
        return ""
    body: list[str] = []
    for idx, row in enumerate(rows, start=1):
        body.append(
            f'<tr><td class="num">{idx}</td>'
            f"<td>{_esc(_text(row.get('group_name')))}</td>"
            f"<td>{_esc(_text(row.get('reviewer')))}</td>"
            f"<td>{_esc(_text(row.get('ts')))}</td></tr>"
        )
    return (
        '<table class="grid">\n      <tr><th>序号</th><th>组名</th>'
        "<th>审核人</th><th>声明时间</th></tr>\n      "
        + "\n      ".join(body)
        + "\n    </table>"
    )


def _render_screenshots(screenshots: Any) -> str:
    """区块四:截图清单(存在→已存档 / 缺失→文件缺失);空列表返回空串。"""
    paths = [str(p) for p in screenshots if str(p).strip()] if isinstance(screenshots, (list, tuple)) else []
    if not paths:
        return ""
    body: list[str] = []
    for idx, raw in enumerate(paths, start=1):
        exists = Path(raw).is_file()
        mark = SCREENSHOT_OK if exists else SCREENSHOT_MISSING
        cls = "ok" if exists else "bad"
        body.append(
            f'<tr><td class="num">{idx}</td><td>{_esc(raw)}</td>'
            f'<td class="{cls}">{_esc(mark)}</td></tr>'
        )
    return (
        '<table class="grid">\n      <tr><th>序号</th><th>截图路径</th>'
        "<th>状态</th></tr>\n      "
        + "\n      ".join(body)
        + "\n    </table>"
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def render_batch_report(batch_id: Any, summary: dict[str, Any], out_path: str) -> str:
    """渲染批量举报结案报告(人工核对稿)HTML,写盘并返回字符串。

    :param batch_id: 批次号(A113 ``new_batch`` 返回值或任意可 str 化标识)。
    :param summary:  A112 ``run_batch`` 返回的批次摘要(鸭子 dict),另支持
        可选扩展键 ``attestations``(声明清单)/``screenshots``(截图路径
        列表)/``generated_at``(覆盖生成时间);任何字段缺失都不抛错,
        展示为"—"或占位文案;可选区块缺失/为空时整段隐藏。
    :param out_path: 输出 HTML 文件路径(父目录自动创建,UTF-8 写出)。
    :return: 完整 HTML 字符串(与写盘内容一致)。

    所有动态插值均经 :func:`html.escape`,可安全嵌入含 ``<script>`` 之类
    特殊字符的组名 / 原因 / 路径。本函数只读 ``screenshots`` 路径的存在性,
    不读文件内容、不发网、不触发任何提交动作。
    """
    if isinstance(summary, (str, bytes)) or not isinstance(summary, Mapping):
        raise TypeError(
            f"summary 必须是批次摘要字典(dict),当前类型是 {type(summary).__name__}"
        )

    batch_text = str(batch_id)
    generated_at = _text(summary.get("generated_at")) if summary.get("generated_at") else datetime.now().strftime(_TS_FORMAT)

    # ---- 区块一:批次信息 ----
    note = str(summary.get("note") or "").strip()
    note_row = f"\n    <tr><th>备注</th><td>{_esc(note)}</td></tr>" if note else ""

    # ---- 区块二:逐条状态 ----
    results_html = _render_results(summary.get("results"))

    # ---- 区块三 / 四:可选区块(缺失或为空 → 整段隐藏) ----
    attestation_html = _render_attestations(summary.get("attestations"))
    attestation_block = (
        f'\n  <h2>三、声明清单</h2>\n  {attestation_html}' if attestation_html else ""
    )
    screenshot_html = _render_screenshots(summary.get("screenshots"))
    screenshot_block = (
        f'\n  <h2>四、截图清单</h2>\n  {screenshot_html}' if screenshot_html else ""
    )

    # ---- 区块五:结论(挂起/暂停时追加对应说明段) ----
    extra_decls = ""
    if summary.get("rate_limited"):
        extra_decls += (
            '\n  <p class="decl">本批次因额度/间隔限制挂起:频控与每日额度在'
            "批量模式下照常生效、未作任何放宽;未执行条目可在额度恢复后凭批次号续批,"
            "已提交条目不受影响。</p>"
        )
    if summary.get("paused"):
        extra_decls += (
            "\n  <p class=\"decl\">本批次在执行过程中经人工暂停,已完成条目见上表,"
            "剩余条目可凭批次号续批。</p>"
        )

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
  <p class="doc-meta">净网哨兵 NetSentinel · 批次 {_esc(batch_text)} · 离线生成 · 仅供人工核对与归档</p>

  <h2>一、批次信息</h2>
  <table class="info">
    <tr><th>批次号</th><td>{_esc(batch_text)}</td></tr>
    <tr><th>生成时间</th><td>{_esc(generated_at)}</td></tr>
    <tr><th>提交成功</th><td>{_esc(_count_cn(summary.get("submitted")))}</td></tr>
    <tr><th>失败</th><td>{_esc(_count_cn(summary.get("failed")))}</td></tr>
    <tr><th>频控挂起</th><td>{_esc(_flag_cn(summary.get("rate_limited")))}</td></tr>
    <tr><th>人工暂停</th><td>{_esc(_flag_cn(summary.get("paused")))}</td></tr>{note_row}
  </table>

  <h2>二、逐条状态</h2>
  {results_html}{attestation_block}{screenshot_block}

  <h2>五、结论</h2>
  <p class="decl">本次批量执行每一条均经人工门完成验证码输入与最终确认:批量仅为顺序编排,任何一条举报的验证码输入与最终确认都由人工在执行器人工门步骤完成,系统不存在自动确认的真实提交路径。</p>{extra_decls}
  <p class="decl">举报人应当保证举报内容真实、准确。捏造事实、进行虚假举报承担法律责任;由此致使他人合法权益受到损害的,还应依法承担相应赔偿责任。</p>

  <p class="foot">本报告为批量执行留痕与核对材料,举报以人工核对并签字后的内容为准。</p>
</div>
</body>
</html>
"""

    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(doc, encoding="utf-8")
    logger.info("批量结案报告已写出:%s(批次 %s,%d 字符)", target, batch_text, len(doc))
    return doc
