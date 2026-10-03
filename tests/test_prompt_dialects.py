# -*- coding: utf-8 -*-
"""prompt_dialects 测试(A72 · 请求方言适配层)。

覆盖(契约 §4 A72 + 任务书):

- ``CAPS`` 与契约逐项一致;
- 三方言 ``build_request`` 逐字段断言(URL / 头 / 载荷 / 图片块 / JSON 模式 /
  system 位置 / 补偿指令);
- 与 A62 ``vlm_client`` 真实载荷的**同构一致性对照**(importorskip 保护,mock
  传输层捕获真实请求,同一输入两实现产出 url / headers / payload JSON 精确
  等价;anthropic 的 JSON 补偿行按任务书口径剥离后比较);
- 图片 bytes → base64 编码正确(1x1 PNG);
- ``parse_response`` 各方言形态提取与形态不符返回 None(不抛);
- 未知 style 报 ValueError(中文)。

V4 红线 20:全部离线,零外呼(一致性对照用 CaptureTransport 捕获,不发网络)。
"""
from __future__ import annotations

import base64
import copy
import json
from dataclasses import dataclass

import pytest

from netsentinel.contracts import Config
from netsentinel.vision.prompt_dialects import (
    ANTHROPIC_VERSION,
    CAPS,
    JSON_ONLY_HINT,
    build_request,
    parse_response,
)

SYS = "你是图片审核助手,只输出 JSON。"
USER = "请审核这张图片。"
KEY = "sk-a72-test-key-0123456789"
BASE = "https://api.example.com/v1"

#: 最小合法 1x1 灰度 PNG(67 字节,本测试的图片夹具)
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptV"
    "AAAACklEQVR4nGNgAAAAAgABSK+kcQAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG_1X1).decode("ascii")

#: 一致性对照用的模型名(仅提示值)
MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4",
    "gemini": "gemini-2.0-flash",
}


# ---------------------------------------------------------------------------
# CAPS 与夹具
# ---------------------------------------------------------------------------


def test_caps_match_contract() -> None:
    """能力矩阵与契约 §4 A72 逐项一致,值为布尔且键集合固定。"""
    assert CAPS == {
        "openai": {"json_mode": True, "system_top": False},
        "anthropic": {"json_mode": False, "system_top": True},
        "gemini": {"json_mode": True, "system_top": True},
    }
    for caps in CAPS.values():
        assert set(caps) == {"json_mode", "system_top"}
        assert all(isinstance(value, bool) for value in caps.values())


def test_png_fixture_is_real_png() -> None:
    """夹具自检:PNG 魔数 + base64 往返一致。"""
    assert PNG_1X1[:8] == b"\x89PNG\r\n\x1a\n"
    assert base64.b64decode(PNG_B64) == PNG_1X1
    assert len(PNG_1X1) > 0


# ---------------------------------------------------------------------------
# openai 方言:请求形态逐字段断言
# ---------------------------------------------------------------------------


