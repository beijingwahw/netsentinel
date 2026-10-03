# -*- coding: utf-8 -*-
"""A192:netsentinel.security.ed25519 单元测试(离线,零网络)。

核心锁定:RFC 8032 §7.1 官方测试向量 1-3(公钥 + 签名逐字节断言)、
确定性(同 seed 同签名)、篡改拒绝(消息/签名/公钥)、防错口径
(非 32 字节 seed / 坏签名格式 → ValueError;数学验签失败 → False 不抛错)。
纯 Python 实现,性能非热路径(单次毫秒-几十毫秒),不做耗时断言。
"""
from __future__ import annotations

import hashlib

import pytest

from netsentinel.security import ed25519

# ---------------------------------------------------------------------------
# RFC 8032 §7.1 官方测试向量 1-3(seed / 公钥 / 消息 / 签名全部逐字节锁定)
# ---------------------------------------------------------------------------

#: 官方向量原始数据(元组,便于测试内直接索引)
_VECTORS: list[tuple[str, str, str, str]] = [
    # (seed_hex, public_hex, message_hex, signature_hex)
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
]

RFC8032_VECTORS = [
    pytest.param(*vec, id=f"rfc8032-test{i + 1}") for i, vec in enumerate(_VECTORS)
]


@pytest.mark.parametrize("seed_hex, pub_hex, msg_hex, sig_hex", RFC8032_VECTORS)
def test_rfc8032_official_vectors(seed_hex: str, pub_hex: str, msg_hex: str, sig_hex: str) -> None:
    """RFC 8032 官方向量:公钥推导与签名输出逐字节等于标准答案。"""
    seed = bytes.fromhex(seed_hex)
    msg = bytes.fromhex(msg_hex)

    assert ed25519.public_key(seed).hex() == pub_hex
    pub, priv = ed25519.keypair(seed)
    assert pub.hex() == pub_hex
    assert priv == seed  # RFC 8032 口径:私钥即 seed
    sig = ed25519.sign(seed, msg)
    assert sig.hex() == sig_hex
    assert len(sig) == ed25519.SIGNATURE_BYTES == 64
    assert ed25519.verify(bytes.fromhex(pub_hex), msg, sig) is True


def test_rfc8032_all_three_vectors_are_distinct() -> None:
    """三组向量的 seed / 公钥 / 签名互不相同(防止参数表重复粘贴)。"""
    cols = list(zip(*_VECTORS))  # (seeds, pubs, msgs, sigs)
    for col in cols:
        assert len(set(col)) == 3


# ---------------------------------------------------------------------------
# 确定性与密钥派生
# ---------------------------------------------------------------------------


def test_seed_determinism() -> None:
    """同一 seed:密钥对与签名恒定;不同 seed:公钥与签名必不同。"""
    seed_a = bytes(range(32))
    seed_b = bytes(range(1, 33))
    msg = b"net-sentinel determinism probe"

    pub1, priv1 = ed25519.keypair(seed_a)
    pub2, priv2 = ed25519.keypair(seed_a)
    assert (pub1, priv1) == (pub2, priv2)

    sig1, sig2 = ed25519.sign(seed_a, msg), ed25519.sign(seed_a, msg)
    assert sig1 == sig2

    assert ed25519.public_key(seed_b) != pub1
    assert ed25519.sign(seed_b, msg) != sig1


def test_keypair_private_reusable_for_sign() -> None:
    """keypair 返回的私钥(seed)可原样再用于 sign,与公钥配对验签通过。"""
    seed = hashlib.sha256(b"netsentinel-seed").digest()
    pub, priv = ed25519.keypair(seed)
    assert priv == seed
    assert ed25519.verify(pub, b"m", ed25519.sign(priv, b"m")) is True


def test_public_key_length_is_32() -> None:
    assert len(ed25519.public_key(bytes(32))) == ed25519.PUBLIC_KEY_BYTES == 32


# ---------------------------------------------------------------------------
# 验签:通过 / 拒绝(篡改消息、篡改签名、错公钥)
# ---------------------------------------------------------------------------


def _fixture_pair() -> tuple[bytes, bytes]:
    """用官方向量 TEST 2 的 seed 构造 (公钥, seed) 固定对。"""
    seed = bytes.fromhex(_VECTORS[1][0])
    return ed25519.keypair(seed)[0], seed


def test_verify_roundtrip_arbitrary_message() -> None:
    """任意长消息(空 / 1B / 大于 SHA-512 分块)sign→verify 往返。"""
    pub, seed = _fixture_pair()
    for msg in (b"", b"x", b"a" * 1000, bytes(range(256)) * 8):
        sig = ed25519.sign(seed, msg)
        assert ed25519.verify(pub, msg, sig) is True


