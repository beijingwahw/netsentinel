"""tests/test_failover.py —— A68 FailoverClassifier(跨平台故障转移路由)单元测试。

离线:全部用可注入的 fake builders 构造假成员,零外呼、零真实密钥、零网络;
不依赖 multi_provider(A63)是否就位——配置类异常在 vlm_client 就位时用真实
``VlmConfigError``/``ModelNotFoundError``,未就位时用 failover 本地兜底类型。
"""
from __future__ import annotations

from collections import defaultdict

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, ImageScore
from netsentinel.vision import failover as failover_module
from netsentinel.vision.failover import FailoverClassifier

classifier_base = pytest.importorskip("netsentinel.vision.classifier_base")


# ---------------------------------------------------------------------------
# 测试工具
# ---------------------------------------------------------------------------

def make_evidence(path: str) -> ImageEvidence:
    """构造无需真实落盘文件的图片证据。"""
    return ImageEvidence(
        path=path,
        url=f"https://example.invalid/{path}",
        source_page="https://example.invalid/index.html",
        sha256="0" * 64,
        width=800,
        height=600,
    )


def make_config(chain: list[str] | None = None) -> Config:
    """构造带故障转移链的配置。"""
    return Config(vlm_fallback_chain=list(chain or []))


def config_error(message: str, *, kind: str = "config") -> Exception:
    """构造配置类异常:A62 就位时用真实 VlmConfigError/ModelNotFoundError,否则本地兜底。"""
    try:
        from netsentinel.vision.vlm_client import ModelNotFoundError, VlmConfigError
    except ImportError:  # pragma: no cover - 仅并行开发期 vlm_client 未就位
        return failover_module._ChainConfigError(message)
    return (VlmConfigError if kind == "config" else ModelNotFoundError)(message)


class FakeMember(classifier_base.NsfwClassifier):
    """可编程假成员:按预设抛异常 / 返回带内错误分 / 返回成功分,并计数。"""

    def __init__(
        self,
        name: str,
        *,
        prob: float = 0.5,
        exc: Exception | None = None,
        error_score: str | None = None,
    ) -> None:
        self.name = name
        self.prob = prob
        self.exc = exc
        self.error_score = error_score
        self.classify_calls = 0

    def classify(self, img: ImageEvidence) -> ImageScore:
        self.classify_calls += 1
        if self.exc is not None:
            raise self.exc
        scores: dict = {
            "categories": ["normal"],
            "reasoning": f"{self.name} 的判定理由",
            "confidence": 0.9,
            "vlm_model": self.name,
        }
        if self.error_score is not None:
            scores["error"] = self.error_score
        return ImageScore(image=img, model=self.name, nsfw_prob=self.prob, scores=scores)


def make_failover(
    members: list,
    *,
    specs: list[str] | None = None,
    clock=None,
    circuit_breaker: bool = True,
):
    """按成员序构造 FailoverClassifier(spec 缺省 m0/m1/...),并返回构建计数表。

    V5:clock / circuit_breaker 透传给 FailoverClassifier(断路器测试注入假时钟)。
    """
    spec_list = specs if specs is not None else [f"s{i}" for i in range(len(members))]
    build_counts: dict[str, int] = defaultdict(int)

    def wrap(spec: str, member):
        def build():
            build_counts[spec] += 1
            return member

        return build

    cfg = make_config(spec_list)
    kwargs: dict = {"circuit_breaker": circuit_breaker}
    if clock is not None:
        kwargs["clock"] = clock
    fc = FailoverClassifier(
        cfg,
        builders={spec: wrap(spec, member) for spec, member in zip(spec_list, members)},
        **kwargs,
    )
    return fc, build_counts


# ---------------------------------------------------------------------------
# 链配置与构造
# ---------------------------------------------------------------------------

def test_empty_chain_from_cfg_raises_value_error() -> None:
    """空链(cfg.vlm_fallback_chain 为空)→ ValueError 中文,提示如何填写。"""
    with pytest.raises(ValueError, match="故障转移链为空.*vlm_fallback_chain"):
        FailoverClassifier(make_config())


