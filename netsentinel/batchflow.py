# -*- coding: utf-8 -*-
"""批量总流程 CLI(NetSentinel V6 · A119)。

把 V6 批量案件流水线的五段 **筛选 → 归组 → 声明 → 批量 → 续批** 串成一条
命令。本模块只做"顺序编排 + 中文提示",不引入任何新的自动决策,更不触碰
提交安全语义:

- :func:`run_flow`(筛选 + 归组):``--input`` 清单 → A107 ``load_bulk`` 加载
  → A107 ``plan_scan`` 规划(canonical 去重 + 指纹未变跳过)→ A108 ``batch_scan``
  并发扫描 → A108 ``group_and_enqueue`` 归组入列 → **V12/A231:trace 收官
  消费**(``cfg.trace_enabled`` 开启的扫描在 ``report.intel["trace_id"]`` 携带
  trace 号,批次收官段消费各站 trace_id——(a) 汇总为批级 ``trace_ids``
  清单写入批次结果;(b) 逐站 span 树经 A198 ``telemetry_trace.export_trace_json``
  落盘 ``data/runs/<站点标识>/trace.json``(手法对齐 A210 finishflow);
  (c) 批次末 ``drain_to_sink`` 把注册表存量转存 ``cfg.audit_path`` 审计
  JSONL 后清空(补 MAX_TRACES=64 逐出的长跑窗口不足);开关关闭零行为,
  任何失败只中文 warning + 计数 ``batchflow.trace_export.failed``、
  **绝不中断批次**)→ 打印中文摘要
  (合法/拒绝/待扫/跳过/组数/入列数/最大组)→ 打印"下一步"指引
  (TUI 逐组声明 → batchflow 续批)。**run_flow 永不提交**——举报仍须
  人工在 TUI 完成逐组声明(红线 25)后,经 :func:`resume_batch` →
  A112 ``run_batch`` 逐条人工门执行(红线 24)。
- :func:`resume_batch`(批量 + 续批):A110 ``ready_entries`` 取待批量条目 →
  A113 ``BatchState.resume`` 查批次未完条目(有 → 续批;无 → 按 ready 新建
  批次)→ A112 ``run_batch`` 顺序逐条执行(注入参数名按对接约定为
  ``plan_12377=`` / ``plan_shdf=``,状态器以 ``state=st.bound(batch_id)``
  绑定批次)。``dry_run`` 缺省取 ``cfg.dry_run_default``;真实执行必须
  **显式** ``dry_run=False``。
- :func:`main`:``--input PATH`` / ``--config PATH`` / ``--dry-run``|``--exec``
  (互斥;``--exec`` 仅 ``--resume`` 续批模式有效,才是真实执行的唯一入口)/
  ``--resume 批次号``;``python -m netsentinel.batchflow`` 入口。

安全红线(CONTRACTS-V6 §0,违反即缺陷):

24. **批量举报仍是逐条人工门**:本模块对 ``executor`` 原样透传、绝不包装,
    不存在任何把 ``auto_confirm`` 置 ``True`` 或绕过 A112 人工门的代码路径;
    ``run_batch`` 内部恒以 ``auto_confirm=False`` 调执行器。
25. **批量确认声明(留痕)**:进入批量的条目一律来自 A110 ``ready_entries``
    (已声明组门控);run_flow 阶段不提交、只指引 TUI 声明。
26. **批量模式频控不得放宽**:频控与额度完全由 A112 ``run_batch`` 收口,
    本模块不提供任何放宽参数。

兄弟模块(A107/A108/A110/A112/A113)全部**惰性导入 + 鸭子注入**,未就位时
抛中文 :class:`RuntimeError` 指明缺失模块;测试可全量离线注入、零外呼。

用法示例(fake 注入,离线)::

    from netsentinel.contracts import Config

    result = run_flow("leads.txt", cfg, scanner=fake_scan, grouping=fake_group)
    # result == {"groups": 3, "enqueued": 3, "to_scan": [...], "summary": {...}}

    summary = resume_batch(7, cfg, ready=fake_ready, state=fake_state,
                           submit=fake_run_batch, dry_run=True)
    # summary == {"submitted": n, "failed": n, ..., "batch_id": 7}
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import pathlib
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from netsentinel import telemetry
from netsentinel.contracts import Config

__all__ = ["PORTAL_DEFAULT", "main", "resume_batch", "run_flow"]

logger = logging.getLogger(__name__)

#: 待批量条目缺 portal 字段时的缺省门户(与 A110 DEFAULT_PORTAL 对齐)
PORTAL_DEFAULT: str = "12377"

#: 新建批次的 note 前缀(A113 batches.note;仅流程标注,不含站点内容)
_NEW_BATCH_NOTE: str = "[batchflow] 就绪清单新建批次(来源:ready_entries)"

# 遥测指标名(只记名称与数字,不记站点内容)
_METRIC_FLOW = "batchflow.flow"
_METRIC_FLOW_EMPTY = "batchflow.flow.empty"
_METRIC_RESUME = "batchflow.resume"
_METRIC_RESUME_NEW = "batchflow.resume.new_batch"
_METRIC_RESUME_CONTINUE = "batchflow.resume.continue"
_METRIC_RESUME_IDLE = "batchflow.resume.idle"
# trace 收官消费(V12 接线收口,A231):成功落盘 / 长窗口转存 / 失败(绝不中断批次)
_METRIC_TRACE_SAVED = "batchflow.trace_export.saved"
_METRIC_TRACE_DRAINED = "batchflow.trace_export.drained"
_METRIC_TRACE_FAILED = "batchflow.trace_export.failed"


# ---------------------------------------------------------------------------
# 惰性导入辅助(兄弟模块并行开发中,未就位抛中文 RuntimeError)
# ---------------------------------------------------------------------------
def _load(module_name: str, attr: str) -> Any:
    """惰性导入 ``module_name.attr``;ImportError/缺属性 → 中文 RuntimeError。"""
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(f"模块 {module_name} 未就位:{exc}") from exc
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise RuntimeError(f"模块 {module_name} 未提供 {attr}:{exc}") from exc


# ---------------------------------------------------------------------------
# 纯辅助:鸭子取值
# ---------------------------------------------------------------------------
def _group_size(group: Any) -> int:
    """组规模:优先 ``entry_ids`` 数量,缺属性回退 ``site_urls`` 数量,再退 1。"""
    entry_ids = getattr(group, "entry_ids", None)
    if entry_ids:
        return len(entry_ids)
    urls = getattr(group, "site_urls", None)
    if urls:
        return len(urls)
    return 1


def _as_mapping(row: Any) -> dict[str, Any]:
    """把批次未完条目(A113 resume 的返回行)宽容转成 dict。

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


