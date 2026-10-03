"""存储内核 · 统一 SQLite 存储底座(NetSentinel V7 · A131)。

全仓已散落 8+ 个各自为政的 sqlite 封装(review_queue / vlm_cache / graph /
phash / site_memory / batch_state / four_eyes / batch_review ...)。本模块把
它们的共同底座收敛成一个可复用内核,**纯新增文件,零改动既有模块**(红线 29):

- 连接策略与全仓一致:``PRAGMA journal_mode=WAL`` + ``busy_timeout=5000`` +
  ``check_same_thread=False`` 单连接 + 全操作共持一把 ``threading.Lock``
  (读写并发由 WAL 承担、跨进程锁等待由忙等承担、跨线程串行化由锁承担);
- :meth:`SQLiteKernel.migrate` 版本化迁移:``schema_version`` 单行版本表,
  相同版本重复迁移**零 DDL 执行**(幂等,红线 31:以执行计数断言),
  降级(目标版本低于当前)抛中文 ``ValueError``;
- :meth:`SQLiteKernel.execute` / :meth:`SQLiteKernel.query` 裸 SQL 口
  (参数化,行转 dict);
- :meth:`SQLiteKernel.repo` 生成表级 CRUD 助手 :class:`Repo`
  (get/insert/update/delete/list,全部参数化防注入;表名/列名/主键名按
  标识符白名单校验后加引号,注入串一律当字面量);
- :meth:`SQLiteKernel.vacuum_if_needed` 按 ``page_count`` 阈值惰性 VACUUM
  (低于阈值零 VACUUM,同样以调用计数断言);
- **既有库兼容**:打开任意既有 sqlite(如 ``review_queue.db``)不建版本表、
  不迁移、不改写任何数据——版本表只在显式调用 ``migrate`` 时创建。

观测:``counters`` 字典(``statements`` / ``ddl_executed`` / ``vacuums``)
提供确定性的操作计数(基准与自检都用它,绝不依赖墙钟,红线 31);迁移与
VACUUM 接入 ``netsentinel.telemetry`` 计数。

用法示例::

    with SQLiteKernel("data/app.db") as k:
        k.migrate(1, ["CREATE TABLE IF NOT EXISTS sites(url TEXT PRIMARY KEY, score REAL)"])
        sites = k.repo("sites", pk="url")
        sites.insert({"url": "https://example.com", "score": 0.9})
        hot = sites.list(where={"score": 0.9})            # 参数化等值过滤
        for row in k.query("SELECT url FROM sites WHERE score >= ?", (0.5,)):
            ...
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Any, Sequence

from netsentinel import telemetry

__all__ = ["BUSY_TIMEOUT_MS", "Repo", "SQLiteKernel", "kernel_selfcheck"]

logger = logging.getLogger(__name__)

#: sqlite 忙等上限(毫秒):跨进程写冲突时等待而非立刻报 SQLITE_BUSY(全仓统一)。
BUSY_TIMEOUT_MS: int = 5000

#: VACUUM 触发的缺省页数阈值:低于该页数的小库不浪费 IO 做碎片整理。
DEFAULT_MIN_PAGE_COUNT: int = 200

#: 版本表名(migrate 首次调用时创建;仅打开既有库绝不创建)。
VERSION_TABLE: str = "schema_version"

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_CREATE_VERSION_SQL = (
    "CREATE TABLE IF NOT EXISTS schema_version ("
    " id INTEGER PRIMARY KEY CHECK (id = 1),"
    " version INTEGER NOT NULL,"
    " applied_at TEXT NOT NULL DEFAULT '')"
)
_READ_VERSION_SQL = "SELECT version FROM schema_version WHERE id = 1"
_WRITE_VERSION_SQL = (
    "INSERT INTO schema_version(id, version, applied_at) VALUES(1, ?, ?) "
    "ON CONFLICT(id) DO UPDATE SET version=excluded.version,"
    " applied_at=excluded.applied_at"
)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def _check_identifier(name: Any, what: str) -> str:
    """标识符白名单校验:仅允许 ``[A-Za-z_][A-Za-z0-9_]*``。

    表名/列名无法走 SQL 参数绑定,必须拼进语句;这里先按白名单拒绝一切
    特殊字符(分号/引号/空格/括号……),再把合法名字加双引号,从两个方向
    封死标识符注入。非法即抛中文 ``ValueError``。
    """
    if not isinstance(name, str) or not _IDENTIFIER_RE.fullmatch(name):
        raise ValueError(
            f"非法{what}:{name!r}(仅允许字母/数字/下划线,且以字母或下划线开头)"
        )
    return name


def _q(identifier: str) -> str:
    """把已校验的标识符加双引号(SQL 合法引用形式)。"""
    return '"' + identifier + '"'


class SQLiteKernel:
    """统一 SQLite 存储内核(WAL + busy_timeout + 单连接 + 全局锁,线程安全)。

    打开时只设 PRAGMA,**不做任何 DDL**:既有库(含 review_queue.db 等其他
    模块的库)可以安全打开做只读/读写操作而不被迁移、不被改写;``schema_version``
    版本表仅在显式调用 :meth:`migrate` 时创建。支持 ``with`` 上下文。
    """

    def __init__(self, path: str | Path) -> None:
        self.db_path = str(path)
        self._lock = threading.Lock()
        #: 确定性操作计数(红线 31:基准以计数断言,不依赖墙钟):
        #: statements=经内核执行的全部 SQL 条数;ddl_executed=migrate 实际
        #: 执行的用户 DDL 条数;vacuums=实际执行的 VACUUM 次数。
        self.counters: dict[str, int] = {
            "statements": 0,
            "ddl_executed": 0,
            "vacuums": 0,
        }
        if self.db_path != ":memory:":
            parent = Path(self.db_path).parent
            if str(parent):
                parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        # 连接策略与全仓一致(review_queue / vlm_cache / graph 等):
        # WAL 允许读写并发;忙等 5s 让跨进程锁冲突等待而非立刻失败。
        self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.commit()
        logger.debug("存储内核已就绪:%s", self.db_path)

    # ------------------------------------------------------------------
    # 底层:唯一执行咽喉(计数在此累加,任何路径不得绕过)
    # ------------------------------------------------------------------

    def _exec(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> sqlite3.Cursor:
        """锁内执行一条语句并累计 statements 计数(调用方须已持锁)。

        ``params=None`` 时必须省略实参(sqlite3 不接受显式 None 参数序列)。
        """
        self.counters["statements"] += 1
        if params is None:
            return self._conn.execute(sql)
        return self._conn.execute(sql, params)

    @property
    def total_changes(self) -> int:
        """底层连接累计改写行数(sqlite 原生计数,零改写断言的硬证据)。"""
        return int(self._conn.total_changes)

    # ------------------------------------------------------------------
    # 裸 SQL 口
    # ------------------------------------------------------------------

    def execute(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> sqlite3.Cursor:
        """执行单条 SQL(写语句即时 commit),返回游标(裸口,参数化)。"""
        with self._lock:
            cur = self._exec(sql, params)
            self._conn.commit()
            return cur

    def query(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> list[dict[str, Any]]:
        """执行查询并把每一行转成 ``{列名: 值}`` dict(无行返回空列表)。"""
        with self._lock:
            cur = self._exec(sql, params)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
        return [dict(zip(cols, row)) for row in rows]

    # ------------------------------------------------------------------
    # 版本化迁移
    # ------------------------------------------------------------------

    def current_version(self) -> int | None:
        """当前 schema 版本;版本表不存在或未写过版本返回 ``None``(未迁移)。

        只读,绝不建表——"打开既有库不迁移不改写"的保证之一。
        """
        with self._lock:
            return self._current_version_locked()

    def _current_version_locked(self) -> int | None:
        row = self._exec(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (VERSION_TABLE,),
        ).fetchone()
        if row is None:
            return None
        vrow = self._exec(_READ_VERSION_SQL).fetchone()
        return int(vrow[0]) if vrow is not None else None

    def migrate(self, version: int, ddl: list[str]) -> bool:
        """把库迁移到 ``version``(DDL 语句列表逐条执行后写版本表)。

        幂等语义(红线 31,以 :attr:`counters` 执行计数断言):

        - 库里还没有 ``schema_version`` 表 → 创建它,并视为未迁移(版本低于
          任何目标版本);
        - 当前版本 **等于** ``version`` → **零执行**(一条用户 DDL 都不跑、
          不改写版本行),返回 ``False``;
        - 当前版本 **低于** ``version`` → 单事务(``BEGIN IMMEDIATE``)内逐条
          执行 DDL 并写版本,任一语句失败整体回滚(已建表/已写数据全部撤销,
          版本号不动,修复后可安全重试),返回 ``True``;
        - 当前版本 **高于** ``version``(回退)→ 拒绝并抛中文 ``ValueError``。

        :param ddl: SQL 语句列表;建议幂等写法(``CREATE TABLE IF NOT
            EXISTS`` 等),失败重试时已执行部分会随事务回滚,故重跑安全。
        """
        version = int(version)
        statements = [str(s) for s in ddl]
        with self._lock:
            current = self._current_version_locked()
            if current is not None and current > version:
                raise ValueError(
                    f"schema 版本不可回退:当前已是 v{current},"
                    f"拒绝迁移到更低的 v{version}(如确需降级请先备份再手动处理)"
                )
            if current == version:
                # 幂等:零 DDL 执行、零改写(测试以 ddl_executed 计数与
                # total_changes 双重断言)。
                return False
            began = False
            if not self._conn.in_transaction:
                self._conn.execute("BEGIN IMMEDIATE")
                began = True
            try:
                self._exec(_CREATE_VERSION_SQL)
                for sql in statements:
                    self._exec(sql)
                    self.counters["ddl_executed"] += 1
                self._exec(_WRITE_VERSION_SQL, (version, _now_iso()))
                self._conn.commit()
            except BaseException:
                if began:
                    self._conn.rollback()
                raise
        telemetry.inc("storage.kernel.migrate")
        logger.info("schema 已迁移到 v%s(执行 DDL %d 条)", version, len(statements))
        return True

    # ------------------------------------------------------------------
    # CRUD 助手
    # ------------------------------------------------------------------

    def repo(self, table: str, pk: str = "id") -> Repo:
        """返回表 ``table`` 的参数化 CRUD 助手(与本内核共用连接与锁)。"""
        return Repo(self, table, pk)

    # ------------------------------------------------------------------
    # 空间回收
    # ------------------------------------------------------------------

    def page_count(self) -> int:
        """当前数据库页数(``PRAGMA page_count``,只读)。"""
        with self._lock:
            row = self._exec("PRAGMA page_count").fetchone()
            return int(row[0])

    def vacuum_if_needed(self, min_page_count: int = DEFAULT_MIN_PAGE_COUNT) -> bool:
        """页数达到阈值才 VACUUM,小库零开销直接跳过。

        :return: 是否真的执行了 VACUUM(``counters["vacuums"]`` 同步 +1,
            低于阈值时计数不变——两分支都以计数断言,红线 31)。
        """
        with self._lock:
            pages = int(self._exec("PRAGMA page_count").fetchone()[0])
            if pages < int(min_page_count):
                return False
            self._exec("VACUUM")
            self.counters["vacuums"] += 1
        telemetry.inc("storage.kernel.vacuum")
        logger.debug("已 VACUUM(整理前 %d 页 ≥ 阈值 %d)", pages, min_page_count)
        return True

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def close(self) -> None:
        """关闭底层连接(幂等容忍;关闭异常不上抛)。"""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover - 关闭异常无需上抛
                logger.debug("关闭存储内核连接时出现异常", exc_info=True)

    def __enter__(self) -> SQLiteKernel:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class Repo:
    """单表参数化 CRUD 助手(get / insert / update / delete / list)。

    由 :meth:`SQLiteKernel.repo` 构造,与宿主内核共用同一连接与同一把锁,
    因此天然线程安全。所有值一律走 ``?`` 参数绑定(注入串只会被当字面量);
    表名/列名/主键名无法参数化,经 :func:`_check_identifier` 白名单校验后
    加双引号拼入。写操作即时 commit(全仓惯例)。
    """

    def __init__(self, kernel: SQLiteKernel, table: str, pk: str = "id") -> None:
        self.kernel = kernel
        self.table = _check_identifier(table, "表名")
        self.pk = _check_identifier(pk, "主键列名")

    # -- 读 ---------------------------------------------------------------

    def get(self, pk_value: Any) -> dict[str, Any] | None:
        """按主键取一行(行转 dict);不存在返回 ``None``。"""
        k = self.kernel
        sql = "SELECT * FROM {} WHERE {} = ?".format(_q(self.table), _q(self.pk))
        with k._lock:
            cur = k._exec(sql, (pk_value,))
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
        return dict(zip(cols, row)) if row is not None else None

    def list(
        self,
        where: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """列出全部/过滤行,按主键升序,行转 dict。

        :param where: 等值过滤字典(全部 ``?`` 参数化):``{"status": "pending"}``
            → ``WHERE "status" = ?``;键按标识符白名单校验。
        :param limit: 非负整数行数上限(``LIMIT ?`` 同样参数化)。
        """
        k = self.kernel
        sql = "SELECT * FROM {}".format(_q(self.table))
        params: list[Any] = []
        if where:
            cols = [_check_identifier(c, "列名") for c in where]
            conds = " AND ".join("{} = ?".format(_q(c)) for c in cols)
            sql += " WHERE " + conds
            params.extend(where.values())
        sql += " ORDER BY {} ASC".format(_q(self.pk))
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ValueError(f"limit 必须是非负整数,得到:{limit!r}")
            sql += " LIMIT ?"
            params.append(int(limit))
        with k._lock:
            cur = k._exec(sql, tuple(params))
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        return [dict(zip(cols, row)) for row in rows]

    # -- 写(即时 commit)---------------------------------------------------

    def insert(self, row: dict[str, Any]) -> int:
        """插入一行(键为列名,经白名单校验),返回新行主键(``lastrowid``)。

        空字典 → ``INSERT ... DEFAULT VALUES``(全默认值行)。
        """
        if not isinstance(row, dict):
            raise ValueError(f"insert 需要 dict(列→值),得到 {type(row).__name__}")
        k = self.kernel
        with k._lock:
            if not row:
                cur = k._exec(
                    "INSERT INTO {} DEFAULT VALUES".format(_q(self.table))
                )
            else:
                cols = [_check_identifier(c, "列名") for c in row]
                col_sql = ", ".join(_q(c) for c in cols)
                placeholders = ", ".join("?" for _ in cols)
                cur = k._exec(
                    "INSERT INTO {} ({}) VALUES ({})".format(
                        _q(self.table), col_sql, placeholders
                    ),
                    tuple(row.values()),
                )
            k._conn.commit()
            return int(cur.lastrowid or 0)

    def update(self, pk_value: Any, fields: dict[str, Any]) -> int:
        """按主键更新指定列(参数化),返回受影响行数(0=主键不存在)。"""
        if not isinstance(fields, dict):
            raise ValueError(
                f"update 需要 dict(列→值),得到 {type(fields).__name__}"
            )
        if not fields:
            raise ValueError("update 至少要给一列(空 dict 请改用无操作)")
        k = self.kernel
        cols = [_check_identifier(c, "列名") for c in fields]
        set_sql = ", ".join("{} = ?".format(_q(c)) for c in cols)
        sql = "UPDATE {} SET {} WHERE {} = ?".format(
            _q(self.table), set_sql, _q(self.pk)
        )
        params = (*fields.values(), pk_value)
        with k._lock:
            cur = k._exec(sql, params)
            k._conn.commit()
            return int(cur.rowcount)

    def delete(self, pk_value: Any) -> int:
        """按主键删除一行(参数化),返回受影响行数(0=主键不存在)。"""
        k = self.kernel
        sql = "DELETE FROM {} WHERE {} = ?".format(_q(self.table), _q(self.pk))
        with k._lock:
            cur = k._exec(sql, (pk_value,))
            k._conn.commit()
            return int(cur.rowcount)


def kernel_selfcheck() -> dict[str, Any]:
    """A138 约定的内核自检:离线、确定性(计数,零墙钟)。

    场景 = 本内核的代差主张:**迁移幂等**——同一套 DDL 二次打开再迁移,
    用户 DDL 执行数为 0(首次为 ``baseline`` 条)。
    """
    ddl = [
        "CREATE TABLE IF NOT EXISTS selfcheck_a(a TEXT)",
        "CREATE TABLE IF NOT EXISTS selfcheck_b(b TEXT)",
    ]
    with tempfile.TemporaryDirectory(prefix="netsentinel-storage-selfcheck-") as tmp:
        db_path = str(Path(tmp) / "selfcheck.db")
        with SQLiteKernel(db_path) as kernel:
            kernel.migrate(1, ddl)
            first = kernel.counters["ddl_executed"]
        with SQLiteKernel(db_path) as kernel2:  # 二次打开(新实例)
            kernel2.migrate(1, ddl)
            second = kernel2.counters["ddl_executed"]
    return {
        "name": "storage.sqlite_kernel",
        "metric": "migrate 二次打开的用户 DDL 执行数",
        "value": second,
        "baseline": f"首次迁移执行 {first} 条 DDL",
    }
