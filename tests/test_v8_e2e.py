# -*- coding: utf-8 -*-
"""A157:V8 端到端测试(自动接管 → 连接向导 → 手动切换 → 密钥只进不显)。

依据 CONTRACTS-V8.md §3 A157 行与 §0 红线 32/33/34,离线覆盖四条主链:

1. **自动接管**:本地 mock ``/v1/models``(含 ``llava:13b`` 与 ``llama3:8b``)
   → ``pipeline.takeover.takeover_once`` → ``action=="local"``。映射陷阱:
   非标准端口的提供方为 ``"openai-compat"``(A143 ``PORT_PROVIDERS`` 之外
   一律如此),而 ``ModelManager.set_active`` 经 ``providers.parse_spec`` 拒
   未知提供方——A155 落地口径是**跳过不在统一目录中的提供方**(绝不产出
   非法 spec),故非标准端口用例断言:真 A143 映射出 ``openai-compat``、
   接管不把非法 spec 推进 manager(fake manager 记录零调用)、整体落向导;
   标准映射用例两条:注入 ``provider="ollama"`` 的 fake scanner 走真 manager
   (tmp ``model_runtime_path``),以及 mock 服务起在 127.0.0.1:11434(被占
   则 skip)由**真扫描器**发现 → ``get_active()=="ollama:llava:13b"`` 且
   ``apply(cfg)`` 后 ``get_classifier(cfg.classifier).name`` 含 ``"ollama"``;
2. **无模型 → 向导**:清空环境(NETSENTINEL_NO_BROWSER=1、tmp 家目录、tmp
   data/audit、无密钥、空探测端口)→ ``takeover_once`` 落 wizard 并经
   **真** A149→A154 链拉起真 A145 服务器;``ensure_setup``(随机端口
   server_factory 注入)→ ``GET /`` 200 且 HTML 含"离线桩,非模型判定"
   (红线 34)与 ``type="password"``(红线 33)→ ``POST /api/activate
   {"spec":"stub"}`` → 真manager ``get_active()=="stub"``;
3. **切换生效**:activate ``"stub"`` → ``ModelManager.apply(cfg)`` →
   ``cfg.ensemble_members==["stub"]``;activate 回本地 spec(真 A147 连通性
   校验打到 mock 服务)→ apply → classifier 恢复为可构建的统一 VLM 分类器;
4. **密钥只进不显**:**真** A152 流程 ``POST /api/setkey``(glm)→ 响应仅
   ``{"ok","masked"}`` 且**响应全文不含密钥本体**(红线 33);二次
   ``/api/status`` 含 masked 不含本体。

红线专项:

- **红线 32**:mock 与向导服务都只绑 ``127.0.0.1``;所有打到 mock 的请求
  客户端地址必须为回环;本地连通性只 GET ``/models`` 清点,**零 POST 评分**;
  缺省探测端口目录恰为契约 §1 的 4 个端口;
- **红线 33**:setkey/status 响应全文逐字节不含密钥本体;
- **红线 34**:向导页明示"离线桩,非模型判定";stub 切换整套替换
  ``ensemble_members``。

并行未就位标注:A143–A156 本轮已全部就位(接管/探测/向导链真模块直跑);
对仍可能缺席的 ``pipeline.takeover``(A155)/``vision.local_probe``(A143)
保留用例级 ``pytest.importorskip`` 门控——未就位时 skip 并在 reason 标注,
不误报失败。

隔离纪律:每条用例独立 tmp(runtime / 家目录 / 密钥文件 / data 目录 /
审计文件);模块级 autouse fixture 负责 NETSENTINEL_NO_BROWSER、密钥环境
变量清空、A149 触发器单例 / A154 守护单例 / A155 会话标记复位,以及
**全部服务器与线程的收尾停止**。
"""
from __future__ import annotations

import importlib
import json
import pathlib
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from netsentinel.contracts import Config
from netsentinel.security import keys
from netsentinel.setup import trigger as setup_trigger
from netsentinel.setup.server import SetupServer
from netsentinel.vision.capability import filter_vision, is_vision_model
from netsentinel.vision.classifier_base import NsfwClassifier, get_classifier
from netsentinel.vision.model_manager import ModelManager

# ---------------------------------------------------------------------------
# 受控样本
# ---------------------------------------------------------------------------

#: mock 本地视觉服务的模型清单:1 个视觉 + 1 个纯文本(接管必须挑中视觉款)
LOCAL_MODELS = ["llava:13b", "llama3:8b"]

#: 红线 33 专项:密钥本体样本(仅在本文件内作为"必须被掩蔽"的敏感串)
RAW_KEY = "sk-test-12345678"
MASKED_KEY = "sk-t****"

#: 红线 32:契约 §1 登记的默认探测端口目录(绝不扫回环全端口段)
CONTRACT_PROBE_PORTS = {"11434", "1234", "8000", "9997"}

#: ollama 惯例端口(A143 映射目录标准端口;被占时该用例 skip)
OLLAMA_PORT = 11434

