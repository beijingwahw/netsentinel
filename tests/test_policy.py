"""A49 声明式政策引擎测试(离线;不访问网络、不读真实门户)。

覆盖:内置默认政策、各 when 条件命中/不命中、首条优先、非法 action 与
未知 when 键拒绝加载、兜底决策、YAML 往返加载、describe_actions 红线文案。

V5 新增(test_v5_* 前缀):policy.decide.<action> 决策计数遥测、when 编译
闭包与逐字段解释的语义一致性(含未知键忽略)、编译缓存不影响 Rule 相等性、
load_policy/default_rules 加载期预热编译。

P4 安全审计清偿新增(test_p4_* 前缀):policy_sha256 入决策审计——sink 注入后
每次决策落 policy_decide 事件且哈希正确、政策内容变更重载后哈希跟随、哈希
与审计旁路不影响匹配语义、sink 异常绝不影响决策、默认未注入零行为。
"""
from __future__ import annotations

import hashlib
import pathlib
from collections.abc import Iterator

import pytest
import yaml

from netsentinel import telemetry
from netsentinel.contracts import SiteReport, Verdict
from netsentinel.policy import (
    VALID_ACTIONS,
    Decision,
    Rule,
    decide,
    default_rules,
    describe_actions,
    load_policy,
)
from netsentinel.policy import engine as policy_engine

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLE_POLICY = PROJECT_ROOT / "netsentinel" / "policy" / "policy.example.yaml"


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def make_report(
    verdict: Verdict = Verdict.CLEAN,
    agg: float = 0.0,
    needs_review: bool = False,
    url_risk: float | None = None,
) -> SiteReport:
    """构造一份最小可判定的站点报告(可选附带 URL 风险情报)。"""
    report = SiteReport(site_url="https://example.com")
    report.verdict = verdict
    report.agg_nsw_prob = agg
    report.needs_review = needs_review
    if url_risk is not None:
        report.intel["url"] = {"risk": url_risk}
    return report


