"""A150 capability 测试(纯离线,零外呼;模型名均为提示级形态样本)。

覆盖契约要求:

- :data:`VISION_PATTERNS` 表形状(≥18 条、全部 IGNORECASE 编译);
- **每条模式 ≥1 正例**(含"逐一模式扫描正例表"的专项断言);
- 明确负例:llama3 纯文本 / gemma-7b 文本 / gpt-3.5 / 文本嵌入模型名;
- 纯文本家族不误伤专项表(通义/glm/deepseek/中文系/开源文本逐族核对,
  含 ``preview``/``v2`` 词中带 v 的陷阱);
- 边界:大小写、版本后缀(llava:13b / qwen2.5vl:7b / gpt-4o-mini)、
  org 路径前缀、首尾空白、超长截断;
- ``filter_vision`` 去重保序 / 空 / None / 非字符串项。
"""
from __future__ import annotations

import re

import pytest

from netsentinel.vision.capability import VISION_PATTERNS, filter_vision, is_vision_model

# ---------------------------------------------------------------------------
# 样本表:正例(家族 → 模型名;覆盖 VISION_PATTERNS 的每一条)
# ---------------------------------------------------------------------------

POSITIVES: dict[str, list[str]] = {
    "llava/bakllava": ["llava", "llava:13b", "llava:latest", "bakllava",
                       "llava-1.5-7b-hf", "LLAVA:LATEST"],
    "llama vision/vl": ["llama3.2-vision", "llama-3.2-vision:90b",
                        "Llama-3.2-11B-Vision-Instruct"],
    "qwen vl/vision": ["qwen-vl-max", "qwen2.5vl:7b", "qwen2.5-vl-7b-instruct",
                       "Qwen/Qwen2.5-VL-72B-Instruct"],
    "minicpm-v": ["minicpm-v", "minicpm-v:8b", "MiniCPM-V-2_6", "minicpm4-v"],
    "moondream": ["moondream", "moondream2"],
    "gemma vl(带边界)": ["gemma-3-4b-vl", "Gemma-3-27b-vl-it", "gemma-3-12b-vl"],
    "cogvlm": ["cogvlm", "cogvlm2-19b"],
    "internvl": ["internvl", "OpenGVLab/InternVL2_5-8B"],
    "phi vision/multimodal": ["phi-3-vision", "phi-3.5-vision-instruct",
                              "phi-4-multimodal-instruct"],
    "deepseek-vl": ["deepseek-vl", "deepseek-vl2", "deepseek-vl-chat:7b"],
    "idefics": ["idefics-9b-instruct", "idefics2-8b"],
    "gpt-4o/4.1/4-vision": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini",
                            "gpt-4-vision-preview"],
    "claude 3/sonnet/haiku/opus": ["claude-3-opus-20240229",
                                   "claude-3-5-sonnet-latest", "claude-sonnet-4",
                                   "claude-haiku-4-5", "claude-opus-4-5"],
    "gemini": ["gemini-1.5-pro", "gemini-2.0-flash", "GEMINI-2.5-PRO"],
    "glm-4v 系": ["glm-4v", "glm-4.5v", "glm-4v-flash", "GLM-4V", "glm4v:9b"],
    "glm vl 形态": ["glm-4vl", "glm-4.6vl"],
    "step-1v/step vision": ["step-1v-8k", "step-1v-32k", "step-1o-turbo-vision"],
    "doubao vision/vl": ["doubao-1.5-vision-pro", "doubao-1.5-vision-lite",
                         "doubao-1.5-vl"],
    "hunyuan vision": ["hunyuan-vision", "hunyuan-turbo-vision"],
    "minimax vl": ["MiniMax-VL-01", "minimax-vl-01"],
    "kimi vision": ["kimi-vision-preview"],
    "moonshot vision": ["moonshot-v1-8k-vision-preview"],
    "ernie vl": ["ernie-4.5-vl", "ernie-4.5-vl-flash"],
    "xcomposer": ["internlm-xcomposer2-4khd", "xcomposer2-7b"],
    "minigpt": ["minigpt-4", "minigpt-v2"],
    "grok vision": ["grok-2-vision", "grok-2-vision-1212"],
}

_ALL_POSITIVES = [n for names in POSITIVES.values() for n in names]

