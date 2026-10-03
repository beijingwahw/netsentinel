# -*- coding: utf-8 -*-
"""A145:netsentinel.setup.server 单元测试。

口径(CONTRACTS-V8 §0 红线 32–34 / §3 A145):
- 对**真实起服务**的 127.0.0.1 随机端口发起 urllib 请求(全程仅回环,红线 32);
- 五个 API(status / probe / activate / setkey / test)各正反例,兄弟模块
  (A143/A144/A146/A147/A152/A156)一律注入 fake 或用 sys.modules 模拟缺席;
- 红线 33 专项:/api/setkey 与 /api/status 响应**绝不含密钥本体**(含出口拦截兜底);
- 坏 JSON 400 / 未知路径 404 / 方法不符 405 / handler 异常 500,错误一律中文 JSON;
- stop 幂等;并发 5 请求(Barrier 证明 ThreadingHTTPServer 真并行);页面路由 200 text/html;
- activate 后 status 反映切换(switched_by="wizard")。
"""
from __future__ import annotations

import copy
import json
import pathlib
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest

from netsentinel.contracts import Config
from netsentinel.setup import server as setup_server
from netsentinel.setup.server import SetupServer

# ---------------------------------------------------------------------------
# 公共样本与 fake 兄弟模块
# ---------------------------------------------------------------------------

#: A143 扫描结果的受控样本(仅数据,不联网)
_SCAN_RESULT = [
    {
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": ["llava", "qwen2.5-vl-7b-instruct"],
        "ok": True,
    }
]

#: 红线 33 断言用的"密钥本体"样本(测试内仅作为应被掩蔽的敏感串)
_RAW_KEY = "sk-live-zx9secret888999"
_MASKED_KEY = "sk-l****"


class FakeScanner:
    """A143 替身:计数 / 延迟 / 抛错均可控。"""

    def __init__(self, result: list | None = None, error: Exception | None = None, delay: float = 0.0):
        self.result = result if result is not None else copy.deepcopy(_SCAN_RESULT)
        self.error = error
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def scan(self) -> list:
        with self._lock:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.result)


class BarrierScanner:
    """并发证明替身:5 个请求必须同时在 scan() 内,否则 Barrier 超时破裂。"""

    def __init__(self, parties: int) -> None:
        self.barrier = threading.Barrier(parties, timeout=10.0)

    def scan(self) -> list:
        self.barrier.wait()
        return copy.deepcopy(_SCAN_RESULT)


class FakeManager:
    """A144 替身:记录 set_active 调用;spec 以 ``bad:`` 开头时抛中文 ValueError。"""

    def __init__(self, active: str | None = None) -> None:
        self.active = active
        self.switched_by: str | None = None
        self.calls: list[tuple[str, str]] = []

    def get_active(self) -> str | None:
        return self.active

    def set_active(self, spec: str, *, switched_by: str, validate: bool = True) -> None:
        self.calls.append((spec, switched_by))
        if spec.startswith("bad:"):
            raise ValueError(f"未知视觉模型提供方:{spec}")
        self.active = spec
        self.switched_by = switched_by

    def status(self) -> dict:
        return {
            "spec": self.active,
            "switched_by": self.switched_by,
            "switched_at": "2026-10-02T12:00:00",
        }

    def clear(self) -> None:
        self.active = None
        self.switched_by = None

    def apply(self, cfg: object) -> None:
        return None


def _ok_acceptor(provider: str, key: str) -> dict:
    """A152 替身:成功返回 ok+masked(外加 stored_path,服务器应只回 ok/masked)。"""
    stripped = key.strip()
    if len(stripped) < 8:
        raise ValueError(f"密钥长度不足:至少 8 位,当前 {len(stripped)} 位")
    return {"ok": True, "masked": stripped[:4] + "****", "stored_path": f"/keys/{provider}"}


def _leaky_acceptor(provider: str, key: str) -> dict:
    """敌意替身:把密钥本体当作 masked 回显,用于红线 33 出口拦截断言。"""
    return {"ok": True, "masked": key}


def _ok_tester(spec: str) -> dict:
    """A147 替身:正常透传结果。"""
    return {"ok": True, "latency_ms": 12.5, "model": spec, "provider": "ollama"}


