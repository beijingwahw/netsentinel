"""A33 netsentinel.intel.site_memory 站点指纹记忆测试。

纯离线、仅用 tmp_path 落库。覆盖:指纹确定性与乱序不敏感、无图退化、
图片变化引起指纹变化、should_rescan 四分支(首见 / 未变未过期 / 变化 /
过期——monkeypatch _now 与注入旧 updated_at 两种方式)、TTL 边界、
remember UPSERT、跨实例持久化、多线程并发读写、损坏重建、stats 计数;
V10.4 追加 should_rescan 可选 ttl_hours 单次覆写(缺省 None 行为快照
一致、显式值生效且不污染库级 TTL)与 temporal 爆发检测联动装配演练。
"""
from __future__ import annotations

import datetime as _dt
import random
import sqlite3
import threading
from collections.abc import Callable
from typing import Any

import pytest

from netsentinel import telemetry
from netsentinel.contracts import ImageEvidence, ImageScore, PageSample, SiteReport
from netsentinel.intel import site_memory
from netsentinel.intel.site_memory import SiteMemory

SITE = "https://example.com/"

FAKE_NOW = _dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def _img(sha: str, page: str = SITE) -> ImageEvidence:
    return ImageEvidence(
        path=f"img_{sha[:8]}.png",
        url=f"{SITE}static/{sha[:8]}.png",
        source_page=page,
        sha256=sha,
        width=200,
        height=200,
    )


def _page(url: str, shas: list[str] | None = None) -> PageSample:
    return PageSample(url=url, image_evidences=[_img(s, url) for s in (shas or [])])


def _report(pages: list[PageSample], site: str = SITE) -> SiteReport:
    return SiteReport(site_url=site, pages=pages)


def _memory(tmp_path: Any, ttl_hours: int = 72) -> SiteMemory:
    return SiteMemory(str(tmp_path / "site_memory.db"), ttl_hours=ttl_hours)


def _fixed_clock(moment: _dt.datetime) -> Callable[[SiteMemory], _dt.datetime]:
    def clock(self: SiteMemory) -> _dt.datetime:
        return moment

    return clock


