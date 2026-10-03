"""A135 netsentinel.submit.style_kernel(风格内核)测试。

纯离线、确定性、零网络零 IO。覆盖(CONTRACTS-V7 §2 A135 + 任务书):

- style_score 四键正反例:len_ok 边界(29/30/240/241)、has_facts(有/无数字、
  空文本)、no_hype(词表逐词命中 + 干净反例)、formality(模板逐词命中 +
  口语化反例);四键独立性;契约返回形状(四键 + total=四键之和);
- 输入容错:None / 非 str 不抛出;空文本全零;
- 遥测:每次打分 ``style_kernel.score`` 恰 +1,polish 不打点;
- polish 三规则:去夸张词(含任务书指定三映射"大量→多张/极其→明显/
  遍布→多处"的整串精确断言 + 全词表覆盖)、超 240 截断至句号边界、
  结尾补齐(分隔句号规则);三规则组合单遍生效;不做事实增删(数值/URL
  原样保留);已规范文本幂等;任意文本 polish 幂等(polish∘polish ==
  polish);空文本/纯空白原样返回;产物不变式(无夸张词、formality=1、
  总长 ≤ 240、结尾句就位);
- 与 describer_critic 并存互补:①数值对但夸张(铺天盖地,critic 词表外)
  → critic 通过、style 抓文风;②数值错但文风规范 → critic 抓"数值 33 无
  事实依据"、style 四维全过——critic 查事实、style 管文风,缺一不可;
- test_v7_bench_batch_scoring_operation_count:批量 100 段打分以遥测
  操作计数断言(恰 100 次,零墙钟,红线 31);
- kernel_selfcheck(A138 约定字段 + 确定性);
- 模块常量一致性:映射表全覆盖夸张词表、中性词不含夸张词、任务书三映射
  精确锁定。
"""
from __future__ import annotations

import pytest

from netsentinel import telemetry
from netsentinel.submit.style_kernel import (
    CLOSING_STATEMENT,
    FORMALITY_MARKERS,
    HYPE_WORDS,
    MAX_CHARS,
    MIN_CHARS,
    NEUTRAL_MAP,
    SCORE_METRIC,
    kernel_selfcheck,
    polish,
    style_score,
)

#: 四维键名(契约 §2 A135)
_KEYS = ("len_ok", "has_facts", "no_hype", "formality")

#: 文风全合规样例(30~240 字、含数字、无夸张词、含法言法语、结尾句就位)
_CLEAN = (
    "举报站点 https://bad.example.com/zone 涉嫌传播淫秽物品,抽样页面 3 个,"
    "达标图片 5 张,页面文本含有违规关键词。以上情况本人已人工核实。"
)


# ---------------------------------------------------------------------------
# style_score:契约形状 + 四键正反例
# ---------------------------------------------------------------------------
def test_score_contract_shape_and_full_marks() -> None:
    """四键 + total(四键之和);全合规文本四键皆 1、total=4。"""
    score = style_score(_CLEAN)
    assert set(score) == {*_KEYS, "total"}
    assert all(score[key] == 1 for key in _KEYS)
    assert score["total"] == 4
    assert all(isinstance(score[key], int) for key in score)


@pytest.mark.parametrize(
    ("length", "expected"),
    [(MIN_CHARS - 1, 0), (MIN_CHARS, 1), (MAX_CHARS, 1), (MAX_CHARS + 1, 0)],
    ids=["29-short", "30-boundary", "240-boundary", "241-over"],
)
def test_score_len_ok_boundaries(length: int, expected: int) -> None:
    """长度区间 [30, 240] 含端点:29→0、30→1、240→1、241→0。"""
    assert style_score("字" * length)["len_ok"] == expected


def test_score_has_facts_digit_presence() -> None:
    """数值密度>0 且 ≥1 个数字:有数字→1;无数字→0;空文本→0。"""
    with_digits = "该站涉嫌传播淫秽物品,抽样页面 3 个。以上情况本人已人工核实。"
    without_digits = "该站涉嫌传播淫秽物品,页面含有违规关键词。以上情况本人已人工核实。"
    assert style_score(with_digits)["has_facts"] == 1
    assert style_score(without_digits)["has_facts"] == 0
    assert style_score("")["has_facts"] == 0


@pytest.mark.parametrize("word", HYPE_WORDS)
def test_score_no_hype_wordlist_hits(word: str) -> None:
    """夸张词表逐词命中 → no_hype=0;无夸张词 → 1。"""
    assert style_score(f"描述:该站存在{word}的问题,涉嫌传播违规内容。")["no_hype"] == 0
    assert style_score("描述:该站存在问题,涉嫌传播违规内容。")["no_hype"] == 1