def _fake_module(name: str, **attrs: object) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _parent_of(name: str) -> types.ModuleType | None:
    """取模块父包对象(用于同步父包同名属性;取不到返回 None)。"""
    parent_name, _, _leaf = name.rpartition(".")
    if not parent_name:
        return None
    parent = sys.modules.get(parent_name)
    if parent is None:
        try:
            parent = __import__(parent_name, fromlist=["__name__"])
        except Exception:  # noqa: BLE001 父包缺席则只靠 sys.modules 生效
            return None
    return parent


def _force_module(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: object) -> types.ModuleType:
    """把 fake 模块同时挂到 sys.modules 与父包属性上。

    只改 sys.modules 时,若真实兄弟模块已被导入(父包属性已指向真身),
    ``from 父包 import 子模块`` 会优先命中父包属性上的真身,fake 失效;
    双写两处才能在并行开发(兄弟随时落地)下保持确定性。
    """
    fake = _fake_module(name, **attrs)
    monkeypatch.setitem(sys.modules, name, fake)
    parent = _parent_of(name)
    if parent is not None:
        monkeypatch.setattr(parent, name.rpartition(".")[2], fake, raising=False)
    return fake


def _force_absent(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """让 ``import name`` 必定失败:sys.modules 置 None + 父包同名属性置 None。"""
    monkeypatch.setitem(sys.modules, name, None)
    parent = _parent_of(name)
    if parent is not None:
        monkeypatch.setattr(parent, name.rpartition(".")[2], None, raising=False)


# ---------------------------------------------------------------------------
# 工具:家目录隔离 / 环境清理 / 服务器工厂 / HTTP 客户端
# ---------------------------------------------------------------------------


@pytest.fixture()
def home_dir(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """隔离家目录与全部提供方密钥环境变量:keys 状态完全由 cfg + tmp 决定。"""
    directory = tmp_path / "home"
    directory.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: directory))
    try:
        from netsentinel.vision.providers import PROVIDERS

        names = list(PROVIDERS)
    except Exception:  # noqa: BLE001 目录缺席时退回内置清单口径
        names = [
            "glm", "openai", "anthropic", "gemini", "qwen", "doubao", "hunyuan",
            "moonshot", "minimax", "stepfun", "siliconflow", "ernie", "openrouter",
            "groq", "together", "xai", "ollama", "vllm", "lmstudio", "xinference",
        ]
    for name in names:
        upper = name.upper()
        monkeypatch.delenv(f"NETSENTINEL_{upper}_API_KEY", raising=False)
        monkeypatch.delenv(f"{upper}_API_KEY", raising=False)
    return directory


@pytest.fixture()
def make_server(home_dir: pathlib.Path):
    """建一个已启动(127.0.0.1 随机端口)的向导服务器;测试结束统一 stop。"""
    made: list[SetupServer] = []

    def _make(*, cfg: Config | None = None, **deps: object) -> tuple[SetupServer, str]:
        srv = SetupServer(cfg or Config(), **deps)
        url = srv.start(port=0)
        made.append(srv)
        return srv, url

    yield _make
    for srv in made:
        srv.stop()


def _request(
    url: str, *, method: str = "POST", body: object = None, timeout: float = 20.0
) -> tuple[int, str, str]:
    """发 HTTP 请求,返回 (状态码, Content-Type, 响应体文本);HTTPError 同样返回。"""
    data: bytes | None = None
    if isinstance(body, dict):
        data = json.dumps(body).encode("utf-8")
    elif isinstance(body, str):
        data = body.encode("utf-8")
    elif isinstance(body, bytes):
        data = body
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        return err.code, err.headers.get("Content-Type", ""), err.read().decode("utf-8", "replace")


def _get_json(text: str) -> dict:
    payload = json.loads(text)
    assert isinstance(payload, dict)
    return payload


# ---------------------------------------------------------------------------
# 生命周期:start / stop / serve 别名 / 上下文 / 回环绑定
# ---------------------------------------------------------------------------


def test_start_returns_loopback_url_and_serves(make_server) -> None:
    srv, url = make_server()
    assert url.startswith("http://127.0.0.1:") and url.endswith("/")
    assert srv.running and srv.url == url and srv.port is not None
    status, ctype, _ = _request(url, method="GET")
    assert status == 200
    assert ctype.startswith("text/html")


