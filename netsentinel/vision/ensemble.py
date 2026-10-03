"""NetSentinel 多模型集成评分(A08)。

把多个成员分类器(stub / nudenet / clip ...)对同一批图片的评分,按图片聚合为一条
集成评分(``model="ensemble"``),供判定阶段(``netsentinel.decision.verdict``)消费。

规则:
- 按 ``image.path`` 分组,输出保持首现顺序;
- 每张图取各成员 ``nsfw_prob`` 的加权平均,权重 ``weights.get(model, 1.0)``;
- 某张图的成员权重全 0(或总和 <= 0)时,按等权兜底;
- 某模型没给某图打分时,仅用现有成员平均,并在 ``scores["missing"]`` 记录缺失模型;
- 结果 ``nsfw_prob`` 截断到 [0, 1];
- 单图单成员时等价于透传该成员分值(仍做截断);
- 输入里 model == "ensemble" 的条目视为已聚合结果,跳过以免重复计数。

用法示例::

    from netsentinel.vision.ensemble import ensemble_scores

    scores = [...]  # 各成员分类器对同一批图片的 ImageScore
    combined = ensemble_scores(scores, weights={"nudenet": 2.0, "clip": 1.0})
    # → 每张图一条 ImageScore(model="ensemble"),按图片首现顺序

仅依赖标准库与共享契约 ``netsentinel.contracts``,离线可用。
"""
from __future__ import annotations

import logging
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import ImageEvidence, ImageScore

__all__ = ["ensemble_scores"]

logger = logging.getLogger(__name__)


def _clamp01(value: float) -> float:
    """把数值截断到 [0, 1]。"""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def ensemble_scores(
    scores: list[ImageScore],
    weights: dict[str, float] | None = None,
) -> list[ImageScore]:
    """把多成员评分聚合为逐图集成评分。

    :param scores: 各成员分类器对一批图片的评分(顺序任意)。
    :param weights: 模型名 -> 权重;缺省按 1.0;某图有效权重全 0 时等权兜底。
    :return: 每张图一条 ``ImageScore``(model="ensemble"),顺序为图片首现顺序。
    """
    logger.debug("ensemble_scores: 收到 %d 条成员评分,权重=%s", len(scores), weights)

    # 过滤:输入应只含成员评分;误传入的 ensemble 结果跳过,避免自我重复计数。
    member_scores = [s for s in scores if s.model != "ensemble"]
    if not member_scores:
        return []

    # 单遍分组(V5,原先两遍合并为一遍):按 image.path 聚合并记录首条 image 引用,
    # 同时收集全局出现过的成员模型(首现顺序),用于计算每张图的缺失成员。
    grouped: dict[str, list[ImageScore]] = {}
    first_image_by_path: dict[str, ImageEvidence] = {}
    all_models: list[str] = []
    seen_models: set[str] = set()
    for s in member_scores:
        path = s.image.path
        grouped.setdefault(path, []).append(s)
        first_image_by_path.setdefault(path, s.image)
        if s.model not in seen_models:
            seen_models.add(s.model)
            all_models.append(s.model)

    results: list[ImageScore] = []
    for path, entries in grouped.items():
        # 该图的成员 -> 概率;同一模型对同一图重复评分时后值覆盖前值。
        members: dict[str, float] = {}
        for s in entries:
            members[s.model] = s.nsfw_prob

        # 有效权重:weights 未给或未提到该模型时按 1.0。
        if weights is None:
            effective: dict[str, float] = {m: 1.0 for m in members}
        else:
            effective = {m: float(weights.get(m, 1.0)) for m in members}
        total = sum(effective.values())
        if total <= 0.0:
            logger.warning("图片 %s 的成员权重总和 <= 0,改用等权平均兜底", path)
            effective = {m: 1.0 for m in members}
            total = float(len(members))

        mean = sum(effective[m] * members[m] for m in members) / total

        extra: dict[str, Any] = {"members": dict(members)}
        missing = [m for m in all_models if m not in members]
        if missing:
            extra["missing"] = missing
            logger.debug("图片 %s 缺少成员评分: %s", path, ", ".join(missing))

        results.append(
            ImageScore(
                image=first_image_by_path[path],
                model="ensemble",
                nsfw_prob=_clamp01(mean),
                scores=extra,
            )
        )

    telemetry.inc("ensemble.images", len(results))
    logger.info("ensemble_scores: %d 张图片完成集成评分", len(results))
    return results
