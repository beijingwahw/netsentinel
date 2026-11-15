# -*- coding: utf-8 -*-
"""增量 link 折叠引擎测试(V10.14)—— 随机追加序列下"增量 ≡ 全量"性质验证。"""
from __future__ import annotations

import random
import types

from netsentinel.contracts import Config
from netsentinel.intel.graph import EvidenceGraph
from netsentinel.pipeline import kernel_wire

_SITES = [f"https://s{i}.example/" for i in range(6)]


def _edges(graph: EvidenceGraph) -> list:
    """shared_image + shared_template 边(排序,含权重)。"""
    data = graph.export_json()
    return sorted(
        (e["src"], e["dst"], e["kind"], e["weight"]) for e in data["edges"]
        if e["kind"] in ("shared_image", "shared_template")
    )


def test_incremental_equivalent_to_full_random_property(tmp_path):
    """固定种子随机追加序列:每步后增量图与全量图的全部边逐位相同。

    覆盖:新站点冷启动、私有素材、跨站共享(2~4 站同素材)、同站重登
    (幂等)、批量写口与单条写口混用、image 与 template 两类混跑。
    """
    rng = random.Random(20261014)
    full = EvidenceGraph(str(tmp_path / "full.db"))
    inc = EvidenceGraph(str(tmp_path / "inc.db"))
    try:
        item_pool = [f"{i:064x}" for i in range(30)]
        for step in range(40):
            # 本步写哪些站点、哪些素材(素材复用制造跨站共享)
            writers = rng.sample(_SITES, rng.randint(1, 3))
            items = rng.sample(item_pool, rng.randint(1, 5))
            for site in writers:
                for item in items:
                    if rng.random() < 0.5:
                        full.add_images_bulk([(item, site)])
                        inc.add_images_bulk([(item, site)])
                    else:
                        full.add_image(item, site)
                        inc.add_image(item, site)
                if rng.random() < 0.4:  # 偶尔登记模板
                    tpl = f"tpl{rng.randrange(8)}"
                    full.add_template(tpl, site)
                    inc.add_template(tpl, site)
            n_full = full.link_shared_images() + full.link_templates()
            n_inc = inc.link_shared_images(touched_sites=writers) + inc.link_templates(
                touched_sites=writers
            )
            # 新增计数一致:新边只能产生于触及站点对(等价定理推论)
            assert n_inc == n_full, f"第 {step} 步新增边数不一致"
            assert _edges(inc) == _edges(full), f"第 {step} 步边集不一致"
        # 收尾:增量图上做一次全量 link,应当零变化(已收敛到全量不动点)
        assert full.link_shared_images() + full.link_templates() == 0
    finally:
        full.close()
        inc.close()


def test_incremental_untouched_pairs_frozen(tmp_path):
    """触及站点私有不共享时:无关旧边权重逐位不动,新增数为 0。"""
    graph = EvidenceGraph(str(tmp_path / "g.db"))
    try:
        graph.add_images_bulk([("a" * 64, _SITES[0]), ("a" * 64, _SITES[1])])
        assert graph.link_shared_images() == 1
        before = _edges(graph)
        # 只给 s2 添加私有素材(不与任何站共享)
        graph.add_images_bulk([("b" * 64, _SITES[2]), ("c" * 64, _SITES[2])])
        assert graph.link_shared_images(touched_sites=[_SITES[2]]) == 0
        assert _edges(graph) == before  # s0-s1 边与权重逐位不变
    finally:
        graph.close()


def test_incremental_weight_refresh_matches_full(tmp_path):
    """触及对的权重刷新:增量权重 = 全量权重(浮点逐位相等)。"""
    full = EvidenceGraph(str(tmp_path / "full.db"))
    inc = EvidenceGraph(str(tmp_path / "inc.db"))
    try:
        for g in (full, inc):
            g.add_images_bulk([("a" * 64, _SITES[0]), ("b" * 64, _SITES[0]),
                               ("a" * 64, _SITES[1]), ("b" * 64, _SITES[1]),
                               ("c" * 64, _SITES[1])])
        full.link_shared_images()  # shared=2, union=3 → 2/3
        inc.link_shared_images(touched_sites=[_SITES[0]])
        assert _edges(inc) == _edges(full)
        # s0 再加一个共享素材:s0-s1 → shared=3, union=4
        for g in (full, inc):
            g.add_image("d" * 64, _SITES[0])
            g.add_image("d" * 64, _SITES[1])
        full.link_shared_images()
        inc.link_shared_images(touched_sites=[_SITES[0], _SITES[1]])
        assert _edges(inc) == _edges(full)
        weight = [e[3] for e in _edges(inc)][0]
        assert weight == 3 / 4
    finally:
        full.close()
        inc.close()


def test_empty_touched_means_full_mode(tmp_path):
    """touched_sites 为空列表 = 显式全量路径(与旧调用形态语义一致)。"""
    graph = EvidenceGraph(str(tmp_path / "g.db"))
    try:
        graph.add_images_bulk([("a" * 64, _SITES[0]), ("a" * 64, _SITES[1])])
        assert graph.link_shared_images(touched_sites=[]) == 1  # 全量:首算
        assert graph.link_shared_images(touched_sites=[]) == 0  # 全量:重跑幂等
    finally:
        graph.close()


def test_kernel_wire_passes_touched_sites(tmp_path, monkeypatch):
    """通电步接线:link_* 收到 touched_sites=[本站](V10.14 增量路径)。"""
    seen: list = []
    orig_img = EvidenceGraph.link_shared_images
    orig_tpl = EvidenceGraph.link_templates

    def spy_img(self, touched_sites=None):
        seen.append(("image", list(touched_sites) if touched_sites else None))
        return orig_img(self, touched_sites=touched_sites)

    def spy_tpl(self, touched_sites=None):
        seen.append(("template", list(touched_sites) if touched_sites else None))
        return orig_tpl(self, touched_sites=touched_sites)

    monkeypatch.setattr(EvidenceGraph, "link_shared_images", spy_img)
    monkeypatch.setattr(EvidenceGraph, "link_templates", spy_tpl)
    cfg = Config(
        graph_db=str(tmp_path / "g.db"),
        phash_db=str(tmp_path / "p.db"),
        data_dir=str(tmp_path / "data"),
    )
    pages = [
        types.SimpleNamespace(
            image_evidences=[types.SimpleNamespace(sha256="a" * 64)]
        )
    ]
    counts = kernel_wire.wire_graph_from_scan("https://x.example/", cfg, pages)
    assert counts["sites"] == 1 and counts["images"] == 1
    assert ("image", ["https://x.example/"]) in seen
    assert ("template", ["https://x.example/"]) in seen
