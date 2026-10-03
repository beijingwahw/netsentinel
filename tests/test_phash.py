"""A43 netsentinel.intel.phash 感知哈希库测试。

纯离线、只用 tmp_path 落图与落库(Pillow 造图)。覆盖:

- phash / ahash 输出格式(16 位小写 hex)与同文件两次调用一致(决定性);
- 同图 200x200 vs 缩放 160x160 vs 轻微亮度变化 → hamming ≤ 10;
  完全不同构图(纯色 vs 大块噪声)→ 距离 > 20;
- hamming 自身 0、已知向量、长度不等 / 非法 hex 抛中文 ValueError;
- 文件不存在 / 损坏 → ValueError;PIL 屏蔽分支的降级语义;
- PhashRegistry:register / find_similar 往返、max_distance 过滤、距离
  升序排序、同 sha256 排除、UPSERT 覆盖、非法入参校验、损坏重建、
  跨实例持久化、多线程并发 register / 查询;
- 端到端近重复流:登记原图后用缩放版的 phash 能查回原记录。
"""
from __future__ import annotations

import hashlib
import math
import random
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

PIL = pytest.importorskip("PIL", reason="需要 Pillow 才能测试感知哈希")
from PIL import Image, ImageDraw  # noqa: E402

from netsentinel.intel.phash import PhashRegistry, ahash, hamming, phash

SITE_A = "https://bad-a.example/gallery/"
SITE_B = "https://mirror-b.example/pics/"


# ---------------------------------------------------------------------------
# 造图辅助:低频结构"照片"、大块噪声、纯色
# ---------------------------------------------------------------------------


