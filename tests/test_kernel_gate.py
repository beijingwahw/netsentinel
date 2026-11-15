"""内核门禁执行器测试(``vision/kernel_gate.py``,V10.37)。

锁定六组语义(全部离线、零外呼、tmp_path 内闭环):

1. **指纹**:gate_digest 对同一测量稳定、对判据数字/模型变化敏感;
2. **三道锁**:evaluate_kernel_gate 逐锁拒开(报告缺失 / 模型不一致 /
   门禁未过 / 无批准 / kernel 不匹配 / model 不匹配 / 报告重测后指纹
   失配 / 多成员集成 / 未配 classifier)与全过放行;
3. **批准落地**:write_approval 只锚定通过的测量(缺失/未过/模型不符
   拒绝写),写入后 evaluate 放行,重跑覆盖更新;
4. **机读报告**:calibration.save_report_json 落盘结构(含 digest)与
   report CLI 无条件写机读 JSON;
5. **approve CLI**:门禁通过可写、未过拒绝退出 1;
6. **未知内核**:ValueError(中文)。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from netsentinel.contracts import Config
from netsentinel.vision import calibration as calib
from netsentinel.vision import kernel_gate as kg

MODEL = "stub:calibrated"


def _gate(passed: bool = True) -> dict:
    """构造 sprt_gate 形状的判据 dict(与 sprt_gate() 输出同构)。"""
    return {
        "criteria": [
            {"criterion": "C1 样本量", "pass": passed, "detail": "人标正样本 25/20,负样本 30/20"},
            {"criterion": "C2 校准 ECE", "pass": passed, "detail": "ECE=0.03(≤ 0.1 为过)"},
        ],
        "passed": passed,
        "verdict": "门禁通过" if passed else "门禁未过",
    }


def _write_report(root: Path, *, model: str = MODEL, passed: bool = True) -> Path:
    report = {"labeled_total": 55, "scored_ok": 55, "coverage": 1.0, "score_errors": 0,
              "by_source": {"human": {"n": 55}, "seed_or_site": {"n": 0}}}
    gate = _gate(passed)
    return calib.save_report_json(root, model, report, gate)


def _write_approval(root: Path, *, kernel: str = "sprt", model: str = MODEL,
                    digest: str | None = None, note: str = "运营者批准") -> None:
    gate = _gate(True)
    kg.approval_path(root).parent.mkdir(parents=True, exist_ok=True)
    kg.approval_path(root).write_text(
        json.dumps({
            "kernel": kernel, "model": model,
            "digest": digest if digest is not None else kg.gate_digest(model, gate),
            "approved_at": "2026-11-10T00:00:00", "note": note,
        }, ensure_ascii=False),
        encoding="utf-8",
    )


def _cfg(root: Path, **kw) -> Config:
    # ensemble_members 显式清空(缺省 ["stub"] 会触发"未测模型"锁——
    # 对生产是正确保护,对单测是噪音;家规同 test_scan_pipeline cfg 夹具)
    kw.setdefault("ensemble_members", [])
    return Config(data_dir=str(root), classifier=MODEL, **kw)


# ---------------------------------------------------------------------------
# 1. 指纹
# ---------------------------------------------------------------------------
def test_gate_digest_stable_and_sensitive() -> None:
    g1, g2 = _gate(True), _gate(True)
    assert kg.gate_digest(MODEL, g1) == kg.gate_digest(MODEL, g2)      # 稳定
    assert kg.gate_digest(MODEL, g1) != kg.gate_digest(MODEL, _gate(False))  # 判据变化
    assert kg.gate_digest(MODEL, g1) != kg.gate_digest("stub:other", g1)     # 模型变化
    # 判据 detail 数字变化(重测后样本量/指标变化)→ 指纹变化
    g3 = json.loads(json.dumps(g1, ensure_ascii=False))
    g3["criteria"][0]["detail"] = "人标正样本 40/20,负样本 30/20"
    assert kg.gate_digest(MODEL, g1) != kg.gate_digest(MODEL, g3)


# ---------------------------------------------------------------------------
# 2. 三道锁:逐锁拒开 + 全过放行
# ---------------------------------------------------------------------------
def test_refused_when_report_missing(tmp_path: Path) -> None:
    res = kg.evaluate_kernel_gate(_cfg(tmp_path), "sprt")
    assert res.allowed is False
    assert any("校准报告缺失" in r for r in res.reasons)


def test_refused_when_report_model_mismatch(tmp_path: Path) -> None:
    _write_report(tmp_path, model="stub:other")
    _write_approval(tmp_path, model="stub:other")
    res = kg.evaluate_kernel_gate(_cfg(tmp_path), "sprt")
    assert res.allowed is False
    assert any("不是当前主分类器" in r for r in res.reasons)


def test_refused_when_gate_not_passed(tmp_path: Path) -> None:
    _write_report(tmp_path, passed=False)
    res = kg.evaluate_kernel_gate(_cfg(tmp_path), "sprt")
    assert res.allowed is False
    assert any("五判据未全过" in r and "C1" in r for r in res.reasons)


def test_refused_when_no_approval(tmp_path: Path) -> None:
    _write_report(tmp_path)
    res = kg.evaluate_kernel_gate(_cfg(tmp_path), "sprt")
    assert res.allowed is False
    assert any("无批准文件" in r for r in res.reasons)
    assert res.gate_digest  # 测量锁已过(指纹可复算)


def test_refused_when_approval_kernel_or_model_mismatch(tmp_path: Path) -> None:
    _write_report(tmp_path)
    _write_approval(tmp_path, kernel="cascade")           # kernel 不匹配
    res = kg.evaluate_kernel_gate(_cfg(tmp_path), "sprt")
    assert res.allowed is False
    assert any("内核是 'cascade'" in r for r in res.reasons)

    _write_approval(tmp_path, model="stub:other")         # model 不匹配
    res = kg.evaluate_kernel_gate(_cfg(tmp_path), "sprt")
    assert res.allowed is False
    assert any("批准文件的模型" in r for r in res.reasons)


def test_refused_when_digest_stale(tmp_path: Path) -> None:
    """报告重测(判据数字变化)→ 指纹换 → 旧批准自动失效(指纹锁)。"""
    _write_report(tmp_path)
    old_gate = _gate(True)
    old_gate["criteria"][0]["detail"] = "人标正样本 21/20,负样本 30/20"  # 旧一次测量
    _write_approval(tmp_path, digest=kg.gate_digest(MODEL, old_gate))
    res = kg.evaluate_kernel_gate(_cfg(tmp_path), "sprt")
    assert res.allowed is False
    assert any("指纹与当前报告不一致" in r for r in res.reasons)


def test_refused_when_ensemble_members_untested(tmp_path: Path) -> None:
    _write_report(tmp_path)
    _write_approval(tmp_path)
    cfg = _cfg(tmp_path, ensemble_members=[MODEL, "glm:glm-5.3-flash"])
    res = kg.evaluate_kernel_gate(cfg, "sprt")
    assert res.allowed is False
    assert any("未测模型" in r for r in res.reasons)


def test_refused_when_classifier_empty(tmp_path: Path) -> None:
    _write_report(tmp_path)
    _write_approval(tmp_path)
    res = kg.evaluate_kernel_gate(
        Config(data_dir=str(tmp_path), classifier="", ensemble_members=[]), "sprt"
    )
    assert res.allowed is False
    assert any("未配置主分类器" in r for r in res.reasons)


def test_allowed_on_full_pass(tmp_path: Path) -> None:
    _write_report(tmp_path)
    _write_approval(tmp_path)
    # ensemble_members 只含主分类器本身(单成员配置)= 已测,放行
    res = kg.evaluate_kernel_gate(_cfg(tmp_path, ensemble_members=[MODEL]), "sprt")
    assert res.allowed is True
    assert res.reasons == []
    assert res.approved_at == "2026-11-10T00:00:00"
    intel = res.as_intel()
    assert intel == {"kernel": "sprt", "allowed": True, "approved_at": "2026-11-10T00:00:00"}


def test_unknown_kernel_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="未登记的内核"):
        kg.evaluate_kernel_gate(_cfg(tmp_path), "cascade")


# ---------------------------------------------------------------------------
# 3. 批准落地(只锚定通过的测量)
# ---------------------------------------------------------------------------
def test_write_approval_requires_existing_report(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="校准报告缺失"):
        kg.write_approval(tmp_path, "sprt", MODEL)


def test_write_approval_requires_passing_report(tmp_path: Path) -> None:
    _write_report(tmp_path, passed=False)
    with pytest.raises(ValueError, match="五判据未全过"):
        kg.write_approval(tmp_path, "sprt", MODEL)


def test_write_approval_overwrites_and_unlocks(tmp_path: Path) -> None:
    _write_report(tmp_path)
    p = kg.write_approval(tmp_path, "sprt", MODEL, note="第一次")
    assert p.is_file()
    obj = json.loads(p.read_text(encoding="utf-8"))
    assert obj["kernel"] == "sprt" and obj["model"] == MODEL and obj["note"] == "第一次"
    assert kg.evaluate_kernel_gate(_cfg(tmp_path), "sprt").allowed is True

    kg.write_approval(tmp_path, "sprt", MODEL, note="重新批准(覆盖)")
    obj2 = json.loads(p.read_text(encoding="utf-8"))
    assert obj2["note"] == "重新批准(覆盖)"
    assert kg.evaluate_kernel_gate(_cfg(tmp_path), "sprt").allowed is True


# ---------------------------------------------------------------------------
# 4. 机读报告:save_report_json 结构 + report CLI 无条件落盘
# ---------------------------------------------------------------------------
def test_save_report_json_structure(tmp_path: Path) -> None:
    p = _write_report(tmp_path)
    assert p == kg.report_json_path(tmp_path, MODEL)
    obj = json.loads(p.read_text(encoding="utf-8"))
    assert obj["model"] == MODEL
    assert obj["generated_at"]
    assert obj["gate"]["passed"] is True
    assert obj["digest"] == kg.gate_digest(MODEL, obj["gate"])


def _seed_cli_corpus(root: Path) -> tuple[Path, Path]:
    """构造 CLI report 所需的标注集 + 评分文件(小闭环,零外呼)。"""
    labeled = root / "labeled.jsonl"
    lines = []
    for i in range(3):
        lines.append(json.dumps({
            "path": f"/tmp/n{i}.png", "sha256": f"{i}" * 64, "label": "clean",
            "labeled_by": "human:console", "labeled_at": "2026-11-10T00:00:00",
            "note": "",
        }, ensure_ascii=False))
    labeled.write_text("\n".join(lines) + "\n", encoding="utf-8")
    scores = root / "scores.jsonl"
    rows = [{"sha256": f"{i}" * 64, "path": f"/tmp/n{i}.png", "label": "clean",
             "nsfw_prob": 0.02, "model": MODEL, "cached": True, "error": ""}
            for i in range(3)]
    calib.save_scores(rows, scores, MODEL)
    return labeled, scores


def test_report_cli_json_at_custom_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """带 --config 的 report:机读 JSON 落到 <data_dir>/calibration/(与
    kernel_gate.evaluate 同一读取口径——制度闭环的关键对齐)。"""
    labeled, scores = _seed_cli_corpus(tmp_path)
    cfg_yaml = tmp_path / "cfg.yaml"
    cfg_yaml.write_text(f"data_dir: {tmp_path / 'data'}\n", encoding="utf-8")
    rc = calib.main([
        "report", "--set", str(labeled), "--scores", str(scores),
        "--config", str(cfg_yaml),
    ])
    assert rc == 0
    p = kg.report_json_path(tmp_path / "data", MODEL)
    assert p.is_file()
    obj = json.loads(p.read_text(encoding="utf-8"))
    assert obj["model"] == MODEL and "digest" in obj


# ---------------------------------------------------------------------------
# 5. approve CLI
# ---------------------------------------------------------------------------
def test_approve_cli_requires_passing_report(tmp_path: Path, capsys) -> None:
    labeled, scores = _seed_cli_corpus(tmp_path)
    cfg_yaml = tmp_path / "cfg.yaml"
    cfg_yaml.write_text(
        f"data_dir: {tmp_path / 'data'}\nclassifier: {MODEL}\n", encoding="utf-8"
    )
    # 门禁未过(样本量 C1 不满足:3 张 < 20)→ approve 拒绝
    calib.main(["report", "--set", str(labeled), "--scores", str(scores),
                "--config", str(cfg_yaml)])
    rc = calib.main(["approve", "--kernel", "sprt", "--config", str(cfg_yaml),
                     "--note", "过早批准"])
    assert rc == 1
    assert not kg.approval_path(tmp_path / "data").exists()


def test_approve_cli_after_passing_report(tmp_path: Path) -> None:
    """全生命周期:标注→评分→report(过)→approve→evaluate 放行。"""
    labeled, scores = _seed_cli_corpus(tmp_path)
    root = tmp_path / "data"
    # 直接写一份通过的机读报告(标注/评分闭环走 kernel_gate 单元已锁,
    # 此处聚焦 approve CLI 与 evaluate 的对齐)
    _write_report(root)
    cfg_yaml = tmp_path / "cfg.yaml"
    cfg_yaml.write_text(
        f"data_dir: {root}\nclassifier: {MODEL}\n", encoding="utf-8"
    )
    rc = calib.main(["approve", "--kernel", "sprt", "--config", str(cfg_yaml),
                     "--note", "门禁三锁验证通过,批准开启"])
    assert rc == 0
    assert kg.evaluate_kernel_gate(
        Config(data_dir=str(root), classifier=MODEL, ensemble_members=[]), "sprt"
    ).allowed is True
