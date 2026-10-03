"""主动学习反馈环(A44,CONTRACTS-V3.md §3)。

人工复核的"批准 / 驳回"结论是系统最珍贵的监督信号。本模块把这些结论
收集起来,反哺为**阈值调整建议**,并给出预算最优的 VLM 送审排序,让系统
"越用越准":

- :class:`ReviewFeedback`:复核结论收集器(纯内存,可选持久化 jsonl);
- :meth:`ReviewFeedback.threshold_suggestions`:基于 approve/reject 分布的
  阈值建议(nsfw_threshold / min_nsw_images / review_threshold);
- :func:`rank_for_vlm`:按不确定度 |p-0.5| 升序挑选"边缘优先"的图片,
  在 VLM 预算(cfg.vlm_daily_budget / vlm_max_images_per_site)内送审。

V5 升级(CONTRACTS-V5.md):threshold_suggestions 的统计改为对样本**单遍**
完成(旧实现多次全表扫描列表);每次产建议后 ``telemetry.inc(
"learn.suggestions", 建议数)`` 接入统一遥测。

安全红线(必须遵守):

1. **只产出建议,绝不自动修改生产阈值**——本模块的任何返回值都是给运营者
   人工评估用的;生效与否由人改配置文件并重启,机器无权代劳;
2. 纯本地统计(statistics / json),不发起任何网络请求,不外呼 VLM;
3. 样本不足(<20 条)时返回空建议,避免小样本噪声误导运营。

用法::

    fb = ReviewFeedback("data/review_feedback.jsonl")
    fb.record("nsfw", "reject", agg=0.75, nsw_count=2)   # 人工驳回一条 NSFW
    for s in fb.threshold_suggestions(cfg):
        print(s["param"], s["current"], "->", s["suggested"], s["reason"])
"""
from __future__ import annotations

import json
import logging
import statistics
from pathlib import Path
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageScore

logger = logging.getLogger(__name__)

__all__ = [
    "MIN_SAMPLES",
    "REVIEW_FLOOR",
    "REVIEW_STEP",
    "SUSPECT_APPROVE_RATIO",
    "VALID_ACTIONS",
    "VALID_VERDICTS",
    "ReviewFeedback",
    "rank_for_vlm",
]

#: 生成阈值建议所需的最少复核样本数(不足则返回空,契约规定)。
MIN_SAMPLES: int = 20

#: 合法人工动作(与复核队列 approve / reject 语义一致)。
VALID_ACTIONS: tuple[str, ...] = ("approve", "reject")

#: 合法判定值(与 contracts.Verdict 取值一致)。
VALID_VERDICTS: tuple[str, ...] = ("clean", "suspect", "nsfw")

#: 规则③:疑似条目人工确认占比超过该值时,建议下调 review_threshold。
SUSPECT_APPROVE_RATIO: float = 0.6

#: 规则③:每次建议下调的步长与下限(不低于 0.3,避免复核门形同虚设)。
REVIEW_STEP: float = 0.05
REVIEW_FLOOR: float = 0.3

#: jsonl 条目字段(持久化与内存条目统一为这四个键)。
_ENTRY_KEYS: tuple[str, ...] = ("verdict", "action", "agg", "nsw_count")


def _coerce_verdict(verdict: object) -> str:
    """判定值规整:容忍 Verdict 枚举(取 .value),统一小写;非法抛中文错误。"""
    value = str(getattr(verdict, "value", verdict)).strip().lower()
    if value not in VALID_VERDICTS:
        raise ValueError(
            f"verdict 必须是 {' / '.join(VALID_VERDICTS)} 之一,当前值:{verdict!r}"
        )
    return value


def _coerce_action(action: object) -> str:
    """人工动作规整:统一小写;非法抛中文错误。"""
    value = str(action).strip().lower()
    if value not in VALID_ACTIONS:
        raise ValueError(
            f"action 必须是 approve(人工确认)或 reject(人工驳回),当前值:{action!r}"
        )
    return value


