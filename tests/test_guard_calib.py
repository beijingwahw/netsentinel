# -*- coding: utf-8 -*-
"""A240 —— 守卫模型标定核验工具(guard_calib)测试。

全部离线、确定性:classify_fn 全链用注入 mock(绝不加载真实模型、绝不联网),
混淆矩阵 / MLE 建议常量对 / Brier 分数全部**手算对照**(小样本拉普拉斯),
敏感性翻转计数用构造场景(TP=FP → p̂_yes=0.5 恰在判决线上),
guard_adapter 只读 import 对账(常量实时读取 + 源码级"绝不改写"断言)。
CLI 冒烟含进程内 main() 与子进程 ``python -m`` 双路径。
"""
from __future__ import annotations

import inspect
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

from netsentinel import telemetry
from netsentinel.vision import guard_adapter
from netsentinel.vision import guard_calib

calibrate = guard_calib.calibrate
PU = guard_adapter.PROB_UNSAFE  # 0.95(现值常量,只读对账)
PS = guard_adapter.PROB_SAFE    # 0.05


# ---------------------------------------------------------------------------
# 测试素材
# ---------------------------------------------------------------------------

#: 基准样本集(n=7):3×Yes&真违规、1×Yes&真合规、2×No&真合规、1×No&真违规。
#: 含大小写/标点变体(穿透解析器容忍层,核验协议面向解析后的 verdict)。
BASE_RECORDS = [
    {"path": "a1.png", "label": 1, "guard_raw": "Yes"},
    {"path": "a2.png", "label": 1, "guard_raw": "Yes"},
    {"path": "a3.png", "label": 1, "guard_raw": "yes."},
    {"path": "b1.png", "label": 0, "guard_raw": "Yes"},
    {"path": "c1.png", "label": 0, "guard_raw": "No"},
    {"path": "c2.png", "label": 0, "guard_raw": "no"},
    {"path": "d1.png", "label": 1, "guard_raw": "No"},
]
# 手算:TP=3 FP=1 TN=2 FN=1;p̂_yes=(3+1)/(3+1+2)=4/6;p̂_no=(1+1)/(1+2+2)=2/5。
BASE_TP, BASE_FP, BASE_TN, BASE_FN = 3, 1, 2, 1
BASE_P_YES = (BASE_TP + 1) / (BASE_TP + BASE_FP + 2)   # 4/6
BASE_P_NO = (BASE_FN + 1) / (BASE_FN + BASE_TN + 2)    # 2/5
# 手算 Brier(现常量映射):Σ(映射概率-真值)²/n
#   = [3·(0.95-1)² + (0.95-0)² + 2·(0.05-0)² + (0.05-1)²] / 7 = 1.8175/7。
BASE_BRIER = (
    3 * (PU - 1) ** 2 + (PU - 0) ** 2 + 2 * (PS - 0) ** 2 + (PS - 1) ** 2
) / 7


def rec(path: str, label: int, raw: str | None = None) -> dict:
    """快速构造一条标注记录。"""
    item = {"path": path, "label": label}
    if raw is not None:
        item["guard_raw"] = raw
    return item


def write_jsonl(path, records) -> str:
    """把记录列表写成 JSONL 现场文件,返回路径字符串。"""
    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return str(path)


def repo_root() -> pathlib.Path:
    """仓库根(tests/ 的上级),与 test_guard_adapter 子进程测试同源。"""
    return pathlib.Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 协议 1+3:混淆矩阵与 MLE 建议常量对(手算对照,小样本拉普拉斯)
# ---------------------------------------------------------------------------

def test_confusion_matrix_hand_computed():
    """混淆矩阵(Yes/No × 真值)与手算逐项一致;畸形计数为 0。"""
    report = calibrate(BASE_RECORDS)
    assert report.confusion == {"tp": BASE_TP, "fp": BASE_FP,
                                "tn": BASE_TN, "fn": BASE_FN}
    assert report.n == 7 and report.n_parseable == 7
    assert report.n_unparseable == 0 and report.unparseable_paths == []
    assert report.family == "shieldgemma2"


