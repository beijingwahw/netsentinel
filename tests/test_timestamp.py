# -*- coding: utf-8 -*-
"""A192:netsentinel.security.timestamp(RFC3161-lite)单元测试。

全部离线:TSA 路径一律 monkeypatch ``urllib.request.urlopen``;
默认 ``tsa_url=None`` 的用例额外断言 **urlopen 根本未被调用**(默认禁网红线)。

A235 追加:TSA 令牌离线 DER 结构校验器(verify_tsa_token,手工构造合法
RFC3161 响应骨架 + 各类截断/坏 TAG/坏时间拒绝、genTime 解析边界、
诚实边界提示)+ 计数器 HMAC 链 v2(正常递增 / 三类篡改检测 / v1 自动
升级 / 无 key 时 v1 明文完全兼容 / 损坏 JSON 有 key 时不再静默)。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import pathlib
import urllib.request

import pytest

from netsentinel import telemetry
from netsentinel.security import timestamp as ts
from netsentinel.security.timestamp import TimeProof, TimestampProver, verify_tsa_token


@pytest.fixture(autouse=True)
def _reset_telemetry() -> None:
    telemetry.reset()


def _prover(path: pathlib.Path, **kwargs) -> TimestampProver:
    return TimestampProver(counter_path=path, **kwargs)


# ---------------------------------------------------------------------------
# 本地时间证明:结构与可序列化
# ---------------------------------------------------------------------------


def test_local_proof_structure(tmp_path: pathlib.Path) -> None:
    """默认全离线:source=local,utc_iso 带 UTC 后缀,无 token 无 reason。"""
    prover = _prover(tmp_path / "c.json")
    proof = prover.issue()

    assert isinstance(proof, TimeProof)
    assert proof.source == "local"
    assert proof.tsa_url is None
    assert proof.token_b64 is None
    assert proof.reason is None
    assert proof.mono_counter == 1  # 首发序号 1
    assert proof.utc_iso.endswith("+00:00")


def test_proof_is_json_serializable(tmp_path: pathlib.Path) -> None:
    """as_dict 可直接 json.dumps(含 message 绑定摘要)且往返无损。"""
    prover = _prover(tmp_path / "c.json")
    plain = prover.issue()
    bound = prover.issue(b"evidence-payload")

    for proof in (plain, bound):
        encoded = json.dumps(proof.as_dict(), ensure_ascii=False)
        assert json.loads(encoded) == proof.as_dict()

    assert plain.message_sha256 is None
    assert bound.message_sha256 == hashlib.sha256(b"evidence-payload").hexdigest()


# ---------------------------------------------------------------------------
# 单调计数器:进程内单调 / 跨实例持久化 / 原子写
# ---------------------------------------------------------------------------


def test_counter_strictly_increasing_within_instance(tmp_path: pathlib.Path) -> None:
    prover = _prover(tmp_path / "c.json")
    counters = [prover.issue().mono_counter for _ in range(5)]
    assert counters == [1, 2, 3, 4, 5]
    assert prover.read_counter() == 5


def test_counter_persisted_across_instances(tmp_path: pathlib.Path) -> None:
    """新实例读同一文件继续递增(跨进程持久化)。"""
    counter_file = tmp_path / "c.json"
    first = _prover(counter_file)
    last = max(first.issue().mono_counter for _ in range(3))

    second = TimestampProver(counter_path=counter_file)
    proof = second.issue()
    assert proof.mono_counter == last + 1


def test_counter_file_is_valid_json_no_leftovers(tmp_path: pathlib.Path) -> None:
    """落盘文件恒为合法 JSON 且不留 .tmp 残件(原子写证据)。"""
    counter_file = tmp_path / "c.json"
    prover = _prover(counter_file)
    for _ in range(3):
        prover.issue()

    data = json.loads(counter_file.read_text(encoding="utf-8"))
    assert data["mono_counter"] == 3
    assert isinstance(data["version"], int)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "c.json"]
    assert leftovers == []


def test_memory_only_mode_without_path() -> None:
    """counter_path=None:纯内存计数,同实例单调,新实例互不干扰。"""
    a = TimestampProver()
    assert a.issue().mono_counter == 1
    assert a.issue().mono_counter == 2
    assert TimestampProver().issue().mono_counter == 1


# ---------------------------------------------------------------------------
# 损坏自愈:坏 JSON / 结构不对 / 负数 → 重置为 1 继续签发
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "garbage",
    [
        "{broken json",                # 坏 JSON
        "[1, 2, 3]",                   # 不是对象
        '{"version": 1}',              # 缺 mono_counter
        '{"mono_counter": "five"}',    # 非整数
        '{"mono_counter": true}',      # bool 不是计数器
        '{"mono_counter": -7}',        # 负数
        '{"mono_counter": 3.5}',       # 浮点(非 int)
    ],
    ids=["bad-json", "not-dict", "missing-key", "str-value", "bool-value", "negative", "float"],
)
def test_corrupt_counter_self_heals(
    tmp_path: pathlib.Path, garbage: str, caplog: pytest.LogCaptureFixture
) -> None:
    counter_file = tmp_path / "c.json"
    counter_file.write_text(garbage, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="netsentinel.security.timestamp"):
        proof = TimestampProver(counter_path=counter_file).issue()

    assert proof.mono_counter == 1  # 自愈重置后首发
    assert any("自愈" in r.message for r in caplog.records)
    # 自愈后的文件恢复合法 JSON,后续继续单调
    follow = TimestampProver(counter_path=counter_file).issue()
    assert follow.mono_counter == 2
    assert json.loads(counter_file.read_text(encoding="utf-8"))["mono_counter"] == 2


def test_missing_counter_file_starts_from_one(tmp_path: pathlib.Path) -> None:
    counter_file = tmp_path / "sub" / "c.json"  # 父目录尚不存在
    prover = TimestampProver(counter_path=counter_file)
    assert prover.read_counter() == 0
    assert prover.issue().mono_counter == 1  # 自动建目录落盘
    assert counter_file.is_file()


# ---------------------------------------------------------------------------
# 默认禁网:tsa_url=None 绝不触网
# ---------------------------------------------------------------------------


def test_offline_default_never_calls_urlopen(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """红线:未配置 tsa_url 时签发 N 份证明,urlopen 必须一次都没被调用。"""
    calls: list[object] = []

    def _boom(*args: object, **kwargs: object):
        calls.append(args)
        raise AssertionError("默认禁网被违反:tsa_url=None 却发起了网络请求")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    prover = _prover(tmp_path / "c.json")  # tsa_url 未配置(默认 None)

    for _ in range(3):
        proof = prover.issue(b"payload")
    assert calls == []
    assert all(p.source == "local" for p in [proof])


def test_tsa_url_empty_string_stays_offline(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tsa_url 传空串/空白 → 视同未配置,零网络。"""
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("触网"))
    )
    prover = TimestampProver(counter_path=tmp_path / "c.json", tsa_url="   ")
    assert prover.tsa_url is None
    assert prover.issue().source == "local"


