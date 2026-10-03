"""A127 netsentinel.intel.phash_lsh 分带 LSH 检索内核测试(A195 增多表)。

纯离线、确定性(固定种子)、不依赖墙钟(红线 31:以操作计数断言代差)。
覆盖:

- 带布局:默认 4 带 × 16bit 桶键的精确 int 切片;bands=1 / 3(非整除)/ 8
  边界;
- insert / query 往返:精确命中、max_distance 过滤、(距离, 插入序) 排序、
  多带候选去重(compare_calls 只按条目计一次);
- 召回:d ≤ bands-1 距离域内与暴力全表扫描 100% 一致(含 bands=8 的
  d≤4、默认 4 带的 d≤3 与"限定 3 带内翻 4 位"构造);每带各翻一位的
  d=4 已知漏检用例(结构性边界,docstring 已声明);
- 远距(>12,构造性保证)零命中;
- 校验:非法 hex / 长度 / bands / max_distance → 中文 ValueError;
- stats(空 / 精确计数 / 重复插入)、空索引;
- build_from 互操作:真 PhashRegistry(合成 hex + 真 PNG 图)、脏行跳过、
  多次调用累加、非 Registry 类型拒绝;与 find_similar 口径一致;
- 并发 insert / query;
- bench(红线 31):10^3 条随机指纹索引上单查询 compare_calls ≤ 全表
  10%,且命中集合与全表扫描完全一致;kernel_selfcheck / 遥测计数。

A195 增量:MultiTableLSH 多表 multi-probe LSH——

- 构造校验 / 置换确定性(同种子同桶布局,异种子异布局)/ 探针掩码集;
- insert / query 基础语义(排序、去重计数、负载身份、归一化、越界);
- 召回:d ≤ 2·probe_depth 可证域内与暴力扫描 100% 一致;probe_depth=0
  (multi-probe 关)与开启的召回对照;单表结构性漏检用例被多表找回;
- sqlite 持久化:flush 惰性加载往返、损坏重建、参数指纹不匹配重算、
  非 JSON 负载拒绝;
- 规模 bench(Random(42) 的 10^4 指纹 + d=8/12/16 已知近邻对):d=16
  召回 > 99% 且单查询比较次数(含最大值)< 全表 1%,旧单表同场景召回
  显著低(对比断言);K/probe 参数扫描的召回-成本权衡数据点;
- 并发、stats、遥测计数、multitable_selfcheck 契约。

A229 增量:MultiTableLSH ``name`` 实例标识泛化——缺省兼容(旧签名 /
stats 形状 / 桶布局零变化)、name 不进参数指纹;主 phash 与 mirror
两实例并存时按各自 db_path 独立落盘、重载后零串扰(多哈希生产接线
的 LSH 前提,消费方见 kernel_wire 的 mirror 源建边)。
"""
from __future__ import annotations

import random
import sqlite3
import threading
from typing import Any

import pytest

from netsentinel.intel.phash import PhashRegistry, hamming
from netsentinel.intel.phash_lsh import (
    BITS,
    DEFAULT_BANDS,
    DEFAULT_KEY_BITS,
    DEFAULT_PROBE_DEPTH,
    DEFAULT_TABLES,
    LSHIndex,
    MultiTableLSH,
    kernel_selfcheck,
    multitable_selfcheck,
)

SITE_A = "https://bad-a.example/gallery/"
SITE_B = "https://mirror-b.example/pics/"

H0 = "0" * 16
H1 = "0000000000000001"     # 距 H0 = 1
H2 = "0000000000000003"     # 距 H0 = 2
H8 = "00000000000000ff"     # 距 H0 = 8
H9 = "00000000000001ff"     # 距 H0 = 9
H_FAR = "f" * 16            # 距 H0 = 64
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def _rand_fp(rng: random.Random) -> str:
    """随机 64bit 指纹(16 位 hex)。"""
    return f"{rng.getrandbits(BITS):016x}"


def _flip(hex64: str, positions: list[int]) -> str:
    """翻转 hex64 指定二进制位(0 = 最低位),返回新 hex。"""
    value = int(hex64, 16)
    for pos in positions:
        value ^= 1 << pos
    return f"{value:016x}"


def _brute_force_ids(
    entries: list[tuple[str, int]], query: str, max_distance: int
) -> list[int]:
    """暴力全表扫描基准:返回距离 ≤ max_distance 的负载 id(升序)。"""
    return sorted(
        seq
        for hex64, seq in entries
        if hamming(query, hex64) <= max_distance
    )


def _assert_lsh_matches_bruteforce(
    index: LSHIndex,
    entries: list[tuple[str, int]],
    query: str,
    max_distance: int,
) -> list[int]:
    """断言 LSH 查询与暴力扫描结果集合一致(去重、序不敏感)。"""
    hits = index.query(query, max_distance=max_distance)
    brute = _brute_force_ids(entries, query, max_distance)
    assert sorted(hits) == brute, (
        f"LSH 命中 {sorted(hits)} 与全表扫描 {brute} 不一致"
        f"(query={query}, max_distance={max_distance})"
    )
    return brute


# ---------------------------------------------------------------------------
# 带布局与边界
# ---------------------------------------------------------------------------


def test_default_bands_layout_16bit_slices() -> None:
    """默认 bands=4:64bit 均分为 4 个 16bit 带;桶键为精确 int 切片。"""
    assert DEFAULT_BANDS == 4 and BITS == 64
    index = LSHIndex()
    assert index.bands == 4
    assert index._layout == ((0, 16), (16, 16), (32, 16), (48, 16))
    # 0x1234567890abcdef:带 0 取最低 16bit,依次向高位推进
    assert index._band_keys(0x1234567890ABCDEF) == (
        0xCDEF, 0x90AB, 0x5678, 0x1234,
    )
    # 桶键确实落到对应带的桶里
    index.insert("1234567890abcdef", "payload")
    assert list(index._buckets[0]) == [0xCDEF]
    assert list(index._buckets[3]) == [0x1234]


def test_invalid_bands_rejected() -> None:
    """bands=0 / 负数 / 超过 64 / bool → 中文 ValueError。"""
    for bad in (0, -2, 65, 10_000, True):
        with pytest.raises(ValueError, match="bands"):
            LSHIndex(bands=bad)  # type: ignore[arg-type]


def test_non_divisor_bands_cover_all_bits() -> None:
    """bands=3(64 不整除):带宽 22/21/21 连续铺满 64 位,不丢信息。"""
    index = LSHIndex(bands=3)
    widths = [width for _, width in index._layout]
    assert widths == [22, 21, 21]
    shift = 0
    for band_shift, width in index._layout:
        assert band_shift == shift
        shift += width
    assert shift == 64
    rng = random.Random(101)
    base = _rand_fp(rng)
    index.insert(base, "hit")
    assert index.query(base, max_distance=0) == ["hit"]
    # d=1 < 3 带:仍保证召回
    index.insert(_flip(base, [7]), "near")
    assert sorted(index.query(base, max_distance=1)) == ["hit", "near"]


def test_bands_1_degrades_to_exact_match() -> None:
    """bands=1:单一 64bit 桶键 → 退化为精确匹配,d=1 即漏(文档化取舍)。"""
    index = LSHIndex(bands=1)
    assert index._layout == ((0, 64),)
    rng = random.Random(202)
    fps = [_rand_fp(rng) for _ in range(5)]
    for seq, hex64 in enumerate(fps):
        index.insert(hex64, seq)
    assert index.stats()["buckets"] == 5  # 互不相同的 64bit 键各占一桶
    assert index.query(fps[2], max_distance=0) == [2]
    assert index.query(_flip(fps[2], [0]), max_distance=1) == []  # 已知退化


# ---------------------------------------------------------------------------
# insert / query 基础语义
# ---------------------------------------------------------------------------


