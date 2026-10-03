"""A105 netsentinel.intel.group_linker 团伙并组测试。

纯离线、零网络、零第三方:FakeGraph 注入 A46 ``related_sites`` 口
(返回 ``[{"site", "via", "weight"}…]``),FakeGroup 与 A104 ``CaseGroup``
八字段同形(鸭子替身,A104 并行开发中不做硬 import)。覆盖:

- ``_jaccard`` 数值精确(全等 / 部分交集 / 不相交 / 双空 / 单空);
- 图谱建边并组:shared_image / redirect / phash_near;shared_template
  开关(默认并入、merge_template=False 不并、混合种类不受牵连);
- 指纹重叠并组:阈值边界 0.3 恰达(6/20)并 / 略差(6/22)不并;
  零重叠即使阈值为 0 也不并;高阈值要求强重叠;
- graph=None / 空图路径:仅指纹重叠判定;无关联多组保持(且原对象透传);
- 合并后字段:aliases / site_urls / entry_ids / image_sha_set 并集去重、
  agg_max 取最大、verdict 取最严重(含 Verdict 枚举)、主名取规模最大
  子组(并列时站点数多者 / 主名字典序)、created_at 取最早;
- union-find 传递闭包(图边 + 指纹边混合成链);
- 输出排序同 A104 键;幂等重跑;空输入;脏数据容忍(非 dict 结果 /
  缺 site / 查询抛错 / 自环命中 / 无主站点)。
"""
from __future__ import annotations

import dataclasses
from typing import Any

from netsentinel.contracts import Verdict
from netsentinel.intel.group_linker import _jaccard, merge_groups


# ---------------------------------------------------------------------------
# 构造辅助(鸭子替身)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class FakeGroup:
    """与 A104 CaseGroup 八字段同形的鸭子替身。"""

    name: str
    aliases: list[str] = dataclasses.field(default_factory=list)
    entry_ids: list[int] = dataclasses.field(default_factory=list)
    site_urls: list[str] = dataclasses.field(default_factory=list)
    agg_max: float = 0.0
    verdict: str = "clean"
    image_sha_set: set[str] = dataclasses.field(default_factory=set)
    created_at: str = ""


class FakeGraph:
    """A46 EvidenceGraph.related_sites 口的最小替身。

    relations: {查询 URL → [{"site", "via", "weight"}…]},缺省查不到;
    error_on 中的查询直接抛错,验证降级不炸。
    """

    def __init__(
        self,
        relations: dict[str, list[dict[str, Any]]] | None = None,
        error_on: set[str] | None = None,
    ) -> None:
        self.relations: dict[str, list[dict[str, Any]]] = dict(relations or {})
        self.error_on = set(error_on or ())
        self.queries: list[str] = []

    def related_sites(self, url: str, depth: int = 1) -> list[dict[str, Any]]:
        self.queries.append(url)
        if url in self.error_on:
            raise RuntimeError("图谱查询失败(测试注入)")
        return self.relations.get(url, [])


def _group(
    name: str,
    *,
    ids: list[int] | None = None,
    urls: list[str] | None = None,
    aliases: list[str] | None = None,
    agg: float = 0.0,
    verdict: str = "clean",
    shas: set[str] | None = None,
    created_at: str = "",
) -> FakeGroup:
    """快捷造组:缺省 urls=[f"https://{name}/"],aliases 含主名。"""
    return FakeGroup(
        name=name,
        aliases=list(aliases if aliases is not None else [name]),
        entry_ids=list(ids or []),
        site_urls=list(urls if urls is not None else [f"https://{name}/"]),
        agg_max=agg,
        verdict=verdict,
        image_sha_set=set(shas or ()),
        created_at=created_at,
    )


def _shas(prefix: str, count: int, start: int = 0) -> set[str]:
    """生成 count 个以 prefix 命名的指纹(如 sha_i7)。"""
    return {f"{prefix}_i{start + k}" for k in range(count)}


