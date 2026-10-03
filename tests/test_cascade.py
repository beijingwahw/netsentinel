# -*- coding: utf-8 -*-
"""级联路由分类器(A42)离线单元测试。

V3 红线 15:全部用例注入 mock client 与 FakeCache,零网络外呼、零真实 VLM 调用;
FakeCache 记录 spend_one 计数,用于断言"每次真实外呼(含升级第二跳)必扣预算"。
"""
from __future__ import annotations

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence
from netsentinel.vision import cascade, vlm_prompts
from netsentinel.vision.cascade import CascadeClassifier
from netsentinel.vision.glm_adapter import VlmOfflineError
from netsentinel.vision.vlm_cache import VlmBudgetExceeded

# 与被测模块运行时一致的缓存键参数(提示词版本 / 模型名 / 图片指纹)
VERSION = cascade._prompt_version()
FLASH = "glm-5.3-flash"
BIG = "glm-4.5v-flash"  # 默认回退链中第一个非 flash 模型,即默认升级模型
SHA = "a" * 64

cal = vlm_prompts.calibrate


# ---------------------------------------------------------------------------
# 测试替身(mock client / FakeCache)
# ---------------------------------------------------------------------------
class FakeClient:
    """mock GLM 客户端:按序回放结果(dict 正常返回 / Exception 抛出),记录每次调用。

    出现预期之外的额外外呼时直接 AssertionError,保证"零多余调用"可被断言。
    """

    def __init__(self, results: list) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def chat_json(self, messages, *, image_paths=None):  # noqa: ANN001,ANN202
        self.calls.append({"messages": messages, "image_paths": image_paths})
        if not self.results:
            raise AssertionError("mock client 出现预期之外的额外外呼")
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeCache:
    """计数缓存:get / put / spend_one;fail_spend_at 表示第几次 spend_one 时预算尽(从 1 起)。"""

    def __init__(self, seed: dict | None = None, fail_spend_at: int | None = None) -> None:
        self.store: dict[tuple, dict] = dict(seed or {})
        self.puts: list[tuple] = []
        self.spends = 0
        self.fail_spend_at = fail_spend_at

    def get(self, model, prompt_version, image_sha256):  # noqa: ANN001
        return self.store.get((model, prompt_version, image_sha256))

    def put(self, model, prompt_version, image_sha256, payload):  # noqa: ANN001
        self.puts.append((model, prompt_version, image_sha256, payload))
        self.store[(model, prompt_version, image_sha256)] = dict(payload)

    def spend_one(self, day=None) -> None:  # noqa: ARG002
        self.spends += 1
        if self.fail_spend_at == self.spends:
            raise VlmBudgetExceeded(self.spends, self.spends - 1)


def make_img(name: str = "pic.png", sha: str = SHA) -> ImageEvidence:
    """构造离线 ImageEvidence(客户端为 mock,文件无需真实存在)。"""
    return ImageEvidence(
        path=f"/offline/{name}",
        url=f"https://example.test/{name}",
        source_page="https://example.test/",
        sha256=sha,
    )


# ---------------------------------------------------------------------------
# 高置信分支:不确定带之外,不升级
# ---------------------------------------------------------------------------
def test_high_confidence_high_branch_no_escalation() -> None:
    """flash 原始 0.97(校准约 0.978)> 0.85:高置信,直接用 flash 分。"""
    client = FakeClient([{"nsfw_prob": 0.97, "reasoning": "明确色情"}])
    cache = FakeCache()
    score = CascadeClassifier(Config(), client=client, cache=cache).classify(make_img())

    assert score.model == "cascade"
    assert score.nsfw_prob == pytest.approx(cal(0.97))
    assert score.nsfw_prob > Config().vlm_escalate_above
    assert score.scores["escalated"] is False
    assert score.scores["flash_prob"] == pytest.approx(cal(0.97))
    assert len(client.calls) == 1
    assert cache.spends == 1
    assert (FLASH, VERSION, SHA) in cache.store  # 高置信结果回写缓存

    # 提示词复用 vlm_prompts.IMAGE_SCORING(契约 A42),图片随消息送审
    assert client.calls[0]["messages"][0]["content"] == vlm_prompts.IMAGE_SCORING_PROMPT
    assert client.calls[0]["image_paths"] == [make_img().path]


def test_high_confidence_low_branch_no_escalation() -> None:
    """flash 原始 0.02(校准约 0.042)< 0.15:高置信正常侧,直接用 flash 分。"""
    client = FakeClient([{"nsfw_prob": 0.02, "reasoning": "正常画面"}])
    cache = FakeCache()
    score = CascadeClassifier(Config(), client=client, cache=cache).classify(make_img())

    assert score.nsfw_prob == pytest.approx(cal(0.02))
    assert score.nsfw_prob < Config().vlm_escalate_below
    assert score.scores["escalated"] is False
    assert len(client.calls) == 1
    assert cache.spends == 1
    assert len(cache.puts) == 1