def test_exact_match_roundtrip_and_compare_calls() -> None:
    """精确匹配命中;单条目索引上一次查询恰好 1 次汉明比较。"""
    index = LSHIndex()
    index.insert(H0, "only")
    assert index.compare_calls == 0          # 只在查询时计数
    before = index.compare_calls
    assert index.query(H0, max_distance=0) == ["only"]
    assert index.compare_calls - before == 1
    # 再插入一条远距记录,精确查询的比较次数仍为 1
    index.insert(H_FAR, "far")
    before = index.compare_calls
    assert index.query(H0, max_distance=0) == ["only"]
    assert index.compare_calls - before == 1


def test_max_distance_filter_and_result_ordering() -> None:
    """max_distance 阈值过滤;结果按距离升序(并列按插入序)。"""
    index = LSHIndex()
    for hex64, payload in ((H0, "d0"), (H1, "d1"), (H8, "d8"), (H9, "d9")):
        index.insert(hex64, payload)
    assert index.query(H0, max_distance=8) == ["d0", "d1", "d8"]
    assert index.query(H0, max_distance=0) == ["d0"]
    assert index.query(H0, max_distance=7) == ["d0", "d1"]
    assert index.query(H0, max_distance=9) == ["d0", "d1", "d8", "d9"]
    assert index.query(H0, max_distance=64) == ["d0", "d1", "d8", "d9"]


def test_candidates_deduped_across_bands() -> None:
    """同一候选命中多个带只比较一次:精确匹配 4 带 + d=1 共 3 带,
    若无去重需 4+3=7 次比较,去重后恰 2 次。"""
    index = LSHIndex()
    index.insert(H0, "exact")
    index.insert(H1, "near")
    before = index.compare_calls
    hits = index.query(H0, max_distance=8)
    assert sorted(hits) == ["exact", "near"]
    assert index.compare_calls - before == 2


def test_payload_identity_preserved() -> None:
    """负载原样返回(同一对象 / None / dict / int,不做拷贝)。"""
    obj = object()
    payloads: list[Any] = [obj, None, 42, {"case": 1, "tags": ["nsfw"]}]
    index = LSHIndex()
    # 0x0001000100010001 × k:四个带键均为 k,互不相同 → 精确查询互不串扰
    for seq, payload in enumerate(payloads):
        index.insert(f"{0x0001000100010001 * (seq + 1):016x}", payload)
    for seq, payload in enumerate(payloads):
        key = f"{0x0001000100010001 * (seq + 1):016x}"
        (hit,) = index.query(key, max_distance=0)
        assert hit is payload or hit == payload


def test_hex_normalization_case_and_whitespace() -> None:
    """大小写与首尾空白容忍:归一化后同一指纹。"""
    index = LSHIndex()
    index.insert("  ABCDEF0123456789  ", "upper")
    assert index.insert("abcdef0123456789", "lower") is None
    assert index.query("AbCdEf0123456789", max_distance=0) == ["upper", "lower"]
    assert index.query("abcdef0123456789\n", max_distance=0) == ["upper", "lower"]


def test_invalid_hex_rejected_chinese() -> None:
    """insert / query 对非法字符、空值、错误长度抛中文 ValueError。"""
    index = LSHIndex()
    with pytest.raises(ValueError, match="非法"):
        index.insert("zzzzzzzzzzzzzzzz", 1)
    with pytest.raises(ValueError, match="非法"):
        index.insert("", 1)
    with pytest.raises(ValueError, match="非法"):
        index.insert(None, 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="长度"):
        index.insert("0" * 15, 1)
    with pytest.raises(ValueError, match="长度"):
        index.insert("0" * 32, 1)
    with pytest.raises(ValueError, match="长度"):
        index.query("0" * 17)
    with pytest.raises(ValueError, match="非法"):
        index.query("0x1234567890abcd")
    assert index.stats()["entries"] == 0  # 失败插入不留半条记录


def test_invalid_max_distance_rejected() -> None:
    """max_distance 越界(负数 / 超 64)→ 中文 ValueError。"""
    index = LSHIndex()
    index.insert(H0, 1)
    with pytest.raises(ValueError, match="max_distance"):
        index.query(H0, max_distance=-1)
    with pytest.raises(ValueError, match="max_distance"):
        index.query(H0, max_distance=65)
    assert index.query(H0, max_distance=64) == [1]  # 边界值合法


def test_empty_index_query_and_stats() -> None:
    """空索引:查询返回空、零比较;stats 全零。"""
    index = LSHIndex()
    assert index.query(H0, max_distance=8) == []
    assert index.compare_calls == 0
    assert index.stats() == {"entries": 0, "buckets": 0, "avg_bucket": 0.0}


# ---------------------------------------------------------------------------
# 召回:与暴力全表扫描一致性
# ---------------------------------------------------------------------------


def test_recall_d3_default_bands_matches_bruteforce() -> None:
    """默认 4 带:d ≤ 3 的近邻 100% 召回,结果集合与暴力扫描一致。"""
    rng = random.Random(20261001)
    index = LSHIndex()
    entries: list[tuple[str, int]] = []
    for seq in range(140):  # 随机填充
        hex64 = _rand_fp(rng)
        index.insert(hex64, seq)
        entries.append((hex64, seq))
    for seq in range(60):  # 每个基址挂一个 d∈{1,2,3} 的近邻
        base = _rand_fp(rng)
        index.insert(base, seq)
        entries.append((base, seq))
        distance = seq % 3 + 1
        neighbor = _flip(base, rng.sample(range(BITS), distance))
        index.insert(neighbor, 1000 + seq)
        entries.append((neighbor, 1000 + seq))
    for seq in range(60):  # 以基址为查询:近邻必在(去重、序不敏感)
        query = entries[140 + 2 * seq][0]
        brute = _assert_lsh_matches_bruteforce(index, entries, query, 4)
        assert 1000 + seq in brute


def test_bands_8_layout_and_d4_recall_vs_bruteforce() -> None:
    """bands=8:8 带 × 8bit;d ≤ 4 的近邻 100% 召回,与暴力扫描一致。"""
    index = LSHIndex(bands=8)
    assert [width for _, width in index._layout] == [8] * 8
    rng = random.Random(20261002)
    entries: list[tuple[str, int]] = []
    for seq in range(120):
        hex64 = _rand_fp(rng)
        index.insert(hex64, seq)
        entries.append((hex64, seq))
    for seq in range(80):
        base = _rand_fp(rng)
        index.insert(base, seq)
        entries.append((base, seq))
        neighbor = _flip(base, rng.sample(range(BITS), seq % 4 + 1))
        index.insert(neighbor, 1000 + seq)
        entries.append((neighbor, 1000 + seq))
    for seq in range(80):
        query = entries[120 + 2 * seq][0]
        brute = _assert_lsh_matches_bruteforce(index, entries, query, 8)
        assert 1000 + seq in brute  # d≤4 < 8 带:理论保证 100% 召回


def test_bands_4_recall_d4_confined_to_three_bands() -> None:
    """默认 4 带:d=4 但翻转限定在 3 个带内 → 第 4 带完好,必召回。"""
    rng = random.Random(20261003)
    index = LSHIndex()
    entries: list[tuple[str, int]] = []
    for seq in range(50):
        base = _rand_fp(rng)
        index.insert(base, seq)
        entries.append((base, seq))
        # 4 个翻转位全部落在带 0~2(位 0..47),带 3(位 48..63)不动
        neighbor = _flip(base, rng.sample(range(48), 4))
        index.insert(neighbor, 1000 + seq)
        entries.append((neighbor, 1000 + seq))
    for seq in range(50):
        brute = _assert_lsh_matches_bruteforce(
            index, entries, entries[2 * seq][0], 8
        )
        assert 1000 + seq in brute


