# -*- coding: utf-8 -*-
"""G3 创世纪升级波验收:intel/phash 分带预过滤 + phash_lsh 桶键核实。

纯离线、固定种子、零外呼;不依赖 Pillow(纯 hex + sqlite + 内存索引)。
覆盖(对应 G3 升级目标):

1. **鸽笼性质**:_diff_band_count 已知向量 + 随机性质(非零带数 ≤ 汉明
   距离,即"非零带数 > max_distance ⇒ 距离 > max_distance"的剪枝安全
   性);_fast_hex64 接受域与 hamming 内部归一化逐字一致(变长 / 非法
   字符 / unicode 数字 / 0x 前缀一律回退原口径);
2. **等价性**:随机库(固定种子:常规行 + d∈{0,1,2,3,4,5,8} 边界邻居 +
   重复指纹 + 十类脏行直写 sqlite)上,find_similar(phash / mirror /
   pyramid 三 kind × max_distance {0,1,2,3,4,5,8,12} × exclude 变体 ×
   非 16hex 查询)与升级前口径的**独立暴力实现逐位一致**(含顺序),
   且 phash.dirty_hash / phash.registry_hit 遥测逐调用对齐;边界距离
   d-1/d/d+1 的命中集合精确;截断(默认 200 与 monkeypatch 小上限)及
   warning 对齐;
3. **操作计数**:find_exact_compares 与独立口径的非零带数统计精确相等;
   d ∈ {0,2,3} 显著剪枝(403 行库 d=0 仅 1 次精确计算),d ≥ 4(≥ 带数)
   退化为全表计算(诚实边界锁定);慢路径(pyramid)同样计数;
4. **phash_lsh 桶键核实(升级目标二)**:单表带键为 64bit 的不交双射
   覆盖(重构成原值)、桶位总数 = 条目 × 带数(无重复桶位)、多带候选
   去重(compare_calls 按条目恰一次);多表探针掩码互异、probe_calls
   恰为 K × 掩码数、桶员总数 = 条目 × 表数且经 flush / 重载保持。
   **结论:未发现重复的桶键计算,按契约 §0.6 放弃改动,以用例锁定**。
"""
from __future__ import annotations

import logging
import random
import sqlite3
from typing import Any

import pytest

import netsentinel.intel.phash as phash_mod
from netsentinel import telemetry
from netsentinel.intel.phash import (
    MIRROR_HASH_HEX_LEN,
    PYRAMID_HASH_HEX_LEN,
    PhashRegistry,
    _diff_band_count,
    _fast_hex64,
    hamming,
)
from netsentinel.intel.phash_lsh import (
    BITS,
    DEFAULT_BANDS,
    LSHIndex,
    MultiTableLSH,
)

SITE = "https://g3.example/pics/"
H0 = "0" * 16


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def _fp(rng: random.Random) -> str:
    """随机 64bit 指纹(16 位 hex)。"""
    return f"{rng.getrandbits(BITS):016x}"


def _flip(hex64: str, positions: list[int]) -> str:
    """翻转 hex64 指定二进制位(0 = 最低位),返回新 hex。"""
    value = int(hex64, 16)
    for pos in positions:
        value ^= 1 << pos
    return f"{value:016x}"


def _nz_bands(a: str, b: str) -> int:
    """**独立实现**的"异或后非零 16bit 带数"。

    刻意不 import 被测的 _diff_band_count,作为等价性 / 操作计数对照的
    独立口径(两处实现漂移即失守)。
    """
    xor = int(a, 16) ^ int(b, 16)
    return sum(1 for i in range(4) if (xor >> (16 * i)) & 0xFFFF)


