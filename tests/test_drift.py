# -*- coding: utf-8 -*-
"""A206 单元测试:语料/分数分布漂移哨兵(benchmarks/drift.py)。

覆盖点(全程离线,只写 tmp_path,对 benchmarks/out 只读):

- 统计正确性:PSI/KL 闭式手算对照(独立公式重算,非调用回环)、
  恒等分布严格为 0、PSI 对称 vs KL 非对称(方向语义:ref‖cur 对
  "当前漏掉参考既有模式"敏感)、ε 平滑边界(空桶/单样本/全零/桶数失配);
- 快照与记录器:直方图分桶与钳位、三类报告(details/kernels/groups)
  的分数提取与越界过滤、快照结构合法性、JSONL 行级原子追加
  (时间戳可注入 → 确定性)、目录模式跳过无分数报告、
  历史读回逐行结构校验(坏行带行号报错);
- 窗口语义:当前窗=最近 window 条;ref=first 用完整首窗(需 ≥2×window),
  ref=prev 用紧邻滚动窗(需 ≥window+1),区间下标显式断言;
- 检测能力(合成序列,固定种子):突变(后 20 快照均值 +0.3)→ PSI 越阈
  且分级为"严重"、退出码 2;渐变(每快照 +0.01)→ prev 模式检出早于
  first;无漂移序列 → 退出码 0 且 PSI ≤ 注意线;evaluate 纯函数确定性
  (同输入同输出);
- CLI:record(真实报告/目录)→ evaluate 门禁退出码;缺文件/历史不足/
  未知子命令等错误路径中文提示 + 退出码 2;
- 报告集成只读验证:对 benchmarks/out 全部 *_report.json 跑一轮
  快照编译(dry,不落盘),断言结构合法;
- bootstrap 95% CI(V13 评测增强批):小样本手算对照(b=1 独立复算
  首个重样本的 PSI)、单桶退化区间塌缩为点值、恒等分布 CI 随 n 收窄、
  同种子逐位确定 / 换种子确实不同、B 参数生效(b=1 → lo==hi,不同 B
  → 不同区间)、非法 B 报错;compare_windows 的 bootstrap_ci 段结构、
  逐标签与条件 KL CI、小窗(n<30)CI 诚实加宽且**退出码仍按点值**
  (CI 上界越告警线但点值 0 → 退出码 0)、无 bootstrap_b 时结果与
  既有行为零差;evaluate/CLI 的 CI 列与方法行、--bootstrap-b 0 关闭、
  负数报错退出码 2。
- KL 双侧重采样(V13-2 评测增强批):``resample="both"`` 的小样本手算
  对照(先参考后当前的配对重样本独立复算)、配对差分语义(当前窗退化
  单桶时单侧塌缩为点值而双侧仍有宽度——参考侧噪声确实计入;参考窗放大
  后双侧区间收窄逼近单侧——参考噪声被稀释)、同种子逐位确定 / 换种子
  不同、psi+both 与非法 resample 值中文报错;compare_windows 段
  ``kl_resample`` 记录(默认 current 时 PSI/整体 KL CI 与既有单侧路径
  逐位一致,双侧只换整体 KL)、evaluate/CLI ``--kl-resample`` 透传与
  默认输出逐字节不变、非法取值 argparse 退出码 2。
"""
from __future__ import annotations

import json
import math
import random
from pathlib import Path

import pytest

from benchmarks import drift
from benchmarks.drift import (
    BOOTSTRAP_B,
    BOOTSTRAP_SEED,
    DriftError,
    append_drift_snapshot,
    bootstrap_ci,
    build_snapshot,
    compare_windows,
    compute_psi,
    conditional_kl,
    evaluate,
    extract_scores,
    histogram,
    kl_divergence,
    load_history,
    main,
    snapshot_from_path,
)

#: 项目根(benchmarks/out 只读集成用)。
ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "benchmarks" / "out"

#: 合成序列参数:每快照 100 个分数,已验证噪声底(无漂移 PSI≈0.06)
#: 与告警线 0.2 之间留有充足裕度。
SYN_N = 100
SYN_SEED = 10000


# ---------------------------------------------------------------------------
# 合成工具(固定种子,确定性)
# ---------------------------------------------------------------------------

def _ts(i: int) -> str:
    """按序号生成可排序的确定性时间戳(load_history 不解析,仅作字段)。"""
    return f"2026-01-01T{i // 60:02d}:{i % 60:02d}:00+08:00"


def _snap(scores: list[float], i: int, source: str = "synthetic") -> dict:
    """无标签快照(直方图复用被测模块的 histogram,与其余字段拼装)。"""
    return {
        "ts": _ts(i),
        "source": source,
        "n": len(scores),
        "histogram": histogram(scores),
    }


def _gen(kind: str, count: int, *, n: int = SYN_N, seed: int = SYN_SEED) -> list[dict]:
    """合成快照序列:none=同分布;gradual=每快照均值 +0.01;sudden=末 20 条 +0.3。"""
    snaps: list[dict] = []
    for i in range(count):
        rng = random.Random(seed + i)
        if kind == "none":
            shift = 0.0
        elif kind == "gradual":
            shift = 0.01 * i
        elif kind == "sudden":
            shift = 0.3 if i >= count - 20 else 0.0
        else:  # pragma: no cover - 测试内部约定
            raise AssertionError(f"未知合成类型 {kind}")
        scores = [min(x + shift, 1.0) for x in (rng.random() for _ in range(n))]
        snaps.append(_snap(scores, i))
    return snaps


