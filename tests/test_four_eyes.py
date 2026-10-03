"""netsentinel.decision.four_eyes 单元测试(A50 · 四眼原则双人复核)。

覆盖契约与任务书要求的全部场景:
  * required=False 单人直通(底层 approve + 返回 reviewers);
  * required=True 状态机 none → awaiting_second → approved(单人绝不放行);
  * 同一审核人不能二次确认(中文拦截);
  * 两人齐后底层 Entry.status == "approved"(真实 ReviewQueue/Entry 集成,
    importorskip 保护);
  * reject 终态:approvals 留痕保留、条目状态已变、再 approve 抛底层错误;
  * status 各态(none / awaiting_second / approved / required 标志 / 不存在);
  * 空白审核人姓名拦截;
  * list / get / reject / mark_submitted / add 直通委托。

A222 追加(事件账本透传,默认关闭):
  * 双写事件序列与 actor 署名 / 双人语义字段(payload reviewers);
  * 先账本后状态崩溃仿真(entry_approved 落盘后崩溃 → 状态未生效;
    four_eyes_awaiting_second 落盘后崩溃 → 留痕未写);
  * 默认关(event_log=None)与启用账本两路跑同一操作序列,
    entries / approvals 快照逐行一致;
  * 同人拒二次在校验阶段拦截,零新事件(回归)。

全部离线,只写 tmp_path,零外呼、零真实门户、零真实 VLM 调用。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from netsentinel import telemetry  # noqa: E402

# 与真实 A10 队列的集成受 importorskip 保护(缺失时跳过而非报错)。
pytest.importorskip("netsentinel.decision.review_queue")
from netsentinel.contracts import SiteReport, Verdict  # noqa: E402
from netsentinel.decision import review_queue as review_queue_module  # noqa: E402
from netsentinel.decision.review_queue import Entry, ReviewQueue  # noqa: E402

pytest.importorskip("netsentinel.decision.four_eyes")
from netsentinel.decision import four_eyes as four_eyes_module  # noqa: E402
from netsentinel.decision.four_eyes import FourEyesQueue  # noqa: E402

pytest.importorskip("netsentinel.storage.event_log")
from netsentinel.storage.event_log import EventLog  # noqa: E402


def make_report(
    url: str = "http://four-eyes.test/page", verdict: Verdict = Verdict.NSFW
) -> SiteReport:
    """构造最小 SiteReport(不依赖兄弟判定模块)。"""
    return SiteReport(
        site_url=url, verdict=verdict, needs_review=(verdict != Verdict.CLEAN)
    )


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """嵌套路径同时验证自动建目录、entries 与 approvals 共库。"""
    return str(tmp_path / "queues" / "four_eyes.db")


def raw_rows(db_path: str, sql: str) -> list[tuple]:
    """直查 SQLite 并确保关闭连接(测试专用小工具)。"""
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


@pytest.fixture()
def open_queue(db_path: str) -> Iterator[Callable[[bool], FourEyesQueue]]:
    """队列工厂:按需开 required=True/False,测试结束统一关闭。"""
    opened: list[FourEyesQueue] = []

    def _make(required: bool) -> FourEyesQueue:
        q = FourEyesQueue(db_path, required)
        opened.append(q)
        return q

    yield _make
    for q in opened:
        q.close()


def _add(q: FourEyesQueue, url: str = "http://four-eyes.test/a") -> int:
    return q.add(make_report(url))


# ---------------------------------------------------------------------------
# required=False:单人直通
# ---------------------------------------------------------------------------


class TestRequiredFalse:
    def test_single_approve_passes_through(self, open_queue) -> None:
        q = open_queue(required=False)
        entry_id = _add(q)
        res = q.approve(entry_id, "张三")
        assert res == {"state": "approved", "reviewers": ["张三"]}
        assert q.get(entry_id) is not None
        assert q.get(entry_id).status == "approved"
        # 四眼未启用:状态面板显示已确认,审核人留痕可见。
        assert q.status(entry_id) == {
            "state": "approved",
            "reviewers": ["张三"],
            "required": False,
        }

    def test_false_mode_passes_through_underlying_errors(
        self, open_queue
    ) -> None:
        q = open_queue(required=False)
        with pytest.raises(ValueError, match="不存在"):
            q.approve(999, "张三")
        entry_id = _add(q)
        q.approve(entry_id, "张三")
        # 已确认条目再确认 → 底层状态机中文错误透传。
        with pytest.raises(ValueError, match="只有"):
            q.approve(entry_id, "李四")

    def test_blank_reviewer_rejected_even_when_disabled(self, open_queue) -> None:
        q = open_queue(required=False)
        entry_id = _add(q)
        with pytest.raises(ValueError, match="审核人"):
            q.approve(entry_id, "")
        with pytest.raises(ValueError, match="审核人"):
            q.approve(entry_id, "   ")
        assert q.get(entry_id).status == "pending"


# ---------------------------------------------------------------------------
# required=True:状态机 none → awaiting_second → approved
# ---------------------------------------------------------------------------


class TestRequiredStateMachine:
    def test_full_state_machine(self, open_queue) -> None:
        q = open_queue(required=True)
        entry_id = _add(q)

        # 初始:none
        assert q.status(entry_id) == {"state": "none", "reviewers": [], "required": True}

        # 第一人:留痕但不放行(红线)
        first = q.approve(entry_id, "张三")
        assert first == {
            "state": "awaiting_second",
            "first": "张三",
            "note": "等待第二审核人确认",
        }
        assert q.get(entry_id).status == "pending"
        assert q.status(entry_id) == {
            "state": "awaiting_second",
            "reviewers": ["张三"],
            "required": True,
        }

        # 第二人:两人齐 → 底层 approved,顺序保留
        second = q.approve(entry_id, "李四")
        assert second == {"state": "approved", "reviewers": ["张三", "李四"]}
        assert q.get(entry_id).status == "approved"
        assert q.status(entry_id) == {
            "state": "approved",
            "reviewers": ["张三", "李四"],
            "required": True,
        }

    def test_single_person_never_approves_when_required(self, open_queue) -> None:
        """红线:required=True 时,单人无论怎么点都到不了 approved。"""
        q = open_queue(required=True)
        entry_id = _add(q)
        q.approve(entry_id, "张三")
        assert q.get(entry_id).status == "pending"
        with pytest.raises(ValueError, match="同一审核人不能二次确认"):
            q.approve(entry_id, "张三")
        assert q.get(entry_id).status == "pending"
        assert q.list(status="approved") == []

    def test_second_approver_explicit_method(self, open_queue) -> None:
        q = open_queue(required=True)
        entry_id = _add(q)
        q.approve(entry_id, "张三")
        res = q.second_approver(entry_id, "李四")
        assert res == {"state": "approved", "reviewers": ["张三", "李四"]}
        assert q.get(entry_id).status == "approved"

    def test_second_approver_requires_existing_first(self, open_queue) -> None:
        q = open_queue(required=True)
        entry_id = _add(q)
        with pytest.raises(ValueError, match="第一审核人"):
            q.second_approver(entry_id, "李四")
        assert q.get(entry_id).status == "pending"
        assert q.status(entry_id)["reviewers"] == []

    def test_reviewer_whitespace_stripped(self, open_queue) -> None:
        q = open_queue(required=True)
        entry_id = _add(q)
        res = q.approve(entry_id, "  张三  ")
        assert res["first"] == "张三"
        assert q.status(entry_id)["reviewers"] == ["张三"]

    def test_missing_entry_raises_chinese_error(self, open_queue) -> None:
        q = open_queue(required=True)
        with pytest.raises(ValueError, match="不存在"):
            q.approve(404, "张三")
        with pytest.raises(ValueError, match="不存在"):
            q.second_approver(404, "李四")
        with pytest.raises(ValueError, match="不存在"):
            q.status(404)


# ---------------------------------------------------------------------------
# 同人拦截
# ---------------------------------------------------------------------------


class TestSameReviewerBlocked:
    def test_same_reviewer_cannot_confirm_twice(self, open_queue) -> None:
        q = open_queue(required=True)
        entry_id = _add(q)
        q.approve(entry_id, "张三")
        with pytest.raises(ValueError, match="同一审核人不能二次确认:张三"):
            q.approve(entry_id, "张三")
        # 状态不受影响:仍在等待第二人。
        assert q.get(entry_id).status == "pending"
        assert q.status(entry_id)["state"] == "awaiting_second"
        assert q.status(entry_id)["reviewers"] == ["张三"]

    def test_same_reviewer_via_second_approver(self, open_queue) -> None:
        q = open_queue(required=True)
        entry_id = _add(q)
        q.approve(entry_id, "张三")
        with pytest.raises(ValueError, match="同一审核人不能二次确认"):
            q.second_approver(entry_id, "张三")
        assert q.get(entry_id).status == "pending"

    def test_blank_reviewer_rejected(self, open_queue) -> None:
        q = open_queue(required=True)
        entry_id = _add(q)
        for bad in ("", "   ", "\t\n"):
            with pytest.raises(ValueError, match="审核人不能为空"):
                q.approve(entry_id, bad)
        with pytest.raises(ValueError, match="审核人不能为空"):
            q.second_approver(entry_id, "")
        # 条目与留痕均未受影响。
        assert q.get(entry_id).status == "pending"
        assert q.status(entry_id) == {"state": "none", "reviewers": [], "required": True}


# ---------------------------------------------------------------------------
# 驳回终态:approvals 保留、状态已变、再确认抛底层错误
# ---------------------------------------------------------------------------


class TestRejectSemantics:
    def test_reject_keeps_approvals_and_blocks_approve(self, open_queue, db_path) -> None:
        q = open_queue(required=True)
        entry_id = _add(q)
        q.approve(entry_id, "张三")

        rejected = q.reject(entry_id, note="证据不足,不予举报")
        assert isinstance(rejected, Entry)
        assert rejected.status == "rejected"

        # 条目状态已变;approvals 留痕保留(审计)。
        assert q.get(entry_id).status == "rejected"
        st = q.status(entry_id)
        assert st["state"] == "none"  # 驳回终态:四眼流程终止
        assert st["reviewers"] == ["张三"]  # 留痕仍在

        # 直查 SQLite:approvals 行确实保留。
        rows = raw_rows(db_path, "SELECT entry_id, reviewer FROM approvals ORDER BY rowid")
        assert rows == [(entry_id, "张三")]

        # 驳回后再 approve(无论何人)→ 底层中文错误。
        with pytest.raises(ValueError, match="已驳回"):
            q.approve(entry_id, "李四")
        with pytest.raises(ValueError, match="已驳回"):
            q.approve(entry_id, "张三")

    def test_reject_before_any_approval(self, open_queue) -> None:
        q = open_queue(required=True)
        entry_id = _add(q)
        q.reject(entry_id)
        assert q.status(entry_id) == {"state": "none", "reviewers": [], "required": True}
        with pytest.raises(ValueError, match="只有"):
            q.approve(entry_id, "张三")


# ---------------------------------------------------------------------------
# status 各态与标志
# ---------------------------------------------------------------------------


class TestStatusStates:
    def test_states_and_required_flag(self, open_queue) -> None:
        q_true = open_queue(required=True)
        entry_id = _add(q_true)
        assert q_true.status(entry_id)["required"] is True
        q_true.approve(entry_id, "张三")
        assert q_true.status(entry_id)["state"] == "awaiting_second"
        q_true.approve(entry_id, "李四")
        assert q_true.status(entry_id)["state"] == "approved"

        # 另一队列实例(同库,required=False)的 status 如实回报自身配置。
        q_false = open_queue(required=False)
        other = _add(q_false, "http://four-eyes.test/b")
        assert q_false.status(other) == {
            "state": "none",
            "reviewers": [],
            "required": False,
        }

    def test_status_after_mark_submitted(self, open_queue) -> None:
        q = open_queue(required=True)
        entry_id = _add(q)
        q.approve(entry_id, "张三")
        q.approve(entry_id, "李四")
        submitted = q.mark_submitted(entry_id)
        assert submitted.status == "submitted"
        # 已提交条目:四眼流程已完成,审核人留痕不变。
        st = q.status(entry_id)
        assert st == {"state": "approved", "reviewers": ["张三", "李四"], "required": True}

    def test_status_missing_entry(self, open_queue) -> None:
        q = open_queue(required=True)
        with pytest.raises(ValueError, match="不存在"):
            q.status(123457)


# ---------------------------------------------------------------------------
# 与真实 ReviewQueue / Entry 集成(importorskip 保护)
# ---------------------------------------------------------------------------


class TestIntegrationWithReviewQueue:
    def test_underlying_queue_sees_two_eye_approval(
        self, open_queue, db_path: str
    ) -> None:
        """A10 队列直接写入的条目,经四眼两人确认后,底层视角即 approved。"""
        with ReviewQueue(db_path) as rq:
            entry_id = rq.add(make_report("http://four-eyes.test/rq"), evidence_zip="e.zip")
            assert rq.get(entry_id).status == "pending"

        q = open_queue(required=True)
        assert q.approve(entry_id, "张三")["state"] == "awaiting_second"
        # 半程:底层(以及新开的真实 ReviewQueue)看到的仍是 pending。
        with ReviewQueue(db_path) as rq2:
            assert rq2.get(entry_id).status == "pending"
        res = q.approve(entry_id, "李四")
        assert res == {"state": "approved", "reviewers": ["张三", "李四"]}

        with ReviewQueue(db_path) as rq3:
            entry = rq3.get(entry_id)
            assert isinstance(entry, Entry)
            assert entry.status == "approved"
            assert entry.site_url == "http://four-eyes.test/rq"
            assert entry.evidence_zip == "e.zip"
        assert q.get(entry_id).status == "approved"

    def test_shared_db_has_both_tables(self, open_queue, db_path: str) -> None:
        q = open_queue(required=True)
        _add(q)
        names = {
            row[0]
            for row in raw_rows(
                db_path, "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"entries", "approvals"} <= names

    def test_summary_and_list_delegate(self, open_queue) -> None:
        q = open_queue(required=True)
        a = _add(q, "http://four-eyes.test/s1")
        _add(q, "http://four-eyes.test/s2")
        entries = q.list()
        assert len(entries) == 2
        assert all(isinstance(e, Entry) for e in entries)
        assert [e.id for e in entries] == [a, a + 1]
        assert q.summary()["pending"] == 2
        q.approve(a, "张三")
        # 第一人确认不改变底层状态:两个条目都仍是 pending(红线)。
        assert [e.id for e in q.list(status="pending")] == [a, a + 1]
        assert q.summary()["pending"] == 2
        q.approve(a, "李四")
        assert [e.id for e in q.list(status="pending")] == [a + 1]
        assert q.summary() == {
            "pending": 1,
            "approved": 1,
            "rejected": 0,
            "submitted": 0,
        }


# ---------------------------------------------------------------------------
# 直通委托的错误语义
# ---------------------------------------------------------------------------


class TestDelegationErrors:
    def test_mark_submitted_passthrough_error(self, open_queue) -> None:
        q = open_queue(required=True)
        entry_id = _add(q)
        q.approve(entry_id, "张三")
        with pytest.raises(ValueError, match="只有"):
            q.mark_submitted(entry_id)  # pending 不能直接提交(人工门红线)

    def test_reject_twice_passthrough_error(self, open_queue) -> None:
        q = open_queue(required=False)
        entry_id = _add(q)
        q.reject(entry_id)
        with pytest.raises(ValueError, match="已驳回"):
            q.reject(entry_id)


# ---------------------------------------------------------------------------
# V5 升级:approvals 连接与底层同一 WAL + busy_timeout 策略;
# 双人确认关键节点 telemetry(four_eyes.awaiting / four_eyes.approved)
# ---------------------------------------------------------------------------


def test_v5_shared_db_wal_and_busy_timeout(open_queue, db_path: str) -> None:
    """共库连接策略:库为 WAL;底层与 approvals 两条连接都忙等 5000ms。"""
    q = open_queue(required=True)
    entry_id = _add(q)

    assert q._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert q._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert q._queue._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000

    # WAL 对后续任意连接生效(库属性),entries/approvals 两表共库共存。
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"entries", "approvals"} <= tables
        assert conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE entry_id = ?", (entry_id,)
        ).fetchone()[0] == 0  # 上面 _add 未触发确认,无留痕
    finally:
        conn.close()


def test_v5_telemetry_awaiting_then_approved(open_queue) -> None:
    """required=True:第一人 awaiting、两眼齐 approved,各计一次。"""
    telemetry.reset()
    q = open_queue(required=True)
    entry_id = _add(q)

    assert telemetry.snapshot()["counters"].get("four_eyes.awaiting") is None
    q.approve(entry_id, "张三")
    assert telemetry.snapshot()["counters"]["four_eyes.awaiting"] == 1
    assert telemetry.snapshot()["counters"].get("four_eyes.approved") is None

    q.approve(entry_id, "李四")
    snap = telemetry.snapshot()
    assert snap["counters"]["four_eyes.awaiting"] == 1  # 不重复计
    assert snap["counters"]["four_eyes.approved"] == 1


def test_v5_telemetry_single_mode_counts_approved(open_queue) -> None:
    """required=False:单人直通确认计 four_eyes.approved(无 awaiting)。"""
    telemetry.reset()
    q = open_queue(required=False)
    entry_id = _add(q)
    q.approve(entry_id, "张三")

    snap = telemetry.snapshot()
    assert snap["counters"]["four_eyes.approved"] == 1
    assert "four_eyes.awaiting" not in snap["counters"]


def test_v5_telemetry_failed_confirmations_not_counted(open_queue) -> None:
    """失败路径不计数:同人重复确认、驳回后再确认的 ValueError 不产生指标。"""
    telemetry.reset()
    q = open_queue(required=True)
    entry_id = _add(q)
    q.approve(entry_id, "张三")

    with pytest.raises(ValueError, match="同一审核人"):
        q.approve(entry_id, "张三")
    q.reject(entry_id, note="证据不足")
    with pytest.raises(ValueError, match="已驳回"):
        q.approve(entry_id, "李四")

    snap = telemetry.snapshot()
    assert snap["counters"]["four_eyes.awaiting"] == 1  # 仅第一人那一次
    assert "four_eyes.approved" not in snap["counters"]


# ---------------------------------------------------------------------------
# A222:事件账本透传 + 四眼域事件双写(默认关闭;先账本后状态)
# ---------------------------------------------------------------------------

#: 快照一致性测试的固定时钟(两路跑同一序列,时间戳不引入差异)。
A222_FIXED_TS = "2026-01-01T09:00:00+08:00"


class TestA222EventLedgerDualWrite:
    """启用 event_log 后:透传底层 entry_* 事件 + 四眼域事件,先账本后状态。"""

    def _open(
        self, tmp_path: Path, name: str, required: bool = True
    ) -> tuple[EventLog, FourEyesQueue, str, str]:
        db = str(tmp_path / f"{name}.db")
        edb = str(tmp_path / f"{name}_events.db")
        log = EventLog(edb)
        q = FourEyesQueue(db, required, event_log=log)
        return log, q, db, edb

    @staticmethod
    def _events(edb: str) -> list:
        with EventLog(edb) as log:
            return log.iter_events()

    def test_pair_flow_events_actor_and_semantics(self, tmp_path: Path) -> None:
        """双人齐全流程:事件序列 + actor 署名 + payload 双人语义字段。"""
        log, q, db, edb = self._open(tmp_path, "pair")
        entry_id = q.add(make_report("http://dual.test"))
        q.approve(entry_id, "张三")
        res = q.approve(entry_id, "李四")
        q.close()
        log.close()

        assert res == {"state": "approved", "reviewers": ["张三", "李四"]}
        events = self._events(edb)
        assert [(e.event_type, e.actor) for e in events] == [
            ("entry_added", ""),  # add 未署名(与底层默认一致)
            ("four_eyes_awaiting_second", "张三"),  # 第一审核人事件
            ("four_eyes_approved", "李四"),  # 双人齐事件(先于底层状态事件)
            ("entry_approved", "李四"),  # 底层状态事件由四眼层署名
        ]
        awaiting = events[1].payload
        assert awaiting["state"] == "awaiting_second"
        assert awaiting["reviewer"] == "张三"
        assert awaiting["reviewers"] == ["张三"]
        approved = events[2].payload
        assert approved["state"] == "approved"
        assert approved["reviewer"] == "李四"
        assert approved["reviewers"] == ["张三", "李四"]  # 双人语义字段
        # 底层状态事件照常携带迁移事实(重放对账可应用)。
        assert events[3].payload["from_status"] == "pending"
        assert events[3].payload["to_status"] == "approved"
        with FourEyesQueue(db, True) as q2:
            assert q2.get(entry_id).status == "approved"

    def test_required_false_signs_actor_and_reject_writes_event(
        self, tmp_path: Path
    ) -> None:
        """required=False 直通也署名 actor;reject 经透传落 entry_rejected。"""
        log, q, db, edb = self._open(tmp_path, "single", required=False)
        e1 = q.add(make_report("http://s1.test"))
        e2 = q.add(make_report("http://s2.test"))
        q.approve(e1, "王五")
        q.reject(e2, note="证据不足")
        q.close()
        log.close()

        events = self._events(edb)
        assert [(e.event_type, e.actor) for e in events] == [
            ("entry_added", ""),
            ("entry_added", ""),
            ("entry_approved", "王五"),  # 单人直通:审核人署名
            ("entry_rejected", ""),  # reject 无复核人参数:按现状不署名
        ]
        assert events[3].payload["note"] == "证据不足"

    def test_pair_completion_ledger_first_crash(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """崩溃仿真:entry_approved 已落盘、entries 未提交 → 状态未生效。"""
        log, q, db, edb = self._open(tmp_path, "crashpair")
        entry_id = q.add(make_report())
        q.approve(entry_id, "张三")
        real = log.append

        def crash_on_state_event(event_type: str, eid: int, **kw) -> int:
            if event_type == "entry_approved":
                seq = real(event_type, eid, **kw)
                raise RuntimeError(
                    f"模拟崩溃:事件 seq={seq} 已落盘,状态未提交"
                )
            return real(event_type, eid, **kw)

        monkeypatch.setattr(log, "append", crash_on_state_event)
        with pytest.raises(RuntimeError, match="模拟崩溃"):
            q.approve(entry_id, "李四")
        q.close()
        log.close()

        # 先账本后状态:账本已有底层状态事件,条目仍是 pending。
        events = self._events(edb)
        assert [e.event_type for e in events] == [
            "entry_added",
            "four_eyes_awaiting_second",
            "four_eyes_approved",
            "entry_approved",  # 事实已落盘
        ]
        with FourEyesQueue(db, True) as q2:
            assert q2.get(entry_id).status == "pending"  # 投影未生效
            # 留痕已按既有语义回滚(仅第一审核人)。
            assert q2.status(entry_id)["reviewers"] == ["张三"]

    def test_first_approver_ledger_first_crash(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """崩溃仿真:four_eyes_awaiting_second 落盘后、approvals 写入前崩溃。"""
        log, q, db, edb = self._open(tmp_path, "crashfirst")
        entry_id = q.add(make_report())
        real = log.append

        def crash_on_awaiting(event_type: str, eid: int, **kw) -> int:
            if event_type == "four_eyes_awaiting_second":
                seq = real(event_type, eid, **kw)
                raise RuntimeError(f"模拟崩溃:事件 seq={seq} 已落盘,留痕未写")
            return real(event_type, eid, **kw)

        monkeypatch.setattr(log, "append", crash_on_awaiting)
        with pytest.raises(RuntimeError, match="模拟崩溃"):
            q.approve(entry_id, "张三")
        q.close()
        log.close()

        # 先账本后状态:事件在账本,approvals 留痕未写。
        events = self._events(edb)
        assert [e.event_type for e in events] == [
            "entry_added",
            "four_eyes_awaiting_second",
        ]
        assert events[1].payload["reviewer"] == "张三"
        rows = raw_rows(db, "SELECT COUNT(*) FROM approvals")[0]
        assert rows[0] == 0
        with FourEyesQueue(db, True) as q2:
            assert q2.get(entry_id).status == "pending"
            assert q2.status(entry_id)["state"] == "none"

    def test_same_reviewer_rejected_emits_no_events(self, tmp_path: Path) -> None:
        """同人拒二次回归:校验先行,账本零新事件、留痕零新行。"""
        log, q, db, edb = self._open(tmp_path, "sameperson")
        entry_id = q.add(make_report())
        q.approve(entry_id, "张三")
        q.close()
        log.close()
        n_events = len(self._events(edb))

        log = EventLog(edb)
        q = FourEyesQueue(db, True, event_log=log)
        with pytest.raises(ValueError, match="同一审核人不能二次确认:张三"):
            q.approve(entry_id, "张三")
        with pytest.raises(ValueError, match="同一审核人不能二次确认:张三"):
            q.second_approver(entry_id, "张三")
        q.close()
        log.close()

        assert len(self._events(edb)) == n_events  # 零新事件
        rows = raw_rows(db, "SELECT COUNT(*) FROM approvals")[0]
        assert rows[0] == 1  # 主键双保险:无重复留痕

    def test_ledger_disabled_snapshot_identical(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """默认关(event_log=None):同一操作序列下快照与启用账本时逐行一致。"""
        monkeypatch.setattr(four_eyes_module, "now_iso", lambda: A222_FIXED_TS)
        monkeypatch.setattr(review_queue_module, "now_iso", lambda: A222_FIXED_TS)

        def run(dirname: str, with_ledger: bool) -> tuple[list, list]:
            root = tmp_path / dirname
            db = str(root / "q.db")
            ledger = (
                EventLog(str(root / "e.db")) if with_ledger else None
            )
            q = FourEyesQueue(db, True, event_log=ledger)
            a = q.add(make_report("http://a.test"))
            b = q.add(make_report("http://b.test"))
            q.approve(a, "张三")
            q.approve(a, "李四")
            q.approve(b, "张三")
            q.reject(b, note="证据不足,不予举报")
            q.mark_submitted(a)
            q.close()
            if ledger is not None:
                ledger.close()
            entries = raw_rows(db, "SELECT * FROM entries ORDER BY id")
            approvals = raw_rows(
                db, "SELECT entry_id, reviewer FROM approvals ORDER BY rowid"
            )
            return entries, approvals

        off_entries, off_approvals = run("off", with_ledger=False)
        on_entries, on_approvals = run("on", with_ledger=True)
        # 现状路径:不创建任何账本文件(账本由调用方持有)。
        assert not (tmp_path / "off" / "e.db").exists()
        # 启用账本不改变 entries / approvals 任何一行(默认关 = 逐字节现状)。
        assert off_entries == on_entries
        assert off_approvals == on_approvals
        # 启用侧确实产生了事件(对照 sanity,防止两边都空跑)。
        assert len(self._events(str(tmp_path / "on" / "e.db"))) >= 6
