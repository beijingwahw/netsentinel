"""webui/dashboard.py 纯逻辑层测试(A56,离线,零外呼)。

- 只测纯函数(trend_rows / agreement_matrix / graph_rows / policy_preview),
  不依赖 streamlit——本文件顶部成功 import 即证明纯逻辑层可独立导入;
- 政策引擎分支用 pytest.importorskip 保护(A49 缺失时自动跳过),
  "政策引擎未就位"分支用 monkeypatch 往 sys.modules 注入 None 模拟缺失;
- 全部用内存对象 / 字面量构造,不建数据库、不联网、不访问真实门户。
"""
from __future__ import annotations

import sys

import pytest

from webui.dashboard import (
    AGREEMENT_TOLERANCE,
    ACTION_CN,
    agreement_matrix,
    graph_rows,
    ledger_health,
    ledger_path_from_cfg,
    policy_preview,
    trend_rows,
)

from netsentinel import telemetry
from netsentinel.contracts import ImageEvidence, ImageScore, SiteReport, Verdict
from netsentinel.decision.review_queue import Entry


# ---------------------------------------------------------------------------
# 造数小工具
# ---------------------------------------------------------------------------

def _entry(
    entry_id: int, created_at: str, status: str = "pending"
) -> Entry:
    return Entry(
        id=entry_id,
        site_url=f"https://s{entry_id}.example.com",
        verdict="suspect",
        status=status,
        created_at=created_at,
    )


def _score(path: str, model: str, prob: float) -> ImageScore:
    return ImageScore(
        image=ImageEvidence(path=path, url=f"https://x/{path}", source_page="https://x/"),
        model=model,
        nsfw_prob=prob,
    )


def _report(
    verdict: Verdict = Verdict.NSFW, agg: float = 0.95, needs_review: bool = True
) -> SiteReport:
    return SiteReport(
        site_url="https://x.example.com",
        verdict=verdict,
        agg_nsw_prob=agg,
        needs_review=needs_review,
    )


# ---------------------------------------------------------------------------
# trend_rows:跨日聚合 / 非法日期跳过 / 未知状态 / 空
# ---------------------------------------------------------------------------

def test_trend_rows_cross_day_aggregation():
    rows = trend_rows(
        [
            _entry(1, "2026-09-29T10:00:00+08:00", "pending"),
            _entry(2, "2026-09-29T23:59:59+08:00", "approved"),
            _entry(3, "2026-09-29T12:00:00+08:00", "rejected"),
            _entry(4, "2026-09-30T08:00:00+08:00", "submitted"),
            _entry(5, "2026-09-30T09:00:00+08:00", "pending"),
        ]
    )
    assert [r["date"] for r in rows] == ["2026-09-29", "2026-09-30"]  # 日期升序
    assert rows[0] == {
        "date": "2026-09-29",
        "pending": 1,
        "approved": 1,
        "rejected": 1,
        "submitted": 0,
        "total": 3,
    }
    assert rows[1]["pending"] == 1
    assert rows[1]["submitted"] == 1
    assert rows[1]["rejected"] == 0
    assert rows[1]["total"] == 2


def test_trend_rows_skips_invalid_dates():
    rows = trend_rows(
        [
            _entry(1, "2026-09-30T10:00:00+08:00", "pending"),
            _entry(2, "not-a-date", "pending"),
            _entry(3, "", "approved"),
            _entry(4, "2026-9-30", "pending"),  # 未零填充的日期不是合法 ISO
        ]
    )
    assert rows == [
        {
            "date": "2026-09-30",
            "pending": 1,
            "approved": 0,
            "rejected": 0,
            "submitted": 0,
            "total": 1,
        }
    ]


def test_trend_rows_unknown_status_counts_total_only():
    rows = trend_rows(
        [
            _entry(1, "2026-09-30T10:00:00+08:00", "weird"),
            _entry(2, "2026-09-30T11:00:00+08:00", "pending"),
        ]
    )
    assert len(rows) == 1
    assert rows[0]["total"] == 2  # 未知状态不丢行数,只进 total
    assert rows[0]["pending"] == 1
    assert rows[0]["approved"] == 0


