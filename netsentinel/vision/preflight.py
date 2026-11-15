"""VLM 扫描预检(P1-5):爬取之前的三道免费闸门,杜绝"爬完才全灭"的空烧。

背景(实测事故,2026-10-13 14:12–14:19):GLM 账户余额耗尽(HTTP 429
code 1113)后,系统仍完整爬完 2 个真实站点(慢站采样 40–80s/页 × 3 页,
合计约 3.5 分钟 capture),57 次 429 盲打后才由 V10.24 快速中止收场。
V10.24 的熔断在**爬完之后**;本模块把同类系统性故障拦在**爬取之前**。

## 三道闸门(全部零外呼、零新成本)

1. **计量检测(该不该检)**:解析本站将使用的分类器成员(主分类器 +
   ``ensemble_members``),只有存在 ``consumes_vlm_budget = True`` 的成员
   (远程 VLM 家族:vlm / glm / cascade / failover)才继续后续检查——
   纯本地扫描(stub / nudenet / clip / guard)零打扰,连 vlm_cache.db
   都不打开。**注入 classifier 的调用路径(测试/服务自定义)整体跳过
   预检**:注入即调用方接管识别链路,预检不越权。
2. **健康账本(上一站还活着吗)**:进程内线程安全账本记录每个成员的
   最近扫描结局——评分阶段抛异常(V10.24 全败中止 / 无可用成员)记
   **dead**,成功返回记 **ok** 并清除 dead。预检时若成员 dead 且未过
   ``vlm_health_ttl_s``(缺省 300s)→ 直接中文报错跳过本站,提示人工
   ``python -m netsentinel.vision.vlmctl ping 规格`` 诊断(红线 20:
   外呼诊断只能人工显式发起,预检**绝不自动 ping**)。批量扫描里,
   上一站的系统性死亡让后续站点毫秒级跳过,不再重复"爬几分钟→全灭"。
3. **本地预算闸(还剩多少额度)**:读 VlmCache 当日 ``used``(同一本账,
   vlm_cache.py:328-342),余量 < 1 → 中文报错跳过本站(计数
   ``scan.skipped.no_budget``;缓存命中不扣预算,纯缓存重扫如需放行可
   临时 ``vlm_budget_precheck: false``)。余量偏小(< 成员数 × 5)只警告
   不拦截——部分预算耗尽属正常运营,V10.24 部分失败诚信升级已兜底。

## 语义边界(如实登记)

- 本地预算账(``vlm_daily_budget``)与**提供方侧**余额/配额是两本账:
  提供方余额耗尽(10/13 事故形态)在本地账上不可见,由闸门 2(上一站
  死亡)与 V10.24 熔断(本站全败)接力拦截,首站冷启动仍需人工 ping
  诊断——预检不发明新外呼。
- 预检抛出的 RuntimeError 与 V10.24 中止同款"整站以错误收场"语义:
  批量池(python ops/pool)按站隔离失败,不拖垮整批。
- 开关:``vlm_budget_precheck``(一等 Config 字段,缺省 True;False =
  逐字节回旧行为,预检零调用)。

仅依赖标准库 + 同包模块;离线可用、零外呼。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config

__all__ = [
    "preflight_scan",
    "record_scan_health",
    "metered_members",
    "reset_preflight_state",
    "PREFLIGHT_SKIP_BUDGET",
    "PREFLIGHT_SKIP_DEAD",
]

logger = logging.getLogger(__name__)

#: 健康账本最小余量警告线:余量 < 计量成员数 × 该常数时仅警告不拦截
_LOW_BUDGET_WARN_FACTOR = 5

#: 进程内健康账本(成员名 -> (ok: bool, 单调时刻));跨进程不共享(如实登记)
_HEALTH: dict[str, tuple[bool, float]] = {}
_HEALTH_LOCK = threading.Lock()

#: 计量成员解析记忆((主名, 成员元组) -> 计量成员名列表):同配置不重复构造
_METERED_MEMO: dict[tuple, list[str]] = {}
_METERED_MEMO_LOCK = threading.Lock()


def reset_preflight_state() -> None:
    """清空健康账本与解析记忆(仅供测试使用)。"""
    with _HEALTH_LOCK:
        _HEALTH.clear()
    with _METERED_MEMO_LOCK:
        _METERED_MEMO.clear()


def _requested_members(cfg: Config) -> list[str]:
    """本站将请求的分类器成员名(与 _collect_member_scores 同一口径)。"""
    names: list[str] = []
    if cfg.classifier:
        names.append(str(cfg.classifier))
    names.extend(str(m) for m in (cfg.ensemble_members or []))
    seen: set[str] = set()
    ordered: list[str] = []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            ordered.append(n)
    return ordered


def metered_members(cfg: Config) -> list[str]:
    """解析配置里消耗 VlmCache 日预算的成员名(远程 VLM 家族)。

    经 ``classifier_base.get_classifier`` 逐名构造后读类属性
    ``consumes_vlm_budget``;构造失败(依赖缺失/配置错)的成员按"不可用"
    处理——扫描主体自会给出该成员的准确中文报错,预检不抢答。结果按
    配置签名记忆(同配置同进程只解析一次)。
    """
    key = (str(cfg.classifier), tuple(str(m) for m in (cfg.ensemble_members or [])))
    with _METERED_MEMO_LOCK:
        cached = _METERED_MEMO.get(key)
    if cached is not None:
        return list(cached)

    from netsentinel.vision.classifier_base import get_classifier

    metered: list[str] = []
    for name in _requested_members(cfg):
        try:
            inst = get_classifier(name, cfg)
        except Exception as exc:  # noqa: BLE001 - 构造失败=不可用,主体报错
            logger.debug("预检:成员 %r 构造失败(扫描主体会报错),预检跳过:%s", name, exc)
            continue
        if bool(getattr(inst, "consumes_vlm_budget", False)):
            metered.append(name)
    with _METERED_MEMO_LOCK:
        _METERED_MEMO[key] = list(metered)
    return metered


def record_scan_health(member_names: list[str], ok: bool) -> None:
    """把一次扫描的评分结局写入健康账本(orchestrator 评分阶段收尾调用)。

    - ``ok=True``(评分阶段完整返回,含部分图片失败——提供方仍活着):
      记 ok 并清除该成员的 dead 标记;
    - ``ok=False``(评分阶段抛异常:V10.24 全败中止 / 无可用成员):
      记 dead,后续站点预检按 TTL 拦截。
    """
    now = time.monotonic()
    with _HEALTH_LOCK:
        for name in member_names:
            _HEALTH[name] = (bool(ok), now)


def _dead_members(members: list[str], ttl_s: float) -> list[str]:
    """TTL 内处于 dead 的成员清单(ok 记录清除 dead;过期视为无记录)。"""
    now = time.monotonic()
    dead: list[str] = []
    with _HEALTH_LOCK:
        for name in members:
            entry = _HEALTH.get(name)
            if entry is None:
                continue
            ok, ts = entry
            if not ok and (now - ts) < ttl_s:
                dead.append(name)
    return dead


#: 预检跳过原因常量(供调用方/测试对账)
PREFLIGHT_SKIP_BUDGET = "no_budget"
PREFLIGHT_SKIP_DEAD = "provider_dead"


def preflight_scan(cfg: Config, site_url: str) -> dict[str, Any] | None:
    """run_scan 爬取前的预检;发现系统性故障时抛中文 RuntimeError 跳过整站。

    检查顺序(全部零外呼):

    1. ``cfg.vlm_budget_precheck`` 为假 → 直接返回 None(逐字节旧行为);
    2. 无计量成员(纯本地扫描)→ 返回 None(零副作用,不开缓存库);
    3. 健康账本:计量成员 dead 且未过 TTL → 抛错(计数
       ``scan.skipped.provider_dead``),提示人工 vlmctl ping 诊断;
    4. 本地预算:当日余量 < 1 → 抛错(计数 ``scan.skipped.no_budget``);
       余量 < 计量成员数 × 5 → 仅中文警告。

    :param cfg: 配置。
    :param site_url: 站点 URL(仅用于日志/报错文案,不做任何请求)。
    :return: 预检上下文(含计量成员名,供评分阶段收尾回报健康);无需检查
        时返回 None。
    :raises RuntimeError: 应跳过本站(中文报错,批量池按站隔离)。
    """
    if not bool(getattr(cfg, "vlm_budget_precheck", True)):
        return None

    metered = metered_members(cfg)
    if not metered:
        return None

    raw_ttl = getattr(cfg, "vlm_health_ttl_s", 300)
    try:
        ttl_s = max(0.0, float(raw_ttl))  # 0 = dead 立即过期(合法值:关闭拦截)
    except (TypeError, ValueError):
        ttl_s = 300.0
    dead = _dead_members(metered, ttl_s)
    if dead:
        telemetry.inc("scan.skipped.provider_dead")
        raise RuntimeError(
            f"预检跳过站点 {site_url}:分类器成员 {', '.join(dead)} 在最近 "
            f"{int(ttl_s)} 秒内发生系统性识别失败(V10.24 中止语义的记账),"
            "大概率是提供方余额耗尽 / 密钥失效 / 网络中断。请先人工执行 "
            "`python -m netsentinel.vision.vlmctl ping 规格`(如 ping glm)"
            "诊断,恢复后自动放行;或等待健康 TTL 过期后由本站自行重试。"
            "(预检绝不自动 ping:外呼诊断只能人工显式发起,红线 20)"
        )

    from netsentinel.vision.vlm_cache import VlmCache

    cache = VlmCache(str(cfg.vlm_cache_db), daily_limit=int(cfg.vlm_daily_budget))
    try:
        state = cache.budget_state()
    finally:
        closer = getattr(cache, "close", None)
        if callable(closer):
            closer()
    remaining = max(0, int(state["limit"]) - int(state["used"]))
    telemetry.gauge("scan.budget.remaining", float(remaining))
    if remaining < 1:
        telemetry.inc("scan.skipped.no_budget")
        raise RuntimeError(
            f"预检跳过站点 {site_url}:今日 VLM 预算已耗尽"
            f"(vlm_daily_budget={state['limit']},已用 {state['used']},"
            f"本地一本账 {cfg.vlm_cache_db})。爬取阶段约需数分钟而评分必然"
            "全灭,不再空烧。可选:次日自动归零;确属纯缓存重扫(缓存命中"
            "不扣预算)可临时设 vlm_budget_precheck: false 放行本站。"
        )
    if remaining < len(metered) * _LOW_BUDGET_WARN_FACTOR:
        logger.warning(
            "预检提示:今日 VLM 预算余量偏小(%d,计量成员 %s);"
            "本站若中途耗尽将按 V10.24 诚信升级转人工复核",
            remaining,
            ",".join(metered),
        )
    return {"metered": metered}


def record_scan_outcome(meta: dict[str, Any] | None, ok: bool) -> None:
    """评分阶段收尾的健康回报(orchestrator 调用;meta 为预检返回上下文)。

    ``meta`` 为 None(未预检:注入路径/本地扫描/开关关闭)时零副作用。
    """
    if not meta:
        return
    members = [str(m) for m in (meta.get("metered") or [])]
    if members:
        record_scan_health(members, ok=ok)