def _dirty_row_values() -> list[tuple[str, str, str, str]]:
    """十类脏行 / 交互行(直写 sqlite 绕过校验):(sha, phash, mirror, pyramid)。

    覆盖:非法字符、15/17 hex 变长、mirror/pyramid 脏值,以及分带短路与
    脏行语义的关键交互——"mirror 合法但远(会被短路)+ phash 空 / 非法":
    旧口径 distances=[远距] 非空 → **不计**脏行遥测,新口径必须逐位保持。
    """
    far_mirror = _flip(H0, [1, 20, 40, 60])  # 距 H0 = 4,四带全非零
    return [
        ("e" * 64, "zz" * 8, "", ""),                        # phash 非法
        ("d" * 64, "0" * 15, "", ""),                        # phash 15 hex
        ("c" * 64, "0" * 17, "", ""),                        # phash 17 hex
        ("b" * 64, "0" * 16, "g" * 16, ""),                  # mirror 非法
        ("a" * 64, "0" * 16, "0" * 15, ""),                  # mirror 15 hex
        ("9" * 64, "0" * 16, "", "z" * 27),                  # pyramid 非法
        ("8" * 64, "0" * 16, "", "0" * 26),                  # pyramid 26 hex
        ("7" * 64, "", far_mirror, ""),                      # phash 空 + mirror 远
        ("6" * 64, "toomanybits" * 20, far_mirror, ""),      # phash 非法 + mirror 远
        ("5" * 64, "toomanybits" * 20, "", ""),              # 纯脏行
    ]


def _build_random_library(db: str, seed: int = 20261020) -> list[tuple[str, str, str, str]]:
    """固定种子随机库:常规行 + 边界距离邻居 + 重复指纹 + 极端值 + 脏行。

    返回合法行的 (sha, phash, mirror, pyramid) 视图(脏行另直写)。
    """
    rng = random.Random(seed)
    reg = PhashRegistry(db)
    rows: list[tuple[str, str, str, str]] = []
    seq = 0

    def add(phash_hex: str, mirror: str | None, pyramid: str | None) -> None:
        nonlocal seq
        sha = f"{seq:064x}"
        seq += 1
        reg.register(sha, phash_hex, SITE, mirror_hash=mirror, pyramid_hash=pyramid)
        rows.append((sha, phash_hex, mirror or "", pyramid or ""))

    bases = [_fp(rng) for _ in range(60)]
    for base in bases:
        mirror = _fp(rng) if rng.random() < 0.6 else None
        pyramid = f"{rng.getrandbits(108):027x}" if rng.random() < 0.5 else None
        add(base, mirror, pyramid)
    # 边界距离邻居(d ∈ {0,1,2,3,4,5,8},翻转位随机散布跨带)
    for d in (0, 1, 2, 3, 4, 5, 8):
        add(_flip(bases[d * 2], rng.sample(range(BITS), d)), None, None)
        add(_fp(rng), _flip(bases[d * 2 + 1], rng.sample(range(BITS), d)), None)
        # pyramid 邻居:三层各翻 d 位 → 金字塔距离 = d(每层同距,min = d)
        pbase = f"{rng.getrandbits(108):027x}"
        pv = int(pbase, 16)
        for _ in range(d):
            layer = rng.randrange(3)
            pv ^= 1 << (36 * layer + rng.randrange(36))
        add(_fp(rng), None, f"{pv:027x}")
    # 重复指纹(同 phash 不同 sha → 并列排序稳定性)+ 极端值
    add(bases[0], None, None)
    add(bases[0], None, None)
    add(H0, H0, "0" * 27)
    add("f" * 16, "f" * 16, "f" * 27)
    reg.close()

    conn = sqlite3.connect(db)
    for sha, p, m, pyr in _dirty_row_values():
        conn.execute(
            "INSERT INTO hashes (sha256, phash, site_url, verdict_tag, created_at, "
            "mirror_hash, pyramid_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sha, p, "https://dirty.example/", "", "2026-01-01T00:00:00+00:00", m, pyr),
        )
    conn.commit()
    conn.close()
    return rows