def test_band_boundaries_closed_interval() -> None:
    """不确定带为闭区间:0.85 / 0.15 恰在带上要升级,0.86 / 0.14 带外直接用。"""
    cases = ((0.85, True), (0.86, False), (0.15, True), (0.14, False))
    for flash_value, expect_escalated in cases:
        seed = {(FLASH, VERSION, SHA): {"nsfw_prob": flash_value}}
        client = FakeClient([{"nsfw_prob": 0.9}] if expect_escalated else [])
        cache = FakeCache(seed=seed)
        score = CascadeClassifier(Config(), client=client, cache=cache).classify(make_img())
        assert score.scores["escalated"] is expect_escalated, flash_value
        if expect_escalated:
            assert score.nsfw_prob == pytest.approx(cal(0.9))
        else:
            assert score.nsfw_prob == pytest.approx(flash_value)


# ---------------------------------------------------------------------------
# 不确定带:升级大模型
# ---------------------------------------------------------------------------
def test_uncertainty_band_escalates_to_big_model() -> None:
    """flash 原始 0.5(校准 0.55)落入不确定带 → 升级模型 0.9,取升级校准分。"""
    client = FakeClient(
        [
            {"nsfw_prob": 0.5, "reasoning": "边缘内容"},
            {"nsfw_prob": 0.9, "reasoning": "升级复核判定色情"},
        ]
    )
    cache = FakeCache()
    score = CascadeClassifier(Config(), client=client, cache=cache).classify(make_img())

    assert score.nsfw_prob == pytest.approx(cal(0.9))
    assert score.scores["escalated"] is True
    assert score.scores["escalated_model"] == BIG
    assert score.scores["flash_prob"] == pytest.approx(cal(0.5))
    assert score.scores["model"] == BIG
    assert len(client.calls) == 2
    assert (FLASH, VERSION, SHA) in cache.store
    assert (BIG, VERSION, SHA) in cache.store  # 第二跳结果独立回写缓存


def test_two_hops_spend_two() -> None:
    """预算红线:不确定带升级一图共两次真实外呼,spend_one 必须恰好扣两次。"""
    client = FakeClient([{"nsfw_prob": 0.5}, {"nsfw_prob": 0.9}])
    cache = FakeCache()
    CascadeClassifier(Config(), client=client, cache=cache).classify(make_img())
    assert cache.spends == 2


def test_case_agent_model_preferred_over_fallback() -> None:
    """case_agent_model 非空时优先于回退链,作为升级模型与缓存命名空间。"""
    cfg = Config(case_agent_model="glm-4.6-big")
    client = FakeClient([{"nsfw_prob": 0.5}, {"nsfw_prob": 0.9}])
    cache = FakeCache()
    score = CascadeClassifier(cfg, client=client, cache=cache).classify(make_img())

    assert score.scores["escalated"] is True
    assert score.scores["escalated_model"] == "glm-4.6-big"
    assert ("glm-4.6-big", VERSION, SHA) in cache.store


def test_no_escalation_model_configured() -> None:
    """未配置升级模型(回退链无可用候选):沿用 flash 分并标注原因,不再外呼。"""
    cfg = Config(case_agent_model="", glm_models_fallback=[])
    client = FakeClient([{"nsfw_prob": 0.5}])
    cache = FakeCache()
    score = CascadeClassifier(cfg, client=client, cache=cache).classify(make_img())

    assert score.nsfw_prob == pytest.approx(cal(0.5))
    assert score.scores["escalated"] is False
    assert score.scores["reason"] == "未配置升级模型"
    assert len(client.calls) == 1
    assert cache.spends == 1


# ---------------------------------------------------------------------------
# 缓存命中:零外呼、零扣预算
# ---------------------------------------------------------------------------
def test_cache_hit_zero_calls() -> None:
    """flash 缓存命中且高置信:直接用缓存值,零调用、零扣预算、零回写。"""
    seed = {(FLASH, VERSION, SHA): {"nsfw_prob": 0.97, "reasoning": "缓存中的高置信分"}}
    client = FakeClient([])
    cache = FakeCache(seed=seed)
    score = CascadeClassifier(Config(), client=client, cache=cache).classify(make_img())

    assert score.nsfw_prob == pytest.approx(0.97)
    assert score.scores["escalated"] is False
    assert score.scores["cached"] is True
    assert client.calls == []
    assert cache.spends == 0
    assert cache.puts == []


def test_cached_flash_in_band_uses_cached_escalation() -> None:
    """缓存中的 flash 分落入不确定带时仍要升级;升级缓存也已命中则全程零调用。"""
    seed = {
        (FLASH, VERSION, SHA): {"nsfw_prob": 0.5},
        (BIG, VERSION, SHA): {"nsfw_prob": 0.9},
    }
    client = FakeClient([])
    cache = FakeCache(seed=seed)
    score = CascadeClassifier(Config(), client=client, cache=cache).classify(make_img())

    assert score.nsfw_prob == pytest.approx(0.9)
    assert score.scores["escalated"] is True
    assert score.scores["escalated_model"] == BIG
    assert client.calls == []
    assert cache.spends == 0


