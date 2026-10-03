# -*- coding: utf-8 -*-
"""连接向导自托管服务(A145,依据 CONTRACTS-V8.md §0 红线 32–34 / §3 A145 条目)。

纯标准库 ``http.server`` 实现(**不依赖 fastapi / streamlit**),只绑定
``127.0.0.1``(红线 32:向导与探测动作仅限回环)。提供中文单页 + 五个 JSON 接口:

- ``GET /``            → 向导页:注入渲染器(A156)惰性,缺席依次回退 A146
  ``page_html()`` 原样、内置兜底页;
- ``POST /api/status`` → ``{"active","switched_by","local","keys"}``;
  ``local`` 为 A143 **现扫描**(非缓存,看门狗限时),``keys`` 仅布尔+掩码
  (红线 33:响应绝不含密钥本体);
- ``POST /api/probe``  → ``{"local":[...]}``(A143 扫描);
- ``POST /api/activate`` ``{"spec"}`` → A144 ``set_active(switched_by="wizard")``,
  返回 ``{"ok","status"}``;失败 400 ``{"error": 中文}``;
- ``POST /api/setkey`` ``{"provider","key"}`` → A152 落盘,只回
  ``{"ok","masked"}``;**响应绝不含 key 本体**(红线 33,含出口拦截兜底);
- ``POST /api/test`` ``{"spec"}`` → A147 连通性测试结果透传。

容错口径:未知路径 404 中文 JSON;已知路径方法不符 405;请求体坏 JSON / 非
JSON 对象 / 超限 400;全部 handler 异常兜底 500 中文(**响应不带栈**,栈只进
服务端日志)。兄弟模块(A143/A144/A146/A147/A152/A156)一律惰性导入、允许
缺席:缺席时对应接口给出中文降级提示而非崩溃,便于 A143–A162 并行开发。

用法示例::

    from netsentinel.setup.server import SetupServer

    srv = SetupServer(cfg)
    url = srv.start(port=0)          # 随机回环端口;默认 cfg.setup_port
    ...
    srv.stop()                       # 幂等
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from netsentinel import telemetry

__all__ = ["SetupServer"]

logger = logging.getLogger(__name__)

#: 默认向导端口(与 contracts.Config.setup_port 一致)
_DEFAULT_SETUP_PORT = 8766

#: 默认本地探测端口(兄弟模块 A143 缺席或 cfg 未配置时的口径)
_DEFAULT_PROBE_PORTS: tuple[str, ...] = ("11434", "1234", "8000", "9997")

#: A143 扫描器单端口超时(契约缺省 1.5s,状态接口收紧到 1s,保证"超时受控")
_SCAN_PER_PORT_S = 1.0

#: 单次扫描的看门狗上限:超过即放弃等待、返回空结果(超时受控的兜底)
_SCAN_BUDGET_S = 6.0

#: 请求体上限(1 MiB):向导只需小 JSON,超限直接 400
_MAX_BODY_BYTES = 1 << 20

#: 超限请求的排空上限:拒绝前读完已发字节,避免连接复位(WinError 10053)
_MAX_DRAIN_BYTES = 16 << 20

#: 兜底向导页(A156/A146 均缺席时使用;含红线 34 明示文案)
_FALLBACK_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>净网哨兵 · 连接向导</title></head>
<body>
<h1>净网哨兵 · 连接向导</h1>
<p>向导页面模块(A146/A156)尚未就位,当前为内置兜底页。</p>
<p>可在本页对应的 API 完成状态查询 / 本机扫描 / 模型启用 / 密钥配置 / 连通性测试。</p>
<p style="color:#b00">使用离线桩继续:离线桩,非模型判定,仅作流程演示,不代表任何模型结论。</p>
</body>
</html>
"""


