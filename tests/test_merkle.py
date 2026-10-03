"""A190:netsentinel.security.merkle 单元测试(离线,纯 stdlib,零网络)。

覆盖:根确定性(空树/1 叶/2 叶/129 叶冻结向量 + 独立朴素参考实现逐规模
全量比对)、包含性证明与独立验证、任一叶 / 任一中间哈希 / 侧位 / 证明
结构被篡改即验证失败、checkpoint HMAC 签名与验签、并发 append 压力
(线程安全 + 叶序一致性)。

冻结向量由本模块实现的公开算法自算后固定(RFC6962 风格已知向量的
离线变体:Bitcoin 末位复制,无域分离前缀,见模块文档),同时在
每个用例里用**测试内独立重写的朴素实现**复核,杜绝"自己证自己"。
"""
from __future__ import annotations

import hashlib
import threading
from typing import Any

import pytest

from netsentinel.security.merkle import (
    DEFAULT_CHECKPOINT_INTERVAL,
    EMPTY_ROOT_HEX,
    HASH_ALGO,
    SIGN_ALGO,
    MerkleTree,
    canonical_bytes,
    sign_checkpoint,
    verify_checkpoint_signature,
    verify_proof,
)

KEY = b"unit-test-hmac-key-32-bytes-xxxxx"


def _ev(i: int) -> dict[str, Any]:
    """确定性测试事件(含中文 / 布尔 / 整数,排序键口径下字节恒定)。"""
    return {"seq": i, "text": f"事件-{i}", "ok": i % 2 == 0}


def _naive_root(leaves: list[bytes]) -> str:
    """独立参考实现:朴素逐层哈希(奇数层复制末位),与被测模块零共享代码。"""
    if not leaves:
        return EMPTY_ROOT_HEX
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])  # Bitcoin 风格末位复制
        level = [
            hashlib.sha256(level[k] + level[k + 1]).digest()
            for k in range(0, len(level), 2)
        ]
    return level[0].hex()


def _build(n: int, *, signing_key: bytes | None = None) -> tuple[MerkleTree, list[bytes]]:
    """构造 n 叶树并返回(树, 叶哈希字节序)。"""
    tree = MerkleTree(signing_key=signing_key)
    leaves = [bytes.fromhex(tree.append(_ev(i))) for i in range(n)]
    return tree, leaves


# ---------------------------------------------------------------------------
# 根确定性
# ---------------------------------------------------------------------------

def test_empty_tree_root_is_known_public_constant() -> None:
    """空树根 = SHA-256(b"") 公开常量(RFC6962 同款约定),且高度/叶数为 0。"""
    tree = MerkleTree()
    assert tree.root_hex() == EMPTY_ROOT_HEX
    assert EMPTY_ROOT_HEX == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert tree.leaf_count == 0
    assert tree.height == 0


@pytest.mark.parametrize(
    ("n", "frozen_root", "frozen_height"),
    [
        (1, "339375e02c7a753754053288da7d794b8bfc6d045dc383fa521832146d9c8f2e", 0),
        (2, "62273a07c547595a78fbe19a6bc39e4c654668fbb4e941a49d8d8d6cd53c561a", 1),
        (129, "82d13905b0aa2a5a2ea0c4f3342350ead39fc7673701764eaa2c6ece8d49ba2f", 8),
    ],
)
def test_root_frozen_known_vectors(n: int, frozen_root: str, frozen_height: int) -> None:
    """冻结已知向量:根与树高逐字节锁定(同时用朴素参考实现交叉复核)。"""
    tree, leaves = _build(n)
    assert tree.leaf_count == n
    assert tree.height == frozen_height
    assert tree.height == ((n - 1).bit_length() if n > 1 else 0)
    assert tree.root_hex() == frozen_root
    assert tree.root_hex() == _naive_root(leaves)


def test_root_incremental_matches_naive_for_every_size_0_to_140() -> None:
    """增量折叠根 == 朴素逐层根:对 n=0..140 每追加一片都全量比对。"""
    tree = MerkleTree()
    leaves: list[bytes] = []
    for n in range(141):
        if n > 0:
            leaves.append(bytes.fromhex(tree.append(_ev(n - 1))))
        assert tree.leaf_count == n
        assert tree.root_hex() == _naive_root(leaves), f"n={n} 增量根与朴素根不一致"


def test_root_deterministic_across_instances_and_orders() -> None:
    """同批事件喂两棵独立树根相同;根与喂入批次大小无关(append-only 语义)。"""
    events = [_ev(i) for i in range(37)]
    tree_a = MerkleTree()
    for e in events:
        tree_a.append(e)
    tree_b = MerkleTree()
    for e in events:
        tree_b.append(e)
    assert tree_a.root_hex() == tree_b.root_hex()

    tree_c = MerkleTree()  # 分批喂入(模拟多次运行累积)结果一致
    for e in events[:5]:
        tree_c.append(e)
    for e in events[5:]:
        tree_c.append(e)
    assert tree_c.root_hex() == tree_a.root_hex()


# ---------------------------------------------------------------------------
# 规范序列化
# ---------------------------------------------------------------------------

