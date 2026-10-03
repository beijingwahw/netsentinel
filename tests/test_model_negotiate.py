# -*- coding: utf-8 -*-
"""model_negotiate 测试(NetSentinel V4 · A71)。

V4 红线 19/20:全部用例注入假 transport / 假 spend,**零网络外呼**,
不访问任何真实门户与真实 VLM 接口;vlm_client(A62)/ providers(A61)就位时
走真实类 + 注入传输层(回放三方言中的 openai 形态响应)。

A63 隔离经验(必须遵守):monkeypatch 兄弟模块时须**同时**

1. ``monkeypatch.setitem(sys.modules, 模块全名, 替身或 None)``;
2. ``monkeypatch.setattr / delattr(netsentinel.vision, 短名, ...)``;

只改一处时,from-import / 已绑定的父包属性会绕过 ``sys.modules`` 里的替身。
被测模块的惰性加载器优先读 ``sys.modules``(``None`` = 未就位),故双改即生效。
"""
from __future__ import annotations

import json
import sys
import types

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.vision import model_negotiate
from netsentinel.vision.model_negotiate import candidate_chain, negotiate

# A62 异常类就位则直接用;未就位(并行开发期)跳过依赖它的用例
try:
    from netsentinel.vision.vlm_client import VlmConfigError
except Exception:  # pragma: no cover - 仅 A62 未就位的并行开发期
    VlmConfigError = None  # type: ignore[assignment]

# A23 预算异常类(缺省惰性闸门的真实语义;未就位时用等价 RuntimeError)
try:
    from netsentinel.vision.vlm_cache import VlmBudgetExceeded
except Exception:  # pragma: no cover
    class VlmBudgetExceeded(RuntimeError):  # type: ignore[no-redef]
        def __init__(self, used: int, limit: int) -> None:
            super().__init__(f"当日 VLM 调用预算已用尽:{used}/{limit}。")


# ---------------------------------------------------------------------------
# 工具与夹具
# ---------------------------------------------------------------------------


def make_cfg(**overrides) -> Config:
    """构造测试配置:密钥只来自 cfg.vlm_api_keys(最高优先级,不触碰真实环境)。"""
    params: dict = dict(
        vlm_online=True,
        vlm_api_keys={
            "glm": "sk-test-glm",
            "openai": "sk-test-openai",
            "openrouter": "sk-test-or",
        },
    )
    params.update(overrides)
    return Config(**params)


def ok_body(content: str = '{"ok": true}') -> str:
    """openai 方言 200 响应体:chat_json 能解析出合法 dict。"""
    return json.dumps(
        {"choices": [{"message": {"content": content}}]}, ensure_ascii=False
    )


def nf_body(name: str = "gone") -> str:
    """openai 方言 404 错误体:触发 A62 ModelNotFoundError。"""
    return json.dumps(
        {"error": {"message": f"model {name} not found"}}, ensure_ascii=False
    )


class FakeTransport:
    """回放式假传输层:按序消费 outcomes,记录每次请求(零网络)。

    outcomes 每项为 ``(status, body)`` 或异常实例(原样上抛,模拟网络错误);
    被超额调用(候选链外的多余外呼)立即 AssertionError 失败。
    """

    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append(
            {"url": url, "model": payload.get("model"), "timeout": timeout}
        )
        if not self.outcomes:
            raise AssertionError("假传输层被超额调用:出现了不该有的额外外呼")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        status, body = outcome
        return int(status), body


class FakeSpend:
    """假预算闸门:计数每次记账;exc 非空则恒抛(预算尽)。"""

    def __init__(self, exc: Exception | None = None) -> None:
        self.calls = 0
        self.exc = exc

    def __call__(self) -> None:
        self.calls += 1
        if self.exc is not None:
            raise self.exc


@pytest.fixture(autouse=True)
def _clean_negotiation_cache():
    """用例间隔离模块级定格缓存(_NEGOTIATED 是进程内状态)。"""
    model_negotiate._NEGOTIATED.clear()
    yield
    model_negotiate._NEGOTIATED.clear()


def _inject_sibling(monkeypatch, short_name: str, fake) -> None:
    """注入兄弟模块替身:sys.modules 与父包属性双改(A63 隔离经验)。"""
    full_name = f"netsentinel.vision.{short_name}"
    monkeypatch.setitem(sys.modules, full_name, fake)
    monkeypatch.setattr(
        sys.modules["netsentinel.vision"], short_name, fake, raising=False
    )


