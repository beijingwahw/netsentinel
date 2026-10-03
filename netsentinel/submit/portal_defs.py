"""门户适配器框架:举报门户定义从 Python 硬编码改为 YAML 声明式。[A53]

背景(v1 的 A13/A14):每接入一个举报门户都要新写一个 portal_xxx.py planner;
v3 起门户差异收敛为一份 YAML 声明,新门户接入从"写代码"降为"写配置":

    portals/<key>.yaml
        name:          门户展示名(进入计划首步 meta["portal"])
        entry_url_key: netsentinel.contracts.Config 上承载入口地址的字段名
        category:      该门户的举报类目文案(映射到 PortalDef.category_value)
        selectors:     可选;缺省即 CONTRACTS.md §3 的 全部契约选择器
        note:          可选中文备注(如上线前人工核验提示)

``load_portal_def`` 负责加载与校验(未知键 / 缺键 / 非法 entry_url_key /
selectors 不合契约一律 ValueError 中文);``build_plan_from_def`` 复用 A12
``form_models`` 的 SELECTORS 常量与 Step 构造语义,按 CONTRACTS.md §5 生成
标准 12 步 :class:`SubmissionPlan`。

安全红线(CONTRACTS §0 / V3 §0,违反即缺陷):
1. **入口 URL 不落 yaml**:定义文件只写 ``entry_url_key`` 字段名;运行期
   地址只能来自 ``getattr(cfg, entry_url_key)`` 或调用方显式传参,
   杜绝在 YAML 里硬编码真实门户地址。
2. **验证码绝不自动填写**:``selectors`` 只允许契约已定义的键,且任何非
   captcha 字段与验证码选择器取值相同都会在加载期被拒绝(别名伪装);
   ``build_plan_from_def`` 生成计划后调用导出的自检函数
   ``_assert_no_captcha_autofill`` —— 任何 FILL/SELECT/CLICK 步骤的
   selector 等于验证码选择器立即抛 :class:`RuntimeError`(篡改防御)。
3. 步骤序列与 A12 ``form_models.build_plan`` 完全一致(12 步固定顺序,
   HUMAN_GATE 为第 9 步),本模块不发明新的步骤语义。

用法示例::

    from netsentinel.contracts import Config, Portal
    from netsentinel.submit import form_models, portal_defs

    defn = portal_defs.load_portal_def("12377")            # 短名,按项目根解析
    payload = form_models.build_payload(entry, Portal.P12377, Config())
    plan = portal_defs.build_plan_from_def(defn, payload, Config())

V5 升级:内置短名的路径解析结果模块级缓存(文件路径不变,免重复 stat);
YAML 顶层映射单遍扫描完成校验(错误优先级与文案逐字保持);成功加载接
``telemetry.inc("portal_defs.load")``。

自愈选择器候选链(schema v2,对标 Healenium / Selenium AI 式 self-healing
selector):``selectors`` 的值从单一 CSS 字符串升级为**有序候选链**——

    selectors:
      url:
        - css: "#report-url"          # 主候选(id 打头,即 Step.selector)
        - role: textbox               # aria 语义候选(get_by_role)
          name: "举报链接"
        - label: "举报链接"            # label 候选(get_by_label)

- 候选类型:``css`` / ``role``+``name`` / ``label`` / ``placeholder`` / ``text``,
  每个候选恰好一种类型,链首必须是 css(id)打头——保证 ``Step.selector``
  永远是合法的 ``#id`` 主选择器,v1 全部字符串比对防御(验证码别名、
  ``_assert_no_captcha_autofill``、``_is_submit_click``)原样生效;
- v1 单值字符串语法完全兼容:解析层归一化为单元素链 ``[{"css": ...}]``;
- 加载期逐候选校验(未知键 / 空链 / 坏类型一律中文 ValueError),且
  **验证码别名拒绝对候选链逐候选生效**:任何非 captcha 字段的任何候选,
  以任何形式指向验证码字段(css 等于验证码选择器、文本型取值与 captcha
  声明值相同或含"验证码"字样)一律拒绝加载(防绕过);
- 候选链进入 ``Step.meta["selector_chain"]``,由执行器
  ``_resolve_selector`` 逐候选自愈探测(胜者只进运行时缓存 + 审计 notes,
  绝不回写 YAML);``PortalDef.selectors`` 仍保留主选择器字符串映射,
  ``PortalDef.selector_chains`` 承载归一化后的完整链。

A233 扩展(门户动态发现与登记闭环)::

    discover_portals(directory=None)   # 扫描 portals/*.yaml(排除内置两文件)
    list_portals(include_disabled=False, directory=None)
    get_portal(name, directory=None)   # 查找顺序:内置 PORTAL_FILES → 动态 enabled

安全红线(默认禁网 + 显式启用哲学的门户侧延伸,违反即缺陷):
**动态发现绝不自动生效新门户的提交通道**——新门户 YAML 必须同时满足
(1) 头部显式 ``enabled: true``(草稿落位工具 ``persist_draft`` 写
``enabled: false``,人工确认后改 true 或经 ``confirm_enable_portal``
口令确认——人工确认语义落在配置文件的显式声明上)且 (2)
``entry_url_key`` 指向 :class:`Config` 现有字段
(:func:`_validate_entry_url_key` 的 hasattr 校验先读 Config 实例),
才会被 ``list_portals`` / ``get_portal`` 当作可用门户。未显式启用的
动态门户 :func:`discover_portals` 照样返回(以 ``PortalDef.enabled``
标注 disabled:``False`` = 显式 false,``None`` = 未声明),但
``list_portals(include_disabled=False)`` 不含、``get_portal`` 拒绝。
**两内置门户零改动、兼容豁免**:不写 ``enabled`` 字段也照常加载
(本 docstring 即该豁免的显式注明)。
"""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field
from typing import Any, Iterable

