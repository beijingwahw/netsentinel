"""决策论复核分诊(triage)—— 有限复核人力按"信息价值"分配(NetSentinel)。

对标 human-in-the-loop 效率最优分配 / review prioritization:审核人力是稀缺
资源,不应按 FIFO 盲目排队,而应优先看"单位注意力收益"最高的条目。本模块
把信息价值拆成四个可解释分量做线性加权::

    priority = w1·p(NSFW)           # 信号强度:站点聚合色情概率
             + w2·危害权重          # 判定档位:nsfw > suspect > clean
             + w3·估计翻案概率      # 不确定性:某档位历史上人工与机器
                                    # 分歧越大,人工看它获得的信息越多
                                    # (value-of-information,在线贝叶斯)
             + w4·老化因子          # 公平性/SLA:min(age/max_age, 1),
                                    # 防止长尾饿死

翻案概率估计器(OverturnStats):从复核历史在线估计 P(人工驳回 | verdict),
冷启动采用均匀先验 Beta(1,1)(后验均值 = (驳回数+1)/(已定案数+2),无历史时
恰为 0.5);数据源既可以是条目对象列表(from_history),也可以是现有 SQLite
库的只读聚合(from_sqlite,不改任何 schema);某档位无历史时回退全局经验。

安全红线:本模块**只影响列表返回顺序**(sort_entries 是纯函数),绝不改变
A10 状态机(pending → approved/rejected → submitted)、四眼复核、人工门、
验证码或频控的任何语义。默认权重"温和"(分量和为 1,排序温和重排而非激进
全反转);全部权重可配,可按需关闭任一分量。

可选提权加项(``sort_entries(boost_by_url=...)``,默认 None = 现状):在四
分量优先级之上再加一个 site_url → 非负数值的加项(典型来源:decision.abstain
的分歧弃权提示 ``priority_weight``——机器说"这题我不答"的站点经此提前看)。
加项只进排序键,**不改** :func:`priority` 的四分量公式本身;对同一批条目,
加项越大排序位置越靠前(单调),取 0 等价于不加。

独立模块:仅依赖标准库,不 import 任何 netsentinel 兄弟模块(避免循环依赖),
对条目做鸭子类型访问(verdict / status / created_at / site_url / id)。
"""
from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

#: 列表排序方式标识(与 ReviewQueue.list 的 sort 参数对接)。
SORT_FIFO = "fifo"
SORT_TRIAGE = "triage"

#: 老化因子的基准时长(秒,默认 72 小时):age ≥ max_age 时老化分量饱和为 1。
DEFAULT_MAX_AGE_SECONDS: float = 72 * 3600.0

#: 判定档位 → 冷启动 p(NSFW) 代理。entries 表按红线不改 schema,未存
#: agg_nsw_prob;上层若掌握真实概率,可通过 priority(p_nsfw=...) /
#: sort_entries(p_nsfw_by_url=...) 注入覆盖,未注入时回落到本代理。
VERDICT_P_NSFW_PROXY: dict[str, float] = {"nsfw": 0.9, "suspect": 0.5, "clean": 0.0}

#: 判定档位 → 危害权重(nsfw > suspect > clean,单调阶梯)。
VERDICT_HARM: dict[str, float] = {"nsfw": 1.0, "suspect": 0.5, "clean": 0.0}

#: 已定案状态:进入这些状态即计入翻案估计样本(rejected=翻案,其余=维持)。
RESOLVED_STATUSES: tuple[str, ...] = ("rejected", "approved", "submitted")

#: 翻案状态:人工驳回机器初筛(reject)视为一次"翻案"。
OVERTURN_STATUS = "rejected"

__all__ = [
    "DEFAULT_MAX_AGE_SECONDS",
    "DEFAULT_WEIGHTS",
    "OVERTURN_STATUS",
    "OverturnStats",
    "RESOLVED_STATUSES",
    "SORT_FIFO",
    "SORT_TRIAGE",
    "TriageWeights",
    "VERDICT_HARM",
    "VERDICT_P_NSFW_PROXY",
    "aging_factor",
    "priority",
    "sort_entries",
]


@dataclass(frozen=True)
class TriageWeights:
    """四分量权重(可配、默认温和:0.4+0.3+0.1+0.2 = 1.0)。

    全部必须为有限数值;置 0 可关闭对应分量(例如只按老化排序)。
    负值语义上允许(如优先看"不太可能翻案"的条目),由调用者自担。
    """

    w_p_nsfw: float = 0.4
    w_harm: float = 0.3
    w_overturn: float = 0.1
    w_age: float = 0.2

    def __post_init__(self) -> None:
        for name in ("w_p_nsfw", "w_harm", "w_overturn", "w_age"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"分诊权重必须是数值:{name}={value!r}")
            if not math.isfinite(float(value)):
                raise ValueError(f"分诊权重必须是有限数值:{name}={value!r}")


