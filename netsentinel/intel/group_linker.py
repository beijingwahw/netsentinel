"""团伙并组(A105):基础组经证据图谱 / 图片指纹重叠并入同一案件组。

依据 CONTRACTS-V6.md §2/§4 A105 条目:镜像归并(A104)产出"基础组"之后,
不同基础组之间若存在以下任一团伙痕迹,则并入同一**案件组**(union-find):

- 证据图谱(A46 ``EvidenceGraph``)关联边:对每组的 name 与各 site_url
  逐一查询 ``graph.related_sites``,返回站点命中**其他组成员 host** 即建边
  (``via`` 为边种类列表);``shared_template`` 类边仅在 ``merge_template=True``
  时计入——模板复用的指向性弱于共享图片 / 感知哈希 / 重定向,允许运营者关闭;
- 图片指纹重叠:两组 ``image_sha_set`` 的 Jaccard 相似度(见 ``_jaccard``)
  ≥ ``overlap_threshold``(契约 Config.group_merge_phash_overlap 默认 0.3),
  且两组至少共享一张图片(零重叠永不并组)。

``graph=None`` 或图谱查不到任何关联时,跳过图谱判定,仅凭指纹重叠并组。

合并规则(契约 §2"案件组"):新组 name 取**规模最大子组**的主名(并列时
取站点数多者、再按名称升序,保证确定);aliases / entry_ids / site_urls /
image_sha_set 取并集去重;agg_max 取最大;verdict 取最严重档;created_at
取最早(案件组溯源到首个子组的建立时刻)。输出排序同 A104:
(verdict 档, agg_max, 组规模) 降序,末位按名称升序;空输入 → []。

幂等:对已并组的列表重跑,合并结果不再变化(并集运算对已并集封闭)。

工程约束:纯标准库、零网络、零第三方依赖;A104 ``CaseGroup`` 并行开发中,
本模块只做**鸭子属性访问**(name/aliases/entry_ids/site_urls/agg_max/
verdict/image_sha_set/created_at),构造新组时惰性导入 A104 的
``CaseGroup``,未就位则退回本地同形兜底 dataclass——两边字段与契约 §4
逐一同形,调用方无感知。
"""
from __future__ import annotations

import dataclasses
import importlib
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from netsentinel import telemetry

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查引用,运行时绝不导入
    from netsentinel.intel.case_group import CaseGroup

logger = logging.getLogger(__name__)

__all__ = ["merge_groups"]

#: 图谱边种类 "shared_template"(与 A46 graph.EDGE_SHARED_TEMPLATE 同值;
#: 为避免对兄弟模块建立导入期耦合,此处本地定义字面量)。
_EDGE_SHARED_TEMPLATE = "shared_template"

#: 判定档位 → 严重度权重(与 decision.fusion._VERDICT_ORDER 同口径:
#: clean < suspect < nsfw;未知名一律视为最低档,不参与"最严重"竞争)。
_VERDICT_RANK: dict[str, int] = {"clean": 0, "suspect": 1, "nsfw": 2}


# ---------------------------------------------------------------------------
# A104 CaseGroup 的惰性构造(并行契约:鸭子访问,不硬 import)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _FallbackCaseGroup:
    """A104 未就位时的本地兜底案件组。

    字段与契约 §4 A104 ``CaseGroup`` 逐一同形;A104 落地后
    ``_case_group_ctor`` 会优先返回真正的 ``CaseGroup``,本类仅为
    并行期的离线替身,调用方按鸭子属性读即可,无感知差异。
    """

    name: str
    aliases: list[str] = dataclasses.field(default_factory=list)
    entry_ids: list[int] = dataclasses.field(default_factory=list)
    site_urls: list[str] = dataclasses.field(default_factory=list)
    agg_max: float = 0.0
    verdict: str = "clean"
    image_sha_set: set[str] = dataclasses.field(default_factory=set)
    created_at: str = ""


def _case_group_ctor() -> Any:
    """惰性取 A104 ``CaseGroup`` 构造器;未就位退回本地兜底 dataclass。"""
    try:
        module = importlib.import_module("netsentinel.intel.case_group")
    except ImportError:
        return _FallbackCaseGroup
    return getattr(module, "CaseGroup", _FallbackCaseGroup)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _jaccard(a: set, b: set) -> float:
    """两集合的 Jaccard 相似度 ``|A∩B| / |A∪B|``;空并集(含双空)→ 0.0。

    入参容忍任意可迭代(内部先凝固为 set),数值精确:分子分母均为整数
    计数相除,不引入浮点累积误差(3/10 与字面量 0.3 在 IEEE 双精度下
    严格相等,边界判定 ``>=`` 无需容差)。
    """
    left, right = set(a), set(b)
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _verdict_rank(verdict: Any) -> int:
    """判定档位 → 严重度权重;兼容 Verdict 枚举(str 混入)与普通字符串。"""
    value = getattr(verdict, "value", verdict)
    return _VERDICT_RANK.get(str(value).strip().lower(), -1)


