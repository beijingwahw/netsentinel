"""netsentinel.submit.portal_12377 单元测试。[A13]

离线测试:不访问外网,举报入口一律使用 127.0.0.1 本地 mock 地址,
绝不出现指向真实门户的请求;form_models(A12)未就位时整体跳过
(契约 §5 并行开发容错)。
"""
from __future__ import annotations

import dataclasses
import inspect
import pathlib
import sys
import types
import zipfile

import pytest

pytest.importorskip("netsentinel.submit.form_models")

from netsentinel import telemetry  # noqa: E402
from netsentinel.contracts import Config, Portal, Step, StepAction  # noqa: E402
from netsentinel.submit import form_models  # noqa: E402
from netsentinel.submit import portal_12377 as portal_mod  # noqa: E402
from netsentinel.submit.portal_12377 import (  # noqa: E402
    CATEGORY,
    PORTAL_NAME,
    plan_12377,
)

#: 本地 mock 入口(离线测试专用,绝不指向真实门户)
MOCK_ENTRY_URL = "http://127.0.0.1:8900/mock/12377_report.html"


@dataclasses.dataclass
class FakeEntry:
    """契约要求的 entry_like 鸭子类型:site_url / verdict / evidence_zip。"""

    site_url: str = "https://suspect.example.test/"
    verdict: str = "nsfw"
    evidence_zip: str = ""
    agg_nsw_prob: float = 0.97

    @property
    def agg(self) -> float:
        """聚合分别名:兼容使用 agg 称呼 agg_nsw_prob 的调用方。"""
        return self.agg_nsw_prob


@pytest.fixture(name="evidence_zip")
def fixture_evidence_zip(tmp_path: pathlib.Path) -> str:
    """在 tmp_path 下生成真实 zip 证据包,满足 payload 校验要求。"""
    zip_path = tmp_path / "evidence.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifest.json", '{"site_url": "https://suspect.example.test/"}')
    return str(zip_path)


@pytest.fixture(name="cfg")
def fixture_cfg() -> Config:
    """入口指向本地 mock:证明 entry_url 只来自 cfg,而非硬编码。"""
    return dataclasses.replace(Config(), portal_12377_base=MOCK_ENTRY_URL)


def test_plan_uses_p12377_and_cfg_entry_url(evidence_zip: str, cfg: Config) -> None:
    plan = plan_12377(FakeEntry(evidence_zip=evidence_zip), cfg)
    assert plan.portal == Portal.P12377
    assert plan.entry_url == MOCK_ENTRY_URL  # 缺省入口必须来自 cfg
    assert plan.payload.portal == Portal.P12377
    assert plan.payload.site_url == "https://suspect.example.test/"
    assert plan.payload.evidence_zip == evidence_zip


def test_category_fixed_and_select_step_in_sync(evidence_zip: str, cfg: Config) -> None:
    """类目固定为"色情低俗信息",且 select 步骤的填写值必须与之一致。"""
    plan = plan_12377(FakeEntry(evidence_zip=evidence_zip), cfg)
    assert CATEGORY == "色情低俗信息"
    assert plan.payload.category == CATEGORY

    selectors = getattr(form_models, "SELECTORS", {})
    type_sel = selectors.get("type", "#report-type")
    select_steps = [
        step
        for step in plan.steps
        if step.action == StepAction.SELECT and step.selector == type_sel
    ]
    assert select_steps, "计划应包含'选择信息类型'的 select 步骤"
    assert all(
        step.value == CATEGORY for step in select_steps
    ), "select 步骤的 value 必须与固定类目一致,否则执行器会填入旧默认值"


def test_explicit_entry_url_overrides_default(evidence_zip: str, cfg: Config) -> None:
    other_url = "http://127.0.0.1:8900/mock/another_entry.html"
    plan = plan_12377(FakeEntry(evidence_zip=evidence_zip), cfg, entry_url=other_url)
    assert plan.entry_url == other_url


def test_plan_has_human_gate_and_no_captcha_autofill(
    evidence_zip: str, cfg: Config
) -> None:
    plan = plan_12377(FakeEntry(evidence_zip=evidence_zip), cfg)
    actions = [step.action for step in plan.steps]
    assert StepAction.HUMAN_GATE in actions, "真实提交前必须有人工门"

    selectors = getattr(form_models, "SELECTORS", {})
    captcha_sel = selectors.get("captcha", "#report-captcha")
    submit_sel = selectors.get("submit", "#report-submit")
    auto_actions = (StepAction.FILL, StepAction.SELECT, StepAction.CLICK)

    for step in plan.steps:
        if step.selector == captcha_sel:
            assert step.action not in auto_actions, "验证码只允许人工输入"

    gate_idx = actions.index(StepAction.HUMAN_GATE)
    submit_clicks = [
        i
        for i, step in enumerate(plan.steps)
        if step.selector == submit_sel and step.action == StepAction.CLICK
    ]
    assert all(gate_idx < i for i in submit_clicks), "人工门必须先于提交点击"


def test_first_step_meta_marks_portal(evidence_zip: str, cfg: Config) -> None:
    plan = plan_12377(FakeEntry(evidence_zip=evidence_zip), cfg)
    assert plan.steps, "计划不能是空步骤列表"
    first_meta = plan.steps[0].meta
    assert first_meta.get("portal") == PORTAL_NAME
    assert first_meta.get("note"), "首步 meta 应附带人工门说明"


