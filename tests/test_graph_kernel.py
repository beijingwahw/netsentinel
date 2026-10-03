"""A128 netsentinel.intel.graph_kernel 增量并查集图谱内核测试。

纯离线、零网络、零第三方依赖(红线 12/29/30)。覆盖:union/find/connected
语义与返回值、路径压缩 + 按秩合并结构性质、传递链成团、components 惰性
缓存失效 / 重算计数、related 深度 1/2/大深度 BFS、环边进邻接表、
ingest_edges(生成器)、export 确定性、空 / 单点 / 自环、与 A46
EvidenceGraph 边种类共存(仅数据互导,不改 A46)、kernel_selfcheck;
红线 31 基准 ×2(10^4 union 后 components O(1) 零重算 + connected
操作计数 O(1)),全部以操作计数断言、禁墙钟。

A194 加权社区检测(Louvain)追加覆盖:louvain_pass 单层局部移动、
louvain_communities 多层聚合、louvain_modularity——模块度单调不减
(seeded 随机图 ×30)、空图 / 单点 / 全连通 / 双团 / 环形团经典结构、
确定性(打乱插入序与双向重复给出)、参数与操作计数(stats 四键)、
非法参数与负权中文 ValueError、kernel_selfcheck 的 louvain 附加键。
并查集旧测试全部保留。
"""
from __future__ import annotations

import json
import random
from typing import Any

import pytest

from netsentinel.intel.graph_kernel import (
    UnionFindKernel,
    kernel_selfcheck,
    louvain_communities,
    louvain_modularity,
    louvain_pass,
)

A = "https://a.example.com/"
B = "https://b.example.com/"
C = "https://c.example.com/"
D = "https://d.example.com/"

H1 = "1" * 64  # 图片 sha256(测试值,形状即可)
T1 = "f" * 16  # 模板 simhash


# ---------------------------------------------------------------------------
# union / find / connected 基本语义
# ---------------------------------------------------------------------------


def test_v7_union_true_only_on_new_merge() -> None:
    """union 仅在真实合并时返回 True;无向冗余(正反 / 跨分量合并后)返回 False。"""
    k = UnionFindKernel()
    assert k.union("a", "b") is True
    assert k.union("a", "b") is False  # 重复边
    assert k.union("b", "a") is False  # 无向:反向同样冗余
    assert k.union("c", "d") is True
    assert k.union("a", "c") is True  # 跨分量合并
    assert k.connected("b", "d") is True  # 传递成团
    assert k.connected("d", "b") is True  # 对称


def test_v7_add_idempotent_and_find_of_singleton() -> None:
    """add 新节点返回 True、重复返回 False;单点的 find 是自身。"""
    k = UnionFindKernel()
    assert k.add("n1") is True
    assert k.add("n1") is False
    assert k.find("n1") == "n1"
    assert k.export()["nodes"] == ["n1"]


def test_v7_find_auto_adds_unknown_node() -> None:
    """find 对未知节点自动登记并自成根;新节点令已物化的分量缓存失效。"""
    k = UnionFindKernel()
    k.components()  # 物化缓存(recompute 1)
    assert k.find("ghost") == "ghost"
    assert k.export()["nodes"] == ["ghost"]
    assert k.invalidate_count == 1  # 新节点 → 丢弃有效缓存
    assert k.components() == {"ghost": {"ghost"}}
    assert k.recompute_count == 2


def test_v7_connected_unknown_nodes_false_without_registration() -> None:
    """connected 是纯查询:未登记节点返回 False 且绝不登记(零副作用)。"""
    k = UnionFindKernel()
    k.union("a", "b")
    assert k.connected("a", "ghost") is False
    assert k.connected("ghost", "ghost") is False  # 未登记节点连自身也不算
    assert "ghost" not in k.export()["nodes"]


def test_v7_connected_with_self() -> None:
    """已登记节点与自身 connected 为 True(单点或成团后均如此)。"""
    k = UnionFindKernel()
    k.add("me")
    assert k.connected("me", "me") is True
    k.union("me", "you")
    assert k.connected("me", "me") is True


# ---------------------------------------------------------------------------
# 结构性质:路径压缩 + 按秩合并
# ---------------------------------------------------------------------------


