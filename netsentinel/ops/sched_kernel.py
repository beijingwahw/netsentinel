"""NetSentinel(净网哨兵)调度内核·优先级预算(netsentinel.ops.sched_kernel,A130)。

核心思想:**一轮巡查的预算是有限的,把它花在最值得扫的目标上**——
把候选站点的"波动度 / URL 风险 / 陈旧度"三因子加权成一个 0~1 优先级,
再按**价值密度**(优先级 ÷ 逐项成本)降序做变成本背包(0/1 背包的
贪心近似),在预算内装入尽可能多的高价值目标;可选的 **aging 因子**
(默认权重 0,对标实时调度 EDF+aging 防饿死理论)让久未被选中的老
站点逐步抬升优先级,杜绝"低分站点永远排不上队"的饥饿。

两个入口(签名见 CONTRACTS-V7.md §2 A130,均为纯函数、零 IO、确定性):

- :func:`priority`:单项因子加权打分(缺字段按 0;``weights`` 可配,
  默认与三因子旧口径**位级等价**——aging 权重 0 时浮点结果一字不差);
- :func:`select_round`:一轮预算选取(价值密度贪心 + 逐项成本背包,
  可观测性计数)。

因子口径(与既有模块只读对齐,不重复实现):

============ ========================= ========================== ==================
因子          取值来源                   归一化                     权重常量
============ ========================= ========================== ==================
volatility   ops.adaptive.volatility   [0, 1] 原样(越界钳制)      W_VOLATILITY=0.45
url_risk     intel.url_intel 的 risk   [0, 1] 原样(越界钳制)      W_RISK=0.35
staleness_h  距上次扫描的小时数         min(h/168, 1)(一周封顶)  W_STALENESS=0.20
age_h        距上次**被选中巡查**的小时  min(h/168, 1)(一周封顶)  W_AGING=0.0(默认关)
============ ========================= ========================== ==================

权重设计说明:波动度权重最高(指纹常变的站点最可能滋生新违规内容),
URL 风险次之(静态可疑特征预示危害概率),陈旧度最低但不可为零
(长期未扫的目标需要兜底覆盖,168 小时=一周即视为"完全陈旧")。
**aging(V10 新增,EDF+aging 防饿死)**:staleness 度量"内容多久没扫",
aging 度量"调度队列里饿了多久"——两者口径不同,故单列第四因子而非
并入 staleness(避免双重计费)。默认权重 0 完全不参与打分(与旧三因子
位级等价,旧调用方零感知);运维侧经 ``weights={"aging": w}`` 显式开启
后,老站点随 age_h 单调抬升、一周封顶,同等条件下老目标不落后于
新目标(防饿死),打分末端统一钳制回 [0, 1]。

预算语义(变成本背包,V10 已激活原"等成本"扩展点):

- 每个候选的逐项成本 ``cost = 站点页数 pages × min_interval_s``
  (缺 ``pages`` 键 / 脏值按 1 页 → 成本恒为 ``min_interval_s``,即
  等成本旧口径,作为退化情形**逐项保持旧结果**);
- 选取 = 贪心 0/1 背包近似:按价值密度 ``priority / cost`` 降序尝试,
  装不下当前项就**跳过继续**试更便宜的项(而非提前终止);
- 零成本项(``pages=0``,如单 URL 观察项)密度视为 ``+inf``,恒可白拿;
  无穷 / 超大成本项任何有限预算都装不下,自然被跳过;
- 预算硬上界:选中项成本之和 ≤ budget + 每项 1e-9 的浮点容差
  (防御 0.3/0.1 类二进制表示误差,与旧口径一致);
- 预算连一项都装不下(含 0 与负数;零成本项除外)→ 返回空。

返回顺序:**贪心决策序**(价值密度降序;平局按 priority 降序,再保持
items 原顺序,即稳定排序)。成本恒定时该序恰为 priority 降序,与旧
口径逐位一致。返回的是原 dict 对象的引用,不复制、不改动调用方数据。

安全与红线:

- 纯函数、零 IO:不打网络、不读文件、不碰时钟——同一输入永远同一输出
  (红线 29:纯新增文件,既有调用方零感知;由 cfg.sched_priority 开关
  接入,默认关闭时行为与旧调度完全一致);
- 可观测性:每次成功的一轮选取决策累加 ``telemetry.inc("sched_kernel.round")``
  (只计数,不存 URL 内容,红线 17/23;参数非法抛错不计)。

用法示例::

    >>> from netsentinel.ops.sched_kernel import priority, select_round
    >>> priority({"volatility": 0.5, "url_risk": 0.5, "staleness_h": 84})
    0.5
    >>> priority({"age_h": 500})  # aging 默认权重 0 → 不参与打分
    0.0
    >>> items = [
    ...     {"url": "a", "volatility": 1.0},
    ...     {"url": "b", "volatility": 0.2},
    ...     {"url": "c", "url_risk": 1.0},
    ...     {"url": "d"},
    ...     {"url": "e", "staleness_h": 168},
    ... ]
    >>> picked = select_round(items, 4.0)      # 预算 4 秒、间隔 1 秒 → 选 4 项
    >>> [it["url"] for it in picked]           # 按 priority 降序(0.45/0.35/0.20/0.09)
    ['a', 'c', 'e', 'b']
    >>> big = {"url": "big", "volatility": 1.0, "url_risk": 1.0,
    ...        "staleness_h": 168, "pages": 3}     # 满分但 3 页成本,预算 2 装不下
    >>> free = {"url": "b", "volatility": 0.2, "pages": 0}  # 零成本白拿
    >>> [it["url"] for it in select_round([big, free], 2.0)]
    ['b']
"""
from __future__ import annotations

