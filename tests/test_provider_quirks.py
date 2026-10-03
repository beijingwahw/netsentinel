# -*- coding: utf-8 -*-
"""A64 provider_quirks 测试:应用/透传/不改入参/结构校验(全程离线、零外呼)。

覆盖契约 §4 A64 的测试要求:quirks 应用、无 quirk 提供方透传、未知提供方透传,
外加:深拷贝不改入参、附加头不覆盖既有头(含 Authorization 与大小写变体)、
QUIRKS 键集合 ⊆ providers.PROVIDERS(A61 未就位时 importorskip 跳过)、
每家 notes 非空。
"""
from __future__ import annotations

import copy

import pytest

from netsentinel.vision import provider_quirks
from netsentinel.vision.provider_quirks import (
    QUIRKS,
    apply_quirks,
    quirk_notes,
)


# ---------------------------------------------------------------------------
# 结构校验
# ---------------------------------------------------------------------------
class TestQuirksStructure:
    """QUIRKS 表本身的形态与收录范围。"""

    def test_covers_domestic_nine_plus_openrouter(self) -> None:
        """9 家国内平台 + openrouter 必须全部收录(至少含 notes)。"""
        required = {
            "glm", "qwen", "doubao", "hunyuan", "moonshot",
            "minimax", "stepfun", "siliconflow", "ernie", "openrouter",
        }
        assert required <= set(QUIRKS)

    def test_only_allowed_keys(self) -> None:
        """每家条目的键只能是四个合法键之一(防手滑写错键名导致静默失效)。"""
        allowed = {"extra_headers", "model_aliases", "response_quirk", "notes"}
        for provider, quirk in QUIRKS.items():
            assert set(quirk) <= allowed, f"{provider} 含非法键:{set(quirk) - allowed}"

    def test_every_provider_has_nonempty_notes(self) -> None:
        """每家 notes 必须非空中文(契约:notes 至少 1 句,含『以官方为准』类口径)。"""
        for provider, quirk in QUIRKS.items():
            notes = quirk.get("notes")
            assert isinstance(notes, str) and notes.strip(), f"{provider} 缺少 notes"
            assert quirk_notes(provider) == notes

    def test_field_types(self) -> None:
        """extra_headers/model_aliases 为 str->str 映射;response_quirk 为 str。"""
        for provider, quirk in QUIRKS.items():
            for field in ("extra_headers", "model_aliases"):
                value = quirk.get(field)
                if value is not None:
                    assert isinstance(value, dict), f"{provider}.{field} 应为 dict"
                    assert all(
                        isinstance(k, str) and isinstance(v, str)
                        for k, v in value.items()
                    ), f"{provider}.{field} 的键值均应为 str"
            rq = quirk.get("response_quirk")
            assert rq is None or isinstance(rq, str), f"{provider}.response_quirk 应为 str"

    def test_openrouter_extra_headers_confirmed_by_docs(self) -> None:
        """openrouter 必须带经官方文档确证的可选归因头 X-Title。"""
        assert QUIRKS["openrouter"]["extra_headers"]["X-Title"] == "NetSentinel"

    def test_quirks_keys_subset_of_catalog(self) -> None:
        """QUIRKS 键集合 ⊆ providers.PROVIDERS(A61 未就位时跳过本断言)。"""
        providers = pytest.importorskip("netsentinel.vision.providers")
        catalog = set(getattr(providers, "PROVIDERS", {}))
        assert catalog, "providers.PROVIDERS 不应为空"
        extra = set(QUIRKS) - catalog
        assert not extra, f"QUIRKS 收录了目录之外的提供方:{extra}"


# ---------------------------------------------------------------------------
# apply_quirks:不改入参(深拷贝)
# ---------------------------------------------------------------------------
class TestNoMutation:
    """apply_quirks 绝不就地修改入参(含嵌套结构)。"""

    def test_inputs_untouched(self) -> None:
        headers = {
            "Authorization": "Bearer sk-test",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "qwen/qwen2.5-vl-72b-instruct:free",
            "messages": [
                {"role": "system", "content": "只输出 JSON"},
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}}]},
            ],
            "response_format": {"type": "json_object"},
        }
        headers_snapshot = copy.deepcopy(headers)
        payload_snapshot = copy.deepcopy(payload)

        new_headers, new_payload = apply_quirks("openrouter", headers, payload)

        assert headers == headers_snapshot
        assert payload == payload_snapshot
        # 返回的是新对象,与入参无共享顶层(嵌套亦不共享)
        assert new_headers is not headers
        assert new_payload is not payload
        new_payload["messages"][0]["content"] = "被改也不许影响入参"
        assert payload["messages"][0]["content"] == "只输出 JSON"


