"""单文件 HTML 举报材料渲染(NetSentinel · A34)。

把人工复核后的结论渲染成**单文件 HTML 举报材料(人工核对稿)**:运营者核对、
签字、随证据包归档。仅使用标准库(html / pathlib / base64 / json),模板用
f-string 字符串拼接,产物是零外部依赖的离线 HTML(内联 CSS,公文风格)。

核心入口 :func:`render_report`:

- ``entry_like`` 鸭子类型取 ``.id / .site_url / .verdict / .created_at / .note /
  .evidence_zip``(容错 getattr,字段缺失不抛错);
- ``report_dict`` 缺省时,若 ``bundle`` 的证据目录里有 manifest.json
  (packager A11 布局 ``{"report": report.as_dict(), "files": [...]}``),
  尝试读回其 ``"report"`` 键作为报告数据;读不到按缺失处理;
- 六个区块:一、基本信息表;二、识别摘要(agg / 达标图片数 / 页面数,
  缺省"—");三、风险要点(intel 的 url/text explain 与 fusion contrib 转中文
  行,≤8 条,无 intel 显示"未启用融合分析");四、证据图片(≤60KB 的文件
  base64 内嵌,最多 12 张,其余只列文件名);五、声明段(辅助初筛 + 人工
  核实确认 + 虚假举报法律风险提示);六、人工核对签名栏(举报人 / 联系方式 /
  日期 三条下划线空位);
- 所有插值一律 :func:`html.escape`;``out_path`` 非空则写文件(父目录自动
  创建,UTF-8),并返回 HTML 字符串。

V5 升级:内嵌图片的存在性 + 大小判断合并为**单次 stat**(再按大小决定
是否读取,读取始终一次);整体渲染耗时接 telemetry(timer ``report.render``)。

用法::

    from netsentinel.report.html_report import render_report
    html_text = render_report(entry, bundle=bundle, out_path="举报材料.html")
"""
from __future__ import annotations

import base64
import html as _html
import json
import logging
import stat as _stat
from pathlib import Path
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import EvidenceBundle

logger = logging.getLogger(__name__)

__all__ = ["render_report"]

#: 文档抬头(也是 <title> 与 <h1>)
DOC_TITLE = "网络有害信息举报材料(人工核对稿)"

#: manifest 文件名(与 evidence.packager 的布局一致)
MANIFEST_NAME = "manifest.json"

#: 单张证据图片允许内嵌的最大字节数(超出只列文件名,见证据包 zip)
MAX_EMBED_BYTES = 60 * 1024

#: 最多内嵌的证据图片张数
MAX_EMBED_IMAGES = 12

#: 风险要点最多展示条数
MAX_RISK_LINES = 8

#: verdict → 中文名(与 decision.review_queue.VERDICT_CN 保持一致口径)
VERDICT_CN: dict[str, str] = {
    "clean": "无风险",
    "suspect": "疑似",
    "nsfw": "高置信色情",
}

#: fusion contrib 特征键 → 中文名
_FEATURE_CN: dict[str, str] = {
    "image": "图像模型",
    "page_vlm": "页面评估",
    "url": "链接特征",
    "text": "文本特征",
}

#: 图片后缀 → MIME 类型(缺省按 png 处理)
_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

#: 内联 CSS:简洁公文风格(宋体系、A4 白纸、黑色细边框,打印友好)
_CSS = """body{margin:0;padding:24px 12px;background:#f5f5f5;
  font-family:"SimSun","Songti SC","Noto Serif CJK SC",serif;color:#1a1a1a;}
.sheet{max-width:860px;margin:0 auto;background:#fff;padding:44px 52px;
  border:1px solid #999;box-shadow:0 2px 8px rgba(0,0,0,.08);}
h1{font-size:22px;text-align:center;letter-spacing:2px;margin:0 0 6px;}
.doc-meta{text-align:center;color:#666;font-size:13px;margin:0 0 26px;}
h2{font-size:16px;border-left:4px solid #8b1e3f;padding-left:8px;
  margin:26px 0 10px;font-weight:700;}
table.info{width:100%;border-collapse:collapse;font-size:14px;}
table.info th,table.info td{border:1px solid #999;padding:6px 10px;
  text-align:left;word-break:break-all;}
table.info th{width:150px;background:#f2f2f2;font-weight:600;white-space:nowrap;}
ul.risk{font-size:14px;line-height:1.9;padding-left:22px;margin:6px 0;}
.gallery{display:flex;flex-wrap:wrap;gap:12px;margin:8px 0;}
figure.ev{margin:0;width:200px;font-size:12px;color:#444;text-align:center;}
figure.ev img{width:100%;height:140px;object-fit:cover;border:1px solid #bbb;
  display:block;margin-bottom:4px;}
p.more,p.muted{font-size:13px;color:#555;line-height:1.8;}
p.decl{font-size:14px;line-height:2;text-indent:2em;margin:6px 0;}
.sign p{font-size:15px;margin:26px 0;}
.sign-blank{display:inline-block;width:240px;height:20px;
  border-bottom:1px solid #1a1a1a;vertical-align:bottom;}
p.foot{margin-top:34px;padding-top:10px;border-top:1px dashed #bbb;
  font-size:12px;color:#777;text-align:center;}
@media print{body{background:#fff;padding:0}.sheet{border:none;box-shadow:none}}"""