def write_policy(tmp_path: pathlib.Path, content: str) -> str:
    """把 YAML 文本写进临时政策文件,返回路径字符串。"""
    p = tmp_path / "policy.yaml"
    p.write_text(content, encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# 默认政策
# ---------------------------------------------------------------------------

def test_load_policy_missing_file_returns_builtin_default(
    tmp_path: pathlib.Path,
) -> None:
    """文件不存在 → 单条 default 规则,action=queue,note 固定中文。"""
    rules = load_policy(str(tmp_path / "nope.yaml"))
    assert rules == [
        Rule(name="default", when={}, action="queue", note="默认:全部进入人工复核")
    ]


def test_load_policy_empty_file_returns_default(tmp_path: pathlib.Path) -> None:
    """空文件 / 空列表 → 同样回退内置默认政策,不会产生零规则。"""
    assert load_policy(write_policy(tmp_path, "")) == default_rules()
    assert load_policy(write_policy(tmp_path, "[]")) == default_rules()


def test_decide_without_rules_queues_every_report() -> None:
    """rules=None → 内置默认政策:任何判级都 queue 且 matched=True。"""
    for verdict in (Verdict.CLEAN, Verdict.SUSPECT, Verdict.NSFW):
        report = make_report(verdict=verdict, agg=0.99)
        decision = decide(report)
        assert isinstance(decision, Decision)
        assert decision.rule_name == "default"
        assert decision.action == "queue"
        assert decision.matched is True


def test_default_rules_immutable_snapshot() -> None:
    """default_rules() 每次返回新对象,调用方修改不污染模块状态。"""
    first = default_rules()
    first[0].action = "ignore"
    assert default_rules()[0].action == "queue"


# ---------------------------------------------------------------------------
# when 各条件:命中 / 不命中
# ---------------------------------------------------------------------------

def test_when_verdict_hit_and_miss() -> None:
    rule = Rule("v", {"verdict": ["nsfw", "suspect"]}, "queue")
    assert decide(make_report(Verdict.NSFW), [rule]).matched is True
    assert decide(make_report(Verdict.SUSPECT), [rule]).rule_name == "v"
    clean_hit = decide(make_report(Verdict.CLEAN), [rule])
    assert clean_hit.matched is False
    assert clean_hit.action == "queue"  # 兜底也是 queue


def test_when_min_agg_boundary() -> None:
    """min_agg 为闭区间下限:agg == min_agg 命中,略低不命中。"""
    rule = Rule("agg", {"min_agg": 0.5}, "notify")
    assert decide(make_report(agg=0.5), [rule]).matched is True
    assert decide(make_report(agg=0.9), [rule]).matched is True
    assert decide(make_report(agg=0.4999), [rule]).matched is False


def test_when_min_url_risk() -> None:
    rule = Rule("url", {"min_url_risk": 0.7}, "notify")
    hit = decide(make_report(url_risk=0.8), [rule])
    assert hit.matched is True and hit.action == "notify"
    assert decide(make_report(url_risk=0.3), [rule]).matched is False


def test_when_min_url_risk_missing_intel_never_matches() -> None:
    """字段存在才比较:intel 缺 url / url 非 dict / risk 非法 → 一律不命中。"""
    rule = Rule("url", {"min_url_risk": 0.7}, "notify")
    assert decide(make_report(), [rule]).matched is False  # 无 intel

    report = make_report()
    report.intel["url"] = {"features": {}}  # 无 risk 键
    assert decide(report, [rule]).matched is False

    report = make_report()
    report.intel["url"] = "https://example.com"  # 非 dict
    assert decide(report, [rule]).matched is False

    report = make_report()
    report.intel["url"] = {"risk": "high"}  # 非数字
    assert decide(report, [rule]).matched is False


def test_when_needs_review_equality() -> None:
    """needs_review 按相等比较,不做真值外推。"""
    rule_true = Rule("nr", {"needs_review": True}, "queue")
    rule_false = Rule("nf", {"needs_review": False}, "ignore")
    assert decide(make_report(needs_review=True), [rule_true]).matched is True
    assert decide(make_report(needs_review=False), [rule_true]).matched is False
    assert decide(make_report(needs_review=False), [rule_false]).matched is True
    assert decide(make_report(needs_review=True), [rule_false]).matched is False


def test_when_conditions_are_combined_with_and() -> None:
    """多条件 AND:只满足其一不命中,全满足才命中。"""
    rule = Rule("combo", {"verdict": ["nsfw"], "min_agg": 0.9, "min_url_risk": 0.5}, "four_eyes")
    assert decide(make_report(Verdict.NSFW, agg=0.95, url_risk=0.6), [rule]).matched is True
    assert decide(make_report(Verdict.NSFW, agg=0.95), [rule]).matched is False
    assert decide(make_report(Verdict.SUSPECT, agg=0.95, url_risk=0.6), [rule]).matched is False


def test_empty_when_matches_everything() -> None:
    """when 为空 dict = 匹配一切报告。"""
    rule = Rule("all", {}, "four_eyes")
    for verdict in (Verdict.CLEAN, Verdict.SUSPECT, Verdict.NSFW):
        assert decide(make_report(verdict), [rule]).matched is True


# ---------------------------------------------------------------------------
# 优先级与兜底
# ---------------------------------------------------------------------------

def test_first_match_wins() -> None:
    """首条命中生效:两条均可命中时,返回列表中靠前那条。"""
    first = Rule("first", {"verdict": ["nsfw"]}, "queue")
    second = Rule("second", {"verdict": ["nsfw"]}, "four_eyes")
    report = make_report(Verdict.NSFW, agg=0.99)
    assert decide(report, [first, second]).rule_name == "first"
    assert decide(report, [second, first]).rule_name == "second"


def test_fallback_decision_when_no_rule_matches() -> None:
    """全部未命中 → 兜底 fallback/queue/matched=False,文案固定。"""
    rule = Rule("only_nsfw", {"verdict": ["nsfw"]}, "four_eyes")
    decision = decide(make_report(Verdict.CLEAN), [rule])
    assert decision.rule_name == "fallback"
    assert decision.action == "queue"
    assert decision.matched is False
    assert decision.note == "未命中任何规则,默认进入人工复核"


def test_decide_rejects_invalid_programmatic_action() -> None:
    """代码直接构造的非法 action 也在 decide 处被拦截(双保险)。"""
    bad = Rule("bad", {}, "auto_submit")
    with pytest.raises(ValueError) as excinfo:
        decide(make_report(), [bad])
    msg = str(excinfo.value)
    assert "auto_submit" in msg and "four_eyes" in msg


# ---------------------------------------------------------------------------
# YAML 加载校验
# ---------------------------------------------------------------------------

def test_load_policy_rejects_invalid_action(tmp_path: pathlib.Path) -> None:
    """非法 action → ValueError(中文,列出合法集合)。"""
    path = write_policy(
        tmp_path,
        "- name: r1\n  when: {}\n  action: auto_submit\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_policy(path)
    msg = str(excinfo.value)
    assert "auto_submit" in msg
    for legal in VALID_ACTIONS:
        assert legal in msg
    assert "人工门" in msg


def test_load_policy_rejects_unknown_when_key(tmp_path: pathlib.Path) -> None:
    """when 未知键 → ValueError(中文,点名未知键与合法键)。"""
    path = write_policy(
        tmp_path,
        "- name: r1\n  when:\n    verdictx: [nsfw]\n  action: queue\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_policy(path)
    msg = str(excinfo.value)
    assert "verdictx" in msg
    for key in ("verdict", "min_agg", "min_url_risk", "needs_review"):
        assert key in msg


def test_load_policy_rejects_bad_value_types(tmp_path: pathlib.Path) -> None:
    """when 值类型不符(verdict 非列表 / 判级拼错 / 阈值越界或非数字 /
    needs_review 非布尔)逐条抛 ValueError。"""
    bad_cases = [
        "- name: r\n  when:\n    verdict: nsfw\n  action: queue\n",
        "- name: r\n  when:\n    verdict: [not_a_verdict]\n  action: queue\n",
        "- name: r\n  when:\n    min_agg: 1.5\n  action: queue\n",
        "- name: r\n  when:\n    min_url_risk: high\n  action: queue\n",
        "- name: r\n  when:\n    needs_review: yes_please\n  action: queue\n",
    ]
    for content in bad_cases:
        with pytest.raises(ValueError):
            load_policy(write_policy(tmp_path, content))


def test_load_policy_rejects_bad_structure(tmp_path: pathlib.Path) -> None:
    """顶层非列表 / 元素非映射 / 缺 name / 未知规则字段 → ValueError。"""
    for content in (
        "rules:\n  - name: r\n",                # 顶层是映射
        "- just_a_string\n",                     # 元素是字符串
        "- when: {}\n  action: queue\n",         # 缺 name
        "- name: r\n  action: queue\n  extra: 1\n",  # 未知字段
    ):
        with pytest.raises(ValueError):
            load_policy(write_policy(tmp_path, content))


def test_load_policy_error_includes_line_number(tmp_path: pathlib.Path) -> None:
    """报错尽量带行号定位:第二条规则(第 4 行)非法时报"行"。"""
    path = write_policy(
        tmp_path,
        "- name: ok\n  when: {}\n  action: queue\n"
        "- name: bad\n  when: {}\n  action: boom\n",
    )
    with pytest.raises(ValueError) as excinfo:
        load_policy(path)
    assert "行" in str(excinfo.value)
    assert "第 2 条" in str(excinfo.value)


def test_yaml_roundtrip(tmp_path: pathlib.Path) -> None:
    """yaml.safe_dump 写出的规则列表 → load_policy 解析回等价 Rule。"""
    rules = [
        Rule("high", {"verdict": ["nsfw"], "min_agg": 0.9}, "four_eyes", "四眼"),
        Rule("notify", {"min_url_risk": 0.7}, "notify"),
        Rule("log", {"verdict": ["clean"], "needs_review": False}, "ignore", ""),
    ]
    path = tmp_path / "roundtrip.yaml"
    path.write_text(
        yaml.safe_dump(
            [
                {"name": r.name, "when": r.when, "action": r.action, "note": r.note}
                for r in rules
            ],
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    assert load_policy(str(path)) == rules


def test_loaded_rules_drive_decide(tmp_path: pathlib.Path) -> None:
    """load_policy 产物直接喂 decide:高置信 nsfw → four_eyes。"""
    path = write_policy(
        tmp_path,
        "- name: high\n  when:\n    verdict: [nsfw]\n    min_agg: 0.9\n"
        "  action: four_eyes\n  note: 追加第二审核人\n",
    )
    rules = load_policy(path)
    decision = decide(make_report(Verdict.NSFW, agg=0.95), rules)
    assert decision == Decision(
        rule_name="high", action="four_eyes", note="追加第二审核人", matched=True
    )


# ---------------------------------------------------------------------------
# 随包示例文件与 describe_actions
# ---------------------------------------------------------------------------

def test_example_policy_loads_and_decides() -> None:
    """policy.example.yaml:≥3 条规则、动作合法,且三分流示例各就各位。"""
    rules = load_policy(str(EXAMPLE_POLICY))
    assert len(rules) >= 3
    assert [r.action for r in rules[:3]] == ["four_eyes", "notify", "ignore"]

    # 高置信 nsfw → 四眼复核
    d1 = decide(make_report(Verdict.NSFW, agg=0.95), rules)
    assert d1.action == "four_eyes" and d1.matched is True
    # 仅 URL 风险高(clean + url risk)→ 通知(顺序即优先级的体现)
    d2 = decide(make_report(Verdict.CLEAN, url_risk=0.8), rules)
    assert d2.action == "notify"
    # verdict=clean 且无 URL 风险 → 仅记录
    d3 = decide(make_report(Verdict.CLEAN), rules)
    assert d3.action == "ignore"

    text = EXAMPLE_POLICY.read_text(encoding="utf-8")
    assert "人工门" in text and "红线" in text


def test_describe_actions_documents_human_gate() -> None:
    """describe_actions:覆盖全部 action,每条文案都注明"人工门"。"""
    descriptions = describe_actions()
    assert set(descriptions.keys()) == set(VALID_ACTIONS)
    for action, text in descriptions.items():
        assert "人工门" in text, f"action {action} 的说明缺少人工门红线"
    assert "第二审核人" in descriptions["four_eyes"]
    assert "追加" in descriptions["four_eyes"]


# ---------------------------------------------------------------------------
# V5:决策遥测(policy.decide.<action>)与规则编译一次
# ---------------------------------------------------------------------------


def test_v5_decide_telemetry_action_counters() -> None:
    """decide 按生效动作计数 policy.decide.<action>;兜底决策计入 queue。"""
    counters = telemetry.snapshot()["counters"]
    base_notify = counters.get("policy.decide.notify", 0.0)
    base_queue = counters.get("policy.decide.queue", 0.0)

    rules = [
        Rule("n", {"min_url_risk": 0.7}, "notify"),
        Rule("only_nsfw", {"verdict": ["nsfw"]}, "four_eyes"),
    ]
    assert decide(make_report(url_risk=0.8), rules).action == "notify"
    assert decide(make_report(), rules).action == "queue"  # 未命中 → 兜底 queue

    counters = telemetry.snapshot()["counters"]
    assert counters.get("policy.decide.notify", 0.0) == base_notify + 1.0
    assert counters.get("policy.decide.queue", 0.0) == base_queue + 1.0


def test_v5_compiled_when_preserves_match_semantics() -> None:
    """编译后的判断闭包与逐字段解释语义一致:四条件 AND、单条件不命中、
    闭区间下限、未知键忽略(程序化规则)、缓存路径重复 decide 结果稳定。"""
    combo = Rule(
        "combo",
        {"verdict": ["nsfw", "suspect"], "min_agg": 0.5, "min_url_risk": 0.5, "needs_review": True},
        "four_eyes",
    )
    hit = make_report(Verdict.NSFW, agg=0.6, needs_review=True, url_risk=0.7)
    assert decide(hit, [combo]).matched is True
    # 逐个条件不命中(其余全满足)
    assert decide(make_report(Verdict.CLEAN, agg=0.6, needs_review=True, url_risk=0.7), [combo]).matched is False
    assert decide(make_report(Verdict.NSFW, agg=0.4, needs_review=True, url_risk=0.7), [combo]).matched is False
    assert decide(make_report(Verdict.NSFW, agg=0.6, needs_review=True), [combo]).matched is False  # url 缺失
    assert decide(make_report(Verdict.NSFW, agg=0.6, needs_review=False, url_risk=0.7), [combo]).matched is False
    # 下限为闭区间:agg == min_agg、risk == min_url_risk 均命中
    assert decide(make_report(Verdict.NSFW, agg=0.5, needs_review=True, url_risk=0.5), [combo]).matched is True
    # 缓存路径:第二次 decide(命中已编译闭包)结果与第一次一致
    assert decide(hit, [combo]) == decide(hit, [combo])

    # 程序化规则含未知键:与旧解释器一致地忽略未知键 → 匹配一切
    unknown = Rule("u", {"verdictx": 1}, "ignore")
    assert decide(make_report(), [unknown]).matched is True

    # 决策后仍拦截非法 action(校验保留在 decide,不受编译缓存影响)
    mutated = Rule("m", {}, "queue")
    decide(make_report(), [mutated])
    mutated.action = "auto_submit"  # 编译后中途改坏 action
    with pytest.raises(ValueError, match="auto_submit"):
        decide(make_report(), [mutated])


def test_v5_rule_equality_and_precompiled_cache() -> None:
    """编译缓存(_match)不参与相等性/repr;load_policy 与 default_rules
    返回前即完成预热编译(decide 热路径零解析)。"""
    a = Rule("r", {"verdict": ["nsfw"]}, "queue")
    b = Rule("r", {"verdict": ["nsfw"]}, "queue")
    decide(make_report(Verdict.NSFW), [a])
    assert a._match is not None  # 首次 decide 后已缓存编译闭包
    assert b._match is None
    assert a == b  # 缓存不影响相等性
    assert "_match" not in repr(a)  # 也不出现在 repr 中

    # 加载期预热:随包示例与内置默认政策的规则均已编译
    rules = load_policy(str(EXAMPLE_POLICY))
    assert all(r._match is not None for r in rules)
    assert default_rules()[0]._match is not None
    # 预热后的规则与手工构造的等值规则仍相等(既有比较语义不变)
    r0 = rules[0]
    assert r0 == Rule(name=r0.name, when=r0.when, action=r0.action, note=r0.note)


# ---------------------------------------------------------------------------
# P4 安全审计清偿:policy_sha256 入决策审计(sink 注入式旁路)
# ---------------------------------------------------------------------------


@pytest.fixture()
def audit_events() -> Iterator[list[dict]]:
    """注入捕获 sink 返回事件列表;用例结束自动清除注入(模块状态零残留)。"""
    events: list[dict] = []
    policy_engine.configure_audit_sink(events.append)
    yield events
    policy_engine.configure_audit_sink()  # 清除注入,不泄漏到其他用例


def test_p4_decision_audit_event_carries_policy_sha256(
    tmp_path: pathlib.Path, audit_events: list[dict]
) -> None:
    """sink 注入后每次决策落一条 policy_decide 事件,policy_sha256 与
    独立计算的文件字节 sha256 一致;成功送达计数 policy.decision.audited。"""
    path = write_policy(tmp_path, "- name: r1\n  when: {}\n  action: queue\n")
    rules = load_policy(path)
    expected = hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
    base_audited = telemetry.snapshot()["counters"].get("policy.decision.audited", 0.0)

    decision = decide(make_report(), rules)

    assert decision.action == "queue"
    assert len(audit_events) == 1  # 一次 decide 恰好一条审计事件
    event = audit_events[0]
    assert event["event"] == "policy_decide"
    assert event["policy_sha256"] == expected
    assert event["rule_name"] == "r1"
    assert event["action"] == "queue"
    assert event["matched"] is True
    assert event["site_url"] == "https://example.com"
    assert event["verdict"] == "clean"  # str 枚举序列化为值
    assert (
        telemetry.snapshot()["counters"].get("policy.decision.audited", 0.0)
        == base_audited + 1.0
    )


def test_p4_policy_content_change_rotates_hash(
    tmp_path: pathlib.Path, audit_events: list[dict]
) -> None:
    """政策内容变更后重载:决策审计哈希跟随新内容(新旧哈希不同)。"""
    path = write_policy(tmp_path, "- name: r1\n  when: {}\n  action: queue\n")
    decide(make_report(), load_policy(path))
    first = audit_events[-1]

    write_policy(
        tmp_path, "- name: r2\n  when:\n    verdict: [nsfw]\n  action: four_eyes\n"
    )
    rules2 = load_policy(path)
    decision2 = decide(make_report(Verdict.NSFW), rules2)
    second = audit_events[-1]

    assert decision2.rule_name == "r2" and decision2.action == "four_eyes"
    assert second["policy_sha256"] == hashlib.sha256(
        pathlib.Path(path).read_bytes()
    ).hexdigest()
    assert first["policy_sha256"] != second["policy_sha256"]
    # 陈旧列表对象哈希不变:用哪份规则决策,就归因到哪份版本
    assert rules2.sha256 == second["policy_sha256"]


def test_p4_hash_and_sink_do_not_change_matching_semantics(
    tmp_path: pathlib.Path, audit_events: list[dict]
) -> None:
    """哈希入审计是纯旁路:注入/清除 sink 后同一批规则的 Decision 逐位一致。"""
    path = write_policy(
        tmp_path,
        "- name: high\n  when:\n    verdict: [nsfw]\n    min_agg: 0.9\n"
        "  action: four_eyes\n",
    )
    rules = load_policy(path)
    hit = make_report(Verdict.NSFW, agg=0.95)
    miss = make_report(Verdict.CLEAN)

    with_sink_hit = decide(hit, rules)
    with_sink_miss = decide(miss, rules)
    assert len(audit_events) == 2

    policy_engine.configure_audit_sink()  # 清除注入再决策一轮
    assert decide(hit, rules) == with_sink_hit
    assert decide(miss, rules) == with_sink_miss
    assert decide(hit, rules) == Decision("high", "four_eyes", "", matched=True)
    assert decide(miss, rules) == Decision(
        "fallback", "queue", "未命中任何规则,默认进入人工复核", matched=False
    )
    assert len(audit_events) == 2  # 清除后不再发事件


def test_p4_builtin_and_fallback_decisions_hash_is_none(
    audit_events: list[dict],
) -> None:
    """内置默认政策(rules=None)与程序化规则:无文件来源,审计哈希记 None;
    兜底决策(未命中)同样落审计事件。"""
    decision = decide(make_report())  # rules=None → 内置默认 queue-all
    assert decision.rule_name == "default" and decision.matched is True
    builtin_event = audit_events[-1]
    assert builtin_event["policy_sha256"] is None
    assert builtin_event["rule_name"] == "default"

    only_nsfw = [Rule("only_nsfw", {"verdict": ["nsfw"]}, "four_eyes")]
    fallback = decide(make_report(Verdict.CLEAN), only_nsfw)
    assert fallback.rule_name == "fallback" and fallback.matched is False
    fallback_event = audit_events[-1]
    assert fallback_event["rule_name"] == "fallback"
    assert fallback_event["action"] == "queue"
    assert fallback_event["matched"] is False
    assert fallback_event["policy_sha256"] is None  # 程序化规则无文件来源

    # 缺文件回退内置默认政策:同样无哈希(诚实口径:无文件即无哈希)
    decide(make_report(), load_policy("no-such-policy-file.yaml"))
    assert audit_events[-1]["policy_sha256"] is None


def test_p4_sink_failure_never_breaks_decision(
    tmp_path: pathlib.Path,
) -> None:
    """sink 抛异常绝不影响决策:返回值照常,计数 policy.decision.audit_error。"""
    rules = load_policy(
        write_policy(tmp_path, "- name: r1\n  when: {}\n  action: queue\n")
    )

    def _boom(payload: dict) -> None:
        raise RuntimeError("审计 sink 模拟故障")

    policy_engine.configure_audit_sink(_boom)
    try:
        base_err = telemetry.snapshot()["counters"].get(
            "policy.decision.audit_error", 0.0
        )
        decision = decide(make_report(), rules)
        assert decision.action == "queue" and decision.matched is True
        assert (
            telemetry.snapshot()["counters"].get("policy.decision.audit_error", 0.0)
            == base_err + 1.0
        )
    finally:
        policy_engine.configure_audit_sink()


def test_p4_no_sink_by_default_zero_side_effects(tmp_path: pathlib.Path) -> None:
    """sink 未注入(默认,向后兼容):不发事件、policy.decision.audited 零增长。"""
    counters = telemetry.snapshot()["counters"]
    base_audited = counters.get("policy.decision.audited", 0.0)
    rules = load_policy(
        write_policy(tmp_path, "- name: r1\n  when: {}\n  action: queue\n")
    )

    decide(make_report(), rules)

    assert (
        telemetry.snapshot()["counters"].get("policy.decision.audited", 0.0)
        == base_audited
    )


def test_p4_policy_rules_still_a_plain_list(tmp_path: pathlib.Path) -> None:
    """PolicyRules 兼容普通 list:相等性/迭代/repr 不变,仅多 sha256 属性;
    内置默认政策为普通 list(无 sha256 属性)。"""
    path = write_policy(tmp_path, "- name: r1\n  when: {}\n  action: queue\n")
    rules = load_policy(path)

    assert isinstance(rules, list)
    assert rules == [Rule("r1", {}, "queue", "")]
    assert rules.sha256 == hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
    assert "sha256" not in repr(rules) and "PolicyRules" not in repr(rules)

    builtin = default_rules()
    assert isinstance(builtin, list) and type(builtin) is list
    assert getattr(builtin, "sha256", None) is None


def test_p4_configure_audit_sink_rejects_non_callable() -> None:
    """注入非可调用对象 → TypeError(中文);None/无参调用即清除。"""
    with pytest.raises(TypeError, match="可调用"):
        policy_engine.configure_audit_sink("not-callable")  # type: ignore[arg-type]
    policy_engine.configure_audit_sink()  # 清除不抛(幂等)
