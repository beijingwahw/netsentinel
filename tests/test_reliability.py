"""A126 netsentinel.decision.reliability / fusion_reliable 可靠性加权融合测试。

纯构造数据,离线、零外呼。覆盖(CONTRACTS-V7 §2 A126、红线 29/30/31):

ReliabilityTracker:
- Brier 手算对照((p-outcome)² 均值,逐条写出推导);
- 样本不足(n < min_n)权重 None;min_n 参数阈值;
- 权重归一(样本充足者 Σ=1,不足者不进分母);
- 坏提供方低权(完美 brier=0 vs 全错 brier=1 → 21/22 : 1/22 精确值);
- 非法输入中文 ValueError(空 provider / 非数字 / NaN / bool / 非布尔 outcome);
- p 越界钳制 [0,1];
- jsonl 往返(跨实例读回一致、追加累积)与坏行跳过(7 类脏行);
- 线程安全(8 线程 × 25 条并发 record,计数与文件行数零丢失);
- kernel_selfcheck(A138 约定字段)。

fuse_reliable:
- 等权时数值与 fusion.fuse 完全一致(单图分歧成员 / 多图同分两场景,
  prob/raw_prob/contrib/weights_used/verdict 逐项对照断言);
- tracker=None 等权回退;无成员分回退 agg_nsw_prob(带 tracker 也回退);
- 高可靠成员主导(分歧分值 + 21/22 权重 → agg=17/22 精确数值);
- 样本不足成员按已知权重均值回退(0.75/0.25/0.5 → agg=2/3 精确数值);
- 只升不降:NSFW 不被可靠性重加权洗白(档位钳制)、needs_review 单向、
  融合概率不低于可靠性口径图像分;
- 不改既有字段:agg_nsw_prob / nsw_image_count / image_scores 原值不动;
- intel 结构:agg_reliable / member_weights / rule 新键;
- 冒号前缀分组(openai:两型号 → 同一提供方)与 ensemble 条目跳过;
- 无关 tracker(权重不含本站成员)等权回退;
- 遥测分列(fusion.fuse_reliable 计时 / fusion.escalated_reliable 升级,
  不污染既有 fusion.escalated);
- test_v7_bench_weight_shift:喂 20 条反馈后同一分值集合的加权 agg 从 0.5
  方向性移动到 19/22(精确数值 + 操作计数,零墙钟,红线 31)。

V12 增量(A216 贝叶斯分层可靠性,test_bayes_* 前缀):
- 后验手算对照(无遗忘 3 对 1 错 → α=4, β=2;8:0/0:8 收缩 → 61/90);
- 遗忘单调(固定时钟注入,as_of 推进后验均值单调;时间倒序翻转结论;
  衰减质量精确到 2^-5·6 = 0.1875;未来事件因果排除);
- 收缩单调(n_eff 大 → 成员数据主导,κ→∞ → 全局均值)与冷启动
  (n_eff < MIN_N → λ=1 完全收缩,3/3 完美新成员不吃幸运红利);
- 漂移仿真(前 200 对后 20 错,half_life=0.5:1 日内后验均值
  0.8865 → 0.4065 减半以上;同数据 Brier 反比份额 0.6395 滞后对比);
- CI 覆盖真值仿真(固定种子 20261003,20 成员 × 40 观测,宽松界);
- weights Σ=1 归一 / 消费接口形状(全数值、无 None);
- 非法输入中文 ValueError(成员/correct/ts/构造参数);
- 确定性(双实例逐字节一致)、操作计数(decay_evals / ci_probes)、
  8 线程并发 record、bayesian_kernel_selfcheck(A138 式字段)。

V15 增量(A242 贝叶斯事件流检查点化,test_ckpt_* 前缀):
- 增量 = 全量(无遗忘逐位相等;短半衰期容差 1e-12;随机流 500 事件
  固定种子全网格 as_of 对照);
- 乱序 ts 的因果排除语义一致(冷路径回退基类重放逐位相等、缓冲期
  待折叠扫描逐位相等、折叠后热路径容差相等);
- 快照 save/load 往返(状态 bitwise 恢复、JSON 结构、继续 record 后
  与全量重放一致、空快照、确定性字节输出);
- 快照校验拒坏(格式/版本/半衰期/κ 不一致、字段非法、重复成员、
  缺文件 FileNotFoundError);
- as_of < 锚点:保留全量事件时精确回退,快照加载后 / keep_history=False
  显式中文 ValueError;
- 操作计数(折叠 + 重锚 + 查询的 exp 记账,精确数值);
- 性能:1e5 事件流(固定种子)weights() < 1s、对照全量重放加速比
  >= 50x(墙钟 min-of-3 + 确定性操作计数比例),权重一致 <= 1e-12;
- 接口同形:isinstance 基类、fuse_reliable(bayes_tracker=...) 与基类
  实例产出一致、构造参数校验、8 线程并发 record。
"""
from __future__ import annotations

import json
import math
import random
import threading
import time

import pytest

from netsentinel import telemetry
from netsentinel.contracts import (
    Config,
    ImageEvidence,
    ImageScore,
    PageSample,
    SiteReport,
    Verdict,
)
from netsentinel.decision.fusion import fuse
from netsentinel.decision.fusion_reliable import fuse_reliable
from netsentinel.decision.reliability import (
    EPSILON,
    MIN_N,
    DEFAULT_HALF_LIFE,
    DEFAULT_KAPPA,
    DEFAULT_FOLD_EVERY,
    BayesianReliabilityTracker,
    CheckpointedBayesianReliabilityTracker,
    ReliabilityTracker,
    kernel_selfcheck,
    bayesian_kernel_selfcheck,
)
from netsentinel.decision.verdict import assess
from netsentinel.vision.ensemble import ensemble_scores

SITE = "https://example.test/"


def sig(x: float) -> float:
    """测试侧独立实现的 sigmoid(手算对照用,不复用被测代码)。"""
    return 1.0 / (1.0 + math.exp(-x))


# ---------------------------------------------------------------------------
# 辅助工厂(与 test_fusion.py 同款,仅内存数据,不落盘)
# ---------------------------------------------------------------------------


def make_evidence(name: str = "img.png", width: int = 250, height: int = 250) -> ImageEvidence:
    return ImageEvidence(
        path=f"data/evidence/{name}",
        url=f"{SITE}images/{name}",
        source_page=SITE,
        width=width,
        height=height,
    )


def member_score(model: str, prob: float, name: str = "img.png") -> ImageScore:
    return ImageScore(image=make_evidence(name), model=model, nsfw_prob=prob)


def build_report(
    ens_entries: list[ImageScore], members: list[ImageScore] = (), cfg: Config | None = None
) -> SiteReport:
    """assess 产出图像侧结论后,把成员分并入 image_scores(装配线语义)。

    传 list(ens_entries) 拷贝:assess 直接持有传入列表,避免测试间共享可变列表。
    """
    pages = [PageSample(url=SITE, screenshot_path="data/shots/home.png")]
    report = assess(SITE, pages, list(ens_entries), cfg or Config())
    report.image_scores.extend(members)
    return report


def dominance_tracker(good: str = "glm", bad: str = "stub", n: int = 6) -> ReliabilityTracker:
    """完美 vs 全错的确定性 tracker:brier 0 / 1 → 权重 21/22 : 1/22。

    推导:raw(good) = 1/(0+0.05) = 20;raw(bad) = 1/(1+0.05) = 20/21;
    归一 w(good) = 20/(20+20/21) = 21/22 ≈ 0.9545,w(bad) = 1/22 ≈ 0.0455。
    """
    tracker = ReliabilityTracker()
    for _ in range(n):
        tracker.record(good, 1.0, True)  # 报 1.0 且确为色情:完美校准
        tracker.record(bad, 1.0, False)  # 报 1.0 实为正常图:全错
    return tracker


def zero_aux() -> tuple[dict, dict, dict]:
    return {"risk": 0.0, "explain": []}, {"risk": 0.0}, {"page_nsfw_prob": 0.0}


def high_aux() -> tuple[dict, dict, dict]:
    return {"risk": 1.0}, {"risk": 1.0}, {"page_nsfw_prob": 1.0}


# ===========================================================================
# ReliabilityTracker:Brier 统计
# ===========================================================================


def test_brier_hand_computed() -> None:
    """Brier=(p-outcome)² 均值,手算:glm 四条 → (0.04+0.04+0.81+0.25)/4 = 0.285。"""
    tracker = ReliabilityTracker()
    tracker.record("glm", 0.8, True)    # (0.8-1)² = 0.04
    tracker.record("glm", 0.2, False)   # (0.2-0)² = 0.04
    tracker.record("glm", 0.9, False)   # (0.9-0)² = 0.81
    tracker.record("glm", 0.5, True)    # (0.5-1)² = 0.25
    tracker.record("stub", 1.0, False)  # (1-0)² = 1.0

    stats = tracker.stats()
    assert set(stats) == {"glm", "stub"}
    assert stats["glm"]["n"] == 4
    assert stats["glm"]["brier"] == pytest.approx(0.285, abs=1e-12)
    assert stats["stub"] == {"n": 1, "brier": 1.0}


def test_record_returns_entry_copy() -> None:
    """record 返回 {"provider","p","outcome"} 副本,改返回值不影响内部状态。"""
    tracker = ReliabilityTracker()
    entry = tracker.record("glm", 0.9, True)
    assert entry == {"provider": "glm", "p": 0.9, "outcome": True}
    entry["provider"] = "hacked"
    entry["p"] = 0.0
    assert tracker.stats() == {"glm": {"n": 1, "brier": pytest.approx(0.01, abs=1e-12)}}


def test_empty_tracker_stats_and_weights() -> None:
    """空 tracker:stats 与 weights 均为空 dict(无任何提供方可言)。"""
    tracker = ReliabilityTracker()
    assert tracker.stats() == {}
    assert tracker.weights() == {}


def test_insufficient_samples_none() -> None:
    """n=4 < MIN_N(5):权重 None,但 stats 照常统计。"""
    tracker = ReliabilityTracker()
    for _ in range(4):
        tracker.record("s", 1.0, True)
    assert MIN_N == 5
    assert tracker.weights() == {"s": None}
    assert tracker.stats()["s"]["n"] == 4


def test_min_n_parameter_threshold() -> None:
    """min_n 可调:同一份数据 min_n=4 → 有权,min_n=10 → None。"""
    tracker = ReliabilityTracker()
    for _ in range(4):
        tracker.record("s", 1.0, True)
    assert tracker.weights(min_n=4) == {"s": 1.0}
    assert tracker.weights(min_n=10) == {"s": None}


def test_weights_normalized_sum_one() -> None:
    """两个样本充足的提供方权重归一:Σ = 1(不足者不参与时亦然)。"""
    tracker = ReliabilityTracker()
    for _ in range(6):
        tracker.record("perfect", 1.0, True)   # brier 0
        tracker.record("noisy", 0.5, True)     # brier 0.25
    weights = tracker.weights()
    assert set(weights) == {"perfect", "noisy"}
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-12)
    assert weights["perfect"] > weights["noisy"]


def test_bad_provider_gets_low_weight_exact() -> None:
    """完美(brier=0)vs 全错(brier=1):w = 21/22 / 1/22 精确对照。"""
    tracker = dominance_tracker()
    weights = tracker.weights()
    w_good_expected = (1.0 / (0.0 + EPSILON)) / (
        1.0 / (0.0 + EPSILON) + 1.0 / (1.0 + EPSILON)
    )
    assert w_good_expected == pytest.approx(21.0 / 22.0, abs=1e-12)
    assert weights["glm"] == pytest.approx(21.0 / 22.0, abs=1e-12)
    assert weights["stub"] == pytest.approx(1.0 / 22.0, abs=1e-12)