def _reference_find_similar(
    db: str,
    phash_hex: str,
    max_distance: int = 8,
    *,
    exclude_sha256: str = "",
    hash_kind: str = "phash",
    cap: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """升级前口径的独立暴力实现(直连 sqlite 读全表,逐行逐列精确比较)。

    与 G3 改造前的 find_similar 逐语句对齐(归一化 / 回退 / 脏行 / min /
    排序 / 截断);返回 (结果列表, 脏行计数)——脏行计数用于对齐
    ``phash.dirty_hash`` 遥测口径。cap 缺省取被测模块当前的
    MAX_FIND_RESULTS(monkeypatch 后亦随动)。
    """
    from netsentinel.intel.phash import _pyramid_distance_hex

    query = str(phash_hex).strip().lower()
    expected_len = {
        "phash": 0,
        "mirror": MIRROR_HASH_HEX_LEN,
        "pyramid": PYRAMID_HASH_HEX_LEN,
    }[hash_kind]
    if expected_len and len(query) != expected_len:
        raise ValueError("参考实现:查询长度不符(测试数据构造错误)")
    compare = _pyramid_distance_hex if hash_kind == "pyramid" else hamming
    primary = {"phash": "phash", "mirror": "mirror_hash", "pyramid": "pyramid_hash"}[hash_kind]
    columns = ("phash",) if hash_kind == "phash" else (primary, "phash")
    exclude = str(exclude_sha256 or "").strip().lower()
    if cap is None:
        cap = phash_mod.MAX_FIND_RESULTS

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT sha256, phash, site_url, mirror_hash, pyramid_hash FROM hashes"
        ).fetchall()
    finally:
        conn.close()

    results: list[dict[str, Any]] = []
    dirty_rows = 0
    for row in rows:
        sha = str(row["sha256"])
        if exclude and sha == exclude:
            continue
        distances: list[int] = []
        dirty = False
        for column in columns:
            value = str(row[column] or "").strip().lower()
            if not value:
                if column == "phash":
                    dirty = True
                continue
            try:
                distances.append(compare(query, value))
            except ValueError:
                dirty = True
        if not distances:
            if dirty:
                dirty_rows += 1
            continue
        distance = min(distances)
        if distance <= max_distance:
            results.append(
                {"sha256": sha, "site": str(row["site_url"]), "distance": distance}
            )
    results.sort(key=lambda item: (item["distance"], item["sha256"]))
    del results[cap:]
    return results, dirty_rows


def _assert_parity(
    reg: PhashRegistry,
    db: str,
    query: str,
    max_distance: int,
    *,
    exclude_sha256: str = "",
    hash_kind: str = "phash",
) -> list[dict[str, Any]]:
    """断言 find_similar 与暴力参考逐位一致 + 遥测逐调用对齐,返回结果。"""
    counters = telemetry.snapshot()["counters"]
    before_dirty = counters.get("phash.dirty_hash", 0)
    before_hit = counters.get("phash.registry_hit", 0)
    got = reg.find_similar(
        query, max_distance, exclude_sha256=exclude_sha256, hash_kind=hash_kind
    )
    expected, dirty_count = _reference_find_similar(
        db, query, max_distance,
        exclude_sha256=exclude_sha256, hash_kind=hash_kind,
    )
    assert got == expected, (
        f"{hash_kind} 查询 {query!r} @d={max_distance} 与暴力扫描不一致:\n"
        f"  got={got}\n  expected={expected}"
    )
    after = telemetry.snapshot()["counters"]
    assert after.get("phash.dirty_hash", 0) - before_dirty == dirty_count, (
        f"dirty_hash 遥测不对齐:期望 {dirty_count},"
        f"实际 {after.get('phash.dirty_hash', 0) - before_dirty}"
    )
    assert after.get("phash.registry_hit", 0) - before_hit == len(expected), (
        "registry_hit 遥测不对齐"
    )
    return got


# ---------------------------------------------------------------------------
# 1. 鸽笼性质与快速判定接受域
# ---------------------------------------------------------------------------


