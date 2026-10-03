"""form_scanner 单元测试:a11y 门户理解器(HTML → 门户 YAML v2 草稿)。

覆盖:
- 解析层:控件提取(位置序/label for 配对/祖先 label 隐式关联/select 选项/
  script-style 忽略/hidden 跳过);
- 语义推断:mock 三页字段覆盖、置信度区间与排序、验证码类字段识别并
  排除(任何候选信号含"验证码/校验码/captcha"字样)、多候选竞争、
  id 冲突、无 id 页面 manual_required;
- 草稿生成:人工确认横幅、键序与候选链结构、与 portals/12377.yaml 的
  形态对照、YAML 值不含验证码字样;
- round-trip:草稿落盘后 load_portal_def 全链校验通过(_validate_selectors/
  _validate_candidate/entry_url_key),并可 build_plan_from_def 生成含
  HUMAN_GATE 的 18 步计划;
- 确定性:同输入同输出(scan/generate/CLI 三条路径);
- Playwright 缺席:毒化 sys.modules 后全流程仍可用(默认 stdlib 路径);
- CLI:URL 拒绝、缺文件、无表单、portals/ 目录写保护、非法 entry_url_key、
  stdout/--out 两模式、退出码 0/1。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml as pyyaml

from netsentinel.contracts import Config, Portal, StepAction, SubmissionPayload
from netsentinel.submit import form_models, form_scanner, portal_defs
from netsentinel.submit.form_scanner import generate_draft, main, scan_html
from netsentinel.submit.portal_defs import load_portal_def

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
MOCKS = {
    "12377": _PROJECT_ROOT / "tests" / "mock_portals" / "12377_mock.html",
    "redesigned": _PROJECT_ROOT / "tests" / "mock_portals" / "12377_mock_redesigned.html",
    "shdf": _PROJECT_ROOT / "tests" / "mock_portals" / "shdf_mock.html",
}
_PORTALS_DIR = _PROJECT_ROOT / "portals"

#: 人工确认横幅(硬性语义:草稿绝不自动生效;与任务书逐字一致)。
BANNER = "草稿：人工确认字段映射后方可放入 portals/ 目录生效"

#: 契约 13 键(§3)。
ALL_KEYS = set(form_models.SELECTORS)

#: 无 id 表单:仅 aria-label/label 语义,推断可识别但无法生成 css 主候选。
NO_ID_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>无 id 表单</title></head><body><form id="f">
  <label>举报链接</label><input type="text" name="u" aria-label="举报链接" placeholder="请输入要举报的网址">
  <label for="t">举报类型</label><select name="t" aria-label="举报类型">
    <option value="">-- 请选择 --</option><option value="网络诈骗类">网络诈骗类</option></select>
  <textarea name="d" aria-label="具体描述" placeholder="请描述违法事实"></textarea>
  <input name="n" aria-label="举报人姓名">
  <input name="p" aria-label="联系电话">
  <input name="c" aria-label="图形验证码">
  <button type="button">提交举报</button>
</form></body></html>"""

#: 混合表单:常规字段 + 验证码字段(id=cap1)+ 提交按钮。
MIXED_HTML = """<html><body><form>
  <input id="site-url" type="text" aria-label="举报链接">
  <input id="cap1" aria-label="图形验证码">
  <button id="go" type="submit">提交举报</button>
</form></body></html>"""

#: 验证码信号变体:placeholder/name/按钮文本/校验码措辞各一。
CAPTCHA_VARIANTS_HTML = """<html><body><form>
  <input id="c1" name="captcha" placeholder="请输入验证码">
  <input id="c2" aria-label="校验码">
  <input id="c3" name="user_captcha_input">
  <button id="b1">获取验证码</button>
  <input id="u1" aria-label="举报链接">
</form></body></html>"""

#: placeholder 含验证码字样的"伪验证码"字段(保守排除)。
CAPTCHA_PLACEHOLDER_HTML = """<html><body><form>
  <input id="x1" aria-label="举报链接" placeholder="此处勿填验证码">
</form></body></html>"""

#: id 冲突:两个字段抢同一个 id。
DUP_ID_HTML = """<html><body><form>
  <input id="dup1" aria-label="举报链接">
  <input id="dup1" aria-label="联系电话">
</form></body></html>"""

#: 同字段多控件竞争(phone 两个候选,#p1 位置靠前胜出)。
COMPETITION_HTML = """<html><body><form>
  <input id="p1" aria-label="联系电话">
  <input id="p2" aria-label="手机号码">
</form></body></html>"""

