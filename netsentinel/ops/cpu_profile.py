"""NetSentinel(净网哨兵)CPU 画像与三档并发档位换算(ops.cpu_profile,A163)。

V9「CPU 自适应三档并发」的探测底座,供 A164 ``ops.concurrency``、
A165 ``ops.tier_state``、A169 ``finishflow`` 等上层消费。**只读本地**:

- :func:`detect` 探测本机 CPU 画像(核数 / 架构 / 平台概要 / psutil 是否
  可用),**零外呼**——本模块不 import 任何网络库、不做任何联网探测;
- :func:`tier_workers` 按 CONTRACTS-V9 §1 的权威公式把档位换算成
  workers 数(low=``max(1, N//4)`` / mid=``max(1, N//2)`` /
  high=``max(1, N-reserve)``),非法档位抛中文 :class:`ValueError`;
- :func:`recommend` 依画像给默认档位建议(核数≤2→low;≤8→mid;>8→high;
  psutil 可用且系统占用>0.8 时降一档),坏输入按 cores=2 容错;
- :func:`validate_tier` 档位校验(合法三值 ``low`` / ``mid`` / ``high``)。

三档定义(CONTRACTS-V9 §1,以 ``os.cpu_count()`` 缺省回退 2 为 N):

===== ======== ======== ===================
N     low      mid      high(reserve=1)
===== ======== ======== ===================
4     1        2        3
8     2        4        7
16    4        8        15
32    8        16       31
===== ======== ======== ===================

红线关联:

- 红线 37(资源治理):三档公式天然满足 workers ≤ N(高档 reserve=0
  时恰等于 N,即"全核");低/中档永远只占一半/四分之一,把余量留给
  外呼礼貌间隔(V6 链)与系统本身;
- 红线 35(压榨边界):本模块**只提供本地换算**,不放宽任何对外频控——
  档位与 ``fetch_delay_s`` / 举报频控互不相干。

测试钩子:环境变量 ``NETSENTINEL_FAKE_CORES``(正整数)可覆盖
:func:`detect` 的核数,便于离线测试与离线演示(A179);非法值(非整数、
<1、空串)一律忽略并回退真实探测。

测试约定:全部离线、零网络、零真实等待;psutil 经 ``sys.modules``
注入 fake 或置 ``None`` 双态验证。
"""
from __future__ import annotations

import os
import platform
import sys

__all__ = [
    "DEFAULT_CORES",
    "ENV_FAKE_CORES",
    "TIERS",
    "USAGE_HIGH_THRESHOLD",
    "detect",
    "recommend",
    "tier_workers",
    "validate_tier",
]

#: 缺省回退核数(与 CONTRACTS-V9 §1「以 os.cpu_count() 缺省回退 2 为 N」一致)
DEFAULT_CORES = 2

#: 合法三档(CONTRACTS-V9 §1:low 后台/省电、mid 默认、high 极限压榨)
TIERS = ("low", "mid", "high")

#: 测试/演示钩子:覆盖 detect() 核数的环境变量名(正整数才生效)
ENV_FAKE_CORES = "NETSENTINEL_FAKE_CORES"

#: recommend 的降档阈值:psutil 可用且系统占用(0~1)> 该值 → 档位降一级
USAGE_HIGH_THRESHOLD = 0.8

#: 降档映射(high→mid→low;low 已是底线不再降)
_DOWNGRADE = {"low": "low", "mid": "low", "high": "mid"}


# ---------------------------------------------------------------------------
# 内部工具(纯本地,容错取向:读不到就按保守缺省)
# ---------------------------------------------------------------------------
def _env_fake_cores() -> int | None:
    """读 ``NETSENTINEL_FAKE_CORES``;非正整数一律忽略返回 None。"""
    raw = os.environ.get(ENV_FAKE_CORES, "")
    if not raw:
        return None
    try:
        n = int(raw.strip())
    except (TypeError, ValueError):
        return None
    return n if n >= 1 else None


def _coerce_cores(value: object) -> int:
    """把任意输入宽容地折成 ≥1 的核数;折不动按 :data:`DEFAULT_CORES`。"""
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_CORES
    return n if n >= 1 else DEFAULT_CORES


def _psutil_available() -> bool:
    """惰性探测 psutil 是否可用(只探测、绝不安装)。

    先查 ``sys.modules`` 缓存(测试注入 fake 的路径),未缓存才尝试真实
    导入;导入失败(未安装或装坏了)一律按 False,不影响任何主流程。
    """
    if sys.modules.get("psutil") is not None:
        return True
    try:
        import psutil  # noqa: F401 - 仅探测可导入性,不调用其网络相关能力
    except Exception:  # noqa: BLE001 - 未安装/C 扩展损坏均按不可用
        return False
    return True


def _norm_usage(value: object) -> float | None:
    """把占用值折成 [0,1]:>1 视为百分数除以 100;折不动/NaN 返回 None。"""
    try:
        x = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN:视为"读不出",不参与降档
        return None
    if x > 1.0:
        x /= 100.0
    return min(max(x, 0.0), 1.0)


