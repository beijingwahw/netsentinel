# -*- coding: utf-8 -*-
"""批量举报批次状态存储(NetSentinel V6 · A113)。

断点续批的数据底座:每一个批量举报批次(A112 ``run_batch`` 的一次调用)
在开跑前先落一条 ``batches`` 记录,批内每个举报条目落一条 ``batch_items``
记录(初始 ``pending``);执行过程中条目状态随 ``mark`` 推进
(pending → running → submitted / failed / skipped / rate_limited),
全部状态与错误原因持久化在 SQLite(WAL)里——进程崩溃、频控挂起、
人工暂停之后都能凭 ``resume`` 找回"未竟条目"重新入列:

    from netsentinel.submit.batch_state import BatchState

    st = BatchState("data/batch_state.db")
    bid = st.new_batch(items, note="[组:example.com] 首批")
    summary = run_batch(items, cfg, ..., state=st.bound(bid))   # A112
    ...
    undone = st.resume(bid)      # pending/running/rate_limited → 下一轮 run_batch

与 A112 的对接(鸭子约定):``run_batch`` 以 ``state.mark(entry_id, status,
error="")`` 调用状态器(**不带 batch_id**);``BatchState.mark`` 需要
batch_id,因此提供 :meth:`BatchState.bound` 适配器——闭包绑定 batch_id 后
暴露 ``mark(entry_id, status, error="")``(及 ``summary()``),可把
``st.bound(batch_id)`` 直接作为 ``run_batch(..., state=...)`` 注入。

状态机(契约 §4 A113,固定六值):

============  =========================================================
pending       建批初始态,尚未开始执行
running       正在执行(进程中断即"未竟",resume 会重新回收)
submitted     提交成功(**终态**,不再回收)
failed        执行失败(**终态**,中文原因在 error 字段)
skipped       跳过(典型:干跑 / 未真实提交;**终态**)
rate_limited  频控/额度挂起(resume 重新入列续批)
============  =========================================================

连接策略与全仓存储模块一致:``PRAGMA journal_mode=WAL`` +
``busy_timeout=5000``(读写并发、跨进程锁等待);支持 ``with`` 上下文,
写操作即时 commit,可重复打开同一数据库。

安全红线(CONTRACTS-V6 §0):本模块只做**状态记账**,不触发任何提交
动作;所有真实提交仍由 A112 ``run_batch`` 逐条人工门完成。
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import now_iso

__all__ = [
    "BUSY_TIMEOUT_MS",
    "RESUME_STATUSES",
    "VALID_STATUSES",
    "BatchState",
    "StateAdapter",
]

logger = logging.getLogger(__name__)

#: SQLite 忙等上限(毫秒):与 review_queue / batch_review 等存储模块同一策略。
BUSY_TIMEOUT_MS: int = 5000

#: 合法条目状态(固定六值;mark 之外的一切取值 → 中文 ValueError)
VALID_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "submitted", "failed", "skipped", "rate_limited"}
)

#: 可续批状态:pending(未开始)/ running(中断未竟)/ rate_limited(频控挂起);
#: submitted / failed / skipped 为终态,resume 一律不回收。
RESUME_STATUSES: frozenset[str] = frozenset({"pending", "running", "rate_limited"})

#: summary/list_batches 中各状态计数的固定展示顺序(总数在前、明细表在后)。
_STATUS_ORDER: tuple[str, ...] = (
    "pending",
    "running",
    "submitted",
    "failed",
    "skipped",
    "rate_limited",
)

_SCHEMA_BATCHES_SQL = """
CREATE TABLE IF NOT EXISTS batches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT    NOT NULL,
    note       TEXT    NOT NULL DEFAULT ''
)
"""

_SCHEMA_ITEMS_SQL = """
CREATE TABLE IF NOT EXISTS batch_items (
    batch_id   INTEGER NOT NULL,
    entry_id           NOT NULL,
    group_name TEXT    NOT NULL DEFAULT '',
    status     TEXT    NOT NULL DEFAULT 'pending',
    error      TEXT    NOT NULL DEFAULT '',
    updated_at TEXT    NOT NULL,
    PRIMARY KEY (batch_id, entry_id)
)
"""

#: 复合索引(batch_id, status):resume/summary 的状态过滤走索引;
#: IF NOT EXISTS 保证旧库打开时补建。
_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_batch_items_status"
    " ON batch_items(batch_id, status)"
)

# 遥测指标名(只记名称与数字,不记条目内容)
_METRIC_NEW = "batch_state.new_batch"
_METRIC_MARK = "batch_state.mark"
_METRIC_RESUME = "batch_state.resume"


# ---------------------------------------------------------------------------
# 条目字段抽取(鸭子:对象属性 或 映射键)
# ---------------------------------------------------------------------------


def _entry_fields(item: Any, idx: int) -> tuple[Any, str]:
    """从待批量条目中抽取 ``(entry_id, group_name)``。

    鸭子约定(契约 §4 A113):items 元素带 ``entry_id`` / ``group_name``
    ——既支持对象属性(如 review_queue.Entry),也支持映射(dict,如
    A110 ``ready_entries`` / A112 items 的元素);缺 ``entry_id``、或
    items 本身不是列表 → 中文 ``ValueError``。
    """
    if isinstance(item, Mapping):
        entry_id = item.get("entry_id")
        group_name = item.get("group_name", "")
    else:
        entry_id = getattr(item, "entry_id", None)
        group_name = getattr(item, "group_name", "")
    if entry_id is None or entry_id == "":
        raise ValueError(
            f"第 {idx} 条待批量条目缺少 entry_id,无法登记批次状态"
        )
    group_name = "" if group_name is None else str(group_name)
    return entry_id, group_name


# ---------------------------------------------------------------------------
# 状态适配器(A112 鸭子接口)
# ---------------------------------------------------------------------------


class StateAdapter:
    """绑定单个批次的 A112 鸭子状态器。

    ``run_batch`` 以 ``state.mark(entry_id, status, error="")`` 调用
    (不带 batch_id);本适配器在构造时闭包绑定 batch_id,把调用原样
    转发给宿主 :class:`BatchState`:

        adapter = st.bound(batch_id)
        run_batch(items, cfg, ..., state=adapter)

    只暴露 ``mark`` 与 ``summary``(以及只读 ``batch_id``),不提供
    ``new_batch`` / ``resume`` 等整库操作——批次生命周期仍由宿主管理。
    """

    __slots__ = ("_state", "_batch_id")

    def __init__(self, state: BatchState, batch_id: int) -> None:
        self._state = state
        self._batch_id = batch_id

    @property
    def batch_id(self) -> int:
        """适配器绑定的批次号(只读)。"""
        return self._batch_id

    def mark(self, entry_id: Any, status: str, error: str = "") -> None:
        """转发到 ``BatchState.mark(batch_id, entry_id, status, error)``。"""
        self._state.mark(self._batch_id, entry_id, status, error=error)

    def summary(self) -> dict[str, Any]:
        """转发到 ``BatchState.summary(batch_id)``。"""
        return self._state.summary(self._batch_id)

    def __repr__(self) -> str:  # pragma: no cover - 调试友好
        return f"StateAdapter(batch_id={self._batch_id!r})"


# ---------------------------------------------------------------------------
# 主对象
# ---------------------------------------------------------------------------


class BatchState:
    """批量举报批次状态台账(SQLite · WAL)。

    - 表 ``batches(id, created_at, note)``:一个批次 = ``run_batch`` 的一次
      调用,note 通常携带组名前缀(如 ``[组:example.com]``)留痕;
    - 表 ``batch_items(batch_id, entry_id, group_name, status, error,
      updated_at)``:批内逐条状态,主键 ``(batch_id, entry_id)``——同一
      entry_id 可出现在不同批次(重跑/续批新开批次),同一批次内唯一;
    - ``entry_id`` 列不声明类型亲和(存什么查什么):复核队列的整数 id
      往返后仍是整数,字符串 id 往返后仍是字符串。

    典型断点续批闭环::

        st = BatchState(db)
        bid = st.new_batch(items, note="...")
        run_batch(items, cfg, ..., state=st.bound(bid))   # 中断/挂起退出
        undone = st.resume(bid)   # 未竟条目 → 重新 run_batch
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        # 与全仓一致:WAL + 忙等 5s(读写并发 / 跨进程锁等待)
        self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA_BATCHES_SQL)
        self._conn.execute(_SCHEMA_ITEMS_SQL)
        self._conn.execute(_INDEX_SQL)
        self._conn.commit()
        logger.debug("批次状态台账已就绪:%s", self.db_path)

    # ------------------------------------------------------------------
    # 建批
    # ------------------------------------------------------------------

    def new_batch(self, items: list[Any], note: str = "") -> int:
        """登记一个新批次并逐条插入 ``pending`` 条目,返回 ``batch_id``。

        :param items: 待批量条目列表;元素鸭子携带 ``entry_id`` /
            ``group_name``(对象属性或映射键均可,详见 :func:`_entry_fields`)。
            空列表合法(建空批次,total=0,resume 为空)。非列表(含
            str/bytes/dict)→ 中文 ``ValueError``。
        :param note: 批次备注(建议含组名前缀,如 ``[组:a.com] 首批``)。
        :return: 新批次号(自增,从 1 起)。
        :raises ValueError: items 非列表 / 某条缺 ``entry_id`` / 同批次内
            ``entry_id`` 重复 —— 任一失败整批回滚,不留半截批次。
        """
        if isinstance(items, (str, bytes)) or not isinstance(items, (list, tuple)):
            raise ValueError(
                f"items 必须是待批量条目列表(list),当前类型是 {type(items).__name__}"
            )
        ts = now_iso()
        note_text = "" if note is None else str(note)
        try:
            cur = self._conn.execute(
                "INSERT INTO batches (created_at, note) VALUES (?, ?)",
                (ts, note_text),
            )
            batch_id = int(cur.lastrowid)
            for idx, item in enumerate(items, start=1):
                entry_id, group_name = _entry_fields(item, idx)
                self._conn.execute(
                    "INSERT INTO batch_items"
                    " (batch_id, entry_id, group_name, status, error, updated_at)"
                    " VALUES (?, ?, ?, 'pending', '', ?)",
                    (batch_id, entry_id, group_name, ts),
                )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise ValueError(
                f"批次登记失败:同一批次内 entry_id 重复(sqlite:{exc});"
                "一个批次内每个举报条目只能出现一次"
            ) from exc
        except ValueError:
            self._conn.rollback()
            raise
        telemetry.inc(_METRIC_NEW)
        logger.info(
            "新批次 #%d:%d 条(pending)note=%s", batch_id, len(items), note_text
        )
        return batch_id

    # ------------------------------------------------------------------
    # 标记
    # ------------------------------------------------------------------

    def mark(
        self,
        batch_id: int,
        entry_id: Any,
        status: str,
        error: str = "",
    ) -> None:
        """推进某批次内某条目的状态(全量覆盖 status/error 并刷新 updated_at)。

        :param batch_id: 批次号。
        :param entry_id: 条目 id(建批时的原样值)。
        :param status: 新状态,必须 ∈ VALID_STATUSES;非法取值 → 中文
            ``ValueError``(消息列出全部合法值)。
        :param error: 中文失败原因(仅失败/挂起态需要;再次 mark 不带
            error 会清空旧原因——"最新状态即真相")。
        :raises ValueError: 批次不存在 / 条目不在该批次 / 状态非法。
        """
        if not isinstance(status, str) or status not in VALID_STATUSES:
            raise ValueError(
                f"非法条目状态:{status!r}(合法值:"
                f"{'/'.join(_STATUS_ORDER)})"
            )
        batch_id = self._require_batch(batch_id)
        updated = self._conn.execute(
            "UPDATE batch_items SET status = ?, error = ?, updated_at = ?"
            " WHERE batch_id = ? AND entry_id = ?",
            (status, "" if error is None else str(error), now_iso(), batch_id, entry_id),
        ).rowcount
        if not updated:
            raise ValueError(
                f"条目 entry_id={entry_id!r} 不在批次 #{batch_id} 中,"
                "无法标记(请检查 new_batch 登记的条目清单)"
            )
        self._conn.commit()
        telemetry.inc(_METRIC_MARK)
        logger.debug("批次 #%d 条目 %s → %s", batch_id, entry_id, status)

    # ------------------------------------------------------------------
    # 续批
    # ------------------------------------------------------------------

    def resume(self, batch_id: int) -> list[dict[str, Any]]:
        """取该批次全部**未竟**条目(断点续批的输入)。

        回收规则(契约 §4 A113):

        - ``pending``(未开始)与 ``rate_limited``(频控/额度挂起)重新入列;
        - ``running`` 视为进程中断的未竟条目,**重新入列**;
        - ``submitted`` / ``failed`` / ``skipped`` 为终态,**不回收**。

        :param batch_id: 批次号;不存在 → 中文 ``ValueError``。
        :return: 未竟条目列表(按 entry_id 升序),每条
            ``{entry_id, group_name, status, error, updated_at}``;
            补上 ``portal`` 字段后即可作为 A112 ``run_batch`` 的 items
            重新入列。全为终态时返回 ``[]``(批次已完成,无需续批)。
        """
        batch_id = self._require_batch(batch_id)
        placeholders = ",".join("?" for _ in RESUME_STATUSES)
        rows = self._conn.execute(
            "SELECT entry_id, group_name, status, error, updated_at"
            f" FROM batch_items WHERE batch_id = ? AND status IN ({placeholders})"
            " ORDER BY entry_id ASC",
            (batch_id, *sorted(RESUME_STATUSES)),
        ).fetchall()
        undone = [_row_to_dict(row) for row in rows]
        telemetry.inc(_METRIC_RESUME, len(undone))
        if undone:
            logger.info("批次 #%d 续批回收 %d 条未竟条目", batch_id, len(undone))
        return undone

    # ------------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------------

    def summary(self, batch_id: int) -> dict[str, Any]:
        """批次汇总:总数、各状态计数与逐条明细。

        :param batch_id: 批次号;不存在 → 中文 ``ValueError``。
        :return: ``{"total": n, "pending": n, "running": n, "submitted": n,
            "failed": n, "skipped": n, "rate_limited": n, "items": [...]}``。
            六个状态键**恒存在**(未出现的状态计 0,便于 A114 报告直接渲染);
            ``items`` 按entry_id 升序,每条含
            ``{entry_id, group_name, status, error, updated_at}``。
        """
        batch_id = self._require_batch(batch_id)
        counts = dict.fromkeys(_STATUS_ORDER, 0)
        for row in self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM batch_items"
            " WHERE batch_id = ? GROUP BY status",
            (batch_id,),
        ):
            if row["status"] in counts:  # 防御:忽略库中被手改出的非法状态
                counts[row["status"]] = int(row["n"])
        items = [
            _row_to_dict(row)
            for row in self._conn.execute(
                "SELECT entry_id, group_name, status, error, updated_at"
                " FROM batch_items WHERE batch_id = ? ORDER BY entry_id ASC",
                (batch_id,),
            )
        ]
        result: dict[str, Any] = {"total": len(items)}
        result.update(counts)
        result["items"] = items
        return result

    def list_batches(self) -> list[dict[str, Any]]:
        """全部批次简版清单(最新在前)。

        :return: 每批 ``{"id", "created_at", "note", "total",
            "pending", "running", "submitted", "failed", "skipped",
            "rate_limited"}``,按 id 降序(最新批次排首行,便于 TUI/报告
            直接展示"最近一次批量")。
        """
        batches = self._conn.execute(
            "SELECT id, created_at, note FROM batches ORDER BY id DESC"
        ).fetchall()
        counts_by_batch: dict[int, dict[str, int]] = {}
        for row in self._conn.execute(
            "SELECT batch_id, status, COUNT(*) AS n FROM batch_items"
            " GROUP BY batch_id, status"
        ):
            bid = int(row["batch_id"])
            per = counts_by_batch.setdefault(bid, dict.fromkeys(_STATUS_ORDER, 0))
            if row["status"] in per:  # 防御:忽略库中被手改出的非法状态
                per[row["status"]] = int(row["n"])
        result: list[dict[str, Any]] = []
        for row in batches:
            bid = int(row["id"])
            per = counts_by_batch.get(bid, dict.fromkeys(_STATUS_ORDER, 0))
            item: dict[str, Any] = {
                "id": bid,
                "created_at": row["created_at"] or "",
                "note": row["note"] or "",
                "total": sum(per.values()),
            }
            item.update(per)
            result.append(item)
        return result

    # ------------------------------------------------------------------
    # A112 适配器
    # ------------------------------------------------------------------

    def bound(self, batch_id: int) -> StateAdapter:
        """返回绑定该批次的 A112 鸭子状态器(闭包绑定 batch_id)。

        适配器暴露 ``mark(entry_id, status, error="")`` 与 ``summary()``,
        可直接注入 ``run_batch(..., state=st.bound(batch_id))``;
        批次不存在 → 中文 ``ValueError``(绑定即校验,错误尽早暴露)。
        """
        batch_id = self._require_batch(batch_id)
        return StateAdapter(self, batch_id)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _require_batch(self, batch_id: Any) -> int:
        """校验批次存在并归一为 int;不存在抛中文 ValueError。"""
        try:
            bid = int(batch_id)
        except (TypeError, ValueError):
            raise ValueError(
                f"批次号非法:{batch_id!r}(应为 new_batch 返回的整数 id)"
            ) from None
        row = self._conn.execute(
            "SELECT 1 FROM batches WHERE id = ?", (bid,)
        ).fetchone()
        if row is None:
            raise ValueError(f"批次 #{bid} 不存在(可能已被清理或批次号写错)")
        return bid

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def close(self) -> None:
        """关闭底层连接(幂等容忍;WAL 数据已随每次 commit 落盘)。"""
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover - 关闭异常无需上抛
            logger.debug("关闭批次状态连接时出现异常", exc_info=True)

    def __enter__(self) -> BatchState:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """batch_items 行 → 明细字典(键序固定,供 resume/summary/报告共用)。"""
    return {
        "entry_id": row["entry_id"],
        "group_name": row["group_name"] or "",
        "status": row["status"] or "",
        "error": row["error"] or "",
        "updated_at": row["updated_at"] or "",
    }
