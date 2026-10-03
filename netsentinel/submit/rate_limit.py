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

用法示例::

    rl = RateLimiter("data/rate_limit.jsonl", min_interval_s=60, max_per_day=5)
    ok, reason = rl.can_submit()
    if ok:
        rl.record()
"""
from __future__ import annotations

import json
import logging
import math
import pathlib
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


class RateLimiter:
    """基于落盘时间戳的提交频控器。

    :param state_path: 状态文件路径(父目录自动创建)。
    :param min_interval_s: 两次提交的最小间隔秒数(>= 0)。
    :param max_per_day: 每个本地自然日允许的最大提交次数(>= 1)。
    """

    def __init__(self, state_path: str, min_interval_s: int, max_per_day: int) -> None:
        if int(min_interval_s) < 0:
            raise ValueError(f"min_interval_s 必须 >= 0,当前为 {min_interval_s!r}")
        if int(max_per_day) < 1:
            raise ValueError(f"max_per_day 必须 >= 1,当前为 {max_per_day!r}")
        self.state_path = pathlib.Path(state_path)
        self.min_interval_s = int(min_interval_s)
        self.max_per_day = int(max_per_day)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
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
            tmp_path.replace(self.state_path)
        except OSError:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

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
        """
        now = _to_local_naive(now or datetime.now())
        stamps, _dirty = self._load_state()

        latest: datetime | None = None
        today_count = 0
        for stamp in stamps:
            if latest is None or stamp > latest:
                latest = stamp
            if stamp.date() == now.date():
                today_count += 1

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
        """
        now = _to_local_naive(now or datetime.now())
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