from netsentinel import telemetry
from netsentinel.contracts import Config, Step, StepAction, SubmissionPayload, SubmissionPlan
from netsentinel.submit import form_models

__all__ = [
    "PortalDef",
    "PORTAL_FILES",
    "load_portal_def",
    "build_plan_from_def",
    # A233:门户动态发现与登记闭环(动态门户须显式 enabled: true 才生效)。
    "discover_portals",
    "list_portals",
    "get_portal",
    # 按 A53 契约显式导出:篡改防御测试与下游执行器需直接调用该自检。
    "_assert_no_captcha_autofill",
]

#: 项目根(portals/ 目录位于项目根,与 netsentinel/、tests/ 同级):
#  本文件在 <root>/netsentinel/submit/portal_defs.py,向上两级即项目根。
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: 内置门户定义文件:key 为门户短名(与 Portal 枚举值一致),
#  value 为相对项目根的 YAML 路径。
PORTAL_FILES: dict[str, str] = {
    "12377": "portals/12377.yaml",
    "shdf": "portals/shdf.yaml",
}

#: V5 性能:内置短名的路径解析结果缓存。键为 ``(短名, 相对路径)``
#  —— 含相对路径意味着 ``PORTAL_FILES`` 被测试重定向后会按新值重新解析,
#  不会吐出陈旧缓存;文件路径不变的前提下,首次解析(含 is_file 探测)
#  之后同一短名不再重复访问文件系统。
_RESOLVED_BUILTIN_PATHS: dict[tuple[str, str], pathlib.Path] = {}

#: 门户定义 YAML 允许的顶层键(与 PortalDef 字段对应;category 映射到
#: PortalDef.category_value,避免 dataclass 字段名直接进 YAML 词汇表;
#: enabled 为 A233 动态发现的人工确认标记,仅动态门户受其门槛约束)。
_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"name", "entry_url_key", "category", "selectors", "note", "enabled"}
)

#: 必填键:缺任何一个都无法安全生成计划。
_REQUIRED_KEYS: tuple[str, ...] = ("name", "entry_url_key", "category")

#: 验证码只允许人工输入:这三类自动动作绝不许触碰验证码选择器。
_AUTO_ACTIONS: tuple[StepAction, ...] = (
    StepAction.FILL,
    StepAction.SELECT,
    StepAction.CLICK,
)

#: 候选链词汇表(schema v2):候选类型键。css 单独使用;role 必须与 name
#: 成对出现;label / placeholder / text 单独使用(每候选恰好一种类型)。
_CANDIDATE_KEYS: frozenset[str] = frozenset(
    {"css", "role", "name", "label", "placeholder", "text"}
)

#: 文本型候选键(label/placeholder/text/name):取值参与验证码别名比对
#: (与 captcha 声明值相同,或含"验证码"字样 → 指向验证码字段,拒绝)。
_TEXTLIKE_KEYS: tuple[str, ...] = ("label", "placeholder", "text", "name")

#: 验证码字样:非 captcha 字段的文本型候选含该词 = 语义上指向验证码字段,
#: 加载期与计划自检期双重拒绝(别名伪装防绕过)。
_CAPTCHA_WORD: str = "验证码"


def _default_chains() -> dict[str, list[dict[str, str]]]:
    """契约 §3 的缺省候选链:每字段单元素 css 链(v1 语义等价形态)。"""
    return {key: [{"css": value}] for key, value in form_models.SELECTORS.items()}


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class PortalDef:
    """一份声明式门户定义(YAML 加载结果,亦可手工构造便于测试)。

    属性:
        name: 门户展示名(中文,进入计划首步 ``meta["portal"]``)。
        entry_url_key: 入口地址在 :class:`~netsentinel.contracts.Config`
            上的字段名(如 ``portal_12377_base``);**不是 URL 本身**——
            红线:真实门户地址只允许存在于 Config 或调用方显式传参。
        category_value: 该门户的举报类目文案(计划生成时覆盖
            ``payload.category``,与 A13/A14 的固定类目语义一致)。
        selectors: 每字段的**主选择器**(css,id)映射;缺省复制契约 §3
            (form_models.SELECTORS)。v2 候选链的首个 css 候选与这里同步。
        selector_chains: schema v2 归一化候选链,``{字段: [候选, ...]}``;
            每个候选是单类型映射(css / role+name / label / placeholder /
            text)。缺省 = 每字段单元素 css 链(v1 语义等价形态)。
        note: 中文备注(如"上线前须人工核验真实入口与字段")。
        enabled: 人工确认标记(**三态**,A233 动态发现门槛):

            - ``None`` = YAML 未声明该键——**内置门户兼容豁免**:两内置
              定义不写 ``enabled`` 也照常加载,行为零改动;动态门户则按
              未启用处理(门槛要求**显式** ``enabled: true``);
            - ``True`` = 显式启用(``enabled: true``);
            - ``False`` = 显式禁用(``enabled: false``,草稿落位工具的
              登记缺省值,人工确认后改 true)。

            动态发现(:func:`discover_portals` / :func:`list_portals` /
            :func:`get_portal`)仅当 ``enabled is True`` 才把动态门户当
            作可用提交通道;按路径/短名的 :func:`load_portal_def` 是纯
            加载器,不据此拒载(落位工具写 false 后仍需全链校验)。
    """

    name: str
    entry_url_key: str
    category_value: str
    selectors: dict[str, str] = field(
        default_factory=lambda: dict(form_models.SELECTORS)
    )
    selector_chains: dict[str, list[dict[str, str]]] = field(
        default_factory=_default_chains
    )
    note: str = ""
    enabled: bool | None = None


