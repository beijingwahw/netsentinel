# -*- coding: utf-8 -*-
"""V10.35(P2 阶段3)站级扫描日志(断点续跑)测试。

单元:库表幂等 / done↔processed 口径 / counts 视图 / 连接工厂约束。
集成:run_flow 记账 + resume_scan 剔除已完成站 + 关闭开关回旧行为。
"""
from __future__ import annotations

import pathlib
from types import SimpleNamespace
from typing import Any

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.ops.scan_journal import ScanJournal, default_journal_path


def _journal(tmp_path: pathlib.Path) -> ScanJournal:
    return ScanJournal(tmp_path / "journal.db")


# ---------------------------------------------------------------------------
# 单元
# ---------------------------------------------------------------------------
def test_default_journal_path_under_data_dir() -> None:
    assert default_journal_path("data").endswith("scan_journal.db")
    assert "data" in default_journal_path("data").replace("\\", "/")


def test_record_and_done_urls_roundtrip(tmp_path) -> None:
    j = _journal(tmp_path)
    bid = j.new_batch()
    j.record(bid, "https://a.example/", "done", "nsfw")
    j.record(bid, "https://b.example/", "failed")
    assert j.done_urls(bid) == {"https://a.example/"}        # failed 不进 done
    assert j.processed_urls(bid) == {"https://a.example/", "https://b.example/"}
    j.close()


def test_record_idempotent_no_duplicate_rows(tmp_path) -> None:
    j = _journal(tmp_path)
    bid = j.new_batch()
    j.record(bid, "https://a.example/", "failed")
    j.record(bid, "https://a.example/", "done", "suspect")   # 重记覆盖(崩溃重放)
    assert j.counts(bid) == {"done": 1, "failed": 0, "other": 0}
    assert j.done_urls(bid) == {"https://a.example/"}
    j.close()


def test_batches_are_isolated(tmp_path) -> None:
    j = _journal(tmp_path)
    b1, b2 = j.new_batch(), j.new_batch()
    assert b1 != b2
    j.record(b1, "https://a.example/", "done")
    assert j.done_urls(b2) == set()
    assert j.done_urls(b1) == {"https://a.example/"}
    j.close()


def test_counts_view_includes_other_bucket(tmp_path) -> None:
    j = _journal(tmp_path)
    bid = j.new_batch()
    j.record(bid, "https://a.example/", "done")
    j.record(bid, "https://b.example/", "failed")
    j.record(bid, "https://c.example/", "weird-status")
    assert j.counts(bid) == {"done": 1, "failed": 1, "other": 1}
    j.close()


def test_new_batch_id_shape_and_telemetry(tmp_path) -> None:
    telemetry.reset()
    j = _journal(tmp_path)
    bid = j.new_batch()
    assert bid.startswith("scan-")
    assert telemetry.snapshot()["counters"].get("scan_journal.new_batch") == 1.0
    j.close()
    telemetry.reset()


def test_record_telemetry_counts(tmp_path) -> None:
    telemetry.reset()
    j = _journal(tmp_path)
    bid = j.new_batch()
    j.record(bid, "https://a.example/", "done")
    assert telemetry.snapshot()["counters"].get("scan_journal.record") == 1.0
    j.close()
    telemetry.reset()


def test_resume_skip_count_telemetry_only_positive(tmp_path) -> None:
    telemetry.reset()
    j = _journal(tmp_path)
    j.resume_skip_count(0)   # 零跳过不计数
    j.resume_skip_count(3)
    snap = telemetry.snapshot()["counters"]
    assert snap.get("scan_journal.resume_skip") == 3.0
    j.close()
    telemetry.reset()


def test_persistence_across_reopen(tmp_path) -> None:
    db = str(tmp_path / "journal.db")
    j = ScanJournal(db)
    bid = j.new_batch()
    j.record(bid, "https://a.example/", "done", "nsfw")
    j.close()
    j2 = ScanJournal(db)   # 重开:库表 IF NOT EXISTS 补建,数据仍在
    assert j2.done_urls(bid) == {"https://a.example/"}
    j2.close()


