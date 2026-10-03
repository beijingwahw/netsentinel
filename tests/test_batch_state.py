# -*- coding: utf-8 -*-
"""A113 批次状态存储(BatchState / StateAdapter)单元测试:全部离线、零外呼。

覆盖:建批(鸭子对象/映射 items、自增 id、note 留痕、空批次、非列表/
缺 entry_id/重复 entry_id 中文 ValueError 且整批回滚)、标记(状态与中文
错误覆盖、再标记清空旧错、updated_at 刷新(假时钟)、非法状态/未知条目/
未知批次 ValueError、同 entry_id 跨批次独立)、resume 三态回收
(pending+running+rate_limited 回收;submitted/failed/skipped 终态不回;
全终态空;未知批次;entry_id 类型往返保持)、summary(六状态键恒存在、
明细排序、未知批次)、list_batches(最新在前、逐批计数、空库)、持久往返
(关闭重开数据完整)与 WAL/busy_timeout 连接策略、bound 适配器
(转发/隔离/未知批次/未知条目)、与真实 A112 run_batch 的集成
(fake executor 成功提交 2 条,state 全程记录,importorskip 保护)。

只写 tmp_path;sqlite 标准库;无网络、无真实门户、无 playwright。
"""
from __future__ import annotations

import pathlib
import sqlite3
from typing import Any

import pytest

import netsentinel.submit.batch_state as batch_state_mod
from netsentinel.contracts import Config, ExecutionResult
from netsentinel.submit.batch_state import (
    RESUME_STATUSES,
    VALID_STATUSES,
    BatchState,
    StateAdapter,
)

#: summary 顶层键的固定全集(总数 + 六状态 + 明细)
ALL_STATUS = ("pending", "running", "submitted", "failed", "skipped", "rate_limited")


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


class DuckItem:
    """鸭子待批量条目:对象属性风格(review_queue.Entry 同形)。"""

    def __init__(self, entry_id: Any, group_name: str = "") -> None:
        self.entry_id = entry_id
        self.group_name = group_name


def dict_items(n: int, portal: str = "12377") -> list[dict[str, Any]]:
    """映射风格条目(A110 ready_entries / A112 items 同形)。"""
    return [
        {"entry_id": i, "group_name": f"案件组{i}", "portal": portal}
        for i in range(1, n + 1)
    ]


def six_state_batch(st: BatchState) -> int:
    """建一个 6 条批次并各推进到一个状态(六状态各占一条)。"""
    bid = st.new_batch(dict_items(6))
    transitions = [
        (1, "submitted", ""),
        (2, "failed", "执行失败:HUMAN_GATE 超时"),
        (3, "skipped", "干跑/未真实提交"),
        (4, "rate_limited", "额度/间隔限制,可续批:每日上限"),
        (5, "running", ""),
    ]
    for entry_id, status, error in transitions:
        # 6 号保持 pending
        if error:
            st.mark(bid, entry_id, status, error=error)
        else:
            st.mark(bid, entry_id, status)
    return bid


@pytest.fixture()
def st(tmp_path: pathlib.Path) -> BatchState:
    """每个用例独立的 BatchState(独立 db 文件,用例结束即关闭)。"""
    state = BatchState(tmp_path / "batch_state.db")
    yield state
    state.close()


# ---------------------------------------------------------------------------
# 建批(new_batch)
# ---------------------------------------------------------------------------


def test_new_batch_returns_increasing_ids(tmp_path: pathlib.Path) -> None:
    """连续建批:批次号自增(1、2),list_batches 最新在前。"""
    with BatchState(tmp_path / "b.db") as state:
        bid1 = state.new_batch(dict_items(2))
        bid2 = state.new_batch(dict_items(1))
        assert bid1 == 1 and bid2 == 2
        assert [row["id"] for row in state.list_batches()] == [2, 1]


def test_new_batch_inserts_pending_duck_items(st: BatchState) -> None:
    """鸭子对象条目:逐条落库为 pending(对象属性风格)。"""
    bid = st.new_batch([DuckItem(7, "example.com"), DuckItem(8, "a.com.cn")])
    info = st.summary(bid)
    assert info["total"] == 2
    assert info["pending"] == 2
    assert {row["entry_id"] for row in info["items"]} == {7, 8}
    assert {row["group_name"] for row in info["items"]} == {"example.com", "a.com.cn"}
    assert all(row["status"] == "pending" for row in info["items"])