def test_empty_chain_explicit_specs_also_raises() -> None:
    """显式传入 specs=[] 同样视为空链 → ValueError。"""
    with pytest.raises(ValueError, match="故障转移链为空"):
        FailoverClassifier(make_config(["glm:glm-5.3-flash"]), specs=[])


def test_invalid_spec_in_chain_raises_value_error() -> None:
    """链中混入空串/非字符串 spec → ValueError 中文。"""
    with pytest.raises(ValueError, match="无效成员 spec"):
        FailoverClassifier(make_config(["glm:glm-5.3-flash", "   "]))
    with pytest.raises(ValueError, match="无效成员 spec"):
        FailoverClassifier(make_config([123]))  # type: ignore[list-item]


def test_specs_param_overrides_cfg_chain() -> None:
    """specs 参数优先于 cfg.vlm_fallback_chain。"""
    member = FakeMember("only", prob=0.2)
    fc = FailoverClassifier(
        make_config(["b:y", "c:z"]),
        builders={"a:x": lambda: member},
        specs=["a:x"],
    )
    assert fc.specs == ["a:x"]
    assert fc.classify(make_evidence("i.jpg")).model == "failover→only"


def test_duplicate_specs_deduplicated() -> None:
    """重复成员按序去重,不重复尝试。"""
    member = FakeMember("dup", prob=0.3)
    build_counts: dict[str, int] = defaultdict(int)

    def build() -> FakeMember:
        build_counts["dup:x"] += 1
        return member

    fc = FailoverClassifier(
        make_config(["dup:x", "dup:x"]), builders={"dup:x": build}
    )
    assert fc.specs == ["dup:x"]
    fc.classify(make_evidence("i.jpg"))
    assert build_counts["dup:x"] == 1


# ---------------------------------------------------------------------------
# classify:转移与返回
# ---------------------------------------------------------------------------

def test_first_member_fails_second_succeeds() -> None:
    """第一成员抛 RuntimeError,第二成员成功 → model 带 fallback_from 标注。"""
    m1 = FakeMember("glm:glm-5.3-flash", exc=RuntimeError("连接超时"))
    m2 = FakeMember("openai:gpt-4o-mini", prob=0.93)
    fc, _ = make_failover([m1, m2])
    img = make_evidence("pic.jpg")

    result = fc.classify(img)

    assert result.model == "failover→openai:gpt-4o-mini"
    assert result.nsfw_prob == pytest.approx(0.93)
    assert result.scores["fallback_from"] == ["glm:glm-5.3-flash"]
    assert result.scores["categories"] == ["normal"]      # 原 scores 透传
    assert result.scores["vlm_model"] == "openai:gpt-4o-mini"
    assert m1.classify_calls == 1 and m2.classify_calls == 1


def test_image_score_fields_pass_through() -> None:
    """ImageScore 字段透传:image 对象、nsfw_prob、原 scores 全部保留。"""
    m = FakeMember("winner", prob=0.77)
    fc, _ = make_failover([m])
    img = make_evidence("pass.jpg")

    result = fc.classify(img)

    assert result.image is img
    assert isinstance(result, ImageScore)
    assert result.nsfw_prob == pytest.approx(0.77)
    assert result.scores["reasoning"] == "winner 的判定理由"
    assert result.scores["confidence"] == 0.9


def test_first_success_yields_empty_fallback_from() -> None:
    """首位成员即成功 → fallback_from 为空列表。"""
    fc, _ = make_failover([FakeMember("m1"), FakeMember("m2")])
    result = fc.classify(make_evidence("i.jpg"))
    assert result.model == "failover→m1"
    assert result.scores["fallback_from"] == []


def test_all_members_fail_raises_last_non_config() -> None:
    """全失败 → 上抛最后一个非配置异常。"""
    m1 = FakeMember("m1", exc=RuntimeError("第一次失败"))
    m2 = FakeMember("m2", exc=ValueError("第二次失败"))
    fc, _ = make_failover([m1, m2])
    with pytest.raises(ValueError, match="第二次失败"):
        fc.classify(make_evidence("i.jpg"))


