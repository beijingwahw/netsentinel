"""HuggingFace CLIP NSFW 分类器适配器(A07)。

- 使用模型 ``nateraw/nsfw-image-classification``(transformers 的
  ``image-classification`` pipeline,输出 ``nsfw`` / ``sfw`` 两类概率);
- transformers / torch 与 Pillow 一律**惰性导入**:模块导入期绝不触发
  重型依赖加载,更不会下载模型;依赖缺失时抛出带中文安装提示的
  ``ImportError``;
- 测试与离线开发可直接注入 fake pipeline(构造参数 ``pipeline``),
  本模块自身不发起任何网络请求;
- 线程安全(V5):pipeline 实例为共享单例,推理在实例锁内串行执行——
  **多线程批量时串行推理是模型侧要求**(torch 模型实例非线程安全);
- 批量(V5):注入协议是单图 ``__call__(image)``,classify_batch 保持
  逐张(见其 docstring 的协议决策说明)。

用法示例(离线注入)::

    from netsentinel.contracts import ImageEvidence
    from netsentinel.vision.hf_clip_adapter import HFCLIPClassifier

    clf = HFCLIPClassifier(pipeline=fake_pipeline)   # 测试注入替身
    score = clf.classify(ImageEvidence(path="a.png", url="u", source_page="p"))
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, ImageScore
from netsentinel.vision.classifier_base import NsfwClassifier, register_classifier

__all__ = ["HFCLIPClassifier"]

logger = logging.getLogger(__name__)

#: transformers / torch 缺失时的安装提示(对应 pyproject 的 clip 附加依赖组)。
_TRANSFORMERS_HINT = "未安装 transformers/torch,请 pip install 'netsentinel[clip]'"
#: Pillow(PIL)缺失时的安装提示。
_PIL_HINT = "未安装 Pillow(PIL),请 pip install Pillow"
#: HuggingFace 模型 ID。
_MODEL_ID = "nateraw/nsfw-image-classification"


class HFCLIPClassifier(NsfwClassifier):
    """基于 HuggingFace CLIP 的 NSFW 图像分类器(契约名 ``"clip"``)。"""

    name = "clip"

    def __init__(self, cfg: Config | None = None, pipeline: Any = None) -> None:
        """初始化分类器。

        :param cfg: 可选的全局配置(保留给后续阈值/设备等扩展,当前未使用)。
        :param pipeline: 可注入的图像分类 pipeline(测试/离线场景);
            为 ``None`` 时才惰性构建真实 transformers pipeline。
        :raises ImportError: 未注入 pipeline 且缺少 transformers/torch 时抛出,
            消息附带 ``pip install 'netsentinel[clip]'`` 安装提示。
        """
        self.cfg = cfg
        self._pipe = pipeline if pipeline is not None else self._load_pipeline()
        # 推理串行锁:多线程批量时串行推理是模型侧要求(共享 pipeline
        # 背后的 torch 模型实例并非线程安全),锁随实例创建、注入替身同样生效。
        self._infer_lock = threading.Lock()

    @staticmethod
    def _load_pipeline() -> Any:
        """惰性构建真实 transformers pipeline(此时才会导入 torch/transformers)。"""
        try:
            from transformers import pipeline as hf_pipeline
        except ImportError as exc:
            raise ImportError(_TRANSFORMERS_HINT) from exc
        return hf_pipeline("image-classification", model=_MODEL_ID)

    def classify(self, img: ImageEvidence) -> ImageScore:
        """对一张落盘图片给出 NSFW 概率。

        映射规则:输出中 ``label == "nsfw"`` 的 ``score`` 即 nsfw_prob;
        缺 ``nsfw`` 标签时取 ``1 - sfw_score``。文件不存在/损坏或推理失败
        时保守记 0 分并在 ``scores["error"]`` 记录原因(warning 日志)。

        推理调用在实例锁内串行执行(多线程批量时串行推理是模型侧要求),
        并记 telemetry 计时 ``clip.infer``;失败计 ``vision.errors``。
        """
        try:
            from PIL import Image
        except ImportError as exc:
            raise ImportError(_PIL_HINT) from exc

        try:
            image = Image.open(img.path).convert("RGB")
            with telemetry.timer("clip.infer"), self._infer_lock:
                results = self._pipe(image)
            nsfw_prob = self._prob_from_results(results)
        except Exception as exc:  # 文件不存在/损坏、推理异常等:保守 0 分,不中断流程
            telemetry.inc("vision.errors")
            logger.warning("CLIP 分类失败,已保守记 0 分: %s -> %r", img.path, exc)
            return ImageScore(
                image=img, model=self.name, scores={"error": str(exc)}, nsfw_prob=0.0
            )
        return ImageScore(
            image=img, model=self.name, scores={"raw": results}, nsfw_prob=nsfw_prob
        )

    def classify_batch(self, imgs: list[ImageEvidence]) -> list[ImageScore]:
        """批量打分:**保持逐张**串行推理(与基类默认同序、同语义)。

        协议决策(V5):本适配器的注入协议是单图 ``__call__(image)``(见
        既有 FakePipeline),classify_batch 因此保持逐张——真实 transformers
        pipeline 虽可接受图像列表,但注入替身无法表达逐图结果形状,强行
        批量会对替身协议做形状猜测、破坏离线可测性。每张各自 PIL 解码
        (线程间可并行)后进入共享锁串行推理:多线程批量时串行推理是
        模型侧要求。
        """
        return [self.classify(img) for img in imgs]

    @staticmethod
    def _prob_from_results(results: Any) -> float:
        """把 ``[{"label": "nsfw"/"sfw", "score": f}, ...]`` 映射为 nsfw 概率。"""
        nsfw_score: float | None = None
        sfw_score: float | None = None
        for entry in results or []:
            label = str(entry.get("label", "")).strip().lower()
            score = float(entry.get("score", 0.0))
            if label == "nsfw":
                nsfw_score = score
            elif label == "sfw":
                sfw_score = score
        if nsfw_score is not None:
            return max(0.0, min(1.0, nsfw_score))
        if sfw_score is not None:
            return max(0.0, min(1.0, 1.0 - sfw_score))
        logger.warning("CLIP 输出缺少 nsfw/sfw 标签,已保守记 0 分: %r", results)
        return 0.0


# 模块导入即注册,供 get_classifier("clip", cfg) 按名获取。
register_classifier("clip", HFCLIPClassifier)
