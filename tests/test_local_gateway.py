# -*- coding: utf-8 -*-
"""netsentinel.vision.local_gateway 单元测试(A66)。

全部离线(红线 20):仅访问 127.0.0.1 上随机端口的本地 http.server,
用其 mock OpenAI 兼容 ``/v1/models`` 的各种形态(正常列表 / 空表 / 404 /
超时挂起 / 坏 JSON / 缺 data 字段);不触达任何真实推理服务或外网地址。
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.vision import local_gateway

# ---------------------------------------------------------------------------
# 本地 mock 网关(127.0.0.1 随机端口,threading + http.server)
# ---------------------------------------------------------------------------

#: /v1/models 正常形态:含重复 id、非字符串 id、缺 id 项,用于验证容错解析
_MODELS_OK_BODY = {
    "object": "list",
    "data": [
        {"id": "llava", "object": "model"},
        {"id": "qwen2.5-vl-7b-instruct"},
        {"id": 123},          # 非字符串 id → 剔除
        {"object": "model"},  # 缺 id → 剔除
        {"id": "llava"},      # 重复 → 去重
    ],
}


class _GatewayHandler(BaseHTTPRequestHandler):
    """按路径回放本地推理网关 /models 接口的各形态。"""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return  # 静默访问日志,避免污染 pytest 输出

    def _send_json(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (ConnectionError, BrokenPipeError):  # V5 用例:客户端读满上限即断开
            pass  # 提前断读属预期,静默即可,不刷 traceback

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定命名
        if self.path == "/v1/models":
            self._send_json(200, json.dumps(_MODELS_OK_BODY, ensure_ascii=False))
        elif self.path == "/empty/models":
            self._send_json(200, json.dumps({"object": "list", "data": []}))
        elif self.path == "/bad/models":
            self._send_json(200, "这不是 JSON{{")  # 200 但坏体
        elif self.path == "/nodata/models":
            self._send_json(200, json.dumps({"object": "list"}))  # 缺 data 字段
        elif self.path == "/slow/models":
            time.sleep(1.0)  # 挂起超过探测超时
            self._send_json(200, json.dumps({"data": []}))
        else:  # 其余路径一律 404
            self._send_json(404, json.dumps({"error": {"message": "not found"}}))


@pytest.fixture()
def gateway_server():
    """运行在 127.0.0.1 随机端口的后台线程 mock 网关。"""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
    thread = threading.Thread(
        target=httpd.serve_forever, daemon=True, name="a66-local-gateway"
    )
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _base(httpd: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{httpd.server_address[1]}"


# ---------------------------------------------------------------------------
# is_local:四家本地提供方
# ---------------------------------------------------------------------------


def test_is_local_four_local_providers():
    for name in ("ollama", "vllm", "lmstudio", "xinference"):
        assert local_gateway.is_local(name) is True
        assert local_gateway.is_local(name.upper()) is True  # 大小写不敏感
        assert local_gateway.is_local(f"  {name} ") is True  # 容忍首尾空白


def test_is_local_cloud_and_unknown_are_false():
    for name in ("glm", "openai", "anthropic", "gemini", "qwen", "doubao", "unknown", ""):
        assert local_gateway.is_local(name) is False


# ---------------------------------------------------------------------------
# probe:成功解析 / 容错
# ---------------------------------------------------------------------------


def test_probe_ok_parses_model_ids(gateway_server):
    result = local_gateway.probe(_base(gateway_server) + "/v1")
    assert result == {
        "ok": True,
        "models": ["llava", "qwen2.5-vl-7b-instruct"],
        "error": "",
    }


def test_probe_accepts_trailing_slash(gateway_server):
    result = local_gateway.probe(_base(gateway_server) + "/v1/")
    assert result["ok"] is True
    assert result["models"] == ["llava", "qwen2.5-vl-7b-instruct"]


def test_probe_empty_model_list_is_ok(gateway_server):
    result = local_gateway.probe(_base(gateway_server) + "/empty")
    assert result == {"ok": True, "models": [], "error": ""}


def test_probe_http_404(gateway_server):
    result = local_gateway.probe(_base(gateway_server) + "/nope")
    assert result["ok"] is False
    assert result["models"] == []
    assert "404" in result["error"]


def test_probe_timeout_short(gateway_server):
    result = local_gateway.probe(_base(gateway_server) + "/slow", timeout=0.2)
    assert result["ok"] is False
    assert result["models"] == []
    assert "超时" in result["error"]


def test_probe_bad_json_body(gateway_server):
    result = local_gateway.probe(_base(gateway_server) + "/bad")
    assert result["ok"] is False
    assert result["models"] == []
    assert "JSON" in result["error"]


def test_probe_missing_data_field(gateway_server):
    result = local_gateway.probe(_base(gateway_server) + "/nodata")
    assert result["ok"] is False
    assert result["models"] == []
    assert "data" in result["error"]


# ---------------------------------------------------------------------------
# probe:仅本机地址(非本机一律拒绝,且不发起任何网络请求)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "remote",
    [
        "http://api.openai.com/v1",
        "https://example.com/v1",
        "http://192.168.1.10:11434/v1",        # 内网地址也拒绝:仅回环
        "http://10.0.0.2:9997/v1",
        "http://[::ffff:127.0.0.1]:11434/v1",  # IPv4 映射形式不认
        "http://[2001:db8::1]:11434/v1",
        "file:///etc/passwd",
        "ftp://127.0.0.1/v1",
        "not-a-url",
        "http://127.0.0.1.evil.com/v1",  # DNS 混淆:看似回环实为公网域名
    ],
)
def test_probe_rejects_non_local_addresses(remote):
    result = local_gateway.probe(remote, timeout=0.2)
    assert result == {"ok": False, "models": [], "error": "仅允许探测本机地址"}


def test_probe_empty_base_url():
    result = local_gateway.probe("")
    assert result["ok"] is False
    assert result["models"] == []
    assert result["error"]  # 中文原因非空


def test_probe_accepts_localhost_hostname(gateway_server):
    """localhost 属于白名单(只验证放行,不依赖其解析到 v4 还是 v6)。"""
    result = local_gateway.probe(
        f"http://localhost:{gateway_server.server_address[1]}/nope", timeout=2.0
    )
    assert result["error"] != "仅允许探测本机地址"


# ---------------------------------------------------------------------------
# local_status:四家汇总
# ---------------------------------------------------------------------------


def test_local_status_summary_structure(gateway_server):
    base = _base(gateway_server)
    cfg = Config()
    cfg.vlm_provider_base_urls = {
        "ollama": base + "/v1",       # 在线,返回模型列表
        "vllm": base + "/empty",      # 在线,空模型表
        "lmstudio": base + "/nope",   # HTTP 404
        "xinference": base + "/bad",  # 200 但坏 JSON
    }
    status = local_gateway.local_status(cfg)

    assert set(status) == {"ollama", "vllm", "lmstudio", "xinference"}
    for name, entry in status.items():
        assert set(entry) == {"ok", "models", "error", "base_url"}
        assert entry["base_url"] == cfg.vlm_provider_base_urls[name]

    assert status["ollama"]["ok"] is True
    assert status["ollama"]["models"] == ["llava", "qwen2.5-vl-7b-instruct"]
    assert status["vllm"] == {
        "ok": True,
        "models": [],
        "error": "",
        "base_url": base + "/empty",
    }
    assert status["lmstudio"]["ok"] is False
    assert "404" in status["lmstudio"]["error"]
    assert status["xinference"]["ok"] is False
    assert "JSON" in status["xinference"]["error"]


def test_local_status_uses_builtin_catalog_defaults(monkeypatch):
    """未配置覆盖时,base_url 回落目录缺省(A61 未就位用内置值,与契约 §2 一致)。"""
    monkeypatch.setattr(
        local_gateway, "probe", lambda b, timeout=2.0: {"ok": False, "models": [], "error": "stub"}
    )
    status = local_gateway.local_status(Config())
    assert set(status) == {"ollama", "vllm", "lmstudio", "xinference"}
    assert status["ollama"]["base_url"] == "http://127.0.0.1:11434/v1"
    assert status["vllm"]["base_url"] == "http://127.0.0.1:8000/v1"
    assert status["lmstudio"]["base_url"] == "http://127.0.0.1:1234/v1"
    assert status["xinference"]["base_url"] == "http://127.0.0.1:9997/v1"


def test_local_status_cfg_override_wins(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        local_gateway,
        "probe",
        lambda b, timeout=2.0: seen.append(b) or {"ok": False, "models": [], "error": "stub"},
    )
    cfg = Config()
    cfg.vlm_provider_base_urls = {"ollama": "http://127.0.0.1:1/v1"}
    status = local_gateway.local_status(cfg)
    assert seen[0] == "http://127.0.0.1:1/v1"  # cfg 覆盖优先于目录缺省
    assert status["ollama"]["base_url"] == "http://127.0.0.1:1/v1"


def test_local_status_not_cached(monkeypatch):
    """契约 A66:local_status 不缓存,每次调用都对四家现查现算。"""
    calls: list[str] = []

    def fake_probe(base_url: str, timeout: float = 2.0) -> dict:
        calls.append(base_url)
        return {"ok": False, "models": [], "error": "stub"}

    monkeypatch.setattr(local_gateway, "probe", fake_probe)
    cfg = Config()
    local_gateway.local_status(cfg)
    local_gateway.local_status(cfg)
    assert len(calls) == 8  # 2 次 × 4 家,绝不复用上次结果


def test_local_status_refuses_remote_override():
    """纵深防御:即使覆盖配置里混入非本机地址,probe 也会逐家拒绝且零外呼。"""
    cfg = Config()
    cfg.vlm_provider_base_urls = {"ollama": "https://api.openai.com/v1"}
    status = local_gateway.local_status(cfg, timeout=1.0)
    assert status["ollama"]["ok"] is False
    assert status["ollama"]["models"] == []
    assert status["ollama"]["error"] == "仅允许探测本机地址"
    assert status["ollama"]["base_url"] == "https://api.openai.com/v1"


# ---------------------------------------------------------------------------
# V5 升级:遥测计数 / 超时分级 / 响应体上限 / local_status 并发
# ---------------------------------------------------------------------------


def test_v5_probe_telemetry_counters(gateway_server):
    """V5 可观测:每次 probe 计 local.probe,失败/拒绝分别计 errors/denied。"""
    telemetry.reset()
    ok = local_gateway.probe(_base(gateway_server) + "/v1")
    http_err = local_gateway.probe(_base(gateway_server) + "/nope")
    denied = local_gateway.probe("https://example.com/v1")
    assert ok["ok"] is True and http_err["ok"] is False and denied["ok"] is False
    snap = telemetry.snapshot()
    assert snap["counters"]["local.probe"] == 3
    assert snap["counters"]["local.probe.errors"] == 1  # 404 一次(网络失败)
    assert snap["counters"]["local.probe.denied"] == 1  # 非本机拒绝单列,不计 errors
    assert snap["timers"]["local.probe.duration"]["count"] == 2  # 拒绝路径不计时


def test_v5_probe_read_timeout_graded_message(gateway_server):
    """V5 分级超时:连接建立后响应慢 → 读取档超时,文案指明读取超时与时长。"""
    result = local_gateway.probe(_base(gateway_server) + "/slow", timeout=0.2)
    assert result["ok"] is False
    assert result["models"] == []
    assert "读取超时" in result["error"]
    assert "0.2s" in result["error"]


def test_v5_probe_connect_timeout_kwarg_accepted(gateway_server):
    """V5 分级超时:connect_timeout 关键字可用,正常路径不受影响。"""
    result = local_gateway.probe(_base(gateway_server) + "/v1", timeout=2.0, connect_timeout=0.5)
    assert result["ok"] is True
    assert result["models"] == ["llava", "qwen2.5-vl-7b-instruct"]


def test_v5_split_endpoint_variants():
    """V5:URL → (scheme, host, port, path) 拆解(缺省端口 / IPv6 / 查询串)。"""
    assert local_gateway._split_endpoint("http://127.0.0.1:11434/v1/models") == (
        "http",
        "127.0.0.1",
        11434,
        "/v1/models",
    )
    assert local_gateway._split_endpoint("https://localhost/v1/models") == (
        "https",
        "localhost",
        443,
        "/v1/models",
    )
    assert local_gateway._split_endpoint("http://127.0.0.1:9/x/models?q=1") == (
        "http",
        "127.0.0.1",
        9,
        "/x/models?q=1",
    )
    assert local_gateway._split_endpoint("http://[::1]:8080/v1/models") == (
        "http",
        "::1",
        8080,
        "/v1/models",
    )


def test_v5_probe_response_size_cap(gateway_server, monkeypatch):
    """V5 健壮:响应体超过 MAX_RESPONSE_BYTES 即停止读取并回报错误。"""
    monkeypatch.setattr(local_gateway, "MAX_RESPONSE_BYTES", 16)
    result = local_gateway.probe(_base(gateway_server) + "/v1")
    assert result["ok"] is False
    assert result["models"] == []
    assert "16 字节上限" in result["error"]


def test_v5_local_status_concurrent_wall_time(monkeypatch):
    """V5 性能:workers=4 并发探测,墙钟 ≈1× 单家耗时(串行为 4×)。"""
    lock = threading.Lock()
    active = [0, 0]  # [当前并发, 峰值并发]

    def slow_probe(base_url: str, timeout: float = 2.0) -> dict:
        with lock:
            active[0] += 1
            active[1] = max(active[1], active[0])
        time.sleep(0.25)  # 模拟每家 0.25s 网络等待
        with lock:
            active[0] -= 1
        return {"ok": False, "models": [], "error": "stub"}

    monkeypatch.setattr(local_gateway, "probe", slow_probe)
    started = time.perf_counter()
    status = local_gateway.local_status(Config(), workers=4)
    elapsed = time.perf_counter() - started

    assert set(status) == {"ollama", "vllm", "lmstudio", "xinference"}
    assert elapsed < 0.7  # 串行下限 4×0.25=1.0s,并发 ≈0.25s
    assert active[1] >= 2  # 确实发生了并行探测


def test_v5_local_status_default_workers_is_serial(monkeypatch):
    """V5 兼容:默认 workers=1 串行逐家(含注入桩的调用顺序),与旧版一致。"""
    lock = threading.Lock()
    active = [0, 0]

    def probe_rec(base_url: str, timeout: float = 2.0) -> dict:
        with lock:
            active[0] += 1
            active[1] = max(active[1], active[0])
        time.sleep(0.02)
        with lock:
            active[0] -= 1
        return {"ok": False, "models": [], "error": "stub"}

    monkeypatch.setattr(local_gateway, "probe", probe_rec)
    status = local_gateway.local_status(Config())
    assert active[1] == 1  # 全程无并发
    assert list(status) == ["ollama", "vllm", "lmstudio", "xinference"]
