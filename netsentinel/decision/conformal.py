"""共形式精度担保(A45,CONTRACTS-V3.md §3;红线 14)。

背景与定位
----------
运营者面对的核心问题是:"这张名单里到底有多少是真的?"。本模块用人工核验过的
校准集( [(分值, 是否真实违规), ...] )为"分值 ≥ 阈值"的入报名单拟合一个可核查的
经验精度下限,让"名单精度 ≥95%"这类承诺有数字、有前提、有边界 —— 而不是一句
无法兑现的广告词。

方法(诚实版的共形思路)
------------------------
1. 校准集按分值降序排列,逐前缀计算 精度 = 前缀内真实违规数 / 前缀长度;
2. 取"精度仍 ≥ 目标"的**最大**前缀 k,阈值 = 第 k 个分值(入选规则 = 分值 ≥ 阈值);
3. 并列分值按"整组进 / 整组出"处理:部署时同分样本不可区分,担保只能落在组
   边界上,否则 apply() 按"≥ 阈值"的实际选集会偏离被担保的前缀,担保失真;
4. 该担保本质是校准集上的有限样本经验度量,近似成立的前提是
   "校准集与线上数据同分布(可交换)",前提破坏时担保即失效,必须原样告知。

有限样本统计担保升级(2026-10,对标 Angelopoulos & Bates 的
Conformal Risk Control / Learn-then-Test):
5. 每个候选阈值(=每个并列分组的组边界)上做**精确单侧二项检验**
   H0: 真实精度 ≤ 目标精度;p 值 = P(Bin(m, 目标) ≥ k),m=前缀长度、
   k=前缀内真实违规数;尾概率在对数空间用 math.lgamma 精确求和,大 n 不溢出;
6. 多重检验按 Bonferroni 除以候选数 |Λ| 得校正水平 δ/|Λ|,在**通过校正的
   候选中取最深前缀**(族错误率 FWER ≤ δ)。为守住"行为向后兼容"红线,
   生效阈值 threshold 保持既有经验选择不变,LTT 以**认证层**形态落地:
   * 经验最深前缀本身通过校正检验 → selection_mode="ltt"(生效选择被
     统计认证,FWER ≤ δ);
   * 有候选通过但都比经验最深前缀浅 → selection_mode="ltt_advisory":
     生效阈值不变(向后兼容),经认证的更保守阈值通过 ltt_threshold /
     ltt_lower_bound 等新增字段**以建议形式**给出(与本模块"产出仅是
     复核建议"的定位一致),供运营者人工采纳;
   * 无任何候选通过 → selection_mode="empirical_fallback",如实标注
     "经验点估计,无 δ 水平统计背书";
7. 生效前缀输出 Clopper-Pearson 精确 (1-δ) 单侧置信下界:二分法解尾方程
   P(Bin(m, p) ≥ k) = δ(全真前缀有闭式 δ^(1/m))。数学事实:凡通过
   Bonferroni 校正检验的前缀,其 CP (1-δ) 下界必 ≥ 目标精度,两组输出
   自洽;未通过时下界可能低于目标,输出原样呈现、不粉饰。
   δ 默认 0.05、可配(fit_threshold(..., delta=…)),旧字段、降级闸门与
   并列分组语义完全不变,仅在结果对象上**新增**统计字段。

安全红线 14(统计担保要诚实)在本模块的落地:
- 一切输出随行 caveat(校准集来源与规模前提),绝不输出无条件精度承诺;
- 降级条件(任一即 valid=False、threshold=None、empirical_precision=None,
  不做任何数字背书):
  * 校准样本 n < 30(经验精度在小样本上波动过大);
  * 校准集没有任何真实违规(正)样本,精度无从定义;
  * 不存在满足目标精度的前缀(例如最高分样本本身就是误报);
- 产出仅以"建议"形式呈现(CONTRACTS-V3 §4:不自动改生产阈值),
  入选名单仍须人工复核,不得据此自动处置(不削弱人工门)。

纯函数、仅标准库、无 IO / 网络 / 全局状态(遥测仅记名称与数字),可离线单测。

用法示例::

    from netsentinel.decision.conformal import apply, fit_threshold, merge_reports

    fit = fit_threshold(calibration, 0.95)     # 拟合"精度>=0.95"的分值阈值
    out = apply(scores, fit["threshold"])       # 分值>=阈值 → 入选名单
    print(merge_reports(fit))                   # 可直呈运营者的中文报告
"""
from __future__ import annotations

