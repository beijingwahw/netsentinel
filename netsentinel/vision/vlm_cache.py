"""VLM 调用缓存与每日预算(V2 · A23,负责人补齐实现;V5 · A86 升级)。

GLM 视觉模型按次计费:同一张图(相同 sha256 + 相同模型 + 相同提示词版本)
的评分结果缓存 30 天;每日调用量硬顶 vlm_daily_budget,超限抛
:class:`VlmBudgetExceeded`,绝不静默超支。

线程安全:sqlite3 ``check_same_thread=False`` + 全操作持 ``threading.Lock``。
损坏数据库文件自动重建(缓存可丢,预算计数从零重新开始并告警)。

V5 升级(A86):

- sqlite 连接一律 ``PRAGMA journal_mode=WAL`` + ``busy_timeout=5000``(读写并发、
  跨进程锁等待由操作系统级忙等兜底);
- 新增批量接口 :meth:`VlmCache.get_many` / :meth:`VlmCache.put_many`(单事务
  ``executemany``,批量去逐条循环;既有 get/put 签名与行为不变);
- TTL 清理在"读时删单键"基础上加"当日顺手批量清全部过期行"(每日至多一次,
  默认行为不变:未过期条目永远不受影响);
- 接入遥测:``vlm_cache.hit`` / ``vlm_cache.miss`` 计数与 ``vlm_cache.get`` 等计时;
- ``usage`` 表按 ``day`` 查询——``day TEXT PRIMARY KEY`` 即自带索引
  (``sqlite_autoindex_usage_1``),无需额外建索引。

用法示例::

    cache = VlmCache("data/vlm_cache.db", daily_limit=200)
    hit = cache.get("glm-4v", "v2.1", image_sha256)
    if hit is None:
        cache.spend_one()                     # 预算计数(超限抛 VlmBudgetExceeded)
        cache.put("glm-4v", "v2.1", image_sha256, {"nsfw_prob": 0.9})
    # 批量(单事务,一次 executemany):
    payloads = cache.get_many([("glm-4v", "v2.1", sha1), ("glm-4v", "v2.1", sha2)])
    cache.put_many([("glm-4v", "v2.1", sha, {"nsfw_prob": 0.9}) for sha in shas])
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Iterable

from netsentinel import telemetry

__all__ = ["VlmBudgetExceeded", "VlmCache", "DEFAULT_DAILY_LIMIT"]

logger = logging.getLogger(__name__)

#: 缓存有效期(天)
TTL_DAYS = 30

#: 每日调用预算缺省值(可被 cfg.vlm_daily_budget 覆盖)
DEFAULT_DAILY_LIMIT = 200

#: sqlite 忙等上限(毫秒):跨进程写冲突时等待而非立刻报 SQLITE_BUSY
_BUSY_TIMEOUT_MS = 5000

#: get_many 单条 SELECT 的最大键数(行值 IN 参数 = 3 × 键数,留出旧版 sqlite 999 参数上限余量)
_SELECT_CHUNK = 256


class VlmBudgetExceeded(RuntimeError):
    """当日 VLM 调用预算已用尽。"""

    def __init__(self, used: int, limit: int) -> None:
        self.used = used
        self.limit = limit
        super().__init__(
            f"当日 VLM 调用预算已用尽:{used}/{limit}。"
            "可在配置中调大 vlm_daily_budget,或等待明日自动重置;"
            "亦可利用 vlm_cache 的 30 天缓存减少重复调用。"
        )


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


class VlmCache:
    """(model, prompt_version, image_sha256) → 评分载荷 的持久缓存 + 每日预算计数。"""

    def __init__(self, db_path: str, daily_limit: int = DEFAULT_DAILY_LIMIT) -> None:
        self.db_path = str(db_path)
        self.daily_limit = int(daily_limit)
        self._lock = threading.Lock()
        #: 当日已做过"顺手批量清过期"的标记(每日至多一次,见 _expire)
        self._last_sweep_day: str | None = None
        self._conn = self._connect()

    # -- 基础设施 ---------------------------------------------------------
    _CREATE_CACHE = (
        "CREATE TABLE IF NOT EXISTS cache ("
        " model TEXT NOT NULL, prompt_version TEXT NOT NULL,"
        " image_sha256 TEXT NOT NULL, payload TEXT NOT NULL,"
        " created_at TEXT NOT NULL,"
        " PRIMARY KEY (model, prompt_version, image_sha256))"
    )
    _CREATE_USAGE = (
        # day TEXT PRIMARY KEY 即索引(sqlite 自动建 sqlite_autoindex_usage_1),
        # budget_state / spend_one 的 WHERE day=? 走索引扫描。
        "CREATE TABLE IF NOT EXISTS usage ("
        " day TEXT PRIMARY KEY, used INTEGER NOT NULL DEFAULT 0)"
    )
    _UPSERT_CACHE = (
        "INSERT INTO cache(model, prompt_version, image_sha256, payload, created_at) "
        "VALUES(?,?,?,?,?) "
        "ON CONFLICT(model, prompt_version, image_sha256) "
        "DO UPDATE SET payload=excluded.payload, created_at=excluded.created_at"
    )
    #: 顺手批量清:created_at 带/不带时区偏移均由 strftime 归一为 UTC 秒;解析失败行为 NULL,
    #: NULL 比较不成立 → 与 get() 的"解析失败视为未过期"语义一致,绝不误删。
    _SWEEP_EXPIRED = (
        "DELETE FROM cache WHERE"
        " CAST(strftime('%s','now') AS INTEGER)"
        " - CAST(strftime('%s', created_at) AS INTEGER) >= ?"
    )

    def _connect(self) -> sqlite3.Connection:
        path = Path(self.db_path)
        if str(path.parent):
            path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return self._open_and_init(path)
        except sqlite3.DatabaseError as exc:
            logger.warning("VLM 缓存库损坏,已重建(%s):%s", self.db_path, exc)
            for suffix in ("", "-wal", "-shm", "-journal"):
                candidate = Path(str(path) + suffix)
                if candidate.exists():
                    try:
                        candidate.unlink()
                    except OSError:
                        logger.warning("无法删除损坏缓存文件:%s", candidate)
            return self._open_and_init(path)

    def _open_and_init(self, path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(path), check_same_thread=False)
        try:
            # V5:一律 WAL + 忙等 5s(读写并发 / 跨进程锁等待)
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(self._CREATE_CACHE)
            conn.execute(self._CREATE_USAGE)
            conn.commit()
        except sqlite3.DatabaseError:
            # Windows 下文件被句柄占用,必须先关连接才能删除重建
            try:
                conn.close()
            except Exception:
                pass
            raise
        return conn

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- 缓存 -------------------------------------------------------------
    @staticmethod
    def _is_expired(created_at: str) -> bool:
        """created_at 是否已过 TTL;解析失败视为未过期(与既有 get 语义一致)。"""
        try:
            created = _dt.datetime.fromisoformat(created_at)
        except ValueError:
            return False
        return (_dt.datetime.now(created.tzinfo) - created).days >= TTL_DAYS

    @staticmethod
    def _load_payload(payload_raw: str) -> dict | None:
        """payload JSON → dict;解析失败或非 dict → None(调用方按未命中处理)。"""
        try:
            data = json.loads(payload_raw)
        except (json.JSONDecodeError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    def _expire(self, key: tuple[str, str, str]) -> None:
        """删除一个过期键:当日首次顺手批量清全部过期行,之后仅删本键。

        批量清每个日历日至多一次(``_last_sweep_day`` 节流),避免大表上
        每次读命中过期都做全表扫描;未过期行永不受影响,默认行为不变。
        """
        with self._lock:
            today = _dt.date.today().isoformat()
            if self._last_sweep_day != today:
                self._last_sweep_day = today
                self._conn.execute(self._SWEEP_EXPIRED, (TTL_DAYS * 86400,))
            else:
                self._conn.execute(
                    "DELETE FROM cache "
                    "WHERE model=? AND prompt_version=? AND image_sha256=?",
                    key,
                )
            self._conn.commit()

    def get(self, model: str, prompt_version: str, image_sha256: str) -> dict | None:
        with telemetry.timer("vlm_cache.get"):
            key = (model, prompt_version, image_sha256)
            with self._lock:
                row = self._conn.execute(
                    "SELECT payload, created_at FROM cache "
                    "WHERE model=? AND prompt_version=? AND image_sha256=?",
                    key,
                ).fetchone()
            if row is None:
                telemetry.inc("vlm_cache.miss")
                return None
            payload_raw, created_at = row
            if self._is_expired(created_at):
                self._expire(key)
                telemetry.inc("vlm_cache.miss")
                return None
            data = self._load_payload(payload_raw)
            if data is None:
                telemetry.inc("vlm_cache.miss")
                return None
            telemetry.inc("vlm_cache.hit")
            return data

    def get_many(
        self, keys: list[tuple[str, str, str]]
    ) -> dict[tuple[str, str, str], dict]:
        """批量读缓存:返回 ``{键: payload}``(仅命中项;过期/缺失/坏载荷按未命中)。

        单条 SELECT 用行值 ``IN (VALUES ...)`` 按块(每块 ≤ :data:`_SELECT_CHUNK` 键)
        取回,替代逐键循环查询;重复键自动去重;命中/未命中逐键计入
        ``vlm_cache.hit`` / ``vlm_cache.miss`` 遥测。
        """
        unique = list(dict.fromkeys(tuple(key) for key in keys))
        if not unique:
            return {}
        hits: dict[tuple[str, str, str], dict] = {}
        expired: list[tuple[str, str, str]] = []
        with telemetry.timer("vlm_cache.get_many"):
            for start in range(0, len(unique), _SELECT_CHUNK):
                chunk = unique[start : start + _SELECT_CHUNK]
                placeholders = ",".join(["(?,?,?)"] * len(chunk))
                params = [value for key in chunk for value in key]
                with self._lock:
                    rows = self._conn.execute(
                        "SELECT model, prompt_version, image_sha256, payload, created_at"
                        f" FROM cache WHERE (model, prompt_version, image_sha256)"
                        f" IN (VALUES {placeholders})",
                        params,
                    ).fetchall()
                for model, prompt_version, image_sha256, payload_raw, created_at in rows:
                    key = (model, prompt_version, image_sha256)
                    if self._is_expired(created_at):
                        expired.append(key)
                        continue
                    data = self._load_payload(payload_raw)
                    if data is not None:
                        hits[key] = data
            for key in expired:
                self._expire(key)
            telemetry.inc("vlm_cache.hit", len(hits))
            telemetry.inc("vlm_cache.miss", len(unique) - len(hits))
        return hits

    def put(
        self, model: str, prompt_version: str, image_sha256: str, payload: dict
    ) -> None:
        with self._lock:
            self._conn.execute(
                self._UPSERT_CACHE,
                (
                    model,
                    prompt_version,
                    image_sha256,
                    json.dumps(payload, ensure_ascii=False),
                    _now_iso(),
                ),
            )
            self._conn.commit()

    def put_many(self, items: Iterable[tuple[str, str, str, dict]]) -> None:
        """批量写缓存:``items`` 为 ``(model, prompt_version, image_sha256, payload)`` 序列。

        全部行先在锁外完成 JSON 序列化(任一失败则整批不落库,单事务原子),
        锁内一次 ``executemany`` + 一次 ``commit``,替代逐条 put 的 N 次事务。
        空输入为无操作(不触碰数据库)。
        """
        rows = [
            (
                model,
                prompt_version,
                image_sha256,
                json.dumps(payload, ensure_ascii=False),
                _now_iso(),
            )
            for model, prompt_version, image_sha256, payload in items
        ]
        if not rows:
            return
        with telemetry.timer("vlm_cache.put_many"):
            with self._lock:
                self._conn.executemany(self._UPSERT_CACHE, rows)
                self._conn.commit()

    # -- 预算 -------------------------------------------------------------
    @staticmethod
    def _today(day: str | None = None) -> str:
        if day:
            return day
        return _dt.date.today().isoformat()

    def budget_state(self, day: str | None = None) -> dict:
        today = self._today(day)
        with self._lock:
            row = self._conn.execute(
                "SELECT used FROM usage WHERE day=?", (today,)
            ).fetchone()
        used = int(row[0]) if row else 0
        return {"day": today, "used": used, "limit": self.daily_limit}

    def spend_one(self, day: str | None = None) -> None:
        today = self._today(day)
        with self._lock:
            row = self._conn.execute(
                "SELECT used FROM usage WHERE day=?", (today,)
            ).fetchone()
            used = int(row[0]) if row else 0
            if used >= self.daily_limit:
                raise VlmBudgetExceeded(used, self.daily_limit)
            self._conn.execute(
                "INSERT INTO usage(day, used) VALUES(?, 1) "
                "ON CONFLICT(day) DO UPDATE SET used = used + 1",
                (today,),
            )
            self._conn.commit()

    # -- 观测 -------------------------------------------------------------
    def stats(self) -> dict:
        with self._lock:
            total = int(
                self._conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
            )
            by_model = {
                row[0]: int(row[1])
                for row in self._conn.execute(
                    "SELECT model, COUNT(*) FROM cache GROUP BY model"
                ).fetchall()
            }
        return {
            "cache_rows": total,
            "by_model": by_model,
            "today": self.budget_state(),
        }
