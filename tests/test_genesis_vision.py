# -*- coding: utf-8 -*-
"""创世纪 G2 · vision 传输层升级测试(V17,全离线零外呼)。

锁定三件事:

1. **prompt_dialects 渲染缓存命中等价**:同 ``(图片字节, mime)`` 重复渲染
   同输入同输出、零重编码(``cache_info`` 命中数与 ``b64encode`` 计数双证),
   跨方言渲染同图共享编码,且每次渲染仍返回全新可变 dict;
2. **response_repair 正则预编译等价**:预编译后的 ``_builtin_candidates``
   与"逐条内联构建"的参照实现(升级前写法逐字符复刻)在全语料 + 合成脏
   样本上逐字符一致;兜底解析器跑全量语料全过;
3. **密钥绝不进缓存**:带 ``api_key`` 的入口 ``build_request`` 与 dict 入参
   的 ``parse_response`` 均未被 lru_cache 包装;模块内所有缓存函数形参名
   不含密钥类字样;换密钥重复渲染时认证头逐次正确、图片缓存照常命中
   (密钥不是任何缓存键的组成部分)。

放弃项核实证据:vlm_client 重试不重复序列化已由 tests/test_vlm_client.py 的
``test_v5_default_transport_serializes_payload_once`` 锁定(V5);本文件另锁
"(V17)成功路径零冗余 json.dumps"(错误片段 snippet 惰性化)。provider_throttle
经核实无真实重复计算(稳态每次取令牌恰一次时钟读 + O(1) 浮点补充),
不加缓存(宁缺毋滥)。
"""
from __future__ import annotations

import base64
import inspect
import json
import re
import sys
from dataclasses import dataclass

import pytest

from netsentinel.contracts import Config
from netsentinel.vision import prompt_dialects as pd
from netsentinel.vision import response_repair as rr
from netsentinel.vision.prompt_dialects import build_request, parse_response
from netsentinel.vision.response_repair import CORPUS, run_corpus

SYS = "你是图片审核助手,只输出 JSON。"
USER = "请审核这张图片。"
KEY_A = "sk-genesis-key-AAAAAAAAAAAA"
KEY_B = "sk-genesis-key-BBBBBBBBBBBB"
BASE = "https://api.example.com/v1"

#: 最小合法 1x1 灰度 PNG(与 A72 测试同款夹具)
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptV"
    "AAAACklEQVR4nGNgAAAAAgABSK+kcQAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG_1X1).decode("ascii")


@pytest.fixture(autouse=True)
def _clear_render_caches():
    """每条用例前后清空渲染缓存与修复器单槽,保证计数彼此独立。"""
    pd._encode_image.cache_clear()
    pd._data_uri.cache_clear()
    yield
    pd._encode_image.cache_clear()
    pd._data_uri.cache_clear()
    rr._PARSER_CACHE.clear()


def _counting_b64encode(monkeypatch: pytest.MonkeyPatch) -> dict:
    """把 pd.base64.b64encode 换成计数版(返回真实结果),返回计数容器。"""
    real_encode = base64.b64encode
    counter = {"n": 0}

    def counting_encode(data: bytes) -> bytes:
        counter["n"] += 1
        return real_encode(data)

    monkeypatch.setattr(pd.base64, "b64encode", counting_encode)
    return counter


# ---------------------------------------------------------------------------
# ① 渲染缓存:命中等价 + 零重算 + 跨方言共享
# ---------------------------------------------------------------------------


def test_encode_image_cache_hit_equivalence(monkeypatch: pytest.MonkeyPatch) -> None:
    """同 (图片字节, mime) 二次调用:同结果、b64encode 零重算、命中数 +1。"""
    counter = _counting_b64encode(monkeypatch)
    expected = ("image/png", PNG_B64)

    first = pd._encode_image(PNG_1X1, "image/png")
    assert first == expected
    assert counter["n"] == 1
    before = pd._encode_image.cache_info()
    assert before.misses == 1 and before.hits == 0

    second = pd._encode_image(PNG_1X1, "image/png")
    assert second == first  # 缓存命中:同输入同输出
    assert counter["n"] == 1, "缓存命中不应重新编码"
    after = pd._encode_image.cache_info()
    assert after.hits == before.hits + 1
    assert after.misses == before.misses  # 无新未命中
    # 不同 mime 是不同缓存键(空串兜底为 png 也独立成键)
    pd._encode_image(PNG_1X1, "")
    assert counter["n"] == 2
    assert pd._encode_image(PNG_1X1, "") == ("image/png", PNG_B64)


