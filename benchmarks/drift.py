# -*- coding: utf-8 -*-
"""NetSentinel 语料/分数分布漂移哨兵(A206)。

**背景与对标**:静态基准一旦固定,分数就只剩"点估计"——V10.4 对抗基准
报告自认"不可外推真实分布",而 2026 评测方法论的主轴正是**动态基准 +
漂移检测**:语料在演化、模型在迭代、线上分布随季节/风控对抗漂移,基准
分数的**分布形状**本身成为需要持续监测的对象。本模块把
``benchmarks/out/`` 下各 ``*_report.json`` 的历史分数分布沉淀为
JSONL 快照序列,用信贷风控行业标准指标 **PSI**(Population Stability
Index,闭式对称)与**标签条件 KL 散度**(nat,非对称)逐窗对照,
输出中文分级报告;退出码 0/2 可直接接入门禁(CI 上基准分数分布
漂移超阈即拦)。**全程离线、纯 stdlib、零第三方依赖。**

能力四件套:

- **记录器** :func:`append_drift_snapshot`——从单个报告文件或整个
  ``benchmarks/out/`` 目录提取分数分布快照
  ``{ts, source, n, histogram(10 桶 [0,1]), label_conditional?}``,
  以**单次 write 的整行追加**写进 JSONL(行级原子,单写者约定);
- **分析器** :func:`compute_psi` / :func:`conditional_kl`——桶概率
  ε 平滑防除零,恒等分布严格为 0;
- **不确定度** :func:`bootstrap_ci`——PSI/KL 的 percentile bootstrap
  95% CI(多项重采样当前窗计数,固定种子逐位确定):点估计之外给出
  "该窗口样本量下抽样噪声能把指标推多远"的诚实区间;
- **评估器** :func:`evaluate`——当前窗口 vs 参考窗口逐指标对照,
  阈值分级 PSI>0.1 注意 / >0.2 告警 / >0.3 严重(行业惯例),
  告警及以上退出码 2。

**窗口语义**(滑动对照,渐变漂移下 ``prev`` 更早可评):

- 当前窗口 = 最近 ``window`` 条快照聚合(直方图按计数池化);
- ``ref="first"``:参考窗口 = 历史开头的完整 ``window`` 条(锚定
  开测基线,需历史 ≥ 2×window——基线窗不完整则对账无意义,报错);
- ``ref="prev"``:参考窗口 = 紧邻当前窗口之前的
  ``min(window, 剩余)`` 条(滚动健康检查,历史 ≥ window+1 即可开评,
  参考窗不足 ``window`` 条时给出功效提示)。滚动模式对渐变漂移
  (温水煮蛙)**检出早于**锚定模式:它不必等首窗基线凑满,且不受
  "长程历史与当前天然缓慢分化"(基线老化)的干扰。

**指标方向语义**:PSI = Σ(cᵢ−rᵢ)ln(cᵢ/rᵢ) 逐项对称,交换参考/当前
结果不变;KL **非对称**——本模块统一取 **KL(ref‖cur)** = Σ rᵢ ln(rᵢ/cᵢ),
即"以参考分布为期望":当前窗口在参考常见桶上的缺失(cur→0)贡献大,
对"漏掉既有模式"(漏检方向)敏感,反向则对"新出现模式"敏感,解读时
务必带上方向。

**bootstrap 95% CI(V13 评测增强批;信息列,不改门禁语义)**:点估计
PSI 只剩一个数,而它带着抽样噪声——对当前窗计数做**多项重采样**
(按桶概率有放回抽 n 次,``random.Random(固定种子)`` 确定性,
B 默认 2000 可配),对每个重样本重算指标,取 2.5%/97.5% 分位得
``(ci_lo, ci_hi)``。三条解读纪律:

- **CI 为信息列**:分级与退出码**只按点值 PSI** 判定,CI 越线不改变
  退出码——CI 回答"噪声能推多远",门禁回答"点值是否已越线";
- 恒等分布的点值 PSI 严格为 0,但小窗 CI 上界可远超告警线(10 桶下
  n=30 的纯噪声期望 PSI ≈ (桶数−1)/n = 0.3)——这不是误报,而是
  "该样本量下 PSI 检验力不足"的诚实表白,窗口越小 CI 越宽;
- 参考窗视为固定(重采样只扰动当前窗):CI 刻画的是"若当前分布真是
  经验分布,重复采样会看到多大波动",而非两窗联合不确定度。

**KL 双侧重采样(V13-2 评测增强批;默认关闭,单侧现状零变动)**:
``bootstrap_ci(metric="kl", resample="both")`` 在每批 B 次重采样中把
参考窗与当前窗**各自独立多项重采样、同批配对**计算 KL(ref*‖cur*)——
与单侧(参考窗视为固定的 plug-in)的差异:单侧 CI 只刻画当前窗抽样
噪声,参考窗自身的有限样本估计误差被忽略;双侧把"参考窗也只是基线的
一次有限采样"计入,replicate 内两个重样本的**联合**波动(含偶然贴近/
偶然背离)构成 CI,因此区间不窄于同种子单侧、参考窗越小加宽越明显,
适合 ``ref="prev"`` 短窗的诚实解读。``evaluate`` / CLI 以
``--kl-resample current|both`` 暴露(默认 current);仅整体 KL 切换,
逐标签与条件 KL 维持单侧。

命令行(可 ``python -m benchmarks.drift``)::

    python -m benchmarks.drift record benchmarks/out --history data/drift_history.jsonl
    python -m benchmarks.drift record benchmarks/out/adversarial_report.json
    python -m benchmarks.drift evaluate --window 20 --ref first
    python -m benchmarks.drift evaluate --window 10 --ref prev --bootstrap-b 2000
    python -m benchmarks.drift evaluate --window 10 --kl-resample both

退出码:0 正常(含"注意"级,只提示不拦截);2 漂移告警及以上
(可作门禁),或流程可预期错误(中文提示到 stderr)。

安全红线:本模块对 ``benchmarks/out/`` 与任何报告**只读**;唯一写入点
是调用方显式指定的 history JSONL;不触网、不引第三方库;分数只是
基准分布特征,最终判定与举报须人工确认。
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: 直方图固定桶数(任务口径:10 桶覆盖 [0,1])。
HIST_BINS = 10

#: 桶概率平滑下限:防 ln(0)/除零;平滑后重归一,保持概率和为 1。
SMOOTH_EPS = 1e-6

#: PSI 分级阈值(信贷风控行业惯例):注意 / 告警 / 严重。
PSI_ATTENTION = 0.1
PSI_ALERT = 0.2
PSI_SEVERE = 0.3

#: 当前窗口聚合样本低于该值时,报告中附"抽样噪声大"提示。
MIN_POWER_N = 50

# --- bootstrap 95% CI 参数(V13 评测增强批;全部确定性:固定种子) ---------

#: percentile bootstrap 重采样次数(默认;evaluate/CLI 可配,0 = 跳过 CI)。
BOOTSTRAP_B = 2000

#: bootstrap 全局种子(2026 · A206→V13;按指标槽位派生子种子,见
#: :func:`_ci_seed`)。固定种子的意义:同一份窗口计数,任何机器、任何次
#: 运行得到逐位一致的 CI——评测报告可复现。
BOOTSTRAP_SEED = 20260206

#: CI 置信水平(2.5% / 97.5% 分位,与对抗基准 V9 的 percentile 口径一致)。
CI_LEVEL = 0.95

#: 当前窗样本低于该值时,报告中明确提示"CI 因抽样噪声显著加宽"
#: (10 桶下 n<30 时纯噪声的期望 PSI ≈ (桶数−1)/n 已达 0.3 量级)。
CI_MIN_HONEST_N = 30


class DriftError(Exception):
    """漂移哨兵的可预期错误(中文消息,CLI 层转退出码 2)。"""


# ---------------------------------------------------------------------------
# (a) 记录器:报告 → 分数分布快照
# ---------------------------------------------------------------------------

def histogram(scores: list[float], bins: int = HIST_BINS) -> list[int]:
    """把 [0,1] 内的分数列表数成等宽桶直方图(第 i 桶 = [i/bins,(i+1)/bins))。

    - ``1.0`` 恒落入最后一桶;±1e-9 内的浮点越界钳回边界;
      显著越界(如把延迟毫秒值当分数传入)直接 :class:`DriftError`,
      宁可失败也不静默产出无意义直方图;
    - 空列表返回全零直方图(由上层决定是否报错)。
    """
    counts = [0] * bins
    for v in scores:
        x = float(v)
        if x < -1e-9 or x > 1.0 + 1e-9:
            raise DriftError(f"分数 {x!r} 超出 [0,1],无法入桶(检查报告字段语义)")
        if x < 0.0:
            x = 0.0
        elif x > 1.0:
            x = 1.0
        idx = min(int(x * bins), bins - 1)
        counts[idx] += 1
    return counts


def _is_number(value: Any) -> bool:
    """True 当且仅当 value 是可入桶的实数(bool 是 int 子类,须排除)。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def extract_scores(report: Any, source: str = "") -> tuple[list[tuple[str | None, float]], str]:
    """从已解析的报告 JSON 提取 ``(标签或 None, 分数)`` 列表。

    支持三种已知报告形态(按字段特征自动识别,顺序确定):

    - ``details`` 列表(主基准 / 对抗 / providers):每条依次取
      ``base_prob``(对抗报告:另加全部变体 ``variants.*.prob``,标签
      沿用原图 label)→ ``prob`` → ``nsfw_prob`` → ``score``;标签键
      优先级 ``label`` > ``family`` > None(providers 无内容标签,用
      厂商族作条件维度,观测"分家族的分数漂移");
    - ``kernels`` 列表(kernel_bench):仅纳入 [0,1] 内的 ``value``
      (越界的延迟/字节数指标跳过并计数,note 说明);
    - ``groups`` 列表(grouping_bench):取 ``majority_share`` 为分数,
      ``majority_label`` 为标签。

    返回 ``(pairs, note)``;note 为中文说明(空串 = 无异常),提取不到
    任何分数时 pairs 为空、note 给出原因(如 tier_report 只含并发档位)。
    """
    if not isinstance(report, dict):
        return [], f"{source or '报告'} 顶层不是 JSON 对象"
    pairs: list[tuple[str | None, float]] = []
    note = ""

    details = report.get("details")
    if isinstance(details, list):
        for entry in details:
            if not isinstance(entry, dict):
                continue
            label = entry.get("label")
            if not isinstance(label, str):  # 空/缺失时回退 family(providers)
                label = entry.get("family")
            if not isinstance(label, str):
                label = None
            if _is_number(entry.get("base_prob")):
                # 对抗报告:原始分 + 全部扰动变体分(变体沿用原图标签)。
                pairs.append((label, float(entry["base_prob"])))
                variants = entry.get("variants")
                if isinstance(variants, dict):
                    for vp in variants.values():
                        if isinstance(vp, dict) and _is_number(vp.get("prob")):
                            pairs.append((label, float(vp["prob"])))
            elif _is_number(entry.get("prob")):
                pairs.append((label, float(entry["prob"])))
            elif _is_number(entry.get("nsfw_prob")):
                pairs.append((label, float(entry["nsfw_prob"])))
            elif _is_number(entry.get("score")):
                pairs.append((label, float(entry["score"])))
        if not pairs:
            note = "details 列表中未找到 prob/base_prob/nsfw_prob/score 分数字段"
    else:
        kernels = report.get("kernels")
        groups = report.get("groups")
        if isinstance(kernels, list):
            dropped = 0
            for entry in kernels:
                if isinstance(entry, dict) and _is_number(entry.get("value")):
                    v = float(entry["value"])
                    if 0.0 <= v <= 1.0:
                        pairs.append((entry.get("name") if isinstance(entry.get("name"), str) else None, v))
                    else:
                        dropped += 1
            if dropped:
                note = f"kernel 报告含 {dropped} 个超出 [0,1] 的指标值(延迟/字节量),已跳过"
        elif isinstance(groups, list):
            for entry in groups:
                if isinstance(entry, dict) and _is_number(entry.get("majority_share")):
                    share = float(entry["majority_share"])
                    label = entry.get("majority_label") if isinstance(entry.get("majority_label"), str) else None
                    if 0.0 <= share <= 1.0:
                        pairs.append((label, share))
            if not pairs:
                note = "groups 列表中未找到 [0,1] 内的 majority_share"
        else:
            note = "未识别的报告结构(无 details/kernels/groups,如 tier_report 只含并发档位)"
    return pairs, note


