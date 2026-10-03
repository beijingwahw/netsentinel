"""NetSentinel(净网哨兵)三档并发持久状态与会话一次性自动建议(ops.tier_state,A165)。

V9「CPU 自适应三档并发」的**档位账本**:把"当前生效档 / workers / 核数 /
由谁设定"落在 ``data/concurrency.json`` 一个小 JSON 文件里,并给批量入口
(A169 ``finishflow`` 等)提供一次性自动探测建议。三个入口(签名见
CONTRACTS-V9 §3 A165):

- :class:`TierState`:持久档位的原子读写器。:meth:`TierState.get` 读档
  (文件缺失 / JSON 损坏 / 结构不符 → 一律 ``None``,绝不抛出);
  :meth:`TierState.set` **原子写**(同目录临时文件 + ``os.replace``,
  写前 flush+fsync,实例内加锁;Windows 下目标被并发短暂占用时短暂重试),
  非法档位 / 非法 workers / cores / set_by 直接抛中文 :class:`ValueError`,
  文件保持原样。
- :func:`resolve_tier`:解析"当前生效档"。**读取优先级恒为
  ``cfg.concurrency_tier`` 字段 > 持久文件**——``Config.concurrency_tier``
  总有值(mid 是默认),无从区分"用户显式 mid"与"默认 mid",故约定
  cfg 恒优先,持久文件仅作展示与 auto 建议来源;cfg 档位非法(绕过
  配置校验直接改 dataclass 才可能出现)时才回退持久档,再回退 ``mid``。
- :func:`tier_once`:会话幂等的一次性自动建议。首调用流程:

  1. ``cfg.concurrency_auto=False`` → ``{"action": "disabled"}``(零副作用);
  2. 持久档已存在 → ``{"action": "kept", "tier": ...}``——**auto 绝不
     覆盖既有档**(用户或上次运行已经选过);
  3. 否则 detect → recommend → ``set(set_by="auto")``(A163
     ``ops.cpu_profile`` 惰性导入,可注入 ``detector`` 便于离线测试)→
     ``{"action": "auto", "tier", "workers"}``,并惰性写一行审计
     ``log_event("tier_once", action="auto", ...)``。

  会话幂等由模块级标记实现:同一进程内第二次及以后的调用直接返回首结果
  (浅拷贝),不再探测、不再写文件、**不再累计遥测**——
  ``telemetry.inc("tier.once.{action}")`` 每动作每会话恰一次。

红线关联:

- 红线 35(压榨边界):本模块只决定本地计算的 workers 档位,不触碰
  ``fetch_delay_s``、引擎限速与举报频控——档位与对外礼貌间隔互不相干;
- 红线 37(资源治理):workers 数由 A163 ``tier_workers`` 按 §1 公式换算,
  恒 ≤ 核数,本模块不自行发明数值。

纯本地、零网络;兄弟模块(``ops.cpu_profile``、``logging_util``)一律
惰性导入且只读——缺席时 :func:`tier_once` 的 auto 分支抛中文
:class:`RuntimeError`,``TierState`` / :func:`resolve_tier` 完全独立可用。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from netsentinel import telemetry

__all__ = [
    "AUDIT_EVENT",
    "DEFAULT_STATE_PATH",
    "TIERS",
    "TierState",
    "resolve_tier",
    "tier_once",
]

logger = logging.getLogger(__name__)

#: 持久档位缺省路径(相对当前工作目录;CONTRACTS-V9 §3 A165)
DEFAULT_STATE_PATH = "data/concurrency.json"

#: 合法三档(CONTRACTS-V9 §1 权威口径;A165 自持以保证档位文件读写独立可用)
TIERS = ("low", "mid", "high")

#: 审计事件名(``JsonlAuditLogger.log_event`` 的 event 字段;仅 auto 落审计)
AUDIT_EVENT = "tier_once"

#: 档位文件缺省核数兜底(与 CONTRACTS-V9 §1「探测不到回退 2」一致)
_FALLBACK_CORES = 2

#: ``os.replace`` 在 Windows 上目标被并发短暂占用时的重试参数
#: (读者句柄 duty-cycle 很高时也需要在 ~0.5s 内找到空隙;耗尽才抛出)
_REPLACE_ATTEMPTS = 50
_REPLACE_WAIT_S = 0.01

# 会话幂等标记:进程内首次 tier_once 的结果(None = 本会话尚未运行)
_ONCE_LOCK = threading.Lock()
_ONCE_RESULT: dict[str, Any] | None = None


def _reset_once_guard() -> None:
    """清空会话幂等标记(**仅测试使用**;与 config._reset_unknown_key_warnings 同口径)。"""
    global _ONCE_RESULT
    with _ONCE_LOCK:
        _ONCE_RESULT = None


# ---------------------------------------------------------------------------
# 字段校验(档位文件格式归 A165 自持;只认恰好四键的规范记录)
# ---------------------------------------------------------------------------
def _is_valid_tier(tier: object) -> bool:
    """档位必须恰为 low / mid / high 之一(区分大小写)。"""
    return isinstance(tier, str) and tier in TIERS


def _is_valid_count(value: object) -> bool:
    """workers / cores 必须是 ≥1 的真整数(bool 是 int 子类,显式排除)。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _validate_record(data: object) -> dict[str, Any] | None:
    """把已解析的 JSON 折成规范四键记录;任何不符 → None(视同损坏)。"""
    if not isinstance(data, dict):
        return None
    tier = data.get("tier")
    workers = data.get("workers")
    cores = data.get("cores")
    set_by = data.get("set_by")
    if not _is_valid_tier(tier) or not _is_valid_count(workers) or not _is_valid_count(cores):
        return None
    if not isinstance(set_by, str) or not set_by.strip():
        return None
    return {"tier": tier, "workers": workers, "cores": cores, "set_by": set_by}