def test_all_config_errors_raise_config_summary() -> None:
    """全部成员都是配置类错误 → 抛 VlmConfigError 语义的中文汇总。"""
    m1 = FakeMember("glm:glm-5.3-flash", exc=config_error("glm 密钥未配置"))
    m2 = FakeMember("qwen:qwen-vl-max", exc=config_error("qwen 密钥未配置", kind="notfound"))
    fc, _ = make_failover([m1, m2])

    with pytest.raises(RuntimeError) as excinfo:
        fc.classify(make_evidence("i.jpg"))

    assert type(excinfo.value).__name__ in ("VlmConfigError", "_ChainConfigError")
    message = str(excinfo.value)
    assert "配置不可用" in message
    assert "glm:glm-5.3-flash" in message and "qwen:qwen-vl-max" in message
    assert "vlm_online" in message and "vlm_api_keys" in message  # 指引查什么


def test_config_error_skipped_not_raised() -> None:
    """配置类异常只跳过不上抛:第一成员配置错、第二成员正常 → 正常返回。"""
    m1 = FakeMember("glm:glm-5.3-flash", exc=config_error("vlm_online 未开启"))
    m2 = FakeMember("ollama:llava", prob=0.66)
    fc, _ = make_failover([m1, m2])

    result = fc.classify(make_evidence("i.jpg"))

    assert result.model == "failover→ollama:llava"
    assert result.scores["fallback_from"] == ["glm:glm-5.3-flash"]


def test_mixed_config_and_runtime_raises_runtime_error() -> None:
    """配置错 + 运行错混排 → 链尽后上抛最后一个非配置异常(配置错不顶替)。"""
    m1 = FakeMember("m1", exc=config_error("密钥缺失"))
    m2 = FakeMember("m2", exc=RuntimeError("网络中断"))
    fc, _ = make_failover([m1, m2])
    with pytest.raises(RuntimeError, match="网络中断"):
        fc.classify(make_evidence("i.jpg"))


def test_model_not_found_treated_as_config_skip() -> None:
    """ModelNotFoundError(模型不存在)同样按配置类跳过,不触发上抛。"""
    m1 = FakeMember("m1", exc=config_error("模型 gpt-x 不存在", kind="notfound"))
    m2 = FakeMember("m2", prob=0.55)
    fc, _ = make_failover([m1, m2])
    result = fc.classify(make_evidence("i.jpg"))
    assert result.model == "failover→m2"
    assert result.scores["fallback_from"] == ["m1"]


def test_inband_error_score_skips_to_next_member() -> None:
    """成员返回带 scores.error 的降级分 → 视为失败,转移到下一成员。"""
    m1 = FakeMember("m1", prob=0.0, error_score="GLM 识别失败:超时")
    m2 = FakeMember("m2", prob=0.88)
    fc, _ = make_failover([m1, m2])

    result = fc.classify(make_evidence("i.jpg"))

    assert result.model == "failover→m2"
    assert result.scores["fallback_from"] == ["m1"]
    assert "error" not in result.scores  # 胜者的 scores 不带 error


def test_all_inband_errors_raise_runtime_summary() -> None:
    """全部成员都返回带内错误分(无异常)→ 链尽抛 RuntimeError 中文汇总。"""
    m1 = FakeMember("m1", error_score="失败一")
    m2 = FakeMember("m2", error_score="失败二")
    fc, _ = make_failover([m1, m2])
    with pytest.raises(RuntimeError, match="全部成员识别失败.*m2:失败二"):
        fc.classify(make_evidence("i.jpg"))


def test_custom_config_exception_types_via_monkeypatch(monkeypatch) -> None:
    """monkeypatch _CONFIG_EXC_TYPES 可扩展配置类异常的识别范围。"""
    class CustomCfgError(RuntimeError):
        pass

    monkeypatch.setattr(failover_module, "_CONFIG_EXC_TYPES", (CustomCfgError,))
    m1 = FakeMember("m1", exc=CustomCfgError("自定义配置错"))
    m2 = FakeMember("m2", prob=0.4)
    fc, _ = make_failover([m1, m2])

    result = fc.classify(make_evidence("i.jpg"))

    assert result.model == "failover→m2"
    assert result.scores["fallback_from"] == ["m1"]


