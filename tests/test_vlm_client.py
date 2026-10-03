# -*- coding: utf-8 -*-
"""vlm_client 测试(A62 · 统一 VLM 传输层,三方言)。

V4 红线 20:全部用例 mock 传输层(注入 FakeTransport),零网络外呼;
密钥只存在于注入的 resolved 对象与请求头里,绝不落日志 / 异常(caplog 断言)。

A243 成本旁路落账(默认关):开关矩阵——缺省关零落账 / 附加属性开启后
维度正确(run_id 透传、tokens/duration_s 可得时带上、est_cost 提示值口径
诚实)/ 落账异常绝不影响 chat_json 返回值(旁路红线)。
"""
from __future__ import annotations

import base64
import json
import logging
import pathlib
import sys
import types
import urllib.error
from dataclasses import dataclass

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.vision import vlm_client
from netsentinel.vision.vlm_client import (
    ModelNotFoundError,
    UniversalVLMClient,
    VlmConfigError,
    _builtin_parse_json_text,
    encode_image,
)

API_KEY = "sk-test-key-0123456789abcdef"
MSGS = [
    {"role": "system", "content": "你是图片审核助手,只输出 JSON。"},
    {"role": "user", "content": "请审核这张图片。"},
]


# ---------------------------------------------------------------------------
# 工具与夹具
# ---------------------------------------------------------------------------


@dataclass
class FakeResolved:
    """A61 ResolvedProvider 的鸭子替身(providers.py 未就位时亦可独立测试)。"""

    provider: str = "openai"
    base_url: str = "https://api.example.com/v1/"
    model: str = "gpt-4o-mini"
    api_key: str = API_KEY
    style: str = "openai"
    local: bool = False


def make_cfg(**overrides) -> Config:
    """构造启用在线 VLM 的测试配置(密钥/开关只影响客户端,不产生真实请求)。"""
    params: dict = dict(vlm_online=True, vlm_request_timeout_s=12.5, vlm_max_image_mb=8.0)
    params.update(overrides)
    return Config(**params)


