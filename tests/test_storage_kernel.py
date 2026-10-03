# -*- coding: utf-8 -*-
"""A131 存储内核(SQLiteKernel / Repo)单元测试:全部离线、零外呼。

覆盖(契约 CONTRACTS-V7.md §2 A131 + 红线 29/31):

- 连接策略与全仓一致:WAL + busy_timeout=5000 + check_same_thread=False +
  threading.Lock(线程并发写用例证明);
- migrate:首次逐条执行并写版本表;同版本二次调用 / 重开实例 **零 DDL 执行**
  (以 counters["ddl_executed"] 与 sqlite 原生 total_changes 双计数断言,
  红线 31);升级路径;回退(目标低于当前)中文 ValueError 且零执行;
  中途失败整事务回滚;空 DDL 只记版本;版本表仅 migrate 时创建;
- execute/query 裸口:参数化、行转 dict、类型保真、空结果;
- repo CRUD 全操作:get/insert(含 DEFAULT VALUES)/update/delete/list
  (where 等值参数化 + limit 参数化 + 主键升序);
- 防注入:值注入串当字面量(查询精确命中且表安然无恙);表名/主键列名/
  列名白名单校验中文 ValueError;
- 既有库兼容:真实 review_queue.db 只读打开不迁移不改写(sqlite_master
  与数据前后逐字一致);纯 sqlite3 造的任意库同样不动;
- vacuum_if_needed:低于阈值零 VACUUM / 达阈值恰一次 / 自定义阈值;
  VACUUM 后页数回收且数据保留;
- bench(红线 31,操作计数断言,零墙钟):test_v7_bench_migrate_idempotent_
  execute_counts、test_v7_bench_vacuum_count_thresholds;
- kernel_selfcheck 离线自检(供 A138 总控调用)。

只写 tmp_path;sqlite3 标准库;无网络、无真实门户、无 playwright。
"""
from __future__ import annotations

import pathlib
import sqlite3
import threading

import pytest

from netsentinel.contracts import SiteReport, Verdict
from netsentinel.decision.review_queue import ReviewQueue
from netsentinel.storage.kernel import (
    BUSY_TIMEOUT_MS,
    DEFAULT_MIN_PAGE_COUNT,
    SQLiteKernel,
    kernel_selfcheck,
)

#: 3 条 DDL 的标准迁移集(migrate 计数断言用)
DDL_V1 = [
    "CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, name TEXT, score REAL)",
    "CREATE TABLE IF NOT EXISTS tags(item_id INTEGER, tag TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_items_name ON items(name)",
]


def table_names(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    ]


def kernel_table_names(k: SQLiteKernel) -> list[str]:
    """经内核查询表名列表(顺带证明 query 裸口可用)。"""
    return [
        r["name"]
        for r in k.query("SELECT name FROM sqlite_master WHERE type = 'table'")
    ]


@pytest.fixture()
def kernel(tmp_path: pathlib.Path) -> SQLiteKernel:
    """独立 db 的内核(用例结束即关闭)。"""
    with SQLiteKernel(tmp_path / "kernel.db") as k:
        k.migrate(1, DDL_V1)
        yield k


# ---------------------------------------------------------------------------
# 连接策略 / 生命周期
# ---------------------------------------------------------------------------


def test_open_sets_wal_and_busy_timeout_pragmas(tmp_path: pathlib.Path) -> None:
    """连接策略与全仓一致:WAL + busy_timeout=5000(打开即生效)。"""
    with SQLiteKernel(tmp_path / "pragma.db") as k:
        assert k.query("PRAGMA journal_mode") == [{"journal_mode": "wal"}]
        # 注:busy_timeout 结果列名在不同 Python 版本为 timeout/busy_timeout,按值断言。
        (timeout_row,) = k.query("PRAGMA busy_timeout")
        assert int(next(iter(timeout_row.values()))) == BUSY_TIMEOUT_MS == 5000


