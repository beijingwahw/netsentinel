"""密钥受纳入口(A152,依据 CONTRACTS-V8.md §0 红线 33 / §3 A152 行)。

V8 连接向导与手动切换需要一处统一的密钥录入关口。本模块只做"受纳":
**校验 → 落盘(委托 :func:`netsentinel.security.keys.set_key`)→ 返回掩码**,
自身不实现任何存储、绝不联网。

红线 33(**密钥只进不显**)在本模块的落实:

- 键盘语义按 password 对待:任何日志只记 **提供方名与长度**,绝不记密钥本体;
- 所有 ``ValueError`` 的中文消息只含 **长度 / 问题类型**(过短、含空白、
  含换行、含控制字符……),**绝不回显密钥内容**——哪怕片段;
- 返回值只给掩码(前 4 位 + ``****``),与 ``security.keys`` 打码口径一致;
- 落盘仅经 :func:`security.keys.set_key`(自动建目录、写首行、收紧权限)。

提供方合法性:惰性加载 A61 目录 ``netsentinel.vision.providers.PROVIDERS``,
未就位 / 导入失败时退回 :data:`security.keys.BUILTIN_PROVIDERS` 兜底清单
(与 A70 的退化口径一致);两种形态下都不在清单内 → 中文 ``ValueError``。

用法示例::

    from netsentinel.security import key_api

    key_api.accept_key_input("qwen", "sk-abcdef123456")
    # {"ok": True, "masked": "sk-a****", "stored": True}
    key_api.masked("qwen")     # "sk-a****";未配置时 None(纯本地查询,不触网)

只用标准库,绝不联网;所有用户可见文案均为中文。
"""
from __future__ import annotations

import logging
import unicodedata
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.security import keys

__all__ = ["accept_key_input", "masked"]

logger = logging.getLogger(__name__)

#: 密钥最小长度(契约 A152:长度 ≥8 才受纳)
MIN_KEY_LENGTH = 8

#: 掩码后缀(与 security.keys / vault 打码口径一致:前 4 位 + ****)
_MASK_SUFFIX = "****"

#: 默认落盘目录(直通 keys.set_key 的同一默认值;``~`` 经 ``Path.home()`` 展开)
_DEFAULT_BASE = "~/.netsentinel/keys"


# ---------------------------------------------------------------------------
# 提供方目录:惰性加载 A61,缺席退回 A70 内置清单
# ---------------------------------------------------------------------------


def _load_provider_catalog() -> dict[str, Any] | None:
    """惰性加载 A61 目录 ``PROVIDERS``;未就位 / 导入失败返回 ``None``。

    与 :func:`netsentinel.security.keys._load_catalog` 同款口径:每次调用
    重新尝试导入(模块缓存后开销可忽略),A61 后续落地无需改本模块。
    """
    try:
        from netsentinel.vision import providers  # noqa: PLC0415 惰性导入:A61 允许缺席
    except Exception:  # noqa: BLE001 目录未就位属于正常并行态,不算错误
        return None
    catalog = getattr(providers, "PROVIDERS", None)
    return catalog if isinstance(catalog, dict) else None


def _known_providers() -> tuple[str, ...]:
    """受支持提供方清单:目录就位取目录键;缺席退回 A70 内置 20 家。"""
    catalog = _load_provider_catalog()
    return tuple(catalog.keys()) if catalog else keys.BUILTIN_PROVIDERS


# ---------------------------------------------------------------------------
# 密钥校验:只产出"问题类型"描述,绝不产出密钥内容
# ---------------------------------------------------------------------------


def _key_problem(stripped: str) -> str | None:
    """对 strip 后的密钥做合规检查,返回中文问题描述;合规返回 ``None``。

    规则(契约 A152):非空、长度 ≥ :data:`MIN_KEY_LENGTH`、不含空白
    (含换行)与控制 / 不可见字符。返回文案只描述问题类型与长度,
    **绝不包含密钥本体或其片段**(红线 33)。
    """
    if not stripped:
        return "密钥为空或仅空白"
    if len(stripped) < MIN_KEY_LENGTH:
        return f"密钥过短(长度 {len(stripped)},至少需要 {MIN_KEY_LENGTH} 位)"
    for ch in stripped:
        if ch == "\n":
            return "密钥包含换行符"
        if ch.isspace():
            return "密钥包含空白字符"
        if unicodedata.category(ch).startswith("C"):
            return "密钥包含控制或不可见字符"
    return None