class _HttpError(Exception):
    """受控业务错误:携带 HTTP 状态码与中文消息,由分发层统一转 JSON。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _brief(value: Any, limit: int = 80) -> str:
    """把任意值裁成短摘要(用于日志;响应正文绝不经此回显)。"""
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001 repr 失败兜底,不影响主流程
        text = "<unrepr>"
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _WizardHTTPServer(ThreadingHTTPServer):
    """绑定 127.0.0.1 的向导 HTTP 服务;daemon 线程池,持有宿主引用。"""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], wizard: "SetupServer") -> None:
        self.wizard = wizard
        super().__init__(address, _WizardHandler)


class _WizardHandler(BaseHTTPRequestHandler):
    """向导请求分发:路由表 + 中文 JSON 错误兜底(不向响应泄漏栈)。"""

    server: _WizardHTTPServer
    server_version = "NetSentinelSetup/8"  # noqa: N815 - http.server 约定命名

    #: 当前请求已读入的原始请求体(由 _dispatch 在路由前填充)
    _raw_body: bytes | None = None

    # -- 基础设施 -----------------------------------------------------------

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug("向导访问 %s %s", self.address_string(), format % args)

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定命名
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _path(self) -> str:
        """取规范路径:去查询串、合并尾部斜杠(``/api/status/`` 等同)。"""
        raw = urllib.parse.urlsplit(self.path).path or "/"
        if len(raw) > 1:
            raw = raw.rstrip("/") or "/"
        return raw

    def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionError, BrokenPipeError):  # 客户端提前断开属预期
            pass

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", body)

    def _send_html(self, status: int, html: str) -> None:
        self._send_bytes(status, "text/html; charset=utf-8", html.encode("utf-8"))

    def _dispatch(self, method: str) -> None:
        telemetry.inc("setup.request")
        path = self._path()
        wizard = self.server.wizard
        try:
            # 先读完请求体再路由:任何早退响应(404/405/400)都不留未读字节,
            # 避免 Windows 上带未读数据关连接触发 RST(客户端 WinError 10053)。
            self._raw_body = self._read_raw_body()
            if path == "/":
                if method != "GET":
                    raise _HttpError(405, "向导首页只支持 GET:请直接用浏览器打开页面")
                self._handle_page(wizard)
                return
            if not path.startswith("/api/"):
                raise _HttpError(404, f"接口不存在:{path}(向导仅提供 / 与 /api/* 接口)")
            api = path[len("/api/") :]
            handler = self._API_HANDLERS.get((method, api))
            if handler is None:
                if any(api == known for _, known in self._API_HANDLERS):
                    raise _HttpError(
                        405, f"请求方法不支持:接口 /api/{api} 不接受 {method}"
                    )
                raise _HttpError(404, f"接口不存在:/api/{api}(请从向导页面发起请求)")
            handler(self, wizard)
        except _HttpError as err:
            self._send_json(err.status, {"error": err.message})
        except Exception as exc:  # noqa: BLE001 兜底:任何异常都转中文 500,不带栈
            logger.exception("向导接口处理异常:%s %s(%s)", method, path, _brief(exc))
            try:
                self._send_json(
                    500, {"error": "服务器内部错误:请求处理失败,详情见服务端日志"}
                )
            except Exception:  # noqa: BLE001 响应已不可写(客户端断开等),静默
                pass

    # -- 请求体 -------------------------------------------------------------

    def _read_raw_body(self) -> bytes | None:
        """一次性读取请求体原始字节;声明超限时排空后返回 ``None``(调用方 400)。

        排空(上限 :data:`_MAX_DRAIN_BYTES`)保证拒绝响应也不留未读字节。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > _MAX_BODY_BYTES:
            self._drain(length)
            return None
        return self.rfile.read(length) or b""

    def _drain(self, length: int) -> None:
        """按块丢弃 ``length`` 字节(封顶 :data:`_MAX_DRAIN_BYTES`),只用于排空。"""
        remaining = min(length, _MAX_DRAIN_BYTES)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _read_json(self, *, required: bool) -> dict[str, Any]:
        """解析(已读入的)JSON 对象请求体;坏输入一律 _HttpError(中文)。"""
        raw = self._raw_body
        if raw is None:
            raise _HttpError(
                400, f"请求体过大(超过 {_MAX_BODY_BYTES} 字节),已拒绝处理"
            )
        if not raw.strip():
            if required:
                raise _HttpError(400, '请求体为空:需要 JSON 对象,如 {"spec": "ollama:llava"}')
            return {}
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _HttpError(400, "请求体不是有效的 UTF-8 文本,无法解析") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _HttpError(
                400, f"请求体不是合法 JSON({exc.msg},第 {exc.lineno} 行第 {exc.colno} 列)"
            ) from exc
        if not isinstance(data, dict):
            raise _HttpError(400, "请求体应为 JSON 对象(如 {\"spec\": \"ollama:llava\"})")
        return data

    @staticmethod
    def _require_str(body: dict[str, Any], field: str, hint: str) -> str:
        value = body.get(field)
        if not isinstance(value, str) or not value.strip():
            raise _HttpError(400, f"缺少必填字段 {field}:{hint}")
        return value.strip()

    # -- 接口实现 -----------------------------------------------------------

    def _handle_page(self, wizard: "SetupServer") -> None:
        status = wizard.status_payload()
        html = wizard.render_page(status, status.get("active"), status.get("local", []))
        self._send_html(200, html)

    def _handle_status(self, wizard: "SetupServer") -> None:
        payload = wizard.status_payload()
        self._send_guarded_json(wizard, 200, payload)

    def _handle_probe(self, wizard: "SetupServer") -> None:
        self._read_json(required=False)  # 允许空体;解析仅用于坏 JSON 早失败
        local, error = wizard.scan_local()
        payload: dict[str, Any] = {"local": local}
        if error:
            payload["error"] = error
        self._send_guarded_json(wizard, 200, payload)

    def _handle_activate(self, wizard: "SetupServer") -> None:
        body = self._read_json(required=True)
        spec = self._require_str(body, "spec", "要启用的模型,如 ollama:llava 或 stub")
        payload = wizard.activate(spec)
        self._send_json(200, payload)

    def _handle_setkey(self, wizard: "SetupServer") -> None:
        body = self._read_json(required=True)
        provider = self._require_str(body, "provider", "云平台提供方名,如 glm / openai")
        key = body.get("key")
        if not isinstance(key, str):
            raise _HttpError(400, "缺少必填字段 key:密钥应为字符串(password 语义,只进不显)")
        payload = wizard.set_key(provider, key)
        # 红线 33 出口拦截:响应绝不含密钥本体,任何实现缺陷都在此兜底
        text = json.dumps(payload, ensure_ascii=False)
        secret = key.strip()
        if secret and secret in text:
            logger.critical("红线 33 拦截:/api/setkey 响应疑似包含密钥本体,已改为通用错误")
            self._send_json(500, {"error": "内部错误:响应校验未通过,已拦截(密钥绝不回显)"})
            return
        self._send_json(200, payload)

    def _handle_test(self, wizard: "SetupServer") -> None:
        body = self._read_json(required=True)
        spec = self._require_str(body, "spec", "要测试的模型,如 ollama:llava")
        payload = wizard.test_connection(spec)
        self._send_guarded_json(wizard, 200, payload)

    def _send_guarded_json(self, wizard: "SetupServer", status: int, payload: dict[str, Any]) -> None:
        """发送前做密钥泄漏兜底:命中 cfg 已知密钥则剥掉掩码字段再发(红线 33)。"""
        text = json.dumps(payload, ensure_ascii=False)
        if not wizard.leaks_secret(text):
            self._send_json(status, payload)
            return
        logger.critical("红线 33 拦截:%s 响应疑似包含密钥本体,已剥除掩码字段", self.path)

        def _strip_masked(value: Any) -> Any:
            """递归剥除所有键名为 masked 的字段(逐层,不限于 keys 一层)。"""
            if isinstance(value, dict):
                return {
                    k: _strip_masked(v)
                    for k, v in value.items()
                    if k != "masked"
                }
            if isinstance(value, list):
                return [_strip_masked(v) for v in value]
            return value

        stripped = _strip_masked(payload)
        text = json.dumps(stripped, ensure_ascii=False)
        if wizard.leaks_secret(text):  # 仍泄漏:整体转通用错误,绝不出密钥
            self._send_json(500, {"error": "内部错误:响应校验未通过,已拦截(密钥绝不回显)"})
            return
        self._send_json(status, stripped)

    #: 路由表:(方法, 接口名) -> 处理函数
    _API_HANDLERS: dict[tuple[str, str], Callable[[_WizardHandler, "SetupServer"], None]] = {
        ("POST", "status"): _handle_status,
        ("POST", "probe"): _handle_probe,
        ("POST", "activate"): _handle_activate,
        ("POST", "setkey"): _handle_setkey,
        ("POST", "test"): _handle_test,
    }


