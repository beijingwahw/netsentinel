"""净网哨兵 · 复核台提供方面板(A76,独立入口,不改动 A30 app.py / A56 dashboard.py)。

定位:面向**运营者**的"全平台视觉模型"接入状态视图——配了哪些平台、本地
推理网关活着没有、跨平台评分一致不一致、花了多少钱。与人工复核台
(app.py,逐条拍板)和运营仪表盘(dashboard.py,趋势与治理)互补:
本页面只做只读展示 + 人工显式诊断(ping / 本地探活),**绝不执行任何
举报提交,常规浏览零外呼**。

结构约定(与 tests/test_providers_page.py 对应):
- 纯逻辑层(本文件上半部分,无 streamlit、无兄弟模块顶层依赖,可独立导入):
  * :func:`provider_rows`       提供方目录 20 家 → 总表行(密钥布尔化,绝不回显);
  * :func:`ping_row`            vlmctl ping 输出 → 单行诊断结果(中文注);
  * :func:`agreement_rows`      A69 analyze 输出 → 跨平台一致性表格行;
  * :func:`cost_rows`           A75 summary.by_provider → 成本表格行;
  * :func:`batch_cost_rows`     runs/*/cost.jsonl 批次成本快照 → 批次行(A232);
  * :func:`weight_ci_rows`      注入 tracker / dict → 贝叶斯权重区间行(A220);
  * :func:`weight_point_rows`   注入 tracker → 权重点值行(区间缺席回退);
  * :func:`bayes_tracker_from_jsonl` 本地反馈账本 → 贝叶斯 tracker(零外呼;
    A230 升级:行内 ts 优先消费、half_life 对齐 ``cfg.bayes_half_life``);
  * :func:`render_status_badge` 布尔 → 中文状态徽章("✅ 正常" / "❌ 异常")。
- UI 层(下半部分,streamlit 顶部惰性 try/except,缺依赖时 main() 打印
  中文安装提示并返回退出码 1):
  * :func:`render` 六页签:①提供方总表 ②本地网关 ③跨平台一致性 ④成本
  ⑤权重区间(贝叶斯,A220)⑥批次成本(A232)。

安全红线(必须体现在代码里):
- **ping / 探活为人工显式诊断动作**(V4 红线 20):本地网关探活只在点击
  "刷新"按钮后发起,云端 ping 须勾选知悉确认后才执行,且复用 A67 vlmctl
  的唯一外呼路径(先记账 vlm_cache.spend_one,再外呼,红线 19);
- **密钥绝不回显**(红线 17):总表只显示 已配置/未配置 布尔,本模块不持有
  任何密钥字符串;
- 本地提供方(ollama/vllm/lmstudio/xinference)数据不出本机(红线 16),
  探活仅允许本机回环地址(A66 probe 自带校验);
- 目录里的模型名 / 端点均为提示值,以各平台官方文档为准,上线前核验一次
  (红线 18)。

V5 升级(A101,本机未装 streamlit,只动纯逻辑层):
- 可观测性:``ping_row`` 每次规整一条人工 ping 诊断结果计数
  ``telemetry.inc("providers_page.ping")``(ping 是本页唯一外呼型
  人工显式诊断动作,纯逻辑层打点即可覆盖);
- 质量:五个纯函数补 docstring 用法示例。

A220 增强(贝叶斯权重区间展示,V13 只读消费):
- 纯逻辑层新增 :func:`weight_ci_rows`(注入 tracker(鸭子类型
  ``weights_with_ci()``)或其输出 dict → 展示行 ``{member, weight,
  ci_lo, ci_hi}``,中文表头"成员/权重/95%区间")、:func:`weight_point_rows`
  (区间缺席时的点值回退)与 :func:`bayes_tracker_from_jsonl`(从本地
  ``reliability.jsonl`` 反馈账本确定性重建贝叶斯事件流,零外呼);
- UI 层新增页签⑤权重区间:数据缺席时中文空态提示,零区间数据回退
  点值展示,既有四页签行为不变。

A232 增强(批次成本快照展示,收官成本归集的只读消费):
- 纯逻辑层新增 :func:`batch_cost_rows`(扫 ``<data_dir>/runs/*/cost.jsonl``
  收官快照 → 行 ``{run_id, models, calls, est_cost, unpriced_calls}``,
  中文表头见 :data:`BATCH_COST_HEADERS_ZH`;目录缺席/坏文件回退空态,
  绝不抛错);
- UI 层新增页签⑥批次成本:空态中文提示;既有五页签行为不变。

A230 升级(贝叶斯重建口径,"自造滴答的展示近似" → 与决策层重放同口径):
- :func:`bayes_tracker_from_jsonl` 优先消费行内 ``ts`` 字段(有 ts 的行按
  真实时刻重放,缺 ts 的旧行滴答回退并计数 ``providers_page.bayes.
  ts_fallback``),``half_life`` 对齐 ``cfg.bayes_half_life``(V14 一等
  字段,单位天,``getattr`` 防御缺省 → ``None`` = 关闭遗忘的展示近似
  口径保留);行构造层零改动(A220 接口形状不变,仅新增可选 ``cfg``
  参数,旧调用向后兼容)。
"""
from __future__ import annotations

import os
import sys
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
    "provider_rows",
    "ping_row",
    "agreement_rows",
    "cost_rows",
    "batch_cost_rows",
    "BATCH_COST_HEADERS_ZH",
    "weight_ci_rows",
    "weight_point_rows",
    "bayes_tracker_from_jsonl",
    "render_status_badge",
    "BADGE_OK",
    "BADGE_FAIL",
    "render",
    "main",
]

# ===========================================================================
# 纯逻辑层(无 streamlit 依赖;兄弟模块一律函数内惰性导入)
# ===========================================================================

#: 状态徽章文案(正常 / 异常)。
BADGE_OK: str = "✅ 正常"
BADGE_FAIL: str = "❌ 异常"

#: 一致性表中"非离群提供方"的固定中文注(离群注来自 A69 outliers[].note)。
AGREE_NORMAL_NOTE: str = "无明显系统性偏差"

#: provider_rows 每行的固定键(顺序即展示顺序;不含任何密钥字段,红线 17)。
_PROVIDER_ROW_KEYS: tuple[str, ...] = (
    "provider",
    "style",
    "default_model",
    "local",
    "key_configured",
    "base_url",
)


def _field(src: Any, name: str, default: Any = "") -> Any:
    """从鸭子类型对象或 dict 里取字段;取到 None 时回退默认值。"""
    if isinstance(src, dict):
        val = src.get(name, default)
    else:
        val = getattr(src, name, default)
    return default if val is None else val


def _cfg_str_map(cfg: Any, attr: str) -> dict[str, Any]:
    """安全读取 Config 上的某个 dict 字段(缺省 / 非 dict / cfg 为空一律空映射)。"""
    if cfg is None:
        return {}
    value = getattr(cfg, attr, None)
    return value if isinstance(value, dict) else {}


def _clean_str(value: Any) -> str:
    """规整为去空白字符串(非字符串 / None → 空串)。"""
    return str(value).strip() if isinstance(value, str) else ""