#: 并行开发中允许缺席的兄弟模块(用例级 importorskip 标注;当前已就位)
A155_TAKEOVER = "netsentinel.pipeline.takeover"
A143_LOCAL_PROBE = "netsentinel.vision.local_probe"


# ---------------------------------------------------------------------------
# mock 本地视觉服务(仅回环;记录客户端地址与请求方法,供红线 32 断言)
# ---------------------------------------------------------------------------


class _MockVisionServer(ThreadingHTTPServer):
    """带状态记录的 mock /v1/models 服务(daemon 线程池,仅绑 127.0.0.1)。"""

    daemon_threads = True

    def __init__(self, models: list[str], port: int = 0) -> None:
        self.models = list(models)
        #: (客户端IP, 方法, 路径) 三元组记录:红线 32 断言的数据源
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
    server_version = "MockLocalVision/8"  # noqa: N815 - http.server 约定命名

    def do_GET(self) -> None:  # noqa: N802
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
        # 读取并丢弃请求体后记录,供断言"零 POST"。
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
        pass  # 静默:测试内不刷屏


# ---------------------------------------------------------------------------
# 服务器注册表与模块级 fixture(起停 / 隔离 / 收尾)
# ---------------------------------------------------------------------------

#: 本测试模块启动的全部服务器(mock + 向导),autouse fixture 收尾统一停止
_ACTIVE_SERVERS: list[Any] = []


def _stop_all_servers() -> None:
    """停止并清空注册表里的全部服务器(幂等;线程 join 限时防挂死)。"""
    while _ACTIVE_SERVERS:
        srv = _ACTIVE_SERVERS.pop()
        try:
            if isinstance(srv, SetupServer):
                srv.stop()
            else:
                srv.shutdown()
                srv.server_close()
        except Exception:  # noqa: BLE001 - 收尾清理绝不抛出掩盖测试结果
            pass


def _reset_daemon_singleton() -> None:
    """复位 A154 守护单例(停服 + 清模块状态;缺席即跳过)。"""
    try:
        daemon = importlib.import_module("netsentinel.setup.daemon")
    except Exception:  # noqa: BLE001 - 未就位属并行常态
        return
    try:
        daemon.stop_setup_server()
    except Exception:  # noqa: BLE001
        pass
    daemon._server = None  # noqa: SLF001 - 测试复位模块级单例状态
    daemon._url = None  # noqa: SLF001
    daemon._lock_file = None  # noqa: SLF001
    daemon._owns_lock = False  # noqa: SLF001


def _reset_takeover_session() -> None:
    """复位 A155 会话幂等标记(优先 reset_session();缺席即跳过)。"""
    try:
        takeover = importlib.import_module(A155_TAKEOVER)
    except Exception:  # noqa: BLE001 - 未就位属并行常态
        return
    reset = getattr(takeover, "reset_session", None)
    if callable(reset):
        reset()


