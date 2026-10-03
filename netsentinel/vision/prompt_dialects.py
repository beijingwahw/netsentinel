# -*- coding: utf-8 -*-
"""请求方言适配层(NetSentinel V4 · A72):抽象请求 → 三种 API 方言的纯渲染。

把"系统提示 + 用户文本 + 若干图片"的**抽象请求**,按三种 API 方言渲染成
正确的 HTTP 形态(``{"url", "headers", "payload"}``):

- ``openai``:POST ``{base}/chat/completions``,roles 消息 + ``image_url``
  data URI,``response_format={"type": "json_object"}``;
- ``anthropic``:POST ``{base}/messages``,system 提取为顶层字符串,图片块
  ``content[].source`` 裸 base64,头 ``x-api-key`` + ``anthropic-version``,
  ``max_tokens`` 必填;
- ``gemini``:POST ``{base}/models/{model}:generateContent``,
  ``systemInstruction.parts[].text``,图片 ``parts[].inline_data``,头
  ``x-goog-api-key``,``generationConfig.responseMimeType=application/json``。

定位(与 A62 vlm_client 的关系):本模块是**纯函数的同构参考实现**——A62
``UniversalVLMClient`` 是带闸门 / 重试 / 日志的传输层,本模块只做"形态渲染"
这一步,供测试对照与未来新方言扩展;两边的请求语义逐字段一致(温度 0.1、
max_tokens 1024、anthropic 版本头 2023-06-01 等,均以 A62 真实实现为准)。

无原生 JSON 输出能力的方言(``CAPS[style]["json_mode"] is False``,即
anthropic)在 user 文本前追加一行中文补偿指令 :data:`JSON_ONLY_HINT`。

纯标准库、纯函数、零外呼;密钥只进入返回的 headers(红线 17:调用方负责
打码后才能落日志)。
"""
from __future__ import annotations

import base64
from types import MappingProxyType
from typing import Any, Mapping

from netsentinel import telemetry

__all__ = [
    "CAPS",
    "SUPPORTED_STYLES",
    "JSON_ONLY_HINT",
    "TEMPERATURE",
    "MAX_TOKENS",
    "ANTHROPIC_VERSION",
    "build_request",
    "parse_response",
]

#: 采样温度:内容审核要求输出稳定,与 A62 vlm_client.TEMPERATURE 一致
TEMPERATURE: float = 0.1

#: 返回长度上限(token);anthropic 方言 max_tokens 必填,与 A62 一致
MAX_TOKENS: int = 1024

#: anthropic 协议版本头(官方 2023-06-01 稳定版,与 A62 一致)
ANTHROPIC_VERSION: str = "2023-06-01"

#: 无原生 JSON 输出能力方言(json_mode=False)的中文补偿指令
JSON_ONLY_HINT: str = "请只输出一个 JSON 对象"

#: 方言能力矩阵(契约 §4 A72;V5 起双层冻结为只读映射,防止运行期被改写):
#: - ``json_mode``:该方言有原生 JSON 输出能力(openai response_format /
#:   gemini responseMimeType;anthropic 无,靠补偿指令);
#: - ``system_top``:system 是顶层参数而非消息(anthropic 的 ``system`` 字段、
#:   gemini 的 ``systemInstruction``;openai 的 system 留在 messages 里)。
CAPS: Mapping[str, Mapping[str, bool]] = MappingProxyType(
    {
        "openai": MappingProxyType({"json_mode": True, "system_top": False}),
        "anthropic": MappingProxyType({"json_mode": False, "system_top": True}),
        "gemini": MappingProxyType({"json_mode": True, "system_top": True}),
    }
)

#: 支持的 API 方言(CAPS 的键,顺序即文档顺序)
SUPPORTED_STYLES: tuple[str, ...] = tuple(CAPS)


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------


def _validate_style(style: str, provider: str = "") -> None:
    """方言合法性校验:未知方言抛 :class:`ValueError`(中文,列出可选项)。"""
    if style not in CAPS:
        who = f"提供方 {provider} 的 " if provider else ""
        raise ValueError(
            f"{who}API 方言未知:{style}(仅支持 {'/'.join(SUPPORTED_STYLES)})"
        )