def test_open_creates_nested_parent_dirs_and_no_ddl_on_fresh_db(
    tmp_path: pathlib.Path,
) -> None:
    """嵌套父目录自动创建;全新库打开零 DDL(连版本表都不建)。"""
    db_path = tmp_path / "deep" / "nested" / "dirs" / "app.db"
    with SQLiteKernel(db_path) as k:
        assert db_path.parent.is_dir()
        assert k.current_version() is None
    raw = sqlite3.connect(str(db_path))
    try:
        assert table_names(raw) == []  # 构造器不做任何 DDL
    finally:
        raw.close()


def test_memory_database_supported() -> None:
    """:memory: 库可用(WAL 对内存库无效但不报错)。"""
    with SQLiteKernel(":memory:") as k:
        k.migrate(1, ["CREATE TABLE t(a TEXT)"])
        assert k.repo("t").insert({"a": "x"}) == 1
        assert k.repo("t").list() == [{"a": "x"}]


def test_with_context_closes_and_close_idempotent(
    tmp_path: pathlib.Path,
) -> None:
    """with 退出即关闭;close 幂等;关闭后再执行报 ProgrammingError。"""
    db_path = tmp_path / "ctx.db"
    with SQLiteKernel(db_path) as k:
        k.migrate(1, DDL_V1)
    k.close()  # 幂等容忍
    with pytest.raises(sqlite3.ProgrammingError):
        k.execute("SELECT 1")


# ---------------------------------------------------------------------------
# 裸口:execute / query
# ---------------------------------------------------------------------------


def test_execute_returns_cursor_and_query_rows_to_dicts(
    kernel: SQLiteKernel,
) -> None:
    """execute 返回游标(裸口);query 行转 dict。"""
    cur = kernel.execute(
        "INSERT INTO items(id, name, score) VALUES (?, ?, ?)", (1, "甲", 0.5)
    )
    assert cur.lastrowid == 1
    rows = kernel.query("SELECT id, name, score FROM items")
    assert rows == [{"id": 1, "name": "甲", "score": 0.5}]


def test_query_empty_result_and_type_fidelity(kernel: SQLiteKernel) -> None:
    """空结果返回空列表;INTEGER/REAL/TEXT/NULL 类型保真。"""
    assert kernel.query("SELECT * FROM items WHERE id = 999") == []
    kernel.execute("CREATE TABLE tf(i INTEGER, r REAL, s TEXT, u TEXT)")
    kernel.execute("INSERT INTO tf VALUES (?, ?, ?, ?)", (7, 0.25, "文本", None))
    (row,) = kernel.query("SELECT * FROM tf")
    assert row == {"i": 7, "r": 0.25, "s": "文本", "u": None}
    assert isinstance(row["i"], int)
    assert isinstance(row["r"], float)
    assert isinstance(row["u"], type(None))


def test_execute_params_injection_treated_as_literal(
    kernel: SQLiteKernel,
) -> None:
    """裸口参数化:注入串只作字面量,永远匹配不到普通行。"""
    kernel.execute("INSERT INTO items(id, name) VALUES (?, ?)", (1, "正常"))
    for payload in ("' OR '1'='1", "1; DROP TABLE items; --", "x' OR 1=1; --"):
        (row,) = kernel.query(
            "SELECT COUNT(*) AS n FROM items WHERE name = ?", (payload,)
        )
        assert row["n"] == 0
    assert kernel.query("SELECT COUNT(*) AS n FROM items")[0]["n"] == 1  # 表还在


# ---------------------------------------------------------------------------
# migrate:版本化迁移(幂等 = 红线 31)
# ---------------------------------------------------------------------------


def test_migrate_first_apply_executes_each_ddl_and_writes_version(
    tmp_path: pathlib.Path,
) -> None:
    """首次迁移:逐条执行 DDL(计数=3)、建版本表并写版本号。"""
    with SQLiteKernel(tmp_path / "m.db") as k:
        assert k.current_version() is None
        assert k.migrate(3, DDL_V1) is True
        assert k.counters["ddl_executed"] == 3
        assert k.current_version() == 3
        tables = kernel_table_names(k)
        assert {"items", "tags", "schema_version"} <= set(tables)


