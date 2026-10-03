"""密钥管理与审计防篡改哈希链(A38,依据 CONTRACTS-V2.md §3 A38 条目)。

系统现在持有 GLM API 密钥,审计日志是举报可信度的凭证,本模块负责:

- ``get_glm_key(cfg)``:按 配置项 → 环境变量 ``NETSENTINEL_GLM_API_KEY`` →
  ``~/.netsentinel/glm_key`` 文件 的顺序解析 GLM 密钥;从文件读到密钥后
  建议收紧权限(POSIX ``chmod 600``;Windows 尝试 ``icacls``,失败仅提示,绝不抛出)。
- ``redact(obj)``:递归对形似密钥的字符串值与敏感键名对应的值打码,返回同构新对象,
  用于把密钥挡在日志 / 报告 / 审计之外。
- ``AuditChain``:为审计事件附加 ``prev_hash`` / ``entry_hash`` 哈希链字段,
  并可对既有 ``audit.jsonl`` 逐行校验,任何篡改或断链都能定位到行号。

只用标准库,绝不联网;所有用户可见文案均为中文。

V5 升级(契约 §1 菜单):

- **性能**::meth:`AuditChain.verify` 大文件流式逐行(打开文件句柄迭代,
  不再整读入内存;审计链长年追加,GB 级文件也不再撑爆诊断进程);
- **健壮性**::func:`redact` 递归深度防护(超 50 层的恶意嵌套整体打码);
  verify 遇到非法 UTF-8 字节不再抛 UnicodeDecodeError,按"该行被篡改"回报;
  Windows ``icacls`` 输出改为字节捕获(与 keys.py 同口径,杜绝 GBK 解码问题);
- **可观测性**::func:`redact` 记 ``vault.redact_items``(处理条数)、
  verify 记 ``vault.verify`` / 失败 ``vault.verify_failures``(仅名称与数字)。

用法示例::

    from netsentinel.security import vault

    vault.get_glm_key(cfg)                        # cfg > 环境变量 > ~/.netsentinel/glm_key
    vault.redact({"api_key": "sk-x..."})          # {"api_key": "****"}
    chain = vault.AuditChain()
    rec = chain.wrap("", "scan_started", url="http://a")
    chain.verify("audit.jsonl")                   # (True, "校验通过:N 条...")
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import re
import stat
import subprocess
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config, now_iso

__all__ = ["get_glm_key", "redact", "AuditChain"]

logger = logging.getLogger(__name__)

#: 环境变量名:GLM API 密钥的第二优先级来源
ENV_GLM_API_KEY = "NETSENTINEL_GLM_API_KEY"

#: 密钥文件(相对家目录)路径
KEY_FILE_RELPATH = pathlib.PurePosixPath(".netsentinel/glm_key")

# ---------------------------------------------------------------------------
# redact:密钥打码
# ---------------------------------------------------------------------------

#: 带前缀的密钥样式:sk-xxxx / id-xxxx,前缀后至少 8 位字母数字
_PREFIXED_SECRET_RE = re.compile(r"^(?:sk-|id-)[A-Za-z0-9]{8,}$")
#: 无前缀的长字母数字串(覆盖 32 位以上 hex / base32 样式令牌)
_LONG_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{32,}$")
#: HTTP Authorization 头样式
_BEARER_MARK = "Bearer "
#: 敏感键名片段(不区分大小写)
_SENSITIVE_KEY_RE = re.compile(r"key|secret|token|password", re.IGNORECASE)

#: 打码后缀
_MASK_SUFFIX = "****"

#: 递归打码的最大深度(V5 健壮性):更深的嵌套视为恶意构造,整体打码
_MAX_REDACT_DEPTH = 50


def _mask_text(text: str) -> str:
    """保留前 4 位 + 掩码,例如 ``sk-abc...`` → ``sk-a****``。"""
    return text[:4] + _MASK_SUFFIX


def redact(obj: Any) -> Any:
    """递归打码:返回与 ``obj`` 同构的新对象,绝不修改入参。

    规则(CONTRACTS-V2 §3 A38):

    - 字符串值命中密钥样式之一 → 保留前 4 位 + ``****``:

      * ``^(sk-|id-)[A-Za-z0-9]{8,}$``
      * ``^[A-Za-z0-9]{32,}$``(含 32 位以上 hex)
      * 含 ``"Bearer "``

    - dict 的键名含 ``key`` / ``secret`` / ``token`` / ``password``
      (不区分大小写)→ 值无论类型直接置为 ``****``;
    - dict / list / tuple 递归保构;其余标量原样返回;
    - V5 深度防护:递归超过 :data:`_MAX_REDACT_DEPTH` 层时,深层内容整体
      置 ``****`` 并记 WARNING(防恶意嵌套构造打码崩溃);处理条数计入
      ``telemetry.inc("vault.redact_items", n)``(仅数字,红线 17)。
    """
    state = [0, False]  # [访问节点数, 是否已告警]
    result = _redact(obj, 0, state)
    telemetry.inc("vault.redact_items", state[0])
    return result


def _redact(obj: Any, depth: int, state: list) -> Any:
    """redact 的递归实现(带深度防护与节点计数,均为内部细节)。"""
    state[0] += 1
    if depth > _MAX_REDACT_DEPTH:
        if not state[1]:
            state[1] = True
            logger.warning(
                "redact 递归深度超过 %d 层,深层内容整体打码(疑似恶意嵌套)",
                _MAX_REDACT_DEPTH,
            )
        return _MASK_SUFFIX
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SENSITIVE_KEY_RE.search(k):
                out[k] = _MASK_SUFFIX
            else:
                out[k] = _redact(v, depth + 1, state)
        return out
    if isinstance(obj, tuple):
        return tuple(_redact(v, depth + 1, state) for v in obj)
    if isinstance(obj, list):
        return [_redact(v, depth + 1, state) for v in obj]
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
# get_glm_key:密钥三来源解析
# ---------------------------------------------------------------------------


def _tighten_key_file_permissions(path: pathlib.Path) -> None:
    """收紧密钥文件权限;任何失败只记日志提示,绝不抛出。

    - POSIX:``os.chmod(path, 0o600)``;
    - Windows(``os.name == 'nt'``):尝试 ``icacls`` 移除继承并仅授权当前用户,
      失败仅提示用户手动处理。V5:输出按**字节**捕获(不传 ``text=True``,
      与 keys.py 同口径),避免非 UTF-8 / GBK 控制台输出触发解码错误。
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


