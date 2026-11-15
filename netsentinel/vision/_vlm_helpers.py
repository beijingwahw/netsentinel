# -*- coding: utf-8 -*-
"""vision 包内共享的 VLM 纯助手(单一实现)。

历史上 :mod:`netsentinel.vision.cascade` 与 :mod:`netsentinel.vision.arbiter`
各自维护一份同名同义副本(合计约 120 行重复);现提取到本模块单一实现,
两个模块保留同名薄封装转发,对外 API 与行为(含日志文案)逐字不变。

本模块的边界约定:

- 只放**纯函数**与**惰性构造助手**(构造失败 → None / 异常语义判定),
  不持有任何密钥、配置全局态或可变进程级状态;
- 兄弟模块(glm_adapter / vlm_cache)一律惰性导入并容错,缺席时按异常
  类名识别 / 返回 None,保证并行开发与离线测试可用(与两个调用方模块的
  既有降级语义一致);
- 各调用方日志文案不同,凡涉及日志的助手一律经参数注入文案模板,
  保证提取后日志输出逐字不变。
"""
from __future__ import annotations

import hashlib
import logging
import math
from typing import Any

from netsentinel.contracts import Config, ImageEvidence

__all__ = [
    "build_glm_client",
    "build_vlm_cache",
    "cache_key",
    "clamp01",
    "extract_prob",
    "extract_reasoning",
    "is_budget_error",
    "is_offline_error",
    "offline_error",
    "sanitize_prob",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 异常语义判定 / 构造(兄弟模块惰性导入,缺席按类名识别)
# ---------------------------------------------------------------------------
def is_offline_error(exc: BaseException) -> bool:
    """识别 glm_adapter.VlmOfflineError;模块未就位时按异常类名识别。"""
    try:
        from netsentinel.vision.glm_adapter import VlmOfflineError
        if isinstance(exc, VlmOfflineError):
            return True
    except Exception:  # noqa: BLE001
        pass
    return type(exc).__name__ == "VlmOfflineError"


def is_budget_error(exc: BaseException) -> bool:
    """识别 vlm_cache.VlmBudgetExceeded;模块未就位时按异常类名识别。"""
    try:
        from netsentinel.vision.vlm_cache import VlmBudgetExceeded
        if isinstance(exc, VlmBudgetExceeded):
            return True
    except Exception:  # noqa: BLE001
        pass
    return type(exc).__name__ == "VlmBudgetExceeded"


def offline_error(message: str) -> Exception:
    """构造离线语义异常:优先复用 glm_adapter.VlmOfflineError,缺席时 RuntimeError。"""
    try:
        from netsentinel.vision.glm_adapter import VlmOfflineError
        return VlmOfflineError(message)
    except Exception:  # noqa: BLE001
        return RuntimeError(message)


# ---------------------------------------------------------------------------
# 惰性构造(client / cache;日志文案由调用方注入,保证逐字不变)
# ---------------------------------------------------------------------------
def build_glm_client(
    cfg: Config,
    *,
    import_fail_msg: str,
    build_fail_msg: str,
) -> Any:
    """惰性构造 GlmVlmClient;模块未就位 / 构造失败 → None。

    :param import_fail_msg: glm_adapter 导入失败的告警文案(``%s`` 为异常)。
    :param build_fail_msg: GlmVlmClient(cfg) 构造失败的告警文案(``%s`` 为异常)。
    """
    try:
        from netsentinel.vision.glm_adapter import GlmVlmClient
    except Exception as exc:  # noqa: BLE001
        logger.warning(import_fail_msg, exc)
        return None
    try:
        return GlmVlmClient(cfg)
    except Exception as exc:  # noqa: BLE001
        logger.warning(build_fail_msg, exc)
        return None


def build_vlm_cache(
    cfg: Config,
    *,
    import_fail_msg: str,
    build_fail_msg: str,
) -> Any:
    """惰性构造 VlmCache;模块未就位 / 构造失败 → None。

    构造签名优先 ``VlmCache(cfg.vlm_cache_db, cfg.vlm_daily_budget)``,
    TypeError(旧签名只收 db_path)时回退 ``VlmCache(cfg.vlm_cache_db)``。

    :param import_fail_msg: vlm_cache 导入失败的告警文案(``%s`` 为异常)。
    :param build_fail_msg: 构造失败的告警文案(``%s`` 为异常;
        旧签名回退仍失败与一般构造失败共用同一文案,与提取前一致)。
    """
    try:
        from netsentinel.vision.vlm_cache import VlmCache
    except Exception as exc:  # noqa: BLE001
        logger.warning(import_fail_msg, exc)
        return None
    try:
        return VlmCache(cfg.vlm_cache_db, cfg.vlm_daily_budget)
    except TypeError:
        # 兼容仅接收 db_path 的旧签名(daily_limit 关键字形态差异)。
        try:
            return VlmCache(cfg.vlm_cache_db)
        except Exception as exc:  # noqa: BLE001
            logger.warning(build_fail_msg, exc)
            return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(build_fail_msg, exc)
        return None


# ---------------------------------------------------------------------------
# 纯辅助(缓存键 / 概率规整 / 载荷提取)
# ---------------------------------------------------------------------------
def cache_key(image: ImageEvidence) -> str:
    """缓存键:优先 sha256;缺失时用路径的稳定 sha256(跨进程一致)。"""
    if image.sha256:
        return image.sha256
    return hashlib.sha256(image.path.encode("utf-8", "surrogatepass")).hexdigest()


def clamp01(value: float) -> float:
    """收敛到 [0, 1](先 float 强转;cascade / multi_provider 的既有口径)。"""
    return min(1.0, max(0.0, float(value)))


def sanitize_prob(value: Any) -> float | None:
    """把任意值规整为 [0,1] 内的概率;不可数值化 / bool / NaN / Inf → None。"""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    if number < 0.0:
        return 0.0
    if number > 1.0:
        return 1.0
    return number


def extract_prob(data: Any) -> float | None:
    """从 VLM 返回 / 缓存载荷中只提取数值字段 nsfw_prob。

    红线 8(提示注入防御):返回内容里任何"指令 / 要求"一律忽略,解析失败按缺失处理。
    """
    if isinstance(data, dict):
        if "nsfw_prob" not in data:
            return None
        return sanitize_prob(data.get("nsfw_prob"))
    if isinstance(data, (int, float)):
        return sanitize_prob(data)
    return None


def extract_reasoning(data: Any) -> str:
    """提取 reasoning 字段(仅接受字符串,其余忽略)。"""
    if isinstance(data, dict):
        value = data.get("reasoning", "")
        if isinstance(value, str):
            return value
    return ""