# ---------------------------------------------------------------------------
# 惰性依赖
# ---------------------------------------------------------------------------
def _import_yaml() -> Any:
    """惰性导入 PyYAML;未安装时抛中文 RuntimeError(附安装提示)。"""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - 环境缺依赖分支
        raise RuntimeError(
            "读取门户定义文件需要 PyYAML(可选依赖),"
            "请先安装:pip install pyyaml"
        ) from exc
    return yaml


# ---------------------------------------------------------------------------
# 路径解析:门户短名 → 项目根下的 portals/<key>.yaml
# ---------------------------------------------------------------------------
def _resolve_portal_file(path_or_key: str | os.PathLike[str]) -> pathlib.Path:
    """把"门户短名或文件路径"解析为存在的 YAML 文件路径。

    - 命中 ``PORTAL_FILES``:按项目根拼接(与运行时工作目录无关),
      解析结果按 (短名, 相对路径) 模块级缓存,后续调用免重复 stat;
    - 否则按文件路径处理(相对路径相对当前工作目录);
    - 既不是内置短名、也不是存在的文件 → ValueError 中文。
    """
    raw = os.fspath(path_or_key)
    if raw in PORTAL_FILES:
        cache_key = (raw, PORTAL_FILES[raw])
        cached = _RESOLVED_BUILTIN_PATHS.get(cache_key)
        if cached is not None:
            return cached
        path = _PROJECT_ROOT / PORTAL_FILES[raw]
        if not path.is_file():
            raise FileNotFoundError(
                f"内置门户 {raw!r} 的定义文件不存在:{path}"
                "(portals/ 目录缺失或文件被移动)"
            )
        _RESOLVED_BUILTIN_PATHS[cache_key] = path
        return path
    path = pathlib.Path(raw)
    if path.is_file():
        return path
    raise ValueError(
        f"未知的门户标识 {raw!r}:既不是内置门户({', '.join(sorted(PORTAL_FILES))}),"
        "也不是一个存在的 YAML 定义文件路径"
    )


# ---------------------------------------------------------------------------
# 字段校验
# ---------------------------------------------------------------------------
def _validate_entry_url_key(key: str) -> None:
    """校验 entry_url_key 指向 Config 现有配置字段(防指向不存在配置)。

    - ``hasattr`` 检查:字段必须是 Config 实例上真实存在的属性;
    - 下划线开头 / 可调用(方法)一律拒绝:入口地址必须是数据字段。
    """
    probe = Config()
    if key.startswith("_"):
        raise ValueError(
            f"entry_url_key 不能以下划线开头:{key!r}(必须是 Config 的公开配置字段名)"
        )
    if not hasattr(probe, key):
        raise ValueError(
            f"entry_url_key {key!r} 不是 netsentinel.contracts.Config 的现有字段;"
            "入口地址必须通过配置字段名引用,请先在 Config 中定义该字段"
        )
    if callable(getattr(probe, key, None)):
        raise ValueError(
            f"entry_url_key {key!r} 指向的是方法而非配置字段,拒绝加载"
        )


def _validate_candidate(raw: Any, *, field_key: str, index: int) -> dict[str, str]:
    """校验并归一化候选链中的单个候选;返回浅拷贝的归一 dict。

    合法形态(每个候选恰好一种类型):

    - ``{"css": "#id"}``(YAML 里写裸字符串按 css 简写归一);
    - ``{"role": "textbox", "name": "网站网址"}``(aria 语义候选,name 必填);
    - ``{"label": "网址"}`` / ``{"placeholder": "请输入网址"}`` / ``{"text": "提交"}``。

    非法形态(未知键 / 空映射 / 坏类型 / 多类型混用 / role 与 name 不成对 /
    css 值非 # 开头)一律中文 :class:`ValueError`,错误信息带字段名与候选序号。
    """
    where = f"selectors.{field_key} 候选链第 {index + 1} 个候选"
    if isinstance(raw, str):
        raw = {"css": raw}
    if not isinstance(raw, dict):
        raise ValueError(
            f"{where} 必须是映射(css / role+name / label / placeholder / text)"
            f"或 css 简写字符串,当前类型是 {type(raw).__name__}"
        )
    if not raw:
        raise ValueError(
            f"{where} 是空映射,必须声明 css / role+name / label / placeholder / text 之一"
        )
    keys = {str(k) for k in raw}
    unknown = sorted(keys - _CANDIDATE_KEYS)
    if unknown:
        raise ValueError(
            f"{where} 含未知键:{unknown}"
            "(候选类型只允许 css / role+name / label / placeholder / text)"
        )
    normalized: dict[str, str] = {}
    for key in sorted(keys):
        item = raw[key]
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"{where} 的 {key} 必须是非空字符串,当前值:{item!r}"
            )
        normalized[key] = item
    # 单一类型约束:css 独占;role 与 name 成对独占;label/placeholder/text 单用。
    if "css" in keys and len(keys) > 1:
        raise ValueError(
            f"{where} 只能是一种类型:css 候选不得与其他键混用(当前键:{sorted(keys)})"
        )
    if keys & {"role", "name"}:
        if keys != {"role", "name"}:
            raise ValueError(
                f"{where} 的 role 与 name 必须成对出现且不与其他键混用"
                f"(当前键:{sorted(keys)})"
            )
    elif len(keys) != 1:
        raise ValueError(
            f"{where} 只能是一种类型:{sorted(keys)} 不是合法组合"
            "(应为 css / role+name / label / placeholder / text 之一)"
        )
    if "css" in normalized and not normalized["css"].startswith("#"):
        raise ValueError(
            f"{where} 的 css 值必须是以 # 开头的 CSS 选择器,当前值:{normalized['css']!r}"
        )
    return normalized


