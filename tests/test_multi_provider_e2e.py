# -*- coding: utf-8 -*-
"""多平台统一接入端到端测试(NetSentinel V4 · A78)。

证明"目录里**每个**提供方都能真正接入跑通识别"——从 ``build_classifier``
的 ``提供方[:模型]`` 语法出发,经 providers.resolve(A61)→ UniversalVLMClient
三方言请求构造(A62)→ UniversalVLMClassifier 缓存/预算/校验校准(A63),
一直到 failover(A68)与 orchestrator 混编 ensemble 的整链路。

覆盖四组场景(契约 §4 A78):

1. **全目录遍历**:parametrize ``providers.PROVIDERS`` 全部 20 家,monkeypatch
   ``vlm_client._http_post_json`` 缺省传输为本地回放(openai/anthropic/gemini
   三模板按 style 选),build_classifier → classify(demo 站 nsfw_hi_1.png)
   → 断言 nsfw_prob>0.9 且 model 含提供方名、请求按方言接线正确;
2. **failover 端到端**:链 ["openai:gpt-4o-mini","ollama:llava"],第一家
   传输层抛 RuntimeError、第二家成功 → model == "failover→ollama:llava";
3. **混编 ensemble**:orchestrator.run_scan(注入 capture 返回含 nsfw_hi 图的
   PageSample;cfg.classifier="stub" + ensemble_members=["stub",
   "openai:gpt-4o-mini"])→ NSFW 判定且集成评分含两家成员模型;
4. **密钥安全**:传输层捕获的 Authorization / x-api-key / x-goog-api-key 值
   等于注入的测试键(证明接线正确),且 DEBUG 级日志中无任何完整密钥;
   离线闸门(vlm_online 关或无密钥)在任何传输发生前即 fail-closed。

安全红线(V4 §0 第 16/17/19/20 条):
- **全部 mock transport,零外呼**;不访问任何真实门户与真实 VLM 接口;
- 兄弟模块未就位用 ``pytest.importorskip`` 容错(skip 是可接受结局,
  就位时全跑);核心四件套(providers/vlm_client/multi_provider/vlm_cache)
  缺任一则整文件跳过——端到端没有它们无从谈起;
- 所有落盘产物(缓存库 / 复核队列 / 审计日志)只写 ``tmp_path``。
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from netsentinel.contracts import Config, ImageEvidence, PageSample, Verdict

# ---- 端到端核心链路依赖(未就位则整文件跳过)-------------------------------
providers = pytest.importorskip("netsentinel.vision.providers")
vlm_client = pytest.importorskip("netsentinel.vision.vlm_client")
pytest.importorskip("netsentinel.vision.multi_provider")
pytest.importorskip("netsentinel.vision.vlm_cache")

from netsentinel.vision.multi_provider import build_classifier  # noqa: E402
from netsentinel.vision.vlm_cache import VlmCache  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "demo_site"

#: mock 评分内容:nsfw_prob=0.99 经 A22 calibrate(锚点表)后 ≈ 0.986 > 0.9
MOCK_NSFW_CONTENT: dict[str, Any] = {
    "nsfw_prob": 0.99,
    "categories": ["色情"],
    "reasoning": "画面含明显色情内容(本地模拟响应,零外呼)",
    "confidence": 0.95,
}


# ---------------------------------------------------------------------------
# 工具:配置 / 图片证据 / 回放传输层 / 三方言响应模板
# ---------------------------------------------------------------------------


def make_cfg(tmp_path: Path, **overrides: Any) -> Config:
    """端到端配置:云端 16 家全部注入测试键,缓存库只写 tmp_path。"""
    params: dict[str, Any] = dict(
        vlm_online=True,
        vlm_api_keys={
            name: f"sk-e2e-{name}"
            for name, spec in providers.PROVIDERS.items()
            if not spec.local
        },
        vlm_cache_db=str(tmp_path / "vlm_cache.db"),
        vlm_daily_budget=200,
        fetch_delay_s=0.0,
    )
    params.update(overrides)
    return Config(**params)


def fixture_evidence(name: str = "nsfw_hi_1.png") -> ImageEvidence:
    """demo 站夹具图片的 ImageEvidence(宽高 200x200,sha256 按文件内容计算)。"""
    path = FIXTURE_DIR / name
    assert path.is_file(), f"缺少演示站点夹具图片:{path}"
    return ImageEvidence(
        path=str(path),
        url=f"https://demo-e2e.invalid/{name}",
        source_page="https://demo-e2e.invalid/index.html",
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        width=200,
        height=200,
    )


def dialect_body(style: str, content: dict[str, Any]) -> str:
    """按 API 方言构造一次"合法响应"(三模板,本地生成、零网络)。"""
    text = json.dumps(content, ensure_ascii=False)
    if style == "openai":
        return json.dumps(
            {"choices": [{"message": {"content": text}}]}, ensure_ascii=False
        )
    if style == "anthropic":
        return json.dumps(
            {"content": [{"type": "text", "text": text}]}, ensure_ascii=False
        )
    if style == "gemini":
        return json.dumps(
            {"candidates": [{"content": {"parts": [{"text": text}]}}]},
            ensure_ascii=False,
        )
    raise AssertionError(f"未知方言:{style}")


#: 回放路由:(url, headers, payload) -> (HTTP 状态码, 响应体文本),也可抛异常
RouteFn = Callable[[str, dict[str, str], dict[str, Any]], tuple[int, str]]


class ReplayTransport:
    """注入 ``vlm_client._http_post_json`` 的本地回放传输层(零网络)。

    先记录每一次"外呼"(url/headers/payload/timeout)再交给路由函数,
    路由函数返回 (status, body) 或直接抛异常以模拟传输层故障。
    """

    def __init__(self, route: RouteFn) -> None:
        self.route = route
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float = 90.0,
        **_: Any,
    ) -> tuple[int, str]:
        self.calls.append(
            {"url": url, "headers": dict(headers), "payload": payload, "timeout": timeout}
        )
        return self.route(url, headers, payload)

    @classmethod
    def always(cls, body: str, status: int = 200) -> ReplayTransport:
        """固定回放同一响应体的便捷构造。"""
        return cls(lambda url, headers, payload: (status, body))


def install_transport(
    monkeypatch: pytest.MonkeyPatch, transport: ReplayTransport
) -> ReplayTransport:
    """替换 vlm_client 的缺省传输(红线 20:测试零外呼)。"""
    monkeypatch.setattr(vlm_client, "_http_post_json", transport)
    return transport


def catalog_spec(provider: str) -> str:
    """目录条目 → 分类器 spec:有默认模型用提供方名,本地无默认三家拼 :test-model。"""
    default_model = providers.PROVIDERS[provider].default_model
    return provider if default_model else f"{provider}:test-model"


# ---------------------------------------------------------------------------
# 1. 全目录遍历:20 家提供方逐一真接线跑通
# ---------------------------------------------------------------------------


class TestFullCatalogTraversal:
    """目录里每个提供方都能 build_classifier → classify 跑通识别(零外呼)。"""

    def test_catalog_has_all_20_providers(self) -> None:
        """契约 §2 权威清单必须恰好 20 家(遍历参数化的完整性护栏)。"""
        assert len(providers.PROVIDERS) == 20, (
            f"目录应含 20 家提供方,实际 {len(providers.PROVIDERS)}:"
            f"{sorted(providers.PROVIDERS)}"
        )
        assert sum(1 for s in providers.PROVIDERS.values() if s.local) == 4, (
            "本地提供方应为 4 家(ollama/vllm/lmstudio/xinference)"
        )

    @pytest.mark.parametrize("provider", list(providers.PROVIDERS))
    def test_every_provider_scores_nsfw_hi_image(
        self, provider: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """单提供方全链路:spec → resolve → 三方言请求 → 校准 → ImageScore。"""
        spec = providers.PROVIDERS[provider]
        expected_model = spec.default_model or "test-model"
        overrides: dict[str, Any] = {}
        if not spec.default_model:
            # 无默认模型的本地三家(vllm/lmstudio/xinference):A63 build_classifier
            # 内部向 resolve 传的是裸提供方名,spec 里的模型需经契约的
            # cfg.vlm_provider_models 覆盖兜底(A61 的"本地必须指定模型"校验
            # 先于 A63 的 parsed-model 回填触发,详见 test_local_no_default_model_paths)
            overrides["vlm_provider_models"] = {provider: "test-model"}
        cfg = make_cfg(tmp_path, **overrides)
        transport = install_transport(
            monkeypatch, ReplayTransport.always(dialect_body(spec.style, MOCK_NSFW_CONTENT))
        )

        clf = build_classifier(catalog_spec(provider), cfg)
        score = clf.classify(fixture_evidence())

        # 识别结论与模型命名(含提供方名)
        assert provider in score.model, f"model 应含提供方名:{score.model}"
        assert score.model == f"{provider}:{expected_model}"
        assert score.nsfw_prob > 0.9, f"{provider} 评分应 > 0.9,实际 {score.nsfw_prob}"
        assert score.scores.get("provider") == provider
        assert "色情" in score.scores.get("categories", [])

        # 接线正确:恰好一次"外呼"(回放),按方言落到正确端点与模型
        assert len(transport.calls) == 1, "应恰好发起一次(回放的)评分请求"
        call = transport.calls[0]
        assert call["url"].startswith(spec.base_url.rstrip("/")), (
            f"{provider} 请求应落在目录端点:{call['url']}"
        )
        if spec.style == "openai":
            assert call["url"].endswith("/chat/completions")
            assert call["payload"]["model"] == expected_model
        elif spec.style == "anthropic":
            assert call["url"].endswith("/messages")
            assert call["payload"]["model"] == expected_model
            assert call["payload"]["max_tokens"] >= 1, "anthropic 方言 max_tokens 必填"
        else:  # gemini:模型名在 URL 的 :generateContent 动作里
            assert f"/models/{expected_model}:generateContent" in call["url"]
            assert (
                call["payload"]["generationConfig"]["responseMimeType"]
                == "application/json"
            )

        # 密钥接线:云端带认证头(值为注入的测试键),本地免认证头
        headers = call["headers"]
        if spec.local:
            assert not any(
                k.lower() in ("authorization", "x-api-key", "x-goog-api-key")
                for k in headers
            ), "本地提供方不应携带认证头"
        else:
            auth = {
                k.lower(): v
                for k, v in headers.items()
                if k.lower() in ("authorization", "x-api-key", "x-goog-api-key")
            }
            assert len(auth) == 1, f"{provider} 应携带且仅携带一个认证头:{headers}"
            assert f"sk-e2e-{provider}" in next(iter(auth.values()))

    def test_local_no_default_model_paths(self, tmp_path: Path) -> None:
        """本地无默认模型三家:resolve 两种语法的行为边界(A61 契约口径)。

        - ``resolve("vllm")``(无模型、无 cfg 覆盖)→ ValueError 中文"必须指定模型";
        - ``resolve("vllm:test-model")``(提供方:模型语法)→ 正常解析出本地成员;
        - ``build_classifier("vllm:test-model", cfg)`` 配合 ``vlm_provider_models``
          覆盖 → 统一分类器定格为 ``vllm:test-model``(全目录遍历采用的路径)。

        已知接线期待办(报负责人):A63 ``build_classifier`` 向 ``resolve`` 传的是
        裸提供方名,spec 中的模型未透传,无 cfg 覆盖时会先触发 A61 的必填校验;
        两侧当前公共可行路径即上面的 cfg 覆盖,本用例将其固定为回归口径。
        """
        with pytest.raises(ValueError, match="必须指定模型"):
            providers.resolve("vllm", Config())
        resolved = providers.resolve("vllm:test-model", Config())
        assert (resolved.provider, resolved.model, resolved.local) == (
            "vllm",
            "test-model",
            True,
        )
        cfg = make_cfg(tmp_path, vlm_provider_models={"vllm": "test-model"})
        clf = build_classifier("vllm:test-model", cfg)
        assert clf.name == "vllm:test-model"
        assert clf.resolved.local is True

    def test_cross_provider_shared_budget_ledger(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """红线 19:跨平台共用一本预算账——两家共用同一 cfg 各评一次,used 应为 2。"""
        cfg = make_cfg(tmp_path)
        transport = install_transport(
            monkeypatch,
            ReplayTransport.always(dialect_body("openai", MOCK_NSFW_CONTENT)),
        )
        img = fixture_evidence()
        for provider in ("glm", "openai"):  # 两家都是 openai 方言,共用同一回放
            build_classifier(catalog_spec(provider), cfg).classify(img)
        assert len(transport.calls) == 2
        state = VlmCache(cfg.vlm_cache_db).budget_state()
        assert state["used"] == 2, f"两平台应共用一本账各记 1 次,实际 {state}"


# ---------------------------------------------------------------------------
# 2. failover 端到端:第一家传输故障 → 自动切本地 ollama
# ---------------------------------------------------------------------------


class TestFailoverEndToEnd:
    """故障转移链端到端(真实 build_classifier 成员 + 注入回放传输,零外呼)。"""

    def test_first_member_transport_failure_falls_back_to_local(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        failover_mod = pytest.importorskip("netsentinel.vision.failover")
        ok_body = dialect_body("openai", MOCK_NSFW_CONTENT)

        def route(
            url: str, headers: dict[str, str], payload: dict[str, Any]
        ) -> tuple[int, str]:
            if "api.openai.com" in url:
                raise RuntimeError("模拟 openai 平台传输层故障(测试注入,零外呼)")
            return 200, ok_body  # ollama 本地(OpenAI 兼容口)成功

        transport = install_transport(monkeypatch, ReplayTransport(route))
        cfg = make_cfg(
            tmp_path, vlm_fallback_chain=["openai:gpt-4o-mini", "ollama:llava"]
        )

        clf = failover_mod.FailoverClassifier(cfg)
        score = clf.classify(fixture_evidence())

        assert score.model == "failover→ollama:llava", f"胜者标注错误:{score.model}"
        assert score.nsfw_prob > 0.9
        assert score.scores.get("fallback_from") == ["openai:gpt-4o-mini"], (
            "应记录先前失败成员:openai:gpt-4o-mini"
        )
        assert score.scores.get("provider") == "ollama"
        # 两次"外呼":第一家一发即抛,第二家一发成功
        assert len(transport.calls) == 2
        assert "api.openai.com" in transport.calls[0]["url"]
        assert "127.0.0.1:11434" in transport.calls[1]["url"]


# ---------------------------------------------------------------------------
# 3. 混编 ensemble:run_scan 里 stub + openai:gpt-4o-mini 同台
# ---------------------------------------------------------------------------


class TestMixedEnsembleRunScan:
    """orchestrator.run_scan 混编本地桩成员与云端统一接入成员(零外呼)。"""

    def test_stub_plus_openai_member_yields_nsfw(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        mods = {
            name: pytest.importorskip(name)
            for name in (
                "netsentinel.pipeline.orchestrator",
                "netsentinel.vision.ensemble",
                "netsentinel.decision.verdict",
                "netsentinel.decision.review_queue",
                "netsentinel.evidence.packager",
                "netsentinel.logging_util",
            )
        }
        orchestrator = mods["netsentinel.pipeline.orchestrator"]

        data_dir = tmp_path / "data"
        cfg = make_cfg(
            tmp_path,
            classifier="stub",
            ensemble_members=["stub", "openai:gpt-4o-mini"],
            use_fusion=False,  # 聚焦多平台成员链路;融合为可选增强项
            data_dir=str(data_dir),
            evidence_dir=str(data_dir / "evidence"),
            db_path=str(data_dir / "review_queue.db"),
            audit_path=str(data_dir / "audit.jsonl"),
            log_path=str(data_dir / "logs" / "netsentinel.log"),
        )
        transport = install_transport(
            monkeypatch,
            ReplayTransport.always(dialect_body("openai", MOCK_NSFW_CONTENT)),
        )
        # 链接发现注入为单页(离线);页面采样注入返回含 4 张 nsfw_hi 夹具图
        monkeypatch.setattr(
            orchestrator, "_default_discover", lambda url, c, fetch_page=None: [url]
        )

        def capture(url: str, c: Config, *, fetch_page: Any = None) -> PageSample:
            return PageSample(
                url=url,
                screenshot_path="",
                image_evidences=[
                    fixture_evidence(f"nsfw_hi_{i}.png") for i in range(1, 5)
                ],
            )

        site = "https://demo-mixed-e2e.invalid/index.html"
        report = orchestrator.run_scan(site, cfg, capture=capture)

        # NSFW 判定(仍须人工复核)
        assert report.verdict is Verdict.NSFW, (
            f"4 张 nsfw_hi 图混编两成员应判 NSFW,实际 {report.verdict}"
            f"(agg={report.agg_nsw_prob})"
        )
        assert report.needs_review is True
        assert report.agg_nsw_prob > 0.9

        # 集成评分:每张图都应同时含两家成员模型的分
        assert report.image_scores, "应产出集成评分"
        for entry in report.image_scores:
            assert entry.model == "ensemble"
            members = entry.scores.get("members", {})
            assert set(members) == {"stub", "openai:gpt-4o-mini"}, (
                f"集成成员应恰为两家:{members}"
            )
            assert members["stub"] > 0.9 and members["openai:gpt-4o-mini"] > 0.9

        # 云端成员确实走了一次/图的统一接入链路(回放,零外呼)
        assert len(transport.calls) == 4
        assert all(
            call["url"].startswith("https://api.openai.com/v1/chat/completions")
            and call["payload"]["model"] == "gpt-4o-mini"
            for call in transport.calls
        )
        # 红线 19:云端成员的 4 次评分均入同一本预算账
        assert VlmCache(cfg.vlm_cache_db).budget_state()["used"] == 4

        # 判定闭环:pending 条目进入人工复核队列(机器初筛,人工拍板)
        queue = mods["netsentinel.decision.review_queue"].ReviewQueue(cfg.db_path)
        try:
            pending = queue.list("pending")
        finally:
            queue.close()
        assert len(pending) == 1, "NSFW 判定应写入一条 pending 复核条目"
        assert pending[0].site_url == site
        assert pending[0].verdict == Verdict.NSFW.value


# ---------------------------------------------------------------------------
# 4. 密钥安全:认证头接线正确 + 日志无真实密钥 + 离线 fail-closed
# ---------------------------------------------------------------------------


class TestKeyWiringAndSafety:
    """密钥只进认证头、绝不进日志;离线闸门在任何传输前拦截。"""

    @pytest.mark.parametrize(
        ("provider", "header_name", "expected_value"),
        [
            ("openai", "Authorization", "Bearer sk-e2e-openai"),
            ("anthropic", "x-api-key", "sk-e2e-anthropic"),
            ("gemini", "x-goog-api-key", "sk-e2e-gemini"),
        ],
    )
    def test_auth_headers_carry_injected_test_keys(
        self,
        provider: str,
        header_name: str,
        expected_value: str,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """三方言认证头 = 注入的测试键(接线正确),且 DEBUG 日志无完整密钥。"""
        style = providers.PROVIDERS[provider].style
        cfg = make_cfg(tmp_path)
        transport = install_transport(
            monkeypatch, ReplayTransport.always(dialect_body(style, MOCK_NSFW_CONTENT))
        )
        with caplog.at_level(logging.DEBUG, logger="netsentinel"):
            score = build_classifier(catalog_spec(provider), cfg).classify(
                fixture_evidence()
            )
        assert score.nsfw_prob > 0.9
        assert len(transport.calls) == 1

        # 接线正确:认证头携带的正是注入的测试键
        assert transport.calls[0]["headers"].get(header_name) == expected_value, (
            f"{provider} 的 {header_name} 应为注入的测试键"
        )

        # 红线 17:DEBUG 级日志已打印打码后的请求头,但完整密钥绝不出现
        assert "VLM 请求已构造" in caplog.text, "应产生 DEBUG 级请求构造日志(打码头)"
        raw_key = expected_value.removeprefix("Bearer ")
        assert raw_key not in caplog.text, "完整密钥不得出现在任何日志记录中"

    @pytest.mark.parametrize(
        ("vlm_online", "with_key"),
        [(False, True), (True, False)],
        ids=["vlm_online=False", "无密钥"],
    )
    def test_offline_gate_blocks_before_any_transport(
        self,
        vlm_online: bool,
        with_key: bool,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """红线 16:云端双条件缺一 → VlmConfigError,且一次传输都不发生。"""
        cfg = make_cfg(
            tmp_path,
            vlm_online=vlm_online,
            vlm_api_keys={"openai": "sk-e2e-openai"} if with_key else {},
        )
        transport = install_transport(
            monkeypatch, ReplayTransport.always(dialect_body("openai", MOCK_NSFW_CONTENT))
        )
        clf = build_classifier("openai:gpt-4o-mini", cfg)
        with pytest.raises(vlm_client.VlmConfigError, match="前置条件"):
            clf.classify(fixture_evidence())
        assert transport.calls == [], "离线闸门应在任何网络传输之前拦截(零外呼)"
