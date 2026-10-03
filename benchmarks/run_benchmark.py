# -*- coding: utf-8 -*-
"""NetSentinel 离线基准测试框架(A37)。

标注语料(benchmarks/corpus)+ 阈值网格 + PR 指标报告,为运营者选型
(stub / glm / 集成)与调整判定阈值提供量化依据。**全程离线**:

- ``load_corpus(corpus_dir)``:读 labels.json(宽高从 PNG 头嗅探),返回
  ``[(ImageEvidence, 标注), ...]``,标注取值 ``nsfw | borderline | clean``;
- ``evaluate(scores, threshold, positive)``:混淆矩阵 tp/fp/fn/tn 与
  precision / recall / f1(分母为零时按 0.0 容错,不抛异常);
- ``pr_curve(scores)``:阈值 0.05..0.95(步长 0.05)逐点计算 PR 指标;
- ``suggest_threshold(curve)``:F1 最大的网格阈值(并列时取更保守的高阈值);
- ``run(corpus_dir, out_dir, classifier_name)``:取分类器(惰性,失败给中文指引)
  → 逐图评分 → 指标 → 写 ``report.md``(中文)/ ``report.json``;
- ``make_corpus(corpus_dir)``:用 scripts/make_png.py(纯标准库)确定性重建语料。

命令行::

    python benchmarks/run_benchmark.py --make-corpus          # 重建语料
    python benchmarks/run_benchmark.py --out benchmarks/out   # 跑基准并出报告
    python benchmarks/run_benchmark.py --classifier glm --strict
    python benchmarks/run_benchmark.py adversarial --gate     # 对抗基准金标门禁(透传)

``adversarial`` 子命令(V10.4):余下参数**原样透传**给
``benchmarks/adversarial.py`` 的 CLI(含 ``--gate`` / ``--update-golden`` /
``--golden`` / ``--missing-baseline``),其返回码(0 正常 / 1 金标输入
错误 / 2 回归违例)由主 CLI 原样返回;不带子命令时行为与本文件原有
CLI 完全一致(零破坏)。

口径说明:borderline(边缘样本)默认**不计入正类**(positive={"nsfw"}),
由 ``run(..., strict=True)`` 或 CLI ``--strict`` 开启计入;报告里会注明。

V5 升级(兼容性零破坏,评测口径与报告数值不变):

- 性能:评分循环改走分类器批量协议 ``classify_batch``(不支持时回退逐张);
  PR 曲线对样本只**排序一次**,19 个阈值单趟扫描(O(N·logN + N + K)
  取代 O(K·N));建议阈值指标直接复用曲线点,省一次全量扫描;
- 可观测:``telemetry.timer("benchmark.run")`` 全程计时,语料张数计入
  ``benchmark.images``,可预期失败计入 ``benchmark.errors``(零依赖,
  只存名称与数字);报告渲染保持单遍(list 累加 + 一次 join)。

安全红线:本框架只读本地语料;glm 等在线分类器需显式配置(vlm_online +
密钥)才允许评测,未配置时给出明确中文提示并以退出码 2 结束,
绝不静默产出全 0 分的无效报告。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import struct
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# 直接以脚本运行(python benchmarks/run_benchmark.py)时,保证项目根在
# sys.path 上,使 netsentinel 包可导入;经包导入(tests)时此步为空操作。
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from netsentinel import telemetry  # noqa: E402
from netsentinel.contracts import Config, ImageEvidence, now_iso  # noqa: E402
from netsentinel.vision.classifier_base import (  # noqa: E402
    NsfwClassifier,
    get_classifier,
)

__all__ = [
    "BenchmarkError",
    "DEFAULT_POSITIVE",
    "VALID_LABELS",
    "default_thresholds",
    "evaluate",
    "load_corpus",
    "make_corpus",
    "pr_curve",
    "render_markdown",
    "run",
    "sniff_png_size",
    "suggest_threshold",
    "write_png",
    "main",
]

logger = logging.getLogger(__name__)

#: 语料合法标注取值(nsfw_hi→nsfw / nsfw_mid→borderline / normal→clean)。
VALID_LABELS: frozenset[str] = frozenset({"nsfw", "borderline", "clean"})

#: 默认正类集合:borderline 不计入(评测口径由 strict 参数控制)。
DEFAULT_POSITIVE: frozenset[str] = frozenset({"nsfw"})

#: GLM 等在线分类器未配置时的环境变量提示名。
ENV_GLM_API_KEY = "NETSENTINEL_GLM_API_KEY"

# 已知但 classifier_base 未内置惰性导入映射的 V2 分类器模块(导入即自注册)。
_EXTRA_LAZY_MODULES: dict[str, str] = {
    "glm": "netsentinel.vision.glm_adapter",
}


class BenchmarkError(RuntimeError):
    """基准测试流程中可预期的错误(中文消息;CLI 捕获后以退出码 2 结束)。"""


# ---------------------------------------------------------------------------
# PNG 头嗅探与语料装载
# ---------------------------------------------------------------------------

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def sniff_png_size(data: bytes) -> tuple[int, int]:
    """从 PNG 字节流头部嗅探 ``(宽, 高)``。

    PNG 结构固定:8 字节签名 + IHDR 块(长度 4B + 类型 4B + 宽 4B + 高 4B),
    故宽高总是位于第 16..24 字节(大端 u32)。非法 PNG 抛 :class:`BenchmarkError`。
    """
    if len(data) < 24 or not data.startswith(_PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise BenchmarkError("非合法 PNG(缺少签名或 IHDR 块),无法嗅探宽高")
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _normalize_labels(raw: Any, source: Path) -> dict[str, str]:
    """把 labels.json 顶层归一为 ``{文件名: 标签}`` 字典。

    兼容两种格式:``{"a.png": "nsfw"}`` 映射(本框架生成)与
    ``[{"file": "a.png", "label": "nsfw"}, ...]`` 记录数组;其余格式报中文错误。
    """
    pairs: list[tuple[str, str]]
    if isinstance(raw, dict):
        pairs = [(str(key), str(value)) for key, value in raw.items()]
    elif isinstance(raw, list):
        pairs = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict) or "file" not in item or "label" not in item:
                raise BenchmarkError(
                    f"{source} 第 {index} 条记录缺少 file/label 字段(数组格式须为逐条对象)"
                )
            pairs.append((str(item["file"]), str(item["label"])))
    else:
        raise BenchmarkError(
            f"{source} 顶层必须是 {{文件名: 标签}} 映射或逐条记录数组,当前为 {type(raw).__name__}"
        )
    mapping: dict[str, str] = {}
    for filename, label in pairs:
        if not filename or filename in mapping:
            raise BenchmarkError(f"{source} 中文件名重复或为空:{filename!r}")
        mapping[filename] = label
    return mapping


def load_corpus(corpus_dir: str | os.PathLike[str]) -> list[tuple[ImageEvidence, str]]:
    """读入标注语料,返回按文件名排序的 ``(ImageEvidence, 标注)`` 列表。

    - labels.json 缺失 / 引用的图片缺失 / 标注非法 → :class:`BenchmarkError`(中文);
    - ImageEvidence 的宽高从 PNG 头嗅探,并回填 sha256(便于对账去重);
    - url/source_page 使用 ``benchmark://`` 虚拟前缀(离线语料无真实来源页)。
    """
    root = Path(corpus_dir)
    labels_path = root / "labels.json"
    if not labels_path.is_file():
        raise BenchmarkError(
            f"未找到标注文件 {labels_path}:请先运行 "
            "`python benchmarks/run_benchmark.py --make-corpus` 重建语料"
        )
    try:
        raw = json.loads(labels_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"标注文件解析失败:{labels_path}({exc})") from exc

    mapping = _normalize_labels(raw, labels_path)
    pairs: list[tuple[ImageEvidence, str]] = []
    for filename in sorted(mapping):
        label = mapping[filename]
        if label not in VALID_LABELS:
            raise BenchmarkError(
                f"图片 {filename} 的标注 '{label}' 不在合法取值 {sorted(VALID_LABELS)} 之内"
            )
        image_path = root / filename
        if not image_path.is_file():
            raise BenchmarkError(f"标注引用的图片不存在:{image_path}")
        data = image_path.read_bytes()
        width, height = sniff_png_size(data)
        evidence = ImageEvidence(
            path=str(image_path),
            url=f"benchmark://corpus/{filename}",
            source_page="benchmark://corpus",
            sha256=hashlib.sha256(data).hexdigest(),
            width=width,
            height=height,
        )
        pairs.append((evidence, label))
    return pairs


# ---------------------------------------------------------------------------
# 语料生成(纯标准库 PNG)
# ---------------------------------------------------------------------------

_MAKE_PNG_MODULE_NAME = "netsentinel_benchmarks_make_png"
_MAKE_PNG_PATH = _ROOT / "scripts" / "make_png.py"

#: 生成尺寸:200x200 与 400x300 交替(均 >= Config.min_image_px=200,参与判定)。
CORPUS_SIZES: tuple[tuple[int, int], ...] = ((200, 200), (400, 300))

#: 各类底色(与真实内容无关,仅为让文件字节可区分;标注只由文件名类别决定)。
_HI_BASE = (0x8B, 0x1E, 0x3F)       # 深红:nsfw_hi
_MID_BASE = (0xC8, 0x78, 0x28)      # 琥珀:nsfw_mid
_NORMAL_BASES = (                    # 蓝绿灰轮换:normal
    (0x2E, 0x54, 0x8A),
    (0x3A, 0x76, 0x58),
    (0x5B, 0x5B, 0x82),
)


def _variant(base: tuple[int, int, int], index: int) -> tuple[int, int, int]:
    """按序号对底色做确定性微移,使同尺寸文件字节(与 sha256)可区分。"""
    r, g, b = base
    return ((r + index * 2) % 256, (g + index) % 256, b)


def _load_make_png() -> Callable[[int, int, tuple[int, int, int]], bytes]:
    """按文件路径惰性载入 scripts/make_png.py 的 ``make_png``(纯标准库生成器)。"""
    if _MAKE_PNG_PATH.is_file():
        module_cached = sys.modules.get(_MAKE_PNG_MODULE_NAME)
        if module_cached is not None:
            func = getattr(module_cached, "make_png", None)
            if callable(func):
                return func  # type: ignore[no-any-return]
        spec = importlib.util.spec_from_file_location(_MAKE_PNG_MODULE_NAME, _MAKE_PNG_PATH)
        if spec is None or spec.loader is None:  # pragma: no cover - 理论上不会发生
            raise BenchmarkError(f"无法载入 PNG 生成器模块:{_MAKE_PNG_PATH}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MAKE_PNG_MODULE_NAME] = module
        spec.loader.exec_module(module)
        func = getattr(module, "make_png", None)
        if not callable(func):
            raise BenchmarkError("scripts/make_png.py 缺少 make_png(width, height, rgb) 函数")
        return func  # type: ignore[no-any-return]
    raise BenchmarkError(
        f"未找到纯标准库 PNG 生成器:{_MAKE_PNG_PATH}(scripts/make_png.py 应随仓库就位)"
    )


def write_png(
    out_path: str | os.PathLike[str],
    width: int,
    height: int,
    rgb: tuple[int, int, int],
) -> Path:
    """用 scripts/make_png.py 生成一张纯色 PNG 落盘(父目录自动创建)。"""
    path = Path(out_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_load_make_png()(width, height, rgb))
    return path


def make_corpus(corpus_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """重建标注语料(确定性输出,同参数字节级可复现)。

    构成:12 张 nsfw_hi_*.png(nsfw)+ 6 张 nsfw_mid_*.png(borderline)
    + 12 张 normal_*.png(clean),尺寸 200x200 / 400x300 交替;
    同时写出 labels.json 与 README.md。返回生成摘要 dict。
    """
    root = Path(corpus_dir)
    root.mkdir(parents=True, exist_ok=True)
    labels: dict[str, str] = {}

    def _emit(kind: str, count: int, label: str, color: tuple[int, int, int]) -> None:
        for i in range(1, count + 1):
            name = f"{kind}_{i:03d}.png"
            width, height = CORPUS_SIZES[i % 2]
            write_png(root / name, width, height, _variant(color, i))
            labels[name] = label

    _emit("nsfw_hi", 12, "nsfw", _HI_BASE)
    _emit("nsfw_mid", 6, "borderline", _MID_BASE)
    _emit("normal", 12, "clean", _NORMAL_BASES[0])

    (root / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (root / "README.md").write_text(CORPUS_README, encoding="utf-8")
    counts = {"nsfw": 12, "borderline": 6, "clean": 12}
    logger.info("语料已重建:%s(%s)", root, counts)
    return {"corpus_dir": str(root), "counts": counts, "total": sum(counts.values())}


CORPUS_README = """\
# benchmarks/corpus —— 离线基准标注语料(NetSentinel A37)

