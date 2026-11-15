# -*- coding: utf-8 -*-
"""站级扫描日志:断点续跑的"已完成站"账本(NetSentinel V10.35 · P2 阶段3)。

**为什么需要**:批量扫描的昂贵段是站内采集(真实慢站 40–80s/页 × 3 页),
而 ``resume`` 语义此前只覆盖举报半程(:mod:`netsentinel.submit.batch_state`);
扫描半程的唯一兜底是 vlm_cache 的图级幂等(重扫不重复扣 VLM 预算)与
site_memory 的指纹跳过(省判定不省抓取)。上百站的批量中途崩溃后重跑,
已完成站会被**整站重新抓取**——本模块把这部分墙钟也省掉:每站扫描结局
落 SQLite 一行,续跑时按批次号跳过已终态的站,在飞站(无记录)重扫
(缓存兜底,幂等安全)。

**模式对齐**:库表结构 / 连接工厂 / WAL + busy_timeout / 遥测口径全部
照抄 :mod:`netsentinel.submit.batch_state`(A113)——``batch_id`` 由调用方
生成(本模块提供 :meth:`ScanJournal.new_batch` 时间戳惯用式),主键
``(batch_id, url)`` 天然幂等(重记覆盖,绝不双行)。

**诚实边界(如实登记)**:

- 日志只记 URL 与结局枚举(``done`` / ``failed``),**不存报告/证据/
  分数**(那些在复核队列与证据包里有唯一事实源;这里只要"这站做完了没");
- 指纹跳过(``skipped``)的站 pool 摘要里只有计数没有 URL 清单——
  日志**不记 skipped**,续跑时这类站重扫一次,由 site_memory 再次
  指纹跳过(判定侧幂等,代价可忽略);
- ``failed`` 站默认**不进**"已完成"集合(:meth:`done_urls` 只认
  ``done``)——失败站在续跑时重试一次是运营直觉;想连失败也跳过,
  调用方自行用 :meth:`processed_urls`。

可观测性:``scan_journal.record`` / ``scan_journal.resume_skip`` /
``scan_journal.new_batch`` 计数器(只存名称与数字,红线 17)。

用法示例(离线)::

    from netsentinel.ops.scan_journal import ScanJournal

    j = ScanJournal(":memory:")
    bid = j.new_batch()
    j.record(bid, "https://a.example/", "done", "suspect")
    assert j.done_urls(bid) == {"https://a.example/"}
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from netsentinel import telemetry, timesource
from netsentinel.storage.kernel import connect

__all__ = ["ScanJournal", "default_journal_path"]

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scan_journal (
    batch_id   TEXT NOT NULL,
    url        TEXT NOT NULL,
    status     TEXT NOT NULL,
    verdict    TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, url)
)
"""

#: (batch_id, status) 复合索引:done_urls / processed_urls 的过滤走索引;
#: IF NOT EXISTS 保证旧库打开时补建。
_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_scan_journal_status"
    " ON scan_journal(batch_id, status)"
)

#: 站级终态枚值(与 pool 摘要口径一致;skipped 不入账,见模块文档)。
STATUS_DONE = "done"
STATUS_FAILED = "failed"

_METRIC_NEW = "scan_journal.new_batch"
_METRIC_RECORD = "scan_journal.record"
_METRIC_RESUME_SKIP = "scan_journal.resume_skip"

#: 进程内批次号自增序号(new_batch 唯一性的同秒兜底)
_BATCH_SEQ = 0


def default_journal_path(data_dir: str) -> str:
    """扫描日志库默认路径:``<data_dir>/scan_journal.db``(父目录自动创建)。"""
    base = Path(data_dir or ".")
    return str(base / "scan_journal.db")


