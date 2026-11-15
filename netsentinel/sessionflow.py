# -*- coding: utf-8 -*-
"""会话累积总流程 CLI(NetSentinel · 会话累积席,与 :mod:`sessionstore` 配套)。

把"跨会话累积 → 存档 → 续跑 → 确认举报"串成一条命令:

- :func:`run_session`(扫描 + 累积 + 存档):``--input`` 清单 / ``--url`` 单站
  → A107 ``load_bulk`` / ``plan_scan`` 规划(canonical 去重 + 指纹未变跳过)→
  A108 ``batch_scan`` 并发扫描 → A108 ``group_and_enqueue`` 归组入列(与 V6
  批量流水线同一链路,条目一律 pending)→ **逐份报告累积入会话库**(按
  canonical 网站名字归组,历史自动并档)→ 结束时写 JSON 存档。下次运行打开
  同一累积库,在上一次基础上继续累积。
- :func:`session_status`(查看):打印累积库全部网站组(判定 / 聚合分 / 扫描
  次数 / 举报状态)与统计。
- :func:`report_all`(确认举报):列出**全部待举报网站组**及各组队列条目
  状态 → 人工确认(审核人 + "我已逐组人工核实" y 确认)→ 确认动作 = 把
  各组 pending 条目置 approved(留审核人)+ 写批量声明(A110 ``attest``,
  红线 25 留痕)→ 按**网站名字逐组排序**交给 A112 ``run_batch`` 顺序举报
  (每条仍走 HUMAN_GATE 人工门)→ 全组提交完成即 ``mark_reported`` 收档
  → 存档刷新。
- :func:`archive_now`(存档):不动扫描,仅把当前累积写成 JSON 存档。

安全红线(与全仓一致,违反即缺陷):

- **红线 2(真实提交前必须人工确认)**:``report_all`` 的批准动作只发生在
  用户显式 y 确认之后(确认前零状态变更);``run_batch`` 内部恒以
  ``auto_confirm=False`` 调执行器——本模块不存在任何把 ``auto_confirm``
  置真的路径(源码级断言测试锁定);
- **红线 24(批量举报仍是逐条人工门)**:本模块对执行器只做原样透传,
  验证码输入与最终确认始终由人工完成;``--exec`` 是真实执行的唯一入口,
  缺省跟随 ``cfg.dry_run_default``(默认干跑);
- **红线 25(批量确认声明留痕)**:进入批量的每组都经 ``BatchReview.attest``
  写声明(审核人 + 含"人工核实"文本,sqlite + 审计留痕),未确认的组
  整组排除;
- **红线 26(批量频控不得放宽)**:频控与额度完全由 A112 ``run_batch``
  收口,本模块不提供任何放宽参数;条数超过 ``batch_max_items`` 自动
  分多批顺序执行,不绕过上限。

兄弟模块全部**惰性导入 + 鸭子注入**,未就位抛中文 :class:`RuntimeError`;
测试可全量离线注入、零外呼。

用法示例(fake 注入,离线)::

    from netsentinel.contracts import Config

    result = run_session(cfg, input_path="leads.txt", scanner=fake_scan,
                         grouping=fake_group)
    # result["store_stats"] == {"groups": 3, "pending": 3, ...}

    summary = report_all(cfg, reviewer="张三", prompt=fake_prompt,
                         submit=fake_run_batch)
"""
from __future__ import annotations

import argparse
import importlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config

from netsentinel.sessionstore import SessionStore

__all__ = ["archive_now", "main", "report_all", "run_session", "session_status"]

logger = logging.getLogger(__name__)

#: 待批量条目缺 portal 字段时的缺省门户(与 A110 DEFAULT_PORTAL 对齐)
PORTAL_DEFAULT: str = "12377"

#: 会话累积库 / 存档文件相对 ``cfg.data_dir`` 的路径
_STORE_SUBDIR = "session"
_STORE_NAME = "accumulation.db"
_ARCHIVE_NAME = "archive.json"

