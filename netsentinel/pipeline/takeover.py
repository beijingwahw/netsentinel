"""A155:首次运行视觉模型自动接管(依据 CONTRACTS-V8.md §2 / §3 A155 条目)。

任意 CLI 入口首次运行时调用 :func:`takeover_once`,按四级优先级把"当前可用
的视觉模型"接为活动模型,让用户开箱即用、无需手工配置:

1. **kept(已有活动 spec)**:A144 ``ModelManager.get_active()`` 非空 →
   沿用已连接模型,**不再重探**(零网络、零打扰);
2. **local(本地视觉服务)**:A143 ``LocalVisionScanner`` 扫描
   ``cfg.local_probe_ports``(端口→提供方映射由 A143 负责,本模块直接采信
   扫描条目的 ``provider`` 字段),任一 ``ok`` 条目含视觉模型(A150 判定,
   缺席按"缺席全返回"降级)→ 取首个视觉模型
   ``set_active("ollama:llava:13b" 之类, switched_by="takeover",
   validate=False)``。**validate=False 的理由(红线 32)**:本地服务与模型
   刚在扫描中被证实存在,再做一次连通测试属于重复探测,而契约规定探测动作
   仅发生在首次初始化与用户显式触发时;
3. **cloud(云端密钥已配)**:``security.keys.configured(cfg)`` 任一 True →
   取其一(按目录顺序首家)``set_active(provider, switched_by="takeover",
   validate=False)``——spec 只写提供方名,模型由
   ``providers.resolve`` 按目录默认值解析。接管**不外呼、不做连通测试**
   (云端外呼仍受 vlm_online+密钥双条件与 VLM 预算约束,红线 34——设置
   spec 不放宽任何既有纪律);
4. **wizard(全无)**:惰性 A149 ``ensure_setup(cfg)`` 拉起连接向导,
   返回 ``{"action": "wizard", "wizard": url|None}``。

行为纪律:

- **会话幂等**:模块级标记,同进程二次调用直接返回
  ``{"action": "already", "detail": "本会话已完成接管..."}``,零副作用
  (不重探、不写审计、不计数);``disabled``(配置关闭)与 ``error``
  (兄弟缺席/异常)不置位——前者根本没接管,后者允许兄弟落地后重试;
- **绝不抛出**:任何兄弟模块缺席(A143/A144/A149 未就位)→
  ``{"action": "error", "detail": 中文原因}``;任何运行期异常同样收敛为
  error 结果(集成接线要求"惰性,异常只告警不阻断");
- **每条决策路径**写一行审计 ``log_event("takeover", action=..., spec=...)``
  (惰性 ``JsonlAuditLogger(cfg.audit_path)``,写失败仅告警不阻断)并计
  ``telemetry.inc(f"takeover.{action}")``;
- **降级口径与 A149 一致**:扫描异常 / 密钥体检异常按"该来源无"降级并记
  WARNING,继续走下一级;只有"兄弟模块缺席"与 set_active 失败才落 error。

密钥纪律(红线 33):本模块只经 ``security.keys.configured`` 问"是否已配",
绝不触碰密钥本体;审计与遥测只记动作名 / spec / 布尔,零敏感内容。

顶层只 import 冻结模块(contracts / telemetry);A143/A144/A149/A150/
providers/keys/logging_util 一律惰性,并行开发期缺席可测。零外呼
(本地探测全部委托 A143,且仅回环受限端口,红线 32)。
"""
from __future__ import annotations

import logging
import threading
from importlib import import_module
from typing import Any, Callable

from netsentinel import telemetry
from netsentinel.contracts import Config

__all__ = ["takeover_once", "reset_session"]

logger = logging.getLogger(__name__)

#: 审计事件名(``JsonlAuditLogger.log_event`` 的 event 字段)
AUDIT_EVENT = "takeover"

#: 本模块写入活动模型时的来源标记(契约 §2 登记来源:takeover/wizard/cli/rest)
SWITCHED_BY = "takeover"

#: 会话幂等标记:True = 本进程已完成一次真实接管(kept/local/cloud/wizard)
_session_done: bool = False

