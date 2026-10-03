# -*- coding: utf-8 -*-
"""模型名协商回退(NetSentinel V4 · A71)。

平台视觉模型名迭代极快:请求报"模型不存在"(:class:`vlm_client.ModelNotFoundError`)
时,本模块按**候选链**自动换名重试,把一次失败请求升级为
"当前名 → 配置覆盖 → 目录默认 → 目录低价推荐 → 平台别名"的逐跳探测,
成功即**进程内定格**(:data:`_NEGOTIATED`,V5 起读写持 :data:`_NEGOTIATED_LOCK`,
同键并发首调经 :data:`_INFLIGHT` 合流为一轮探测),此后同一 ``提供方:模型``
直接返回定格结果——零额外外呼、零额外记账。

候选链(:func:`candidate_chain`,去重保序,五路来源):

1. 当前请求的 ``model``(首选永远是"用户想要的名字");
2. ``cfg.vlm_provider_models[provider]``(运营者显式覆盖);
3. 提供方目录(A61 ``providers.PROVIDERS``)的 ``default_model``;
4. 模型目录(A65 ``model_catalog.suggest(provider, "cheap")``)低价档推荐;
5. 平台特化别名(A64 ``provider_quirks.QUIRKS[provider]["model_aliases"]`` 的键,
   如豆包 ``ep-`` 推理接入点说明条目)。说明键里的**文档占位符形态**(含
   ``<`` ``>`` ``{`` ``}``,如 ``ep-<接入点ID>``、``{组织名}/{模型名}``)不是
   可调用的模型名,一律排除,避免浪费真实外呼与预算。

空串与重复候选剔除;五路全部落空时按契约原样返回 ``[model]``。

安全红线(V4 §0 红线 19/20,叠加既有红线):

- **协商的每一跳都是真实外呼**:每次候选尝试前必须 ``spend_one`` 记账
  (``spend`` 注入,缺省惰性 ``vlm_cache.VlmCache(cfg.vlm_cache_db,
  cfg.vlm_daily_budget).spend_one``);预算用尽 → RuntimeError(中文),
  绝不静默超支;本地提供方(ollama 等)照常记账(成本是本地算力,一本账);
- ``vlm_cache`` 未就位且未注入 ``spend`` → **直接拒绝外呼**(没有账本,
  一次真实请求都不允许发射);
- 只换名、不修配置:``VlmConfigError``(vlm_online 未开 / 缺密钥)、HTTP 500
  等其他异常一律原样上抛——配置错换名也救不回;
- 全链 ``ModelNotFoundError`` → RuntimeError(中文)列出全部试过的候选;
- 密钥绝不入日志 / 异常消息(红线 17,传输层 A62 已打码,本模块只记录
  提供方与模型名);
- 测试零外呼:``transport`` 全程注入。

兄弟模块(providers / vlm_client / model_catalog / provider_quirks / vlm_cache)
一律惰性导入,且**优先读 ``sys.modules``**:A63 经验表明,测试对兄弟模块打桩时
必须同时改 ``sys.modules`` 与父包属性(from-import 会绕过 ``sys.modules``
单独打桩);``sys.modules[模块名]`` 被显式置 ``None`` 即视为"未就位"。
"""
from __future__ import annotations

import copy
import dataclasses
import importlib
import logging
import sys
import threading
from collections.abc import Callable
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config

__all__ = ["candidate_chain", "negotiate"]

logger = logging.getLogger(__name__)

#: 传输层类型(与 A62 ``vlm_client.Transport`` 同形):
#: ``(url, headers, payload, timeout) -> (HTTP 状态码, 响应体文本)``
Transport = Callable[[str, dict[str, str], dict[str, Any], float], tuple[int, str]]

#: 预算闸门类型:零参调用;预算尽抛 RuntimeError(A23 ``VlmBudgetExceeded`` 语义)
SpendFn = Callable[[], None]

#: 兄弟模块全名(惰性导入用)
_PROVIDERS_MODULE = "netsentinel.vision.providers"
_VLM_CLIENT_MODULE = "netsentinel.vision.vlm_client"
_VLM_CACHE_MODULE = "netsentinel.vision.vlm_cache"
_MODEL_CATALOG_MODULE = "netsentinel.vision.model_catalog"
_PROVIDER_QUIRKS_MODULE = "netsentinel.vision.provider_quirks"

