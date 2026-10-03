"""案件分组引擎 —— 同站条目按 canonical(可注册域)归并为基础组(NetSentinel · A104)。

依据 CONTRACTS-V6.md §2 核心概念 / §4 A104 条目实现 V6 "镜像归并":

    同 canonical_key(可注册域小写)的所有条目 → 同一基础组;
    www / 子域 / 端口 / 路径差异视为同站变体(host 记入 aliases);
    一个基础组 = 一份合并证据包的候选 = 一次举报的候选单位。

分工与降级:A103 ``netsentinel.intel.canonical`` 与本模块并行开发——本模块
对其**惰性导入**,未就位 / 加载失败 / 单条解析异常时一律降级到**内置同语义
兜底**(同一张多段后缀表 com.cn/net.cn/org.cn/gov.cn/edu.cn/ac.cn + 通用
TLD 取末两段;IP 直连 → IP 串;解析失败 → ""),分组结果保持一致。

分组只是编排:本模块不抓取、不联网、不产生任何提交动作(红线 24 不涉及);
离线纯函数,仅依赖标准库与共享契约(contracts / telemetry)。

用法::

    from netsentinel.intel.case_group import CaseGroup, group_entries

    groups = group_entries(queue_entries, {e.id: report for e in queue_entries})
    groups[0].name        # "example.com"(canonical 主名)
    groups[0].aliases     # ["www.example.com", "example.com", "img.example.com"]
    groups[0].verdict     # "nsfw"(组内最严重档)

关键口径(契约 §4 A104):

- EntryLike 为鸭子类型(getattr 容错:id / site_url / verdict / evidence_zip,
  缺失属性按空值处理;verdict 兼容 Verdict 枚举或字符串,evidence_zip 当前
  不参与分组,仅供后续合并证据包阶段取用);
- agg_max 取组内各 report 的 agg_nsw_prob 最大值(reports 缺该条目 → 不计入;
  全缺 → 0.0);verdict 取组内条目判定中最严重档(CLEAN < SUSPECT < NSFW);
- image_sha_set 为组内各 report 页面图片 sha256 的并集(去重、跳过空 sha);
- 输出按 (verdict 档, agg_max, 组规模) 降序;三键全同的组按组名升序,
  保证结果确定性;空输入返回 []。
"""
from __future__ import annotations

import importlib
import ipaddress
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, runtime_checkable
from urllib.parse import urlsplit

from netsentinel import telemetry
from netsentinel.contracts import Verdict, now_iso

logger = logging.getLogger(__name__)

__all__ = ["CaseGroup", "EntryLike", "group_entries"]

# ---------------------------------------------------------------------------
# 判定档位(CLEAN < SUSPECT < NSFW)
# ---------------------------------------------------------------------------

#: 合法判定档位 → 严重度序数(未知/空判定不在表内,按 -1 处理,低于 clean)。
_VERDICT_RANK: dict[str, int] = {
    Verdict.CLEAN.value: 0,
    Verdict.SUSPECT.value: 1,
    Verdict.NSFW.value: 2,
}

#: A103 canonical 模块的完整名(惰性导入目标)。
_CANONICAL_MODULE = "netsentinel.intel.canonical"

#: 内置兜底:常见多段后缀表(与 A103 契约 §4 给定集合一致,禁增删)。
_MULTI_SUFFIXES: frozenset[str] = frozenset(
    {"com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn"}
)

#: 兜底 canonical_name 对解析失败 URL 的截断长度(契约:"原 URL 截断")。
_NAME_TRUNCATE = 60


def _now() -> str:
    """当前时间戳(独立包装,便于测试冻结;内部走 contracts.now_iso)。"""
    return now_iso()


def _norm_verdict(value: Any) -> str:
    """把判定值(Verdict 枚举 / 字符串 / 其他)归一为合法档位小写字符串。

    未知或空判定返回 ""(调用方按"低于 clean"处理,不参与最严重档竞选)。
    """
    raw = getattr(value, "value", value)  # 兼容 Verdict(str, Enum) 实例
    text = str(raw or "").strip().lower()
    return text if text in _VERDICT_RANK else ""


# ---------------------------------------------------------------------------
# CaseGroup 数据结构
# ---------------------------------------------------------------------------


@runtime_checkable
class EntryLike(Protocol):
    """复核条目鸭子接口:id / site_url / verdict / evidence_zip。

    实际取值一律走 getattr 容错(缺属性按空值),满足该协议的既有对象包括
    ``decision.review_queue.Entry``;本协议仅供类型标注,不做运行时强校验。
    """

    id: int
    site_url: str
    verdict: str
    evidence_zip: str


