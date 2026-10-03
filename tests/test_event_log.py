"""netsentinel.storage.event_log 单元测试(A213 · 事件账本)。

覆盖:建库幂等与重开、append/iter/query 语义、参数与负载校验(中文报错)、
append-only 触发器(数据库层拒绝 UPDATE/DELETE)、并发追加原子性
(seq 无洞不重——共享实例与跨实例两路)、损坏安全失败(中文报错,绝不把
损坏数据当正常事件吐出)、确定性序列化(同 payload 字节级稳定)。

全部离线,只写 tmp_path,零第三方依赖。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from netsentinel.storage import event_log as event_log_module
from netsentinel.storage.event_log import (
    REVIEW_EVENT_TYPES,
    Event,
    EventLog,
    EventLogError,
)

# ---------------------------------------------------------------------------
# 基础:append / iter / 重开
# ---------------------------------------------------------------------------


class TestBasics:
    def test_append_returns_monotonic_seq(self, tmp_path: Path):
        with EventLog(tmp_path / "ev.db") as log:
            assert log.append("entry_added", 1) == 1
            assert log.append("entry_approved", 1) == 2
            assert log.append("entry_annotated", 7) == 3
            assert log.latest_seq() == 3

    def test_event_fields_roundtrip(self, tmp_path: Path):
        payload = {"site_url": "http://例子.test/页", "note": "中文负载"}
        with EventLog(tmp_path / "ev.db") as log:
            seq = log.append(
                "entry_added", 42, actor="张三", payload=payload
            )
            events = log.iter_events()
        assert len(events) == 1
        ev = events[0]
        assert isinstance(ev, Event)
        assert ev.seq == seq == 1
        assert ev.ts  # 落盘时间非空(ISO 字符串)
        assert ev.event_type == "entry_added"
        assert ev.entry_id == 42
        assert ev.actor == "张三"
        assert ev.payload == payload  # dict 往返,中文无损

    def test_payload_none_defaults_empty_dict(self, tmp_path: Path):
        with EventLog(tmp_path / "ev.db") as log:
            log.append("entry_added", 1)
            assert log.iter_events()[0].payload == {}

    def test_iter_events_empty_ledger(self, tmp_path: Path):
        with EventLog(tmp_path / "ev.db") as log:
            assert log.iter_events() == []
            assert log.latest_seq() == 0

    def test_reopen_keeps_events_and_is_idempotent(self, tmp_path: Path):
        path = tmp_path / "ev.db"
        with EventLog(path) as log:
            for i in range(5):
                log.append("entry_added", i + 1)
        # 二次打开:幂等 DDL(IF NOT EXISTS),既有事件完整保留,seq 续排。
        with EventLog(path) as log2:
            assert len(log2.iter_events()) == 5
            assert log2.append("entry_approved", 1) == 6

    def test_review_event_types_complete(self):
        # 复核域五类事件类型常量齐备(spec 对齐)。
        assert set(REVIEW_EVENT_TYPES) == {
            "entry_added",
            "entry_approved",
            "entry_rejected",
            "entry_marked_submitted",
            "entry_annotated",
        }


# ---------------------------------------------------------------------------
# query:按 entry_id / 类型 / seq 范围
# ---------------------------------------------------------------------------


class TestQuery:
    @pytest.fixture()
    def seeded(self, tmp_path: Path) -> Path:
        path = tmp_path / "ev.db"
        with EventLog(path) as log:
            log.append("entry_added", 1)          # seq 1
            log.append("entry_added", 2)          # seq 2
            log.append("entry_approved", 1)       # seq 3
            log.append("entry_annotated", 2)      # seq 4
            log.append("entry_marked_submitted", 1)  # seq 5
        return path

    def test_query_by_entry_id(self, seeded: Path):
        with EventLog(seeded) as log:
            events = log.query(entry_id=1)
        assert [e.seq for e in events] == [1, 3, 5]
        assert all(e.entry_id == 1 for e in events)

    def test_query_by_event_type(self, seeded: Path):
        with EventLog(seeded) as log:
            events = log.query(event_type="entry_added")
        assert [e.seq for e in events] == [1, 2]

    def test_query_seq_range_inclusive(self, seeded: Path):
        with EventLog(seeded) as log:
            assert [e.seq for e in log.query(seq_from=2, seq_to=4)] == [2, 3, 4]
            assert [e.seq for e in log.query(seq_from=3)] == [3, 4, 5]
            assert [e.seq for e in log.query(seq_to=2)] == [1, 2]

    def test_query_combined_filters(self, seeded: Path):
        with EventLog(seeded) as log:
            events = log.query(
                entry_id=1, event_type="entry_approved", seq_from=1, seq_to=5
            )
        assert [e.seq for e in events] == [3]

    def test_query_bad_range_raises_chinese(self, seeded: Path):
        with EventLog(seeded) as log, pytest.raises(
            EventLogError, match="seq 范围非法"
        ):
            log.query(seq_from=4, seq_to=2)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"entry_id": "1"},
            {"entry_id": True},
            {"event_type": ""},
            {"event_type": 3},
            {"seq_from": "1"},
            {"seq_to": 1.5},
        ],
    )
    def test_query_invalid_filters_raise_chinese(self, seeded: Path, kwargs: dict):
        with EventLog(seeded) as log, pytest.raises(EventLogError):
            log.query(**kwargs)


# ---------------------------------------------------------------------------
# append 参数与负载校验
# ---------------------------------------------------------------------------


class TestAppendValidation:
    @pytest.mark.parametrize(
        "args,kwargs",
        [
            (("", 1), {}),
            (("entry_added", "1"), {}),
            (("entry_added", True), {}),
            (("entry_added", 1), {"actor": 3}),
            (("entry_added", 1), {"payload": ["list"]}),
        ],
    )
    def test_invalid_arguments_raise_chinese(self, tmp_path: Path, args, kwargs):
        with EventLog(tmp_path / "ev.db") as log, pytest.raises(EventLogError):
            log.append(*args, **kwargs)

    def test_unserializable_payload_raises_chinese(self, tmp_path: Path):
        with EventLog(tmp_path / "ev.db") as log, pytest.raises(
            EventLogError, match="序列化"
        ):
            log.append("entry_added", 1, payload={"bad": {1, 2}})

    def test_deterministic_payload_bytes(self, tmp_path: Path):
        # 同一 payload 不同键插入顺序 → 落盘字节一致(键排序 + 紧凑分隔)。
        p1 = tmp_path / "a.db"
        p2 = tmp_path / "b.db"
        with EventLog(p1) as a, EventLog(p2) as b:
            a.append("entry_added", 1, payload={"x": 1, "y": {"b": 2, "a": 3}})
            b.append("entry_added", 1, payload={"y": {"a": 3, "b": 2}, "x": 1})
        raw = []
        for p in (p1, p2):
            con = sqlite3.connect(p)
            raw.append(con.execute("SELECT payload_json FROM events").fetchone()[0])
            con.close()
        assert raw[0] == raw[1]
        assert raw[0] == '{"x":1,"y":{"a":3,"b":2}}'


# ---------------------------------------------------------------------------
# append-only:数据库层触发器
# ---------------------------------------------------------------------------


class TestAppendOnly:
    def test_update_blocked_by_trigger(self, tmp_path: Path):
        path = tmp_path / "ev.db"
        with EventLog(path) as log:
            log.append("entry_added", 1, actor="张三")
        # 绕过 EventLog,拿裸连接直改:数据库层拒绝(中文 ABORT 消息)。
        con = sqlite3.connect(path)
        try:
            with pytest.raises(sqlite3.Error, match="只允许追加"):
                con.execute("UPDATE events SET actor = '篡改者'")
        finally:
            con.close()

    def test_delete_blocked_by_trigger(self, tmp_path: Path):
        path = tmp_path / "ev.db"
        with EventLog(path) as log:
            log.append("entry_added", 1)
        con = sqlite3.connect(path)
        try:
            with pytest.raises(sqlite3.Error, match="只允许追加"):
                con.execute("DELETE FROM events WHERE seq = 1")
        finally:
            con.close()

    def test_api_surface_has_no_update_delete(self):
        # API 层:数据面方法只有 append / iter_events / query / latest_seq。
        public = {
            name
            for name in dir(EventLog)
            if not name.startswith("_") and callable(getattr(EventLog, name))
        }
        dataface = public - {"close", "__enter__", "__exit__"}
        assert not any(
            kw in name for name in dataface for kw in ("update", "delete", "drop")
        ), sorted(dataface)


# ---------------------------------------------------------------------------
# 并发原子性:seq 无洞不重
# ---------------------------------------------------------------------------


class TestConcurrentAppend:
    def test_shared_instance_no_gap_no_duplicate(self, tmp_path: Path):
        threads_n, per_thread = 8, 25
        with EventLog(tmp_path / "ev.db") as log:
            returned: list[int] = []
            lock = threading.Lock()
            barrier = threading.Barrier(threads_n)

            def worker(tid: int) -> None:
                barrier.wait()
                mine = [
                    log.append("entry_added", tid * 100 + i) for i in range(per_thread)
                ]
                with lock:
                    returned.extend(mine)

            threads = [
                threading.Thread(target=worker, args=(t,)) for t in range(threads_n)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            total = threads_n * per_thread
            assert len(returned) == total
            # seq 无洞不重:提交成功的事件恰好铺满 1..total。
            assert sorted(returned) == list(range(1, total + 1))
            assert log.latest_seq() == total
            assert len(log.iter_events()) == total

    def test_separate_instances_no_gap_no_duplicate(self, tmp_path: Path):
        # 跨连接(WAL + busy_timeout):先建库,再由各线程独立开连接并发追加。
        path = str(tmp_path / "ev.db")
        threads_n, per_thread = 4, 25
        with EventLog(path):
            pass
        returned: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(threads_n)
        errors: list[Exception] = []

        def worker(tid: int) -> None:
            try:
                barrier.wait()
                with EventLog(path) as log:
                    mine = [
                        log.append("entry_added", tid)
                        for _ in range(per_thread)
                    ]
                with lock:
                    returned.extend(mine)
            except Exception as exc:  # noqa: BLE001 - 线程内异常回传主线程断言
                with lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(t,)) for t in range(threads_n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        total = threads_n * per_thread
        assert sorted(returned) == list(range(1, total + 1))
        with EventLog(path) as log:
            assert log.latest_seq() == total


# ---------------------------------------------------------------------------
# 损坏安全失败(fail-safe 中文报错)
# ---------------------------------------------------------------------------


class TestCorruption:
    def test_open_garbage_file_raises_chinese(self, tmp_path: Path):
        path = tmp_path / "bad.db"
        path.write_bytes(b"this is definitely not a sqlite database" * 8)
        with pytest.raises(EventLogError, match="损坏"):
            EventLog(path)

    def test_corrupted_after_close_raises_on_read(self, tmp_path: Path):
        path = tmp_path / "ev.db"
        with EventLog(path) as log:
            log.append("entry_added", 1)
        # 覆写为垃圾字节:重开后的读取必须安全失败(中文),不吐半条事件。
        path.write_bytes(b"\x00garbage\xff" * 64)
        with pytest.raises(EventLogError, match="损坏"):
            EventLog(path).iter_events()

    def test_tampered_payload_json_raises_chinese(self, tmp_path: Path):
        path = tmp_path / "ev.db"
        with EventLog(path) as log:
            log.append("entry_added", 1, payload={"site_url": "http://x.test"})
        # 蓄意篡改:先拆触发器(模拟拿到 DBA 权限的攻击者)再改 payload。
        con = sqlite3.connect(path)
        con.execute("DROP TRIGGER events_no_update")
        con.execute("UPDATE events SET payload_json = '{oops'")
        con.commit()
        con.close()
        with EventLog(path) as log, pytest.raises(EventLogError, match="篡改"):
            log.iter_events()

    def test_tampered_payload_non_object_raises_chinese(self, tmp_path: Path):
        path = tmp_path / "ev.db"
        with EventLog(path) as log:
            log.append("entry_added", 1)
        con = sqlite3.connect(path)
        con.execute("DROP TRIGGER events_no_update")
        con.execute("UPDATE events SET payload_json = '[1,2]'")
        con.commit()
        con.close()
        with EventLog(path) as log, pytest.raises(EventLogError, match="篡改"):
            log.iter_events()


# ---------------------------------------------------------------------------
# 确定性:同一操作序列 → 同一事件流(monkeypatch 时间戳)
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_ops_same_event_stream(self, tmp_path: Path, monkeypatch):
        fixed = "2026-01-01T08:00:00+08:00"
        monkeypatch.setattr(event_log_module, "now_iso", lambda: fixed)

        def build(name: str) -> list[tuple]:
            with EventLog(tmp_path / name) as log:
                log.append("entry_added", 1, actor="张三", payload={"a": 1})
                log.append("entry_approved", 1, actor="李四", payload={"b": 2})
                return [
                    (e.seq, e.ts, e.event_type, e.entry_id, e.actor, e.payload)
                    for e in log.iter_events()
                ]

        assert build("a.db") == build("b.db")