def _field(group: Any, name: str, default: Any) -> Any:
    """鸭子读 ``group`` 的字段;缺失或 None 时回退默认值(脏数据不炸)。"""
    value = getattr(group, name, default)
    return default if value is None else value


def _host_of(url: Any) -> str:
    """URL / 域名 → 小写 host(端口、路径、大小写差异全部抹平)。

    无 scheme 的裸域名补 ``//`` 再解析;解析失败(非法端口等)返回 ""。
    """
    text = str(url or "").strip()
    if not text:
        return ""
    candidate = text if "//" in text else f"//{text}"
    try:
        host = urlsplit(candidate).hostname
    except ValueError:
        return ""
    return (host or "").lower()


def _group_hosts(group: Any) -> set[str]:
    """一组成员 host:全部 site_urls 的 host ∪ 主名 host(去掉空值)。"""
    hosts = {_host_of(url) for url in _field(group, "site_urls", [])}
    hosts.add(_host_of(_field(group, "name", "")))
    return {host for host in hosts if host}


def _query_urls(group: Any) -> list[str]:
    """向图谱发起查询的键:主名 + 全部 site_urls(去空、去重、保序)。"""
    candidates = [str(_field(group, "name", ""))]
    candidates += [str(url) for url in _field(group, "site_urls", [])]
    return [url for url in dict.fromkeys(candidates) if url]


def _group_size(group: Any) -> int:
    """组规模 = 成员条目数(与 A104 排序键的"组规模"同口径)。"""
    return len(_field(group, "entry_ids", []) or [])


def _dedup_sorted(values: list[Any]) -> list[Any]:
    """去重(保首次出现)后尽量排序;元素不可比时保持去重序(脏数据容忍)。"""
    unique = list(dict.fromkeys(values))
    try:
        return sorted(unique)
    except TypeError:  # pragma: no cover - 混型 id 等脏数据
        return unique


# ---------------------------------------------------------------------------
# 边收集:图谱关联 + 指纹重叠
# ---------------------------------------------------------------------------


def _graph_edges(
    groups: list[Any], graph: Any, merge_template: bool
) -> set[frozenset[int]]:
    """经 ``graph.related_sites`` 收集组间团伙边(返回组下标对集合)。

    - 对每组的 name / site_urls 逐一查询;返回的每个关联站点按 host 归属
      到持该 host 的**其他**组 → 建边(命中本组自身或无主站点忽略);
    - ``via`` 边种类列表里,``shared_template`` 仅当 ``merge_template=True``
      计入,``shared_image`` / ``phash_near`` / ``redirect`` 恒计入;
    - 图谱缺失 ``related_sites`` 口或单次查询抛错 → 跳过该查询(降级为
      仅指纹重叠判定),不中断整批并组。
    """
    related = getattr(graph, "related_sites", None)
    if not callable(related):
        return set()

    host_owner: dict[str, int] = {}
    for idx, group in enumerate(groups):
        for host in _group_hosts(group):
            host_owner.setdefault(host, idx)  # 同 host 理论上已被 A104 归并,撞车取先到

    edges: set[frozenset[int]] = set()
    for idx, group in enumerate(groups):
        for url in _query_urls(group):
            try:
                results = related(url)
            except Exception:  # noqa: BLE001 - 图谱读写异常不阻断并组
                logger.debug("related_sites 查询失败,跳过:%s", url, exc_info=True)
                continue
            for item in results or []:
                if not isinstance(item, dict):
                    continue
                other = host_owner.get(_host_of(item.get("site")))
                if other is None or other == idx:
                    continue
                kinds = item.get("via") or []
                qualifies = any(
                    str(kind) != _EDGE_SHARED_TEMPLATE or merge_template
                    for kind in kinds
                )
                if qualifies:
                    edges.add(frozenset((idx, other)))
    return edges


def _fingerprint_edges(
    groups: list[Any], overlap_threshold: float
) -> set[frozenset[int]]:
    """组间图片指纹重叠边:Jaccard ≥ 阈值且交集非空(零重叠永不并组)。"""
    shas = [set(_field(group, "image_sha_set", set())) for group in groups]
    edges: set[frozenset[int]] = set()
    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            shared = shas[i] & shas[j]
            if shared and len(shared) / len(shas[i] | shas[j]) >= overlap_threshold:
                edges.add(frozenset((i, j)))
    return edges


# ---------------------------------------------------------------------------
# union-find 与并组
# ---------------------------------------------------------------------------


