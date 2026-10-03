"""A150 · 模型名视觉能力判定(纯名称形态匹配,仅标准库,离线可用)。

V8 自动接管/连接向导/手动切换的公共判定件:给定一个模型名,判断它**是否
看起来是视觉(VLM)模型**。消费者:

- A143 local_probe:本地 ``/v1/models`` 扫描结果按本表过滤(惰性导入,缺席
  时全返回并标 ``unfiltered``);
- A161 models_page:复核台"模型"页的"视觉"判定列;
- A160 MODEL_SWITCH 手册的"能力判定表"(本表即权威来源)。

判定纪律(对应 CONTRACTS-V8 §3 A150 行,延续红线 18 的提示级定位):

- **只看名字形态**:命中 ≠ 该部署真的开了图像输入,更不构成可用性承诺;
  命名随平台迭代频繁变动,一切以官方文档为准;
- **宁缺勿滥**:仅收录契约列出的家族;名字不带视觉标记的一律 False
  (如 ``kimi-latest``/``grok-beta``/``gpt-4-turbo``,哪怕官方说明支持图像);
- **防误伤**:``vl``/``v`` 这类超短 token 一律带边界(负向断言 ``(?![a-z])``,
  在 IGNORECASE 下同时挡大小写字母),避免 ``glm-3-turbo-preview``、
  ``doubao-pro-v2``、``gemma-7b`` 这类纯文本名被"名字里有个 v"误收;
  gemma 家族另要求 ``-vl``/``-vision`` 以连字符显式出现;
- 纯函数:不联网、不落盘、无第三方依赖;``None``/非字符串/空白/超长一律
  False,绝不抛异常。

用法示例::

    from netsentinel.vision.capability import filter_vision, is_vision_model

    is_vision_model("llava:13b")        # -> True
    is_vision_model("llama3")           # -> False(纯文本)
    is_vision_model(None)               # -> False
    filter_vision(["llava", "llama3", "qwen2.5vl:7b"])
    # -> ["llava", "qwen2.5vl:7b"](去重保序)
"""
from __future__ import annotations

import re

__all__ = [
    "VISION_PATTERNS",
    "is_vision_model",
    "filter_vision",
]

#: 模型名最大长度(超长输入直接 False,防滥用正则;与 model_catalog 同额)
_MAX_MODEL_LEN = 200

# ---------------------------------------------------------------------------
# 模式表(≥18 条,全部 IGNORECASE;顺序即家族登记顺序,仅作文档用,
# 匹配语义与顺序无关——任一命中即判视觉)
# ---------------------------------------------------------------------------
#
# 说明:每条后面的注释给出代表性正例;``(?![a-z])`` 为边界负向断言,
# IGNORECASE 下同时挡大写字母(见模块 docstring"防误伤")。