def _clear_provider_key_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空全部 20 家提供方的密钥环境变量(两形态),保证"无任何模型"。"""
    for provider in keys.BUILTIN_PROVIDERS:
        upper = provider.upper()
        monkeypatch.delenv(f"NETSENTINEL_{upper}_API_KEY", raising=False)
        monkeypatch.delenv(f"{upper}_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _e2e_env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    """每条用例的统一隔离环境 + 收尾(模块级起停保障)。

    - ``NETSENTINEL_NO_BROWSER=1``:任何入口都不得弹浏览器;
    - 家目录指向 tmp(密钥文件 ``~/.netsentinel/keys`` 与真实机器隔离),
      并关闭 Windows icacls 收权子进程(tmp 目录上拖慢且无意义);
    - 清空 20 家提供方密钥环境变量;
    - 复位 A149 向导触发器单例、A154 守护单例、A155 会话标记
      (三个模块级可变状态,不复位会跨用例串扰);
    - teardown:停止本用例注册的全部服务器(mock / 向导)并再次复位单例。
    """
    monkeypatch.setenv("NETSENTINEL_NO_BROWSER", "1")
    home_dir = tmp_path / "home"
    home_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home_dir))
    monkeypatch.setattr(keys, "_tighten_key_file_permissions", lambda path: None)
    _clear_provider_key_envs(monkeypatch)
    monkeypatch.setattr(setup_trigger, "_started_url", None)
    _reset_daemon_singleton()
    _reset_takeover_session()
    yield
    _stop_all_servers()
    _reset_daemon_singleton()


# ---------------------------------------------------------------------------
# 小工具:HTTP 客户端 / 配置工厂 / 向导工厂
# ---------------------------------------------------------------------------


def _http(method: str, url: str, payload: dict | None = None, timeout: float = 10.0):
    """对回环地址发一次 HTTP 请求,返回 (状态码, 头 dict, 响应全文文本)。

    连接被拒 / 超时等 URLError 返回 ``(0, {}, "")``——状态码 0 表示"无 HTTP
    应答"(用于断言服务器确已关闭)。
    """
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 仅回环
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read().decode("utf-8", "replace")
    except urllib.error.URLError:
        return 0, {}, ""


def _port_free(port: int) -> bool:
    """端口在回环上是否可绑(探测后立即释放)。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _free_port() -> int:
    """取一个系统分配的空闲回环端口(立即释放,存在极小竞态,可接受)。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _make_cfg(
    tmp_path: pathlib.Path,
    *,
    probe_ports: list[str] | None = None,
    base_urls: dict[str, str] | None = None,
) -> Config:
    """端到端用 Config:runtime / data / 审计全部落 tmp,密钥表清空。

    ``data_dir`` / ``audit_path`` 必须指到 tmp——A154 锁文件与 A155 审计
    都按 cfg 落盘,不隔离会污染仓库 data/ 目录。
    """
    cfg = Config()
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    cfg.data_dir = str(data_dir)
    cfg.audit_path = str(data_dir / "audit.jsonl")
    cfg.model_runtime_path = str(data_dir / "model_runtime.json")
    cfg.setup_port = _free_port()
    cfg.vlm_api_keys = {}
    cfg.onboarding_auto_open = False
    if probe_ports is not None:
        cfg.local_probe_ports = [str(p) for p in probe_ports]
    if base_urls:
        cfg.vlm_provider_base_urls = dict(base_urls)
    return cfg


def _start_mock(models: list[str] | None = None, port: int = 0) -> _MockVisionServer:
    """起 mock 本地视觉服务(缺省随机回环端口)并注册收尾。"""
    srv = _MockVisionServer(models if models is not None else list(LOCAL_MODELS), port)
    thread = threading.Thread(
        target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    _ACTIVE_SERVERS.append(srv)
    return srv


def _start_wizard(
    cfg: Config, *, manager: Any = None, tester: Any = None
) -> SetupServer:
    """起真 A145 向导服务(随机回环端口)并注册收尾。"""
    srv = SetupServer(cfg, manager=manager, tester=tester)
    srv.start(port=0, open_browser=False)
    _ACTIVE_SERVERS.append(srv)
    return srv


def _wizard_factory(**kwargs: Any):
    """ensure_setup 的 server_factory 注入:起**真**向导服务(随机端口注入)。"""

    def factory(cfg: Config) -> str:
        srv = _start_wizard(cfg, **kwargs)
        return str(srv.url)

    return factory


class FakeManager:
    """A144 替身:只记录 set_active 调用(验证接管逻辑给出的 spec 映射)。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def set_active(self, spec: str, *, switched_by: str = "manual", **_: Any) -> None:
        self.calls.append((spec, switched_by))

    def get_active(self) -> str | None:
        return self.calls[-1][0] if self.calls else None

    def status(self) -> dict:
        return {"spec": self.get_active(), "switched_by": "wizard", "switched_at": ""}

    def apply(self, cfg: Any) -> None:
        return None


class FakeScanner:
    """A143 替身:返回固定扫描结果(标准端口 provider="ollama" 形态)。"""

    def __init__(self, result: list[dict]) -> None:
        self.result = result
        self.calls = 0

    def scan(self) -> list[dict]:
        self.calls += 1
        return list(self.result)


# ---------------------------------------------------------------------------
# 用例组 1:自动接管(A155/A143 未就位时 importorskip 标注)
# ---------------------------------------------------------------------------


def test_takeover_nonstandard_port_openai_compat_mapping(tmp_path: pathlib.Path) -> None:
    """接管①:非标准端口 → A143 映射 openai-compat;接管不产出非法 spec。

    mock /v1/models(含 llava:13b 与 llama3:8b)起在**随机端口**(不在
    A143 ``PORT_PROVIDERS`` 中)→ 真 ``LocalVisionScanner`` 映射出
    ``provider=="openai-compat"`` 且只剩视觉款;``openai-compat`` 不在
    ``providers.PROVIDERS`` 目录中(真 ``ModelManager.set_active`` 会经
    ``parse_spec`` 拒绝),A155 落地口径为跳过该条目——注入 fake manager
    记录,断言**零调用**(绝不把非法 spec 推进 manager),整体继续落向导。
    """
    takeover = pytest.importorskip(
        A155_TAKEOVER, reason="A155 pipeline.takeover 未就位(并行开发中)"
    )
    local_probe = pytest.importorskip(
        A143_LOCAL_PROBE, reason="A143 vision.local_probe 未就位(需要真端口→提供方映射)"
    )
    mock = _start_mock()
    cfg = _make_cfg(tmp_path, probe_ports=[str(mock.port)])

    # ① 真 A143:非标准端口 → openai-compat;视觉过滤只剩 llava:13b
    rows = local_probe.LocalVisionScanner([str(mock.port)]).scan()
    assert len(rows) == 1
    row = rows[0]
    assert row["ok"] is True
    assert row["provider"] == "openai-compat"
    assert row["models"] == ["llava:13b"]

    # ② 真 A155 + fake manager:未映射提供方被跳过,不产出非法 spec
    fake_manager = FakeManager()
    result = takeover.takeover_once(cfg, manager=fake_manager)
    assert result["action"] == "wizard"  # 本地不可接 → 全无模型 → 向导
    assert result.get("spec") is None
    assert fake_manager.calls == []  # 绝无 openai-compat:... 进 set_active
    assert isinstance(result["detail"], str) and result["detail"]