def _signature(groups: list[Any]) -> list[tuple]:
    """把组列表折叠成可比较的字段签名(幂等 / 排序断言用)。"""
    return [
        (
            g.name,
            tuple(g.aliases),
            tuple(g.entry_ids),
            tuple(g.site_urls),
            g.agg_max,
            str(g.verdict),
            frozenset(g.image_sha_set),
            g.created_at,
        )
        for g in groups
    ]


# ---------------------------------------------------------------------------
# _jaccard 数值精确
# ---------------------------------------------------------------------------


def test_jaccard_exact_values() -> None:
    assert _jaccard({"a", "b", "c"}, {"a", "b", "c"}) == 1.0
    assert _jaccard({"a", "b"}, {"b", "c"}) == 1 / 3
    assert _jaccard({"a", "b", "c", "d"}, {"c", "d", "e", "f"}) == 2 / 6
    assert _jaccard({"a"}, {"b"}) == 0.0
    assert _jaccard({"a"}, {"a", "b"}) == 1 / 2


def test_jaccard_empty_union_returns_zero() -> None:
    # 契约:空并集 → 0(双空不得算作完全相似 1.0)
    assert _jaccard(set(), set()) == 0.0
    assert _jaccard(set(), {"a"}) == 0.0
    assert _jaccard({"a"}, set()) == 0.0


def test_jaccard_boundary_six_of_twenty_is_exactly_threshold() -> None:
    # 6/20 与字面量 0.3 在 IEEE 双精度下严格相等(边界无容差依据)
    a = _shas("sha", 13)          # i0..i12
    b = {f"sha_i{k}" for k in range(7, 20)}  # i7..i19
    assert len(a & b) == 6 and len(a | b) == 20
    assert _jaccard(a, b) == 0.3


# ---------------------------------------------------------------------------
# 空输入 / 无关联保持
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty_list() -> None:
    assert merge_groups([]) == []
    assert merge_groups([], graph=FakeGraph(), overlap_threshold=0.3) == []


def test_unrelated_groups_stay_apart_and_pass_through() -> None:
    a = _group("a.com", ids=[1], shas=_shas("a", 3))
    b = _group("b.com", ids=[2], shas=_shas("b", 3))
    c = _group("c.com", ids=[3], shas=_shas("c", 3))
    out = merge_groups([a, b, c])
    assert len(out) == 3
    # 无关联的组原对象透传(身份不变;输出按排序键恰为名称升序)
    assert [id(g) for g in out] == [id(a), id(b), id(c)]


# ---------------------------------------------------------------------------
# 图谱建边并组
# ---------------------------------------------------------------------------


def test_graph_shared_image_edge_merges() -> None:
    a = _group("a.com", ids=[1], agg=0.9, verdict="nsfw")
    b = _group("b.com", ids=[2], agg=0.4, verdict="suspect")
    graph = FakeGraph(
        {"https://a.com/": [{"site": "https://b.com/", "via": ["shared_image"], "weight": 0.5}]}
    )
    out = merge_groups([a, b], graph=graph)
    assert len(out) == 1
    merged = out[0]
    assert merged.name == "a.com"  # a 规模并列时站点数相同 → 主名字典序
    assert set(merged.site_urls) == {"https://a.com/", "https://b.com/"}
    assert set(merged.entry_ids) == {1, 2}


def test_graph_redirect_edge_merges() -> None:
    a = _group("a.com", ids=[1])
    b = _group("b.com", ids=[2])
    graph = FakeGraph(
        {"https://b.com/": [{"site": "https://a.com/x", "via": ["redirect"], "weight": 1.0}]}
    )
    assert len(merge_groups([a, b], graph=graph)) == 1


def test_graph_phash_near_edge_merges() -> None:
    a = _group("a.com", ids=[1])
    b = _group("b.com", ids=[2])
    graph = FakeGraph(
        {"https://a.com/": [{"site": "https://b.com/", "via": ["phash_near"], "weight": 0.8}]}
    )
    assert len(merge_groups([a, b], graph=graph)) == 1


