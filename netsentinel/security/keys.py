"""多平台密钥环(A70,依据 CONTRACTS-V4.md §0 红线 17 / §4 A70 条目)。

V4 把视觉模型接入扩展到 20 个提供方,各家密钥需要统一口径管理。本模块负责:

- ``get_key(provider, cfg)``:按 **配置项 ``vlm_api_keys`` → 提供方目录
  ``key_envs`` 逐个环境变量 → OS 密钥库 keyring(可选,P4)→
  ``~/.netsentinel/keys/<provider>`` 文件首行** 的优先级解析密钥,
  全无来源时返回 ``""``;绝不打印 / 记录密钥本身(红线 17);
- ``set_key(provider, key, *, base="~/.netsentinel/keys")``:把密钥写入
  ``<base>/<provider>`` 文件(utf-8 首行,自动建目录)并尝试收紧权限
  (POSIX ``chmod 600``;Windows 尝试 ``icacls``,输出按字节捕获防 GBK 解码问题,
  只看退出码,失败仅中文提示,绝不抛出);
- ``store_key_to_keyring(name, value)``(P4):把密钥**显式**写入 OS 密钥库
  keyring(服务名 ``netsentinel``,用户名 = 提供方名);读取路径绝不自动写回;
- ``configured(cfg)``:对提供方全目录逐家返回"密钥是否已配置"布尔表,
  供 vlmctl list / WebUI 面板使用(只显示 已配置/未配置,绝不回显密钥);
- ``redact_keys(obj)``:递归打码——``vlm_api_keys`` 的值保留前 4 位 + ``****``,
  其余沿用 vault.redact 语义(sk-/id- 前缀、32 位以上长令牌、Bearer、敏感键名);
  本模块独立实现,不硬依赖 vault。

提供方目录由 A61 ``netsentinel.vision.providers.PROVIDERS`` 提供;该模块未就位时,
环境变量退回约定两形态 ``NETSENTINEL_{提供方大写}_API_KEY`` /
``{提供方大写}_API_KEY``,目录清单退回内置 20 家(与契约 §2 一致)。

P4 安全审计清偿(OS 密钥库托管,凭据失窃面收敛):

- **keyring 为可选 extra,绝非必装依赖**(红线:零第三方运行时依赖):
  需要时自行 ``pip install keyring``;未安装 / 无可用后端 / 读取抛任何异常,
  都**静默降级**到文件路径来源(仅 debug 日志),行为与历史版本一致;
- 读取顺序为 显式参数(cfg) > 环境变量 > keyring > 文件路径:keyring 命中
  即返回(凭据留在 OS 密钥库,不落明文文件);文件来源仍作最后兜底;
- 写入 keyring 只经 :func:`store_key_to_keyring` **显式**触发——get_key
  读取路径绝不自动写回,避免读取行为意外持久化凭据。

只用标准库,绝不联网;所有用户可见文案均为中文。

V5 升级(契约 §1 菜单):

- **性能**::func:`configured` 对 ``os.environ`` 做一次快照批量传递,
  20 家逐家解析不再重复穿透进程环境(目录加载也从每家一次降为整表一次);
- **健壮性**::func:`redact_keys` 递归深度防护(超 50 层的恶意嵌套整体打码,
  不再可能以深层嵌套触发递归崩溃);
- **可观测性**::func:`configured` 记 ``telemetry.inc("keys.configured")``
  (值为已配置家数)、:func:`set_key` 记 ``keys.set_key``、
  :func:`redact_keys` 记 ``keys.redact_items``(仅名称与数字,红线 17)、
  :func:`store_key_to_keyring` 成功记 ``keys.keyring.store``。

用法示例::

    from netsentinel.security import keys

    keys.set_key("qwen", "sk-...")          # 落盘 ~/.netsentinel/keys/qwen
    keys.store_key_to_keyring("qwen", "sk-...")  # 可选:改存 OS 密钥库
    keys.get_key("qwen", cfg)               # cfg > 环境变量 > keyring > 密钥文件
    keys.configured(cfg)                    # {"glm": True, ..., "ollama": False}
    keys.redact_keys({"vlm_api_keys": {...}})  # 打码副本
"""
from __future__ import annotations

import logging
import os
import pathlib
import re
import stat
import subprocess
from typing import Any, Mapping

from netsentinel import telemetry
from netsentinel.contracts import Config

__all__ = ["get_key", "set_key", "store_key_to_keyring", "configured", "redact_keys"]