def test_v7_path_compression_flattens_parents() -> None:
    """find 后路径上所有节点直挂根;再次 find 至多 1 次父指针跳转。"""
    k = UnionFindKernel()
    k.ingest_edges([("a", "b"), ("b", "c"), ("c", "d"), ("d", "e")])
    root = k.find("e")
    for node in "abcde":
        if node != root:
            assert k._parent[node] == root  # 压缩后直挂根
    before = k.find_hops
    assert k.find("e") == root
    assert k.find_hops - before <= 1


def test_v7_union_by_rank_bounds_tree_depth() -> None:
    """按秩合并:等秩合并秩 +1;10^3 单点逐个并入后树深 ≤ log2(1024)=10。"""
    k = UnionFindKernel()
    k.ingest_edges([("a", "b"), ("c", "d")])  # 两棵 rank-1 树
    k.union("a", "c")  # 等秩合并 → 根的秩 2
    root = k.find("a")
    assert k._rank[root] == 2
    for i in range(1_000):
        k.union(f"n{i}", "a")
    assert k.find("n999") == root
    depth = 0
    x = "n999"
    while k._parent[x] != x:
        x = k._parent[x]
        depth += 1
    assert depth <= 10


def test_v7_transitive_chain_forms_one_component() -> None:
    """10^3 长链经传递闭合成单一分量;related 沿链逐跳展开。"""
    k = UnionFindKernel()
    n = 1_000
    merged = k.ingest_edges((f"c{i}", f"c{i + 1}") for i in range(n - 1))
    assert merged == n - 1  # 链上每条边都是真实合并
    assert k.connected("c0", f"c{n - 1}") is True
    comps = k.components()
    assert len(comps) == 1
    assert len(comps[next(iter(comps))]) == n
    assert k.related("c0", 1) == {"c1"}
    assert k.related("c0", 2) == {"c1", "c2"}
    mid = n // 2
    assert k.related(f"c{mid}", 1) == {f"c{mid - 1}", f"c{mid + 1}"}


# ---------------------------------------------------------------------------
# components 惰性缓存:失效与重算计数
# ---------------------------------------------------------------------------


def test_v7_components_lazy_cache_recompute_count() -> None:
    """缓存命中返回同一对象且零重算;冗余 union 不失效;真合并恰失效一次。"""
    k = UnionFindKernel()
    assert k.recompute_count == 0 and k.invalidate_count == 0
    assert k.union("a", "b") is True
    assert k.invalidate_count == 0  # 缓存从未物化:不算失效

    c1 = k.components()
    assert k.recompute_count == 1
    assert k.components() is c1  # O(1) 命中:同一对象
    assert k.recompute_count == 1

    assert k.union("a", "b") is False  # 冗余 union:不失效、不重算
    assert k.components() is c1
    assert k.recompute_count == 1 and k.invalidate_count == 0

    assert k.union("b", "c") is True  # 真合并:丢弃有效缓存
    assert k.invalidate_count == 1
    c2 = k.components()
    assert c2 is not c1 and k.recompute_count == 2
    root = k.find("a")
    assert c2 == {root: {"a", "b", "c"}}


def test_v7_add_invalidates_components_cache() -> None:
    """新节点产生新单点分量:缓存失效、下次查询重算;重复 add 不失效。"""
    k = UnionFindKernel()
    k.union("a", "b")
    k.components()
    assert k.recompute_count == 1

    assert k.add("x") is True
    assert k.invalidate_count == 1
    c = k.components()
    assert k.recompute_count == 2
    assert c[k.find("x")] == {"x"}
    assert sum(len(members) for members in c.values()) == 3

    assert k.add("x") is False  # 幂等:不失效
    assert k.invalidate_count == 1
    k.components()
    assert k.recompute_count == 2


def test_v7_components_partition_invariants() -> None:
    """components 是全体节点的划分:两两不相交、根属于自身分量、覆盖全部。"""
    k = UnionFindKernel()
    k.ingest_edges([("a", "b"), ("b", "c"), ("d", "e")])
    k.add("solo")
    comps = k.components()
    all_members: set[str] = set()
    for root, members in comps.items():
        assert root in members  # 根是自己的分量成员
        assert not all_members & members  # 分量两两不相交
        all_members |= members
    assert all_members == {"a", "b", "c", "d", "e", "solo"}
    assert len(comps) == 3
    assert {k.find(x) for x in "abc"} <= set(comps)
    assert k.find("solo") == "solo"


