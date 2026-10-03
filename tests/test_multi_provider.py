# -*- coding: utf-8 -*-
"""multi_provider 测试(NetSentinel V4 · A63)。

V4 红线 19/20:全部用例使用注入的假 client / 假 cache 与注入传输层,**零网络外呼**,
不访问任何真实门户与真实 VLM 接口:

- providers(A61)未就位或需精确回放时,向 ``sys.modules`` 注入鸭子模块
  (parse_spec / resolve / PROVIDERS 目录语义);
- vlm_client(A62)就位时补一组"真实 resolve + 真实客户端 + 注入 transport"的
  端到端用例(传输层回放三方言响应,仍零外呼);未就位则自动跳过;
- 兄弟模块"未就位"场景用 ``sys.modules`` 置 None 模拟,验证中文 RuntimeError。
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import logging
import struct
import sys
import types
import zlib

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, ImageScore
from netsentinel.vision import multi_provider
from netsentinel.vision import vlm_prompts
from netsentinel.vision.multi_provider import UniversalVLMClassifier, build_classifier
from netsentinel.vision.vlm_cache import VlmBudgetExceeded

# vlm_client(A62)就位则直接用其异常类;未就位时用同名假类(按类名识别上抛路径)
try:
    from netsentinel.vision.vlm_client import ModelNotFoundError, VlmConfigError
except Exception:  # pragma: no cover - 仅 A62 未就位的并行开发期

    class VlmConfigError(RuntimeError):  # type: ignore[no-redef]
        pass

    class ModelNotFoundError(RuntimeError):  # type: ignore[no-redef]
        pass


# ---------------------------------------------------------------------------
# 工具与夹具
# ---------------------------------------------------------------------------


def make_cfg(**overrides) -> Config:
    """构造测试配置(密钥/开关只影响解析与闸门,不产生真实请求)。"""
    params: dict = dict(
        vlm_online=True,
        vlm_api_keys={"openai": "sk-test-openai", "qwen": "sk-test-qwen"},
    )
    params.update(overrides)
    return Config(**params)


def make_img(name: str = "a_nsfw_hi.jpg", sha256: str = "ab" * 32) -> ImageEvidence:
    return ImageEvidence(
        path=f"data/img/{name}",
        url=f"https://example.invalid/{name}",
        source_page="https://example.invalid/index.html",
        sha256=sha256,
    )


def make_png_bytes(width: int = 4, height: int = 4, rgb=(255, 0, 0)) -> bytes:
    """最小合法 PNG(纯标准库),供真实客户端 encode_image 读取。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress((b"\x00" + bytes(rgb) * width) * height)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


class FakeResolved:
    """providers.ResolvedProvider 鸭子(契约字段:base_url/model/api_key/style/local)。"""

    def __init__(
        self,
        provider: str = "openai",
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        api_key: str = "sk-test",
        style: str = "openai",
        local: bool = False,
    ) -> None:
        self.provider = provider
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.style = style
        self.local = local