def test_migrate_same_version_zero_execution_same_instance(
    tmp_path: pathlib.Path,
) -> None:
    """同实例重复迁移同版本:零 DDL 执行、零改写(total_changes 不动)。"""
    with SQLiteKernel(tmp_path / "m.db") as k:
        k.migrate(3, DDL_V1)
        ddl_before = k.counters["ddl_executed"]
        changes_before = k.total_changes
        assert k.migrate(3, DDL_V1) is False
        assert k.counters["ddl_executed"] == ddl_before  # 增量 0
        assert k.total_changes == changes_before  # sqlite 原生计数:一行未改
        assert k.current_version() == 3


def test_migrate_reopen_idempotent_zero_ddl(tmp_path: pathlib.Path) -> None:
    """重开库(模拟下一次进程启动)再迁移同版本:零 DDL 执行。"""
    db_path = str(tmp_path / "m.db")
    with SQLiteKernel(db_path) as k:
        k.migrate(3, DDL_V1)
    with SQLiteKernel(db_path) as k2:
        assert k2.current_version() == 3
        assert k2.migrate(3, DDL_V1) is False
        assert k2.counters["ddl_executed"] == 0
        assert k2.total_changes == 0  # 新连接从头计:零改写
        assert k2.migrate(3, DDL_V1) is False  # 再来一次仍零执行
        assert k2.counters["ddl_executed"] == 0


def test_migrate_upgrade_to_higher_version(tmp_path: pathlib.Path) -> None:
    """v1 → v2 增量 DDL 执行且版本前进。"""
    with SQLiteKernel(tmp_path / "up.db") as k:
        k.migrate(1, ["CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY)"])
        repo = k.repo("items")
        repo.insert({"id": 1})
        assert k.migrate(
            2,
            [
                "CREATE TABLE IF NOT EXISTS extra(id INTEGER PRIMARY KEY, note TEXT)",
                "CREATE INDEX IF NOT EXISTS idx_extra_note ON extra(note)",
            ],
        )
        assert k.current_version() == 2
        assert k.counters["ddl_executed"] == 3  # 1 + 2(只数用户 DDL)
        assert repo.list() == [{"id": 1}]  # 旧数据迁移后完好


def test_migrate_downgrade_rejected_value_error_cn(
    tmp_path: pathlib.Path,
) -> None:
    """版本回退(目标低于当前)拒绝:中文 ValueError,且零执行。"""
    with SQLiteKernel(tmp_path / "dg.db") as k:
        k.migrate(2, ["CREATE TABLE IF NOT EXISTS a(x TEXT)"])
        ddl_before = k.counters["ddl_executed"]
        with pytest.raises(ValueError, match="回退") as excinfo:
            k.migrate(1, ["CREATE TABLE IF NOT EXISTS b(y TEXT)"])
        msg = str(excinfo.value)
        assert "schema 版本不可回退" in msg
        assert "v2" in msg and "v1" in msg
        assert k.counters["ddl_executed"] == ddl_before  # 拒绝路径零 DDL
        assert k.current_version() == 2
        assert "b" not in kernel_table_names(k)  # 回退 DDL 未执行


def test_migrate_failure_rolls_back_atomically(tmp_path: pathlib.Path) -> None:
    """迁移中途失败:整事务回滚(已建表撤销、版本表不落、版本号不写)。"""
    with SQLiteKernel(tmp_path / "rb.db") as k:
        with pytest.raises(sqlite3.OperationalError):
            k.migrate(
                1,
                [
                    "CREATE TABLE should_rollback(a TEXT)",
                    "CREATE TABLE 语法错误(",
                ],
            )
        assert k.current_version() is None
        tables = set(kernel_table_names(k))
        assert "should_rollback" not in tables
        assert "schema_version" not in tables  # 版本表也随事务回滚
        # 修复 DDL 后重试成功(回滚保证可安全重试):
        assert k.migrate(1, ["CREATE TABLE should_rollback(a TEXT)"]) is True
        assert k.current_version() == 1