import math

from netsentinel import telemetry

__all__ = [
    "MIN_CALIBRATION_N",
    "DEFAULT_DELTA",
    "CAVEAT",
    "fit_threshold",
    "apply",
    "merge_reports",
]

#: 给出担保所需的最小校准样本量;n < 30 时经验精度的置信区间过宽,直接降级
MIN_CALIBRATION_N: int = 30

#: 默认统计误差预算 δ(Clopper-Pearson 置信水平 = 1-δ = 0.95);可经
#: fit_threshold(..., delta=…) 配置,必须严格落在 (0, 0.5) 开区间
DEFAULT_DELTA: float = 0.05

#: 担保前提(固定随行;红线 14:不得宣传无条件精度)
CAVEAT: str = "担保依赖校准集与线上数据同分布;n<30 不给出担保"


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _finite_float(score: int | float) -> bool:
    """分值转 float 并要求有限;巨整溢出/NaN/Inf 一律按非法处理(V7 安全内核加固)。"""
    try:
        return not math.isnan(float(score)) and math.isfinite(float(score))
    except (OverflowError, ValueError):
        return False


def _sig(x: float) -> float:
    """规整到 12 位有效数字:清除求和/二分尾部的浮点噪声,保证输出确定性。

    用 %g 而非固定小数位:p 值可达 1e-8 量级,f"{x:.12f}" 会把它抹成 0,
    而有效数字规整保留量级只去掉末尾噪声。
    """
    return float(f"{x:.12g}")


def _binom_upper_tail(
    k: int, m: int, p: float, stop_above: float | None = None
) -> float:
    """精确计算二项上尾概率 P(Bin(m, p) ≥ k)(单侧 p 值 / CP 求根共用)。

    对数空间逐项求和:log C(m,j) 经 math.lgamma 计算(组合数在 m 大时
    天文级大,直接 math.comb 相乘会溢出/损失精度;lgamma 全程对数无溢出,
    项数 m-k+1 ≤ n 线性)。每个被加项非负,概率空间累加无相消误差,
    单项相对误差 ~1e-15 量级。

    stop_above 非空时启用**提前中止**:部分和一旦严格超过 stop_above 即返回
    (真值只会更大,判"不通过"的决策不受影响)——Learn-then-Test 候选扫描
    中绝大多数候选的 p 值远大于校正水平,可省去绝大部分求和;选中项的
    报告值另用无中止模式精确重算。
    """
    if k <= 0:
        return 1.0
    if k > m:
        return 0.0
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    log_p = math.log(p)
    log_q = math.log1p(-p)
    lgam_m1 = math.lgamma(m + 1)
    total = 0.0
    for j in range(k, m + 1):
        log_term = (
            lgam_m1
            - math.lgamma(j + 1)
            - math.lgamma(m - j + 1)
            + j * log_p
            + (m - j) * log_q
        )
        total += math.exp(log_term)  # 极小项下溢为 0.0,对和的贡献确为 ~0
        if stop_above is not None and total > stop_above:
            return total
    return min(1.0, total)


def _clopper_pearson_lower(k: int, m: int, delta: float) -> float:
    """Clopper-Pearson 精确 (1-δ) 单侧置信下界:解尾方程 P(Bin(m, p) ≥ k) = δ。

    尾概率对 p 单调递增(随机单调性),故二分必收敛;k=m 时有闭式
    δ^(1/m)。二分下界 lo 恒满足尾概率 < δ(偏保守一侧),绝不虚高;
    全真前缀 n 有限时下界 = δ^(1/n) < 1——这正是"有限样本上无法诚实
    担保绝对精度"的数学表达(红线 14:统计担保要诚实)。
    """
    if m <= 0 or k <= 0:
        return 0.0
    if k >= m:
        return _sig(delta ** (1.0 / m))
    lo, hi = 0.0, 1.0  # f(p)=尾概率-δ:f(lo)<0 < f(hi),单增故根唯一
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if _binom_upper_tail(k, m, mid) < delta:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-16:
            break
    return _sig(lo)