# ---------------------------------------------------------------------------
# 持久档位:原子读写
# ---------------------------------------------------------------------------
class TierState:
    """三档并发持久状态(``data/concurrency.json``)的原子读写器。

    文件格式:恰好四键的 JSON 对象 ``{"tier", "workers", "cores", "set_by"}``;
    读取侧对任何异常(缺失 / 不可读 / JSON 损坏 / 结构不符)一律按
    "无持久档"返回 ``None``,绝不抛出——档位建议是锦上添花,不该把
    批量主流程砸崩。写入侧则是**严格的**:非法输入抛中文
    :class:`ValueError` 且不动原文件,成功则原子替换(读者要么看到旧
    整档、要么看到新整档,永远看不到半行 JSON)。
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"TierState({str(self.path)!r})"

    # -- 读 -----------------------------------------------------------------
    def get(self) -> dict[str, Any] | None:
        """读取持久档位;无文件 / 损坏 / 结构不符 → ``None``。

        :return: 规范四键记录 ``{"tier", "workers", "cores", "set_by"}``,
            或 ``None``(调用方一律按"从未设定档位"处理)。
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None  # 缺失 / 短暂占用 / 编码坏:视同无持久档
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None  # 损坏 JSON → None(契约 §3 A165)
        return _validate_record(data)

    # -- 写 -----------------------------------------------------------------
    def set(self, tier: str, *, workers: int, cores: int, set_by: str) -> dict[str, Any]:
        """原子写入一条档位记录并返回该记录。

        原子性:先写同目录临时文件 ``<name>.tmp``(flush + fsync 落盘),
        再 ``os.replace`` 原子替换;实例内加锁串行化同进程多线程写,
        Windows 下目标文件被读者短暂占用时短暂重试(总计 ≤ 约 0.2s),
        重试耗尽才抛出,并清理残留临时文件。

        :param tier: 档位,必须为 ``low`` / ``mid`` / ``high``;
        :param workers: 该档 workers 数(≥1 整数,由调用方按 §1 公式换算);
        :param cores: 写档时探测到的核数(≥1 整数);
        :param set_by: 设定来源标识(``"auto"`` / ``"user"`` / CLI 子命令名等)。
        :raises ValueError: 任一字段非法(中文消息,含当前值)。
        :raises OSError: 临时文件或替换最终仍失败(原文件保持原样)。
        """
        if not _is_valid_tier(tier):
            raise ValueError(
                f"并发档位非法:{tier!r},必须是 low / mid / high 三者之一"
            )
        if not _is_valid_count(workers):
            raise ValueError(f"workers 非法:{workers!r},必须是 ≥1 的整数")
        if not _is_valid_count(cores):
            raise ValueError(f"cores 非法:{cores!r},必须是 ≥1 的整数")
        if not isinstance(set_by, str) or not set_by.strip():
            raise ValueError(f"set_by 非法:{set_by!r},必须是非空字符串")
        record = {"tier": tier, "workers": workers, "cores": cores, "set_by": set_by}
        payload = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
        tmp = self.path.with_name(self.path.name + ".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                _replace_with_retry(tmp, self.path)
            except BaseException:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
        return record


def _replace_with_retry(src: Path, dst: Path) -> None:
    """``os.replace`` 原子替换;Windows 目标被短暂占用时有限重试后放弃。"""
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_WAIT_S)


