# -*- coding: utf-8 -*-
"""统一 VLM 传输层(NetSentinel V4 · A62):三种 API 方言,一套客户端。

按 CONTRACTS-V4 §4 A62 实现 :class:`UniversalVLMClient`,把全部 20 个提供方
(§2 目录)归一到三种 API 方言:

- ``openai``:POST ``{base}/chat/completions``,图片走 ``image_url`` data URI,
  头 ``Authorization: Bearer <key>``(glm/qwen/doubao/ollama 等绝大多数提供方);
- ``anthropic``:POST ``{base}/messages``,system 提取到顶层、必填 ``max_tokens``,
  图片块 ``content[].source.base64``,头 ``x-api-key`` + ``anthropic-version``;
- ``gemini``:POST ``{base}/models/{model}:generateContent``,图片走
  ``parts[].inline_data``,头 ``x-goog-api-key``,
  ``generationConfig.responseMimeType=application/json``。

安全红线(V4 §0 红线 16/17/20):

- 云端提供方外呼前置双条件——``cfg.vlm_online=True`` **且** 已解析到 API 密钥,
  缺一即 :class:`VlmConfigError`(本地提供方 ``local=True`` 免此闸门);
- 密钥绝不写入日志 / 异常消息(错误体片段先经 ``_scrub`` 抹除密钥再嵌入);
- 传输层可注入(``transport``),测试与常规扫描零外呼。

V5 升级(A89 · 统一传输组,CONTRACTS-V5 §1/§4):

- **健壮性·断路器**(按 provider 维度,模块级):连续
  :data:`BREAKER_FAILURE_THRESHOLD` 次**连接层失败**(URLError 重试耗尽)→
  熔断 :data:`BREAKER_COOLDOWN_S` 秒,期间 ``chat_json`` 直接抛
  RuntimeError("熔断中,稍后重试")并计 ``vlm.breaker_rejected``;冷却期满转
  半开、放行一次探活,成功复位、失败重新熔断。HTTP 应用层错误
  (4xx/5xx / 模型不存在 / 解析失败)**不计入**断路器——那类失败由
  failover(A68)/ model_negotiate(A71) 按链回退处理,熔断只针对"提供方
  连不上"这一连接层症状;闸门(:class:`VlmConfigError`)同样不计入;
- **健壮性·指数退避重试**:URLError 重试前休眠 ``RETRY_BASE_S * 2**attempt +
  均匀抖动``;总尝试次数 ≤ 2(首发 + 1 次重试,与 V4 "自动重试 1 次"语义
  一致)。休眠函数与时钟均可注入(``_sleep`` / ``_clock``,测试不真等);
- **性能**:默认传输层的请求体 JSON **只序列化一次**,重试直接复用同一份
  字节串(图片 base64 不随重试反复重编 / 重序列化);
- **可观测性**::func:`UniversalVLMClient.chat_json` 全程计
  ``telemetry.observe("vlm.chat", 秒)`` 与 ``vlm.<provider>.calls``,失败计
  ``vlm.errors``,超过 :data:`SLOW_CHAT_WARNING_S` 秒输出 WARNING。

与 glm_adapter(A21)解耦:本模块是通用层,不导入 glm 专用实现;A22 vlm_prompts、
A64 provider_quirks 等兄弟模块一律惰性导入,未就位时用本模块内置等价实现。

A243 成本旁路落账(默认关;A232 接线指引 ① 的统一落账点):``chat_json``
成功返回处(200 且解析通过)经 ``cost_meter.CostMeter`` 向
``<cfg.data_dir>/vlm_cost.jsonl`` 记一笔金额账(provider/model/images +
est_cost 提示值口径,run_id/tokens/duration_s 可选维度);仅当 cfg 挂
**附加实例属性** ``cost_ledger_enabled=True``(getattr 动态读取,缺省
False = 现状零落账,向后兼容)时生效。落账是**旁路**:全路径 try/except
包裹,任何异常静默降级 + 计数 ``vision.cost_log.failed``,绝不影响分类
主流程。``run_id`` 沿 cfg 附加属性 ``cfg.run_id`` 透传(A232 指引 ②);
类属性 :attr:`UniversalVLMClient._ledger_records_cost` 标记"本客户端自
落账",供 vlmctl ping 等上层调用点避免同一笔调用双记。
"""
from __future__ import annotations

import base64
import json
import logging
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config

__all__ = [
    "VlmConfigError",
    "ModelNotFoundError",
    "encode_image",
    "UniversalVLMClient",
]

logger = logging.getLogger(__name__)

#: 传输层类型:(url, headers, payload, timeout) -> (HTTP 状态码, 响应体文本)
Transport = Callable[[str, dict[str, str], dict[str, Any], float], tuple[int, str]]

#: 单图送审大小上限(兆字节),与 Config.vlm_max_image_mb 默认值一致
DEFAULT_MAX_IMAGE_MB = 8.0
#: 采样温度:内容审核要求输出稳定,取低温
TEMPERATURE = 0.1
#: 返回长度上限(token);anthropic 方言 max_tokens 必填
MAX_TOKENS = 1024
#: anthropic 协议版本头(官方 2023-06-01 稳定版)
ANTHROPIC_VERSION = "2023-06-01"

#: 支持的 API 方言
STYLE_OPENAI = "openai"
STYLE_ANTHROPIC = "anthropic"
STYLE_GEMINI = "gemini"
_STYLES = (STYLE_OPENAI, STYLE_ANTHROPIC, STYLE_GEMINI)

