"""NetSentinel(净网哨兵)日志与审计模块。[A01]

- ``setup_logging``:配置 root logger —— 控制台(简格式)+ 文件 ``cfg.log_path``
  (含时间/级别/模块,utf-8,自动建目录)。重复调用只重建本模块自有 handler,
  不产生重复输出,也不影响 pytest 等外部已挂的 handler。V5:重建过程加锁,
  多线程并发调用安全;日志文件不可写(磁盘满/被占用/路径非法)时降级为仅
  控制台输出并 WARNING,绝不抛出;每次调用计入 ``telemetry.inc("log.setup")``。
- ``JsonlAuditLogger``:按行追加 JSON 审计记录(自动带 ``ts``/``event``,
  自动建父目录,utf-8)。V5:支持可选批量缓冲(``buffer_size``),高频审计
  场景可减少打开文件次数;默认 ``buffer_size=0`` 即写透(write-through),
  行为与升级前完全一致;事件计数 ``telemetry.inc("audit.event")``。
- ``JsonlAuditLogger`` 的 Merkle 审计透明日志接入(可选,opt-in):
  构造时注入 ``merkle_key``(非空 bytes,HMAC-SHA256 签名密钥,调用方自行
  从 :mod:`netsentinel.security.keys` / ``bundle_sign`` 体系获取)后,每条
  事件同步进入 append-only Merkle 树(与写文件同一把锁,叶序与行序恒一致),
  行内新增 ``"merkle_leaf"`` 字段;每 ``merkle_interval``(默认 128)条事件
  追加一行 ``event=merkle_checkpoint`` 封根记录(含签名根 / 叶数 / 树高)。
  另有 :meth:`JsonlAuditLogger.seal_now` 手动封根:高敏操作后立即封根
  锁定尾部盲区,不必等 interval 计数(A193 交付报告点名的需求)。
  Merkle 侧任何异常都安全失效:审计日志本体绝不丢,降级时写一行
  ``event=merkle_degraded`` 标记并停用树(计数 ``audit.merkle_degraded``),
  封根成功另计 ``audit.merkle_checkpoint``。默认不注入密钥 → 行为与升级前
  完全一致(零 ``merkle_leaf`` 字段、零 checkpoint 行)。

用法示例::

    from netsentinel.contracts import Config
    from netsentinel.logging_util import JsonlAuditLogger, setup_logging

    setup_logging(Config(), verbose=False)      # 控制台 + data/logs/netsentinel.log

    audit = JsonlAuditLogger("data/audit.jsonl")
    audit.log_event("human_confirmed", verdict="nsfw")

    # 高频审计可选缓冲:攒满 buffer_size 条一次性落盘,退出前自动 flush
    with JsonlAuditLogger("data/audit.jsonl", buffer_size=64) as buffered:
        buffered.log_event("scan_started", site_url="http://example.com")

    # 审计透明日志(可选):注入 HMAC 密钥即启用,每 128 条封根签名
    audited = JsonlAuditLogger(
        "data/audit.jsonl", merkle_key=b"...", merkle_interval=128
    )
    audited.log_event("human_confirmed", verdict="nsfw")  # 行内自动带 merkle_leaf
    proof = audited.merkle_tree.include_proof(0)           # 包含性证明
"""
from __future__ import annotations

import json
import logging
import pathlib
import threading
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config, now_iso
from netsentinel.security.merkle import (
    DEFAULT_CHECKPOINT_INTERVAL,
    MerkleTree,
)

__all__ = ["JsonlAuditLogger", "setup_logging"]

#: 打在自有 handler 上的标记:重复 setup_logging 时据此清理旧 handler
_HANDLER_MARK = "_netsentinel_handler"

_CONSOLE_FORMAT = "%(levelname)s %(name)s: %(message)s"
_FILE_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(module)s: %(message)s"

#: setup_logging 的 handler 重建锁:并发调用时摘旧/挂新原子化,防止 handler 重复或丢失
_SETUP_LOCK = threading.Lock()


def _own_handlers() -> list[logging.Handler]:
    """返回 setup_logging 挂到 root 上的自有 handler(不含外部 handler)。"""
    root = logging.getLogger()
    return [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]