def test_new_batch_accepts_mapping_items(st: BatchState) -> None:
    """映射条目(dict,含 portal 等额外字段)同样可登记,额外字段被忽略。"""
    bid = st.new_batch(dict_items(3))
    assert st.summary(bid)["total"] == 3
    assert st.summary(bid)["pending"] == 3


def test_new_batch_empty_items(st: BatchState) -> None:
    """空 items:合法建批(total=0、resume 空),不抛错。"""
    bid = st.new_batch([], note="空批次")
    info = st.summary(bid)
    assert info["total"] == 0
    assert info["items"] == []
    assert st.resume(bid) == []


def test_new_batch_note_stored(st: BatchState) -> None:
    """note 备注(组名前缀)留痕在批次上。"""
    st.new_batch(dict_items(2), note="[组:example.com] 首批")
    row = st.list_batches()[0]
    assert row["note"] == "[组:example.com] 首批"
    assert row["total"] == 2


@pytest.mark.parametrize("bad", ["not-a-list", {"entry_id": 1}, 42, None])
def test_new_batch_not_a_list_raises(st: BatchState, bad: Any) -> None:
    """items 非列表(字符串/字典/整数/None)→ 中文 ValueError。"""
    with pytest.raises(ValueError, match="列表"):
        st.new_batch(bad)


def test_new_batch_missing_entry_id_rolls_back(st: BatchState) -> None:
    """某条缺 entry_id → 中文 ValueError 指明序号,且整批回滚不留批次。"""
    with pytest.raises(ValueError) as ei:
        st.new_batch([{"group_name": "组A"}, {"entry_id": 2, "group_name": "组B"}])
    assert "第 1 条" in str(ei.value) and "entry_id" in str(ei.value)
    assert st.list_batches() == []  # 半截批次不留库


def test_new_batch_duplicate_entry_id_raises(st: BatchState) -> None:
    """同批次 entry_id 重复 → 中文 ValueError,整批回滚。"""
    with pytest.raises(ValueError, match="重复"):
        st.new_batch(
            [{"entry_id": 1, "group_name": "组A"}, {"entry_id": 1, "group_name": "组A"}]
        )
    assert st.list_batches() == []


def test_same_entry_id_across_batches_independent(st: BatchState) -> None:
    """同一 entry_id 出现在不同批次互不干扰(主键含 batch_id)。"""
    bid1 = st.new_batch([{"entry_id": 1, "group_name": "组A"}])
    bid2 = st.new_batch([{"entry_id": 1, "group_name": "组A"}])
    st.mark(bid1, 1, "submitted")
    assert st.summary(bid1)["submitted"] == 1
    assert st.summary(bid2)["pending"] == 1


# ---------------------------------------------------------------------------
# 标记(mark)
# ---------------------------------------------------------------------------


def test_mark_updates_status_and_error(st: BatchState) -> None:
    """mark 推进状态并写入中文错误原因。"""
    bid = st.new_batch([{"entry_id": 3, "group_name": "组C"}])
    st.mark(bid, 3, "running")
    st.mark(bid, 3, "failed", error="执行失败:验证码输入超时")
    row = st.summary(bid)["items"][0]
    assert row["status"] == "failed"
    assert row["error"] == "执行失败:验证码输入超时"


def test_mark_remark_clears_previous_error(st: BatchState) -> None:
    """再次 mark 不带 error:旧错误清空(最新状态即真相)。"""
    bid = st.new_batch([{"entry_id": 1, "group_name": "组A"}])
    st.mark(bid, 1, "rate_limited", error="额度/间隔限制,可续批:每日上限")
    st.mark(bid, 1, "submitted")
    row = st.summary(bid)["items"][0]
    assert row["status"] == "submitted" and row["error"] == ""