def build_snapshot(report: Any, source: str, *, now: datetime | None = None) -> dict:
    """把报告 JSON 编译为一条快照 ``{ts, source, n, histogram, label_conditional?}``。

    - ``ts``:本地时区 ISO 秒级时间戳(``now`` 可注入,测试确定性);
    - ``label_conditional``:有标签样本按标签分组各自的 10 桶直方图
      (键排序,序列化确定);无任何标签时省略该键;
    - 提取不到分数 → :class:`DriftError`(绝不写入空快照污染历史)。
    """
    pairs, note = extract_scores(report, source)
    if not pairs:
        raise DriftError(f"报告 {source or '<未知>'} 未提取到分数分布:{note or '无分数字段'}")
    ts = now or datetime.now(timezone.utc).astimezone()
    snap: dict[str, Any] = {
        "ts": ts.isoformat(timespec="seconds"),
        "source": source,
        "n": len(pairs),
        "histogram": histogram([s for _, s in pairs]),
    }
    by_label: dict[str, list[float]] = {}
    for label, score in pairs:
        if label is not None:
            by_label.setdefault(label, []).append(score)
    if by_label:
        snap["label_conditional"] = {
            label: histogram(by_label[label]) for label in sorted(by_label)
        }
    return snap


def snapshot_from_path(report_path: Path, *, now: datetime | None = None) -> dict:
    """读取单个报告文件并编译快照(``now`` 可注入,测试确定性)。"""
    path = Path(report_path)
    if not path.is_file():
        raise DriftError(f"报告不存在或不是文件:{path}")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DriftError(f"报告读取/解析失败:{path}({exc})") from exc
    return build_snapshot(report, path.name, now=now)