class SetupServer:
    """连接向导服务(A145):纯标准库、仅回环、兄弟依赖全部可注入/惰性。

    参数:
        cfg: ``netsentinel.contracts.Config``;用到 ``setup_port`` /
            ``local_probe_ports`` / ``model_runtime_path`` / ``vlm_api_keys``。
        scanner: 扫描器(需提供 ``scan() -> list``);缺省惰性构建 A143
            ``LocalVisionScanner``(缺席则本地扫描降级为空结果+中文提示)。
        manager: 模型管理器(需提供 ``set_active`` / ``status`` / ``get_active``);
            缺省惰性构建 A144 ``ModelManager(cfg.model_runtime_path)``。
        key_acceptor: 密钥落盘函数 ``(provider, key) -> {"ok","masked",...}``;
            缺省惰性委托 A152 ``accept_key_input``。
        tester: 连通性测试函数 ``(spec) -> dict``;缺省惰性委托 A147
            ``test_connection(spec, cfg)``。
        page_renderer: 页面渲染函数 ``(status, active, local_models) -> str``;
            缺省惰性 A156 ``render_page``,再缺席回退 A146 ``page_html()``,
            仍缺席用内置兜底页。

    线程模型:``start`` 在后台 daemon 线程跑 ``ThreadingHTTPServer``
    (每请求一线程);扫描另设看门狗线程限时(超时受控)。
    """

    def __init__(
        self,
        cfg: Any,
        *,
        scanner: Any = None,
        manager: Any = None,
        key_acceptor: Callable[[str, str], dict[str, Any]] | None = None,
        tester: Callable[[str], dict[str, Any]] | None = None,
        page_renderer: Callable[[dict[str, Any], Any, list], str] | None = None,
    ) -> None:
        self.cfg = cfg
        self._scanner = scanner
        self._scanner_resolved = scanner is not None
        self._manager = manager
        self._manager_resolved = manager is not None
        self._key_acceptor = key_acceptor
        self._key_acceptor_resolved = key_acceptor is not None
        self._tester = tester
        self._tester_resolved = tester is not None
        self._page_renderer = page_renderer
        self._httpd: _WizardHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._url: str | None = None
        self._lock = threading.Lock()

    # -- 生命周期 -----------------------------------------------------------

    @property
    def url(self) -> str | None:
        """当前服务地址(未启动为 ``None``;仅 ``http://127.0.0.1:<port>/``)。"""
        return self._url

    @property
    def port(self) -> int | None:
        """当前监听端口(未启动为 ``None``)。"""
        if self._httpd is None:
            return None
        return int(self._httpd.server_address[1])

    @property
    def running(self) -> bool:
        """服务是否在运行。"""
        return self._httpd is not None

    def _resolve_port(self, port: int | None) -> int:
        if port is not None:
            return int(port)
        raw = getattr(self.cfg, "setup_port", _DEFAULT_SETUP_PORT)
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning("配置项 setup_port=%r 不是整数,回退默认 %d", raw, _DEFAULT_SETUP_PORT)
            return _DEFAULT_SETUP_PORT

    def start(self, port: int | None = None, *, open_browser: bool = False) -> str:
        """启动向导服务(幂等:已运行直接返回现有地址),返回回环 URL。

        - 仅绑定 ``127.0.0.1``(红线 32);``port=0`` 由系统分配随机端口;
          缺省用 ``cfg.setup_port``(再缺省 8766);
        - ``open_browser=True`` 时打开系统浏览器;环境变量
          ``NETSENTINEL_NO_BROWSER=1`` 时只打印/记录 URL 不弹窗
          (与 A149/A154 的口径一致)。
        """
        with self._lock:
            if self._httpd is not None:
                logger.warning("向导服务已在运行:%s(重复 start 返回现有地址)", self._url)
                return str(self._url)
            bind_port = self._resolve_port(port)
            httpd = _WizardHTTPServer(("127.0.0.1", bind_port), self)
            thread = threading.Thread(
                target=httpd.serve_forever,
                kwargs={"poll_interval": 0.05},
                daemon=True,
                name="netsentinel-setup-server",
            )
            thread.start()
            self._httpd = httpd
            self._thread = thread
            self._url = f"http://127.0.0.1:{int(httpd.server_address[1])}/"
        telemetry.inc("setup.server_start")
        logger.info("连接向导已启动:%s", self._url)
        if open_browser:
            if os.environ.get("NETSENTINEL_NO_BROWSER") == "1":
                logger.info("NETSENTINEL_NO_BROWSER=1:跳过自动打开浏览器,请手动访问 %s", self._url)
            else:
                try:
                    webbrowser.open(self._url)
                except Exception as exc:  # noqa: BLE001 弹窗失败不阻断服务
                    logger.warning("自动打开浏览器失败(%s),请手动访问 %s", exc, self._url)
        return str(self._url)

    def serve(self, port: int | None = None, *, open_browser: bool = False) -> str:
        """``start`` 的契约别名(A148 ``modelmgr serve`` 等入口使用)。"""
        return self.start(port, open_browser=open_browser)

    def stop(self) -> None:
        """停止服务(幂等:未启动/已停均可重复调用)。"""
        with self._lock:
            httpd, self._httpd = self._httpd, None
            thread, self._thread = self._thread, None
            self._url = None
        if httpd is None:
            return
        try:
            httpd.shutdown()
        except Exception as exc:  # noqa: BLE001 停服是清理动作,失败只记日志
            logger.warning("向导服务 shutdown 异常(忽略):%s", _brief(exc))
        try:
            httpd.server_close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("向导服务 server_close 异常(忽略):%s", _brief(exc))
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        telemetry.inc("setup.server_stop")
        logger.info("连接向导已停止")

    def __enter__(self) -> "SetupServer":
        """上下文支持:进入时若未运行则自动启动(随机回环端口)。"""
        if not self.running:
            self.start(port=0)
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- 兄弟依赖(惰性解析,允许缺席)--------------------------------------

    def _resolve_scanner(self) -> Any:
        if self._scanner_resolved:
            return self._scanner
        self._scanner_resolved = True
        try:
            from netsentinel.vision.local_probe import (  # noqa: PLC0415 惰性导入:A143 允许缺席
                LocalVisionScanner,
            )

            ports = getattr(self.cfg, "local_probe_ports", None) or list(_DEFAULT_PROBE_PORTS)
            self._scanner = LocalVisionScanner(list(ports), timeout=_SCAN_PER_PORT_S)
        except Exception as exc:  # noqa: BLE001 并行期未就位/语法期错误一律降级
            logger.warning("本地探测模块(A143)未就位(%s),本地扫描降级为空结果", _brief(exc))
            self._scanner = None
        return self._scanner

    def _resolve_manager(self) -> Any:
        if self._manager_resolved:
            return self._manager
        self._manager_resolved = True
        try:
            from netsentinel.vision.model_manager import (  # noqa: PLC0415 惰性导入:A144 允许缺席
                ModelManager,
            )

            path = str(getattr(self.cfg, "model_runtime_path", "data/model_runtime.json"))
            self._manager = ModelManager(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("模型管理模块(A144)未就位(%s),活动模型读写不可用", _brief(exc))
            self._manager = None
        return self._manager

    def _resolve_key_acceptor(self) -> Callable[[str, str], dict[str, Any]] | None:
        if self._key_acceptor_resolved:
            return self._key_acceptor
        self._key_acceptor_resolved = True
        try:
            from netsentinel.security.key_api import (  # noqa: PLC0415 惰性导入:A152 允许缺席
                accept_key_input,
            )

            def _accept(provider: str, key: str) -> dict[str, Any]:
                return accept_key_input(provider, key)

            self._key_acceptor = _accept
        except Exception as exc:  # noqa: BLE001
            logger.warning("密钥接口模块(A152)未就位(%s),密钥写入不可用", _brief(exc))
            self._key_acceptor = None
        return self._key_acceptor

    def _resolve_tester(self) -> Callable[[str], dict[str, Any]] | None:
        if self._tester_resolved:
            return self._tester
        self._tester_resolved = True
        try:
            from netsentinel.vision.connectivity import (  # noqa: PLC0415 惰性导入:A147 允许缺席
                test_connection,
            )

            cfg = self.cfg

            def _test(spec: str) -> dict[str, Any]:
                return test_connection(spec, cfg)

            self._tester = _test
        except Exception as exc:  # noqa: BLE001
            logger.warning("连通性测试模块(A147)未就位(%s),连接测试不可用", _brief(exc))
            self._tester = None
        return self._tester

    # -- 业务动作(供 handler 调用;异常口径见各 docstring)------------------

    def scan_local(self) -> tuple[list[Any], str | None]:
        """触发本地扫描(现扫,不缓存),返回 ``(结果列表, 中文错误或 None)``。

        - A143 缺席 → ``([], 中文提示)``;
        - 扫描异常 → 原样抛出(由 handler 兜底 500);
        - 看门狗:超过 :data:`_SCAN_BUDGET_S` 秒放弃等待,返回空结果+超时提示
          (超时受控;迟到的扫描线程为 daemon,不影响进程退出)。
        """
        scanner = self._resolve_scanner()
        if scanner is None:
            return [], "本地探测模块(A143)未就位,无法扫描本机服务"
        holder: dict[str, Any] = {}

        def _run() -> None:
            try:
                holder["result"] = scanner.scan()
            except BaseException as exc:  # noqa: BLE001 跨线程带回原异常
                holder["error"] = exc

        worker = threading.Thread(target=_run, daemon=True, name="netsentinel-setup-scan")
        worker.start()
        worker.join(_SCAN_BUDGET_S)
        if worker.is_alive():
            return [], f"本地扫描超时(超过 {_SCAN_BUDGET_S:.0f} 秒),已返回空结果"
        if "error" in holder:
            raise holder["error"]
        result = holder.get("result")
        if not isinstance(result, list):
            logger.warning("本地扫描返回非列表(%s),按空结果处理", _brief(result))
            return [], "本地扫描返回结果格式异常,已按空结果处理"
        return result, None

    def _active_info(self) -> tuple[Any, Any]:
        """读活动模型 ``(spec, switched_by)``;模块缺席/异常降级 ``(None, None)``。"""
        manager = self._resolve_manager()
        if manager is None:
            return None, None
        try:
            status = manager.status()
            if isinstance(status, dict):
                return status.get("spec"), status.get("switched_by")
        except Exception as exc:  # noqa: BLE001 状态读取失败不影响其余字段
            logger.warning("读取活动模型状态失败(%s),按未连接处理", _brief(exc))
        try:
            return manager.get_active(), None
        except Exception:  # noqa: BLE001
            return None, None

    def _keys_status(self) -> dict[str, dict[str, Any]]:
        """密钥配置表:每提供方仅 ``{"configured": bool, "masked": str|None}``。

        掩码优先用 A152 ``masked()``(前 4 位+****);A152 缺席时本地用
        ``security.keys.get_key`` 现算同样的掩码。**绝不含密钥本体**(红线 33),
        出口还有 :meth:`leaks_secret` 二次兜底。
        """
        try:
            from netsentinel.security import keys as keys_mod  # noqa: PLC0415 既有模块

            configured = keys_mod.configured(self.cfg)
        except Exception as exc:  # noqa: BLE001 密钥模块异常不拖垮状态接口
            logger.warning("读取密钥配置状态失败(%s),keys 字段降级为空表", _brief(exc))
            return {}
        if not isinstance(configured, dict):
            return {}
        masked_fn: Callable[[str], Any] | None = None
        try:
            from netsentinel.security import key_api  # noqa: PLC0415 惰性:A152 允许缺席

            masked_fn = getattr(key_api, "masked", None)
        except Exception:  # noqa: BLE001 A152 缺席走本地掩码
            masked_fn = None
        result: dict[str, dict[str, Any]] = {}
        for provider, ok in configured.items():
            masked: str | None = None
            if ok:
                if masked_fn is not None:
                    try:
                        masked = masked_fn(str(provider))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("A152 masked(%s) 失败(%s),回退本地掩码", provider, _brief(exc))
                        masked = None
                if not masked:
                    masked = self._local_mask(str(provider))
            result[str(provider)] = {"configured": bool(ok), "masked": masked}
        return result

    def _local_mask(self, provider: str) -> str | None:
        """A152 缺席时的本地掩码:读密钥 → 前 4 位 + ``****``;读不到为 ``None``。"""
        try:
            from netsentinel.security import keys as keys_mod  # noqa: PLC0415

            raw = keys_mod.get_key(provider, self.cfg)
        except Exception:  # noqa: BLE001
            return None
        raw = raw.strip() if isinstance(raw, str) else ""
        return raw[:4] + "****" if raw else None

    def _secrets(self) -> list[str]:
        """已知密钥样本(cfg.vlm_api_keys 的值);泄漏检测用,绝不外发。"""
        values = getattr(self.cfg, "vlm_api_keys", None)
        if not isinstance(values, dict):
            return []
        return [
            v.strip()
            for v in values.values()
            if isinstance(v, str) and len(v.strip()) >= 8
        ]

    def leaks_secret(self, text: str) -> bool:
        """检测响应文本是否含任一已知密钥本体(红线 33 兜底)。"""
        return any(secret in text for secret in self._secrets())

    def status_payload(self) -> dict[str, Any]:
        """组装 ``/api/status`` 与页面渲染共用的状态字典。"""
        local, scan_error = self.scan_local()
        active, switched_by = self._active_info()
        payload: dict[str, Any] = {
            "active": active,
            "switched_by": switched_by,
            "local": local,
            "keys": self._keys_status(),
        }
        if scan_error:
            payload["local_error"] = scan_error
        return payload

    def render_page(
        self, status: dict[str, Any], active: Any, local_models: list[Any]
    ) -> str:
        """渲染向导页:注入渲染器 > A156 > A146 > 内置兜底(逐级容错)。"""
        if self._page_renderer is not None:
            try:
                html = self._page_renderer(status, active, local_models)
                if isinstance(html, str) and html:
                    return html
                logger.warning("注入的页面渲染器返回空结果,回退默认渲染链")
            except Exception as exc:  # noqa: BLE001 渲染失败回退,页面必须可开
                logger.warning("注入的页面渲染器异常(%s),回退默认渲染链", _brief(exc))
        try:
            from netsentinel.setup.render import render_page  # noqa: PLC0415 惰性:A156 允许缺席

            html = render_page(status, active, local_models)
            if isinstance(html, str) and html:
                return html
        except Exception as exc:  # noqa: BLE001
            logger.warning("A156 渲染未就位或失败(%s),回退 A146", _brief(exc))
        try:
            from netsentinel.setup.page import page_html  # noqa: PLC0415 惰性:A146 允许缺席

            html = page_html()
            if isinstance(html, str) and html:
                return html
        except Exception as exc:  # noqa: BLE001
            logger.warning("A146 页面未就位或失败(%s),使用内置兜底页", _brief(exc))
        return _FALLBACK_PAGE

    def activate(self, spec: str) -> dict[str, Any]:
        """启用模型:A144 ``set_active(spec, switched_by="wizard")``。

        返回 ``{"ok": True, "status": {...}}``;spec 非法 / 校验失败由 A144 抛
        中文 ``ValueError`` → handler 转 400;模块缺席抛 ``_HttpError``(500)。
        """
        manager = self._resolve_manager()
        if manager is None:
            raise _HttpError(500, "模型管理模块(A144)未就位,无法启用模型")
        try:
            manager.set_active(spec, switched_by="wizard")
        except _HttpError:
            raise
        except ValueError as exc:
            raise _HttpError(400, str(exc) or "模型启用失败:spec 无效") from exc
        except Exception as exc:  # noqa: BLE001 非预期异常交分发层 500 兜底
            raise RuntimeError(f"set_active 异常:{_brief(exc)}") from exc
        try:
            status = manager.status()
        except Exception as exc:  # noqa: BLE001 状态读取失败不影响启用结果
            logger.warning("启用后读取状态失败(%s),返回最小状态", _brief(exc))
            status = {"spec": spec, "switched_by": "wizard"}
        if not isinstance(status, dict):
            status = {"spec": spec, "switched_by": "wizard"}
        return {"ok": True, "status": status}

    def set_key(self, provider: str, key: str) -> dict[str, Any]:
        """密钥落盘(A152);只返回 ``{"ok","masked"}``,绝不回显 key 本体。"""
        acceptor = self._resolve_key_acceptor()
        if acceptor is None:
            raise _HttpError(500, "密钥接口模块(A152)未就位,无法写入密钥")
        try:
            result = acceptor(provider, key)
        except _HttpError:
            raise
        except ValueError as exc:
            raise _HttpError(400, str(exc) or "密钥写入失败:校验未通过") from exc
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"accept_key_input 异常:{_brief(exc)}") from exc
        if not isinstance(result, dict):
            raise _HttpError(500, "密钥接口返回结果格式异常")
        return {
            "ok": bool(result.get("ok", True)),
            "masked": result.get("masked"),
        }

    def test_connection(self, spec: str) -> dict[str, Any]:
        """连通性测试(A147)结果透传;接口内错误(如 VlmConfigError)已在
        结果体内以中文 ``error`` 字段返回,HTTP 层保持 200。"""
        tester = self._resolve_tester()
        if tester is None:
            raise _HttpError(500, "连通性测试模块(A147)未就位,无法测试连接")
        try:
            result = tester(spec)
        except _HttpError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"test_connection 异常:{_brief(exc)}") from exc
        if not isinstance(result, dict):
            raise _HttpError(500, "连通性测试返回结果格式异常")
        return result