#: 图片扩展名 -> mime 类型(未知扩展名按 png 处理)
_MIME_BY_EXT: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

#: (V5)URLError 指数退避基准秒数:重试前休眠 ``RETRY_BASE_S * 2**attempt + 抖动``
RETRY_BASE_S = 1.0
#: (V5)退避抖动上限(秒):每次重试额外均匀采样 ``[0, RETRY_JITTER_S)``
RETRY_JITTER_S = 0.5
#: (V5)传输层总尝试次数上限(首发 + 1 次重试,与 V4"自动重试 1 次"语义一致)
_MAX_TRANSPORT_ATTEMPTS = 2
#: (V5)断路器:连续连接层失败(URLError 重试耗尽)的熔断阈值
BREAKER_FAILURE_THRESHOLD = 5
#: (V5)断路器:熔断冷却时长(秒),期满转半开放行一次探活
BREAKER_COOLDOWN_S = 60.0
#: (V5)慢调用告警阈值(秒):chat_json 全程耗时超过即输出 WARNING
SLOW_CHAT_WARNING_S = 1.0

# (V5)可注入的时钟 / 休眠 / 计时接缝(测试用假时钟与空休眠,不真等):
_sleep: Callable[[float], None] = time.sleep
_clock: Callable[[], float] = time.monotonic
_perf_counter: Callable[[], float] = time.perf_counter

#: 判定"模型不存在/不可用"的错误体关键词(HTTP 400/404 之外的第二判据,中英文双判)
_MODEL_ERROR_MARKERS: tuple[str, ...] = (
    "model not found",
    "model_not_found",
    "model not exist",
    "model_not_exist",
    "model does not exist",
    "model doesn't exist",
    "no such model",
    "unknown model",
    "invalid model",
    "model unavailable",
    "模型不存在",
    "模型未找到",
    "未找到模型",
    "无此模型",
    "模型不可用",
)

#: 宽松双判(契约"错误体含 model/not found 字样"):同时提到模型与"未找到/不存在"
_LOOSE_NOT_FOUND_WORDS: tuple[str, ...] = (
    "not found",
    "not exist",
    "no such",
    "unknown",
    "doesn't exist",
    "未找到",
    "不存在",
    "无此",
)

#: ``\uXXXX`` 转义序列(很多网关按 ensure_ascii 返回 JSON,中文标记被转义)
_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")

#: 代码围栏(```json ... ```)剥离正则
_FENCE_RE = re.compile(r"```[A-Za-z0-9_-]*[ \t]*\n?(.*?)```", re.DOTALL)
#: 需要打码的认证类请求头
_AUTH_HEADER_KEYS = ("authorization", "x-api-key", "x-goog-api-key")


class VlmConfigError(RuntimeError):
    """VLM 云端调用前置条件不满足(中文,含提供方与缺失项)。

    云端提供方需同时满足 ``cfg.vlm_online=True`` 与已解析到 API 密钥;
    本地提供方(ollama/vllm/lmstudio/xinference)免此闸门(V4 红线 16)。
    """


class ModelNotFoundError(RuntimeError):
    """提供方返回"模型不存在/不可用"(中文,含模型与提供方)。

    触发条件:HTTP 400/404,或错误体含中英文"模型不存在"字样;
    供 A71 model_negotiate / A68 failover 按链回退。
    """


# ---------------------------------------------------------------------------
# 图片编码(独立于 glm_adapter 的通用实现)
# ---------------------------------------------------------------------------


def encode_image(
    path: str,
    *,
    max_mb: float = DEFAULT_MAX_IMAGE_MB,
) -> tuple[str, str, int]:
    """读取本地图片并编码,返回 ``(data_uri, mime, size_bytes)``。

    - mime 按扩展名映射(png/jpg/jpeg/webp/gif),未知扩展名按 png 处理;
    - ``data_uri`` 形如 ``data:<mime>;base64,...``,供 openai 方言直接使用;
      anthropic / gemini 方言取小数点后的裸 base64 与 mime 组块;
    - 单图超过 ``max_mb``(兆字节)抛 :class:`ValueError`(中文);
      :class:`UniversalVLMClient.chat_json` 会捕获该异常并跳图 + warning。
    """
    data = Path(path).read_bytes()
    mime = _MIME_BY_EXT.get(Path(path).suffix.lower(), "image/png")
    limit_bytes = float(max_mb) * 1024.0 * 1024.0
    if len(data) > limit_bytes:
        raise ValueError(
            f"图片超过送审大小上限,已拒绝编码:path={path} "
            f"大小={len(data) / 1024 / 1024:.2f}MB 上限={float(max_mb):.2f}MB"
        )
    encoded = base64.b64encode(data).decode("ascii")
    data_uri = f"data:{mime};base64,{encoded}"
    logger.debug("图片已编码:path=%s mime=%s 大小=%d 字节", path, mime, len(data))
    return data_uri, mime, len(data)


# ---------------------------------------------------------------------------
# 标准库传输封装
# ---------------------------------------------------------------------------


def _json_bytes(payload: dict[str, Any]) -> bytes:
    """(V5)请求体序列化唯一入口:重试复用同一份字节串,不重复编码图片 base64。"""
    return json.dumps(payload).encode("utf-8")