def append_drift_snapshot(
    source: str | Path,
    history_path: str | Path = "data/drift_history.jsonl",
    *,
    now: datetime | None = None,
) -> list[dict]:
    """把 ``source``(单个报告文件或 out 目录)的快照追加进 history JSONL。

    - 目录模式:按文件名序处理 ``report.json`` 与全部 ``*_report.json``;
      不含分数分布的报告(如 tier_report)静默跳过,不打断整批记录;
    - **原子行写**:每条快照序列化为一行后**单次 write** 追加
      (append 模式 + 一行一写,与 telemetry.export_jsonl 同款约定,
      单写者场景下行级原子,不会写出半行);目录不存在自动创建;
    - 返回本次实际追加的快照列表(目录模式下长度 ≤ 报告数)。
    """
    src = Path(source)
    if src.is_dir():
        # 主基准报告叫 report.json(无 _report 前缀),其余为 *_report.json。
        report_paths = sorted(
            {p for pat in ("report.json", "*_report.json") for p in src.glob(pat)}
        )
        if not report_paths:
            raise DriftError(f"目录 {src} 下没有 report.json / *_report.json 报告")
    else:
        report_paths = [src]
    snapshots: list[dict] = []
    for path in report_paths:
        try:
            snapshots.append(snapshot_from_path(path, now=now))
        except DriftError:
            if not src.is_dir():
                raise  # 单文件模式:显式指定的报告提不到分数 = 错误
            continue  # 目录模式:跳过不含分数分布的报告
    if not snapshots:
        raise DriftError(f"{src} 下没有任何报告可提取分数分布")

    target = Path(history_path)
    if str(target.parent) not in ("", "."):
        target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        for snap in snapshots:  # 一行一写:任何时刻中断都不会留半行
            fh.write(json.dumps(snap, ensure_ascii=False, sort_keys=False) + "\n")
    return snapshots


def load_history(history_path: str | Path) -> list[dict]:
    """读回 history JSONL 为快照列表,逐行校验结构(坏行 → 带行号的 DriftError)。

    校验口径:必有 ``ts/source/n/histogram``;``histogram`` 为 10 个
    非负整数且计数和 == ``n``;``label_conditional`` 可选,各标签直方图
    计数和须 ≤ ``n``(允许部分样本无标签)。
    """
    path = Path(history_path)
    if not path.is_file():
        raise DriftError(f"历史文件不存在:{path}(先运行 record 生成)")
    snaps: list[dict] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            snap = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DriftError(f"历史文件第 {lineno} 行不是合法 JSON:{exc}") from exc
        if not isinstance(snap, dict):
            raise DriftError(f"历史文件第 {lineno} 行不是 JSON 对象")
        missing = [k for k in ("ts", "source", "n", "histogram") if k not in snap]
        if missing:
            raise DriftError(f"历史文件第 {lineno} 行缺字段:{','.join(missing)}")
        h = snap["histogram"]
        if (
            not isinstance(h, list)
            or len(h) != HIST_BINS
            or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in h)
        ):
            raise DriftError(
                f"历史文件第 {lineno} 行 histogram 须为 {HIST_BINS} 个非负整数"
            )
        if sum(h) != snap["n"]:
            raise DriftError(
                f"历史文件第 {lineno} 行 histogram 计数和 {sum(h)} != n {snap['n']}"
            )
        cond = snap.get("label_conditional")
        if cond is not None:
            if not isinstance(cond, dict):
                raise DriftError(f"历史文件第 {lineno} 行 label_conditional 须为对象")
            for label, lh in cond.items():
                if (
                    not isinstance(lh, list)
                    or len(lh) != HIST_BINS
                    or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in lh)
                    or sum(lh) > snap["n"]
                ):
                    raise DriftError(
                        f"历史文件第 {lineno} 行标签 {label!r} 的直方图非法"
                        "(须为 10 个非负整数且计数和 ≤ n)"
                    )
        snaps.append(snap)
    if not snaps:
        raise DriftError(f"历史文件为空:{path}")
    return snaps


# ---------------------------------------------------------------------------
# (b) 分析器:PSI / 条件 KL(闭式,ε 平滑)
# ---------------------------------------------------------------------------

def _smooth_probs(hist: list[float], eps: float = SMOOTH_EPS) -> list[float]:
    """把计数(或权重)直方图变成 ε 平滑后的概率分布(和恒为 1)。

    步骤:归一化 → 概率下限钳到 ε → 重归一。全零直方图无分布可言,
    抛 :class:`DriftError` 而非伪造均匀分布。
    """
    total = sum(hist)
    if total <= 0:
        raise DriftError("直方图全为零(无样本),无法构成分布——先积累快照再对照")
    probs = [max(float(v) / total, eps) for v in hist]
    z = sum(probs)
    return [p / z for p in probs]


def compute_psi(ref_hist: list[float], cur_hist: list[float], eps: float = SMOOTH_EPS) -> float:
    """Population Stability Index:Σᵢ (cᵢ−rᵢ)·ln(cᵢ/rᵢ)(闭式,ε 平滑)。

    - 输入为计数或权重直方图(桶数一致,≥1 桶),内部平滑后计算;
    - **对称性**:逐项 (cᵢ−rᵢ)ln(cᵢ/rᵢ) 在交换 ref/cur 后不变,故
      PSI(a,b) == PSI(b,a)——它是"两个分布的分歧度",无方向;
    - 恒等分布恒为 0;空桶由 ε 平滑(下限后重归一)保证有限;
    - 行业分级:>0.1 注意 / >0.2 告警 / >0.3 严重。
    """
    if len(ref_hist) != len(cur_hist) or not ref_hist:
        raise DriftError(f"直方图桶数不一致或为空:{len(ref_hist)} vs {len(cur_hist)}")
    r = _smooth_probs(ref_hist, eps)
    c = _smooth_probs(cur_hist, eps)
    value = sum((ci - ri) * math.log(ci / ri) for ri, ci in zip(r, c))
    return max(value, 0.0)  # 恒等分布浮点残差钳回严格 0


