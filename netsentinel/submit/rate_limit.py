"""提交频控(NetSentinel · A18;V5 流式升级)。

安全红线(CONTRACTS.md §0/§2):两次真实提交之间必须间隔
``submit_min_interval_s`` 秒(默认 60),且每个本地自然日最多提交
``submit_max_per_day`` 次(默认 5)。频控在 ``run_submit`` 前强制生效,
不可绕过;本模块只做"是否允许提交"的判定与提交时间戳落盘,不发起任何网络请求。

状态文件(V5 起为 JSONL,每行一个 JSON 字符串形态的 ISO 时间戳,逐行流式读写)::

    "2026-10-01T09:00:00"
    "2026-10-01T10:30:00"

V5 之前的旧版整文件 JSON(``{"timestamps": [...]}``)仍可正常读取(兼容既有
部署),下一次 :meth:`RateLimiter.record` 时自动整体迁移重写为 JSONL。

规则:
- 所有时间统一折算为本地时区 naive datetime 再比较(传入 aware datetime
  会自动 ``astimezone()`` 换算,避免 naive/aware 混比抛 TypeError);
- "当日"按本地时区自然日计算,跨天后历史时间戳不再计入当日次数(跨天清零);
- 最小间隔对照的是最近一次提交时间(不限当日);
- 状态文件损坏(不存在 / 非法 JSON / 结构不符)时告警并按空状态重置,
  绝不因状态文件问题误拦合法提交;单条无法解析的时间戳跳过并告警,
  下次 record 时自愈重写为干净文件;
- 目录自动创建;仅使用标准库,离线可用。

V5 升级(契约 §1):
- 性能:读取逐行流式解析(内存占用与文件行长而非文件总量成正比);
  record 健康路径只追加一行(O(1) 写),不再整体读入 + 排序 + 全量重写;
  can_submit 对时间戳单遍扫描同时得到"最近一次"与"当日计数"(旧实现两遍);
- 可观测性:频控拒绝记 ``telemetry.inc("ratelimit.blocked")``;
- 健壮性:整体重写走临时文件 + 原子替换,避免半写损坏状态文件。

V17 升级(G5):``can_submit`` 经 ``_scan_window`` 单遍流式聚合「最近一次 +
当日计数」,不再物化完整时间戳列表(判断内存 O(1));落盘格式与历史兼容
不变,"只读尾部"的 IO 捷径经核实会放宽频控(乱序语义),按契约 §0.6 放弃
(详见 :meth:`_scan_window` docstring)。

进程级互斥(V? · 并发 ``--exec`` 加固):状态文件的读-改-写此前无锁,
多个进程并发 ``--exec`` 时存在竞态(典型:两进程同时触发损坏自愈/旧格式
迁移的整体重写,后写者覆盖前写者 → 丢时间戳行 → 当日计数被低估 → 频控
被放宽 = 超发)。现引入 :class:`_StateFileLock` 跨平台咨询锁:

- Windows 用 ``msvcrt.locking``、POSIX 用 ``fcntl.flock``(均标准库,
  函数内惰性导入);平台判断失败降级为无锁 + debug 日志,频控功能照常;
- 锁文件 ``<state>.lock`` 与状态文件同目录;持有者进程崩溃时 OS 随句柄
  自动释放,另有 stale 超时自愈(默认 30s 后放弃等待、降级无锁继续);
- 单进程语义零变化(锁对既有调用方完全透明)。

决策间隙闭环(V10.18 · TOCTOU 收口):can_submit 与 record 各自持锁只保证
单次读/写的原子性,"判定通过 → 业务执行 → 记账"的**间隙**仍可能被并发
消耗(典型:两个批量提交进程同时通过判定、各自记账,当日配额超发)。
现新增 :meth:`RateLimiter.locked` 上下文管理器(进入即取同一把状态文件
锁 → ``yield self`` → 退出释放),调用方(如 ``submit.batch_submit``)把
整段判定/执行/记账包进同一临界区;段内 ``can_submit`` / ``record`` /
嵌套 ``locked()`` **免重入**(重入计数线程本地),不使用 ``locked()`` 的
既有调用方行为与升级前逐字节一致。

用法示例::

    rl = RateLimiter("data/rate_limit.jsonl", min_interval_s=60, max_per_day=5)
    ok, reason = rl.can_submit()
    if ok:
        rl.record()

    # 或(V10.18):判定→执行→记账整段闭进同一临界区,消除决策间隙
    with rl.locked():
        ok, reason = rl.can_submit()
        if ok:
            ...  # 业务执行
            rl.record()
"""
from __future__ import annotations

