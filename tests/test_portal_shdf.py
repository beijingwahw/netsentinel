"""A14:扫黄打非门户举报计划生成器(portal_shdf)测试。

- 集成部分依赖兄弟模块 ``netsentinel.submit.form_models``(A12 并行开发中),
  按 CONTRACTS §5 用 ``pytest.importorskip`` 容错;未就位时整体 skip。
- 单元部分(StubPlanner 系列)注入一个严格遵循契约 §2/§3 的 form_models 替身,
  只验证 portal_shdf 自身行为:分类固定、入口 URL 缺省、首步 meta 门户名、
  红线自检(人工门缺失 / 自动填验证码 / 模块未就位)。

全部用例离线,绝不访问 www.shdf.gov.cn(仅出现 URL 字符串,不发起请求)。
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

# A12 并行开发中:模块未就位时,本文件按团队约定整体 skip
form_models = pytest.importorskip("netsentinel.submit.form_models")

from netsentinel import telemetry  # noqa: E402
from netsentinel.contracts import (  # noqa: E402
    Config,
    Portal,
    Step,
    StepAction,
    SubmissionPayload,
    SubmissionPlan,
    Verdict,
)
from netsentinel.submit.portal_shdf import (  # noqa: E402
    PORTAL_NAME,
    SHDF_CATEGORY,
    plan_shdf,
)

#: 契约 §3 的验证码选择器(form_models.SELECTORS 与本地 mock 共用)
try:
    CAPTCHA_SELECTOR = form_models.SELECTORS["captcha"]
except (AttributeError, KeyError):  # 防御:SELECTORS 尚未按契约提供
    CAPTCHA_SELECTOR = "#report-captcha"


# ---------------------------------------------------------------------------
# 测试夹具:鸭子类型举报条目(entry_like)
# ---------------------------------------------------------------------------

def make_entry() -> SimpleNamespace:
    """模拟复核队列中已人工批准(approved)的条目(entry_like 鸭子类型)。"""
    return SimpleNamespace(
        id=1,
        site_url="https://nsfw-example.test/entry",
        verdict=Verdict.NSFW,
        status="approved",
        evidence_zip="data/evidence/nsfw-example.test_demo.zip",
    )


@pytest.fixture()
def entry() -> SimpleNamespace:
    return make_entry()


@pytest.fixture()
def cfg() -> Config:
    return Config()


# ---------------------------------------------------------------------------
# 集成测试:plan_shdf 构造断言(依赖真实 form_models)
# ---------------------------------------------------------------------------

class TestPlanShdfIntegration:
    def test_portal_is_shdf_and_default_entry_url(self, entry: SimpleNamespace, cfg: Config) -> None:
        plan = plan_shdf(entry, cfg)
        assert plan.portal is Portal.SHDF
        # entry_url 缺省取 cfg.portal_shdf_base(仅字符串,不发请求)
        assert plan.entry_url == cfg.portal_shdf_base

    def test_entry_url_explicit_override(self, entry: SimpleNamespace, cfg: Config) -> None:
        local_mock = "http://127.0.0.1:8901/shdf_mock.html"
        plan = plan_shdf(entry, cfg, entry_url=local_mock)
        assert plan.entry_url == local_mock

    def test_category_fixed(self, entry: SimpleNamespace, cfg: Config) -> None:
        plan = plan_shdf(entry, cfg)
        assert plan.payload.category == SHDF_CATEGORY == "淫秽色情类"

    def test_contains_human_gate(self, entry: SimpleNamespace, cfg: Config) -> None:
        plan = plan_shdf(entry, cfg)
        assert any(step.action is StepAction.HUMAN_GATE for step in plan.steps)

    def test_no_captcha_autofill(self, entry: SimpleNamespace, cfg: Config) -> None:
        """红线:验证码只允许人工门处理,任何自动步骤不得触碰验证码选择器。"""
        plan = plan_shdf(entry, cfg)
        assert plan.steps, "计划不应为空"
        offenders = [
            step
            for step in plan.steps
            if step.selector == CAPTCHA_SELECTOR and step.action not in (StepAction.HUMAN_GATE, StepAction.FOCUS)
        ]
        assert offenders == []

    def test_first_step_meta_has_portal_name(self, entry: SimpleNamespace, cfg: Config) -> None:
        plan = plan_shdf(entry, cfg)
        assert plan.steps, "计划不应为空"
        assert plan.steps[0].meta.get("portal") == PORTAL_NAME
        assert "扫黄打非" in PORTAL_NAME

    def test_first_step_is_goto(self, entry: SimpleNamespace, cfg: Config) -> None:
        """契约 §3:首步 goto(entry_url),meta 门户名应落在这一步上。"""
        plan = plan_shdf(entry, cfg)
        assert plan.steps[0].action is StepAction.GOTO
        assert plan.steps[0].value == cfg.portal_shdf_base

    def test_select_step_value_matches_fixed_category(self, entry: SimpleNamespace, cfg: Config) -> None:
        """信息类型下拉步骤的 value 必须就是固定分类(防止 payload 与步骤不一致)。"""
        try:
            type_selector = form_models.SELECTORS["type"]
        except (AttributeError, KeyError):  # 防御:SELECTORS 尚未按契约提供
            type_selector = "#report-type"
        plan = plan_shdf(entry, cfg)
        select_steps = [
            step for step in plan.steps
            if step.action is StepAction.SELECT and step.selector == type_selector
        ]
        assert select_steps, "计划应包含针对信息类型下拉的 select 步骤"
        for step in select_steps:
            assert step.value == "淫秽色情类"
        assert plan.payload.category == "淫秽色情类"


# ---------------------------------------------------------------------------
# 单元测试:注入契约一致的 form_models 替身,只测 portal_shdf 自身逻辑
# ---------------------------------------------------------------------------

STUB_SELECTORS = {
    "url": "#report-url",
    "type": "#report-type",
    "desc": "#report-desc",
    "name": "#report-name",
    "phone": "#report-phone",
    "file": "#report-file",
    "captcha": "#report-captcha",
    "submit": "#report-submit",
}


def _stub_build_payload(entry_like, portal, cfg, extra="", reporter_name="", reporter_phone=""):
    """契约 §2 的最小实现:中文模板描述并声明辅助初筛 + 人工核实。"""
    return SubmissionPayload(
        portal=portal,
        site_url=entry_like.site_url,
        category="色情低俗",
        description=(
            f"该站点经图像识别辅助系统初筛判定为疑似色情内容(判定:{entry_like.verdict}),"
            "已由人工核实确认。请有关部门依法核查处置。"
        ) + extra,
        evidence_zip=getattr(entry_like, "evidence_zip", ""),
        reporter_name=reporter_name,
        reporter_phone=reporter_phone,
    )


def _stub_build_plan(payload, cfg, entry_url=None, *, include_human_gate=True, autofill_captcha=False):
    """契约 §3 步骤序列的最小实现;可按需破坏红线以测试自检。"""
    steps = [
        Step(action=StepAction.GOTO, label="打开举报入口", value=entry_url or payload.site_url),
        Step(action=StepAction.WAIT, label="等待页面加载", value="1"),
        Step(action=StepAction.SELECT, label="选择信息类型", selector=STUB_SELECTORS["type"], value=payload.category),
        Step(action=StepAction.FILL, label="填写举报链接", selector=STUB_SELECTORS["url"], value=payload.site_url),
        Step(action=StepAction.FILL, label="填写具体描述", selector=STUB_SELECTORS["desc"], value=payload.description),
        Step(action=StepAction.FILL, label="填写举报人姓名", selector=STUB_SELECTORS["name"], value=payload.reporter_name),
        Step(action=StepAction.FILL, label="填写举报人电话", selector=STUB_SELECTORS["phone"], value=payload.reporter_phone),
        Step(action=StepAction.SCREENSHOT, label="截图:填写完成"),
    ]
    if autofill_captcha:
        steps.append(
            Step(action=StepAction.FILL, label="违规:自动填验证码", selector=STUB_SELECTORS["captcha"], value="1234")
        )
    if include_human_gate:
        steps.append(
            Step(action=StepAction.HUMAN_GATE, label="人工核对信息、上传证据包 zip 并输入验证码")
        )
    steps.append(Step(action=StepAction.CLICK, label="点击提交", selector=STUB_SELECTORS["submit"]))
    return SubmissionPlan(portal=payload.portal, entry_url=entry_url or "", payload=payload, steps=steps)


def _install_stub(monkeypatch: pytest.MonkeyPatch, *, include_human_gate: bool = True,
                  autofill_captcha: bool = False, with_build_interfaces: bool = True) -> types.ModuleType:
    """构造并注入契约一致的 form_models 替身到 sys.modules。"""
    stub = types.ModuleType("netsentinel.submit.form_models")
    stub.SELECTORS = dict(STUB_SELECTORS)

    if with_build_interfaces:
        def build_payload(entry_like, portal, cfg, extra="", reporter_name="", reporter_phone=""):
            return _stub_build_payload(entry_like, portal, cfg, extra, reporter_name, reporter_phone)

        def build_plan(payload, cfg, entry_url=None):
            return _stub_build_plan(
                payload, cfg, entry_url,
                include_human_gate=include_human_gate,
                autofill_captcha=autofill_captcha,
            )

        stub.build_payload = build_payload
        stub.build_plan = build_plan

    monkeypatch.setitem(sys.modules, "netsentinel.submit.form_models", stub)
    return stub


class TestPlanShdfWithStub:
    def test_composition_fixes_category_before_build_plan(self, monkeypatch, entry, cfg) -> None:
        """在 build_plan 之前固定分类:SELECT 步骤值与 payload.category 都是"淫秽色情类"。

        回归背景:曾因在拿到计划后才改 payload.category,导致 SELECT 步骤仍携带
        替身默认"色情低俗",被 A16 的 mock 贯通测试抓到。
        """
        _install_stub(monkeypatch)
        plan = plan_shdf(entry, cfg, extra="补充:含大量露骨图片。", reporter_name="张三")
        assert plan.portal is Portal.SHDF
        assert plan.entry_url == cfg.portal_shdf_base
        assert plan.payload.category == "淫秽色情类"  # 覆盖替身默认"色情低俗"
        type_steps = [
            step for step in plan.steps
            if step.action is StepAction.SELECT and step.selector == STUB_SELECTORS["type"]
        ]
        assert type_steps and all(step.value == "淫秽色情类" for step in type_steps)
        assert plan.payload.reporter_name == "张三"
        assert "补充:含大量露骨图片。" in plan.payload.description
        assert plan.steps[0].meta.get("portal") == PORTAL_NAME

    def test_build_interfaces_missing_rejected(self, monkeypatch, entry, cfg) -> None:
        """替身缺失契约 §2 的 build_payload/build_plan 时抛中文 RuntimeError。"""
        _install_stub(monkeypatch, with_build_interfaces=False)
        with pytest.raises(RuntimeError, match="build_payload"):
            plan_shdf(entry, cfg)

    def test_form_models_not_ready_raises_chinese_runtimeerror(self, monkeypatch, entry, cfg) -> None:
        """模块未就位(sys.modules 置 None)时抛中文 RuntimeError。"""
        monkeypatch.setitem(sys.modules, "netsentinel.submit.form_models", None)
        with pytest.raises(RuntimeError, match="form_models"):
            plan_shdf(entry, cfg)

    def test_missing_human_gate_rejected(self, monkeypatch, entry, cfg) -> None:
        """红线:计划缺少 HUMAN_GATE 时拒绝生成。"""
        _install_stub(monkeypatch, include_human_gate=False)
        with pytest.raises(RuntimeError, match="人工门"):
            plan_shdf(entry, cfg)

    def test_captcha_autofill_rejected(self, monkeypatch, entry, cfg) -> None:
        """红线:任何自动填写验证码的步骤都拒绝。"""
        _install_stub(monkeypatch, autofill_captcha=True)
        with pytest.raises(RuntimeError, match="验证码"):
            plan_shdf(entry, cfg)


# ---------------------------------------------------------------------------
# V5 升级锁定:计划遥测、自检复用 form_models 常量(不重复定义兜底)
# ---------------------------------------------------------------------------
class TestV5Upgrades:
    def test_v5_plan_telemetry_counter(self, entry, cfg) -> None:
        telemetry.reset()
        plan_shdf(entry, cfg)
        assert telemetry.snapshot()["counters"].get("portal.plan.shdf") == 1.0

    def test_v5_failed_plan_not_counted(self, monkeypatch, entry, cfg) -> None:
        telemetry.reset()
        _install_stub(monkeypatch, include_human_gate=False)
        with pytest.raises(RuntimeError, match="人工门"):
            plan_shdf(entry, cfg)
        assert "portal.plan.shdf" not in telemetry.snapshot()["counters"]

    def test_v5_custom_captcha_selector_autofill_rejected(self, monkeypatch, entry, cfg) -> None:
        """红线跟随契约常量:替身 form_models 用自定义验证码选择器(#cap-x,
        而非契约字面量)时,针对它的自动填写步骤同样被拒——证明 _enforce_safety
        动态复用 form_models.SELECTORS,而非本地重复定义的字面量。"""
        stub = types.ModuleType("netsentinel.submit.form_models")
        stub.SELECTORS = {**STUB_SELECTORS, "captcha": "#cap-x"}
        stub.build_payload = _stub_build_payload

        def _build_plan_with_custom_autofill(payload, cfg, entry_url=None):
            plan = _stub_build_plan(payload, cfg, entry_url)
            plan.steps.append(
                Step(action=StepAction.FILL, label="违规:自动填自定义验证码",
                     selector="#cap-x", value="1234")
            )
            return plan

        stub.build_plan = _build_plan_with_custom_autofill
        monkeypatch.setitem(sys.modules, "netsentinel.submit.form_models", stub)

        telemetry.reset()
        with pytest.raises(RuntimeError, match="验证码"):
            plan_shdf(entry, cfg)
        assert "portal.plan.shdf" not in telemetry.snapshot()["counters"]
