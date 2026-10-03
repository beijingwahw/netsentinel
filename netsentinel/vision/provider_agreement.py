"""NetSentinel 跨平台一致性分析(A69)。

同一批图片交给多个平台的视觉模型打分后,由本模块回答三个问题:
谁偏松、谁偏严、哪张图争议大——多平台 ensemble 的质检仪。

核心口径:
- ``provider_of``:从 ``ImageScore.model`` 还原提供方名。
  ``"openai:gpt-4o-mini"`` → ``"openai"``(冒号前缀即提供方);
  ``"failover→glm:glm-5.3-flash"`` → ``"failover"``(故障转移胜者标注以 "→" 分隔,
  先于 ":" 拆分);无冒号无箭头(``stub`` / ``ensemble`` / ``vlm-arbiter`` ...)原样返回。
- ``analyze``:按 ``image.path`` 分组,仅取组内 ≥2 个**不同提供方**的组参与统计;
  共识 = 组内全部评分的均值(同一提供方对同一图多条评分先按提供方取均值去重)。输出:
  - ``providers``:参与分析提供方去重排序;
  - ``per_image_spread``:每图 ``{"image","max","min","spread"}``,spread = max-min,
    按 spread 降序,最多 :data:`MAX_SPREAD_ROWS` 条;
  - ``bias``:提供方逐图(分值-共识)的均值,>0 偏松、<0 偏严;
  - ``outliers``:|bias| > :data:`BIAS_OUTLIER_THRESHOLD` 的提供方,附中文处置建议;
  - ``pair_agreement``:每对提供方(按名称排序的二元组键)在同一图上
    |pa-pb| <= :data:`PAIR_AGREEMENT_TOLERANCE` 的配对占比(0~1);
    从未同图出现的配对不计入。
- 无有效组(空输入或全部单提供方组)时返回全空结构。

仅依赖标准库与共享契约 ``netsentinel.contracts``,纯函数、离线可用、零外呼。

V5 升级:``analyze`` 由"先分组收集整条评分、再逐组重算"合并为**单遍流式**——
分组的同时就地累加各提供方与全组的 (Σ, n),统计遍只扫轻量累加器,不再重建
每组的评分列表;求和顺序与旧实现逐位一致,输出严格等价。入口接
``telemetry.timer("agreement.analyze")``,慢路径(> :data:`SLOW_ANALYZE_SECONDS`)
记 WARNING 日志。

用法示例::

    from netsentinel.vision.provider_agreement import analyze, provider_of

    provider_of("openai:gpt-4o-mini")    # -> "openai"
    provider_of("failover→glm:glm-5.3-flash")  # -> "failover"
    res = analyze(scores)                # 多平台 ImageScore 列表
    res["bias"]["glm"]                   # -> 0.2(glm 系统性偏松 0.2)
"""
from __future__ import annotations

import logging
import time
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import ImageScore

__all__ = ["provider_of", "analyze"]

logger = logging.getLogger(__name__)

#: |bias| 超过该值视为系统性偏离(离群提供方)
BIAS_OUTLIER_THRESHOLD = 0.15
#: 同图两提供方分差不超过该值视为"一致"
PAIR_AGREEMENT_TOLERANCE = 0.2
#: per_image_spread 最多输出的条数
MAX_SPREAD_ROWS = 20
#: 慢路径阈值(秒):analyze 超过该时长记 WARNING(V5 可观测性)
SLOW_ANALYZE_SECONDS = 1.0

#: 浮点容差:避免 0.9-0.7 = 0.2000...07 这类表示误差干扰边界判断
_EPS = 1e-9

#: 离群提供方的中文处置建议(按偏高/偏低二选一填充)
_NOTE_HIGH = "该提供方系统性偏高,建议人工抽检其评分样本"
_NOTE_LOW = "该提供方系统性偏低,建议人工抽检其评分样本"


class _GroupAcc:
    """单图累加器(V5):分组遍历中就地累计,避免整组缓存评分对象。

    ``by``:提供方 → ``[Σ分值, 条数]``;``total``/``count``:全组 Σ 与条数。
    """

    __slots__ = ("by", "total", "count")

    def __init__(self, prob: float, provider: str) -> None:
        self.by: dict[str, list[float]] = {provider: [prob, 1.0]}
        self.total = prob
        self.count = 1


def provider_of(model_name: str) -> str:
    """从模型名还原提供方名。

    - ``"openai:gpt-4o-mini"`` → ``"openai"``(提供方 = 冒号前缀);
    - ``"failover→glm:glm-5.3-flash"`` → ``"failover"``(先按 "→" 拆出故障转移标注);
    - 无冒号无箭头(``stub`` / ``ensemble`` / ``vlm-arbiter`` / ``nudenet`` ...)原样返回;
    - 空字符串原样返回。

    :param model_name: ``ImageScore.model`` 字段。
    :return: 提供方名(用于跨平台对比的分组键)。
    """
    if not model_name:
        return model_name
    if "→" in model_name:
        return model_name.split("→", 1)[0]
    if ":" in model_name:
        return model_name.split(":", 1)[0]
    return model_name