# ---------------------------------------------------------------------------
# apply_quirks:附加头合并与不覆盖
# ---------------------------------------------------------------------------
class TestHeaderMerge:
    """附加头按『未被占用才附加』合并,既有头(尤其 Authorization)绝不覆盖。"""

    def test_extra_headers_appended(self) -> None:
        headers = {"Authorization": "Bearer sk-or"}
        new_headers, _ = apply_quirks("openrouter", headers, {"model": "m"})
        assert new_headers["X-Title"] == "NetSentinel"
        assert new_headers["Authorization"] == "Bearer sk-or"  # 既有头原样保留

    def test_existing_header_not_overridden(self) -> None:
        """调用方已设 X-Title 时,quirks 附加值不得覆盖。"""
        headers = {"X-Title": "我的自定义应用"}
        new_headers, _ = apply_quirks("openrouter", headers, {})
        assert new_headers["X-Title"] == "我的自定义应用"
        assert len([k for k in new_headers if k.lower() == "x-title"]) == 1

    def test_header_name_comparison_case_insensitive(self) -> None:
        """头名大小写不敏感:已有小写 x-title 也不产生重复大写键。"""
        headers = {"x-title": "mine"}
        new_headers, _ = apply_quirks("openrouter", headers, {})
        assert new_headers["x-title"] == "mine"
        assert [k for k in new_headers if k.lower() == "x-title"] == ["x-title"]


# ---------------------------------------------------------------------------
# apply_quirks:透传语义
# ---------------------------------------------------------------------------
class TestPassthrough:
    """无 quirk 提供方与未知提供方:内容原样、仍返回深拷贝。"""

    def test_unknown_provider_passthrough(self) -> None:
        headers = {"Authorization": "Bearer k"}
        payload = {"model": "whatever", "messages": [{"role": "user", "content": "hi"}]}
        new_headers, new_payload = apply_quirks("no-such-provider", headers, payload)
        assert new_headers == headers
        assert new_payload == payload
        assert new_headers is not headers and new_payload is not payload

    def test_notes_only_provider_passthrough(self) -> None:
        """只含 notes 的提供方(如 glm):头与体原样返回(仅拷贝)。"""
        for provider in ("glm", "qwen", "hunyuan", "stepfun"):
            assert "extra_headers" not in QUIRKS[provider]
        headers = {"Authorization": "Bearer k", "Content-Type": "application/json"}
        payload = {"model": "glm-5.3-flash", "temperature": 0.1}
        new_headers, new_payload = apply_quirks("glm", headers, payload)
        assert new_headers == headers
        assert new_payload == payload

    def test_payload_transparent_even_with_quirk(self) -> None:
        """有附加头的提供方,payload 也不被改动(当前实现为透传+预留)。"""
        payload = {"model": "m", "max_tokens": 1024}
        _, new_payload = apply_quirks("openrouter", {"Authorization": "Bearer k"}, payload)
        assert new_payload == payload

    def test_payload_defaults_only_fill_missing(self) -> None:
        """预留注入表语义锁定:只补缺失字段,不改已有值(当前为空表)。"""
        assert provider_quirks.PAYLOAD_DEFAULTS == {}
        # 用临时表锁定『缺键才补』语义,避免未来填表时悄悄变成覆盖
        provider_quirks.PAYLOAD_DEFAULTS["__test__"] = {"temperature": 0.1}
        try:
            _, p1 = apply_quirks("__test__", {}, {"model": "m"})
            assert p1["temperature"] == 0.1
            _, p2 = apply_quirks("__test__", {}, {"model": "m", "temperature": 0.9})
            assert p2["temperature"] == 0.9
        finally:
            provider_quirks.PAYLOAD_DEFAULTS.clear()


# ---------------------------------------------------------------------------
# quirk_notes
# ---------------------------------------------------------------------------
class TestQuirkNotes:
    """quirk_notes:收录提供方返回非空中文;未收录返回空串。"""

    def test_known_provider_returns_notes(self) -> None:
        for provider in QUIRKS:
            assert quirk_notes(provider).strip(), f"{provider} 的 notes 为空"

    def test_unknown_provider_returns_empty(self) -> None:
        assert quirk_notes("no-such-provider") == ""

    def test_domestic_notes_mention_official_docs(self) -> None:
        """国内平台的备注须带『以官方…为准』口径(红线 18 的落点)。"""
        for provider in QUIRKS:
            assert "以官方" in quirk_notes(provider), f"{provider} notes 缺少『以官方为准』口径"