@dataclass
class CaseGroup:
    """案件基础组:同 canonical(可注册域)的全部条目归并后的聚合体。

    字段与契约 §4 A104 一一对应(顺序即构造位置参数顺序):

    - name:canonical 主名(可注册域,展示用);
    - aliases:全部去重 host(小写,www/子域/端口变体都记入,首见顺序);
    - entry_ids:成员条目 id(保持输入顺序);
    - site_urls:成员条目 site_url(去重,首见顺序);
    - agg_max:组内 report 的 agg_nsw_prob 最大值(缺 report → 0.0);
    - verdict:组内条目判定的最严重档(CLEAN < SUSPECT < NSFW);
    - image_sha_set:组内 report 页面图片 sha256 并集(去重);
    - created_at:建组时间戳(contracts.now_iso,秒级 ISO8601)。
    """

    name: str = ""
    aliases: list[str] = field(default_factory=list)
    entry_ids: list[int] = field(default_factory=list)
    site_urls: list[str] = field(default_factory=list)
    agg_max: float = 0.0
    verdict: str = Verdict.CLEAN.value
    image_sha_set: set[str] = field(default_factory=set)
    created_at: str = field(default_factory=_now)

    @classmethod
    def verdict_rank(cls, verdict: Any) -> int:
        """判定档位序数:clean=0 < suspect=1 < nsfw=2;未知/空 → -1。"""
        return _VERDICT_RANK.get(_norm_verdict(verdict), -1)

    @classmethod
    def worst_verdict(cls, *verdicts: Any) -> str:
        """返回若干判定值中最严重的档位;无任何有效判定时返回 "clean"。

        未知 / 空判定不参与竞选(低于 clean),因此结果恒为合法档位字符串。
        """
        worst = Verdict.CLEAN.value
        for value in verdicts:
            if cls.verdict_rank(value) > cls.verdict_rank(worst):
                worst = _norm_verdict(value)
        return worst

    def sort_key(self) -> tuple[int, float, int, str]:
        """列表排序键:(verdict 档, agg_max, 组规模)降序,组名升序兜底。

        契约给定前三键降序;三键全同的相邻组按组名升序,使输出完全确定。
        """
        return (
            -self.verdict_rank(self.verdict),
            -self.agg_max,
            -len(self.entry_ids),
            self.name,
        )


# ---------------------------------------------------------------------------
# A103 canonical 惰性导入 + 内置同语义兜底
# ---------------------------------------------------------------------------


def _load_canonical() -> Any | None:
    """惰性导入 A103 canonical;任何异常按未就位处理(返回 None 走兜底)。

    守卫刻意放宽(不限于 ImportError):兄弟模块破损(如 SyntaxError)、
    被测试用 None 占位阻断等,一律静默降级,绝不影响分组主流程。
    """
    try:
        return importlib.import_module(_CANONICAL_MODULE)
    except Exception as exc:  # noqa: BLE001 - 守卫宽:任何异常按未就位
        logger.debug("canonical(A103)未就位,启用内置兜底:%s", exc)
        return None


def _is_ip_literal(host: str) -> bool:
    """host 是否为 IP 地址字面量(快路径:普通域名直接短路)。"""
    if ":" not in host and not host[:1].isdigit():
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _builtin_canonical_key(url: str) -> str:
    """内置兜底 canonical_key(语义与 A103 契约一致)。

    规则:小写可注册域——末两段命中多段后缀表(com.cn 等)时取末三段,
    否则取末两段;IP 直连返回 IP 串;单标签主机(如 localhost)取其本身;
    解析失败(空串 / 无 host / 结构非法)返回 ""。纯字符串运算,不发网。
    """
    try:
        host = urlsplit(str(url or "").strip()).hostname
    except ValueError:
        return ""
    host = (host or "").rstrip(".")
    if not host:
        return ""
    if _is_ip_literal(host):
        return host
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI_SUFFIXES:
        return ".".join(labels[-3:])
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return labels[-1]


def _builtin_canonical_name(url: str) -> str:
    """内置兜底 canonical_name:成功 → canonical_key;失败 → 原 URL 截断。"""
    key = _builtin_canonical_key(url)
    if key:
        return key
    return str(url or "").strip()[:_NAME_TRUNCATE]


def _canonical_pair(url: str, canonical: Any | None) -> tuple[str, str]:
    """返回 (canonical_key, canonical_name);A103 可用则优先,异常落兜底。

    A103 返回的 key 为 ""(解析失败)时尊重其结论,不再追问兜底;只有
    A103 调用本身抛异常 / 缺接口时才改用内置实现,保证语义与契约对齐。
    """
    if canonical is not None:
        try:
            key = str(getattr(canonical, "canonical_key")(url) or "")
            name = str(getattr(canonical, "canonical_name")(url) or "")
            return key, name
        except Exception as exc:  # noqa: BLE001 - 单条解析异常按兜底处理
            logger.debug("canonical 对 %r 解析异常,按内置兜底处理:%s", url, exc)
    return _builtin_canonical_key(url), _builtin_canonical_name(url)