def analyze(scores: list[ImageScore]) -> dict[str, Any]:
    """跨平台一致性分析(口径见模块 docstring)。

    :param scores: 多平台成员分类器对一批图片的评分(顺序任意,
        兼容 A56 agreement_matrix 的 ImageScore 列表输入)。
    :return: ``{"providers", "per_image_spread", "bias", "outliers", "pair_agreement"}``;
        无有效组时各项为空列表 / 空字典。

    V5:单遍流式——分组的同时就地累加每提供方与全组的 (Σ, n),统计遍只扫
    轻量累加器;求和/均值顺序与旧"收集后重算"逐位一致,输出严格等价;
    入口计时 ``telemetry.timer("agreement.analyze")``,超
    :data:`SLOW_ANALYZE_SECONDS` 记 WARNING。
    """
    start = time.perf_counter()
    with telemetry.timer("agreement.analyze"):
        # 单遍:按 image.path 分组并就地累加(Σ, n)——不再整组缓存评分对象
        # 再逐组重算;同一 model 名在整批评分中高度重复,provider_of 结果按名
        # 备忘(纯字符串函数,结果与逐条现算逐位一致)。
        grouped: dict[str, _GroupAcc] = {}
        prov_cache: dict[str, str] = {}
        for s in scores:
            path = s.image.path
            g = grouped.get(path)
            model = s.model
            pv_key = prov_cache.get(model)
            if pv_key is None:
                pv_key = prov_cache[model] = provider_of(model)
            prob = s.nsfw_prob
            if g is None:
                grouped[path] = _GroupAcc(prob, pv_key)
                continue
            g.total += prob
            g.count += 1
            pv = g.by.get(pv_key)
            if pv is None:
                g.by[pv_key] = [prob, 1.0]
            else:
                pv[0] += prob
                pv[1] += 1.0

        providers_seen: set[str] = set()
        spread_rows: list[dict[str, Any]] = []
        dev_sum: dict[str, float] = {}                       # 提供方 -> Σ(分值-共识)
        dev_cnt: dict[str, int] = {}                         # 提供方 -> 参与图片数
        pair_ok: dict[tuple[str, str], int] = {}             # 配对 -> 一致次数
        pair_total: dict[tuple[str, str], int] = {}          # 配对 -> 同图出现次数

        for path, g in grouped.items():
            by_provider = g.by
            if len(by_provider) < 2:
                logger.debug(
                    "图片 %s 仅 %d 个提供方评分,不参与一致性统计",
                    path, len(by_provider),
                )
                continue

            # 同一提供方多条评分先取均值去重(与旧实现 sum(list)/len(list) 逐位一致)
            provider_value = {p: pv[0] / pv[1] for p, pv in by_provider.items()}
            consensus = g.total / g.count

            providers_seen.update(provider_value)
            values = list(provider_value.values())
            hi, lo = max(values), min(values)
            spread_rows.append(
                {
                    "image": path,
                    "max": round(hi, 4),
                    "min": round(lo, 4),
                    "spread": round(hi - lo, 4),
                }
            )

            for p, v in provider_value.items():
                dev_sum[p] = dev_sum.get(p, 0.0) + (v - consensus)
                dev_cnt[p] = dev_cnt.get(p, 0) + 1

            names = sorted(provider_value)
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    key = (names[i], names[j])
                    pair_total[key] = pair_total.get(key, 0) + 1
                    if (
                        abs(provider_value[names[i]] - provider_value[names[j]])
                        <= PAIR_AGREEMENT_TOLERANCE + _EPS
                    ):
                        pair_ok[key] = pair_ok.get(key, 0) + 1

        if not providers_seen:  # 空输入或全部单提供方组 → 全空结构
            logger.info("provider_agreement: 无多提供方图片,返回空结构")
            return {
                "providers": [],
                "per_image_spread": [],
                "bias": {},
                "outliers": [],
                "pair_agreement": {},
            }

        bias = {p: round(dev_sum[p] / dev_cnt[p], 4) for p in sorted(dev_cnt)}

        outliers: list[dict[str, Any]] = []
        for p in sorted(bias):
            raw = dev_sum[p] / dev_cnt[p]
            if abs(raw) > BIAS_OUTLIER_THRESHOLD + _EPS:
                outliers.append(
                    {
                        "provider": p,
                        "delta": bias[p],
                        "note": _NOTE_HIGH if raw > 0 else _NOTE_LOW,
                    }
                )
        outliers.sort(key=lambda o: (-abs(o["delta"]), o["provider"]))

        spread_rows.sort(key=lambda r: -r["spread"])  # 稳定排序:同 spread 保持首现顺序
        pair_agreement = {
            key: round(pair_ok.get(key, 0) / total, 4)
            for key, total in sorted(pair_total.items())
        }

        logger.info(
            "provider_agreement: %d 个提供方、%d 张有效图片、%d 个离群提供方",
            len(providers_seen),
            len(spread_rows),
            len(outliers),
        )
        result = {
            "providers": sorted(providers_seen),
            "per_image_spread": spread_rows[:MAX_SPREAD_ROWS],
            "bias": bias,
            "outliers": outliers,
            "pair_agreement": pair_agreement,
        }

    elapsed = time.perf_counter() - start
    if elapsed > SLOW_ANALYZE_SECONDS:
        logger.warning(
            "provider_agreement.analyze 耗时 %.3fs(>%.1fs),评分批量过大,建议分批",
            elapsed, SLOW_ANALYZE_SECONDS,
        )
    return result