def test_diff_band_count_known_vectors() -> None:
    """已知向量:全零 0 带;单带掩码 1;跨带各 1 位 → 4;全一 → 4。"""
    assert _diff_band_count(0) == 0
    assert _diff_band_count(1) == 1
    assert _diff_band_count(0xFFFF) == 1                      # 带 0 满
    assert _diff_band_count(0x1_0000) == 1                    # 带 1 最低位
    assert _diff_band_count(1 << 63) == 1                     # 带 3 最高位
    assert _diff_band_count(0x0001_0001_0001_0001) == 4       # 四带各 1 位
    assert _diff_band_count(0xFFFF_0000_FFFF_0000) == 2
    assert _diff_band_count((1 << BITS) - 1) == 4


def test_pigeonhole_property_random_pairs() -> None:
    """随机性质(剪枝安全性的全部依据):非零带数 ≤ 汉明距离。

    每个非零带至少含 1 个翻转位且不同带的翻转位不重叠,故带数 ≤ 距离;
    逆否即"非零带数 > max_distance ⇒ 距离 > max_distance ⇒ 必不命中"。
    """
    rng = random.Random(20261013)
    for _ in range(800):
        a, b = rng.getrandbits(BITS), rng.getrandbits(BITS)
        xor = a ^ b
        assert _diff_band_count(xor) == _nz_bands(f"{a:016x}", f"{b:016x}")
        assert _diff_band_count(xor) <= xor.bit_count()
        for max_distance in range(0, BITS, 7):
            if xor.bit_count() <= max_distance:
                assert _diff_band_count(xor) <= max_distance


def test_fast_hex64_acceptance_mirror_of_normalize() -> None:
    """_fast_hex64 接受域 = hamming 对已归一化值的接受域:恰 16 位小写
    ASCII hex 才走快速路径;变长 / 非法字符 / 大写(未归一化)/ unicode
    数字 / 0x 前缀 / 内部空白一律 None 回退原口径(脏行语义逐位保持)。"""
    assert _fast_hex64("0" * 16) == 0
    assert _fast_hex64("0f1e2d3c4b5a6978") == 0x0F1E2D3C4B5A6978
    assert _fast_hex64("f" * 16) == (1 << BITS) - 1
    for bad in (
        "", "0" * 15, "0" * 17, "g" * 16, "0x" + "0" * 14,
        "A" * 16, "١" * 16, "0 0000000000000", "+1234567890abcd",
        "1_234567890abcde",
    ):
        assert _fast_hex64(bad) is None, f"{bad!r} 不应走快速路径"


# ---------------------------------------------------------------------------
# 2. 等价性:预过滤 vs 全量暴力(随机库 + 边界距离 + 脏行 + 截断)
# ---------------------------------------------------------------------------


