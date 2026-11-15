"""GLM 误判率/校准测量(P2 第 5 步)——SPRT / cascade / 批处理的数字门禁。

背景与定位:SPRT 的错误率担保(α/β=0.05)以**校准概率**为前提
(decision/sprt.py 校准假设);cascade 的不确定带、多图批处理的分数分布
同理。本模块把「能不能开」从感觉变成**可复算的账本**:

1. **标注集(labeled set)**:JSONL 清单,每条带**溯源分级**
   (``labeled_by``:human=人标 > site_verdict=站点结论 > seed=种子启发)。
   报告按来源分层——mock/种子标签绝不冒充人标指标。
2. **评分(score)**:走**生产分类器链路**(``get_classifier`` →
   ``classify``,缓存与日预算一本账生效)——测量即生产路径的真实行为,
   不是旁路复制品;已有 vlm_cache 命中零外呼(2026-10-13 实弹留下的
   真实 GLM 分数可直接入账)。
3. **测量(measure)**:Brier / ECE(10 桶可靠性曲线)/ 生产三阈值
   (0.5 复核线、0.8 计数线、0.9 NSFW 线)混淆矩阵 / FP-FN 明细 /
   覆盖率(按来源分层)。
4. **门禁(sprt_gate)**:五判据逐条判定(样本量 / ECE / Brier /
   FN@0.9 / FP@0.5),任何一项不过即列出——**报告只出数字与判据结论,
   开启决策权永远留给运营者**(红线:全自动化≠无人监管)。

诚实边界:种子标签只含**内容可验证**的合成图(mock 纯色图按内容标
clean——文件名含 nsfw_hi 亦然,真实 GLM 判其 0.02-0.03 佐证);真实
NSFW 正样本必须由人工标注(``label`` 子命令),无正样本时 FN 门禁
自动判「不可测」而非通过。站点级对账(复核结论 vs 机器分)见
``decision.review_queue.reconcile_human_machine``(队列侧模块,本模块
不跨层引用)。

仅依赖标准库 + 同包;离线可用(评分除外——评分走生产链路,预算门禁在)。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, ImageScore

__all__ = [
    "LABEL_CLEAN",
    "LABEL_NSFW",
    "LabeledItem",
    "LabeledSet",
    "brier_score",
    "ece",
    "confusion_at",
    "measure",
    "sprt_gate",
    "render_markdown",
    "score_items",
    "seed_from_dirs",
    "add_pending_from_dirs",
    "import_labeled_dir",
    "IMPORT_SOURCE",
    "save_report_json",
    "main",
]

logger = logging.getLogger(__name__)

LABEL_CLEAN = "clean"
LABEL_NSFW = "nsfw"
VALID_LABELS = (LABEL_CLEAN, LABEL_NSFW)

#: 生产三阈值(与 contracts Config 默认同源;测量口径显式列出)
THRESHOLDS = {"review_0.5": 0.5, "count_0.8": 0.8, "nsfw_0.9": 0.9}

#: 门禁判据缺省值(可在调用/CLI 覆盖;判据本身与理由写进报告)
GATE_DEFAULTS = {
    "min_pos": 20,
    "min_neg": 20,
    "ece_max": 0.10,
    "brier_max": 0.20,
    "fn_max_at_nsw": 0.10,
    "fp_max_at_review": 0.10,
}

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


# ---------------------------------------------------------------------------
# 标注集
# ---------------------------------------------------------------------------
@dataclass
class LabeledItem:
    """一条标注(标签 + 溯源;lineage 决定它在报告里的可信层级)。"""

    path: str
    sha256: str = ""
    label: str = ""            # clean / nsfw;空串 = 待标注(pending)
    labeled_by: str = ""       # human:xxx / site_verdict:xxx / seed:xxx
    note: str = ""
    labeled_at: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "path": self.path, "sha256": self.sha256, "label": self.label,
            "labeled_by": self.labeled_by, "note": self.note,
            "labeled_at": self.labeled_at,
        }


class LabeledSet:
    """标注集清单(JSONL 持久化;标签条目与 pending 条目同文件)。"""

    def __init__(self, items: list[LabeledItem] | None = None) -> None:
        self.items: list[LabeledItem] = list(items or [])

    @staticmethod
    def load(path: str | Path) -> "LabeledSet":
        p = Path(path)
        items: list[LabeledItem] = []
        if p.is_file():
            for line_no, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"标注集 {p} 第 {line_no} 行不是合法 JSON:{exc}") from exc
                items.append(LabeledItem(
                    path=str(d.get("path", "")),
                    sha256=str(d.get("sha256", "")),
                    label=str(d.get("label", "")),
                    labeled_by=str(d.get("labeled_by", "")),
                    note=str(d.get("note", "")),
                    labeled_at=str(d.get("labeled_at", "")),
                ))
        s = LabeledSet(items)
        s._validate()
        return s

    def _validate(self) -> None:
        seen: set[str] = set()
        for it in self.items:
            if not it.path:
                raise ValueError("标注条目缺 path")
            if it.label and it.label not in VALID_LABELS:
                raise ValueError(f"非法标签 {it.label!r}(合法:{VALID_LABELS})")
            key = it.sha256 or it.path
            if key in seen:
                raise ValueError(f"标注集重复条目:{it.path}")
            seen.add(key)

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(it.as_dict(), ensure_ascii=False) for it in self.items]
        p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def labeled(self) -> list[LabeledItem]:
        return [i for i in self.items if i.label in VALID_LABELS]

    def pending(self) -> list[LabeledItem]:
        return [i for i in self.items if i.label not in VALID_LABELS]

    def stats(self) -> dict[str, Any]:
        lab = self.labeled()
        return {
            "total": len(self.items),
            "labeled": len(lab),
            "pending": len(self.items) - len(lab),
            "by_label": {v: sum(1 for i in lab if i.label == v) for v in VALID_LABELS},
            "by_source": _count_by(lab, lambda i: (i.labeled_by.split(":", 1)[0] or "?")),
        }


def _count_by(items: list[Any], key_fn) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        k = key_fn(it)
        out[k] = out.get(k, 0) + 1
    return out


def _sha256_of(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


#: mock 合成站图片的文件名特征(纯色/渐变合成图,内容可验证为 clean)
_MOCK_NAME_MARKERS = ("normal_", "nsfw_hi_", "nsfw_mid_")


def seed_from_dirs(dirs: list[str | Path]) -> tuple[LabeledSet, int]:
    """从证据/夹具目录生成种子标注集(只含内容可验证的种子标签)。

    规则(诚实分级):
    - mock 合成图(文件名 normal_* / nsfw_hi_* / nsfw_mid_*):按**内容**
      标 clean(``seed:mock-synthetic``)——文件名 nsfw_hi_* 亦为纯色
      合成图,2026-10-13 真实 GLM 判其 0.02-0.03 佐证;note 记录该事实,
      人可改标;
    - 其余图片 → pending(hint 记来源目录),留给 ``label`` 子命令人工标注。

    :return: (种子集, pending 数)。
    """
    items: list[LabeledItem] = []
    pending = 0
    seen_sha: set[str] = set()
    for d in dirs:
        root = Path(d)
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in _IMAGE_EXTS:
                continue
            sha = _sha256_of(p)
            if sha in seen_sha:
                continue  # 跨目录同内容(fixture 与 evidence 互为拷贝):只留首份
            seen_sha.add(sha)
            name = p.name.lower()
            if any(m in name for m in _MOCK_NAME_MARKERS):
                note = "mock 合成图,按内容标 clean"
                if "nsfw" in name:
                    note += "(文件名含 nsfw 但内容为纯色合成;真实 GLM 2026-10-13 判 0.02-0.03)"
                items.append(LabeledItem(
                    path=str(p), sha256=sha, label=LABEL_CLEAN,
                    labeled_by="seed:mock-synthetic", note=note,
                ))
            else:
                pending += 1
                items.append(LabeledItem(
                    path=str(p), sha256=sha, label="",
                    labeled_by="", note=f"待人工标注(来源:{p.parent.name})",
                ))
    s = LabeledSet(items)  # 构造内含校验(重复即抛,调用方修目录)
    return s, pending


def add_pending_from_dirs(s: LabeledSet, dirs: list[str | Path]) -> int:
    """把目录新图并入**既有**标注集为 pending(sha 去重;绝不碰已有标签)。

    何时需要:门禁 C1 要求正/负样本各 ≥20,单份语料常常只有一侧充足
    (证据目录天然偏 nsfw、夹具目录天然偏 clean)——运营者补充第二目录
    扩充语料时用本函数;与 :func:`seed_from_dirs` 的区别是**保留已标
    条目**(种子与人标都是资产,不可因扩充而丢)。

    :param s: 既有标注集(原地追加,调用方负责 save)。
    :param dirs: 图片目录(递归;后缀口径与 seed 同源)。
    :return: 新增 pending 条数(0 = 目录里没有新内容)。
    """
    known = {it.sha256 for it in s.items if it.sha256}
    added = 0
    for d in dirs:
        root = Path(d)
        if not root.is_dir():
            raise ValueError(f"补充语料目录不存在:{root}")
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in _IMAGE_EXTS:
                continue
            sha = _sha256_of(p)
            if sha in known:
                continue
            known.add(sha)
            s.items.append(LabeledItem(
                path=str(p), sha256=sha, label="", labeled_by="",
                note=f"待人工标注(补充:{p.parent.name})",
            ))
            added += 1
    if added:
        s._validate()  # 原地追加后的完整性(重复即抛)
    return added


#: 整组导入的缺省溯源(运营者对目录内容的整组判定;与逐张人标并列,
#: by_source 归 "human" 层,note 留目录审计线索)
IMPORT_SOURCE = "human:dir-import"


def import_labeled_dir(
    s: LabeledSet,
    dirs: list[str | Path],
    label: str,
    source: str = IMPORT_SOURCE,
) -> dict[str, int]:
    """把目录图片**整组导入为已标注**(正/负样本批量入口)。

    语义(数据集目录式标注:运营者对整组内容作出判定,溯源独立可审计):

    - 新图(不在集内)→ 追加为已标 ``label``;
    - 集内 **pending** 条目(如证据语料待标图)→ **升级**为已标(运营者
      的显式判定覆盖"待标",这正是补齐正样本缺口的捷径);
    - 集内**已标**条目 → **跳过绝不覆盖**(改标只能走显式改标路径,
      整组导入无权改既有标签——防误导覆盖人工资产);
    - ``label`` 只认 clean / nsfw(同 :data:`VALID_LABELS`);目录不存在
      中文 ValueError。

    :return: ``{"added": 新增, "promoted": pending 升级, "skipped": 已标跳过}``。
    """
    from netsentinel import timesource

    if label not in VALID_LABELS:
        raise ValueError(f"非法标签 {label!r}(合法:{VALID_LABELS})")
    known: dict[str, LabeledItem] = {it.sha256: it for it in s.items if it.sha256}
    out = {"added": 0, "promoted": 0, "skipped": 0}
    now = timesource.now_iso()
    for d in dirs:
        root = Path(d)
        if not root.is_dir():
            raise ValueError(f"导入目录不存在:{root}")
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in _IMAGE_EXTS:
                continue
            sha = _sha256_of(p)
            existing = known.get(sha)
            if existing is not None:
                if existing.label in VALID_LABELS:
                    out["skipped"] += 1          # 已标资产不覆盖
                    continue
                existing.label = label           # pending → 升级
                existing.labeled_by = source
                existing.labeled_at = now
                existing.note = f"目录整组导入({label};原 {existing.note or 'pending'})"
                out["promoted"] += 1
                continue
            s.items.append(LabeledItem(
                path=str(p), sha256=sha, label=label,
                labeled_by=source, labeled_at=now,
                note=f"目录整组导入({label};来源:{p.parent.name})",
            ))
            known[sha] = s.items[-1]
            out["added"] += 1
    if out["added"] or out["promoted"]:
        s._validate()
    return out


# ---------------------------------------------------------------------------
# 评分(生产链路)
# ---------------------------------------------------------------------------
def score_items(
    items: list[LabeledItem], cfg: Config, model: str
) -> list[dict[str, Any]]:
    """对已标注条目逐张评分(生产分类器链路:缓存/预算一本账生效)。

    :return: 评分行列表({sha256, path, label, nsfw_prob, model, cached,
        error});单张失败记 error 行,不中断整批。
    """
    from netsentinel.vision.classifier_base import get_classifier

    inst = get_classifier(model, cfg)
    rows: list[dict[str, Any]] = []
    for it in items:
        ev = ImageEvidence(
            path=it.path, url=f"file://{it.path}", source_page="calibration",
            sha256=it.sha256,
        )
        try:
            score = inst.classify(ev)
            rows.append({
                "sha256": it.sha256, "path": it.path, "label": it.label,
                "model": str(getattr(score, "model", model)),
                "nsfw_prob": float(score.nsfw_prob),
                "cached": bool((score.scores or {}).get("cached", False)),
                "error": "",
            })
            telemetry.inc("calibration.scored")
        except Exception as exc:  # noqa: BLE001 - 单张失败记行不中断
            rows.append({
                "sha256": it.sha256, "path": it.path, "label": it.label,
                "model": model, "nsfw_prob": -1.0, "cached": False,
                "error": str(exc)[:200],
            })
            telemetry.inc("calibration.score_errors")
    return rows


def save_scores(rows: list[dict[str, Any]], path: str | Path, model: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    p.write_text(f"# model={model}\n" + body + ("\n" if body else ""), encoding="utf-8")


def load_scores(path: str | Path) -> tuple[str, list[dict[str, Any]]]:
    p = Path(path)
    model = ""
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("# model="):
            model = line.split("=", 1)[1]
            continue
        rows.append(json.loads(line))
    return model, rows


# ---------------------------------------------------------------------------
# 度量(纯函数)
# ---------------------------------------------------------------------------
def brier_score(probs: list[float], labels01: list[int]) -> float:
    """Brier 分数:mean((p - y)^2);越小越好(0=完美,0.25=随机猜)。"""
    if not probs:
        return float("nan")
    return sum((p - y) ** 2 for p, y in zip(probs, labels01)) / len(probs)


def ece(
    probs: list[float], labels01: list[int], bins: int = 10
) -> tuple[float, list[dict[str, float]]]:
    """期望校准误差(ECE)+ 可靠性曲线桶数据。

    桶口径:[i/bins, (i+1)/bins) 左闭右开,末桶闭区间;每桶输出
    {bin_lo, bin_hi, n, avg_prob, freq}——ECE = Σ n_b/N × |avg_prob_b − freq_b|。
    """
    n = len(probs)
    if n == 0:
        return float("nan"), []
    total_gap = 0.0
    rows: list[dict[str, float]] = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, p in enumerate(probs)
               if (lo <= p < hi) or (b == bins - 1 and p == 1.0)]
        if not idx:
            rows.append({"bin_lo": lo, "bin_hi": hi, "n": 0,
                         "avg_prob": 0.0, "freq": 0.0})
            continue
        avg_p = sum(probs[i] for i in idx) / len(idx)
        freq = sum(labels01[i] for i in idx) / len(idx)
        total_gap += len(idx) / n * abs(avg_p - freq)
        rows.append({"bin_lo": lo, "bin_hi": hi, "n": float(len(idx)),
                     "avg_prob": round(avg_p, 4), "freq": round(freq, 4)})
    return total_gap, rows


def confusion_at(probs: list[float], labels01: list[int], threshold: float) -> dict[str, Any]:
    """单阈值混淆(label=1 为 NSFW 正类,p ≥ threshold 判正)。"""
    tp = fp = fn = tn = 0
    for p, y in zip(probs, labels01):
        pred = 1 if p >= threshold else 0
        if pred and y:
            tp += 1
        elif pred and not y:
            fp += 1
        elif not pred and y:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    return {
        "threshold": threshold, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": precision, "recall": recall,
        "fp_rate": (fp / (fp + tn)) if (fp + tn) else None,
        "fn_rate": (fn / (tp + fn)) if (tp + fn) else None,
    }


def measure(items: list[LabeledItem], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """标注 × 评分 → 校准/误判报告(按 sha 连接;来源分层;覆盖率)。"""
    label_by_sha = {i.sha256: i for i in items if i.label in VALID_LABELS and i.sha256}
    joined: list[tuple[LabeledItem, dict[str, Any]]] = []
    errors = 0
    for r in rows:
        it = label_by_sha.get(str(r.get("sha256", "")))
        if it is None:
            continue
        if r.get("error") or float(r.get("nsfw_prob", -1)) < 0:
            errors += 1
            continue
        joined.append((it, r))
    covered_shas = {i.sha256 for i, _ in joined}

    def _block(subset: list[tuple[LabeledItem, dict[str, Any]]]) -> dict[str, Any]:
        probs = [float(r["nsfw_prob"]) for _, r in subset]
        y = [1 if it.label == LABEL_NSFW else 0 for it, _ in subset]
        e, bins = ece(probs, y)
        return {
            "n": len(subset),
            "pos": sum(y), "neg": len(y) - sum(y),
            "brier": round(brier_score(probs, y), 4),
            "ece": round(e, 4) if subset else None,
            "reliability_bins": bins,
            "confusion": {
                name: confusion_at(probs, y, thr) for name, thr in THRESHOLDS.items()
            },
            "fp_at_review": [
                {"path": it.path, "label": it.label, "prob": float(r["nsfw_prob"])}
                for it, r in subset
                if it.label == LABEL_CLEAN and float(r["nsfw_prob"]) >= THRESHOLDS["review_0.5"]
            ],
            "fn_at_nsw": [
                {"path": it.path, "label": it.label, "prob": float(r["nsfw_prob"])}
                for it, r in subset
                if it.label == LABEL_NSFW and float(r["nsfw_prob"]) < THRESHOLDS["nsfw_0.9"]
            ],
        }

    human = _block([p for p in joined if p[0].labeled_by.startswith("human")])
    seed = _block([p for p in joined if not p[0].labeled_by.startswith("human")])
    return {
        "labeled_total": len(label_by_sha),
        "scored_ok": len(joined),
        "coverage": round(len(covered_shas) / len(label_by_sha), 4) if label_by_sha else None,
        "score_errors": errors,
        "by_source": {"human": human, "seed_or_site": seed},
        "all_sources": _block(joined),
    }


def sprt_gate(report: dict[str, Any], **overrides: float) -> dict[str, Any]:
    """数字门禁:五判据逐条判定(通过≠自动开启——决策权留给运营者)。

    判据(缺省见 :data:`GATE_DEFAULTS`,可覆盖):
    C1 样本量(pos ≥ min_pos 且 neg ≥ min_neg,人标层)
    C2 校准 ECE ≤ ece_max(人标层)
    C3 Brier ≤ brier_max(人标层)
    C4 漏报 FN@0.9 ≤ fn_max_at_nsw(人标层;无正样本判「不可测」)
    C5 误报 FP@0.5 ≤ fp_max_at_review(人标层;无负样本判「不可测」)
    """
    p = dict(GATE_DEFAULTS)
    p.update({k: v for k, v in overrides.items() if k in p})
    human = report.get("by_source", {}).get("human", {})

    def _c(name: str, ok: bool, detail: str) -> dict[str, Any]:
        return {"criterion": name, "pass": ok, "detail": detail}

    criteria = []
    pos, neg = human.get("pos", 0), human.get("neg", 0)
    criteria.append(_c(
        "C1 样本量", pos >= p["min_pos"] and neg >= p["min_neg"],
        f"人标正样本 {pos}/{p['min_pos']},负样本 {neg}/{p['min_neg']}",
    ))
    e = human.get("ece")
    criteria.append(_c(
        "C2 校准 ECE", e is not None and e <= p["ece_max"],
        f"ECE={e}(≤ {p['ece_max']} 为过;None=无人标样本)",
    ))
    b = human.get("brier")
    criteria.append(_c(
        "C3 Brier", b is not None and b <= p["brier_max"],
        f"Brier={b}(≤ {p['brier_max']} 为过)",
    ))
    conf = human.get("confusion", {})
    fn_rate = (conf.get("nsfw_0.9") or {}).get("fn_rate")
    criteria.append(_c(
        "C4 漏报 FN@0.9", fn_rate is not None and fn_rate <= p["fn_max_at_nsw"],
        f"FN@0.9={fn_rate}(≤ {p['fn_max_at_nsw']} 为过;None=无正样本,不可测)",
    ))
    fp_rate = (conf.get("review_0.5") or {}).get("fp_rate")
    criteria.append(_c(
        "C5 误报 FP@0.5", fp_rate is not None and fp_rate <= p["fp_max_at_review"],
        f"FP@0.5={fp_rate}(≤ {p['fp_max_at_review']} 为过;None=无负样本,不可测)",
    ))
    passed = all(c["pass"] for c in criteria)
    return {
        "criteria": criteria,
        "passed": passed,
        "verdict": (
            "门禁通过:SPRT/cascade 可提请开启(最终决策权在运营者;"
            "开启后 clean 早停站抽样入人工复核对账)"
            if passed else
            "门禁未过:维持默认关闭(use_sprt=False);未过判据如下,补齐后复测"
        ),
    }


def render_markdown(report: dict[str, Any], gate: dict[str, Any]) -> str:
    """把测量报告 + 门禁结论渲染为单文件 Markdown(运营者可读)。"""
    lines: list[str] = []
    a = lines.append
    a("# GLM 误判率 / 校准测量报告(P2 第 5 步)")
    a("")
    a(f"- 覆盖:已标注 {report['labeled_total']} 张,有效评分 {report['scored_ok']} 张"
      f"(覆盖率 {report['coverage']}),评分失败 {report['score_errors']} 张")
    a("")
    for src, title in (("human", "人标层(可信)"), ("seed_or_site", "种子/站点层(参考,不冒充人标)")):
        blk = report["by_source"][src]
        a(f"## {title}:n={blk['n']}(正 {blk['pos']} / 负 {blk['neg']})")
        a("")
        a(f"- Brier = {blk['brier']},ECE = {blk['ece']}")
        a("| 阈值 | TP | FP | FN | TN | 精确率 | 召回率 | FP率 | FN率 |")
        a("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for name, c in blk["confusion"].items():
            a(f"| {name} ({c['threshold']}) | {c['tp']} | {c['fp']} | {c['fn']} |"
              f" {c['tn']} | {c['precision']} | {c['recall']} |"
              f" {c['fp_rate']} | {c['fn_rate']} |")
        if blk["fp_at_review"]:
            a("")
            a("**FP 明细(≥0.5 的 clean 图)**:")
            for r in blk["fp_at_review"]:
                a(f"- {r['path']} → {r['prob']}")
        if blk["fn_at_nsw"]:
            a("")
            a("**FN 明细(<0.9 的 NSFW 图)**:")
            for r in blk["fn_at_nsw"]:
                a(f"- {r['path']} → {r['prob']}")
        a("")
    a("## SPRT/cascade 开启门禁(五判据)")
    a("")
    a("| 判据 | 结果 | 依据 |")
    a("| --- | --- | --- |")
    for c in gate["criteria"]:
        a(f"| {c['criterion']} | {'✅ 过' if c['pass'] else '❌ 未过'} | {c['detail']} |")
    a("")
    a(f"**结论:{gate['verdict']}**")
    a("")
    a("> 诚实边界:种子标签只含内容可验证的合成图;真实 NSFW 正样本必须人工标注;"
      "无正样本时 FN 判据为「不可测」而非通过。站点级对账见"
      " `python -m netsentinel.decision.review_queue reconcile`。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 机读报告(V10.37:kernel_gate 的事实源)
# ---------------------------------------------------------------------------
def save_report_json(
    data_dir: str | Path, model: str, report: dict[str, Any], gate: dict[str, Any]
) -> Path:
    """机读报告落盘:**内核门禁执行器**(``vision/kernel_gate.py``)的事实源。

    内容(全部由本函数一地构造,口径单一)::

        model          被测模型标签(与 scores 文件头一致)
        generated_at   落盘时刻(timesource.now_iso)
        report         measure() 全量测量(按来源分层)
        gate           sprt_gate() 五判据(criteria + passed)
        digest         gate_digest(model, gate)——把「这一份测量」钉死:
                       模型重测 / 标注集变化 → 判据 detail 变 → digest 变
                       → 引用旧 digest 的批准自动失效(想再开重新人批)

    原子落盘(tmp + :func:`netsentinel.atomic.replace_with_retry`,Windows
    安全);路径 = ``<data_dir>/calibration/report_<模型标签>.json``
    (与 kernel_gate.report_json_path 同一约定——**两处必须一致**,改一处
    必须同步另一处)。
    """
    from netsentinel import timesource
    from netsentinel.atomic import replace_with_retry
    from netsentinel.vision.kernel_gate import gate_digest, report_json_path

    payload = {
        "model": model,
        "generated_at": timesource.now_iso(),
        "report": report,
        "gate": gate,
        "digest": gate_digest(model, gate),
    }
    target = report_json_path(data_dir, model)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    replace_with_retry(tmp, target)
    return target


# ---------------------------------------------------------------------------
# CLI:init / label / import / score / report / approve
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    pr = argparse.ArgumentParser(
        prog="python -m netsentinel.vision.calibration",
        description="GLM 误判率/校准测量:标注集 + 评分 + 校准报告 + SPRT 数字门禁",
    )
    sub = pr.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="从证据/夹具目录生成种子标注集(合成图种子标+其余 pending)")
    p_init.add_argument("--dirs", nargs="+", required=True, help="图片目录(可多个)")
    p_init.add_argument("--out", default="data/calibration/labeled.jsonl", help="标注集路径")
    p_label = sub.add_parser("label", help="交互式人工标注 pending 条目(唯一可信标签来源)")
    p_label.add_argument("--set", default="data/calibration/labeled.jsonl", dest="path")
    p_import = sub.add_parser(
        "import", help="整组导入已标注目录(正/负样本批量入口;你对该目录内容作出整组判定)"
    )
    p_import.add_argument("--dirs", nargs="+", required=True, help="图片目录(可多个)")
    p_import.add_argument(
        "--label", required=True, choices=[LABEL_CLEAN, LABEL_NSFW],
        help="整组标签:clean(负样本)/ nsfw(正样本)——显式给出即视为你对整组内容的判定",
    )
    p_import.add_argument("--set", default="data/calibration/labeled.jsonl", dest="path")
    p_score = sub.add_parser("score", help="对已标注条目评分(生产链路:缓存/预算一本账)")
    p_score.add_argument("--set", default="data/calibration/labeled.jsonl", dest="path")
    p_score.add_argument("--model", default="", help="如 glm:glm-5.3-flash(缺省取 cfg.classifier)")
    p_score.add_argument("--config", default="", help="可选 config.yaml(vlm_cache_db 等路径)")
    p_score.add_argument("--out", default="", help="评分输出(缺省 data/calibration/scores_<model>.jsonl)")
    p_report = sub.add_parser("report", help="标注×评分 → 校准报告(markdown)+ 门禁判定")
    p_report.add_argument("--set", default="data/calibration/labeled.jsonl", dest="path")
    p_report.add_argument("--scores", required=True, help="score 子命令输出的 JSONL")
    p_report.add_argument("--out", default="", help="报告输出路径(缺省打印 stdout)")
    p_report.add_argument(
        "--config", default="",
        help="可选 config.yaml(data_dir 定位机读报告;<data_dir>/calibration/)",
    )
    from netsentinel.vision.kernel_gate import GATED_KERNELS
    p_approve = sub.add_parser(
        "approve",
        help="运营者显式批准受门禁内核(仅当当前模型的报告门禁通过时可写)",
    )
    p_approve.add_argument(
        "--kernel", required=True, choices=list(GATED_KERNELS),
        help=f"受门禁内核({'/'.join(GATED_KERNELS)})",
    )
    p_approve.add_argument(
        "--model", default="", help="被批模型(如 glm:glm-5.3-flash;缺省取 cfg.classifier)"
    )
    p_approve.add_argument(
        "--config", default="", help="可选 config.yaml(data_dir / 缺省模型)"
    )
    p_approve.add_argument("--note", default="", help="批准备注(运营者留痕)")
    return pr


def _load_cfg(config_path: str) -> Config:
    if config_path:
        from netsentinel.config import load_config
        return load_config(config_path)
    return Config()


def _model_tag(model: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in model)[:40] or "model"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _build_parser().parse_args(argv)
    try:
        if args.cmd == "init":
            s, pending = seed_from_dirs(args.dirs)
            s.save(args.out)
            print(f"种子标注集已写入 {args.out}:{s.stats()};pending {pending} 条"
                  "待人工标注(python -m netsentinel.vision.calibration label)")
            return 0
        if args.cmd == "label":
            s = LabeledSet.load(args.path)
            import netsentinel.timesource as ts
            changed = 0
            for it in s.pending():
                print(f"\n{it.path}\n  提示:{it.note or '(无)'}")
                ans = input("  标签 [c=clean n=nsfw s=跳过 q=退出]: ").strip().lower()
                if ans == "q":
                    break
                if ans == "s":
                    continue
                if ans not in ("c", "n"):
                    print("  未识别,跳过")
                    continue
                it.label = LABEL_CLEAN if ans == "c" else LABEL_NSFW
                it.labeled_by = "human:console"
                it.labeled_at = ts.now_iso()
                changed += 1
            if changed:
                s.save(args.path)
            print(f"已标注 {changed} 条;当前统计:{s.stats()}")
            return 0
        if args.cmd == "import":
            s = LabeledSet.load(args.path)
            result = import_labeled_dir(s, args.dirs, args.label)
            if result["added"] or result["promoted"]:
                s.save(args.path)
            print(
                f"整组导入({args.label}):新增 {result['added']},"
                f"pending 升级 {result['promoted']},已标跳过 {result['skipped']}"
                f"(溯源 {IMPORT_SOURCE});当前统计:{s.stats()}"
            )
            return 0
        if args.cmd == "score":
            s = LabeledSet.load(args.path)
            labeled = s.labeled()
            if not labeled:
                print("标注集没有已标注条目,先 label", file=sys.stderr)
                return 1
            cfg = _load_cfg(args.config)
            model = args.model or cfg.classifier
            rows = score_items(labeled, cfg, model)
            out = args.out or f"data/calibration/scores_{_model_tag(model)}.jsonl"
            save_scores(rows, out, model)
            errors = sum(1 for r in rows if r["error"])
            print(f"评分完成:{len(rows)} 张(失败 {errors})→ {out}"
                  f"(缓存命中免外呼;预算一本账已扣)")
            return 0
        if args.cmd == "report":
            s = LabeledSet.load(args.path)
            _model, rows = load_scores(args.scores)
            report = measure(s.labeled(), rows)
            gate = sprt_gate(report)
            md = render_markdown(report, gate)
            # 机读报告无条件落盘(kernel_gate 事实源):md 给人看,JSON 给
            # 门禁执行器看;两份内容同源同刻,digest 把这份数据钉死。
            cfg = _load_cfg(args.config)
            json_path = save_report_json(cfg.data_dir, _model, report, gate)
            if args.out:
                Path(args.out).parent.mkdir(parents=True, exist_ok=True)
                Path(args.out).write_text(md, encoding="utf-8")
                print(f"报告已写入 {args.out}")
            else:
                print(md)
            print(
                f"机读门禁报告:{json_path}(kernel_gate 事实源,"
                "批准将绑定这份测量的指纹)"
            )
            return 0
        if args.cmd == "approve":
            from netsentinel.vision.kernel_gate import write_approval
            cfg = _load_cfg(args.config)
            model = args.model or cfg.classifier
            if not model:
                print("错误:未指定 --model 且配置没有 classifier", file=sys.stderr)
                return 1
            p = write_approval(cfg.data_dir, args.kernel, model, args.note)
            print(
                f"内核批准已写入 {p}:kernel={args.kernel} model={model}\n"
                "(批准绑定了当前通过报告的测量指纹;重测后指纹变化,"
                "旧批准自动失效,需重新 approve)"
            )
            return 0
        return 1  # pragma: no cover
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"错误:{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