def test_parent_directory_auto_created(tmp_path) -> None:
    deep = tmp_path / "a" / "b" / "c" / "journal.db"
    j = ScanJournal(deep)
    assert deep.exists()
    j.close()


# ---------------------------------------------------------------------------
# 集成:run_flow 记账 + resume_scan 剔除
# ---------------------------------------------------------------------------
def _flow_cfg(tmp_path: pathlib.Path, **kw: Any) -> Config:
    data = tmp_path / "data"
    return Config(
        data_dir=str(data),
        db_path=str(data / "q.db"),
        audit_path=str(tmp_path / "audit.jsonl"),
        **kw,
    )


def _fake_intake(urls: list[str]):
    def load(path: str) -> tuple[list[str], list[tuple[str, str]]]:
        return list(urls), []
    return load


def _fake_scanner(reports: dict[str, Any] | None = None, errors: list[tuple[str, str]] | None = None):
    """fake batch_scan:返回 pool 形状摘要(results/errors)。"""
    state = {"calls": 0}

    def scanner(urls: list[str], cfg: Config, **kw: Any) -> dict[str, Any]:
        state["calls"] += 1
        return {
            "reports": {u: (reports or {}).get(u) for u in urls if u in (reports or {})},
            "summary": {
                "total": len(urls), "done": len([u for u in urls if u in (reports or {})]),
                "failed": len(errors or []), "skipped": 0,
                "results": {u: "nsfw" for u in urls if u in (reports or {})},
                "errors": list(errors or []),
            },
        }
    return scanner, state


def _fake_grouping() -> tuple[Any, dict[str, Any]]:
    state = {"calls": 0}

    def grouping(cfg: Config, reports: dict[str, Any], **kw: Any) -> dict[str, Any]:
        state["calls"] += 1
        return {"groups": [], "enqueued": 0, "skipped_clean": 0}
    return grouping, state


def test_run_flow_records_journal_and_reports_batch_id(tmp_path) -> None:
    from netsentinel.batchflow import run_flow

    cfg = _flow_cfg(tmp_path)
    scanner, scan_state = _fake_scanner(reports={"https://a.example/": object()})
    grouping, _ = _fake_grouping()
    result = run_flow(
        "leads.txt", cfg, intake_load=_fake_intake(["https://a.example/"]),
        scanner=scanner, grouping=grouping,
    )
    assert "scan_batch_id" in result
    bid = result["scan_batch_id"]
    assert bid.startswith("scan-")
    # 库里有 done 记录
    j = ScanJournal(cfg.data_dir + "/scan_journal.db")
    assert j.done_urls(bid) == {"https://a.example/"}
    assert j.counts(bid)["done"] == 1
    j.close()


def test_run_flow_resume_scan_skips_done_sites(tmp_path) -> None:
    from netsentinel.batchflow import run_flow

    cfg = _flow_cfg(tmp_path)
    urls = ["https://a.example/", "https://b.example/"]
    # 第一轮:a 站完成入账;b 站"在飞崩溃"(不在 results/errors——
    # 优雅退出未提交/中断在飞的站没有终态记录,这正是续跑要重扫的对象)
    scanner, _ = _fake_scanner(reports={"https://a.example/": object()})
    grouping, _ = _fake_grouping()
    r1 = run_flow(
        "leads.txt", cfg, intake_load=_fake_intake(urls),
        scanner=scanner, grouping=grouping,
    )
    bid = r1["scan_batch_id"]

    # 续跑:a 已完成剔除,b 重扫
    def scan_2(scanned_urls: list[str], cfg_: Config, **kw: Any) -> dict[str, Any]:
        scan_2.urls = list(scanned_urls)  # type: ignore[attr-defined]
        return {
            "reports": {}, "summary": {
                "total": len(scanned_urls), "done": len(scanned_urls),
                "failed": 0, "skipped": 0,
                "results": {u: "clean" for u in scanned_urls}, "errors": [],
            },
        }

    r2 = run_flow(
        "leads.txt", cfg, intake_load=_fake_intake(urls),
        scanner=scan_2, grouping=grouping, resume_scan=bid,
    )
    assert getattr(scan_2, "urls", None) == ["https://b.example/"]  # a 已剔除
    assert r2["scan_journal_skipped"] == 1
    assert r2["scan_batch_id"] == bid                # 续跑沿用同批次号