def _merge_resumed_items(
    rows: list[Any], ready_items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """批次未完条目 ←→ 就绪清单 合并成 run_batch 的 items。

    - 就绪条目按 ``entry_id`` 建索引;批次行优先保留自身字段,就绪条目补齐/
      覆盖非空的 ``portal`` / ``site_url`` / ``evidence_zip`` 等执行细节;
    - ``portal`` 双缺时补 :data:`PORTAL_DEFAULT`;
    - 合并后仍缺 ``entry_id`` / ``group_name`` → 中文 :class:`ValueError`
      指明条目(run_batch 前置校验的前移,报错更贴近数据来源)。
    """
    ready_by_id: dict[Any, dict[str, Any]] = {}
    for item in ready_items:
        eid = item.get("entry_id")
        if eid is not None:
            ready_by_id[eid] = dict(item)
    merged: list[dict[str, Any]] = []
    for row in rows:
        item = _as_mapping(row)
        base = ready_by_id.get(item.get("entry_id"), {})
        for key, value in base.items():
            if value not in (None, ""):
                item[key] = value
        item.setdefault("entry_id", base.get("entry_id"))
        item.setdefault("group_name", base.get("group_name"))
        if not item.get("portal"):
            item["portal"] = PORTAL_DEFAULT
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


# ---------------------------------------------------------------------------
# ⭢ trace 收官消费(V12 接线收口,A231;手法对齐 A210 finishflow——只读参考):
#    cfg.trace_enabled 开启时,批次收官段消费各站 intel["trace_id"]:
#    (a) 批级 trace 清单 → result["trace_ids"];(b) 逐站 span 树落盘
#    <data_dir>/runs/<站点标识>/trace.json;(c) 批次末 drain_to_sink 长窗口
#    转存审计 JSONL。开关关闭零行为;任何失败只中文 warning + 计数
#    batchflow.trace_export.failed,绝不中断批次主流程。
# ---------------------------------------------------------------------------
def _trace_id_of(report: Any) -> str:
    """鸭子取 ``report.intel["trace_id"]``;无 intel / 非映射 / 无键 → 空串。

    trace_id 是 16-hex 非内容字段(A198 契约),不涉站点内容,可安全计数。
    (与 A210 finishflow._trace_id_of 同一口径。)
    """
    intel = getattr(report, "intel", None)
    if isinstance(intel, Mapping):
        tid = intel.get("trace_id")
        if isinstance(tid, str) and tid:
            return tid
    return ""


def _site_dirname(url: str, report: Any) -> str:
    """站点目录名:canonical 键优先,回退主机名,再退安全化整串(纯本地、确定)。

    与 A210 finishflow._site_dirname 同一安全化口径:优先
    ``intel.canonical.canonical_key``(项目既定站点同一性口径),模块缺席
    时降级 ``urlsplit().hostname``;任何来源都经 ``[^0-9A-Za-z._-]`` 安全化,
    保证跨平台可作目录名;全部解析失败回退 ``"site"``。
    """
    candidates = [str(url or ""), str(getattr(report, "site_url", "") or "")]
    try:
        canonical_key = _load("netsentinel.intel.canonical", "canonical_key")
    except RuntimeError:
        canonical_key = None
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


def _consume_scan_traces(
    reports: Mapping[str, Any], cfg: Config
) -> tuple[list[str], list[str]]:
    """批次收官段:消费各站 ``intel["trace_id"]`` → 批级清单 + 逐站 trace.json。

    - **开关门槛**::attr:`Config.trace_enabled` 关闭(默认)立即返回空
      双列表——不导入 telemetry_trace、不触碰文件系统、不计数、不打印
      (现状零行为);    - **(a) 清单**:携带 trace_id 的各站报告汇总为去重保序的批级
      ``trace_ids``(无论落盘成败都返回,供批次结果留档);
    - **(b) 落盘**:逐站 ``<cfg.data_dir>/runs/<站点标识>/trace.json``
      (与 A210 finishflow 同路径惯例;同批同站多份报告覆盖前一份,
      路径去重保序);
    - **绝不中断**(红线:trace 落盘失败绝不中断批次):模块缺席 / 导出
      异常 / IO 失败一律中文 warning + 计数 ``batchflow.trace_export.failed``
      后继续下一站点;成功计数 ``batchflow.trace_export.saved``。

    :param reports: ``scan_result["reports"]``({url: SiteReport} 或鸭子替身)。
    :param cfg: 全局配置(只读;仅取 ``data_dir`` 定落盘根)。
    :return: ``(trace_ids, 写出的 trace.json 路径列表)``(双空 = 零行为)。
    """
    if not getattr(cfg, "trace_enabled", False):
        return [], []
    pending: list[tuple[str, Any, str]] = []
    seen: set[str] = set()
    for url, report in dict(reports or {}).items():
        tid = _trace_id_of(report)
        if tid and tid not in seen:
            seen.add(tid)
            pending.append((str(url), report, tid))
    trace_ids = [tid for _, _, tid in pending]
    if not pending:
        return [], []

    try:
        export_trace_json = _load("netsentinel.telemetry_trace", "export_trace_json")
    except RuntimeError as exc:
        telemetry.inc(_METRIC_TRACE_FAILED, len(pending))
        logger.warning(
            "trace 树导出模块未就位,已跳过 %d 份 trace 落盘(不中断批次):%s",
            len(pending), exc,
        )
        return trace_ids, []

    data_dir = pathlib.Path(str(getattr(cfg, "data_dir", "data") or "data"))
    written: list[str] = []
    for url, report, tid in pending:
        out_path = data_dir / "runs" / _site_dirname(url, report) / "trace.json"
        try:
            tree = export_trace_json(tid)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                json.dumps(tree, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            written.append(str(out_path))
            logger.info("trace 树已落盘:%s(trace_id=%s)", out_path, tid)
        except Exception as exc:  # noqa: BLE001 - trace 落盘失败绝不中断批次
            telemetry.inc(_METRIC_TRACE_FAILED)
            logger.warning(
                "trace 树落盘失败(已跳过,不中断批次):%s(%s)", tid, exc
            )
    if written:
        telemetry.inc(_METRIC_TRACE_SAVED, len(written))
    return trace_ids, list(dict.fromkeys(written))  # 去重保序(同站多次扫描同一路径)


def _drain_trace_sink(cfg: Config) -> int:
    """批次末长窗口转存:注册表全部 trace 经 ``drain_to_sink`` 写审计 JSONL。

    - **开关门槛**::attr:`Config.trace_enabled` 关闭(默认)立即返回 0
      ——不导入任何模块、不触碰文件系统、不计数、不打印(现状零行为);
    - A203 交付报告:注册表 ``MAX_TRACES=64`` 逐出,长跑窗口不足——批次
      收官时一次性把存量(含将被逐出的)转存 ``cfg.audit_path`` 的 JSONL
      (``event=trace_export``,四键载荷含嵌套 span 树)后清空注册表;
    - sink 以**显式参数**注入(等价"audit_sink 已配置"):模块级注入的
      sink 由各次扫描自身接线并在扫描结束复位(orchestrator 约定),批次
      末不复用全局态,统一走 ``cfg.audit_path`` 避免跨批泄漏;注册表为空
      时转存零条、不建任何文件(JsonlAuditLogger 惰性建文件);
    - **绝不中断**(红线:转存失败绝不中断主流程):模块缺席 / 日志器
      构造失败 / 整体异常一律中文 warning + 计数
      ``batchflow.trace_export.failed``;单条写失败由内核计数
      ``trace.drain.error`` 后继续;成功计数 ``batchflow.trace_export.drained``。

    :param cfg: 全局配置(只读;仅取 ``audit_path`` 定转存目标)。
    :return: 成功转存的 trace 条数(0 = 开关关闭/无存量/失败,均不中断批次)。
    """
    if not getattr(cfg, "trace_enabled", False):
        return 0
    try:
        drain_to_sink = _load("netsentinel.telemetry_trace", "drain_to_sink")
        audit = _load("netsentinel.logging_util", "JsonlAuditLogger")(
            str(getattr(cfg, "audit_path", "data/audit.jsonl"))
        )
        drained = int(drain_to_sink(sink=lambda payload: audit.log_event(**payload)))
    except Exception as exc:  # noqa: BLE001 - 长窗口转存失败绝不中断批次
        telemetry.inc(_METRIC_TRACE_FAILED)
        logger.warning("trace 长窗口转存失败(已跳过,不中断批次):%s", exc)
        return 0
    if drained:
        telemetry.inc(_METRIC_TRACE_DRAINED, drained)
        logger.info("trace 长窗口转存:%d 条 trace → %s", drained, cfg.audit_path)
    return drained


# ---------------------------------------------------------------------------
# ① 筛选 + 归组:run_flow(永不提交)
# ---------------------------------------------------------------------------
def run_flow(
    input_path: str,
    cfg: Config,
    *,
    intake_load: Callable[[str], tuple[list[str], list[tuple[str, str]]]] | None = None,
    scanner: Callable[..., Any] | None = None,
    submit: Any | None = None,
    dry_run: bool = True,
    memory: Any | None = None,
    run_scan: Callable[[str, Any], Any] | None = None,
    pool_runner: Callable[..., Any] | None = None,
    grouping: Callable[..., Any] | None = None,
    queue: Any | None = None,
    packager: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """批量总流程·前半段:加载清单 → 规划 → 扫描 → 归组入列 → 中文摘要。

    流程(兄弟模块全惰性,可整体注入替换):

    1. ``intake_load``(缺省 A107 :func:`~netsentinel.ops.bulk_intake.load_bulk`)
       加载清单 → ``(合法 URL, 拒绝清单)``;清单不存在/格式坏 → 中文
       :class:`ValueError`(原样上抛,由 CLI 转中文错误码);
    2. A107 ``plan_scan`` 规划(canonical 去重 + 指纹未变跳过;``memory``
       注入透传);
    3. 待扫为空 → **短路返回**(不触碰扫描/归组兄弟模块),计入遥测
       ``batchflow.flow.empty``;
    4. ``scanner``(缺省 A108 ``batch_scan``,``run_scan`` / ``memory`` /
       ``pool_runner`` 透传)并发扫描 → ``{"reports", "summary"}``;
    5. ``grouping``(缺省 A108 ``group_and_enqueue``,``queue`` / ``packager``
       透传)归组 + 合并证据 + 每组入列一条;
    6. 打印中文摘要(合法/拒绝/待扫/跳过/组数/入列数/最大组)与
       "下一步"指引(TUI 逐组声明 → batchflow 续批)。

    红线 24/25:**本函数永不提交**——``submit`` 参数仅为契约签名兼容保留,
    任何取值都不会被调用;``dry_run`` 只影响提示语与返回摘要,不会触发
    任何提交动作(想真实执行请走 :func:`resume_batch` + ``--exec``)。

    :return: ``{"groups": 组数, "enqueued": 入列数, "to_scan": [待扫 URL], "summary": {...}}``;
        ``cfg.trace_enabled`` 开启且扫描结果携带 trace_id 时追加
        ``"trace_ids": [批级 trace 清单]`` 与 ``"trace_json_paths": [落盘路径]``
        (仅在实际消费到/落盘时携带,开关关闭的结果形态与旧版逐字节一致)。
    :raises ValueError: 清单加载失败(A107 中文消息)。
    :raises RuntimeError: 缺省依赖(A107/A108)未就位,中文消息指明模块。
    """
    load_fn = intake_load if intake_load is not None else _load(
        "netsentinel.ops.bulk_intake", "load_bulk"
    )
    valid, rejected = load_fn(input_path)
    valid = [str(u) for u in (valid or [])]
    rejected = list(rejected or [])

    plan_fn = _load("netsentinel.ops.bulk_intake", "plan_scan")
    plan_kwargs: dict[str, Any] = {} if memory is None else {"memory": memory}
    plan = plan_fn(valid, cfg, **plan_kwargs)
    to_scan: list[str] = [str(u) for u in (plan.get("to_scan") or [])]
    skipped_unchanged = int(plan.get("skipped_unchanged", 0) or 0)
    duplicate_urls = int(plan.get("duplicate_urls", 0) or 0)
    telemetry.inc(_METRIC_FLOW)

    reports: dict[str, Any] = {}
    scan_error_count = 0
    groups: list[Any] = []
    enqueued = 0
    skipped_clean = 0
    trace_ids: list[str] = []
    trace_json_paths: list[str] = []
    drained_traces = 0

    if to_scan:
        scan_fn = scanner if scanner is not None else _load(
            "netsentinel.ops.batch_scan", "batch_scan"
        )
        # V9:并发档位只在入口包一层 pool_runner(workers=io_workers);
        # 显式 pool_runner 优先;红线 35——对外礼貌间隔与频控不受档位影响。
        effective_pool_runner = pool_runner
        if pool_runner is None:
            try:
                from netsentinel.ops.concurrency import io_workers as _io_workers
                from netsentinel.ops import pool as _pool_mod

                _workers = _io_workers(cfg)

                def _tier_pool_runner(cfg_, items, **kw):  # noqa: ANN001
                    kw.setdefault("workers", _workers)
                    return _pool_mod.run_pool(cfg_, items, **kw)

                effective_pool_runner = _tier_pool_runner
            except Exception:  # noqa: BLE001 - 档位解析失败回退 pool 默认
                effective_pool_runner = None
        scan_kwargs = {
            key: value
            for key, value in (
                ("run_scan", run_scan),
                ("memory", memory),
                ("pool_runner", effective_pool_runner),
            )
            if value is not None
        }
        with telemetry.timer("batchflow.run_flow.scan"):
            scan_result = scan_fn(to_scan, cfg, **scan_kwargs)
        reports = dict((scan_result or {}).get("reports") or {})
        errors = ((scan_result or {}).get("summary") or {}).get("errors") or []
        scan_error_count = len(errors)

        group_fn = grouping if grouping is not None else _load(
            "netsentinel.ops.batch_scan", "group_and_enqueue"
        )
        group_kwargs = {
            key: value for key, value in (("queue", queue), ("packager", packager))
            if value is not None
        }
        with telemetry.timer("batchflow.run_flow.group"):
            group_result = group_fn(cfg, reports=reports, **group_kwargs)
        groups = list((group_result or {}).get("groups") or [])
        enqueued = int((group_result or {}).get("enqueued", 0) or 0)
        skipped_clean = int((group_result or {}).get("skipped_clean", 0) or 0)

        # ⑥ trace 收官消费(V12 接线收口,A231;finishflow 侧 A210 已收口):
        # cfg.trace_enabled 开启的扫描在 report.intel["trace_id"] 携带 trace 号,
        # 批次收官段消费各站 trace_id——批级清单 + 逐站 trace.json 落盘 +
        # 批次末长窗口转存(注册表转存审计 JSONL 后清空,补 MAX_TRACES=64
        # 长跑窗口不足);开关未开启零行为,任何失败只中文 warning + 计数
        # batchflow.trace_export.failed、绝不中断批次(见 _consume_scan_traces)。
        trace_ids, trace_json_paths = _consume_scan_traces(reports, cfg)
        drained_traces = _drain_trace_sink(cfg)
    else:
        telemetry.inc(_METRIC_FLOW_EMPTY)
        print(f"待扫描清单为空,未执行扫描与归组:{input_path}")
        logger.info("待扫描清单为空,跳过扫描与归组:%s", input_path)

    max_group_size = max((_group_size(g) for g in groups), default=0)
    summary = {
        "valid": len(valid),
        "rejected": len(rejected),
        "to_scan": len(to_scan),
        "skipped_unchanged": skipped_unchanged,
        "duplicate_urls": duplicate_urls,
        "scan_errors": scan_error_count,
        "groups": len(groups),
        "enqueued": enqueued,
        "skipped_clean": skipped_clean,
        "max_group_size": max_group_size,
        "dry_run": bool(dry_run),
    }

    _print_flow_summary(input_path, summary)
    if trace_json_paths:
        print(f"trace 落盘 : {len(trace_json_paths)} 份(runs/<站点>/trace.json)")
    if drained_traces:
        print(f"trace 转存 : {drained_traces} 条(审计 JSONL 长窗口)")
    _print_flow_next_steps()
    logger.info(
        "批量筛选流程完成:%s(合法 %d/拒绝 %d/待扫 %d/组 %d/入列 %d/trace %d/转存 %d)",
        input_path,
        summary["valid"],
        summary["rejected"],
        summary["to_scan"],
        summary["groups"],
        summary["enqueued"],
        len(trace_ids),
        drained_traces,
    )
    result: dict[str, Any] = {
        "groups": len(groups),
        "enqueued": enqueued,
        "to_scan": to_scan,
        "summary": summary,
    }
    if trace_ids:
        # (a) 批级 trace 清单:仅在实际消费到 trace_id 时携带(开关关闭的
        # 批次结果形态与旧版逐字节一致)
        result["trace_ids"] = trace_ids
    if trace_json_paths:
        result["trace_json_paths"] = trace_json_paths
    return result


def _print_flow_summary(input_path: str, s: dict[str, Any]) -> None:
    """中文打印批量筛选摘要(合法/拒绝/待扫/跳过/组数/入列数/最大组)。"""
    print(f"── 批量筛选流程摘要({input_path})──")
    print(f"合法 URL  : {s['valid']}")
    print(f"拒绝条目  : {s['rejected']}")
    print(f"待扫    : {s['to_scan']}")
    print(
        f"跳过    : {s['skipped_unchanged']}(指纹未变)"
        f" / {s['duplicate_urls']}(同站重复)"
    )
    print(f"案件组数  : {s['groups']}")
    print(f"入列数   : {s['enqueued']}(clean 跳过 {s['skipped_clean']} 组)")
    print(f"最大组   : {s['max_group_size']} 个站点")
    if s.get("scan_errors"):
        print(f"扫描失败  : {s['scan_errors']} 个(详见扫描日志,失败不中断整批)")


def _print_flow_next_steps() -> None:
    """打印后续步骤指引:TUI 逐组声明(红线 25)→ batchflow 续批(红线 24)。"""
    print("下一步:python -m netsentinel.cli.batch_tui 声明")
    print("  1) python -m netsentinel.cli.batch_tui")
    print("     groups 查看分组 → attest <组名> --reviewer <审核人> 逐组声明")
    print("     (红线 25:无声明的组不得进入批量)")
    print("  2) 声明完成后:python -m netsentinel.batchflow --resume <批次号> --exec")
    print("     (红线 24:批量仍是逐条人工门,每条提交均需人工输入验证码并确认)")
    print("  注:本流程到此为止,未执行任何提交。")


# ---------------------------------------------------------------------------
# ② 批量 + 续批:resume_batch
# ---------------------------------------------------------------------------
def resume_batch(
    batch_id: int | None,
    cfg: Config,
    *,
    ready: Callable[..., list[dict[str, Any]]] | None = None,
    executor: Callable[..., Any] | None = None,
    dry_run: bool | None = None,
    state: Any | None = None,
    submit: Callable[..., Any] | None = None,
    plan_12377: Callable[[Any, Config], Any] | None = None,
    plan_shdf: Callable[[Any, Config], Any] | None = None,
    rate: Any | None = None,
) -> dict[str, Any]:
    """批量总流程·后半段:就绪清单 → 批次状态 → 顺序批量举报(逐条人工门)。

    流程(兄弟模块全惰性,可注入替换):

    1. ``ready``(缺省 A110 ``ready_entries``)取待批量条目——即已确认且
       所在组已完成人工核实声明的条目(红线 25 在上游收口);
    2. ``state``(缺省惰性 A113 ``BatchState(cfg.db_path)``,对接约定用法
       ``st = BatchState(db); bid = st.new_batch(items); run_batch(items,
       cfg, state=st.bound(bid))``)查 ``resume(batch_id)`` 未完条目:

       - 有未完条目 → **续批**:``batch_id`` 原样沿用,条目与就绪清单按
         ``entry_id`` 合并补齐 portal 等执行细节;
       - 无未完条目 → 就绪清单非空时**新建批次**(note 留痕),空则直接
         返回零值摘要(不新建、不执行);

    3. ``submit``(缺省惰性 A112 ``run_batch``,注入参数名按对接约定为
       ``plan_12377=`` / ``plan_shdf=``,**不是** portal_planner_*)顺序逐条
       执行,状态器以 ``state=st.bound(batch_id)`` 绑定批次;
    4. 返回 run_batch 结果字典 + ``batch_id`` 键。

    dry_run 语义(红线 24):``dry_run=None``(缺省)取 ``cfg.dry_run_default``
    (默认 True);真实执行必须**显式** ``dry_run=False``——本函数对
    ``executor`` 只做原样透传,绝不包装、绝不出现 ``auto_confirm=True``
    的路径,逐条验证码输入与最终确认始终由人工完成。

    :param batch_id: 待续批次号;``None`` 或批次无未完条目时按就绪清单新建。
    :raises RuntimeError: A110/A112/A113 未就位,或注入状态器不满足
        ``resume/new_batch/bound`` 鸭子约定,中文消息指明原因。
    :return: ``{"submitted": n, "failed": n, "rate_limited": bool, "paused": bool,
        "results": [...], "note": str, "batch_id": 批次号}``。
    """
    effective_dry_run = bool(cfg.dry_run_default) if dry_run is None else bool(dry_run)

    ready_fn = ready if ready is not None else _load(
        "netsentinel.decision.batch_review", "ready_entries"
    )
    ready_items = [dict(item) for item in (ready_fn(cfg) or [])]

    own_state = state is None
    st = state if state is not None else _load(
        "netsentinel.submit.batch_state", "BatchState"
    )(getattr(cfg, "db_path", "data/review_queue.db"))

    try:
        resume_fn = getattr(st, "resume", None)
        if not callable(resume_fn):
            raise RuntimeError(
                "注入的批次状态器未提供 resume(batch_id) 方法(对接约定 A113):"
                f"{type(st).__name__}"
            )
        unfinished = list(resume_fn(batch_id) or []) if batch_id is not None else []
        if unfinished:
            items = _merge_resumed_items(unfinished, ready_items)
            bid = batch_id
            telemetry.inc(_METRIC_RESUME_CONTINUE)
            print(f"续批批次 {bid}:未完条目 {len(items)} 条(就绪清单 {len(ready_items)} 条)")
        elif ready_items:
            new_batch = getattr(st, "new_batch", None)
            if not callable(new_batch):
                raise RuntimeError(
                    "注入的批次状态器未提供 new_batch(items, note) 方法(对接约定 A113):"
                    f"{type(st).__name__}"
                )
            items = ready_items
            bid = new_batch(items, note=_NEW_BATCH_NOTE)
            telemetry.inc(_METRIC_RESUME_NEW)
            print(
                f"批次 {batch_id or '(无)'} 无未完条目,已按就绪清单新建批次 {bid}"
                f"(共 {len(items)} 条)"
            )
        else:
            items = []
            bid = batch_id
            telemetry.inc(_METRIC_RESUME_IDLE)
            print(
                f"无待批量条目:批次 {batch_id or '(无)'} 无未完条目,"
                "就绪清单为空(未新建批次、未执行任何提交)"
            )

        if not items:
            result: dict[str, Any] = {
                "submitted": 0,
                "failed": 0,
                "rate_limited": False,
                "paused": False,
                "results": [],
                "note": "无待批量条目,未执行",
            }
        else:
            bound = getattr(st, "bound", None)
            if not callable(bound):
                raise RuntimeError(
                    "注入的批次状态器未提供 bound(batch_id) 方法(对接约定 A113,"
                    "run_batch 状态落账须绑定批次):"
                    f"{type(st).__name__}"
                )
            run_batch_fn = submit if submit is not None else _load(
                "netsentinel.submit.batch_submit", "run_batch"
            )
            if not effective_dry_run:
                print(
                    f"真实执行模式:批次 {bid} 共 {len(items)} 条,将依次提交;"
                    "每条仍需人工输入验证码并最终确认(红线 24,无任何自动确认)。"
                )
            run_kwargs: dict[str, Any] = {"dry_run": effective_dry_run}
            if executor is not None:
                run_kwargs["executor"] = executor
            if plan_12377 is not None:
                run_kwargs["plan_12377"] = plan_12377
            if plan_shdf is not None:
                run_kwargs["plan_shdf"] = plan_shdf
            if rate is not None:
                run_kwargs["rate"] = rate
            with telemetry.timer("batchflow.resume_batch.run"):
                result = dict(run_batch_fn(items, cfg, state=bound(bid), **run_kwargs))
    finally:
        if own_state:  # 缺省状态器归本函数所有,用毕即关(注入的归调用方)
            close = getattr(st, "close", None)
            if callable(close):
                close()

    result["batch_id"] = bid
    telemetry.inc(_METRIC_RESUME)
    _print_resume_summary(result, effective_dry_run)
    logger.info(
        "批量续批完成:批次 %s submitted=%d failed=%d rate_limited=%s paused=%s",
        bid,
        result.get("submitted", 0),
        result.get("failed", 0),
        result.get("rate_limited", False),
        result.get("paused", False),
    )
    return result


def _print_resume_summary(result: dict[str, Any], dry_run: bool) -> None:
    """中文打印续批结果与后续指引(挂起/暂停时提示续批命令)。"""
    mode = "干跑演练(dry_run,未真实提交)" if dry_run else "真实执行"
    print(f"── 批量续批摘要({mode})──")
    print(f"批次号  : {result.get('batch_id')}")
    print(f"提交成功: {result.get('submitted', 0)} 条")
    print(f"失败    : {result.get('failed', 0)} 条")
    if result.get("note"):
        print(f"说明    : {result['note']}")
    if result.get("rate_limited") or result.get("paused"):
        print(
            "可续批  : python -m netsentinel.batchflow --resume "
            f"{result.get('batch_id')} --exec(额度恢复/确认继续后)"
        )
    print(
        "红线 24:每条提交的验证码输入与最终确认均由人工完成;"
        "红线 26:批量频控与每日额度与单站流程一致,未放宽。"
    )


# ---------------------------------------------------------------------------
# ③ CLI:python -m netsentinel.batchflow
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m netsentinel.batchflow",
        description=(
            "净网哨兵批量总流程:筛选 → 归组 →(TUI 声明)→ 批量 → 续批;"
            "批量举报仍是逐条人工门(红线 24)"
        ),
    )
    parser.add_argument(
        "--input",
        default=None,
        help="批量清单路径(.txt/.csv/.yaml/.yml),执行筛选→归组流程(不提交)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径(默认 ./config.yaml,缺失则用默认配置)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="干跑模式(默认;批量只演练,不真实提交)",
    )
    parser.add_argument(
        "--exec",
        action="store_true",
        help=(
            "真实执行模式:仅 --resume 续批有效,与 --dry-run 互斥;"
            "每条提交仍需人工输入验证码(红线 24)"
        ),
    )
    parser.add_argument(
        "--resume",
        type=int,
        default=None,
        metavar="批次号",
        help=(
            "续批:批次号(批次无未完条目时按就绪清单新建批次;"
            "真实执行需同时给 --exec"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口:``--input ... [--config] [--dry-run|--exec] [--resume 批次号]``。


    模式(互斥):

    - ``--input PATH``:**筛选流程**(加载→规划→扫描→归组→摘要),恒不提交;
      结束后打印"下一步:python -m netsentinel.cli.batch_tui 声明";
    - ``--resume 批次号``:**续批**(就绪清单→批次状态→run_batch);
      默认干跑,真实执行必须显式 ``--exec``(``--dry-run`` 与 ``--exec``
      互斥;``--exec`` 仅在 ``--resume`` 模式有效)。

    返回码:0 成功;2 用法/配置/清单错误(ValueError);3 兄弟模块未就位或
    对接约定不满足(RuntimeError,中文消息)。
    """
    args = _build_parser().parse_args(argv)

    if args.dry_run and args.exec:
        print("错误:--dry-run 与 --exec 互斥,真实执行请只给 --exec")
        return 2
    if args.exec and args.resume is None:
        print("错误:--exec 仅在 --resume 续批模式下有效(筛选流程恒不提交)")
        return 2
    if args.input and args.resume is not None:
        print("错误:--input(筛选流程)与 --resume(续批)互斥,请分开执行")
        return 2
    if not args.input and args.resume is None:
        print("错误:需要 --input 批量清单(筛选流程)或 --resume 批次号(续批)之一")
        return 2

    from netsentinel.config import load_config  # 惰性:纯用法错误不触碰配置

    try:
        cfg = load_config(args.config)
    except ValueError as exc:
        print(f"配置错误:{exc}")
        return 2
    # V8:安装后自动接管本地视觉模型(无模型时弹连接向导);失败只告警不阻断。
    try:
        from netsentinel.pipeline.takeover import takeover_once

        takeover_result = takeover_once(cfg)
        if takeover_result.get("action") in ("local", "cloud", "wizard"):
            print(f"[接管] {takeover_result.get('action')}: {takeover_result.get('detail', '')[:80]}")
    except Exception as exc:  # noqa: BLE001 - 接管是增强项
        print(f"[接管] 跳过:{exc}")

    try:
        if args.resume is not None:
            # CLI 强制显式:--exec → dry_run=False(真实);否则恒 dry_run=True
            resume_batch(args.resume, cfg, dry_run=not args.exec)
        else:
            run_flow(args.input, cfg, dry_run=not args.exec)
    except ValueError as exc:
        print(f"错误:{exc}")
        return 2
    except RuntimeError as exc:
        print(f"错误:{exc}")
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    raise SystemExit(main())