logger = logging.getLogger(__name__)

#: 密钥目录(相对家目录):每提供方一个同名文件,首行即密钥
KEY_DIR_RELPATH = pathlib.PurePosixPath(".netsentinel/keys")

#: keyring 服务名(OS 密钥库中的命名空间;提供方名作用户名,如 get_password("netsentinel", "qwen"))
KEYRING_SERVICE = "netsentinel"

#: 提供方目录未就位(A61 未落地)时的内置 20 家清单,顺序与契约 §2 表格一致
BUILTIN_PROVIDERS: tuple[str, ...] = (
    "glm",
    "openai",
    "anthropic",
    "gemini",
    "qwen",
    "doubao",
    "hunyuan",
    "moonshot",
    "minimax",
    "stepfun",
    "siliconflow",
    "ernie",
    "openrouter",
    "groq",
    "together",
    "xai",
    "ollama",
    "vllm",
    "lmstudio",
    "xinference",
)

#: 提供方名同时用作文件名 / 环境变量片段:只允许字母数字开头 + 字母数字下划线连字符,
#: 拒绝路径分隔符与 ``..``,防止借提供方名读写目录之外的文件
_SAFE_PROVIDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

#: 环境变量名形态:提供方名大写后拼入(如 qwen → NETSENTINEL_QWEN_API_KEY / QWEN_API_KEY)
_ENV_PREFIX = "NETSENTINEL_"
_ENV_SUFFIX = "_API_KEY"

# ---------------------------------------------------------------------------
# redact_keys:密钥打码(独立实现,语义对齐 vault.redact + vlm_api_keys 特例)
# ---------------------------------------------------------------------------

#: 带前缀的密钥样式:sk-xxxx / id-xxxx,前缀后至少 8 位字母数字
_PREFIXED_SECRET_RE = re.compile(r"^(?:sk-|id-)[A-Za-z0-9]{8,}$")
#: 无前缀的长字母数字串(覆盖 32 位以上 hex / base32 样式令牌)
_LONG_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{32,}$")
#: HTTP Authorization 头样式
_BEARER_MARK = "Bearer "
#: 敏感键名片段(不区分大小写)
_SENSITIVE_KEY_RE = re.compile(r"key|secret|token|password", re.IGNORECASE)

#: V4 配置里"提供方 → 密钥"字典的字段名:其值打码为 前 4 位 + ****(保留提供方名便于排障)
_VLM_API_KEYS_FIELD = "vlm_api_keys"

#: 打码后缀
_MASK_SUFFIX = "****"

#: 递归打码的最大深度(V5 健壮性):更深的嵌套视为恶意构造,整体打码
_MAX_REDACT_DEPTH = 50


def _mask_text(text: str) -> str:
    """保留前 4 位 + 掩码,例如 ``sk-abc...`` → ``sk-a****``。"""
    return text[:4] + _MASK_SUFFIX


def _mask_provider_key_map(value: Any) -> Any:
    """打码 ``vlm_api_keys`` 的值:值 → 前 4 位 + ``****``,提供方名保留。

    值为字典(正常形态)时逐项打码;值不是字典(配置异常)时整体置 ``****``。
    """
    if isinstance(value, dict):
        return {
            k: _mask_text(v) if isinstance(v, str) else _MASK_SUFFIX
            for k, v in value.items()
        }
    return _MASK_SUFFIX


def redact_keys(obj: Any) -> Any:
    """递归打码:返回与 ``obj`` 同构的新对象,绝不修改入参。

    规则(CONTRACTS-V4 §0 红线 17,A70 口径):

    - 键名为 ``vlm_api_keys`` 且值为字典 → 每个提供方的密钥值保留前 4 位 +
      ``****``(提供方名保留,便于在日志里看出"哪几家已配置");
    - 其余键名含 ``key`` / ``secret`` / ``token`` / ``password``
      (不区分大小写)→ 值无论类型直接置为 ``****``;
    - 字符串值命中密钥样式之一 → 保留前 4 位 + ``****``:

      * ``^(sk-|id-)[A-Za-z0-9]{8,}$``
      * ``^[A-Za-z0-9]{32,}$``(含 32 位以上 hex)
      * 含 ``"Bearer "``

    - dict / list / tuple 递归保构;其余标量原样返回;
    - V5 深度防护:递归超过 :data:`_MAX_REDACT_DEPTH` 层时,深层内容整体
      置 ``****`` 并记 WARNING(防恶意嵌套构造打码崩溃),处理条数计入
      ``telemetry.inc("keys.redact_items", n)``(仅数字)。
    """
    state = [0, False]  # [访问节点数, 是否已告警]
    result = _redact_keys(obj, 0, state)
    telemetry.inc("keys.redact_items", state[0])
    return result