def test_insufficient_excluded_from_normalization() -> None:
    """样本不足者值为 None 且不进归一化分母:充足者 Σ 仍 = 1。"""
    tracker = ReliabilityTracker()
    for _ in range(6):
        tracker.record("a", 1.0, True)
        tracker.record("b", 1.0, False)
    for _ in range(2):
        tracker.record("c", 1.0, True)
    weights = tracker.weights()
    assert weights["c"] is None
    assert weights["a"] + weights["b"] == pytest.approx(1.0, abs=1e-12)


def test_all_insufficient_all_none() -> None:
    """全部样本不足:全部 None(归一化无意义,调用方回退等权)。"""
    tracker = ReliabilityTracker()
    for _ in range(3):
        tracker.record("a", 0.9, True)
        tracker.record("b", 0.1, False)
    assert tracker.weights() == {"a": None, "b": None}


def test_invalid_inputs_raise_chinese_valueerror() -> None:
    """非法输入中文 ValueError:空 provider / 非数字 p / NaN / bool / 非布尔 outcome。"""
    tracker = ReliabilityTracker()
    with pytest.raises(ValueError, match="provider"):
        tracker.record("", 0.5, True)
    with pytest.raises(ValueError, match="p 必须"):
        tracker.record("glm", "high", True)
    with pytest.raises(ValueError, match="p 必须"):  # bool 不是合法概率
        tracker.record("glm", True, True)
    with pytest.raises(ValueError, match="NaN"):
        tracker.record("glm", float("nan"), True)
    with pytest.raises(ValueError, match="outcome"):
        tracker.record("glm", 0.5, "yes")
    with pytest.raises(ValueError, match="outcome"):
        tracker.record("glm", 0.5, 2)
    assert tracker.stats() == {}  # 一条都没记进去


def test_p_clamped_to_unit_interval() -> None:
    """p 越界钳进 [0,1]:p=1.5→1.0、p=-0.2→0.0,brier 均为 0。"""
    tracker = ReliabilityTracker()
    tracker.record("x", 1.5, True)
    tracker.record("x", -0.2, False)
    stats = tracker.stats()
    assert stats["x"]["n"] == 2
    assert stats["x"]["brier"] == 0.0


# ===========================================================================
# ReliabilityTracker:jsonl 持久化与线程安全
# ===========================================================================


def test_jsonl_roundtrip_across_instances(tmp_path) -> None:
    """jsonl 往返:新实例读回同款 stats/weights,追加跨实例累积。"""
    path = str(tmp_path / "reliability.jsonl")
    t1 = ReliabilityTracker(path)
    for _ in range(6):
        t1.record("glm", 1.0, True)
        t1.record("stub", 1.0, False)

    t2 = ReliabilityTracker(path)
    assert t2.stats() == t1.stats()
    assert t2.weights() == t1.weights()

    t2.record("glm", 1.0, True)  # 追加累积
    t3 = ReliabilityTracker(path)
    assert t3.stats()["glm"]["n"] == 7
    assert t3.stats()["stub"]["n"] == 6