#: 协商探测用的极小文本消息:不发图、不带任何站点数据,最小化 token 与内容暴露
_PROBE_MESSAGE: list[dict[str, str]] = [{"role": "user", "content": "ping"}]

#: 兄弟模块 vlm_cache 未就位时的内部缺省(与 contracts.Config 字段缺省一致)
_DEFAULT_CACHE_DB: str = "data/vlm_cache.db"
_DEFAULT_DAILY_BUDGET: int = 200

#: quirks ``model_aliases`` 说明键中的占位符字符:出现任一即视为文档占位
#: (如 ``ep-<接入点ID>`` / ``{组织名}/{模型名}``),不是可调用的模型名
_PLACEHOLDER_CHARS: tuple[str, ...] = ("<", ">", "{", "}")

#: 进程内协商定格缓存:键 ``f"{provider}:{model}"``(协商前的原始写法)→ 成功模型名。
#: 提供方名取自目录(不含冒号),模型名内部可含冒号(openrouter 的 ``:free`` 等),
#: 故按首个冒号拼接不产生歧义;仅在探测成功后写入,永不失败定格。
#: V5:读写一律持 :data:`_NEGOTIATED_LOCK`(并发首调合流,见 :data:`_INFLIGHT`)。
_NEGOTIATED: dict[str, str] = {}

#: 定格缓存的互斥锁:读(命中检查)/写(定格)/合流表维护共用一把
#: (临界区均为纯内存字典操作,微秒级,不包住任何网络探测)。
_NEGOTIATED_LOCK = threading.Lock()

#: 同键并发首调合流表:键 → 先行线程注册的 :class:`threading.Event`。
#:
#: 首个到达的线程成为"探测方"逐候选外呼,其余线程在 Event 上等待并直接
#: 复用定格结果——同一 ``提供方:模型`` 的并发首调**只探测一轮、只记一轮账**
#: (红线 19:绝不因线程竞争而双倍外呼/双倍记账)。先行线程失败(异常上抛)
#: 时等待方超时或被唤醒后仍无定格,则自行探测(退化路径,语义不变)。
_INFLIGHT: dict[str, threading.Event] = {}

#: 等待先行线程定格的上限秒数:超时(先行线程异常/卡死)则自行探测;
#: 真实探测的单跳超时由 A62 传输层约束,故该值只需大于一轮完整链路的耗时。
_INFLIGHT_WAIT_TIMEOUT_S: float = 120.0


# ---------------------------------------------------------------------------
# 兄弟模块惰性加载(A63 隔离经验:优先读 sys.modules)
# ---------------------------------------------------------------------------


def _load_sibling(module_name: str) -> Any:
    """惰性加载兄弟模块;**优先读 ``sys.modules``**,未收录再 import。

    - ``sys.modules[module_name]`` 已收录 → 原样返回(测试注入的替身直接生效;
      被显式置 ``None`` 则返回 ``None``,即"未就位");
    - 未收录 → ``importlib.import_module``;ImportError / 并行期语法错误等
      一律降级为 ``None``,不硬依赖任何兄弟模块。
    """
    if module_name in sys.modules:
        return sys.modules[module_name]  # None = 测试显式标记未就位
    try:
        return importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - ImportError / 并行开发期 SyntaxError 等
        logger.debug("兄弟模块 %s 暂不可用:%s", module_name, exc)
        return None


def _is_concrete_model_name(name: str) -> bool:
    """是否为具体可调用的模型名:非空且不含文档占位符字符。"""
    cleaned = name.strip()
    return bool(cleaned) and not any(ch in cleaned for ch in _PLACEHOLDER_CHARS)


# ---------------------------------------------------------------------------
# 候选链
# ---------------------------------------------------------------------------


