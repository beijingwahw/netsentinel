"""举报表单数据模型:选择器契约、举报信息构造与声明式步骤计划。

模块内容(A12,后续 A13/A14 门户 planner、A15 执行器、A17 playbook 依赖):
- ``SELECTORS``:举报表单 CSS 选择器契约(CONTRACTS.md §3,与本地 mock 表单、
  两个门户 planner、执行器共同遵守,勿改)。
- ``DEFAULT_CATEGORY``:默认举报信息类型。
- ``build_payload``:从复核队列条目(entry_like 鸭子类型)构造 SubmissionPayload。
- ``build_plan``:按 §5 固定顺序生成声明式步骤计划 SubmissionPlan。
- ``plan_from_entry``:build_payload + build_plan 组合的便捷函数。

安全红线(契约 §0):验证码只允许人工输入 —— 本模块绝不生成任何自动填写
验证码的步骤;真实提交前的人工核对统一通过 HUMAN_GATE 步骤表达,顺序固定。

用法示例::

    from netsentinel.contracts import Config, Portal
    from netsentinel.submit import form_models

    entry = review_queue.Entry(...)            # 任何含 site_url/verdict/... 的对象
    payload = form_models.build_payload(entry, Portal.P12377, Config())
    plan = form_models.build_plan(payload, Config())
    errors = payload.validate()                # 中文错误列表,空列表 = 可提交

V5 升级:SELECTORS 冻结为不可变映射(写操作立即 TypeError);
description 模板单次 format;build_plan 12 步列表字面量一次成型;
关键入口接 ``netsentinel.telemetry``(``form.plan_built``)。

反脆弱等待升级:WAIT 步可通过 ``Step.meta["wait_until"]`` 声明事件驱动条件
(:data:`WAIT_UNTIL_KEYS` + :func:`validate_wait_until` / :func:`step_wait_until`),
缺省不声明时保持固定盲睡语义,旧计划 JSON 完全兼容。
"""
from __future__ import annotations

from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import (
    Config,
    Portal,
    Step,
    StepAction,
    SubmissionPayload,
    SubmissionPlan,
    Verdict,
)

__all__ = [
    "SELECTORS",
    "DEFAULT_CATEGORY",
    "WAIT_UNTIL_KEYS",
    "validate_wait_until",
    "step_wait_until",
    "build_payload",
    "build_plan",
    "plan_from_entry",
]


