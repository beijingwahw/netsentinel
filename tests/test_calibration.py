"""P2 第 5 步 校准测量测试(全部离线、零外呼)。

覆盖四组:

1. **度量纯函数**:Brier / ECE(完美校准≈0、系统性过信>0、可靠性桶)、
   三阈值混淆(FP/FN/率);
2. **标注集**:加载/保存/校验(非法标签、重复条目)、种子生成
   (mock 合成图按内容标 clean + nsfw_hi 文件名事实注记、其余 pending)、
   溯源分层统计;
3. **测量与门禁**:measure 按来源分层与 FP/FN 明细;sprt_gate 五判据
   分支(样本不足/无正样本判不可测/全过/单项不过);
4. **评分与对账**:score_items 走生产链路(stub 注册名)、错误行不中断;
   review_queue machine_agg 迁移 + 入列定格 + reconcile 四口径。
"""
from __future__ import annotations

from typing import Any

import pytest

from netsentinel.contracts import Config, ImageEvidence, ImageScore, PageSample, SiteReport, Verdict
from netsentinel.vision.calibration import (
    GATE_DEFAULTS,
    LABEL_CLEAN,
    LABEL_NSFW,
    LabeledItem,
    LabeledSet,
    brier_score,
    confusion_at,
    ece,
    measure,
    render_markdown,
    score_items,
    seed_from_dirs,
    sprt_gate,
)
from netsentinel.vision.classifier_base import NsfwClassifier, register_classifier

SITE = "http://localhost/a"


def _sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def calibration_import(s, dirs, label):
    """测试包装:整组导入(与被测函数同名语义解耦,便于本文件旧用例阅读)。"""
    from netsentinel.vision.calibration import import_labeled_dir

    return import_labeled_dir(s, dirs, label)


# ---------------------------------------------------------------------------
# 1. 度量纯函数
# ---------------------------------------------------------------------------
def test_brier_bounds() -> None:
    assert brier_score([0.0, 1.0], [0, 1]) == 0.0          # 完美
    assert brier_score([1.0, 0.0], [0, 1]) == 1.0          # 全错
    assert abs(brier_score([0.5, 0.5], [0, 1]) - 0.25) < 1e-9  # 随机猜


def test_ece_perfect_vs_overconfident() -> None:
    # 完美校准构造:每桶 20 样本、桶内均值 == 正例频率(确定性构造)
    probs2, labels2 = [], []
    for b in range(10):
        target = b / 10 + 0.05          # 桶内均值目标
        positives = round(target * 20)  # 频率 == 均值 → 桶间隙 0
        probs2.extend([target] * 20)
        labels2.extend([1] * positives + [0] * (20 - positives))
    e_perfect, bins2 = ece(probs2, labels2, bins=10)
    assert e_perfect < 1e-9, (e_perfect, bins2)
    # 系统性过信:clean(标签 0)全给 0.9 → ECE 大
    e_over, _ = ece([0.9] * 20, [0] * 20)
    assert e_over >= 0.85


def test_confusion_at_thresholds() -> None:
    probs = [0.95, 0.85, 0.6, 0.2]
    y = [1, 1, 0, 1]  # TP/FP/FN 混合
    c5 = confusion_at(probs, y, 0.5)
    assert (c5["tp"], c5["fp"], c5["fn"], c5["tn"]) == (2, 1, 1, 0)
    assert c5["recall"] == pytest.approx(2 / 3)
    c9 = confusion_at(probs, y, 0.9)
    assert (c9["tp"], c9["fn"]) == (1, 2)
    assert confusion_at([], [], 0.5)["fp_rate"] is None  # 空集不可测


# ---------------------------------------------------------------------------
# 2. 标注集
# ---------------------------------------------------------------------------
def _item(path: str, sha: str, label: str = "", by: str = "") -> LabeledItem:
    return LabeledItem(path=path, sha256=sha, label=label, labeled_by=by)