def _structured_photo(size: int) -> Image.Image:
    """构造有低频结构的灰度"照片":平滑双向渐变 + 三个大块几何形状。

    最大像素值 210,后续 1.08 倍亮度提升不会触发 255 截断。
    """
    rng = random.Random(20261001)
    img = Image.new("L", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            base = 96 + 48 * math.sin(x * 2 * math.pi / size + 0.7) \
                + 32 * math.cos(y * 2 * math.pi / size + 1.9)
            px[x, y] = max(0, min(255, int(base)))
    draw = ImageDraw.Draw(img)
    draw.ellipse((size * 0.15, size * 0.20, size * 0.55, size * 0.70), fill=210)
    draw.rectangle((size * 0.55, size * 0.10, size * 0.90, size * 0.45), fill=40)
    draw.ellipse((size * 0.50, size * 0.55, size * 0.95, size * 0.95), fill=150)
    for _ in range(size):  # 少量散点噪声,避免完全可预测
        px[rng.randrange(size), rng.randrange(size)] = rng.randrange(256)
    return img


def _blocky_noise(size: int, seed: int = 99) -> Image.Image:
    """8x8 大块随机灰度噪声:与平滑渐变构图完全不同。"""
    rng = random.Random(seed)
    img = Image.new("L", (size, size))
    px = img.load()
    block = max(1, size // 8)
    for by in range(0, size, block):
        for bx in range(0, size, block):
            value = rng.randrange(256)
            for y in range(by, min(by + block, size)):
                for x in range(bx, min(bx + block, size)):
                    px[x, y] = value
    return img


def _save(img: Image.Image, path: Path) -> str:
    img.save(path, format="PNG")
    return str(path)


def _file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_row(db_path: str, sha: str) -> sqlite3.Row | None:
    """独立连接直读一行,验证落盘内容(不经过被测对象)。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM hashes WHERE sha256 = ?", (sha,)
        ).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 哈希函数:格式 / 决定性 / 不变性 / 区分度
# ---------------------------------------------------------------------------


def test_hash_format_and_deterministic(tmp_path: Any) -> None:
    """phash 与 ahash 均返回 16 位小写 hex,且同文件两次结果一致。"""
    path = _save(_structured_photo(200), tmp_path / "photo.png")
    for func in (phash, ahash):
        h1 = func(path)
        h2 = func(path)
        assert h1 == h2, f"{func.__name__} 同文件两次结果不一致"
        assert len(h1) == 16
        assert h1 == h1.lower()
        int(h1, 16)  # 能被解析为十六进制


def test_same_image_resize_and_brightness_stay_close(tmp_path: Any) -> None:
    """同图缩放(200→160)与轻微亮度变化(×1.08)后哈希距离 ≤ 10。"""
    base = _structured_photo(200)
    p200 = _save(base, tmp_path / "base200.png")
    p160 = _save(base.resize((160, 160), Image.Resampling.LANCZOS),
                 tmp_path / "base160.png")
    pbright = _save(base.point(lambda v: min(255, int(v * 1.08))),
                    tmp_path / "bright.png")

    h200, h160, hbright = phash(p200), phash(p160), phash(pbright)
    assert hamming(h200, h160) <= 10
    assert hamming(h200, hbright) <= 10
    # aHash 同样应保持稳定(均值阈值对整体缩放 / 线性亮度不敏感)
    assert hamming(ahash(p200), ahash(p160)) <= 10
    assert hamming(ahash(p200), ahash(pbright)) <= 10


def test_different_compositions_far_apart(tmp_path: Any) -> None:
    """纯色 vs 大块噪声:构图完全不同,距离 > 20。"""
    flat = _save(Image.new("L", (200, 200), 128), tmp_path / "flat.png")
    noise = _save(_blocky_noise(200), tmp_path / "noise.png")
    assert hamming(phash(flat), phash(noise)) > 20
    assert hamming(ahash(flat), ahash(noise)) > 20
    assert phash(flat) != phash(noise)


def test_hamming_known_values_and_self_zero() -> None:
    """hamming 自身 0;已知向量精确;大小写容忍。"""
    h = "0f1e2d3c4b5a6978"
    assert hamming(h, h) == 0
    assert hamming(h.upper(), h) == 0
    assert hamming("00" * 8, "01" * 8) == 8      # 每字节 1 bit
    assert hamming("f" * 16, "0" * 16) == 64     # 全部 64 bit 不同
    assert hamming("ab", "ab ") == 0             # 首尾空白容忍


def test_hamming_invalid_inputs() -> None:
    """长度不等 / 非法十六进制 → 中文 ValueError。"""
    with pytest.raises(ValueError, match="长度不一致"):
        hamming("0" * 16, "0" * 15)
    with pytest.raises(ValueError, match="长度不一致"):
        hamming("f" * 16, "ff" * 16)
    with pytest.raises(ValueError, match="非法"):
        hamming("zzzzzzzzzzzzzzzz", "0" * 16)
    with pytest.raises(ValueError, match="非法"):
        hamming("", "0" * 16)


def test_missing_or_corrupt_file_raises_chinese(tmp_path: Any) -> None:
    """文件不存在 / 损坏 → ValueError 且为中文提示。"""
    missing = str(tmp_path / "nope.png")
    with pytest.raises(ValueError, match="不存在"):
        phash(missing)
    with pytest.raises(ValueError, match="不存在"):
        ahash(missing)

    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"this is definitely not a png image payload")
    with pytest.raises(ValueError, match="无法解码"):
        phash(str(corrupt))
    with pytest.raises(ValueError, match="无法解码"):
        ahash(str(corrupt))


def test_pil_missing_degrades_to_ahash_path(monkeypatch: Any, tmp_path: Any) -> None:
    """屏蔽 PIL 后 phash 退化为 aHash 路径;无 Pillow 无法解码 → 中文 ValueError。"""
    path = _save(_structured_photo(120), tmp_path / "photo.png")  # 先落盘
    monkeypatch.setitem(sys.modules, "PIL", None)
    with pytest.raises(ValueError, match="Pillow"):
        phash(path)
    with pytest.raises(ValueError, match="Pillow"):
        ahash(path)


# ---------------------------------------------------------------------------
# PhashRegistry:往返 / 过滤 / 排序 / UPSERT / 损坏重建 / 并发
# ---------------------------------------------------------------------------

#: 构造好的已知哈希:距 H0 分别为 0 / 1 / 8 / 64。
H0 = "0" * 16
H1 = "0000000000000001"
H2 = "00000000000000ff"
H_FAR = "f" * 16
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _registry(tmp_path: Any) -> PhashRegistry:
    return PhashRegistry(str(tmp_path / "phash.db"))


def _register_known_set(reg: PhashRegistry) -> None:
    reg.register(SHA_A, H0, SITE_A, "nsfw")
    reg.register(SHA_B, H1, SITE_B)
    reg.register(SHA_C, H2, SITE_A, "suspect")
    reg.register(SHA_D, H_FAR, SITE_B)


def test_registry_find_similar_roundtrip(tmp_path: Any) -> None:
    """register / find_similar 往返:距离正确、过滤正确、按距离升序。"""
    reg = _registry(tmp_path)
    _register_known_set(reg)

    hits = reg.find_similar(H0, max_distance=8)
    # 升序:自身(0)→ SHA_B(1)→ SHA_C(8);远程 H_FAR 被 max_distance 过滤
    assert [h["sha256"] for h in hits] == [SHA_A, SHA_B, SHA_C]
    assert hits[0]["distance"] == 0 and hits[0]["site"] == SITE_A
    assert hits[1]["distance"] == 1 and hits[1]["site"] == SITE_B
    assert hits[2]["distance"] == 8 and hits[2]["site"] == SITE_A
    assert set(hits[0]) == {"sha256", "site", "distance"}

    assert reg.find_similar(H0, max_distance=0) == [
        {"sha256": SHA_A, "site": SITE_A, "distance": 0}
    ]
    assert [h["sha256"] for h in reg.find_similar(H0, max_distance=7)] == [SHA_A, SHA_B]
    # H_FAR 查询排除自身记录后,8 位内无任何近邻(H0 系列距其 64)
    assert reg.find_similar(H_FAR, max_distance=8, exclude_sha256=SHA_D) == []
    reg.close()


def test_registry_excludes_same_sha256(tmp_path: Any) -> None:
    """exclude_sha256 排除"当前图自身"的记录;不排除时自身可命中。"""
    reg = _registry(tmp_path)
    reg.register(SHA_A, H0, SITE_A)

    assert reg.find_similar(H0, max_distance=0) != []       # 自身在库可命中
    assert reg.find_similar(H0, max_distance=64, exclude_sha256=SHA_A) == []
    reg.close()


def test_registry_upsert_overwrites(tmp_path: Any) -> None:
    """同 sha256 重复登记 = UPSERT 覆盖:总数不涨,内容更新。"""
    db = str(tmp_path / "phash.db")
    reg = PhashRegistry(db)
    reg.register(SHA_A, H0, SITE_A, "old-tag")
    reg.register(SHA_A, H_FAR, SITE_B, "new-tag")
    assert reg.stats()["total"] == 1

    row = _read_row(db, SHA_A)
    assert row["phash"] == H_FAR
    assert row["site_url"] == SITE_B
    assert row["verdict_tag"] == "new-tag"
    assert row["created_at"]

    hits = reg.find_similar(H_FAR, max_distance=0)
    assert [h["sha256"] for h in hits] == [SHA_A]
    assert reg.find_similar(H0, max_distance=0) == []       # 旧哈希已被覆盖
    reg.close()


def test_registry_register_validation(tmp_path: Any) -> None:
    """register 对空 sha / 非法 phash 抛中文 ValueError。"""
    reg = _registry(tmp_path)
    with pytest.raises(ValueError, match="sha256"):
        reg.register("", H0, SITE_A)
    with pytest.raises(ValueError, match="非法"):
        reg.register(SHA_A, "not-hex!", SITE_A)
    assert reg.stats()["total"] == 0
    with pytest.raises(ValueError, match="非法"):
        reg.find_similar("xyz")
    reg.close()


def test_registry_stats_and_persistence(tmp_path: Any) -> None:
    """stats 计数正确;close 后重开实例数据仍在。"""
    db = str(tmp_path / "phash.db")
    reg = PhashRegistry(db)
    _register_known_set(reg)
    stats = reg.stats()
    assert stats == {"total": 4, "sites": 2}
    reg.close()

    reg2 = PhashRegistry(db)
    assert reg2.stats() == stats
    assert [h["sha256"] for h in reg2.find_similar(H0, max_distance=8)] == [
        SHA_A, SHA_B, SHA_C,
    ]
    reg2.close()


def test_registry_corrupt_rebuild(tmp_path: Any) -> None:
    """库文件损坏时删除重建:打开不抛错、计数归零、可继续登记查询。"""
    db_path = tmp_path / "phash.db"
    reg = PhashRegistry(str(db_path))
    reg.register(SHA_A, H0, SITE_A)
    reg.close()

    db_path.write_bytes(b"garbage bytes, not a sqlite database" * 20)

    reg2 = PhashRegistry(str(db_path))
    assert reg2.stats()["total"] == 0
    reg2.register(SHA_B, H1, SITE_B, "after-rebuild")
    assert [h["sha256"] for h in reg2.find_similar(H0, max_distance=1)] == [SHA_B]
    reg2.close()


def test_registry_skips_dirty_hash_rows(tmp_path: Any) -> None:
    """库中混入无法比较的脏哈希记录时跳过,不中断查询。"""
    db = str(tmp_path / "phash.db")
    reg = PhashRegistry(db)
    reg.register(SHA_B, H1, SITE_B)
    reg.close()
    # 绕过校验直接写入脏数据(长度不匹配 / 非法字符)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO hashes (sha256, phash, site_url, verdict_tag, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (SHA_C, "toomanybits" * 20, SITE_A, "", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    reg2 = PhashRegistry(db)
    assert [h["sha256"] for h in reg2.find_similar(H0, max_distance=1)] == [SHA_B]
    reg2.close()


def test_registry_concurrent_register_and_query(tmp_path: Any) -> None:
    """8 线程并发 register / find_similar / stats:无异常、写入全落盘。"""
    reg = _registry(tmp_path)
    errors: list[str] = []
    workers = 8
    per_worker = 25

    def worker(tid: int) -> None:
        try:
            for i in range(per_worker):
                sha = f"{tid:02x}{i:04x}" + "0" * 58
                reg.register(sha, H0, f"https://s{tid}.example/", f"tag-{tid}")
                if i % 10 == 0:
                    reg.find_similar(H0, max_distance=0)
                    reg.stats()
        except Exception as exc:  # noqa: BLE001 - 线程内异常汇总断言
            errors.append(f"线程 {tid}:{exc}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    total = workers * per_worker
    assert reg.stats()["total"] == total
    assert len(reg.find_similar(H0, max_distance=0)) == total
    reg.close()


# ---------------------------------------------------------------------------
# 端到端:跨案件近重复识别流
# ---------------------------------------------------------------------------


def test_near_duplicate_flow_site_reregister(tmp_path: Any) -> None:
    """同一批图换站重传:登记 200x200 原图后,用 160x160 缩放版查询能命中。"""
    base = _structured_photo(200)
    p200 = _save(base, tmp_path / "origin.png")
    p160 = _save(base.resize((160, 160), Image.Resampling.LANCZOS),
                 tmp_path / "reupload.png")

    reg = _registry(tmp_path)
    sha_origin = _file_sha256(p200)
    reg.register(sha_origin, phash(p200), SITE_A, "nsfw")
    # 无关记录不应干扰
    reg.register(SHA_D, H_FAR, "https://unrelated.example/")

    hits = reg.find_similar(phash(p160), max_distance=10)
    assert hits, "换站重传的近重复图应能命中原登记记录"
    assert hits[0]["sha256"] == sha_origin
    assert hits[0]["site"] == SITE_A
    assert hits[0]["distance"] <= 4
    reg.close()


# ---------------------------------------------------------------------------
# V5 升级锁定:WAL + busy_timeout / find_similar 结果上限 / 遥测
# ---------------------------------------------------------------------------


def test_v5_sqlite_wal_and_busy_timeout(tmp_path: Any) -> None:
    """V5:库连接启用 WAL 与 busy_timeout=5000(并发读写不互相阻塞)。"""
    reg = _registry(tmp_path)
    try:
        mode = reg._conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout = reg._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert str(mode).lower() == "wal"
        assert int(timeout) == 5000
    finally:
        reg.close()


def test_v5_wal_survives_rebuild_after_corruption(tmp_path: Any) -> None:
    """损坏重建后的新连接同样带 WAL 与 busy_timeout,且记 telemetry.rebuild。"""
    from netsentinel import telemetry

    db_path = tmp_path / "phash.db"
    reg = PhashRegistry(str(db_path))
    reg.register(SHA_A, H0, SITE_A)
    reg.close()
    db_path.write_bytes(b"garbage not sqlite" * 30)

    telemetry.reset()
    try:
        reg2 = PhashRegistry(str(db_path))
        assert telemetry.snapshot()["counters"].get("phash.rebuild") == 1
        assert str(
            reg2._conn.execute("PRAGMA journal_mode").fetchone()[0]
        ).lower() == "wal"
        assert int(reg2._conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 5000
        reg2.close()
    finally:
        telemetry.reset()


def test_v5_find_similar_caps_results_with_warning(
    tmp_path: Any, monkeypatch: Any, caplog: Any
) -> None:
    """命中超过 MAX_FIND_RESULTS 时截断到上限并记 warning 提示。"""
    import logging as _logging

    import netsentinel.intel.phash as phash_mod

    reg = _registry(tmp_path)
    for i in range(10):
        reg.register(f"{i:064x}", H0, f"https://s{i}.example/")
    monkeypatch.setattr(phash_mod, "MAX_FIND_RESULTS", 3)

    with caplog.at_level(_logging.WARNING, logger="netsentinel.intel.phash"):
        hits = reg.find_similar(H0, max_distance=8)
    assert len(hits) == 3
    assert all(h["distance"] == 0 for h in hits)
    assert any("截断" in r.message for r in caplog.records)
    reg.close()


def test_v5_telemetry_compute_timer_and_registry_hits(tmp_path: Any) -> None:
    """telemetry:phash/ahash 计时 phash.compute;find_similar 命中数累加。"""
    from netsentinel import telemetry

    path = _save(_structured_photo(120), tmp_path / "photo.png")
    telemetry.reset()
    try:
        phash(path)
        ahash(path)
        snap = telemetry.snapshot()
        assert snap["timers"]["phash.compute"]["count"] == 2

        reg = _registry(tmp_path)
        _register_known_set(reg)
        hits = reg.find_similar(H0, max_distance=8)
        counters = telemetry.snapshot()["counters"]
        assert counters.get("phash.registry_hit") == len(hits) == 3
        # 无命中查询不计数
        assert reg.find_similar(H_FAR, max_distance=0, exclude_sha256=SHA_D) == []
        assert telemetry.snapshot()["counters"].get("phash.registry_hit") == 3
        reg.close()
    finally:
        telemetry.reset()


# ---------------------------------------------------------------------------
# V12 · A219 多哈希列:register 扩展 / schema 迁移 / find_similar hash_kind
# (不变性召回与误报矩阵见 tests/test_phash_invariant.py)
# ---------------------------------------------------------------------------

#: 构造好的 27 位 pyramid 哈希(层值 1/2/3,每层 9 hex)。
PYR_A = "000000001" + "000000002" + "000000003"
#: 与 PYR_A 仅第二层差 1 bit(2 → 3)。
PYR_A_NEAR = "000000001" + "000000003" + "000000003"
#: 构造好的 16 位 mirror 哈希:距 H0 恰 8、距 H1(0x…01)恰 7(位运算可手验)。
MIR_A = "00000000000000ff"
SHA_E = "e" * 64


def test_v12_register_multi_hash_columns_roundtrip(tmp_path: Any) -> None:
    """register 带 mirror/pyramid 可选字段:落盘正确;不传 = 空串(向后
    兼容);UPSERT 覆盖语义(再次登记不传 → 新列覆盖回空)。"""
    db = str(tmp_path / "phash.db")
    reg = PhashRegistry(db)
    reg.register(SHA_A, H0, SITE_A, mirror_hash=MIR_A, pyramid_hash=PYR_A)
    row = _read_row(db, SHA_A)
    assert row["mirror_hash"] == MIR_A
    assert row["pyramid_hash"] == PYR_A

    reg.register(SHA_B, H1, SITE_B)  # 不传可选字段 → 空
    row = _read_row(db, SHA_B)
    assert row["mirror_hash"] == "" and row["pyramid_hash"] == ""

    reg.register(SHA_A, H0, SITE_A)  # UPSERT:全列覆盖
    row = _read_row(db, SHA_A)
    assert row["mirror_hash"] == "" and row["pyramid_hash"] == ""
    reg.close()


def test_v12_register_multi_hash_validation(tmp_path: Any) -> None:
    """可选字段非法(非 hex / 长度不符)→ 中文 ValueError,不入库。"""
    reg = _registry(tmp_path)
    with pytest.raises(ValueError, match="mirror_hash"):
        reg.register(SHA_A, H0, SITE_A, mirror_hash="zz")            # 非 hex
    with pytest.raises(ValueError, match="mirror_hash"):
        reg.register(SHA_A, H0, SITE_A, mirror_hash="0" * 15)        # 长度
    with pytest.raises(ValueError, match="pyramid_hash"):
        reg.register(SHA_A, H0, SITE_A, pyramid_hash="0" * 26)       # 长度
    assert reg.stats()["total"] == 0
    reg.close()


def test_v12_schema_migration_from_v1(tmp_path: Any) -> None:
    """v1 旧库(无 mirror_hash/pyramid_hash 列)打开即补列,老数据不动。"""
    db = str(tmp_path / "phash.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE hashes (sha256 TEXT PRIMARY KEY, phash TEXT NOT NULL "
        "DEFAULT '', site_url TEXT NOT NULL DEFAULT '', verdict_tag TEXT NOT "
        "NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        "INSERT INTO hashes (sha256, phash, site_url, verdict_tag, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (SHA_E, H0, SITE_A, "old", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    reg = PhashRegistry(db)
    columns = {
        str(row[1]) for row in reg._conn.execute("PRAGMA table_info(hashes)")
    }
    assert {"mirror_hash", "pyramid_hash"} <= columns
    assert reg.stats()["total"] == 1  # 老数据保留
    # 旧记录 mirror 查询:mirror 列为空 → 回退 phash 列比较(现有行为)
    hits = reg.find_similar(H0, max_distance=0, hash_kind="mirror")
    assert [h["sha256"] for h in hits] == [SHA_E]
    reg.close()


def test_v12_find_similar_hash_kinds(tmp_path: Any) -> None:
    """find_similar hash_kind:mirror(列命中 + 回退)、pyramid(三层取 min
    距离)、非法 kind / 查询长度 → 中文 ValueError;缺省 "phash" 行为
    与旧调用完全一致(由本文件既有用例锁定)。"""
    reg = _registry(tmp_path)
    reg.register(SHA_A, H0, SITE_A, mirror_hash=MIR_A, pyramid_hash=PYR_A)
    reg.register(SHA_B, H1, SITE_B)  # 旧式记录:两新列为空

    # mirror:精确命中 mirror 列
    hits = reg.find_similar(MIR_A, max_distance=0, hash_kind="mirror")
    assert [(h["sha256"], h["distance"]) for h in hits] == [(SHA_A, 0)]
    # mirror 查询同时看回退列:SHA_B(mirror 列空)的 phash=H1 与 MIR_A
    # 距 7(0xff ^ 0x01 = 0xfe)= 多哈希列任一命中即候选
    hits = reg.find_similar(MIR_A, max_distance=8, hash_kind="mirror")
    assert [(h["sha256"], h["distance"]) for h in hits] == [(SHA_A, 0), (SHA_B, 7)]

    # pyramid:PYR_A_NEAR 与 PYR_A 第二层差 1 bit,但三层取 min → 距 0
    hits = reg.find_similar(PYR_A_NEAR, max_distance=0, hash_kind="pyramid")
    assert [(h["sha256"], h["distance"]) for h in hits] == [(SHA_A, 0)]
    # 27 hex 查询对 16 hex 旧记录长度不可比 → 按现有"脏记录跳过"语义
    assert reg.find_similar(PYR_A_NEAR, max_distance=36, hash_kind="pyramid",
                            exclude_sha256=SHA_A) == []

    # 校验分支
    with pytest.raises(ValueError, match="hash_kind"):
        reg.find_similar(H0, hash_kind="bogus")
    with pytest.raises(ValueError, match="27 位"):
        reg.find_similar(H0, hash_kind="pyramid")
    with pytest.raises(ValueError, match="16 位"):
        reg.find_similar(PYR_A, hash_kind="mirror")
    reg.close()
