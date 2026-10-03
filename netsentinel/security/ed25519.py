"""纯 Python Ed25519 签名(RFC 8032,零第三方依赖)。

背景(A192 第三方可信证据):证据包原先只用 HMAC-SHA256 对称签名,
验证方与签发方共享同一密钥——司法举证时"谁都能自造签名"是效力短板。
本模块按 RFC 8032 §5.1 以**纯标准库**实现 Ed25519 数字签名
(SHA-512 用 :mod:`hashlib`,素域标量运算用 Python 大整数),
签发方持 32 字节 seed,验证方只需 32 字节公钥,对标 DSSE / in-toto
provenance 签名实践,补齐非对称可信证据差距。

实现要点:

- 曲线:edwards25519(twisted Edwards)``-x^2 + y^2 = 1 + d*x^2*y^2``,
  ``p = 2^255 - 19``,群阶 ``L = 2^252 + 27742317777372353535851937790883648493``;
- 点运算用扩展齐次坐标 (X, Y, Z, T),避免模逆;仅编码/解码时做一次
  :func:`pow` 模逆(Fermat 小定理,指数 p-2);
- 标量乘为朴素双加(double-and-add),**非抗侧信道**——本模块只用于
  签名(私钥在本地),不用于处理攻击者可控的机密;性能非热路径:
  CPython 上单次 sign/verify 约几毫秒到几十毫秒量级,完全可接受
  (每证据包只在打包落签 / 核验时各调用一次);
- 确定性签名(RFC 8032 §5.1.6):同一 seed 对同一消息签名恒等,
  便于测试固定种子断言。

防错口径:

- ``keypair`` / ``public_key`` / ``sign``:seed 非 32 字节 bytes → ``ValueError``;
- ``verify``:公钥非 32 字节 / 签名非 64 字节 / 参数类型不对 → ``ValueError``
  (坏格式明确报错);格式正确但数学验签不通过 → 返回 ``False``,
  **绝不抛错**(验签失败是常态输入,不是异常)。

只用标准库,绝不联网;所有用户可见文案均为中文。

用法::

    from netsentinel.security import ed25519

    seed = bytes(range(32))                     # 32 字节
    pub, priv = ed25519.keypair(seed)           # priv == seed(RFC 8032 口径)
    sig = ed25519.sign(priv, b"证据内容")
    ed25519.verify(pub, b"证据内容", sig)       # True
    ed25519.verify(pub, b"被篡改的内容", sig)   # False
"""
from __future__ import annotations

import hashlib

__all__ = [
    "keypair",
    "public_key",
    "sign",
    "verify",
    "SEED_BYTES",
    "PUBLIC_KEY_BYTES",
    "SIGNATURE_BYTES",
]

#: 私钥 seed 字节数(RFC 8032 固定 32)
SEED_BYTES = 32

#: 公钥字节数(小端 y 坐标 255 bit + x 符号位 1 bit)
PUBLIC_KEY_BYTES = 32

#: 签名字节数(R ‖ S,各 32 字节)
SIGNATURE_BYTES = 64

# ---------------------------------------------------------------------------
# edwards25519 域参数(RFC 8032 §5.1)
# ---------------------------------------------------------------------------

#: 素域模数 p = 2^255 - 19
_P = 2**255 - 19

#: 基点群阶 L
_L = 2**252 + 27742317777372353535851937790883648493

#: 曲线常数 d = -121665/121666 mod p
_D = (-121665 * pow(121666, _P - 2, _P)) % _P

#: sqrt(-1) mod p(p ≡ 5 mod 8,由 2^((p-1)/4) 给出)
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _inv_p(x: int) -> int:
    """模 p 求逆(Fermat 小定理:x^(p-2) mod p;x 非 0 时恒成立)。"""
    return pow(x, _P - 2, _P)


def _recover_x(y: int, sign: int) -> int | None:
    """由 y 坐标(与符号位)恢复曲线上的 x;无解 → None(RFC 8032 §5.1.3)。"""
    if y >= _P:
        return None
    xx = (y * y - 1) * _inv_p(_D * y * y + 1) % _P
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:  # 不是平方剩余:补乘 sqrt(-1) 再试
        x = x * _SQRT_M1 % _P
    if (x * x - xx) % _P != 0:  # 仍非平方剩余:y 不在曲线上
        return None
    if x % 2 != sign:  # 符号位对不上:取 -x
        x = _P - x
    return x


def _base_point() -> list[int]:
    """基点 B(扩展齐次坐标);y = 4/5,x 由恢复算法确定(恒取偶值)。"""
    by = 4 * _inv_p(5) % _P
    bx = _recover_x(by, 0)
    if bx is None:  # pragma: no cover - 基点是构造期常数,不可能失败
        raise RuntimeError("edwards25519 基点推导失败:实现存在缺陷")
    return [bx, by, 1, bx * by % _P]


#: 基点 B(扩展齐次坐标 (X, Y, Z, T))
_B = _base_point()

#: 单位元 (0, 1, 1, 0)
_IDENT = [0, 1, 1, 0]


# ---------------------------------------------------------------------------
# 点运算:扩展齐次坐标下的加法与标量乘
# ---------------------------------------------------------------------------


def _point_add(p: list[int], q: list[int]) -> list[int]:
    """edwards25519 点加(RFC 8032 §5.1.4,扩展齐次坐标,免模逆)。"""
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return [e * f % _P, g * h % _P, f * g % _P, e * h % _P]


def _scalar_mult(point: list[int], scalar: int) -> list[int]:
    """双加法标量乘(scalar ≥ 0);朴素实现,非热路径。"""
    result = list(_IDENT)
    addend = list(point)
    while scalar > 0:
        if scalar & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        scalar >>= 1
    return result