def test_labeled_set_roundtrip_and_validation(tmp_path) -> None:
    p = tmp_path / "labeled.jsonl"
    s = LabeledSet([_item("a.png", "a" * 64, LABEL_CLEAN, "human:console"),
                    _item("b.png", "b" * 64, "", "")])
    s.save(p)
    loaded = LabeledSet.load(p)
    assert loaded.stats()["labeled"] == 1 and loaded.stats()["pending"] == 1

    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"path": "x.png", "label": "banana"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="非法标签"):
        LabeledSet.load(bad)

    dup = tmp_path / "dup.jsonl"
    LabeledSet([_item("a.png", "a" * 64), _item("a2.png", "a" * 64)]).save(dup)
    with pytest.raises(ValueError, match="重复"):
        LabeledSet.load(dup)


def test_seed_from_dirs(tmp_path) -> None:
    (tmp_path / "mock").mkdir()
    (tmp_path / "mock" / "normal_1.png").write_bytes(b"\x89PNG fake")
    (tmp_path / "mock" / "nsfw_hi_2.png").write_bytes(b"\x89PNG fake2")
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "logo_x.png").write_bytes(b"realbytes")
    s, pending = seed_from_dirs([tmp_path / "mock", tmp_path / "real"])
    labels = {i.path: i for i in s.items}
    n1 = labels[str(tmp_path / "mock" / "normal_1.png")]
    assert n1.label == LABEL_CLEAN and n1.labeled_by == "seed:mock-synthetic"
    hi = labels[str(tmp_path / "mock" / "nsfw_hi_2.png")]
    assert hi.label == LABEL_CLEAN  # 按内容:纯色合成图
    assert "真实 GLM" in hi.note    # 事实注记留痕
    assert labels[str(tmp_path / "real" / "logo_x.png")].label == ""
    assert pending == 1


# ---------------------------------------------------------------------------
# 3. 测量与门禁
# ---------------------------------------------------------------------------
def _mk(rows: list[tuple[str, str, float]]) -> tuple[list[LabeledItem], list[dict]]:
    """(sha, label, prob) → items + score rows;labeled_by 按 label 前缀 h/s。"""
    items, scores = [], []
    for sha, label, prob in rows:
        by = "human:console" if sha.startswith("h") else "seed:mock-synthetic"
        items.append(_item(f"{sha}.png", sha + "0" * 62, label, by))
        scores.append({"sha256": sha + "0" * 62, "path": f"{sha}.png", "label": label,
                       "model": "m", "nsfw_prob": prob, "cached": False, "error": ""})
    return items, scores


def test_measure_layering_and_fp_fn_detail() -> None:
    items, rows = _mk([
        ("h1", LABEL_NSFW, 0.97),   # 人标正,高分 TP
        ("h2", LABEL_CLEAN, 0.7),   # 人标负,≥0.5 → FP 明细
        ("h3", LABEL_NSFW, 0.3),    # 人标正,<0.9 → FN 明细
        ("s1", LABEL_CLEAN, 0.02),  # 种子层,不进人标统计
    ])
    report = measure(items, rows)
    human = report["by_source"]["human"]
    assert human["n"] == 3 and human["pos"] == 2 and human["neg"] == 1
    assert report["by_source"]["seed_or_site"]["n"] == 1
    assert [r["path"] for r in human["fp_at_review"]] == ["h2.png"]
    assert [r["path"] for r in human["fn_at_nsw"]] == ["h3.png"]
    assert report["coverage"] == 1.0


