"""时序内核·Kleinberg 爆发检测与巡查 TTL 联动建议(V10.4)。

对标 Jon Kleinberg, "Bursty and Hierarchical Structure in Streams"
(KDD 2002) 的爆发检测自动机,以及 temporal graph analysis 中"按边
时间戳识别团伙活跃期"的思路:**团伙扩张期**(图谱上 phash_near /
redirect 边密集出现)动态缩短相关站点的重扫 TTL,稳态则维持基准
TTL 省预算。V10.4 起 :mod:`netsentinel.intel.graph` 的边已持久化
``created_at`` 时间戳,本模块是它的第一个时序消费者。

五个纯函数入口(零 IO、零网络、零第三方依赖,只 import math 与
collections.abc;不 import 任何 netsentinel 模块,与 intel 内核
graph_kernel / phash_lsh 同纪律):

- :func:`kleinberg_bursts` —— 经典两状态(默认 ``levels=2``,可扩展
  k 层)无限自动机 burst 检测:输入事件时间戳序列(小时),Viterbi
  动态规划求最优状态路径,输出 burst 区间列表
  ``[{"start", "end", "level", "weight"}]``;
- :func:`kleinberg_bursts_nested` —— 与 :func:`kleinberg_bursts` 共用
  同一条 Viterbi 最优状态路径与操作计数,但按 level 阈值分层切分为
  原论文式层级嵌套树 ``[{level, start, end, weight, children}]``
  (level q 区间必然嵌套于 ≤ q−1 区间内;无穷背景层不输出);
- :func:`burst_factor` —— 依据 burst 列表与当前时刻给出 [0, 1] 的
  "当前爆发强度"(区间内按权重饱和、区间外按半衰期指数衰减);
- :func:`suggest_ttl` —— 由基准 TTL 与爆发强度给出建议 TTL
  (爆发期收缩,公式见 docstring,保持对 factor 单调不增);
- :func:`calibrate_params` —— 半衰期 × 收缩系数网格标定工具(A207
  交付后续:两参数原为经验缺省):给定多站点事件流与"实际需要重扫"
  的真值时点,滚动仿真各参数组合的建议 TTL 序列,统计命中 / 浪费,
  输出帕累托前沿与中文取舍建议(无单一最优时诚实呈现权衡)。

对数域数值稳定(防溢出,红线级实现纪律):

- 基态速率 λ0 = n/T 的对数用 ``log(n) - log(T)`` 直接相减获得,
  T 极大(如 1e300 小时)时 n/T 已是次正规浮点甚至 0,先除后取对数
  会丢精度 / 抛错,对数域相减则始终有限;
- 似然中的组合归一化项 ln(n!) 用 :func:`math.lgamma` 计算——
  ``float(factorial(n))`` 在 n ≥ 171 即溢出,lgamma 到 n = 10^18
  仍然有限(该项对基线与最优路径同加,权重差值中完全抵消);
- 每档速率差 (s^q - 1) 用 :func:`math.expm1` 计算,小 q·ln s 时
  避免 ``s**q - 1`` 的灾难性消去;
- 顶层速率对数 ``(levels-1)·ln s + ln n`` 超过 float 指数域上限
  (~709) 时直接抛中文 ValueError 拒算,而非中途溢出为 inf。

操作计数(红线 31:基准以操作计数断言,禁墙钟;沿用
``louvain_communities(adjacency, stats=...)`` 的出参惯例):
``stats`` 传入 dict 时,函数会填入 ``events_in / events_used /
gaps / levels / dp_cells / dp_trans_evals / cost_base / cost_opt /
bursts``。核心不变量:``dp_cells == gaps × levels``、
``dp_trans_evals == (gaps-1) × levels²``——对事件数**线性**、对层数
平方,远优于穷举 ``levels^gaps`` 条状态路径。``kernel_selfcheck()``
供 A138 内核基准总控离线调用。

联动装配点(**只读说明**,scheduler / adaptive 均在领地外,本模块
不改动它们;以下为推荐接线方式)::

    # scheduler / adaptive 侧(示意,未实现):
    # 1) 取图谱中与本站点相关的边时间戳(小时序;created_at 为 ISO 串)
    edge_hours = [
        (parse_iso(row["created_at"]) - epoch).total_seconds() / 3600.0
        for row in graph.edges_of(site_url)
        if row["kind"] in ("phash_near", "redirect")
    ]
    # 2) 爆发检测 → 当前强度 → 建议 TTL
    bursts = kleinberg_bursts(edge_hours)              # 无事件/事件不足 → []
    factor = burst_factor(bursts, now_hours)           # [0, 1]
    ttl = suggest_ttl(base_ttl_h=mem.ttl_hours, factor=factor)
    # 3) 喂给站点记忆做单次决策(默认 None = 库级 TTL,行为不变)
    need, reason = mem.should_rescan(site_url, fp, ttl_hours=ttl)

红线:建议 TTL 只影响"指纹未变时跳过多久"这一档,**不绕过任何
频控**——重扫频率仍受全局礼貌间隔(politeness)与频控钳制约束,
建议值本身也钳制在 ``[min_h, max_h]``(默认 [6, 720] 小时,与
ops.adaptive 的 MIN/MAX_INTERVAL_H 同口径)内。

模型备忘(经典两状态,记号沿用原论文):事件间隔服从指数分布,
基态(状态 0)速率 λ0 = n/T(n 为事件数、T 为时间窗宽度),状态 q
速率 λq = s^q·λ0;间隔 Δ 处于状态 q 的代价 = λq·Δ − ln λq(指数
分布的负对数似然),从状态 p 升到 q>p 的转移代价 = γ·(q−p)·ln s
(回落免费)。均匀流中 λ0·Δ ≈ n/(n−1) > 1,升档每间隔净亏
ln s − λ0Δ·(s−1) < 0(s=2 时约 −0.31),自动机停留在基态 → 无
burst;密集段中 λ0·Δ → 0,升档每间隔净赚 ≈ q·ln s,一次爬升代价
γ·q·ln s 被多间隔摊薄 → burst 区间恰好覆盖密集段。
"""
from __future__ import annotations