def _write_history(path: Path, snaps: list[dict]) -> Path:
    """把快照列表写成 history JSONL(只落在 tmp_path)。"""
    path.write_text(
        "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in snaps),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# 1. 统计正确性:手算对照 / 恒等 / 对称性 / 平滑边界
# ---------------------------------------------------------------------------

def test_psi_hand_computed_two_bins() -> None:
    """两桶手算:ref=[.5,.5],cur=[.25,.75](计数输入应与概率输入同值)。"""
    expected = (0.25 - 0.5) * math.log(0.25 / 0.5) + (0.75 - 0.5) * math.log(0.75 / 0.5)
    assert compute_psi([10, 10], [5, 15]) == pytest.approx(expected, abs=1e-12)
    assert compute_psi([0.5, 0.5], [0.25, 0.75]) == pytest.approx(expected, abs=1e-9)


def test_psi_identity_is_exactly_zero() -> None:
    """恒等分布(含同位置空桶)严格为 0;比例缩放不改变分布 → 0。"""
    assert compute_psi([3, 7, 0], [3, 7, 0]) == 0.0
    assert compute_psi([2, 2], [10, 10]) == 0.0
    assert compute_psi([0, 5, 0], [0, 5, 0]) == 0.0


def test_psi_is_symmetric() -> None:
    """PSI 逐项 (c−r)ln(c/r) 交换不变 → 无方向(与 KL 形成对照)。"""
    a, b = [8, 2, 0], [3, 3, 4]
    assert compute_psi(a, b) == pytest.approx(compute_psi(b, a), abs=1e-12)


def test_kl_hand_computed_two_bins() -> None:
    """KL([.5,.5]‖[.25,.75]) 手算对照(nat)。"""
    expected = 0.5 * math.log(0.5 / 0.25) + 0.5 * math.log(0.5 / 0.75)
    assert kl_divergence([1, 1], [1, 3]) == pytest.approx(expected, abs=1e-12)


def test_kl_identity_zero_and_nonnegative() -> None:
    """恒等 = 0;随机分布对(固定种子)恒非负。"""
    assert kl_divergence([4, 6], [4, 6]) == 0.0
    rng = random.Random(2026)
    for _ in range(50):
        a = [rng.randint(0, 20) for _ in range(10)]
        b = [rng.randint(0, 20) for _ in range(10)]
        assert kl_divergence(a, b) >= 0.0


def test_kl_asymmetric_and_direction_semantics() -> None:
    """KL 非对称;方向 KL(ref‖cur):当前漏掉参考常见桶(漏检方向)贡献大。"""
    ref, cur = [10, 10], [20, 0]
    miss_direction = kl_divergence(ref, cur)  # 当前窗丢掉参考的右半分布
    reverse = kl_divergence(cur, ref)
    assert miss_direction != pytest.approx(reverse, abs=1e-9)
    assert miss_direction > 5.0  # 漏掉一半既有质量 → nat 数应显著
    assert reverse > 0.0


def test_smoothing_empty_bin_and_single_sample_boundaries() -> None:
    """空桶/单样本:ε 平滑后有限;全零/桶数失配/空输入 → DriftError。"""
    # 完全错开的空桶:有限且大
    assert 0.0 < compute_psi([10, 0], [0, 10]) < math.inf
    assert 0.0 < kl_divergence([10, 0], [0, 10]) < math.inf
    # 单样本直方图 vs 其他:有限
    single = [0] * 10
    single[9] = 1
    other = [0] * 10
    other[0] = 1
    assert 0.0 < compute_psi(single, other) < math.inf
    assert 0.0 < kl_divergence(single, other) < math.inf
    # 平滑不能凭空造分布
    with pytest.raises(DriftError, match="全为零"):
        compute_psi([0, 0], [1, 1])
    with pytest.raises(DriftError):
        kl_divergence([1, 1], [0, 0])
    with pytest.raises(DriftError, match="桶数不一致"):
        compute_psi([1, 2], [1, 2, 3])
    with pytest.raises(DriftError):
        kl_divergence([], [])


def test_conditional_kl_reference_mass_weighting() -> None:
    """条件 KL = 共同标签按参考质量加权的 KL 之和(手排权重对照)。"""
    ref = {"a": [5, 5], "b": [10, 0]}
    cur = {"a": [5, 5], "b": [0, 10]}
    # 参考质量:a=10,b=10 → 权重各 0.5;KL_a=0
    expected = 0.5 * 0.0 + 0.5 * kl_divergence(ref["b"], cur["b"])
    assert conditional_kl(ref, cur) == pytest.approx(expected, abs=1e-12)
    # 参考质量失衡:权重随之倾斜
    ref2 = {"a": [5, 5], "b": [30, 0]}
    w_b = 30 / 40
    expected2 = (1 - w_b) * 0.0 + w_b * kl_divergence(ref2["b"], cur["b"])
    assert conditional_kl(ref2, cur) == pytest.approx(expected2, abs=1e-12)
    # 无共同标签:条件 KL 无定义
    with pytest.raises(DriftError, match="无共同标签"):
        conditional_kl({"x": [1, 1]}, {"y": [1, 1]})


def test_grade_thresholds_follow_industry_convention() -> None:
    """分级阈值边界:≤0.1 稳定 / >0.1 注意 / >0.2 告警 / >0.3 严重。"""
    assert drift._grade(0.0) == "稳定"
    assert drift._grade(0.1) == "稳定"
    assert drift._grade(0.1 + 1e-9) == "注意"
    assert drift._grade(0.2) == "注意"
    assert drift._grade(0.2 + 1e-9) == "告警"
    assert drift._grade(0.3) == "告警"
    assert drift._grade(0.3 + 1e-9) == "严重"


# ---------------------------------------------------------------------------
# 2. 直方图 / 分数提取 / 快照结构
# ---------------------------------------------------------------------------

def test_histogram_bucketing_and_clamping() -> None:
    """分桶边界:[0,.1)→桶0、1.0→桶9、±1e-9 浮点越界钳回、显著越界报错。"""
    assert histogram([0.0, 0.09999, 0.1, 0.55, 0.99, 1.0]) == [
        2, 1, 0, 0, 0, 1, 0, 0, 0, 2,
    ]
    assert histogram([1.0 + 1e-10]) == [0] * 9 + [1]
    assert histogram([-1e-10]) == [1] + [0] * 9
    assert histogram([]) == [0] * 10
    with pytest.raises(DriftError, match="超出"):
        histogram([1.5])
    with pytest.raises(DriftError, match="超出"):
        histogram([-0.2])


def test_extract_scores_details_prob_and_variants() -> None:
    """主基准 details.prob 与对抗 base_prob+变体(标签沿用原图)。"""
    report = {
        "details": [
            {"file": "a.png", "label": "nsfw", "prob": 0.9},
            {"file": "b.png", "label": "clean", "prob": 0.1},
        ]
    }
    pairs, note = extract_scores(report, "report.json")
    assert pairs == [("nsfw", 0.9), ("clean", 0.1)]
    assert note == ""

    adv = {
        "details": [
            {
                "file": "a.png",
                "label": "nsfw",
                "base_prob": 0.8,
                "variants": {"blur": {"prob": 0.5}, "jpeg": {"prob": 0.7}},
            }
        ]
    }
    pairs, _ = extract_scores(adv, "adversarial_report.json")
    assert pairs == [("nsfw", 0.8), ("nsfw", 0.5), ("nsfw", 0.7)]


def test_extract_scores_providers_family_label_and_missing_prob() -> None:
    """providers:nsfw_prob 为分数、family 作条件维度;None 分数跳过。"""
    report = {
        "details": [
            {"name": "p1", "family": "openai", "nsfw_prob": 0.87},
            {"name": "p2", "family": "gemini", "nsfw_prob": None},
            {"name": "p3", "family": "openai", "nsfw_prob": 0.03},
        ]
    }
    pairs, note = extract_scores(report, "providers_report.json")
    assert pairs == [("openai", 0.87), ("openai", 0.03)]
    assert note == ""


def test_extract_scores_kernels_filter_out_of_range() -> None:
    """kernel 报告只纳入 [0,1] 的 value,越界指标(延迟/字节)跳过并说明。"""
    report = {
        "kernels": [
            {"name": "skin", "metric": "gap", "value": 0.97},
            {"name": "lat", "metric": "ms", "value": 3981},
            {"name": "mem", "metric": "bytes", "value": 262144.0},
            {"name": "flag", "metric": "ok", "value": 1},
        ]
    }
    pairs, note = extract_scores(report, "kernel_report.json")
    assert pairs == [("skin", 0.97), ("flag", 1)]
    assert "2 个超出 [0,1]" in note


def test_extract_scores_groups_majority_share() -> None:
    """grouping 报告:majority_share 为分数,majority_label 为标签。"""
    report = {
        "groups": [
            {"index": 1, "members": 3, "majority_label": "site0", "majority_share": 1.0},
            {"index": 2, "members": 2, "majority_label": "site1", "majority_share": 0.5},
        ]
    }
    pairs, note = extract_scores(report, "grouping_report.json")
    assert pairs == [("site0", 1.0), ("site1", 0.5)]
    assert note == ""


def test_extract_scores_unrecognized_structure() -> None:
    """tier_report 之类无分数字段 → 空列表 + 中文原因。"""
    pairs, note = extract_scores({"tiers": {"low": {"peak": 1}}}, "tier_report.json")
    assert pairs == []
    assert "tier_report" in note


def test_build_snapshot_schema() -> None:
    """快照结构:{ts,source,n,histogram} + 有标签时 label_conditional(键有序)。"""
    report = {
        "details": [
            {"label": "nsfw", "prob": 0.95},
            {"label": "clean", "prob": 0.05},
            {"label": "clean", "prob": 0.15},
        ]
    }
    from datetime import datetime

    snap = build_snapshot(report, "report.json", now=datetime(2026, 10, 3, 12, 0, 0))
    assert snap["ts"] == "2026-10-03T12:00:00"  # 本地时间无偏移场景由注入保证
    assert snap["source"] == "report.json"
    assert snap["n"] == 3
    assert sum(snap["histogram"]) == 3 == snap["n"]
    assert list(snap["label_conditional"]) == ["clean", "nsfw"]  # 键排序 → 序列化确定
    assert sum(snap["label_conditional"]["clean"]) == 2
    # 无标签报告:省略 label_conditional 键
    snap2 = build_snapshot({"details": [{"prob": 0.4}]}, "x.json")
    assert "label_conditional" not in snap2
    # 提不到分数:宁可报错也不产空快照
    with pytest.raises(DriftError, match="未提取到分数分布"):
        build_snapshot({"tiers": {}}, "tier_report.json")


# ---------------------------------------------------------------------------
# 3. 记录器:JSONL 行级原子追加 / 目录模式 / 历史读回校验
# ---------------------------------------------------------------------------

def test_append_drift_snapshot_jsonl_append_and_dirs(tmp_path: Path) -> None:
    """追加写:两轮各 1 行、ts 可注入(确定性)、父目录自动创建。"""
    from datetime import datetime

    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps({"details": [{"label": "nsfw", "prob": 0.9}, {"label": "clean", "prob": 0.1}]}),
        encoding="utf-8",
    )
    history = tmp_path / "deep" / "nested" / "drift_history.jsonl"
    t1 = datetime(2026, 10, 1, 8, 0, 0)
    t2 = datetime(2026, 10, 2, 8, 0, 0)
    snaps1 = append_drift_snapshot(report_path, history, now=t1)
    snaps2 = append_drift_snapshot(report_path, history, now=t2)
    assert len(snaps1) == 1 and len(snaps2) == 1
    lines = history.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # 追加而非覆盖
    first, second = (json.loads(x) for x in lines)
    assert first["ts"] == "2026-10-01T08:00:00"
    assert second["ts"] == "2026-10-02T08:00:00"
    assert second["n"] == 2 and sum(second["histogram"]) == 2


