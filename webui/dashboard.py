"""净网哨兵 · 复核台 v2 运营仪表盘(A56,独立入口,不改动 A30 的 webui/app.py)。

定位:面向**运营者**的统计与治理视图——复核趋势、模型一致性、站点关联
图谱、政策模拟与四眼复核状态。与 A30 人工复核台(app.py,面向"逐条
拍板")互补:本页面只做只读分析 + 政策预演 + 四眼留痕,**绝不执行任何
举报提交**;举报仍一律走 orchestrator/CLI 在人工门约束下的既有流程。

结构约定(与 tests/test_dashboard_helpers.py 对应):
- 纯逻辑层(本文件上半部分,无 streamlit、无兄弟模块顶层依赖,可独立导入):
  * :func:`trend_rows`        复核条目按入列日期聚合趋势行;
  * :func:`agreement_matrix`  同图多模型评分的两两一致率矩阵;
  * :func:`graph_rows`        图谱 export_json → 站点关联表格行;
  * :func:`policy_preview`    政策引擎决策 → 中文一行预览;
  * :func:`ledger_health`     事件账本健康行(读侧零写入,A234);
  * :func:`ledger_path_from_cfg` cfg 附加属性 event_ledger 的读取口径;
  * :data:`EDGE_KIND_CN` 等展示用中文映射常量。
- UI 层(下半部分,streamlit 顶部惰性 try/except,缺依赖时 main() 打印
  中文安装提示并返回退出码 1):
  * :func:`render` 侧栏数据目录(NETSENTINEL_DATA_DIR)+ 四页签:
    ①趋势 ②模型一致性 ③关联图谱 ④政策与四眼。

安全红线(必须体现在代码里):
- 本页面零网络行为:数据只来自本地 SQLite 队列/图谱库与证据包 manifest;
- 批准前必须勾选"我已人工核实证据真实有效"(与 A30 同款双重确认);
- 四眼批准只完成复核留痕(只增审批、不削弱人工门),提交仍须人工门。

V5 升级(A101,本机未装 streamlit,只动纯逻辑层):
- 性能:``trend_rows`` 桶模板由"每条目构造一次"降为"每日构造一次"
  (N 条目 D 天少构造 N-D 个字典);
- 可观测性:``policy_preview`` 每次预演计数 ``telemetry.inc
  ("dashboard.policy_preview")``(纯逻辑层打点);
- 质量:四个纯函数注解细化(入参容忍 None)并补 docstring 用法示例。

A234 升级(事件账本接线,默认关闭):
- 纯逻辑层新增 :func:`ledger_health`(账本健康行:latest_seq / 事件数 /
  最近事件类型,**读侧零写入**——只调 query / latest_seq,不 append)与
  :func:`ledger_path_from_cfg`(cfg 附加属性 ``event_ledger`` 的读取口径,
  缺省空 = 关闭 = 现状);
- UI 层:侧栏数据目录追加"账本健康"行;页签④构造四眼队列时按
  ``cfg.event_ledger`` 注入 :class:`~netsentinel.storage.event_log.EventLog`
  (透传 FourEyesQueue,A222 双写自动生效),账本生命周期归页签函数
  (用毕即关,读侧零写入)。
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
from dataclasses import dataclass, field
from typing import Any

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
    "trend_rows",
    "agreement_matrix",
    "graph_rows",
    "policy_preview",
    "ledger_health",
    "ledger_path_from_cfg",
    "EDGE_KIND_CN",
    "ACTION_CN",
    "render",
    "main",
]

# ===========================================================================
# 纯逻辑层(无 streamlit 依赖;兄弟模块一律函数内导入)
# ===========================================================================

#: 趋势聚合的四类状态(与 review_queue.VALID_STATUSES 同口径,本地声明
#: 以保持本模块可独立导入,不顶层依赖兄弟模块)。
_TREND_STATUSES: tuple[str, ...] = ("pending", "approved", "rejected", "submitted")

#: 模型一致性判定容差:同一张图上两模型 nsfw_prob 之差 ≤ 该值记为一致。
AGREEMENT_TOLERANCE: float = 0.2

#: 浮点边界保护:0.9 - 0.7 之类的"恰好 0.2"因二进制误差略大于 0.2,
#: 加一个小 epsilon 保证数学意义上的 ≤ 0.2 一律记一致。
_EPS: float = 1e-9

#: 图谱节点 id 的站点命名空间前缀(与 intel/graph._site_id 一致)。
_SITE_PREFIX: str = "site:"

#: 数据目录覆盖环境变量(与 webui/app.py 同名约定,默认 ./data)。
DATA_DIR_ENV: str = "NETSENTINEL_DATA_DIR"

#: 图谱边种类 → 中文(展示用;未知种类原样展示)。
EDGE_KIND_CN: dict[str, str] = {
    "shared_image": "共享图片",
    "phash_near": "近重复图片",
    "shared_template": "共享模板",
    "redirect": "重定向",
}

#: 政策 action → 中文(与 netsentinel.policy.VALID_ACTIONS 对应)。
ACTION_CN: dict[str, str] = {
    "queue": "进入人工复核队列",
    "notify": "发送待复核提醒",
    "ignore": "仅记录,不再提醒",
    "four_eyes": "四眼复核",
}

#: 队列状态 → 中文(UI 展示用)。
_STATUS_CN: dict[str, str] = {
    "pending": "待复核",
    "approved": "已批准",
    "rejected": "已驳回",
    "submitted": "已提交",
}


def _field(src: Any, name: str, default: Any = "") -> Any:
    """从鸭子类型对象或 dict 里取字段;取到 None 时回退默认值。"""
    if isinstance(src, dict):
        val = src.get(name, default)
    else:
        val = getattr(src, name, default)
    return default if val is None else val


# ---------------------------------------------------------------------------
# trend_rows:复核条目按日期聚合
# ---------------------------------------------------------------------------

def trend_rows(entries: list | None) -> list[dict]:
    """把复核条目按 ``created_at`` 的日期聚合为趋势行。

    - 条目鸭子类型:``created_at``(ISO 字符串)与 ``status``;Entry 对象、
      dict 均可;``entries`` 为 None 按空处理;
    - 非法日期(空串 / 非 ISO / 无法解析)的条目整行跳过,不计入任何桶;
    - 每行形如 ``{"date": "YYYY-MM-DD", "pending": n, "approved": n,
      "rejected": n, "submitted": n, "total": n}``:四个状态桶只计已知状态,
      ``total`` 计该日全部合法条目(未知状态只进 total,不丢行数);
    - 返回按日期升序排序(ISO 日期字符串排序即时间序)。

    V5 性能:桶模板每日只构造一次(原先每个条目都会先构造一份模板字典
    再 setdefault 丢弃),N 条 D 天少构造 N-D 个字典,单遍聚合不变。

    示例::

        >>> trend_rows([{"created_at": "2026-10-01T09:00:00+08:00",
        ...              "status": "pending"}])
        [{'date': '2026-10-01', 'pending': 1, 'approved': 0,
          'rejected': 0, 'submitted': 0, 'total': 1}]
    """
    buckets: dict[str, dict[str, Any]] = {}
    for entry in entries or []:
        raw = str(_field(entry, "created_at", "") or "").strip()
        try:
            day = _dt.datetime.fromisoformat(raw).date().isoformat()
        except ValueError:
            continue
        row = buckets.get(day)
        if row is None:
            row = {"date": day, **dict.fromkeys(_TREND_STATUSES, 0), "total": 0}
            buckets[day] = row
        row["total"] += 1
        status = str(_field(entry, "status", "") or "")
        if status in _TREND_STATUSES:
            row[status] += 1
    return [buckets[day] for day in sorted(buckets)]


# ---------------------------------------------------------------------------
# agreement_matrix:同图多模型评分的两两一致率
# ---------------------------------------------------------------------------

def _parse_score(score: Any) -> tuple[str, str, float] | None:
    """把一条评分规整为 ``(图片路径, 模型名, nsfw_prob)``;不可用返回 None。

    同时容忍两种形态:
    - :class:`~netsentinel.contracts.ImageScore` 对象(``image.path`` /
      ``model`` / ``nsfw_prob``);
    - 其 ``as_dict()`` 产物(dict,``image`` 直接是路径字符串)。
    路径 / 模型为空,或概率缺失 / 非数字(bool 不算)的行一律丢弃。
    """
    if isinstance(score, dict):
        image = score.get("image")
        path = image if isinstance(image, str) else str(_field(image, "path", "") or "")
        model = score.get("model")
        prob = score.get("nsfw_prob")
    else:
        image = getattr(score, "image", None)
        path = str(_field(image, "path", "") or "")
        model = getattr(score, "model", None)
        prob = getattr(score, "nsfw_prob", None)
    path = str(path or "").strip()
    model = str(model or "").strip()
    if not path or not model:
        return None
    if isinstance(prob, bool) or not isinstance(prob, (int, float)):
        return None
    return path, model, float(prob)


def agreement_matrix(image_scores: list | None) -> dict:
    """对同一 image path 上的多模型分数计算两两一致率矩阵。

    - 一致定义:同一张图上两模型的 ``nsfw_prob`` 之差
      ≤ :data:`AGREEMENT_TOLERANCE`(0.2,含浮点边界保护);
    - 一致率 = 一致配对数 / 该模型对的总配对数(两模型至少共同评过
      一张图才会出现在矩阵里,否则该对缺省);
    - 返回 ``{"models": [去重排序的模型名], "matrix": {(a, b): rate}}``,
      键 ``(a, b)`` 规范化为字典序 ``a < b``,每个无序模型对只存一条
      (配对去重、对称:取 (b, a) 与取 (a, b) 是同一条数据);
    - 单模型 → ``{"models": [m], "matrix": {}}``;空输入 / 全部不可用 →
      ``{"models": [], "matrix": {}}``。

    示例::

        >>> agreement_matrix([
        ...     {"image": "a.png", "model": "m1", "nsfw_prob": 0.5},
        ...     {"image": "a.png", "model": "m2", "nsfw_prob": 0.6},
        ... ])["matrix"][("m1", "m2")]
        1.0
    """
    by_image: dict[str, dict[str, float]] = {}
    for score in image_scores or []:
        parsed = _parse_score(score)
        if parsed is None:
            continue
        path, model, prob = parsed
        by_image.setdefault(path, {})[model] = prob  # 同图同模型以后值为准
    models = sorted({m for scores in by_image.values() for m in scores})
    agree: dict[tuple[str, str], int] = {}
    pairs: dict[tuple[str, str], int] = {}
    for scores in by_image.values():
        names = sorted(scores)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                key = (names[i], names[j])
                pairs[key] = pairs.get(key, 0) + 1
                if abs(scores[names[i]] - scores[names[j]]) <= (
                    AGREEMENT_TOLERANCE + _EPS
                ):
                    agree[key] = agree.get(key, 0) + 1
    matrix = {pair: agree.get(pair, 0) / n for pair, n in pairs.items() if n > 0}
    return {"models": models, "matrix": matrix}


# ---------------------------------------------------------------------------
# graph_rows:图谱导出 → 站点关联表格行
# ---------------------------------------------------------------------------

def _strip_site(node_id: str) -> str:
    """剥掉站点节点 id 的 ``site:`` 前缀(与 intel/graph._site_url 同口径)。"""
    if node_id.startswith(_SITE_PREFIX):
        return node_id[len(_SITE_PREFIX):]
    return node_id


def graph_rows(export: dict | None) -> list[dict]:
    """把 :meth:`intel.graph.EvidenceGraph.export_json` 的产物转成表格行。

    - 只保留 **site→site** 的边(``src`` / ``dst`` 均带 ``site:`` 前缀,
      image/template 等其他命名空间的边一律过滤);
    - 每行形如 ``{"source": 站点URL, "target": 站点URL, "kind": 边种类,
      "weight": float}``(前缀已剥除;weight 缺失/非法按 0.0);
    - 容忍空输入 / 结构缺失(``None``、无 ``edges`` 键、非 dict 边元素),
      这些情况返回空列表或跳过该行,不抛错;保持导出顺序(确定性)。

    示例::

        >>> graph_rows({"edges": [{"src": "site:a", "dst": "site:b",
        ...                        "kind": "redirect"}]})
        [{'source': 'a', 'target': 'b', 'kind': 'redirect', 'weight': 0.0}]
    """
    edges = export.get("edges") if isinstance(export, dict) else None
    rows: list[dict] = []
    for edge in edges or []:
        if not isinstance(edge, dict):
            continue
        src = str(edge.get("src") or "")
        dst = str(edge.get("dst") or "")
        if not (src.startswith(_SITE_PREFIX) and dst.startswith(_SITE_PREFIX)):
            continue
        weight = edge.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            weight = 0.0
        rows.append(
            {
                "source": _strip_site(src),
                "target": _strip_site(dst),
                "kind": str(edge.get("kind") or ""),
                "weight": float(weight),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# policy_preview:政策决策 → 中文一行
# ---------------------------------------------------------------------------

def policy_preview(report: Any, rules: Any) -> str:
    """把 :func:`netsentinel.policy.decide` 的结果转成中文一行预览。

    - 命中显式规则 → ``"命中规则 {规则名} → 动作:{中文动作}({说明})"``
      (规则无备注时省略括号);
    - 未命中任何规则 → ``"未命中规则 → 兜底动作:{中文动作}({说明})"``
      (兜底动作固定为 queue=进入人工复核队列);
    - 政策模块缺失 → ``"政策引擎未就位"``;
    - 规则 action 非法等 :class:`ValueError` → ``"政策决策失败:{中文原因}"``
      (页面不因坏规则崩溃,红线:任何 action 都不能跳过人工门)。

    每次预演计入 ``telemetry.inc("dashboard.policy_preview")``(V5
    可观测性;含失败预演,便于观测坏规则出现的频率)。

    示例::

        >>> policy_preview(_report(), None)  # doctest: +SKIP
        '命中规则 兜底入列 → 动作:进入人工复核队列(...)'
    """
    telemetry.inc("dashboard.policy_preview")
    try:
        from netsentinel.policy import decide
    except ImportError:
        return "政策引擎未就位"
    try:
        decision = decide(report, rules)
    except ValueError as exc:
        return f"政策决策失败:{exc}"
    action_cn = ACTION_CN.get(decision.action, str(decision.action))
    note = str(decision.note or "").strip()
    suffix = f"({note})" if note else ""
    if decision.matched:
        return f"命中规则 {decision.rule_name} → 动作:{action_cn}{suffix}"
    return f"未命中规则 → 兜底动作:{action_cn}{suffix}"


# ---------------------------------------------------------------------------
# ledger_health / ledger_path_from_cfg:事件账本接线(A234,读侧零写入)
# ---------------------------------------------------------------------------

#: 账本健康行的字段契约(latest_seq / 事件数 / 最近事件类型 / 错误)。
LEDGER_HEALTH_KEYS: tuple[str, ...] = (
    "enabled",
    "latest_seq",
    "events_total",
    "last_event_type",
    "error",
)


def ledger_path_from_cfg(cfg: Any) -> str:
    """从 cfg 读取事件账本路径(附加属性 ``event_ledger``,默认空 = 关闭)。

    采用**附加属性**口径(与 ``cfg.policy_path`` / ``cfg.four_eyes_required``
    的"cfg 随 render 传入页签"惯例一致):``getattr`` 防御性读取,配置对象
    没有该属性 / 值为 None / 纯空白一律视为未启用 = 现状(零接线)。
    """
    return str(getattr(cfg, "event_ledger", "") or "").strip()


def ledger_health(event_log: Any) -> dict:
    """事件账本健康行(latest_seq / 事件数 / 最近事件类型,读侧零写入)。

    - ``event_log`` 为 ``None`` → ``{"enabled": False, ...}``(未接线 = 现状);
    - 只调用鸭子契约的 ``latest_seq()`` / ``iter_events()`` 两个**读**方法
      (EventLog 契约),绝不 ``append``——读侧零写入;
    - 账本损坏等读取异常**不抛**:落入 ``error`` 字段的中文提示(健康行
      绝不让页面崩溃,与 :func:`policy_preview` 的防御口径一致)。

    示例::

        >>> ledger_health(None)["enabled"]
        False
    """
    if event_log is None:
        return {
            "enabled": False,
            "latest_seq": 0,
            "events_total": 0,
            "last_event_type": "",
            "error": "",
        }
    try:
        latest = int(event_log.latest_seq())
        events = list(event_log.iter_events())
        last_type = str(events[-1].event_type) if events else ""
        return {
            "enabled": True,
            "latest_seq": latest,
            "events_total": len(events),
            "last_event_type": last_type,
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001 - 健康行兜底:读取失败不上抛
        return {
            "enabled": True,
            "latest_seq": 0,
            "events_total": 0,
            "last_event_type": "",
            "error": f"账本读取失败:{exc}",
        }


# ===========================================================================
# UI 层(以下代码仅在 streamlit 运行时执行;兄弟模块一律函数内导入)
# ===========================================================================

_PAGE_TITLE = "净网哨兵 · 运营仪表盘"
_MOTTO = "运营视图:趋势 · 模型一致性 · 团伙关联 · 政策预演;机器初筛,人工拍板,本页不执行举报提交。"
_CONFIRM_LABEL = "我已人工核实证据真实有效"

#: 一致性页签最多回读的证据包条数(最近优先,IO 保护)。
_MAX_MANIFEST_ENTRIES: int = 100

#: 四眼状态 → 中文。
_FOUR_EYES_STATE_CN: dict[str, str] = {
    "none": "尚无审核人确认",
    "awaiting_second": "已有一名审核人确认,等待第二审核人",
    "approved": "复核确认已完成",
}


def _load_report(entry: Any) -> dict | None:
    """从条目的证据包读回报告 dict(复用 A30 app.py 的 manifest 定位逻辑)。"""
    from webui.app import load_report_dict  # 兄弟模块函数内导入

    return load_report_dict(str(_field(entry, "evidence_zip", "")))


@dataclass
class _ReportProxy:
    """把复核条目 + 证据包报告 dict 适配成 policy.decide 需要的鸭子报告。

    decide 只读 ``verdict.value / agg_nsw_prob / needs_review / intel``;
    报告 dict 缺失或字段非法时给出保守默认(agg=0、needs_review=True——
    pending 条目按需复核处理,绝不凭空放大风险)。
    """

    verdict: Any = None
    agg_nsw_prob: float = 0.0
    needs_review: bool = True
    intel: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_entry(cls, entry: Any, report_dict: dict | None) -> _ReportProxy:
        from netsentinel.contracts import Verdict  # 兄弟模块函数内导入

        raw = _field(entry, "verdict", "")
        verdict_str = str(getattr(raw, "value", None) or raw or "")
        try:
            verdict: Any = Verdict(verdict_str)
        except ValueError:
            verdict = Verdict.CLEAN
        report = report_dict if isinstance(report_dict, dict) else {}
        agg = report.get("agg_nsw_prob")
        if isinstance(agg, bool) or not isinstance(agg, (int, float)):
            agg = 0.0
        needs = report.get("needs_review")
        if not isinstance(needs, bool):
            needs = True
        intel = report.get("intel")
        if not isinstance(intel, dict):
            intel = {}
        return cls(
            verdict=verdict,
            agg_nsw_prob=float(agg),
            needs_review=needs,
            intel=dict(intel),
        )


def _four_eyes_result_cn(result: dict) -> str:
    """把 FourEyesQueue.approve 的返回 dict 转成中文结果文案。"""
    if str(result.get("state", "")) == "awaiting_second":
        return f"第一审核人 {result.get('first', '')} 已确认,等待另一名审核人再次批准。"
    reviewers = result.get("reviewers") or []
    names = "、".join(str(r) for r in reviewers) or "-"
    return f"复核确认完成(审核人:{names});举报提交仍须通过人工门,本页面不执行提交。"


def _rate_cell_style(value: Any) -> str:
    """一致率单元格配色:>0.8 绿、>0.6 黄、其余红;空值不着色。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return ""
    if num != num:  # NaN:两模型无共同评分
        return ""
    if num > 0.8:
        return "background-color: #c6efce; color: #006100"
    if num > 0.6:
        return "background-color: #ffeb9c; color: #9c6500"
    return "background-color: #ffc7ce; color: #9c0006"