# ---------------------------------------------------------------------------
# related:BFS 邻域
# ---------------------------------------------------------------------------


def test_v7_related_depth_one_and_two() -> None:
    """星 + 链拓扑:深度 1 只见直接邻居,深度 2 再展开一跳。"""
    k = UnionFindKernel()
    k.ingest_edges(
        [("hub", "a"), ("hub", "b"), ("a", "x"), ("b", "y"), ("y", "z")]
    )
    assert k.related("hub", 1) == {"a", "b"}
    assert k.related("hub", 2) == {"a", "b", "x", "y"}
    assert k.related("hub", 3) == {"a", "b", "x", "y", "z"}
    assert k.related("z", 1) == {"y"}


def test_v7_related_edge_cases() -> None:
    """未登记 / 孤立节点、depth<=0 均为空集;结果永不含自身。"""
    k = UnionFindKernel()
    assert k.related("ghost", 1) == set()  # 未登记节点
    k.add("lonely")
    assert k.related("lonely", 1) == set()  # 孤立节点无邻居
    k.union("a", "b")
    assert k.related("a", 0) == set()
    assert k.related("a", -2) == set()
    assert "a" not in k.related("a", 5)  # 永不含自身


def test_v7_related_large_depth_returns_whole_component() -> None:
    """深度 ≥ 分量直径时返回整个分量(去自身);孤立分量互不串扰。"""
    k = UnionFindKernel()
    k.ingest_edges([("a", "b"), ("b", "c"), ("c", "d")])
    k.add("island")
    assert k.related("a", 10) == {"b", "c", "d"}
    assert k.related("island", 10) == set()


def test_v7_cycle_edge_recorded_despite_redundant_union() -> None:
    """环边 union 返回 False(已同分量)但照实进入邻接表与导出边集。"""
    k = UnionFindKernel()
    assert k.union("a", "b") is True
    assert k.union("b", "c") is True
    assert k.union("c", "a") is False  # 环边:无结构变化
    assert k.related("a", 1) == {"b", "c"}  # 邻域拓扑仍记录 A-C
    assert len(k.export()["edges"]) == 3


# ---------------------------------------------------------------------------
# ingest_edges / export
# ---------------------------------------------------------------------------


def test_v7_ingest_edges_counts_merges_and_accepts_generator() -> None:
    """ingest_edges 接受生成器并返回真实合并数;空 / 冗余灌入返回 0。"""
    k = UnionFindKernel()
    merged = k.ingest_edges((f"g{i}", f"g{i + 1}") for i in range(5))
    assert merged == 5
    assert k.connected("g0", "g5") is True
    assert k.ingest_edges([]) == 0
    assert k.ingest_edges(iter([("g0", "g1")])) == 0  # 已同分量


def test_v7_export_deterministic_structure() -> None:
    """export:节点 / 边排序输出;边端点规范化、重复去重、自环不入边集。"""
    k = UnionFindKernel()
    k.ingest_edges([("b", "a"), ("a", "b"), ("c", "c"), ("d", "a")])
    data = k.export()
    assert set(data) == {"nodes", "edges"}
    assert data["nodes"] == ["a", "b", "c", "d"]  # c 经 find 自动登记为节点
    assert data["edges"] == [("a", "b"), ("a", "d")]  # 规范化 + 去重 + 排序
    json.dumps(data)  # 可 JSON 序列化(与 A46 互导口径)


# ---------------------------------------------------------------------------
# 空 / 单点 / 自环
# ---------------------------------------------------------------------------


def test_v7_empty_kernel() -> None:
    """空内核:components 为空 dict、查询全空,且查询零副作用。"""
    k = UnionFindKernel()
    assert k.connected("a", "b") is False  # 纯查询不登记节点
    assert k.export() == {"nodes": [], "edges": []}
    assert k.components() == {}
    assert k.recompute_count == 1  # 空也算一次物化
    assert k.related("anyone", 1) == set()