# ---------------------------------------------------------------------------
# 基础取值 / 格式化
# ---------------------------------------------------------------------------

def _esc(value: Any) -> str:
    """转义任意插值为安全 HTML 文本(html.escape,属性/正文通用)。"""
    return _html.escape(str(value), quote=True)


def _field(src: Any, name: str, default: Any = "") -> Any:
    """从鸭子类型对象(或 dict)里容错取字段;取到 None 回退默认值。"""
    if isinstance(src, dict):
        val = src.get(name, default)
    else:
        val = getattr(src, name, default)
    return default if val is None else val


def _fmt_prob(value: Any) -> str:
    """概率 / 分值格式化:数值 → ``%.4f``;缺失 → ``—``。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    return f"{float(value):.4f}"


def _fmt_count(value: Any) -> str:
    """计数值格式化:整数 → 十进制字符串;缺失 → ``—``。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    return str(int(value))


# ---------------------------------------------------------------------------
# manifest 读取(bundle → report_dict)
# ---------------------------------------------------------------------------

def _load_report_from_bundle(bundle: EvidenceBundle | None) -> dict | None:
    """从证据包定位 manifest.json 并读回 ``"report"`` 键;失败返回 None。

    兼容 packager(A11)与 webui(A30)的两种定位方式:优先 ``manifest_path``,
    其次 ``dir_path/manifest.json``。JSON 损坏 / 文件缺失只记 warning,
    绝不抛异常;若根节点本身就像报告(含 site_url),也容忍直接返回。
    """
    if bundle is None:
        return None
    candidates: list[Path] = []
    manifest_path = str(getattr(bundle, "manifest_path", "") or "")
    if manifest_path:
        candidates.append(Path(manifest_path))
    dir_path = str(getattr(bundle, "dir_path", "") or "")
    if dir_path:
        candidates.append(Path(dir_path) / MANIFEST_NAME)
    for path in candidates:
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("读取证据包 manifest 失败,按缺失处理:%s(%s)", path, exc)
            continue
        if isinstance(data, dict):
            report = data.get("report")
            if isinstance(report, dict):
                logger.info("已从 manifest 读回报告:%s", path)
                return report
            if "site_url" in data:  # 根节点本身即报告(容错)
                return data
    return None


# ---------------------------------------------------------------------------
# 区块三:风险要点(intel → 中文行)
# ---------------------------------------------------------------------------

def _risk_lines(intel: Any) -> list[str]:
    """把 intel 的 url/text explain 与 fusion prob/contrib 转成中文要点行。

    结构约定(与 A29 fusion 写入 ``SiteReport.intel`` 一致,全部容忍缺失):
    - ``url`` / ``text``: ``{"risk": float, "explain": [中文, ...]}``;
    - ``fusion``: ``{"prob": float, "contrib": {特征: logit 贡献}, "rule": str}``。

    返回去重保序、最多 :data:`MAX_RISK_LINES` 条的中文行列表。
    """
    if not isinstance(intel, dict) or not intel:
        return []
    lines: list[str] = []
    for key, tag in (("url", "【链接特征】"), ("text", "【文本特征】")):
        feat = intel.get(key)
        if not isinstance(feat, dict):
            continue
        for item in feat.get("explain") or []:
            text = str(item).strip()
            if text:
                lines.append(f"{tag}{text}")
    fusion = intel.get("fusion")
    if isinstance(fusion, dict):
        prob = fusion.get("prob")
        if isinstance(prob, (int, float)) and not isinstance(prob, bool):
            rule = str(fusion.get("rule") or "").strip()
            suffix = f"(规则:{rule})" if rule else ""
            lines.append(f"【融合判定】综合风险概率 {float(prob):.2f}{suffix}")
        contrib = fusion.get("contrib")
        if isinstance(contrib, dict) and contrib:
            parts: list[str] = []
            for key, val in list(contrib.items())[:4]:
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    name = _FEATURE_CN.get(str(key), str(key))
                    parts.append(f"{name} {float(val):+.2f}")
            if parts:
                lines.append("【融合贡献】" + "、".join(parts))
    seen: set[str] = set()
    unique: list[str] = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            unique.append(line)
    return unique[:MAX_RISK_LINES]


