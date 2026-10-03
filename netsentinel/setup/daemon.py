# -*- coding: utf-8 -*-
"""连接向导常驻守护单例(A154,依据 CONTRACTS-V8.md §3 A154 / §0 红线 32)。

与 A149(``setup.trigger.ensure_setup``)的分工:A149 只在**首次全无模型**时
拉起向导("首启触发",有模型绝不打扰);本模块面向**常驻**——向导页本身
就是模型切换器,任意入口(CLI ``modelmgr serve`` / REST / 复核台跳转)都经
本单例保证"进程内至多一个实例 + 跨进程至多一个监听端口",重复调用零成本。

单例语义(两层,依次判定):

1. **进程内**:模块级缓存 ``_url``——同进程二次调用直接返回已启动地址,
   零工厂调用、零探活;
2. **跨进程**:锁文件 ``{cfg.data_dir}/.setup.lock``(内容=端口号)——先读
   锁并对 ``http://127.0.0.1:{port}/`` 做轻量 GET 探活(超时
   :data:`PROBE_TIMEOUT_S` 秒;任何 HTTP 应答(含 4xx/5xx)都算存活:
   能应答即有监听者)。存活 → 直接复用该地址并缓存(**零 factory 调用**,
   锁可能属于另一进程,本进程只"借用",``stop_setup_server`` 不动它);
   死亡 / 无锁 / 内容坏 → 视为陈旧锁,经 ``factory``(缺省惰性 A145
   ``SetupServer``)重启:端口从 ``cfg.setup_port`` 起试,被占
   (``OSError``)则依次 +1..+:data:`PORT_SPAN`,全部用尽抛中文
   ``RuntimeError``;成功后按**实际绑定端口**回写锁文件。

弹窗三态与 A149 完全一致(**参数 > 环境变量 ``NETSENTINEL_NO_BROWSER`` >
配置 ``onboarding_auto_open``**,缺省 True),裁决结果原样传给
``factory(cfg).start(port=..., open_browser=...)``——真正弹窗由 A145
``SetupServer.start`` 执行,本模块不重复弹(防双开;A149 调本模块时同样
固定传 ``open_browser=False``)。复用路径不起服务,自然也不弹窗。

红线协同:探活只打 ``127.0.0.1`` 上的锁文件指定端口,绝不扫段(红线 32);
本模块不触碰密钥(红线 33);无模型时的"离线桩"明示由向导页面负责
(红线 34)。测试经 ``factory`` 注入伪服务器、monkeypatch :func:`_port_alive`
注入伪探活,全程离线。

用法示例::

    from netsentinel.setup.daemon import ensure_setup_server, stop_setup_server

    url = ensure_setup_server(cfg)     # 常驻单例;重复调用幂等复用
    ...
    stop_setup_server()                # 停本进程实例并清锁(幂等)
"""
from __future__ import annotations

import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from netsentinel import telemetry

__all__ = ["ensure_setup_server", "stop_setup_server", "NO_BROWSER_ENV", "LOCK_FILENAME"]

logger = logging.getLogger(__name__)

#: 不弹浏览器、只打印/记录地址的环境变量开关(与 A149 同名同语义;值非空即生效)
NO_BROWSER_ENV = "NETSENTINEL_NO_BROWSER"

#: 锁文件名(落在 ``cfg.data_dir`` 下,内容=向导实际监听端口)
LOCK_FILENAME = ".setup.lock"

#: 缺省向导端口(与 contracts.Config.setup_port 一致)
_DEFAULT_SETUP_PORT = 8766

#: 端口被占时的回退跨度:依次尝试 setup_port+1 .. setup_port+PORT_SPAN
PORT_SPAN = 5

#: 探活超时(秒):轻量 GET ``/``,超时即视为死
PROBE_TIMEOUT_S = 0.5

#: 向导地址形态(A145 只绑 127.0.0.1)
_URL_TEMPLATE = "http://127.0.0.1:{port}/"

#: 真实启动(经 factory 起了新服务)的遥测计数名
DAEMON_START_METRIC = "setup.daemon_start"

#: 复用跨进程存活实例(零 factory)的遥测计数名
DAEMON_REUSE_METRIC = "setup.daemon_reuse"

#: 停止(停本进程服务或清自有锁)的遥测计数名
DAEMON_STOP_METRIC = "setup.daemon_stop"

# ---- 模块级单例状态(仅在 _STATE_LOCK 内读写;测试经 autouse 夹具复位)----

#: 本进程经 factory 构建的服务对象(A145 SetupServer 或注入的伪对象;复用外来锁时为 None)
_server: Any = None

#: 进程内缓存:已确保的向导地址(None=尚未确保)
_url: str | None = None

#: 最近一次 ensure 对应的锁文件路径
_lock_file: Path | None = None

#: 锁文件是否由**本进程**写入(复用外来存活锁时为 False,stop 不清它)
_owns_lock: bool = False

