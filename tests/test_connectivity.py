# -*- coding: utf-8 -*-
"""netsentinel.vision.connectivity 单元测试(A147 · V8)。

全部离线(红线 20/34):云端与本地路径一律注入 mock transport,不发任何
真实外呼;唯一的"无注入传输"用例也只访问 127.0.0.1 上随机端口的本地
mock 服务(与 A66 test_local_gateway 同款做法,红线 32 仅回环)。

覆盖:本地命中/模型名不符(大小写)/超时/连接失败/HTTP 错/坏 JSON/缺
data 字段;云端双条件缺一即拒且 transport 计数 0(红线 34)、成功路径
(latency>0 且真送 1x1 PNG)、预算拒绝、ModelNotFound、VlmConfigError、
模块未就位降级;spec 非法;1x1 PNG 合法性(签名断言);telemetry 计数。
"""
from __future__ import annotations

import base64
import importlib
import json
import struct
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.vision import connectivity

# ---------------------------------------------------------------------------
# 测试替身:传输层 spy / 预算 spy
# ---------------------------------------------------------------------------


class _TransportSpy:
    """可注入传输层:记录全部调用;固定回放结果或抛固定异常。"""

    def __init__(self, result: tuple[int, str] | None = None, exc: Exception | None = None):
        self.calls: list[dict] = []
        self._result = result
        self._exc = exc

    def __call__(self, url, headers, payload, timeout):
        self.calls.append(
            {"url": url, "headers": dict(headers), "payload": payload, "timeout": timeout}
        )
        if self._exc is not None:
            raise self._exc
        return self._result


class _SpendSpy:
    """预算记账 spy:计数;可选抛异常(预算尽/记账失败)。"""

    def __init__(self, exc: Exception | None = None):
        self.calls = 0
        self._exc = exc

    def __call__(self):
        self.calls += 1
        if self._exc is not None:
            raise self._exc


def _models_body(*names: str) -> str:
    """OpenAI 兼容 /v1/models 响应体。"""
    return json.dumps({"object": "list", "data": [{"id": n} for n in names]})


def _scoring_ok_body() -> str:
    """云端评分 ping 的成功响应(openai 方言,choices[0].message.content)。"""
    content = json.dumps(
        {"nsfw_prob": 0.01, "categories": [], "reasoning": "1x1 灰色测试图,无违规内容", "confidence": 0.9},
        ensure_ascii=False,
    )
    return json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": content}}]},
        ensure_ascii=False,
    )


def _cloud_cfg(**overrides) -> Config:
    """云端测试配置:双条件齐备(vlm_online=True + openai 密钥)。"""
    cfg = Config()
    cfg.vlm_online = True
    cfg.vlm_api_keys = {"openai": "sk-test-a147-secret-key-000"}
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


# ---------------------------------------------------------------------------
# 本地提供方:GET {base}/models(红线 32:零预算、零评分外呼)
# ---------------------------------------------------------------------------


def test_local_success_returns_ok_mode_local():
    spy = _TransportSpy(result=(200, _models_body("llava", "qwen2.5-vl-7b-instruct")))
    result = connectivity.test_connection("ollama:llava", Config(), transport=spy)
    assert set(result) == {"ok", "latency_ms", "model", "mode"}
    assert result["ok"] is True
    assert result["model"] == "llava"
    assert result["mode"] == "local"
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["url"] == "http://127.0.0.1:11434/v1/models"  # GET {base}/models
    assert call["payload"] is None  # GET 探测,无评分载荷
    assert call["timeout"] == connectivity.LOCAL_TIMEOUT_S == 3.0  # 固定 3.0s
    assert "error" not in result


def test_local_match_is_case_insensitive_both_directions():
    # 服务端大写、请求小写
    spy = _TransportSpy(result=(200, _models_body("LLAVA")))
    assert connectivity.test_connection("ollama:llava", Config(), transport=spy)["ok"] is True
    # 请求大写、服务端小写;model 字段回显请求侧模型名
    spy2 = _TransportSpy(result=(200, _models_body("llava")))
    result = connectivity.test_connection("ollama:LLaVA", Config(), transport=spy2)
    assert result["ok"] is True
    assert result["model"] == "LLaVA"
    assert result["mode"] == "local"


