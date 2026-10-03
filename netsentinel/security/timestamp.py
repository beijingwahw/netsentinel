"""RFC3161-lite 可信时间证明(A192 第三方可信证据)。

背景:证据包原以本地时钟 ``now_iso()`` 记录签署时间,而本地时钟**可回拨**,
事后难以自证"签名时刻"。本模块产出结构化时间证明 ``TimeProof``,把

- **UTC 时间戳**(供人读的绝对时间)与
- **本地单调计数器**(跨进程持久化、只增不减,回拨时钟无法令其倒退)

绑定为一个可 JSON 序列化的 proof 对象;配置 ``tsa_url`` 时再可选叠加
**RFC3161 时间戳权威(TSA)令牌**(第三方对"内容摘要 + 权威时间"的签名),
对标 RFC3161 与 sigstore / DSSE 的第三方可信证据实践。

红线约束(默认全离线):

- ``tsa_url`` **默认 None**:完全不发起任何网络请求;只有显式配置才经
  :mod:`urllib`(标准库)外呼,超时短(默认 5 秒),任何异常**安全降级**
  为本地证明并在 ``reason`` 字段记录降级原因,绝不因 TSA 故障阻断签名;
- 单调计数器持久化为 JSON 文件(路径可注入,默认不出网、不落家目录);
  写入为**原子写**(同目录临时文件 + :func:`os.replace`,断电/崩溃不留
  半截文件);文件损坏(坏 JSON / 结构不对 / 负数)→ **自愈重置**为 1
  并记告警——计数器只保证"正常路径下单调",损坏重置属可观测的降级。

A235 收口(两项,补 A192 遗留风险项):

- :func:`verify_tsa_token`:TSA 令牌**离线 DER 结构校验器**(纯 stdlib
  解析 ASN.1 最小子集,定位 TimeStampResp → TSTInfo 的 genTime 与
  messageImprint)——补上"本地不解析 CMS"中可离线核验的一半;
  **诚实边界:不验证签名链**(不验签、不校验证书),司法等严肃场景
  的密码学核验仍需 OpenSSL 等独立工具;
- 计数器 HMAC 链 v2:构造参数 ``chain_key`` 注入链密钥(与签名密钥
  注入体系同风格)后,计数器文件升级为链式结构
  ``{version, counter, prev_hmac, chain_hmac}``
  (``chain_hmac = HMAC-SHA256(key, f"{counter}|{prev_hmac}")``);
  读取时链校验失败**不再静默重置**,而是中文告警 + 计数器从 1 重启 +
  ``tamper_detected`` 标志写入 ``TimeProof.reason``(证据链上可见篡改
  痕迹);缺省无 key 时保持 v1 明文格式完全兼容(逐字节同旧行为),
  v1 旧文件在有 key 实例下读取不破坏并自动升级 v2。链只能检出对 v2
  文件字段的**就地篡改**;持有文件写权限的攻击者整卷重写/回退为 v1
  格式无法仅靠本链检出,可信锚点仍以 TSA 令牌(若有)为准。

只用标准库;所有用户可见文案均为中文。

用法::

    from netsentinel.security.timestamp import TimestampProver

    prover = TimestampProver(counter_path="data/.tscounter.json")  # 全离线
    proof = prover.issue()            # TimeProof(source="local")
    proof.as_dict()                   # 可直接 json.dumps 进 manifest

    prover = TimestampProver(counter_path=..., tsa_url="https://tsa.example/ts")  # 显式开 TSA
    proof = prover.issue()            # TSA 可达 → source="tsa" + token_b64
    # TSA 超时/报错 → source="local",reason 记录原因
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import logging
import os
import pathlib
import re
import tempfile
from dataclasses import dataclass

from netsentinel import telemetry

__all__ = [
    "TimeProof",
    "TimestampProver",
    "COUNTER_FILE_NAME",
    "DEFAULT_TSA_TIMEOUT_S",
    "verify_tsa_token",
]

logger = logging.getLogger(__name__)

#: 单调计数器默认文件名(位于 data_dir 下,由 BundleSigner 组合出完整路径)
COUNTER_FILE_NAME = ".tscounter.json"

#: TSA 请求默认超时秒数(短超时:TSA 只是增强项,不值得长等)
DEFAULT_TSA_TIMEOUT_S = 5.0

#: 本地证明来源标识
SOURCE_LOCAL = "local"

#: TSA 证明来源标识
SOURCE_TSA = "tsa"

#: 计数器 JSON 的当前模式版本(结构升级时迁移依据)
_COUNTER_VERSION = 1

#: 计数器 HMAC 链(v2)结构版本:{version, counter, prev_hmac, chain_hmac}
_COUNTER_VERSION_V2 = 2

#: v2 链的创世前值:首环(counter=1)之前没有上一环,取 64 个 "0" 定值,
#: 保证同 key 下链条可独立复算(确定性,不引入随机数)
_CHAIN_GENESIS_PREV = "0" * 64

#: v2 链字段(prev_hmac / chain_hmac)的形态:64 位十六进制(HMAC-SHA256)
_HEX64 = re.compile(r"[0-9a-fA-F]{64}")


@dataclass
class TimeProof:
    """结构化时间证明(全部字段可 JSON 序列化)。

    字段口径:

    - ``utc_iso``:UTC ISO8601 时间戳(秒级,带 +00:00 后缀);
    - ``mono_counter``:签发时的本地单调计数器值(跨进程持久化、只增不减);
    - ``source``:证明来源,``"local"``(本地)或 ``"tsa"``(第三方权威);
    - ``tsa_url``:配置的 TSA 地址(未配置时为 None,一并留痕);
    - ``token_b64``:RFC3161 响应令牌(base64 DER;仅 TSA 成功时非 None;
      lite 口径:令牌作为不透明证据原样保存,不在本地解析 CMS);
    - ``message_sha256``:请求 TSA 时对消息摘要再计入证明的字段(未提供
      消息时为 None;绑定"该证明对应哪份内容");
    - ``reason``:降级与告警原因(均正常时为 None)。A235 起:计数器
      HMAC 链校验失败时此字段携带 ``tamper_detected`` 标志(证据链上
      可见篡改痕迹);TSA 降级原因与之共存时以 ";" 连接。
    """

    utc_iso: str
    mono_counter: int
    source: str = SOURCE_LOCAL
    tsa_url: str | None = None
    token_b64: str | None = None
    message_sha256: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict:
        """返回可 ``json.dumps`` 的字典(键序即字段序,稳定可复算)。"""
        return {
            "utc_iso": self.utc_iso,
            "mono_counter": self.mono_counter,
            "source": self.source,
            "tsa_url": self.tsa_url,
            "token_b64": self.token_b64,
            "message_sha256": self.message_sha256,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# RFC3161-lite:极小 DER 编码器(TimeStampReq 专用,只写不读)
# ---------------------------------------------------------------------------

#: SHA-256 的 OID(2.16.840.1.101.3.4.2.1),DER 内容八位组
_SHA256_OID_DER = bytes.fromhex("0609608648016503040201")


def _der_len(n: int) -> bytes:
    """DER 长度八位组(短形式 / 长形式)。"""
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _der_tlv(tag: int, content: bytes) -> bytes:
    """DER TLV 编码(tag + 长度 + 内容)。"""
    return bytes([tag]) + _der_len(len(content)) + content


def _build_timestamp_request(message: bytes) -> bytes:
    """构造 RFC3161 TimeStampReq(DER):version=1 + SHA-256 messageImprint。

    结构(省略可选项,合法的完整最小请求)::

        SEQUENCE {
            INTEGER 1,
            SEQUENCE {                       -- messageImprint
                SEQUENCE {                   -- hashAlgorithm(AlgorithmIdentifier)
                    OID 2.16.840.1.101.3.4.2.1   -- SHA-256,参数省略
                },
                OCTET STRING <sha256(message)>    -- hashedMessage
            }
        }
    """
    digest = hashlib.sha256(message).digest()
    algorithm = _der_tlv(0x30, _SHA256_OID_DER)  # SEQUENCE(OID)
    imprint = _der_tlv(0x30, algorithm + _der_tlv(0x04, digest))
    return _der_tlv(0x30, _der_tlv(0x02, b"\x01") + imprint)


# ---------------------------------------------------------------------------
# A235:极小 DER 解码器(TimeStampResp 结构校验专用,只读不写)
# ---------------------------------------------------------------------------
#: ASN.1 标签字节(本模块**解释**所需的最小子集;其余标签只按 TLV
#: 结构解析后跳过,不解释内容)
_TAG_INTEGER = 0x02
_TAG_OCTET_STRING = 0x04
_TAG_OID = 0x06
_TAG_SEQUENCE = 0x30
_TAG_UTCTIME = 0x17
_TAG_GENERALIZEDTIME = 0x18
_TAG_CTX_0 = 0xA0  # [0] EXPLICIT(ContentInfo content / encapContentInfo eContent)

#: id-ct-TSTInfo(RFC 3161):TimeStampToken 与 TSTInfo 的 contentType OID
_OID_ID_CT_TST_INFO = "1.2.840.113549.1.9.16.1.4"

#: messageImprint 常见摘要算法 OID → 名称(未识别不判失败,只提示人工核验)
_DIGEST_OID_NAMES = {
    "1.3.14.3.2.26": "SHA-1",
    "2.16.840.1.101.3.4.2.4": "SHA-224",
    "2.16.840.1.101.3.4.2.1": "SHA-256",
    "2.16.840.1.101.3.4.2.2": "SHA-384",
    "2.16.840.1.101.3.4.2.3": "SHA-512",
}


class _DerStructError(ValueError):
    """DER 结构不符合预期(中文消息直接进入校验结果 reasons)。"""


def _read_tlv(data: bytes, offset: int) -> tuple[int, bytes, int]:
    """从 ``offset`` 解析一个 TLV,返回 ``(tag, content, 结束偏移)``。

    严格 DER 口径:不定长(``0x80``)、长形式前导零(非最短编码)、
    高位标签编号、内容越界(截断)一律抛 :class:`_DerStructError`。
    """
    if offset >= len(data):
        raise _DerStructError("DER 数据在标签字节处截断")
    tag = data[offset]
    if tag & 0x1F == 0x1F:
        raise _DerStructError(f"不支持高位标签编号(tag=0x{tag:02X})")
    pos = offset + 1
    if pos >= len(data):
        raise _DerStructError("DER 数据在长度字节处截断")
    first = data[pos]
    pos += 1
    if first == 0x80:
        raise _DerStructError("不允许不定长编码(长度 0x80,非 DER)")
    if first < 0x80:
        length = first
    else:
        n = first & 0x7F
        if pos + n > len(data):
            raise _DerStructError("DER 长度八位组截断")
        body = data[pos : pos + n]
        pos += n
        if body[0] == 0:
            raise _DerStructError("长度编码非最短形式(前导零)")
        length = int.from_bytes(body, "big")
    if pos + length > len(data):
        raise _DerStructError(f"内容长度越界(声明 {length},实际剩余 {len(data) - pos})")
    return tag, data[pos : pos + length], pos + length


def _der_children(content: bytes) -> list[tuple[int, bytes]]:
    """把一个 TLV 的内容按序完整拆分为子 TLV 列表(残缺即抛错)。"""
    out: list[tuple[int, bytes]] = []
    pos = 0
    while pos < len(content):
        tag, inner, pos = _read_tlv(content, pos)
        out.append((tag, inner))
    return out


def _decode_integer(content: bytes) -> int:
    """解码 INTEGER 内容(有符号大端);空内容抛错。"""
    if not content:
        raise _DerStructError("INTEGER 内容为空")
    return int.from_bytes(content, "big", signed=True)


def _decode_oid(content: bytes) -> str:
    """解码 OID 内容为点分十进制字符串;续位截断 / 空内容抛错。"""
    if not content:
        raise _DerStructError("OID 内容为空")
    parts: list[int] = []
    value = 0
    for i, byte in enumerate(content):
        value = (value << 7) | (byte & 0x7F)
        if byte & 0x80:
            if i == len(content) - 1:
                raise _DerStructError("OID 末字节仍含续位(编码截断)")
            continue
        if not parts:
            # 首个弧值 = 40*X+Y(X<2 时);X=2 时值为 80+ 余数
            if value < 80:
                parts.extend(divmod(value, 40))
            else:
                parts.extend((2, value - 80))
        else:
            parts.append(value)
        value = 0
    return ".".join(str(p) for p in parts)


def _parse_asn1_time(tag: int, content: bytes) -> str:
    """解析 UTCTime / GeneralizedTime 为 ISO8601(带时区)字符串。

    - GeneralizedTime:``YYYYMMDDHH[MM[SS[.f*]]]`` + ``Z`` 或 ``±HHMM``;
    - UTCTime:``YYMMDDHH[MM[SS]]`` + ``Z`` 或 ``±HHMM``,两位年份按
      RFC 5280 惯例(≥50 → 19xx,<50 → 20xx);
    - 秒缺省按 00 补齐;小数秒截断到微秒(datetime 精度上限)。
    """
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError as exc:
        raise _DerStructError(f"时间字段不是 ASCII:{exc}") from exc
    if tag == _TAG_UTCTIME:
        pattern = r"^(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})?(\d{2})?(\.\d+)?(Z|[+-]\d{4})\Z"
        year_pivot = True
    else:
        pattern = r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})?(\d{2})?(\.\d+)?(Z|[+-]\d{4})\Z"
        year_pivot = False
    m = re.match(pattern, text)
    if m is None:
        raise _DerStructError(f"时间字段格式非法:{text!r}")
    year = int(m.group(1))
    if year_pivot:
        year += 1900 if year >= 50 else 2000
    month, day, hour = int(m.group(2)), int(m.group(3)), int(m.group(4))
    minute = int(m.group(5) or 0)
    second = int(m.group(6) or 0)
    frac = (m.group(7) or "").lstrip(".")
    micro = int((frac + "000000")[:6]) if frac else 0
    zone = m.group(8)
    if zone == "Z":
        tz = _dt.timezone.utc
    else:
        sign = 1 if zone[0] == "+" else -1
        tz = _dt.timezone(sign * _dt.timedelta(hours=int(zone[1:3]), minutes=int(zone[3:5])))
    try:
        moment = _dt.datetime(
            year, month, day, hour, minute, second, micro, tzinfo=tz
        )
    except ValueError as exc:  # 月/日/时等越界
        raise _DerStructError(f"时间字段取值非法({text!r}):{exc}") from exc
    return moment.isoformat()


def verify_tsa_token(token_b64: str | bytes) -> dict:
    """离线校验 RFC3161 TSA 令牌的 **DER 结构骨架**(纯 stdlib,零网络)。

    诚实边界(**重要**):本函数只做**结构校验**,不做密码学验证——
    不解析 CMS SignedData 的证书链、不校验签名者证书、不验签。结构
    校验能证明"这是一份形状合法的 TimeStampResp,genTime 与
    messageImprint 可读且自洽",**不能**证明"它确实出自该 TSA";
    司法等严肃场景仍需 OpenSSL 等独立工具(如 ``openssl ts -verify``)
    做完整密码学核验。校验通过时 ``reasons`` 也会带上这条边界提示。

    校验内容(ASN.1 最小子集,只解释 SEQUENCE / OID / UTCTime /
    GeneralizedTime / OCTET STRING,其余标签仅按 TLV 跳过)::

        TimeStampResp ::= SEQUENCE{
            status  PKIStatusInfo ::= SEQUENCE{ INTEGER status, ... },
            token   TimeStampToken ::= ContentInfo ::= SEQUENCE{
                OID id-ct-TSTInfo(1.2.840.113549.1.9.16.1.4),
                [0] EXPLICIT SignedData ::= SEQUENCE{
                    ...,
                    encapContentInfo ::= SEQUENCE{
                        OID id-ct-TSTInfo,
                        [0] EXPLICIT OCTET STRING( TSTInfo ::= SEQUENCE{
                            INTEGER version, OID policy,
                            messageImprint ::= SEQUENCE{
                                SEQUENCE{ OID hashAlgorithm },
                                OCTET STRING hashedMessage },
                            INTEGER serialNumber,
                            GeneralizedTime genTime, ... } ) } } } }

    :param token_b64: base64 编码的 TimeStampResp DER(即
        :class:`TimeProof` 的 ``token_b64`` 字段;也宽容接受原始 bytes)。
    :return: ``{"valid": bool}`` — 总结论(结构合法且 PKIStatus=0 granted);
        ``{"structure_ok": bool}`` — DER 骨架是否完整合法;
        ``{"gen_time": str | None}`` — ISO8601 带时区(解析失败为 None);
        ``{"message_imprint_hex": str | None}`` — hashedMessage 的 hex;
        ``{"reasons": [str]}`` — 中文说明(问题原因 / 诚实边界提示)。
        任何失败都不抛异常,一律以返回值承载。
    """
    reasons: list[str] = []
    result: dict = {
        "valid": False,
        "structure_ok": False,
        "gen_time": None,
        "message_imprint_hex": None,
        "reasons": reasons,
    }

    def _fail(msg: str) -> dict:
        reasons.append(msg)
        return result

    if not isinstance(token_b64, (str, bytes, bytearray)):
        return _fail(f"令牌类型非法:{type(token_b64).__name__}(应为 base64 字符串)")
    try:
        der = base64.b64decode(bytes(token_b64) if not isinstance(token_b64, str) else token_b64.strip(), validate=True)
    except ValueError as exc:  # binascii.Error 是 ValueError 子类
        return _fail(f"令牌 base64 解码失败:{exc}")
    if not der:
        return _fail("令牌解码后为空")

    try:
        # -- TimeStampResp 顶层 ------------------------------------------------
        tag, content, end = _read_tlv(der, 0)
        if tag != _TAG_SEQUENCE:
            raise _DerStructError(f"顶层不是 SEQUENCE(tag=0x{tag:02X})")
        if end != len(der):
            raise _DerStructError("顶层元素之后存在多余字节(尾随垃圾)")
        resp = _der_children(content)
        if len(resp) != 2:
            raise _DerStructError(
                f"TimeStampResp 应恰有 status+token 两个成员,实得 {len(resp)} 个"
                "(可能为 TSA 拒绝响应:无 timeStampToken)"
            )
        s_tag, s_content = resp[0]
        if s_tag != _TAG_SEQUENCE:
            raise _DerStructError("PKIStatusInfo 不是 SEQUENCE")
        status_children = _der_children(s_content)
        if not status_children or status_children[0][0] != _TAG_INTEGER:
            raise _DerStructError("PKIStatusInfo 首成员不是 INTEGER 状态码")
        status = _decode_integer(status_children[0][1])

        # -- TimeStampToken = ContentInfo --------------------------------------
        ci_tag, ci_content = resp[1]
        if ci_tag != _TAG_SEQUENCE:
            raise _DerStructError("timeStampToken 不是 SEQUENCE(ContentInfo)")
        ci = _der_children(ci_content)
        if len(ci) != 2 or ci[0][0] != _TAG_OID:
            raise _DerStructError("ContentInfo 应恰有 contentType OID + [0] content")
        content_type = _decode_oid(ci[0][1])
        if content_type != _OID_ID_CT_TST_INFO:
            raise _DerStructError(
                f"contentType OID 应为 id-ct-TSTInfo({_OID_ID_CT_TST_INFO}),"
                f"实为 {content_type}"
            )
        if ci[1][0] != _TAG_CTX_0:
            raise _DerStructError("ContentInfo 缺少 [0] EXPLICIT content")

        # -- SignedData:扫描定位 encapContentInfo(eContentType 同为 TSTInfo)--
        sd = _der_children(ci[1][1])
        if len(sd) != 1 or sd[0][0] != _TAG_SEQUENCE:
            raise _DerStructError("[0] content 内不是单个 SignedData SEQUENCE")
        encap: list[tuple[int, bytes]] | None = None
        for c_tag, c_content in _der_children(sd[0][1]):
            if c_tag != _TAG_SEQUENCE:
                continue  # INTEGER version / SET digestAlgorithms / [0] 证书 …
            sub = _der_children(c_content)
            if sub and sub[0][0] == _TAG_OID:
                try:
                    if _decode_oid(sub[0][1]) == _OID_ID_CT_TST_INFO:
                        encap = sub
                        break
                except _DerStructError:
                    continue  # 证书等其余 SEQUENCE 的首成员不是合法 OID 则跳过
        if encap is None:
            raise _DerStructError(
                "SignedData 内未找到 eContentType 为 id-ct-TSTInfo 的 encapContentInfo"
            )
        if len(encap) < 2 or encap[1][0] != _TAG_CTX_0:
            raise _DerStructError("encapContentInfo 缺少 [0] EXPLICIT eContent")
        econtent = _der_children(encap[1][1])
        if len(econtent) != 1 or econtent[0][0] != _TAG_OCTET_STRING:
            raise _DerStructError("eContent 内不是单个 OCTET STRING")

        # -- TSTInfo ------------------------------------------------------------
        tst_der = econtent[0][1]
        t_tag, t_content, t_end = _read_tlv(tst_der, 0)
        if t_tag != _TAG_SEQUENCE:
            raise _DerStructError("TSTInfo 不是 SEQUENCE")
        if t_end != len(tst_der):
            raise _DerStructError("TSTInfo 之后存在多余字节")
        fields = _der_children(t_content)
        if len(fields) < 5:
            raise _DerStructError(
                "TSTInfo 字段不足(应至少含 version/policy/messageImprint/"
                f"serialNumber/genTime 五项,实得 {len(fields)} 项)"
            )
        if fields[0][0] != _TAG_INTEGER:
            raise _DerStructError("TSTInfo[0] version 不是 INTEGER")
        if fields[1][0] != _TAG_OID:
            raise _DerStructError("TSTInfo[1] policy 不是 OID")
        if fields[2][0] != _TAG_SEQUENCE:
            raise _DerStructError("TSTInfo[2] messageImprint 不是 SEQUENCE")
        mi = _der_children(fields[2][1])
        if len(mi) < 2 or mi[0][0] != _TAG_SEQUENCE:
            raise _DerStructError(
                "messageImprint 结构非法(应为 SEQUENCE(hashAlgorithm, hashedMessage))"
            )
        halg = _der_children(mi[0][1])
        if not halg or halg[0][0] != _TAG_OID:
            raise _DerStructError("hashAlgorithm 缺少算法 OID")
        digest_oid = _decode_oid(halg[0][1])
        if mi[1][0] != _TAG_OCTET_STRING:
            raise _DerStructError("hashedMessage 不是 OCTET STRING")
        if fields[3][0] != _TAG_INTEGER:
            raise _DerStructError("TSTInfo[3] serialNumber 不是 INTEGER")
        g_tag, g_content = fields[4]
        if g_tag not in (_TAG_UTCTIME, _TAG_GENERALIZEDTIME):
            raise _DerStructError(
                f"TSTInfo[4] genTime 不是 UTCTime/GeneralizedTime(tag=0x{g_tag:02X})"
            )
        gen_time = _parse_asn1_time(g_tag, g_content)
    except _DerStructError as exc:
        return _fail(f"DER 结构校验失败:{exc}")
    except (ValueError, OverflowError, IndexError) as exc:
        # datetime 越界等意外解析错误统一收敛为结构失败(防御:绝不抛出)
        return _fail(f"DER 结构校验失败(意外解析错误):{type(exc).__name__}: {exc}")

    # -- 骨架完整:回填结果 -----------------------------------------------------
    result["structure_ok"] = True
    result["gen_time"] = gen_time
    result["message_imprint_hex"] = mi[1][1].hex()
    if status != 0:
        reasons.append(f"PKIStatus={status}(非 0 granted),TSA 未正常授戳")
    digest_name = _DIGEST_OID_NAMES.get(digest_oid)
    if digest_name is None:
        reasons.append(f"messageImprint 摘要算法 OID 未识别:{digest_oid}(请人工核验)")
    reasons.append(
        "仅完成 DER 结构校验,未验证 CMS 签名链;密码学验证请用 OpenSSL 等"
        "独立工具(如 openssl ts -verify)"
    )
    result["valid"] = result["structure_ok"] and status == 0
    return result


# ---------------------------------------------------------------------------
# TimestampProver
# ---------------------------------------------------------------------------


@dataclass
class _CounterState:
    """计数器文件的内存形态(版本 + 已签发次数;签发时 +1,首发即 1)。

    ``mono_counter`` 语义是"已签发的最后序号":文件缺失 / 损坏自愈 → 0,
    下一次 :meth:`TimestampProver.issue` 取 1。

    A235 v2 链字段(仅 ``chain_key`` 注入时使用):

    - ``prev_hmac``:上一环的 ``chain_hmac``(首环为 :data:`_CHAIN_GENESIS_PREV`);
    - ``chain_hmac``:本环链值 = HMAC-SHA256(key, f"{counter}|{prev_hmac}");
    - ``tamper_detected``:读取时链校验失败(由 issue 写入 TimeProof.reason,
      证据链上可见篡改痕迹);无链密钥时恒为 False。
    """

    mono_counter: int = 0
    version: int = _COUNTER_VERSION
    prev_hmac: str | None = None
    chain_hmac: str | None = None
    tamper_detected: bool = False


def _chain_hmac(key: bytes, counter: int, prev_hmac: str) -> str:
    """v2 计数器链的一环:HMAC-SHA256(key, f"{counter}|{prev_hmac}") 的 hex。"""
    return hmac.new(
        key, f"{counter}|{prev_hmac}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


class TimestampProver:
    """时间证明签发器:单调计数器 + 可选 TSA 令牌,产出 :class:`TimeProof`。

    用法::

        prover = TimestampProver(counter_path=tmp / "c.json")
        proof1 = prover.issue()          # mono_counter == 1
        proof2 = prover.issue()          # mono_counter == 2(持久化,跨实例单调)
    """

    def __init__(
        self,
        *,
        counter_path: str | os.PathLike | None = None,
        tsa_url: str | None = None,
        tsa_timeout_s: float = DEFAULT_TSA_TIMEOUT_S,
        chain_key: str | bytes | None = None,
    ) -> None:
        """初始化时间证明签发器。

        - ``chain_key``(A235):计数器 HMAC 链密钥,与签名密钥注入体系
          同风格经构造参数注入(str 按 UTF-8 取字节 / bytes 原样;空白
          视同未提供);注入后计数器文件升级 v2 链式结构,链校验失败
          的篡改痕迹写入 ``TimeProof.reason``;缺省 None 保持 v1 明文
          格式**完全兼容**(字节与旧行为一致);
        - 其余参数口径不变(见模块 docstring)。
        """
        self.counter_path = pathlib.Path(counter_path) if counter_path is not None else None
        cleaned_tsa = str(tsa_url).strip() if tsa_url is not None else ""
        self.tsa_url = cleaned_tsa or None  # 空串/空白视同未配置(保持离线)
        self.tsa_timeout_s = max(0.1, float(tsa_timeout_s))
        if isinstance(chain_key, str):
            cleaned = chain_key.strip()
            self._chain_key: bytes | None = cleaned.encode("utf-8") if cleaned else None
        elif isinstance(chain_key, (bytes, bytearray, memoryview)):
            self._chain_key = bytes(chain_key) or None
        else:
            self._chain_key = None
        # counter_path=None 时的进程内计数(不落盘,同实例内仍单调;
        # 有链密钥时链值同样在内存中延续)
        self._memory_counter = 0
        self._memory_prev: str | None = None
        self._memory_chain: str | None = None

    # -- 计数器持久化 ---------------------------------------------------------

    def _read_counter(self) -> _CounterState:
        """读取计数器文件;缺失 → 0 起;损坏 → v1 自愈 / v2 篡改告警重启。

        ``counter_path`` 为 None 时读进程内计数(同实例单调,不落盘)。

        - **无链密钥(默认,v1 兼容)**:坏 JSON / 结构不对 / 负数 → 中文
          告警 + 自愈重置为 1(旧行为逐字节兼容);v2 文件也可被读取
          (取 ``counter`` 值,不验链——旧实例读新文件不破坏);
        - **有链密钥(A235 v2)**:v2 文件逐环校验
          ``chain_hmac = HMAC(key, f"{counter}|{prev_hmac}")``,校验失败或
          文件损坏 → **不再静默重置**:中文告警 + telemetry
          ``timestamp.counter_tamper`` + 重置为 1 + ``tamper_detected``
          (由 :meth:`issue` 写入 ``TimeProof.reason``,证据链上可见);
          v1 旧文件不告警、直接读取,下次写入自动升级 v2。
        """
        if self.counter_path is None:
            return _CounterState(
                mono_counter=self._memory_counter,
                prev_hmac=self._memory_prev,
                chain_hmac=self._memory_chain,
            )
        try:
            raw = self.counter_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError(f"计数器文件不是 JSON 对象:{type(data).__name__}")
            if self._chain_key is not None:
                if "chain_hmac" in data:
                    return self._read_counter_v2(data)
                if data.get("version") == _COUNTER_VERSION_V2:
                    # 带 v2 版本标记却缺链字段:是残缺的 v2 而非合法 v1 遗留
                    raise ValueError("v2 计数器文件缺少链字段(chain_hmac)")
            value = data.get("mono_counter")
            if value is None and "counter" in data:
                value = data["counter"]  # v2 文件被无链密钥实例读取:取数不验链
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"mono_counter 字段非法:{value!r}")
            return _CounterState(mono_counter=value, version=_COUNTER_VERSION)
        except FileNotFoundError:
            return _CounterState()
        except (OSError, ValueError, KeyError) as exc:  # JSONDecodeError 属 ValueError
            if self._chain_key is not None:
                # A235:有链密钥时损坏不再静默自愈 → 篡改痕迹可见
                logger.warning(
                    "时间证明计数器文件损坏或 HMAC 链校验失败(%s:%s),"
                    "检测到篡改痕迹(tamper_detected):计数器从 1 重启并开启新链,"
                    "此前序号不再可信,以 TSA 令牌(若有)为准",
                    self.counter_path,
                    exc,
                )
                telemetry.inc("timestamp.counter_tamper")
                return _CounterState(tamper_detected=True)
            logger.warning(
                "时间证明计数器文件损坏(%s:%s),自愈重置为 1 并继续:"
                "重置期间计数器单调性不保证,以 TSA 令牌(若有)为准",
                self.counter_path,
                exc,
            )
            return _CounterState()

    def _read_counter_v2(self, data: dict) -> _CounterState:
        """读取并逐环校验 v2 计数器文件;不匹配 → ValueError(上层按篡改处理)。"""
        counter = data.get("counter")
        prev = data.get("prev_hmac")
        chain = data.get("chain_hmac")
        if not isinstance(counter, int) or isinstance(counter, bool) or counter < 1:
            raise ValueError(f"counter 字段非法:{counter!r}")
        for name, value in (("prev_hmac", prev), ("chain_hmac", chain)):
            if not isinstance(value, str) or not _HEX64.fullmatch(value):
                raise ValueError(f"{name} 字段非法(应为 64 位十六进制):{value!r}")
        expected = _chain_hmac(self._chain_key, counter, prev)  # type: ignore[arg-type]
        if not hmac.compare_digest(expected, chain):
            raise ValueError("chain_hmac 链校验失败:与 counter|prev_hmac 不匹配")
        return _CounterState(
            mono_counter=counter,
            version=_COUNTER_VERSION_V2,
            prev_hmac=prev,
            chain_hmac=chain,
        )

    def _write_counter(self, state: _CounterState) -> None:
        """原子写计数器 JSON(同目录临时文件 + os.replace;任何失败仅告警)。

        有链密钥时写 v2 链式结构(``{version, counter, prev_hmac,
        chain_hmac}``);无链密钥时写 v1 明文结构,字节与旧行为一致。
        """
        if self.counter_path is None:
            self._memory_counter = state.mono_counter
            self._memory_prev = state.prev_hmac
            self._memory_chain = state.chain_hmac
            return
        if self._chain_key is not None:
            payload = json.dumps(
                {
                    "version": _COUNTER_VERSION_V2,
                    "counter": state.mono_counter,
                    "prev_hmac": state.prev_hmac,
                    "chain_hmac": state.chain_hmac,
                },
                ensure_ascii=False,
            )
        else:
            payload = json.dumps(
                {"version": state.version, "mono_counter": state.mono_counter},
                ensure_ascii=False,
            )
        try:
            self.counter_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self.counter_path.parent), prefix=".tscounter-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(tmp_name, self.counter_path)  # 原子替换,不留半截文件
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError as exc:  # 计数器写失败:证明照发(降级为内存计数),只告警
            logger.warning("时间证明计数器写入失败(%s:%s),本次计数不持久化", self.counter_path, exc)

    def read_counter(self) -> int:
        """只读当前持久化计数器值(不递增;无文件 → 0,即尚未签发)。"""
        return self._read_counter().mono_counter

    # -- TSA(可选,显式配置才联网)-------------------------------------------

    def _fetch_tsa_token(self, message: bytes) -> str:
        """向配置的 TSA 发 RFC3161 请求,返回 base64 令牌;任何异常原样上抛。

        延迟导入 :mod:`urllib.request`:``tsa_url`` 未配置的默认离线路径
        连 urllib 都不加载;超时由 ``tsa_timeout_s`` 控制(短超时)。
        """
        if not self.tsa_url:
            raise ValueError("未配置 tsa_url,不应发起 TSA 请求")
        from urllib.request import Request, urlopen  # 延迟导入:默认路径零网络

        request = _build_timestamp_request(message)
        req = Request(
            self.tsa_url,
            data=request,
            method="POST",
            headers={
                "Content-Type": "application/timestamp-query",
                "Accept": "application/timestamp-reply",
                "User-Agent": "NetSentinel-RFC3161lite/1",
            },
        )
        with urlopen(req, timeout=self.tsa_timeout_s) as resp:  # noqa: S310 显式配置的 TSA 地址
            token = resp.read()
        if not token:
            raise ValueError("TSA 返回空响应")
        return base64.b64encode(token).decode("ascii")

    # -- 签发 ----------------------------------------------------------------

    def issue(self, message: bytes | None = None) -> TimeProof:
        """签发一份时间证明:计数器 +1(持久化),叠加可选 TSA 令牌。

        - ``message``:可选的被证明内容(如证据包签名载荷);提供时其
          sha256 计入 ``message_sha256`` 并作为 TSA 摘要输入;
        - ``tsa_url`` 未配置 → 纯本地证明,零网络;
        - 配置了但请求失败/超时 → 本地证明 + ``reason`` 记录降级原因
          (并计 telemetry ``timestamp.tsa_degrade``),绝不抛错;
        - A235:注入 ``chain_key`` 后计数器落 v2 链式结构;读取时链校验
          失败的本次证明 ``reason`` 携带 ``tamper_detected`` 标志
          (中文告警 + telemetry ``timestamp.counter_tamper``,计数器
          从 1 重启开启新链),证据链上可见篡改痕迹。
        """
        state = self._read_counter()
        state.mono_counter += 1
        if self._chain_key is not None:
            # 链延续:上一环的 chain_hmac 是本环的 prev(篡改重启后从创世值起重开新链)
            prev = state.chain_hmac or _CHAIN_GENESIS_PREV
            state.prev_hmac = prev
            state.chain_hmac = _chain_hmac(self._chain_key, state.mono_counter, prev)
        self._write_counter(state)
        tamper_reason: str | None = None
        if state.tamper_detected:
            tamper_reason = (
                "计数器 HMAC 链校验失败,检测到篡改痕迹(tamper_detected):"
                "计数器已从 1 重启并开启新链,此前序号不再可信,以 TSA 令牌(若有)为准"
            )
            logger.warning(tamper_reason)

        utc_iso = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        msg = bytes(message) if message is not None else None
        msg_sha = hashlib.sha256(msg).hexdigest() if msg is not None else None

        if not self.tsa_url:
            return TimeProof(
                utc_iso=utc_iso,
                mono_counter=state.mono_counter,
                source=SOURCE_LOCAL,
                tsa_url=None,
                message_sha256=msg_sha,
                reason=tamper_reason,
            )

        try:
            token_b64 = self._fetch_tsa_token(msg if msg is not None else utc_iso.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 TSA 是增强项:任何异常都安全降级
            reason = f"TSA 请求失败,已降级为本地时间证明:{type(exc).__name__}: {exc}"
            if tamper_reason:
                reason = f"{tamper_reason};{reason}"
            logger.warning("%s(tsa_url=%s)", reason, self.tsa_url)
            telemetry.inc("timestamp.tsa_degrade")
            return TimeProof(
                utc_iso=utc_iso,
                mono_counter=state.mono_counter,
                source=SOURCE_LOCAL,
                tsa_url=self.tsa_url,
                message_sha256=msg_sha,
                reason=reason,
            )
        return TimeProof(
            utc_iso=utc_iso,
            mono_counter=state.mono_counter,
            source=SOURCE_TSA,
            tsa_url=self.tsa_url,
            token_b64=token_b64,
            message_sha256=msg_sha,
            reason=tamper_reason,
        )
