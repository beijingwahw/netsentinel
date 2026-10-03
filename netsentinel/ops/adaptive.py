"""NetSentinel(净网哨兵)自适应重扫调度(netsentinel.ops.adaptive,A59)。

核心思想:**站点越活跃(指纹常变)查得越勤,长期不变则退避**——
把有限的巡查预算优先花在变化频繁的目标上,对"死水"站点拉长间隔。

纯函数、纯标准库(仅用 datetime 语义,无 IO、无网络、无隐藏时钟依赖:
``next_run`` 的"当前时间"由调用方注入,缺省才取 ``datetime.now()``),
同一输入永远得到同一输出,便于测试与人工复核。

四个入口(签名见 CONTRACTS-V3.md §3 A59):

- :func:`volatility`:指纹序列的变化频率(0~1);
- :func:`suggest_interval_hours`:按波动分档建议下次重扫间隔(小时);
- :func:`next_run`:按"最近一次运行时间 + 间隔"推算下次应运行时刻;
- :func:`plan_row`:把以上三者组装成调度器 / 复核台 UI 直接消费的一行计划。

分档规则(``base_h`` 为基准间隔,来自 ``cfg.adaptive_base_interval_h``,默认 72 小时):

============ =============================== =================
档位          条件                              建议间隔
============ =============================== =================
历史不足      历史指纹 < 2 期                    ``base_h``
高波动        volatility >= 0.5                  ``max(base_h//2, 6)``
低波动退避    volatility <= 0.1 且历史 >= 3 期   ``min(base_h*2, 720)``
中间          其余                               ``base_h``
============ =============================== =================

上下限钳制:任何建议间隔都落在 ``[6, 720]`` 小时内(最快约一天 4 次
扫同一站点、最慢约一月一扫),防止极端配置把目标刷爆或彻底遗忘。

时间语义:统一 naive 本地时间——aware 输入(带时区)先 ``astimezone()``
换算成本机墙钟再去掉 tzinfo,避免 naive/aware 混用抛 ``TypeError``。

用法示例::

    >>> from netsentinel.ops.adaptive import volatility, suggest_interval_hours
    >>> volatility(["fp1", "fp1", "fp2"])        # 3 期 1 变 → 0.5(高波动)
    0.5
    >>> suggest_interval_hours(["fp1", "fp2"], 72)   # 高波动 → 36 小时
    36

可观测性(V5):每次成功的间隔建议累加 ``telemetry.inc("adaptive.suggest")``
(仅计数,不改变纯函数语义——同输入仍得同输出)。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from netsentinel import telemetry

__all__ = [
    "MIN_INTERVAL_H",
    "MAX_INTERVAL_H",
    "volatility",
    "suggest_interval_hours",
    "next_run",
    "plan_row",
]

#: 建议间隔的硬下限(小时):再活跃的站点也不短于 6 小时一扫
MIN_INTERVAL_H = 6

#: 建议间隔的硬上限(小时):再稳定的站点也不超过 30 天一扫
MAX_INTERVAL_H = 720

#: 高波动档阈值:相邻指纹变化频率达到该值即"加密巡查"
HIGH_VOLATILITY = 0.5

#: 低波动档阈值:变化频率不高于该值(且历史充足)才允许"退避拉长"
LOW_VOLATILITY = 0.1

#: 低波动退避所需的最少历史期数:至少看过 3 个指纹才有资格说"它很稳定"
LOW_TIER_MIN_HISTORY = 3


# ---------------------------------------------------------------------------
# 波动度
# ---------------------------------------------------------------------------
def volatility(history: list[str] | None) -> float:
    """计算指纹序列的变化频率,返回 [0, 1] 内的浮点数。

    定义:相邻指纹不同的次数 ÷ (期数 - 1)。

    - ``history`` 为 None、空列表或只有 1 期:没有任何"相邻变化"可数,
      返回 ``0.0``(宁可低估波动,不凭空加密巡查);
    - 全部相同 → ``0.0``(纹丝不动);全部不同 → ``1.0``(每期都变);
    - 序列中的 ``None`` 元素按空指纹 "" 参与比较(防御脏数据)。

    实现为**单遍循环**(V5):一次遍历同时完成 None 规范化、相邻比较与
    计数,不物化中间列表(旧实现先整体拷贝一遍、再 ``zip`` 逐对比较,
    两次遍历外加两份临时列表)。

    :param history: 按时间先后排列的站点指纹序列(来自站点记忆 / 报告)。
    """
    if not history:  # None 或空序列
        return 0.0
    changes = 0
    count = 0
    prev = ""
    have_prev = False
    for fp in history:
        cur = "" if fp is None else fp
        if have_prev and prev != cur:
            changes += 1
        prev = cur
        have_prev = True
        count += 1
    if count < 2:
        return 0.0
    value = changes / (count - 1)
    # 浮点防御:理论上已落在 [0,1],仍显式钳制
    return min(1.0, max(0.0, value))


# ---------------------------------------------------------------------------
# 间隔建议
# ---------------------------------------------------------------------------
def suggest_interval_hours(history: list[str] | None, base_h: int) -> int:
    """按历史波动分档给出下次重扫间隔(小时),恒在 ``[6, 720]`` 内。

    分档(自上而下首条命中生效):

    1. 历史不足(< 2 期):无从判断波动,保守返回 ``base_h``;
    2. 高波动(volatility >= 0.5):站点指纹常变,加密到
       ``max(base_h // 2, 6)``——但绝不低于 :data:`MIN_INTERVAL_H`;
    3. 低波动(volatility <= 0.1 且历史 >= 3 期):长期不变,退避到
       ``min(base_h * 2, 720)``——但绝不超过 :data:`MAX_INTERVAL_H`;
    4. 其余(中间地带或历史仅 2 期的低波动):维持 ``base_h``。

    :param history: 按时间先后排列的指纹序列(可为 None)。
    :param base_h: 基准间隔(小时),须 > 0(来自 ``adaptive_base_interval_h``)。
    :raises ValueError: ``base_h <= 0`` 时抛出(中文消息)。
    """
    if base_h <= 0:
        raise ValueError(
            f"基准间隔 base_h 必须为正整数(小时),当前为:{base_h}"
        )
    telemetry.inc("adaptive.suggest")  # V5:每次成功建议计数(不影响返回值)
    if not history or len(history) < 2:
        return int(base_h)

    vol = volatility(history)
    if vol >= HIGH_VOLATILITY:
        return max(int(base_h) // 2, MIN_INTERVAL_H)
    if vol <= LOW_VOLATILITY and len(history) >= LOW_TIER_MIN_HISTORY:
        return min(int(base_h) * 2, MAX_INTERVAL_H)
    return int(base_h)


# ---------------------------------------------------------------------------
# 下次运行时刻
# ---------------------------------------------------------------------------
def _as_naive(dt: datetime) -> datetime:
    """把 datetime 统一成 naive 本地时间。

    - naive:原样返回(不同来源的 naive 视为同一本地墙钟语义);
    - aware:先 ``astimezone()`` 换算到本机时区墙钟,再去掉 tzinfo,
      避免 naive/aware 直接比较抛 ``TypeError``。
    """
    if dt.tzinfo is not None and dt.utcoffset() is not None:
        return dt.astimezone().replace(tzinfo=None)
    if dt.tzinfo is not None:  # 声明了 tzinfo 却给不出偏移的病态对象:只剥壳
        return dt.replace(tzinfo=None)
    return dt


def next_run(
    schedule: list[datetime] | None,
    interval_h: int,
    now: datetime | None = None,
) -> datetime:
    """推算下次应运行时刻:``max(schedule) + interval_h`` 小时。

    - ``schedule`` 为空(或全是 None):视为"从未运行过",从"现在"
      起算,即返回 ``now + interval_h``;
    - 多个历史运行时间取**最近一次**(max),而不是平均或首个;
    - naive / aware 混用防御:输入统一转成 naive 本地时间后再比较与
      加法,永远返回 naive datetime;
    - ``now`` 由调用方注入便于测试与重放;缺省取 ``datetime.now()``
      (本地墙钟,naive)。

    :param schedule: 该站点历史运行时刻列表(乱序允许)。
    :param interval_h: 间隔(小时),通常来自 :func:`suggest_interval_hours`。
    :param now: "现在"时刻;None 时取 ``datetime.now()``。
    """
    now_dt = _as_naive(now) if now is not None else datetime.now()
    stamps = [dt for dt in (schedule or []) if dt is not None]
    last = max(_as_naive(dt) for dt in stamps) if stamps else now_dt
    return last + timedelta(hours=interval_h)


# ---------------------------------------------------------------------------
# 调度行组装
# ---------------------------------------------------------------------------
def plan_row(
    url: str,
    history: list[str] | None,
    base_h: int,
    last_run: datetime | None = None,
) -> dict[str, Any]:
    """组装一行重扫计划,供调度器与复核台 UI 直接消费。

    结构::

        {
            "url":        str,                 # 站点地址(原样透传)
            "volatility": float,               # 变化频率 [0, 1]
            "interval_h": int,                 # 建议间隔(小时,[6, 720])
            "next_run":   str | None,          # 下次运行 ISO 时间戳;
                                               # 从未运行过(last_run 为
                                               # None)时为 None
        }

    ``last_run`` 为 None 时不推算 next_run(缺"最近一次运行时间"这一
    必要输入,返回 None 交由上层决定是立刻扫还是等人工指派),而不是
    偷偷用当前时间起算。

    :raises ValueError: ``base_h <= 0`` 时透传 :func:`suggest_interval_hours`。
    """
    vol = volatility(history)
    interval_h = suggest_interval_hours(history, base_h)
    if last_run is None:
        next_iso: str | None = None
    else:
        next_iso = next_run([last_run], interval_h).isoformat()
    return {
        "url": url,
        "volatility": vol,
        "interval_h": interval_h,
        "next_run": next_iso,
    }