def test_local_model_not_in_list_lists_available():
    spy = _TransportSpy(result=(200, _models_body("qwen2.5-vl-7b-instruct", "minicpm-v")))
    result = connectivity.test_connection("ollama:llava", Config(), transport=spy)
    assert result["ok"] is False
    assert "llava" in result["error"]
    assert "qwen2.5-vl-7b-instruct" in result["error"]  # 给出可用模型提示
    assert len(spy.calls) == 1


def test_local_empty_model_list_is_failure():
    spy = _TransportSpy(result=(200, _models_body()))
    result = connectivity.test_connection("ollama:llava", Config(), transport=spy)
    assert result["ok"] is False
    assert "未加载模型" in result["error"]


def test_local_timeout_returns_chinese_error():
    spy = _TransportSpy(exc=TimeoutError("timed out"))
    result = connectivity.test_connection("ollama:llava", Config(), transport=spy)
    assert result["ok"] is False
    assert "超时" in result["error"]
    assert len(spy.calls) == 1


def test_local_connection_refused_returns_chinese_error():
    spy = _TransportSpy(exc=ConnectionRefusedError(10061, "connect refused"))
    result = connectivity.test_connection("ollama:llava", Config(), transport=spy)
    assert result["ok"] is False
    assert "无法连接本地服务" in result["error"]


def test_local_http_404_is_error():
    spy = _TransportSpy(result=(404, json.dumps({"error": "not found"})))
    result = connectivity.test_connection("ollama:llava", Config(), transport=spy)
    assert result["ok"] is False
    assert "404" in result["error"]


def test_local_http_500_is_error():
    spy = _TransportSpy(result=(500, "internal error"))
    result = connectivity.test_connection("ollama:llava", Config(), transport=spy)
    assert result["ok"] is False
    assert "500" in result["error"]


def test_local_bad_json_body_is_error():
    spy = _TransportSpy(result=(200, "这不是 JSON{{"))
    result = connectivity.test_connection("ollama:llava", Config(), transport=spy)
    assert result["ok"] is False
    assert "JSON" in result["error"]


def test_local_missing_data_field_is_error():
    spy = _TransportSpy(result=(200, json.dumps({"object": "list"})))
    result = connectivity.test_connection("ollama:llava", Config(), transport=spy)
    assert result["ok"] is False
    assert "data" in result["error"]


def test_local_zero_budget_zero_scoring_red_line_32():
    """红线 32:本地探测只清点模型——零预算记账、零评分外呼。"""
    spend = _SpendSpy()
    spy = _TransportSpy(result=(200, _models_body("llava")))
    result = connectivity.test_connection("ollama:llava", Config(), transport=spy, spend=spend)
    assert result["ok"] is True
    assert spend.calls == 0  # 零预算记账
    assert len(spy.calls) == 1  # 仅一次 GET /models
    assert spy.calls[0]["payload"] is None  # 不是评分 POST
    assert not spy.calls[0]["url"].endswith("chat/completions")


def test_local_non_loopback_base_url_is_denied_red_line_32():
    cfg = Config()
    cfg.vlm_provider_base_urls = {"ollama": "http://192.168.1.5:11434/v1"}
    spy = _TransportSpy(result=(200, _models_body("llava")))
    result = connectivity.test_connection("ollama:llava", cfg, transport=spy)
    assert result["ok"] is False
    assert "回环" in result["error"]
    assert spy.calls == []  # 拒绝时零网络


def test_local_spec_without_model_is_error():
    # vllm 目录无默认模型:resolve 抛"本地提供方必须指定模型"(ollama 有默认 llava 不适用)
    spy = _TransportSpy(result=(200, _models_body("llava")))
    result = connectivity.test_connection("vllm", Config(), transport=spy)
    assert result["ok"] is False
    assert "必须指定模型" in result["error"]
    assert spy.calls == []