#: 新建批次的 note 前缀(与 batchflow 同风格;仅流程标注,不含站点内容)
_NEW_BATCH_NOTE: str = "[sessionflow] 会话累积举报批次(按网站名字分组)"

#: 声明文本(红线 25:必含"人工核实";审核人与时间戳由 attest 另行留痕)
_ATTEST_TEXT: str = "会话举报确认:已逐组人工核实全部证据,同意按网站名字分组举报"

# 遥测指标名(只记名称与数字,不记站点内容)
_METRIC_RUN = "sessionflow.run"
_METRIC_REPORT = "sessionflow.report"
_METRIC_STATUS = "sessionflow.status"
_METRIC_ARCHIVED = "sessionflow.archive"
_METRIC_APPROVED = "sessionflow.report.approved"
_METRIC_ATTESTED = "sessionflow.report.attested"
_METRIC_CONFIRM_SKIPPED = "sessionflow.report.confirm_skipped"

_VERDICT_CN: dict[str, str] = {
    "clean": "无风险",
    "suspect": "疑似",
    "nsfw": "高置信色情",
}

_VERDICT_RANK: dict[str, int] = {"clean": 0, "suspect": 1, "nsfw": 2}


# ---------------------------------------------------------------------------
# 惰性导入辅助(兄弟模块并行开发中,未就位抛中文 RuntimeError)
#
# 交叉注释(设计说明):本函数在 __main__ / batchflow / finishflow /
# sessionflow 四处各有一份逐字重复——**入口独立性是设计**(每个 CLI 入口
# 零兄弟依赖、可单独运行),不做收敛;batchflow / finishflow 共用的纯辅助
# 已提取到 netsentinel/_flowutil.py(其内部持有自己的私有惰性导入,不经
# 本函数)。
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
# 路径:累积库与存档(全部挂在 cfg.data_dir 下,零新增配置键)
# ---------------------------------------------------------------------------
def store_path(cfg: Config) -> str:
    """会话累积库路径:``<data_dir>/session/accumulation.db``。"""
    return str(Path(str(getattr(cfg, "data_dir", "data") or "data")) / _STORE_SUBDIR / _STORE_NAME)


def archive_path(cfg: Config) -> str:
    """会话存档 JSON 路径:``<data_dir>/session/archive.json``。"""
    return str(Path(str(getattr(cfg, "data_dir", "data") or "data")) / _STORE_SUBDIR / _ARCHIVE_NAME)


def _open_store(cfg: Config, store: SessionStore | None) -> SessionStore:
    """打开(或复用注入的)累积库。"""
    return store if store is not None else SessionStore(store_path(cfg))


