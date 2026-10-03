"""NudeNet 适配器:把 NudeDetector 的检测框映射为 NetSentinel 的 ImageScore。

设计要点:
- 真实 nudenet 三方库惰性导入(构造分类器时才 import),缺失时抛出带安装提示的
  ImportError;模块本身被 import 不会触发任何模型下载。
- 测试一律注入 fake detector(构造参数 detector=...),完全离线。
- 检测标签 → 权重表见 LABEL_WEIGHTS;单图 nsfw_prob 取所有检测框加权后的最大值。
- 线程安全:检测器实例为共享单例,推理(含批量)一律在实例锁内串行执行——
  **多线程批量时串行推理是模型侧要求**(底层 ONNX 会话非线程安全)。
- 批量:classify_batch 优先复用底层 ``detect_batch(paths)`` 一次传入全部
  路径(检测到该方法时);注入替身的单图协议(detect(path))或批量失败时
  回退逐张,单图失败不中断整批。

用法示例(离线注入)::

    from netsentinel.contracts import ImageEvidence
    from netsentinel.vision.nudenet_adapter import NudeNetClassifier

    clf = NudeNetClassifier(detector=fake_detector)     # 测试注入替身
    scores = clf.classify_batch([ImageEvidence(path="a.png", url="u", source_page="p")])
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, ImageScore

__all__ = [
    "LABEL_WEIGHTS",
    "NEUTRAL_LABELS",
    "NudeNetClassifier",
]

logger = logging.getLogger(__name__)

try:  # 正常情况基类必在;并行开发期 A05 未就位时静默降级(不注册)
    from netsentinel.vision.classifier_base import NsfwClassifier, register_classifier
except ImportError:  # pragma: no cover - 仅并行开发期出现
    NsfwClassifier = object  # type: ignore[assignment,misc]
    register_classifier = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# 标签加权表:NudeNet 检测标签 → 该标签单独出现时的基础权重
# NudeNet 不同版本标签拼写不一(EXPOSED_BUTTOCKS / EXPOSED_GLUTEUS),分支兼容
# ---------------------------------------------------------------------------
LABEL_WEIGHTS: dict[str, float] = {
    "EXPOSED_BREAST_F": 0.93,
    "EXPOSED_BREAST_M": 0.93,
    "EXPOSED_GENITALIA_F": 0.97,
    "EXPOSED_GENITALIA_M": 0.97,
    "EXPOSED_BUTTOCKS": 0.85,  # 旧版 NudeNet 标签
    "EXPOSED_GLUTEUS": 0.85,   # NudeNet 2.x 起的标签
    "EXPOSED_ANUS": 0.98,
}

EXPOSED_PREFIX = "EXPOSED_"
COVERED_PREFIX = "COVERED_"
EXPOSED_FALLBACK_WEIGHT = 0.80   # 其余未列名的 EXPOSED_* 标签
COVERED_WEIGHT = 0.05            # 一切 COVERED_* 标签
NEUTRAL_WEIGHT = 0.10            # 中性身体部位
NEUTRAL_LABELS = frozenset(
    {"BELLY", "BUTTOCKS", "ARMPITS", "FEET", "FACE_F", "FACE_M", "FEMALE_FACE", "MALE_FACE"}
)
UNKNOWN_WEIGHT = 0.05            # 未知标签:宁可低权,不误伤
NO_DETECTION_PROB = 0.01         # 无任何检测框时的兜底概率
MAX_DETECTIONS_IN_SCORES = 10    # scores["detections"] 只保留前 10 条


def _label_weight(label: str) -> float:
    """查标签权重:精确表 → EXPOSED_/COVERED_ 前缀 → 中性标签 → 未知。"""
    normalized = label.strip().upper()
    if normalized in LABEL_WEIGHTS:
        return LABEL_WEIGHTS[normalized]
    if normalized.startswith(EXPOSED_PREFIX):
        return EXPOSED_FALLBACK_WEIGHT
    if normalized.startswith(COVERED_PREFIX):
        return COVERED_WEIGHT
    if normalized in NEUTRAL_LABELS:
        return NEUTRAL_WEIGHT
    return UNKNOWN_WEIGHT


def _detection_label(detection: dict[str, Any]) -> str:
    """取检测标签:NudeNet 新版字段为 class,旧版为 label。"""
    value = detection.get("class") or detection.get("label") or ""
    return str(value)


def _detection_score(detection: dict[str, Any]) -> float:
    """取检测置信度并防御性转 float(异常按 0.0 处理)。"""
    try:
        return float(detection.get("score", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


class NudeNetClassifier(NsfwClassifier):
    """基于 NudeNet NudeDetector 的 NSFW 分类器,注册名 "nudenet"。"""

    name = "nudenet"

    def __init__(self, cfg: Config | None = None, detector: Any = None) -> None:
        self._cfg = cfg
        # 推理串行锁:多线程批量时串行推理是模型侧要求(共享的底层
        # ONNX 会话/检测器实例并非线程安全),锁随实例创建、注入替身同样生效。
        self._infer_lock = threading.Lock()
        if detector is not None:
            self._detector = detector  # 测试注入 fake,绝不触碰真实模型
        else:
            self._detector = self._create_detector()

    @staticmethod
    def _create_detector() -> Any:
        """惰性导入并构造真实 NudeDetector;未安装时给出中文安装提示。"""
        try:
            from nudenet import NudeDetector
        except ImportError as exc:
            raise ImportError(
                "未安装 nudenet,请 pip install 'netsentinel[vision]' 或 pip install nudenet"
            ) from exc
        return NudeDetector()

    def classify(self, img: ImageEvidence) -> ImageScore:
        """对单张图打分:nsfw_prob = max(min(1.0, weight * (0.5 + 0.5*score)))。

        推理在实例锁内串行执行(多线程批量时串行推理是模型侧要求),
        并记 telemetry 计时 ``nudenet.infer``;单图失败计 ``vision.errors``。
        """
        try:
            with telemetry.timer("nudenet.infer"), self._infer_lock:
                detections = self._detector.detect(img.path)
        except Exception as exc:  # noqa: BLE001 - 单图检测失败不得中断整站扫描
            telemetry.inc("vision.errors")
            logger.warning("NudeNet 检测失败 图片=%s 错误=%s", img.path, exc)
            return ImageScore(
                image=img,
                model=self.name,
                scores={"error": str(exc)},
                nsfw_prob=0.0,
            )
        return self._score_from_detections(img, detections)

    def classify_batch(self, imgs: list[ImageEvidence]) -> list[ImageScore]:
        """批量打分:支持批量协议时一次传入,否则逐张串行(均保持输入顺序)。

        协议决策(V5):底层 NudeDetector 若提供 ``detect_batch(paths)``
        (nudenet 3.x 起)则**一次传入全部路径**(锁内单次调用,消除逐张的
        锁开销与调用间隙);注入替身的协议是单图 ``detect(path)``,此时保持
        逐张——docstring 注明而非强行猜测批量语义。批量调用异常或返回长度
        与输入不符时回退逐张,单图失败仍不中断整批。
        """
        if not imgs:
            return []
        detect_batch = getattr(self._detector, "detect_batch", None)
        if callable(detect_batch):
            try:
                with telemetry.timer("nudenet.infer"), self._infer_lock:
                    results = detect_batch([img.path for img in imgs])
            except Exception as exc:  # noqa: BLE001 - 批量失败回退逐张,不中断
                telemetry.inc("vision.errors")
                logger.warning(
                    "NudeNet 批量推理失败,回退逐张(共 %d 张): %s", len(imgs), exc
                )
            else:
                if isinstance(results, list) and len(results) == len(imgs):
                    return [
                        self._score_from_detections(img, detections)
                        for img, detections in zip(imgs, results)
                    ]
                logger.warning(
                    "NudeNet detect_batch 返回形状异常(期望 %d 项,实得 %r),回退逐张",
                    len(imgs),
                    type(results).__name__ if not isinstance(results, list) else len(results),
                )
        return [self.classify(img) for img in imgs]

    @staticmethod
    def _score_from_detections(
        img: ImageEvidence, detections: list[dict[str, Any]] | Any
    ) -> ImageScore:
        """把一次检测输出(可能为空/None)映射为 ImageScore,规则与单图完全一致。"""
        if not detections:
            return ImageScore(
                image=img,
                model=NudeNetClassifier.name,
                scores={"detections": []},
                nsfw_prob=NO_DETECTION_PROB,
            )

        nsfw_prob = 0.0
        for detection in detections:
            weight = _label_weight(_detection_label(detection))
            score = _detection_score(detection)
            nsfw_prob = max(nsfw_prob, min(1.0, weight * (0.5 + 0.5 * score)))

        trimmed = [
            {"label": _detection_label(d), "score": _detection_score(d)}
            for d in detections[:MAX_DETECTIONS_IN_SCORES]
        ]
        return ImageScore(
            image=img,
            model=NudeNetClassifier.name,
            scores={"detections": trimmed},
            nsfw_prob=nsfw_prob,
        )


if register_classifier is not None:  # 正常情况:导入即注册 "nudenet"
    register_classifier("nudenet", NudeNetClassifier)
