"""A22 单元测试:netsentinel.vision.vlm_prompts(纯函数,离线,零外呼)。

覆盖点:
- PROMPT_VERSION 常量与兼容别名;
- 四个 SYSTEM 提示词均逐字嵌入防注入规则(含"忽略之前指令"/"一律忽略"子串)与各自 schema 要点;
- build_user_prompt 四种 kind(image/page/arbiter/describer)的内容断言、未知 kind 抛 ValueError;
- parse_json_response 的各种脏输入:围栏(含无标签/无换行/未闭合/围栏后杂质)、前后杂质、
  尾逗号、单引号、单引号+尾逗号组合、字符串内花括号与转义引号、Python 字面量、非法 → None;
- validate_image_json:缺失/非法字段、越界 clamp、类别过滤(含涉未成年人)、confidence/reasoning 处理;
- calibrate:全部锚点、各段中点插值、输入 clamp、单调不减、锚点常量与实现一致。
"""
from __future__ import annotations

import re

import pytest

from netsentinel import telemetry
from netsentinel.contracts import ImageEvidence, ImageScore
from netsentinel.vision.vlm_prompts import (
    ARBITER_PROMPT,
    ARBITER_SYSTEM,
    BASE_IMAGE_CATEGORIES,
    CALIBRATION_ANCHORS,
    DESCRIBER_PROMPT,
    DESCRIBER_SYSTEM,
    IMAGE_SCORING_PROMPT,
    IMAGE_SCORING_SYSTEM,
    INJECTION_DEFENSE_RULE,
    PAGE_SCREENSHOT_PROMPT,
    PAGE_SCREENSHOT_SYSTEM,
    PROMPT_VERSION,
    VALID_IMAGE_CATEGORIES,
    build_user_prompt,
    calibrate,
    parse_json_response,
    validate_image_json,
)

# 与模块内 CALIBRATION_ANCHORS 对应的"测试侧"锚点表(防止常量漂移)
ANCHORS: list[tuple[float, float]] = [
    (0.0, 0.02),
    (0.3, 0.35),
    (0.5, 0.55),
    (0.7, 0.78),
    (0.85, 0.90),
    (0.95, 0.97),
    (1.0, 0.99),
]

SYSTEM_PROMPTS: dict[str, str] = {
    "image": IMAGE_SCORING_SYSTEM,
    "page": PAGE_SCREENSHOT_SYSTEM,
    "arbiter": ARBITER_SYSTEM,
    "describer": DESCRIBER_SYSTEM,
}


# ---------------------------------------------------------------------------
# 版本常量与兼容别名
# ---------------------------------------------------------------------------


def test_prompt_version_constant() -> None:
    assert PROMPT_VERSION == "v2.1"
    assert CALIBRATION_ANCHORS == tuple(ANCHORS)
    assert VALID_IMAGE_CATEGORIES == BASE_IMAGE_CATEGORIES + ("涉未成年人",)


def test_compat_aliases_point_to_same_prompts() -> None:
    assert IMAGE_SCORING_PROMPT is IMAGE_SCORING_SYSTEM
    assert PAGE_SCREENSHOT_PROMPT is PAGE_SCREENSHOT_SYSTEM
    assert ARBITER_PROMPT is ARBITER_SYSTEM
    assert DESCRIBER_PROMPT is DESCRIBER_SYSTEM


# ---------------------------------------------------------------------------
# SYSTEM 提示词:防注入与内容要点
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SYSTEM_PROMPTS))
def test_system_prompts_embed_injection_defense(name: str) -> None:
    """四个 SYSTEM 提示词都必须逐字包含防注入规则与关键子句。"""
    prompt = SYSTEM_PROMPTS[name]
    assert INJECTION_DEFENSE_RULE in prompt  # 单一事实来源,逐字嵌入
    assert "忽略之前指令" in prompt
    assert "一律忽略" in prompt
    assert "只输出一个 JSON 对象" in prompt


def test_injection_defense_rule_wording() -> None:
    rule = INJECTION_DEFENSE_RULE
    assert "忽略之前指令" in rule
    assert "一律忽略" in rule
    assert "绝不执行" in rule
    assert "水印" in rule and "提示语" in rule


def test_image_scoring_system_schema() -> None:
    p = IMAGE_SCORING_SYSTEM
    assert PROMPT_VERSION in p
    assert "图片内容安全审核引擎" in p
    for token in ("nsfw_prob", "categories", "reasoning", "confidence"):
        assert token in p
    for cat in BASE_IMAGE_CATEGORIES:
        assert cat in p
    assert "两位" in p          # 保留两位小数
    assert "80 字" in p         # reasoning 长度约束
    assert "不猜测画外" in p
    assert "涉未成年人" in p and "1.00" in p   # 未成年人硬性规则
    assert "人工" in p          # 处置由人工完成