def candidate_chain(provider: str, model: str, cfg: Config | None = None) -> list[str]:
    """构造模型名协商候选链(去重保序;纯本地计算,零外呼、零记账)。

    五路来源按序并入(详见模块 docstring):当前模型 → ``cfg.vlm_provider_models``
    覆盖 → providers 目录 ``default_model`` → ``model_catalog.suggest(provider,
    "cheap")`` → ``provider_quirks`` 的 ``model_aliases`` 具体键。

    规则:

    - 空串与非字符串一律剔除;重复(大小写敏感的精确比较)只保留首个;
    - 别名键须通过占位符过滤(``ep-<接入点ID>`` 这类说明形态不进链);
    - 任一来源的兄弟模块未就位 / 读取异常 → 仅少一路候选,不报错;
    - 五路全部落空 → 按契约原样返回 ``[model]``(即使 ``model`` 为空串)。
    """
    provider = str(provider or "").strip()
    chain: list[str] = []

    def _add(raw: Any) -> None:
        if not isinstance(raw, str):
            return
        name = raw.strip()
        if name and name not in chain:
            chain.append(name)

    # 1) 当前请求模型(协商起点:能不换就不换)
    _add(model)

    # 2) cfg.vlm_provider_models[provider](运营者显式覆盖,最可靠的"正确答案")
    override_map = getattr(cfg, "vlm_provider_models", None)
    if isinstance(override_map, dict):
        _add(override_map.get(provider))

    # 3) 提供方目录 default_model(A61)
    providers_mod = _load_sibling(_PROVIDERS_MODULE)
    if providers_mod is not None:
        try:
            spec = providers_mod.PROVIDERS.get(provider)
            if spec is not None:
                _add(getattr(spec, "default_model", ""))
        except Exception as exc:  # noqa: BLE001 - 目录异常仅少一路候选,不阻塞协商
            logger.debug("读取提供方目录默认模型失败(provider=%s):%s", provider, exc)

    # 4) 模型目录低价档推荐(A65:cheap → balanced 回退链)
    catalog = _load_sibling(_MODEL_CATALOG_MODULE)
    if catalog is not None:
        try:
            _add(catalog.suggest(provider, "cheap"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("model_catalog.suggest 失败(provider=%s):%s", provider, exc)

    # 5) 平台别名表(A64 model_aliases;文档占位键排除)
    quirks = _load_sibling(_PROVIDER_QUIRKS_MODULE)
    if quirks is not None:
        try:
            quirk = quirks.QUIRKS.get(provider)
            aliases = quirk.get("model_aliases") if isinstance(quirk, dict) else None
            if isinstance(aliases, dict):
                for alias in aliases:
                    if isinstance(alias, str) and _is_concrete_model_name(alias):
                        _add(alias)
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取 provider_quirks 别名表失败(provider=%s):%s", provider, exc)

    if not chain:
        logger.debug(
            "候选链为空,按契约原样返回当前模型:provider=%s model=%r", provider, model
        )
        return [model]
    logger.debug("候选链已构造:provider=%s 候选=%s", provider, chain)
    return chain


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _with_model(resolved: Any, model: str) -> Any:
    """复制 resolved 并替换 ``model`` 字段(dataclasses.replace;鸭子降级浅拷贝)。"""
    try:
        return dataclasses.replace(resolved, model=model)
    except TypeError:  # 非 dataclass 的鸭子对象:浅拷贝后改属性
        clone = copy.copy(resolved)
        clone.model = model
        return clone


def _is_model_not_found(exc: BaseException, not_found_cls: Any) -> bool:
    """判定异常是否"模型不存在":优先 A62 异常类 isinstance,兜底类名匹配。

    兜底按类名比较,兼容测试注入假 vlm_client 模块携带同名异常类的场景
    (A63 经验:替身模块里的异常类与真实类不同源,isinstance 不成立)。
    """
    if (
        isinstance(not_found_cls, type)
        and issubclass(not_found_cls, BaseException)
        and isinstance(exc, not_found_cls)
    ):
        return True
    return type(exc).__name__ == "ModelNotFoundError"


def _default_spend(cfg: Config) -> SpendFn:
    """构造缺省预算闸门:惰性 ``vlm_cache.VlmCache(...).spend_one``。

    - 首次调用时才构造缓存(不在 negotiate 入口提前建库,配置错路径零落盘);
    - ``vlm_cache`` 未就位 → 抛 RuntimeError(中文)直接拒绝外呼
      (红线 19:无 spend_one 记账,一次真实请求都不允许)。
    """
    state: dict[str, Any] = {}

    def _call() -> None:
        if "spend" not in state:
            vlm_cache_mod = _load_sibling(_VLM_CACHE_MODULE)
            cache_cls = (
                getattr(vlm_cache_mod, "VlmCache", None)
                if vlm_cache_mod is not None
                else None
            )
            if cache_cls is None:
                raise RuntimeError(
                    "预算模块 vlm_cache 未就位且未注入 spend:按 V4 红线 19,"
                    "无 spend_one 记账即拒绝任何模型名协商外呼;"
                    "请落地 vlm_cache,或在调用 negotiate 时注入 spend。"
                )
            db_path = str(
                getattr(cfg, "vlm_cache_db", _DEFAULT_CACHE_DB) or _DEFAULT_CACHE_DB
            )
            daily_budget = int(getattr(cfg, "vlm_daily_budget", _DEFAULT_DAILY_BUDGET) or 0)
            state["spend"] = cache_cls(db_path, daily_budget).spend_one
        state["spend"]()

    return _call


def _spend_before_probe(spend_fn: SpendFn, provider: str, candidate: str) -> None:
    """单跳尝试前记账(红线 19);预算尽 → RuntimeError(中文),绝不外呼。"""
    try:
        spend_fn()
    except RuntimeError as exc:
        raise RuntimeError(
            f"模型名协商外呼被预算闸门拒绝(提供方 {provider},候选 {candidate}):{exc}"
        ) from exc


# ---------------------------------------------------------------------------
# 协商主入口
# ---------------------------------------------------------------------------


def negotiate(
    provider: str,
    model: str,
    cfg: Config | None = None,
    *,
    transport: Transport | None = None,
    spend: SpendFn | None = None,
) -> str:
    """按候选链协商出提供方当前可用的模型名,成功即进程内定格并返回。

    流程:

    1. **已定格** → 持锁读进程内缓存,直接返回(零外呼、零记账);
       未定格 → 登记合流槽:同键并发首调只允许一个线程探测,其余等待后
       复用定格结果(V5:并发不双倍外呼、不双倍记账);
    2. 依赖就位检查:``vlm_client.UniversalVLMClient`` 与 ``providers.resolve``
       缺任一 → RuntimeError(中文),拒绝盲发;
    3. ``providers.resolve(提供方:模型, cfg)`` 构造 resolved 基座
       (配置类 ValueError 原样上抛,不换名);
    4. 逐候选:``spend_one`` 记账 → ``dataclasses.replace(model=候选)`` 构造
       resolved 鸭子 → ``UniversalVLMClient(..., transport=transport).chat_json``
       发送极小文本探测消息 ``{"role": "user", "content": "ping"}``;
    5. **任何合法 dict 返回**(不校验评分 schema)→ 持锁定格该候选并返回;
       ``ModelNotFoundError`` → 下一候选;其他异常 → 原样上抛(配置错不换名);
    6. 全链 ``ModelNotFoundError`` → RuntimeError(中文)列出全部试过的候选。

    遥测(V5):定格候选与协商前模型不同(发生了换名回退)时
    ``telemetry.inc("negotiate.fallback")`` 计一次;进程内复用定格不重复计。

    :param provider: 提供方名(目录键,如 ``"glm"``;不带模型部分)
    :param model: 协商前的原始模型名(可为空串 = 由链上其他来源补位)
    :param cfg: 全局配置(密钥 / 覆盖表 / 预算库路径)
    :param transport: 注入传输层(测试用);缺省走 A62 标准库真实外呼
    :param spend: 注入预算闸门(零参 callable);缺省惰性 ``vlm_cache.spend_one``
    :return: 协商成功(或已定格)的模型名
    :raises RuntimeError: 依赖未就位 / 预算尽 / 全链失败(均为中文)
    :raises ValueError: 未知提供方、本地提供方未指定模型等配置错(原样上抛)
    :raises VlmConfigError: vlm_online 未开 / 缺密钥等前置条件不满足(原样上抛)
    """
    cfg = cfg if cfg is not None else Config()
    provider = str(provider or "").strip()
    model = str(model or "").strip()
    cache_key = f"{provider}:{model}"

    # 0) 已定格(持锁读):进程内缓存命中,零外呼、零记账;未命中则登记合流槽
    #    ——同键并发首调只允许一个线程探测,其余等待后直接复用定格结果
    #    (V5:并发首调不双倍外呼、不双倍记账,红线 19)。
    while True:
        with _NEGOTIATED_LOCK:
            pinned = _NEGOTIATED.get(cache_key)
            inflight: threading.Event | None = _INFLIGHT.get(cache_key)
            probing_owner = pinned is None and inflight is None
            if probing_owner:
                inflight = threading.Event()
                _INFLIGHT[cache_key] = inflight
        if pinned:
            logger.debug(
                "模型名协商已定格,直接复用:提供方=%s 原模型=%s → %s",
                provider, model, pinned,
            )
            return pinned
        if probing_owner:
            break
        # 并发等待方:等先行线程完成;被唤醒或超时后回到循环头部——
        # 已定格则复用,先行失败/超时(合流槽已释放)则由自己接管探测
        if inflight is not None and not inflight.wait(_INFLIGHT_WAIT_TIMEOUT_S):
            logger.warning(
                "等待同键协商探测超时(%.0fs),准备自行探测:提供方=%s 模型=%s",
                _INFLIGHT_WAIT_TIMEOUT_S, provider, model or "(未指定)",
            )

    try:
        # 1) 依赖就位检查:统一传输层 + 提供方目录(缺任一即拒绝协商,绝不盲发)
        vlm_client_mod = _load_sibling(_VLM_CLIENT_MODULE)
        client_cls = (
            getattr(vlm_client_mod, "UniversalVLMClient", None)
            if vlm_client_mod is not None
            else None
        )
        if client_cls is None:
            raise RuntimeError(
                f"统一传输层 vlm_client 未就位,拒绝发起模型名协商外呼(提供方 {provider});"
                "请先落地 A62 UniversalVLMClient,或修复其导入错误后重试。"
            )
        providers_mod = _load_sibling(_PROVIDERS_MODULE)
        resolve_fn = (
            getattr(providers_mod, "resolve", None) if providers_mod is not None else None
        )
        if not callable(resolve_fn):
            raise RuntimeError(
                f"提供方目录 providers 未就位,无法解析 {provider} 的端点与密钥,"
                "拒绝发起模型名协商外呼。"
            )

        # 2) resolved 基座:显式带上原始模型(优先级高于配置覆盖;本地提供方必填模型)
        try:
            base_resolved = resolve_fn(f"{provider}:{model}" if model else provider, cfg)
        except Exception as exc:  # noqa: BLE001 - 配置错不换名,原样上抛
            logger.warning(
                "协商前置解析失败(配置错不换名):提供方=%s 模型=%s 错误=%s",
                provider, model or "(未指定)", exc,
            )
            raise

        # 3) 候选链与预算闸门(红线 19:每跳尝试前必须 spend_one)
        candidates = candidate_chain(provider, model, cfg)
        spend_fn = spend if spend is not None else _default_spend(cfg)
        not_found_cls = getattr(vlm_client_mod, "ModelNotFoundError", None)

        tried: list[str] = []
        last_exc: Exception | None = None
        for index, candidate in enumerate(candidates, start=1):
            _spend_before_probe(spend_fn, provider, candidate)
            client = client_cls(_with_model(base_resolved, candidate), cfg, transport=transport)
            logger.info(
                "模型名协商探测:提供方=%s 候选=%s(第 %d/%d 跳,协商前模型=%s)",
                provider, candidate, index, len(candidates), model or "(未指定)",
            )
            try:
                result = client.chat_json([dict(_PROBE_MESSAGE[0])])
            except Exception as exc:  # noqa: BLE001 - 分拣:模型不存在→下一跳;其余上抛
                if _is_model_not_found(exc, not_found_cls):
                    tried.append(candidate)
                    last_exc = exc
                    logger.warning(
                        "候选模型不存在,按链回退:提供方=%s 模型=%s(%d/%d)",
                        provider, candidate, index, len(candidates),
                    )
                    continue
                raise
            # 成功:任何合法 dict 返回即视为该候选可用(不做评分 schema 校验)
            with _NEGOTIATED_LOCK:
                _NEGOTIATED[cache_key] = candidate
            if candidate != model:
                telemetry.inc("negotiate.fallback")  # 换名成功定格一次,复用不重复计
            logger.info(
                "模型名协商成功并定格:提供方=%s 协商前模型=%s → %s(返回字段=%s)",
                provider,
                model or "(未指定)",
                candidate,
                sorted(result) if isinstance(result, dict) else type(result).__name__,
            )
            return candidate

        raise RuntimeError(
            f"提供方 {provider} 的模型名协商全链失败:候选链共 {len(candidates)} 项,"
            f"已逐一探测 {len(tried)} 项({('、'.join(tried))})均报模型不存在。"
            "请用 cfg.vlm_provider_models 显式配置该提供方当前可用的模型名,"
            "或以 vlmctl models / ping 人工核对(目录里的模型名只是提示值,"
            "以各平台官方文档为准)。最后一次错误:" + str(last_exc)
        )
    finally:
        # 探测方离场:释放合流槽并唤醒全部等待方(无论成功/失败/上抛)
        if probing_owner and inflight is not None:
            with _NEGOTIATED_LOCK:
                _INFLIGHT.pop(cache_key, None)
            inflight.set()
