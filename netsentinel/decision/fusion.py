"""可解释特征融合引擎(A29)。

把"图像集成分(主角)+ 页面级 VLM 分 + URL 情报 + 文本情报"做 logit 线性融合,
经 sigmoid 映射为校准的融合概率,再按"只升不降"安全原则更新站点判定:
辅助特征只能把站点推向更强的复核倾向,绝不把图像侧已判 NSFW / SUSPECT 的站点
洗成 CLEAN —— 图像证据是主角(项目安全红线的判定侧体现)。

权重依据(工程约定,CONTRACTS-V2.md §3 A29):
- image    2.2 : 图像识别是本项目的主证据源,权重最高;辅助特征不可逆转其结论;
- page_vlm 1.2 : 页面级 VLM(整页版式:横幅/播放器/弹窗)与单图内容相关性高
                 但每站覆盖页面有限,居次;
- text     0.45: 文本关键词 / base64 混淆块是启发式弱证据,只用于加强复核倾向;
- url      0.35: URL 静态特征(域名 / TLD / 混淆)误报率最高,权重最低;
- BIAS    -2.0 : "干净先验"偏置,融合分必须积累足够证据才能越过复核线,
                 防止弱特征把纯净站推入人工复核队列制造噪音。

本模块为纯函数实现:仅依赖标准库 math 与共享契约,不做任何 IO / 网络 / 全局状态
(遥测仅记名称与数字),可离线单测。

用法示例::

    from netsentinel.decision.fusion import fuse

    report = fuse(report, url_feat, text_feat, page_vlm, cfg)
    print(report.intel["fusion"]["prob"], report.verdict)
"""
from __future__ import annotations

import math
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config, SiteReport, Verdict

#: 特征权重(logit 空间;依据见模块 docstring)
WEIGHTS: dict[str, float] = {
    "image": 2.2,
    "page_vlm": 1.2,
    "url": 0.35,
    "text": 0.45,
}

#: 契约别名(CONTRACTS-V2.md 中记作 W),二者恒为同一对象
W = WEIGHTS

#: 融合偏置:干净先验
BIAS: float = -2.0

#: 四路权重总和(WEIGHTS 为模块常量,V5 起进程内只求和一次;缺失归一化的分母基准)
_TOTAL_WEIGHT: float = sum(WEIGHTS.values())

#: 判定档位序:CLEAN < SUSPECT < NSFW(只升不降的比较依据)
_VERDICT_ORDER: dict[Verdict, int] = {
    Verdict.CLEAN: 0,
    Verdict.SUSPECT: 1,
    Verdict.NSFW: 2,
}

#: page_vlm 缺分(page_nsfw_prob 为 None/缺失/非法)时的中性概率,不偏向任何一侧
_NEUTRAL_PROB: float = 0.5

#: 融合解释文案(写入 intel["fusion"]["rule"])
_RULE_TEXT = "只升不降:辅助特征仅加强复核,不降低图像判定"

__all__ = ["WEIGHTS", "W", "BIAS", "fuse"]