# ---------------------------------------------------------------------------
# 生效档解析:cfg 恒优先
# ---------------------------------------------------------------------------
def resolve_tier(cfg: Any, *, state: TierState | None = None) -> str:
    """解析当前生效档:**``cfg.concurrency_tier`` 显式值恒优先**。

    优先级(自上而下):

    1. ``cfg.concurrency_tier`` 合法(low/mid/high)→ 原样返回。它总是
       有值(mid 是默认),"用户显式 mid"与"默认 mid"无从区分,故约定
       **cfg 恒优先**,持久文件只作展示与 auto 建议,不参与生效档仲裁;
    2. cfg 档位非法(仅绕过配置校验直接改 dataclass 才会出现)→ 回退
       持久档(:class:`TierState.get`,损坏 / 缺失按无档),并记 WARNING;
    3. 持久档也没有 → 回退默认 ``"mid"``。

    :param cfg: 配置对象(取 ``concurrency_tier`` 字段);
    :param state: 持久档读写器;``None`` 时用 :data:`DEFAULT_STATE_PATH`。
    :return: ``"low"`` / ``"mid"`` / ``"high"`` 之一。
    """
    tier = getattr(cfg, "concurrency_tier", "mid")
    if _is_valid_tier(tier):
        return tier
    logger.warning(
        "配置 concurrency_tier=%r 非法(应为 low/mid/high),回退持久档/默认 mid", tier
    )
    if state is None:
        state = TierState(DEFAULT_STATE_PATH)
    persisted = state.get()
    if persisted is not None:
        return persisted["tier"]
    return "mid"


# ---------------------------------------------------------------------------
# 会话一次性自动建议
# ---------------------------------------------------------------------------
def _load_detector() -> Any:
    """惰性导入 A163 ``ops.cpu_profile`` 作为缺省探测器;缺席抛中文 RuntimeError。

    经 :func:`importlib.import_module` 按名导入(而非 ``from ... import``):
    兄弟模块被测试置 ``None`` / 尚未交付 / 损坏时都会走到 except 分支,
    统一收敛为中文 RuntimeError,不让上层看到 ImportError 谜语。
    """
    try:
        from importlib import import_module

        return import_module("netsentinel.ops.cpu_profile")
    except Exception as exc:  # noqa: BLE001 - 兄弟模块缺席不该变成 ImportError 谜语
        raise RuntimeError(
            "CPU 探测模块未就位:netsentinel.ops.cpu_profile(A163)缺席或损坏,"
            f"无法自动建议并发档位({exc})"
        ) from None


def _audit_auto(cfg: Any, record: dict[str, Any]) -> None:
    """惰性写一行 auto 审计(``JsonlAuditLogger``);任何失败只告警不阻断。

    仅 auto 分支调用(kept/disabled 是无副作用跳过,零文件零审计);
    ``cfg`` 无 ``audit_path`` 或构造/写入失败均按"审计不可用"降级。
    """
    try:
        from netsentinel.logging_util import JsonlAuditLogger
    except Exception as exc:  # noqa: BLE001
        logger.warning("审计日志器未就位(netsentinel.logging_util 缺席),跳过档位审计:%s", exc)
        return
    audit_path = str(getattr(cfg, "audit_path", "") or "")
    if not audit_path:
        return
    try:
        JsonlAuditLogger(audit_path).log_event(
            AUDIT_EVENT,
            action="auto",
            tier=record["tier"],
            workers=record["workers"],
            cores=record["cores"],
            set_by="auto",
        )
    except Exception as exc:  # noqa: BLE001 - 审计失败不阻断档位建议
        logger.warning("档位审计写入失败(不阻断自动建议):%s", exc)