def kl_divergence(ref_hist: list[float], cur_hist: list[float], eps: float = SMOOTH_EPS) -> float:
    """KL(ref‖cur) = Σᵢ rᵢ·ln(rᵢ/cᵢ),单位 **nat**(ε 平滑)。

    **方向语义(非对称,务必带方向解读)**:本函数以 ref 为"真实/期望"
    分布——参考窗常见而当前窗缺失的桶(curᵢ→0,ε 兜底)贡献大,即对
    **"当前漏掉参考既有模式"(漏检方向)** 敏感;KL(a‖b) ≠ KL(b‖a),
    反向 KL 对"当前新出现的模式"敏感。恒等分布 = 0,恒非负。
    """
    if len(ref_hist) != len(cur_hist) or not ref_hist:
        raise DriftError(f"直方图桶数不一致或为空:{len(ref_hist)} vs {len(cur_hist)}")
    r = _smooth_probs(ref_hist, eps)
    c = _smooth_probs(cur_hist, eps)
    value = sum(ri * math.log(ri / ci) for ri, ci in zip(r, c))
    return max(value, 0.0)


def conditional_kl(
    ref_labels: dict[str, list[float]],
    cur_labels: dict[str, list[float]],
    eps: float = SMOOTH_EPS,
) -> float:
    """标签条件 KL(ref‖cur, nat):各共同标签的 KL 按**参考窗标签质量**加权。

    - 权重 wₗ = 参考窗中标签 l 的样本数 / 共同标签样本总数(参考侧
      越常见的标签,其条件漂移越主导混合分布的分歧);
    - 只在**共同标签**上计算并重归一权重:参考缺失的标签无"参考侧
      条件分布"可言;标签集增/减由调用方(compare_windows)作为独立
      证据单列,不塞进这一个标量里;
    - 无任何共同标签 → :class:`DriftError`(标签集完全失配,条件 KL
      无定义,应按"标签集变化"证据解读而非数值)。
    """
    common = sorted(set(ref_labels) & set(cur_labels))
    if not common:
        raise DriftError("参考与当前无共同标签,条件 KL 无定义(解读标签集变化)")
    weights = {label: sum(ref_labels[label]) for label in common}
    total = sum(weights.values())
    if total <= 0:
        raise DriftError("参考窗共同标签样本数为零,条件 KL 无定义")
    return sum(
        (weights[label] / total) * kl_divergence(ref_labels[label], cur_labels[label], eps)
        for label in common
    )


# ---------------------------------------------------------------------------
# (b2) bootstrap 95% CI:多项重采样当前窗计数(固定种子,逐位确定)
# ---------------------------------------------------------------------------

def _ci_seed(slot: int) -> int:
    """按指标槽位派生确定性子种子(与调用顺序无关,同槽位恒同种子)。

    槽位分配(见 :func:`_bootstrap_ci_section`):0 = 整体 PSI+KL(共享同
    一批重采样),1..k = 排序后第 i 个标签的 PSI+KL,末槽 = 条件 KL。
    不同指标互不干扰:任一指标的种子不随其它指标增删而漂移。
    """
    return (BOOTSTRAP_SEED + 1_000_003 * slot) % (2**31)


def _multinomial_resample(
    rng: random.Random, probs: list[float], n: int
) -> list[int]:
    """按 ``probs`` 多项抽样 n 次,返回各桶重采样计数(纯 stdlib)。

    实现:累积权重 + ``bisect`` 定位,每次抽取 = 一次 ``rng.random()`` +
    一次二分(累积权重只构建一次,B 次重采样复用);概率为 0 的桶不可能
    被抽中(经验分布里没有的质量不会被重采样造出来)。浮点边界
    (``u`` 因舍入恰好等于累积和)钳回最后一桶,保证索引恒有效。
    """
    cum: list[float] = []
    acc = 0.0
    for p in probs:
        acc += p
        cum.append(acc)
    counts = [0] * len(probs)
    draw = rng.random
    locate = bisect.bisect_left
    for _ in range(n):
        u = draw() * acc
        idx = locate(cum, u)
        if idx >= len(counts):  # 浮点舍入防护:u == acc 时钳回最后一桶
            idx = len(counts) - 1
        counts[idx] += 1
    return counts


def _validate_bootstrap_b(b: int) -> None:
    """校验重采样次数:正整数(0 = 显式关闭 CI,由调用方先行分流)。"""
    if not isinstance(b, int) or isinstance(b, bool) or b < 1:
        raise DriftError(f"bootstrap 重采样次数 B 须为正整数,收到 {b!r}(0 = 关闭 CI)")


def _percentile_ci95(sorted_values: list[float]) -> tuple[float, float]:
    """已升序排序的重样本指标值 → (2.5%, 97.5%) 分位(最近秩法,
    序号 ⌊q·(B−1)⌋,与对抗基准 V9 的 percentile bootstrap 口径一致)。"""
    b = len(sorted_values)
    return (
        sorted_values[int(0.025 * (b - 1))],
        sorted_values[int(0.975 * (b - 1))],
    )


def _bootstrap_psi_kl_replicates(
    ref_hist: list[float], cur_hist: list[float], *, b: int, seed: int
) -> tuple[list[float], list[float]]:
    """对当前窗计数做 B 次多项重采样,逐个重算 (PSI, KL)——共享同一批
    重采样序列(参考窗固定),返回两个**未排序**的指标值列表。

    内部函数:PSI 与 KL 用同一批重样本计算,采样成本减半且两者的 CI
    刻画同一随机源;排序由调用方按需进行。
    """
    n = sum(cur_hist)
    if n <= 0:
        raise DriftError("当前窗计数全为零,无法重采样(先积累快照再对照)")
    probs = [float(v) / n for v in cur_hist]
    rng = random.Random(seed)
    psi_values: list[float] = []
    kl_values: list[float] = []
    for _ in range(b):
        resampled = _multinomial_resample(rng, probs, n)
        psi_values.append(compute_psi(ref_hist, resampled))
        kl_values.append(kl_divergence(ref_hist, resampled))
    return psi_values, kl_values


