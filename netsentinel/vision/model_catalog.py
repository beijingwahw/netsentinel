"""A65 · 视觉模型目录与推荐(纯提示信息,仅标准库,离线可用)。

按提供方列出常见视觉(VLM)模型,并给出按需求的推荐(suggest)、
目录/通配校验(validate_model)与跨平台关键词检索(search)。

安全红线(对应 CONTRACTS-V4 红线 18):
- 目录里的模型名/端点一律是**提示信息**,不构成可用性承诺;命名与档位随各平台
  迭代频繁变动,一切以官方文档为准,**上线前须人工核验一次**;
- 全部条目可经 ``vlm_provider_models`` 配置覆盖,目录仅作缺省提示;
- 本模块不写入任何价格数字,仅给 cheap/balanced/flagship 定性档位。

标签体系(tags,多选):
- ``cheap`` 低价/轻量档;``balanced`` 均衡主力档;``flagship`` 高能力旗舰档;
- ``local`` 本地推理(数据不出本机,免密钥);``free`` 平台明确标注的免费额度
  (目前仅 OpenRouter 的 ``:free`` 后缀模型);
- ``open-weights`` 开放权重(模型权重可下载自持);``guard`` 守卫/审查类模型
  (安全分类而非通用 VLM,A217 起由 guard_adapter 本地推理接入)。

guard 族(A217 · 世界前沿调研 TOP7 第 2 项):
- 目录键 ``"guard"`` **不是 API 提供方**,而是开放权重守卫模型的族目录
  (ShieldGemma-2 / Llama Guard 类);目录条目仅作下载提示,真实推理经
  ``vision/guard_adapter.py`` 从**本地目录**加载(禁网、local_files_only),
  与 providers.PROVIDERS 的 20 家 API 提供方互不重叠;
- ``open-weights`` / ``guard`` 与 ``local`` 同属"无回退档":suggest 在该档
  未命中时返回 None,绝不硬塞一个无关模型。

通配白名单(validate_model 第二道口,正则形状校验,同样是提示级):
- 含 ``/`` 的开源路径形态 ``org/name[:tag]``(siliconflow/openrouter/together/
  vllm/lmstudio/xinference 等常见);
- 火山方舟(豆包)``ep-`` 开头的推理接入点 ID;
- ``基名:版本号`` 形态(如 Ollama 的 ``llava:13b`` / ``:latest``、
  OpenRouter 的 ``:free``),要求基名命中该提供方目录。

本模块为纯函数:不联网、不落盘、无第三方依赖,便于离线测试。

用法示例::

    from netsentinel.vision.model_catalog import search, suggest, validate_model

    suggest("glm")                      # -> "glm-5.3-flash"(balanced 档提示名)
    suggest("openai", "flagship")       # -> "gpt-4o"
    validate_model("ollama", "llava:13b")   # -> True(通配白名单:基名:版本号)
    [m.id for m in search("flash")]     # -> 跨平台含 "flash" 的模型(目录顺序)

V5 性能:三类查询的索引均在模块加载时一次性预构建(见 :func:`_build_indexes`),
suggest/validate 由 O(目录条目) 线性扫降为 dict 查表;search 由全表扫描
降为"首字符倒排桶"内扫描——语义与全扫严格等价(kw 是文本子串的必要条件
是 kw[0] 出现在该文本中,故按文本含有的字符建桶不会漏;桶内再精确子串
复核保证不误报)。目录数据 :data:`MODELS` 本身不变。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from netsentinel import telemetry

__all__ = [
    "VALID_TAGS",
    "ModelInfo",
    "MODELS",
    "UNCERTAIN_IDS",
    "suggest",
    "validate_model",
    "search",
]

#: 合法标签全集(顺序即文档顺序;open-weights/guard 为 A217 守卫族能力标记)
VALID_TAGS: tuple[str, ...] = (
    "cheap", "balanced", "flagship", "local", "free", "open-weights", "guard",
)

#: 模型名最大长度(validate_model 拒绝超长输入,防滥用正则)
_MAX_MODEL_LEN = 200


@dataclass(frozen=True)
class ModelInfo:
    """目录中的单个视觉模型条目。

    - ``id``:调用 API 时使用的模型名(提示值,以官方为准);
    - ``tags``:档位标签,取自 :data:`VALID_TAGS`,至少一个;
    - ``note``:中文备注;按红线 18,一律含"以官方为准"字样。
    """

    id: str
    tags: tuple[str, ...]
    note: str = ""


# ---------------------------------------------------------------------------
# 模型目录(提示信息,上线前核验一次;可被 vlm_provider_models 覆盖)
# ---------------------------------------------------------------------------

MODELS: dict[str, list[ModelInfo]] = {
    "glm": [
        ModelInfo(
            "glm-5.3-flash", ("cheap", "balanced"),
            "智谱轻量多模态模型,V4 目录默认;以官方为准",
        ),
        ModelInfo(
            "glm-4.5v", ("flagship",),
            "智谱多模态旗舰,疑难图复核可用;以官方为准",
        ),
        ModelInfo(
            "glm-4v", ("balanced",),
            "上一代主力视觉模型;以官方为准",
        ),
        ModelInfo(
            "glm-4v-flash", ("cheap",),
            "官方曾提供免费档的轻量视觉模型;以官方为准",
        ),
    ],
    "openai": [
        ModelInfo(
            "gpt-4o-mini", ("cheap", "balanced"),
            "OpenAI 轻量多模态,V4 目录默认;以官方为准",
        ),
        ModelInfo(
            "gpt-4o", ("flagship",),
            "OpenAI 旗舰多模态;以官方为准",
        ),
        ModelInfo(
            "gpt-4.1-mini", ("cheap", "balanced"),
            "新一代轻量多模态;以官方为准",
        ),
    ],
    "anthropic": [
        ModelInfo(
            "claude-sonnet-4", ("balanced", "flagship"),
            "Anthropic 主力视觉模型,V4 目录默认;以官方为准",
        ),
        ModelInfo(
            "claude-haiku-4-5", ("cheap",),
            "低成本快速档;以官方为准",
        ),
        ModelInfo(
            "claude-opus-4-5", ("flagship",),
            "顶配档,适合复核抽检;以官方为准",
        ),
    ],
    "gemini": [
        ModelInfo(
            "gemini-2.0-flash", ("cheap", "balanced"),
            "Google 轻量多模态,V4 目录默认;以官方为准",
        ),
        ModelInfo(
            "gemini-2.0-pro", ("flagship",),
            "任务给定的旗舰档提示名,公开可用性以官方为准",
        ),
        ModelInfo(
            "gemini-2.5-flash", ("cheap", "balanced"),
            "2.5 代轻量档;以官方为准",
        ),
        ModelInfo(
            "gemini-2.5-pro", ("flagship",),
            "2.5 代旗舰档;以官方为准",
        ),
    ],
    "qwen": [
        ModelInfo(
            "qwen-vl-max", ("flagship",),
            "通义旗舰视觉模型,V4 目录默认;以官方为准",
        ),
        ModelInfo(
            "qwen-vl-plus", ("balanced",),
            "均衡档;以官方为准",
        ),
        ModelInfo(
            "qwen-vl-flash", ("cheap",),
            "低价快速档;以官方为准",
        ),
    ],
    "doubao": [
        ModelInfo(
            "doubao-1.5-vision-pro", ("flagship", "balanced"),
            "豆包主力视觉模型,V4 目录默认,亦可用 ep- 接入点 ID;以官方为准",
        ),
        ModelInfo(
            "doubao-1.5-vision-lite", ("cheap",),
            "轻量档;以官方为准",
        ),
    ],
    "hunyuan": [
        ModelInfo(
            "hunyuan-vision", ("balanced",),
            "腾讯混元视觉模型,V4 目录默认;以官方为准",
        ),
        ModelInfo(
            "hunyuan-turbo-vision", ("flagship",),
            "高性能档;以官方为准",
        ),
    ],
    "moonshot": [
        ModelInfo(
            "kimi-latest", ("balanced", "flagship"),
            "Kimi 当前主力模型(支持视觉输入),V4 目录默认;以官方为准",
        ),
        ModelInfo(
            "moonshot-v1-8k-vision-preview", ("cheap",),
            "早期视觉预览版,可能已下线;以官方为准",
        ),
    ],
    "minimax": [
        ModelInfo(
            "MiniMax-VL-01", ("flagship", "balanced"),
            "MiniMax 视觉模型,V4 目录默认;以官方为准",
        ),
        ModelInfo(
            "MiniMax-M2", ("balanced",),
            "新一代多模态系列,视觉输入能力以官方为准",
        ),
    ],
    "stepfun": [
        ModelInfo(
            "step-1v-8k", ("cheap", "balanced"),
            "阶跃轻量视觉模型,V4 目录默认;以官方为准",
        ),
        ModelInfo(
            "step-1v-32k", ("balanced",),
            "长上下文视觉档;以官方为准",
        ),
        ModelInfo(
            "step-1o-turbo-vision", ("cheap",),
            "Omni 系列快速档视觉;以官方为准",
        ),
    ],
    "siliconflow": [
        ModelInfo(
            "Qwen/Qwen2.5-VL-7B-Instruct", ("cheap", "balanced"),
            "硅基流动开源视觉模型,V4 目录默认;以官方为准",
        ),
        ModelInfo(
            "Qwen/Qwen2.5-VL-72B-Instruct", ("flagship",),
            "72B 开源大杯;以官方为准",
        ),
        ModelInfo(
            "OpenGVLab/InternVL2_5-8B", ("balanced",),
            "开源视觉模型,确切路径写法以官方为准",
        ),
    ],
    "ernie": [
        ModelInfo(
            "ernie-4.5-vl", ("flagship", "balanced"),
            "文心 4.5 视觉旗舰,V4 目录默认;以官方为准",
        ),
        ModelInfo(
            "ernie-4.5-vl-flash", ("cheap",),
            "低价快速档;以官方为准",
        ),
    ],
    "openrouter": [
        ModelInfo(
            "qwen/qwen2.5-vl-72b-instruct:free", ("free", "cheap"),
            "OpenRouter 免费额度开源视觉模型,V4 目录默认;以官方为准",
        ),
        ModelInfo(
            "meta-llama/llama-4-scout-17b-16e-instruct:free", ("free",),
            "免费档 Llama 4 视觉;以官方为准",
        ),
        ModelInfo(
            "openai/gpt-4o-mini", ("cheap", "balanced"),
            "付费便宜档,走 OpenAI 上游;以官方为准",
        ),
    ],
    "groq": [
        ModelInfo(
            "meta-llama/llama-4-scout-17b-16e-instruct", ("cheap", "balanced"),
            "Groq 高速 Llama 4 视觉,V4 目录默认;以官方为准",
        ),
        ModelInfo(
            "meta-llama/llama-4-maverick-17b-128e-instruct", ("flagship",),
            "Llama 4 大杯;以官方为准",
        ),
    ],
    "together": [
        ModelInfo(
            "meta-llama/Llama-4-Scout-17B-16E-Instruct", ("cheap", "balanced"),
            "Together 开源视觉,V4 目录默认;以官方为准",
        ),
        ModelInfo(
            "meta-llama/Llama-4-Maverick-17B-128E-Instruct", ("flagship",),
            "Llama 4 大杯;以官方为准",
        ),
    ],
    "xai": [
        ModelInfo(
            "grok-2-vision-1212", ("balanced", "flagship"),
            "Grok 视觉模型,V4 目录默认;以官方为准",
        ),
        ModelInfo(
            "grok-2-vision", ("balanced",),
            "不带日期后缀的别名;以官方为准",
        ),
        ModelInfo(
            "grok-4", ("flagship",),
            "新一代旗舰,视觉输入能力以官方为准",
        ),
    ],
    "ollama": [
        ModelInfo(
            "llava", ("local", "cheap"),
            "本地轻量视觉模型,V4 目录默认,数据不出本机;以官方为准",
        ),
        ModelInfo(
            "llama3.2-vision", ("local", "balanced"),
            "本地 Llama 3.2 视觉(11B/90B 用 :tag 区分),数据不出本机;以官方为准",
        ),
        ModelInfo(
            "qwen2.5vl", ("local", "flagship"),
            "本地 Qwen 视觉大杯,数据不出本机;以官方为准",
        ),
        ModelInfo(
            "minicpm-v", ("local", "cheap"),
            "本地端侧轻量视觉,数据不出本机;以官方为准",
        ),
    ],
    "vllm": [
        ModelInfo(
            "Qwen/Qwen2.5-VL-7B-Instruct", ("local", "cheap"),
            "本地自托管,HF 路径名必填,数据不出本机;以官方为准",
        ),
        ModelInfo(
            "llava-hf/llava-1.5-7b-hf", ("local", "cheap"),
            "本地经典视觉,数据不出本机;以官方为准",
        ),
        ModelInfo(
            "OpenGVLab/InternVL2_5-8B", ("local", "balanced"),
            "本地开源视觉,确切路径以官方为准,数据不出本机;以官方为准",
        ),
    ],
    "lmstudio": [
        ModelInfo(
            "qwen2.5-vl-7b-instruct", ("local", "balanced"),
            "本地 LM Studio 模型目录名,数据不出本机;以官方为准",
        ),
        ModelInfo(
            "llava-1.5-7b-hf", ("local", "cheap"),
            "本地轻量视觉,数据不出本机;以官方为准",
        ),
    ],
    "xinference": [
        ModelInfo(
            "qwen2.5-vl", ("local", "balanced"),
            "本地部署视觉,数据不出本机;以官方为准",
        ),
        ModelInfo(
            "llava", ("local", "cheap"),
            "本地轻量视觉,数据不出本机;以官方为准",
        ),
        ModelInfo(
            "minicpm-v", ("local", "cheap"),
            "本地端侧视觉,数据不出本机;以官方为准",
        ),
    ],
    # A217 · 守卫模型族:开放权重、可本地推理的安全审查模型(guard_adapter
    # 适配,仅本地目录加载、禁网)。该键不是 API 提供方(providers.PROVIDERS
    # 不含 "guard"),目录条目仅作人工下载提示。
    "guard": [
        ModelInfo(
            "google/shieldgemma-2-4b-it", ("local", "open-weights", "guard"),
            "ShieldGemma-2 4B 图像安全守卫(开放权重),guard_adapter 族 "
            "shieldgemma2;须人工下载到本地目录,推理禁网;以官方为准",
        ),
        ModelInfo(
            "meta-llama/Llama-Guard-3-11B-vision", ("local", "open-weights", "guard"),
            "Llama Guard 3 Vision 11B 多模态守卫(开放权重),guard_adapter 族 "
            "llamaguard;须人工下载到本地目录,推理禁网;以官方为准",
        ),
        ModelInfo(
            "meta-llama/Llama-Guard-4-12B", ("local", "open-weights", "guard"),
            "Llama Guard 4 12B 多模态守卫(开放权重),guard_adapter 族 "
            "llamaguard;须人工下载到本地目录,推理禁网;以官方为准",
        ),
    ],
}

#: 命名/存在性把握不足的条目(``提供方:模型`` 键);其 note 必含"以官方为准",
#: 上线前必须逐条人工核验(红线 18)。
UNCERTAIN_IDS: frozenset[str] = frozenset(
    {
        "glm:glm-4v-flash",
        "gemini:gemini-2.0-pro",
        "qwen:qwen-vl-flash",
        "doubao:doubao-1.5-vision-lite",
        "hunyuan:hunyuan-turbo-vision",
        "moonshot:kimi-latest",
        "moonshot:moonshot-v1-8k-vision-preview",
        "minimax:MiniMax-VL-01",
        "minimax:MiniMax-M2",
        "stepfun:step-1v-32k",
        "stepfun:step-1o-turbo-vision",
        "siliconflow:OpenGVLab/InternVL2_5-8B",
        "ernie:ernie-4.5-vl",
        "ernie:ernie-4.5-vl-flash",
        "openrouter:meta-llama/llama-4-scout-17b-16e-instruct:free",
        "xai:grok-2-vision",
        "xai:grok-4",
        "vllm:OpenGVLab/InternVL2_5-8B",
        "lmstudio:qwen2.5-vl-7b-instruct",
        "lmstudio:llava-1.5-7b-hf",
        # A217 守卫族:HF 仓库命名/可用性随官方迭代,把握不足,逐条核验。
        "guard:google/shieldgemma-2-4b-it",
        "guard:meta-llama/Llama-Guard-3-11B-vision",
        "guard:meta-llama/Llama-Guard-4-12B",
    }
)

# ---------------------------------------------------------------------------
# 推荐(suggest)
# ---------------------------------------------------------------------------

#: 各需求的标签回退链:先精确匹配需求标签,再按链降级,最后回退到目录首条;
#: 无回退档(``local``/``open-weights``/``guard``)例外——该档未命中应返回
#: None:云端提供方没有本地模型、非守卫提供方没有守卫模型,硬塞无关模型比
#: 返回 None 更误导。
_SUGGEST_CHAINS: dict[str, tuple[str, ...]] = {
    "balanced": ("balanced", "cheap"),
    "cheap": ("cheap", "balanced"),
    "flagship": ("flagship", "balanced", "cheap"),
    "free": ("free", "cheap"),
    "local": ("local",),
    "open-weights": ("open-weights", "local"),
    "guard": ("guard",),
}

#: 无回退档:链上全部未命中时 suggest 返回 None(见 _SUGGEST_CHAINS 注释)。
_NO_FALLBACK_TAGS: frozenset[str] = frozenset({"local", "open-weights", "guard"})


# ---------------------------------------------------------------------------
# 预构建索引(V5:模块加载时一次性建好,查询一律查表)
# ---------------------------------------------------------------------------

#: provider → (标签 → 该标签在目录声明顺序下的首个模型 id)
_SUGGEST_INDEX: dict[str, dict[str, str]] = {}

#: provider → 目录首条模型 id(suggest 兜底回退)
_FIRST_MODEL: dict[str, str] = {}

#: provider → 目录条目 id 的小写集合(validate_model 大小写不敏感命中查表)
_LOWER_IDS: dict[str, frozenset[str]] = {}

#: 平铺目录顺序(与 MODELS 声明顺序一致),供 search 命中后按目录顺序输出
_ALL_INFOS: list[ModelInfo] = []

#: 单个可检索文本(注意 note 与旧实现一致**不**做小写化:中文无大小写,
#: 英文关键词只对 id 大小写不敏感,note 按原文匹配)
_SearchRow = tuple  # (序号, 条目, 已归一文本)

#: 字符 → 该字符出现于其中的 (序号, 条目, 文本) 列表;kw 命中某文本的
#: 必要条件是 kw[0] 出现在该文本中,故查 kw[0] 的桶再精确子串复核即严格等价
_SEARCH_INDEX: dict[str, list[tuple[int, ModelInfo, str]]] = {}


def _build_indexes() -> None:
    """从 :data:`MODELS` 一次性构建全部查询索引(模块加载时调用,仅此一次)。

    - suggest/validate 索引:把"逐条线性扫标签/逐条 lower 比较"预先算成
      dict 查表;
    - search 倒排桶:每条 id(小写)与 note 原文,按其**含有的每个字符**
      挂桶;桶内条目保持目录声明顺序。
    """
    seq = 0
    for provider, models in MODELS.items():
        first_by_tag: dict[str, str] = {}
        lowered: set[str] = set()
        for info in models:
            for tag in info.tags:
                first_by_tag.setdefault(tag, info.id)
            lowered.add(info.id.lower())
            _ALL_INFOS.append(info)
            for text in (info.id.lower(), info.note):
                if not text:
                    continue
                for ch in set(text):
                    _SEARCH_INDEX.setdefault(ch, []).append((seq, info, text))
            seq += 1
        _SUGGEST_INDEX[provider] = first_by_tag
        _FIRST_MODEL[provider] = models[0].id
        _LOWER_IDS[provider] = frozenset(lowered)


_build_indexes()


def suggest(provider: str, need: str = "balanced") -> str | None:
    """按需求档位推荐一个模型名(纯目录提示,以官方为准)。

    - ``need`` ∈ :data:`VALID_TAGS`(默认 ``balanced``);未知值抛 ValueError(中文);
    - 命中顺序:需求标签 → 回退链(如 balanced→cheap)→ 目录首条(任意);
    - ``need`` 为 ``local``/``open-weights``/``guard``(无回退档)时,链上未命中
      返回 None(本地档绝不映射到云端模型,守卫档绝不映射到通用 VLM);
    - 未知提供方 / 空目录返回 None。

    V5:命中顺序由模块加载时预构建的 :data:`_SUGGEST_INDEX`(标签→该标签
    首个模型)查表得出,与逐条线性扫严格等价。
    """
    key = str(provider or "").strip().lower()
    wanted = ("balanced" if need is None else str(need)).strip().lower()
    if wanted not in VALID_TAGS:
        raise ValueError(
            f"未知的需求档位 need={need!r},有效值:{'/'.join(VALID_TAGS)}"
        )
    tags = _SUGGEST_INDEX.get(key)
    if not tags:  # 未知提供方 / 空目录(索引在加载时按目录一次建全)
        return None
    for tag in _SUGGEST_CHAINS[wanted]:
        hit = tags.get(tag)
        if hit is not None:
            return hit
    if wanted in _NO_FALLBACK_TAGS:
        return None
    return _FIRST_MODEL[key]


# ---------------------------------------------------------------------------
# 校验(validate_model)
# ---------------------------------------------------------------------------

#: 模型名安全字符集(字母数字/点/下划线/连字符/斜杠/冒号;禁止空格与查询串)
_SAFE_CHARS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")

#: 开源路径形态 org/name[:tag](tag 为 free/latest 或数字开头版本)
_OPEN_SOURCE_PATH_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?::(?:free|latest|[0-9][0-9A-Za-z._-]*))?$"
)

#: 火山方舟(豆包)推理接入点 ID 形态,ep- 后至少 6 个安全字符
_ENDPOINT_RE = re.compile(r"^ep-[0-9A-Za-z-]{6,}$")

#: 基名:版本号 形态(Ollama :13b/:latest、OpenRouter :free)
_VERSION_TAG_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*:(?:free|latest|[0-9][0-9A-Za-z._-]*)$"
)


def validate_model(provider: str, model: str) -> bool:
    """校验 ``提供方:模型`` 里的模型名是否可用(提示级,通过不等于保证可用)。

    判定顺序:

    1. 提供方必须在目录中(未知提供方一律 False);
    2. 模型名与目录条目精确命中(大小写不敏感);
    3. 通配白名单(三种正则形态):
       - ``org/name[:tag]`` 开源路径(如 ``Qwen/Qwen2.5-VL-32B-Instruct``);
       - ``ep-`` 开头的火山方舟接入点 ID(仅 doubao 提供方);
       - ``基名:版本号``(如 ``llava:13b``),基名须命中该提供方目录;
    4. 其余(空串、含空格/查询符、裸家族名如 ``claude-sonnet``)一律 False。

    命中也只是形状合法:真实可用性以官方为准,上线前核验一次(红线 18)。

    V5:目录命中由 :data:`_LOWER_IDS`(小写 id 集合)查表判定——原实现
    ``info.id == name or info.id.lower() == lowered`` 中精确比较被小写比较
    包含(``info.id == name`` 时两侧 lower 必相等),故查表严格等价。
    """
    if not isinstance(provider, str) or not isinstance(model, str):
        return False
    key = provider.strip().lower()
    lowered_ids = _LOWER_IDS.get(key)
    if lowered_ids is None:
        return False
    name = model.strip()
    if not name or len(name) > _MAX_MODEL_LEN or not _SAFE_CHARS_RE.fullmatch(name):
        return False
    if name.lower() in lowered_ids:
        return True
    if key == "doubao" and _ENDPOINT_RE.fullmatch(name):
        return True
    if _OPEN_SOURCE_PATH_RE.fullmatch(name):
        return True
    if _VERSION_TAG_RE.fullmatch(name):
        base = name.rsplit(":", 1)[0].lower()
        if base in lowered_ids:
            return True
    return False


# ---------------------------------------------------------------------------
# 检索(search)
# ---------------------------------------------------------------------------


def search(keyword: str) -> list[ModelInfo]:
    """跨平台检索目录:模型 id 与 note 的子串匹配(大小写不敏感)。

    - 命中按目录声明顺序返回(提供方顺序 → 列表顺序),不去重跨平台的同名模型;
    - 空白/非字符串关键词返回空列表。

    V5:由 :data:`_SEARCH_INDEX` 首字符倒排桶定位候选(kw[0] 必出现在命中
    文本中),桶内精确子串复核,同一模型 id 与 note 双双命中时只返回一条;
    与全表扫描严格等价,桶内/输出均保持目录声明顺序。
    """
    if not isinstance(keyword, str):
        return []
    kw = keyword.strip().lower()
    if not kw:
        return []
    telemetry.inc("catalog.search")
    matched: set[int] = set()
    for seq, info, text in _SEARCH_INDEX.get(kw[0], ()):
        if seq not in matched and kw in text:
            matched.add(seq)
    return [_ALL_INFOS[seq] for seq in sorted(matched)]
