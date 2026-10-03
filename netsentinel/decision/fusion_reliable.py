"""融合内核·可靠性加权融合(V7 · A126,CONTRACTS-V7.md §2)。

与 :func:`netsentinel.decision.fusion.fuse` **同构**的可解释特征融合,
唯一差异在图像侧:不再直接采用 ``report.agg_nsw_prob``(ensemble 等权
口径),而是把 ``report.image_scores`` 里的**各成员模型评分**按提供方
可靠性(:class:`~netsentinel.decision.reliability.ReliabilityTracker` 的
Brier 反比权重)加权合成 ``agg_reliable``,以之作为图像特征进入 logit
融合——报得准的平台话语权大,报得离谱的平台话语权小。

成员认定与权重:

- 成员分 = ``model != "ensemble"`` 的评分条目(ensemble 聚合条目跳过,
  否则自我重复计数);提供方 = 模型名**冒号前缀**(``"glm:glm-5.3"`` →
  ``"glm"``;无冒号原样,如 ``"stub"`` / ``"nudenet"``);
- 同一提供方多条评分(多图)先按提供方取均值,再按可靠性加权平均——
  权重是"平台话语权",不随其评分张数放大;
- ``tracker.weights()`` 中样本充足的提供方按归一权重参与;**缺失 / None
  (样本不足)的成员按已知成员权重的均值参与**(平均可信度);一个可用
  权重都没有(含 ``tracker=None`` / 成员分全缺席)时全体等权——数值上
  即成员均值,与旧行为一致。``member_weights`` 记录各成员归一后的话语权
  占比(Σ = 1),供解释与审计。

同构复用(契约 §2:兄弟模块只读、一律惰性导入;本函数不 import 就不改
fusion 的任何行为):权重常量 ``fusion.WEIGHTS`` / ``fusion.BIAS``、缺失
剔除后的归一化(按原始总权重回补)、page_vlm 缺分按 0.5 中性计且权重
减半、数值稳定 sigmoid、只升不降,全部照搬 :mod:`netsentinel.decision.fusion`。

只升不降(与 fusion 同构 + 安全底线):

- ``fused_final = max(fused, agg_reliable)``:融合概率不低于(可靠性加权
  口径下的)图像侧结论;
- verdict 档位不低于 ``report`` 原档位:可靠性重加权可能让图像分**如实**
  下移(报得离谱的成员被降权),但**绝不能把图像已判 NSFW / SUSPECT 的
  站点洗成 CLEAN**;needs_review 单向恒真;
- 不改既有字段语义:``report.agg_nsw_prob`` / ``nsw_image_count`` /
  ``image_scores`` 保持原值不动(与 fusion 口径一致,``agg_nsw_prob``
  恒为图像 ensemble 原值);可靠性视角单列在 ``intel["fusion"]`` 的新增键
  ``agg_reliable`` / ``member_weights``,``rule`` 注明
  ``"reliable-weighted 只升不降"``。

零外呼(红线 30):不联网、不调 VLM,只消费调用方传入的本地 tracker 与
报告;开关 ``cfg.use_reliability_fusion`` 由装配线
(:mod:`netsentinel.pipeline.kernel_wire`,A139)把关,本模块不读开关、
始终可离线调用。遥测:``fusion.fuse_reliable`` 计时、
``fusion.escalated_reliable`` 升级计数(与既有 fusion 指标分列,互不污染)。

V12 增量(A216,opt-in):``bayes_tracker`` 参数注入
:class:`~netsentinel.decision.reliability.BayesianReliabilityTracker`
(分层 Beta 后验 + 指数遗忘 + 冷启动收缩)。**缺省 ``None`` = 现 Brier
反比路径逐字节不变**;注入时 ``bayes_tracker.weights()`` 优先于
``tracker.weights()``,并在 ``intel["fusion"]`` 多记一钥
``weight_model = "bayes"``(默认路径无此键,输出形状不变)。

用法::

    from netsentinel.decision.fusion_reliable import fuse_reliable

    report = fuse_reliable(report, url_feat, text_feat, page_vlm, cfg,
                           tracker=tracker)
    report.intel["fusion"]["agg_reliable"]    # 可靠性加权后的图像侧概率
    report.intel["fusion"]["member_weights"]  # {"glm": 0.95, "stub": 0.05}
"""
from __future__ import annotations

from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config, SiteReport, Verdict
from netsentinel.decision.reliability import (
    BayesianReliabilityTracker,
    ReliabilityTracker,
)

__all__ = ["fuse_reliable"]

#: 融合解释文案(写入 intel["fusion"]["rule"];契约 §2 A126 注明 reliable-weighted)
_RULE_TEXT = "reliable-weighted 只升不降"

#: 无辅助特征时的说明文案(与 fusion 同义,标明可靠性加权口径)
_NO_AUX_NOTE = "无辅助特征:融合概率等于图像概率(可靠性加权口径)"


def _provider_of(model_name: str) -> str:
    """提供方 = 模型名冒号前缀(``"glm:glm-5.3"`` → ``"glm"``;无冒号原样)。"""
    if ":" in model_name:
        return model_name.split(":", 1)[0]
    return model_name