def test_local_default_transport_urllib_against_loopback_mock():
    """缺省 urllib GET 路径:仅 127.0.0.1 随机端口 mock 服务(红线 32)。"""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackModelsHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True, name="a147-loopback")
    thread.start()
    try:
        cfg = Config()
        cfg.vlm_provider_base_urls = {
            "ollama": f"http://127.0.0.1:{httpd.server_address[1]}/v1"
        }
        result = connectivity.test_connection("ollama:llava", cfg)
        assert result["ok"] is True
        assert result["mode"] == "local"
        assert result["model"] == "llava"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class _LoopbackModelsHandler(BaseHTTPRequestHandler):
    """127.0.0.1 mock:/v1/models 返回 llava(仅供缺省传输用例)。"""

    def log_message(self, format, *args):  # noqa: A002
        return

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/models"):
            body = json.dumps({"object": "list", "data": [{"id": "llava"}]}).encode("utf-8")
            self.send_response(200)
        else:
            body = b"{}"
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# 云端提供方:双条件(红线 34)+ 预算(红线 19)+ 1x1 PNG 评分 ping
# ---------------------------------------------------------------------------


def test_cloud_success_ok_mode_cloud():
    events: list[str] = []
    spend_calls: list[int] = []

    def spend() -> None:
        events.append("spend")
        spend_calls.append(1)

    def transport(url, headers, payload, timeout):
        events.append("transport")
        return (200, _scoring_ok_body())

    cfg = _cloud_cfg()
    result = connectivity.test_connection(
        "openai:gpt-4o-mini", cfg, transport=transport, spend=spend
    )
    assert result["ok"] is True
    assert result["mode"] == "cloud"
    assert result["model"] == "gpt-4o-mini"
    assert isinstance(result["latency_ms"], int) and result["latency_ms"] > 0
    assert events == ["spend", "transport"]  # 先记账、后外呼(红线 19)
    assert len(spend_calls) == 1


def test_cloud_success_sends_1x1_png_image():
    spy = _TransportSpy(result=(200, _scoring_ok_body()))
    result = connectivity.test_connection(
        "openai:gpt-4o-mini", _cloud_cfg(), transport=spy, spend=_SpendSpy()
    )
    assert result["ok"] is True
    assert len(spy.calls) == 1
    url, payload = spy.calls[0]["url"], spy.calls[0]["payload"]
    assert url == "https://api.openai.com/v1/chat/completions"
    assert spy.calls[0]["headers"]["Authorization"].startswith("Bearer ")
    png_b64 = base64.b64encode(connectivity.make_1x1_png()).decode("ascii")
    assert png_b64 in json.dumps(payload, ensure_ascii=False)  # 真送 1x1 PNG
    assert payload["model"] == "gpt-4o-mini"


def test_cloud_vlm_online_off_rejects_without_any_call_red_line_34():
    spy = _TransportSpy(result=(200, _scoring_ok_body()))
    spend = _SpendSpy()
    cfg = _cloud_cfg(vlm_online=False)  # 密钥在,但 vlm_online=False
    result = connectivity.test_connection(
        "openai:gpt-4o-mini", cfg, transport=spy, spend=spend
    )
    assert result["ok"] is False
    assert "vlm_online" in result["error"]
    assert spy.calls == []  # 双条件缺一即拒:transport 计数 0
    assert spend.calls == 0


def test_cloud_key_missing_rejects_without_any_call_red_line_34(monkeypatch):
    providers = importlib.import_module("netsentinel.vision.providers")
    stub = SimpleNamespace(
        provider="openai",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        api_key="",  # 未配置密钥(替身注入,免疫环境变量/密钥文件)
        style="openai",
        local=False,
    )
    monkeypatch.setattr(providers, "resolve", lambda spec, cfg=None: stub)
    spy = _TransportSpy(result=(200, _scoring_ok_body()))
    spend = _SpendSpy()
    result = connectivity.test_connection(
        "openai:gpt-4o-mini", _cloud_cfg(), transport=spy, spend=spend
    )
    assert result["ok"] is False
    assert "密钥" in result["error"]
    assert spy.calls == []  # 不外呼(红线 34)
    assert spend.calls == 0