def test_trend_rows_empty_inputs():
    assert trend_rows([]) == []
    assert trend_rows(None) == []  # 容忍 None


def test_trend_rows_accepts_dict_entries():
    rows = trend_rows(
        [{"created_at": "2026-10-01T00:00:00+00:00", "status": "pending"}]
    )
    assert rows == [
        {
            "date": "2026-10-01",
            "pending": 1,
            "approved": 0,
            "rejected": 0,
            "submitted": 0,
            "total": 1,
        }
    ]


# ---------------------------------------------------------------------------
# agreement_matrix:一致率端点 / 配对去重对称 / 单模型 / 空
# ---------------------------------------------------------------------------

def test_agreement_perfect_agreement_is_one():
    scores = [
        _score("a.png", "flash", 0.91),
        _score("a.png", "pro", 0.90),
        _score("b.png", "flash", 0.12),
        _score("b.png", "pro", 0.10),
    ]
    result = agreement_matrix(scores)
    assert result["models"] == ["flash", "pro"]
    assert result["matrix"][("flash", "pro")] == pytest.approx(1.0)


def test_agreement_complete_disagreement_is_zero():
    scores = [
        _score("a.png", "flash", 0.95),
        _score("a.png", "pro", 0.05),
        _score("b.png", "flash", 0.90),
        _score("b.png", "pro", 0.10),
    ]
    result = agreement_matrix(scores)
    assert result["matrix"][("flash", "pro")] == pytest.approx(0.0)


def test_agreement_partial_rate_is_fraction():
    # 四张同图配对:两张一致(差 0.05、0.2),两张分歧(差 0.9、0.7)→ 0.5
    scores = [
        _score("a.png", "flash", 0.50),
        _score("a.png", "pro", 0.55),
        _score("b.png", "flash", 0.70),
        _score("b.png", "pro", 0.50),  # 恰好 0.2:浮点边界仍记一致
        _score("c.png", "flash", 0.95),
        _score("c.png", "pro", 0.05),
        _score("d.png", "flash", 0.90),
        _score("d.png", "pro", 0.20),
    ]
    result = agreement_matrix(scores)
    assert result["matrix"][("flash", "pro")] == pytest.approx(0.5)


def test_agreement_boundary_tolerance_inclusive():
    # 0.9 - 0.7 = 0.2000000000000000_7(二进制误差),数学上 ≤ 0.2 仍应一致
    scores = [
        _score("a.png", "m1", 0.9),
        _score("a.png", "m2", 0.7),
    ]
    assert agreement_matrix(scores)["matrix"][("m1", "m2")] == pytest.approx(1.0)
    assert AGREEMENT_TOLERANCE == 0.2


def test_agreement_pair_keys_dedup_symmetric():
    # 三个模型 → 恰好 3 个无序对;键规范为 (小, 大),不出现 (b, a) 重复
    scores = [
        _score("a.png", "zz", 0.5),
        _score("a.png", "mm", 0.5),
        _score("a.png", "aa", 0.5),
        _score("b.png", "zz", 0.1),
        _score("b.png", "mm", 0.9),
    ]
    result = agreement_matrix(scores)
    assert result["models"] == ["aa", "mm", "zz"]  # 去重 + 排序
    assert set(result["matrix"]) == {("aa", "mm"), ("aa", "zz"), ("mm", "zz")}
    assert ("mm", "aa") not in result["matrix"]  # 配对去重:无反向重复键
    assert result["matrix"][("aa", "mm")] == pytest.approx(1.0)
    assert result["matrix"][("aa", "zz")] == pytest.approx(1.0)
    # mm/zz 共享两张图:a.png 双方都 0.5(一致)、b.png 0.9 vs 0.1(分歧)→ 0.5
    assert result["matrix"][("mm", "zz")] == pytest.approx(0.5)


def test_agreement_single_model_has_empty_matrix():
    scores = [
        _score("a.png", "stub", 0.5),
        _score("b.png", "stub", 0.9),
    ]
    assert agreement_matrix(scores) == {"models": ["stub"], "matrix": {}}


def test_agreement_empty_and_none_inputs():
    assert agreement_matrix([]) == {"models": [], "matrix": {}}
    assert agreement_matrix(None) == {"models": [], "matrix": {}}


