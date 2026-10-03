"""A149:连接向导首启触发(依据 CONTRACTS-V8.md §3 A149 条目 / §0 红线 32)。

首启语义:用户已经"有模型可用"时绝不打扰;全无时才拉起连接向导
(A145/A154 自托管页面),并按需弹浏览器。本模块只回答两个问题:

- :func:`has_any_model`:三个来源依次探测——

  1. **活动模型运行时**(A144 ``ModelManager(cfg.model_runtime_path).get_active()``
     非空):用户此前已经接过模型(接管/向导/CLI/REST 任一入口);
  2. **云密钥已配**(:func:`netsentinel.security.keys.configured` 任一 True):
     有密钥即可构建云端分类器;
  3. **本地扫描命中**(注入 ``scanner`` 或缺省惰性 A143
     ``LocalVisionScanner``):任一 ``ok`` 条目含视觉模型(A150 判定,惰性)。

  三来源任一命中即 ``True``;各来源独立容错,一处异常不拖累其余
  (异常按"该来源无"降级并记日志)。

- :func:`ensure_setup`:需要时(全无模型)启动向导服务器——

  - ``server_factory`` 可注入(测试);缺省惰性走 A154
    ``setup.daemon.ensure_setup_server`` 单例,A154 缺席再退 A145
    ``setup.server.serve``;浏览器开/合决策统一收在本模块,对下层一律
    ``open_browser=False``,避免双开;
  - 本模块自身亦幂等(进程内一次):二次调用直接复用已启动地址,不再
    起服务、不再计数、不再打印;
  - 弹窗三态优先级:**参数 > 环境变量 ``NETSENTINEL_NO_BROWSER`` > 配置
    ``onboarding_auto_open``**;不弹时打印中文提示与地址
    (``未检测到视觉模型,连接向导已启动:{url}``);
  - 每次真实首启记 ``telemetry.inc("setup.wizard_shown")``。

红线协同:本地探测只经 A143 回环受限端口(红线 32),本模块不自行发网;
密钥只经 ``security.keys`` 判"是否已配",绝不回显本体(红线 33);
无模型时的"离线桩"明示由向导页面负责,本模块只负责把用户送去向导(红线 34)。

兄弟模块(A143/A144/A145/A150/A154)允许缺席:惰性导入 + 中文降级日志,
缺席时按"该来源无"处理,保证并行开发期导入零依赖。只用标准库。
"""
from __future__ import annotations

import logging
import os
import threading
import webbrowser
from importlib import import_module
from typing import Any, Callable

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.security import keys

__all__ = ["has_any_model", "ensure_setup"]

logger = logging.getLogger(__name__)

#: 不弹浏览器、只打印地址的环境变量开关(值非空即生效)
NO_BROWSER_ENV = "NETSENTINEL_NO_BROWSER"

#: 向导真实首启的遥测计数名
WIZARD_SHOWN_METRIC = "setup.wizard_shown"

#: 向导缺省地址形态(A154/A145 均绑定 127.0.0.1,契约 §1 setup_port)
_URL_TEMPLATE = "http://127.0.0.1:{port}/"

#: 向导启动单例(进程内幂等):None=尚未启动;str=已启动地址
_started_url: str | None = None

_STATE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 惰性兄弟模块导入(并行期允许缺席,降级不抛)
# ---------------------------------------------------------------------------


def _try_import(dotted: str) -> Any:
    """惰性导入兄弟模块;任何导入失败都视为"未就位"返回 None(降级,不抛)。"""
    try:
        return import_module(dotted)
    except Exception as exc:  # noqa: BLE001 - 并行期 ImportError/SyntaxError 等一律降级
        logger.debug("兄弟模块 %s 未就位(忽略):%s", dotted, exc)
        return None