# ---------------------------------------------------------------------------
# 离线与预算语义透传
# ---------------------------------------------------------------------------
def test_offline_error_propagates() -> None:
    """离线(VlmOfflineError)向上透传,由 orchestrator 的成员循环跳过本成员。"""
    client = FakeClient([VlmOfflineError("GLM 视觉模型离线:未配置 glm_api_key")])
    cache = FakeCache()
    with pytest.raises(VlmOfflineError):
        CascadeClassifier(Config(), client=client, cache=cache).classify(make_img())


def test_offline_on_second_hop_propagates() -> None:
    """升级跳离线同样透传;flash 跳已扣过一次预算,第二跳扣完预算后离线。"""
    client = FakeClient([{"nsfw_prob": 0.5}, VlmOfflineError("升级跳离线")])
    cache = FakeCache()
    with pytest.raises(VlmOfflineError):
        CascadeClassifier(Config(), client=client, cache=cache).classify(make_img())
    assert cache.spends == 2
    assert len(client.calls) == 2


def test_budget_exceeded_propagates() -> None:
    """预算尽(VlmBudgetExceeded)在扣预算处直接上抛,绝不超支外呼。"""
    cache = FakeCache(fail_spend_at=1)
    client = FakeClient([])
    with pytest.raises(VlmBudgetExceeded):
        CascadeClassifier(Config(), client=client, cache=cache).classify(make_img())
    assert client.calls == []


def test_budget_exceeded_on_second_hop() -> None:
    """第二跳预算尽:flash 跳已完成(一次调用),升级跳扣预算时上抛。"""
    cache = FakeCache(fail_spend_at=2)
    client = FakeClient([{"nsfw_prob": 0.5}])
    with pytest.raises(VlmBudgetExceeded):
        CascadeClassifier(Config(), client=client, cache=cache).classify(make_img())
    assert len(client.calls) == 1
    assert cache.spends == 2


# ---------------------------------------------------------------------------
# 单跳失败降级(A21 语义:单图失败不中断整站扫描)
# ---------------------------------------------------------------------------
def test_escalation_parse_failure_falls_back_to_flash() -> None:
    """升级跳解析失败(无有效 nsfw_prob):回退 flash 校准分,垃圾返回不入缓存。"""
    client = FakeClient(
        [
            {"nsfw_prob": 0.5, "reasoning": "边缘内容"},
            {"categories": ["正常"], "reasoning": "返回里没有数值字段"},
        ]
    )
    cache = FakeCache()
    score = CascadeClassifier(Config(), client=client, cache=cache).classify(make_img())

    assert score.nsfw_prob == pytest.approx(cal(0.5))  # 回退 flash 值
    assert score.scores["escalated"] is False
    assert score.scores["error"]  # 中文错误说明
    assert len(client.calls) == 2
    assert cache.spends == 2
    assert (FLASH, VERSION, SHA) in cache.store
    assert (BIG, VERSION, SHA) not in cache.store  # 失败返回不回写缓存


def test_flash_parse_failure_degrades_to_error_score() -> None:
    """flash 跳解析失败:本张降级 0.0 + scores.error,失败结果不入缓存。"""
    client = FakeClient([{"reasoning": "没有数值字段"}])
    cache = FakeCache()
    score = CascadeClassifier(Config(), client=client, cache=cache).classify(make_img())

    assert score.nsfw_prob == 0.0
    assert score.scores["escalated"] is False
    assert score.scores["error"]
    assert cache.spends == 1
    assert cache.store == {}


def test_flash_runtime_error_degrades() -> None:
    """flash 跳非离线异常(如 HTTP 502):同样降级为 0.0 + 中文错误,不上抛。"""
    client = FakeClient([RuntimeError("GLM 接口调用失败:HTTP 502")])
    cache = FakeCache()
    score = CascadeClassifier(Config(), client=client, cache=cache).classify(make_img())
    assert score.nsfw_prob == 0.0
    assert "502" in score.scores["error"]


# ---------------------------------------------------------------------------
# 注册协议
# ---------------------------------------------------------------------------
def test_registered_as_cascade() -> None:
    """cascade 模块导入即自注册,classifier_base 工厂可按名取实例。"""
    cb = pytest.importorskip("netsentinel.vision.classifier_base")
    classifier = cb.get_classifier("cascade", Config())
    assert isinstance(classifier, CascadeClassifier)
    assert classifier.name == "cascade"


def test_default_config_escalation_model_from_fallback() -> None:
    """默认配置下升级模型 = 回退链中第一个非 flash 模型(glm-4.5v-flash)。"""
    classifier = CascadeClassifier(Config())
    assert classifier._escalation_model() == BIG