def _validate_selectors(
    value: Any,
) -> tuple[dict[str, str], dict[str, list[dict[str, str]]]]:
    """校验 selectors(v1 单值字符串 / v2 有序候选链)。

    :return: ``(主选择器映射, 归一化候选链)`` ——
        - v1 兼容:值为 ``"#xxx"`` 字符串 → 归一化为单元素链
          ``[{"css": "#xxx"}]``,主选择器即该字符串;
        - v2 候选链:值为有序列表,第 1 个候选必须是 css(id)打头
          (作为主选择器进入 ``Step.selector``),其后可跟 role+name /
          label / placeholder / text 兜底候选。

    红线(**逐候选生效**,防绕过):任何非 captcha 字段的任何候选,以任何
    形式指向验证码字段一律拒绝——css 取值等于验证码选择器(含契约兜底字
    面量)、文本型取值(label/placeholder/text/name)与 captcha 声明值相同
    或含"验证码"字样。
    """
    if not isinstance(value, dict):
        raise ValueError(
            "selectors 必须是键值映射(契约 §3 的 8 个键),"
            f"当前类型是 {type(value).__name__}"
        )
    expected = set(form_models.SELECTORS)
    given = {str(k) for k in value}
    missing = sorted(expected - given)
    if missing:
        raise ValueError(
            f"selectors 缺少契约键:{missing}(必须提供全部 全部契约选择器)"
        )
    extra = sorted(given - expected)
    if extra:
        raise ValueError(
            f"selectors 含未知键:{extra}(契约 §3 定义的选择器键见 form_models.SELECTORS)"
        )
    selectors: dict[str, str] = {}
    chains: dict[str, list[dict[str, str]]] = {}
    for key in form_models.SELECTORS:  # 按契约键序输出,便于比对
        raw = value[key]
        if isinstance(raw, str):
            # v1 单值语法:错误文案与历史逐字保持。
            if not raw.strip():
                raise ValueError(
                    f"selectors.{key} 必须是非空字符串,当前值:{raw!r}"
                )
            if not raw.startswith("#"):
                raise ValueError(
                    f"selectors.{key} 的值必须是以 # 开头的 CSS 选择器,当前值:{raw!r}"
                )
            selectors[key] = raw
            chains[key] = [{"css": raw}]
        elif isinstance(raw, list):
            if not raw:
                raise ValueError(
                    f"selectors.{key} 候选链不能是空列表(至少提供 css 主候选)"
                )
            candidates = [
                _validate_candidate(item, field_key=key, index=i)
                for i, item in enumerate(raw)
            ]
            if "css" not in candidates[0]:
                raise ValueError(
                    f"selectors.{key} 候选链第 1 个候选必须是 css(id)打头"
                    f"(作为主选择器),当前首个候选:{candidates[0]!r}"
                )
            selectors[key] = candidates[0]["css"]
            chains[key] = candidates
        else:
            raise ValueError(
                f"selectors.{key} 必须是非空字符串(v1)或候选链列表(v2),"
                f"当前类型是 {type(raw).__name__}"
            )
    # 红线一(主选择器层面,文案与 v1 逐字保持):验证码选择器别名伪装。
    captcha = selectors["captcha"]
    aliased = sorted(k for k, v in selectors.items() if k != "captcha" and v == captcha)
    if aliased:
        raise ValueError(
            f"安全红线:验证码选择器({captcha})只允许 captcha 键使用,"
            f"以下字段取值与之相同(别名伪装),拒绝加载:{aliased}"
        )
    # 红线二(逐候选,防绕过):任何非 captcha 字段的任何候选指向验证码字段。
    # 比对集合 = 本定义 captcha 链的全部取值 ∪ 契约验证码选择器双保险;
    # 文本型候选额外做"验证码"字样子串匹配,杜绝换措辞伪装。
    suspect_css = {captcha, form_models.SELECTORS.get("captcha", ""), "#report-captcha"}
    suspect_css.discard("")
    suspect_texts = {
        cand[key]
        for cand in chains["captcha"]
        for key in _TEXTLIKE_KEYS
        if key in cand
    }
    for key in form_models.SELECTORS:
        if key == "captcha":
            continue
        for i, cand in enumerate(chains[key]):
            css = cand.get("css")
            if css in suspect_css:
                raise ValueError(
                    f"安全红线:selectors.{key} 候选链第 {i + 1} 个候选的 css"
                    f"({css})指向验证码选择器(别名伪装),拒绝加载"
                )
            for textlike in _TEXTLIKE_KEYS:
                text = cand.get(textlike)
                if text and (text in suspect_texts or _CAPTCHA_WORD in text):
                    raise ValueError(
                        f"安全红线:selectors.{key} 候选链第 {i + 1} 个候选的"
                        f"{textlike}({text!r})指向验证码字段(别名伪装),拒绝加载"
                    )
    return selectors, chains