def test_find_similar_matches_bruteforce_all_kinds(tmp_path: Any) -> None:
    """随机库全量对照:三 kind × 八档 max_distance × exclude 变体 × 非
    16hex 查询,输出(含顺序)与升级前暴力口径逐位一致,遥测对齐。"""
    db = str(tmp_path / "phash.db")
    rows = _build_random_library(db)
    reg = PhashRegistry(db)
    rng = random.Random(20261021)
    exclude_sha = rows[0][0]
    distances = (0, 1, 2, 3, 4, 5, 8, 12)
    telemetry.reset()
    try:
        # phash kind:库内指纹 + 随机 + 极端 + 变长查询(变长全脏也须一致)
        phash_queries = [r[1] for r in rows[::9]] \
            + [_fp(rng) for _ in range(4)] + [H0, "f" * 16, "ab"]
        for i, query in enumerate(phash_queries):
            for d in distances:
                _assert_parity(
                    reg, db, query, d,
                    exclude_sha256=exclude_sha if i % 3 == 0 else "",
                )
        # mirror kind:库内 mirror 值 + 随机 + 极端
        mirror_queries = [r[2] for r in rows if r[2]][::3] \
            + [_fp(rng) for _ in range(3)] + [H0, "f" * 16]
        for query in mirror_queries:
            for d in distances:
                _assert_parity(reg, db, query, d, hash_kind="mirror")
        # pyramid kind(保持原路径):库内 27hex + 三层翻转变体 + 随机
        pyramid_bases = [r[3] for r in rows if r[3]]
        pyramid_queries = pyramid_bases[::3]
        for base in pyramid_bases[:4]:
            value = int(base, 16)
            for _ in range(2):  # 三层各翻 1 位 → 金字塔距离 1
                layer = rng.randrange(3)
                value ^= 1 << (36 * layer + rng.randrange(36))
            pyramid_queries.append(f"{value:027x}")
        pyramid_queries += [f"{rng.getrandbits(108):027x}" for _ in range(3)]
        pyramid_queries += ["0" * 27, "f" * 27]
        for query in pyramid_queries:
            for d in (0, 1, 3, 11, 36):
                _assert_parity(reg, db, query, d, hash_kind="pyramid")
    finally:
        telemetry.reset()
        reg.close()


def test_boundary_distances_d_minus_at_plus_one(tmp_path: Any) -> None:
    """边界距离专项:距查询恰 d-1 / d / d+1 的行,命中集合在 d 处精确切割
    (含 = d,不含 d+1),与暴力口径一致;d 扫过 0..5 覆盖剪枝有效域内外。"""
    db = str(tmp_path / "phash.db")
    reg = PhashRegistry(db)
    base = "0123456789abcdef"
    bv = int(base, 16)
    for d in range(0, 6):
        for delta in range(0, 7):  # d-1 / d / d+1 及邻域
            sha = f"{(d * 10 + delta):064x}"
            reg.register(sha, f"{bv ^ ((1 << delta) - 1):016x}", SITE)
    reg.close()

    reg = PhashRegistry(db)
    try:
        for d in range(0, 6):
            hits = reg.find_similar(base, max_distance=d)
            shas = {h["sha256"] for h in hits}
            expected = {
                f"{(dd * 10 + delta):064x}"
                for dd in range(6) for delta in range(7) if delta <= d
            }
            assert shas == expected, f"d={d} 边界命中集合失守"
            assert all(h["distance"] <= d for h in hits)
    finally:
        reg.close()


def test_truncation_parity_and_warning(
    tmp_path: Any, monkeypatch: Any, caplog: Any
) -> None:
    """截断触发:250 行同指纹(不同 sha)d=0 全命中 → 默认截 200 条且记
    warning;monkeypatch 小上限后与参考(同 cap)逐位一致;exclude 生效。"""
    db = str(tmp_path / "phash.db")
    target = "1234567890abcdef"
    reg = PhashRegistry(db)
    for i in range(250):
        reg.register(f"{i:064x}", target, f"https://s{i}.example/")
    first_sha = "0" * 64
    reg.close()

    reg = PhashRegistry(db)
    try:
        got = reg.find_similar(target, max_distance=0)
        expected, _ = _reference_find_similar(db, target, 0)
        assert len(got) == len(expected) == phash_mod.MAX_FIND_RESULTS == 200
        assert got == expected
        # 排除一条后仍截断到 200(排序稳定,sha 升序前 200)
        got = reg.find_similar(target, max_distance=0, exclude_sha256=first_sha)
        expected, _ = _reference_find_similar(
            db, target, 0, exclude_sha256=first_sha
        )
        assert got == expected and first_sha not in {h["sha256"] for h in got}

        monkeypatch.setattr(phash_mod, "MAX_FIND_RESULTS", 7)
        with caplog.at_level(logging.WARNING, logger="netsentinel.intel.phash"):
            got = reg.find_similar(target, max_distance=0)
        expected, _ = _reference_find_similar(db, target, 0)  # cap 随 monkeypatch
        assert len(got) == len(expected) == 7
        assert got == expected
        assert any("截断" in r.message for r in caplog.records)
    finally:
        reg.close()