#: 默认温和权重(排序温和重排:单分量最多贡献 0.4,不会全反转 FIFO)。
DEFAULT_WEIGHTS = TriageWeights()


@dataclass(frozen=True)
class OverturnStats:
    """翻案概率在线估计器(Beta(1,1) 均匀先验的贝叶斯后验)。

    ``overturns_by_verdict`` / ``resolved_by_verdict`` 按判定档位聚合计数;
    ``overturn_prob(verdict)`` 返回后验均值 ``(翻案数+1)/(已定案数+2)``:

    * 冷启动(无任何历史)→ 0.5(均匀先验均值);
    * 该档位无历史但全局有 → 回退全局后验(层级收缩的轻量版);
    * 只统计已定案条目(rejected/approved/submitted),pending 不入样本。
    """

    overturns_by_verdict: Mapping[str, int] = field(default_factory=dict)
    resolved_by_verdict: Mapping[str, int] = field(default_factory=dict)

    # -- 构造 ---------------------------------------------------------------

    @classmethod
    def from_history(cls, entries: Iterable[Any]) -> OverturnStats:
        """从条目对象列表聚合(鸭子类型:status / verdict 字段)。

        只读输入,不修改任何条目;pending(未定案)不计入样本。
        """
        weighted: list[tuple[str, str, int]] = [
            (str(getattr(e, "verdict", "") or ""), str(getattr(e, "status", "") or ""), 1)
            for e in entries
        ]
        return cls._from_weighted(weighted)

    @classmethod
    def from_sqlite(cls, conn: sqlite3.Connection) -> OverturnStats:
        """从现有 SQLite 库只读聚合(不改 schema;entries 表结构不便或
        表不存在时安全回落冷启动均匀先验)。"""
        try:
            rows = conn.execute(
                "SELECT verdict, status, COUNT(*) FROM entries"
                " GROUP BY verdict, status"
            ).fetchall()
        except sqlite3.Error:
            logger.debug("翻案统计聚合失败,回落冷启动均匀先验", exc_info=True)
            return cls()
        # sqlite3.Row 与普通 tuple 都可按位置索引。
        return cls._from_weighted(
            (str(r[0] or ""), str(r[1] or ""), int(r[2])) for r in rows
        )

    @classmethod
    def _from_weighted(
        cls, weighted_pairs: Iterable[tuple[str, str, int]]
    ) -> OverturnStats:
        overturns: dict[str, int] = {}
        resolved: dict[str, int] = {}
        for verdict, status, n in weighted_pairs:
            if status not in RESOLVED_STATUSES:
                continue
            resolved[verdict] = resolved.get(verdict, 0) + n
            if status == OVERTURN_STATUS:
                overturns[verdict] = overturns.get(verdict, 0) + n
        return cls(overturns, resolved)

    # -- 查询 ---------------------------------------------------------------

    def total_resolved(self) -> int:
        return sum(self.resolved_by_verdict.values())

    def total_overturns(self) -> int:
        return sum(self.overturns_by_verdict.values())

    def overturn_prob(self, verdict: str) -> float:
        """P(人工驳回 | verdict) 的后验均值(Beta(1,1) 先验)。"""
        key = str(verdict or "")
        overturns = self.overturns_by_verdict.get(key, 0)
        resolved = self.resolved_by_verdict.get(key, 0)
        if resolved == 0:
            # 该档位无历史 → 借全局经验;全局也无历史 → 均匀先验均值 0.5。
            resolved = self.total_resolved()
            overturns = self.total_overturns()
        return (overturns + 1) / (resolved + 2)