def test_tampered_message_rejected() -> None:
    """篡改消息(加一字节/换一字节)→ False,绝不抛错。"""
    pub, seed = _fixture_pair()
    msg = b"evidence-content"
    sig = ed25519.sign(seed, msg)
    assert ed25519.verify(pub, msg + b"!", sig) is False
    assert ed25519.verify(pub, b"Evidence-content", sig) is False


def test_tampered_signature_rejected() -> None:
    """篡改签名任一段(R 或 S 的首/尾字节翻转)→ False。"""
    pub, seed = _fixture_pair()
    msg = b"evidence-content"
    sig = bytearray(ed25519.sign(seed, msg))
    for pos in (0, 31, 32, 63):
        bad = bytearray(sig)
        bad[pos] ^= 0x01
        assert ed25519.verify(pub, msg, bytes(bad)) is False, f"翻转第 {pos} 字节应拒绝"


def test_wrong_public_key_rejected() -> None:
    """正确签名 + 别家公钥 → False。"""
    pub_a, seed_a = _fixture_pair()
    pub_b, _ = ed25519.keypair(bytes(range(1, 33)))
    sig = ed25519.sign(seed_a, b"evidence-content")
    assert ed25519.verify(pub_b, b"evidence-content", sig) is False
    assert ed25519.verify(pub_a, b"evidence-content", sig) is True


def test_non_point_public_key_rejected_as_false() -> None:
    """公钥是格式合法(32B)但不在曲线上的点 → False(不是异常)。"""
    _, seed = _fixture_pair()
    sig = ed25519.sign(seed, b"m")
    # 全 0xff:y ≥ p,必然解码失败
    assert ed25519.verify(b"\xff" * 32, b"m", sig) is False


def test_malleable_signature_high_s_rejected() -> None:
    """S ≥ L 的延展签名(验证通过签名的 S + L)→ False。"""
    from netsentinel.security.ed25519 import _L

    pub, seed = _fixture_pair()
    msg = b"malleability-probe"
    sig = ed25519.sign(seed, msg)
    s = int.from_bytes(sig[32:], "little")
    s_mal = s + _L
    assert s_mal.bit_length() <= 256  # 仍是 32 字节可编码
    malformed = sig[:32] + s_mal.to_bytes(32, "little")
    assert ed25519.verify(pub, msg, malformed) is False


# ---------------------------------------------------------------------------
# 防错:坏 seed / 坏签名格式 → ValueError;bytes 类型族
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_seed", [b"", b"short", b"x" * 31, b"x" * 33, bytes(64)])
def test_bad_seed_length_raises_valueerror(bad_seed: bytes) -> None:
    """非 32 字节 seed:keypair / public_key / sign 一律明确 ValueError。"""
    with pytest.raises(ValueError, match="32 字节"):
        ed25519.keypair(bad_seed)
    with pytest.raises(ValueError, match="32 字节"):
        ed25519.public_key(bad_seed)
    with pytest.raises(ValueError, match="32 字节"):
        ed25519.sign(bad_seed, b"m")


def test_non_bytes_seed_raises_valueerror() -> None:
    with pytest.raises(ValueError, match="字节类型"):
        ed25519.sign("0" * 32, b"m")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="字节类型"):
        ed25519.public_key(123)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_sig", [b"", b"short", b"s" * 63, b"s" * 65])
def test_bad_signature_format_raises_valueerror(bad_sig: bytes) -> None:
    """非 64 字节签名 → verify 明确 ValueError(坏格式与验签失败区分)。"""
    pub, _ = ed25519.keypair(bytes(32))
    with pytest.raises(ValueError, match="64 字节"):
        ed25519.verify(pub, b"m", bad_sig)


def test_bad_public_key_format_raises_valueerror() -> None:
    _, seed = ed25519.keypair(bytes(32))
    sig = ed25519.sign(seed, b"m")
    with pytest.raises(ValueError, match="32 字节"):
        ed25519.verify(b"\x01", b"m", sig)
    with pytest.raises(ValueError, match="字节类型"):
        ed25519.verify("public-key", b"m", sig)  # type: ignore[arg-type]


def test_verify_never_raises_on_cryptographic_failure() -> None:
    """数学验签失败只返回 False:全路径(错点/错消息/错签名)不抛任何异常。"""
    pub, seed = _fixture_pair()
    sig = ed25519.sign(seed, b"m")
    assert ed25519.verify(pub, b"other", sig) is False
    assert ed25519.verify(pub, b"m", bytes(64)) is False  # 格式合法的随机签名
    assert ed25519.verify(b"\x00" * 32, b"m", sig) is False


def test_bytearray_and_memoryview_accepted() -> None:
    """bytes/bytearray/memoryview 一视同仁(签名返回恒为 bytes)。"""
    pub, seed = _fixture_pair()
    sig = ed25519.sign(bytearray(seed), memoryview(b"m"))
    assert isinstance(sig, bytes)
    assert ed25519.verify(bytearray(pub), bytearray(b"m"), memoryview(sig)) is True