# ---------------------------------------------------------------------------
# ① 扫描 + 累积 + 存档:run_session
# ---------------------------------------------------------------------------
def run_session(
    cfg: Config,
    *,
    input_path: str | None = None,
    urls: list[str] | None = None,
    intake_load: Callable[[str], tuple[list[str], list[tuple[str, str]]]] | None = None,
    plan_fn: Callable[..., Any] | None = None,
    scanner: Callable[..., Any] | None = None,
    grouping: Callable[..., Any] | None = None,
    store: SessionStore | None = None,
) -> dict[str, Any]:
    """会话扫描主流程:加载 → 规划 → 扫描 → 归组入列 → 累积 → 存档。

    - ``input_path``(清单文件,经 A107 ``load_bulk``)与 ``urls``(直接给
      URL 列表)二选一,都缺省抛中文 :class:`ValueError`;
    - 扫描/归组链路与 V6 ``batchflow.run_flow`` 一致(A107 规划 / A108
      ``batch_scan`` + ``group_and_enqueue``),条目一律入列 pending,
      **本函数永不提交**;
    - 每份报告经 :meth:`SessionStore.accumulate_report` 按网站名字并入
      累积库(下次运行自动在本次结果之上继续);结束后写 JSON 存档。

    :return: ``{"scanned", "groups", "enqueued", "skipped_clean",
        "store_stats", "archive_path", "pending_groups"}``。
    :raises ValueError: 输入参数二选一失败 / 清单加载失败(A107 中文消息)。
    :raises RuntimeError: 兄弟模块未就位,中文消息指明模块。
    """
    if input_path is None and not urls:
        raise ValueError("需要 --input 批量清单或 --url 单站地址之一(不能都为空)")
    if input_path is not None and urls:
        raise ValueError("--input 批量清单与 --url 单站地址互斥,请分开执行")

    telemetry.inc(_METRIC_RUN)
    own_store = store is None
    st = _open_store(cfg, store)
    try:
        before = st.stats()

        # 1) 取 URL(清单 or 直给)
        if input_path is not None:
            load_fn = intake_load if intake_load is not None else _load(
                "netsentinel.ops.bulk_intake", "load_bulk"
            )
            valid, rejected = load_fn(input_path)
            valid = [str(u) for u in (valid or [])]
        else:
            valid = [str(u) for u in (urls or [])]
            rejected = []
        if not valid:
            print("没有可扫描的 URL,累积库保持不变(存档已刷新)")
            archive = st.archive(archive_path(cfg))
            return {
                "scanned": 0, "groups": 0, "enqueued": 0, "skipped_clean": 0,
                "store_stats": st.stats(), "archive_path": archive,
                "pending_groups": len(st.pending_groups()),
            }

        # 2) 规划(canonical 去重 + 指纹未变跳过)
        if plan_fn is None:
            plan_fn = _load("netsentinel.ops.bulk_intake", "plan_scan")
        plan = plan_fn(valid, cfg)
        to_scan: list[str] = [str(u) for u in (plan.get("to_scan") or [])]
        skipped_unchanged = int(plan.get("skipped_unchanged", 0) or 0)

        # 3) 并发扫描(与 batchflow 同链路,可注入)
        reports: dict[str, Any] = {}
        enqueued = 0
        skipped_clean = 0
        groups_count = 0
        if to_scan:
            scan_fn = scanner if scanner is not None else _load(
                "netsentinel.ops.batch_scan", "batch_scan"
            )
            scan_result = scan_fn(to_scan, cfg)
            reports = dict((scan_result or {}).get("reports") or {})

            group_fn = grouping if grouping is not None else _load(
                "netsentinel.ops.batch_scan", "group_and_enqueue"
            )
            group_result = group_fn(cfg, reports=reports)
            groups_count = len(list((group_result or {}).get("groups") or []))
            enqueued = int((group_result or {}).get("enqueued", 0) or 0)
            skipped_clean = int((group_result or {}).get("skipped_clean", 0) or 0)

            # 4) 按网站名字累积(核心:跨会话并档)
            for report in reports.values():
                st.accumulate_report(report)

        after = st.stats()
        archive = st.archive(archive_path(cfg))

        scanned = len(reports)
        print(f"── 会话扫描摘要 ──")
        print(
            f"延续基线 : 上次累积 {before['groups']} 组 / {before['scans']} 次扫描"
            "(本次在其基础上继续)"
        )
        print(f"本次扫描 : {scanned} 份报告(跳过指纹未变 {skipped_unchanged} 个)")
        print(
            f"归组入列 : {groups_count} 组 / 入列 {enqueued} 条"
            f"(clean 跳过 {skipped_clean} 组)"
        )
        print(
            f"累积现状 : 共 {after['groups']} 组(待举报 {after['pending']},"
            f"已举报 {after['reported']}),累计扫描 {after['scans']} 次"
        )
        print(f"存档    : {archive}")
        print(
            "下一步 : python -m netsentinel.sessionflow --status 查看累积;"
            " --report 确认后按网站名字逐组举报(--exec 才真实提交)"
        )
        logger.info(
            "会话扫描完成:报告 %d / 组 %d / 入列 %d / 累积 %d 组(待 %d)",
            scanned, groups_count, enqueued, after["groups"], after["pending"],
        )
        return {
            "scanned": scanned,
            "groups": groups_count,
            "enqueued": enqueued,
            "skipped_clean": skipped_clean,
            "store_stats": after,
            "archive_path": archive,
            "pending_groups": len(st.pending_groups()),
        }
    finally:
        if own_store:
            st.close()


