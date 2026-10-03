"""webui/groups_page.py 纯逻辑层测试(A115,离线,零外呼,不依赖 streamlit)。

- 只测纯函数(group_rows / attest_badge / batch_preview / portal_cn)及
  展示常量:本文件顶部成功 import 即证明纯逻辑层可独立导入(本机未装
  streamlit 时同样成立);
- group_rows 用真实兄弟模块 A104 的 CaseGroup 与 group_entries 做小型
  集成校验(只读引用,惰性口径与页面一致);
- 全部用内存对象 / 字面量构造,不建数据库、不联网、不访问真实门户、
  不启动 streamlit 服务。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from webui.groups_page import (
    BADGE_ATTESTED,
    BADGE_UNATTESTED,
    PORTAL_CN,
    VERDICT_CN,
    attest_badge,
    batch_preview,
    group_rows,
    portal_cn,
)

from netsentinel.contracts import Config, Verdict
from netsentinel.intel.case_group import CaseGroup, group_entries

# ---------------------------------------------------------------------------
# 造数小工具
# ---------------------------------------------------------------------------

_ROW_KEYS = {"name", "entries", "sites", "aliases", "agg_max", "verdict", "attested"}


def _group(
    name: str = "example.com",
    *,
    aliases: list[str] | None = None,
    entry_ids: list[int] | None = None,
    site_urls: list[str] | None = None,
    agg_max: float = 0.9,
    verdict: str = "nsfw",
) -> CaseGroup:
    """构造一个典型案件组(默认:3 条目 / 2 站点 / 4 别名 / nsfw / 0.9)。"""
    return CaseGroup(
        name=name,
        aliases=aliases
        if aliases is not None
        else ["example.com", "www.example.com", "img.example.com", "m.example.com"],
        entry_ids=entry_ids if entry_ids is not None else [1, 2, 3],
        site_urls=site_urls
        if site_urls is not None
        else ["https://example.com/", "https://www.example.com/a"],
        agg_max=agg_max,
        verdict=verdict,
    )


# ---------------------------------------------------------------------------
# group_rows:字段 / 中文映射 / attested 集合 / 容错 / 空输入
# ---------------------------------------------------------------------------


def test_group_rows_exact_keys_and_value_types() -> None:
    """每行固定七键,值类型规整(计数 int / 风险分 float / 声明 bool)。"""
    rows = group_rows([_group()])
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == _ROW_KEYS
    assert row["name"] == "example.com"
    assert isinstance(row["entries"], int)
    assert isinstance(row["sites"], int)
    assert isinstance(row["aliases"], int)
    assert isinstance(row["agg_max"], float)
    assert isinstance(row["verdict"], str)
    assert isinstance(row["attested"], bool)


def test_group_rows_counts_match_duck_fields() -> None:
    """条目数 = len(entry_ids),站点数 = len(site_urls),别名数 = len(aliases)。"""
    row = group_rows([_group()])[0]
    assert row["entries"] == 3
    assert row["sites"] == 2
    assert row["aliases"] == 4


def test_group_rows_verdict_cn_mapping() -> None:
    """判定档位 → 中文:clean/suspect/nsfw;Verdict 枚举同样映射。"""
    rows = group_rows(
        [
            _group("a.com", verdict="clean"),
            _group("b.com", verdict="suspect"),
            _group("c.com", verdict="nsfw"),
            CaseGroup(name="d.com", verdict=Verdict.SUSPECT),
        ]
    )
    assert [r["verdict"] for r in rows] == [
        "无风险",
        "疑似",
        "高置信色情",
        "疑似",
    ]


def test_group_rows_unknown_or_missing_verdict() -> None:
    """未知判定原样展示;完全缺失按「未知」,不抛错。"""
    rows = group_rows(
        [
            CaseGroup(name="a.com", verdict="weird"),
            CaseGroup(name="b.com", verdict=""),
        ]
    )
    assert rows[0]["verdict"] == "weird"
    assert rows[1]["verdict"] == "未知"


def test_group_rows_attested_set_membership() -> None:
    """attested 集合按组名命中:集合内 True,集合外 False。"""
    rows = group_rows(
        [_group("alpha.com"), _group("beta.com"), _group("gamma.com")],
        {"beta.com"},
    )
    assert [r["attested"] for r in rows] == [False, True, False]


def test_group_rows_attested_none_and_empty_set() -> None:
    """attested 缺省(None)与空集都视为全部未声明。"""
    groups = [_group("a.com"), _group("b.com")]
    assert all(r["attested"] is False for r in group_rows(groups))
    assert all(r["attested"] is False for r in group_rows(groups, set()))
    # 空名组即使撞上集合里的空串也不算已声明(声明必须落到具体组)。
    assert group_rows([CaseGroup(name="")], {""})[0]["attested"] is False


def test_group_rows_agg_rounded_two_decimals() -> None:
    """agg_max 四舍五入两位小数;非法值按 0.0,不抛错。"""
    rows = group_rows(
        [
            _group("a.com", agg_max=0.876),
            _group("b.com", agg_max=0.999),
            CaseGroup(name="c.com", agg_max="bad"),  # type: ignore[arg-type]
            CaseGroup(name="d.com"),  # 默认 0.0
        ]
    )
    assert rows[0]["agg_max"] == 0.88
    assert rows[1]["agg_max"] == 1.0
    assert rows[2]["agg_max"] == 0.0
    assert rows[3]["agg_max"] == 0.0


def test_group_rows_duck_missing_attrs_tolerated() -> None:
    """鸭子容错:缺属性对象 / 裸 dict 都不抛错,缺失按空值。"""
    rows = group_rows([object(), SimpleNamespace(), {"name": "from-dict.com"}])
    assert len(rows) == 3
    for row in rows[:2]:
        assert row["name"] == ""
        assert row["entries"] == 0
        assert row["sites"] == 0
        assert row["aliases"] == 0
        assert row["agg_max"] == 0.0
        assert row["verdict"] == "未知"
        assert row["attested"] is False
    assert rows[2]["name"] == "from-dict.com"
    assert rows[2]["entries"] == 0


def test_group_rows_empty_and_none_input() -> None:
    """空列表 / None → [](空输入红线:A104 空输入语义在展示层同样成立)。"""
    assert group_rows([]) == []
    assert group_rows(None) == []
    assert group_rows(None, {"x.com"}) == []


def test_group_rows_preserves_input_order() -> None:
    """保持输入顺序(排序责任在上游 group_entries,展示层不重排)。"""
    names = ["c.com", "a.com", "b.com"]
    rows = group_rows([_group(n) for n in names])
    assert [r["name"] for r in rows] == names


def test_group_rows_integration_with_group_entries() -> None:
    """与 A104 真实归组集成:同 canonical 域(www/子域变体)并入同一组。"""
    entries = [
        SimpleNamespace(id=1, site_url="https://www.a.com/x", verdict="nsfw", evidence_zip=""),
        SimpleNamespace(id=2, site_url="https://img.a.com/y", verdict="clean", evidence_zip=""),
        SimpleNamespace(id=3, site_url="https://b.com/", verdict="suspect", evidence_zip=""),
    ]
    rows = group_rows(group_entries(entries, {}))
    assert len(rows) == 2
    by_name = {r["name"]: r for r in rows}
    assert by_name["a.com"]["entries"] == 2
    assert by_name["a.com"]["sites"] == 2
    assert by_name["a.com"]["aliases"] == 2  # www.a.com + img.a.com
    assert by_name["a.com"]["verdict"] == "高置信色情"  # 组内最严重档
    assert by_name["b.com"]["entries"] == 1
    assert by_name["b.com"]["verdict"] == "疑似"


# ---------------------------------------------------------------------------
# attest_badge:声明徽章
# ---------------------------------------------------------------------------


def test_attest_badge_values() -> None:
    """真 → ✓ 已声明;假(含 0 / 空)→ ✗ 未声明。"""
    assert attest_badge(True) == "✓ 已声明"
    assert attest_badge(1) == "✓ 已声明"
    assert attest_badge(False) == "✗ 未声明"
    assert attest_badge(0) == "✗ 未声明"
    assert attest_badge(None) == "✗ 未声明"


def test_attest_badge_constants() -> None:
    """徽章与展示常量口径固定(中文界面,避免散落魔法字符串)。"""
    assert BADGE_ATTESTED == "✓ 已声明"
    assert BADGE_UNATTESTED == "✗ 未声明"
    assert VERDICT_CN == {"clean": "无风险", "suspect": "疑似", "nsfw": "高置信色情"}


# ---------------------------------------------------------------------------
# batch_preview:中文预览文案(含频控数字,红线 26)
# ---------------------------------------------------------------------------


def test_batch_preview_default_config_numbers() -> None:
    """默认 Config:条数 / 间隔 90s / 上限 20 / 人工验证码提示全部在文案里。"""
    preview = batch_preview([{"entry_id": 1}, {"entry_id": 2}, {"entry_id": 3}], Config())
    assert "将依次提交 3 条" in preview
    assert "间隔 ≥90s" in preview
    assert "每条需人工输入验证码" in preview  # 红线 24 固定提示
    assert "单批上限 20" in preview


def test_batch_preview_duck_cfg_overrides_and_defaults() -> None:
    """鸭子 cfg:对象 / dict 覆盖生效;缺失字段回退 90 / 20;None 同样可用。"""
    cfg = SimpleNamespace(batch_item_interval_s=120, batch_max_items=5)
    assert "间隔 ≥120s" in batch_preview([{"x": 1}], cfg)
    assert "单批上限 5" in batch_preview([{"x": 1}], cfg)
    assert "间隔 ≥90s" in batch_preview([{"x": 1}], {"batch_item_interval_s": 90})
    # 缺字段 / None cfg → 默认频控数字
    assert "间隔 ≥90s" in batch_preview([{"x": 1}], SimpleNamespace())
    assert "单批上限 20" in batch_preview([{"x": 1}], None)


def test_batch_preview_invalid_values_fallback() -> None:
    """非法值(字符串 / None)回退默认 90 / 20,不抛错。"""
    cfg = SimpleNamespace(batch_item_interval_s="fast", batch_max_items=None)
    preview = batch_preview([{"x": 1}], cfg)
    assert "间隔 ≥90s" in preview
    assert "单批上限 20" in preview


def test_batch_preview_empty_items() -> None:
    """空清单 / None → 0 条,文案仍完整(频控提示不因空队列消失)。"""
    assert "将依次提交 0 条" in batch_preview([], Config())
    assert "将依次提交 0 条" in batch_preview(None, Config())
    assert "每条需人工输入验证码" in batch_preview([], Config())


# ---------------------------------------------------------------------------
# portal_cn:门户中文名
# ---------------------------------------------------------------------------


def test_portal_cn_known_and_unknown() -> None:
    """shdf → 扫黄打非;12377 原样;未知 / 空 / None 原样回显不抛错。"""
    assert portal_cn("12377") == "12377"
    assert portal_cn("shdf") == "扫黄打非"
    assert portal_cn("weird") == "weird"
    assert portal_cn("") == ""
    assert portal_cn(None) == ""
    assert PORTAL_CN == {"12377": "12377", "shdf": "扫黄打非"}


# ---------------------------------------------------------------------------
# 纯逻辑层可独立导入(本机未装 streamlit 时 import 成功即证)
# ---------------------------------------------------------------------------


def test_pure_layer_importable_without_streamlit() -> None:
    """模块可导入且纯函数齐备:顶部 import 已证,这里再锁 __all__ 契约。"""
    import webui.groups_page as groups_page

    for name in (
        "group_rows",
        "attest_badge",
        "batch_preview",
        "portal_cn",
        "render",
        "main",
    ):
        assert name in groups_page.__all__
        assert callable(getattr(groups_page, name))