def _trend_frame(rows: list[dict]) -> Any:
    """趋势行 → 以日期为索引、中文列名的图表数据帧(streamlit 依赖 pandas)。"""
    import pandas as pd

    frame = pd.DataFrame(rows).set_index("date")
    frame = frame.rename(
        columns={s: _STATUS_CN[s] for s in _TREND_STATUSES}
    )
    return frame[[_STATUS_CN[s] for s in _TREND_STATUSES]]


def _agreement_table(result: dict) -> Any:
    """一致率结果 → 对称热力表(对角线 1.0,缺配对为空;单元格按阈值配色)。"""
    import pandas as pd

    models = result["models"]
    matrix = result["matrix"]
    data: dict[str, list[Any]] = {}
    for row in models:
        cells: list[Any] = []
        for col in models:
            if row == col:
                cells.append(1.0)
            else:
                pair = (row, col) if row < col else (col, row)
                cells.append(matrix.get(pair))
        data[row] = cells
    frame = pd.DataFrame(data, index=models)
    styler = frame.style.format(lambda v: "" if pd.isna(v) else f"{float(v):.2f}")
    # pandas ≥2.1 为 Styler.map,<2.1 为 applymap(3.0 已移除),双兼容。
    applier = getattr(styler, "map", None)
    if applier is None:  # pragma: no cover - 老版本 pandas 分支
        applier = styler.applymap
    return applier(_rate_cell_style)