# ---------------------------------------------------------------------------
# V5 升级(A88):进程内记忆 / 遥测两级计时 / 预算快速失败锁定
# ---------------------------------------------------------------------------
class AlwaysBrokeCache(FakeCache):
    """spend_one 永远预算尽:锁定"预算尽快速失败"路径。"""

    def spend_one(self, day=None) -> None:  # noqa: ARG002
        self.spends += 1
        raise VlmBudgetExceeded(self.spends, self.spends - 1)


def test_v5_budget_exhausted_fail_fast_no_double_spend() -> None:
    """V5 健壮性:预算尽后每张图仍在扣预算处第一时间失败——零外呼、每图恰好一次尝试。

    单张图片内绝不重复扣预算(缓存未命中 → spend_one 即上抛,不进外呼、不进第二跳)。
    """
    cache = AlwaysBrokeCache()
    client = FakeClient([])
    clf = CascadeClassifier(Config(), client=client, cache=cache)
    for i in range(3):
        with pytest.raises(VlmBudgetExceeded):
            clf.classify(make_img(name=f"broke{i}.png", sha=str(i) * 64))
    assert client.calls == []  # 快速失败:从不带预算外呼
    assert cache.spends == 3   # 3 张图各扣一次即上抛(3 次尝试,非同图重复扣)


def test_v5_escalation_model_memoized_per_instance() -> None:
    """V5 性能:升级模型解析结果进程内(实例内)记忆——回退链只遍历一次。"""

    class CountingFallback(list):
        """记录自身被遍历次数的回退链。"""

        def __init__(self, items: list) -> None:
            super().__init__(items)
            self.iter_count = 0

        def __iter__(self):  # noqa: ANN204
            self.iter_count += 1
            return super().__iter__()

    chain = CountingFallback([FLASH, BIG])
    clf = CascadeClassifier(Config(case_agent_model="", glm_models_fallback=chain))
    for _ in range(5):
        assert clf._escalation_model() == BIG
    assert chain.iter_count == 1  # 5 次解析只遍历 1 次回退链(其余命中记忆)


def test_v5_lazy_helpers_memoized_per_instance(monkeypatch) -> None:
    """V5 性能:提示词版本 / 提示词对 / 校准函数每实例只做一次惰性解析。"""
    counters = {"version": 0, "prompts": 0, "calibrate": 0}
    real_version = cascade._prompt_version
    real_prompts = cascade._scoring_prompts
    real_calibrate = cascade._calibrate_fn

    def counting_version() -> str:
        counters["version"] += 1
        return real_version()

    def counting_prompts():  # noqa: ANN202
        counters["prompts"] += 1
        return real_prompts()

    def counting_calibrate():  # noqa: ANN202
        counters["calibrate"] += 1
        return real_calibrate()

    monkeypatch.setattr(cascade, "_prompt_version", counting_version)
    monkeypatch.setattr(cascade, "_scoring_prompts", counting_prompts)
    monkeypatch.setattr(cascade, "_calibrate_fn", counting_calibrate)

    client = FakeClient([{"nsfw_prob": 0.5}, {"nsfw_prob": 0.9}, {"nsfw_prob": 0.97}])
    clf = CascadeClassifier(Config(), client=client, cache=FakeCache())
    clf.classify(make_img(name="a.png", sha="a" * 64))  # 升级路径:三个惰性依赖全触达
    clf.classify(make_img(name="b.png", sha="b" * 64))  # 高置信路径:全部命中记忆
    assert counters == {"version": 1, "prompts": 1, "calibrate": 1}


def test_v5_telemetry_flash_high_confidence_counters() -> None:
    """V5 可观测:高置信单跳 → timer("cascade.flash") 一条,无升级/错误计数。"""
    telemetry.reset()
    client = FakeClient([{"nsfw_prob": 0.97}])
    CascadeClassifier(Config(), client=client, cache=FakeCache()).classify(make_img())
    snap = telemetry.snapshot()
    assert snap["timers"]["cascade.flash"]["count"] == 1
    assert snap["timers"].get("cascade.escalated", {}).get("count", 0) == 0
    assert snap["counters"].get("cascade.escalations", 0) == 0
    assert snap["counters"].get("cascade.errors", 0) == 0


def test_v5_telemetry_escalation_counters() -> None:
    """V5 可观测:不确定带升级 → 两级 timer 各一 + inc("cascade.escalations")=1。"""
    telemetry.reset()
    client = FakeClient([{"nsfw_prob": 0.5}, {"nsfw_prob": 0.9}])
    CascadeClassifier(Config(), client=client, cache=FakeCache()).classify(make_img())
    snap = telemetry.snapshot()
    assert snap["timers"]["cascade.flash"]["count"] == 1
    assert snap["timers"]["cascade.escalated"]["count"] == 1
    assert snap["counters"]["cascade.escalations"] == 1
    assert snap["counters"].get("cascade.errors", 0) == 0


