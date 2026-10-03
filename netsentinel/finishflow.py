# -*- coding: utf-8 -*-
"""收官 CLI(NetSentinel V9 · A169)。

批量跑完后的**收官入口**(独立 CLI,不改 batchflow):把 V9 并发档位、
批量扫描与结案代理串成一条命令,并在声明完成后接管"批量举报准备":

- :func:`finish`(收官流程):A165 ``tier_once``(并发档位一次性初始化,
  cfg 字段优先)→ A164 ``io_workers`` 计算 worker 数 → A108 ``batch_scan``
  并发扫描(workers 经 pool_runner 包装透传给 A58 ``run_pool``;注入
  ``scan`` 时以 ``workers=`` 关键字透传)→ **V12:trace 树落盘**(扫描
  结果携带 ``intel["trace_id"]`` 时经 A198 ``telemetry_trace.export_trace_json``
  逐站点导出 span 树到 ``data/runs/<站点标识>/trace.json``;无 trace_id
  零行为,落盘失败只中文 warning + 计数 ``finishflow.trace_export.failed``,
  **绝不中断收官流程**)→ **V13:批次成本归集**(全局账本
  ``data/vlm_cost.jsonl`` 存在时,经 A75 ``cost_meter.aggregate`` 按
  model/run_id 聚合写 ``data/runs/<批次标识>/cost.jsonl``(**永远写**);
  pyarrow 可得(惰性 import)时追加导出 ``cost.parquet``,失败降级 jsonl
  + 中文 warning + 计数 ``finishflow.cost_export.failed``;账本不存在零
  行为;失败**绝不中断收官**)→ **A232 批预算哨兵检查**(cfg 附加属性
  ``batch_cost_budget``,getattr 缺省 None = 不检查;数值则经
  ``cost_meter.check_budget`` 聚合该批费用,超限时中文醒目告警 + 计数
  ``finishflow.budget.exceeded`` + result 标记 ``budget_exceeded=True``
  与明细;**只告警与标记——绝不删除已产出文件、绝不改队列状态**,详见
  :func:`_check_batch_budget`)→ A168 ``SummaryAgent.run``
  分类汇总 + 举报准备(列待声明组)→ 打印中文收官汇总(站点/扫描/
  失败/组数/待声明组/就绪条目/档位/worker 数)→ 返回 outcome dict。
- :func:`run_report`(举报准备):A110 ``ready_entries`` 取就绪条目 →
  A174 ``SequentialReportAgent.run`` 编排 A112 ``run_batch``(**默认干跑**;
  ``batch_id`` 给定则经 A113 ``BatchState`` 续批)→ 返回
  ``{"result": run_batch 摘要, "report_path": 批次报告路径}``。
- :func:`main`:``--input PATH``(收官流程)/ ``--config PATH`` /
  ``--tier low|mid|high``(覆盖 ``cfg.concurrency_tier`` 再跑)/
  ``--report``(举报准备模式,与 ``--input`` 互斥)/ ``--resume 批次号`` /
  ``--dry-run``|``--exec``(互斥,``--exec`` 仅 ``--report`` 模式有效);
  ``python -m netsentinel.finishflow`` 入口。

安全红线(CONTRACTS-V9 §0,违反即缺陷):

35. **压榨边界**:并发档位只作用于本地计算与本地回环 IO;对外网络的礼貌
    间隔(``cfg.fetch_delay_s``)、引擎限速、举报频控(红线 26)**一概不
    放宽——本模块对 ``cfg`` 只读、绝不改写礼貌/频控字段,``--tier`` 覆盖
    经 ``dataclasses.replace`` 生成**新** Config(其余字段原样保留),
    ``cfg`` 原样透传给扫描与汇总。
36. **结案代理无自主提交权**:``finish`` 只汇总与列待声明组;``run_report``
    默认干跑,真实执行必须显式 ``--exec``,且 SequentialReportAgent 内部
    仍恒 ``auto_confirm=False``、逐组人工声明、逐条 HUMAN_GATE——本模块
    源码不存在任何 ``auto_confirm=True`` 或绕过 attest 的路径。

兄弟模块(A164/A165/A168/A174 并行开发中)全部**惰性导入 + 鸭子注入**,
未就位时抛中文 :class:`RuntimeError` 指明缺失模块;测试可全量离线注入、
零外呼。

用法示例(fake 注入,离线)::

    from netsentinel.contracts import Config

    outcome = finish(["https://a.com/"], cfg, scan=fake_scan,
                     summary=FakeSummaryAgent())
    # outcome == {"sites": 1, "scanned": 1, "failed": 0, "groups": 1,
    #             "attest_pending": 1, "ready_count": 0, "tier": "mid",
    #             "workers": 2, ...}

    report = run_report(cfg, items=[...], runner=fake_runner, dry_run=True)
    # report == {"result": {...run_batch 摘要...}, "report_path": "out/...md"}
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
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

__all__ = ["PORTAL_DEFAULT", "finish", "main", "run_report"]

logger = logging.getLogger(__name__)

#: 续批条目缺 portal 字段时的缺省门户(与 A110 DEFAULT_PORTAL 对齐)
PORTAL_DEFAULT: str = "12377"

#: 新建批次的 note 前缀(A113 batches.note;仅流程标注,不含站点内容)
_NEW_BATCH_NOTE: str = "[finishflow] 就绪清单新建批次(来源:ready_entries)"

# 遥测指标名(只记名称与数字,不记站点内容)
_METRIC_FINISH = "finishflow.finish"
_METRIC_FINISH_SCAN = "finishflow.finish.scan"
_METRIC_FINISH_SUMMARY = "finishflow.finish.summary"
_METRIC_REPORT = "finishflow.report"
_METRIC_REPORT_NEW = "finishflow.report.new_batch"
_METRIC_REPORT_CONTINUE = "finishflow.report.continue"
_METRIC_REPORT_IDLE = "finishflow.report.idle"
_METRIC_TRACE_EXPORT_SAVED = "finishflow.trace_export.saved"
_METRIC_TRACE_EXPORT_FAILED = "finishflow.trace_export.failed"
# V13 批次成本归集(收官新增步):成功/parquet/失败计数(失败绝不中断收官)
_METRIC_COST_SAVED = "finishflow.cost_export.saved"
_METRIC_COST_PARQUET = "finishflow.cost_export.parquet"
_METRIC_COST_EXPORT_FAILED = "finishflow.cost_export.failed"
# A232 批预算哨兵检查(收官新增步:超限只告警计数,绝不执行熔断动作)
_METRIC_BUDGET_EXCEEDED = "finishflow.budget.exceeded"

#: 全局 VLM 成本账本文件名(与 webui providers 页同口径:<data_dir>/vlm_cost.jsonl)
_COST_LEDGER_NAME: str = "vlm_cost.jsonl"

#: A168 SummaryOutcome 的已知字段(鸭子取值用;未知字段不丢——见 _outcome_dict)
_OUTCOME_KEYS: tuple[str, ...] = (
    "groups",
    "report_md",
    "report_html",
    "ready_count",
    "attest_pending",
    "workers",
    "tier",
    "next_steps",
)


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
# 纯辅助:鸭子取值 / 计数
# ---------------------------------------------------------------------------
def _count(value: Any) -> int:
    """把"组数"类字段宽容转成 int:数值直取,列表/集合取长度,其余 0。"""
    if value is None:
        return 0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    try:
        return int(len(value))
    except TypeError:
        return 0


def _outcome_dict(outcome: Any) -> dict[str, Any]:
    """把 A168 ``SummaryOutcome`` 宽容转成 dict。

    兼容映射 / dataclass(A168 契约形态)/ 普通对象(按 :data:`_OUTCOME_KEYS`
    取属性);完全取不到字段时回退 ``{"raw": outcome}``,不丢结果。
    """
    if isinstance(outcome, Mapping):
        return dict(outcome)
    if dataclasses.is_dataclass(outcome) and not isinstance(outcome, type):
        return {f.name: getattr(outcome, f.name) for f in dataclasses.fields(outcome)}
    picked = {k: getattr(outcome, k) for k in _OUTCOME_KEYS if hasattr(outcome, k)}
    return picked if picked else {"raw": outcome}


def _pool_runner_with_workers(
    workers: int, pool_runner: Callable[..., Any] | None = None
) -> Callable[..., Any]:
    """造一个给 ``run_pool`` 补 ``workers=`` 的池运行器包装(A108 对接)。

    A108 ``batch_scan`` 不收 workers(签名冻结),因此收官流程经
    ``pool_runner`` 注入包装器:鸭子复刻 ``run_pool(cfg, items, *,
    run_scan=..., memory=...)`` 签名,调用前 ``setdefault("workers", n)``
    ——worker 数来自 A164 ``io_workers``(红线 35:只影响本地回环并发,
    礼貌间隔 ``fetch_delay_s`` 等由 ``run_pool`` 原样执行,不随档位放宽)。
    """
    runner = pool_runner if pool_runner is not None else _load(
        "netsentinel.ops.pool", "run_pool"
    )

    def _wrapped(cfg_: Any, items: list[str], **kwargs: Any) -> Any:
        kwargs.setdefault("workers", int(workers))
        return runner(cfg_, items, **kwargs)

    return _wrapped


def _as_mapping(row: Any) -> dict[str, Any]:
    """把续批行(A113 resume 返回的 dict/sqlite Row/对象)宽容转成 dict。"""
    if isinstance(row, Mapping):
        return dict(row)
    keys = getattr(row, "keys", None)
    if callable(keys):
        try:
            return {str(k): row[k] for k in keys()}
        except Exception:  # noqa: BLE001 - 鸭子行取值失败按空映射处理
            return {}
    state = getattr(row, "__dict__", None)
    return dict(state) if isinstance(state, dict) else {}


def _merge_resumed_items(
    rows: list[Any], ready_items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """续批行 ←→ 就绪清单按 ``entry_id`` 合并补齐执行细节。

    - 就绪条目按 ``entry_id`` 建索引;续批行优先保留自身字段,就绪条目
      补齐/覆盖非空的 ``portal`` / ``site_url`` / ``evidence_zip`` 等;
    - ``portal`` 双缺时补 :data:`PORTAL_DEFAULT`(A112 run_batch 前置)。
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
        if not item.get("portal"):
            item["portal"] = PORTAL_DEFAULT
        merged.append(item)
    return merged


