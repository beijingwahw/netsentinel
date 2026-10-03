"""离线 Merkle 审计透明日志(A190,纯 stdlib:hashlib/hmac/json/threading)。

思想对标 sigstore/rekor 透明日志(append-only、可独立验证的包含性证明、
定期 checkpoint 封根签名),做**离线优先**变体:不依赖在线见证人 / gossip,
checkpoint 由本地 HMAC-SHA256 密钥签名;密钥经构造函数注入 ``bytes``,
调用方自行从 :mod:`netsentinel.security.keys`(如 ``get_key``)或
``bundle_sign`` 体系的密钥字节获取——本模块只读引用其思想,不 import
那些模块,保持零依赖、可独立审计。

树的结构约定(与 RFC6962 的差异必须知情):

- **叶** = 事件字节的规范序列化的 SHA-256:排序键 JSON
  (``sort_keys=True``、紧凑分隔符、``ensure_ascii=False``)再 UTF-8;
- **内部节点** = 左右子哈希**字节直接拼接**后的 SHA-256(与 RFC6962
  不同,不加 ``0x00``/``0x01`` 前缀域分离;本模块 API 只接受 Mapping
  事件作为叶,内部节点哈希不会回流为叶,二次原像风险面因此收窄;
  若未来需要严格域分离,升级 ``_leaf_hash`` 加前缀即可,但根值会变,
  需按 ``algo`` 字段版本化);
- **奇数叶复制提升(Bitcoin 风格)**:每层末尾未配对的节点复制自身
  参与上一层配对(仅复制"末尾未配对"节点,是 CVE-2012-2459 之后的
  定型做法,不存在任意复制的多根歧义)。含该复制兄弟的包含性证明
  同样可验证。

线程安全:``append`` 与读路径(``root_hex``/``include_proof`` 等)由
内部锁保护,读走快照,绝不阻塞并发追加;上层(如
:class:`~netsentinel.logging_util.JsonlAuditLogger`)可用自己的锁把
"追加事件 + 写日志行"原子化,保证叶序与落盘行序一致。

用法示例::

    from netsentinel.security.merkle import MerkleTree, verify_proof

    tree = MerkleTree(signing_key=b"...", checkpoint_interval=128)
    leaf0 = tree.append({"event": "human_confirmed", "verdict": "nsfw"})
    tree.append({"event": "submitted", "case": "C-01"})

    root = tree.root_hex()                       # O(log n) 增量折叠
    proof = tree.include_proof(0)                # [{"hash": ..., "side": "L|R"}]
    verify_proof(leaf0, proof, root)             # True(独立函数,零内部状态)

    cp = tree.checkpoint()                       # 封根 + HMAC-SHA256 签名
    # cp == {"algo": ..., "root": ..., "leaf_count": ..., "height": ...,
    #        "sign_algo": ..., "signature": ..., "signed": True}
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "DEFAULT_CHECKPOINT_INTERVAL",
    "EMPTY_ROOT_HEX",
    "HASH_ALGO",
    "SIGN_ALGO",
    "MerkleTree",
    "canonical_bytes",
    "sign_checkpoint",
    "verify_checkpoint_signature",
    "verify_proof",
]

#: 哈希算法标识(写入 checkpoint,供验签方版本化识别)
HASH_ALGO = "SHA-256"

#: checkpoint 签名算法标识
SIGN_ALGO = "HMAC-SHA256"

#: 默认每多少片叶封一次根(可配,构造函数注入)
DEFAULT_CHECKPOINT_INTERVAL = 128

#: 空树的根:SHA-256(b"") 十六进制(RFC6962 同款约定,已知公开向量)
EMPTY_ROOT_HEX = hashlib.sha256(b"").hexdigest()


def canonical_bytes(event: Mapping[str, Any]) -> bytes:
    """事件的规范序列化字节:排序键 + 紧凑分隔符 + 非 ASCII 原样的 UTF-8。

    与 :mod:`netsentinel.storage` 哈希链的口径一致(``sort_keys=True``、
    ``ensure_ascii=False``,中文原样参与哈希);额外收紧分隔符
    (``(",", ":")``)使同一条事件在任何字段顺序 / 解释器下字节恒定。
    不可 JSON 序列化的值以 ``str()`` 兜底,序列化永不因奇异值失败。
    """
    if not isinstance(event, Mapping):
        raise TypeError(f"事件应为 Mapping(如 dict),当前为 {type(event).__name__}")
    text = json.dumps(
        dict(event),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return text.encode("utf-8")


def _leaf_hash(event: Mapping[str, Any]) -> bytes:
    """叶哈希:规范序列化字节的 SHA-256(原始 32 字节,内部以 bytes 参与)。"""
    return hashlib.sha256(canonical_bytes(event)).digest()


def _node_hash(left: bytes, right: bytes) -> bytes:
    """内部节点哈希:子哈希字节直接拼接后 SHA-256(无前缀,见模块文档)。"""
    return hashlib.sha256(left + right).digest()


def sign_checkpoint(root_hex: str, leaf_count: int, key: bytes) -> str:
    """对 checkpoint 签 HMAC-SHA256:消息为 ``{"leaf_count", "root"}`` 规范 JSON。

    签名同时覆盖根与叶数,防"换根重放"(同一根在不同叶数语境下冒充)。
    ``key`` 为调用方注入的原始字节(如来自 keys/bundle_sign 体系)。
    """
    payload = json.dumps(
        {"leaf_count": int(leaf_count), "root": str(root_hex)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_checkpoint_signature(
    root_hex: str, leaf_count: int, signature_hex: str, key: bytes
) -> bool:
    """独立验证 checkpoint 签名;任何入参形态非法都返回 False,绝不抛出。"""
    if not isinstance(key, (bytes, bytearray)) or not key:
        return False
    if not isinstance(root_hex, str) or not isinstance(signature_hex, str):
        return False
    if not isinstance(leaf_count, int) or isinstance(leaf_count, bool):
        return False
    try:
        expected = sign_checkpoint(root_hex, leaf_count, bytes(key))
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, signature_hex)


def verify_proof(
    leaf_hex: str, proof: Sequence[Mapping[str, Any]], root_hex: str
) -> bool:
    """独立验证包含性证明:从叶哈希沿审计路径折叠,与根做常数时间比对。

    ``proof`` 为 :meth:`MerkleTree.include_proof` 的返回形态,每步为
    ``{"hash": <hex>, "side": "L"|"R"}``(``L`` = 兄弟在左)。任何形态
    非法(坏 hex、坏 side、非映射步、非序列)都返回 False,绝不抛出,
    便于直接用于外部(如日志文件)送来的证明数据。
    """
    if not isinstance(leaf_hex, str) or not isinstance(root_hex, str):
        return False
    if not isinstance(proof, (list, tuple)):
        return False
    try:
        current = bytes.fromhex(leaf_hex)
        root = bytes.fromhex(root_hex)
    except ValueError:
        return False
    for step in proof:
        if not isinstance(step, Mapping):
            return False
        try:
            sibling = bytes.fromhex(step["hash"])
        except (KeyError, TypeError, ValueError):
            return False
        side = step.get("side")
        if side == "L":
            current = _node_hash(sibling, current)
        elif side == "R":
            current = _node_hash(current, sibling)
        else:
            return False
    return hmac.compare_digest(current, root)


def _fold_root(levels: list[list[bytes]]) -> bytes:
    """O(log n) 增量根折叠(私有):沿"未配对右脊"自底向上合并。

    ``levels`` 为本模块维护的实际层级(每层只存真实成对父节点,不含
    复制提升出的虚拟节点);``carry`` 是从下层带上来的虚拟节点(可能为
    ``None``)。折叠规则与朴素逐层算法(奇数补末叶复制)等价,由
    tests/test_merkle.py 对 n=0..140 逐规模全量比对锁定。
    """
    carry: bytes | None = None
    for level in levels:
        count = len(level) + (1 if carry is not None else 0)
        if count == 1:  # 该层单独成根(单叶树 / 恰好收敛)
            return level[0] if carry is None else carry
        if count % 2 == 0:
            # 实际层为奇数(末节点未配对)且带 carry:两者配对上提;
            # 实际层为偶数且无 carry:全部已在上层物化,无事可做
            carry = _node_hash(level[-1], carry) if carry is not None else None
        else:
            trailing = carry if carry is not None else level[-1]
            carry = _node_hash(trailing, trailing)  # Bitcoin 末位复制提升
    if carry is None:  # pragma: no cover - 结构不变量被破坏时的防御
        raise RuntimeError("Merkle 根折叠未收敛:内部层级结构不一致")
    return carry


def _virtual_levels(levels: list[list[bytes]]) -> list[list[bytes]]:
    """把实际层级物化为朴素逐层虚拟层级(含复制提升节点),末层即单节点根。

    只在生成包含性证明时调用,代价 O(高度 + 首层拷贝);规则自底向上:
    虚拟层为奇数 → 末节点复制提升;虚拟层为偶数且含虚拟节点 → 末两位
    (真实未配对节点 + 虚拟节点)配对上提;纯真实偶数层 → 全部已物化。
    """
    out: list[list[bytes]] = []
    cur = list(levels[0])
    carried = False
    i = 0
    while True:
        if not cur:  # pragma: no cover - 结构不变量被破坏时的防御
            raise RuntimeError("Merkle 虚拟层级为空:内部层级结构不一致")
        out.append(cur)
        if len(cur) == 1:
            return out
        nxt = list(levels[i + 1]) if i + 1 < len(levels) else []
        if len(cur) % 2:
            nxt.append(_node_hash(cur[-1], cur[-1]))
            carried = True
        elif carried:
            nxt.append(_node_hash(cur[-2], cur[-1]))
        else:
            carried = False
        cur = nxt
        i += 1


class MerkleTree:
    """append-only Merkle 树:追加 O(log n)、根 O(log n)、证明 O(树高)。

    参数:``signing_key`` 为 HMAC 签名密钥字节(可选;不注入则
    :meth:`checkpoint` 产出未签名条目,``signed=False``);
    ``checkpoint_interval`` 为建议封根间隔(默认 128,仅作为元数据
    透出,由调用方——如 JsonlAuditLogger——决定落盘点)。内部层级只
    存真实成对节点,复制提升仅发生在根折叠与证明生成的虚拟物化中。
    """

    def __init__(
        self,
        *,
        signing_key: bytes | bytearray | None = None,
        checkpoint_interval: int = DEFAULT_CHECKPOINT_INTERVAL,
    ) -> None:
        if not isinstance(checkpoint_interval, int) or isinstance(
            checkpoint_interval, bool
        ):
            raise TypeError(
                f"checkpoint_interval 应为整数,当前为 {type(checkpoint_interval).__name__}"
            )
        if checkpoint_interval < 1:
            raise ValueError(f"checkpoint_interval 应为正整数,当前为 {checkpoint_interval!r}")
        if signing_key is not None and (
            not isinstance(signing_key, (bytes, bytearray)) or len(signing_key) == 0
        ):
            raise ValueError("signing_key 需为非空 bytes/bytearray(由调用方注入)")
        self.checkpoint_interval = int(checkpoint_interval)
        self._signing_key = bytes(signing_key) if signing_key is not None else None
        self._levels: list[list[bytes]] = []
        self._lock = threading.Lock()

    # -- 读属性 -----------------------------------------------------------

    @property
    def leaf_count(self) -> int:
        """当前叶数(= 追加事件数)。"""
        with self._lock:
            return len(self._levels[0]) if self._levels else 0

    @property
    def height(self) -> int:
        """树高(根到叶的边数):空树 0,单叶 0,n 叶 = (n-1).bit_length()。"""
        n = self.leaf_count
        return (n - 1).bit_length() if n > 0 else 0

    def leaf_hex(self, index: int) -> str:
        """第 ``index`` 片叶的十六进制哈希(供审计方重建 / 核对叶序)。"""
        with self._lock:
            total = len(self._levels[0]) if self._levels else 0
            if not 0 <= index < total:
                raise IndexError(f"叶索引越界:应为 0 <= index < {total},当前 index={index!r}")
            return self._levels[0][index].hex()

    def _snapshot(self) -> list[list[bytes]]:
        """锁内拷贝层级快照,读侧计算全部基于快照,不阻塞追加。"""
        with self._lock:
            return [list(level) for level in self._levels]

    # -- 写路径 -----------------------------------------------------------

    def append(self, event: Mapping[str, Any]) -> str:
        """追加一个事件(append-only),返回其叶哈希 hex。

        事件先做规范序列化再哈希(见 :func:`canonical_bytes`);追加在
        锁内完成层级级联(每成对一次即物化一个父节点),线程安全。
        """
        leaf = _leaf_hash(event)
        with self._lock:
            if not self._levels:
                self._levels.append([leaf])
                return leaf.hex()
            self._levels[0].append(leaf)
            i = 0
            while len(self._levels[i]) % 2 == 0:
                if i + 1 == len(self._levels):
                    self._levels.append([])
                level = self._levels[i]
                self._levels[i + 1].append(_node_hash(level[-2], level[-1]))
                i += 1
        return leaf.hex()

    # -- 透明性读路径 ------------------------------------------------------

    def root_hex(self) -> str:
        """当前根(hex);空树返回 :data:`EMPTY_ROOT_HEX`(已知公开向量)。"""
        snap = self._snapshot()
        if not snap:
            return EMPTY_ROOT_HEX
        return _fold_root(snap).hex()

    def include_proof(self, index: int) -> list[dict[str, str]]:
        """生成第 ``index`` 片叶的包含性证明(审计路径)。

        返回 ``[{"hash": <兄弟 hex>, "side": "L"|"R"}, ...]``,自叶向根
        逐层排列;Bitcoin 末位复制情形以兄弟=自身(side 恒为 R)表达,
        与 :func:`verify_proof` 的折叠口径一致。
        """
        snap = self._snapshot()
        total = len(snap[0]) if snap else 0
        if not 0 <= index < total:
            raise IndexError(f"叶索引越界:应为 0 <= index < {total},当前 index={index!r}")
        virtual = _virtual_levels(snap)
        path: list[dict[str, str]] = []
        j = index
        for i in range(len(virtual) - 1):
            cur = virtual[i]
            sib_idx = j ^ 1
            if sib_idx < len(cur):
                path.append(
                    {"hash": cur[sib_idx].hex(), "side": "L" if sib_idx < j else "R"}
                )
            else:  # 末位复制提升:兄弟即自身(Bitcoin 风格,见模块文档)
                path.append({"hash": cur[j].hex(), "side": "R"})
            j >>= 1
        return path

    def checkpoint(self) -> dict[str, Any]:
        """封根:返回含根 / 叶数 / 树高 / 签名的自描述 checkpoint 字典。

        有 ``signing_key`` 时 ``signature`` 为 HMAC-SHA256(hex,覆盖
        根与叶数)且 ``signed=True``;无密钥时 ``signature=""``、
        ``signed=False``(透明性仍可离线复核,只是不可防抵赖)。
        """
        root = self.root_hex()
        count = self.leaf_count
        cp: dict[str, Any] = {
            "algo": HASH_ALGO,
            "root": root,
            "leaf_count": count,
            "height": self.height,
            "sign_algo": SIGN_ALGO,
        }
        if self._signing_key is not None:
            cp["signature"] = sign_checkpoint(root, count, self._signing_key)
            cp["signed"] = True
        else:
            cp["signature"] = ""
            cp["signed"] = False
        return cp