def test_takeover_fake_scanner_ollama_real_manager(tmp_path: pathlib.Path) -> None:
    """接管②:provider="ollama" 的扫描结果 → 真 manager 落位并可构建分类器。

    注入 scanner 返回 provider="ollama"(标准端口映射形态)的 fake,走**真**
    ModelManager(tmp model_runtime_path)→ 断言 get_active()==
    "ollama:llava:13b"、switched_by=="takeover",且 apply(cfg) 后
    get_classifier(cfg.classifier).name 含 "ollama"。
    """
    takeover = pytest.importorskip(
        A155_TAKEOVER, reason="A155 pipeline.takeover 未就位(并行开发中)"
    )
    mock = _start_mock()
    cfg = _make_cfg(
        tmp_path,
        probe_ports=[str(mock.port)],
        base_urls={"ollama": f"http://127.0.0.1:{mock.port}"},
    )
    scanner = FakeScanner(
        [
            {
                "provider": "ollama",
                "base_url": f"http://127.0.0.1:{mock.port}/v1",
                "models": list(LOCAL_MODELS),
                "ok": True,
            }
        ]
    )
    manager = ModelManager(cfg.model_runtime_path, cfg=cfg)
    result = takeover.takeover_once(cfg, scanner=scanner, manager=manager)
    assert result["action"] == "local"
    assert result["spec"] == "ollama:llava:13b"
    assert manager.get_active() == "ollama:llava:13b"
    assert manager.status()["switched_by"] == "takeover"
    manager.apply(cfg)
    assert cfg.classifier == "ollama:llava:13b"
    clf = get_classifier(cfg.classifier, cfg)
    assert isinstance(clf, NsfwClassifier)
    assert "ollama" in clf.name


def test_takeover_standard_port_11434_real_scanner(tmp_path: pathlib.Path) -> None:
    """接管③:mock 起在 127.0.0.1:11434(被占则 skip)→ 真扫描器全程接管。

    11434 是 A143 映射目录的标准 ollama 端口 → 真 ``LocalVisionScanner``
    发现 provider="ollama" → 真 A155 接管到真 ModelManager →
    ``get_active()=="ollama:llava:13b"``,apply 后分类器可构建。
    """
    takeover = pytest.importorskip(
        A155_TAKEOVER, reason="A155 pipeline.takeover 未就位(并行开发中)"
    )
    pytest.importorskip(
        A143_LOCAL_PROBE, reason="A143 vision.local_probe 未就位(需要真扫描器)"
    )
    if not _port_free(OLLAMA_PORT):
        pytest.skip(f"127.0.0.1:{OLLAMA_PORT} 被占用(本机或有真实 ollama),跳过")
    mock = _start_mock(port=OLLAMA_PORT)
    cfg = _make_cfg(
        tmp_path,
        probe_ports=[str(OLLAMA_PORT)],
        base_urls={"ollama": f"http://127.0.0.1:{mock.port}"},
    )
    manager = ModelManager(cfg.model_runtime_path, cfg=cfg)
    result = takeover.takeover_once(cfg, manager=manager)  # 缺省惰性真 A143 扫描器
    assert result["action"] == "local"
    assert result["spec"] == "ollama:llava:13b"
    assert manager.get_active() == "ollama:llava:13b"
    manager.apply(cfg)
    assert "ollama" in get_classifier(cfg.classifier, cfg).name
    # 红线 32:接管探测只打回环 mock 的清点端点,零评分 POST
    for client_ip, method, path in mock.snapshot():
        assert client_ip == "127.0.0.1"
        assert method == "GET"
        assert path in ("/v1/models", "/models")


def test_takeover_no_model_falls_back_to_wizard(tmp_path: pathlib.Path) -> None:
    """接管④:无任何模型(空扫描 / 无密钥 / 无 runtime)→ 落连接向导。

    scanner 注入返回空结果,环境已由 autouse fixture 清空 → takeover_once →
    action=="wizard" 且注入的 ensure 被以 cfg 调用一次。
    """
    takeover = pytest.importorskip(
        A155_TAKEOVER, reason="A155 pipeline.takeover 未就位(并行开发中)"
    )
    cfg = _make_cfg(tmp_path, probe_ports=[])
    seen: list[Config] = []

    def fake_ensure(c: Config) -> str:
        seen.append(c)
        return "http://127.0.0.1:1/"

    result = takeover.takeover_once(cfg, scanner=FakeScanner([]), ensure=fake_ensure)
    assert result["action"] == "wizard"
    assert result["wizard"] == "http://127.0.0.1:1/"
    assert seen == [cfg]


