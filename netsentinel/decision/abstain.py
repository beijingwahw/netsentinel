"""分歧驱动的选择性弃权 + risk-coverage 曲线(NetSentinel · decision/abstain)。

对标 2024-26 前沿的 disagreement-based abstention / selective prediction:
自动判定名单的精度可控——当多个成员模型对同一样本打分高度分歧时,聚合分
不可信,与其硬给一个判定,不如**弃权**:样本直送人工复核队列。弃权不是
verdict 三档(clean/suspect/nsfw)之外的第四判定档,而是**复核优先级信号**:
机器明确说"这题我不答,请人来看"。三档判定契约、人工门与四眼语义一概
不触碰。

组件(全部纯函数、仅标准库、无 IO / 网络 / 随机 / 时钟,离线可测):

1. :func:`decide` —— 单样本弃权判定。输入一组模型成员分数(list[float] 或
   {成员名: 分数}),输出 :class:`AbstainDecision`。分歧度量可配,内置两种:
   * ``"range"`` 极差 max-min(默认;与 ``vision/arbiter.py`` 的
     ``DISAGREE_GAP = 0.35`` 同语义——spread ≥ 阈值即升级,只读对标、
     不 import 兄弟模块,常量独立声明);
   * ``"std"`` 总体标准差(成员分数围绕均值的散布);
   亦可直接传入自定义可调用(入参 list[float],返回 float)。阈值可配,默认
   :data:`DEFAULT_ABSTAIN_THRESHOLD` = 0.35。边界:分歧恰等于阈值按弃权处理
   (与 arbiter 的 ≥ 闭边界一致);**单成员分歧恒为 0,永不弃权**(无论阈值,
   含阈值 0,见 decide docstring);空输入不弃权。
2. :func:`sweep_disagreement_threshold` —— risk-coverage 曲线。阈值定义在
   **一致度 = 1 - 分歧度**刻度上(等价于对分歧度设容忍上限 1-阈值):
   一致度不足阈值的样本弃权送人工,其余自动判定。于是阈值↑ ⟹ 容忍分歧↓
   ⟹ 弃权更多 ⟹ 覆盖率单调不增(**由构造保证**:阈值升序排序后自动判定
   保留集嵌套收缩)。端点:阈 0 → 全覆盖(容忍一切分歧);阈 ∞ → 零覆盖
   (全部弃权)。注意本阈值刻度与 decide() 的分歧阈值方向相反:t ≈ 1 - g
   (如 t = 1 - 0.35 = 0.65 与 decide(threshold=0.35) 对应同一容忍上限;
   分歧恰等于容忍上限的边界样本 sweep 按保留处理,以保证阈 0 恒全覆盖)。
3. :func:`batch_decide` —— 批量弃权判定,保序输出,同输入同输出。

与 triage 的集成(不改 triage.py)::func:`to_triage_hint` 把弃权判定映射为
dict 提示(p_nsfw 透传 / priority_weight 分歧加权),供上层接
``netsentinel.decision.triage`` 的 ``priority(p_nsfw=...)`` 或复核队列排序
加权;实际接线属后续任务。

用法示例::

    from netsentinel.decision.abstain import (
        batch_decide, decide, sweep_disagreement_threshold, to_triage_hint,
    )

    d = decide({"stub": 0.9, "nudenet": 0.1})          # 极差 0.8 ≥ 0.35 → 弃权
    hint = to_triage_hint(d, p_nsfw=0.5)               # 复核队列加权提示
    curve = sweep_disagreement_threshold(               # 校准集上扫曲线
        [[0.9, 0.95], [0.1, 0.12]], [1, 0],
    )
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

__all__ = [
    "DEFAULT_ABSTAIN_THRESHOLD",
    "DEFAULT_BINARIZE",
    "DEFAULT_SWEEP_THRESHOLDS",
    "METRIC_RANGE",
    "METRIC_STD",
    "AbstainDecision",
    "batch_decide",
    "decide",
    "disagreement_range",
    "disagreement_std",
    "sweep_disagreement_threshold",
    "to_triage_hint",
]

#: 默认弃权分歧阈值:对齐 ``vision/arbiter.py`` 的 ``DISAGREE_GAP = 0.35``
#: 语义(成员分极差达到该值即视为重大分歧,需升级处理)。只读对标,
#: 本模块不 import 任何 netsentinel 兄弟模块(与 triage 同款独立性约定)。
DEFAULT_ABSTAIN_THRESHOLD: float = 0.35

#: 极差度量名:max - min(与 arbiter 的 spread 同口径)。
METRIC_RANGE: str = "range"

#: 标准差度量名:总体标准差(除以 n,单成员自然为 0)。
METRIC_STD: str = "std"

#: 自动判定二值化阈值默认值:成员分数均值 ≥ 该值视为 1(违规),否则 0。
DEFAULT_BINARIZE: float = 0.5

#: sweep 默认阈值网格(一致度刻度,升序,含端点 0 与 ∞;0.65 = 1 - 0.35,
#: 与 decide() 默认工作点互补对应)。
DEFAULT_SWEEP_THRESHOLDS: tuple[float, ...] = (
    0.0, 0.25, 0.5, 0.65, 0.75, 1.0, float("inf"),
)

#: 内置分歧度量注册表(度量名 → 纯函数)。
_METRIC_FNS: dict[str, Callable[[list[float]], float]] = {}

#: 度量名 → 中文标签(用于 reason 文案)。
_METRIC_LABELS: dict[str, str] = {METRIC_RANGE: "极差", METRIC_STD: "标准差"}

#: 提示 dict 的来源标识(to_triage_hint 输出固定携带,便于上游溯源)。
HINT_SOURCE: str = "decision.abstain"


# ---------------------------------------------------------------------------
# 契约对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AbstainDecision:
    """单样本弃权判定契约(字段即契约, frozen 不可变、可比较、可审计)。

    安全语义:``abstain=True`` 只表示"该样本建议直送人工复核",**不是**
    verdict 三档之外的第四判定档,也绝不改变任何判定结果;自动判定样本
    仍走既有 verdict 公式与人工门。

    字段:
        abstain:      是否弃权送人工复核;
        disagreement: 成员分歧度(度量可配,极差/标准差);单成员与空输入恒 0.0;
        threshold:    生效的弃权分歧阈值(回显,便于审计与复算);
        reason:       中文可解释原因(随行说明,可直接呈报运营者)。
    """

    abstain: bool
    disagreement: float
    threshold: float
    reason: str


# ---------------------------------------------------------------------------
# 分歧度量(纯函数;输入应为有限数值,decide/sweep 入口已统一校验)
# ---------------------------------------------------------------------------


def disagreement_range(values: list[float]) -> float:
    """极差分歧度:max - min(与 arbiter 的 spread 同口径)。

    空列表按 0.0 处理(无成员即无分歧);单成员恒 0.0。
    """
    if not values:
        return 0.0
    return max(values) - min(values)


def disagreement_std(values: list[float]) -> float:
    """总体标准差分歧度:sqrt(Σ(v - mean)² / n)(除以 n 的总体口径)。

    空列表与单成员恒 0.0(单成员无散布;样本标准差除以 n-1 在 n=1 时
    无定义,故取总体口径)。求和按输入顺序,同输入同输出。
    """
    n = len(values)
    if n == 0:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(variance)


_METRIC_FNS[METRIC_RANGE] = disagreement_range
_METRIC_FNS[METRIC_STD] = disagreement_std


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _sig(x: float) -> float:
    """规整到 12 位有效数字:清除求差/求和尾部的浮点噪声,保证输出确定性。

    用 %g 而非固定小数位:保留量级只去掉末尾噪声(与 conformal 同款做法)。
    """
    return float(f"{x:.12g}")


def _validate_threshold(threshold: float, what: str) -> float:
    """校验并规整阈值(有限、非负;拒绝 bool);非法抛中文 ValueError。"""
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(float(threshold))
    ):
        raise ValueError(f"{what}必须是有限数值:{threshold!r}")
    value = float(threshold)
    if value < 0.0:
        raise ValueError(f"{what}必须非负:{threshold!r}")
    return value


def _normalize_scores(scores: Any, where: str) -> list[float]:
    """把 list[float] / {成员名: 分数} 统一规整为有限浮点列表(校验中文报错)。"""
    if isinstance(scores, Mapping):
        raw = list(scores.values())
    elif isinstance(scores, (str, bytes)):
        raise ValueError(f"{where}必须是数值序列或 {{成员名: 分数}} 映射,收到 {scores!r}")
    else:
        try:
            raw = list(scores)
        except TypeError:
            raise ValueError(
                f"{where}必须是数值序列或 {{成员名: 分数}} 映射,"
                f"收到 {type(scores).__name__}"
            ) from None
    values: list[float] = []
    for idx, item in enumerate(raw):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError(f"{where}第 {idx} 项必须是有限数值,收到 {item!r}")
        number = float(item)
        if not math.isfinite(number):
            raise ValueError(f"{where}第 {idx} 项必须是有限数值,收到 {item!r}")
        values.append(number)
    return values


def _resolve_metric(
    metric: str | Callable[[list[float]], float],
) -> tuple[Callable[[list[float]], float], str]:
    """解析分歧度量:内置名查表,可调用对象直接采用;未知名抛中文 ValueError。"""
    if callable(metric):
        name = getattr(metric, "__name__", "custom")
        return metric, name
    key = str(metric)
    fn = _METRIC_FNS.get(key)
    if fn is None:
        raise ValueError(
            f"未知分歧度量 {metric!r},内置可选:{sorted(_METRIC_FNS)}"
            "(或直接传入 list[float] -> float 的自定义可调用)"
        )
    return fn, key


def _apply_metric(
    fn: Callable[[list[float]], float], values: list[float], where: str
) -> float:
    """调用度量函数并规整:输出非有限数值时抛中文 ValueError(防御式)。"""
    raw = fn(values)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{where}输出的分歧度量必须是数值,收到 {raw!r}")
    number = float(raw)
    if not math.isfinite(number):
        raise ValueError(f"{where}输出的分歧度量必须是有限数值,收到 {raw!r}")
    return _sig(number)


def _label_01(value: Any, idx: int) -> int:
    """人工真值标签规整为 0/1:接受 bool / 0|1 的 int/float,其余抛中文报错。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number in (0.0, 1.0):
            return int(number)
    raise ValueError(f"labels 第 {idx} 项人工真值必须为 0/1,收到 {value!r}")