def _redact_keys(obj: Any, depth: int, state: list) -> Any:
    """redact_keys 的递归实现(带深度防护与节点计数,均为内部细节)。"""
    state[0] += 1
    if depth > _MAX_REDACT_DEPTH:
        if not state[1]:
            state[1] = True
            logger.warning(
                "redact_keys 递归深度超过 %d 层,深层内容整体打码(疑似恶意嵌套)",
                _MAX_REDACT_DEPTH,
            )
        return _MASK_SUFFIX
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            if k == _VLM_API_KEYS_FIELD:
                out[k] = _mask_provider_key_map(v)
            elif isinstance(k, str) and _SENSITIVE_KEY_RE.search(k):
                out[k] = _MASK_SUFFIX
            else:
                out[k] = _redact_keys(v, depth + 1, state)
        return out
    if isinstance(obj, tuple):
        return tuple(_redact_keys(v, depth + 1, state) for v in obj)
    if isinstance(obj, list):
        return [_redact_keys(v, depth + 1, state) for v in obj]
    if isinstance(obj, str):
        if (
            _PREFIXED_SECRET_RE.match(obj)
            or _LONG_TOKEN_RE.match(obj)
            or _BEARER_MARK in obj
        ):
            return _mask_text(obj)
        return obj
    return obj


# ---------------------------------------------------------------------------
# 提供方目录:A61 未就位时的约定退化
# ---------------------------------------------------------------------------


def _load_catalog() -> dict[str, Any] | None:
    """惰性加载 A61 提供方目录 ``PROVIDERS``;未就位 / 导入失败返回 ``None``。

    每次调用都重新尝试导入(模块缓存后开销可忽略),保证 A61 后续落地后
    本模块无需改动即可切换到真实目录。
    """
    try:
        from netsentinel.vision import providers  # noqa: PLC0415 惰性导入:A61 允许缺席
    except Exception:  # noqa: BLE001 目录未就位属于正常并行态,不算错误
        return None
    catalog = getattr(providers, "PROVIDERS", None)
    return catalog if isinstance(catalog, dict) else None


def _conventional_env_names(provider: str) -> list[str]:
    """目录未就位 / 未知提供方时的约定环境变量两形态。

    如 ``qwen`` → ``["NETSENTINEL_QWEN_API_KEY", "QWEN_API_KEY"]``。
    """
    upper = provider.upper()
    return [f"{_ENV_PREFIX}{upper}{_ENV_SUFFIX}", f"{upper}{_ENV_SUFFIX}"]


def _env_names_for(provider: str, catalog: dict[str, Any] | None = None) -> list[str]:
    """取提供方的密钥环境变量名列表(按优先级排序)。

    - 目录就位且提供方在目录中 → 目录 ``key_envs``(本地提供方为空列表,
      即完全不看环境变量);
    - 目录未就位或提供方未知 → 约定两形态;
    - ``catalog`` 可传入已加载目录(V5 批量化:configured 全表解析时只加载一次)。
    """
    if catalog is None:
        catalog = _load_catalog()
    if catalog is not None and provider in catalog:
        envs = getattr(catalog[provider], "key_envs", None)
        return [str(name) for name in (envs or [])]
    return _conventional_env_names(provider)


def _expand_dir(base: str) -> pathlib.Path:
    """把 ``base`` 解析为目录 ``Path``:``~`` / ``~/`` 前缀经 ``Path.home()`` 展开。

    与 :func:`pathlib.Path.expanduser`(走 ``os.path.expanduser`` 的环境变量)
    不同,这里统一经 ``Path.home()``,与 vault.py 及测试的家目录隔离手段一致;
    其余形态(绝对 / 相对路径)按字面解析。
    """
    text = str(base)
    if text == "~":
        return pathlib.Path.home()
    if text.startswith("~/"):
        return pathlib.Path.home() / text[2:]
    return pathlib.Path(text)


def _key_file_path(provider: str, base: str = "~/.netsentinel/keys") -> pathlib.Path:
    """密钥文件路径:``<base>/<provider>``;``base`` 支持家目录 ``~`` 前缀展开。"""
    return _expand_dir(base) / provider