def get_glm_key(cfg: Config) -> str:
    """解析 GLM API 密钥,返回密钥字符串;全无来源时返回 ``""``。

    优先级(先到先得,来源记入日志):

    1. ``cfg.glm_api_key`` 非空;
    2. 环境变量 ``NETSENTINEL_GLM_API_KEY``(非空生效,info 提示来源为 env);
    3. ``~/.netsentinel/glm_key`` 文件首行(strip 后非空生效),
       读到后尝试收紧文件权限(POSIX chmod 600 / Windows icacls,失败仅提示);
    4. 均无 → ``""``(info 提示未配置)。
    """
    key = (cfg.glm_api_key or "").strip()
    if key:
        logger.info("GLM API 密钥来源:配置项 glm_api_key")
        return key

    env_key = (os.environ.get(ENV_GLM_API_KEY) or "").strip()
    if env_key:
        logger.info("GLM API 密钥来源:环境变量 %s", ENV_GLM_API_KEY)
        return env_key

    key_path = pathlib.Path.home() / KEY_FILE_RELPATH
    if key_path.is_file():
        try:
            lines = key_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("读取密钥文件 %s 失败:%s", key_path, exc)
        else:
            first_line = lines[0].strip() if lines else ""
            if first_line:
                logger.info("GLM API 密钥来源:文件 %s", key_path)
                _tighten_key_file_permissions(key_path)
                return first_line
            logger.info("密钥文件 %s 首行为空,视为未配置", key_path)

    logger.info(
        "未找到 GLM API 密钥(配置项 glm_api_key / 环境变量 %s / 文件 ~/.netsentinel/glm_key 均为空)",
        ENV_GLM_API_KEY,
    )
    return ""


# ---------------------------------------------------------------------------
# AuditChain:审计防篡改哈希链
# ---------------------------------------------------------------------------


