"""批量复核与声明 —— 红线 24/25 的技术收口(NetSentinel · A110)。

V6 把"单站办案"升级为"批量案件流水线",但两条红线不允许随之松动:

- **红线 24(逐条人工门)**:批量只是顺序编排,本模块只产出"待批量清单"
  (``ready_entries``),不触碰提交流程;真正的验证码输入与最终确认仍由
  人工在执行器 HUMAN_GATE 完成。
- **红线 25(批量确认声明留痕)**:案件组进入批量队列前,运营者必须逐组
  完成证据核验并显式声明(声明文本入审计日志,含组名/条数/审核人);
  **无声明的组不得提交** —— ``ready_entries`` 在 ``batch_require_attestation=True``
  (默认)时只放行已声明组的条目,未声明组整组排除并计数留痕。

核心对象:

- :class:`Attestation`:一份批量确认声明(组名/条数/审核人/时间戳/声明文本)。
- :class:`BatchReview`:声明台账(SQLite ``attestations`` 表,组名主键,
  重复声明覆盖更新并刷新时间戳);``attest()`` 强制校验审核人非空、声明文本
  含"人工核实"四字,并可选写 JSONL 审计日志(惰性 ``JsonlAuditLogger``,
  ``audit_path`` 缺省时不写文件、只落 sqlite)。
- :func:`ready_entries`:从复核队列取"已确认(approved)"条目,按组名做声明
  门控,输出待批量清单(每条 ``{entry_id, group_name, site_url,
  evidence_zip, portal}``),并按 ``cfg.batch_max_items`` 截断(截断会以
  WARNING 日志 + telemetry 计数标注,超出部分留待下一批)。

组名解析规则:优先取条目 note 中的 ``[组:{名}]`` 标记(A108 归组入列时写入);
无组标记时回退为站点 canonical 归一名(惰性复用 A103,未安装时退化为站点
host 小写)。用法示例::

    from netsentinel.contracts import Config
    from netsentinel.decision.batch_review import BatchReview, ready_entries

    review = BatchReview("data/review_queue.db", audit_path="data/audit.jsonl")
    review.attest("example.com", items=3, reviewer="张三",
                  text="我已逐站人工核实全部证据,同意批量举报")
    items = ready_entries(Config())          # 未声明组自动排除
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from netsentinel import telemetry
from netsentinel.contracts import Config, now_iso
from netsentinel.logging_util import JsonlAuditLogger

if TYPE_CHECKING:  # 仅类型标注用,运行时惰性导入避免硬依赖
    from netsentinel.decision.review_queue import Entry

logger = logging.getLogger(__name__)

#: 默认举报门户(中央网信办 12377)。
DEFAULT_PORTAL: str = "12377"

#: SQLite 忙等上限(毫秒):与 review_queue / vlm_cache 等存储模块同一策略。
BUSY_TIMEOUT_MS: int = 5000

#: 声明文本必须包含的关键字:证据"人工核实"是红线 25 的核心动作。
REQUIRED_PHRASE: str = "人工核实"

#: note 中的组名标记 ``[组:{名}]``(兼容全角冒号与首尾空白)。
GROUP_MARKER_RE = re.compile(r"\[组[:：]\s*([^\]]+?)\s*\]")

__all__ = [
    "Attestation",
    "BatchReview",
    "DEFAULT_PORTAL",
    "GROUP_MARKER_RE",
    "REQUIRED_PHRASE",
    "ready_entries",
    "resolve_group_name",
]

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS attestations (
    group_name TEXT    PRIMARY KEY,
    items      INTEGER NOT NULL DEFAULT 0,
    reviewer   TEXT    NOT NULL DEFAULT '',
    text       TEXT    NOT NULL DEFAULT '',
    ts         TEXT    NOT NULL DEFAULT ''
)
"""


@dataclass
class Attestation:
    """一份批量确认声明(字段与 attestations 表一一对应)。"""

    group_name: str
    items: int
    reviewer: str
    ts: str
    text: str