#: 祖先 label 隐式关联 + 显式 for 配对优先 + script 内容忽略 + hidden 跳过。
LABEL_SEMANTICS_HTML = """<html><body><form>
  <label>外层文本<input id="a" name="a" aria-label="举报链接"></label>
  <label for="a">显式label文本</label>
  <input type="hidden" name="csrf" value="t">
  <script>var s = "验证码 验证码"; document.getElementById("a");</script>
</form></body></html>"""


def _mock_html(key: str) -> str:
    return MOCKS[key].read_text(encoding="utf-8")


def _draft(text: str, source: str = "fixture.html") -> str:
    return generate_draft(scan_html(text), source=source)


def _load_draft(tmp_path: Path, draft: str, name: str = "draft.yaml") -> object:
    """草稿落盘 → load_portal_def 全链校验(v2 schema + 红线)。"""
    path = tmp_path / name
    path.write_text(draft, encoding="utf-8")
    return load_portal_def(path)


def _payload() -> SubmissionPayload:
    return SubmissionPayload(
        portal=Portal.P12377,
        site_url="https://example.example.com/a",
        category="色情低俗信息",
        description="x" * 40,
        evidence_zip="data/evidence/x.zip",
    )


# ---------------------------------------------------------------------------
# 解析层
# ---------------------------------------------------------------------------
class TestParseLayer:
    def test_mock_12377_controls_and_order(self):
        controls = form_scanner.parse_form(_mock_html("12377"))
        assert len(controls) == 13  # 10 input + select + textarea + button
        assert [c.pos for c in controls] == list(range(1, 14))
        assert [c.tag for c in controls].count("input") == 10
        by_id = {c.id: c for c in controls}
        # for/id 显式配对:label 文本进入控件(label 的必填星号不干扰子串匹配)
        assert "举报链接" in by_id["report-url"].label_text
        assert by_id["report-type"].tag == "select"
        assert by_id["report-type"].options  # 选项文本已收集
        assert by_id["report-desc"].tag == "textarea"
        assert by_id["report-submit"].text == "提交举报(模拟)"

    def test_label_semantics_explicit_for_wins_and_script_ignored(self):
        controls = form_scanner.parse_form(LABEL_SEMANTICS_HTML)
        assert len(controls) == 1  # hidden 跳过;script/style 内容不算控件
        ctrl = controls[0]
        assert ctrl.id == "a"
        assert ctrl.label_text == "显式label文本"  # for/id 配对优先于祖先 label
        result = scan_html(LABEL_SEMANTICS_HTML)
        assert not result.captcha_controls  # script 里的"验证码"不是表单信号

    def test_implicit_ancestor_label(self):
        html = '<html><body><form><label>举报链接 <input name="u1"></label></form></body></html>'
        controls = form_scanner.parse_form(html)
        assert controls[0].label_text == "举报链接"