# ---------------------------------------------------------------------------
# 加载器
# ---------------------------------------------------------------------------
def load_portal_def(path_or_key: str | os.PathLike[str]) -> PortalDef:
    """加载并校验一份门户定义 YAML,返回 :class:`PortalDef`。

    参数:
        path_or_key: 内置门户短名("12377" / "shdf",按项目根解析到
            ``portals/<key>.yaml``),或任意 YAML 定义文件的路径
            (``str`` / ``pathlib.Path`` 均可)。

    异常:
        ValueError: YAML 不合法 / 顶层不是映射 / 含未知键 / 缺必填键
            (name、entry_url_key、category)/ 字段类型错误 /
            entry_url_key 不是 Config 现有字段 / selectors 不合契约
            (缺键、多键、值非 # 开头、与验证码选择器重复;v2 候选链另含:
            未知候选键、空链、坏候选类型、多类型混用、role 与 name 不成对、
            链首非 css、任何候选以任何形式指向验证码字段)。
        FileNotFoundError: 内置短名对应的定义文件在磁盘上不存在。
        RuntimeError: PyYAML 未安装(中文安装提示)。
    """
    path = _resolve_portal_file(path_or_key)
    yaml = _import_yaml()
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"门户定义文件不是合法 YAML:{path}(解析失败:{exc})") from exc

    if not isinstance(raw, dict):
        raise ValueError(
            f"门户定义文件顶层必须是映射(键值表):{path},"
            f"当前类型是 {type(raw).__name__}"
        )

    # V5 性能/质量:对顶层映射单遍扫描,一次遍历同时完成"未知键收集"
    # 与"已知键取值"(替代原先的多次独立扫描);随后按既有优先级
    # (未知键 > 缺必填键 > 字段类型)报错,错误文案逐字保持不变。
    allowed_values: dict[str, Any] = {}
    unknown: list[str] = []
    for raw_key in raw:
        key = str(raw_key)
        if key in _ALLOWED_KEYS:
            allowed_values[key] = raw[raw_key]
        else:
            unknown.append(key)
    unknown.sort()
    if unknown:
        raise ValueError(
            f"门户定义包含未知键:{unknown},"
            f"只允许 {sorted(_ALLOWED_KEYS)}(文件:{path})"
        )

    missing = [key for key in _REQUIRED_KEYS if key not in allowed_values]
    if missing:
        raise ValueError(f"门户定义缺少必填键 {missing[0]!r}(文件:{path})")

    name = allowed_values["name"]
    entry_url_key = allowed_values["entry_url_key"]
    category = allowed_values["category"]
    for label, val in (("name", name), ("entry_url_key", entry_url_key), ("category", category)):
        if not isinstance(val, str) or not val.strip():
            raise ValueError(
                f"门户定义的 {label} 必须是非空字符串,当前值:{val!r}(文件:{path})"
            )

    # 红线:入口只允许引用 Config 字段,防止 YAML 指向不存在的配置。
    _validate_entry_url_key(entry_url_key)

    note = allowed_values.get("note", "")
    if not isinstance(note, str):
        raise ValueError(f"门户定义的 note 必须是字符串,当前值:{note!r}(文件:{path})")

    # A233 人工确认标记:布尔三态(None=未声明)。仅动态发现侧受其门槛
    # 约束,load_portal_def 本身是纯加载器(内置门户不写该键也加载——
    # 兼容豁免;落位工具写 false 后仍需经本函数全链校验)。
    enabled = allowed_values.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError(
            f"门户定义的 enabled 必须是布尔值(true/false),当前值:{enabled!r}(文件:{path})"
        )

    selectors = allowed_values.get("selectors")
    if selectors is None:
        selectors = dict(form_models.SELECTORS)  # 缺省 = 契约 §3
        selector_chains = _default_chains()      # 缺省链 = 每字段单元素 css 链
    else:
        selectors, selector_chains = _validate_selectors(selectors)

    telemetry.inc("portal_defs.load")
    return PortalDef(
        name=name,
        entry_url_key=entry_url_key,
        category_value=category,
        selectors=selectors,
        selector_chains=selector_chains,
        note=note,
        enabled=enabled,
    )


# ---------------------------------------------------------------------------
# 动态发现与登记闭环(A233):discover_portals / list_portals / get_portal
# ---------------------------------------------------------------------------
#: 动态门户文件名后缀(仅扫描 *.yaml;短名 = 去后缀文件名)。
_DYNAMIC_SUFFIX: str = ".yaml"


def discover_portals(
    directory: str | os.PathLike[str] | None = None,
) -> dict[str, PortalDef | ValueError]:
    """扫描目录下的动态门户定义,返回 ``{短名: PortalDef 或 加载错误}``。

    扫描规则(确定性:按文件名排序,同目录同结果):

    - 目录缺省为项目根 ``portals/``;目录不存在 → 空映射(不抛错);
    - 仅取直下 ``*.yaml``;**排除内置两文件名**(``12377.yaml`` /
      ``shdf.yaml``,内置门户由 :data:`PORTAL_FILES` 显式登记,不参与
      动态发现,杜绝同名遮蔽);
    - 每个文件逐一经 :func:`load_portal_def` 全链校验(未知键 / 缺键 /
      非法 entry_url_key / selectors 验证码别名红线 / enabled 类型),
      成功 → :class:`PortalDef`,失败 → 原样保存 :class:`ValueError`
      实例作"加载错误"标注(**不中断扫描**,坏文件不遮蔽同目录其余文件)。

    enabled 门槛(红线:动态发现绝不自动生效新门户的提交通道):

    - **未启用的门户本函数照样返回**,以 ``PortalDef.enabled`` 标注
      disabled——``False`` = 显式 ``enabled: false``,
      ``None`` = 未声明该键(门槛要求**显式** ``enabled: true``,
      草稿落位工具写 false、人工确认后改 true);
    - "是否可用"的过滤发生在 :func:`list_portals` /
      :func:`get_portal`(仅 ``enabled is True`` 的动态门户可加载),
      本函数只做发现与标注,供人工巡检/登记台展示。

    注意:entry_url_key 的 Config 对齐由 ``load_portal_def`` 内的
    :func:`_validate_entry_url_key` 先读 Config 实例(hasattr)保证——
    动态 YAML 指向不存在的配置字段在这里就是加载错误。
    """
    root = (
        pathlib.Path(directory)
        if directory is not None
        else _PROJECT_ROOT / "portals"
    )
    if not root.is_dir():
        return {}
    builtin_names = {pathlib.Path(rel).name for rel in PORTAL_FILES.values()}
    found: dict[str, PortalDef | ValueError] = {}
    for path in sorted(root.glob(f"*{_DYNAMIC_SUFFIX}")):
        if not path.is_file() or path.name in builtin_names:
            continue
        try:
            found[path.stem] = load_portal_def(path)
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            found[path.stem] = exc  # 加载错误标注(中文消息原样保留)
    return found