class BatchReview:
    """批量确认声明台账(SQLite)。

    - 表 ``attestations(group_name PK, items, reviewer, text, ts)``:
      一个案件组只有一份有效声明,重复声明覆盖更新并刷新时间戳。
    - ``attest()`` 是声明的唯一入口:审核人空白/缺失、声明文本缺少
      "人工核实"都会抛 ``ValueError``(中文)—— 把红线 25 变成代码约束。
    - 可选 ``audit_path``:提供时惰性创建 ``JsonlAuditLogger`` 把每次声明
      追加进 JSONL 审计(含组名/条数/审核人/声明全文/是否覆盖更新);
      缺省不写任何文件,只落 sqlite。
    - 连接策略与 ReviewQueue 一致:WAL + busy_timeout,可 ``with`` 使用。
    """

    def __init__(self, db_path: str | Path, audit_path: str | Path | None = None) -> None:
        self.db_path = str(db_path)
        self._audit_path = str(audit_path) if audit_path else ""
        self._audit: JsonlAuditLogger | None = None  # 惰性:首次声明时才创建
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA_SQL)
        self._conn.commit()
        logger.debug("批量声明台账已就绪:%s(审计:%s)", self.db_path, self._audit_path or "关闭")

    # ------------------------------------------------------------------
    # 声明(红线 25 收口)
    # ------------------------------------------------------------------

    def attest(
        self,
        group_name: str,
        items: int,
        reviewer: str,
        text: str,
    ) -> Attestation:
        """为案件组写入一份批量确认声明,返回落库后的 :class:`Attestation`。

        校验(任一不满足抛 ``ValueError`` 中文消息):

        - 组名非空(声明必须能留痕到具体案件组);
        - 审核人非空白(红线 25:声明含审核人);
        - 声明文本包含"人工核实"四字(逐组完成证据核验的显式自述);
        - 条数 items 为非负整数。

        同组重复声明 = 覆盖更新(items/reviewer/text 全量替换,ts 刷新),
        并在审计记录里以 ``overwrite=true`` 区分首次声明与覆盖更新。
        """
        name = (group_name or "").strip()
        if not name:
            raise ValueError("声明缺少组名(group_name),无法留痕到案件组")
        if reviewer is None or not str(reviewer).strip():
            raise ValueError("声明缺少审核人(reviewer):每份批量声明必须留痕审核人")
        reviewer_cn = str(reviewer).strip()
        text_cn = "" if text is None else str(text)
        if REQUIRED_PHRASE not in text_cn:
            raise ValueError(
                "声明缺少『人工核实』,批量举报前必须逐组完成证据核验"
            )
        try:
            n_items = int(items)
        except (TypeError, ValueError):
            raise ValueError(f"声明条数 items 应为非负整数,当前为 {items!r}") from None
        if n_items < 0:
            raise ValueError(f"声明条数 items 不能为负,当前为 {n_items}")
        ts = now_iso()
        existing = self._conn.execute(
            "SELECT 1 FROM attestations WHERE group_name = ?", (name,)
        ).fetchone()
        self._conn.execute(
            """
            INSERT INTO attestations (group_name, items, reviewer, text, ts)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(group_name) DO UPDATE SET
                items    = excluded.items,
                reviewer = excluded.reviewer,
                text     = excluded.text,
                ts       = excluded.ts
            """,
            (name, n_items, reviewer_cn, text_cn, ts),
        )
        self._conn.commit()
        att = Attestation(
            group_name=name, items=n_items, reviewer=reviewer_cn, ts=ts, text=text_cn
        )
        self._audit_attestation(att, overwrite=existing is not None)
        telemetry.inc("batch_review.attest")
        action = "覆盖更新" if existing is not None else "首次声明"
        logger.info(
            "批量声明已留痕(%s):组=%s 条数=%d 审核人=%s", action, name, n_items, reviewer_cn
        )
        return att

    def is_attested(self, group_name: str) -> bool:
        """该案件组是否已有有效声明(空组名恒为 False)。"""
        if not group_name or not group_name.strip():
            return False
        row = self._conn.execute(
            "SELECT 1 FROM attestations WHERE group_name = ?", (group_name.strip(),)
        ).fetchone()
        return row is not None

    def list_attestations(self) -> list[Attestation]:
        """列出全部声明,按组名升序(确定性输出)。"""
        rows = self._conn.execute(
            "SELECT group_name, items, reviewer, text, ts FROM attestations"
            " ORDER BY group_name ASC"
        ).fetchall()
        return [
            Attestation(
                group_name=row["group_name"] or "",
                items=int(row["items"] or 0),
                reviewer=row["reviewer"] or "",
                ts=row["ts"] or "",
                text=row["text"] or "",
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # 审计(可选文件,惰性)
    # ------------------------------------------------------------------

    def _audit_attestation(self, att: Attestation, *, overwrite: bool) -> None:
        """把一次声明写入 JSONL 审计;未配置 audit_path 时为无操作。"""
        if not self._audit_path:
            return
        if self._audit is None:  # 惰性创建:不开 audit_path 就零文件副作用
            self._audit = JsonlAuditLogger(self._audit_path)
        self._audit.log_event(
            "batch_attest",
            group_name=att.group_name,
            items=att.items,
            reviewer=att.reviewer,
            text=att.text,
            overwrite=overwrite,
        )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def close(self) -> None:
        """关闭底层连接并 flush 审计缓冲(幂等容忍)。"""
        if self._audit is not None:
            try:
                self._audit.close()
            except OSError:  # pragma: no cover - 审计关闭异常不阻断
                logger.warning("关闭批量声明审计日志器时出现异常", exc_info=True)
            self._audit = None
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover - 关闭异常无需上抛
            logger.debug("关闭批量声明台账连接时出现异常", exc_info=True)

    def __enter__(self) -> BatchReview:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# 组名解析与批量就绪清单
# ---------------------------------------------------------------------------

def _host_of(url: str) -> str:
    """从 URL 提取 host(小写);无 scheme 时按 https 补齐再解析;失败回空串。"""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        host = urlparse(raw if "://" in raw else f"https://{raw}").hostname
    except ValueError:
        return ""
    return (host or "").lower()


def fallback_group_name(site_url: str) -> str:
    """无组标记条目的组名回退:canonical 归一名(A103,惰性)→ 站点 host → 原 URL。

    A103 ``intel/canonical.py`` 为并行开发的兄弟模块,此处惰性导入且容忍其
    缺席:导入失败或归一失败时退化为站点 host 小写,保证本模块独立可测。
    """
    try:
        from netsentinel.intel.canonical import canonical_name  # 惰性,兄弟模块只读
    except Exception:
        canonical_name = None  # type: ignore[assignment]
    if canonical_name is not None:
        try:
            name = canonical_name(site_url)
        except Exception:
            name = ""
        if name:
            return name
    host = _host_of(site_url)
    return host or (site_url or "")


def resolve_group_name(note: str, site_url: str) -> str:
    """解析条目所属案件组名。

    优先取 note 中的 ``[组:{名}]`` 标记(A108 归组入列时写入的前缀);
    没有组标记时回退 :func:`fallback_group_name`(canonical 或站点 host)。
    """
    match = GROUP_MARKER_RE.search(note or "")
    if match:
        return match.group(1).strip()
    return fallback_group_name(site_url)


def ready_entries(
    cfg: Config,
    *,
    queue=None,
    review: BatchReview | None = None,
    portal: str = DEFAULT_PORTAL,
) -> list[dict]:
    """产出"待批量举报"就绪清单(红线 25 门控 + 红线 24 只列不交)。

    - ``queue`` 缺省时惰性 ``ReviewQueue(cfg.db_path)``;``review`` 缺省时
      惰性 :class:`BatchReview`(cfg.db_path)(与队列同库,entries 与
      attestations 两表共存);两者均可注入测试替身。
    - 只取 ``approved``(已确认)条目,按队列 id 升序。
    - ``cfg.batch_require_attestation=True``(默认)时逐条解析组名
      (note 的 ``[组:{名}]``,无标记回退 canonical/host),仅已声明组的
      条目入选;未声明条目整组排除并计数(WARNING 日志 + telemetry
      ``batch_review.skipped_unattested``,标注涉及组名)。为 ``False``
      时(仅当 cfg 明示)跳过声明要求,全部 approved 入选。
    - 每条输出固定五键:``{entry_id, group_name, site_url, evidence_zip,
      portal}``;``portal`` 缺省 "12377",空白回退缺省。
    - 超过 ``cfg.batch_max_items`` 时截断保留前 N 条,并以 WARNING 日志 +
      telemetry ``batch_review.truncated`` 标注(其余条目留待下一批)。

    本函数不执行任何提交动作;提交仍由 A112 ``run_batch`` 逐条走人工门。
    """
    require = bool(getattr(cfg, "batch_require_attestation", True))
    try:
        max_items = int(getattr(cfg, "batch_max_items", 20))
    except (TypeError, ValueError):
        max_items = 20
    portal_id = str(portal).strip() or DEFAULT_PORTAL

    own_queue = queue is None
    own_review = review is None
    if own_queue:  # 惰性构造,依赖注入优先
        from netsentinel.decision.review_queue import ReviewQueue

        queue = ReviewQueue(getattr(cfg, "db_path", "data/review_queue.db"))
    if own_review:
        review = BatchReview(getattr(cfg, "db_path", "data/review_queue.db"))
    try:
        approved: list[Entry] = queue.list("approved")
        selected: list[dict] = []
        skipped = 0
        skipped_groups: list[str] = []
        for entry in approved:
            group = resolve_group_name(
                getattr(entry, "note", "") or "",
                getattr(entry, "site_url", "") or "",
            )
            if require and not review.is_attested(group):
                skipped += 1
                if group not in skipped_groups:
                    skipped_groups.append(group)
                continue
            selected.append(
                {
                    "entry_id": int(getattr(entry, "id", 0)),
                    "group_name": group,
                    "site_url": getattr(entry, "site_url", "") or "",
                    "evidence_zip": getattr(entry, "evidence_zip", "") or "",
                    "portal": portal_id,
                }
            )
        if skipped:
            telemetry.inc("batch_review.skipped_unattested", skipped)
            logger.warning(
                "批量就绪筛选:%d 条已确认条目因所在组未完成人工核实声明被排除"
                "(未声明组:%s)—— 红线 25:无声明的组不得提交",
                skipped,
                "、".join(skipped_groups) or "(未知组)",
            )
        total = len(selected)
        if total > max_items:
            selected = selected[:max_items]
            telemetry.inc("batch_review.truncated")
            logger.warning(
                "批量就绪清单 %d 条超过单批上限 batch_max_items=%d,"
                "已截断保留前 %d 条,其余 %d 条留待下一批",
                total,
                max_items,
                max_items,
                total - max_items,
            )
        return selected
    finally:
        # 只关闭本函数自己构造的缺省实例,注入对象归调用方管理。
        if own_queue:
            close_q = getattr(queue, "close", None)
            if callable(close_q):
                close_q()
        if own_review and review is not None:
            review.close()
