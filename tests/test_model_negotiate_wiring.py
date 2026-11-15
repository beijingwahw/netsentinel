# -*- coding: utf-8 -*-
"""A71 接线测试:multi_provider 捕获 ModelNotFoundError 后的协商换名回退。

项目红线:新接线默认关闭——``cfg.model_negotiate_enabled`` 缺省 False 时
``ModelNotFoundError`` 照旧上抛,行为与接线前逐字一致。开启后:

- 首调报"模型不存在"→ 经注入的 negotiate_fn 协商换名 → **重试前再
  spend_one 一次**(红线 19:重试也是真实外呼)→ 新名定格到 client.model;
- 协商失败 → 原 ModelNotFoundError 上抛(orchestrator 成员循环语义不变);
- 非 ModelNotFoundError(网络错 / 配置错)不经协商,原样上抛。

全部用例注入假 client / 假 cache / 协商替身,零网络外呼。
"""
from __future__ import annotations

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence
from netsentinel.vision.multi_provider import UniversalVLMClassifier
from netsentinel.vision.vlm_cache import VlmBudgetExceeded

try:
    from netsentinel.vision.vlm_client import ModelNotFoundError
except Exception:  # pragma: no cover - 仅 A62 未就位的并行开发期

    class ModelNotFoundError(RuntimeError):  # type: ignore[no-redef]
        pass


def make_cfg(**overrides) -> Config:
    params: dict = dict(
        vlm_online=True,
        vlm_api_keys={"openai": "sk-test-openai"},
    )
    params.update(overrides)
    return Config(**params)


def make_img(name: str = "negotiate.png", sha256: str = "cd" * 32) -> ImageEvidence:
    return ImageEvidence(
        path=f"data/img/{name}",
        url=f"https://example.invalid/{name}",
        source_page="https://example.invalid/index.html",
        sha256=sha256,
    )


class FakeCache:
    """vlm_cache.VlmCache 鸭子:get 恒未命中、spend_one 计数、put 记录。"""

    def __init__(self) -> None:
        self.spent = 0
        self.puts: list[tuple] = []

    def get(self, model, version, key):
        return None

    def spend_one(self) -> None:
        self.spent += 1
        if self.spent > 10:
            raise VlmBudgetExceeded("当日 VLM 预算已用尽")

    def put(self, model, version, key, payload):
        self.puts.append((model, version, key, payload))


class FlakyClient:
    """首调抛 ModelNotFoundError、此后按脚本应答的假客户端。"""

    def __init__(self, model: str = "gpt-4o-mini", reply: dict | None = None) -> None:
        self.model = model
        self.reply = reply if reply is not None else {
            "nsfw_prob": 0.42,
            "reasoning": "协商换名后应答",
        }
        self.calls: list[dict] = []

    def chat_json(self, messages, *, image_paths=None):
        self.calls.append({"model": self.model, "messages": messages})
        if len(self.calls) == 1:
            raise ModelNotFoundError("提供方 openai 的模型 gpt-4o-mini 不存在或不可用")
        return dict(self.reply)


class FakeResolved:
    """providers.ResolvedProvider 鸭子(固定身份 openai / gpt-4o-mini)。"""

    def __init__(
        self,
        provider: str = "openai",
        model: str = "gpt-4o-mini",
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "sk-test-openai",
        style: str = "openai",
        local: bool = False,
    ) -> None:
        self.provider = provider
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.style = style
        self.local = local


def make_classifier(cfg, client, cache, negotiate_fn=None) -> UniversalVLMClassifier:
    return UniversalVLMClassifier(
        cfg, resolved=FakeResolved(), client=client, cache=cache, negotiate_fn=negotiate_fn
    )


# ---------------------------------------------------------------------------
# 默认关闭(项目红线:行为与接线前逐字一致)
# ---------------------------------------------------------------------------


def test_disabled_by_default_model_not_found_propagates() -> None:
    """缺省:ModelNotFoundError 原样上抛,协商函数绝不被调用。"""
    calls: list[tuple] = []

    def negotiate_fn(provider, model, cfg):
        calls.append((provider, model))
        return "should-never-happen"

    cfg = make_cfg()
    client = FlakyClient()
    cache = FakeCache()
    clf = make_classifier(cfg, client, cache, negotiate_fn=negotiate_fn)
    with pytest.raises(ModelNotFoundError):
        clf.classify(make_img())
    assert calls == []
    assert len(client.calls) == 1
    assert cache.spent == 1  # 只有首调记账,未发生重试


def test_disabled_other_errors_untouched() -> None:
    """缺省:非模型不存在异常照旧走既有降级(0 分 + scores.error)。"""
    cfg = make_cfg()

    class Boom:
        model = "gpt-4o-mini"

        def chat_json(self, messages, *, image_paths=None):
            raise RuntimeError("网络炸了")

    clf = make_classifier(cfg, Boom(), FakeCache())
    score = clf.classify(make_img())
    assert score.nsfw_prob == 0.0
    assert "error" in score.scores