def _bootstrap_kl_paired_replicates(
    ref_hist: list[float], cur_hist: list[float], *, b: int, seed: int
) -> list[float]:
    """**双侧重采样**:每批同时独立重采样参考窗与当前窗,配对计算
    KL(ref*‖cur*),返回 B 个**未排序**的指标值。

    与单侧(:func:`_bootstrap_psi_kl_replicates`,参考窗固定)的差异:
    参考窗也只是基线分布的一次有限采样——本函数把参考侧的抽样噪声计入
    CI。每个 replicate 内先抽参考重样本、再抽当前重样本(同一 RNG 流,
    顺序固定 → 逐位确定),两个重样本**配对**进同一次 KL 计算:replicate
    分布捕获两窗估计的联合波动(含偶然贴近 → 低值、偶然背离 → 高值),
    而非两个独立 CI 的外积。参考窗计数全为零 → :class:`DriftError`。
    """
    n_ref = sum(ref_hist)
    n_cur = sum(cur_hist)
    if n_ref <= 0:
        raise DriftError("参考窗计数全为零,无法双侧重采样(先积累快照再对照)")
    if n_cur <= 0:
        raise DriftError("当前窗计数全为零,无法重采样(先积累快照再对照)")
    ref_probs = [float(v) / n_ref for v in ref_hist]
    cur_probs = [float(v) / n_cur for v in cur_hist]
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(b):
        # 同批配对:先参考后当前,顺序固定(确定性),两侧独立抽样。
        ref_resampled = _multinomial_resample(rng, ref_probs, n_ref)
        cur_resampled = _multinomial_resample(rng, cur_probs, n_cur)
        values.append(kl_divergence(ref_resampled, cur_resampled))
    return values


def bootstrap_ci(
    ref_hist: list[float],
    cur_hist: list[float],
    *,
    metric: str = "psi",
    b: int = BOOTSTRAP_B,
    seed: int = BOOTSTRAP_SEED,
    resample: str = "current",
) -> tuple[float, float]:
    """PSI / KL 的 percentile bootstrap 95% CI(多项重采样当前窗计数)。

    单侧口径(默认 ``resample="current"``):当前窗计数视为经验分布 →
    按桶概率**多项有放回重采样** n 次得重样本直方图 → 对每个重样本重算
    指标(ref 固定)→ 取 2.5%/97.5% 分位。这刻画的是"**若当前分布真是
    经验分布,重复采样会把指标推多远**"——即当前窗口样本量下的抽样噪声
    幅度,参考窗不确定度不计入。

    双侧口径(``resample="both"``,仅 ``metric="kl"``):每批把参考窗与
    当前窗**各自独立**多项重采样后**同批配对**计算 KL(ref*‖cur*)。
    配对差分语义:KL 是两分布的散度泛函,replicate 内的一对重样本让两侧
    抽样噪声进入同一次计算——CI 刻画两窗联合抽样波动,含"参考窗自身
    也是有限采样"这部分被单侧忽略的不确定度。由此:同数据同种子下双侧
    与单侧是**不同的区间**(双侧通常更宽,参考窗越小越明显);点值不受
    影响,门禁仍只按点值判定。单侧退化情形(当前窗质量集中单桶 → 任何
    当前重采样都还原原计数 → 单侧 CI 塌缩为点值)在双侧下不再塌缩——
    参考重样本仍在波动。

    - ``random.Random(seed)`` 固定种子 → 完全确定性,同数据同种子必得
      同区间(评测报告逐位可复现);双侧模式同一 RNG 流按
      "先参考后当前"的固定顺序驱动两次重采样;
    - 恒等分布:点值 PSI 严格为 0,但重样本几乎不可能与参考逐桶相等,
      故 CI 上界 > 0 且随 n 收窄(≈ (桶数−1)/n 量级)——这是**检验力
      的诚实表白**,不是漂移误报;
    - 当前窗全部质量集中在单桶(或 n=1)时,任何重采样都与原计数相同
      → 单侧 CI 退化为 (点值, 点值);
    - ``metric`` 只允许 ``"psi" | "kl"``;``resample`` 只允许
      ``"current" | "both"``,且 ``"both"`` 只对 ``metric="kl"`` 有意义
      (PSI 的双侧配对比对属另一口径,不在此暴露);``b`` 须为正整数;
      空输入 / 全零计数 → :class:`DriftError`(中文)。
    """
    if metric not in ("psi", "kl"):
        raise DriftError(f"未知指标 metric={metric!r}(只支持 psi|kl)")
    if resample not in ("current", "both"):
        raise DriftError(
            f"未知重采样模式 resample={resample!r}(只支持 current|both)"
        )
    if resample == "both" and metric != "kl":
        raise DriftError(
            f"双侧重采样(resample='both')只对 metric='kl' 有意义,收到 metric={metric!r}"
        )
    _validate_bootstrap_b(b)
    if resample == "both":
        values = sorted(
            _bootstrap_kl_paired_replicates(ref_hist, cur_hist, b=b, seed=seed)
        )
    else:
        psi_values, kl_values = _bootstrap_psi_kl_replicates(
            ref_hist, cur_hist, b=b, seed=seed
        )
        values = sorted(psi_values if metric == "psi" else kl_values)
    return _percentile_ci95(values)


def _conditional_kl_bootstrap_ci(
    ref_cond: dict[str, list[int]],
    cur_cond: dict[str, list[int]],
    *,
    b: int,
    seed: int,
) -> tuple[float, float] | None:
    """条件 KL 的 bootstrap CI:每次重采样**各标签**当前窗计数(独立、
    共用同一 RNG 流),按固定参考权重重算加权条件 KL。

    无共同标签时返回 None(与点值条件 KL 的"不可计算"口径一致)。
    """
    common = sorted(set(ref_cond) & set(cur_cond))
    if not common:
        return None
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(b):
        resampled: dict[str, list[int]] = {}
        for label in common:
            hist = cur_cond[label]
            total = sum(hist)
            if total <= 0:  # 防御:聚合层保证 ≥1,理论不可达
                raise DriftError(f"标签 {label!r} 当前窗计数为零,无法重采样")
            resampled[label] = _multinomial_resample(
                rng, [float(v) / total for v in hist], total
            )
        values.append(conditional_kl(ref_cond, resampled))
    return _percentile_ci95(sorted(values))