def test_jsonl_corrupt_lines_skipped(tmp_path) -> None:
    """坏行跳过:非 JSON / 缺字段 / 值类型错 / 空 provider / JSON 数组 / 空行。"""
    path = tmp_path / "bad.jsonl"
    lines = [
        json.dumps({"provider": "glm", "p": 0.9, "outcome": True}),   # 好
        "<<<not-json>>>",                                              # 非 JSON
        json.dumps({"provider": "glm", "p": 0.9}),                     # 缺字段
        json.dumps({"provider": "glm", "p": "high", "outcome": True}),  # p 非数字
        json.dumps({"provider": "glm", "p": 0.9, "outcome": "yes"}),   # outcome 非布尔
        json.dumps({"provider": "   ", "p": 0.9, "outcome": False}),   # 空 provider
        "[1, 2, 3]",                                                   # JSON 数组
        "",                                                             # 空行
        json.dumps({"provider": "stub", "p": 1.0, "outcome": False}),  # 好
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    tracker = ReliabilityTracker(str(path))
    stats = tracker.stats()
    assert set(stats) == {"glm", "stub"}  # 只有两条好行被读回
    assert stats["glm"] == {"n": 1, "brier": pytest.approx(0.01, abs=1e-12)}
    assert stats["stub"] == {"n": 1, "brier": 1.0}


def test_thread_safety_concurrent_records(tmp_path) -> None:
    """8 线程 × 25 条并发 record:内存计数与 jsonl 行数零丢失。"""
    path = str(tmp_path / "threads.jsonl")
    tracker = ReliabilityTracker(path)
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for i in range(25):
                tracker.record(f"p{i % 3}", 0.8, i % 2 == 0)
        except Exception as exc:  # pragma: no cover —— 记录即失败
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    stats = tracker.stats()
    # 0..24 中 i%3==0 共 9 个、i%3==1/2 各 8 个;× 8 线程
    assert {p: s["n"] for p, s in stats.items()} == {"p0": 72, "p1": 64, "p2": 64}
    assert sum(s["n"] for s in stats.values()) == 200
    with open(path, encoding="utf-8") as fh:
        assert sum(1 for _ in fh) == 200
    # 读回也一致(文件没被写坏)
    assert ReliabilityTracker(path).stats() == stats


def test_kernel_selfcheck_fields() -> None:
    """kernel_selfcheck 返回 A138 约定四字段,值为确定性的 21/22。"""
    result = kernel_selfcheck()
    assert set(result) == {"name", "metric", "value", "baseline"}
    assert result["name"] == "reliability"
    assert result["value"] == pytest.approx(21.0 / 22.0, abs=1e-12)
    assert result["baseline"] == 0.5
    assert result["value"] > result["baseline"]  # 学习后好提供方权重压倒等权基线


# ===========================================================================
# fuse_reliable:等权回退 = 与 fusion.fuse 数值一致
# ===========================================================================


def test_equal_weights_single_image_matches_fuse() -> None:
    """单图分歧成员 + tracker=None:等权 agg=(0.8+0.2)/2=0.5,与 fuse 逐项一致。"""
    cfg = Config()
    members = [member_score("glm:glm-5.3", 0.8), member_score("stub", 0.2)]
    ens = ensemble_scores(members)
    assert len(ens) == 1 and ens[0].nsfw_prob == pytest.approx(0.5)

    r_fuse = build_report(ens, members, cfg)
    r_rel = build_report(ens, members, cfg)
    aux = ({"risk": 0.3}, {"risk": 0.2}, {"page_nsfw_prob": 0.6})

    fuse(r_fuse, *aux, cfg)
    ret = fuse_reliable(r_rel, *aux, cfg, tracker=None)
    assert ret is r_rel  # 就地更新,返回同一对象

    f_plain = r_fuse.intel["fusion"]
    f_rel = r_rel.intel["fusion"]
    assert f_rel["prob"] == f_plain["prob"]
    assert f_rel["raw_prob"] == f_plain["raw_prob"]
    assert f_rel["contrib"] == f_plain["contrib"]
    assert f_rel["weights_used"] == f_plain["weights_used"]
    assert r_rel.verdict is r_fuse.verdict is Verdict.SUSPECT
    assert r_rel.needs_review == r_fuse.needs_review
    assert f_rel["agg_reliable"] == pytest.approx(0.5)
    assert f_rel["member_weights"] == {"glm": 0.5, "stub": 0.5}
    assert f_rel["rule"] == "reliable-weighted 只升不降"


def test_equal_weights_multi_image_matches_fuse() -> None:
    """多图同分成员(3 图 × glm 0.6 / stub 0.8):等权均值 = agg(max)= 0.7。"""
    cfg = Config()
    members = [
        member_score("glm:glm-5.3", 0.6, f"img{i}.png") for i in range(3)
    ] + [member_score("stub", 0.8, f"img{i}.png") for i in range(3)]
    ens = ensemble_scores(members)
    r_fuse = build_report(ens, members, cfg)
    r_rel = build_report(ens, members, cfg)
    assert r_fuse.agg_nsw_prob == pytest.approx(0.7)  # max(每图均值 0.7)

    aux = ({"risk": 0.4}, {"risk": 0.5}, {"page_nsfw_prob": 0.9})
    fuse(r_fuse, *aux, cfg)
    fuse_reliable(r_rel, *aux, cfg, tracker=ReliabilityTracker())  # 空 tracker 同 None

    f_plain = r_fuse.intel["fusion"]
    f_rel = r_rel.intel["fusion"]
    assert f_rel["prob"] == f_plain["prob"]
    assert f_rel["contrib"] == f_plain["contrib"]
    assert f_rel["agg_reliable"] == pytest.approx(0.7, abs=1e-4)


def test_tracker_none_equal_weights() -> None:
    """tracker=None:三成员等权(各 1/3),数值与 fuse 一致。"""
    cfg = Config()
    members = [
        member_score("glm:glm-5.3", 0.6),
        member_score("stub", 0.8),
        member_score("nudenet", 0.7),
    ]
    ens = ensemble_scores(members)
    r_fuse = build_report(ens, members, cfg)
    r_rel = build_report(ens, members, cfg)

    fuse(r_fuse, *zero_aux(), cfg)
    fuse_reliable(r_rel, *zero_aux(), cfg)  # tracker 缺省 = None
    f_rel = r_rel.intel["fusion"]
    assert f_rel["prob"] == r_fuse.intel["fusion"]["prob"]
    assert f_rel["agg_reliable"] == pytest.approx(0.7, abs=1e-4)
    assert set(f_rel["member_weights"]) == {"glm", "stub", "nudenet"}
    assert sum(f_rel["member_weights"].values()) == pytest.approx(1.0, abs=1e-3)
    assert all(abs(w - 1.0 / 3.0) < 1e-3 for w in f_rel["member_weights"].values())


def test_no_member_scores_falls_back_to_fuse_behavior() -> None:
    """image_scores 只有 ensemble 条目:即使带了 tracker,也回退 agg_nsw_prob。"""
    cfg = Config()
    ens = [
        ImageScore(image=make_evidence(f"img{i}.png"), model="ensemble", nsfw_prob=p)
        for i, p in enumerate([0.6, 0.7, 0.8])
    ]
    r_fuse = build_report(ens, [], cfg)
    r_rel = build_report(ens, [], cfg)
    assert r_fuse.agg_nsw_prob == pytest.approx(0.8)  # max

    aux = ({"risk": 0.2}, {"risk": 0.1}, {"page_nsfw_prob": 0.4})
    fuse(r_fuse, *aux, cfg)
    fuse_reliable(r_rel, *aux, cfg, tracker=dominance_tracker())

    f_rel = r_rel.intel["fusion"]
    assert f_rel["prob"] == r_fuse.intel["fusion"]["prob"]
    assert f_rel["contrib"] == r_fuse.intel["fusion"]["contrib"]
    assert f_rel["agg_reliable"] == pytest.approx(0.8)  # 回退图像原值
    assert f_rel["member_weights"] == {}


# ===========================================================================
# fuse_reliable:可靠性加权主导(精确数值)
# ===========================================================================


def test_high_reliability_member_dominates_exact() -> None:
    """分歧分值 + 21/22 权重:agg=0.8*21/22+0.2/22=17/22 精确靠向高可靠方。"""
    cfg = Config()
    members = [member_score("glm:glm-5.3", 0.8), member_score("stub", 0.2)]
    ens = ensemble_scores(members)  # 单图 ensemble = 0.5
    report = build_report(ens, members, cfg)
    assert report.verdict is Verdict.SUSPECT  # agg 0.5

    fuse_reliable(report, *zero_aux(), cfg, tracker=dominance_tracker())
    fusion = report.intel["fusion"]

    # 手算:agg_reliable = 0.8*(21/22) + 0.2*(1/22) = (16.8+0.2)/22 = 17/22
    assert fusion["agg_reliable"] == pytest.approx(17.0 / 22.0, abs=1e-4)  # 0.7727
    assert fusion["member_weights"] == {"glm": 0.9545, "stub": 0.0455}
    # z = -2.0 + 2.2*(17/22) = -0.3 → fused = sigmoid(-0.3) ≈ 0.4256
    assert fusion["contrib"]["image"] == pytest.approx(1.7, abs=1e-4)
    assert fusion["raw_prob"] == pytest.approx(sig(-0.3), abs=1e-4)
    # 只升不降:prob = max(0.4256, 0.7727) = 0.7727
    assert fusion["prob"] == pytest.approx(17.0 / 22.0, abs=1e-4)
    assert report.verdict is Verdict.SUSPECT  # count 0 < 3,最高 SUSPECT
    assert report.needs_review is True
    # 方向性:加权 agg 从等权 0.5 靠向高可靠 glm 的 0.8
    assert abs(fusion["agg_reliable"] - 0.8) < abs(0.5 - 0.8)


def test_insufficient_member_fallback_mean_weight_exact() -> None:
    """样本不足成员按已知权重均值参与:0.75/0.25 + 回退 0.5 → agg = 2/3。"""
    tracker = ReliabilityTracker()
    for _ in range(5):
        tracker.record("alpha", 1.0, True)   # 5 条全对:brier 0 → raw 20
    tracker.record("beta", 0.5, False)       # 0.25
    tracker.record("beta", 0.5, False)       # 0.25
    for _ in range(3):
        tracker.record("beta", 0.0, False)   # 0 ×3 → brier = 0.5/5 = 0.1
    for _ in range(3):
        tracker.record("gamma", 0.5, True)   # n=3 < 5 → None
    # 归一:raw alpha=20,beta=1/0.15 → w = 0.75 / 0.25
    weights = tracker.weights()
    assert weights["alpha"] == pytest.approx(0.75, abs=1e-9)
    assert weights["beta"] == pytest.approx(0.25, abs=1e-9)
    assert weights["gamma"] is None

    cfg = Config()
    members = [
        member_score("alpha:x", 0.9),
        member_score("beta:y", 0.1),
        member_score("gamma:z", 0.6),
    ]
    ens = ensemble_scores(members)
    report = build_report(ens, members, cfg)
    fuse_reliable(report, *zero_aux(), cfg, tracker=tracker)
    fusion = report.intel["fusion"]

    # 手算:agg = (0.75*0.9 + 0.25*0.1 + 0.5*0.6) / (0.75+0.25+0.5) = 1.0/1.5 = 2/3
    assert fusion["agg_reliable"] == pytest.approx(2.0 / 3.0, abs=1e-4)  # 0.6667
    # member_weights 为归一话语权:0.75/0.25/0.5 → 0.5 / 1/6 / 1/3
    mw = fusion["member_weights"]
    assert mw["alpha"] == pytest.approx(0.5, abs=1e-4)
    assert mw["beta"] == pytest.approx(1.0 / 6.0, abs=1e-4)
    assert mw["gamma"] == pytest.approx(1.0 / 3.0, abs=1e-4)
    # 回退语义可见:gamma(样本不足,回退权 0.5)话语权恰为 beta(0.25)的两倍
    assert mw["gamma"] == pytest.approx(2 * mw["beta"], abs=1e-4)
    assert sum(mw.values()) == pytest.approx(1.0, abs=1e-4)


# ===========================================================================
# fuse_reliable:只升不降与既有字段不动
# ===========================================================================


def test_nsfw_not_whitewashed_verdict_floor() -> None:
    """图像 NSFW + 高可靠成员低分:融合分如实下移,但档位钳制保持 NSFW。

    手算:agg_reliable = 0.3*(21/22) + 0.98*(1/22) = 7.28/22 ≈ 0.3309;
    z = -2.0 + 2.2*0.3309 ≈ -1.272 → fused ≈ 0.2189;
    重算为 CLEAN,但原档 NSFW → 钳制回 NSFW,needs_review 恒 True。
    """
    cfg = Config()
    report = SiteReport(
        site_url=SITE,
        image_scores=[
            member_score("glm:glm-5.3", 0.3),
            member_score("stub", 0.98),
        ],
        agg_nsw_prob=0.95,
        nsw_image_count=4,
        verdict=Verdict.NSFW,
        needs_review=True,
    )
    fuse_reliable(report, *zero_aux(), cfg, tracker=dominance_tracker())
    fusion = report.intel["fusion"]

    assert fusion["agg_reliable"] == pytest.approx(7.28 / 22.0, abs=1e-4)  # 0.3309
    assert fusion["raw_prob"] == pytest.approx(sig(-1.272), abs=1e-3)
    assert fusion["prob"] == pytest.approx(7.28 / 22.0, abs=1e-4)  # 只升不降下限
    assert report.verdict is Verdict.NSFW  # 档位只升不降,可靠性重加权不能洗白
    assert report.needs_review is True
    assert report.agg_nsw_prob == pytest.approx(0.95)  # 图像原值不动


def test_needs_review_and_prob_floor_only_up() -> None:
    """needs_review 单向恒真;融合概率恒不低于可靠性口径图像分。"""
    cfg = Config()
    members = [member_score("glm:glm-5.3", 0.05), member_score("stub", 0.05)]
    ens = ensemble_scores(members)
    report = build_report(ens, members, cfg)
    assert report.verdict is Verdict.CLEAN
    report.needs_review = True  # 外部已标记复核

    fuse_reliable(report, *zero_aux(), cfg, tracker=dominance_tracker())
    fusion = report.intel["fusion"]
    assert report.verdict is Verdict.CLEAN
    assert report.needs_review is True  # 恒 True 保留原 True
    assert fusion["prob"] >= fusion["agg_reliable"]  # 概率下限 = 可靠性图像分


def test_report_image_fields_unchanged() -> None:
    """fuse_reliable 不改图像侧字段:agg/count/image_scores 及成员分原值不动。"""
    cfg = Config()
    members = [member_score("glm:glm-5.3", 0.8), member_score("stub", 0.2)]
    ens = ensemble_scores(members)
    report = build_report(ens, members, cfg)
    scores_before = [s.model for s in report.image_scores]
    probs_before = [s.nsfw_prob for s in report.image_scores]

    fuse_reliable(report, *zero_aux(), cfg, tracker=dominance_tracker())

    assert report.agg_nsw_prob == pytest.approx(0.5)  # 保持 ensemble 原值,非 0.7727
    assert report.nsw_image_count == 0
    assert [s.model for s in report.image_scores] == scores_before
    assert [s.nsfw_prob for s in report.image_scores] == probs_before


# ===========================================================================
# fuse_reliable:intel 结构 / 分组口径 / 遥测
# ===========================================================================


def test_intel_structure_rule_and_new_keys() -> None:
    """intel 结构:四键齐全 + fusion 新增 agg_reliable / member_weights / rule。"""
    cfg = Config()
    members = [member_score("glm:glm-5.3", 0.8), member_score("stub", 0.2)]
    ens = ensemble_scores(members)
    report = build_report(ens, members, cfg)
    url_feat, text_feat, page_vlm = ({"risk": 0.1}, {"risk": 0.2}, {"page_nsfw_prob": 0.3})

    fuse_reliable(report, url_feat, text_feat, page_vlm, cfg, tracker=dominance_tracker())

    intel = report.intel
    assert set(intel) == {"url", "text", "page_vlm", "fusion"}
    assert intel["url"] is url_feat  # 原文引用透传
    fusion = intel["fusion"]
    for key in ("prob", "raw_prob", "contrib", "weights_used", "rule",
                "agg_reliable", "member_weights"):
        assert key in fusion
    assert fusion["rule"] == "reliable-weighted 只升不降"
    assert isinstance(fusion["agg_reliable"], float)
    assert set(fusion["member_weights"]) == {"glm", "stub"}
    # as_dict() 在 intel 非空时输出
    d = report.as_dict()
    assert d["intel"]["fusion"]["agg_reliable"] == fusion["agg_reliable"]


def test_colon_prefix_grouping_and_ensemble_skip() -> None:
    """冒号前缀分组:openai 两个型号 → 同一提供方取均值;ensemble 条目跳过。"""
    cfg = Config()
    report = SiteReport(
        site_url=SITE,
        image_scores=[
            ImageScore(image=make_evidence("ens.png"), model="ensemble", nsfw_prob=0.99),
            member_score("openai:gpt-4o-mini", 0.10, "a.png"),
            member_score("openai:gpt-4o", 0.20, "b.png"),
            member_score("stub", 0.30, "c.png"),
        ],
        agg_nsw_prob=0.99,
        nsw_image_count=0,
        verdict=Verdict.SUSPECT,
        needs_review=True,
    )
    fuse_reliable(report, *zero_aux(), cfg)  # tracker=None → 等权
    fusion = report.intel["fusion"]

    # openai 均值 (0.10+0.20)/2 = 0.15,stub 0.30,等权 → (0.15+0.30)/2 = 0.225
    # ensemble 的 0.99 被跳过(若计入会得到完全不同的值)
    assert set(fusion["member_weights"]) == {"openai", "stub"}
    assert fusion["agg_reliable"] == pytest.approx(0.225, abs=1e-4)
    assert fusion["member_weights"] == {"openai": 0.5, "stub": 0.5}


def test_irrelevant_tracker_equal_fallback() -> None:
    """tracker 权重不含本站成员:全部回退等权(与旧行为一致)。"""
    cfg = Config()
    tracker = ReliabilityTracker()
    for _ in range(6):
        tracker.record("zzz", 1.0, True)
    assert tracker.weights() == {"zzz": 1.0}

    members = [member_score("glm:glm-5.3", 0.8), member_score("stub", 0.2)]
    ens = ensemble_scores(members)
    report = build_report(ens, members, cfg)
    fuse_reliable(report, *zero_aux(), cfg, tracker=tracker)
    fusion = report.intel["fusion"]
    assert fusion["agg_reliable"] == pytest.approx(0.5, abs=1e-4)
    assert fusion["member_weights"] == {"glm": 0.5, "stub": 0.5}


def test_telemetry_timer_and_escalation_isolated() -> None:
    """fusion.fuse_reliable 计时;升级计数走 fusion.escalated_reliable,
    不污染既有 fusion.escalated / fusion.fuse 指标。"""
    snap = telemetry.snapshot()
    esc_old = snap["counters"].get("fusion.escalated", 0.0)
    esc_rel = snap["counters"].get("fusion.escalated_reliable", 0.0)
    fuse_runs = snap["timers"].get("fusion.fuse", {}).get("count", 0)
    rel_runs = snap["timers"].get("fusion.fuse_reliable", {}).get("count", 0)

    # 无升级:NSFW + 辅助全零 → 档位不变
    cfg = Config()
    hot = [member_score("glm:glm-5.3", 0.95, f"hot{i}.png") for i in range(4)]
    hot += [member_score("stub", 0.95, f"hot{i}.png") for i in range(4)]
    ens_hot = ensemble_scores(hot)
    r_nsfw = build_report(ens_hot, hot, cfg)
    assert r_nsfw.verdict is Verdict.NSFW
    fuse_reliable(r_nsfw, *zero_aux(), cfg, tracker=dominance_tracker())
    assert r_nsfw.verdict is Verdict.NSFW

    # 升级:CLEAN + 辅助全高 → SUSPECT
    low = [member_score("glm:glm-5.3", 0.2), member_score("stub", 0.2)]
    ens_low = ensemble_scores(low)
    r_clean = build_report(ens_low, low, cfg)
    assert r_clean.verdict is Verdict.CLEAN
    fuse_reliable(r_clean, *high_aux(), cfg)
    assert r_clean.verdict is Verdict.SUSPECT

    after = telemetry.snapshot()
    assert after["counters"].get("fusion.escalated_reliable", 0.0) == esc_rel + 1.0
    assert after["counters"].get("fusion.escalated", 0.0) == esc_old  # 旧指标不动
    assert after["timers"]["fusion.fuse_reliable"]["count"] >= rel_runs + 2
    assert after["timers"].get("fusion.fuse", {}).get("count", 0) == fuse_runs


# ===========================================================================
# V7 基准(红线 31:确定性构造数据上的精确结果 + 操作计数,零墙钟)
# ===========================================================================


def test_v7_bench_weight_shift() -> None:
    """喂 20 条反馈后,同一分值集合的加权 agg 发生方向性移动。

    构造:10 图,vlm:big 全报 0.9、stub 全报 0.1(ensemble 每图 0.5,
    等权 agg = 0.5);喂反馈 vlm 10 条全对(brier=0)、stub 10 条全错
    (brier=1)→ 权重 21/22 : 1/22 → agg = 0.9*(21/22)+0.1*(1/22) = 19/22。
    """
    cfg = Config()
    members = [member_score("vlm:big", 0.9, f"b{i}.png") for i in range(10)]
    members += [member_score("stub", 0.1, f"b{i}.png") for i in range(10)]
    ens = ensemble_scores(members)

    tracker = ReliabilityTracker()
    before = build_report(ens, members, cfg)
    fuse_reliable(before, *zero_aux(), cfg, tracker=tracker)  # 反馈前:等权
    agg_before = before.intel["fusion"]["agg_reliable"]
    assert agg_before == pytest.approx(0.5, abs=1e-4)

    # 喂 20 条本地反馈(红线 30:零外呼,仅运营者复核结论)
    for _ in range(10):
        tracker.record("vlm", 1.0, True)
        tracker.record("stub", 1.0, False)
    stats = tracker.stats()
    assert stats["vlm"]["n"] == 10 and stats["stub"]["n"] == 10  # 操作计数
    assert sum(s["n"] for s in stats.values()) == 20

    after = build_report(ens, members, cfg)  # 同一分值集合
    fuse_reliable(after, *zero_aux(), cfg, tracker=tracker)
    fusion_after = after.intel["fusion"]

    # 精确结果:19/22 ≈ 0.8636,方向性靠向 vlm 的 0.9
    assert fusion_after["agg_reliable"] == pytest.approx(19.0 / 22.0, abs=1e-4)
    assert fusion_after["agg_reliable"] - agg_before > 0.3  # 显著方向性移动
    assert fusion_after["member_weights"] == {"vlm": 0.9545, "stub": 0.0455}
    # 融合概率同步上移(0.5 → 0.8636),档位受图像张数闸门保持 SUSPECT
    assert fusion_after["prob"] > before.intel["fusion"]["prob"]
    assert before.verdict is after.verdict is Verdict.SUSPECT


# ===========================================================================
# V12 贝叶斯分层可靠性(A216):Beta 后验 + 指数遗忘 + 分层收缩
# ===========================================================================


def bayes_tracker(
    half_life: float | None = None, kappa: float = DEFAULT_KAPPA
) -> BayesianReliabilityTracker:
    """关闭遗忘的贝叶斯 tracker 工厂(精确手算路径;κ 可调)。"""
    return BayesianReliabilityTracker(half_life=half_life, kappa=kappa)


def test_bayes_posterior_hand_computed() -> None:
    """无遗忘单成员 3 对 1 错:α=1+3=4, β=1+1=2, mean=2/3(逐项手算)。"""
    tracker = bayes_tracker()
    tracker.record("m", True)
    tracker.record("m", True)
    tracker.record("m", True)
    tracker.record("m", False)
    stats = tracker.stats()
    assert set(stats) == {"m"}
    s = stats["m"]
    assert s["n"] == 4
    assert s["n_eff"] == pytest.approx(4.0, abs=1e-12)
    assert s["alpha"] == pytest.approx(4.0, abs=1e-12)
    assert s["beta"] == pytest.approx(2.0, abs=1e-12)
    assert s["mean"] == pytest.approx(2.0 / 3.0, abs=1e-12)
    # 单成员时全局池 == 自身:收缩不动均值(pool_mean == mean_raw)
    post = tracker.posterior()
    assert post["m"]["pool_mean"] == pytest.approx(2.0 / 3.0, abs=1e-12)
    assert post["m"]["mean"] == pytest.approx(2.0 / 3.0, abs=1e-12)


def test_bayes_record_returns_entry_copy_and_default_ts_ticks() -> None:
    """record 返回条目副本;ts 缺省 = 上一条 + 1.0(首条 1.0),零墙钟滴答。"""
    tracker = bayes_tracker()
    e1 = tracker.record("a", True)
    e2 = tracker.record("a", False)
    e3 = tracker.record("a", True, ts=100.0)
    e4 = tracker.record("a", True)  # 显式时钟后:100.0 + 1.0
    assert e1 == {"member": "a", "correct": True, "ts": 1.0}
    assert e2["ts"] == 2.0
    assert e3["ts"] == 100.0
    assert e4["ts"] == 101.0
    e1["member"] = "hacked"  # 改返回值不影响内部
    assert set(tracker.stats()) == {"a"}
    assert tracker.stats()["a"]["n"] == 4


def test_bayes_shrinkage_hand_computed() -> None:
    """8:0 / 0:8 双成员,κ=10:λ=10/18,pool=0.5 → θ* = 61/90 / 29/90 精确。"""
    tracker = bayes_tracker()
    for _ in range(8):
        tracker.record("a", True, ts=1.0)
        tracker.record("b", False, ts=1.0)
    post = tracker.posterior()
    # 手算:mean_raw(a)=9/10、(b)=1/10;池 α_G=β_G=9 → μ_G=1/2;
    # λ = 10/(10+8) = 5/9;θ*(a) = (4/9)·(9/10) + (5/9)·(1/2) = 61/90
    assert post["a"]["shrink"] == pytest.approx(5.0 / 9.0, abs=1e-12)
    assert post["a"]["pool_mean"] == pytest.approx(0.5, abs=1e-12)
    assert post["a"]["mean"] == pytest.approx(61.0 / 90.0, abs=1e-12)
    assert post["b"]["mean"] == pytest.approx(29.0 / 90.0, abs=1e-12)
    # 收缩后参数保持成员自身浓度:n* = α_raw+β_raw = 10
    assert post["a"]["alpha"] == pytest.approx(61.0 / 9.0, abs=1e-12)  # 61/90 × 10
    assert post["a"]["beta"] == pytest.approx(29.0 / 9.0, abs=1e-12)
    # 权重 = 归一均值:Σ = 61/90 + 29/90 = 1 → w(a) = 61/90
    weights = tracker.weights()
    assert weights["a"] == pytest.approx(61.0 / 90.0, abs=1e-12)
    assert weights["b"] == pytest.approx(29.0 / 90.0, abs=1e-12)
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-12)


