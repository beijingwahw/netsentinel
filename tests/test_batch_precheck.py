"""V10.1 批量预审 + 半自动焦点测试。"""
from __future__ import annotations

import io
import types

import pytest

from netsentinel.contracts import Config, StepAction
from netsentinel.submit.batch_precheck import BatchPrecheck, preview_table
from netsentinel.submit.form_models import build_plan, build_payload, SELECTORS


FAKE_ITEMS = [
    {"entry_id": 1, "group_name": "a.example.com", "site_url": "http://a.example.com/",
     "verdict": "nsfw", "evidence_zip": "a.zip", "reason": "a.example.com抽查3页,3图涉色情,已核实",
     "portal": "12377"},
    {"entry_id": 2, "group_name": "b.example.org", "site_url": "http://b.example.org/",
     "verdict": "suspect", "evidence_zip": "b.zip", "reason": "b.example.org涉嫌传播色情,已经人工核实",
     "portal": "12377"},
]


class TestPreviewTable:
    def test_headers_and_rows(self):
        table = preview_table(FAKE_ITEMS)
        assert "组名" in table and "站点" in table and "理由" in table
        assert "a.example.com" in table and "b.example.org" in table
        assert "共 2 条" in table

    def test_empty(self):
        assert "无待批量条目" in preview_table([])


class TestBatchPrecheck:
    def _make(self, items, confirm="Y", dry=True):
        cfg = Config()
        cfg.data_dir = "/tmp/ns_precheck_test"
        stdin = io.StringIO()
        stdout = io.StringIO()
        agent = types.SimpleNamespace()
        agent.run = lambda its, c, dry_run=None, on_item=None: {
            "result": {"submitted": len(its) if not dry_run else 0,
                       "failed": 0, "results": []},
            "report_path": "/tmp/report.html",
        }
        pc = BatchPrecheck(cfg, ready_fn=lambda c: items, agent=agent,
                           stdin=stdin, stdout=stdout)
        return pc, stdin, stdout

    def test_confirm_y_executes(self):
        pc, stdin, stdout = self._make(FAKE_ITEMS, "Y")
        stdin.write("Y\n")
        stdin.seek(0)
        result = pc.run(dry_run=True)
        out = stdout.getvalue()
        assert "批量预审" in out and "共 2 条" in out
        assert result["submitted"] == 0  # dry_run

    def test_confirm_n_cancels(self):
        pc, stdin, stdout = self._make(FAKE_ITEMS)
        stdin.write("N\n")
        stdin.seek(0)
        result = pc.run()
        out = stdout.getvalue()
        assert "已取消" in out
        assert result.get("cancelled") is True

    def test_empty_items_skips(self):
        pc, _, stdout = self._make([])
        result = pc.run()
        assert "无待批量条目" in stdout.getvalue()
        assert result.get("skipped") is True

    def test_collect_ready_with_reason(self):
        cfg = Config()
        cfg.data_dir = "/tmp/ns_pc_r"
        pc = BatchPrecheck(cfg, ready_fn=lambda c: [
            {"entry_id": 1, "site_url": "http://x.example.com/", "nsw_image_count": 3, "pages": [1, 2, 3]}
        ])
        items = pc.collect_ready()
        assert items[0]["reason"] and len(items[0]["reason"]) <= 39


class TestFocusStep:
    def test_plan_has_focus_before_human_gate(self):
        from netsentinel.contracts import Portal
        cfg = Config()
        e = types.SimpleNamespace(site_url="http://x/", verdict="nsfw", evidence_zip="z")
        payload = build_payload(e, Portal.P12377, cfg)
        plan = build_plan(payload, cfg)
        focus_steps = [s for s in plan.steps if s.action is StepAction.FOCUS]
        assert len(focus_steps) == 1
        assert focus_steps[0].selector == SELECTORS["captcha"]
        # FOCUS 在 HUMAN_GATE 之前
        idx_focus = plan.steps.index(focus_steps[0])
        idx_gate = next(i for i, s in enumerate(plan.steps) if s.action is StepAction.HUMAN_GATE)
        assert idx_focus < idx_gate
        # HUMAN_GATE 文案引导用户到浏览器
        gate = plan.steps[idx_gate]
        assert "浏览器" in gate.label and "回车" in gate.label