def _http_post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float = 90.0,
    *,
    data: bytes | None = None,
) -> tuple[int, str]:
    """标准库 POST(JSON)封装:成功返回 ``(status, body)``。

    - ``data``(V5)为调用方预序列化好的请求体字节串,传入即直接复用,
      避免重试时对含大图 base64 的载荷反复 ``json.dumps``;
    - ``HTTPError`` 不上抛,捕获后转成 ``(状态码, 错误体文本)`` 返回,由调用方
      按状态码决定回退 / 报错;
    - 其余 ``URLError``(网络层错误)原样上抛,由上层指数退避重试。
    """
    body_bytes = data if data is not None else _json_bytes(payload)
    request = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = int(getattr(response, "status", 200) or 200)
            return status, body
    except urllib.error.HTTPError as exc:  # HTTP 层错误:转为状态码 + 错误体
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 错误体读取失败不应掩盖状态码
            body = ""
        return int(exc.code), body


# ---------------------------------------------------------------------------
# (V5)按 provider 维度的模块级断路器:闭路 → 连续失败计数 → 熔断 → 半开探活
# ---------------------------------------------------------------------------


@dataclass
class _BreakerState:
    """单个提供方的断路器状态(模块级,跨客户端实例共享)。"""

    consecutive_failures: int = 0
    #: 熔断打开时刻(``_clock()`` 单调秒);``None`` = 闭路(健康)
    opened_at: float | None = None


#: 各提供方的断路器状态表(provider -> state)
_BREAKERS: dict[str, _BreakerState] = {}
_BREAKERS_LOCK = threading.Lock()


def _reset_breakers() -> None:
    """清空全部断路器状态(仅供测试与诊断使用)。"""
    with _BREAKERS_LOCK:
        _BREAKERS.clear()


def _backoff_delay(attempt: int) -> float:
    """(V5)第 ``attempt`` 次失败后的指数退避时长:``base * 2**attempt + 均匀抖动``。"""
    return RETRY_BASE_S * (2**attempt) + random.uniform(0.0, RETRY_JITTER_S)


def _breaker_allow(provider: str) -> bool:
    """闭路或冷却期满(半开)放行;熔断冷却期内拒绝。"""
    with _BREAKERS_LOCK:
        state = _BREAKERS.get(provider)
        if state is None or state.opened_at is None:
            return True
        return (_clock() - state.opened_at) >= BREAKER_COOLDOWN_S


def _ensure_breaker_closed(provider: str) -> None:
    """熔断冷却期内的快速失败:计 ``vlm.breaker_rejected`` 并抛 RuntimeError(中文)。

    只在闸门 / 配置校验通过之后调用——配置错误不是提供方健康问题,不消耗熔断
    额度;快速失败发生在图片编码之前,熔断期间连 base64 都不再算。
    """
    if _breaker_allow(provider):
        return
    telemetry.inc("vlm.breaker_rejected")
    raise RuntimeError(
        f"提供方 {provider} 熔断中,稍后重试"
        f"(连续 {BREAKER_FAILURE_THRESHOLD} 次连接失败,冷却 {int(BREAKER_COOLDOWN_S)} 秒,"
        f"期间拒绝外呼)。"
    )


def _breaker_record_failure(provider: str) -> None:
    """记录一次连接层失败(URLError 重试耗尽):连续达阈值 → 熔断;半开探活失败 → 重新熔断。"""
    tripped = False
    with _BREAKERS_LOCK:
        state = _BREAKERS.setdefault(provider, _BreakerState())
        if state.opened_at is not None:  # 半开探活失败:以当前时刻重新计时熔断
            state.opened_at = _clock()
            state.consecutive_failures = 0
            return
        state.consecutive_failures += 1
        if state.consecutive_failures >= BREAKER_FAILURE_THRESHOLD:
            state.opened_at = _clock()
            state.consecutive_failures = 0
            tripped = True
    if tripped:
        logger.warning(
            "提供方 %s 连续 %d 次连接失败,熔断 %d 秒(期间快速失败)",
            provider, BREAKER_FAILURE_THRESHOLD, int(BREAKER_COOLDOWN_S),
        )


def _breaker_record_success(provider: str) -> None:
    """记录一次成功(任何 HTTP 响应都证明连接层健康):断路器复位闭路。"""
    with _BREAKERS_LOCK:
        state = _BREAKERS.pop(provider, None)
        was_open = state is not None and state.opened_at is not None
    if was_open:
        logger.info("提供方 %s 断路器探活成功,已恢复放行", provider)


# ---------------------------------------------------------------------------
# 内置 JSON 容错解析(A22 vlm_prompts 未就位时的等价兜底)
# ---------------------------------------------------------------------------


def _strip_code_fence(text: str) -> str:
    """存在成对 ``` 围栏时取第一段围栏内内容,否则原样返回。"""
    match = _FENCE_RE.search(text)
    return match.group(1) if match else text


def _extract_balanced_object(text: str) -> str | None:
    """返回首个花括号配平的子串(字符串内的花括号与转义引号不参与配平)。"""
    pos = text.find("{")
    while pos != -1:
        end = _scan_balanced(text, pos)
        if end is not None:
            return text[pos:end]
        pos = text.find("{", pos + 1)
    return None


def _scan_balanced(text: str, start: int) -> int | None:
    """从 ``start`` 处的 '{' 开始配平;返回匹配 '}' 的后一位下标,不配平返回 None。"""
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def _builtin_parse_json_text(text: str) -> dict | None:
    """内置兜底解析:剥离代码围栏 → 截取花括号配平子串 → ``json.loads``。

    成功且为 dict 才返回,否则 None(绝不抛出、绝不执行其中内容)。
    """
    if not isinstance(text, str):
        return None
    candidate = _extract_balanced_object(_strip_code_fence(text))
    if candidate is None:
        return None
    try:
        obj = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _load_vlm_prompts() -> Any:
    """惰性导入 A22 vlm_prompts;未就位(并行开发期)返回 None,不硬依赖。"""
    try:
        from netsentinel.vision import vlm_prompts
    except Exception as exc:  # noqa: BLE001 - ImportError / 并行期语法错误等一律降级
        logger.debug("vlm_prompts 暂不可用,使用 vlm_client 内置解析:%s", exc)
        return None
    return vlm_prompts