class FakeTransport:
    """按脚本回放的假传输层:脚本元素为 (status, body) 或异常对象。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    def __call__(self, url, headers, payload, timeout=90.0):
        self.calls.append(
            {"url": url, "headers": dict(headers), "payload": payload, "timeout": timeout}
        )
        if not self.script:
            raise AssertionError("假传输层脚本已耗尽,出现意外的一次额外请求")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item
        return int(status), body


def write_img(tmp_path, name: str, size: int) -> str:
    """写一个指定大小的假图片文件(encode_image 不校验图片内容,零依赖)。"""
    path = tmp_path / name
    path.write_bytes(b"x" * size)
    return str(path)


def ok_openai(content) -> str:
    """构造 openai 方言 200 响应体。"""
    return json.dumps({"choices": [{"message": {"content": content}}]})


def ok_anthropic(*texts: str) -> str:
    """构造 anthropic 方言 200 响应体(多 content 块)。"""
    return json.dumps({"content": [{"type": "text", "text": t} for t in texts]})


def ok_gemini(*texts: str) -> str:
    """构造 gemini 方言 200 响应体(多 parts)。"""
    return json.dumps(
        {"candidates": [{"content": {"parts": [{"text": t} for t in texts]}}]}
    )


def make_client(resolved=None, cfg=None, script=None, **resolved_kwargs):
    """一站式构造:client + FakeTransport(resolved_kwargs 覆盖 FakeResolved 字段)。"""
    resolved = resolved or FakeResolved(**resolved_kwargs)
    cfg = cfg or make_cfg()
    transport = FakeTransport(script if script is not None else [(200, ok_openai('{"a": 1}'))])
    client = UniversalVLMClient(resolved, cfg, transport=transport)
    return client, transport


# ---------------------------------------------------------------------------
# 异常类型
# ---------------------------------------------------------------------------


def test_exception_types_are_runtime_error() -> None:
    """VlmConfigError / ModelNotFoundError 均为 RuntimeError 子类(契约签名固定)。"""
    assert issubclass(VlmConfigError, RuntimeError)
    assert issubclass(ModelNotFoundError, RuntimeError)


# ---------------------------------------------------------------------------
# openai 方言:请求形态逐字段断言
# ---------------------------------------------------------------------------


def test_openai_request_shape(tmp_path) -> None:
    """URL/认证头/载荷字段/图片 data URI 块/system 位置逐字段断言。"""
    img = write_img(tmp_path, "pic.png", 128)
    client, transport = make_client(script=[(200, ok_openai('{"a": 1}'))])
    assert client.chat_json(MSGS, image_paths=[img]) == {"a": 1}

    call = transport.calls[0]
    assert call["url"] == "https://api.example.com/v1/chat/completions"  # 尾斜杠已 rstrip
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["headers"]["Authorization"] == f"Bearer {API_KEY}"
    payload = call["payload"]
    assert payload["model"] == "gpt-4o-mini"
    assert payload["temperature"] == 0.1
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["max_tokens"] == 1024
    # system 保留在 messages 内,不提取
    messages = payload["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == MSGS[0]["content"]
    # 图片并入最后一条 user 消息:text 块在前,图片块在后,base64 可还原
    content = messages[-1]["content"]
    assert content[0] == {"type": "text", "text": "请审核这张图片。"}
    assert content[1]["type"] == "image_url"
    data_uri = content[1]["image_url"]["url"]
    assert data_uri.startswith("data:image/png;base64,")
    assert base64.b64decode(data_uri.split(",", 1)[1]) == b"x" * 128


def test_openai_without_images_keeps_plain_content() -> None:
    """无图片时不改写消息:content 保持字符串原样。"""
    client, transport = make_client(script=[(200, ok_openai('{"ok": true}'))])
    assert client.chat_json(MSGS) == {"ok": True}
    messages = transport.calls[0]["payload"]["messages"]
    assert messages[-1]["content"] == "请审核这张图片。"


def test_openai_multiple_images_appended_in_order(tmp_path) -> None:
    """多图按传入顺序追加到最后一条 user 消息,且不修改调用方 messages。"""
    imgs = [write_img(tmp_path, "a.jpg", 8), write_img(tmp_path, "b.webp", 8)]
    original = [dict(m) for m in MSGS]
    client, transport = make_client(script=[(200, ok_openai('{"a": 1}'))])
    client.chat_json(MSGS, image_paths=imgs)
    content = transport.calls[0]["payload"]["messages"][-1]["content"]
    assert [part["type"] for part in content] == ["text", "image_url", "image_url"]
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert content[2]["image_url"]["url"].startswith("data:image/webp;base64,")
    assert MSGS == original  # 入参未被就地修改


def test_timeout_from_config_passed_to_transport() -> None:
    """超时取 cfg.vlm_request_timeout_s(默认传输层同参)。"""
    client, transport = make_client(cfg=make_cfg(vlm_request_timeout_s=12.5))
    client.chat_json(MSGS)
    assert transport.calls[0]["timeout"] == 12.5


# ---------------------------------------------------------------------------
# anthropic 方言:请求形态逐字段断言
# ---------------------------------------------------------------------------


def test_anthropic_request_shape(tmp_path) -> None:
    """URL/x-api-key+版本头/system 顶层/图片 base64 块/max_tokens 逐字段断言。"""
    img = write_img(tmp_path, "pic.png", 100)
    client, transport = make_client(
        style="anthropic", provider="anthropic", model="claude-sonnet-4",
        base_url="https://api.anthropic.com/v1",
        script=[(200, ok_anthropic('{"a": 2}'))],
    )
    assert client.chat_json(MSGS, image_paths=[img]) == {"a": 2}

    call = transport.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["headers"]["x-api-key"] == API_KEY
    assert call["headers"]["anthropic-version"] == "2023-06-01"
    payload = call["payload"]
    assert payload["model"] == "claude-sonnet-4"
    assert payload["max_tokens"] == 1024  # anthropic 必填
    # system 提取到顶层,messages 里不再出现
    assert payload["system"] == MSGS[0]["content"]
    roles = [m["role"] for m in payload["messages"]]
    assert "system" not in roles and roles == ["user"]
    # 图片块:裸 base64 + media_type(不是 data URI)
    blocks = payload["messages"][-1]["content"]
    assert blocks[0] == {"type": "text", "text": "请审核这张图片。"}
    source = blocks[1]["source"]
    assert blocks[1]["type"] == "image"
    assert source == {"type": "base64", "media_type": "image/png", "data": source["data"]}
    assert base64.b64decode(source["data"]) == b"x" * 100


def test_anthropic_without_images_and_multi_system() -> None:
    """无图片时 content 保持字符串;多条 system 消息合并为顶层 system。"""
    messages = [
        {"role": "system", "content": "规则一。"},
        {"role": "system", "content": "规则二。"},
        {"role": "user", "content": "看图。"},
    ]
    client, transport = make_client(
        style="anthropic", script=[(200, ok_anthropic('{"b": 1}'))]
    )
    client.chat_json(messages)
    payload = transport.calls[0]["payload"]
    assert payload["system"] == "规则一。\n\n规则二。"
    assert payload["messages"] == [{"role": "user", "content": "看图。"}]


# ---------------------------------------------------------------------------
# gemini 方言:请求形态逐字段断言
# ---------------------------------------------------------------------------


def test_gemini_request_shape(tmp_path) -> None:
    """URL/头/contents.parts/systemInstruction/generationConfig 逐字段断言。"""
    img = write_img(tmp_path, "pic.png", 96)
    client, transport = make_client(
        style="gemini", provider="gemini", model="gemini-2.0-flash",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        script=[(200, ok_gemini('{"c": 3}'))],
    )
    assert client.chat_json(MSGS, image_paths=[img]) == {"c": 3}

    call = transport.calls[0]
    assert call["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
    )
    assert call["headers"]["x-goog-api-key"] == API_KEY
    assert "Authorization" not in call["headers"]
    payload = call["payload"]
    assert "model" not in payload  # gemini 模型只在 URL,不进载荷
    # systemInstruction 顶层
    assert payload["systemInstruction"] == {"parts": [{"text": MSGS[0]["content"]}]}
    # contents:[{role:user, parts:[{text},{inline_data}]}]
    contents = payload["contents"]
    assert len(contents) == 1 and contents[0]["role"] == "user"
    parts = contents[0]["parts"]
    assert parts[0] == {"text": "请审核这张图片。"}
    inline = parts[1]["inline_data"]
    assert set(inline) == {"mime_type", "data"}
    assert inline["mime_type"] == "image/png"
    assert base64.b64decode(inline["data"]) == b"x" * 96
    # generationConfig:JSON 模式 + 低温
    assert payload["generationConfig"] == {
        "responseMimeType": "application/json",
        "temperature": 0.1,
    }


def test_gemini_assistant_role_mapped_and_multi_images(tmp_path) -> None:
    """assistant -> model 角色映射;多图 inline_data 按顺序追加到最后一条 user。"""
    imgs = [write_img(tmp_path, "a.gif", 8), write_img(tmp_path, "b.jpg", 8)]
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "第一轮"},
        {"role": "assistant", "content": "好的"},
        {"role": "user", "content": "请看图"},
    ]
    client, transport = make_client(style="gemini", script=[(200, ok_gemini('{"d": 4}'))])
    client.chat_json(messages, image_paths=imgs)
    contents = transport.calls[0]["payload"]["contents"]
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert "systemInstruction" in transport.calls[0]["payload"]
    last_parts = contents[-1]["parts"]
    assert [p["inline_data"]["mime_type"] for p in last_parts[1:]] == [
        "image/gif", "image/jpeg",
    ]
    assert last_parts[0] == {"text": "请看图"}


# ---------------------------------------------------------------------------
# 三方言响应解析
# ---------------------------------------------------------------------------


def test_openai_content_already_dict() -> None:
    """openai json_object 模式下部分网关直接返回对象:原样取用。"""
    client, _ = make_client(script=[(200, ok_openai({"nsfw_prob": 0.7}))])
    assert client.chat_json(MSGS) == {"nsfw_prob": 0.7}


def test_anthropic_multi_blocks_concat() -> None:
    """anthropic 多 content 块文本拼接后再解析。"""
    client, _ = make_client(
        style="anthropic",
        script=[(200, ok_anthropic('{"nsfw_prob": ', "0.5, ", '"note": "ok"}'))],
    )
    assert client.chat_json(MSGS) == {"nsfw_prob": 0.5, "note": "ok"}


def test_gemini_multi_parts_concat() -> None:
    """gemini 多 parts 文本拼接后再解析。"""
    client, _ = make_client(
        style="gemini", script=[(200, ok_gemini('{"e": ', "5}"))],
    )
    assert client.chat_json(MSGS) == {"e": 5}


def test_fenced_json_parsed_all_styles() -> None:
    """围栏 ```json 输出可解析(经 vlm_prompts.parse_json_response)。"""
    fenced = "```json\n{\"nsfw_prob\": 0.9}\n```"
    for style, body in (
        ("openai", ok_openai(fenced)),
        ("anthropic", ok_anthropic(fenced)),
        ("gemini", ok_gemini(fenced)),
    ):
        client, _ = make_client(style=style, script=[(200, body)])
        assert client.chat_json(MSGS) == {"nsfw_prob": 0.9}


def test_json_with_leading_prose_parsed() -> None:
    """模型先说废话再给 JSON("以下是JSON:"风格):平衡括号提取。"""
    client, _ = make_client(script=[(200, ok_openai('以下是JSON:{"k": 1} 以上。'))])
    assert client.chat_json(MSGS) == {"k": 1}


def test_builtin_parser_fallback_semantics() -> None:
    """内置兜底解析:围栏剥离 + 平衡括号;无花括号 / 非 dict 返回 None。"""
    assert _builtin_parse_json_text('```json\n{"a": 1}\n```') == {"a": 1}
    assert _builtin_parse_json_text('前置文字 {"a": {"b": 2}} 后置') == {"a": {"b": 2}}
    assert _builtin_parse_json_text('{"s": "含}花括号"}') == {"s": "含}花括号"}
    assert _builtin_parse_json_text("没有 JSON") is None
    assert _builtin_parse_json_text("[1, 2, 3]") is None


def test_non_json_content_raises_runtime_error() -> None:
    """文本不是 JSON → RuntimeError(中文)。"""
    client, _ = make_client(script=[(200, ok_openai("这不是 JSON"))])
    with pytest.raises(RuntimeError, match="不是 JSON"):
        client.chat_json(MSGS)


def test_array_json_content_raises_runtime_error() -> None:
    """JSON 但非对象(数组)→ RuntimeError。"""
    client, _ = make_client(script=[(200, ok_openai("[1,2,3]"))])
    with pytest.raises(RuntimeError, match="不是 JSON 对象"):
        client.chat_json(MSGS)


def test_openai_missing_choices_and_gemini_missing_candidates() -> None:
    """响应缺关键字段(choices/candidates)→ RuntimeError 中文。"""
    client, _ = make_client(script=[(200, json.dumps({"error": "empty"}))])
    with pytest.raises(RuntimeError, match="choices"):
        client.chat_json(MSGS)
    client, _ = make_client(
        style="gemini", script=[(200, json.dumps({"candidates": []}))],
    )
    with pytest.raises(RuntimeError, match="candidates"):
        client.chat_json(MSGS)


def test_non_json_response_body_raises() -> None:
    """200 但响应体整体不是 JSON → RuntimeError 中文。"""
    client, _ = make_client(script=[(200, "<html>gateway error</html>")])
    with pytest.raises(RuntimeError, match="不是合法 JSON"):
        client.chat_json(MSGS)


# ---------------------------------------------------------------------------
# 前置闸门(红线 16:云端双条件;本地免闸门)
# ---------------------------------------------------------------------------


def test_cloud_gate_vlm_online_off() -> None:
    """云端提供方 vlm_online=False → VlmConfigError(含提供方与缺失项),零外呼。"""
    client, transport = make_client(cfg=make_cfg(vlm_online=False))
    with pytest.raises(VlmConfigError, match="openai") as exc_info:
        client.chat_json(MSGS)
    assert "vlm_online" in str(exc_info.value)
    assert transport.calls == []  # 拒发前绝不构造/发送请求


def test_cloud_gate_missing_key() -> None:
    """云端提供方开在线但无密钥 → VlmConfigError(含提供方与密钥缺失项)。"""
    client, transport = make_client(api_key="", cfg=make_cfg(vlm_online=True))
    with pytest.raises(VlmConfigError, match="密钥") as exc_info:
        client.chat_json(MSGS)
    assert "openai" in str(exc_info.value)
    assert transport.calls == []


def test_cloud_gate_both_missing_lists_two_items() -> None:
    """双条件同时缺失:异常消息同时列出两项。"""
    client, _ = make_client(api_key="", cfg=make_cfg(vlm_online=False))
    with pytest.raises(VlmConfigError) as exc_info:
        client.chat_json(MSGS)
    message = str(exc_info.value)
    assert "vlm_online" in message and "密钥" in message and "openai" in message


def test_local_provider_bypasses_gate(tmp_path) -> None:
    """本地提供方免 vlm_online/密钥闸门:照常发请求,且不带 Authorization 头。"""
    client, transport = make_client(
        provider="ollama", base_url="http://127.0.0.1:11434/v1",
        model="llava", api_key="", local=True, cfg=make_cfg(vlm_online=False),
        script=[(200, ok_openai('{"a": 1}'))],
    )
    assert client.chat_json(MSGS) == {"a": 1}
    assert len(transport.calls) == 1
    assert "Authorization" not in transport.calls[0]["headers"]


def test_unknown_style_raises_value_error() -> None:
    """未知方言 → ValueError 中文。"""
    client, _ = make_client(style="grpc", script=[])
    with pytest.raises(ValueError, match="方言"):
        client.chat_json(MSGS)


def test_missing_model_raises_config_error() -> None:
    """模型缺失(如 vllm 未指定模型)→ VlmConfigError 中文提示显式指定。"""
    client, _ = make_client(model="", script=[])
    with pytest.raises(VlmConfigError, match="model"):
        client.chat_json(MSGS)


# ---------------------------------------------------------------------------
# 图片编码:超限跳过与 mime 表
# ---------------------------------------------------------------------------


def test_oversize_image_skipped_with_warning(tmp_path, caplog) -> None:
    """单图超过 cfg.vlm_max_image_mb:跳图 + warning,请求仍发出(纯文本)。"""
    big = write_img(tmp_path, "huge.png", 4096)
    with caplog.at_level(logging.WARNING, logger="netsentinel.vision.vlm_client"):
        client, transport = make_client(
            cfg=make_cfg(vlm_max_image_mb=0.001),  # 约 1048 字节上限
            script=[(200, ok_openai('{"a": 1}'))],
        )
        assert client.chat_json(MSGS, image_paths=[big]) == {"a": 1}
    assert any("跳过" in r.message and "huge.png" in r.message for r in caplog.records)
    content = transport.calls[0]["payload"]["messages"][-1]["content"]
    assert content == "请审核这张图片。"  # 图片未进入载荷


def test_encode_image_oversize_raises_value_error(tmp_path) -> None:
    """encode_image 直调超限 → ValueError 中文(含路径与上限)。"""
    img = write_img(tmp_path, "big.jpg", 4096)
    with pytest.raises(ValueError, match="上限") as exc_info:
        encode_image(img, max_mb=0.001)
    assert "big.jpg" in str(exc_info.value)


def test_encode_image_boundary_exactly_at_limit_ok(tmp_path) -> None:
    """恰好等于上限(==,非 >)应放行。"""
    img = write_img(tmp_path, "edge.png", 1024)  # max_mb=1/1024 -> 恰 1024 字节
    data_uri, _mime, size = encode_image(img, max_mb=1 / 1024)
    assert size == 1024
    assert data_uri.startswith("data:image/png;base64,")


def test_encode_image_mime_table_and_size(tmp_path) -> None:
    """扩展名 -> mime 表(png/jpg/jpeg/webp/gif),未知扩展名按 png,data URI 前缀正确。"""
    cases = {
        "a.png": "image/png",
        "b.jpg": "image/jpeg",
        "c.jpeg": "image/jpeg",
        "d.webp": "image/webp",
        "e.gif": "image/gif",
        "f.bin": "image/png",  # 未知扩展名兜底
    }
    for name, mime in cases.items():
        data_uri, got_mime, size = encode_image(write_img(tmp_path, name, 32))
        assert got_mime == mime
        assert data_uri.startswith(f"data:{mime};base64,")
        assert size == 32
        assert base64.b64decode(data_uri.split(",", 1)[1]) == b"x" * 32


def test_unreadable_image_skipped_with_warning(tmp_path, caplog) -> None:
    """图片文件缺失:跳过 + warning,不中断请求。"""
    with caplog.at_level(logging.WARNING, logger="netsentinel.vision.vlm_client"):
        client, transport = make_client(script=[(200, ok_openai('{"a": 1}'))])
        missing = str(tmp_path / "ghost.png")
        assert client.chat_json(MSGS, image_paths=[missing]) == {"a": 1}
    assert any("不可读" in r.message for r in caplog.records)
    assert transport.calls[0]["payload"]["messages"][-1]["content"] == "请审核这张图片。"


# ---------------------------------------------------------------------------
# ModelNotFoundError 判定(400/404/错误体中英文双判)
# ---------------------------------------------------------------------------


def test_model_not_found_on_404() -> None:
    """HTTP 404 → ModelNotFoundError(含模型与提供方)。"""
    client, _ = make_client(script=[(404, json.dumps({"error": "not found"}))])
    with pytest.raises(ModelNotFoundError) as exc_info:
        client.chat_json(MSGS)
    message = str(exc_info.value)
    assert "gpt-4o-mini" in message and "openai" in message


def test_model_not_found_on_400() -> None:
    """HTTP 400 → ModelNotFoundError(与 404 同判)。"""
    client, _ = make_client(script=[(400, json.dumps({"error": "bad model"}))])
    with pytest.raises(ModelNotFoundError):
        client.chat_json(MSGS)


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"error": {"message": "The model 'x' does not exist"}}),
        json.dumps({"error": "model not found: gpt-x"}),
        json.dumps({"error": "请求的模型不存在,请检查模型名"}),
        json.dumps({"error": "未找到模型 gpt-x"}),
    ],
    ids=["en-does-not-exist", "en-not-found", "cn-model-not-exist", "cn-not-found"],
)
def test_model_not_found_by_error_body_markers(body: str) -> None:
    """非 400/404 状态但错误体含模型不存在字样(中英文双判)→ ModelNotFoundError。"""
    client, _ = make_client(script=[(500, body)])
    with pytest.raises(ModelNotFoundError):
        client.chat_json(MSGS)