def test_v5_telemetry_error_counter_on_flash_failure() -> None:
    """V5 可观测:flash 跳解析失败 → inc("cascade.errors"),flash timer 仍计时。"""
    telemetry.reset()
    client = FakeClient([{"reasoning": "没有数值字段"}])
    score = CascadeClassifier(
        Config(), client=client, cache=FakeCache()
    ).classify(make_img())
    assert score.nsfw_prob == 0.0
    snap = telemetry.snapshot()
    assert snap["counters"]["cascade.errors"] == 1
    assert snap["timers"]["cascade.flash"]["count"] == 1
    assert snap["timers"].get("cascade.escalated", {}).get("count", 0) == 0


# ---------------------------------------------------------------------------
# V10 升级(A197):FrugalGPT 式自适应级联不确定带
#
# 固定构造的历史夹具(零随机源,确定性断言):
# - _no_gain_history():高端模型对中分段几乎无增益 → 自适应带应缩窄、成本下降;
# - _mono_history():风险质量分布在带内不同深度 → 带宽随风险预算单调变化;
# - _cap_history():全部风险质量集中于正中 → 无约束最优极窄,限幅窗口生效。
# ---------------------------------------------------------------------------
STATIC_BAND = (Config().vlm_escalate_below, Config().vlm_escalate_above)  # (0.15, 0.85)


def _rec(score: float, cost: float, escalated: bool, correct: bool | None = None) -> dict:
    """构造一条 A197 历史记录(score 为 flash 校准分;correct 可缺省)。"""
    record = {"score": score, "cost": cost, "escalated": escalated}
    if correct is not None:
        record["correct"] = correct
    return record


def _no_gain_history() -> list[dict]:
    """高端模型对中分段几乎无增益:带边缘 40 条无增益,正中 10 条半数判错。

    正中 10 条标注 5 对 5 错 → 升级风险拉普拉斯估计 (5+1)/(10+2) = 0.5;
    边缘条目不确定度代理 1-|2s-1| ≤ 0.42 < 0.5 → 升级零增益,应被裁出带宽。
    """
    edge = [0.16, 0.19, 0.21, 0.79, 0.81, 0.84]
    history = [_rec(s, 5.0, True) for s in (edge * 7)[:40]]
    history += [_rec(0.5, 5.0, True, correct=(i % 2 == 0)) for i in range(10)]
    history += [_rec(s, 1.0, False) for s in ([0.03, 0.05, 0.95, 0.97] * 8)[:30]]
    return history


def _mono_history() -> list[dict]:
    """风险质量分层:0.20/0.24(带内深部)与 0.50(正中,标注出升级风险 0.3)。"""
    history = [_rec(0.20, 5.0, True) for _ in range(12)]
    history += [_rec(0.24, 5.0, True) for _ in range(12)]
    history += [_rec(0.50, 5.0, True, correct=(i < 6)) for i in range(8)]
    history += [_rec(s, 1.0, False) for s in ([0.02, 0.04, 0.96, 0.98] * 5)[:20]]
    return history


def _cap_history() -> list[dict]:
    """全部风险质量集中于正中(0.5),近中段 0.40/0.60 升级零增益。

    无约束最优带宽 = 0.0875(只罩正中);默认 ±20% 限幅 → 收缩止步于 0.28。
    """
    history = [_rec(0.5, 5.0, True, correct=(i % 2 == 0)) for i in range(10)]
    history += [_rec(s, 5.0, True) for s in ([0.40, 0.60] * 15)[:30]]
    history += [_rec(s, 1.0, False) for s in ([0.03, 0.97] * 15)[:30]]
    return history


# ---------------------------------------------------------------------------
# 冷启动与兼容(字节级:返回静态带缺省本身)
# ---------------------------------------------------------------------------
def test_a197_cold_start_returns_static_default_byte_level() -> None:
    """样本不足(< 20):任何风险预算都原样返回 static_default(同一对象)。"""
    short = _no_gain_history()[: cascade.ADAPTIVE_MIN_SAMPLES - 1]
    assert len(short) < cascade.ADAPTIVE_MIN_SAMPLES
    static_default = (0.15, 0.85)
    for budget in (0.0, 0.3, 0.9, 1.0):
        result = cascade.band_from_history(
            short, risk_budget=budget, static_default=static_default
        )
        assert result is static_default  # 字节级:同一对象,非等值新元组


def test_a197_cold_start_garbage_records_sanitized() -> None:
    """非法条目(非 dict / 缺 score / score 非 number)剔除后不足下限 → 静态带。"""
    history = [
        {"score": 0.5, "cost": 5.0, "escalated": True},  # 有效
        "not-a-dict",
        {"cost": 5.0, "escalated": True},  # 缺 score
        {"score": "high", "cost": 5.0, "escalated": True},  # score 不可数值化
        {"score": True, "cost": 5.0, "escalated": True},  # bool 不当 number
    ]
    static_default = (0.15, 0.85)
    assert cascade.band_from_history(
        history, risk_budget=0.9, static_default=static_default
    ) is static_default
    # 非列表历史(None / dict)同样按冷启动处理
    assert cascade.band_from_history(
        None, risk_budget=0.9, static_default=static_default
    ) is static_default