#: 会话标记读写锁(检查与置位分开持锁:决策本身不持锁,避免扫描阻塞其他线程)
_STATE_LOCK = threading.Lock()


def reset_session() -> None:
    """清空会话幂等标记(仅测试 / 用户显式手动重触发使用)。

    CLI ``python -m netsentinel.modelmgr takeover`` 需要强制重跑时可用;
    正常入口不应调用——幂等的目的就是"会话内只打扰一次"。
    """
    global _session_done
    with _STATE_LOCK:
        _session_done = False


# ---------------------------------------------------------------------------
# 惰性兄弟模块导入(并行期允许缺席;缺席 = 抛中文错误由调用方收敛为 error)
# ---------------------------------------------------------------------------


def _try_import(dotted: str) -> Any:
    """惰性导入兄弟模块;任何导入失败都视为"未就位"返回 None(不抛)。"""
    try:
        return import_module(dotted)
    except Exception as exc:  # noqa: BLE001 - 并行期 ImportError/SyntaxError 等一律降级
        logger.debug("兄弟模块 %s 未就位(忽略):%s", dotted, exc)
        return None


def _resolve_manager(cfg: Config, manager: Any) -> Any:
    """取活动模型管理器:注入优先,缺省惰性 A144 ``ModelManager(path)``。"""
    if manager is not None:
        return manager
    mod = _try_import("netsentinel.vision.model_manager")
    cls = getattr(mod, "ModelManager", None) if mod else None
    if cls is None:
        raise RuntimeError(
            "活动模型管理器未就位:netsentinel.vision.model_manager(A144)缺席,"
            "无法读取或写入活动模型"
        )
    try:
        return cls(getattr(cfg, "model_runtime_path", "data/model_runtime.json"))
    except Exception as exc:  # noqa: BLE001 - 构造失败同样按兄弟异常收敛
        raise RuntimeError(f"构造活动模型管理器失败:{exc}") from exc


def _load_vision_filter() -> Callable[[str], bool] | None:
    """惰性取 A150 ``is_vision_model``;未就位返回 None(调用方按"缺席全返回"降级)。"""
    mod = _try_import("netsentinel.vision.capability")
    fn = getattr(mod, "is_vision_model", None) if mod else None
    return fn if callable(fn) else None


def _provider_known(provider: str) -> bool:
    """提供方是否在统一目录中(惰性 A61 ``providers.PROVIDERS``)。

    A143 对未映射端口给出的 "openai 兼容" 之类不在目录内——这类条目跳过
    (spec 无法通过 A144 的语法校验);目录模块本身缺席时无从校验,返回
    True 放行,由 set_active 的 parse_spec 兜底把关。
    """
    mod = _try_import("netsentinel.vision.providers")
    catalog = getattr(mod, "PROVIDERS", None) if mod else None
    if not isinstance(catalog, dict):
        return True
    return provider in catalog


def _model_names(models: Any) -> list[str]:
    """把扫描结果的模型列表规整为名字列表:兼容 str / {"id"|"name"} / 属性对象。"""
    names: list[str] = []
    for item in models or []:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = str(item.get("id") or item.get("name") or "")
        else:
            name = str(getattr(item, "id", None) or getattr(item, "name", None) or "")
        if name.strip():
            names.append(name.strip())
    return names


def _load_keys_configured() -> Callable[[Config], dict] | None:
    """惰性取 ``security.keys.configured``;模块缺席返回 None(调用方报 error)。"""
    mod = _try_import("netsentinel.security.keys")
    fn = getattr(mod, "configured", None) if mod else None
    return fn if callable(fn) else None


def _load_audit_logger(cfg: Config) -> Any:
    """惰性构造 ``JsonlAuditLogger(cfg.audit_path)``;缺席 / 构造失败返回 None。"""
    mod = _try_import("netsentinel.logging_util")
    cls = getattr(mod, "JsonlAuditLogger", None) if mod else None
    if cls is None:
        logger.debug("审计日志器未就位(netsentinel.logging_util 缺席),跳过接管审计")
        return None
    try:
        return cls(getattr(cfg, "audit_path", "data/audit.jsonl"))
    except Exception as exc:  # noqa: BLE001 - 审计不可用不阻断接管
        logger.warning("构造审计日志器失败,本次跳过接管审计:%s", exc)
        return None