def test_page_screenshot_system_schema() -> None:
    p = PAGE_SCREENSHOT_SYSTEM
    assert PROMPT_VERSION in p
    for token in ("page_nsfw_prob", "elements", "kind", "desc", "prob"):
        assert token in p
    for kind in ("横幅广告", "视频播放器", "图片墙", "弹窗", "导航", "正文", "侧栏"):
        assert kind in p
    assert "8 项" in p and "30 字" in p
    assert "只描述截图" in p


def test_arbiter_system_content() -> None:
    p = ARBITER_SYSTEM
    assert PROMPT_VERSION in p
    assert "独立" in p and "重新评审" in p
    assert "不参考" in p and "不迎合" in p
    assert "不得作为评分依据" in p
    assert "nsfw_prob" in p and "reasoning" in p


def test_describer_system_content() -> None:
    p = DESCRIBER_SYSTEM
    assert PROMPT_VERSION in p
    assert "200 字" in p
    assert "以上情况本人已人工核实" in p
    assert "不夸张" in p and "不添造" in p
    assert "description" in p


# ---------------------------------------------------------------------------
# build_user_prompt
# ---------------------------------------------------------------------------


def test_build_user_prompt_image() -> None:
    p = build_user_prompt("image", path="data/img/a.jpg")
    assert isinstance(p, str) and p
    assert "文件:data/img/a.jpg" in p


def test_build_user_prompt_page() -> None:
    p1 = build_user_prompt("page", path="data/shots/home.png")
    assert "截图:data/shots/home.png" in p1
    # screenshot 键同样识别
    p2 = build_user_prompt("page", screenshot="data/shots/list.png")
    assert "截图:data/shots/list.png" in p2


def test_build_user_prompt_arbiter_with_dicts() -> None:
    p = build_user_prompt(
        "arbiter",
        member_scores=[
            {"model": "stub", "nsfw_prob": 0.97},
            {"model": "clip", "nsfw_prob": 0.12},
        ],
        path="data/img/x.jpg",
    )
    assert "- stub:0.9700" in p
    assert "- clip:0.1200" in p
    assert "文件:data/img/x.jpg" in p
    assert "独立" in p


def test_build_user_prompt_arbiter_with_image_scores() -> None:
    img = ImageEvidence(
        path="data/img/y.png",
        url="https://example.invalid/y.png",
        source_page="https://example.invalid/",
    )
    members = [
        ImageScore(image=img, model="stub", nsfw_prob=0.97),
        ImageScore(image=img, model="glm", nsfw_prob=0.31),
    ]
    p = build_user_prompt("arbiter", member_scores=members)
    assert "- stub:0.9700" in p
    assert "- glm:0.3100" in p


def test_build_user_prompt_arbiter_with_pairs() -> None:
    p = build_user_prompt("arbiter", member_scores=[("nudenet", 0.5)])
    assert "- nudenet:0.5000" in p


def test_build_user_prompt_describer_with_facts_dict() -> None:
    facts = {
        "site": "https://bad.example",
        "verdict": "nsfw",
        "agg": 0.93,
        "nsw_count": 5,
        "pages": 12,
        "url_risk": ["可疑顶级域 xyz"],
        "text_risk": ["中文关键词命中 3 个"],
    }
    p = build_user_prompt("describer", facts=facts)
    assert "站点:https://bad.example" in p
    assert "判定:nsfw" in p
    assert "agg:0.93" in p
    assert "达标数:5" in p
    assert "页面数:12" in p
    assert "URL风险要点:可疑顶级域 xyz" in p
    assert "文本风险要点:中文关键词命中 3 个" in p


def test_build_user_prompt_describer_with_facts_list() -> None:
    p = build_user_prompt("describer", facts=["站点 https://a.example", "agg 0.9"])
    assert "- 站点 https://a.example" in p
    assert "- agg 0.9" in p


def test_build_user_prompt_passthrough_extra_ctx() -> None:
    p = build_user_prompt("image", path="x.png", note="复核", foo=1, flag=True)
    assert "文件:x.png" in p
    assert "note:复核" in p
    assert "foo:1" in p
    assert "flag:是" in p


