"""flow 入口共用纯辅助(batchflow / finishflow 双拷贝收敛,V?)。

四个函数(``as_mapping`` / ``merge_resumed_items`` / ``trace_id_of`` /
``site_dirname``)此前在 :mod:`netsentinel.batchflow` 与
:mod:`netsentinel.finishflow` 各有一份逐字拷贝(merge 略有口径差异,
经 ``require_fields`` 开关保留两套行为);现提取到本单一实现,两个 flow
改调用。

**不动的部分**:``_load`` 惰性导入辅助在 ``__main__`` / ``batchflow`` /
``finishflow`` / ``sessionflow`` 四处各有一份重复——这是**入口独立性的
设计**(每个 CLI 入口零兄弟依赖、可单独运行),故**不**收敛;本模块为
共用辅助自带一份私有 ``_lazy_attr``(口径与各入口的 ``_load`` 一致)。

只用标准库;纯本地、确定性;不触碰任何提交 / 频控语义。
"""
from __future__ import annotations

import importlib
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

__all__ = ["as_mapping", "merge_resumed_items", "site_dirname", "trace_id_of"]


def _lazy_attr(module_name: str, attr: str) -> Any:
    """惰性导入 ``module_name.attr``;失败返回 ``None``(调用方按缺席降级)。

    与各 CLI 入口的 ``_load`` 同口径,但**不**抛 RuntimeError——共用辅助
    (如 :func:`site_dirname` 的 canonical 键降级)按"模块缺席走回退"设计;
    入口级的 ``_load`` 重复是刻意设计(入口独立性),见模块 docstring。
    """
    try:
        module = importlib.import_module(module_name)
        return getattr(module, attr)
    except (ImportError, AttributeError):
        return None


def as_mapping(row: Any) -> dict[str, Any]:
    """把续批行(A113 resume 返回的 dict/sqlite Row/对象)宽容转成 dict。

    兼容映射 / sqlite3.Row(有 ``keys()``)/ dataclass 与普通对象
    (``__dict__``);完全取不到字段时返回空 dict(由调用方按缺字段报错)。
    """
    if isinstance(row, Mapping):
        return dict(row)
    keys = getattr(row, "keys", None)
    if callable(keys):
        try:
            return {str(k): row[k] for k in keys()}
        except Exception:  # noqa: BLE001 - 鸭子行取值失败按空映射处理
            return {}
    state = getattr(row, "__dict__", None)
    if isinstance(state, dict):
        return dict(state)
    return {}


def merge_resumed_items(
    rows: list[Any],
    ready_items: list[dict[str, Any]],
    *,
    portal_default: str,
    require_fields: bool = False,
) -> list[dict[str, Any]]:
    """续批行 ←→ 就绪清单按 ``entry_id`` 合并补齐执行细节(两 flow 共用)。

    - 就绪条目按 ``entry_id`` 建索引;续批行优先保留自身字段,就绪条目
      补齐/覆盖非空的 ``portal`` / ``site_url`` / ``evidence_zip`` 等;
    - ``portal`` 双缺时补 ``portal_default``(各 flow 的 :data:`PORTAL_DEFAULT`);
    - ``require_fields``(**batchflow 口径**,缺省 False = finishflow 口径):
      ``True`` 时额外 ``setdefault`` ``entry_id`` / ``group_name`` 并对合并后
      仍缺 ``entry_id`` / ``group_name`` / ``portal`` 的条目抛中文
      :class:`ValueError` 指明条目(run_batch 前置校验的前移,报错更贴近
      数据来源);``False`` 时只合并补齐,不做缺字段校验。
    """
    ready_by_id: dict[Any, dict[str, Any]] = {}
    for item in ready_items:
        eid = item.get("entry_id")
        if eid is not None:
            ready_by_id[eid] = dict(item)
    merged: list[dict[str, Any]] = []
    for row in rows:
        item = as_mapping(row)
        base = ready_by_id.get(item.get("entry_id"), {})
        for key, value in base.items():
            if value not in (None, ""):
                item[key] = value
        if require_fields:
            item.setdefault("entry_id", base.get("entry_id"))
            item.setdefault("group_name", base.get("group_name"))
        if not item.get("portal"):
            item["portal"] = portal_default
        if require_fields:
            missing = [
                key
                for key in ("entry_id", "group_name", "portal")
                if item.get(key) in (None, "")
            ]
            if missing:
                raise ValueError(
                    "批次未完条目缺少必填字段:"
                    f"{'、'.join(missing)}(条目:{item or '(空)'})"
                )
        merged.append(item)
    return merged


def trace_id_of(report: Any) -> str:
    """鸭子取 ``report.intel["trace_id"]``;无 intel / 非映射 / 无键 → 空串。

    trace_id 是 16-hex 非内容字段(A198 契约),不涉站点内容,可安全计数。
    """
    intel = getattr(report, "intel", None)
    if isinstance(intel, Mapping):
        tid = intel.get("trace_id")
        if isinstance(tid, str) and tid:
            return tid
    return ""


def site_dirname(url: str, report: Any) -> str:
    """站点目录名:canonical 键优先,回退主机名,再退安全化整串(纯本地、确定)。

    - 优先 ``intel.canonical.canonical_key``(项目既定站点同一性口径,
      ``www.`` 归并/IP 直连原样),模块缺席时降级 ``urlsplit().hostname``;
    - 任何来源都经 ``[^0-9A-Za-z._-]`` 安全化(IPv6 冒号等目录非法字符
      归一为 ``_``),保证跨平台可作目录名;全部解析失败回退 ``"site"``。
    """
    candidates = [str(url or ""), str(getattr(report, "site_url", "") or "")]
    canonical_key = _lazy_attr("netsentinel.intel.canonical", "canonical_key")
    if canonical_key is not None:
        for candidate in candidates:
            key = str(canonical_key(candidate) or "")
            if key:
                return re.sub(r"[^0-9A-Za-z._-]+", "_", key)
    for candidate in candidates:
        try:
            host = (urlsplit(candidate).hostname or "").strip().lower()
        except ValueError:
            host = ""
        if host:
            return re.sub(r"[^0-9A-Za-z._-]+", "_", host)
    for candidate in candidates:
        slug = re.sub(r"[^0-9A-Za-z._-]+", "_", candidate).strip("._-")
        if slug:
            return slug[:64]
    return "site"