def test_takeover_no_model_real_chain_starts_real_wizard(tmp_path: pathlib.Path) -> None:
    """接管⑤(全真链):takeover_once → A149 ensure_setup → A154 守护 → 真 A145。

    cfg 探测端口置空(保证"无本地服务"确定性)、无密钥、空 runtime →
    takeover_once 不注入任何替身 → 应返回 action=="wizard" 且 wizard 为
    **真服务器**地址(GET 200);A154 锁文件落在 tmp data 目录。
    """
    takeover = pytest.importorskip(
        A155_TAKEOVER, reason="A155 pipeline.takeover 未就位(并行开发中)"
    )
    cfg = _make_cfg(tmp_path, probe_ports=[])
    result = takeover.takeover_once(cfg)
    assert result["action"] == "wizard"
    url = result["wizard"]
    assert isinstance(url, str) and url.startswith("http://127.0.0.1:")
    status, _, html = _http("GET", url)
    assert status == 200
    assert "离线桩,非模型判定" in html  # 红线 34
    assert 'type="password"' in html  # 红线 33


def test_takeover_session_idempotent(tmp_path: pathlib.Path) -> None:
    """接管⑥:会话幂等——二次 takeover_once 返回 already,零副作用。"""
    takeover = pytest.importorskip(
        A155_TAKEOVER, reason="A155 pipeline.takeover 未就位(并行开发中)"
    )
    cfg = _make_cfg(tmp_path, probe_ports=[])
    scanner = FakeScanner([])
    ensure_calls: list[int] = []

    def fake_ensure(c: Config) -> str:
        ensure_calls.append(1)
        return "http://127.0.0.1:1/"

    first = takeover.takeover_once(cfg, scanner=scanner, ensure=fake_ensure)
    assert first["action"] == "wizard"
    second = takeover.takeover_once(cfg, scanner=scanner, ensure=fake_ensure)
    assert second["action"] == "already"
    assert scanner.calls == 1
    assert len(ensure_calls) == 1


def test_takeover_kept_when_runtime_has_spec(tmp_path: pathlib.Path) -> None:
    """接管⑦:已有活动 spec → kept 沿用,不重探(零网络)。"""
    takeover = pytest.importorskip(
        A155_TAKEOVER, reason="A155 pipeline.takeover 未就位(并行开发中)"
    )
    mock = _start_mock()
    cfg = _make_cfg(tmp_path, probe_ports=[str(mock.port)])
    ModelManager(cfg.model_runtime_path).set_active("stub", switched_by="wizard")
    scanner = FakeScanner([])  # 若被重探也不会命中,但 calls 应保持 0
    result = takeover.takeover_once(cfg, scanner=scanner)
    assert result["action"] == "kept"
    assert result["spec"] == "stub"
    assert scanner.calls == 0  # kept 一级绝不重探(红线 32)
    assert mock.snapshot() == []  # 零网络


# ---------------------------------------------------------------------------
# 用例组 2:接管链路核心(映射 → 真 manager → 可构建分类器;今日直跑)
# ---------------------------------------------------------------------------


def test_local_spec_lands_in_runtime_and_classifier(tmp_path: pathlib.Path) -> None:
    """本地 spec 经真 A147 校验落位:runtime → apply → get_classifier 全链。

    mock /v1/models 含 llava:13b;真 ModelManager(tmp runtime, cfg 带回环
    base_urls 覆盖)set_active("ollama:llava:13b")(validate=True 走真 A147
    探测 mock)→ get_active/status/apply → get_classifier 可构建且 name 含
    "ollama";非 stub spec 不得动 ensemble_members(契约 A144)。
    """
    mock = _start_mock()
    cfg = _make_cfg(tmp_path, base_urls={"ollama": f"http://127.0.0.1:{mock.port}"})
    assert cfg.ensemble_members == ["stub"]
    manager = ModelManager(cfg.model_runtime_path, cfg=cfg)
    status = manager.set_active("ollama:llava:13b", switched_by="takeover")
    assert status["spec"] == "ollama:llava:13b"
    assert manager.get_active() == "ollama:llava:13b"
    manager.apply(cfg)
    assert cfg.classifier == "ollama:llava:13b"
    assert cfg.ensemble_members == ["stub"]  # 非 stub 不动 ensemble(契约)
    clf = get_classifier(cfg.classifier, cfg)
    assert isinstance(clf, NsfwClassifier)
    assert clf.name == "ollama:llava:13b"
    assert "ollama" in clf.name


def test_unknown_openai_compat_spec_rejected_by_real_manager(
    tmp_path: pathlib.Path,
) -> None:
    """真 manager 拒未知提供方:openai-compat:llava:13b → 中文 ValueError。

    这是"非标准端口提供方不能直接进真 manager"的陷阱断言:parse_spec 只认
    20 家目录,接管/向导必须先把 openai 兼容服务映射到受支持的本地提供方。
    """
    manager = ModelManager(str(tmp_path / "model_runtime.json"))
    with pytest.raises(ValueError, match="未知视觉模型提供方"):
        manager.set_active("openai-compat:llava:13b", switched_by="takeover")
    assert manager.get_active() is None  # 拒绝后状态保持未连接


def test_vision_filter_picks_llava_over_text_model() -> None:
    """接管候选筛选:视觉过滤器必须挑中 llava:13b、剔除 llama3:8b(A150)。"""
    assert is_vision_model("llava:13b") is True
    assert is_vision_model("llama3:8b") is False
    assert filter_vision(list(LOCAL_MODELS)) == ["llava:13b"]