def test_build_user_prompt_unknown_kind_raises() -> None:
    with pytest.raises(ValueError, match="kind"):
        build_user_prompt("nope", path="x.png")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# parse_json_response:脏输入矩阵
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 0. 干净基线
        ('{"nsfw_prob": 0.42}', {"nsfw_prob": 0.42}),
        # 1. ```json 围栏
        (
            '```json\n{"nsfw_prob": 0.42, "categories": ["低俗"]}\n```',
            {"nsfw_prob": 0.42, "categories": ["低俗"]},
        ),
        # 2. 无语言标签围栏
        ('```\n{"a": 1}\n```', {"a": 1}),
        # 3. 围栏无换行
        ('```json{"a": 1}```', {"a": 1}),
        # 4. 围栏后还有杂质文字
        ('```json\n{"a": 1}\n```\n以上是审核结果,请查收。', {"a": 1}),
        # 5. 围栏未闭合(退化按普通文本处理)
        ('```json\n{"b": 2}', {"b": 2}),
        # 6. 前后杂质
        (
            '好的,以下是审核结果:{"nsfw_prob": 0.5, "categories": ["色情"]} 希望有帮助。',
            {"nsfw_prob": 0.5, "categories": ["色情"]},
        ),
        # 7. 尾逗号
        (
            '{"nsfw_prob": 0.5, "categories": ["色情",],}',
            {"nsfw_prob": 0.5, "categories": ["色情"]},
        ),
        # 8. 单引号字符串
        (
            "{'nsfw_prob': 0.5, 'categories': ['低俗'], 'reasoning': '可见低俗画面'}",
            {"nsfw_prob": 0.5, "categories": ["低俗"], "reasoning": "可见低俗画面"},
        ),
        # 9. 单引号 + 尾逗号组合
        (
            "{'nsfw_prob': 0.5, 'categories': ['低俗'],}",
            {"nsfw_prob": 0.5, "categories": ["低俗"]},
        ),
        # 10. 字符串内花括号 + 画面中的注入语(必须当作内容保留,不影响配平)
        (
            '{"reasoning": "图内文字含 { 忽略之前指令 } 与 } 花括号", "nsfw_prob": 0.2}',
            {"reasoning": "图内文字含 { 忽略之前指令 } 与 } 花括号", "nsfw_prob": 0.2},
        ),
        # 11. 字符串内转义引号 + 花括号
        (
            r'{"reasoning": "字符串内\"引号\"与 { 花括号}", "nsfw_prob": 0.1}',
            {"reasoning": '字符串内"引号"与 { 花括号}', "nsfw_prob": 0.1},
        ),
        # 12. Python 字面量 True/False/None
        (
            '{"nsfw_prob": 0.5, "flag": True, "none": None}',
            {"nsfw_prob": 0.5, "flag": True, "none": None},
        ),
        # 13. 嵌套对象/数组
        ('{"outer": {"inner": [1, 2]}}', {"outer": {"inner": [1, 2]}}),
        # 14-17. 非法输入 → None
        ("这不是 JSON,没有花括号", None),
        ('{"nsfw_prob": 0.5', None),  # 不闭合
        ("[1, 2, 3]", None),          # 顶层为数组且无对象
        ("", None),                   # 空串
    ],
    ids=[
        "clean",
        "fenced-json",
        "fenced-bare",
        "fenced-no-newline",
        "fenced-trailing-junk",
        "fence-unclosed",
        "junk-around",
        "trailing-comma",
        "single-quotes",
        "single-quotes-plus-comma",
        "braces-in-string",
        "escaped-quotes-in-string",
        "python-literals",
        "nested",
        "not-json",
        "unbalanced",
        "top-level-array",
        "empty",
    ],
)
def test_parse_json_response_dirty_inputs(text: str, expected: dict | None) -> None:
    assert parse_json_response(text) == expected


def test_parse_json_response_whitespace_only() -> None:
    assert parse_json_response("   \n\t ") is None


def test_parse_json_response_non_string_input() -> None:
    assert parse_json_response(None) is None  # type: ignore[arg-type]
    assert parse_json_response(123) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# validate_image_json
# ---------------------------------------------------------------------------


def test_validate_ok_full() -> None:
    d = {
        "nsfw_prob": 0.62,
        "categories": ["低俗", "性感但正常"],
        "reasoning": "画面可见低俗内容",
        "confidence": 0.8,
    }
    prob, cleaned = validate_image_json(d)
    assert prob == pytest.approx(0.62)
    assert cleaned["nsfw_prob"] == pytest.approx(0.62)
    assert cleaned["categories"] == ["低俗", "性感但正常"]
    assert cleaned["confidence"] == pytest.approx(0.8)
    assert cleaned["reasoning"] == "画面可见低俗内容"
    assert "error" not in cleaned