# ---------------------------------------------------------------------------
# 对外 API:单样本 / 批量弃权判定
# ---------------------------------------------------------------------------


def decide(
    scores: list[float] | Mapping[str, float],
    *,
    threshold: float = DEFAULT_ABSTAIN_THRESHOLD,
    metric: str | Callable[[list[float]], float] = METRIC_RANGE,
) -> AbstainDecision:
    """单样本弃权判定:成员分歧达到阈值即弃权送人工,否则允许自动判定。

    :param scores:   一组模型成员分数,``list[float]`` 或 ``{成员名: 分数}``。
    :param threshold: 弃权分歧阈值(非负有限);默认
        :data:`DEFAULT_ABSTAIN_THRESHOLD` = 0.35,对齐 arbiter 的
        ``DISAGREE_GAP`` 语义。**边界为闭边界**:分歧恰等于阈值即弃权
        (与 arbiter 的 ``spread >= gap`` 一致)。注意阈值取 0 时任何
        多成员输入的分歧 ≥ 0 恒弃权(退化同 arbiter gap=0 全升级),
        实用阈值应取正数。
    :param metric:   分歧度量:``"range"``(极差,默认)或 ``"std"``
        (总体标准差),或自定义可调用(list[float] -> float)。
    :return: :class:`AbstainDecision`(abstain / disagreement / threshold / reason)。
    :raises ValueError: 分数 / 阈值 / 度量非法时(中文消息)。

    边界语义(docstring 注明,测试锁定):
        * **单成员**:分歧恒为 0,**永不弃权**(无论阈值取多少,含 0);
        * **空输入**:无成员即无分歧,不弃权(disagreement=0.0);
        * 分歧恰等于阈值:弃权(闭边界,对齐 arbiter)。
    """
    t = _validate_threshold(threshold, "弃权阈值")
    fn, name = _resolve_metric(metric)
    values = _normalize_scores(scores, "成员分数")
    if not values:
        return AbstainDecision(False, 0.0, t, "无成员分数:无分歧可言,不弃权")
    if len(values) == 1:
        # 单成员:无散布,分歧恒为 0,永不弃权(即使阈值取 0 也不弃权)。
        return AbstainDecision(False, 0.0, t, "仅 1 个成员分数:分歧恒为 0,永不弃权")
    d = _apply_metric(fn, values, f"分歧度量 {name!r}")
    label = _METRIC_LABELS.get(name, name)
    if d >= t:
        return AbstainDecision(
            True,
            d,
            t,
            f"成员分歧度 {d:.4f}({label})≥ 阈值 {t:g}:自动判定弃权,样本直送人工复核",
        )
    return AbstainDecision(
        False,
        d,
        t,
        f"成员分歧度 {d:.4f}({label})< 阈值 {t:g}:分歧可接受,允许自动判定",
    )