def test_cloud_budget_exhausted_cancels_call_red_line_19():
    spend = _SpendSpy(exc=RuntimeError("当日 VLM 调用预算已用尽:200/200。可调大 vlm_daily_budget 或明日重试。"))
    spy = _TransportSpy(result=(200, _scoring_ok_body()))
    result = connectivity.test_connection(
        "openai:gpt-4o-mini", _cloud_cfg(), transport=spy, spend=spend
    )
    assert result["ok"] is False
    assert "预算" in result["error"] and "已用尽" in result["error"]
    assert spend.calls == 1
    assert spy.calls == []  # 预算尽 → 取消外呼,transport 计数 0


def test_cloud_model_not_found_returns_chinese_error():
    body = json.dumps({"error": {"message": "The model 'gpt-4o-mini' does not exist"}})
    spy = _TransportSpy(result=(404, body))
    spend = _SpendSpy()
    result = connectivity.test_connection(
        "openai:gpt-4o-mini", _cloud_cfg(), transport=spy, spend=spend
    )
    assert result["ok"] is False
    assert "模型" in result["error"]
    assert "gpt-4o-mini" in result["error"]
    assert spend.calls == 1  # 记账先于外呼(外呼失败不退账,与生产链路一致)
    assert len(spy.calls) == 1


def test_cloud_http_500_returns_chinese_error():
    spy = _TransportSpy(result=(500, "upstream boom"))
    result = connectivity.test_connection(
        "openai:gpt-4o-mini", _cloud_cfg(), transport=spy, spend=_SpendSpy()
    )
    assert result["ok"] is False
    assert "500" in result["error"]


def test_cloud_transport_generic_exception_never_raises():
    spy = _TransportSpy(exc=RuntimeError("boom"))
    result = connectivity.test_connection(
        "openai:gpt-4o-mini", _cloud_cfg(), transport=spy, spend=_SpendSpy()
    )
    assert result["ok"] is False
    assert "失败" in result["error"]


def test_cloud_vlm_config_error_returns_chinese_error(monkeypatch):
    providers = importlib.import_module("netsentinel.vision.providers")
    stub = SimpleNamespace(  # model 缺失 → vlm_client 抛 VlmConfigError
        provider="openai",
        base_url="https://api.openai.com/v1",
        model="",
        api_key="sk-test-a147-secret-key-000",
        style="openai",
        local=False,
    )
    monkeypatch.setattr(providers, "resolve", lambda spec, cfg=None: stub)
    spy = _TransportSpy(result=(200, _scoring_ok_body()))
    result = connectivity.test_connection(
        "openai:gpt-4o-mini", _cloud_cfg(), transport=spy, spend=_SpendSpy()
    )
    assert result["ok"] is False
    assert "前置条件" in result["error"]
    assert spy.calls == []


def test_cloud_default_spend_uses_lazy_vlm_cache(tmp_path):
    """spend 缺省时惰性走 vlm_cache.spend_one:预算 1 次,第 2 次被拒。"""
    cfg = _cloud_cfg(vlm_cache_db=str(tmp_path / "budget.db"), vlm_daily_budget=1)
    spy = _TransportSpy(result=(200, _scoring_ok_body()))
    first = connectivity.test_connection("openai:gpt-4o-mini", cfg, transport=spy)
    assert first["ok"] is True
    second = connectivity.test_connection("openai:gpt-4o-mini", cfg, transport=spy)
    assert second["ok"] is False
    assert "预算" in second["error"] and "已用尽" in second["error"]
    assert len(spy.calls) == 1  # 第 2 次在记账阶段即被拒,未再外呼


def test_cloud_vlm_cache_missing_refuses_call(monkeypatch):
    monkeypatch.setitem(sys.modules, "netsentinel.vision.vlm_cache", None)
    spy = _TransportSpy(result=(200, _scoring_ok_body()))
    # spend 不注入 → 走缺省惰性 vlm_cache 路径;模块未就位 → 按红线 19 拒绝外呼
    result = connectivity.test_connection("openai:gpt-4o-mini", _cloud_cfg(), transport=spy)
    assert result["ok"] is False
    assert "vlm_cache" in result["error"]
    assert spy.calls == []


