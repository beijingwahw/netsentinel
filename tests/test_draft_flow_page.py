"""webui/draft_flow_page.py 测试(A220,离线,零外呼,不依赖 streamlit)。

覆盖(webui 双段结构铁律对应的三个层面):

- 纯逻辑层(draft_parse / DraftConfirmFlow / persist_draft):本文件顶部成功
  import 即证明纯逻辑层可独立导入(本机未装 streamlit 时同样成立);
  * 草稿解析包装:字段映射行(置信度/推断依据列)、警告、URL 拒绝、
    无表单拒绝、元数据校验透传、确定性(同输入同输出);
  * 确认状态机转移矩阵:未确认 → 逐项确认 → 全确认+声明 → ready →
    placed → enabled(A233 新转移);非法跳转(未全确认落位 / 未声明
    落位 / 落位后变更 / 重复落位 / 未落位启用 / 重复启用 / 未知字段)
    一律 ValueError;无批量确认捷径(源码断言);
  * 落位:load_portal_def 全链校验失败拒绝且零残留、口令门槛、状态门槛、
    路径穿越拒绝、同名覆盖拒绝、成功写入且状态机封存;A233 落位即登记
    (enabled: false 头 + 指引注释、动态发现侧 disabled 标注与门槛);
  * 启用确认(confirm_enable_portal):复述门户名口令、校验失败拒绝且
    文件零残留改动、成功后 enabled: true 可被 get_portal 加载、
    状态机 placed → enabled;
- UI 守卫(webui_smoke 惯例对齐):streamlit 缺失时可导入、main() 打印
  中文安装提示退出码 1、render() 抛 RuntimeError、已装环境 importorskip;
- 全程只写 tmp_path,不联网、不访问真实门户、不启动 streamlit 服务、
  不触碰项目 portals/ 目录(list_portals 内置项只读加载)。
"""
from __future__ import annotations

import inspect
import pathlib
from pathlib import Path

import pytest

import webui.draft_flow_page as draft_flow_page
from webui.draft_flow_page import (
    PHASE_CONFIRMING,
    PHASE_ENABLED,
    PHASE_PLACED,
    PHASE_READY,
    PERSIST_PHRASE,
    DraftBundle,
    DraftConfirmFlow,
    confirm_enable_portal,
    draft_parse,
    persist_draft,
    placed_notice,
)