def _read_row(db_path: str, site_url: str) -> sqlite3.Row | None:
    """用独立连接直读一行,验证落盘内容(不经过被测对象)。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM sites WHERE site_url = ?", (site_url,)
        ).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 指纹:确定性与乱序不敏感
# ---------------------------------------------------------------------------


def test_fingerprint_deterministic_and_format(tmp_path: Any) -> None:
    """同一份 report 两次调用指纹一致,且为 16 位十六进制。"""
    mem = _memory(tmp_path)
    report = _report([_page(SITE, ["a" * 64, "b" * 64])])
    fp1 = mem.fingerprint(report)
    fp2 = mem.fingerprint(report)
    assert fp1 == fp2
    assert len(fp1) == 16
    int(fp1, 16)  # 合法十六进制
    mem.close()


def test_fingerprint_equal_for_equivalent_reports(tmp_path: Any) -> None:
    """内容等价但独立构造的两份 report 指纹相同(页序/图序打乱)。"""
    mem = _memory(tmp_path)
    urls = [f"{SITE}p{i}.html" for i in range(5)]
    shas = [f"{c * 64}" for c in "abcde"]
    a = _report([_page(u, [shas[i], shas[(i + 1) % 5]]) for i, u in enumerate(urls)])
    shuffled_pages = list(a.pages)
    random.Random(2026).shuffle(shuffled_pages)
    b = _report(
        [
            PageSample(
                url=p.url,
                image_evidences=list(reversed(p.image_evidences)),  # 图序打乱
            )
            for p in shuffled_pages  # 页序打乱
        ]
    )
    assert mem.fingerprint(a) == mem.fingerprint(b)
    mem.close()


def test_fingerprint_duplicate_pages_and_images_do_not_change_it(tmp_path: Any) -> None:
    """重复出现的页面 URL / 图片 sha 去重:重复一份指纹不变。"""
    mem = _memory(tmp_path)
    lean = _report([_page(SITE, ["a" * 64])])
    fat = _report([_page(SITE, ["a" * 64]), _page(SITE, ["a" * 64])])
    assert mem.fingerprint(lean) == mem.fingerprint(fat)
    mem.close()


def test_fingerprint_empty_report_still_valid(tmp_path: Any) -> None:
    """空 report(无页面无图)也能给出合法 16 位十六进制指纹。"""
    mem = _memory(tmp_path)
    fp = mem.fingerprint(_report([]))
    assert len(fp) == 16
    int(fp, 16)
    mem.close()


# ---------------------------------------------------------------------------
# 指纹:无图退化与素材敏感性
# ---------------------------------------------------------------------------


def test_fingerprint_no_images_degrades_to_url_list(tmp_path: Any) -> None:
    """无图退化:指纹只取决于 URL 列表,且对页序不敏感。"""
    mem = _memory(tmp_path)
    a = _report([_page(f"{SITE}a.html"), _page(f"{SITE}b.html")])
    b = _report([_page(f"{SITE}b.html"), _page(f"{SITE}a.html")])
    assert mem.fingerprint(a) == mem.fingerprint(b)
    mem.close()


def test_fingerprint_changes_when_urls_change(tmp_path: Any) -> None:
    """URL 列表不同(无论有无图)指纹不同。"""
    mem = _memory(tmp_path)
    a = _report([_page(f"{SITE}a.html"), _page(f"{SITE}b.html")])
    c = _report([_page(f"{SITE}a.html"), _page(f"{SITE}c.html")])
    assert mem.fingerprint(a) != mem.fingerprint(c)
    mem.close()


def test_fingerprint_changes_when_image_changes(tmp_path: Any) -> None:
    """页面集合不变、任一图片 sha256 变化 → 指纹变化。"""
    mem = _memory(tmp_path)
    a = _report([_page(f"{SITE}v1.html", ["a" * 64]), _page(f"{SITE}v2.html", ["b" * 64])])
    b = _report([_page(f"{SITE}v1.html", ["a" * 64]), _page(f"{SITE}v2.html", ["c" * 64])])
    assert mem.fingerprint(a) != mem.fingerprint(b)
    mem.close()


def test_fingerprint_adding_images_changes_it(tmp_path: Any) -> None:
    """同 URL 集合:从无图到有图指纹必须变化(无图是退化,不是忽略图)。"""
    mem = _memory(tmp_path)
    bare = _report([_page(f"{SITE}a.html")])
    with_img = _report([_page(f"{SITE}a.html", ["d" * 64])])
    assert mem.fingerprint(bare) != mem.fingerprint(with_img)
    mem.close()


def test_fingerprint_counts_image_scores_only_shas(tmp_path: Any) -> None:
    """只出现在 image_scores 里(页面 evidences 之外)的 sha 也计入素材。"""
    mem = _memory(tmp_path)
    r1 = _report([_page(f"{SITE}a.html")])
    r2 = _report([_page(f"{SITE}a.html")])
    r2.image_scores = [ImageScore(image=_img("e" * 64), model="stub", nsfw_prob=0.1)]
    assert mem.fingerprint(r1) != mem.fingerprint(r2)
    mem.close()


# ---------------------------------------------------------------------------
# should_rescan:四分支
# ---------------------------------------------------------------------------


def test_should_rescan_first_seen(tmp_path: Any) -> None:
    """分支一:无记录 → 必扫,原因为"首次收录"。"""
    mem = _memory(tmp_path)
    need, reason = mem.should_rescan(SITE, "aaaa")
    assert need is True
    assert reason == "首次收录"
    mem.close()


def test_should_rescan_unchanged_within_ttl(tmp_path: Any, monkeypatch: Any) -> None:
    """分支二:指纹相同且未过 TTL → 跳过,原因含"指纹未变化 / 距上次 N 小时"。"""
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW))
    mem = _memory(tmp_path, ttl_hours=72)
    fp = mem.fingerprint(_report([_page(f"{SITE}a.html", ["a" * 64])]))
    mem.remember(SITE, fp)
    need, reason = mem.should_rescan(SITE, fp)
    assert need is False
    assert "指纹未变化" in reason
    assert "距上次" in reason
    assert "小时" in reason
    mem.close()


def test_should_rescan_changed_fingerprint(tmp_path: Any, monkeypatch: Any) -> None:
    """分支三:记录存在但指纹不同 → 必扫,"站点内容已变化"。"""
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW))
    mem = _memory(tmp_path)
    mem.remember(SITE, "aaaa")
    need, reason = mem.should_rescan(SITE, "bbbb")
    assert need is True
    assert reason == "站点内容已变化"
    mem.close()


def test_should_rescan_expired_via_fake_clock(tmp_path: Any, monkeypatch: Any) -> None:
    """分支四(方式一:monkeypatch _now 推进时钟):指纹相同但超 TTL → 必扫。"""
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW))
    mem = _memory(tmp_path, ttl_hours=24)
    mem.remember(SITE, "aaaa")
    # 时钟前进 25 小时(> TTL 24)
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW + _dt.timedelta(hours=25)))
    need, reason = mem.should_rescan(SITE, "aaaa")
    assert need is True
    assert reason == "超过 TTL 需复查"
    mem.close()


def test_should_rescan_expired_via_stale_updated_at(tmp_path: Any, monkeypatch: Any) -> None:
    """分支四(方式二:注入旧 updated_at):落盘时间早已超 TTL → 必扫。"""
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW))
    mem = _memory(tmp_path, ttl_hours=72)
    mem.remember(SITE, "aaaa")
    db_path = str(tmp_path / "site_memory.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE sites SET updated_at = '2020-01-01T00:00:00+00:00' WHERE site_url = ?",
        (SITE,),
    )
    conn.commit()
    conn.close()
    need, reason = mem.should_rescan(SITE, "aaaa")
    assert need is True
    assert reason == "超过 TTL 需复查"
    mem.close()


def test_should_rescan_ttl_boundary(tmp_path: Any, monkeypatch: Any) -> None:
    """TTL 边界:恰好到 TTL 视为过期必扫;差一秒未到则跳过。"""
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW))
    mem = _memory(tmp_path, ttl_hours=1)
    mem.remember(SITE, "aaaa")

    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW + _dt.timedelta(hours=1)))
    need_at_edge, _ = mem.should_rescan(SITE, "aaaa")
    assert need_at_edge is True  # 恰好 ttl_hours:按过期处理

    monkeypatch.setattr(
        SiteMemory, "_now", _fixed_clock(FAKE_NOW + _dt.timedelta(hours=1) - _dt.timedelta(seconds=1))
    )
    need_just_under, reason = mem.should_rescan(SITE, "aaaa")
    assert need_just_under is False
    assert "指纹未变化" in reason
    mem.close()


def test_should_rescan_unparseable_updated_at_treated_expired(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """脏数据防御:updated_at 无法解析时按过期处理(宁可多扫)。"""
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW))
    mem = _memory(tmp_path)
    mem.remember(SITE, "aaaa")
    conn = sqlite3.connect(str(tmp_path / "site_memory.db"))
    conn.execute("UPDATE sites SET updated_at = '不是时间' WHERE site_url = ?", (SITE,))
    conn.commit()
    conn.close()
    need, reason = mem.should_rescan(SITE, "aaaa")
    assert need is True
    assert reason == "超过 TTL 需复查"
    mem.close()


def test_should_rescan_reports_age_hours(tmp_path: Any, monkeypatch: Any) -> None:
    """跳过原因里携带实际经过小时数(如 "距上次 10.0 小时")。"""
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW))
    mem = _memory(tmp_path, ttl_hours=72)
    mem.remember(SITE, "aaaa")
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW + _dt.timedelta(hours=10)))
    _, reason = mem.should_rescan(SITE, "aaaa")
    assert "距上次 10.0 小时" in reason
    mem.close()


# ---------------------------------------------------------------------------
# remember:UPSERT 与持久化
# ---------------------------------------------------------------------------


def test_remember_upsert_updates_not_duplicates(tmp_path: Any, monkeypatch: Any) -> None:
    """同一 URL 记两次:仍只有一行,指纹与 updated_at 均更新为新值。"""
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW))
    mem = _memory(tmp_path)
    mem.remember(SITE, "aaaa")
    t1 = _read_row(str(tmp_path / "site_memory.db"), SITE)
    assert t1 is not None and t1["fingerprint"] == "aaaa"

    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW + _dt.timedelta(hours=5)))
    mem.remember(SITE, "bbbb")
    t2 = _read_row(str(tmp_path / "site_memory.db"), SITE)
    assert mem.stats() == 1
    assert t2 is not None
    assert t2["fingerprint"] == "bbbb"
    assert t2["updated_at"] > t1["updated_at"]
    assert mem.last_fingerprint(SITE) == "bbbb"
    mem.close()


def test_memory_persists_across_instances(tmp_path: Any, monkeypatch: Any) -> None:
    """记忆落盘可跨实例生效:重开后未变站点依旧跳过。"""
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW))
    db_path = str(tmp_path / "site_memory.db")
    fp = SiteMemory(db_path).fingerprint(_report([_page(f"{SITE}a.html")]))
    with SiteMemory(db_path) as mem1:
        mem1.remember(SITE, fp)
    with SiteMemory(db_path) as mem2:
        assert mem2.stats() == 1
        need, reason = mem2.should_rescan(SITE, fp)
        assert need is False
        assert "指纹未变化" in reason
        assert mem2.last_fingerprint(SITE) == fp


def test_last_fingerprint_missing_returns_empty(tmp_path: Any) -> None:
    """读口:无记录的站点返回空串而非抛错。"""
    mem = _memory(tmp_path)
    assert mem.last_fingerprint("https://never.example.com/") == ""
    mem.close()


# ---------------------------------------------------------------------------
# stats 与多线程
# ---------------------------------------------------------------------------


def test_stats_counts_rows(tmp_path: Any) -> None:
    """stats 返回 sites 表行数,随 remember 增长。"""
    mem = _memory(tmp_path)
    assert mem.stats() == 0
    for i in range(3):
        mem.remember(f"https://s{i}.example.com/", f"fp{i}")
    assert mem.stats() == 3
    mem.close()


def test_thread_safe_concurrent_remember(tmp_path: Any) -> None:
    """多线程并发 remember 同一 / 不同 URL:无异常、无丢行、库可继续用。"""
    mem = _memory(tmp_path)
    same_url = "https://same.example.com/"
    errors: list[Exception] = []

    def hammer_same(worker: int) -> None:
        try:
            for i in range(30):
                mem.remember(same_url, f"fp-{worker}-{i}")
        except Exception as exc:  # noqa: BLE001 - 收集给主线程断言
            errors.append(exc)

    def hammer_distinct(worker: int) -> None:
        try:
            for i in range(30):
                url = f"https://d{worker}-{i}.example.com/"
                mem.remember(url, f"fp-{worker}-{i}")
                mem.should_rescan(url, f"fp-{worker}-{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=hammer_same, args=(k,)) for k in range(4)
    ] + [
        threading.Thread(target=hammer_distinct, args=(k,)) for k in range(4, 8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert mem.stats() == 1 + 4 * 30  # 同 URL 归一行 + 各线程独立 URL
    # 并发写入后库仍可正常决策
    need, reason = mem.should_rescan(same_url, "not-the-final-fp")
    assert need is True
    assert reason == "站点内容已变化"
    mem.close()


# ---------------------------------------------------------------------------
# 损坏重建
# ---------------------------------------------------------------------------


def test_corrupt_db_is_rebuilt(tmp_path: Any) -> None:
    """库文件损坏:重建为空库(行数归零),之后读写一切正常。"""
    db_path = tmp_path / "site_memory.db"
    with _memory(tmp_path) as mem:
        mem.remember(SITE, "aaaa")
        assert mem.stats() == 1
    db_path.write_bytes(b"this is definitely not a sqlite database !!!")

    mem2 = SiteMemory(str(db_path))
    assert mem2.stats() == 0  # 旧记忆随损坏文件丢弃
    need, reason = mem2.should_rescan(SITE, "aaaa")
    assert need is True
    assert reason == "首次收录"
    mem2.remember(SITE, "bbbb")
    assert mem2.stats() == 1
    assert mem2.last_fingerprint(SITE) == "bbbb"
    mem2.close()


def test_corrupt_db_ignores_sidecar_files(tmp_path: Any) -> None:
    """损坏重建时连 -wal/-shm 伴生文件一并清理,不残留脏数据。"""
    db_path = tmp_path / "site_memory.db"
    with _memory(tmp_path) as mem:
        mem.remember(SITE, "aaaa")
    db_path.write_bytes(b"\x00garbage\x00not-sqlite")
    (tmp_path / "site_memory.db-wal").write_bytes(b"stale-wal")
    (tmp_path / "site_memory.db-shm").write_bytes(b"stale-shm")

    mem2 = SiteMemory(str(db_path))
    assert mem2.stats() == 0
    mem2.remember(SITE, "cccc")
    need, _ = mem2.should_rescan(SITE, "cccc")
    assert need is False
    mem2.close()


def test_ttl_hours_is_configurable(tmp_path: Any, monkeypatch: Any) -> None:
    """自定义 TTL 生效:ttl=0 一切按过期处理,必扫。"""
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW))
    mem = _memory(tmp_path, ttl_hours=0)
    mem.remember(SITE, "aaaa")
    need, reason = mem.should_rescan(SITE, "aaaa")
    assert need is True
    assert reason == "超过 TTL 需复查"
    mem.close()


def test_default_ttl_matches_contract(tmp_path: Any) -> None:
    """默认 TTL 为 72 小时(与契约 / scheduler 常量一致)。"""
    mem = _memory(tmp_path)
    assert mem.ttl_hours == 72
    mem.close()


def test_db_parent_directory_auto_created(tmp_path: Any) -> None:
    """库路径的父目录不存在时自动创建(如 data/nested/site_memory.db)。"""
    db_path = tmp_path / "deep" / "nested" / "site_memory.db"
    mem = SiteMemory(str(db_path))
    assert db_path.exists()
    mem.remember(SITE, "aaaa")
    assert mem.stats() == 1
    mem.close()


@pytest.mark.parametrize("bad_path", ["", "   "])
def test_blank_site_url_roundtrips(tmp_path: Any, bad_path: str) -> None:
    """边界:空白 site_url 也能落库 / 读回(不抛错,由调用方保证语义)。"""
    mem = _memory(tmp_path)
    mem.remember(bad_path, "aaaa")
    need, reason = mem.should_rescan(bad_path, "aaaa")
    assert need is False
    assert "指纹未变化" in reason
    mem.close()


# ---------------------------------------------------------------------------
# V5 升级:WAL + busy_timeout、时间戳解析缓存、rescan_skip 遥测
# ---------------------------------------------------------------------------
def test_v5_wal_and_busy_timeout_pragmas(tmp_path: Any) -> None:
    """新连接统一启用 WAL 日志与 5 秒忙等待(含损坏重建路径)。"""
    db_path = str(tmp_path / "site_memory.db")
    with SiteMemory(db_path) as mem:
        mode = mem._conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout = mem._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert str(mode).lower() == "wal"
        assert int(timeout) == site_memory.BUSY_TIMEOUT_MS == 5000

    pathlib_like = tmp_path / "site_memory.db"
    pathlib_like.write_bytes(b"garbage-not-sqlite")  # 触发损坏重建路径
    with SiteMemory(db_path) as mem2:
        mode2 = mem2._conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout2 = mem2._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert str(mode2).lower() == "wal"
        assert int(timeout2) == 5000


def test_v5_should_rescan_skip_telemetry_counter(tmp_path: Any, monkeypatch: Any) -> None:
    """只有"指纹未变未过期"分支累加 site_memory.rescan_skip。"""
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW))
    mem = _memory(tmp_path, ttl_hours=72)
    mem.remember(SITE, "aaaa")
    telemetry.reset()
    need_keep, _ = mem.should_rescan(SITE, "aaaa")  # 跳过分支
    need_change, _ = mem.should_rescan(SITE, "bbbb")  # 变化分支
    need_first, _ = mem.should_rescan("https://new.example.com/", "aaaa")  # 首见分支
    assert (need_keep, need_change, need_first) == (False, True, True)
    assert telemetry.snapshot()["counters"]["site_memory.rescan_skip"] == 1
    mem.close()


def test_v5_parse_ts_cached_hits_cache(tmp_path: Any) -> None:
    """相同 updated_at 字符串只解析一次(缓存命中返回同一对象);空串不缓存解析结果。"""
    ts = "2026-10-01T12:00:00+00:00"
    site_memory._TS_CACHE.clear()
    first = site_memory._parse_ts_cached(ts)
    assert ts in site_memory._TS_CACHE
    assert site_memory._parse_ts_cached(ts) is first  # 命中缓存,零重复解析
    assert site_memory._parse_ts_cached("") is None  # 空串:未缓存也返回 None
    # 缓存条目数有界:灌满上限后整体清空,不会无界增长
    assert site_memory._TS_CACHE_MAX > 0
    site_memory._TS_CACHE.clear()


# ---------------------------------------------------------------------------
# V10.4 联动:should_rescan 可选 ttl_hours 单次覆写(向后兼容)
# ---------------------------------------------------------------------------


def test_v104_ttl_hours_none_is_byte_identical_snapshot(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """缺省路径快照一致:不传参与 ttl_hours=None 结果逐字节相同(含原因串)。"""
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW))
    mem = _memory(tmp_path, ttl_hours=72)
    mem.remember(SITE, "aaaa")
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW + _dt.timedelta(hours=10)))
    without_param = mem.should_rescan(SITE, "aaaa")
    with_none = mem.should_rescan(SITE, "aaaa", ttl_hours=None)
    assert without_param == with_none
    # 与 V10.4 之前的既有消息逐字符一致(向后兼容红线)
    assert with_none == (
        False,
        "指纹未变化,距上次 10.0 小时,未超 TTL(72 小时),本轮跳过",
    )
    mem.close()


def test_v104_explicit_ttl_hours_overrides_for_single_call(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """显式覆写仅当次生效:年龄 30h 时默认 72h 跳过、显式 24 过期、100 仍跳过。"""
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW))
    mem = _memory(tmp_path, ttl_hours=72)
    mem.remember(SITE, "aaaa")
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW + _dt.timedelta(hours=30)))

    need_default, reason_default = mem.should_rescan(SITE, "aaaa")
    assert need_default is False  # 30h < 72h:默认跳过
    assert "TTL(72 小时)" in reason_default

    need_short, reason_short = mem.should_rescan(SITE, "aaaa", ttl_hours=24)
    assert need_short is True  # 30h >= 24h:覆写后过期
    assert reason_short == "超过 TTL 需复查"

    need_long, reason_long = mem.should_rescan(SITE, "aaaa", ttl_hours=100)
    assert need_long is False
    assert "TTL(100 小时)" in reason_long  # 原因里显示生效中的 TTL

    # 库级 TTL 未被污染:覆写后再走缺省路径,行为回到 72h
    need_after, reason_after = mem.should_rescan(SITE, "aaaa")
    assert need_after is False
    assert "TTL(72 小时)" in reason_after
    mem.close()


def test_v104_ttl_hours_zero_and_negative_expire_immediately(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """极端覆写:0 / 负值视同立即过期(安全方向:多扫不漏扫)。"""
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW))
    mem = _memory(tmp_path)
    mem.remember(SITE, "aaaa")
    assert mem.should_rescan(SITE, "aaaa", ttl_hours=0) == (True, "超过 TTL 需复查")
    assert mem.should_rescan(SITE, "aaaa", ttl_hours=-5) == (True, "超过 TTL 需复查")
    mem.close()


def test_v104_ttl_hours_float_truncates_to_whole_hours(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """小数 TTL(suggest_ttl 的返回是 float)向零截断:14.9 生效为 14。"""
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW))
    mem = _memory(tmp_path, ttl_hours=72)
    mem.remember(SITE, "aaaa")
    # 年龄 13.5h < int(14.9)=14 → 跳过,原因里显示截断后的整数 14
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW + _dt.timedelta(hours=13, minutes=30)))
    need_keep, reason = mem.should_rescan(SITE, "aaaa", ttl_hours=14.9)
    assert need_keep is False
    assert "TTL(14 小时)" in reason  # int(14.9) == 14,不带小数
    # 年龄 14.5h ≥ 14 → 同一覆写值判定为过期
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW + _dt.timedelta(hours=14, minutes=30)))
    need_expire, _ = mem.should_rescan(SITE, "aaaa", ttl_hours=14.9)
    assert need_expire is True
    mem.close()


def test_v104_ttl_hours_does_not_touch_dominant_branches(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """首见 / 指纹变化分支不受 ttl_hours 影响(它们先于 TTL 判定)。"""
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW))
    mem = _memory(tmp_path)
    mem.remember(SITE, "aaaa")
    assert mem.should_rescan(SITE, "bbbb", ttl_hours=999) == (True, "站点内容已变化")
    assert mem.should_rescan("https://new.example.com/", "aaaa", ttl_hours=0) == (
        True,
        "首次收录",
    )
    mem.close()


def test_v104_ttl_hours_boundary_matches_legacy_semantics(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """覆写值沿用既有边界语义:恰好到 TTL 视为过期,差一秒未到则跳过。"""
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW))
    mem = _memory(tmp_path, ttl_hours=72)
    mem.remember(SITE, "aaaa")
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW + _dt.timedelta(hours=10)))
    need_at_edge, _ = mem.should_rescan(SITE, "aaaa", ttl_hours=10)
    assert need_at_edge is True  # 恰好 10h:按过期处理
    monkeypatch.setattr(
        SiteMemory,
        "_now",
        _fixed_clock(FAKE_NOW + _dt.timedelta(hours=10) - _dt.timedelta(seconds=1)),
    )
    need_just_under, reason = mem.should_rescan(SITE, "aaaa", ttl_hours=10)
    assert need_just_under is False
    assert "TTL(10 小时)" in reason
    mem.close()


def test_v104_linkage_burst_suggested_ttl_feeds_should_rescan(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """联动装配演练(temporal → site_memory;scheduler 在领地外,不改):

    图谱边时间戳 → kleinberg_bursts → burst_factor → suggest_ttl →
    should_rescan(ttl_hours=建议值):爆发期建议 TTL 收短使"同指纹也
    到期复查",稳态 factor=0 时建议值等于基准、行为与缺省一致。
    """
    from netsentinel.intel.temporal import burst_factor, kleinberg_bursts, suggest_ttl

    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW))
    mem = _memory(tmp_path, ttl_hours=72)
    mem.remember(SITE, "aaaa")
    # 距上次 30 小时:默认 72h 内应跳过
    monkeypatch.setattr(SiteMemory, "_now", _fixed_clock(FAKE_NOW + _dt.timedelta(hours=30)))

    # 稳态:无爆发边 → 建议值 = 基准 72 → 与缺省路径同判(跳过)
    calm_factor = burst_factor(kleinberg_bursts([]), 30.0)
    calm_ttl = suggest_ttl(72, calm_factor)
    assert calm_ttl == pytest.approx(72.0)
    need_calm, _ = mem.should_rescan(SITE, "aaaa", ttl_hours=calm_ttl)
    assert need_calm is False

    # 扩张期:团伙边密集出现(小时序,[300, 302.9] 密集段,now=301 在段内)
    edge_hours = [float(i) for i in range(0, 300, 24)] + [300.0 + 0.1 * i for i in range(30)]
    bursts = kleinberg_bursts(edge_hours)
    hot_factor = burst_factor(bursts, 301.0)
    assert hot_factor > 0.5  # 强爆发
    hot_ttl = suggest_ttl(72, hot_factor)
    assert hot_ttl < 30.0  # 建议已收短到当前年龄之下
    need_hot, reason_hot = mem.should_rescan(SITE, "aaaa", ttl_hours=hot_ttl)
    assert need_hot is True  # 同指纹未变,但按爆发期建议 TTL 到期复查
    assert reason_hot == "超过 TTL 需复查"
    mem.close()