def _clean_and_sort(
    calibration: list[tuple[float, bool]],
) -> list[tuple[float, bool]]:
    """校验校准集并按分值降序(稳定排序)整理;非法项抛 ValueError(中文)。

    对应红线 8 的防御式风格:上游送来的分值 / 标签不做任何"猜测式"解释,
    非二元组、非有限数值分值、非 bool 标签一律拒绝,避免脏数据悄悄扭曲担保。
    """
    cleaned: list[tuple[float, bool]] = []
    for idx, item in enumerate(calibration):
        try:
            score, label = item
        except (TypeError, ValueError):
            raise ValueError(
                f"校准集第 {idx} 项应为 (分值, 是否真实违规) 二元组,"
                f"收到 {item!r}"
            ) from None
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not _finite_float(score)
        ):
            raise ValueError(
                f"校准集第 {idx} 项分值非法: {score!r}(应为可排序的有限数值)"
            )
        if not isinstance(label, bool):
            raise ValueError(
                f"校准集第 {idx} 项标签非法: {label!r}(应为 bool:是否真实违规)"
            )
        cleaned.append((float(score), label))
    cleaned.sort(key=lambda pair: pair[0], reverse=True)
    return cleaned


def _degraded(n: int, target_precision: float, reason: str, delta: float) -> dict:
    """构造降级结果:不给阈值、不给数字背书,只给中文原因与固定前提。

    任何降级路径(小样本 / 全负样本 / 无达标前缀)统一在此计数
    ``conformal.degraded``,供运营侧监控担保失效率。统计升级字段
    (lower_bound / p_value / corrected_alpha / n_candidates)在降级时
    一律为 None:没做过选择就不给数字,与"不做数字背书"的语义一致。
    """
    telemetry.inc("conformal.degraded")
    return {
        "threshold": None,
        "guarantee": f"未给出担保:{reason}",
        "empirical_precision": None,
        "n": n,
        "valid": False,
        "caveat": CAVEAT,
        "target_precision": target_precision,
        "delta": delta,
        "confidence": _sig(1.0 - delta),
        "lower_bound": None,
        "p_value": None,
        "corrected_alpha": None,
        "n_candidates": None,
        "selection_mode": "degraded",
        "ltt_threshold": None,
        "ltt_prefix_size": None,
        "ltt_lower_bound": None,
    }


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------