def _render_trend(entries: list, stats: dict[str, int]) -> None:
    """页签①趋势:summary 指标卡 + 按日聚合柱状图。"""
    st.subheader("复核趋势")
    cols = st.columns(5)
    for col, key in zip(cols, _TREND_STATUSES):
        col.metric(_STATUS_CN[key], int(stats.get(key, 0)))
    cols[4].metric("总计", sum(int(v) for v in stats.values()))
    rows = trend_rows(entries)
    if not rows:
        st.info("复核队列暂无条目,无法统计趋势。")
        return
    st.bar_chart(_trend_frame(rows))
    st.caption("按条目入列日期(created_at)聚合;只统计日期合法的条目。")


def _render_agreement(entries: list) -> None:
    """页签②模型一致性:聚合证据包 image_scores → 两两一致率热力表。"""
    st.subheader("模型一致性")
    scores: list[Any] = []
    for entry in entries[-_MAX_MANIFEST_ENTRIES:]:
        report = _load_report(entry)
        if isinstance(report, dict) and isinstance(report.get("image_scores"), list):
            scores.extend(report["image_scores"])
    result = agreement_matrix(scores)
    models = result["models"]
    if not models:
        st.info("暂无模型评分数据(证据包尚未生成,或未包含 image_scores)。")
        return
    st.caption(f"参与模型({len(models)} 个):{'、'.join(models)}")
    if len(models) < 2:
        st.info(f"当前只有单一模型「{models[0]}」,没有跨模型一致性可计算。")
        return
    st.dataframe(_agreement_table(result))
    st.caption(
        f"一致率 = 同一张图上两模型 nsfw_prob 之差 ≤ {AGREEMENT_TOLERANCE} 的配对占比;"
        "配色:绿 > 0.8、黄 > 0.6、红 ≤ 0.6;对角线恒为 1.0;"
        "空白表示两模型从未评过同一张图。"
    )
    st.caption(f"数据来源:最近 {_MAX_MANIFEST_ENTRIES} 条复核条目的证据包 manifest。")


