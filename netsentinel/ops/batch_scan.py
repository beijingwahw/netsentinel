"""NetSentinel(净网哨兵)批量扫描编排(ops.batch_scan,A108)。

把 V6 批量案件流水线的**前半段**串起来:并发扫描 → 归组 → 合并证据 →
每组入列一条。本模块只做"顺序编排",不引入任何新的自动决策:

1. :func:`batch_scan`:复用 A58 ``ops.pool.run_pool``(惰性导入,可注入
   ``pool_runner`` 替换)对一批 URL 做**有界并发扫描**,并借"包裹扫描函数"
   把每个 URL 产出的 :class:`~netsentinel.contracts.SiteReport` 原对象
   捕获到 ``reports`` 字典里(pool 本身只回汇总,不回报告对象);失败的
   URL 不进 ``reports``,只体现在 ``summary["errors"]`` 里(失败隔离,
   与 pool 契约一致)。
2. :func:`group_and_enqueue`:把 ``reports`` 转成**伪 entries**(每份报告
   一条,``entry_id`` 用负数索引占位,避免与复核队列里的真实自增 id 冲突)
   → A104 ``case_group.group_entries`` 归组(惰性)→ A105
   ``group_linker.merge_groups`` 团伙归并(惰性,阈值取 cfg 的
   ``group_merge_phash_overlap`` / ``group_merge_template``)→ 每个需复核
   组构造一份**合并报告**(agg 取组内 agg_max、verdict 取最严重档、pages
   取组内全部报告的 pages 并集)交给 A11 ``evidence.packager.build_bundle``
   打包(惰性,可注入 fake)→ 复核队列 ``add`` 一条(note 前缀
   ``[组:组名]``,标注该条来自哪个案件组)。

红线与安全约定:
- **红线 24(批量仍是逐条人工门)**:本模块入列的条目一律是 ``pending``,
  与单站流程完全同队;这里没有任何提交动作,举报仍须走人工复核 →
  executor 人工门的既有链路;
- **CLEAN 组不入列**:判定为 clean(且 agg 未达复核阈值)的组只计数
  ``skipped_clean``,不打包、不入列,避免污染人工队列;
- 注入的 ``queue.add`` 若不支持 ``note`` 关键字(既有
  ``decision.review_queue.ReviewQueue.add`` 即如此),自动回退两参调用,
  组名同时落在合并报告的 ``intel`` 里(manifest 随证据包落盘,不丢失);
- 兄弟模块(A104/A105/A109 等)并行开发中:全部**惰性导入 + 鸭子注入**,
  未就位时抛中文 :class:`RuntimeError` 指明缺失模块,测试可全量离线注入。

可观测性:``batch.scan.reports`` / ``batch.groups`` / ``batch.enqueued`` /
``batch.skipped_clean`` 计数与 ``batch.enqueue`` 计时(仅名称与数字,
不涉及站点内容)。

仅使用标准库;Python 3.10+;全部依赖可注入,零网络、零外呼。
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from netsentinel import telemetry
from netsentinel.contracts import SiteReport, Verdict

__all__ = ["PseudoEntry", "batch_scan", "group_and_enqueue"]

logger = logging.getLogger(__name__)

#: 判定档位(clean < suspect < nsfw);未知档位按 suspect 处理(安全方向:
#: 宁可多送人工复核,绝不漏放)
_VERDICT_SEVERITY: dict[str, int] = {"clean": 0, "suspect": 1, "nsfw": 2}
_UNKNOWN_SEVERITY = 1

#: V6 归组参数缺省值(与 contracts.Config 的 V6 字段缺省一致;鸭子 cfg
#: 缺字段时兜底)
_DEFAULT_PHASH_OVERLAP = 0.3
_DEFAULT_MERGE_TEMPLATE = True

#: 组入列时 note 前缀模板:"[组:组名]"
NOTE_PREFIX = "[组:{name}]"


# ---------------------------------------------------------------------------
# 伪 entry:reports → EntryLike(A104 契约:id/site_url/verdict/evidence_zip)
# ---------------------------------------------------------------------------
@dataclass
class PseudoEntry:
    """由单份扫描报告临时构造的复核条目(鸭子 EntryLike,A104 契约)。

    ``id`` 用**负数索引占位**(-1, -2, ...):既满足 A104 按
    ``reports: dict[entry_id, SiteReport]`` 取报告的签名,又不会与复核
    队列里的真实自增 id(恒正)混淆。该对象只活在归组过程中,不落库。
    """

    id: int
    site_url: str
    verdict: str
    evidence_zip: str = ""


# ---------------------------------------------------------------------------
# 惰性导入辅助(兄弟模块并行开发中,未就位抛中文 RuntimeError)
# ---------------------------------------------------------------------------
def _lazy_import(module_name: str, attr: str) -> Any:
    """惰性导入 ``module_name.attr``;ImportError/缺属性 → 中文 RuntimeError。"""
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(f"模块 {module_name} 未就位:{exc}") from exc
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise RuntimeError(f"模块 {module_name} 未提供 {attr}:{exc}") from exc


def _verdict_str(value: Any) -> str:
    """把 Verdict 枚举或字符串统一成小写字符串(取不到返回空串)。"""
    raw = getattr(value, "value", value)
    return str(raw) if raw is not None else ""


def _as_float(value: Any, default: float = 0.0) -> float:
    """宽容浮点转换(duck 报告里可能是 None / 字符串)。"""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _severity(verdict: Any) -> int:
    """判定档位:clean=0 / suspect=1 / nsfw=2;未知按 suspect(安全方向)。"""
    return _VERDICT_SEVERITY.get(_verdict_str(verdict), _UNKNOWN_SEVERITY)


# ---------------------------------------------------------------------------
# 批量扫描:并发池 + 报告捕获
# ---------------------------------------------------------------------------
def batch_scan(
    urls: Iterable[str],
    cfg: Any,
    *,
    run_scan: Callable[[str, Any], Any] | None = None,
    memory: Any | None = None,
    pool_runner: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """并发扫描一批 URL 并捕获每站报告,返回 ``{"reports", "summary"}``。

    流程:

    1. 把 ``run_scan`` 包一层**捕获壳**:壳按 ``(url, cfg)`` 调真实扫描,
       返回前把报告对象按 URL 存入本函数局部的 ``captured`` 字典——
       pool 只回汇总不回报告,捕获壳是把两者拼齐的唯一挂点;
    2. ``pool_runner`` 注入则用之(鸭子复刻 ``ops.pool.run_pool`` 签名:
       ``(cfg, items, *, run_scan=None, memory=None, ...) -> 摘要 dict``),
       缺省惰性导入 ``netsentinel.ops.pool.run_pool``(有界并发、礼貌
       间隔、失败隔离、指纹去重,详见 A58);``run_scan`` / ``memory``
       原样透传(缺省扫描函数惰性导入
       ``netsentinel.pipeline.orchestrator.run_scan``);
    3. 返回 ``{"reports": {url: SiteReport}, "summary": pool 摘要 dict}``:
       **失败的 URL 不在 ``reports`` 里**,但出现在
       ``summary["errors"] == [(url, 中文原因), ...]`` 中(与 pool 契约
       一致);``skipped``(指纹未变)的 URL 同样不产生新报告条目。

    :param urls: 目标 URL 列表(去重/准入由 pool 负责,本函数不扩大范围)。
    :param cfg: 全局配置(透传给扫描函数与 pool)。
    :param run_scan: 注入的单站扫描函数 ``(url, cfg) -> SiteReport``。
    :param memory: 注入的站点指纹记忆(A33 API;透传给 pool)。
    :param pool_runner: 注入的并发池运行器(测试离线注入;缺省真池)。
    :return: ``{"reports": {url: report}, "summary": {...pool 摘要...}}``。
    :raises RuntimeError: 缺省依赖(orchestrator / ops.pool)未就位时,
        中文消息指明缺失模块。
    """
    targets = [str(u) for u in (urls or [])]
    captured: dict[str, Any] = {}

    if run_scan is not None:
        scan_fn = run_scan
    else:
        scan_fn = _lazy_import("netsentinel.pipeline.orchestrator", "run_scan")

    def _capture_scan(url: str, cfg_: Any) -> Any:
        report = scan_fn(url, cfg_)
        captured[str(url)] = report
        return report

    if pool_runner is not None:
        runner = pool_runner
    else:
        runner = _lazy_import("netsentinel.ops.pool", "run_pool")

    logger.info("批量扫描开始:目标 %d 个", len(targets))
    summary = runner(cfg, targets, run_scan=_capture_scan, memory=memory)
    telemetry.inc("batch.scan.reports", len(captured))
    logger.info(
        "批量扫描结束:捕获报告 %d 份(失败/跳过见 summary)", len(captured)
    )
    return {"reports": captured, "summary": summary}


# ---------------------------------------------------------------------------
# 归组 + 合并证据 + 入列
# ---------------------------------------------------------------------------
def _to_pseudo_entries(
    reports: Mapping[str, Any],
) -> tuple[list[PseudoEntry], dict[int, Any]]:
    """reports → (伪 entries, {entry_id: report})。

    每份报告一条伪 entry,id 取负数索引占位(-1 起);``verdict`` 统一为
    字符串档位;``evidence_zip`` 若此前的扫描流程把路径放在
    ``report.intel["evidence_zip"]`` 则带出,否则留空(组级证据以本模块
    重新打包的合并 zip 为准)。
    """
    entries: list[PseudoEntry] = []
    reports_by_id: dict[int, Any] = {}
    for idx, (url, report) in enumerate(reports.items()):
        intel = getattr(report, "intel", None)
        zip_path = ""
        if isinstance(intel, Mapping):
            zip_path = str(intel.get("evidence_zip", "") or "")
        entry = PseudoEntry(
            id=-(idx + 1),
            site_url=str(url),
            verdict=_verdict_str(getattr(report, "verdict", "")),
            evidence_zip=zip_path,
        )
        entries.append(entry)
        reports_by_id[entry.id] = report
    return entries, reports_by_id


def _group_name(group: Any) -> str:
    """组名(CaseGroup.name;鸭子缺名时回退首个站点 URL,再退 "未命名组")。"""
    name = str(getattr(group, "name", "") or "")
    if name:
        return name
    urls = getattr(group, "site_urls", None) or []
    return str(urls[0]) if urls else "未命名组"


def _group_needs_review(group: Any, cfg: Any) -> bool:
    """组是否需要人工复核:verdict 非 clean 档,或 agg 达复核阈值。

    未知 verdict 档位按 suspect 处理(安全方向);clean 且 agg 低于
    ``cfg.review_threshold`` 的组计 ``skipped_clean``,不打包不入列。
    """
    if _severity(getattr(group, "verdict", "")) >= 1:
        return True
    agg = _as_float(getattr(group, "agg_max", 0.0))
    threshold = _as_float(getattr(cfg, "review_threshold", 0.50), 0.50)
    return agg >= threshold


def _coerce_verdict(value: Any) -> Any:
    """尽量归一成 :class:`Verdict` 枚举;归一失败原样返回(容忍鸭子)。"""
    try:
        return Verdict(_verdict_str(value))
    except ValueError:
        return value


def _member_reports(group: Any, reports: Mapping[str, Any]) -> list[Any]:
    """按组内 site_urls 顺序取成员报告(URL 不在 reports 里则跳过)。"""
    urls = getattr(group, "site_urls", None) or []
    return [reports[str(u)] for u in urls if str(u) in reports]


def _pick_primary(members: list[Any]) -> Any | None:
    """选组内"最严重成员"作合并报告的主站(档位 → agg,并列取先出现者)。"""
    best: Any | None = None
    best_key: tuple[int, float] = (-1, -1.0)
    for report in members:
        key = (
            _severity(getattr(report, "verdict", "")),
            _as_float(getattr(report, "agg_nsw_prob", 0.0)),
        )
        if key > best_key:
            best, best_key = report, key
    return best


def _build_group_report(group: Any, reports: Mapping[str, Any], note: str) -> SiteReport:
    """把一个案件组合并成单份 :class:`SiteReport`(交给证据打包器)。

    - ``agg_nsw_prob`` 取组 ``agg_max``(缺属性时回退成员 agg 最大值);
    - ``verdict`` 取组最严重档(尽量归一为 Verdict 枚举);
    - ``pages`` / ``image_scores`` 取组内全部成员报告的**并集拼接**
      (按 site_urls 顺序),``nsw_image_count`` 求和;
    - ``site_url`` 取组内最严重成员的 URL(并列取先出现者),无成员时
      回退组内首个 URL;
    - ``intel`` 携带组名 / 别名 / 组内 URL 清单 / 队列 note(随 manifest
      落盘,即使队列不支持 note 关键字也不丢失组归属信息)。
    """
    members = _member_reports(group, reports)
    pages: list[Any] = []
    image_scores: list[Any] = []
    nsw_count = 0
    for report in members:
        pages.extend(getattr(report, "pages", None) or [])
        image_scores.extend(getattr(report, "image_scores", None) or [])
        nsw_count += int(_as_float(getattr(report, "nsw_image_count", 0)))
    agg_raw = getattr(group, "agg_max", None)
    if agg_raw is None:  # 鸭子组缺 agg_max:回退成员最大 agg
        agg_raw = max(
            (_as_float(getattr(r, "agg_nsw_prob", 0.0)) for r in members),
            default=0.0,
        )
    primary = _pick_primary(members)
    site_url = ""
    if primary is not None:
        site_url = str(getattr(primary, "site_url", "") or "")
    if not site_url:
        urls = getattr(group, "site_urls", None) or []
        site_url = str(urls[0]) if urls else _group_name(group)
    intel = {
        "group_name": _group_name(group),
        "group_aliases": list(getattr(group, "aliases", None) or []),
        "group_site_urls": [str(u) for u in (getattr(group, "site_urls", None) or [])],
        "queue_note": note,
    }
    return SiteReport(
        site_url=site_url,
        pages=pages,  # type: ignore[arg-type]
        image_scores=image_scores,  # type: ignore[arg-type]
        agg_nsw_prob=_as_float(agg_raw),
        nsw_image_count=nsw_count,
        verdict=_coerce_verdict(getattr(group, "verdict", Verdict.CLEAN)),
        needs_review=True,
        intel=intel,
    )


def _queue_add(queue: Any, report: SiteReport, zip_path: str, note: str) -> Any:
    """入列一条(组级)复核条目,note 前缀 ``[组:组名]``。

    优先尝试 ``queue.add(report, zip, note=note)``(支持 note 的鸭子队列,
    如 A110+ 的扩展队列);队列不支持 note 关键字时(既有
    ``decision.review_queue.ReviewQueue.add`` 的冻结签名即如此)回退两参
    调用并告警——组归属信息已随合并报告的 ``intel.queue_note`` 写进证据包
    manifest,不会丢失。队列对象没有 ``add`` 方法直接抛中文 RuntimeError。
    """
    add = getattr(queue, "add", None)
    if not callable(add):
        raise RuntimeError(
            "注入的复核队列对象未提供 add(report, evidence_zip) 方法,"
            f"无法入列:{type(queue).__name__}"
        )
    try:
        return add(report, zip_path, note=note)
    except TypeError:
        logger.warning(
            "复核队列 add 不支持 note 参数,已回退两参调用(组名改由证据包 "
            "manifest 的 intel.queue_note 留痕):%s", note,
        )
        return add(report, zip_path)


def group_and_enqueue(
    cfg: Any,
    *,
    reports: Mapping[str, Any] | None = None,
    queue: Any | None = None,
    packager: Callable[[Any, Any], Any] | None = None,
    linker: Callable[..., Any] | None = None,
    grouper: Callable[[list[Any], dict[int, Any]], list[Any]] | None = None,
) -> dict[str, Any]:
    """归组 → 合并证据 → 每组入列一条,返回 ``{"groups", "enqueued", "skipped_clean"}``。

    流程(全部依赖注入、离线可测;缺省实现惰性导入兄弟模块):

    1. ``reports`` 缺省空;空报告直接返回空结果(不触碰任何兄弟模块);
    2. 每份报告 → 一条伪 entry(:class:`PseudoEntry`,负数 id 占位)→
       ``grouper``(缺省 A104 ``case_group.group_entries``)按 canonical
       归并出**基础组**;
    3. ``linker``(缺省 A105 ``group_linker.merge_groups``)做团伙归并,
       阈值取 ``cfg.group_merge_phash_overlap`` / ``cfg.group_merge_template``
       (缺字段按 V6 缺省 0.3 / True 兜底);
    4. 逐组判定:需复核的组(verdict 非 clean 档,或 agg ≥
       ``cfg.review_threshold``)构造合并报告 → ``packager``(缺省
       A11 ``evidence.packager.build_bundle``)打证据包 → 队列
       ``add`` 一条(note 前缀 ``[组:组名]``,见 :func:`_queue_add`);
       clean 组只计 ``skipped_clean``,不打包不入列;
    5. 返回 ``{"groups": [CaseGroup, ...], "enqueued": n,
       "skipped_clean": m}``(``groups`` 为 linker 输出原对象列表)。

    红线 24:入列条目一律 pending,后续举报仍逐条走人工复核与 executor
    人工门——本函数没有任何提交动作。

    :raises RuntimeError: 缺省依赖(case_group / group_linker /
        packager / review_queue)未就位或注入对象不满足鸭子契约时,
        中文消息指明缺失模块/原因。
    """
    all_reports = {str(k): v for k, v in (reports or {}).items()}
    if not all_reports:
        return {"groups": [], "enqueued": 0, "skipped_clean": 0}

    entries, reports_by_id = _to_pseudo_entries(all_reports)

    if grouper is not None:
        grouper_fn = grouper
    else:
        grouper_fn = _lazy_import("netsentinel.intel.case_group", "group_entries")
    base_groups = grouper_fn(entries, reports_by_id)
    logger.info("基础归组完成:%d 组(来自 %d 份报告)", len(base_groups), len(entries))

    if linker is not None:
        linker_fn = linker
    else:
        linker_fn = _lazy_import("netsentinel.intel.group_linker", "merge_groups")
    overlap = _as_float(
        getattr(cfg, "group_merge_phash_overlap", _DEFAULT_PHASH_OVERLAP),
        _DEFAULT_PHASH_OVERLAP,
    )
    merge_template = bool(
        getattr(cfg, "group_merge_template", _DEFAULT_MERGE_TEMPLATE)
    )
    groups = list(
        linker_fn(base_groups, overlap_threshold=overlap, merge_template=merge_template)
    )
    logger.info("团伙归并完成:%d 组(重叠阈值 %.2f,模板并组=%s)",
                len(groups), overlap, merge_template)

    if packager is not None:
        packager_fn = packager
    else:
        packager_fn = _lazy_import("netsentinel.evidence.packager", "build_bundle")
    if queue is not None:
        queue_obj = queue
    else:
        review_queue_cls = _lazy_import(
            "netsentinel.decision.review_queue", "ReviewQueue"
        )
        queue_obj = review_queue_cls(str(getattr(cfg, "db_path", "data/review_queue.db")))

    enqueued = 0
    skipped_clean = 0
    try:
        with telemetry.timer("batch.enqueue"):
            for group in groups:
                name = _group_name(group)
                note = NOTE_PREFIX.format(name=name)
                if not _group_needs_review(group, cfg):
                    skipped_clean += 1
                    logger.info("组判定为 clean,不入列:%s", name)
                    continue
                merged = _build_group_report(group, all_reports, note)
                bundle = packager_fn(merged, cfg)
                zip_path = str(getattr(bundle, "zip_path", bundle) or "")
                _queue_add(queue_obj, merged, zip_path, note)
                enqueued += 1
                logger.info("案件组已入列:%s(证据包 %s)", note, zip_path or "(空)")
    finally:
        if queue is None:  # 缺省队列归本函数所有,用毕即关(注入的归调用方)
            close = getattr(queue_obj, "close", None)
            if callable(close):
                close()

    telemetry.inc("batch.groups", len(groups))
    telemetry.inc("batch.enqueued", enqueued)
    telemetry.inc("batch.skipped_clean", skipped_clean)
    return {"groups": groups, "enqueued": enqueued, "skipped_clean": skipped_clean}