def test_shared_template_merges_when_enabled() -> None:
    a = _group("a.com", ids=[1])
    b = _group("b.com", ids=[2])
    graph = FakeGraph(
        {"https://a.com/": [{"site": "https://b.com/", "via": ["shared_template"], "weight": 0.6}]}
    )
    # 默认 merge_template=True 并入
    assert len(merge_groups([a, b], graph=graph)) == 1
    # 显式 True 同样并入
    assert len(merge_groups([a, b], graph=graph, merge_template=True)) == 1


def test_shared_template_not_merged_when_disabled() -> None:
    a = _group("a.com", ids=[1], shas=_shas("a", 2))
    b = _group("b.com", ids=[2], shas=_shas("b", 2))
    graph = FakeGraph(
        {"https://a.com/": [{"site": "https://b.com/", "via": ["shared_template"], "weight": 0.6}]}
    )
    out = merge_groups([a, b], graph=graph, merge_template=False)
    assert len(out) == 2
    assert {g.name for g in out} == {"a.com", "b.com"}


def test_template_off_does_not_block_other_kinds() -> None:
    # via 混合 shared_template + redirect:关闭模板后 redirect 仍然生效
    a = _group("a.com", ids=[1])
    b = _group("b.com", ids=[2])
    graph = FakeGraph(
        {
            "https://a.com/": [
                {"site": "https://b.com/", "via": ["shared_template", "redirect"], "weight": 1.0}
            ]
        }
    )
    out = merge_groups([a, b], graph=graph, merge_template=False)
    assert len(out) == 1
    # 纯 shared_template + phash_near:关闭模板后仍靠 phash_near 并入
    graph2 = FakeGraph(
        {
            "https://a.com/": [
                {"site": "https://b.com/", "via": ["shared_template", "phash_near"], "weight": 0.9}
            ]
        }
    )
    assert len(merge_groups([a, b], graph=graph2, merge_template=False)) == 1


def test_empty_via_list_never_merges() -> None:
    a = _group("a.com", ids=[1])
    b = _group("b.com", ids=[2])
    graph = FakeGraph(
        {"https://a.com/": [{"site": "https://b.com/", "via": [], "weight": 0.9}]}
    )
    assert len(merge_groups([a, b], graph=graph)) == 2


def test_graph_queries_use_name_and_all_site_urls() -> None:
    a = _group(
        "a.com",
        ids=[1],
        urls=["https://a.com/", "https://www.a.com/x"],
    )
    b = _group("b.com", ids=[2])
    graph = FakeGraph(
        {"https://www.a.com/x": [{"site": "https://b.com/", "via": ["redirect"], "weight": 1.0}]}
    )
    out = merge_groups([a, b], graph=graph)
    assert len(out) == 1
    # 主名与每个 site_url 都被查询过
    assert "a.com" in graph.queries
    assert "https://a.com/" in graph.queries
    assert "https://www.a.com/x" in graph.queries


def test_graph_hit_matches_by_host_ignores_path_and_port() -> None:
    a = _group("a.com", ids=[1], urls=["https://a.com:8443/base"])
    b = _group("b.com", ids=[2])
    graph = FakeGraph(
        {"https://a.com:8443/base": [{"site": "https://b.com/some/path", "via": ["shared_image"], "weight": 0.5}]}
    )
    assert len(merge_groups([a, b], graph=graph)) == 1


def test_graph_self_hit_and_unowned_site_ignored() -> None:
    a = _group("a.com", ids=[1], urls=["https://a.com/", "https://mirror.a.com/"])
    b = _group("b.com", ids=[2])
    graph = FakeGraph(
        {
            # 命中本组自己的镜像 → 不建边
            "https://a.com/": [{"site": "https://mirror.a.com/", "via": ["redirect"], "weight": 1.0}],
            # 命中不属于任何组的站点 → 忽略
            "https://b.com/": [{"site": "https://stranger.net/", "via": ["shared_image"], "weight": 1.0}],
        }
    )
    assert len(merge_groups([a, b], graph=graph)) == 2