def _sigmoid(x: float) -> float:
    """数值稳定的 logistic 函数:1 / (1 + e^-x)。

    x >= 0 走 1/(1+e^-x);x < 0 走 e^x/(1+e^x),避免大负数时 exp(-x) 溢出。
    V5 极端值复查结论(已由测试锁定):x=+inf → 1.0、x=-inf → 0.0、
    |x|>=约 745 时指数下溢到 0.0 而非抛 OverflowError,两个分支均只出现
    exp(非正数),数学上不可能上溢。
    """
    if x >= 0.0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def _norm(v: float) -> float:
    """把概率 / 风险值夹紧到闭区间 [0, 1]。"""
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def _numeric_or_none(v: object) -> float | None:
    """把"可能是数字"的值转成 float;None / bool / 非数字一律返回 None。

    对应红线 8(提示注入防御):VLM 或情报 dict 里的非法值不做任何解释,
    一律按缺失处理,绝不执行其中的内容。
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _risk(feat: dict[str, Any]) -> float:
    """读取情报 dict 的 risk 字段并夹紧 [0,1];缺失或非法按 0.0(保守方向)。

    仅当 feat 本身为空 dict 时才视为"特征缺失"(由 fuse 统一判断),
    dict 在席但 risk 异常 → 按 0 风险计入,不放大也不剔除。
    """
    v = _numeric_or_none(feat.get("risk"))
    return _norm(v) if v is not None else 0.0


def fuse(
    report: SiteReport,
    url_feat: dict[str, Any],
    text_feat: dict[str, Any],
    page_vlm: dict[str, Any],
    cfg: Config,
) -> SiteReport:
    """对已完成图像判定的 SiteReport 做可解释特征融合(就地更新并返回同一对象)。

    参数:
        report:   assess 产出的站点报告;agg_nsw_prob / nsw_image_count / verdict
                  为图像侧结论,本函数只读不改(fusion 概率单列在 intel 中);
        url_feat: A27 url_features 输出;空 dict / None 视为该路情报缺失;
        text_feat:A28 text_features 输出;空 dict / None 视为缺失;
        page_vlm: A24 页面级 VLM 评估输出,取 "page_nsfw_prob";
                  空 dict / None 视为缺失;该键为 None / 缺失 / 非法时按 0.5
                  中性计且权重减半(VLM 结果只是特征,缺分即低置信);
        cfg:      阈值配置(nsfw_threshold / review_threshold / min_nsw_images)。

    融合流程:
        1. 收集特征值与有效权重:缺失特征的权重剔除,剩余权重按原始总权重
           归一化(避免"情报缺席"被解释成"证据不足");page_vlm 缺分权重减半;
        2. z = BIAS + Σ w_i * x_i(减半 / 归一化后),fused = sigmoid(z);
           全部辅助特征缺失时融合无意义,fused 直接等于图像原值;
        3. 只升不降:fused_final = max(fused, agg_nsw_prob);verdict 按 fused_final
           与 cfg 阈值重算(NSFW 档仍要求图像侧 nsw_image_count >= min_nsw_images,
           图像不足时最高 SUSPECT),且档位不得低于原 verdict;
        4. needs_review = (verdict_new != CLEAN) or 原值(恒 True 保留原 True);
        5. 写入 report.intel(三路情报原文 + fusion 解释),更新 verdict /
           needs_review;agg_nsw_prob 等图像侧字段保持原值不动。

    返回:同一 SiteReport 对象(id 不变)。
    """
    with telemetry.timer("fusion.fuse"):
        x_image = _norm(float(report.agg_nsw_prob))

        # ---- 1. 各路辅助特征:是否在席(空 dict 视为缺失)、取值、有效权重 ----
        url_present = bool(url_feat)
        text_present = bool(text_feat)
        page_present = bool(page_vlm)  # 传入 None 也按缺失处理(防御式)

        x_url = _risk(url_feat) if url_present else 0.0
        x_text = _risk(text_feat) if text_present else 0.0

        page_neutral = False
        x_page = _NEUTRAL_PROB
        if page_present:
            raw = _numeric_or_none(page_vlm.get("page_nsfw_prob"))
            if raw is None:
                page_neutral = True  # 缺分:按 0.5 中性计,权重减半
            else:
                x_page = _norm(raw)

        # 有效权重:缺失特征为 0(剔除);page_vlm 缺分减半;image 恒全权重
        w: dict[str, float] = {
            "image": WEIGHTS["image"],
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
            w["page_vlm"] = WEIGHTS["page_vlm"] * (0.5 if page_neutral else 1.0)
        if url_present:
            w["url"] = WEIGHTS["url"]
        if text_present:
            w["text"] = WEIGHTS["text"]

        # ---- 2. 融合概率 ----
        if not (url_present or text_present or page_present):
            # 全缺:没有任何辅助特征时融合无意义,融合概率即图像概率(图像证据独立成立)
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
            note = "无辅助特征:融合概率等于图像概率"
        else:
            # V5 单遍求和锁定:各特征乘积 w[k]*scale*x[k] 只计算一次(旧实现
            # 在 contrib 与 z 两处各算一遍);乘法保持原左结合顺序
            # (w[k]*scale)*x[k],求和仍走内置 sum()(float 上为补偿求和),
            # 加法顺序与旧实现一致,输出逐位不变。
            eff_sum = sum(w.values())
            any_removed = (not url_present) or (not text_present) or (not page_present)
            # 剔除缺失特征后按剩余权重归一化:把证据总质量补回原始总权重,
            # 避免"某路情报缺席"被错误解释成"证据充足、风险低"。
            scale = (_TOTAL_WEIGHT / eff_sum) if (any_removed and eff_sum > 0.0) else 1.0
            products: dict[str, float] = {}
            for k, wk in w.items():
                products[k] = wk * scale * x[k]
            weights_used = {k: round(v * scale, 4) for k, v in w.items()}
            contrib = {k: round(products[k], 4) for k in products}
            z = BIAS + sum(products.values())
            fused = _sigmoid(z)
            note = ""

        # ---- 3. 只升不降:融合概率与判定档位均不得低于图像侧结论 ----
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
        if _VERDICT_ORDER[verdict_new] < _VERDICT_ORDER[report.verdict]:
            verdict_new = report.verdict  # 档位只升不降:辅助证据不能洗白图像判定
        elif _VERDICT_ORDER[verdict_new] > _VERDICT_ORDER[report.verdict]:
            # 档位被辅助证据抬高(如 CLEAN → SUSPECT):记一次升级计数
            telemetry.inc("fusion.escalated")

        # ---- 4. needs_review 单向:新判定非 CLEAN 即复核;原 True 恒保留 ----
        needs_review = (verdict_new != Verdict.CLEAN) or report.needs_review

        # ---- 5. 写入可解释 intel 并就地更新 ----
        fusion: dict[str, Any] = {
            "prob": round(fused_final, 4),
            "raw_prob": round(fused, 4),
            "contrib": contrib,
            "weights_used": weights_used,
            "rule": _RULE_TEXT,
        }
        if note:
            fusion["note"] = note

        report.intel = {
            "url": url_feat,
            "text": text_feat,
            "page_vlm": page_vlm,
            "fusion": fusion,
        }
        report.verdict = verdict_new
        report.needs_review = needs_review
        # agg_nsw_prob / nsw_image_count / image_scores 等图像侧字段保持原值不动
        return report