def test_mle_suggestion_hand_computed_with_laplace():
    """MLE+拉普拉斯建议对:p̂_yes=4/6、p̂_no=2/5(手算对照)。"""
    report = calibrate(BASE_RECORDS)
    assert report.suggested_pair == (pytest.approx(BASE_P_YES),
                                     pytest.approx(BASE_P_NO))


def test_mle_laplace_on_tiny_sample():
    """极小样本拉普拉斯显效:单条 Yes&真违规 → p̂_yes=(1+1)/(1+0+2)=2/3,
    绝不信单样本给出的"100% 违规"。"""
    report = calibrate([rec("only.png", 1, "Yes")])
    assert report.suggested_pair[0] == pytest.approx(2 / 3)
    # No 侧无判定样本 → 如实给 None + 中文告警(统计诚实性)。
    assert report.suggested_pair[1] is None
    assert any("无 No" in w for w in report.warnings)


def test_mle_none_side_when_verdict_missing():
    """全部 Yes(无 No 判定):No 侧 MLE 无从估计 → None + 告警。"""
    report = calibrate([rec("y1.png", 1, "Yes"), rec("y2.png", 0, "Yes")])
    assert report.suggested_pair == (pytest.approx((1 + 1) / (1 + 1 + 2)), None)
    assert any("无 No" in w for w in report.warnings)
    assert not any("无 Yes" in w for w in report.warnings)


def test_stored_dict_shape_raw_normalized_via_coerce():
    """guard_raw 允许 pipeline dict 形态:经 coerce_model_text 归一后照常解析。"""
    report = calibrate([
        {"path": "a.png", "label": 1, "guard_raw": {"generated_text": "Yes"}},
        {"path": "b.png", "label": 0, "guard_raw": ["No"]},
    ])
    assert report.confusion == {"tp": 1, "fp": 0, "tn": 1, "fn": 0}


# ---------------------------------------------------------------------------
# 协议 2:当前常量映射下的 Brier 分数(手算对照)
# ---------------------------------------------------------------------------

def test_brier_current_mapping_hand_computed():
    """现常量对(0.95/0.05)下 Brier = 1.8175/7 ≈ 0.2596(手算对照)。"""
    report = calibrate(BASE_RECORDS)
    assert report.brier_current == pytest.approx(BASE_BRIER)
    assert report.current_pair == (PU, PS)


def test_brier_excludes_unparseable_records():
    """畸形输出不计入 Brier(单列);可解析子集照常计算。"""
    report = calibrate(BASE_RECORDS + [rec("bad.png", 1, "看不懂的输出")])
    assert report.n_unparseable == 1
    assert report.unparseable_paths == ["bad.png"]
    assert report.brier_current == pytest.approx(BASE_BRIER)  # 分母仍为 7
    assert any("无法解析" in w for w in report.warnings)