def test_mirror_band_skip_dirty_telemetry_interplay(tmp_path: Any) -> None:
    """分带短路 × 脏行遥测的关键交互(逐位保持旧口径):

    - mirror 合法但远(d ≤ 3 时被短路)+ phash 非法 / 空:旧口径
      distances=[远距] 非空 → **不**计 phash.dirty_hash、不命中;
    - mirror 为空 + phash 非法:旧口径 distances=[] + dirty → 计 1;
    - mirror 合法近距 + phash 非法:命中且不计脏(与短路无关的旧语义)。
    """
    db = str(tmp_path / "phash.db")
    reg = PhashRegistry(db)
    near_sha, ok_sha = "1" * 64, "2" * 64
    reg.register(near_sha, "0" * 16, SITE, mirror_hash=H0)   # mirror 距查询 0
    reg.register(ok_sha, H0, SITE)                           # 正常行
    reg.close()
    far_mirror = _flip(H0, [1, 20, 40, 60])                  # 距 4、四带全非零
    conn = sqlite3.connect(db)
    for sha, p, m in (
        ("a" * 64, "toomanybits" * 20, far_mirror),  # 远 mirror + 脏 phash
        ("b" * 64, "", far_mirror),                  # 远 mirror + 空 phash
        ("c" * 64, "toomanybits" * 20, ""),          # 空 mirror + 脏 phash
    ):
        conn.execute(
            "INSERT INTO hashes (sha256, phash, site_url, verdict_tag, created_at, "
            "mirror_hash, pyramid_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sha, p, "https://dirty.example/", "", "2026-01-01T00:00:00+00:00", m, ""),
        )
    conn.commit()
    conn.close()

    reg = PhashRegistry(db)
    telemetry.reset()
    try:
        # d=2:far_mirror 非零带数 4 > 2 → 短路;两行交互行均不计脏
        hits = reg.find_similar(H0, max_distance=2, hash_kind="mirror")
        assert {h["sha256"] for h in hits} == {near_sha, ok_sha}
        assert telemetry.snapshot()["counters"].get("phash.dirty_hash", 0) == 1  # 仅 c 行
        # d=8:不再短路,far_mirror 精确计算(距 4 ≤ 8 → 命中);交互行
        # distances 非空 → 依旧不计脏;仅 "c" 行(空 mirror + 脏 phash)再计 1
        counters = telemetry.snapshot()["counters"]
        before = counters.get("phash.dirty_hash", 0)
        hits = reg.find_similar(H0, max_distance=8, hash_kind="mirror")
        assert {h["sha256"] for h in hits} == {"a" * 64, "b" * 64, near_sha, ok_sha}
        assert telemetry.snapshot()["counters"].get("phash.dirty_hash", 0) == before + 1
    finally:
        telemetry.reset()
        reg.close()


# ---------------------------------------------------------------------------
# 3. 操作计数:预过滤比较次数对照(红线 31 口径,不依赖墙钟)
# ---------------------------------------------------------------------------