# ---------------------------------------------------------------------------
# ② 查看:session_status
# ---------------------------------------------------------------------------
def session_status(
    cfg: Config, *, store: SessionStore | None = None
) -> dict[str, Any]:
    """打印累积库全部网站组与统计;返回 :meth:`SessionStore.snapshot`。"""
    telemetry.inc(_METRIC_STATUS)
    own_store = store is None
    st = _open_store(cfg, store)
    try:
        snap = st.snapshot()
        stats = snap["stats"]
        print(f"── 会话累积现状(存档基线 {snap['archived_at']})──")
        print(
            f"网站组 {stats['groups']} 个(待举报 {stats['pending']} / "
            f"已举报 {stats['reported']}),累计扫描 {stats['scans']} 次"
        )
        rows = snap["pending_groups"] + snap["reported_groups"]
        if not rows:
            print("(空)尚无累积结果;先用 --input / --url 扫描累积")
        for g in rows:
            verdict_cn = _VERDICT_CN.get(g["verdict"], g["verdict"])
            flag = "已举报" if g["reported"] else "待举报"
            print(
                f"  [{flag}] {g['name']:<32} {verdict_cn}"
                f"(agg_max={g['agg_max']:.4f}, 扫描 {g['scan_count']} 次,"
                f"变体 {len(g['aliases'])} 个)"
            )
        return snap
    finally:
        if own_store:
            st.close()


# ---------------------------------------------------------------------------
# ③ 存档:archive_now
# ---------------------------------------------------------------------------
def archive_now(cfg: Config, *, store: SessionStore | None = None) -> str:
    """结束运行时把当前累积写成 JSON 存档;返回存档路径。"""
    telemetry.inc(_METRIC_ARCHIVED)
    own_store = store is None
    st = _open_store(cfg, store)
    try:
        path = st.archive(archive_path(cfg))
        stats = st.stats()
        print(
            f"已存档:{path}(网站组 {stats['groups']} 个,累计扫描 "
            f"{stats['scans']} 次;下次运行自动延续)"
        )
        return path
    finally:
        if own_store:
            st.close()


# ---------------------------------------------------------------------------
# ④ 确认举报:report_all(确认前零状态变更;确认后按网站名字逐组举报)
# ---------------------------------------------------------------------------
def _group_counts_and_items(
    queue: Any, pending_names: set[str], *, portal: str = PORTAL_DEFAULT
) -> tuple[dict[str, dict[str, int]], list[dict[str, Any]]]:
    """扫描复核队列,产出 (各组条目状态计数, 待举报条目清单)。

    - 组名解析复用 A110 ``resolve_group_name``(note 的 ``[组:名]`` 标记,
      回退 canonical/host),与累积库组名同口径;
    - 待举报条目 = pending(待本次确认批准)或 approved(此前已批准)且
      属于累积库待举报组的条目,按队列 id 升序;每条固定五键
      {entry_id, group_name, site_url, evidence_zip, portal}(与 A110/A112
      对接约定一致);
    - rejected / submitted 条目只计数,不进入清单。
    """
    resolve_group_name = _load("netsentinel.decision.batch_review", "resolve_group_name")
    portal_id = str(portal or "").strip() or PORTAL_DEFAULT
    counts: dict[str, dict[str, int]] = {}
    items: list[dict[str, Any]] = []
    for entry in queue.list() or []:
        group = str(resolve_group_name(getattr(entry, "note", ""), getattr(entry, "site_url", "")))
        if group not in pending_names:
            continue  # 非本会话待举报组(已举报组 / 其他流程条目),不掺和
        status = str(getattr(entry, "status", "") or "")
        bucket = counts.setdefault(
            group, {"pending": 0, "approved": 0, "submitted": 0, "rejected": 0, "other": 0}
        )
        key = status if status in bucket else "other"
        bucket[key] += 1
        if status in ("pending", "approved"):
            items.append(
                {
                    "entry_id": int(getattr(entry, "id", 0) or 0),
                    "group_name": group,
                    "site_url": str(getattr(entry, "site_url", "") or ""),
                    "evidence_zip": str(getattr(entry, "evidence_zip", "") or ""),
                    "portal": portal_id,
                }
            )
    return counts, items


