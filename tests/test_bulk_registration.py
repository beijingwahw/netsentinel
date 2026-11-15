# -*- coding: utf-8 -*-
"""批级写入测试:PhashRegistry.register_many 与 EvidenceGraph.add_*_bulk(V10.13)。"""
from __future__ import annotations

import sqlite3

import pytest

from netsentinel.intel.graph import EvidenceGraph
from netsentinel.intel.phash import PhashRegistry

_H16 = "ab" * 16  # phash 主列(任意非空十六进制即可)
_M16 = "cd" * 8   # mirror_hash(恰 16 hex = 64bit)
_P27 = "ef" * 13 + "a"  # pyramid_hash(恰 27 hex = 108bit)


def _rows_of(db_path, table, cols):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            f"SELECT {', '.join(cols)} FROM {table} ORDER BY 1"
        ).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# PhashRegistry.register_many
# ---------------------------------------------------------------------------
def test_register_many_equivalent_to_sequential(tmp_path):
    """批量单事务与逐条 register 落库结果逐行一致(时间戳列除外)。"""
    rows = [
        ("a" * 64, _H16, "https://a.com/", "", _M16, _P27),
        ("b" * 64, _H16, "https://b.com/"),
        ("c" * 64, "12" * 16, "https://c.com/", "nsfw"),
    ]
    seq = PhashRegistry(str(tmp_path / "seq.db"))
    bulk = PhashRegistry(str(tmp_path / "bulk.db"))
    try:
        for row in rows:
            if len(row) > 4:
                seq.register(row[0], row[1], row[2], row[3],
                             mirror_hash=row[4], pyramid_hash=row[5])
            elif len(row) > 3:
                seq.register(row[0], row[1], row[2], row[3])
            else:
                seq.register(row[0], row[1], row[2])
        n = bulk.register_many(rows)
        assert n == 3
        cols = "sha256, phash, site_url, verdict_tag, mirror_hash, pyramid_hash"
        assert _rows_of(tmp_path / "seq.db", "hashes", cols.split(", ")) == _rows_of(
            tmp_path / "bulk.db", "hashes", cols.split(", ")
        )
    finally:
        seq.close()
        bulk.close()


def test_register_many_atomic_validation(tmp_path):
    """任一行非法:整批不写(先全量校验后写库,不落半截数据)。"""
    registry = PhashRegistry(str(tmp_path / "p.db"))
    try:
        with pytest.raises(ValueError):
            registry.register_many(
                [("a" * 64, _H16, "https://a.com/"), ("", _H16, "https://b.com/")]
            )
        assert _rows_of(tmp_path / "p.db", "hashes", ["sha256"]) == []
        with pytest.raises(ValueError):  # 非法十六进制同样整批拒绝
            registry.register_many([("a" * 64, "zz" * 16, "https://a.com/")])
        assert _rows_of(tmp_path / "p.db", "hashes", ["sha256"]) == []
    finally:
        registry.close()


def test_register_many_upsert_and_empty(tmp_path):
    """批内同 sha 后行覆盖前行;重登覆盖;空批返回 0 零写入。"""
    registry = PhashRegistry(str(tmp_path / "p.db"))
    try:
        assert registry.register_many([]) == 0
        registry.register_many(
            [("a" * 64, _H16, "https://a.com/"), ("a" * 64, "12" * 16, "https://x.com/")]
        )
        rows = _rows_of(tmp_path / "p.db", "hashes", ["sha256", "phash", "site_url"])
        assert rows == [("a" * 64, "12" * 16, "https://x.com/")]
        registry.register_many([("a" * 64, "34" * 16, "https://y.com/")])
        rows = _rows_of(tmp_path / "p.db", "hashes", ["sha256", "site_url"])
        assert rows == [("a" * 64, "https://y.com/")]
    finally:
        registry.close()


# ---------------------------------------------------------------------------
# EvidenceGraph.add_images_bulk / add_templates_bulk
# ---------------------------------------------------------------------------
def test_graph_bulk_equivalent_to_sequential(tmp_path):
    """批级单事务与逐条 add_image/add_template 的节点/meta/统计完全一致。"""
    items = [("a" * 64, "https://s1.com/"), ("b" * 64, "https://s1.com/")]
    seq = EvidenceGraph(str(tmp_path / "seq.db"))
    bulk = EvidenceGraph(str(tmp_path / "bulk.db"))
    try:
        seq.add_image(items[0][0], items[0][1])
        seq.add_image(items[1][0], items[1][1])
        seq.add_template("t1", "https://s1.com/")
        n_img = bulk.add_images_bulk(items)
        n_tpl = bulk.add_templates_bulk([("t1", "https://s1.com/")])
        assert (n_img, n_tpl) == (2, 1)
        assert seq.stats() == bulk.stats()
        # 共享素材折叠边:两条路径产出的关联边一致
        assert seq.link_shared_images() == bulk.link_shared_images()
        assert seq.export_json() == bulk.export_json()
    finally:
        seq.close()
        bulk.close()


def test_graph_bulk_skip_empty_and_cross_site_merge(tmp_path):
    """空键/空站跳过且不计入返回值;同素材跨站并入同一节点 meta.sites。"""
    graph = EvidenceGraph(str(tmp_path / "g.db"))
    try:
        n = graph.add_images_bulk(
            [("", "https://s.com/"), ("a" * 64, ""), ("a" * 64, "https://s1.com/")]
        )
        assert n == 1
        graph.add_images_bulk([("a" * 64, "https://s2.com/")])
        data = graph.export_json()
        image_nodes = [
            node for node in data.get("nodes", [])
            if str(node.get("id", "")).startswith("image:")
        ]
        assert len(image_nodes) == 1
        sites = image_nodes[0].get("meta", {}).get("sites", [])
        assert set(sites) == {"https://s1.com/", "https://s2.com/"}
        # 幂等:重复批量写同一 (素材, 站点) 不产生变化
        before = graph.stats()
        graph.add_images_bulk([("a" * 64, "https://s1.com/")])
        assert graph.stats() == before
    finally:
        graph.close()