def test_agreement_accepts_dict_scores_and_skips_unusable():
    scores = [
        # as_dict 形态:image 直接是路径字符串
        {"image": "a.png", "model": "flash", "nsfw_prob": 0.5},
        {"image": "a.png", "model": "pro", "nsfw_prob": 0.5},
        # 不可用行:缺模型 / 缺路径 / 缺概率 / 概率非数字
        {"image": "a.png", "model": "", "nsfw_prob": 0.5},
        {"image": "", "model": "m", "nsfw_prob": 0.5},
        {"image": "a.png", "model": "m"},
        {"image": "a.png", "model": "m", "nsfw_prob": "high"},
    ]
    result = agreement_matrix(scores)
    assert result["models"] == ["flash", "pro"]
    assert result["matrix"][("flash", "pro")] == pytest.approx(1.0)


def test_agreement_groups_by_image_path():
    # 不同图片同名模型不互配;同一路径才构成配对
    scores = [
        _score("a.png", "m1", 0.1),
        _score("b.png", "m2", 0.9),  # m1/m2 无共同图片 → 不入矩阵
    ]
    result = agreement_matrix(scores)
    assert result["models"] == ["m1", "m2"]
    assert result["matrix"] == {}


# ---------------------------------------------------------------------------
# graph_rows:site→site 过滤 / 字段 / 容错
# ---------------------------------------------------------------------------

def _sample_export() -> dict:
    return {
        "nodes": [
            {"id": "site:https://a.example.com", "kind": "site", "meta": {}},
            {"id": "site:https://b.example.com", "kind": "site", "meta": {}},
            {"id": "image:sha-abc", "kind": "image", "meta": {}},
        ],
        "edges": [
            {
                "src": "site:https://a.example.com",
                "dst": "site:https://b.example.com",
                "kind": "shared_image",
                "weight": 0.5,
                "created_at": "2026-10-01T00:00:00+08:00",
            },
            {  # site → image:非 site→site,过滤
                "src": "site:https://a.example.com",
                "dst": "image:sha-abc",
                "kind": "shared_image",
                "weight": 1.0,
                "created_at": "x",
            },
            {  # image → site:过滤
                "src": "image:sha-abc",
                "dst": "site:https://b.example.com",
                "kind": "phash_near",
                "weight": 0.9,
                "created_at": "x",
            },
            {  # 无前缀的历史数据:视为非站点边,过滤
                "src": "https://legacy.example.com",
                "dst": "https://other.example.com",
                "kind": "redirect",
                "weight": 1.0,
                "created_at": "x",
            },
            {
                "src": "site:https://c.example.com",
                "dst": "site:https://d.example.com",
                "kind": "redirect",
                "weight": 1,
                "created_at": "x",
            },
        ],
    }


def test_graph_rows_filters_site_to_site_and_strips_prefix():
    rows = graph_rows(_sample_export())
    assert rows == [
        {
            "source": "https://a.example.com",
            "target": "https://b.example.com",
            "kind": "shared_image",
            "weight": 0.5,
        },
        {
            "source": "https://c.example.com",
            "target": "https://d.example.com",
            "kind": "redirect",
            "weight": 1.0,  # int weight 也规整为 float
        },
    ]


def test_graph_rows_field_names_exact():
    (row,) = graph_rows(
        {"edges": [{"src": "site:x", "dst": "site:y", "kind": "redirect"}]}
    )
    assert set(row) == {"source", "target", "kind", "weight"}
    assert row["source"] == "x"
    assert row["target"] == "y"
    assert row["kind"] == "redirect"
    assert row["weight"] == 0.0  # weight 缺失按 0.0,不抛错


def test_graph_rows_tolerates_bad_input():
    assert graph_rows({}) == []
    assert graph_rows(None) == []
    assert graph_rows({"nodes": []}) == []
    assert graph_rows({"edges": ["bad", None, 42]}) == []


# ---------------------------------------------------------------------------
# policy_preview:中文文案 / 兜底 / 未就位 / 非法规则
# ---------------------------------------------------------------------------