def report_all(
    cfg: Config,
    *,
    reviewer: str = "",
    portal: str = PORTAL_DEFAULT,
    dry_run: bool | None = None,
    prompt: Callable[[str], str] | None = None,
    store: SessionStore | None = None,
    queue: Any | None = None,
    review: Any | None = None,
    state_cls: Any | None = None,
    submit: Callable[..., Any] | None = None,
    max_items: int | None = None,
) -> dict[str, Any]:
    """确认举报全部累积结果,按网站名字逐组顺序举报。

    流程(确认前**零状态变更**):

    1. 取累积库待举报组(按严重度排序);为空直接返回零值摘要;
    2. 扫描复核队列,解析各组条目状态(pending/approved/submitted/rejected);
       条目已全部终态(submitted/rejected)的组自动 ``mark_reported`` 收档;
    3. 打印逐组汇总表 → 人工确认:审核人(参数或交互输入)+ 显式 y
       (提示语含"我已逐组人工核实";非 y 一律中止,零状态变更);
    4. 确认动作 = 状态变更的**唯一入口**:
       a. 各组 pending 条目 ``queue.approve(id, note, actor=审核人)``;
       b. 各组写批量声明 ``BatchReview.attest``(红线 25 留痕);
    5. 按(组严重度, agg_max)排序组装条目 → 超过 ``batch_max_items`` 自动
       分批,每批 A113 ``BatchState`` 建批 + A112 ``run_batch`` 顺序举报
       (恒 ``auto_confirm=False``,红线 24/26;频控挂起即停,可续跑);
    6. 全组条目提交完成的组 ``mark_reported``;存档刷新;返回中文摘要。

    :param dry_run: 缺省取 ``cfg.dry_run_default``(默认 True 干跑);
        ``--exec`` 才真实提交(每条仍走 HUMAN_GATE 人工门)。
    :param prompt: 确认输入函数(测试注入);缺省 ``input``。
    :return: ``{"groups_total", "groups_reported", "batches", "submitted",
        "failed", "rate_limited", "approved", "confirmed", "archive_path"}``。
    """
    telemetry.inc(_METRIC_REPORT)
    effective_dry_run = bool(cfg.dry_run_default) if dry_run is None else bool(dry_run)
    try:
        limit = int(max_items if max_items is not None else cfg.batch_max_items)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, limit)

    own_store = store is None
    st = _open_store(cfg, store)
    own_queue = queue is None
    try:
        pending = st.pending_groups()
        if not pending:
            print("累积库中没有待举报的网站组(全部已举报或尚无累积结果)")
            return {
                "groups_total": 0, "groups_reported": 0, "batches": 0,
                "submitted": 0, "failed": 0, "rate_limited": False,
                "approved": 0, "confirmed": False,
                "archive_path": st.archive(archive_path(cfg)),
            }

        if own_queue:
            queue = _load("netsentinel.decision.review_queue", "ReviewQueue")(
                str(getattr(cfg, "db_path", "data/review_queue.db"))
            )
        counts, items = _group_counts_and_items(
            queue, {g.name for g in pending}, portal=portal
        )

        # 无可执行条目的组(全部终态或无条目):自动收档后从本轮剔除
        actionable: list = []
        for g in pending:
            c = counts.get(g.name)
            if c and (c["pending"] or c["approved"]):
                actionable.append(g)
            elif c and (c["submitted"] or c["rejected"]) and not (c["pending"] or c["approved"]):
                st.mark_reported([g.name])
                logger.info("组 %s 条目已全部终态,自动收档为已举报", g.name)
        if not actionable:
            print("待举报组均无可执行条目(可能已全部终态);累积库已同步收档")
            return {
                "groups_total": 0, "groups_reported": 0, "batches": 0,
                "submitted": 0, "failed": 0, "rate_limited": False,
                "approved": 0, "confirmed": False,
                "archive_path": st.archive(archive_path(cfg)),
            }

        # ---- 人工确认(红线 2/25:确认前零状态变更)----
        print(f"── 待举报网站组({len(actionable)} 组,{len(items)} 条)──")
        for g in actionable:
            c = counts.get(g.name, {})
            verdict_cn = _VERDICT_CN.get(g.verdict, g.verdict)
            print(
                f"  {g.name:<32} {verdict_cn}(agg_max={g.agg_max:.4f},"
                f"扫描 {g.scan_count} 次,条目 待审{c.get('pending', 0)}/"
                f"已批{c.get('approved', 0)})"
            )
        ask = prompt if prompt is not None else input
        who = (reviewer or "").strip() or ask("审核人姓名:").strip()
        if not who:
            print("错误:举报确认必须留痕审核人(未提供,已中止,零状态变更)")
            return {
                "groups_total": len(actionable), "groups_reported": 0, "batches": 0,
                "submitted": 0, "failed": 0, "rate_limited": False,
                "approved": 0, "confirmed": False, "archive_path": "",
            }
        answer = ask(
            f"我已逐组人工核实以上 {len(actionable)} 组全部证据,"
            "确认批准并按网站名字举报?(y/N):"
        ).strip().lower()
        if answer != "y":
            telemetry.inc(_METRIC_CONFIRM_SKIPPED)
            print("未确认(y),已中止——未批准任何条目、未提交任何举报")
            return {
                "groups_total": len(actionable), "groups_reported": 0, "batches": 0,
                "submitted": 0, "failed": 0, "rate_limited": False,
                "approved": 0, "confirmed": False, "archive_path": "",
            }

        # ---- 确认动作 a:批准 pending 条目(留审核人;一次性遍历,逐条 approve)----
        resolve_group_name = _load(
            "netsentinel.decision.batch_review", "resolve_group_name"
        )
        actionable_names = {g.name for g in actionable}
        approved_n = 0
        for entry in queue.list(status="pending") or []:
            group = str(resolve_group_name(
                getattr(entry, "note", ""), getattr(entry, "site_url", "")
            ))
            if group in actionable_names:
                queue.approve(
                    int(getattr(entry, "id", 0) or 0),
                    "会话举报确认(人工批准)",
                    actor=who,
                )
                approved_n += 1
        telemetry.inc(_METRIC_APPROVED, approved_n)

        # ---- 确认动作 b:逐组批量声明(红线 25 留痕)----
        own_review = review is None
        if own_review:
            review = _load("netsentinel.decision.batch_review", "BatchReview")(
                str(getattr(cfg, "db_path", "data/review_queue.db")),
                audit_path=str(getattr(cfg, "audit_path", "data/audit.jsonl")),
            )
        try:
            for g in actionable:
                review.attest(
                    g.name,
                    items=len([i for i in items if i["group_name"] == g.name]),
                    reviewer=who,
                    text=_ATTEST_TEXT,
                )
        finally:
            if own_review:
                close = getattr(review, "close", None)
                if callable(close):
                    close()
        telemetry.inc(_METRIC_ATTESTED, len(actionable))

        # ---- 按网站名字排序组装条目(组顺序 = 累积库严重度序)----
        by_group: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            by_group.setdefault(item["group_name"], []).append(item)
        ordered: list[dict[str, Any]] = []
        for g in actionable:
            ordered.extend(by_group.get(g.name, []))

        # ---- 分批顺序举报(红线 24/26:恒 auto_confirm=False,频控不放宽)----
        if state_cls is None:
            state_cls = _load("netsentinel.submit.batch_state", "BatchState")
        run_batch_fn = submit if submit is not None else _load(
            "netsentinel.submit.batch_submit", "run_batch"
        )
        chunks = [ordered[i:i + limit] for i in range(0, len(ordered), limit)]
        submitted_n = 0
        failed_n = 0
        rate_limited = False
        batch_ids: list[int] = []
        mode_cn = "真实执行" if not effective_dry_run else "干跑演练(dry_run,未真实提交)"
        print(
            f"确认通过(审核人 {who},模式:{mode_cn}):"
            f"{len(actionable)} 组 / {len(ordered)} 条,分 {len(chunks)} 批顺序执行;"
            "每条提交仍需人工输入验证码并最终确认(红线 24)。"
        )
        for chunk in chunks:
            state = state_cls(str(getattr(cfg, "db_path", "data/review_queue.db")))
            try:
                bid = state.new_batch(chunk, note=_NEW_BATCH_NOTE)
                batch_ids.append(int(bid))
                result = dict(run_batch_fn(
                    chunk, cfg, state=state.bound(bid), dry_run=effective_dry_run
                ))
            finally:
                close = getattr(state, "close", None)
                if callable(close):
                    close()
            submitted_n += int(result.get("submitted", 0) or 0)
            failed_n += int(result.get("failed", 0) or 0)
            rate_limited = rate_limited or bool(result.get("rate_limited", False))
            # 真实提交成功的条目回写复核队列(approved → submitted,与单站
            # run_submit 同口径;否则下次 --report 会重复收集已提交条目)。
            for row in result.get("results") or []:
                if row.get("submitted"):
                    try:
                        queue.mark_submitted(int(row.get("entry_id") or 0), actor=who)
                    except Exception as exc:  # noqa: BLE001 - 回写失败只告警,不阻断
                        logger.warning(
                            "回写复核队列 submitted 状态失败(entry=%s):%s",
                            row.get("entry_id"), exc,
                        )
            if rate_limited or result.get("paused", False):
                print(
                    "频控/暂停:剩余条目留待下次 --report 续跑"
                    "(额度恢复后自动从未完成条目继续)"
                )
                break

        # ---- 收档:全组条目均 submitted 的组标记已举报 ----
        # 以复核队列终态为单一事实源(不受批次切片/中途挂起影响)重扫一遍。
        done_groups: list[str] = []
        final_counts, _ = _group_counts_and_items(
            queue, {g.name for g in pending}, portal=portal
        )
        for g in actionable:
            c = final_counts.get(g.name, {})
            if c.get("pending", 0) == 0 and c.get("approved", 0) == 0 and c.get("submitted", 0):
                done_groups.append(g.name)
        if done_groups:
            st.mark_reported(done_groups)

        archive = st.archive(archive_path(cfg))
        summary = {
            "groups_total": len(actionable),
            "groups_reported": len(done_groups),
            "batches": len(batch_ids),
            "batch_ids": batch_ids,
            "submitted": submitted_n,
            "failed": failed_n,
            "rate_limited": rate_limited,
            "approved": approved_n,
            "confirmed": True,
            "archive_path": archive,
        }
        print(f"── 会话举报摘要({mode_cn})──")
        print(f"确认组数: {summary['groups_total']}(完成收档 {summary['groups_reported']} 组)")
        print(f"批准条目: {summary['approved']} 条;提交成功 {summary['submitted']} 条,失败 {summary['failed']} 条")
        if rate_limited:
            print("频控挂起:下次运行 --report 自动从未完成条目继续")
        print(f"存档    : {archive}")
        print("红线 24:每条提交的验证码与最终确认均由人工完成;频控与每日额度未放宽(红线 26)。")
        logger.info(
            "会话举报完成:组 %d / 批 %d / submitted=%d failed=%d rate_limited=%s",
            summary["groups_total"], summary["batches"],
            submitted_n, failed_n, rate_limited,
        )
        return summary
    finally:
        if own_store:
            st.close()
        if own_queue and queue is not None:
            close = getattr(queue, "close", None)
            if callable(close):
                close()