# ---------------------------------------------------------------------------
# 用例组 3:无模型 → 向导(真 A149 ensure_setup + 真 A145 服务器)
# ---------------------------------------------------------------------------


def test_no_model_ensure_setup_starts_real_wizard(tmp_path: pathlib.Path) -> None:
    """向导①:全无模型 → ensure_setup 起真服务器;页面红线 33/34 断言。

    NETSENTINEL_NO_BROWSER=1(tmp 家目录 + 无密钥 + 空 runtime)→
    has_any_model 为 False → ensure_setup(server_factory 注入随机端口)→
    GET / 200 且 text/html;HTML 含"离线桩,非模型判定"(红线 34)与
    type="password"(红线 33)。
    """
    cfg = _make_cfg(tmp_path, probe_ports=[])
    assert setup_trigger.has_any_model(cfg) is False
    url = setup_trigger.ensure_setup(cfg, server_factory=_wizard_factory())
    assert url is not None and url.startswith("http://127.0.0.1:")
    status, headers, html = _http("GET", url)
    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")
    assert "离线桩,非模型判定" in html  # 红线 34
    assert 'type="password"' in html  # 红线 33


def test_ensure_setup_idempotent_reuses_url(tmp_path: pathlib.Path) -> None:
    """向导②:进程内二次 ensure_setup 复用同一地址,不再起新服务。"""
    cfg = _make_cfg(tmp_path, probe_ports=[])
    starts: list[int] = []

    def factory(c: Config) -> str:
        starts.append(1)
        srv = _start_wizard(c)
        return str(srv.url)

    first = setup_trigger.ensure_setup(cfg, server_factory=factory)
    second = setup_trigger.ensure_setup(cfg, server_factory=factory)
    assert first == second and first is not None
    assert starts == [1]  # 工厂只被调用一次


def test_ensure_setup_skips_when_model_present(tmp_path: pathlib.Path) -> None:
    """向导③:已有活动模型(runtime 存 spec)→ ensure_setup 返回 None 不打扰。"""
    cfg = _make_cfg(tmp_path, probe_ports=[])
    ModelManager(cfg.model_runtime_path).set_active("stub", switched_by="wizard")
    assert setup_trigger.has_any_model(cfg) is True

    def boom_factory(c: Config) -> str:
        raise AssertionError("已有模型时不得启动向导服务")

    assert setup_trigger.ensure_setup(cfg, server_factory=boom_factory) is None


def test_wizard_activate_stub_e2e(tmp_path: pathlib.Path) -> None:
    """向导④:POST /api/activate {"spec":"stub"} → 真 manager 落位 stub。

    真 A145 服务器(缺省 manager 惰性构建于 cfg.model_runtime_path)+
    真 A144:stub 免语法与连通性校验(离线桩永远可用,红线 34)。
    """
    cfg = _make_cfg(tmp_path, probe_ports=[])
    url = setup_trigger.ensure_setup(cfg, server_factory=_wizard_factory())
    assert url is not None
    status, _, text = _http("POST", f"{url}api/activate", {"spec": "stub"})
    assert status == 200
    payload = json.loads(text)
    assert payload["ok"] is True
    assert payload["status"]["spec"] == "stub"
    assert payload["status"]["switched_by"] == "wizard"
    # 真 manager 从同一 runtime 文件读回
    assert ModelManager(cfg.model_runtime_path).get_active() == "stub"


# ---------------------------------------------------------------------------
# 用例组 4:切换生效(stub ⇄ 本地视觉模型,真 A147 + mock 服务)
# ---------------------------------------------------------------------------


def test_switch_stub_then_local_via_wizard(tmp_path: pathlib.Path) -> None:
    """切换①:stub 整套替换 ensemble;切回本地 spec 后 classifier 恢复。

    activate "stub" → apply → cfg.ensemble_members==["stub"] 且
    classifier=="stub";activate "ollama:llava:13b"(注入携带 cfg 的真
    manager,真 A147 连通性校验打到 mock /models)→ apply →
    classifier=="ollama:llava:13b"、ensemble 保持 ["stub"](契约:非 stub
    不动 ensemble)、get_classifier 可构建且 name 含 "ollama"。
    """
    mock = _start_mock()
    cfg = _make_cfg(tmp_path, base_urls={"ollama": f"http://127.0.0.1:{mock.port}"})
    manager = ModelManager(cfg.model_runtime_path, cfg=cfg)
    wizard = _start_wizard(cfg, manager=manager)
    url = str(wizard.url)

    # -- 切到离线桩:整套替换(红线 34) --
    status, _, text = _http("POST", f"{url}api/activate", {"spec": "stub"})
    assert status == 200, text
    cfg_stub = _make_cfg(tmp_path, base_urls={"ollama": f"http://127.0.0.1:{mock.port}"})
    manager.apply(cfg_stub)
    assert cfg_stub.classifier == "stub"
    assert cfg_stub.ensemble_members == ["stub"]

    # -- 切回本地视觉模型:真 A147 校验经 mock,classifier 恢复 --
    status, _, text = _http("POST", f"{url}api/activate", {"spec": "ollama:llava:13b"})
    assert status == 200, text
    assert json.loads(text)["status"]["switched_by"] == "wizard"
    assert manager.get_active() == "ollama:llava:13b"
    manager.apply(cfg_stub)
    assert cfg_stub.classifier == "ollama:llava:13b"  # classifier 恢复
    assert cfg_stub.ensemble_members == ["stub"]  # 非 stub 不动 ensemble
    clf = get_classifier(cfg_stub.classifier, cfg_stub)
    assert isinstance(clf, NsfwClassifier)
    assert "ollama" in clf.name