# ---------------------------------------------------------------------------
# 区块四:证据图片(收集 + base64 内嵌)
# ---------------------------------------------------------------------------

def _collect_image_paths(report_dict: dict | None, base: Path | None) -> list[Path]:
    """从 report_dict 的 ``pages[].screenshot_path / images`` 收集图片路径。

    相对路径以 ``base``(bundle 证据目录)解析;去重保序;不做存在性校验
    (存在性 / 大小过滤在渲染阶段处理,不合格的只列文件名)。
    """
    if not isinstance(report_dict, dict):
        return []
    pages = report_dict.get("pages")
    if not isinstance(pages, list):
        return []
    out: list[Path] = []
    seen: set[str] = set()
    for page in pages:
        if not isinstance(page, dict):
            continue
        candidates: list[str] = []
        screenshot = page.get("screenshot_path")
        if isinstance(screenshot, str) and screenshot.strip():
            candidates.append(screenshot.strip())
        for raw in page.get("images") or []:
            if isinstance(raw, str) and raw.strip():
                candidates.append(raw.strip())
        for raw in candidates:
            path = Path(raw)
            if not path.is_absolute() and base is not None:
                path = base / path
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            out.append(path)
    return out


def _mime_for(path: Path) -> str:
    """按扩展名给 MIME 类型;未知后缀按 png 处理。"""
    return _MIME.get(path.suffix.lower(), "image/png")


def _render_images(report_dict: dict | None, base: Path | None) -> str:
    """渲染证据图片区块:≤60KB 的真实文件 base64 内嵌(最多 12 张)。

    不存在的文件、超过 60KB 的大图、以及超出 12 张名额的部分一律不内嵌,
    只在"其余见证据包 zip:"后列出文件名(运营者去 zip 里核对原件)。

    V5:每张候选图**单次 stat** 同时完成存在性(常规文件判定)与大小检查,
    通过后才读取一次——旧实现 is_file()+stat() 需要两次系统调用。
    """
    paths = _collect_image_paths(report_dict, base)
    if not paths:
        return '<p class="muted">(未提供可内嵌的证据图片,全部证据请查看证据包 zip。)</p>'
    figures: list[str] = []
    overflow: list[str] = []
    for path in paths:
        if len(figures) >= MAX_EMBED_IMAGES:
            overflow.append(path.name)
            continue
        try:
            st = path.stat()  # 单次 stat:存在性 + 常规文件 + 大小一次拿全
            if not _stat.S_ISREG(st.st_mode) or st.st_size > MAX_EMBED_BYTES:
                overflow.append(path.name)
                continue
            data = path.read_bytes()
        except OSError as exc:  # 不可读文件与大图同待遇:只列名
            logger.warning("证据图片读取失败,只列文件名:%s(%s)", path, exc)
            overflow.append(path.name)
            continue
        size = st.st_size
        b64 = base64.b64encode(data).decode("ascii")
        figures.append(
            f'<figure class="ev">'
            f'<img src="data:{_mime_for(path)};base64,{b64}" alt="{_esc(path.name)}">'
            f"<figcaption>{_esc(path.name)}({size} 字节)</figcaption></figure>"
        )
    parts: list[str] = []
    if figures:
        parts.append('<div class="gallery">' + "".join(figures) + "</div>")
    if overflow:
        parts.append(
            '<p class="more">其余见证据包 zip:' + _esc("、".join(overflow)) + "</p>"
        )
    return "\n      ".join(parts)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