def test_canonical_bytes_sorted_compact_and_utf8() -> None:
    """规范序列化:键排序 + 紧凑分隔符 + 中文原样 UTF-8,与字段顺序无关。"""
    assert canonical_bytes({"b": 1, "a": "中"}) == b'{"a":"\xe4\xb8\xad","b":1}'
    assert canonical_bytes({"a": 1, "b": 2}) == canonical_bytes({"b": 2, "a": 1})
    with pytest.raises(TypeError):
        canonical_bytes(["not", "a", "mapping"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 包含性证明与验证
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n", [1, 2, 3, 5, 8, 17, 128, 129])
def test_include_proof_verifies_for_every_leaf(n: int) -> None:
    """每片叶的包含性证明都能被独立 verify_proof 验证通过。"""
    tree, _ = _build(n)
    root = tree.root_hex()
    for i in range(n):
        proof = tree.include_proof(i)
        assert len(proof) == tree.height
        assert verify_proof(tree.leaf_hex(i), proof, root) is True


def test_verify_proof_rejects_wrong_leaf_or_wrong_root() -> None:
    """换一片叶 / 换一个根,验证必须失败(防"证明挪用")。"""
    tree, _ = _build(9)
    root = tree.root_hex()
    proof = tree.include_proof(3)
    assert verify_proof(tree.leaf_hex(4), proof, root) is False  # 他人之叶
    assert verify_proof(tree.leaf_hex(3), proof, EMPTY_ROOT_HEX) is False


def test_verify_proof_rejects_any_tampered_step() -> None:
    """任一中间哈希 / 侧位被篡改即失败;截断 / 加长 / 结构坏也失败,绝不抛。"""
    tree, _ = _build(9)
    root = tree.root_hex()
    leaf = tree.leaf_hex(5)
    proof = tree.include_proof(5)
    assert verify_proof(leaf, proof, root) is True  # 基线通过

    for i in range(len(proof)):  # 篡改任一中间哈希(翻转一个 hex 字符)
        bad = [dict(s) for s in proof]
        ch = "0" if bad[i]["hash"][0] != "0" else "1"
        bad[i]["hash"] = ch + bad[i]["hash"][1:]
        assert verify_proof(leaf, bad, root) is False, f"篡改第 {i} 步哈希未被识破"

    for i in range(len(proof)):  # 篡改任一侧位 L<->R
        bad = [dict(s) for s in proof]
        bad[i]["side"] = "R" if bad[i]["side"] == "L" else "L"
        assert verify_proof(leaf, bad, root) is False, f"篡改第 {i} 步侧位未被识破"

    assert verify_proof(leaf, proof[:-1], root) is False  # 截断
    assert verify_proof(leaf, list(proof) + list(proof), root) is False  # 加长
    assert verify_proof(leaf, [], root) is False  # 空证明对非单叶树
    assert verify_proof(leaf, "not-a-list", root) is False  # type: ignore[arg-type]
    assert verify_proof(leaf, [{"hash": "zz", "side": "L"}], root) is False  # 坏 hex
    assert verify_proof(leaf, [{"hash": proof[0]["hash"]}], root) is False  # 缺 side
    assert verify_proof(leaf, [("nope", "L")], root) is False  # 非映射步
    assert verify_proof("not-hex", proof, root) is False  # 叶不是 hex
    assert verify_proof(leaf, proof, "not-hex") is False  # 根不是 hex


def test_tampered_event_changes_root_and_breaks_proof() -> None:
    """任一叶事件被篡改 → 叶哈希变 → 与已封根 / 原证明不再匹配(透明性核心)。"""
    tree, _ = _build(16)
    root = tree.root_hex()
    proof_7 = tree.include_proof(7)
    assert verify_proof(tree.leaf_hex(7), proof_7, root) is True  # 基线

    tampered = dict(_ev(7))
    tampered["text"] = "事件-7-被篡改"
    tampered_leaf = hashlib.sha256(canonical_bytes(tampered)).hexdigest()
    assert tampered_leaf != tree.leaf_hex(7)  # 篡改必然改变叶哈希
    assert verify_proof(tampered_leaf, proof_7, root) is False  # 旧根下无处容身

    tree.append(tampered)  # append-only:篡改只能以"再追加"形式进入
    assert tree.root_hex() != root  # 根必然移动,旧 checkpoint 不再覆盖新状态


def test_leaf_hex_accessor_and_bounds() -> None:
    """leaf_hex 越界给中文 IndexError;空树 include_proof 同样拒绝。"""
    tree, _ = _build(3)
    assert tree.leaf_hex(0) == tree.leaf_hex(0)
    with pytest.raises(IndexError, match="叶索引越界"):
        tree.leaf_hex(3)
    empty = MerkleTree()
    with pytest.raises(IndexError, match="叶索引越界"):
        empty.include_proof(0)


# ---------------------------------------------------------------------------
# checkpoint 签名
# ---------------------------------------------------------------------------

def test_checkpoint_signed_and_verified() -> None:
    """封根条目自描述且签名可独立验签;错钥 / 篡根 / 篡叶数全部拒绝。"""
    tree, _ = _build(130, signing_key=KEY)
    cp = tree.checkpoint()
    assert cp["algo"] == HASH_ALGO == "SHA-256"
    assert cp["sign_algo"] == SIGN_ALGO == "HMAC-SHA256"
    assert cp["root"] == tree.root_hex()
    assert cp["leaf_count"] == 130
    assert cp["height"] == 8
    assert cp["signed"] is True
    assert cp["signature"]

    assert verify_checkpoint_signature(cp["root"], cp["leaf_count"], cp["signature"], KEY)
    assert not verify_checkpoint_signature(
        cp["root"], cp["leaf_count"], cp["signature"], b"wrong-key"
    )
    # 篡改根 / 叶数后旧签名失效(签名同时覆盖根与叶数)
    assert not verify_checkpoint_signature(EMPTY_ROOT_HEX, cp["leaf_count"], cp["signature"], KEY)
    assert not verify_checkpoint_signature(cp["root"], cp["leaf_count"] + 1, cp["signature"], KEY)
    assert not verify_checkpoint_signature(cp["root"], cp["leaf_count"], "deadbeef", KEY)


def test_checkpoint_signature_defensive_against_bad_inputs() -> None:
    """验签对非法形态一律 False,绝不抛出。"""
    sig = sign_checkpoint(EMPTY_ROOT_HEX, 0, KEY)
    assert verify_checkpoint_signature(EMPTY_ROOT_HEX, 0, sig, KEY)
    assert not verify_checkpoint_signature(EMPTY_ROOT_HEX, 0, sig, b"")  # 空钥
    assert not verify_checkpoint_signature(EMPTY_ROOT_HEX, 0, sig, "str-key")  # type: ignore[arg-type]
    assert not verify_checkpoint_signature(None, 0, sig, KEY)  # type: ignore[arg-type]
    assert not verify_checkpoint_signature(EMPTY_ROOT_HEX, "3", sig, KEY)  # type: ignore[arg-type]
    assert not verify_checkpoint_signature(EMPTY_ROOT_HEX, True, sig, KEY)  # type: ignore[arg-type]
    assert not verify_checkpoint_signature(EMPTY_ROOT_HEX, 0, None, KEY)  # type: ignore[arg-type]


def test_unsigned_checkpoint_when_no_key() -> None:
    """未注入密钥:封根仍可用(透明性离线复核),但 signature 为空且 signed=False。"""
    tree, _ = _build(5)
    cp = tree.checkpoint()
    assert cp["signature"] == ""
    assert cp["signed"] is False
    assert cp["root"] == tree.root_hex()


def test_constructor_validation_and_defaults() -> None:
    """非法间隔 / 非法密钥早失败(中文报错);默认间隔 128。"""
    assert DEFAULT_CHECKPOINT_INTERVAL == 128
    with pytest.raises(ValueError, match="正整数"):
        MerkleTree(checkpoint_interval=0)
    with pytest.raises(ValueError, match="正整数"):
        MerkleTree(checkpoint_interval=-8)
    with pytest.raises(TypeError):
        MerkleTree(checkpoint_interval="128")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="signing_key"):
        MerkleTree(signing_key=b"")
    with pytest.raises(ValueError, match="signing_key"):
        MerkleTree(signing_key="not-bytes")  # type: ignore[arg-type]
    assert MerkleTree().checkpoint_interval == 128


# ---------------------------------------------------------------------------
# 并发 append 压力(小样本)
# ---------------------------------------------------------------------------

def test_concurrent_append_thread_safety() -> None:
    """8 线程 × 32 事件并发追加:叶数 / 叶集合 / 每叶证明 / 根一致性全对账。"""
    tree = MerkleTree(signing_key=KEY)
    threads_n, per_thread = 8, 32
    total = threads_n * per_thread
    returned: list[str] = []
    returned_lock = threading.Lock()
    errors: list[BaseException] = []

    def worker(t: int) -> None:
        local: list[str] = []
        try:
            for k in range(per_thread):
                local.append(tree.append({"thread": t, "k": k, "text": f"并发-{t}-{k}"}))
        except BaseException as exc:  # noqa: BLE001 - 线程内异常带回主线程断言
            errors.append(exc)
        with returned_lock:
            returned.extend(local)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(threads_n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert errors == []
    assert tree.leaf_count == total
    assert len(returned) == total
    assert len(set(returned)) == total  # 无重复叶(哈希无碰撞 / 无丢失)
    on_tree = {tree.leaf_hex(i) for i in range(total)}
    assert on_tree == set(returned)  # 返回值与树内叶集合一致

    root = tree.root_hex()
    for i in range(total):  # 每片叶(无论哪个线程追加)证明均可验证
        assert verify_proof(tree.leaf_hex(i), tree.include_proof(i), root), f"叶 {i}"
    assert root == _naive_root([bytes.fromhex(tree.leaf_hex(i)) for i in range(total)])
    assert verify_checkpoint_signature(root, total, tree.checkpoint()["signature"], KEY)