def fit_threshold(
    calibration: list[tuple[float, bool]],
    target_precision: float,
    delta: float = DEFAULT_DELTA,
) -> dict:
    """在人工核验校准集上拟合"名单精度 ≥ target_precision"的分值阈值。

    参数:
        calibration:      [(分值, 是否真实违规), ...];分值通常为模型 / 融合分
                          (不强制 0~1 范围,仅要求可排序的有限数值),
                          标签必须为 bool(True=人工确认真实违规);
        target_precision: 目标名单精度,必须严格落在 (0.5, 1) 开区间,
                          否则 ValueError —— ≤0.5 的"精度目标"没有运营意义,
                          而 =1 的绝对精度在有限样本上无法诚实达到;
        delta:            统计误差预算(默认 0.05);置信水平 = 1-δ。必须严格
                          落在 (0, 0.5) 开区间,否则 ValueError —— δ ≥ 0.5 的
                          "置信水平 ≤ 50%"不构成有意义的统计担保。

    算法(2026-10 升级,Learn-then-Test 风格):
        1. 降序排列,并列分值整组进出,枚举全部组边界候选阈值(=前缀);
        2. 每个候选取精确单侧二项 p 值 P(Bin(m, 目标) ≥ k),Bonferroni
           除以候选数得校正水平 δ/|Λ|;取**通过校正的最深前缀**;
        3. 生效阈值 threshold 恒保持既有经验语义(精度仍 ≥ 目标的最大
           达标前缀 —— 红线"行为向后兼容":同输入下旧字段输出逐字节不变);
           LTT 结果作为认证层叠加:
           * 经验最深前缀本身通过校正 → selection_mode="ltt";
           * 仅更浅候选通过 → "ltt_advisory",认证通过的更保守阈值以
             ltt_threshold 等新字段**建议式**给出(不自动改生效阈值);
           * 无候选通过 → "empirical_fallback"(纯经验点估计,无统计背书)。
           凡通过 Bonferroni 校正的前缀,其 Clopper-Pearson (1-δ) 下界
           必 ≥ 目标精度(两组输出数学自洽)。

    返回 dict 键:
        threshold:            生效阈值(入选规则 = 分值 ≥ threshold;
                              与升级前完全一致);任何降级情形下为 None;
        guarantee:            中文担保 / 降级说明(降级时只讲原因,不做数字背书);
        empirical_precision:  该阈值在校准集上的经验精度(降级时为 None);
        n:                    校准集规模;
        valid:                是否给出有效担保;
        caveat:               担保前提(固定文案,红线 14);
        target_precision:     目标精度回显(供 merge_reports 等报告层使用);
        delta:                统计误差预算回显;
        confidence:           置信水平 = 1-δ(降级时同样回显,前提本身恒真);
        lower_bound:          生效前缀精度的 Clopper-Pearson 精确 (1-δ) 单侧
                              下界(降级时为 None);
        p_value:              生效前缀的精确单侧二项 p 值
                              P(Bin(m, target) ≥ k)(降级时为 None);
        corrected_alpha:      Bonferroni 校正水平 δ/|Λ|(降级时为 None);
        n_candidates:         候选阈值(并列分组边界)个数(降级时为 None);
        selection_mode:       "ltt" | "ltt_advisory" | "empirical_fallback"
                              | "degraded";
        ltt_threshold:        通过校正的最深前缀的阈值(None = 无候选通过;
                              "ltt" 模式下 == threshold);
        ltt_prefix_size:      该认证前缀的长度(候选通过时);
        ltt_lower_bound:      该认证前缀的 Clopper-Pearson (1-δ) 下界
                              (候选通过时;数学上必 ≥ 目标精度)。

    降级条件(任一即 valid=False 且 threshold=None):n < 30、全负样本、
    或不存在满足目标精度的前缀(LTT 通过 ⟹ 经验达标,故后一降级不可能
    被 LTT 翻案)。
    """
    if (
        isinstance(target_precision, bool)
        or not isinstance(target_precision, (int, float))
        or math.isnan(float(target_precision))
        or not (0.5 < float(target_precision) < 1.0)
    ):
        raise ValueError(
            f"目标精度必须严格落在 (0.5, 1) 开区间,收到 {target_precision!r};"
            "更低的目标没有运营意义,等于 1 的绝对精度无法诚实担保"
        )
    if (
        isinstance(delta, bool)
        or not isinstance(delta, (int, float))
        or math.isnan(float(delta))
        or not (0.0 < float(delta) < 0.5)
    ):
        raise ValueError(
            f"统计误差预算 delta 必须严格落在 (0, 0.5) 开区间,"
            f"收到 {delta!r};置信水平 1-delta 须高于 50% 才构成统计担保"
        )

    with telemetry.timer("conformal.fit"):
        n = len(calibration)
        # V5 结构锁定:排序一次(_clean_and_sort 内校验+降序排序同趟)+ 前缀
        # 分组扫描一次;全负样本判定已并入扫描(tp 累计即正样本总数),
        # 相比旧实现少一趟 any() 全量遍历。
        ordered = _clean_and_sort(calibration)

        # ---- 降级闸门 1:样本量不足(红线:样本不足不得给担保)----
        if n < MIN_CALIBRATION_N:
            return _degraded(
                n,
                target_precision,
                f"校准样本量 n={n} < {MIN_CALIBRATION_N},经验精度在小样本上"
                f"波动过大,不给出阈值与任何精度担保;请积累不少于 "
                f"{MIN_CALIBRATION_N} 例人工核验样本(分值 + 是否真实违规)后重新拟合",
                delta,
            )

        # ---- 分组扫描:并列分值整组进出,持续覆盖得到"仍达标的最大前缀" ----
        # boundaries 收集每个组边界的 (累计真实数, 前缀长度) 作为 LTT 候选;
        # 经验语义(best_k/best_prec)与升级前逐字节一致,行为向后兼容。
        best_k = 0
        best_tp = 0
        best_prec = 0.0
        boundaries: list[tuple[int, int]] = []
        k = 0
        tp = 0  # 扫描结束时即正样本总数(兼作"全负样本"降级判据)
        i = 0
        while i < n:
            score = ordered[i][0]
            while i < n and ordered[i][0] == score:  # 同分组:整组累计
                if ordered[i][1]:
                    tp += 1
                k += 1
                i += 1
            prec = tp / k
            boundaries.append((tp, k))  # 组边界 = 一个候选阈值(前缀)
            if prec >= target_precision:
                best_k = k  # 不提前 break:精度非单调,后段可能重新达标且 k 更大
                best_tp = tp
                best_prec = prec

        # ---- 降级闸门 2:没有任何正样本,精度无从定义 ----
        if tp == 0:
            return _degraded(
                n,
                target_precision,
                "校准集中没有任何真实违规(正)样本,名单精度无从定义,"
                "不给出阈值与任何精度担保;请补充含真实违规样本的校准集",
                delta,
            )

        # ---- 降级闸门 3:连 k=1 都不达标(如最高分样本即为误报)----
        if best_k == 0:
            return _degraded(
                n,
                target_precision,
                f"校准集中不存在任何满足目标精度 {target_precision} 的分值前缀"
                "(例如最高分样本本身就是误报),无法给出阈值;建议降低目标精度、"
                "或修正模型 / 融合分后重新校准",
                delta,
            )

        # ---- Learn-then-Test:候选阈值上的精确二项检验 + Bonferroni 校正 ----
        n_candidates = len(boundaries)
        corrected_alpha = delta / n_candidates
        ltt_k = 0
        ltt_m = 0
        for cand_k, cand_m in boundaries:
            # 经验精度 ≤ 目标 ⟹ p 值 ≥ P(Bin(m,目标) ≥ 均值附近) ≳ 0.5
            # > 校正水平(α_c ≤ δ < 0.5),必不通过:跳过精确求和(大 n 提速,
            # 判定与精确计算完全一致,不影响选择结果)。
            if cand_k <= cand_m * target_precision:
                continue
            p_scan = _binom_upper_tail(
                cand_k, cand_m, target_precision, stop_above=corrected_alpha
            )
            if p_scan <= corrected_alpha and cand_m > ltt_m:
                ltt_k, ltt_m = cand_k, cand_m  # 取通过校正的最深前缀

        confidence = _sig(1.0 - delta)
        # 生效阈值保持既有经验语义(红线:行为向后兼容);统计量针对该生效前缀
        threshold = ordered[best_k - 1][0]
        prec = round(best_prec, 4)
        p_value = _binom_upper_tail(
            best_tp, best_k, target_precision
        )  # 无中止:报告值精确
        lower_bound = _clopper_pearson_lower(best_tp, best_k, delta)
        base = (
            f"分值≥{threshold} 的名单经验精度 {prec:.2f} ≥ 目标 {target_precision}"
        )

        ltt_threshold = None
        ltt_prefix_size = None
        ltt_lower_bound = None
        if ltt_m and ltt_m == best_k:
            # 经验最深前缀本身通过校正:生效选择即被认证(FWER ≤ δ)
            selection_mode = "ltt"
            telemetry.inc("conformal.ltt_certified")
            ltt_threshold = threshold
            ltt_prefix_size = best_k
            ltt_lower_bound = lower_bound
            guarantee = (
                base
                + f";Learn-then-Test 已在 {n_candidates} 个候选阈值上以 Bonferroni "
                f"校正(水平 {corrected_alpha:.4g})通过精确二项检验"
                f"(p={p_value:.4g}),Clopper-Pearson {confidence:g} 置信下界 "
                f"{lower_bound:.4f} 不劣于目标精度,统计上站得住"
            )
        elif ltt_m:
            # 仅更浅前缀通过:生效阈值不变(向后兼容),认证阈值以建议形式随行
            selection_mode = "ltt_advisory"
            telemetry.inc("conformal.ltt_certified")
            ltt_threshold = ordered[ltt_m - 1][0]
            ltt_prefix_size = ltt_m
            ltt_lower_bound = _clopper_pearson_lower(ltt_k, ltt_m, delta)
            guarantee = (
                base
                + f";注意:该经验选择未通过 Bonferroni 校正检验"
                f"(p={p_value:.4g} > {corrected_alpha:.4g}),Clopper-Pearson "
                f"{confidence:g} 置信下界 {lower_bound:.4f};统计认证(FWER ≤ "
                f"{delta:g})可支撑的更保守阈值为 {ltt_threshold}(前缀 "
                f"{ltt_m} 条,CP 下界 {ltt_lower_bound:.4f}),仅作建议,"
                f"由运营者人工决定是否采纳"
            )
        else:
            # 无任何候选通过:纯经验点估计,如实标注无统计背书
            selection_mode = "empirical_fallback"
            guarantee = (
                base
                + f";注意:无任何候选阈值通过 Bonferroni 校正检验(校正水平 "
                f"{corrected_alpha:.4g}),经验精度仅为点估计,Clopper-Pearson "
                f"{confidence:g} 置信下界 {lower_bound:.4f},有限样本上不构成"
                f"{confidence:g} 置信的达标担保"
            )
        return {
            "threshold": threshold,
            "guarantee": guarantee,
            "empirical_precision": prec,
            "n": n,
            "valid": True,
            "caveat": CAVEAT,
            "target_precision": target_precision,
            "delta": delta,
            "confidence": confidence,
            "lower_bound": lower_bound,
            "p_value": _sig(p_value),
            "corrected_alpha": _sig(corrected_alpha),
            "n_candidates": n_candidates,
            "selection_mode": selection_mode,
            "ltt_threshold": ltt_threshold,
            "ltt_prefix_size": ltt_prefix_size,
            "ltt_lower_bound": ltt_lower_bound,
        }