def batch_decide(
    scores_list: Iterable[list[float] | Mapping[str, float]],
    *,
    threshold: float = DEFAULT_ABSTAIN_THRESHOLD,
    metric: str | Callable[[list[float]], float] = METRIC_RANGE,
) -> list[AbstainDecision]:
    """批量弃权判定:对每组成员分数逐项 :func:`decide`,**保序**输出。

    纯函数:不修改输入,不引入任何随机 / 时钟 / IO;同输入同输出
    (确定性由度量的顺序固定求和与 :func:`_sig` 规整共同保证)。空输入
    返回空列表。
    """
    return [
        decide(item, threshold=threshold, metric=metric) for item in scores_list
    ]


# ---------------------------------------------------------------------------
# 对外 API:risk-coverage 曲线
# ---------------------------------------------------------------------------


def sweep_disagreement_threshold(
    scores_list: Iterable[list[float] | Mapping[str, float]],
    labels: Iterable[Any],
    *,
    thresholds: Iterable[float] | None = None,
    metric: str | Callable[[list[float]], float] = METRIC_RANGE,
    binarize: float = DEFAULT_BINARIZE,
) -> list[tuple[float, float, float]]:
    """在人工核验集上扫分歧阈值,生成 risk-coverage 曲线点列。

    阈值刻度(与 decide() 方向相反,务必注意):阈值定义在**一致度 =
    1 - 分歧度**上——自动判定的保留条件为 ``分歧度 ≤ 1 - 阈值``,即阈值是
    对分歧度的容忍上限 1-阈值、或等价地是对一致度的下限要求。于是:

    * 阈值↑ ⟹ 容忍分歧↓ ⟹ 弃权更多 ⟹ **覆盖率单调不增**(由构造保证:
      阈值升序排序后保留集嵌套收缩,不依赖数据);
    * 端点:阈 0 → 全覆盖(保留一切分歧 ≤ 1 的样本,极差度量下即全部);
      阈 ∞(或 > 1)→ 零覆盖(全部弃权);
    * 阈值 t 与 decide() 阈值 g 互补对应(t = 1 - g,如 t=0.65 ⟺ g=0.35);
      分歧恰等于容忍上限的边界样本按保留处理(闭边界,保证阈 0 恒全覆盖)。

    每个样本的"自动判定预测" = 成员分数均值 ≥ ``binarize`` 二值化为 1/0
    (默认 0.5);``selective_risk`` = 自动判定样本中预测与人工真值
    ``labels``(0/1)不一致的比例(弃权样本不计入);零覆盖时风险按 0.0
    报(无自动判定即无已发生误判)。

    :param scores_list: 每样本一组成员分数(list[float] 或 {成员名: 分数})。
    :param labels:      与 scores_list 等长的人工真值 0/1(接受 bool)。
    :param thresholds:  待扫阈值网格(一致度刻度);缺省用
        :data:`DEFAULT_SWEEP_THRESHOLDS`;输入乱序将按升序输出(重复保留)。
    :param metric:      分歧度量(与 decide 同款:range / std / 自定义可调用)。
    :param binarize:    自动判定二值化阈值([0,1] 内有限数值,默认 0.5)。
    :return: ``[(threshold, coverage, selective_risk), ...]`` 按阈值升序;
        coverage = 自动判定比例;空输入样本集或空网格返回 ``[]``。
    :raises ValueError: 长度不一致 / 真值非 0/1 / 成员分数为空或非法 /
        网格或二值化阈值非法时(中文消息)。

    注意:校准集样本带空成员分数时**拒绝**(宁拒不猜:没有成员分的样本
    连二值化预测都无从谈起,混入会扭曲曲线;decide 对空输入的单样本宽容
        ——只回答"是否弃权",而本函数要度量自动判定质量,口径更严)。
    """
    groups_raw = list(scores_list)
    ys_raw = list(labels)
    if len(groups_raw) != len(ys_raw):
        raise ValueError(
            f"scores_list 与 labels 长度不一致:{len(groups_raw)} != {len(ys_raw)}"
        )
    if (
        isinstance(binarize, bool)
        or not isinstance(binarize, (int, float))
        or not math.isfinite(float(binarize))
        or not (0.0 <= float(binarize) <= 1.0)
    ):
        raise ValueError(
            f"二值化阈值必须是 [0,1] 内的有限数值:{binarize!r}"
        )
    b = float(binarize)

    grid: list[float] = []
    for value in (
        DEFAULT_SWEEP_THRESHOLDS if thresholds is None else list(thresholds)
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f"sweep 阈值网格每项必须为非负有限数值(允许 +inf):{value!r}"
            )
        number = float(value)
        if math.isnan(number) or number == -math.inf or number < 0.0:
            raise ValueError(
                f"sweep 阈值网格每项必须为非负有限数值(允许 +inf):{value!r}"
            )
        grid.append(number)
    grid.sort()  # 升序:保证保留集嵌套收缩(覆盖率单调不增由构造成立)

    if not groups_raw or not grid:
        return []

    fn, name = _resolve_metric(metric)
    disagreements: list[float] = []
    predictions: list[int] = []
    errors: list[bool] = []
    for i, group in enumerate(groups_raw):
        where = f"第 {i} 个样本的成员分数"
        values = _normalize_scores(group, where)
        if not values:
            raise ValueError(f"{where}为空:无成员分即无自动判定预测,拒绝入校准集")
        disagreements.append(_apply_metric(fn, values, f"分歧度量 {name!r}"))
        predicted = 1 if (sum(values) / len(values)) >= b else 0
        predictions.append(predicted)
        errors.append(predicted != _label_01(ys_raw[i], i))

    n = len(disagreements)
    points: list[tuple[float, float, float]] = []
    for t in grid:
        bound = 1.0 - t  # t=inf → -inf:任何有限分歧都 > -inf → 全弃权
        kept_errors = 0
        kept = 0
        for d, wrong in zip(disagreements, errors):
            if d <= bound:
                kept += 1
                if wrong:
                    kept_errors += 1
        coverage = _sig(kept / n)
        risk = _sig(kept_errors / kept) if kept else 0.0
        points.append((t, coverage, risk))
    return points