# ---------------------------------------------------------------------------
# 调用计数与成员复用
# ---------------------------------------------------------------------------

def test_no_calls_beyond_first_success() -> None:
    """成功成员之后的成员不再构建、不再调用(计数验证)。"""
    m1 = FakeMember("m1", prob=0.9)
    m2 = FakeMember("m2", prob=0.5)
    m3 = FakeMember("m3", prob=0.5)
    fc, build_counts = make_failover([m1, m2, m3])

    result = fc.classify(make_evidence("i.jpg"))

    assert result.model == "failover→m1"
    assert m1.classify_calls == 1
    assert m2.classify_calls == 0 and m3.classify_calls == 0
    assert build_counts["s1"] == 0 and build_counts["s2"] == 0  # 构建器都未触发


def test_member_instance_built_once_and_reused() -> None:
    """成员实例只构建一次并跨图复用(评分缓存由成员自理,这里只验证不重复 build)。"""
    m1 = FakeMember("m1", prob=0.8)
    fc, build_counts = make_failover([m1])

    fc.classify(make_evidence("a.jpg"))
    fc.classify(make_evidence("b.jpg"))

    assert build_counts["s0"] == 1
    assert m1.classify_calls == 2


# ---------------------------------------------------------------------------
# classify_batch 语义与注册表
# ---------------------------------------------------------------------------

def test_classify_batch_allows_different_members_per_image() -> None:
    """classify_batch 逐图走 classify:批内不同图允许落到不同成员。"""
    flaky = FakeMember("flaky")  # 第一张抛异常,第二张成功(模拟瞬时故障恢复)
    original_classify = flaky.classify
    state = {"failed_once": False}

    def flaky_classify(img: ImageEvidence) -> ImageScore:
        if not state["failed_once"]:
            state["failed_once"] = True
            raise RuntimeError("瞬时故障")
        return original_classify(img)

    flaky.classify = flaky_classify  # type: ignore[method-assign]
    backup = FakeMember("backup", prob=0.31)
    fc, _ = make_failover([flaky, backup])

    results = fc.classify_batch([make_evidence("x.jpg"), make_evidence("y.jpg")])

    assert len(results) == 2
    assert results[0].model == "failover→backup"
    assert results[0].scores["fallback_from"] == ["flaky"]
    assert results[1].model == "failover→flaky"          # 恢复后回到主成员
    assert results[1].scores["fallback_from"] == []


def test_registered_name_resolvable_via_factory() -> None:
    """导入 failover 模块后,注册名 "failover" 可经工厂获取。"""
    import importlib

    importlib.import_module("netsentinel.vision.failover")
    cfg = make_config(["glm:glm-5.3-flash", "ollama:llava"])
    instance = classifier_base.get_classifier("failover", cfg)
    assert isinstance(instance, FailoverClassifier)
    assert instance.name == "failover"
    assert instance.specs == ["glm:glm-5.3-flash", "ollama:llava"]


def test_default_builder_missing_multi_provider_raises_chinese(monkeypatch) -> None:
    """缺省构建器在 multi_provider 未就位时抛中文 RuntimeError(按成员失败转移)。

    隔离口径(A63 集成经验):``from netsentinel.vision import multi_provider``
    走父包属性解析,仅 patch ``builtins.__import__`` / ``sys.modules.pop`` 无法
    阻断真实模块加载;必须同时 ``sys.modules`` 置 None **并** 删除父包属性。
    """
    import sys

    import netsentinel.vision as vision_pkg

    monkeypatch.setitem(sys.modules, "netsentinel.vision.multi_provider", None)
    monkeypatch.delattr(vision_pkg, "multi_provider", raising=False)

    fc = FailoverClassifier(make_config(["glm:glm-5.3-flash"]))
    with pytest.raises(RuntimeError, match="multi_provider 未就位"):
        fc.classify(make_evidence("i.jpg"))