def test_migrate_empty_ddl_still_records_version(tmp_path: pathlib.Path) -> None:
    """空 DDL 列表:仅记版本(纯版本簿记),ddl_executed == 0。"""
    with SQLiteKernel(tmp_path / "empty.db") as k:
        assert k.migrate(5, []) is True
        assert k.counters["ddl_executed"] == 0
        assert k.current_version() == 5


def test_current_version_none_until_migrate(tmp_path: pathlib.Path) -> None:
    """只读打开(含查询)绝不创建版本表:current_version 恒为 None。"""
    with SQLiteKernel(tmp_path / "ro.db") as k:
        k.execute("CREATE TABLE t(a TEXT)")
        k.execute("INSERT INTO t VALUES ('x')")
        assert k.query("SELECT * FROM t") == [{"a": "x"}]
        assert k.current_version() is None
        assert "schema_version" not in kernel_table_names(k)


# ---------------------------------------------------------------------------
# repo:参数化 CRUD
# ---------------------------------------------------------------------------


def test_repo_insert_get_roundtrip(kernel: SQLiteKernel) -> None:
    repo = kernel.repo("items")
    new_id = repo.insert({"id": 10, "name": "站点甲", "score": 0.9})
    assert new_id == 10
    assert repo.get(10) == {"id": 10, "name": "站点甲", "score": 0.9}


def test_repo_get_missing_returns_none(kernel: SQLiteKernel) -> None:
    assert kernel.repo("items").get(404) is None


def test_repo_update_persists_and_returns_rowcount(kernel: SQLiteKernel) -> None:
    repo = kernel.repo("items")
    repo.insert({"id": 1, "name": "旧", "score": 0.1})
    repo.insert({"id": 2, "name": "乙", "score": 0.2})
    assert repo.update(1, {"name": "新", "score": 0.8}) == 1
    assert repo.get(1) == {"id": 1, "name": "新", "score": 0.8}
    assert repo.get(2) == {"id": 2, "name": "乙", "score": 0.2}  # 其余行不动
    assert repo.update(999, {"name": "无"}) == 0  # 主键不存在 → 0
    with pytest.raises(ValueError, match="至少"):
        repo.update(1, {})  # 空 dict 拒绝(防呆)


def test_repo_delete_returns_rowcount(kernel: SQLiteKernel) -> None:
    repo = kernel.repo("items")
    repo.insert({"id": 1, "name": "a", "score": 0.0})
    assert repo.delete(1) == 1
    assert repo.get(1) is None
    assert repo.delete(1) == 0  # 再删 → 0


def test_repo_list_all_where_limit(kernel: SQLiteKernel) -> None:
    """list:主键升序、where 等值参数化过滤、limit 参数化。"""
    repo = kernel.repo("items")
    for i, (name, score) in enumerate(
        [("c", 0.9), ("a", 0.9), ("b", 0.1)], start=1
    ):
        repo.insert({"id": i, "name": name, "score": score})
    assert [r["id"] for r in repo.list()] == [1, 2, 3]  # 主键升序
    hot = repo.list(where={"score": 0.9})
    assert [r["id"] for r in hot] == [1, 2]
    assert [r["id"] for r in repo.list(limit=2)] == [1, 2]
    assert repo.list(limit=0) == []
    assert repo.list(where={"score": 0.9}, limit=1) == [
        {"id": 1, "name": "c", "score": 0.9}
    ]


def test_repo_list_invalid_limit_rejected(kernel: SQLiteKernel) -> None:
    repo = kernel.repo("items")
    for bad in (-1, "5", 1.5, True):
        with pytest.raises(ValueError, match="limit"):
            repo.list(limit=bad)