def _bootstrap_ci_section(
    ref_agg: dict[str, Any], cur_agg: dict[str, Any], *, b: int,
    kl_resample: str = "current",
) -> dict[str, Any]:
    """由窗口聚合结果构建 ``result["bootstrap_ci"]`` 段(compare_windows 用)。

    指标槽位(确定性,与标签集合增删的相互影响最小化):0 = 整体,
    1..k = 排序后第 i 个共同标签,末槽 = 条件 KL;整体与逐标签的
    PSI/KL 共享同一批重采样(PSI 与 KL 同随机源)。

    ``kl_resample="both"``(V13-2)时仅**整体 KL** 切换为双侧重采样
    (参考窗与当前窗独立重采样、同批配对,见
    :func:`_bootstrap_kl_paired_replicates`);PSI、逐标签 KL 与条件 KL
    维持单侧(分层指标保持既有口径,避免解释负担)。默认 ``current``
    时本段输出与历史版本**逐字节一致**。
    """
    if kl_resample not in ("current", "both"):
        raise DriftError(
            f"未知重采样模式 kl_resample={kl_resample!r}(只支持 current|both)"
        )
    common = sorted(set(ref_agg["label_conditional"]) & set(cur_agg["label_conditional"]))

    def _pair(slot: int, ref_hist: list[int], cur_hist: list[int]) -> tuple[
        list[float], list[float]
    ]:
        psi_values, kl_values = _bootstrap_psi_kl_replicates(
            ref_hist, cur_hist, b=b, seed=_ci_seed(slot)
        )
        return sorted(psi_values), sorted(kl_values)

    psi_sorted, kl_sorted = _pair(0, ref_agg["histogram"], cur_agg["histogram"])
    kl_ci = _percentile_ci95(kl_sorted)
    if kl_resample == "both":
        kl_ci = _percentile_ci95(
            sorted(
                _bootstrap_kl_paired_replicates(
                    ref_agg["histogram"], cur_agg["histogram"], b=b, seed=_ci_seed(0)
                )
            )
        )
    psi_by_label: dict[str, list[float]] = {}
    kl_by_label: dict[str, list[float]] = {}
    for i, label in enumerate(common):
        p_sorted, k_sorted = _pair(
            1 + i, ref_agg["label_conditional"][label], cur_agg["label_conditional"][label]
        )
        psi_by_label[label] = list(_percentile_ci95(p_sorted))
        kl_by_label[label] = list(_percentile_ci95(k_sorted))
    ckl_ci = _conditional_kl_bootstrap_ci(
        ref_agg["label_conditional"],
        cur_agg["label_conditional"],
        b=b,
        seed=_ci_seed(1 + len(common)),
    )
    method_text = (
        "percentile bootstrap 95% CI:多项重采样当前窗计数"
        f"(random.Random 固定种子 {BOOTSTRAP_SEED},B={b},逐位确定)"
    )
    if kl_resample == "both":
        method_text += ";整体 KL 为双侧重采样(参考窗与当前窗独立重采样,同批配对)"
    return {
        "b": b,
        "seed": BOOTSTRAP_SEED,
        "level": CI_LEVEL,
        "kl_resample": kl_resample,
        "method": method_text,
        "psi": list(_percentile_ci95(psi_sorted)),
        "kl": list(kl_ci),
        "psi_by_label": psi_by_label,
        "kl_by_label": kl_by_label,
        "conditional_kl": list(ckl_ci) if ckl_ci is not None else None,
        "advisory": (
            "CI 为信息列:分级与退出码只按点值 PSI 判定;"
            f"当前窗样本 n<{CI_MIN_HONEST_N} 时 CI 因抽样噪声显著加宽属正常表现"
        ),
    }


# ---------------------------------------------------------------------------
# (c) 评估器:窗口对照 + 中文分级报告
# ---------------------------------------------------------------------------

def _grade(psi: float) -> str:
    """PSI → 中文分级(行业惯例阈值)。"""
    if psi > PSI_SEVERE:
        return "严重"
    if psi > PSI_ALERT:
        return "告警"
    if psi > PSI_ATTENTION:
        return "注意"
    return "稳定"


def aggregate(snapshots: list[dict]) -> dict:
    """把一组快照的直方图按计数池化(丢弃原始分数后的正确聚合层级)。"""
    hist = [0] * HIST_BINS
    by_label: dict[str, list[int]] = {}
    for snap in snapshots:
        for i, v in enumerate(snap["histogram"]):
            hist[i] += v
        for label, lh in (snap.get("label_conditional") or {}).items():
            acc = by_label.setdefault(label, [0] * HIST_BINS)
            for i, v in enumerate(lh):
                acc[i] += v
    return {
        "n": sum(hist),
        "histogram": hist,
        "label_conditional": {k: by_label[k] for k in sorted(by_label)},
    }


