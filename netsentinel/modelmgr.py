# -*- coding: utf-8 -*-
"""A148 · modelmgr —— 模型管理 CLI(手动切换三入口之一,契约 §2/§3)。

子命令一览::

    python -m netsentinel.modelmgr status              # 活动模型/来源/时间/云密钥布尔/本地服务一行摘要
    python -m netsentinel.modelmgr probe               # 本地回环扫描表(端口/提供方/视觉模型数/模型前 5)
    python -m netsentinel.modelmgr list [provider]     # 模型目录 + 本地扫描发现(标"本地")
    python -m netsentinel.modelmgr switch SPEC [--force]  # 手动切换(switched_by=cli,下一条扫描生效)
    python -m netsentinel.modelmgr test [SPEC]         # 连通性测试(缺省测活动 spec)
    python -m netsentinel.modelmgr takeover            # 手动触发自动接管(A155)
    python -m netsentinel.modelmgr serve [--port N]    # 启动连接向导常驻服务(A154)并打印地址

安全红线(CONTRACTS-V8 §0 第 32–34 条,全部体现在代码路径上):

* **本地探测仅限回环**(红线 32):probe/list/status 的扫描只针对
  cfg.local_probe_ports 指定端口上的 127.0.0.1 OpenAI 兼容 /v1/models,
  超时受控、不扫外网、不扫全端口段;且都发生在用户显式敲下命令时;
* **密钥只进不显**(红线 33):本 CLI 不提供任何密钥输入;status 的云密钥
  只显示"已配置家数"布尔统计(经 security.keys.configured 惰性导入),
  绝不回显 cfg.vlm_api_keys、环境变量或密钥文件内容;
* **stub 必须明示**(红线 34):活动模型或切换目标为 "stub" 时,输出显式
  标注"离线桩,非模型判定"。

兄弟模块(A143 local_probe / A144 model_manager / A147 connectivity /
A154 setup.daemon / A155 pipeline.takeover / A65 model_catalog /
A70 security.keys)一律**惰性导入**:未就位时输出中文错误并以退出码 1
结束。``main`` 提供 scanner/manager/tester/takeover/cfg 五个注入缝,
测试与嵌入场景可注入同构 fake,不触网、不落盘。

退出码:0=成功;1=错误(依赖未就位/校验失败/连通失败);2=用法错误。
"""
from __future__ import annotations

import argparse
import dataclasses
import importlib
import logging
import os
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config

__all__ = ["main", "CliError", "UsageError"]

logger = logging.getLogger(__name__)

#: 单个子命令超过该秒数视为慢路径,记 WARNING 日志(可观测性:慢路径告警)
_SLOW_COMMAND_S = 1.0

#: probe/list 扫描本地服务的超时秒数(与 A143 LocalVisionScanner 缺省一致)
_SCAN_TIMEOUT_S = 1.5

#: status 一行摘要用的快速扫描超时(受控:状态命令不应久等)
_STATUS_SCAN_TIMEOUT_S = 0.5

#: probe 表"模型(前 N)"列显示的模型名个数
_PROBE_MODEL_PREVIEW = 5


class CliError(RuntimeError):
    """面向运营者的中文错误:main 捕获后打印到 stderr 并返回退出码 1。"""


class UsageError(CliError):
    """用法错误(缺子命令/未知命令/缺参数):退出码 2。"""


# ---------------------------------------------------------------------------
# 兄弟模块惰性导入(并行开发期未就位时给出中文错误,绝不硬依赖)
# ---------------------------------------------------------------------------

#: 兄弟模块 -> 负责人提示(错误消息里指明缺谁)
_SIBLING_HINTS: dict[str, str] = {
    "netsentinel.vision.local_probe": "A143 local_probe.py:LocalVisionScanner 本地视觉服务扫描",
    "netsentinel.vision.model_manager": "A144 model_manager.py:ModelManager 活动模型管理",
    "netsentinel.vision.connectivity": "A147 connectivity.py:test_connection 连通性测试",
    "netsentinel.setup.daemon": "A154 setup/daemon.py:ensure_setup_server 向导常驻服务",
    "netsentinel.pipeline.takeover": "A155 pipeline/takeover.py:takeover_once 自动接管",
    "netsentinel.vision.model_catalog": "A65 model_catalog.py:MODELS 模型目录",
    "netsentinel.security.keys": "A70 security/keys.py:configured 密钥配置体检",
}


