"""netsentinel G6 席位新增测试(CONTRACTS-V17 · 创世纪升级波)。

覆盖:

- ``EventLog.append_many`` 批量口:与逐条 ``append`` 落库结果一致(seq /
  类型 / 条目 / 署名 / payload 逐字段相等,重放投影等价)、原子拒绝
  (任一条非法整批拒绝、账本零污染、seq 不受影响)、写入中途 DB 错误
  整批回滚(锁类错误原样上抛、其余中文包装)、空批次零触碰、seq 连续
  且与单条 append 互操作接续;
- 报表渲染消重(html_report 图片循环重复 escape 消重)前后**输出逐字节
  一致**:固定输入的 sha256 快照在改动前冻结,改动后渲染比对;输入只含
  固定文件名(不含机器相关绝对路径),同输入二次渲染亦逐字节一致。

全部离线,只写 tmp_path,零第三方依赖、零外呼。
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from netsentinel.report.batch_report import render_batch_report
from netsentinel.report.html_report import render_report
from netsentinel.report.run_summary import render_run_summary
from netsentinel.storage.event_log import EventLog, EventLogError
from netsentinel.storage.replay import build_projection

# ---------------------------------------------------------------------------
# 固定事件批(覆盖复核域五类事件,批量口与逐条口共用)
# ---------------------------------------------------------------------------

_BATCH: list[dict] = [
    {
        "event_type": "entry_added",
        "entry_id": 1,
        "actor": "张三",
        "payload": {
            "site_url": "http://a.test",
            "verdict": "nsfw",
            "note": "初始备注",
            "priority_weight": 0.5,
            "status": "pending",
        },
    },
    {
        "event_type": "entry_approved",
        "entry_id": 1,
        "actor": "李四",
        "payload": {"from_status": "pending", "to_status": "approved", "note": ""},
    },
    {"event_type": "entry_added", "entry_id": 2, "payload": {"site_url": "http://b.test"}},
    {
        "event_type": "entry_annotated",
        "entry_id": 2,
        "actor": "王五",
        "payload": {"previous_weight": 0, "priority_weight": 1.5},
    },
    {
        "event_type": "entry_marked_submitted",
        "entry_id": 1,
        "actor": "李四",
        "payload": {"from_status": "approved", "to_status": "submitted"},
    },
    {
        "event_type": "entry_rejected",
        "entry_id": 3,
        "actor": "赵六",
        "payload": {"from_status": "pending", "to_status": "rejected"},
    },
]


def _append_one_by_one(log: EventLog, batch: list[dict]) -> list[int]:
    """逐条 append 口消费同一批事件规格(缺省键语义与 append_many 对齐)。"""
    return [
        log.append(
            item["event_type"],
            item["entry_id"],
            actor=item.get("actor", ""),
            payload=item.get("payload"),
        )
        for item in batch
    ]


# ---------------------------------------------------------------------------
# append_many ≡ 逐条 append
# ---------------------------------------------------------------------------


class TestAppendManyEquivalence:
    def test_same_rows_as_one_by_one_append(self, tmp_path: Path):
        with EventLog(tmp_path / "batch.db") as log_batch, EventLog(
            tmp_path / "seq.db"
        ) as log_seq:
            seqs_batch = log_batch.append_many(_BATCH)
            seqs_seq = _append_one_by_one(log_seq, _BATCH)
            assert seqs_batch == seqs_seq == list(range(1, len(_BATCH) + 1))

            def row_key(ev):
                # ts 是落盘墙钟(两次运行天然不同),不参与等价比较。
                return (ev.seq, ev.event_type, ev.entry_id, ev.actor, ev.payload)

            rows_batch = [row_key(e) for e in log_batch.iter_events()]
            rows_seq = [row_key(e) for e in log_seq.iter_events()]
            assert rows_batch == rows_seq

    def test_replay_projection_equivalent(self, tmp_path: Path):
        # 重放侧等价:同一批事件经两个口落库,build_projection 投影逐字段
        # 相同(ts/created_at 依赖墙钟,剔除后比较——语义不变的可证口径)。
        with EventLog(tmp_path / "batch.db") as log_batch, EventLog(
            tmp_path / "seq.db"
        ) as log_seq:
            log_batch.append_many(_BATCH)
            _append_one_by_one(log_seq, _BATCH)
            proj_a, unknown_a = build_projection(log_batch.iter_events())
            proj_b, unknown_b = build_projection(log_seq.iter_events())
            assert unknown_a == unknown_b == 0

            def proj_key(p):
                return (
                    p.entry_id,
                    p.site_url,
                    p.verdict,
                    p.status,
                    p.note,
                    p.evidence_zip,
                    p.priority_weight,
                    tuple((s.seq, s.status) for s in p.status_chain),
                    tuple(
                        (t.seq, t.event_type, t.actor, t.summary)
                        for t in p.timeline
                    ),
                )

            keys_a = {eid: proj_key(p) for eid, p in proj_a.items()}
            keys_b = {eid: proj_key(p) for eid, p in proj_b.items()}
            assert keys_a == keys_b


# ---------------------------------------------------------------------------
# 原子拒绝 / 回滚 / seq 连续
# ---------------------------------------------------------------------------


class TestAppendManyAtomicity:
    @pytest.mark.parametrize(
        "bad",
        [
            {"event_type": "entry_added", "entry_id": True},
            {"event_type": "", "entry_id": 1},
            {"event_type": "x", "entry_id": 1, "actor": 5},
            {"event_type": "x", "entry_id": 1, "payload": ["列表"]},
            {"event_type": "x", "entry_id": 1, "payload": {"s": {1, 2}}},
            {"event_type": "x", "entry_id": 1, "unknown_key": 1},
            ("tuple", 1),
            {"entry_id": 1},
            {"event_type": "x"},
        ],
    )
    def test_any_invalid_item_rejects_whole_batch(self, tmp_path: Path, bad):
        with EventLog(tmp_path / "ev.db") as log:
            prior = log.append("entry_added", 9)
            batch = [
                {"event_type": "entry_added", "entry_id": 1, "payload": {"a": 1}},
                bad,  # 第 2 条非法:整批拒绝,第 1 条也不得落库
                {"event_type": "entry_added", "entry_id": 2},
            ]
            with pytest.raises(EventLogError):
                log.append_many(batch)
            assert log.latest_seq() == prior == 1
            assert len(log.iter_events()) == 1
            # 拒绝后账本不受污染:下一条 append 紧接 prior 续排。
            assert log.append("entry_added", 10) == 2

    def test_db_error_mid_batch_rolls_back_whole_batch_lock_passthrough(
        self, tmp_path: Path, monkeypatch
    ):
        # 写入中途 DB 错误:第一条 INSERT 已在事务内成功,第二条失败 →
        # 整批回滚;锁类(BUSY/LOCKED)错误按契约原样上抛(不误报损坏)。
        with EventLog(tmp_path / "ev.db") as log:
            log.append("entry_added", 1)
            kernel = log._kernel
            orig_exec = kernel._exec
            calls = {"n": 0}

            def flaky_exec(sql, params=None):
                calls["n"] += 1
                if calls["n"] > 1:
                    raise sqlite3.OperationalError("database is locked")
                return orig_exec(sql, params)

            monkeypatch.setattr(kernel, "_exec", flaky_exec)
            with pytest.raises(sqlite3.OperationalError):
                log.append_many(
                    [
                        {"event_type": "entry_added", "entry_id": 2},
                        {"event_type": "entry_added", "entry_id": 3},
                    ]
                )
            monkeypatch.undo()
            assert log.latest_seq() == 1
            assert [e.entry_id for e in log.iter_events()] == [1]
            # 回滚的 INSERT 不占号:后续 append 仍从 2 续排(无洞)。
            assert log.append("entry_added", 4) == 2

    def test_db_error_mid_batch_wraps_chinese(self, tmp_path: Path, monkeypatch):
        # 非锁类 DB 错误 → 中文 EventLogError 包装(与单条 append 同口径),
        # 且同样整批回滚。
        with EventLog(tmp_path / "ev.db") as log:
            log.append("entry_added", 1)
            kernel = log._kernel
            orig_exec = kernel._exec
            calls = {"n": 0}

            def flaky_exec(sql, params=None):
                calls["n"] += 1
                if calls["n"] > 1:
                    raise sqlite3.DatabaseError("disk image is malformed")
                return orig_exec(sql, params)

            monkeypatch.setattr(kernel, "_exec", flaky_exec)
            with pytest.raises(EventLogError, match="批量写入失败"):
                log.append_many(
                    [
                        {"event_type": "entry_added", "entry_id": 2},
                        {"event_type": "entry_added", "entry_id": 3},
                    ]
                )
            monkeypatch.undo()
            assert log.latest_seq() == 1
            assert len(log.iter_events()) == 1

    def test_empty_batch_is_noop(self, tmp_path: Path):
        with EventLog(tmp_path / "ev.db") as log:
            assert log.append_many([]) == []
            assert log.append_many(iter([])) == []
            assert log.latest_seq() == 0
            assert log.iter_events() == []

    def test_seq_continuous_and_interops_with_append(self, tmp_path: Path):
        with EventLog(tmp_path / "ev.db") as log:
            assert log.append("entry_added", 1) == 1
            seqs = log.append_many(_BATCH)
            assert seqs == list(range(2, 2 + len(_BATCH)))
            assert log.append("entry_added", 99) == 2 + len(_BATCH)
            assert log.append_many(_BATCH[:2]) == [
                3 + len(_BATCH),
                4 + len(_BATCH),
            ]
            all_seqs = [e.seq for e in log.iter_events()]
            assert all_seqs == list(range(1, len(all_seqs) + 1))  # 无洞不重


# ---------------------------------------------------------------------------
# 渲染消重前后逐字节一致(固定输入 sha256 快照,改动前冻结)
# ---------------------------------------------------------------------------


class _Entry:
    id = 7
    site_url = "http://例子.test/<script>x</script>"
    verdict = "nsfw"
    created_at = "2026-10-01 09:00:00"
    note = "含 <特殊> & 字符"
    evidence_zip = "evidence/BUNDLE-7.zip"


class _Bundle:
    def __init__(self, dir_path: Path):
        self.dir_path = str(dir_path)


_REPORT_DICT = {
    "agg_nsw_prob": 0.8123,
    "nsw_image_count": 3,
    "pages": [
        {"screenshot_path": "shot_01.png", "images": ["shot_02.jpg", "shot_01.png"]},
        {"screenshot_path": "missing.png"},
        {"images": ["genesis_big_snapshot.png"]},
    ],
    "intel": {
        "url": {"risk": 0.9, "explain": ["可疑域名 <b>特征</b>", "可疑域名 <b>特征</b>"]},
        "text": {"risk": 0.4, "explain": ["文本命中 & 关键词"]},
        "fusion": {
            "prob": 0.7312,
            "rule": "v2",
            "contrib": {
                "image": 1.234,
                "page_vlm": -0.5,
                "unknown_feat": 0.1,
                "url": 0.2,
                "text": 0.05,
            },
        },
    },
}

_BATCH_SUMMARY = {
    "submitted": 2,
    "failed": 1,
    "rate_limited": True,
    "paused": False,
    "results": [
        {"entry_id": 7, "group_name": "组A<script>", "ok": True,
         "submitted": True, "error": "", "portal": "12377"},
        {"entry_id": 8, "group_name": "组B", "ok": False,
         "submitted": False, "error": "验证码超时", "portal": "shdf"},
        {"entry_id": 9, "group_name": "组C", "ok": True,
         "submitted": False, "error": "", "portal": "unknown"},
    ],
    "note": "备注 & <b>粗体</b>",
    "attestations": [{"group_name": "组A", "reviewer": "张三",
                      "ts": "2026-10-01 08:00:00"}],
    # 固定相对文件名(项目根下不存在)→ 稳定命中"文件缺失"分支,快照确定。
    "screenshots": ["genesis_missing_snapshot_9f2c.png"],
    "generated_at": "2026-10-01 09:30:00",
}

_RUN_STATS = {
    "sites": 12,
    "scanned": 11,
    "failed": 1,
    "verdict_dist": {"clean": 5, "suspect": 3, "nsfw": 3, "weird": 0},
    "groups": ["组A", "组B"],
    "tier": "high",
    "workers": 8,
    "cores": 4,
    "cost_est": 1.25,
    "budget_used": 0.31,
    "attest_pending": ["组B", "组<script>"],
    "ready_count": 4,
    "wall_s": 123.4,
    "errors": [("http://a.test", "超时"), ("http://b.test", "DNS 失败")] * 8,
    "generated_at": "2026-10-01 10:00:00",
}

# 改动前(消重前)冻结的输出 sha256:优化后同输入渲染必须逐字节一致。
_SNAP_HTML_FULL = "3df724a5c4e51a4208f2ef4ff622ed8ff34c478fcf16494f932403a93f186b40"
_SNAP_HTML_MINI = "61ffcc64f895845e71c9fe4071e5b5d348fe5e0394d160c987264e591fcd9a1c"
_SNAP_BATCH_HTML = "ca028dbe211532682cd70464fcb8f23c1f1a82e996693d69944fce2892de9d02"
_SNAP_RUN_MD = "5df682f9c40b9671938180a8a08c6060cd60082fe96e6528c08bb873ccb6a30d"
_SNAP_RUN_HTML = "0c8f62dedbeb8de39b152036bf175e39f1af4aaacfd1d57fb24915fddcc08ff8"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TestRenderByteIdentical:
    def _make_images(self, base: Path) -> None:
        (base / "shot_01.png").write_bytes(bytes(range(256)) * 8)
        (base / "shot_02.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"J" * 1000)
        (base / "genesis_big_snapshot.png").write_bytes(b"B" * (60 * 1024 + 1))

    def test_html_report_snapshot_full(self, tmp_path: Path):
        # 覆盖:内嵌图(含同名去重)、缺失文件、超限大图(只列名)、
        # 风险要点去重、中文/特殊字符转义、fusion 贡献前 4 项。
        self._make_images(tmp_path)
        html = render_report(_Entry(), bundle=_Bundle(tmp_path), report_dict=_REPORT_DICT)
        assert _sha(html) == _SNAP_HTML_FULL
        # 消重点位自查:同一文件名的 alt 与 figcaption 使用同一转义结果。
        assert 'alt="shot_01.png"' in html
        assert "shot_01.png(2048 字节)" in html

    def test_html_report_snapshot_mini(self):
        # 无 bundle / 空报告:全占位分支。
        html = render_report(_Entry(), report_dict={})
        assert _sha(html) == _SNAP_HTML_MINI
        # 确定性:同输入二次渲染逐字节一致。
        assert render_report(_Entry(), report_dict={}) == html

    def test_batch_report_snapshot(self, tmp_path: Path):
        html = render_batch_report(
            "BAT-20261001-01", _BATCH_SUMMARY, str(tmp_path / "batch.html")
        )
        assert _sha(html) == _SNAP_BATCH_HTML

    def test_run_summary_snapshot(self, tmp_path: Path):
        md_path, html_path = render_run_summary(_RUN_STATS, str(tmp_path / "run"))
        assert _sha(Path(md_path).read_text(encoding="utf-8")) == _SNAP_RUN_MD
        assert _sha(Path(html_path).read_text(encoding="utf-8")) == _SNAP_RUN_HTML
