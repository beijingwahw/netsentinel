"""离线规则分类器(stub):按文件名关键词给出固定分数。

仅供离线开发与单元测试使用,不解码图像、不发起网络请求:
- 文件名(basename,转小写)含 ``nsfw_hi``  → nsfw_prob = 0.97
- 文件名(basename,转小写)含 ``nsfw_mid`` → nsfw_prob = 0.72
- 其余                                        → nsfw_prob = 0.02

用法示例::

    from netsentinel.contracts import Config, ImageEvidence
    from netsentinel.vision.stub_classifier import StubClassifier

    clf = StubClassifier(Config())
    score = clf.classify(ImageEvidence(path="data/x_nsfw_hi.jpg", url="..."))
    assert score.nsfw_prob == 0.97

模块导入时自动以 "stub" 注册到 classifier_base 的注册表。
"""
from __future__ import annotations

import logging
import os

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, ImageScore
from netsentinel.vision.classifier_base import NsfwClassifier, register_classifier

__all__ = ["StubClassifier"]

logger = logging.getLogger(__name__)

#: 文件名含 ``nsfw_hi`` 关键词时的固定分值(高风险桩分)。
PROB_NSFW_HI: float = 0.97
#: 文件名含 ``nsfw_mid`` 关键词时的固定分值(中风险桩分)。
PROB_NSFW_MID: float = 0.72
#: 其余文件名的固定分值(干净桩分)。
PROB_CLEAN: float = 0.02


class StubClassifier(NsfwClassifier):
    """基于文件名关键词的桩分类器(离线开发/测试专用)。"""

    name = "stub"

    def __init__(self, cfg: Config | None = None) -> None:
        # 桩分类器不读取配置;保留入参以满足工厂统一构造约定 cls(cfg)。
        self.cfg = cfg

    def classify(self, img: ImageEvidence) -> ImageScore:
        """按 basename(转小写)中的关键词打分。

        示例:``classify(ImageEvidence(path="img/a_nsfw_mid.jpg", ...))``
        → ``ImageScore(nsfw_prob=0.72, scores={"hint": "nsfw_mid"})``。
        """
        filename = os.path.basename(img.path).lower()
        if "nsfw_hi" in filename:
            prob = PROB_NSFW_HI
            hint = "nsfw_hi"
        elif "nsfw_mid" in filename:
            prob = PROB_NSFW_MID
            hint = "nsfw_mid"
        else:
            prob = PROB_CLEAN
            hint = "none"
        logger.debug("stub 打分:path=%s hint=%s prob=%s", img.path, hint, prob)
        telemetry.inc("stub.classify")
        return ImageScore(
            image=img,
            model=self.name,
            scores={"hint": hint},
            nsfw_prob=prob,
        )


register_classifier("stub", StubClassifier)