def test_a197_no_risk_budget_returns_static() -> None:
    """risk_budget 未设(None):历史再充足也返回静态带(与现状完全一致)。"""
    static_default = (0.15, 0.85)
    assert cascade.band_from_history(
        _no_gain_history(), risk_budget=None, static_default=static_default
    ) is static_default
    # 非法预算(bool / NaN)同样视为未设
    assert cascade.band_from_history(
        _no_gain_history(), risk_budget=True, static_default=static_default
    ) is static_default
    assert cascade.band_from_history(
        _no_gain_history(), risk_budget=float("nan"), static_default=static_default
    ) is static_default


def test_a197_invalid_static_default_passthrough() -> None:
    """静态带本身不合法(below ≥ above):原样返回,不替调用方猜。"""
    bad = (0.8, 0.2)
    assert cascade.band_from_history(
        _no_gain_history(), risk_budget=0.9, static_default=bad
    ) is bad


# ---------------------------------------------------------------------------
# 单调性:风险预算收紧 → 带宽单调收缩;预算放大 → 带宽不缩
# ---------------------------------------------------------------------------
def test_a197_budget_tightening_shrinks_band_monotonically() -> None:
    """预算收紧带宽收缩、预算放宽带宽不缩:宽度随 risk_budget 单调不降。"""
    budgets = [0.2, 0.4, 0.5, 0.7, 0.9, 1.0]
    bands = [
        cascade.band_from_history(
            _mono_history(), risk_budget=b, static_default=STATIC_BAND
        )
        for b in budgets
    ]
    widths = [above - below for below, above in bands]
    # 风险预算↑ → 带宽不缩(单调不降);等价地,预算收紧 → 带宽单调收缩
    assert all(widths[i] <= widths[i + 1] + 1e-9 for i in range(len(widths) - 1))
    assert widths[0] < widths[-1]  # 存在严格档:收紧 1.0 → 0.2 带宽实际收缩
    assert widths[0] < 0.85 - 0.15  # 收紧后严格窄于静态带
    # 具体档位(确定性:固定夹具 → 固定带宽)
    assert bands[0] == pytest.approx((0.22, 0.78))
    assert bands[3] == pytest.approx((0.2025, 0.7975))
    assert bands[5] == pytest.approx(STATIC_BAND)  # 预算 1.0 = 买回全部可化解风险


def test_a197_never_widens_beyond_static_band() -> None:
    """只收不放:即便预算极松、大模型零风险(无标注),带宽也不超过静态带。"""
    unlabeled = [_rec(s, 5.0, True) for s in [0.16, 0.2, 0.3, 0.5, 0.7, 0.8, 0.84] * 5]
    unlabeled += [_rec(s, 1.0, False) for s in [0.02, 0.97] * 5]
    for budget in (0.5, 0.9, 1.0):
        band = cascade.band_from_history(
            unlabeled, risk_budget=budget, static_default=STATIC_BAND
        )
        assert band[0] >= STATIC_BAND[0] - 1e-9
        assert band[1] <= STATIC_BAND[1] + 1e-9


# ---------------------------------------------------------------------------
# 防过拟合:带宽每轮相对上界限幅(±20%),可逐轮复合
# ---------------------------------------------------------------------------
def test_a197_step_cap_limits_shrink_per_round() -> None:
    """单轮收缩至多 20%:无约束最优 0.0875 被钳到 0.28(静态宽 × 0.8)。"""
    history = _cap_history()
    # 默认 ±20%:带宽 (0.22, 0.78) = 静态带宽收缩恰好 20%
    assert cascade.band_from_history(
        history, risk_budget=0.1, static_default=STATIC_BAND
    ) == pytest.approx((0.22, 0.78))
    # 放宽限幅到 ±50%:钳到 0.175 → (0.325, 0.675)
    assert cascade.band_from_history(
        history, risk_budget=0.1, static_default=STATIC_BAND, max_step=0.5
    ) == pytest.approx((0.325, 0.675))
    # 不限幅(max_step=1.0):无约束最优 = 0.0875 → (0.4125, 0.5875),证明限幅确实生效
    assert cascade.band_from_history(
        history, risk_budget=0.1, static_default=STATIC_BAND, max_step=1.0
    ) == pytest.approx((0.4125, 0.5875))


def test_a197_step_cap_compounds_across_rounds() -> None:
    """多轮复合:以上一轮 (0.325, 0.675) 为上界,再收 20% → (0.36, 0.64)。"""
    history = _cap_history()
    assert cascade.band_from_history(
        history,
        risk_budget=0.1,
        static_default=STATIC_BAND,
        previous=(0.325, 0.675),
    ) == pytest.approx((0.36, 0.64))