def test_repo_insert_default_values_empty_dict(kernel: SQLiteKernel) -> None:
    """空 dict → INSERT DEFAULT VALUES(全默认值行)。"""
    kernel.execute(
        "CREATE TABLE dv(id INTEGER PRIMARY KEY, tag TEXT NOT NULL DEFAULT 'none',"
        " n INTEGER NOT NULL DEFAULT 7)"
    )
    repo = kernel.repo("dv")
    assert repo.insert({}) == 1
    assert repo.get(1) == {"id": 1, "tag": "none", "n": 7}


def test_repo_insert_rejects_non_dict(kernel: SQLiteKernel) -> None:
    with pytest.raises(ValueError, match="dict"):
        kernel.repo("items").insert([("id", 1)])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 防注入
# ---------------------------------------------------------------------------


def test_repo_where_injection_string_is_literal(kernel: SQLiteKernel) -> None:
    """where 值注入串只作字面量:精确命中自身行,其余行与表结构分毫无损。"""
    repo = kernel.repo("items")
    payloads = [
        "1 OR 1=1",
        "Robert'); DROP TABLE items;--",
        "x' OR '1'='1",
        "'; DELETE FROM items WHERE 1=1; --",
    ]
    for i, name in enumerate(["正常甲", "正常乙", *payloads], start=1):
        repo.insert({"id": i, "name": name, "score": 0.0})
    for payload in payloads:
        hits = repo.list(where={"name": payload})
        assert len(hits) == 1  # 只命中存了该字面量的那一行
        assert hits[0]["name"] == payload
    assert len(repo.list()) == 2 + len(payloads)  # 没有全表命中、没有行丢失
    (cnt,) = kernel.query("SELECT COUNT(*) AS n FROM items")
    assert cnt["n"] == 2 + len(payloads)  # 表安然无恙


def test_repo_bad_identifier_rejected(kernel: SQLiteKernel) -> None:
    """表名/主键列名/列名走标识符白名单:非法即中文 ValueError。"""
    before = len(kernel.repo("items").list())
    with pytest.raises(ValueError, match="表名"):
        kernel.repo("items; DROP TABLE items")
    with pytest.raises(ValueError, match="表名"):
        kernel.repo('"items"')
    with pytest.raises(ValueError, match="主键列名"):
        kernel.repo("items", pk="id; DROP TABLE items")
    repo = kernel.repo("items")
    with pytest.raises(ValueError, match="列名"):
        repo.list(where={"id = 1 OR 1=1": 1})
    with pytest.raises(ValueError, match="列名"):
        repo.insert({"id; DROP TABLE items": 1, "name": "x", "score": 0.0})
    with pytest.raises(ValueError, match="列名"):
        repo.update(1, {"name--": "x"})
    assert len(repo.list()) == before  # 以上全部被拦,数据零变化


# ---------------------------------------------------------------------------
# 线程安全
# ---------------------------------------------------------------------------


def test_thread_concurrent_repo_inserts_all_lands(tmp_path: pathlib.Path) -> None:
    """4 线程 × 25 行并发写(单连接 + 全局锁):全部落库、零异常。"""
    with SQLiteKernel(tmp_path / "conc.db") as k:
        k.migrate(1, ["CREATE TABLE IF NOT EXISTS log(id INTEGER PRIMARY KEY, s TEXT)"])
        repo = k.repo("log")
        n_threads, per_thread = 4, 25
        barrier = threading.Barrier(n_threads)
        errors: list[BaseException] = []

        def worker(t: int) -> None:
            try:
                barrier.wait()
                for i in range(per_thread):
                    repo.insert({"id": t * per_thread + i + 1, "s": f"t{t}-{i}"})
            except BaseException as exc:  # noqa: BLE001 - 收集后统一断言
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
        assert errors == []
        rows = repo.list()
        assert len(rows) == n_threads * per_thread
        assert [r["id"] for r in rows] == list(range(1, n_threads * per_thread + 1))


# ---------------------------------------------------------------------------
# 既有库兼容(打开不迁移不改写)
# ---------------------------------------------------------------------------


