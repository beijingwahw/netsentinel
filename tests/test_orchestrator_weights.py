"""校准驱动的动态 ensemble(可靠性权重回流主链路)单元测试。

覆盖四个面向(对应任务验收口径):
1. **开关默认关闭 = 与旧行为完全一致**:`_default_ensemble` 收到的调用
   **不携带 weights 形参**(逐字快照),集成分值与直接调
   ``vision.ensemble.ensemble_scores``(等权)的结果逐项相等;
2. **开启 + 样本充足(>= MIN_N)**:构造 Brier 差异极大的 mock(完美 0 /
   全错 1,各 6 条),离群成员被降权(权重 21:1,集成分精确偏向高可靠方);
3. **开启 + 样本不足**:自动回退等权(调用口径与旧版一致);tracker
   异常同样降级等权,绝不阻断扫描;
4. **权重归一化不变量**:适配层输出 Σ w = 1;缺失成员按已知权重均值兜底,
   已知成员间权重比值在重归一后保持不变。

V14 贝叶斯权重回流(``cfg.bayes_reliability``,V14 收录批起升格为 Config
一等字段,与伴随漂移参数 ``bayes_half_life`` 同段收录)
在前四个面向之上追加:开关优先级矩阵(bayes 优先于 Brier,两开关全关 =
等权现状逐字节)、贝叶斯权重生效构造场景(手算精确分数)、冷启动回退
(收缩拉平)、零记录回退与遥测语义;V14 增量再加:ts 驱动的遗忘重放
(record 流可选 ``ts`` 字段 + ``bayes_half_life`` 半衰期,缺 ts 行回退滴答)。

另含配置开关本身的行为:YAML 读取(一等字段加载,不触发未知键告警)、
类型校验(中文 ValueError)、落盘(仅显式开启时写键,默认输出与旧版
逐字节一致)。全部离线、零网络;真实兄弟模块
(decision.reliability / vision.ensemble / decision.verdict)均为纯标准库实现。
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any

import pytest

import netsentinel.pipeline.orchestrator as orchestrator
from netsentinel import telemetry
from netsentinel.contracts import (
    Config,
    EvidenceBundle,
    ImageEvidence,
    ImageScore,
    PageSample,
    SiteReport,
    Verdict,
)
from netsentinel.decision.reliability import (
    MIN_N,
    BayesianReliabilityTracker,
    ReliabilityTracker,
)

SITE = "http://localhost/weights"

#: 完美 vs 全错的确定性权重:brier 0 / 1 → 归一权重 21/22 : 1/22(比值 21)。
W_GOOD = 21.0 / 22.0
W_BAD = 1.0 / 22.0

#: 同样 6 对 / 6 错数据下的贝叶斯收缩权重(手算,见 test_bayes_* 用例):
#: mean_raw(good) = 7/8、mean_raw(bad) = 1/8(α=1+对, β=1+错);
#: λ = κ/(κ+n_eff) = 10/16 = 5/8,池均值 = (1+6)/(2+12) = 1/2;
#: mean*(good) = (3/8)(7/8) + (5/8)(1/2) = 41/64,
#: mean*(bad)  = (3/8)(1/8) + (5/8)(1/2) = 23/64;Σ = 1(对称构造恰好闭式)。
W_BAYES_GOOD = 41.0 / 64.0
W_BAYES_BAD = 23.0 / 64.0

#: 贝叶斯加权后的集成分:good=0.2 / bad=0.8 → 0.2·(41/64) + 0.8·(23/64)
#: = 26.6/64 = 0.415625(< review_threshold 0.5 → CLEAN,< 等权 0.5)。
W_BAYES_ENSEMBLE_PROB = 0.2 * W_BAYES_GOOD + 0.8 * W_BAYES_BAD

_UNSET = object()  # 哨兵:区分「未传 weights 形参」与「显式传了值」


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class FakeCapture:
    """页面采样 fake:返回含 N 张 300x300 图片的 PageSample(路径不存在,
    图片增强静默跳过,不触碰磁盘)。"""

    def __init__(self, count: int = 4) -> None:
        self.count = count

    def __call__(self, url: str, cfg: Config, *, fetch_page: Any = None) -> PageSample:
        imgs = [
            ImageEvidence(
                path=os.path.join("fake", "imgs", f"plain_{i}.jpg"),
                url=f"http://img/plain/{i}",
                source_page=url,
                width=300,
                height=300,
            )
            for i in range(self.count)
        ]
        return PageSample(url=url, screenshot_path="", image_evidences=imgs)


class FixedClassifier:
    """固定分值分类器 fake:每张图都给同一个 nsfw_prob(用于构造成员分歧)。"""

    def __init__(self, name: str, prob: float) -> None:
        self.name = name
        self.prob = prob

    def classify_batch(self, imgs: list[ImageEvidence]) -> list[ImageScore]:
        return [
            ImageScore(image=i, model=self.name, nsfw_prob=self.prob, scores={})
            for i in imgs
        ]


class RecordingEnsemble:
    """ensemble 工厂 fake:记录每次调用,再转交真实 vision.ensemble 执行。

    ``weights`` 形参用哨兵 ``_UNSET`` 作缺省——只有主链路真的**传了**该形参,
    记录里才会出现具体映射;等权兜底(旧行为)则是「未传形参」。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[list[ImageScore], Any]] = []

    def __call__(
        self, scores: list[ImageScore], weights: Any = _UNSET
    ) -> list[ImageScore]:
        self.calls.append((list(scores), weights))
        ensemble = orchestrator._load("netsentinel.vision.ensemble")
        effective = None if weights is _UNSET else weights
        return ensemble.ensemble_scores(scores, weights=effective)


class FakeQueue:
    def __init__(self) -> None:
        self.added: list[tuple[SiteReport, str]] = []

    def add(self, report: SiteReport, evidence_zip: str = "") -> int:
        self.added.append((report, evidence_zip))
        return 77


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log_event(self, event: str, **fields: Any) -> None:
        self.events.append({"event": event, **fields})


def _patch_scan_side_effects(monkeypatch: pytest.MonkeyPatch) -> RecordingEnsemble:
    """替换 run_scan 的外部依赖工厂;返回 ensemble 调用记录器。"""
    monkeypatch.setattr(
        orchestrator, "_default_discover", lambda url, c, fetch_page=None: [url]
    )
    recorder = RecordingEnsemble()
    monkeypatch.setattr(orchestrator, "_default_ensemble", recorder)
    monkeypatch.setattr(
        orchestrator,
        "_default_build_bundle",
        lambda report, cfg: EvidenceBundle(
            site_url=report.site_url,
            dir_path="fake/evidence/dir",
            manifest_path="fake/evidence/dir/manifest.json",
            zip_path="fake/evidence/bundle.zip",
        ),
    )
    monkeypatch.setattr(orchestrator, "_default_queue", lambda db_path: FakeQueue())
    monkeypatch.setattr(orchestrator, "_default_audit_logger", lambda path: FakeAudit())
    return recorder