# ---------------------------------------------------------------------------
# V5 升级(A88):断路器(连续 3 败 → 熔断 30s → 半开;时钟注入)+ 遥测
# ---------------------------------------------------------------------------
class ScriptedMember(classifier_base.NsfwClassifier):
    """V5 断路器测试用可编程成员:按脚本回放,脚本耗尽后重复最后一项。

    脚本元素:BaseException → 抛出;str → 带内错误分(scores.error=str);
    数值 → 成功分;dict → 原样作为 scores(nsfw_prob 缺省 0.5)。
    """

    def __init__(self, name: str, script: list) -> None:
        self.name = name
        self._script = list(script)
        self.calls = 0

    def classify(self, img: ImageEvidence) -> ImageScore:
        self.calls += 1
        item = self._script[0]
        if len(self._script) > 1:
            self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, str):
            item = {"nsfw_prob": 0.0, "error": item}
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            item = {"nsfw_prob": float(item)}
        data = dict(item)
        return ImageScore(
            image=img,
            model=self.name,
            nsfw_prob=float(data.get("nsfw_prob", 0.5)),
            scores=data,
        )


class FakeClock:
    """可推进的假时钟(单调秒),供断路器冷却窗口测试注入。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def test_v5_circuit_breaker_unit_state_machine() -> None:
    """V5 单元:closed →(连续 3 败)open 30s → half_open → 成功复位 / 失败再 open。"""
    clock = FakeClock()
    breaker = failover_module.CircuitBreaker(clock=clock)
    assert breaker.state == "closed" and breaker.allow()
    assert breaker.record_failure() is False
    assert breaker.record_failure() is False
    assert breaker.state == "closed"  # 未达阈值不熔断
    assert breaker.record_failure() is True  # 第 3 次 → 触发熔断
    assert breaker.state == "open" and not breaker.allow()
    assert breaker.remaining_cooldown() == pytest.approx(30.0)
    clock.advance(29.9)
    assert breaker.state == "open" and not breaker.allow()
    clock.advance(0.1)  # 期满 → 半开放行
    assert breaker.state == "half_open" and breaker.allow()
    assert breaker.record_failure() is True  # 半开失败 → 再熔断
    assert breaker.state == "open"
    clock.advance(30.0)
    assert breaker.allow()
    breaker.record_success()  # 半开成功 → 复位
    assert breaker.state == "closed" and breaker.failures == 0


def test_v5_circuit_trips_after_three_failures_and_skips_member() -> None:
    """V5:连续 3 次失败熔断——第 4 张图跳过该成员(零调用),fallback_from + note=熔断。"""
    clock = FakeClock()
    failing = ScriptedMember("glm:x", [RuntimeError("连接超时")])
    backup = FakeMember("backup", prob=0.5)
    fc, _ = make_failover([failing, backup], clock=clock)

    for i in range(3):  # 三张图各失败一次 → 连续 3 次
        assert fc.classify(make_evidence(f"a{i}.jpg")).model == "failover→backup"
    assert failing.calls == 3

    result = fc.classify(make_evidence("a3.jpg"))  # 熔断中:直接下一家
    assert failing.calls == 3  # 不再调用熔断成员
    assert result.model == "failover→backup"
    assert result.scores["fallback_from"] == ["glm:x"]  # 熔断跳过计入失败标注
    assert result.scores["note"] == "熔断"


def test_v5_circuit_skip_note_does_not_clobber_member_note() -> None:
    """V5:熔断 note 用 setdefault——成员自带 note 不被覆盖。"""
    clock = FakeClock()
    failing = ScriptedMember("m1", [ValueError("挂了")])
    backup = ScriptedMember("m2", [{"nsfw_prob": 0.6, "note": "成员自带备注"}])
    fc, _ = make_failover([failing, backup], clock=clock)
    for i in range(3):
        fc.classify(make_evidence(f"a{i}.jpg"))
    result = fc.classify(make_evidence("a3.jpg"))
    assert result.scores["fallback_from"] == ["m1"]
    assert result.scores["note"] == "成员自带备注"  # 成员自带 note 优先


def test_v5_circuit_half_open_recovery_after_cooldown() -> None:
    """V5 半开:熔断期满后下一次尝试放行;成功 → 断路器复位,回到主成员。"""
    clock = FakeClock()
    flaky = ScriptedMember(
        "flaky",
        [RuntimeError("超时"), RuntimeError("超时"), RuntimeError("超时"), 0.42],
    )
    backup = FakeMember("backup", prob=0.3)
    fc, _ = make_failover([flaky, backup], clock=clock)
    for i in range(3):
        assert fc.classify(make_evidence(f"a{i}.jpg")).model == "failover→backup"

    clock.advance(30.0)  # 冷却期满 → 半开
    recovered = fc.classify(make_evidence("a3.jpg"))
    assert recovered.model == "failover→flaky"  # 放行的探活尝试成功
    assert recovered.nsfw_prob == pytest.approx(0.42)
    assert recovered.scores["fallback_from"] == []
    assert flaky.calls == 4

    again = fc.classify(make_evidence("a4.jpg"))  # 复位后正常留在主成员
    assert again.model == "failover→flaky"


def test_v5_circuit_half_open_failure_reopens() -> None:
    """V5 半开失败 → 再次熔断 30s:冷却未满前跳过,期满后又一次探活。"""
    clock = FakeClock()
    flaky = ScriptedMember("flaky", [RuntimeError("超时")] * 4 + [0.7])
    backup = FakeMember("backup", prob=0.3)
    fc, _ = make_failover([flaky, backup], clock=clock)
    for i in range(3):
        fc.classify(make_evidence(f"a{i}.jpg"))

    clock.advance(30.0)
    fc.classify(make_evidence("a3.jpg"))  # 半开尝试:失败 → 再熔断
    assert flaky.calls == 4

    skipped = fc.classify(make_evidence("a4.jpg"))  # 新冷却未满:跳过
    assert flaky.calls == 4
    assert skipped.model == "failover→backup"
    assert skipped.scores["note"] == "熔断"

    clock.advance(30.0)
    recovered = fc.classify(make_evidence("a5.jpg"))  # 再次半开 → 成功
    assert recovered.model == "failover→flaky"
    assert flaky.calls == 5


def test_v5_circuit_success_resets_consecutive_counter() -> None:
    """V5:成功清零连续计数——2 败 1 成后需再连败 3 次才熔断(连续语义)。"""
    clock = FakeClock()
    m1 = ScriptedMember(
        "m1",
        [
            RuntimeError("a"),
            RuntimeError("b"),
            0.55,  # 成功 → 复位
            RuntimeError("c"),
            RuntimeError("d"),
            RuntimeError("e"),  # 连续第 3 败 → 熔断
            0.9,
        ],
    )
    m2 = FakeMember("m2", prob=0.2)
    fc, _ = make_failover([m1, m2], clock=clock)

    fc.classify(make_evidence("0.jpg"))  # 败(计数 1)
    fc.classify(make_evidence("1.jpg"))  # 败(计数 2)
    assert fc.classify(make_evidence("2.jpg")).model == "failover→m1"  # 成功复位
    fc.classify(make_evidence("3.jpg"))  # 败(计数 1)
    fc.classify(make_evidence("4.jpg"))  # 败(计数 2,未熔断)
    assert fc.classify(make_evidence("5.jpg")).model == "failover→m2"  # 第 3 败 → 熔断转移
    assert m1.calls == 6

    skipped = fc.classify(make_evidence("6.jpg"))  # 熔断跳过
    assert m1.calls == 6
    assert skipped.scores["fallback_from"] == ["m1"]
    assert skipped.scores["note"] == "熔断"


def test_v5_circuit_config_errors_never_trip() -> None:
    """V5:配置类错误不计入熔断——连续多轮仍逐次尝试,"全配置错→VlmConfigError"语义不变。"""
    clock = FakeClock()
    m1 = FakeMember("glm:x", exc=config_error("glm 密钥未配置"))
    m2 = FakeMember("qwen:y", exc=config_error("qwen 密钥未配置"))
    fc, _ = make_failover([m1, m2], clock=clock)
    for _ in range(5):  # 远超熔断阈值:配置错误不应触发熔断跳过
        with pytest.raises(RuntimeError) as excinfo:
            fc.classify(make_evidence("i.jpg"))
        assert type(excinfo.value).__name__ in ("VlmConfigError", "_ChainConfigError")
        assert "配置不可用" in str(excinfo.value)
    assert m1.classify_calls == 5 and m2.classify_calls == 5  # 从未被跳过


def test_v5_circuit_all_members_open_raises_chinese_runtime_error() -> None:
    """V5:全部成员熔断跳过 → 链尽上抛中文 RuntimeError(含"熔断"与成员名)。"""
    clock = FakeClock()
    m1 = ScriptedMember("m1", [RuntimeError("挂一")])
    m2 = ScriptedMember("m2", [RuntimeError("挂二")])
    fc, _ = make_failover([m1, m2], clock=clock)
    for i in range(3):  # 两成员各连败 3 次 → 双双熔断;期间每图仍上抛最后非配置异常
        with pytest.raises(RuntimeError):
            fc.classify(make_evidence(f"a{i}.jpg"))

    with pytest.raises(RuntimeError, match="熔断") as excinfo:
        fc.classify(make_evidence("a3.jpg"))
    message = str(excinfo.value)
    assert "m1" in message and "m2" in message
    assert m1.calls == 3 and m2.calls == 3  # 熔断后零调用


def test_v5_circuit_disabled_restores_legacy_behavior() -> None:
    """V5:circuit_breaker=False 关闭断路器——任意连续失败都逐次尝试,无 note/gauge。"""
    telemetry.reset()
    clock = FakeClock()
    failing = ScriptedMember("m1", [RuntimeError("一直挂")])
    backup = FakeMember("m2", prob=0.4)
    fc, _ = make_failover([failing, backup], clock=clock, circuit_breaker=False)
    for i in range(6):
        result = fc.classify(make_evidence(f"a{i}.jpg"))
        assert result.model == "failover→m2"
        assert "note" not in result.scores
    assert failing.calls == 6  # 从不跳过(完全恢复 A68 旧行为)
    assert "failover.circuit_open" not in telemetry.snapshot()["gauges"]  # 关闭不发仪表


def test_v5_circuit_inband_error_scores_count_as_failures() -> None:
    """V5:带内错误分(scores.error)同样计入连续失败 → 3 次后熔断跳过。"""
    clock = FakeClock()
    degraded = ScriptedMember("m1", ["GLM 识别失败:超时"])  # 永远返回带内错误分
    backup = FakeMember("m2", prob=0.61)
    fc, _ = make_failover([degraded, backup], clock=clock)
    for i in range(3):
        assert fc.classify(make_evidence(f"a{i}.jpg")).model == "failover→m2"
    assert degraded.calls == 3

    skipped = fc.classify(make_evidence("a3.jpg"))
    assert degraded.calls == 3  # 熔断跳过
    assert skipped.scores["fallback_from"] == ["m1"]
    assert skipped.scores["note"] == "熔断"


def test_v5_failover_telemetry_switch_gauge_and_timer() -> None:
    """V5 可观测:成员转移 inc("failover.switch");熔断数 gauge;classify 全程计时。"""
    telemetry.reset()
    clock = FakeClock()
    failing = ScriptedMember("m1", [RuntimeError("超时")])
    backup = FakeMember("m2", prob=0.4)
    fc, _ = make_failover([failing, backup], clock=clock)

    for i in range(3):
        fc.classify(make_evidence(f"a{i}.jpg"))
    snap = telemetry.snapshot()
    assert snap["counters"]["failover.switch"] == 3  # 每图一次成员转移
    assert snap["gauges"]["failover.circuit_open"] == 1  # 第 3 图熔断
    assert snap["timers"]["failover.classify"]["count"] == 3

    result = fc.classify(make_evidence("a3.jpg"))  # 熔断跳过也是一次转移
    snap2 = telemetry.snapshot()
    assert result.model == "failover→m2"
    assert snap2["counters"]["failover.switch"] == 4
    assert snap2["gauges"]["failover.circuit_open"] == 1
    assert snap2["timers"]["failover.classify"]["count"] == 4