def test_cloud_vlm_client_missing_returns_chinese_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "netsentinel.vision.vlm_client", None)
    spy = _TransportSpy(result=(200, _scoring_ok_body()))
    spend = _SpendSpy()
    result = connectivity.test_connection(
        "openai:gpt-4o-mini", _cloud_cfg(), transport=spy, spend=spend
    )
    assert result["ok"] is False
    assert "vlm_client" in result["error"]
    assert spy.calls == []
    assert spend.calls == 0


def test_cloud_error_messages_never_leak_key_red_line_17():
    body = json.dumps({"error": {"message": "bad key sk-test-a147-secret-key-000 rejected"}})
    spy = _TransportSpy(result=(401, body))
    result = connectivity.test_connection(
        "openai:gpt-4o-mini", _cloud_cfg(), transport=spy, spend=_SpendSpy()
    )
    assert result["ok"] is False
    assert "sk-test-a147-secret-key-000" not in json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# spec 解析 / 模块未就位 / 1x1 PNG / telemetry
# ---------------------------------------------------------------------------


def test_unknown_provider_returns_chinese_error():
    spy = _TransportSpy(result=(200, _models_body("llava")))
    result = connectivity.test_connection("nosuch:model", Config(), transport=spy)
    assert result["ok"] is False
    assert "未知" in result["error"]
    assert spy.calls == []


@pytest.mark.parametrize("bad_spec", ["", "   ", ":", None])
def test_invalid_or_empty_specs_never_raise(bad_spec):
    spy = _TransportSpy(result=(200, _models_body("llava")))
    result = connectivity.test_connection(bad_spec, Config(), transport=spy)
    assert result["ok"] is False
    assert isinstance(result["error"], str) and result["error"]
    assert spy.calls == []


def test_providers_module_missing_returns_chinese_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "netsentinel.vision.providers", None)
    spy = _TransportSpy(result=(200, _models_body("llava")))
    result = connectivity.test_connection("ollama:llava", Config(), transport=spy)
    assert result["ok"] is False
    assert result["error"] == "providers 模块未就位,无法解析提供方;请等目录模块落地后再试"
    assert spy.calls == []


def _png_chunks(data: bytes) -> dict[bytes, bytes]:
    """逐块解析 PNG(校验签名与每块 CRC),返回 {tag: payload}。"""
    assert data.startswith(b"\x89PNG\r\n\x1a\n")  # PNG 签名
    chunks: dict[bytes, bytes] = {}
    pos = 8
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        tag = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        assert crc == zlib.crc32(tag + payload) & 0xFFFFFFFF  # 每块 CRC 合法
        chunks[tag] = payload
        pos += 12 + length
    assert pos == len(data)
    return chunks


def test_probe_png_is_valid_1x1_png_bytes():
    """契约 A147:评分 ping 送审图为合法 1x1 PNG(签名 + 块结构逐项断言)。"""
    data = connectivity.make_1x1_png()
    chunks = _png_chunks(data)
    assert set(chunks) == {b"IHDR", b"IDAT", b"IEND"}
    assert chunks[b"IHDR"] == struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1 8bit RGB
    assert zlib.decompress(chunks[b"IDAT"]) == b"\x00\x80\x80\x80"  # filter0 + RGB 灰
    assert chunks[b"IEND"] == b""


def test_telemetry_counts_every_connectivity_test():
    telemetry.reset()
    spy = _TransportSpy(result=(200, _models_body("llava")))
    assert connectivity.test_connection("ollama:llava", Config(), transport=spy)["ok"] is True
    assert connectivity.test_connection("nosuch:x", Config(), transport=spy)["ok"] is False
    assert connectivity.test_connection("ollama:llava", Config(), transport=_TransportSpy(exc=OSError("x")))["ok"] is False
    counters = telemetry.snapshot()["counters"]
    assert counters.get("connectivity.test") == 3
    assert counters.get("connectivity.ok") == 1
    assert counters.get("connectivity.errors") == 2