# ---------------------------------------------------------------------------
# 语义推断:覆盖 / 置信度 / 验证码红线 / 边界
# ---------------------------------------------------------------------------
class TestClassification:
    @pytest.mark.parametrize("key", sorted(MOCKS))
    def test_mock_pages_full_coverage(self, key):
        result = scan_html(_mock_html(key))
        mapped = {f.key for f in result.fields.values() if f.reason == "mapped"}
        # 除 captcha(红线排除)外的全部契约字段都被识别出候选链
        assert mapped == ALL_KEYS - {"captcha"}
        for f in result.fields.values():
            if f.reason == "mapped":
                assert f.candidates[0].get("css", "").startswith("#")
                assert f.confidence >= 0.5
            assert 0.0 <= f.confidence <= 0.99 or f.key == "captcha"

    def test_chain_structure_matches_v2_vocab(self):
        result = scan_html(_mock_html("12377"))
        chain = result.fields["url"].candidates
        assert chain[0] == {"css": "#report-url"}
        assert chain[1] == {"role": "textbox", "name": "举报链接"}
        assert chain[2] == {"label": "举报链接"}
        assert chain[3] == {"placeholder": "要举报的站点链接"}
        assert result.fields["type"].candidates[1] == {"role": "combobox", "name": "举报类型"}
        assert result.fields["submit"].candidates == [
            {"css": "#report-submit"},
            {"text": "提交举报(模拟)"},
        ]

    def test_confidence_bounds_and_ordering(self):
        result = scan_html(_mock_html("12377"))
        confs = [f.confidence for f in result.ranked_fields()]
        assert confs == sorted(confs, reverse=True)  # 排序确定性
        by_key = {f.key: f.confidence for f in result.fields.values()}
        assert 0.5 <= by_key["file"] <= 0.99
        assert by_key["id"] > by_key["url"] > by_key["type"]  # 依据强度可复现
        # 证据列表非空且带中文依据文本
        assert result.fields["url"].evidence
        assert any("命中关键词" in e for e in result.fields["url"].evidence)

    @pytest.mark.parametrize("html", [MIXED_HTML, CAPTCHA_VARIANTS_HTML, CAPTCHA_PLACEHOLDER_HTML])
    def test_captcha_fields_excluded_with_warnings(self, html):
        result = scan_html(html)
        assert result.captcha_controls, "验证码类字段必须被识别"
        captcha_warnings = [w for w in result.warnings if w.startswith("[验证码红线]")]
        assert captcha_warnings, "验证码排除必须显著警告"
        assert all("绝不生成任何指向验证码的候选" in w for w in captcha_warnings)
        # 排除的控件绝不进入任何字段的候选链
        excluded_css = {f"#{c.id}" for c in result.captcha_controls if c.id}
        for f in result.fields.values():
            for cand in f.candidates:
                assert cand.get("css") not in excluded_css
        # captcha 键:契约缺省 + manual_required(人工确认语义)
        assert result.fields["captcha"].candidates == [{"css": "#report-captcha"}]
        assert result.fields["captcha"].manual_required

    def test_captcha_variants_all_caught(self):
        result = scan_html(CAPTCHA_VARIANTS_HTML)
        ids = {c.id for c in result.captcha_controls}
        assert ids == {"c1", "c2", "c3", "b1"}  # placeholder/aria/name/按钮文本四路信号
        # 验证码按钮不冒充提交按钮:submit 回退契约缺省
        assert result.fields["submit"].reason == "fallback"
        assert result.fields["url"].candidates[0] == {"css": "#u1"}

    def test_no_id_form_manual_required(self):
        result = scan_html(NO_ID_HTML)
        mapped = {f.key: f for f in result.fields.values() if f.control is not None}
        # 语义推断仍完成:aria/label 关键词把字段识别出来
        assert {"url", "type", "desc", "name", "phone", "submit"} <= set(mapped)
        for key in ("url", "type", "desc", "name", "phone", "submit"):
            inf = result.fields[key]
            assert inf.confidence >= 0.5, f"{key} 推断置信度不足"
            # v2 要求 css(#id)打头——无 id 则该字段标 manual_required 排除出链,
            # 回退契约缺省选择器(不违反加载校验)
            assert inf.manual_required and inf.reason == "no-id"
            assert inf.candidates == [{"css": form_models.SELECTORS[key]}]
        assert any("manual_required" in w and "无 id" in w for w in result.warnings)
        # 验证码字段同样被排除(aria-label=图形验证码)
        assert any(c.aria_label == "图形验证码" for c in result.captcha_controls)

    def test_duplicate_id_conflict(self):
        result = scan_html(DUP_ID_HTML)
        assert result.fields["url"].reason == "mapped"
        assert result.fields["url"].candidates[0] == {"css": "#dup1"}
        assert result.fields["phone"].reason == "conflict-css"
        assert result.fields["phone"].candidates == [{"css": "#report-phone"}]
        assert any("id 冲突" in w or "id 与其他字段重复" in w for w in result.warnings)

    def test_field_competition_prefers_higher_confidence(self):
        result = scan_html(COMPETITION_HTML)
        assert result.fields["phone"].control.id == "p1"  # 同置信度取位置靠前者
        assert result.fields["phone"].candidates[0] == {"css": "#p1"}
        assert any("多个候选控件" in w or "次优候选" in w for w in result.warnings)

    def test_unclassified_controls_recorded(self):
        html = '<html><body><form><input id="z1" aria-label="备注信息"></form></body></html>'
        result = scan_html(html)
        assert [c.id for c in result.unclassified] == ["z1"]
        assert result.fields["url"].reason == "fallback"


