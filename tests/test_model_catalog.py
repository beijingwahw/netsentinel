"""A65 model_catalog 测试(纯离线,零外呼;模型名均为提示信息)。"""
from __future__ import annotations

import dataclasses

import pytest

from netsentinel.vision.model_catalog import (
    MODELS,
    UNCERTAIN_IDS,
    VALID_TAGS,
    ModelInfo,
    search,
    suggest,
    validate_model,
)

#: CONTRACTS-V4 §2 提供方表的 20 个键(与 A61 providers.PROVIDERS 保持一致)
EXPECTED_PROVIDERS = {
    "glm", "openai", "anthropic", "gemini", "qwen", "doubao", "hunyuan",
    "moonshot", "minimax", "stepfun", "siliconflow", "ernie", "openrouter",
    "groq", "together", "xai", "ollama", "vllm", "lmstudio", "xinference",
}

LOCAL_PROVIDERS = {"ollama", "vllm", "lmstudio", "xinference"}

#: A217 守卫模型族目录键(开放权重守卫模型,guard_adapter 本地推理接入;
#: 不是 API 提供方——providers.PROVIDERS 不含该键)。
GUARD_FAMILY_KEY = "guard"

#: guard 族的全部条目 id(与 model_catalog.MODELS["guard"] 对账用)。
GUARD_MODEL_IDS = {
    "google/shieldgemma-2-4b-it",
    "meta-llama/Llama-Guard-3-11B-vision",
    "meta-llama/Llama-Guard-4-12B",
}


# ---------------------------------------------------------------------------
# 目录完整性
# ---------------------------------------------------------------------------


def test_catalog_covers_all_contract_providers():
    """目录覆盖全部 20 家契约提供方,并额外收录 guard 守卫模型族(A217)。"""
    assert set(MODELS) == EXPECTED_PROVIDERS | {GUARD_FAMILY_KEY}


def test_guard_family_entries_and_capability_tags():
    """A217 guard 族:条目对齐 ModelInfo 结构,能力标记 local/open-weights/guard。"""
    entries = MODELS[GUARD_FAMILY_KEY]
    ids = [m.id for m in entries]
    assert GUARD_MODEL_IDS <= set(ids)
    for info in entries:
        assert {"local", "open-weights", "guard"} <= set(info.tags), info.id
        assert set(info.tags) <= set(VALID_TAGS), info.id


def test_guard_family_ids_all_uncertain():
    """guard 族为 HF 仓库命名,把握不足:逐条入册 UNCERTAIN_IDS 强制人工核验。"""
    for info in MODELS[GUARD_FAMILY_KEY]:
        assert f"{GUARD_FAMILY_KEY}:{info.id}" in UNCERTAIN_IDS, info.id


def test_guard_family_validates_and_searchable():
    """guard 族条目可被 validate_model 精确命中、可被 search 检索到。"""
    for info in MODELS[GUARD_FAMILY_KEY]:
        assert validate_model(GUARD_FAMILY_KEY, info.id)
    hits = search("守卫")
    assert {h.id for h in hits} >= GUARD_MODEL_IDS
    assert all("guard" in h.tags for h in hits)


def test_suggest_guard_tier_semantics():
    """guard/open-weights 为无回退档:守卫族命中守卫模型,非守卫提供方返回 None。"""
    assert suggest(GUARD_FAMILY_KEY, "guard") in GUARD_MODEL_IDS
    assert suggest(GUARD_FAMILY_KEY, "local") in GUARD_MODEL_IDS
    assert suggest("openai", "guard") is None          # 云端通用 VLM 不是守卫
    assert suggest("ollama", "guard") is None
    assert suggest("gemini", "open-weights") is None   # 闭源 API 档无开放权重
    assert suggest("ollama", "open-weights") == "llava"  # 本地开源走 local 回退


def test_every_provider_has_at_least_two_models():
    for provider, models in MODELS.items():
        assert len(models) >= 2, f"{provider} 目录条目不足 2 条"
    total = sum(len(v) for v in MODELS.values())
    assert total >= 40


def test_tags_validity_full_table():
    for provider, models in MODELS.items():
        for info in models:
            assert isinstance(info.id, str) and info.id, provider
            assert isinstance(info.tags, tuple) and info.tags, info.id
            assert set(info.tags) <= set(VALID_TAGS), info.id
            assert len(set(info.tags)) == len(info.tags), f"{info.id} 标签重复"
            assert all(t in VALID_TAGS for t in info.tags), info.id


def test_model_ids_unique_per_provider():
    for provider, models in MODELS.items():
        ids = [m.id for m in models]
        assert len(ids) == len(set(ids)), provider