import math
from collections.abc import Iterable

__all__ = [
    "CALIB_DEFAULT_HALFLIVES_H",
    "CALIB_DEFAULT_SHRINKS",
    "DEFAULT_DECAY_HALFLIFE_H",
    "DEFAULT_GAMMA",
    "DEFAULT_LEVELS",
    "DEFAULT_S",
    "MAX_TTL_H",
    "MIN_TTL_H",
    "TTL_SHRINK",
    "burst_factor",
    "calibrate_params",
    "kernel_selfcheck",
    "kleinberg_bursts",
    "kleinberg_bursts_nested",
    "suggest_ttl",
]

#: 速率缩放系数 s 缺省(状态 q 速率为基态的 s^q 倍;原论文默认 2)。
DEFAULT_S = 2

#: 爬升转移惩罚 γ 缺省(升一层代价 γ·ln s 个自然对数单位;越大越保守)。
DEFAULT_GAMMA = 1

#: 状态层数缺省(2 = 经典两状态:基态 0 + 一层爆发态 1)。
DEFAULT_LEVELS = 2

#: burst_factor 缺省半衰期(小时):burst 结束后强度每过这么久减半。
DEFAULT_DECAY_HALFLIFE_H = 24.0

#: suggest_ttl 的收缩系数:factor=1 时建议 TTL 收缩到基准的 20%。
TTL_SHRINK = 0.8

#: 建议 TTL 硬下限(小时;与 ops.adaptive.MIN_INTERVAL_H 同口径)。
MIN_TTL_H = 6

#: 建议 TTL 硬上限(小时;与 ops.adaptive.MAX_INTERVAL_H 同口径)。
MAX_TTL_H = 720

#: 参数标定缺省半衰期网格(小时)。A207 交付报告后续:DEFAULT_DECAY_HALFLIFE_H
#: = 24h 为经验缺省、尚无真实巡查数据标定,calibrate_params 以此网格搜索。
CALIB_DEFAULT_HALFLIVES_H = (6.0, 12.0, 24.0, 48.0, 96.0)

#: 参数标定缺省收缩系数网格。TTL_SHRINK = 0.8 同为经验缺省,待真实数据标定。
CALIB_DEFAULT_SHRINKS = (0.5, 0.8, 0.9)

#: calibrate_params 单流重扫序列的安全上限(防"时间跨度 / TTL 下限"过大时
#: 仿真失控;正常巡查窗口以小时计,远达不到该量级)。
_CALIB_MAX_RESCANS = 50_000

_LN2 = math.log(2.0)

#: float 指数域安全上限的自然资源对数(exp 参数超过它即溢出为 inf)。
_LN_MAX_EXP = 709.0


def _validate_events(events: Iterable[float]) -> tuple[int, list[float]]:
    """把输入事件规范化为"(原始条数, 有限浮点去重升序列表)"。

    - 元素经 ``float()`` 强转(拒绝不可转对象,自然抛 TypeError);
    - 含 inf / nan 直接抛中文 ValueError(排序与对数运算都会失真);
    - 重复时间戳去重(零间隔会让 ln Δ 发散,语义上同刻事件合并)。
    """
    times: list[float] = []
    raw = 0
    for e in events:
        t = float(e)
        raw += 1
        if not math.isfinite(t):
            raise ValueError(f"事件时间戳必须为有限数(小时序),当前遇到:{t!r}")
        times.append(t)
    return raw, sorted(set(times))