def list_portals(
    include_disabled: bool = False,
    directory: str | os.PathLike[str] | None = None,
) -> dict[str, PortalDef]:
    """列出可加载门户:内置(豁免,恒含)∪ 动态 enabled。

    - 内置门户按 :data:`PORTAL_FILES` 顺序经 :func:`load_portal_def`
      加载(**兼容豁免**:不写 ``enabled`` 键也包含,行为零改动);
    - 动态门户来自 :func:`discover_portals`:默认仅含显式
      ``enabled: true`` 的定义;``include_disabled=True`` 时把未启用
      (``enabled`` 为 ``False``/``None``)的动态定义也列出(登记台
      巡检用——列出 ≠ 可经 :func:`get_portal` 按短名加载);
    - 加载失败的动态文件(错误标注)一律跳过,不中断列举(错误详情
      用 :func:`discover_portals` 查看)。

    :return: ``{短名: PortalDef}``(内置在前、动态按短名排序,确定性)。
    """
    combined: dict[str, PortalDef] = {}
    for key in PORTAL_FILES:
        combined[key] = load_portal_def(key)
    for name, entry in sorted(discover_portals(directory).items()):
        if isinstance(entry, PortalDef) and (entry.enabled is True or include_disabled):
            combined[name] = entry
    return combined


def get_portal(
    name: str,
    directory: str | os.PathLike[str] | None = None,
) -> PortalDef:
    """按短名取门户定义,查找顺序 = 内置 → 动态 enabled。

    - 命中 :data:`PORTAL_FILES` → 直接 :func:`load_portal_def(短名)`
      (内置豁免:不写 ``enabled`` 键也加载,行为与 A53 起零差别);
    - 否则查 :func:`discover_portals`:仅 **显式 ``enabled: true``** 的
      动态定义可返回(动态发现绝不自动生效未确认门户的红线落点);
      ``enabled`` 为 ``False``/``None`` → 中文 :class:`ValueError`
      (提示人工把 ``enabled`` 改 ``true`` 后自动生效);定义文件加载
      失败 → 透传加载错误;两者皆无 → "未知的门户标识"。
    """
    key = str(name)
    if key in PORTAL_FILES:
        return load_portal_def(key)
    entry = discover_portals(directory).get(key)
    if entry is None:
        raise ValueError(
            f"未知的门户标识 {key!r}:既不是内置门户({', '.join(sorted(PORTAL_FILES))}),"
            "也不是 portals/ 目录下经 discover_portals 发现的动态门户"
        )
    if isinstance(entry, Exception):
        raise ValueError(
            f"动态门户 {key!r} 的定义文件加载失败,拒绝按短名使用:{entry}"
        ) from entry
    if entry.enabled is not True:
        raise ValueError(
            f"动态门户 {key!r} 尚未启用(enabled={entry.enabled!r},"
            "须显式声明 enabled: true)——红线:动态发现绝不自动生效新门户的"
            "提交通道;人工核验后把定义文件头部 enabled 改为 true"
            "(或经草稿工作流 confirm_enable_portal 口令确认),即自动生效"
        )
    return entry


# ---------------------------------------------------------------------------
# 篡改防御自检(按 A53 契约导出)
# ---------------------------------------------------------------------------
def _assert_no_captcha_autofill(
    steps: Iterable[Step],
    captcha_selectors: Iterable[str] | None = None,
) -> None:
    """校验步骤序列:验证码选择器绝不许出现在自动填写/选择/点击步骤里。

    比对集合 = 契约 §3 验证码选择器(form_models.SELECTORS["captcha"] 与
    兜底字面量 ``#report-captcha`` 双保险)∪ 调用方补充的门户自定义值
    (v2 起调用方可同时传入 captcha 链的文本型候选值);任何
    FILL/SELECT/CLICK 步骤的 ``selector`` 命中该集合 → 中文
    :class:`RuntimeError`。HUMAN_GATE(人工输入验证码)不受限制。

    **候选链逐候选生效**(红线,v2):``Step.meta["selector_chain"]`` 里的
    每个候选同样受检——css 取值命中比对集合、文本型取值(label/
    placeholder/text/name)与比对集合相同或含"验证码"字样,均立即拒绝
    (防"主选择器干净、候选链夹带验证码"的绕过路径)。

    该函数按 A53 契约导出:除 ``build_plan_from_def`` 内部调用外,
    执行器/测试可对任意 Step 序列直接做篡改防御检查。
    """
    suspects: set[str] = {
        form_models.SELECTORS.get("captcha", ""),
        "#report-captcha",  # 契约兜底:防 SELECTORS 常量本身被篡改
    }
    suspects.update(s for s in (captcha_selectors or ()) if s)
    suspects.discard("")
    for step in steps:
        if step.action not in _AUTO_ACTIONS:
            continue
        if step.selector and step.selector in suspects:
            raise RuntimeError(
                f"安全红线:检测到自动步骤({step.action.value}「{step.label}」)"
                f"试图触碰验证码选择器 {step.selector};"
                "验证码只允许 HUMAN_GATE 步骤人工输入,该计划已被拒绝"
            )
        chain = step.meta.get("selector_chain") if isinstance(step.meta, dict) else None
        if not isinstance(chain, list):
            continue
        for i, cand in enumerate(chain):
            if not isinstance(cand, dict):
                continue
            css = cand.get("css")
            if isinstance(css, str) and css in suspects:
                raise RuntimeError(
                    f"安全红线:检测到自动步骤({step.action.value}「{step.label}」)"
                    f"候选链第 {i + 1} 个候选的 css({css})指向验证码选择器;"
                    "验证码只允许 HUMAN_GATE 步骤人工输入,该计划已被拒绝"
                )
            for textlike in _TEXTLIKE_KEYS:
                text = cand.get(textlike)
                if isinstance(text, str) and text and (text in suspects or _CAPTCHA_WORD in text):
                    raise RuntimeError(
                        f"安全红线:检测到自动步骤({step.action.value}「{step.label}」)"
                        f"候选链第 {i + 1} 个候选的 {textlike}({text!r})指向验证码字段;"
                        "验证码只允许 HUMAN_GATE 步骤人工输入,该计划已被拒绝"
                    )