def _active_runtime_spec(cfg: Config) -> str | None:
    """读活动模型运行时(A144);未就位 / 异常 / 空 spec 一律返回 None。"""
    manager_mod = _try_import("netsentinel.vision.model_manager")
    manager_cls = getattr(manager_mod, "ModelManager", None) if manager_mod else None
    if manager_cls is None:
        logger.debug("A144 model_manager 未就位,按无活动模型处理")
        return None
    try:
        spec = manager_cls(cfg.model_runtime_path).get_active()
    except Exception as exc:  # noqa: BLE001 - 损坏运行时文件等按"无"降级
        logger.warning("读取活动模型运行时 %s 失败(按无活动模型处理):%s",
                       getattr(cfg, "model_runtime_path", None), exc)
        return None
    return spec if isinstance(spec, str) and spec.strip() else None


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


def _load_vision_filter() -> Callable[[str], bool] | None:
    """惰性取 A150 ``is_vision_model``;未就位返回 None(调用方降级)。"""
    capability = _try_import("netsentinel.vision.capability")
    func = getattr(capability, "is_vision_model", None) if capability else None
    return func if callable(func) else None


def _local_vision_found(cfg: Config, scanner: Any) -> bool:
    """本地扫描来源:任一 ``ok`` 条目含视觉模型即 True。

    - ``scanner`` 注入时直接使用;缺省惰性构造 A143
      ``LocalVisionScanner(cfg.local_probe_ports)``(红线 32:回环受限端口
      由 A143 负责保证,本模块不另行发网);
    - 视觉判定委托 A150;A150 未就位时按 A143 同款口径降级——
      "缺席全返回",即任意非空模型名都视为候选(宁可不打扰用户);
    - 扫描异常按"未发现"降级并记 WARNING,不抛。
    """
    if scanner is None:
        probe_mod = _try_import("netsentinel.vision.local_probe")
        scanner_cls = getattr(probe_mod, "LocalVisionScanner", None) if probe_mod else None
        if scanner_cls is None:
            logger.debug("A143 local_probe 未就位,跳过本地扫描来源")
            return False
        try:
            ports = [str(p) for p in (getattr(cfg, "local_probe_ports", None) or [])]
            scanner = scanner_cls(ports)
        except Exception as exc:  # noqa: BLE001 - 构造失败按"未发现"降级
            logger.warning("构造本地视觉扫描器失败(按未发现处理):%s", exc)
            return False
    try:
        results = scanner.scan()
    except Exception as exc:  # noqa: BLE001 - 扫描失败按"未发现"降级
        logger.warning("本地视觉服务扫描失败(按未发现处理):%s", exc)
        return False

    is_vision = _load_vision_filter()
    if is_vision is None:
        logger.debug("A150 capability 未就位,本地模型不做视觉过滤(缺席全返回)")
    for entry in results or []:
        if not isinstance(entry, dict) or not entry.get("ok"):
            continue
        names = _model_names(entry.get("models"))
        if any((is_vision(name) if is_vision else True) for name in names):
            return True
    return False


# ---------------------------------------------------------------------------
# has_any_model:三来源探测
# ---------------------------------------------------------------------------


def has_any_model(cfg: Config, *, scanner: Any = None) -> bool:
    """判断用户当前是否已有可用视觉模型(三来源任一命中即 True)。

    1. 活动模型运行时(A144 ``get_active()`` 非空);
    2. 任一云平台密钥已配(``keys.configured(cfg)`` 任一 True);
    3. 本地扫描命中(注入 ``scanner`` 或缺省惰性 A143):任一 ``ok``
       条目含视觉模型(A150 判定;缺席时按"缺席全返回"降级)。

    只读探测、绝不写状态、绝不抛出;``scanner`` 供测试注入伪扫描器。
    """
    if _active_runtime_spec(cfg) is not None:
        return True
    try:
        if any(keys.configured(cfg).values()):
            return True
    except Exception as exc:  # noqa: BLE001 - 密钥体检异常不拖累后续来源
        logger.warning("云密钥配置体检失败(按未配置处理):%s", exc)
    return _local_vision_found(cfg, scanner)