def test_every_note_mentions_official_source():
    """红线 18:目录里的模型名是提示信息,每条 note 都须写明以官方为准。"""
    for provider, models in MODELS.items():
        for info in models:
            assert "以官方为准" in info.note, f"{provider}:{info.id}"


def test_uncertain_ids_marked_official():
    """不确定项必须显式列入 UNCERTAIN_IDS,且 note 含“以官方为准”。"""
    for key in UNCERTAIN_IDS:
        provider, _, model_id = key.partition(":")
        assert provider in MODELS, key
        ids = [m.id for m in MODELS[provider]]
        assert model_id in ids, key
        info = next(m for m in MODELS[provider] if m.id == model_id)
        assert "以官方为准" in info.note, key
    # 抽查若干明确不确定的条目确实入册
    assert {"gemini:gemini-2.0-pro", "qwen:qwen-vl-flash",
            "minimax:MiniMax-M2"} <= UNCERTAIN_IDS


def test_known_catalog_examples_present():
    assert "gpt-4o-mini" in [m.id for m in MODELS["openai"]]
    assert "gpt-4o" in [m.id for m in MODELS["openai"]]
    assert "glm-5.3-flash" in [m.id for m in MODELS["glm"]]
    assert any("haiku" in m.id for m in MODELS["anthropic"])
    assert "qwen-vl-max" in [m.id for m in MODELS["qwen"]]
    assert "llava" in [m.id for m in MODELS["ollama"]]


def test_openrouter_has_free_tagged_model():
    free_models = [m for m in MODELS["openrouter"] if "free" in m.tags]
    assert free_models and any(m.id.endswith(":free") for m in free_models)


def test_local_providers_models_all_tagged_local():
    for provider in LOCAL_PROVIDERS:
        assert all("local" in m.tags for m in MODELS[provider]), provider


def test_modelinfo_is_frozen():
    info = ModelInfo("x", ("cheap",), "备注")
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.id = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# suggest
# ---------------------------------------------------------------------------


def test_suggest_default_balanced():
    assert suggest("openai") == "gpt-4o-mini"
    assert suggest("glm") == "glm-5.3-flash"
    assert suggest("ollama") == "llama3.2-vision"
    assert suggest("doubao") == "doubao-1.5-vision-pro"


def test_suggest_tiers():
    assert suggest("anthropic", "cheap") == "claude-haiku-4-5"
    assert suggest("gemini", "flagship") == "gemini-2.0-pro"
    assert suggest("qwen", "flagship") == "qwen-vl-max"
    assert suggest("qwen", "cheap") == "qwen-vl-flash"
    assert suggest("openai", "flagship") == "gpt-4o"
    assert suggest("ollama", "cheap") == "llava"
    assert suggest("ollama", "flagship") == "qwen2.5vl"
    assert suggest("openrouter", "free") == "qwen/qwen2.5-vl-72b-instruct:free"


def test_suggest_fallback_chain():
    # openai 无 free 档 → 回退到 cheap
    assert suggest("openai", "free") == "gpt-4o-mini"
    # qwen 无 balanced 命中链上 balanced 标签存在 → qwen-vl-plus
    assert suggest("qwen", "balanced") == "qwen-vl-plus"


def test_suggest_local_need_on_cloud_provider_returns_none():
    assert suggest("openai", "local") is None
    assert suggest("gemini", "local") is None


def test_suggest_local_need_on_local_provider():
    assert suggest("ollama", "local") == "llava"
    assert suggest("vllm", "local") == "Qwen/Qwen2.5-VL-7B-Instruct"


def test_suggest_unknown_provider_returns_none():
    assert suggest("不存在的提供方") is None
    assert suggest("") is None


def test_suggest_unknown_need_raises():
    with pytest.raises(ValueError, match="有效值"):
        suggest("openai", "expensive")


def test_suggest_results_always_in_provider_catalog():
    for provider, models in MODELS.items():
        ids = {m.id for m in models}
        for need in VALID_TAGS:
            picked = suggest(provider, need)
            if picked is not None:
                assert picked in ids, (provider, need, picked)
                assert validate_model(provider, picked)


# ---------------------------------------------------------------------------
# validate_model
# ---------------------------------------------------------------------------


def test_validate_exact_hits():
    assert validate_model("openai", "gpt-4o-mini")
    assert validate_model("glm", "glm-4.5v")
    assert validate_model("gemini", "gemini-2.0-flash")
    assert validate_model("qwen", "qwen-vl-max")
    assert validate_model("anthropic", "claude-sonnet-4")


def test_validate_case_insensitive_and_trim():
    assert validate_model("openai", "GPT-4O-MINI")
    assert validate_model("OpenAI", "gpt-4o")
    assert validate_model("ollama", "  llava  ")