def test_graph_dirty_results_tolerated() -> None:
    a = _group("a.com", ids=[1])
    b = _group("b.com", ids=[2])
    graph = FakeGraph(
        {
            "https://a.com/": [
                "not-a-dict",                              # 非字典条目
                {"via": ["shared_image"], "weight": 1.0},  # 缺 site 键
                {"site": "https://b.com/", "via": ["shared_image"], "weight": 1.0},  # 合法
            ]
        }
    )
    assert len(merge_groups([a, b], graph=graph)) == 1


def test_graph_query_error_degrades_to_fingerprint_only() -> None:
    a = _group("a.com", ids=[1], shas=_shas("x", 4))
    b = _group("b.com", ids=[2], shas=_shas("x", 4))  # 完全同图 → 指纹重叠 1.0
    graph = FakeGraph(error_on={"https://a.com/"})
    out = merge_groups([a, b], graph=graph)
    assert len(out) == 1  # 图谱查询抛错不阻断,指纹重叠仍然并组


# ---------------------------------------------------------------------------
# 指纹重叠并组(图谱缺省 / 空图)
# ---------------------------------------------------------------------------


def test_fingerprint_overlap_merges_without_graph() -> None:
    a = _group("a.com", ids=[1], shas=_shas("x", 10))
    b = _group("b.com", ids=[2], shas=_shas("x", 10))  # Jaccard=1.0
    out = merge_groups([a, b])
    assert len(out) == 1
    assert out[0].name in {"a.com", "b.com"}
    assert len(out[0].image_sha_set) == 10


def test_fingerprint_boundary_exactly_at_threshold_merges() -> None:
    a = _group("a.com", ids=[1], shas={f"sha_i{k}" for k in range(13)})
    b = _group("b.com", ids=[2], shas={f"sha_i{k}" for k in range(7, 20)})
    out = merge_groups([a, b], overlap_threshold=0.3)  # 6/20 == 0.3,恰达并组
    assert len(out) == 1
    assert len(out[0].image_sha_set) == 20


def test_fingerprint_just_below_threshold_stays_apart() -> None:
    a = _group("a.com", ids=[1], shas={f"sha_i{k}" for k in range(14)})
    b = _group("b.com", ids=[2], shas={f"sha_i{k}" for k in range(8, 22)})
    out = merge_groups([a, b], overlap_threshold=0.3)  # 6/22 < 0.3
    assert len(out) == 2


def test_fingerprint_zero_overlap_never_merges_even_threshold_zero() -> None:
    a = _group("a.com", ids=[1], shas=_shas("a", 3))
    b = _group("b.com", ids=[2], shas=_shas("b", 3))
    out = merge_groups([a, b], overlap_threshold=0.0)  # 零重叠仍不并
    assert len(out) == 2


def test_high_threshold_requires_strong_overlap() -> None:
    same = _shas("x", 6)
    a = _group("a.com", ids=[1], shas=same | _shas("a", 3))
    b = _group("b.com", ids=[2], shas=same | _shas("b", 3))
    assert len(merge_groups([a, b], overlap_threshold=0.9)) == 2  # 6/12=0.5
    assert len(merge_groups([a, b], overlap_threshold=0.5)) == 1  # 恰达 0.5


def test_graph_none_without_overlap_keeps_groups() -> None:
    a = _group("a.com", ids=[1], shas=_shas("a", 4))
    b = _group("b.com", ids=[2], shas=_shas("b", 4))
    assert len(merge_groups([a, b], graph=None)) == 2


def test_empty_graph_uses_fingerprint_only() -> None:
    a = _group("a.com", ids=[1], shas=_shas("x", 5))
    b = _group("b.com", ids=[2], shas=_shas("x", 5))
    c = _group("c.com", ids=[3], shas=_shas("c", 5))
    out = merge_groups([a, b, c], graph=FakeGraph())
    assert len(out) == 2
    assert {g.name for g in out} >= {"c.com"}
    merged = next(g for g in out if len(g.entry_ids) == 2)
    assert {g.name for g in out if len(g.entry_ids) == 2} <= {"a.com", "b.com"}
    assert set(merged.entry_ids) == {1, 2}


# ---------------------------------------------------------------------------
# 合并后字段
# ---------------------------------------------------------------------------