# ---------------------------------------------------------------------------
# 计划生成
# ---------------------------------------------------------------------------
def _selector_of(defn: PortalDef, key: str, default: str = "") -> str:
    """取门户定义的选择器;定义缺失该键时回退契约 §3 常量。"""
    value = defn.selectors.get(key)
    if isinstance(value, str) and value:
        return value
    return form_models.SELECTORS[key]


def _captcha_tokens(defn: PortalDef) -> list[str]:
    """captcha 声明链的全部文本型候选值(供自检比对集合使用)。"""
    return [
        cand[key]
        for cand in defn.selector_chains.get("captcha", ())
        for key in _TEXTLIKE_KEYS
        if key in cand
    ]


def _chain_of(defn: PortalDef, key: str, default: str = "") -> list[dict[str, str]]:
    """取字段的归一化候选链:主选择器(以 selectors 字典为准)+ 声明链兜底。

    第 1 个候选恒为 ``{"css": <主选择器>}``,与 ``Step.selector`` 同源;
    其余候选浅拷贝自 ``defn.selector_chains[key][1:]``。加载期已校验声明链
    首位 css 与主选择器一致;若定义对象事后被原地篡改(selectors 字典被
    monkeypatch 等),则以 selectors 字典为准重建首候选 —— 保证
    ``Step.selector`` 与候选链永不脱钩,主选择器层面的全部字符串比对
    防御(验证码别名 / 提交按钮识别)对链式计划同样生效。
    """
    primary = _selector_of(defn, key, default)
    tail = [dict(cand) for cand in defn.selector_chains.get(key, [])[1:]]
    return [{"css": primary}, *tail]