# ---------------------------------------------------------------------------
# (a) Kleinberg 爆发检测
# ---------------------------------------------------------------------------
def _solve_states(
    events: Iterable[float],
    *,
    s: float,
    gamma: float,
    levels: int,
    stats: dict | None,
) -> tuple[list[float], list[float], float, float, int, list[int], dict[str, float | int]] | None:
    """kleinberg_bursts / kleinberg_bursts_nested 共用的求解内核(模块私有)。

    参数校验 → 事件规范化 → Viterbi DP → 成本口径,返回
    ``(times, gap_lns, ln_base, ln_s, levels, state, info)``:

    - ``times`` —— 去重升序事件时刻;``gap_lns[k]`` —— 间隔 k 的对数;
    - ``ln_base`` / ``ln_s`` —— ln λ0 与 ln s(对数域速率组装材料);
    - ``state[k]`` —— 间隔 k 的最优状态(0 = 基态;一维状态路径,
      两个公开函数只在这条路径上做不同的区间切分,DP 与操作计数一致);
    - ``info`` —— 操作计数与成本口径(此时尚未含最终的 ``bursts`` 计数)。

    事件不足 2 个不同时刻时填好 stats 并返回 None(无间隔可解)。
    """
    if s <= 1:
        raise ValueError(f"速率缩放系数 s 必须大于 1(状态 q 速率为基态的 s^q 倍),当前为:{s}")
    if gamma <= 0:
        raise ValueError(f"爬升转移惩罚 gamma 必须为正数,当前为:{gamma}")
    if int(levels) != levels or levels < 2:
        raise ValueError(f"状态层数 levels 必须为不小于 2 的整数(基态 + 爆发态),当前为:{levels}")

    events_in, times = _validate_events(events)
    n = len(times)
    info: dict[str, float | int] = {
        "events_in": events_in,
        "events_used": n,
        "gaps": max(0, n - 1),
        "levels": int(levels),
        "dp_cells": 0,
        "dp_trans_evals": 0,
        "cost_base": 0.0,
        "cost_opt": 0.0,
        "bursts": 0,
    }
    if stats is not None:
        stats.clear()
        stats.update(info)
    if n < 2:
        return None

    m = n - 1
    lv = int(levels)
    ln_s = math.log(s)
    if math.log(n) + (lv - 1) * ln_s > _LN_MAX_EXP:
        raise ValueError(
            f"参数组合溢出:ln(n) + (levels-1)·ln s ≈ {math.log(n) + (lv - 1) * ln_s:.1f} "
            f"超过 float 指数域上限 {_LN_MAX_EXP},请减小 levels 或 s"
        )

    # 对数域速率:ln λq = q·ln s + (ln n − ln T)。先除后取对数会在
    # T 极大时把 n/T 压成次正规浮点甚至 0,这里全程不做那次除法。
    total = times[-1] - times[0]
    ln_base = math.log(n) - math.log(total)
    gap_lns = [math.log(times[i + 1] - times[i]) for i in range(m)]  # 去重保证 Δ > 0

    def _cost(q: int, i: int) -> float:
        """间隔 i 处于状态 q 的负对数似然:λq·Δ − ln λq(对数域组装)。"""
        return math.exp(q * ln_s + ln_base + gap_lns[i]) - (q * ln_s + ln_base)

    # ---- Viterbi DP:gaps × levels 个单元格,每格在 levels 个前驱里选优 ----
    def _rise(p: int, q: int) -> float:
        """状态 p → q 的转移代价:升档 γ·(q−p)·ln s,回落免费。"""
        return gamma * (q - p) * ln_s if q > p else 0.0

    prev = [gamma * q * ln_s + _cost(q, 0) for q in range(lv)]  # 首间隔自带 0→q 爬升
    back: list[list[int]] = [[-1] * lv]
    dp_cells = lv
    trans_evals = 0
    for i in range(1, m):
        cur = [0.0] * lv
        brow = [0] * lv
        for q in range(lv):
            best_p = 0
            best_v = prev[0] + _rise(0, q)
            trans_evals += 1
            for p in range(1, lv):
                cand = prev[p] + _rise(p, q)
                trans_evals += 1
                if cand < best_v:  # 严格小于:平局取状态号最小者,确定性
                    best_v, best_p = cand, p
            cur[q] = best_v + _cost(q, i)
            dp_cells += 1
            brow[q] = best_p
        prev = cur
        back.append(brow)

    # ---- 回溯最优状态路径(state[i] = 间隔 i 所处状态) ----
    state = [0] * m
    state[m - 1] = min(range(lv), key=lambda q: (prev[q], q))
    for i in range(m - 1, 0, -1):
        state[i - 1] = back[i][state[i]]

    # 成本口径:组合归一化常数 ln(n!) 对两条路径同加,权重差值中抵消;
    # float(factorial(n)) 在 n ≥ 171 溢出,lgamma 全程有限(防溢出)。
    ln_fact = math.lgamma(n + 1.0)
    cost_opt = prev[state[m - 1]] + ln_fact
    cost_base = sum(_cost(0, i) for i in range(m)) + ln_fact

    info.update(
        {
            "events_used": n,
            "gaps": m,
            "levels": lv,
            "dp_cells": dp_cells,
            "dp_trans_evals": trans_evals,
            "cost_base": cost_base,
            "cost_opt": cost_opt,
        }
    )
    if stats is not None:
        stats.update(info)
    return times, gap_lns, ln_base, ln_s, lv, state, info


def kleinberg_bursts(
    events: Iterable[float],
    *,
    s: float = DEFAULT_S,
    gamma: float = DEFAULT_GAMMA,
    levels: int = DEFAULT_LEVELS,
    stats: dict | None = None,
) -> list[dict]:
    """经典 Kleinberg 两状态(k 层可扩展)自动机 burst 检测,纯函数。

    输入 ``events`` 为事件时间戳(小时序;任意顺序、含重复均可,内部
    规范化为有限浮点去重升序),输出按 ``start`` 升序的 burst 区间列表,
    每项形如 ``{"start": float, "end": float, "level": int, "weight": float}``:

    - ``start`` / ``end`` —— 爆发段首 / 末事件时刻(小时,原输入量纲);
    - ``level`` —— 该段最高活跃层级(≥ 1;经典两状态恒为 1);
    - ``weight`` —— 该段相对基态的代价节省量(对数似然比,> 0;
      不含爬升转移代价,故最优解中每个 burst 的 weight 必 ≥ γ·level·ln s)。

    模型与算法(详见模块 docstring"模型备忘"):间隔 Δ 在状态 q 的代价
    ``λq·Δ − ln λq``,其中 ``λq = s^q·(n/T)``;升档转移代价
    ``γ·(q−p)·ln s``,回落免费;Viterbi DP 对 gaps×levels 个单元格求
    最优状态路径,burst = 路径上状态 ≥ 1 的极大连续段(**多层数据按
    "极大连续段取最高 level"合并为单区间**;需要原论文式层级嵌套区间
    树时用 :func:`kleinberg_bursts_nested`)。

    数值口径:全部在对数域组装(``ln λq = q·ln s + ln n − ln T``),
    λq·Δ 以 ``exp(ln λq + ln Δ)`` 计算(参数 ≤ ln n + (levels−1)·ln s,
    入口处校验不越 float 指数域);(s^q − 1) 用 :func:`math.expm1`;
    负对数似然的组合常数 ln(n!) 用 :func:`math.lgamma`(对基线与最优
    路径同加,权重差值中抵消)。因此对任意时间量纲**尺度不变**:
    全体时间戳同乘常数 c,输出区间随之缩放、level 与 weight 不变。

    :param events: 事件时间戳(小时;不足 2 个不同时刻 → 无 burst,返回 [])。
    :param s: 速率缩放系数,须 > 1。
    :param gamma: 爬升惩罚,须 > 0;调大可抑制"孤立短间隔"伪爆发。
    :param levels: 状态层数,须 ≥ 2(基态 0 + 至少一层爆发态)。
    :param stats: 可选出参 dict,填入操作计数与成本口径(见模块 docstring)。
    :raises ValueError: 参数越界或时间戳非有限时抛出(中文消息)。

    用法(叙述式示例,非 doctest)::

        dense = [float(i) for i in range(0, 100, 10)] + [300 + 0.1 * i for i in range(30)]
        bursts = kleinberg_bursts(dense)      # 恰覆盖 [300, 302.9] 密集段
        kleinberg_bursts([float(i) for i in range(100)])  # 均匀流 → []
    """
    solved = _solve_states(events, s=s, gamma=gamma, levels=levels, stats=stats)
    if solved is None:
        return []
    times, gap_lns, ln_base, ln_s, lv, state, info = solved
    m = len(state)

    # ---- 提取状态 ≥ 1 的极大连续段为 burst;weight 为相对基态的节省量 ----
    bursts: list[dict] = []
    i = 0
    while i < m:
        if state[i] < 1:
            i += 1
            continue
        j = i
        while j + 1 < m and state[j + 1] >= 1:
            j += 1
        # weight = Σ (cost(0,Δ) − cost(q,Δ)) = Σ [q·ln s − λ0·Δ·(s^q − 1)]
        # λ0·Δ·(s^q−1) 中的 (s^q−1) 用 expm1 保精度。
        weight = 0.0
        top = 0
        for k in range(i, j + 1):
            q = state[k]
            top = max(top, q)
            weight += q * ln_s - math.exp(ln_base + gap_lns[k]) * math.expm1(q * ln_s)
        bursts.append(
            {
                "start": times[i],
                "end": times[j + 1],
                "level": top,
                "weight": weight,
            }
        )
        i = j + 1

    info["bursts"] = len(bursts)
    if stats is not None:
        stats.update(info)
    return bursts


