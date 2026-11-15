"""V10.21 GLM 第三方分歧仲裁接线(orchestrator ``_wire_arbiter``)单元测试。

全部离线、依赖注入 fake(仿 tests/test_orchestrator.py 的既有基建风格):

- 门控 ``cfg.arbiter_enabled`` 为附加实例属性、getattr 缺省 False;
- 默认关 = 与现状逐字节一致:仲裁内核工厂是**炸弹**也绝不被触碰,
  判定消费的集成条目 / 遥测计数 / 入队口径零扰动;
- 开启 + 存在分歧图 → ``arbiter.arbitrate`` 被调且 ensemble 条目被替换
  (model="vlm-arbiter")后进判定;开启 + 无分歧 → 不触发任何仲裁路径;
- 仲裁内核未就位 / arbitrate 抛异常 → 优雅降级(原样集成、warning、
  ``scan.arbiter.errors`` 计数),主线扫描不受损。

零外呼:仲裁内核经 ``_default_arbiter_module`` 工厂替换为 fake,真实
``netsentinel.vision.arbiter`` 与 GLM 客户端全程不被导入。
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest

import netsentinel.pipeline.orchestrator as orchestrator
from netsentinel import telemetry
from netsentinel.contracts import (
    Config,
    ImageEvidence,
    ImageScore,
    PageSample,
    SiteReport,
    Verdict,
)

SITE = "http://localhost/arb"


# ---------------------------------------------------------------------------
# fakes(与 test_orchestrator.py 同款的最小复制,保持本文件自包含)
# ---------------------------------------------------------------------------
class FakeCapture:
    """页面采样 fake:返回含 N 张 300×300 图片的单页 PageSample。"""

    def __init__(self, count: int = 4) -> None:
        self.count = count

    def __call__(self, url: str, cfg: Config, *, fetch_page: Any = None) -> PageSample:
        imgs = [
            ImageEvidence(
                path=os.path.join("fake", "imgs", f"pic_{i}.jpg"),
                url=f"http://img/pic/{i}",
                source_page=url,
                width=300,
                height=300,
            )
            for i in range(self.count)
        ]
        return PageSample(url=url, screenshot_path="", image_evidences=imgs)


class FixedScoreClassifier:
    """固定分值成员 fake:每张图都给同一 nsfw_prob(用于构造成员分歧)。"""

    def __init__(self, name: str, prob: float) -> None:
        self.name = name
        self.prob = prob

    def classify_batch(self, imgs: list[ImageEvidence]) -> list[ImageScore]:
        return [
            ImageScore(image=i, model=self.name, nsfw_prob=self.prob, scores={})
            for i in imgs
        ]


class RecordingQueue:
    """复核队列 fake:记录 add 调用(含关键字实参)。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def add(self, report: SiteReport, evidence_zip: str = "", **kwargs: Any) -> int:
        self.calls.append(
            {"report": report, "zip": evidence_zip, "kwargs": dict(kwargs)}
        )
        return 7