def test_existing_review_queue_db_readonly_compat(tmp_path: pathlib.Path) -> None:
    """真实 review_queue.db:可读可过滤,但零迁移零改写。"""
    db_path = str(tmp_path / "review_queue.db")
    with ReviewQueue(db_path) as queue:
        queue.add(SiteReport(site_url="https://a.example", verdict=Verdict.SUSPECT))
        queue.add(SiteReport(site_url="https://b.example", verdict=Verdict.NSFW))
    with SQLiteKernel(db_path) as k:
        rows = k.query("SELECT id, site_url, verdict, status FROM entries ORDER BY id")
        assert [r["site_url"] for r in rows] == [
            "https://a.example",
            "https://b.example",
        ]
        assert all(
            r["status"] == "pending" and r["verdict"] in ("suspect", "nsfw")
            for r in rows
        )
        entries = k.repo("entries")
        assert entries.get(1)["site_url"] == "https://a.example"
        assert len(entries.list(where={"status": "pending"})) == 2
        # 不迁移:版本表不存在、版本号 None
        assert "schema_version" not in kernel_table_names(k)
        assert k.current_version() is None
    raw = sqlite3.connect(db_path)
    try:
        assert "schema_version" not in table_names(raw)
        assert raw.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2
    finally:
        raw.close()


def test_existing_plain_sqlite_db_untouched(tmp_path: pathlib.Path) -> None:
    """任意既有 sqlite(外部工具建的普通库):schema 与数据逐字不变。"""
    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE legacy(k TEXT PRIMARY KEY, v INTEGER)")
    conn.execute("INSERT INTO legacy VALUES ('a', 1)")
    conn.execute("INSERT INTO legacy VALUES ('b', 2)")
    conn.commit()
    master_before = conn.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY name"
    ).fetchall()
    conn.close()
    with SQLiteKernel(db_path) as k:
        assert k.query("SELECT k, v FROM legacy ORDER BY k") == [
            {"k": "a", "v": 1},
            {"k": "b", "v": 2},
        ]
        legacy = k.repo("legacy", pk="k")
        assert legacy.get("a") == {"k": "a", "v": 1}
        assert len(legacy.list()) == 2
        assert k.current_version() is None
    # 注:打开即设 WAL 与全仓一致(ReviewQueue 打开同一库也会设),不属数据改写。
    conn2 = sqlite3.connect(db_path)
    try:
        master_after = conn2.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
        assert master_after == master_before  # schema 逐字未变
        assert conn2.execute("SELECT COUNT(*) FROM legacy").fetchone()[0] == 2
    finally:
        conn2.close()


# ---------------------------------------------------------------------------
# vacuum_if_needed(阈值两分支)
# ---------------------------------------------------------------------------


def test_vacuum_below_threshold_zero_vacuum(kernel: SQLiteKernel) -> None:
    """小库(页数 < 200):零 VACUUM(计数断言),返回 False。"""
    kernel.repo("items").insert({"id": 1, "name": "小", "score": 0.0})
    assert kernel.page_count() < DEFAULT_MIN_PAGE_COUNT  # 前置:确实低于阈值
    assert kernel.vacuum_if_needed() is False
    assert kernel.counters["vacuums"] == 0
    assert kernel.vacuum_if_needed(min_page_count=10_000) is False
    assert kernel.counters["vacuums"] == 0


def test_vacuum_above_threshold_runs_once_and_reclaims(
    tmp_path: pathlib.Path,
) -> None:
    """大库删空后页数仍高:恰 VACUUM 一次、页数回收、数据保留。"""
    with SQLiteKernel(tmp_path / "big.db") as k:
        k.migrate(1, ["CREATE TABLE IF NOT EXISTS blobs(id INTEGER PRIMARY KEY, p TEXT)"])
        repo = k.repo("blobs")
        chunk = "x" * 8192  # 每行 ≥ 2 页(页 4096):400 行 → 远超 200 页
        for i in range(400):
            repo.insert({"id": i, "p": chunk})
        before = k.page_count()
        assert before >= DEFAULT_MIN_PAGE_COUNT  # 确定性构造的前置
        k.execute("DELETE FROM blobs")
        assert k.page_count() >= before  # 删除不缩页(碎片):阈值仍触发
        assert k.vacuum_if_needed() is True
        assert k.counters["vacuums"] == 1
        after = k.page_count()
        assert after < DEFAULT_MIN_PAGE_COUNT  # 页数已回收
        assert repo.list() == []  # 数据语义不变(仍为空)