@telemetry.timed("report.render")
def render_report(
    entry_like: Any,
    bundle: EvidenceBundle | None = None,
    report_dict: dict | None = None,
    out_path: str = "",
) -> str:
    """渲染单文件 HTML 举报材料(人工核对稿),返回 HTML 字符串。

    参数:
        entry_like:  复核条目(Entry 或鸭子类型),取
            ``.id / .site_url / .verdict / .created_at / .note / .evidence_zip``;
        bundle:      证据包(:class:`EvidenceBundle` 或鸭子类型);``report_dict``
            缺省时尝试读其 manifest.json 的 ``"report"`` 键;
        report_dict: 报告 dict(:meth:`SiteReport.as_dict` 形状);优先级高于
            manifest;无 intel 时风险要点显示"未启用融合分析";
        out_path:    非空则写出 HTML 文件(父目录自动创建,UTF-8)。

    所有动态插值均经 :func:`html.escape`,可安全嵌入含 ``<script>`` 之类
    特殊字符的站点 URL。任何字段缺失都不抛错,展示为"—"或占位文案。

    可观测性(V5):整体渲染耗时(含可选落盘)记 telemetry ``report.render``。
    """
    # ---- entry_like 鸭子取值(全部容错) ----
    entry_id_text = str(_field(entry_like, "id", "") or "")
    site_url = str(_field(entry_like, "site_url", "") or "")
    verdict_raw = _field(entry_like, "verdict", "")
    verdict_value = str(getattr(verdict_raw, "value", verdict_raw) or "")
    verdict_display = (
        f"{VERDICT_CN.get(verdict_value, '未知')}({verdict_value})"
        if verdict_value
        else "—"
    )
    created_at = str(_field(entry_like, "created_at", "") or "")
    note = str(_field(entry_like, "note", "") or "")
    evidence_zip = str(_field(entry_like, "evidence_zip", "") or "")
    if not evidence_zip and bundle is not None:
        evidence_zip = str(getattr(bundle, "zip_path", "") or "")

    # ---- report_dict 缺省 → 读 bundle 的 manifest.json ----
    if report_dict is None:
        report_dict = _load_report_from_bundle(bundle)
    report = report_dict if isinstance(report_dict, dict) else {}

    # ---- 证据图片的相对路径基准(bundle 证据目录) ----
    dir_path = str(getattr(bundle, "dir_path", "") or "") if bundle is not None else ""
    base = Path(dir_path) if dir_path else None

    # ---- 区块一:基本信息 ----
    note_row = (
        f"\n    <tr><th>复核备注</th><td>{_esc(note)}</td></tr>" if note else ""
    )

    # ---- 区块二:识别摘要(缺省"—") ----
    agg_text = _fmt_prob(report.get("agg_nsw_prob"))
    nsw_text = _fmt_count(report.get("nsw_image_count"))
    pages = report.get("pages")
    pages_text = str(len(pages)) if isinstance(pages, list) else "—"

    # ---- 区块三:风险要点 ----
    intel = report.get("intel")
    risk_items = _risk_lines(intel)
    if risk_items:
        risk_html = (
            '<ul class="risk">'
            + "".join(f'<li class="risk">{_esc(line)}</li>' for line in risk_items)
            + "</ul>"
        )
    elif isinstance(intel, dict) and intel:
        risk_html = '<p class="muted">(融合分析未输出显著风险要点。)</p>'
    else:
        risk_html = '<p class="muted">未启用融合分析</p>'

    # ---- 区块四:证据图片 ----
    images_html = _render_images(report, base)

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

  <h2>一、基本信息</h2>
  <table class="info">
    <tr><th>编号</th><td>{_esc(entry_id_text or "—")}</td></tr>
    <tr><th>站点</th><td>{_esc(site_url or "—")}</td></tr>
    <tr><th>判定</th><td>{_esc(verdict_display)}</td></tr>
    <tr><th>生成时间</th><td>{_esc(created_at or "—")}</td></tr>
    <tr><th>证据包路径</th><td>{_esc(evidence_zip or "—")}</td></tr>{note_row}
  </table>

  <h2>二、识别摘要</h2>
  <table class="info">
    <tr><th>聚合色情分值(agg)</th><td>{_esc(agg_text)}</td></tr>
    <tr><th>达标图片数</th><td>{_esc(nsw_text)}</td></tr>
    <tr><th>抽样页面数</th><td>{_esc(pages_text)}</td></tr>
  </table>

  <h2>三、风险要点</h2>
  {risk_html}

  <h2>四、证据图片</h2>
  {images_html}

  <h2>五、声明</h2>
  <p class="decl">本材料由"净网哨兵(NetSentinel)"辅助系统自动生成,内容为系统对目标站点的机器辅助初筛结果,仅供举报时参考。</p>
  <p class="decl">运营者已按流程完成人工复核,并对上述站点内容及证据逐项人工核实,确认举报事项真实、证据与描述一致后,方可在下方签名提交。</p>
  <p class="decl">举报人应当保证举报内容真实、准确。捏造事实、进行虚假举报的,将依法承担相应法律责任;由此致使他人合法权益受到损害的,还应依法承担赔偿责任。</p>

  <h2>六、人工核对签名栏</h2>
  <div class="sign">
    <p>举报人(签名):<span class="sign-blank"></span></p>
    <p>联系方式:<span class="sign-blank"></span></p>
    <p>日期:<span class="sign-blank"></span></p>
  </div>

  <p class="foot">本材料为机器初筛辅助产物,举报以人工核对并签字后的内容为准。</p>
</div>
</body>
</html>
"""

    if out_path:
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(doc, encoding="utf-8")
        logger.info("举报材料(人工核对稿)已写出:%s(%d 字符)", target, len(doc))
    return doc
