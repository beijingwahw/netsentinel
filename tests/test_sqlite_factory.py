# -*- coding: utf-8 -*-
"""SQLite 连接工厂守门(V10.21 席位3,对应 netsentinel/storage/kernel.py::connect)。

背景:全仓曾散落 15 处裸 ``sqlite3.connect(...)`` + 各自为政的 PRAGMA
(review_queue / four_eyes / batch_review / batch_state / sessionstore /
graph / phash / phash_lsh / site_memory / vlm_cache / discovery 缓存 /
replay CLI 补写直连 / kernel 本体)。V10.21 席位3 把它们收敛进
:func:`netsentinel.storage.kernel.connect`——**全仓唯一许可的
``sqlite3.connect`` 入口**(WAL + busy_timeout 统一口径;``readonly=True``
走 ``file:...?mode=ro`` URI;``:memory:`` 跳过 WAL 保持原语义)。

两个对账段(仿 test_timesource 的显式登记制):

1. **AST 反向扫描守门**:扫描 netsentinel/ 包内全部 .py,裸调用
   ``sqlite3.connect(...)``(任意 import 别名根名)只许出现在
   ``storage/kernel.py`` 本体与显式白名单内——新增未登记即测试失败
   (连接策略漂移无处藏身);白名单条目清零调用后不删登记也会失败
   (登记表与代码现状双向不许漂移)。

   范围注记:仓库根 ``service/app.py`` 不在 netsentinel/ 包内,天然不在
   本扫描范围;其原 URI 只读直连 ``sqlite3.connect(uri, uri=True,
   timeout=5.0)`` 已于 **V10.24 席位3 收编**——``_max_entry_id_sqlite``
   改走 ``kernel.connect(path, readonly=True)``(失败回落旧全量路径
   保留),至此仓库内再无 netsentinel/ 包外的已知裸 ``sqlite3.connect``
   持有文件;包外文件不受本守门约束,若未来有包外文件需要直连,须移入
   包内走工厂,或在此显式登记并注明理由。

2. **工厂语义测试**:busy_timeout 生效(5000)/ WAL 生效 /
   readonly 删行抛错(写操作被数据库层拒绝)/ ``:memory:`` 可用且
   不启用 WAL / 返回类型 / readonly 缺文件不误建空库 / readonly 与
   ``:memory:`` 组合的参数校验 / 跨线程(check_same_thread=False 口径)。

全部测试离线、零网络、零真实 data/ 写入(仅 tmp_path)。
"""
from __future__ import annotations

import ast
import sqlite3
import threading
from pathlib import Path

import pytest

from netsentinel.storage.kernel import BUSY_TIMEOUT_MS, connect

_ROOT = Path(__file__).resolve().parents[1]
_PKG = _ROOT / "netsentinel"

# ---------------------------------------------------------------------------
# ① AST 反向扫描守门:裸 sqlite3.connect 只许在 storage/kernel.py + 白名单
# ---------------------------------------------------------------------------

#: 工厂实现本体(全仓唯一合法的裸 sqlite3.connect 持有文件)。
_FACTORY_IMPL = "storage/kernel.py"

#: 显式白名单(当前为空)。新增条目必须附理由注释;条目清零调用后须删除
#: 登记(双向对账,仿 doctest / timesource 收编模式)。
_CONNECT_ALLOWLIST: tuple[str, ...] = ()

#: sqlite3 模块的常见本地别名根名(检测只认这些根名;工厂调用方经
#: ``from netsentinel.storage.kernel import connect as sqlite_connect`` 引入,
#: 根名是 sqlite_connect,天然不触发)。
_SQLITE_ROOTS = frozenset({"sqlite3", "_sqlite3"})

#: 合法出现文件全集(工厂本体 + 白名单)。
_ALLOWED_FILES = frozenset({_FACTORY_IMPL} | set(_CONNECT_ALLOWLIST))


def _root_name(expr: ast.expr) -> str | None:
    """沿属性链下取最左根名(``sqlite3.connect`` → ``sqlite3``)。"""
    while isinstance(expr, ast.Attribute):
        expr = expr.value
    return expr.id if isinstance(expr, ast.Name) else None


def _find_bare_connect_calls(tree: ast.Module) -> list[int]:
    """AST 定位裸 ``sqlite3.connect(...)`` 调用行号(import 别名无关)。

    只认真实调用表达式:``sqlite3.connect(`` / ``_sqlite3.connect(``;
    docstring、注释、``sqlite_connect(...)``(工厂别名)均不触发。
    """
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "connect":
            root = _root_name(func)
            if root in _SQLITE_ROOTS:
                hits.append(node.lineno)
    return hits