def test_openai_request_shape_field_by_field() -> None:
    """URL/Bearer 头/roles 消息/json_object/1024/0.1/data URI 图片块。"""
    req = build_request(
        "openai", "openai", SYS, USER, [(PNG_1X1, "image/png")],
        model=MODELS["openai"], api_key=KEY, base_url=BASE,
    )
    assert req["url"] == BASE + "/chat/completions"
    assert req["headers"] == {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {KEY}",
    }
    payload = req["payload"]
    assert payload["model"] == MODELS["openai"]
    assert payload["temperature"] == 0.1
    assert payload["max_tokens"] == 1024
    assert payload["response_format"] == {"type": "json_object"}
    assert "system" not in payload  # system 留在 messages,不提顶层(system_top=False)
    # roles 消息:system 在前,user 在后
    messages = payload["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == SYS
    # user content:text 块在前,图片块在后;有原生 JSON 能力,不加补偿行
    content = messages[-1]["content"]
    assert content[0] == {"type": "text", "text": USER}
    assert JSON_ONLY_HINT not in content[0]["text"]
    assert content[1]["type"] == "image_url"
    data_uri = content[1]["image_url"]["url"]
    assert data_uri == f"data:image/png;base64,{PNG_B64}"
    assert base64.b64decode(data_uri.split(",", 1)[1]) == PNG_1X1


def test_openai_without_images_keeps_plain_content() -> None:
    """无图片时 user content 保持纯字符串,system 消息仍在 messages。"""
    req = build_request("openai", "openai", SYS, USER, [], api_key=KEY, base_url=BASE)
    messages = req["payload"]["messages"]
    assert messages == [
        {"role": "system", "content": SYS},
        {"role": "user", "content": USER},
    ]


# ---------------------------------------------------------------------------
# anthropic 方言:请求形态逐字段断言
# ---------------------------------------------------------------------------


def test_anthropic_request_shape_field_by_field() -> None:
    """URL/x-api-key+版本头/system 顶层/裸 base64 图片块/JSON 补偿行。"""
    req = build_request(
        "anthropic", "anthropic", SYS, USER, [(PNG_1X1, "image/png")],
        model=MODELS["anthropic"], api_key=KEY, base_url=BASE,
    )
    assert req["url"] == BASE + "/messages"
    assert req["headers"] == {
        "Content-Type": "application/json",
        "x-api-key": KEY,
        "anthropic-version": "2023-06-01",
    }
    assert ANTHROPIC_VERSION == "2023-06-01"
    payload = req["payload"]
    assert payload["model"] == MODELS["anthropic"]
    assert payload["max_tokens"] == 1024  # anthropic 必填
    assert payload["system"] == SYS  # system 顶层字符串(system_top=True)
    assert "response_format" not in payload  # json_mode=False:无原生 JSON 能力
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["user"]  # messages 里不允许出现 system
    # content 块:text(带补偿行)在前,image(裸 base64)在后
    blocks = payload["messages"][-1]["content"]
    assert blocks[0] == {"type": "text", "text": f"{JSON_ONLY_HINT}\n{USER}"}
    image_block = blocks[1]
    assert image_block["type"] == "image"
    source = image_block["source"]
    assert set(source) == {"type", "media_type", "data"}
    assert source == {
        "type": "base64",
        "media_type": "image/png",
        "data": PNG_B64,
    }
    assert base64.b64decode(source["data"]) == PNG_1X1  # 裸 base64 可还原


def test_anthropic_without_images_and_empty_system() -> None:
    """无图片时 content 保持纯字符串(含补偿行);空 system 省略顶层键。"""
    req = build_request(
        "anthropic", "anthropic", "", USER, [], api_key=KEY, base_url=BASE,
    )
    payload = req["payload"]
    assert "system" not in payload
    assert payload["messages"] == [
        {"role": "user", "content": f"{JSON_ONLY_HINT}\n{USER}"}
    ]


# ---------------------------------------------------------------------------
# gemini 方言:请求形态逐字段断言
# ---------------------------------------------------------------------------


def test_gemini_request_shape_field_by_field() -> None:
    """URL 含模型名/x-goog-api-key/systemInstruction/inline_data/generationConfig。"""
    req = build_request(
        "gemini", "gemini", SYS, USER, [(PNG_1X1, "image/png")],
        model=MODELS["gemini"], api_key=KEY, base_url=BASE,
    )
    assert req["url"] == f"{BASE}/models/{MODELS['gemini']}:generateContent"
    assert req["headers"] == {
        "Content-Type": "application/json",
        "x-goog-api-key": KEY,
    }
    assert "Authorization" not in req["headers"]
    payload = req["payload"]
    assert "model" not in payload  # gemini 模型只在 URL,不进载荷
    assert payload["systemInstruction"] == {"parts": [{"text": SYS}]}
    contents = payload["contents"]
    assert len(contents) == 1 and contents[0]["role"] == "user"
    parts = contents[0]["parts"]
    assert parts[0] == {"text": USER}  # 有原生 JSON 能力,不加补偿行
    assert JSON_ONLY_HINT not in parts[0]["text"]
    inline = parts[1]["inline_data"]
    assert inline == {"mime_type": "image/png", "data": PNG_B64}
    assert base64.b64decode(inline["data"]) == PNG_1X1
    assert payload["generationConfig"] == {
        "responseMimeType": "application/json",
        "temperature": 0.1,
    }


def test_gemini_without_images_and_empty_system() -> None:
    """无图片时 parts 只有 text;空 system 省略 systemInstruction。"""
    req = build_request("gemini", "gemini", "", USER, [], api_key=KEY, base_url=BASE)
    payload = req["payload"]
    assert "systemInstruction" not in payload
    assert payload["contents"] == [{"role": "user", "parts": [{"text": USER}]}]


# ---------------------------------------------------------------------------
# 横切:多图顺序 / mime 兜底 / base_url 尾斜杠 / 默认参 / 补偿行归属
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("style", ["openai", "anthropic", "gemini"])
def test_multiple_images_appended_in_order(style: str) -> None:
    """多图按传入顺序追加在文本之后,mime 各自正确,base64 可还原。"""
    jpg_bytes = b"\xff\xd8\xff\xe0-fake-jpeg"
    req = build_request(
        style, style, SYS, USER,
        [(jpg_bytes, "image/jpeg"), (PNG_1X1, "image/png")],
        model=MODELS[style], api_key=KEY, base_url=BASE,
    )
    payload = req["payload"]
    if style == "openai":
        content = payload["messages"][-1]["content"]
        assert [part["type"] for part in content] == ["text", "image_url", "image_url"]
        urls = [part["image_url"]["url"] for part in content[1:]]
        assert urls[0].startswith("data:image/jpeg;base64,")
        assert urls[1] == f"data:image/png;base64,{PNG_B64}"
        assert base64.b64decode(urls[0].split(",", 1)[1]) == jpg_bytes
    elif style == "anthropic":
        blocks = payload["messages"][-1]["content"]
        assert [b["type"] for b in blocks] == ["text", "image", "image"]
        sources = [b["source"] for b in blocks[1:]]
        assert [s["media_type"] for s in sources] == ["image/jpeg", "image/png"]
        assert base64.b64decode(sources[0]["data"]) == jpg_bytes
        assert sources[1]["data"] == PNG_B64
    else:
        parts = payload["contents"][0]["parts"]
        assert len(parts) == 3 and parts[0] == {"text": USER}
        inlines = [p["inline_data"] for p in parts[1:]]
        assert [i["mime_type"] for i in inlines] == ["image/jpeg", "image/png"]
        assert base64.b64decode(inlines[0]["data"]) == jpg_bytes
        assert inlines[1]["data"] == PNG_B64


@pytest.mark.parametrize("style", ["openai", "anthropic", "gemini"])
def test_empty_mime_defaults_to_png(style: str) -> None:
    """mime 为空串时按 png 兜底(与 A62 encode_image 口径一致)。"""
    req = build_request(style, style, SYS, USER, [(PNG_1X1, "")], model="m", base_url=BASE)
    assert "image/png" in json.dumps(req["payload"], ensure_ascii=False)
    if style == "openai":
        data_uri = req["payload"]["messages"][-1]["content"][1]["image_url"]["url"]
        assert data_uri.startswith("data:image/png;base64,")


@pytest.mark.parametrize("style", ["openai", "anthropic", "gemini"])
def test_base_url_trailing_slash_stripped(style: str) -> None:
    """base_url 尾斜杠剥掉后再拼端点(与 A62 rstrip 口径一致)。"""
    with_slash = build_request(
        style, style, SYS, USER, [(PNG_1X1, "image/png")],
        model="m", api_key=KEY, base_url=BASE + "/",
    )
    without = build_request(
        style, style, SYS, USER, [(PNG_1X1, "image/png")],
        model="m", api_key=KEY, base_url=BASE,
    )
    assert with_slash["url"] == without["url"]
    assert with_slash["url"].startswith(BASE)


def test_default_arguments_and_empty_key() -> None:
    """默认 model="m" / api_key="k" / base_url="https://x";空密钥不加认证头。"""
    req = build_request("openai", "openai", SYS, USER, [])
    assert req["url"] == "https://x/chat/completions"
    assert req["payload"]["model"] == "m"
    assert req["headers"]["Authorization"] == "Bearer k"

    gemini = build_request("gemini", "gemini", SYS, USER, [], api_key="")
    assert gemini["url"] == "https://x/models/m:generateContent"
    assert "x-goog-api-key" not in gemini["headers"]

    anthropic = build_request("anthropic", "anthropic", SYS, USER, [], api_key="")
    assert "x-api-key" not in anthropic["headers"]
    assert anthropic["headers"]["anthropic-version"] == "2023-06-01"


@pytest.mark.parametrize(
    ("style", "expect_hint"),
    [
        ("openai", False),
        ("anthropic", True),
        ("gemini", False),
    ],
)
def test_compensation_line_matches_json_mode(style: str, expect_hint: bool) -> None:
    """补偿指令恰好出现在 json_mode=False 的方言(anthropic)的 user 文本前。"""
    req = build_request(style, style, SYS, USER, [], model="m", base_url=BASE)
    dumped = json.dumps(req["payload"], ensure_ascii=False)
    assert (JSON_ONLY_HINT in dumped) is expect_hint
    assert CAPS[style]["json_mode"] is not expect_hint


# ---------------------------------------------------------------------------
# 未知方言:ValueError(中文)
# ---------------------------------------------------------------------------


def test_unknown_style_raises_value_error() -> None:
    """build_request 与 parse_response 对未知 style 均抛中文 ValueError。"""
    with pytest.raises(ValueError, match="方言未知"):
        build_request("azure", "azure", SYS, USER, [])
    with pytest.raises(ValueError, match="方言未知"):
        parse_response("azure", {})


def test_unknown_style_error_lists_styles_and_provider() -> None:
    """错误消息列出全部可选项,并带上提供方名便于排障。"""
    with pytest.raises(ValueError, match="openai/anthropic/gemini"):
        build_request("litellm", "某提供方", SYS, USER, [])
    with pytest.raises(ValueError, match="提供方 某提供方"):
        build_request("litellm", "某提供方", SYS, USER, [])


# ---------------------------------------------------------------------------
# parse_response:三种形态提取
# ---------------------------------------------------------------------------


def test_parse_response_openai_text() -> None:
    body = {"choices": [{"message": {"role": "assistant", "content": '{"nsfw_prob": 0.9}'}}]}
    assert parse_response("openai", body) == '{"nsfw_prob": 0.9}'


def test_parse_response_anthropic_concat_and_skips_non_text() -> None:
    """content 列表 text 块拼接,非 text 块(如 image)跳过。"""
    body = {
        "content": [
            {"type": "text", "text": "第一段"},
            {"type": "image", "source": {"type": "base64"}},
            {"type": "text", "text": "第二段"},
        ]
    }
    assert parse_response("anthropic", body) == "第一段第二段"


def test_parse_response_gemini_concat() -> None:
    body = {"candidates": [{"content": {"role": "model", "parts": [{"text": "A"}, {"text": "B"}]}}]}
    assert parse_response("gemini", body) == "AB"


@pytest.mark.parametrize(
    ("style", "body"),
    [
        # openai:choices 缺失 / 空列表 / 元素非字典 / 缺 message / content 非字符串
        ("openai", {}),
        ("openai", {"choices": []}),
        ("openai", {"choices": ["x"]}),
        ("openai", {"choices": [{}]}),
        ("openai", {"choices": [{"message": {}}]}),
        ("openai", {"choices": [{"message": {"content": {"a": 1}}}]}),
        ("openai", {"choices": [{"message": {"content": None}}]}),
        # anthropic:content 缺失 / 非列表 / 空列表 / 元素非字典
        ("anthropic", {}),
        ("anthropic", {"content": "不是列表"}),
        ("anthropic", {"content": []}),
        ("anthropic", {"content": [42]}),
        # gemini:candidates 缺失 / 空列表 / 元素非字典 / content 非字典 / parts 非列表或空
        ("gemini", {}),
        ("gemini", {"candidates": []}),
        ("gemini", {"candidates": [{}]}),
        ("gemini", {"candidates": [{"content": "x"}]}),
        ("gemini", {"candidates": [{"content": {}}]}),
        ("gemini", {"candidates": [{"content": {"parts": None}}]}),
        ("gemini", {"candidates": [{"content": {"parts": []}}]}),
    ],
)
def test_parse_response_malformed_returns_none(style: str, body: dict) -> None:
    """形态不符一律返回 None,绝不抛出。"""
    assert parse_response(style, body) is None


@pytest.mark.parametrize("bad_body", ["choices", None, 123, []])
def test_parse_response_non_dict_body_returns_none(bad_body) -> None:
    """body 不是 dict(调用方已 json.loads 失败的兜底场景)返回 None。"""
    assert parse_response("openai", bad_body) is None


# ---------------------------------------------------------------------------
# 与 A62 vlm_client 的同构一致性对照(importorskip 保护,mock 传输零外呼)
# ---------------------------------------------------------------------------


@dataclass
class FakeResolved:
    """A61 ResolvedProvider 的鸭子替身(仅提供 A62 客户端读取的六个属性)。"""

    provider: str
    base_url: str
    model: str
    api_key: str
    style: str
    local: bool = False


class CaptureTransport:
    """捕获请求并按方言回放一个合法 200 响应(零外呼)。"""

    def __init__(self, body: str) -> None:
        self.body = body
        self.calls: list[dict] = []

    def __call__(self, url, headers, payload, timeout=90.0):
        self.calls.append(
            {"url": url, "headers": dict(headers), "payload": copy.deepcopy(payload)}
        )
        return 200, self.body


def a62_ok_body(style: str) -> str:
    """按方言构造 A62 客户端能成功解析的 200 响应体。"""
    if style == "openai":
        return json.dumps({"choices": [{"message": {"content": '{"a": 1}'}}]})
    if style == "anthropic":
        return json.dumps({"content": [{"type": "text", "text": '{"a": 1}'}]})
    return json.dumps({"candidates": [{"content": {"parts": [{"text": '{"a": 1}'}]}}]})


A62_MSGS = [
    {"role": "system", "content": SYS},
    {"role": "user", "content": USER},
]


def a62_captured_request(tmp_path, monkeypatch, *, style: str, image: tuple[bytes, str] | None):
    """用 A62 UniversalVLMClient + CaptureTransport 发一次(mock)请求,返回捕获结果。"""
    vlm = pytest.importorskip("netsentinel.vision.vlm_client")
    # 隔离 A64 provider_quirks:保证对照的是 A62 纯净的方言渲染
    monkeypatch.setattr(vlm, "_load_provider_quirks", lambda: None)

    transport = CaptureTransport(a62_ok_body(style))
    resolved = FakeResolved(
        provider=style, base_url=BASE, model=MODELS[style], api_key=KEY, style=style
    )
    client = vlm.UniversalVLMClient(resolved, Config(vlm_online=True), transport=transport)
    image_paths = None
    if image is not None:
        data, mime = image
        path = tmp_path / ("pic." + mime.split("/", 1)[1])
        path.write_bytes(data)
        image_paths = [str(path)]
    assert client.chat_json(A62_MSGS, image_paths=image_paths) == {"a": 1}
    assert len(transport.calls) == 1
    return transport.calls[0]


def _assert_json_equal(mine: dict, theirs: dict) -> None:
    """payload JSON 精确比较(dict 深比较 + 序列化逐字符比较,键序无关)。"""
    assert mine == theirs
    assert json.dumps(mine, sort_keys=True, ensure_ascii=False) == json.dumps(
        theirs, sort_keys=True, ensure_ascii=False
    )


def test_consistency_with_vlm_client_openai(tmp_path, monkeypatch) -> None:
    """同一输入下,本模块与 A62 真实请求 url/headers/payload 完全等价(openai)。"""
    call = a62_captured_request(
        tmp_path, monkeypatch, style="openai", image=(PNG_1X1, "image/png")
    )
    mine = build_request(
        "openai", "openai", SYS, USER, [(PNG_1X1, "image/png")],
        model=MODELS["openai"], api_key=KEY, base_url=BASE,
    )
    assert mine["url"] == call["url"]
    assert mine["headers"] == call["headers"]
    _assert_json_equal(mine["payload"], call["payload"])


def test_consistency_with_vlm_client_gemini(tmp_path, monkeypatch) -> None:
    """同一输入下,本模块与 A62 真实请求 url/headers/payload 完全等价(gemini)。"""
    call = a62_captured_request(
        tmp_path, monkeypatch, style="gemini", image=(PNG_1X1, "image/png")
    )
    mine = build_request(
        "gemini", "gemini", SYS, USER, [(PNG_1X1, "image/png")],
        model=MODELS["gemini"], api_key=KEY, base_url=BASE,
    )
    assert mine["url"] == call["url"]
    assert mine["headers"] == call["headers"]
    _assert_json_equal(mine["payload"], call["payload"])


def test_consistency_with_vlm_client_anthropic(tmp_path, monkeypatch) -> None:
    """anthropic:剥离任务书规定的 JSON 补偿行后,与 A62 载荷完全等价。"""
    call = a62_captured_request(
        tmp_path, monkeypatch, style="anthropic", image=(PNG_1X1, "image/png")
    )
    mine = build_request(
        "anthropic", "anthropic", SYS, USER, [(PNG_1X1, "image/png")],
        model=MODELS["anthropic"], api_key=KEY, base_url=BASE,
    )
    assert mine["url"] == call["url"]
    assert mine["headers"] == call["headers"]

    payload = copy.deepcopy(mine["payload"])
    text_block = payload["messages"][-1]["content"][0]
    # 差异仅限补偿行:user 文本 = 补偿指令一行 + A62 的原 user 文本
    assert text_block == {
        "type": "text",
        "text": f"{JSON_ONLY_HINT}\n{call['payload']['messages'][-1]['content'][0]['text']}",
    }
    text_block["text"] = call["payload"]["messages"][-1]["content"][0]["text"]
    _assert_json_equal(payload, call["payload"])


@pytest.mark.parametrize("style", ["openai", "anthropic", "gemini"])
def test_consistency_with_vlm_client_without_images(tmp_path, monkeypatch, style: str) -> None:
    """无图片场景三方言同样等价(anthropic 的补偿行按同口径剥离)。"""
    call = a62_captured_request(tmp_path, monkeypatch, style=style, image=None)
    mine = build_request(
        style, style, SYS, USER, [],
        model=MODELS[style], api_key=KEY, base_url=BASE,
    )
    assert mine["url"] == call["url"]
    assert mine["headers"] == call["headers"]

    payload = copy.deepcopy(mine["payload"])
    if style == "anthropic":  # 无图时 content 是纯字符串,剥离补偿行后比较
        assert payload["messages"][-1]["content"] == (
            f"{JSON_ONLY_HINT}\n{call['payload']['messages'][-1]['content']}"
        )
        payload["messages"][-1]["content"] = call["payload"]["messages"][-1]["content"]
    _assert_json_equal(payload, call["payload"])


# ---------------------------------------------------------------------------
# V5 升级锁定:CAPS 冻结 / 每图恰好一次 base64 编码 / dialect.build 遥测
# ---------------------------------------------------------------------------


def test_v5_caps_frozen_readonly() -> None:
    """能力矩阵双层冻结:外层与内层均不可写、不可删,读语义不变。"""
    with pytest.raises(TypeError):
        CAPS["openai"] = {"json_mode": False, "system_top": True}  # type: ignore[index]
    with pytest.raises(TypeError):
        del CAPS["anthropic"]  # type: ignore[misc]
    with pytest.raises(TypeError):
        CAPS["gemini"]["json_mode"] = False  # type: ignore[index]
    assert CAPS["openai"]["json_mode"] is True
    assert tuple(CAPS) == ("openai", "anthropic", "gemini")


def test_v5_single_base64_encode_per_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """每张图恰好一次 b64 编码:三图三编、无图零编(编码单次产出锁定)。"""
    from netsentinel.vision import prompt_dialects as pd

    real_encode = base64.b64encode
    counter = {"n": 0}

    def counting_encode(data: bytes) -> bytes:
        counter["n"] += 1
        return real_encode(data)

    monkeypatch.setattr(pd.base64, "b64encode", counting_encode)
    images = [(PNG_1X1, "image/png"), (b"\xff\xd8\xff\xe0-fake", "image/jpeg"), (b"zz", "")]
    build_request("openai", "openai", SYS, USER, images, model="m", base_url=BASE)
    assert counter["n"] == 3
    counter["n"] = 0
    build_request("anthropic", "anthropic", SYS, USER, images, model="m", base_url=BASE)
    assert counter["n"] == 3
    counter["n"] = 0
    build_request("gemini", "gemini", SYS, USER, [], model="m", base_url=BASE)
    assert counter["n"] == 0


def test_v5_dialect_build_telemetry() -> None:
    """成功渲染一次计 dialect.build;未知方言抛 ValueError 不计数。"""
    from netsentinel import telemetry

    telemetry.reset()
    try:
        for style in ("openai", "anthropic", "gemini"):
            build_request(style, style, SYS, USER, [], model="m", base_url=BASE)
        assert telemetry.snapshot()["counters"].get("dialect.build") == 3
        with pytest.raises(ValueError, match="方言未知"):
            build_request("azure", "azure", SYS, USER, [])
        assert telemetry.snapshot()["counters"].get("dialect.build") == 3
    finally:
        telemetry.reset()
