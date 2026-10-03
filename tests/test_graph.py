"""A46 netsentinel.intel.graph 站点关联图谱测试。

纯离线、仅用 tmp_path 落库、零网络行为(红线 12:图谱仅存本地)。覆盖:
表结构与 kind 约束、add_site/add_image/add_template 幂等与 meta.sites 维护、
三站点两共享图的关联与权重(共享数 / 并集数)、重复 link 幂等(第二次 0 新边)、
重跑刷新权重与陈旧边清理、related_sites 的 BFS 深度 / via 聚合 / 最大权重 /
排序、template 关联、redirect 边(A47 写入通道)、A204 公开通用写口 add_edge
(kind 白名单中文 ValueError / 自环拒绝 / 幂等刷新权重保 created_at / 查询
即时可见=无缓存可失效)、A244 mirror_near 独立边种类(白名单 / 幂等 /
旧库 kind CHECK 整表重建迁移 / BFS 默认含新种类)、export_json 结构、stats、
损坏重建(含伴生文件)、跨实例持久化、多线程并发读写。
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Any

import pytest

from netsentinel.intel.graph import EvidenceGraph

A = "https://a.example.com/"
B = "https://b.example.com/"
C = "https://c.example.com/"

H1 = "1" * 64  # 图片 sha256(测试值,形状即可)
H2 = "2" * 64
H3 = "3" * 64
T1 = "f" * 16  # 模板 simhash


def _graph(tmp_path: Any, name: str = "graph.db") -> EvidenceGraph:
    return EvidenceGraph(str(tmp_path / name))


def _edges_of_kind(g: EvidenceGraph, kind: str) -> list[tuple[str, str, float]]:
    """按种类取出边,端点转为站点 URL 并排序(规范化 A-B / B-A)。"""
    out = []
    for e in g.export_json()["edges"]:
        if e["kind"] == kind:
            pair = tuple(sorted((e["src"], e["dst"])))
            out.append((pair[0], pair[1], float(e["weight"])))
    return out


def _sites_pair(*urls: str) -> tuple[str, str]:
    pair = tuple(sorted(f"site:{u}" for u in urls))
    return pair[0], pair[1]


# ---------------------------------------------------------------------------
# 表结构与 kind 约束
# ---------------------------------------------------------------------------


def test_schema_tables_and_columns(tmp_path: Any) -> None:
    """nodes/edges 两表存在且列名与契约一致(id/kind/meta;src/dst/kind/weight/created_at)。"""
    db_path = str(tmp_path / "graph.db")
    with _graph(tmp_path) as g:
        g.add_site(A)
    conn = sqlite3.connect(db_path)
    try:
        node_cols = {row[1] for row in conn.execute("PRAGMA table_info(nodes)")}
        edge_cols = {row[1] for row in conn.execute("PRAGMA table_info(edges)")}
        assert {"id", "kind", "meta"} <= node_cols
        assert {"src", "dst", "kind", "weight", "created_at"} <= edge_cols
    finally:
        conn.close()


def test_kind_check_constraints(tmp_path: Any) -> None:
    """非法 kind(节点与边)被 CHECK 约束拒绝,防脏数据入库。"""
    db_path = str(tmp_path / "graph.db")
    with _graph(tmp_path) as g:
        g.add_site(A)
    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO nodes (id, kind) VALUES ('x', 'bogus')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO edges (src, dst, kind) VALUES ('a', 'b', 'bogus')")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 写口:幂等与 meta 维护
# ---------------------------------------------------------------------------


def test_add_site_idempotent(tmp_path: Any) -> None:
    """同一站点 add 多次仍只有一个 site 节点。"""
    with _graph(tmp_path) as g:
        for _ in range(3):
            g.add_site(A)
        assert g.stats()["sites"] == 1
        assert g.stats()["nodes"] == 1


def test_add_image_creates_nodes_and_meta(tmp_path: Any) -> None:
    """add_image 自动补建 image 节点(与 site 节点),meta.sites 记录站点。"""
    with _graph(tmp_path) as g:
        g.add_image(H1, A)
        g.add_image(H1, A)  # 幂等
        st = g.stats()
        assert st["sites"] == 1
        assert st["images"] == 1
        exported = {n["id"]: n for n in g.export_json()["nodes"]}
        assert exported[f"image:{H1}"]["kind"] == "image"
        assert exported[f"image:{H1}"]["meta"] == {"sites": [A]}
        g.add_image(H1, B)  # 第二个站点出现 → sites 并入
        assert exported is not None
        assert {n["id"]: n for n in g.export_json()["nodes"]}[f"image:{H1}"]["meta"] == {
            "sites": [A, B]
        }


def test_add_template_idempotent_and_meta(tmp_path: Any) -> None:
    """add_template 与 add_image 同语义:simhash 节点 + meta.sites,幂等。"""
    with _graph(tmp_path) as g:
        g.add_template(T1, A)
        g.add_template(T1, A)
        g.add_template(T1, B)
        st = g.stats()
        assert st["templates"] == 1
        assert st["sites"] == 2
        node = {n["id"]: n for n in g.export_json()["nodes"]}[f"template:{T1}"]
        assert node["kind"] == "template"
        assert node["meta"] == {"sites": [A, B]}


# ---------------------------------------------------------------------------
# 核心场景:三站点两共享图
# ---------------------------------------------------------------------------


def _seed_three_sites(g: EvidenceGraph) -> None:
    """A、B 共享 h1;B、C 共享 h2(A={h1}, B={h1,h2}, C={h2})。"""
    for url in (A, B, C):
        g.add_site(url)
    g.add_image(H1, A)
    g.add_image(H1, B)
    g.add_image(H2, B)
    g.add_image(H2, C)


def test_link_shared_images_basic_relations(tmp_path: Any) -> None:
    """A-B、B-C 建边,A-C 无边;返回新增边数 2。"""
    with _graph(tmp_path) as g:
        _seed_three_sites(g)
        assert g.link_shared_images() == 2
        edges = _edges_of_kind(g, "shared_image")
        pairs = {(s, d) for s, d, _ in edges}
        assert _sites_pair(A, B) in pairs
        assert _sites_pair(B, C) in pairs
        assert _sites_pair(A, C) not in pairs
        assert len(edges) == 2


def test_link_shared_images_weight_matches_shared_count(tmp_path: Any) -> None:
    """权重 = 共享图数 / 该站点对图并集数:A-B 与 B-C 均为 1/2。"""
    with _graph(tmp_path) as g:
        _seed_three_sites(g)
        g.link_shared_images()
        weights = {
            (s, d): w for s, d, w in _edges_of_kind(g, "shared_image")
        }
        assert weights[_sites_pair(A, B)] == pytest.approx(1 / 2)
        assert weights[_sites_pair(B, C)] == pytest.approx(1 / 2)


def test_link_weight_ratio_with_more_sharing(tmp_path: Any) -> None:
    """共享 2 / 并集 3 → 权重 2/3;全部图片都共享 → 权重 1.0。"""
    with _graph(tmp_path) as g:
        g.add_site(A)
        g.add_site(B)
        g.add_image(H1, A)
        g.add_image(H1, B)
        g.add_image(H3, A)
        g.add_image(H3, B)
        g.add_image(H2, B)  # B 独有 → 并集 3
        g.link_shared_images()
        weights = {(s, d): w for s, d, w in _edges_of_kind(g, "shared_image")}
        assert weights[_sites_pair(A, B)] == pytest.approx(2 / 3)

    with _graph(tmp_path, "graph2.db") as g2:  # 两站仅共享同一张图 → 1.0
        g2.add_site(A)
        g2.add_site(B)
        g2.add_image(H1, A)
        g2.add_image(H1, B)
        g2.link_shared_images()
        weights2 = {(s, d): w for s, d, w in _edges_of_kind(g2, "shared_image")}
        assert weights2[_sites_pair(A, B)] == pytest.approx(1.0)


def test_link_rerun_idempotent_returns_zero(tmp_path: Any) -> None:
    """数据未变时重复 link:返回 0 新边,边数与权重保持不变。"""
    with _graph(tmp_path) as g:
        _seed_three_sites(g)
        assert g.link_shared_images() == 2
        before = _edges_of_kind(g, "shared_image")
        assert g.link_shared_images() == 0  # 幂等:第二次无新增
        assert _edges_of_kind(g, "shared_image") == before
        assert g.link_shared_images() == 0


def test_link_refreshes_weight_on_new_data(tmp_path: Any) -> None:
    """重跑刷新:新图入账后权重从 1.0 降到 1/3(该对已存在 → 返回 0 新边)。"""
    with _graph(tmp_path) as g:
        g.add_site(A)
        g.add_site(B)
        g.add_image(H1, A)
        g.add_image(H1, B)
        g.link_shared_images()
        weights = {(s, d): w for s, d, w in _edges_of_kind(g, "shared_image")}
        assert weights[_sites_pair(A, B)] == pytest.approx(1.0)

        g.add_image(H3, A)  # 双方各添一张独有图 → 共享 1 / 并集 3
        g.add_image(H2, B)
        assert g.link_shared_images() == 0  # 边已存在,仅刷新权重
        weights = {(s, d): w for s, d, w in _edges_of_kind(g, "shared_image")}
        assert weights[_sites_pair(A, B)] == pytest.approx(1 / 3)


def test_link_drops_stale_edges(tmp_path: Any) -> None:
    """刷新语义:素材不再被两站共享(meta 被改)→ 陈旧边被清理。"""
    db_path = str(tmp_path / "graph.db")
    with _graph(tmp_path) as g:
        _seed_three_sites(g)
        g.link_shared_images()
        assert g.stats()["edges"] == 2

    conn = sqlite3.connect(db_path)  # 直改 meta:模拟 h2 不再属于 C
    conn.execute(
        "UPDATE nodes SET meta = ? WHERE id = ?",
        ('{"sites": ["%s"]}' % B, f"image:{H2}"),
    )
    conn.commit()
    conn.close()

    with EvidenceGraph(db_path) as g2:
        assert g2.link_shared_images() == 0
        edges = _edges_of_kind(g2, "shared_image")
        pairs = {(s, d) for s, d, _ in edges}
        assert _sites_pair(B, C) not in pairs  # 陈旧边已删
        assert _sites_pair(A, B) in pairs
        assert g2.stats()["edges"] == 1


def test_link_with_no_shared_images_returns_zero(tmp_path: Any) -> None:
    """无任何共享素材(或空库)时 link 返回 0、不建边。"""
    with _graph(tmp_path) as g:
        assert g.link_shared_images() == 0
        g.add_site(A)
        g.add_image(H1, A)
        assert g.link_shared_images() == 0
        assert g.stats()["edges"] == 0


# ---------------------------------------------------------------------------
# related_sites:深度 / via 聚合 / 最大权重
# ---------------------------------------------------------------------------


def test_related_sites_depth_one_and_two(tmp_path: Any) -> None:
    """深度 1:A 只见 B 不见 C;深度 2:C 经 B 出现(A-C 无直接边)。"""
    with _graph(tmp_path) as g:
        _seed_three_sites(g)
        g.link_shared_images()

        near = {r["site"]: r for r in g.related_sites(A, depth=1)}
        assert set(near) == {B}
        far = {r["site"]: r for r in g.related_sites(A, depth=2)}
        assert set(far) == {B, C}

        c_entry = far[C]
        assert c_entry["via"] == ["shared_image"]
        assert c_entry["weight"] == pytest.approx(0.5)  # 路径上最强一跳
        assert near[B]["weight"] == pytest.approx(0.5)


def test_related_sites_from_middle_sees_both_and_sorts(tmp_path: Any) -> None:
    """B 一跳直达 A 与 C;结果按权重降序、同权重按站点 URL 升序,且不含自身。"""
    with _graph(tmp_path) as g:
        _seed_three_sites(g)
        g.link_shared_images()
        results = g.related_sites(B, depth=1)
        assert [r["site"] for r in results] == [A, C]
        assert all(r["site"] != B for r in results)
        assert all(r["via"] == ["shared_image"] for r in results)


def test_related_sites_no_edges_before_link(tmp_path: Any) -> None:
    """link 之前不产生关联(边由 link 显式折叠,查询不隐式建边)。"""
    with _graph(tmp_path) as g:
        _seed_three_sites(g)
        assert g.related_sites(A, depth=2) == []


def test_related_sites_aggregates_via_and_max_weight(tmp_path: Any) -> None:
    """同一对多种边:via 聚合去重排序,weight 取各边最大值。"""
    with _graph(tmp_path) as g:
        g.add_site(A)
        g.add_site(B)
        g.add_image(H1, A)
        g.add_image(H1, B)
        g.add_image(H2, B)  # A-B shared_image 权重 = 1/2
        g.add_template(T1, A)
        g.add_template(T1, B)  # A-B shared_template 权重 = 1.0
        g.add_redirect(A, B)  # A-B redirect 权重 = 1.0
        g.link_shared_images()
        g.link_templates()

        results = g.related_sites(A, depth=1)
        assert len(results) == 1
        entry = results[0]
        assert entry["site"] == B
        assert entry["via"] == ["redirect", "shared_image", "shared_template"]
        assert entry["weight"] == pytest.approx(1.0)  # 取最大


def test_related_sites_edge_cases(tmp_path: Any) -> None:
    """depth<=0 返回空;未收录站点返回空;无邻边的站点返回空。"""
    with _graph(tmp_path) as g:
        _seed_three_sites(g)
        g.link_shared_images()
        assert g.related_sites(A, depth=0) == []
        assert g.related_sites(A, depth=-3) == []
        assert g.related_sites("https://never.example.com/") == []
        g.add_site("https://lonely.example.com/")
        assert g.related_sites("https://lonely.example.com/") == []


def test_related_sites_depth_two_takes_strongest_hop(tmp_path: Any) -> None:
    """多跳权重 = 抵达路径上的最大边权:A-h(0.5)->B-t(1.0)->C,C 权重 1.0。"""
    with _graph(tmp_path) as g:
        g.add_site(A)
        g.add_site(B)
        g.add_site(C)
        g.add_image(H1, A)
        g.add_image(H1, B)
        g.add_image(H2, B)  # A-B shared_image = 1/2
        g.add_template(T1, B)
        g.add_template(T1, C)  # B-C shared_template = 1.0
        g.link_shared_images()
        g.link_templates()

        one_hop = {r["site"]: r for r in g.related_sites(A, depth=1)}
        assert set(one_hop) == {B}
        two_hop = {r["site"]: r for r in g.related_sites(A, depth=2)}
        assert set(two_hop) == {B, C}
        assert two_hop[C]["via"] == ["shared_template"]
        assert two_hop[C]["weight"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# template 关联与 redirect 边
# ---------------------------------------------------------------------------


def test_link_templates_relations(tmp_path: Any) -> None:
    """相同 simhash 的模板把两站连起来;不同 simhash 不建边。"""
    with _graph(tmp_path) as g:
        g.add_site(A)
        g.add_site(B)
        g.add_site(C)
        g.add_template(T1, A)
        g.add_template(T1, B)
        g.add_template("e" * 16, A)
        g.add_template("e" * 16, C)  # 另一模板只连 A-C
        assert g.link_templates() == 2
        pairs = {(s, d) for s, d, _ in _edges_of_kind(g, "shared_template")}
        assert _sites_pair(A, B) in pairs
        assert _sites_pair(A, C) in pairs
        assert _sites_pair(B, C) not in pairs
        assert g.link_templates() == 0  # 幂等
        # 模板边不影响 shared_image 统计
        assert g.stats()["edges_by_kind"] == {"shared_template": 2}


def test_link_templates_weight_ratio(tmp_path: Any) -> None:
    """模板权重同样按 共享数 / 并集数:A={t1,t2}, B={t1} → 1/2。"""
    with _graph(tmp_path) as g:
        g.add_site(A)
        g.add_site(B)
        g.add_template(T1, A)
        g.add_template(T1, B)
        g.add_template("e" * 16, A)
        g.link_templates()
        weights = {(s, d): w for s, d, w in _edges_of_kind(g, "shared_template")}
        assert weights[_sites_pair(A, B)] == pytest.approx(0.5)


def test_add_redirect_creates_edge_and_nodes(tmp_path: Any) -> None:
    """add_redirect 自动补建站点节点并建 redirect 边;自环被忽略;幂等。"""
    with _graph(tmp_path) as g:
        g.add_redirect(A, B)
        g.add_redirect(A, B)  # 幂等
        g.add_redirect(A, A)  # 自环忽略
        st = g.stats()
        assert st["sites"] == 2
        assert st["edges"] == 1
        assert st["edges_by_kind"] == {"redirect": 1}
        edge = g.export_json()["edges"][0]
        assert edge["kind"] == "redirect"
        assert edge["weight"] == pytest.approx(1.0)
        assert edge["created_at"]  # 记录首建时间
        results = g.related_sites(B, depth=1)
        assert [r["site"] for r in results] == [A]
        assert results[0]["via"] == ["redirect"]


# ---------------------------------------------------------------------------
# add_edge:A204 公开通用边写口(白名单 / 自环 / 幂等 / 查询即时可见)
# ---------------------------------------------------------------------------


def test_add_edge_whitelist_rejects_unknown_kind(tmp_path: Any) -> None:
    """kind 白名单:四种契约边种类全放行;非法种类抛中文 ValueError。"""
    with _graph(tmp_path) as g:
        for kind in ("shared_image", "phash_near", "shared_template", "redirect"):
            assert g.add_edge(A, B, kind=kind) is True
        assert g.stats()["edges_by_kind"] == {
            "phash_near": 1,
            "redirect": 1,
            "shared_image": 1,
            "shared_template": 1,
        }
        for bogus in ("friendship", "", "SHARED_IMAGE", None, 123):
            with pytest.raises(ValueError, match="边种类无效"):
                g.add_edge(A, B, kind=bogus)  # type: ignore[arg-type]
        assert g.stats()["edges"] == 4  # 拒绝的写入不落边


def test_add_edge_creates_edge_auto_nodes_and_sorts_endpoints(tmp_path: Any) -> None:
    """add_edge 自动补建站点节点;端点排序规范化(A-B 与 B-A 落同一条边)。"""
    with _graph(tmp_path) as g:
        assert g.add_edge(A, B, kind="phash_near", weight=0.75) is True
        assert g.add_edge(B, A, kind="phash_near", weight=0.75) is True  # 同边
        st = g.stats()
        assert st["sites"] == 2
        assert st["edges"] == 1
        assert st["edges_by_kind"] == {"phash_near": 1}
        edges = _edges_of_kind(g, "phash_near")
        assert edges == [(_sites_pair(A, B)[0], _sites_pair(A, B)[1], pytest.approx(0.75))]


def test_add_edge_idempotent_refreshes_weight_keeps_created_at(tmp_path: Any) -> None:
    """幂等 = 末次写入刷新权重(不累加 / 不取最大),created_at 保留首建时间。"""
    with _graph(tmp_path) as g:
        g.add_edge(A, B, kind="phash_near", weight=1.0)
        first_created = g.export_json()["edges"][0]["created_at"]

        assert g.add_edge(A, B, kind="phash_near", weight=0.4) is True  # 刷新
        edges = g.export_json()["edges"]
        assert len(edges) == 1  # 仍只有一条边
        assert edges[0]["weight"] == pytest.approx(0.4)  # 末次写入生效
        assert edges[0]["created_at"] == first_created  # 首建时间保留(UPSERT 不动)

        g.add_edge(A, B, kind="phash_near", weight=0.9)  # 再次覆盖(非取最大)
        assert g.export_json()["edges"][0]["weight"] == pytest.approx(0.9)


def test_add_edge_rejects_self_loop(tmp_path: Any) -> None:
    """自环拒绝写入:返回 False、不落边、不抛错;端点站点节点仍补建。"""
    with _graph(tmp_path) as g:
        assert g.add_edge(A, A, kind="redirect") is False
        st = g.stats()
        assert st["sites"] == 1  # 端点节点已补建(与 add_redirect 自环口径一致)
        assert st["edges"] == 0


def test_add_edge_immediately_visible_to_queries(tmp_path: Any) -> None:
    """查询口直读 SQLite、无进程内缓存:add_edge 后 related_sites / export_json
    立即可见新边与刷新后的权重(等价于"写后失效缓存"的可观察口径)。"""
    with _graph(tmp_path) as g:
        assert g.related_sites(A, depth=1) == []  # 写前无边
        g.add_edge(A, B, kind="phash_near", weight=0.5)
        results = g.related_sites(A, depth=1)  # 写后立即可见,无需任何失效操作
        assert [r["site"] for r in results] == [B]
        assert results[0]["via"] == ["phash_near"]
        assert results[0]["weight"] == pytest.approx(0.5)

        g.add_edge(A, B, kind="phash_near", weight=1.0)  # 权重刷新同样即时可见
        assert g.related_sites(A, depth=1)[0]["weight"] == pytest.approx(1.0)
        assert g.stats()["edges_by_kind"] == {"phash_near": 1}


# ---------------------------------------------------------------------------
# A244:mirror_near 独立边种类(白名单 / 幂等 / 旧库 CHECK 迁移 / BFS)
# ---------------------------------------------------------------------------


def test_add_edge_mirror_near_whitelist_and_idempotent(tmp_path: Any) -> None:
    """mirror_near 入白名单(第五种契约边):写入 / 端点排序规范化 / 幂等 =
    末次刷新权重且保 created_at / 自环拒绝——与既有四种边种类逐项同口径;
    近似名("mirror" / 大写)仍按白名单拒绝。"""
    with _graph(tmp_path) as g:
        assert g.add_edge(A, B, kind="mirror_near", weight=0.8) is True
        first_created = g.export_json()["edges"][0]["created_at"]

        assert g.add_edge(B, A, kind="mirror_near", weight=0.3) is True  # 同边
        edges = _edges_of_kind(g, "mirror_near")
        assert edges == [
            (_sites_pair(A, B)[0], _sites_pair(A, B)[1], pytest.approx(0.3))
        ]
        assert g.export_json()["edges"][0]["created_at"] == first_created  # 首建保留

        assert g.add_edge(A, A, kind="mirror_near") is False  # 自环拒绝
        assert g.stats()["edges_by_kind"] == {"mirror_near": 1}
        for bogus in ("mirror", "MIRROR_NEAR", "mirror-near"):
            with pytest.raises(ValueError, match="边种类无效"):
                g.add_edge(A, B, kind=bogus)  # type: ignore[arg-type]
        assert g.stats()["edges"] == 1  # 拒绝的写入不落边


#: A46 旧口径(四种类 CHECK)edges 表 DDL——模拟 A244 升级前的既有库。
_OLD_EDGES_SCHEMA_SQL = """
CREATE TABLE edges (
    src        TEXT NOT NULL,
    dst        TEXT NOT NULL,
    kind       TEXT NOT NULL
               CHECK (kind IN ('shared_image', 'phash_near', 'shared_template', 'redirect')),
    weight     REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (src, dst, kind)
);
CREATE INDEX idx_edges_src ON edges(src);
CREATE INDEX idx_edges_dst ON edges(dst);
"""


def test_old_db_edges_check_rebuilt_additively(tmp_path: Any) -> None:
    """旧库迁移:四种类 CHECK 的既有库重开时 kind CHECK 整表重建为含
    mirror_near 的新口径——旧边逐行原样保留(零迁移 / 值不变)、
    mirror_near 可写、脏种类仍被拒、索引随重建恢复;telemetry 记
    graph.edges_check_migrated(恰好一次,新库与重开都不记)。"""
    from netsentinel import telemetry

    db_path = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_OLD_EDGES_SCHEMA_SQL)
        conn.execute(
            "INSERT INTO edges (src, dst, kind, weight, created_at) VALUES "
            "('site:x', 'site:y', 'phash_near', 0.5, '2024-01-01T00:00:00')"
        )
        conn.commit()
        # 迁移前提自证:旧 CHECK 确实拒绝 mirror_near
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO edges (src, dst, kind) VALUES ('x', 'y', 'mirror_near')"
            )
        conn.rollback()
    finally:
        conn.close()

    telemetry.reset()
    try:
        with EvidenceGraph(db_path) as g:
            # 旧边原样保留(行 / 值 / 首建时间零迁移)
            assert g.export_json()["edges"] == [
                {
                    "src": "site:x",
                    "dst": "site:y",
                    "kind": "phash_near",
                    "weight": 0.5,
                    "created_at": "2024-01-01T00:00:00",
                }
            ]
            # 新口径可写 mirror_near
            assert g.add_edge(A, B, kind="mirror_near", weight=1.0) is True
            assert g.stats()["edges_by_kind"] == {"mirror_near": 1, "phash_near": 1}
            assert g.related_sites(A, depth=1)[0]["via"] == ["mirror_near"]
        assert telemetry.snapshot()["counters"].get("graph.edges_check_migrated") == 1

        with EvidenceGraph(db_path) as g2:  # 重开:DDL 已新口径,不再迁移
            assert g2.stats()["edges"] == 2
        assert telemetry.snapshot()["counters"].get("graph.edges_check_migrated") == 1
    finally:
        telemetry.reset()

    # 直连验证:CHECK 已含 mirror_near;脏种类仍拒;索引已随重建恢复
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO edges (src, dst, kind) VALUES ('site:p', 'site:q', 'mirror_near')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO edges (src, dst, kind) VALUES ('a', 'b', 'bogus')"
            )
        conn.rollback()
        indexes = {str(row[1]) for row in conn.execute("PRAGMA index_list(edges)")}
        assert {"idx_edges_src", "idx_edges_dst"} <= indexes
    finally:
        conn.close()


def test_fresh_db_schema_includes_mirror_near_without_migration(
    tmp_path: Any,
) -> None:
    """新库直接建新口径:sqlite_master DDL 含 mirror_near,零迁移计数;
    schema CHECK 对全部五种边种类放行。"""
    from netsentinel import telemetry

    db_path = str(tmp_path / "graph.db")
    telemetry.reset()
    try:
        with _graph(tmp_path) as g:
            for kind in (
                "shared_image",
                "phash_near",
                "shared_template",
                "redirect",
                "mirror_near",
            ):
                assert g.add_edge(A, B, kind=kind) is True
            assert g.stats()["edges_by_kind"] == {
                "mirror_near": 1,
                "phash_near": 1,
                "redirect": 1,
                "shared_image": 1,
                "shared_template": 1,
            }
        conn = sqlite3.connect(db_path)
        try:
            (ddl,) = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='edges'"
            ).fetchone()
        finally:
            conn.close()
        assert "mirror_near" in str(ddl)
        assert telemetry.snapshot()["counters"].get("graph.edges_check_migrated") is None
    finally:
        telemetry.reset()


def test_related_sites_traverses_mirror_near_by_default(tmp_path: Any) -> None:
    """related_sites 默认含 mirror_near 边(BFS 对边种类无过滤):via 聚合
    新种类、同对多边权重取最大、多跳取路径最强一跳;镜像边权重即其
    近重复占比,降权 / 并团把关在消费侧 resolve_gangs(图口不降权)。"""
    with _graph(tmp_path) as g:
        g.add_edge(A, B, kind="mirror_near", weight=0.4)
        g.add_edge(A, B, kind="phash_near", weight=0.6)
        g.add_edge(B, C, kind="mirror_near", weight=0.9)

        by_site = {r["site"]: r for r in g.related_sites(A, depth=2)}
        assert set(by_site) == {B, C}
        assert by_site[B]["via"] == ["mirror_near", "phash_near"]  # 聚合去重排序
        assert by_site[B]["weight"] == pytest.approx(0.6)  # 同对取最大
        assert by_site[C]["via"] == ["mirror_near"]
        assert by_site[C]["weight"] == pytest.approx(0.9)  # 路径上最强一跳
        # stats / export_json 对新种类按 kind 分组计数,无需特判
        assert g.stats()["edges_by_kind"] == {
            "mirror_near": 2,
            "phash_near": 1,
        }


# ---------------------------------------------------------------------------
# export_json 与 stats
# ---------------------------------------------------------------------------


def test_export_json_structure(tmp_path: Any) -> None:
    """export_json 顶层结构与节点 / 边字段齐备,meta 已解析为 dict。"""
    with _graph(tmp_path) as g:
        _seed_three_sites(g)
        g.add_template(T1, A)
        g.add_template(T1, B)
        g.add_redirect(B, C)
        g.link_shared_images()
        g.link_templates()

        data = g.export_json()
        assert set(data) == {"nodes", "edges"}
        nodes = {n["id"]: n for n in data["nodes"]}
        assert all(set(n) == {"id", "kind", "meta"} for n in data["nodes"])
        assert nodes[f"site:{A}"]["kind"] == "site"
        assert nodes[f"site:{A}"]["meta"] == {}
        assert nodes[f"image:{H1}"]["meta"] == {"sites": [A, B]}
        assert nodes[f"template:{T1}"]["meta"] == {"sites": [A, B]}

        # (A,B)(B,C) shared_image + (A,B) shared_template + (B,C) redirect
        assert len(data["edges"]) == 4
        for e in data["edges"]:
            assert set(e) == {"src", "dst", "kind", "weight", "created_at"}
            assert e["kind"] in {"shared_image", "shared_template", "redirect"}
            assert isinstance(e["weight"], float)
        kinds = sorted(e["kind"] for e in data["edges"])
        assert kinds == ["redirect", "shared_image", "shared_image", "shared_template"]


def test_stats_counts(tmp_path: Any) -> None:
    """stats 报告各类节点计数、节点总数、边总数与按边种类计数。"""
    with _graph(tmp_path) as g:
        st = g.stats()
        assert st["sites"] == 0
        assert st["images"] == 0
        assert st["templates"] == 0
        assert st["nodes"] == 0
        assert st["edges"] == 0
        assert st["edges_by_kind"] == {}

        _seed_three_sites(g)  # 3 site + 2 image 节点
        g.add_template(T1, A)
        g.add_template(T1, B)
        g.link_shared_images()
        g.link_templates()

        st = g.stats()
        assert st["sites"] == 3
        assert st["images"] == 2
        assert st["templates"] == 1
        assert st["nodes"] == 6
        assert st["edges"] == 3
        assert st["edges_by_kind"] == {"shared_image": 2, "shared_template": 1}


# ---------------------------------------------------------------------------
# 持久化 / 损坏重建 / 并发
# ---------------------------------------------------------------------------


def test_persists_across_instances(tmp_path: Any) -> None:
    """图谱落盘可跨实例使用:重开后 stats 与 related_sites 一致。"""
    db_path = str(tmp_path / "graph.db")
    with EvidenceGraph(db_path) as g1:
        _seed_three_sites(g1)
        g1.link_shared_images()
        before = g1.related_sites(A, depth=2)
    with EvidenceGraph(db_path) as g2:
        assert g2.stats()["nodes"] == 5
        assert g2.related_sites(A, depth=2) == before
        assert g2.link_shared_images() == 0  # 已建边,幂等


def test_corrupt_db_is_rebuilt(tmp_path: Any) -> None:
    """库文件损坏:重建为空库,之后读写一切正常。"""
    db_path = tmp_path / "graph.db"
    with _graph(tmp_path) as g:
        _seed_three_sites(g)
        g.link_shared_images()
        assert g.stats()["edges"] == 2
    db_path.write_bytes(b"this is definitely not a sqlite database !!!")

    with EvidenceGraph(str(db_path)) as g2:
        st = g2.stats()
        assert st["nodes"] == 0 and st["edges"] == 0
        assert g2.related_sites(A, depth=2) == []
        g2.add_site(A)
        g2.add_image(H1, A)
        assert g2.link_shared_images() == 0
        assert g2.stats()["images"] == 1


def test_corrupt_db_ignores_sidecar_files(tmp_path: Any) -> None:
    """损坏重建连 -wal/-shm 伴生文件一并清理,不残留脏数据。"""
    db_path = tmp_path / "graph.db"
    with _graph(tmp_path) as g:
        g.add_site(A)
    db_path.write_bytes(b"\x00garbage\x00not-sqlite")
    (tmp_path / "graph.db-wal").write_bytes(b"stale-wal")
    (tmp_path / "graph.db-shm").write_bytes(b"stale-shm")

    with EvidenceGraph(str(db_path)) as g2:
        assert g2.stats()["nodes"] == 0
        g2.add_site(B)
        assert g2.stats()["sites"] == 1


def test_concurrent_adds_and_reads(tmp_path: Any) -> None:
    """多线程并发 add_site/add_image/add_template/add_redirect + 并发读:无异常、计数精确。"""
    shared_site = "https://shared.example.com/"
    workers, per_thread = 6, 5
    errors: list[Exception] = []

    def hammer(k: int) -> None:
        try:
            own = f"https://t{k}.example.com/"
            for i in range(per_thread):
                sha = f"{k:02d}{i:02d}" + "a" * 60
                g.add_site(shared_site)  # 同站点反复 add:幂等
                g.add_image(sha, shared_site)
                g.add_image(sha, own)
                g.add_template(f"t{k:02d}", shared_site)
                g.add_template(f"t{k:02d}", own)
                g.add_redirect(own, shared_site, weight=0.5)
        except Exception as exc:  # noqa: BLE001 - 收集给主线程断言
            errors.append(exc)

    def reader() -> None:
        try:
            for _ in range(40):
                g.stats()
                g.export_json()
                g.related_sites(shared_site, depth=1)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    with _graph(tmp_path) as g:
        threads = [threading.Thread(target=hammer, args=(k,)) for k in range(workers)]
        threads += [threading.Thread(target=reader) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        st = g.stats()
        assert st["sites"] == 1 + workers
        assert st["images"] == workers * per_thread
        assert st["templates"] == workers
        assert st["edges"] == workers  # 每线程 (shared, t_k) 一条 redirect

        assert g.link_shared_images() == workers  # 每线程一对 (shared, t_k)
        assert g.link_templates() == workers
        assert g.link_shared_images() == 0  # 幂等

        related = {r["site"]: r for r in g.related_sites(shared_site, depth=1)}
        assert len(related) == workers
        # 共享枢纽站聚合了全部 30 张图:对 (shared, t_k) 而言
        # shared_image 权重 = 5/30 = 1/6、shared_template = 1/6,
        # redirect = 0.5 → 聚合权重取最大 = 0.5;via 聚合全部三种边。
        sample = related["https://t0.example.com/"]
        assert sample["via"] == ["redirect", "shared_image", "shared_template"]
        assert sample["weight"] == pytest.approx(0.5)
        img_weights = {(s, d): w for s, d, w in _edges_of_kind(g, "shared_image")}
        assert img_weights[_sites_pair(shared_site, "https://t0.example.com/")] == (
            pytest.approx(per_thread / (workers * per_thread))
        )


def test_db_parent_directory_auto_created(tmp_path: Any) -> None:
    """库路径父目录不存在时自动创建(如 data/nested/graph.db)。"""
    db_path = tmp_path / "deep" / "nested" / "graph.db"
    with EvidenceGraph(str(db_path)) as g:
        assert db_path.exists()
        g.add_site(A)
        assert g.stats()["sites"] == 1


# ---------------------------------------------------------------------------
# V5 升级锁定:WAL + busy_timeout / 批量写等价 / 遥测
# ---------------------------------------------------------------------------


def test_v5_sqlite_wal_and_busy_timeout(tmp_path: Any) -> None:
    """V5:图谱连接启用 WAL 与 busy_timeout=5000。"""
    with _graph(tmp_path) as g:
        mode = g._conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout = g._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert str(mode).lower() == "wal"
        assert int(timeout) == 5000


def test_v5_wal_allows_reader_while_writer_open(tmp_path: Any) -> None:
    """WAL 下第二实例(独立连接)可在第一实例持库期间正常读取。"""
    db_path = str(tmp_path / "graph.db")
    with EvidenceGraph(db_path) as writer:
        _seed_three_sites(writer)
        writer.link_shared_images()
        with EvidenceGraph(db_path) as reader:  # 旧 journal 模式下读写常互斥
            assert reader.stats()["edges"] == 2
            assert {r["site"] for r in reader.related_sites(A, depth=2)} == {B, C}


def test_v5_link_telemetry_timer(tmp_path: Any) -> None:
    """link 记 telemetry.timer("graph.link")(shared_image 与 template 各一次)。"""
    from netsentinel import telemetry

    telemetry.reset()
    try:
        with _graph(tmp_path) as g:
            _seed_three_sites(g)
            g.add_template(T1, A)
            g.add_template(T1, B)
            g.link_shared_images()
            g.link_templates()
            g.link_shared_images()  # 幂等重跑同样计时
        snap = telemetry.snapshot()
        assert snap["timers"]["graph.link"]["count"] == 3
    finally:
        telemetry.reset()


def test_v5_corrupt_rebuild_counted_in_telemetry(tmp_path: Any) -> None:
    """损坏重建记 telemetry.inc("graph.rebuild")(可观测的降级事件)。"""
    from netsentinel import telemetry

    db_path = tmp_path / "graph.db"
    with _graph(tmp_path) as g:
        g.add_site(A)
    db_path.write_bytes(b"garbage bytes, not a sqlite database")

    telemetry.reset()
    try:
        with EvidenceGraph(str(db_path)) as g2:
            assert g2.stats()["nodes"] == 0
        assert telemetry.snapshot()["counters"].get("graph.rebuild") == 1
    finally:
        telemetry.reset()


def test_v5_link_batched_write_matches_reference(tmp_path: Any) -> None:
    """V5 executemany 批量写:产出边集合 / 权重与逐条写参考实现逐项一致。"""
    from netsentinel.intel.graph import (
        _UPSERT_EDGE_SQL,
        EDGE_SHARED_IMAGE,
        NODE_IMAGE,
        _load_meta,
        _site_id,
    )

    with _graph(tmp_path, "ref.db") as ref:
        for url in (A, B, C):
            ref.add_site(url)
        ref.add_image(H1, A)
        ref.add_image(H1, B)
        ref.add_image(H2, B)
        ref.add_image(H2, C)
        ref.add_image(H3, A)
        ref.add_image(H3, B)

        # 参考实现:与旧版逐条 execute 完全一致的写路径(在真实连接上执行)
        conn = ref._conn
        with ref._lock:
            rows = conn.execute(
                "SELECT id, meta FROM nodes WHERE kind = ?", (NODE_IMAGE,)
            ).fetchall()
            site_ids = {
                r["id"] for r in conn.execute(
                    "SELECT id FROM nodes WHERE kind = ?", ("site",)
                )
            }
            site_items: dict[str, set[str]] = {}
            pair_shared: dict[tuple[str, str], set[str]] = {}
            for row in rows:
                sites = sorted(
                    {str(s) for s in _load_meta(row["meta"]).get("sites", [])
                     if s and _site_id(str(s)) in site_ids}
                )
                for s in sites:
                    site_items.setdefault(s, set()).add(row["id"])
                for i in range(len(sites)):
                    for j in range(i + 1, len(sites)):
                        pair_shared.setdefault((sites[i], sites[j]), set()).add(row["id"])
            existing = {
                (r["src"], r["dst"]) for r in conn.execute(
                    "SELECT src, dst FROM edges WHERE kind = ?", (EDGE_SHARED_IMAGE,)
                )
            }
            assert existing == set()  # 参考写之前库中无 shared_image 边
            wanted: dict[tuple[str, str], float] = {}
            for (a, b), shared in pair_shared.items():
                union = site_items.get(a, set()) | site_items.get(b, set())
                wanted[(_site_id(a), _site_id(b))] = (
                    len(shared) / len(union) if union else 0.0
                )
            now = ref._now_iso()
            for (src, dst), w in sorted(wanted.items()):
                conn.execute(_UPSERT_EDGE_SQL, (src, dst, EDGE_SHARED_IMAGE, w, now))
            conn.commit()
        ref_edges = {
            (e["src"], e["dst"]): e["weight"]
            for e in ref.export_json()["edges"]
            if e["kind"] == EDGE_SHARED_IMAGE
        }

    with _graph(tmp_path, "new.db") as new:
        for url in (A, B, C):
            new.add_site(url)
        for sha, site in ((H1, A), (H1, B), (H2, B), (H2, C), (H3, A), (H3, B)):
            new.add_image(sha, site)
        assert new.link_shared_images() == len(ref_edges)
        new_edges = {
            (e["src"], e["dst"]): e["weight"]
            for e in new.export_json()["edges"]
            if e["kind"] == EDGE_SHARED_IMAGE
        }
        assert new_edges == ref_edges
