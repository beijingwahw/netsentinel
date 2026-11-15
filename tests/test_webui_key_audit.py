"""V10.20 Seat C:webui 控件 key 唯一性审计断言(纯逻辑层,离线)。

背景:V10.19 实跑抓到"页签控件 key 碰撞"渲染期真 bug(st.tabs 每轮执行
**全部**页签体,同一 pending 条目在"待复核"与"全部"页签同时渲染,不带
页签前缀的控件 key 直接 DuplicateElementKey)。V10.20 把各页面散落在
UI 层的 key f-string 构造**形式化为纯逻辑层纯函数**,本文件以组合枚举
锁定其唯一性;渲染期的端到端保护由 tests/test_webui_render_smoke.py 的
AppTest 冒烟承担(渲染零异常即无 DuplicateElementKey)。

审计结论(8 页 × key 构造模式):
- app.py             card_control_key   页签前缀 × 控件种类 × 条目号;
- keywords_page.py   set_action_key     操作种类 × 词组名(文件名唯一);
- models_page.py     switch_row_key     行索引(单次扫描内唯一);
- draft_flow_page.py field_confirm_key  字段键(DraftConfirmFlow 拒重复);
- dashboard.py       four_eyes_key      控件种类 × 选中条目号;
- groups_page.py     attest_key         控件种类 × 选中组标签;
- providers_page.py / runs_page.py 无显式 key 控件(全部走 label 自动
  分配,单容器单处渲染),由渲染冒烟覆盖,无纯函数可断言。

全程不联网、不读真实 data/。
"""
from __future__ import annotations

import itertools

from webui.app import _CARD_KEY_KINDS, _TAB_STATUSES, card_control_key, tab_key_prefix
from webui.dashboard import four_eyes_key
from webui.draft_flow_page import (
    _FIELD_CONFIRM_PREFIX,
    DraftConfirmFlow,
    field_confirm_key,
)
from webui.groups_page import attest_key
from webui.keywords_page import _SET_KEY_KINDS, set_action_key
from webui.models_page import switch_row_key


# ---------------------------------------------------------------------------
# app.py:页签前缀法(V10.19 修复的形式化)
# ---------------------------------------------------------------------------

def test_tab_key_prefixes_distinct():
    """三个页签身份的前缀互不相同(None → all_ 兜底)。"""
    prefixes = [tab_key_prefix(s) for s in _TAB_STATUSES]
    assert prefixes == ["pending_", "approved_", "all_"]
    assert len(set(prefixes)) == 3


def test_card_control_key_unique_across_tabs_and_entries():
    """页签 × 控件种类 × 条目号 全组合无重复——同一条目跨页签不碰撞。

    这是 V10.19 实测 bug(pending 条目同时在"待复核"与"全部"页签渲染)
    的防回归锁:任何一维丢掉(比如用侧栏筛选值当页签前缀)都会立刻
    在本断言中现形。
    """
    entry_ids = [1, 2, 7, 42, 999]
    keys = [
        card_control_key(tab, kind, eid)
        for tab in _TAB_STATUSES
        for kind in _CARD_KEY_KINDS
        for eid in entry_ids
    ]
    assert len(keys) == 3 * 6 * 5
    assert len(set(keys)) == len(keys)
    # 形态:前缀 + 种类 + 条目号(与 V10.19 手工前缀口径逐字兼容)
    assert card_control_key("pending", "chk", 7) == "pending_chk_7"
    assert card_control_key(None, "plan", 7) == "all_plan_7"


def test_card_control_key_tolerates_non_int_ids():
    """条目号容错规整(鸭子输入):str/float 折成 int,不抛错。"""
    assert card_control_key("pending", "ok", "12") == "pending_ok_12"
    assert card_control_key(None, "no", 3.0) == "all_no_3"


# ---------------------------------------------------------------------------
# keywords_page.py:词组操作按钮
# ---------------------------------------------------------------------------

def test_set_action_key_unique_per_set():
    """操作种类 × 词组名组合唯一;中文/带连字符词组名原样进 key。"""
    names = ["abc", "group-1", "色情线索", "A1_中-文", "my_set"]
    keys = [set_action_key(kind, name) for kind in _SET_KEY_KINDS for name in names]
    assert len(set(keys)) == len(keys) == 3 * 5
    assert set_action_key("switch", "色情线索") == "kw_switch_色情线索"