def test_validate_endpoint_wildcard_doubao_only():
    assert validate_model("doubao", "ep-20250101000000-abc123")
    assert not validate_model("doubao", "ep-abc")  # 过短
    assert not validate_model("openai", "ep-20250101000000-abc123")  # 仅限豆包


def test_validate_open_source_path_wildcard():
    assert validate_model("siliconflow", "Qwen/Qwen2.5-VL-32B-Instruct")
    assert validate_model("vllm", "OpenGVLab/InternVL3-14B")
    assert validate_model("openrouter", "deepseek-ai/deepseek-vl2:free")
    assert validate_model("together", "Qwen/Qwen2.5-VL-72B-Instruct")
    assert not validate_model("siliconflow", "Qwen//bad")
    assert not validate_model("siliconflow", "Qwen/")


def test_validate_version_tag_wildcard():
    assert validate_model("ollama", "llava:13b")
    assert validate_model("ollama", "llava:latest")
    assert validate_model("ollama", "qwen2.5vl:7b")
    assert validate_model("openai", "gpt-4o:free")
    assert not validate_model("openai", "no-such-base:7b")  # 基名不在目录
    assert not validate_model("openai", "gpt-4o:cheap")  # 非白名单后缀


def test_validate_illegal_inputs():
    assert not validate_model("openai", "")
    assert not validate_model("openai", "   ")
    assert not validate_model("openai", "gpt 4o")  # 空格
    assert not validate_model("openai", "gpt-4o?x=1")  # 查询串
    assert not validate_model("openai", "gpt-4o#frag")
    assert not validate_model("unknown-provider", "gpt-4o")
    assert not validate_model("openai", "claude-sonnet")  # 串门模型
    assert not validate_model("anthropic", "claude-sonnet")  # 裸家族名(无版本)
    assert not validate_model("openai", None)  # type: ignore[arg-type]
    assert not validate_model(None, "gpt-4o")  # type: ignore[arg-type]


def test_validate_every_catalog_id():
    for provider, models in MODELS.items():
        for info in models:
            assert validate_model(provider, info.id), f"{provider}:{info.id}"


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_english_keyword():
    hits = search("flash")
    ids = [h.id for h in hits]
    assert "glm-5.3-flash" in ids
    assert "gemini-2.0-flash" in ids
    assert all("flash" in (h.id + h.note).lower() for h in hits)


def test_search_case_insensitive():
    hits = search("GPT-4O")
    assert [h.id for h in hits] == ["gpt-4o-mini", "gpt-4o", "openai/gpt-4o-mini"]


def test_search_chinese_keyword_local():
    hits = search("本地")
    assert len(hits) >= 10
    assert all("local" in h.tags for h in hits)


def test_search_chinese_keyword_flagship():
    hits = search("旗舰")
    assert hits
    assert all("flagship" in h.tags for h in hits)


def test_search_chinese_keyword_free():
    hits = search("免费")
    assert len(hits) >= 3
    assert all("free" in h.tags or "cheap" in h.tags for h in hits)