# ---------------------------------------------------------------------------
# ensure_setup:需要时首启向导
# ---------------------------------------------------------------------------


def _resolve_open_browser(cfg: Config, open_browser: bool | None) -> bool:
    """弹窗三态裁决:**参数 > 环境变量 ``NETSENTINEL_NO_BROWSER`` > 配置**。

    - 参数显式给 True/False → 直接生效(最高优先级);
    - 未给参数但环境变量 ``NETSENTINEL_NO_BROWSER`` 值非空 → 不弹;
    - 两者皆无 → 取 ``cfg.onboarding_auto_open``(缺省 True)。
    """
    if open_browser is not None:
        return bool(open_browser)
    if os.environ.get(NO_BROWSER_ENV):
        return False
    return bool(getattr(cfg, "onboarding_auto_open", True))


def _default_server_factory(cfg: Config) -> str | None:
    """缺省服务器工厂:惰性优先 A154 单例,退 A145 线程服务;均缺席抛中文错误。

    对下层一律 ``open_browser=False``——浏览器开/合由本模块统一决策,
    避免 A154/A145 各自再开一次造成双开。
    """
    daemon = _try_import("netsentinel.setup.daemon")
    ensure = getattr(daemon, "ensure_setup_server", None) if daemon else None
    if callable(ensure):
        return ensure(cfg, open_browser=False)
    server_mod = _try_import("netsentinel.setup.server")
    serve = getattr(server_mod, "serve", None) if server_mod else None
    if callable(serve):
        port = int(getattr(cfg, "setup_port", 8766) or 8766)
        serve(port, open_browser=False)
        return _URL_TEMPLATE.format(port=port)
    raise RuntimeError(
        "连接向导服务未就位:netsentinel.setup.daemon(A154)与 "
        "netsentinel.setup.server(A145)均缺席;请等兄弟模块落地后再试"
    )


def _announce(url: str, should_open: bool) -> None:
    """按裁决结果通知用户:弹浏览器;不弹 / 弹失败时打印中文提示与地址。"""
    opened = False
    if should_open:
        try:
            opened = bool(webbrowser.open(url))
        except Exception as exc:  # noqa: BLE001 - 弹窗失败退回打印,不抛
            logger.warning("打开浏览器失败(退回打印地址):%s", exc)
    if not opened:
        print(f"未检测到视觉模型,连接向导已启动:{url}", flush=True)


def ensure_setup(cfg: Config, *, open_browser: bool | None = None,
                 server_factory: Any = None) -> str | None:
    """需要时启动连接向导并返回其地址;已有可用模型时返回 None(不打扰)。

    - ``has_any_model(cfg)`` 为 True → 直接返回 ``None``,零副作用;
    - 否则启动向导服务器:``server_factory`` 注入(测试);缺省惰性走
      A154 ``ensure_setup_server`` 单例(A145 退路,仍缺席则抛中文
      RuntimeError——集成方按"异常只告警不阻断"兜底);
    - 弹窗裁决见 :func:`_resolve_open_browser`(参数 > 环境变量 > 配置);
      不弹或弹失败时打印 ``未检测到视觉模型,连接向导已启动:{url}``;
    - 幂等:进程内二次调用直接复用已启动地址(不再起服务 / 打印 / 计数),
      工厂返回空值视为启动失败,不缓存、下次可重试;
    - 每次真实首启记 ``telemetry.inc("setup.wizard_shown")``。
    """
    if has_any_model(cfg):
        return None
    should_open = _resolve_open_browser(cfg, open_browser)

    global _started_url
    with _STATE_LOCK:
        if _started_url is not None:
            return _started_url
        factory = server_factory if server_factory is not None else _default_server_factory
        url = factory(cfg)
        if not (isinstance(url, str) and url.strip()):
            logger.warning("连接向导服务器启动失败(未返回地址),本次不打扰用户")
            return None
        url = url.strip()
        telemetry.inc(WIZARD_SHOWN_METRIC)
        _started_url = url
    _announce(url, should_open)
    return url