def test_documented_miss_one_flip_per_band() -> None:
    """已知结构性漏检:4 带、每带恰好各翻 1 位(d=4)→ 全带皆毁无候选。

    这是 LSH 方法论的固有取舍(类 docstring 已声明 d ≤ bands-1 才保证
    召回),本用例锁定该边界行为,并证明暴力扫描确实能找到——即漏检
    只发生在"无候选"路径,而已命中的候选由汉明过滤精确裁决。
    """
    index = LSHIndex()
    base = "0f1e2d3c4b5a6978"
    neighbor = _flip(base, [1, 17, 33, 49])  # 每带各一位
    assert hamming(base, neighbor) == 4
    index.insert(neighbor, "near")
    before = index.compare_calls
    assert index.query(base, max_distance=8) == []   # 漏检:零候选
    assert index.compare_calls - before == 0
    # 暴力口径下它本应命中(d=4 ≤ 8)
    assert hamming(base, neighbor) <= 8


def test_far_distance_query_zero_hits() -> None:
    """远距(>12,构造性保证)零命中:顶 24 位取反 → 距查询 ≥ 24。"""
    rng = random.Random(20261004)
    query = _rand_fp(rng)
    query_value = int(query, 16)
    mask_high = ((1 << 24) - 1) << 40  # 覆盖位 40..63
    index = LSHIndex()
    entries: list[tuple[str, int]] = []
    for seq in range(100):
        value = (rng.getrandbits(BITS) & ~mask_high) | (~query_value & mask_high)
        hex64 = f"{value:016x}"
        assert hamming(query, hex64) >= 24  # 构造性远距(> 12)
        index.insert(hex64, seq)
        entries.append((hex64, seq))
    _assert_lsh_matches_bruteforce(index, entries, query, 8)
    assert index.query(query, max_distance=12) == []


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def test_stats_exact_counts() -> None:
    """stats 精确计数:无带冲突时 buckets = entries × bands,avg = 1.0;
    重复插入同指纹共享全部桶 → 桶数不变、平均值上升。"""
    index = LSHIndex()
    # 0x0001000100010001 × k(k=1..5):四个带键均为 k,互不相同、无跨条目碰撞
    for k in range(1, 6):
        index.insert(f"{0x0001000100010001 * k:016x}", k)
    stats = index.stats()
    assert stats["entries"] == 5
    assert stats["buckets"] == 5 * 4
    assert stats["avg_bucket"] == pytest.approx(1.0)
    assert set(stats) == {"entries", "buckets", "avg_bucket"}

    index.insert(f"{0x0001000100010001 * 2:016x}", "dup")  # 与 k=2 完全同指纹
    stats = index.stats()
    assert stats["entries"] == 6
    assert stats["buckets"] == 20            # 未新增桶
    assert stats["avg_bucket"] == pytest.approx(24 / 20)


# ---------------------------------------------------------------------------
# build_from:与 PhashRegistry 互操作
# ---------------------------------------------------------------------------


def test_build_from_registry_interop_synthetic(tmp_path: Any) -> None:
    """真 PhashRegistry + 合成 hex:全表装载、查询命中集合与
    find_similar 口径一致;负载为 {"sha256","site","verdict_tag"}。"""
    reg = PhashRegistry(str(tmp_path / "phash.db"))
    try:
        reg.register(SHA_A, H0, SITE_A, "nsfw")
        reg.register(SHA_B, H2, SITE_B)
        reg.register(SHA_C, H_FAR, SITE_A, "suspect")

        index = LSHIndex()
        assert index.build_from(reg) == 3
        assert index.stats()["entries"] == 3

        hits = index.query(H0, max_distance=8)
        assert {p["sha256"] for p in hits} == {SHA_A, SHA_B}
        assert {h["sha256"] for h in reg.find_similar(H0, 8)} == {
            p["sha256"] for p in hits
        }
        by_sha = {p["sha256"]: p for p in hits}
        assert by_sha[SHA_A] == {
            "sha256": SHA_A, "site": SITE_A, "verdict_tag": "nsfw",
        }
        assert by_sha[SHA_B]["site"] == SITE_B
        assert by_sha[SHA_B]["verdict_tag"] == ""
        # 远距查询只命中自身记录
        (only_far,) = index.query(H_FAR, max_distance=8)
        assert only_far["sha256"] == SHA_C
    finally:
        reg.close()


def test_build_from_skips_dirty_rows_with_telemetry(tmp_path: Any) -> None:
    """库中混入非 64bit 脏哈希行时跳过不中断,并计 telemetry 脏行数。"""
    from netsentinel import telemetry

    db = str(tmp_path / "phash.db")
    reg = PhashRegistry(db)
    reg.register(SHA_A, H0, SITE_A)
    reg.register(SHA_B, H1, SITE_B)
    reg.close()
    conn = sqlite3.connect(db)  # 绕过校验直写脏行
    for sha, dirty in (("e" * 64, "zz"), ("f" * 64, "0" * 10), ("d" * 64, "g" * 32)):
        conn.execute(
            "INSERT INTO hashes (sha256, phash, site_url, verdict_tag, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (sha, dirty, SITE_A, "", "2026-01-01T00:00:00+00:00"),
        )
    conn.commit()
    conn.close()

    telemetry.reset()
    try:
        reg2 = PhashRegistry(db)
        try:
            index = LSHIndex()
            assert index.build_from(reg2) == 2
            assert index.stats()["entries"] == 2
            assert telemetry.snapshot()["counters"].get(
                "phash.lsh_dirty_hash"
            ) == 3
            assert sorted(
                p["sha256"] for p in index.query(H0, max_distance=1)
            ) == sorted([SHA_A, SHA_B])
        finally:
            reg2.close()
    finally:
        telemetry.reset()


def test_build_from_rejects_non_registry() -> None:
    """非 PhashRegistry 对象 → TypeError(中文提示)。"""
    index = LSHIndex()
    with pytest.raises(TypeError, match="PhashRegistry"):
        index.build_from(object())
    with pytest.raises(TypeError, match="PhashRegistry"):
        index.build_from("not-a-registry")


def test_build_from_appends_and_repeats(tmp_path: Any) -> None:
    """build_from 不清空既有内容;重复调用按次累加(负载可重复)。"""
    reg = PhashRegistry(str(tmp_path / "phash.db"))
    try:
        reg.register(SHA_A, H0, SITE_A)
        reg.register(SHA_B, H1, SITE_B)
        index = LSHIndex()
        index.insert(H0, "manual")
        assert index.build_from(reg) == 2
        assert index.stats()["entries"] == 3
        assert index.build_from(reg) == 2            # 再次装载,累加
        assert index.stats()["entries"] == 5
        hits = index.query(H0, max_distance=0)
        expected: list[Any] = [
            "manual",
            {"sha256": SHA_A, "site": SITE_A, "verdict_tag": ""},
            {"sha256": SHA_A, "site": SITE_A, "verdict_tag": ""},
        ]
        assert sorted(map(repr, hits)) == sorted(map(repr, expected))
    finally:
        reg.close()