def test_bayes_cold_start_shrinks_fully_to_pool() -> None:
    """n_eff=3 < MIN_N:λ=1,均值恰等于全局池均值——幸运成员不吃自身数据红利。

    构造(κ=2):rich 15 对 5 错(诚实 8/11)、cold 3 对 0 错(幸运 4/5)。
    无门(min_n=0)时 cold 均值 98/125 = 0.784 > rich → 幸运主导排名;
    冷启动门把 cold 钉在池共识 19/25 = 0.76(自数据溢价 0.024 被没收),
    权重份额回到等权邻域(≈0.51,无支配)。
    """
    tracker = BayesianReliabilityTracker(half_life=None, kappa=2.0)
    for i in range(15):
        tracker.record("rich", True, ts=float(i))
    for i in range(5):
        tracker.record("rich", False, ts=100.0 + i)
    for i in range(3):
        tracker.record("cold", True, ts=50.0 + i)  # n_eff=3 < MIN_N=5

    post = tracker.posterior()
    assert post["cold"]["shrink"] == 1.0  # 冷启动:完全收缩
    assert post["cold"]["pool_mean"] == pytest.approx(19.0 / 25.0, abs=1e-12)
    assert post["cold"]["mean"] == post["cold"]["pool_mean"]  # 逐位相等,恰为池共识
    assert post["cold"]["mean_raw"] == pytest.approx(4.0 / 5.0, abs=1e-12)  # 幸运原始均值
    # rich 走连续收缩:λ = 2/(2+20) = 1/11;θ* = (10/11)(8/11)+(1/11)(19/25)
    assert post["rich"]["shrink"] == pytest.approx(1.0 / 11.0, abs=1e-12)
    assert post["rich"]["mean"] == pytest.approx(
        (10.0 / 11.0) * (8.0 / 11.0) + (1.0 / 11.0) * (19.0 / 25.0), abs=1e-12
    )

    # 对照:关掉冷启动门(min_n=0)→ cold 反超 rich(幸运主导),门内则无支配
    ungated = tracker.weights(min_n=0)
    post_ungated = tracker.posterior(min_n=0)
    assert post_ungated["cold"]["shrink"] == pytest.approx(2.0 / 5.0, abs=1e-12)
    assert post_ungated["cold"]["mean"] == pytest.approx(98.0 / 125.0, abs=1e-12)
    assert ungated["cold"] > ungated["rich"]  # 无门:幸运新成员排名反超
    weights = tracker.weights()
    assert abs(weights["cold"] - 0.5) < 0.02  # 有门:份额回等权邻域
    assert weights["cold"] < ungated["cold"]  # 自身数据溢价被门没收(0.510 < 0.518)


def test_bayes_forgetting_monotone_fixed_clock() -> None:
    """固定时钟注入:6 对(t=0)后 2 错(t=10),half_life=2 → as_of 推进均值单调降。"""
    tracker = BayesianReliabilityTracker(half_life=2.0)
    for _ in range(6):
        tracker.record("m", True, ts=0.0)
    tracker.record("m", False, ts=10.0)
    tracker.record("m", False, ts=10.0)
    m0 = tracker.stats(as_of=0.0)["m"]["mean"]   # 只见 6 对 → 7/8
    m5 = tracker.stats(as_of=5.0)["m"]["mean"]   # 错还未发生,但对已衰减
    m10 = tracker.stats(as_of=10.0)["m"]["mean"]
    assert m0 == pytest.approx(7.0 / 8.0, abs=1e-12)
    assert m0 > m5 > m10 > 0.0  # 旧"对"权重渐衰,均值单调下滑
    # 衰减质量手算:6·2^{-10/2} = 6/32 = 0.1875;错未衰减 = 2.0
    s10 = tracker.stats(as_of=10.0)["m"]
    assert s10["alpha"] == pytest.approx(1.1875, abs=1e-12)
    assert s10["beta"] == pytest.approx(3.0, abs=1e-12)
    assert s10["n_eff"] == pytest.approx(2.1875, abs=1e-12)