def _resolve_usage(profile: dict) -> float | None:  # type: ignore[type-arg]
    """解析系统占用(0~1),解析不了返回 None(即不降档)。

    优先级:①画像里显式给的 ``cpu_usage``(测试/调用方注入,支持 0~1
    分数或百分数);②画像显式声明 ``psutil=False`` → 不采样;③惰性导入
    psutil 采样 ``cpu_percent(interval=None)``(非阻塞,首次调用返回
    0.0 表示"自上次调用以来"——读不出高占用就不降档,取向保守不激进)。
    """
    if "cpu_usage" in profile:
        return _norm_usage(profile["cpu_usage"])
    if profile.get("psutil") is False:
        return None
    try:
        import psutil
    except Exception:  # noqa: BLE001 - psutil 不可用就没有"系统占用"一说
        return None
    try:
        pct = psutil.cpu_percent(interval=None)
    except Exception:  # noqa: BLE001 - 采样失败按"读不出"处理
        return None
    return _norm_usage(pct)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def detect() -> dict:
    """探测本机 CPU 画像(零外呼、不安装任何依赖)。

    :return: 恰好四键的画像字典:

        - ``"cores"``:逻辑核数。优先 ``NETSENTINEL_FAKE_CORES``(正整数
          测试钩子),缺省 ``os.cpu_count()``,探测不到回退
          :data:`DEFAULT_CORES`(=2,与契约 §1 一致);
        - ``"arch"``:``platform.machine()``(如 ``"AMD64"`` / ``"x86_64"``),
          个别平台返回空串时回退 ``"unknown"``;
        - ``"platform"``:平台概要 ``"<system>/<sys.platform>"``(如
          ``"Windows/win32"``、``"Linux/linux"``);
        - ``"psutil"``:psutil 是否可导入(惰性探测,**不安装**;不可用
          只影响 recommend 的降档采样,不影响任何换算)。

    用法示例::

        p = detect()
        assert set(p) == {"cores", "arch", "platform", "psutil"}
        assert p["cores"] >= 1 and isinstance(p["psutil"], bool)
    """
    cores = _env_fake_cores()
    if cores is None:
        cores = os.cpu_count() or DEFAULT_CORES
    return {
        "cores": int(cores),
        "arch": platform.machine() or "unknown",
        "platform": f"{platform.system()}/{sys.platform}",
        "psutil": _psutil_available(),
    }


def validate_tier(tier: object) -> str:
    """校验并发档位合法(必须恰为 ``low`` / ``mid`` / ``high``,区分大小写)。

    :param tier: 待校验档位。
    :return: 合法时原样返回该档位字符串。
    :raises ValueError: 非法档位(中文报错),供 :func:`tier_workers` 与
        上层 ``ops.concurrency``(A164)复用同一套报错口径。
    """
    if isinstance(tier, str) and tier in TIERS:
        return tier
    raise ValueError(
        f"并发档位非法:{tier!r},必须是 low / mid / high 三者之一"
    )


def tier_workers(tier: str, *, reserve: int = 1, cores: int | None = None) -> int:
    """按 CONTRACTS-V9 §1 权威公式把档位换算成 workers 数。

    以 N 为核数(显式 ``cores`` 优先;缺省取 :func:`detect` 的核数,
    坏输入按 2 容错):

    - ``low``:``max(1, N//4)`` —— 后台/省电;
    - ``mid``:``max(1, N//2)`` —— 默认;
    - ``high``:``max(1, N - reserve)`` —— 最大限度压榨(reserve=0 即
      全核;reserve ≥ N 时退化 1,绝不返回 0 或负数)。

    红线 37:三档结果恒满足 workers ≤ N(高档 reserve=0 时恰为 N),
    低/中档公式天然只占四分之一/一半。

    :param tier: 档位,非法抛中文 :class:`ValueError`。
    :param reserve: 高档保留核心数(缺省 1,负数按 0;仅作用于 high 档,
        low/mid 不受影响)。
    :param cores: 显式核数(注入用);缺省 ``detect()`` 的核数(受
        ``NETSENTINEL_FAKE_CORES`` 影响)。
    :return: 该档位的 workers 数,恒 ≥ 1。
    """
    tier = validate_tier(tier)
    try:
        reserve_n = int(reserve)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        raise ValueError(
            f"保留核心数 reserve 非法:{reserve!r},必须是非负整数"
        ) from None
    if reserve_n < 0:
        reserve_n = 0
    n = _coerce_cores(cores) if cores is not None else detect()["cores"]
    if tier == "low":
        return max(1, n // 4)
    if tier == "mid":
        return max(1, n // 2)
    return max(1, n - reserve_n)


def recommend(profile: dict) -> str:  # type: ignore[type-arg]
    """依 CPU 画像给默认档位建议(A165 tier_once 的 auto 档来源)。

    规则(核数取向保守、降档取向克制):

    1. 核数:取 ``profile["cores"]``;画像坏输入(``None`` / 非字典 /
       缺键 / 非整数 / <1)一律按 cores=2 容错;
    2. 基础档:cores ≤ 2 → ``low``;cores ≤ 8 → ``mid``;cores > 8 →
       ``high``;
    3. 降档:psutil 可用且系统占用 > :data:`USAGE_HIGH_THRESHOLD`
       (0.8)时降一级(high→mid→low;low 不再降)。占用来源:画像显式
       ``cpu_usage``(0~1 分数或百分数)优先,否则惰性采样
       ``psutil.cpu_percent(interval=None)``;psutil 不可用 / 采样失败 /
       读不出 → 不降档。

    :param profile: :func:`detect` 的画像(或鸭子等价物;坏输入容错)。
    :return: ``"low"`` / ``"mid"`` / ``"high"`` 之一。
    """
    cores = DEFAULT_CORES
    if isinstance(profile, dict):
        cores = _coerce_cores(profile.get("cores"))
    base = "low" if cores <= 2 else ("mid" if cores <= 8 else "high")
    if base == "low":
        return "low"  # low 已是底线,系统再忙也无处可降
    if isinstance(profile, dict):
        usage = _resolve_usage(profile)
    else:
        usage = None
    if usage is not None and usage > USAGE_HIGH_THRESHOLD:
        return _DOWNGRADE[base]
    return base