@pytest.mark.parametrize("marker", FORMALITY_MARKERS)
def test_score_formality_markers(marker: str) -> None:
    """法言法语模板逐词命中(经核实/涉嫌/含有/违反/人工核实)→ formality=1。"""
    assert style_score(f"情况说明:{marker}。")["formality"] == 1


def test_score_formality_colloquial_negative() -> None:
    """口语化文本四模板皆不中 → formality=0(契约"口语词"要点的反面承载)。"""
    colloquial = "这个网站也太离谱了吧,看着真不舒服,都不想再打开了,赶紧看看吧"
    assert len(colloquial) >= MIN_CHARS  # 确保只考察 formality 维度
    assert style_score(colloquial)["formality"] == 0


def test_score_keys_independent() -> None:
    """四键独立:全零文本、仅缺数字、仅含夸张词,各自只扣对应键。"""
    all_zero = style_score("太离谱了,大量垃圾内容")
    assert all_zero == {"len_ok": 0, "has_facts": 0, "no_hype": 0, "formality": 0, "total": 0}

    only_facts_missing = style_score(
        "该站涉嫌传播淫秽物品,页面含有违规关键词。以上情况本人已人工核实。"
    )
    assert only_facts_missing == {"len_ok": 1, "has_facts": 0, "no_hype": 1, "formality": 1, "total": 3}

    only_hype = style_score(_CLEAN.replace("页面文本", "页面大量充斥"))
    assert only_hype["no_hype"] == 0
    assert [only_hype[key] for key in ("len_ok", "has_facts", "formality")] == [1, 1, 1]
    assert only_hype["total"] == 3


def test_score_empty_text() -> None:
    """空文本:len_ok/has_facts/formality 全 0;no_hype 空真命中(无夸张词)为 1。"""
    assert style_score("") == {"len_ok": 0, "has_facts": 0, "no_hype": 1, "formality": 0, "total": 1}


def test_score_defensive_coercion() -> None:
    """输入容错:None 视同空串;非 str 经 str() 规整,均不抛出。"""
    assert style_score(None) == style_score("")  # type: ignore[arg-type]
    assert style_score(123)["has_facts"] == 1     # type: ignore[arg-type]  # "123" 含数字


def test_score_telemetry_counter() -> None:
    """每次 style_score 记 telemetry.inc("style_kernel.score");polish 不打点。"""
    telemetry.reset()
    style_score(_CLEAN)
    style_score("短")
    style_score("")
    assert telemetry.snapshot()["counters"].get(SCORE_METRIC) == 3.0
    polish("该站含有大量违规图片。")
    polish("长" * 300)
    assert telemetry.snapshot()["counters"].get(SCORE_METRIC) == 3.0


# ---------------------------------------------------------------------------
# polish:三规则各自生效与组合
# ---------------------------------------------------------------------------
def test_polish_exact_mapping_for_specified_words() -> None:
    """任务书指定三映射的整串精确断言:"大量"→"多张"、"极其"→"明显"、"遍布"→"多处"。"""
    text = "该站页面遍布违规弹窗,含有大量违规图片,情节极其恶劣。"
    expected = "该站页面多处违规弹窗,含有多张违规图片,情节明显恶劣。" + CLOSING_STATEMENT
    assert polish(text) == expected
    for word in ("大量", "极其", "遍布"):
        assert word not in polish(text)


@pytest.mark.parametrize("word", HYPE_WORDS)
def test_polish_replaces_every_hype_word(word: str) -> None:
    """全词表覆盖:每个夸张词都被替换为其中性映射,且不再残留。"""
    out = polish(f"该站存在{word}的问题,涉嫌传播违规内容。")
    assert word not in out
    assert NEUTRAL_MAP[word] in out
    assert out.endswith(CLOSING_STATEMENT)


def test_polish_truncates_at_sentence_boundary() -> None:
    """超 240 截断至句号:300 字(每 10 字一句)→ 正文恰为 22 个整句 + 结尾句。"""
    unit = "123456789。"
    text = unit * 30  # 300 字
    out = polish(text)
    expected = unit * 22 + CLOSING_STATEMENT  # 220 + 12 = 232
    assert out == expected
    assert len(out) == 232 <= MAX_CHARS
    assert out.removesuffix(CLOSING_STATEMENT).endswith("。")  # 句号边界,非半句硬截
    assert text.startswith(out[: -len(CLOSING_STATEMENT)])     # 只去尾部整句


def test_polish_hard_cut_without_period() -> None:
    """预算内无句号:硬截到 227 字,补分隔句号与结尾句后总长恰 240。"""
    out = polish("长" * 300)
    assert out == "长" * 227 + "。" + CLOSING_STATEMENT
    assert len(out) == 240


