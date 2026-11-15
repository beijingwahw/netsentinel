"""受界并发 VLM 送审(P0-2):把逐图串行外呼改为有界线程池,判定结果不变。

背景(实测,2026-10-13/14):
- VLM 串行推理是全链路唯一最大瓶颈——真实 GLM 单图 P50 4.91s
  (data/logs/netsentinel.log 13:41),站内占比 71.6%,并发度恒为 1
  (classifier_base 默认 classify_batch 就是逐张 for 循环);
- 模拟并发对照(bench B2):20 张图 VLM 阶段 86.5s → 11.0s(7.9×)。

## 设计契约(世界性要点)

1. **只并行"默认批量"分类器(资格检测)**:仅当 ``type(inst).classify_batch
   is NsfwClassifier.classify_batch``(未覆写基类默认逐张循环)时才并行;
   覆写了 ``classify_batch`` 的分类器(failover 链 / nudenet / clip / guard
   的原生批量逻辑)与任何注入的替身分类器一律走原串行路径——**注入
   fake 的既有测试全部自带 classify_batch,自动落在串行路径,零扰动**。
2. **复用已审计机器**:并发执行委托 :func:`vision.parallel_classify.
   classify_parallel` 注入**调用方持有的受界线程池**(A166 契约:注入
   executor 不关闭、单条异常容错为 error 分、保序返回)。不重新发明
   收集/容错逻辑。
3. **异常语义(与串行的差异,如实登记)**:串行默认循环中单图异常会
   炸掉整批 → 该成员被跳过;并行路径单图异常按 A166 容错为
   ``nsfw_prob=0.0 + scores={"error": ...}`` 带内条目。对生产主分类器
   (multi_provider / glm_adapter)而言:classify 内部已把绝大多数错误
   (HTTP 429/网络)就地降级为 error 分——两路径本就同款;唯一分歧是
   "必须上抛"类(如 VlmBudgetExceeded 预算耗尽):串行=成员跳过后由
   编排层报"没有可用成员",并行=全部 error 分后命中 **V10.24 快速中止**
   (首笔全败 ≥5 → 中文 RuntimeError,附余额/密钥/网络根因提示与
   ``vlmctl ping`` 指引)——**并行的报错信息更可诊断**,两条路径都
   以中止收场,绝无静默 clean。
4. **线程安全审计结论(并发放行的前提)**:
   - VlmCache:全部操作(含 ``spend_one`` 预算"查+增")在 ``self._lock``
     内,连接经 storage.kernel 工厂 ``check_same_thread=False`` 建立——
     跨线程安全,预算扣减原子(并发不超支);
   - vlm_client 断路器:模块级 ``_BREAKERS`` 全程持锁;每提供方限速桶
     (``provider_rpm``)建桶持锁;
   - multi_provider 惰性定格(``_get_resolved``/``_get_client``)存在良性
     竞态:重复构造同一对象后幂等覆盖,无中间态可见性风险。
5. **受界(红线 35 精神)**:自动档 workers = min(档位 io_workers,
   ``DEFAULT_MAX_WORKERS=6``);Config 字段 ``vlm_parallel_workers``(V10.29
   收录,缺省 0 = 自动档;1 = 强制串行逐字节回旧;2~16 显式,>16 钳制)。
   本模块只加速"对自有 VLM 提供方"的 IO 等待;对目标站点的礼貌抓取
   (fetch_delay_s)、举报频控一概不触碰。礼貌护栏:并发下如需限速,
   配 ``cfg.provider_rpm``(A74 每提供方 RPM 令牌桶,V10.29 起为一等
   Config 字段,缺省 0 = 不限)。

## 遥测

并行路径才计数(``vlm.parallel.batches`` / ``vlm.parallel.items`` /
gauge ``vlm.parallel.workers``);串行路径零计数零扰动,行为与 P0-2
之前逐字节一致。整批耗时由 classify_parallel 内置 ``classify.parallel``
计时承接。

仅依赖标准库 + 同包 parallel_classify;离线可用。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, ImageScore
from netsentinel.vision.classifier_base import NsfwClassifier
from netsentinel.vision.parallel_classify import _io_workers, classify_parallel

__all__ = ["classify_bounded", "resolve_workers", "DEFAULT_MAX_WORKERS", "ABS_MAX_WORKERS"]

logger = logging.getLogger(__name__)

#: 自动档并发上限:实测 GLM P50 4.91s/张,6 并发 ≈ 单站一批(P0-1 后
#: 送审数通常 ≤10)在一个半延迟窗内完成;再大对单站收益递减、对提供方
#: 瞬时压力线性上升。
DEFAULT_MAX_WORKERS = 6

#: 显式配置的绝对上限(防配置误写把提供方打爆;需要更高请同时评估
#: provider_rpm 与提供方配额)。
ABS_MAX_WORKERS = 16


def resolve_workers(cfg: Config) -> int:
    """解析送审并发 workers(:data:`vlm_parallel_workers` Config 字段,V10.29)。

    - ``0``(缺省)/不可数值化 → 自动档 ``min(档位 io_workers,
      DEFAULT_MAX_WORKERS)``(mid 档 20 核 → 10 → 钳到 6);
    - ``1`` → 串行(调用方按 <2 走旧路径,逐字节旧行为);
    - ``>=2`` → ``min(值, ABS_MAX_WORKERS)``。

    :return: 生效 workers(1 = 串行)。
    """
    raw = getattr(cfg, "vlm_parallel_workers", None)
    if raw is None:
        raw = 0
    try:
        val = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "vlm_parallel_workers 配置不可数值化(%r),按自动档兜底", raw
        )
        return max(1, min(int(_io_workers(cfg)), DEFAULT_MAX_WORKERS))
    if val == 1:
        return 1
    if val < 1:  # 0 或负数 = 自动档
        return max(1, min(int(_io_workers(cfg)), DEFAULT_MAX_WORKERS))
    return min(val, ABS_MAX_WORKERS)


def _uses_default_batch(inst: Any) -> bool:
    """分类器是否沿用基类默认 ``classify_batch``(逐张 for 循环)。

    只有默认批量才是"逐张 classify 的等价重排",可安全并行;任何覆写
    (failover 链 / nudenet / clip / guard 原生批量,或注入替身)都按
    其自身语义串行调用,绝不替它改执行模型。
    """
    try:
        return type(inst).classify_batch is NsfwClassifier.classify_batch
    except AttributeError:  # 协议替身(无 classify_batch 属性)不可能命中本分支,防御
        return False


def classify_bounded(
    inst: Any, imgs: list[ImageEvidence], cfg: Config
) -> list[ImageScore]:
    """成员批量送审入口:P0-2 受界并发,串行路径逐字节旧行为。

    调度决策(按序短路):

    1. workers < 2(``vlm_parallel_workers`` 为 0/1,或档位推导为 1)→
       原样 ``inst.classify_batch(imgs)``;
    2. 批量 < 2 张 → 同上(单张开池纯开销);
    3. 分类器覆写了 ``classify_batch`` → 同上(尊重其原生批量语义);
    4. 其余 → 注入受界线程池(``min(workers, len(imgs))``)交给
       :func:`classify_parallel` 保序执行。

    :param inst: 成员分类器实例(需有 ``classify``/``classify_batch``)。
    :param imgs: 送审图片(P0-1 闸门出口清单)。
    :param cfg: 配置。
    :return: 与输入等长、保序的评分列表。
    """
    workers = resolve_workers(cfg)
    if workers < 2 or len(imgs) < 2 or not _uses_default_batch(inst):
        return inst.classify_batch(imgs)

    cap = min(workers, len(imgs))
    telemetry.inc("vlm.parallel.batches")
    telemetry.inc("vlm.parallel.items", float(len(imgs)))
    telemetry.gauge("vlm.parallel.workers", float(cap))
    name = str(getattr(inst, "name", "") or "member")
    logger.info(
        "受界并发送审:%s 张 × workers=%d(成员=%s;串行等价、保序返回)",
        len(imgs),
        cap,
        name,
    )
    with ThreadPoolExecutor(max_workers=cap, thread_name_prefix="vlmsend") as pool:
        # 注入 executor 归属本函数(classify_parallel 契约:不关闭注入池),
        # with 退出即关——不留孤儿线程(红线 37)。
        return classify_parallel(inst, imgs, cfg, executor=pool)