def _components(count: int, edges: set[frozenset[int]]) -> dict[int, list[int]]:
    """并查集(路径压缩 + 按秩合并)把下标按连通分量聚桶,键为根下标。"""
    parent = list(range(count))
    rank = [0] * count

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # 路径压缩(两趟迭代写法)
            parent[x], x = root, parent[x]
        return root

    for edge in edges:
        if len(edge) != 2:  # 自环被上游过滤,防御性跳过
            continue
        a, b = tuple(edge)
        a, b = find(a), find(b)
        if a == b:
            continue
        if rank[a] < rank[b]:
            a, b = b, a
        parent[b] = a
        if rank[a] == rank[b]:
            rank[a] += 1

    buckets: dict[int, list[int]] = {}
    for idx in range(count):
        buckets.setdefault(find(idx), []).append(idx)
    return buckets


def _merge_component(members: list[CaseGroup]) -> CaseGroup:
    """把同案的多个子组合并为一个案件组(合并规则见模块 docstring)。

    子组先按 (规模降序, 站点数降序, 主名升序) 排定次序:首位即"规模最大
    子组",其主名作为新组 name;verdict 并列时也由规模更大的一方胜出。
    """
    ordered = sorted(
        members,
        key=lambda g: (
            -_group_size(g),
            -len(_field(g, "site_urls", []) or []),
            str(_field(g, "name", "")),
        ),
    )
    lead = ordered[0]

    aliases = _dedup_sorted(
        [str(alias) for g in ordered for alias in _field(g, "aliases", [])]
    )
    entry_ids = _dedup_sorted([eid for g in ordered for eid in _field(g, "entry_ids", [])])
    site_urls = _dedup_sorted(
        [str(url) for g in ordered for url in _field(g, "site_urls", [])]
    )
    agg_max = max(
        (float(_field(g, "agg_max", 0.0) or 0.0) for g in ordered), default=0.0
    )
    worst = max(ordered, key=lambda g: _verdict_rank(_field(g, "verdict", "clean")))
    image_sha_set = {
        str(sha) for g in ordered for sha in _field(g, "image_sha_set", set())
    }
    stamps = [str(_field(g, "created_at", "")) for g in ordered]
    created_at = min((s for s in stamps if s), default="")

    fields = {
        "name": str(_field(lead, "name", "")),
        "aliases": aliases,
        "entry_ids": entry_ids,
        "site_urls": site_urls,
        "agg_max": agg_max,
        "verdict": _field(worst, "verdict", "clean"),
        "image_sha_set": image_sha_set,
        "created_at": created_at or str(_field(lead, "created_at", "")),
    }
    try:
        return _case_group_ctor()(**fields)
    except TypeError:  # pragma: no cover - A104 签名漂移时退回兜底同形类
        return _FallbackCaseGroup(**fields)


# ---------------------------------------------------------------------------
# 对外主口
# ---------------------------------------------------------------------------


def merge_groups(
    groups: list[CaseGroup],
    *,
    graph: Any = None,
    overlap_threshold: float = 0.3,
    merge_template: bool = True,
) -> list[CaseGroup]:
    """把存在团伙关联的基础组并入同一案件组(union-find)。

    :param groups: A104 产出的基础组列表(鸭子访问 CaseGroup 八字段);
    :param graph: A46 ``EvidenceGraph`` 或任何带 ``related_sites`` 口的对象
        (测试可注入 FakeGraph);``None`` 或无关联 → 仅凭指纹重叠判定;
    :param overlap_threshold: 图片指纹集合 Jaccard 并组阈值(≥ 才并,
        对应 Config.group_merge_phash_overlap,默认 0.3);
    :param merge_template: 是否把 ``shared_template`` 类图谱边计入并组
        (对应 Config.group_merge_template,默认 True);
    :return: 案件组列表,按 (verdict 档, agg_max, 组规模) 降序、末位主名
        升序;未并组的成员**原对象透传**;空输入 → []。

    幂等:对输出重跑本函数,已并组的成员不再分裂、字段不再变化。
    """
    items = list(groups or [])
    if not items:
        return []

    edges = _fingerprint_edges(items, overlap_threshold)
    if graph is not None:
        edges |= _graph_edges(items, graph, merge_template)

    merged = 0
    merged_members = 0
    output: list[CaseGroup] = []
    for members in _components(len(items), edges).values():
        if len(members) == 1:
            output.append(items[members[0]])  # 无关联:原对象透传
            continue
        component = [items[idx] for idx in members]
        output.append(_merge_component(component))
        merged += 1
        merged_members += sum(_group_size(group) for group in component)

    if merged:
        telemetry.inc("group_linker.merged_groups", merged)
        telemetry.inc("group_linker.merged_members", merged_members)
    logger.debug(
        "团伙并组完成:%d 个基础组 → %d 个案件组(新并 %d 组)",
        len(items),
        len(output),
        merged,
    )

    output.sort(
        key=lambda g: (
            -_verdict_rank(_field(g, "verdict", "clean")),
            -float(_field(g, "agg_max", 0.0) or 0.0),
            -_group_size(g),
            str(_field(g, "name", "")),
        )
    )
    return output