def test_v7_single_node() -> None:
    """单点内核:自成一分量、自连通、无邻域、无边。"""
    k = UnionFindKernel()
    assert k.add("only") is True
    assert k.connected("only", "only") is True
    assert k.related("only", 3) == set()
    assert k.components() == {"only": {"only"}}
    data = k.export()
    assert data["nodes"] == ["only"]
    assert data["edges"] == []


def test_v7_self_loop_union_ignored_as_edge() -> None:
    """union(x, x) 返回 False:节点经 find 自动登记,但自环不产生边 / 邻域。"""
    k = UnionFindKernel()
    assert k.union("x", "x") is False
    assert k.export() == {"nodes": ["x"], "edges": []}
    assert k.related("x", 1) == set()
    assert k.invalidate_count == 0  # 无结构变化:不失效(缓存本就未物化)


# ---------------------------------------------------------------------------
# 与 A46 EvidenceGraph 共存(仅数据互导,不改 A46)
# ---------------------------------------------------------------------------


def test_v7_interop_with_evidence_graph(tmp_path: Any) -> None:
    """三种边种类(shared_image / shared_template / redirect)互导后团伙归并与
    邻域语义与 A46 一致;kernel 导出边与 A46 导出边(规范化后)完全相等。"""
    from netsentinel.intel.graph import EvidenceGraph  # 惰性导入,只读 A46

    with EvidenceGraph(str(tmp_path / "interop.db")) as g:
        for url in (A, B, C, D):
            g.add_site(url)
        g.add_image(H1, A)
        g.add_image(H1, B)  # shared_image: A-B
        g.add_template(T1, C)
        g.add_template(T1, D)  # shared_template: C-D
        g.add_redirect(B, C)  # redirect: B-C → 两个团伙连成一片
        assert g.link_shared_images() == 1
        assert g.link_templates() == 1
        payload = g.export_json()
        g_related_1 = {r["site"] for r in g.related_sites(A, depth=1)}
        g_related_2 = {r["site"] for r in g.related_sites(A, depth=2)}

    kernel = UnionFindKernel()
    merged = kernel.ingest_edges(
        (str(e["src"]), str(e["dst"])) for e in payload["edges"]
    )
    assert merged == 3  # 三条边各完成一次真实合并

    comps = kernel.components()
    assert len(comps) == 1  # redirect 把 {A,B} ∪ {C,D} 并为一个团伙
    everyone = set().union(*comps.values())
    assert everyone == {f"site:{u}" for u in (A, B, C, D)}

    # 邻域语义与 A46 related_sites 一致(一跳 B;两跳 B、C)
    assert kernel.related(f"site:{A}", depth=1) == {f"site:{B}"}
    assert g_related_1 == {B}
    assert kernel.related(f"site:{A}", depth=2) == {f"site:{B}", f"site:{C}"}
    assert g_related_2 == {B, C}

    # 回向互导:kernel 导出边与 A46 导出边(端点规范化后)完全一致
    normalized = {tuple(sorted(e)) for e in kernel.export()["edges"]}
    original = {
        tuple(sorted((str(e["src"]), str(e["dst"])))) for e in payload["edges"]
    }
    assert normalized == original


# ---------------------------------------------------------------------------
# 红线 31 基准:操作计数断言(禁墙钟)
# ---------------------------------------------------------------------------