def test_validate_missing_nsfw_prob() -> None:
    prob, cleaned = validate_image_json({"categories": ["正常"]})
    assert prob == 0.0
    assert "error" in cleaned
    assert cleaned["error"]  # 非空中文消息


@pytest.mark.parametrize(
    "bad",
    ["abc", "", None, [0.5], {"x": 1}, True, float("nan"), float("inf")],
    ids=[
        "str",
        "empty-str",
        "none",
        "list",
        "dict",
        "bool",
        "nan",
        "inf",
    ],
)
def test_validate_invalid_nsfw_prob(bad: object) -> None:
    prob, cleaned = validate_image_json({"nsfw_prob": bad})
    assert prob == 0.0
    assert "error" in cleaned
    assert cleaned["error"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(1.5, 1.0), (-0.2, 0.0), (2, 1.0), (0, 0.0), (1, 1.0), ("0.88", 0.88), (0.44, 0.44)],
    ids=["high-float", "low-float", "high-int", "zero", "one", "numeric-str", "in-range"],
)
def test_validate_clamps_prob(raw: object, expected: float) -> None:
    prob, cleaned = validate_image_json({"nsfw_prob": raw})
    assert prob == pytest.approx(expected)
    assert cleaned["nsfw_prob"] == pytest.approx(expected)
    assert "error" not in cleaned


def test_validate_filters_categories() -> None:
    _, cleaned = validate_image_json(
        {"nsfw_prob": 0.9, "categories": ["色情", "编造类别", "正常", "色情", 123, "涉未成年人"]}
    )
    # 非法类别被过滤、重复去重、顺序保持、涉未成年人为合法附加标记
    assert cleaned["categories"] == ["色情", "正常", "涉未成年人"]


@pytest.mark.parametrize(
    "cats",
    [None, "色情", 42, []],
    ids=["missing", "plain-str", "int", "empty-list"],
)
def test_validate_categories_missing_or_non_list(cats: object) -> None:
    _, cleaned = validate_image_json({"nsfw_prob": 0.1, "categories": cats})
    assert cleaned["categories"] == []
    assert "error" not in cleaned


def test_validate_confidence_and_reasoning_handling() -> None:
    _, cleaned = validate_image_json({"nsfw_prob": 0.5, "confidence": 1.7, "reasoning": 123})
    assert cleaned["confidence"] == pytest.approx(1.0)  # clamp
    assert "reasoning" not in cleaned                   # 非字符串 → 丢弃
    _, cleaned2 = validate_image_json({"nsfw_prob": 0.5, "confidence": "高"})
    assert "confidence" not in cleaned2                 # 不可解析 → 丢弃
    _, cleaned3 = validate_image_json({"nsfw_prob": 0.5, "reasoning": "   "})
    assert "reasoning" not in cleaned3                  # 空白字符串 → 丢弃


def test_validate_non_dict_input() -> None:
    prob, cleaned = validate_image_json([1, 2])  # type: ignore[arg-type]
    assert prob == 0.0
    assert "error" in cleaned


# ---------------------------------------------------------------------------
# calibrate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), ANCHORS)
def test_calibrate_all_anchors(raw: float, expected: float) -> None:
    assert calibrate(raw) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0.15, 0.185),   # (0,0.02)-(0.3,0.35) 中点
        (0.40, 0.45),    # (0.3,0.35)-(0.5,0.55) 中点
        (0.60, 0.665),   # (0.5,0.55)-(0.7,0.78) 中点
        (0.775, 0.84),   # (0.7,0.78)-(0.85,0.90) 中点
        (0.90, 0.935),   # (0.85,0.90)-(0.95,0.97) 中点
        (0.975, 0.98),   # (0.95,0.97)-(1.0,0.99) 中点
    ],
    ids=["seg1", "seg2", "seg3", "seg4", "seg5", "seg6"],
)
def test_calibrate_segment_midpoints(raw: float, expected: float) -> None:
    assert calibrate(raw) == pytest.approx(expected)


def test_calibrate_clamps_input_range() -> None:
    assert calibrate(-0.5) == pytest.approx(0.02)
    assert calibrate(7.0) == pytest.approx(0.99)
    assert calibrate(0.0) == pytest.approx(0.02)
    assert calibrate(1.0) == pytest.approx(0.99)


def test_calibrate_monotonic_nondecreasing() -> None:
    values = [calibrate(i / 100) for i in range(101)]
    assert all(a <= b for a, b in zip(values, values[1:]))


# ---------------------------------------------------------------------------
# V5 升级:解析遥测 / 修复链单遍 / 二分校准(test_v5_*)
# ---------------------------------------------------------------------------