# ---------------------------------------------------------------------------
# §3 举报表单选择器契约(与 CONTRACTS.md 完全一致,勿改)
# ---------------------------------------------------------------------------
class _FrozenSelectors(dict):
    """冻结的选择器映射:内容随契约固定,任何原地修改立即 :class:`TypeError`。

    V5 说明:不用 ``types.MappingProxyType`` 是因为它不是 ``dict`` 的实例,
    会破坏既有用例对 ``SELECTORS`` 的 ``isinstance(x, dict)`` 断言与各处
    dict 语义用法;改用封禁全部变更方法的 dict 子类,达到同样的冻结效果
    (读行为与普通 dict 完全等价,写操作全部拒绝)。需要可改副本时用
    ``dict(SELECTORS)``(返回普通 dict)。
    """

    __slots__ = ()

    def _frozen(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError(
            "SELECTORS 是冻结的契约常量(CONTRACTS.md §3),禁止原地修改;"
            "如需定制请先 dict(SELECTORS) 复制再修改副本"
        )

    __setitem__ = _frozen      # d[k] = v
    __delitem__ = _frozen      # del d[k]
    __ior__ = _frozen          # d |= other
    clear = _frozen
    pop = _frozen
    popitem = _frozen
    setdefault = _frozen
    update = _frozen
    del _frozen

    def __reduce__(self) -> tuple[Any, ...]:
        """copy/deepcopy/pickle 经构造函数重建,绕开被封禁的 __setitem__。"""
        return (self.__class__, (dict(self),))


#: 举报表单 CSS 选择器契约(只读;值与 CONTRACTS.md §3 逐字一致,勿改)。
SELECTORS: dict[str, str] = _FrozenSelectors({
    "url": "#report-url",          # 举报链接
    "type": "#report-type",        # 信息类型 select
    "desc": "#report-desc",        # 具体描述 textarea
    "name": "#report-name",        # 举报人姓名
    "phone": "#report-phone",      # 举报人电话
    "email": "#report-email",      # 电子邮箱(V10.2)
    "id": "#report-idcard",        # 身份证号(V10.2)
    "address": "#report-address",  # 通讯地址(V10.2)
    "postcode": "#report-postcode",# 邮政编码(V10.2)
    "org": "#report-org",          # 单位名称(V10.2)
    "file": "#report-file",        # 附件(证据包 zip)
    "captcha": "#report-captcha",  # 验证码 —— 只允许人工输入
    "submit": "#report-submit",    # 提交按钮
})

#: 默认举报信息类型(A13/A14 门户 planner 会按门户覆盖为各自的取值)。
DEFAULT_CATEGORY: str = "色情低俗"

#: 描述末尾的人工核实声明(红线:举报前必须经人工确认)。
_HUMAN_CONFIRM_CLAIM: str = "以上信息已由举报人人工核实确认。"

#: 举报描述中文模板;声明"辅助系统初筛 + 人工核实"。
#: V5:人工核实声明在模块加载期折叠进模板尾部,build_payload 运行期
#: 只做一次 str.format、零次字符串拼接(输出与逐字一致)。
_DESCRIPTION_TEMPLATE: str = (
    "举报站点:{site_url}。"
    "经图像识别辅助系统初筛并经人工核实,该站点多张抽样页面图片含色情低俗内容"
    "(站点聚合最高分值 {agg:.2f},达标图片 {nsw_count} 张,抽样页面 {pages} 个)。"
    "证据材料见附件 zip。{extra}" + _HUMAN_CONFIRM_CLAIM
)

#: §5 步骤序列中的等待秒数(魔法数字提为常量;值为契约固定,勿改)。
_PAGE_LOAD_WAIT_S: str = "1"   # 打开举报入口后等待页面加载
_SUBMIT_SETTLE_WAIT_S: str = "2"  # 点击提交后等待受理结果


# ---------------------------------------------------------------------------
# WAIT 步事件驱动等待条件(反脆弱等待;缺省不启用,旧计划 JSON 语义完全不变)
# ---------------------------------------------------------------------------
#: ``Step.meta["wait_until"]`` 允许的条件键(声明式等待词汇表)。
#: - ``selector_visible``: 值为 CSS 选择器,等待该元素可见;
#: - ``text_present``: 值为文本片段,轮询 page.content() 等待文本出现;
#: - ``requests_idle``: 值为超时秒数,等待网络空闲(networkidle)。
#: 多条件可组合,任一满足即通过(Any-of)。
WAIT_UNTIL_KEYS: frozenset[str] = frozenset(
    {"selector_visible", "text_present", "requests_idle"}
)


def validate_wait_until(wait_until: Any) -> dict[str, Any]:
    """校验并归一化 WAIT 步的 ``wait_until`` 条件映射。

    输入为 ``Step.meta["wait_until"]`` 的原始值(计划 JSON 反序列化产物)。
    合法输入:非空 dict,键全部属于 :data:`WAIT_UNTIL_KEYS`,其中
    ``selector_visible`` / ``text_present`` 必须是非空字符串,
    ``requests_idle`` 必须是可转正数的数值(或数字字符串,容忍 JSON 里
    写成 ``"3"``)。布尔值显式拒绝(``True`` 是 int 子类,易藏 bug)。

    :return: 归一化后的新 dict(只含声明过的键,按 selector_visible →
        text_present → requests_idle 规范顺序;requests_idle 归一为 float 秒)。
    :raises ValueError: 中文错误信息,指出第一个不合法之处(计划作者可读)。

    缺省不校验 None —— "未声明 wait_until" 是合法的旧语义(执行器保持固定
    盲睡);本函数只负责"声明了就必须声明得对"。
    """
    if not isinstance(wait_until, dict):
        raise ValueError(
            f"wait_until 必须是条件映射(dict),收到 {type(wait_until).__name__};"
            f"合法条件键:{sorted(WAIT_UNTIL_KEYS)}"
        )
    if not wait_until:
        raise ValueError(
            "wait_until 声明后至少需要一种条件(selector_visible / "
            "text_present / requests_idle);无需条件请整个删除 wait_until 键"
        )
    unknown = sorted(k for k in wait_until if k not in WAIT_UNTIL_KEYS)
    if unknown:
        raise ValueError(
            f"未知 wait_until 条件键:{unknown};"
            f"合法条件键:{sorted(WAIT_UNTIL_KEYS)}"
        )

    normalized: dict[str, Any] = {}

    if "selector_visible" in wait_until:
        selector = wait_until["selector_visible"]
        if not isinstance(selector, str) or not selector.strip():
            raise ValueError(
                "wait_until 条件 selector_visible 必须是非空 CSS 选择器字符串"
            )
        normalized["selector_visible"] = selector

    if "text_present" in wait_until:
        text = wait_until["text_present"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError(
                "wait_until 条件 text_present 必须是非空文本字符串"
            )
        normalized["text_present"] = text

    if "requests_idle" in wait_until:
        idle_raw = wait_until["requests_idle"]
        if isinstance(idle_raw, bool) or not isinstance(idle_raw, (int, float, str)):
            raise ValueError(
                "wait_until 条件 requests_idle 必须是正数秒(int/float/数字字符串)"
            )
        try:
            idle_s = float(idle_raw)
        except (TypeError, ValueError):
            raise ValueError(
                "wait_until 条件 requests_idle 必须是正数秒(int/float/数字字符串)"
            ) from None
        if not idle_s > 0:
            raise ValueError(
                "wait_until 条件 requests_idle 必须是正数秒(> 0)"
            )
        normalized["requests_idle"] = idle_s

    return normalized


def step_wait_until(step: Step) -> dict[str, Any] | None:
    """从 Step 提取并校验 wait_until 条件;未声明返回 None(旧盲睡语义)。

    执行器入口:声明了 wait_until 但内容非法时立即抛中文 :class:`ValueError`
    (由执行器的步骤级异常捕获记录为错误并中止,绝不静默降级 —— 错误的等待
    计划应当在审计里暴露,而不是悄悄退回盲睡)。
    """
    raw = step.meta.get("wait_until") if isinstance(step.meta, dict) else None
    if raw is None:
        return None
    return validate_wait_until(raw)


# ---------------------------------------------------------------------------
# 鸭子类型容错辅助
# ---------------------------------------------------------------------------
def _first_attr(obj: Any, *names: str, default: Any = None) -> Any:
    """按优先级返回 obj 上第一个存在且非 None 的属性(兼容别名属性名)。"""
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def _to_float(value: Any, default: float = 0.0) -> float:
    """容错转 float;失败或缺省返回 default。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    """容错转 int;失败或缺省返回 default。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _page_count(value: Any) -> int:
    """pages 统计值容错:兼容 list[PageSample] / int / 缺省。"""
    if isinstance(value, (list, tuple, set)):
        return len(value)
    return _to_int(value)


def _verdict_text(value: Any) -> str:
    """verdict 容错:兼容 Verdict 枚举与普通字符串,统一为字符串。"""
    if isinstance(value, Verdict):
        return value.value
    return str(value) if value else ""


# ---------------------------------------------------------------------------
# payload 构造
# ---------------------------------------------------------------------------
def build_payload(
    entry_like: Any,
    portal: Portal,
    cfg: Config,
    extra: str = "",
    reporter_name: str = "",
    reporter_phone: str = "",
    *,
    reason: str | None = None,
    auto_profile: bool = True,
) -> SubmissionPayload:
    """从复核队列条目构造一次举报要填写的全部信息。

    entry_like 为鸭子类型(典型是 decision.review_queue.Entry 或 SiteReport),
    各属性均容错缺失:
    - ``site_url``:举报站点链接(必填语义,缺失则为空串,由 validate 拦截);
    - ``verdict``:Verdict 枚举或字符串均可(payload 无对应字段,仅容错读取);
    - ``evidence_zip`` / ``evidence_zip_path``:证据包 zip 路径,兼容两种属性名;
    - ``agg_nsw_prob`` / ``nsw_image_count`` / ``pages``:描述模板统计值,
      缺省 0 / 0 / 0。

    evidence_zip 为空时同样构造 payload,不在此处抛错,由 validate() 拦截。
    cfg 参数保留以符合契约签名,便于后续扩展。
    """
    site_url = str(_first_attr(entry_like, "site_url", default="") or "")
    # verdict 归一化仅用于验证枚举/字符串两种形态均可安全读取(鸭子类型容错)。
    _verdict = _verdict_text(_first_attr(entry_like, "verdict", default=""))
    evidence_zip = str(
        _first_attr(entry_like, "evidence_zip", "evidence_zip_path", default="") or ""
    )
    agg = _to_float(_first_attr(entry_like, "agg_nsw_prob", default=0.0))
    nsw_count = _to_int(_first_attr(entry_like, "nsw_image_count", default=0))
    pages = _page_count(_first_attr(entry_like, "pages", default=0))

    # V10:举报理由自动生成(≤39 字,红线 39:只基于已核实事实字段,失败回退模板);
    # 作为描述首行自动填入表单,同时单独存 payload.reason 供专用字段使用。
    if reason is None:
        try:
            from netsentinel.submit.reason_gen import generate_reason

            reason = generate_reason(entry_like, cfg, use_glm=False)
        except Exception:  # noqa: BLE001 - 理由生成绝不阻断举报
            reason = ""
    reason = reason or ""

    # V10:个人信息模板自动带入(优先级:显式传参 > env > profile.yaml > config);
    # 个人信息只进表单不进日志(红线 39)。
    if auto_profile and not reporter_name and not reporter_phone:
        try:
            from netsentinel.submit.profile import load_profile

            _profile = load_profile(cfg)
            reporter_name = reporter_name or _profile.name
            reporter_phone = reporter_phone or _profile.phone
        except Exception:  # noqa: BLE001
            pass

    description_body = _DESCRIPTION_TEMPLATE.format(
        site_url=site_url,
        agg=agg,
        nsw_count=nsw_count,
        pages=pages,
        extra=extra,
    )
    description = (reason + chr(10) + description_body) if reason else description_body

    # V10.2:完整个人信息(姓名/电话已上;邮箱/身份证/地址/邮编/类型/单位经 profile 自动带入)
    extra_profile: dict[str, str] = {}
    try:
        from netsentinel.submit.profile import load_profile as _lp

        _p = _lp(cfg)
        extra_profile = _p.to_fill_dict()
    except Exception:  # noqa: BLE001
        extra_profile = {}

    return SubmissionPayload(
        portal=portal,
        site_url=site_url,
        category=DEFAULT_CATEGORY,
        description=description,
        evidence_zip=evidence_zip,
        reporter_name=reporter_name or extra_profile.get("reporter_name", ""),
        reporter_phone=reporter_phone or extra_profile.get("reporter_phone", ""),
        reason=reason,
        reporter_email=extra_profile.get("reporter_email", ""),
        reporter_id=extra_profile.get("reporter_id", ""),
        reporter_address=extra_profile.get("reporter_address", ""),
        reporter_postcode=extra_profile.get("reporter_postcode", ""),
        reporter_type=extra_profile.get("reporter_type", ""),
        reporter_org=extra_profile.get("reporter_org", ""),
    )


# ---------------------------------------------------------------------------
# 步骤计划
# ---------------------------------------------------------------------------
def build_plan(
    payload: SubmissionPayload,
    cfg: Config,
    entry_url: str | None = None,
) -> SubmissionPlan:
    """按 CONTRACTS.md §5 的固定顺序生成声明式举报步骤计划。

    - entry_url 缺省:portal 为 P12377 取 cfg.portal_12377_base,
      为 SHDF 取 cfg.portal_shdf_base;
    - name/phone 即使值为空也生成 fill 步骤,并以 ``meta={"skippable": True}``
      提示执行器跳过空值;
    - 验证码环节只能是 HUMAN_GATE(人工输入),绝不生成自动填写验证码的步骤;
      附件 zip 亦不做自动上传,统一在人工门由人工完成。
    """
    if entry_url is None:
        if payload.portal == Portal.P12377:
            entry_url = cfg.portal_12377_base
        elif payload.portal == Portal.SHDF:
            entry_url = cfg.portal_shdf_base
        else:  # 防御:Portal 目前仅两个成员
            raise ValueError(f"未知举报门户: {payload.portal!r}")

    # V5:12 步用单个列表字面量一次成型(勿改回逐步 append —— 一次构造
    # 避免 12 次列表扩容,且步骤顺序即契约 §5 顺序,便于逐行比对)。
    steps: list[Step] = [
        Step(
            action=StepAction.GOTO,
            label="打开举报入口",
            value=entry_url,
            text="举报入口页面",
        ),
        Step(action=StepAction.WAIT, label="等待页面加载", value=_PAGE_LOAD_WAIT_S),
        Step(
            action=StepAction.SELECT,
            label="选择信息类型",
            selector=SELECTORS["type"],
            value=payload.category,
            text="信息类型下拉框",
        ),
        Step(
            action=StepAction.FILL,
            label="填写举报链接",
            selector=SELECTORS["url"],
            value=payload.site_url,
            text="举报链接输入框",
        ),
        Step(
            action=StepAction.FILL,
            label="填写具体描述",
            selector=SELECTORS["desc"],
            value=payload.description,
            text="具体描述文本域",
        ),
        Step(
            action=StepAction.FILL,
            label="填写举报人姓名",
            selector=SELECTORS["name"],
            value=payload.reporter_name,
            text="举报人姓名输入框",
            meta={"skippable": True},
        ),
        Step(
            action=StepAction.FILL,
            label="填写举报人电话",
            selector=SELECTORS["phone"],
            value=payload.reporter_phone,
            text="举报人电话输入框",
            meta={"skippable": True},
        ),
        # V10.2:两门户完整个人信息(空值 skippable,由 profile.yaml 自动填入)
        Step(
            action=StepAction.FILL,
            label="填写电子邮箱",
            selector=SELECTORS["email"],
            value=getattr(payload, "reporter_email", ""),
            text="电子邮箱输入框",
            meta={"skippable": True},
        ),
        Step(
            action=StepAction.FILL,
            label="填写身份证号",
            selector=SELECTORS["id"],
            value=getattr(payload, "reporter_id", ""),
            text="身份证号输入框",
            meta={"skippable": True},
        ),
        Step(
            action=StepAction.FILL,
            label="填写通讯地址",
            selector=SELECTORS["address"],
            value=getattr(payload, "reporter_address", ""),
            text="通讯地址输入框",
            meta={"skippable": True},
        ),
        Step(
            action=StepAction.FILL,
            label="填写邮政编码",
            selector=SELECTORS["postcode"],
            value=getattr(payload, "reporter_postcode", ""),
            text="邮政编码输入框",
            meta={"skippable": True},
        ),
        Step(
            action=StepAction.FILL,
            label="填写单位名称",
            selector=SELECTORS["org"],
            value=getattr(payload, "reporter_org", ""),
            text="单位名称输入框",
            meta={"skippable": True},
        ),
        Step(action=StepAction.SCREENSHOT, label="填写完成后截图"),
        # V10.1:半自动焦点——光标自动聚焦到验证码输入框,用户只需在浏览器输入验证码+回终端回车
        Step(
            action=StepAction.FOCUS,
            label="聚焦验证码输入框(半自动)",
            selector=SELECTORS["captcha"],
            text="验证码输入框",
        ),
        Step(
            action=StepAction.HUMAN_GATE,
            label="请在浏览器输入验证码,然后回到终端按回车提交",
            selector=SELECTORS["captcha"],
            text="验证码仅限人工输入(光标已就位,直接打字即可)",
        ),
        Step(
            action=StepAction.CLICK,
            label="点击提交",
            selector=SELECTORS["submit"],
            text="提交按钮",
        ),
        Step(action=StepAction.WAIT, label="等待提交处理", value=_SUBMIT_SETTLE_WAIT_S),
        Step(action=StepAction.SCREENSHOT, label="提交结果截图"),
    ]
    telemetry.inc("form.plan_built")
    return SubmissionPlan(
        portal=payload.portal,
        entry_url=entry_url,
        payload=payload,
        steps=steps,
    )


def plan_from_entry(
    entry_like: Any,
    portal: Portal,
    cfg: Config,
    entry_url: str | None = None,
    **payload_kw: Any,
) -> SubmissionPlan:
    """build_payload + build_plan 组合:从复核队列条目一步生成步骤计划。

    payload_kw 透传给 build_payload(extra / reporter_name / reporter_phone)。
    """
    payload = build_payload(entry_like, portal, cfg, **payload_kw)
    return build_plan(payload, cfg, entry_url=entry_url)
