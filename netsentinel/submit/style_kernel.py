"""举报文本风格内核(A135 · V7):文风四维打分 + 不动事实的轻量改写。

契约(CONTRACTS-V7 §2 A135):
- ``style_score(text) -> dict``:举报描述的**文风四维打分**,四键独立 0/1,
  另附 ``"total"``(四键之和,0~4):
  1. ``len_ok``:30 ≤ 字符数 ≤ 240——下限保证信息量,上限与 A52
     ``describer_critic.MAX_DRAFT_CHARS`` / A36 ``llm_describer`` 一致;
  2. ``has_facts``:数值密度 > 0 且至少 1 个数字——只看"有没有数字",
     **不核对数字真伪**(真伪核对是 A52 的职责,见下"分工");
  3. ``no_hype``:未命中夸张词表 :data:`HYPE_WORDS`(前 7 词与 A52 的
     HYPERBOLE_WORDS 对齐,另补文风专用词"铺天盖地/触目惊心"——它们不在
     critic 词表内,由本内核管辖);
  4. ``formality``:命中法言法语模板 ≥ 1(:data:`FORMALITY_MARKERS`;
     契约指定"经核实/涉嫌/含有/违反",追加"人工核实"——项目规定的结尾句
     "以上情况本人已人工核实。"本身即法言法语)。口语化文本模板皆不中,
     该维度即为契约括号中"口语词"要点的反面承载。
- ``polish(text) -> str``:按规则**轻改**,三步固定顺序:
  ① 去夸张词——按 :data:`NEUTRAL_MAP` 映射为中性词("大量"→"多张"、
     "极其"→"明显"、"遍布"→"多处"……),单遍正则替换,替换产物不会被
     二次扫描(映射值均不含夸张词,模块常量一致性由测试锁定);
  ② 超 240 截断至句号——先预留结尾句(12 字)与至多 1 个分隔句号的额度
     再截,保证补齐结尾后总长仍 ≤ 240;截断位置回退到预算内最后一个句号
     (含该句号),预算内无句号则硬截;
  ③ 结尾确保"以上情况本人已人工核实。"——已存在则不动(幂等快路径),
     缺失则补齐(正文不以句读结尾时先补一个句号作分隔);改写前仅去掉
     文末空白(排版需要,不改内容)。
- ``kernel_selfcheck()``:A138 基准总控离线自检(确定性、零墙钟)。

与 describer_critic(A52)分工——**语义互补不重叠**:
- critic 查**事实**:草稿数值是否有事实清单依据(防幻觉防编造),返回问题
  清单供人工定稿;
- style 管**文风**:长度/数值存在性/夸张词/法言法语四维打分,并做轻改。
  本内核**不做任何事实核对,也不增删任何事实性内容**——数值、站点、行为
  描述一概不动;截断仅发生在超 240 字时且只去尾部整句。需要核对数值真伪
  请叠加使用 ``describer_critic.critique_description``:数值错 critic 抓、
  文风差 style 抓,两者缺一不可。

可观测性:每次 ``style_score`` 记 ``telemetry.inc("style_kernel.score")``
(:data:`SCORE_METRIC`;``polish`` 按契约不打点)。

工程约束:仅标准库;纯函数、确定性(同输入同输出);零网络零 IO;输入
容错(None → 空串、非 str → ``str()`` 规整,绝不向调用方抛出)。
"""
from __future__ import annotations

import re
from typing import Any

from netsentinel import telemetry

__all__ = [
    "CLOSING_STATEMENT",
    "FORMALITY_MARKERS",
    "HYPE_WORDS",
    "MAX_CHARS",
    "MIN_CHARS",
    "NEUTRAL_MAP",
    "SCORE_METRIC",
    "kernel_selfcheck",
    "polish",
    "style_score",
]

#: 遥测指标名:style_score 调用计数(每次 +1)
SCORE_METRIC = "style_kernel.score"

#: 文风合格长度区间(字符数,含端点;上限与 A52/A36 的 240 字一致)
MIN_CHARS = 30
MAX_CHARS = 240

#: 项目规定的举报描述结尾句(与 A36 llm_describer._HUMAN_CONFIRM_CLAIM 同文)
CLOSING_STATEMENT = "以上情况本人已人工核实。"

#: 截断预算:正文 + 至多 1 个分隔句号 + 结尾句(12 字)后总长恰 ≤ MAX_CHARS
_BODY_BUDGET = MAX_CHARS - len(CLOSING_STATEMENT) - 1  # 227

#: 夸张词表(模块常量):前 7 词与 A52 HYPERBOLE_WORDS 对齐,
#: 后 2 词为本内核管辖的文风专用夸张词(critic 词表之外)
HYPE_WORDS: tuple[str, ...] = (
    "大量",
    "极其",
    "遍布",
    "全部",
    "所有",
    "泛滥成灾",
    "数不胜数",
    "铺天盖地",
    "触目惊心",
)

#: 夸张词 → 中性词映射表(模块常量):polish ① 的替换依据,
#: 覆盖 HYPE_WORDS 全表;值均不含任何夸张词(单遍替换即终结)
NEUTRAL_MAP: dict[str, str] = {
    "大量": "多张",
    "极其": "明显",
    "遍布": "多处",
    "全部": "相关",
    "所有": "相关",
    "泛滥成灾": "较多",
    "数不胜数": "较多",
    "铺天盖地": "多处",
    "触目惊心": "明显",
}

#: 法言法语模板(模块常量):前 4 个为契约指定,追加"人工核实"
#: (规定结尾句本身即法言法语,使 polish 产物 formality 恒为 1)
FORMALITY_MARKERS: tuple[str, ...] = ("经核实", "涉嫌", "含有", "违反", "人工核实")