def _hide_sibling(monkeypatch, short_name: str) -> None:
    """模拟兄弟模块未就位:sys.modules 置 None + 父包属性删除(A63 隔离经验)。"""
    full_name = f"netsentinel.vision.{short_name}"
    monkeypatch.setitem(sys.modules, full_name, None)
    monkeypatch.delattr(sys.modules["netsentinel.vision"], short_name, raising=False)


# ---------------------------------------------------------------------------
# 候选链:顺序 / 去重 / 占位符排除 / 空链回退 / 兄弟未就位容忍
# ---------------------------------------------------------------------------


class TestCandidateChain:
    def test_order_and_dedup_with_override(self) -> None:
        """当前模型 → 配置覆盖;与目录默认/低价推荐重复的候选只保留首个。"""
        cfg = Config(vlm_provider_models={"glm": "glm-5.3-flash"})
        # 目录默认 glm-5.3-flash 与 suggest cheap glm-5.3-flash 均与覆盖项重复
        assert candidate_chain("glm", "glm-4.5v", cfg) == [
            "glm-4.5v",
            "glm-5.3-flash",
        ]

    def test_order_without_override(self) -> None:
        """无覆盖:当前模型 → 目录默认 → 低价推荐(不同名才依次保留)。"""
        assert candidate_chain("openai", "gpt-4o", Config()) == [
            "gpt-4o",
            "gpt-4o-mini",  # 目录默认;suggest cheap 同名去重
        ]
        assert candidate_chain("doubao", "doubao-x", Config()) == [
            "doubao-x",
            "doubao-1.5-vision-pro",  # 目录默认
            "doubao-1.5-vision-lite",  # suggest cheap
        ]

    def test_placeholder_alias_keys_excluded(self) -> None:
        """A64 model_aliases 的文档占位键(ep-<接入点ID> 等)不得进入候选链。"""
        chain = candidate_chain("doubao", "doubao-x", Config())
        for candidate in chain:
            assert "<" not in candidate and ">" not in candidate
            assert "{" not in candidate and "}" not in candidate

    def test_injected_fake_siblings_full_order(self, monkeypatch) -> None:
        """替身模块验证五路来源完整顺序(A63 双改注入,真实数据不参与)。"""
        fake_providers = types.ModuleType("netsentinel.vision.providers")
        fake_providers.PROVIDERS = {
            "fakep": types.SimpleNamespace(default_model="dir-default")
        }
        fake_catalog = types.ModuleType("netsentinel.vision.model_catalog")
        fake_catalog.suggest = (
            lambda provider, need="balanced": "cat-cheap" if need == "cheap" else None
        )
        fake_quirks = types.ModuleType("netsentinel.vision.provider_quirks")
        fake_quirks.QUIRKS = {
            "fakep": {
                "model_aliases": {"real-alias": "说明", "ep-<占位>": "说明"}
            }
        }
        _inject_sibling(monkeypatch, "providers", fake_providers)
        _inject_sibling(monkeypatch, "model_catalog", fake_catalog)
        _inject_sibling(monkeypatch, "provider_quirks", fake_quirks)
        cfg = Config(vlm_provider_models={"fakep": "cfg-override"})
        assert candidate_chain("fakep", "cur-model", cfg) == [
            "cur-model",      # 1) 当前模型
            "cfg-override",   # 2) cfg.vlm_provider_models
            "dir-default",    # 3) providers 目录 default_model
            "cat-cheap",      # 4) model_catalog suggest(cheap)
            "real-alias",     # 5) quirks model_aliases 具体键(占位键已排除)
        ]

    def test_no_candidates_returns_model_verbatim(self) -> None:
        """未知提供方:五路全部落空 → 按契约原样返回 [model]。"""
        assert candidate_chain("nosuch", "mystery-model", Config()) == [
            "mystery-model"
        ]

    def test_siblings_absent_tolerated(self, monkeypatch) -> None:
        """兄弟模块全部未就位(sys.modules 置 None + 父包删属性)不报错。"""
        for short_name in ("providers", "model_catalog", "provider_quirks"):
            _hide_sibling(monkeypatch, short_name)
        # cfg 来源是本地读取,不受兄弟缺席影响
        assert candidate_chain("glm", "", Config(vlm_provider_models={"glm": "ov"})) == [
            "ov"
        ]
        # 全无候选 → 原样(空模型也原样)
        assert candidate_chain("glm", "", Config()) == [""]