# ---------------------------------------------------------------------------
# 草稿生成 + round-trip(load_portal_def 全链校验)
# ---------------------------------------------------------------------------
class TestDraftGeneration:
    def test_header_banner_and_placeholders(self):
        draft = _draft(_mock_html("12377"))
        assert BANNER in draft
        assert "尚未生效" in draft
        assert draft.index(BANNER) < draft.index("name:")  # 横幅在头部注释
        assert "entry_url_key: portal_12377_base" in draft
        assert "captcha" in draft
        # 警告与验证码排除进头部注释
        assert "验证码" in draft.split("name:")[0]

    @pytest.mark.parametrize("key", sorted(MOCKS))
    def test_round_trip_load_portal_def(self, tmp_path, key):
        draft = _draft(_mock_html(key), source=str(MOCKS[key]))
        defn = _load_draft(tmp_path, draft)
        assert set(defn.selector_chains) == ALL_KEYS
        for field_key, chain in defn.selector_chains.items():
            assert chain[0].get("css", "").startswith("#"), field_key
            for i, cand in enumerate(chain):  # 逐候选过 portal_defs 校验
                portal_defs._validate_candidate(cand, field_key=field_key, index=i)
        assert defn.selectors["captcha"] == "#report-captcha"
        assert defn.entry_url_key == "portal_12377_base"

    @pytest.mark.parametrize(
        "html, name",
        [(NO_ID_HTML, "no_id"), (MIXED_HTML, "mixed"), (CAPTCHA_VARIANTS_HTML, "captcha"),
         (CAPTCHA_PLACEHOLDER_HTML, "captcha_ph"), (DUP_ID_HTML, "dup"), (COMPETITION_HTML, "comp")],
    )
    def test_round_trip_load_portal_def_edge_fixtures(self, tmp_path, html, name):
        """无 id / 验证码排除 / id 冲突等边界草稿同样必须全链可加载。"""
        defn = _load_draft(tmp_path, _draft(html), name=f"{name}.yaml")
        assert set(defn.selector_chains) == ALL_KEYS
        for field_key, chain in defn.selector_chains.items():
            assert chain[0].get("css", "").startswith("#"), field_key
            for i, cand in enumerate(chain):
                portal_defs._validate_candidate(cand, field_key=field_key, index=i)

    @pytest.mark.parametrize("key", sorted(MOCKS))
    def test_round_trip_build_plan_keeps_human_gate(self, tmp_path, key):
        defn = _load_draft(tmp_path, _draft(_mock_html(key)))
        plan = portal_defs.build_plan_from_def(defn, _payload(), Config())
        actions = [s.action for s in plan.steps]
        assert len(plan.steps) == 18
        assert StepAction.HUMAN_GATE in actions
        assert StepAction.FOCUS in actions  # 验证码聚焦(半自动)仍在
        # 草稿的候选链进入 Step.meta,供执行器自愈回退
        fill = next(s for s in plan.steps if s.label == "填写举报链接")
        assert fill.meta["selector_chain"][0]["css"].startswith("#")

    def test_redesigned_mock_uses_new_ids(self, tmp_path):
        defn = _load_draft(tmp_path, _draft(_mock_html("redesigned")))
        assert defn.selectors["url"] == "#ns-link"
        assert defn.selector_chains["type"][0] == {"css": "#ns-type"}

    def test_shape_matches_builtin_12377_yaml(self, tmp_path):
        """12377 mock 的草稿与 portals/12377.yaml 的 url 链前三候选逐字一致。"""
        defn = _load_draft(tmp_path, _draft(_mock_html("12377")))
        builtin = load_portal_def("12377")
        assert defn.selector_chains["url"][:3] == builtin.selector_chains["url"][:3]
        assert defn.selector_chains["submit"][0] == builtin.selector_chains["submit"][0]

    @pytest.mark.parametrize(
        "html, excluded_css",
        [
            (_mock_html("12377"), {"#report-captcha"}),
            (_mock_html("redesigned"), {"#ns-captcha"}),
            (_mock_html("shdf"), {"#report-captcha"}),
            (MIXED_HTML, {"#cap1"}),
            (NO_ID_HTML, set()),
            (CAPTCHA_VARIANTS_HTML, {"#c1", "#c2", "#c3", "#b1"}),
            (CAPTCHA_PLACEHOLDER_HTML, {"#x1"}),
            (DUP_ID_HTML, set()),
        ],
    )
    def test_yaml_values_never_reference_captcha(self, html, excluded_css):
        """红线:非 captcha 字段的任何候选取值绝不指向验证码(结构级断言)。"""
        data = pyyaml.safe_load(_draft(html))
        captcha_css = {"#report-captcha", form_models.SELECTORS["captcha"]}
        for field_key, chain in data["selectors"].items():
            for cand in chain:
                for kind, value in cand.items():
                    low = str(value).lower()
                    if field_key == "captcha":
                        assert kind == "css" and value in captcha_css
                        continue
                    assert not any(w in low for w in ("验证码", "校验码", "captcha")), \
                        f"{field_key}.{kind}={value!r} 指向验证码"
                    assert value not in captcha_css | excluded_css

    def test_draft_rejects_bad_entry_url_key(self):
        result = scan_html(MIXED_HTML)
        with pytest.raises(ValueError, match="entry_url_key"):
            generate_draft(result, source="x.html", entry_url_key="no_such_field")
        with pytest.raises(ValueError, match="name"):
            generate_draft(result, source="x.html", name=" ")