def test_no_real_portal_url_hardcoded() -> None:
    source = inspect.getsource(portal_mod)
    assert "www.12377.cn" not in source, "入口 URL 只能来自 cfg/参数,不得硬编码"


def test_fixed_kwargs_rejected(evidence_zip: str, cfg: Config) -> None:
    entry = FakeEntry(evidence_zip=evidence_zip)
    with pytest.raises(TypeError, match="category"):
        plan_12377(entry, cfg, category="其他类型")
    with pytest.raises(TypeError, match="portal"):
        plan_12377(entry, cfg, portal=Portal.SHDF)


def test_invalid_entry_url_rejected(evidence_zip: str, cfg: Config) -> None:
    with pytest.raises(ValueError, match="entry_url"):
        plan_12377(FakeEntry(evidence_zip=evidence_zip), cfg, entry_url="not-a-url")


def test_reporter_kwargs_forwarded(evidence_zip: str, cfg: Config) -> None:
    plan = plan_12377(
        FakeEntry(evidence_zip=evidence_zip),
        cfg,
        reporter_name="测试举报人",
        reporter_phone="13800000000",
    )
    assert plan.payload.reporter_name == "测试举报人"
    assert plan.payload.reporter_phone == "13800000000"


def test_missing_form_models_raises_chinese_runtime_error(
    monkeypatch: pytest.MonkeyPatch, evidence_zip: str, cfg: Config
) -> None:
    """form_models 不可用时必须抛中文 RuntimeError,而不是裸 ImportError。"""
    import netsentinel.submit as submit_pkg

    monkeypatch.delattr(submit_pkg, "form_models", raising=False)
    monkeypatch.setitem(sys.modules, "netsentinel.submit.form_models", None)
    with pytest.raises(RuntimeError, match="form_models"):
        plan_12377(FakeEntry(evidence_zip=evidence_zip), cfg)


# ---------------------------------------------------------------------------
# V5 升级锁定:计划遥测、自检复用 form_models 常量(不重复定义兜底)
# ---------------------------------------------------------------------------
class TestV5Upgrades:
    def test_v5_plan_telemetry_counter(self, evidence_zip: str, cfg: Config) -> None:
        telemetry.reset()
        plan_12377(FakeEntry(evidence_zip=evidence_zip), cfg)
        assert telemetry.snapshot()["counters"].get("portal.plan.12377") == 1.0

    def test_v5_failed_plan_not_counted(self, evidence_zip: str, cfg: Config) -> None:
        telemetry.reset()
        with pytest.raises(ValueError, match="entry_url"):
            plan_12377(FakeEntry(evidence_zip=evidence_zip), cfg, entry_url="not-a-url")
        assert "portal.plan.12377" not in telemetry.snapshot()["counters"]

    def test_v5_safety_follows_form_models_selectors_dynamically(
        self, monkeypatch: pytest.MonkeyPatch, evidence_zip: str, cfg: Config
    ) -> None:
        """V5:自检与步骤构造复用 form_models.SELECTORS 单一事实来源——
        换掉契约常量后,人工门选择器随之变化,且本模块源码无本地硬编码。"""
        tampered = {**form_models.SELECTORS, "captcha": "#custom-cap"}
        monkeypatch.setattr(form_models, "SELECTORS", tampered)
        plan = plan_12377(FakeEntry(evidence_zip=evidence_zip), cfg)
        gates = [s for s in plan.steps if s.action is StepAction.HUMAN_GATE]
        assert len(gates) == 1
        assert gates[0].selector == "#custom-cap"
        assert "#custom-cap" not in inspect.getsource(portal_mod)

    def test_v5_custom_captcha_selector_autofill_rejected(
        self, monkeypatch: pytest.MonkeyPatch, evidence_zip: str, cfg: Config
    ) -> None:
        """红线跟随契约常量:替身 form_models 用自定义验证码选择器时,
        针对该选择器的自动填写步骤同样被拒(若本模块只认本地字面量则会漏检)。"""
        import netsentinel.submit as submit_pkg

        stub = types.ModuleType("netsentinel.submit.form_models")
        stub.SELECTORS = {**form_models.SELECTORS, "captcha": "#cap-x"}
        stub.build_payload = form_models.build_payload
        stub.build_plan = form_models.build_plan

        def _stub_plan_from_entry(entry_like, portal, cfg, entry_url=None, **kw):
            payload = form_models.build_payload(entry_like, portal, cfg, **kw)
            plan = form_models.build_plan(payload, cfg, entry_url=entry_url)
            plan.steps.insert(3, Step(
                action=StepAction.FILL,
                label="篡改:自动填自定义验证码",
                selector="#cap-x",
                value="0000",
            ))
            return plan

        stub.plan_from_entry = _stub_plan_from_entry
        monkeypatch.setitem(sys.modules, "netsentinel.submit.form_models", stub)
        monkeypatch.setattr(submit_pkg, "form_models", stub)

        telemetry.reset()
        with pytest.raises(RuntimeError, match="验证码"):
            plan_12377(FakeEntry(evidence_zip=evidence_zip), cfg)
        assert "portal.plan.12377" not in telemetry.snapshot()["counters"]