def test_all_unparseable_degrades_honestly():
    """全部畸形:矩阵全 0、Brier=None、建议对全 None、敏感性建议侧不评估。"""
    report = calibrate([rec("x.png", 1, "junk"), rec("y.png", 0, "")])
    assert report.n_parseable == 0 and report.n_unparseable == 2
    assert report.confusion == {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
    assert report.brier_current is None
    assert report.suggested_pair == (None, None)
    assert report.sensitivity["suggested"] is None
    assert report.sensitivity["current"]["flips"] == 0
    assert len(report.warnings) == 3  # 畸形 + 无Yes + 无No
    assert "无从计算" in report.render()


# ---------------------------------------------------------------------------
# 协议 3:建议与现值差异超容差的中文告警
# ---------------------------------------------------------------------------

def test_warning_when_suggestion_beyond_tolerance():
    """基准集:p̂_yes=4/6、p̂_no=2/5 与现值差均 > 0.05 → 两条中文告警。"""
    report = calibrate(BASE_RECORDS)
    tolerance_warnings = [w for w in report.warnings if "容差" in w]
    assert len(tolerance_warnings) == 2
    assert all("人工复核" in w for w in tolerance_warnings)


def test_no_warning_within_tolerance():
    """校准良好场景(tp=18,fp=1,fn=0,tn=19):两侧建议均在容差内,无告警。"""
    # 手算:tp=18,fp=1 → p̂_yes=19/21≈0.9048(与 0.95 差 0.0452<0.05);
    #       fn=0,tn=19 → p̂_no=1/21≈0.0476(与 0.05 差 0.0024<0.05)。
    records = (
        [rec(f"y{i}.png", 1, "Yes") for i in range(18)]
        + [rec("fp.png", 0, "Yes")]
        + [rec(f"n{i}.png", 0, "No") for i in range(19)]
    )
    report = calibrate(records)
    assert report.suggested_pair == (pytest.approx(19 / 21), pytest.approx(1 / 21))
    assert report.warnings == []


def test_tolerance_override_widens_silence():
    """容差可配:tolerance=0.5 时基准集的差异(0.28/0.35)不再告警。"""
    report = calibrate(BASE_RECORDS, tolerance=0.5)
    assert report.warnings == []
    assert report.tolerance == 0.5


# ---------------------------------------------------------------------------
# 协议 4:样本量充分性(CP 风格降级,红线 38 统计诚实性)
# ---------------------------------------------------------------------------

def test_small_sample_degraded_marker():
    """n<30:显式"样本不足,仅演示"降级标记 + 遥测计数。"""
    records = [rec(f"y{i}.png", 1, "Yes") for i in range(29)]
    telemetry.reset()
    try:
        report = calibrate(records)
        assert report.sufficient is False
        assert "样本不足" in report.sufficiency_note
        assert "仅演示" in report.sufficiency_note
        assert "不构成采纳依据" in report.sufficiency_note
        assert telemetry.snapshot()["counters"]["guard_calib.degraded"] >= 1
        assert "样本不足" in report.render()
    finally:
        telemetry.reset()


def test_sufficient_sample_passes_gate():
    """n=30:充分性通过;良好校准场景无告警、两侧建议均在容差内。"""
    records = (
        [rec(f"y{i}.png", 1, "Yes") for i in range(15)]
        + [rec(f"n{i}.png", 0, "No") for i in range(15)]
    )
    report = calibrate(records)
    assert report.n == 30 and report.sufficient is True
    assert "≥" in report.sufficiency_note
    assert report.warnings == []
    # 手算:p̂_yes=16/17≈0.9412、p̂_no=1/17≈0.0588,均在容差内且远离判决线。
    assert report.suggested_pair == (pytest.approx(16 / 17), pytest.approx(1 / 17))
    assert report.sensitivity["suggested"]["flips"] == 0


def test_min_calibration_n_aligned_with_conformal():
    """门槛与 decision/conformal.MIN_CALIBRATION_N 同值对齐(CP 风格)。"""
    from netsentinel.decision import conformal  # 只读参考

    assert guard_calib.MIN_CALIBRATION_N == conformal.MIN_CALIBRATION_N == 30


# ---------------------------------------------------------------------------
# 协议 6:阈值敏感性(脆弱带 + ±δ 扰动翻转计数,构造场景)
# ---------------------------------------------------------------------------

def test_sensitivity_current_pair_never_flips():
    """现常量对 0.95/0.05 远离判决线:任何样本、任何 ±0.05 扰动翻转恒 0。"""
    report = calibrate(BASE_RECORDS)
    cur = report.sensitivity["current"]
    assert cur["flips"] == 0
    assert cur["fragile_band_share"] == 0.0
    assert report.sensitivity["delta"] == 0.05


def test_sensitivity_flips_when_suggestion_on_decision_line():
    """构造场景:TP=FP → p̂_yes=(1+1)/(1+1+2)=0.5 恰在判决线上 →
    ±0.05 扰动必翻转全部 Yes 样本(2 例),脆弱带占比 2/3。"""
    records = [
        rec("y1.png", 1, "Yes"),
        rec("y2.png", 0, "Yes"),   # TP=1, FP=1 → p̂_yes=0.5
        rec("n1.png", 0, "No"),    # FN=0, TN=1 → p̂_no=1/3(远离 0.5)
    ]
    report = calibrate(records)
    assert report.suggested_pair[0] == pytest.approx(0.5)
    assert report.sensitivity["suggested"]["flips"] == 2
    assert report.sensitivity["suggested"]["fragile_band_share"] == pytest.approx(2 / 3)
    # No 侧 1/3±0.05 不跨线 → 翻转只来自 Yes 侧,恰为 2。
    assert report.sensitivity["current"]["flips"] == 0


def test_sensitivity_no_flips_when_suggestion_off_the_line():
    """基准集建议对(4/6, 0.4)离判决线够远:扰动翻转 0、脆弱带 0。"""
    report = calibrate(BASE_RECORDS)
    sug = report.sensitivity["suggested"]
    assert sug["flips"] == 0
    assert sug["fragile_band_share"] == 0.0
    assert sug["yes_prob"] == pytest.approx(BASE_P_YES)
    assert sug["no_prob"] == pytest.approx(BASE_P_NO)


def test_sensitivity_edge_exact_half_still_flips():
    """边界语义:p̂=0.5 时基线判决为真(≥0.5),-δ 扰动翻为假 → 计翻转;
    真值排列与主场景互换(TP/FP 同为一),结论只依赖建议常量落点。"""
    report = calibrate([
        rec("y1.png", 0, "Yes"),
        rec("y2.png", 1, "Yes"),   # TP=1, FP=1 → p̂_yes=0.5
        rec("n1.png", 0, "No"),    # 补 No 侧,建议对完整才评估敏感性
    ])
    assert report.suggested_pair[0] == pytest.approx(0.5)
    assert report.sensitivity["suggested"]["flips"] == 2


# ---------------------------------------------------------------------------
# 协议 5:llamaguard 变体(类别码 S1..S14 每类支持度)
# ---------------------------------------------------------------------------

LLAMA_RECORDS = [
    {"path": "l1.png", "label": 1, "guard_raw": "unsafe\nS5"},
    {"path": "l2.png", "label": 1, "guard_raw": "unsafe\nS1, S10"},
    {"path": "l3.png", "label": 1, "guard_raw": "unsafe\nS5\nS5"},  # 去重:每记录计 1
    {"path": "l4.png", "label": 1, "guard_raw": "unsafe"},          # 无类别码
    {"path": "l5.png", "label": 0, "guard_raw": "safe"},
    {"path": "l6.png", "label": 1, "guard_raw": "definitely safe"},  # 畸形
]


def test_llamaguard_category_support_statistics():
    """类别支持度:每码按记录去重计数;无码 unsafe / safe / 畸形单列。"""
    report = calibrate(LLAMA_RECORDS, family="llamaguard")
    stats = report.category_stats
    assert stats is not None
    assert stats["category_counts"] == {"S5": 2, "S1": 1, "S10": 1}
    assert stats["category_counts_positive"] == {"S5": 2, "S1": 1, "S10": 1}
    assert stats["unsafe_without_category"] == 1
    assert stats["n_safe"] == 1
    assert stats["n_unparseable"] == 1
    assert stats["n_records"] == 6
    # 混淆矩阵:unsafe×真违规 4 条(l1-l4)→ TP=4;safe×真合规 → TN=1。
    assert report.confusion == {"tp": 4, "fp": 0, "tn": 1, "fn": 0}
    assert "LlamaGuard 类别支持度" in report.render()


def test_category_stats_positive_excludes_label0():
    """真值子统计:label=0 的 unsafe 记录只入总数、不入 positive 子统计。"""
    records = [
        rec("p1.png", 1, "unsafe\nS5"),
        rec("p2.png", 0, "unsafe\nS5"),   # 误报:入总数,不入 positive
        rec("p3.png", 0, "unsafe\nS2"),
    ]
    stats = calibrate(records, family="llamaguard").category_stats
    assert stats["category_counts"] == {"S5": 2, "S2": 1}
    assert stats["category_counts_positive"] == {"S5": 1}


def test_category_stats_none_for_other_families():
    """非 llamaguard 族不做类别统计(None),记录文本原样走 Yes/No 解析。"""
    report = calibrate([rec("a.png", 1, "Yes")], family="shieldgemma2")
    assert report.category_stats is None
    assert "类别支持度" not in report.render()
    # llamaguard 文本喂给 Yes/No 族解析 → 全部畸形(族与语法须匹配)。
    mixed = calibrate(LLAMA_RECORDS, family="shieldgemma2")
    assert mixed.n_unparseable == 6 and mixed.n_parseable == 0


# ---------------------------------------------------------------------------
# classify_fn 注入模式(全链 mock,离线)
# ---------------------------------------------------------------------------

def test_classify_fn_produces_raw_for_missing_records():
    """缺 guard_raw 的记录由 classify_fn 现场产生:逐条调用、全链可分。"""
    calls: list[str] = []

    def fake_classify(path: str) -> str:
        calls.append(path)
        return {"a.png": "Yes", "b.png": "No", "c.png": "junk"}[path]

    records = [rec("a.png", 1), rec("b.png", 0), rec("c.png", 1)]
    report = calibrate(records, classify_fn=fake_classify)
    assert calls == ["a.png", "b.png", "c.png"]  # 恰好按序补缺
    assert report.n_parseable == 2 and report.n_unparseable == 1
    assert report.confusion == {"tp": 1, "fp": 0, "tn": 1, "fn": 0}
    assert report.unparseable_paths == ["c.png"]


def test_stored_guard_raw_takes_precedence_over_classify_fn():
    """历史值优先回放:带 guard_raw 的记录不触发 classify_fn(确定性)。"""
    calls: list[str] = []

    def fake_classify(path: str) -> str:
        calls.append(path)
        return "Yes"

    records = [rec("a.png", 1, "No"), rec("b.png", 0)]
    report = calibrate(records, classify_fn=fake_classify)
    assert calls == ["b.png"]  # a.png 用历史 "No",不现场推理
    # a.png: No&真违规 → FN=1;b.png: Yes&真合规 → FP=1。
    assert report.confusion == {"tp": 0, "fp": 1, "tn": 0, "fn": 1}


def test_classify_fn_exception_propagates():
    """classify_fn 异常原样上抛(标定协议不吞错,由调用方兜底)。"""
    def broken(path: str) -> str:
        raise RuntimeError("推理通道故障")

    with pytest.raises(RuntimeError, match="推理通道故障"):
        calibrate([rec("a.png", 1)], classify_fn=broken)


def test_classify_fn_allows_pipeline_shapes():
    """注入替身可返回 pipeline dict 形态(coerce 归一后解析)。"""
    def fake(path: str) -> dict:
        return {"generated_text": "unsafe\nS5"} if path == "u.png" else "safe"

    report = calibrate(
        [rec("u.png", 1), rec("s.png", 0)],
        family="llamaguard", classify_fn=fake,
    )
    assert report.confusion == {"tp": 1, "fp": 0, "tn": 1, "fn": 0}
    assert report.category_stats["category_counts"] == {"S5": 1}


# ---------------------------------------------------------------------------
# 防御式校验(中文报错,红线 8)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_records,match", [
    ([], "标注样本集为空"),
    (["x"], "应为 dict"),
    ([{"label": 1, "guard_raw": "Yes"}], "path 非法"),
    ([{"path": "a.png", "guard_raw": "Yes"}], "label 非法"),
    ([{"path": "a.png", "label": True, "guard_raw": "Yes"}], "label 非法"),
    ([{"path": "a.png", "label": 2, "guard_raw": "Yes"}], "label 非法"),
    ([{"path": "a.png", "label": 1}], "缺 guard_raw"),
    ("not-a-list", "应为 list/tuple"),
])
def test_record_validation_chinese_errors(bad_records, match):
    with pytest.raises(ValueError, match=match):
        calibrate(bad_records)


