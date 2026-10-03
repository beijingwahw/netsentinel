"""A23 vlm_cache 测试(离线,只写 tmp_path);V5(A86)升级用例以 test_v5_ 前缀。"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading

import pytest

from netsentinel import telemetry
from netsentinel.vision.vlm_cache import (
    DEFAULT_DAILY_LIMIT,
    VlmBudgetExceeded,
    VlmCache,
)


@pytest.fixture()
def tel():
    """隔离 telemetry 全局态:进入/退出均清零(reset 仅供测试使用)。"""
    telemetry.reset()
    yield telemetry
    telemetry.reset()


def test_put_get_roundtrip(tmp_path):
    c = VlmCache(str(tmp_path / "c.db"), daily_limit=10)
    c.put("glm", "v2.1", "a" * 64, {"nsfw_prob": 0.9, "reasoning": "测试"})
    got = c.get("glm", "v2.1", "a" * 64)
    assert got is not None and got["nsfw_prob"] == pytest.approx(0.9)


def test_get_miss_returns_none(tmp_path):
    c = VlmCache(str(tmp_path / "c.db"))
    assert c.get("glm", "v2.1", "nope") is None


def test_put_upsert_overwrites(tmp_path):
    c = VlmCache(str(tmp_path / "c.db"))
    c.put("glm", "v2.1", "k", {"nsfw_prob": 0.1})
    c.put("glm", "v2.1", "k", {"nsfw_prob": 0.8})
    assert c.get("glm", "v2.1", "k")["nsfw_prob"] == pytest.approx(0.8)


def _seed_old_entry(c: VlmCache, days: int) -> None:
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    with c._lock:
        c._conn.execute(
            "INSERT INTO cache(model, prompt_version, image_sha256, payload, created_at)"
            " VALUES('glm','v2.1','old',:p,:t)"
            " ON CONFLICT(model, prompt_version, image_sha256)"
            " DO UPDATE SET created_at=excluded.created_at",
            {"p": json.dumps({"nsfw_prob": 0.5}), "t": old},
        )
        c._conn.commit()


def test_ttl_expired_entry_removed(tmp_path):
    c = VlmCache(str(tmp_path / "c.db"))
    _seed_old_entry(c, days=45)
    assert c.get("glm", "v2.1", "old") is None
    with c._lock:
        n = c._conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
    assert n == 0


def test_ttl_fresh_entry_survives(tmp_path):
    c = VlmCache(str(tmp_path / "c.db"))
    _seed_old_entry(c, days=1)
    assert c.get("glm", "v2.1", "old") is not None


def test_budget_starts_zero_and_increments(tmp_path):
    c = VlmCache(str(tmp_path / "c.db"), daily_limit=3)
    assert c.budget_state()["used"] == 0
    c.spend_one()
    c.spend_one()
    assert c.budget_state()["used"] == 2


def test_budget_exceeded_raises_with_chinese_message(tmp_path):
    c = VlmCache(str(tmp_path / "c.db"), daily_limit=1)
    c.spend_one()
    with pytest.raises(VlmBudgetExceeded) as ei:
        c.spend_one()
    assert "vlm_daily_budget" in str(ei.value) and "1/1" in str(ei.value)


def test_budget_resets_next_day(tmp_path):
    c = VlmCache(str(tmp_path / "c.db"), daily_limit=1)
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    with c._lock:
        c._conn.execute("INSERT INTO usage(day, used) VALUES(?,?)", (yesterday, 1))
        c._conn.commit()
    c.spend_one()  # 昨日用量不影响今日
    assert c.budget_state()["used"] == 1


def test_corrupt_db_rebuilds(tmp_path):
    db = tmp_path / "c.db"
    db.write_text("not a sqlite file", encoding="utf-8")
    c = VlmCache(str(db), daily_limit=5)
    c.put("glm", "v2.1", "k", {"nsfw_prob": 0.2})
    assert c.get("glm", "v2.1", "k") is not None


def test_thread_safety_spend(tmp_path):
    c = VlmCache(str(tmp_path / "c.db"), daily_limit=1000)
    errors: list[Exception] = []

    def worker() -> None:
        for _ in range(20):
            try:
                c.spend_one()
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert c.budget_state()["used"] == 160


def test_thread_safety_cache_rw(tmp_path):
    c = VlmCache(str(tmp_path / "c.db"), daily_limit=10)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            c.put("glm", "v2.1", f"k{i}", {"nsfw_prob": i / 100})
            c.get("glm", "v2.1", f"k{i}")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert c.stats()["cache_rows"] == 8


def test_stats(tmp_path):
    c = VlmCache(str(tmp_path / "c.db"), daily_limit=9)
    c.put("glm", "v2.1", "k1", {"nsfw_prob": 0.1})
    c.put("vlm-arbiter", "v2.1", "k2", {"nsfw_prob": 0.2})
    c.spend_one()
    s = c.stats()
    assert s["cache_rows"] == 2 and set(s["by_model"]) == {"glm", "vlm-arbiter"}
    assert s["today"]["used"] == 1 and s["today"]["limit"] == 9


# ===========================================================================
# V5(A86)升级用例
# ===========================================================================


def _seed_age(c: VlmCache, key: str, days: int) -> None:
    """按指定键名与年龄(天)直接写入 cache 表(绕过 put 的"当前时间"时间戳)。"""
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    with c._lock:
        c._conn.execute(
            "INSERT INTO cache(model, prompt_version, image_sha256, payload, created_at)"
            " VALUES('glm','v2.1',:k,:p,:t)"
            " ON CONFLICT(model, prompt_version, image_sha256)"
            " DO UPDATE SET created_at=excluded.created_at, payload=excluded.payload",
            {"k": key, "p": json.dumps({"nsfw_prob": 0.5}), "t": old},
        )
        c._conn.commit()


def test_v5_wal_and_busy_timeout_enabled(tmp_path) -> None:
    """V5 性能:连接一律 WAL + busy_timeout=5000。"""
    c = VlmCache(str(tmp_path / "c.db"))
    with c._lock:
        mode = c._conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout = c._conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert str(mode).lower() == "wal"
    assert int(timeout) == 5000


def test_v5_usage_day_primary_key_is_indexed(tmp_path) -> None:
    """V5 确认项:usage.day 是 TEXT PRIMARY KEY → sqlite 自动建 pk 索引,无需另建。"""
    c = VlmCache(str(tmp_path / "c.db"))
    with c._lock:
        infos = c._conn.execute("PRAGMA index_list('usage')").fetchall()
    # (seq, name, unique, origin, ...) 第 4 列 origin == 'pk' 即主键自动索引
    assert any(info[2] == 1 and info[3] == "pk" for info in infos)


def test_v5_default_daily_limit_constant() -> None:
    """V5 质量:每日预算缺省值 200 提为常量且行为冻结。"""
    assert DEFAULT_DAILY_LIMIT == 200


def test_v5_get_telemetry_hit_miss_and_timer(tmp_path, tel) -> None:
    """V5 可观测:get 命中/未命中计数 + vlm_cache.get 计时。"""
    c = VlmCache(str(tmp_path / "c.db"))
    c.put("glm", "v2.1", "k", {"nsfw_prob": 0.1})
    tel.reset()
    assert c.get("glm", "v2.1", "k") is not None  # 命中
    assert c.get("glm", "v2.1", "missing") is None  # 未命中
    snap = tel.snapshot()
    assert snap["counters"]["vlm_cache.hit"] == 1.0
    assert snap["counters"]["vlm_cache.miss"] == 1.0
    assert snap["timers"]["vlm_cache.get"]["count"] == 2


def test_v5_ttl_expired_get_counts_as_miss(tmp_path, tel) -> None:
    """V5:过期条目按未命中计数(与既有"读时删"行为一致)。"""
    c = VlmCache(str(tmp_path / "c.db"))
    _seed_age(c, "exp", 45)
    assert c.get("glm", "v2.1", "exp") is None
    assert tel.snapshot()["counters"]["vlm_cache.miss"] == 1.0


def test_v5_get_many_batch_hit_miss_and_dedup(tmp_path, tel) -> None:
    """V5 性能:批量读一次取回,命中/未命中逐键计数,重复键去重。"""
    c = VlmCache(str(tmp_path / "c.db"))
    c.put("glm", "v2.1", "k1", {"nsfw_prob": 0.1})
    c.put("glm", "v2.1", "k2", {"nsfw_prob": 0.2})
    tel.reset()
    hits = c.get_many(
        [
            ("glm", "v2.1", "k1"),
            ("glm", "v2.1", "k1"),  # 重复键:去重后只计一次命中
            ("glm", "v2.1", "missing"),
            ("other", "v2.1", "k1"),
        ]
    )
    assert set(hits) == {("glm", "v2.1", "k1")}
    assert hits[("glm", "v2.1", "k1")]["nsfw_prob"] == pytest.approx(0.1)
    snap = tel.snapshot()
    assert snap["counters"]["vlm_cache.hit"] == 1.0
    assert snap["counters"]["vlm_cache.miss"] == 2.0
    assert snap["timers"]["vlm_cache.get_many"]["count"] == 1


def test_v5_get_many_empty_input_is_noop(tmp_path, tel) -> None:
    """V5:空键列表零开销(不触碰数据库、不产生遥测)。"""
    c = VlmCache(str(tmp_path / "c.db"))
    assert c.get_many([]) == {}
    assert c.stats()["cache_rows"] == 0
    snap = tel.snapshot()
    assert "vlm_cache.get_many" not in snap["timers"]
    assert "vlm_cache.hit" not in snap["counters"]


def test_v5_get_many_chunks_large_batches(tmp_path) -> None:
    """V5 性能:超过单块上限(256 键)的批量读分块执行,结果完整。"""
    c = VlmCache(str(tmp_path / "c.db"))
    keys = [("glm", "v2.1", f"k{i:04d}") for i in range(600)]  # 3 块
    c.put_many([(*key, {"nsfw_prob": 0.5}) for key in keys])
    assert len(c.get_many(keys)) == 600
    assert len(c.get_many(keys + [("glm", "v2.1", "nope")])) == 600


def test_v5_get_many_treats_expired_as_miss_and_sweeps(tmp_path) -> None:
    """V5:批量读中过期键按未命中返回,并触发顺手清理。"""
    c = VlmCache(str(tmp_path / "c.db"))
    _seed_age(c, "exp", 45)
    c.put("glm", "v2.1", "fresh", {"nsfw_prob": 0.7})
    hits = c.get_many([("glm", "v2.1", "exp"), ("glm", "v2.1", "fresh")])
    assert set(hits) == {("glm", "v2.1", "fresh")}
    assert c.stats()["cache_rows"] == 1  # 过期键已被清掉


def test_v5_put_many_single_transaction_upsert(tmp_path) -> None:
    """V5 性能:批量写一次 executemany 落库,支持覆盖写(upsert 语义与 put 一致)。"""
    c = VlmCache(str(tmp_path / "c.db"))
    c.put_many(
        [
            ("glm", "v2.1", "k1", {"nsfw_prob": 0.1}),
            ("glm", "v2.1", "k2", {"nsfw_prob": 0.2}),
            ("other", "v2.1", "k1", {"nsfw_prob": 0.3}),
        ]
    )
    assert c.stats()["cache_rows"] == 3
    c.put_many([("glm", "v2.1", "k1", {"nsfw_prob": 0.9})])
    assert c.get("glm", "v2.1", "k1")["nsfw_prob"] == pytest.approx(0.9)
    assert c.stats()["cache_rows"] == 3


def test_v5_put_many_atomic_on_serialization_failure(tmp_path) -> None:
    """V5 健壮性:任一条目序列化失败 → 整批不落库(单事务原子,无部分写入)。"""
    c = VlmCache(str(tmp_path / "c.db"))
    with pytest.raises(TypeError):  # set 不可 JSON 序列化
        c.put_many(
            [
                ("glm", "v2.1", "ok", {"nsfw_prob": 0.1}),
                ("glm", "v2.1", "bad", {"unserializable": {1, 2}}),
            ]
        )
    assert c.stats()["cache_rows"] == 0


def test_v5_put_many_empty_input_is_noop(tmp_path) -> None:
    """V5:空批量不触碰数据库。"""
    c = VlmCache(str(tmp_path / "c.db"))
    c.put_many([])
    assert c.stats()["cache_rows"] == 0


def test_v5_ttl_opportunistic_sweep_once_per_day(tmp_path) -> None:
    """V5 性能:读到过期键时顺手批量清当日全部过期行(每日至多一次);
    未过期条目永不受影响(默认行为不变);当日已清后回到单键删除。"""
    c = VlmCache(str(tmp_path / "c.db"))
    _seed_age(c, "exp1", 45)
    _seed_age(c, "exp2", 40)
    _seed_age(c, "fresh", 1)

    # 读 exp1:过期 → 触发批量清,exp2(未被读取)也被顺手清掉
    assert c.get("glm", "v2.1", "exp1") is None
    assert c.stats()["cache_rows"] == 1
    assert c.get("glm", "v2.1", "fresh") is not None  # 新鲜条目无恙

    # 当日已批量清过:后写入的过期键走单键删除,新鲜条目仍不受影响
    _seed_age(c, "exp3", 50)
    assert c.get("glm", "v2.1", "exp3") is None
    assert c.stats()["cache_rows"] == 1


def test_v5_thread_safety_batch_interfaces(tmp_path) -> None:
    """V5:批量接口与单键接口并发混用安全(全操作仍持同一把锁)。"""
    c = VlmCache(str(tmp_path / "c.db"), daily_limit=100)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            keys = [("glm", "v2.1", f"k{i}-{j}") for j in range(5)]
            c.put_many([(*key, {"nsfw_prob": 0.1}) for key in keys])
            assert len(c.get_many(keys)) == 5
            c.put("glm", "v2.1", f"single{i}", {"nsfw_prob": 0.2})
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert c.stats()["cache_rows"] == 8 * 5 + 8