# ---------------------------------------------------------------------------
# negotiate:定格 / 换名 / 全链失败 / 预算 / 配置错终止
# ---------------------------------------------------------------------------


class TestNegotiate:
    def test_first_candidate_success_zero_rename(self) -> None:
        """首候选(当前模型)成功:零换名,一次外呼,一次记账。"""
        cfg = make_cfg(vlm_provider_models={"glm": "glm-5.3-flash"})
        transport = FakeTransport([(200, ok_body())])
        spend = FakeSpend()
        assert negotiate("glm", "glm-4.5v", cfg, transport=transport, spend=spend) == (
            "glm-4.5v"
        )
        assert len(transport.calls) == 1
        assert transport.calls[0]["model"] == "glm-4.5v"
        assert spend.calls == 1  # spend 次数 == 尝试次数

    def test_404_404_200_pins_and_caches(self) -> None:
        """404→404→200:定格链上存活候选;二次调用零 transport / 零 spend。"""
        cfg = make_cfg(vlm_provider_models={"glm": "bad-b"})
        transport = FakeTransport(
            [(404, nf_body("bad-a")), (404, nf_body("bad-b")), (200, ok_body())]
        )
        spend = FakeSpend()
        result = negotiate("glm", "bad-a", cfg, transport=transport, spend=spend)
        # 链:bad-a → bad-b(覆盖)→ glm-5.3-flash(目录默认;低价推荐重复剔除)
        assert result == "glm-5.3-flash"
        assert [c["model"] for c in transport.calls] == [
            "bad-a",
            "bad-b",
            "glm-5.3-flash",
        ]
        assert spend.calls == 3  # spend 次数 == 尝试次数

        # 进程内定格:换一套 transport/spend 再调,零调用
        transport2 = FakeTransport([(200, ok_body())])
        spend2 = FakeSpend()
        assert (
            negotiate("glm", "bad-a", cfg, transport=transport2, spend=spend2)
            == "glm-5.3-flash"
        )
        assert transport2.calls == []
        assert spend2.calls == 0

    def test_full_chain_failure_lists_all_candidates(self) -> None:
        """全链 ModelNotFound → RuntimeError(中文)列出全部试过的候选。"""
        cfg = make_cfg(vlm_provider_models={"openai": "gone-b"})
        chain = candidate_chain("openai", "gone-a", cfg)
        assert chain == ["gone-a", "gone-b", "gpt-4o-mini"]  # 前置自证链形状
        transport = FakeTransport([(404, nf_body())] * len(chain))
        spend = FakeSpend()
        with pytest.raises(RuntimeError) as excinfo:
            negotiate("openai", "gone-a", cfg, transport=transport, spend=spend)
        message = str(excinfo.value)
        for name in chain:
            assert name in message
        assert "协商全链失败" in message
        assert len(transport.calls) == len(chain)
        assert spend.calls == len(chain)  # spend 次数 == 尝试次数

    def test_budget_exhausted_refuses_before_any_outbound(self) -> None:
        """预算尽:第一跳记账即拒,零外呼(红线 19)。"""
        cfg = make_cfg()
        transport = FakeTransport([(200, ok_body())])
        spend = FakeSpend(exc=VlmBudgetExceeded(200, 200))
        with pytest.raises(RuntimeError, match="预算"):
            negotiate("glm", "glm-4.5v", cfg, transport=transport, spend=spend)
        assert transport.calls == []

    def test_default_spend_refuses_without_vlm_cache(self, monkeypatch) -> None:
        """缺省惰性闸门:vlm_cache 未就位且未注入 spend → 直接拒绝外呼。"""
        _hide_sibling(monkeypatch, "vlm_cache")
        cfg = make_cfg()
        transport = FakeTransport([(200, ok_body())])
        with pytest.raises(RuntimeError, match="vlm_cache"):
            negotiate("glm", "glm-4.5v", cfg, transport=transport)  # spend=None
        assert transport.calls == []

    @pytest.mark.skipif(VlmConfigError is None, reason="A62 vlm_client 未就位")
    def test_config_error_terminates_without_rename(self) -> None:
        """VlmConfigError(vlm_online 未开)属配置错:原样上抛,不换名重试。"""
        cfg = make_cfg(vlm_online=False)
        transport = FakeTransport([(200, ok_body())])
        spend = FakeSpend()
        with pytest.raises(VlmConfigError):
            negotiate("glm", "glm-4.5v", cfg, transport=transport, spend=spend)
        assert transport.calls == []  # 前置闸门拦截,零外呼
        assert spend.calls == 1  # 尝试已发起(记账在前),预算照记一次

    def test_non_model_http_error_terminates(self) -> None:
        """HTTP 500 非"模型不存在":立即上抛,不尝试后续候选。"""
        cfg = make_cfg(vlm_provider_models={"openai": "gone-b"})
        transport = FakeTransport([(500, json.dumps({"error": "server exploded"}))])
        spend = FakeSpend()
        with pytest.raises(RuntimeError, match="500"):
            negotiate("openai", "gone-a", cfg, transport=transport, spend=spend)
        assert len(transport.calls) == 1
        assert spend.calls == 1

    def test_local_provider_bypasses_gate_but_still_spends(self) -> None:
        """本地提供方免 vlm_online/密钥闸门,但每跳照常记账(红线 19)。"""
        cfg = Config()  # 默认 vlm_online=False、无密钥:本地提供方仍可协商
        transport = FakeTransport([(404, nf_body()), (200, ok_body())])
        spend = FakeSpend()
        result = negotiate("ollama", "bad-llava", cfg, transport=transport, spend=spend)
        assert result == "llava"
        assert [c["model"] for c in transport.calls] == ["bad-llava", "llava"]
        assert spend.calls == 2

    def test_model_name_with_colon_survives_and_pins(self) -> None:
        """openrouter 的 org/name:free 模型名:解析、探测、定格全程不裂开。"""
        cfg = make_cfg()
        model = "qwen/qwen2.5-vl-72b-instruct:free"
        transport = FakeTransport([(200, ok_body())])
        spend = FakeSpend()
        assert negotiate("openrouter", model, cfg, transport=transport, spend=spend) == (
            model
        )
        assert transport.calls[0]["model"] == model
        assert model_negotiate._NEGOTIATED[f"openrouter:{model}"] == model

    def test_unknown_provider_propagates_value_error(self) -> None:
        """未知提供方是配置错:ValueError 原样上抛,零外呼、零记账。"""
        transport = FakeTransport([])
        spend = FakeSpend()
        with pytest.raises(ValueError, match="未知视觉模型提供方"):
            negotiate("nosuch", "m", make_cfg(), transport=transport, spend=spend)
        assert transport.calls == []
        assert spend.calls == 0

    def test_vlm_client_absent_refuses(self, monkeypatch) -> None:
        """vlm_client 未就位:拒绝协商外呼(中文 RuntimeError)。"""
        _hide_sibling(monkeypatch, "vlm_client")
        with pytest.raises(RuntimeError, match="vlm_client"):
            negotiate(
                "glm",
                "glm-4.5v",
                make_cfg(),
                transport=FakeTransport([]),
                spend=FakeSpend(),
            )