def test_bayes_forgetting_time_reversal_flips_conclusion() -> None:
    """同计数反时序:6 错(旧)+ 2 对(新)→ 后验偏向新观测,结论翻转。"""
    old_first = BayesianReliabilityTracker(half_life=2.0)
    for _ in range(6):
        old_first.record("m", True, ts=0.0)   # 对是旧的
    old_first.record("m", False, ts=10.0)
    old_first.record("m", False, ts=10.0)
    new_first = BayesianReliabilityTracker(half_life=2.0)
    for _ in range(6):
        new_first.record("m", False, ts=0.0)  # 错是旧的
    new_first.record("m", True, ts=10.0)
    new_first.record("m", True, ts=10.0)
    m_old = old_first.posterior(as_of=10.0)["m"]["mean"]
    m_new = new_first.posterior(as_of=10.0)["m"]["mean"]
    # 旧观测衰减殆尽:前者只剩"新错"主导,后者只剩"新对"主导
    assert m_old < 0.4 < 0.6 < m_new
    # 对称性:两场景的衰减质量互为镜像 → mean(new_first) == 1 - mean(old_first)
    assert m_new == pytest.approx(1.0 - m_old, abs=1e-12)


def test_bayes_future_events_excluded_causally() -> None:
    """as-of 时刻之后的事件不参与重放(因果性);缺省 as_of = 最大 ts。"""
    tracker = BayesianReliabilityTracker(half_life=1.0)
    tracker.record("m", True, ts=5.0)
    tracker.record("m", False, ts=10.0)
    s5 = tracker.stats(as_of=5.0)["m"]
    assert s5["n"] == 1  # t=10 的事件在 as_of=5 尚未发生
    assert s5["alpha"] == pytest.approx(2.0, abs=1e-12)  # 1 + 2^0 = 2
    assert s5["beta"] == pytest.approx(1.0, abs=1e-12)
    # 缺省 as_of 取最大 ts(10.0):两条都在
    s_def = tracker.stats()["m"]
    assert s_def["n"] == 2
    assert s_def["beta"] == pytest.approx(2.0, abs=1e-12)


def test_bayes_shrinkage_monotone_in_n_eff() -> None:
    """收缩单调:n_eff=200 → 成员数据主导(|θ*-mean_raw| < 0.05);κ→∞ → 全局均值。"""
    big = bayes_tracker()
    for i in range(200):
        big.record("a", True, ts=float(i))
        big.record("b", False, ts=float(i))
    post = big.posterior()
    assert post["a"]["n_eff"] == pytest.approx(200.0, abs=1e-12)
    # λ = 10/210 ≈ 0.048:成员数据主导,θ* 距 mean_raw(201/202)不足 0.05,
    # 且距池均值(0.5)超过 0.44 —— 收缩几乎不影响大样本成员
    assert abs(post["a"]["mean"] - post["a"]["mean_raw"]) < 0.05
    assert abs(post["a"]["mean"] - 0.5) > 0.44
    assert post["a"]["mean"] > 0.9 and post["b"]["mean"] < 0.1

    huge_kappa = bayes_tracker(kappa=1_000_000_000.0)
    for i in range(200):
        huge_kappa.record("a", True, ts=float(i))
        huge_kappa.record("b", False, ts=float(i))
    post_inf = huge_kappa.posterior()
    # κ→∞:λ→1 → 两成员均值都收敛到全局池均值 0.5(差异 < 1e-6)
    assert abs(post_inf["a"]["mean"] - 0.5) < 1e-6
    assert abs(post_inf["b"]["mean"] - 0.5) < 1e-6


def test_bayes_weights_sum_one_and_consumer_shape() -> None:
    """weights():全数值(无 None)、Σ=1、排序 = 后验均值排序;with_ci 同口径。"""
    tracker = bayes_tracker()
    for i in range(12):
        tracker.record("hi", True, ts=float(i))
        tracker.record("mid", True, ts=float(i))
        tracker.record("mid", False, ts=float(i))
        tracker.record("lo", False, ts=float(i))
    weights = tracker.weights()
    assert set(weights) == {"hi", "mid", "lo"}
    assert all(isinstance(w, float) for w in weights.values())  # 消费接口形状
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-12)
    assert weights["hi"] > weights["mid"] > weights["lo"] > 0.0
    # weights_with_ci:weight 与 weights() 逐位一致,附 Beta 95% 区间
    with_ci = tracker.weights_with_ci()
    assert set(with_ci) == set(weights)
    for name, item in with_ci.items():
        assert item["weight"] == pytest.approx(weights[name], abs=1e-15)
        lo, hi = item["ci95"]
        assert 0.0 <= lo < item["mean"] < hi <= 1.0  # 区间夹住均值
    assert sum(i["weight"] for i in with_ci.values()) == pytest.approx(1.0, abs=1e-12)


def test_bayes_empty_tracker_queries() -> None:
    """空 tracker:stats / posterior / weights / weights_with_ci 全为空 dict。"""
    tracker = bayes_tracker()
    assert tracker.stats() == {}
    assert tracker.posterior() == {}
    assert tracker.weights() == {}
    assert tracker.weights_with_ci() == {}
    assert tracker.decay_evals == 0


def test_bayes_invalid_inputs_raise_chinese_valueerror() -> None:
    """非法输入中文 ValueError:空成员 / 非布尔 correct / ts 非法 / 构造参数。"""
    tracker = bayes_tracker()
    with pytest.raises(ValueError, match="provider"):
        tracker.record("", True)
    with pytest.raises(ValueError, match="outcome"):
        tracker.record("m", "yes")
    with pytest.raises(ValueError, match="ts"):
        tracker.record("m", True, ts=float("nan"))
    with pytest.raises(ValueError, match="ts"):
        tracker.record("m", True, ts=float("inf"))
    with pytest.raises(ValueError, match="ts 必须"):  # 布尔不是合法时刻
        tracker.record("m", True, ts=True)
    with pytest.raises(ValueError, match="as_of"):
        tracker.stats(as_of=float("nan"))
    assert tracker.stats() == {}  # 一条都没记进去
    # 构造参数:half_life 非正 / 非数字,kappa 负数 → 拒绝
    with pytest.raises(ValueError, match="half_life"):
        BayesianReliabilityTracker(half_life=0.0)
    with pytest.raises(ValueError, match="half_life"):
        BayesianReliabilityTracker(half_life=-3.0)
    with pytest.raises(ValueError, match="kappa"):
        BayesianReliabilityTracker(kappa=-1.0)
    # half_life=inf 等价于关闭遗忘(合法)
    no_decay = BayesianReliabilityTracker(half_life=float("inf"))
    assert no_decay.half_life is None
    # 缺省参数可见性(默认 7.0 / 10.0)
    assert DEFAULT_HALF_LIFE == 7.0
    assert DEFAULT_KAPPA == 10.0


def test_bayes_determinism_identical_trackers() -> None:
    """确定性:同样事件序列的两个 tracker,全部查询输出逐字节相等。"""
    def build() -> BayesianReliabilityTracker:
        t = BayesianReliabilityTracker(half_life=1.5)
        for i in range(30):
            t.record("a", i % 3 != 0, ts=0.5 * i)
            t.record("b", i % 4 != 0, ts=0.5 * i + 0.25)
        return t

    t1, t2 = build(), build()
    assert t1.weights(as_of=8.0) == t2.weights(as_of=8.0)
    assert t1.posterior(as_of=12.0) == t2.posterior(as_of=12.0)  # 含 ci95 逐位相等
    assert t1.weights_with_ci() == t2.weights_with_ci()
    assert t1.decay_evals == t2.decay_evals
    assert t1.ci_probes == t2.ci_probes
    # 同一实例重复调用同样确定
    assert t1.weights() == t1.weights()
    assert t1.posterior() == t1.posterior()


def test_bayes_ops_counters() -> None:
    """操作计数(红线 31 惯例):遗忘关闭 decay_evals 恒 0;开启后每查询每事件 +1;
    ci_probes 为 CI 二分的尾概率探针次数,确定性且随成员数线性。"""
    off = bayes_tracker()
    for i in range(5):
        off.record("m", True, ts=float(i))
    off.weights()
    off.weights_with_ci()
    assert off.decay_evals == 0  # 关闭遗忘不评估 exp

    on = BayesianReliabilityTracker(half_life=3.0)
    for i in range(5):
        on.record("m", True, ts=float(i))
    on.weights()
    assert on.decay_evals == 5  # 一次查询重放 5 条事件
    assert on.ci_probes == 0  # weights() 不算 CI
    on.weights_with_ci()
    assert on.decay_evals == 10  # 第二次查询再 +5
    probes_after_first = on.ci_probes
    assert probes_after_first > 0  # CI 二分确实发生
    on.weights_with_ci()
    assert on.ci_probes == 2 * probes_after_first  # 等量查询等量探针(线性)


def test_bayes_thread_safety_concurrent_records() -> None:
    """8 线程 × 25 条并发 record:事件零丢失(200 条),滴答 ts 单调无重复。"""
    tracker = bayes_tracker()  # half_life=None:纯计数校验不受衰减影响
    errors: list[Exception] = []

    def worker(w: int) -> None:
        try:
            for i in range(25):
                tracker.record(f"p{i % 3}", i % 2 == 0)
        except Exception as exc:  # pragma: no cover —— 记录即失败
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    stats = tracker.stats()
    assert sum(s["n"] for s in stats.values()) == 200
    assert {p: s["n"] for p, s in stats.items()} == {"p0": 72, "p1": 64, "p2": 64}
    assert sum(s["n_eff"] for s in stats.values()) == pytest.approx(200.0, abs=1e-9)


def test_bayes_drift_forgetting_vs_brier_lag() -> None:
    """漂移仿真(V10.4 点名痛点):前 200 对后 1 日 20 错,half_life=0.5。

    量化"1 日内消退":drifted 后验均值 0.8865 → 0.4065(减半以上),
    权重份额 0.5511 → 0.4214;同数据 Brier 反比点估计份额 0.6395
    ——20 错只把 brier 拉到 1/11,反而把 drifted 顶到过半份额(滞后)。
    """
    tracker = BayesianReliabilityTracker(half_life=0.5)
    for d in range(10):  # good:10 天 ×(16 对 + 4 错)= 稳态 80%
        for k in range(16):
            tracker.record("good", True, ts=d + 0.05 * k)
        for k in range(4):
            tracker.record("good", False, ts=d + 0.8 + 0.05 * k)
    for i in range(200):  # drifted:前 10 天全对(密度同款,n_eff 稳态 ≈ 14)
        tracker.record("drifted", True, ts=0.05 * i)
    before = tracker.posterior(as_of=10.0)
    w_before = tracker.weights(as_of=10.0)
    assert before["drifted"]["n_eff"] >= MIN_N  # 非冷启动,走连续收缩

    for i in range(20):  # 漂移:1 日内连错 20 条(t ∈ [10.0, 10.2])
        tracker.record("drifted", False, ts=10.0 + 0.01 * i)
    after = tracker.posterior(as_of=10.2)
    w_after = tracker.weights(as_of=10.2)

    # 指数遗忘:1 日内后验均值减半以上(0.8865 → 0.4065)
    assert before["drifted"]["mean"] == pytest.approx(0.886537, abs=1e-5)
    assert after["drifted"]["mean"] == pytest.approx(0.406531, abs=1e-5)
    assert after["drifted"]["mean"] < 0.5 * before["drifted"]["mean"]
    # 权重份额显著回落(0.5511 → 0.4214),且 drifted 不再压过 good
    assert w_before["drifted"] == pytest.approx(0.551085, abs=1e-5)
    assert w_after["drifted"] == pytest.approx(0.421400, abs=1e-5)
    assert w_after["drifted"] < w_after["good"]
    assert w_before["drifted"] - w_after["drifted"] > 0.12

    # 对照:Brier 反比点估计(同样 200 对 + 20 错 vs good 160 对 40 错)
    brier = ReliabilityTracker()
    for _ in range(160):
        brier.record("good", 1.0, True)
    for _ in range(40):
        brier.record("good", 1.0, False)
    for _ in range(200):
        brier.record("drifted", 1.0, True)
    for _ in range(20):
        brier.record("drifted", 1.0, False)
    brier_share = brier.weights()["drifted"]
    assert brier_share == pytest.approx(0.639535, abs=1e-5)
    # 滞后断言:Brier 份额仍过半,比贝叶斯当日份额高 0.2 以上
    assert brier_share > 0.5
    assert brier_share - w_after["drifted"] > 0.2