def test_measure_skips_error_rows_and_uncovered() -> None:
    items, rows = _mk([("h1", LABEL_CLEAN, 0.01)])
    # 已标注但评分失败的条目(z1):计入 score_errors,不进有效样本
    rows.append({"sha256": "z" + "0" * 63, "path": "z1.png", "label": LABEL_CLEAN,
                 "model": "m", "nsfw_prob": -1.0, "cached": False, "error": "HTTP 429"})
    items.append(_item("z1.png", "z" + "0" * 63, LABEL_CLEAN, "human:console"))
    items.append(_item("never_scored.png", "q" * 64, LABEL_CLEAN, "human:console"))
    report = measure(items, rows)
    assert report["scored_ok"] == 1 and report["score_errors"] == 1
    # 覆盖 = 有效评分 / 已标注(h1 有效;z1 失败;never 未评 → 1/3)
    assert report["coverage"] == pytest.approx(1 / 3, abs=1e-3)  # 报告按 4 位小数


def test_sprt_gate_branches() -> None:
    def rep(pos: int, neg: int, ece_v: float, brier_v: float,
            fnr: float | None, fpr: float | None) -> dict:
        return {"by_source": {"human": {
            "n": pos + neg, "pos": pos, "neg": neg,
            "brier": brier_v, "ece": ece_v,
            "confusion": {
                "nsfw_0.9": {"fn_rate": fnr},
                "review_0.5": {"fp_rate": fpr},
            },
        }}}

    # 样本不足 → C1 未过;无正样本 → C4 不可测
    g = sprt_gate(rep(3, 30, 0.02, 0.05, None, 0.0))
    names = {c["criterion"]: c["pass"] for c in g["criteria"]}
    assert names["C1 样本量"] is False
    assert names["C4 漏报 FN@0.9"] is False  # 不可测 ≠ 通过
    assert g["passed"] is False and "门禁未过" in g["verdict"]

    # 全过
    g2 = sprt_gate(rep(25, 25, 0.05, 0.10, 0.02, 0.03))
    assert g2["passed"] is True and "门禁通过" in g2["verdict"]
    assert "运营者" in g2["verdict"]  # 决策权留人

    # 单项不过(校准差)
    g3 = sprt_gate(rep(25, 25, 0.30, 0.10, 0.02, 0.03))
    assert g3["passed"] is False
    assert {c["criterion"]: c["pass"] for c in g3["criteria"]}["C2 校准 ECE"] is False


def test_gate_defaults_overridable() -> None:
    def rep(pos, neg):
        return {"by_source": {"human": {
            "n": pos + neg, "pos": pos, "neg": neg, "brier": 0.1, "ece": 0.05,
            "confusion": {"nsfw_0.9": {"fn_rate": 0.0},
                          "review_0.5": {"fp_rate": 0.0}}}}}
    assert sprt_gate(rep(10, 10))["passed"] is False           # 缺省 20
    assert sprt_gate(rep(10, 10), min_pos=10, min_neg=10)["passed"] is True
    assert GATE_DEFAULTS["min_pos"] == 20


def test_render_markdown_smoke() -> None:
    items, rows = _mk([("h1", LABEL_NSFW, 0.97), ("h2", LABEL_CLEAN, 0.7)])
    report = measure(items, rows)
    gate = sprt_gate(report)
    md = render_markdown(report, gate)
    for token in ("校准测量报告", "人标层", "SPRT/cascade 开启门禁", "门禁"):
        assert token in md


# ---------------------------------------------------------------------------
# 4. 评分(生产链路)与队列对账
# ---------------------------------------------------------------------------
class CalibFake(NsfwClassifier):
    """按 sha 首字符定分(注册名走生产 get_classifier 链路)。"""

    name = "calib_fake"
    calls = 0

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg

    def classify(self, img: ImageEvidence) -> ImageScore:
        CalibFake.calls += 1
        if img.path.endswith("gone.png"):
            raise ValueError("图片文件不存在(模拟)")
        prob = 0.9 if img.sha256.startswith("h") else 0.1
        return ImageScore(image=img, model=self.name, nsfw_prob=prob)


register_classifier("calib_fake", CalibFake)