# ---------------------------------------------------------------------------
# 开启协商(cfg 附加属性 model_negotiate_enabled,不新增 Config 字段)
# ---------------------------------------------------------------------------


def test_enabled_negotiates_and_retries_with_new_model() -> None:
    """开启:首调模型不存在 → 协商换名 → 重试前再记账 → 新名定格到 client.model。"""
    seen: list[tuple] = []

    def negotiate_fn(provider, model, cfg):
        seen.append((provider, model, cfg))
        return "gpt-4o-mini-2024-07"

    cfg = make_cfg()
    cfg.model_negotiate_enabled = True  # 附加实例属性(getattr 动态读取)
    client = FlakyClient()
    cache = FakeCache()
    clf = make_classifier(cfg, client, cache, negotiate_fn=negotiate_fn)

    score = clf.classify(make_img())

    # 协商函数收到 (provider, 原模型名, 同一 cfg)
    assert seen == [("openai", "gpt-4o-mini", cfg)]
    # 两次真实外呼:首调(旧名)+ 换名重试(新名已定格到 client.model)
    assert [call["model"] for call in client.calls] == [
        "gpt-4o-mini",
        "gpt-4o-mini-2024-07",
    ]
    # 红线 19:重试也是真实外呼,重试前再 spend_one 一次(共 2 次)
    assert cache.spent == 2
    # 结果正常返回(经 A22 calibrate 校准),实际应答模型诚实记入 scores["vlm_model"]
    from netsentinel.vision import vlm_prompts

    expected_prob = min(1.0, max(0.0, float(vlm_prompts.calibrate(0.42))))
    assert score.nsfw_prob == pytest.approx(expected_prob)
    assert score.scores["vlm_model"] == "gpt-4o-mini-2024-07"
    assert cache.puts and cache.puts[0][3]["vlm_model"] == "gpt-4o-mini-2024-07"


def test_enabled_negotiation_failure_reraises_original() -> None:
    """开启但协商失败:原 ModelNotFoundError 上抛(成员循环语义保持)。"""
    cfg = make_cfg()
    cfg.model_negotiate_enabled = True

    def broken_negotiate(provider, model, cfg):
        raise RuntimeError("协商全链失败")

    client = FlakyClient()
    cache = FakeCache()
    clf = make_classifier(cfg, client, cache, negotiate_fn=broken_negotiate)
    with pytest.raises(ModelNotFoundError):
        clf.classify(make_img())
    assert len(client.calls) == 1  # 未发生重试
    assert cache.spent == 1  # 未发生重试记账


def test_enabled_non_model_error_skips_negotiation() -> None:
    """开启但异常非模型不存在:不经协商,原样上抛/降级路径不变。"""
    calls: list[tuple] = []

    def negotiate_fn(provider, model, cfg):
        calls.append((provider, model))
        return "never"

    cfg = make_cfg()
    cfg.model_negotiate_enabled = True

    class ConfigError:
        model = "gpt-4o-mini"

        def chat_json(self, messages, *, image_paths=None):
            raise RuntimeError("提供方 openai 未满足云端调用前置条件")

    clf = make_classifier(cfg, ConfigError(), FakeCache(), negotiate_fn=negotiate_fn)
    score = clf.classify(make_img())
    assert calls == []
    assert score.nsfw_prob == 0.0


# ---------------------------------------------------------------------------
# V10.18 可观测补齐:vision.negotiate.retry(纯遥测,零行为变化)
# ---------------------------------------------------------------------------


def test_v10_negotiate_retry_counter_counts_successful_retry() -> None:
    """协商换名重试真实发生时计一次 vision.negotiate.retry。"""
    cfg = make_cfg()
    cfg.model_negotiate_enabled = True
    client = FlakyClient()
    cache = FakeCache()
    clf = make_classifier(cfg, client, cache, negotiate_fn=lambda *a: "gpt-4o-mini-2024-07")
    before = telemetry.snapshot()["counters"].get("vision.negotiate.retry", 0.0)

    clf.classify(make_img())

    after = telemetry.snapshot()["counters"].get("vision.negotiate.retry", 0.0)
    assert len(client.calls) == 2  # 首调 + 换名重试
    assert after == before + 1.0


def test_v10_negotiate_retry_counter_not_counted_on_failure() -> None:
    """协商失败(未发生重试)不计 vision.negotiate.retry。"""
    cfg = make_cfg()
    cfg.model_negotiate_enabled = True

    def broken_negotiate(provider, model, cfg):
        raise RuntimeError("协商全链失败")

    clf = make_classifier(cfg, FlakyClient(), FakeCache(), negotiate_fn=broken_negotiate)
    before = telemetry.snapshot()["counters"].get("vision.negotiate.retry", 0.0)

    with pytest.raises(ModelNotFoundError):
        clf.classify(make_img())

    after = telemetry.snapshot()["counters"].get("vision.negotiate.retry", 0.0)
    assert after == before
