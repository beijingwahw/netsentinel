"""A12 单元测试:netsentinel/submit/form_models.py(离线,仅标准库 + pytest)。

覆盖:
- SELECTORS 与 CONTRACTS.md §3 完全一致(硬编码断言);
- build_payload:中文模板 / verdict 枚举与字符串 / extra 拼接 / 别名与缺省容错;
- SubmissionPayload.validate:三类中文错误与合法通过;
- build_plan:步骤序列(契约 §5,共 12 步,HUMAN_GATE 为第 9 步)、
  验证码只允许人工门(绝不自动填写)、entry_url 门户缺省、skippable 标记;
- plan_from_entry 组合行为。
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from netsentinel import telemetry
from netsentinel.contracts import (
    Config,
    Portal,
    Step,
    StepAction,
    SubmissionPayload,
    Verdict,
)
from netsentinel.submit import form_models

#: 契约 §5 固定顺序(共 12 步;HUMAN_GATE 位于第 9 步 / 索引 8)。
EXPECTED_ACTIONS: list[StepAction] = [
    StepAction.GOTO,       # 1 打开举报入口
    StepAction.WAIT,       # 2 等待 1s
    StepAction.SELECT,     # 3 选择信息类型
    StepAction.FILL,       # 4 举报链接
    StepAction.FILL,       # 5 具体描述
    StepAction.FILL,       # 6 举报人姓名(skippable)
    StepAction.FILL,       # 7 举报人电话(skippable)
    StepAction.FILL,       # 8 电子邮箱(skippable, V10.2)
    StepAction.FILL,       # 9 身份证号(skippable, V10.2)
    StepAction.FILL,       # 10 通讯地址(skippable, V10.2)
    StepAction.FILL,       # 11 邮政编码(skippable, V10.2)
    StepAction.FILL,       # 12 单位名称(skippable, V10.2)
    StepAction.SCREENSHOT, # 13 填写完成后截图
    StepAction.FOCUS,      # 14 聚焦验证码输入框(V10.1 半自动)
    StepAction.HUMAN_GATE, # 15 人工在浏览器输入验证码+回车
    StepAction.CLICK,      # 16 点击提交
    StepAction.WAIT,       # 17 等待 2s
    StepAction.SCREENSHOT, # 18 提交结果截图
]


def _cjk(text: str) -> bool:
    """错误文案应为中文。"""
    return re.search(r"[\u4e00-\u9fff]", text) is not None


def make_entry(**overrides) -> SimpleNamespace:
    """构造 entry_like 桩对象;传 None 表示删除该属性(测容错)。"""
    fields: dict = {
        "site_url": "http://bad.example.com/",
        "verdict": Verdict.NSFW,
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


# ---------------------------------------------------------------------------
# SELECTORS 契约
# ---------------------------------------------------------------------------

class TestSelectors:
    def test_matches_contract_exactly(self):
        assert form_models.SELECTORS == {
        "url": "#report-url",
        "type": "#report-type",
        "desc": "#report-desc",
        "name": "#report-name",
        "phone": "#report-phone",
        "email": "#report-email",
        "id": "#report-idcard",
        "address": "#report-address",
        "postcode": "#report-postcode",
        "org": "#report-org",
        "file": "#report-file",
        "captcha": "#report-captcha",
        "submit": "#report-submit",
    }

    def test_eight_keys_only(self):
        assert set(form_models.SELECTORS) == {
        "url", "type", "desc", "name", "phone",
        "email", "id", "address", "postcode", "org",
        "file", "captcha", "submit",
    }

    def test_default_category(self):
        assert form_models.DEFAULT_CATEGORY == "色情低俗"


# ---------------------------------------------------------------------------
# build_payload
# ---------------------------------------------------------------------------

class TestBuildPayload:
    def test_description_exact_template(self):
        # V10:自动理由(≤39字)作为首行,正文模板逐字保持
        payload = form_models.build_payload(make_entry(), Portal.P12377, Config())
        first_line, _, body = payload.description.partition(chr(10))
        assert first_line == "bad.example.com抽查3页,3张图涉色情内容,已人工核实"
        assert len(first_line) <= 39
        assert payload.reason == first_line
        assert body == (
            "举报站点:http://bad.example.com/。"
            "经图像识别辅助系统初筛并经人工核实,该站点多张抽样页面图片含色情低俗内容"
            "(站点聚合最高分值 0.97,达标图片 3 张,抽样页面 3 个)。"
            "证据材料见附件 zip。"
            "以上信息已由举报人人工核实确认。"
        )

    def test_description_mentions_site_and_human_confirm(self):
        payload = form_models.build_payload(make_entry(), Portal.SHDF, Config())
        assert "http://bad.example.com/" in payload.description
        assert "经图像识别辅助系统初筛并经人工核实" in payload.description
        assert payload.description.endswith("以上信息已由举报人人工核实确认。")

    def test_verdict_enum_and_str_both_accepted(self):
        cfg = Config()
        from_enum = form_models.build_payload(
            make_entry(verdict=Verdict.NSFW), Portal.P12377, cfg
        )
        from_str = form_models.build_payload(
            make_entry(verdict="nsfw"), Portal.P12377, cfg
        )
        assert from_enum.description == from_str.description
        assert from_enum.site_url == from_str.site_url == "http://bad.example.com/"

    def test_extra_is_appended_before_final_claim(self):
        payload = form_models.build_payload(
            make_entry(), Portal.P12377, Config(), extra="补充:首页含弹窗广告。"
        )
        assert "证据材料见附件 zip。补充:首页含弹窗广告。" in payload.description
        assert payload.description.endswith("以上信息已由举报人人工核实确认。")

    def test_evidence_zip_path_alias(self):
        entry = make_entry(evidence_zip=None, evidence_zip_path="data/evidence/alt.zip")
        payload = form_models.build_payload(entry, Portal.P12377, Config())
        assert payload.evidence_zip == "data/evidence/alt.zip"

    def test_missing_stats_default_to_zero(self):
        entry = make_entry(agg_nsw_prob=None, nsw_image_count=None, pages=None)
        payload = form_models.build_payload(entry, Portal.P12377, Config())
        assert "站点聚合最高分值 0.00" in payload.description
        assert "达标图片 0 张" in payload.description
        assert "抽样页面 0 个" in payload.description

    def test_pages_as_plain_int(self):
        payload = form_models.build_payload(
            make_entry(pages=7), Portal.P12377, Config()
        )
        assert "抽样页面 7 个" in payload.description

    def test_fields_passthrough(self):
        payload = form_models.build_payload(
            make_entry(),
            Portal.SHDF,
            Config(),
            reporter_name="张三",
            reporter_phone="13800000000",
        )
        assert payload.portal == Portal.SHDF
        assert payload.category == form_models.DEFAULT_CATEGORY
        assert payload.reporter_name == "张三"
        assert payload.reporter_phone == "13800000000"
        assert payload.site_url == "http://bad.example.com/"
        assert payload.evidence_zip == "data/evidence/bad.example.com_20260101.zip"

    def test_empty_evidence_zip_still_builds_then_validate_catches(self):
        payload = form_models.build_payload(
            make_entry(evidence_zip=""), Portal.P12377, Config()
        )
        assert payload.evidence_zip == ""
        errors = payload.validate()
        assert any("evidence_zip" in e for e in errors)


# ---------------------------------------------------------------------------
# SubmissionPayload.validate(契约规定的行为,由本模块测试锁定)
# ---------------------------------------------------------------------------

class TestValidate:
    def _valid_payload(self) -> SubmissionPayload:
        return SubmissionPayload(
            portal=Portal.P12377,
            site_url="http://bad.example.com/",
            description="这是一段足够长的合法举报描述," * 3,
            evidence_zip="data/evidence/whatever.zip",
        )

    def test_valid_payload_has_no_errors(self):
        assert self._valid_payload().validate() == []

    @pytest.mark.parametrize("bad_desc", ["", "   \n\t ", "太短的描述"])
    def test_short_description_rejected_in_chinese(self, bad_desc):
        payload = self._valid_payload()
        payload.description = bad_desc
        errors = payload.validate()
        matched = [e for e in errors if "description" in e]
        assert matched and all(_cjk(e) for e in matched)

    def test_empty_evidence_zip_rejected_in_chinese(self):
        payload = self._valid_payload()
        payload.evidence_zip = ""
        errors = payload.validate()
        matched = [e for e in errors if "zip" in e]
        assert matched and all(_cjk(e) for e in matched)

    @pytest.mark.parametrize(
        "bad_url", ["", "ftp://bad.example.com/", "bad.example.com", "www.bad.example.com"]
    )
    def test_non_http_site_url_rejected_in_chinese(self, bad_url):
        payload = self._valid_payload()
        payload.site_url = bad_url
        errors = payload.validate()
        matched = [e for e in errors if "http" in e]
        assert matched and all(_cjk(e) for e in matched)


# ---------------------------------------------------------------------------
# build_plan
# ---------------------------------------------------------------------------

class TestBuildPlan:
    def _payload(self, **kw) -> SubmissionPayload:
        return form_models.build_payload(make_entry(), Portal.P12377, Config(), **kw)

    def _steps(self, payload: SubmissionPayload, cfg: Config | None = None,
               entry_url: str | None = None) -> list[Step]:
        return form_models.build_plan(payload, cfg or Config(), entry_url).steps

    def test_step_sequence_order_and_actions(self):
        steps = self._steps(self._payload())
        assert len(steps) == len(EXPECTED_ACTIONS) == 18
        assert [s.action for s in steps] == EXPECTED_ACTIONS
        assert all(isinstance(s, Step) for s in steps)

    def test_step9_is_human_gate_targeting_captcha(self):
        steps = self._steps(self._payload())
        gate = steps[14]  # 第 9 步
        assert gate.action is StepAction.HUMAN_GATE
        assert gate.selector == form_models.SELECTORS["captcha"]
        assert "验证码仅限人工输入" in gate.text and "光标已就位" in gate.text
        assert "验证码" in gate.label
        assert "验证码" in gate.label

    def test_no_step_ever_fills_captcha(self):
        steps = self._steps(self._payload())
        captcha_steps = [s for s in steps if s.selector == form_models.SELECTORS["captcha"] and s.action is not form_models.StepAction.FOCUS]
        # 旧写法保留供参考:
        # captcha_steps_old = []
        # 验证码只出现在 HUMAN_GATE,绝不存在 FILL(或其它自动动作)到验证码。
        assert len(captcha_steps) == 1
        assert captcha_steps[0].action is StepAction.HUMAN_GATE
        assert not any(
            s.action is StepAction.FILL
            and s.selector == form_models.SELECTORS["captcha"]
            for s in steps
        )

    def test_exactly_one_human_gate(self):
        steps = self._steps(self._payload())
        gates = [s for s in steps if s.action is StepAction.HUMAN_GATE]
        assert len(gates) == 1

    def test_step_selectors_values_and_labels(self):
        payload = self._payload(reporter_name="张三", reporter_phone="13800000000")
        (goto, wait1, sel, f_url, f_desc, f_name, f_phone, f_email, f_id, f_addr, f_pc, f_org, shot, focus, gate, click, wait2, shot2) = self._steps(payload)
        assert (goto.action, goto.label, goto.value) == (
            StepAction.GOTO, "打开举报入口", "https://www.12377.cn",
        )
        assert (wait1.action, wait1.value) == (StepAction.WAIT, "1")
        assert (wait2.action, wait2.value) == (StepAction.WAIT, "2")
        assert (sel.action, sel.selector, sel.value, sel.label) == (
            StepAction.SELECT, form_models.SELECTORS["type"],
            payload.category, "选择信息类型",
        )
        assert (f_url.selector, f_url.value) == (
            form_models.SELECTORS["url"], payload.site_url,
        )
        assert (f_desc.selector, f_desc.value) == (
            form_models.SELECTORS["desc"], payload.description,
        )
        assert (f_name.selector, f_name.value) == (
            form_models.SELECTORS["name"], "张三",
        )
        assert (f_phone.selector, f_phone.value) == (
            form_models.SELECTORS["phone"], "13800000000",
        )
        assert (shot.action, shot.label) == (
            StepAction.SCREENSHOT, "填写完成后截图",
        )
        assert (click.action, click.selector, click.label) == (
            StepAction.CLICK, form_models.SELECTORS["submit"], "点击提交",
        )
        assert (shot2.action, shot2.label) == (
            StepAction.SCREENSHOT, "提交结果截图",
        )

    def test_name_phone_steps_marked_skippable_others_not(self):
        steps = self._steps(self._payload(reporter_name="张三", reporter_phone="13800000000"))
        for idx in (5, 6):  # name / phone
            assert steps[idx].meta.get("skippable") is True
        for idx, step in enumerate(steps):
            if idx not in (5, 6):
                assert not (step.meta.get("skippable") and step.value), f"步骤 {idx + 1} 有值但标记了 skippable"

    def test_empty_name_phone_still_generate_fill_steps(self):
        # V10.3:禁用 auto_profile 隔离本机永久模板(本测试专验"无信息→空值步骤仍在")
        cfg = Config()
        cfg.profile_path = "/nonexistent/isolated_profile.yaml"
        payload = form_models.build_payload(
            self._payload().__class__(**vars(self._payload())) if hasattr(self._payload(), "__dict__") else self._payload(),
            Portal.P12377, cfg, auto_profile=False, reason="测试",
        )
        assert payload.reporter_name == "" and payload.reporter_phone == ""
        steps = self._steps(payload)
        assert len(steps) == 18
        assert (steps[5].action, steps[5].selector, steps[5].value) == (
            StepAction.FILL, form_models.SELECTORS["name"], "",
        )
        assert (steps[6].action, steps[6].selector, steps[6].value) == (
            StepAction.FILL, form_models.SELECTORS["phone"], "",
        )
        assert steps[5].meta.get("skippable") is True
        assert steps[6].meta.get("skippable") is True

    def test_entry_url_defaults_per_portal(self):
        cfg = Config()
        assert form_models.build_plan(
            self._payload(), cfg
        ).entry_url == cfg.portal_12377_base == "https://www.12377.cn"
        shdf_payload = form_models.build_payload(make_entry(), Portal.SHDF, cfg)
        assert form_models.build_plan(
            shdf_payload, cfg
        ).entry_url == cfg.portal_shdf_base == "https://www.shdf.gov.cn"

    def test_entry_url_defaults_follow_custom_config(self):
        cfg = Config(
            portal_12377_base="http://127.0.0.1:8800/",
            portal_shdf_base="http://127.0.0.1:8801/",
        )
        assert form_models.build_plan(self._payload(), cfg).entry_url == "http://127.0.0.1:8800/"
        shdf_payload = form_models.build_payload(make_entry(), Portal.SHDF, cfg)
        assert form_models.build_plan(shdf_payload, cfg).entry_url == "http://127.0.0.1:8801/"

    def test_explicit_entry_url_overrides_default(self):
        payload = self._payload()
        plan = form_models.build_plan(
            payload, Config(), entry_url="http://127.0.0.1:9000/report"
        )
        assert plan.entry_url == "http://127.0.0.1:9000/report"
        assert plan.steps[0].action is StepAction.GOTO
        assert plan.steps[0].value == "http://127.0.0.1:9000/report"

    def test_plan_carries_portal_and_payload(self):
        payload = self._payload()
        plan = form_models.build_plan(payload, Config())
        assert plan.portal is Portal.P12377
        assert plan.payload is payload


# ---------------------------------------------------------------------------
# plan_from_entry
# ---------------------------------------------------------------------------

class TestPlanFromEntry:
    def test_combines_build_payload_and_build_plan(self):
        cfg = Config()
        plan = form_models.plan_from_entry(make_entry(), Portal.SHDF, cfg)
        assert isinstance(plan.payload, SubmissionPayload)
        assert plan.portal is Portal.SHDF
        assert plan.payload.portal == Portal.SHDF
        assert plan.entry_url == cfg.portal_shdf_base
        assert [s.action for s in plan.steps] == EXPECTED_ACTIONS

    def test_forwards_payload_kw_and_entry_url(self):
        plan = form_models.plan_from_entry(
            make_entry(),
            Portal.P12377,
            Config(),
            entry_url="http://127.0.0.1:7000/form",
            extra="补充线索。",
            reporter_name="李四",
            reporter_phone="13900000000",
        )
        assert plan.entry_url == "http://127.0.0.1:7000/form"
        assert "补充线索。" in plan.payload.description
        assert plan.payload.reporter_name == "李四"
        assert plan.payload.reporter_phone == "13900000000"
        name_step, phone_step = plan.steps[5], plan.steps[6]
        assert (name_step.selector, name_step.value) == (
            form_models.SELECTORS["name"], "李四",
        )
        assert (phone_step.selector, phone_step.value) == (
            form_models.SELECTORS["phone"], "13900000000",
        )


# ---------------------------------------------------------------------------
# V5 升级锁定:SELECTORS 冻结、单次格式化输出不变、计划无共享可变状态、遥测
# ---------------------------------------------------------------------------
class TestV5Upgrades:
    def test_v5_selectors_frozen_against_inplace_mutation(self):
        """冻结契约常量:全部原地变更途径(含 |= / pop / update)立即 TypeError。"""
        before = dict(form_models.SELECTORS)
        for mutate in (
            lambda: form_models.SELECTORS.__setitem__("url", "#tampered"),
            lambda: form_models.SELECTORS.__delitem__("url"),
            lambda: form_models.SELECTORS.update({"url": "#tampered"}),
            lambda: form_models.SELECTORS.pop("url"),
            lambda: form_models.SELECTORS.popitem(),
            lambda: form_models.SELECTORS.clear(),
            lambda: form_models.SELECTORS.setdefault("url", "#tampered"),
            lambda: form_models.SELECTORS.__ior__({"url": "#tampered"}),
        ):
            with pytest.raises(TypeError, match="SELECTORS"):
                mutate()
        assert dict(form_models.SELECTORS) == before

    def test_v5_selectors_operator_inplace_or_rejected(self):
        with pytest.raises(TypeError):
            form_models.SELECTORS |= {"url": "#tampered"}  # type: ignore[operator]

    def test_v5_selectors_keeps_dict_and_mapping_semantics(self):
        """读行为与普通 dict 完全等价(供 mock 贯通测试等既有消费方依赖)。"""
        import copy

        selectors = form_models.SELECTORS
        assert isinstance(selectors, dict)
        from collections.abc import Mapping
        assert isinstance(selectors, Mapping)
        assert selectors == dict(selectors)
        assert set(selectors) == set(dict(selectors))
        assert list(selectors.items()) == list(dict(selectors).items())
        assert selectors.get("captcha") == "#report-captcha"
        assert len(selectors) == 13
        assert copy.deepcopy(selectors) == dict(selectors)  # 经构造函数重建
        assert repr(dict(selectors)) == repr(dict(dict(selectors)))

    def test_v5_selectors_dict_copy_is_mutable_plain_dict(self):
        copy = dict(form_models.SELECTORS)
        assert type(copy) is dict
        copy["url"] = "#changed"  # 副本可改,原契约不受影响
        assert form_models.SELECTORS["url"] == "#report-url"

    def test_v5_description_single_format_output_unchanged(self):
        """模板折叠人工声明后正文逐字不变(V10 起首行为自动理由,正文单次 format)。"""
        entry = make_entry()
        payload = form_models.build_payload(entry, Portal.P12377, Config())
        _, _, body = payload.description.partition(chr(10))
        assert body == (
            "举报站点:http://bad.example.com/。"
            "经图像识别辅助系统初筛并经人工核实,该站点多张抽样页面图片含色情低俗内容"
            "(站点聚合最高分值 0.97,达标图片 3 张,抽样页面 3 个)。"
            "证据材料见附件 zip。"
            "以上信息已由举报人人工核实确认。"
        )
        with_extra = form_models.build_payload(
            entry, Portal.P12377, Config(), extra="补充:首页含弹窗广告。"
        )
        _, _, extra_body = with_extra.description.partition(chr(10))
        assert extra_body == (
            "举报站点:http://bad.example.com/。"
            "经图像识别辅助系统初筛并经人工核实,该站点多张抽样页面图片含色情低俗内容"
            "(站点聚合最高分值 0.97,达标图片 3 张,抽样页面 3 个)。"
            "证据材料见附件 zip。补充:首页含弹窗广告。"
            "以上信息已由举报人人工核实确认。"
        )

    def test_v5_build_plan_telemetry_counter(self):
        telemetry.reset()
        payload = form_models.build_payload(make_entry(), Portal.P12377, Config())
        form_models.build_plan(payload, Config())
        assert telemetry.snapshot()["counters"].get("form.plan_built") == 1.0

    def test_v5_plan_from_entry_counts_exactly_once(self):
        telemetry.reset()
        form_models.plan_from_entry(make_entry(), Portal.SHDF, Config())
        assert telemetry.snapshot()["counters"].get("form.plan_built") == 1.0

    def test_v5_plan_rejected_portal_not_counted(self):
        telemetry.reset()
        bogus = SubmissionPayload(
            portal=Portal.P12377,
            site_url="http://bad.example.com/",
        )
        bogus.portal = "not-a-portal"  # type: ignore[assignment]
        with pytest.raises(ValueError, match="未知举报门户"):
            form_models.build_plan(bogus, Config())
        assert "form.plan_built" not in telemetry.snapshot()["counters"]

    def test_v5_build_plan_no_shared_step_state_between_plans(self):
        """每次 build_plan 生成全新 Step 与 meta dict:改一份不影响另一份。"""
        payload = form_models.build_payload(make_entry(), Portal.P12377, Config())
        plan_a = form_models.build_plan(payload, Config())
        plan_b = form_models.build_plan(payload, Config())
        assert all(a is not b for a, b in zip(plan_a.steps, plan_b.steps))
        assert all(a.meta is not b.meta for a, b in zip(plan_a.steps, plan_b.steps))
        plan_a.steps[5].meta["skippable"] = False
        plan_a.steps[0].meta["tainted"] = True
        assert plan_b.steps[5].meta.get("skippable") is True
        assert "tainted" not in plan_b.steps[0].meta

    def test_v5_wait_step_values_still_contract_seconds(self):
        payload = form_models.build_payload(make_entry(), Portal.P12377, Config())
        steps = form_models.build_plan(payload, Config()).steps
        assert steps[1].value == "1"   # 页面加载等待
        assert steps[16].value == "2"  # 提交受理等待


# ---------------------------------------------------------------------------
# 反脆弱事件驱动等待:wait_until 条件词汇表与校验
# ---------------------------------------------------------------------------
class TestWaitUntil:
    """wait_until 词汇表(Step.meta["wait_until"])的校验与缺省兼容锁定。"""

    def test_wait_until_keys_exact_vocabulary(self):
        assert form_models.WAIT_UNTIL_KEYS == frozenset(
            {"selector_visible", "text_present", "requests_idle"}
        )
        assert isinstance(form_models.WAIT_UNTIL_KEYS, frozenset)

    def test_validate_all_three_conditions_normalized(self):
        normalized = form_models.validate_wait_until({
            "selector_visible": "#report-url",
            "text_present": "举报已受理",
            "requests_idle": "3",  # JSON 数字字符串容忍
        })
        assert normalized == {
            "selector_visible": "#report-url",
            "text_present": "举报已受理",
            "requests_idle": 3.0,
        }
        assert isinstance(normalized["requests_idle"], float)

    def test_validate_single_condition_subset(self):
        assert form_models.validate_wait_until({"requests_idle": 1.5}) == {
            "requests_idle": 1.5
        }
        assert form_models.validate_wait_until({"selector_visible": "#x"}) == {
            "selector_visible": "#x"
        }

    def test_validate_rejects_unknown_condition_key_in_chinese(self):
        with pytest.raises(ValueError, match="未知 wait_until 条件键.*bogus"):
            form_models.validate_wait_until({"bogus": 1, "requests_idle": 1})

    def test_validate_rejects_empty_dict_in_chinese(self):
        with pytest.raises(ValueError, match="至少需要一种条件"):
            form_models.validate_wait_until({})

    def test_validate_rejects_non_dict_in_chinese(self):
        with pytest.raises(ValueError, match="条件映射"):
            form_models.validate_wait_until(["selector_visible"])

    @pytest.mark.parametrize("bad", [
        {"selector_visible": 123},
        {"selector_visible": "   "},
        {"text_present": ""},
        {"text_present": 42},
        {"requests_idle": -1},
        {"requests_idle": 0},
        {"requests_idle": "abc"},
        {"requests_idle": True},  # bool 是 int 子类,显式拒绝
    ])
    def test_validate_rejects_bad_condition_values(self, bad):
        with pytest.raises(ValueError):
            form_models.validate_wait_until(bad)

    def test_step_wait_until_none_when_absent(self):
        step = Step(StepAction.WAIT, "等待页面加载", value="1")
        assert form_models.step_wait_until(step) is None
        step.meta["unrelated"] = True  # 其它 meta 键不影响
        assert form_models.step_wait_until(step) is None

    def test_step_wait_until_validates_and_normalizes(self):
        step = Step(
            StepAction.WAIT, "等待页面加载", value="1",
            meta={"wait_until": {"requests_idle": "2"}},
        )
        assert form_models.step_wait_until(step) == {"requests_idle": 2.0}

    def test_step_wait_until_invalid_raises_chinese(self):
        step = Step(StepAction.WAIT, "等待", meta={"wait_until": {"nope": 1}})
        with pytest.raises(ValueError, match="未知 wait_until 条件键"):
            form_models.step_wait_until(step)

    def test_build_plan_default_steps_never_declare_wait_until(self):
        """完全兼容锁定:缺省计划不携带任何 wait_until(旧计划 JSON 语义不变)。"""
        payload = form_models.build_payload(make_entry(), Portal.P12377, Config())
        steps = form_models.build_plan(payload, Config()).steps
        assert all("wait_until" not in s.meta for s in steps)
        wait_steps = [s for s in steps if s.action is StepAction.WAIT]
        assert [s.value for s in wait_steps] == ["1", "2"]