def test_prefilter_operation_counts_prune_and_boundary(tmp_path: Any) -> None:
    """操作计数:精确计算数 == 非零带数 ≤ max_distance 的行数(独立口径);

    - d=0:403 行库仅 1 次(同指纹行)——剪枝率 > 99%;
    - d ∈ {2,3}:显著剪枝且计数精确;
    - d ≥ 4(≥ 带数):非零带数 ≤ 4 恒成立 → 无剪枝、全表计算(诚实边界);
    - 计数 ≥ 命中数(不漏计)。
    """
    db = str(tmp_path / "phash.db")
    rng = random.Random(20261022)
    reg = PhashRegistry(db)
    total = 400
    stored: list[str] = []
    for i in range(total):
        stored.append(_fp(rng))
        reg.register(f"{i:064x}", stored[-1], SITE)
    query = _fp(rng)
    qv = int(query, 16)
    near = [f"{qv ^ ((1 << d) - 1):016x}" for d in (0, 1, 2)]  # 距 0/1/2
    for j, hex64 in enumerate(near):
        reg.register(f"{(total + j):064x}", hex64, SITE)
    all_hashes = stored + near

    assert reg.find_exact_compares == 0  # 初始为零
    for d in (0, 2, 3):
        before = reg.find_exact_compares
        hits = reg.find_similar(query, max_distance=d)
        used = reg.find_exact_compares - before
        expected_compares = sum(1 for h in all_hashes if _nz_bands(query, h) <= d)
        assert used == expected_compares, f"d={d} 精确计算数 {used} ≠ 独立口径 {expected_compares}"
        assert expected_compares < total, f"d={d} 应有剪枝(全表 {total + 3} 行)"
        assert len(hits) == sum(1 for h in near if hamming(query, h) <= d)
        assert used >= len(hits)
    # d=0 极限:只有同指纹行进入精确计算
    before = reg.find_exact_compares
    reg.find_similar(query, max_distance=0)
    assert reg.find_exact_compares - before == 1
    # 诚实边界:d ≥ 带数(4)→ 无剪枝,全表逐行精确计算,行为与旧口径一致
    for d in (4, 8, 64):
        before = reg.find_exact_compares
        reg.find_similar(query, max_distance=d)
        assert reg.find_exact_compares - before == total + 3, f"d={d} 不应有剪枝"
    reg.close()


def test_counter_counts_slow_path_and_dirty_only(tmp_path: Any) -> None:
    """计数器覆盖慢路径:pyramid(原路径)可比行逐行计数;纯脏库零计数。"""
    db = str(tmp_path / "phash.db")
    reg = PhashRegistry(db)
    p1 = "000000001" + "000000002" + "000000003"
    p2 = "000000001" + "000000003" + "000000003"  # 与 p1 第二层差 1 bit → 距 0
    reg.register("1" * 64, "0" * 16, SITE, pyramid_hash=p1)
    reg.register("2" * 64, "0" * 16, SITE, pyramid_hash=p2)
    reg.register("3" * 64, "0" * 16, SITE)  # 无 pyramid → 回退 phash(27vs16 脏)
    assert reg.find_exact_compares == 0
    hits = reg.find_similar(p1, max_distance=1, hash_kind="pyramid")
    assert reg.find_exact_compares == 2  # 两个可比 pyramid 列;回退列全脏不计
    assert {h["sha256"] for h in hits} == {"1" * 64, "2" * 64}
    reg.close()

    db2 = str(tmp_path / "dirty.db")
    reg2 = PhashRegistry(db2)
    reg2.register("9" * 64, H0, SITE)
    reg2.close()
    conn = sqlite3.connect(db2)
    for sha in ("8" * 64, "7" * 64):
        conn.execute(
            "INSERT INTO hashes (sha256, phash, site_url, verdict_tag, created_at, "
            "mirror_hash, pyramid_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sha, "garbage", SITE, "", "2026-01-01T00:00:00+00:00", "", ""),
        )
    conn.commit()
    conn.close()
    reg3 = PhashRegistry(db2)
    try:
        assert reg3.find_similar("abc", max_distance=1) == []  # 变长查询全脏
        assert reg3.find_exact_compares == 0
    finally:
        reg3.close()


# ---------------------------------------------------------------------------
# 4. phash_lsh 桶键核实(升级目标二:核实有无重复,无则放弃并锁定)
# ---------------------------------------------------------------------------