def test_v7_bench_10k_unions_components_o1_no_full_recompute() -> None:
    """10^4 随机 union 后:components O(1) 查询零重算(全程增量 ≤ 2)、
    冗余 union 零失效;合并数 = 节点数 − 分量数(不变式)。"""
    rng = random.Random(7)
    n = 10_000
    k = UnionFindKernel()
    assert k.recompute_count == 0

    merged = k.ingest_edges(
        (f"s{i}", f"s{rng.randrange(n)}") for i in range(n)
    )
    assert k.recompute_count == 0  # 灌入期零重算(从不物化缓存)
    assert k.invalidate_count == 0  # 缓存从未物化:零失效
    assert merged > n // 2  # 随机边确有大量真实合并

    comps = k.components()  # 首次查询:恰好一次重算
    assert k.recompute_count == 1
    assert merged == n - len(comps)  # 合并数 = 节点数 − 分量数

    for _ in range(100):  # O(1) 查询 ×100:同一缓存对象、零重算
        assert k.components() is comps
    assert k.recompute_count == 1  # 全程重算增量 1 ≤ 2(红线 31:无全量重算)

    # 正确性抽查(确定性:取自 comps 本身)
    members = sorted(max(comps.values(), key=len))
    assert len(members) >= 2
    assert k.connected(members[0], members[-1]) is True
    roots = list(comps)
    if len(roots) >= 2:  # 不同分量成员必不连通
        a = next(iter(comps[roots[0]]))
        b = next(iter(comps[roots[1]]))
        assert k.connected(a, b) is False

    # 查询后再灌冗余边:仍不失效、不重算
    assert k.union(members[0], members[-1]) is False
    assert k.invalidate_count == 0
    assert k.components() is comps
    assert k.recompute_count == 1


def test_v7_bench_connected_is_o1_after_compression() -> None:
    """全量压缩后 10^4 次 connected:每次至多 2 次父指针跳转(操作计数
    证明 O(1) 查询,非墙钟)。"""
    rng = random.Random(31)
    n = 10_000
    k = UnionFindKernel()
    k.ingest_edges((f"s{i}", f"s{rng.randrange(n)}") for i in range(n))
    k.components()  # 物化分量,顺带全量路径压缩
    queries = [
        (f"s{rng.randrange(n)}", f"s{rng.randrange(n)}") for _ in range(n)
    ]

    start = k.find_hops
    for a, b in queries:
        k.connected(a, b)
    hops = k.find_hops - start
    assert hops <= 2 * n  # 每次 connected ≤ 2 跳(两次 find 各 ≤ 1)
    assert hops / n <= 2.0


def test_v7_kernel_selfcheck_reports_generational_gap() -> None:
    """kernel_selfcheck:旧四键结构不变、value ≪ baseline(代差)、两次运行完全一致;
    A194 起另附 ``louvain`` 附加键(操作计数,主键口径不变)。"""
    first = kernel_selfcheck()
    assert {"name", "metric", "value", "baseline"} <= set(first)
    assert first["name"] == "graph_kernel.union_find"
    assert 0 < first["value"] < first["baseline"] // 100  # 跳转数远小于全表扫描
    louvain = first["louvain"]  # A194 附加键(A138 总控收入 extra 留档)
    assert louvain["nodes"] == 18 and louvain["communities"] == 4
    assert louvain["moves"] >= 1 and louvain["gain_evals"] >= 1  # 操作计数在席
    assert louvain["modularity"] > louvain["singleton_modularity"]  # 划分更优
    assert kernel_selfcheck() == first  # 确定性:离线可复现


# ---------------------------------------------------------------------------
# A194 加权社区检测(Louvain):单层局部移动 + 多层聚合(纯函数,零 IO)
# ---------------------------------------------------------------------------


def _two_clique_graph() -> dict[str, dict[str, float]]:
    """经典双团结构:两个 8-团(内边 1.0)+ 2 条 0.2 弱桥 + 2 个孤立点。"""
    adjacency: dict[str, dict[str, float]] = {}
    clique_a = [f"s{i}" for i in range(8)]
    clique_b = [f"t{i}" for i in range(8)]
    for members in (clique_a, clique_b):
        for i in range(len(members)):
            adjacency.setdefault(members[i], {})
            for j in range(i + 1, len(members)):
                adjacency[members[i]][members[j]] = 1.0  # 单方向给出即可
    adjacency["s7"]["t0"] = 0.2  # 团间弱桥:不足以并团
    adjacency["s6"]["t1"] = 0.2
    adjacency.setdefault("u0", {})
    adjacency.setdefault("u1", {})
    return adjacency


def _singleton_partition(adjacency: dict[str, dict[str, float]]) -> dict[str, int]:
    return {node: idx for idx, node in enumerate(sorted(adjacency))}