def test_start_binds_loopback_only(make_server) -> None:
    """红线 32:服务只绑 127.0.0.1,绝不监听其他网卡。"""
    srv, _url = make_server()
    httpd = srv._httpd
    assert httpd is not None
    assert httpd.server_address[0] == "127.0.0.1"


def test_start_uses_cfg_setup_port_when_port_omitted(make_server) -> None:
    """port=None 时用 cfg.setup_port(测试里置 0 走随机端口,避免撞固定端口)。"""
    cfg = Config()
    cfg.setup_port = 0
    _srv, url = make_server(cfg=cfg)
    status, _, _ = _request(url, method="GET")
    assert status == 200


def test_start_idempotent_returns_same_url(make_server) -> None:
    srv, url = make_server()
    assert srv.start(port=0) == url
    status, _, _ = _request(url, method="GET")
    assert status == 200


def test_serve_alias_behaves_like_start(home_dir: pathlib.Path) -> None:
    """契约 A145 的 serve 别名(A148 modelmgr serve 入口使用)。"""
    srv = SetupServer(Config())
    try:
        url = srv.serve(port=0)
        assert url.startswith("http://127.0.0.1:")
        status, _, _ = _request(url, method="GET")
        assert status == 200
    finally:
        srv.stop()


def test_stop_idempotent(make_server) -> None:
    srv, _ = make_server()
    srv.stop()
    srv.stop()
    srv.stop()
    assert not srv.running and srv.url is None and srv.port is None


def test_stop_frees_port(make_server) -> None:
    srv, url = make_server()
    srv.stop()
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(url, timeout=5)


def test_stop_before_start_is_noop(home_dir: pathlib.Path) -> None:
    srv = SetupServer(Config())
    srv.stop()  # 不抛即通过
    assert not srv.running


def test_restart_after_stop(home_dir: pathlib.Path) -> None:
    srv = SetupServer(Config())
    try:
        url1 = srv.start(port=0)
        srv.stop()
        url2 = srv.start(port=0)
        assert url2.startswith("http://127.0.0.1:")
        status, _, _ = _request(url2, method="GET")
        assert status == 200
        with pytest.raises(urllib.error.URLError):  # 旧地址已停
            urllib.request.urlopen(url1, timeout=5)
    finally:
        srv.stop()


def test_context_manager_stops_on_exit(home_dir: pathlib.Path) -> None:
    with SetupServer(Config()) as srv:
        assert srv.running
        status, _, _ = _request(str(srv.url), method="GET")
        assert status == 200
        url = srv.url
    assert not srv.running
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(str(url), timeout=5)