@pytest.mark.parametrize("status", [403, 429, 500], ids=["403", "429", "500"])
def test_other_http_errors_are_runtime_error_with_status(status: int) -> None:
    """其余 4xx/5xx → RuntimeError 中文且含状态码,而非 ModelNotFoundError。"""
    client, _ = make_client(script=[(status, json.dumps({"error": "boom"}))])
    with pytest.raises(RuntimeError, match=str(status)) as exc_info:
        client.chat_json(MSGS)
    assert not isinstance(exc_info.value, ModelNotFoundError)


# ---------------------------------------------------------------------------
# URLError 重试
# ---------------------------------------------------------------------------


def test_urlerror_retry_once_then_success() -> None:
    """首次 URLError 自动重试 1 次,第二次成功 → 正常返回,共 2 次调用。"""
    client, transport = make_client(
        script=[
            urllib.error.URLError("connection refused"),
            (200, ok_openai('{"a": 1}')),
        ]
    )
    assert client.chat_json(MSGS) == {"a": 1}
    assert len(transport.calls) == 2
    assert transport.calls[0]["url"] == transport.calls[1]["url"]


def test_urlerror_twice_raises_runtime_error() -> None:
    """两次 URLError → RuntimeError 中文(已重试),共 2 次调用后停止。"""
    client, transport = make_client(
        script=[urllib.error.URLError("timeout"), urllib.error.URLError("timeout")]
    )
    with pytest.raises(RuntimeError, match="网络错误"):
        client.chat_json(MSGS)
    assert len(transport.calls) == 2