@pytest.mark.parametrize("kwargs,match", [
    ({"family": "shieldgemma"}, "未知的守卫模型族"),
    ({"family": ""}, "未知的守卫模型族"),
    ({"tolerance": 0}, "容差"),
    ({"tolerance": 1.0}, "容差"),
    ({"delta": 0}, "扰动幅度"),
    ({"delta": 0.5}, "扰动幅度"),
])
def test_parameter_validation_chinese_errors(kwargs, match):
    with pytest.raises(ValueError, match=match):
        calibrate(BASE_RECORDS, **kwargs)


def test_family_case_insensitive():
    """family 大小写归一:"ShieldGemma2" 照常工作。"""
    report = calibrate(BASE_RECORDS, family="ShieldGemma2")
    assert report.family == "shieldgemma2"
    assert report.confusion["tp"] == BASE_TP


# ---------------------------------------------------------------------------
# 确定性
# ---------------------------------------------------------------------------

def test_deterministic_repeated_calibration():
    """同输入反复标定:as_dict 与 render 逐字节一致。"""
    first = calibrate(BASE_RECORDS)
    for _ in range(5):
        again = calibrate(BASE_RECORDS)
        assert again.as_dict() == first.as_dict()
        assert again.render() == first.render()
    payload = json.dumps(first.as_dict(), ensure_ascii=False)
    assert '"tp": 3' in payload  # JSON 可序列化,键值在位