# ---------------------------------------------------------------------------
# accept_key_input:受纳关口(校验 → set_key 落盘 → 掩码返回)
# ---------------------------------------------------------------------------


def accept_key_input(provider: str, key: str, *, base: str | None = None) -> dict:
    """受纳用户提供方密钥:校验通过即落盘,返回 ``{"ok", "masked", "stored"}``。

    流程(全程离线,绝不联网):

    1. **提供方合法性** ``provider``(strip 后)必须在受支持清单中
       (目录 ``PROVIDERS`` 惰性校验,缺席退回 ``keys.BUILTIN_PROVIDERS``
       兜底);未知 / 非字符串 → 中文 ``ValueError``;
    2. **密钥校验**(对 strip 后的值):非空、长度 ≥8、不含空白与控制
       字符、不含换行;不合格 → 中文 ``ValueError``,**消息只含长度 /
       问题类型,绝不回显密钥内容**(红线 33);
    3. 通过 → :func:`security.keys.set_key(provider, key, base=base)`
       落盘(``base=None`` 用默认 ``~/.netsentinel/keys``);``set_key``
       抛出的 ``ValueError`` 原样上抛(同样不含密钥本体);
    4. 返回 ``{"ok": True, "masked": 前4位+"****", "stored": True}``,
       并计 ``telemetry.inc("key_api.accept")``。

    日志纪律(红线 33):成功与拒绝的日志**只记提供方与长度**,
    绝不记密钥本体或片段。

    :param provider: 提供方注册名(如 ``"qwen"``),前后空白会被忽略;
    :param key: 密钥原文(password 语义),首尾空白会被去掉后落盘;
    :param base: 落盘根目录(透传 ``keys.set_key``);``None`` 为默认;
    :raises ValueError: 未知提供方 / 密钥不合格 / 落盘失败(均中文,不含密钥)。
    """
    name = provider.strip() if isinstance(provider, str) else ""
    known = _known_providers()
    if not isinstance(provider, str) or not name or name not in known:
        logger.warning(
            "拒绝密钥输入:提供方 %r 不在受支持目录中(共支持 %d 家)", name, len(known)
        )
        raise ValueError(
            f"未知提供方:{provider!r} 不在受支持的提供方目录中(共支持 {len(known)} 家)"
        )

    if not isinstance(key, str):
        logger.warning("拒绝提供方 %s 的密钥输入:密钥不是字符串", name)
        raise ValueError(f"密钥格式错误:提供方 {name} 的密钥必须是字符串,拒绝写入")

    stripped = key.strip()
    problem = _key_problem(stripped)
    if problem is not None:
        logger.warning(
            "拒绝提供方 %s 的密钥输入:%s(输入长度 %d)", name, problem, len(stripped)
        )
        raise ValueError(
            f"{problem}:提供方 {name} 的密钥被拒绝写入(输入长度 {len(stripped)})"
        )

    keys.set_key(name, stripped, base=base if base is not None else _DEFAULT_BASE)
    telemetry.inc("key_api.accept")
    logger.info(
        "已接受提供方 %s 的密钥输入(长度 %d),已落盘并收紧权限", name, len(stripped)
    )
    return {"ok": True, "masked": stripped[:4] + _MASK_SUFFIX, "stored": True}


# ---------------------------------------------------------------------------
# masked:已配置密钥的掩码查询(纯本地,不触网)
# ---------------------------------------------------------------------------


def masked(provider: str) -> str | None:
    """返回提供方已配置密钥的掩码(前 4 位 + ``****``);未配置返回 ``None``。

    经 :func:`security.keys.get_key` 三来源解析(配置项 → 环境变量 →
    密钥文件,默认 ``Config()`` 无配置项);纯本地读,绝不联网、绝不
    返回密钥本体(红线 33)。提供方名非法 / 非字符串时 ``get_key``
    按未配置处理,本函数返回 ``None``。
    """
    name = provider.strip() if isinstance(provider, str) else ""
    value = keys.get_key(name, Config())
    if not value:
        return None
    return value[:4] + _MASK_SUFFIX