> **合成语料,仅用于管线基准,不含真实违规内容。**

- 全部图片由 `scripts/make_png.py`(纯标准库 PNG 生成器)程序化生成的**纯色 PNG**,
  文件名中的关键词仅供桩分类器(stub)按规则打分,与真实图像内容无关;
- 构成(尺寸为 200x200 与 400x300 混合,均不低于 `Config.min_image_px=200`):
  - `nsfw_hi_*.png` × 12,标注 `nsfw`(stub 规则分 0.97);
  - `nsfw_mid_*.png` × 6,标注 `borderline`(stub 规则分 0.72);
  - `normal_*.png` × 12,标注 `clean`(stub 规则分 0.02);
- `labels.json`:`{文件名: 标签}`,标签取值 `nsfw | borderline | clean`
  (读取端也兼容 `[{"file": ..., "label": ...}]` 数组格式);
- 重建:`python benchmarks/run_benchmark.py --make-corpus`(确定性生成,字节级可复现);
- 用途:验证"评分 → 阈值 → 指标"链路、演示阈值选型。stub 分数由文件名决定,
  任何指标均为**管线锚点**,不代表真实识别能力;生产请使用 glm / nudenet 集成。
"""


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------


def evaluate(
    scores: list[tuple[str, float]],
    threshold: float,
    positive: Iterable[str] = DEFAULT_POSITIVE,
) -> dict[str, Any]:
    """按阈值计算混淆矩阵与 precision / recall / f1。

    - ``scores``:``[(标注, 分类器 nsfw_prob), ...]``;
    - 预测为正 = ``prob >= threshold``;实际为正 = 标注 ∈ ``positive``;
    - 除零容错:precision / recall / f1 在分母为零时取 0.0,不抛异常。
    """
    pos = {str(p) for p in positive if str(p)}
    tp = fp = fn = tn = 0
    for label, prob in scores:
        actual = label in pos
        predicted = float(prob) >= float(threshold)
        if actual and predicted:
            tp += 1
        elif actual and not predicted:
            fn += 1
        elif not actual and predicted:
            fp += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "threshold": round(float(threshold), 4),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "positive_labels": sorted(pos),
    }


def default_thresholds() -> list[float]:
    """默认阈值网格:0.05, 0.10, ..., 0.95(步长 0.05,共 19 点)。"""
    return [round(0.05 * k, 2) for k in range(1, 20)]


def pr_curve(
    scores: list[tuple[str, float]],
    thresholds: Sequence[float] | None = None,
    positive: Iterable[str] = DEFAULT_POSITIVE,
) -> list[dict[str, Any]]:
    """对每个阈值计算一组 PR 指标(阈值升序,含 tp/fp/fn/tn 便于复核)。

    V5 性能:实现上对样本**只排序一次**,随后按阈值升序单趟扫描
    (:func:`_pr_curve_sweep`,O(N·logN + N + K) 取代逐阈值全量重扫的
    O(K·N));数值口径与 ``[evaluate(scores, t) for t in grid]`` 完全一致
    (新增测试锁定),输出顺序仍为传入网格顺序。
    """
    grid = list(thresholds) if thresholds is not None else default_thresholds()
    if not grid:
        raise BenchmarkError("阈值网格为空,无法计算 PR 曲线")
    return _pr_curve_sweep(scores, grid, positive)


def _pr_curve_sweep(
    scores: list[tuple[str, float]],
    grid: Sequence[float],
    positive: Iterable[str],
) -> list[dict[str, Any]]:
    """PR 曲线内部实现:一次排序,多阈值扫描;结果与逐阈值 evaluate 等价。

    原理:预测为正 ⇔ ``prob >= t``。把样本按 prob 降序排序后,阈值升序
    扫描时只需前进一个指针——留在指针右侧的样本即"预测为正";正/负类
    的个数用后缀计数直接读出,无需按阈值重扫全表。
    """
    pos = {str(p) for p in positive if str(p)}
    ordered = sorted(
        ((float(prob), str(label) in pos) for label, prob in scores),
        key=lambda item: item[0],
    )
    total = len(ordered)
    total_pos = sum(1 for _, actual in ordered if actual)
    total_neg = total - total_pos
    # pos_suffix[i]:ordered[i:] 中的正类个数(指针右侧即"预测为正"区)。
    # 升序排序下,阈值越大指针越靠右,单趟扫描即可得全部阈值的结果。
    pos_suffix = [0] * (total + 1)
    for i in range(total - 1, -1, -1):
        pos_suffix[i] = pos_suffix[i + 1] + (1 if ordered[i][1] else 0)

    results: list[dict[str, Any] | None] = [None] * len(grid)
    pointer = 0
    for index in sorted(range(len(grid)), key=lambda i: float(grid[i])):
        threshold = float(grid[index])
        while pointer < total and ordered[pointer][0] < threshold:
            pointer += 1
        tp = pos_suffix[pointer]
        predicted_pos = total - pointer
        fp = predicted_pos - tp
        fn = total_pos - tp
        tn = total_neg - fp
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        results[index] = {
            "threshold": round(threshold, 4),
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "tn": tn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "positive_labels": sorted(pos),
        }
    return [point for point in results if point is not None]


def suggest_threshold(curve: list[dict[str, Any]]) -> float:
    """取 F1 最大的网格阈值;F1 并列时取**更保守的高阈值**。

    实现按阈值升序遍历并用 ``>=`` 刷新最优,使并列时后出现(更高)的阈值胜出。
    """
    if not curve:
        raise BenchmarkError("PR 曲线为空,无法给出阈值建议")
    ordered = sorted(curve, key=lambda item: float(item["threshold"]))
    best_threshold = float(ordered[0]["threshold"])
    best_f1 = -1.0
    for point in ordered:
        if float(point["f1"]) >= best_f1:
            best_f1 = float(point["f1"])
            best_threshold = float(point["threshold"])
    return round(best_threshold, 4)


# ---------------------------------------------------------------------------
# 分类器解析(惰性 + 中文指引 + glm 配置预检)
# ---------------------------------------------------------------------------


def _resolve_classifier(classifier_name: str, cfg: Config) -> NsfwClassifier:
    """经工厂取分类器;工厂惰性导入表之外的 V2 模块(glm)在此补一次惰性导入。"""
    try:
        return get_classifier(classifier_name, cfg)
    except ValueError:
        module_path = _EXTRA_LAZY_MODULES.get(classifier_name)
        if module_path:
            try:
                importlib.import_module(module_path)
            except ImportError as exc:
                logger.debug("惰性导入 %s 失败(忽略):%s", module_path, exc)
        try:
            return get_classifier(classifier_name, cfg)
        except ValueError as exc:
            raise BenchmarkError(
                f"无法创建分类器 '{classifier_name}':{exc}\n"
                "提示:离线管线基准请用 --classifier stub(开箱即用);"
                "glm 需 V2 模块就位且配置 glm_api_key 并开启 vlm_online;"
                "nudenet / clip 需安装对应可选依赖(pip install '.[vision]' / '.[clip]')。"
            ) from exc


def _preflight_online_classifier(classifier_name: str, cfg: Config) -> None:
    """在线分类器(glm)评测前配置预检:未配置直接拒绝,避免静默产出全 0 分报告。"""
    if classifier_name != "glm":
        return
    reasons: list[str] = []
    if not cfg.vlm_online:
        reasons.append("cfg.vlm_online=False(默认安全态:图像数据不出本机)")
    api_key = cfg.glm_api_key or os.environ.get(ENV_GLM_API_KEY, "")
    if not api_key:
        reasons.append(f"未配置 glm_api_key,环境变量 {ENV_GLM_API_KEY} 也为空")
    if reasons:
        raise BenchmarkError(
            "GLM 基准评测未配置就绪:" + ";".join(reasons)
            + "。离线基准请改用 --classifier stub;确需在线评测,请在配置中设置 "
            "vlm_online: true 并提供 glm_api_key(红线:开启后图片仅发往 glm_base_url,"
            "且 GLM 结果只是特征,最终判定与举报仍须人工确认)。"
        )


def _build_classifier(classifier_name: str, cfg: Config) -> NsfwClassifier:
    """解析 + 预检,返回可用分类器实例;失败抛 :class:`BenchmarkError`(中文)。"""
    _preflight_online_classifier(classifier_name, cfg)
    return _resolve_classifier(classifier_name, cfg)


# ---------------------------------------------------------------------------
# 主流程与报告
# ---------------------------------------------------------------------------


def _classify_corpus(
    classifier: NsfwClassifier, evidences: list[ImageEvidence]
) -> list[float]:
    """对整份语料评分,返回与输入同序的 nsfw_prob 列表。

    V5 性能:优先走分类器协议的批量接口 ``classify_batch``(glm /
    multi_provider 等可合并传输,stub 基类默认实现等价逐张);分类器
    未提供批量接口或返回长度不符时,回退逐张 ``classify``,绝不改变结果。
    """
    batch_fn = getattr(classifier, "classify_batch", None)
    if callable(batch_fn):
        batch = batch_fn(evidences)
        if isinstance(batch, list) and len(batch) == len(evidences):
            return [float(score.nsfw_prob) for score in batch]
        logger.warning(
            "classify_batch 返回长度不符(期望 %d),回退逐张 classify",
            len(evidences),
        )
    return [float(classifier.classify(ev).nsfw_prob) for ev in evidences]


def run(
    corpus_dir: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    classifier_name: str = "stub",
    *,
    strict: bool = False,
    threshold: float | None = None,
) -> dict[str, Any]:
    """跑一次离线基准:评分 → 指标 → 写 ``out_dir/report.md`` + ``report.json``。

    - 分类器经工厂惰性获取;失败(glm 未配置 / 未注册 / 依赖未装)抛
      :class:`BenchmarkError`(中文指引),CLI 层转退出码 2;
    - 正类口径:默认 ``{"nsfw"}``;``strict=True`` 时把 borderline 也计入正类;
    - 默认判定阈值取 ``Config.nsfw_threshold``(0.90),可用 ``threshold`` 覆盖;
    - V5 可观测:全程计时 ``telemetry.timer("benchmark.run")``,语料张数计入
      ``benchmark.images``,报告指标可通过 ``telemetry.snapshot()`` 复核;
    - 返回写入 report.json 的同一份 payload(便于测试与上层复用)。
    """
    cfg = Config()
    with telemetry.timer("benchmark.run"):
        classifier = _build_classifier(classifier_name, cfg)
        pairs = load_corpus(corpus_dir)
        if not pairs:
            raise BenchmarkError(
                f"语料为空:{corpus_dir}(请检查 labels.json,或用 --make-corpus 重建)"
            )
        telemetry.inc("benchmark.images", amount=len(pairs))

        positive: list[str] = ["nsfw", "borderline"] if strict else ["nsfw"]
        default_threshold = round(
            float(threshold if threshold is not None else cfg.nsfw_threshold), 4
        )

        probs = _classify_corpus(classifier, [evidence for evidence, _ in pairs])
        scores: list[tuple[str, float]] = []
        details: list[dict[str, Any]] = []
        label_counts: dict[str, int] = {}
        size_counts: dict[str, int] = {}
        for (evidence, label), prob in zip(pairs, probs):
            scores.append((label, prob))
            details.append(
                {
                    "file": Path(evidence.path).name,
                    "label": label,
                    "prob": round(prob, 4),
                    "predicted_nsfw": prob >= default_threshold,
                }
            )
            label_counts[label] = label_counts.get(label, 0) + 1
            size_key = f"{evidence.width}x{evidence.height}"
            size_counts[size_key] = size_counts.get(size_key, 0) + 1

        main_metrics = evaluate(scores, default_threshold, positive)
        curve = pr_curve(scores, positive=positive)
        suggested = suggest_threshold(curve)
        # 建议阈值必落在网格上:直接复用曲线点,省一次全量 evaluate 扫描。
        suggested_metrics = next(
            (
                {key: point[key] for key in (
                    "threshold", "tp", "fp", "fn", "tn", "precision", "recall", "f1"
                )}
                for point in curve
                if float(point["threshold"]) == suggested
            ),
            evaluate(scores, suggested, positive),
        )

        payload: dict[str, Any] = {
            "generated_at": now_iso(),
            "classifier": classifier.name or classifier_name,
            "classifier_name": classifier_name,
            "corpus": {
                "dir": str(Path(corpus_dir)),
                "total": len(pairs),
                "labels": dict(sorted(label_counts.items())),
                "sizes": dict(sorted(size_counts.items())),
            },
            "eval": {
                "positive_labels": main_metrics["positive_labels"],
                "strict": strict,
                "borderline_note": (
                    "strict=False:borderline(边缘样本)不计入正类,只按 nsfw 标注核对"
                    if not strict
                    else "strict=True:borderline 与 nsfw 均计入正类"
                ),
                "threshold": default_threshold,
                "confusion": {
                    key: main_metrics[key] for key in ("tp", "fp", "fn", "tn")
                },
                "precision": main_metrics["precision"],
                "recall": main_metrics["recall"],
                "f1": main_metrics["f1"],
            },
            "pr_curve": [
                {
                    key: (round(value, 6) if isinstance(value, float) else value)
                    for key, value in point.items()
                }
                for point in curve
            ],
            "suggested_threshold": suggested,
            "suggested_metrics": suggested_metrics,
            "details": details,
        }

        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (out / "report.md").write_text(render_markdown(payload), encoding="utf-8")
        logger.info(
            "基准完成:classifier=%s total=%d threshold=%s 建议阈值=%s",
            payload["classifier"],
            payload["corpus"]["total"],
            default_threshold,
            suggested,
        )
    return payload


def _r4(value: float) -> str:
    """报告用的 4 位小数格式化。"""
    return f"{float(value):.4f}"


def render_markdown(payload: dict[str, Any]) -> str:
    """把基准 payload 渲染为中文 Markdown 报告(单文件,无外部依赖)。"""
    corpus = payload["corpus"]
    ev = payload["eval"]
    conf = ev["confusion"]
    positive_labels = list(ev["positive_labels"])
    negative_labels = sorted(VALID_LABELS - set(positive_labels))

    lines: list[str] = []
    lines.append("# NetSentinel 离线基准测试报告(A37)")
    lines.append("")
    lines.append(f"- 生成时间:{payload['generated_at']}")
    lines.append(f"- 分类器:`{payload['classifier']}`")
    labels_desc = "、".join(f"{k} {v} 张" for k, v in corpus["labels"].items())
    sizes_desc = "、".join(f"{k} {v} 张" for k, v in corpus["sizes"].items())
    lines.append(
        f"- 语料:`{corpus['dir']}`,共 {corpus['total']} 张({labels_desc};尺寸:{sizes_desc})"
    )
    lines.append(
        f"- 评测口径:正类 = {{{', '.join(positive_labels)}}};{ev['borderline_note']};"
        "预测为正 = 分类器 nsfw_prob ≥ 阈值"
    )
    lines.append(f"- 判定阈值:{ev['threshold']}(与 `Config.nsfw_threshold` 生产默认一致)")
    lines.append("")

    lines.append(f"## 一、混淆矩阵(阈值 {ev['threshold']})")
    lines.append("")
    lines.append("| 实际 \\ 预测 | 预测为正(≥ 阈值) | 预测为负 |")
    lines.append("| --- | ---: | ---: |")
    lines.append(f"| 正类({', '.join(positive_labels)}) | TP = {conf['tp']} | FN = {conf['fn']} |")
    lines.append(
        f"| 负类({', '.join(negative_labels)}) | FP = {conf['fp']} | TN = {conf['tn']} |"
    )
    lines.append("")
    lines.append(f"- 精确率(precision)= {_r4(ev['precision'])}")
    lines.append(f"- 召回率(recall)= {_r4(ev['recall'])}")
    lines.append(f"- F1 = {_r4(ev['f1'])}")
    lines.append("")

    grid = payload["pr_curve"]
    first, last = grid[0]["threshold"], grid[-1]["threshold"]
    lines.append(f"## 二、PR 表(阈值 {first} → {last},步长 0.05)")
    lines.append("")
    lines.append("| 阈值 | TP | FP | FN | TN | 精确率 | 召回率 | F1 |")
    lines.append("| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for point in grid:
        lines.append(
            "| {:.2f} | {} | {} | {} | {} | {} | {} | {} |".format(
                point["threshold"],
                point["tp"],
                point["fp"],
                point["fn"],
                point["tn"],
                _r4(point["precision"]),
                _r4(point["recall"]),
                _r4(point["f1"]),
            )
        )
    lines.append("")

    suggested = payload["suggested_threshold"]
    sm = payload["suggested_metrics"]
    lines.append("## 三、建议阈值")
    lines.append("")
    lines.append(f"- 按 F1 最大原则:建议阈值 = **{suggested}**(F1 并列时取更保守的高阈值)")
    lines.append(
        f"- 该阈值下:精确率 {_r4(sm['precision'])}、召回率 {_r4(sm['recall'])}、"
        f"F1 = {_r4(sm['f1'])}(TP={sm['tp']} FP={sm['fp']} FN={sm['fn']} TN={sm['tn']})"
    )
    lines.append(
        f"- 对比:当前生产默认阈值 {ev['threshold']} 下 F1 = {_r4(ev['f1'])}"
        f"(精确率 {_r4(ev['precision'])}、召回率 {_r4(ev['recall'])})"
    )
    lines.append("")

    lines.append("## 四、逐图明细")
    lines.append("")
    lines.append("| 文件 | 标注 | nsfw_prob | 预测(默认阈值) |")
    lines.append("| --- | --- | ---: | --- |")
    for row in payload["details"]:
        verdict = "正" if row["predicted_nsfw"] else "负"
        lines.append(
            f"| {row['file']} | {row['label']} | {_r4(row['prob'])} | {verdict} |"
        )
    lines.append("")

    lines.append("## 五、结论")
    lines.append("")
    lines.append("- **stub 仅为管线基准锚点,生产请用 glm/nudenet 集成**。stub 分数来自"
                 "文件名规则(nsfw_hi→0.97 / nsfw_mid→0.72 / 其他→0.02),本报告数字"
                 "只用于验证\"评分 → 阈值 → 指标\"链路与阈值选型方法,不代表真实识别能力。")
    if payload["classifier"] != "stub":
        lines.append(
            f"- 本次评测分类器为 `{payload['classifier']}`,可与 stub 基线报告对比"
            "观察能力差异(同一语料、同一口径)。"
        )
    lines.append("- 语料为程序生成的合成图片,不含真实违规内容;指标不可外推到真实业务分布。")
    lines.append("- 阈值取舍:以 F1 最大为默认建议(并列取高阈值更保守);若运营侧更重视"
                 "精确率(少误报)可在 PR 表中选更高阈值,更重视召回率(少漏报)则反之。")
    lines.append("- 按契约红线,任何模型(含 glm)的结果都只是特征:最终判定与举报须人工确认。")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _ensure_utf8_stdio() -> None:
    """Windows 控制台编码非 UTF-8 时切换标准流编码,避免中文输出报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if (
                stream is not None
                and stream.encoding
                and stream.encoding.lower() not in ("utf-8", "utf8")
            ):
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 重新配置失败不影响主流程
            pass


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    返回码:0 成功;2 语料/分类器等可预期错误(中文提示输出到 stderr);
    ``adversarial`` 子命令的返回码(含金标门禁违例 2 / 金标输入错误 1)
    原样透传返回。
    """
    _ensure_utf8_stdio()
    tokens = list(sys.argv[1:] if argv is None else argv)
    # adversarial 子命令(V10.4 金标门禁透传):首词命中即整体转交
    # benchmarks/adversarial.py 的 CLI。惰性导入避免环——adversarial 顶层
    # 导入了本模块的 BenchmarkError / load_corpus;不带子命令的原有 CLI
    # 路径零改动。
    if tokens and tokens[0] == "adversarial":
        from benchmarks.adversarial import main as adversarial_main

        return adversarial_main(tokens[1:])
    default_corpus = str(Path(__file__).resolve().parent / "corpus")
    default_out = str(Path(__file__).resolve().parent / "out")
    parser = argparse.ArgumentParser(
        prog="python benchmarks/run_benchmark.py",
        description=(
            "NetSentinel 离线基准测试:标注语料 × 分类器 → 混淆矩阵 / PR 曲线 / 阈值建议"
            "(全程离线;glm 等在线分类器需显式配置);子命令 adversarial 透传对抗基准"
            " CLI(含金标门禁 --gate/--update-golden/--golden)"
        ),
        epilog="子命令:adversarial —— 对抗鲁棒性基准(A55/V10.4),其余参数原样透传,"
        "例如 python benchmarks/run_benchmark.py adversarial --gate",
    )
    parser.add_argument(
        "--corpus", default=default_corpus, help=f"标注语料目录(默认 {default_corpus})"
    )
    parser.add_argument(
        "--out", default=default_out, help=f"报告输出目录(默认 {default_out})"
    )
    parser.add_argument(
        "--classifier",
        default="stub",
        help="分类器注册名(默认 stub,离线可用;glm 需配置 vlm_online 与密钥)",
    )
    parser.add_argument(
        "--make-corpus",
        action="store_true",
        help="先重建标注语料(确定性输出),随后继续跑基准",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="borderline 也计入正类(默认只按 nsfw 计正类)",
    )
    args = parser.parse_args(tokens)

    try:
        if args.make_corpus:
            built = make_corpus(args.corpus)
            print(
                f"语料已重建:{built['corpus_dir']}"
                f"(共 {built['total']} 张:{built['counts']})"
            )
        payload = run(args.corpus, args.out, classifier_name=args.classifier, strict=args.strict)
    except BenchmarkError as exc:
        telemetry.inc("benchmark.errors")
        print(f"错误:{exc}", file=sys.stderr)
        return 2

    conf = payload["eval"]["confusion"]
    print(
        "基准完成:分类器={} 语料={}张 阈值={} → TP={} FP={} FN={} TN={} P={} R={} F1={}".format(
            payload["classifier"],
            payload["corpus"]["total"],
            payload["eval"]["threshold"],
            conf["tp"],
            conf["fp"],
            conf["fn"],
            conf["tn"],
            _r4(payload["eval"]["precision"]),
            _r4(payload["eval"]["recall"]),
            _r4(payload["eval"]["f1"]),
        )
    )
    print(f"建议阈值:{payload['suggested_threshold']}(F1 最大,并列取更保守高阈值)")
    out_dir = Path(args.out)
    print(f"报告已写出:{out_dir / 'report.md'} 与 {out_dir / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