def _render_graph(graph_path: str) -> None:
    """页签③关联图谱:节点计数指标卡 + site→site 关联边表。"""
    st.subheader("站点关联图谱")
    try:
        from netsentinel.intel.graph import EvidenceGraph  # 兄弟模块函数内导入
    except ImportError:
        st.warning("图谱模块(netsentinel.intel.graph)未就位,无法展示站点关联。")
        return
    try:
        graph = EvidenceGraph(graph_path)
    except Exception as exc:  # 库文件打不开等异常不让页面崩溃
        st.error(f"打开图谱库失败:{exc}")
        return
    stats = graph.stats()
    cols = st.columns(4)
    cols[0].metric("站点节点", int(stats.get("sites", 0)))
    cols[1].metric("图片节点", int(stats.get("images", 0)))
    cols[2].metric("模板节点", int(stats.get("templates", 0)))
    cols[3].metric("关联边", int(stats.get("edges", 0)))
    rows = graph_rows(graph.export_json())
    if not rows:
        st.info("图谱暂无站点间关联边(需至少两个站点共享图片/模板,或存在重定向)。")
        return
    display = [
        {
            "源站点": r["source"],
            "目标站点": r["target"],
            "关联类型": EDGE_KIND_CN.get(r["kind"], r["kind"]),
            "权重": f"{r['weight']:.2f}",
        }
        for r in sorted(
            rows, key=lambda r: (-r["weight"], r["source"], r["target"])
        )
    ]
    st.table(display)
    st.caption("仅展示 site→site 边;权重 = 两站点共享素材数 / 并集素材数(Jaccard),按权重降序。")