def _member_agg(
    report: SiteReport,
    tracker: ReliabilityTracker | None,
    bayes_tracker: BayesianReliabilityTracker | None = None,
) -> tuple[float, dict[str, float]]:
    """按提供方可靠性加权合成图像侧 agg。

    :param bayes_tracker: V12 opt-in 贝叶斯追踪器;非 ``None`` 时其
        ``weights()``(分层收缩后验均值,无 None 值)**优先于** Brier
        ``tracker.weights()``;两者都缺 = 等权(旧行为)。
    :return: ``(agg, member_weights)``;agg 为 [0,1] 加权均值
        (无成员分时回退 ``report.agg_nsw_prob``,与 fusion.fuse 完全一致);
        member_weights 为各参与提供方**归一后的话语权占比**(Σ = 1,
        加权均值对整体缩放不变,展示口径与 weights() 一致),供 intel 解释。
    """
    # 单遍分组:按提供方收集成员分(ensemble 条目跳过),记录首现顺序
    probs: dict[str, list[float]] = {}
    order: list[str] = []
    for s in report.image_scores:
        if s.model == "ensemble":
            continue  # 已聚合条目,跳过以免自我重复计数
        prov = _provider_of(s.model)
        if prov not in probs:
            probs[prov] = []
            order.append(prov)
        probs[prov].append(s.nsfw_prob)

    if not probs:
        # 没有成员分(默认 v6/v7 关闭链路即如此):退化为旧行为
        return float(report.agg_nsw_prob), {}

    # 提供方均值:权重是平台话语权,不随评分张数放大
    means = {prov: sum(probs[prov]) / len(probs[prov]) for prov in order}

    if bayes_tracker is not None:
        w_map: dict[str, Any] = bayes_tracker.weights()
    elif tracker is not None:
        w_map = tracker.weights()
    else:
        w_map = {}
    usable: dict[str, float] = {}
    for prov in order:
        w = w_map.get(prov)
        if isinstance(w, (int, float)) and not isinstance(w, bool):
            usable[prov] = float(w)

    if not usable:
        # 一个可用权重都没有(含 tracker=None):全体等权 = 旧行为
        weights_used = {prov: 1.0 for prov in order}
    else:
        # 样本不足 / 未知的成员按已知成员权重的均值参与(平均可信度)
        fallback = sum(usable.values()) / len(usable)
        weights_used = {prov: usable.get(prov, fallback) for prov in order}

    total_w = sum(weights_used.values())  # 权重恒正,无需防零
    agg = sum(weights_used[prov] * means[prov] for prov in order) / total_w
    # 展示口径:归一到 Σ=1(话语权占比;加权均值对整体缩放不变)
    share = {prov: weights_used[prov] / total_w for prov in order}
    return agg, share