def test_v9_louvain_pass_classic_two_cliques() -> None:
    """双团经典结构:单层局部移动即正确切出 团A / 团B / 两孤立点 四社区。"""
    adjacency = _two_clique_graph()
    partition = louvain_pass(adjacency)
    assert len(set(partition.values())) == 4
    assert len({partition[f"s{i}"] for i in range(8)}) == 1  # 团 A 归一
    assert len({partition[f"t{i}"] for i in range(8)}) == 1  # 团 B 归一
    assert partition["s0"] != partition["t0"]  # 弱桥不得误并两团
    assert partition["u0"] != partition["u1"]  # 孤立点互不相干
    # 输出为 0..k-1 紧凑标签且覆盖全部节点(划分不变式)
    assert sorted(set(partition.values())) == [0, 1, 2, 3]
    assert set(partition) == set(adjacency)


def test_v9_louvain_empty_single_and_edgeless() -> None:
    """空图 → 空 dict;单点 → {节点: 0};无边多点 → 各自成区。"""
    assert louvain_pass({}) == {}
    assert louvain_communities({}) == {}
    assert louvain_pass({"a": {}}) == {"a": 0}
    assert louvain_communities({"a": {}}) == {"a": 0}
    edgeless = {"a": {}, "b": {}, "c": {}}
    assert louvain_pass(edgeless) == {"a": 0, "b": 1, "c": 2}
    assert louvain_communities(edgeless) == {"a": 0, "b": 1, "c": 2}


def test_v9_louvain_fully_connected_graphs() -> None:
    """全连通图:等权 K5 的模块度最优解即全单点(已知偏置,正确行为);
    权重分层的全连通图(组内强 / 组间弱)正确切出两社区。"""
    complete = {c: {} for c in "abcde"}
    for i, a in enumerate("abcde"):
        for b in "abcde"[i + 1:]:
            complete[a][b] = 1.0
    partition = louvain_communities(complete)
    assert len(set(partition.values())) == 5  # 等权完全图:单点即最优(Q 不降)
    q_singleton = louvain_modularity(complete, _singleton_partition(complete))
    assert louvain_modularity(complete, partition) == pytest.approx(q_singleton)

    # 全连通但权重分层:8 点完全图,组内边权 5.0、组间边权 1.0
    weighted: dict[str, dict[str, float]] = {}
    names = [f"a{i}" for i in range(4)] + [f"b{i}" for i in range(4)]
    for i, x in enumerate(names):
        for y in names[i + 1:]:
            weighted.setdefault(x, {})[y] = 5.0 if x[0] == y[0] else 1.0
    part_w = louvain_communities(weighted)
    assert {part_w[f"a{i}"] for i in range(4)} == {part_w[f"a0"]}
    assert {part_w[f"b{i}"] for i in range(4)} == {part_w[f"b0"]}
    assert part_w["a0"] != part_w["b0"]
    assert louvain_modularity(weighted, part_w) > louvain_modularity(
        weighted, _singleton_partition(weighted)
    )


def test_v9_louvain_modularity_monotone_nondecreasing() -> None:
    """模块度单调不减:全单点 → 单层 pass → 多层 communities 逐级不降
    (每次被采纳的移动净增量恒正);30 个 seeded 随机加权图全部成立。"""
    for seed in range(30):
        rng = random.Random(seed)
        adjacency: dict[str, dict[str, float]] = {}
        n = 30
        for i in range(n):
            for j in range(i + 1, n):
                if rng.random() < 0.15:
                    adjacency.setdefault(f"n{i}", {})[f"n{j}"] = round(rng.random(), 3)
        for i in range(n):
            adjacency.setdefault(f"n{i}", {})
        q_singleton = louvain_modularity(adjacency, _singleton_partition(adjacency))
        one_pass = louvain_pass(adjacency)
        multi = louvain_communities(adjacency)
        q_pass = louvain_modularity(adjacency, one_pass)
        q_multi = louvain_modularity(adjacency, multi)
        assert q_pass >= q_singleton - 1e-12, f"seed={seed}:单层 pass 不得降低模块度"
        assert q_multi >= q_pass - 1e-12, f"seed={seed}:多层聚合不得降低模块度"


