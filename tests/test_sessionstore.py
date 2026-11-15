# -*- coding: utf-8 -*-
"""sessionstore(会话累积库)单元测试 —— 全离线,只写 tmp_path。"""
from __future__ import annotations

import json

from netsentinel.contracts import Verdict
from netsentinel.sessionstore import SessionStore, SiteGroupAccum


def _open(tmp_path):
    return SessionStore(tmp_path / "session" / "accumulation.db")


# ---------------------------------------------------------------------------
# 累积与归组
# ---------------------------------------------------------------------------
def test_accumulate_same_site_merges_by_canonical(tmp_path):
    """www / 子域变体算同一网站:一组累积,别名并集,verdict 取最严重档。"""
    with _open(tmp_path) as store:
        g1 = store.accumulate("https://www.a.com/x", "suspect", 0.60, nsw_count=1)
        g2 = store.accumulate("https://img.a.com/y", "nsfw", 0.95, nsw_count=4)

        assert g1.name == g2.name == "a.com"
        assert g2.scan_count == 2
        assert g2.verdict == "nsfw"          # 历史最严重档(只升不降)
        assert g2.last_verdict == "nsfw"      # 最近一次档位
        assert g2.agg_max == 0.95
        assert g2.nsw_total == 5
        assert set(g2.aliases) == {"www.a.com", "img.a.com"}
        assert len(g2.site_urls) == 2

        groups = store.list_groups()
        assert len(groups) == 1
        assert groups[0].name == "a.com"


def test_accumulate_multisuffix_cn_domain(tmp_path):
    """多段后缀(com.cn)取末三段:www.site.com.cn 与子域同组。"""
    with _open(tmp_path) as store:
        store.accumulate("https://www.site.com.cn/a", "suspect", 0.5)
        g = store.accumulate("https://cdn.site.com.cn/b", "nsfw", 0.9)
        assert g.name == "site.com.cn"
        assert store.stats()["groups"] == 1


def test_accumulate_report_duck(tmp_path):
    """accumulate_report 鸭子读取 SiteReport(真实数据类)。"""
    from netsentinel.contracts import SiteReport

    report = SiteReport(
        site_url="https://www.b.com/",
        verdict=Verdict.NSFW,
        agg_nsw_prob=0.88,
        nsw_image_count=3,
        needs_review=True,
    )
    with _open(tmp_path) as store:
        g = store.accumulate_report(report)
        assert g.name == "b.com"
        assert g.verdict == "nsfw"
        assert g.agg_max == 0.88
        assert g.nsw_total == 3


def test_verdict_never_downgrades(tmp_path):
    """后续较轻的判定不稀释历史最严重档。"""
    with _open(tmp_path) as store:
        store.accumulate("https://c.com/", "nsfw", 0.99)
        g = store.accumulate("https://c.com/", "clean", 0.01)
        assert g.verdict == "nsfw"
        assert g.last_verdict == "clean"


def test_builtin_canonical_fallback(tmp_path):
    """A103 canonical 缺席(注入 None)时内置兜底保持同语义。"""
    store = SessionStore(
        tmp_path / "s.db", canonical=None
    )
    try:
        g = store.accumulate("https://www.fallback-example.com/p", "suspect", 0.5)
        assert g.name == "fallback-example.com"
    finally:
        store.close()


def test_pending_groups_sort_by_severity(tmp_path):
    """待举报组按 (严重档, agg_max) 降序。"""
    with _open(tmp_path) as store:
        store.accumulate("https://clean.com/", "clean", 0.1)
        store.accumulate("https://sus.com/", "suspect", 0.6)
        store.accumulate("https://bad.com/", "nsfw", 0.95)
        names = [g.name for g in store.pending_groups()]
        assert names == ["bad.com", "sus.com", "clean.com"]


# ---------------------------------------------------------------------------
# 持久化:下次运行继续
# ---------------------------------------------------------------------------
def test_reopen_continues_accumulation(tmp_path):
    """关库重开 = 下次运行在上一次基础上继续累积(跨会话延续)。"""
    db = tmp_path / "session" / "accumulation.db"
    with SessionStore(db) as store:
        store.accumulate("https://www.a.com/1", "suspect", 0.6)
    with SessionStore(db) as store:
        g = store.accumulate("https://www.a.com/2", "nsfw", 0.9)
        assert g.scan_count == 2
        stats = store.stats()
        assert stats == {"groups": 1, "pending": 1, "reported": 0, "scans": 2}
        events = store.group_events("a.com")
        assert len(events) == 2
        assert events[0]["verdict"] == "nsfw"  # 最新在前


def test_mark_reported_and_pending(tmp_path):
    """举报收档:mark_reported 幂等,收档后退出待举报清单。"""
    with _open(tmp_path) as store:
        store.accumulate("https://a.com/", "nsfw", 0.9)
        store.accumulate("https://b.com/", "suspect", 0.6)
        assert len(store.pending_groups()) == 2
        changed = store.mark_reported(["a.com"])
        assert changed == 1
        assert [g.name for g in store.pending_groups()] == ["b.com"]
        assert store.get_group("a.com").reported == 1
        # 幂等:重复标记不报错
        assert store.mark_reported(["a.com"]) == 1
        assert store.stats()["reported"] == 1


# ---------------------------------------------------------------------------
# 存档
# ---------------------------------------------------------------------------
def test_archive_writes_json_snapshot(tmp_path):
    """存档:JSON 快照落盘(临时文件 + 原子替换),含待举报/已举报分组。"""
    with _open(tmp_path) as store:
        store.accumulate("https://a.com/", "nsfw", 0.9)
        store.accumulate("https://b.com/", "clean", 0.1)
        store.mark_reported(["b.com"])
        path = store.archive(tmp_path / "session" / "archive.json")

        data = json.loads((tmp_path / "session" / "archive.json").read_text("utf-8"))
        assert data["stats"]["groups"] == 2
        assert data["stats"]["pending"] == 1
        assert [g["name"] for g in data["pending_groups"]] == ["a.com"]
        assert [g["name"] for g in data["reported_groups"]] == ["b.com"]
        assert data["reported_groups"][0]["reported"] is True
        assert not (tmp_path / "session" / "archive.json.tmp").exists()
        assert path.endswith("archive.json")


def test_site_group_accum_sort_key_deterministic():
    """排序键全同的组按组名升序兜底(输出确定)。"""
    a = SiteGroupAccum(name="b.com", verdict="suspect", agg_max=0.5, scan_count=1)
    b = SiteGroupAccum(name="a.com", verdict="suspect", agg_max=0.5, scan_count=1)
    assert b.sort_key() < a.sort_key()