# ---------------------------------------------------------------------------
# 真实目录 / 模型目录就位一致性(importorskip,一条)
# ---------------------------------------------------------------------------


def test_real_providers_and_catalog_consistency() -> None:
    """真实 providers(A61)/ model_catalog(A65)就位时:默认模型互相咬合,
    每个提供方的候选链非空、首项为当前模型、无重复、无占位符形态。"""
    providers = pytest.importorskip("netsentinel.vision.providers")
    catalog = pytest.importorskip("netsentinel.vision.model_catalog")
    pytest.importorskip("netsentinel.vision.provider_quirks")

    # 1) 目录 default_model(非空者)必须能在模型目录中找到(提示值一致)
    for name, spec in providers.PROVIDERS.items():
        if spec.default_model:
            known_ids = {info.id for info in catalog.MODELS.get(name, [])}
            assert spec.default_model in known_ids, (
                f"{name} 的目录默认模型 {spec.default_model} 不在 model_catalog"
            )

    # 2) 每个提供方(含 20 家全量)的候选链形状不变量
    for name in providers.PROVIDERS:
        chain = candidate_chain(name, "zz-current-model", Config())
        assert chain, f"{name} 候选链为空"
        assert chain[0] == "zz-current-model", f"{name} 候选链首项应为当前模型"
        assert len(chain) == len(set(chain)), f"{name} 候选链存在重复"
        for candidate in chain:
            assert _is_concrete(candidate), f"{name} 候选 {candidate} 含占位符形态"