import math
from typing import Any, Iterable

from netsentinel import telemetry

__all__ = [
    "W_VOLATILITY",
    "W_RISK",
    "W_STALENESS",
    "W_AGING",
    "STALENESS_WINDOW_H",
    "AGING_WINDOW_H",
    "priority",
    "select_round",
    "kernel_selfcheck",
]

#: 波动度权重:指纹变化越频繁越优先(因子来自 ops.adaptive.volatility)
W_VOLATILITY = 0.45

#: URL 风险权重(因子来自 intel.url_intel.url_features 返回的 risk)
W_RISK = 0.35

#: 陈旧度权重:距上次扫描的时间越久越优先
W_STALENESS = 0.20

#: 陈旧度归一化窗口(小时):168h = 7 天,超过一周按"完全陈旧"计 1.0
STALENESS_WINDOW_H = 168

#: aging(防饿死)因子权重:**默认 0.0 = 完全关闭**——打分与 V7 三因子
#: 口径位级等价(0.0 × 任何因子贡献恰为 0.0,浮点结果一字不差);
#: 经 priority/select_round 的 ``weights={"aging": w}`` 显式开启
W_AGING = 0.0

#: aging 归一化窗口(小时):168h = 7 天,距上次被选中巡查超过一周按
#: "饿满"计 1.0 封顶(与 staleness 同窗,但口径独立、不重复计费)
AGING_WINDOW_H = 168

#: 浮点防御误差(如 0.3/0.1 的二进制表示为 2.999...):预算比较时容忍该量级
_EPS = 1e-9


# ---------------------------------------------------------------------------
# 因子规范化
# ---------------------------------------------------------------------------
def _factor01(value: Any) -> float:
    """把任意输入规范成 ``[0, 1]`` 内的浮点因子(脏数据防御)。

    - ``None`` / 缺失 / 无法转 float 的值 → ``0.0``(宁可低估,不抛错);
    - ``NaN`` → ``0.0``;``+inf`` → ``1.0``、``-inf`` → ``0.0``(经钳制);
    - 越界值(如 2.0 / -0.5)一律钳制回 ``[0, 1]``。
    """
    if value is None:
        return 0.0
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(num):
        return 0.0
    return min(1.0, max(0.0, num))