from netsentinel import telemetry
from netsentinel.submit.portal_defs import (
    discover_portals,
    get_portal,
    list_portals,
    load_portal_def,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
MOCK_12377 = _PROJECT_ROOT / "tests" / "mock_portals" / "12377_mock.html"

#: 混合表单:url + 验证码 + 提交按钮(验证码被红线排除,url/submit 推断)。
MIXED_HTML = """<html><body><form>
  <input id="site-url" type="text" aria-label="举报链接">
  <input id="cap1" aria-label="图形验证码">
  <button id="go" type="submit">提交举报</button>
</form></body></html>"""


# ---------------------------------------------------------------------------
# 造数小工具
# ---------------------------------------------------------------------------

def _bundle(name: str = "测试门户") -> DraftBundle:
    """确定性草稿包(url/submit 推断 + 验证码红线排除 + 其余回退)。"""
    return draft_parse(
        MIXED_HTML,
        source="fixture.html",
        name=name,
        entry_url_key="portal_12377_base",
        category="色情低俗信息",
    )


def _ready_flow(bundle: DraftBundle) -> DraftConfirmFlow:
    """全字段确认 + 声明复述通过的 ready 态状态机。"""
    flow = DraftConfirmFlow(bundle.portal_name, bundle.field_keys)
    for key in bundle.field_keys:
        flow.confirm_field(key)
    assert flow.set_declaration(bundle.portal_name)
    return flow


def _persist_ok(tmp_path: Path, bundle: DraftBundle, flow: DraftConfirmFlow,
                slug: str = "newportal") -> Path:
    return persist_draft(
        bundle.draft_yaml,
        flow,
        portals_dir=tmp_path / "portals",
        confirm_phrase=PERSIST_PHRASE,
        slug=slug,
    )


# ---------------------------------------------------------------------------
# draft_parse:草稿解析包装
# ---------------------------------------------------------------------------
class TestDraftParse:
    def test_bundle_shape_and_field_rows(self):
        bundle = _bundle()
        assert isinstance(bundle, DraftBundle)
        assert bundle.portal_name == "测试门户"
        assert bundle.source == "fixture.html"
        # 字段键 = 契约 13 键(与 form_models.SELECTORS 同序)
        from netsentinel.submit import form_models

        assert bundle.field_keys == list(form_models.SELECTORS)
        assert len(bundle.field_rows) == len(bundle.field_keys)
        for row in bundle.field_rows:
            assert set(row) == {
                "key", "status_cn", "confidence", "evidence", "chain",
                "control", "manual_required",
            }
            assert isinstance(row["status_cn"], str) and row["status_cn"]
            assert 0.0 <= row["confidence"] <= 0.99
            assert isinstance(row["chain"], str) and row["chain"].startswith("#")
            assert isinstance(row["manual_required"], bool)
        # 推断出的 url 字段:中文状态 + 非空推断依据列
        url_row = next(r for r in bundle.field_rows if r["key"] == "url")
        assert url_row["status_cn"].startswith("已识别")
        assert "命中关键词" in url_row["evidence"]
        assert url_row["manual_required"] is False
        assert "input#site-url" in url_row["control"]
        # 验证码字段:红线状态 + manual_required + 契约缺省链
        cap_row = next(r for r in bundle.field_rows if r["key"] == "captcha")
        assert "验证码" in cap_row["status_cn"]
        assert cap_row["manual_required"] is True
        assert cap_row["chain"] == "#report-captcha"
        # 警告列表:验证码排除置顶
        assert any(w.startswith("[验证码红线]") for w in bundle.warnings)
        assert bundle.mapped_count >= 2  # url + submit
        assert bundle.manual_count >= 1  # captcha 恒 manual_required

    def test_draft_yaml_carries_banner(self):
        bundle = _bundle()
        assert "草稿：人工确认字段映射后方可放入 portals/ 目录生效" in bundle.draft_yaml
        assert "尚未生效" in bundle.draft_yaml

    def test_default_name_falls_back_to_scanner_placeholder(self):
        bundle = draft_parse(MIXED_HTML, source="x.html")
        from netsentinel.submit import form_scanner

        assert bundle.portal_name == form_scanner._DEFAULT_NAME

    def test_rejects_url_source(self):
        for source in ("https://x.example/a.html", "http://y.cn/", "file:///c/x.html", "ftp://z/1"):
            with pytest.raises(ValueError, match="禁止 URL"):
                draft_parse(MIXED_HTML, source=source)

    def test_rejects_empty_html(self):
        with pytest.raises(ValueError, match="HTML 内容为空"):
            draft_parse("   ", source="x.html")
        with pytest.raises(ValueError, match="HTML 内容为空"):
            draft_parse(None, source="x.html")  # type: ignore[arg-type]

    def test_rejects_formless_page(self):
        with pytest.raises(ValueError, match="未在 x.html 中发现任何表单控件"):
            draft_parse("<html><body><p>没有表单</p></body></html>", source="x.html")

    def test_propagates_metadata_validation(self):
        with pytest.raises(ValueError, match="entry_url_key"):
            draft_parse(MIXED_HTML, source="x.html", name="n",
                        entry_url_key="no_such_field")
        with pytest.raises(ValueError, match="name"):
            draft_parse(MIXED_HTML, source="x.html", name=" ")

    def test_deterministic(self):
        assert _bundle() == _bundle()

    def test_mock_12377_round_trip(self):
        """真实 mock 集成:包装层不破坏 A209 的 load_portal_def 全链可加载性。"""
        import tempfile

        html = MOCK_12377.read_text(encoding="utf-8")
        bundle = draft_parse(html, source=str(MOCK_12377), name="中央举报",
                             entry_url_key="portal_12377_base")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m.yaml"
            path.write_text(bundle.draft_yaml, encoding="utf-8")
            defn = load_portal_def(path)
        assert defn.name == "中央举报"
        assert defn.selectors["url"].startswith("#")


# ---------------------------------------------------------------------------
# DraftConfirmFlow:状态机转移矩阵
# ---------------------------------------------------------------------------
class TestConfirmStateMachine:
    def test_initial_state_zero_confirmed(self):
        bundle = _bundle()
        flow = DraftConfirmFlow(bundle.portal_name, bundle.field_keys)
        assert flow.phase == PHASE_CONFIRMING
        assert flow.can_persist() is False
        assert flow.confirmed_keys() == []
        assert flow.missing_keys() == bundle.field_keys
        assert flow.declaration_ok() is False
        assert flow.is_confirmed(bundle.field_keys[0]) is False
        assert flow.summary() == {
            "phase": PHASE_CONFIRMING,
            "portal_name": "测试门户",
            "total": len(bundle.field_keys),
            "confirmed": 0,
            "missing": bundle.field_keys,
            "declaration_ok": False,
            "can_persist": False,
            "placed": False,
        }

    def test_constructor_validation(self):
        with pytest.raises(ValueError, match="门户名"):
            DraftConfirmFlow("", ["url"])
        with pytest.raises(ValueError, match="门户名"):
            DraftConfirmFlow("   ", ["url"])
        with pytest.raises(ValueError, match="字段键"):
            DraftConfirmFlow("n", [])
        with pytest.raises(ValueError, match="字段键"):
            DraftConfirmFlow("n", ["url", "url"])  # 重复键
        with pytest.raises(ValueError, match="字段键"):
            DraftConfirmFlow("n", ["url", " "])  # 空键

    def test_confirm_one_by_one_reaches_ready_only_at_end(self):
        bundle = _bundle()
        flow = DraftConfirmFlow(bundle.portal_name, bundle.field_keys)
        # 声明先行也不影响:字段未全确认仍 confirming
        assert flow.set_declaration(bundle.portal_name) is True
        for i, key in enumerate(bundle.field_keys):
            assert flow.phase == PHASE_CONFIRMING
            flow.confirm_field(key)
            if i < len(bundle.field_keys) - 1:
                assert flow.can_persist() is False
        # 全确认 + 声明已复述 → ready
        assert flow.can_persist() is True
        assert flow.phase == PHASE_READY

    def test_declaration_must_restate_portal_name_exactly(self):
        bundle = _bundle()
        flow = DraftConfirmFlow(bundle.portal_name, bundle.field_keys)
        for key in bundle.field_keys:
            flow.confirm_field(key)
        # 全确认但声明错 → 仍不可落位
        for bad in ("", "测试门户!", "这不是门户名", " 测 试 门 户 "):
            assert flow.set_declaration(bad) is False
        assert flow.can_persist() is False
        assert flow.phase == PHASE_CONFIRMING
        # 原样复述(允许首尾空白)→ 通过
        assert flow.set_declaration("  测试门户  ") is True
        assert flow.can_persist() is True
        # 通过后再改错 → 回到未声明态(撤回只会更严)
        assert flow.set_declaration("错") is False
        assert flow.can_persist() is False

    def test_unconfirm_retracts_to_stricter(self):
        bundle = _bundle()
        flow = _ready_flow(bundle)
        assert flow.can_persist() is True
        flow.unconfirm_field(bundle.field_keys[0])
        assert flow.can_persist() is False
        assert flow.missing_keys() == [bundle.field_keys[0]]

    def test_unknown_field_rejected(self):
        bundle = _bundle()
        flow = DraftConfirmFlow(bundle.portal_name, bundle.field_keys)
        with pytest.raises(ValueError, match="未知字段键"):
            flow.confirm_field("no_such_key")
        with pytest.raises(ValueError, match="未知字段键"):
            flow.unconfirm_field("no_such_key")
        # 未知键查询不抛错(UI 友好)
        assert flow.is_confirmed("no_such_key") is False

    def test_mark_placed_rejects_illegal_jumps(self):
        bundle = _bundle()
        # 跳转 1:零确认直接落位
        flow = DraftConfirmFlow(bundle.portal_name, bundle.field_keys)
        with pytest.raises(ValueError, match="落位门槛未满足"):
            flow.mark_placed()
        # 跳转 2:部分确认 + 声明
        flow.confirm_field(bundle.field_keys[0])
        flow.set_declaration(bundle.portal_name)
        with pytest.raises(ValueError, match="落位门槛未满足"):
            flow.mark_placed()
        # 跳转 3:全确认但未声明
        flow2 = DraftConfirmFlow(bundle.portal_name, bundle.field_keys)
        for key in flow2.field_keys:
            flow2.confirm_field(key)
        with pytest.raises(ValueError, match="落位门槛未满足"):
            flow2.mark_placed()

    def test_placed_flow_is_frozen(self):
        bundle = _bundle()
        flow = _ready_flow(bundle)
        flow.mark_placed()
        assert flow.phase == PHASE_PLACED
        assert flow.can_persist() is False  # 落位后不可再次落位
        with pytest.raises(ValueError, match="状态机封存"):
            flow.confirm_field(bundle.field_keys[0])
        with pytest.raises(ValueError, match="状态机封存"):
            flow.unconfirm_field(bundle.field_keys[0])
        with pytest.raises(ValueError, match="状态机封存"):
            flow.set_declaration(bundle.portal_name)
        with pytest.raises(ValueError, match="不可重复落位"):
            flow.mark_placed()
        # 只读查询照常(确认清单保持落位时快照)
        assert flow.confirmed_keys() == list(flow.field_keys)

    def test_confirmed_keys_order_deterministic(self):
        bundle = _bundle()
        flow = DraftConfirmFlow(bundle.portal_name, bundle.field_keys)
        for key in reversed(bundle.field_keys):  # 逆序确认
            flow.confirm_field(key)
        assert flow.confirmed_keys() == bundle.field_keys  # 仍按契约键序

    def test_no_bulk_confirm_shortcut(self):
        """红线:状态机只有逐项确认接口,不存在任何批量/全选捷径。"""
        source = inspect.getsource(DraftConfirmFlow)
        for banned in ("confirm_all", "confirm_fields", "bulk", "select_all"):
            assert banned not in source, f"状态机不得提供批量确认捷径:{banned}"


# ---------------------------------------------------------------------------
# persist_draft:落位门槛 + 全链校验 + 路径约束
# ---------------------------------------------------------------------------
class TestPersistDraft:
    def test_success_writes_portals_yaml_and_seals_flow(self, tmp_path: Path):
        bundle = _bundle()
        flow = _ready_flow(bundle)
        target = _persist_ok(tmp_path, bundle, flow)
        assert target == tmp_path / "portals" / "newportal.yaml"
        assert target.is_file()
        # 写入的文件可被 load_portal_def 全链加载(内容未被改写)
        defn = load_portal_def(target)
        assert defn.name == "测试门户"
        assert defn.entry_url_key == "portal_12377_base"
        # 状态机封存
        assert flow.phase == PHASE_PLACED
        # portals 目录内恰好一个文件(临时校验文件绝不留在目标目录)
        assert [p.name for p in (tmp_path / "portals").iterdir()] == ["newportal.yaml"]

    def test_rejects_wrong_phrase(self, tmp_path: Path):
        bundle = _bundle()
        flow = _ready_flow(bundle)
        for bad in ("", "落位", "确认 落位", "确认落位 "):
            with pytest.raises(ValueError, match="落位口令不符"):
                persist_draft(bundle.draft_yaml, flow,
                              portals_dir=tmp_path / "portals", confirm_phrase=bad)
        assert not (tmp_path / "portals").exists()  # 口令错在一切 IO 之前

    def test_rejects_flow_not_ready(self, tmp_path: Path):
        bundle = _bundle()
        # 零确认
        flow0 = DraftConfirmFlow(bundle.portal_name, bundle.field_keys)
        with pytest.raises(ValueError, match="落位门槛未满足"):
            persist_draft(bundle.draft_yaml, flow0,
                          portals_dir=tmp_path, confirm_phrase=PERSIST_PHRASE)
        # 部分确认
        flow1 = DraftConfirmFlow(bundle.portal_name, bundle.field_keys)
        flow1.confirm_field(bundle.field_keys[0])
        with pytest.raises(ValueError, match="落位门槛未满足"):
            persist_draft(bundle.draft_yaml, flow1,
                          portals_dir=tmp_path, confirm_phrase=PERSIST_PHRASE)
        # 全确认但未声明
        flow2 = DraftConfirmFlow(bundle.portal_name, bundle.field_keys)
        for key in flow2.field_keys:
            flow2.confirm_field(key)
        with pytest.raises(ValueError, match="落位门槛未满足"):
            persist_draft(bundle.draft_yaml, flow2,
                          portals_dir=tmp_path, confirm_phrase=PERSIST_PHRASE)
        # 已落位的 flow 不可再落位
        flow3 = _ready_flow(bundle)
        _persist_ok(tmp_path, bundle, flow3, slug="first")
        with pytest.raises(ValueError, match="落位门槛未满足"):
            persist_draft(bundle.draft_yaml, flow3,
                          portals_dir=tmp_path, confirm_phrase=PERSIST_PHRASE, slug="second")
        assert sorted(p.name for p in tmp_path.joinpath("portals").iterdir()) == ["first.yaml"]

    def test_rejects_empty_draft(self, tmp_path: Path):
        bundle = _bundle()
        flow = _ready_flow(bundle)
        with pytest.raises(ValueError, match="草稿 YAML 文本为空"):
            persist_draft("  ", flow, portals_dir=tmp_path,
                          confirm_phrase=PERSIST_PHRASE, slug="x1")

    def test_validation_failure_refuses_write_and_keeps_flow(self, tmp_path: Path):
        bundle = _bundle()
        flow = _ready_flow(bundle)
        # 构造能通过门槛、但通不过 load_portal_def 的"坏草稿"
        # (未知顶层键 → portal_defs 中文拒绝)
        bad_yaml = "name: 坏草稿\nentry_url_key: portal_12377_base\ncategory: x\nbad_key: 1\n"
        with pytest.raises(ValueError, match="全链校验失败") as excinfo:
            persist_draft(bad_yaml, flow, portals_dir=tmp_path,
                          confirm_phrase=PERSIST_PHRASE, slug="bad1")
        assert "未知键" in str(excinfo.value)  # 原因透传展示
        assert list(tmp_path.iterdir()) == []  # portals 目录零残留
        assert flow.phase == PHASE_READY  # 状态未消费,可修正后重试
        # 修正为合法草稿后同一 flow 仍可落位
        target = _persist_ok(tmp_path, bundle, flow, slug="bad1")
        assert target.is_file()

    def test_validation_failure_captcha_alias_refused(self, tmp_path: Path):
        """验证码别名红线:候选链夹带验证码字样的草稿拒绝写入(全链校验)。"""
        bundle = _bundle()
        flow = _ready_flow(bundle)
        alias_yaml = (
            "name: 别名草稿\nentry_url_key: portal_12377_base\ncategory: x\n"
            "selectors:\n"
            "  url:\n    - css: \"#x-url\"\n    - label: \"图形验证码\"\n"
        )
        with pytest.raises(ValueError, match="全链校验失败"):
            persist_draft(alias_yaml, flow, portals_dir=tmp_path,
                          confirm_phrase=PERSIST_PHRASE, slug="alias")
        assert not (tmp_path / "alias.yaml").exists()

    def test_refuses_overwrite_existing(self, tmp_path: Path):
        bundle = _bundle()
        flow = _ready_flow(bundle)
        target = _persist_ok(tmp_path, bundle, flow, slug="dup")
        original = target.read_text(encoding="utf-8")
        # 重新扫描确认后再次落位同名文件 → 拒绝覆盖
        flow2 = _ready_flow(_bundle())
        with pytest.raises(ValueError, match="拒绝覆盖"):
            persist_draft(bundle.draft_yaml, flow2,
                          portals_dir=tmp_path / "portals",
                          confirm_phrase=PERSIST_PHRASE, slug="dup")
        assert target.read_text(encoding="utf-8") == original  # 既有文件原样

    @pytest.mark.parametrize("slug", ["../evil", "a/b", "a\\b", ".", "..", "中文短名", "-lead", ""])
    def test_rejects_path_traversal_and_bad_slugs(self, tmp_path: Path, slug):
        bundle = _bundle()
        flow = _ready_flow(bundle)
        with pytest.raises(ValueError, match="短名"):
            persist_draft(bundle.draft_yaml, flow, portals_dir=tmp_path,
                          confirm_phrase=PERSIST_PHRASE, slug=slug)
        assert list(tmp_path.rglob("*.yaml")) == []  # 任何位置零残留

    def test_slug_derivation_from_ascii_name(self, tmp_path: Path):
        bundle = _bundle(name="New Portal 42")  # ASCII 名可推导
        flow = _ready_flow(bundle)
        target = _persist_ok(tmp_path, bundle, flow, slug=None)
        assert target.name == "NewPortal42.yaml"

    def test_slug_derivation_empty_for_chinese_name(self, tmp_path: Path):
        bundle = _bundle(name="中文门户")  # 中文名推导为空 → 显式短名必填
        flow = _ready_flow(bundle)
        with pytest.raises(ValueError, match="短名"):
            persist_draft(bundle.draft_yaml, flow, portals_dir=tmp_path,
                          confirm_phrase=PERSIST_PHRASE)

    def test_telemetry_counters(self, tmp_path: Path):
        telemetry.reset()
        bundle = _bundle()
        flow = _ready_flow(bundle)
        _persist_ok(tmp_path, bundle, flow, slug="tele")
        counters = telemetry.snapshot()["counters"]
        assert counters.get("draft_flow_page.scan") == 1.0
        assert counters.get("draft_flow_page.persist") == 1.0


# ---------------------------------------------------------------------------
# UI 守卫(webui_smoke 惯例对齐:惰性导入断言)
# ---------------------------------------------------------------------------
def test_module_importable_without_streamlit() -> None:
    # 顶部已成功 import:即证明纯逻辑层不依赖 streamlit(webui 双段结构铁律)。
    assert isinstance(draft_flow_page._HAS_ST, bool)
    assert callable(draft_flow_page.draft_parse)
    assert callable(draft_flow_page.DraftConfirmFlow)
    assert callable(draft_flow_page.persist_draft)
    assert callable(draft_flow_page.render)
    assert callable(draft_flow_page.main)


def test_pure_layer_never_touches_streamlit_symbols() -> None:
    """纯逻辑层函数源码不得引用 st(UI 与逻辑零耦合,双段结构铁律)。"""
    for fn in (draft_flow_page.draft_parse, draft_flow_page.persist_draft):
        assert "st." not in inspect.getsource(fn)
    assert "st." not in inspect.getsource(DraftConfirmFlow)


def test_main_without_streamlit_prints_hint_and_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    if draft_flow_page._HAS_ST:  # pragma: no cover - 已装 streamlit 的环境跳过
        pytest.skip("本环境已安装 streamlit,缺失分支不可测")
    assert draft_flow_page.main() == 1
    err = capsys.readouterr().err
    assert "Streamlit" in err and "pip install" in err
    assert "draft_flow_page" in err  # 提示里给出本页面的启动命令


def test_render_without_streamlit_raises() -> None:
    if draft_flow_page._HAS_ST:  # pragma: no cover - 已装 streamlit 的环境跳过
        pytest.skip("本环境已安装 streamlit,缺失分支不可测")
    with pytest.raises(RuntimeError, match="streamlit"):
        draft_flow_page.render()


def test_streamlit_ui_guard() -> None:
    pytest.importorskip("streamlit")
    assert draft_flow_page._HAS_ST is True
    assert callable(draft_flow_page.render)
    assert callable(draft_flow_page.main)


def test_ui_checkbox_defaults_to_unchecked() -> None:
    """红线:UI 层字段勾选默认全不勾(绝不自动勾选),源码级锁定。"""
    source = inspect.getsource(draft_flow_page._render_confirm)
    assert "value=False" in source


# ---------------------------------------------------------------------------
# A233:落位即登记(enabled: false 头 + 动态发现侧 disabled 标注与门槛)
# ---------------------------------------------------------------------------
class TestPersistRegistersDisabled:
    """persist_draft 落位闭环:写入文件以 disabled 登记,绝不自动生效。"""

    def test_persisted_file_carries_enabled_false_header(self, tmp_path: Path):
        bundle = _bundle()
        flow = _ready_flow(bundle)
        target = _persist_ok(tmp_path, bundle, flow)
        text = target.read_text(encoding="utf-8")
        lines = text.splitlines()
        # 恰好一行顶层 enabled 键,值为 false;头部注释在键之前
        assert [ln for ln in lines if ln.startswith("enabled:")] == ["enabled: false"]
        assert lines[0].startswith("#")
        assert "人工确认" in text  # 确认指引注释随登记头写入
        defn = load_portal_def(target)
        assert defn.enabled is False
        assert defn.name == "测试门户"  # 头注入不破坏定义内容
        assert defn.entry_url_key == "portal_12377_base"

    def test_persisted_portal_discovered_disabled_and_gated(self, tmp_path: Path):
        """红线闭环:落位后被 discover_portals 发现但标注 disabled;
        list_portals 默认不含、get_portal 拒绝(动态发现绝不自动生效)。"""
        bundle = _bundle()
        flow = _ready_flow(bundle)
        _persist_ok(tmp_path, bundle, flow)
        portals = tmp_path / "portals"
        found = discover_portals(portals)
        assert set(found) == {"newportal"}
        assert found["newportal"].enabled is False
        assert set(list_portals(directory=portals)) == {"12377", "shdf"}
        assert "newportal" in list_portals(include_disabled=True, directory=portals)
        with pytest.raises(ValueError, match="尚未启用"):
            get_portal("newportal", directory=portals)

    def test_persist_rejects_draft_with_own_enabled_key(self, tmp_path: Path):
        """防静默启用:草稿自带顶层 enabled 键(哪怕 true)一律拒绝落位。"""
        bundle = _bundle()
        flow = _ready_flow(bundle)
        draft = "enabled: true\n" + bundle.draft_yaml
        with pytest.raises(ValueError, match="enabled") as excinfo:
            persist_draft(draft, flow, portals_dir=tmp_path,
                          confirm_phrase=PERSIST_PHRASE, slug="selfen")
        assert "拒绝落位" in str(excinfo.value)
        assert list(tmp_path.rglob("*.yaml")) == []  # 零残留
        assert flow.phase == PHASE_READY  # 状态未消费

    def test_placed_notice_wording(self, tmp_path: Path):
        """纯逻辑层提示文案:已登记为 disabled,人工改 true 后自动生效。"""
        notice = placed_notice(tmp_path / "portals" / "x.yaml")
        assert "已登记为 disabled" in notice
        assert "enabled" in notice and "true" in notice
        assert "自动" in notice
        # 旧口径(须代码登记)绝不再出现
        assert "PORTAL_FILES" not in notice


# ---------------------------------------------------------------------------
# confirm_enable_portal:启用确认(口令 / 校验失败零残留 / 成功后可加载)
# ---------------------------------------------------------------------------
class TestConfirmEnablePortal:
    """placed → enabled 的显式人工动作:复述门户名口令 + 全链校验 + 原子改写。"""

    def _placed(self, tmp_path: Path, name: str = "测试门户",
                slug: str = "newportal") -> tuple[Path, DraftConfirmFlow]:
        bundle = _bundle(name=name)
        flow = _ready_flow(bundle)
        target = _persist_ok(tmp_path, bundle, flow, slug=slug)
        return target, flow

    def test_wrong_phrase_rejected_file_unchanged(self, tmp_path: Path):
        target, flow = self._placed(tmp_path)
        before = target.read_bytes()
        for bad in ("", "测试门户!", "测试 门户", "这不是门户名", None):
            with pytest.raises(ValueError, match="启用口令不符"):
                confirm_enable_portal(target, bad, flow=flow)  # type: ignore[arg-type]
        assert target.read_bytes() == before  # 字节级零改动
        assert flow.phase == PHASE_PLACED  # 状态机未消费

    def test_load_failure_rejected_zero_modification(self, tmp_path: Path):
        portals = tmp_path / "portals"
        portals.mkdir()
        bad = portals / "bad.yaml"
        bad.write_text(
            "enabled: false\nname: x\nentry_url_key: portal_12377_base"
            "\ncategory: c\nbogus: 1\n",
            encoding="utf-8",
        )
        before = bad.read_bytes()
        with pytest.raises(ValueError, match="全链校验失败") as excinfo:
            confirm_enable_portal(bad, "x")
        assert "未知键" in str(excinfo.value)  # 原因透传
        assert bad.read_bytes() == before
        assert [p.name for p in portals.iterdir()] == ["bad.yaml"]  # 零残留

    def test_success_flips_enabled_flow_transitions_and_loads(self, tmp_path: Path):
        target, flow = self._placed(tmp_path)
        result = confirm_enable_portal(target, "  测试门户  ", flow=flow)
        assert result == target
        lines = target.read_text(encoding="utf-8").splitlines()
        assert "enabled: true" in lines
        assert "enabled: false" not in lines
        assert "人工确认" in target.read_text(encoding="utf-8")  # 注释逐字保留
        defn = load_portal_def(target)
        assert defn.enabled is True
        # 状态机 placed → enabled(终态)
        assert flow.phase == PHASE_ENABLED
        assert flow.is_enabled() is True
        assert flow.can_persist() is False
        # 启用后即刻被动态发现按短名加载(登记闭环)
        portals = tmp_path / "portals"
        assert get_portal("newportal", directory=portals).name == "测试门户"
        assert set(list_portals(directory=portals)) == {"12377", "shdf", "newportal"}

    def test_already_enabled_rejected(self, tmp_path: Path):
        target, flow = self._placed(tmp_path)
        confirm_enable_portal(target, "测试门户", flow=flow)
        before = target.read_bytes()
        with pytest.raises(ValueError, match="已启用"):
            confirm_enable_portal(target, "测试门户", flow=flow)
        assert target.read_bytes() == before

    def test_flow_not_placed_rejected(self, tmp_path: Path):
        target, _flow = self._placed(tmp_path)
        fresh = _ready_flow(_bundle())  # ready 态:未落位,不可直接启用
        with pytest.raises(ValueError, match="placed"):
            confirm_enable_portal(target, "测试门户", flow=fresh)
        assert [ln for ln in target.read_text(encoding="utf-8").splitlines()
                if ln.startswith("enabled:")] == ["enabled: false"]

    def test_flow_name_mismatch_rejected(self, tmp_path: Path):
        target, _flow = self._placed(tmp_path, name="甲门户")
        other = _ready_flow(_bundle(name="乙门户"))
        other.mark_placed()
        with pytest.raises(ValueError, match="不一致"):
            confirm_enable_portal(target, "甲门户", flow=other)
        assert "enabled: false" in target.read_text(encoding="utf-8")

    def test_nonexistent_file_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError, match="不存在"):
            confirm_enable_portal(tmp_path / "ghost.yaml", "测试门户")

    def test_handwritten_file_without_enabled_key_gets_true(self, tmp_path: Path):
        """手写动态 YAML(无 enabled 键)的启用路径:确认后在文件头补 true。"""
        portals = tmp_path / "portals"
        portals.mkdir()
        hand = portals / "hand.yaml"
        hand.write_text(
            "name: 手写门户\nentry_url_key: portal_shdf_base\ncategory: c\n",
            encoding="utf-8",
        )
        assert confirm_enable_portal(hand, "手写门户") == hand
        assert hand.read_text(encoding="utf-8").startswith("enabled: true\n")
        assert load_portal_def(hand).enabled is True
        assert get_portal("hand", directory=portals).name == "手写门户"

    def test_yaml11_false_variant_and_inline_comment_preserved(self, tmp_path: Path):
        """YAML 1.1 假值变体(no)同样可启用;行尾注释逐字保留(回归)。"""
        portals = tmp_path / "portals"
        portals.mkdir()
        p = portals / "variant.yaml"
        p.write_text(
            "enabled: no  # 人工核验后保持启用\n"
            "name: 变体门户\nentry_url_key: portal_12377_base\ncategory: c\n",
            encoding="utf-8",
        )
        confirm_enable_portal(p, "变体门户")
        first = p.read_text(encoding="utf-8").splitlines()[0]
        assert first == "enabled: true  # 人工核验后保持启用"
        assert load_portal_def(p).enabled is True

    def test_flow_style_mapping_rejected_without_touching_file(self, tmp_path: Path):
        """无法行级改写的形态(流式单行映射)显式拒绝,原文件字节不变
        (改写复核兜底:绝不静默写出"看似启用实则未生效"的文件)。"""
        portals = tmp_path / "portals"
        portals.mkdir()
        p = portals / "flow.yaml"
        raw = (
            '{"enabled": false, "name": "乙", "entry_url_key": "portal_shdf_base",'
            ' "category": "c"}\n'
        )
        p.write_text(raw, encoding="utf-8")
        with pytest.raises(ValueError, match="文件保持原样"):
            confirm_enable_portal(p, "乙")
        assert p.read_text(encoding="utf-8") == raw
        assert [q.name for q in portals.iterdir()] == ["flow.yaml"]  # 零残留

    def test_telemetry_counter(self, tmp_path: Path):
        telemetry.reset()
        target, flow = self._placed(tmp_path)
        confirm_enable_portal(target, "测试门户", flow=flow)
        assert telemetry.snapshot()["counters"].get(
            "draft_flow_page.confirm_enable") == 1.0


