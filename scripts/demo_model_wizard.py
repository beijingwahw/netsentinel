# -*- coding: utf-8 -*-
"""净网哨兵(NetSentinel)V8 模型连接向导离线演示脚本 —— A158。

依据 CONTRACTS-V8.md §3 A158 条目与 §0 红线 32-34,在完全离线的环境中演示
"自动接管 → 连接向导 → 手动切换 → 离线桩兜底"的完整闭环:

① mock 本地视觉服务:127.0.0.1 随机回环端口,``/v1/models`` 返回
   llava:13b / qwen2.5vl:7b / llama3:8b(2 个视觉 + 1 个纯文本);
② 自动接管(真实链):``pipeline.takeover.takeover_once(cfg)`` 内部走真 A143
   扫描 → 真 A144 落盘。**映射陷阱**(与 tests/test_v8_e2e.py 同源):mock 起
   在随机端口,A143 ``PORT_PROVIDERS`` 会把非标准端口判为 ``openai-compat``,
   而 A155 对不在统一目录中的提供方一律跳过——本演示的处理是把该端口
   **临时注册**进 ``netsentinel.vision.local_probe.PORT_PROVIDERS``(运行时
   改 dict,finally 还原),让真扫描器映射出 "ollama" → 接管命中 llava:13b;
③ 向导服务器(A154 ``ensure_setup_server`` 守护 → 真 A145):不弹浏览器、
   打印地址,依次演示 /api/status(活动模型)→ /api/probe(mock 命中)→
   /api/activate 切到 qwen2.5vl:7b(真 A147 连通性校验打到 mock)→
   /api/status(新活动)→ /api/activate 切 stub(红字警示)→
   /api/test(mock 校验 ok);
④ 红线 33/34 页面证据:GET / 的 HTML 中"离线桩,非模型判定"与
   ``type="password"`` 密钥输入逐行打印;
⑤ 收尾:全程零外呼(仅本机回环)、密钥只进不显;生产入口
   ``python -m netsentinel.modelmgr serve``。

安全红线(与团队契约一致):

- 红线 32:mock 与向导服务都只绑 127.0.0.1;演示结束打印自证
  (mock 收到的每个请求客户端均为回环、仅 GET /v1/models 清点、零评分 POST);
- 红线 33:不写入、不回显任何密钥;页面证据仅证明 password 语义输入;
- 红线 34:切 stub 后红字打印"离线桩,非模型判定"警示;
- ``NETSENTINEL_NO_BROWSER`` 语义遵守:该环境变量非空 → 明确提示"不弹
  浏览器";本演示无论如何都以参数 ``open_browser=False`` 起服务(参数 >
  环境变量 > 配置 的三态裁决中参数优先),保证零弹窗、零外呼。

用法:NETSENTINEL_NO_BROWSER=1 python scripts/demo_model_wizard.py
退出码:0=演示成功;2=V8 兄弟模块未就位;1=其他错误。
"""
from __future__ import annotations

import importlib
import json
import shutil
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

#: 项目根目录(脚本位于 <root>/scripts/)
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from netsentinel import telemetry  # noqa: E402
from netsentinel.contracts import Config  # noqa: E402

#: 演示依赖的 V8 兄弟模块(模块名 → 中文角色说明);任一缺失都以退出码 2 结束
REQUIRED_SIBLING_MODULES: list[tuple[str, str]] = [
    ("netsentinel.pipeline.takeover", "自动接管(A155)"),
    ("netsentinel.vision.local_probe", "本地视觉服务探测(A143)"),
    ("netsentinel.vision.model_manager", "活动模型管理器(A144)"),
    ("netsentinel.vision.connectivity", "连通性测试(A147)"),
    ("netsentinel.vision.capability", "视觉模型判定(A150)"),
    ("netsentinel.setup.server", "连接向导服务(A145)"),
    ("netsentinel.setup.daemon", "向导常驻守护(A154)"),
]

#: mock 本地视觉服务的模型清单:2 个视觉 + 1 个纯文本(纯文本必须被过滤)
LOCAL_MODELS: list[str] = ["llava:13b", "qwen2.5vl:7b", "llama3:8b"]

#: 红线 34 的固定警示文案(与 setup/page.py STUB_NOTE_TEXT 同源)
STUB_NOTE_TEXT = "离线桩,非模型判定"

#: 不弹浏览器的环境变量开关(与 A149/A154/A145 同名同语义)
NO_BROWSER_ENV = "NETSENTINEL_NO_BROWSER"


# ---------------------------------------------------------------------------
# 输出工具(UTF-8 / 红字)
# ---------------------------------------------------------------------------