# ---------------------------------------------------------------------------
# TSA 路径(显式配置,monkeypatch urlopen):成功 / 降级 / 超时与请求结构
# ---------------------------------------------------------------------------


class _FakeResponse(io.BytesIO):
    """urlopen 返回物的替身:只用到 read() 与上下文管理器。"""


def test_tsa_success_token_recorded(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = b"\x30\x82\x01\x02FAKE-RFC3161-REPLY"
    seen: dict[str, object] = {}

    def _fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["data"] = req.data
        seen["timeout"] = timeout
        seen["content_type"] = req.get_header("Content-type")
        return _FakeResponse(token)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    prover = TimestampProver(
        counter_path=tmp_path / "c.json", tsa_url="https://tsa.example/timestamp"
    )
    proof = prover.issue(b"the-evidence")

    assert proof.source == "tsa"
    assert proof.token_b64 == base64.b64encode(token).decode("ascii")
    assert proof.reason is None
    assert proof.message_sha256 == hashlib.sha256(b"the-evidence").hexdigest()
    assert seen["url"] == "https://tsa.example/timestamp"
    assert seen["timeout"] == ts.DEFAULT_TSA_TIMEOUT_S  # 短超时
    assert str(seen["content_type"]).lower() == "application/timestamp-query"
    # 请求体为 DER TimeStampReq:SEQUENCE 起始且含 SHA-256 OID 与摘要
    body = bytes(seen["data"])  # type: ignore[arg-type]
    assert body[0] == 0x30
    assert bytes.fromhex("0609608648016503040201") in body
    assert hashlib.sha256(b"the-evidence").digest() in body


def test_tsa_der_request_minimal_structure() -> None:
    """DER 请求 = SEQUENCE( INTEGER 1, SEQUENCE( SEQUENCE(OID), OCTET STRING ) )。"""
    req = ts._build_timestamp_request(b"abc")
    assert req[0] == 0x30
    assert req[1] == len(req) - 2  # 定长形式:长度八位组覆盖全部内容
    assert req[2:5] == b"\x02\x01\x01"  # version INTEGER 1
    digest = hashlib.sha256(b"abc").digest()
    assert digest in req


@pytest.mark.parametrize(
    "exc",
    [OSError("网络不可达"), TimeoutError("TSA 超时"), ValueError("bad-der-reply")],
    ids=["oserror", "timeout", "bad-reply"],
)
def test_tsa_failure_degrades_to_local(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """TSA 任何异常 → 本地证明 + reason 记录原因 + telemetry 降级计数,不抛错。"""
    def _fake_urlopen(req, timeout=None):
        raise exc

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    prover = TimestampProver(
        counter_path=tmp_path / "c.json", tsa_url="https://tsa.example/ts"
    )
    proof = prover.issue(b"payload")

    assert proof.source == "local"
    assert proof.token_b64 is None
    assert proof.tsa_url == "https://tsa.example/ts"
    assert type(exc).__name__ in proof.reason
    assert "降级" in proof.reason
    assert telemetry.snapshot()["counters"]["timestamp.tsa_degrade"] == 1


def test_tsa_empty_reply_degrades(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(b""))
    prover = TimestampProver(
        counter_path=tmp_path / "c.json", tsa_url="https://tsa.example/ts"
    )
    proof = prover.issue()
    assert proof.source == "local"
    assert proof.reason is not None and "空" in proof.reason


def test_tsa_custom_timeout_passed_through(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def _fake_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        return _FakeResponse(b"tok")

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    prover = TimestampProver(
        counter_path=tmp_path / "c.json", tsa_url="https://tsa.example/ts", tsa_timeout_s=2.5
    )
    assert prover.tsa_timeout_s == 2.5
    prover.issue()
    assert seen["timeout"] == 2.5


def test_tsa_timeout_floored_at_minimum(tmp_path: pathlib.Path) -> None:
    """超时下限 0.1s:传 0/负数不会被钳成非法值。"""
    prover = TimestampProver(
        counter_path=tmp_path / "c.json", tsa_url="https://tsa.example/ts", tsa_timeout_s=0
    )
    assert prover.tsa_timeout_s == 0.1


def test_tsa_success_not_counted_as_degrade(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(b"tok"))
    prover = TimestampProver(
        counter_path=tmp_path / "c.json", tsa_url="https://tsa.example/ts"
    )
    prover.issue()
    assert "timestamp.tsa_degrade" not in telemetry.snapshot()["counters"]


# ---------------------------------------------------------------------------
# A235:TSA 令牌离线 DER 结构校验器 verify_tsa_token
# 手工构造合法 RFC3161 TimeStampResp 骨架(确定性,无第三方样例文件依赖)
# ---------------------------------------------------------------------------
#: id-ct-TSTInfo(1.2.840.113549.1.9.16.1.4):TimeStampToken/TSTInfo 的 contentType
_OID_TST_INFO = "1.2.840.113549.1.9.16.1.4"

#: id-data(1.2.840.113549.1.7.1):错误的 contentType 对照
_OID_ID_DATA = "1.2.840.113549.1.7.1"

#: SHA-256 摘要算法 OID(请求侧同款)
_OID_SHA256 = "2.16.840.1.101.3.4.2.1"

#: A235 确定性测试链密钥(固定值,禁随机)
CHAIN_KEY = b"a235-counter-chain-key-0123456789"


def _tlv(tag: int, content: bytes) -> bytes:
    """测试侧 DER TLV 编码(短/长长度形式;与被测解码器独立实现)。"""
    n = len(content)
    if n < 0x80:
        length = bytes([n])
    else:
        body = n.to_bytes((n.bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(body)]) + body
    return bytes([tag]) + length + content


def _oid_der(dotted: str) -> bytes:
    """点分十进制 OID → DER 内容八位组(base-128,独立于被测实现)。"""
    parts = [int(x) for x in dotted.split(".")]
    out = bytearray([40 * parts[0] + parts[1]])
    for p in parts[2:]:
        if p == 0:
            out.append(0)
            continue
        stack = []
        while p:
            stack.append(p & 0x7F)
            p >>= 7
        for i, b in enumerate(reversed(stack)):
            out.append(b | (0x80 if i < len(stack) - 1 else 0))
    return bytes(out)


def _build_tst_info(
    gen_time: str = "20261001123456Z",
    *,
    gen_tag: int = 0x18,
    with_gen_time: bool = True,
    with_message_imprint: bool = True,
    digest: bytes = bytes.fromhex("ab" * 32),
    trailing: bytes = b"",
) -> bytes:
    """构造 TSTInfo DER(version/policy/messageImprint/serialNumber/genTime)。

    ``trailing``:追加在末尾的附加字段(如 ordering/nonce 的 INTEGER),
    用于构造"字段数足够但关键字段缺失/错位"的畸形用例。
    """
    fields = [
        _tlv(0x02, b"\x01"),                              # version 1
        _tlv(0x06, _oid_der("1.3.6.1.4.1.9999")),         # policy(测试值)
    ]
    if with_message_imprint:
        halg = _tlv(0x30, _tlv(0x06, _oid_der(_OID_SHA256)))
        fields.append(_tlv(0x30, halg + _tlv(0x04, digest)))
    fields.append(_tlv(0x02, b"\x2a"))                    # serialNumber 42
    if with_gen_time:
        fields.append(_tlv(gen_tag, gen_time.encode("ascii")))
    if trailing:
        fields.append(trailing)
    return _tlv(0x30, b"".join(fields))


def _build_token(
    tst_info: bytes,
    *,
    content_oid: str = _OID_TST_INFO,
    encap_oid: str | None = None,
) -> bytes:
    """构造 ContentInfo(OID + [0] SignedData(… encapContentInfo(eContent)))。"""
    encap = _tlv(
        0x30,
        _tlv(0x06, _oid_der(encap_oid or _OID_TST_INFO))
        + _tlv(0xA0, _tlv(0x04, tst_info)),
    )
    signed_data = _tlv(
        0x30,
        _tlv(0x02, b"\x01")                                        # version
        + _tlv(0x31, _tlv(0x30, _tlv(0x06, _oid_der(_OID_SHA256))))  # digestAlgorithms
        + encap
        + _tlv(0x31, b""),                                         # signerInfos(空)
    )
    return _tlv(0x30, _tlv(0x06, _oid_der(content_oid)) + _tlv(0xA0, signed_data))


def _build_resp(token: bytes | None = ..., status: int = 0) -> bytes:
    """构造 TimeStampResp(PKIStatusInfo + 可选 timeStampToken)。"""
    status_info = _tlv(0x30, _tlv(0x02, bytes([status])))
    if token is None:
        return _tlv(0x30, status_info)
    return _tlv(0x30, status_info + token)


def _b64(der: bytes) -> str:
    return base64.b64encode(der).decode("ascii")


def _valid_resp_b64(**kwargs) -> str:
    return _b64(_build_resp(_build_token(_build_tst_info(**kwargs))))


def test_a235_verify_valid_rfc3161_structure() -> None:
    """合法骨架:valid/structure_ok 双真,genTime 与 imprint 精确回读,
    reasons 携带诚实边界提示(结构校验非密码学验证)。"""
    digest = bytes.fromhex("ab" * 32)
    out = verify_tsa_token(_valid_resp_b64(digest=digest))

    assert out["valid"] is True
    assert out["structure_ok"] is True
    assert out["gen_time"] == "2026-10-01T12:34:56+00:00"
    assert out["message_imprint_hex"] == digest.hex()
    assert isinstance(out["reasons"], list) and out["reasons"]
    assert all(isinstance(r, str) and r for r in out["reasons"])
    # 诚实边界:成功路径也明示未验证签名链(A192 风险项的文档化边界)
    assert any("未验证" in r and "签名链" in r for r in out["reasons"])


@pytest.mark.parametrize(
    ("name", "token", "needle"),
    [
        ("empty", "", "为空"),
        ("bad-base64", "!!!not@base64!!!", "base64 解码失败"),
        ("not-a-string", None, "类型非法"),
        ("truncated", None, "越界"),
        ("top-not-sequence", None, "顶层不是 SEQUENCE"),
        ("trailing-garbage", None, "多余字节"),
        ("wrong-content-type", None, "contentType OID"),
        ("wrong-encap-oid", None, "encapContentInfo"),
        ("missing-gen-time", None, "字段不足"),
        ("bad-gen-time-value", None, "取值非法"),
        ("bad-gen-time-tag", None, "genTime 不是"),
        ("missing-message-imprint", None, "messageImprint 不是 SEQUENCE"),
        ("indefinite-length", None, "不定长"),
        ("rejection-no-token", None, "timeStampToken"),
    ],
    ids=[
        "empty", "bad-base64", "not-a-string", "truncated", "top-not-seq",
        "trailing", "wrong-content-oid", "wrong-encap-oid", "no-gen-time",
        "bad-gen-value", "bad-gen-tag", "no-imprint", "indefinite-len", "rejected",
    ],
)
def test_a235_verify_rejects_malformed_tokens(name: str, token, needle: str) -> None:
    """七类以上畸形令牌一律拒绝:valid/structure_ok 双假 + 中文原因,绝不抛错。"""
    if name == "not-a-string":
        payload = 12345  # type: ignore[assignment]
    elif name == "truncated":
        der = _build_resp(_build_token(_build_tst_info()))
        payload = _b64(der[: len(der) // 2])  # DER 中途截断(长度声明越界)
    elif name == "top-not-sequence":
        der = _build_resp(_build_token(_build_tst_info()))
        payload = _b64(bytes([0x31]) + der[1:])  # 顶层 SEQUENCE → SET
    elif name == "trailing-garbage":
        payload = _b64(_build_resp(_build_token(_build_tst_info())) + b"\x00")
    elif name == "wrong-content-type":
        payload = _b64(_build_resp(_build_token(_build_tst_info(), content_oid=_OID_ID_DATA)))
    elif name == "wrong-encap-oid":
        payload = _b64(_build_resp(_build_token(_build_tst_info(), encap_oid=_OID_ID_DATA)))
    elif name == "missing-gen-time":
        payload = _b64(_build_resp(_build_token(_build_tst_info(with_gen_time=False))))
    elif name == "bad-gen-time-value":
        payload = _b64(_build_resp(_build_token(_build_tst_info(gen_time="20261301123456Z"))))
    elif name == "bad-gen-time-tag":
        payload = _b64(_build_resp(
            _build_token(_build_tst_info(gen_time="20261001123456Z", gen_tag=0x04))
        ))
    elif name == "missing-message-imprint":
        # 缺 messageImprint 但补一个尾部 INTEGER(ordering)保持字段数,
        # 命中"messageImprint 不是 SEQUENCE"分支而非"字段不足"
        payload = _b64(_build_resp(_build_token(_build_tst_info(
            with_message_imprint=False, trailing=_tlv(0x02, b"\x01")
        ))))
    elif name == "indefinite-length":
        payload = _b64(b"\x30\x80\x02\x01\x00\x00\x00")
    elif name == "rejection-no-token":
        payload = _b64(_build_resp(None, status=2))
    else:
        payload = token

    out = verify_tsa_token(payload)
    assert out["valid"] is False
    assert out["structure_ok"] is False
    assert out["gen_time"] is None and out["message_imprint_hex"] is None
    assert any(needle in r for r in out["reasons"]), out["reasons"]


def test_a235_verify_nonzero_status_with_token_structure_ok_but_invalid() -> None:
    """PKIStatus 非 0(如 grantedWithMods=1 / 其他):骨架完整可解析,
    valid 判假并给出中文原因。"""
    out = verify_tsa_token(_b64(_build_resp(_build_token(_build_tst_info()), status=1)))

    assert out["structure_ok"] is True
    assert out["valid"] is False
    assert out["gen_time"] == "2026-10-01T12:34:56+00:00"  # 结构字段照常回读
    assert any("PKIStatus" in r for r in out["reasons"])


@pytest.mark.parametrize(
    ("gen_time", "gen_tag", "expected"),
    [
        ("20261001123456Z", 0x18, "2026-10-01T12:34:56+00:00"),
        ("20261001123456.123456Z", 0x18, "2026-10-01T12:34:56.123456+00:00"),
        ("20261001203456+0800", 0x18, "2026-10-01T20:34:56+08:00"),
        ("20261001123456-0500", 0x18, "2026-10-01T12:34:56-05:00"),
        ("2026100112Z", 0x18, "2026-10-01T12:00:00+00:00"),          # 缺分秒按 0 补
        ("261001123456Z", 0x17, "2026-10-01T12:34:56+00:00"),        # UTCTime 20xx
        ("991231235959Z", 0x17, "1999-12-31T23:59:59+00:00"),        # UTCTime 19xx
        ("501001000000Z", 0x17, "1950-10-01T00:00:00+00:00"),        # 年份枢轴 50
    ],
    ids=["gt-z", "gt-frac", "gt-plus0800", "gt-minus0500", "gt-hh-only", "utc-20xx", "utc-19xx", "utc-pivot"],
)
def test_a235_gen_time_parsing_variants(gen_time: str, gen_tag: int, expected: str) -> None:
    """genTime 解析边界:GeneralizedTime 分数秒/时区偏移、UTCTime 两位年
    枢轴(≥50 → 19xx)、缺分秒补零。"""
    out = verify_tsa_token(_b64(_build_resp(_build_token(
        _build_tst_info(gen_time=gen_time, gen_tag=gen_tag)
    ))))
    assert out["gen_time"] == expected


@pytest.mark.parametrize(
    "gen_time",
    ["20261001123456", "20261032123456Z", "20261001256000Z", "20261001123456Z ", "261001123456", "20261001123456Z\n"],
    ids=["no-zone", "day32", "min60", "trailing-space", "utc-no-zone", "trailing-newline"],
)
def test_a235_gen_time_invalid_formats_rejected(gen_time: str) -> None:
    """genTime 非法形态(缺时区 / 日期越界 / 尾随空白)→ 结构校验失败。"""
    out = verify_tsa_token(_b64(_build_resp(_build_token(
        _build_tst_info(gen_time=gen_time.encode("ascii", errors="ignore").decode("ascii"))
    ))))
    assert out["valid"] is False and out["structure_ok"] is False
    assert out["gen_time"] is None


def test_a235_verify_token_from_prover_roundtrip(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端:prover 收到合法 RFC3161 响应 → token_b64 入证明 →
    verify_tsa_token 离线复核通过,imprint 与被证明内容 sha256 一致。"""
    message = b"the-evidence-payload"
    digest = hashlib.sha256(message).digest()
    resp = _build_resp(_build_token(_build_tst_info(digest=digest)))

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(resp)
    )
    prover = TimestampProver(
        counter_path=tmp_path / "c.json", tsa_url="https://tsa.example/ts"
    )
    proof = prover.issue(message)

    assert proof.source == "tsa" and proof.reason is None
    out = verify_tsa_token(proof.token_b64)
    assert out["valid"] is True and out["structure_ok"] is True
    assert out["message_imprint_hex"] == digest.hex()
    assert proof.message_sha256 == digest.hex()


def test_a235_verify_docstring_declares_no_crypto_boundary() -> None:
    """文档化诚实边界:verify_tsa_token 的 docstring 明示不验签、需独立工具。"""
    text = verify_tsa_token.__doc__ or ""
    assert "结构校验" in text
    assert "不" in text and ("密码学" in text or "验签" in text)
    assert "OpenSSL" in text


# ---------------------------------------------------------------------------
# A235:计数器 HMAC 链 v2(正常递增 / 篡改检测 / v1 升级 / 无 key 兼容)
# ---------------------------------------------------------------------------
def _expected_chain(counter: int, prev: str) -> str:
    """测试侧独立复算链公式:HMAC-SHA256(key, f"{counter}|{prev}")。"""
    return hmac.new(
        CHAIN_KEY, f"{counter}|{prev}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _hex64_ok(value: str) -> bool:
    """v2 链字段形态检查:恰 64 个小写十六进制字符。"""
    return isinstance(value, str) and len(value) == 64 and all(
        c in "0123456789abcdef" for c in value
    )


def test_a235_chain_normal_increment_and_file_format(tmp_path: pathlib.Path) -> None:
    """正常路径:计数递增,文件为 v2 链式结构,链值可独立复算,reason 干净。"""
    counter_file = tmp_path / "c.json"
    prover = TimestampProver(counter_path=counter_file, chain_key=CHAIN_KEY)
    first = prover.issue()
    second = prover.issue()

    assert (first.mono_counter, second.mono_counter) == (1, 2)
    assert first.reason is None and second.reason is None

    data = json.loads(counter_file.read_text(encoding="utf-8"))
    assert data["version"] == 2 and data["counter"] == 2
    # 首环 prev 为全零创世值;第二环 prev 为首环 chain_hmac(链式衔接)
    genesis = "0" * 64
    assert data["prev_hmac"] == _expected_chain(1, genesis)
    assert data["chain_hmac"] == _expected_chain(2, data["prev_hmac"])
    assert _hex64_ok(data["chain_hmac"]) and _hex64_ok(data["prev_hmac"])
    # 链完整时零篡改计数
    assert "timestamp.counter_tamper" not in telemetry.snapshot()["counters"]


def _tamper_and_issue(counter_file: pathlib.Path, mutate) -> TimeProof:
    """读出 v2 文件 → 施加篡改 → 新实例签发,返回首份证明。"""
    data = json.loads(counter_file.read_text(encoding="utf-8"))
    mutate(data)
    counter_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    telemetry.reset()
    return TimestampProver(counter_path=counter_file, chain_key=CHAIN_KEY).issue()


def test_a235_chain_tampered_counter_detected(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """篡改计数器数值(2 → 9,链值原样):检出 → 从 1 重启 + tamper_detected
    入 reason + 中文告警 + telemetry 计数;后续恢复单调。"""
    counter_file = tmp_path / "c.json"
    prover = TimestampProver(counter_path=counter_file, chain_key=CHAIN_KEY)
    prover.issue()
    prover.issue()

    with caplog.at_level(logging.WARNING, logger="netsentinel.security.timestamp"):
        proof = _tamper_and_issue(counter_file, lambda d: d.update(counter=9))

    assert proof.mono_counter == 1  # 不再静默:从 1 重启开启新链
    assert proof.reason is not None and "tamper_detected" in proof.reason
    assert any("篡改" in r.getMessage() for r in caplog.records)
    assert telemetry.snapshot()["counters"]["timestamp.counter_tamper"] == 1
    follow = TimestampProver(counter_path=counter_file, chain_key=CHAIN_KEY).issue()
    assert follow.mono_counter == 2 and follow.reason is None  # 新链恢复正常


def test_a235_chain_tampered_chain_hmac_detected(tmp_path: pathlib.Path) -> None:
    """篡改 chain_hmac(改一个 hex 字符):链公式不匹配 → 检出。"""
    counter_file = tmp_path / "c.json"
    prover = TimestampProver(counter_path=counter_file, chain_key=CHAIN_KEY)
    prover.issue()

    def _mutate(d: dict) -> None:
        bad = list(d["chain_hmac"])
        bad[0] = "0" if bad[0] != "0" else "1"
        d["chain_hmac"] = "".join(bad)

    proof = _tamper_and_issue(counter_file, _mutate)
    assert proof.mono_counter == 1
    assert proof.reason is not None and "tamper_detected" in proof.reason


def test_a235_chain_tampered_prev_hmac_detected(tmp_path: pathlib.Path) -> None:
    """篡改 prev_hmac(链式衔接字段):HMAC 覆盖 prev → 检出。"""
    counter_file = tmp_path / "c.json"
    prover = TimestampProver(counter_path=counter_file, chain_key=CHAIN_KEY)
    prover.issue()
    prover.issue()  # 第二环的 prev_hmac 才非创世值

    def _mutate(d: dict) -> None:
        bad = list(d["prev_hmac"])
        bad[-1] = "0" if bad[-1] != "0" else "1"
        d["prev_hmac"] = "".join(bad)

    proof = _tamper_and_issue(counter_file, _mutate)
    assert proof.mono_counter == 1
    assert proof.reason is not None and "tamper_detected" in proof.reason


@pytest.mark.parametrize(
    "garbage", ["{broken json", '{"version": 2, "counter": 5}', "[]"],
    ids=["bad-json", "half-v2-no-chain", "not-dict"],
)
def test_a235_chain_corrupt_file_with_key_flags_tamper(
    tmp_path: pathlib.Path, garbage: str, caplog: pytest.LogCaptureFixture
) -> None:
    """有链密钥时文件损坏(坏 JSON / 半截 v2 / 非对象)→ 不再静默自愈:
    篡改痕迹可见(reason 标志 + 告警 + 计数),从 1 重启。"""
    counter_file = tmp_path / "c.json"
    counter_file.write_text(garbage, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="netsentinel.security.timestamp"):
        proof = TimestampProver(counter_path=counter_file, chain_key=CHAIN_KEY).issue()

    assert proof.mono_counter == 1
    assert proof.reason is not None and "tamper_detected" in proof.reason
    assert any("篡改" in r.getMessage() for r in caplog.records)
    assert telemetry.snapshot()["counters"]["timestamp.counter_tamper"] == 1


def test_a235_v1_file_auto_upgrades_to_v2(tmp_path: pathlib.Path) -> None:
    """v1 旧文件(明文 mono_counter)被有 key 实例读取不破坏:计数延续,
    下次写入自动升级 v2 链式结构,再下一环链校验通过。"""
    counter_file = tmp_path / "c.json"
    counter_file.write_text(
        json.dumps({"version": 1, "mono_counter": 5}), encoding="utf-8"
    )
    prover = TimestampProver(counter_path=counter_file, chain_key=CHAIN_KEY)
    proof = prover.issue()

    assert proof.mono_counter == 6  # v1 计数延续
    assert proof.reason is None     # 升级不是篡改:零告警标志
    data = json.loads(counter_file.read_text(encoding="utf-8"))
    assert data["version"] == 2 and data["counter"] == 6
    assert data["prev_hmac"] == "0" * 64  # 旧明文无链可续 → 创世值起
    assert data["chain_hmac"] == _expected_chain(6, "0" * 64)
    follow = TimestampProver(counter_path=counter_file, chain_key=CHAIN_KEY).issue()
    assert follow.mono_counter == 7 and follow.reason is None  # 新链继续单调


def test_a235_no_key_keeps_v1_plain_format(tmp_path: pathlib.Path) -> None:
    """缺省无 key:保持 v1 明文格式完全兼容(键集与数值同旧行为)。"""
    counter_file = tmp_path / "c.json"
    prover = TimestampProver(counter_path=counter_file)  # 无 chain_key
    prover.issue()
    prover.issue()

    data = json.loads(counter_file.read_text(encoding="utf-8"))
    assert set(data) == {"version", "mono_counter"}
    assert data == {"version": 1, "mono_counter": 2}


def test_a235_blank_chain_key_treated_as_absent(tmp_path: pathlib.Path) -> None:
    """空白链密钥(str)视同未提供:计数器仍走 v1 明文格式。"""
    counter_file = tmp_path / "c.json"
    prover = TimestampProver(counter_path=counter_file, chain_key="   ")
    prover.issue()
    data = json.loads(counter_file.read_text(encoding="utf-8"))
    assert set(data) == {"version", "mono_counter"}


def test_a235_v2_file_readable_without_key(tmp_path: pathlib.Path) -> None:
    """向后兼容:v2 文件被无 key 实例读取不破坏(取 counter 值,不验链)。"""
    counter_file = tmp_path / "c.json"
    keyed = TimestampProver(counter_path=counter_file, chain_key=CHAIN_KEY)
    keyed.issue()
    keyed.issue()
    keyed.issue()

    plain = TimestampProver(counter_path=counter_file)
    assert plain.read_counter() == 3
    assert plain.issue().mono_counter == 4


def test_a235_memory_mode_with_chain_key() -> None:
    """counter_path=None(纯内存)+ 链密钥:同实例单调,链在内存中延续。"""
    prover = TimestampProver(chain_key=CHAIN_KEY)
    first = prover.issue()
    second = prover.issue()
    assert (first.mono_counter, second.mono_counter) == (1, 2)
    assert first.reason is None and second.reason is None
