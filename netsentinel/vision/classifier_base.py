"""NSFW 分类器体系公共底座(抽象基类 + 注册表 + 工厂)。

本模块是 vision 子系统的契约实现(见 CONTRACTS.md §2):
- ``NsfwClassifier``:所有分类器的抽象基类(stub / nudenet / clip 均实现它);
- ``register_classifier``:写入模块级注册表(重名时后者覆盖并记录警告日志);
- ``get_classifier``:按名称实例化分类器,支持适配器模块惰性导入自注册。

用法示例::

    from netsentinel.contracts import Config
    from netsentinel.vision.classifier_base import get_classifier

    clf = get_classifier("stub", Config())          # 已注册名 → 直接实例化
    clf = get_classifier("openai:gpt-4o-mini", cfg)  # "提供方:模型" → 统一 VLM 网关

线程安全:注册表的读/写由模块级锁保护,多线程并发注册与并发 get 互不破坏。

仅使用标准库;离线运行,不发起任何网络请求。
"""
from __future__ import annotations

import importlib
import logging
import threading
from abc import ABC, abstractmethod

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, ImageScore

__all__ = ["NsfwClassifier", "get_classifier", "register_classifier"]

logger = logging.getLogger(__name__)

# 模块级分类器注册表:分类器名 -> 分类器类。
# 注意:始终原地增删键、不整体替换 dict 对象(multi_provider 的错误消息与
# 既有测试都按"同一对象"读取该表)。
_REGISTRY: dict[str, type[NsfwClassifier]] = {}

# 注册表读写锁:RLock 而非 Lock,因为 get_classifier 的惰性导入会触发适配器
# 模块级自注册(同一线程内重入 register_classifier);当前实现把导入放在锁外,
# 可重入锁是防御将来代码重组时不自锁。
_REGISTRY_LOCK = threading.RLock()

# 名称未注册时尝试惰性导入的模块映射(导入即触发其模块级自注册)。
# stub 一并纳入,保证默认分类器在调用方未显式 import 时也能通过工厂获取。
# glm(V2):GLM 视觉大模型适配器,导入时自注册 "glm"。
# guard(A217):本地守卫模型适配器(ShieldGemma-2 / Llama Guard 类),导入时
# 自注册 "guard";模型路径经 ``cfg.guard_model_path`` 注入(附加属性,
# getattr 动态读取),缺席时由适配器构造期抛中文 ValueError(缺省错误语义)。
_LAZY_IMPORT_MODULES: dict[str, str] = {
    "stub": "netsentinel.vision.stub_classifier",
    "nudenet": "netsentinel.vision.nudenet_adapter",
    "clip": "netsentinel.vision.hf_clip_adapter",
    "glm": "netsentinel.vision.glm_adapter",
    "guard": "netsentinel.vision.guard_adapter",
}

# 惰性导入失败记忆(V5):导入抛过 ImportError 的模块路径只尝试一次,
# 之后同名 miss 直接走转交/报错路径,不再重复执行 importlib 查找与异常抛接
# (依赖缺失时每次 get_classifier 都全量扫描 finder 的开销被消除)。
# 进程内一次性生效:三方依赖不会在进程运行中途"出现",无需过期策略。
_LAZY_IMPORT_FAILED: set[str] = set()


class NsfwClassifier(ABC):
    """NSFW 图像分类器抽象基类。

    子类约定:
      1. 必须定义非空类属性 ``name``(与注册表键一致,如 "stub"/"nudenet"/"clip");
      2. 必须实现 ``classify``;
      3. 构造函数应接受 ``cfg: Config``——工厂以 ``cls(cfg)`` 实例化。
    """

    name: str = ""

    @abstractmethod
    def classify(self, img: ImageEvidence) -> ImageScore:
        """对单张图片打分,返回含 ``nsfw_prob`` 的 :class:`ImageScore`。"""

    def classify_batch(self, imgs: list[ImageEvidence]) -> list[ImageScore]:
        """批量打分:默认逐张调用 ``classify`` 并保持输入顺序;子类可覆写以做批量优化。"""
        return [self.classify(img) for img in imgs]


