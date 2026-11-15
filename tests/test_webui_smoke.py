"""webui/app.py 冒烟测试(A30,离线)。

- 纯逻辑层(verdict_cn / build_entry_card / filter_entries / safe_image_paths /
  apply_data_dir / load_report_dict)不依赖 streamlit,未装 UI 依赖也能跑;
- A233 门户动态化:portal_choices(清单/回退/动态发现)与 build_portal_plan
  (与 v1 planner 语义等价、动态门户、未知/未启用拒绝)同样纯逻辑可测;
- 标签口径统一:四页共享 webui/_labels.py 同一字典对象;
- UI 部分用 pytest.importorskip("streamlit") 保护,缺依赖自动跳过;
- 只写 tmp_path,不联网、不访问真实门户、不启动 streamlit 服务。
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from webui.app import (
    MAX_EXPLAIN_LINES,
    VERDICT_CN_MAP,
    apply_data_dir,
    build_entry_card,
    build_portal_plan,
    filter_entries,
    load_report_dict,
    portal_choices,
    safe_image_paths,
    verdict_cn,
)

from netsentinel import telemetry
from netsentinel.contracts import Verdict
from netsentinel.decision.review_queue import Entry

_MB = 1024 * 1024


# ---------------------------------------------------------------------------
# verdict_cn 映射
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expect"),
    [
        ("clean", "未见违规"),
        ("suspect", "疑似"),
        ("nsfw", "高置信"),
        (Verdict.CLEAN, "未见违规"),
        (Verdict.SUSPECT, "疑似"),
        (Verdict.NSFW, "高置信"),
        ("weird", "未知"),
        ("", "未知"),
        (None, "未知"),
    ],
)
def test_verdict_cn_mapping(raw, expect):
    assert verdict_cn(raw) == expect


# ---------------------------------------------------------------------------
# build_entry_card:卡片组装 + intel 解释提炼
# ---------------------------------------------------------------------------
def _sample_report() -> dict:
    return {
        "site_url": "https://bad.example.com/a",
        "agg_nsw_prob": 0.93,
        "intel": {
            "url": {"risk": 0.8, "explain": ["可疑顶级域 .xyz"]},
            "text": {"risk": 0.7, "explain": ["命中中文色情关键词 12 次"]},
            "page_vlm": {
                "page_nsfw_prob": 0.81,
                "elements": [{"kind": "banner", "desc": "顶部横幅含裸露图像", "prob": 0.9}],
            },
            "fusion": {"prob": 0.9, "contrib": {"image": 2.1, "url": 0.2}, "rule": "只升不降"},
        },
    }


def test_build_entry_card_full():
    entry = Entry(
        id=7,
        site_url="https://bad.example.com/a",
        verdict="nsfw",
        status="pending",
        evidence_zip="data/evidence/bad.example.com_20260101.zip",
        created_at="2026-10-01T10:00:00+08:00",
    )
    report = _sample_report()
    card = build_entry_card(entry, report)

    assert card["id"] == 7
    assert card["site_url"] == entry.site_url
    assert card["verdict_cn"] == "高置信"
    assert card["status"] == "pending"
    assert card["agg"] == pytest.approx(0.93)
    assert card["created_at"] == entry.created_at
    assert card["evidence_zip"] == entry.evidence_zip
    assert card["intel"] is report["intel"]  # 原样透传

    lines = card["explain_lines"]
    assert 0 < len(lines) <= MAX_EXPLAIN_LINES  # ≤ 6 条
    assert all(isinstance(line, str) and line for line in lines)
    assert any("可疑顶级域" in line for line in lines)          # URL explain
    assert any("关键词" in line for line in lines)               # 文本 explain
    assert any("整页风险概率 0.81" in line for line in lines)     # 页面级 VLM
    assert any("裸露" in line for line in lines)                 # 页面元素
    assert any("综合概率 0.90" in line for line in lines)         # 融合概率
    assert any("只升不降" in line for line in lines)              # 融合规则
    assert any("image +2.10" in line for line in lines)          # 融合贡献


def test_build_entry_card_explain_capped_at_six():
    entry = Entry(id=1, site_url="https://x.com", verdict="suspect")
    report = {
        "agg_nsw_prob": 0.5,
        "intel": {
            "url": {"explain": [f"要点{i}" for i in range(10)]},
        },
    }
    card = build_entry_card(entry, report)
    assert len(card["explain_lines"]) == MAX_EXPLAIN_LINES
    assert card["explain_lines"][0].endswith("要点0")  # 保序截断


def test_build_entry_card_without_report():
    entry = Entry(id=3, site_url="https://y.com", verdict="clean", status="rejected")
    card = build_entry_card(entry, None)
    assert card["verdict_cn"] == "未见违规"
    assert card["agg"] == 0.0
    assert card["intel"] == {}
    assert card["explain_lines"] == []


def test_build_entry_card_accepts_dict_entry():
    card = build_entry_card(
        {"id": 9, "site_url": "https://z.com", "verdict": "suspect", "status": "pending"},
        {"agg_nsw_prob": 0.66},
    )
    assert card["id"] == 9
    assert card["verdict_cn"] == "疑似"
    assert card["agg"] == pytest.approx(0.66)


def test_build_entry_card_tolerates_enum_verdict():
    card = build_entry_card(
        {"id": 1, "site_url": "https://e.com", "verdict": Verdict.NSFW},
        {},
    )
    assert card["verdict_cn"] == "高置信"


# ---------------------------------------------------------------------------
# filter_entries:状态 + 子串
# ---------------------------------------------------------------------------
def _entries() -> list[Entry]:
    return [
        Entry(id=1, site_url="https://a.example.com", verdict="suspect", status="pending"),
        Entry(id=2, site_url="https://B.Sample.net/x", verdict="nsfw", status="approved"),
        Entry(id=3, site_url="https://c.example.org", verdict="clean", status="rejected"),
        Entry(id=4, site_url="https://d.other.io", verdict="suspect", status="pending"),
    ]


def test_filter_entries_by_status():
    assert [e.id for e in filter_entries(_entries(), status="pending")] == [1, 4]
    assert [e.id for e in filter_entries(_entries(), status="approved")] == [2]
    assert filter_entries(_entries(), status="submitted") == []


def test_filter_entries_by_substring_case_insensitive():
    assert [e.id for e in filter_entries(_entries(), q="EXAMPLE")] == [1, 3]
    assert [e.id for e in filter_entries(_entries(), q="sample")] == [2]
    assert [e.id for e in filter_entries(_entries(), q="  ")] == [1, 2, 3, 4]  # 空白=不过滤


def test_filter_entries_combined():
    assert [e.id for e in filter_entries(_entries(), status="pending", q="other")] == [4]
    assert filter_entries(_entries(), status="approved", q="example") == []


def test_filter_entries_default_returns_all_and_accepts_dicts():
    entries = _entries()
    assert filter_entries(entries) == entries
    dicts = [{"id": 5, "site_url": "https://x.com", "status": "pending"}]
    assert [d["id"] for d in filter_entries(dicts, status="pending")] == [5]


# ---------------------------------------------------------------------------
# safe_image_paths:存在性 + 大小过滤 + base 解析 + 去重
# ---------------------------------------------------------------------------
def test_safe_image_paths(tmp_path: Path):
    small1 = tmp_path / "a.png"
    small1.write_bytes(b"\x89PNG" + b"0" * 100)
    small2 = tmp_path / "b.png"
    small2.write_bytes(b"\x89PNG" + b"0" * 50)
    big = tmp_path / "big.png"
    big.write_bytes(b"\x89PNG" + b"0" * (8 * _MB + 1))  # 超过 8MB 上限
    rel_dir = tmp_path / "rel"
    rel_dir.mkdir()
    rel_img = rel_dir / "c.png"
    rel_img.write_bytes(b"x" * 10)

    report = {
        "pages": [
            {
                "url": "u1",
                "screenshot_path": str(small1),
                "images": [str(small2), str(tmp_path / "missing.png"), "rel/c.png"],
            },
            {"url": "u2", "images": [str(big)]},
            {"url": "u3", "images": [str(small2)]},  # 重复路径
        ]
    }
    paths = safe_image_paths(report, str(tmp_path))

    assert str(small1) in paths          # 截图也算证据
    assert str(small2) in paths
    assert str(rel_img) in paths         # 相对路径按 base 解析
    assert str(big) not in paths         # 超过 8MB 被过滤
    assert str(tmp_path / "missing.png") not in paths
    assert paths.count(str(small2)) == 1  # 去重
    assert len(paths) == 3


def test_safe_image_paths_empty_inputs(tmp_path: Path):
    assert safe_image_paths({}, str(tmp_path)) == []
    assert safe_image_paths(None, str(tmp_path)) == []
    assert safe_image_paths({"pages": []}, str(tmp_path)) == []
    assert safe_image_paths({"pages": [{"images": [123, None]}]}, str(tmp_path)) == []


# ---------------------------------------------------------------------------
# apply_data_dir:NETSENTINEL_DATA_DIR 覆盖
# ---------------------------------------------------------------------------
def test_apply_data_dir_env_override(tmp_path: Path, monkeypatch):
    from netsentinel.contracts import Config

    monkeypatch.delenv("NETSENTINEL_DATA_DIR", raising=False)
    cfg = apply_data_dir(Config())
    assert cfg.data_dir == "data"            # 未设置环境变量:保持默认
    assert cfg.db_path == "data/review_queue.db"

    monkeypatch.setenv("NETSENTINEL_DATA_DIR", str(tmp_path))
    cfg2 = apply_data_dir(Config())
    assert cfg2.data_dir == str(tmp_path)
    assert cfg2.evidence_dir == str(tmp_path / "evidence")
    assert cfg2.db_path == str(tmp_path / "review_queue.db")
    assert cfg2.audit_path == str(tmp_path / "audit.jsonl")
    assert cfg2.log_path == str(tmp_path / "logs" / "netsentinel.log")
    assert cfg2.vlm_cache_db == str(tmp_path / "vlm_cache.db")

    # env 参数可显式注入(不读进程环境)
    cfg3 = apply_data_dir(Config(), env={})
    assert cfg3.db_path == "data/review_queue.db"
    cfg4 = apply_data_dir(Config(), env={"NETSENTINEL_DATA_DIR": "/tmp/x"})
    assert cfg4.db_path.endswith("review_queue.db") and "x" in cfg4.data_dir


# ---------------------------------------------------------------------------
# load_report_dict:manifest 定位与容错
# ---------------------------------------------------------------------------
def test_load_report_dict(tmp_path: Path):
    bundle = tmp_path / "site_20260101"
    bundle.mkdir()
    report = {
        "site_url": "https://x.com",
        "agg_nsw_prob": 0.5,
        "intel": {"url": {"explain": ["要点"]}},
    }
    (bundle / "manifest.json").write_text(
        json.dumps({"report": report, "files": []}, ensure_ascii=False), encoding="utf-8"
    )
    zip_path = tmp_path / "site_20260101.zip"
    zip_path.write_bytes(b"PK")  # 占位,不真正解压

    assert load_report_dict(str(zip_path)) == report      # 从 zip 路径定位
    assert load_report_dict(str(bundle)) == report        # 传 bundle 目录
    assert load_report_dict(str(bundle / "manifest.json")) == report  # 传 manifest

    assert load_report_dict("") is None
    assert load_report_dict(str(tmp_path / "nope.zip")) is None

    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "manifest.json").write_text("{oops", encoding="utf-8")  # 损坏 JSON
    assert load_report_dict(str(bad)) is None


# ---------------------------------------------------------------------------
# UI 守卫:streamlit 缺失时可导入,存在时可渲染入口
# ---------------------------------------------------------------------------
def test_module_importable_without_streamlit():
    # 本文件顶部已成功 from webui.app import ...:即证明纯逻辑层不依赖 streamlit。
    import webui.app as app

    assert isinstance(app._HAS_ST, bool)
    assert callable(app.main)
    assert callable(app.render)


def test_streamlit_ui_guard():
    pytest.importorskip("streamlit")
    import webui.app as app

    assert app._HAS_ST is True
    assert callable(app.render)
    assert callable(app.main)


# ---------------------------------------------------------------------------
# V5 升级(A101):load_report_dict 的 zip 包内读取 + 坏包容错 +
# build_entry_card 的 webui.card_built 遥测
# ---------------------------------------------------------------------------
def _write_zip(tmp_path: Path, name: str, members: dict[str, object]) -> Path:
    """在 tmp_path 下构造真实 zip 证据包(成员名 → 文本或字节内容)。"""
    target = tmp_path / name
    with zipfile.ZipFile(target, "w") as zf:
        for member, content in members.items():
            zf.writestr(member, content)
    return target


def test_v5_load_report_dict_reads_manifest_inside_zip(tmp_path: Path):
    report = {"site_url": "https://z.com", "agg_nsw_prob": 0.7}
    zip_path = _write_zip(
        tmp_path,
        "site_20260102.zip",
        {"manifest.json": json.dumps({"report": report, "files": []})},
    )
    # 边车 bundle 目录不存在:V5 起回退读 zip 包内 manifest.json
    assert load_report_dict(str(zip_path)) == report


def test_v5_load_report_dict_nested_manifest_inside_zip(tmp_path: Path):
    report = {"site_url": "https://n.com"}
    zip_path = _write_zip(
        tmp_path,
        "site_20260103.zip",
        {"site_20260103/manifest.json": json.dumps({"report": report})},
    )
    assert load_report_dict(str(zip_path)) == report
    # 多个候选子目录 → 无法唯一定位,宁可不猜(返回 None)
    ambiguous = _write_zip(
        tmp_path,
        "site_20260104.zip",
        {
            "a/manifest.json": json.dumps({"report": report}),
            "b/manifest.json": json.dumps({"report": report}),
        },
    )
    assert load_report_dict(str(ambiguous)) is None


def test_v5_load_report_dict_bad_zip_returns_none(tmp_path: Path):
    # 红线级容错:坏 zip 绝不抛异常,一律返回 None
    bad = tmp_path / "broken.zip"
    bad.write_bytes(b"this is not a zip archive at all")
    assert load_report_dict(str(bad)) is None

    empty = tmp_path / "empty.zip"
    empty.write_bytes(b"")
    assert load_report_dict(str(empty)) is None

    assert load_report_dict(str(tmp_path / "missing.zip")) is None  # 文件不存在

    # 包内 manifest 损坏 JSON / 非 UTF-8 字节:同样 None
    corrupt_json = _write_zip(tmp_path, "corrupt_json.zip", {"manifest.json": "{oops"})
    assert load_report_dict(str(corrupt_json)) is None
    gbk_bytes = _write_zip(tmp_path, "gbk.zip", {"manifest.json": b"\xd6\xd0\xce\xc4"})
    assert load_report_dict(str(gbk_bytes)) is None


def test_v5_load_report_dict_sidecar_wins_over_zip_member(tmp_path: Path):
    bundle = tmp_path / "site_20260105"
    bundle.mkdir()
    sidecar_report = {"site_url": "https://side.car"}
    (bundle / "manifest.json").write_text(
        json.dumps({"report": sidecar_report}), encoding="utf-8"
    )
    inner_report = {"site_url": "https://inner.zip"}
    zip_path = _write_zip(
        tmp_path,
        "site_20260105.zip",
        {"manifest.json": json.dumps({"report": inner_report})},
    )
    # 既有布局优先:边车 manifest 命中时不再读 zip 内部
    assert load_report_dict(str(zip_path)) == sidecar_report


def test_v5_build_entry_card_counts_telemetry():
    telemetry.reset()
    build_entry_card({"id": 1, "site_url": "https://t.com", "verdict": "nsfw"}, None)
    build_entry_card({"id": 2, "site_url": "https://t.com", "verdict": "clean"}, {})
    counters = telemetry.snapshot()["counters"]
    assert counters.get("webui.card_built") == 2.0  # 每次成功组装 +1(含空报告)


# ---------------------------------------------------------------------------
# A233 门户动态化:portal_choices / build_portal_plan(纯逻辑层)
# ---------------------------------------------------------------------------
def test_portal_choices_includes_builtin_portals():
    """缺省清单:内置 12377/shdf 在列,展示名非空,无告警。"""
    choices, warning = portal_choices()
    assert {"12377", "shdf"} <= set(choices)
    assert all(str(v).strip() for v in choices.values())
    assert warning == ""


def test_portal_choices_fallback_when_portal_defs_broken(monkeypatch):
    """portal_defs 不可用 → 回退内置 12377/shdf + 中文告警,绝不抛错。"""
    from netsentinel.submit import portal_defs

    def _boom(*args, **kwargs):
        raise RuntimeError("门户模块损坏(测试注入)")

    monkeypatch.setattr(portal_defs, "list_portals", _boom)
    choices, warning = portal_choices()
    assert set(choices) == {"12377", "shdf"}
    assert "回退" in warning

    # 清单为空同样回退(不抛错)
    monkeypatch.setattr(portal_defs, "list_portals", lambda *a, **k: {})
    choices, warning = portal_choices()
    assert set(choices) == {"12377", "shdf"}
    assert "回退" in warning


def test_portal_choices_discovers_dynamic_enabled_portal(tmp_path, monkeypatch):
    """动态门户(enabled: true)进入清单;enabled 缺省/为 false 不进。"""
    from netsentinel.submit import portal_defs

    (tmp_path / "dyn.yaml").write_text(
        "name: 动态测试门户\n"
        "entry_url_key: portal_12377_base\n"
        "category: 测试类目\n"
        "enabled: true\n",
        encoding="utf-8",
    )
    (tmp_path / "off.yaml").write_text(
        "name: 未启用门户\n"
        "entry_url_key: portal_12377_base\n"
        "category: 测试类目\n"
        "enabled: false\n",
        encoding="utf-8",
    )
    real_list = portal_defs.list_portals
    monkeypatch.setattr(
        portal_defs, "list_portals",
        lambda include_disabled=False, directory=None: real_list(
            include_disabled, str(tmp_path)
        ),
    )
    choices, warning = portal_choices()
    assert warning == ""
    assert "dyn" in choices and choices["dyn"] == "动态测试门户"
    assert "off" not in choices  # 红线:未显式 enabled:true 的动态门户不进清单
    assert {"12377", "shdf"} <= set(choices)


def test_build_portal_plan_agrees_with_v1_planners():
    """A233:与 v1 plan_12377/plan_shdf 产出语义等价(步序/标签/类目/首步 meta)。"""
    from netsentinel.contracts import Config
    from netsentinel.submit.portal_12377 import plan_12377
    from netsentinel.submit.portal_shdf import plan_shdf

    cfg = Config(
        portal_12377_base="http://127.0.0.1:8900/",
        portal_shdf_base="http://127.0.0.1:8901/",
    )
    entry = Entry(id=1, site_url="https://bad.example.com/x", verdict="nsfw")
    for portal, v1 in (("12377", plan_12377), ("shdf", plan_shdf)):
        ours = build_portal_plan(entry, portal, cfg)
        theirs = v1(entry, cfg)
        assert [s.action for s in ours.steps] == [s.action for s in theirs.steps]
        assert [s.label for s in ours.steps] == [s.label for s in theirs.steps]
        assert ours.payload.category == theirs.payload.category
        assert ours.entry_url == theirs.entry_url
        assert ours.steps[0].meta["portal"] == theirs.steps[0].meta["portal"]
        assert str(ours.portal) == str(theirs.portal)


def test_build_portal_plan_rejects_unknown_portal():
    """未知门户 → ValueError 中文(动态与内置清单皆无该短名)。"""
    from netsentinel.contracts import Config

    with pytest.raises(ValueError, match="未知的门户标识"):
        build_portal_plan(Entry(id=1, site_url="https://x.com", verdict="nsfw"), "nope", Config())


def test_build_portal_plan_supports_dynamic_portal(tmp_path, monkeypatch):
    """动态门户经 get_portal(显式 enabled:true)可生成计划;未启用的被拒绝。"""
    from netsentinel.contracts import Config
    from netsentinel.submit import portal_defs

    (tmp_path / "dyn.yaml").write_text(
        "name: 动态测试门户\n"
        "entry_url_key: portal_12377_base\n"
        "category: 动态类目\n"
        "enabled: true\n",
        encoding="utf-8",
    )
    (tmp_path / "off.yaml").write_text(
        "name: 未启用门户\n"
        "entry_url_key: portal_12377_base\n"
        "category: 动态类目\n"
        "enabled: false\n",
        encoding="utf-8",
    )
    real_get = portal_defs.get_portal
    monkeypatch.setattr(
        portal_defs, "get_portal",
        lambda name, directory=None: real_get(name, str(tmp_path)),
    )
    cfg = Config(portal_12377_base="http://127.0.0.1:8900/")
    entry = Entry(id=1, site_url="https://bad.example.com/x", verdict="nsfw")

    plan = build_portal_plan(entry, "dyn", cfg)
    assert plan.entry_url == "http://127.0.0.1:8900/"
    assert plan.payload.category == "动态类目"
    assert plan.steps[0].meta["portal"] == "动态测试门户"
    assert any(s.action.value == "human_gate" for s in plan.steps)

    with pytest.raises(ValueError, match="尚未启用"):
        build_portal_plan(entry, "off", cfg)


def test_labels_single_authority_shared_by_pages():
    """标签口径统一:app/groups/runs/dashboard 四页与 _labels 同一字典对象。"""
    import webui._labels as labels
    import webui.dashboard as dashboard
    import webui.groups_page as groups_page
    import webui.runs_page as runs_page

    assert labels.VERDICT_CN == {"clean": "未见违规", "suspect": "疑似", "nsfw": "高置信"}
    assert labels.STATUS_CN == {
        "pending": "待复核", "approved": "已批准", "rejected": "已驳回", "submitted": "已提交",
    }
    assert VERDICT_CN_MAP is labels.VERDICT_CN
    assert groups_page.VERDICT_CN is labels.VERDICT_CN
    assert runs_page.VERDICT_LABELS_CN is labels.VERDICT_CN
    assert dashboard._STATUS_CN is labels.STATUS_CN
