"""站点级判定公式(A09)。

唯一实现依据:CONTRACTS.md §4。对 ensemble 汇总后的图片评分做站点级判定:
CLEAN / SUSPECT / NSFW。注意安全红线:NSFW 也必须人工确认(needs_review=True)
之后才允许进入举报提交流程,本模块绝不自动触发任何举报动作。

用法示例(纯函数、离线)::

    from netsentinel.contracts import Config
    from netsentinel.decision.verdict import assess

    report = assess(site_url, pages, ensemble_scores, Config())
    print(report.verdict, report.needs_review, report.agg_nsw_prob)
"""
from __future__ import annotations

from netsentinel import telemetry
from netsentinel.contracts import (
    Config,
    ImageScore,
    PageSample,
    SiteReport,
    Verdict,
    now_iso,
)

__all__ = ["assess"]


def assess(
    site_url: str,
    pages: list[PageSample],
    ensemble: list[ImageScore],
    cfg: Config,
) -> SiteReport:
    """按契约 §4 公式生成站点级判定报告。

    - candidates:宽或高至少一边 >= cfg.min_image_px 的评分项(过滤图标等小图);
    - agg:候选中的最大 nsfw_prob(无候选时为 0.0);
    - count:候选中 nsfw_prob >= cfg.prob_count_line 的张数;
    - 判定:NSFW 需 agg >= nsfw_threshold 且 count >= min_nsw_images;
      否则 agg >= review_threshold 判 SUSPECT;再否则 CLEAN;
    - needs_review = (verdict != CLEAN),NSFW 同样须人工复核。

    性能(V5):候选过滤 / agg / count 在**单遍循环**内完成——旧实现先建
    candidates 中间列表再跑两趟聚合(3 遍);现用 None 哨兵精确复刻
    ``max(..., default=0.0)`` 的语义(含空候选返回 0.0、负分候选返回负最大值、
    NaN 比较短路等逐位一致),并消除中间列表的内存分配。
    """
    with telemetry.timer("verdict.assess"):
        min_px = cfg.min_image_px
        line = cfg.prob_count_line
        agg: float | None = None  # None = 尚无候选(max default=0.0 语义)
        count = 0
        # V5 单遍扫描锁定:过滤(宽或高 >= min_px)、agg(保序 max)、count 同循环完成。
        for s in ensemble:
            if s.image.width >= min_px or s.image.height >= min_px:
                prob = s.nsfw_prob
                if agg is None or prob > agg:  # 与 max() 同款比较方向
                    agg = prob
                if prob >= line:
                    count += 1
        if agg is None:
            agg = 0.0

        if agg >= cfg.nsfw_threshold and count >= cfg.min_nsw_images:
            verdict = Verdict.NSFW
        elif agg >= cfg.review_threshold:
            verdict = Verdict.SUSPECT
        else:
            verdict = Verdict.CLEAN

    telemetry.inc(f"verdict.{verdict.value}")  # verdict.clean / suspect / nsfw

    return SiteReport(
        site_url=site_url,
        pages=pages,
        image_scores=ensemble,
        agg_nsw_prob=agg,
        nsw_image_count=count,
        verdict=verdict,
        needs_review=verdict != Verdict.CLEAN,
        created_at=now_iso(),
    )