import json
import logging
import math
import os
import pathlib
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from netsentinel import telemetry

__all__ = ["RateLimiter"]

logger = logging.getLogger(__name__)

#: 旧版状态文件(整文件 JSON dict,indent 缩进)首行嗅探前缀
_LEGACY_PREFIX = "{"

#: 频控拒绝的遥测计数名
_BLOCKED_METRIC = "ratelimit.blocked"

#: 整体重写时的临时文件后缀(写完后原子替换)
_TMP_SUFFIX = ".tmp"

#: 锁文件后缀(与状态文件同目录,如 ``rate_limit.jsonl.lock``)
_LOCK_SUFFIX = ".lock"

#: stale 锁超时(秒):等待锁超过该时长视为持有者挂起,放弃等待、降级为
#: 无锁继续(持有者进程崩溃时锁由 OS 随句柄自动释放,本超时兜底的是
#: "进程活着但卡死"的场景——频控绝不因锁故障死等或误拦合法提交)。
_LOCK_STALE_TIMEOUT_S = 30.0

#: 获取锁的非阻塞轮询间隔(秒)
_LOCK_POLL_S = 0.05

#: 平台锁类型缓存(``"msvcrt"`` / ``"fcntl"`` / ``None`` = 降级无锁;
#: 首次使用时探测,进程内只探测一次)。
_LOCK_FLAVOR: str | None = None

#: 平台锁是否已探测(区分"未探测"与"探测结果为 None"两态)。
_LOCK_FLAVOR_PROBED = False


def _to_local_naive(dt: datetime) -> datetime:
    """把 aware datetime 折算为本地时区 naive;naive 原样返回。"""
    if dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt


def _serialize_stamp(stamp: datetime) -> str:
    """单个时间戳 → 一行 JSONL(JSON 字符串形态的 ISO 时间戳)。"""
    return json.dumps(stamp.isoformat(), ensure_ascii=False)


def _parse_stamp_line(line: str) -> datetime | None:
    """一行状态记录 → datetime;无法解析返回 None。

    兼容两种行形态:标准 JSONL(JSON 字符串)与手工写入的裸 ISO 文本;
    解析出的非字符串(如数字 / 对象)视为非法行。
    """
    text: Any = None
    try:
        text = json.loads(line)
    except ValueError:
        text = line  # 裸 ISO 文本兜底(手写 / 截断修复场景)
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return _to_local_naive(datetime.fromisoformat(text.strip()))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 进程级互斥:跨平台文件锁(Windows msvcrt / POSIX fcntl,均标准库)
# ---------------------------------------------------------------------------
def _detect_lock_flavor() -> str | None:
    """探测本平台可用的锁原语;返回 ``"msvcrt"`` / ``"fcntl"`` / ``None``。

    - Windows(``os.name == "nt"``)优先 ``msvcrt``;其余平台尝试 ``fcntl``;
    - 模块在函数内惰性导入(均为标准库);两个都不可用 / 导入失败 →
      返回 ``None`` = 降级为无锁 + debug 日志(频控功能照常,绝不因锁
      故障误拦合法提交);
    - 结果进程内缓存(模块级 :data:`_LOCK_FLAVOR`),只探测一次。
    """
    global _LOCK_FLAVOR, _LOCK_FLAVOR_PROBED
    if _LOCK_FLAVOR_PROBED:
        return _LOCK_FLAVOR
    _LOCK_FLAVOR_PROBED = True
    if os.name == "nt" or sys.platform == "win32":
        try:
            import msvcrt  # noqa: F401 - 标准库,惰性导入(仅探测可用性)

            _LOCK_FLAVOR = "msvcrt"
            return _LOCK_FLAVOR
        except ImportError:
            logger.debug("msvcrt 不可用,尝试 fcntl 作为频控锁后备")
    try:
        import fcntl  # noqa: F401 - 标准库,惰性导入(仅探测可用性)

        _LOCK_FLAVOR = "fcntl"
    except ImportError:
        _LOCK_FLAVOR = None
        logger.debug(
            "本平台无可用文件锁原语(msvcrt/fcntl 均不可用),"
            "频控状态文件降级为无锁互斥(单进程行为不受影响)"
        )
    return _LOCK_FLAVOR