def test_vacuum_custom_threshold_and_repeat(tmp_path: pathlib.Path) -> None:
    """自定义阈值:min_page_count=1 小库也触发;每次调用都按当前页数判断。"""
    with SQLiteKernel(tmp_path / "small.db") as k:
        k.migrate(1, ["CREATE TABLE IF NOT EXISTS t(a TEXT)"])
        k.repo("t").insert({"a": "x"})
        assert k.vacuum_if_needed(min_page_count=1) is True
        assert k.counters["vacuums"] == 1
        assert k.vacuum_if_needed(min_page_count=1) is True  # 页数仍 ≥1 → 再触发
        assert k.counters["vacuums"] == 2
        assert k.vacuum_if_needed(min_page_count=500) is False
        assert k.counters["vacuums"] == 2


# ---------------------------------------------------------------------------
# bench(红线 31:操作计数断言,零墙钟)
# ---------------------------------------------------------------------------


def test_v7_bench_migrate_idempotent_execute_counts(tmp_path: pathlib.Path) -> None:
    """代差主张:同一套 DDL,二次打开再迁移的用户 DDL 执行数 == 0。

    计数证据链(全部确定性,不依赖时间):

    - 首次:ddl_executed == 3(逐条执行),版本写为 3;
    - 重开:ddl_executed == 0 且 total_changes == 0(sqlite 原生改写行计数,
      连版本行都没重写),版本仍为 3;
    - 同实例第三次调用仍为 0。
    """
    db_path = str(tmp_path / "bench.db")
    with SQLiteKernel(db_path) as k:
        assert k.migrate(3, DDL_V1) is True
        assert k.counters["ddl_executed"] == 3
    with SQLiteKernel(db_path) as k2:
        assert k2.migrate(3, DDL_V1) is False
        assert k2.counters["ddl_executed"] == 0
        assert k2.total_changes == 0
        assert k2.current_version() == 3
        assert k2.migrate(3, DDL_V1) is False
        assert k2.counters["ddl_executed"] == 0


def test_v7_bench_vacuum_count_thresholds(tmp_path: pathlib.Path) -> None:
    """代差主张:VACUUM 调用次数严格受页数阈值控制(0 → 1 → 冻结)。"""
    with SQLiteKernel(tmp_path / "bench_v.db") as k:
        k.migrate(1, ["CREATE TABLE IF NOT EXISTS blobs(id INTEGER PRIMARY KEY, p TEXT)"])
        repo = k.repo("blobs")
        assert k.vacuum_if_needed() is False  # 空库:0 次
        assert k.counters["vacuums"] == 0
        for i in range(250):  # 250 行 × 8KB ≈ 500+ 页 ≥ 200(确定性构造)
            repo.insert({"id": i, "p": "y" * 8192})
        k.execute("DELETE FROM blobs")
        assert k.page_count() >= DEFAULT_MIN_PAGE_COUNT
        assert k.vacuum_if_needed() is True  # 达阈值:恰 1 次
        assert k.counters["vacuums"] == 1
        assert k.vacuum_if_needed() is False  # 回收后再低于阈值:冻结在 1
        assert k.counters["vacuums"] == 1
        assert repo.list() == []


# ---------------------------------------------------------------------------
# 自检(A138 总控入口)
# ---------------------------------------------------------------------------


def test_kernel_selfcheck_zero_ddl_on_second_open() -> None:
    """kernel_selfcheck:离线、确定性,value == 0(二次打开零 DDL)。"""
    report = kernel_selfcheck()
    assert report["name"] == "storage.sqlite_kernel"
    assert report["value"] == 0
    assert "首次迁移执行 2 条" in report["baseline"]
    assert report["metric"]