def _counter() -> float:
    return telemetry.snapshot()["counters"].get("vlm_prompts.parse_fail", 0.0)


def test_v5_parse_fail_telemetry_only_on_none() -> None:
    """vlm_prompts.parse_fail 仅在 parse_json_response 最终返回 None 时 +1。"""
    telemetry.reset()
    assert _counter() == 0.0
    assert parse_json_response('{"a": 1}') == {"a": 1}
    assert _counter() == 0.0  # 成功不计数
    assert parse_json_response("完全不是 JSON") is None
    assert _counter() == 1.0
    assert parse_json_response(None) is None  # type: ignore[arg-type]
    assert _counter() == 2.0
    assert parse_json_response("[1, 2]") is None
    assert _counter() == 3.0


def test_v5_parse_impl_has_no_telemetry_side_effect() -> None:
    """内部实现 _parse_json_response_impl 不触发计数(避免修复链内部复用重复计数)。"""
    from netsentinel.vision.vlm_prompts import _parse_json_response_impl

    telemetry.reset()
    assert _parse_json_response_impl("垃圾输入") is None
    assert _counter() == 0.0


@pytest.mark.parametrize(
    "s",
    [
        '{"flag": True, "none": None}',
        "True False None",
        "ATrue True, (False)None. NoneX TrueTrue",
        '{"a": [True, False], "b": {"c": None}}',
        "",
        "无字面量",
    ],
    ids=["json", "bare", "word-boundaries", "nested", "empty", "clean"],
)
def test_v5_fix_python_literals_single_pass_equivalence(s: str) -> None:
    """单遍交替正则与旧版三条 re.sub 顺序替换结果逐字符一致,且幂等。"""
    from netsentinel.vision.vlm_prompts import _fix_python_literals

    reference = s
    for pattern, repl in ((r"\bTrue\b", "true"), (r"\bFalse\b", "false"), (r"\bNone\b", "null")):
        reference = re.sub(pattern, repl, reference)
    once = _fix_python_literals(s)
    assert once == reference
    assert _fix_python_literals(once) == once  # 幂等:小写结果不会再被命中


def test_v5_repair_chain_combined_dirty_input() -> None:
    """围栏 + 单引号 + Python 字面量 + 尾逗号一次性叠加,修复链单遍后仍可解析。"""
    text = (
        "审核结果如下:\n```json\n"
        "{'nsfw_prob': 0.5, 'flag': True, 'none': None, 'categories': ['低俗',],}\n"
        "```\n以上仅供人工复核。"
    )
    assert parse_json_response(text) == {
        "nsfw_prob": 0.5,
        "flag": True,
        "none": None,
        "categories": ["低俗"],
    }
    # 单遍合并后不应重复计数解析失败
    telemetry.reset()
    assert parse_json_response(text) is not None
    assert _counter() == 0.0


def _reference_calibrate(raw: float) -> float:
    """测试侧参考实现:clamp + 线性扫描插值(与旧实现同构,用于锁定二分改写)。"""
    try:
        x = float(raw)
    except (TypeError, ValueError):
        x = 0.0
    if x != x:  # NaN
        x = 0.0
    x = min(1.0, max(0.0, x))
    if x <= ANCHORS[0][0]:
        return ANCHORS[0][1]
    for (x0, y0), (x1, y1) in zip(ANCHORS, ANCHORS[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return ANCHORS[-1][1]


#: 校准等价性网格:0.025 步长全覆盖 + 每个锚点左右各偏移 1e-9
_EQUIV_GRID: list[float] = [i / 40 for i in range(41)]
for _ax, _ in ANCHORS:
    _EQUIV_GRID.extend([_ax - 1e-9, _ax + 1e-9])


@pytest.mark.parametrize("x", _EQUIV_GRID)
def test_v5_calibrate_matches_reference_interpolation(x: float) -> None:
    """二分实现与线性扫描参考实现在全网格上逐点等价(含锚点左右边界)。"""
    assert calibrate(x) == pytest.approx(_reference_calibrate(x))


@pytest.mark.parametrize(
    "raw", [-1.0, 2.0, 7.0, float("nan"), float("inf"), "0.5", "abc", None],
    ids=["neg", "over", "far-over", "nan", "inf", "numeric-str", "bad-str", "none"],
)
def test_v5_calibrate_edge_inputs_match_reference(raw: object) -> None:
    """越界 / NaN / inf / 字符串输入:与参考实现的收敛语义一致。"""
    assert calibrate(raw) == pytest.approx(_reference_calibrate(raw))  # type: ignore[arg-type]