class FakeClient:
    """假统一客户端:记录调用并回放结果/异常,零网络。"""

    def __init__(self, result: dict | None = None, exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc
        self.calls: list[dict] = []

    def chat_json(self, messages, *, image_paths=None):
        self.calls.append(
            {
                "messages": [dict(m) for m in messages],
                "image_paths": list(image_paths or []),
            }
        )
        if self.exc is not None:
            raise self.exc
        return dict(self.result or {})


class FakeCache:
    """假缓存/预算:记录 get/spend_one/put;可选持久化(put 写入后 get 可命中)。"""

    def __init__(
        self,
        hit: dict | None = None,
        spend_exc: Exception | None = None,
        persistent: bool = False,
    ) -> None:
        self.hit = hit
        self.spend_exc = spend_exc
        self.persistent = persistent
        self.store: dict[tuple, dict] = {}
        self.get_calls: list[tuple] = []
        self.put_calls: list[tuple] = []
        self.spends: int = 0

    def get(self, model, prompt_version, image_sha256):
        self.get_calls.append((model, prompt_version, image_sha256))
        if self.persistent:
            found = self.store.get((model, prompt_version, image_sha256))
            return dict(found) if found is not None else None
        return dict(self.hit) if self.hit is not None else None

    def spend_one(self, day=None):
        self.spends += 1
        if self.spend_exc is not None:
            raise self.spend_exc

    def put(self, model, prompt_version, image_sha256, payload):
        self.put_calls.append((model, prompt_version, image_sha256, dict(payload)))
        self.store[(model, prompt_version, image_sha256)] = dict(payload)


class RecordingTransport:
    """注入 vlm_client 的回放传输层:(url, headers, payload, timeout) -> (status, body)。"""

    def __init__(self, body: str, status: int = 200) -> None:
        self.status = status
        self.body = body
        self.calls: list[dict] = []

    def __call__(self, url, headers, payload, timeout=None, **kwargs):
        self.calls.append(
            {"url": url, "headers": dict(headers), "payload": payload, "timeout": timeout}
        )
        return self.status, self.body


# ---------------------------------------------------------------------------
# 假 providers 模块(A61 未就位/需精确回放时注入 sys.modules)
# ---------------------------------------------------------------------------

_FAKE_CATALOG: dict[str, tuple[str, str, str, bool]] = {
    # key -> (base_url, style, default_model, local)
    "glm": ("https://open.bigmodel.cn/api/paas/v4", "openai", "glm-5.3-flash", False),
    "openai": ("https://api.openai.com/v1", "openai", "gpt-4o-mini", False),
    "qwen": ("https://dashscope.example/compatible-mode/v1", "openai", "qwen-vl-max", False),
    "anthropic": ("https://api.anthropic.com/v1", "anthropic", "claude-sonnet-4", False),
    "gemini": ("https://gemini.example/v1beta", "gemini", "gemini-2.0-flash", False),
    "ollama": ("http://127.0.0.1:11434/v1", "openai", "llava", True),
    "vllm": ("http://127.0.0.1:8000/v1", "openai", "", True),
}


class _FakeProviderSpec:
    def __init__(self, key: str, base_url: str, style: str, default_model: str, local: bool):
        self.key = key
        self.base_url = base_url
        self.style = style
        self.default_model = default_model
        self.local = local
        self.key_envs = [f"NETSENTINEL_{key.upper()}_API_KEY"]
        self.notes = "测试目录条目"


def install_fake_providers(monkeypatch, *, forget_default: bool = False) -> types.ModuleType:
    """注入鸭子 providers 模块(parse_spec/resolve/PROVIDERS);forget_default=True 时
    resolve 刻意不回填目录默认模型,用于验证 build_classifier 的兜底逻辑。"""
    module = types.ModuleType("netsentinel.vision.providers")
    module.PROVIDERS = {
        key: _FakeProviderSpec(key, *fields) for key, fields in _FAKE_CATALOG.items()
    }

    def parse_spec(name):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("分类器规格不能为空,应形如 '提供方' 或 '提供方:模型'")
        if ":" in name:
            provider, _, model = name.partition(":")
            provider, model = provider.strip(), model.strip()
        else:
            provider, model = name.strip(), None
        if provider not in module.PROVIDERS:
            raise ValueError(
                f"未知的 VLM 提供方 '{provider}',可用:"
                + ", ".join(sorted(module.PROVIDERS))
            )
        return provider, (model or None)

    def resolve(provider, cfg):
        spec = module.PROVIDERS[provider]
        model_overrides = dict(getattr(cfg, "vlm_provider_models", None) or {})
        if provider in model_overrides:
            model = str(model_overrides[provider])
        elif forget_default:
            model = ""
        else:
            model = spec.default_model
        base_overrides = dict(getattr(cfg, "vlm_provider_base_urls", None) or {})
        api_keys = dict(getattr(cfg, "vlm_api_keys", None) or {})
        return FakeResolved(
            provider=provider,
            base_url=str(base_overrides.get(provider) or spec.base_url),
            model=model,
            api_key=str(api_keys.get(provider) or ""),
            style=spec.style,
            local=spec.local,
        )

    module.parse_spec = parse_spec
    module.resolve = resolve
    monkeypatch.setitem(sys.modules, "netsentinel.vision.providers", module)
    return module


def block_module(monkeypatch, module_name: str) -> None:
    """把 sys.modules 条目置 None,使 importlib.import_module 视为"未就位"。"""
    monkeypatch.setitem(sys.modules, module_name, None)


# ---------------------------------------------------------------------------
# build_classifier
# ---------------------------------------------------------------------------


class TestBuildClassifier:
    def test_build_full_syntax(self, monkeypatch):
        install_fake_providers(monkeypatch)
        clf = build_classifier("openai:gpt-4o-mini", make_cfg())
        assert isinstance(clf, UniversalVLMClassifier)
        assert clf.name == "openai:gpt-4o-mini"
        assert clf.provider == "openai"
        assert clf.resolved.base_url == "https://api.openai.com/v1"
        assert clf.resolved.api_key == "sk-test-openai"  # cfg.vlm_api_keys 优先
        assert clf.resolved.style == "openai"

    def test_build_provider_only_uses_catalog_default(self, monkeypatch):
        install_fake_providers(monkeypatch)
        assert build_classifier("qwen", make_cfg()).name == "qwen:qwen-vl-max"
        assert build_classifier("ollama", make_cfg()).name == "ollama:llava"

    def test_build_local_spec(self, monkeypatch):
        install_fake_providers(monkeypatch)
        clf = build_classifier("ollama:llava", make_cfg())
        assert clf.name == "ollama:llava"
        assert clf.resolved.local is True
        assert clf.resolved.base_url == "http://127.0.0.1:11434/v1"

    def test_build_respects_cfg_overrides(self, monkeypatch):
        install_fake_providers(monkeypatch)
        cfg = make_cfg(
            vlm_provider_models={"openai": "gpt-4.1-mini"},
            vlm_provider_base_urls={"openai": "https://proxy.example/v1"},
        )
        clf = build_classifier("openai", cfg)
        assert clf.name == "openai:gpt-4.1-mini"
        assert clf.resolved.base_url == "https://proxy.example/v1"

    def test_build_fills_catalog_default_when_resolve_forgets(self, monkeypatch):
        install_fake_providers(monkeypatch, forget_default=True)
        clf = build_classifier("qwen", make_cfg())
        assert clf.name == "qwen:qwen-vl-max"  # 目录默认模型兜底
        assert clf.resolved.model == "qwen-vl-max"

    def test_build_empty_name_raises_valueerror(self):
        for bad in ("", "   "):
            with pytest.raises(ValueError, match="不能为空"):
                build_classifier(bad, make_cfg())

    def test_build_unknown_provider_raises_valueerror(self, monkeypatch):
        install_fake_providers(monkeypatch)
        with pytest.raises(ValueError) as excinfo:
            build_classifier("nope:model-x", make_cfg())
        message = str(excinfo.value)
        assert "nope" in message
        assert "qwen" in message  # 列出可用提供方
        assert "stub" in message or "vlm" in message  # 附已注册分类器(get_classifier 契约)

    def test_build_missing_providers_module_raises_runtimeerror(self, monkeypatch):
        block_module(monkeypatch, "netsentinel.vision.providers")
        with pytest.raises(RuntimeError) as excinfo:
            build_classifier("openai:gpt-4o-mini", make_cfg())
        message = str(excinfo.value)
        assert "providers" in message  # 指明缺失模块
        assert "A61" in message
        assert "openai:gpt-4o-mini" in message

    def test_build_with_real_providers_module(self, monkeypatch):
        """A61 真模块就位时走真实 parse_spec/resolve(仍未外呼)。"""
        if importlib.util.find_spec("netsentinel.vision.providers") is None:
            pytest.skip("providers(A61)未就位,鸭子模块路径已由上方用例覆盖")
        monkeypatch.delitem(sys.modules, "netsentinel.vision.providers", raising=False)
        clf = build_classifier("qwen", make_cfg())
        assert clf.name.startswith("qwen:")
        clf2 = build_classifier("ollama:llava", Config())
        assert clf2.name == "ollama:llava"


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------


class TestClassify:
    def _classifier(self, cache=None, client=None, resolved=None) -> UniversalVLMClassifier:
        resolved = resolved or FakeResolved(provider="openai", model="gpt-4o-mini")
        client = client if client is not None else FakeClient(
            result={
                "nsfw_prob": 0.96,
                "categories": ["色情"],
                "reasoning": "画面含明显色情内容",
                "confidence": 0.93,
            }
        )
        cache = cache if cache is not None else FakeCache()
        return UniversalVLMClassifier(make_cfg(), resolved=resolved, client=client, cache=cache)

    def test_classify_success(self):
        img = make_img()
        client, cache = FakeClient(
            result={
                "nsfw_prob": 0.96,
                "categories": ["色情"],
                "reasoning": "画面含明显色情内容",
                "confidence": 0.93,
            }
        ), FakeCache()
        clf = self._classifier(cache=cache, client=client)

        score = clf.classify(img)

        assert clf.name == "openai:gpt-4o-mini"
        assert score.model == "openai:gpt-4o-mini"
        # 校准后的分值(A22 calibrate:0.96 -> 0.974)
        assert score.nsfw_prob == pytest.approx(vlm_prompts.calibrate(0.96))
        assert score.scores["provider"] == "openai"
        assert score.scores["categories"] == ["色情"]
        assert "色情内容" in score.scores["reasoning"]
        assert score.scores["confidence"] == pytest.approx(0.93)
        assert score.scores["vlm_model"] == "gpt-4o-mini"

        # 请求形态:system 用 A22 提示词,user 附文件路径,图片走 image_paths
        assert len(client.calls) == 1
        system_msg, user_msg = client.calls[0]["messages"]
        assert system_msg["content"] == vlm_prompts.IMAGE_SCORING_SYSTEM
        assert user_msg["content"] == vlm_prompts.build_user_prompt("image", path=img.path)
        assert client.calls[0]["image_paths"] == [img.path]

        # 缓存与预算:键=(模型名, 提示词版本, 图片 sha),先记账后调用,成功后回写
        assert cache.get_calls == [
            ("openai:gpt-4o-mini", vlm_prompts.PROMPT_VERSION, img.sha256)
        ]
        assert cache.spends == 1
        assert len(cache.put_calls) == 1
        model, version, sha, payload = cache.put_calls[0]
        assert (model, version, sha) == ("openai:gpt-4o-mini", vlm_prompts.PROMPT_VERSION, img.sha256)
        assert payload["nsfw_prob"] == pytest.approx(vlm_prompts.calibrate(0.96))

    def test_classify_cache_key_falls_back_to_path_hash(self):
        img = make_img(sha256="")
        cache = FakeCache()
        clf = self._classifier(cache=cache)
        clf.classify(img)
        expected = hashlib.sha256(img.path.encode("utf-8", "surrogatepass")).hexdigest()
        assert cache.get_calls[0][2] == expected

    def test_classify_cache_hit_zero_calls(self):
        img = make_img()
        client = FakeClient()
        cache = FakeCache(
            hit={
                "nsfw_prob": 0.42,
                "categories": ["低俗"],
                "reasoning": "缓存回放",
                "provider": "openai",
                "vlm_model": "gpt-4o-mini",
                "confidence": 0.5,
            }
        )
        clf = self._classifier(cache=cache, client=client)

        score = clf.classify(img)

        assert client.calls == []  # 缓存命中:零调用、零记账
        assert cache.spends == 0
        assert cache.put_calls == []
        assert score.nsfw_prob == pytest.approx(0.42)
        assert score.scores["cached"] is True
        assert score.scores["provider"] == "openai"

    def test_classify_cache_roundtrip(self):
        img = make_img()
        client = FakeClient(
            result={"nsfw_prob": 0.9, "categories": ["色情"], "reasoning": "首次"}
        )
        cache = FakeCache(persistent=True)
        clf = self._classifier(cache=cache, client=client)

        first = clf.classify(img)
        second = clf.classify(img)

        assert len(client.calls) == 1  # 第二次命中缓存
        assert cache.spends == 1
        assert second.nsfw_prob == first.nsfw_prob
        assert second.scores.get("cached") is True

    def test_classify_budget_exceeded_propagates(self):
        img = make_img()
        client = FakeClient()
        cache = FakeCache(spend_exc=VlmBudgetExceeded(200, 200))
        clf = self._classifier(cache=cache, client=client)

        with pytest.raises(VlmBudgetExceeded):
            clf.classify(img)
        assert client.calls == []  # 预算尽:绝不外呼(红线 19)

    def test_classify_vlm_config_error_propagates(self):
        clf = self._classifier(client=FakeClient(exc=VlmConfigError("离线安全态")))
        with pytest.raises(VlmConfigError):
            clf.classify(make_img())

    def test_classify_model_not_found_propagates(self):
        clf = self._classifier(client=FakeClient(exc=ModelNotFoundError("模型不存在")))
        with pytest.raises(ModelNotFoundError):
            clf.classify(make_img())

    def test_classify_generic_error_degrades_to_zero(self, caplog):
        clf = self._classifier(client=FakeClient(exc=RuntimeError("网络炸了")))
        with caplog.at_level(logging.WARNING):
            score = clf.classify(make_img())

        assert score.nsfw_prob == 0.0
        assert "识别失败" in score.scores["error"]
        assert "网络炸了" in score.scores["error"]
        assert score.scores["provider"] == "openai"
        assert any("识别失败" in record.getMessage() for record in caplog.records)

    def test_classify_validate_error_returns_zero_without_caching(self):
        client = FakeClient(result={"categories": ["色情"]})  # 缺 nsfw_prob
        cache = FakeCache()
        clf = self._classifier(cache=cache, client=client)

        score = clf.classify(make_img())

        # 校验失败按 0 处理,再经 A22 校准(0.0 -> 0.02),与 glm_adapter 语义一致
        assert score.nsfw_prob == pytest.approx(vlm_prompts.calibrate(0.0))
        assert score.nsfw_prob < 0.05
        assert "error" in score.scores  # A22 validate 的中文原因
        assert cache.put_calls == []  # 坏结果不进缓存

    def test_classify_builtin_prompts_when_vlm_prompts_missing(self, monkeypatch):
        block_module(monkeypatch, "netsentinel.vision.vlm_prompts")
        img = make_img()
        client = FakeClient(result={"nsfw_prob": 0.8, "categories": ["低俗"]})
        cache = FakeCache()
        clf = self._classifier(cache=cache, client=client)

        score = clf.classify(img)

        assert score.nsfw_prob == pytest.approx(0.8)  # 内置校准为恒等(仅收敛)
        system_msg, user_msg = client.calls[0]["messages"]
        assert "图片内容安全审核助手" in system_msg["content"]  # 内置系统提示词
        assert "文件:" in user_msg["content"] and img.path in user_msg["content"]
        assert cache.get_calls[0][1] == "v2.1"  # 提示词版本回退缺省值

    def test_classify_name_without_model_uses_provider_only(self):
        img = make_img()
        cache = FakeCache()
        clf = self._classifier(
            cache=cache, resolved=FakeResolved(provider="ollama", model="", local=True)
        )
        assert clf.name == "ollama"
        score = clf.classify(img)
        assert score.model == "ollama"
        assert cache.get_calls[0][0] == "ollama"

    def test_classify_missing_vlm_client_module_raises(self, monkeypatch):
        block_module(monkeypatch, "netsentinel.vision.vlm_client")
        clf = UniversalVLMClassifier(
            make_cfg(),
            resolved=FakeResolved(provider="openai", model="gpt-4o-mini"),
            cache=FakeCache(),
        )
        with pytest.raises(RuntimeError) as excinfo:
            clf.classify(make_img())
        assert "vlm_client" in str(excinfo.value)
        assert "A62" in str(excinfo.value)

    def test_classify_missing_vlm_cache_module_raises(self, monkeypatch):
        block_module(monkeypatch, "netsentinel.vision.vlm_cache")
        clf = UniversalVLMClassifier(
            make_cfg(),
            resolved=FakeResolved(provider="openai", model="gpt-4o-mini"),
            client=FakeClient(),
        )
        with pytest.raises(RuntimeError, match="红线 19"):
            clf.classify(make_img())

    def test_classify_batch_inherited(self):
        imgs = [make_img("one.jpg", sha256="11" * 32), make_img("two.jpg", sha256="22" * 32)]
        clf = self._classifier(cache=FakeCache(persistent=True))
        scores = clf.classify_batch(imgs)
        assert [s.image.path for s in scores] == [i.path for i in imgs]
        assert all(s.model == "openai:gpt-4o-mini" for s in scores)


# ---------------------------------------------------------------------------
# "vlm" 泛型注册与 get_classifier 接线
# ---------------------------------------------------------------------------


class TestVlmGenericRegistration:
    def test_vlm_registered_and_gettable(self, monkeypatch):
        install_fake_providers(monkeypatch)
        cb = pytest.importorskip("netsentinel.vision.classifier_base")
        cfg = make_cfg(vlm_provider="qwen")
        clf = cb.get_classifier("vlm", cfg)
        assert isinstance(clf, UniversalVLMClassifier)
        assert clf.name == "qwen:qwen-vl-max"  # 按 cfg.vlm_provider 定格(目录默认模型)
        assert clf.provider == "qwen"
        again = cb.get_classifier("vlm", cfg)
        assert again is not clf  # 每次返回新实例

    def test_vlm_generic_with_provider_model_spec(self, monkeypatch):
        install_fake_providers(monkeypatch)
        cb = pytest.importorskip("netsentinel.vision.classifier_base")
        clf = cb.get_classifier("vlm", make_cfg(vlm_provider="openai:gpt-4o-mini"))
        assert isinstance(clf, UniversalVLMClassifier)
        assert clf.name == "openai:gpt-4o-mini"

    def test_get_classifier_spec_routes_to_build(self, monkeypatch):
        install_fake_providers(monkeypatch)
        cb = pytest.importorskip("netsentinel.vision.classifier_base")
        clf = cb.get_classifier("openai:gpt-4o-mini", make_cfg())
        assert isinstance(clf, UniversalVLMClassifier)
        assert clf.name == "openai:gpt-4o-mini"

    def test_get_classifier_unknown_name_keeps_valueerror_contract(self, monkeypatch):
        """providers 未就位时,get_classifier(未知名) 仍须 ValueError 且列出已注册名。"""
        block_module(monkeypatch, "netsentinel.vision.providers")
        cb = pytest.importorskip("netsentinel.vision.classifier_base")
        importlib.import_module("netsentinel.vision.stub_classifier")  # 确保 "stub" 已注册
        with pytest.raises(ValueError) as excinfo:
            cb.get_classifier("no_such_classifier", Config())
        message = str(excinfo.value)
        assert "no_such_classifier" in message
        assert "stub" in message


# ---------------------------------------------------------------------------
# 端到端:真实 vlm_client(A62)+ 注入 transport(零外呼)
# ---------------------------------------------------------------------------


def _vlm_client_or_skip():
    try:
        from netsentinel.vision import vlm_client
    except Exception as exc:  # pragma: no cover - 仅 A62 未就位的并行开发期
        pytest.skip(f"vlm_client(A62)未就位:{exc}")
    return vlm_client


def _content_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


class TestEndToEndRealClient:
    """真实 resolve 鸭子 + 真实 UniversalVLMClient + 回放 transport,三方言各一发。"""

    def _run(self, tmp_path, resolved, body):
        vlm_client = _vlm_client_or_skip()
        cfg = Config()
        transport = RecordingTransport(body)
        client = vlm_client.UniversalVLMClient(resolved, cfg, transport=transport)
        png_path = tmp_path / "sample.png"
        png_path.write_bytes(make_png_bytes())
        img = ImageEvidence(
            path=str(png_path),
            url="https://example.invalid/sample.png",
            source_page="https://example.invalid/index.html",
            sha256=hashlib.sha256(png_path.read_bytes()).hexdigest(),
        )
        clf = UniversalVLMClassifier(cfg, resolved=resolved, client=client, cache=FakeCache())
        score = clf.classify(img)
        return score, transport

    def test_openai_dialect(self, monkeypatch, tmp_path):
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": _content_json(
                                {
                                    "nsfw_prob": 0.96,
                                    "categories": ["色情"],
                                    "reasoning": "画面含明显色情内容",
                                    "confidence": 0.93,
                                }
                            )
                        }
                    }
                ]
            }
        )
        resolved = FakeResolved(
            provider="ollama",
            base_url="http://127.0.0.1:11434/v1",
            model="llava",
            api_key="",
            local=True,
        )
        score, transport = self._run(tmp_path, resolved, body)

        assert score.model == "ollama:llava"
        assert score.nsfw_prob == pytest.approx(vlm_prompts.calibrate(0.96))
        assert score.scores["provider"] == "ollama"
        assert score.scores["categories"] == ["色情"]
        assert len(transport.calls) == 1  # 唯一一次"外呼"即回放 transport,零真实网络
        call = transport.calls[0]
        assert call["url"] == "http://127.0.0.1:11434/v1/chat/completions"
        assert call["payload"]["model"] == "llava"
        assert call["payload"]["response_format"] == {"type": "json_object"}

    def test_anthropic_dialect(self, monkeypatch, tmp_path):
        body = json.dumps(
            {
                "content": [
                    {"type": "text", "text": _content_json({"nsfw_prob": 0.2, "categories": ["正常"]})}
                ]
            }
        )
        resolved = FakeResolved(
            provider="anthropic",
            base_url="https://api.anthropic.com/v1",
            model="claude-sonnet-4",
            api_key="test-anthropic-key",
            style="anthropic",
            local=True,  # 测试免 vlm_online 闸门(仍零外呼:transport 注入)
        )
        score, transport = self._run(tmp_path, resolved, body)

        assert score.model == "anthropic:claude-sonnet-4"
        assert 0.0 < score.nsfw_prob < 0.5
        call = transport.calls[0]
        assert call["url"].endswith("/messages")
        assert "anthropic-version" in call["headers"]
        assert call["payload"]["model"] == "claude-sonnet-4"
        assert call["payload"]["system"]  # system 提取到顶层
        assert call["payload"]["max_tokens"] == 1024

    def test_gemini_dialect(self, monkeypatch, tmp_path):
        body = json.dumps(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": _content_json({"nsfw_prob": 0.1, "categories": ["正常"]})}
                            ]
                        }
                    }
                ]
            }
        )
        resolved = FakeResolved(
            provider="gemini",
            base_url="https://gemini.example/v1beta",
            model="gemini-2.0-flash",
            api_key="",
            style="gemini",
            local=True,
        )
        score, transport = self._run(tmp_path, resolved, body)

        assert score.model == "gemini:gemini-2.0-flash"
        call = transport.calls[0]
        assert ":generateContent" in call["url"]
        assert call["payload"]["generationConfig"]["responseMimeType"] == "application/json"
        assert call["payload"]["contents"]  # contents 结构就位

    def test_end_to_end_with_real_providers_build(self, monkeypatch, tmp_path):
        """A61 真模块就位:build_classifier → 真实 resolved → 真实客户端(注入 transport)。"""
        if importlib.util.find_spec("netsentinel.vision.providers") is None:
            pytest.skip("providers(A61)未就位")
        vlm_client = _vlm_client_or_skip()
        monkeypatch.delitem(sys.modules, "netsentinel.vision.providers", raising=False)
        cfg = Config()
        clf = build_classifier("ollama:llava", cfg)
        assert clf.name == "ollama:llava"

        body = json.dumps(
            {
                "choices": [
                    {"message": {"content": _content_json({"nsfw_prob": 0.97, "categories": ["色情"]})}}
                ]
            }
        )
        transport = RecordingTransport(body)
        # 注入真实客户端(回放 transport)与假缓存:全程零外呼、零落盘副作用
        clf._client = vlm_client.UniversalVLMClient(clf.resolved, cfg, transport=transport)
        clf._cache = FakeCache()
        png_path = tmp_path / "sample.png"
        png_path.write_bytes(make_png_bytes())
        img = ImageEvidence(
            path=str(png_path),
            url="https://example.invalid/sample.png",
            source_page="https://example.invalid/index.html",
        )
        score = clf.classify(img)
        assert score.nsfw_prob > 0.9
        assert "ollama" in score.model
        assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# V5 升级锁定:遥测 / 缓存键单次计算 / 泛型入口不做进程级 resolve 缓存