def kleinberg_bursts_nested(
    events: Iterable[float],
    *,
    s: float = DEFAULT_S,
    gamma: float = DEFAULT_GAMMA,
    levels: int = DEFAULT_LEVELS,
    stats: dict | None = None,
) -> list[dict]:
    """层级嵌套形态的 Kleinberg burst 检测(原论文式多 level 并列输出),纯函数。

    与 :func:`kleinberg_bursts` 消费**同一条** Viterbi 最优状态路径
    (参数校验、DP、操作计数、成本口径完全一致),但不做"极大连续段
    取最高 level"的扁平合并,而是按 level 阈值分层切分为树::

        [{"level": 1, "start": t0, "end": t1, "weight": w0, "children": [
            {"level": 2, "start": .., "end": .., "weight": .., "children": []},
        ]}, ...]

    - **嵌套性质**(Kleinberg 2002 论文层级结构的直接推论):最优状态
      路径上,"处于状态 ≥ q 的间隔集合"随 q 递增而单调收缩,故 level q
      的极大区间必然整个落在某个 level ≤ q−1 的区间内——子节点区间
      ⊆ 父节点区间(允许相等,如路径直接从基态跳到高层时父子同界);
      level 0 是无穷背景基态,**不输出**。
    - ``level`` —— 该节点对应的阈值层(顶层恒为 1,子层逐级 +1);
      扁平输出的 ``level``(极大连续段内最高状态)对应本树中以该节点
      为根的子树内最大 ``level``。
    - ``weight`` —— 该段相对**父层状态 q−1** 的提升节省量
      ``Σ [(q−t)·ln s − λ_{t−1}·Δ·(s^{q−t} − 1)]``(q 为各间隔实际
      状态);level 1 时即相对基态,与扁平输出的 weight **逐位一致**
      (同一公式、同一求和顺序)。每个节点 weight 必 ≥ γ·ln s > 0
      ——否则最优路径宁可整段留在父层,省下那笔爬升代价。
    - ``children`` —— 嵌套的下一层节点列表(按 ``start`` 升序)。

    退化关系:``levels=2``(经典两状态)时状态路径只含 0/1,树退化为
    无子节点的单层森林——剥掉 ``children`` 键后与 :func:`kleinberg_bursts`
    的输出**逐项相等**(含 weight 逐位相等)。

    数值口径与 :func:`kleinberg_bursts` 相同(对数域组装、expm1、
    lgamma、尺度不变、入口校验 float 指数域)。

    :param events: 事件时间戳(小时;不足 2 个不同时刻 → 返回 [])。
    :param s: 速率缩放系数,须 > 1。
    :param gamma: 爬升惩罚,须 > 0。
    :param levels: 状态层数,须 ≥ 2;树最深嵌套 levels−1 层。
    :param stats: 可选出参 dict:除 :func:`kleinberg_bursts` 的全部键外,
        ``bursts`` 为顶层一级区间数(与扁平输出区间数一致),另增
        ``nodes`` 为树形总节点数(含嵌套子层;无嵌套时 == bursts)。
    :raises ValueError: 参数越界或时间戳非有限时抛出(中文消息)。

    用法(叙述式示例,非 doctest)::

        background = [float(i) for i in range(720)]
        cluster = [360.0 + 0.5 * i / 47 for i in range(48)]
        tree = kleinberg_bursts_nested(background + cluster, levels=3)
        # tree == [{"level": 1, "start": 360.0, "end": 360.5, "weight": ..,
        #           "children": [{"level": 2, ..., "children": []}]}]
    """
    solved = _solve_states(events, s=s, gamma=gamma, levels=levels, stats=stats)
    if solved is None:
        return []
    times, gap_lns, ln_base, ln_s, lv, state, info = solved
    m = len(state)

    def _weight(i: int, j: int, t: int) -> float:
        """间隔 [i, j] 相对父层 t−1 的提升节省量(t=1 即相对基态,与扁平输出同口径)。"""
        ln_parent = (t - 1) * ln_s  # ln λ_{t−1} 中由层数贡献的部分
        total = 0.0
        for k in range(i, j + 1):
            r = state[k] - (t - 1)  # 相对父层的提升层数(≥ 1)
            total += r * ln_s - math.exp(ln_parent + ln_base + gap_lns[k]) * math.expm1(r * ln_s)
        return total

    def _build(i: int, j: int, t: int) -> dict:
        """把"状态 ≥ t"的极大连续段 [i, j] 建为 level t 节点(递归下钻)。"""
        children: list[dict] = []
        k = i
        while k <= j:
            if state[k] >= t + 1:
                h = k
                while h + 1 <= j and state[h + 1] >= t + 1:
                    h += 1
                children.append(_build(k, h, t + 1))
                k = h + 1
            else:
                k += 1
        return {
            "level": t,
            "start": times[i],
            "end": times[j + 1],
            "weight": _weight(i, j, t),
            "children": children,
        }

    forest: list[dict] = []
    i = 0
    while i < m:
        if state[i] >= 1:
            j = i
            while j + 1 < m and state[j + 1] >= 1:
                j += 1
            forest.append(_build(i, j, 1))
            i = j + 1
        else:
            i += 1

    nodes = 0
    stack = list(forest)
    while stack:
        node = stack.pop()
        nodes += 1
        stack.extend(node["children"])

    info["bursts"] = len(forest)  # 顶层一级区间数,与扁平输出的区间数一致
    info["nodes"] = nodes  # 树形总节点数(含嵌套子层)
    if stats is not None:
        stats.update(info)
    return forest


