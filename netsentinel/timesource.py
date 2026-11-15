"""NetSentinel 时间源单源化(V10.21 席位2,L0 基座级:零内部依赖)。

动机
----
V10.18 审计 Top1:全库散布 17 处 naive ``datetime.now()`` 长尾(目录名 /
时间戳字符串 / 显示文本),外加 aware 惯用式 ``datetime.now(timezone.utc)
.astimezone().isoformat(...)`` 在 contracts/storage/intel/telemetry/
vision 等处的并行复制。两个隐患:

1. **naive 长尾**:``datetime.now()`` 返回无时区墙钟,跨机/跨时区部署
   时时间戳语义漂移,且与 aware 值混比抛 ``TypeError``;
2. **3.12 弃用趋势**:``datetime.utcnow()`` 自 Python 3.12 起 Deprecated,
   naive "现在"系列惯用式整体处于被官方劝退通道,新代码应统一经本模块
   取时,后续若要整体切tz策略只改这一处。

本模块与 :mod:`netsentinel.mathx` 同级(基座叶子):**零内部依赖**,
只 import 标准库 ``datetime``,可被任何层安全引用。

豁免清单(不迁移、保留原式,均有明确理由)
-----------------------------------------
- ``contracts.py``:**冻结文件**(仅项目负责人可改),其内部
  ``now_iso()`` 惯用式保留,并在本 docstring 注记豁免;语义上
  ``contracts.now_iso()`` 与本模块 ``timesource.now_iso()`` 逐字节
  等价(同一惯用式)。
- ``submit/rate_limit.py`` / ``ops/adaptive.py``:**自洽 naive 契约**
  (内部统一 naive 本地时间语义,aware 输入先 ``astimezone()`` 归一,
  文档已写明),迁移反而破坏其自洽性。
- ``storage/kernel.py`` / ``intel/graph.py`` / ``intel/site_memory.py``:
  **他席领地**(V10.21 席位3 接入),本席位不越界。

等价性承诺(逐字节)
--------------------
本模块各函数与其所替换的既有惯用式在**同一时刻调用时生成的字符串 /
数值完全相同**(strftime 字段、ISO 格式、时区偏移一字不差):

- ``now_iso()`` ≡ ``datetime.now(timezone.utc).astimezone().isoformat(
  timespec=...)``(本地墙钟 + 本机 tz 偏移);
- ``now_local().strftime(...)`` ≡ ``datetime.now(timezone.utc).astimezone()
  .strftime(...)`` ≡ ``datetime.now().strftime(...)``(strftime 只读
  墙钟字段,naive 与 aware 本地时刻输出相同);
- ``now_naive_local()`` ≡ ``datetime.now()``(naive 本地,供豁免子系统
  与既有 naive 调用方 documenting/过渡);
- ``fromtimestamp_local(ts)`` ≡ ``datetime.fromtimestamp(ts)``(naive
  本地墙钟显示用)。

守门:``tests/test_timesource.py`` 用 AST 反向扫描全库,断言 aware
惯用式只许出现在本模块与豁免白名单内;新增未登记即测试失败。
"""
from __future__ import annotations

import datetime as _dt

__all__ = [
    "utc_now",
    "now_local",
    "now_iso",
    "now_naive_local",
    "fromtimestamp_local",
]


def utc_now() -> _dt.datetime:
    """当前 UTC 时刻(aware,``tzinfo=timezone.utc``)。

    替代散布的 ``datetime.now(timezone.utc)``;aware 返回值可直接与
    任何 aware 时刻比较/相减,杜绝 naive/aware 混比。
    """
    return _dt.datetime.now(_dt.timezone.utc)


def now_local() -> _dt.datetime:
    """当前本地时刻(aware,带本机时区)。

    等价 ``datetime.now(timezone.utc).astimezone()``,亦等价旧式
    ``datetime.now().astimezone()``;strftime 输出与 naive 本地
    ``datetime.now()`` 完全相同(墙钟字段一致)。
    """
    return _dt.datetime.now(_dt.timezone.utc).astimezone()


def now_iso(timespec: str = "seconds") -> str:
    """当前本地时间的 ISO8601 字符串(带时区偏移)。

    逐字节等价既有惯用式::

        datetime.now(timezone.utc).astimezone().isoformat(timespec=timespec)

    与 ``contracts.now_iso()``(冻结豁免)输出相同;签名多一个
    ``timespec`` 形参以覆盖 ``timespec="microseconds"`` 等变体。
    """
    return now_local().isoformat(timespec=timespec)


def now_naive_local() -> _dt.datetime:
    """当前本地墙钟(naive,**无时区**)。

    逐字节等价 ``datetime.now()``。仅供:(a) 豁免子系统
    (rate_limit/adaptive 的 naive 契约)的文档化对照;(b) 既有
    naive 调用方在"strftime 只读墙钟"语义下的**行为等价**迁移
    (strftime 输出与 ``now_local()`` 完全一致)。新代码默认应选
    :func:`now_local` / :func:`utc_now`。
    """
    return _dt.datetime.now()


def fromtimestamp_local(ts: float) -> _dt.datetime:
    """Unix 时间戳 → 本地墙钟(naive,显示用)。

    逐字节等价 ``datetime.fromtimestamp(ts)``(本机时区、无 tzinfo);
    供 modelmgr 等处把模型元数据的时间戳数值格式化为显示文本。
    """
    return _dt.datetime.fromtimestamp(ts)