def _drop_own_handlers() -> None:
    """摘除并关闭自有 handler,保证 setup_logging 可重复调用。"""
    root = logging.getLogger()
    for handler in _own_handlers():
        root.removeHandler(handler)
        handler.close()


def setup_logging(cfg: Config, verbose: bool = False) -> None:
    """初始化全局日志:控制台 + ``cfg.log_path`` 文件;可安全重复调用(线程安全)。

    ``verbose=True`` 时 root 级别为 DEBUG,否则 INFO。日志文件不可写(目录无法
    创建/被占用/磁盘满等 ``OSError``)时降级为仅控制台输出并记录 WARNING,
    绝不向上抛出;每次调用计入 ``telemetry.inc("log.setup")``,降级时另计
    ``telemetry.inc("log.setup.degraded")``。
    """
    telemetry.inc("log.setup")
    degraded: tuple[pathlib.Path, OSError] | None = None
    with _SETUP_LOCK:
        root = logging.getLogger()
        _drop_own_handlers()
        root.setLevel(logging.DEBUG if verbose else logging.INFO)

        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
        setattr(console, _HANDLER_MARK, True)
        root.addHandler(console)

        log_path = pathlib.Path(cfg.log_path)
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
        except OSError as exc:  # 磁盘满/被占用/路径非法等:降级,不抛出
            degraded = (log_path, exc)
            file_handler = None
        if file_handler is not None:
            file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
            setattr(file_handler, _HANDLER_MARK, True)
            root.addHandler(file_handler)

    if degraded is not None:
        failed_path, exc = degraded
        telemetry.inc("log.setup.degraded")
        # 锁外发告警,避免持锁期间再进入日志子系统
        logging.getLogger(__name__).warning(
            "日志文件不可写,已降级为仅控制台输出:%s(%s)", failed_path, exc
        )