def _render_policy_and_four_eyes(
    entries: list, db_path: str, cfg: Any
) -> None:
    """页签④政策与四眼:选一条 pending → 政策预演 + 四眼状态与双人批准。"""
    st.subheader("政策模拟与四眼复核")
    pending = [e for e in entries if str(_field(e, "status", "")) == "pending"]
    if not pending:
        st.info("当前没有待复核条目,无需政策模拟或四眼确认。")
        return
    options = {
        f"#{_field(e, 'id', 0)} · {_field(e, 'site_url', '')}": e for e in pending
    }
    label = st.selectbox("选择待复核条目", list(options))
    entry = options[label]
    entry_id = int(_field(entry, "id", 0))
    proxy = _ReportProxy.from_entry(entry, _load_report(entry))

    # ---- 政策模拟(只读预演,不做任何分流动作)----
    st.markdown("**政策模拟**")
    try:
        from netsentinel.policy import load_policy  # 兄弟模块函数内导入
    except ImportError:
        st.warning("政策引擎未就位,无法进行政策模拟。")
    else:
        try:
            rules = load_policy(cfg.policy_path)
        except (ValueError, ImportError) as exc:
            st.warning(f"政策文件加载失败:{exc}")
        else:
            st.caption(
                f"政策文件:`{cfg.policy_path}`(缺失时使用内置默认政策:全部进入人工复核)"
            )
            st.info(policy_preview(proxy, rules))
    st.caption(
        "政策只决定“入列 / 提醒 / 记录 / 追加第二审核人”,任何 action 都不能跳过人工门。"
    )

    # ---- 四眼复核状态与双人批准 ----
    st.divider()
    st.markdown("**四眼复核状态**")
    try:
        from netsentinel.decision.four_eyes import FourEyesQueue  # 兄弟模块函数内导入
    except ImportError:
        st.warning("四眼复核模块(netsentinel.decision.four_eyes)未就位。")
        return
    four_eyes_on = st.checkbox(
        "启用四眼复核(双人批准)",
        value=bool(cfg.four_eyes_required),
        help="会话级开关,取自配置 four_eyes_required;只会增加审批环节,不会削弱人工门。",
    )
    # A234:cfg 附加属性 event_ledger(默认空 = 关闭 = 现状)。启用时把账本
    # 透传给 FourEyesQueue(A222:approve/双人确认先账本后状态双写);账本
    # 生命周期归本函数(用毕即关),打开失败只告警不阻断四眼复核。
    ledger_path = ledger_path_from_cfg(cfg)
    ledger = None
    if ledger_path:
        from netsentinel.storage.event_log import EventLog  # 兄弟模块函数内导入

        try:
            ledger = EventLog(ledger_path)
        except Exception as exc:  # noqa: BLE001 - 打开失败不阻断页面
            st.warning(f"事件账本打开失败:{exc}(四眼复核继续,但本次不落事件账本)")
    try:
        queue = FourEyesQueue(db_path, four_eyes_on, event_log=ledger)
        try:
            status = queue.status(entry_id)
        except ValueError as exc:
            st.warning(f"{exc}")
            return
        state = str(status.get("state", ""))
        reviewers = status.get("reviewers") or []
        st.markdown(
            f"- 当前状态:{_FOUR_EYES_STATE_CN.get(state, state)}\n"
            f"- 已确认审核人:{('、'.join(str(r) for r in reviewers)) if reviewers else '(无)'}\n"
            f"- 四眼要求:{'双人批准' if status.get('required') else '单人即可(四眼未启用)'}"
        )
        reviewer = st.text_input("审核人姓名", value="", key=f"fe_reviewer_{entry_id}")
        confirmed = st.checkbox(_CONFIRM_LABEL, key=f"fe_confirm_{entry_id}")
        if st.button(
            "批准(四眼)",
            key=f"fe_approve_{entry_id}",
            type="primary",
            disabled=not confirmed,
        ):
            name = reviewer.strip()
            if not name:
                st.warning("请填写审核人姓名。")
            else:
                try:
                    st.success(_four_eyes_result_cn(queue.approve(entry_id, name)))
                except ValueError as exc:
                    st.warning(f"{exc}")
        st.caption("四眼批准只完成复核留痕;举报提交仍须在复核台/CLI 走人工门,本页面不执行提交。")
    finally:
        if ledger is not None:
            try:
                ledger.close()
            except Exception:  # pragma: no cover - 关闭异常不阻断页面
                pass