def test_score_items_production_path(tmp_path, monkeypatch) -> None:
    CalibFake.calls = 0
    img = tmp_path / "x.png"
    img.write_bytes(b"png")
    items = [_item(str(img), "h" * 64, LABEL_NSFW, "human:console"),
             _item(str(tmp_path / "gone.png"), "z" * 64, LABEL_CLEAN, "human:console")]
    rows = score_items(items, Config(), "calib_fake")
    assert CalibFake.calls == 2  # 生产链路逐张评分(第二张进入 classify 后抛错)
    assert rows[0]["nsfw_prob"] == 0.9 and rows[0]["error"] == ""
    assert rows[1]["nsfw_prob"] == -1.0 and rows[1]["error"]  # 单张失败记行不中断


def test_review_queue_machine_agg_and_reconcile(tmp_path) -> None:
    from netsentinel.decision.review_queue import ReviewQueue, reconcile_human_machine

    q = ReviewQueue(str(tmp_path / "q.db"))

    def report(agg: float, verdict: Verdict = Verdict.NSFW) -> SiteReport:
        return SiteReport(site_url=f"http://s/{agg}", pages=[], image_scores=[],
                          agg_nsw_prob=agg, verdict=verdict, needs_review=True)

    id_nsw = q.add(report(0.97), "z1.zip")                 # 机器 nsfw 高分
    id_suspect = q.add(report(0.6, Verdict.SUSPECT), "z2.zip")
    id_trustlost = q.add(report(0.92), "z3.zip",
                         note="[识别失败 2 张,判定置信受损]")
    id_fp = q.add(report(0.95), "z4.zip")                  # 机器最高档,人否决
    q.approve(id_nsw)
    q.approve(id_suspect)
    q.reject(id_trustlost)
    q.reject(id_fp)
    listed = q.list(status="rejected")
    aggs = sorted(e.machine_agg for e in listed)
    assert aggs == [0.92, 0.95]                            # 对账字段入列定格
    q.close()

    r = reconcile_human_machine(str(tmp_path / "q.db"))
    # 失信条目单独口径:rejected 只计非失信的 1 条(id_fp)
    assert r["human_confirmed"] == 2 and r["human_rejected"] == 1
    assert r["machine_nsw_confirmed_by_human"] == 1        # nsfw 档被确认
    assert r["machine_suspect_confirmed_by_human"] == 1    # suspect 档被人补判
    assert r["trust_lost_entries"] == 1                    # 识别失信单独口径
    assert r["fp_candidates"] == 1                         # 高分被否决 = FP 候选
    assert r["fp_candidate_rows"][0]["machine_agg"] == 0.95
    assert "FN" in r["fn_note"]                            # 诚实边界留痕


def test_review_queue_old_db_migration(tmp_path) -> None:
    """旧库(无 machine_agg 列)打开即自动补列,老数据补 -1。"""
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE entries (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " site_url TEXT NOT NULL, verdict TEXT NOT NULL, status TEXT NOT NULL"
        " DEFAULT 'pending', evidence_zip TEXT NOT NULL DEFAULT '',"
        " created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',"
        " note TEXT NOT NULL DEFAULT '', priority_weight REAL NOT NULL DEFAULT 0.0)"
    )
    conn.execute(
        "INSERT INTO entries (site_url, verdict) VALUES ('http://old', 'suspect')"
    )
    conn.commit()
    conn.close()

    from netsentinel.decision.review_queue import ReviewQueue
    q = ReviewQueue(str(db))
    rows = q.list()
    assert rows and rows[0].machine_agg == -1.0            # 迁移补列 + 老数据 -1
    new_id = q.add(SiteReport(site_url="http://new", pages=[], image_scores=[],
                              agg_nsw_prob=0.5, verdict=Verdict.SUSPECT,
                              needs_review=True))
    assert q.list(status="pending")[-1].machine_agg == 0.5  # 新条目正常定格
    q.close()