# ---------------------------------------------------------------------------
# get_key:四来源优先级解析(cfg > 环境变量 > keyring > 文件)
# ---------------------------------------------------------------------------


def _load_keyring() -> Any | None:
    """惰性导入可选依赖 keyring;未安装 / 无可用后端时返回 ``None``(绝不抛出)。

    keyring 为**可选 extra**(需要时 ``pip install keyring``),绝非必装依赖
    ——导入失败按"缺席"处理:读取链静默跳过 OS 密钥库一步,行为与历史
    版本一致(红线:零第三方运行时依赖)。
    """
    try:
        import keyring  # noqa: PLC0415 惰性导入:可选 extra,缺席属正常态
    except Exception:  # noqa: BLE001 未安装 / 后端初始化失败均按缺席处理
        logger.debug("keyring 不可用(未安装或无可用后端),跳过 OS 密钥库来源")
        return None
    return keyring


def _keyring_get_password(provider: str) -> str:
    """从 OS 密钥库读取提供方密钥;任何失败(未安装/未命中/异常)返回 ``""``。

    - 服务名 :data:`KEYRING_SERVICE`(``"netsentinel"``),用户名 = 提供方名;
    - 未命中(keyring 返回 None / 空白 / 非字符串)与任何异常一律静默降级
      到文件路径来源,仅记 **debug** 日志(密钥库故障不构成告警噪音);
    - 日志只含提供方名与异常类型,绝不回显密钥或异常消息(防后端把凭据
      拼进异常文本造成泄漏,红线 17)。
    """
    keyring = _load_keyring()
    if keyring is None:
        return ""
    try:
        value = keyring.get_password(KEYRING_SERVICE, provider)
    except Exception as exc:  # noqa: BLE001 密钥库故障绝不阻断解析:静默降级
        logger.debug(
            "keyring 读取提供方 %s 的密钥失败(%s),静默降级到文件路径来源",
            provider,
            type(exc).__name__,
        )
        return ""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def get_key(provider: str, cfg: Config, *, environ: Mapping[str, str] | None = None) -> str:
    """解析指定提供方的 API 密钥,返回密钥字符串;全无来源时返回 ``""``。

    优先级(先到先得,仅记录来源,绝不记录密钥本身):

    1. ``cfg.vlm_api_keys[provider]`` 非空;
    2. 提供方目录 ``key_envs`` 逐个环境变量(目录未就位 / 未知提供方时
       退回 ``NETSENTINEL_{提供方大写}_API_KEY`` 与 ``{提供方大写}_API_KEY``
       两形态,逐个尝试,首个非空生效);
    3. OS 密钥库 keyring(服务名 ``netsentinel``,可选 extra,未安装时
       ``pip install keyring``):命中即返回,凭据不落明文文件(P4);
       未安装 / 未命中 / 任何异常**静默降级**到第 4 步(仅 debug 日志);
    4. ``~/.netsentinel/keys/<provider>`` 文件首行(strip 后非空生效);
    5. 均无 → ``""``。

    读取路径**绝不**反向写入 keyring(自动写回属意外持久化,只有显式调用
    :func:`store_key_to_keyring` 才落 OS 密钥库)。

    提供方名非法(含路径分隔符 / ``..`` 等)时不触碰文件系统,直接视为未配置。

    V5 批量化:``environ`` 可传入环境快照(:func:`configured` 全表解析时
    ``dict(os.environ)`` 一次,避免 20 家 × 逐环境变量重复穿透进程环境);
    缺省 ``None`` 时照旧现查 ``os.environ``,单次调用行为不变。
    """
    if not isinstance(provider, str) or not _SAFE_PROVIDER_RE.match(provider):
        logger.warning("提供方名称非法:%r,按未配置处理", provider)
        return ""

    env = os.environ if environ is None else environ
    env_names = _env_names_for(provider)  # V5:目录只加载一次(下方日志复用)

    # 1) 配置项(最高优先级)
    cfg_keys = getattr(cfg, "vlm_api_keys", None)
    raw = cfg_keys.get(provider) if isinstance(cfg_keys, dict) else None
    cfg_key = raw.strip() if isinstance(raw, str) else ""
    if cfg_key:
        logger.info("提供方 %s 密钥来源:配置项 vlm_api_keys", provider)
        return cfg_key

    # 2) 环境变量(按 key_envs / 约定两形态逐个)
    for env_name in env_names:
        env_key = (env.get(env_name) or "").strip()
        if env_key:
            logger.info("提供方 %s 密钥来源:环境变量 %s", provider, env_name)
            return env_key

    # 3) OS 密钥库 keyring(P4:可选 extra,未安装/未命中/异常 → 静默降级文件)
    keyring_key = _keyring_get_password(provider)
    if keyring_key:
        logger.info(
            "提供方 %s 密钥来源:OS 密钥库 keyring(服务名 %s)", provider, KEYRING_SERVICE
        )
        return keyring_key

    # 4) 密钥文件首行
    key_path = _key_file_path(provider)
    if key_path.is_file():
        try:
            lines = key_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("读取提供方 %s 的密钥文件 %s 失败:%s", provider, key_path, exc)
        else:
            first_line = lines[0].strip() if lines else ""
            if first_line:
                logger.info("提供方 %s 密钥来源:文件 %s", provider, key_path)
                return first_line
            logger.info("提供方 %s 的密钥文件 %s 首行为空,视为未配置", provider, key_path)

    logger.info(
        "提供方 %s 未配置密钥(配置项 vlm_api_keys / 环境变量 %s / keyring / 文件 %s 均为空)",
        provider,
        "、".join(env_names) or "(该提供方无约定环境变量)",
        key_path,
    )
    return ""