def compare_windows(
    snapshots: list[dict],
    window: int,
    ref: str = "first",
    *,
    bootstrap_b: int | None = None,
    kl_resample: str = "current",
) -> dict:
    """窗口对照主逻辑(纯函数,同输入同输出):当前窗 vs 参考窗逐指标。

    窗口语义(详见模块 docstring):当前窗 = 最近 ``window`` 条;
    ``ref="first"`` 参考窗 = 开头完整 ``window`` 条(需 ≥ 2×window);
    ``ref="prev"`` 参考窗 = 紧邻当前窗之前的 min(window, 剩余) 条
    (需 ≥ window+1,滚动模式更早可评 → 渐变漂移检出更早)。

    返回指标字典:整体/逐标签 PSI 与 KL、参考质量加权的条件 KL、
    标签集增减、参考/当前窗区间与样本量、分级与退出码(PSI > 0.2
    即告警级 → exit_code=2;KL 无行业阈值,只报告值,门禁只看 PSI)。

    ``bootstrap_b``(默认 None = 不算 CI,既有行为与开销不变):传入
    正整数时对整体/逐标签 PSI·KL 与条件 KL 附 percentile bootstrap
    95% CI(多项重采样当前窗计数,固定种子),结果挂 ``result
    ["bootstrap_ci"]``,并在当前窗 n < :data:`CI_MIN_HONEST_N` 时附
    "CI 加宽"提示;0 = 显式关闭(与 None 等价,供 CLI 分流)。
    **CI 为信息列:分级与退出码仍只按点值 PSI 判定,不因 CI 越线改变。**

    ``kl_resample``(V13-2,默认 ``"current"`` = 单侧现状零变动):
    ``"both"`` 时整体 KL 的 CI 切换为**双侧重采样**(参考窗与当前窗独立
    重采样、同批配对,见 :func:`bootstrap_ci`);非法取值在计算 CI 时
    :class:`DriftError`(不计算 CI 时该参数无效果)。
    """
    if ref not in ("first", "prev"):
        raise DriftError(f"未知参考模式 ref={ref!r}(只支持 first|prev)")
    if not isinstance(window, int) or isinstance(window, bool) or window < 1:
        raise DriftError(f"window 须为正整数,收到 {window!r}")
    total = len(snapshots)
    if ref == "first" and total < 2 * window:
        raise DriftError(
            f"历史仅 {total} 条:ref='first' 需 ≥ 2×window={2 * window} 条"
            "(完整首窗基线 + 当前窗)"
        )
    if ref == "prev" and total < window + 1:
        raise DriftError(
            f"历史仅 {total} 条:ref='prev' 需 ≥ window+1={window + 1} 条"
            "(当前窗 + 至少 1 条滚动参考)"
        )

    cur_sl = snapshots[total - window:]
    if ref == "first":
        ref_sl = snapshots[:window]
        ref_desc = f"首窗基线 {len(ref_sl)} 条"
        ref_range = [0, len(ref_sl)]
    else:
        avail = min(window, total - window)
        ref_sl = snapshots[total - window - avail:total - window]
        ref_desc = f"紧邻滚动窗 {len(ref_sl)} 条"
        ref_range = [total - window - len(ref_sl), total - window]
    ref_agg = aggregate(ref_sl)
    cur_agg = aggregate(cur_sl)

    ref_cond = ref_agg["label_conditional"]
    cur_cond = cur_agg["label_conditional"]
    common = sorted(set(ref_cond) & set(cur_cond))
    new_labels = sorted(set(cur_cond) - set(ref_cond))
    gone_labels = sorted(set(ref_cond) - set(cur_cond))

    psi = compute_psi(ref_agg["histogram"], cur_agg["histogram"])
    kl = kl_divergence(ref_agg["histogram"], cur_agg["histogram"])
    psi_by_label = {
        label: compute_psi(ref_cond[label], cur_cond[label]) for label in common
    }
    kl_by_label = {
        label: kl_divergence(ref_cond[label], cur_cond[label]) for label in common
    }
    ckl = conditional_kl(ref_cond, cur_cond) if common else None

    max_psi = max([psi, *psi_by_label.values()])  # 整体 PSI 恒在,列表非空
    notes: list[str] = []
    if ref == "prev" and len(ref_sl) < window:
        notes.append(
            f"参考窗仅 {len(ref_sl)} 条(<window={window}):滚动对照可用,统计功效较低"
        )
    if cur_agg["n"] < MIN_POWER_N:
        notes.append(f"当前窗聚合样本 n={cur_agg['n']} < {MIN_POWER_N}:PSI 抽样噪声大,结论仅供参考")
    if bootstrap_b == 0:  # 0 = 显式关闭(与 None 等价)
        bootstrap_b = None
    if bootstrap_b is not None:
        _validate_bootstrap_b(bootstrap_b)
        if cur_agg["n"] < CI_MIN_HONEST_N:
            noise = float(HIST_BINS - 1) / max(cur_agg["n"], 1)
            notes.append(
                f"当前窗聚合样本 n={cur_agg['n']} < {CI_MIN_HONEST_N}:"
                f"bootstrap 95% CI 因抽样噪声显著加宽(纯噪声期望 PSI≈"
                f"{HIST_BINS - 1}/{cur_agg['n']}={noise:.3f}),"
                "请以 CI 而非点值解读;门禁退出码不受影响"
            )
    if new_labels:
        notes.append(f"当前窗出现参考窗没有的标签:{new_labels}")
    if gone_labels:
        notes.append(f"参考窗标签在当前窗消失:{gone_labels}")
    if ckl is None:
        notes.append("参考与当前无共同标签:条件 KL 不可计算(按标签集失配解读)")

    result = {
        "ref": ref,
        "window": window,
        "total": total,
        "ref_range": ref_range,
        "cur_range": [total - window, total],
        "ref_desc": ref_desc,
        "n_ref": ref_agg["n"],
        "n_cur": cur_agg["n"],
        "psi": psi,
        "kl": kl,
        "psi_by_label": psi_by_label,
        "kl_by_label": kl_by_label,
        "n_by_label": {label: [sum(ref_cond[label]), sum(cur_cond[label])] for label in common},
        "new_labels": new_labels,
        "gone_labels": gone_labels,
        "conditional_kl": ckl,
        "max_psi": max_psi,
        "level": _grade(max_psi),
        "exit_code": 2 if max_psi > PSI_ALERT else 0,
        "notes": notes,
    }
    # bootstrap 95% CI(信息列:不影响上面的 level/exit_code,纯附加)
    if bootstrap_b is not None:
        result["bootstrap_ci"] = _bootstrap_ci_section(
            ref_agg, cur_agg, b=bootstrap_b, kl_resample=kl_resample
        )
    return result


def render_report(result: dict) -> str:
    """把 :func:`compare_windows` 结果渲染为中文表格 + 结论(纯字符串)。

    ``result["bootstrap_ci"]`` 存在时(见 :func:`compare_windows` 的
    ``bootstrap_b``)总表与逐标签行追加 "PSI 95%CI / KL 95%CI" 两列,
    条件 KL 行填其 CI;无该段时输出与历史版本逐字节一致。
    """
    ci = result.get("bootstrap_ci")
    lines: list[str] = []
    lines.append("=" * 64)
    lines.append("NetSentinel 漂移哨兵 · 语料/分数分布漂移评估")
    lines.append("=" * 64)
    lines.append(
        f"历史 {result['total']} 条快照 · 当前窗=最近 {result['window']} 条"
        f"(#{result['cur_range'][0]}–#{result['cur_range'][1] - 1} 聚合)"
        f" · 参考={result['ref_desc']}"
        f"(#{result['ref_range'][0]}–#{result['ref_range'][1] - 1}) [ref={result['ref']}]"
    )
    lines.append("")
    header = f"{'指标':<12}{'参考n':>8}{'当前n':>8}{'PSI':>10}{'分级':>6}{'KL(nat)':>10}"
    if ci is not None:
        header += f"{'PSI 95%CI':>20}{'KL 95%CI':>20}"
    lines.append(header)
    lines.append("-" * len(header))
    ci_columns = ""
    if ci is not None:
        ci_columns = (
            f"[{ci['psi'][0]:.4f},{ci['psi'][1]:.4f}]".rjust(20)
            + f"[{ci['kl'][0]:.4f},{ci['kl'][1]:.4f}]".rjust(20)
        )
    lines.append(
        f"{'整体分布':<12}{result['n_ref']:>8}{result['n_cur']:>8}"
        f"{result['psi']:>10.4f}{_grade(result['psi']):>6}{result['kl']:>10.4f}"
        + ci_columns
    )
    for label, value in result["psi_by_label"].items():
        n_ref, n_cur = result["n_by_label"][label]
        label_ci = ""
        if ci is not None:
            p_ci, k_ci = ci["psi_by_label"][label], ci["kl_by_label"][label]
            label_ci = (
                f"[{p_ci[0]:.4f},{p_ci[1]:.4f}]".rjust(20)
                + f"[{k_ci[0]:.4f},{k_ci[1]:.4f}]".rjust(20)
            )
        lines.append(
            f"[{label}]".ljust(12)
            + f"{n_ref:>8}{n_cur:>8}{value:>10.4f}{_grade(value):>6}"
            + f"{result['kl_by_label'][label]:>10.4f}"
            + label_ci
        )
    ckl = result["conditional_kl"]
    ckl_text = f"{ckl:.4f}" if ckl is not None else "不可计算"
    ckl_ci = ""
    if ci is not None:
        ckl_ci = "—".rjust(20)
        if ci["conditional_kl"] is not None:
            lo, hi = ci["conditional_kl"]
            ckl_ci = f"[{lo:.4f},{hi:.4f}]".rjust(20)
    lines.append(
        f"{'条件KL加权':<12}{'':>8}{'':>8}{'—':>10}{'—':>6}{ckl_text:>10}"
        + ckl_ci
    )
    lines.append("")
    for note in result["notes"]:
        lines.append(f"提示:{note}")
    if result["notes"]:
        lines.append("")
    lines.append(
        f"阈值:PSI≤{PSI_ATTENTION} 稳定 · >{PSI_ATTENTION} 注意 · "
        f">{PSI_ALERT} 告警 · >{PSI_SEVERE} 严重(行业惯例;"
        "KL 无行业阈值只报告值,门禁只按 PSI)"
    )
    if ci is not None:
        lines.append(
            f"CI:{ci['method']}——CI 为信息列,分级与退出码只按点值 PSI 判定,"
            "窗口越小 CI 越宽属正常抽样噪声表现(不改变门禁语义)"
        )
    if result["exit_code"] == 2:
        lines.append(
            f"结论:检测到分数分布漂移 —— 最大 PSI={result['max_psi']:.4f}"
            f"({result['level']}级,>{PSI_ALERT} 告警线)→ 退出码 2"
        )
    elif result["level"] == "注意":
        lines.append(
            f"结论:轻度漂移(注意级,最大 PSI={result['max_psi']:.4f},"
            f"≤{PSI_ALERT} 告警线)→ 退出码 0,建议关注"
        )
    else:
        lines.append(
            f"结论:未检测到显著漂移(最大 PSI={result['max_psi']:.4f},"
            f"≤{PSI_ATTENTION} 注意线)→ 退出码 0"
        )
    return "\n".join(lines)