class FakeAudit:
    """审计 fake:吞掉 scan 审计事件,免真实落盘。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log_event(self, event: str, **fields: Any) -> None:
        self.events.append({"event": event, **fields})


def _patch_scan_side_effects(monkeypatch: pytest.MonkeyPatch) -> RecordingQueue:
    """替换 run_scan 的 discover / build_bundle / queue / audit 默认工厂。"""
    monkeypatch.setattr(
        orchestrator, "_default_discover", lambda url, c, fetch_page=None: [url]
    )
    monkeypatch.setattr(
        orchestrator,
        "_default_build_bundle",
        lambda report, cfg: SimpleNamespace(zip_path="fake/evidence/bundle.zip"),
    )
    queue = RecordingQueue()
    monkeypatch.setattr(orchestrator, "_default_queue", lambda db_path: queue)
    monkeypatch.setattr(orchestrator, "_default_audit_logger", lambda path: FakeAudit())
    return queue


def _counter(name: str) -> float:
    return telemetry.snapshot()["counters"].get(name, 0.0)


def _run_two_member(
    monkeypatch: pytest.MonkeyPatch, cfg: Config, p_a: float, p_b: float
) -> SiteReport:
    """两成员扫描:mA=注入分类器(固定 p_a)/ mB=经工厂(固定 p_b)。"""
    _patch_scan_side_effects(monkeypatch)
    monkeypatch.setattr(
        orchestrator,
        "_default_classifier",
        lambda name, c: FixedScoreClassifier("mB", p_b),
    )
    return orchestrator.run_scan(
        SITE, cfg, capture=FakeCapture(), classifier=FixedScoreClassifier("mA", p_a)
    )


@pytest.fixture()
def cfg(tmp_path) -> Config:
    """仲裁接线测试统一配置:关融合、双成员(分歧可控)。"""
    c = Config(
        data_dir=str(tmp_path / "data"),
        evidence_dir=str(tmp_path / "evidence"),
        db_path=str(tmp_path / "data" / "review_queue.db"),
        audit_path=str(tmp_path / "data" / "audit.jsonl"),
        log_path=str(tmp_path / "data" / "logs" / "netsentinel.log"),
    )
    c.use_fusion = False
    c.ensemble_members = ["mB"]
    return c


class FakeArbiterModule:
    """仲裁内核 fake:记录 arbitrate 调用并按需替换 / 炸弹 / 抛错。

    常量与真实 ``netsentinel.vision.arbiter`` 对齐(DISAGREE_GAP=0.35、
    ARBITER_MODEL="vlm-arbiter"),供 orchestrator 前置分歧检查消费。
    """

    DISAGREE_GAP = 0.35
    ARBITER_MODEL = "vlm-arbiter"

    def __init__(self, *, replacement_prob: float | None = None,
                 error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.replacement_prob = replacement_prob
        self.error = error

    def arbitrate(
        self,
        ensemble: list[ImageScore],
        member_scores: list[ImageScore],
        cfg: Config,
        **kwargs: Any,
    ) -> list[ImageScore]:
        self.calls.append({"ensemble": list(ensemble), "members": list(member_scores)})
        if self.error is not None:
            raise self.error
        if self.replacement_prob is None:  # 炸弹模式:被调即失败
            raise AssertionError("无分歧时 arbitrate 不应被调用")
        return [
            ImageScore(
                image=e.image,
                model=self.ARBITER_MODEL,
                nsfw_prob=self.replacement_prob,
                scores={"resolved": True},
            )
            for e in ensemble
        ]


# ---------------------------------------------------------------------------
# 1. 默认关:零行为(仲裁内核为炸弹也不被调)
# ---------------------------------------------------------------------------
def test_default_off_bomb_module_never_touched(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """缺省(不设 arbiter_enabled):扫描全程不导入仲裁内核——工厂为
    炸弹也不被触碰;判定消费的仍是 ensemble 条目;计数只走 skipped 侧。"""
    def bomb() -> Any:
        raise AssertionError("默认关时不应触碰仲裁内核")

    monkeypatch.setattr(orchestrator, "_default_arbiter_module", bomb)
    base_skip = _counter("scan.arbiter.skipped")
    base_enabled = _counter("scan.arbiter.enabled")

    report = _run_two_member(monkeypatch, cfg, p_a=0.95, p_b=0.05)  # 分歧 0.9

    # 两成员等权集成 0.50 → SUSPECT(agg 达 review_threshold 0.50)。
    assert report.verdict is Verdict.SUSPECT
    assert report.agg_nsw_prob == pytest.approx(0.50)
    assert {s.model for s in report.image_scores} == {"ensemble"}
    assert _counter("scan.arbiter.skipped") == base_skip + 1.0
    assert _counter("scan.arbiter.enabled") == base_enabled


def test_explicit_false_same_as_default(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """显式 ``arbiter_enabled = False`` 与缺省完全等价(向后兼容口径)。"""
    cfg.arbiter_enabled = False
    monkeypatch.setattr(
        orchestrator,
        "_default_arbiter_module",
        lambda: (_ for _ in ()).throw(AssertionError("关时不触碰仲裁内核")),
    )
    base_skip = _counter("scan.arbiter.skipped")

    report = _run_two_member(monkeypatch, cfg, p_a=0.95, p_b=0.05)

    assert report.verdict is Verdict.SUSPECT
    assert {s.model for s in report.image_scores} == {"ensemble"}
    assert _counter("scan.arbiter.skipped") == base_skip + 1.0


# ---------------------------------------------------------------------------
# 2. 开启 + 存在分歧图:触发仲裁且 ensemble 条目被替换
# ---------------------------------------------------------------------------
def test_enabled_disagreement_replaces_ensemble_entries(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """开启 + 分歧 0.9 ≥ 0.35:arbitrate 恰被调一次,传入集成条目与成员
    原始分;替换条目(model=vlm-arbiter)进判定,档位随仲裁分翻转。"""
    cfg.arbiter_enabled = True
    fake = FakeArbiterModule(replacement_prob=0.05)
    monkeypatch.setattr(orchestrator, "_default_arbiter_module", lambda: fake)
    base_enabled = _counter("scan.arbiter.enabled")

    report = _run_two_member(monkeypatch, cfg, p_a=0.95, p_b=0.05)

    # 仲裁被调一次:首参为 4 条 ensemble(每图一条),次参为 8 条成员原始分。
    assert len(fake.calls) == 1
    assert [s.model for s in fake.calls[0]["ensemble"]] == ["ensemble"] * 4
    assert sorted({s.model for s in fake.calls[0]["members"]}) == ["mA", "mB"]
    assert len(fake.calls[0]["members"]) == 8
    # 替换后每图 0.05 → agg 0.05 < review_threshold → CLEAN,不入复核队列。
    assert report.verdict is Verdict.CLEAN
    assert report.needs_review is False
    assert {s.model for s in report.image_scores} == {"vlm-arbiter"}
    assert _counter("scan.arbiter.enabled") == base_enabled + 1.0


def test_enabled_boundary_disagreement_triggers(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """分歧度恰等于 DISAGREE_GAP(0.35,闭边界)也触发仲裁。"""
    cfg.arbiter_enabled = True
    fake = FakeArbiterModule(replacement_prob=0.02)
    monkeypatch.setattr(orchestrator, "_default_arbiter_module", lambda: fake)

    report = _run_two_member(monkeypatch, cfg, p_a=0.70, p_b=0.35)  # max-min=0.35

    assert len(fake.calls) == 1
    assert {s.model for s in report.image_scores} == {"vlm-arbiter"}
    assert report.verdict is Verdict.CLEAN


# ---------------------------------------------------------------------------
# 3. 开启 + 无分歧:不触发任何仲裁路径
# ---------------------------------------------------------------------------
def test_enabled_no_disagreement_never_calls_arbitrate(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """开启但成员完全一致(分歧 0):arbitrate 为炸弹也不被调;判定与
    计数(enabled 侧 +1)保持门控语义,集成条目原样进判定。"""
    cfg.arbiter_enabled = True
    fake = FakeArbiterModule()  # 未给 replacement_prob:被调即断言失败
    monkeypatch.setattr(orchestrator, "_default_arbiter_module", lambda: fake)
    base_enabled = _counter("scan.arbiter.enabled")

    report = _run_two_member(monkeypatch, cfg, p_a=0.90, p_b=0.90)

    assert fake.calls == []
    # 等权集成 0.90,4 张图 ≥ prob_count_line 0.80 且 ≥ min_nsw_images → NSFW。
    assert report.verdict is Verdict.NSFW
    assert {s.model for s in report.image_scores} == {"ensemble"}
    assert _counter("scan.arbiter.enabled") == base_enabled + 1.0


def test_enabled_single_member_never_triggers(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """开启但仅一个成员(不可能分歧):同样不触发仲裁。"""
    cfg.arbiter_enabled = True
    cfg.ensemble_members = []  # 只留注入的主分类器 mA
    fake = FakeArbiterModule()
    monkeypatch.setattr(orchestrator, "_default_arbiter_module", lambda: fake)
    queue = _patch_scan_side_effects(monkeypatch)

    report = orchestrator.run_scan(
        SITE, cfg, capture=FakeCapture(), classifier=FixedScoreClassifier("mA", 0.9)
    )

    assert fake.calls == []
    assert report.verdict is Verdict.NSFW
    assert queue.calls and queue.calls[0]["kwargs"] == {}


# ---------------------------------------------------------------------------
# 4. 优雅降级:内核未就位 / arbitrate 抛异常,主线不受损
# ---------------------------------------------------------------------------
def test_arbiter_module_not_ready_degrades_to_ensemble(
    monkeypatch: pytest.MonkeyPatch, cfg: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """开启 + 分歧,但仲裁内核未就位(工厂抛中文 RuntimeError):warning +
    ``scan.arbiter.errors`` + 原样集成判定,扫描绝不中断。"""
    cfg.arbiter_enabled = True

    def not_ready() -> Any:
        raise RuntimeError("模块 netsentinel.vision.arbiter 未就位:测试注入")

    monkeypatch.setattr(orchestrator, "_default_arbiter_module", not_ready)
    base_err = _counter("scan.arbiter.errors")

    with caplog.at_level("WARNING", logger="netsentinel.pipeline.orchestrator"):
        report = _run_two_member(monkeypatch, cfg, p_a=0.95, p_b=0.05)

    assert report.verdict is Verdict.SUSPECT  # 等权 0.50,与未仲裁一致
    assert {s.model for s in report.image_scores} == {"ensemble"}
    assert _counter("scan.arbiter.errors") == base_err + 1.0
    assert any("第三方仲裁已跳过" in r.message for r in caplog.records)


def test_arbitrate_exception_degrades_to_ensemble(
    monkeypatch: pytest.MonkeyPatch, cfg: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """开启 + 分歧,但 arbitrate 本身抛异常(如传输层炸裂):同样优雅降级
    (warning + errors 计数 + 原样集成),主线扫描不受损。"""
    cfg.arbiter_enabled = True
    fake = FakeArbiterModule(error=RuntimeError("GLM 视觉服务传输层炸裂(测试注入)"))
    monkeypatch.setattr(orchestrator, "_default_arbiter_module", lambda: fake)
    base_err = _counter("scan.arbiter.errors")

    with caplog.at_level("WARNING", logger="netsentinel.pipeline.orchestrator"):
        report = _run_two_member(monkeypatch, cfg, p_a=0.95, p_b=0.05)

    assert len(fake.calls) == 1  # 确认真的走到了仲裁调用再降级
    assert report.verdict is Verdict.SUSPECT
    assert {s.model for s in report.image_scores} == {"ensemble"}
    assert _counter("scan.arbiter.errors") == base_err + 1.0
    assert any("第三方仲裁已跳过" in r.message for r in caplog.records)


def test_arbitrate_offline_returns_ensemble_unchanged(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """开启 + 分歧,仲裁整体性放弃(返回 ensemble 原样,arbiter 自带的
    离线/预算尽语义在真实内核里实现):orchestrator 原样接受返回值,
    判定与未仲裁世界逐字节一致,不重复计 errors。"""
    cfg.arbiter_enabled = True
    returned: list[ImageScore] | None = None

    class PassthroughArbiter:
        DISAGREE_GAP = 0.35
        ARBITER_MODEL = "vlm-arbiter"

        def arbitrate(self, ensemble, member_scores, cfg_, **kw):
            nonlocal returned
            returned = list(ensemble)  # 原样返回(arbiter 离线语义)
            return returned

    monkeypatch.setattr(
        orchestrator, "_default_arbiter_module", lambda: PassthroughArbiter()
    )
    base_err = _counter("scan.arbiter.errors")

    report = _run_two_member(monkeypatch, cfg, p_a=0.95, p_b=0.05)

    assert returned is not None
    assert report.verdict is Verdict.SUSPECT
    assert {s.model for s in report.image_scores} == {"ensemble"}
    assert _counter("scan.arbiter.errors") == base_err  # 无 errors:这不是异常路径