# ---------------------------------------------------------------------------
# 确定性与 Playwright 缺席路径
# ---------------------------------------------------------------------------
class TestDeterminismAndDeps:
    def test_same_input_same_output_scan_and_generate(self):
        for html in (_mock_html("12377"), NO_ID_HTML, MIXED_HTML):
            assert _draft(html) == _draft(html)

    def test_cli_deterministic_bytes(self, tmp_path):
        out1, out2 = tmp_path / "a.yaml", tmp_path / "b.yaml"
        assert main([str(MOCKS["shdf"]), "--out", str(out1)]) == 0
        assert main([str(MOCKS["shdf"]), "--out", str(out2)]) == 0
        assert out1.read_bytes() == out2.read_bytes()

    def test_playwright_absent_stdlib_default(self, monkeypatch, tmp_path):
        """毒化 sys_modules['playwright'] 后全流程仍可用:默认 stdlib 路径。"""
        monkeypatch.setitem(sys.modules, "playwright", None)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
        result = scan_html(_mock_html("12377"))
        draft = generate_draft(result, source="mock.html")
        assert "selectors:" in draft
        out = tmp_path / "d.yaml"
        assert main([str(MOCKS["12377"]), "--out", str(out)]) == 0
        assert out.is_file()

    def test_draft_not_auto_registered(self, tmp_path):
        """草稿生成后不进入 PORTAL_FILES(绝不自动加载/生效)。"""
        before = dict(portal_defs.PORTAL_FILES)
        main([str(MOCKS["12377"]), "--out", str(tmp_path / "d.yaml")])
        assert dict(portal_defs.PORTAL_FILES) == before


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
class TestCLI:
    def test_rejects_url(self, capsys):
        for url in ("https://example.com/f.html", "http://example.cn/", "file:///c/x.html"):
            assert main([url]) == 1
        assert "禁止 URL" in capsys.readouterr().err

    def test_rejects_missing_file(self, capsys):
        assert main([str(_PROJECT_ROOT / "tests" / "no_such.html")]) == 1
        assert "不存在" in capsys.readouterr().err

    def test_rejects_formless_page(self, tmp_path, capsys):
        page = tmp_path / "empty.html"
        page.write_text("<html><body><p>没有表单</p></body></html>", encoding="utf-8")
        assert main([str(page)]) == 1
        assert "未在" in capsys.readouterr().err

    def test_stdout_mode(self, capsys):
        assert main([str(MOCKS["12377"])]) == 0
        out = capsys.readouterr().out
        assert BANNER in out
        assert "selectors:" in out
        assert 'css: "#report-url"' in out

    def test_out_file_loadable(self, tmp_path):
        out = tmp_path / "draft.yaml"
        assert main([str(MOCKS["12377"]), "--out", str(out),
                     "--name", "测试门户", "--category", "网络诈骗类"]) == 0
        defn = load_portal_def(out)
        assert defn.name == "测试门户"
        assert defn.category_value == "网络诈骗类"

    def test_out_refused_inside_portals_dir(self, capsys):
        target = _PORTALS_DIR / "_draft_should_not_exist.yaml"
        assert main([str(MOCKS["12377"]), "--out", str(target)]) == 1
        assert not target.exists()  # 拒绝发生在写入之前
        assert "绝不自动生效" in capsys.readouterr().err

    def test_bad_entry_url_key_cli(self, capsys):
        assert main([str(MOCKS["12377"]), "--entry-url-key", "oops",
                     "--out", "x.yaml"]) == 1
        assert "entry_url_key" in capsys.readouterr().err

    def test_argparse_error_returns_one(self):
        assert main([]) == 1  # 缺位置参数
        assert main(["--unknown"]) == 1