# ---------------------------------------------------------------------------
# ⑤ CLI:python -m netsentinel.sessionflow
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m netsentinel.sessionflow",
        description=(
            "净网哨兵会话累积流程:扫描结果按网站名字自动累积,结束自动存档,"
            "下次运行延续累积;确认举报时按网站名字逐组举报(逐条人工门,红线 24)"
        ),
    )
    parser.add_argument(
        "--input", default=None,
        help="批量清单路径(.txt/.csv/.yaml/.yml):扫描并累积(不提交)",
    )
    parser.add_argument(
        "--url", default=None, help="单站 URL:扫描并累积(与 --input 互斥)",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="查看累积现状(全部网站组与统计)",
    )
    parser.add_argument(
        "--report", action="store_true",
        help="确认举报:列出全部待举报组,人工确认后按网站名字逐组举报",
    )
    parser.add_argument(
        "--archive", action="store_true", help="立即存档当前累积(结束运行时用)",
    )
    parser.add_argument(
        "--reviewer", default="", help="举报确认审核人(留痕;缺省交互输入)",
    )
    parser.add_argument(
        "--config", default=None,
        help="配置文件路径(默认 ./config.yaml,缺失则用默认配置)",
    )
    dry = parser.add_mutually_exclusive_group()
    dry.add_argument(
        "--dry-run", action="store_true",
        help="干跑模式(默认;举报只演练,不真实提交)",
    )
    dry.add_argument(
        "--exec", action="store_true",
        help="真实执行模式(仅 --report 有效;每条仍需人工输入验证码,红线 24)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口:``--input|--url|--status|--report|--archive`` 五选一。

    模式:

    - ``--input PATH`` / ``--url URL``:扫描 + 归组入列 + 按网站名字累积 +
      结束存档(永不提交);
    - ``--status``:查看累积现状;
    - ``--report [--reviewer 名字] [--exec]``:确认举报全部累积结果(默认
      干跑;``--exec`` 才真实提交,且每条仍走人工门);
    - ``--archive``:立即存档。

    返回码:0 成功;2 用法/配置/输入错误(ValueError);3 兄弟模块未就位
    (RuntimeError,中文消息)。
    """
    args = _build_parser().parse_args(argv)

    modes = [
        bool(args.input), bool(args.url), args.status, args.report, args.archive
    ]
    if sum(modes) != 1:
        print(
            "错误:--input / --url / --status / --report / --archive 五选一"
            "(当前选择了 %d 个)" % sum(modes)
        )
        return 2
    if args.exec and not args.report:
        print("错误:--exec 仅在 --report 模式下有效(其余模式永不提交)")
        return 2

    from netsentinel.config import load_config  # 惰性:纯用法错误不触碰配置

    try:
        cfg = load_config(args.config)
    except ValueError as exc:
        print(f"配置错误:{exc}")
        return 2
    from netsentinel.logging_util import setup_logging

    setup_logging(cfg)

    try:
        if args.report:
            report_all(
                cfg,
                reviewer=str(args.reviewer or ""),
                dry_run=False if args.exec else (True if args.dry_run else None),
            )
        elif args.status:
            session_status(cfg)
        elif args.archive:
            archive_now(cfg)
        elif args.input:
            run_session(cfg, input_path=args.input)
        else:
            run_session(cfg, urls=[str(args.url)])
    except ValueError as exc:
        print(f"错误:{exc}")
        return 2
    except RuntimeError as exc:
        print(f"错误:{exc}")
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    raise SystemExit(main())