class ReviewFeedback:
    """人工复核结论收集器:内存列表为主,可选追加持久化到 jsonl。

    - ``jsonl_path`` 为空:纯内存,进程退出即失(适合一次性分析);
    - ``jsonl_path`` 非空:构造时读回已有记录,record 时逐条追加,
      重复打开同一文件可累积(跨批次反馈)。
    - 损坏 / 缺字段的 jsonl 行会被跳过并记 debug 日志,不让单行脏数据
      拖垮整个反馈环。

    本类只做统计与建议,**绝不写配置、绝不改生产阈值**。
    """

    def __init__(self, jsonl_path: str = "") -> None:
        self.jsonl_path = jsonl_path
        self._entries: list[dict[str, Any]] = []
        if jsonl_path:
            self._load()

    # ------------------------------------------------------------------
    # 记录与读取
    # ------------------------------------------------------------------

    def record(
        self, verdict: str, action: str, agg: float, nsw_count: int
    ) -> dict[str, Any]:
        """记录一条人工复核结论,返回归一化后的条目(副本)。

        :param verdict:  复核条目的机器判定("clean" / "suspect" / "nsfw",
                         也容忍 :class:`~netsentinel.contracts.Verdict` 枚举);
        :param action:   人工动作,"approve"(确认)或 "reject"(驳回);
        :param agg:      该条目判定时的站点聚合分 agg_nsw_prob;
        :param nsw_count:该条目判定时的达标图片数 nsw_image_count。
        :raises ValueError: verdict / action / agg / nsw_count 非法时(中文消息)。
        """
        v = _coerce_verdict(verdict)
        a = _coerce_action(action)
        try:
            score = round(float(agg), 4)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"agg 必须是数字,当前值:{agg!r}") from exc
        try:
            count = int(nsw_count)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"nsw_count 必须是整数,当前值:{nsw_count!r}") from exc

        entry = {"verdict": v, "action": a, "agg": score, "nsw_count": count}
        self._entries.append(entry)
        if self.jsonl_path:
            self._append_jsonl(entry)
        return dict(entry)

    def snapshot(self) -> list[dict[str, Any]]:
        """返回当前全部条目的深拷贝列表(改返回值不影响内部状态)。"""
        return [dict(e) for e in self._entries]

    def count(self) -> int:
        """已记录的复核结论条数。"""
        return len(self._entries)

    # ------------------------------------------------------------------
    # 持久化(jsonl)
    # ------------------------------------------------------------------

    def _append_jsonl(self, entry: dict[str, Any]) -> None:
        path = Path(self.jsonl_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _load(self) -> None:
        path = Path(self.jsonl_path)
        if not path.is_file():
            return
        with path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                raw = line.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                    if not isinstance(obj, dict) or any(k not in obj for k in _ENTRY_KEYS):
                        raise ValueError("缺少字段")
                    entry = {
                        "verdict": _coerce_verdict(obj["verdict"]),
                        "action": _coerce_action(obj["action"]),
                        "agg": round(float(obj["agg"]), 4),
                        "nsw_count": int(obj["nsw_count"]),
                    }
                except (ValueError, TypeError, json.JSONDecodeError):
                    logger.debug("跳过损坏的反馈记录:%s 第 %d 行", self.jsonl_path, lineno)
                    continue
                self._entries.append(entry)

    # ------------------------------------------------------------------
    # 阈值建议(核心:人工结论反哺)
    # ------------------------------------------------------------------

    def threshold_suggestions(self, cfg: Config) -> list[dict[str, Any]]:
        """基于已收集的 approve / reject 分布,产出阈值调整**建议**列表。

        样本不足(<20 条)时返回空列表 ``[]``:小样本下的中位数与占比
        噪声太大,给出建议反而误导运营(契约 §3 A44"返回空并说明")。

        规则(每条返回 ``{"param", "current", "suggested", "reason", "n"}``):

        ① 被人工驳回(reject)的 NSFW 样本 agg 中位数 < cfg.nsfw_threshold
          → 建议 nsfw_threshold 调整为该中位数(低分 NSFW 被人工驳回集中,
            说明阈值与人工判断的落点错位,应向驳回集中段对齐);
        ② 被驳回的 NSFW 样本 nsw_count 中位数 < cfg.min_nsw_images
          → 建议 min_nsw_images 上调 1(少量达标图即判高置信,易误报);
        ③ 疑似(suspect)条目中人工确认(approve)占比 > 0.6
          → 建议 review_threshold 下调 0.05(不低于 0.3;门槛偏严,
            把大量最终被确认的站点挡在了复核之外)。

        安全红线:返回值**仅为建议**,绝不自动修改 cfg 或任何生产配置;
        是否采纳由运营者人工评估后手动修改配置文件。

        V5 性能:三条规则所需的全部统计量(rejected NSFW 的 agg / nsw_count
        两组样本、疑似条目总数与确认数)在**一次**遍历内收集,替代旧实现的
        多次全列表扫描。
        """
        entries = self._entries
        if len(entries) < MIN_SAMPLES:
            return []

        suggestions: list[dict[str, Any]] = []

        # ---- 单遍统计:规则①②③ 的输入一次性收集 ----
        rejected_agg: list[float] = []
        rejected_count: list[int] = []
        suspect_total = 0
        suspect_approved = 0
        for e in entries:
            if e["verdict"] == "nsfw":
                if e["action"] == "reject":
                    rejected_agg.append(e["agg"])
                    rejected_count.append(e["nsw_count"])
            elif e["verdict"] == "suspect":
                suspect_total += 1
                if e["action"] == "approve":
                    suspect_approved += 1

        # ---- 规则①②:被驳回的 NSFW(机器误报)集中段 ----
        if rejected_agg:
            med_agg = statistics.median(rejected_agg)
            if med_agg < cfg.nsfw_threshold:
                suggestions.append({
                    "param": "nsfw_threshold",
                    "current": cfg.nsfw_threshold,
                    "suggested": round(float(med_agg), 4),
                    "reason": (
                        "低分 NSFW 被人工驳回集中:被驳回的 NSFW 样本 agg 中位数"
                        "低于当前 nsfw_threshold,建议将其调整为该中位数以对齐"
                        "人工判断落点(仅为建议,须人工评估后手动修改配置)"
                    ),
                    "n": len(rejected_agg),
                })
            med_count = statistics.median(rejected_count)
            if med_count < cfg.min_nsw_images:
                suggestions.append({
                    "param": "min_nsw_images",
                    "current": cfg.min_nsw_images,
                    "suggested": cfg.min_nsw_images + 1,
                    "reason": (
                        "被驳回的 NSFW 样本达标图片数中位数低于当前 min_nsw_images,"
                        "少量达标图即触发高置信判定容易误报,建议上调 1"
                        "(仅为建议,须人工评估后手动修改配置)"
                    ),
                    "n": len(rejected_count),
                })

        # ---- 规则③:疑似条目确认占比高 → 复核门槛偏严 ----
        if suspect_total and suspect_approved / suspect_total > SUSPECT_APPROVE_RATIO:
            suggested = max(round(cfg.review_threshold - REVIEW_STEP, 4), REVIEW_FLOOR)
            if suggested < cfg.review_threshold:
                suggestions.append({
                    "param": "review_threshold",
                    "current": cfg.review_threshold,
                    "suggested": suggested,
                    "reason": (
                        f"疑似(suspect)条目的人工确认占比超过 "
                        f"{SUSPECT_APPROVE_RATIO:.0%},review_threshold 偏严,"
                        f"建议下调 {REVIEW_STEP}(不低于 {REVIEW_FLOOR})"
                        "(仅为建议,须人工评估后手动修改配置)"
                    ),
                    "n": suspect_total,
                })

        telemetry.inc("learn.suggestions", len(suggestions))
        return suggestions


def rank_for_vlm(image_scores: list[ImageScore], budget: int) -> list[ImageScore]:
    """预算最优的 VLM 送审排序:不确定度 |p - 0.5| 升序(边缘优先),截预算。

    只取 ``model == "ensemble"`` 的条目作为候选(与 SiteReport 契约一致:
    ensemble 评分应已并入 image_scores;单模型成员分与 vlm-arbiter 仲裁分
    不参与排序)。不确定度最低(最接近 0.5)的图片信息量最大,优先送 VLM
    复核;高置信正/负样本留给人或直接跳过,从而在固定预算内最大化增益。

    :param image_scores: 图片评分列表(通常即 ``report.image_scores``);
    :param budget:       本次最多送审的图片数(VLM 费用保护,>0 才有效);
    :return: 排序后的新列表(元素为入参对象的引用,入参本身不被重排);
             空输入、预算 ≤0 或没有 ensemble 条目时返回 ``[]``。

    安全红线:本函数只做排序,不发起任何 VLM 调用(外呼与预算扣减由
    vlm_cache / orchestrator 统一管辖)。
    """
    if not image_scores or budget <= 0:
        return []
    ensemble = [s for s in image_scores if s.model == "ensemble"]
    if not ensemble:
        return []
    ordered = sorted(ensemble, key=lambda s: abs(float(s.nsfw_prob) - 0.5))
    return ordered[:budget]