@pytest.fixture()
def cfg(tmp_path) -> Config:
    """隔离到临时目录的默认配置(显式关掉 fusion,聚焦 ensemble 主链路)。"""
    c = Config(
        data_dir=str(tmp_path / "data"),
        evidence_dir=str(tmp_path / "evidence"),
        db_path=str(tmp_path / "data" / "review_queue.db"),
        audit_path=str(tmp_path / "data" / "audit.jsonl"),
        log_path=str(tmp_path / "data" / "logs" / "netsentinel.log"),
        use_fusion=False,
    )
    c.ensemble_members = ["good", "bad"]
    return c


def _run_two_member_scan(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> tuple[SiteReport, RecordingEnsemble]:
    """两成员扫描:good=0.2(注入)/ bad=0.8(经 _default_classifier 工厂)。"""
    recorder = _patch_scan_side_effects(monkeypatch)
    good, bad = FixedClassifier("good", 0.2), FixedClassifier("bad", 0.8)
    monkeypatch.setattr(orchestrator, "_default_classifier", lambda name, c: bad)
    report = orchestrator.run_scan(SITE, cfg, capture=FakeCapture(), classifier=good)
    return report, recorder


def _ensemble_entries(report: SiteReport) -> list[ImageScore]:
    return [s for s in report.image_scores if s.model == "ensemble"]


def _dominance_tracker(n: int = 6) -> ReliabilityTracker:
    """完美(good, brier=0)vs 全错(bad, brier=1)的确定性 tracker。"""
    tracker = ReliabilityTracker()
    for _ in range(n):
        tracker.record("good", 1.0, True)
        tracker.record("bad", 1.0, False)
    return tracker


def _thin_tracker(n: int = MIN_N - 1) -> ReliabilityTracker:
    """样本不足的 tracker:good 仅 n < MIN_N 条记录 → weights() 全 None。"""
    tracker = ReliabilityTracker()
    for _ in range(n):
        tracker.record("good", 1.0, True)
    return tracker


def _bayes_dominance_tracker(n: int = 6) -> BayesianReliabilityTracker:
    """good 全对 vs bad 全错的确定性贝叶斯 tracker(遗忘关闭,事件权重恒 1)。

    与 :func:`_dominance_tracker` 同源数据(对/错各 n 条),供适配层与
    主链路用例对照两种权重模型对同一反馈的不同读法。
    """
    tracker = BayesianReliabilityTracker(half_life=None)
    for _ in range(n):
        tracker.record("good", True)
        tracker.record("bad", False)
    return tracker


def _write_reliability_jsonl(cfg: Config, rows: list[tuple[str, float, bool]]) -> None:
    """向 ``cfg.data_dir/reliability.jsonl`` 追加 Brier record 流三元组行。

    行结构与 :meth:`ReliabilityTracker.record` 落盘格式逐键一致
    (``{"provider", "p", "outcome"}``),供两条权重路径读**同一份**数据源。
    """
    path = pathlib.Path(cfg.data_dir) / orchestrator.RELIABILITY_JSONL_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for provider, prob, outcome in rows:
            fh.write(
                json.dumps(
                    {"provider": provider, "p": prob, "outcome": outcome},
                    ensure_ascii=False,
                )
                + "\n"
            )


def _write_reliability_jsonl_ts(
    cfg: Config, rows: list[tuple[str, float, bool, float]]
) -> None:
    """追加带 ``ts`` 时刻的四键行(V14 record 写侧格式,Unix epoch 秒)。

    行结构与 ``ReliabilityTracker.record(..., ts=...)`` 落盘格式逐键一致
    (``{"provider", "p", "outcome", "ts"}``),供贝叶斯遗忘重放按真实
    时刻衰减;与 :func:`_write_reliability_jsonl` 的无 ts 行可混写。
    """
    path = pathlib.Path(cfg.data_dir) / orchestrator.RELIABILITY_JSONL_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for provider, prob, outcome, ts in rows:
            fh.write(
                json.dumps(
                    {
                        "provider": provider,
                        "p": prob,
                        "outcome": outcome,
                        "ts": ts,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def _sibling_cfg(cfg: Config, name: str) -> Config:
    """同款隔离配置,但 data_dir 换成兄弟目录(单测内多场景互不串档)。"""
    import dataclasses

    sibling = pathlib.Path(cfg.data_dir).parent / name
    return dataclasses.replace(cfg, data_dir=str(sibling))


# ---------------------------------------------------------------------------
# 1. 开关默认关闭:行为与旧版完全一致(快照断言)
# ---------------------------------------------------------------------------
def test_switch_defaults_off_field_default_false() -> None:
    """V11 升格后 ensemble_reliability_weights 为一等字段,默认 False = 等权
    (与升格前"属性不存在 + getattr 缺省 False"的行为完全一致)。"""
    default_cfg = Config()
    assert default_cfg.ensemble_reliability_weights is False
    assert getattr(default_cfg, "ensemble_reliability_weights", False) is False


def test_switch_off_calls_ensemble_without_weights_kwarg(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """默认关闭:_default_ensemble 收到的调用**不携带 weights 形参**(与旧版
    逐字一致),且集成结果与直接等权调用 vision.ensemble 完全相等。"""
    base_weighted = telemetry.snapshot()["counters"].get(
        "scan.ensemble_weights.reliability_weighted", 0.0
    )
    report, recorder = _run_two_member_scan(monkeypatch, cfg)

    assert len(recorder.calls) == 1
    raw_scores, weights_arg = recorder.calls[0]
    assert weights_arg is _UNSET  # 旧调用口径:单形参,未传 weights
    assert sorted({s.model for s in raw_scores}) == ["bad", "good"]

    # 快照:与直接调 vision.ensemble.ensemble_scores(等权)逐项相等
    vision_ensemble = orchestrator._load("netsentinel.vision.ensemble")
    legacy = vision_ensemble.ensemble_scores(raw_scores)
    assert _ensemble_entries(report) == legacy

    # 等权数值快照:每图 (0.2 + 0.8) / 2 = 0.5;members 明细原样保留
    for s in _ensemble_entries(report):
        assert s.nsfw_prob == pytest.approx(0.5, abs=1e-12)
        assert s.scores["members"] == {"good": 0.2, "bad": 0.8}
    assert report.agg_nsw_prob == pytest.approx(0.5, abs=1e-12)
    assert report.verdict is Verdict.SUSPECT  # 0.5 >= review_threshold
    # 开关关闭:不应产生任何可靠性权重遥测(计数器前后不变)
    snap = telemetry.snapshot()["counters"]
    assert snap.get("scan.ensemble_weights.reliability_weighted", 0.0) == base_weighted


def test_switch_off_explicit_false_behaves_identically(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """显式 cfg.ensemble_reliability_weights = False:与不设置完全一致。"""
    cfg.ensemble_reliability_weights = False
    report, recorder = _run_two_member_scan(monkeypatch, cfg)
    assert recorder.calls[0][1] is _UNSET
    assert all(s.nsfw_prob == pytest.approx(0.5, abs=1e-12) for s in _ensemble_entries(report))


# ---------------------------------------------------------------------------
# 2. 开启 + 样本充足:离群模型被降权
# ---------------------------------------------------------------------------
def test_switch_on_sufficient_samples_downweights_outlier(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """开启 + tracker 样本充足(brier 0 vs 1):权重 21/22 : 1/22 精确传入
    ensemble,集成分从等权 0.5 偏向高可靠方 = 5/22 ≈ 0.227(离群 bad 降权)。"""
    cfg.ensemble_reliability_weights = True
    monkeypatch.setattr(
        orchestrator, "_default_reliability_tracker", lambda c: _dominance_tracker()
    )
    base = telemetry.snapshot()["counters"].get(
        "scan.ensemble_weights.reliability_weighted", 0.0
    )

    report, recorder = _run_two_member_scan(monkeypatch, cfg)

    # 传递给 ensemble 的权重映射:精确等于 tracker 归一权重,Σ = 1
    assert len(recorder.calls) == 1
    weights = recorder.calls[0][1]
    assert weights == {"good": pytest.approx(W_GOOD, abs=1e-12),
                       "bad": pytest.approx(W_BAD, abs=1e-12)}
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-12)

    # 每图集成分 = 0.2*(21/22) + 0.8*(1/22) = 5/22 ≈ 0.2273 < 等权 0.5
    expected = 0.2 * W_GOOD + 0.8 * W_BAD
    assert expected == pytest.approx(5.0 / 22.0, abs=1e-12)
    for s in _ensemble_entries(report):
        assert s.nsfw_prob == pytest.approx(expected, abs=1e-12)
    assert report.agg_nsw_prob == pytest.approx(expected, abs=1e-12)
    assert report.verdict is Verdict.CLEAN  # 0.227 < review_threshold

    # 遥测:可靠性加权集成生效一次
    snap = telemetry.snapshot()["counters"]
    assert snap.get("scan.ensemble_weights.reliability_weighted", 0.0) == base + 1.0


def test_switch_on_reads_tracker_from_data_dir_jsonl(
    monkeypatch: pytest.MonkeyPatch, cfg: Config, tmp_path
) -> None:
    """缺省工厂从 cfg.data_dir/reliability.jsonl 读本地反馈(与 kernel_wire
    同一约定):写 6 条 good 全对反馈后,good 权重应压倒等权基线。"""
    import pathlib

    jsonl = pathlib.Path(cfg.data_dir) / orchestrator.RELIABILITY_JSONL_NAME
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    with jsonl.open("a", encoding="utf-8") as fh:
        for _ in range(6):
            fh.write('{"provider": "good", "p": 1.0, "outcome": true}\n')
            fh.write('{"provider": "bad", "p": 1.0, "outcome": false}\n')

    cfg.ensemble_reliability_weights = True  # 不 monkeypatch tracker 工厂
    _report, recorder = _run_two_member_scan(monkeypatch, cfg)

    weights = recorder.calls[0][1]
    assert weights is not _UNSET
    assert weights["good"] > weights["bad"] * 10  # 21:1,离群成员显著降权


# ---------------------------------------------------------------------------
# 3. 开启 + 样本不足 / 异常:自动回退等权
# ---------------------------------------------------------------------------
def test_switch_on_insufficient_samples_falls_back_equal(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """开启但样本 < MIN_N:weights() 全 None → 不传 weights 形参,等权 0.5。"""
    cfg.ensemble_reliability_weights = True
    monkeypatch.setattr(
        orchestrator, "_default_reliability_tracker", lambda c: _thin_tracker()
    )
    base = telemetry.snapshot()["counters"].get(
        "scan.ensemble_weights.equal_fallback", 0.0
    )

    report, recorder = _run_two_member_scan(monkeypatch, cfg)

    assert recorder.calls[0][1] is _UNSET  # 等权:调用口径与旧版一致
    for s in _ensemble_entries(report):
        assert s.nsfw_prob == pytest.approx(0.5, abs=1e-12)
    snap = telemetry.snapshot()["counters"]
    assert snap.get("scan.ensemble_weights.equal_fallback", 0.0) == base + 1.0


def test_switch_on_empty_tracker_falls_back_equal(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """开启但反馈为空(无任何记录):同样回退等权。"""
    cfg.ensemble_reliability_weights = True
    monkeypatch.setattr(
        orchestrator, "_default_reliability_tracker", lambda c: ReliabilityTracker()
    )
    report, recorder = _run_two_member_scan(monkeypatch, cfg)
    assert recorder.calls[0][1] is _UNSET
    assert all(s.nsfw_prob == pytest.approx(0.5, abs=1e-12) for s in _ensemble_entries(report))


def test_switch_on_tracker_failure_degrades_to_equal(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """开启但可靠性内核未就位 / tracker 构造失败:告警 + 降级等权,扫描不中断。"""
    cfg.ensemble_reliability_weights = True

    def broken(cfg: Config) -> Any:
        raise RuntimeError("模块 netsentinel.decision.reliability 未就位:模拟缺失")

    monkeypatch.setattr(orchestrator, "_default_reliability_tracker", broken)
    base = telemetry.snapshot()["counters"].get("scan.ensemble_weights.skipped", 0.0)

    report, recorder = _run_two_member_scan(monkeypatch, cfg)

    assert recorder.calls[0][1] is _UNSET
    assert all(s.nsfw_prob == pytest.approx(0.5, abs=1e-12) for s in _ensemble_entries(report))
    snap = telemetry.snapshot()["counters"]
    assert snap.get("scan.ensemble_weights.skipped", 0.0) == base + 1.0


# ---------------------------------------------------------------------------
# 4. 权重归一化不变量(适配层单测,不动兄弟模块)
# ---------------------------------------------------------------------------
def test_adapter_full_coverage_exact_normalized() -> None:
    """全员样本充足:输出即 tracker 归一权重,Σ = 1。"""
    weights = orchestrator._ensemble_weights_from_tracker(
        ["good", "bad"], _dominance_tracker()
    )
    assert weights is not None
    assert weights == {"good": pytest.approx(W_GOOD, abs=1e-12),
                       "bad": pytest.approx(W_BAD, abs=1e-12)}
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-12)


def test_adapter_missing_member_mean_fill_ratio_preserved() -> None:
    """缺失成员按已知权重均值兜底后重归一:Σ = 1;已知成员间 21:1 比值不变。

    推导:known = {good: 21/22, bad: 1/22}(Σ=1),均值 = 0.5;
    merged 总和 = 1.5 → good = (21/22)/1.5 = 7/11,bad = 1/33,newbie = 1/3。
    """
    weights = orchestrator._ensemble_weights_from_tracker(
        ["good", "bad", "newbie"], _dominance_tracker()
    )
    assert weights is not None
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-12)
    assert weights["good"] == pytest.approx(7.0 / 11.0, abs=1e-12)
    assert weights["bad"] == pytest.approx(1.0 / 33.0, abs=1e-12)
    assert weights["newbie"] == pytest.approx(1.0 / 3.0, abs=1e-12)  # 均值兜底
    assert weights["good"] / weights["bad"] == pytest.approx(21.0, abs=1e-9)


def test_adapter_insufficient_member_gets_neutral_weight() -> None:
    """样本不足的成员(weights()= None)同样按均值兜底:与唯一充足成员等权。"""
    tracker = ReliabilityTracker()
    for _ in range(6):
        tracker.record("good", 1.0, True)
    for _ in range(MIN_N - 1):  # weak 样本不足
        tracker.record("weak", 1.0, False)
    assert tracker.weights() == {"good": 1.0, "weak": None}

    weights = orchestrator._ensemble_weights_from_tracker(["good", "weak"], tracker)
    assert weights == {"good": pytest.approx(0.5, abs=1e-12),
                       "weak": pytest.approx(0.5, abs=1e-12)}
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-12)


def test_adapter_unrelated_providers_degenerate_equal() -> None:
    """tracker 只有不相关的提供方:全员均值兜底 → 退化为等权(Σ 仍 = 1)。"""
    tracker = ReliabilityTracker()
    for _ in range(6):
        tracker.record("glm", 1.0, True)
    weights = orchestrator._ensemble_weights_from_tracker(["stub", "clip"], tracker)
    assert weights == {"stub": pytest.approx(0.5, abs=1e-12),
                       "clip": pytest.approx(0.5, abs=1e-12)}


def test_adapter_all_insufficient_or_empty_returns_none() -> None:
    """全员样本不足 / 空成员列表 → None(调用方回退等权)。"""
    assert orchestrator._ensemble_weights_from_tracker(["good"], _thin_tracker()) is None
    assert (
        orchestrator._ensemble_weights_from_tracker(["good"], ReliabilityTracker())
        is None
    )
    assert orchestrator._ensemble_weights_from_tracker([], _dominance_tracker()) is None


def test_bayes_adapter_all_insufficient_or_empty_returns_none() -> None:
    """bayes 形状下适配层的 None 语义:仅零事件流(空 tracker)触发;
    成员列表为空同样 None(调用方回退等权)。"""
    empty = BayesianReliabilityTracker(half_life=None)
    assert orchestrator._ensemble_weights_from_tracker(["good"], empty) is None
    assert orchestrator._ensemble_weights_from_tracker([], _bayes_dominance_tracker()) is None


def test_bayes_adapter_missing_member_mean_fill() -> None:
    """V10.4 适配层不变量对 bayes 形状(weights() 恒数值、无 None)同样成立:
    缺失成员按已知权重均值兜底(= 0.5)、Σ = 1、已知成员 41:23 比值不变。

    推导:known = {good: 41/64, bad: 23/64}(Σ=1),均值 = 0.5;
    merged 总和 = 1.5 → good = (41/64)/1.5 = 41/96,bad = 23/96,newbie = 1/3。
    """
    weights = orchestrator._ensemble_weights_from_tracker(
        ["good", "bad", "newbie"], _bayes_dominance_tracker()
    )
    assert weights is not None
    assert weights["good"] == pytest.approx(41.0 / 96.0, abs=1e-12)
    assert weights["bad"] == pytest.approx(23.0 / 96.0, abs=1e-12)
    assert weights["newbie"] == pytest.approx(1.0 / 3.0, abs=1e-12)  # 均值兜底
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-12)
    assert weights["good"] / weights["bad"] == pytest.approx(41.0 / 23.0, abs=1e-9)


# ---------------------------------------------------------------------------
# 5. 贝叶斯权重回流(cfg.bayes_reliability 附加属性,优先于 Brier 开关)
# ---------------------------------------------------------------------------
def test_bayes_switch_is_attachment_default_off() -> None:
    """bayes_reliability 为 Config 契约外附加属性(V14 收录前):缺省
    getattr False = 现状等权(与 graph_wire / abstain_enabled 同款注入惯例)。"""
    assert getattr(Config(), "bayes_reliability", False) is False
    cfg = Config()
    cfg.bayes_reliability = True  # 实例属性注入即开
    assert getattr(cfg, "bayes_reliability", False) is True


def test_bayes_priority_matrix_same_jsonl(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """开关优先级矩阵(同一份 reliability.jsonl 喂两条路径):

    ① 两开关全关 → 不传 weights(等权现状逐字节),无任何权重遥测增量;
    ② Brier 开 / bayes 关 → Brier 反比 21/22 : 1/22;
    ③ bayes 开 / Brier 关 → 贝叶斯收缩 41/64 : 23/64,计数 bayes +1;
    ④ 两开关同开 → **贝叶斯优先**:权重仍 41/64(21/22 被忽略),Brier
       工厂不被调用(bomb 不触发)、Brier 生效计数零增量。
    """
    _write_reliability_jsonl(
        cfg, [("good", 1.0, True), ("bad", 1.0, False)] * 6
    )

    def counters() -> dict[str, float]:
        return telemetry.snapshot()["counters"]

    # ① 两开关全关:等权现状快照(不传 weights + 遥测零增量)
    base = counters()
    report, recorder = _run_two_member_scan(monkeypatch, cfg)
    assert recorder.calls[0][1] is _UNSET
    for s in _ensemble_entries(report):
        assert s.nsfw_prob == pytest.approx(0.5, abs=1e-12)
    for key in (
        "scan.ensemble_weights.bayes",
        "scan.ensemble_weights.bayes_fallback",
        "scan.ensemble_weights.reliability_weighted",
    ):
        assert counters().get(key, 0.0) == base.get(key, 0.0)

    # ② Brier 开 / bayes 关:Brier 反比权重
    cfg.ensemble_reliability_weights = True
    _report, recorder = _run_two_member_scan(monkeypatch, cfg)
    assert recorder.calls[0][1] == {
        "good": pytest.approx(W_GOOD, abs=1e-12),
        "bad": pytest.approx(W_BAD, abs=1e-12),
    }

    # ③ bayes 开 / Brier 关:贝叶斯收缩权重生效,集成分精确偏移
    cfg.ensemble_reliability_weights = False
    cfg.bayes_reliability = True
    base = counters()
    report, recorder = _run_two_member_scan(monkeypatch, cfg)
    weights = recorder.calls[0][1]
    assert weights is not _UNSET
    assert weights == {
        "good": pytest.approx(W_BAYES_GOOD, abs=1e-12),
        "bad": pytest.approx(W_BAYES_BAD, abs=1e-12),
    }
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-12)
    for s in _ensemble_entries(report):
        assert s.nsfw_prob == pytest.approx(W_BAYES_ENSEMBLE_PROB, abs=1e-12)
    assert report.agg_nsw_prob == pytest.approx(W_BAYES_ENSEMBLE_PROB, abs=1e-12)
    assert report.verdict is Verdict.CLEAN  # 0.415625 < review_threshold
    assert (
        counters().get("scan.ensemble_weights.bayes", 0.0)
        == base.get("scan.ensemble_weights.bayes", 0.0) + 1.0
    )

    # ④ 两开关同开:贝叶斯优先——Brier 工厂埋雷证明不被调用
    cfg.ensemble_reliability_weights = True

    def brier_bomb(c: Config) -> Any:
        raise AssertionError("bayes_reliability 开启时不应触碰 Brier 路径")

    monkeypatch.setattr(orchestrator, "_default_reliability_tracker", brier_bomb)
    base = counters()
    _report, recorder = _run_two_member_scan(monkeypatch, cfg)
    weights = recorder.calls[0][1]
    assert weights == {
        "good": pytest.approx(W_BAYES_GOOD, abs=1e-12),
        "bad": pytest.approx(W_BAYES_BAD, abs=1e-12),
    }
    assert (
        counters().get("scan.ensemble_weights.bayes", 0.0)
        == base.get("scan.ensemble_weights.bayes", 0.0) + 1.0
    )
    # Brier 生效 / 异常计数零增量:bomb 若被调用会走 skipped(+1)而非生效
    for key in ("scan.ensemble_weights.reliability_weighted",
                "scan.ensemble_weights.skipped"):
        assert counters().get(key, 0.0) == base.get(key, 0.0)


def test_bayes_default_tracker_maps_jsonl_correctness(cfg: Config) -> None:
    """数据源适配:Brier record 流 → correctness 布尔(``(p >= 0.5) == outcome``)。

    calm 低报正常图(p=0.3, outcome=false)与 crywolf 高报正常图(p=0.9,
    outcome=false)在 Brier 口径下损失不同,在 correctness 口径下分别为
    对 / 错——映射后 calm 权重 41/64 压倒 crywolf 23/64(与 good/bad 同构)。
    """
    _write_reliability_jsonl(
        cfg, [("calm", 0.3, False), ("crywolf", 0.9, False)] * 6
    )
    tracker = orchestrator._default_bayes_reliability_tracker(cfg)
    assert isinstance(tracker, BayesianReliabilityTracker)
    weights = tracker.weights()
    assert weights == {
        "calm": pytest.approx(W_BAYES_GOOD, abs=1e-12),
        "crywolf": pytest.approx(W_BAYES_BAD, abs=1e-12),
    }
    stats = tracker.stats()  # 佐证:各 6 条,遗忘关闭(n_eff = 原始条数)
    assert stats["calm"]["n"] == 6 and stats["crywolf"]["n"] == 6
    assert stats["calm"]["n_eff"] == 6.0


def test_bayes_default_tracker_skips_corrupt_lines(cfg: Config) -> None:
    """坏行(非 JSON / 缺键 / 空名 / 类型非法 / NaN)逐行跳过,不拖垮重放。"""
    _write_reliability_jsonl(cfg, [("good", 1.0, True)] * 6)
    path = pathlib.Path(cfg.data_dir) / orchestrator.RELIABILITY_JSONL_NAME
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not json\n")
        fh.write('{"provider": "x"}\n')                                # 缺 p/outcome
        fh.write('{"provider": "", "p": 0.9, "outcome": true}\n')      # 空 provider
        fh.write('{"provider": "y", "p": "high", "outcome": true}\n')  # p 非数字
        fh.write('{"provider": "z", "p": 0.9, "outcome": "yes"}\n')    # outcome 非布尔
        fh.write('{"p": 0.9, "outcome": true}\n')                      # 缺 provider
        fh.write('{"provider": "n", "p": NaN, "outcome": true}\n')     # NaN 概率
    tracker = orchestrator._default_bayes_reliability_tracker(cfg)
    assert set(tracker.stats()) == {"good"}
    assert tracker.weights() == {"good": 1.0}


def test_bayes_cold_start_shrinks_to_equal_weights(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """冷启动回退:全员 n_eff < MIN_N(3 对 / 3 错)→ V13 收缩把两成员完全
    拉平到池均值 → 权重 0.5/0.5、集成分与等权现状一致(0.5)。"""
    _write_reliability_jsonl(cfg, [("good", 1.0, True), ("bad", 1.0, False)] * 3)
    cfg.bayes_reliability = True
    base = telemetry.snapshot()["counters"].get("scan.ensemble_weights.bayes", 0.0)

    report, recorder = _run_two_member_scan(monkeypatch, cfg)

    weights = recorder.calls[0][1]
    assert weights is not _UNSET  # 权重仍传递(数值恰为等权)
    assert weights == {
        "good": pytest.approx(0.5, abs=1e-12),
        "bad": pytest.approx(0.5, abs=1e-12),
    }
    for s in _ensemble_entries(report):
        assert s.nsfw_prob == pytest.approx(0.5, abs=1e-12)
    assert report.verdict is Verdict.SUSPECT  # 0.5 >= review_threshold
    snap = telemetry.snapshot()["counters"]
    assert snap.get("scan.ensemble_weights.bayes", 0.0) == base + 1.0


def test_bayes_zero_records_falls_back_equal_unset(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """bayes 开但反馈零记录(jsonl 缺失):适配层 None → 不传 weights 形参
    (等权调用口径与旧版一致),计数 bayes_fallback +1。"""
    cfg.bayes_reliability = True
    base_fallback = telemetry.snapshot()["counters"].get(
        "scan.ensemble_weights.bayes_fallback", 0.0
    )
    base_effective = telemetry.snapshot()["counters"].get(
        "scan.ensemble_weights.bayes", 0.0
    )

    report, recorder = _run_two_member_scan(monkeypatch, cfg)

    assert recorder.calls[0][1] is _UNSET
    for s in _ensemble_entries(report):
        assert s.nsfw_prob == pytest.approx(0.5, abs=1e-12)
    snap = telemetry.snapshot()["counters"]
    assert snap.get("scan.ensemble_weights.bayes_fallback", 0.0) == base_fallback + 1.0
    # bayes 生效计数不增(零记录不是"生效")
    assert snap.get("scan.ensemble_weights.bayes", 0.0) == base_effective


def test_bayes_tracker_failure_degrades_to_equal(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """bayes 开但内核未就位 / tracker 构造失败:告警 + skipped 计数 + 降级
    等权,扫描不中断(与 Brier 路径同语义共用 skipped 计数)。"""
    cfg.bayes_reliability = True

    def broken(c: Config) -> Any:
        raise RuntimeError("模块 netsentinel.decision.reliability 未就位:模拟缺失")

    monkeypatch.setattr(orchestrator, "_default_bayes_reliability_tracker", broken)
    base = telemetry.snapshot()["counters"].get("scan.ensemble_weights.skipped", 0.0)

    report, recorder = _run_two_member_scan(monkeypatch, cfg)

    assert recorder.calls[0][1] is _UNSET
    for s in _ensemble_entries(report):
        assert s.nsfw_prob == pytest.approx(0.5, abs=1e-12)
    snap = telemetry.snapshot()["counters"]
    assert snap.get("scan.ensemble_weights.skipped", 0.0) == base + 1.0


# ---------------------------------------------------------------------------
# 配置开关本身:load / save
# ---------------------------------------------------------------------------
def test_config_switch_load_true_false_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """YAML 键读为一等字段(V11 升格);不写该键 = 默认 False;不是未知键(无告警)。"""
    import netsentinel.config as config

    monkeypatch.chdir(tmp_path)
    config._reset_unknown_key_warnings()
    p = tmp_path / "config.yaml"

    p.write_text("ensemble_reliability_weights: true\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="netsentinel.config"):
        cfg = config.load_config(str(p))
    assert getattr(cfg, "ensemble_reliability_weights", False) is True
    assert "未知配置键" not in caplog.text  # 受支持的开关,不走未知键告警

    p.write_text("ensemble_reliability_weights: false\n", encoding="utf-8")
    cfg = config.load_config(str(p))
    assert cfg.ensemble_reliability_weights is False  # 显式 false 覆盖默认

    p.write_text("max_pages: 3\n", encoding="utf-8")  # 旧配置:无该键
    cfg = config.load_config(str(p))
    assert cfg.ensemble_reliability_weights is False  # 缺键 = 默认 False(等权现状)
    assert cfg.max_pages == 3


def test_config_switch_invalid_type_raises_chinese(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """非布尔值 → 中文 ValueError(含键名)。"""
    import netsentinel.config as config

    monkeypatch.chdir(tmp_path)
    p = tmp_path / "config.yaml"
    p.write_text("ensemble_reliability_weights: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ensemble_reliability_weights"):
        config.load_config(str(p))


def test_config_switch_save_roundtrip_and_default_persist(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """V11 升格后按一等字段惯例落盘:默认关闭写 false、显式开启写 true;
    读回等值(往返兼容)。"""
    import netsentinel.config as config

    monkeypatch.chdir(tmp_path)

    target = tmp_path / "default.yaml"
    config.save_config(Config(), str(target))
    assert "ensemble_reliability_weights: false" in target.read_text(encoding="utf-8")

    cfg = Config()
    cfg.ensemble_reliability_weights = True
    target_on = tmp_path / "on.yaml"
    config.save_config(cfg, str(target_on))
    text = target_on.read_text(encoding="utf-8")
    assert "ensemble_reliability_weights: true" in text

    loaded = config.load_config(str(target_on))
    assert getattr(loaded, "ensemble_reliability_weights", False) is True
    assert loaded == Config(ensemble_reliability_weights=True)  # 一等字段往返等值


# ---------------------------------------------------------------------------
# 6. V14 收录:bayes_reliability/bayes_half_life 一等字段 + ts 驱动的遗忘重放
# ---------------------------------------------------------------------------
def test_bayes_switch_promoted_to_first_class_fields() -> None:
    """V14 收录后两键为 Config 一等字段:默认 False/None = 收录前 getattr
    缺省现状;实例属性注入(A223 测试写法)依旧可用。"""
    cfg = Config()
    assert cfg.bayes_reliability is False
    assert cfg.bayes_half_life is None
    # 消费方 getattr 读取路径命中字段(缺省语义与收录前逐字一致)
    assert getattr(cfg, "bayes_reliability", False) is False
    assert getattr(cfg, "bayes_half_life", None) is None
    cfg.bayes_reliability = True  # 实例属性注入即开(收录前惯例继续可用)
    cfg.bayes_half_life = 7.0
    assert getattr(cfg, "bayes_reliability", False) is True
    assert getattr(cfg, "bayes_half_life", None) == 7.0


def test_bayes_ts_jsonl_forgetting_replay_reversal(cfg: Config) -> None:
    """带 ts 的 jsonl + 短半衰期 → 指数遗忘重放生效:同计数反时序结论翻转
    (对照 A216 test_bayes_forgetting_time_reversal_flips_conclusion 手法)。

    构造:6 对(旧,ts=0)+ 2 错(新,ts=10 天),half_life=2 天 →
    旧对衰减 6·2^-5 = 6/32,新错权重 1 → α=1.1875、β=3.0、mean≈0.2836;
    镜像(旧错新对)mean≈0.7164,两者互为 1-x;同数据半衰期 None(现状)
    → mean = 7/10(遗忘关闭逐字节现状)。
    """
    day = 86400.0
    rows_old_first = [("m", 1.0, True, 0.0)] * 6 + [("m", 1.0, False, 10 * day)] * 2
    rows_new_first = [("m", 1.0, False, 0.0)] * 6 + [("m", 1.0, True, 10 * day)] * 2

    off_cfg = _sibling_cfg(cfg, "off")  # 半衰期 None = 关闭遗忘(现状)
    _write_reliability_jsonl_ts(off_cfg, rows_old_first)
    t_off = orchestrator._default_bayes_reliability_tracker(off_cfg)
    assert t_off.half_life is None
    assert t_off.posterior()["m"]["mean"] == pytest.approx(0.7, abs=1e-12)

    on_cfg = _sibling_cfg(cfg, "on")
    _write_reliability_jsonl_ts(on_cfg, rows_old_first)
    on_cfg.bayes_half_life = 2.0
    t_old = orchestrator._default_bayes_reliability_tracker(on_cfg)
    assert t_old.half_life == pytest.approx(2.0 * day)  # 天 → 秒换算对齐 ts 量纲
    s_old = t_old.stats()["m"]
    assert s_old["alpha"] == pytest.approx(1.1875, abs=1e-12)  # 1 + 6/32
    assert s_old["beta"] == pytest.approx(3.0, abs=1e-12)      # 1 + 2(新错不衰减)
    assert s_old["n_eff"] == pytest.approx(2.1875, abs=1e-12)

    mirror_cfg = _sibling_cfg(cfg, "mirror")
    _write_reliability_jsonl_ts(mirror_cfg, rows_new_first)
    mirror_cfg.bayes_half_life = 2.0
    t_new = orchestrator._default_bayes_reliability_tracker(mirror_cfg)

    m_old = t_old.posterior()["m"]["mean"]
    m_new = t_new.posterior()["m"]["mean"]
    # 旧观测衰减殆尽:前者只剩"新错"主导,后者只剩"新对"主导(结论翻转)
    assert m_old < 0.4 < 0.6 < m_new
    assert m_new == pytest.approx(1.0 - m_old, abs=1e-12)  # 对称镜像


def test_bayes_half_life_none_ignores_ts_rows(cfg: Config) -> None:
    """半衰期缺省 None:即使行带 ts 也关闭遗忘(现状),权重与无 ts 数据
    的 41/64 : 23/64 手算逐位一致(ts 行照常重放,只是不做衰减)。"""
    _write_reliability_jsonl_ts(
        cfg, [("good", 1.0, True, 0.0), ("bad", 1.0, False, 864000.0)] * 6
    )
    tracker = orchestrator._default_bayes_reliability_tracker(cfg)
    assert tracker.half_life is None
    assert tracker.decay_evals == 0  # 遗忘关闭不评估 exp(操作计数现状)
    weights = tracker.weights()
    assert weights == {
        "good": pytest.approx(W_BAYES_GOOD, abs=1e-12),
        "bad": pytest.approx(W_BAYES_BAD, abs=1e-12),
    }


def test_bayes_missing_ts_rows_fall_back_to_tick(
    cfg: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """缺 ts 旧行回退确定性滴答(上一条 +1.0)并计数:全部行重放入账、
    n_eff 近满衰(滴答间隔相对 30 天半衰期可忽略)、debug 日志报告回退条数。"""
    _write_reliability_jsonl(
        cfg, [("good", 1.0, True)] * 6 + [("bad", 1.0, False)] * 6
    )
    cfg.bayes_half_life = 30.0
    with caplog.at_level(logging.DEBUG, logger="netsentinel.pipeline.orchestrator"):
        tracker = orchestrator._default_bayes_reliability_tracker(cfg)
    stats = tracker.stats()
    assert stats["good"]["n"] == 6 and stats["bad"]["n"] == 6  # 全量入账
    # 滴答 ts=1..12、as_of=12:Δt ≤ 11 秒 << 30 天半衰期 → n_eff ≈ 原始条数
    assert stats["good"]["n_eff"] == pytest.approx(6.0, abs=1e-4)
    assert stats["bad"]["n_eff"] == pytest.approx(6.0, abs=1e-4)
    assert any("缺 ts 回退滴答 12 条" in r for r in caplog.messages)


def test_bayes_mixed_ts_and_tick_rows_forgetting(cfg: Config) -> None:
    """混流(旧行无 ts / 新行带 ts):滴答时刻(1..6)在 epoch 秒量纲下距
    as_of(100 天)千万半衰期级 → 遗忘压到 ~0;新事件主导,双双冷启动
    收缩到池 → 等权(确定性,无异常)。"""
    _write_reliability_jsonl(cfg, [("good", 1.0, True)] * 6)  # 无 ts → 滴答
    _write_reliability_jsonl_ts(cfg, [("bad", 1.0, False, 100 * 86400.0)] * 2)
    cfg.bayes_half_life = 1.0
    tracker = orchestrator._default_bayes_reliability_tracker(cfg)
    stats = tracker.stats()
    assert stats["good"]["n_eff"] == pytest.approx(0.0, abs=1e-12)  # ~6·2^-100
    assert stats["bad"]["n_eff"] == pytest.approx(2.0, abs=1e-12)
    weights = tracker.weights()  # n_eff 双双 < MIN_N → 冷启动完全收缩 → 等权
    assert weights["good"] == pytest.approx(0.5, abs=1e-9)
    assert weights["bad"] == pytest.approx(0.5, abs=1e-9)


def test_bayes_default_tracker_skips_invalid_ts_rows(cfg: Config) -> None:
    """ts 存在但非法(字符串/NaN/布尔)→ 整行跳过;缺 ts 行照常重放。"""
    _write_reliability_jsonl(cfg, [("good", 1.0, True)] * 6)
    path = pathlib.Path(cfg.data_dir) / orchestrator.RELIABILITY_JSONL_NAME
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"provider": "a", "p": 1.0, "outcome": true, "ts": "soon"}\n')
        fh.write('{"provider": "b", "p": 1.0, "outcome": true, "ts": NaN}\n')
        fh.write('{"provider": "c", "p": 1.0, "outcome": true, "ts": true}\n')
    tracker = orchestrator._default_bayes_reliability_tracker(cfg)
    assert set(tracker.stats()) == {"good"}
    assert tracker.stats()["good"]["n"] == 6


def test_bayes_half_life_extreme_values(cfg: Config) -> None:
    """半衰期极端值边界:

    - 极小(1e-6 天 = 86.4 毫秒):非 as_of 事件的衰减 exp(-ln2·1e6) 双精度
      下溢为恰 0 → 远古成员 good n_eff=0(记忆清零,先验裸值 α=β=1),
      冷启动 λ=1 与 bad 的连续收缩都收敛到同一池均值 → 权重恰 0.5/0.5
      (手算:池 = (1+0)/(2+6) = 1/8,bad mean_raw 同为 1/8,收缩不动中心);
    - 极大(1e9 天):衰减 ≈ 1 → n_eff ≈ 原始条数,权重与关闭遗忘的
      41/64 : 23/64 手算一致(1e-9 内)。
    """
    events = [("good", 1.0, True, 0.0)] * 6 + [("bad", 1.0, False, 86400.0)] * 6

    small_cfg = _sibling_cfg(cfg, "small")
    _write_reliability_jsonl_ts(small_cfg, events)
    small_cfg.bayes_half_life = 1e-6
    small = orchestrator._default_bayes_reliability_tracker(small_cfg)
    s = small.stats()
    assert s["good"]["n_eff"] == 0.0  # 下溢为恰 0(非近似)
    assert s["good"]["alpha"] == 1.0 and s["good"]["beta"] == 1.0  # 先验裸值
    assert s["bad"]["n_eff"] == 6.0  # as_of 处事件 Δt=0,权重全保留
    post = small.posterior()
    assert post["good"]["shrink"] == 1.0  # n_eff=0 → 冷启动完全收缩
    assert post["bad"]["pool_mean"] == pytest.approx(0.125, abs=1e-12)
    assert post["bad"]["mean"] == pytest.approx(0.125, abs=1e-12)  # 收缩不动中心
    w = small.weights()
    assert w["good"] == pytest.approx(0.5, abs=1e-12)  # 远古成员零信息 → 等权
    assert w["bad"] == pytest.approx(0.5, abs=1e-12)
    assert sum(w.values()) == pytest.approx(1.0, abs=1e-12)

    big_cfg = _sibling_cfg(cfg, "big")
    _write_reliability_jsonl_ts(big_cfg, events)
    big_cfg.bayes_half_life = 1e9
    big = orchestrator._default_bayes_reliability_tracker(big_cfg)
    s2 = big.stats()
    assert s2["good"]["n_eff"] == pytest.approx(6.0, abs=1e-8)  # 衰减 ≈ 1
    # 极大半衰期 ≈ 关闭遗忘:6 对 / 6 错的经典构造 → 权重回 41/64 : 23/64
    w2 = big.weights()
    assert w2["good"] == pytest.approx(W_BAYES_GOOD, abs=1e-9)
    assert w2["bad"] == pytest.approx(W_BAYES_BAD, abs=1e-9)


def test_bayes_half_life_requires_switch(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """bayes_half_life 只在 bayes_reliability 开启时被消费:两开关全关 +
    显式半衰期 → 仍等权现状(不传 weights 形参,bayes 生效计数零增量)。"""
    _write_reliability_jsonl_ts(
        cfg,
        [("good", 1.0, True, 0.0)] * 6 + [("bad", 1.0, False, 864000.0)] * 6,
    )
    cfg.bayes_half_life = 2.0  # 开关全关:半衰期不被消费
    base = telemetry.snapshot()["counters"].get("scan.ensemble_weights.bayes", 0.0)

    report, recorder = _run_two_member_scan(monkeypatch, cfg)

    assert recorder.calls[0][1] is _UNSET  # 等权:调用口径与旧版一致
    for s in _ensemble_entries(report):
        assert s.nsfw_prob == pytest.approx(0.5, abs=1e-12)
    snap = telemetry.snapshot()["counters"]
    assert snap.get("scan.ensemble_weights.bayes", 0.0) == base


def test_bayes_forgetting_drift_weights_end_to_end(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """遗忘重开端到端(V10.4 漂移痛点,A223 后续):bad 曾长期全对(100 条,
    ts=0)近期连错 20 条(ts=10 天),good 近期 20 对。

    - half_life=2 天:百年功业衰减 2^-5 → bad 旧对仅剩 3.125 等效,
      good 权重份额显著抬升(> 0.7),bad 回落(< 0.3);
    - half_life=None(现状):120 条历史一视同仁 → bad 份额仍近半(≈0.47);
    - jsonl+ts 重放与直接构造 BayesianReliabilityTracker(同事件流)权重
      逐位一致(数据源适配层等价性)。
    """
    day = 86400.0
    rows = (
        [("good", 1.0, True, 10 * day)] * 20
        + [("bad", 1.0, True, 0.0)] * 100
        + [("bad", 1.0, False, 10 * day)] * 20
    )

    forget_cfg = _sibling_cfg(cfg, "forget")
    _write_reliability_jsonl_ts(forget_cfg, rows)
    forget_cfg.bayes_reliability = True
    forget_cfg.bayes_half_life = 2.0
    t_forget = orchestrator._default_bayes_reliability_tracker(forget_cfg)
    w_forget = t_forget.weights()
    assert w_forget["good"] > 0.7 and w_forget["bad"] < 0.3

    plain_cfg = _sibling_cfg(cfg, "plain")
    _write_reliability_jsonl_ts(plain_cfg, rows)
    plain_cfg.bayes_reliability = True  # half_life None = 关闭遗忘(现状)
    t_plain = orchestrator._default_bayes_reliability_tracker(plain_cfg)
    w_plain = t_plain.weights()
    assert abs(w_plain["bad"] - 0.474) < 0.01  # 旧历史拖住权重(滞后可见)

    # 漂移感知:遗忘使 good 份额比现状口径高 0.2 以上
    assert w_forget["good"] - w_plain["good"] > 0.2

    # 等价性:jsonl+ts 重放 == 直接构造的同事件流 tracker(独立路径对账)
    direct = BayesianReliabilityTracker(half_life=2.0 * day)
    for provider, prob, outcome, ts in rows:
        correct = (prob >= 0.5) == outcome
        direct.record(provider, correct, ts=ts)
    assert t_forget.weights() == pytest.approx(direct.weights(), abs=1e-12)

    # 主链路:遗忘权重真实流入 ensemble(good=0.2 / bad=0.8)
    report, recorder = _run_two_member_scan(monkeypatch, forget_cfg)
    weights = recorder.calls[0][1]
    assert weights is not _UNSET
    assert weights["good"] == pytest.approx(w_forget["good"], abs=1e-12)
    assert weights["bad"] == pytest.approx(w_forget["bad"], abs=1e-12)
    for s in _ensemble_entries(report):
        expected = 0.2 * w_forget["good"] + 0.8 * w_forget["bad"]
        assert s.nsfw_prob == pytest.approx(expected, abs=1e-12)
    assert report.verdict is Verdict.CLEAN  # 集成分显著低于等权 0.5