_STATE_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# 锁文件与端口工具
# ---------------------------------------------------------------------------


def _lock_path_for(cfg: Any) -> Path:
    """取锁文件路径 ``{cfg.data_dir}/.setup.lock``(data_dir 缺省 ``data``)。"""
    return Path(getattr(cfg, "data_dir", "data")) / LOCK_FILENAME


def _read_lock_port(path: Path) -> int | None:
    """读锁文件端口;文件缺失 / 内容非整数 / 超出 1..65535 一律返回 None。"""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        port = int(text)
    except ValueError:
        logger.info("锁文件 %s 内容 %r 不是端口,视为无锁", path, text[:32])
        return None
    if not 1 <= port <= 65535:
        logger.info("锁文件 %s 端口 %d 越界,视为无锁", path, port)
        return None
    return port


def _write_lock(path: Path, port: int) -> bool:
    """回写锁文件(自动建目录);失败只告警不抛——向导可用性优先于簿记。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{port}\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("写入锁文件 %s 失败(忽略,不影响向导运行):%s", path, exc)
        return False
    return True


def _remove_lock(path: Path) -> None:
    """删除锁文件(幂等;失败只告警不抛)。"""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("删除锁文件 %s 失败(忽略):%s", path, exc)


def _coerce_port(raw: Any, fallback: int = _DEFAULT_SETUP_PORT) -> int:
    """把配置端口规整为 int;非整数回退缺省 8766(与 A145 同口径)。"""
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("配置端口 %r 不是整数,回退默认 %d", raw, fallback)
        return fallback


def _port_from_url(url: str) -> int | None:
    """从服务返回的 URL 解析端口(失败返回 None,由调用方回退请求端口)。"""
    try:
        port = urllib.parse.urlsplit(url).port
    except ValueError:
        return None
    return int(port) if port else None


# ---------------------------------------------------------------------------
# 探活与弹窗裁决
# ---------------------------------------------------------------------------


def _port_alive(port: int) -> bool:
    """对 ``http://127.0.0.1:{port}/`` 做轻量 GET 探活(超时 :data:`PROBE_TIMEOUT_S` 秒)。

    - 任何 HTTP 应答(含 4xx/5xx)都证明端口上有服务在应答 → 存活;
    - 连接被拒 / 超时 / 重置等一律视为死亡;
    - 只访问回环地址上的单个锁内端口(红线 32:绝不扫段),并显式绕过
      系统代理(``HTTP_PROXY`` 等不得劫持回环探活)。
    测试经 ``monkeypatch.setattr(daemon, "_port_alive", ...)`` 注入伪探活。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(_URL_TEMPLATE.format(port=port), timeout=PROBE_TIMEOUT_S) as resp:
            resp.getcode()
            return True
    except urllib.error.HTTPError:
        return True  # 有 HTTP 应答即有监听者
    except Exception:  # noqa: BLE001 URLError/timeout/refused/坏端口等一律视为死
        return False


def _resolve_open_browser(cfg: Any, open_browser: bool | None) -> bool:
    """弹窗三态裁决(与 A149 完全一致):**参数 > 环境变量 > 配置**。

    - 参数显式给 True/False → 直接生效(最高优先级);
    - 未给参数但 ``NETSENTINEL_NO_BROWSER`` 值非空 → 不弹;
    - 两者皆无 → 取 ``cfg.onboarding_auto_open``(缺省 True)。
    """
    if open_browser is not None:
        return bool(open_browser)
    if os.environ.get(NO_BROWSER_ENV):
        return False
    return bool(getattr(cfg, "onboarding_auto_open", True))


def _default_factory(cfg: Any) -> Any:
    """缺省工厂:惰性构建 A145 ``SetupServer``(模块缺席抛中文 RuntimeError)。"""
    try:
        from netsentinel.setup.server import SetupServer  # noqa: PLC0415 惰性导入:A145
    except Exception as exc:  # noqa: BLE001 ImportError/SyntaxError 等并行期错误
        raise RuntimeError(
            f"连接向导服务模块(A145)未就位,无法启动守护:netsentinel.setup.server 导入失败({exc})"
        ) from exc
    return SetupServer(cfg)


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------