class _StateFileLock:
    """频控状态文件的跨平台咨询锁(``with`` 语句使用)。

    - **原语**:Windows ``msvcrt.locking(LK_NBLCK)`` 锁文件首字节;
      POSIX ``fcntl.flock(LOCK_EX | LOCK_NB)`` 独占锁。两者都以**打开的
      文件句柄**为持有者:同进程的两个线程各自打开锁文件同样互斥,
      持有者进程崩溃时 OS 自动释放(锁不落死锁);
    - **锁文件**:``<state>.lock`` 与状态文件同目录,长驻不删(避免
      "释放-删除-重建"窗口里另一进程锁到旧 inode 的经典竞态);
    - **stale 自愈**:非阻塞重试等待 ``stale_timeout_s``(默认 30s,
      见 :data:`_LOCK_STALE_TIMEOUT_S`);超时视为持有者挂起,放弃等待、
      **降级为无锁继续**并记 debug 日志——绝不因锁故障死等或误拦合法
      提交(降级窗口内的并发写与升级前一样由 append 的原子性兜底);
    - **降级**::func:`_detect_lock_flavor` 探测失败(``None``)时本锁
      恒为空操作 + debug 日志。
    """

    __slots__ = ("lock_path", "stale_timeout_s", "poll_s", "_fh")

    def __init__(
        self,
        state_path: pathlib.Path,
        *,
        stale_timeout_s: float = _LOCK_STALE_TIMEOUT_S,
        poll_s: float = _LOCK_POLL_S,
    ) -> None:
        self.lock_path = state_path.with_name(state_path.name + _LOCK_SUFFIX)
        self.stale_timeout_s = max(0.0, float(stale_timeout_s))
        self.poll_s = max(0.0, float(poll_s))
        self._fh = None

    # -- 内部:单次非阻塞加锁尝试 --------------------------------------
    def _try_lock_once(self, fh: Any, flavor: str) -> bool:
        if flavor == "msvcrt":
            import msvcrt  # 标准库;惰性导入(对齐探测函数)

            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        if flavor == "fcntl":
            import fcntl  # 标准库;惰性导入(对齐探测函数)

            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                return False
        return False  # 未知原语:视为降级

    def __enter__(self) -> "_StateFileLock":
        flavor = _detect_lock_flavor()
        if flavor is None:
            return self  # 降级:无锁(探测处已记 debug 日志)
        deadline = time.monotonic() + self.stale_timeout_s
        try:
            fh = self.lock_path.open("a+b")  # 建文件不截断;句柄即锁持有者
        except OSError as exc:
            logger.debug(
                "频控锁文件不可打开,降级为无锁继续:%s(%s)", self.lock_path, exc
            )
            return self
        while True:
            if self._try_lock_once(fh, flavor):
                self._fh = fh
                return self
            if time.monotonic() >= deadline:
                # stale 自愈:持有者疑似挂起,放弃等待、降级无锁继续。
                logger.debug(
                    "频控锁等待超时(%.0fs,持有者疑似挂起),降级为无锁继续:%s",
                    self.stale_timeout_s, self.lock_path,
                )
                try:
                    fh.close()
                except OSError:
                    pass
                return self
            time.sleep(self.poll_s)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        fh = self._fh
        if fh is None:
            return
        self._fh = None
        try:
            flavor = _detect_lock_flavor()
            if flavor == "msvcrt":
                import msvcrt  # 标准库;惰性导入

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            elif flavor == "fcntl":
                import fcntl  # 标准库;惰性导入

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            # 释放失败只留痕:句柄随 with 退出关闭,OS 兜底回收锁。
            logger.debug("频控锁释放失败(句柄关闭时由 OS 回收):%s", exc)
        finally:
            try:
                fh.close()
            except OSError:
                pass