def _encode_image(data: bytes, mime: str) -> tuple[str, str]:
    """图片字节 → ``(mime, 裸 base64)`` 恰好一次编码,各方言按需取用。

    - 裸 base64 供 anthropic ``source.data`` / gemini ``inline_data.data``
      组块使用(两方言均不接受 data URI 前缀);
    - openai 方言的 data URI 前缀拼接推迟到 ``_build_openai`` 内完成——
      非 openai 方言不再为用不到的 data URI 付出一次 O(base64 长度) 的
      字符串拼接(V5:编码单次产出、产物按需成形);
    - mime 缺省按 png 处理(与 A62 encode_image 的兜底口径一致)。
    """
    raw = base64.b64encode(data).decode("ascii")
    media_type = mime or "image/png"
    return media_type, raw


def _compensated_user(style: str, user: str) -> str:
    """无原生 JSON 能力的方言在 user 文本前追加一行中文补偿指令。"""
    if CAPS[style]["json_mode"]:
        return user
    return f"{JSON_ONLY_HINT}\n{user}"


# ---------------------------------------------------------------------------
# 三方言请求构造
# ---------------------------------------------------------------------------


def build_request(
    style: str,
    provider: str,
    system: str,
    user: str,
    images: list[tuple[bytes, str]],
    *,
    model: str = "m",
    api_key: str = "k",
    base_url: str = "https://x",
) -> dict:
    """把抽象请求按方言渲染为 ``{"url": …, "headers": …, "payload": …}``。

    参数说明:

    - ``style``:API 方言,``openai`` / ``anthropic`` / ``gemini`` 之一,
      未知方言抛 :class:`ValueError`(中文);
    - ``provider``:提供方名(仅供错误消息与调用方记账,不进入请求形态);
    - ``system`` / ``user``:系统提示与用户文本;
    - ``images``:``(图片字节, mime)`` 列表,base64 编码在本函数内完成;
    - ``model`` / ``api_key`` / ``base_url``:模型名、密钥与端点根
      (base_url 尾斜杠会被剥掉,与 A62 一致;密钥为空串时不加认证头)。

    各方言形态(与 A62 vlm_client 真实请求语义逐字段一致):

    - openai → ``{base}/chat/completions``,``Authorization: Bearer``,
      messages=[system 消息, user 消息],有图时 user content 为
      ``[{"type": "text"}, {"type": "image_url", …}]``,无图保持纯字符串;
      ``response_format={"type": "json_object"}``、``max_tokens=1024``、
      ``temperature=0.1``;
    - anthropic → ``{base}/messages``,``x-api-key`` +
      ``anthropic-version: 2023-06-01``,system 顶层字符串(空则省略),
      content=[``{"type": "text"}``, ``{"type": "image", source 裸 base64}``],
      ``max_tokens=1024``;
    - gemini → ``{base}/models/{model}:generateContent``,``x-goog-api-key``,
      ``systemInstruction.parts[].text``,``contents=[{role: user,
      parts=[{text}, {inline_data}]}]``,
      ``generationConfig={responseMimeType: application/json,
      temperature: 0.1}``(模型名只在 URL,不进载荷)。
    """
    _validate_style(style, provider)
    base = base_url.rstrip("/")
    # 每张图恰好编码一次(多图互不影响;同一 bytes 传两遍也按两张图各编一次,
    # 与 A62 的逐图语义一致);遥测只计成功渲染的请求(未知方言抛错不计)
    encoded = [_encode_image(data, mime) for data, mime in images]
    telemetry.inc("dialect.build")
    if style == "openai":
        return _build_openai(base, system, user, encoded, model, api_key)
    if style == "anthropic":
        return _build_anthropic(base, system, user, encoded, model, api_key)
    return _build_gemini(base, system, user, encoded, api_key, model)