#: 视为句读的收尾字符(正文已以其收尾时,结尾句前不再补句号)
_SENTENCE_END = ("。", "!", "?", ";", "!", "?", "…")

#: 夸张词单遍替换正则(长词在前,防短词截长词;一遍扫完不回扫替换产物)
_HYPE_RE = re.compile("|".join(re.escape(w) for w in sorted(HYPE_WORDS, key=len, reverse=True)))

#: 数字字符提取正则(数值密度 = 数字字符数 / 文本字符数)
_DIGIT_RE = re.compile(r"\d")


def _coerce_text(text: Any) -> str:
    """输入容错:None → 空串,非 str → ``str()`` 规整(绝不抛出)。"""
    if isinstance(text, str):
        return text
    return "" if text is None else str(text)


def _replace_hype(match: re.Match[str]) -> str:
    """正则替换回调:命中词查 :data:`NEUTRAL_MAP` 取中性词。"""
    return NEUTRAL_MAP[match.group(0)]


def _truncate_to_sentence(text: str, limit: int) -> str:
    """截到 ``limit`` 内最后一个句号(含该句号);无句号则硬截到 ``limit``。"""
    window = text[:limit]
    pos = window.rfind("。")
    if pos >= 0:
        return window[: pos + 1]
    return window


def style_score(text: str) -> dict[str, int]:
    """举报描述文风四维打分(各键独立 0/1,另附四键之和 ``total``,0~4)。

    规则(详见模块 docstring):
    - ``len_ok``:30 ≤ ``len(text)`` ≤ 240;
    - ``has_facts``:数值密度(数字字符数 / 字符数)> 0 且至少 1 个数字
      ——只判存在性,**不核对真伪**(真伪归 describer_critic);
    - ``no_hype``:未命中 :data:`HYPE_WORDS` 任意词;
    - ``formality``:命中 :data:`FORMALITY_MARKERS` ≥ 1。

    确定性纯函数;每次调用记 ``telemetry.inc("style_kernel.score")``;
    输入容错(None → 空串、非 str → ``str()``),不向调用方抛出。
    """
    safe = _coerce_text(text)
    digits = _DIGIT_RE.findall(safe)
    density = (len(digits) / len(safe)) if safe else 0.0
    score = {
        "len_ok": int(MIN_CHARS <= len(safe) <= MAX_CHARS),
        "has_facts": int(len(digits) >= 1 and density > 0.0),
        "no_hype": int(_HYPE_RE.search(safe) is None),
        "formality": int(any(marker in safe for marker in FORMALITY_MARKERS)),
    }
    score["total"] = sum(score[key] for key in ("len_ok", "has_facts", "no_hype", "formality"))
    telemetry.inc(SCORE_METRIC)
    return score


def polish(text: str) -> str:
    """按规则轻量改写举报描述,使其文风合规(去夸张 / 截断 / 补结尾句)。

    三步固定顺序:
    ① 去夸张词:按 :data:`NEUTRAL_MAP` 单遍替换为中性词;
    ② 超 240 截断至句号:预算 = 240 − 结尾句 12 字 − 分隔句号 1 字,
       截到预算内最后一个句号(无句号则硬截);
    ③ 结尾确保 :data:`CLOSING_STATEMENT`:已存在且总长 ≤ 240 直接返回
       (幂等快路径),缺失则补齐(正文无句读收尾时先补一个句号)。

    **不做事实增删**:不核对、不新增、不改写任何事实性内容——数值、站点、
    行为描述一概不动,截断只在超 240 字时发生且只去尾部整句;数值真伪核对
    请叠加 :func:`netsentinel.submit.describer_critic.critique_description`。

    输入容错(None → 空串);空白文本原样返回(无可改写内容,不凭空替人
    "核实");确定性:``polish(polish(x)) == polish(x)``。
    """
    safe = _coerce_text(text)
    if not safe.strip():
        return safe

    # ① 去夸张词(单遍正则;文末空白一并去掉,仅为排版)
    out = _HYPE_RE.sub(_replace_hype, safe).rstrip()

    # 幂等快路径:结尾句已就位且总长达标 → 只可能发生了 ①,直接返回
    if out.endswith(CLOSING_STATEMENT) and len(out) <= MAX_CHARS:
        return out

    # ② 超 240 截断至句号(先预留结尾句与分隔句号的额度)
    if len(out) > _BODY_BUDGET:
        out = _truncate_to_sentence(out, _BODY_BUDGET)

    # ③ 结尾确保"以上情况本人已人工核实。"
    if not out.endswith(CLOSING_STATEMENT):
        separator = "" if out.endswith(_SENTENCE_END) else "。"
        out = out + separator + CLOSING_STATEMENT
    return out


def kernel_selfcheck() -> dict[str, Any]:
    """V7 内核自检(A138 kernel_bench 统一调用;确定性、零墙钟)。

    一段含多处夸张词、缺结尾句的草稿经 :func:`polish` 单遍改写后:
    夸张词命中数归零、总长 ≤ 240、结尾句就位——以**命中计数对比**
    (0 vs 改写前命中数)证明代差,非计时断言(红线 31)。
    """
    draft = (
        "该站含有大量淫秽图片,弹窗广告遍布页面各处,情节极其恶劣,"
        "全部页面均涉嫌传播淫秽物品,涉案图片数不胜数,铺天盖地的"
        "色情信息触目惊心,严重违反网络安全管理规定。"
    )
    before = len(_HYPE_RE.findall(draft))
    polished = polish(draft)
    after = len(_HYPE_RE.findall(polished))
    return {
        "name": "style_kernel",
        "metric": "夸张段落 polish 后的夸张词命中数",
        "value": after,
        "baseline": before,
        "polished_len": len(polished),
        "ends_with_closing": polished.endswith(CLOSING_STATEMENT),
    }