# ---------------------------------------------------------------------------
# set_key:落盘 + 权限收紧
# ---------------------------------------------------------------------------


def _tighten_key_file_permissions(path: pathlib.Path) -> None:
    """收紧密钥文件权限;任何失败只记日志提示,绝不抛出。

    - POSIX:``os.chmod(path, 0o600)``;
    - Windows(``os.name == 'nt'``):尝试 ``icacls`` 移除继承并仅授权当前用户。
      输出按**字节**捕获(不传 ``text=True``),避免非 UTF-8 / GBK 控制台输出
      触发解码错误;只看退出码,非 0 仅中文提示手动处理。
    """
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME") or os.environ.get("USERDOMAIN") or ""
            grant = f"{user}:F" if user else f"{path.owner()}:F"
            result = subprocess.run(  # noqa: S603 本地权限命令,参数受控
                ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
                capture_output=True,  # 字节捕获:绝不解码 icacls 输出,只看退出码
                timeout=10,
                check=False,
            )
            if result.returncode != 0:
                logger.info(
                    "密钥文件 %s 未能自动收紧权限(icacls 退出码 %s),"
                    "建议手动执行:icacls \"%s\" /inheritance:r /grant:r \"%s\"",
                    path,
                    result.returncode,
                    path,
                    grant,
                )
            else:
                logger.info("已通过 icacls 收紧密钥文件权限:%s", path)
        else:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
            logger.info("已将密钥文件权限收紧为仅属主可读写(chmod 600):%s", path)
    except Exception as exc:  # noqa: BLE001 权限收紧是建议性动作,失败不阻断
        logger.warning(
            "无法自动收紧密钥文件 %s 的权限(%s),建议手动限制为仅当前用户可读。",
            path,
            exc,
        )


def set_key(provider: str, key: str, *, base: str = "~/.netsentinel/keys") -> str:
    """把提供方密钥写入 ``<base>/<provider>`` 文件并收紧权限,返回文件路径字符串。

    - 密钥以 utf-8 写为文件首行(自动补换行,读取方只取首行);
    - 目录不存在时自动逐级创建;
    - 权限收紧:POSIX ``chmod 600``;Windows 尝试 ``icacls``,失败仅中文提示;
    - ``key`` 为空串 / 纯空白 → ``ValueError``(中文);提供方名非法同样拒绝;
    - 日志只提示路径与环境变量替代写法,绝不回显密钥。
    """
    if not isinstance(provider, str) or not _SAFE_PROVIDER_RE.match(provider):
        raise ValueError(
            f"提供方名称非法:{provider!r}(仅允许字母/数字开头,含字母、数字、下划线、连字符)"
        )
    stripped = key.strip() if isinstance(key, str) else ""
    if not stripped:
        raise ValueError(f"密钥不能为空:提供方 {provider} 的密钥为空串,拒绝写入")

    key_path = _key_file_path(provider, base)
    try:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text(stripped + "\n", encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"写入提供方 {provider} 的密钥文件 {key_path} 失败:{exc}") from exc

    _tighten_key_file_permissions(key_path)
    telemetry.inc("keys.set_key")

    env_hint = "、".join(_env_names_for(provider))
    if env_hint:
        logger.info(
            "已写入提供方 %s 的密钥文件:%s(也可改用环境变量 %s 配置,避免落盘)",
            provider,
            key_path,
            env_hint,
        )
    else:
        logger.info(
            "已写入提供方 %s 的密钥文件:%s(该提供方无约定环境变量)",
            provider,
            key_path,
        )
    return str(key_path)


