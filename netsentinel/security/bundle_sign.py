"""证据包 HMAC 签名(A54,依据 CONTRACTS-V3.md §3 A54 条目)。

举报材料的防篡改完整性担保:对证据包目录(A11 packager 产出的
``manifest.json`` + 证据文件)计算签名并写回 manifest,
事后 :meth:`BundleSigner.verify` 重算比对——谁动过一张图、增删过一个文件,
都能被检出。

签名覆盖范围(契约固定口径):

- manifest ``files`` 清单内每个 ``path``(按 path 排序)对应 ``bundle_dir``
  内**实际文件**的 sha256;文件缺失 → 该项记 ``"MISSING:<path>"``,不拒绝
  签名但记录告警,交由 :meth:`verify` 判不通过;
- manifest 清单之外、目录内多出的文件(如 summary.md,或事后塞入的文件)
  纳入链尾 ``extra`` 段(相对路径 + sha256,按路径排序);
- HMAC 载荷 = HMAC-SHA256(密钥, json.dumps({"files": {...}, "extra": [...]},
  sort_keys=True, ensure_ascii=False));manifest 的 report 等文本字段不在
  链内(覆盖的是证据文件内容与目录内容清单)。

密钥解析顺序(先到先得,来源记日志但绝不记录密钥本身):

1. 环境变量(默认 ``NETSENTINEL_SIGN_KEY``,可经 ``env_name`` 指定别的
   名字,例如上层从配置的 ``sign_key_env`` 字段传入);
2. ``<data_dir>/.signkey`` 文件首行(自动生成的内容为 32 字节随机数的
   十六进制,即 64 个 hex 字符);
3. 均无 → 自动生成并写入该文件,同时收紧文件权限(POSIX ``chmod 600``;
   Windows 尝试 ``icacls``,失败仅提示,绝不抛出);
4. 生成也失败(目录不可写等)→ :meth:`verify` 返回 ``(False, 中文提示)``,
   :meth:`sign_manifest` 抛 ValueError。

只用标准库,绝不联网;所有用户可见文案均为中文。

V5 升级确认与增强:签名链本就**按 path 排序后流式逐文件哈希**(1 MiB 分块,
不整读大文件),sign 与 verify 复用同一 :func:`_collect_chain_parts`,口径
完全一致——本次仅把循环内重复的 ``bundle_dir.resolve()`` 提出循环
(O(2n) → O(n+1) 次路径解析),并接 telemetry:timer ``sign.manifest`` /
``sign.verify``,counter ``sign.verify_fail``(**仅**校验不通过时计数)。

A192 第三方可信证据升级(策略模式,**默认行为逐字节兼容**):

- 构造参数 ``algo`` 策略分发:``"hmac-sha256"``(默认,载荷与 manifest
  写入口径与旧版完全一致)| ``"ed25519"``(:mod:`netsentinel.security.ed25519`
  纯 Python RFC 8032 非对称签名,私钥 seed 经 ``ed25519_seed`` 注入 bytes,
  验签方只需公钥,无需共享密钥);
- Ed25519 载荷在 {"files", "extra"} 之上**追加 ``timestamp_proof`` 字段**
  (:mod:`netsentinel.security.timestamp` 的 RFC3161-lite 时间证明),
  签名同时覆盖时间证明——签署时间被密码学绑定,本地时钟回拨无法事后抵赖;
  HMAC 路径的**签名载荷保持旧口径不变**(逐字节兼容),时间证明仅作为
  manifest 签名块内的附加元数据字段(未入 HMAC 链);
- 签名块新增字段:``timestamp_proof``(两算法皆有)、``public_key``
  (仅 Ed25519,hex 公钥,便于验签方脱离签发方独立校验);
- :meth:`verify` 按 manifest ``signature.algo`` 双算法分发:**无 ``algo``
  字段的旧格式包走原 HMAC 路径逐字节兼容**;``"HMAC-SHA256"`` → 原路径;
  ``"ed25519"`` → 非对称路径(用签名块内的 ``public_key`` 验签,
  完全不需要 HMAC 密钥)。

用法::

    signer = BundleSigner("data")            # HMAC(默认,与旧版完全兼容)
    hex_value = signer.sign_manifest(bundle.dir_path)
    ok, msg = signer.verify(bundle.dir_path)

    signer = BundleSigner("data", algo="ed25519", ed25519_seed=seed_bytes)
    hex_value = signer.sign_manifest(bundle.dir_path)   # 非对称 + 时间证明
    ok, msg = signer.verify(bundle.dir_path)            # 只需公钥即可复核
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import pathlib
import secrets
import subprocess

from netsentinel import telemetry
from netsentinel.contracts import now_iso
from netsentinel.security import ed25519, timestamp

__all__ = [
    "BundleSigner",
    "DEFAULT_ENV_NAME",
    "MANIFEST_NAME",
    "KEY_FILE_NAME",
    "ALGO",
    "ALGO_HMAC",
    "ALGO_ED25519",
]

logger = logging.getLogger(__name__)

#: 环境变量名:签名密钥的第一优先级来源
DEFAULT_ENV_NAME = "NETSENTINEL_SIGN_KEY"

#: manifest 文件名(与 netsentinel.evidence.packager.MANIFEST_NAME 一致)
MANIFEST_NAME = "manifest.json"

#: 签名密钥文件名(位于 data_dir 下,内容为 hex(32B) + 换行)
KEY_FILE_NAME = ".signkey"

#: 签名算法标识(写入 manifest["signature"]["algo"];HMAC 旧格式值,兼容保留)
ALGO = "HMAC-SHA256"

#: 构造函数 algo 参数:HMAC 策略(默认,大小写不敏感归一化)
ALGO_HMAC = "hmac-sha256"

#: 构造函数 algo 参数 / manifest algo 字段:Ed25519 策略(RFC 8032)
ALGO_ED25519 = "ed25519"

#: 缺失文件在签名 files 段中的标记前缀
MISSING_PREFIX = "MISSING:"

#: 流式哈希分块字节数(1 MiB)
HASH_CHUNK_BYTES = 1 << 20


# ---------------------------------------------------------------------------
# 内部工具:哈希 / 路径规整 / payload 构造
# ---------------------------------------------------------------------------


def _sha256_of(path: pathlib.Path) -> str:
    """流式计算文件 sha256(十六进制摘要,1 MiB 分块,不整读大文件)。"""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _norm_rel(path: str) -> str | None:
    """把 manifest 内的相对路径规整为 posix 形态;绝对路径 / 含 ``..`` / 空 → None。"""
    cleaned = path.strip().replace("\\", "/")
    if not cleaned or cleaned.startswith("/"):
        return None
    pure = pathlib.PurePosixPath(cleaned)
    if pure.is_absolute() or ".." in pure.parts:
        return None
    rel = pure.as_posix()
    return rel or None


def _safe_target(base: pathlib.Path, rel: str) -> pathlib.Path | None:
    """把规整后的相对路径解析为 bundle 内的实际文件;越界(含符号链接逃逸)→ None。

    ``base`` 为调用方**预先 resolve 一次**的 bundle 根目录(V5:避免在
    逐文件循环里重复解析同一目录)。
    """
    try:
        target = (base / rel).resolve()
        target.relative_to(base)  # 不在 bundle 内 → ValueError
    except (ValueError, OSError):
        return None
    return target


def _collect_chain_parts(
    bundle_dir: pathlib.Path, manifest: dict
) -> tuple[dict[str, str], list[dict[str, str]], list[str], int]:
    """遍历证据包,产出签名链的三个组成部分。

    返回 ``(files 段, extra 段, 缺失文件原始路径列表, 实际哈希到的清单文件数)``:

    - ``files``:{规整路径 → sha256 或 ``"MISSING:<原始路径>"``},按 path 排序遍历,
      值以 bundle_dir 内实际文件为准;
    - ``extra``:目录内 manifest 清单之外的所有文件(manifest.json 自身除外),
      ``[{"path": 相对路径, "sha256": ...}]`` 按路径排序;
    - 同一实现供 sign / verify 共用,保证两次计算口径完全一致。

    V5:bundle 根目录只 resolve 一次并复用(旧实现每清单文件 resolve 两次)。
    """
    files_field: dict[str, str] = {}
    missing: list[str] = []
    hashed_n = 0

    entries = manifest.get("files")
    if entries is None:
        entries = []
    elif not isinstance(entries, list):
        logger.warning("manifest 的 files 字段不是列表,按空清单处理(签名比对将不通过)")
        entries = []

    raw_paths: list[str] = []
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            raw_paths.append(entry["path"])
        else:
            logger.warning("manifest files 含无法识别的条目,已跳过:%r", entry)

    try:
        base = bundle_dir.resolve()
    except OSError as exc:  # 极罕见(如符号链接环路):清单文件将全部记为缺失
        logger.warning("解析证据包目录 %s 失败:%s", bundle_dir, exc)
        base = None

    listed: set[str] = set()
    for raw in sorted(raw_paths):
        rel = _norm_rel(raw)
        key = rel if rel is not None else raw
        target = _safe_target(base, key) if rel is not None and base is not None else None
        if target is not None and target.is_file():
            files_field[key] = _sha256_of(target)
            listed.add(key)
            hashed_n += 1
        else:
            files_field[key] = f"{MISSING_PREFIX}{raw}"
            missing.append(raw)
            logger.warning("清单文件缺失或路径越界,签名记 %s%s", MISSING_PREFIX, raw)

    extra: list[dict[str, str]] = []
    try:
        candidates = sorted(bundle_dir.rglob("*"))
    except OSError as exc:  # 目录不可读:extra 段为空并告警,签名比对将不通过
        logger.warning("扫描证据包目录 %s 失败:%s", bundle_dir, exc)
        candidates = []
    for item in candidates:
        if not item.is_file():
            continue
        rel = item.relative_to(bundle_dir).as_posix()
        if rel == MANIFEST_NAME or rel in listed:
            continue
        extra.append({"path": rel, "sha256": _sha256_of(item)})
    extra.sort(key=lambda e: e["path"])
    if extra:
        logger.info(
            "manifest 清单之外纳入签名链尾的附加文件 %d 个:%s",
            len(extra),
            ", ".join(e["path"] for e in extra),
        )
    return files_field, extra, missing, hashed_n


def _payload_bytes(files_field: dict[str, str], extra: list[dict[str, str]]) -> bytes:
    """按契约口径序列化签名载荷(与 A54 规范逐字一致,sign/verify 共用)。"""
    return json.dumps(
        {"files": files_field, "extra": extra},
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")


def _payload_bytes_signed(
    files_field: dict[str, str],
    extra: list[dict[str, str]],
    timestamp_proof: dict,
) -> bytes:
    """A192 Ed25519 载荷:在 {files, extra} 之上追加时间证明字段。

    与 :func:`_payload_bytes` 同一序列化口径(sort_keys + ensure_ascii=False +
    UTF-8);verify 侧用 manifest 签名块内**存储的** timestamp_proof 原样
    复算,保证口径一致。HMAC 路径不走本函数(载荷保持旧口径逐字节兼容)。
    """
    return json.dumps(
        {
            "files": files_field,
            "extra": extra,
            "timestamp_proof": timestamp_proof,
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")


def _hmac_hex(key: str, payload: bytes) -> str:
    """密钥字符串按 UTF-8 字节作为 HMAC 密钥,返回 HMAC-SHA256 十六进制值。"""
    return hmac.new(key.encode("utf-8"), payload, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# 内部工具:密钥文件权限收紧
# ---------------------------------------------------------------------------


def _tighten_key_file_permissions(path: pathlib.Path) -> None:
    """收紧密钥文件权限;任何失败只记日志提示,绝不抛出。

    - POSIX:``os.chmod(path, 0o600)``;
    - Windows(``os.name == 'nt'``):尝试 ``icacls`` 移除继承并仅授权当前用户,
      失败仅提示用户手动处理。
    """
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME") or os.environ.get("USERDOMAIN") or ""
            grant = f"{user}:F" if user else f"{path.owner()}:F"
            # 按字节捕获(icacls 在中文 Windows 输出 GBK,避免按 UTF-8 解码崩溃;
            # 本处只关心退出码,输出内容不使用)
            result = subprocess.run(  # noqa: S603 本地权限命令,参数受控
                ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
                capture_output=True,
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
                logger.info("已通过 icacls 收紧签名密钥文件权限:%s", path)
        else:
            os.chmod(path, 0o600)
            logger.info("已将签名密钥文件权限收紧为仅属主可读写(chmod 600):%s", path)
    except Exception as exc:  # noqa: BLE001 权限收紧是建议性动作,失败不阻断
        logger.warning(
            "无法自动收紧签名密钥文件 %s 的权限(%s),建议手动限制为仅当前用户可读。",
            path,
            exc,
        )


# ---------------------------------------------------------------------------
# BundleSigner
# ---------------------------------------------------------------------------


class BundleSigner:
    """证据包签名器(策略模式):sign_manifest 产签、verify 验签、rotate_key 换钥。

    用法::

        signer = BundleSigner("data")            # HMAC(默认,与旧版完全兼容)
        hex_value = signer.sign_manifest(bundle.dir_path)   # 签名写回 manifest
        ok, msg = signer.verify(bundle.dir_path)            # (bool, 中文结论)

        # A192 Ed25519 非对称 + 时间证明:
        signer = BundleSigner(
            "data",
            algo="ed25519",
            ed25519_seed=seed_bytes,           # 32 字节私钥 seed,构造注入
            timestamp_prover=prover,           # 可注入;缺省用 data_dir 下计数器
        )

    密钥每次调用实时解析(不缓存),环境变量增删与 :meth:`rotate_key` 立即生效;
    Ed25519 seed 在构造期注入并固化(确定性密钥对,验签只需签名块内公钥)。
    """

    def __init__(
        self,
        data_dir: str = "data",
        *,
        env_name: str = DEFAULT_ENV_NAME,
        algo: str = ALGO_HMAC,
        ed25519_seed: bytes | None = None,
        timestamp_prover: timestamp.TimestampProver | None = None,
    ) -> None:
        """初始化签名器并选择算法策略。

        - ``algo``:``"hmac-sha256"``(默认,完全向后兼容)| ``"ed25519"``,
          大小写不敏感;其他值 → ``ValueError``;
        - ``ed25519_seed``:Ed25519 私钥 seed(32 字节 bytes,沿用密钥注入
          风格经构造函数传入);提供时立即校验长度,非法 → ``ValueError``;
          HMAC 策略下忽略该参数;
        - ``timestamp_prover``:时间证明签发器,可注入(测试 / 自定义 TSA
          配置);缺省惰性构造,计数器落 ``<data_dir>/.tscounter.json``,
          默认 ``tsa_url=None`` 全离线。
        """
        normalized = str(algo).strip().lower()
        if normalized not in (ALGO_HMAC, ALGO_ED25519):
            raise ValueError(
                f"不支持的签名算法:{algo!r}(仅支持 {ALGO_HMAC!r} / {ALGO_ED25519!r})"
            )
        seed_bytes: bytes | None = None
        if ed25519_seed is not None:
            if not isinstance(ed25519_seed, (bytes, bytearray, memoryview)):
                raise ValueError(
                    "Ed25519 私钥 seed 必须是 bytes 等字节类型,"
                    f"收到 {type(ed25519_seed).__name__}"
                )
            seed_bytes = bytes(ed25519_seed)
            if len(seed_bytes) != ed25519.SEED_BYTES:
                raise ValueError(
                    "Ed25519 私钥 seed 必须是 "
                    f"{ed25519.SEED_BYTES} 字节,收到 {len(seed_bytes)} 字节"
                )
        self.algo = normalized
        self.ed25519_seed = seed_bytes
        self.data_dir = str(data_dir)
        self.env_name = str(env_name).strip() or DEFAULT_ENV_NAME
        self._timestamp_prover = timestamp_prover

    @property
    def timestamp_prover(self) -> timestamp.TimestampProver:
        """时间证明签发器(惰性构造):计数器持久化在 data_dir 下,默认全离线。"""
        if self._timestamp_prover is None:
            self._timestamp_prover = timestamp.TimestampProver(
                counter_path=pathlib.Path(self.data_dir)
                / timestamp.COUNTER_FILE_NAME,
                tsa_url=None,  # 默认禁网:TSA 只有经显式配置才外呼
            )
        return self._timestamp_prover

    @property
    def key_path(self) -> pathlib.Path:
        """签名密钥文件路径 = ``<data_dir>/.signkey``。"""
        return pathlib.Path(self.data_dir) / KEY_FILE_NAME

    # -- 密钥解析 -----------------------------------------------------------

    def _read_key_file(self) -> str:
        """读取密钥文件首行(strip);文件不存在 / 不可读 / 首行为空 → ``""``。"""
        if not self.key_path.is_file():
            return ""
        try:
            lines = self.key_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("读取签名密钥文件 %s 失败:%s", self.key_path, exc)
            return ""
        first = lines[0].strip() if lines else ""
        if not first:
            logger.info("签名密钥文件 %s 首行为空,视为未配置", self.key_path)
        return first

    def _generate_key_file(self) -> str:
        """自动生成 32 字节随机密钥(hex 编码)写入密钥文件,返回密钥字符串。

        已有非空密钥文件时不覆盖(重新读回复用);目录不可写等失败 → 返回 ``""``。
        """
        key = secrets.token_hex(32)
        try:
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:  # 并发竞争或首行为空的既有文件:先尝试复用
                existing = self._read_key_file()
                if existing:
                    return existing
                fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(key + "\n")
        except OSError as exc:
            logger.warning("自动生成签名密钥失败(%s):%s", self.key_path, exc)
            return ""
        logger.info(
            "已自动生成签名密钥:%s(32 字节随机数,hex 编码;请妥善备份)",
            self.key_path,
        )
        _tighten_key_file_permissions(self.key_path)
        return key

    def _resolve_key(self) -> str:
        """解析签名密钥:环境变量 → 密钥文件 → 自动生成;失败 → ``""``。"""
        env_key = (os.environ.get(self.env_name) or "").strip()
        if env_key:
            logger.info("签名密钥来源:环境变量 %s", self.env_name)
            return env_key
        from_file = self._read_key_file()
        if from_file:
            logger.info("签名密钥来源:文件 %s", self.key_path)
            return from_file
        return self._generate_key_file()

    # -- 签名 ----------------------------------------------------------------

    @telemetry.timed("sign.manifest")
    def sign_manifest(self, bundle_dir: str) -> str:
        """按当前策略(HMAC 或 Ed25519)对证据包签名并写回 manifest,返回十六进制签名值。

        - 读 ``bundle_dir/manifest.json``,对 ``files`` 清单(按 path 排序)逐项
          **流式**计算实际文件的 sha256(1 MiB 分块);缺失文件记
          ``"MISSING:<path>"``,不拒绝签名但记录告警(由 :meth:`verify` 判不通过);
        - manifest 清单之外的目录内容(如 summary.md)纳入链尾 ``extra`` 段;
        - HMAC 策略(默认):签名载荷与写入口径与旧版**逐字节一致**;
          签名块额外附带 ``timestamp_proof`` 元数据(不入 HMAC 链);
        - Ed25519 策略:载荷 = {files, extra, **timestamp_proof**}(时间证明被
          签名覆盖);签名块写入 ``algo="ed25519"``、``public_key``(hex)与
          ``timestamp_proof``;
        - HMAC 无可用密钥 → ``ValueError``;Ed25519 未注入 seed → ``ValueError``;
          manifest 缺失 → ``FileNotFoundError``;解析失败 → ``ValueError``;
        - 可观测性(V5):整体耗时记 telemetry ``sign.manifest``。
        """
        bundle_path = pathlib.Path(bundle_dir)
        manifest_path = bundle_path / MANIFEST_NAME
        if not manifest_path.is_file():
            raise FileNotFoundError(f"manifest.json 不存在:{manifest_path}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"manifest.json 读取失败:{exc}") from exc
        if not isinstance(manifest, dict):
            raise ValueError(f"manifest.json 不是 JSON 对象:{manifest_path}")

        if self.algo == ALGO_ED25519:
            return self._sign_ed25519(bundle_path, manifest, manifest_path)
        return self._sign_hmac(bundle_path, manifest, manifest_path)

    def _sign_hmac(
        self, bundle_path: pathlib.Path, manifest: dict, manifest_path: pathlib.Path
    ) -> str:
        """HMAC-SHA256 策略:签名载荷与签名块 algo 口径与旧版逐字节一致。"""
        key = self._resolve_key()
        if not key:
            raise ValueError(
                "无法获取签名密钥:环境变量 "
                f"{self.env_name} 未设置,且密钥文件自动生成失败"
                f"(请检查 {self.key_path} 所在目录是否可写)"
            )

        files_field, extra, missing, hashed_n = _collect_chain_parts(bundle_path, manifest)
        value = _hmac_hex(key, _payload_bytes(files_field, extra))
        # A192:时间证明仅作为签名块附加元数据(不进 HMAC 链,载荷保持旧口径)
        proof = self.timestamp_prover.issue(_payload_bytes(files_field, extra))

        manifest["signature"] = {
            "algo": ALGO,
            "value": value,
            "signed_at": now_iso(),
            "files_hashed": hashed_n,
            "timestamp_proof": proof.as_dict(),
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(
            "证据包已签名:目录=%s,清单文件哈希 %d 个,缺失 %d 个,附加文件 %d 个,"
            "签名值=%s...",
            bundle_path,
            hashed_n,
            len(missing),
            len(extra),
            value[:12],
        )
        if missing:
            logger.warning(
                "签名时发现缺失文件(已记 %s前缀,verify 将判不通过):%s",
                MISSING_PREFIX,
                ", ".join(missing),
            )
        return value

    def _sign_ed25519(
        self, bundle_path: pathlib.Path, manifest: dict, manifest_path: pathlib.Path
    ) -> str:
        """Ed25519 策略:非对称签名 + 签名载荷内嵌 RFC3161-lite 时间证明。"""
        seed = self.ed25519_seed
        if seed is None:
            raise ValueError(
                "Ed25519 签名需要私钥 seed:请在构造函数以 ed25519_seed=bytes"
                "(32 字节)注入"
            )

        files_field, extra, missing, hashed_n = _collect_chain_parts(bundle_path, manifest)
        content_bytes = _payload_bytes(files_field, extra)
        # 时间证明绑定 {files, extra} 内容摘要,再被签名覆盖 → 双向绑定
        proof = self.timestamp_prover.issue(content_bytes)
        value = ed25519.sign(seed, _payload_bytes_signed(files_field, extra, proof.as_dict())).hex()

        manifest["signature"] = {
            "algo": ALGO_ED25519,
            "value": value,
            "signed_at": now_iso(),
            "files_hashed": hashed_n,
            "public_key": ed25519.public_key(seed).hex(),
            "timestamp_proof": proof.as_dict(),
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(
            "证据包已签名(Ed25519):目录=%s,清单文件哈希 %d 个,缺失 %d 个,"
            "附加文件 %d 个,签名值=%s...",
            bundle_path,
            hashed_n,
            len(missing),
            len(extra),
            value[:12],
        )
        if missing:
            logger.warning(
                "签名时发现缺失文件(已记 %s前缀,verify 将判不通过):%s",
                MISSING_PREFIX,
                ", ".join(missing),
            )
        return value

    # -- 校验 ----------------------------------------------------------------

    def verify(self, bundle_dir: str) -> tuple[bool, str]:
        """重算并比对证据包签名(按签名块 ``algo`` 双算法分发),返回 ``(bool, 中文结论)``。

        分发规则(A192):

        - 签名块无 ``algo`` 字段(最旧格式包)或 ``"HMAC-SHA256"`` → 原 HMAC
          路径,**逐字节兼容**旧包(含密钥检查顺序与全部中文提示);
        - ``"ed25519"`` → 非对称路径:用签名块内的 ``public_key`` 验签,
          **不需要 HMAC 密钥**(验证方可独立复核);
        - 其他取值 → ``(False, 算法不受支持)``。

        - HMAC 路径:无可用密钥 → ``(False, 密钥提示)``;manifest 缺失 /
          无法解析 → ``(False, 中文原因)``;无 ``signature`` 字段 →
          ``(False, "未签名")``;签名字段损坏 → ``(False, 损坏提示)``;
        - 重算(剥离 signature 键后按签名同口径)与存储值比对:

          * 有 MISSING → ``(False, "缺失文件:...(签名比对一致/不匹配...)")``;
          * 比对不一致 → ``(False, "签名不匹配:内容被篡改或密钥/公钥不符")``;
          * 一致且无缺失 → ``(True, "校验通过:N 个文件,完整性完好")``。

        可观测性(V5):耗时记 telemetry ``sign.verify``;校验**不通过**时计数
        ``sign.verify_fail``(通过时绝不计数)。
        """
        with telemetry.timer("sign.verify"):
            ok, msg = self._verify_impl(bundle_dir)
        if not ok:
            telemetry.inc("sign.verify_fail")
        return ok, msg

    def _verify_impl(self, bundle_dir: str) -> tuple[bool, str]:
        """:meth:`verify` 的实现主体:读 manifest → 按 algo 分发到对应策略。"""
        bundle_path = pathlib.Path(bundle_dir)
        manifest_path = bundle_path / MANIFEST_NAME
        if not manifest_path.is_file():
            return False, f"manifest.json 不存在:{manifest_path}"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"manifest.json 读取失败:{exc}"
        if not isinstance(manifest, dict):
            return False, f"manifest.json 不是 JSON 对象:{manifest_path}"

        signature = manifest.get("signature")
        raw_algo = signature.get("algo") if isinstance(signature, dict) else None
        algo = str(raw_algo).strip().lower() if raw_algo is not None else None

        if algo is None or algo == ALGO_HMAC:
            # 无 algo 字段的最旧格式包 → HMAC 原路径(逐字节兼容)
            return self._verify_hmac(bundle_path, manifest, signature)
        if algo == ALGO_ED25519:
            return self._verify_ed25519(bundle_path, manifest, signature)
        return False, f"签名算法不受支持:{raw_algo!r}(仅支持 {ALGO} / {ALGO_ED25519})"

    def _verify_hmac(
        self, bundle_path: pathlib.Path, manifest: dict, signature: object
    ) -> tuple[bool, str]:
        """HMAC-SHA256 验签(原路径):检查顺序与中文提示与旧版一致。"""
        key = self._resolve_key()
        if not key:
            return False, (
                "未找到签名密钥,无法校验:请设置环境变量 "
                f"{self.env_name},或确保数据目录可写以便自动生成 {KEY_FILE_NAME}"
            )
        if signature is None:
            return False, "未签名"
        if not isinstance(signature, dict):
            return False, "签名字段损坏:signature 不是 JSON 对象"
        stored = signature.get("value")
        if not isinstance(stored, str) or not stored.strip():
            return False, "签名字段损坏:缺少签名值 value"

        # 重算时剥离 signature 键,与签名时口径一致
        stripped = {k: v for k, v in manifest.items() if k != "signature"}
        files_field, extra, missing, hashed_n = _collect_chain_parts(bundle_path, stripped)
        recomputed = _hmac_hex(key, _payload_bytes(files_field, extra))
        match = hmac.compare_digest(recomputed, stored)

        if missing:
            cmp_text = "一致" if match else "不匹配:内容被篡改或密钥不符"
            return False, f"缺失文件:{'、'.join(missing)}(签名比对{cmp_text})"
        if not match:
            return False, "签名不匹配:内容被篡改或密钥不符"
        return True, f"校验通过:{hashed_n} 个文件,完整性完好"

    def _verify_ed25519(
        self, bundle_path: pathlib.Path, manifest: dict, signature: dict
    ) -> tuple[bool, str]:
        """Ed25519 验签:用签名块内公钥复核,不需要 HMAC 密钥。

        载荷用签名块内**存储的** ``timestamp_proof`` 原样复算(时间证明在
        签名链内,事后改动证明或内容都会验签失败)。
        """
        stored = signature.get("value")
        if not isinstance(stored, str) or not stored.strip():
            return False, "签名字段损坏:缺少签名值 value"
        pk_hex = signature.get("public_key")
        if not isinstance(pk_hex, str) or not pk_hex.strip():
            return False, "签名字段损坏:缺少 Ed25519 公钥 public_key"
        proof = signature.get("timestamp_proof")
        if not isinstance(proof, dict):
            return False, "签名字段损坏:缺少时间证明 timestamp_proof"

        stripped = {k: v for k, v in manifest.items() if k != "signature"}
        files_field, extra, missing, hashed_n = _collect_chain_parts(bundle_path, stripped)
        payload = _payload_bytes_signed(files_field, extra, proof)

        try:
            pk_bytes = bytes.fromhex(pk_hex.strip())
            sig_bytes = bytes.fromhex(stored.strip())
            match = ed25519.verify(pk_bytes, payload, sig_bytes)
        except ValueError as exc:  # 坏格式(hex 长度错 / 字节数错)→ 损坏提示
            return False, f"签名字段损坏:Ed25519 验签输入非法({exc})"

        if missing:
            cmp_text = "一致" if match else "不匹配:内容被篡改或公钥不符"
            return False, f"缺失文件:{'、'.join(missing)}(签名比对{cmp_text})"
        if not match:
            return False, "签名不匹配:内容被篡改或公钥不符"
        return True, (
            f"校验通过:{hashed_n} 个文件,完整性完好"
            f"(algo=ed25519,签署时间 {proof.get('utc_iso', '未知')})"
        )

    # -- 密钥轮换 ------------------------------------------------------------

    def rotate_key(self) -> str:
        """轮换签名密钥:生成新密钥写入密钥文件,返回新密钥(hex)。

        注意:轮换后**旧密钥签发的所有签名立即失效**(verify 将报"签名不匹配"),
        既有证据包需用新密钥重新签署;若当前生效密钥来自环境变量
        (``env_name``),则仅轮换密钥文件,清除该环境变量前实际密钥不变。
        本操作只作用于 **HMAC 密钥**;Ed25519 密钥对由 ``ed25519_seed`` 派生,
        轮换 = 换一个新 seed 重新构造签名器。
        """
        if (os.environ.get(self.env_name) or "").strip():
            logger.warning(
                "当前签名密钥来自环境变量 %s:本次轮换只更新密钥文件 %s,"
                "清除该环境变量前实际生效密钥不会变化",
                self.env_name,
                self.key_path,
            )
        new_key = secrets.token_hex(32)
        try:
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(new_key + "\n")
        except OSError as exc:
            raise RuntimeError(f"签名密钥轮换失败:{exc}") from exc
        _tighten_key_file_permissions(self.key_path)
        logger.warning(
            "签名密钥已轮换(%s):旧密钥签发的所有签名将失效,既有证据包需重新签署",
            self.key_path,
        )
        return new_key