def test_data_uri_cache_hit_equivalence() -> None:
    """data URI 拼接缓存:同 (mime, 裸 base64) 同输出,命中数 +1。"""
    assert pd._data_uri("image/png", PNG_B64) == f"data:image/png;base64,{PNG_B64}"
    before = pd._data_uri.cache_info()
    again = pd._data_uri("image/png", PNG_B64)
    assert again == f"data:image/png;base64,{PNG_B64}"
    after = pd._data_uri.cache_info()
    assert after.hits == before.hits + 1


def test_repeat_render_same_output_and_no_reencode(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一请求渲染两次:结果深比较一致、图片只编码一次、dict 各自全新。"""
    counter = _counting_b64encode(monkeypatch)
    images = [(PNG_1X1, "image/png"), (b"\xff\xd8\xff\xe0-fake", "image/jpeg")]
    args = ("openai", "openai", SYS, USER, images)
    kwargs = {"model": "m", "api_key": KEY_A, "base_url": BASE}

    first = build_request(*args, **kwargs)
    second = build_request(*args, **kwargs)
    assert first == second  # 同输入同输出(含 headers / payload 深比较)
    assert counter["n"] == 2, "两图两次渲染:第二次应全部缓存命中,只编码 2 次"
    # 返回的必须是全新可变结构(调用方可放心修改,不污染缓存或彼此)
    assert first is not second
    assert first["payload"] is not second["payload"]
    assert first["payload"]["messages"] is not second["payload"]["messages"]


def test_cross_dialect_render_shares_image_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """同图换方言渲染(openai→anthropic→gemini):base64 只编码一次且形态正确。"""
    counter = _counting_b64encode(monkeypatch)
    results = {
        style: build_request(
            style, style, SYS, USER, [(PNG_1X1, "image/png")],
            model="m", api_key=KEY_A, base_url=BASE,
        )
        for style in ("openai", "anthropic", "gemini")
    }
    assert counter["n"] == 1, "跨方言共享编码:同一图片只编码一次"
    dumped = {style: json.dumps(req["payload"]) for style, req in results.items()}
    assert PNG_B64 in dumped["openai"]
    assert PNG_B64 in dumped["anthropic"]
    assert PNG_B64 in dumped["gemini"]
    assert f"data:image/png;base64,{PNG_B64}" in dumped["openai"]  # data URI 拼接正确


def test_same_bytes_twice_in_one_call_still_two_blocks() -> None:
    """单次渲染传同一 bytes 两遍:仍产出两个相同图片块(输出形态不变)。"""
    req = build_request(
        "anthropic", "anthropic", SYS, USER,
        [(PNG_1X1, "image/png"), (PNG_1X1, "image/png")],
        model="m", api_key=KEY_A, base_url=BASE,
    )
    blocks = req["payload"]["messages"][-1]["content"]
    assert [b["type"] for b in blocks] == ["text", "image", "image"]
    assert blocks[1] == blocks[2]
    assert blocks[1]["source"]["data"] == PNG_B64


# ---------------------------------------------------------------------------
# ② response_repair:正则预编译与逐条构建全量等价
# ---------------------------------------------------------------------------


def _inline_candidates(s: str):
    """升级前写法逐字符复刻的参照:每次调用内联 re.sub 临时构建。"""
    yield s
    fixed = re.sub(r"\bTrue\b", "true", s)
    fixed = re.sub(r"\bFalse\b", "false", fixed)
    fixed = re.sub(r"\bNone\b", "null", fixed)
    fixed = re.sub(r",\s*([}\])])", r"\1", fixed)
    yield fixed
    yield rr._builtin_fix_single_quotes(fixed)


#: 合成脏样本:字面量 / 尾逗号 / 撇号 / 中文引号归一化后的组合形态
_SYNTHETIC_CANDIDATES = [
    "{'nsfw_prob': 0.5, 'flag': True, 'ok': False, 'x': None}",
    '{"nsfw_prob": 0.5, "lits": [True, False, None],}',
    "{'s': \"it's None, True story\", 'n': None,}",
    '{"a": True, "b": [False,], "c": {"d": None,}}',
    '{"no_literals": 0.1, "plain": "无字面量"}',
]


def _corpus_candidate_strings() -> list[str]:
    """全语料按兜底解析器同口径(剥围栏→取首个平衡对象)提取候选串。"""
    candidates: list[str] = []
    for case in CORPUS:
        text = case.text
        match = rr._BUILTIN_FENCE_RE.search(text)
        body = match.group(1) if match else text
        got = rr._builtin_extract_balanced(body)
        if got is not None:
            candidates.append(got)
    return candidates


def test_precompiled_literals_are_module_level_patterns() -> None:
    """三个字面量修复正则为模块级预编译 Pattern,且 pattern 串逐字符一致。"""
    assert isinstance(rr._BUILTIN_TRUE_RE, re.Pattern)
    assert isinstance(rr._BUILTIN_FALSE_RE, re.Pattern)
    assert isinstance(rr._BUILTIN_NONE_RE, re.Pattern)
    assert rr._BUILTIN_TRUE_RE.pattern == r"\bTrue\b"
    assert rr._BUILTIN_FALSE_RE.pattern == r"\bFalse\b"
    assert rr._BUILTIN_NONE_RE.pattern == r"\bNone\b"


def test_precompiled_candidates_match_inline_reference() -> None:
    """预编译候选链 == 逐条内联构建:全语料候选串 + 合成脏样本逐字符一致。"""
    samples = _corpus_candidate_strings() + _SYNTHETIC_CANDIDATES
    assert len(samples) >= 60, "样本量应覆盖全语料的绝大多数候选串"
    for sample in samples:
        precompiled = list(rr._builtin_candidates(sample))
        inline = list(_inline_candidates(sample))
        assert precompiled == inline, f"预编译与内联构建不一致:{sample!r}"


def test_builtin_parser_corpus_equivalence(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底解析器(预编译正则)跑全量语料:与逐条期望全等(88/88)。"""
    monkeypatch.setitem(sys.modules, "netsentinel.vision.vlm_prompts", None)
    rr._PARSER_CACHE.clear()
    try:
        assert rr._PARSER_CACHE == []
        assert rr._parser() is rr._builtin_parse_json_response
        result = run_corpus()
        assert result == {"total": len(CORPUS), "passed": len(CORPUS), "failed": []}
    finally:
        rr._PARSER_CACHE.clear()


def test_repair_semantics_unchanged_after_precompile() -> None:
    """主解析器路径修复语义不变:全语料全过 + 关键形态抽查。"""
    assert run_corpus() == {"total": len(CORPUS), "passed": len(CORPUS), "failed": []}
    assert rr.repair('{"nsfw_prob": 0.42}') == {"nsfw_prob": 0.42}
    assert rr.repair("{'nsfw_prob': 0.34, 'flag': True}") == {
        "nsfw_prob": 0.34,
        "flag": True,
    }
    assert rr.repair('{"nsfw_prob": 0.') is None  # 截断不硬修


# ---------------------------------------------------------------------------
# ③ 密钥绝不进缓存
# ---------------------------------------------------------------------------


def test_key_bearing_entries_are_not_cached() -> None:
    """带 api_key 的 build_request 与 dict 入参的 parse_response 均未被缓存。"""
    assert not hasattr(build_request, "cache_info")
    assert not hasattr(build_request, "cache_clear")
    assert not hasattr(parse_response, "cache_info")
    assert not hasattr(parse_response, "cache_clear")


def test_cached_function_params_contain_no_key_material() -> None:
    """模块内所有 lru_cache 包装函数的形参名不含密钥类字样(静态核查)。"""
    cached = [
        (name, obj)
        for name, obj in vars(pd).items()
        if callable(obj) and hasattr(obj, "cache_info")
    ]
    names = sorted(name for name, _ in cached)
    assert names == ["_data_uri", "_encode_image"], f"意外的新缓存函数:{names}"
    for name, fn in cached:
        params = " ".join(inspect.signature(fn).parameters).lower()
        for banned in ("key", "token", "secret", "auth"):
            assert banned not in params, f"{name} 的形参含密钥类字样:{params}"


@pytest.mark.parametrize("style", ["openai", "anthropic", "gemini"])
def test_changing_key_keeps_cache_hits_and_correct_headers(style: str) -> None:
    """换密钥重复渲染:认证头各按当次密钥生成,图片编码缓存照常命中。"""
    pd._encode_image.cache_clear()
    first = build_request(
        style, style, SYS, USER, [(PNG_1X1, "image/png")],
        model="m", api_key=KEY_A, base_url=BASE,
    )
    hits_after_first = pd._encode_image.cache_info().hits
    assert hits_after_first == 0  # 首次渲染:未命中

    second = build_request(
        style, style, SYS, USER, [(PNG_1X1, "image/png")],
        model="m", api_key=KEY_B, base_url=BASE,
    )
    # 认证头逐次正确(密钥永远实时进入 headers,不来自任何缓存)
    auth = {
        "openai": ("Authorization", f"Bearer {KEY_B}"),
        "anthropic": ("x-api-key", KEY_B),
        "gemini": ("x-goog-api-key", KEY_B),
    }[style]
    assert second["headers"][auth[0]] == auth[1]
    assert first["headers"][auth[0]] != second["headers"][auth[0]]
    # 图片缓存照常命中 +1:密钥不是图片缓存键的组成部分
    assert pd._encode_image.cache_info().hits == hits_after_first + 1
    # 载荷与密钥无关,两次完全一致
    assert first["payload"] == second["payload"]
    assert KEY_A not in json.dumps(second)
    assert KEY_B not in json.dumps(first)


# ---------------------------------------------------------------------------
# ④ vlm_client:成功路径零冗余序列化(V17 snippet 惰性化)
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


def test_vlm_client_success_path_no_redundant_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    """成功提取不再为错误片段预序列化整份响应体:全程零 json.dumps。"""
    vlm = pytest.importorskip("netsentinel.vision.vlm_client")
    monkeypatch.setattr(vlm, "_load_provider_quirks", lambda: None)

    body = json.dumps({"choices": [{"message": {"content": '{"nsfw_prob": 0.1}'}}]})

    real_dumps = json.dumps
    counter = {"n": 0}

    def counting_dumps(*args, **kwargs):
        counter["n"] += 1
        return real_dumps(*args, **kwargs)

    monkeypatch.setattr(vlm.json, "dumps", counting_dumps)

    def transport(url, headers, payload, timeout=90.0):
        return 200, body

    resolved = FakeResolved(
        provider="openai", base_url=BASE, model="gpt-4o-mini",
        api_key=KEY_A, style="openai",
    )
    client = vlm.UniversalVLMClient(resolved, Config(vlm_online=True), transport=transport)
    assert client.chat_json([{"role": "user", "content": USER}]) == {"nsfw_prob": 0.1}
    assert counter["n"] == 0, "成功路径不应有任何冗余 json.dumps(snippet 惰性化)"


def test_vlm_client_error_snippet_still_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    """失败分支的响应片段照常携带且经打码:惰性化只省掉成功路径的开销。"""
    vlm = pytest.importorskip("netsentinel.vision.vlm_client")
    monkeypatch.setattr(vlm, "_load_provider_quirks", lambda: None)

    body = json.dumps({"unexpected": "形态不符", "key_material": KEY_A})

    def transport(url, headers, payload, timeout=90.0):
        return 200, body

    resolved = FakeResolved(
        provider="openai", base_url=BASE, model="gpt-4o-mini",
        api_key=KEY_A, style="openai",
    )
    client = vlm.UniversalVLMClient(resolved, Config(vlm_online=True), transport=transport)
    with pytest.raises(RuntimeError, match="返回缺少 choices") as excinfo:
        client.chat_json([{"role": "user", "content": USER}])
    message = str(excinfo.value)
    assert KEY_A not in message, "红线 17:密钥绝不进异常消息(打码后片段)"
    assert "****" in message