# ---------------------------------------------------------------------------
# 构造性成本验证:高端模型对中分段几乎无增益 → 带宽缩窄、期望成本显著下降
# ---------------------------------------------------------------------------
def test_a197_no_gain_history_cuts_band_and_saves_cost() -> None:
    """无增益历史 → 自适应带缩窄,期望成本下降 ≥ 30%(实际约 57%)。"""
    history = _no_gain_history()
    band = cascade.band_from_history(
        history, risk_budget=0.9, static_default=STATIC_BAND
    )
    # 带宽严格缩窄(裁掉零增益的边缘段,保留正中有增益段)
    assert band[0] > STATIC_BAND[0]
    assert band[1] < STATIC_BAND[1]
    assert band == pytest.approx((0.22, 0.78))

    # 期望成本量化:静态带 3.5/条 → 自适应 1.5/条,节省 ≥ 30%
    cost_static = cascade.expected_band_cost(history, *STATIC_BAND)
    cost_adaptive = cascade.expected_band_cost(history, *band)
    assert cost_static == pytest.approx(3.5)
    assert cost_adaptive == pytest.approx(1.5)
    saving = (cost_static - cost_adaptive) / cost_static
    assert saving >= 0.30  # 实际 ≈ 0.571,满足"成本 ↓30% 量级"目标

    # 升级次数口径:落带(需升级复核)条目 50 → 10(−80%)
    n_static = sum(1 for r in history if STATIC_BAND[0] <= r["score"] <= STATIC_BAND[1])
    n_adaptive = sum(1 for r in history if band[0] <= r["score"] <= band[1])
    assert (n_static, n_adaptive) == (50, 10)


def test_a197_deterministic_same_history_same_output() -> None:
    """确定性:同一历史重复推导,输出逐字节一致(无随机源)。"""
    history = _mono_history()
    results = [
        cascade.band_from_history(history, risk_budget=0.7, static_default=STATIC_BAND)
        for _ in range(3)
    ]
    assert results[0] == results[1] == results[2]
    assert repr(results[0]) == repr(results[1])


# ---------------------------------------------------------------------------
# 集成:cascade 主流程 adaptive_band 参数(缺省 None = 静态带现状)
# ---------------------------------------------------------------------------
def test_a197_default_constructor_static_band_and_telemetry() -> None:
    """缺省构造:带宽 = cfg 静态带浮点值本身,遥测计 cascade.band.static。"""
    telemetry.reset()
    cfg = Config()
    clf = CascadeClassifier(cfg, client=FakeClient([]), cache=FakeCache())
    assert clf._band == (float(cfg.vlm_escalate_below), float(cfg.vlm_escalate_above))
    snap = telemetry.snapshot()
    assert snap["counters"].get("cascade.band.static", 0) == 1
    assert snap["counters"].get("cascade.band.adaptive", 0) == 0


def test_a197_adaptive_band_changes_routing() -> None:
    """自适应带生效:flash 0.21 在静态带内但不在自适应带 (0.22, 0.78) 内 → 不升级。"""
    seed = {(FLASH, VERSION, SHA): {"nsfw_prob": 0.21}}

    # 静态带现状:0.21 ∈ [0.15, 0.85] → 升级大模型
    static_client = FakeClient([{"nsfw_prob": 0.9}])
    static_score = CascadeClassifier(
        Config(), client=static_client, cache=FakeCache(seed=seed)
    ).classify(make_img())
    assert static_score.scores["escalated"] is True
    assert static_score.nsfw_prob == pytest.approx(cal(0.9))

    # 自适应带:0.21 < 0.22 → 高置信直出,零外呼、零扣预算
    telemetry.reset()
    both_seeded = {
        (FLASH, VERSION, SHA): {"nsfw_prob": 0.21},
        (FLASH, VERSION, "b" * 64): {"nsfw_prob": 0.5},
    }
    adaptive_client = FakeClient([{"nsfw_prob": 0.9}])
    adaptive_cache = FakeCache(seed=both_seeded)
    clf = CascadeClassifier(
        Config(),
        client=adaptive_client,
        cache=adaptive_cache,
        adaptive_band=cascade.make_adaptive_band(_no_gain_history(), 0.9),
    )
    adaptive_score = clf.classify(make_img())
    assert clf._band == pytest.approx((0.22, 0.78))
    assert adaptive_score.scores["escalated"] is False
    assert adaptive_score.nsfw_prob == pytest.approx(0.21)
    assert adaptive_client.calls == []
    assert adaptive_cache.spends == 0
    assert telemetry.snapshot()["counters"].get("cascade.band.adaptive", 0) == 1

    # 带内(0.5)仍正常升级:自适应带只是缩窄,不是关闸
    score2 = clf.classify(make_img(name="mid.png", sha="b" * 64))
    assert score2.scores["escalated"] is True
    assert score2.nsfw_prob == pytest.approx(cal(0.9))
    assert len(adaptive_client.calls) == 1
    assert adaptive_cache.spends == 1  # flash 命中缓存,仅升级跳扣一次预算


