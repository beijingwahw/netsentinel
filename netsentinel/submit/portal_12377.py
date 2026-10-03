"""12377 门户举报计划生成器(planner)。[A13]

职责:把人工确认后的举报条目(``entry_like`` 鸭子类型,至少含
``site_url`` / ``verdict`` / ``evidence_zip`` 属性,如 review_queue.Entry)
转换为面向中央网信办违法和不良信息举报中心(12377)的
:class:`SubmissionPlan` 声明式步骤计划。本模块只生成计划:
不发起任何网络请求、不驱动浏览器、不触碰真实表单。

安全红线(CONTRACTS §0,违反即缺陷):
1. 产品代码绝不硬编码真实门户地址:举报入口 URL 只来自
   ``cfg.portal_12377_base`` 或调用方显式传入的 ``entry_url``。
2. 验证码绝不自动识别/填写:除 ``HUMAN_GATE`` 外,计划中不允许出现
   任何指向验证码选择器的自动 fill/select/click 步骤;返回前防御性校验。
3. 计划必须包含 ``HUMAN_GATE``(人工核对信息、人工上传证据包 zip、
   人工输入验证码、人工点击提交);该校验无条件执行,不受配置影响。

用法示例::

    from netsentinel.contracts import Config
    from netsentinel.submit.portal_12377 import plan_12377

    plan = plan_12377(entry, Config(portal_12377_base="http://127.0.0.1:8900/"))
    # plan.portal == Portal.P12377;真实提交前的核对/验证码在 HUMAN_GATE 步骤

V5 升级:构造后自检的选择器一律复用 ``form_models.SELECTORS`` 单一事实
来源(本模块不再重复定义兜底常量);成功生成计划接
``telemetry.inc("portal.plan.12377")``。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config, Portal, StepAction, SubmissionPlan

__all__ = ["PORTAL_NAME", "CATEGORY", "plan_12377"]

#: 门户展示名(用于计划首步 meta 与日志展示,不是 URL)
PORTAL_NAME = "中央网信办违法和不良信息举报中心(12377)"

#: 12377 门户的举报类目(契约 §2 固定值,调用方不可覆盖)
CATEGORY = "色情低俗信息"

#: 计划首步 meta 中的说明键与文案。
#: SubmissionPlan 契约结构没有 meta 字段,门户级说明挂在首步 ``Step.meta``。
_NOTE_KEY = "note"
_NOTE_TEXT = (
    "本计划由 NetSentinel 辅助生成:自动化仅预填公开信息;证据包上传、"
    "验证码输入与最终提交必须由人工在 HUMAN_GATE 步骤完成。"
)


def _load_form_models() -> Any:
    """惰性加载兄弟模块 form_models(A12);未就位时抛中文 RuntimeError。"""
    try:
        from netsentinel.submit import form_models
    except Exception as exc:  # 模块缺失或兄弟模块内部错误,统一按不可用处理
        raise RuntimeError(
            f"12377 举报计划依赖 netsentinel.submit.form_models(A12),"
            f"当前不可用({exc});请先完成该模块后再生成举报计划。"
        ) from exc
    return form_models


def _selector_of(form_models: Any, key: str, default: str) -> str:
    """从已加载的 form_models 读取契约选择器(单一事实来源,V5 不再本地重复定义)。

    Mapping 检查同时兼容 dict 与只读映射(form_models.SELECTORS 已冻结);
    仅当替身/旧版模块连 SELECTORS 都不可用时,才回退到调用方给的字面量
    (篡改防御兜底,与 portal_defs 的做法一致)。
    """
    selectors = getattr(form_models, "SELECTORS", None)
    if isinstance(selectors, Mapping):
        value = selectors.get(key)
        if isinstance(value, str) and value:
            return value
    return default


def _force_category(plan: SubmissionPlan, form_models: Any) -> None:
    """把举报类目固定为 :data:`CATEGORY`,并同步 select 步骤的填写值。

    form_models 的默认类目是"色情低俗",而 12377 门户按契约 §2 固定为
    "色情低俗信息";必须同时改 payload 与"选择信息类型"步骤的 value,
    否则执行器会把旧值填进下拉框,造成 payload 与实际填写不一致。
    """
    plan.payload.category = CATEGORY
    type_selector = _selector_of(form_models, "type", "#report-type")
    for step in plan.steps:
        if step.selector == type_selector and step.action == StepAction.SELECT:
            step.value = CATEGORY


def _build_plan(
    form_models: Any,
    entry_like: Any,
    cfg: Config,
    entry_url: str,
    payload_kw: dict[str, Any],
) -> SubmissionPlan:
    """委托 form_models 生成计划:优先 plan_from_entry,否则用契约组合。"""
    plan_factory = getattr(form_models, "plan_from_entry", None)
    if plan_factory is not None:
        try:
            # 不向 plan_from_entry 传 category:A12 的实现把额外关键字透传给
            # build_payload(不接受 category);类目由本模块在返回后强制固定。
            plan = plan_factory(
                entry_like,
                portal=Portal.P12377,
                cfg=cfg,
                entry_url=entry_url,
                **payload_kw,
            )
            _force_category(plan, form_models)
            return plan
        except TypeError:
            # 兄弟模块 plan_from_entry 签名与本模块假设不一致时,
            # 回退到契约 §2 规定的 build_payload + build_plan 组合。
            pass
    payload = form_models.build_payload(
        entry_like, portal=Portal.P12377, cfg=cfg, **payload_kw
    )
    payload.category = CATEGORY  # 类目由本模块固定(契约 §2)
    return form_models.build_plan(payload, cfg, entry_url=entry_url)


def _validate_plan_safety(plan: SubmissionPlan, captcha_selector: str) -> None:
    """防御性校验安全红线:必须有人工门,且验证码绝不自动填写/点击。"""
    if not plan.steps:
        raise RuntimeError("安全红线:form_models 生成的计划没有任何步骤,拒绝返回")
    if not any(step.action == StepAction.HUMAN_GATE for step in plan.steps):
        raise RuntimeError(
            "安全红线:计划缺少 HUMAN_GATE 人工门步骤(人工核对与验证码输入),拒绝返回"
        )
    auto_actions = (StepAction.FILL, StepAction.SELECT, StepAction.CLICK)
    for step in plan.steps:
        if step.selector and step.selector == captcha_selector:
            if step.action in auto_actions:
                raise RuntimeError(
                    "安全红线:计划包含针对验证码选择器的自动填写/点击步骤,拒绝返回"
                )


def plan_12377(
    entry_like: Any,
    cfg: Config,
    entry_url: str | None = None,
    **payload_kw: Any,
) -> SubmissionPlan:
    """生成 12377 门户的举报步骤计划。

    参数:
        entry_like:举报条目(鸭子类型),至少含 ``site_url`` / ``verdict`` /
            ``evidence_zip`` 属性(如 ``review_queue.Entry``)。
        cfg:全局配置;``entry_url`` 缺省时取 ``cfg.portal_12377_base``。
        entry_url:举报入口完整 URL;只允许来自配置或调用方显式传参,
            本模块绝不硬编码真实门户地址。
        payload_kw:转发给 ``form_models.build_payload`` 的附加参数
            (如 ``extra`` / ``reporter_name`` / ``reporter_phone``)。

    返回:
        SubmissionPlan:``portal`` 固定为 ``Portal.P12377``,类目固定为
        :data:`CATEGORY`;首步 ``meta`` 带 ``{"portal": PORTAL_NAME}`` 标记
        与人工门说明文字。

    异常:
        RuntimeError:form_models(A12)未就位,或生成的计划违反安全红线。
        TypeError:试图通过关键字参数覆盖固定值(portal/category)。
        ValueError:入口 URL 不是 http(s):// 开头的完整链接。
    """
    for fixed_name in ("portal", "category"):
        if fixed_name in payload_kw:
            raise TypeError(
                f"plan_12377 不接受关键字参数 {fixed_name!r}:"
                "门户与举报类目由本模块按契约固定,不可覆盖"
            )

    resolved_url = (entry_url or cfg.portal_12377_base or "").strip()
    if not resolved_url.lower().startswith(("http://", "https://")):
        raise ValueError(
            "举报入口 entry_url 必须是 http(s):// 开头的完整 URL"
            "(缺省取 cfg.portal_12377_base)"
        )

    form_models = _load_form_models()
    plan = _build_plan(form_models, entry_like, cfg, resolved_url, payload_kw)

    if getattr(plan, "portal", None) != Portal.P12377:
        raise RuntimeError("form_models 返回的计划 portal 不是 P12377,拒绝返回")
    if (getattr(plan, "entry_url", "") or "").strip() != resolved_url:
        raise RuntimeError(
            "form_models 返回的计划 entry_url 与传入的举报入口不一致,拒绝返回"
        )
    plan.payload.category = CATEGORY  # 双保险:类目最终以本模块固定值为准

    _validate_plan_safety(
        plan, _selector_of(form_models, "captcha", "#report-captcha")
    )

    first_step = plan.steps[0]
    first_step.meta["portal"] = PORTAL_NAME
    first_step.meta.setdefault(_NOTE_KEY, _NOTE_TEXT)
    telemetry.inc("portal.plan.12377")
    return plan