def _ensure_utf8_stdio() -> None:
    """Windows 管道/终端非 UTF-8 时切换标准输出编码,避免中文打印报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and stream.encoding and stream.encoding.lower() not in (
                "utf-8",
                "utf8",
            ):
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 重新配置失败不影响主流程
            pass


_ANSI_OK = False


def _enable_ansi() -> None:
    """尝试启用终端 ANSI 转义(Windows 需开 VT 模式);失败则红字退化为前缀。"""
    global _ANSI_OK
    try:
        if not sys.stdout.isatty():
            return
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        _ANSI_OK = True
    except Exception:  # noqa: BLE001 - 着色是锦上添花,绝不影响主流程
        _ANSI_OK = False


def _red(text: str) -> str:
    """红字输出(终端不支持 ANSI 时退化为【红】前缀,语义不丢)。"""
    if _ANSI_OK:
        return f"\033[31m{text}\033[0m"
    return f"【红】{text}"


def _disp_width(text: str) -> int:
    """中文等东亚宽字符按 2 列计的显示宽度(纯标准库近似,与既有演示同款)。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _section(title: str) -> None:
    """打印中文分节横幅(按显示宽度对齐补线)。"""
    print()
    used = _disp_width(f"── {title} ")
    print(f"── {title} " + "─" * max(3, 50 - used))
    sys.stdout.flush()


def _require(condition: Any, message: str) -> None:
    """演示自证断言:条件不成立即抛错(退出码 1)。"""
    if not condition:
        raise RuntimeError(f"演示自证失败:{message}")


# ---------------------------------------------------------------------------
# HTTP 小工具(仅回环)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """取一个系统分配的空闲回环端口(立即释放;存在极小竞态,可接受)。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http(method: str, url: str, payload: dict | None = None, timeout: float = 10.0):
    """对回环地址发一次 HTTP 请求,返回 (状态码, 响应全文文本)。"""
    import urllib.error
    import urllib.request

    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 仅回环
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# mock 本地视觉服务(仅回环;记录客户端地址与请求方法,供红线 32 自证)
# ---------------------------------------------------------------------------


class _MockVisionServer(ThreadingHTTPServer):
    """带状态记录的 mock /v1/models 服务(daemon 线程池,仅绑 127.0.0.1)。"""

    daemon_threads = True

    def __init__(self, models: list[str], port: int = 0) -> None:
        self.models = list(models)
        #: (客户端IP, 方法, 路径) 三元组记录:红线 32 自证的数据源
        self.requests: list[tuple[str, str, str]] = []
        self._lock = threading.Lock()
        super().__init__(("127.0.0.1", port), _MockVisionHandler)

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def record(self, client: str, method: str, path: str) -> None:
        with self._lock:
            self.requests.append((client, method, path))

    def snapshot(self) -> list[tuple[str, str, str]]:
        with self._lock:
            return list(self.requests)


class _MockVisionHandler(BaseHTTPRequestHandler):
    """OpenAI 兼容模型清点端点(GET /v1/models 与 /models 等价)。"""

    server: _MockVisionServer
    server_version = "NetSentinelDemoWizard/8"  # noqa: N815 - http.server 约定命名

    def do_GET(self) -> None:  # noqa: N802
        import urllib.parse

        path = urllib.parse.urlsplit(self.path).path
        self.server.record(self.client_address[0], "GET", path)
        if path in ("/v1/models", "/models"):
            body = json.dumps(
                {"object": "list", "data": [{"id": name} for name in self.server.models]}
            ).encode("utf-8")
            self._reply(200, body)
        else:
            self._reply(404, json.dumps({"error": "not found"}).encode("utf-8"))

    def do_POST(self) -> None:  # noqa: N802
        # 本地清点链路绝不应有评分 POST(红线 32/34:本地零评分外呼);
        # 读取并丢弃请求体后记录,供自证"零 POST"。
        import urllib.parse

        length = int(self.headers.get("Content-Length") or 0)
        if length > 0:
            self.rfile.read(min(length, 1 << 20))
        path = urllib.parse.urlsplit(self.path).path
        self.server.record(self.client_address[0], "POST", path)
        self._reply(404, json.dumps({"error": "not found"}).encode("utf-8"))

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionError, BrokenPipeError):
            pass

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # 静默:演示输出不刷屏


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def check_sibling_modules() -> list[str]:
    """逐一导入演示依赖的 V8 兄弟模块,返回未就位模块的中文描述列表。"""
    missing: list[str] = []
    for name, role in REQUIRED_SIBLING_MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - 并行开发中未就位/依赖缺失均视为未就位
            missing.append(f"{name} —— {role}(导入失败:{exc})")
    return missing