def fuse_reliable(
    report: SiteReport,
    url_feat: dict[str, Any],
    text_feat: dict[str, Any],
    page_vlm: dict[str, Any],
    cfg: Config,
    *,
    tracker: ReliabilityTracker | None = None,
    bayes_tracker: BayesianReliabilityTracker | None = None,
) -> SiteReport:
    """可靠性加权的可解释特征融合(就地更新并返回同一 SiteReport 对象)。

    参数与 :func:`netsentinel.decision.fusion.fuse` 相同,额外:

    :param tracker: Brier 反比可靠性追踪器(keyword-only);``None`` 或其中
        无本站成员的可用权重时,成员按等权聚合 = 旧行为。
    :param bayes_tracker: V12 opt-in 贝叶斯分层可靠性追踪器(keyword-only,
        A216);缺省 ``None`` = Brier 路径**逐字节不变**。非 ``None`` 时
        其 ``weights()`` 优先于 ``tracker``(两者同传以贝叶斯为准),
        且 ``intel["fusion"]`` 追加 ``weight_model = "bayes"`` 一键;
        冷启动成员由收缩吸收(权重恒为数值,无 None 回退歧义)。

    融合流程(与 fuse 同构,差异仅图像特征取 ``agg_reliable``):

        1. 成员分按提供方可靠性加权 → ``agg_reliable``
           (无成员分时即 ``report.agg_nsw_prob``,行为零差异);
        2. 各路辅助特征在席判断 / 取值 / 有效权重(缺失剔除、page_vlm
           缺分减半、按原始总权重归一化)——同 fuse;
        3. ``z = BIAS + Σ w_i·x_i``,``fused = sigmoid(z)``;全缺时
           ``fused = agg_reliable``;同 fuse;
        4. 只升不降:``fused_final = max(fused, agg_reliable)``;verdict 按
           阈值重算(NSFW 档仍要求 ``nsw_image_count >= min_nsw_images``)
           且档位不低于原档位;needs_review 单向;
        5. 写入 intel(三路情报原文 + fusion 解释,新增 agg_reliable /
           member_weights 两键,rule = "reliable-weighted 只升不降");
           ``agg_nsw_prob`` 等图像侧字段保持原值不动。

    返回:同一 SiteReport 对象(id 不变)。
    """
    # 兄弟模块只读、一律惰性导入(契约 §2);fusion 是冻结模块,
    # 复用其常量与私有辅助保证与 fuse 数值逐位同构。
    from netsentinel.decision import fusion as _fusion

    with telemetry.timer("fusion.fuse_reliable"):
        # ---- 0. 图像特征 = 成员模型可靠性加权 agg(本内核的核心差异) ----
        agg_reliable, member_w = _member_agg(report, tracker, bayes_tracker)
        x_image = _fusion._norm(agg_reliable)

        # ---- 1. 各路辅助特征:是否在席(空 dict 视为缺失)、取值、有效权重 ----
        url_present = bool(url_feat)
        text_present = bool(text_feat)
        page_present = bool(page_vlm)  # 传入 None 也按缺失处理(防御式)

        x_url = _fusion._risk(url_feat) if url_present else 0.0
        x_text = _fusion._risk(text_feat) if text_present else 0.0

        page_neutral = False
        x_page = _fusion._NEUTRAL_PROB
        if page_present:
            raw = _fusion._numeric_or_none(page_vlm.get("page_nsfw_prob"))
            if raw is None:
                page_neutral = True  # 缺分:按 0.5 中性计,权重减半
            else:
                x_page = _fusion._norm(raw)

        w: dict[str, float] = {
            "image": _fusion.WEIGHTS["image"],
            "page_vlm": 0.0,
            "url": 0.0,
            "text": 0.0,
        }
        x: dict[str, float] = {
            "image": x_image,
            "page_vlm": x_page,
            "url": x_url,
            "text": x_text,
        }
        if page_present:
            w["page_vlm"] = _fusion.WEIGHTS["page_vlm"] * (
                0.5 if page_neutral else 1.0
            )
        if url_present:
            w["url"] = _fusion.WEIGHTS["url"]
        if text_present:
            w["text"] = _fusion.WEIGHTS["text"]

        # ---- 2. 融合概率(归一化口径与 fuse 逐位一致) ----
        if not (url_present or text_present or page_present):
            weights_used: dict[str, float] = {
                "image": 1.0,
                "page_vlm": 0.0,
                "url": 0.0,
                "text": 0.0,
            }
            contrib: dict[str, float] = {
                "image": round(x_image, 4),
                "page_vlm": 0.0,
                "url": 0.0,
                "text": 0.0,
            }
            fused = x_image
            note = _NO_AUX_NOTE
        else:
            eff_sum = sum(w.values())
            any_removed = (not url_present) or (not text_present) or (not page_present)
            scale = (
                (_fusion._TOTAL_WEIGHT / eff_sum)
                if (any_removed and eff_sum > 0.0)
                else 1.0
            )
            products = {k: wk * scale * x[k] for k, wk in w.items()}
            weights_used = {k: round(v * scale, 4) for k, v in w.items()}
            contrib = {k: round(products[k], 4) for k in products}
            z = _fusion.BIAS + sum(products.values())
            fused = _fusion._sigmoid(z)
            note = ""

        # ---- 3. 只升不降:融合概率不低于(可靠性口径)图像侧,档位不低于原档 ----
        fused_final = max(fused, x_image)

        if (
            fused_final >= cfg.nsfw_threshold
            and report.nsw_image_count >= cfg.min_nsw_images
        ):
            verdict_new = Verdict.NSFW
        elif fused_final >= cfg.review_threshold:
            verdict_new = Verdict.SUSPECT
        else:
            verdict_new = Verdict.CLEAN
        if _fusion._VERDICT_ORDER[verdict_new] < _fusion._VERDICT_ORDER[report.verdict]:
            verdict_new = report.verdict  # 档位只升不降:可靠性重加权不能洗白图像判定
        elif (
            _fusion._VERDICT_ORDER[verdict_new] > _fusion._VERDICT_ORDER[report.verdict]
        ):
            telemetry.inc("fusion.escalated_reliable")

        # ---- 4. needs_review 单向:新判定非 CLEAN 即复核;原 True 恒保留 ----
        needs_review = (verdict_new != Verdict.CLEAN) or report.needs_review

        # ---- 5. 写入可解释 intel 并就地更新(agg_nsw_prob 等图像侧字段不动) ----
        fusion_intel: dict[str, Any] = {
            "prob": round(fused_final, 4),
            "raw_prob": round(fused, 4),
            "contrib": contrib,
            "weights_used": weights_used,
            "rule": _RULE_TEXT,
            "agg_reliable": round(x_image, 4),
            "member_weights": {prov: round(wv, 4) for prov, wv in member_w.items()},
        }
        if bayes_tracker is not None:
            # V12 opt-in 标记:仅贝叶斯路径出现,默认路径输出形状逐字节不变
            fusion_intel["weight_model"] = "bayes"
        if note:
            fusion_intel["note"] = note

        report.intel = {
            "url": url_feat,
            "text": text_feat,
            "page_vlm": page_vlm,
            "fusion": fusion_intel,
        }
        report.verdict = verdict_new
        report.needs_review = needs_review
        # agg_nsw_prob / nsw_image_count / image_scores 等图像侧字段保持原值不动
        return report