def test_wizard_activate_unknown_spec_400(tmp_path: pathlib.Path) -> None:
    """切换②:向导 REST 拒未知提供方 spec → 400 中文错误。"""
    cfg = _make_cfg(tmp_path, probe_ports=[])
    wizard = _start_wizard(cfg)
    status, _, text = _http(
        "POST", f"{wizard.url}api/activate", {"spec": "openai-compat:llava:13b"}
    )
    assert status == 400
    payload = json.loads(text)
    assert "未知" in payload["error"]
    # 拒绝后 runtime 保持未连接
    assert ModelManager(cfg.model_runtime_path).get_active() is None


def test_activate_local_model_missing_in_service_rejected(
    tmp_path: pathlib.Path,
) -> None:
    """切换③:服务上不存在的模型 → 真 A147 校验不过 → 400 且状态不变。

    mock 只含 llava:13b;activate "ollama:absent-model" → 连通性测试发现
    模型缺失 → ModelManager 抛中文 ValueError → 向导 400。
    """
    mock = _start_mock()
    cfg = _make_cfg(tmp_path, base_urls={"ollama": f"http://127.0.0.1:{mock.port}"})
    manager = ModelManager(cfg.model_runtime_path, cfg=cfg)
    manager.set_active("stub", switched_by="wizard")  # 预置旧值,验证不被破坏
    wizard = _start_wizard(cfg, manager=manager)
    status, _, text = _http(
        "POST", f"{wizard.url}api/activate", {"spec": "ollama:absent-model"}
    )
    assert status == 400
    assert "连通性" in json.loads(text)["error"]
    assert manager.get_active() == "stub"  # 切换失败保持原活动模型


def test_wizard_test_endpoint_real_connectivity(tmp_path: pathlib.Path) -> None:
    """切换④:POST /api/test 走真 A147:命中模型 ok / 缺失模型中文 error。"""
    mock = _start_mock()
    cfg = _make_cfg(tmp_path, base_urls={"ollama": f"http://127.0.0.1:{mock.port}"})
    wizard = _start_wizard(cfg)
    url = str(wizard.url)

    status, _, text = _http("POST", f"{url}api/test", {"spec": "ollama:llava:13b"})
    assert status == 200
    ok_result = json.loads(text)
    assert ok_result["ok"] is True
    assert ok_result["model"] == "llava:13b"
    assert ok_result["mode"] == "local"

    status, _, text = _http("POST", f"{url}api/test", {"spec": "ollama:absent-model"})
    assert status == 200  # 连通性错误在结果体内,HTTP 层不 5xx
    bad_result = json.loads(text)
    assert bad_result["ok"] is False
    assert isinstance(bad_result.get("error"), str) and bad_result["error"]


def test_manager_injected_tester_allows_switch(tmp_path: pathlib.Path) -> None:
    """切换⑤:tester 注入协议(spec, cfg) -> {"ok": True} 即可切换本地 spec。"""
    cfg = _make_cfg(tmp_path, probe_ports=[])
    manager = ModelManager(cfg.model_runtime_path, cfg=cfg)

    def ok_tester(spec: str, c: Any) -> dict:
        return {"ok": True, "latency_ms": 1, "model": spec.partition(":")[2]}

    status = manager.set_active(
        "lmstudio:llava:13b", switched_by="wizard", tester=ok_tester
    )
    assert status["spec"] == "lmstudio:llava:13b"
    manager.apply(cfg)
    assert "lmstudio" in get_classifier(cfg.classifier, cfg).name


# ---------------------------------------------------------------------------
# 用例组 5:密钥只进不显(红线 33;真 A152 + 真 A145)
# ---------------------------------------------------------------------------


