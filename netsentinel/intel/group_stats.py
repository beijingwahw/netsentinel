"""案件分组战况统计 —— 批量筛选后的"战况"量化(NetSentinel · A117)。

依据 CONTRACTS-V6.md §4 A117 条目实现 V6 分组流水线的只读统计层:

    stats(groups)              组数 / 最大组 Top5 / 单站·多站组 / 判定分布 /
                               重复线索域排行 —— 一次调用产出整份"战报"
    export_csv(groups, path)   组清单导出 utf-8-sig CSV(Excel 双击不乱码)
    repeat_offenders(...)      同组内曾驳回又复现的站点清单(重复犯案线索)

口径约定(全模块统一,CSV 与 stats 共用同一套鸭子取值辅助):

- **组鸭子**:`groups` 中每个元素按属性名访问(`name` / `aliases` /
  `site_urls` / `agg_max` / `verdict`),一律 getattr 容错——缺失属性按
  空值处理,天然兼容 ``netsentinel.intel.case_group.CaseGroup`` 与
  SimpleNamespace 等任意鸭子;``verdict`` 兼容 ``Verdict`` 枚举或字符串;
- **站点数** = 组内去重别名域(host)个数(``aliases``,小写化后去重);
  **URL 数** = 组内去重 URL 条数(``site_urls``);两者是不同粒度的量,
  CSV 的"站点数/URL数"两列即分别对应;
- **单站组** = 站点数 ≤ 1(仅一个域名,通常不值得批量关注);多站组 =
  站点数 ≥ 2(镜像 / 团伙并组的价值所在);契约所述"单站组占比"可由
  ``single_site_groups / group_count`` 自行计算;
- **最大组 Top5**:按 (URL 数降序, agg_max 降序, 组名升序) 取前 5,
  保证结果确定;
- **重复线索域排行(alias_top5)**:同一别名域出现在 ≥ 2 个组中才算
  "重复线索"(跨组复现的域名),按出现组数降序、域名升序取前 5;
  无任何跨组复现时为空列表;
- **repeat_offenders**:条目(鸭子 ``id/site_url/verdict/note``)曾驳回
  = note 含 ``[rejected]`` 标记 **或** ``status`` 属性为 ``rejected``,
  且其 site_url 的 canonical(可注册域,复用 A103,惰性导入)落在某个
  当前组的 canonical 集合内 → 视为"同组内曾驳回又复现";按
  (site_url, group_name) 去重,保持输入顺序。

依赖与降级:A103 ``netsentinel.intel.canonical`` 惰性导入(兄弟模块只读),
未就位 / 调用异常时降级到内置同语义兜底(多段后缀表 + 末两段 / IP 直连 /
单标签原样),repeat_offenders 的归组判定不受影响。

统计层纯只读:不抓取、不联网、不改任何业务状态、不触发任何提交动作
(红线 24/25 不涉及);仅 export_csv 落盘一个 CSV 文件。
纯标准库 + 共享 telemetry,离线可测。

用法::

    from netsentinel.intel.group_stats import stats, export_csv, repeat_offenders

    report = stats(groups)
    report["group_count"], report["verdict_dist"]["nsfw"]   # 战况概览
    rows = export_csv(groups, "data/groups.csv")            # 返回数据行数
    recidivists = repeat_offenders(queue.entries, groups)   # 驳回复现清单
"""
from __future__ import annotations

import csv
import importlib
import ipaddress
import logging
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from netsentinel import telemetry

logger = logging.getLogger(__name__)

__all__ = ["CSV_HEADER", "export_csv", "repeat_offenders", "stats"]

#: 导出 CSV 表头(列序即契约给定:组名/站点数/URL数/判定/agg_max/aliases,禁改)。
CSV_HEADER: tuple[str, ...] = ("组名", "站点数", "URL数", "判定", "agg_max", "aliases")

#: 合法判定档位全集(verdict_dist 固定三键,恒出现在结果中)。
_VERDICTS: tuple[str, ...] = ("clean", "suspect", "nsfw")

#: 排行榜截断长度(Top5,契约给定)。
_TOP_N: int = 5

#: 驳回复现识别:历史备注中的驳回标记(字面子串,与复核队列落库格式一致)。
_REJECTED_MARK: str = "[rejected]"