def test_append_single_file_error_paths(tmp_path: Path) -> None:
    """单文件模式:文件缺失 / 无分数字段 → DriftError(不污染历史)。"""
    history = tmp_path / "h.jsonl"
    with pytest.raises(DriftError, match="不存在"):
        append_drift_snapshot(tmp_path / "nope.json", history)
    scoreless = tmp_path / "tier_report.json"
    scoreless.write_text(json.dumps({"tiers": {"low": {"peak": 1}}}), encoding="utf-8")
    with pytest.raises(DriftError, match="未提取到分数分布"):
        append_drift_snapshot(scoreless, history)
    assert not history.exists()


def test_append_dir_mode_skips_scoreless_reports(tmp_path: Path) -> None:
    """目录模式:处理 report.json 与 *_report.json,无分数报告静默跳过。"""
    out = tmp_path / "out"
    out.mkdir()
    (out / "report.json").write_text(
        json.dumps({"details": [{"label": "nsfw", "prob": 0.9}]}), encoding="utf-8"
    )
    (out / "tier_report.json").write_text(
        json.dumps({"tiers": {"low": {"peak": 1}}}), encoding="utf-8"
    )
    (out / "ignored.md").write_text("not a report", encoding="utf-8")
    history = tmp_path / "h.jsonl"
    snaps = append_drift_snapshot(out, history)
    assert [s["source"] for s in snaps] == ["report.json"]
    with pytest.raises(DriftError, match="没有"):
        append_drift_snapshot(tmp_path, history)  # 空目录