# ---------------------------------------------------------------------------


class TestV5Upgrades:
    def _clf(self, **kwargs) -> UniversalVLMClassifier:
        resolved = kwargs.get("resolved") or FakeResolved(provider="openai", model="gpt-4o-mini")
        client = kwargs.get("client") or FakeClient(
            result={"nsfw_prob": 0.9, "categories": ["色情"]}
        )
        cache = kwargs.get("cache") or FakeCache()
        return UniversalVLMClassifier(make_cfg(), resolved=resolved, client=client, cache=cache)

    def test_v5_classify_telemetry_ok_including_cache_hit(self):
        """成功返回(含缓存命中)计 vlm.classify.ok;计时器逐次采样。"""
        img = make_img()
        clf = self._clf(cache=FakeCache(persistent=True))
        telemetry.reset()
        try:
            clf.classify(img)  # 真实评分
            clf.classify(img)  # 缓存命中
            snap = telemetry.snapshot()
        finally:
            telemetry.reset()
        counters = snap["counters"]
        assert counters.get("vlm.classify.ok") == 2
        assert "vlm.classify.error" not in counters
        assert snap["timers"]["multi_provider.classify"]["count"] == 2

    def test_v5_classify_telemetry_error_on_degrade(self):
        """降级为 0 分计 vlm.classify.error,不计 ok。"""
        clf = self._clf(client=FakeClient(exc=RuntimeError("网络炸了")))
        telemetry.reset()
        try:
            score = clf.classify(make_img())
            snap = telemetry.snapshot()
        finally:
            telemetry.reset()
        assert score.nsfw_prob == 0.0
        counters = snap["counters"]
        assert counters.get("vlm.classify.error") == 1
        assert "vlm.classify.ok" not in counters
        assert snap["timers"]["multi_provider.classify"]["count"] == 1

    def test_v5_classify_telemetry_reraise_not_counted(self):
        """上抛路径(配置错)不计 ok/error;计时器仍采样(finally 口径)。"""
        clf = self._clf(client=FakeClient(exc=VlmConfigError("离线安全态")))
        telemetry.reset()
        try:
            with pytest.raises(VlmConfigError):
                clf.classify(make_img())
            snap = telemetry.snapshot()
        finally:
            telemetry.reset()
        counters = snap["counters"]
        assert "vlm.classify.ok" not in counters
        assert "vlm.classify.error" not in counters
        assert snap["timers"]["multi_provider.classify"]["count"] == 1

    def test_v5_cache_key_computed_once_and_reused(self, monkeypatch):
        """缓存键(路径 sha256)在单次 classify 内恰好计算一次,get/put 复用同键。"""
        calls = {"n": 0}
        real_sha256 = hashlib.sha256

        def counting_sha256(data=b""):
            calls["n"] += 1
            return real_sha256(data)

        monkeypatch.setattr(multi_provider.hashlib, "sha256", counting_sha256)
        img = make_img(sha256="")  # 无 sha256 → 走路径 hash 分支
        cache = FakeCache()
        self._clf(cache=cache).classify(img)
        assert calls["n"] == 1  # 单次计算,不在 get/put 两路重复哈希
        assert cache.get_calls[0][2] == cache.put_calls[0][2]
        expected = real_sha256(img.path.encode("utf-8", "surrogatepass")).hexdigest()
        assert cache.get_calls[0][2] == expected

    def test_v5_generic_entry_no_process_level_resolve_cache(self, monkeypatch):
        """泛型 "vlm" 的 resolve 结果只定格在实例上:配置可变,新实例按当次 cfg 重解析。"""
        install_fake_providers(monkeypatch)
        cb = pytest.importorskip("netsentinel.vision.classifier_base")
        clf1 = cb.get_classifier("vlm", make_cfg(vlm_provider="qwen"))
        assert clf1.name == "qwen:qwen-vl-max"
        clf2 = cb.get_classifier("vlm", make_cfg(vlm_provider="openai:gpt-4o-mini"))
        assert clf2.name == "openai:gpt-4o-mini"
        assert clf1.name == "qwen:qwen-vl-max"  # 先前实例不被串改
        assert clf1.resolved is not clf2.resolved