# ---------------------------------------------------------------------------
# 四级决策:kept → local → cloud → wizard
# ---------------------------------------------------------------------------


def _local_hit(cfg: Config, scanner: Any) -> dict[str, str] | None:
    """本地扫描:返回 ``{"provider", "model"}``(首个 ok 且含视觉模型的条目)。

    - ``scanner`` 注入优先;缺省惰性构造 A143
      ``LocalVisionScanner(cfg.local_probe_ports)``(回环受限端口与
      端口→提供方映射均由 A143 保证,红线 32;本模块不自行发网);
    - A143 缺席 → 中文 RuntimeError(由外层收敛为 error,不抛出到调用方);
    - ``scan()`` 运行期异常 → 按"未发现"降级并记 WARNING,继续走云端一级
      (与 A149 的容错口径一致);
    - 视觉判定委托 A150;A150 缺席时按"缺席全返回"——任意非空模型名都算
      候选(宁可多试一次,不打扰用户去向导);
    - 条目 ``provider`` 不在统一目录中(如未映射端口的 "openai 兼容")→
      跳过该条目继续找。
    """
    if scanner is None:
        probe_mod = _try_import("netsentinel.vision.local_probe")
        scanner_cls = getattr(probe_mod, "LocalVisionScanner", None) if probe_mod else None
        if scanner_cls is None:
            raise RuntimeError(
                "本地视觉扫描器未就位:netsentinel.vision.local_probe(A143)缺席,"
                "无法探测本机视觉服务"
            )
        try:
            ports = [str(p) for p in (getattr(cfg, "local_probe_ports", None) or [])]
            scanner = scanner_cls(ports)
        except Exception as exc:  # noqa: BLE001 - 构造失败按兄弟异常收敛为 error
            raise RuntimeError(f"构造本地视觉扫描器失败:{exc}") from exc
    try:
        results = scanner.scan()
    except Exception as exc:  # noqa: BLE001 - 扫描失败按"未发现"降级,不阻断后续来源
        logger.warning("本地视觉服务扫描失败(按未发现处理):%s", exc)
        return None

    is_vision = _load_vision_filter()
    if is_vision is None:
        logger.debug("A150 capability 未就位,本地模型不做视觉过滤(缺席全返回)")
    for entry in results or []:
        if not isinstance(entry, dict) or not entry.get("ok"):
            continue
        provider = str(entry.get("provider") or "").strip()
        if not provider:
            logger.debug("跳过缺少 provider 字段的本地扫描条目:%r", entry)
            continue
        if not _provider_known(provider):
            logger.info(
                "跳过不在统一目录中的本地提供方:%r(端口未映射,无法构造合法 spec)",
                provider,
            )
            continue
        for name in _model_names(entry.get("models")):
            if is_vision is None or is_vision(name):
                return {"provider": provider, "model": name}
    return None


def _configured_cloud_provider(cfg: Config) -> str | None:
    """云端一级:返回首家"密钥已配"的提供方名(目录顺序);全无返回 None。

    - ``keys.configured`` 惰性;模块缺席 → RuntimeError(外层收敛 error);
    - 体检运行期异常 → 按"未配置"降级并记 WARNING(同 A149 口径);
    - 只问"是否已配"(布尔),绝不触碰密钥本体(红线 33)。
    """
    fn = _load_keys_configured()
    if fn is None:
        raise RuntimeError(
            "密钥体检模块未就位:netsentinel.security.keys 缺席,无法判断云端密钥"
        )
    try:
        table = fn(cfg)
    except Exception as exc:  # noqa: BLE001 - 体检异常按未配置降级
        logger.warning("云密钥配置体检失败(按未配置处理):%s", exc)
        return None
    if not isinstance(table, dict):
        logger.warning("keys.configured 返回形态异常(按未配置处理):%r", type(table).__name__)
        return None
    for provider, configured in table.items():
        if configured and _provider_known(str(provider)):
            return str(provider).strip()
    return None