def test_aggregation_order_invariant():
    """记录顺序不影响聚合统计(矩阵/建议对/Brier/敏感性一致)。"""
    straight = calibrate(BASE_RECORDS)
    reversed_ = calibrate(list(reversed(BASE_RECORDS)))
    assert straight.confusion == reversed_.confusion
    assert straight.suggested_pair == reversed_.suggested_pair
    assert straight.brier_current == reversed_.brier_current
    assert straight.sensitivity == reversed_.sensitivity


# ---------------------------------------------------------------------------
# guard_adapter 只读对账(红线:领地外只读)
# ---------------------------------------------------------------------------

def test_current_pair_reads_adapter_constants_live():
    """报告的现值常量对来自 guard_adapter 实时读取(不复制快照)。"""
    report = calibrate(BASE_RECORDS)
    assert report.current_pair == (guard_adapter.PROB_UNSAFE,
                                   guard_adapter.PROB_SAFE)
    # 猴补 adapter 常量后重新标定 → 报告跟随现值,Brier 同步重算。
    monkeypatch_target = pytest.MonkeyPatch()
    monkeypatch_target.setattr(guard_adapter, "PROB_UNSAFE", 0.9)
    try:
        patched = calibrate([rec("a.png", 1, "Yes")])
        assert patched.current_pair[0] == 0.9
        assert patched.brier_current == pytest.approx((0.9 - 1) ** 2)
    finally:
        monkeypatch_target.undo()