# ---------------------------------------------------------------------------
# (b) 当前爆发强度
# ---------------------------------------------------------------------------
def burst_factor(
    bursts: list[dict] | None,
    now: float,
    *,
    decay_halflife_h: float = DEFAULT_DECAY_HALFLIFE_H,
) -> float:
    """由 burst 区间列表与当前时刻计算"当前爆发强度",返回 [0, 1]。

    对每个 burst 取贡献度,输出最大者(多个 burst 并存时看最强的):

    - ``start ≤ now ≤ end``(正处于爆发段):贡献 = ``w / (w + 1)``,
      对 weight 单调递增、天然饱和于 1(weight=0 → 0);
    - ``now > end``(爆发已结束):按半衰期指数衰减
      ``w/(w+1) · 2^(−(now−end)/decay_halflife_h)``,指数为负,
      久远爆发自然下溢到 0.0,无溢出风险;
    - ``now < start``(爆发尚未开始,时间戳是历史):贡献 0。

    结果显式钳制在 [0, 1];``bursts`` 为 None / 空列表 → 0.0。
    纯函数:同一 (bursts, now, 半衰期) 恒得同一输出。

    :param bursts: :func:`kleinberg_bursts` 的输出(缺 ``weight`` 键按 0)。
    :param now: 当前时刻(小时,与 events 同量纲)。
    :param decay_halflife_h: 结束后强度的半衰期(小时),须 > 0。
    :raises ValueError: 半衰期非正或 now 非有限时抛出(中文消息)。
    """
    if decay_halflife_h <= 0:
        raise ValueError(
            f"衰减半衰期 decay_halflife_h 必须为正数(小时),当前为:{decay_halflife_h}"
        )
    now = float(now)
    if not math.isfinite(now):
        raise ValueError(f"当前时刻 now 必须为有限数(小时),当前为:{now!r}")

    best = 0.0
    for b in bursts or []:
        start = float(b["start"])
        end = float(b["end"])
        w = float(b.get("weight", 0.0))
        if w <= 0.0 or not math.isfinite(w):
            # 零权重无贡献;inf 权重直接饱和到 1(inf/(inf+1) 是 nan,须特判)
            strength = 1.0 if w == math.inf else 0.0
        else:
            strength = w / (w + 1.0)
        if now < start:
            contrib = 0.0
        elif now <= end:
            contrib = strength
        else:
            contrib = strength * math.exp(-_LN2 * (now - end) / decay_halflife_h)
        best = max(best, contrib)
    return min(1.0, max(0.0, best))


# ---------------------------------------------------------------------------
# (c) 建议 TTL
# ---------------------------------------------------------------------------
def suggest_ttl(
    base_ttl_h: float,
    factor: float,
    *,
    min_h: float = MIN_TTL_H,
    max_h: float = MAX_TTL_H,
) -> float:
    """由基准 TTL 与爆发强度给出"建议重扫 TTL"(小时),纯函数。

    公式(对 factor 单调不增,爆发期收缩、稳态不动)::

        ttl = clamp(base_ttl_h · (1 − 0.8 · factor), min_h, max_h)

    - ``factor = 0``(稳态 / 无爆发)→ ``base_ttl_h``,与既有默认行为
      完全一致(向后兼容);
    - ``factor = 1``(最强爆发)→ ``base_ttl_h · 0.2``,即最多收缩到
      基准的 1/5(收缩系数 :data:`TTL_SHRINK` = 0.8);
    - 结果钳制在 ``[min_h, max_h]``(默认 [6, 720] 小时,与
      ``ops.adaptive`` 的 ``MIN_INTERVAL_H / MAX_INTERVAL_H`` 同口径),
      防止极端配置把目标刷爆或彻底遗忘。

    单调性证明要点:``1 − 0.8·f`` 对 f 严格递减,乘正基数保持严格
    递减;对 [min_h, max_h] 的钳制是单调(非降)映射,复合后仍单调
    不增——factor 每 +ε,TTL 绝不增加。

    红线:本函数只给"指纹未变时跳过多久"的建议,**不绕过任何频控**;
    重扫频率仍受全局礼貌间隔与频控钳制约束(装配方负责)。

    :param base_ttl_h: 基准 TTL(小时),须 > 0(如 ``SiteMemory.ttl_hours``)。
    :param factor: 爆发强度([0, 1];越界值防御性钳制到 [0, 1])。
    :param min_h: 建议 TTL 下限(小时),须 > 0 且 ≤ max_h。
    :param max_h: 建议 TTL 上限(小时)。
    :raises ValueError: base_ttl_h ≤ 0 或上下限非法时抛出(中文消息)。
    """
    if base_ttl_h <= 0:
        raise ValueError(f"基准 TTL base_ttl_h 必须为正数(小时),当前为:{base_ttl_h}")
    if min_h <= 0 or max_h <= 0 or min_h > max_h:
        raise ValueError(
            f"建议 TTL 上下限必须为正且 min_h ≤ max_h,当前为:min_h={min_h}, max_h={max_h}"
        )
    factor = min(1.0, max(0.0, float(factor)))
    ttl = float(base_ttl_h) * (1.0 - TTL_SHRINK * factor)
    return min(float(max_h), max(float(min_h), ttl))