#: A103 canonical 模块完整名(惰性导入目标,兄弟模块只读)。
_CANONICAL_MODULE: str = "netsentinel.intel.canonical"

#: 内置兜底:常见多段后缀表(与契约 §4 A103/A104 给定核心集合一致,禁增删)。
_MULTI_SUFFIXES: frozenset[str] = frozenset(
    {"com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn"}
)


# ---------------------------------------------------------------------------
# 鸭子取值辅助(组 / 条目一律按属性名容错读取)
# ---------------------------------------------------------------------------


def _seq_of(obj: Any, attr: str) -> list[str]:
    """容错读取对象的序列属性(list/tuple/set 均可);缺失 / 其他类型 → []。"""
    value = getattr(obj, attr, None)
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [str(item) for item in value]


def _name_of(group: Any) -> str:
    """组名(展示主名);缺失 / None → 空串。"""
    return str(getattr(group, "name", "") or "").strip()


def _agg_of(group: Any) -> float:
    """组 agg_max;缺失 / None / 非法值 → 0.0(不抛错)。"""
    try:
        return float(getattr(group, "agg_max", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _hosts_of(group: Any) -> list[str]:
    """组的去重别名域列表:小写、去空白、保首见顺序(空值跳过)。"""
    hosts: list[str] = []
    seen: set[str] = set()
    for item in _seq_of(group, "aliases"):
        host = item.strip().lower()
        if host and host not in seen:
            seen.add(host)
            hosts.append(host)
    return hosts


def _norm_verdict(value: Any) -> str:
    """把判定值(Verdict 枚举 / 字符串 / 其他)归一为合法档位小写字符串。

    未知 / 空判定返回 ""(不计入 verdict_dist,CSV 中回退为原始字符串)。
    """
    raw = getattr(value, "value", value)  # 兼容 Verdict(str, Enum) 实例
    text = str(raw or "").strip().lower()
    return text if text in _VERDICTS else ""


def _verdict_label(group: Any) -> str:
    """组的判定列展示值:合法档位归一小写;未知回退原始字符串(保真)。"""
    verdict = _norm_verdict(getattr(group, "verdict", ""))
    if verdict:
        return verdict
    raw = getattr(group, "verdict", "")
    raw = getattr(raw, "value", raw)
    return str(raw or "").strip()


# ---------------------------------------------------------------------------
# A103 canonical 惰性导入 + 内置同语义兜底
# ---------------------------------------------------------------------------


def _load_canonical() -> Any | None:
    """惰性导入 A103 canonical;任何异常按未就位处理(返回 None 走兜底)。

    守卫刻意放宽(不限 ImportError):兄弟模块破损、被测试用 None 占位
    阻断等,一律静默降级,统计主流程不受影响。
    """
    try:
        return importlib.import_module(_CANONICAL_MODULE)
    except Exception as exc:  # noqa: BLE001 - 守卫宽:任何异常按未就位
        logger.debug("canonical(A103)未就位,启用内置兜底:%s", exc)
        return None


def _builtin_canonical_key(url: str) -> str:
    """内置兜底 canonical_key(语义与契约 §2 一致,供 A103 未就位时降级)。

    规则:小写可注册域——末两段命中多段后缀表(com.cn 等)时取末三段,
    否则取末两段;IP 直连返回 IP 串;单标签主机(如 localhost)取其本身;
    解析失败(空串 / 无 host / 结构非法)返回 ""。纯字符串运算,不发网。
    """
    try:
        host = urlsplit(str(url or "").strip()).hostname
    except ValueError:
        return ""
    host = (host or "").rstrip(".").lower()
    if not host:
        return ""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI_SUFFIXES:
        return ".".join(labels[-3:])
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return labels[-1]


def _key_with(canonical: Any | None, url: str) -> str:
    """返回 url 的 canonical 键;A103 可用则优先,异常 / 未就位落兜底。

    A103 返回 ""(解析失败)时尊重其结论;只有调用本身抛异常 / 缺接口
    时才改用内置实现,保证口径与契约对齐。
    """
    if canonical is not None:
        try:
            return str(getattr(canonical, "canonical_key")(url) or "")
        except Exception as exc:  # noqa: BLE001 - 单条解析异常按兜底处理
            logger.debug("canonical 对 %r 解析异常,按内置兜底处理:%s", url, exc)
    return _builtin_canonical_key(url)


# ---------------------------------------------------------------------------
# stats:分组战况总览
# ---------------------------------------------------------------------------


def stats(groups: Iterable[Any] | None) -> dict[str, Any]:
    """统计案件组列表的整体"战况",返回固定七键的字典。

    参数:
      groups:组鸭子列表(按属性名容错访问 name / aliases / site_urls /
        agg_max / verdict);``None`` / 空序列按空战况处理。

    返回键(契约 §4 A117 + 任务书给定,不多不少):

      - ``group_count``:组总数;
      - ``single_site_groups``:单站组数(去重别名域 ≤ 1);
      - ``multi_site_groups``:多站组数(去重别名域 ≥ 2);
      - ``total_urls``:全部组的去重 URL 条数总和;
      - ``max_group_top5``:最大组 Top5,元素 ``{"name", "urls", "agg_max"}``
        (urls 为 URL 条数;按 URL 数降序 / agg_max 降序 / 组名升序,确定);
      - ``verdict_dist``:``{"clean": n, "suspect": n, "nsfw": n}`` 三键恒在,
        按组判定计数(枚举 / 字符串兼容;未知判定不计入,组数仍计入
        group_count);
      - ``alias_top5``:重复线索域排行,元素 ``{"host", "groups"}``(该别名
        域出现在几个组中;仅保留出现组数 ≥ 2 的"重复线索",按组数降序 /
        域名升序取前 5;无跨组复现时为空列表)。

    纯本地计算,不联网、不落盘、不改任何业务状态。
    """
    groups = list(groups) if groups is not None else []

    verdict_dist: dict[str, int] = dict.fromkeys(_VERDICTS, 0)
    host_group_count: dict[str, int] = {}
    rows: list[tuple[str, int, float]] = []
    total_urls = 0
    single_site = 0
    multi_site = 0

    with telemetry.timer("group_stats.stats"):
        for group in groups:
            urls = _seq_of(group, "site_urls")
            hosts = _hosts_of(group)
            total_urls += len(urls)
            if len(hosts) >= 2:
                multi_site += 1
            else:
                single_site += 1
            verdict = _norm_verdict(getattr(group, "verdict", ""))
            if verdict:
                verdict_dist[verdict] += 1
            for host in hosts:  # 组内已去重,直接累计出现组数
                host_group_count[host] = host_group_count.get(host, 0) + 1
            rows.append((_name_of(group), len(urls), _agg_of(group)))

        ranked = sorted(rows, key=lambda row: (-row[1], -row[2], row[0]))
        max_group_top5 = [
            {"name": name, "urls": urls, "agg_max": agg}
            for name, urls, agg in ranked[:_TOP_N]
        ]
        repeats = sorted(
            ((host, count) for host, count in host_group_count.items() if count >= 2),
            key=lambda item: (-item[1], item[0]),
        )
        alias_top5 = [
            {"host": host, "groups": count} for host, count in repeats[:_TOP_N]
        ]

    telemetry.inc("group_stats.stats_calls")
    telemetry.inc("group_stats.groups_measured", len(groups))
    return {
        "group_count": len(groups),
        "single_site_groups": single_site,
        "multi_site_groups": multi_site,
        "total_urls": total_urls,
        "max_group_top5": max_group_top5,
        "verdict_dist": verdict_dist,
        "alias_top5": alias_top5,
    }


# ---------------------------------------------------------------------------
# export_csv:组清单导出(Excel 友好)
# ---------------------------------------------------------------------------


def export_csv(groups: Iterable[Any] | None, path: str | Path) -> int:
    """把组清单导出为 utf-8-sig CSV,返回写入的数据行数(不含表头)。

    - 编码 ``utf-8-sig``:带 BOM,Excel 双击直接打开中文不乱码;
    - 列序固定(契约给定):组名 / 站点数 / URL数 / 判定 / agg_max /
      aliases(去重别名域以 ``;`` 拼入一格,避免与 CSV 逗号冲突);
    - 行序保持输入顺序(纯函数口径,不额外排序);
    - ``groups`` 为 ``None`` / 空序列时仅写表头,返回 0。

    参数:
      groups:组鸭子列表(取值口径与 :func:`stats` 完全一致);
      path:目标 CSV 路径(str / Path 均可,父目录自动创建)。

    返回:数据行数(表头单独一行,不计入)。仅落盘该 CSV,无其他副作用。
    """
    groups = list(groups) if groups is not None else []
    target = Path(path)
    if str(target.parent):
        target.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    with telemetry.timer("group_stats.export_csv"):
        with open(target, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(CSV_HEADER)
            for group in groups:
                writer.writerow(
                    [
                        _name_of(group),
                        len(_hosts_of(group)),  # 站点数 = 去重别名域个数
                        len(_seq_of(group, "site_urls")),  # URL 数
                        _verdict_label(group),
                        str(_agg_of(group)),
                        ";".join(_hosts_of(group)),
                    ]
                )
                written += 1

    telemetry.inc("group_stats.export_csv")
    telemetry.inc("group_stats.exported_rows", written)
    return written


# ---------------------------------------------------------------------------
# repeat_offenders:同组内曾驳回又复现
# ---------------------------------------------------------------------------


def _rejection_source(entry: Any) -> str | None:
    """判断条目是否曾驳回;是则返回中文依据,否则 None。

    两种信号(任一命中即视为曾驳回):
      1. note(备注)含字面 ``[rejected]`` 标记(历史驳回留痕);
      2. status 属性为 ``rejected``(复核队列状态机终态之一)。
    """
    note = str(getattr(entry, "note", "") or "")
    if _REJECTED_MARK in note:
        return "历史备注含 [rejected] 驳回标记"
    status = getattr(entry, "status", "")
    status = getattr(status, "value", status)  # 兼容枚举值
    if str(status or "").strip().lower() == "rejected":
        return "复核状态为 rejected(已驳回)"
    return None


def repeat_offenders(
    entries: Iterable[Any] | None,
    groups: Iterable[Any] | None,
) -> list[dict[str, str]]:
    """找出"同组内曾驳回又复现"的条目,生成重复犯案线索清单。

    参数:
      entries:复核条目鸭子列表(容错访问 id / site_url / verdict / note,
        另可选读 status);曾驳回 = note 含 ``[rejected]`` 或 status 为
        ``rejected``;
      groups:当前案件组鸭子列表(以 site_urls 的 canonical 集合代表该组
        覆盖的可注册域;canonical 复用 A103,未就位走内置兜底)。

    判定:条目曾驳回,且其 site_url 的 canonical 键命中某组的 canonical
    集合 → 该站点在此组"曾驳回又复现"(驳回过的站点换 URL / 换子域再次
    出现在当前分组结果中)。canonical 解析失败的条目无法归组,跳过。

    返回:``[{"site_url": str, "group_name": str, "reason": 中文说明}]``,
    按条目输入顺序;以 (site_url, group_name) 去重(同站同组只报一次);
    无命中返回空列表。纯本地计算,不联网、不改状态。
    """
    entries = list(entries) if entries is not None else []
    groups = list(groups) if groups is not None else []

    canonical = _load_canonical()
    # 预计算每组的 canonical 键集合(site_urls 全量归一;全解析失败 → 空集,
    # 该组不可能命中任何条目)。
    group_keys = [
        {
            key
            for key in (_key_with(canonical, url.strip()) for url in _seq_of(group, "site_urls"))
            if key
        }
        for group in groups
    ]

    results: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    with telemetry.timer("group_stats.repeat_offenders"):
        for entry in entries:
            source = _rejection_source(entry)
            if source is None:
                continue  # 从未驳回,不构成"复现"
            site_url = str(getattr(entry, "site_url", "") or "").strip()
            if not site_url:
                continue
            key = _key_with(canonical, site_url)
            if not key:
                continue  # 解析失败,无法归组
            for group, keys in zip(groups, group_keys):
                if key not in keys:
                    continue
                group_name = _name_of(group)
                dedup = (site_url, group_name)
                if dedup in seen:
                    break  # 同站同组已报过
                seen.add(dedup)
                results.append(
                    {
                        "site_url": site_url,
                        "group_name": group_name,
                        "reason": (
                            f"曾驳回又复现:{source},该站点现再次归入案件组「{group_name}」"
                        ),
                    }
                )
                break

    telemetry.inc("group_stats.repeat_offenders", len(results))
    return results