# ---------------------------------------------------------------------------
# models_page.py:本地服务切换按钮
# ---------------------------------------------------------------------------

def test_switch_row_key_unique_per_row():
    """行索引序列唯一(单次扫描结果内行号稳定)。"""
    keys = [switch_row_key(i) for i in range(12)]
    assert len(set(keys)) == 12
    assert switch_row_key(0) == "models_page_switch_0"


# ---------------------------------------------------------------------------
# draft_flow_page.py:字段确认勾选框
# ---------------------------------------------------------------------------

def test_field_confirm_key_unique_and_flow_rejects_duplicates():
    """字段键唯一,且 DraftConfirmFlow 构造层就拒绝重复字段键(双保险)。"""
    fields = ["a", "b", "portal_field__url"]
    keys = [field_confirm_key(f) for f in fields]
    assert len(set(keys)) == 3
    # 构造层防线:重复字段键直接中文 ValueError,根本到不了渲染
    try:
        DraftConfirmFlow("门户", ["a", "a"])
    except ValueError as exc:
        assert "重复" in str(exc)
    else:  # pragma: no cover - 防线失效时让测试失败
        raise AssertionError("重复字段键应被 DraftConfirmFlow 拒绝")
    # 重扫清理前缀与 key 构造同源(陈旧勾选清理不漏)
    assert field_confirm_key("x").startswith(_FIELD_CONFIRM_PREFIX)


# ---------------------------------------------------------------------------
# dashboard.py / groups_page.py:选中条目维度的控件
# ---------------------------------------------------------------------------

def test_four_eyes_key_unique_per_entry():
    kinds = ("reviewer", "confirm", "approve")
    keys = [four_eyes_key(kind, eid) for kind in kinds for eid in (1, 2, 33)]
    assert len(set(keys)) == len(keys) == 9
    assert four_eyes_key("approve", 3) == "fe_approve_3"


def test_attest_key_unique_per_group():
    kinds = ("reviewer", "confirm", "attest")
    labels = ["a.com", "b.org", "(未命名)"]
    keys = [attest_key(kind, label) for kind in kinds for label in labels]
    assert len(set(keys)) == len(keys) == 9


# ---------------------------------------------------------------------------
# 跨页命名空间:不同页面的 key 构造器输出互不碰撞
# ---------------------------------------------------------------------------

def test_key_namespaces_do_not_collide_across_pages():
    """八页面控件 key 命名空间两两无交集(样例数据代表各页形态)。

    页面身份由函数前缀保证(pending_/approved_/all_ · kw_ · models_page_
    · draft_confirm_ · fe_ · gp_),providers/runs 页无显式 key 不参与。
    """
    samples = [
        # app(三页签 × 条目 7 的全部控件)
        {card_control_key(t, k, 7) for t in _TAB_STATUSES for k in _CARD_KEY_KINDS},
        # keywords(词组操作)
        {set_action_key(k, n) for k in _SET_KEY_KINDS for n in ("abc", "词组一")},
        # keywords 页静态 key(编辑器/模板/新建/另存)
        {"kw_active_editor", "kw_templates", "kw_new_name", "kw_new_text",
         "kw_create", "kw_as_name", "kw_save_as"},
        # models
        {switch_row_key(i) for i in range(5)},
        # draft_flow(字段勾选 + 静态)
        {field_confirm_key(f) for f in ("a", "b")}
        | {"draft_flow_slug", "draft_flow_enable_phrase"},
        # dashboard
        {four_eyes_key(k, e) for k in ("reviewer", "confirm", "approve") for e in (1, 2)},
        # groups
        {attest_key(k, l) for k in ("reviewer", "confirm", "attest") for l in ("a.com", "b.org")},
    ]
    for (i, left), (j, right) in itertools.combinations(list(enumerate(samples)), 2):
        overlap = left & right
        assert not overlap, f"页面组 #{i} 与 #{j} 的控件 key 命名空间碰撞:{overlap}"