def render() -> None:
    """运营仪表盘主界面(streamlit 脚本入口调用的渲染函数)。"""
    if not _HAS_ST:  # pragma: no cover - main() 已拦截,防御性兜底
        raise RuntimeError(
            "streamlit 不可用,无法渲染运营仪表盘;请先安装 python -m pip install -e \".[ui]\""
        )

    from pathlib import Path

    from netsentinel.config import load_config  # 兄弟模块函数内导入
    from netsentinel.decision.review_queue import ReviewQueue

    st.set_page_config(page_title=_PAGE_TITLE, page_icon="📊", layout="wide")
    st.title(f"📊 {_PAGE_TITLE}")
    st.caption(_MOTTO)

    cfg = load_config()
    with st.sidebar:
        st.header("运行参数")
        root = st.text_input(
            "数据根目录",
            value=os.environ.get(DATA_DIR_ENV, "data"),
            help=f"初值取环境变量 {DATA_DIR_ENV}(默认 ./data);复核队列与图谱库从该目录读取。",
        )
        db_path = str(Path(root) / "review_queue.db")
        graph_path = str(Path(root) / "graph.db")
        st.caption(
            f"- 复核队列:`{db_path}`\n"
            f"- 关联图谱:`{graph_path}`\n"
            f"- 政策文件:`{cfg.policy_path}`"
        )
        # A234:账本健康行(cfg 附加属性 event_ledger,默认空 = 不展示 =
        # 现状)。读侧零写入:文件存在才打开(不因展示而建库),只读
        # latest_seq / iter_events。
        ledger_path = ledger_path_from_cfg(cfg)
        if ledger_path:
            if Path(ledger_path).is_file():
                from netsentinel.storage.event_log import (  # 兄弟模块函数内导入
                    EventLog,
                )

                try:
                    with EventLog(ledger_path) as ledger:
                        health = ledger_health(ledger)
                except Exception as exc:  # noqa: BLE001 - 健康行兜底
                    health = {
                        "enabled": True,
                        "latest_seq": 0,
                        "events_total": 0,
                        "last_event_type": "",
                        "error": f"账本打开失败:{exc}",
                    }
            else:
                health = {
                    "enabled": True,
                    "latest_seq": 0,
                    "events_total": 0,
                    "last_event_type": "",
                    "error": "账本文件不存在(尚未接线或未生成)",
                }
            if health.get("error"):
                st.caption(f"- 事件账本:`{ledger_path}` —— {health['error']}")
            else:
                st.caption(
                    f"- 事件账本:`{ledger_path}`"
                    f"(latest_seq {health['latest_seq']} / "
                    f"事件 {health['events_total']} 条 / "
                    f"最近事件 {health['last_event_type'] or '-'})"
                )
        st.divider()
        st.caption("本页面只做只读分析、政策预演与四眼留痕,不执行任何举报提交。")

    queue = ReviewQueue(db_path)
    entries = queue.list(None)
    stats = queue.summary()

    tab_trend, tab_agree, tab_graph, tab_policy = st.tabs(
        ["趋势", "模型一致性", "关联图谱", "政策与四眼"]
    )
    with tab_trend:
        _render_trend(entries, stats)
    with tab_agree:
        _render_agreement(entries)
    with tab_graph:
        _render_graph(graph_path)
    with tab_policy:
        _render_policy_and_four_eyes(entries, db_path, cfg)


def main() -> int:
    """脚本入口:缺 streamlit 时打印中文安装提示并返回退出码 1。"""
    if not _HAS_ST:
        print(
            "未安装 Streamlit,运营仪表盘无法启动。\n"
            "请先执行:python -m pip install -e \".[ui]\"\n"
            "然后运行:streamlit run webui/dashboard.py",
            file=sys.stderr,
        )
        return 1
    render()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