# ---------------------------------------------------------------------------
# (d) 半衰期 × 收缩系数网格标定(A207 交付后续:两参数原为经验缺省)
# ---------------------------------------------------------------------------
def calibrate_params(
    event_streams: Iterable[Iterable[float]],
    outcomes: Iterable[Iterable[float]],
    *,
    base_ttl_h: float = 72.0,
    half_lives: Iterable[float] = CALIB_DEFAULT_HALFLIVES_H,
    shrinks: Iterable[float] = CALIB_DEFAULT_SHRINKS,
    min_h: float = MIN_TTL_H,
    max_h: float = MAX_TTL_H,
    s: float = DEFAULT_S,
    gamma: float = DEFAULT_GAMMA,
    levels: int = DEFAULT_LEVELS,
) -> dict:
    """用真实巡查真值标定 ``decay_halflife_h`` 与 TTL 收缩系数,纯函数。

    背景::data:`DEFAULT_DECAY_HALFLIFE_H`(24h)与 :data:`TTL_SHRINK`
    (0.8)是经验缺省(A207 交付报告遗留)。本函数在给定网格(缺省
    half_life ∈ {6, 12, 24, 48, 96} 小时 × shrink ∈ {0.5, 0.8, 0.9})
    上做**回放仿真**,输出各组合的命中 / 浪费、帕累托前沿与中文建议,
    供运维据实取舍;无单一最优时诚实呈现权衡,不假装收敛。

    仿真模型(确定性,逐站点独立):

    1. 检测:对每条事件流跑 :func:`kleinberg_bursts`(参数 s/gamma/
       levels 透传)得到 burst 区间,全程不变(离线回放口径);
    2. 调度:仿真窗口 ``[t_lo, t_hi]`` = 全体事件与真值时点的最小 /
       最大时刻;视 ``t_lo`` 已发生一次基线巡查,此后滚动——在当前
       时刻 ``cur`` 取 ``factor = burst_factor(bursts, cur,
       decay_halflife_h=half_life)``,按 ``ttl = clamp(base·(1 −
       shrink·factor), min_h, max_h)``(与 :func:`suggest_ttl` 同公式、
       同钳制,shrink 参数化;shrink=0.8 时逐位一致)推进到下一次重扫,
       直至越过 ``t_hi``;每步的重扫时刻即该组合的"suggest_ttl 序列";
    3. **命中**(重扫及时性 = 真值前 TTL 已到):真值时点 g 被命中,
       当且仅当存在重扫时刻 r 落在上一个真值(首个则 t_lo)到 g 的
       左开右闭窗口 ``(anchor, g]`` 内——即该窗口内 TTL 到期过至少
       一次,变化不会被整段漏过;各真值的窗口互斥,故一个重扫时刻
       至多"接住"一个真值(命中数 ≤ 重扫数恒成立);
    4. **浪费**(无真值时的多余重扫):浪费 = 重扫总次数 − 命中数——
       没有接住任何真值的重扫都计入;**无真值的流只贡献浪费、不贡献
       命中**(诚实降级:此时所有组合命中为 0,只能按浪费取舍)。

    输出 dict(键全部确定性,重复调用逐项相等)::

        {"sites": 流数, "sites_scored": 可仿真流数(事件与真值非全空),
         "sites_without_truth": 无真值流数, "truths": 真值总数,
         "combos": 网格组合数, "grid": 行列表, "pareto": 前沿行列表,
         "advice": 中文建议}

    每行 ``{"half_life_h", "shrink", "hits", "truths", "hit_rate",
    "rescans", "waste", "rescan_times"}``,按 half_lives × shrinks 的
    传入顺序排列;``rescan_times`` 为逐站点的重扫时刻序列(审计用)。
    支配定义:a 支配 b ⟺ a 命中 ≥ b 命中且 a 浪费 ≤ b 浪费(至少一
    严格);帕累托前沿 = 不被任何行支配的行(保持网格顺序)。

    :param event_streams: 各站点事件时间戳序列(小时;与 kleinberg_bursts 同口径)。
    :param outcomes: 各站点"实际需要重扫"的真值时点(小时),与流一一对应。
    :param base_ttl_h: 基准 TTL(小时,缺省 72 = SiteMemory.ttl_hours 缺省)。
    :param half_lives: 半衰期网格(小时,全为正;缺省 5 档)。
    :param shrinks: 收缩系数网格(均 ∈ (0, 1);缺省 3 档)。
    :param min_h / max_h: 建议 TTL 钳制上下限(缺省 [6, 720],红线口径)。
    :param s / gamma / levels: 透传给 :func:`kleinberg_bursts` 的检测参数。
    :raises ValueError: 流数与真值数不匹配、网格为空 / 越界、TTL 参数
        非法、时间戳或真值非有限、仿真窗口与下限之比超安全上限时抛出
        (中文消息)。

    用法(叙述式示例,非 doctest)::

        report = calibrate_params([stream_a, stream_b], [[350.0], [900.0]])
        report["pareto"]   # 命中/浪费不被同时压过的参数组合
        report["advice"]   # 中文取舍建议(无单一最优时呈现两端权衡)
    """
    streams = [list(stream) for stream in event_streams]
    truth_lists = [list(truths) for truths in outcomes]
    if len(streams) != len(truth_lists):
        raise ValueError(
            f"event_streams 与 outcomes 必须一一对应(每条流配一组真值时点),"
            f"当前为 {len(streams)} 条流对 {len(truth_lists)} 组真值"
        )
    if base_ttl_h <= 0:
        raise ValueError(f"基准 TTL base_ttl_h 必须为正数(小时),当前为:{base_ttl_h}")
    if min_h <= 0 or max_h <= 0 or min_h > max_h:
        raise ValueError(
            f"建议 TTL 上下限必须为正且 min_h ≤ max_h,当前为:min_h={min_h}, max_h={max_h}"
        )
    hl_grid = [float(h) for h in half_lives]
    sh_grid = [float(x) for x in shrinks]
    if not hl_grid or not sh_grid:
        raise ValueError("标定网格不能为空:half_lives 与 shrinks 须各含至少一个候选值")
    for h in hl_grid:
        if not math.isfinite(h) or h <= 0:
            raise ValueError(f"半衰期网格候选值必须为正的有限数(小时),当前含:{h!r}")
    for x in sh_grid:
        if not math.isfinite(x) or not (0.0 < x < 1.0):
            raise ValueError(f"收缩系数网格候选值必须落在开区间 (0, 1),当前含:{x!r}")

    sites: list[tuple[list[dict], float, float, list[float]]] = []
    sites_without_truth = 0
    total_truths = 0
    for stream, raw_truths in zip(streams, truth_lists):
        for g in raw_truths:
            if not math.isfinite(float(g)):
                raise ValueError(f"真值时点必须为有限数(小时序),当前遇到:{float(g)!r}")
        _, times = _validate_events(stream)  # 复用检测器的事件规范化(非有限即抛中文异常)
        truths = sorted({float(g) for g in raw_truths})
        bursts = kleinberg_bursts(times, s=s, gamma=gamma, levels=levels)
        if not truths:
            sites_without_truth += 1
        total_truths += len(truths)
        if not times and not truths:
            continue  # 无任何时间点,无法仿真,诚实跳过(计入 sites 不计入 sites_scored)
        t_lo = min(times[0] if times else math.inf, truths[0] if truths else math.inf)
        t_hi = max(times[-1] if times else -math.inf, truths[-1] if truths else -math.inf)
        if (t_hi - t_lo) / float(min_h) > _CALIB_MAX_RESCANS:
            raise ValueError(
                f"仿真窗口与 TTL 下限之比超过安全上限 {_CALIB_MAX_RESCANS}"
                f"(窗口 {t_hi - t_lo:g} 小时 / 下限 {min_h:g} 小时),请缩小标定窗口"
            )
        sites.append((bursts, t_lo, t_hi, truths))

    base = float(base_ttl_h)
    rows: list[dict] = []
    for hl in hl_grid:
        for sh in sh_grid:
            hits = 0
            rescans_total = 0
            rescan_times: list[list[float]] = []
            for bursts, t_lo, t_hi, truths in sites:
                # 滚动调度:与 suggest_ttl 同公式同钳制(shrink 参数化)。
                schedule: list[float] = []
                cur = t_lo
                while True:
                    factor = burst_factor(bursts, cur, decay_halflife_h=hl)
                    ttl = min(float(max_h), max(float(min_h), base * (1.0 - sh * factor)))
                    nxt = cur + ttl
                    if nxt > t_hi:
                        break
                    schedule.append(nxt)
                    cur = nxt
                # 命中:窗口 (anchor, g] 内存在重扫时刻(窗口互斥 → 指针单调前移即可)。
                j = 0
                anchor = t_lo
                for g in truths:
                    while j < len(schedule) and schedule[j] <= anchor:
                        j += 1
                    if j < len(schedule) and schedule[j] <= g:
                        hits += 1
                    anchor = g
                rescans_total += len(schedule)
                rescan_times.append(schedule)
            rows.append(
                {
                    "half_life_h": hl,
                    "shrink": sh,
                    "hits": hits,
                    "truths": total_truths,
                    "hit_rate": (hits / total_truths) if total_truths else 0.0,
                    "rescans": rescans_total,
                    "waste": rescans_total - hits,
                    "rescan_times": rescan_times,
                }
            )

    pareto = [row for row in rows if not any(_row_dominates(other, row) for other in rows)]

    advice = _calibration_advice(rows, pareto, total_truths, len(streams), len(sites))
    return {
        "sites": len(streams),
        "sites_scored": len(sites),
        "sites_without_truth": sites_without_truth,
        "truths": total_truths,
        "combos": len(rows),
        "grid": rows,
        "pareto": [dict(row) for row in pareto],
        "advice": advice,
    }