def test_polish_appends_closing_separator_rules() -> None:
    """结尾补齐:句号收尾直接拼接;无句读先补句号;叹号收尾不再补。"""
    ends_period = "该站涉嫌传播淫秽物品,抽样页面 3 个,达标图片 5 张。"
    assert polish(ends_period) == ends_period + CLOSING_STATEMENT
    ends_bare = "该站涉嫌传播淫秽物品,抽样页面 3 个,达标图片 5 张"
    assert polish(ends_bare) == ends_bare + "。" + CLOSING_STATEMENT
    ends_bang = "该站情节恶劣!"
    assert polish(ends_bang) == ends_bang + CLOSING_STATEMENT


def test_polish_preserves_facts() -> None:
    """不做事实增删:数值(3/5/0.97)与整段 URL 原样保留,仅补结尾句。"""
    text = (
        "举报站点 https://x123.example.com/p7.html 涉嫌传播淫秽物品,"
        "抽样页面 3 个,达标图片 5 张,聚合最高分 0.97,页面含有违规关键词。"
    )
    out = polish(text)
    assert out == text + CLOSING_STATEMENT  # 无夸张、未超限 → 事实性内容一字未动
    assert "https://x123.example.com/p7.html" in out
    for token in ("3", "5", "0.97"):
        assert token in out


def test_polish_idempotent_on_clean_text() -> None:
    """已规范文本(结尾句就位、≤240、无夸张词)polish 原样返回。"""
    assert polish(_CLEAN) == _CLEAN


@pytest.mark.parametrize(
    "text",
    [
        "该站含有大量违规图片,情节极其恶劣。",  # 夸张词
        "123456789。" * 30,  # 超长有句号
        "长" * 300,  # 超长无句号
        "该站情节恶劣!",  # 缺结尾句、叹号收尾
        "大量" * 120,  # 夸张且去夸张后仍超长
        CLOSING_STATEMENT,  # 仅结尾句
    ],
    ids=["hype", "overlong", "overlong-no-period", "bang", "hype-overlong", "closing-only"],
)
def test_polish_idempotent_repeated(text: str) -> None:
    """任意文本:polish(polish(x)) == polish(x)(改写一次即达不动点)。"""
    once = polish(text)
    assert polish(once) == once


def test_polish_empty_and_blank_unchanged() -> None:
    """空文本/纯空白原样返回:无可改写内容,不凭空替人"人工核实"。"""
    assert polish("") == ""
    blank = "   \t\n "
    assert polish(blank) == blank


def test_polish_combined_rules() -> None:
    """三规则组合单遍生效:夸张 + 超长 + 缺结尾 → 一次改写全部合规。"""
    text = "该站含有大量违规图片,情节极其恶劣。" + "补充细节句。" * 40
    assert len(text) > MAX_CHARS
    out = polish(text)
    assert all(word not in out for word in HYPE_WORDS)  # ① 去夸张
    assert out.startswith("该站含有多张违规图片,情节明显恶劣。")
    assert len(out) <= MAX_CHARS                          # ② 截断
    assert out.endswith(CLOSING_STATEMENT)                # ③ 结尾补齐


@pytest.mark.parametrize(
    "text",
    [
        _CLEAN,
        "该站含有大量违规图片,极其恶劣。",
        "123456789。" * 30,
        "长" * 300,
        "内容",
        CLOSING_STATEMENT,
        "全部图片泛滥成灾,数不胜数!",
    ],
    ids=["clean", "hype", "overlong", "hard-cut", "tiny", "closing", "multi-hype"],
)
def test_polish_output_invariants(text: str) -> None:
    """产物不变式:非空输入 polish 后无夸张词、formality=1、≤240、结尾句就位。"""
    out = polish(text)
    score = style_score(out)
    assert score["no_hype"] == 1
    assert score["formality"] == 1
    assert len(out) <= MAX_CHARS
    assert out.endswith(CLOSING_STATEMENT)


# ---------------------------------------------------------------------------
# 与 describer_critic(A52)并存:critic 查事实、style 管文风,互补不重叠
# ---------------------------------------------------------------------------
class _OfflineClient:
    """恒抛异常的假客户端:强制 critic 走离线确定性规则版。"""

    def chat_json(self, messages, *, image_paths=None):  # pragma: no cover - 恒抛
        raise RuntimeError("离线")


_FACTS = {
    "site_url": "https://bad.example.com/zone",
    "agg": 0.966,
    "nsw_count": 5,
    "pages": 3,
    "url_risk": "域名使用可疑顶级域",
    "text_risk": "命中色情关键词 6 处",
}

#: 数值对(3/5 均有依据)但文风夸张("铺天盖地"在 critic 词表之外)
_HYPE_BUT_GROUNDED = (
    "举报站点 https://bad.example.com/zone 涉嫌传播淫秽物品,抽样页面 3 个,"
    "达标图片 5 张,页面充斥铺天盖地的违规弹窗。以上情况本人已人工核实。"
)