def _ensure_setup(cfg: Config, ensure: Any) -> str | None:
    """向导一级:惰性 A149 ``ensure_setup(cfg)``;缺席 → 中文 RuntimeError。

    返回值规整为 ``str | None``(空串 / 非字符串视为 None = 未能启动);
    运行期异常同样降级为 None(向导起不来不是接管失败的理由,结果仍是
    wizard,只是地址为空,详情里给出手动入口)。
    """
    if ensure is not None:
        fn = ensure
    else:
        mod = _try_import("netsentinel.setup.trigger")
        fn = getattr(mod, "ensure_setup", None) if mod else None
        if fn is None:
            raise RuntimeError(
                "连接向导触发器未就位:netsentinel.setup.trigger(A149)缺席,"
                "无法启动连接向导"
            )
    try:
        url = fn(cfg)
    except Exception as exc:  # noqa: BLE001 - 向导启动失败降级为无地址
        logger.warning("连接向导启动失败(返回空地址):%s", exc)
        return None
    if isinstance(url, str) and url.strip():
        return url.strip()
    return None


def _switch(mgr: Any, spec: str) -> None:
    """``set_active(spec, switched_by="takeover", validate=False)`` 落盘。

    validate=False(红线 32):本地提供方刚在扫描中被证实存在;云端提供方
    在接管阶段零外呼(vlm_online/预算纪律在真正调用时才生效,红线 34)。
    set_active 失败(语法校验 / 写盘异常等)→ 中文 RuntimeError 收敛为
    error 结果,绝不抛出到调用方。
    """
    try:
        mgr.set_active(spec, switched_by=SWITCHED_BY, validate=False)
    except Exception as exc:  # noqa: BLE001 - 切换失败收敛为 error,不阻断入口
        raise RuntimeError(f"设置活动模型失败:{spec}({exc})") from exc


def _decide(
    cfg: Config,
    *,
    scanner: Any,
    manager: Any,
    ensure: Any,
) -> dict[str, Any]:
    """四级决策主体(可能抛中文异常,由 :func:`takeover_once` 收敛为 error)。"""
    mgr = _resolve_manager(cfg, manager)

    # ① 已有活动 spec → 沿用,不重探
    active = mgr.get_active()
    if isinstance(active, str) and active.strip():
        spec = active.strip()
        logger.info("已有活动模型,沿用不重探:%s", spec)
        return {"action": "kept", "spec": spec, "detail": f"沿用已连接模型:{spec}"}

    # ② 本地扫描 → 首个视觉模型接管
    hit = _local_hit(cfg, scanner)
    if hit is not None:
        spec = f"{hit['provider']}:{hit['model']}"
        _switch(mgr, spec)
        logger.info("本地视觉模型已接管:%s", spec)
        return {"action": "local", "spec": spec, "detail": f"本地视觉模型已接管:{spec}"}

    # ③ 云端密钥已配 → 取其一(目录默认模型)
    provider = _configured_cloud_provider(cfg)
    if provider:
        _switch(mgr, provider)
        logger.info("云端视觉模型已接管:%s(目录默认模型)", provider)
        return {
            "action": "cloud",
            "spec": provider,
            "detail": f"云端视觉模型已接管:{provider}(使用目录默认模型)",
        }

    # ④ 全无 → 连接向导
    url = _ensure_setup(cfg, ensure)
    if url:
        return {
            "action": "wizard",
            "wizard": url,
            "detail": f"未发现可用视觉模型,已启动连接向导:{url}",
        }
    return {
        "action": "wizard",
        "wizard": None,
        "detail": (
            "未发现可用视觉模型,连接向导未能启动;"
            "可运行 python -m netsentinel.modelmgr serve 手动打开"
        ),
    }


# ---------------------------------------------------------------------------
# 审计 + 遥测(每条决策路径一行;already 零副作用不记)
# ---------------------------------------------------------------------------