def test_module_never_rewrites_adapter_constants():
    """源码级只读对账:guard_calib 绝不定义/改写 PROB_* 常量、不重导出。"""
    assert not hasattr(guard_calib, "PROB_UNSAFE")
    assert not hasattr(guard_calib, "PROB_SAFE")
    source = inspect.getsource(guard_calib)
    assert not re.search(r"^\s*PROB_(?:UNSAFE|SAFE)\s*(?::[^=\n]+)?=", source, re.M)
    # 引用一律经由 guard_adapter 命名空间(只读访问)。
    assert "guard_adapter.PROB_UNSAFE" in source
    assert "guard_adapter.PROB_SAFE" in source


# ---------------------------------------------------------------------------
# CLI 冒烟:进程内 main() + 子进程 python -m
# ---------------------------------------------------------------------------

def test_cli_smoke_in_process(tmp_path, capsys):
    """main([jsonl]) → 0,中文报告含关键小节;退出内容可直呈运营者。"""
    path = write_jsonl(tmp_path / "records.jsonl", BASE_RECORDS)
    assert guard_calib.main([path]) == 0
    out = capsys.readouterr().out
    assert "守卫模型标定核验报告" in out
    assert "混淆矩阵" in out and f"TP={BASE_TP}" in out
    assert "建议常量对" in out and "Brier" in out
    assert "样本充分性" in out and "阈值敏感性" in out
    assert "仅为标定建议" in out  # 免责:采纳是人工决策
    capsys.readouterr()
    assert guard_calib.main([path, "--tolerance", "0.5"]) == 0
    assert "告警:无" in capsys.readouterr().out