#: 文风规范(四维全过)但数值编造(33 无依据)
_STYLED_BUT_FABRICATED = (
    "举报站点 https://bad.example.com/zone 涉嫌传播淫秽物品,抽样页面 33 个,"
    "达标图片 5 张,页面含有违规关键词。以上情况本人已人工核实。"
)


def test_complementary_with_describer_critic() -> None:
    """critic 抓数值错、style 抓文风差:两个方向各构造一例,单用任一都会漏。"""
    from netsentinel.contracts import Config
    from netsentinel.submit.describer_critic import critique_description, passes

    # ① 数值对但夸张:critic 规则版全过(数值有依据、无 critic 词表问题),
    #    style 抓住 critic 词表之外的夸张词 → 文风不合规
    critic_ok = critique_description(_HYPE_BUT_GROUNDED, _FACTS, Config(), client=_OfflineClient())
    assert critic_ok == [] and passes(critic_ok) is True
    style_bad = style_score(_HYPE_BUT_GROUNDED)
    assert style_bad["no_hype"] == 0
    assert [style_bad[key] for key in ("len_ok", "has_facts", "formality")] == [1, 1, 1]

    # ② 数值错但文风规范:critic 抓"数值 33 无事实依据",
    #    style 四维全过(has_facts 只看存在性,数值真伪不在文风管辖)
    critic_bad = critique_description(_STYLED_BUT_FABRICATED, _FACTS, Config(), client=_OfflineClient())
    assert critic_bad == ["数值 33 无事实依据"]
    assert style_score(_STYLED_BUT_FABRICATED)["total"] == 4


# ---------------------------------------------------------------------------
# bench(红线 31:操作计数断言,零墙钟)+ kernel_selfcheck
# ---------------------------------------------------------------------------
def _bench_batch() -> list[str]:
    """确定性构造 100 段:50 段文风全合规(4 分)+ 50 段仅含夸张词(3 分)。"""
    clean = [
        f"该站涉嫌传播淫秽物品,抽样页面 {i} 个,达标图片 {i + 2} 张,"
        f"页面文本含有违规关键词。以上情况本人已人工核实。"
        for i in range(1, 51)
    ]
    flawed = [text.replace("达标图片", "大量达标图片") for text in clean]
    return clean + flawed


def test_v7_bench_batch_scoring_operation_count() -> None:
    """bench:批量 100 段打分——遥测计数恰 +100(操作计数,零墙钟,红线 31);
    总分精确 = 50×4 + 50×3;重跑逐段一致(可复现);polish 批量不改打点数。"""
    batch = _bench_batch()
    assert len(batch) == 100

    telemetry.reset()
    first = [style_score(text) for text in batch]
    assert telemetry.snapshot()["counters"].get(SCORE_METRIC) == 100.0
    assert sum(score["total"] for score in first) == 50 * 4 + 50 * 3

    second = [style_score(text) for text in batch]
    assert second == first  # 确定性:同输入同输出
    assert telemetry.snapshot()["counters"].get(SCORE_METRIC) == 200.0

    polished = [polish(text) for text in batch]
    assert telemetry.snapshot()["counters"].get(SCORE_METRIC) == 200.0  # polish 不打点
    assert all(len(out) <= MAX_CHARS and out.endswith(CLOSING_STATEMENT) for out in polished)


def test_kernel_selfcheck_report() -> None:
    """kernel_selfcheck(A138 约定):字段齐全、polish 后夸张命中 0 < 改写前 7,
    产物 ≤240 且结尾句就位;两次运行完全一致(确定性)。"""
    report = kernel_selfcheck()
    assert report["name"] == "style_kernel"
    assert report["metric"]
    assert report["value"] == 0
    assert report["baseline"] == 7  # 大量/遍布/极其/全部/数不胜数/铺天盖地/触目惊心
    assert report["value"] < report["baseline"]
    assert report["polished_len"] <= MAX_CHARS
    assert report["ends_with_closing"] is True
    assert kernel_selfcheck() == report


# ---------------------------------------------------------------------------
# 模块常量一致性
# ---------------------------------------------------------------------------
def test_module_constants_consistency() -> None:
    """映射表与词表互洽:全覆盖、无重复、中性词不含夸张词;任务书三映射锁定。"""
    assert set(NEUTRAL_MAP) == set(HYPE_WORDS)
    assert len(HYPE_WORDS) == len(set(HYPE_WORDS))
    for neutral in NEUTRAL_MAP.values():
        assert all(word not in neutral for word in HYPE_WORDS)  # 单遍替换即终结
    assert NEUTRAL_MAP["大量"] == "多张"
    assert NEUTRAL_MAP["极其"] == "明显"
    assert NEUTRAL_MAP["遍布"] == "多处"
    assert ("经核实", "涉嫌", "含有", "违反") == FORMALITY_MARKERS[:4]  # 契约指定前四