class ScanJournal:
    """站级扫描日志(每实例一条连接;线程安全由调用方串行化或自备锁)。

    批量流程在主线程串行调用(记录发生在 pool 返回后,见 run_flow 接线),
    与 batch_state 同款"调用方owns连接生命周期"约定:
    用完 ``close()``,或依赖 ``__exit__``。
    """

    __slots__ = ("_conn", "_path")

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        if self._path != ":memory:":
            parent = Path(self._path).parent
            if str(parent):
                parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection = connect(self._path)
        self._conn.execute(_SCHEMA_SQL)
        self._conn.execute(_INDEX_SQL)
        self._conn.commit()

    # -- 批次号 ------------------------------------------------------------
    def new_batch(self) -> str:
        """生成新批次号 ``scan-<UTC 秒级时间戳>-<进程内序号>``(可读且进程内唯一)。

        秒级时间戳保可读;尾部进程内自增序号防同秒碰撞(跨进程天然不同
        时刻,概率碰撞由 (时间戳, 序号) 组合兜住;极端正交场景由主键
        ``(batch_id, url)`` 的幂等覆盖语义兜底——重记覆盖,绝不双行)。
        """
        global _BATCH_SEQ
        bid = (
            "scan-" + timesource.now_iso()[:19].replace(":", "").replace("-", "")
            + "-" + str(_BATCH_SEQ)
        )
        _BATCH_SEQ += 1
        telemetry.inc(_METRIC_NEW)
        return bid

    # -- 记录与查询 ---------------------------------------------------------
    def record(self, batch_id: str, url: str, status: str, verdict: str = "") -> None:
        """登记一站结局(主键幂等:重记覆盖,**绝不双行**)。

        :param status: ``done`` / ``failed``(其余值照记,查询按枚值过滤)。
        :param verdict: 判定值(仅 done 有意义;只存短字符串枚值,不存报告)。
        """
        now = timesource.now_iso()
        self._conn.execute(
            "INSERT INTO scan_journal(batch_id, url, status, verdict, updated_at)"
            " VALUES(?, ?, ?, ?, ?)"
            " ON CONFLICT(batch_id, url) DO UPDATE SET"
            " status=excluded.status, verdict=excluded.verdict,"
            " updated_at=excluded.updated_at",
            (str(batch_id), str(url), str(status), str(verdict or ""), now),
        )
        self._conn.commit()
        telemetry.inc(_METRIC_RECORD)

    def _urls(self, batch_id: str, statuses: tuple[str, ...]) -> set[str]:
        if not statuses:
            return set()
        marks = ",".join("?" for _ in statuses)
        rows = self._conn.execute(
            f"SELECT url FROM scan_journal WHERE batch_id = ?"
            f" AND status IN ({marks})",  # noqa: S608 - statuses 为枚值占位
            (str(batch_id), *statuses),
        ).fetchall()
        return {str(row[0]) for row in rows}

    def done_urls(self, batch_id: str) -> set[str]:
        """该批次**成功完成**的站(续跑默认跳过集;failed 不在其中)。"""
        return self._urls(batch_id, (STATUS_DONE,))

    def processed_urls(self, batch_id: str) -> set[str]:
        """该批次**已有终态记录**的站(done + failed;想连失败也跳过时用)。"""
        return self._urls(batch_id, (STATUS_DONE, STATUS_FAILED))

    def counts(self, batch_id: str) -> dict[str, int]:
        """该批次按状态的计数视图(``{"done": n, "failed": n, "other": n}``)。"""
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM scan_journal"
            " WHERE batch_id = ? GROUP BY status",
            (str(batch_id),),
        ).fetchall()
        out: dict[str, int] = {"done": 0, "failed": 0, "other": 0}
        for status, num in rows:
            key = str(status) if str(status) in out else "other"
            out[key] += int(num)
        return out

    def resume_skip_count(self, skipped: int) -> None:
        """续跑跳过计数(由调用方在过滤后回填;只记数字)。"""
        if skipped > 0:
            telemetry.inc(_METRIC_RESUME_SKIP, skipped)

    # -- 生命周期 -----------------------------------------------------------
    def close(self) -> None:
        """关闭连接(重复 close 无害)。"""
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - 关库失败不向上抛
            pass

    def __enter__(self) -> "ScanJournal":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - 调试友好
        return f"ScanJournal({self._path!r})"
