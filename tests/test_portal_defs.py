"""A53 单元测试:netsentinel/submit/portal_defs.py + portals/*.yaml(离线)。

覆盖:
- 内置 PORTAL_FILES 常量与两份 YAML 落位、字段正确、YAML 不含真实门户地址;
- load_portal_def:按短名 / 按路径加载、缺省 selectors=契约 §3、
  category(YAML 键)→ category_value(字段)映射、note 缺省;
- 校验拒绝:未知键 / 缺必填键 / 非法 entry_url_key(Config 无该字段、
  下划线开头)/ selectors 缺键、多键、值非 # 开头、与验证码选择器别名
  重复 / 顶层非映射 / 非法 YAML / 未知标识;
- build_plan_from_def:入口 URL 缺省取 cfg 字段(两个门户)、显式传参
  优先、入口缺失拒绝、首步 meta["portal"]、类目覆盖 payload 与 SELECT;
- 篡改防御:captcha 只出现在 HUMAN_GATE;把 captcha 塞进 FILL 后调用
  导出的自检函数 _assert_no_captcha_autofill 抛中文 RuntimeError;
  经 monkeypatch 别名 selectors 后 build 同样拒绝;
- 与 A12 form_models 计划一致性:步数(12)、顺序、SELECT value=类目,
  并与 A13/A14 planner 的产出语义对齐。
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config, Portal, Step, StepAction, SubmissionPayload
from netsentinel.submit import form_models, portal_defs
from netsentinel.submit.portal_defs import (
    PORTAL_FILES,
    PortalDef,
    _assert_no_captcha_autofill,
    build_plan_from_def,
    load_portal_def,
)

#: 契约 §5 固定顺序(与 tests/test_form_models.py 的 EXPECTED_ACTIONS 一致)。
EXPECTED_ACTIONS: list[StepAction] = [
    StepAction.GOTO,
    StepAction.WAIT,
    StepAction.SELECT,     # 信息类型
    StepAction.FILL,       # url
    StepAction.FILL,       # desc
    StepAction.FILL,       # name(skippable)
    StepAction.FILL,       # phone(skippable)
    StepAction.FILL,       # email(skippable, V10.2)
    StepAction.FILL,       # idcard(skippable, V10.2)
    StepAction.FILL,       # address(skippable, V10.2)
    StepAction.FILL,       # postcode(skippable, V10.2)
    StepAction.FILL,       # org(skippable, V10.2)
    StepAction.SCREENSHOT,
    StepAction.FOCUS,      # V10.1:聚焦验证码框(半自动)
    StepAction.HUMAN_GATE, # 人工在浏览器输入验证码 + 回终端回车
    StepAction.CLICK,
    StepAction.WAIT,
    StepAction.SCREENSHOT,
]

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _cjk(text: str) -> bool:
    """错误文案应为中文。"""
    return re.search(r"[\u4e00-\u9fff]", text) is not None


def make_entry(**overrides) -> SimpleNamespace:
    """构造 entry_like 桩对象(与 test_form_models.make_entry 同构)。"""
    fields: dict = {
        "site_url": "http://bad.example.com/",
        "verdict": "nsfw",
        "evidence_zip": "data/evidence/bad.example.com_20260101.zip",
        "agg_nsw_prob": 0.972,
        "nsw_image_count": 3,
        "pages": [f"http://bad.example.com/p{i}" for i in range(3)],
    }
    for key, value in overrides.items():
        if value is None:
            fields.pop(key, None)
        else:
            fields[key] = value
    return SimpleNamespace(**fields)


def make_payload(portal: Portal = Portal.P12377) -> SubmissionPayload:
    """构造已通过 validate 的举报 payload 桩。"""
    return form_models.build_payload(make_entry(), portal, Config())


def write_yaml(tmp_path: Path, text: str) -> Path:
    """把 YAML 文本写入临时文件并返回路径。"""
    path = tmp_path / "portal.yaml"
    path.write_text(text, encoding="utf-8")
    return path


MINIMAL_YAML = (
    "name: 测试门户\n"
    "entry_url_key: portal_12377_base\n"
    "category: 测试类目\n"
)

#: 两份内置 YAML 的 v2 候选链声明结构(锁定:id 打头 + aria/label 兜底;
#: file/captcha 仅 css 单候选,submit 另带 role+text 兜底)。
_V2_CHAIN_LABELS: dict[str, str] = {
    "url": "举报链接",
    "type": "举报类型",
    "desc": "具体描述",
    "name": "举报人姓名",
    "phone": "联系电话",
    "email": "电子邮箱",
    "id": "身份证号",
    "address": "通讯地址",
    "postcode": "邮政编码",
    "org": "单位名称",
}


def _builtin_expected_chains() -> dict[str, list[dict[str, str]]]:
    """构造与内置 YAML 逐字对应的期望候选链(改动 YAML 链需同步本函数)。"""
    chains: dict[str, list[dict[str, str]]] = {}
    for key, css in form_models.SELECTORS.items():
        if key in ("file", "captcha"):
            chains[key] = [{"css": css}]
        elif key == "submit":
            chains[key] = [
                {"css": css},
                {"role": "button", "name": "提交举报"},
                {"text": "提交举报"},
            ]
        else:
            label = _V2_CHAIN_LABELS[key]
            role = "combobox" if key == "type" else "textbox"
            chains[key] = [{"css": css}, {"role": role, "name": label}, {"label": label}]
    return chains


# ---------------------------------------------------------------------------
# 常量与内置定义文件落位
# ---------------------------------------------------------------------------
class TestPortalFiles:
    def test_portal_files_mapping(self):
        assert PORTAL_FILES == {
            "12377": "portals/12377.yaml",
            "shdf": "portals/shdf.yaml",
        }

    def test_builtin_portal_files_exist_at_project_root(self):
        for rel in PORTAL_FILES.values():
            assert (_PROJECT_ROOT / rel).is_file(), f"缺少内置门户定义:{rel}"

    @pytest.mark.parametrize("key", sorted(PORTAL_FILES))
    def test_yaml_has_no_hardcoded_real_portal_url(self, key):
        """红线:门户地址只来自 Config 字段,YAML 里不许出现真实门户域名。"""
        text = (_PROJECT_ROOT / PORTAL_FILES[key]).read_text(encoding="utf-8")
        assert "12377.cn" not in text
        assert "shdf.gov.cn" not in text
        assert "http://" not in text and "https://" not in text


# ---------------------------------------------------------------------------
# 两份内置 YAML 的加载结果
# ---------------------------------------------------------------------------
class TestLoadBuiltinDefs:
    def test_12377_fields(self):
        defn = load_portal_def("12377")
        assert defn.name == "中央网信办违法和不良信息举报中心(12377)"
        assert defn.entry_url_key == "portal_12377_base"
        assert defn.category_value == "色情低俗信息"  # 与 A13 CATEGORY 一致
        assert "上线前须人工核验真实入口与字段" in defn.note

    def test_shdf_fields(self):
        defn = load_portal_def("shdf")
        assert defn.name == "全国'扫黄打非'工作小组办公室(扫黄打非网)"
        assert defn.entry_url_key == "portal_shdf_base"
        assert defn.category_value == "淫秽色情类"  # 与 A14 SHDF_CATEGORY 一致
        assert "上线前须人工核验真实入口与字段" in defn.note

    @pytest.mark.parametrize("key", sorted(PORTAL_FILES))
    def test_default_selectors_equal_contract_and_are_copies(self, key):
        defn = load_portal_def(key)
        # 缺省 = 契约 §3(与 form_models.SELECTORS 逐键一致),且是独立副本
        assert defn.selectors == form_models.SELECTORS
        assert defn.selectors is not form_models.SELECTORS
        assert set(defn.selectors) == set(form_models.SELECTORS)

    def test_load_by_explicit_path_str_and_path(self):
        for target in (
            str(_PROJECT_ROOT / "portals" / "12377.yaml"),
            _PROJECT_ROOT / "portals" / "12377.yaml",
        ):
            defn = load_portal_def(target)
            assert defn.entry_url_key == "portal_12377_base"

    @pytest.mark.parametrize("key", sorted(PORTAL_FILES))
    def test_key_and_path_load_equivalent(self, key):
        by_key = load_portal_def(key)
        by_path = load_portal_def(_PROJECT_ROOT / PORTAL_FILES[key])
        assert by_key == by_path


# ---------------------------------------------------------------------------
# 加载与校验(临时定义文件)
# ---------------------------------------------------------------------------
class TestLoadValidation:
    def test_minimal_definition_loads_with_defaults(self, tmp_path):
        defn = load_portal_def(write_yaml(tmp_path, MINIMAL_YAML))
        assert isinstance(defn, PortalDef)
        assert defn.note == ""
        assert defn.selectors == form_models.SELECTORS

    def test_yaml_category_key_maps_to_category_value_field(self, tmp_path):
        defn = load_portal_def(write_yaml(tmp_path, MINIMAL_YAML))
        assert defn.category_value == "测试类目"

    def test_unknown_key_rejected(self, tmp_path):
        path = write_yaml(tmp_path, MINIMAL_YAML + "entry_url: https://real.example\n")
        with pytest.raises(ValueError, match="未知键"):
            load_portal_def(path)

    def test_unknown_selector_like_key_rejected(self, tmp_path):
        path = write_yaml(tmp_path, MINIMAL_YAML + "steps: []\n")
        with pytest.raises(ValueError) as excinfo:
            load_portal_def(path)
        assert _cjk(str(excinfo.value))

    @pytest.mark.parametrize("missing", ["name", "entry_url_key", "category"])
    def test_missing_required_key_rejected(self, tmp_path, missing):
        lines = [ln for ln in MINIMAL_YAML.splitlines() if not ln.startswith(missing)]
        path = write_yaml(tmp_path, "\n".join(lines) + "\n")
        with pytest.raises(ValueError, match=missing):
            load_portal_def(path)

    def test_empty_name_rejected(self, tmp_path):
        path = write_yaml(tmp_path, "name: ''\nentry_url_key: portal_12377_base\ncategory: c\n")
        with pytest.raises(ValueError, match="name"):
            load_portal_def(path)

    def test_non_string_category_rejected(self, tmp_path):
        path = write_yaml(tmp_path, "name: n\nentry_url_key: portal_12377_base\ncategory: 123\n")
        with pytest.raises(ValueError, match="category"):
            load_portal_def(path)

    def test_entry_url_key_not_a_config_field_rejected(self, tmp_path):
        path = write_yaml(
            tmp_path, "name: n\nentry_url_key: portal_nonexistent_base\ncategory: c\n"
        )
        with pytest.raises(ValueError, match="portal_nonexistent_base"):
            load_portal_def(path)

    def test_entry_url_key_dunder_rejected(self, tmp_path):
        path = write_yaml(tmp_path, "name: n\nentry_url_key: __init__\ncategory: c\n")
        with pytest.raises(ValueError, match="下划线"):
            load_portal_def(path)

    def test_valid_entry_url_keys_accepted(self, tmp_path):
        for key in ("portal_12377_base", "portal_shdf_base"):
            path = write_yaml(tmp_path, f"name: n\nentry_url_key: {key}\ncategory: c\n")
            assert load_portal_def(path).entry_url_key == key

    def test_top_level_not_mapping_rejected(self, tmp_path):
        path = write_yaml(tmp_path, "- name\n- entry_url_key\n")
        with pytest.raises(ValueError, match="映射"):
            load_portal_def(path)

    def test_malformed_yaml_rejected(self, tmp_path):
        path = write_yaml(tmp_path, "name: [unclosed\n")
        with pytest.raises(ValueError) as excinfo:
            load_portal_def(path)
        assert _cjk(str(excinfo.value))

    def test_unknown_portal_key_rejected(self):
        with pytest.raises(ValueError, match="未知"):
            load_portal_def("no-such-portal")

    def test_nonexistent_builtin_file_raises_file_not_found(self, monkeypatch):
        monkeypatch.setitem(PORTAL_FILES, "ghost", "portals/ghost.yaml")
        with pytest.raises(FileNotFoundError):
            load_portal_def("ghost")


# ---------------------------------------------------------------------------
# selectors 校验
# ---------------------------------------------------------------------------
class TestSelectorsValidation:
    def _full_selectors_block(self, **overrides) -> str:
        values = dict(form_models.SELECTORS)
        values.update(overrides)
        body = "\n".join(f"    {k}: \"{v}\"" for k, v in values.items())
        return MINIMAL_YAML + "selectors:\n" + body + "\n"

    def test_full_valid_override_loads(self, tmp_path):
        path = write_yaml(tmp_path, self._full_selectors_block(url="#custom-url"))
        defn = load_portal_def(path)
        assert defn.selectors["url"] == "#custom-url"
        assert defn.selectors["captcha"] == form_models.SELECTORS["captcha"]

    def test_missing_one_key_rejected(self, tmp_path):
        values = dict(form_models.SELECTORS)
        values.pop("file")
        body = "\n".join(f"    {k}: \"{v}\"" for k, v in values.items())
        path = write_yaml(tmp_path, MINIMAL_YAML + "selectors:\n" + body + "\n")
        with pytest.raises(ValueError, match="file"):
            load_portal_def(path)

    def test_extra_key_rejected(self, tmp_path):
        path = write_yaml(tmp_path, self._full_selectors_block(extra="#nope"))
        with pytest.raises(ValueError, match="extra"):
            load_portal_def(path)

    def test_value_not_hash_prefixed_rejected(self, tmp_path):
        path = write_yaml(tmp_path, self._full_selectors_block(url="report-url"))
        with pytest.raises(ValueError, match="#"):
            load_portal_def(path)

    def test_selectors_not_mapping_rejected(self, tmp_path):
        path = write_yaml(tmp_path, MINIMAL_YAML + "selectors: [url, type]\n")
        with pytest.raises(ValueError, match="selectors"):
            load_portal_def(path)

    def test_aliasing_captcha_selector_rejected_at_load(self, tmp_path):
        """红线:把 captcha 选择器塞给其他字段(别名伪装)在加载期即拒绝。"""
        path = write_yaml(
            tmp_path,
            self._full_selectors_block(url=form_models.SELECTORS["captcha"]),
        )
        with pytest.raises(ValueError, match="别名"):
            load_portal_def(path)


# ---------------------------------------------------------------------------
# build_plan_from_def:入口 URL 与类目
# ---------------------------------------------------------------------------
class TestBuildPlanFromDef:
    def test_entry_url_defaults_to_cfg_field_12377(self):
        cfg = Config(portal_12377_base="http://127.0.0.1:8800/")
        plan = build_plan_from_def(load_portal_def("12377"), make_payload(), cfg)
        assert plan.entry_url == "http://127.0.0.1:8800/"
        assert plan.steps[0].action is StepAction.GOTO
        assert plan.steps[0].value == "http://127.0.0.1:8800/"

    def test_entry_url_defaults_to_cfg_field_shdf(self):
        cfg = Config(portal_shdf_base="http://127.0.0.1:8801/")
        plan = build_plan_from_def(load_portal_def("shdf"), make_payload(Portal.SHDF), cfg)
        assert plan.entry_url == "http://127.0.0.1:8801/"

    def test_explicit_entry_url_overrides_cfg(self):
        cfg = Config(portal_12377_base="http://127.0.0.1:8800/")
        plan = build_plan_from_def(
            load_portal_def("12377"), make_payload(), cfg,
            entry_url="http://127.0.0.1:9000/form",
        )
        assert plan.entry_url == "http://127.0.0.1:9000/form"

    def test_missing_entry_url_rejected(self):
        cfg = Config(portal_12377_base="")
        with pytest.raises(ValueError, match="entry_url"):
            build_plan_from_def(load_portal_def("12377"), make_payload(), cfg)

    def test_non_http_entry_url_rejected(self):
        with pytest.raises(ValueError, match="http"):
            build_plan_from_def(
                load_portal_def("12377"), make_payload(), Config(),
                entry_url="ftp://example.com/form",
            )

    def test_bogus_entry_url_key_rejected(self):
        defn = PortalDef(name="x", entry_url_key="no_such_field", category_value="c")
        with pytest.raises(ValueError, match="no_such_field"):
            build_plan_from_def(defn, make_payload(), Config())

    def test_first_step_meta_marks_portal(self):
        for key, portal in (("12377", Portal.P12377), ("shdf", Portal.SHDF)):
            defn = load_portal_def(key)
            plan = build_plan_from_def(defn, make_payload(portal), Config())
            assert plan.steps[0].meta["portal"] == defn.name

    @pytest.mark.parametrize("key", sorted(PORTAL_FILES))
    def test_category_overrides_payload_and_select_step(self, key):
        defn = load_portal_def(key)
        payload = make_payload()
        assert payload.category == form_models.DEFAULT_CATEGORY  # 被 defn 覆盖前
        plan = build_plan_from_def(defn, payload, Config())
        select_step = plan.steps[2]
        assert select_step.action is StepAction.SELECT
        assert select_step.selector == form_models.SELECTORS["type"]
        assert select_step.value == defn.category_value
        assert plan.payload.category == defn.category_value

    def test_plan_carries_portal_and_payload(self):
        payload = make_payload(Portal.SHDF)
        plan = build_plan_from_def(load_portal_def("shdf"), payload, Config())
        assert plan.portal is Portal.SHDF
        assert plan.payload is payload


# ---------------------------------------------------------------------------
# 篡改防御(红线):验证码只允许人工门
# ---------------------------------------------------------------------------
class TestTamperDefense:
    def _plan_steps(self):
        return build_plan_from_def(
            load_portal_def("12377"), make_payload(), Config()
        ).steps

    def test_captcha_selector_only_in_human_gate(self):
        steps = self._plan_steps()
        captcha = form_models.SELECTORS["captcha"]
        # V10.1:FOCUS(安全聚焦) + HUMAN_GATE(人工输入)各触碰一次 captcha
        hits = [s for s in steps if s.selector == captcha]
        assert len(hits) == 2
        assert {h.action for h in hits} == {StepAction.FOCUS, StepAction.HUMAN_GATE}
        focus, gate = steps[13], steps[14]
        assert focus.action is StepAction.FOCUS
        gate_text = gate.text or ""
        assert "验证码" in gate_text and "人工输入" in gate_text

    def test_assert_accepts_clean_sequence(self):
        assert _assert_no_captcha_autofill(self._plan_steps()) is None

    @pytest.mark.parametrize("action", [StepAction.FILL, StepAction.SELECT, StepAction.CLICK])
    def test_injected_captcha_auto_step_rejected(self, action):
        """负向用例:手工把 captcha 塞进 FILL/SELECT/CLICK 后自检必须抛错。"""
        steps = list(self._plan_steps())
        steps[3] = Step(
            action=action,
            label="篡改:自动处理验证码",
            selector=form_models.SELECTORS["captcha"],
            value="1234",
        )
        with pytest.raises(RuntimeError) as excinfo:
            _assert_no_captcha_autofill(steps)
        message = str(excinfo.value)
        assert _cjk(message)
        assert "验证码" in message

    def test_build_rejects_tampered_selectors_via_monkeypatch(self, monkeypatch):
        """篡改校验:定义合法加载后,把 captcha 选择器别名到 FILL 字段再构建,
        build_plan_from_def 的生成后自检必须拒绝(中文 RuntimeError)。"""
        defn = load_portal_def("12377")
        assert defn.selectors == form_models.SELECTORS
        monkeypatch.setitem(defn.selectors, "url", defn.selectors["captcha"])
        with pytest.raises(RuntimeError, match="验证码"):
            build_plan_from_def(defn, make_payload(), Config())

    def test_build_rejects_tampered_captcha_key_via_monkeypatch(self, monkeypatch):
        """自定义验证码选择器被别名到 url(绕过契约字面量)同样被拒。"""
        defn = load_portal_def("12377")
        monkeypatch.setitem(defn.selectors, "captcha", "#custom-captcha")
        monkeypatch.setitem(defn.selectors, "url", "#custom-captcha")
        with pytest.raises(RuntimeError, match="验证码"):
            build_plan_from_def(defn, make_payload(), Config())

    def test_contract_captcha_monkeypatched_still_caught(self, monkeypatch):
        """自检动态读取契约常量:契约验证码选择器被换后,新值同样受保护。

        V5 更新理由(form_models.SELECTORS 已冻结):原用例以
        ``monkeypatch.setitem`` 原地改写 SELECTORS 模拟篡改,而冻结后原地
        修改本身已不可能(立即 TypeError,防护更强);改为整体替换模块属性,
        仍验证自检在调用时动态读取 ``form_models.SELECTORS`` 的新值。
        """
        steps = [
            Step(action=StepAction.FILL, label="自动填验证码",
                 selector="#tampered-captcha", value="0000"),
        ]
        monkeypatch.setattr(
            form_models, "SELECTORS",
            {**form_models.SELECTORS, "captcha": "#tampered-captcha"},
        )
        with pytest.raises(RuntimeError, match="#tampered-captcha"):
            _assert_no_captcha_autofill(steps)

    def test_plan_always_contains_exactly_one_human_gate(self):
        gates = [s for s in self._plan_steps() if s.action is StepAction.HUMAN_GATE]
        assert len(gates) == 1


# ---------------------------------------------------------------------------
# 与 A12 form_models 计划一致性
# ---------------------------------------------------------------------------
class TestFormModelsConsistency:
    def _reference_plan(self, category: str, portal: Portal, entry_url: str):
        payload = form_models.build_payload(make_entry(), portal, Config())
        payload.category = category
        return form_models.build_plan(payload, Config(), entry_url=entry_url)

    @pytest.mark.parametrize("key, portal", [("12377", Portal.P12377), ("shdf", Portal.SHDF)])
    def test_steps_match_form_models_plan(self, key, portal):
        entry_url = f"http://127.0.0.1:880{0 if portal is Portal.P12377 else 1}/"
        defn = load_portal_def(key)
        plan = build_plan_from_def(defn, make_payload(portal), Config(), entry_url=entry_url)
        ref = self._reference_plan(defn.category_value, portal, entry_url)

        assert len(plan.steps) == len(ref.steps) == 18
        assert [s.action for s in plan.steps] == [s.action for s in ref.steps]
        assert [s.action for s in plan.steps] == EXPECTED_ACTIONS
        for got, want in zip(plan.steps, ref.steps):
            assert (got.label, got.selector, got.value, got.text) == (
                want.label, want.selector, want.value, want.text,
            )
            assert got.meta.get("skippable") == want.meta.get("skippable")

    @pytest.mark.parametrize("key, portal", [("12377", Portal.P12377), ("shdf", Portal.SHDF)])
    def test_agrees_with_v1_portal_planner(self, key, portal):
        """与 A13/A14 的 planner 产出语义一致(步序、标签、类目、首步 meta)。"""
        planner_mod = pytest.importorskip(f"netsentinel.submit.portal_{key}")
        defn = load_portal_def(key)
        entry_url = f"http://127.0.0.1:99{0 if portal is Portal.P12377 else 1}/form"
        ours = build_plan_from_def(defn, make_payload(portal), Config(), entry_url=entry_url)
        theirs = planner_mod.plan_12377(make_entry(), Config(), entry_url=entry_url) \
            if key == "12377" else planner_mod.plan_shdf(make_entry(), Config(), entry_url=entry_url)

        assert len(ours.steps) == len(theirs.steps) == 18
        assert [s.action for s in ours.steps] == [s.action for s in theirs.steps]
        assert [s.label for s in ours.steps] == [s.label for s in theirs.steps]
        assert ours.payload.category == theirs.payload.category == defn.category_value
        assert ours.steps[0].meta["portal"] == theirs.steps[0].meta["portal"] == defn.name
        select_ours = ours.steps[2]
        select_theirs = theirs.steps[2]
        assert select_ours.value == select_theirs.value == defn.category_value


# ---------------------------------------------------------------------------
# V5 升级锁定:单遍校验、路径解析缓存、加载遥测、自检语义不弱化
# ---------------------------------------------------------------------------
class TestV5Upgrades:
    def test_v5_load_telemetry_counter(self):
        telemetry.reset()
        load_portal_def("12377")
        assert telemetry.snapshot()["counters"].get("portal_defs.load") == 1.0

    def test_v5_failed_load_not_counted(self):
        telemetry.reset()
        with pytest.raises(ValueError, match="未知"):
            load_portal_def("no-such-portal")
        assert "portal_defs.load" not in telemetry.snapshot()["counters"]

    def test_v5_builtin_path_resolution_cached(self):
        """内置短名解析结果进模块级缓存:键为 (短名, 相对路径),值为绝对路径。"""
        portal_defs._RESOLVED_BUILTIN_PATHS.clear()
        load_portal_def("12377")
        cache = portal_defs._RESOLVED_BUILTIN_PATHS
        expected_key = ("12377", "portals/12377.yaml")
        assert cache[expected_key] == _PROJECT_ROOT / "portals" / "12377.yaml"
        # 第二次加载直接复用缓存(不再触碰文件系统解析层),结果一致
        assert load_portal_def("12377") == PortalDef(
            name="中央网信办违法和不良信息举报中心(12377)",
            entry_url_key="portal_12377_base",
            category_value="色情低俗信息",
            selectors=dict(form_models.SELECTORS),
            selector_chains=_builtin_expected_chains(),
            note=load_portal_def("12377").note,
        )

    def test_v5_cache_re_resolves_when_builtin_mapping_repointed(self, monkeypatch, tmp_path):
        """PORTAL_FILES 被重定向(键含相对路径)后按新路径重新解析,不吐陈旧缓存。"""
        alt = write_yaml(tmp_path, MINIMAL_YAML)
        monkeypatch.setitem(PORTAL_FILES, "12377", str(alt))
        assert load_portal_def("12377").name == "测试门户"

    def test_v5_explicit_paths_bypass_builtin_cache(self, tmp_path):
        """按路径加载不走内置短名缓存(每次仍按该路径读取校验)。"""
        path = write_yaml(tmp_path, MINIMAL_YAML)
        before = dict(portal_defs._RESOLVED_BUILTIN_PATHS)
        defn = load_portal_def(path)
        assert defn.name == "测试门户"
        assert portal_defs._RESOLVED_BUILTIN_PATHS == before

    def test_v5_single_pass_validation_error_priority_preserved(self, tmp_path):
        """单遍扫描后错误优先级保持:未知键 > 缺必填键(文案不变)。"""
        lines = [ln for ln in MINIMAL_YAML.splitlines() if not ln.startswith("name")]
        path = write_yaml(tmp_path, "\n".join(lines) + "\nbogus: 1\n")
        with pytest.raises(ValueError, match="未知键"):
            load_portal_def(path)

    def test_v5_missing_required_still_reports_first_missing_key(self, tmp_path):
        lines = [
            ln for ln in MINIMAL_YAML.splitlines()
            if not ln.startswith(("name", "entry_url_key"))
        ]
        path = write_yaml(tmp_path, "\n".join(lines) + "\n")
        with pytest.raises(ValueError, match="name"):
            load_portal_def(path)

    def test_v5_assert_covers_extra_captcha_selectors_argument(self):
        """captcha_selectors 补充参数:自定义验证码选择器同样受保护(空串被忽略)。"""
        bad = [Step(action=StepAction.FILL, label="自动填自定义验证码",
                    selector="#custom-cap", value="0000")]
        with pytest.raises(RuntimeError, match="#custom-cap"):
            _assert_no_captcha_autofill(bad, ["#custom-cap", ""])
        # 未提供补充值时,自定义选择器不属于比对集合(契约值仍受保护)
        with pytest.raises(RuntimeError, match="验证码"):
            _assert_no_captcha_autofill(bad + [
                Step(action=StepAction.CLICK, label="点验证码",
                     selector=form_models.SELECTORS["captcha"]),
            ])

    def test_v5_assert_non_auto_actions_on_captcha_still_allowed(self):
        """既有语义锁定(不弱化也不扩大):自检只拒绝 FILL/SELECT/CLICK,
        GOTO/WAIT/SCREENSHOT 携带验证码选择器不触发(它们不产生输入)。"""
        for action in (StepAction.GOTO, StepAction.WAIT, StepAction.SCREENSHOT):
            steps = [Step(action=action, label="x",
                          selector=form_models.SELECTORS["captcha"])]
            assert _assert_no_captcha_autofill(steps) is None
        for action in (StepAction.FILL, StepAction.SELECT, StepAction.CLICK):
            steps = [Step(action=action, label="x",
                          selector=form_models.SELECTORS["captcha"])]
            with pytest.raises(RuntimeError, match="验证码"):
                _assert_no_captcha_autofill(steps)


# ---------------------------------------------------------------------------
# 自愈选择器候选链(schema v2:解析归一化 / 逐候选校验 / 验证码逐候选拒绝)
# ---------------------------------------------------------------------------
def _render_chain(chain: list) -> str:
    """把候选链渲染为 YAML 值:JSON 流式语法是合法 YAML,空映射/异型值不丢失。"""
    import json

    return json.dumps(chain, ensure_ascii=False)


def _full_v2_selectors_block(**overrides) -> str:
    """构造完整 selectors 块:缺省每键 v1 单值字符串,overrides 提供候选链。"""
    parts: list[str] = []
    for key, css in form_models.SELECTORS.items():
        chain = overrides.get(key, css)
        if isinstance(chain, str):
            parts.append(f'  {key}: "{chain}"')
        else:
            parts.append(f"  {key}: {_render_chain(chain)}")
    return MINIMAL_YAML + "selectors:\n" + "\n".join(parts) + "\n"


class TestSelectorChainsV2:
    """v2 候选链解析:v1 归一化、链结构、混合语法、逐候选校验拒绝。"""

    def test_minimal_definition_default_chains_are_single_css(self, tmp_path):
        defn = load_portal_def(write_yaml(tmp_path, MINIMAL_YAML))
        expected = {key: [{"css": css}] for key, css in form_models.SELECTORS.items()}
        assert defn.selector_chains == expected

    def test_v1_string_selectors_normalized_to_single_element_chains(self, tmp_path):
        path = write_yaml(tmp_path, _full_v2_selectors_block(url="#custom-url"))
        defn = load_portal_def(path)
        assert defn.selector_chains["url"] == [{"css": "#custom-url"}]
        assert defn.selectors["url"] == "#custom-url"

    def test_mixed_v1_v2_syntax_per_key_loads(self, tmp_path):
        """同一 selectors 块里 v1 字符串与 v2 链按键混用(逐键归一化)。"""
        path = write_yaml(
            tmp_path,
            _full_v2_selectors_block(
                url=["#report-url", {"role": "textbox", "name": "举报链接"}, {"label": "举报链接"}],
            ),
        )
        defn = load_portal_def(path)
        assert defn.selector_chains["url"] == [
            {"css": "#report-url"},
            {"role": "textbox", "name": "举报链接"},
            {"label": "举报链接"},
        ]
        assert defn.selector_chains["type"] == [{"css": "#report-type"}]

    def test_chain_order_and_candidate_shapes_preserved(self, tmp_path):
        chain = [
            "#report-desc",
            {"placeholder": "请描述违法事实"},
            {"text": "具体描述"},
            {"label": "具体描述"},
        ]
        path = write_yaml(tmp_path, _full_v2_selectors_block(desc=chain))
        defn = load_portal_def(path)
        assert defn.selector_chains["desc"] == [
            {"css": "#report-desc"},
            {"placeholder": "请描述违法事实"},
            {"text": "具体描述"},
            {"label": "具体描述"},
        ]

    @pytest.mark.parametrize("key", sorted(PORTAL_FILES))
    def test_builtin_yaml_chains_match_expected_v2_shape(self, key):
        """两份内置 YAML 均为 v2 候选链:id 打头 + aria/label 兜底(锁定)。"""
        defn = load_portal_def(key)
        assert defn.selector_chains == _builtin_expected_chains()
        for field_key, chain in defn.selector_chains.items():
            assert chain and chain[0] == {"css": defn.selectors[field_key]}

    @pytest.mark.parametrize(
        ("bad_chain", "match"),
        [
            (["#report-url", {"xpath": "//input"}], "未知键"),
            ([], "不能是空列表"),
            (["#report-url", {}], "空映射"),
            (["#report-url", 123], "必须是映射"),
            (["#report-url", {"css": "#a", "label": "b"}], "一种类型"),
            (["#report-url", {"role": "textbox"}], "成对出现"),
            (["#report-url", {"name": "举报链接"}], "成对出现"),
            ([{"label": "举报链接"}, "#report-url"], "打头"),
            (["#report-url", {"css": "plain-id"}], "#"),
            (["#report-url", {"css": ""}], "非空字符串"),
        ],
    )
    def test_invalid_chains_rejected_with_chinese_errors(self, tmp_path, bad_chain, match):
        path = write_yaml(tmp_path, _full_v2_selectors_block(url=bad_chain))
        with pytest.raises(ValueError, match=match) as excinfo:
            load_portal_def(path)
        assert _cjk(str(excinfo.value))
        assert "url" in str(excinfo.value)  # 错误信息带字段名(计划作者可读)

    def test_selectors_value_of_wrong_type_rejected(self, tmp_path):
        # 全键齐全但 url 的值为 int:既不是 v1 字符串也不是 v2 链 → 报类型
        full = MINIMAL_YAML + "selectors:\n" + "\n".join(
            f'  {k}: 123' if k == "url" else f'  {k}: "{v}"'
            for k, v in form_models.SELECTORS.items()
        ) + "\n"
        path = write_yaml(tmp_path, full)
        with pytest.raises(ValueError, match="候选链列表"):
            load_portal_def(path)


class TestCaptchaAliasPerCandidate:
    """红线:验证码别名拒绝对候选链逐候选生效(任何形式指向验证码即拒绝)。"""

    def test_captcha_css_alias_in_chain_tail_rejected(self, tmp_path):
        chain = ["#report-url", "#report-captcha"]
        path = write_yaml(tmp_path, _full_v2_selectors_block(url=chain))
        with pytest.raises(ValueError, match="别名") as excinfo:
            load_portal_def(path)
        assert "第 2 个候选" in str(excinfo.value)

    def test_custom_captcha_css_alias_in_chain_tail_rejected(self, tmp_path):
        """自定义验证码 css 被别名到 url 链尾:双保险字面量兜底同样拒绝。"""
        chain = ["#report-url", "#cap-x"]
        path = write_yaml(
            tmp_path,
            _full_v2_selectors_block(url=chain, captcha=["#cap-x", {"label": "图形校验码"}]),
        )
        with pytest.raises(ValueError, match="别名"):
            load_portal_def(path)

    @pytest.mark.parametrize(
        ("cand", "field"),
        [
            ({"label": "验证码(仅限人工输入)"}, "url"),
            ({"placeholder": "请输入验证码"}, "desc"),
            ({"text": "验证码"}, "name"),
            ({"role": "textbox", "name": "短信验证码"}, "phone"),
        ],
    )
    def test_textlike_candidate_with_captcha_word_rejected(self, tmp_path, cand, field):
        """文本型候选含"验证码"字样 = 语义上指向验证码字段,加载期拒绝。"""
        path = write_yaml(
            tmp_path,
            _full_v2_selectors_block(**{field: [form_models.SELECTORS[field], cand]}),
        )
        with pytest.raises(ValueError, match="别名") as excinfo:
            load_portal_def(path)
        assert "验证码" in str(excinfo.value)

    def test_exact_alias_of_custom_captcha_label_rejected(self, tmp_path):
        """不含"验证码"字样的自定义验证码文案(如"图形校验码")被精确别名
        到普通字段:与 captcha 声明链文本值相同 → 同样拒绝(防换措辞绕过)。"""
        path = write_yaml(
            tmp_path,
            _full_v2_selectors_block(
                url=["#report-url", {"label": "图形校验码"}],
                captcha=["#cap-x", {"label": "图形校验码"}],
            ),
        )
        with pytest.raises(ValueError, match="别名"):
            load_portal_def(path)

    def test_captcha_key_own_textlike_candidates_allowed(self, tmp_path):
        """captcha 键自己的链可以声明验证码文案(供人工门语义,合法)。"""
        path = write_yaml(
            tmp_path,
            _full_v2_selectors_block(captcha=["#cap-x", {"label": "图形校验码"}]),
        )
        defn = load_portal_def(path)
        assert defn.selector_chains["captcha"] == [
            {"css": "#cap-x"},
            {"label": "图形校验码"},
        ]

    def test_assert_rejects_clean_selector_with_captcha_chain_candidate(self):
        """自检(篡改防御):主选择器干净、候选链夹带验证码 css → Runtime错。"""
        steps = [
            Step(
                action=StepAction.FILL,
                label="伪装的链接填写",
                selector="#report-url",
                value="http://x",
                meta={"selector_chain": [
                    {"css": "#report-url"},
                    {"css": "#report-captcha"},
                ]},
            ),
        ]
        with pytest.raises(RuntimeError, match="验证码") as excinfo:
            _assert_no_captcha_autofill(steps)
        assert "第 2 个候选" in str(excinfo.value)

    def test_assert_rejects_captcha_word_in_chain_textlike(self):
        for cand in (
            {"label": "验证码"},
            {"placeholder": "输入验证码"},
            {"text": "验证码"},
            {"role": "textbox", "name": "验证码"},
        ):
            steps = [
                Step(
                    action=StepAction.CLICK,
                    label="伪装点击",
                    selector="#x",
                    meta={"selector_chain": [{"css": "#x"}, dict(cand)]},
                ),
            ]
            with pytest.raises(RuntimeError, match="候选链"):
                _assert_no_captcha_autofill(steps)

    def test_assert_accepts_clean_chain_on_auto_steps(self):
        steps = [
            Step(
                action=StepAction.FILL,
                label="填写举报链接",
                selector="#report-url",
                value="http://x",
                meta={"selector_chain": [
                    {"css": "#report-url"},
                    {"role": "textbox", "name": "举报链接"},
                    {"label": "举报链接"},
                ]},
            ),
        ]
        assert _assert_no_captcha_autofill(steps) is None

    def test_build_rejects_tampered_chain_via_monkeypatch(self, monkeypatch):
        """篡改校验:定义合法加载后把验证码候选塞进 url 链尾,构建期自检拒绝。"""
        defn = load_portal_def("12377")
        tampered = [dict(c) for c in defn.selector_chains["url"]]
        tampered.append({"css": "#report-captcha"})
        monkeypatch.setitem(defn.selector_chains, "url", tampered)
        with pytest.raises(RuntimeError, match="候选链"):
            build_plan_from_def(defn, make_payload(), Config())


class TestPlanCarriesChains:
    """build_plan_from_def:候选链进入 SELECT/FILL/CLICK 步骤 meta。"""

    def _plan(self, entry_url: str = "http://127.0.0.1:9900/form"):
        defn = load_portal_def("12377")
        return build_plan_from_def(defn, make_payload(), Config(), entry_url=entry_url)

    def test_auto_steps_carry_chain_first_candidate_equals_selector(self):
        plan = self._plan()
        auto_steps = [
            s for s in plan.steps
            if s.action in (StepAction.FILL, StepAction.SELECT, StepAction.CLICK)
        ]
        assert auto_steps, "计划应包含 FILL/SELECT/CLICK 步骤"
        for step in auto_steps:
            chain = step.meta.get("selector_chain")
            assert isinstance(chain, list) and chain, f"{step.label} 缺少候选链"
            assert chain[0] == {"css": step.selector}, f"{step.label} 链首与主选择器脱钩"
            assert all(isinstance(c, dict) for c in chain)

    def test_chain_declares_expected_builtin_shape(self):
        plan = self._plan()
        by_label = {s.label: s for s in plan.steps}
        assert by_label["填写举报链接"].meta["selector_chain"] == [
            {"css": "#report-url"},
            {"role": "textbox", "name": "举报链接"},
            {"label": "举报链接"},
        ]
        assert by_label["选择信息类型"].meta["selector_chain"] == [
            {"css": "#report-type"},
            {"role": "combobox", "name": "举报类型"},
            {"label": "举报类型"},
        ]
        assert by_label["点击提交"].meta["selector_chain"] == [
            {"css": "#report-submit"},
            {"role": "button", "name": "提交举报"},
            {"text": "提交举报"},
        ]

    def test_skippable_meta_coexists_with_chain(self):
        plan = self._plan()
        name_step = next(s for s in plan.steps if s.label == "填写举报人姓名")
        assert name_step.meta["skippable"] is True
        assert name_step.meta["selector_chain"][0] == {"css": name_step.selector}

    def test_human_gate_focus_and_non_auto_steps_have_no_chain(self):
        """红线:验证码相关步骤(FOCUS/HUMAN_GATE)与等待/截图/导航步骤
        不携带候选链 —— 验证码语义保持 v1 形态,不受自愈机制影响。"""
        plan = self._plan()
        for step in plan.steps:
            if step.action in (StepAction.FILL, StepAction.SELECT, StepAction.CLICK):
                continue
            assert "selector_chain" not in step.meta, f"{step.label} 不应携带候选链"

    def test_chain_objects_are_fresh_copies(self):
        """步骤候选链是独立拷贝:改步骤链不影响定义对象(防共享可变状态)。"""
        defn = load_portal_def("12377")
        plan = build_plan_from_def(defn, make_payload(), Config(), entry_url="http://127.0.0.1:9900/f")
        url_step = next(s for s in plan.steps if s.label == "填写举报链接")
        url_step.meta["selector_chain"].append({"label": "篡改"})
        assert defn.selector_chains["url"][-1] != {"label": "篡改"}

    def test_default_defn_builds_single_css_chains(self):
        """手工构造(未带链)的 PortalDef 构建计划 → 链退化为单 css 候选。"""
        defn = PortalDef(name="x", entry_url_key="portal_12377_base", category_value="c")
        plan = build_plan_from_def(defn, make_payload(), Config(), entry_url="http://127.0.0.1:9900/g")
        url_step = next(s for s in plan.steps if s.label == "填写举报链接")
        assert url_step.meta["selector_chain"] == [{"css": "#report-url"}]


# ---------------------------------------------------------------------------
# A233:门户动态发现与登记闭环(discover / enabled 门槛 / 内置豁免)
# ---------------------------------------------------------------------------
class TestDynamicDiscovery:
    """discover_portals:无动态文件=空 / 坏 YAML 错误标注 / enabled 门槛 / 内置排除。"""

    @staticmethod
    def _portals_dir(tmp_path: Path) -> Path:
        portals = tmp_path / "portals"
        portals.mkdir(exist_ok=True)
        return portals

    @staticmethod
    def _write(portals: Path, filename: str, text: str) -> Path:
        path = portals / filename
        path.write_text(text, encoding="utf-8")
        return path

    def test_empty_directory_discovers_nothing(self, tmp_path):
        assert portal_defs.discover_portals(self._portals_dir(tmp_path)) == {}

    def test_missing_directory_discovers_nothing(self, tmp_path):
        assert portal_defs.discover_portals(tmp_path / "no-such-dir") == {}

    def test_default_directory_is_project_portals_and_clean(self):
        """红线:项目 portals/ 目录当前无任何动态门户(仅内置两文件,
        被排除)——发现闭环不引入任何未经人工确认的提交通道。"""
        assert portal_defs.discover_portals() == {}

    def test_builtin_filenames_excluded_from_discovery(self, tmp_path):
        portals = self._portals_dir(tmp_path)
        self._write(portals, "12377.yaml", MINIMAL_YAML)
        self._write(portals, "shdf.yaml", MINIMAL_YAML)
        assert portal_defs.discover_portals(portals) == {}

    def test_non_yaml_files_ignored(self, tmp_path):
        portals = self._portals_dir(tmp_path)
        self._write(portals, "notes.txt", MINIMAL_YAML)
        self._write(portals, "draft.yml", MINIMAL_YAML)
        assert portal_defs.discover_portals(portals) == {}

    def test_malformed_yaml_annotated_as_load_error(self, tmp_path):
        portals = self._portals_dir(tmp_path)
        self._write(portals, "broken.yaml", "name: [unclosed\n")
        found = portal_defs.discover_portals(portals)
        assert set(found) == {"broken"}
        assert isinstance(found["broken"], ValueError)
        assert "合法 YAML" in str(found["broken"])

    def test_invalid_definition_annotated_as_load_error(self, tmp_path):
        portals = self._portals_dir(tmp_path)
        self._write(portals, "badkey.yaml", MINIMAL_YAML + "bogus: 1\n")
        found = portal_defs.discover_portals(portals)
        assert isinstance(found["badkey"], ValueError)
        assert "未知键" in str(found["badkey"])

    def test_bad_entry_url_key_annotated_as_load_error(self, tmp_path):
        """门槛之二:entry_url_key 不在 Config 字段集 → 动态发现即加载错误
        (动态 YAML 绝不许指向不存在的配置字段)。"""
        portals = self._portals_dir(tmp_path)
        self._write(
            portals,
            "nokey.yaml",
            "enabled: true\nname: n\nentry_url_key: portal_ghost_base\ncategory: c\n",
        )
        found = portal_defs.discover_portals(portals)
        assert isinstance(found["nokey"], ValueError)
        assert "portal_ghost_base" in str(found["nokey"])

    def test_enabled_true_discovers_enabled_portal(self, tmp_path):
        portals = self._portals_dir(tmp_path)
        self._write(portals, "newdyn.yaml", "enabled: true\n" + MINIMAL_YAML)
        found = portal_defs.discover_portals(portals)
        assert set(found) == {"newdyn"}
        defn = found["newdyn"]
        assert isinstance(defn, PortalDef)
        assert defn.enabled is True
        assert defn.name == "测试门户"

    def test_enabled_false_discovered_but_marked_disabled(self, tmp_path):
        portals = self._portals_dir(tmp_path)
        self._write(portals, "offdyn.yaml", "enabled: false\n" + MINIMAL_YAML)
        defn = portal_defs.discover_portals(portals)["offdyn"]
        assert isinstance(defn, PortalDef)
        assert defn.enabled is False  # disabled 标注(发现 ≠ 生效)

    def test_missing_enabled_key_marked_disabled(self, tmp_path):
        """门槛要求**显式**声明:未写 enabled 的动态门户按未启用标注。"""
        portals = self._portals_dir(tmp_path)
        self._write(portals, "nodecl.yaml", MINIMAL_YAML)
        defn = portal_defs.discover_portals(portals)["nodecl"]
        assert isinstance(defn, PortalDef)
        assert defn.enabled is None

    def test_enabled_non_bool_rejected_at_load(self, tmp_path):
        path = write_yaml(tmp_path, 'enabled: "true"\n' + MINIMAL_YAML)
        with pytest.raises(ValueError, match="布尔"):
            load_portal_def(path)

    def test_builtin_defs_exempt_enabled_is_none(self):
        """兼容豁免:两内置门户不写 enabled 字段也照常加载(值为 None)。"""
        for key in sorted(PORTAL_FILES):
            defn = load_portal_def(key)
            assert defn.enabled is None
            assert defn.entry_url_key  # 照常可用

    def test_discovery_deterministic_and_sorted(self, tmp_path):
        portals = self._portals_dir(tmp_path)
        for name in ("zeta.yaml", "alpha.yaml", "mid.yaml"):
            self._write(portals, name, "enabled: true\n" + MINIMAL_YAML)
        found = portal_defs.discover_portals(portals)
        assert list(found) == ["alpha", "mid", "zeta"]
        assert portal_defs.discover_portals(portals) == found


class TestListAndGetPortals:
    """list_portals / get_portal:内置恒含(豁免)、动态仅 enabled、顺序与拒绝。"""

    @staticmethod
    def _dyn_portals(tmp_path: Path) -> Path:
        """构造含启用/禁用/未声明/损坏四类动态文件的 portals 目录。"""
        portals = tmp_path / "portals"
        portals.mkdir()
        (portals / "ondyn.yaml").write_text(
            "enabled: true\n" + MINIMAL_YAML, encoding="utf-8")
        (portals / "offdyn.yaml").write_text(
            "enabled: false\n" + MINIMAL_YAML, encoding="utf-8")
        (portals / "nodecl.yaml").write_text(MINIMAL_YAML, encoding="utf-8")
        (portals / "broken.yaml").write_text("name: [unclosed\n", encoding="utf-8")
        return portals

    def test_list_portals_builtins_always_present_without_dynamic(self, tmp_path):
        portals = tmp_path / "portals"
        portals.mkdir()
        combined = portal_defs.list_portals(directory=portals)
        assert set(combined) == set(PORTAL_FILES)
        assert combined["12377"].entry_url_key == "portal_12377_base"
        assert combined["shdf"].entry_url_key == "portal_shdf_base"

    def test_list_portals_default_excludes_disabled_and_undeclared(self, tmp_path):
        portals = self._dyn_portals(tmp_path)
        combined = portal_defs.list_portals(directory=portals)
        assert set(combined) == {"12377", "shdf", "ondyn"}
        assert combined["ondyn"].enabled is True

    def test_list_portals_include_disabled_lists_all_dynamic(self, tmp_path):
        portals = self._dyn_portals(tmp_path)
        combined = portal_defs.list_portals(include_disabled=True, directory=portals)
        assert set(combined) == {"12377", "shdf", "ondyn", "offdyn", "nodecl"}
        assert combined["offdyn"].enabled is False
        assert combined["nodecl"].enabled is None

    def test_list_portals_skips_broken_files(self, tmp_path):
        portals = self._dyn_portals(tmp_path)
        combined = portal_defs.list_portals(include_disabled=True, directory=portals)
        assert "broken" not in combined  # 错误文件不中断列举(详情看 discover)

    def test_get_portal_builtin_priority_and_unchanged(self):
        """查找顺序=内置优先:内置短名仍走 PORTAL_FILES,行为与 A53 起零差别。"""
        assert portal_defs.get_portal("12377") == load_portal_def("12377")
        assert portal_defs.get_portal("shdf").entry_url_key == "portal_shdf_base"

    def test_get_portal_dynamic_enabled(self, tmp_path):
        portals = self._dyn_portals(tmp_path)
        defn = portal_defs.get_portal("ondyn", directory=portals)
        assert defn.name == "测试门户"
        assert defn.enabled is True

    def test_get_portal_dynamic_disabled_rejected(self, tmp_path):
        """红线:enabled: false 的动态门户 get_portal 拒绝(绝不自动生效)。"""
        portals = self._dyn_portals(tmp_path)
        with pytest.raises(ValueError, match="尚未启用") as excinfo:
            portal_defs.get_portal("offdyn", directory=portals)
        assert "enabled" in str(excinfo.value)

    def test_get_portal_dynamic_undeclared_rejected(self, tmp_path):
        portals = self._dyn_portals(tmp_path)
        with pytest.raises(ValueError, match="尚未启用"):
            portal_defs.get_portal("nodecl", directory=portals)

    def test_get_portal_dynamic_broken_rejected(self, tmp_path):
        portals = self._dyn_portals(tmp_path)
        with pytest.raises(ValueError, match="加载失败"):
            portal_defs.get_portal("broken", directory=portals)

    def test_get_portal_unknown_rejected(self, tmp_path):
        portals = self._dyn_portals(tmp_path)
        with pytest.raises(ValueError, match="未知"):
            portal_defs.get_portal("ghost", directory=portals)