def _iter_pkg_files() -> list[Path]:
    """netsentinel/ 包内全部 .py(存在性哨卫:包失踪即显式失败)。"""
    if not _PKG.is_dir():
        pytest.fail(f"未找到 {_PKG}:包目录失踪,守门拒绝静默")
    return sorted(_PKG.rglob("*.py"))


def _ast_of(path: Path) -> ast.Module:
    """读入并解析 .py;语法损坏显式失败(守门要求代码可解析)。"""
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:  # pragma: no cover - 防御:源码语法损坏
        pytest.fail(f"{path} 语法解析失败:{exc}(守门要求可解析)")


def test_bare_sqlite_connect_confined_to_factory() -> None:
    """反向扫描:netsentinel/ 全部 .py 中裸 ``sqlite3.connect(...)`` 只许
    出现在 storage/kernel.py(工厂本体)与显式白名单内;新违规即红。"""
    offenders: list[str] = []
    for path in _iter_pkg_files():
        rel = path.relative_to(_PKG).as_posix()
        hits = _find_bare_connect_calls(_ast_of(path))
        if hits and rel not in _ALLOWED_FILES:
            offenders.append(f"{rel}:{hits}")
    assert not offenders, (
        "未登记文件出现裸 sqlite3.connect 调用:"
        f"{offenders};新代码必须改走 netsentinel.storage.kernel.connect"
        "(全仓唯一 sqlite3.connect 入口,WAL + busy_timeout 由工厂内置);"
        "确属豁免场景请在 _CONNECT_ALLOWLIST 显式登记并注明理由"
    )


def test_factory_impl_still_holds_the_call() -> None:
    """双向对账:工厂本体必须仍持有裸调用(实现失踪即登记失效)。"""
    path = _PKG / _FACTORY_IMPL
    assert path.is_file(), f"工厂实现文件失踪:{_FACTORY_IMPL}"
    assert _find_bare_connect_calls(_ast_of(path)), (
        "storage/kernel.py 已不含裸 sqlite3.connect 调用:"
        "连接工厂实现被移走或重命名,请同步本守门测试的 _FACTORY_IMPL 登记"
    )


def test_allowlist_entries_still_hold_the_call() -> None:
    """双向对账:白名单条目必须仍持有裸调用(清零后须删登记)。"""
    stale: list[str] = []
    for rel in sorted(_CONNECT_ALLOWLIST):
        path = _PKG / rel
        if not path.is_file():
            stale.append(f"{rel}(文件已不存在)")
            continue
        if not _find_bare_connect_calls(_ast_of(path)):
            stale.append(f"{rel}(该文件已无裸 sqlite3.connect,应从白名单移除)")
    assert not stale, (
        f"白名单出现失效条目:{stale};请同步增删 _CONNECT_ALLOWLIST 登记"
    )


# ---------------------------------------------------------------------------
# ② 工厂语义测试
# ---------------------------------------------------------------------------


def test_connect_returns_sqlite3_connection(tmp_path: Path) -> None:
    """返回类型恰为 sqlite3.Connection(str / Path 入参均可)。"""
    for path in (str(tmp_path / "a.db"), tmp_path / "b.db"):
        conn = connect(path)
        try:
            assert isinstance(conn, sqlite3.Connection)
        finally:
            conn.close()


def test_connect_busy_timeout_applied(tmp_path: Path) -> None:
    """读写与只读、内存连接统一 busy_timeout=BUSY_TIMEOUT_MS(5000)。"""
    db = tmp_path / "busy.db"
    rw = connect(db)
    try:
        assert int(rw.execute("PRAGMA busy_timeout").fetchone()[0]) == BUSY_TIMEOUT_MS
        assert BUSY_TIMEOUT_MS == 5000
    finally:
        rw.close()
    ro = connect(db, readonly=True)
    try:
        assert int(ro.execute("PRAGMA busy_timeout").fetchone()[0]) == BUSY_TIMEOUT_MS
    finally:
        ro.close()
    mem = connect(":memory:")
    try:
        assert int(mem.execute("PRAGMA busy_timeout").fetchone()[0]) == BUSY_TIMEOUT_MS
    finally:
        mem.close()


def test_connect_wal_applied(tmp_path: Path) -> None:
    """文件库缺省启用 WAL(与全仓既有连接策略一致)。"""
    conn = connect(tmp_path / "wal.db")
    try:
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        assert mode == "wal"
    finally:
        conn.close()