# ---------------------------------------------------------------------------
# V10.37:整组导入正/负样本(import_labeled_dir + CLI import)
# ---------------------------------------------------------------------------
def test_import_labeled_dir_adds_new_as_labeled(tmp_path) -> None:
    d = tmp_path / "pos"
    d.mkdir()
    (d / "a.png").write_bytes(b"\x89PNG pos-a")
    (d / "b.jpg").write_bytes(b"\xff\xd8 jpg-b")
    s = LabeledSet()
    result = calibration_import(s, [d], LABEL_NSFW)
    assert result == {"added": 2, "promoted": 0, "skipped": 0}
    lab = s.labeled()
    assert len(lab) == 2 and all(i.label == LABEL_NSFW for i in lab)
    assert all(i.labeled_by == "human:dir-import" for i in lab)  # 溯源独立可审计
    assert all(i.labeled_at for i in lab)
    assert all("目录整组导入" in i.note for i in lab)


def test_import_labeled_dir_promotes_pending_not_labeled(tmp_path) -> None:
    d = tmp_path / "pos"
    d.mkdir()
    (d / "a.png").write_bytes(b"\x89PNG pos-a")
    s = LabeledSet([LabeledItem(path="old.png", sha256=_sha(b"\x89PNG pos-a"),
                                label="", labeled_by="", note="待人工标注")])
    result = calibration_import(s, [d], LABEL_NSFW)
    assert result == {"added": 0, "promoted": 1, "skipped": 0}
    it = s.items[0]
    assert it.label == LABEL_NSFW and it.labeled_by == "human:dir-import"
    assert "目录整组导入" in it.note and "待人工标注" in it.note  # 原线索保留


def test_import_labeled_dir_never_overwrites_labeled(tmp_path) -> None:
    d = tmp_path / "pos"
    d.mkdir()
    (d / "a.png").write_bytes(b"\x89PNG pos-a")
    s = LabeledSet([LabeledItem(path="old.png", sha256=_sha(b"\x89PNG pos-a"),
                                label=LABEL_CLEAN, labeled_by="human:webui")])
    result = calibration_import(s, [d], LABEL_NSFW)
    assert result == {"added": 0, "promoted": 0, "skipped": 1}
    it = s.items[0]
    assert it.label == LABEL_CLEAN and it.labeled_by == "human:webui"  # 人工资产原封不动


def test_import_labeled_dir_reimport_idempotent(tmp_path) -> None:
    d = tmp_path / "pos"
    d.mkdir()
    (d / "a.png").write_bytes(b"\x89PNG pos-a")
    s = LabeledSet()
    assert calibration_import(s, [d], LABEL_NSFW)["added"] == 1
    result = calibration_import(s, [d], LABEL_NSFW)  # 二刷:已标全部跳过
    assert result == {"added": 0, "promoted": 0, "skipped": 1}
    assert len(s.items) == 1


def test_import_labeled_dir_validates_label_and_dir(tmp_path) -> None:
    s = LabeledSet()
    with pytest.raises(ValueError, match="非法标签"):
        calibration_import(s, [tmp_path], "suspect")
    with pytest.raises(ValueError, match="不存在"):
        calibration_import(s, ["Z:/no/such/dir"], LABEL_NSFW)


def test_import_cli_subcommand(tmp_path, capsys) -> None:
    from netsentinel.vision.calibration import main as calib_main

    d = tmp_path / "neg"
    d.mkdir()
    (d / "c.png").write_bytes(b"\x89PNG neg-c")
    setfile = tmp_path / "labeled.jsonl"
    LabeledSet().save(setfile)
    rc = calib_main(["import", "--dirs", str(d), "--label", "clean",
                     "--set", str(setfile)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "新增 1" in out and "pending 升级 0" in out and "已标跳过 0" in out
    back = LabeledSet.load(setfile)
    assert back.labeled()[0].label == LABEL_CLEAN