def test_open_browser_flag_and_env_override(home_dir: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """open_browser=True 弹浏览器;NETSENTINEL_NO_BROWSER=1 只记 URL 不弹。"""
    opened: list[str] = []
    monkeypatch.setattr(setup_server.webbrowser, "open", lambda u: opened.append(u) or True)
    srv = SetupServer(Config())
    try:
        monkeypatch.setenv("NETSENTINEL_NO_BROWSER", "1")
        url1 = srv.start(port=0, open_browser=True)
        assert opened == []
        srv.stop()
        monkeypatch.delenv("NETSENTINEL_NO_BROWSER")
        url2 = srv.start(port=0, open_browser=True)
        assert opened == [url2] and url2 != url1
    finally:
        srv.stop()


# ---------------------------------------------------------------------------
# 页面路由:GET /(注入渲染器 / A156 → A146 → 内置兜底 链)
# ---------------------------------------------------------------------------


def test_get_page_returns_html_with_redline34_text(make_server) -> None:
    _srv, url = make_server()
    status, ctype, body = _request(url, method="GET")
    assert status == 200
    assert ctype.startswith("text/html") and "utf-8" in ctype.lower()
    assert "离线桩,非模型判定" in body  # 红线 34 明示文案(兜底页/A146 均含)


def test_get_page_uses_injected_renderer(make_server) -> None:
    captured: dict = {}

    def renderer(status: dict, active: object, local_models: list) -> str:
        captured.update(status=status, active=active, local=local_models)
        return f"<html>PAGE_FROM_RENDERER active={active}</html>"

    mgr = FakeManager(active="ollama:llava")
    scanner = FakeScanner()
    _srv, url = make_server(scanner=scanner, manager=mgr, page_renderer=renderer)
    status, ctype, body = _request(url, method="GET")
    assert status == 200 and ctype.startswith("text/html")
    assert "PAGE_FROM_RENDERER active=ollama:llava" in body
    assert captured["active"] == "ollama:llava"
    assert captured["local"] == _SCAN_RESULT
    assert set(captured["status"]) >= {"active", "switched_by", "local", "keys"}


def test_get_page_falls_back_when_renderer_raises(make_server) -> None:
    def bad_renderer(status: dict, active: object, local_models: list) -> str:
        raise RuntimeError("渲染炸了")

    _srv, url = make_server(page_renderer=bad_renderer)
    status, ctype, body = _request(url, method="GET")
    assert status == 200 and ctype.startswith("text/html")
    assert "离线桩,非模型判定" in body  # 回退链最终兜底页仍可开


def test_page_prefers_a156_over_a146(make_server, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_module(
        monkeypatch, "netsentinel.setup.render",
        render_page=lambda s, a, loc: "<html>A156_PAGE</html>",
    )
    _force_module(monkeypatch, "netsentinel.setup.page", page_html=lambda: "<html>A146_PAGE</html>")
    _srv, url = make_server()
    _status, _ctype, body = _request(url, method="GET")
    assert "A156_PAGE" in body and "A146_PAGE" not in body


def test_page_falls_back_to_a146_when_a156_fails(make_server, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(status: dict, active: object, local_models: list) -> str:
        raise ValueError("A156 渲染失败")

    _force_module(monkeypatch, "netsentinel.setup.render", render_page=boom)
    _force_module(
        monkeypatch, "netsentinel.setup.page", page_html=lambda: "<html>A146_PAGE 原样</html>"
    )
    _srv, url = make_server()
    status, ctype, body = _request(url, method="GET")
    assert status == 200 and ctype.startswith("text/html")
    assert "A146_PAGE 原样" in body


def test_page_builtin_fallback_when_siblings_absent(make_server, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_absent(monkeypatch, "netsentinel.setup.render")
    _force_absent(monkeypatch, "netsentinel.setup.page")
    _srv, url = make_server()
    status, ctype, body = _request(url, method="GET")
    assert status == 200 and ctype.startswith("text/html")
    assert "连接向导" in body and "离线桩,非模型判定" in body


def test_path_normalization_query_and_trailing_slash(make_server) -> None:
    _srv, url = make_server(scanner=FakeScanner())
    status, _, _ = _request(url + "?x=1", method="GET")
    assert status == 200
    status, _, body = _request(url + "api/status/", body={})
    assert status == 200
    assert _get_json(body)["local"] == _SCAN_RESULT


# ---------------------------------------------------------------------------
# POST /api/status
# ---------------------------------------------------------------------------


def test_status_shape_and_json_content_type(make_server) -> None:
    mgr = FakeManager(active=None)
    scanner = FakeScanner()
    _srv, url = make_server(scanner=scanner, manager=mgr)
    status, ctype, body = _request(url + "api/status", body={})
    assert status == 200
    assert ctype.startswith("application/json")
    payload = _get_json(body)
    assert payload["active"] is None and payload["switched_by"] is None
    assert payload["local"] == _SCAN_RESULT
    assert isinstance(payload["keys"], dict) and payload["keys"]
    for entry in payload["keys"].values():  # 红线 33:仅布尔+掩码两种信息
        assert set(entry) == {"configured", "masked"}
        assert isinstance(entry["configured"], bool)
        assert entry["masked"] is None or isinstance(entry["masked"], str)


def test_status_after_activate_reflects_switch(make_server) -> None:
    mgr = FakeManager()
    scanner = FakeScanner()
    _srv, url = make_server(scanner=scanner, manager=mgr)
    code, _, text = _request(url + "api/activate", body={"spec": "ollama:llava"})
    assert code == 200
    status, _, body = _request(url + "api/status", body={})
    payload = _get_json(body)
    assert payload["active"] == "ollama:llava"
    assert payload["switched_by"] == "wizard"


def test_status_scans_fresh_each_call(make_server) -> None:
    scanner = FakeScanner()
    _srv, url = make_server(scanner=scanner)
    for expected in (1, 2):
        status, _, _ = _request(url + "api/status", body={})
        assert status == 200
        assert scanner.calls == expected  # 现扫,不缓存


def test_status_scan_timeout_is_bounded(make_server, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup_server, "_SCAN_BUDGET_S", 0.5)
    slow = FakeScanner(delay=3.0)  # 远超预算
    _srv, url = make_server(scanner=slow)
    started = time.monotonic()
    status, _, body = _request(url + "api/status", body={})
    elapsed = time.monotonic() - started
    assert status == 200
    assert elapsed < 2.5  # 0.5s 看门狗 + 余量,绝不等满 3s
    payload = _get_json(body)
    assert payload["local"] == []
    assert "超时" in payload["local_error"]


def test_status_keys_masked_and_no_secret_body(make_server) -> None:
    """红线 33:keys 只有布尔+掩码;响应绝不含 cfg 里配置的密钥本体。"""
    cfg = Config()
    cfg.vlm_api_keys = {"glm": _RAW_KEY}
    _srv, url = make_server(cfg=cfg, scanner=FakeScanner(), manager=FakeManager())
    status, _, body = _request(url + "api/status", body={})
    assert status == 200
    assert _RAW_KEY not in body
    entry = _get_json(body)["keys"]["glm"]
    assert entry["configured"] is True
    masked = entry["masked"]
    assert masked is None or (isinstance(masked, str) and masked.endswith("****") and _RAW_KEY not in masked)


def test_status_guards_against_leaky_masked_impl(make_server, monkeypatch: pytest.MonkeyPatch) -> None:
    """敌意 A152(masked 回显本体):出口兜底剥掉 masked,密钥绝不外泄。"""
    cfg = Config()
    cfg.vlm_api_keys = {"glm": _RAW_KEY}
    _force_module(
        monkeypatch, "netsentinel.security.key_api", masked=lambda provider: _RAW_KEY
    )
    _srv, url = make_server(cfg=cfg, scanner=FakeScanner(), manager=FakeManager())
    status, _, body = _request(url + "api/status", body={})
    assert status == 200
    assert _RAW_KEY not in body  # 红线 33 出口拦截
    payload = _get_json(body)
    assert payload["keys"]["glm"]["configured"] is True
    assert "masked" not in payload["keys"]["glm"]  # 泄漏字段被剥除


# ---------------------------------------------------------------------------
# POST /api/probe
# ---------------------------------------------------------------------------


def test_probe_returns_local_scan(make_server) -> None:
    _srv, url = make_server(scanner=FakeScanner())
    status, _, body = _request(url + "api/probe", body={})
    assert status == 200
    payload = _get_json(body)
    assert payload["local"] == _SCAN_RESULT
    assert "error" not in payload


def test_probe_scanner_exception_500_chinese(make_server) -> None:
    _srv, url = make_server(scanner=FakeScanner(error=RuntimeError("扫描线程炸了")))
    status, ctype, body = _request(url + "api/probe", body={})
    assert status == 500
    assert ctype.startswith("application/json")
    payload = _get_json(body)
    assert "服务器内部错误" in payload["error"]
    assert "Traceback" not in body  # 不带栈


def test_probe_scanner_module_absent_degrades(make_server, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_absent(monkeypatch, "netsentinel.vision.local_probe")
    _srv, url = make_server()
    status, _, body = _request(url + "api/probe", body={})
    assert status == 200
    payload = _get_json(body)
    assert payload["local"] == []
    assert "未就位" in payload["error"]


# ---------------------------------------------------------------------------
# POST /api/activate
# ---------------------------------------------------------------------------


def test_activate_ok_calls_set_active_with_wizard(make_server) -> None:
    mgr = FakeManager()
    _srv, url = make_server(manager=mgr, scanner=FakeScanner())
    status, _, body = _request(url + "api/activate", body={"spec": "ollama:llava"})
    assert status == 200
    payload = _get_json(body)
    assert payload["ok"] is True
    assert payload["status"]["spec"] == "ollama:llava"
    assert mgr.calls == [("ollama:llava", "wizard")]


def test_activate_invalid_spec_400_chinese(make_server) -> None:
    _srv, url = make_server(manager=FakeManager(), scanner=FakeScanner())
    status, _, body = _request(url + "api/activate", body={"spec": "bad:foo"})
    assert status == 400
    assert _get_json(body)["error"] == "未知视觉模型提供方:bad:foo"


@pytest.mark.parametrize("payload", [{}, {"spec": "   "}, {"spec": 123}, {"other": 1}])
def test_activate_missing_or_bad_spec_400(make_server, payload: dict) -> None:
    _srv, url = make_server(manager=FakeManager(), scanner=FakeScanner())
    status, _, body = _request(url + "api/activate", body=payload)
    assert status == 400
    assert "spec" in _get_json(body)["error"]


def test_activate_manager_absent_500(make_server, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_absent(monkeypatch, "netsentinel.vision.model_manager")
    _srv, url = make_server(scanner=FakeScanner())
    status, _, body = _request(url + "api/activate", body={"spec": "ollama:llava"})
    assert status == 500
    assert "未就位" in _get_json(body)["error"]


# ---------------------------------------------------------------------------
# POST /api/setkey(红线 33 主战场)
# ---------------------------------------------------------------------------


def test_setkey_ok_returns_only_ok_and_masked(make_server) -> None:
    seen: list[tuple[str, str]] = []

    def acceptor(provider: str, key: str) -> dict:
        seen.append((provider, key))
        return _ok_acceptor(provider, key)

    _srv, url = make_server(key_acceptor=acceptor, scanner=FakeScanner(), manager=FakeManager())
    status, ctype, body = _request(url + "api/setkey", body={"provider": "glm", "key": _RAW_KEY})
    assert status == 200 and ctype.startswith("application/json")
    assert seen == [("glm", _RAW_KEY)]
    payload = _get_json(body)
    assert set(payload) == {"ok", "masked"}  # 只有 ok+masked,连 stored_path 也不带
    assert payload["ok"] is True and payload["masked"].endswith("****")
    assert _RAW_KEY not in body  # 红线 33:响应绝不含密钥本体


def test_setkey_invalid_key_400_chinese(make_server) -> None:
    _srv, url = make_server(key_acceptor=_ok_acceptor, scanner=FakeScanner())
    status, _, body = _request(url + "api/setkey", body={"provider": "glm", "key": "short"})
    assert status == 400
    assert "密钥长度不足" in _get_json(body)["error"]


@pytest.mark.parametrize(
    "payload, field",
    [({}, "provider"), ({"key": "sk-123456789"}, "provider"), ({"provider": "glm"}, "key"),
     ({"provider": "glm", "key": 42}, "key")],
)
def test_setkey_missing_fields_400(make_server, payload: dict, field: str) -> None:
    _srv, url = make_server(key_acceptor=_ok_acceptor, scanner=FakeScanner())
    status, _, body = _request(url + "api/setkey", body=payload)
    assert status == 400
    assert field in _get_json(body)["error"]


def test_setkey_redline33_intercepts_leaked_key(make_server) -> None:
    """敌意实现把密钥本体塞进响应:出口拦截转通用 500,密钥绝不回显。"""
    _srv, url = make_server(key_acceptor=_leaky_acceptor, scanner=FakeScanner())
    status, _, body = _request(url + "api/setkey", body={"provider": "glm", "key": _RAW_KEY})
    assert status == 500
    assert _RAW_KEY not in body  # 红线 33 兜底断言
    assert "拦截" in _get_json(body)["error"]


def test_setkey_module_absent_500(make_server, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_absent(monkeypatch, "netsentinel.security.key_api")
    _srv, url = make_server(scanner=FakeScanner())
    status, _, body = _request(url + "api/setkey", body={"provider": "glm", "key": _RAW_KEY})
    assert status == 500
    assert "未就位" in _get_json(body)["error"]


# ---------------------------------------------------------------------------
# POST /api/test
# ---------------------------------------------------------------------------


def test_test_connection_result_passthrough(make_server) -> None:
    _srv, url = make_server(tester=_ok_tester, scanner=FakeScanner(), manager=FakeManager())
    status, _, body = _request(url + "api/test", body={"spec": "ollama:llava"})
    assert status == 200
    assert _get_json(body) == {"ok": True, "latency_ms": 12.5, "model": "ollama:llava", "provider": "ollama"}


def test_test_inband_error_still_200(make_server) -> None:
    """A147 口径:VlmConfigError 等已转结果体内中文 error,HTTP 层透传 200。"""

    def tester(spec: str) -> dict:
        return {"ok": False, "latency_ms": None, "error": "云端提供方未开启 vlm_online,拒绝外呼"}

    _srv, url = make_server(tester=tester, scanner=FakeScanner())
    status, _, body = _request(url + "api/test", body={"spec": "openai:gpt-4o"})
    assert status == 200
    assert _get_json(body)["ok"] is False


@pytest.mark.parametrize("payload", [{}, {"spec": ""}])
def test_test_missing_spec_400(make_server, payload: dict) -> None:
    _srv, url = make_server(tester=_ok_tester, scanner=FakeScanner())
    status, _, body = _request(url + "api/test", body=payload)
    assert status == 400
    assert "spec" in _get_json(body)["error"]


def test_test_tester_exception_500_chinese(make_server) -> None:
    def boom(spec: str) -> dict:
        raise RuntimeError("测试线程炸了")

    _srv, url = make_server(tester=boom, scanner=FakeScanner())
    status, _, body = _request(url + "api/test", body={"spec": "ollama:llava"})
    assert status == 500
    assert "服务器内部错误" in _get_json(body)["error"]


def test_test_module_absent_500(make_server, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_absent(monkeypatch, "netsentinel.vision.connectivity")
    _srv, url = make_server(scanner=FakeScanner())
    status, _, body = _request(url + "api/test", body={"spec": "ollama:llava"})
    assert status == 500
    assert "未就位" in _get_json(body)["error"]


# ---------------------------------------------------------------------------
# 协议容错:坏 JSON / 404 / 405 / 请求体超限
# ---------------------------------------------------------------------------


def test_bad_json_body_400_chinese(make_server) -> None:
    _srv, url = make_server(scanner=FakeScanner(), manager=FakeManager())
    status, ctype, body = _request(url + "api/activate", body="{oops 不是 json")
    assert status == 400 and ctype.startswith("application/json")
    assert "JSON" in _get_json(body)["error"]


def test_non_object_json_body_400(make_server) -> None:
    _srv, url = make_server(scanner=FakeScanner())
    status, _, body = _request(url + "api/activate", body="[1, 2, 3]")
    assert status == 400
    assert "JSON 对象" in _get_json(body)["error"]


def test_empty_body_when_required_400(make_server) -> None:
    _srv, url = make_server(scanner=FakeScanner())
    status, _, body = _request(url + "api/activate", body="")
    assert status == 400
    assert "请求体为空" in _get_json(body)["error"]


def test_body_over_limit_400(make_server, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup_server, "_MAX_BODY_BYTES", 16)
    _srv, url = make_server(scanner=FakeScanner())
    payload = json.dumps({"spec": "ollama:llava", "pad": "x" * 64}).encode("utf-8")
    status, _, body = _request(url + "api/activate", body=payload)
    assert status == 400
    assert "过大" in _get_json(body)["error"]


@pytest.mark.parametrize("path", ["/nope", "/api/unknown", "/api/", "/static/path"])
def test_unknown_path_404_json_chinese(make_server, path: str) -> None:
    _srv, url = make_server(scanner=FakeScanner())
    for method in ("GET", "POST"):
        status, ctype, body = _request(url + path.lstrip("/"), method=method, body="{}")
        assert status == 404 and ctype.startswith("application/json")
        assert "不存在" in _get_json(body)["error"]


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_method_not_allowed_405_json_chinese(make_server, method: str) -> None:
    _srv, url = make_server(scanner=FakeScanner())
    status, ctype, body = _request(url + "api/status", method=method)
    assert status == 405 and ctype.startswith("application/json")
    assert "方法不支持" in _get_json(body)["error"]


def test_post_root_405(make_server) -> None:
    _srv, url = make_server(scanner=FakeScanner())
    status, _, body = _request(url, method="POST", body="{}")
    assert status == 405
    assert "GET" in _get_json(body)["error"]


# ---------------------------------------------------------------------------
# 并发(证明 ThreadingHTTPServer 真并行,红线 32 内的回环压测)
# ---------------------------------------------------------------------------


def test_five_concurrent_status_requests_all_served(make_server) -> None:
    scanner = BarrierScanner(5)  # 若被串行处理,Barrier 10s 超时破裂 → 500
    mgr = FakeManager(active="ollama:llava")
    _srv, url = make_server(scanner=scanner, manager=mgr)

    def one(_: int) -> tuple[int, str, str]:
        return _request(url + "api/status", body={})

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(one, range(5)))
    assert len(results) == 5
    for status, ctype, body in results:
        assert status == 200 and ctype.startswith("application/json")
        payload = _get_json(body)
        assert payload["active"] == "ollama:llava"
        assert payload["local"] == _SCAN_RESULT