def test_merged_fields_union_and_dedup() -> None:
    a = _group(
        "a.com",
        ids=[1, 2],
        urls=["https://a.com/", "https://www.a.com/"],
        aliases=["a.com", "www.a.com"],
        shas={"sha1", "sha2"},
    )
    b = _group(
        "b.com",
        ids=[2, 3],  # entry 2 与 a 重叠 → 并集去重
        urls=["https://b.com/", "https://a.com/"],  # a 的 URL 重复出现
        aliases=["b.com", "www.a.com"],  # 别名重复出现
        shas={"sha2", "sha3"},
    )
    graph = FakeGraph(
        {"https://a.com/": [{"site": "https://b.com/", "via": ["shared_image"], "weight": 0.5}]}
    )
    merged = merge_groups([a, b], graph=graph)[0]
    assert set(merged.entry_ids) == {1, 2, 3}
    assert merged.entry_ids == sorted(merged.entry_ids)
    assert set(merged.site_urls) == {"https://a.com/", "https://www.a.com/", "https://b.com/"}
    assert set(merged.aliases) == {"a.com", "www.a.com", "b.com"}
    assert merged.image_sha_set == {"sha1", "sha2", "sha3"}


def test_merged_agg_max_and_verdict_most_severe() -> None:
    a = _group("a.com", ids=[1], agg=0.95, verdict="suspect")
    b = _group("b.com", ids=[2], agg=0.30, verdict="nsfw")
    graph = FakeGraph(
        {"https://a.com/": [{"site": "https://b.com/", "via": ["redirect"], "weight": 1.0}]}
    )
    merged = merge_groups([a, b], graph=graph)[0]
    assert merged.agg_max == 0.95
    assert merged.verdict == "nsfw"
    # clean + suspect → suspect
    c = _group("c.com", ids=[3], agg=0.1, verdict="clean")
    d = _group("d.com", ids=[4], agg=0.2, verdict="suspect")
    graph2 = FakeGraph(
        {"https://c.com/": [{"site": "https://d.com/", "via": ["redirect"], "weight": 1.0}]}
    )
    assert merge_groups([c, d], graph=graph2)[0].verdict == "suspect"


def test_merged_verdict_accepts_verdict_enum() -> None:
    a = _group("a.com", ids=[1], verdict="clean")
    b = _group("b.com", ids=[2], verdict=Verdict.NSFW)  # type: ignore[arg-type]
    graph = FakeGraph(
        {"https://a.com/": [{"site": "https://b.com/", "via": ["redirect"], "weight": 1.0}]}
    )
    merged = merge_groups([a, b], graph=graph)[0]
    assert merged.verdict == Verdict.NSFW
    assert merged.verdict == "nsfw"


def test_name_taken_from_largest_subgroup() -> None:
    big = _group("big.com", ids=[1, 2, 3])
    small = _group("small.com", ids=[4])
    graph = FakeGraph(
        {"https://small.com/": [{"site": "https://big.com/", "via": ["shared_image"], "weight": 0.5}]}
    )
    assert merge_groups([big, small], graph=graph)[0].name == "big.com"


def test_name_tie_broken_by_site_count_then_lexicographic() -> None:
    # 规模并列 → 站点数多者胜
    many_urls = _group("z.com", ids=[1], urls=["https://z.com/", "https://mirror.z.com/"])
    one_url = _group("a.com", ids=[2], urls=["https://a.com/"])
    graph = FakeGraph(
        {"https://a.com/": [{"site": "https://z.com/", "via": ["redirect"], "weight": 1.0}]}
    )
    assert merge_groups([many_urls, one_url], graph=graph)[0].name == "z.com"
    # 规模与站点数都并列 → 主名字典序小者胜(确定性)
    p = _group("beta.com", ids=[1])
    q = _group("alpha.com", ids=[2])
    graph2 = FakeGraph(
        {"https://beta.com/": [{"site": "https://alpha.com/", "via": ["redirect"], "weight": 1.0}]}
    )
    assert merge_groups([p, q], graph=graph2)[0].name == "alpha.com"