def test_search_miss_and_blank():
    assert search("根本不存在的关键词xyzzy") == []
    assert search("") == []
    assert search("   ") == []
    assert search(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 与 A61 providers 目录的一致性(未合入时跳过)
# ---------------------------------------------------------------------------


def test_models_keys_match_providers_registry():
    """API 提供方注册表 ⊆ 目录键;guard 族是目录专有键(非 API 提供方)。"""
    pytest.importorskip("netsentinel.vision.providers")
    from netsentinel.vision.providers import PROVIDERS

    assert set(PROVIDERS) == set(MODELS) - {GUARD_FAMILY_KEY}
    assert GUARD_FAMILY_KEY not in PROVIDERS  # 守卫模型本地推理,不走 API 提供方


# ---------------------------------------------------------------------------
# V5:索引预构建 + 查表与全表扫描严格等价 + 遥测
# ---------------------------------------------------------------------------


def _brute_search(keyword):
    """V4 全表扫描参考实现(与旧 search 语义一致:id 小写化、note 原文)。"""
    if not isinstance(keyword, str):
        return []
    kw = keyword.strip().lower()
    if not kw:
        return []
    hits: list[ModelInfo] = []
    for models in MODELS.values():
        for info in models:
            if kw in info.id.lower() or kw in info.note:
                hits.append(info)
    return hits


#: 等价性抽查关键词:英文/数字/符号/中文/大小写变体/未命中
_V5_KEYWORDS = (
    "flash", "glm", "gpt", "4o", "qwen", "vl", "vision", "llava", "free",
    ":free", "/", "-", "2.5", "2.0", "latest", "maverick", "scout",
    "GPT-4O", "LlAvA", "Qwen", "  flash  ",
    "本地", "旗舰", "免费", "官方为准", "轻量", "智谱", "豆包", "数据不出本机",
    "根本不存在的关键词xyzzy", "", "   ",
)


def test_v5_search_equivalent_to_full_scan_reference():
    """V5 索引版 search 与 V4 全表扫描参考在全部抽查关键词上逐条等价。"""
    for kw in _V5_KEYWORDS:
        assert search(kw) == _brute_search(kw), f"search({kw!r}) 与全扫参考不一致"


def test_v5_search_equivalent_for_every_catalog_substring():
    """穷举:每个模型 id 与 note 的各长子串(及单字符)均与全扫参考等价。"""
    checked = 0
    seen_kw: set[str] = set()
    for models in MODELS.values():
        for info in models:
            for text in (info.id.lower(), info.note):
                for size in (1, 2, 3, len(text)):
                    for start in range(0, max(1, len(text) - size + 1)):
                        kw = text[start:start + size].strip().lower()
                        if not kw or kw in seen_kw:
                            continue
                        seen_kw.add(kw)
                        assert search(kw) == _brute_search(kw), f"kw={kw!r}"
                        checked += 1
    assert checked >= 500  # 抽样规模下限,防目录变动后测试空转


def test_v5_indexes_prebuilt_at_import():
    """三类索引在模块加载时一次性建好(非惰性),且与 MODELS 严格一致。"""
    from netsentinel.vision import model_catalog as mc

    total = sum(len(v) for v in MODELS.values())
    assert len(mc._ALL_INFOS) == total
    # suggest 索引:标签 → 目录声明顺序下的首个模型 id,与全表扫描一致
    for provider, models in MODELS.items():
        expect_first: dict[str, str] = {}
        for info in models:
            for tag in info.tags:
                expect_first.setdefault(tag, info.id)
        assert mc._SUGGEST_INDEX[provider] == expect_first, provider
        assert mc._FIRST_MODEL[provider] == models[0].id
        assert mc._LOWER_IDS[provider] == frozenset(m.id.lower() for m in models)
    assert mc._SEARCH_INDEX  # 倒排桶非空


#: V4 suggest 参考实现:标签回退链 + 线性扫目录声明顺序
#: (A217:open-weights/guard 与 local 同为"无回退档",链上未命中返回 None)
_V5_CHAINS = {
    "balanced": ("balanced", "cheap"),
    "cheap": ("cheap", "balanced"),
    "flagship": ("flagship", "balanced", "cheap"),
    "free": ("free", "cheap"),
    "local": ("local",),
    "open-weights": ("open-weights", "local"),
    "guard": ("guard",),
}

_V5_NO_FALLBACK = frozenset({"local", "open-weights", "guard"})


def _brute_suggest(provider, need="balanced"):
    key = str(provider or "").strip().lower()
    wanted = ("balanced" if need is None else str(need)).strip().lower()
    if wanted not in VALID_TAGS:
        raise ValueError(f"未知的需求档位 need={need!r},有效值:{'/'.join(VALID_TAGS)}")
    models = MODELS.get(key)
    if not models:
        return None
    for tag in _V5_CHAINS[wanted]:
        for info in models:
            if tag in info.tags:
                return info.id
    if wanted in _V5_NO_FALLBACK:
        return None
    return models[0].id


def test_v5_suggest_equivalent_to_linear_scan_reference():
    """V5 查表版 suggest 与 V4 线性扫参考在全目录 × 全档位上逐格等价。"""
    for provider in list(MODELS) + ["", "unknown", "GLM", " OpenAI "]:
        for need in VALID_TAGS:
            assert suggest(provider, need) == _brute_suggest(provider, need), (
                provider, need,
            )


def test_v5_validate_catalog_hits_equivalent_to_linear_scan():
    """V5 查表版 validate 的目录命中与 V4 逐条 lower 比较等价(含大小写变体)。"""
    for provider, models in MODELS.items():
        lowers = {m.id.lower() for m in models}
        for info in models:
            for variant in (info.id, info.id.lower(), info.id.upper(), info.id.title()):
                assert validate_model(provider, variant) == (variant.strip().lower() in lowers), (
                    provider, variant,
                )


def test_v5_search_telemetry_counter():
    """可观测性:有效检索计 telemetry 'catalog.search';空白/非字符串不计。"""
    from netsentinel import telemetry

    telemetry.reset()
    try:
        assert search("flash")  # 有效
        assert search("   ") == []  # 空白不计
        assert search(None) == []  # type: ignore[arg-type]
        snap = telemetry.snapshot()
        assert snap["counters"].get("catalog.search") == 1.0
    finally:
        telemetry.reset()