def test_build_from_real_images_pillow(tmp_path: Any) -> None:
    """真 PNG 图互操作:登记原图后,用缩放版的 phash 经 LSH 查回原记录。

    取 bands=32(d ≤ 31 保证召回;本图对缩放实测距离 ≤ 20),把"召回"
    从带数权衡中隔离出来,单独验证互操作链路(phash → registry →
    build_from → query);远距控制(距 origin 恰 20 位)在 md=12 下由
    汉明过滤兜底为零命中。
    """
    PIL_Image = pytest.importorskip(
        "PIL.Image", reason="需要 Pillow 才能做真图互操作测试"
    )
    import math

    from netsentinel.intel.phash import phash

    size = 200
    img = PIL_Image.new("L", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            base = 96 + 48 * math.sin(x * 2 * math.pi / size + 0.7) \
                + 32 * math.cos(y * 2 * math.pi / size + 1.9)
            px[x, y] = max(0, min(255, int(base)))
    p200 = tmp_path / "origin.png"
    p160 = tmp_path / "resized.png"
    img.save(p200, format="PNG")
    img.resize((160, 160), PIL_Image.Resampling.LANCZOS).save(
        p160, format="PNG"
    )
    rng = random.Random(20261005)
    noise = PIL_Image.new("L", (size, size))
    npx = noise.load()
    block = size // 8
    for by in range(0, size, block):
        for bx in range(0, size, block):
            value = rng.randrange(256)
            for y in range(by, min(by + block, size)):
                for x in range(bx, min(bx + block, size)):
                    npx[x, y] = value
    pnoise = tmp_path / "noise.png"
    noise.save(pnoise, format="PNG")

    import hashlib

    h_origin = phash(str(p200))
    h_resized = phash(str(p160))
    distance = hamming(h_origin, h_resized)
    assert distance <= 20  # 缩放稳定性(与 test_phash 的近重复口径一致)

    sha_origin = hashlib.sha256(p200.read_bytes()).hexdigest()
    reg = PhashRegistry(str(tmp_path / "phash.db"))
    try:
        reg.register(sha_origin, h_origin, SITE_A, "nsfw")
        reg.register(SHA_C, phash(str(pnoise)), SITE_B)
        index = LSHIndex(bands=32)
        assert index.build_from(reg) == 2
        assert index.stats()["entries"] == 2

        hits = index.query(h_resized, max_distance=max(distance, 4))
        assert sha_origin in {p["sha256"] for p in hits}, (
            "换站重传的缩放版近重复图应能经 LSH 查回原登记记录"
        )
        far = _flip(h_origin, list(range(20)))
        assert hamming(h_origin, far) == 20
        assert index.query(far, max_distance=12) == []
    finally:
        reg.close()


# ---------------------------------------------------------------------------
# bench(红线 31:操作计数,不依赖墙钟)
# ---------------------------------------------------------------------------


def test_v7_bench_query_compare_calls_vs_full_scan() -> None:
    """bench:10^3 随机指纹 + 5 个近邻(d≤3),单查询比较次数 ≤ 全表 10%。

    全表扫描需 1005 次汉明比较;分带 LSH 只比较桶候选(期望 ≈ 5 次,
    随机同桶概率 ≈ 4/2^16),且命中集合与全表扫描完全一致——以操作
    计数证明代差而非计时。
    """
    rng = random.Random(20261031)
    index = LSHIndex()
    entries: list[tuple[str, int]] = []
    for seq in range(1000):
        hex64 = _rand_fp(rng)
        index.insert(hex64, seq)
        entries.append((hex64, seq))
    probe = _rand_fp(rng)
    for j in range(5):  # 近邻 d∈{1,2,3}:4 带下保证召回
        neighbor = _flip(probe, rng.sample(range(BITS), j % 3 + 1))
        index.insert(neighbor, 1000 + j)
        entries.append((neighbor, 1000 + j))
    total = len(entries)
    assert total == 1005

    before = index.compare_calls
    hits = index.query(probe, max_distance=8)
    used = index.compare_calls - before

    brute = _brute_force_ids(entries, probe, 8)
    assert sorted(hits) == brute                      # 结果与全表扫描一致
    assert brute == [1000, 1001, 1002, 1003, 1004]    # 5 个近邻全召回
    assert used >= len(hits)                          # 计数不小于命中数
    assert used * 10 <= total, (
        f"LSH 单查询比较 {used} 次超过全表 {total} 的 10%"
        f"(全表扫描需 {total} 次,代差断言失败)"
    )


def test_v7_bench_bands8_compare_fraction() -> None:
    """bench(bands=8):随机同桶概率升到 ≈ 8/2^8,候选期望 ≈ 31,
    单查询比较次数仍 ≤ 全表 10%(换取 d ≤ 7 的召回保证)。"""
    rng = random.Random(20261115)
    index = LSHIndex(bands=8)
    entries: list[tuple[str, int]] = []
    for seq in range(1000):
        hex64 = _rand_fp(rng)
        index.insert(hex64, seq)
        entries.append((hex64, seq))
    probe = _rand_fp(rng)
    before = index.compare_calls
    hits = index.query(probe, max_distance=8)
    used = index.compare_calls - before
    assert sorted(hits) == _brute_force_ids(entries, probe, 8)
    assert used * 10 <= 1000, f"bands=8 单查询比较 {used} 次超过全表 10%"


def test_kernel_selfcheck_ratio() -> None:
    """kernel_selfcheck(A138 契约):确定性自检返回比较比例 ≤ 10%。"""
    report = kernel_selfcheck()
    assert report["name"] == "phash_lsh"
    assert report["metric"] == "query_compare_ratio_vs_full_scan"
    assert report["baseline"] == 1.0
    assert 0 < report["value"] <= 0.1


def test_telemetry_query_counters() -> None:
    """遥测:每次查询 phash.lsh_query +1,比较次数累加 phash.lsh_compare。"""
    from netsentinel import telemetry

    index = LSHIndex()
    index.insert(H0, "a")
    index.insert(H1, "b")
    index.insert(H_FAR, "c")
    telemetry.reset()
    try:
        before = index.compare_calls
        index.query(H0, max_distance=8)
        counters = telemetry.snapshot()["counters"]
        assert counters.get("phash.lsh_query") == 1
        assert counters.get("phash.lsh_compare") == index.compare_calls - before
        index.query(H_FAR, max_distance=0)
        counters = telemetry.snapshot()["counters"]
        assert counters.get("phash.lsh_query") == 2
    finally:
        telemetry.reset()


# ---------------------------------------------------------------------------
# 并发
# ---------------------------------------------------------------------------


def test_concurrent_insert_and_query() -> None:
    """8 线程并发 insert / query:无异常、条目全落地、计数器单调不减。"""
    index = LSHIndex()
    errors: list[str] = []
    workers = 8
    per_worker = 50

    def worker(tid: int) -> None:
        try:
            rng = random.Random(9000 + tid)
            local = []
            for i in range(per_worker):
                hex64 = _rand_fp(rng)
                index.insert(hex64, tid * 1000 + i)
                local.append(hex64)
                if i % 10 == 0:
                    assert index.query(local[-1], max_distance=0)
        except Exception as exc:  # noqa: BLE001 - 线程内异常汇总断言
            errors.append(f"线程 {tid}:{exc}")

    threads = [
        threading.Thread(target=worker, args=(t,)) for t in range(workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    stats = index.stats()
    assert stats["entries"] == workers * per_worker
    assert stats["buckets"] <= workers * per_worker * 4
    assert index.compare_calls >= 0


# ---------------------------------------------------------------------------
# A195 MultiTableLSH:多表 multi-probe LSH(与上方单表 LSHIndex 并存)
# ---------------------------------------------------------------------------

#: 规模 bench 用的多表参数(K 张 26bit 桶键表 + 探针深度 3)。
#: 实测(Random(42) 数据集,确定复现):d=16 召回 119/120 ≈ 99.2%,
#: 单查询比较均值 ≈ 55 / 最大 72,均 < 全表(10200)的 1%。
_MT_SCALE_TABLES = 150
_MT_SCALE_KEY_BITS = 26
_MT_SCALE_PROBE = 3


def _plant_pairs(
    rng: random.Random, entries_by_distance: dict[int, int]
) -> list[tuple[int, str, str]]:
    """按距离各生成若干 (distance, base, neighbor) 已知近邻对(确定性)。

    生成序固定:每对先取随机基址,再 ``rng.sample`` 抽 distance 个位翻转;
    neighbor 入库、base 作查询(模拟"换站重传版本在库,原图来查")。
    """
    pairs: list[tuple[int, str, str]] = []
    for distance, count in sorted(entries_by_distance.items()):
        for _ in range(count):
            base = _rand_fp(rng)
            neighbor = _flip(base, rng.sample(range(BITS), distance))
            assert hamming(base, neighbor) == distance
            pairs.append((distance, base, neighbor))
    return pairs


# ---------------------------------------------------------------------------
# 构造 / 桶布局 / 探针掩码
# ---------------------------------------------------------------------------


def test_mt_constructor_defaults_and_validation() -> None:
    """默认参数 K=8 / B=16 / p=2;非法参数与探针组合数上限 → 中文 ValueError。"""
    index = MultiTableLSH()
    assert index.tables == DEFAULT_TABLES == 8
    assert index.key_bits == DEFAULT_KEY_BITS == 16
    assert index.probe_depth == DEFAULT_PROBE_DEPTH == 2
    assert index.db_path is None            # 缺省纯内存
    assert index.compare_calls == 0 and index.probe_calls == 0
    with pytest.raises(ValueError, match="tables"):
        MultiTableLSH(tables=0)
    with pytest.raises(ValueError, match="tables"):
        MultiTableLSH(tables=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="key_bits"):
        MultiTableLSH(key_bits=0)
    with pytest.raises(ValueError, match="key_bits"):
        MultiTableLSH(key_bits=65)
    with pytest.raises(ValueError, match="probe_depth"):
        MultiTableLSH(probe_depth=-1)
    with pytest.raises(ValueError, match="probe_depth"):
        MultiTableLSH(key_bits=8, probe_depth=9)   # 超过键宽
    with pytest.raises(ValueError, match="seed"):
        MultiTableLSH(seed="x")  # type: ignore[arg-type]
    # 探针组合数上限:B=64、p=5 → Σ C(64,i) 远超 65536
    with pytest.raises(ValueError, match="probe_depth"):
        MultiTableLSH(key_bits=64, probe_depth=5)
    assert set(index.stats()) == {
        "entries", "tables", "key_bits", "probe_depth", "buckets", "avg_bucket",
    }


def test_mt_permutation_deterministic_and_seed_sensitive() -> None:
    """同种子 → K 张表的桶键逐位一致;异种子 → 桶布局不同;键值域正确。"""
    value = int("0f1e2d3c4b5a6978", 16)
    a = MultiTableLSH(tables=8, key_bits=16, probe_depth=2)
    b = MultiTableLSH(tables=8, key_bits=16, probe_depth=2)
    keys_a = a._table_keys(value)
    keys_b = b._table_keys(value)
    assert keys_a == keys_b                     # 确定性:同种子同布局
    assert len(keys_a) == 8
    assert all(0 <= key < (1 << 16) for key in keys_a)
    # 探针掩码集:原桶 0 + 16 个单翻 + 120 个双翻,互异且都在键值域内
    deltas = a._probe_deltas
    assert len(deltas) == 1 + 16 + 120
    assert deltas[0] == 0
    assert len(set(deltas)) == len(deltas)
    assert all(0 <= d < (1 << 16) for d in deltas)
    assert a._probe_deltas == b._probe_deltas
    # 异种子:置换不同,同一指纹至少在一张表落入不同桶
    c = MultiTableLSH(tables=8, key_bits=16, probe_depth=2, seed=a.seed + 1)
    keys_c = c._table_keys(value)
    assert any(x != y for x, y in zip(keys_a, keys_c))


# ---------------------------------------------------------------------------
# insert / query 基础语义
# ---------------------------------------------------------------------------


def test_mt_roundtrip_filter_ordering_and_payloads() -> None:
    """精确命中、max_distance 过滤、(距离, 插入序) 排序、负载身份原样返回。"""
    index = MultiTableLSH()
    index.insert(H0, "d0")
    index.insert(H1, "d1")
    index.insert(H2, "d2")
    index.insert(H8, "d8")
    index.insert(H9, "d9")
    assert index.query(H0, max_distance=8) == ["d0", "d1", "d2", "d8"]
    assert index.query(H0, max_distance=0) == ["d0"]
    assert index.query(H0, max_distance=1) == ["d0", "d1"]
    assert index.query(H0, max_distance=64) == ["d0", "d1", "d2", "d8", "d9"]
    payload = {"case": 3, "tags": ["nsfw"]}
    index.insert(H_FAR, payload)
    (hit,) = index.query(H_FAR, max_distance=0)
    assert hit is payload


def test_mt_candidate_dedup_and_counters() -> None:
    """多表命中同一候选只比较一次;compare_calls 按候选数、probe_calls 按桶查找计。"""
    index = MultiTableLSH(tables=8, key_bits=16, probe_depth=2)
    index.insert(H0, "exact")
    index.insert(H1, "near")
    before_c, before_p = index.compare_calls, index.probe_calls
    hits = index.query(H0, max_distance=8)
    assert sorted(hits) == ["exact", "near"]
    # 候选去重:两条记录各比较一次(8 张表 × 137 探针命中同一候选不重计)
    assert index.compare_calls - before_c == 2
    # 桶查找 = K × 探针掩码数(含落空),与命中无关
    assert index.probe_calls - before_p == 8 * (1 + 16 + 120)
    # 远距查询零命中:精确过滤兜底,不因多表探针引入假阳性
    assert index.query(H_FAR, max_distance=0) == []


def test_mt_hex_normalization_and_validation() -> None:
    """大小写 / 空白归一化;非法 hex、越界 max_distance → 中文 ValueError。"""
    index = MultiTableLSH()
    index.insert("  ABCDEF0123456789  ", "upper")
    assert index.insert("abcdef0123456789", "lower") is None
    assert index.query("AbCdEf0123456789", max_distance=0) == ["upper", "lower"]
    with pytest.raises(ValueError, match="非法"):
        index.insert("zzzzzzzzzzzzzzzz", 1)
    with pytest.raises(ValueError, match="长度"):
        index.insert("0" * 15, 1)
    with pytest.raises(ValueError, match="非法"):
        index.query(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_distance"):
        index.query(H0, max_distance=-1)
    with pytest.raises(ValueError, match="max_distance"):
        index.query(H0, max_distance=65)
    assert index.stats()["entries"] == 2     # 失败插入不留半条记录
    # 空索引:零命中、零比较;探针照常执行(K × 掩码数,含落空)
    empty = MultiTableLSH()
    assert empty.query(H0, max_distance=8) == []
    assert empty.compare_calls == 0
    assert empty.probe_calls == 8 * 137
    stats = empty.stats()
    assert stats["entries"] == 0 and stats["buckets"] == 0
    assert stats["avg_bucket"] == 0.0


def test_mt_stats_buckets_and_collision_promotion() -> None:
    """stats 精确计数;同指纹重复插入把 int 桶升格 tuple,均值翻倍。"""
    index = MultiTableLSH(tables=4, key_bits=16, probe_depth=1)
    for k in range(1, 6):
        index.insert(f"{0x1111111111111111 * k:016x}", k)
    stats = index.stats()
    assert stats["entries"] == 5
    assert stats["buckets"] == 5 * 4        # 无跨条目碰撞:每条每表各一桶
    assert stats["avg_bucket"] == pytest.approx(1.0)
    index.insert(f"{0x1111111111111111 * 2:016x}", "dup")
    stats = index.stats()
    assert stats["entries"] == 6
    assert stats["buckets"] == 20            # 未新增桶,旧桶升格 tuple
    assert stats["avg_bucket"] == pytest.approx(24 / 20)


# ---------------------------------------------------------------------------
# 召回:可证域 / 探针开关对照 / 对单表结构性漏检的找回
# ---------------------------------------------------------------------------


def test_mt_guarantee_domain_d4_matches_bruteforce() -> None:
    """默认参数下 d=4(恰为 2·probe_depth 边界)近邻 100% 召回,
    与暴力全表扫描完全一致(可证召回域,漏检上界 ~1e-11/对)。"""
    rng = random.Random(20261011)
    index = MultiTableLSH()
    entries: list[tuple[str, int]] = []
    for seq in range(500):
        hex64 = _rand_fp(rng)
        index.insert(hex64, seq)
        entries.append((hex64, seq))
    pairs: list[tuple[str, str, int]] = []
    for seq in range(60):
        base = _rand_fp(rng)
        neighbor = _flip(base, rng.sample(range(BITS), 4))
        index.insert(neighbor, 1000 + seq)
        entries.append((neighbor, 1000 + seq))
        pairs.append((base, neighbor, 1000 + seq))
    for base, _neighbor, payload in pairs:
        hits = index.query(base, max_distance=4)
        brute = _brute_force_ids(entries, base, 4)
        assert sorted(hits) == brute, (
            f"可证域(d=4 ≤ 2p)命中 {sorted(hits)} 与全表扫描 {brute} 不一致"
        )
        assert payload in hits               # 已知近邻全召回


def test_mt_multiprobe_off_vs_on_recall_contrast() -> None:
    """probe_depth=0(仅原桶)与 p=2(默认探针)在 d=8 的召回对照:
    关闭探针召回 < 60%,开启后 100%;probe_calls 精确等于 K×掩码数。"""
    rng = random.Random(20261012)
    fillers = [_rand_fp(rng) for _ in range(300)]
    pairs = [(base := _rand_fp(rng), _flip(base, rng.sample(range(BITS), 8)))
             for _ in range(20)]
    off = MultiTableLSH(tables=8, key_bits=16, probe_depth=0)
    on = MultiTableLSH(tables=8, key_bits=16, probe_depth=2)
    for seq, hex64 in enumerate(fillers):
        off.insert(hex64, seq)
        on.insert(hex64, seq)
    for seq, (_base, neighbor) in enumerate(pairs):
        off.insert(neighbor, 1000 + seq)
        on.insert(neighbor, 1000 + seq)

    recall = {"off": 0.0, "on": 0.0}
    for mode, index in (("off", off), ("on", on)):
        before_p = index.probe_calls
        found = sum(
            1
            for seq, (base, _neighbor) in enumerate(pairs)
            if 1000 + seq in index.query(base, max_distance=8)
        )
        recall[mode] = found / len(pairs)
        probes = index.probe_calls - before_p
        per_query = 8 * 1 if mode == "off" else 8 * 137
        assert probes == per_query * len(pairs)
    assert recall["on"] == 1.0                       # 开启 multi-probe 全召回
    assert recall["off"] < 0.6, "关闭探针的召回应显著低于开启(d=8 高距区)"
    assert recall["on"] - recall["off"] > 0.4        # 量化提升


def test_mt_finds_what_single_table_structurally_misses() -> None:
    """单表版每带各翻一位的结构性漏检用例,被多表 multi-probe 找回。"""
    base = "0f1e2d3c4b5a6978"
    neighbor = _flip(base, [1, 17, 33, 49])          # 4 带 × 16bit 各毁一位
    assert hamming(base, neighbor) == 4
    old = LSHIndex()
    old.insert(neighbor, "near")
    assert old.query(base, max_distance=8) == []      # 单表:零候选漏检(既有边界)
    index = MultiTableLSH()                           # 多表默认参数
    index.insert(neighbor, "near")
    assert index.query(base, max_distance=8) == ["near"]


def test_mt_far_distance_zero_hits() -> None:
    """远距(≥24,构造性)零命中:精确汉明过滤兜底,邻桶探针不产生假阳性。"""
    rng = random.Random(20261013)
    query = _rand_fp(rng)
    query_value = int(query, 16)
    mask_high = ((1 << 24) - 1) << 40
    index = MultiTableLSH()
    for seq in range(100):
        value = (rng.getrandbits(BITS) & ~mask_high) | (~query_value & mask_high)
        hex64 = f"{value:016x}"
        assert hamming(query, hex64) >= 24
        index.insert(hex64, seq)
    assert index.query(query, max_distance=12) == []


# ---------------------------------------------------------------------------
# sqlite 持久化(flush 协议:惰性加载 / 全量覆盖 / 安全重建)
# ---------------------------------------------------------------------------


def test_mt_persistence_roundtrip_and_flush_protocol(tmp_path: Any) -> None:
    """flush → 惰性加载 → 追加 → 再 flush 的完整往返;负载经 JSON 等价往返。"""
    rng = random.Random(20261014)
    db = str(tmp_path / "mt_lsh.db")
    entries: list[tuple[str, Any]] = []
    index = MultiTableLSH(tables=4, key_bits=12, probe_depth=1, db_path=db)
    for seq in range(200):
        hex64 = _rand_fp(rng)
        payload = {"seq": seq, "tag": f"t{seq}", "meta": [seq, True, None]}
        index.insert(hex64, payload)
        entries.append((hex64, payload))
    assert index.flush() == 200
    assert index.flush() == 200                      # 幂等
    index.close()

    reloaded = MultiTableLSH(tables=4, key_bits=12, probe_depth=1, db_path=db)
    assert reloaded.stats()["entries"] == 200        # 首次访问触发惰性加载
    for hex64, payload in entries[:20]:
        hits = reloaded.query(hex64, max_distance=0)
        assert hits == [payload]                     # JSON 往返内容等价
    reloaded.insert("0" * 16, "added")
    assert reloaded.flush() == 201
    reloaded.close()

    again = MultiTableLSH(tables=4, key_bits=12, probe_depth=1, db_path=db)
    try:
        assert again.stats()["entries"] == 201
        assert again.query("0" * 16, max_distance=0) == ["added"]
    finally:
        again.close()

    # 未注入 db_path 的纯内存索引不可 flush
    plain = MultiTableLSH(tables=2, key_bits=8)
    with pytest.raises(ValueError, match="db_path"):
        plain.flush()


def test_mt_persistence_rejects_non_json_payload(tmp_path: Any) -> None:
    """不可 JSON 序列化的负载 flush 抛中文 ValueError,内存态不受影响;
    None 负载合法(落库为 NULL)。"""
    index = MultiTableLSH(tables=2, key_bits=8, db_path=str(tmp_path / "x.db"))
    try:
        index.insert(H0, object())
        with pytest.raises(ValueError, match="JSON"):
            index.flush()
        assert index.query(H0, max_distance=0)      # 内存行为完好
        # 把不可序列化负载换成 None(落库为 NULL)后 flush 通过
        hex0, value0, _payload = index._entries[0]
        index._entries[0] = (hex0, value0, None)
        index.insert(H1, None)
        assert index.flush() == 2
    finally:
        index.close()


def test_mt_persistence_corrupt_file_rebuilds(tmp_path: Any) -> None:
    """库文件损坏(非 sqlite 格式)→ 告警重建空库,计数 telemetry,可继续使用。"""
    from netsentinel import telemetry

    db = tmp_path / "mt_lsh.db"
    index = MultiTableLSH(tables=2, key_bits=8, db_path=str(db))
    index.insert(H0, "a")
    assert index.flush() == 1
    index.close()
    db.write_bytes(b"this is definitely not a sqlite database file")
    for suffix in ("-wal", "-shm"):
        (tmp_path / f"mt_lsh.db{suffix}").unlink(missing_ok=True)

    telemetry.reset()
    try:
        revived = MultiTableLSH(tables=2, key_bits=8, db_path=str(db))
        try:
            assert revived.stats()["entries"] == 0   # 空库重建
            assert telemetry.snapshot()["counters"].get(
                "phash.mt_lsh_rebuild"
            ) == 1.0
            revived.insert(H1, "b")                  # 重建后可继续用
            assert revived.flush() == 1
            assert revived.query(H1, max_distance=0) == ["b"]
        finally:
            revived.close()
    finally:
        telemetry.reset()


def test_mt_persistence_param_mismatch_recomputes(tmp_path: Any) -> None:
    """换 seed 复用旧库:参数指纹不匹配 → 只装条目、按当前参数重算桶位。"""
    from netsentinel import telemetry

    db = str(tmp_path / "mt_lsh.db")
    rng = random.Random(20261015)
    entries: list[tuple[str, int]] = []
    a = MultiTableLSH(tables=4, key_bits=12, probe_depth=1, db_path=db)
    for seq in range(150):
        hex64 = _rand_fp(rng)
        a.insert(hex64, seq)
        entries.append((hex64, seq))
    a.insert(_flip(entries[3][0], [1, 2, 3]), 999)   # 一个 d=3 近邻
    a.flush()
    a.close()

    telemetry.reset()
    try:
        b = MultiTableLSH(
            tables=4, key_bits=12, probe_depth=1, seed=a.seed + 7, db_path=db
        )
        try:
            assert b.stats()["entries"] == 151
            assert telemetry.snapshot()["counters"].get(
                "phash.mt_lsh_param_mismatch"
            ) == 1.0
            # 重算桶位后:查询与暴力扫描一致(条目不丢,只是换了一组置换)
            neighbor_hex = _flip(entries[3][0], [1, 2, 3])
            all_entries = entries + [(neighbor_hex, 999)]
            hits = b.query(entries[3][0], max_distance=4)
            brute = sorted(
                seq
                for hex64, seq in all_entries
                if hamming(entries[3][0], hex64) <= 4
            )
            assert sorted(hits) == brute
        finally:
            b.close()
    finally:
        telemetry.reset()


# ---------------------------------------------------------------------------
# 规模 bench(红线 31:10^4 指纹、操作计数、与旧单表对比)
# ---------------------------------------------------------------------------


def _build_scale_dataset() -> tuple[
    list[str], list[tuple[int, str, str]]
]:
    """Random(42) 确定的 10^4 随机指纹 + d=8/12/16 各 40/40/120 个已知近邻对。

    数据流固定(先 10^4 个 getrandbits,再按 8/12/16 逐对取基址 + 抽位
    翻转),任何环境下字节级一致,召回与比较次数为确定复现的实测值。
    """
    rng = random.Random(42)
    fillers = [_rand_fp(rng) for _ in range(10_000)]
    pairs = _plant_pairs(rng, {8: 40, 12: 40, 16: 120})
    assert len(pairs) == 200
    return fillers, pairs


def test_mt_scale_10k_recall_and_compare_budget() -> None:
    """规模 bench:10^4 指纹 + 200 个已知近邻对(d=8/12/16)。

    多表参数 K=150 × B=26bit × p=3(确定性实测,见模块常量注释):

    - d=16 召回 119/120 ≈ 99.2% > 99%(multi-probe 开;同场景旧单表
      仅 1/120 ≈ 0.8%,每带各毁致结构性漏检——对比断言量化提升);
    - d=8 / d=12 召回 100%;
    - 单查询汉明比较次数均值 ≈ 55、最大 72,均 < 全表(10200)的 1%;
      桶查找次数恰为 K × (1+26+325+2600) = 442800/查询(操作计数验证);
    - d=8 查询的命中集合与暴力全表扫描完全一致(正确性对照)。
    """
    fillers, pairs = _build_scale_dataset()
    index = MultiTableLSH(
        tables=_MT_SCALE_TABLES,
        key_bits=_MT_SCALE_KEY_BITS,
        probe_depth=_MT_SCALE_PROBE,
    )
    for seq, hex64 in enumerate(fillers):
        index.insert(hex64, seq)
    for j, (_distance, _base, neighbor) in enumerate(pairs):
        index.insert(neighbor, 10_000 + j)
    total = index.stats()["entries"]
    assert total == 10_200

    recall: dict[int, float] = {}
    avg_compares: dict[int, float] = {}
    max_compares: dict[int, int] = {}
    for distance in (8, 12, 16):
        found = 0
        used_total = 0
        used_max = 0
        for j, (pair_distance, base, _neighbor) in enumerate(pairs):
            if pair_distance != distance:
                continue
            before = index.compare_calls
            hits = index.query(base, max_distance=distance)
            used = index.compare_calls - before
            used_total += used
            used_max = max(used_max, used)
            if 10_000 + j in hits:
                found += 1
            if distance == 8:
                # d=8 随机近距无意外命中:结果集合与暴力扫描完全一致
                expected = [
                    seq for seq, hex64 in enumerate(fillers)
                    if hamming(base, hex64) <= 8
                ]
                assert sorted(hits) == sorted(
                    expected + [10_000 + j]
                ), "d=8 命中集合必须与全表扫描一致"
        count = {8: 40, 12: 40, 16: 120}[distance]
        recall[distance] = found / count
        avg_compares[distance] = used_total / count
        max_compares[distance] = used_max

    # 桶查找计数独立核验:200 次查询 × K × 掩码数
    assert index.probe_calls == 200 * _MT_SCALE_TABLES * (1 + 26 + 325 + 2600)

    budget = total * 0.01                                  # 全表扫描的 1%
    assert recall[16] > 0.99, f"d=16 召回 {recall[16]:.4f} 未达 99%"
    assert recall[8] == 1.0 and recall[12] == 1.0
    for distance in (8, 12, 16):
        assert avg_compares[distance] < budget, (
            f"d={distance} 平均比较 {avg_compares[distance]:.1f} "
            f"超过全表 {total} 的 1%(={budget:.0f})"
        )
        assert max_compares[distance] < budget, (
            f"d={distance} 单查询最大比较 {max_compares[distance]} 超过 1% 预算"
        )

    # ---- 旧单表同场景对照:结构性漏检,召回显著低 ----
    old = LSHIndex()
    for seq, hex64 in enumerate(fillers):
        old.insert(hex64, seq)
    for j, (_distance, _base, neighbor) in enumerate(pairs):
        old.insert(neighbor, 10_000 + j)
    old_found = sum(
        1
        for j, (pair_distance, base, _neighbor) in enumerate(pairs)
        if pair_distance == 16 and 10_000 + j in old.query(base, max_distance=16)
    )
    old_recall = old_found / 120
    assert old_recall < 0.05, f"旧单表 d=16 召回应显著低,实测 {old_recall:.4f}"
    assert recall[16] > old_recall + 0.9                  # 量化提升(99% vs ~1%)
    assert int(recall[16] * 120) >= 20 * max(old_found, 1)  # 至少一个数量级


def test_mt_param_sweep_recall_cost_tradeoff() -> None:
    """K / probe_depth 参数扫描的召回-成本权衡数据点(N=2000,d=16 近邻)。

    确定性实测(B=16,默认种子;recall 为 40 个 d=16 对的召回、cost 为
    单查询平均汉明比较数):

        (K, p)   recall   avg_compares
        ( 4, 0)    0.0%        0.1     ← 仅原桶:几乎全漏
        ( 8, 0)    5.0%        0.2
        ( 8, 1)   22.5%        3.5
        ( 8, 2)   82.5%       32.9
        (16, 2)   92.5%       61.2
        (16, 3)  100.0%      268.4     ← 探针半径换召回的边际成本陡增

    断言权衡单调性:同 K 下 p 增 → 召回升且比较升;同 p 下 K 增 → 召回升。
    """
    rng = random.Random(20261016)
    fillers = [_rand_fp(rng) for _ in range(2000)]
    pairs = _plant_pairs(rng, {16: 40})

    configs = ((4, 0), (8, 0), (8, 1), (8, 2), (16, 2), (16, 3))
    sweep: dict[tuple[int, int], tuple[float, float]] = {}
    for tables, probe in configs:
        index = MultiTableLSH(tables=tables, probe_depth=probe)
        for seq, hex64 in enumerate(fillers):
            index.insert(hex64, seq)
        for j, (_d, _base, neighbor) in enumerate(pairs):
            index.insert(neighbor, 3000 + j)
        found = 0
        used = 0
        for j, (_d, base, _neighbor) in enumerate(pairs):
            before = index.compare_calls
            if 3000 + j in index.query(base, max_distance=16):
                found += 1
            used += index.compare_calls - before
        sweep[(tables, probe)] = (found / len(pairs), used / len(pairs))

    assert sweep[(8, 0)][0] < 0.2          # 仅原桶:召回坍塌(实测 5.0%)
    assert sweep[(8, 2)][0] > 0.5          # 开探针:召回抬升(实测 82.5%)
    assert sweep[(16, 3)][0] > 0.95        # 加表加深探针:实测 100%
    # 单调性:probe_depth 增 → 召回不减、比较成本增
    assert sweep[(8, 1)][0] >= sweep[(8, 0)][0]
    assert sweep[(8, 2)][0] >= sweep[(8, 1)][0]
    assert sweep[(16, 3)][0] >= sweep[(16, 2)][0]
    assert sweep[(8, 0)][1] < sweep[(8, 1)][1] < sweep[(8, 2)][1]
    # 同 p 增表数 → 召回不减
    assert sweep[(16, 2)][0] >= sweep[(8, 2)][0]
    # 所有配置仍是亚线性:平均比较远小于全表扫描 2040
    assert all(cost < 2040 for _recall, cost in sweep.values())


def test_mt_telemetry_counters() -> None:
    """遥测:每次查询 phash.mt_lsh_query +1,比较次数累加 phash.mt_lsh_compare。"""
    from netsentinel import telemetry

    index = MultiTableLSH()
    index.insert(H0, "a")
    index.insert(H1, "b")
    telemetry.reset()
    try:
        before = index.compare_calls
        index.query(H0, max_distance=8)
        counters = telemetry.snapshot()["counters"]
        assert counters.get("phash.mt_lsh_query") == 1
        assert counters.get("phash.mt_lsh_compare") == (
            index.compare_calls - before
        )
        index.query(H_FAR, max_distance=0)
        assert telemetry.snapshot()["counters"].get("phash.mt_lsh_query") == 2
    finally:
        telemetry.reset()


def test_mt_concurrent_insert_and_query() -> None:
    """4 线程并发 insert / query:无异常、条目全落地、计数器单调。"""
    index = MultiTableLSH(tables=8, key_bits=16, probe_depth=1)
    errors: list[str] = []
    workers, per_worker = 4, 40

    def worker(tid: int) -> None:
        try:
            rng = random.Random(9500 + tid)
            local = []
            for i in range(per_worker):
                hex64 = _rand_fp(rng)
                index.insert(hex64, tid * 1000 + i)
                local.append(hex64)
                if i % 10 == 0:
                    assert index.query(local[-1], max_distance=0)
        except Exception as exc:  # noqa: BLE001 - 线程内异常汇总断言
            errors.append(f"线程 {tid}:{exc}")

    threads = [
        threading.Thread(target=worker, args=(t,)) for t in range(workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    assert index.stats()["entries"] == workers * per_worker


def test_multitable_selfcheck_report() -> None:
    """multitable_selfcheck(A195 契约):d=4 保证域全召回 + 比较比例 ≤ 10%。"""
    report = multitable_selfcheck()
    assert report["name"] == "phash_lsh_mt"
    assert report["metric"] == "query_compare_ratio_vs_full_scan"
    assert report["baseline"] == 1.0
    assert 0 < report["value"] <= 0.1
    assert report["recall_d4"] == 1.0


# ---------------------------------------------------------------------------
# A229:name 实例标识泛化(多哈希生产接线:主 phash 与 mirror 各一实例)
# ---------------------------------------------------------------------------


def test_mt_name_parameter_and_backward_compatible_signature() -> None:
    """A229 name 实例标识:缺省 ``""``(旧构造完全兼容,stats 键集不变);
    name 不进参数指纹——不影响置换 / 探针 / 桶布局 / 持久化格式;
    旧位置参数签名(tables, key_bits, probe_depth, seed, db_path)逐位不变,
    name 追加在 db_path 之后。"""
    plain = MultiTableLSH()
    assert plain.name == ""
    named = MultiTableLSH(name="mirror")
    assert named.name == "mirror"
    with pytest.raises(ValueError, match="name"):
        MultiTableLSH(name=123)  # type: ignore[arg-type]

    # 旧签名(位置参数逐位不变)依旧可用
    legacy = MultiTableLSH(4, 12, 1, 20261003, None)
    assert legacy.tables == 4 and legacy.key_bits == 12
    assert legacy.probe_depth == 1 and legacy.db_path is None
    assert legacy.name == ""

    # name 不影响置换 / 探针 / 桶布局(实例隔离由 db_path 承担)
    value = int("0f1e2d3c4b5a6978", 16)
    assert named._table_keys(value) == plain._table_keys(value)
    assert named._probe_deltas == plain._probe_deltas
    # stats 键集合与旧口径完全一致(name 不入 stats,旧断言零破坏)
    assert set(named.stats()) == set(plain.stats()) == {
        "entries", "tables", "key_bits", "probe_depth", "buckets", "avg_bucket",
    }
    # repr 携带实例标识(多实例运维定位)
    assert "mirror" in repr(named) and "MultiTableLSH" in repr(named)


def test_mt_multi_instance_isolated_persistence(tmp_path: Any) -> None:
    """主 phash 与 mirror 两实例并存:各自 db_path(沿 ``.mtlsh`` /
    ``.mtlsh.mirror`` 惯例派生)独立落盘,重载后条目 / 查询零串扰
    (多哈希生产接线的前提,A229)。"""
    main_db = str(tmp_path / "phash.db.mtlsh")
    mirror_db = main_db + ".mirror"
    main = MultiTableLSH(tables=4, key_bits=12, probe_depth=1, db_path=main_db)
    mirror = MultiTableLSH(
        tables=4, key_bits=12, probe_depth=1, db_path=mirror_db, name="mirror"
    )
    try:
        main.insert(H0, {"sha256": SHA_A, "site": SITE_A})
        main.insert(H1, {"sha256": SHA_B, "site": SITE_A})
        mirror.insert(H2, {"sha256": SHA_C, "site": SITE_B, "kind": "mirror"})
        assert main.flush() == 2
        assert mirror.flush() == 1
    finally:
        main.close()
        mirror.close()

    r_main = MultiTableLSH(tables=4, key_bits=12, probe_depth=1, db_path=main_db)
    r_mirror = MultiTableLSH(
        tables=4, key_bits=12, probe_depth=1, db_path=mirror_db, name="mirror"
    )
    try:
        assert r_main.stats()["entries"] == 2
        assert r_mirror.stats()["entries"] == 1
        # 查询零串扰:mirror 条目不在主索引、主条目不在镜像索引
        assert r_main.query(H2, max_distance=0) == []
        assert r_mirror.query(H0, max_distance=0) == []
        (hit,) = r_mirror.query(H2, max_distance=0)
        assert hit["kind"] == "mirror" and hit["sha256"] == SHA_C
        assert {p["sha256"] for p in r_main.query(H0, max_distance=2)} == {
            SHA_A, SHA_B,
        }
    finally:
        r_main.close()
        r_mirror.close()