def _try_import(dotted: str) -> Any:
    """惰性导入兄弟模块;任何导入失败都视为"未就位"返回 None(降级,不抛)。"""
    try:
        return importlib.import_module(dotted)
    except Exception as exc:  # noqa: BLE001 - 并行期 ImportError/SyntaxError 等一律降级
        logger.debug("兄弟模块 %s 未就位(忽略):%s", dotted, exc)
        return None


def _require(dotted: str, cmd: str) -> Any:
    """惰性导入并强制要求就位;缺失时抛中文 CliError(退出码 1)。"""
    module = _try_import(dotted)
    if module is None:
        hint = _SIBLING_HINTS.get(dotted, dotted)
        raise CliError(
            f"依赖模块未就位:{dotted}({hint});modelmgr {cmd} 需要它,"
            "请等该兄弟模块落地后再试"
        )
    return module


# ---------------------------------------------------------------------------
# 注入缝上下文与缺省构造(scanner/manager/tester/takeover 注入优先)
# ---------------------------------------------------------------------------


@dataclass
class _Ctx:
    """一次 CLI 调用的上下文:配置 + 注入缝(测试/嵌入场景)。"""

    cfg: Config
    command: str
    scanner: Any = None  # 同构 A143 LocalVisionScanner:有 scan() -> list[dict]
    manager: Any = None  # 同构 A144 ModelManager:get_active/status/set_active
    tester: Any = None  # callable(spec) -> {"ok","latency_ms","model","error?"}(A147)
    takeover: Any = None  # callable(cfg) -> {"action","spec?","wizard?","detail"}(A155)


def _resolve_manager(ctx: _Ctx) -> Any:
    """活动模型管理器:注入优先;缺省惰性构造 A144 ModelManager(路径取 cfg)。"""
    if ctx.manager is not None:
        return ctx.manager
    mod = _require("netsentinel.vision.model_manager", ctx.command)
    cls = getattr(mod, "ModelManager", None)
    if not callable(cls):
        raise CliError("model_manager 缺少 ModelManager(形态异常)")
    return cls(ctx.cfg.model_runtime_path)


def _resolve_scanner(ctx: _Ctx, timeout: float) -> Any:
    """本地扫描器:注入优先;缺省惰性构造 A143 LocalVisionScanner(仅回环端口)。"""
    if ctx.scanner is not None:
        return ctx.scanner
    mod = _require("netsentinel.vision.local_probe", ctx.command)
    cls = getattr(mod, "LocalVisionScanner", None)
    if not callable(cls):
        raise CliError("local_probe 缺少 LocalVisionScanner(形态异常)")
    ports = [str(p) for p in (ctx.cfg.local_probe_ports or [])]
    return cls(ports, timeout=timeout)


def _resolve_tester(ctx: _Ctx) -> Any:
    """连通性测试:注入优先;缺省惰性绑定 A147 test_connection(spec, cfg)。"""
    if ctx.tester is not None:
        return ctx.tester
    mod = _require("netsentinel.vision.connectivity", ctx.command)
    fn = getattr(mod, "test_connection", None)
    if not callable(fn):
        raise CliError("connectivity 缺少 test_connection(形态异常)")
    cfg = ctx.cfg
    return lambda spec: fn(spec, cfg)


def _scan_entries(ctx: _Ctx, timeout: float) -> tuple[list[dict], str | None]:
    """执行一次本地扫描并过滤出 dict 条目;降级时返回 ([], 中文说明)。

    仅被 status/list 这类"扫描是辅助信息"的命令使用——降级不致命;
    probe 的扫描是主任务,缺模块/异常须致命(直接走 _resolve_scanner + CliError)。
    """
    try:
        scanner = _resolve_scanner(ctx, timeout)
    except CliError as exc:
        return [], f"扫描不可用({exc})"
    try:
        raw = scanner.scan()
    except Exception as exc:  # noqa: BLE001 - 扫描失败对辅助命令不致命
        return [], f"扫描失败({exc})"
    return [e for e in (raw or []) if isinstance(e, dict)], None