# ---------------------------------------------------------------------------
# V5:深拷贝 → 等价拷贝(容器逐层重建 + 不可变叶子共享)
# ---------------------------------------------------------------------------
class TestV5SharedCopy:
    """V5 性能升级锁定:不再 deepcopy,但容器层级隔离语义保持不变。"""

    def test_v5_deepcopy_no_longer_used(self, monkeypatch) -> None:
        """copy.deepcopy 被禁用后 apply_quirks 仍正常工作(改用 _shared_copy)。"""
        import copy as copy_mod

        def _boom(*args, **kwargs):  # pragma: no cover - 触发即失败
            raise AssertionError("V5 后 apply_quirks 不应再调用 copy.deepcopy")

        monkeypatch.setattr(copy_mod, "deepcopy", _boom)
        headers = {"Authorization": "Bearer k"}
        payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        new_h, new_p = apply_quirks("openrouter", headers, payload)
        assert new_h["X-Title"] == "NetSentinel"
        assert new_p == payload

    def test_v5_isolation_at_every_container_level(self) -> None:
        """返回对象在任意容器层级(dict/list/嵌套 dict)都不与入参共享。"""
        payload = {
            "model": "m",
            "messages": [
                {"role": "user", "content": [{"type": "image_url",
                                              "image_url": {"url": "data:..."}}]},
            ],
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": "Bearer k", "X-Custom": {"deep": ["v"]}}
        _, new_p = apply_quirks("openrouter", headers, payload)
        new_h, _ = apply_quirks("openrouter", headers, payload)

        # 各层容器对象均为新建:改返回值的任意层级都不影响入参
        assert new_p["messages"] is not payload["messages"]
        assert new_p["messages"][0] is not payload["messages"][0]
        new_p["messages"][0]["content"][0]["image_url"]["url"] = "mutated"
        new_p["response_format"]["type"] = "mutated"
        new_p["messages"].append({"role": "system"})
        assert payload["messages"][0]["content"][0]["image_url"]["url"] == "data:..."
        assert payload["response_format"]["type"] == "json_object"
        assert len(payload["messages"]) == 1

        new_h["X-Custom"]["deep"].append("mutated")
        assert headers["X-Custom"]["deep"] == ["v"]

    def test_v5_immutable_leaves_shared(self) -> None:
        """不可变叶子(str/int/float/bool/None/tuple)直接共享——优化本身的行为锁。"""
        payload = {
            "model": "m",
            "temperature": 0.1,
            "max_tokens": 1024,
            "stream": False,
            "meta": None,
            "tuple_leaf": (1, 2, "x"),
        }
        _, new_p = apply_quirks("glm", {}, payload)
        assert new_p["model"] is payload["model"]
        assert new_p["temperature"] is payload["temperature"]
        assert new_p["max_tokens"] is payload["max_tokens"]
        assert new_p["stream"] is payload["stream"]
        assert new_p["meta"] is payload["meta"]
        # 全不可变元组可安全共享或重建,等值即可(实现自由度)
        assert new_p["tuple_leaf"] == (1, 2, "x")

    def test_v5_tuple_with_mutable_inside_isolated(self) -> None:
        """含可变元素的 tuple(可作为 dict 值)也被逐层重建隔离。"""
        payload = {"choices": (["a", "b"], {"k": 1})}
        _, new_p = apply_quirks("openrouter", {}, payload)
        new_p["choices"][0].append("c")
        new_p["choices"][1]["k"] = 999
        assert payload["choices"] == (["a", "b"], {"k": 1})

    def test_v5_type_annotations_complete(self) -> None:
        """质量:apply_quirks 公共签名注解完整(参数与返回值)。"""
        import inspect

        sig = inspect.signature(apply_quirks)
        assert list(sig.parameters) == ["provider", "headers", "payload"]
        assert all(p.annotation is not inspect.Parameter.empty for p in sig.parameters.values())
        assert sig.return_annotation is not inspect.Parameter.empty
        assert provider_quirks.__all__  # 质量:__all__ 非空