# ---------------------------------------------------------------------------
# ③.5 trace 树落盘(V12 接线收口):intel["trace_id"] → data/runs/<站点>/trace.json
# ---------------------------------------------------------------------------
def _trace_id_of(report: Any) -> str:
    """鸭子取 ``report.intel["trace_id"]``;无 intel / 非映射 / 无键 → 空串。

    trace_id 是 16-hex 非内容字段(A198 契约),不涉站点内容,可安全计数。
    """
    intel = getattr(report, "intel", None)
    if isinstance(intel, Mapping):
        tid = intel.get("trace_id")
        if isinstance(tid, str) and tid:
            return tid
    return ""


def _site_dirname(url: str, report: Any) -> str:
    """站点目录名:canonical 键优先,回退主机名,再退安全化整串(纯本地、确定)。

    - 优先 ``intel.canonical.canonical_key``(项目既定站点同一性口径,
      ``www.`` 归并/IP 直连原样),模块缺席时降级 ``urlsplit().hostname``;
    - 任何来源都经 ``[^0-9A-Za-z._-]`` 安全化(IPv6 冒号等目录非法字符
      归一为 ``_``),保证跨平台可作目录名;全部解析失败回退 ``"site"``。
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


def _export_scan_traces(reports: Mapping[str, Any], cfg: Config) -> list[str]:
    """把扫描结果携带的 ``intel["trace_id"]`` 逐站点导出为 span 树 JSON。

    - **零行为门槛**:没有任何 trace_id 时立即返回空列表——不导入
      ``telemetry_trace``、不触碰文件系统、不计数、不打印(与升格前
      逐字节一致);
    - **落盘惯例**:``<cfg.data_dir>/runs/<站点标识>/trace.json``(与
      A114 batch_report 同以 ``data_dir`` 为根的路径惯例对齐;同批同站
      的多份报告覆盖前一份,以最后一次扫描为准,返回路径去重保序);
    - **绝不中断**(红线:trace 落盘失败绝不中断收官流程):模块缺席 /
      导出异常 / IO 失败一律中文 warning + 计数
      ``finishflow.trace_export.failed`` 后继续下一站点;成功计数
      ``finishflow.trace_export.saved``。

    :param reports: ``scan_result["reports"]``({url: SiteReport} 或鸭子替身)。
    :param cfg: 全局配置(只读;仅取 ``data_dir`` 定落盘根)。
    :return: 成功写出的 trace.json 路径列表(空列表 = 无 trace 零行为)。
    """
    pending: list[tuple[str, Any, str]] = []
    for url, report in dict(reports or {}).items():
        tid = _trace_id_of(report)
        if tid:
            pending.append((str(url), report, tid))
    if not pending:
        return []

    try:
        export_trace_json = _load("netsentinel.telemetry_trace", "export_trace_json")
    except RuntimeError as exc:
        telemetry.inc(_METRIC_TRACE_EXPORT_FAILED, len(pending))
        logger.warning(
            "trace 树导出模块未就位,已跳过 %d 份 trace 落盘(不中断收官):%s",
            len(pending), exc,
        )
        return []

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
        except Exception as exc:  # noqa: BLE001 - trace 落盘失败绝不中断收官
            telemetry.inc(_METRIC_TRACE_EXPORT_FAILED)
            logger.warning(
                "trace 树落盘失败(已跳过,不中断收官):%s(%s)", tid, exc
            )
    if written:
        telemetry.inc(_METRIC_TRACE_EXPORT_SAVED, len(written))
    return list(dict.fromkeys(written))  # 去重保序(同站多次扫描同一路径)


# ---------------------------------------------------------------------------
# ③.6 批次成本归集(V13 收官新增步):vlm_cost.jsonl → runs/<批次>/cost.{jsonl,parquet}
# ---------------------------------------------------------------------------
def _default_run_id() -> str:
    """缺省批次标识:本地时间 ``run-YYYYMMDD-HHMMSS``(纯本地、可排序)。"""
    return "run-" + _dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def _safe_run_dirname(run_id: str) -> str:
    """批次标识 → 安全目录名(与 :func:`_site_dirname` 同一安全化口径)。"""
    slug = re.sub(r"[^0-9A-Za-z._-]+", "_", str(run_id or "")).strip("._-")
    return slug[:64] if slug else "run"


def _export_cost_parquet(
    out_path: pathlib.Path, aggregates: Mapping[str, dict[str, Any]]
) -> str | None:
    """把聚合账导出为 parquet(**可选** analytics extra;失败上抛由调用方降级)。

    - pyarrow **惰性 import**:未安装(缺 analytics extra)→ 返回 None 静默
      跳过——jsonl 已落盘,属正常可选路径,不算失败、不计数;
    - 可得 → 全部聚合行(各维度明细行 + 各维度合计行,行内带 ``by`` 标注)
      经 ``pa.Table.from_pylist`` 建表、``pq.write_table`` 落盘,返回路径;
    - 任何导出异常原样上抛(:func:`_close_out_cost` 统一降级 jsonl)。
    """
    try:
        import pyarrow as pa  # 惰性:可选 analytics extra,缺失降级 jsonl
        import pyarrow.parquet as pq
    except ImportError:
        logger.info(
            "pyarrow 未安装(analytics 可选 extra),跳过 cost.parquet 导出"
            "(cost.jsonl 已落盘,降级可用)"
        )
        return None
    rows: list[dict[str, Any]] = []
    for by, agg in aggregates.items():
        rows.extend({"by": by, **row} for row in (agg.get("rows") or []))
        rows.append({"by": f"{by}:totals", **(agg.get("totals") or {})})
    if not rows:
        logger.debug("聚合账为空,跳过 cost.parquet 导出:%s", out_path)
        return None
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, out_path)
    return str(out_path)


def _close_out_cost(cfg: Config, run_id: str) -> dict[str, Any] | None:
    """批次成本归集收官步(V13 新增:**任何失败只告警计数,绝不中断收官**)。

    - **零行为门槛**:全局成本账本 ``<cfg.data_dir>/vlm_cost.jsonl``
      (与 webui providers 页同口径)不存在 → 立即返回 None——不建目录、
      不计数、不打印(现状无 VLM 记账的收官行为与升格前一致);
    - 账本存在 → 快照聚合(by=model 明细 + by=run_id 总账;旧账目行回退
      "(未标记批次)" 桶)写 ``<data_dir>/runs/<批次标识>/cost.jsonl``
      (**永远写**,与 pyarrow 无关,首行 meta + 聚合行 + 合计行);
    - pyarrow 可得 → 追加导出 ``cost.parquet`` 并计数;导出异常 → 中文
      warning + 计数 ``finishflow.cost_export.failed`` 后**降级已落盘的
      jsonl**(不重写、不中断);不可得 → 静默跳过(可选 extra);
    - 其余任何异常(模块未就位 / IO 失败等)同样只中文 warning + 计数
      failed,绝不中断收官流程。

    :param cfg: 全局配置(只读;仅取 ``data_dir`` 定账本与落盘根)。
    :param run_id: 本批收官标识(目录名经 :func:`_safe_run_dirname` 安全化)。
    :return: 归集结果 dict(``run_id`` / ``cost_jsonl`` / ``cost_parquet`` /
        ``by_model`` / ``by_run``);账本不存在或整体失败时 None。
    """
    data_dir = pathlib.Path(str(getattr(cfg, "data_dir", "data") or "data"))
    ledger = data_dir / _COST_LEDGER_NAME
    if not ledger.exists():
        return None  # 零行为门槛:无账本即无账可归,不建目录不计数不打印
    try:
        from netsentinel.vision.cost_meter import CostMeter  # 领地内兄弟,惰性导入
    except Exception as exc:  # noqa: BLE001 - 模块未就位只告警,不中断收官
        telemetry.inc(_METRIC_COST_EXPORT_FAILED)
        logger.warning("成本计量模块未就位,已跳过批次成本归集(不中断收官):%s", exc)
        return None
    try:
        meter = CostMeter(ledger)
        by_model = meter.aggregate(by="model")
        by_run = meter.aggregate(by="run_id")
        out_dir = data_dir / "runs" / _safe_run_dirname(run_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        cost_jsonl = out_dir / "cost.jsonl"
        payload: list[dict[str, Any]] = [
            {
                "kind": "meta",
                "run_id": run_id,
                "generated_at": _dt.datetime.now().astimezone().isoformat(
                    timespec="seconds"
                ),
                "ledger": _COST_LEDGER_NAME,
                "records": int(by_run["totals"]["calls"]),
            }
        ]
        payload.extend(
            {"kind": "agg", "by": "model", **row} for row in by_model["rows"]
        )
        payload.extend(
            {"kind": "agg", "by": "run_id", **row} for row in by_run["rows"]
        )
        payload.append({"kind": "totals", "by": "model", **by_model["totals"]})
        payload.append({"kind": "totals", "by": "run_id", **by_run["totals"]})
        with cost_jsonl.open("w", encoding="utf-8", newline="\n") as fh:
            for obj in payload:
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        telemetry.inc(_METRIC_COST_SAVED)
        logger.info(
            "批次成本账已落盘:%s(模型 %d 行 / 批次 %d 行 / 共 %d 次调用)",
            cost_jsonl, len(by_model["rows"]), len(by_run["rows"]),
            int(by_run["totals"]["calls"]),
        )
    except Exception as exc:  # noqa: BLE001 - 归集失败绝不中断收官
        telemetry.inc(_METRIC_COST_EXPORT_FAILED)
        logger.warning("批次成本归集失败(已跳过,不中断收官):%s", exc)
        return None
    # parquet 为可选增强:单独兜底,失败降级已落盘的 jsonl(绝不中断收官)
    parquet_path: str | None = None
    try:
        parquet_path = _export_cost_parquet(
            out_dir / "cost.parquet", {"model": by_model, "run_id": by_run}
        )
        if parquet_path:
            telemetry.inc(_METRIC_COST_PARQUET)
    except Exception as exc:  # noqa: BLE001 - parquet 失败降级 jsonl
        telemetry.inc(_METRIC_COST_EXPORT_FAILED)
        logger.warning(
            "成本 parquet 导出失败,已降级 cost.jsonl(不中断收官):%s", exc
        )
    return {
        "run_id": run_id,
        "cost_jsonl": str(cost_jsonl),
        "cost_parquet": parquet_path,
        "by_model": by_model,
        "by_run": by_run,
    }


def _print_cost_summary(cost: dict[str, Any]) -> None:
    """中文打印批次成本表(模型 × 调用次数 × 费用;提示值口径,以账单为准)。"""
    by_model = cost["by_model"]
    print("── 批次成本汇总(提示值口径,以账单为准,红线 18)──")
    print(f"批次标识: {cost['run_id']}")
    print(f"{'模型(提供方:模型)':<32}{'调用次数':>8}{'图片数':>9}{'费用(元)':>12}")
    for row in by_model["rows"]:
        print(
            f"{row['model'][:48]:<48}{row['calls']:>8}{row['images']:>9}"
            f"{row['est_cost']:>12.2f}"
        )
    totals = by_model["totals"]
    print(
        f"{'合计':<48}{totals['calls']:>8}{totals['images']:>9}"
        f"{totals['est_cost']:>12.2f}"
    )
    if totals["unpriced_calls"]:
        print(
            f"无价格提示调用: {totals['unpriced_calls']}"
            "(未计入费用,以账单为准,红线 18)"
        )
    runs = cost["by_run"]["rows"]
    if runs:
        parts = "、".join(
            f"{r['run_id']} {r['calls']} 次 {r['est_cost']:.2f} 元" for r in runs
        )
        print(f"总账(按批次): {parts}")
    parquet = cost.get("cost_parquet")
    suffix = "(+ cost.parquet)" if parquet else ""
    print(f"成本快照: {cost['cost_jsonl']}{suffix}")


# ---------------------------------------------------------------------------
# ③.7 批预算哨兵检查(A232 收官新增步):cfg.batch_cost_budget → 告警+标记
# ---------------------------------------------------------------------------
def _check_batch_budget(cfg: Config, run_id: str) -> Any | None:
    """批预算哨兵检查(**哨兵不是执行器**:只告警 + 标记,绝不执行熔断)。

    - **零行为门槛**:cfg 附加属性 ``batch_cost_budget``(getattr 缺省
      None = 未配置,与 V11 前开关挂载同款动态读取)为空 → 立即返回
      None——不读账本、不计数、不打印(与未配置前的收官行为逐字节一致);
      全局账本 ``<cfg.data_dir>/vlm_cost.jsonl`` 不存在 → 同样返回 None
      (无账可查,与成本归集步同门槛);
    - ``batch_cost_budget`` 为数值 → 经 A75 ``cost_meter.check_budget``
      聚合该批(``run_id``)est_cost 总额(unpriced 诚实单独计数);
      **超限** → 计数 ``finishflow.budget.exceeded`` 并返回状态,由
      :func:`finish` 标记 result(``budget_exceeded=True`` + 明细)并
      中文醒目告警;未超限 → 返回状态但不打印(零噪音);
    - **哨兵不是执行器(红线)**:本函数与调用方**只**做告警、计数与
      result 标记——**绝不删除已产出的证据包 / 成本账 / trace,绝不改
      复核队列与批次状态**;"停止发起后续扫描"仅作为人工处置建议出现在
      告警文案里,是否继续由人决定(本模块从不代为执行任何熔断动作);
    - 配置非法(非数值 / 负数 / bool)、模块未就位、账本读取异常 →
      中文 warning 后返回 None,**绝不中断收官**(与成本归集同款兜底)。

    :param cfg: 全局配置(只读;仅取 ``data_dir`` 与附加属性
        ``batch_cost_budget``)。
    :param run_id: 本批收官标识(与成本归集步共用同一标识)。
    :return: 超限/未超限均返回 ``BudgetStatus``(调用方按 ``.over`` 分支);
        未配置 / 无账本 / 失败时 None。
    """
    budget = getattr(cfg, "batch_cost_budget", None)
    if budget is None:
        return None  # 未配置批预算:不检查(零行为)
    if (
        isinstance(budget, bool)
        or not isinstance(budget, (int, float))
        or float(budget) < 0
    ):
        logger.warning(
            "批预算配置 batch_cost_budget 非法(需非负数值,元;已跳过预算检查,"
            "不中断收官):%r", budget,
        )
        return None
    data_dir = pathlib.Path(str(getattr(cfg, "data_dir", "data") or "data"))
    ledger = data_dir / _COST_LEDGER_NAME
    if not ledger.exists():
        return None  # 无账本即无账可查:与成本归集步同门槛,零行为
    try:
        from netsentinel.vision.cost_meter import CostMeter  # 领地内兄弟,惰性导入

        status = CostMeter(ledger).check_budget(
            run_id=run_id, max_cost=float(budget)
        )
    except Exception as exc:  # noqa: BLE001 - 预算检查失败绝不中断收官
        logger.warning("批预算检查失败(已跳过,不中断收官):%s", exc)
        return None
    if status is not None and status.over:
        telemetry.inc(_METRIC_BUDGET_EXCEEDED)
    return status


def _print_budget_alert(run_id: str, status: Any) -> None:
    """中文醒目告警批预算超限(只告警:证据包/复核队列不受影响,哨兵不是执行器)。"""
    exceed = round(float(status.spent) - float(status.max_cost), 2)
    print("⚠️ " + "─" * 6 + " 批预算超限告警 " + "─" * 6 + " ⚠️")
    print(f"批次标识: {run_id}")
    print(
        f"本批费用: {status.spent:.2f} 元 / 预算 {status.max_cost:.2f} 元"
        f"(超出 {exceed:.2f} 元,共 {status.calls} 次调用)"
    )
    if status.unpriced_calls:
        print(
            f"无价格提示调用: {status.unpriced_calls} 次"
            "(未计入上述费用,实际可能更高;以账单为准,红线 18)"
        )
    print(f"处置建议: {status.advice}")
    print(
        "哨兵不是执行器:已产出的证据包、成本账与复核队列保持原样,"
        "本流程不删除任何文件、不改任何队列状态;后续是否继续扫描由人工决定。"
    )


# ---------------------------------------------------------------------------
# ① 收官流程:tier_once → batch_scan → SummaryAgent → 中文收官汇总
# ---------------------------------------------------------------------------
def finish(
    urls: list[str],
    cfg: Config,
    *,
    scan: Callable[..., Any] | None = None,
    summary: Any | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """收官流程:并发档位初始化 → 并发扫描 → 结案汇总 → 成本归集 → 批预算哨兵 → 打印中文收官汇总。

    流程(兄弟模块全惰性,可整体注入替换):

    1. A165 ``tier_once(cfg)``:并发档位一次性初始化(``concurrency_auto``
       且无持久档时探测写入建议;**读取优先级 cfg 字段 > 持久文件**);
    2. A164 ``io_workers(cfg)`` 计算 worker 数(§1 三档公式;红线 35:
       只作用于扫描池的本地并发,**不触碰** ``fetch_delay_s`` 等礼貌参数,
       ``cfg`` 原对象原样透传,绝不改写);
    3. ``scan`` 注入则以 ``scan(urls, cfg, workers=n)`` 调用(注入的扫描
       函数需接受可选 ``workers=`` 关键字);缺省惰性 A108 ``batch_scan``,
       worker 数经 :func:`_pool_runner_with_workers` 包装透传给 A58
       ``run_pool``;
    3.5 **V12 trace 树落盘**:扫描结果(``reports``)中携带
       ``intel["trace_id"]`` 的报告(trace_enabled 开启才会产生),经
       :func:`_export_scan_traces` 逐站点导出 span 树到
       ``<cfg.data_dir>/runs/<站点标识>/trace.json``;无 trace_id 时零
       行为变化,落盘失败只告警计数、绝不中断;
    4. ``summary`` 注入则按鸭子调用(优先 ``.run(scan_result, cfg)``,
       否则直接调用);缺省惰性 A168 ``SummaryAgent().run`` → 做分类汇总、
       收官报告与**举报准备**(列待声明组,仅统计不激活,红线 36);
    5. **V13 批次成本归集**(:func:`_close_out_cost`):全局成本账本
       ``<cfg.data_dir>/vlm_cost.jsonl`` 存在时,聚合(by=model 明细 +
       by=run_id 总账)写 ``<cfg.data_dir>/runs/<批次标识>/cost.jsonl``
       (**永远写**),pyarrow 可得(惰性 import)时追加导出
       ``cost.parquet``(失败降级 jsonl + 中文 warning + 计数
       ``finishflow.cost_export.failed``),并打印中文成本表;账本不存在
       零行为;任何失败只告警计数、**绝不中断收官**;
    6. **A232 批预算哨兵检查**(:func:`_check_batch_budget`,**哨兵不是
       执行器**):cfg 附加属性 ``batch_cost_budget``(getattr 缺省
       None = 不检查,零行为)为数值时聚合该批(``run_id``)费用,
       **超限** → 计数 ``finishflow.budget.exceeded`` + result 标记
       ``budget_exceeded=True`` 与 ``budget`` 明细(spent / max_cost /
       unpriced_calls / calls / advice)+ 中文醒目告警;**只告警与
       标记——绝不删除已产出的证据包/成本账/trace,绝不改复核队列与
       批次状态**,"停止发起后续扫描"仅为人工建议;
    7. 打印中文收官汇总(站点/扫描/失败/组数/待声明组/就绪条目/档位/
       worker 数)与"下一步"指引(TUI 逐组声明 → ``--report`` 举报准备);
    8. 返回 outcome dict(A168 SummaryOutcome 字段 + 站点/扫描/失败/跳过
       等扫描计数,下游与测试可直接断言;成本归集实际发生时附
       ``run_id`` / ``cost_jsonl`` / ``cost_parquet``(可选)键;批预算
       超限时附 ``budget_exceeded`` / ``budget`` 键)。

    :param urls: 目标 URL 列表(原样交给扫描;去重/准入由扫描链负责)。
    :param cfg: 全局配置(只读透传;礼貌参数不随档位放宽,红线 35)。
    :param scan: 注入的批量扫描函数 ``(urls, cfg, *, workers) -> dict``。
    :param summary: 注入的结案汇总器(``.run`` 方法或可调用)。
    :param run_id: 本批收官标识(缺省本地时间 ``run-YYYYMMDD-HHMMSS``;
        目录名经安全化,见 :func:`_safe_run_dirname`;⑤ 成本归集与
        ⑥ 批预算检查共用同一标识)。
    :return: outcome dict(见上文第 7 步)。
    :raises RuntimeError: 缺省依赖(A165/A164/A108/A168/ops.pool)未就位,
        中文消息指明模块。
    """
    targets = [str(u) for u in (urls or [])]

    # ① 并发档位一次性初始化(A165;cfg.concurrency_tier 显式配置优先)
    tier_once = _load("netsentinel.ops.tier_state", "tier_once")
    tier_once(cfg)

    # ② worker 数(A164;只影响扫描池并发,礼貌参数不动——红线 35)
    io_workers = _load("netsentinel.ops.concurrency", "io_workers")
    workers = int(io_workers(cfg))

    # ③ 并发扫描(A108;workers 透传给注入 scan 或 pool_runner)
    if scan is not None:
        with telemetry.timer(_METRIC_FINISH_SCAN):
            scan_result = scan(targets, cfg, workers=workers)
    else:
        batch_scan = _load("netsentinel.ops.batch_scan", "batch_scan")
        pool_runner = _pool_runner_with_workers(workers)
        with telemetry.timer(_METRIC_FINISH_SCAN):
            scan_result = batch_scan(targets, cfg, pool_runner=pool_runner)
    scan_result = dict(scan_result or {})
    scan_summary = dict(scan_result.get("summary") or {})
    reports = dict(scan_result.get("reports") or {})
    errors = list(scan_summary.get("errors") or [])
    scanned = int(scan_summary["done"]) if scan_summary.get("done") is not None else len(reports)
    failed = int(scan_summary["failed"]) if scan_summary.get("failed") is not None else len(errors)
    skipped = int(scan_summary.get("skipped", 0) or 0)

    # ③.5 trace 树落盘(V12 接线收口):trace_enabled 开启的扫描会在
    # report.intel["trace_id"] 携带 trace 号,收官时逐站点导出 span 树;
    # 无 trace_id 零行为,任何失败只告警计数、绝不中断(见 _export_scan_traces)。
    trace_json_paths = _export_scan_traces(reports, cfg)

    # ④ 结案汇总 + 举报准备(A168;零提交路径——红线 36)
    if summary is not None:
        summary_run = getattr(summary, "run", None)
        if callable(summary_run):
            outcome_raw = summary_run(scan_result, cfg)
        elif callable(summary):
            outcome_raw = summary(scan_result, cfg)
        else:
            raise RuntimeError(
                "注入的结案汇总器既不可调用也未提供 run(scan_result, cfg) 方法:"
                f"{type(summary).__name__}"
            )
    else:
        summary_cls = _load("netsentinel.agent.summary_agent", "SummaryAgent")
        outcome_raw = summary_cls().run(scan_result, cfg)
    with telemetry.timer(_METRIC_FINISH_SUMMARY):
        outcome = _outcome_dict(outcome_raw)

    result: dict[str, Any] = dict(outcome)
    result["groups"] = _count(result.get("groups"))
    result["attest_pending"] = _count(result.get("attest_pending"))
    result["ready_count"] = _count(result.get("ready_count"))
    result.setdefault("tier", str(getattr(cfg, "concurrency_tier", "mid") or "mid"))
    if result.get("workers") in (None, ""):
        result["workers"] = workers
    else:
        result["workers"] = int(result["workers"])
    result["sites"] = len(targets)
    result["scanned"] = scanned
    result["failed"] = failed
    result["skipped"] = skipped
    if trace_json_paths:
        # 仅在实际落盘时携带(无 trace_id 的收官结果形态与旧版逐字节一致)
        result["trace_json_paths"] = trace_json_paths

    # ⑤ 批次成本归集(V13 收官新增步):账本存在才聚合落盘 runs/<批次>/
    # cost.jsonl(+ 可选 cost.parquet)并打印中文成本表;账本不存在零行为;
    # 任何失败只中文 warning + 计数 finishflow.cost_export.failed、绝不中断。
    # run_id 只生成一次:⑤ 归集与 ⑥ 预算检查共用同一批次标识。
    effective_run_id = str(run_id) if run_id is not None else _default_run_id()
    cost = _close_out_cost(cfg, effective_run_id)
    if cost is not None:
        result["run_id"] = cost["run_id"]
        result["cost_jsonl"] = cost["cost_jsonl"]
        if cost["cost_parquet"]:
            result["cost_parquet"] = cost["cost_parquet"]

    # ⑥ 批预算哨兵检查(A232 收官新增步):cfg 附加属性 batch_cost_budget
    # (getattr 缺省 None = 不检查,零行为);数值则聚合该批费用,超限时
    # 计数 finishflow.budget.exceeded + result 标记 budget_exceeded=True 与
    # 明细,并中文醒目告警——**只告警与标记,绝不删除已产出文件、绝不改
    # 队列状态**(哨兵不是执行器,"停止后续扫描"仅为人工建议)。
    budget_status = _check_batch_budget(cfg, effective_run_id)
    if budget_status is not None and budget_status.over:
        result["budget_exceeded"] = True
        result["budget"] = {
            "run_id": effective_run_id,
            "spent": budget_status.spent,
            "max_cost": budget_status.max_cost,
            "unpriced_calls": budget_status.unpriced_calls,
            "calls": budget_status.calls,
            "advice": budget_status.advice,
        }

    _print_finish_summary(result)
    if cost is not None:
        _print_cost_summary(cost)
    if budget_status is not None and budget_status.over:
        _print_budget_alert(effective_run_id, budget_status)
    _print_finish_next_steps(result)
    telemetry.inc(_METRIC_FINISH)
    logger.info(
        "收官流程完成:站点 %d/扫描 %d/失败 %d/组 %d/待声明 %d/ready %d/"
        "tier %s/workers %d/trace %d",
        result["sites"], result["scanned"], result["failed"], result["groups"],
        result["attest_pending"], result["ready_count"], result["tier"],
        result["workers"], len(trace_json_paths),
    )
    return result


def _print_finish_summary(r: dict[str, Any]) -> None:
    """中文打印收官汇总(站点/扫描/失败/组数/待声明组/就绪/档位/worker 数)。"""
    print("── 收官汇总(结案代理,批量流程收官)──")
    print(f"站点    : {r['sites']}")
    print(f"扫描成功: {r['scanned']}(指纹未变跳过 {r['skipped']})")
    if r["failed"]:
        print(f"扫描失败: {r['failed']} 个(失败不中断整批,详见扫描日志)")
    else:
        print("扫描失败: 0 个")
    print(f"案件组数: {r['groups']}")
    print(
        f"待声明组: {r['attest_pending']}"
        "(batch_tui 逐组声明后方可进入批量举报,红线 25)"
    )
    print(f"就绪条目: {r['ready_count']}(已声明组的待举报条目)")
    print(
        f"并发档位: {r['tier']}(worker {r['workers']} 个;"
        "礼貌间隔与举报频控不随档位放宽,红线 35)"
    )
    traces = r.get("trace_json_paths")
    if traces:
        print(f"trace 树 : {len(traces)} 份已落盘(data/runs/<站点>/trace.json)")


def _print_finish_next_steps(r: dict[str, Any]) -> None:
    """打印后续步骤指引(优先 A168 next_steps;缺省给固定两步)。"""
    print("下一步:")
    steps = r.get("next_steps")
    if isinstance(steps, str):
        steps = [steps]
    for step in list(steps or []):
        print(f"  · {step}")
    if not steps:
        print(
            "  1) python -m netsentinel.cli.batch_tui → "
            "attest <组名> --reviewer <审核人> 逐组声明"
        )
        print("  2) python -m netsentinel.finishflow --report(批量举报准备,默认干跑)")
    print("  红线 36:结案代理只做汇总与举报准备,全程无任何自动提交路径。")


# ---------------------------------------------------------------------------
# ② 举报准备:ready_entries → SequentialReportAgent(默认干跑)
# ---------------------------------------------------------------------------
def run_report(
    cfg: Config,
    *,
    items: list[dict[str, Any]] | None = None,
    runner: Any | None = None,
    dry_run: bool | None = None,
    batch_id: int | None = None,
) -> dict[str, Any]:
    """举报准备:就绪清单 → (续批)→ SequentialReportAgent 批量举报编排。

    流程(兄弟模块全惰性,可注入替换;**默认干跑**,红线 36):

    1. ``items`` 给定则直接用作条目;缺省惰性 A110 ``ready_entries(cfg)``
       取"待批量举报"就绪清单(已确认且所在组已完成人工核实声明,
       红线 25 在上游收口);
    2. ``batch_id`` 给定则经 A113 ``BatchState`` 续批:批次有未完条目 →
       与就绪清单按 ``entry_id`` 合并补齐 portal 等执行细节后沿用该批次;
       批次已完且就绪清单非空 → 按清单新建批次(note 留痕);无未完且
       清单为空 → 零值返回。``batch_id`` 缺省且清单非空 → 同样新建批次;
    3. ``runner`` 注入则按鸭子调用(优先 ``.run(items, cfg, dry_run=,
       state=)``;否则直接调用);缺省惰性 A174 ``SequentialReportAgent``
       ——内部直接编排 A112 ``run_batch``(恒 ``auto_confirm=False``,
       逐条 HUMAN_GATE,频控沿用 V6 链,红线 36),状态器以
       ``state=st.bound(batch_id)`` 绑定批次;
    4. 返回 ``{"result": run_batch 摘要 dict, "report_path": 批次报告路径}``
       (路径由 A174 结束后经 A114 ``batch_report`` 渲染产生,鸭子取
       ``report_path`` / ``report`` 键,取不到为 ``None``)。

    dry_run 语义(红线 36):``dry_run=None``(缺省)取
    ``cfg.dry_run_default``(默认 True);真实执行必须**显式**
    ``dry_run=False``——本模块对 runner 原样透传,绝不包装、绝不出现
    ``auto_confirm=True`` 的路径。

    :raises RuntimeError: A110/A113/A174 未就位,中文消息指明模块。
    :raises ValueError: 续批批次不存在 / 建批条目缺字段(A113 中文消息)。
    """
    effective_dry_run = (
        bool(getattr(cfg, "dry_run_default", True)) if dry_run is None else bool(dry_run)
    )

    if items is not None:
        ready_items = [dict(item) for item in items]
    else:
        ready_fn = _load("netsentinel.decision.batch_review", "ready_entries")
        ready_items = [dict(item) for item in (ready_fn(cfg) or [])]

    batch_state_cls = _load("netsentinel.submit.batch_state", "BatchState")
    st = batch_state_cls(getattr(cfg, "db_path", "data/review_queue.db"))
    try:
        bid = batch_id
        if batch_id is not None:
            unfinished = list(st.resume(batch_id) or [])
            ready_n = len(ready_items)
            if unfinished:
                ready_items = _merge_resumed_items(unfinished, ready_items)
                telemetry.inc(_METRIC_REPORT_CONTINUE)
                print(
                    f"续批批次 {batch_id}:未完条目 {len(unfinished)} 条"
                    f"(就绪清单 {ready_n} 条,按 entry_id 合并补齐执行细节)"
                )
            elif ready_items:
                bid = st.new_batch(ready_items, note=_NEW_BATCH_NOTE)
                telemetry.inc(_METRIC_REPORT_NEW)
                print(
                    f"批次 {batch_id} 无未完条目,已按就绪清单新建批次 {bid}"
                    f"(共 {len(ready_items)} 条)"
                )
        elif ready_items:
            bid = st.new_batch(ready_items, note=_NEW_BATCH_NOTE)
            telemetry.inc(_METRIC_REPORT_NEW)
            print(f"已按就绪清单新建批次 {bid}(共 {len(ready_items)} 条)")

        if not ready_items:
            telemetry.inc(_METRIC_REPORT_IDLE)
            print(
                "无待举报条目:就绪清单为空"
                + (f"(批次 {batch_id} 亦无未完条目)" if batch_id is not None else "")
                + "(未新建批次、未执行任何动作)"
            )
            result: dict[str, Any] = {
                "submitted": 0,
                "failed": 0,
                "rate_limited": False,
                "paused": False,
                "results": [],
                "note": "无待举报条目,未执行",
            }
            report_path: str | None = None
        else:
            if runner is not None:
                runner_run = getattr(runner, "run", None)
                if callable(runner_run):
                    run_fn: Callable[..., Any] = runner_run
                elif callable(runner):
                    run_fn = runner
                else:
                    raise RuntimeError(
                        "注入的举报编排器既不可调用也未提供 "
                        f"run(items, cfg, ...) 方法:{type(runner).__name__}"
                    )
            else:
                agent_cls = _load(
                    "netsentinel.agent.sequential_report", "SequentialReportAgent"
                )
                run_fn = agent_cls().run
            if not effective_dry_run:
                print(
                    f"真实执行模式:批次 {bid} 共 {len(ready_items)} 条,将依次举报;"
                    "每条仍需人工输入验证码并最终确认(红线 36,无任何自动确认)。"
                )
            with telemetry.timer(_METRIC_REPORT):
                result = dict(run_fn(ready_items, cfg, dry_run=effective_dry_run,
                                     state=st.bound(bid)))
            report_path = (
                result.get("report_path") or result.get("report") or None
            )
    finally:
        close = getattr(st, "close", None)
        if callable(close):  # 缺省状态器归本函数所有,用毕即关
            close()

    result.setdefault("batch_id", bid)
    telemetry.inc(_METRIC_REPORT)
    _print_report_summary(result, report_path, effective_dry_run)
    logger.info(
        "举报准备完成:批次 %s dry_run=%s submitted=%d failed=%d report=%s",
        bid, effective_dry_run, result.get("submitted", 0),
        result.get("failed", 0), report_path or "(无)",
    )
    return {"result": result, "report_path": report_path}


def _print_report_summary(
    result: dict[str, Any], report_path: str | None, dry_run: bool
) -> None:
    """中文打印举报准备摘要与后续指引(挂起/暂停时提示续批命令)。"""
    mode = "干跑演练(dry_run,未真实提交)" if dry_run else "真实执行(逐条人工门)"
    print(f"── 收官举报摘要({mode})──")
    print(f"批次号  : {result.get('batch_id')}")
    print(f"条目总数: {result.get('total', 0)}")
    print(f"提交成功: {result.get('submitted', 0)} 条")
    print(f"失败    : {result.get('failed', 0)} 条")
    if result.get("note"):
        print(f"说明    : {result['note']}")
    print(f"批次报告: {report_path or '(未生成)'}")
    if result.get("rate_limited") or result.get("paused"):
        print(
            "可续批  : python -m netsentinel.finishflow --report --resume "
            f"{result.get('batch_id')} --exec(额度恢复/确认继续后)"
        )
    print(
        "红线 36:逐组人工声明 + 逐条人工门,全程无自动确认;"
        "红线 26:举报频控与每日额度未放宽。"
    )


# ---------------------------------------------------------------------------
# ③ CLI:python -m netsentinel.finishflow
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m netsentinel.finishflow",
        description=(
            "净网哨兵收官 CLI:批量扫描收官汇总(--input)→ TUI 逐组声明 → "
            "批量举报准备(--report);举报仍是逐组声明、逐条人工门(红线 36)"
        ),
    )
    parser.add_argument(
        "--input",
        default=None,
        help="批量清单路径(.txt/.csv/.yaml/.yml):收官流程(档位→扫描→结案汇总),不提交",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径(默认 ./config.yaml,缺失则用默认配置)",
    )
    parser.add_argument(
        "--tier",
        choices=("low", "mid", "high"),
        default=None,
        help=(
            "并发档位覆盖 low|mid|high(覆盖 cfg.concurrency_tier 再跑);"
            "只影响本地并发,礼貌间隔/举报频控不变(红线 35)"
        ),
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="举报准备模式:就绪清单 → SequentialReportAgent(默认干跑);与 --input 互斥",
    )
    parser.add_argument(
        "--precheck",
        action="store_true",
        help=(
            "批量预审模式(V10.1):一次确认全部就绪条目,然后逐条执行;"
            "每条自动填写+聚焦验证码框,你只需在浏览器输入验证码+回终端回车"
        ),
    )
    parser.add_argument(
        "--resume",
        type=int,
        default=None,
        metavar="批次号",
        help="续批批次号(仅 --report 模式有效;批次无未完条目时按就绪清单新建)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="干跑模式(默认;举报只演练,不真实提交)",
    )
    parser.add_argument(
        "--exec",
        action="store_true",
        help=(
            "真实执行模式:仅 --report 模式有效,与 --dry-run 互斥;"
            "每条举报仍需人工输入验证码并最终确认(红线 36)"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口:收官流程 / 举报准备两模式 + ``--tier`` 档位覆盖。

    模式(互斥):

    - ``--input PATH``:**收官流程**(:func:`finish`;A107 ``load_bulk``
      加载清单,空清单直接返回),恒不提交;结束后打印"下一步"
      (batch_tui 声明 → ``--report``);
    - ``--report``:**举报准备**(:func:`run_report`,默认干跑;真实执行
      必须显式 ``--exec``,内部仍是逐组声明 + 逐条人工门,红线 36);
      ``--resume 批次号`` 续批。

    ``--tier low|mid|high``:经 ``dataclasses.replace`` 生成新 Config 覆盖
    ``concurrency_tier`` 再跑,其余字段(含 ``fetch_delay_s`` 等礼貌参数)
    原样保留(红线 35)。

    返回码:0 成功;2 用法/配置/清单错误(ValueError);3 兄弟模块未就位或
    对接约定不满足(RuntimeError,中文消息)。
    """
    args = _build_parser().parse_args(argv)

    if args.dry_run and args.exec:
        print("错误:--dry-run 与 --exec 互斥,真实执行请只给 --exec")
        return 2
    if args.input and args.report:
        print("错误:--input(收官流程)与 --report(举报准备)互斥,请分开执行")
        return 2
    if args.input and args.precheck:
        print("错误:--input(收官流程)与 --precheck(批量预审)互斥")
        return 2
    if args.report and args.precheck:
        print("错误:--report 与 --precheck 互斥(两种举报执行模式)")
        return 2
    if args.exec and not args.report and not args.precheck:
        print("错误:--exec 仅在 --report 或 --precheck 模式下有效(收官流程恒不提交)")
        return 2
    if args.resume is not None and not args.report:
        print("错误:--resume 仅在 --report 举报准备模式下有效")
        return 2
    if not args.input and not args.report and not args.precheck:
        print("错误:需要 --input(收官)或 --report(举报准备)或 --precheck(批量预审)之一")
        return 2

    from netsentinel.config import load_config  # 惰性:纯用法错误不触碰配置

    try:
        cfg = load_config(args.config)
    except ValueError as exc:
        print(f"配置错误:{exc}")
        return 2
    if args.tier is not None:
        # 红线 35:replace 生成新对象,礼貌/频控字段原样保留,绝不原地改写
        cfg = dataclasses.replace(cfg, concurrency_tier=args.tier)
        print(f"已覆盖并发档位:{args.tier}(礼貌间隔与举报频控不变,红线 35)")

    try:
        if args.precheck:
            from netsentinel.submit.batch_precheck import BatchPrecheck

            pc = BatchPrecheck(cfg)
            dr = args.exec  # --exec = 真实;默认干跑
            pc.run(dry_run=dr)
        elif args.report:
            # CLI 强制显式:--exec → dry_run=False(真实);否则恒干跑
            run_report(cfg, batch_id=args.resume, dry_run=not args.exec)
        else:
            load_bulk = _load("netsentinel.ops.bulk_intake", "load_bulk")
            valid, _rejected = load_bulk(args.input)
            valid = [str(u) for u in (valid or [])]
            if not valid:
                print(f"清单为空,未执行收官流程:{args.input}")
                return 0
            finish(valid, cfg)
    except ValueError as exc:
        print(f"错误:{exc}")
        return 2
    except RuntimeError as exc:
        print(f"错误:{exc}")
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    raise SystemExit(main())