def _as_int(value: Any) -> int:
    """规整为非负整数展示值:数值(bool 除外)取整,其余按 0。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _as_float_or_none(value: Any, ndigits: int = 4) -> float | None:
    """规整为浮点(四舍五入);非数值(bool 除外)返回 ``None``。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), ndigits)


# ---------------------------------------------------------------------------
# provider_rows:提供方目录 → 总表行
# ---------------------------------------------------------------------------


def _fallback_key_map(cfg: Any, specs: dict[str, Any]) -> dict[str, bool]:
    """A70 未就位时的兜底:cfg.vlm_api_keys 非空 → key_envs 逐个环境变量。

    只产出布尔,绝不保留密钥本身(红线 17)。
    """
    cfg_keys = _cfg_str_map(cfg, "vlm_api_keys")
    out: dict[str, bool] = {}
    for name, spec in specs.items():
        raw = cfg_keys.get(name)
        if isinstance(raw, str) and raw.strip():
            out[str(name)] = True
            continue
        envs = getattr(spec, "key_envs", None) or []
        out[str(name)] = any(
            (os.environ.get(str(env)) or "").strip() for env in envs
        )
    return out


def _key_config_map(cfg: Any, specs: dict[str, Any]) -> dict[str, bool]:
    """逐提供方解析"密钥是否已配置"(布尔):惰性委托 A70 ``keys.configured``。

    A70 缺席 / 调用异常时降级为环境变量兜底;任何路径都只返回布尔表。
    """
    keys_mod: Any = None
    try:
        from netsentinel.security import keys as keys_mod  # noqa: PLC0415 惰性导入
    except Exception:  # noqa: BLE001 - A70 允许缺席(并行开发期)
        keys_mod = None
    fn = getattr(keys_mod, "configured", None) if keys_mod is not None else None
    if callable(fn):
        try:
            result = fn(cfg)
        except Exception:  # noqa: BLE001 - A70 接口异常时降级兜底,不硬依赖
            result = None
        if isinstance(result, dict):
            return {str(k): bool(v) for k, v in result.items()}
    return _fallback_key_map(cfg, specs)