def test_v9_louvain_deterministic_same_input_same_output() -> None:
    """确定性:同输入(含打乱插入顺序 / 双向重复给出)同输出;调用两次相等。"""
    adjacency = _two_clique_graph()
    first = louvain_communities(adjacency)
    assert louvain_communities(adjacency) == first
    shuffled = {k: adjacency[k] for k in sorted(adjacency, reverse=True)}
    assert louvain_communities(shuffled) == first
    # 双向都给出(权重求和口径)与单方向给出切分一致
    mirrored: dict[str, dict[str, float]] = {k: {} for k in adjacency}
    for a, nbrs in adjacency.items():
        for b, w in nbrs.items():
            mirrored[a][b] = mirrored[b][a] = w
    assert louvain_communities(mirrored) == first


def test_v9_louvain_params_and_stats() -> None:
    """参数与操作计数:resolution 越大社区越细(K5 合并证据)、stats 四键、
    非法参数中文 ValueError。"""
    complete = {c: {} for c in "abcde"}
    for i, a in enumerate("abcde"):
        for b in "abcde"[i + 1:]:
            complete[a][b] = 1.0
    fine = louvain_communities(complete, resolution=0.4)  # 低分辨率 → 归一
    coarse = louvain_communities(complete, resolution=1.0)  # 默认 → 全单点
    assert len(set(fine.values())) < len(set(coarse.values()))

    stats: dict[str, int] = {}
    louvain_communities(_two_clique_graph(), stats=stats)
    assert set(stats) == {"sweeps", "moves", "gain_evals", "levels"}
    assert stats["sweeps"] >= 1 and stats["levels"] >= 1
    assert stats["gain_evals"] >= stats["moves"]  # 每次移动至少评估一个候选

    with pytest.raises(ValueError):
        louvain_pass(complete, resolution=0.0)
    with pytest.raises(ValueError):
        louvain_pass(complete, resolution=-1.0)
    with pytest.raises(ValueError):
        louvain_pass(complete, max_sweeps=0)
    with pytest.raises(ValueError):
        louvain_pass(complete, threshold=-1e-9)
    with pytest.raises(ValueError):
        louvain_communities(complete, max_levels=0)
    with pytest.raises(ValueError):
        louvain_pass({"a": {"b": -0.5}})  # 负权拒绝(Jaccard 类权重恒非负)
    with pytest.raises(ValueError):
        louvain_pass("not-a-dict")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        louvain_modularity(complete, {}, resolution=0.0)  # type: ignore[arg-type]


def test_v9_louvain_multi_level_aggregation() -> None:
    """多层聚合:环形排布的 4 个三元团(相邻团间一条弱边)——单层即并团,
    多层包装不拆散、不误并,结果稳定且模块度不降。"""
    adjacency: dict[str, dict[str, float]] = {}
    cliques = [[f"c{k}_{i}" for i in range(3)] for k in range(4)]
    for members in cliques:
        for i in range(3):
            adjacency.setdefault(members[i], {})
            for j in range(i + 1, 3):
                adjacency[members[i]][members[j]] = 1.0
    for k in range(4):  # 环形弱桥:c0-c1-c2-c3-c0
        a = cliques[k][0]
        b = cliques[(k + 1) % 4][0]
        adjacency.setdefault(a, {})[b] = 0.15
    partition = louvain_communities(adjacency)
    assert len(set(partition.values())) == 4  # 四个团各自成社区
    for members in cliques:
        assert len({partition[m] for m in members}) == 1
    assert louvain_modularity(adjacency, partition) > louvain_modularity(
        adjacency, _singleton_partition(adjacency)
    )


def test_v9_louvain_self_loop_and_zero_weights() -> None:
    """自环(聚合层产生)参与但不改变划分;零权边等价于无边。"""
    with_loop = {"a": {"a": 3.0}, "b": {}, "c": {}}
    assert louvain_pass(with_loop) == {"a": 0, "b": 1, "c": 2}
    zero_edge = {"a": {"b": 0.0}, "b": {}}
    assert louvain_pass(zero_edge) == {"a": 0, "b": 1}
    # 自环不产生邻居关系:带自环节点不与任何点成团
    assert louvain_modularity(with_loop, {"a": 0, "b": 1, "c": 2}) == pytest.approx(
        louvain_modularity(with_loop, {"a": 0, "b": 1, "c": 2}, resolution=1.0)
    )