def _entry_hash(prev_hash: str, record: dict[str, Any]) -> str:
    """计算一条审计记录的链哈希:sha256(prev_hash + "|" + 其余字段 JSON) 前 32 位十六进制。

    ``record`` 为不含 ``entry_hash`` 的其余字段;``sort_keys=True`` 保证对字段
    顺序稳定,``ensure_ascii=False`` 保证中文原样参与哈希。``wrap`` 与 ``verify``
    共用本函数,口径一致。
    """
    payload = prev_hash + "|" + json.dumps(record, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class AuditChain:
    """审计日志哈希链:wrap 产链、verify 验链。

    与既有 :class:`~netsentinel.logging_util.JsonlAuditLogger` 兼容——
    ``wrap`` 的返回值就是一行 JSON 记录(dict),由调用方序列化落盘;
    旧格式(无 ``entry_hash`` 字段)的行 ``verify`` 会计数跳过,不阻断校验。
    """

    #: 记录中的链字段名
    PREV_FIELD = "prev_hash"
    HASH_FIELD = "entry_hash"

    def wrap(self, prev_hash: str, event: str, **fields: Any) -> dict[str, Any]:
        """包装一条审计记录:附 ``ts`` / ``prev_hash`` / ``entry_hash``。

        产出 ``{"ts": now_iso(), "event": event, **fields, "prev_hash": prev_hash,
        "entry_hash": sha256(...)[:32]}``;``entry_hash`` 覆盖除其自身外的全部
        字段(含 ``prev_hash`` 与 ``ts``),对字段顺序稳定。
        """
        record: dict[str, Any] = {
            "ts": now_iso(),
            "event": event,
            **fields,
            self.PREV_FIELD: prev_hash,
        }
        record[self.HASH_FIELD] = _entry_hash(prev_hash, record)
        return record

    def verify(self, path: str) -> tuple[bool, str]:
        """逐行校验 JSONL 审计文件的哈希链,返回 ``(是否通过, 中文结论)``。

        - 无 ``entry_hash`` 字段的旧行计数跳过(``legacy_n``);
        - 有哈希的行:重算 ``entry_hash`` 比对,并与上一条哈希记录的
          ``entry_hash`` 链式衔接;任何篡改 / 断链 / 无法解析的行 →
          ``(False, "...第 N 行被篡改或断链...")``(N 为物理行号,从 1 起);
        - 全部通过 → ``(True, "校验通过:N 条哈希链记录,M 条旧格式跳过")``;
        - 空文件或文件不存在 → ``(True, "无记录")``。

        V5:大文件**流式逐行**(打开句柄迭代,不再 ``read_text`` 整读);
        非法 UTF-8 字节按 U+FFFD 替换后由逐行 JSON 解析判"被篡改",
        绝不抛 UnicodeDecodeError。调用与失败分别计入 ``vault.verify`` /
        ``vault.verify_failures``(仅数字)。
        """
        telemetry.inc("vault.verify")
        ok, message = self._verify_streaming(path)
        if not ok:
            telemetry.inc("vault.verify_failures")
        return ok, message

    def _verify_streaming(self, path: str) -> tuple[bool, str]:
        """verify 的流式实现(私有):逐行读、逐行校验,内存占用与文件大小无关。"""
        file_path = pathlib.Path(path)
        if not file_path.is_file():
            return True, "无记录"
        try:
            fh = file_path.open("r", encoding="utf-8", errors="replace")
        except OSError as exc:
            return False, f"审计文件无法读取:{exc}"

        hashed_n = 0
        legacy_n = 0
        prev_entry_hash: str | None = None  # 上一条含哈希记录的 entry_hash
        with fh:
            for lineno, line in enumerate(fh, start=1):
                if not line.strip():
                    continue  # 容忍空行
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    return False, f"校验失败:第 {lineno} 行被篡改或断链(无法解析为 JSON)"
                if not isinstance(record, dict):
                    return False, f"校验失败:第 {lineno} 行被篡改或断链(记录不是 JSON 对象)"
                stored_hash = record.get(self.HASH_FIELD)
                if not isinstance(stored_hash, str) or not stored_hash:
                    legacy_n += 1  # 旧格式行:无哈希,计数跳过
                    continue

                prev_hash = record.get(self.PREV_FIELD)
                if not isinstance(prev_hash, str):
                    return False, f"校验失败:第 {lineno} 行被篡改或断链(prev_hash 缺失)"
                if prev_entry_hash is not None and prev_hash != prev_entry_hash:
                    return False, f"校验失败:第 {lineno} 行被篡改或断链(与上一条哈希链断裂)"

                rest = {k: v for k, v in record.items() if k != self.HASH_FIELD}
                if _entry_hash(prev_hash, rest) != stored_hash:
                    return False, f"校验失败:第 {lineno} 行被篡改或断链(entry_hash 不匹配)"

                prev_entry_hash = stored_hash
                hashed_n += 1

        if hashed_n == 0 and legacy_n == 0:
            return True, "无记录"
        return True, f"校验通过:{hashed_n} 条哈希链记录,{legacy_n} 条旧格式跳过"