def _as_float_or_zero(value: Any) -> float:
    """staleness_h / age_h 专用预处理:小时数(允许负/超大),脏值按 0,再交 :func:`_factor01` 钳制。"""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


#: 可配权重的四因子键名(与因子字段的对应:volatility / url_risk /
#: staleness←staleness_h / aging←age_h);未知键一律忽略(宁缺勿错)
_WEIGHT_KEYS = ("volatility", "url_risk", "staleness", "aging")

#: 默认权重表:aging 默认 0(V7 三因子语义原样,位级等价)
_DEFAULT_WEIGHTS: dict[str, float] = {
    "volatility": W_VOLATILITY,
    "url_risk": W_RISK,
    "staleness": W_STALENESS,
    "aging": W_AGING,
}


def _norm_weights(weights: Any) -> dict[str, float]:
    """规范化权重覆盖:``None`` → 默认四因子;mapping → 逐键覆盖,脏值防御。

    - 只认 :data:`_WEIGHT_KEYS` 四键,未知键忽略;
    - 每个权重逐经 :func:`_factor01` 规范:非数 / NaN → 0,钳制 [0, 1]
      (负权重会破坏 aging 单调性,>1 的超配由 :func:`priority` 末端的
      [0, 1] 钳制兜底);
    - 非 mapping 的脏输入(如 ``weights=42``)→ 整体按默认权重。
    """
    if weights is None:
        return dict(_DEFAULT_WEIGHTS)
    merged = dict(_DEFAULT_WEIGHTS)
    for key in _WEIGHT_KEYS:
        try:
            if key in weights:
                merged[key] = _factor01(weights[key])
        except TypeError:
            return dict(_DEFAULT_WEIGHTS)
    return merged


def _page_count(item: dict) -> float:
    """逐项成本基数:站点页数(变成本背包的成本因子,单位 = 页)。

    - 缺 ``pages`` 键 / ``None`` / 非数 / NaN → ``1.0``(不了解规模的
      站点按"普通一页"计,恰好退化为等成本旧口径);
    - ``pages <= 0``(0 页或负数脏值)→ ``0.0``(零成本:单 URL 观察
      项白拿,不占预算);
    - ``+inf`` / 超大页数原样保留(成本无穷大,任何有限预算装不下)。
    """
    raw = item.get("pages")
    if raw is None:
        return 1.0
    try:
        pages = float(raw)
    except (TypeError, ValueError):
        return 1.0
    if math.isnan(pages):
        return 1.0
    if pages <= 0.0:
        return 0.0
    return pages