def test_bayes_ci_covers_truth_simulation() -> None:
    """CI 覆盖真值仿真(固定种子 20261003,宽松界):20 成员 × 40 观测。

    真值 θ ~ U(0.3, 0.95);实测覆盖 19/20(名义 95%,分层收缩对离群
    成员有少量覆盖损耗,宽松下界 ≥ 18/20);CI 恒夹住后验均值。
    """
    rng = random.Random(20261003)
    tracker = bayes_tracker()
    truth: dict[str, float] = {}
    for m in range(20):
        theta = 0.3 + 0.65 * rng.random()
        truth[f"m{m}"] = theta
        for i in range(40):
            tracker.record(f"m{m}", rng.random() < theta, ts=float(i))
    post = tracker.posterior()
    hits = 0
    for name, theta in truth.items():
        lo, hi = post[name]["ci95"]
        if lo <= theta <= hi:
            hits += 1
        assert lo < post[name]["mean"] < hi
        assert post[name]["n_eff"] == pytest.approx(40.0, abs=1e-12)
    assert hits >= 18  # 宽松界:名义 95%,实测 19/20


def test_bayesian_kernel_selfcheck_fields() -> None:
    """bayesian_kernel_selfcheck:A138 式字段 + 漂移消退断言 + 操作计数。"""
    result = bayesian_kernel_selfcheck()
    assert result["name"] == "reliability.bayes"
    for key in ("name", "metric", "value", "baseline", "decay_evals"):
        assert key in result
    # 漂移 1 日内后验均值减半以上(0.8865 → 0.4065)
    assert result["value"] < 0.5 * result["baseline"]
    assert result["baseline"] > 0.8
    # 对照指标:Brier 同日份额仍过半(滞后),贝叶斯份额显著更低
    assert result["brier_share_same_day"] > 0.5
    assert result["brier_share_same_day"] - result["bayes_share_same_day"] > 0.2
    # 操作计数:三次查询(400 + 420 + 420)
    assert result["decay_evals"] == 1240


# ===========================================================================
# V14:record 流可选 ts 字段(写侧落盘 + 旧行读取兼容;Brier 语义零变化)
# ===========================================================================


def test_record_without_ts_writes_legacy_three_key_row(tmp_path) -> None:
    """ts 缺省不写该键:落盘行与历史逐字节一致(旧读取方零感知)。"""
    path = tmp_path / "reliability.jsonl"
    tracker = ReliabilityTracker(str(path))
    entry = tracker.record("glm", 0.9, True)
    assert entry == {"provider": "glm", "p": 0.9, "outcome": True}
    assert path.read_text(encoding="utf-8") == (
        '{"provider": "glm", "p": 0.9, "outcome": true}\n'
    )


def test_record_with_ts_persists_and_roundtrips(tmp_path) -> None:
    """显式 ts(Unix epoch 秒)随行落盘;读回 Brier 统计不受 ts 影响。

    int ts 规整为 float;带 ts 行与缺省行可混写同一文件。
    """
    path = str(tmp_path / "reliability.jsonl")
    t1 = ReliabilityTracker(path)
    e1 = t1.record("glm", 1.0, True, ts=1760000000.0)
    t1.record("glm", 1.0, True)  # 缺省行:无 ts 键
    e3 = t1.record("glm", 1.0, True, ts=1760000060)  # int → float
    assert e1["ts"] == 1760000000.0
    assert "ts" not in e3 or e3["ts"] == 1760000060.0
    t2 = ReliabilityTracker(path)
    assert t2.stats() == t1.stats()
    assert t2.stats()["glm"]["n"] == 3  # ts 不改变 Brier 计数口径
    lines = [json.loads(x) for x in open(path, encoding="utf-8")]
    assert "ts" in lines[0] and lines[0]["ts"] == 1760000000.0
    assert "ts" not in lines[1]
    assert lines[2]["ts"] == 1760000060.0


def test_record_invalid_ts_raises_chinese(tmp_path) -> None:
    """ts 非法(布尔/字符串/NaN/inf)→ 中文 ValueError,且整条不入账。"""
    tracker = ReliabilityTracker()
    for bad in (True, "soon", float("nan"), float("inf")):
        with pytest.raises(ValueError, match="ts"):
            tracker.record("glm", 0.9, True, ts=bad)
    assert tracker.stats() == {}


