"""A110 批量复核与声明(batch_review)单元测试。

覆盖(离线、零外呼、只写 tmp_path):

- 声明校验四分支:缺"人工核实"字样 / 空白审核人 / 正常 / 覆盖刷新;
- is_attested / list_attestations 查询;
- ready_entries:声明门控(未声明排除)、note 组名解析、无组标记回退、
  require=False 放行、batch_max_items 截断标注、portal 透传、仅 approved;
- JSONL 审计写入(可选 audit_path)与缺省零文件副作用;
- SQLite 持久往返(关闭重开);与 ReviewQueue 同库共存。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from netsentinel.contracts import Config, SiteReport, Verdict
from netsentinel.decision import batch_review
from netsentinel.decision.batch_review import (
    Attestation,
    BatchReview,
    ready_entries,
    resolve_group_name,
)
from netsentinel.decision.review_queue import ReviewQueue

# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path, **overrides: object) -> Config:
    """指向 tmp_path 的测试配置(可覆盖批量相关字段)。"""
    kwargs: dict[str, object] = {"db_path": str(tmp_path / "queue.db")}
    kwargs.update(overrides)
    return Config(**kwargs)  # type: ignore[arg-type]


def _approve(
    queue: ReviewQueue,
    url: str,
    note: str = "",
    evidence: str = "",
    verdict: Verdict = Verdict.NSFW,
) -> int:
    """入列一条并立即人工确认,返回条目 id。"""
    eid = queue.add(SiteReport(site_url=url, verdict=verdict), evidence)
    queue.approve(eid, note) if note else queue.approve(eid)
    return eid


def _read_jsonl(path: Path) -> list[dict]:
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


VALID_TEXT = "我已逐站人工核实全部证据,同意批量举报"

# ---------------------------------------------------------------------------
# 声明校验:四分支 + 边界
# ---------------------------------------------------------------------------


class TestAttestValidation:
    def test_text_missing_phrase_raises(self, tmp_path: Path) -> None:
        review = BatchReview(tmp_path / "b.db")
        with pytest.raises(ValueError) as ei:
            review.attest("example.com", 3, "张三", "我大概看过了,没问题")
        assert "人工核实" in str(ei.value)
        assert "批量举报前必须逐组完成证据核验" in str(ei.value)
        review.close()

    def test_empty_text_raises(self, tmp_path: Path) -> None:
        review = BatchReview(tmp_path / "b.db")
        with pytest.raises(ValueError):
            review.attest("example.com", 3, "张三", "")
        review.close()

    def test_blank_reviewer_raises(self, tmp_path: Path) -> None:
        review = BatchReview(tmp_path / "b.db")
        with pytest.raises(ValueError) as ei:
            review.attest("example.com", 3, "   ", VALID_TEXT)
        assert "审核人" in str(ei.value)
        review.close()

    def test_missing_reviewer_none_raises(self, tmp_path: Path) -> None:
        review = BatchReview(tmp_path / "b.db")
        with pytest.raises(ValueError):
            review.attest("example.com", 3, None, VALID_TEXT)  # type: ignore[arg-type]
        review.close()

    def test_ok_returns_attestation(self, tmp_path: Path) -> None:
        review = BatchReview(tmp_path / "b.db")
        att = review.attest("example.com", 3, "张三", VALID_TEXT)
        assert isinstance(att, Attestation)
        assert att.group_name == "example.com"
        assert att.items == 3
        assert att.reviewer == "张三"
        assert att.text == VALID_TEXT
        assert att.ts  # 时间戳非空
        review.close()

    def test_overwrite_updates_fields_and_refreshes_ts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "b.db"
        review = BatchReview(db)
        monkeypatch.setattr(batch_review, "now_iso", lambda: "2026-01-01T00:00:00")
        review.attest("g1", 2, "张三", VALID_TEXT)
        monkeypatch.setattr(batch_review, "now_iso", lambda: "2026-01-02T00:00:00")
        att = review.attest("g1", 5, "李四", VALID_TEXT + ",补充复核")
        atts = review.list_attestations()
        assert len(atts) == 1  # 覆盖而非新增
        assert atts[0].ts == "2026-01-02T00:00:00"  # ts 刷新
        assert atts[0].items == 5 and atts[0].reviewer == "李四"
        assert att.ts == "2026-01-02T00:00:00"
        assert review.is_attested("g1")
        review.close()

    def test_blank_group_name_raises(self, tmp_path: Path) -> None:
        review = BatchReview(tmp_path / "b.db")
        with pytest.raises(ValueError) as ei:
            review.attest("  ", 1, "张三", VALID_TEXT)
        assert "组名" in str(ei.value)
        review.close()

    def test_negative_items_raises(self, tmp_path: Path) -> None:
        review = BatchReview(tmp_path / "b.db")
        with pytest.raises(ValueError) as ei:
            review.attest("g1", -1, "张三", VALID_TEXT)
        assert "不能为负" in str(ei.value)
        review.close()


# ---------------------------------------------------------------------------
# 查询:is_attested / list_attestations
# ---------------------------------------------------------------------------


class TestQueries:
    def test_is_attested_false_initially(self, tmp_path: Path) -> None:
        review = BatchReview(tmp_path / "b.db")
        assert review.is_attested("any.example.com") is False
        review.close()

    def test_is_attested_true_after_attest(self, tmp_path: Path) -> None:
        review = BatchReview(tmp_path / "b.db")
        review.attest("example.com", 1, "张三", VALID_TEXT)
        assert review.is_attested("example.com") is True
        review.close()

    def test_is_attested_false_for_other_group(self, tmp_path: Path) -> None:
        review = BatchReview(tmp_path / "b.db")
        review.attest("a.com", 1, "张三", VALID_TEXT)
        assert review.is_attested("b.com") is False
        review.close()

    def test_list_attestations_empty(self, tmp_path: Path) -> None:
        review = BatchReview(tmp_path / "b.db")
        assert review.list_attestations() == []
        review.close()

    def test_list_attestations_roundtrip_fields(self, tmp_path: Path) -> None:
        review = BatchReview(tmp_path / "b.db")
        review.attest("a.com", 2, "张三", VALID_TEXT)
        review.attest("b.com", 7, "李四", VALID_TEXT + "2")
        atts = review.list_attestations()
        assert [a.group_name for a in atts] == ["a.com", "b.com"]  # 按组名升序
        by_name = {a.group_name: a for a in atts}
        assert by_name["a.com"].items == 2 and by_name["a.com"].reviewer == "张三"
        assert by_name["b.com"].items == 7 and by_name["b.com"].text.endswith("2")

    def test_list_attestations_sorted_deterministic(self, tmp_path: Path) -> None:
        review = BatchReview(tmp_path / "b.db")
        for name in ("z.com", "a.com", "m.com"):
            review.attest(name, 1, "张三", VALID_TEXT)
        assert [a.group_name for a in review.list_attestations()] == [
            "a.com",
            "m.com",
            "z.com",
        ]
        review.close()


# ---------------------------------------------------------------------------
# ready_entries:门控 / 组名 / 截断 / portal
# ---------------------------------------------------------------------------


class TestReadyEntries:
    def test_empty_queue_returns_empty(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        queue = ReviewQueue(cfg.db_path)
        review = BatchReview(cfg.db_path)
        assert ready_entries(cfg, queue=queue, review=review) == []
        queue.close()
        review.close()

    def test_gate_excludes_unattested_group_with_log(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg = _cfg(tmp_path)
        queue = ReviewQueue(cfg.db_path)
        review = BatchReview(cfg.db_path)
        _approve(queue, "https://bad.example.com/x", note="[组:赌球团伙]")
        with caplog.at_level(logging.WARNING, logger="netsentinel.decision.batch_review"):
            items = ready_entries(cfg, queue=queue, review=review)
        assert items == []  # 红线 25:未声明的组不得进入批量清单
        assert "未完成人工核实声明" in caplog.text
        assert "赌球团伙" in caplog.text
        assert "1 条" in caplog.text
        queue.close()
        review.close()

    def test_attested_group_included_fixed_schema(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        queue = ReviewQueue(cfg.db_path)
        review = BatchReview(cfg.db_path)
        eid = _approve(queue, "https://bad.example.com/x", note="[组:赌球团伙]", evidence="e.zip")
        review.attest("赌球团伙", 1, "张三", VALID_TEXT)
        items = ready_entries(cfg, queue=queue, review=review)
        assert len(items) == 1
        assert set(items[0]) == {"entry_id", "group_name", "site_url", "evidence_zip", "portal"}
        assert items[0]["entry_id"] == eid
        assert items[0]["group_name"] == "赌球团伙"
        assert items[0]["site_url"] == "https://bad.example.com/x"
        assert items[0]["evidence_zip"] == "e.zip"
        queue.close()
        review.close()

    def test_note_group_marker_parsed(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        queue = ReviewQueue(cfg.db_path)
        review = BatchReview(cfg.db_path)
        _approve(queue, "https://x1.example.com/", note="[组:棋牌诈骗集团] 批次 2026-10-01")
        review.attest("棋牌诈骗集团", 1, "张三", VALID_TEXT)
        items = ready_entries(cfg, queue=queue, review=review)
        assert [i["group_name"] for i in items] == ["棋牌诈骗集团"]
        queue.close()
        review.close()

    def test_no_marker_falls_back_to_site_name(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        queue = ReviewQueue(cfg.db_path)
        review = BatchReview(cfg.db_path)
        url = "https://www.42labs.io/landing"
        _approve(queue, url)  # 无组标记
        expected = resolve_group_name("", url)
        # 回退结果要么是 canonical(A103 在场),要么是站点 host(缺席)。
        assert expected.lower() in {"42labs.io", "www.42labs.io"}
        review.attest(expected, 1, "张三", VALID_TEXT)
        items = ready_entries(cfg, queue=queue, review=review)
        assert [i["group_name"] for i in items] == [expected]
        queue.close()
        review.close()

    def test_require_false_passes_unattested(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        cfg = _cfg(tmp_path, batch_require_attestation=False)
        queue = ReviewQueue(cfg.db_path)
        review = BatchReview(cfg.db_path)
        _approve(queue, "https://a.example.com/", note="[组:G1]")
        with caplog.at_level(logging.WARNING, logger="netsentinel.decision.batch_review"):
            items = ready_entries(cfg, queue=queue, review=review)
        assert len(items) == 1 and items[0]["group_name"] == "G1"
        assert "未完成人工核实声明" not in caplog.text
        queue.close()
        review.close()

    def test_only_approved_entries_eligible(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        queue = ReviewQueue(cfg.db_path)
        review = BatchReview(cfg.db_path)
        review.attest("G", 1, "张三", VALID_TEXT)
        eid_pending = queue.add(SiteReport(site_url="https://p.example.com/"), "p.zip")
        eid_rejected = queue.add(SiteReport(site_url="https://r.example.com/"), "r.zip")
        queue.reject(eid_rejected)
        eid_submitted = _approve(queue, "https://s.example.com/", note="[组:G]")
        queue.mark_submitted(eid_submitted)
        eid_ok = _approve(queue, "https://ok.example.com/", note="[组:G]")
        items = ready_entries(cfg, queue=queue, review=review)
        assert [i["entry_id"] for i in items] == [eid_ok]
        assert eid_pending != eid_ok
        queue.close()
        review.close()

    def test_max_items_truncation_annotated(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg = _cfg(tmp_path, batch_max_items=3)
        queue = ReviewQueue(cfg.db_path)
        review = BatchReview(cfg.db_path)
        review.attest("G", 5, "张三", VALID_TEXT)
        ids = [
            _approve(queue, f"https://n{i}.example.com/", note="[组:G]") for i in range(5)
        ]
        with caplog.at_level(logging.WARNING, logger="netsentinel.decision.batch_review"):
            items = ready_entries(cfg, queue=queue, review=review)
        assert len(items) == 3
        assert [i["entry_id"] for i in items] == ids[:3]  # 保留队首(id 升序)
        assert "batch_max_items=3" in caplog.text
        assert "已截断保留前 3 条" in caplog.text
        assert "其余 2 条留待下一批" in caplog.text
        queue.close()
        review.close()

    def test_within_limit_no_truncation_log(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg = _cfg(tmp_path, batch_max_items=20)
        queue = ReviewQueue(cfg.db_path)
        review = BatchReview(cfg.db_path)
        review.attest("G", 1, "张三", VALID_TEXT)
        _approve(queue, "https://only.example.com/", note="[组:G]")
        with caplog.at_level(logging.WARNING, logger="netsentinel.decision.batch_review"):
            items = ready_entries(cfg, queue=queue, review=review)
        assert len(items) == 1
        assert "已截断" not in caplog.text
        queue.close()
        review.close()

    def test_only_attested_group_selected_others_counted(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg = _cfg(tmp_path)
        queue = ReviewQueue(cfg.db_path)
        review = BatchReview(cfg.db_path)
        review.attest("G-A", 2, "张三", VALID_TEXT)  # 只声明 G-A
        _approve(queue, "https://a1.example.com/", note="[组:G-A]")
        _approve(queue, "https://a2.example.com/", note="[组:G-A]")
        _approve(queue, "https://b1.example.com/", note="[组:G-B]")
        _approve(queue, "https://b2.example.com/", note="[组:G-B]")
        with caplog.at_level(logging.WARNING, logger="netsentinel.decision.batch_review"):
            items = ready_entries(cfg, queue=queue, review=review)
        assert [i["group_name"] for i in items] == ["G-A", "G-A"]
        assert "2 条" in caplog.text and "G-B" in caplog.text
        queue.close()
        review.close()

    def test_portal_default_12377(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path, batch_require_attestation=False)
        queue = ReviewQueue(cfg.db_path)
        review = BatchReview(cfg.db_path)
        _approve(queue, "https://a.example.com/", note="[组:G]")
        items = ready_entries(cfg, queue=queue, review=review)
        assert items and items[0]["portal"] == "12377"
        queue.close()
        review.close()

    def test_portal_passthrough_shdf(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path, batch_require_attestation=False)
        queue = ReviewQueue(cfg.db_path)
        review = BatchReview(cfg.db_path)
        _approve(queue, "https://a.example.com/", note="[组:G]")
        items = ready_entries(cfg, queue=queue, review=review, portal="shdf")
        assert items and items[0]["portal"] == "shdf"
        queue.close()
        review.close()

    def test_portal_blank_falls_back_to_default(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path, batch_require_attestation=False)
        queue = ReviewQueue(cfg.db_path)
        review = BatchReview(cfg.db_path)
        _approve(queue, "https://a.example.com/", note="[组:G]")
        items = ready_entries(cfg, queue=queue, review=review, portal="   ")
        assert items and items[0]["portal"] == "12377"
        queue.close()
        review.close()

    def test_lazy_default_construction_from_cfg_db(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)  # 不注入 queue/review:验证缺省惰性构造
        queue = ReviewQueue(cfg.db_path)  # 先用真实队列铺数据
        review = BatchReview(cfg.db_path)
        _approve(queue, "https://a.example.com/", note="[组:G]")
        review.attest("G", 1, "张三", VALID_TEXT)
        queue.close()
        review.close()
        items = ready_entries(cfg)  # 缺省路径:自建 ReviewQueue + BatchReview
        assert [i["group_name"] for i in items] == ["G"]

    def test_injected_fakes_decouple_storage(self) -> None:
        fake_entry = SimpleNamespace(
            id=7,
            site_url="https://fake.example.com/",
            evidence_zip="fake.zip",
            note="[组:FAKE]",
        )

        class _FakeQueue:
            def list(self, status: str | None = None) -> list[SimpleNamespace]:
                return [fake_entry] if status == "approved" else []

            def close(self) -> None:  # 不会被调用(注入对象归调用方管理)
                raise AssertionError("注入的 queue 不应由 ready_entries 关闭")

        fake_review = SimpleNamespace(is_attested=lambda g: g == "FAKE")
        cfg = Config(db_path="unused-path.db")  # 注入后 cfg.db_path 不应被触碰
        items = ready_entries(cfg, queue=_FakeQueue(), review=fake_review)  # type: ignore[arg-type]
        assert len(items) == 1
        assert items[0] == {
            "entry_id": 7,
            "group_name": "FAKE",
            "site_url": "https://fake.example.com/",
            "evidence_zip": "fake.zip",
            "portal": "12377",
        }


# ---------------------------------------------------------------------------
# 组名解析
# ---------------------------------------------------------------------------


class TestResolveGroupName:
    def test_marker_basic(self) -> None:
        assert resolve_group_name("[组:ABC] 备注", "https://x.com/") == "ABC"

    def test_marker_fullwidth_colon_and_spaces(self) -> None:
        assert resolve_group_name("前置 [组:　集团甲　] 后置", "https://x.com/") == "集团甲"

    def test_marker_mid_note_first_match_wins(self) -> None:
        assert resolve_group_name("说明 [组:一号] 中 [组:二号]", "https://x.com/") == "一号"

    def test_no_marker_falls_back_host_lowercased(self) -> None:
        got = resolve_group_name("", "https://a.Example.COM:8080/p?q=1")
        assert got.lower() in {"example.com", "a.example.com"}

    def test_no_scheme_still_resolves(self) -> None:
        got = resolve_group_name("", "example.org/path")
        assert got.lower() in {"example.org", ""} or got == "example.org/path"

    def test_garbage_note_falls_back_nonempty(self) -> None:
        # 绝不因解析失败而得到空组名(空组名会让门控永远放不进清单)。
        assert resolve_group_name("没有任何标记", "not a url 造数据")


# ---------------------------------------------------------------------------
# 审计写入(JSONL,可选 audit_path)
# ---------------------------------------------------------------------------


class TestAudit:
    def test_audit_jsonl_written_on_attest(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit" / "batch_audit.jsonl"  # 子目录:自动创建
        review = BatchReview(tmp_path / "b.db", audit_path=audit)
        review.attest("g1", 4, "王五", VALID_TEXT)
        review.close()
        records = _read_jsonl(audit)
        assert len(records) == 1
        rec = records[0]
        assert rec["event"] == "batch_attest"
        assert rec["group_name"] == "g1"
        assert rec["items"] == 4
        assert rec["reviewer"] == "王五"
        assert rec["text"] == VALID_TEXT
        assert rec["overwrite"] is False
        assert rec["ts"]  # JsonlAuditLogger 自动附时间戳

    def test_audit_overwrite_flag_on_second_attest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audit = tmp_path / "a.jsonl"
        review = BatchReview(tmp_path / "b.db", audit_path=audit)
        monkeypatch.setattr(batch_review, "now_iso", lambda: "2026-01-01T00:00:00")
        review.attest("g1", 1, "张三", VALID_TEXT)
        monkeypatch.setattr(batch_review, "now_iso", lambda: "2026-01-01T12:00:00")
        review.attest("g1", 2, "李四", VALID_TEXT)
        review.close()
        records = _read_jsonl(audit)
        assert len(records) == 2
        assert records[0]["overwrite"] is False
        assert records[1]["overwrite"] is True
        assert records[1]["reviewer"] == "李四"

    def test_no_audit_file_by_default(self, tmp_path: Path) -> None:
        before = {p.name for p in tmp_path.rglob("*")}
        review = BatchReview(tmp_path / "b.db")  # 不给 audit_path
        review.attest("g1", 1, "张三", VALID_TEXT)
        review.close()
        after = {p.name for p in tmp_path.rglob("*")}
        new_files = after - before
        # 只允许出现 sqlite 库文件,不允许冒出任何 .jsonl 审计文件。
        assert not any(name.endswith(".jsonl") for name in new_files)
        assert (tmp_path / "b.db").exists()

    def test_failed_validation_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audit = tmp_path / "a.jsonl"
        review = BatchReview(tmp_path / "b.db", audit_path=audit)
        monkeypatch.setattr(batch_review, "now_iso", lambda: "2026-01-01T00:00:00")
        with pytest.raises(ValueError):
            review.attest("g1", 1, "  ", VALID_TEXT)  # 审核人空白
        review.close()
        assert not audit.exists()  # 校验失败:既不落库也不写审计
        assert BatchReview(tmp_path / "b.db").list_attestations() == []


# ---------------------------------------------------------------------------
# SQLite 持久化
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_roundtrip_reopen(self, tmp_path: Path) -> None:
        db = tmp_path / "b.db"
        review = BatchReview(db)
        review.attest("keep.example.com", 6, "赵六", VALID_TEXT)
        review.close()
        reopened = BatchReview(db)  # 关闭后重开:数据仍在
        assert reopened.is_attested("keep.example.com") is True
        atts = reopened.list_attestations()
        assert len(atts) == 1
        assert atts[0].items == 6 and atts[0].reviewer == "赵六"
        assert atts[0].text == VALID_TEXT and atts[0].ts
        reopened.close()

    def test_overwrite_survives_reopen(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "b.db"
        review = BatchReview(db)
        monkeypatch.setattr(batch_review, "now_iso", lambda: "2026-01-01T00:00:00")
        review.attest("g", 1, "张三", VALID_TEXT)
        review.close()
        review = BatchReview(db)
        monkeypatch.setattr(batch_review, "now_iso", lambda: "2026-03-01T00:00:00")
        review.attest("g", 9, "李四", VALID_TEXT + "(更新)")
        review.close()
        atts = BatchReview(db).list_attestations()
        assert len(atts) == 1
        assert atts[0].ts == "2026-03-01T00:00:00"
        assert atts[0].items == 9

    def test_coexists_with_review_queue_same_db(self, tmp_path: Path) -> None:
        db = str(tmp_path / "shared.db")
        queue = ReviewQueue(db)
        review = BatchReview(db, audit_path=tmp_path / "a.jsonl")
        eid = _approve(queue, "https://mix.example.com/", note="[组:同库]")
        review.attest("同库", 1, "张三", VALID_TEXT)
        cfg = Config(db_path=db)
        items = ready_entries(cfg, queue=queue, review=review)
        assert len(items) == 1 and items[0]["entry_id"] == eid
        # 两表互不干扰
        assert queue.get(eid) is not None
        assert len(review.list_attestations()) == 1
        queue.close()
        review.close()

    def test_context_manager_closes(self, tmp_path: Path) -> None:
        with BatchReview(tmp_path / "b.db") as review:
            review.attest("g", 1, "张三", VALID_TEXT)
        assert BatchReview(tmp_path / "b.db").is_attested("g") is True