class JsonlAuditLogger:
    """JSONL 审计日志器:每条事件一行 JSON,自动附 ``ts`` 与 ``event``。

    ``buffer_size=0``(默认)为写透模式:每条事件立即落盘,行为与升级前一致。
    ``buffer_size>0`` 为批量缓冲模式:攒满 ``buffer_size`` 条一次性追加写出,
    适合高频审计以减少打开文件次数;缓冲模式下请务必以 ``with`` 语句使用,
    或在进程结束前显式调用 :meth:`flush` / :meth:`close`,否则未满一批的
    记录会随对象丢弃。审计写入失败仍按原行为向上抛出(审计证据不可静默丢失)。

    Merkle 审计透明日志(可选,注入 ``merkle_key`` 启用):

    - 每条事件在写行前同步进入 :class:`~netsentinel.security.merkle.MerkleTree`
      (与文件写入同一把可重入锁保护,线程安全,叶序与落盘行序恒一致),
      行内新增 ``"merkle_leaf"``(该事件规范序列化的 SHA-256,hex);
    - 每凑满 ``merkle_interval``(默认 128)条事件,紧随其后追加一行
      ``event=merkle_checkpoint`` 记录(含 HMAC-SHA256 签名根 / 叶数 / 树高);
      checkpoint 行本身不入树(它是树状态的元数据,不是被审计事件);
    - :meth:`seal_now` 手动封根:高敏操作后立即封根锁定尾部盲区,不必
      等待 interval 计数(A193 交付报告点名的需求;与 log_event 同锁,
      并发竞争安全);
    - Merkle 侧任何异常都**安全失效**:降级为纯 JSONL 审计(事件本体绝不丢),
      写一行 ``event=merkle_degraded`` 标记且此后停用树,公开属性
      ``merkle_degraded`` 置 True,计数 ``telemetry.inc("audit.merkle_degraded")``;
    - 默认(不注入密钥)不建树、不加字段、不写 checkpoint,行为与升级前
      完全一致;公开属性 ``merkle_tree`` 恒可读(None = 未启用)。
    """

    def __init__(
        self,
        path: str,
        *,
        buffer_size: int = 0,
        merkle_key: bytes | bytearray | None = None,
        merkle_interval: int = DEFAULT_CHECKPOINT_INTERVAL,
    ) -> None:
        if buffer_size < 0:
            raise ValueError(f"buffer_size 应为非负整数,当前为 {buffer_size!r}")
        if not isinstance(merkle_interval, int) or isinstance(merkle_interval, bool):
            raise TypeError(
                f"merkle_interval 应为整数,当前为 {type(merkle_interval).__name__}"
            )
        if merkle_interval < 1:
            raise ValueError(f"merkle_interval 应为正整数,当前为 {merkle_interval!r}")
        if merkle_key is not None and (
            not isinstance(merkle_key, (bytes, bytearray)) or len(merkle_key) == 0
        ):
            raise ValueError("merkle_key 需为非空 bytes/bytearray(由调用方注入)")
        self.path = pathlib.Path(path)
        self.buffer_size = int(buffer_size)
        self.merkle_interval = int(merkle_interval)
        self.merkle_tree: MerkleTree | None = (
            MerkleTree(
                signing_key=bytes(merkle_key), checkpoint_interval=self.merkle_interval
            )
            if merkle_key is not None
            else None
        )
        #: Merkle 是否已安全失效(降级为纯 JSONL);只读语义,由本类维护
        self.merkle_degraded = False
        self._degrade_reason = ""
        self._degrade_marker_written = False
        self._buffer: list[str] = []
        #: 事件行/缓冲/Merkle 追加/封根共用的可重入锁(flush 复用同锁无死锁)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log_event(self, event: str, **fields: Any) -> None:
        """追加一条审计事件;``fields`` 展开到记录顶层,``ts``/``event`` 恒定保留。

        非法 JSON 值(如 Path、dataclass)以 ``str()`` 兜底,保证审计永不因
        序列化失败而中断。文件以 utf-8 追加写入;写透模式逐条落盘,缓冲模式
        攒满 ``buffer_size`` 条批量落盘。事件计数 ``telemetry.inc("audit.event")``。

        启用 Merkle(``merkle_key``)时:锁内先入树再写行(行内带
        ``merkle_leaf``),凑满间隔紧随写一行 ``merkle_checkpoint``;Merkle
        异常安全失效——首条降级前写一行 ``merkle_degraded`` 标记,事件本体
        照常落盘且后续事件不再尝试入树。
        """
        record: dict[str, Any] = {**fields, "event": event, "ts": now_iso()}
        telemetry.inc("audit.event")
        with self._lock:
            leaf_hex = self._merkle_append(record)
            if leaf_hex is not None:
                record["merkle_leaf"] = leaf_hex
            line = json.dumps(record, ensure_ascii=False, default=str)
            lines = [line]
            marker = self._degrade_marker_line_locked()
            if marker is not None:
                lines.insert(0, marker)  # 标记先于首条无叶事件出现
            checkpoint = self._checkpoint_line_locked()
            if checkpoint is not None:
                lines.append(checkpoint)
            if self.buffer_size <= 0:  # 写透(默认):行为与升级前一致
                self._write_lines(lines)
                return
            self._buffer.extend(lines)
            if len(self._buffer) >= self.buffer_size:
                self._write_lines(self._buffer)
                self._buffer.clear()

    def _merkle_append(self, record: dict[str, Any]) -> str | None:
        """锁内调用:事件入树返回叶哈希 hex;树未启用/已降级/异常返回 None。"""
        if self.merkle_tree is None or self.merkle_degraded:
            return None
        try:
            return self.merkle_tree.append(record)
        except Exception as exc:  # noqa: BLE001 - Merkle 失效绝不拖垮审计本体
            self._degrade(exc)
            return None

    def _degrade(self, exc: BaseException) -> None:
        """安全失效:置降级标记并计数告警(锁内调用;绝不向上抛)。"""
        self.merkle_degraded = True
        self._degrade_reason = f"{type(exc).__name__}: {exc}"
        telemetry.inc("audit.merkle_degraded")
        logging.getLogger(__name__).warning(
            "Merkle 审计树已失效,审计日志降级为纯 JSONL:%s", self._degrade_reason
        )

    def _degrade_marker_line_locked(self) -> str | None:
        """锁内调用:首次降级时产出一次性 ``merkle_degraded`` 标记行。"""
        if not self.merkle_degraded or self._degrade_marker_written:
            return None
        self._degrade_marker_written = True
        marker = {
            "event": "merkle_degraded",
            "ts": now_iso(),
            "reason": self._degrade_reason,
        }
        return json.dumps(marker, ensure_ascii=False, default=str)

    def _checkpoint_line_locked(self) -> str | None:
        """锁内调用:凑满间隔时封根,产出 ``merkle_checkpoint`` 行;否则 None。"""
        tree = self.merkle_tree
        if (
            tree is None
            or self.merkle_degraded
            or tree.leaf_count <= 0
            or tree.leaf_count % self.merkle_interval != 0
        ):
            return None
        try:
            cp = tree.checkpoint()
        except Exception as exc:  # noqa: BLE001 - 封根失败同样安全降级
            self._degrade(exc)
            return None
        telemetry.inc("audit.merkle_checkpoint")
        record: dict[str, Any] = {"event": "merkle_checkpoint", "ts": now_iso(), **cp}
        return json.dumps(record, ensure_ascii=False, default=str)

    def _write_lines(self, lines: list[str]) -> None:
        """一批记录一次性追加写出(单次打开文件;自动重建可能被清理的父目录)。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)  # 目录可能被外部清理
        with self.path.open("a", encoding="utf-8") as fh:
            fh.writelines(f"{line}\n" for line in lines)

    def seal_now(self) -> dict[str, Any] | None:
        """手动封根:立即产出并落盘一行 ``merkle_checkpoint``,不必等 interval。

        适用场景(A193 交付报告点名的需求):interval 封根只覆盖到上一次
        计数点,卷尾新增的叶在下一 checkpoint 前处于**未封状态**(尾部盲区);
        高敏操作——人工确认提交、密钥轮换、批量举报放行等——完成后立即
        调用本方法,把当时的全部叶一次性锁定进签名根,事后抵赖/篡改即刻
        可检(:mod:`netsentinel.ops.audit_verify` 可整卷离线复核)。

        行为口径:

        - 与 :meth:`log_event` / :meth:`flush` 共用同一把可重入锁,并发
          竞争安全(封根与写行互斥,checkpoint 行恒出现在当时全部事件行
          之后,文件行序与叶序保持一致);
        - Merkle 未启用(未注入 ``merkle_key``)、已降级、或**空树(0 叶)**
          → 返回 ``None``,绝不产出空 checkpoint 行;
        - 封根异常按既有口径安全降级(:meth:`_degrade`)并返回 ``None``,
          审计本体不受影响;
        - 成功:计数 ``audit.merkle_checkpoint``(与自动封根同一计数),
          checkpoint 行立即写透落盘(缓冲模式先 flush 既有缓冲再写,保证
          行序),返回该行内容(dict,含 ``event``/``ts``/``root``/
          ``leaf_count``/``height``/``signature`` 等字段)供调用方即时
          展示或转存;
        - checkpoint 行本身不入树(树状态元数据),后续事件照常追加,到
          下一 interval 倍数仍会自动封根(多次封根相互独立、均可用
          audit_verify 对账)。
        """
        with self._lock:
            tree = self.merkle_tree
            if tree is None or self.merkle_degraded or tree.leaf_count <= 0:
                return None
            try:
                cp = tree.checkpoint()
            except Exception as exc:  # noqa: BLE001 - 封根失败同样安全降级
                self._degrade(exc)
                return None
            telemetry.inc("audit.merkle_checkpoint")
            record: dict[str, Any] = {"event": "merkle_checkpoint", "ts": now_iso(), **cp}
            line = json.dumps(record, ensure_ascii=False, default=str)
            if self._buffer:  # 缓冲模式:先落既有缓冲,checkpoint 恒在事件行之后
                self._write_lines(self._buffer)
                self._buffer.clear()
            self._write_lines([line])
            return record

    def flush(self) -> None:
        """把缓冲中的记录立即写出;写透模式恒为无操作。"""
        with self._lock:
            if self._buffer:
                self._write_lines(self._buffer)
                self._buffer.clear()

    def close(self) -> None:
        """等价于 :meth:`flush`(批量缓冲模式的收尾出口)。"""
        self.flush()

    def __enter__(self) -> JsonlAuditLogger:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