def ensure_setup_server(
    cfg: Any,
    *,
    open_browser: bool | None = None,
    factory: Callable[[Any], Any] | None = None,
) -> str | None:
    """确保连接向导守护在运行并返回其地址(单例;重复调用幂等)。

    判定顺序:

    1. 进程内缓存命中 → 直接返回(零副作用);
    2. 锁文件存在且端口探活存活 → 复用 ``http://127.0.0.1:{port}/`` 并缓存
       (零 factory 调用;外来进程的锁**不**归属本进程,stop 不会清它);
    3. 无锁 / 死锁 / 坏锁 → ``factory(cfg)`` 构建服务(缺省惰性 A145
       ``SetupServer``),从 ``cfg.setup_port`` 起 ``start(port=…, open_browser=…)``,
       端口被占(``OSError``)则 +1..+:data:`PORT_SPAN` 递增重试(同一服务
       对象重试, factory 仅调一次);全部用尽抛中文 ``RuntimeError``;
       成功后按实际绑定端口回写锁文件并缓存。

    参数:
        cfg: ``netsentinel.contracts.Config``(用 ``data_dir`` / ``setup_port`` /
            ``onboarding_auto_open``;宽松 getattr,容忍并行期伪配置对象)。
        open_browser: 弹窗三态裁决见 :func:`_resolve_open_browser`;裁决结果
            原样传给 ``start``(真正弹窗由 A145 执行,本模块不重复弹)。
        factory: ``(cfg) -> 服务对象``(对象需提供
            ``start(port=…, open_browser=…) -> url`` 与可选 ``stop()``);
            测试注入点,缺省 :func:`_default_factory`。

    返回:
        向导回环地址(形如 ``http://127.0.0.1:8766/``)。

    异常:
        RuntimeError: 端口 ``setup_port``~``setup_port+5`` 全被占用(中文),
            A145 模块缺席(中文),或服务未返回有效地址(中文);
            工厂/``start`` 抛出的非 ``OSError`` 异常原样上抛。
    """
    global _server, _url, _lock_file, _owns_lock
    with _STATE_LOCK:
        # 1) 进程内缓存(同进程二次调用零成本)
        if _url is not None:
            return _url

        # 2) 跨进程锁:先读端口再探活,活着直接复用(零 factory)
        lock_file = _lock_path_for(cfg)
        locked_port = _read_lock_port(lock_file)
        if locked_port is not None:
            if _port_alive(locked_port):
                url = _URL_TEMPLATE.format(port=locked_port)
                _url, _lock_file, _owns_lock = url, lock_file, False
                telemetry.inc(DAEMON_REUSE_METRIC)
                logger.info("复用已在运行的连接向导:%s(锁文件 %s)", url, lock_file)
                return url
            logger.info(
                "锁文件 %s 指向端口 %d 但无应答,视为陈旧锁,重新启动向导",
                lock_file,
                locked_port,
            )

        # 3) 无锁 / 死锁:经工厂重启,端口被占则 +1..+PORT_SPAN
        make_server = factory if factory is not None else _default_factory
        should_open = _resolve_open_browser(cfg, open_browser)
        base = _coerce_port(getattr(cfg, "setup_port", _DEFAULT_SETUP_PORT))
        server = make_server(cfg)
        last_error: OSError | None = None
        for offset in range(PORT_SPAN + 1):
            port = base + offset
            try:
                returned = server.start(port=port, open_browser=should_open)
            except OSError as exc:  # 端口被占(A145 绑定失败):换下一端口重试
                logger.warning("向导端口 %d 被占用(%s),尝试下一端口", port, exc)
                last_error = exc
                continue
            if not isinstance(returned, str) or not returned.strip():
                raise RuntimeError("连接向导服务启动失败:服务器未返回有效地址")
            url = returned.strip()
            actual_port = _port_from_url(url) or port
            _write_lock(lock_file, actual_port)
            _server, _url, _lock_file, _owns_lock = server, url, lock_file, True
            telemetry.inc(DAEMON_START_METRIC)
            logger.info("连接向导守护已启动:%s(锁文件 %s)", url, lock_file)
            return url
        raise RuntimeError(
            f"连接向导端口 {base}~{base + PORT_SPAN} 均被占用,无法启动守护;"
            f"请释放端口或在配置中修改 setup_port(最后一次错误:{last_error})"
        )


def stop_setup_server() -> None:
    """停止本进程持有的向导守护并清理自有锁文件(幂等,绝不抛出)。

    - 只停**本进程经 factory 启动**的服务(``_server``,调其 ``stop()``);
      复用的外来(跨进程)实例不在本进程管辖范围,对应锁文件保持原样;
    - 未启动 / 已停均为无操作;停止失败 / 删锁失败只记日志。
    """
    global _server, _url, _lock_file, _owns_lock
    with _STATE_LOCK:
        server, lock_file, owns = _server, _lock_file, _owns_lock
        _server, _url, _lock_file, _owns_lock = None, None, None, False
    did_work = False
    if server is not None:
        stop = getattr(server, "stop", None)
        if callable(stop):
            try:
                stop()
                did_work = True
            except Exception as exc:  # noqa: BLE001 停服是清理动作,失败只记日志
                logger.warning("停止向导服务异常(忽略):%s", exc)
    if owns and lock_file is not None:
        _remove_lock(lock_file)
        did_work = True
    if did_work:
        telemetry.inc(DAEMON_STOP_METRIC)
        logger.info("连接向导守护已停止")