def _host_of(url: str) -> str:
    """提取 URL 的 host(小写、去端口、去 IPv6 方括号、去尾点);失败 → ""。"""
    try:
        host = urlsplit(str(url or "").strip()).hostname
    except ValueError:
        host = ""
    return (host or "").rstrip(".").lower()


# ---------------------------------------------------------------------------
# group_entries 主流程
# ---------------------------------------------------------------------------


@dataclass
class _Bucket:
    """分组聚合过程中的中间累积器(避免边聚合边构造 CaseGroup)。"""

    name: str
    aliases: list[str] = field(default_factory=list)
    entry_ids: list[int] = field(default_factory=list)
    site_urls: list[str] = field(default_factory=list)
    verdicts: list[str] = field(default_factory=list)
    aggs: list[float] = field(default_factory=list)
    shas: set[str] = field(default_factory=set)


def _entry_id(entry: Any) -> int:
    """鸭子容错读取条目 id;缺失 / 非数字一律按 0(不抛错)。"""
    try:
        return int(getattr(entry, "id", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _safe_agg(report: Any) -> float:
    """鸭子容错读取 report 的 agg_nsw_prob;缺失 / 非法按 0.0。"""
    try:
        return float(getattr(report, "agg_nsw_prob", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _report_shas(report: Any) -> set[str]:
    """收集单个 report 全部页面图片的 sha256(跳过空 sha,自然去重)。"""
    shas: set[str] = set()
    pages = getattr(report, "pages", None)
    if not isinstance(pages, (list, tuple)):
        return shas
    for page in pages:
        evidences = getattr(page, "image_evidences", None)
        if not isinstance(evidences, (list, tuple)):
            continue
        for evidence in evidences:
            sha = str(getattr(evidence, "sha256", "") or "").strip()
            if sha:
                shas.add(sha)
    return shas


def group_entries(
    entries: Iterable[EntryLike] | None,
    reports: dict[int, Any] | None,
) -> list[CaseGroup]:
    """把复核条目按 canonical(可注册域)归并为基础案件组。

    参数:
      entries:EntryLike 鸭子列表(getattr 容错 id/site_url/verdict/
        evidence_zip);``None`` 或空序列返回 []。
      reports:entry_id → SiteReport 鸭子映射,提供 agg_nsw_prob 与页面图片
        sha;缺某条目时该条目不贡献 agg 与 sha;``None``/空 dict 同样可工作
        (纯条目分组);多余的 id 不会被使用。

    归并口径:canonical_key 相同 → 同组;解析失败(key 为 "")的条目**各自
    成组**(以原始 URL 为组键),避免互不相干的非法 URL 被错误并组。

    返回:按 (verdict 档, agg_max, 组规模) 降序(组名升序兜底)的 CaseGroup
    列表;组内 entry_ids 保持输入顺序,aliases/site_urls 去重且保持首见顺序。
    本函数纯本地计算,不联网、不落盘、不触发任何提交动作。
    """
    entries = list(entries or [])
    if not entries:
        return []
    reports = reports or {}

    canonical = _load_canonical()
    used_fallback = canonical is None
    buckets: dict[str, _Bucket] = {}

    with telemetry.timer("case_group.group_entries"):
        for entry in entries:
            site_url = str(getattr(entry, "site_url", "") or "").strip()
            entry_id = _entry_id(entry)
            key, canonical_name = _canonical_pair(site_url, canonical)
            # 解析失败的条目按原始 URL 独立成组,互不合并。
            group_key = key if key else f"raw::{site_url}"
            bucket = buckets.get(group_key)
            if bucket is None:
                bucket = _Bucket(name=canonical_name)
                buckets[group_key] = bucket
            bucket.entry_ids.append(entry_id)
            if site_url and site_url not in bucket.site_urls:
                bucket.site_urls.append(site_url)
            host = _host_of(site_url)
            if host and host not in bucket.aliases:
                bucket.aliases.append(host)
            verdict = _norm_verdict(getattr(entry, "verdict", ""))
            if verdict:
                bucket.verdicts.append(verdict)
            report = reports.get(entry_id)
            if report is not None:
                bucket.aggs.append(_safe_agg(report))
                bucket.shas.update(_report_shas(report))

        groups = [
            CaseGroup(
                name=bucket.name,
                aliases=list(bucket.aliases),
                entry_ids=list(bucket.entry_ids),
                site_urls=list(bucket.site_urls),
                agg_max=max(bucket.aggs) if bucket.aggs else 0.0,
                verdict=CaseGroup.worst_verdict(*bucket.verdicts),
                image_sha_set=set(bucket.shas),
            )
            for bucket in buckets.values()
        ]
        groups.sort(key=CaseGroup.sort_key)

    if used_fallback:
        telemetry.inc("case_group.canonical_fallback")
    telemetry.inc("case_group.groups", len(groups))
    telemetry.inc("case_group.entries_grouped", len(entries))
    return groups