# ---------------------------------------------------------------------------
# store_key_to_keyring:显式写入 OS 密钥库(P4,可选 extra)
# ---------------------------------------------------------------------------


def store_key_to_keyring(name: str, value: str) -> bool:
    """把提供方密钥**显式**写入 OS 密钥库 keyring;返回是否成功(绝不抛出)。

    - 服务名固定 :data:`KEYRING_SERVICE`(``"netsentinel"``),用户名 = 提供方名
      ——与 :func:`get_key` 读取链第 3 步同一坐标系,写入即可读回;
    - **仅显式调用才写**:get_key 读取路径绝不自动写回(避免读取行为意外
      持久化凭据);本助手与 :func:`set_key` 一样是运维动作;
    - keyring 为可选 extra:未安装(可 ``pip install keyring``)/ 无可用后端 /
      写入异常 → 返回 ``False`` 并记中文日志;提供方名非法 / 密钥空白 →
      :class:`ValueError`(与 set_key 同口径,属用法错误须当场暴露);
    - 成功计数 ``telemetry.inc("keys.keyring.store")``;日志只含提供方名与
      服务名,绝不回显密钥或后端异常消息(红线 17)。

    :param name:  提供方名(同 set_key 的合法名规则);
    :param value: 密钥(写入前 strip,与 set_key 一致);
    :return: ``True`` 已入库;``False`` keyring 不可用或写入失败。
    """
    if not isinstance(name, str) or not _SAFE_PROVIDER_RE.match(name):
        raise ValueError(
            f"提供方名称非法:{name!r}(仅允许字母/数字开头,含字母、数字、下划线、连字符)"
        )
    stripped = value.strip() if isinstance(value, str) else ""
    if not stripped:
        raise ValueError(f"密钥不能为空:提供方 {name} 的密钥为空串,拒绝写入")

    keyring = _load_keyring()
    if keyring is None:
        logger.info(
            "keyring 不可用,未能把提供方 %s 的密钥写入 OS 密钥库"
            "(可选 extra:pip install keyring;也可继续用 set_key 落盘文件)",
            name,
        )
        return False
    try:
        keyring.set_password(KEYRING_SERVICE, name, stripped)
    except Exception as exc:  # noqa: BLE001 密钥库写入失败返回 False,绝不抛出
        logger.warning(
            "写入提供方 %s 的密钥到 OS 密钥库失败(%s)", name, type(exc).__name__
        )
        return False
    telemetry.inc("keys.keyring.store")
    logger.info(
        "已把提供方 %s 的密钥写入 OS 密钥库(服务名 %s);"
        "如需移除请在系统凭据管理器 / seahorse 中删除该条目",
        name,
        KEYRING_SERVICE,
    )
    return True


# ---------------------------------------------------------------------------
# configured:全目录配置体检
# ---------------------------------------------------------------------------


def configured(cfg: Config) -> dict[str, bool]:
    """对提供方全目录逐家返回"密钥是否已配置"。

    目录就位时遍历 ``PROVIDERS`` 全部键;未就位时遍历内置 20 家清单
    (与契约 §2 一致)。每家调用 :func:`get_key`,非空即 ``True``。
    本地提供方(ollama/vllm/lmstudio/xinference)免密钥,天然为 ``False``,
    由调用方按目录 ``local`` 标记另行展示。

    V5 批量化:``os.environ`` 只做一次快照传给逐家解析(旧实现对 20 家
    逐家现查,每家最多穿透 2 个环境变量 + 重复加载目录;现在整表一次)。
    已配置家数计入 ``telemetry.inc("keys.configured", 家数)``(仅数字)。
    """
    catalog = _load_catalog()
    names = list(catalog.keys()) if catalog else list(BUILTIN_PROVIDERS)
    env_snapshot = dict(os.environ)  # V5:一次性快照,避免逐家重复穿透进程环境
    result = {provider: bool(get_key(provider, cfg, environ=env_snapshot)) for provider in names}
    telemetry.inc("keys.configured", sum(result.values()))
    return result