class RateLimiter:
    """基于落盘时间戳的提交频控器。

    :param state_path: 状态文件路径(父目录自动创建)。
    :param min_interval_s: 两次提交的最小间隔秒数(>= 0)。
    :param max_per_day: 每个本地自然日允许的最大提交次数(>= 1)。
    :param lock_stale_timeout_s: 状态文件锁的 stale 超时秒数(缺省
        :data:`_LOCK_STALE_TIMEOUT_S` = 30s;仅供测试注入,生产用缺省)。
    """

    def __init__(
        self,
        state_path: str,
        min_interval_s: int,
        max_per_day: int,
        *,
        lock_stale_timeout_s: float = _LOCK_STALE_TIMEOUT_S,
    ) -> None:
        if int(min_interval_s) < 0:
            raise ValueError(f"min_interval_s 必须 >= 0,当前为 {min_interval_s!r}")
        if int(max_per_day) < 1:
            raise ValueError(f"max_per_day 必须 >= 1,当前为 {max_per_day!r}")
        self.state_path = pathlib.Path(state_path)
        self.min_interval_s = int(min_interval_s)
        self.max_per_day = int(max_per_day)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        # 进程级互斥:can_submit / record 的状态文件读写都套同一把文件锁
        # (<state>.lock,跨进程 + 跨线程),单进程单线程语义零变化。
        self._lock = _StateFileLock(
            self.state_path, stale_timeout_s=lock_stale_timeout_s
        )
        # V10.18 决策间隙闭环:locked() 临界区的重入计数(线程本地,
        # 见 :meth:`locked`)。恒为 0 的线程里 can_submit / record 的
        # 持锁行为与升级前逐字节一致。
        self._local = threading.local()
        logger.debug(
            "频控器就绪:%s(最小间隔 %ds,每日上限 %d 次)",
            self.state_path,
            self.min_interval_s,
            self.max_per_day,
        )

    # ------------------------------------------------------------------
    # 状态读取(流式:逐行解析,内存占用只与单行长度相关)
    # ------------------------------------------------------------------
    def _load_state(self) -> tuple[list[datetime], bool]:
        """流式读取状态文件,返回 ``(时间戳列表, 是否需要修复/迁移)``。

        ``dirty=True`` 表示文件存在损坏行或仍是旧版 dict 格式,调用方在下次
        落盘时应整体重写自愈;文件缺失返回 ``([], False)``。
        """
        if not self.state_path.exists():
            return [], False
        stamps: list[datetime] = []
        dirty = False
        try:
            with self.state_path.open("r", encoding="utf-8") as fh:
                first_line = True
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    if first_line:
                        first_line = False
                        if line.startswith(_LEGACY_PREFIX):
                            # 旧版整文件 JSON:兼容读取,标记待迁移
                            return self._load_legacy(), True
                    stamp = _parse_stamp_line(line)
                    if stamp is None:
                        dirty = True
                        logger.warning("跳过无法解析的提交时间戳:%r", line)
                    else:
                        stamps.append(stamp)
        except OSError as exc:
            logger.warning("频控状态文件不可读,按空状态处理:%s(%s)", self.state_path, exc)
            return [], True
        return stamps, dirty

    def _load_legacy(self) -> list[datetime]:
        """兼容读取旧版整文件 JSON(``{"timestamps": [...]}``);损坏时重置为空。"""
        try:
            data: Any = json.loads(self.state_path.read_text(encoding="utf-8"))
            raw = data.get("timestamps") if isinstance(data, dict) else None
            if not isinstance(raw, list):
                raise ValueError("状态文件缺少合法的 timestamps 列表")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("频控状态文件损坏,已重置为空:%s(%s)", self.state_path, exc)
            return []
        stamps: list[datetime] = []
        for item in raw:
            try:
                stamps.append(_to_local_naive(datetime.fromisoformat(str(item))))
            except (TypeError, ValueError):
                logger.warning("跳过无法解析的提交时间戳:%r", item)
        return stamps

    # ------------------------------------------------------------------
    # 状态写入(健康路径 O(1) 追加;修复/迁移路径临时文件 + 原子替换)
    # ------------------------------------------------------------------
    def _append_stamp(self, stamp: datetime) -> None:
        """把单个时间戳追加为一行 JSONL(不再整体重写历史;目录自动创建)。"""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.state_path.open("a", encoding="utf-8") as fh:
            fh.write(_serialize_stamp(stamp) + "\n")

    def _rewrite_stamps(self, stamps: list[datetime]) -> None:
        """整体重写状态文件(仅损坏自愈 / 旧格式迁移时使用)。

        逐行流式写出,先写临时文件再原子替换,避免半写把状态文件写坏。
        """
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_name(self.state_path.name + _TMP_SUFFIX)
        try:
            with tmp_path.open("w", encoding="utf-8") as fh:
                for stamp in stamps:
                    fh.write(_serialize_stamp(stamp) + "\n")
            # V10.12:并行两次提交可同时触发状态改写,Windows 上目标被短暂
            # 占用时有限重试(POSIX 行为不变;频控语义零变化)。
            from netsentinel.atomic import replace_with_retry  # 惰性:兄弟微工具

            replace_with_retry(tmp_path, self.state_path)
        except OSError:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

    # ------------------------------------------------------------------
    # 窗口判断(V17:单遍流式聚合,内存 O(1),不再物化完整时间戳列表)
    # ------------------------------------------------------------------
    def _scan_window(self, now: datetime) -> tuple[datetime | None, int]:
        """单遍流式扫描状态文件,聚合出 ``(最近一次提交, 当日提交次数)``。

        与旧实现(先 :meth:`_load_state` 物化完整列表、再扫一遍列表)逐语义
        等价——同一组行、同一比较口径(最近一次取全局最大、当日按本地自然日
        计数,**乱序落盘同样正确**);旧版整文件 JSON、裸 ISO 行与损坏行告警
        跳过的兼容逻辑与 :meth:`_load_state` 完全一致;读取失败(OSError)
        整体按空状态处理(绝不因状态文件问题误拦合法提交)。

        O(1) 口径与诚实边界(V17 · G5,契约 §0.6):
        - 聚合只维护「最近一次 + 当日计数」两个标量,内存占用与文件行数无关
          (旧实现在内存中持有全部历史时间戳);解析一遍产出,不再有第二遍
          列表扫描;
        - **放弃**"只读尾部 N 行"的 IO 捷径:状态文件允许乱序落盘(V5 兼容
          语义,手写/参数乱序均合法),任何尾部截断都可能把「最近一次」算小,
          从而放宽提交频控(红线);record 健康路径只追加、旧行字节不变(亦被
          测试锁定),无法借写入维护窗口元数据。故 IO 仍按行流式全量,这是
          乱序语义下的信息论下界。
        """
        latest: datetime | None = None
        today_count = 0

        def _feed(stamp: datetime) -> None:
            nonlocal latest, today_count
            if latest is None or stamp > latest:
                latest = stamp
            if stamp.date() == now.date():
                today_count += 1

        if not self.state_path.exists():
            return latest, today_count
        try:
            with self.state_path.open("r", encoding="utf-8") as fh:
                first_line = True
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    if first_line:
                        first_line = False
                        if line.startswith(_LEGACY_PREFIX):
                            # 旧版整文件 JSON:兼容读取(与 _load_state 同口径)
                            for stamp in self._load_legacy():
                                _feed(stamp)
                            return latest, today_count
                    stamp = _parse_stamp_line(line)
                    if stamp is None:
                        logger.warning("跳过无法解析的提交时间戳:%r", line)
                    else:
                        _feed(stamp)
        except OSError as exc:
            logger.warning("频控状态文件不可读,按空状态处理:%s(%s)", self.state_path, exc)
            return None, 0
        return latest, today_count

    # ------------------------------------------------------------------
    # 决策间隙闭环(V10.18):locked() 临界区 + 免重入状态锁
    # ------------------------------------------------------------------
    @contextmanager
    def locked(self) -> Iterator["RateLimiter"]:
        """频控决策临界区:进入即取状态文件锁 → ``yield self`` → 退出释放。

        把调用方"``can_submit`` 判定 → 业务执行 → ``record`` 落账"整段包进
        同一临界区,消除"判定与记账之间当日额度被并发消耗"的 TOCTOU 决策
        间隙(V10.18 · V10.17 遗留③)。

        - **互斥口径**:与 :meth:`can_submit` / :meth:`record` 内部使用的
          是同一把 ``<state>.lock`` 文件锁(跨进程 + 跨线程)——临界区内
          其他线程/进程的频控读写照常排队等待;stale 超时降级语义不变
          (持有者疑似挂起时放弃等待、降级无锁继续,绝不死等或误拦合法
          提交);
        - **免重入**:本线程已在 ``locked()`` 段内时,段内的
          ``can_submit`` / ``record`` / 嵌套 ``locked()`` 不再重复取锁,
          直接共享外层临界区(重入计数线程本地:别的线程照常争锁,
          永不误判);异常退出时计数在 ``finally`` 复位;
        - **等价性**:不使用 ``locked()`` 的既有调用方(重入计数恒为 0)
          行为与升级前逐字节一致——``can_submit`` / ``record`` 仍各自
          持锁完成读-改-写。

        用法::

            with rl.locked():
                ok, reason = rl.can_submit()
                if ok:
                    ...  # 业务执行
                    rl.record()
        """
        depth = getattr(self._local, "depth", 0)
        if depth > 0:  # 本线程已持锁:免重入,共享外层临界区
            self._local.depth = depth + 1
            try:
                yield self
            finally:
                self._local.depth = depth
            return
        with self._lock:
            self._local.depth = 1
            try:
                yield self
            finally:
                self._local.depth = 0

    @contextmanager
    def _state_lock(self) -> Iterator[None]:
        """can_submit / record 的状态锁:本线程已持 :meth:`locked` 时免重入。

        重入计数为 0(默认路径,含全部既有调用方与测试)时严格等价于
        ``with self._lock``——同一把文件锁、同一等待/降级语义,零行为差异;
        计数 > 0(仅在 ``locked()`` 段内)时跳过重复取锁,读写共享外层
        临界区。
        """
        if getattr(self._local, "depth", 0) > 0:
            yield
            return
        with self._lock:
            yield

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------
    def can_submit(self, now: datetime | None = None) -> tuple[bool, str]:
        """判断当前时刻是否允许提交。

        :param now: 判定时刻;缺省取本地当前时间(aware 入参自动换算)。
        :return: ``(True, "")`` 表示允许;``(False, 中文原因)`` 表示被频控拒绝
            (拒绝时记 ``telemetry.inc("ratelimit.blocked")``)。

        V5:对时间戳单遍扫描同时求出"最近一次提交"与"当日提交次数"
        (旧实现先 ``sum`` 再 ``max`` 两遍扫描)。
        V17(G5):改走 :meth:`_scan_window` 单遍流式聚合——边读边解析边聚合,
        不再物化完整时间戳列表(内存 O(1)),判定口径与文案逐字不变。
        进程级互斥:扫描全程持有状态文件锁,避免读到另一进程整体重写途中
        的半写文件(半读会被误判为损坏行 → 自愈重写丢历史)。V10.18:本
        线程已持 :meth:`locked` 临界区时免重入(共享外层临界区,语义不变)。
        """
        now = _to_local_naive(now or datetime.now())
        with self._state_lock():
            latest, today_count = self._scan_window(now)

        if today_count >= self.max_per_day:
            telemetry.inc(_BLOCKED_METRIC)
            return (
                False,
                f"当日(本地时区)已提交 {today_count} 次,"
                f"达到每日上限 {self.max_per_day} 次,请明日再提交",
            )

        if latest is not None:
            elapsed = (now - latest).total_seconds()
            if elapsed < self.min_interval_s:
                wait = max(1, math.ceil(self.min_interval_s - elapsed))
                telemetry.inc(_BLOCKED_METRIC)
                return (
                    False,
                    f"距上次提交仅 {max(0, int(elapsed))} 秒,"
                    f"小于最小间隔 {self.min_interval_s} 秒,请约 {wait} 秒后再试",
                )
        return (True, "")

    def record(self, now: datetime | None = None) -> None:
        """记录一次提交并落盘。

        V5:状态文件健康时只追加一行(O(1) 写,历史字节原样保留);检测到
        损坏行或旧版 dict 格式时先自愈重写为干净的 JSONL(含恢复出的有效
        历史与本次提交,排序保持时间先后)。
        进程级互斥:整个"读状态 → 追加/重写"读-改-写全程持有状态文件锁
        (``<state>.lock``),多进程并发 ``--exec`` 时不再有后写覆盖前写、
        丢时间戳行(当日计数被低估 → 频控被放宽)的竞态;锁降级
        (平台无原语 / stale 超时)时行为与升级前一致。V10.18:本线程已持
        :meth:`locked` 临界区时免重入(共享外层临界区,语义不变)。
        """
        now = _to_local_naive(now or datetime.now())
        with self._state_lock():
            stamps, dirty = self._load_state()
            if dirty:
                stamps.append(now)
                self._rewrite_stamps(sorted(stamps))
            else:
                self._append_stamp(now)
                stamps.append(now)
        logger.info(
            "记录一次提交:%s(累计 %d 条,状态文件:%s)",
            now.isoformat(),
            len(stamps),
            self.state_path,
        )