def test_mark_refreshes_updated_at(
    st: BatchState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每次 mark 刷新 updated_at(假时钟注入,免真实等待)。"""
    ticks = iter(
        [
            "2026-10-02T08:00:00+08:00",  # 建批(created_at / 条目 updated_at)
            "2026-10-02T09:30:00+08:00",  # 第一次 mark
            "2026-10-02T11:00:00+08:00",  # 第二次 mark
        ]
    )
    monkeypatch.setattr(batch_state_mod, "now_iso", lambda: next(ticks))
    bid = st.new_batch([{"entry_id": 1, "group_name": "组A"}])
    assert st.summary(bid)["items"][0]["updated_at"] == "2026-10-02T08:00:00+08:00"
    st.mark(bid, 1, "running")
    st.mark(bid, 1, "submitted")
    assert st.summary(bid)["items"][0]["updated_at"] == "2026-10-02T11:00:00+08:00"


@pytest.mark.parametrize("bad_status", ["done", "", "PENDING", "Submitted", None, 5])
def test_mark_invalid_status_raises(st: BatchState, bad_status: Any) -> None:
    """非法状态(含非字符串与非规范大小写)→ 中文 ValueError,且状态不变。"""
    bid = st.new_batch([{"entry_id": 1, "group_name": "组A"}])
    with pytest.raises(ValueError) as ei:
        st.mark(bid, 1, bad_status)
    assert "非法条目状态" in str(ei.value) and "pending" in str(ei.value)
    assert st.summary(bid)["items"][0]["status"] == "pending"  # 原状态未动


def test_mark_unknown_entry_raises(st: BatchState) -> None:
    """条目不在该批次 → 中文 ValueError。"""
    bid = st.new_batch([{"entry_id": 1, "group_name": "组A"}])
    with pytest.raises(ValueError, match="不在批次"):
        st.mark(bid, 99, "running")


def test_mark_unknown_batch_raises(st: BatchState) -> None:
    """批次不存在 → 中文 ValueError。"""
    st.new_batch([{"entry_id": 1, "group_name": "组A"}])
    with pytest.raises(ValueError, match="不存在"):
        st.mark(999, 1, "running")


# ---------------------------------------------------------------------------
# 续批(resume)
# ---------------------------------------------------------------------------


def test_resume_recycles_pending_running_rate_limited(st: BatchState) -> None:
    """三态回收:pending(未开始)/ running(中断未竟)/ rate_limited(挂起)。"""
    bid = six_state_batch(st)
    undone = st.resume(bid)
    assert sorted(row["entry_id"] for row in undone) == [4, 5, 6]
    by_id = {row["entry_id"]: row for row in undone}
    assert by_id[4]["status"] == "rate_limited"
    assert by_id[5]["status"] == "running"
    assert by_id[6]["status"] == "pending"


def test_resume_excludes_terminal_statuses(st: BatchState) -> None:
    """终态 submitted / failed / skipped 不回收(resume 是白名单三态)。"""
    bid = six_state_batch(st)
    statuses = {row["status"] for row in st.resume(bid)}
    assert statuses <= RESUME_STATUSES
    assert {"submitted", "failed", "skipped"} & statuses == set()


def test_resume_all_terminal_returns_empty(st: BatchState) -> None:
    """全部条目终态 → resume 为空(批次已完成,无需续批)。"""
    bid = st.new_batch(dict_items(2))
    st.mark(bid, 1, "submitted")
    st.mark(bid, 2, "failed", error="执行失败:表单校验不过")
    assert st.resume(bid) == []


def test_resume_unknown_batch_raises(st: BatchState) -> None:
    """未知批次 resume → 中文 ValueError(比静默空表更早暴露批次号写错)。"""
    with pytest.raises(ValueError, match="不存在"):
        st.resume(42)


def test_resume_item_fields_complete(st: BatchState) -> None:
    """resume 条目字典键集固定(entry_id/group_name/status/error/updated_at)。"""
    bid = st.new_batch([{"entry_id": 11, "group_name": "组K"}])
    rows = st.resume(bid)
    assert len(rows) == 1
    assert set(rows[0]) == {
        "entry_id",
        "group_name",
        "status",
        "error",
        "updated_at",
    }
    assert rows[0]["group_name"] == "组K"


def test_resume_preserves_entry_id_type(st: BatchState) -> None:
    """entry_id 类型往返保持:整数仍是 int、字符串仍是 str。"""
    bid = st.new_batch([{"entry_id": 1, "group_name": "a"}, {"entry_id": "x1", "group_name": "b"}])
    undone = st.resume(bid)
    types = {row["group_name"]: type(row["entry_id"]).__name__ for row in undone}
    assert types == {"a": "int", "b": "str"}


# ---------------------------------------------------------------------------
# 汇总(summary / list_batches)
# ---------------------------------------------------------------------------


def test_summary_counts_and_items(st: BatchState) -> None:
    """summary:总数、六状态计数、按 entry_id 升序的逐条明细。"""
    bid = six_state_batch(st)
    info = st.summary(bid)
    assert info["total"] == 6
    for status in ALL_STATUS:
        assert info[status] == 1, status
    assert [row["entry_id"] for row in info["items"]] == [1, 2, 3, 4, 5, 6]
    assert info["items"][1]["error"] == "执行失败:HUMAN_GATE 超时"


def test_summary_keys_fixed_and_zero_when_absent(st: BatchState) -> None:
    """六状态键恒存在(未出现计 0),顶层键集固定(total+六状态+items)。"""
    bid = st.new_batch([{"entry_id": 1, "group_name": "组A"}])
    st.mark(bid, 1, "submitted")
    info = st.summary(bid)
    assert set(info) == {"total", *ALL_STATUS, "items"}
    assert info["pending"] == 0 and info["failed"] == 0
    assert info["submitted"] == 1


def test_summary_unknown_batch_raises(st: BatchState) -> None:
    """未知批次 summary → 中文 ValueError。"""
    with pytest.raises(ValueError, match="不存在"):
        st.summary(7)


def test_list_batches_newest_first_with_counts(st: BatchState) -> None:
    """list_batches 简版:最新在前,逐批 id/created_at/note/各状态计数。"""
    bid1 = st.new_batch(dict_items(2), note="第一批")
    st.mark(bid1, 1, "submitted")
    st.mark(bid1, 2, "failed", error="执行失败:网络异常")
    bid2 = st.new_batch(dict_items(1), note="第二批")
    st.mark(bid2, 1, "running")
    rows = st.list_batches()
    assert [row["id"] for row in rows] == [bid2, bid1]
    first, second = rows
    assert first["note"] == "第二批" and first["running"] == 1 and first["total"] == 1
    assert second["note"] == "第一批"
    assert second["submitted"] == 1 and second["failed"] == 1
    assert second["pending"] == 0 and second["total"] == 2
    assert "T" in second["created_at"]  # ISO 时间戳非空


def test_list_batches_empty_db(st: BatchState) -> None:
    """空库 list_batches → 空列表。"""
    assert st.list_batches() == []


# ---------------------------------------------------------------------------
# 持久化(WAL)与连接策略
# ---------------------------------------------------------------------------


def test_persistence_roundtrip(tmp_path: pathlib.Path) -> None:
    """关闭重开同一 db:批次、条目、状态、错误、resume 全部完整往返。"""
    db = tmp_path / "batch_state.db"
    with BatchState(db) as first:
        bid = first.new_batch(dict_items(3), note="[组:example.com] 首批")
        first.mark(bid, 1, "submitted")
        first.mark(bid, 2, "failed", error="执行失败:页面 500")
    with BatchState(db) as second:
        info = second.summary(bid)
        assert info["total"] == 3
        assert info["submitted"] == 1 and info["failed"] == 1 and info["pending"] == 1
        assert info["items"][1]["error"] == "执行失败:页面 500"
        undone = second.resume(bid)
        assert [row["entry_id"] for row in undone] == [3]
        rows = second.list_batches()
        assert rows[0]["note"] == "[组:example.com] 首批" and rows[0]["total"] == 3


def test_wal_and_busy_timeout_active(st: BatchState) -> None:
    """连接策略与全仓一致:journal_mode=WAL、busy_timeout=5000。"""
    mode = st._conn.execute("PRAGMA journal_mode").fetchone()[0]
    timeout = st._conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert mode.lower() == "wal"
    assert timeout == 5000


def test_wal_mode_persists_in_db_file(tmp_path: pathlib.Path) -> None:
    """WAL 是库文件属性:独立新连接读到同一模式(跨进程可并发读写)。"""
    db = tmp_path / "batch_state.db"
    BatchState(db).close()
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# bound 适配器(A112 鸭子接口)
# ---------------------------------------------------------------------------


def test_bound_adapter_forwards_mark_and_summary(st: BatchState) -> None:
    """适配器 mark/summary 原样转发到绑定批次(不带 batch_id 的鸭子接口)。"""
    bid = st.new_batch(dict_items(2))
    adapter = st.bound(bid)
    assert isinstance(adapter, StateAdapter)
    assert adapter.batch_id == bid
    adapter.mark(1, "running")
    adapter.mark(1, "submitted")
    assert adapter.summary()["submitted"] == 1
    assert st.summary(bid)["submitted"] == 1  # 宿主视角一致


def test_bound_adapter_isolated_between_batches(st: BatchState) -> None:
    """适配器只写绑定批次:另一批次不受影响。"""
    bid1 = st.new_batch([{"entry_id": 1, "group_name": "组A"}])
    bid2 = st.new_batch([{"entry_id": 1, "group_name": "组A"}])
    st.bound(bid1).mark(1, "submitted")
    assert st.summary(bid2)["pending"] == 1


def test_bound_unknown_batch_raises(st: BatchState) -> None:
    """绑定未知批次 → 中文 ValueError(绑定即校验,错误尽早暴露)。"""
    with pytest.raises(ValueError, match="不存在"):
        st.bound(123)


def test_bound_adapter_mark_unknown_entry_raises(st: BatchState) -> None:
    """经适配器标记未知条目同样抛中文 ValueError。"""
    bid = st.new_batch([{"entry_id": 1, "group_name": "组A"}])
    with pytest.raises(ValueError, match="不在批次"):
        st.bound(bid).mark(2, "running")


def test_bound_adapter_invalid_status_raises(st: BatchState) -> None:
    """经适配器传非法状态同样抛中文 ValueError。"""
    bid = st.new_batch([{"entry_id": 1, "group_name": "组A"}])
    with pytest.raises(ValueError, match="非法条目状态"):
        st.bound(bid).mark(1, "done")


# ---------------------------------------------------------------------------
# 与真实 A112 run_batch 的集成(importorskip 保护)
# ---------------------------------------------------------------------------


class FakeRateOk:
    """频控器替身:恒放行,record 计数。"""

    def __init__(self) -> None:
        self.records = 0

    def can_submit(self) -> tuple[bool, str]:
        return (True, "")

    def record(self) -> None:
        self.records += 1


def test_integration_run_batch_records_two_submitted(
    tmp_path: pathlib.Path,
) -> None:
    """真实 A112 run_batch + BatchState.bound:fake executor 成功提交 2 条,
    state 全程记录,summary 断言 submitted=2(A112 未就位则跳过)。"""
    batch_submit = pytest.importorskip("netsentinel.submit.batch_submit")
    cfg = Config(
        data_dir=str(tmp_path / "data"),
        audit_path=str(tmp_path / "audit" / "audit.jsonl"),
        dry_run_default=True,
        batch_max_items=20,
        batch_item_interval_s=90,
        submit_min_interval_s=60,
        submit_max_per_day=5,
    )
    items = dict_items(2)
    rate = FakeRateOk()
    seen_flags: list[dict[str, Any]] = []

    def fake_executor(
        plan: Any, cfg_: Config, *, auto_confirm: bool, dry_run: bool, **_: Any
    ) -> ExecutionResult:
        seen_flags.append({"auto_confirm": auto_confirm, "dry_run": dry_run})
        return ExecutionResult(portal="12377", ok=True, submitted=True)

    with BatchState(tmp_path / "batch_state.db") as state:
        bid = state.new_batch(items, note="[组:example.com] 集成")
        result = batch_submit.run_batch(
            items,
            cfg,
            executor=fake_executor,
            plan_12377=lambda entry, c: {"entry_id": entry["entry_id"]},
            plan_shdf=lambda entry, c: {"entry_id": entry["entry_id"]},
            rate=rate,
            state=state.bound(bid),
            dry_run=True,
        )
        assert result["submitted"] == 2 and result["failed"] == 0

        info = state.summary(bid)
        assert info["total"] == 2
        assert info["submitted"] == 2
        assert info["running"] == 0 and info["pending"] == 0
        assert all(row["status"] == "submitted" for row in info["items"])
        assert state.resume(bid) == []  # 全终态,无需续批
    assert rate.records == 2
    # 红线 24:执行器恒 auto_confirm=False
    assert seen_flags and all(f["auto_confirm"] is False for f in seen_flags)