def register_classifier(name: str, cls: type[NsfwClassifier]) -> None:
    """向模块级注册表注册分类器类(线程安全)。

    重名注册时后者覆盖前者,并记录 WARNING 日志(中文)。

    示例::

        class MyClassifier(NsfwClassifier):
            name = "my"

            def classify(self, img): ...

        register_classifier("my", MyClassifier)
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("分类器注册名必须是非空字符串")
    if not (isinstance(cls, type) and issubclass(cls, NsfwClassifier)):
        raise ValueError(f"待注册对象必须是 NsfwClassifier 的子类,实际收到:{cls!r}")
    if not getattr(cls, "name", ""):
        raise ValueError(f"分类器类 {cls.__name__} 缺少非空类属性 name,无法注册")
    with _REGISTRY_LOCK:
        previous = _REGISTRY.get(name)
        if previous is not None:
            logger.warning(
                "分类器 '%s' 原已注册为 %s,现被 %s 覆盖",
                name,
                previous.__name__,
                cls.__name__,
            )
        _REGISTRY[name] = cls
    logger.debug("分类器注册成功:name=%s cls=%s", name, cls.__name__)


def get_classifier(name: str, cfg: Config) -> NsfwClassifier:
    """按名称从注册表取分类器并实例化(每次调用返回新实例;线程安全)。

    名称未注册时,先尝试惰性导入已知适配器模块以触发其自注册
    (导入抛 ImportError 时忽略并记忆失败——同一模块路径每进程只尝试一次;
    未安装三方依赖或并行开发中模块未就位均属正常);
    仍未注册且名称形如 ``提供方`` 或 ``提供方:模型``(V4 全平台语法,如
    ``openai:gpt-4o-mini`` / ``gemini:gemini-2.0-flash`` / ``ollama:llava``)时,
    惰性转交 ``netsentinel.vision.multi_provider.build_classifier`` 构建统一
    VLM 分类器;全部失败抛 ``ValueError``(中文消息,列出已注册分类器与可用提供方),
    并累计遥测计数 ``classifier.miss``。

    转交语义(V4)为兼容红线:凡注册表与惰性映射都未命中的名称一律交给
    multi_provider 处理,"vlm" 泛型与 "provider:model" 规格均依赖该路径。
    """
    with _REGISTRY_LOCK:
        cls = _REGISTRY.get(name)
    if cls is None and name in _LAZY_IMPORT_MODULES:
        module_path = _LAZY_IMPORT_MODULES[name]
        # 失败记忆:命中则跳过 importlib(依赖缺失时避免每次 miss 都全量查找)。
        memoized_failure = module_path in _LAZY_IMPORT_FAILED
        if not memoized_failure:
            try:
                # 导入在锁外执行:模块级自注册会经 register_classifier 重入锁。
                importlib.import_module(module_path)
            except ImportError as exc:
                _LAZY_IMPORT_FAILED.add(module_path)
                logger.debug("惰性导入 %s 失败(已记忆,不再重试):%s", module_path, exc)
            with _REGISTRY_LOCK:
                cls = _REGISTRY.get(name)
    if cls is None:
        # V4:全平台视觉模型 —— "provider[:model]" 语法走统一网关模块。
        try:
            multi_provider = importlib.import_module(
                "netsentinel.vision.multi_provider"
            )
        except ImportError as exc:
            logger.debug("multi_provider 未就位(忽略):%s", exc)
        else:
            try:
                return multi_provider.build_classifier(name, cfg)
            except ValueError:
                # 未知提供方/非法规格等转交失败同样计入 miss(裸 raise 保留原栈)。
                telemetry.inc("classifier.miss")
                raise
        telemetry.inc("classifier.miss")
        with _REGISTRY_LOCK:
            registered = ", ".join(sorted(_REGISTRY)) or "(无)"
        raise ValueError(f"未注册的分类器 '{name}';当前已注册的分类器:{registered}")
    logger.debug("创建分类器实例:name=%s cls=%s", name, cls.__name__)
    return cls(cfg)