def test_load_skips_rows_with_invalid_ts(tmp_path) -> None:
    """读侧:ts 存在但非法的行按坏行跳过(与 p/outcome 非法同口径,
    保证 Brier 读侧与贝叶斯重放侧对同一行裁决一致);缺 ts 旧行照常读回。"""
    path = tmp_path / "reliability.jsonl"
    lines = [
        json.dumps({"provider": "glm", "p": 0.9, "outcome": True, "ts": 100.0}),
        json.dumps({"provider": "glm", "p": 0.9, "outcome": True, "ts": "soon"}),
        json.dumps({"provider": "glm", "p": 0.9, "outcome": True}),  # 旧行(无 ts)
        json.dumps({"provider": "x", "p": 0.9, "outcome": True, "ts": float("inf")}),
        json.dumps({"provider": "y", "p": 0.9, "outcome": True, "ts": True}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tracker = ReliabilityTracker(str(path))
    stats = tracker.stats()
    assert set(stats) == {"glm"}
    assert stats["glm"] == {"n": 2, "brier": pytest.approx(0.01, abs=1e-12)}


def test_jsonl_ts_roundtrip_brier_weights_identical(tmp_path) -> None:
    """同一份数据全部带 ts 落盘重开:Brier 权重与不带 ts 的历史口径逐位一致
    (ts 是纯元数据,权重分子分母零变化)。"""
    path_with = str(tmp_path / "with_ts.jsonl")
    path_without = str(tmp_path / "without_ts.jsonl")
    for _ in range(6):
        ReliabilityTracker(path_with).record("good", 1.0, True, ts=1760000000.0)
        ReliabilityTracker(path_with).record("bad", 1.0, False, ts=1760000001.0)
        ReliabilityTracker(path_without).record("good", 1.0, True)
        ReliabilityTracker(path_without).record("bad", 1.0, False)
    w_with = ReliabilityTracker(path_with).weights()
    w_without = ReliabilityTracker(path_without).weights()
    assert w_with == w_without
    assert w_with["good"] == pytest.approx(21.0 / 22.0, abs=1e-12)


# ===========================================================================
# V15 检查点化(A242):增量折叠 + 时间校正 = 全量重放;快照 save/load
# ===========================================================================


def ckpt_tracker(
    half_life: float | None = None,
    kappa: float = DEFAULT_KAPPA,
    fold_every: int = 4,
    keep_history: bool = True,
) -> CheckpointedBayesianReliabilityTracker:
    """检查点 tracker 工厂(小 fold_every 逼出多次折叠,压测增量路径)。"""
    return CheckpointedBayesianReliabilityTracker(
        half_life=half_life, kappa=kappa, fold_every=fold_every,
        keep_history=keep_history,
    )


def feed_both(
    events: list[tuple[str, bool, float]],
    half_life: float | None,
    fold_every: int = 4,
    keep_history: bool = True,
) -> tuple[BayesianReliabilityTracker, CheckpointedBayesianReliabilityTracker]:
    """同一事件流喂两个实现(全量重放基线 / 检查点增量),返回 (base, ckpt)。"""
    base = BayesianReliabilityTracker(half_life=half_life)
    ckpt = ckpt_tracker(half_life, fold_every=fold_every, keep_history=keep_history)
    for member, correct, ts in events:
        base.record(member, correct, ts=ts)
        ckpt.record(member, correct, ts=ts)
    return base, ckpt


def assert_stats_close(
    base_stats: dict, ckpt_stats: dict, *, rel: float = 1e-12, abs_tol: float = 1e-12
) -> None:
    """stats/posterior 数值字段逐成员对照(n 精确相等,其余容差)。"""
    assert list(ckpt_stats) == list(base_stats)  # 首现顺序一致
    for name, st in base_stats.items():
        ck = ckpt_stats[name]
        assert ck["n"] == st["n"]
        for key in ("n_eff", "alpha", "beta", "mean"):
            assert ck[key] == pytest.approx(st[key], rel=rel, abs=abs_tol), (
                f"{name}.{key}: {ck[key]!r} vs {st[key]!r}"
            )


def test_ckpt_no_forgetting_bitwise_equal_to_replay() -> None:
    """无遗忘(half_life=None):权重恒 1、计数为整数——检查点路径与全量重放
    逐位相等(stats/posterior/weights 全部 ==,连 exp 都不评估)。"""
    events = [
        (f"m{i % 3}", i % 5 != 0, float(i)) for i in range(26)
    ]
    events += [("m0", True, 5.5), ("m2", False, 2.25)]  # 乱序回跳
    base, ckpt = feed_both(events, half_life=None, fold_every=4)

    assert ckpt.stats() == base.stats()
    assert ckpt.posterior() == base.posterior()  # 含 ci95 逐位相等
    assert ckpt.weights() == base.weights()
    assert list(ckpt.stats()) == list(base.stats()) == ["m0", "m1", "m2"]
    assert ckpt.weights_with_ci() == base.weights_with_ci()
    assert sum(ckpt.stats()[m]["n"] for m in ckpt.stats()) == 28


def test_ckpt_short_half_life_matches_replay_within_1e12() -> None:
    """短半衰期(half_life=2.0)+ 乱序到达:多次折叠后热路径与全量重放
    在多个 as_of(缺省 / 锚点 / 未来)上 weights 容差 1e-12、后验字段一致。"""
    events = [(f"m{i % 3}", i % 5 != 0, float(i)) for i in range(24)]
    events += [("m0", True, 5.5), ("m2", False, 2.25)]  # 乱序回跳进缓冲
    base, ckpt = feed_both(events, half_life=2.0, fold_every=4)  # 6 次折叠

    for as_of in (None, 23.0, 100.0):
        w_base = base.weights(as_of=as_of)
        w_ckpt = ckpt.weights(as_of=as_of)
        assert set(w_ckpt) == set(w_base)
        for name in w_base:
            assert w_ckpt[name] == pytest.approx(w_base[name], abs=1e-12)
        assert_stats_close(base.stats(as_of=as_of), ckpt.stats(as_of=as_of))
    post_b, post_c = base.posterior(), ckpt.posterior()
    assert_stats_close(post_b, post_c)
    for name in post_b:
        assert post_c[name]["shrink"] == pytest.approx(post_b[name]["shrink"], abs=1e-12)
        assert post_c[name]["pool_mean"] == pytest.approx(
            post_b[name]["pool_mean"], abs=1e-12
        )
        lo_c, hi_c = post_c[name]["ci95"]
        lo_b, hi_b = post_b[name]["ci95"]
        assert lo_c == pytest.approx(lo_b, abs=1e-9)
        assert hi_c == pytest.approx(hi_b, abs=1e-9)


def test_ckpt_causal_exclusion_and_cold_path_exact() -> None:
    """乱序 ts + 因果排除:as_of 之后的事件两实现一致剔除。

    三条路径全部对照基类:m(T,10) m(F,5) n(T,3) m(T,12)、fold_every=2
    (前两条折叠锚点 10,后两条折叠重锚到 12)——
    - 冷路径 as_of=7 / 11(< 锚点,回退基类全量重放)与基类**逐位相等**;
    - 热路径缺省 as_of=12(检查点 × 因子 1)与基类容差相等;
    - 缓冲期(fold_every=100 不折叠)as_of=7 走因果扫描,**逐位相等**。
    """
    events = [
        ("m", True, 10.0), ("m", False, 5.0), ("n", True, 3.0), ("m", True, 12.0),
    ]
    base, ckpt = feed_both(events, half_life=2.0, fold_every=2)

    # 冷路径:保留全量事件 → 回退基类重放,同一事件列表同一算法 → 逐位相等
    for as_of in (7.0, 11.0):
        assert ckpt.stats(as_of=as_of) == base.stats(as_of=as_of)
        assert ckpt.weights(as_of=as_of) == base.weights(as_of=as_of)
    # 手算锚定:as_of=7 只见 (m,F,5)(w=2^-1)与 (n,T,3)(w=2^-2)
    s7 = base.stats(as_of=7.0)
    assert s7["m"]["alpha"] == pytest.approx(1.0, abs=1e-12)
    assert s7["m"]["beta"] == pytest.approx(1.5, abs=1e-12)
    assert s7["n"]["alpha"] == pytest.approx(1.25, abs=1e-12)

    # 热路径:缺省 as_of=12 = 锚点(因子 1),检查点和 vs 全量重放(浮点
    # 复合差异 ~eps 量级,1e-12 容差)
    w_base, w_ckpt = base.weights(), ckpt.weights()
    assert set(w_ckpt) == set(w_base)
    for name in w_base:
        assert w_ckpt[name] == pytest.approx(w_base[name], abs=1e-12)

    # 缓冲期:不折叠 → 纯因果扫描,exp 实参与基类相同 → 逐位相等
    base2, ckpt2 = feed_both(events, half_life=2.0, fold_every=100)
    assert ckpt2.stats(as_of=7.0) == base2.stats(as_of=7.0)
    assert ckpt2.weights(as_of=7.0) == base2.weights(as_of=7.0)
    assert ckpt2.weights() == base2.weights()


def test_ckpt_snapshot_roundtrip_and_continue(tmp_path) -> None:
    """快照往返:save→load 状态 bitwise 恢复(无遗忘路径);继续 record
    (首条缺省 ts = checkpoint_ts + 1)后与全量重放一致;JSON 结构对齐。"""
    events = [(f"m{i % 3}", i % 5 != 0, float(i)) for i in range(26)]
    events += [("m0", True, 5.5), ("m2", False, 2.25)]
    base, ckpt = feed_both(events, half_life=None, fold_every=4)
    path = tmp_path / "bayes_checkpoint.json"
    ckpt.save_snapshot(path)

    # JSON 结构:格式 / 版本 / 锚点(= 全流最大 ts)/ 成员首现顺序 /
    # alpha-beta-n 与基类 stats 对齐
    snap = json.loads(path.read_text(encoding="utf-8"))
    assert snap["format"] == "netsentinel.bayes-checkpoint"
    assert snap["version"] == 1
    assert snap["checkpoint_ts"] == 25.0  # 全流最大 ts(range(26) → 0..25)
    assert snap["half_life"] is None and snap["kappa"] == DEFAULT_KAPPA
    assert [m["member"] for m in snap["members"]] == list(base.stats())
    base_stats = base.stats()
    for m in snap["members"]:
        st = base_stats[m["member"]]
        assert m["alpha"] == pytest.approx(st["alpha"], abs=1e-12)
        assert m["beta"] == pytest.approx(st["beta"], abs=1e-12)
        assert m["n"] == st["n"]

    loaded = CheckpointedBayesianReliabilityTracker.load_snapshot(
        path, half_life=None
    )
    assert loaded.stats() == base.stats()
    assert loaded.posterior() == base.posterior()
    assert loaded.weights() == base.weights()

    # 加载后继续:首条缺省 ts 滴答 = checkpoint_ts + 1.0(确定性续接)
    entry = loaded.record("m1", True)
    assert entry["ts"] == 26.0
    base.record("m1", True, ts=26.0)
    for i in range(9):
        loaded.record(f"m{i % 3}", i % 4 != 0, ts=27.0 + i)
        base.record(f"m{i % 3}", i % 4 != 0, ts=27.0 + i)
    assert loaded.weights() == base.weights()  # 仍逐位相等(无遗忘)
    assert loaded.stats() == base.stats()
    assert_stats_close(base.posterior(), loaded.posterior())


def test_ckpt_asof_before_anchor_after_load_raises(tmp_path) -> None:
    """as_of < 锚点:加载前(保留全量事件)回退基类重放、与基类一致;
    快照加载后 / keep_history=False 显式中文 ValueError(聚合无法拆出
    "未来"事件,宁可拒绝不可近似)。"""
    events = [("m", i % 3 != 0, float(i)) for i in range(12)]
    base, ckpt = feed_both(events, half_life=3.0, fold_every=4)  # 锚点 7,缓冲 8..11
    assert ckpt.stats(as_of=6.5) == base.stats(as_of=6.5)  # 冷路径(< 锚点)精确

    path = tmp_path / "cp.json"
    ckpt.save_snapshot(path)  # 冲刷缓冲 → 快照锚点 = 11(全流最大 ts)
    loaded = CheckpointedBayesianReliabilityTracker.load_snapshot(
        path, half_life=3.0
    )
    with pytest.raises(ValueError, match="检查点锚点"):
        loaded.stats(as_of=6.5)
    with pytest.raises(ValueError, match="检查点锚点"):
        loaded.weights(as_of=10.9)
    # 加载后的查询在锚点及之后照常(与基类一致)
    assert set(loaded.weights()) == set(base.weights())
    for name, w in base.weights().items():
        assert loaded.weights()[name] == pytest.approx(w, abs=1e-12)

    _, lean = feed_both(events, half_life=3.0, fold_every=4, keep_history=False)
    with pytest.raises(ValueError, match="检查点锚点"):
        lean.weights(as_of=6.5)


def test_ckpt_keep_history_false_exact_until_horizon() -> None:
    """keep_history=False:折叠前(仅缓冲)任意 as_of 精确;折叠后热路径
    照常精确 / 容差一致,as_of < 锚点拒绝;无遗忘热路径逐位相等。"""
    events = [("m", i % 3 != 0, float(i)) for i in range(10)]
    # 折叠前(fold_every 大):纯缓冲因果扫描,与基类逐位相等
    base1, lean1 = feed_both(events, half_life=None, fold_every=100,
                             keep_history=False)
    for as_of in (None, 3.0, 7.5):
        assert lean1.stats(as_of=as_of) == base1.stats(as_of=as_of)
        assert lean1.weights(as_of=as_of) == base1.weights(as_of=as_of)

    # 折叠后(fold_every=4 → 锚点 7,缓冲 8/9):缺省 as_of 热路径逐位相等
    base2, lean2 = feed_both(events, half_life=None, fold_every=4,
                             keep_history=False)
    assert lean2.weights() == base2.weights()
    assert lean2.stats() == base2.stats()
    with pytest.raises(ValueError, match="检查点锚点"):
        lean2.weights(as_of=6.5)
    # 遗忘开启时热路径与基类容差一致
    base3, lean3 = feed_both(events, half_life=1.5, fold_every=4,
                             keep_history=False)
    for name, w in base3.weights().items():
        assert lean3.weights()[name] == pytest.approx(w, abs=1e-12)


def test_ckpt_snapshot_validation_rejects(tmp_path) -> None:
    """load_snapshot 拒坏:缺文件 / 非 JSON / 格式 / 版本 / half_life 与
    kappa 不一致 / 锚点非法 / 成员字段非法 / 重复成员 / 空快照带锚点。"""
    events = [("a", True, 1.0), ("b", False, 2.0), ("a", False, 3.0),
              ("b", True, 4.0)]
    _, ckpt = feed_both(events, half_life=3.0, fold_every=4)
    path = tmp_path / "cp.json"
    ckpt.save_snapshot(path)

    missing = tmp_path / "nope.json"
    with pytest.raises(FileNotFoundError):
        CheckpointedBayesianReliabilityTracker.load_snapshot(missing)

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("<<<not-json>>>", encoding="utf-8")
    with pytest.raises(ValueError):  # JSONDecodeError 是 ValueError 子类
        CheckpointedBayesianReliabilityTracker.load_snapshot(corrupt)

    def tamper(mutate) -> None:
        obj = json.loads(path.read_text(encoding="utf-8"))
        mutate(obj)
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps(obj), encoding="utf-8")
        with pytest.raises(ValueError):
            CheckpointedBayesianReliabilityTracker.load_snapshot(bad)

    tamper(lambda o: o.__setitem__("format", "other"))
    tamper(lambda o: o.__setitem__("version", 99))
    tamper(lambda o: o["members"][0].__setitem__("alpha", 0.5))  # < 先验 1
    tamper(lambda o: o["members"][0].__setitem__("n", 0))
    tamper(lambda o: o["members"].append(dict(o["members"][0])))  # 重复成员
    tamper(lambda o: o.__setitem__("checkpoint_ts", "soon"))
    tamper(lambda o: (o.__setitem__("members", []),))  # 空 members 但带锚点

    # 参数不一致(语义漂移显式拒绝;kappa 用例须带一致的 half_life)
    with pytest.raises(ValueError, match="half_life"):
        CheckpointedBayesianReliabilityTracker.load_snapshot(path, half_life=7.0)
    with pytest.raises(ValueError, match="kappa"):
        CheckpointedBayesianReliabilityTracker.load_snapshot(
            path, half_life=3.0, kappa=5.0
        )
    # 一致参数照常加载
    ok = CheckpointedBayesianReliabilityTracker.load_snapshot(path, half_life=3.0)
    assert set(ok.stats()) == {"a", "b"}


def test_ckpt_empty_tracker_snapshot_roundtrip(tmp_path) -> None:
    """空 tracker 快照往返:checkpoint_ts 为 null,加载后等价于新建实例;
    record 照常(首条 ts = 1.0,滴答语义与基类一致)。"""
    ckpt = ckpt_tracker(half_life=2.0)
    path = tmp_path / "empty.json"
    ckpt.save_snapshot(path)
    snap = json.loads(path.read_text(encoding="utf-8"))
    assert snap["checkpoint_ts"] is None and snap["members"] == []

    loaded = CheckpointedBayesianReliabilityTracker.load_snapshot(path, half_life=2.0)
    assert loaded.stats() == {} and loaded.weights() == {}
    entry = loaded.record("m", True)
    assert entry["ts"] == 1.0
    assert loaded.stats()["m"]["n"] == 1


def test_ckpt_random_stream_equals_replay_all_queries() -> None:
    """随机流(固定种子 20261003)× 乱序 ts × 6 成员 × 全网格 as_of:
    检查点实现与全量重放一致(stats 数值 / 首现顺序 / weights / CI)。"""
    rng = random.Random(20261003)
    events: list[tuple[str, bool, float]] = []
    ts = 0.0
    for _ in range(500):
        ts += rng.random() * 2.0
        # 20% 概率乱序回跳(最多 30 个时间单位)
        ets = ts if rng.random() > 0.2 else max(0.0, ts - rng.random() * 30.0)
        events.append((f"m{rng.randrange(6)}", rng.random() < 0.6, round(ets, 3)))
    base, ckpt = feed_both(events, half_life=3.0, fold_every=16)

    max_ts = max(ts for _, _, ts in events)
    grid = [None] + [max_ts * k / 20.0 for k in range(21)]
    for as_of in grid:
        assert list(ckpt.stats(as_of=as_of)) == list(base.stats(as_of=as_of))
        assert_stats_close(base.stats(as_of=as_of), ckpt.stats(as_of=as_of))
        w_base = base.weights(as_of=as_of)
        w_ckpt = ckpt.weights(as_of=as_of)
        assert set(w_ckpt) == set(w_base)
        for name in w_base:
            assert w_ckpt[name] == pytest.approx(w_base[name], abs=1e-12)
    # CI 路径(收缩参数对 alpha/beta 微扰稳定,1e-9 宽松)
    ci_b, ci_c = base.weights_with_ci(), ckpt.weights_with_ci()
    for name in ci_b:
        assert ci_c[name]["weight"] == pytest.approx(ci_b[name]["weight"], abs=1e-12)
        lo_c, hi_c = ci_c[name]["ci95"]
        lo_b, hi_b = ci_b[name]["ci95"]
        assert lo_c == pytest.approx(lo_b, abs=1e-9)
        assert hi_c == pytest.approx(hi_b, abs=1e-9)


def test_ckpt_performance_100k_events() -> None:
    """性能(A216 风险项闭环):1e5 事件流(固定种子、5 成员)上 weights():

    - 检查点实现 < 1s(宽松);
    - 对照全量重放基线加速比 >= 50x(墙钟 min-of-3);
    - 操作计数(确定性、零墙钟):基线单次查询 decay_evals +100000,
      检查点 +161(1 次锚点因子 + 160 条缓冲),比例 ≈ 621x;
    - 一致性:两实现 weights 逐成员 |Δ| <= 1e-12。
    """
    rng = random.Random(20261003)
    stream = [
        (f"p{rng.randrange(5)}", rng.random() < 0.7, 0.25 * i)
        for i in range(100_000)
    ]
    base = BayesianReliabilityTracker(half_life=7.0)
    ckpt = CheckpointedBayesianReliabilityTracker(
        half_life=7.0, keep_history=False  # 长流推荐:只留检查点 + 缓冲
    )
    for member, correct, ts in stream:
        base.record(member, correct, ts=ts)
        ckpt.record(member, correct, ts=ts)
    assert DEFAULT_FOLD_EVERY == 256 and 100_000 % 256 == 160

    def best_of(fn, runs: int = 3) -> float:
        best = math.inf
        for _ in range(runs):
            t0 = time.perf_counter()
            fn()
            best = min(best, time.perf_counter() - t0)
        return best

    t_base = best_of(lambda: base.weights())
    t_ckpt = best_of(lambda: ckpt.weights())
    assert t_ckpt < 1.0  # 宽松上限(实测 ~微秒量级)
    assert t_base / t_ckpt >= 50.0  # 加速比(结构上 ~600x,留足余量)

    # 确定性操作计数:单次查询的 exp 评估数
    b0, c0 = base.decay_evals, ckpt.decay_evals
    base.weights()
    ckpt.weights()
    assert base.decay_evals - b0 == 100_000  # 全量重放:每事件一次
    assert ckpt.decay_evals - c0 == 161      # 1 因子 + 160 缓冲事件
    assert (base.decay_evals - b0) / (ckpt.decay_evals - c0) >= 50.0

    # 一致性:1e5 事件 + 390 次折叠的浮点复合误差仍 <= 1e-12
    w_base, w_ckpt = base.weights(), ckpt.weights()
    assert set(w_ckpt) == set(w_base)
    for name in w_base:
        assert abs(w_ckpt[name] - w_base[name]) <= 1e-12


def test_ckpt_ops_counters_fold_accounting() -> None:
    """操作计数精确记账(half_life=1.0、fold_every=4、ts=1..10):

    - 折叠 #4(锚点 None→4,无重锚):4 次事件 exp;
    - 折叠 #8(重锚 4→8):1 次因子 + 4 次事件 exp → 累计 9;
    - weights()(as_of=10 > 锚点 8):1 因子 + 缓冲 2 条 → 累计 12;
    - stats(as_of=8)= 锚点:因子免评估、缓冲 9/10 均被因果排除 → 12;
    - stats(as_of=9):1 因子 + 缓冲 1 条 → 累计 14;
    - 无遗忘路径恒 0(与基类"关闭遗忘不评估 exp"同口径)。
    """
    ckpt = ckpt_tracker(half_life=1.0, fold_every=4)
    for i in range(1, 11):
        ckpt.record("m", i % 2 == 0, ts=float(i))
    assert ckpt.decay_evals == 9  # 4 + (1 + 4)
    ckpt.weights()
    assert ckpt.decay_evals == 12  # + 1 因子 + 2 缓冲
    ckpt.stats(as_of=8.0)
    assert ckpt.decay_evals == 12  # 锚点处查询零评估
    ckpt.stats(as_of=9.0)
    assert ckpt.decay_evals == 14  # + 1 因子 + 1 缓冲
    assert ckpt.ci_probes == 0  # stats/weights 不算 CI

    off = ckpt_tracker(half_life=None, fold_every=4)
    for i in range(1, 11):
        off.record("m", i % 2 == 0, ts=float(i))
    off.weights()
    off.weights_with_ci()
    assert off.decay_evals == 0


def test_ckpt_record_parity_and_ctor_validation() -> None:
    """record 语义与基类逐字一致(校验 / 返回条目 / 缺省 ts 滴答);
    构造参数 fold_every / keep_history 非法中文 ValueError;fold_every=1
    (逐条即时折叠)仍与基类逐位相等。"""
    base = bayes_tracker()
    ckpt = ckpt_tracker(half_life=None, fold_every=1)
    seq = [None, None, 100.0, None, None, 50.0, None]  # 混合缺省 / 显式 / 回跳
    out_b, out_c = [], []
    for k, ts in enumerate(seq):
        out_b.append(base.record(f"m{k % 2}", k % 3 != 0, ts=ts))
        out_c.append(ckpt.record(f"m{k % 2}", k % 3 != 0, ts=ts))
    assert out_c == out_b  # 返回条目逐条一致(含滴答与回跳后的滴答)
    assert ckpt.stats() == base.stats()  # 逐条即时折叠仍逐位相等

    with pytest.raises(ValueError, match="fold_every"):
        CheckpointedBayesianReliabilityTracker(fold_every=0)
    with pytest.raises(ValueError, match="fold_every"):
        CheckpointedBayesianReliabilityTracker(fold_every=True)
    with pytest.raises(ValueError, match="fold_every"):
        CheckpointedBayesianReliabilityTracker(fold_every=2.5)
    with pytest.raises(ValueError, match="keep_history"):
        CheckpointedBayesianReliabilityTracker(keep_history="yes")

    bad = ckpt_tracker()
    with pytest.raises(ValueError, match="provider"):
        bad.record("", True)
    with pytest.raises(ValueError, match="outcome"):
        bad.record("m", "yes")
    with pytest.raises(ValueError, match="ts"):
        bad.record("m", True, ts=float("nan"))
    with pytest.raises(ValueError, match="as_of"):
        bad.stats(as_of=float("nan"))
    with pytest.raises(ValueError, match="as_of"):
        CheckpointedBayesianReliabilityTracker().stats(as_of=float("inf"))
    assert bad.stats() == {}  # 一条都没记进去


def test_ckpt_thread_safety_concurrent_records() -> None:
    """8 线程 × 25 条并发 record(fold_every=32 逼出折叠竞态窗口):
    计数零丢失,与基类口径一致。"""
    tracker = ckpt_tracker(half_life=None, fold_every=32)
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for i in range(25):
                tracker.record(f"p{i % 3}", i % 2 == 0)
        except Exception as exc:  # pragma: no cover —— 记录即失败
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    stats = tracker.stats()
    assert {p: s["n"] for p, s in stats.items()} == {"p0": 72, "p1": 64, "p2": 64}
    assert sum(s["n_eff"] for s in stats.values()) == pytest.approx(200.0, abs=1e-9)
    assert sum(tracker.weights().values()) == pytest.approx(1.0, abs=1e-12)


def test_ckpt_determinism_identical_snapshots(tmp_path) -> None:
    """确定性:同样事件流的两个检查点 tracker 输出逐位一致,快照文件
    字节一致(状态演化的每一步都确定)。"""
    events = [(f"m{i % 3}", i % 4 != 0, float(i)) for i in range(20)]
    events += [("m1", True, 3.5), ("m0", False, 1.25)]

    def build() -> CheckpointedBayesianReliabilityTracker:
        t = ckpt_tracker(half_life=2.5, fold_every=8)
        for member, correct, ts in events:
            t.record(member, correct, ts=ts)
        return t

    t1, t2 = build(), build()
    assert t1.weights() == t2.weights()
    assert t1.posterior() == t2.posterior()  # 含 ci95
    assert t1.decay_evals == t2.decay_evals
    p1, p2 = tmp_path / "s1.json", tmp_path / "s2.json"
    t1.save_snapshot(p1)
    t2.save_snapshot(p2)
    assert p1.read_bytes() == p2.read_bytes()
    l1 = CheckpointedBayesianReliabilityTracker.load_snapshot(p1, half_life=2.5)
    l2 = CheckpointedBayesianReliabilityTracker.load_snapshot(p2, half_life=2.5)
    assert l1.weights() == l2.weights() == t1.weights()


def test_ckpt_drop_in_for_fuse_reliable_and_weights_shape() -> None:
    """接口同形(orchestrator 接线前提):isinstance 基类;weights 全数值
    Σ=1;fuse_reliable(bayes_tracker=ckpt) 与传基类实例产出一致
    (member_weights / agg_reliable / weight_model 逐位相同)。"""
    assert issubclass(CheckpointedBayesianReliabilityTracker,
                      BayesianReliabilityTracker)

    def build(kind):
        t = kind(half_life=None)
        for i in range(12):
            t.record("glm", True, ts=float(i))
            t.record("stub", False, ts=float(i))
        return t

    base_bayes = build(BayesianReliabilityTracker)
    ckpt_bayes = build(CheckpointedBayesianReliabilityTracker)
    w_b, w_c = base_bayes.weights(), ckpt_bayes.weights()
    assert w_c == w_b  # 无遗忘逐位相等
    assert set(w_b) == {"glm", "stub"}
    assert sum(w_c.values()) == pytest.approx(1.0, abs=1e-12)
    assert w_c["glm"] > w_c["stub"]  # 全对 vs 全错,方向正确

    cfg = Config()
    members = [member_score("glm:glm-5.3", 0.8), member_score("stub", 0.2)]
    ens = ensemble_scores(members)
    r_base = build_report(ens, members, cfg)
    r_ckpt = build_report(ens, members, cfg)
    fuse_reliable(r_base, *zero_aux(), cfg, bayes_tracker=base_bayes)
    fuse_reliable(r_ckpt, *zero_aux(), cfg, bayes_tracker=ckpt_bayes)
    f_b, f_c = r_base.intel["fusion"], r_ckpt.intel["fusion"]
    assert f_c["member_weights"] == f_b["member_weights"]
    assert f_c["agg_reliable"] == f_b["agg_reliable"]
    assert f_c["weight_model"] == f_b["weight_model"] == "bayes"
    assert f_c["prob"] == f_b["prob"]
