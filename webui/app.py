"""净网哨兵 · Streamlit 人工复核台(A30)。

定位:机器初筛 → **人工复核** → 辅助举报。本页面只做"看证据、看解释、
人工拍板"这一段;举报提交仍走既有流程(计划预览 → 人工门 → 提交)。

结构约定(与 tests/test_webui_smoke.py 对应):
- 纯逻辑层(本文件上半部分,无 streamlit 依赖,可离线单测):
  * :func:`verdict_cn`          判定中文映射;
  * :func:`build_entry_card`    复核卡片展示模型组装(含 intel 解释提炼);
  * :func:`filter_entries`      状态过滤 + 站点 URL 子串搜索;
  * :func:`safe_image_paths`    从 report_dict 收集存在且 ≤8MB 的本地图;
  * :func:`apply_data_dir`      NETSENTINEL_DATA_DIR 环境变量覆盖数据路径;
  * :func:`load_report_dict`    从证据包旁的 manifest.json 读回报告 dict
    (V5:边车缺失时进一步读 zip 包内的 manifest.json;坏 zip 不抛,返回
    None——证据包是外部产物,复核台绝不因单个坏包崩溃)。

V5 升级(A101,本机未装 streamlit,只动纯逻辑层):
- 健壮性:``load_report_dict`` 增加 zip 包内 manifest 读取与全套容错
  (BadZipFile / 加密包 / IO / 非 UTF-8 / 坏 JSON 一律返回 None);
- 可观测性:``build_entry_card`` 每次成功组装计数 ``telemetry.inc
  ("webui.card_built")``(纯逻辑层打点,UI 层不加);
- 质量:六个纯逻辑公共函数补 docstring 用法示例,zipfile 函数内惰性导入。
- UI 层(下半部分,streamlit 惰性导入,缺依赖时 main() 退出码 1):
  * :func:`render` 侧栏统计/筛选/搜索 + 三页签条目卡片 + 批准/驳回 +
    举报计划预览(playbook markdown,**不执行提交**)。

安全红线(必须体现在代码里):
- 机器结论只是初筛:批准前必须勾选"我已人工核实证据真实有效"双重确认;
- 举报须真实,虚假举报违法:页面显著位置固定提示;
- 本页面绝不自动提交举报、绝不去往真实门户站点:计划只是本地生成的文本
  预览,真正的提交由 orchestrator/CLI 在人工门约束下完成。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from netsentinel import telemetry

# ---------------------------------------------------------------------------
# streamlit 惰性导入:缺失时纯逻辑层仍可被测试导入(UI 在 main() 里拦截)
# ---------------------------------------------------------------------------
try:  # pragma: no cover - 取决于运行环境
    import streamlit as st

    _HAS_ST = True
except ImportError:  # pragma: no cover - 取决于运行环境
    st = None  # type: ignore[assignment]
    _HAS_ST = False

__all__ = [
    "verdict_cn",
    "build_entry_card",
    "filter_entries",
    "safe_image_paths",
    "apply_data_dir",
    "load_report_dict",
    "render",
    "main",
]

# ===========================================================================
# 纯逻辑层(无 streamlit 依赖)
# ===========================================================================

#: 单张证据图片的展示上限(与 contracts.Config.max_image_mb 默认值一致)。
MAX_IMAGE_BYTES: int = 8 * 1024 * 1024

#: 卡片解释要点条数上限。
MAX_EXPLAIN_LINES: int = 6

#: 数据目录覆盖环境变量(默认 ./data)。
DATA_DIR_ENV: str = "NETSENTINEL_DATA_DIR"

#: 判定值 → 中文(A30 规定映射;clean=未发现 / suspect=疑似 / nsfw=高置信)。
VERDICT_CN_MAP: dict[str, str] = {
    "clean": "未发现",
    "suspect": "疑似",
    "nsfw": "高置信",
}

#: 状态 → 中文(UI 展示用,与 review_queue.STATUS_CN 一致)。
STATUS_CN: dict[str, str] = {
    "pending": "待复核",
    "approved": "已批准",
    "rejected": "已驳回",
    "submitted": "已提交",
}

#: 判定 → 徽章颜色(红/黄/绿,用于 expander 标题)。
VERDICT_BADGE: dict[str, str] = {
    "nsfw": "🔴",  # 红
    "suspect": "🟡",  # 黄
    "clean": "🟢",  # 绿
}


def verdict_cn(v: Any) -> str:
    """判定值(字符串或 Verdict 枚举)→ 中文名;未知值返回"未知"。

    示例::

        >>> verdict_cn("nsfw")
        '高置信'
        >>> verdict_cn(Verdict.CLEAN)
        '未发现'
        >>> verdict_cn("weird")
        '未知'
    """
    value = getattr(v, "value", v)  # 容忍 Verdict 枚举
    text = str(value or "").strip().lower()
    return VERDICT_CN_MAP.get(text, "未知")


def _field(src: Any, name: str, default: Any = "") -> Any:
    """从鸭子类型对象或 dict 里取字段;取到 None 时回退默认值。"""
    if isinstance(src, dict):
        val = src.get(name, default)
    else:
        val = getattr(src, name, default)
    return default if val is None else val


def _domain(url: str) -> str:
    """从站点 URL 提取域名用于卡片标题;失败时原样返回。"""
    try:
        netloc = urlparse(url or "").netloc
    except ValueError:  # pragma: no cover - urlparse 对怪串几乎不抛
        netloc = ""
    return netloc or (url or "未知站点")


def _extract_explain_lines(intel: dict | None) -> list[str]:
    """从 intel(URL/文本/页面级 VLM/融合)提炼中文解释要点,去重保序,≤6 条。

    intel 结构(与 A29 fusion 写入 SiteReport.intel 的约定一致,缺失容忍):
    - ``url`` / ``text``: ``{"risk": float, "explain": [中文, ...]}``;
    - ``page_vlm``: ``{"page_nsfw_prob": float|None, "elements": [...], "error": str}``;
    - ``fusion``: ``{"prob": float, "contrib": {特征: logit 贡献}, "rule": str}``。
    """
    if not isinstance(intel, dict):
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

    page = intel.get("page_vlm")
    if isinstance(page, dict):
        prob = page.get("page_nsfw_prob")
        if isinstance(prob, bool):  # bool 是 int 子类,显式排除
            prob = None
        if isinstance(prob, (int, float)):
            lines.append(f"【页面评估】整页风险概率 {float(prob):.2f}")
        elif page.get("error"):
            lines.append(f"【页面评估】{str(page['error']).strip()}")
        for element in (page.get("elements") or [])[:2]:
            if not isinstance(element, dict) or not element.get("desc"):
                continue
            eprob = element.get("prob")
            suffix = (
                f"(概率 {float(eprob):.2f})"
                if isinstance(eprob, (int, float)) and not isinstance(eprob, bool)
                else ""
            )
            lines.append(f"【页面元素】{str(element['desc']).strip()}{suffix}")

    fusion = intel.get("fusion")
    if isinstance(fusion, dict):
        prob = fusion.get("prob")
        if isinstance(prob, (int, float)) and not isinstance(prob, bool):
            rule = str(fusion.get("rule") or "").strip()
            suffix = f"({rule})" if rule else ""
            lines.append(f"【融合判定】综合概率 {float(prob):.2f}{suffix}")
        contrib = fusion.get("contrib")
        if isinstance(contrib, dict) and contrib:
            parts = [
                f"{k} {float(v):+.2f}"
                for k, v in list(contrib.items())[:4]
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            ]
            if parts:
                lines.append("【融合贡献】" + "、".join(parts))

    seen: set[str] = set()
    unique: list[str] = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            unique.append(line)
    return unique[:MAX_EXPLAIN_LINES]


def build_entry_card(entry_like: Any, report_dict: dict | None) -> dict:
    """把复核条目(Entry/鸭子类型/dict)+ 报告 dict 组装成卡片展示模型。

    返回键:``id / site_url / verdict / verdict_cn / status / agg /
    created_at / evidence_zip / intel / explain_lines``;report_dict 为空时
    intel 与 explain_lines 退化为空,agg 归 0,不抛错。

    每次成功组装计入 ``telemetry.inc("webui.card_built")``(V5 可观测性;
    纯逻辑层打点,UI 层不加)。

    示例::

        >>> card = build_entry_card(
        ...     {"id": 1, "site_url": "https://x.com", "verdict": "nsfw",
        ...      "status": "pending"},
        ...     {"agg_nsw_prob": 0.9, "intel": {}},
        ... )
        >>> card["verdict_cn"], card["agg"]
        ('高置信', 0.9)
    """
    telemetry.inc("webui.card_built")
    report = report_dict if isinstance(report_dict, dict) else {}
    intel = report.get("intel")
    if not isinstance(intel, dict):
        intel = {}
    agg = report.get("agg_nsw_prob")
    if not isinstance(agg, (int, float)) or isinstance(agg, bool):
        agg = 0.0
    return {
        "id": _field(entry_like, "id", 0),
        "site_url": str(_field(entry_like, "site_url", "")),
        "verdict": str(getattr(_field(entry_like, "verdict", ""), "value", None)
                       or _field(entry_like, "verdict", "")),
        "verdict_cn": verdict_cn(_field(entry_like, "verdict", "")),
        "status": str(_field(entry_like, "status", "")),
        "agg": float(agg),
        "created_at": str(_field(entry_like, "created_at", "")),
        "evidence_zip": str(_field(entry_like, "evidence_zip", "")),
        "intel": intel,
        "explain_lines": _extract_explain_lines(intel),
    }


def filter_entries(
    entries: list[Any] | None, status: str | None = None, q: str = ""
) -> list[Any]:
    """状态过滤 + 站点 URL 子串搜索(大小写不敏感),保持原顺序。

    ``status`` 为空/None 表示不过滤状态;``q`` 为空表示不过滤 URL。
    条目可以是 Entry 对象或 dict(鸭子读取)。

    示例::

        >>> filter_entries(
        ...     [{"site_url": "https://a.example.com", "status": "pending"},
        ...      {"site_url": "https://b.other.io", "status": "approved"}],
        ...     status="pending",
        ... ) == [{"site_url": "https://a.example.com", "status": "pending"}]
        True
    """
    keyword = (q or "").strip().lower()
    result: list[Any] = []
    for entry in entries or []:
        if status and str(_field(entry, "status", "")) != status:
            continue
        if keyword and keyword not in str(_field(entry, "site_url", "")).lower():
            continue
        result.append(entry)
    return result


def safe_image_paths(report_dict: dict | None, base: str | Path | None = None) -> list[str]:
    """收集可安全展示的证据图片本地路径。

    来源:report_dict 的 ``pages[].images`` 与 ``pages[].screenshot_path``
    (整页截图也是关键证据)。规则:相对路径以 ``base`` 解析;只保留真实存在
    的文件,且大小 ≤ :data:`MAX_IMAGE_BYTES`(8MB);去重保序。

    示例(tmp_path 下已有 a.png、无 gone.png 时)::

        >>> safe_image_paths(
        ...     {"pages": [{"images": [str(tmp_path / "a.png"),
        ...                            str(tmp_path / "gone.png")]}]},
        ...     str(tmp_path),
        ... )  # doctest: +SKIP
        ['/.../a.png']       # 不存在的 gone.png 被过滤,绝不抛错
    """
    base_path = Path(base) if base else None
    out: list[str] = []
    seen: set[str] = set()
    pages = (report_dict or {}).get("pages") if isinstance(report_dict, dict) else None
    for page in pages or []:
        if not isinstance(page, dict):
            continue
        candidates: list[str] = []
        screenshot = page.get("screenshot_path")
        if isinstance(screenshot, str) and screenshot.strip():
            candidates.append(screenshot)
        for raw in page.get("images") or []:
            if isinstance(raw, str) and raw.strip():
                candidates.append(raw)
        for raw in candidates:
            path = Path(raw.strip())
            if not path.is_absolute() and base_path is not None:
                path = base_path / path
            try:
                if not path.is_file():
                    continue
                if path.stat().st_size > MAX_IMAGE_BYTES:
                    continue
            except OSError:
                continue
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
    return out


def apply_data_dir(cfg: Any, env: dict[str, str] | None = None) -> Any:
    """按 ``NETSENTINEL_DATA_DIR`` 环境变量覆盖数据路径(就地更新并返回 cfg)。

    未设置环境变量时原样返回;设置后按默认目录布局整体迁移:
    ``<root>/{evidence, review_queue.db, audit.jsonl, logs/netsentinel.log,
    vlm_cache.db}``。UI 与测试共用,便于把运行数据指到任意目录。

    示例::

        >>> cfg = apply_data_dir(Config(), env={"NETSENTINEL_DATA_DIR": "/tmp/x"})
        >>> cfg.db_path.endswith("review_queue.db")
        True
    """
    environ = os.environ if env is None else env
    root = str(environ.get(DATA_DIR_ENV, "") or "").strip()
    if not root:
        return cfg
    cfg.data_dir = root
    cfg.evidence_dir = str(Path(root) / "evidence")
    cfg.db_path = str(Path(root) / "review_queue.db")
    cfg.audit_path = str(Path(root) / "audit.jsonl")
    cfg.log_path = str(Path(root) / "logs" / "netsentinel.log")
    cfg.vlm_cache_db = str(Path(root) / "vlm_cache.db")
    return cfg


def _report_from_manifest_data(data: Any) -> dict | None:
    """manifest 顶层 JSON → 报告 dict;形态不符返回 None。

    容忍两种形态:``{"report": {...}, "files": [...]}``(packager 标准布局)
    与根节点本身即报告(含 ``site_url``);其余(非 dict、无报告键)None。
    """
    if not isinstance(data, dict):
        return None
    report = data.get("report")
    if isinstance(report, dict):
        return report
    if "site_url" in data:  # 容忍:manifest 本身就是报告
        return data
    return None


def _report_inside_zip(zip_path: Path) -> dict | None:
    """从 zip 证据包**内部**读 manifest.json(内存中读,不解压落盘)。

    V5 健壮性:证据包是外部产物,任何读包失败都不得让复核台抛错——
    损坏的 zip(:class:`zipfile.BadZipFile`)、加密包(RuntimeError)、
    IO 错误、非 UTF-8 manifest、损坏 JSON 一律返回 ``None``。

    定位规则:优先包根的 ``manifest.json``;缺失时容忍恰好一层 bundle
    子目录里的 ``manifest.json``(多个或零个命中都放弃,避免猜错)。
    """
    import zipfile  # noqa: PLC0415 - 顶层导入瘦身:仅 zip 读取路径需要

    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            member = "manifest.json"
            if member not in names:
                nested = [
                    name
                    for name in names
                    if name.endswith("/manifest.json")
                    and "/" not in name[: -len("/manifest.json")]
                ]
                if len(nested) != 1:
                    return None
                member = nested[0]
            raw = zf.read(member)
    except (
        OSError,            # 文件不可读 / 磁盘错误
        ValueError,         # 解码类错误(UnicodeDecodeError 子类)
        KeyError,           # 成员名异常
        RuntimeError,       # 加密 zip 等不支持的形态
        NotImplementedError,
        zipfile.BadZipFile, # 损坏的 zip(非 ValueError 子类,需单列)
    ):
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return _report_from_manifest_data(data)


def load_report_dict(evidence_zip: str) -> dict | None:
    """从证据包定位 manifest.json 并读回报告 dict;找不到/损坏返回 None。

    packager(A11)的布局是 ``<evidence_dir>/<host>_<ts>/manifest.json`` 与
    同名 zip 并列,因此支持三种入参:zip 路径、bundle 目录、manifest 路径。
    manifest 结构 ``{"report": report.as_dict(), "files": [...]}``;若根节点
    本身就像报告(含 site_url),也容忍返回。

    V5 健壮性:zip 路径的边车 manifest 缺失 / 损坏时,进一步读 zip 包内
    的 manifest.json(边车优先);坏 zip / 加密包 / 坏 JSON 一律返回
    ``None``,绝不抛出。

    示例::

        >>> load_report_dict("data/evidence/site_20260101.zip")  # doctest: +SKIP
        {"site_url": "https://...", "agg_nsw_prob": ..., ...}
        >>> load_report_dict("") is None
        True
    """
    raw = (evidence_zip or "").strip()
    if not raw:
        return None
    path = Path(raw)
    candidates: list[Path] = []
    if path.suffix.lower() == ".zip":
        candidates.append(path.with_suffix("") / "manifest.json")
    candidates.append(path / "manifest.json")  # 直接传 bundle 目录
    candidates.append(path)  # 直接传 manifest.json 路径
    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        report = _report_from_manifest_data(data)
        if report is not None:
            return report
    if path.suffix.lower() == ".zip" and path.is_file():
        return _report_inside_zip(path)  # V5:边车缺失/损坏时读包内 manifest
    return None


# ===========================================================================
# UI 层(以下代码仅在 streamlit 运行时执行;兄弟模块一律函数内导入)
# ===========================================================================

_PAGE_TITLE = "净网哨兵 · 人工复核台"
_MOTTO = "机器初筛,人工拍板;举报须真实,虚假举报违法。"
_CONFIRM_LABEL = "我已人工核实证据真实有效"
_MAX_GRID_IMAGES = 9  # 每卡片最多展示 3x3 张缩略图,避免页面过重


def _render_actions(entry: Any, queue: Any, cfg: Any) -> None:
    """卡片底部的人工拍板区:批准(双重确认)/ 驳回 / 计划预览。"""
    entry_id = int(_field(entry, "id", 0))
    status = str(_field(entry, "status", ""))
    if status == "pending":
        st.markdown("**人工拍板**")
        confirmed = st.checkbox(_CONFIRM_LABEL, key=f"chk_{entry_id}")
        reason = st.text_input(
            "驳回理由(驳回时必填)", value="", key=f"rej_{entry_id}"
        )
        col_yes, col_no = st.columns(2)
        approve_clicked = col_yes.button(
            "批准进入举报队列",
            key=f"ok_{entry_id}",
            type="primary",
            disabled=not confirmed,
        )
        reject_clicked = col_no.button("驳回", key=f"no_{entry_id}")
        if approve_clicked:
            if not confirmed:  # 双重确认兜底(button disabled 之外再校验一次)
                st.warning("请先勾选“我已人工核实证据真实有效”。")
            else:
                try:
                    queue.approve(entry_id, note="Web 复核台人工确认")
                    st.success("已批准进入举报队列;举报提交仍须通过人工门,本页面不自动提交。")
                    st.rerun()
                except ValueError as exc:
                    st.warning(f"{exc}")
        if reject_clicked:
            note = reason.strip()
            if not note:
                st.warning("驳回请填写理由,便于后续审计追溯。")
            else:
                try:
                    queue.reject(entry_id, note=note)
                    st.success("已驳回。")
                    st.rerun()
                except ValueError as exc:
                    st.warning(f"{exc}")
    elif status == "approved":
        st.success("该条目已人工批准,可生成举报计划预览(本页面不执行提交)。")
        _render_plan_preview(entry, queue, cfg)
    elif status == "submitted":
        st.info("该条目已完成举报提交。")
    else:  # rejected 或其他
        st.info(f"该条目已被驳回。备注:{_field(entry, 'note', '') or '-'}")


def _render_plan_preview(entry: Any, queue: Any, cfg: Any) -> None:
    """举报计划预览:门户选择 → plan_12377/plan_shdf → playbook markdown。

    只在本地生成文本展示,不打开浏览器、不访问任何真实门户站点。
    """
    entry_id = int(_field(entry, "id", 0))
    st.markdown("**生成举报计划预览**")
    portal = st.selectbox(
        "举报门户",
        ["12377", "shdf"],
        key=f"portal_{entry_id}",
        format_func=lambda v: (
            "12377(中央网信办违法和不良信息举报中心)"
            if v == "12377"
            else "扫黄打非(全国“扫黄打非”工作小组办公室)"
        ),
    )
    if not st.button("生成计划预览", key=f"plan_{entry_id}"):
        st.caption("点击后本地生成计划文本(含 HUMAN_GATE 人工门),不打开浏览器、不提交。")
        return
    # 重新 get 组装 entry_like,确保用最新状态/字段生成 payload
    entry_like = queue.get(entry_id) or entry
    try:
        if portal == "12377":
            from netsentinel.submit.portal_12377 import plan_12377 as _plan

            plan = _plan(entry_like, cfg)
        else:
            from netsentinel.submit.portal_shdf import plan_shdf as _plan

            plan = _plan(entry_like, cfg)
        from netsentinel.submit.playbook_gen import plan_to_markdown

        st.code(plan_to_markdown(plan), language="markdown")
        st.caption("⚠️ 计划含 HUMAN_GATE 人工门:验证码输入与最终提交必须人工完成。")
    except Exception as exc:  # 兄弟模块缺失/计划校验失败等,均不让页面崩溃
        st.error(f"生成举报计划失败:{exc}")


def _render_card(entry: Any, queue: Any, cfg: Any) -> None:
    """单个复核条目卡片:概要指标 + 证据图片网格 + 解释要点 + intel JSON + 拍板区。"""
    report_dict = load_report_dict(str(_field(entry, "evidence_zip", "")))
    card = build_entry_card(entry, report_dict)
    badge = VERDICT_BADGE.get(card["verdict"], "⚪")
    title = f"{badge} #{card['id']} · {_domain(card['site_url'])} · {card['verdict_cn']}"
    with st.expander(title, expanded=False):
        col1, col2, col3 = st.columns(3)
        col1.metric("综合分值 agg", f"{card['agg']:.2f}")
        col2.metric("机器判定", card["verdict_cn"])
        col3.metric("状态", STATUS_CN.get(card["status"], card["status"] or "-"))
        st.markdown(
            f"- 站点:`{card['site_url']}`\n"
            f"- 入列时间:{card['created_at'] or '-'}\n"
            f"- 证据包:`{card['evidence_zip'] or '(尚未打包)'}`"
        )
        images = safe_image_paths(report_dict, cfg.data_dir)
        if images:
            shown = images[:_MAX_GRID_IMAGES]
            st.markdown(
                f"**证据图片**(命中 {len(images)} 张,展示前 {len(shown)} 张,"
                "已过滤不存在或超过 8MB 的文件):"
            )
            for start in range(0, len(shown), 3):
                row = st.columns(3)
                for offset, column in enumerate(row):
                    idx = start + offset
                    if idx < len(shown):
                        column.image(
                            shown[idx],
                            caption=Path(shown[idx]).name,
                            use_container_width=True,
                        )
        else:
            st.info("未找到可展示的证据图片(证据包尚未生成、路径失效或图片超限)。")
        if card["explain_lines"]:
            st.markdown("**模型/规则解释要点**")
            st.markdown("\n".join(f"- {line}" for line in card["explain_lines"]))
        else:
            st.caption("暂无 intel 解释(旧数据或未启用 fusion 时属正常)。")
        if card["intel"]:
            with st.expander("intel 特征明细(JSON,折叠)"):
                st.json(card["intel"])
        _render_actions(entry, queue, cfg)


def render() -> None:
    """复核台主界面(streamlit 脚本入口调用的渲染函数)。"""
    if not _HAS_ST:  # pragma: no cover - main() 已拦截,防御性兜底
        raise RuntimeError("streamlit 不可用,无法渲染复核台;请先安装 python -m pip install -e \".[ui]\"")

    # 兄弟模块函数内导入(契约:兄弟模块一律惰性导入)
    from netsentinel.config import load_config
    from netsentinel.decision.review_queue import ReviewQueue

    st.set_page_config(page_title=_PAGE_TITLE, page_icon="🛡️", layout="wide")
    st.title(f"🛡️ {_PAGE_TITLE}")
    st.caption(_MOTTO)

    cfg = apply_data_dir(load_config())
    queue = ReviewQueue(cfg.db_path)
    entries = queue.list(None)

    # ---------------- 侧栏:统计 + 筛选 + 数据目录说明 ----------------
    with st.sidebar:
        st.header("队列概览")
        stats = queue.summary()
        m1, m2 = st.columns(2)
        m1.metric("待复核", stats.get("pending", 0))
        m2.metric("已批准", stats.get("approved", 0))
        m3, m4 = st.columns(2)
        m3.metric("已驳回", stats.get("rejected", 0))
        m4.metric("已提交", stats.get("submitted", 0))

        st.divider()
        status_filter = st.selectbox(
            "状态筛选",
            ["", "pending", "approved", "rejected", "submitted"],
            format_func=lambda v: "全部(跟随页签)" if not v else f"{STATUS_CN.get(v, v)}({v})",
        )
        query = st.text_input(
            "搜索站点 URL", value="", placeholder="子串匹配,如 example.com"
        )

        st.divider()
        st.subheader("数据目录")
        st.markdown(
            f"- 数据根目录:`{cfg.data_dir}`\n"
            f"- 复核队列库:`{cfg.db_path}`\n"
            f"- 证据目录:`{cfg.evidence_dir}`\n"
            f"- 可用环境变量 `{DATA_DIR_ENV}` 覆盖(默认 `./data`)"
        )

    # ---------------- 主区:三个页签 ----------------
    tab_pending, tab_approved, tab_all = st.tabs(["待复核", "已批准", "全部"])
    for tab, tab_status in ((tab_pending, "pending"), (tab_approved, "approved"), (tab_all, None)):
        with tab:
            # 侧栏选了具体状态时,以侧栏为准;否则跟随页签
            effective = status_filter or tab_status
            shown = filter_entries(entries, status=effective or None, q=query)
            if not shown:
                st.info("当前筛选条件下没有条目。")
                continue
            st.caption(f"共 {len(shown)} 条")
            for entry in shown:
                _render_card(entry, queue, cfg)


def main() -> int:
    """脚本入口:缺 streamlit 时打印中文安装提示并返回退出码 1。"""
    if not _HAS_ST:
        print(
            "未安装 Streamlit,人工复核台无法启动。\n"
            "请先执行:python -m pip install -e \".[ui]\"\n"
            "然后运行:streamlit run webui/app.py",
            file=sys.stderr,
        )
        return 1
    render()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
