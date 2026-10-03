"""扫黄打非门户(www.shdf.gov.cn)举报计划生成器。[A14]

职责:
- ``plan_shdf``:把复核队列中已人工批准的举报条目(entry_like,鸭子类型:
  site_url / verdict / evidence_zip)转成针对扫黄打非门户的声明式
  :class:`~netsentinel.contracts.SubmissionPlan`,交给 executor 干跑或人工门接续执行。

安全红线(违反即缺陷):
- 本模块只生成"计划",绝不发起任何网络请求;入口 URL 一律取
  ``cfg.portal_shdf_base`` 或调用方显式传入的 ``entry_url``,不硬编码直连真实门户。
- 验证码只能由人工门(HUMAN_GATE)处理:``_enforce_safety`` 会拒绝任何
  在人工门之外触碰验证码选择器的步骤,并要求计划必须包含人工门。
- 兄弟模块 ``netsentinel.submit.form_models``(A12 并行开发中)按契约惰性导入,
  未就位时抛中文 :class:`RuntimeError`。

用法示例::

    from netsentinel.contracts import Config
    from netsentinel.submit.portal_shdf import plan_shdf

    plan = plan_shdf(entry, Config(portal_shdf_base="http://127.0.0.1:8901/"))
    # plan.payload.category == "淫秽色情类";验证码只出现在 HUMAN_GATE 步骤

V5 升级:安全自检的选择器一律复用 ``form_models.SELECTORS`` 单一事实来源
(本模块不再重复定义兜底常量);成功生成计划接
``telemetry.inc("portal.plan.shdf")``。
"""
from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config, Portal, StepAction, SubmissionPlan

#: 门户展示名(中文,供计划首步 meta / playbook / 审计日志展示)
PORTAL_NAME = "全国'扫黄打非'工作小组办公室(扫黄打非网)"

#: 扫黄打非门户的举报分类文案(门户分类体系以"淫秽色情类"指代色情内容,
#: 调研记录见 docs/portal_shdf_notes.md;上线前须按门户实际文案人工核验)
SHDF_CATEGORY = "淫秽色情类"


def _import_form_models() -> Any:
    """惰性导入兄弟模块 form_models;未就位抛中文 RuntimeError。"""
    try:
        # importlib 路径便于测试注入替身;依赖 A12,绝不模块级导入
        return importlib.import_module("netsentinel.submit.form_models")
    except ImportError as exc:
        raise RuntimeError(
            "生成扫黄打非举报计划需要 netsentinel.submit.form_models 模块"
            "(A12 并行开发中,当前尚未就位),请等待该模块完成后再试。"
        ) from exc


def _selector_of(form_models: Any, key: str, fallback: str) -> str:
    """按契约 §3 从 form_models.SELECTORS 取选择器(单一事实来源,V5 去重)。

    Mapping 检查同时兼容 dict 与只读映射(SELECTORS 已冻结为不可变映射);
    仅当替身/旧版模块连 SELECTORS 都不可用时才回退到调用方给的字面量
    (篡改防御兜底,与 portal_defs 的做法一致)。
    """
    selectors = getattr(form_models, "SELECTORS", None)
    if isinstance(selectors, Mapping):
        value = selectors.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback


def _enforce_safety(plan: SubmissionPlan, form_models: Any) -> None:
    """红线自检,违反抛中文 RuntimeError。

    1. 计划必须包含 HUMAN_GATE 人工门(真实提交前必须人工确认)。
    2. 除 HUMAN_GATE 外,任何步骤不得触碰验证码选择器(绝不自动识别/填写验证码)。
    """
    if not any(step.action is StepAction.HUMAN_GATE for step in plan.steps):
        raise RuntimeError(
            "安全红线:扫黄打非举报计划缺少人工门(HUMAN_GATE)步骤,"
            "真实提交前必须有人工确认,该要求不可关闭。"
        )
    captcha_sel = _selector_of(form_models, "captcha", "#report-captcha")
    for step in plan.steps:
        # V10.1:FOCUS(聚焦验证码框)是安全的半自动动作——只聚焦不填写
        if (
            step.action is not StepAction.HUMAN_GATE
            and step.action is not StepAction.FOCUS
            and step.selector == captcha_sel
        ):
            raise RuntimeError(
                "安全红线:验证码只允许人工门(HUMAN_GATE)人工输入,"
                f"检测到自动步骤试图触碰验证码字段({captcha_sel})。"
            )


def plan_shdf(
    entry_like: Any,
    cfg: Config,
    entry_url: str | None = None,
    **payload_kw: Any,
) -> SubmissionPlan:
    """生成扫黄打非门户的举报填写计划。

    - ``entry_like``:复核队列条目(鸭子类型,至少含 site_url / verdict / evidence_zip)。
    - ``entry_url`` 缺省取 ``cfg.portal_shdf_base``(本地 mock 场景可显式传
      ``http://127.0.0.1:...``)。
    - ``payload_kw``:透传给 form_models(如 extra / reporter_name / reporter_phone)。
    - 分类固定为"淫秽色情类";计划首步 meta 标注 ``{"portal": PORTAL_NAME}``。

    本函数只做计划构造,不访问网络;真实填写与提交由 executor 在人工门之下执行。
    """
    form_models = _import_form_models()

    if entry_url is None:
        entry_url = cfg.portal_shdf_base

    plan = _compose_plan(form_models, entry_like, cfg, entry_url, payload_kw)

    # 双保险:即使下游实现疏忽,也保证 payload 与信息类型步骤的分类文案一致
    plan.payload.category = SHDF_CATEGORY
    type_sel = _selector_of(form_models, "type", "#report-type")
    for step in plan.steps:
        if step.action is StepAction.SELECT and step.selector == type_sel:
            step.value = SHDF_CATEGORY
    # 首步 meta 标注门户名,便于执行器 / playbook / 审计区分举报渠道
    if plan.steps:
        plan.steps[0].meta["portal"] = PORTAL_NAME

    _enforce_safety(plan, form_models)
    telemetry.inc("portal.plan.shdf")
    return plan


def _compose_plan(
    form_models: Any,
    entry_like: Any,
    cfg: Config,
    entry_url: str,
    payload_kw: dict[str, Any],
) -> SubmissionPlan:
    """按契约 §2 组合 build_payload → 固定分类 → build_plan。

    不走 ``plan_from_entry`` 便捷函数:其 ``**payload_kw`` 只透传给
    ``build_payload``(extra / reporter_name / reporter_phone),无法在
    payload 与步骤生成之间注入门户分类;若在拿到计划后才改
    ``payload.category``,SELECT 步骤的 value 仍是旧值,会造成计划内部不一致
    (已被 A16 mock 贯通测试抓到过一次)。故这里显式分步组合,
    在 ``build_plan`` 之前就把分类固定为"淫秽色情类"。
    """
    build_payload = getattr(form_models, "build_payload", None)
    build_plan = getattr(form_models, "build_plan", None)
    if not callable(build_payload) or not callable(build_plan):
        raise RuntimeError(
            "netsentinel.submit.form_models 缺少契约 §2 规定的 "
            "build_payload / build_plan 接口(A12 尚未完成),无法生成扫黄打非举报计划。"
        )
    payload = build_payload(entry_like, Portal.SHDF, cfg, **payload_kw)
    payload.category = SHDF_CATEGORY
    return build_plan(payload, cfg, entry_url=entry_url)
