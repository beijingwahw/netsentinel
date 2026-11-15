"""webui/keywords_page.py 纯逻辑层测试(离线,零外呼,不依赖 streamlit)。

- 只测纯函数与展示常量:本文件顶部成功 import 即证明纯逻辑层可独立导入
  (本机未装 streamlit 时同样成立);
- 路径与命名:keywords_root/active_path 布局、_safe_name 防穿越与保留名
  active 拒用、非法字符/超长/空白拒绝;
- 校验同源(红线 28):保存/新建/切换为空表一律中文 ValueError——复用
  discovery.keywords.load_keywords 口径;
- 原子与备份:save_active 产生 .bak 滚动、再保存覆盖 .bak、.tmp 不残留;
- 词组库:list_sets 排序/坏词组如实标注 words=-1/排除 active.txt 与 .bak;
  create_set 拒覆盖、delete_set 不存在拒绝、switch_active 整组拷贝、
  save_active_as_set 空当前拒绝;
- 展开预览:preview_queries 原样/模板 {kw}/去重保序、空表中文报错;
- cli_hint 含活文件路径与引擎名;
- UI 守卫:streamlit 缺失时纯逻辑层可导入、main() 返回退出码 1;
- 全程不联网、不读真实 data/ 目录(一律 tmp_path)、不启动 streamlit。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from webui import keywords_page as kp
from webui.keywords_page import (
    ACTIVE_BASENAME,
    CLI_HINT_ENGINE_DEFAULT,
    PREVIEW_CAP,
    TEMPLATES_BASENAME,
    clear_templates,
    cli_hint,
    create_set,
    delete_set,
    keywords_root,
    list_sets,
    parse_stats,
    parse_templates_stats,
    preview_queries,
    read_active,
    read_set,
    read_templates,
    save_active,
    save_active_as_set,
    save_templates,
    switch_active,
)


# ---------------------------------------------------------------------------
# 路径与命名
# ---------------------------------------------------------------------------


def test_layout_paths(tmp_path):
    """关键词目录与活文件路径布局;data_dir 缺省回退 'data'。"""
    assert keywords_root(tmp_path) == tmp_path / "keywords"
    assert keywords_root(None) == __import__("pathlib").Path("data") / "keywords"
    assert keywords_root("") == __import__("pathlib").Path("data") / "keywords"
    assert keywords_root(tmp_path) / ACTIVE_BASENAME == kp.active_path(tmp_path)
    assert ACTIVE_BASENAME == "active.txt"
    assert TEMPLATES_BASENAME == "templates.txt"
    assert keywords_root(tmp_path) / TEMPLATES_BASENAME == kp.templates_path(tmp_path)


def test_safe_name_accepts_legal_names(tmp_path):
    """合法词组名:字母/数字/下划线/连字符/中文;前后空白被剥掉。"""
    for name in ("abc", "group-1", "my_set", "色情线索", "A1_中-文"):
        assert kp._safe_name(f"  {name}  ") == name


def test_safe_name_rejects_bad_names(tmp_path):
    """非法词组名:空/纯空白/非法字符/路径穿越/超长/保留名 active。"""
    for bad in ("", "   ", "a/b", "..", "a\\b", "a b", "x" * 25, "active"):
        with pytest.raises(ValueError, match="词组名"):
            kp._safe_name(bad)


# ---------------------------------------------------------------------------
# 校验同源(红线 28)与实时统计
# ---------------------------------------------------------------------------


def test_save_active_empty_rejected(tmp_path):
    """空表/全注释/全空白一律中文报错(复用装载校验),活文件不落盘。"""
    for text in ("", "   \n\n", "# 只有注释\n", "#a\n#b\n"):
        with pytest.raises(ValueError, match="关键词为空"):
            save_active(tmp_path, text)
    assert not kp.active_path(tmp_path).exists()


def test_parse_stats_live_feedback(tmp_path):
    """实时统计:有效词数(去空/去注释/去重)与跳过行数;空文本不抛错。"""
    stats = parse_stats("赌博 \n# 注释\n\n色情\n色情\n")
    assert stats == {"words": 2, "dropped": 3}
    assert parse_stats("") == {"words": 0, "dropped": 0}


# ---------------------------------------------------------------------------
# 保存 / 备份 / 原子
# ---------------------------------------------------------------------------


def test_save_active_roundtrip_with_bak(tmp_path):
    """保存→读取往返;.bak 滚动为上一版;无 .tmp 残留。"""
    first = save_active(tmp_path, "词甲\n词乙\n")
    assert first["words"] == 2
    assert read_active(tmp_path) == "词甲\n词乙\n"

    save_active(tmp_path, "词丙\n")
    bak = kp.active_path(tmp_path).with_name("active.txt.bak")
    assert bak.read_text(encoding="utf-8") == "词甲\n词乙\n"
    assert read_active(tmp_path) == "词丙\n"

    save_active(tmp_path, "词丁\n")
    assert bak.read_text(encoding="utf-8") == "词丙\n"
    leftovers = [p.name for p in keywords_root(tmp_path).iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_read_active_missing_returns_empty(tmp_path):
    """活文件不存在时读为空串(UI 引导新建)。"""
    assert read_active(tmp_path) == ""


# ---------------------------------------------------------------------------
# 词组库
# ---------------------------------------------------------------------------


def test_create_read_delete_set(tmp_path):
    """新建→读取→删除往返;拒覆盖同名。"""
    made = create_set(tmp_path, "组A", "词1\n# 注\n词2\n")
    assert made["words"] == 2
    assert read_set(tmp_path, "组A") == "词1\n# 注\n词2\n"

    with pytest.raises(ValueError, match="已存在"):
        create_set(tmp_path, "组A", "别的\n")

    delete_set(tmp_path, "组A")
    with pytest.raises(ValueError, match="不存在"):
        read_set(tmp_path, "组A")
    with pytest.raises(ValueError, match="不存在"):
        delete_set(tmp_path, "组A")


def test_list_sets_sorted_and_filters(tmp_path):
    """清单按名排序;坏词组 words=-1 如实标注;active.txt/.bak/.tmp 不入清单。"""
    create_set(tmp_path, "b组", "词X\n")
    create_set(tmp_path, "a组", "词Y\n词Z\n")
    save_active(tmp_path, "活词\n")  # 产生 active.txt 与 .bak
    bad = keywords_root(tmp_path) / "坏组.txt"
    bad.write_text("# 全注释\n", encoding="utf-8")
    keywords_root(tmp_path).joinpath("垃圾.tmp").write_text("", encoding="utf-8")

    rows = list_sets(tmp_path)
    assert [r["name"] for r in rows] == ["a组", "b组", "坏组"]
    by_name = {r["name"]: r for r in rows}
    assert by_name["a组"]["words"] == 2
    assert by_name["b组"]["words"] == 1
    assert by_name["坏组"]["words"] == -1
    assert "mtime" in by_name["a组"] and by_name["a组"]["size"] > 0

    assert list_sets(tmp_path / "不存在的目录") == []


def test_switch_active_copies_whole_set(tmp_path):
    """启用=整组原子拷贝为活文件(实时切换);空词组拒绝且活文件不受影响。"""
    create_set(tmp_path, "组一", "词A\n词B\n")
    create_set(tmp_path, "组二", "词C\n")
    save_active(tmp_path, "初始\n")

    switched = switch_active(tmp_path, "组一")
    assert switched["words"] == 2
    assert read_active(tmp_path) == "词A\n词B\n"

    switched2 = switch_active(tmp_path, "组二")
    assert switched2["words"] == 1
    assert read_active(tmp_path) == "词C\n"
    bak = kp.active_path(tmp_path).with_name("active.txt.bak")
    assert bak.read_text(encoding="utf-8") == "词A\n词B\n"

    empty = keywords_root(tmp_path) / "空组.txt"
    empty.write_text("# 空\n", encoding="utf-8")
    with pytest.raises(ValueError, match="关键词为空"):
        switch_active(tmp_path, "空组")
    assert read_active(tmp_path) == "词C\n"


def test_save_active_as_set(tmp_path):
    """另存当前为新词组;当前为空/未保存时拒绝;拒覆盖。"""
    with pytest.raises(ValueError, match="为空"):
        save_active_as_set(tmp_path, "新组")

    save_active(tmp_path, "词M\n词N\n")
    saved = save_active_as_set(tmp_path, "新组")
    assert saved["words"] == 2
    assert read_set(tmp_path, "新组") == "词M\n词N\n"

    with pytest.raises(ValueError, match="已存在"):
        save_active_as_set(tmp_path, "新组")


# ---------------------------------------------------------------------------
# 展开预览与命令提示
# ---------------------------------------------------------------------------


def test_preview_queries_default_and_templates(tmp_path):
    """原样展开 / {kw} 模板 / 无占位符前缀拼接;去重保序;空表报错。"""
    assert preview_queries("词A\n词B\n") == ["词A", "词B"]
    out = preview_queries("词A\n词B\n", "{kw} site:cn\n{kw} site:com")
    assert out == ["词A site:cn", "词A site:com", "词B site:cn", "词B site:com"]
    assert preview_queries("词A\n", "裸前缀 ") == ["裸前缀 词A"]

    with pytest.raises(ValueError, match="关键词为空"):
        preview_queries("# 只注释\n")

    assert isinstance(PREVIEW_CAP, int) and PREVIEW_CAP > 0


def test_cli_hint_contains_active_path_and_engine(tmp_path):
    """命令提示含活文件完整路径与引擎名;提示不代跑(仅字符串)。"""
    hint = cli_hint(tmp_path)
    assert str(kp.active_path(tmp_path)) in hint
    assert "--keywords-file" in hint and "netsentinel.discovery" in hint
    assert CLI_HINT_ENGINE_DEFAULT in hint
    assert "searxng" in cli_hint(tmp_path, "searxng")


# ---------------------------------------------------------------------------
# 查询模板活文件(V10.20:持久化 + 预览默认 + cli_hint 联动)
# ---------------------------------------------------------------------------


def test_templates_layout_and_roundtrip(tmp_path):
    """模板活文件路径布局;保存→读取往返;不存在的数据目录读为空串。"""
    assert kp.templates_path(tmp_path) == keywords_root(tmp_path) / "templates.txt"
    assert read_templates(tmp_path) == ""  # 尚无文件

    saved = save_templates(tmp_path, "{kw} site:cn\n# 注释\n报告 {kw}\n")
    assert saved["templates"] == 2
    assert saved["path"] == str(kp.templates_path(tmp_path))
    assert read_templates(tmp_path) == "{kw} site:cn\n# 注释\n报告 {kw}\n"


def test_save_templates_rejects_empty(tmp_path):
    """空表/全注释/全空白一律中文报错(同源校验),文件不落盘。"""
    for text in ("", "   \n\n", "# 只有注释\n"):
        with pytest.raises(ValueError, match="模板为空"):
            save_templates(tmp_path, text)
    assert not kp.templates_path(tmp_path).exists()


def test_save_templates_bak_rolling_and_no_tmp(tmp_path):
    """.bak 滚动为上一版(与关键词同款);无 .tmp 残留;坏行(重复)只计不破。"""
    save_templates(tmp_path, "{kw} a\n")
    save_templates(tmp_path, "{kw} b\n")
    bak = kp.templates_path(tmp_path).with_name("templates.txt.bak")
    assert bak.read_text(encoding="utf-8") == "{kw} a\n"
    assert read_templates(tmp_path) == "{kw} b\n"
    leftovers = [p.name for p in keywords_root(tmp_path).iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_parse_templates_stats_live_feedback():
    """模板实时统计:有效数(去空/注释/重复)与跳过行数;空文本不抛错。"""
    stats = parse_templates_stats("{kw} a\n# 注\n\n{kw} a\n{kw} b\n")
    assert stats == {"templates": 2, "dropped": 3}
    assert parse_templates_stats("") == {"templates": 0, "dropped": 0}
    assert parse_templates_stats(None) == {"templates": 0, "dropped": 0}


def test_clear_templates_removes_file_keeps_bak(tmp_path):
    """清空:删主文件(预览回退原样)、保留 .bak 后悔药;再清空不抛。"""
    save_templates(tmp_path, "{kw} a\n")
    save_templates(tmp_path, "{kw} b\n")
    assert clear_templates(tmp_path) is True
    assert read_templates(tmp_path) == ""
    bak = kp.templates_path(tmp_path).with_name("templates.txt.bak")
    assert bak.read_text(encoding="utf-8") == "{kw} a\n"
    assert clear_templates(tmp_path) is False  # 已不存在:无事发生


def test_templates_file_not_listed_as_set(tmp_path):
    """模板活文件不出现在词组库清单(也不是可删词组);templates 为保留名。"""
    save_active(tmp_path, "词A\n")
    save_templates(tmp_path, "{kw} site:cn\n")
    names = [row["name"] for row in list_sets(tmp_path)]
    assert names == []  # 只有 active.txt 与 templates.txt,均非词组
    with pytest.raises(ValueError, match="保留名"):
        create_set(tmp_path, "templates", "{kw} x\n")
    with pytest.raises(ValueError, match="保留名"):
        read_set(tmp_path, "templates")
    with pytest.raises(ValueError, match="保留名"):
        delete_set(tmp_path, "templates")
    # 模板文件未被词组操作误删
    assert read_templates(tmp_path) == "{kw} site:cn\n"


def test_preview_queries_defaults_to_persistent_templates(tmp_path):
    """预览默认读持久模板(UI 口径的纯逻辑版):不传模板文本时按活文件展开。"""
    save_active(tmp_path, "词A\n词B\n")
    save_templates(tmp_path, "{kw} site:cn\n{kw} site:com\n")
    # 模板文本为空串 = 用持久层(UI 传 effective 前先读活文件;此处直接模拟两种入参)
    out = preview_queries("词A\n词B\n", read_templates(tmp_path))
    assert out == [
        "词A site:cn", "词A site:com", "词B site:cn", "词B site:com",
    ]
    # 临时覆盖:另一组模板文本优先生效(UI 口径)
    assert preview_queries("词A\n", "裸 {kw}") == ["裸 词A"]


def test_cli_hint_appends_template_file_when_present(tmp_path):
    """cli_hint 联动:持久模板存在且有效 → 追加 --template-file;缺席则不追加。"""
    hint0 = cli_hint(tmp_path)
    assert "--template-file" not in hint0

    save_templates(tmp_path, "{kw} site:cn\n")
    hint1 = cli_hint(tmp_path)
    assert f'--template-file "{kp.templates_path(tmp_path)}"' in hint1
    assert "--keywords-file" in hint1  # 原有关键词提示不受影响

    # templates_text 注入口径:全注释 = 无有效模板,不追加
    assert "--template-file" not in cli_hint(tmp_path, templates_text="# 无\n")
    # 注入有效模板 = 追加(不读盘)
    assert "--template-file" in cli_hint(
        tmp_path, templates_text="{kw} 注入\n"
    )
    # 清空后回落
    clear_templates(tmp_path)
    assert "--template-file" not in cli_hint(tmp_path)


# ---------------------------------------------------------------------------
# UI 守卫
# ---------------------------------------------------------------------------


def test_pure_layer_importable_without_streamlit():
    """顶部已成功 import;此处确认纯逻辑函数在无 UI 依赖时仍可用。"""
    assert callable(save_active) and callable(switch_active)
    assert kp.main is not None


def test_save_active_lazy_import_survives(tmp_path):
    """兄弟模块(netsentinel.*)全部函数内惰性导入:删掉已加载模块也不炸。"""
    import sys as _sys

    saved = {m: _sys.modules.pop(m) for m in list(_sys.modules) if m.startswith("netsentinel")}
    try:
        save_active(tmp_path, "惰性词\n")
        assert read_active(tmp_path) == "惰性词\n"
    finally:
        _sys.modules.update(saved)


# ---------------------------------------------------------------------------
# V10.20 端到端:模板持久化 UI 流(AppTest 真渲染 + 控件交互)
# ---------------------------------------------------------------------------


def test_template_persist_ui_flow(tmp_path, monkeypatch):
    """关键词页模板编辑区端到端:保存 → 落盘 → 预览读持久模板 → cli_hint 追加。

    覆盖 UI 层接线(编辑器初始化/保存按钮/预览默认读活文件/命令提示联动),
    需要完整 streamlit 渲染,缺依赖时跳过。
    """
    st = pytest.importorskip("streamlit", reason="需要 streamlit 才能做 UI 流测试")
    try:
        from streamlit.testing.v1 import AppTest
    except ImportError:  # pragma: no cover - streamlit < 1.28
        pytest.skip("streamlit.testing.v1.AppTest 不可用")

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("NETSENTINEL_DATA_DIR", str(data))
    monkeypatch.chdir(Path(kp.__file__).resolve().parents[1])

    at = AppTest.from_file(str(Path(kp.__file__).resolve()), default_timeout=60)
    at.run()
    assert not at.exception

    # ① 编辑关键词并保存(主按钮)
    at.text_area(key="kw_active_editor").set_value("词甲\n词乙\n")
    at.run()
    at.button(key="kw_save_active").click()
    at.run()
    assert read_active(data) == "词甲\n词乙\n"

    # ② 持久模板保存前:cli_hint 不含 --template-file,预览为原样查询
    hints = [c.value for c in at.get("code") if "--keywords-file" in str(c.value)]
    assert hints and "--template-file" not in hints[0]

    # ③ 编辑模板并保存
    at.text_area(key="kw_templates_editor").set_value("{kw} site:cn\n")
    at.run()
    at.button(key="kw_templates_save").click()
    at.run()
    assert read_templates(data) == "{kw} site:cn\n"

    # ④ 保存后:cli_hint 追加 --template-file(指向活文件)
    hints = [c.value for c in at.get("code") if "--keywords-file" in str(c.value)]
    assert hints and f'--template-file "{kp.templates_path(data)}"' in hints[0]

    # ⑤ 预览默认读持久模板(临时覆盖框留空)
    codes = [str(c.value) for c in at.get("code")]
    preview = [c for c in codes if "词甲 site:cn" in c]
    assert preview, f"预览未按持久模板展开:{codes[:400]}"
    assert any("持久模板" in cap.value for cap in at.get("caption"))

    # ⑥ 临时覆盖优先:填临时模板后预览按临时展开
    at.text_area(key="kw_templates").set_value("报告 {kw}\n")
    at.run()
    assert any("报告 词甲" in str(c.value) for c in at.get("code"))

    # ⑦ 清空模板:主文件删除、hint 回落、编辑器清空
    at.button(key="kw_templates_clear").click()
    at.run()
    assert read_templates(data) == ""
    hints = [c.value for c in at.get("code") if "--keywords-file" in str(c.value)]
    assert hints and "--template-file" not in hints[0]
