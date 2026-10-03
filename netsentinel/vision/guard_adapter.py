"""本地守卫模型适配器(A217 · 世界前沿调研 TOP7 第 2 项)。

把 **ShieldGemma-2 / Llama Guard 类开放权重守卫模型**(guard models,
输入图片+安全政策提示词、输出"安全/不安全"文本判定)接入 NetSentinel 的
多分类器 ensemble——与现有多提供方适配器(hf_clip / glm / multi_provider)
架构零冲突:同为 ``NsfwClassifier`` 契约实现,注册名 ``"guard"``。

安全红线(对应默认禁网/零必装依赖):

- transformers/torch **惰性可选**:仅真实加载模型时才 ``import transformers``,
  缺失时抛出带中文安装提示的 ``ImportError``(对齐 hf_clip 惯例,复用
  ``pip install 'netsentinel[clip]'`` 附加依赖组);本模块绝不出现在必装依赖里,
  模块导入期零副作用(不导入 transformers/torch/PIL、不建 pipeline、不联网);
- 模型来源**仅本地目录路径**(构造参数 ``model_path`` 或 ``cfg.guard_model_path``
  注入),加载一律 ``local_files_only=True``——transformers 默认可能在缺件时
  自动联网下载,本适配器显式关闭该行为,任何情况下都不发起网络请求;
  模型本体须事先按官方模型卡人工下载到本地(见 model_catalog 的 guard 族提示)。

模型族(``family``):

- ``shieldgemma2``:ShieldGemma-2(Google,PaliGemma2 底座)图像安全守卫,
  内置官方安全政策提示词,输出 ``Yes``/``No`` 文本;
- ``llamaguard``:Llama Guard 3 Vision / Llama Guard 4(Meta)多模态守卫,
  审查指令由模型 chat 模板承载,输出 ``safe`` / ``unsafe\\nS{1..14}`` 标签序列;
- ``custom-prompt``:自定义提示词模板(``prompt_template`` 必填),输出按
  Yes/No 语法解析。

错误语义(docstring 注明的选择):解析失败/推理失败/文件损坏一律
``nsfw_prob=0.0`` + ``scores={"error": 中文}`` + WARNING 日志 + 遥测
``vision.errors``——**保守低分、绝不误报**,与 hf_clip_adapter / glm_adapter /
multi_provider 的全库惯例逐字对齐(classifier_base 本身是纯抽象、无错误语义,
惯例落在适配器层)。理由:本工具是合法取证/举报通道,单图"达标"判定
(nsfw_threshold=0.90)直接驱动对外举报,误报代价远高于漏报;且 ensemble 按
均值聚合,故障成员若以高分"保守"注入会虚增整图 agg 分。高歧义的正确出口是
``scores["error"]`` → 人工复核(needs_review / arbiter),而非概率注水。

用法示例(离线注入)::

    from netsentinel.contracts import ImageEvidence
    from netsentinel.vision.guard_adapter import GuardModelAdapter

    clf = GuardModelAdapter(family="shieldgemma2",
                            pipeline=fake_pipe)      # 测试注入替身
    score = clf.classify(ImageEvidence(path="a.png", url="u", source_page="p"))

真实加载(须先人工下载模型到本地目录)::

    clf = GuardModelAdapter(model_path="models/shieldgemma-2-4b-it")

注意:classifier_base 的惰性导入映射表(兄弟模块,本席位只读)未收录
``"guard"``,工厂 ``get_classifier("guard", cfg)`` 生效前需先
``import netsentinel.vision.guard_adapter`` 触发模块级自注册。

提示词与调用形态均为提示信息,以各模型官方模型卡为准(红线 18 同源要求)。
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Callable

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, ImageScore

try:  # 正常情况基类必在;并行开发期 A05 未就位时静默降级(不注册)
    from netsentinel.vision.classifier_base import NsfwClassifier, register_classifier
except ImportError:  # pragma: no cover - 仅并行开发期出现
    NsfwClassifier = object  # type: ignore[assignment,misc]
    register_classifier = None  # type: ignore[assignment]

__all__ = [
    "GUARD_FAMILIES",
    "FAMILY_PROMPTS",
    "FAMILY_TASKS",
    "FAMILY_PARSERS",
    "PROB_UNSAFE",
    "PROB_SAFE",
    "GuardModelAdapter",
    "coerce_model_text",
    "detect_family",
    "parse_llamaguard",
    "parse_yes_no",
]

logger = logging.getLogger(__name__)

#: transformers / torch 缺失时的安装提示(复用 hf_clip 的 clip 附加依赖组)。
_TRANSFORMERS_HINT = "未安装 transformers/torch,请 pip install 'netsentinel[clip]'"
#: Pillow(PIL)缺失时的安装提示(与 hf_clip 一致)。
_PIL_HINT = "未安装 Pillow(PIL),请 pip install Pillow"

#: 支持的守卫模型族(顺序即文档顺序)。
GUARD_FAMILIES: tuple[str, ...] = ("shieldgemma2", "llamaguard", "custom-prompt")

#: 守卫文本判定是二值结论、不带校准概率:高置信但非满分,给 ensemble 其它
#: 成员留纠偏空间(量级对齐 stub 的 0.97/0.02 高置信桩分语义)。
PROB_UNSAFE: float = 0.95
PROB_SAFE: float = 0.05

#: ShieldGemma-2 官方安全政策提示词(图像版,节选自官方模型卡的安全审查问句;
#: 以官方模型卡为准,可在族内自行替换实验)。
SHIELDGEMMA2_PROMPT = (
    "You are a policy expert trying to help determine whether a user prompt "
    "is in violation of the defined safety policies.\n"
    "<safety_properties>\n"
    "A chat between a curious user and an artificial intelligence assistant. "
    "The assistant gives helpful, detailed, and polite answers to the user's "
    "questions. The assistant's image input is followed by a question.\n"
    "</safety_properties>\n"
    "<user_prompt>\n"
    "Question: Does the image contain sexually explicit, nude, or graphic "
    "content?\n"
    "</user_prompt>"
)

#: Llama Guard 审查指令:安全分类学(taxonomy)与提示词模板由模型自带的
#: chat 模板承载,这里只下发展短指令(以官方模型卡为准)。
LLAMAGUARD_PROMPT = (
    "Moderate the content of this image according to the safety policy. "
    "Answer with 'safe' or 'unsafe' followed by violated category codes."
)

#: 各族内置提示词(custom-prompt 族无内置,必须显式传 prompt_template)。
FAMILY_PROMPTS: dict[str, str | None] = {
    "shieldgemma2": SHIELDGEMMA2_PROMPT,
    "llamaguard": LLAMAGUARD_PROMPT,
    "custom-prompt": None,
}

#: 各族的 transformers pipeline 任务名(提示值,以官方模型卡为准;多模态
#: 守卫走 image-text-to-text,图片+文本进、文本出)。
FAMILY_TASKS: dict[str, str] = {
    "shieldgemma2": "image-text-to-text",
    "llamaguard": "image-text-to-text",
    "custom-prompt": "image-text-to-text",
}


# ---------------------------------------------------------------------------
# 输出归一与解析(纯函数,可独立单测)
# ---------------------------------------------------------------------------

#: transformers 各 pipeline 返回形态中的"生成文本"候选键(按序取首个命中)。
_TEXT_KEYS: tuple[str, ...] = ("generated_text", "output_text", "text", "content")

#: Llama Guard 类别码形态:S1..S14(如 S5=成人性内容;语义以官方分类学为准)。
_LLAMAGUARD_CATEGORY_RE = re.compile(r"^S\d{1,2}$")


def coerce_model_text(output: Any) -> str:
    """把 pipeline 的多种返回形态统一归一为生成文本(纯函数)。

    支持的输入形状(transformers 各任务返回不一,注入替身亦可直接给字符串):

    - ``str`` → 原样返回;
    - ``dict`` → 依序取 ``generated_text`` / ``output_text`` / ``text`` /
      ``content`` 首个命中键的值并 str() 化;
    - ``list`` / ``tuple`` → 取首个元素递归归一(batch 形状取首条);
    - ``None`` → 空串;其余对象 → ``str(output)``。
    """
    if isinstance(output, str):
        return output
    if output is None:
        return ""
    if isinstance(output, dict):
        for key in _TEXT_KEYS:
            if key in output:
                return coerce_model_text(output[key])
        return ""
    if isinstance(output, (list, tuple)):
        return coerce_model_text(output[0]) if output else ""
    return str(output)


#: 判定 token 首尾可容忍的标点/引号集合(Yes. / "No" 等形态归一)。
_LINE_PUNCT = ".,;:!?、。,\"'`“”‘’"


def _first_line(text: str) -> str:
    """取首个非空行并做小写化/去首尾标点(Yes/No 与 safe/unsafe 都在首行)。"""
    for line in str(text or "").splitlines():
        token = line.strip().strip(_LINE_PUNCT)
        if token:
            return token.lower()
    return ""


def parse_yes_no(text: str) -> tuple[float, dict[str, Any]]:
    """解析 ShieldGemma-2 风格的 Yes/No 文本判定(纯函数,custom-prompt 族复用)。

    真值表(大小写/首尾标点/前后空白均容忍,取首个非空行):

    - ``Yes`` → ``(PROB_UNSAFE, {"verdict": "unsafe"})``(Yes = 含违规内容);
    - ``No``  → ``(PROB_SAFE,   {"verdict": "safe"})``;
    - 畸形(空/其它文本)→ ``(0.0, {"verdict": "unparseable", "error": 中文})``
      ——错误语义见模块 docstring:保守低分 + error 标记,交人工复核。
    """
    token = _first_line(text)
    if token in ("yes", "y"):
        return PROB_UNSAFE, {"verdict": "unsafe"}
    if token in ("no", "n"):
        return PROB_SAFE, {"verdict": "safe"}
    return 0.0, {
        "verdict": "unparseable",
        "error": f"守卫模型输出无法解析(期望 Yes/No):{text!r}",
    }


def parse_llamaguard(text: str) -> tuple[float, dict[str, Any]]:
    """解析 Llama Guard 风格的安全标签序列(纯函数)。

    真值表(官方输出形态 ``safe`` 或 ``unsafe\\nS{d}``,多类别以换行/逗号分隔):

    - 首行 ``safe`` → ``(PROB_SAFE, {"verdict": "safe", "categories": []})``;
    - 首行 ``unsafe`` → ``(PROB_UNSAFE, {"verdict": "unsafe",
      "categories": ["S5", ...]})``(后续行/逗号里的合法类别码按序去重收集,
      无合法类别码时 categories 为空列表,判定仍为 unsafe);
    - 畸形(空/首行非 safe|unsafe)→ ``(0.0, {"verdict": "unparseable",
      "error": 中文})``——错误语义同 parse_yes_no。
    """
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    if not lines:
        return 0.0, {
            "verdict": "unparseable",
            "error": "守卫模型输出无法解析(期望 safe/unsafe 标签):''",
        }
    verdict = lines[0].strip(_LINE_PUNCT).lower()
    if verdict == "safe":
        return PROB_SAFE, {"verdict": "safe", "categories": []}
    if verdict == "unsafe":
        categories: list[str] = []
        for chunk in lines[1:]:
            for token in chunk.split(","):
                code = token.strip().upper()
                if _LLAMAGUARD_CATEGORY_RE.fullmatch(code) and code not in categories:
                    categories.append(code)
        return PROB_UNSAFE, {"verdict": "unsafe", "categories": categories}
    return 0.0, {
        "verdict": "unparseable",
        "error": f"守卫模型输出无法解析(期望 safe/unsafe 标签):{text!r}",
    }


#: 各族的输出解析器(custom-prompt 沿用 Yes/No 语法,docstring 已注明)。
FAMILY_PARSERS: dict[str, Callable[[str], tuple[float, dict[str, Any]]]] = {
    "shieldgemma2": parse_yes_no,
    "llamaguard": parse_llamaguard,
    "custom-prompt": parse_yes_no,
}


def detect_family(model_path: str) -> str | None:
    """从本地模型目录路径推断模型族(纯函数;推断不了返回 None)。

    - 路径(转小写)含 ``shieldgemma`` → ``shieldgemma2``;
    - 路径同时含 ``llama`` 与 ``guard`` → ``llamaguard``(Llama-Guard-3-11B-vision
      等官方命名均命中);
    - 其余 → None(由调用方报中文错误或显式指定 family)。
    """
    lowered = str(model_path or "").lower()
    if "shieldgemma" in lowered:
        return "shieldgemma2"
    if "llama" in lowered and "guard" in lowered:
        return "llamaguard"
    return None


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------


class GuardModelAdapter(NsfwClassifier):
    """本地开放权重守卫模型分类器(契约名 ``"guard"``)。

    注入协议(测试/离线):``pipeline(image, prompt) -> 任意形态``,返回值经
    :func:`coerce_model_text` 归一为文本后按族解析;推理在实例锁内串行执行
    (对齐 hf_clip/nudenet:共享 pipeline 背后的 torch 模型实例非线程安全)。
    """

    name = "guard"

    def __init__(
        self,
        cfg: Config | None = None,
        pipeline: Any = None,
        model_path: str = "",
        family: str = "",
        prompt_template: str = "",
        task: str = "",
    ) -> None:
        """初始化守卫适配器。

        :param cfg: 可选全局配置;``model_path``/``family`` 未显式给出时读
            ``cfg.guard_model_path`` / ``cfg.guard_family``(getattr 动态读取,
            Config 尚未收录该字段——对齐 V10/V11 附加属性的挂载惯例)。
        :param pipeline: 可注入的守卫 pipeline 替身(测试/离线场景);为 ``None``
            时才惰性加载真实 transformers pipeline(仅本地目录,禁网)。
        :param model_path: 守卫模型的**本地目录路径**(必须已人工下载;
                真实加载时目录不存在抛中文 ``FileNotFoundError``)。
        :param family: 模型族,见 :data:`GUARD_FAMILIES`;未给时按
            ``model_path`` 推断,推断不出抛中文 ``ValueError``。
        :param prompt_template: custom-prompt 族的提示词模板(该族必填,
            缺失抛中文 ``ValueError``);其余族忽略、用内置模板。
        :param task: transformers pipeline 任务名缺省取 :data:`FAMILY_TASKS`
            (一般无需显式指定)。
        :raises FileNotFoundError: 未注入 pipeline 且本地模型目录不存在。
        :raises ValueError: family 非法/推断不出,或 custom-prompt 缺模板。
        :raises ImportError: 未注入 pipeline 且缺少 transformers/torch 时抛出,
            消息附带 ``pip install 'netsentinel[clip]'`` 安装提示。
        """
        self.cfg = cfg
        if not model_path:
            model_path = str(getattr(cfg, "guard_model_path", "") or "")
        if not family:
            family = str(getattr(cfg, "guard_family", "") or "")

        # 模型族:显式指定 → 校验;否则按路径推断;再不行报中文错误。
        family = family.strip().lower()
        if family:
            if family not in GUARD_FAMILIES:
                raise ValueError(
                    f"未知的守卫模型族 family={family!r},"
                    f"有效值:{'/'.join(GUARD_FAMILIES)}"
                )
        else:
            detected = detect_family(model_path)
            if detected is None:
                raise ValueError(
                    "无法从模型路径推断守卫模型族,请显式指定 family="
                    f"{'/'.join(GUARD_FAMILIES)}(model_path={model_path!r})"
                )
            family = detected
        self.family = family

        # 提示词模板:前两族内置;custom-prompt 必须显式给。
        if prompt_template:
            self.prompt = str(prompt_template)
        elif family == "custom-prompt":
            raise ValueError(
                "custom-prompt 族必须显式提供 prompt_template 提示词模板"
            )
        else:
            self.prompt = FAMILY_PROMPTS[family] or ""

        self.model_path = str(model_path or "")
        self.task = str(task or "").strip() or FAMILY_TASKS[family]
        self._parse: Callable[[str], tuple[float, dict[str, Any]]] = FAMILY_PARSERS[family]

        self._pipe = pipeline if pipeline is not None else self._load_pipeline()
        # 推理串行锁:多线程批量时串行推理是模型侧要求(共享 pipeline 背后的
        # torch 模型实例并非线程安全),注入替身同样生效(对齐 hf_clip)。
        self._infer_lock = threading.Lock()

    # -- 模型加载(惰性 + 仅本地 + 禁网) -----------------------------------

    def _load_pipeline(self) -> Any:
        """惰性构建真实 transformers pipeline(此时才导入 transformers/torch)。

        红线落实:

        - **仅本地目录**:``model_path`` 必须是已存在的本地目录(模型须事先按
          官方模型卡人工下载),否则抛中文 ``FileNotFoundError``;
        - **禁网**:``local_files_only=True`` 显式关闭 transformers 缺件时
          自动联网下载的默认行为——任何情况下不发起网络请求。
        """
        if not self.model_path or not os.path.isdir(self.model_path):
            shown = self.model_path or "(空)"
            raise FileNotFoundError(
                f"守卫模型本地目录不存在:'{shown}'——"
                "本适配器仅支持本地目录路径、绝不联网下载;请先按官方模型卡"
                "把模型下载到本地,再经 model_path 或 cfg.guard_model_path 注入"
            )
        try:
            from transformers import pipeline as hf_pipeline
        except ImportError as exc:
            raise ImportError(_TRANSFORMERS_HINT) from exc
        # local_files_only=True:防止 transformers 在本地缺件时自动联网下载。
        raw = hf_pipeline(self.task, model=self.model_path, local_files_only=True)
        return _RealPipelineWrapper(raw)

    # -- 分类 ----------------------------------------------------------------

    def classify(self, img: ImageEvidence) -> ImageScore:
        """对一张落盘图片给出守卫模型判定的 NSFW 概率。

        流程:PIL 解码 → 实例锁内 ``pipeline(image, prompt)`` → 文本归一 →
        按族解析为 ``(nsfw_prob, meta)``。nsfw_prob 归一 [0,1]:Yes/unsafe →
        :data:`PROB_UNSAFE`,No/safe → :data:`PROB_SAFE`,畸形输出 → 0.0 +
        error(错误语义与全库适配器对齐,见模块 docstring)。任何异常
        (文件不存在/损坏、推理崩溃)同样保守记 0 分 + ``scores["error"]``,
        单图失败不中断整站扫描;推理计时 ``guard.infer``,失败计 ``vision.errors``。
        """
        try:
            from PIL import Image
        except ImportError as exc:
            raise ImportError(_PIL_HINT) from exc

        try:
            image = Image.open(img.path).convert("RGB")
            with telemetry.timer("guard.infer"), self._infer_lock:
                raw = self._pipe(image, self.prompt)
            text = coerce_model_text(raw)
            nsfw_prob, meta = self._parse(text)
        except Exception as exc:  # 文件不存在/损坏、推理异常等:保守 0 分,不中断流程
            telemetry.inc("vision.errors")
            logger.warning("守卫模型分类失败,已保守记 0 分: %s -> %r", img.path, exc)
            return ImageScore(
                image=img, model=self.name, scores={"error": str(exc)}, nsfw_prob=0.0
            )

        scores: dict[str, Any] = {"raw_text": text, "family": self.family}
        scores.update(meta)
        if meta.get("error"):
            # 解析失败与推理失败同等对待:计数 + WARNING,交人工复核路径。
            telemetry.inc("vision.errors")
            logger.warning("守卫模型输出解析失败,已保守记 0 分: %r -> %s", text, img.path)
        return ImageScore(
            image=img, model=self.name, scores=scores, nsfw_prob=max(0.0, min(1.0, nsfw_prob))
        )

    def classify_batch(self, imgs: list[ImageEvidence]) -> list[ImageScore]:
        """批量打分:**保持逐张**串行推理(与基类默认同序、同语义)。

        协议决策:本适配器的注入协议是单图 ``__call__(image, prompt)``(见
        模块 docstring),与 hf_clip 相同的理由保持逐张——强行批量须对注入
        替身协议做形状猜测,破坏离线可测性;多线程批量时由实例锁保证串行。
        """
        return [self.classify(img) for img in imgs]


class _RealPipelineWrapper:
    """真实 transformers pipeline 的薄包装:归一为 ``(image, prompt)`` 协议。

    transformers 各任务的入参形态不一(image-text-to-text 官方模型卡用
    ``pipe({"images": 图像, "text": 提示词})``),这里统一成测试注入同款
    ``__call__(image, prompt)``;具体调用形态以官方模型卡为准(提示信息)。
    """

    __slots__ = ("_raw",)

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def __call__(self, image: Any, prompt: str) -> Any:
        return self._raw({"images": image, "text": prompt})


# 模块导入即注册,供(显式 import 本模块后的)get_classifier("guard", cfg) 获取。
if register_classifier is not None:  # pragma: no branch - A05 缺位时不注册
    register_classifier("guard", GuardModelAdapter)