def provider_rows(cfg: Any) -> list[dict]:
    """提供方目录(A61 ``providers.PROVIDERS`` 全部 20 家)→ 总表行。

    每行固定键 ``{provider, style, default_model, local, key_configured,
    base_url}``:

    - ``default_model`` / ``base_url``:优先取 ``cfg.vlm_provider_models`` /
      ``cfg.vlm_provider_base_urls`` 覆盖,其次目录提示值(空模型 = 必须显式
      指定,由 UI 层另行标注);
    - ``local``:目录 ``local`` 标记(ollama / vllm / lmstudio / xinference);
    - ``key_configured``:经 A70 ``keys.configured`` 布尔化,本地提供方天然
      为 ``False``(免密钥,UI 按 local 标记展示"免密钥");
    - **密钥绝不出现**:本函数不返回、不记录任何密钥字符串(红线 17)。

    目录模块未就位 / 为空时返回 ``[]``(页面显示中文提示,不抛错);
    ``cfg`` 为 ``None`` 时按无覆盖处理。

    示例::

        >>> rows = provider_rows(Config())   # doctest: +SKIP
        >>> rows[0]["provider"], rows[0]["key_configured"]
        ('glm', False)                       # 密钥只布尔化,绝不回显
    """
    providers_mod: Any = None
    try:
        from netsentinel.vision import providers as providers_mod  # noqa: PLC0415 惰性导入
    except Exception:  # noqa: BLE001 - A61 允许缺席(并行开发期)
        providers_mod = None
    specs = getattr(providers_mod, "PROVIDERS", None)
    if not isinstance(specs, dict) or not specs:
        return []

    model_overrides = _cfg_str_map(cfg, "vlm_provider_models")
    base_overrides = _cfg_str_map(cfg, "vlm_provider_base_urls")
    key_map = _key_config_map(cfg, specs)

    rows: list[dict] = []
    for name, spec in specs.items():
        provider = str(name)
        model = _clean_str(model_overrides.get(provider)) or _clean_str(
            getattr(spec, "default_model", "")
        )
        base_url = _clean_str(base_overrides.get(provider)) or _clean_str(
            getattr(spec, "base_url", "")
        )
        rows.append(
            {
                "provider": provider,
                "style": _clean_str(getattr(spec, "style", "")) or "未知",
                "default_model": model,
                "local": bool(getattr(spec, "local", False)),
                "key_configured": bool(key_map.get(provider, False)),
                "base_url": base_url,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# ping_row:vlmctl ping 输出 → 诊断行
# ---------------------------------------------------------------------------


def _ping_ok(data: dict) -> bool:
    """ping 结果的成败判定:显式 ``ok`` 优先,否则"解析成功且无错误"。"""
    if "ok" in data:
        return bool(data.get("ok"))
    if data.get("parsed") is True:
        return not _clean_str(data.get("error"))
    return False


def _ping_note(data: dict, ok: bool, latency_ms: float | None) -> str:
    """组装中文注:成功 → 链路可用 + 延迟 + 评分;失败 → 原因。"""
    if not ok:
        error = _clean_str(data.get("error"))
        if error:
            return error
        if data.get("parsed") is False:
            return "链路可达但返回不合规范,未能解析出有效评分"
        return "未完成调用(无延迟与解析结果,配置或链路异常)"
    parts = ["链路可用"]
    if latency_ms is not None:
        parts.append(f"延迟 {latency_ms:.1f} ms")
    prob = _as_float_or_none(data.get("nsfw_prob"))
    if prob is not None:
        parts.append(f"nsfw_prob {prob:.4f}")
    return ",".join(parts)


def ping_row(ping: dict | None) -> dict:
    """把 vlmctl ping(A67 ``_do_ping_call``)的输出 dict 规整为单行诊断结果。

    兼容多种形态(返回固定键 ``{provider, ok, latency_ms, model, note}``):

    - 成功:``{provider, latency_ms, model, nsfw_prob, parsed=True, error=""}``
      → ``ok=True``,中文注"链路可用,延迟 … ms,nsfw_prob …";
    - 解析失败:``parsed=False`` + ``error`` → ``ok=False``,注为错误原因;
    - 调用异常:CliError 转 ``{provider, error}``(无延迟/解析字段)→ ``ok=False``;
    - 显式 ``ok`` 布尔优先(本地探活等调用方自带结论时);
    - 空入参 / 形态不完整 → ``ok=False`` + 中文说明,绝不抛错。

    ``latency_ms`` 非数值(bool / 字符串等)时归为 ``None``;
    ``provider`` 取入参的 ``provider`` 键(由调用方把 spec 解析结果带入)。

    每次规整计入 ``telemetry.inc("providers_page.ping")``(V5 可观测性:
    ping 是人工显式诊断动作,打点后可在 snapshot 里看到发生了多少次)。

    示例::

        >>> ping_row({"provider": "glm", "latency_ms": 812.34,
        ...           "model": "glm-5.3-flash", "nsfw_prob": 0.0123,
        ...           "parsed": True, "error": ""})
        {'provider': 'glm', 'ok': True, 'latency_ms': 812.3,
         'model': 'glm-5.3-flash', 'note': '链路可用,延迟 812.3 ms,nsfw_prob 0.0123'}
    """
    telemetry.inc("providers_page.ping")
    data = ping if isinstance(ping, dict) else {}
    latency_ms = _as_float_or_none(data.get("latency_ms"), ndigits=1)
    ok = _ping_ok(data)
    return {
        "provider": _clean_str(data.get("provider")),
        "ok": ok,
        "latency_ms": latency_ms,
        "model": _clean_str(data.get("model")),
        "note": _ping_note(data, ok, latency_ms),
    }


# ---------------------------------------------------------------------------
# agreement_rows:A69 analyze 输出 → 一致性表格行
# ---------------------------------------------------------------------------


def agreement_rows(analysis: dict | None) -> list[dict]:
    """把 :func:`netsentinel.vision.provider_agreement.analyze` 的输出表格化。

    每行 ``{provider, bias, outlier, note}``:

    - ``bias``:该提供方相对共识的平均偏离(A69 ``bias``,正值偏松、负值偏严),
      非数值按 0 处理,保留 4 位小数;
    - ``outlier``:是否离群(出现在 A69 ``outliers`` 中,|bias| > 0.15);
    - ``note``:离群 → A69 的中文处置建议(系统性偏高/偏低,建议人工抽检),
      非离群 → :data:`AGREE_NORMAL_NOTE`;
    - 行序按 |bias| 降序(离群方排最前,便于人工优先处置),同值按名称排序。

    ``analysis`` 为空 / 无 ``bias`` / 形态不对时返回 ``[]``,不抛错。

    示例::

        >>> agreement_rows({"bias": {"glm": 0.3, "qwen": 0.01},
        ...                 "outliers": [{"provider": "glm",
        ...                               "note": "系统性偏高"}]})
        [{'provider': 'glm', 'bias': 0.3, 'outlier': True, 'note': '系统性偏高'},
         {'provider': 'qwen', 'bias': 0.01, 'outlier': False,
          'note': '无明显系统性偏差'}]
    """
    data = analysis if isinstance(analysis, dict) else {}
    bias = data.get("bias")
    if not isinstance(bias, dict) or not bias:
        return []

    outlier_notes: dict[str, str] = {}
    outliers = data.get("outliers")
    if isinstance(outliers, list):
        for item in outliers:
            if isinstance(item, dict):
                name = _clean_str(item.get("provider"))
                if name:
                    outlier_notes[name] = _clean_str(item.get("note"))

    rows: list[dict] = []
    for name, raw in bias.items():
        provider = _clean_str(name)
        if not provider:
            continue
        value = _as_float_or_none(raw)
        bias_value = value if value is not None else 0.0
        is_outlier = provider in outlier_notes
        rows.append(
            {
                "provider": provider,
                "bias": bias_value,
                "outlier": is_outlier,
                "note": outlier_notes.get(provider) or AGREE_NORMAL_NOTE,
            }
        )
    rows.sort(key=lambda r: (-abs(r["bias"]), r["provider"]))
    return rows


# ---------------------------------------------------------------------------
# cost_rows:A75 summary → 成本表格行
# ---------------------------------------------------------------------------


def cost_rows(summary: dict | None) -> list[dict]:
    """把 :class:`netsentinel.vision.cost_meter.CostMeter.summary` 的输出表格化。

    优先读 A75 实际形态 ``summary["by_provider"]``,同时容忍契约字面形态
    ``{provider: {calls, images, est_cost}}``(整个 dict 即映射)。

    每行 ``{provider, calls, images, est_cost, unpriced_calls}``:

    - ``calls`` / ``images`` / ``unpriced_calls``:规整为非负整数(非法值按 0);
    - ``est_cost``:估算成本(元,提示值口径),规整为两位小数浮点;
      **无价格提示(``None``)保持 ``None``**——绝不猜价(红线 18);
    - 行序按提供方名排序(与 A75 ``by_provider`` 的排序一致)。

    ``summary`` 为空 / 无有效条目时返回 ``[]``,不抛错。

    示例::

        >>> cost_rows({"by_provider": {"glm": {"calls": 3, "images": 10,
        ...                                    "est_cost": 0.05}}})
        [{'provider': 'glm', 'calls': 3, 'images': 10, 'est_cost': 0.05,
          'unpriced_calls': 0}]
    """
    data = summary if isinstance(summary, dict) else {}
    by_provider = data.get("by_provider")
    if not isinstance(by_provider, dict):
        by_provider = data  # 容忍字面契约形态:{provider: stats} 直接作为映射
    rows: list[dict] = []
    for name in sorted(by_provider):
        slot = by_provider.get(name)
        if not isinstance(slot, dict):
            continue
        provider = _clean_str(name)
        if not provider:
            continue
        est = _as_float_or_none(slot.get("est_cost"), ndigits=2)
        rows.append(
            {
                "provider": provider,
                "calls": _as_int(slot.get("calls")),
                "images": _as_int(slot.get("images")),
                "est_cost": est,
                "unpriced_calls": _as_int(slot.get("unpriced_calls")),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# batch_cost_rows(A232):runs/*/cost.jsonl 批次成本快照 → 批次行(零外呼)
# ---------------------------------------------------------------------------

#: 批次成本行字段 → 中文表头(页签⑥直接可用;键 = 行字段名)。
BATCH_COST_HEADERS_ZH: dict[str, str] = {
    "run_id": "批次",
    "models": "模型数",
    "calls": "总调用",
    "est_cost": "总费用(元)",
    "unpriced_calls": "无价格提示调用",
}


def batch_cost_rows(runs_dir: str | os.PathLike[str] | None) -> list[dict]:
    """扫 ``runs/*/cost.jsonl`` 批次成本快照(收官归集落盘)→ 展示行(只读)。

    输入目录通常为 ``<cfg.data_dir>/runs``(finishflow 批次成本归集步的
    落盘根);每个子目录的 ``cost.jsonl`` 逐行解析(meta / agg / totals):

    - **meta 段**定批次标识(``run_id``;缺失回退目录名);
    - ``models`` = 该快照 ``by=model`` 明细行数(**账本快照口径**:全局
      账本 vlm_cost.jsonl 在该次收官时的活跃模型数,非批次专属);
    - ``calls`` / ``est_cost`` / ``unpriced_calls`` = ``by=run_id`` 明细中
      该批那一行(**批次专属口径**:只统计本批收官标识的调用与费用,
      金额为提示值,以账单为准,红线 18);
    - 行序按目录名降序(``run-YYYYMMDD-HHMMSS`` 标识即最新批次在前)。

    容错(绝不抛错):目录缺席 / 不是目录 / 路径非法 → ``[]``;子目录无
    ``cost.jsonl`` → 跳过;损坏行(非 JSON / 非对象)跳过;该批无
    ``by=run_id`` 明细行(如零调用批)→ 数值按 0 诚实展示。

    示例::

        >>> batch_cost_rows("data/runs")[0]["run_id"]   # doctest: +SKIP
        'run-20261003-120000'
    """
    import json
    from pathlib import Path  # 局部导入,纯逻辑层顶层保持零 IO 依赖

    rows: list[dict] = []
    if runs_dir is None:
        return []
    try:
        root = Path(runs_dir)
        entries = sorted(root.iterdir(), key=lambda p: p.name, reverse=True)
    except (OSError, ValueError, TypeError):
        return []  # 目录缺席 / 不是目录 / 路径非法:空态回退(UI 给中文提示)

    for run_dir in entries:
        try:
            if not run_dir.is_dir():
                continue
        except OSError:  # pragma: no cover - 极端竞态(遍历中被删)不致命
            continue
        cost_path = run_dir / "cost.jsonl"
        try:
            if not cost_path.is_file():
                continue
        except OSError:  # pragma: no cover - 同上
            continue
        meta_run = ""
        models = 0
        calls = 0
        est_cost = 0.0
        unpriced = 0
        try:
            with cost_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except ValueError:
                        continue  # 坏行跳过,单行脏数据不拖垮整个快照
                    if not isinstance(obj, dict):
                        continue
                    kind = obj.get("kind")
                    if kind == "meta" and not meta_run:
                        meta_run = _clean_str(obj.get("run_id"))
                    elif kind == "agg" and obj.get("by") == "model":
                        models += 1
                    elif (
                        kind == "agg"
                        and obj.get("by") == "run_id"
                        and _clean_str(obj.get("run_id")) == (meta_run or run_dir.name)
                    ):
                        calls = _as_int(obj.get("calls"))
                        est = _as_float_or_none(obj.get("est_cost"), ndigits=2)
                        est_cost = est if est is not None else 0.0
                        unpriced = _as_int(obj.get("unpriced_calls"))
        except OSError:
            continue  # 读失败(占用/权限等):该批按缺席跳过,不影响其余批次
        rows.append(
            {
                "run_id": meta_run or run_dir.name,
                "models": models,
                "calls": calls,
                "est_cost": round(float(est_cost), 2),
                "unpriced_calls": unpriced,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# weight_ci_rows / weight_point_rows / bayes_tracker_from_jsonl(A220):
# V13 贝叶斯可靠性权重区间 → 展示行(只读消费,零外呼)
# ---------------------------------------------------------------------------


def _ci_pair(raw: Any) -> tuple[float | None, float | None]:
    """把 ``ci95`` 取值规整为 ``(lo, hi)``;非二元序列 / 非数值 → (None, None)。"""
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return _as_float_or_none(raw[0]), _as_float_or_none(raw[1])
    return None, None


def weight_ci_rows(source: Any) -> list[dict]:
    """把贝叶斯可靠性权重区间(V13 ``weights_with_ci``)规整为展示行。

    入参 ``source`` 两种形态(鸭子容错):

    - **注入 tracker**:任何带 ``weights_with_ci()`` 方法的对象
      (如 :class:`netsentinel.decision.reliability.BayesianReliabilityTracker`,
      测试可直接注入固定事件流的实例);调用异常时按数据缺席返回 ``[]``;
    - **结果 dict**:已经是 ``{member: {"weight", "mean", "ci95", "n_eff"}}``
      形态的映射(消费方已调用过 ``weights_with_ci`` 的场景)。

    每行固定键 ``{member, weight, ci_lo, ci_hi}``(中文表头"成员/权重/
    95%区间"):

    - ``weight``:归一权重点值(Σ=1),非数值的条目整行跳过;
    - ``ci_lo`` / ``ci_hi``:Beta 等尾 95% 置信区间(覆盖成员真实正确率,
      未归一);``ci95`` 缺席 / 形态不对时为 ``None``——**点值仍在,区间
      列置空(UI 显示"-"),即"零区间数据回退点值展示"**;
    - 行序按权重点值降序(同值按成员名排序,确定性)。

    空数据 / 形态不符一律 ``[]``(页面显示中文空态),绝不抛错。

    每次规整计入 ``telemetry.inc("providers_page.weight_ci")``。

    示例::

        >>> weight_ci_rows({"glm": {"weight": 0.7, "ci95": [0.5, 0.9],
        ...                         "n_eff": 20}})
        [{'member': 'glm', 'weight': 0.7, 'ci_lo': 0.5, 'ci_hi': 0.9}]
    """
    telemetry.inc("providers_page.weight_ci")
    data = source
    if not isinstance(data, dict):
        fn = getattr(source, "weights_with_ci", None)
        if not callable(fn):
            return []
        try:
            data = fn()
        except Exception:  # noqa: BLE001 - 数据缺席不让页面崩溃
            return []
    if not isinstance(data, dict) or not data:
        return []

    rows: list[dict] = []
    for name, slot in data.items():
        member = _clean_str(name)
        if not member or not isinstance(slot, dict):
            continue
        weight = _as_float_or_none(slot.get("weight"))
        if weight is None:
            continue  # 无点值的条目不产出任何展示信息
        ci_lo, ci_hi = _ci_pair(slot.get("ci95"))
        rows.append(
            {"member": member, "weight": weight, "ci_lo": ci_lo, "ci_hi": ci_hi}
        )
    rows.sort(key=lambda r: (-r["weight"], r["member"]))
    return rows


def weight_point_rows(source: Any) -> list[dict]:
    """把权重点值(``weights()`` 口径)规整为展示行(区间缺席的回退)。

    入参同样鸭子容错:带 ``weights()`` 方法的 tracker(贝叶斯
    ``weights()`` 恒为数值;Brier :class:`ReliabilityTracker.weights()`
    对样本不足成员返回 ``None``——**None 行跳过**,只展示统计上可信的
    成员),或已经是 ``{member: weight}`` 映射的 dict。

    每行固定键 ``{member, weight}``,按权重点值降序(同值按成员名排序);
    空 / 形态不符 / 调用异常一律 ``[]``,不抛错。

    示例::

        >>> weight_point_rows({"glm": 0.7, "stub": 0.3})
        [{'member': 'glm', 'weight': 0.7}, {'member': 'stub', 'weight': 0.3}]
    """
    data = source
    if not isinstance(data, dict):
        fn = getattr(source, "weights", None)
        if not callable(fn):
            return []
        try:
            data = fn()
        except Exception:  # noqa: BLE001 - 数据缺席不让页面崩溃
            return []
    if not isinstance(data, dict) or not data:
        return []

    rows: list[dict] = []
    for name, raw in data.items():
        member = _clean_str(name)
        weight = _as_float_or_none(raw)
        if not member or weight is None:
            continue
        rows.append({"member": member, "weight": weight})
    rows.sort(key=lambda r: (-r["weight"], r["member"]))
    return rows


def bayes_tracker_from_jsonl(
    jsonl_path: str | os.PathLike[str],
    cfg: Any = None,
) -> Any | None:
    """从本地反馈账本 ``reliability.jsonl`` 确定性重建贝叶斯 tracker(只读)。

    V13 :class:`BayesianReliabilityTracker` 为纯内存事件流设计,本函数把
    Brier 账本(``{provider, p, outcome[, ts]}`` 逐行 JSON,与
    :class:`ReliabilityTracker` 同一持久化格式)重放为 correctness 事件:

    - **correctness 换算口径(展示近似)**:提供方报告概率 ``p``,事实
      ``outcome``;按 0.5 二值口径判对错——``correct = (p >= 0.5) == outcome``
      (报 >= 0.5 且确为违规 = 对;报 >= 0.5 实为正常 = 错,依此类推);
    - **事件时刻(A230 升级,与 orchestrator 重放同口径)**:优先消费行内
      可选 ``ts`` 字段(Unix epoch 秒)——有 ts 的行按真实时刻重放;缺
      ts 的旧行回退 tracker 缺省滴答(上一条 + 1.0,首条 1.0,零墙钟)
      并经 ``telemetry.inc("providers_page.bayes.ts_fallback", N)`` 计数
      (回退条数可观测);``ts`` 存在但非法(布尔 / 非数字 / NaN / inf)
      的行按坏行跳过;
    - **遗忘半衰期(A230 升级)**:``half_life`` 对齐 ``cfg.bayes_half_life``
      (V14 一等字段,单位**天**,``getattr`` 防御缺省;天 → 秒换算
      ``× 86400`` 与行内 ts 同量纲)——``None`` / 缺字段 / 非正值 =
      关闭遗忘(展示近似口径保留:与 Brier"历史一视同仁"对齐,展示层
      不引入额外的遗忘假设);
    - 损坏 / 缺字段 / 值非法的行跳过(与 ReliabilityTracker._load 同款
      容错);文件缺失 / 无任何有效行返回 ``None``(调用方按数据缺席处理)。

    只读本机文件,绝不联网(零外呼);账本路径通常为
    ``cfg.data_dir/reliability.jsonl``;``cfg`` 缺省 ``None`` = 关闭遗忘的
    展示近似(向后兼容 A220 旧口径)。
    """
    import json
    import math
    from pathlib import Path  # 局部导入,纯逻辑层顶层保持零 IO 依赖

    from netsentinel.decision.reliability import (  # noqa: PLC0415 惰性导入
        BayesianReliabilityTracker,
    )

    path = Path(jsonl_path)
    if not path.is_file():
        return None
    # 半衰期对齐 cfg.bayes_half_life(单位天;getattr 防御缺省 → None =
    # 关闭遗忘的展示近似口径;天 → 秒换算,与行内 ts 的 epoch 秒同量纲)。
    hl_days = getattr(cfg, "bayes_half_life", None)
    half_life: float | None = None
    if (
        hl_days is not None
        and not isinstance(hl_days, bool)
        and isinstance(hl_days, (int, float))
        and math.isfinite(float(hl_days))
        and float(hl_days) > 0.0
    ):
        half_life = float(hl_days) * 86400.0
    tracker = BayesianReliabilityTracker(half_life=half_life)
    loaded = 0
    tick_rows = 0
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                    if not isinstance(obj, dict):
                        continue
                    provider = _clean_str(obj.get("provider"))
                    p_val = obj.get("p")
                    outcome = obj.get("outcome")
                    outcome_ok = isinstance(outcome, bool) or (
                        isinstance(outcome, int) and outcome in (0, 1)
                    )
                    if (
                        not provider
                        or isinstance(p_val, bool)
                        or not isinstance(p_val, (int, float))
                        or not outcome_ok
                    ):
                        continue
                    correct = (float(p_val) >= 0.5) == bool(outcome)
                    ts_raw = obj.get("ts")
                    if ts_raw is None:
                        when: float | None = None  # 缺 ts 旧行:滴答回退
                    else:
                        if (
                            isinstance(ts_raw, bool)
                            or not isinstance(ts_raw, (int, float))
                            or not math.isfinite(float(ts_raw))
                        ):
                            raise ValueError("ts 存在但非法")  # 有 ts 但坏 → 整行跳过
                        when = float(ts_raw)
                except (ValueError, TypeError):
                    continue  # 坏行跳过,单行脏数据不拖垮整个重建
                if when is None:
                    # 滴笔回退(tracker 缺省:上一条 +1,首条 1)并计数。
                    tracker.record(provider, correct)
                    tick_rows += 1
                else:
                    tracker.record(provider, correct, ts=when)
                loaded += 1
    except OSError:
        return None
    if tick_rows:
        telemetry.inc("providers_page.bayes.ts_fallback", tick_rows)
    return tracker if loaded else None


# ---------------------------------------------------------------------------
# render_status_badge:布尔 → 中文徽章
# ---------------------------------------------------------------------------


def render_status_badge(ok: bool) -> str:
    """布尔状态 → 中文徽章文案:真 → "✅ 正常",假 → "❌ 异常"。

    示例::

        >>> render_status_badge(True)
        '✅ 正常'
        >>> render_status_badge(0)
        '❌ 异常'
    """
    return BADGE_OK if bool(ok) else BADGE_FAIL


# ===========================================================================
# UI 层(以下代码仅在 streamlit 运行时执行;兄弟模块一律函数内导入)
# ===========================================================================

_PAGE_TITLE = "全平台视觉模型 · 提供方面板"
_MOTTO = "接入状态一目了然:平台配置 · 本地网关 · 跨平台一致性 · 成本提示;本页面不执行任何举报提交。"
_MANUAL_NOTICE = (
    "⚠️ ping / 本地探活均为**人工显式诊断动作**:仅在点击对应按钮后发起,"
    "常规浏览零外呼(V4 红线 20);密钥只显示 已配置/未配置,绝不回显(红线 17)。"
)

#: 本地网关探活结果在 session_state 里的键(未点刷新前不发起任何探测)。
_SS_LOCAL_STATUS = "providers_page_local_status"
#: 历次人工 ping 结果在 session_state 里的键(仅会话内展示,不落盘)。
_SS_PING_ROWS = "providers_page_ping_rows"

#: 一致性页签最多回看的复核条数(最近优先,IO 保护)。
_MAX_RECENT_ENTRIES: int = 50

#: 本地网关行展示模型名的上限(超出的以"等 N 个"收尾)。
_MAX_MODELS_SHOWN: int = 8

#: 本地提供方四家(UI 展示顺序与 A66 LOCAL_PROVIDERS 一致)。
_LOCAL_NAMES: tuple[str, ...] = ("ollama", "vllm", "lmstudio", "xinference")


def _key_state_cn(row: dict) -> str:
    """密钥状态列的中文展示:本地免密钥;云端只显示 已配置/未配置(红线 17)。"""
    if row["local"]:
        return "免密钥"
    return "已配置" if row["key_configured"] else "未配置"


def _render_catalog(cfg: Any) -> None:
    """页签①提供方总表:目录 20 家 + 筛选(本地 / 已配置密钥)。"""
    st.subheader("提供方总表")
    rows = provider_rows(cfg)
    if not rows:
        st.warning("提供方目录(netsentinel.vision.providers)未就位或为空,无法生成总表。")
        return

    n_local = sum(1 for r in rows if r["local"])
    n_keyed = sum(1 for r in rows if r["key_configured"])
    cols = st.columns(3)
    cols[0].metric("提供方总数", len(rows))
    cols[1].metric("本地网关", n_local)
    cols[2].metric("已配置密钥(云端)", n_keyed)

    only_local = st.checkbox("仅看本地提供方(数据不出本机)", value=False)
    only_keyed = st.checkbox("仅看已配置密钥的提供方", value=False)
    shown = [
        r
        for r in rows
        if (r["local"] or not only_local)
        and (r["key_configured"] or not only_keyed)
    ]
    if not shown:
        st.info("当前筛选条件下没有提供方。")
        return
    st.caption(f"共 {len(shown)} / {len(rows)} 家")
    st.table(
        [
            {
                "提供方": r["provider"],
                "方言": r["style"],
                "默认模型": r["default_model"] or "(必须显式指定模型)",
                "本地": "是(数据不出本机)" if r["local"] else "否",
                "密钥": _key_state_cn(r),
                "端点(提示值)": r["base_url"],
            }
            for r in shown
        ]
    )
    st.caption(
        "目录中的模型名 / 端点均为提示值,以各平台官方文档为准,上线前核验一次(红线 18);"
        "可用 cfg.vlm_provider_models / vlm_provider_base_urls 覆盖,覆盖后以本表展示为准;"
        "密钥列只显示 已配置/未配置,绝不回显密钥值(红线 17);本页签零外呼(红线 20)。"
    )


def _render_local_gateway(cfg: Any) -> None:
    """页签②本地网关:四家探活(仅点击"刷新"时调用)+ 人工 ping 面板。"""
    st.subheader("本地推理网关探活")
    st.caption(
        "对 Ollama / vLLM / LM Studio / Xinference 四家本地网关逐一探测"
        "(GET /v1/models,仅允许本机回环地址,数据不出本机,红线 16);"
        "结果不缓存,点击「刷新」按钮才发起(人工显式诊断动作,红线 20)。"
    )
    if st.button("刷新(探测四家本地网关)", type="primary"):
        try:
            from netsentinel.vision.local_gateway import local_status  # 兄弟模块函数内导入
        except Exception as exc:  # noqa: BLE001 - A66 允许缺席
            st.error(f"本地网关模块(netsentinel.vision.local_gateway)未就位:{exc}")
        else:
            st.session_state[_SS_LOCAL_STATUS] = local_status(cfg)

    status = st.session_state.get(_SS_LOCAL_STATUS)
    if status is None:
        st.info("尚未探测。点击「刷新」按钮发起一次人工显式诊断(不点不探,常规浏览零外呼)。")
    elif not isinstance(status, dict) or not status:
        st.warning("探活结果为空(四家本地网关均无返回)。")
    else:
        for name in _LOCAL_NAMES:
            entry = status.get(name)
            if not isinstance(entry, dict):
                continue
            badge = render_status_badge(entry.get("ok"))
            models = [str(m) for m in (entry.get("models") or []) if str(m)]
            if len(models) > _MAX_MODELS_SHOWN:
                model_text = "、".join(models[:_MAX_MODELS_SHOWN]) + f" 等 {len(models)} 个"
            else:
                model_text = "、".join(models) if models else "(无模型)"
            error = _clean_str(entry.get("error"))
            line = f"- {badge} **{name}**(`{entry.get('base_url', '')}`):{model_text}"
            if error:
                line += f" —— {error}"
            st.markdown(line)
        st.caption("探活仅访问本机回环地址;模型清单以本地服务实际加载为准。")

    # ---- 人工 ping(唯一允许外呼的诊断动作,复用 A67 vlmctl 唯一外呼路径)----
    st.divider()
    st.subheader("人工 ping 诊断(云端 / 本地通用)")
    st.caption(
        "对指定提供方[:模型] 发一次真实评分调用(1×1 测试图),复用 vlmctl 的唯一"
        "外呼路径:先计入 vlm_cache 预算再外呼(红线 19/20);结果仅在本会话展示,不落盘。"
    )
    default_spec = _clean_str(_field(cfg, "vlm_provider", "glm")) or "glm"
    spec = st.text_input(
        "目标(提供方 或 提供方:模型)",
        value=default_spec,
        help="如 qwen:qwen-vl-max、anthropic、ollama:llava;模型名以官方文档为准。",
    )
    confirmed = st.checkbox(
        "我知悉:点击后将发起一次真实外呼并计入每日预算", value=False
    )
    if st.button("发起 ping", disabled=not confirmed):
        target = spec.strip()
        if not target:
            st.warning("请先填写目标,如 glm 或 openai:gpt-4o-mini。")
        else:
            provider = target.split(":", 1)[0].strip()
            try:
                from netsentinel.vision import vlmctl  # 兄弟模块函数内导入
            except Exception as exc:  # noqa: BLE001 - A67 允许缺席
                st.error(f"诊断 CLI 模块(netsentinel.vision.vlmctl)未就位:{exc}")
            else:
                do_ping = getattr(vlmctl, "_do_ping_call", None)
                if not callable(do_ping):
                    st.error("vlmctl 缺少 ping 调用入口(_do_ping_call),无法发起诊断。")
                else:
                    try:
                        raw = dict(do_ping(cfg, target, None))
                        raw.setdefault("provider", provider)
                    except Exception as exc:  # noqa: BLE001 - CLI 中文错误直接展示
                        raw = {"provider": provider, "error": str(exc)}
                    st.session_state.setdefault(_SS_PING_ROWS, []).insert(
                        0, ping_row(raw)
                    )
                    st.rerun()
    pings = st.session_state.get(_SS_PING_ROWS) or []
    if not pings:
        st.caption("本会话尚未发起过 ping(保持零外呼)。")
    else:
        st.table(
            [
                {
                    "目标提供方": p["provider"],
                    "状态": render_status_badge(p["ok"]),
                    "延迟(ms)": "-" if p["latency_ms"] is None else f"{p['latency_ms']:.1f}",
                    "模型回显": p["model"] or "(未回显)",
                    "诊断说明": p["note"],
                }
                for p in pings
            ]
        )


def _image_scores_from_report(report: Any) -> list[Any]:
    """把证据包 manifest 报告里的 ``image_scores`` 还原为 ImageScore 对象列表。

    manifest 存的是 :meth:`ImageScore.as_dict` 形态(``image`` 直接是路径
    字符串);缺路径 / 缺模型 / 概率非数值的行跳过。A69 analyze 需要
    真实对象(``image.path`` / ``model`` / ``nsfw_prob``),故做本转换。
    """
    raw_scores = report.get("image_scores") if isinstance(report, dict) else None
    if not isinstance(raw_scores, list):
        return []
    from netsentinel.contracts import ImageEvidence, ImageScore  # 兄弟模块函数内导入

    scores: list[Any] = []
    for item in raw_scores:
        if not isinstance(item, dict):
            continue
        path = _clean_str(item.get("image"))
        model = _clean_str(item.get("model"))
        prob = item.get("nsfw_prob")
        if not path or not model or isinstance(prob, bool) or not isinstance(
            prob, (int, float)
        ):
            continue
        raw_extra = item.get("scores")
        scores.append(
            ImageScore(
                image=ImageEvidence(path=path, url="", source_page=""),
                model=model,
                scores=dict(raw_extra) if isinstance(raw_extra, dict) else {},
                nsfw_prob=float(prob),
            )
        )
    return scores


def _recent_scores(cfg: Any) -> tuple[list[Any], str]:
    """从**最近**的证据包读回 image_score 对象列表;返回 ``(评分, 证据包路径)``。

    按复核条目从新到旧回看(上限 :data:`_MAX_RECENT_ENTRIES` 条),取第一个
    真正包含 ``image_scores`` 的证据包(manifest 布局复用 A30
    ``load_report_dict`` 的定位逻辑);全部为空时返回 ``([], "")``。
    """
    from webui.app import load_report_dict  # 兄弟模块函数内导入

    try:
        from netsentinel.decision.review_queue import ReviewQueue  # 兄弟模块函数内导入
    except Exception:  # noqa: BLE001 - 队列不可用时无从定位证据包
        return [], ""
    try:
        entries = ReviewQueue(_clean_str(_field(cfg, "db_path", "data/review_queue.db"))).list(None)
    except Exception:  # noqa: BLE001 - 库文件缺失 / 损坏不让页面崩溃
        return [], ""
    for entry in reversed(entries[-_MAX_RECENT_ENTRIES:]):  # 最近优先
        report = load_report_dict(_clean_str(_field(entry, "evidence_zip", "")))
        scores = _image_scores_from_report(report)
        if scores:
            return scores, _clean_str(_field(entry, "evidence_zip", ""))
    return [], ""


def _agreement_table(rows: list[dict]) -> Any:
    """一致性行 → 离群高亮表格(pandas Styler;离群行红底,失败时退纯表格)。"""
    import pandas as pd

    frame = pd.DataFrame(
        [
            {
                "提供方": r["provider"],
                "平均偏差(正=偏松)": f"{r['bias']:+.4f}",
                "是否离群": "⚠️ 离群" if r["outlier"] else "正常",
                "中文注": r["note"],
            }
            for r in rows
        ]
    )

    def _highlight_outlier(row: Any) -> list[str]:
        return [
            "background-color: #ffc7ce; color: #9c0006"
            if row["是否离群"] == "⚠️ 离群"
            else ""
        ] * len(row)

    styler = frame.style.apply(_highlight_outlier, axis=1)
    return styler.hide(axis="index")


def _render_agreement(cfg: Any) -> None:
    """页签③跨平台一致性:最近证据包 image_scores → A69 analyze → 表格(离群高亮)。"""
    st.subheader("跨平台一致性")
    st.caption(
        "从最近证据包读回 image_scores,经 provider_agreement.analyze 计算:"
        "共识 = 逐图均值;平均偏差 = 各提供方相对共识的均值(正=偏松,负=偏严);"
        "|偏差| > 0.15 判为离群(红色高亮),建议人工抽检其评分样本。"
    )
    try:
        from netsentinel.vision import provider_agreement  # 兄弟模块函数内导入
    except Exception as exc:  # noqa: BLE001 - A69 允许缺席
        st.warning(f"一致性分析模块(netsentinel.vision.provider_agreement)未就位:{exc}")
        return

    scores, source_zip = _recent_scores(cfg)
    if not scores:
        st.info(
            "暂无可对比的多平台评分(最近证据包未包含 image_scores,或复核队列为空);"
            "跑一次多成员 ensemble 扫描后即可在此对比各家口径。"
        )
        return

    analysis = provider_agreement.analyze(scores)
    rows = agreement_rows(analysis)
    if not rows:
        st.info("最近证据包的评分只来自单一提供方,没有跨平台一致性可计算。")
        st.caption(f"数据来源:最近证据包 `{source_zip or '(路径未知)'}`。")
        return

    n_outliers = sum(1 for r in rows if r["outlier"])
    spread = analysis.get("per_image_spread") or []
    cols = st.columns(3)
    cols[0].metric("参与提供方", len(rows))
    cols[1].metric("离群提供方", n_outliers)
    cols[2].metric("有效图片", len(spread))
    try:
        st.dataframe(_agreement_table(rows), use_container_width=True)
    except Exception:  # noqa: BLE001 - pandas 版本差异等退化为纯表格
        st.table(
            [
                {
                    "提供方": r["provider"],
                    "平均偏差": f"{r['bias']:+.4f}",
                    "是否离群": "⚠️ 离群" if r["outlier"] else "正常",
                    "中文注": r["note"],
                }
                for r in rows
            ]
        )
    st.caption(f"数据来源:最近证据包 `{source_zip or '(路径未知)'}`;行序按 |平均偏差| 降序。")

    if spread:
        st.markdown("**争议最大的图片(同图各提供方分差,降序)**")
        st.table(
            [
                {
                    "图片": str(item.get("image", "")),
                    "最高分": f"{float(item.get('max', 0.0)):.4f}",
                    "最低分": f"{float(item.get('min', 0.0)):.4f}",
                    "分差": f"{float(item.get('spread', 0.0)):.4f}",
                }
                for item in spread[:5]
                if isinstance(item, dict)
            ]
        )


def _render_cost(cfg: Any) -> None:
    """页签④成本:A75 CostMeter 汇总表(提示值口径,以账单为准)。"""
    st.subheader("调用成本(提示值)")
    try:
        from netsentinel.vision.cost_meter import CostMeter  # 兄弟模块函数内导入
    except Exception as exc:  # noqa: BLE001 - A75 允许缺席
        st.warning(f"成本计量模块(netsentinel.vision.cost_meter,A75)未就位:{exc}")
        return

    from pathlib import Path

    state_path = Path(_clean_str(_field(cfg, "data_dir", "data"))) / "vlm_cost.jsonl"
    summary = CostMeter(state_path).summary()
    rows = cost_rows(summary)
    cols = st.columns(2)
    cols[0].metric("总估算成本(元,提示值)", f"{float(summary.get('total_est', 0.0)):.2f}")
    cols[1].metric("无价格提示的调用", int(summary.get("unpriced_calls", 0)))
    if not rows:
        st.info(f"成本账本暂无记录(账本文件:`{state_path}`);产生真实 VLM 调用后自动累积。")
        return
    st.table(
        [
            {
                "提供方": r["provider"],
                "调用次数": r["calls"],
                "图片数": r["images"],
                "估算成本(元)": "-"
                if r["est_cost"] is None
                else f"{r['est_cost']:.2f}",
                "无价格提示调用": r["unpriced_calls"],
            }
            for r in rows
        ]
    )
    st.caption(
        f"账本文件:`{state_path}`;"
        "单价全部为提示值(人民币元 / 每千次图像调用),以各平台账单为准(红线 18);"
        "本地提供方按本地算力记 0(红线 16);本表与 vlm_cache 每日调用预算互补(红线 19)。"
    )


def _render_batch_cost(cfg: Any) -> None:
    """页签⑥批次成本:runs/*/cost.jsonl 收官快照 → 批次费用表(只读,零外呼)。

    数据链:``cfg.data_dir/runs/<批次>/cost.jsonl``(finishflow 批次成本
    归集步落盘)→ :func:`batch_cost_rows` 展示行 → 中文表
    (:data:`BATCH_COST_HEADERS_ZH`)。目录缺席 / 无快照 → 中文空态提示。
    """
    from pathlib import Path

    st.subheader("批次成本(收官快照)")
    runs_dir = Path(_clean_str(_field(cfg, "data_dir", "data"))) / "runs"
    rows = batch_cost_rows(runs_dir)
    if not rows:
        st.info(
            f"暂无批次成本快照(`{runs_dir}` 目录缺席或为空)——批量收官流程"
            "(python -m netsentinel.finishflow --input 清单)完成批次成本归集后,"
            "这里会按批次展示模型数、调用次数与费用(提示值口径,以账单为准);"
            "本页签只读本机快照文件,零外呼。"
        )
        return
    st.table(
        [
            {
                BATCH_COST_HEADERS_ZH["run_id"]: r["run_id"],
                BATCH_COST_HEADERS_ZH["models"]: r["models"],
                BATCH_COST_HEADERS_ZH["calls"]: r["calls"],
                BATCH_COST_HEADERS_ZH["est_cost"]: f"{r['est_cost']:.2f}",
                BATCH_COST_HEADERS_ZH["unpriced_calls"]: r["unpriced_calls"],
            }
            for r in rows
        ]
    )
    st.caption(
        f"数据来源:`{runs_dir}/<批次>/cost.jsonl`(收官成本归集快照);"
        "模型数 = 该快照 by=model 明细行数(账本快照口径,非批次专属);"
        "总调用/总费用/无价格提示 = 该批次(by=run_id)聚合(批次专属口径);"
        "金额为提示值,以各平台账单为准(红线 18);行序按目录名降序"
        "(时间戳标识最新在前);本页签零外呼。"
    )


def _render_weight_ci(cfg: Any) -> None:
    """页签⑤权重区间:本地反馈 → 贝叶斯权重点值 + Beta 95% 区间(零外呼)。

    数据链:``cfg.data_dir/reliability.jsonl``(Brier 反馈账本)→
    :func:`bayes_tracker_from_jsonl` 确定性重建事件流 → V13
    ``weights_with_ci`` → :func:`weight_ci_rows` 展示行。三层缺席降级:

    1. 有区间数据 → 中文表"成员 / 权重 / 95%区间";
    2. 零区间数据但账本可读出 Brier 点值 → 回退点值表(既有口径不受影响);
    3. 全部缺席 → 中文空态提示(完成人工复核反馈后自动出现)。
    """
    st.subheader("贝叶斯可靠性权重区间(V13,只读)")
    st.caption(
        "数据只来自本地复核反馈账本 reliability.jsonl(零外呼);权重点值 = "
        "贝叶斯分层后验均值归一(Σ=1,冷启动成员向全局池收缩);95% 区间 = "
        "Beta 等尾置信区间,覆盖成员真实正确率(未归一口径);换算与遗忘口径"
        "见 bayes_tracker_from_jsonl 说明(0.5 二值判对错;行内 ts 优先消费、"
        "半衰期对齐 cfg.bayes_half_life,缺省时关闭遗忘的展示近似)。"
    )
    from pathlib import Path

    jsonl_path = Path(_clean_str(_field(cfg, "data_dir", "data"))) / "reliability.jsonl"
    rows = weight_ci_rows(bayes_tracker_from_jsonl(str(jsonl_path), cfg))
    if rows:
        st.table(
            [
                {
                    "成员": r["member"],
                    "权重": f"{r['weight']:.4f}",
                    "95%区间": "-"
                    if r["ci_lo"] is None or r["ci_hi"] is None
                    else f"[{r['ci_lo']:.4f}, {r['ci_hi']:.4f}]",
                }
                for r in rows
            ]
        )
        if all(r["ci_lo"] is None for r in rows):
            st.caption("本次数据未携带置信区间,已回退点值展示(区间列显示\"-\")。")
        st.caption(
            f"数据来源:`{jsonl_path}`;行序按权重点值降序;"
            "权重是各成员在可靠性别融合中的话语权占比(Σ=1),不是评分本身。"
        )
        return

    # 零区间数据 → 回退 Brier 点值展示(既有口径),仍无 → 中文空态
    try:
        from netsentinel.decision.reliability import ReliabilityTracker  # 惰性导入
    except Exception:  # noqa: BLE001 - V7 允许缺席
        st.info("可靠性模块(netsentinel.decision.reliability)未就位,无法展示权重区间。")
        return
    try:
        point_rows = weight_point_rows(ReliabilityTracker(str(jsonl_path)))
    except Exception:  # noqa: BLE001 - 账本缺失 / 损坏不让页面崩溃
        point_rows = []
    if point_rows:
        st.table([{"成员": r["member"], "权重": f"{r['weight']:.4f}"} for r in point_rows])
        st.caption(
            f"贝叶斯区间数据缺席,已回退 Brier 反比点值展示(数据来源:`{jsonl_path}`;"
            "样本不足的成员不展示;既有口径不受影响)。"
        )
    else:
        st.info(
            f"尚无本地可靠性反馈记录(`{jsonl_path}`)——完成人工复核反馈后,"
            "这里会展示各成员的贝叶斯权重点值与 Beta 95% 置信区间;"
            "本页签零外呼,只读本机反馈账本。"
        )


def render() -> None:
    """提供方面板主界面(streamlit 脚本入口调用的渲染函数)。"""
    if not _HAS_ST:  # pragma: no cover - main() 已拦截,防御性兜底
        raise RuntimeError(
            "streamlit 不可用,无法渲染提供方面板;请先安装 python -m pip install -e \".[ui]\""
        )

    from netsentinel.config import load_config  # 兄弟模块函数内导入
    from webui.app import apply_data_dir

    st.set_page_config(page_title=_PAGE_TITLE, page_icon="🛰️", layout="wide")
    st.title(f"🛰️ {_PAGE_TITLE}")
    st.caption(_MOTTO)
    st.warning(_MANUAL_NOTICE)  # 页面顶部声明:ping/探活为人工显式诊断动作

    cfg = apply_data_dir(load_config())
    tabs = st.tabs(
        ["提供方总表", "本地网关", "跨平台一致性", "成本", "权重区间", "批次成本"]
    )
    tab_catalog, tab_local, tab_agree, tab_cost, tab_weight, tab_batch = tabs
    with tab_catalog:
        _render_catalog(cfg)
    with tab_local:
        _render_local_gateway(cfg)
    with tab_agree:
        _render_agreement(cfg)
    with tab_cost:
        _render_cost(cfg)
    with tab_weight:
        _render_weight_ci(cfg)
    with tab_batch:
        _render_batch_cost(cfg)


def main() -> int:
    """脚本入口:缺 streamlit 时打印中文安装提示并返回退出码 1。"""
    if not _HAS_ST:
        print(
            "未安装 Streamlit,提供方面板无法启动。\n"
            "请先执行:python -m pip install -e \".[ui]\"\n"
            "然后运行:streamlit run webui/providers_page.py",
            file=sys.stderr,
        )
        return 1
    render()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