# ---------------------------------------------------------------------------
# 对外 API:triage 集成提示(不改 triage.py 的纯映射)
# ---------------------------------------------------------------------------


def to_triage_hint(
    decision: AbstainDecision, *, p_nsfw: float | None = None
) -> dict[str, Any]:
    """把 :class:`AbstainDecision` 映射为 triage(复核分诊)可消费的提示 dict。

    本函数只做无副作用映射,**不改 triage.py**;实际接线属后续任务。接线
    方式(上层任选其一):

    * **p_nsfw 路径**:上层若掌握样本真实聚合概率(如站点 agg_nsw_prob),
      调用时透传 ``p_nsfw=...``,再把 ``hint["p_nsfw"]`` 注入
      ``netsentinel.decision.triage`` 的 ``priority(entry, p_nsfw=...)`` 或
      ``sort_entries(p_nsfw_by_url={url: hint["p_nsfw"]})``,替代档位代理;
    * **priority 加权路径**:``hint["priority_weight"]`` 在弃权时等于分歧度
      本身(极差度量下天然落在 [0,1],分歧越大越优先复核),非弃权恒 0.0
      ——复核队列可将其作为排序键的加权输入,例如
      ``final = base_priority * (1.0 + w * hint["priority_weight"])``。

    返回键(只读快照,可安全序列化 / 记审计):
        source("decision.abstain") / abstain / disagreement / threshold /
        p_nsfw(未提供为 None)/ priority_weight / reason。

    :raises ValueError: p_nsfw 非有限数值或越出 [0,1] 时(中文消息)。
    """
    if p_nsfw is not None:
        if (
            isinstance(p_nsfw, bool)
            or not isinstance(p_nsfw, (int, float))
            or not math.isfinite(float(p_nsfw))
            or not (0.0 <= float(p_nsfw) <= 1.0)
        ):
            raise ValueError(
                f"p_nsfw 必须是 [0,1] 内的有限数值:{p_nsfw!r}"
            )
    weight = float(decision.disagreement) if decision.abstain else 0.0
    return {
        "source": HINT_SOURCE,
        "abstain": bool(decision.abstain),
        "disagreement": float(decision.disagreement),
        "threshold": float(decision.threshold),
        "p_nsfw": None if p_nsfw is None else float(p_nsfw),
        "priority_weight": weight,
        "reason": str(decision.reason),
    }
