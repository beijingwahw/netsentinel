# -*- coding: utf-8 -*-
"""全平台视觉模型提供方目录(NetSentinel V4 · A61,V4 的地基模块)。

与 CONTRACTS-V4 §2 / §4 A61 逐条一致:

- :class:`ProviderSpec`:单个提供方的目录条目(端点 / 方言 / 默认模型 / 密钥环境
  变量 / 是否本地 / 中文备注);
- :data:`PROVIDERS`:权威清单,覆盖 §2 表全部 **20 个**提供方,值逐项照抄契约表;
- :func:`parse_spec`:解析 ``提供方:模型`` 语法(如 ``openai:gpt-4o-mini``),
  无冒号时模型为 ``None``,未知提供方抛 ValueError(中文,列出全部可用项);
- :func:`resolve`:按三级优先级(cfg.vlm_provider_models / base_urls / api_keys
  覆盖 → 目录默认)解析出 :class:`ResolvedProvider`;密钥解析惰性委托
  ``netsentinel.security.keys.get_key``(A70),未就位时按 ``key_envs`` 顺序读
  环境变量,再退化到空串;本地提供方免密钥但必须显式指定模型;
- :func:`is_local` / :func:`provider_names`:便捷查询。

安全与红线:

- **本模块是纯本地数据结构,零网络**(V4 红线 16/20);
- 目录里的模型名与端点均为**提示值,以各平台官方文档为准,上线前须核验一次**
  (V4 红线 18),全部可用 ``vlm_provider_models`` / ``vlm_provider_base_urls``
  覆盖;
- **密钥绝不入日志 / 异常消息**(V4 红线 17):``ResolvedProvider`` 的 repr 只显示
  "已配置 / 未配置",日志只记录提供方名、模型与布尔状态;
- 循环导入防护:**顶层不 import 任何 vision/security 兄弟模块**,
  ``security.keys`` 一律惰性导入(A70 允许缺席)。

V5 升级(A89 · 统一传输组,CONTRACTS-V5 §1/§4):

- **性能**::func:`resolve` 的热点缓存——``(提供方, base_url 覆盖, 模型覆盖)``
  → ``(base_url, model, style, local, key_envs)`` 进程内 LRU(≤ :data:`RESOLVE_CACHE_MAX`
  项);**api_key 绝不缓存**,每次现取 ``security.keys.get_key`` 并照常回填到
  :class:`ResolvedProvider`;缓存键已包含全部 cfg 覆盖项,覆盖变化自然落到新键,
  无需手工失效;解析失败(ValueError)不写入缓存,错误语义与 V4 完全一致;
- **可观测性**:``providers.resolve``(总次数)/ ``providers.resolve_cached``
  (缓存命中)/ ``providers.errors``(解析失败)三枚计数器接入 ``telemetry``。

用法示例::

    from netsentinel.vision import providers

    resolved = providers.resolve("qwen:qwen-vl-max", cfg)   # 首次解析并入缓存
    again = providers.resolve("qwen:qwen-vl-max", cfg)       # 命中缓存,密钥仍现取
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from netsentinel import telemetry

if TYPE_CHECKING:  # 仅类型标注用,运行时零依赖(避免任何循环导入可能)
    from netsentinel.contracts import Config

__all__ = [
    "STYLES",
    "ProviderSpec",
    "ResolvedProvider",
    "PROVIDERS",
    "LOCAL_PROVIDERS",
    "parse_spec",
    "resolve",
    "is_local",
    "provider_names",
]

logger = logging.getLogger(__name__)

#: 三种 API 方言(契约 §2):openai / anthropic / gemini
STYLES: tuple[str, ...] = ("openai", "anthropic", "gemini")

#: (V5)resolve 热点缓存容量上限(LRU,进程内):20 个提供方 × 常见覆盖组合足够
RESOLVE_CACHE_MAX = 64


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class ProviderSpec:
    """提供方目录条目(值均为提示信息,以官方文档为准,上线前核验一次)。

    字段:

    - ``key``:提供方注册名(即 ``PROVIDERS`` 的键,如 ``"openai"``);
    - ``base_url``:API 根端点(提示值,可用 ``cfg.vlm_provider_base_urls`` 覆盖);
    - ``style``:API 方言,``openai | anthropic | gemini``;
    - ``default_model``:默认视觉模型名(提示值,可用 ``cfg.vlm_provider_models``
      覆盖;本地提供方可为空 = 必须显式指定模型);
    - ``key_envs``:密钥环境变量候选(按顺序逐个尝试,首个非空生效);
    - ``local``:是否本地推理(免 ``vlm_online`` 闸门、免密钥,数据不出本机);
    - ``notes``:中文一句话特点备注。
    """

    key: str
    base_url: str
    style: str
    default_model: str
    key_envs: list[str] = field(default_factory=list)
    local: bool = False
    notes: str = ""


@dataclass
class ResolvedProvider:
    """一次解析的最终结果:交给 A62 ``UniversalVLMClient`` 构造。

    字段优先级见 :func:`resolve`;``api_key`` 为空串表示未配置(是否拒绝外呼由
    传输层按红线 16 判定,本模块只做解析、不外呼)。
    """

    provider: str
    base_url: str
    model: str
    api_key: str
    style: str
    local: bool = False

    def __repr__(self) -> str:  # noqa: D105 - 红线 17:repr 不得回显密钥
        key_state = "已配置" if self.api_key else ("免密钥" if self.local else "未配置")
        return (
            f"ResolvedProvider(provider={self.provider!r}, base_url={self.base_url!r}, "
            f"model={self.model!r}, style={self.style!r}, local={self.local!r}, "
            f"api_key=<{key_state}>)"
        )

    __str__ = __repr__


# ---------------------------------------------------------------------------
# 权威提供方目录(契约 §2 全部 20 项,值逐项照抄契约表)
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, ProviderSpec] = {
    "glm": ProviderSpec(
        key="glm",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        style="openai",
        default_model="glm-5.3-flash",
        key_envs=["NETSENTINEL_GLM_API_KEY", "GLM_API_KEY"],
        notes="智谱 GLM 视觉模型,OpenAI 兼容口,项目默认提供方,flash 档性价比高。",
    ),
    "openai": ProviderSpec(
        key="openai",
        base_url="https://api.openai.com/v1",
        style="openai",
        default_model="gpt-4o-mini",
        key_envs=["NETSENTINEL_OPENAI_API_KEY", "OPENAI_API_KEY"],
        notes="OpenAI GPT-4o 系列,生态最成熟,分钟级限速需留意。",
    ),
    "anthropic": ProviderSpec(
        key="anthropic",
        base_url="https://api.anthropic.com/v1",
        style="anthropic",
        default_model="claude-sonnet-4",
        key_envs=["NETSENTINEL_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"],
        notes="Anthropic Claude,anthropic 方言(system 顶层、max_tokens 必填),模型名以官方为准。",
    ),
    "gemini": ProviderSpec(
        key="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        style="gemini",
        default_model="gemini-2.0-flash",
        key_envs=["NETSENTINEL_GEMINI_API_KEY", "GEMINI_API_KEY"],
        notes="Google Gemini,gemini 方言(x-goog-api-key 标头、inline_data 传图)。",
    ),
    "qwen": ProviderSpec(
        key="qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        style="openai",
        default_model="qwen-vl-max",
        key_envs=["NETSENTINEL_QWEN_API_KEY", "DASHSCOPE_API_KEY"],
        notes="阿里通义千问 VL,DashScope OpenAI 兼容模式,国内访问稳定。",
    ),
    "doubao": ProviderSpec(
        key="doubao",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        style="openai",
        default_model="doubao-1.5-vision-pro",
        key_envs=["NETSENTINEL_DOUBAO_API_KEY", "ARK_API_KEY"],
        notes="字节豆包视觉模型(火山方舟),也可填推理接入点 ID,模型名以控制台为准。",
    ),
    "hunyuan": ProviderSpec(
        key="hunyuan",
        base_url="https://api.hunyuan.cloud.tencent.com/v1",
        style="openai",
        default_model="hunyuan-vision",
        key_envs=["NETSENTINEL_HUNYUAN_API_KEY"],
        notes="腾讯混元视觉模型,OpenAI 兼容口,仅专用环境变量名。",
    ),
    "moonshot": ProviderSpec(
        key="moonshot",
        base_url="https://api.moonshot.cn/v1",
        style="openai",
        default_model="kimi-latest",
        key_envs=["NETSENTINEL_MOONSHOT_API_KEY", "MOONSHOT_API_KEY"],
        notes="月之暗面 Kimi 视觉模型,长上下文强,长文输出建议显式 max_tokens。",
    ),
    "minimax": ProviderSpec(
        key="minimax",
        base_url="https://api.minimax.chat/v1",
        style="openai",
        default_model="MiniMax-VL-01",
        key_envs=["NETSENTINEL_MINIMAX_API_KEY"],
        notes="MiniMax 视觉模型,OpenAI 兼容口,模型名以官方文档为准。",
    ),
    "stepfun": ProviderSpec(
        key="stepfun",
        base_url="https://api.stepfun.com/v1",
        style="openai",
        default_model="step-1v-8k",
        key_envs=["NETSENTINEL_STEPFUN_API_KEY"],
        notes="阶跃星辰 Step-1V 多模态模型,国内直连。",
    ),
    "siliconflow": ProviderSpec(
        key="siliconflow",
        base_url="https://api.siliconflow.cn/v1",
        style="openai",
        default_model="Qwen/Qwen2.5-VL-7B-Instruct",
        key_envs=["NETSENTINEL_SILICONFLOW_API_KEY"],
        notes="硅基流动聚合平台,一个密钥调用多家开源视觉模型(模型名带命名空间)。",
    ),
    "ernie": ProviderSpec(
        key="ernie",
        base_url="https://qianfan.baidubce.com/v2",
        style="openai",
        default_model="ernie-4.5-vl",
        key_envs=["NETSENTINEL_ERNIE_API_KEY", "QIANFAN_API_KEY"],
        notes="百度文心 ERNIE VL(千帆 v2 OpenAI 兼容口),模型名以官方为准。",
    ),
    "openrouter": ProviderSpec(
        key="openrouter",
        base_url="https://openrouter.ai/api/v1",
        style="openai",
        default_model="qwen/qwen2.5-vl-72b-instruct:free",
        key_envs=["NETSENTINEL_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"],
        notes="OpenRouter 聚合入口,免配置换平台,建议附加 HTTP-Referer/X-Title 头。",
    ),
    "groq": ProviderSpec(
        key="groq",
        base_url="https://api.groq.com/openai/v1",
        style="openai",
        default_model="meta-llama/llama-4-scout-17b-16e-instruct",
        key_envs=["NETSENTINEL_GROQ_API_KEY", "GROQ_API_KEY"],
        notes="Groq 超低延迟推理,Llama 4 Scout 视觉,速率配额较小。",
    ),
    "together": ProviderSpec(
        key="together",
        base_url="https://api.together.xyz/v1",
        style="openai",
        default_model="meta-llama/Llama-4-Scout-17B-16E-Instruct",
        key_envs=["NETSENTINEL_TOGETHER_API_KEY", "TOGETHER_API_KEY"],
        notes="Together AI 开源模型托管,OpenAI 兼容口,按量计费。",
    ),
    "xai": ProviderSpec(
        key="xai",
        base_url="https://api.x.ai/v1",
        style="openai",
        default_model="grok-2-vision-1212",
        key_envs=["NETSENTINEL_XAI_API_KEY", "XAI_API_KEY"],
        notes="xAI Grok 视觉模型,OpenAI 兼容口,模型名以官方为准。",
    ),
    "ollama": ProviderSpec(
        key="ollama",
        base_url="http://127.0.0.1:11434/v1",
        style="openai",
        default_model="llava",
        key_envs=[],
        local=True,
        notes="本地 Ollama,数据不出本机,免 vlm_online 闸门与密钥,默认模型 llava。",
    ),
    "vllm": ProviderSpec(
        key="vllm",
        base_url="http://127.0.0.1:8000/v1",
        style="openai",
        default_model="",
        key_envs=[],
        local=True,
        notes="本地 vLLM 推理服务(OpenAI 兼容口),模型随启动参数而定,必须显式指定。",
    ),
    "lmstudio": ProviderSpec(
        key="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        style="openai",
        default_model="",
        key_envs=[],
        local=True,
        notes="本地 LM Studio 桌面端本地服务,模型由用户手动加载,必须显式指定。",
    ),
    "xinference": ProviderSpec(
        key="xinference",
        base_url="http://127.0.0.1:9997/v1",
        style="openai",
        default_model="",
        key_envs=[],
        local=True,
        notes="本地 Xinference 推理框架,模型自行部署,必须显式指定。",
    ),
}

#: 本地提供方(免 vlm_online / 免密钥,但仍受预算红线 19 约束)
LOCAL_PROVIDERS: tuple[str, ...] = tuple(name for name, spec in PROVIDERS.items() if spec.local)


# ---------------------------------------------------------------------------
# 语法解析:提供方[:模型]
# ---------------------------------------------------------------------------


def _unknown_provider_error(name: str) -> ValueError:
    """构造"未知提供方"中文错误(列出全部可用项,绝不含密钥信息)。"""
    return ValueError(
        f"未知视觉模型提供方:{name or '(空)'}。"
        f"可用提供方({'、'.join(provider_names())})。"
        "写法:classifier: 提供方 或 提供方:模型,如 anthropic、qwen:qwen-vl-max、ollama:llava;"
        "模型名以各平台官方文档为准。"
    )


def parse_spec(name: str) -> tuple[str, str | None]:
    """解析 ``提供方:模型`` 语法,返回 ``(提供方, 模型或 None)``。

    - 按第一个冒号拆分:``"openai:gpt-4o-mini" -> ("openai", "gpt-4o-mini")``;
      模型名内部再含冒号(如 openrouter 的 ``qwen/...:free``)不受影响;
    - 无冒号(如 ``"anthropic"``)→ ``(name, None)``,模型留给 resolve 按目录/配置解析;
    - 冒号后为空(如 ``"ollama:"``)视为未指定模型;
    - 未知提供方 → ValueError(中文,列出全部可用项)。
    """
    text = str(name or "").strip()
    if ":" in text:
        provider, _, model = text.partition(":")
        provider = provider.strip()
        model = model.strip()
    else:
        provider, model = text, ""
    if provider not in PROVIDERS:
        raise _unknown_provider_error(provider)
    return provider, (model or None)


# ---------------------------------------------------------------------------
# 密钥解析(惰性委托 A70,未就位时环境变量兜底)
# ---------------------------------------------------------------------------


def _load_get_key() -> Callable[[str, "Config"], str] | None:
    """惰性导入 ``netsentinel.security.keys.get_key``;未就位返回 ``None``。

    顶层绝不 import 兄弟模块(防循环导入);并行开发期 A70 缺席或暂不可导入
    (语法错误未修完等)都降级为"未就位",由环境变量兜底,不硬依赖。
    """
    try:
        from netsentinel.security import keys  # noqa: PLC0415 - 契约要求惰性导入
    except Exception as exc:  # noqa: BLE001 - ImportError / 并行期 SyntaxError 等一律降级
        logger.debug("security.keys 暂不可用,提供方密钥按 key_envs 环境变量兜底:%s", exc)
        return None
    fn = getattr(keys, "get_key", None)
    return fn if callable(fn) else None


def _cfg_str_map(cfg: "Config | None", attr: str) -> dict[str, Any]:
    """安全读取 Config 上的某个 dict 字段(缺省/非 dict 一律视为空映射)。"""
    if cfg is None:
        return {}
    value = getattr(cfg, attr, None)
    return value if isinstance(value, dict) else {}


def _fallback_api_key(
    provider: str,
    key_envs: tuple[str, ...],
    cfg: "Config | None",
) -> str:
    """A70 未就位时的兜底:cfg.vlm_api_keys → key_envs 逐个环境变量 → 空串。

    (V5)``key_envs`` 直接来自 :func:`_resolve_static` 的缓存值,缓存命中路径
    无需再查目录条目——密钥本身仍然每次现取,绝不进缓存。
    """
    raw = _cfg_str_map(cfg, "vlm_api_keys").get(provider)
    cfg_key = raw.strip() if isinstance(raw, str) else ""
    if cfg_key:
        logger.debug("提供方 %s 密钥来源:配置项 vlm_api_keys(兜底路径)", provider)
        return cfg_key
    for env_name in key_envs:
        env_key = (os.environ.get(env_name) or "").strip()
        if env_key:
            logger.debug("提供方 %s 密钥来源:环境变量 %s(兜底路径)", provider, env_name)
            return env_key
    return ""


def _resolve_api_key(
    provider: str,
    key_envs: tuple[str, ...],
    cfg: "Config | None",
) -> str:
    """解析云端提供方密钥:优先惰性委托 ``security.keys.get_key``(A70)。

    每次调用都现取(V5:密钥绝不缓存),让密钥轮换 / 环境变量变化即时生效。
    """
    get_key = _load_get_key()
    if get_key is not None:
        try:
            return str(get_key(provider, cfg) or "")
        except Exception as exc:  # noqa: BLE001 - A70 接口异常时降级兜底,不硬依赖
            logger.debug("security.keys.get_key 调用异常,改用环境变量兜底:%s", exc)
    return _fallback_api_key(provider, key_envs, cfg)


# ---------------------------------------------------------------------------
# resolve:三级优先级解析(V5:静态部分走热点 LRU 缓存,密钥现取)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=RESOLVE_CACHE_MAX)
def _resolve_static(
    provider: str,
    base_url_override: str,
    model_input: str,
) -> tuple[str, str, str, bool, tuple[str, ...]]:
    """解析与密钥无关的静态部分,返回 ``(base_url, model, style, local, key_envs)``。

    (V5 · A89)进程内 LRU 热点缓存(≤ :data:`RESOLVE_CACHE_MAX` 项):

    - 缓存键 ``(provider, base_url_override, model_input)`` 已包含全部 cfg 覆盖
      项的相关切片——覆盖一变即落入新键,**天然正确失效**,无需手工清缓存;
    - **缓存值绝不包含 api_key**:密钥由 :func:`resolve` 每次现取
      ``security.keys.get_key`` 并回填到 :class:`ResolvedProvider`;
    - 解析失败(本地提供方缺模型)抛 ValueError——``lru_cache`` 不缓存异常,
      下次调用重新求值,错误语义与 V4 完全一致;
    - 线程安全由 ``functools.lru_cache`` 内部保证。

    ``model_input`` 为"模型覆盖链的最高一级非空值"(完整写法的当场模型 >
    cfg.vlm_provider_models 覆盖 > 空串 = 用目录默认)。
    """
    spec = PROVIDERS[provider]
    base_url = (base_url_override or "").strip() or spec.base_url
    model = model_input or spec.default_model
    if spec.local and not model:
        raise ValueError(
            f"本地提供方必须指定模型,如 ollama:llava;"
            f"当前 {provider} 未给出模型且目录无默认值"
            f"(可用 classifier: {provider}:<模型名> 或 cfg.vlm_provider_models 指定,"
            "模型名以本地服务实际加载为准)。"
        )
    return base_url.rstrip("/"), model, spec.style, spec.local, tuple(spec.key_envs)


def resolve(provider: str, cfg: "Config | None" = None) -> ResolvedProvider:
    """把提供方名(或 ``提供方:模型`` 写法)解析成 :class:`ResolvedProvider`。

    优先级(高 → 低):

    1. ``cfg.vlm_provider_models[provider]`` / ``cfg.vlm_provider_base_urls[provider]``
       / ``cfg.vlm_api_keys[provider]``(配置覆盖;若传入的是 ``提供方:模型``
       完整写法,其中的模型视为更明确的当场选择,优先于配置覆盖);
    2. 目录 :data:`PROVIDERS` 的默认模型 / 端点;
    3. 密钥:惰性委托 ``security.keys.get_key``(A70,内部再按 vlm_api_keys →
       key_envs 环境变量 → 密钥文件);A70 未就位时按 ``key_envs`` 顺序读
       ``os.environ``,再退化到空串。

    规则:

    - 未知提供方 → ValueError(中文,列出全部可用项);
    - 本地提供方(local=True):免密钥(``api_key=""``);模型为空且目录也无默认
      → ValueError(中文,"本地提供方必须指定模型,如 ollama:llava");
    - 云端提供方密钥缺失**不在此报错**(是否拒发由传输层按红线 16 判定),
      仅以空串返回。

    (V5)静态部分(base_url/model/style/local/key_envs)命中 :func:`_resolve_static`
    的进程内 LRU 时直接复用并计 ``providers.resolve_cached``;**api_key 每次现取,
    绝不缓存**。本模块纯本地数据结构,零网络;日志只记录提供方名 / 模型 /
    布尔状态,绝不记录密钥(红线 17)。
    """
    telemetry.inc("providers.resolve")
    provider = str(provider or "").strip()
    model_override: str | None = None
    if ":" in provider:
        provider, model_override = parse_spec(provider)
    if provider not in PROVIDERS:
        telemetry.inc("providers.errors")
        raise _unknown_provider_error(provider)

    raw_base = _cfg_str_map(cfg, "vlm_provider_base_urls").get(provider)
    base_override = raw_base.strip() if isinstance(raw_base, str) else ""
    raw_model = _cfg_str_map(cfg, "vlm_provider_models").get(provider)
    cfg_model = raw_model.strip() if isinstance(raw_model, str) else ""

    hits_before = _resolve_static.cache_info().hits
    try:
        base_url, model, style, local, key_envs = _resolve_static(
            provider, base_override, model_override or cfg_model
        )
    except ValueError:
        telemetry.inc("providers.errors")
        raise
    if _resolve_static.cache_info().hits > hits_before:
        telemetry.inc("providers.resolve_cached")

    # 2) 密钥:本地免密钥;云端每次现取(绝不进缓存)
    if local:
        api_key = ""
    else:
        api_key = _resolve_api_key(provider, key_envs, cfg)

    resolved = ResolvedProvider(
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=api_key,
        style=style,
        local=local,
    )
    logger.debug(
        "提供方已解析:provider=%s model=%s style=%s local=%s 密钥=%s 端点已确定",
        provider,
        model,
        style,
        local,
        "免密钥" if local else ("已配置" if api_key else "未配置"),
    )
    return resolved


# ---------------------------------------------------------------------------
# 便捷查询
# ---------------------------------------------------------------------------


def is_local(provider: str) -> bool:
    """判断提供方是否本地推理(ollama / vllm / lmstudio / xinference)。

    未知提供方一律返回 ``False``(不抛错,便于诊断类调用方使用)。
    """
    spec = PROVIDERS.get(str(provider or "").strip())
    return bool(spec and spec.local)


def provider_names() -> list[str]:
    """全部可用提供方名(按目录顺序,共 20 项)。"""
    return list(PROVIDERS)