def test_lsh_band_keys_disjoint_bijective_cover() -> None:
    """单表带键提取:4 带不交且铺满 64bit(重构成原值)——带间无重叠 /
    重复计算,键值域恰为 [0, 2^16)。"""
    rng = random.Random(20261023)
    index = LSHIndex()
    for _ in range(300):
        value = rng.getrandbits(BITS)
        keys = index._band_keys(value)
        rebuilt = sum(
            key << shift
            for key, (shift, _width) in zip(keys, index._layout)
        )
        assert rebuilt == value, "带键不是 64bit 的不交双射覆盖"
        assert len(keys) == DEFAULT_BANDS == 4
        assert all(0 <= key < (1 << 16) for key in keys)


def test_lsh_bucket_placements_no_duplicate_members() -> None:
    """桶位无重复:条目在每带恰占一个桶位,总桶位 = 条目数 × 带数
    (含跨条目同键碰撞载荷 0x0001000100010001×k,四带同值的压力构造)。"""
    index = LSHIndex()
    n = 120
    for k in range(1, n + 1):
        index.insert(f"{0x0001000100010001 * k:016x}", k)
    placements = sum(
        len(members) for band in index._buckets for members in band.values()
    )
    assert placements == n * DEFAULT_BANDS
    assert index.stats()["entries"] == n


def test_lsh_query_compares_each_candidate_once() -> None:
    """多带候选去重:与查询共享 3 带的近邻 + 精确命中各只比较一次;
    四带全毁的 d=4 行零候选零比较(结构性边界,与既有测试口径一致)。"""
    index = LSHIndex()
    base = "0f1e2d3c4b5a6978"
    index.insert(base, "exact")
    index.insert(_flip(base, [1]), "near")             # 共享带 1/2/3 → 3 带命中
    index.insert(_flip(base, [1, 17, 33, 49]), "miss")  # 四带全毁 → 非候选
    before = index.compare_calls
    hits = index.query(base, max_distance=8)
    assert sorted(hits) == ["exact", "near"]
    assert index.compare_calls - before == 2


def test_mt_probe_deltas_unique_and_probe_calls_exact() -> None:
    """多表探针掩码互异([0] + 汉明球逐层组合,组合数公式核验);
    单查询桶查找恰为 K × 掩码数——无重复探针、无遗漏。"""
    from math import comb

    for key_bits, depth in ((16, 2), (12, 1), (8, 3)):
        index = MultiTableLSH(tables=4, key_bits=key_bits, probe_depth=depth)
        deltas = index._probe_deltas
        expected_count = sum(comb(key_bits, i) for i in range(depth + 1))
        assert len(deltas) == expected_count
        assert deltas[0] == 0
        assert len(set(deltas)) == len(deltas), "探针掩码存在重复"
        assert all(0 <= d < (1 << key_bits) for d in deltas)

    index = MultiTableLSH(tables=8, key_bits=16, probe_depth=2)
    index.insert(H0, "a")
    before = index.probe_calls
    index.query(H0, max_distance=0)
    assert index.probe_calls - before == 8 * len(index._probe_deltas)


def test_mt_bucket_members_unique_and_survive_reload(tmp_path: Any) -> None:
    """多表桶员无重复:总桶员 = 条目 × 表数;经 flush / 惰性重载保持
    (sqlite 联合主键 (table_no, bucket_key, seq) 兜底唯一性)。"""
    db = str(tmp_path / "mt.db")
    rng = random.Random(20261024)
    tables = 4
    index = MultiTableLSH(tables=tables, key_bits=12, probe_depth=1, db_path=db)
    n = 150
    for i in range(n):
        index.insert(_fp(rng), i)

    def member_count(idx: MultiTableLSH) -> int:
        return sum(
            1 if type(m) is int else len(m)
            for t in idx._tables for m in t.values()
        )

    assert member_count(index) == n * tables
    assert index.flush() == n
    index.close()

    reloaded = MultiTableLSH(tables=tables, key_bits=12, probe_depth=1, db_path=db)
    try:
        assert reloaded.stats()["entries"] == n
        assert member_count(reloaded) == n * tables  # 重载后仍无重复桶员
    finally:
        reloaded.close()