def test_policy_preview_hit_four_eyes_in_chinese():
    policy = pytest.importorskip("netsentinel.policy")
    rules = [
        policy.Rule(
            name="高置信须四眼",
            when={"verdict": ["nsfw"]},
            action="four_eyes",
            note="高置信案件双人复核",
        )
    ]
    text = policy_preview(_report(), rules)
    assert text == "命中规则 高置信须四眼 → 动作:四眼复核(高置信案件双人复核)"
    assert "命中规则" in text and "四眼复核" in text
    assert ACTION_CN["four_eyes"] == "四眼复核"


def test_policy_preview_other_actions_and_noteless_rule():
    policy = pytest.importorskip("netsentinel.policy")
    rules = [
        policy.Rule(name="低风险仅记录", when={"verdict": ["clean"]}, action="ignore"),
        policy.Rule(name="兜底入列", when={}, action="queue", note=""),
    ]
    text = policy_preview(_report(verdict=Verdict.CLEAN, agg=0.0), rules)
    # 首条命中生效:clean 命中"低风险仅记录",无备注则省略括号
    assert text == f"命中规则 低风险仅记录 → 动作:{ACTION_CN['ignore']}"
    assert not text.endswith(")")


def test_policy_preview_fallback_when_no_rule_matches():
    pytest.importorskip("netsentinel.policy")
    from netsentinel.policy import Rule

    rules = [Rule(name="只看clean", when={"verdict": ["clean"]}, action="ignore")]
    text = policy_preview(_report(), rules)  # nsfw 不命中任何规则
    assert "未命中规则" in text
    assert ACTION_CN["queue"] in text  # 兜底动作固定 queue
    assert "人工复核" in ACTION_CN["queue"]


def test_policy_preview_engine_missing(monkeypatch):
    pytest.importorskip("netsentinel.policy")  # 真模块缺失时本文件其余用例已跳过
    # 往 sys.modules 注入 None 模拟政策模块不可用(import 立即抛 ImportError)
    monkeypatch.setitem(sys.modules, "netsentinel.policy", None)
    monkeypatch.setitem(sys.modules, "netsentinel.policy.engine", None)
    assert policy_preview(_report(), None) == "政策引擎未就位"


def test_policy_preview_invalid_action_returns_chinese_error():
    pytest.importorskip("netsentinel.policy")
    from netsentinel.policy import Rule

    rules = [Rule(name="坏规则", when={}, action="auto_submit")]  # 非法 action
    text = policy_preview(_report(), rules)
    assert text.startswith("政策决策失败:")
    assert "坏规则" in text  # 保留引擎的中文定位信息


def test_policy_preview_uses_default_rules_when_rules_none():
    pytest.importorskip("netsentinel.policy")
    text = policy_preview(_report(), None)  # rules=None → 内置默认 queue-all
    assert "命中规则" in text
    assert ACTION_CN["queue"] in text


# ---------------------------------------------------------------------------
# 模块守卫:无 streamlit 也能导入(顶部 import 成功已是证明,此处再核入口)
# ---------------------------------------------------------------------------

def test_module_importable_without_streamlit():
    import webui.dashboard as dashboard

    assert isinstance(dashboard._HAS_ST, bool)
    assert callable(dashboard.trend_rows)
    assert callable(dashboard.agreement_matrix)
    assert callable(dashboard.graph_rows)
    assert callable(dashboard.policy_preview)
    assert callable(dashboard.render)
    assert callable(dashboard.main)


# ---------------------------------------------------------------------------
# V5 升级(A101):trend_rows 桶模板单次构造、policy_preview 遥测
# ---------------------------------------------------------------------------

def test_v5_trend_rows_many_entries_same_day_single_bucket():
    # 锁定桶模板优化的行为:N 条同日条目聚为一行、计数精确不丢
    entries = [
        _entry(i, "2026-10-01T%02d:00:00+08:00" % (i % 24), "pending")
        for i in range(500)
    ]
    rows = trend_rows(entries)
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-10-01"
    assert rows[0]["total"] == 500
    assert rows[0]["pending"] == 500
    assert rows[0]["approved"] == 0


def test_v5_trend_rows_mixed_statuses_scale_correctly():
    entries = []
    for i in range(300):
        status = ("pending", "approved", "rejected", "submitted")[i % 4]
        entries.append(_entry(i, "2026-11-0%dT08:00:00+08:00" % (i % 9 + 1), status))
    rows = trend_rows(entries)
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)
    assert sum(r["total"] for r in rows) == 300
    statuses = ("pending", "approved", "rejected", "submitted")
    assert sum(r[s] for r in rows for s in statuses) == 300