# ---------------------------------------------------------------------------
# 优先级打分
# ---------------------------------------------------------------------------
def priority(item: dict, *, weights: dict | None = None) -> float:
    """计算单个候选目标的巡查优先级,返回 ``[0, 1]`` 内的浮点数。

    四因子加权(aging 默认权重 0,即 V7 三因子口径)::

        priority = W_VOLATILITY * volatility
                 + W_RISK       * url_risk
                 + W_STALENESS  * min(staleness_h / 168, 1)
                 + W_AGING      * min(age_h / 168, 1)      # 默认 0,可配

    ``item`` 约定结构(全部因子字段可缺省)::

        {
            "url":         str,    # 仅透传,不参与打分
            "volatility":  float,  # 0~1,缺省按 0
            "url_risk":    float,  # 0~1,缺省按 0
            "staleness_h": float,  # 距上次扫描的小时数,缺省按 0
            "age_h":       float,  # 距上次被选中巡查的小时数(V10,缺省按 0)
            "pages":       float,  # 站点页数(仅作成本,不参与打分,V10)
        }

    - 缺字段 / None / 非数 → 该因子按 0(与 :func:`_factor01` 一致);
    - 越界因子钳制到 ``[0, 1]``,陈旧度 / aging 各自在 168 小时处封顶;
    - 三因子全为 0 → 0.0,全为 1 → 1.0(默认权重和恰为 1);
    - ``weights``(V10,可配权重):``{"volatility": w1, "url_risk": w2,
      "staleness": w3, "aging": w4}`` 逐键覆盖默认常量,未提及的键保持
      默认;``None`` / 脏 mapping 按全默认(见 :func:`_norm_weights`)。
      **默认(None)时 aging 权重为 0,任取 age_h 打分与旧三因子口径
      位级相等**(0.0 × 因子贡献恰为 +0.0,浮点结果一字不差);
    - aging 开启后随 age_h **单调不减**(防饿死:老目标不落后于新目标),
      打分末端统一钳制回 [0, 1](自定义权重和 > 1 时兜底)。

    :param item: 候选目标描述 dict(url / pages 键存在与否不影响分数)。
    :param weights: 可选的四因子权重覆盖(键见 :data:`_WEIGHT_KEYS`)。
    """
    w = _norm_weights(weights)
    vol = _factor01(item.get("volatility"))
    risk = _factor01(item.get("url_risk"))
    stale = _factor01(_as_float_or_zero(item.get("staleness_h")) / STALENESS_WINDOW_H)
    age = _factor01(_as_float_or_zero(item.get("age_h")) / AGING_WINDOW_H)
    score = (
        w["volatility"] * vol
        + w["url_risk"] * risk
        + w["staleness"] * stale
        + w["aging"] * age
    )
    return min(1.0, score)


# ---------------------------------------------------------------------------
# 一轮预算选取
# ---------------------------------------------------------------------------
def select_round(
    items: Iterable[dict],
    budget: float,
    *,
    min_interval_s: float = 1.0,
    weights: dict | None = None,
) -> list[dict]:
    """在预算内按价值密度降序贪心选取本轮要巡查的目标(纯函数,零 IO)。

    变成本 0/1 背包近似(V10 已激活原等成本扩展点):每个候选的
    逐项成本 ``cost = 站点页数 pages × min_interval_s``(缺 ``pages`` /
    脏值按 1 页,即等成本旧口径的退化情形),价值 = :func:`priority`
    (可经 ``weights`` 启用 aging 防饿死因子)。按 ``价值 ÷ 成本`` 密度
    降序贪心装入,装不下当前项就**跳过继续**试更便宜的项;等成本下
    密度序 ≡ priority 降序,且"装不下"对后续全部成立,故**成本恒定时
    与旧实现逐项一致**(跳过与提前终止等价)。

    行为细节:

    - 返回顺序:贪心决策序(价值密度降序;平局按 priority 降序,再保持
      ``items`` 原顺序,即稳定排序;成本恒定时恰为 priority 降序);
    - 返回原 dict 对象引用,不复制、不改动输入(包括输入列表本身);
    - ``items`` 为空或 ``None`` → 空列表;
    - ``pages=0``(零页 / 负数脏值钳 0)→ 零成本恒可入选(密度 +inf),
      连零预算也白拿;负预算则一分不花(全跳过);
    - 超大 / ``inf`` 页数 → 成本无穷大,任何有限预算都装不下,被跳过;
    - 预算硬上界:选中项成本之和 ≤ budget + 每项 ``1e-9`` 浮点容差;
    - ``budget`` 为 0 / 负 / 小于最便宜正成本项 →(零成本项除外)空列表;
    - ``min_interval_s <= 0`` 属非法配置(成本为零会装下一切)→ 抛
      ``ValueError``(中文消息),且不累加遥测;
    - 每次成功决策(含空选取)累加 ``telemetry.inc("sched_kernel.round")``。

    复杂度:单遍打分 + 计成本 ``O(n)``、排序 ``O(n log n)``、单遍装入
    ``O(n)``;每项恰一次 :func:`priority` 调用与一次成本计算——预算、
    页数、权重变化均不触发重扫(操作计数不变量)。

    :param items: 候选目标列表(元素为 :func:`priority` 约定的 dict);
    :param budget: 本轮预算(单位与 ``min_interval_s`` 一致,演示口径为秒);
    :param min_interval_s: 相邻两次扫描的礼貌间隔(= 单页站点的成本),须 > 0;
    :param weights: 可选四因子权重覆盖(透传 :func:`priority`,
        如 ``{"aging": 0.15}`` 开启防饿死;默认 None 即旧三因子口径)。
    :raises ValueError: ``min_interval_s <= 0`` 时抛出(中文消息)。
    """
    if min_interval_s <= 0:
        raise ValueError(
            f"礼貌间隔 min_interval_s 必须为正数(秒),当前为:{min_interval_s}"
        )
    telemetry.inc("sched_kernel.round")  # 每轮决策计数(空轮也算一轮决策)
    if items is None:  # type: ignore[unreachable]
        return []
    w = _norm_weights(weights)
    # 单遍打分 + 计成本(操作计数不变量:每项恰一次 priority,与预算无关;
    # weights 为 None 时走原生的 priority 引用,保持旧调用路径零感知)
    score_of = priority if weights is None else (lambda it: priority(it, weights=w))
    scored: list[tuple[float, float, dict]] = []
    for it in items:
        scored.append((score_of(it), _page_count(it) * min_interval_s, it))

    def _rank_key(entry: tuple[float, float, dict]) -> tuple[float, float]:
        value, cost, _item = entry
        # 零成本项密度 +inf(白拿);次键 value:成本恒定时密度是 priority 的
        # 单调不变换(除法只可能把严格序压成平局,不会倒置),平局回退
        # value 比较 → 排序结果与旧"priority 降序"逐位一致
        return (value / cost if cost > 0.0 else math.inf, value)

    scored.sort(key=_rank_key, reverse=True)  # 稳定:平局保持 items 原顺序
    remaining = float(budget)
    picked: list[dict] = []
    for _value, cost, it in scored:
        if remaining + _EPS < cost:
            continue  # 装不下:跳过继续试更便宜的(等成本下与提前终止等价)
        picked.append(it)
        remaining -= cost
    return picked


