"""netsentinel.decision.review_queue 单元测试(A10)。

覆盖:自动建目录/建表、add/list/get 往返、状态机红线(机器初筛、人工拍板:
只有 pending 可人工确认/驳回、只有 approved 可标记已提交)、状态过滤与统计、
CLI(--db 注入 tmp 库,capsys 断言中文输出与返回码)。

全部离线,只写 tmp_path,不依赖任何兄弟模块。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from netsentinel import telemetry
from netsentinel.contracts import SiteReport, Verdict
from netsentinel.decision.review_queue import (
    BUSY_TIMEOUT_MS,
    MAX_CELL_DISPLAY_WIDTH,
    VALID_STATUSES,
    Entry,
    ReviewQueue,
    _display_width,
    main,
)

ROOT = Path(__file__).resolve().parents[1]


def make_report(
    url: str = "http://site.test/page", verdict: Verdict = Verdict.SUSPECT
) -> SiteReport:
    """构造最小 SiteReport(不依赖 A09 的 verdict 模块)。"""
    return SiteReport(
        site_url=url, verdict=verdict, needs_review=(verdict != Verdict.CLEAN)
    )


@pytest.fixture()
def queue(tmp_path: Path) -> ReviewQueue:
    """嵌套路径同时验证 __init__ 自动建目录与建表。"""
    q = ReviewQueue(tmp_path / "queues" / "review_queue.db")
    yield q
    q.close()


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "cli" / "review_queue.db")


def _contains_chinese(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


# ---------------------------------------------------------------------------
# add / list / get 往返
# ---------------------------------------------------------------------------


class TestAddListGet:
    def test_roundtrip_fields(self, queue: ReviewQueue):
        report = make_report("http://example.test/home", Verdict.NSFW)
        entry_id = queue.add(
            report, evidence_zip="data/evidence/example_20260101.zip"
        )
        assert isinstance(entry_id, int) and entry_id >= 1

        entries = queue.list()
        assert len(entries) == 1
        entry = entries[0]
        assert isinstance(entry, Entry)
        assert entry.id == entry_id
        assert entry.site_url == "http://example.test/home"
        assert entry.verdict == "nsfw"  # 存 Verdict.value 而非枚举名
        assert entry.status == "pending"
        assert entry.evidence_zip == "data/evidence/example_20260101.zip"
        assert entry.created_at
        assert entry.updated_at == entry.created_at
        assert entry.note == ""

        got = queue.get(entry_id)
        assert got == entry  # dataclass 逐字段相等

    def test_get_missing_returns_none(self, queue: ReviewQueue):
        assert queue.get(424242) is None

    def test_add_without_zip_defaults_empty(self, queue: ReviewQueue):
        entry_id = queue.add(make_report())
        assert queue.get(entry_id).evidence_zip == ""

    @pytest.mark.parametrize("verdict", list(Verdict))
    def test_verdict_stored_as_value(self, queue: ReviewQueue, verdict: Verdict):
        entry_id = queue.add(make_report(verdict=verdict))
        assert queue.get(entry_id).verdict == verdict.value

    def test_list_ascending_by_id(self, queue: ReviewQueue):
        ids = [queue.add(make_report(f"http://s{i}.test/")) for i in range(3)]
        assert [e.id for e in queue.list()] == ids

    def test_persistence_across_connections(self, tmp_path: Path):
        db = str(tmp_path / "persist.db")
        q1 = ReviewQueue(db)
        entry_id = q1.add(make_report("http://persist.test/"))
        q1.approve(entry_id, note="人工已确认")
        q1.close()

        q2 = ReviewQueue(db)
        try:
            entry = q2.get(entry_id)
            assert entry is not None
            assert entry.status == "approved"
            assert entry.note == "人工已确认"
            assert entry.site_url == "http://persist.test/"
        finally:
            q2.close()


# ---------------------------------------------------------------------------
# 状态机:机器初筛、人工拍板
# ---------------------------------------------------------------------------


class TestStateMachine:
    def test_approve_then_mark_submitted(self, queue: ReviewQueue):
        entry_id = queue.add(make_report())
        approved = queue.approve(entry_id, note="已人工核实,证据充分")
        assert approved.status == "approved"
        entry = queue.get(entry_id)
        assert entry is not None
        assert entry.status == "approved"
        assert entry.note == "已人工核实,证据充分"

        queue.mark_submitted(entry_id)
        entry = queue.get(entry_id)
        assert entry is not None
        assert entry.status == "submitted"

    def test_approve_requires_pending(self, queue: ReviewQueue):
        entry_id = queue.add(make_report())
        queue.approve(entry_id)
        with pytest.raises(ValueError, match="pending"):
            queue.approve(entry_id)

    def test_mark_submitted_requires_approved(self, queue: ReviewQueue):
        entry_id = queue.add(make_report())
        # pending 不能直接 submitted:必须先经人工确认
        with pytest.raises(ValueError, match="approved"):
            queue.mark_submitted(entry_id)
        queue.approve(entry_id)
        queue.mark_submitted(entry_id)
        # submitted 也不能再次标记
        with pytest.raises(ValueError, match="approved"):
            queue.mark_submitted(entry_id)

    def test_rejected_cannot_approve_or_resubmit(self, queue: ReviewQueue):
        entry_id = queue.add(make_report())
        queue.reject(entry_id, note="误报")
        assert queue.get(entry_id).status == "rejected"
        with pytest.raises(ValueError):
            queue.approve(entry_id)
        with pytest.raises(ValueError):
            queue.reject(entry_id)
        with pytest.raises(ValueError):
            queue.mark_submitted(entry_id)

    def test_operations_on_missing_id_raise_chinese(self, queue: ReviewQueue):
        for call in (queue.approve, queue.reject, queue.mark_submitted):
            with pytest.raises(ValueError, match="不存在"):
                call(987654)

    def test_error_message_is_chinese(self, queue: ReviewQueue):
        entry_id = queue.add(make_report())
        queue.approve(entry_id)
        with pytest.raises(ValueError) as exc_info:
            queue.approve(entry_id)
        assert _contains_chinese(str(exc_info.value))

    def test_note_preserved_by_later_transition(self, queue: ReviewQueue):
        entry_id = queue.add(make_report())
        queue.approve(entry_id, note="已人工核实")
        queue.mark_submitted(entry_id)  # 不带备注的迁移不清空原备注
        entry = queue.get(entry_id)
        assert entry is not None
        assert entry.status == "submitted"
        assert entry.note == "已人工核实"


# ---------------------------------------------------------------------------
# list 过滤与 summary 统计
# ---------------------------------------------------------------------------


class TestFilterAndSummary:
    def test_list_filter_by_status_and_summary(self, queue: ReviewQueue):
        e1 = queue.add(make_report("http://a.test/"))
        e2 = queue.add(make_report("http://b.test/"))
        e3 = queue.add(make_report("http://c.test/"))
        queue.approve(e1)
        queue.reject(e2)
        # e3 保持 pending

        assert [e.id for e in queue.list()] == [e1, e2, e3]
        assert [e.id for e in queue.list("pending")] == [e3]
        assert [e.id for e in queue.list("approved")] == [e1]
        assert [e.id for e in queue.list("rejected")] == [e2]
        assert queue.list("submitted") == []

        queue.mark_submitted(e1)
        assert queue.summary() == {
            "pending": 1,
            "approved": 0,
            "rejected": 1,
            "submitted": 1,
        }

    def test_summary_empty_db(self, queue: ReviewQueue):
        assert queue.summary() == {s: 0 for s in VALID_STATUSES}


# ---------------------------------------------------------------------------
# CLI:--db 注入 tmp 库,不触碰真实 data/review_queue.db
# ---------------------------------------------------------------------------


class TestCli:
    @staticmethod
    def _seed(db: str) -> int:
        q = ReviewQueue(db)
        try:
            return q.add(
                make_report("http://cli.test/页面", Verdict.SUSPECT),
                evidence_zip="data/evidence/cli.zip",
            )
        finally:
            q.close()

    def test_list_pending(self, db_path: str, capsys: pytest.CaptureFixture):
        self._seed(db_path)
        rc = main(["--db", db_path, "list"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "编号" in out and "站点" in out and "待复核" in out
        assert "http://cli.test/页面" in out
        assert "data/evidence/cli.zip" in out

    def test_list_status_filter(self, db_path: str, capsys: pytest.CaptureFixture):
        entry_id = self._seed(db_path)
        q = ReviewQueue(db_path)
        q.approve(entry_id)
        q.close()

        rc = main(["--db", db_path, "list", "--status", "pending"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "http://cli.test/页面" not in out

        rc = main(["--db", db_path, "list", "--status", "approved"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "http://cli.test/页面" in out
        assert "已确认" in out

    def test_show(self, db_path: str, capsys: pytest.CaptureFixture):
        entry_id = self._seed(db_path)
        rc = main(["--db", db_path, "show", str(entry_id)])
        out = capsys.readouterr().out
        assert rc == 0
        for token in ("编号", "站点", "判定", "状态", "证据包", "备注"):
            assert token in out
        assert "http://cli.test/页面" in out
        assert "疑似" in out
        assert "pending" in out

    def test_show_unknown_id_returns_1(
        self, db_path: str, capsys: pytest.CaptureFixture
    ):
        self._seed(db_path)
        rc = main(["--db", db_path, "show", "404404"])
        captured = capsys.readouterr()
        assert rc == 1
        assert "404404" in captured.err
        assert "不存在" in captured.err
        assert _contains_chinese(captured.err)

    def test_approve_with_note(self, db_path: str, capsys: pytest.CaptureFixture):
        entry_id = self._seed(db_path)
        rc = main(
            ["--db", db_path, "approve", str(entry_id), "--note", "人工核实通过"]
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "已人工确认" in out
        assert "人工核实通过" in out

        q = ReviewQueue(db_path)
        try:
            entry = q.get(entry_id)
            assert entry is not None
            assert entry.status == "approved"
            assert entry.note == "人工核实通过"
        finally:
            q.close()

    def test_reject(self, db_path: str, capsys: pytest.CaptureFixture):
        entry_id = self._seed(db_path)
        rc = main(["--db", db_path, "reject", str(entry_id), "--note", "误报"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "已人工驳回" in out

        q = ReviewQueue(db_path)
        try:
            entry = q.get(entry_id)
            assert entry is not None
            assert entry.status == "rejected"
            assert entry.note == "误报"
        finally:
            q.close()

    def test_approve_rejected_via_cli_returns_1(
        self, db_path: str, capsys: pytest.CaptureFixture
    ):
        entry_id = self._seed(db_path)
        q = ReviewQueue(db_path)
        q.reject(entry_id)
        q.close()

        rc = main(["--db", db_path, "approve", str(entry_id)])
        captured = capsys.readouterr()
        assert rc == 1
        assert "错误" in captured.err
        assert _contains_chinese(captured.err)

    def test_db_option_accepted_after_subcommand(
        self, db_path: str, capsys: pytest.CaptureFixture
    ):
        entry_id = self._seed(db_path)
        rc = main(["show", str(entry_id), "--db", db_path])
        out = capsys.readouterr().out
        assert rc == 0
        assert "http://cli.test/页面" in out

    def test_module_entrypoint(self, tmp_path: Path):
        db = str(tmp_path / "module.db")
        self._seed(db)
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "netsentinel.decision.review_queue",
                "--db",
                db,
                "list",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(ROOT),
            env=env,
        )
        assert proc.returncode == 0, proc.stderr
        assert "编号" in proc.stdout
        assert "待复核" in proc.stdout
        assert "http://cli.test/页面" in proc.stdout


# ---------------------------------------------------------------------------
# V5 升级:WAL + busy_timeout、status 索引(旧库补建)、CLI 宽 URL 截断、
# telemetry(queue.add / queue.approve / queue.reject / queue.submit +
# summary 后 queue.total 仪表)
# ---------------------------------------------------------------------------


def _index_names(db: str) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    finally:
        conn.close()


def test_v5_wal_and_busy_timeout_pragmas(tmp_path: Path):
    """连接策略:journal_mode=wal、busy_timeout=5000,重开连接仍生效。"""
    db = str(tmp_path / "wal.db")
    q = ReviewQueue(db)
    try:
        mode = q._conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout = q._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert mode == "wal"
        assert timeout == BUSY_TIMEOUT_MS == 5000
    finally:
        q.close()

    # WAL 是库属性:再次打开(新连接)仍是 WAL。
    q2 = ReviewQueue(db)
    try:
        assert q2._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        q2.close()


def test_v5_status_index_created_and_list_uses_it(tmp_path: Path):
    """entries(status, id) 索引存在,且 list(status) 查询计划走索引。"""
    db = str(tmp_path / "idx.db")
    q = ReviewQueue(db)
    try:
        assert "idx_entries_status" in _index_names(db)
        plan = q._conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM entries WHERE status = ? ORDER BY id ASC",
            ("pending",),
        ).fetchall()
        details = " | ".join(str(row[-1]) for row in plan)
        assert "idx_entries_status" in details
        assert "SCAN" not in details  # 不再全表扫描
    finally:
        q.close()


def test_v5_status_index_backfilled_on_old_db(tmp_path: Path):
    """旧库兼容:无索引的历史库打开时自动补建,老数据可读可筛。"""
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    try:
        # 模拟 V4 时代的旧库:只有表,没有索引。
        conn.execute(
            """
            CREATE TABLE entries (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                site_url     TEXT    NOT NULL,
                verdict      TEXT    NOT NULL,
                status       TEXT    NOT NULL DEFAULT 'pending',
                evidence_zip TEXT    NOT NULL DEFAULT '',
                created_at   TEXT    NOT NULL DEFAULT '',
                updated_at   TEXT    NOT NULL DEFAULT '',
                note         TEXT    NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "INSERT INTO entries (site_url, verdict) VALUES (?, ?)",
            ("http://legacy.test/", "suspect"),
        )
        conn.commit()
    finally:
        conn.close()
    assert "idx_entries_status" not in _index_names(db)

    q = ReviewQueue(db)
    try:
        assert "idx_entries_status" in _index_names(db)  # 打开即补建
        legacy = q.list("pending")
        assert [e.site_url for e in legacy] == ["http://legacy.test/"]
    finally:
        q.close()