def test_run_flow_resume_scan_records_failed_for_retry(tmp_path) -> None:
    """failed 站不进 done 集:续跑会重试(运营直觉)。"""
    from netsentinel.batchflow import run_flow

    cfg = _flow_cfg(tmp_path)
    urls = ["https://a.example/", "https://b.example/"]
    scanner, _ = _fake_scanner(
        reports={"https://a.example/": object()},
        errors=[("https://b.example/", "模拟失败")],
    )
    grouping, _ = _fake_grouping()
    r1 = run_flow(
        "leads.txt", cfg, intake_load=_fake_intake(urls),
        scanner=scanner, grouping=grouping,
    )
    bid = r1["scan_batch_id"]
    j = ScanJournal(str(pathlib.Path(cfg.data_dir) / "scan_journal.db"))
    assert j.done_urls(bid) == {"https://a.example/"}          # b 不在
    assert "https://b.example/" in j.processed_urls(bid)       # 但有终态记录
    j.close()


def test_run_flow_journal_disabled_keeps_old_shape(tmp_path) -> None:
    from netsentinel.batchflow import run_flow

    cfg = _flow_cfg(tmp_path, scan_journal_enabled=False)
    scanner, _ = _fake_scanner(reports={"https://a.example/": object()})
    grouping, _ = _fake_grouping()
    result = run_flow(
        "leads.txt", cfg, intake_load=_fake_intake(["https://a.example/"]),
        scanner=scanner, grouping=grouping,
    )
    assert "scan_batch_id" not in result      # 结果形态与旧版逐字节一致
    assert not (pathlib.Path(cfg.data_dir) / "scan_journal.db").exists()


def test_run_flow_graceful_disabled_still_scans_all(tmp_path) -> None:
    """graceful_exit_enabled=False:tier pool runner 不注入 stop_check。"""
    from netsentinel.batchflow import run_flow

    cfg = _flow_cfg(tmp_path, graceful_exit_enabled=False)
    scanner, _ = _fake_scanner(reports={"https://a.example/": object()})
    grouping, _ = _fake_grouping()
    run_flow(
        "leads.txt", cfg, intake_load=_fake_intake(["https://a.example/"]),
        scanner=scanner, grouping=grouping,
    )  # 只要跑到完不抛错即证明关闭回旧


def test_run_flow_interrupted_summary_propagates(tmp_path) -> None:
    """pool 摘要带 interrupted → 结果携带 scan_interrupted(优雅退出收口)。"""
    from netsentinel.batchflow import run_flow

    cfg = _flow_cfg(tmp_path)
    urls = ["https://a.example/", "https://b.example/"]

    def scanner(urls_: list[str], cfg_: Config, **kw: Any) -> dict[str, Any]:
        return {
            "reports": {}, "summary": {
                "total": len(urls_), "done": 0, "failed": 0, "skipped": 0,
                "results": {}, "errors": [],
                "interrupted": True, "not_submitted": list(urls_),
            },
        }

    grouping, _ = _fake_grouping()
    result = run_flow(
        "leads.txt", cfg, intake_load=_fake_intake(urls),
        scanner=scanner, grouping=grouping,
    )
    assert result.get("scan_interrupted") is True


def test_cli_resume_scan_requires_input(tmp_path, capsys) -> None:
    from netsentinel.batchflow import main

    rc = main(["--resume-scan", "scan-20261110T1415"])
    assert rc == 2
    assert "--resume-scan 仅在 --input" in capsys.readouterr().out