def _build_openai(
    base: str,
    system: str,
    user: str,
    encoded: list[tuple[str, str]],
    model: str,
    api_key: str,
) -> dict:
    """openai 方言:system 留在 messages,JSON 模式 + 低温 + image_url data URI。"""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    user_message: dict[str, Any] = {"role": "user"}
    if encoded:
        parts: list[dict[str, Any]] = [{"type": "text", "text": user}]
        parts.extend(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{raw}"},
            }
            for mime, raw in encoded
        )
        user_message["content"] = parts
    else:  # 无图不改写:content 保持纯字符串(与 A62 一致)
        user_message["content"] = user
    messages.append(user_message)

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": TEMPERATURE,
        "response_format": {"type": "json_object"},
        "max_tokens": MAX_TOKENS,
    }
    return {"url": base + "/chat/completions", "headers": headers, "payload": payload}


def _build_anthropic(
    base: str,
    system: str,
    user: str,
    encoded: list[tuple[str, str]],
    model: str,
    api_key: str,
) -> dict:
    """anthropic 方言:system 顶层 + 裸 base64 图片块 + JSON 补偿指令。"""
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "anthropic-version": ANTHROPIC_VERSION,
    }
    if api_key:
        headers["x-api-key"] = api_key

    text = _compensated_user("anthropic", user)  # json_mode=False 的补偿行
    content: Any = text
    if encoded:
        blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
        blocks.extend(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": raw},
            }
            for mime, raw in encoded
        )
        content = blocks

    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
    }
    if system:
        payload["system"] = system
    return {"url": base + "/messages", "headers": headers, "payload": payload}


def _build_gemini(
    base: str,
    system: str,
    user: str,
    encoded: list[tuple[str, str]],
    api_key: str,
    model: str,
) -> dict:
    """gemini 方言:模型名进 URL,systemInstruction 顶层,inline_data 图片。"""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["x-goog-api-key"] = api_key

    parts: list[dict[str, Any]] = [{"text": user}]
    parts.extend(
        {"inline_data": {"mime_type": mime, "data": raw}}
        for mime, raw in encoded
    )
    payload: dict[str, Any] = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": TEMPERATURE,
        },
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    return {
        "url": f"{base}/models/{model}:generateContent",
        "headers": headers,
        "payload": payload,
    }


# ---------------------------------------------------------------------------
# 三方言响应提取
# ---------------------------------------------------------------------------


def parse_response(style: str, body: dict) -> str | None:
    """按方言从 200 响应体(dict)中提取模型输出文本。

    - openai → ``choices[0].message.content``(须为字符串);
    - anthropic → ``content`` 列表各 ``text`` 块拼接;
    - gemini → ``candidates[0].content.parts[*].text`` 拼接;
    - 形态不符(缺键 / 类型不对 / 非对象)返回 ``None``,绝不抛出;
    - 未知方言抛 :class:`ValueError`(中文)——那是调用方代码 bug,
      与"响应形态不符"性质不同。
    """
    _validate_style(style)
    if not isinstance(body, dict):
        return None
    if style == "openai":
        return _parse_openai(body)
    if style == "anthropic":
        return _parse_anthropic(body)
    return _parse_gemini(body)


def _parse_openai(body: dict) -> str | None:
    """openai:choices[0].message.content;任何一环缺失 / 类型不对 → None。"""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


def _parse_anthropic(body: dict) -> str | None:
    """anthropic:content 列表 text 块拼接(非 text 块不计入拼接)。"""
    blocks = body.get("content")
    if not isinstance(blocks, list) or not blocks:
        return None
    if not all(isinstance(block, dict) for block in blocks):
        return None
    return "".join(block.get("text", "") for block in blocks)


def _parse_gemini(body: dict) -> str | None:
    """gemini:candidates[0].content.parts[*].text 拼接。"""
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    first = candidates[0]
    if not isinstance(first, dict):
        return None
    content = first.get("content")
    if not isinstance(content, dict):
        return None
    parts = content.get("parts")
    if not isinstance(parts, list) or not parts:
        return None
    if not all(isinstance(part, dict) for part in parts):
        return None
    return "".join(part.get("text", "") for part in parts)