def _record(cfg: Config, result: dict[str, Any]) -> None:
    """对一条决策结果计数并写审计;两者任何失败都只告警,绝不阻断。"""
    action = str(result.get("action") or "unknown")
    try:
        telemetry.inc(f"takeover.{action}")
    except Exception as exc:  # noqa: BLE001 - 遥测不可用不影响接管
        logger.warning("接管遥测计数失败(忽略):%s", exc)
    audit = _load_audit_logger(cfg)
    if audit is None:
        return
    fields: dict[str, Any] = {"action": action}
    if result.get("spec"):
        fields["spec"] = result["spec"]
    if "wizard" in result:
        fields["wizard"] = result.get("wizard")
    if action == "error":
        fields["detail"] = str(result.get("detail") or "")
    try:
        audit.log_event(AUDIT_EVENT, **fields)
    except Exception as exc:  # noqa: BLE001 - 审计写失败不阻断接管
        logger.warning("接管审计写入失败(不阻断):%s", exc)


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def takeover_once(
    cfg: Config,
    *,
    scanner: Any = None,
    manager: Any = None,
    ensure: Any = None,
    tester: Any = None,
) -> dict[str, Any]:
    """执行一次自动接管(会话幂等,绝不抛出),返回决策结果字典。

    参数(全部可选,测试 / 集成注入缝):

    - ``scanner``:本地扫描器(协议 ``scan() -> [{"provider","models","ok"}]``);
      缺省惰性 A143 ``LocalVisionScanner(cfg.local_probe_ports)``;
    - ``manager``:活动模型管理器(协议 ``get_active() / set_active(spec, *,
      switched_by, validate)``);缺省惰性 A144
      ``ModelManager(cfg.model_runtime_path)``;
    - ``ensure``:向导触发器(协议 ``ensure(cfg) -> url|None``);
      缺省惰性 A149 ``setup.trigger.ensure_setup``;
    - ``tester``:连通性测试器(**保留缝,当前策略下永不被调用**)——
      本地路径 ``validate=False``(扫描已证实存在,不做二次探测,红线 32),
      云端路径接管阶段零外呼(红线 34);注入仅为可断言"不触发连通测试"。

    返回(恒为 dict,``detail`` 恒中文):

    - ``{"action": "disabled", "detail": ...}``:``cfg.takeover_auto=False``;
    - ``{"action": "already", "detail": ...}``:本会话已接管过(零副作用);
    - ``{"action": "kept", "spec": ..., "detail": ...}``:沿用已连接模型;
    - ``{"action": "local", "spec": "提供方:模型", "detail": ...}``;
    - ``{"action": "cloud", "spec": "提供方", "detail": ...}``;
    - ``{"action": "wizard", "wizard": url|None, "detail": ...}``;
    - ``{"action": "error", "detail": ...}``:兄弟缺席 / 执行异常(不抛)。

    每条决策路径(除 already)写一行 ``log_event("takeover", action=...,
    spec=...)``(惰性 JsonlAuditLogger,写失败仅告警)并计
    ``telemetry.inc(f"takeover.{action}")``。``disabled`` 与 ``error``
    不置会话标记:前者没有接管,后者允许兄弟模块落地后重试。
    """
    global _session_done
    # 0) 配置开关(最高优先:功能关闭时零探测、零副作用,也不占会话名额)
    if not bool(getattr(cfg, "takeover_auto", True)):
        result = {
            "action": "disabled",
            "detail": "配置关闭:takeover_auto=False,跳过自动接管",
        }
        _record(cfg, result)
        return result

    # 会话幂等:已接过 → 直接说明,不重探、不写审计、不计数
    with _STATE_LOCK:
        if _session_done:
            return {
                "action": "already",
                "detail": "本会话已完成接管,不再重复执行",
            }

    try:
        result = _decide(cfg, scanner=scanner, manager=manager, ensure=ensure)
    except Exception as exc:  # noqa: BLE001 - 入口级兜底:接管绝不抛出(只告警)
        logger.warning("自动接管执行失败(收敛为 error 结果):%s", exc)
        result = {"action": "error", "detail": f"自动接管执行失败:{exc}"}

    if result.get("action") != "error":
        with _STATE_LOCK:
            _session_done = True
    _record(cfg, result)
    return result