def test_merged_created_at_takes_earliest() -> None:
    a = _group("a.com", ids=[1], created_at="2026-10-01T09:00:00+08:00")
    b = _group("b.com", ids=[2], created_at="2026-09-28T18:30:00+08:00")
    graph = FakeGraph(
        {"https://a.com/": [{"site": "https://b.com/", "via": ["redirect"], "weight": 1.0}]}
    )
    assert merge_groups([a, b], graph=graph)[0].created_at == "2026-09-28T18:30:00+08:00"


# ---------------------------------------------------------------------------
# union-find 传递性 / 排序 / 幂等
# ---------------------------------------------------------------------------


def test_transitive_chain_merges_all_three() -> None:
    # a—b 靠图谱 redirect,b—c 靠指纹重叠 → 传递闭包三组归一
    a = _group("a.com", ids=[1], shas=_shas("u", 3))
    b = _group("b.com", ids=[2], shas=_shas("u", 3) | _shas("v", 3))
    c = _group("c.com", ids=[3], shas=_shas("v", 3))
    graph = FakeGraph(
        {"https://a.com/": [{"site": "https://b.com/", "via": ["redirect"], "weight": 1.0}]}
    )
    out = merge_groups([a, b, c], graph=graph)
    assert len(out) == 1
    assert set(out[0].entry_ids) == {1, 2, 3}


def test_two_independent_components_stay_two_groups() -> None:
    # a—b 靠图谱边成一组;c—d 靠指纹重叠成另一组;两组互不粘连
    a = _group("a.com", ids=[1])
    b = _group("b.com", ids=[2])
    c = _group("c.com", ids=[3, 5], shas=_shas("w", 4))
    d = _group("d.com", ids=[4, 6], shas=_shas("w", 4))
    graph = FakeGraph(
        {"https://a.com/": [{"site": "https://b.com/", "via": ["shared_image"], "weight": 0.5}]}
    )
    out = merge_groups([a, b, c, d], graph=graph)
    assert len(out) == 2
    sizes = sorted(len(g.entry_ids) for g in out)
    assert sizes == [2, 4]
    assert next(g for g in out if len(g.entry_ids) == 4).name == "c.com"


def test_output_sorted_like_a104_key() -> None:
    nsfw_big = _group("z.com", ids=[1, 2, 3], agg=0.9, verdict="nsfw")
    nsfw_small = _group("y.com", ids=[4], agg=0.99, verdict="nsfw")
    suspect = _group("x.com", ids=[5, 6], agg=0.7, verdict="suspect")
    clean = _group("w.com", ids=[7], agg=0.1, verdict="clean")
    out = merge_groups([clean, suspect, nsfw_small, nsfw_big])
    assert [g.name for g in out] == ["y.com", "z.com", "x.com", "w.com"]
    # 同档内 agg 高者前(y 0.99 > z 0.9);再同则规模大者前(补一例)
    p = _group("p.com", ids=[1, 2], agg=0.5, verdict="nsfw")
    q = _group("q.com", ids=[3], agg=0.5, verdict="nsfw")
    assert [g.name for g in merge_groups([q, p])] == ["p.com", "q.com"]


def test_idempotent_rerun_unchanged() -> None:
    a = _group("a.com", ids=[1], agg=0.8, verdict="suspect", shas=_shas("u", 4), created_at="2026-10-01T08:00:00+08:00")
    b = _group("b.com", ids=[2], agg=0.4, verdict="nsfw", shas=_shas("u", 4) | _shas("v", 2), created_at="2026-09-30T08:00:00+08:00")
    c = _group("c.com", ids=[3], shas=_shas("c", 2))
    graph = FakeGraph(
        {
            "https://a.com/": [
                {"site": "https://b.com/", "via": ["shared_image"], "weight": 0.5},
                {"site": "https://c.com/", "via": ["redirect"], "weight": 1.0},
            ]
        }
    )
    first = merge_groups([a, b, c], graph=graph)
    assert len(first) == 1  # a-b-c 全并
    second = merge_groups(first, graph=graph)
    assert _signature(second) == _signature(first)
    third = merge_groups(second, graph=graph, merge_template=True)
    assert _signature(third) == _signature(first)