def _is_concrete(name: str) -> bool:
    """测试侧独立复刻的占位符判定(避免直接引用被测私有函数造成同义反复)。"""
    return not any(ch in name for ch in "<>{}")


# ---------------------------------------------------------------------------
# V5 升级锁定:定格缓存加锁 / 并发首调合流 / negotiate.fallback 遥测
# ---------------------------------------------------------------------------


class TestV5Upgrades:
    def test_v5_concurrent_first_call_single_probe_round(self) -> None:
        """并发首调合流:双线程同时协商同键,只探测一轮链、只记一轮账(红线 19)。"""
        import threading
        import time

        class SlowTransport(FakeTransport):
            """慢速回放:拉开单轮探测时长,确保并发线程必然进入等待分支。"""

            def __call__(self, url, headers, payload, timeout):
                time.sleep(0.05)
                return super().__call__(url, headers, payload, timeout)

        cfg = make_cfg(vlm_provider_models={"glm": "bad-b"})
        # 链:bad-a → bad-b(覆盖)→ glm-5.3-flash(目录默认),共 3 跳
        transport = SlowTransport(
            [(404, nf_body("bad-a")), (404, nf_body("bad-b")), (200, ok_body())]
        )
        spend = FakeSpend()
        start = threading.Barrier(2)
        results: dict[int, str] = {}
        errors: dict[int, BaseException] = {}

        def worker(idx: int) -> None:
            start.wait()
            try:
                results[idx] = negotiate("glm", "bad-a", cfg, transport=transport, spend=spend)
            except BaseException as exc:  # noqa: BLE001 - 线程内异常带回主线程断言
                errors[idx] = exc

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert not errors, f"并发协商不应抛错:{errors}"
        assert results[0] == results[1] == "glm-5.3-flash"
        assert len(transport.calls) == 3, "并发首调应只探测一轮链(3 跳),而非两轮 6 次"
        assert spend.calls == 3, "并发首调应只记一轮账"
        assert model_negotiate._INFLIGHT == {}, "探测结束后合流槽必须释放"

    def test_v5_negotiate_fallback_telemetry(self) -> None:
        """换名成功定格计 negotiate.fallback 一次;首候选成功与定格复用均不计数。"""
        telemetry.reset()
        try:
            # 首候选(当前模型)成功:零换名,不计数
            first = FakeTransport([(200, ok_body())])
            assert (
                negotiate(
                    "glm",
                    "glm-4.5v",
                    make_cfg(vlm_provider_models={"glm": "glm-4.5v"}),
                    transport=first,
                    spend=FakeSpend(),
                )
                == "glm-4.5v"
            )
            assert telemetry.snapshot()["counters"].get("negotiate.fallback", 0) == 0

            # 404 → 换名成功:计一次
            cfg = make_cfg(vlm_provider_models={"glm": "glm-5.3-flash"})
            second = FakeTransport([(404, nf_body("bad-a")), (200, ok_body())])
            assert negotiate("glm", "bad-a", cfg, transport=second, spend=FakeSpend()) == (
                "glm-5.3-flash"
            )
            assert telemetry.snapshot()["counters"].get("negotiate.fallback") == 1

            # 进程内定格复用:零外呼,不重复计数
            assert (
                negotiate("glm", "bad-a", cfg, transport=FakeTransport([]), spend=FakeSpend())
                == "glm-5.3-flash"
            )
            assert telemetry.snapshot()["counters"].get("negotiate.fallback") == 1
        finally:
            telemetry.reset()

    def test_v5_inflight_released_after_full_failure(self) -> None:
        """全链失败上抛后合流槽必须释放:不残留、不定格,可立即重试。"""
        cfg = make_cfg(vlm_provider_models={"openai": "gone-b"})
        chain = candidate_chain("openai", "gone-a", cfg)
        transport = FakeTransport([(404, nf_body())] * len(chain))
        with pytest.raises(RuntimeError, match="全链失败"):
            negotiate("openai", "gone-a", cfg, transport=transport, spend=FakeSpend())
        assert model_negotiate._INFLIGHT == {}
        assert model_negotiate._NEGOTIATED == {}