# ---------------------------------------------------------------------------
# 终端表格渲染(中文宽度感知,与 vlmctl 同构的精简版)
# ---------------------------------------------------------------------------


def _disp_width(text: str) -> int:
    """按东亚宽度计显示宽度(全角/宽字符记 2,其余记 1)。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    """右侧补空格到指定显示宽度。"""
    return text + " " * max(0, width - _disp_width(text))


def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    """渲染等宽表格(两列间 2 空格;行尾去空格,便于脚本解析)。"""
    widths = [_disp_width(h) for h in headers]
    spans: list[list[int]] = []
    for row in rows:
        row_widths: list[int] = []
        for i, cell in enumerate(row):
            w = _disp_width(str(cell))
            row_widths.append(w)
            widths[i] = max(widths[i], w)
        spans.append(row_widths)
    lines = [
        "  ".join(_pad(h, widths[i]) for i, h in enumerate(headers)).rstrip(),
        "  ".join("-" * w for w in widths),
    ]
    for row, row_widths in zip(rows, spans):
        lines.append(
            "  ".join(
                str(cell) + " " * max(0, widths[i] - row_widths[i])
                for i, cell in enumerate(row)
            ).rstrip()
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 小工具:端口/主机端口/时间/键值拾取
# ---------------------------------------------------------------------------


def _port_of(entry: dict) -> str:
    """从扫描条目提取端口显示文本(优先 port 字段,缺省从 base_url 解析)。"""
    port = entry.get("port")
    if port is not None:
        return str(port)
    base = str(entry.get("base_url") or "")
    tail = base.rsplit(":", 1)[-1] if ":" in base else ""
    digits = "".join(ch for ch in tail if ch.isdigit())
    return digits or "?"


def _host_port_of(entry: dict) -> str:
    """从扫描条目提取 host:port 显示文本(如 127.0.0.1:11434)。"""
    base = str(entry.get("base_url") or "")
    hostport = base.split("://", 1)[-1].split("/", 1)[0]
    return hostport or f"127.0.0.1:{_port_of(entry)}"


def _fmt_time(value: Any) -> str:
    """切换时间的容错格式化:数值按时间戳,其余按原文,缺失记'未知'。"""
    if value is None:
        return "未知"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return str(value)
    return str(value)


def _pick(info: dict, *keys: str) -> Any:
    """从状态字典里按候选键序取第一个非空值(A144 字段名容错)。"""
    for key in keys:
        value = info.get(key)
        if value not in (None, ""):
            return value
    return None


def _is_stub(spec: str) -> bool:
    """spec 是否为离线桩(红线 34:输出必须明示"离线桩,非模型判定")。"""
    return str(spec).strip().lower() == "stub"


def _browser_allowed() -> bool:
    """serve 是否自动打开浏览器:NETSENTINEL_NO_BROWSER 显式关闭(与 A149 同语义)。"""
    raw = (os.environ.get("NETSENTINEL_NO_BROWSER") or "").strip().lower()
    return raw in ("", "0", "false", "no", "off")


# ---------------------------------------------------------------------------
# status:活动模型 / 云密钥布尔 / 本地服务一行摘要
# ---------------------------------------------------------------------------


def _keys_line(ctx: _Ctx) -> str:
    """云密钥一行:只出"已配置 N/M 家"布尔统计,绝不回显密钥值(红线 33)。"""
    keys_mod = _try_import("netsentinel.security.keys")
    if keys_mod is None or not callable(getattr(keys_mod, "configured", None)):
        return "云密钥:无法判定(security.keys 未就位;本行只显示布尔,绝不回显密钥,红线 33)"
    try:
        mapping = keys_mod.configured(ctx.cfg)
    except Exception as exc:  # noqa: BLE001 - A70 异常时降级为"无法判定"
        return f"云密钥:无法判定(configured 异常:{exc})"
    if not isinstance(mapping, dict):
        return "云密钥:无法判定(configured 返回形态异常)"
    names = [str(k) for k, v in mapping.items() if v is True]
    base = f"云密钥:已配置 {len(names)}/{len(mapping)} 家"
    if names:
        return f"{base}({'、'.join(names)})"
    return f"{base}(尚未配置任何云平台密钥;写密钥请走连接向导或 security.keys.set_key)"


def _local_summary(ctx: _Ctx) -> str:
    """本地服务一行摘要:现扫(快速超时),降级不致命。"""
    entries, degrade = _scan_entries(ctx, _STATUS_SCAN_TIMEOUT_S)
    ports = "/".join(str(p) for p in (ctx.cfg.local_probe_ports or [])) or "(未配置)"
    if degrade is not None:
        return f"本地服务:{degrade}(探测端口 {ports},仅 127.0.0.1 回环,红线 32)"
    online = [e for e in entries if e.get("ok")]
    if not online:
        return f"本地服务:未发现在线本地视觉服务(已探测端口 {ports},仅回环,红线 32)"
    parts = ";".join(
        f"{_host_port_of(e)}({e.get('provider') or '?'})视觉模型 {len(e.get('models') or [])} 个"
        for e in online
    )
    return f"本地服务:{len(online)} 个在线 —— {parts}"


def _cmd_status(args: argparse.Namespace, ctx: _Ctx) -> int:
    """status 子命令:活动模型/来源/时间 + 云密钥布尔 + 本地服务一行。"""
    manager = _resolve_manager(ctx)
    spec: str | None = None
    info: dict = {}
    get_active = getattr(manager, "get_active", None)
    if callable(get_active):
        try:
            active = get_active()
        except Exception as exc:  # noqa: BLE001 - 读活动模型失败属硬错误
            raise CliError(f"读取活动模型失败:{exc}") from exc
        spec = str(active) if active is not None else None
    status_fn = getattr(manager, "status", None)
    if callable(status_fn):
        try:
            raw = status_fn()
            if isinstance(raw, dict):
                info = raw
        except Exception as exc:  # noqa: BLE001 - status() 只是补充信息,异常即忽略
            logger.debug("manager.status() 异常,忽略:%s", exc)
    if spec is None:
        picked = _pick(info, "spec")
        spec = str(picked) if picked is not None else None

    if spec is None:
        print("[modelmgr] 活动模型:未设置(可运行 takeover 自动接管,或 switch SPEC 手动切换;全无模型时可用离线桩 stub)")
    else:
        by = _pick(info, "switched_by", "by", "source")
        at = _pick(info, "switched_at", "at", "time")
        line = f"[modelmgr] 活动模型:{spec}(来源:{str(by) if by is not None else '未知'},切换时间:{_fmt_time(at)})"
        if _is_stub(spec):
            line += " —— 离线桩,非模型判定(红线 34)"
        print(line)
    print("[modelmgr] " + _keys_line(ctx))
    print("[modelmgr] " + _local_summary(ctx))
    return 0


# ---------------------------------------------------------------------------
# probe:本地回环扫描表
# ---------------------------------------------------------------------------


def _cmd_probe(args: argparse.Namespace, ctx: _Ctx) -> int:
    """probe 子命令:端口/提供方/视觉模型数/模型列表前 5 的扫描表(仅回环)。"""
    scanner = _resolve_scanner(ctx, _SCAN_TIMEOUT_S)  # 缺模块即致命(退出码 1)
    ports = [str(p) for p in (ctx.cfg.local_probe_ports or [])]
    print(
        f"[modelmgr] 本地视觉服务探测(仅 127.0.0.1 回环,红线 32;"
        f"端口 {'/'.join(ports) or '(未配置)'},超时 {_SCAN_TIMEOUT_S}s)"
    )
    try:
        raw = scanner.scan()
    except Exception as exc:  # noqa: BLE001 - 扫描异常对 probe 是硬错误
        raise CliError(f"本地扫描失败:{exc}") from exc
    if not isinstance(raw, list):
        raise CliError(f"扫描返回形态异常(期望列表,得到 {type(raw).__name__})")
    entries = [e for e in raw if isinstance(e, dict)]
    online = [e for e in entries if e.get("ok")]
    if online:
        rows: list[list[str]] = []
        for entry in online:
            models = [str(m) for m in (entry.get("models") or [])]
            count_cell = f"{len(models)}(未过滤)" if entry.get("unfiltered") else str(len(models))
            shown = "、".join(models[:_PROBE_MODEL_PREVIEW])
            if len(models) > _PROBE_MODEL_PREVIEW:
                shown += "…"
            rows.append(
                [
                    _port_of(entry),
                    str(entry.get("provider") or "?"),
                    count_cell,
                    shown or "(无视觉模型)",
                ]
            )
        print(_render_table(["端口", "提供方", "视觉模型数", f"模型(前 {_PROBE_MODEL_PREVIEW})"], rows))
    else:
        print("[modelmgr] (未发现在线本地视觉服务)")
    print(f"[modelmgr] 探测完成:共 {len(ports)} 个端口,在线 {len(online)} 个;未在线端口不外呼、不重试")
    return 0


# ---------------------------------------------------------------------------
# list:模型目录(+本地扫描发现的追加,标"本地")
# ---------------------------------------------------------------------------


def _cmd_list(args: argparse.Namespace, ctx: _Ctx) -> int:
    """list 子命令:model_catalog.MODELS 为主,本地回环发现追加并标"本地"。"""
    catalog = _require("netsentinel.vision.model_catalog", "list")
    models_map = getattr(catalog, "MODELS", None)
    if not isinstance(models_map, dict):
        raise CliError("model_catalog.MODELS 形态异常(期望 dict,无法列出)")
    wanted = (str(args.provider).strip().lower() if args.provider else "") or None
    if wanted is not None and wanted not in {str(k).lower() for k in models_map}:
        raise CliError(f"未知提供方 '{args.provider}';可用:{', '.join(map(str, models_map)) or '(无)'}")
    catalog_map = {str(k): list(v or []) for k, v in models_map.items()}
    if wanted is not None:
        catalog_map = {k: v for k, v in catalog_map.items() if k.lower() == wanted}

    print('[modelmgr] 视觉模型目录(提示信息,以各平台官方文档为准;标"本地"者为本机回环扫描发现,红线 32)')
    entries, degrade = _scan_entries(ctx, _SCAN_TIMEOUT_S)  # 扫描是辅助:降级不致命
    found: dict[str, list[str]] = {}
    for entry in entries:
        if not entry.get("ok"):
            continue
        provider = str(entry.get("provider") or "未知")
        bucket = found.setdefault(provider, [])
        for model in (str(m) for m in (entry.get("models") or [])):
            if model not in bucket:
                bucket.append(model)
    if wanted is not None:  # 过滤视图下只保留同名提供方的本地发现
        found = {k: v for k, v in found.items() if k.lower() == wanted}

    appended = 0
    catalog_total = 0
    for name, infos in catalog_map.items():
        catalog_total += len(infos)
        print(f"{name}:")
        known: set[str] = set()
        for info in infos:
            model_id = str(getattr(info, "id", info))
            known.add(model_id.lower())
            tags = ", ".join(map(str, getattr(info, "tags", None) or ())) or "-"
            line = f"  {model_id} [{tags}]"
            note = str(getattr(info, "note", "") or "")
            if note:
                line += f"  {note}"
            print(line)
        for model in found.get(name, []):
            if model.lower() in known:  # 目录已有同名条目:不重复追加
                continue
            appended += 1
            known.add(model.lower())
            print(f"  (本地){model} —— 本地扫描发现")
    for scan_provider, models in found.items():
        if scan_provider in catalog_map or not models:
            continue  # 已按目录提供方处理过的跳过;其余(如"openai 兼容")单列
        print(f"{scan_provider}(本地服务):")
        for model in models:
            appended += 1
            print(f"  (本地){model} —— 本地扫描发现")
    if degrade is not None:
        print(f"[modelmgr] 本地扫描降级:{degrade};以上仅目录条目")
    print(f"[modelmgr] 共 {len(catalog_map)} 家提供方 / {catalog_total} 个目录模型;本地追加 {appended} 个")
    return 0


# ---------------------------------------------------------------------------
# switch / test / takeover / serve
# ---------------------------------------------------------------------------


def _cmd_switch(args: argparse.Namespace, ctx: _Ctx) -> int:
    """switch 子命令:ModelManager.set_active(switched_by='cli',validate=not force)。"""
    manager = _resolve_manager(ctx)
    spec = str(args.spec)
    print(f"[modelmgr] 切换目标:{spec}(switched_by=cli)")
    try:
        manager.set_active(spec, switched_by="cli", validate=not args.force)
    except CliError:
        raise
    except Exception as exc:  # noqa: BLE001 - A144 抛的中文 ValueError 原样透传
        raise CliError(str(exc) or f"切换失败({type(exc).__name__})") from exc
    if args.force:
        print("[modelmgr] --force:已跳过连通性测试(建议随后手动 'test' 复核一次)")
    elif not _is_stub(spec):  # A144 对 stub 免校验,不冒称"测试通过"
        print("[modelmgr] 连通性测试通过(经 A147;未通过会报错并以退出码 1 结束)")
    if _is_stub(spec):
        print("[modelmgr] 注意:stub 为离线桩,非模型判定(红线 34)")
    print(f"[modelmgr] 已切换:{spec}(下一条扫描生效)")
    telemetry.inc("modelmgr.switch")
    return 0


def _cmd_test(args: argparse.Namespace, ctx: _Ctx) -> int:
    """test 子命令:连通性测试;缺省测当前活动 spec。"""
    spec = str(args.spec).strip() if args.spec else ""
    if not spec:
        manager = _resolve_manager(ctx)
        get_active = getattr(manager, "get_active", None)
        if not callable(get_active):
            raise CliError("manager 缺少 get_active(形态异常)")
        try:
            active = get_active()
        except Exception as exc:  # noqa: BLE001
            raise CliError(f"读取活动模型失败:{exc}") from exc
        if not active:
            raise CliError("当前无活动模型:请先 'switch SPEC'、运行 'takeover',或直接 'test SPEC'")
        spec = str(active)

    tester = _resolve_tester(ctx)
    print(f"[modelmgr] 测试目标:{spec}")
    try:
        raw = tester(spec)
    except CliError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CliError(f"连通性测试异常:{exc}") from exc
    if not isinstance(raw, dict):
        raise CliError(f"连通性测试返回形态异常(期望字典,得到 {type(raw).__name__})")
    latency = raw.get("latency_ms")
    latency_text = (
        f"{float(latency):.1f} ms"
        if isinstance(latency, (int, float)) and not isinstance(latency, bool)
        else "(未测量)"
    )
    print(f"[modelmgr] 延迟:{latency_text}  模型回显:{str(raw.get('model') or '(未回显)')}")
    if raw.get("ok"):
        print("[modelmgr] 结论:✅ 连通正常")
        telemetry.inc("modelmgr.test")
        return 0
    print(f"[modelmgr] 结论:❌ 连通失败:{raw.get('error') or '未给出原因'}")
    return 1


def _cmd_takeover(args: argparse.Namespace, ctx: _Ctx) -> int:
    """takeover 子命令:手动触发 A155 takeover_once 并打印中文结果。"""
    print("[modelmgr] 手动触发自动接管 takeover_once(仅探测本机回环,红线 32)...")
    if ctx.takeover is not None:
        try:
            result = ctx.takeover(ctx.cfg)
        except Exception as exc:  # noqa: BLE001
            raise CliError(f"takeover 执行失败:{exc}") from exc
    else:
        mod = _require("netsentinel.pipeline.takeover", "takeover")
        fn = getattr(mod, "takeover_once", None)
        if not callable(fn):
            raise CliError("pipeline.takeover 缺少 takeover_once(形态异常)")
        try:
            result = fn(ctx.cfg)
        except Exception as exc:  # noqa: BLE001
            raise CliError(f"takeover 执行失败:{exc}") from exc
    if not isinstance(result, dict):
        raise CliError(f"takeover 返回形态异常(期望字典,得到 {type(result).__name__})")
    print(f"[modelmgr] 动作:{result.get('action') or 'unknown'}")
    if result.get("spec"):
        print(f"[modelmgr] 接管模型:{result['spec']}")
    if result.get("wizard"):
        print(f"[modelmgr] 已触发连接向导:{result['wizard']}")
    detail = result.get("detail")
    if detail:
        print(f"[modelmgr] 详情:{detail}")
    telemetry.inc("modelmgr.takeover")
    return 0


def _cmd_serve(args: argparse.Namespace, ctx: _Ctx) -> int:
    """serve 子命令:起 A154 向导常驻服务;NO_BROWSER 时不自动开浏览器。"""
    cfg = ctx.cfg
    if args.port is not None:
        print(f"[modelmgr] --port {args.port}(覆盖配置 setup_port={cfg.setup_port})")
        try:
            cfg = dataclasses.replace(cfg, setup_port=int(args.port))
        except TypeError as exc:  # pragma: no cover - Config 非数据类时的兜底
            raise CliError(f"覆盖 setup_port 失败:{exc}") from exc
    daemon = _require("netsentinel.setup.daemon", "serve")
    fn = getattr(daemon, "ensure_setup_server", None)
    if not callable(fn):
        raise CliError("setup.daemon 缺少 ensure_setup_server(形态异常)")
    open_browser = _browser_allowed()
    print("[modelmgr] 启动连接向导常驻服务(A154;向导页即手动切换器)...")
    try:
        url = fn(cfg, open_browser=open_browser)
    except Exception as exc:  # noqa: BLE001
        raise CliError(f"向导服务器启动失败:{exc}") from exc
    if not url:
        raise CliError("向导服务器未能启动(ensure_setup_server 返回空;详见 A154 日志)")
    print(f"[modelmgr] 向导地址:{url}")
    if open_browser:
        print("[modelmgr] 浏览器:已请求自动打开(设 NETSENTINEL_NO_BROWSER=1 可改为仅打印地址)")
    else:
        print("[modelmgr] 检测到 NETSENTINEL_NO_BROWSER:不自动打开浏览器,请手动访问上述地址")
    telemetry.inc("modelmgr.serve")
    return 0


# ---------------------------------------------------------------------------
# argparse 与入口
# ---------------------------------------------------------------------------


class _ChineseParser(argparse.ArgumentParser):
    """参数错误转 UsageError(main 统一转中文退出码 2,不让 argparse 打印英文用法)。"""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(
            f"参数错误:{message};可用 'python -m netsentinel.modelmgr --help' 查看用法"
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = _ChineseParser(
        prog="modelmgr",
        description=(
            "NetSentinel 模型管理 CLI(A148):status/probe/list/switch/test/takeover/serve;"
            "本地探测仅 127.0.0.1 回环(红线 32);密钥只显示布尔(红线 33)"
        ),
        epilog=(
            "示例:\n"
            "  python -m netsentinel.modelmgr status\n"
            "  python -m netsentinel.modelmgr probe\n"
            "  python -m netsentinel.modelmgr list\n"
            "  python -m netsentinel.modelmgr list glm\n"
            "  python -m netsentinel.modelmgr switch ollama:llava\n"
            "  python -m netsentinel.modelmgr switch glm:glm-4.5v --force\n"
            "  python -m netsentinel.modelmgr test\n"
            "  python -m netsentinel.modelmgr takeover\n"
            "  python -m netsentinel.modelmgr serve --port 8766\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default=None, help="配置文件路径(缺省读 ./config.yaml,不存在则用默认配置)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="命令")

    p_status = sub.add_parser("status", help="当前状态:活动模型/云密钥布尔/本地服务一行摘要")
    p_status.set_defaults(func=_cmd_status)

    p_probe = sub.add_parser("probe", help="探测本机回环本地视觉服务(端口表,红线 32)")
    p_probe.set_defaults(func=_cmd_probe)

    p_list = sub.add_parser("list", help="视觉模型目录(含本地扫描发现,标'本地')")
    p_list.add_argument("provider", nargs="?", default=None, help="只看某提供方,如 glm / ollama")
    p_list.set_defaults(func=_cmd_list)

    p_switch = sub.add_parser("switch", help="切换活动模型(switched_by=cli;默认先做连通性测试)")
    p_switch.add_argument("spec", help="目标 spec,如 ollama:llava、glm:glm-4.5v、stub")
    p_switch.add_argument("--force", action="store_true", help="跳过连通性测试直接切换")
    p_switch.set_defaults(func=_cmd_switch)

    p_test = sub.add_parser("test", help="连通性测试(缺省测当前活动模型)")
    p_test.add_argument("spec", nargs="?", default=None, help="目标 spec(缺省用活动模型)")
    p_test.set_defaults(func=_cmd_test)

    p_takeover = sub.add_parser("takeover", help="手动触发自动接管 takeover_once(A155)")
    p_takeover.set_defaults(func=_cmd_takeover)

    p_serve = sub.add_parser("serve", help="启动连接向导常驻服务(A154)并打印地址")
    p_serve.add_argument("--port", type=int, default=None, help="向导端口(缺省 cfg.setup_port)")
    p_serve.set_defaults(func=_cmd_serve)
    return parser


def _load_config_or_default(path: str | None) -> Config:
    """读取配置文件;缺省读 ./config.yaml,不存在/模块未就位则用默认 Config。"""
    config_mod = _try_import("netsentinel.config")
    if config_mod is None or not callable(getattr(config_mod, "load_config", None)):
        return Config()
    try:
        return config_mod.load_config(path)
    except Exception as exc:  # noqa: BLE001 - 配置坏文件要有明确中文提示
        raise CliError(f"读取配置文件失败({'./config.yaml' if path is None else path}):{exc}") from exc


def main(
    argv: list[str] | None = None,
    *,
    scanner: Any = None,
    manager: Any = None,
    tester: Any = None,
    takeover: Any = None,
    cfg: Config | None = None,
) -> int:
    """modelmgr 入口:解析参数并分发子命令;中文输出,退出码 0/1/2。

    注入缝(便于测试/嵌入,不触网不落盘):

    * ``scanner``:同构 A143 ``LocalVisionScanner``(有 ``scan()``);
    * ``manager``:同构 A144 ``ModelManager``(get_active/status/set_active);
    * ``tester``:callable(spec) -> ``{"ok","latency_ms","model","error?"}``(A147);
    * ``takeover``:callable(cfg) -> ``{"action","spec?","wizard?","detail"}``(A155);
    * ``cfg``:直接注入配置(缺省按 ``--config`` / ``./config.yaml`` 加载)。

    退出码:0=成功;1=错误(依赖未就位/校验失败/连通失败);2=用法错误。
    每个子命令的耗时记入 ``telemetry``(``modelmgr.<命令>``),错误计数
    ``modelmgr.errors`` / 用法错误计数 ``modelmgr.usage_errors``;遥测只存
    名称与数字,不含参数、密钥或 URL。
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
        if cfg is None:
            cfg = _load_config_or_default(getattr(args, "config", None))
        command = str(getattr(args, "command", "") or "unknown")
        ctx = _Ctx(
            cfg=cfg,
            command=command,
            scanner=scanner,
            manager=manager,
            tester=tester,
            takeover=takeover,
        )
        handler = getattr(args, "func", None)
        if handler is None:  # pragma: no cover - required=True 已保证有子命令
            raise UsageError("缺少子命令;可用 status/probe/list/switch/test/takeover/serve")
        started = time.perf_counter()
        try:
            return int(handler(args, ctx))
        finally:
            elapsed = time.perf_counter() - started
            telemetry.observe(f"modelmgr.{command}", elapsed)
            if elapsed > _SLOW_COMMAND_S:
                logger.warning(
                    "modelmgr 子命令 %s 耗时 %.2fs,超过 %.1fs 慢路径阈值",
                    command,
                    elapsed,
                    _SLOW_COMMAND_S,
                )
    except UsageError as exc:
        telemetry.inc("modelmgr.usage_errors")
        print(f"[modelmgr] 用法错误:{exc}", file=sys.stderr)
        return 2
    except CliError as exc:
        telemetry.inc("modelmgr.errors")
        print(f"[modelmgr] 错误:{exc}", file=sys.stderr)
        return 1
    except SystemExit as exc:  # --help / -h 等正常退出路径
        code = exc.code
        return int(code) if isinstance(code, int) else 0
    except KeyboardInterrupt:  # pragma: no cover - 人工中断
        print("[modelmgr] 已被人工中断", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 兜底:任何异常都转中文退出码 1
        telemetry.inc("modelmgr.errors")
        logger.debug("modelmgr 未预期异常", exc_info=True)
        print(f"[modelmgr] 未预期的错误({type(exc).__name__}):{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - python -m netsentinel.modelmgr
    sys.exit(main())