_PATTERN_SOURCES: tuple[str, ...] = (
    # llava / bakllava(bakllava 内含 llava 子串)/ llava-1.5-7b-hf / llava:13b
    r"llava",
    # llama3.2-vision / Llama-3.2-11B-Vision-Instruct(纯文本 llama3 不含 vision|vl)
    r"llama[\w.-]*(?:vision|vl)(?![a-z])",
    # qwen-vl-max / qwen2.5vl:7b / Qwen/Qwen2.5-VL-72B-Instruct
    r"qwen[\w.-]*(?:vl|vision)(?![a-z])",
    # minicpm-v / MiniCPM-V-2_6 / minicpm-v:8b(文本版 minicpm3-4b 无 "-v")
    r"minicpm[\w.-]*-v(?![a-z])",
    # moondream / moondream2
    r"moondream",
    # gemma-3-4b-vl:要求连字符显式接 vl|vision,纯文本 gemma-7b 不误伤
    r"gemma[\w.-]*-(?:vl|vision)(?![a-z])",
    # cogvlm / cogvlm2-19b(图像生成的 cogview 不收,生成≠视觉判定)
    r"cogvlm",
    # internvl / OpenGVLab/InternVL2_5-8B(文本 internlm 不含 internvl)
    r"internvl",
    # phi-3-vision / phi-3.5-vision-instruct / phi-4-multimodal-instruct
    r"phi[\w.-]*(?:vision|multimodal)(?![a-z])",
    # deepseek-vl / deepseek-vl2 / deepseek-vl-chat:7b(r1/chat 等纯文本不收)
    r"deepseek-vl",
    # idefics-9b-instruct / idefics2-8b
    r"idefics",
    # gpt-4o / gpt-4o-mini;gpt-4-vision-preview;gpt-4.1 / gpt-4.1-mini
    # (gpt-3.5 与无 vision 字样的 gpt-4-32k 不收——宁缺勿滥)
    r"gpt-4o(?![a-z])|gpt-4[\w.-]*vision(?![a-z])|gpt-4\.1(?![a-z0-9])",
    # claude-3 系 / claude-sonnet-4 / claude-3-5-haiku / claude-opus-4-5
    # (claude-2.x 为纯文本,不收)
    r"claude-(?:3|sonnet|haiku|opus)",
    # gemini 全系多模态:gemini-1.5-pro / gemini-2.0-flash
    r"gemini",
    # glm-4v / glm-4.5v / glm4v:9b / glm-4vl——"glm"+数字后显式接 v,
    # glm-3-turbo-preview 里的 v(preview 词中)不误伤
    r"glm-?\d[\d.]*v(?:ision|l)?(?![a-z])",
    # glm…vl 形态兜底(如 glm-4.6vl / 非数字中缀写法)
    r"glm[\w.-]*vl(?![a-z])",
    # step-1v-8k / step-1v-32k / step-1o-turbo-vision(step-1-8k 纯文本不收)
    r"step-\d[\w.-]*v(?:ision|l)?(?![a-z])",
    # doubao-1.5-vision-pro / doubao-1.5-vision-lite / doubao-1.5-vl
    # (doubao-pro-32k / doubao-lite-4k / doubao-pro-v2 纯文本不收)
    r"doubao[\w.-]*(?:vision|vl)(?![a-z])",
    # hunyuan-vision / hunyuan-turbo-vision(hunyuan-pro 纯文本不收)
    r"hunyuan[\w.-]*vision(?![a-z])",
    # MiniMax-VL-01 / minimax-vl-01(abab 纯文本系不收)
    r"minimax[\w.-]*(?:vl|vision)(?![a-z])",
    # kimi 系显式 vision 变体(kimi-latest 名字无标记→未知 False,宁缺勿滥)
    r"kimi[\w.-]*vision(?![a-z])",
    # moonshot-v1-8k-vision-preview(moonshot-v1-8k 纯文本不收)
    r"moonshot[\w.-]*vision(?![a-z])",
    # ernie-4.5-vl / ernie-4.5-vl-flash(ernie-4.5 纯文本不收)
    r"ernie[\w.-]*(?:vl|vision)(?![a-z])",
    # internlm-xcomposer2-4khd(文本 internlm2 不收)
    r"xcomposer",
    # minigpt-4 / minigpt-v2
    r"minigpt",
    # grok-2-vision / grok-2-vision-1212(grok-beta 未知不收)
    r"grok[\w.-]*vision(?![a-z])",
)

#: 视觉模型名模式表(契约要求 ≥18 条;导出供 A160 目录页/A143 复用)。
#: 全部已按 ``re.IGNORECASE`` 编译。
VISION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(src, re.IGNORECASE) for src in _PATTERN_SOURCES
)


# ---------------------------------------------------------------------------
# 单名判定
# ---------------------------------------------------------------------------


def is_vision_model(name: str) -> bool:
    """判断模型名是否为视觉(VLM)模型(纯名称形态判定,提示级)。

    - ``None`` / 非字符串 → False(绝不抛异常,供直接消化 API 返回值);
    - 空白串 / 长度超 :data:`_MAX_MODEL_LEN` → False;
    - 判定 = :data:`VISION_PATTERNS` 任一模式子串命中(大小写不敏感,
      前后空白先剥离);
    - **宁缺勿滥**:名字不带视觉标记的(如 ``kimi-latest``、``grok-beta``)
      一律 False;命中也不保证该部署真的可用/开了图像输入,以官方为准。

    用例::

        is_vision_model("llava:13b")     # True
        is_vision_model("gemma-7b")      # False(纯文本)
        is_vision_model("GLM-4V-Flash")  # True
        is_vision_model(None)            # False
    """
    if not isinstance(name, str):
        return False
    cleaned = name.strip()
    if not cleaned or len(cleaned) > _MAX_MODEL_LEN:
        return False
    return any(pattern.search(cleaned) for pattern in VISION_PATTERNS)


# ---------------------------------------------------------------------------
# 列表过滤
# ---------------------------------------------------------------------------


def filter_vision(models: list[str] | None) -> list[str]:
    """从模型名列表中筛出视觉模型,去重且保持首次出现顺序。

    - 逐项调 :func:`is_vision_model`:非字符串/None 项直接跳过(不抛异常);
    - 去重键为 ``strip().casefold()``(大小写与首尾空白不敏感),
      保留**首次出现**的原始写法——``["LLaVA", "llava"]`` 得 ``["LLaVA"]``;
    - ``None`` / 空列表 → ``[]``;输入列表不被修改(返回新列表)。

    供 A143 本地扫描结果过滤与 A161 模型页"视觉"列复用。
    """
    if not models:
        return []
    seen: set[str] = set()
    kept: list[str] = []
    for item in models:
        if not is_vision_model(item):
            continue
        key = item.strip().casefold()
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)
    return kept