def _load_provider_quirks() -> Any:
    """惰性导入 A64 provider_quirks;未就位返回 None(quirks 视为透传)。

    优先查 ``sys.modules``:真实模块一旦被任何测试或兄弟模块导入,就会绑定为
    包属性,此后 ``from ... import`` 会绕过 ``sys.modules`` 里测试注入的替身;
    先查 ``sys.modules`` 保证替身注入始终生效(语义不变:未就位→None)。
    """
    injected: Any = sys.modules.get("netsentinel.vision.provider_quirks")
    if injected is not None:
        return injected
    try:
        from netsentinel.vision import provider_quirks
    except Exception as exc:  # noqa: BLE001
        logger.debug("provider_quirks 暂不可用,请求按原样发送:%s", exc)
        return None
    return provider_quirks


def _mask_header(value: str) -> str:
    """认证头打码:保留前 4 位 + ``****``(红线 17:请求头只在 debug 级打码打印)。"""
    return value[:4] + "****" if len(value) > 4 else "****"


# ---------------------------------------------------------------------------
# (A243)旁路成本落账:默认关,开启后向 <data_dir>/vlm_cost.jsonl 记账
# ---------------------------------------------------------------------------


def _as_int(value: Any) -> int | None:
    """宽容整数:int(排除 bool)→ int,整数 float 归一为 int,其余 → None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _usage_tokens_from_body(body: str, style: str) -> int | None:
    """从成功响应体中提取 token 用量(可得时;三方言 usage 字段口径)。

    - openai(及 openai 兼容网关):``usage.total_tokens``;
    - anthropic:``usage.input_tokens + usage.output_tokens``;
    - gemini:``usageMetadata.totalTokenCount``;
    - 任何缺失 / 类型不对 / JSON 解析失败 → ``None``(诚实缺省,不猜数)。
    """
    try:
        document = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(document, dict):
        return None
    usage = document.get("usage")
    if isinstance(usage, dict):
        if style == STYLE_ANTHROPIC:
            got_input = _as_int(usage.get("input_tokens"))
            got_output = _as_int(usage.get("output_tokens"))
            if got_input is not None and got_output is not None:
                return got_input + got_output
        else:
            total = _as_int(usage.get("total_tokens"))
            if total is not None:
                return total
    if style == STYLE_GEMINI:
        meta = document.get("usageMetadata")
        if isinstance(meta, dict):
            total = _as_int(meta.get("totalTokenCount"))
            if total is not None:
                return total
    return None


def _ledger_cost_call(
    cfg: Any,
    provider: str,
    model: str,
    images: int,
    *,
    tokens: int | None = None,
    duration_s: float | None = None,
) -> None:
    """(A243)旁路成本落账:开关开启时向 ``<cfg.data_dir>/vlm_cost.jsonl`` 记一笔。

    开关与批次标识均为 cfg **附加实例属性**(getattr 动态读取,与 V11 前
    开关挂载同款,不是配置文件键——写进 YAML 会按未知键告警忽略):

    - ``cfg.cost_ledger_enabled``:落账开关,**缺省 False = 现状零落账**
      (向后兼容红线:不挂属性时行为与 A243 前完全一致);开启方式:
      在构造调用链入口处 ``cfg.cost_ledger_enabled = True``;
    - ``cfg.run_id``:批次标识(A232 接线指引 ②:收官流程沿 cfg 原对象
      透传链挂载);缺省不传 → 账目行不带 run_id 键,聚合归
      "(未标记批次)"桶(与 V13 前旧行同形态)。

    金额口径:经 :class:`cost_meter.CostMeter` 的 ``record`` 内部
    ``estimate``——``PRICE_HINTS`` 命中记提示值、本地提供方记 ``0.0``、
    目录缺失记 ``null``(unpriced 诚实,以账单为准,红线 18)。

    **落账失败绝不影响调用主流程**(旁路红线):本函数整体 try/except
    包裹,任何异常只计 ``telemetry.inc("vision.cost_log.failed")`` +
    中文 debug 日志,绝不抛出。
    """
    try:
        if cfg is None or not bool(getattr(cfg, "cost_ledger_enabled", False)):
            return  # 开关缺省关 = 现状零落账(向后兼容)
        from netsentinel.vision import cost_meter  # 惰性导入,不硬依赖

        data_dir = str(getattr(cfg, "data_dir", "data") or "data")
        meter = cost_meter.CostMeter(Path(data_dir) / "vlm_cost.jsonl")
        meter.record(
            str(provider),
            str(model),
            int(images),
            run_id=getattr(cfg, "run_id", None),
            tokens=tokens,
            duration_s=duration_s,
        )
    except Exception as exc:  # noqa: BLE001 - 旁路落账失败静默降级 + 计数
        telemetry.inc("vision.cost_log.failed")
        logger.debug("成本落账旁路失败(已忽略,不影响分类主流程):%s", exc)


# ---------------------------------------------------------------------------
# 统一客户端
# ---------------------------------------------------------------------------


class UniversalVLMClient:
    """跨提供方统一 VLM 客户端:三种 API 方言(openai/anthropic/gemini)一套代码。

    用法(通常由 A63 multi_provider 经 providers.resolve 构造)::

        client = UniversalVLMClient(resolved, cfg)   # resolved 为 ResolvedProvider 鸭子
        result = client.chat_json(messages, image_paths=[img_path])

    ``resolved`` 只读以下属性(与 A61 providers.ResolvedProvider 鸭子兼容):
    ``provider / base_url / model / api_key / style / local``。
    ``transport`` 可注入(测试用,签名 ``(url, headers, payload, timeout) ->
    (status, body)``),缺省走模块内 :func:`_http_post_json`(纯标准库)。
    """

    #: (A243)本客户端在 ``cost_ledger_enabled`` 开启时**自行**旁路落账
    #: (见 :func:`_chat_json` 成功返回处);上层调用点(如 vlmctl ping)
    #: 检查该标记,客户端自落账时不再补记,避免同一笔调用双记。
    _ledger_records_cost: bool = True

    def __init__(
        self,
        resolved: Any,
        cfg: Config,
        *,
        transport: Transport | None = None,
    ) -> None:
        self._cfg = cfg
        self._transport: Transport = transport if transport is not None else _http_post_json
        self.provider: str = str(getattr(resolved, "provider", "") or "")
        self.base_url: str = str(getattr(resolved, "base_url", "") or "").rstrip("/")
        self.model: str = str(getattr(resolved, "model", "") or "")
        self._api_key: str = str(getattr(resolved, "api_key", "") or "")
        self.style: str = str(getattr(resolved, "style", STYLE_OPENAI) or STYLE_OPENAI).lower()
        self.local: bool = bool(getattr(resolved, "local", False))

    # ------------------------------------------------------------------
    # 对话(JSON 输出)
    # ------------------------------------------------------------------

    def chat_json(
        self,
        messages: list[dict],
        *,
        image_paths: list[str] | None = None,
    ) -> dict:
        """发起一次多模态对话并要求 JSON 输出,返回解析后的 dict。

        - 前置闸门(红线 16):非本地提供方且(未开 ``vlm_online`` 或无密钥)
          → :class:`VlmConfigError`,绝不外呼;
        - 断路器(V5):提供方处于熔断冷却期 → RuntimeError("熔断中,稍后重试"),
          不编码图片、不外呼,计 ``vlm.breaker_rejected``;
        - ``image_paths`` 逐张 :func:`encode_image`(超限 / 不可读的图跳过并
          warning,不中断请求);
        - 按 ``style`` 构造三方言请求(URL / 头 / 载荷),A64 quirks 就位时套用;
        - 超时 ``cfg.vlm_request_timeout_s``;URLError 按指数退避重试 1 次
          (base 1s + 抖动,总尝试 ≤ 2 次);连接层失败计入按 provider 维度的
          模块级断路器(连续 5 次 → 熔断 60s → 半开探活);
        - 模型不存在(400/404 或错误体含中英文"模型不存在"字样)→
          :class:`ModelNotFoundError`;其余 4xx/5xx → RuntimeError(中文含状态码);
        - 响应按方言取文本(openai choices[0].message.content / anthropic
          content[].text 拼接 / gemini candidates[0].content.parts[*].text 拼接),
          再解析为 dict(优先 A22 parse_json_response,未就位内置围栏剥离 +
          平衡括号);非 dict → RuntimeError;
        - 成本旁路落账(A243,默认关):成功返回处经 :func:`_ledger_cost_call`
          向 ``<cfg.data_dir>/vlm_cost.jsonl`` 记一笔(provider/model/images +
          est_cost 提示值口径 + run_id/tokens/duration_s 可选维度);仅当 cfg
          附加属性 ``cost_ledger_enabled=True`` 时生效,落账异常静默降级 +
          计数 ``vision.cost_log.failed``,绝不影响返回值;
        - 遥测(V5):``vlm.chat`` 计时、``vlm.<provider>.calls`` 计数、失败计
          ``vlm.errors``;全程超过 1s 输出慢调用 WARNING。
        """
        start = _perf_counter()
        try:
            return self._chat_json(messages, image_paths=image_paths)
        finally:
            elapsed = _perf_counter() - start
            telemetry.observe("vlm.chat", elapsed)
            if elapsed >= SLOW_CHAT_WARNING_S:
                logger.warning(
                    "VLM 调用慢:提供方=%s 模型=%s 耗时=%.2fs(阈值 %.1fs)",
                    self.provider, self.model, elapsed, SLOW_CHAT_WARNING_S,
                )

    def _chat_json(
        self,
        messages: list[dict],
        *,
        image_paths: list[str] | None,
    ) -> dict:
        """:func:`chat_json` 的实现主体(计时包装器之外的全部逻辑)。"""
        self._ensure_allowed()
        self._validate_style_and_endpoints()
        _ensure_breaker_closed(self.provider)  # 熔断期快速失败(图片编码之前)
        images = self._encode_images(image_paths or [])
        url, headers, payload = self._build_request(messages, images)
        headers, payload = self._apply_quirks(headers, payload)

        logger.debug(
            "VLM 请求已构造:提供方=%s 方言=%s 模型=%s url=%s 认证头=%s 载荷字段=%s 图片数=%d",
            self.provider,
            self.style,
            self.model,
            url,
            {k: _mask_header(v) for k, v in headers.items() if k.lower() in _AUTH_HEADER_KEYS},
            sorted(payload),
            len(images),
        )
        telemetry.inc(f"vlm.{self.provider}.calls")

        # (A243)落账用时延(含重试退避):旁路计时直接走 time.perf_counter,
        # 不复用可注入的 _perf_counter 接缝——该接缝的调用次数被既有测试
        # (test_v5_slow_chat_warning_logged 的有限时钟迭代器)锁定。
        transport_started = time.perf_counter()
        try:
            status, body = self._post_with_retry(url, headers, payload)
        except urllib.error.URLError as exc:
            _breaker_record_failure(self.provider)  # 连接层失败计入断路器
            telemetry.inc("vlm.errors")
            raise RuntimeError(
                f"提供方 {self.provider} 接口网络错误(已自动重试 1 次):{exc}"
            ) from exc
        _breaker_record_success(self.provider)  # 收到任何 HTTP 响应 = 连接层健康
        transport_s = time.perf_counter() - transport_started

        if status == 200:
            try:
                parsed = self._parse_success(body)
            except RuntimeError:
                telemetry.inc("vlm.errors")
                raise
            logger.debug(
                "VLM 调用成功:提供方=%s 方言=%s 模型=%s 字段=%s",
                self.provider, self.style, self.model, sorted(parsed),
            )
            # (A243)旁路成本落账(A232 指引 ① 的统一落账点):成功返回处记一笔;
            # 默认关,异常静默降级,绝不影响返回值(见 _ledger_cost_call)。
            _ledger_cost_call(
                self._cfg,
                self.provider,
                self.model,
                len(images),
                tokens=_usage_tokens_from_body(body, self.style),
                duration_s=transport_s,
            )
            return parsed
        telemetry.inc("vlm.errors")
        snippet = self._scrub(body[:200])
        if self._is_model_unavailable(status, body):
            raise ModelNotFoundError(
                f"提供方 {self.provider} 的模型 {self.model} 不存在或不可用"
                f"(HTTP {status});请核对该提供方的模型名,可用 cfg.vlm_provider_models "
                f"覆盖,或以 vlmctl models 查看候选。响应片段:{snippet}"
            )
        raise RuntimeError(
            f"提供方 {self.provider} 接口调用失败:HTTP {status};响应片段:{snippet}"
        )

    # ------------------------------------------------------------------
    # 前置闸门与配置校验
    # ------------------------------------------------------------------

    def _ensure_allowed(self) -> None:
        """云端外呼双条件闸门:本地提供方放行;否则 vlm_online+密钥缺一即拒。"""
        if self.local:
            return
        missing: list[str] = []
        if not self._cfg.vlm_online:
            missing.append("vlm_online=False(默认,图像数据不出本机)")
        if not self._api_key:
            missing.append("API 密钥未配置(cfg.vlm_api_keys 或对应环境变量)")
        if missing:
            raise VlmConfigError(
                f"提供方 {self.provider} 未满足云端调用前置条件:"
                + ";".join(missing)
                + "。云端提供方需同时开启 vlm_online=True 且配置密钥;"
                "本地提供方(ollama/vllm/lmstudio/xinference)免此闸门。"
            )

    def _validate_style_and_endpoints(self) -> None:
        """方言与端点完整性校验:未知方言 / 缺 base_url / 缺模型即时报错(中文)。"""
        if self.style not in _STYLES:
            raise ValueError(
                f"提供方 {self.provider} 的 API 方言未知:{self.style}"
                f"(仅支持 {'/'.join(_STYLES)})"
            )
        if not self.base_url:
            raise VlmConfigError(
                f"提供方 {self.provider} 未满足云端调用前置条件:base_url 缺失"
                "(可用 cfg.vlm_provider_base_urls 覆盖)"
            )
        if not self.model:
            raise VlmConfigError(
                f"提供方 {self.provider} 未满足云端调用前置条件:model 缺失"
                "(该提供方无默认模型,须显式指定,如 classifier: "
                f"{self.provider or 'ollama'}:<模型名>)"
            )

    # ------------------------------------------------------------------
    # 图片编码(超限/不可读跳图 + warning)
    # ------------------------------------------------------------------

    def _encode_images(self, image_paths: list[str]) -> list[tuple[str, str, int]]:
        """逐张编码为 ``(data_uri, mime, size_bytes)``;超限或不可读的图跳过。"""
        encoded: list[tuple[str, str, int]] = []
        for path in image_paths:
            try:
                encoded.append(encode_image(path, max_mb=self._cfg.vlm_max_image_mb))
            except ValueError as exc:
                logger.warning("跳过超限图片:%s", exc)
            except OSError as exc:
                logger.warning("跳过不可读图片:path=%s 错误=%s", path, exc)
        return encoded

    # ------------------------------------------------------------------
    # 三方言请求构造
    # ------------------------------------------------------------------

    def _build_request(
        self,
        messages: list[dict],
        images: list[tuple[str, str, int]],
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """按 style 分派到对应方言的请求构造器,返回 ``(url, headers, payload)``。"""
        if self.style == STYLE_OPENAI:
            return self._build_openai(messages, images)
        if self.style == STYLE_ANTHROPIC:
            return self._build_anthropic(messages, images)
        return self._build_gemini(messages, images)

    def _build_openai(
        self,
        messages: list[dict],
        images: list[tuple[str, str, int]],
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """openai 方言:POST {base}/chat/completions,image_url=data URI(glm 泛化)。"""
        url = self.base_url + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        built = [dict(m) for m in messages]  # 不就地修改调用方入参
        if images:
            parts: list[dict[str, Any]] = []
            user_index = _last_user_index(built)
            if user_index is not None:
                content = built[user_index].get("content")
                if isinstance(content, str):
                    parts.append({"type": "text", "text": content})
                elif isinstance(content, list):
                    parts.extend(content)
            parts.extend(
                {"type": "image_url", "image_url": {"url": data_uri}}
                for data_uri, _mime, _size in images
            )
            if user_index is None:
                built.append({"role": "user", "content": parts})
            else:
                built[user_index] = {**built[user_index], "content": parts}
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": built,
            "temperature": TEMPERATURE,
            # 思考模型族(glm-5*)带 response_format 实测会截断输出,故省略
            **({} if str(self.model).startswith("glm-5") else {"response_format": {"type": "json_object"}}),
            "max_tokens": MAX_TOKENS,
        }
        return url, headers, payload

    def _build_anthropic(
        self,
        messages: list[dict],
        images: list[tuple[str, str, int]],
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """anthropic 方言:POST {base}/messages,system 顶层 + base64 图片块。"""
        url = self.base_url + "/messages"
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if self._api_key:
            headers["x-api-key"] = self._api_key

        built: list[dict] = []
        system_texts: list[str] = []
        for message in messages:
            role = message.get("role", "user")
            if role == "system":  # system 一律提取到顶层(anthropic 不允许出现在 messages)
                system_texts.append(_content_to_text(message.get("content")))
                continue
            built.append(dict(message))
        if images:
            blocks: list[dict[str, Any]] = []
            user_index = _last_user_index(built)
            if user_index is not None:
                content = built[user_index].get("content")
                if isinstance(content, str):
                    blocks.append({"type": "text", "text": content})
                elif isinstance(content, list):
                    blocks.extend(content)
            blocks.extend(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": _raw_b64(data_uri),
                    },
                }
                for data_uri, mime, _size in images
            )
            if user_index is None:
                built.append({"role": "user", "content": blocks})
            else:
                built[user_index] = {**built[user_index], "content": blocks}

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": built,
            "max_tokens": MAX_TOKENS,
            "temperature": TEMPERATURE,
        }
        if system_texts:
            payload["system"] = "\n\n".join(text for text in system_texts if text)
        return url, headers, payload

    def _build_gemini(
        self,
        messages: list[dict],
        images: list[tuple[str, str, int]],
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """gemini 方言:POST {base}/models/{model}:generateContent,inline_data 图片。"""
        url = f"{self.base_url}/models/{self.model}:generateContent"
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["x-goog-api-key"] = self._api_key

        contents: list[dict[str, Any]] = []
        system_texts: list[str] = []
        for message in messages:
            role = message.get("role", "user")
            if role == "system":  # system 提取到 systemInstruction 顶层
                system_texts.append(_content_to_text(message.get("content")))
                continue
            gemini_role = "model" if role == "assistant" else role  # gemini 用 model 指代助手
            contents.append({"role": gemini_role, "parts": [{"text": _content_to_text(message.get("content"))}]})
        if images:
            user_index = None
            for i in range(len(contents) - 1, -1, -1):
                if contents[i].get("role") == "user":
                    user_index = i
                    break
            parts: list[dict[str, Any]] = [
                {"inline_data": {"mime_type": mime, "data": _raw_b64(data_uri)}}
                for data_uri, mime, _size in images
            ]
            if user_index is None:
                contents.append({"role": "user", "parts": parts})
            else:
                contents[user_index]["parts"].extend(parts)

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": TEMPERATURE,
            },
        }
        if system_texts:
            payload["systemInstruction"] = {
                "parts": [{"text": "\n\n".join(text for text in system_texts if text)}]
            }
        return url, headers, payload

    # ------------------------------------------------------------------
    # 发送与重试
    # ------------------------------------------------------------------

    def _apply_quirks(
        self,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> tuple[dict[str, str], dict[str, Any]]:
        """惰性套用 A64 provider_quirks.apply_quirks;未就位 / 异常时原样透传。"""
        quirks = _load_provider_quirks()
        if quirks is None:
            return headers, payload
        try:
            result = quirks.apply_quirks(self.provider, headers, payload)
        except Exception as exc:  # noqa: BLE001 - quirks 失败不阻塞请求
            logger.debug("provider_quirks.apply_quirks 异常,按原样发送:%s", exc)
            return headers, payload
        if isinstance(result, tuple) and len(result) == 2:
            new_headers, new_payload = result
            if isinstance(new_headers, dict) and isinstance(new_payload, dict):
                return new_headers, new_payload
        return headers, payload

    def _post_with_retry(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> tuple[int, str]:
        """发起请求;URLError(非 HTTP 状态错误)按指数退避重试 1 次,仍失败则上抛。

        (V5)与 V4 的差异:

        - 重试前休眠 ``RETRY_BASE_S * 2**attempt + 均匀抖动``(默认 1.0~1.5s,
          ``_sleep`` 可注入);总尝试次数 ≤ :data:`_MAX_TRANSPORT_ATTEMPTS`
          (2 次,保持 V4"自动重试 1 次"语义);
        - 默认传输层(``_http_post_json``)的请求体字节串**只序列化一次**,
          两次尝试复用同一份 ``data``——含大图 base64 的载荷不再随重试反复
          ``json.dumps``;注入传输层仍收 dict(Transport 签名冻结)。
        """
        timeout = self._cfg.vlm_request_timeout_s
        use_default_transport = self._transport is _http_post_json
        serialized: bytes | None = None
        last_error: urllib.error.URLError | None = None
        for attempt in range(_MAX_TRANSPORT_ATTEMPTS):
            try:
                if use_default_transport:
                    if serialized is None:
                        serialized = _json_bytes(payload)
                    return _http_post_json(url, headers, payload, timeout, data=serialized)
                return self._transport(url, headers, payload, timeout)
            except urllib.error.URLError as exc:  # HTTPError 已在传输层转为状态码
                last_error = exc
                if attempt + 1 < _MAX_TRANSPORT_ATTEMPTS:
                    delay = _backoff_delay(attempt)
                    logger.warning(
                        "提供方 %s 请求网络错误(%s),%.2f 秒后按指数退避重试(第 %d 次)",
                        self.provider, exc, delay, attempt + 1,
                    )
                    _sleep(delay)
        assert last_error is not None  # pragma: no cover - 循环内必已赋值
        raise last_error

    @staticmethod
    def _is_model_unavailable(status: int, body: str) -> bool:
        """判定"模型不存在":HTTP 400/404,或错误体含中英文模型不存在字样。

        错误体先经 ``\\uXXXX`` 反转义(很多网关按 ensure_ascii 返回中文),再做
        两级匹配:精确短语(如 ``model_not_exist`` / ``模型不存在``)命中即真;
        否则宽松双判——同时出现"模型"字样与"未找到/不存在"类措辞。
        """
        if status in (400, 404):
            return True
        text = _UNICODE_ESCAPE_RE.sub(
            lambda m: chr(int(m.group(1), 16)), body
        ).lower()
        if any(marker in text for marker in _MODEL_ERROR_MARKERS):
            return True
        if "model" in text or "模型" in text:
            return any(word in text for word in _LOOSE_NOT_FOUND_WORDS)
        return False

    def _scrub(self, text: str) -> str:
        """把密钥本身从文本中抹除后再用于异常消息(红线 17)。"""
        if self._api_key and len(self._api_key) >= 4:
            return text.replace(self._api_key, "****")
        return text

    # ------------------------------------------------------------------
    # 响应解析
    # ------------------------------------------------------------------

    def _parse_success(self, body: str) -> dict:
        """200 响应:按方言提取文本并解析为 dict;非 dict 抛 RuntimeError(中文)。"""
        try:
            document = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"提供方 {self.provider} 接口返回体不是合法 JSON:{self._scrub(body[:200])}"
            ) from exc
        if not isinstance(document, dict):
            raise RuntimeError(
                f"提供方 {self.provider} 接口返回体不是 JSON 对象:{self._scrub(body[:200])}"
            )
        content = self._extract_content(document)
        if isinstance(content, dict):  # 部分网关在 json 模式下直接返回对象
            return content
        if not isinstance(content, str):
            raise RuntimeError(
                f"提供方 {self.provider} 返回内容不是 JSON 对象,无法提取审核数值"
            )
        parsed: dict | None = None
        prompts = _load_vlm_prompts()
        if prompts is not None:
            try:
                parsed = prompts.parse_json_response(content)
            except Exception as exc:  # noqa: BLE001 - A22 接口异常时降级内置解析
                logger.debug("vlm_prompts.parse_json_response 异常,降级内置解析:%s", exc)
                parsed = None
        if parsed is None:
            parsed = _builtin_parse_json_text(content)
        if not isinstance(parsed, dict):
            raise RuntimeError(
                f"提供方 {self.provider} 返回内容不是 JSON 对象,无法提取审核数值:"
                f"{self._scrub(content[:200])}"
            )
        return parsed

    def _extract_content(self, document: dict) -> Any:
        """按方言从 200 响应体中提取模型输出文本(或对象)。"""
        snippet = self._scrub(json.dumps(document, ensure_ascii=False)[:200])
        if self.style == STYLE_OPENAI:
            choices = document.get("choices")
            if not isinstance(choices, list) or not choices:
                raise RuntimeError(f"提供方 {self.provider} 返回缺少 choices:{snippet}")
            first = choices[0] if isinstance(choices[0], dict) else {}
            message = first.get("message") if isinstance(first, dict) else {}
            if not isinstance(message, dict) or "content" not in message:
                raise RuntimeError(f"提供方 {self.provider} 返回缺少 message.content:{snippet}")
            return message["content"]
        if self.style == STYLE_ANTHROPIC:
            blocks = document.get("content")
            if not isinstance(blocks, list):
                raise RuntimeError(f"提供方 {self.provider} 返回缺少 content:{snippet}")
            return "".join(
                block.get("text", "") for block in blocks if isinstance(block, dict)
            )
        # gemini:candidates[0].content.parts[*].text 拼接
        candidates = document.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise RuntimeError(f"提供方 {self.provider} 返回缺少 candidates:{snippet}")
        first = candidates[0] if isinstance(candidates[0], dict) else {}
        content = first.get("content") if isinstance(first, dict) else {}
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            raise RuntimeError(f"提供方 {self.provider} 返回缺少 content.parts:{snippet}")
        return "".join(part.get("text", "") for part in parts if isinstance(part, dict))


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------


def _last_user_index(messages: list[dict]) -> int | None:
    """返回最后一条 role=user 的消息下标(从后向前找),无则 None。"""
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], dict) and messages[i].get("role") == "user":
            return i
    return None


def _content_to_text(content: Any) -> str:
    """把消息 content(str 或多模态块列表)归一为纯文本(system 提取用)。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        )
    return "" if content is None else str(content)


def _raw_b64(data_uri: str) -> str:
    """从 data URI 中取出逗号后的裸 base64(anthropic/gemini 图片块用)。"""
    return data_uri.split(",", 1)[1] if "," in data_uri else data_uri