# ---------------------------------------------------------------------------
# 样本表:纯文本家族不误伤专项表(组 → 必须判 False 的名字)
# ---------------------------------------------------------------------------

TEXT_ONLY_GROUPS: dict[str, list[str]] = {
    "llama 纯文本": ["llama3", "llama-3.1-8b-instruct", "llama-2-7b-chat",
                    "codellama-34b", "llama-3.1-8b-instruct:latest"],
    "gemma 纯文本": ["gemma-7b", "gemma-2-9b-it", "gemma-2b-it", "gemma-7b-v2"],
    "gpt 纯文本": ["gpt-3.5-turbo", "gpt-3.5-turbo-16k", "gpt-4-32k"],
    "文本嵌入/表示模型": ["text-embedding-ada-002", "text-embedding-3-small",
                        "bge-large-en-v1.5", "bert-base-uncased", "e5-large-v2",
                        "t5-large"],
    "通义纯文本": ["qwen-14b-chat", "qwen2.5-72b-instruct",
                 "qwen2.5-coder-32b-instruct"],
    "glm 纯文本": ["glm-4-9b-chat", "glm-3-turbo-preview", "glm-4.5-air",
                 "glm-4-plus", "glm-4-turbo", "chatglm3-6b-32k"],
    "deepseek 纯文本": ["deepseek-r1", "deepseek-chat", "deepseek-coder-v2"],
    "中文家族纯文本": ["doubao-pro-32k", "doubao-lite-4k", "hunyuan-pro",
                    "step-1-8k", "step-2-16k", "ernie-4.5", "moonshot-v1-8k",
                    "abab6.5s-chat"],
    "开源/其他文本": ["internlm2-7b", "minicpm3-4b", "phi-3-mini-4k-instruct",
                   "mistral-7b-instruct", "mpt-7b", "yi-6b-chat",
                   "wizardcoder-15b", "claude-2.1", "cogview3"],
    "未知(宁缺勿滥)": ["kimi-latest", "grok-beta", "vision-less-700b",
                     "unknown-model-xyz", "vision", "vl"],
}


# ---------------------------------------------------------------------------
# 模式表本身
# ---------------------------------------------------------------------------


def test_patterns_table_is_tuple_with_at_least_18():
    assert isinstance(VISION_PATTERNS, tuple)
    assert len(VISION_PATTERNS) >= 18


def test_patterns_all_compiled_and_case_insensitive():
    for pattern in VISION_PATTERNS:
        assert isinstance(pattern, re.Pattern)
        assert pattern.flags & re.IGNORECASE, pattern.pattern


def test_every_pattern_has_at_least_one_positive():
    """每条模式都必须在正例表中至少命中一个名字(逐条扫描,防僵尸模式)。"""
    for i, pattern in enumerate(VISION_PATTERNS):
        assert any(
            pattern.search(name) for name in _ALL_POSITIVES
        ), f"第 {i} 条模式 {pattern.pattern!r} 没有任何正例"


# ---------------------------------------------------------------------------
# 正例(家族逐名参数化)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "family,name",
    [(family, name) for family, names in POSITIVES.items() for name in names],
)
def test_positive_names(family, name):
    assert is_vision_model(name) is True, f"{family}: {name}"


# ---------------------------------------------------------------------------
# 负例:纯文本家族不误伤专项表
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "group,name",
    [(group, name) for group, names in TEXT_ONLY_GROUPS.items() for name in names],
)
def test_text_only_families_never_match(group, name):
    assert is_vision_model(name) is False, f"{group}: {name}"


def test_required_negative_examples():
    """契约点名负例:llama3 纯文本 / gemma-7b 文本 / gpt-3.5 / 文本嵌入。"""
    assert is_vision_model("llama3") is False
    assert is_vision_model("gemma-7b") is False
    assert is_vision_model("gpt-3.5-turbo") is False
    assert is_vision_model("text-embedding-ada-002") is False


# ---------------------------------------------------------------------------
# 边界:大小写 / 版本后缀 / 词中 v 陷阱
# ---------------------------------------------------------------------------