def apply(scores: list[tuple[str, float]], threshold: float | None) -> dict:
    """按已拟合阈值从 (标识, 分值) 列表选取入报名单(分值 ≥ 阈值,按分值降序)。

    threshold=None(拟合降级)时选出空名单:宁可不报警,也不输出无担保的名单;
    此时全部条目计入 rejected,保留人工研判,不做任何精度承诺。

    返回 dict 键:
        selected:                [(标识, 分值), ...] 按分值降序;
        expected_precision_note: 中文说明(重申担保前提与人工复核不可省略);
        rejected:                未入选条数(分值 < 阈值;无阈值时为全部)。
    """
    cleaned: list[tuple[str, float]] = []
    for idx, item in enumerate(scores):
        try:
            sid, p = item
        except (TypeError, ValueError):
            raise ValueError(
                f"scores 第 {idx} 项应为 (标识, 分值) 二元组,收到 {item!r}"
            ) from None
        if isinstance(p, bool) or not isinstance(p, (int, float)) or math.isnan(
            float(p)
        ):
            raise ValueError(f"scores 第 {idx} 项分值非法: {p!r}(应为有限数值)")
        cleaned.append((sid, float(p)))

    if threshold is None:
        return {
            "selected": [],
            "expected_precision_note": (
                "无有效阈值(校准样本不足或未达标):不输出自动名单,"
                "全部条目保留人工研判,不做任何精度承诺"
            ),
            "rejected": len(cleaned),
        }

    selected = sorted(
        (pair for pair in cleaned if pair[1] >= threshold),
        key=lambda pair: pair[1],
        reverse=True,
    )
    rejected = len(cleaned) - len(selected)
    note = (
        f"已按阈值 {threshold} 选取 {len(selected)} 条(分值≥{threshold},降序),"
        f"拒绝 {rejected} 条;名单精度为校准集上的经验值,仅在校准集与线上数据"
        "同分布时近似成立,入选条目仍须人工复核后方可对外使用"
    )
    return {
        "selected": selected,
        "expected_precision_note": note,
        "rejected": rejected,
    }