def test_cli_smoke_llamaguard_family(tmp_path, capsys):
    """--family llamaguard:报告含类别支持度小节。"""
    path = write_jsonl(tmp_path / "lg.jsonl", LLAMA_RECORDS)
    assert guard_calib.main([path, "--family", "llamaguard"]) == 0
    out = capsys.readouterr().out
    assert "LlamaGuard 类别支持度" in out
    assert "S5=2" in out


def test_cli_error_paths_return_2(tmp_path, capsys):
    """文件/记录/参数错误 → 退出码 2 + 中文错误信息。"""
    # 1. 文件不存在
    assert guard_calib.main([str(tmp_path / "nope.jsonl")]) == 2
    assert "无法读取" in capsys.readouterr().out
    # 2. 行非法 JSON(带行号)
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json}\n", encoding="utf-8")
    assert guard_calib.main([str(bad)]) == 2
    assert "第 1 行" in capsys.readouterr().out
    # 3. 行非对象
    arr = tmp_path / "arr.jsonl"
    arr.write_text("[1, 2]\n", encoding="utf-8")
    assert guard_calib.main([str(arr)]) == 2
    assert "JSON 对象" in capsys.readouterr().out
    # 4. 记录缺 guard_raw(CLI 无 classify_fn)
    missing = write_jsonl(tmp_path / "missing.jsonl", [rec("a.png", 1)])
    assert guard_calib.main([missing]) == 2
    assert "缺 guard_raw" in capsys.readouterr().out
    # 5. 空文件
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n\n", encoding="utf-8")
    assert guard_calib.main([str(empty)]) == 2
    assert "未包含任何标注记录" in capsys.readouterr().out
    # 6. 参数越界
    ok = write_jsonl(tmp_path / "ok.jsonl", BASE_RECORDS)
    assert guard_calib.main([ok, "--tolerance", "0"]) == 2
    assert "容差" in capsys.readouterr().out
    assert guard_calib.main([ok, "--delta", "0.6"]) == 2
    assert "扰动幅度" in capsys.readouterr().out


def test_cli_unknown_family_argparse_exits(tmp_path):
    """非法 --family 由 argparse 拦截(SystemExit 2),不进入标定。"""
    path = write_jsonl(tmp_path / "r.jsonl", BASE_RECORDS)
    with pytest.raises(SystemExit) as excinfo:
        guard_calib.main([path, "--family", "nope"])
    assert excinfo.value.code == 2


def test_cli_python_m_subprocess(tmp_path):
    """子进程冒烟:python -m netsentinel.vision.guard_calib 端到端可用。"""
    path = write_jsonl(tmp_path / "m.jsonl", BASE_RECORDS)
    root = str(repo_root())
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-m", "netsentinel.vision.guard_calib", path],
        cwd=root, capture_output=True, text=True,
        encoding="utf-8", env=env, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    assert "守卫模型标定核验报告" in proc.stdout
    assert f"TP={BASE_TP}" in proc.stdout
    # 错误路径同样返回 2
    proc2 = subprocess.run(
        [sys.executable, "-m", "netsentinel.vision.guard_calib",
         str(tmp_path / "nope.jsonl")],
        cwd=root, capture_output=True, text=True,
        encoding="utf-8", env=env, timeout=180,
    )
    assert proc2.returncode == 2
    assert "错误" in proc2.stdout