def evaluate(
    history_path: str | Path,
    *,
    window: int,
    ref: str = "first",
    bootstrap_b: int | None = BOOTSTRAP_B,
    kl_resample: str = "current",
) -> int:
    """评估入口:读历史 → 窗口对照(附 bootstrap CI)→ 打印中文报告 →
    返回 0/2(可作门禁)。

    返回码:0 = 正常或仅注意级;2 = 存在 PSI > 0.2(告警/严重级)的
    指标(整体或任一标签)。**CI 为信息列**:``bootstrap_b`` 默认
    :data:`BOOTSTRAP_B`(2000)时报告每指标附 95% CI,但分级与退出码
    仍只按点值 PSI 判定——CI 上界越线不拦截、CI 收窄不放行,门禁语义
    与无 CI 时代完全一致;``bootstrap_b=0`` 显式跳过 CI。对同一历史文件
    是纯函数:输出逐字节确定。

    ``kl_resample``(V13-2,默认 ``"current"`` = 单侧现状零变动):
    ``"both"`` 时整体 KL 的 CI 用双侧重采样(参考窗与当前窗独立重采样、
    同批配对;见 :func:`bootstrap_ci`),报告方法行附注说明——不影响
    点值、分级与退出码。
    """
    snapshots = load_history(history_path)
    effective_b = None if bootstrap_b == 0 else bootstrap_b
    result = compare_windows(
        snapshots, window, ref, bootstrap_b=effective_b, kl_resample=kl_resample
    )
    print(render_report(result))
    return result["exit_code"]


# ---------------------------------------------------------------------------
# (d) CLI
# ---------------------------------------------------------------------------

def _ensure_utf8_stdio() -> None:
    """Windows 控制台编码非 UTF-8 时切换标准流编码,避免中文输出报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if (
                stream is not None
                and stream.encoding
                and stream.encoding.lower() not in ("utf-8", "utf8")
            ):
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 重新配置失败不影响主流程
            pass


def main(argv: list[str] | None = None) -> int:
    """命令行入口:``python -m benchmarks.drift record|evaluate ...``。

    子命令::

        record <报告.json|目录> [--history data/drift_history.jsonl]
        evaluate [--history data/drift_history.jsonl] [--window 20] [--ref first|prev]
                 [--bootstrap-b 2000] [--kl-resample current|both]

    返回码:0 正常(record 至少记入 1 条快照 / evaluate 未越告警线);
    2 漂移告警及以上,或流程可预期错误(中文提示到 stderr)。
    """
    _ensure_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.drift",
        description=(
            "NetSentinel 语料/分数分布漂移哨兵(A206):记录基准报告的"
            "分数分布快照,PSI/条件 KL 逐窗对照,中文分级报告,"
            "退出码可作 CI 门禁(全程离线,纯 stdlib)"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_record = sub.add_parser("record", help="从报告文件/目录提取快照并追加 JSONL")
    p_record.add_argument("source", help="报告文件或目录(目录则处理 report.json 与全部 *_report.json)")
    p_record.add_argument(
        "--history", default="data/drift_history.jsonl", help="快照历史 JSONL 路径"
    )

    p_eval = sub.add_parser("evaluate", help="窗口对照评估,打印中文报告")
    p_eval.add_argument("--history", default="data/drift_history.jsonl", help="快照历史 JSONL 路径")
    p_eval.add_argument("--window", type=int, default=20, help="当前窗口快照条数(默认 20)")
    p_eval.add_argument(
        "--ref", choices=("first", "prev"), default="first",
        help="参考窗口:first=完整首窗基线(需≥2×window);prev=紧邻滚动窗(需≥window+1)",
    )
    p_eval.add_argument(
        "--bootstrap-b", type=int, default=BOOTSTRAP_B,
        help=(
            f"PSI/KL percentile bootstrap 95%% CI 重采样次数(默认 {BOOTSTRAP_B},"
            "固定种子逐位确定;0 = 跳过 CI。CI 为信息列,不改变退出码语义)"
        ),
    )
    p_eval.add_argument(
        "--kl-resample", choices=("current", "both"), default="current",
        help=(
            "整体 KL 的 CI 重采样口径:current=参考窗固定(默认,单侧现状);"
            "both=参考窗与当前窗独立重采样、同批配对(双侧重采样,计入参考窗"
            "自身的抽样噪声;仅影响整体 KL 的 CI,不改点值/分级/退出码)"
        ),
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            src = Path(args.source)
            snaps = append_drift_snapshot(src, args.history)
            for snap in snaps:
                print(
                    f"已记录快照:source={snap['source']} n={snap['n']} ts={snap['ts']}"
                )
            print(f"历史:{args.history}(本次追加 {len(snaps)} 条)")
            if src.is_dir():
                reports = sorted(src.glob("*_report.json"))
                skipped = [p.name for p in reports if p.name not in {s["source"] for s in snaps}]
                if skipped:
                    print(f"跳过(不含分数分布):{', '.join(skipped)}")
            return 0
        return evaluate(
            args.history, window=args.window, ref=args.ref,
            bootstrap_b=args.bootstrap_b, kl_resample=args.kl_resample,
        )
    except DriftError as exc:
        print(f"错误:{exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"错误:I/O 失败:{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