def test_load_history_validates_each_line(tmp_path: Path) -> None:
    """读回校验:坏 JSON / 桶数 / 计数和 / 标签直方图 / 空文件,均带行号或中文。"""
    good = _write_history(tmp_path / "ok.jsonl", _gen("none", 2, n=4))
    assert len(load_history(good)) == 2

    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"ts": "t", "source": "s", "n": 1, "histogram": [1, 0]}\n', encoding="utf-8")
    with pytest.raises(DriftError, match="第 1 行"):
        load_history(bad)

    bad.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(DriftError, match="第 1 行"):
        load_history(bad)

    # 计数和 != n
    bad.write_text(
        json.dumps({"ts": "t", "source": "s", "n": 5, "histogram": [1] * 10}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(DriftError, match="计数和"):
        load_history(bad)

    # 标签直方图计数和 > n
    snap = {"ts": "t", "source": "s", "n": 2, "histogram": [2] + [0] * 9,
            "label_conditional": {"x": [3] + [0] * 9}}
    bad.write_text(json.dumps(snap) + "\n", encoding="utf-8")
    with pytest.raises(DriftError, match="标签 'x'"):
        load_history(bad)

    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n\n", encoding="utf-8")
    with pytest.raises(DriftError, match="为空"):
        load_history(empty)
    with pytest.raises(DriftError, match="不存在"):
        load_history(tmp_path / "nope.jsonl")


# ---------------------------------------------------------------------------
# 4. 窗口滑动语义(区间下标显式断言)
# ---------------------------------------------------------------------------

def test_window_slice_semantics_explicit_ranges() -> None:
    """当前窗=最近 window 条;first=开头完整 window 条;prev=紧邻前 window 条。"""
    # 第 i 条快照的全部样本落在桶 i%10,使各窗口直方图可区分
    snaps = []
    for i in range(25):
        h = [0] * 10
        h[i % 10] = 10
        snaps.append({"ts": _ts(i), "source": "synthetic", "n": 10, "histogram": h})

    res_first = compare_windows(snaps, window=10, ref="first")
    assert res_first["cur_range"] == [15, 25]  # 最近 10 条
    assert res_first["ref_range"] == [0, 10]  # 完整首窗
    assert res_first["total"] == 25

    res_prev = compare_windows(snaps, window=10, ref="prev")
    assert res_prev["cur_range"] == [15, 25]
    assert res_prev["ref_range"] == [5, 15]  # 紧邻当前窗之前

    # prev 的短参考:历史刚过当前窗时,参考窗取剩余全部(功效提示)
    res_short = compare_windows(snaps[:12], window=10, ref="prev")
    assert res_short["ref_range"] == [0, 2]  # 当前窗 [2,12),之前只剩 2 条
    assert any("统计功效较低" in n for n in res_short["notes"])


def test_compare_windows_input_validation(tmp_path: Path) -> None:
    """window 非法 / 未知 ref / 历史不足:中文错误,边界正好卡在 2×window。"""
    snaps = _gen("none", 5, n=4)
    with pytest.raises(DriftError, match="window 须为正整数"):
        compare_windows(snaps, window=0)
    with pytest.raises(DriftError, match="first\\|prev"):
        compare_windows(snaps, window=2, ref="middle")
    with pytest.raises(DriftError, match="2×window=10"):
        compare_windows(snaps, window=5, ref="first")  # 5 < 2×5
    assert compare_windows(_gen("none", 10, n=4), window=5, ref="first")["total"] == 10
    with pytest.raises(DriftError, match="window\\+1=6"):
        compare_windows(snaps, window=5, ref="prev")  # 5 < 6
    assert compare_windows(_gen("none", 6, n=4), window=5, ref="prev")["total"] == 6


def test_compare_windows_label_set_changes_and_conditional_kl() -> None:
    """标签集增减作为独立证据列出;条件 KL 按参考质量加权。"""
    def labeled(label: str, bin_idx: int, n: int = 10) -> dict:
        h = [0] * 10
        h[bin_idx] = n
        return {
            "ts": "t",
            "source": "s",
            "n": n,
            "histogram": list(h),
            "label_conditional": {label: list(h)},
        }

    ref_snaps = [labeled("nsfw", 9), labeled("clean", 0)]
    cur_snaps = [labeled("nsfw", 5), labeled("borderline", 4)]  # clean 消失,borderline 新增
    res = compare_windows(ref_snaps + cur_snaps, window=2, ref="first")
    assert res["new_labels"] == ["borderline"]
    assert res["gone_labels"] == ["clean"]
    assert set(res["psi_by_label"]) == {"nsfw"}  # 只在共同标签上对照
    # 条件 KL = KL(nsfw ref‖cur)(唯一共同标签,权重 1)
    assert res["conditional_kl"] == pytest.approx(
        kl_divergence(ref_snaps[0]["histogram"], cur_snaps[0]["histogram"])
    )
    assert any("borderline" in n for n in res["notes"])
    assert any("clean" in n for n in res["notes"])


# ---------------------------------------------------------------------------
# 5. 漂移检测能力:合成序列(固定种子)
# ---------------------------------------------------------------------------

def test_sudden_shift_detected_with_correct_level(tmp_path: Path, capsys) -> None:
    """前 100 同分布 + 后 20 均值 +0.3 → PSI 越阈、分级"严重"、退出码 2。"""
    snaps = _gen("sudden", 120)
    res = compare_windows(snaps, window=20, ref="first")
    assert res["max_psi"] > drift.PSI_SEVERE  # 越严重线 0.3
    assert res["level"] == "严重"
    assert res["exit_code"] == 2
    history = _write_history(tmp_path / "h.jsonl", snaps)
    assert evaluate(history, window=20, ref="first") == 2
    out = capsys.readouterr().out
    assert "严重" in out and "退出码 2" in out


def test_gradual_drift_prev_detects_earlier_than_first(tmp_path: Path) -> None:
    """渐变(+0.01/快照):滚动对照(prev)的首次告警时点早于锚定基线(first)。

    prev 只需当前窗之前还有快照即可开评(参考窗取剩余),first 需要凑满
    完整首窗(2×window);且滚动参考不受长程基线老化的稀释。
    """
    snaps = _gen("gradual", 120)
    window = 10
    prev_hit: int | None = None
    for total in range(window + 1, 121):
        if compare_windows(snaps[:total], window, ref="prev")["exit_code"] == 2:
            prev_hit = total
            break
    first_hit: int | None = None
    for total in range(2 * window, 121):
        if compare_windows(snaps[:total], window, ref="first")["exit_code"] == 2:
            first_hit = total
            break
    assert prev_hit is not None, "渐变漂移下 prev 模式应触发告警"
    assert first_hit is not None, "渐变漂移下 first 模式最终也应触发告警"
    assert prev_hit < first_hit


def test_no_drift_sequence_exits_zero(tmp_path: Path, capsys) -> None:
    """无漂移序列:PSI 低于注意线,退出码 0(prev/first 双模式)。"""
    snaps = _gen("none", 120)
    for ref in ("first", "prev"):
        res = compare_windows(snaps, window=20, ref=ref)
        assert res["max_psi"] <= drift.PSI_ATTENTION, ref
        assert res["level"] == "稳定"
    history = _write_history(tmp_path / "h.jsonl", snaps)
    assert evaluate(history, window=20, ref="first") == 0
    assert evaluate(history, window=20, ref="prev") == 0
    out = capsys.readouterr().out
    assert "退出码 0" in out


def test_evaluate_is_deterministic(tmp_path: Path, capsys) -> None:
    """确定性:同一历史文件两次评估,报告逐字节一致、退出码一致。"""
    history = _write_history(tmp_path / "h.jsonl", _gen("sudden", 60))
    rc1 = evaluate(history, window=20, ref="first")
    out1 = capsys.readouterr().out
    rc2 = evaluate(history, window=20, ref="first")
    out2 = capsys.readouterr().out
    assert rc1 == rc2 == 2
    assert out1 == out2
    # 纯函数层:compare_windows 对同输入返回逐字段相等结果
    snaps = _gen("gradual", 40)
    assert compare_windows(snaps, 10, "prev") == compare_windows(list(snaps), 10, "prev")


def test_render_report_contains_table_and_conclusion(tmp_path: Path, capsys) -> None:
    """中文报告形态:表头/阈值行/结论行/提示行齐全。"""
    history = _write_history(tmp_path / "h.jsonl", _gen("sudden", 40))
    assert evaluate(history, window=10, ref="first") == 2
    out = capsys.readouterr().out
    for fragment in ("整体分布", "PSI", "分级", "KL(nat)", "阈值", "结论", "告警线"):
        assert fragment in out


# ---------------------------------------------------------------------------
# 6. CLI:record / evaluate 全链路与错误路径
# ---------------------------------------------------------------------------

def test_cli_record_then_evaluate_gate(tmp_path: Path, capsys) -> None:
    """CLI 全链路:真实报告反复 record(同分布)→ 0;注入漂移 → 门禁 2。"""
    history = tmp_path / "drift_history.jsonl"
    report = OUT_DIR / "report.json"
    for _ in range(30):  # 30 条同分布快照(同一报告的分数)
        assert main(["record", str(report), "--history", str(history)]) == 0
    lines = history.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 30 and json.loads(lines[0])["source"] == "report.json"

    # 无漂移:门禁放行
    assert main(["evaluate", "--history", str(history), "--window", "10"]) == 0
    assert "未检测到显著漂移" in capsys.readouterr().out

    # 注入漂移:追加 10 条"全部分数堆到末桶"的合成快照(带同款标签,
    # 保证条件维度可比)→ 退出码 2
    with history.open("a", encoding="utf-8") as fh:
        for i in range(10):
            fh.write(
                json.dumps(
                    {
                        "ts": _ts(i),
                        "source": "synthetic",
                        "n": 30,
                        "histogram": [0] * 9 + [30],
                        "label_conditional": {
                            "clean": [0] * 9 + [15],
                            "nsfw": [0] * 9 + [15],
                        },
                    }
                )
                + "\n"
            )
    assert main(["evaluate", "--history", str(history), "--window", "10"]) == 2
    out = capsys.readouterr().out
    assert "检测到分数分布漂移" in out
    assert "[clean]" in out  # 真实报告带标签 → 条件维度行出现


def test_cli_record_directory_real_out(tmp_path: Path, capsys) -> None:
    """目录模式集成:真实 benchmarks/out 下 5 份分数报告入册,tier 跳过。"""
    history = tmp_path / "h.jsonl"
    assert main(["record", str(OUT_DIR), "--history", str(history)]) == 0
    out = capsys.readouterr().out
    snaps = [json.loads(x) for x in history.read_text(encoding="utf-8").splitlines()]
    assert {s["source"] for s in snaps} == {
        "adversarial_report.json",
        "grouping_report.json",
        "kernel_report.json",
        "providers_report.json",
        "report.json",
    }
    assert "tier_report.json" in out and "跳过" in out
    # 快照可直接进入评估器(历史读回校验通过)
    assert len(load_history(history)) == 5


def test_cli_error_paths_exit_two(tmp_path: Path, capsys) -> None:
    """错误路径:缺文件/历史不足/空目录 → 退出码 2 + 中文 stderr。"""
    assert main(["record", str(tmp_path / "nope.json"), "--history", str(tmp_path / "h.jsonl")]) == 2
    assert "错误" in capsys.readouterr().err

    history = _write_history(tmp_path / "short.jsonl", _gen("none", 3, n=4))
    assert main(["evaluate", "--history", str(history), "--window", "10"]) == 2
    assert "历史仅 3 条" in capsys.readouterr().err

    assert main(["record", str(tmp_path), "--history", str(tmp_path / "h.jsonl")]) == 2
    assert "没有" in capsys.readouterr().err


def test_cli_unknown_subcommand_exits_two() -> None:
    """未知子命令:argparse 用法错误(SystemExit 2),与门禁退出码约定一致。"""
    with pytest.raises(SystemExit) as excinfo:
        main(["frobnicate"])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# 7. 报告集成只读验证:真实 benchmarks/out 快照结构合法性(dry,不落盘)
# ---------------------------------------------------------------------------

def test_real_reports_compile_to_valid_snapshots_dry() -> None:
    """对每份真实报告跑一轮快照编译:结构合法、计数自洽(全程零写入)。"""
    from datetime import datetime

    expected_scored = {
        "adversarial_report.json",
        "grouping_report.json",
        "kernel_report.json",
        "providers_report.json",
        "report.json",
    }
    seen: set[str] = set()
    for path in sorted(OUT_DIR.glob("*.json")):
        if path.name == "tier_report.json":
            with pytest.raises(DriftError, match="tier_report"):
                snapshot_from_path(path)
            continue
        snap = snapshot_from_path(path)
        seen.add(path.name)
        # 结构合法性:字段齐全、10 桶、计数自洽、ts 可解析
        assert {"ts", "source", "n", "histogram"} <= set(snap)
        assert snap["source"] == path.name
        assert len(snap["histogram"]) == 10
        assert sum(snap["histogram"]) == snap["n"] > 0
        datetime.fromisoformat(snap["ts"])
        cond = snap.get("label_conditional")
        if path.name in ("report.json", "adversarial_report.json", "providers_report.json"):
            assert cond, f"{path.name} 应有条件维度"
        for label, lh in (cond or {}).items():
            assert len(lh) == 10 and 0 < sum(lh) <= snap["n"], label
    assert seen == expected_scored


# ---------------------------------------------------------------------------
# 8. bootstrap 95% CI(V13 评测增强批):手算对照 / 收窄 / 确定性 / B 参数
# ---------------------------------------------------------------------------


def _inline_smoothed_psi(ref: list[float], cur: list[float]) -> float:
    """测试内独立重算 ε 平滑 PSI(不复用被测模块的函数,防回环)。"""
    def probs(hist: list[float]) -> list[float]:
        total = sum(hist)
        p = [max(float(v) / total, 1e-6) for v in hist]
        z = sum(p)
        return [x / z for x in p]

    r, c = probs(ref), probs(cur)
    return max(sum((ci - ri) * math.log(ci / ri) for ri, ci in zip(r, c)), 0.0)


def test_bootstrap_ci_manual_replication_small_sample() -> None:
    """小样本手算对照:b=1 时区间 = 首个重样本的 PSI,重样本独立复算。

    独立内联复算:与模块同口径的 random.Random(seed) 抽 n 个均匀数,
    按当前窗经验概率(3/10, 7/10)分桶计数,再按平滑 PSI 公式重算——
    验证"多项重采样当前窗计数 + percentile 取值"的实现正确性。
    """
    ref, cur = [6, 4], [3, 7]  # 2 桶,n=10;经验概率 0.3/0.7
    lo, hi = bootstrap_ci(ref, cur, metric="psi", b=1, seed=99)
    assert lo == hi  # 单个重样本 → 区间两端为同一次复算值

    rng = random.Random(99)
    counts = [0, 0]
    for _ in range(10):
        u = rng.random()
        counts[0 if u <= 0.3 else 1] += 1  # bisect_left 累积权重 [0.3, 1.0]
    expected = _inline_smoothed_psi(ref, counts)
    assert lo == pytest.approx(expected, abs=1e-12)
    # 与点值不同(重采样确实扰动了计数),且非负
    assert lo > 0.0
    assert lo != pytest.approx(compute_psi(ref, cur), abs=1e-9) or counts == cur


def test_bootstrap_ci_degenerate_current_collapses_to_point() -> None:
    """当前窗全部质量在单桶(或 n=1):任何多项重采样都还原原计数 →
    CI 塌缩为 (点值, 点值)——闭式可推,零随机性。"""
    ref = [5, 5] + [0] * 8
    cur = [0] * 9 + [10]
    point = compute_psi(ref, cur)
    lo, hi = bootstrap_ci(ref, cur, metric="psi", b=50)
    assert (lo, hi) == (point, point)
    lo_kl, hi_kl = bootstrap_ci(ref, cur, metric="kl", b=50)
    assert (lo_kl, hi_kl) == (drift.kl_divergence(ref, cur),) * 2
    # 单样本当前窗:重采样恒等于该样本 → 同样退化
    single = [0, 0, 1]
    lo1, hi1 = bootstrap_ci([1, 1, 1], single, b=30)
    assert (lo1, hi1) == (compute_psi([1, 1, 1], single),) * 2


def test_bootstrap_ci_identity_shrinks_with_n() -> None:
    """恒等分布:点值恒 0,CI 宽度随 n 收窄(n=4000 比 n=40 显著更窄)——
    窗口越小 CI 越宽属诚实表现(纯抽样噪声即可推高 PSI)。"""
    small = bootstrap_ci([4] * 10, [4] * 10, b=400)        # n=40
    big = bootstrap_ci([400] * 10, [400] * 10, b=400)      # n=4000(同形状)
    assert small[0] >= 0.0 and big[0] >= 0.0  # PSI 非负
    assert (big[1] - big[0]) < (small[1] - small[0]) / 10  # 数量级收窄
    assert big[1] < drift.PSI_ATTENTION < small[1]  # 大 n 时噪声已压到注意线下


def test_bootstrap_ci_deterministic_same_seed() -> None:
    """同种子同数据 → 区间逐位相等(评测可复现);换种子 → 重采样序列不同。"""
    ref, cur = [8, 2, 0, 5, 5], [3, 3, 4, 2, 8]
    first = bootstrap_ci(ref, cur, b=200, seed=BOOTSTRAP_SEED)
    second = bootstrap_ci(ref, cur, b=200, seed=BOOTSTRAP_SEED)
    assert first == second
    other = bootstrap_ci(ref, cur, b=200, seed=BOOTSTRAP_SEED + 1)
    assert other != first
    # 非法输入:未知指标 / 非正整数 B / 全零当前窗 → 中文 DriftError
    with pytest.raises(DriftError, match=r"只支持 psi\|kl"):
        bootstrap_ci(ref, cur, metric="js")
    for bad in (0, -3, 2.5, True):
        with pytest.raises(DriftError, match="正整数"):
            bootstrap_ci(ref, cur, b=bad)
    with pytest.raises(DriftError, match="全为零"):
        bootstrap_ci([1, 1], [0, 0], b=10)


def test_bootstrap_b_parameter_takes_effect() -> None:
    """B 参数生效:b=1 → lo==hi;b=50 与 b=2000 的区间(连续值)不同;
    分位索引随 B 变化(最近秩法 ⌊q·(B−1)⌋)。"""
    ref, cur = [7, 3, 5, 1], [2, 6, 4, 4]
    one = bootstrap_ci(ref, cur, b=1)
    assert one[0] == one[1]
    b50 = bootstrap_ci(ref, cur, b=50)
    b2000 = bootstrap_ci(ref, cur, b=2000)
    assert b50 != b2000
    # 区间端点取自重样本值域(无越界捏造)
    lo, hi = b2000
    assert 0.0 <= lo <= hi < math.inf


def test_compare_windows_bootstrap_section_structure() -> None:
    """compare_windows(bootstrap_b=...) 的 CI 段结构:整体/逐标签/条件 KL
    各就各位;不传 bootstrap_b 时无该键(既有行为与开销零变)。"""
    def labeled(label: str, bin_idx: int, n: int = 10) -> dict:
        h = [0] * 10
        h[bin_idx] = n
        return {
            "ts": "t",
            "source": "s",
            "n": n,
            "histogram": list(h),
            "label_conditional": {label: list(h)},
        }

    ref_snaps = [labeled("nsfw", 9), labeled("clean", 0)]
    cur_snaps = [labeled("nsfw", 5), labeled("clean", 1)]
    snaps = ref_snaps + cur_snaps
    res = compare_windows(snaps, window=2, ref="first", bootstrap_b=100)
    ci = res["bootstrap_ci"]
    assert ci["b"] == 100 and ci["seed"] == BOOTSTRAP_SEED and ci["level"] == 0.95
    assert "percentile bootstrap" in ci["method"]
    assert len(ci["psi"]) == 2 and len(ci["kl"]) == 2
    assert ci["psi"][0] <= ci["psi"][1] and ci["kl"][0] <= ci["kl"][1]
    assert set(ci["psi_by_label"]) == set(res["psi_by_label"]) == {"nsfw", "clean"}
    for bounds in list(ci["psi_by_label"].values()) + list(ci["kl_by_label"].values()):
        assert len(bounds) == 2 and bounds[0] <= bounds[1]
    assert ci["conditional_kl"] is not None and ci["conditional_kl"][0] <= ci["conditional_kl"][1]
    # 点值不受 CI 影响;无 bootstrap_b → 无该键
    plain = compare_windows(snaps, window=2, ref="first")
    assert "bootstrap_ci" not in plain
    for key in ("psi", "kl", "psi_by_label", "kl_by_label", "exit_code", "level"):
        assert res[key] == plain[key]
    # B=0 = 显式关闭(与 None 等价);非法 B → DriftError
    assert "bootstrap_ci" not in compare_windows(snaps, 2, "first", bootstrap_b=0)
    with pytest.raises(DriftError, match="正整数"):
        compare_windows(snaps, 2, "first", bootstrap_b=-1)


def test_small_window_ci_widens_but_exit_code_unchanged() -> None:
    """小窗(n<30)诚实表现:CI 因抽样噪声显著加宽——恒等分布点值 PSI=0,
    CI 上界可远超告警线 0.2,但**退出码仍按点值判定为 0**(CI 为信息列)。"""
    snaps = [
        {"ts": _ts(i), "source": "synthetic", "n": 20, "histogram": [2] * 10}
        for i in range(2)
    ]
    res = compare_windows(snaps, window=1, ref="first", bootstrap_b=300)
    assert res["psi"] == 0.0 and res["n_cur"] == 20 < drift.CI_MIN_HONEST_N
    ci_hi = res["bootstrap_ci"]["psi"][1]
    assert ci_hi > drift.PSI_ALERT  # 纯抽样噪声即可把 CI 上界推过告警线
    assert res["level"] == "稳定" and res["exit_code"] == 0  # 但门禁只看点值
    assert any("CI 因抽样噪声显著加宽" in n and "n=20" in n for n in res["notes"])
    # 对照:n=3000 的同形状窗口,CI 上界已收窄回注意线内,且不再附小窗提示
    big = [
        {"ts": _ts(i), "source": "synthetic", "n": 3000, "histogram": [300] * 10}
        for i in range(2)
    ]
    res_big = compare_windows(big, window=1, ref="first", bootstrap_b=300)
    assert res_big["bootstrap_ci"]["psi"][1] < drift.PSI_ATTENTION
    assert not any("CI 因抽样噪声显著加宽" in n for n in res_big["notes"])


def test_evaluate_reports_ci_columns_and_method_line(tmp_path: Path, capsys) -> None:
    """evaluate 默认附 CI:表头两列 + 区间值 + 方法行(CI 为信息列声明);
    报告整体逐字节确定(固定种子)。"""
    history = _write_history(tmp_path / "h.jsonl", _gen("none", 6, n=60))
    assert evaluate(history, window=3, ref="first") == 0
    out = capsys.readouterr().out
    assert "PSI 95%CI" in out and "KL 95%CI" in out
    assert "percentile bootstrap" in out and "信息列" in out
    assert "[0." in out  # 区间以 [lo,hi] 呈现
    rc2 = evaluate(history, window=3, ref="first")
    assert rc2 == 0 and capsys.readouterr().out == out  # 两次输出逐字节一致


def test_cli_bootstrap_b_flag(tmp_path: Path, capsys) -> None:
    """CLI --bootstrap-b:默认 2000 附 CI;0 关闭(无 CI 列);负数 → 退出码 2。"""
    history = _write_history(tmp_path / "h.jsonl", _gen("none", 6, n=60))
    assert main(
        ["evaluate", "--history", str(history), "--window", "3", "--bootstrap-b", "100"]
    ) == 0
    assert "PSI 95%CI" in capsys.readouterr().out
    assert main(
        ["evaluate", "--history", str(history), "--window", "3", "--bootstrap-b", "0"]
    ) == 0
    assert "PSI 95%CI" not in capsys.readouterr().out
    assert main(
        ["evaluate", "--history", str(history), "--window", "3", "--bootstrap-b", "-5"]
    ) == 2
    assert "正整数" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 9. KL 双侧重采样(V13-2):手算对照 / 配对差分语义 / 确定性 / 段集成 / CLI
# ---------------------------------------------------------------------------


def _inline_smoothed_kl(ref: list[float], cur: list[float]) -> float:
    """测试内独立重算 ε 平滑 KL(不复用被测模块的函数,防回环)。"""
    def probs(hist: list[float]) -> list[float]:
        total = sum(hist)
        p = [max(float(v) / total, 1e-6) for v in hist]
        z = sum(p)
        return [x / z for x in p]

    r, c = probs(ref), probs(cur)
    return max(sum(ri * math.log(ri / ci) for ri, ci in zip(r, c)), 0.0)


def test_bootstrap_kl_both_manual_replication_small_sample() -> None:
    """双侧小样本手算对照:b=1 时区间 = 首个**配对**重样本的 KL;重采样
    顺序为先参考(n_ref 次)后当前(n_cur 次),同一 RNG 流独立复算。"""
    ref, cur = [6, 4], [3, 7]  # n_ref=10(概率 .6/.4),n_cur=10(概率 .3/.7)
    lo, hi = bootstrap_ci(ref, cur, metric="kl", b=1, seed=99, resample="both")
    assert lo == hi  # 单个配对重样本 → 区间两端为同一次复算值

    rng = random.Random(99)
    ref_counts = [0, 0]
    for _ in range(10):  # 先参考:累积权重 [0.6, 1.0]
        u = rng.random()
        ref_counts[0 if u <= 0.6 else 1] += 1
    cur_counts = [0, 0]
    for _ in range(10):  # 后当前:累积权重 [0.3, 1.0]
        u = rng.random()
        cur_counts[0 if u <= 0.3 else 1] += 1
    expected = _inline_smoothed_kl(ref_counts, cur_counts)
    assert lo == pytest.approx(expected, abs=1e-12)
    # 与单侧不同(参考窗也被扰动)且非负
    one = bootstrap_ci(ref, cur, metric="kl", b=1, seed=99)
    assert lo != pytest.approx(one[0], abs=1e-9) or ref_counts == [6, 4]
    assert lo >= 0.0


def test_bootstrap_kl_paired_semantics_vs_one_sided() -> None:
    """配对差分语义:当前窗退化单桶 → 单侧塌缩为点值(重采样恒等),双侧
    仍有宽度——参考窗重样本在波动,证明参考侧噪声确实计入;参考窗与当前
    窗同退化 → 双侧也塌缩为 (0, 0);参考窗放大后双侧收窄逼近单侧(稀释)。"""
    ref = [5, 5] + [0] * 8
    cur = [0] * 9 + [10]
    point = drift.kl_divergence(ref, cur)
    one_lo, one_hi = bootstrap_ci(ref, cur, metric="kl", b=50)
    assert (one_lo, one_hi) == (point, point)  # 单侧:当前重采样恒等 → 点值
    both_lo, both_hi = bootstrap_ci(ref, cur, metric="kl", b=50, resample="both")
    assert both_hi > both_lo  # 双侧:参考重样本波动 → 区间有宽度
    assert (both_lo, both_hi) != (one_lo, one_hi)  # 两种口径是不同区间
    # 两侧同退化(同位置单桶):任何配对重采样都还原 → KL 恒 0
    degenerate = [0] * 9 + [10]
    assert bootstrap_ci(
        degenerate, cur, metric="kl", b=20, resample="both"
    ) == (0.0, 0.0)

    # 参考窗噪声稀释:同形状参考窗放大 40 倍(n=15 → 600),双侧宽度收窄
    # 并落到单侧附近(参考侧抽样噪声 ~ 1/n_ref)
    cur_mixed = [3, 3, 4, 2, 8]
    ref_small = [2, 4, 4, 2, 3]
    ref_big = [v * 40 for v in ref_small]
    one_small = bootstrap_ci(ref_small, cur_mixed, metric="kl", b=600)
    both_small = bootstrap_ci(
        ref_small, cur_mixed, metric="kl", b=600, resample="both"
    )
    both_big = bootstrap_ci(ref_big, cur_mixed, metric="kl", b=600, resample="both")
    assert (both_small[1] - both_small[0]) > (one_small[1] - one_small[0])
    assert (both_big[1] - both_big[0]) < (both_small[1] - both_small[0])


def test_bootstrap_kl_both_deterministic_and_validation() -> None:
    """双侧确定性:同种子逐位一致、换种子不同;参数校验(非法 resample /
    psi+both 组合 / 参考窗全零)中文报错;单侧默认逐字节不变
    (resample="current" 显式值与缺省完全同值)。"""
    ref, cur = [8, 2, 0, 5, 5], [3, 3, 4, 2, 8]
    # 单侧默认零变动:缺省与显式 current 逐位相等
    assert bootstrap_ci(ref, cur, metric="kl") == bootstrap_ci(
        ref, cur, metric="kl", resample="current"
    )
    first = bootstrap_ci(ref, cur, metric="kl", b=200, resample="both")
    assert first == bootstrap_ci(ref, cur, metric="kl", b=200, resample="both")
    other = bootstrap_ci(
        ref, cur, metric="kl", b=200, seed=BOOTSTRAP_SEED + 1, resample="both"
    )
    assert other != first
    with pytest.raises(DriftError, match=r"current\|both"):
        bootstrap_ci(ref, cur, metric="kl", resample="double")
    with pytest.raises(DriftError, match="只对 metric='kl'"):
        bootstrap_ci(ref, cur, metric="psi", resample="both")
    with pytest.raises(DriftError, match="参考窗计数全为零"):
        bootstrap_ci([0, 0], cur, metric="kl", b=10, resample="both")


def test_compare_windows_kl_resample_both_section() -> None:
    """compare_windows(kl_resample=...):段内记录口径;双侧只换**整体 KL**
    CI(PSI CI 逐位不变、逐标签/条件 KL 维持单侧);默认 current 时整体
    KL CI 与 bootstrap_ci 既有单侧路径逐位一致;点值/分级/退出码零影响。"""
    ref_hist = [3, 2, 1, 0, 0, 0, 0, 0, 0, 0]
    cur_hist = [1, 1, 2, 0, 0, 0, 0, 0, 0, 3]
    snaps = [
        {"ts": _ts(0), "source": "s", "n": sum(ref_hist), "histogram": list(ref_hist)},
        {"ts": _ts(1), "source": "s", "n": sum(cur_hist), "histogram": list(cur_hist)},
    ]
    plain = compare_windows(snaps, window=1, ref="first", bootstrap_b=100)
    both = compare_windows(
        snaps, window=1, ref="first", bootstrap_b=100, kl_resample="both"
    )
    assert plain["bootstrap_ci"]["kl_resample"] == "current"
    assert both["bootstrap_ci"]["kl_resample"] == "both"
    # PSI CI 不受 KL 重采样口径影响(逐位一致)
    assert both["bootstrap_ci"]["psi"] == plain["bootstrap_ci"]["psi"]
    # 整体 KL CI:双侧 ≠ 单侧;默认单侧 = 函数级既有路径逐位一致
    #(_ci_seed(0) == BOOTSTRAP_SEED → 与 bootstrap_ci 默认种子同流)
    assert both["bootstrap_ci"]["kl"] != plain["bootstrap_ci"]["kl"]
    assert plain["bootstrap_ci"]["kl"] == list(
        bootstrap_ci(ref_hist, cur_hist, metric="kl", b=100)
    )
    # 逐标签与条件 KL 维持单侧(口径只切整体)
    assert both["bootstrap_ci"]["kl_by_label"] == plain["bootstrap_ci"]["kl_by_label"]
    assert both["bootstrap_ci"]["conditional_kl"] == plain["bootstrap_ci"]["conditional_kl"]
    # 点值/分级/退出码完全不受影响(CI 为信息列)
    for key in ("psi", "kl", "psi_by_label", "kl_by_label", "exit_code", "level"):
        assert both[key] == plain[key]
    # 方法行:双侧 = 既有文案 + 附注(单侧保持既有文案原样)
    assert both["bootstrap_ci"]["method"].startswith(plain["bootstrap_ci"]["method"])
    assert "双侧重采样" in both["bootstrap_ci"]["method"]
    with pytest.raises(DriftError, match=r"current\|both"):
        compare_windows(snaps, 1, "first", bootstrap_b=10, kl_resample="double")


def test_evaluate_and_cli_kl_resample_flag(tmp_path: Path, capsys) -> None:
    """evaluate/CLI 的 --kl-resample:默认 current 输出与既有逐字节一致;
    both 时 KL CI 变化且方法行附注、结论行不变;非法取值 argparse 退出码 2。"""
    history = _write_history(tmp_path / "h.jsonl", _gen("none", 6, n=60))
    assert evaluate(history, window=3, ref="first") == 0
    default_out = capsys.readouterr().out
    assert evaluate(history, window=3, ref="first", kl_resample="current") == 0
    assert capsys.readouterr().out == default_out  # 单侧默认逐字节不变
    assert evaluate(history, window=3, ref="first", kl_resample="both") == 0
    both_out = capsys.readouterr().out
    assert both_out != default_out  # KL CI 列确实变化
    assert "双侧重采样" in both_out
    assert both_out.splitlines()[-1] == default_out.splitlines()[-1]  # 结论行不变

    # CLI 透传:--kl-resample both 生效;非法值 argparse SystemExit 2
    assert main(
        ["evaluate", "--history", str(history), "--window", "3", "--kl-resample", "both"]
    ) == 0
    assert "双侧重采样" in capsys.readouterr().out
    with pytest.raises(SystemExit) as excinfo:
        main(
            ["evaluate", "--history", str(history), "--window", "3",
             "--kl-resample", "double"]
        )
    assert excinfo.value.code == 2