def test_v5_telemetry_counters_and_total_gauge(tmp_path: Path):
    """telemetry:入列/确认/驳回/提交计数,summary 后回写 queue.total。"""
    telemetry.reset()
    q = ReviewQueue(tmp_path / "tele.db")
    try:
        e1 = q.add(make_report("http://t1.test/"))
        e2 = q.add(make_report("http://t2.test/"))
        q.add(make_report("http://t3.test/"))
        q.approve(e1)
        q.mark_submitted(e1)
        q.reject(e2)
        # 第 3 条保持 pending
        stats = q.summary()
        assert stats["pending"] == 1

        snap = telemetry.snapshot()
        assert snap["counters"]["queue.add"] == 3
        assert snap["counters"]["queue.approve"] == 1
        assert snap["counters"]["queue.reject"] == 1
        assert snap["counters"]["queue.submit"] == 1
        assert snap["gauges"]["queue.total"] == 3  # summary 后回写总数
    finally:
        q.close()


_LONG_URL = "http://wide.test/" + "x" * 140  # 显示宽度 157 > 48
_LONG_ZIP = "data/evidence/" + "y" * 140 + ".zip"


def test_v5_cli_truncates_wide_url_by_default(
    db_path: str, capsys: pytest.CaptureFixture
):
    """CLI list 默认截断超宽 URL/路径:输出带省略号、行长有界(终端友好)。"""
    q = ReviewQueue(db_path)
    try:
        q.add(make_report(_LONG_URL, Verdict.SUSPECT), evidence_zip=_LONG_ZIP)
    finally:
        q.close()

    rc = main(["--db", db_path, "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert _LONG_URL not in out  # 完整超宽 URL 不出现
    assert "…" in out
    assert "http://wide.test/" in out  # 截断保留可辨认前缀
    # 截断后每行宽度有界(两个 URL 列各 ≤ 48 显示列 + 其余定宽列)。
    for line in out.splitlines():
        assert _display_width(line) <= MAX_CELL_DISPLAY_WIDTH * 2 + 100


def test_v5_cli_truncation_does_not_alter_data(db_path: str):
    """截断只影响显示:库中仍是完整 URL 与证据包路径。"""
    q = ReviewQueue(db_path)
    try:
        entry_id = q.add(make_report(_LONG_URL), evidence_zip=_LONG_ZIP)
    finally:
        q.close()

    with ReviewQueue(db_path) as checker:
        entry = checker.get(entry_id)
        assert entry is not None
        assert entry.site_url == _LONG_URL
        assert entry.evidence_zip == _LONG_ZIP


def test_v5_cli_wide_flag_shows_full_url(
    db_path: str, capsys: pytest.CaptureFixture
):
    """--wide 完整显示超宽 URL(不受默认截断影响)。"""
    q = ReviewQueue(db_path)
    try:
        q.add(make_report(_LONG_URL), evidence_zip=_LONG_ZIP)
    finally:
        q.close()

    rc = main(["--db", db_path, "list", "--wide"])
    out = capsys.readouterr().out
    assert rc == 0
    assert _LONG_URL in out
    assert _LONG_ZIP in out
    assert "…" not in out


def test_v5_short_url_unaffected_by_truncation(
    db_path: str, capsys: pytest.CaptureFixture
):
    """常规长度 URL 默认显示不受截断影响(既有 CLI 输出语义保持)。"""
    q = ReviewQueue(db_path)
    try:
        q.add(make_report("http://short.test/a", Verdict.SUSPECT))
    finally:
        q.close()

    rc = main(["--db", db_path, "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "http://short.test/a" in out
    assert "…" not in out


# ---------------------------------------------------------------------------
# 决策论复核分诊(triage):list(sort=...) 只改返回顺序,不改状态机/四眼/人工门
# ---------------------------------------------------------------------------


def _backdate(queue: ReviewQueue, entry_id: int, created_at: str) -> None:
    """测试辅助:直接改写 created_at 以构造不同老化程度(仅测试库)。"""
    queue._conn.execute(
        "UPDATE entries SET created_at = ? WHERE id = ?", (created_at, entry_id)
    )
    queue._conn.commit()


class TestTriageSort:
    def test_default_sort_is_fifo_unchanged(self, queue: ReviewQueue):
        """红线:默认(不传 sort 或 sort="fifo")与历史行为完全一致。"""
        ids = [queue.add(make_report(f"http://s{i}.test/")) for i in range(3)]
        assert [e.id for e in queue.list()] == ids
        assert [e.id for e in queue.list(sort="fifo")] == ids

    def test_sort_triage_reorders_by_information_value(self, queue: ReviewQueue):
        """老 nsfw > 老 suspect > 新 suspect(危害档位 + 老化分量)。"""
        now = datetime.now(timezone.utc)
        e_new_suspect = queue.add(make_report("http://new-suspect.test/"))
        e_old_nsfw = queue.add(
            make_report("http://old-nsfw.test/", Verdict.NSFW)
        )
        e_mid_suspect = queue.add(make_report("http://mid-suspect.test/"))
        _backdate(queue, e_old_nsfw, (now - timedelta(days=10)).isoformat())
        _backdate(queue, e_mid_suspect, (now - timedelta(days=5)).isoformat())

        fifo = [e.id for e in queue.list()]
        assert fifo == [e_new_suspect, e_old_nsfw, e_mid_suspect]  # FIFO 基线
        triaged = [e.id for e in queue.list(sort="triage")]
        assert triaged == [e_old_nsfw, e_mid_suspect, e_new_suspect]

    def test_sort_triage_same_score_keeps_fifo_tiebreak(
        self, queue: ReviewQueue
    ):
        """同分平局:按 id 升序(FIFO 决断),排序确定。"""
        ts = datetime.now(timezone.utc).isoformat()
        ids = [
            queue.add(make_report(f"http://tie-{i}.test/", Verdict.SUSPECT))
            for i in range(4)
        ]
        for entry_id in ids:
            _backdate(queue, entry_id, ts)

        assert [e.id for e in queue.list(sort="triage")] == ids

    def test_sort_triage_with_status_filter(self, queue: ReviewQueue):
        """sort 与 status 过滤正交:只在过滤结果内重排。"""
        now = datetime.now(timezone.utc)
        e1 = queue.add(make_report("http://p-new.test/"))
        e2 = queue.add(make_report("http://p-old.test/", Verdict.NSFW))
        _backdate(queue, e2, (now - timedelta(days=9)).isoformat())
        queue.approve(e1)  # e1 离开 pending

        pending_triaged = queue.list("pending", sort="triage")
        assert [e.id for e in pending_triaged] == [e2]
        assert [e.id for e in queue.list("approved", sort="triage")] == [e1]

    def test_invalid_sort_raises_chinese(self, queue: ReviewQueue):
        queue.add(make_report())
        with pytest.raises(ValueError, match="排序方式") as exc_info:
            queue.list(sort="lifo")
        assert _contains_chinese(str(exc_info.value))

    def test_triage_sort_does_not_touch_state_machine(
        self, queue: ReviewQueue
    ):
        """红线回归:分诊排序后,状态机转换约束与历史完全一致。"""
        e1 = queue.add(make_report("http://m1.test/", Verdict.NSFW))
        e2 = queue.add(make_report("http://m2.test/"))
        queue.list(sort="triage")  # 排序本身无副作用

        queue.approve(e1)
        queue.mark_submitted(e1)
        queue.reject(e2)
        # 非法迁移仍被拒绝:submitted 不能再 approve/reject/mark。
        for call in (queue.approve, queue.reject, queue.mark_submitted):
            with pytest.raises(ValueError):
                call(e1)
        with pytest.raises(ValueError):
            queue.approve(e2)  # rejected 终态
        assert queue.get(e1).status == "submitted"
        assert queue.get(e2).status == "rejected"

    def test_triage_sort_is_pure_read(self, queue: ReviewQueue):
        """红线:triage 排序不写库(条目数据与 updated_at 均不变)。"""
        e1 = queue.add(make_report("http://pure.test/"))
        before = queue.get(e1)
        queue.list(sort="triage")
        queue.list(sort="triage")
        assert queue.get(e1) == before
        assert queue.summary()["pending"] == 1

    def test_overturn_stats_from_queue_connection(self, queue: ReviewQueue):
        """翻案估计器从本库只读聚合,与逐条历史聚合结果一致。"""
        from netsentinel.decision.triage import OverturnStats

        a1 = queue.add(make_report("http://o1.test/", Verdict.NSFW))
        a2 = queue.add(make_report("http://o2.test/", Verdict.NSFW))
        a3 = queue.add(make_report("http://o3.test/", Verdict.NSFW))
        queue.reject(a1)
        queue.approve(a2)
        queue.mark_submitted(a2)
        # a3 保持 pending,不入样本

        from_sql = OverturnStats.from_sqlite(queue._conn)
        from_history = OverturnStats.from_history(queue.list())
        assert from_sql == from_history
        assert from_sql.overturn_prob("nsfw") == pytest.approx((1 + 1) / (2 + 2))
        assert from_sql.total_resolved() == 2

    def test_cli_sort_triage_and_default_fifo(
        self, db_path: str, capsys: pytest.CaptureFixture
    ):
        """CLI:默认输出 FIFO;--sort triage 重排显示(数据不变)。"""
        now = datetime.now(timezone.utc)
        q = ReviewQueue(db_path)
        try:
            q.add(make_report("http://cli-new.test/"))
            old_id = q.add(
                make_report("http://cli-old.test/", Verdict.NSFW)
            )
            _backdate(q, old_id, (now - timedelta(days=10)).isoformat())
        finally:
            q.close()

        rc = main(["--db", db_path, "list"])
        out = capsys.readouterr().out
        assert rc == 0
        assert out.index("http://cli-new.test/") < out.index("http://cli-old.test/")

        rc = main(["--db", db_path, "list", "--sort", "triage"])
        out = capsys.readouterr().out
        assert rc == 0
        assert out.index("http://cli-old.test/") < out.index("http://cli-new.test/")

        # 显示重排不改变库内数据与 FIFO 语义。
        with ReviewQueue(db_path) as checker:
            entries = checker.list()
            assert [e.id for e in entries] == sorted(e.id for e in entries)
            assert [e.site_url for e in entries] == [
                "http://cli-new.test/",
                "http://cli-old.test/",
            ]

    def test_cli_rejects_unknown_sort(
        self, db_path: str, capsys: pytest.CaptureFixture
    ):
        q = ReviewQueue(db_path)
        try:
            q.add(make_report())
        finally:
            q.close()
        with pytest.raises(SystemExit) as exc_info:
            main(["--db", db_path, "list", "--sort", "random"])
        assert exc_info.value.code != 0  # argparse choices 拦截


# ---------------------------------------------------------------------------
# 分歧弃权提权消费:priority_weight 列(add 附带 / annotate 批注 /
# triage 排序联动);状态机与历史行为零改动
# ---------------------------------------------------------------------------


def _column_names(db: str) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {str(row[1]) for row in conn.execute("PRAGMA table_info(entries)")}
    finally:
        conn.close()


class TestPriorityWeight:
    def test_fresh_schema_has_priority_weight_column(self, tmp_path: Path):
        """新库:entries 表自带 priority_weight 列(默认 0.0)。"""
        db = str(tmp_path / "fresh.db")
        q = ReviewQueue(db)
        try:
            assert "priority_weight" in _column_names(db)
            entry_id = q.add(make_report())
            assert q.get(entry_id).priority_weight == 0.0
        finally:
            q.close()

    def test_add_with_priority_weight_roundtrip(self, queue: ReviewQueue):
        """入队附带提权加项:完整往返(其余字段不受影响)。"""
        report = make_report("http://abstain.test/", Verdict.SUSPECT)
        entry_id = queue.add(report, evidence_zip="z.zip", priority_weight=0.48)
        entry = queue.get(entry_id)
        assert entry is not None
        assert entry.priority_weight == pytest.approx(0.48)
        assert entry.status == "pending"  # 状态机入口不变:仍是 pending
        assert entry.verdict == "suspect"
        assert entry.evidence_zip == "z.zip"
        # 不传该参数:默认 0.0(与旧版入库行为一致)
        other = queue.add(make_report("http://plain.test/"))
        assert queue.get(other).priority_weight == 0.0

    @pytest.mark.parametrize("bad", [-0.1, float("nan"), float("inf"), "0.5", True])
    def test_add_rejects_invalid_priority_weight(self, queue: ReviewQueue, bad):
        """非法加项(负/NaN/inf/字符串/bool)→ 中文 ValueError,且不入库。"""
        before = queue.summary()["pending"]
        with pytest.raises(ValueError, match="提权权重"):
            queue.add(make_report(), priority_weight=bad)
        assert queue.summary()["pending"] == before

    def test_annotate_updates_weight_only(
        self, queue: ReviewQueue, tmp_path: Path
    ):
        """批注只写加项与更新时间:status / verdict / note / created_at
        原样保留,状态机约束在批注后完全不变。"""
        entry_id = queue.add(
            make_report("http://anno.test/"), note="初筛备注", priority_weight=0.1
        )
        before = queue.get(entry_id)

        annotated = queue.annotate(entry_id, priority_weight=0.8)

        assert annotated.priority_weight == pytest.approx(0.8)
        assert annotated.status == before.status == "pending"
        assert annotated.verdict == before.verdict
        assert annotated.note == before.note == "初筛备注"
        assert annotated.created_at == before.created_at
        assert annotated.updated_at >= before.updated_at
        assert queue.get(entry_id).priority_weight == pytest.approx(0.8)

        # 状态机零改动:批注后 pending 仍可确认/驳回,非法迁移仍被拒绝。
        queue.approve(entry_id)
        with pytest.raises(ValueError):
            queue.reject(entry_id)  # approved 不能再驳回
        queue.mark_submitted(entry_id)
        assert queue.get(entry_id).status == "submitted"
        # 已定案条目仍可批注(排序提示不是状态迁移)
        again = queue.annotate(entry_id, priority_weight=0.0)
        assert again.status == "submitted" and again.priority_weight == 0.0

    def test_annotate_missing_id_raises_chinese(self, queue: ReviewQueue):
        with pytest.raises(ValueError, match="不存在"):
            queue.annotate(987654, priority_weight=0.5)

    @pytest.mark.parametrize("bad", [-1.0, float("nan"), "high"])
    def test_annotate_rejects_invalid_weight(self, queue: ReviewQueue, bad):
        entry_id = queue.add(make_report())
        with pytest.raises(ValueError, match="提权权重"):
            queue.annotate(entry_id, priority_weight=bad)
        assert queue.get(entry_id).priority_weight == 0.0  # 未被污染

    def test_legacy_db_backfills_priority_weight(self, tmp_path: Path):
        """旧库兼容:无该列的历史库打开时自动补列,老数据补 0,行为不变。"""
        db = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                """
                CREATE TABLE entries (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_url     TEXT    NOT NULL,
                    verdict      TEXT    NOT NULL,
                    status       TEXT    NOT NULL DEFAULT 'pending',
                    evidence_zip TEXT    NOT NULL DEFAULT '',
                    created_at   TEXT    NOT NULL DEFAULT '',
                    updated_at   TEXT    NOT NULL DEFAULT '',
                    note         TEXT    NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                "INSERT INTO entries (site_url, verdict) VALUES (?, ?)",
                ("http://legacy.test/", "suspect"),
            )
            conn.commit()
        finally:
            conn.close()
        assert "priority_weight" not in _column_names(db)

        q = ReviewQueue(db)
        try:
            assert "priority_weight" in _column_names(db)  # 打开即补建
            legacy = q.list("pending")
            assert [e.site_url for e in legacy] == ["http://legacy.test/"]
            assert legacy[0].priority_weight == 0.0  # 老数据补默认 0
            q.annotate(legacy[0].id, priority_weight=0.42)
            assert q.get(legacy[0].id).priority_weight == pytest.approx(0.42)
        finally:
            q.close()

    def test_annotate_telemetry_counter(self, tmp_path: Path):
        telemetry.reset()
        q = ReviewQueue(tmp_path / "anno-tele.db")
        try:
            entry_id = q.add(make_report())
            q.annotate(entry_id, priority_weight=0.5)
            q.annotate(entry_id, priority_weight=0.7)
            assert telemetry.snapshot()["counters"]["queue.annotate"] == 2
        finally:
            q.close()


class TestTriageBoostIntegration:
    def test_list_triage_applies_priority_weight_boost(self, queue: ReviewQueue):
        """联动:同分条目中带提权加项的先看;fifo 顺序不受影响;纯读。"""
        ts = datetime.now(timezone.utc).isoformat()
        e1 = queue.add(make_report("http://same-1.test/"))
        e2 = queue.add(make_report("http://same-2.test/"))
        for entry_id in (e1, e2):
            _backdate(queue, entry_id, ts)
        queue.annotate(e2, priority_weight=1.0)

        assert [e.id for e in queue.list()] == [e1, e2]  # fifo 不变
        assert [e.id for e in queue.list(sort="fifo")] == [e1, e2]
        assert [e.id for e in queue.list(sort="triage")] == [e2, e1]  # 提权反超

        # 纯读:排序不写库(加项与更新时间不变)
        queue.list(sort="triage")
        assert queue.get(e1).priority_weight == 0.0
        assert queue.get(e2).priority_weight == pytest.approx(1.0)

    def test_zero_weights_keep_triage_order_unchanged(self, queue: ReviewQueue):
        """全 0 加项:triage 排序与无该功能时完全一致(默认现状)。"""
        ts = datetime.now(timezone.utc).isoformat()
        ids = [
            queue.add(make_report(f"http://zero-{i}.test/")) for i in range(3)
        ]
        for entry_id in ids:
            _backdate(queue, entry_id, ts)
        assert [e.id for e in queue.list(sort="triage")] == ids  # 同分 FIFO

    def test_same_url_multiple_entries_take_max_boost(
        self, queue: ReviewQueue
    ):
        """同站点多条:取最大加项(站点级提权语义)。"""
        ts = datetime.now(timezone.utc).isoformat()
        e1 = queue.add(make_report("http://dup.test/"))
        e2 = queue.add(make_report("http://other.test/"))
        for entry_id in (e1, e2):
            _backdate(queue, entry_id, ts)
        queue.annotate(e1, priority_weight=0.2)
        # 同站点再入一条,加项更小:站点级有效加项仍取最大(0.2 > 0.05)。
        e3 = queue.add(make_report("http://dup.test/"), priority_weight=0.05)
        _backdate(queue, e3, ts)

        triaged = [e.id for e in queue.list(sort="triage")]
        assert triaged[0] == e1  # dup 站点(0.2)整体最优先
        # e3 与 e1 同站点同加项:平局按 id FIFO,e1 < e3
        assert triaged[:2] == [e1, e3]

    def test_boost_with_status_filter(self, queue: ReviewQueue):
        """加项只影响 triage 排序,与 status 过滤正交。"""
        ts = datetime.now(timezone.utc).isoformat()
        p1 = queue.add(make_report("http://p1.test/"))
        p2 = queue.add(make_report("http://p2.test/"))
        for entry_id in (p1, p2):
            _backdate(queue, entry_id, ts)
        queue.annotate(p2, priority_weight=0.9)
        queue.approve(p1)

        assert [e.id for e in queue.list("pending", sort="triage")] == [p2]
        assert [e.id for e in queue.list("approved", sort="triage")] == [p1]

    def test_add_priority_weight_flows_from_abstain_hint_shape(
        self, queue: ReviewQueue
    ):
        """与 decision.abstain 提示的集成形状:to_triage_hint 的
        priority_weight 可以原样入队(键名与数值口径对齐)。"""
        from netsentinel.decision.abstain import decide, to_triage_hint

        hint = to_triage_hint(decide([0.9, 0.1]), p_nsfw=0.5)
        entry_id = queue.add(
            make_report("http://hint.test/"),
            priority_weight=float(hint["priority_weight"]),
        )
        assert queue.get(entry_id).priority_weight == pytest.approx(hint["priority_weight"])


# ---------------------------------------------------------------------------
# A213 · 事件账本双写(先账本后状态;event_log=None = 现状逐字节不变)
# ---------------------------------------------------------------------------


class TestEventLedgerDualWrite:
    """双写模式:add/approve/reject/mark_submitted/annotate 在状态变更提交前
    先把事件 append 进账本;非法迁移零事件;账本失败 → 状态回滚。"""

    @pytest.fixture()
    def pair(self, tmp_path: Path):
        """返回 (EventLog, 启用双写的 ReviewQueue);用毕各自关闭。"""
        from netsentinel.storage.event_log import EventLog

        log = EventLog(tmp_path / "ledger" / "events.db")
        q = ReviewQueue(tmp_path / "queues" / "review_queue.db", event_log=log)
        yield log, q
        q.close()
        log.close()

    # -- 默认不启用 = 现状 ---------------------------------------------------

    def test_disabled_by_default_no_events_anywhere(self, tmp_path: Path):
        """不传 event_log:队列库无 events 表、目录里无账本文件、零事件。"""
        q = ReviewQueue(tmp_path / "q.db")
        entry_id = q.add(make_report())
        q.approve(entry_id)
        rows = q._conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'events'"
        ).fetchall()
        q.close()
        assert rows == []  # 队列库绝无 events 表
        assert list(tmp_path.iterdir()) == [tmp_path / "q.db"]  # 无账本文件

    def test_disabled_statement_trace_is_legacy(self, tmp_path: Path):
        """禁用路径的 SQL 语句面与旧版一致:只碰 entries,绝无 events。"""
        statements: list[str] = []
        q = ReviewQueue(tmp_path / "q.db")
        q._conn.set_trace_callback(statements.append)
        entry_id = q.add(make_report())
        q.approve(entry_id)
        q._conn.set_trace_callback(None)
        q.close()
        joined = "\n".join(statements)
        assert "events" not in joined  # 账本零参与
        assert sum(1 for s in statements if "INSERT INTO entries" in s) == 1
        assert sum(1 for s in statements if "FROM entries" in s) == 1  # 校验 SELECT
        assert sum(1 for s in statements if "UPDATE entries" in s) == 1

    def test_explicit_none_equals_legacy_snapshot(self, tmp_path: Path, monkeypatch):
        """event_log=None 显式传参与不传参:同操作序列 → 逐字节同一快照。"""
        monkeypatch.setattr(
            "netsentinel.decision.review_queue.now_iso",
            lambda: "2026-01-01T08:00:00+08:00",
        )

        def build(name: str, kwargs: dict) -> str:
            q = ReviewQueue(tmp_path / name, **kwargs)
            e1 = q.add(make_report("http://a.test"), note="组:专案一")
            e2 = q.add(make_report("http://b.test"), priority_weight=1.5)
            q.approve(e1, "已核实")
            q.reject(e2, "误报")
            q.annotate(e1, priority_weight=2.0)
            dump = "\n".join(q._conn.iterdump())
            q.close()
            return dump

        baseline = build("legacy.db", {})
        explicit = build("explicit.db", {"event_log": None})
        assert baseline == explicit  # 语义逐字节一致(iterdump 全量比对)

    # -- 事件先于状态落盘 -----------------------------------------------------

    def test_add_emits_entry_added_before_commit(self, pair):
        log, q = pair
        entry_id = q.add(
            make_report("http://ev.test/"),
            evidence_zip="data/ev.zip",
            note="组:测试",
            priority_weight=1.5,
            actor="机器初筛",
        )
        events = log.query(entry_id=entry_id)
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "entry_added"
        assert ev.actor == "机器初筛"
        assert ev.payload["site_url"] == "http://ev.test/"
        assert ev.payload["verdict"] == "suspect"
        assert ev.payload["evidence_zip"] == "data/ev.zip"
        assert ev.payload["note"] == "组:测试"
        assert ev.payload["priority_weight"] == 1.5
        assert ev.payload["status"] == "pending"

    @pytest.mark.parametrize(
        "method,kwargs,event_type,expect_payload",
        [
            (
                "approve",
                {"note": "已核实"},
                "entry_approved",
                {"from_status": "pending", "to_status": "approved", "note": "已核实"},
            ),
            (
                "reject",
                {"note": "误报"},
                "entry_rejected",
                {"from_status": "pending", "to_status": "rejected", "note": "误报"},
            ),
        ],
    )
    def test_transition_emits_event(
        self, pair, method, kwargs, event_type, expect_payload
    ):
        log, q = pair
        entry_id = q.add(make_report())
        getattr(q, method)(entry_id, **kwargs)
        events = log.query(event_type=event_type)
        assert len(events) == 1
        ev = events[0]
        assert ev.entry_id == entry_id
        for key, value in expect_payload.items():
            assert ev.payload[key] == value

    def test_mark_submitted_emits_event(self, pair):
        log, q = pair
        entry_id = q.add(make_report())
        q.approve(entry_id)
        q.mark_submitted(entry_id, actor="系统")
        events = log.query(event_type="entry_marked_submitted")
        assert events[0].payload == {
            "from_status": "approved",
            "to_status": "submitted",
            "note": "",
        }
        assert events[0].actor == "系统"

    def test_annotate_emits_event_with_weights(self, pair):
        log, q = pair
        entry_id = q.add(make_report(), priority_weight=1.0)
        q.annotate(entry_id, priority_weight=2.5, actor="张三")
        events = log.query(event_type="entry_annotated")
        assert len(events) == 1
        assert events[0].payload == {
            "priority_weight": 2.5,
            "previous_weight": 1.0,
        }
        assert events[0].actor == "张三"

    def test_actor_default_empty(self, pair):
        log, q = pair
        entry_id = q.add(make_report())
        q.approve(entry_id)
        assert all(ev.actor == "" for ev in log.iter_events())

    def test_illegal_transition_emits_no_event(self, pair):
        """状态机校验先于事件:非法迁移抛 ValueError 且账本零事件。"""
        log, q = pair
        entry_id = q.add(make_report())
        q.approve(entry_id)
        before = log.latest_seq()
        with pytest.raises(ValueError, match="只有"):
            q.approve(entry_id)  # approved 不能再确认
        with pytest.raises(ValueError, match="只有"):
            q.reject(entry_id)  # approved 不能驳回
        with pytest.raises(ValueError, match="不存在"):
            q.mark_submitted(424242)
        assert log.latest_seq() == before  # 一条事件都没多

    def test_append_failure_rolls_back_state(self, pair, monkeypatch):
        """账本 append 失败 → 状态写入整体回滚(账本与状态都不动)。"""
        log, q = pair
        entry_id = q.add(make_report())

        def boom(*args, **kwargs):
            raise RuntimeError("账本不可用")

        monkeypatch.setattr(log, "append", boom)
        with pytest.raises(RuntimeError, match="账本不可用"):
            q.approve(entry_id)
        fresh = ReviewQueue(q.db_path)
        try:
            assert fresh.get(entry_id).status == "pending"  # 状态未变
        finally:
            fresh.close()
        assert len(log.query(event_type="entry_approved")) == 0  # 账本未记

    def test_crash_window_leads_ledger_by_one_event(self, pair, monkeypatch):
        """崩溃窗口:事件落盘后、状态提交前终止 → 账本多一条未生效事件。"""
        log, q = pair
        entry_id = q.add(make_report())
        real_append = log.append

        def append_then_die(*args, **kwargs):
            real_append(*args, **kwargs)
            raise RuntimeError("模拟崩溃")

        monkeypatch.setattr(log, "append", append_then_die)
        with pytest.raises(RuntimeError):
            q.approve(entry_id)
        monkeypatch.undo()
        # 状态仍是 pending(写入已回滚),账本已多出 entry_approved。
        assert q.get(entry_id).status == "pending"
        approved = log.query(event_type="entry_approved")
        assert len(approved) == 1 and approved[0].entry_id == entry_id

    def test_full_ledger_covers_all_five_event_types(self, pair):
        log, q = pair
        e1 = q.add(make_report("http://a.test"))
        e2 = q.add(make_report("http://b.test"))
        q.annotate(e2, priority_weight=1.0)
        q.approve(e1)
        q.mark_submitted(e1)
        q.reject(e2)
        types = [ev.event_type for ev in log.iter_events()]
        assert types == [
            "entry_added",
            "entry_added",
            "entry_annotated",
            "entry_approved",
            "entry_marked_submitted",
            "entry_rejected",
        ]

    def test_close_does_not_close_ledger(self, pair):
        """队列不拥有账本生命周期:队列 close 后账本仍可写可读。"""
        log, q = pair
        q.add(make_report())
        q.close()
        seq = log.append("entry_added", 99)
        assert log.query(entry_id=99)[0].seq == seq