def test_case_insensitive():
    for name in ["LLAVA", "LlAvA:13B", "QWEN2.5VL", "QWEN-VL-MAX",
                 "GPT-4O-MINI", "Claude-Sonnet-4", "GEMINI-2.5-PRO",
                 "GEMMA-3-4B-VL", "GLM-4V", "MiniCPM-V", "Moondream2"]:
        assert is_vision_model(name) is True, name


def test_version_and_tag_suffixes():
    for name in ["llava:13b", "llava:latest", "qwen2.5vl:7b",
                 "qwen2.5-vl-7b-instruct", "gpt-4o-mini", "gpt-4o-2024-11-20",
                 "gpt-4.1-mini", "glm-4v-flash", "glm-4.5v",
                 "minicpm-v:8b", "deepseek-vl-chat:7b", "llama3.2-vision:90b"]:
        assert is_vision_model(name) is True, name


def test_org_path_prefixes():
    assert is_vision_model("Qwen/Qwen2.5-VL-72B-Instruct") is True
    assert is_vision_model("OpenGVLab/InternVL2_5-8B") is True
    assert is_vision_model("meta-llama/llava-1.5-7b-hf") is True
    assert is_vision_model("qwen/qwen2.5-72b-instruct:free") is False


def test_v_inside_words_no_false_positive():
    """词中带 v/vl 的纯文本名不误伤(preview/v2/v1 陷阱)。"""
    for name in ["glm-3-turbo-preview", "glm-4-plus-preview",
                 "doubao-pro-v2", "hunyuan-embedding-v1", "gemma-7b-v2",
                 "vision-less-700b", "preview-only-model"]:
        assert is_vision_model(name) is False, name


# ---------------------------------------------------------------------------
# is_vision_model:空 / None / 非字符串 / 空白 / 超长
# ---------------------------------------------------------------------------


def test_none_and_non_string_inputs():
    for bad in [None, 123, 3.14, b"llava", ["llava"], {"model": "llava"}, True]:
        assert is_vision_model(bad) is False, repr(bad)


def test_empty_and_whitespace_only():
    assert is_vision_model("") is False
    assert is_vision_model("   ") is False
    assert is_vision_model("\t\n ") is False


def test_surrounding_whitespace_stripped():
    assert is_vision_model("  llava  ") is True
    assert is_vision_model("\tqwen2.5vl:7b\n") is True


def test_overlong_name_rejected():
    assert is_vision_model("llava" + "x" * 195) is True  # 恰 200 字符,放行
    assert is_vision_model("llava" + "x" * 196) is False  # 201 字符,拒


def test_return_type_is_bool():
    assert isinstance(is_vision_model("llava"), bool)
    assert isinstance(is_vision_model("llama3"), bool)
    assert isinstance(is_vision_model(None), bool)


# ---------------------------------------------------------------------------
# filter_vision:去重保序 / 空 / None / 非字符串项
# ---------------------------------------------------------------------------


def test_filter_keeps_first_seen_order():
    src = ["llava", "llama3", "qwen2.5vl:7b", "gpt-3.5-turbo", "gpt-4o-mini"]
    assert filter_vision(src) == ["llava", "qwen2.5vl:7b", "gpt-4o-mini"]


def test_filter_dedup_exact_duplicates():
    assert filter_vision(["llava", "llava", "llava:13b"]) == ["llava", "llava:13b"]


def test_filter_dedup_case_and_whitespace_insensitive():
    assert filter_vision(["LLaVA", "llava", "LLAVA"]) == ["LLaVA"]
    assert filter_vision(["qwen2.5vl:7b", "Qwen2.5VL:7B"]) == ["qwen2.5vl:7b"]
    assert filter_vision([" llava ", "llava"]) == [" llava "]  # 保留首次原始写法


def test_filter_empty_and_none():
    assert filter_vision([]) == []
    assert filter_vision(None) == []


def test_filter_all_text_returns_empty():
    assert filter_vision(["llama3", "gemma-7b", "text-embedding-ada-002"]) == []


def test_filter_skips_non_string_entries():
    src = [None, "llava", 42, "gpt-3.5-turbo", "moondream2", b"qwen2.5vl"]
    assert filter_vision(src) == ["llava", "moondream2"]


def test_filter_does_not_mutate_input():
    src = ["llava", "llava", "llama3"]
    snapshot = list(src)
    filter_vision(src)
    assert src == snapshot