# ---------------------------------------------------------------------------
# 密钥安全(红线 17:密钥绝不落日志/异常)
# ---------------------------------------------------------------------------


def test_api_key_never_in_logs(tmp_path, caplog) -> None:
    """成功 + 跳图 + 重试 + 各类报错全流程,任何日志都不含密钥。"""
    with caplog.at_level(logging.DEBUG, logger="netsentinel.vision.vlm_client"):
        # 成功(debug 打印打码后的认证头)
        client, _ = make_client(script=[(200, ok_openai('{"a": 1}'))])
        client.chat_json(MSGS)
        # 跳图 warning
        client, _ = make_client(
            cfg=make_cfg(vlm_max_image_mb=0.001), script=[(200, ok_openai('{"a": 1}'))],
        )
        client.chat_json(MSGS, image_paths=[write_img(tmp_path, "big.png", 4096)])
        # 重试 warning + 网络错误异常
        client, _ = make_client(
            script=[urllib.error.URLError("refused"), urllib.error.URLError("refused")],
        )
        with pytest.raises(RuntimeError):
            client.chat_json(MSGS)
        # HTTP 错误异常
        client, _ = make_client(script=[(403, "forbidden")])
        with pytest.raises(RuntimeError):
            client.chat_json(MSGS)
    for record in caplog.records:
        assert API_KEY not in record.getMessage()
        assert API_KEY not in record.getMessage().replace(" ", "")