def _auto_pick(cfg: Any, state: TierState, detector: Any) -> dict[str, Any]:
    """auto 分支:detect → recommend → set(set_by="auto")并返回结果。"""
    mod = detector if detector is not None else _load_detector()
    detect_fn = getattr(mod, "detect", None)
    recommend_fn = getattr(mod, "recommend", None)
    workers_fn = getattr(mod, "tier_workers", None)
    if not callable(detect_fn) or not callable(recommend_fn) or not callable(workers_fn):
        raise RuntimeError(
            "探测器形态异常:须提供可调用的 detect()/recommend()/tier_workers()"
            "(A163 ops.cpu_profile 契约)"
        )
    profile = detect_fn()
    tier = recommend_fn(profile)
    cores_in = profile.get("cores") if isinstance(profile, dict) else None
    workers = workers_fn(tier, reserve=int(getattr(cfg, "cpu_reserve", 1)), cores=cores_in)
    cores = cores_in if _is_valid_count(cores_in) else _FALLBACK_CORES
    record = state.set(tier, workers=int(workers), cores=int(cores), set_by="auto")
    _audit_auto(cfg, record)
    return {"action": "auto", "tier": record["tier"], "workers": record["workers"]}


def _run_once(cfg: Any, *, state: TierState | None, detector: Any) -> dict[str, Any]:
    """首会话执行体(调用方负责幂等门与缓存;此处恒真实执行一次)。"""
    if not bool(getattr(cfg, "concurrency_auto", False)):
        return {"action": "disabled"}
    if state is None:
        state = TierState(DEFAULT_STATE_PATH)
    persisted = state.get()
    if persisted is not None:
        # auto 绝不覆盖既有档:用户(或上次运行)已选过,原样保留
        return {"action": "kept", "tier": persisted["tier"]}
    return _auto_pick(cfg, state, detector)


def tier_once(
    cfg: Any, *, state: TierState | None = None, detector: Any = None
) -> dict[str, Any]:
    """会话一次性档位自动建议(幂等;模块级标记,同进程只真实执行一次)。

    分支与返回值:

    - ``cfg.concurrency_auto=False`` → ``{"action": "disabled"}``;
    - 持久档已存在 → ``{"action": "kept", "tier": <持久档>}``(不覆盖、
      不探测、零文件副作用);
    - 否则 detect → recommend → ``set(set_by="auto")`` →
      ``{"action": "auto", "tier", "workers"}``,并惰性写一行审计。

    遥测:每次真实执行累加 ``telemetry.inc("tier.once.{action}")``
    (disabled/kept/auto 三种);幂等重复调用返回首结果浅拷贝,不再计数。
    自动建议写入的持久档**只是建议**:生效档仍由 :func:`resolve_tier`
    按 cfg 恒优先解析(契约 §3 A165)。

    :param cfg: 配置对象(``concurrency_auto`` / ``cpu_reserve`` / ``audit_path``);
    :param state: 持久档读写器;``None`` 时用 :data:`DEFAULT_STATE_PATH`;
    :param detector: 注入探测器(须有 ``detect()`` / ``recommend(profile)`` /
        ``tier_workers(tier, *, reserve, cores)``,鸭子等价于 A163 cpu_profile);
        ``None`` 时惰性导入 :mod:`netsentinel.ops.cpu_profile`。
    :raises RuntimeError: auto 分支下 A163 缺席或探测器形态异常。
    """
    global _ONCE_RESULT
    with _ONCE_LOCK:
        if _ONCE_RESULT is not None:  # 本会话已运行:直接复用首结果
            return dict(_ONCE_RESULT)
        result = _run_once(cfg, state=state, detector=detector)
        telemetry.inc(f"tier.once.{result['action']}")
        _ONCE_RESULT = dict(result)
        return dict(result)