def test_connect_memory_skips_wal_and_works() -> None:
    """:memory: 可用:建表/写入/读回全通;journal_mode 保持内存语义(非 WAL)。"""
    conn = connect(":memory:")
    try:
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        assert mode != "wal"  # 内存库不支持 WAL,工厂跳过(保持原语义)
        conn.execute("CREATE TABLE t(x INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()
        assert conn.execute("SELECT x FROM t").fetchone()[0] == 42
    finally:
        conn.close()


def test_connect_readonly_rejects_writes(tmp_path: Path) -> None:
    """readonly=True:读得到;写(DELETE)抛 sqlite3.OperationalError。"""
    db = tmp_path / "ro.db"
    rw = connect(db)
    try:
        rw.execute("CREATE TABLE t(x INTEGER)")
        rw.execute("INSERT INTO t VALUES (1), (2)")
        rw.commit()
    finally:
        rw.close()
    ro = connect(db, readonly=True)
    try:
        assert ro.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("DELETE FROM t")
        ro.commit()
    finally:
        ro.close()
    # 只读连接没有改到数据:再开读写连接验证行数不变。
    rw2 = connect(db)
    try:
        assert rw2.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
    finally:
        rw2.close()


def test_connect_readonly_reads_wal_database(tmp_path: Path) -> None:
    """readonly 连接可正常读 WAL 库(工厂建出的库即 WAL 模式)。"""
    db = tmp_path / "wal_ro.db"
    rw = connect(db)
    try:
        rw.execute("CREATE TABLE t(x TEXT)")
        rw.execute("INSERT INTO t VALUES ('hello')")
        rw.commit()
    finally:
        rw.close()
    ro = connect(db, readonly=True)
    try:
        assert ro.execute("SELECT x FROM t").fetchone()[0] == "hello"
    finally:
        ro.close()


def test_connect_readonly_missing_file_never_creates(tmp_path: Path) -> None:
    """readonly 对不存在的文件抛 OperationalError,且绝不误建空库文件
    (读写连接的 sqlite 默认行为是静默建空库——只读消费方的期望语义)。"""
    missing = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        connect(missing, readonly=True)
    assert not missing.exists()


def test_connect_readonly_memory_rejected() -> None:
    """:memory: + readonly=True 组合无意义,工厂显式拒绝(中文 ValueError)。"""
    with pytest.raises(ValueError):
        connect(":memory:", readonly=True)


def test_connect_readonly_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """readonly 对相对路径同样成立(经 resolve().as_uri() 规范转义)。"""
    db = tmp_path / "rel.db"
    rw = connect(db)
    try:
        rw.execute("CREATE TABLE t(x INTEGER)")
        rw.commit()
    finally:
        rw.close()
    monkeypatch.chdir(tmp_path)
    ro = connect("rel.db", readonly=True)
    try:
        assert ro.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    finally:
        ro.close()


def test_connect_cross_thread_usable(tmp_path: Path) -> None:
    """check_same_thread=False 口径:连接可在另一线程上执行语句
    (全仓惯例:跨线程串行化由调用方锁承担,连接本身不设线程哨兵)。"""
    conn = connect(tmp_path / "thread.db")
    try:
        conn.execute("CREATE TABLE t(x INTEGER)")
        conn.commit()
        results: list[int] = []

        def _worker() -> None:
            results.append(int(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]))

        thread = threading.Thread(target=_worker)
        thread.start()
        thread.join(timeout=10)
        assert results == [0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ③ 迁移对账代表样本:领地模块的连接确实经工厂(口径全仓一致)
# ---------------------------------------------------------------------------


def test_migrated_modules_use_factory_pragmas(tmp_path: Path) -> None:
    """代表样本:review_queue / graph / vlm_cache / batch_state 打开的连接
    busy_timeout=5000 且文件库为 WAL(工厂口径的活体证据)。"""
    from netsentinel.decision.review_queue import ReviewQueue
    from netsentinel.intel.graph import EvidenceGraph
    from netsentinel.submit.batch_state import BatchState
    from netsentinel.vision.vlm_cache import VlmCache

    rq = ReviewQueue(str(tmp_path / "review.db"))
    try:
        assert int(rq._conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 5000
        assert str(rq._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    finally:
        rq.close()

    graph = EvidenceGraph(str(tmp_path / "graph.db"))
    try:
        assert int(graph._conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 5000
        assert str(graph._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    finally:
        graph.close()

    cache = VlmCache(str(tmp_path / "vlm.db"))
    try:
        assert int(cache._conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 5000
        assert str(cache._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    finally:
        cache.close()

    st = BatchState(str(tmp_path / "batch.db"))
    try:
        assert int(st._conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 5000
        assert str(st._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    finally:
        st.close()