def test_api_key_scrubbed_from_error_body_snippet() -> None:
    """错误体回显了密钥:异常消息中也被抹成 ****。"""
    body = json.dumps({"error": f"invalid key {API_KEY}"})
    client, _ = make_client(script=[(500, body)])
    with pytest.raises(RuntimeError) as exc_info:
        client.chat_json(MSGS)
    assert API_KEY not in str(exc_info.value)
    assert "****" in str(exc_info.value)


# ---------------------------------------------------------------------------
# provider_quirks 惰性套用(A64 未就位时透传;就位时经其 deep-copy 规则应用)
# ---------------------------------------------------------------------------


def _fake_quirks_module(monkeypatch, apply) -> None:
    """同时打补丁 sys.modules 与包属性:``from X import Y`` 优先取包属性,
    只补 sys.modules 在 A64 已被其他测试导入过(属性已绑定)时会失效。"""
    import netsentinel.vision

    fake = types.SimpleNamespace(apply_quirks=apply)
    monkeypatch.setattr(netsentinel.vision, "provider_quirks", fake, raising=False)
    monkeypatch.setitem(sys.modules, "netsentinel.vision.provider_quirks", fake)


def test_quirks_module_applied_when_present(monkeypatch) -> None:
    """A64 就位时:apply_quirks 追加的头进入最终请求。"""
    seen: list[str] = []

    def fake_apply_quirks(provider, headers, payload):
        seen.append(provider)
        return {**headers, "X-Test-Quirk": "1"}, payload

    _fake_quirks_module(monkeypatch, fake_apply_quirks)
    client, transport = make_client(provider="tests-provider", script=[(200, ok_openai('{"a": 1}'))])
    client.chat_json(MSGS)
    assert seen == ["tests-provider"]
    assert transport.calls[0]["headers"]["X-Test-Quirk"] == "1"


def test_quirks_absent_or_broken_is_noop(monkeypatch) -> None:
    """A64 未就位 / 抛异常:请求按原样发送,不阻塞。"""
    # 未就位(模块属性与 sys.modules 均移除):默认路径即透传
    import netsentinel.vision

    monkeypatch.delattr(netsentinel.vision, "provider_quirks", raising=False)
    monkeypatch.delitem(sys.modules, "netsentinel.vision.provider_quirks", False)
    client, transport = make_client(provider="tests-provider", script=[(200, ok_openai('{"a": 1}'))])
    client.chat_json(MSGS)
    assert "X-Test-Quirk" not in transport.calls[0]["headers"]
    # 就位但抛异常:同样透传
    def broken_apply(*args, **kwargs):
        raise RuntimeError("quirk table corrupted")

    _fake_quirks_module(monkeypatch, broken_apply)
    client, transport = make_client(provider="tests-provider", script=[(200, ok_openai('{"a": 1}'))])
    assert client.chat_json(MSGS) == {"a": 1}
    assert "X-Test-Quirk" not in transport.calls[0]["headers"]


# ---------------------------------------------------------------------------
# V5 升级(A89):按 provider 断路器 / URLError 指数退避 / 遥测 / 序列化复用
# ---------------------------------------------------------------------------


