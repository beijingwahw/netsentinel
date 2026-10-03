"""守卫模型标定核验工具(A240;A217 交付报告点名遗留项)。

背景
----
A217 的 :mod:`netsentinel.vision.guard_adapter` 把守卫模型的文本判定映射为
常数概率:Yes→``PROB_UNSAFE``(0.95)、No→``PROB_SAFE``(0.05)。该常量对
本身是**提示信息**(高置信但非满分的工程拍板值),真实模型上线前须用少量
人工标注图核验——本模块就是这套**最小样本核验协议**的实施工具,对标概率
标定工程实践(常数概率映射上线前的最小样本核验)。

协议(输入 → 输出)
------------------
输入 = 人工标注样本集 ``[{path, label: 0|1, guard_raw: "Yes"/"unsafe\\nS5"/…}, …]``:

- ``path``:     样本标识(本地图片路径或任意字符串键,只做透传/展示);
- ``label``:    人工真值,1=确认违规、0=确认合规(须 int,拒绝 bool);
- ``guard_raw``:守卫模型的原始输出文本。可直接给历史记录(离线回放),
  也可缺省、由注入的 ``classify_fn(path)`` **现场产生**(推理接口全部
  可注入/mock,绝不真实联网加载模型——离线红线)。

输出 = :class:`CalibrationReport`:

1. **混淆矩阵**(Yes/No × 真值):Yes(unsafe)判定记预测正类,与人工
   真值交叉计数 TP/FP/TN/FN;无法解析的畸形输出单列(unparseable),
   绝不混入矩阵(统计诚实性,红线 38);
2. **当前常量映射下的 Brier 分数**:对全部可解析样本,按
   ``guard_adapter.PROB_UNSAFE / PROB_SAFE`` 实时读取(只读对账,不复制
   快照)映射后与真值计算均方误差;
3. **建议常量对**(极大似然 + 拉普拉斯平滑,小样本防 0/1 走极端):

   - Yes→不安全概率 ``p̂_yes = (TP + 1) / (TP + FP + 2)``;
   - No→不安全概率 ``p̂_no  = (FN + 1) / (FN + TN + 2)``;

   建议与现值差异超容差(默认 0.05)时输出中文告警;
4. **样本量充分性**(CP 风格,对齐 :mod:`netsentinel.decision.conformal`
   的 ``MIN_CALIBRATION_N``):n < 30 时显式"样本不足,仅演示"降级标记,
   MLE 建议不构成采纳依据(红线 38 统计诚实性);
5. **llamaguard 变体**:类别码 S1..S14 的每类支持度统计(每条 unsafe
   记录的类别码按解析器去重后计 1 次);
6. **阈值敏感性**:判决线 0.5 附近的脆弱带(映射概率落在
   ``[0.5-δ, 0.5+δ]`` 的样本占比),以及映射常量 ±δ(默认 0.05)四组合
   扰动下的逐样本翻转计数——当前常量对 0.95/0.05 远离 0.5,理论翻转恒
   为 0;建议常量对若逼近 0.5(如 TP≈FP)则翻转激增,正是本项要暴露的风险。

安全红线(与全库对齐):

- 零第三方依赖:仅标准库(argparse/json/dataclasses/pathlib/typing);
- 纯离线:``classify_fn`` 是注入协议,本模块绝不加载模型、绝不联网;
- ``guard_adapter`` / ``conformal`` 只读 import(常量与思想对账,绝不改写);
- **产出仅为标定建议**:常量映射(0.95/0.05)是否采纳是人工决策,本模块
  不改任何生产常量、不写任何状态文件。

CLI::

    python -m netsentinel.vision.guard_calib <records.jsonl> \
        [--family shieldgemma2|llamaguard|custom-prompt] [--tolerance 0.05] \
        [--delta 0.05]

records.jsonl 每行一个 JSON 对象(path/label/guard_raw;CLI 模式无
classify_fn,guard_raw 必填)。返回码:0 成功;2 文件/记录/参数错误。

用法示例(离线注入)::

    from netsentinel.vision.guard_calib import calibrate

    records = [{"path": "a.png", "label": 1, "guard_raw": "Yes"},
               {"path": "b.png", "label": 0, "guard_raw": "No"}]
    report = calibrate(records, family="shieldgemma2")
    print(report.render())          # 可直呈运营者的中文报告
    report.as_dict()                # 结构化输出(测试/下游工具)
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from netsentinel import telemetry
from netsentinel.vision import guard_adapter  # 兄弟模块,只读 import(常量对账)

__all__ = [
    "MIN_CALIBRATION_N",
    "DEFAULT_TOLERANCE",
    "SENSITIVITY_DELTA",
    "CAVEAT",
    "CalibrationReport",
    "calibrate",
    "load_records_jsonl",
    "main",
]

#: 样本量充分性门槛(与 decision/conformal.MIN_CALIBRATION_N 同值对账):
#: n < 30 时 MLE 建议在小样本上波动过大,显式降级为"样本不足,仅演示"。
MIN_CALIBRATION_N: int = 30

#: 建议常量对与现值差异的告警容差(默认 0.05,可经 calibrate(..., tolerance=) 覆盖)。
DEFAULT_TOLERANCE: float = 0.05

#: 阈值敏感性的常量扰动幅度(默认 ±0.05,可经 calibrate(..., delta=) 覆盖)。
SENSITIVITY_DELTA: float = 0.05

#: 固定随行前提(红线 38:不得宣传无条件结论)。
CAVEAT: str = (
    "核验依赖人工标注与守卫输出同源配对;畸形输出不混入统计;"
    f"n<{MIN_CALIBRATION_N} 仅演示、不构成采纳依据"
)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _sig(x: float) -> float:
    """规整到 12 位有效数字:清除除法/求和的浮点尾噪,保证输出确定性。

    与 decision/conformal._sig 同款手法(%g 保量级、去末尾噪声)。
    """
    return float(f"{x:.12g}")


@dataclass
class _Sample:
    """归一后的单条标注样本(内部结构)。"""

    path: str
    label: int            # 0|1,1=人工确认违规
    text: str | None      # 守卫原始输出文本(归一后;None=既无记录又未现场产生)


def _normalize_records(
    records: Sequence[Any],
    classify_fn: Callable[[str], Any] | None,
) -> list[_Sample]:
    """校验并归一标注样本集;非法项抛中文 ``ValueError``(红线 8 防御式)。

    ``guard_raw`` 优先级:记录内已有的历史值直接使用(确定性回放);
    缺失且注入了 ``classify_fn`` 时现场调用 ``classify_fn(path)`` 产生
    (返回任意形态,经 ``guard_adapter.coerce_model_text`` 归一为文本;
    ``classify_fn`` 自身异常**原样上抛**,由调用方兜底——标定协议不吞错)。
    """
    if not isinstance(records, (list, tuple)):
        raise ValueError(
            f"标注样本集应为 list/tuple,收到 {type(records).__name__}"
        )
    if not records:
        raise ValueError(
            "标注样本集为空:标定协议至少需要 1 条(path+label+guard_raw)"
            f"人工标注样本;建议积累不少于 {MIN_CALIBRATION_N} 例后再核验"
        )
    samples: list[_Sample] = []
    for idx, item in enumerate(records):
        if not isinstance(item, dict):
            raise ValueError(
                f"第 {idx} 条标注记录应为 dict"
                f"(path/label/guard_raw),收到 {type(item).__name__}:{item!r}"
            )
        path = item.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError(
                f"第 {idx} 条记录 path 非法:{path!r}(应为非空字符串,便于追溯)"
            )
        label = item.get("label")
        if isinstance(label, bool) or not isinstance(label, int) or label not in (0, 1):
            raise ValueError(
                f"第 {idx} 条记录 label 非法:{label!r}(应为 0|1 整数,"
                "1=人工确认违规、0=人工确认合规;bool 不接受)"
            )
        raw = item.get("guard_raw")
        if raw is None and classify_fn is not None:
            raw = classify_fn(path)  # 现场产生;异常原样上抛(见 docstring)
        if raw is None:
            raise ValueError(
                f"第 {idx} 条记录缺 guard_raw 且未注入 classify_fn:"
                "请补历史守卫输出,或经 classify_fn 现场产生(离线注入)"
            )
        samples.append(
            _Sample(path=path, label=label,
                    text=guard_adapter.coerce_model_text(raw))
        )
    return samples


def _decision(p_yes: float, p_no: float, verdict: str) -> bool:
    """按常量对映射出二值判决(verdict=unsafe 用 p_yes,safe 用 p_no;≥0.5 判违规)。"""
    return (p_yes if verdict == "unsafe" else p_no) >= 0.5


def _flip_count(
    verdicts: Sequence[str], p_yes: float, p_no: float, delta: float
) -> int:
    """常量对 ±δ 四组合扰动下的逐样本翻转计数。

    一个样本在**任一**扰动组合下 0.5 线判决翻转即计 1(取并集,而非单组合
    计数)。扰动值夹取 [0,1](建议常量贴近边界时 ±δ 可能越界)。
    """
    base = [_decision(p_yes, p_no, v) for v in verdicts]
    flips = 0
    for i, verdict in enumerate(verdicts):
        hit = False
        for d_yes in (-delta, delta):
            for d_no in (-delta, delta):
                py = min(1.0, max(0.0, p_yes + d_yes))
                pn = min(1.0, max(0.0, p_no + d_no))
                if _decision(py, pn, verdict) != base[i]:
                    hit = True
                    break
            if hit:
                break
        if hit:
            flips += 1
    return flips


def _fragile_share(
    verdicts: Sequence[str], p_yes: float, p_no: float, delta: float
) -> float:
    """脆弱带宽度:映射概率落在 [0.5-δ, 0.5+δ](含边界)的样本占比(描述性)。"""
    if not verdicts:
        return 0.0
    in_band = sum(
        1
        for v in verdicts
        if 0.5 - delta <= (p_yes if v == "unsafe" else p_no) <= 0.5 + delta
    )
    return _sig(in_band / len(verdicts))


def _pair_sensitivity(
    verdicts: Sequence[str], p_yes: float, p_no: float, delta: float
) -> dict[str, Any]:
    """单个常量对的敏感性画像(翻转计数 + 脆弱带占比)。"""
    return {
        "yes_prob": _sig(p_yes),
        "no_prob": _sig(p_no),
        "flips": _flip_count(verdicts, p_yes, p_no, delta),
        "fragile_band_share": _fragile_share(verdicts, p_yes, p_no, delta),
    }


def _category_stats(
    rows: Sequence[tuple[_Sample, str, dict[str, Any]]],
) -> dict[str, Any]:
    """llamaguard 类别码 S1..S14 的每类支持度统计(按记录去重计数)。

    每条 unsafe 记录的类别码经 :func:`guard_adapter.parse_llamaguard` 去重后,
    每码计 1 次;另单列 unsafe 无类别码 / safe / 畸形的条数与真值为违规的
    子统计(``category_counts_positive``,供治理侧对照"哪类最常真命中")。
    """
    counts: dict[str, int] = {}
    counts_pos: dict[str, int] = {}
    unsafe_no_category = 0
    n_safe = 0
    n_unparseable = 0
    for sample, verdict, meta in rows:
        if verdict == "safe":
            n_safe += 1
            continue
        if verdict != "unsafe":  # unparseable:单列,绝不混入类别统计
            n_unparseable += 1
            continue
        categories = meta.get("categories") or []
        if not categories:
            unsafe_no_category += 1
        for code in categories:
            counts[code] = counts.get(code, 0) + 1
            if sample.label == 1:
                counts_pos[code] = counts_pos.get(code, 0) + 1
    ordered = sorted(counts)
    ordered_pos = sorted(counts_pos)
    return {
        "category_counts": {code: counts[code] for code in ordered},
        "category_counts_positive": {
            code: counts_pos[code] for code in ordered_pos
        },
        "unsafe_without_category": unsafe_no_category,
        "n_safe": n_safe,
        "n_unparseable": n_unparseable,
        "n_records": len(rows),
    }


# ---------------------------------------------------------------------------
# 标定报告
# ---------------------------------------------------------------------------


@dataclass
class CalibrationReport:
    """守卫模型常量映射标定核验报告(纯数据对象,可离线断言/序列化)。

    字段语义见模块 docstring 的协议 1-6;全部为只读快照,
    :meth:`render` 生成可直呈运营者的中文报告。
    """

    family: str                                  # shieldgemma2 / llamaguard / custom-prompt
    n: int                                       # 样本总数(含畸形)
    n_parseable: int                             # 可解析样本数(Yes/No 或 safe/unsafe)
    n_unparseable: int                           # 畸形输出条数(单列,不入矩阵)
    confusion: dict[str, int]                    # tp/fp/tn/fn(Yes/No × 真值)
    unparseable_paths: list[str]                 # 畸形输出样本路径(追溯用)
    current_pair: tuple[float, float]            # (Yes→p, No→p) 现值(guard_adapter 实时读取)
    suggested_pair: tuple[float | None, float | None]  # MLE+拉普拉斯 建议(None=该侧无样本)
    brier_current: float | None                  # 现常量对下可解析样本的 Brier 分数
    warnings: list[str] = field(default_factory=list)  # 中文告警(超容差/缺判定侧/畸形)
    sufficient: bool = False                     # n ≥ MIN_CALIBRATION_N
    sufficiency_note: str = ""                   # 充分性说明(降级时含"样本不足,仅演示")
    sensitivity: dict[str, Any] = field(default_factory=dict)  # 阈值敏感性(见 _pair_sensitivity)
    category_stats: dict[str, Any] | None = None  # llamaguard 专属(其余族为 None)
    caveat: str = CAVEAT                         # 固定随行前提(红线 38)
    tolerance: float = DEFAULT_TOLERANCE         # 容差回显
    delta: float = SENSITIVITY_DELTA             # 扰动幅度回显

    def as_dict(self) -> dict[str, Any]:
        """全量结构化输出(JSON 可序列化,供测试与下游工具消费)。"""
        return {
            "family": self.family,
            "n": self.n,
            "n_parseable": self.n_parseable,
            "n_unparseable": self.n_unparseable,
            "confusion": dict(self.confusion),
            "unparseable_paths": list(self.unparseable_paths),
            "current_pair": tuple(self.current_pair),
            "suggested_pair": tuple(self.suggested_pair),
            "brier_current": self.brier_current,
            "warnings": list(self.warnings),
            "sufficient": self.sufficient,
            "sufficiency_note": self.sufficiency_note,
            "sensitivity": json.loads(json.dumps(self.sensitivity)),
            "category_stats": (
                json.loads(json.dumps(self.category_stats))
                if self.category_stats is not None
                else None
            ),
            "caveat": self.caveat,
            "tolerance": self.tolerance,
            "delta": self.delta,
        }

    def render(self) -> str:
        """汇编可直呈运营者的中文报告(含前提与免责,对齐 conformal.merge_reports)。"""
        tp = self.confusion.get("tp", 0)
        fp = self.confusion.get("fp", 0)
        tn = self.confusion.get("tn", 0)
        fn = self.confusion.get("fn", 0)
        cur_yes, cur_no = self.current_pair
        sug_yes, sug_no = self.suggested_pair

        def _fmt(value: float | None) -> str:
            return "无(该侧无判定样本)" if value is None else f"{value:.4f}"

        lines = [
            "【守卫模型标定核验报告(常量映射上线前最小样本核验)】",
            f"- 模型族:{self.family};样本 n={self.n}"
            f"(可解析 {self.n_parseable} / 畸形 {self.n_unparseable})",
            f"- 混淆矩阵(Yes/No × 真值,Yes=判不安全):"
            f"TP={tp} FP={fp} TN={tn} FN={fn}",
            f"- 当前常量对:Yes→{cur_yes:.4f}、No→{cur_no:.4f}"
            "(guard_adapter.PROB_UNSAFE/PROB_SAFE,实时只读对账);"
            + (
                f"该映射下 Brier 分数 = {self.brier_current:.4f}"
                if self.brier_current is not None
                else "无可解析样本,Brier 分数无从计算"
            ),
            f"- 建议常量对(MLE+拉普拉斯):"
            f"Yes→{_fmt(sug_yes)} [(TP+1)/(TP+FP+2)],"
            f"No→{_fmt(sug_no)} [(FN+1)/(FN+TN+2)]",
        ]
        if self.warnings:
            for warning in self.warnings:
                lines.append(f"- 告警:{warning}")
        else:
            lines.append(
                f"- 告警:无(建议与现值差异均在容差 {self.tolerance} 内)"
            )
        lines.append(
            f"- 样本充分性:{self.sufficiency_note}"
        )
        sens_cur = self.sensitivity.get("current") or {}
        sens_sug = self.sensitivity.get("suggested")
        lines.append(
            f"- 阈值敏感性(±{self.delta:g} 扰动,判决线 0.5):"
            f"当前对翻转 {sens_cur.get('flips', 0)} 例"
            f"(脆弱带占比 {float(sens_cur.get('fragile_band_share', 0.0)):.2%})"
            + (
                f";建议对翻转 {sens_sug.get('flips', 0)} 例"
                f"(脆弱带占比 {float(sens_sug.get('fragile_band_share', 0.0)):.2%})"
                if sens_sug
                else ";建议对不完整(有侧无样本),不评估"
            )
        )
        if self.category_stats is not None:
            counts = self.category_stats.get("category_counts") or {}
            support = (
                "、".join(f"{code}={cnt}" for code, cnt in counts.items())
                or "无任何类别码"
            )
            lines.append(
                f"- LlamaGuard 类别支持度:{support};"
                f"unsafe 无类别码 {self.category_stats.get('unsafe_without_category', 0)} 例、"
                f"safe {self.category_stats.get('n_safe', 0)} 例、"
                f"畸形 {self.category_stats.get('n_unparseable', 0)} 例"
            )
        if self.unparseable_paths:
            shown = "、".join(self.unparseable_paths[:3])
            suffix = f" 等 {len(self.unparseable_paths)} 条" if len(
                self.unparseable_paths
            ) > 3 else ""
            lines.append(f"- 畸形输出样本(前 3):{shown}{suffix}")
        lines.append(f"- 前提:{self.caveat}(标注须与守卫输出同源配对,并随数据漂移定期重核)")
        lines.append(
            "- 免责:本报告仅为标定建议,常量对(0.95/0.05)是否调整是人工决策;"
            "采纳前须人工复核样本与结论,不得据此自动改写生产常量或自动处置。"
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------


def calibrate(
    records: Sequence[Any],
    family: str = "shieldgemma2",
    classify_fn: Callable[[str], Any] | None = None,
    tolerance: float = DEFAULT_TOLERANCE,
    delta: float = SENSITIVITY_DELTA,
) -> CalibrationReport:
    """对人工标注样本集执行守卫常量映射的最小样本核验协议(纯离线)。

    :param records:    ``[{path, label: 0|1, guard_raw?}, ...]``;guard_raw 缺省
                       时须给 classify_fn 现场产生(注入/mock,绝不真实联网);
    :param family:     守卫模型族(:data:`guard_adapter.GUARD_FAMILIES` 之一),
                       决定用哪个解析器(Yes/No 或 safe/unsafe+类别码);
    :param classify_fn: ``path -> 守卫原始输出`` 的注入替身;仅对缺 guard_raw
                       的记录调用(历史值优先回放);其异常原样上抛;
    :param tolerance:  建议常量对与现值差异的告警容差,须严格落在 (0, 1) 开区间;
    :param delta:      阈值敏感性扰动幅度,须严格落在 (0, 0.5) 开区间。
    :raises ValueError: 样本/参数非法(中文消息,红线 8 防御式)。
    :return:           :class:`CalibrationReport`(仅建议,采纳是人工决策)。
    """
    family = str(family or "").strip().lower()
    if family not in guard_adapter.GUARD_FAMILIES:
        raise ValueError(
            f"未知的守卫模型族 family={family!r},"
            f"有效值:{'/'.join(guard_adapter.GUARD_FAMILIES)}"
        )
    if (
        isinstance(tolerance, bool)
        or not isinstance(tolerance, (int, float))
        or not (0.0 < float(tolerance) < 1.0)
    ):
        raise ValueError(
            f"容差 tolerance 必须严格落在 (0, 1) 开区间,收到 {tolerance!r}"
        )
    if (
        isinstance(delta, bool)
        or not isinstance(delta, (int, float))
        or not (0.0 < float(delta) < 0.5)
    ):
        raise ValueError(
            f"扰动幅度 delta 必须严格落在 (0, 0.5) 开区间,收到 {delta!r}"
            "——过小无信息量,过大则任何常量对都显得脆弱"
        )

    with telemetry.timer("guard_calib.fit"):
        samples = _normalize_records(records, classify_fn)
        parser = guard_adapter.FAMILY_PARSERS[family]

        # 逐条解析(纯函数),verdict ∈ {unsafe, safe, unparseable}。
        rows: list[tuple[_Sample, str, dict[str, Any]]] = []
        for sample in samples:
            _, meta = parser(sample.text or "")
            rows.append((sample, str(meta.get("verdict", "unparseable")), meta))
        parseable = [row for row in rows if row[1] in ("unsafe", "safe")]
        unparseable = [row for row in rows if row[1] not in ("unsafe", "safe")]

        # 1. 混淆矩阵:Yes(unsafe)=预测正类 × 人工真值。
        confusion = {
            "tp": sum(1 for s, v, _ in parseable if v == "unsafe" and s.label == 1),
            "fp": sum(1 for s, v, _ in parseable if v == "unsafe" and s.label == 0),
            "tn": sum(1 for s, v, _ in parseable if v == "safe" and s.label == 0),
            "fn": sum(1 for s, v, _ in parseable if v == "safe" and s.label == 1),
        }

        # 2. 当前常量映射下的 Brier 分数(常量实时读取 guard_adapter,只读对账)。
        current_yes = float(guard_adapter.PROB_UNSAFE)
        current_no = float(guard_adapter.PROB_SAFE)
        brier_current: float | None = None
        if parseable:
            total = 0.0
            for sample, verdict, _ in parseable:
                mapped = current_yes if verdict == "unsafe" else current_no
                total += (mapped - sample.label) ** 2
            brier_current = _sig(total / len(parseable))

        # 3. 建议常量对:MLE + 拉普拉斯(该侧无判定样本时如实给 None)。
        suggested_yes = (
            (confusion["tp"] + 1) / (confusion["tp"] + confusion["fp"] + 2)
            if (confusion["tp"] + confusion["fp"]) > 0
            else None
        )
        suggested_no = (
            (confusion["fn"] + 1) / (confusion["fn"] + confusion["tn"] + 2)
            if (confusion["fn"] + confusion["tn"]) > 0
            else None
        )
        if suggested_yes is not None:
            suggested_yes = _sig(suggested_yes)
        if suggested_no is not None:
            suggested_no = _sig(suggested_no)

        # 中文告警:畸形输出 / 缺判定侧 / 建议与现值超容差。
        warnings: list[str] = []
        if unparseable:
            warnings.append(
                f"{len(unparseable)} 条守卫输出无法解析(未计入混淆矩阵与 Brier),"
                "上线前须排查提示词/解析器匹配度"
            )
        if suggested_yes is None:
            warnings.append(
                "样本中无 Yes(unsafe)判定:Yes→不安全概率的 MLE 无从估计,"
                "建议常量对该侧为 None,须补充含 Yes 判定的样本"
            )
        if suggested_no is None:
            warnings.append(
                "样本中无 No(safe)判定:No→不安全概率的 MLE 无从估计,"
                "建议常量对该侧为 None,须补充含 No 判定的样本"
            )
        if suggested_yes is not None and abs(suggested_yes - current_yes) > tolerance:
            warnings.append(
                f"建议与现值差异超容差({tolerance}):Yes→不安全概率 "
                f"建议 {suggested_yes:.4f} vs 现值 {current_yes:.4f},"
                "请人工复核样本后再决定是否采纳(采纳是人工决策)"
            )
        if suggested_no is not None and abs(suggested_no - current_no) > tolerance:
            warnings.append(
                f"建议与现值差异超容差({tolerance}):No→不安全概率 "
                f"建议 {suggested_no:.4f} vs 现值 {current_no:.4f},"
                "请人工复核样本后再决定是否采纳(采纳是人工决策)"
            )

        # 4. 样本量充分性(CP 风格降级,红线 38 统计诚实性)。
        n = len(samples)
        sufficient = n >= MIN_CALIBRATION_N
        if sufficient:
            sufficiency_note = (
                f"n={n} ≥ {MIN_CALIBRATION_N},达到最小核验协议要求"
                "(MLE 建议仍依赖标注与线上同分布,建议持续积累并定期重核)"
            )
        else:
            telemetry.inc("guard_calib.degraded")
            sufficiency_note = (
                f"样本不足,仅演示:n={n} < {MIN_CALIBRATION_N},"
                "小样本上 MLE 波动过大,建议常量对不构成采纳依据"
                f"(CP 风格降级,与 conformal.MIN_CALIBRATION_N={MIN_CALIBRATION_N} 对齐)"
            )

        # 6. 阈值敏感性:当前对与建议对各做一次画像。
        verdicts = [verdict for _, verdict, _ in parseable]
        sensitivity: dict[str, Any] = {
            "delta": float(delta),
            "current": _pair_sensitivity(verdicts, current_yes, current_no, delta),
            "suggested": (
                _pair_sensitivity(verdicts, suggested_yes, suggested_no, delta)
                if suggested_yes is not None and suggested_no is not None
                else None
            ),
        }

        # 5. llamaguard 变体:类别码支持度统计(其余族为 None)。
        category_stats = _category_stats(rows) if family == "llamaguard" else None

        return CalibrationReport(
            family=family,
            n=n,
            n_parseable=len(parseable),
            n_unparseable=len(unparseable),
            confusion=confusion,
            unparseable_paths=[sample.path for sample, _, _ in unparseable],
            current_pair=(current_yes, current_no),
            suggested_pair=(suggested_yes, suggested_no),
            brier_current=brier_current,
            warnings=warnings,
            sufficient=sufficient,
            sufficiency_note=sufficiency_note,
            sensitivity=sensitivity,
            category_stats=category_stats,
            tolerance=float(tolerance),
            delta=float(delta),
        )


def load_records_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """从 JSONL 文件读入标注记录(每行一个 JSON 对象,空行跳过)。

    文件不可读 / 行非法 JSON / 行非对象 / 全空时抛中文 ``ValueError``
    (CLI 捕获后返回退出码 2)。**只读现场文件,绝不联网**。
    """
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"无法读取标注记录文件 {path}: {exc}") from None
    records: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{path} 第 {lineno} 行不是合法 JSON:{exc}"
            ) from None
        if not isinstance(obj, dict):
            raise ValueError(
                f"{path} 第 {lineno} 行应为 JSON 对象"
                f"(path/label/guard_raw),收到 {type(obj).__name__}"
            )
        records.append(obj)
    if not records:
        raise ValueError(f"{path} 未包含任何标注记录(空文件或全为空行)")
    return records


# ---------------------------------------------------------------------------
# CLI:python -m netsentinel.vision.guard_calib <records.jsonl> [--family ...]
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器(返回码约定:0 成功 / 2 用法与数据错误)。"""
    parser = argparse.ArgumentParser(
        prog="python -m netsentinel.vision.guard_calib",
        description=(
            "守卫模型标定核验工具:用人工标注样本核验 Yes/No→常数概率映射"
            "(0.95/0.05)是否需要调整;输出混淆矩阵、Brier 分数、MLE 建议"
            "常量对与阈值敏感性,仅为标定建议,采纳是人工决策"
        ),
    )
    parser.add_argument(
        "records",
        help="标注记录 JSONL 文件:每行 {path, label(0|1), guard_raw}",
    )
    parser.add_argument(
        "--family",
        default="shieldgemma2",
        choices=list(guard_adapter.GUARD_FAMILIES),
        help="守卫模型族(默认 shieldgemma2;llamaguard 额外输出类别支持度)",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        help=f"建议与现值差异的告警容差(默认 {DEFAULT_TOLERANCE},开区间 (0,1))",
    )
    parser.add_argument(
        "--delta",
        type=float,
        default=SENSITIVITY_DELTA,
        help=f"阈值敏感性扰动幅度(默认 {SENSITIVITY_DELTA},开区间 (0,0.5))",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口:读入标注 JSONL → :func:`calibrate` → 打印中文报告。

    CLI 模式无 classify_fn(绝不联网推理),每条记录必须自带 guard_raw
    历史值;现场产生模式请走 Python API 注入 ``classify_fn``。

    返回码:0 成功;2 文件/记录/参数错误(ValueError,中文消息)。
    """
    args = _build_parser().parse_args(argv)
    try:
        records = load_records_jsonl(args.records)
        report = calibrate(
            records,
            family=args.family,
            tolerance=args.tolerance,
            delta=args.delta,
        )
    except ValueError as exc:
        print(f"错误:{exc}")
        return 2
    print(report.render())
    return 0


if __name__ == "__main__":  # python -m netsentinel.vision.guard_calib ...
    raise SystemExit(main())