def _build_demo_config(temp_root: Path, mock_port: int) -> Config:
    """构建演示配置:产物全部落临时目录,探测端口只指 mock,零残留。"""
    cfg = Config()
    data_dir = temp_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg.data_dir = str(data_dir)
    cfg.audit_path = str(data_dir / "audit.jsonl")
    cfg.model_runtime_path = str(data_dir / "model_runtime.json")
    cfg.setup_port = _free_port()  # 向导端口也走随机回环,避免与本机 8766 冲突
    cfg.local_probe_ports = [str(mock_port)]  # 只探测 mock 端口(红线 32:绝不扫段)
    cfg.vlm_provider_base_urls = {
        "ollama": f"http://127.0.0.1:{mock_port}/v1"
    }  # 真 A147 连通性校验经目录覆盖打到 mock
    cfg.vlm_api_keys = {}  # 演示不配置任何密钥(密钥只进不显,红线 33)
    return cfg


def _print_status_line(label: str, text: str) -> None:
    print(f"{label}{text}")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001 - 预留参数
    """演示主流程;返回进程退出码。"""
    _ensure_utf8_stdio()
    _enable_ansi()
    telemetry.reset()
    print("═" * 62)
    print(" 净网哨兵(NetSentinel)V8 模型连接向导离线演示 —— A158")
    print(" 安全提示:全程零外呼(仅本机回环)、零真实提交;不写入任何密钥")
    print("═" * 62)

    # 前置:V8 兄弟模块就位检查(缺席则打印清单并以退出码 2 结束)
    missing = check_sibling_modules()
    if missing:
        print()
        print("演示无法启动:以下 V8 兄弟模块尚未就位 ——")
        for item in missing:
            print(f"  - {item}")
        print()
        print("并行开发仍在进行,请等待对应代理完成后再运行本演示。")
        return 2

    # 演示主角(上方检查已保证可导入)
    from netsentinel.pipeline.takeover import takeover_once
    from netsentinel.setup.daemon import ensure_setup_server, stop_setup_server
    from netsentinel.setup.server import SetupServer
    from netsentinel.vision import local_probe
    from netsentinel.vision.model_manager import ModelManager

    temp_root = Path(tempfile.mkdtemp(prefix="netsentinel_demo_wizard_"))
    mock: _MockVisionServer | None = None
    mock_thread: threading.Thread | None = None
    port_key: str | None = None  # 本次临时注册进 PORT_PROVIDERS 的端口键
    try:
        # ------------------------------------------------------------------
        _section("① mock 本地视觉服务(随机回环端口)")
        mock = _MockVisionServer(list(LOCAL_MODELS))
        mock_thread = threading.Thread(
            target=mock.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
            name="demo-wizard-mock",
        )
        mock_thread.start()
        _print_status_line(
            "  服务地址:", f"http://127.0.0.1:{mock.port}/v1/models(仅绑定 127.0.0.1)"
        )
        _print_status_line(
            "  模型清单:",
            "、".join(LOCAL_MODELS) + "(llava/qwen2.5vl 为视觉款,llama3 为纯文本,应被过滤)",
        )

        cfg = _build_demo_config(temp_root, mock.port)

        # ------------------------------------------------------------------
        _section("② 自动接管(真实链:takeover_once → A143 扫描 → A144 落盘)")
        # 映射陷阱(tests/test_v8_e2e.py 同源):随机端口不在 A143 PORT_PROVIDERS
        # 目录中会被判为 "openai-compat",而接管对不在统一目录中的提供方一律跳过。
        # 处理:运行时把该端口临时注册为 "ollama"(演示注入,结束还原)→ 真实
        # 扫描/接管/向导全链可命中。
        port_key = str(mock.port)
        local_probe.PORT_PROVIDERS[port_key] = "ollama"
        print(
            f"  端口映射注入:PORT_PROVIDERS[{port_key}] = \"ollama\""
            "(运行时注册,演示结束还原;非标准端口否则被判 openai-compat 而跳过)"
        )
        # 允许本进程重复演示:清空 A155 会话幂等标记
        reset_session = getattr(
            importlib.import_module("netsentinel.pipeline.takeover"), "reset_session", None
        )
        if callable(reset_session):
            reset_session()

        with telemetry.timer("demo.takeover"):
            result = takeover_once(cfg)  # 真实链:内部真 A143 扫描 → 真 A144 set_active
        _require(result.get("action") == "local", f"接管应命中本地,实际:{result}")
        _require(result.get("spec") == "ollama:llava:13b", f"接管应命中 llava,实际:{result}")
        _print_status_line("  takeover_once → ", f"action={result['action']}(detail:{result['detail']})")
        _print_status_line(
            "  自动接管:",
            f"{result['spec']}(首个视觉模型;纯文本 llama3:8b 已被 A150 过滤;零连通性重探)",
        )
        runtime = ModelManager(cfg.model_runtime_path).status()  # 真 A144 从磁盘回读
        _print_status_line(
            "  model_runtime.json 回读:",
            f"spec={runtime.get('spec')},switched_by={runtime.get('switched_by')}",
        )
        _require(runtime.get("spec") == "ollama:llava:13b", "runtime 回读应为接管 spec")
        _require(runtime.get("switched_by") == "takeover", "runtime 来源应为 takeover")

        # ------------------------------------------------------------------
        _section("③ 向导服务器(ensure_setup_server,A154 守护 → 真 A145)")
        no_browser = bool(__import__("os").environ.get(NO_BROWSER_ENV))
        if no_browser:
            print(f"  {NO_BROWSER_ENV}=1:遵守语义,不弹浏览器,仅打印地址")
        else:
            print(
                f"  {NO_BROWSER_ENV} 未设置:演示仍以参数 open_browser=False 起服务"
                "(三态裁决中参数优先),保证零弹窗"
            )

        def _wizard_factory(c: Config) -> SetupServer:
            """A154 工厂注入(与测试同款接缝):向导的 A144 管理器携带演示 cfg,
            使真 A147 连通性校验按 ``vlm_provider_base_urls`` 覆盖打到 mock。"""
            return SetupServer(c, manager=ModelManager(c.model_runtime_path, cfg=c))

        with telemetry.timer("demo.wizard"):
            wizard_url = ensure_setup_server(
                cfg, open_browser=False, factory=_wizard_factory
            )
        _require(isinstance(wizard_url, str) and wizard_url.startswith("http://127.0.0.1:"),
                 f"向导地址应为回环 URL,实际:{wizard_url}")
        _print_status_line("  向导地址:", wizard_url)

        # -- 状态:接管后的活动模型 --
        status, text = _http("POST", f"{wizard_url}api/status")
        _require(status == 200, f"/api/status 应 200,实际 {status}:{text}")
        payload = json.loads(text)
        _print_status_line(
            "  POST /api/status → ",
            f"活动模型:{payload.get('active')}(来源:{payload.get('switched_by')})",
        )
        _require(payload.get("active") == "ollama:llava:13b", "向导状态应显示接管的活动模型")

        # -- 扫描:mock 命中 --
        status, text = _http("POST", f"{wizard_url}api/probe")
        _require(status == 200, f"/api/probe 应 200,实际 {status}:{text}")
        rows = json.loads(text).get("local") or []
        hits = [r for r in rows if r.get("ok")]
        _require(len(hits) == 1, f"/api/probe 应命中 1 个本地服务,实际:{rows}")
        hit = hits[0]
        _print_status_line(
            "  POST /api/probe → ",
            f"命中:provider={hit['provider']},base_url={hit['base_url']},"
            f"视觉模型={hit['models']}",
        )
        _require(hit["provider"] == "ollama", "扫描应映射出 ollama(端口已注册)")
        _require(hit["models"] == ["llava:13b", "qwen2.5vl:7b"], "扫描应只剩两个视觉模型")

        # -- 切换:qwen2.5vl(真 A147 连通性校验经 mock)--
        status, text = _http("POST", f"{wizard_url}api/activate", {"spec": "ollama:qwen2.5vl:7b"})
        _require(status == 200, f"/api/activate(qwen) 应 200,实际 {status}:{text}")
        _print_status_line(
            "  POST /api/activate ollama:qwen2.5vl:7b → ",
            f"ok={json.loads(text)['ok']}(真 A147 连通性校验经 mock /v1/models 通过)",
        )
        status, text = _http("POST", f"{wizard_url}api/status")
        payload = json.loads(text)
        _print_status_line(
            "  POST /api/status → ",
            f"活动模型:{payload.get('active')}(来源:{payload.get('switched_by')})",
        )
        _require(payload.get("active") == "ollama:qwen2.5vl:7b", "切换后活动模型应为 qwen2.5vl")
        _require(payload.get("switched_by") == "wizard", "切换来源应为 wizard")

        # -- 切换:stub(离线桩,红字警示)--
        status, text = _http("POST", f"{wizard_url}api/activate", {"spec": "stub"})
        _require(status == 200, f"/api/activate(stub) 应 200,实际 {status}:{text}")
        stub_status = json.loads(text)["status"]
        _print_status_line(
            "  POST /api/activate stub → ",
            f"ok=True(spec={stub_status.get('spec')},来源:{stub_status.get('switched_by')})",
        )
        status, text = _http("POST", f"{wizard_url}api/status")
        _require(json.loads(text).get("active") == "stub", "切桩后活动模型应为 stub")
        _print_status_line("  POST /api/status → ", "活动模型:stub(离线桩,非模型判定)")

        # -- 连通性测试:mock 校验 ok(活动模型是 stub 也可测任意 spec)--
        status, text = _http("POST", f"{wizard_url}api/test", {"spec": "ollama:qwen2.5vl:7b"})
        _require(status == 200, f"/api/test 应 200,实际 {status}:{text}")
        test_result = json.loads(text)
        _require(test_result.get("ok") is True, f"/api/test 应通过,实际:{test_result}")
        _print_status_line(
            "  POST /api/test ollama:qwen2.5vl:7b → ",
            f"ok={test_result['ok']},mode={test_result.get('mode')},"
            f"model={test_result.get('model')},latency={test_result.get('latency_ms')}ms",
        )

        # ------------------------------------------------------------------
        _section("④ 红线 33/34 页面证据(GET / 向导页 HTML)")
        status, html = _http("GET", wizard_url)
        _require(status == 200, f"GET / 应 200,实际 {status}")
        _require(STUB_NOTE_TEXT in html, "页面应包含“离线桩,非模型判定”(红线 34)")
        _require('type="password"' in html, "页面应包含 password 密钥输入(红线 33)")
        for line in html.splitlines():
            if STUB_NOTE_TEXT in line:
                print(f"  红线 34(离线桩明示):{line.strip()}")
            if 'type="password"' in line:
                print(f"  红线 33(密钥只进不显):{line.strip()}")
        print(_red(f"  ⚠ 已切换到离线桩:{STUB_NOTE_TEXT} —— 仅作流程兜底,结论不代表任何视觉模型判定"))
        _print_status_line(
            "  证据确认:",
            "页面同时包含“离线桩,非模型判定”警示与 password 语义密钥输入(掩码回显)",
        )

        # ------------------------------------------------------------------
        _section("⑤ 收尾")
        requests = mock.snapshot()
        _require(requests, "mock 应收到过清点请求(接管/扫描/校验链路)")
        for client_ip, method, path in requests:
            _require(client_ip == "127.0.0.1", f"红线 32:出现非回环客户端 {client_ip}")
            _require(method == "GET", f"红线 32:本地链路出现非清点请求 {method} {path}")
            _require(path in ("/v1/models", "/models"), f"红线 32:越界路径 {path}")
        _print_status_line(
            "  红线 32 自证: ",
            f"mock 共收到 {len(requests)} 笔请求,全部来自 127.0.0.1、"
            "全部为 GET /v1|/models 清点、零评分 POST",
        )
        print("  全程零外呼(仅本机回环)、密钥只进不显(本演示未写入任何密钥)")
        print("  生产环境启动向导(常驻切换器):python -m netsentinel.modelmgr serve")

        stop_setup_server()  # 停 A154 守护(顺带清理临时 data 目录里的锁文件)
        mock.shutdown()
        mock.server_close()
        mock_thread.join(timeout=5)
        if port_key is not None:
            local_probe.PORT_PROVIDERS.pop(port_key, None)  # 还原端口映射
            port_key = None
        shutil.rmtree(temp_root, ignore_errors=True)
        print("  收尾清理:向导守护已停止、mock 已停止、PORT_PROVIDERS 已还原、临时目录已清理")

        print()
        print("═" * 62)
        print(" 演示完成:自动接管 → 向导切换 → 离线桩兜底,全程仅本机回环(退出码 0)")
        print("═" * 62)
        return 0
    except Exception as exc:  # noqa: BLE001 - 演示脚本顶层兜底
        import traceback

        print(f"演示失败:{type(exc).__name__}:{exc}", file=sys.stderr)
        traceback.print_exc()
        return 1
    finally:
        # 兜底清理(正常路径已在上方完成;此处保证异常路径也零残留)
        try:
            stop_setup_server()
        except Exception:  # noqa: BLE001
            pass
        if mock is not None and mock.socket is not None:
            try:
                mock.shutdown()
                mock.server_close()
            except Exception:  # noqa: BLE001
                pass
        if mock_thread is not None:
            mock_thread.join(timeout=5)
        if port_key is not None:
            local_probe.PORT_PROVIDERS.pop(port_key, None)
        shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