class FakeClock:
    """可手动推进的假单调时钟(断路器冷却判定用)。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clean_breaker():
    """隔离模块级断路器状态:前后各复位一次,不污染同文件既有用例与其他文件。"""
    vlm_client._reset_breakers()
    yield
    vlm_client._reset_breakers()


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """注入空休眠并记录每次退避时长(测试不真等)。"""
    sleeps: list[float] = []
    monkeypatch.setattr(vlm_client, "_sleep", sleeps.append)
    return sleeps


def _trip_breaker(provider: str, times: int = vlm_client.BREAKER_FAILURE_THRESHOLD) -> None:
    """让指定提供方连续 `times` 次连接层失败(URLError 重试耗尽)。"""
    for _ in range(times):
        client, _ = make_client(
            provider=provider,
            script=[urllib.error.URLError("conn down"), urllib.error.URLError("conn down")],
        )
        with pytest.raises(RuntimeError, match="网络错误"):
            client.chat_json(MSGS)


def _counter(name: str) -> float:
    return telemetry.snapshot()["counters"].get(name, 0.0)


def _timer_count(name: str) -> int:
    stats = telemetry.snapshot()["timers"].get(name)
    return stats["count"] if stats else 0


# ---------------------------------------------------------------------------
# 断路器:连续 5 次连接层失败 → 熔断 60s → 半开探活 → 成功复位 / 失败重熔断
# ---------------------------------------------------------------------------


def test_v5_breaker_opens_after_five_consecutive_network_failures(
    clean_breaker, no_sleep, monkeypatch
) -> None:
    """连续 5 次 URLError 重试耗尽 → 熔断:第 6 次零外呼直接 RuntimeError(中文),计 vlm.breaker_rejected。"""
    monkeypatch.setattr(vlm_client, "_clock", FakeClock())
    _trip_breaker("tests-breaker")
    rejected_before = _counter("vlm.breaker_rejected")
    client, transport = make_client(provider="tests-breaker", script=[])
    with pytest.raises(RuntimeError) as exc_info:
        client.chat_json(MSGS)
    message = str(exc_info.value)
    assert "熔断中,稍后重试" in message
    assert "tests-breaker" in message
    assert transport.calls == []  # 熔断期绝不构造 / 发送请求(FakeTransport 脚本未动)
    assert _counter("vlm.breaker_rejected") - rejected_before == 1.0
    # 4 次失败尚不熔断:边界恰好卡在阈值上
    vlm_client._reset_breakers()
    _trip_breaker("tests-breaker-4", times=4)
    client_ok, transport_ok = make_client(
        provider="tests-breaker-4", script=[(200, ok_openai('{"a": 1}'))],
    )
    assert client_ok.chat_json(MSGS) == {"a": 1}  # 第 5 类调用正常放行
    assert len(transport_ok.calls) == 1


def test_v5_breaker_half_open_probe_success_closes(
    clean_breaker, no_sleep, monkeypatch
) -> None:
    """熔断期满转半开:放行一次探活,成功即复位闭路(后续调用正常放行)。"""
    clock = FakeClock()
    monkeypatch.setattr(vlm_client, "_clock", clock)
    _trip_breaker("tests-breaker")
    # 冷却期内:拒绝
    rejected, _ = make_client(provider="tests-breaker", script=[])
    with pytest.raises(RuntimeError, match="熔断中"):
        rejected.chat_json(MSGS)
    # 推进恰好 60s → 半开,一次探活成功 → 闭路
    clock.advance(vlm_client.BREAKER_COOLDOWN_S)
    probe, probe_transport = make_client(
        provider="tests-breaker", script=[(200, ok_openai('{"ok": 1}'))],
    )
    assert probe.chat_json(MSGS) == {"ok": 1}
    assert len(probe_transport.calls) == 1
    # 闭路后连续成功,不再拒绝
    again, _ = make_client(provider="tests-breaker", script=[(200, ok_openai('{"ok": 2}'))])
    assert again.chat_json(MSGS) == {"ok": 2}


def test_v5_breaker_half_open_probe_failure_reopens(
    clean_breaker, no_sleep, monkeypatch
) -> None:
    """半开探活失败 → 立即重新熔断(冷却期重新计时)。"""
    clock = FakeClock()
    monkeypatch.setattr(vlm_client, "_clock", clock)
    _trip_breaker("tests-breaker")
    clock.advance(vlm_client.BREAKER_COOLDOWN_S)  # 转半开
    probe, _ = make_client(
        provider="tests-breaker",
        script=[urllib.error.URLError("still down"), urllib.error.URLError("still down")],
    )
    with pytest.raises(RuntimeError, match="网络错误"):
        probe.chat_json(MSGS)
    # 探活失败:未等 60s 也已重新熔断
    reopened, _ = make_client(provider="tests-breaker", script=[])
    with pytest.raises(RuntimeError, match="熔断中"):
        reopened.chat_json(MSGS)
    # 再推进 60s 又转半开(冷却期被重新计时过)
    clock.advance(vlm_client.BREAKER_COOLDOWN_S)
    probe2, _ = make_client(provider="tests-breaker", script=[(200, ok_openai('{"b": 1}'))])
    assert probe2.chat_json(MSGS) == {"b": 1}


def test_v5_breaker_success_resets_failure_streak(clean_breaker, no_sleep) -> None:
    """成功即复位连续计数:4 败 1 成 4 败,始终未达 5 连败,不熔断。"""
    _trip_breaker("tests-breaker", times=4)
    ok, _ = make_client(provider="tests-breaker", script=[(200, ok_openai('{"a": 1}'))])
    assert ok.chat_json(MSGS) == {"a": 1}
    _trip_breaker("tests-breaker", times=4)
    final, final_transport = make_client(
        provider="tests-breaker", script=[(200, ok_openai('{"a": 2}'))],
    )
    assert final.chat_json(MSGS) == {"a": 2}  # 未熔断,正常放行
    assert len(final_transport.calls) == 1


def test_v5_breaker_independent_per_provider(clean_breaker, no_sleep) -> None:
    """断路器按 provider 维度隔离:A 熔断不影响 B。"""
    _trip_breaker("tests-breaker-a")
    rejected_a, _ = make_client(provider="tests-breaker-a", script=[])
    with pytest.raises(RuntimeError, match="熔断中"):
        rejected_a.chat_json(MSGS)
    client_b, transport_b = make_client(
        provider="tests-breaker-b", script=[(200, ok_openai('{"who": "b"}'))],
    )
    assert client_b.chat_json(MSGS) == {"who": "b"}
    assert len(transport_b.calls) == 1


def test_v5_breaker_ignores_http_and_parse_errors(clean_breaker) -> None:
    """HTTP 应用层错误(5xx)与 200 解析失败不计入断路器:连续多次后仍正常外呼。"""
    for _ in range(6):  # 6 次 500(> 阈值 5)
        client, _ = make_client(provider="tests-breaker-http", script=[(500, "boom")])
        with pytest.raises(RuntimeError, match="500"):
            client.chat_json(MSGS)
    for _ in range(6):  # 6 次 200 但内容非 JSON(解析失败)
        client, _ = make_client(
            provider="tests-breaker-http", script=[(200, ok_openai("not json"))],
        )
        with pytest.raises(RuntimeError, match="不是 JSON"):
            client.chat_json(MSGS)
    # 未熔断:下一次调用仍实际发出(404 → ModelNotFoundError,而非熔断 RuntimeError)
    client, _ = make_client(provider="tests-breaker-http", script=[(404, "gone")])
    with pytest.raises(ModelNotFoundError):
        client.chat_json(MSGS)


# ---------------------------------------------------------------------------
# URLError 指数退避(base 1s + 抖动,sleep 可注入)
# ---------------------------------------------------------------------------


def test_v5_urlerror_backoff_sleeps_before_retry(no_sleep) -> None:
    """重试前休眠一次 base+抖动(1.0~1.5s);总尝试仍为 2 次(V4 语义不变)。"""
    client, transport = make_client(
        script=[urllib.error.URLError("refused"), (200, ok_openai('{"a": 1}'))],
    )
    assert client.chat_json(MSGS) == {"a": 1}
    assert len(no_sleep) == 1
    assert (
        vlm_client.RETRY_BASE_S <= no_sleep[0]
        <= vlm_client.RETRY_BASE_S + vlm_client.RETRY_JITTER_S
    )
    assert len(transport.calls) == 2


def test_v5_no_sleep_when_first_attempt_succeeds(no_sleep) -> None:
    """首发成功:零休眠、零重试。"""
    client, transport = make_client(script=[(200, ok_openai('{"a": 1}'))])
    client.chat_json(MSGS)
    assert no_sleep == []
    assert len(transport.calls) == 1


def test_v5_backoff_delay_exponential_with_jitter() -> None:
    """退避公式锁定:delay(attempt) ∈ [base*2**attempt, base*2**attempt + jitter)。"""
    for attempt in range(3):
        low = vlm_client.RETRY_BASE_S * (2 ** attempt)
        for _ in range(20):
            delay = vlm_client._backoff_delay(attempt)
            assert low <= delay <= low + vlm_client.RETRY_JITTER_S


# ---------------------------------------------------------------------------
# 遥测:vlm.chat 计时 / vlm.<provider>.calls / vlm.errors / 慢调用告警
# ---------------------------------------------------------------------------


def test_v5_telemetry_calls_timer_errors(clean_breaker, no_sleep) -> None:
    """每次实际外呼计 vlm.<provider>.calls 与 vlm.chat 样本;失败另计 vlm.errors。"""
    calls_before = _counter("vlm.tests-tel.calls")
    errors_before = _counter("vlm.errors")
    timer_before = _timer_count("vlm.chat")
    client, _ = make_client(provider="tests-tel", script=[(200, ok_openai('{"a": 1}'))])
    client.chat_json(MSGS)
    assert _counter("vlm.tests-tel.calls") - calls_before == 1.0
    assert _timer_count("vlm.chat") - timer_before == 1
    assert _counter("vlm.errors") - errors_before == 0.0
    failing, _ = make_client(provider="tests-tel", script=[(500, "boom")])
    with pytest.raises(RuntimeError):
        failing.chat_json(MSGS)
    assert _counter("vlm.tests-tel.calls") - calls_before == 2.0
    assert _counter("vlm.errors") - errors_before == 1.0
    # 网络错误同样计 errors
    netfail, _ = make_client(
        provider="tests-tel",
        script=[urllib.error.URLError("down"), urllib.error.URLError("down")],
    )
    with pytest.raises(RuntimeError, match="网络错误"):
        netfail.chat_json(MSGS)
    assert _counter("vlm.errors") - errors_before == 2.0


def test_v5_slow_chat_warning_logged(clean_breaker, monkeypatch, caplog) -> None:
    """耗时超过阈值输出 WARNING;快调用不告警(时钟注入)。"""
    # 快调用:起止几乎同时 → 无告警
    monkeypatch.setattr(vlm_client, "_perf_counter", lambda: 100.0)
    fast, _ = make_client(script=[(200, ok_openai('{"a": 1}'))])
    with caplog.at_level(logging.WARNING, logger="netsentinel.vision.vlm_client"):
        fast.chat_json(MSGS)
    assert not any("慢" in r.message for r in caplog.records)
    # 慢调用:起 0.0 止 2.5 → 告警,且 vlm.chat 记录样本
    ticks = iter([0.0, 2.5])
    monkeypatch.setattr(vlm_client, "_perf_counter", lambda: next(ticks))
    timer_before = _timer_count("vlm.chat")
    slow, _ = make_client(script=[(200, ok_openai('{"a": 1}'))])
    with caplog.at_level(logging.WARNING, logger="netsentinel.vision.vlm_client"):
        slow.chat_json(MSGS)
    assert any("VLM 调用慢" in r.message for r in caplog.records)
    assert _timer_count("vlm.chat") - timer_before == 1


# ---------------------------------------------------------------------------
# 请求体序列化一次复用(重试不重编图片 base64)
# ---------------------------------------------------------------------------


def test_v5_default_transport_serializes_payload_once(clean_breaker, no_sleep, monkeypatch) -> None:
    """默认传输层:含 base64 的载荷只 json.dumps 一次,重试复用同一份字节串。"""
    serializations: list[dict] = []
    real_json_bytes = vlm_client._json_bytes

    def spy_json_bytes(payload):
        serializations.append(payload)
        return real_json_bytes(payload)

    monkeypatch.setattr(vlm_client, "_json_bytes", spy_json_bytes)
    bodies: list[bytes | None] = []

    def fake_default_transport(url, headers, payload, timeout=90.0, *, data=None):
        bodies.append(data)
        if len(bodies) == 1:
            raise urllib.error.URLError("flaky network")
        return 200, '{"choices": [{"message": {"content": "{\\"a\\": 1}"}}]}'

    monkeypatch.setattr(vlm_client, "_http_post_json", fake_default_transport)
    client = UniversalVLMClient(
        FakeResolved(provider="tests-ser"), make_cfg()
    )  # 不注入 transport → 走默认传输层(已被替换为假实现)
    assert client.chat_json(MSGS) == {"a": 1}
    assert len(bodies) == 2                       # 首发 + 1 次重试
    assert len(serializations) == 1               # 只序列化了一次
    assert bodies[0] is bodies[1]                 # 重试复用同一份字节串
    assert bodies[0] is not None and b"chat" not in bodies[0]  # 请求体确为载荷 JSON


# ---------------------------------------------------------------------------
# A243:成本旁路落账(默认关;开启后维度正确;异常绝不影响分类返回值)
# ---------------------------------------------------------------------------


def _ledger_rows(tmp_path: pathlib.Path) -> list[dict]:
    """读取 <tmp>/data/vlm_cost.jsonl 的全部账目行(文件不存在返回空表)。"""
    path = pathlib.Path(tmp_path) / "data" / "vlm_cost.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_a243_ledger_default_off_writes_nothing(tmp_path) -> None:
    """开关缺省关:chat_json 成功后不写 vlm_cost.jsonl(现状零落账,向后兼容)。"""
    cfg = make_cfg(data_dir=str(tmp_path / "data"))
    client, _ = make_client(
        provider="tests-ledger-off", cfg=cfg, script=[(200, ok_openai('{"a": 1}'))]
    )
    assert client.chat_json(MSGS, image_paths=[write_img(tmp_path, "p.png", 16)]) == {"a": 1}
    assert _ledger_rows(tmp_path) == []  # 零落账快照:文件不存在/无账目行


def test_a243_ledger_enabled_records_all_dimensions(tmp_path) -> None:
    """开启(附加属性 cost_ledger_enabled=True)后:成功调用记一条,维度齐全。"""
    cfg = make_cfg(data_dir=str(tmp_path / "data"))
    cfg.cost_ledger_enabled = True  # 附加实例属性(getattr 动态读取,非配置键)
    cfg.run_id = "run-a243"         # A232 指引 ②:批次标识沿 cfg 附加属性透传
    body = json.dumps({
        "choices": [{"message": {"content": '{"a": 1}'}}],
        "usage": {"total_tokens": 123},
    })
    client, _ = make_client(
        provider="openai", model="gpt-4o-mini", cfg=cfg, script=[(200, body)]
    )
    assert client.chat_json(MSGS, image_paths=[write_img(tmp_path, "p.png", 16)]) == {"a": 1}
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "openai" and row["model"] == "gpt-4o-mini"
    assert row["images"] == 1
    assert row["run_id"] == "run-a243"        # run_id 透传进账目行
    assert row["tokens"] == 123               # 响应 usage 可得时带上
    assert isinstance(row["duration_s"], float) and row["duration_s"] >= 0.0
    assert row["est_cost"] == 0.01            # PRICE_HINTS openai:gpt-4o-mini=10 元/千次 ×1 图


def test_a243_ledger_unpriced_honest_and_no_run_id(tmp_path) -> None:
    """目录无价格提示:est_cost=null(unpriced 诚实,不猜价);无 run_id 不落该键
    (账目行与 V13 前旧行同形态,聚合归"(未标记批次)"桶)。"""
    cfg = make_cfg(data_dir=str(tmp_path / "data"))
    cfg.cost_ledger_enabled = True  # 不挂 cfg.run_id
    client, _ = make_client(
        provider="tests-ledger-unpriced", model="no-hint-model",
        cfg=cfg, script=[(200, ok_openai('{"a": 1}'))],  # 响应无 usage → tokens 亦不落键
    )
    assert client.chat_json(MSGS) == {"a": 1}
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["est_cost"] is None
    assert "run_id" not in rows[0]
    assert "tokens" not in rows[0]
    assert "duration_s" in rows[0]


def test_a243_ledger_local_provider_costs_zero(tmp_path) -> None:
    """本地提供方(LOCAL_FREE,数据不出本机):est_cost=0.0(红线 16 口径)。"""
    cfg = make_cfg(data_dir=str(tmp_path / "data"))
    cfg.cost_ledger_enabled = True
    client, _ = make_client(
        provider="ollama", base_url="http://127.0.0.1:11434/v1",
        model="llava", api_key="", local=True, cfg=cfg,
        script=[(200, ok_openai('{"a": 1}'))],
    )
    assert client.chat_json(MSGS) == {"a": 1}
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1 and rows[0]["provider"] == "ollama"
    assert rows[0]["est_cost"] == 0.0


def test_a243_ledger_failed_call_not_recorded(tmp_path) -> None:
    """失败调用不落账:落账点在成功返回处(HTTP 500 / 网络错误均不记)。"""
    cfg = make_cfg(data_dir=str(tmp_path / "data"))
    cfg.cost_ledger_enabled = True
    client, _ = make_client(
        provider="tests-ledger-fail", cfg=cfg, script=[(500, "boom")]
    )
    with pytest.raises(RuntimeError, match="500"):
        client.chat_json(MSGS)
    assert _ledger_rows(tmp_path) == []


def test_a243_ledger_skipped_images_not_counted(tmp_path) -> None:
    """images 记实际送审数:超限跳过的图不计入(与请求载荷一致)。"""
    cfg = make_cfg(data_dir=str(tmp_path / "data"), vlm_max_image_mb=0.001)
    cfg.cost_ledger_enabled = True
    client, _ = make_client(
        provider="tests-ledger-skip", cfg=cfg, script=[(200, ok_openai('{"a": 1}'))]
    )
    assert client.chat_json(MSGS, image_paths=[write_img(tmp_path, "big.png", 4096)]) == {"a": 1}
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1 and rows[0]["images"] == 0  # 唯一一张图超限被跳过


def test_a243_ledger_failure_never_breaks_chat(tmp_path, monkeypatch, caplog) -> None:
    """旁路红线:记账抛异常 → 静默降级 + 计数 vision.cost_log.failed + 中文
    debug 日志,chat_json 返回值不受任何影响。"""
    from netsentinel.vision import cost_meter as cost_meter_mod

    def _boom(self, *args, **kwargs):
        raise RuntimeError("mock:账盘只读,记账失败")

    monkeypatch.setattr(cost_meter_mod.CostMeter, "record", _boom)
    cfg = make_cfg(data_dir=str(tmp_path / "data"))
    cfg.cost_ledger_enabled = True
    failed_before = _counter("vision.cost_log.failed")
    with caplog.at_level(logging.DEBUG, logger="netsentinel.vision.vlm_client"):
        client, _ = make_client(
            provider="tests-ledger-exc", cfg=cfg, script=[(200, ok_openai('{"a": 1}'))]
        )
        assert client.chat_json(MSGS) == {"a": 1}  # 分类返回值原样
    assert _counter("vision.cost_log.failed") - failed_before == 1.0
    assert any("成本落账旁路失败" in r.message for r in caplog.records)
    assert _ledger_rows(tmp_path) == []


def test_a243_client_marks_self_ledgering() -> None:
    """真实客户端自带 _ledger_records_cost 标记:vlmctl 等上层据此避免双记。"""
    assert UniversalVLMClient._ledger_records_cost is True


@pytest.mark.parametrize(
    ("style", "body", "want"),
    [
        ("openai", json.dumps({"usage": {"total_tokens": 7}}), 7),
        ("openai", json.dumps({"usage": {"prompt_tokens": 3, "completion_tokens": 4}}), None),
        ("anthropic", json.dumps({"usage": {"input_tokens": 3, "output_tokens": 4}}), 7),
        ("anthropic", json.dumps({"usage": {"input_tokens": 3}}), None),
        ("gemini", json.dumps({"usageMetadata": {"totalTokenCount": 9}}), 9),
        ("gemini", json.dumps({"candidates": []}), None),
        ("openai", "不是 JSON", None),
        ("openai", json.dumps({"usage": {"total_tokens": True}}), None),  # bool 不算数
        ("openai", json.dumps({"usage": {"total_tokens": 12.0}}), 12),    # 整数 float 归一
    ],
    ids=["openai-total", "openai-partial", "anthropic-sum", "anthropic-partial",
         "gemini-meta", "gemini-absent", "not-json", "bool-rejected", "float-int"],
)
def test_a243_usage_tokens_extraction(style: str, body: str, want) -> None:
    """token 用量提取:三方言 usage 字段口径;任何缺失/类型不对 → None(诚实)。"""
    assert vlm_client._usage_tokens_from_body(body, style) == want