# ---------------------------------------------------------------------------
# V7 内核自检(供 benchmarks/kernel_bench.py,A138 统一调用)
# ---------------------------------------------------------------------------
def kernel_selfcheck() -> dict[str, Any]:
    """确定性自检:10 项候选、预算 4.0、间隔 1.0 → 恰选优先级最高的 4 项。

    零 IO、零时钟:固定构造数据上比对精确结果,供 A138 基准总控汇总。
    主四键(``name / metric / value / baseline``)口径与 V7 完全一致;
    V10 升级另附两个确定性附加键(总控收入 ``extra`` 留档,不进四键):

    - ``varcost_degenerate_top4``:同批数据逐项 ``pages=1``(成本恒 =
      min_interval_s)时,变成本路径选中间数与等成本旧口径一致(= 4);
    - ``aging_default_weight_equivalent``:aging 权重默认 0 时,任取
      age_h(含封顶前后与超窗值)的打分与三因子旧口径**位级相等**
      (浮点精确 ``==``,非近似)——四舍六入的"零感知升级"铁证。
    """
    items = [
        {
            "url": f"https://site{i}.example",
            "volatility": (i % 10) / 10,
            "url_risk": ((9 - i) % 10) / 10,
            "staleness_h": i * 24,
        }
        for i in range(10)
    ]
    picked = select_round(items, 4.0)
    varcost_picked = select_round([dict(it, pages=1) for it in items], 4.0)
    ages = (0, 1, 24, 84, 167, 168, 169, 336, 500, 9999)
    aging_equivalent = all(
        priority(dict(it, age_h=age)) == priority(it) for it, age in zip(items, ages)
    )
    return {
        "name": "sched_kernel",
        "metric": "top4_of_10_selected",
        "value": len(picked),
        "baseline": 4,
        "varcost_degenerate_top4": len(varcost_picked),
        "aging_default_weight_equivalent": aging_equivalent,
    }
