"""A153 · 连接向导流程状态机(依据 CONTRACTS-V8.md §3 A153 行 / §0 红线 34)。

向导(setup 页)与自动接管(takeover)共用的**纯逻辑状态机**:回答
"用户现在处于哪个连接阶段、下一步该提示什么、推荐走哪条路"。本模块:

- **不联网、不落密钥**:本地探测结果由 A143(仅回环受限端口,红线 32)
  产出后以 ``scanner_results`` 传入;云密钥只问"是否已配"
  (``security.keys.configured`` 布尔表,绝不回显本体,红线 33);
- **状态机四态**::class:`SetupState`(str Enum)——``no_model`` /
  ``local_connected`` / ``cloud_connected`` / ``stub_only``;
  :func:`next_step` 给出每态的中文下一步提示;
- **推荐路由**::func:`recommend` 按 **本地优先 → 云 → 向导** 三级推荐
  (契约 §3 A153 行;本地条目含视觉模型时给出可直接启用的 spec,
  提供方按端口映射目录还原);
- **状态持久化**::func:`persist` / :func:`load`(``setup_state.json``),
  损坏 / 缺席 / 未知值一律安全回退 ``no_model``,绝不抛出;
- **转移表**::data:`ALLOWED`——向导(no_model)完成本地/云激活后转对应
  态;离线桩可从任意态进入(含 stub 自身;红线 34:桩态提示必须明示
  "离线桩,非模型判定")。

红线协同(红线 34):``stub_only`` 的提示文案由本模块统一口径
("正在使用离线桩,非模型判定,结果仅供参考"),向导页面/输出直接复用,
保证任何入口看到的桩警示一致。

兄弟模块 A150 ``vision.capability`` 允许缺席:视觉过滤惰性导入,缺席时按
A143 同款口径降级——"缺席全返回"(任意非空模型名都算候选,宁可不误扰
用户)。只用标准库。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from enum import Enum
from importlib import import_module
from pathlib import Path
from typing import Any, Callable

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.security import keys

__all__ = [
    "SetupState",
    "ALLOWED",
    "PORT_PROVIDER_MAP",
    "next_step",
    "can_transition",
    "allowed_targets",
    "recommend",
    "persist",
    "load",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 状态枚举与下一步提示
# ---------------------------------------------------------------------------


class SetupState(str, Enum):
    """向导流程四态(str Enum:值即小写状态名,可直接 JSON 序列化)。

    - ``NO_MODEL``:既无本地视觉服务、也无云密钥——需要向导引导;
    - ``LOCAL_CONNECTED``:已激活本地视觉模型(ollama/lmstudio/vllm/
      xinference,免 vlm_online 闸门与密钥);
    - ``CLOUD_CONNECTED``:已连接云平台(仍受 vlm_online + 密钥双条件与
      VLM 预算约束,红线 34 不因接管放宽);
    - ``STUB_ONLY``:使用离线桩继续——非模型判定,结果仅供参考
      (红线 34 的界面明示口径)。
    """

    NO_MODEL = "no_model"
    LOCAL_CONNECTED = "local_connected"
    CLOUD_CONNECTED = "cloud_connected"
    STUB_ONLY = "stub_only"


#: 各态的中文下一步提示(契约 §3 A153;stub 文案即红线 34 明示口径)
_NEXT_STEP_HINTS: dict[SetupState, str] = {
    SetupState.NO_MODEL: "请扫描本机服务或配置云平台密钥",
    SetupState.LOCAL_CONNECTED: "已连接本地视觉模型,可直接开始扫描",
    SetupState.CLOUD_CONNECTED: "已连接云平台(注意 vlm_online 与预算)",
    SetupState.STUB_ONLY: "正在使用离线桩,非模型判定,结果仅供参考",
}


def _coerce_state(state: Any) -> SetupState:
    """把入参规整为 :class:`SetupState` 成员;无法识别时抛中文 ValueError。

    接受成员本身、**值**字符串(``"stub_only"``,大小写不敏感)与
    **名称**字符串(``"STUB_ONLY"``);其余(空串 / None / 数字等)
    一律 ValueError(中文,列出全部合法值)。
    """
    if isinstance(state, SetupState):
        return state
    if isinstance(state, str):
        text = state.strip()
        lowered = text.casefold()
        for member in SetupState:
            if lowered == member.value.casefold() or lowered == member.name.casefold():
                return member
    raise ValueError(
        f"未知的向导流程状态:{state!r}。合法值:"
        + "、".join(m.value for m in SetupState)
    )


def _coerce_state_or_none(state: Any) -> SetupState | None:
    """:func:`_coerce_state` 的容错版:无法识别返回 None(不抛)。"""
    try:
        return _coerce_state(state)
    except ValueError:
        return None


def next_step(state: SetupState | str) -> str:
    """返回指定状态的中文下一步提示(纯函数,绝不抛出业务异常)。

    - 四态提示::

        NO_MODEL        → 请扫描本机服务或配置云平台密钥
        LOCAL_CONNECTED → 已连接本地视觉模型,可直接开始扫描
        CLOUD_CONNECTED → 已连接云平台(注意 vlm_online 与预算)
        STUB_ONLY       → 正在使用离线桩,非模型判定,结果仅供参考

    - ``state`` 可传成员或值/名字符串(大小写不敏感);
    - 未知输入抛中文 ``ValueError``(向导页兜底为 NO_MODEL 提示即可,
      本模块宁可显式失败也不静默给错提示)。
    """
    return _NEXT_STEP_HINTS[_coerce_state(state)]


# ---------------------------------------------------------------------------
# 转移表:ALLOWED
# ---------------------------------------------------------------------------

#: 合法状态转移表(契约 §3 A153:no_model→{local_connected, cloud_connected,
#: stub_only};向导完成本地/云激活后转对应态;离线桩可从**任意**态进入,
#: 含 stub 自身重入)。字符串入参请经 :func:`can_transition` 规整后判定。
ALLOWED: frozenset[tuple[SetupState, SetupState]] = frozenset({
    # 向导(no_model)完成激活:本地 / 云
    (SetupState.NO_MODEL, SetupState.LOCAL_CONNECTED),
    (SetupState.NO_MODEL, SetupState.CLOUD_CONNECTED),
    # 离线桩:任意态可进入(红线 34 桩兜底永远可用)
    (SetupState.NO_MODEL, SetupState.STUB_ONLY),
    (SetupState.LOCAL_CONNECTED, SetupState.STUB_ONLY),
    (SetupState.CLOUD_CONNECTED, SetupState.STUB_ONLY),
    (SetupState.STUB_ONLY, SetupState.STUB_ONLY),
})


def can_transition(source: SetupState | str, target: SetupState | str) -> bool:
    """判断 ``source → target`` 是否为 :data:`ALLOWED` 内的合法转移。

    入参可为成员或值/名字符串;任一端无法识别 → ``False``
    (宁可拒绝也不猜)。同态自留(除 stub 重入)不算转移。
    """
    src = _coerce_state_or_none(source)
    dst = _coerce_state_or_none(target)
    if src is None or dst is None:
        return False
    return (src, dst) in ALLOWED


def allowed_targets(state: SetupState | str) -> set[SetupState]:
    """返回 ``state`` 的全部合法后继态(向导页渲染可选去向用)。"""
    src = _coerce_state(state)
    return {dst for (frm, dst) in ALLOWED if frm == src}


# ---------------------------------------------------------------------------
# recommend:本地优先 → 云 → 向导
# ---------------------------------------------------------------------------

#: 本地端口 → 提供方映射(契约 §1 local_probe_ports 与 A61 目录 base_url
#: 一致:11434→ollama、1234→lmstudio、8000→vllm、9997→xinference)
PORT_PROVIDER_MAP: dict[str, str] = {
    "11434": "ollama",
    "1234": "lmstudio",
    "8000": "vllm",
    "9997": "xinference",
}

#: 提供方回退白名单形态:小写字母开头 + 字母数字下划线连字符
#: (挡掉 A143 对未知端口的 "openai 兼容" 这类非 spec 口径文案)
_PROVIDER_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _try_import(dotted: str) -> Any:
    """惰性导入兄弟模块;任何导入失败都视为"未就位"返回 None(降级,不抛)。"""
    try:
        return import_module(dotted)
    except Exception as exc:  # noqa: BLE001 - 并行期 ImportError/SyntaxError 等一律降级
        logger.debug("兄弟模块 %s 未就位(忽略):%s", dotted, exc)
        return None


def _load_vision_predicate() -> Callable[[str], bool] | None:
    """惰性取 A150 ``is_vision_model``;未就位返回 None(调用方降级全返回)。"""
    capability = _try_import("netsentinel.vision.capability")
    func = getattr(capability, "is_vision_model", None) if capability else None
    return func if callable(func) else None


def _model_names(models: Any) -> list[str]:
    """把扫描结果的模型列表规整为名字列表:兼容 str / {"id"|"name"} / 属性对象。"""
    names: list[str] = []
    for item in models or []:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = str(item.get("id") or item.get("name") or "")
        else:
            name = str(getattr(item, "id", None) or getattr(item, "name", None) or "")
        if name.strip():
            names.append(name.strip())
    return names


def _port_from_base_url(base_url: Any) -> str | None:
    """从 base_url 提取端口数字串;无端口 / 形态异常返回 None。

    ``http://127.0.0.1:11434/v1`` → ``"11434"``;IPv6 字面量
    ``http://[::1]:1234`` → ``"1234"``(rsplit 取最后一段)。
    """
    text = str(base_url or "").strip()
    if not text:
        return None
    host_port = text.split("//", 1)[-1].split("/", 1)[0]
    if ":" not in host_port:
        return None
    port = host_port.rsplit(":", 1)[-1].strip()
    return port if port.isdigit() else None


def _provider_for_entry(entry: dict[str, Any]) -> str | None:
    """为扫描条目还原 spec 提供方:**端口映射优先**,回退条目 provider 白名单。

    - 端口命中 :data:`PORT_PROVIDER_MAP` → 对应本地提供方;
    - 端口未知时取条目 ``provider`` 字段,仅当其为白名单形态
      (如 ``"lmstudio"``)才采用——"openai 兼容" 这类描述性文案
      不是合法 spec 提供方,拒绝;
    - 两者皆无 → None(调用方跳过该条目)。
    """
    port = _port_from_base_url(entry.get("base_url"))
    if port is not None and port in PORT_PROVIDER_MAP:
        return PORT_PROVIDER_MAP[port]
    provider = str(entry.get("provider") or "").strip().casefold()
    if provider and _PROVIDER_SLUG_RE.match(provider):
        return provider
    return None


def _first_local_vision_spec(scanner_results: list[Any]) -> str | None:
    """在扫描结果(按传入顺序)中找**第一个**可启用的本地视觉模型 spec。

    - 只看 ``ok`` 为真的字典条目(服务未就绪的不算);
    - 视觉过滤委托 A150;A150 缺席时"缺席全返回"(任意非空模型名
      都算候选,宁可不误扰用户);
    - 提供方按 :func:`_provider_for_entry` 还原;无法还原的条目跳过;
    - 命中即返回 ``"ollama:llava:13b"`` 形态的 spec(模型名内含冒号
      不受影响,providers.parse_spec 按第一个冒号拆分)。
    """
    is_vision = _load_vision_predicate()
    if is_vision is None:
        logger.debug("A150 capability 未就位,本地模型不做视觉过滤(缺席全返回)")
    for entry in scanner_results or []:
        if not isinstance(entry, dict) or not entry.get("ok"):
            continue
        names = _model_names(entry.get("models"))
        candidates = [n for n in names if is_vision(n)] if is_vision else names
        if not candidates:
            continue
        provider = _provider_for_entry(entry)
        if provider is None:
            logger.debug(
                "扫描条目无法还原 spec 提供方(端口未知且 provider 非白名单),跳过:%r",
                entry.get("base_url"),
            )
            continue
        return f"{provider}:{candidates[0]}"
    return None


def _keys_configured(cfg: Config) -> dict[str, bool]:
    """缺省密钥体检:惰性调 ``security.keys.configured``;异常按未配置降级。"""
    try:
        result = keys.configured(cfg)
    except Exception as exc:  # noqa: BLE001 - 体检失败不拖累推荐路由
        logger.warning("云密钥配置体检失败(按未配置处理):%s", exc)
        return {}
    return result if isinstance(result, dict) else {}


def recommend(
    scanner_results: list[dict[str, Any]],
    cfg: Config,
    *,
    keys_configured: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """按 **本地优先 → 云 → 向导** 三级推荐下一步动作(纯决策,零副作用)。

    - **本地**:`scanner_results`(A143 ``scan()`` 结果,仅回环产出)任一
      ``ok`` 条目含视觉模型 → ``{"action": "activate_local",
      "spec": "ollama:首个视觉模型", "state": LOCAL_CONNECTED}``;
      spec 提供方按端口映射还原(:data:`PORT_PROVIDER_MAP`);
    - **云**:无本地但 ``keys_configured`` 任一 True(缺省惰性
      ``keys.configured(cfg)``,只看布尔绝不回显密钥,红线 33)→
      ``{"action": "use_cloud", "state": CLOUD_CONNECTED}``
      (云端仍受 vlm_online + 预算约束,红线 34,由下游照旧执行);
    - **向导**:全无 → ``{"action": "wizard", "state": NO_MODEL}``
      (具体连哪家 / 是否退离线桩由用户在向导页决定)。

    返回 dict 的 ``state`` 为 :class:`SetupState` 成员(str Enum,可直接
    序列化);仅本地分支带 ``spec`` 键。
    """
    spec = _first_local_vision_spec(scanner_results)
    if spec is not None:
        return {
            "action": "activate_local",
            "spec": spec,
            "state": SetupState.LOCAL_CONNECTED,
        }
    configured = keys_configured if keys_configured is not None else _keys_configured(cfg)
    if any(bool(v) for v in (configured or {}).values()):
        return {"action": "use_cloud", "state": SetupState.CLOUD_CONNECTED}
    return {"action": "wizard", "state": SetupState.NO_MODEL}


# ---------------------------------------------------------------------------
# persist / load:状态持久化(损坏回退 no_model)
# ---------------------------------------------------------------------------

#: 状态文件里存放状态值的键名
_STATE_KEY = "state"

#: persist 成功的遥测计数名
STATE_PERSISTED_METRIC = "setup.state_persisted"

#: load 遇到损坏/未知内容回退 no_model 的遥测计数名
STATE_FALLBACK_METRIC = "setup.state_fallback"


def persist(path: str | Path, state: SetupState | str) -> Path:
    """把向导流程状态写入 JSON 文件(utf-8),返回落盘路径。

    - 文件形态:``{"state": "local_connected", "updated_at": "..."}``
      (时间戳为 UTC ISO-8601,便于排障,不含任何敏感信息);
    - ``state`` 可传成员或值/名字符串,无法识别抛中文 ``ValueError``;
    - 父目录不存在时自动逐级创建;写失败抛中文 ``ValueError``(不静默);
    - 成功记 ``telemetry.inc("setup.state_persisted")``。
    """
    member = _coerce_state(state)
    target = Path(path)
    payload = {
        _STATE_KEY: member.value,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise ValueError(f"写入向导流程状态文件 {target} 失败:{exc}") from exc
    telemetry.inc(STATE_PERSISTED_METRIC)
    logger.info("向导流程状态已落盘:%s → %s", target, member.value)
    return target


def load(path: str | Path) -> SetupState:
    """读取向导流程状态;**任何异常情形都安全回退 ``NO_MODEL``,绝不抛出**。

    - 文件缺席 → ``NO_MODEL``(全新安装的缺省态,debug 日志);
    - 读失败 / JSON 损坏 / 非 dict / 状态值未知 → 记 WARNING 并回退
      ``NO_MODEL``,同时记 ``telemetry.inc("setup.state_fallback")``
      (损坏自愈:下次 persist 即重建合法文件)。
    """
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.debug("向导流程状态文件不存在(视为全新安装,no_model):%s", source)
        return SetupState.NO_MODEL
    except (OSError, ValueError) as exc:
        logger.warning("向导流程状态文件 %s 损坏(回退 no_model):%s", source, exc)
        telemetry.inc(STATE_FALLBACK_METRIC)
        return SetupState.NO_MODEL

    value = data.get(_STATE_KEY) if isinstance(data, dict) else None
    member = _coerce_state_or_none(value)
    if member is None:
        logger.warning(
            "向导流程状态文件 %s 内容无法识别(state=%r),回退 no_model", source, value
        )
        telemetry.inc(STATE_FALLBACK_METRIC)
        return SetupState.NO_MODEL
    return member