def merge_reports(fit: dict, extra_note: str = "") -> str:
    """把 fit_threshold 的结果汇编成一段可直呈运营者的中文报告(含前提与免责)。

    报告固定包含:阈值 / 经验精度 / 目标精度 / 校准样本量 / 担保前提(caveat,
    并注明校准集须来自与线上同分布的人工核验且需定期重校准)/ 免责声明
    (不构成无条件精度承诺,人工复核不可省略)。extra_note 非空时追加在末尾
    (例如 "校准集来源:2026-09 第 38 周人工核验 60 例")。

    对缺失键容错(.get):部分 dict 也能生成说明,不会抛异常 —— 报告层宁可
    说"未提供",也不能因为少一个键而崩溃或编造数字。
    """
    valid = bool(fit.get("valid", False))
    n = fit.get("n", 0)
    target = fit.get("target_precision")
    prec = fit.get("empirical_precision")
    threshold = fit.get("threshold")
    prec_text = f"{prec:.2f}" if isinstance(prec, (int, float)) else "无"
    target_text = (
        f"{float(target) * 100:g}%" if isinstance(target, (int, float)) else "未提供"
    )

    lines = ["【名单精度担保报告(共形式校准)】"]
    if valid and threshold is not None:
        lines.append(f"- 生效阈值:分值 ≥ {threshold}")
        lines.append(f"- 经验精度:{prec_text}(目标 {target_text})")
        lines.append(f"- 校准样本量:n={n}")
    else:
        lines.append("- 生效阈值:无(未给出有效担保,threshold=None)")
        lines.append(f"- 校准样本量:n={n}")
        lines.append(f"- 经验精度:{prec_text}")
    # 统计升级字段(2026-10):缺键 / None 容错 —— 没做过检验就不写,绝不编数字
    lower_bound = fit.get("lower_bound")
    confidence = fit.get("confidence")
    p_value = fit.get("p_value")
    corrected_alpha = fit.get("corrected_alpha")
    mode = fit.get("selection_mode")
    if isinstance(lower_bound, (int, float)) and not isinstance(lower_bound, bool):
        conf_text = (
            f"{float(confidence) * 100:g}%"
            if isinstance(confidence, (int, float))
            else "未知置信水平"
        )
        lines.append(
            f"- 统计背书:Clopper-Pearson {conf_text} 置信下界 "
            f"{float(lower_bound):.4f}"
        )
        if isinstance(p_value, (int, float)) and isinstance(
            corrected_alpha, (int, float)
        ):
            verdict = "通过" if float(p_value) <= float(corrected_alpha) else "未通过"
            lines.append(
                f"- Learn-then-Test 检验:精确二项 p={float(p_value):.4g},"
                f"Bonferroni 校正水平 {float(corrected_alpha):.4g}({verdict};"
                f"选择模式 {mode or '未知'})"
            )
        ltt_threshold = fit.get("ltt_threshold")
        if isinstance(ltt_threshold, (int, float)) and mode == "ltt_advisory":
            ltt_lb = fit.get("ltt_lower_bound")
            ltt_lb_text = (
                f"{float(ltt_lb):.4f}"
                if isinstance(ltt_lb, (int, float))
                else "未提供"
            )
            lines.append(
                f"- 统计认证建议:更保守阈值 分值 ≥ {ltt_threshold}"
                f"(Clopper-Pearson 下界 {ltt_lb_text}),仅建议,人工决定是否采纳"
            )
    lines.append(
        "- 担保前提:"
        f"{fit.get('caveat') or CAVEAT}"
        "(校准集须来自与线上数据同分布的人工核验结果,并随数据漂移定期重校准)"
    )
    lines.append(
        "- 免责:以上为校准集上的有限样本经验度量,不构成无条件精度承诺;"
        "名单仅是复核建议,入选条目仍须人工核实,不得据此自动处置。"
    )
    guarantee = fit.get("guarantee")
    if guarantee:
        lines.append(f"- 担保说明:{guarantee}")
    if extra_note:
        lines.append(f"- 附注:{extra_note}")
    return "\n".join(lines)