def test_a197_adaptive_band_tuple_override() -> None:
    """元组形态:直接生效为 (0.3, 0.7);0.25 落带内升级、0.8 带外直出。"""
    cfg = Config()
    clf = CascadeClassifier(
        cfg, client=FakeClient([]), cache=FakeCache(), adaptive_band=(0.3, 0.7)
    )
    assert clf._band == (0.3, 0.7)

    in_band = {(FLASH, VERSION, SHA): {"nsfw_prob": 0.5}}
    client = FakeClient([{"nsfw_prob": 0.9}])
    score = CascadeClassifier(
        cfg, client=client, cache=FakeCache(seed=in_band), adaptive_band=(0.3, 0.7)
    ).classify(make_img())
    assert score.scores["escalated"] is True

    out_of_band = {(FLASH, VERSION, SHA): {"nsfw_prob": 0.8}}
    client2 = FakeClient([])
    score2 = CascadeClassifier(
        cfg, client=client2, cache=FakeCache(seed=out_of_band), adaptive_band=(0.3, 0.7)
    ).classify(make_img())
    assert score2.scores["escalated"] is False
    assert score2.nsfw_prob == pytest.approx(0.8)


def test_a197_broken_factory_falls_back_to_static() -> None:
    """工厂异常 / 返回非法带宽:告警回退静态带,分类不受阻断。"""
    telemetry.reset()

    def boom(**_ctx):  # noqa: ANN003,ANN202
        raise RuntimeError("工厂离线")

    for bad_factory in (boom, lambda **_ctx: "wide", lambda **_ctx: (0.9, 0.1)):
        client = FakeClient([{"nsfw_prob": 0.9}])
        clf = CascadeClassifier(
            Config(),
            client=client,
            cache=FakeCache(seed={(FLASH, VERSION, SHA): {"nsfw_prob": 0.5}}),
            adaptive_band=bad_factory,
        )
        assert clf._band == STATIC_BAND  # 回退静态带
        score = clf.classify(make_img())  # 分类照常:0.5 落带内 → 升级
        assert score.scores["escalated"] is True
    snap = telemetry.snapshot()
    assert snap["counters"].get("cascade.band.static", 0) == 3
    assert snap["counters"].get("cascade.band.adaptive", 0) == 0


def test_a197_factory_receives_context() -> None:
    """工厂以关键字上下文调用:static_default / history / risk_budget 齐备。"""
    seen: dict = {}

    def spy(**ctx):  # noqa: ANN003,ANN202
        seen.update(ctx)
        return (0.2, 0.8)

    history = _no_gain_history()
    clf = CascadeClassifier(
        Config(),
        client=FakeClient([]),
        cache=FakeCache(),
        adaptive_band=spy,
        band_history=history,
        risk_budget=0.6,
    )
    assert seen["static_default"] == STATIC_BAND
    assert seen["history"] == history
    assert seen["risk_budget"] == 0.6
    assert clf._band == (0.2, 0.8)


def test_a197_true_flag_with_history_and_cfg_budget() -> None:
    """adaptive_band=True:走 band_from_history;预算可取 cfg.cascade_risk_budget。"""
    history = _no_gain_history()

    # 显式 risk_budget 优先
    clf = CascadeClassifier(
        Config(),
        client=FakeClient([]),
        cache=FakeCache(),
        adaptive_band=True,
        band_history=history,
        risk_budget=0.9,
    )
    assert clf._band == pytest.approx((0.22, 0.78))

    # 未显式给预算 → 取 cfg.cascade_risk_budget(契约暂无该字段,动态属性预留)
    cfg = Config()
    cfg.cascade_risk_budget = 0.9
    clf2 = CascadeClassifier(
        cfg,
        client=FakeClient([]),
        cache=FakeCache(),
        adaptive_band=True,
        band_history=history,
    )
    assert clf2._band == pytest.approx((0.22, 0.78))

    # 历史不足 → 冷启动静态带,遥测归入 static
    telemetry.reset()
    clf3 = CascadeClassifier(
        Config(),
        client=FakeClient([]),
        cache=FakeCache(),
        adaptive_band=True,
        band_history=history[:5],
        risk_budget=0.9,
    )
    assert clf3._band == STATIC_BAND
    snap = telemetry.snapshot()
    assert snap["counters"].get("cascade.band.static", 0) == 1
    assert snap["counters"].get("cascade.band.adaptive", 0) == 0


def test_a197_unsupported_adaptive_band_form_warns_static() -> None:
    """不支持的参数形态(如字符串):告警回退静态带。"""
    telemetry.reset()
    clf = CascadeClassifier(
        Config(),
        client=FakeClient([]),
        cache=FakeCache(),
        adaptive_band="wide",  # type: ignore[arg-type]
    )
    assert clf._band == STATIC_BAND
    assert telemetry.snapshot()["counters"].get("cascade.band.static", 0) == 1


def test_a197_expected_band_cost_empty_history() -> None:
    """expected_band_cost 纯函数:空历史 → 0.0(口径兜底)。"""
    assert cascade.expected_band_cost([], 0.15, 0.85) == 0.0