def _point_encode(point: list[int]) -> bytes:
    """点编码为 32 字节:仿射 y(255 bit 小端)+ 仿射 x 最低位(符号位)。"""
    z_inv = _inv_p(point[2])
    x = point[0] * z_inv % _P
    y = point[1] * z_inv % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _point_decode(data: bytes) -> list[int] | None:
    """32 字节解码为曲线点;长度坏 / 不在曲线上 → None(供 verify 判 False)。"""
    val = int.from_bytes(data, "little")
    sign = (val >> 255) & 1
    y = val & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    if x is None:
        return None
    point = [x, y, 1, x * y % _P]
    # 在曲线校验(防御性,理论上 _recover_x 已保证):-x^2+y^2 ≡ 1+d*x^2*y^2
    if (y * y - x * x - 1 - _D * x * x * y * y) % _P != 0:
        return None
    return point


def _sha512_int(*parts: bytes) -> int:
    """SHA-512 拼接摘要并按小端解释为非负整数(RFC 8032 的 Hint 函数)。"""
    digest = hashlib.sha512()
    for part in parts:
        digest.update(part)
    return int.from_bytes(digest.digest(), "little")


def _clamped_scalar(h32: bytes) -> int:
    """把 seed 哈希前 32 字节钳制为标量 a(RFC 8032 §5.1.5)。"""
    a = int.from_bytes(h32, "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a


# ---------------------------------------------------------------------------
# 输入规整与防错
# ---------------------------------------------------------------------------


def _as_bytes(value: object, name: str) -> bytes:
    """bytes/bytearray/memoryview → bytes;其他类型 → ValueError(中文提示)。"""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    raise ValueError(f"Ed25519 {name} 必须是 bytes 等字节类型,收到 {type(value).__name__}")


def _require_seed(seed: bytes) -> bytes:
    """校验私钥 seed:必须是 32 字节字节串,否则 ValueError。"""
    data = _as_bytes(seed, "私钥 seed")
    if len(data) != SEED_BYTES:
        raise ValueError(
            f"Ed25519 私钥 seed 必须是 {SEED_BYTES} 字节,收到 {len(data)} 字节"
        )
    return data


# ---------------------------------------------------------------------------
# 公开 API:keypair / public_key / sign / verify
# ---------------------------------------------------------------------------


def public_key(seed: bytes) -> bytes:
    """由 32 字节 seed 推导 32 字节公钥(RFC 8032 §5.1.5)。

    h = SHA-512(seed);a = clamp(h[0:32]);公钥 = encode(a·B)。
    """
    data = _require_seed(seed)
    h = hashlib.sha512(data).digest()
    a = _clamped_scalar(h[:32])
    return _point_encode(_scalar_mult(_B, a))


def keypair(seed: bytes) -> tuple[bytes, bytes]:
    """由 32 字节 seed 生成 ``(公钥, 私钥)``;私钥即 seed 本身(RFC 8032 口径)。

    与 :func:`public_key` 的区别只是同时返回 seed,便于上层把
    ``(pub, priv)`` 作为一个整体传递;确定性:同 seed 恒得同密钥对。
    """
    data = _require_seed(seed)
    return public_key(data), data


def sign(seed: bytes, message: bytes) -> bytes:
    """确定性 Ed25519 签名(RFC 8032 §5.1.6),返回 64 字节 R ‖ S。

    h = SHA-512(seed);a = clamp(h[:32]);prefix = h[32:64];
    r = SHA-512(prefix ‖ M) mod L;R = encode(r·B);
    k = SHA-512(R ‖ A ‖ M) mod L;S = (r + k·a) mod L。
    """
    data = _require_seed(seed)
    msg = _as_bytes(message, "待签消息")
    h = hashlib.sha512(data).digest()
    a = _clamped_scalar(h[:32])
    prefix = h[32:64]
    pub = _point_encode(_scalar_mult(_B, a))

    r = _sha512_int(prefix, msg) % _L
    r_point = _point_encode(_scalar_mult(_B, r))
    k = _sha512_int(r_point, pub, msg) % _L
    s = (r + k * a) % _L
    return r_point + s.to_bytes(32, "little")


def verify(pub: bytes, message: bytes, signature: bytes) -> bool:
    """Ed25519 验签(RFC 8032 §5.1.7):通过 → True,不通过 → False 不抛错。

    坏格式(公钥非 32 字节 / 签名非 64 字节 / 类型不对)→ ``ValueError``;
    格式正确但数学验签失败(篡改消息、篡改签名、公钥不符、S ≥ L、
    点不在曲线上)→ 一律返回 ``False``。
    """
    pk = _as_bytes(pub, "公钥")
    msg = _as_bytes(message, "待验消息")
    sig = _as_bytes(signature, "签名")
    if len(pk) != PUBLIC_KEY_BYTES:
        raise ValueError(f"Ed25519 公钥必须是 {PUBLIC_KEY_BYTES} 字节,收到 {len(pk)} 字节")
    if len(sig) != SIGNATURE_BYTES:
        raise ValueError(
            f"Ed25519 签名必须是 {SIGNATURE_BYTES} 字节,收到 {len(sig)} 字节"
        )

    a_point = _point_decode(pk)
    if a_point is None:  # 公钥不是曲线点 → 验签失败(False,不是异常)
        return False
    r_bytes = sig[:32]
    r_point = _point_decode(r_bytes)
    if r_point is None:  # R 不是曲线点
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= _L:  # 标量越界(malleability 拒绝)
        return False

    k = _sha512_int(r_bytes, pk, msg) % _L
    left = _scalar_mult(_B, s)  # s·B
    right = _point_add(r_point, _scalar_mult(a_point, k))  # R + k·A
    return _point_encode(left) == _point_encode(right)