def build_plan_from_def(
    defn: PortalDef,
    payload: SubmissionPayload,
    cfg: Config,
    entry_url: str | None = None,
) -> SubmissionPlan:
    """按门户定义生成 CONTRACTS.md §5 的标准 12 步举报计划。

    - 入口 URL:``entry_url`` 显式传参优先,否则 ``getattr(cfg,
      defn.entry_url_key)``;两者皆无 / 非 http(s) → ValueError 中文
      (红线:YAML 与本模块都不硬编码真实门户地址)。
    - 类目:``defn.category_value`` 覆盖 ``payload.category``(与 A13/A14
      固定类目语义一致),并同步 SELECT 步骤与 payload 保持一致。
    - 选择器:取 ``defn.selectors``(缺省即 form_models.SELECTORS 副本),
      Step 的 label/text/value/meta(skippable)语义与 A12 完全一致;
      v2 起每个 SELECT/FILL/CLICK 步骤额外携带
      ``meta["selector_chain"]``(主候选与 ``Step.selector`` 同源,兜底
      候选来自 YAML 声明链),供执行器自愈回退;FOCUS/HUMAN_GATE 步骤
      不携带候选链(验证码语义红线,保持 v1 形态)。
    - 首步 ``meta`` 标注 ``{"portal": defn.name}`` 供执行器/审计区分渠道。
    - 生成后自检(防篡改红线):任何 FILL/SELECT/CLICK 步骤的 selector
      不得等于验证码选择器,否则 :class:`RuntimeError` 中文;同时要求
      计划必须包含 HUMAN_GATE 人工门。

    异常:
        ValueError: 入口 URL 缺失或不是 http(s):// 完整链接。
        RuntimeError: 生成的步骤序列违反安全红线(验证码自动填写 /
            缺少人工门)。
    """
    # 1) 入口地址:显式参数 > cfg 字段;绝不取自 YAML。
    resolved = entry_url if entry_url is not None else getattr(cfg, defn.entry_url_key, "")
    if not isinstance(resolved, str):
        resolved = str(resolved)
    resolved = resolved.strip()
    if not resolved.lower().startswith(("http://", "https://")):
        raise ValueError(
            "举报入口 entry_url 缺失或不是 http(s):// 开头的完整 URL"
            f"(缺省应取 cfg.{defn.entry_url_key};本模块与门户 YAML 均不硬编码门户地址)"
        )

    # 2) 类目:门户定义覆盖 payload 类目,payload 与 SELECT 步骤保持一致。
    if defn.category_value:
        payload.category = defn.category_value

    sel_type = _selector_of(defn, "type")
    sel_url = _selector_of(defn, "url")
    sel_desc = _selector_of(defn, "desc")
    sel_name = _selector_of(defn, "name")
    sel_phone = _selector_of(defn, "phone")
    sel_captcha = _selector_of(defn, "captcha")
    sel_submit = _selector_of(defn, "submit")
    # v2 自愈候选链:主候选与上面主选择器同源,其余候选来自 YAML 声明链。
    chain_type = _chain_of(defn, "type")
    chain_url = _chain_of(defn, "url")
    chain_desc = _chain_of(defn, "desc")
    chain_name = _chain_of(defn, "name")
    chain_phone = _chain_of(defn, "phone")
    chain_submit = _chain_of(defn, "submit")

    # 3) 契约 §5 固定 12 步(Step 语义与 A12 form_models.build_plan 一致)。
    steps: list[Step] = [
        Step(
            action=StepAction.GOTO,
            label="打开举报入口",
            value=resolved,
            text="举报入口页面",
            meta={"portal": defn.name},
        ),
        Step(action=StepAction.WAIT, label="等待页面加载", value="1"),
        Step(
            action=StepAction.SELECT,
            label="选择信息类型",
            selector=sel_type,
            value=payload.category,
            text="信息类型下拉框",
            meta={"selector_chain": chain_type},
        ),
        Step(
            action=StepAction.FILL,
            label="填写举报链接",
            selector=sel_url,
            value=payload.site_url,
            text="举报链接输入框",
            meta={"selector_chain": chain_url},
        ),
        Step(
            action=StepAction.FILL,
            label="填写具体描述",
            selector=sel_desc,
            value=payload.description,
            text="具体描述文本域",
            meta={"selector_chain": chain_desc},
        ),
        Step(
            action=StepAction.FILL,
            label="填写举报人姓名",
            selector=sel_name,
            value=payload.reporter_name,
            text="举报人姓名输入框",
            meta={"skippable": True, "selector_chain": chain_name},
        ),
        Step(
            action=StepAction.FILL,
            label="填写举报人电话",
            selector=sel_phone,
            value=payload.reporter_phone,
            text="举报人电话输入框",
            meta={"skippable": True, "selector_chain": chain_phone},
        ),
        # V10.2:两门户完整个人信息(空值 skippable)
        Step(action=StepAction.FILL, label="填写电子邮箱",
             selector=_selector_of(defn, "email", "#report-email"),
             value=getattr(payload, "reporter_email", ""), text="电子邮箱输入框",
             meta={"skippable": True, "selector_chain": _chain_of(defn, "email", "#report-email")}),
        Step(action=StepAction.FILL, label="填写身份证号",
             selector=_selector_of(defn, "id", "#report-idcard"),
             value=getattr(payload, "reporter_id", ""), text="身份证号输入框",
             meta={"skippable": True, "selector_chain": _chain_of(defn, "id", "#report-idcard")}),
        Step(action=StepAction.FILL, label="填写通讯地址",
             selector=_selector_of(defn, "address", "#report-address"),
             value=getattr(payload, "reporter_address", ""), text="通讯地址输入框",
             meta={"skippable": True, "selector_chain": _chain_of(defn, "address", "#report-address")}),
        Step(action=StepAction.FILL, label="填写邮政编码",
             selector=_selector_of(defn, "postcode", "#report-postcode"),
             value=getattr(payload, "reporter_postcode", ""), text="邮政编码输入框",
             meta={"skippable": True, "selector_chain": _chain_of(defn, "postcode", "#report-postcode")}),
        Step(action=StepAction.FILL, label="填写单位名称",
             selector=_selector_of(defn, "org", "#report-org"),
             value=getattr(payload, "reporter_org", ""), text="单位名称输入框",
             meta={"skippable": True, "selector_chain": _chain_of(defn, "org", "#report-org")}),
        Step(action=StepAction.SCREENSHOT, label="填写完成后截图"),
        # V10.1:半自动焦点——光标自动聚焦到验证码输入框
        Step(
            action=StepAction.FOCUS,
            label="聚焦验证码输入框(半自动)",
            selector=sel_captcha,
            text="验证码输入框",
        ),
        Step(
            action=StepAction.HUMAN_GATE,
            label="请在浏览器输入验证码,然后回到终端按回车提交",
            selector=sel_captcha,
            text="验证码仅限人工输入(光标已就位,直接打字即可)",
        ),
        Step(
            action=StepAction.CLICK,
            label="点击提交",
            selector=sel_submit,
            text="提交按钮",
            meta={"selector_chain": chain_submit},
        ),
        Step(action=StepAction.WAIT, label="等待提交处理", value="2"),
        Step(action=StepAction.SCREENSHOT, label="提交结果截图"),
    ]

    # 4) 防篡改自检(红线):验证码绝不自动填写;必须有人工门。
    #    比对集合 = 主验证码选择器 + captcha 声明链的文本型候选值 ——
    #    自检对候选链逐候选生效(见 _assert_no_captcha_autofill)。
    _assert_no_captcha_autofill(steps, [sel_captcha, *_captcha_tokens(defn)])
    if not any(step.action is StepAction.HUMAN_GATE for step in steps):
        raise RuntimeError(
            "安全红线:门户计划缺少 HUMAN_GATE 人工门步骤"
            "(人工核对与验证码输入),拒绝返回"
        )

    return SubmissionPlan(
        portal=payload.portal,
        entry_url=resolved,
        payload=payload,
        steps=steps,
    )