def _calibration_advice(
    rows: list[dict],
    pareto: list[dict],
    total_truths: int,
    n_streams: int,
    n_scored: int,
) -> str:
    """由网格与前沿生成中文取舍建议(模块私有;确定性,无墙钟、无随机)。"""
    if n_scored == 0:
        return (
            f"诚实降级:{n_streams} 条事件流均无任何时间点,网格各行命中与浪费皆为 0,"
            "无法标定;请提供带时间戳的事件流(或真值时点)后重试。"
        )
    if total_truths == 0:
        min_waste = min(row["waste"] for row in rows)
        return (
            f"诚实降级:{n_streams} 条事件流均无真值时点,全部 {len(rows)} 组参数命中数为 0,"
            f"无法标定重扫及时性,只能按浪费预算取舍(浪费最小 {min_waste} 次,帕累托前沿即"
            "浪费最小的全部组合)。建议保持缺省组合(半衰期 24 小时、收缩 0.8),"
            "先积累真实巡查真值再回来标定。"
        )
    default_row = next(
        (
            row
            for row in rows
            if row["half_life_h"] == DEFAULT_DECAY_HALFLIFE_H and row["shrink"] == TTL_SHRINK
        ),
        None,
    )
    if default_row is not None:
        on_front = any(
            row["half_life_h"] == default_row["half_life_h"] and row["shrink"] == default_row["shrink"]
            for row in pareto
        )
        if on_front:
            default_note = "缺省组合(半衰期 24 小时、收缩 0.8)本身在帕累托前沿上,可暂不调整。"
        else:
            better = next(other for other in pareto if _row_dominates(other, default_row))
            default_note = (
                "缺省组合(半衰期 24 小时、收缩 0.8)已被支配:如改用半衰期 "
                f"{better['half_life_h']:g} 小时、收缩 {better['shrink']:g},命中 "
                f"{better['hits']}/{total_truths}、浪费 {better['waste']} 次"
                f"(缺省为 {default_row['hits']}/{total_truths}、{default_row['waste']} 次)。"
            )
    else:
        default_note = "当前网格未覆盖缺省组合(半衰期 24 小时、收缩 0.8),未评估其位置。"
    if len(pareto) == 1:
        only = pareto[0]
        return (
            f"唯一帕累托最优:半衰期 {only['half_life_h']:g} 小时、收缩 {only['shrink']:g}"
            f"(命中 {only['hits']}/{total_truths}、重扫 {only['rescans']} 次、浪费 "
            f"{only['waste']} 次),建议采用。{default_note}"
        )
    best_hits = max(row["hits"] for row in rows)
    row_best = next(row for row in rows if row["hits"] == best_hits)
    min_waste = min(row["waste"] for row in pareto)
    row_lean = next(row for row in pareto if row["waste"] == min_waste)
    return (
        f"无单一最优:帕累托前沿共 {len(pareto)} 组,请在及时发现与巡查预算之间取舍——"
        f"命中最高 {best_hits}/{total_truths}(半衰期 {row_best['half_life_h']:g} 小时、"
        f"收缩 {row_best['shrink']:g},浪费 {row_best['waste']} 次);前沿上浪费最低 "
        f"{min_waste} 次(半衰期 {row_lean['half_life_h']:g} 小时、收缩 "
        f"{row_lean['shrink']:g},命中 {row_lean['hits']}/{total_truths})。{default_note}"
    )