# ---------------------------------------------------------------------------
# DraftConfirmFlow:A233 新转移 placed → enabled(非法跳转拒绝、终态封存)
# ---------------------------------------------------------------------------
class TestEnabledStateMachine:
    def test_mark_enabled_rejects_illegal_jumps(self):
        bundle = _bundle()
        # 跳转 1:零确认(confirming)直接启用
        flow0 = DraftConfirmFlow(bundle.portal_name, bundle.field_keys)
        with pytest.raises(ValueError, match="placed → enabled"):
            flow0.mark_enabled()
        # 跳转 2:ready(全确认+声明)但未落位
        flow1 = _ready_flow(bundle)
        with pytest.raises(ValueError, match="placed → enabled"):
            flow1.mark_enabled()

    def test_placed_flow_not_enabled_by_default(self):
        flow = _ready_flow(_bundle())
        flow.mark_placed()
        assert flow.phase == PHASE_PLACED
        assert flow.is_enabled() is False

    def test_enabled_is_terminal_and_frozen(self):
        bundle = _bundle()
        flow = _ready_flow(bundle)
        flow.mark_placed()
        flow.mark_enabled()
        assert flow.phase == PHASE_ENABLED
        assert flow.is_enabled() is True
        assert flow.can_persist() is False
        with pytest.raises(ValueError, match="状态机封存"):
            flow.confirm_field(bundle.field_keys[0])
        with pytest.raises(ValueError, match="状态机封存"):
            flow.unconfirm_field(bundle.field_keys[0])
        with pytest.raises(ValueError, match="状态机封存"):
            flow.set_declaration(bundle.portal_name)
        with pytest.raises(ValueError, match="不可重复落位"):
            flow.mark_placed()
        with pytest.raises(ValueError, match="不可重复启用"):
            flow.mark_enabled()
        # 只读查询照常(确认清单保持落位时快照)
        assert flow.confirmed_keys() == list(flow.field_keys)

    def test_no_bulk_enable_shortcut(self):
        """红线:启用确认同样只有单文件口令接口,无任何批量捷径。"""
        source = inspect.getsource(draft_flow_page.confirm_enable_portal)
        for banned in ("enable_all", "confirm_all", "glob", "iterdir"):
            assert banned not in source, f"启用接口不得提供批量捷径:{banned}"