def test_setkey_only_masked_never_plaintext(tmp_path: pathlib.Path) -> None:
    """密钥①:POST /api/setkey 真 A152 落盘 → 响应只回 ok/masked,全文无本体。

    家目录已由 autouse fixture 指向 tmp。断言四层:
    ① 响应 JSON 恰为 {"ok": true, "masked": "sk-t****"};
    ② **响应全文**不含密钥本体(红线 33,逐字节);
    ③ 密钥经 security.keys.set_key 落盘(tmp 家目录,只进不显的"进");
    ④ 二次 /api/status:keys.glm configured=True 且 masked 相同,全文无本体。
    """
    cfg = _make_cfg(tmp_path, probe_ports=[])
    wizard = _start_wizard(cfg)
    url = str(wizard.url)

    status, _, text = _http("POST", f"{url}api/setkey", {"provider": "glm", "key": RAW_KEY})
    assert status == 200, text
    payload = json.loads(text)
    assert payload == {"ok": True, "masked": MASKED_KEY}
    assert RAW_KEY not in text  # 红线 33:响应全文不含密钥本体

    # ③ 落盘仅经 security.keys.set_key(tmp 家目录下的 glm 文件首行)
    key_file = pathlib.Path.home() / ".netsentinel" / "keys" / "glm"
    assert key_file.is_file()
    assert key_file.read_text(encoding="utf-8").splitlines()[0].strip() == RAW_KEY

    # ④ 二次状态查询:含 masked,不含本体
    status, _, text = _http("POST", f"{url}api/status")
    assert status == 200
    assert RAW_KEY not in text  # 红线 33
    status_payload = json.loads(text)
    glm = status_payload["keys"]["glm"]
    assert glm["configured"] is True
    assert glm["masked"] == MASKED_KEY


def test_setkey_short_key_rejected(tmp_path: pathlib.Path) -> None:
    """密钥②:过短密钥 → 400 中文错误,错误信息与响应全文均无密钥痕迹。"""
    cfg = _make_cfg(tmp_path, probe_ports=[])
    wizard = _start_wizard(cfg)
    short = "sk-12"
    status, _, text = _http(
        "POST", f"{wizard.url}api/setkey", {"provider": "glm", "key": short}
    )
    assert status == 400
    payload = json.loads(text)
    assert "过短" in payload["error"]
    assert short not in text
    # 拒绝后不落盘、未配置
    assert not (pathlib.Path.home() / ".netsentinel" / "keys" / "glm").exists()
    status, _, text = _http("POST", f"{wizard.url}api/status")
    assert json.loads(text)["keys"]["glm"]["configured"] is False


# ---------------------------------------------------------------------------
# 用例组 6:红线 32 专项(回环限定 / 固定端口目录 / 本地零评分外呼)
# ---------------------------------------------------------------------------


def test_loopback_only_and_no_scoring_posts(tmp_path: pathlib.Path) -> None:
    """红线 32:全部流量仅回环、仅 GET /models 清点、零 POST 评分。

    跑一遍"真 A147 连通性 + 向导 activate"链路后断言:
    ① mock 与向导服务均只绑 127.0.0.1;
    ② mock 收到的每个请求客户端地址都是 127.0.0.1;
    ③ 本地清点只有 GET 且路径为 /models 系——没有任何评分 POST
       (红线 32/34:本地提供方零评分外呼、零预算记账);
    ④ 缺省 cfg.local_probe_ports 恰为契约 §1 登记的 4 个端口。
    """
    mock = _start_mock()
    cfg = _make_cfg(tmp_path, base_urls={"ollama": f"http://127.0.0.1:{mock.port}"})
    manager = ModelManager(cfg.model_runtime_path, cfg=cfg)
    wizard = _start_wizard(cfg, manager=manager)
    url = str(wizard.url)

    status, _, text = _http("POST", f"{url}api/test", {"spec": "ollama:llava:13b"})
    assert json.loads(text)["ok"] is True
    status, _, text = _http("POST", f"{url}api/activate", {"spec": "ollama:llava:13b"})
    assert status == 200, text

    requests = mock.snapshot()
    assert requests, "mock 应至少收到连通性清点请求"
    for client_ip, method, path in requests:
        assert client_ip == "127.0.0.1", f"红线 32:出现非回环客户端 {client_ip}"
        assert method == "GET", f"本地链路出现非清点请求:{method} {path}"
        assert path in ("/v1/models", "/models")
    # 服务绑定面:mock 与向导都只听回环
    assert mock.server_address[0] == "127.0.0.1"
    assert wizard.url is not None and wizard.url.startswith("http://127.0.0.1:")
    # 契约 §1:探测端口目录固定,绝不扩到回环全端口段
    assert set(Config().local_probe_ports) == CONTRACT_PROBE_PORTS


# ---------------------------------------------------------------------------
# 收尾自证:注册表清空、无遗留线程
# ---------------------------------------------------------------------------


def test_teardown_stops_all_registered_servers(tmp_path: pathlib.Path) -> None:
    """收尾:autouse fixture 停止注册表内全部服务器且注册表清空。"""
    mock = _start_mock()
    cfg = _make_cfg(tmp_path, probe_ports=[])
    wizard = _start_wizard(cfg)
    assert mock.port > 0 and wizard.running
    _stop_all_servers()  # teardown 同款路径,显式跑一遍以断言可停性
    assert wizard.running is False
    assert _ACTIVE_SERVERS == []
    # 停止后端口不再有 HTTP 应答(连接被拒 → 状态码 0)
    status, _, _ = _http("POST", f"http://127.0.0.1:{mock.port}/api/status", timeout=5)
    assert status == 0