def test_v5_agreement_matrix_many_images_correct():
    # 120 张图:m1/m2 半数完全一致、半数差 0.4(> 0.2)→ 一致率恰 0.5
    scores = []
    for i in range(120):
        scores.append(_score(f"img{i}.png", "m1", 0.5))
        scores.append(_score(f"img{i}.png", "m2", 0.5 if i % 2 == 0 else 0.9))
    result = agreement_matrix(scores)
    assert result["models"] == ["m1", "m2"]
    assert result["matrix"][("m1", "m2")] == pytest.approx(0.5)


def test_v5_policy_preview_counts_telemetry():
    pytest.importorskip("netsentinel.policy")
    telemetry.reset()
    policy_preview(_report(), None)
    assert telemetry.snapshot()["counters"].get("dashboard.policy_preview") == 1.0


# ---------------------------------------------------------------------------
# A234:事件账本接线 —— ledger_health(读侧零写入)/ ledger_path_from_cfg
# ---------------------------------------------------------------------------


def _disabled_health() -> dict:
    return {
        "enabled": False,
        "latest_seq": 0,
        "events_total": 0,
        "last_event_type": "",
        "error": "",
    }


def test_ledger_health_disabled_when_none():
    """未接线(None)→ enabled False 的零值健康行(现状口径)。"""
    assert ledger_health(None) == _disabled_health()


def test_ledger_health_reads_real_event_log(tmp_path):
    """真实账本:latest_seq / 事件数 / 最近事件类型如实读出(只读)。"""
    from netsentinel.storage.event_log import EventLog

    ledger = tmp_path / "health.db"
    with EventLog(ledger) as log:
        log.append("entry_added", 1, actor="机器初筛", payload={"site_url": "a"})
        log.append("entry_annotated", 1, actor="张三", payload={})
        health = ledger_health(log)
        assert health == {
            "enabled": True,
            "latest_seq": 2,
            "events_total": 2,
            "last_event_type": "entry_annotated",
            "error": "",
        }
    # 空账本:latest_seq 0、最近事件类型为空串。
    with EventLog(tmp_path / "empty.db") as empty:
        health = ledger_health(empty)
        assert health["enabled"] is True
        assert health["latest_seq"] == 0
        assert health["events_total"] == 0
        assert health["last_event_type"] == ""


def test_ledger_health_swallows_read_errors():
    """读取异常不抛:落入 error 字段的中文提示(健康行绝不让页面崩溃)。"""
    from netsentinel.storage.event_log import EventLogError

    class BrokenLog:
        def latest_seq(self):
            raise EventLogError("事件账本损坏或不是 SQLite 数据库:x.db")

        def iter_events(self):
            raise EventLogError("事件账本损坏或不是 SQLite 数据库:x.db")

    health = ledger_health(BrokenLog())
    assert health["enabled"] is True
    assert health["error"].startswith("账本读取失败:")
    assert "损坏" in health["error"]
    assert health["latest_seq"] == 0 and health["events_total"] == 0


def test_ledger_path_from_cfg_variants():
    """cfg 附加属性 event_ledger 的读取口径:缺省/None/空白 = 关,其余原样。"""
    from types import SimpleNamespace

    # 默认 Config(无该字段)与无属性对象 → 关闭。
    from netsentinel.config import Config

    assert ledger_path_from_cfg(Config()) == ""
    assert ledger_path_from_cfg(SimpleNamespace()) == ""
    # 显式附加属性:调用方 setattr 注入(附加属性口径,不改 Config 定义)。
    assert ledger_path_from_cfg(SimpleNamespace(event_ledger="data/e.db")) == (
        "data/e.db"
    )
    assert ledger_path_from_cfg(SimpleNamespace(event_ledger=None)) == ""
    assert ledger_path_from_cfg(SimpleNamespace(event_ledger="   ")) == ""
    assert ledger_path_from_cfg(SimpleNamespace(event_ledger=" a.db ")) == "a.db"