def _row_dominates(a: dict, b: dict) -> bool:
    """行支配判定(a 命中 ≥ b 且浪费 ≤ b,至少一项严格)——帕累托口径。"""
    return (a["hits"] >= b["hits"] and a["waste"] <= b["waste"]) and (
        a["hits"] > b["hits"] or a["waste"] < b["waste"]
    )


# ---------------------------------------------------------------------------
# 离线自检(A138 基准总控调用;红线 31:操作计数,不依赖墙钟)
# ---------------------------------------------------------------------------
def kernel_selfcheck() -> dict[str, object]:
    """确定性微基准:均匀流无爆发 + 密集簇定位 + 操作计数不变量。

    场景一(均匀流):720 个整点事件 → 必须零 burst(λ0·Δ ≈ n/(n−1) > 1,
    升档每间隔净亏);操作计数 ``dp_cells == gaps × levels``。

    场景二(密集簇):同一均匀流在 t∈[360, 360.5] 注入 48 事件后,
    必须恰有一个 burst 覆盖密集段(level ≥ 1、weight > 0、区间与
    [360, 360.5] 相交且远窄于全窗);两次调用输出逐项相等(离线可
    复现)。``value`` / ``baseline`` 为 DP 单元格评估数及其理论值
    gaps × levels(线性复杂度自证,穷举口径为 levels^gaps)。
    """
    uniform = [float(i) for i in range(720)]
    stats_u: dict[str, int | float] = {}
    assert kleinberg_bursts(uniform, stats=stats_u) == [], "均匀流不得出现 burst(自查)"
    assert stats_u["dp_cells"] == stats_u["gaps"] * stats_u["levels"], "dp_cells 应恰为 gaps×levels(自查)"

    cluster = [360.0 + 0.5 * i / 47 for i in range(48)]  # 48 事件挤进 0.5 小时
    mixed = sorted(uniform + cluster)
    stats_m: dict[str, int | float] = {}
    bursts = kleinberg_bursts(mixed, stats=stats_m)
    assert len(bursts) == 1, f"密集簇应恰产生 1 个 burst,得到 {len(bursts)} 个(自查)"
    only = bursts[0]
    assert only["level"] >= 1 and only["weight"] > 0.0, "burst 层级 ≥ 1 且权重为正(自查)"
    assert only["start"] <= 360.5 and only["end"] >= 360.0, "burst 区间必须覆盖密集段(自查)"
    assert only["end"] - only["start"] < 30.0, "burst 区间应远窄于 720 小时全窗(自查)"
    assert kleinberg_bursts(mixed) == bursts, "同输入两次调用输出必须逐项相等(自查)"
    assert stats_m["dp_cells"] == stats_m["gaps"] * stats_m["levels"], "操作计数不变量(自查)"

    return {
        "name": "temporal.kleinberg",
        "metric": (
            f"Viterbi DP 单元格评估数(理论 = gaps × levels;"
            f"穷举口径 = levels^gaps = 2^{stats_m['gaps']})"
        ),
        "value": int(stats_m["dp_cells"]),
        "baseline": int(stats_m["gaps"] * stats_m["levels"]),
        "uniform_stream_cells": int(stats_u["dp_cells"]),
    }