def _parse_iso_utc(ts: str) -> datetime | None:
    """解析 ISO8601 时间戳为 UTC aware datetime;无时区按 UTC,失败返回 None。"""
    try:
        parsed = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def aging_factor(
    entry: Any,
    *,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> float:
    """老化因子 min(age/max_age, 1):越老越大,超过基准时长后饱和为 1。

    created_at 缺失 / 不可解析 / 晚于 now(时钟偏移)时保守取 0,绝不抛错。
    """
    if max_age_seconds <= 0:
        raise ValueError(f"老化基准时长必须为正数:max_age_seconds={max_age_seconds!r}")
    ts = str(getattr(entry, "created_at", "") or "")
    if not ts:
        return 0.0
    created = _parse_iso_utc(ts)
    if created is None:
        return 0.0
    reference = now if now is not None else datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    age_seconds = (reference - created).total_seconds()
    return min(max(age_seconds, 0.0) / float(max_age_seconds), 1.0)


def priority(
    entry: Any,
    *,
    weights: TriageWeights | None = None,
    overturn_stats: OverturnStats | None = None,
    now: datetime | None = None,
    p_nsfw: float | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> float:
    """计算单条复核条目的信息价值优先级(越大越先看)。

    :param entry: 鸭子类型条目(verdict / created_at 字段)。
    :param weights: 四分量权重;默认 :data:`DEFAULT_WEIGHTS`(温和)。
    :param overturn_stats: 翻案概率估计器;默认冷启动均匀先验(全部 0.5)。
    :param now: 计算老化用的时钟(注入以保证可测/同批一致);默认当前 UTC。
    :param p_nsfw: 显式 p(NSFW);None 时回落 verdict 档位代理。
    :param max_age_seconds: 老化基准时长(默认 72h)。
    :raises ValueError: 权重/概率/时长非法时(中文消息)。
    """
    w = weights if weights is not None else DEFAULT_WEIGHTS
    stats = overturn_stats if overturn_stats is not None else OverturnStats()
    verdict = str(getattr(entry, "verdict", "") or "")
    if p_nsfw is None:
        p = VERDICT_P_NSFW_PROXY.get(verdict, 0.0)
    else:
        p = float(p_nsfw)
        if not math.isfinite(p):
            raise ValueError(f"p(NSFW) 必须是有限数值:{p_nsfw!r}")
    harm = VERDICT_HARM.get(verdict, 0.0)
    overturn = stats.overturn_prob(verdict)
    age = aging_factor(entry, now=now, max_age_seconds=max_age_seconds)
    return (
        w.w_p_nsfw * p
        + w.w_harm * harm
        + w.w_overturn * overturn
        + w.w_age * age
    )


def _validate_boost_map(
    boost_by_url: Mapping[str, float] | None,
) -> dict[str, float]:
    """校验并规整提权加项映射:值必须是非负有限数值(中文报错)。

    键统一 ``str()`` 规整(与条目 site_url 的读取口径一致);空映射 /
    None 返回空 dict(等价于不加,行为与旧版完全一致)。
    """
    if not boost_by_url:
        return {}
    boosts: dict[str, float] = {}
    for url, value in boost_by_url.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError(f"提权加项必须是有限数值:{url!r} -> {value!r}")
        number = float(value)
        if number < 0.0:
            raise ValueError(f"提权加项必须是非负数值:{url!r} -> {value!r}")
        boosts[str(url)] = number
    return boosts


def sort_entries(
    entries: Iterable[Any],
    *,
    weights: TriageWeights | None = None,
    overturn_stats: OverturnStats | None = None,
    now: datetime | None = None,
    p_nsfw_by_url: Mapping[str, float] | None = None,
    boost_by_url: Mapping[str, float] | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> list[Any]:
    """按 priority 降序排序(同分按 id 升序 FIFO 平局决断),纯函数。

    * 只影响返回顺序,不修改输入条目;返回新列表(输入可迭代对象任意);
    * 同一批排序共用同一时钟(now 只解析一次),保证年龄可比、结果确定;
    * ``p_nsfw_by_url``:site_url → 真实 agg_nsw_prob 的注入映射,未命中
      的站点回落 verdict 档位代理(库内未存概率时的红线兼容做法);
    * ``boost_by_url``:site_url → 非负提权加项(典型来源:decision.abstain
      的 ``priority_weight`` 分歧弃权提示)。加项直接**加**在四分量优先级
      之上(不进 :func:`priority` 公式),同批条目中加项越大排位越靠前
      (单调),取 0 等价于不加;默认 None = 现状零变化。
    :raises ValueError: 权重/概率/时长/提权加项非法时(中文消息)。
    """
    w = weights if weights is not None else DEFAULT_WEIGHTS
    stats = overturn_stats if overturn_stats is not None else OverturnStats()
    reference = now if now is not None else datetime.now(timezone.utc)
    overrides = p_nsfw_by_url or {}
    boosts = _validate_boost_map(boost_by_url)
    keyed: list[tuple[float, float, Any]] = []
    for index, entry in enumerate(entries):
        site_url = str(getattr(entry, "site_url", "") or "")
        override = overrides.get(site_url)
        score = (
            priority(
                entry,
                weights=w,
                overturn_stats=stats,
                now=reference,
                p_nsfw=override,
                max_age_seconds=max_age_seconds,
            )
            + boosts.get(site_url, 0.0)
        )
        # 无 id 的对象按输入序号兜底,保证键可比、排序确定且稳定。
        tiebreak = getattr(entry, "id", index)
        keyed.append((-score, tiebreak, entry))
    keyed.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in keyed]
