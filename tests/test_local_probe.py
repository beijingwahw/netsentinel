# -*- coding: utf-8 -*-
"""netsentinel.vision.local_probe 单元测试(A143 · V8)。

全部离线(红线 20/34):真实 HTTP 用例仅访问 127.0.0.1 上随机端口的本地
mock 服务(红线 32 仅回环,不触任何外部地址);其余用 spy opener 注入替身,
零网络。

覆盖:视觉+文本混合过滤 / 空 data 在线无模型 / 404 / 小超时窗 0.2s / 坏
JSON / 缺 data 字段 / 响应超大;端口→provider 映射目录与缺省端口表(与
contracts.Config 对齐);unfiltered 降级(capability 缺席 + filter_vision
抛异常);host 恒 127.0.0.1(源码断言 + 运行时 URL 断言);只打配置端口
(计数);scan 决定性;非法端口零网络;重定向不跟随(红线 32);连接失败
/ 超时包装 / 未预期异常的中文转译;telemetry 计数。
"""
from __future__ import annotations

import importlib
import inspect
import json
import sys
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.vision import local_probe
from netsentinel.vision.local_probe import DEFAULT_PORTS, LocalVisionScanner

#: 结果行的七键恒定形态
_ROW_KEYS = {"provider", "port", "base_url", "models", "ok", "error", "unfiltered"}

#: 视觉+文本混合名单(llava/qwen2.5-vl/minicpm-v 命中;llama3.2/gemma-7b 不命中)
_MIXED_MODELS = [
    "llava:13b",
    "llama3.2",
    "qwen2.5-vl-7b-instruct",
    "gemma-7b",
    "minicpm-v",
]
_MIXED_VISION = ["llava:13b", "qwen2.5-vl-7b-instruct", "minicpm-v"]


def _models_body(*names: str) -> str:
    """OpenAI 兼容 /v1/models 响应体。"""
    return json.dumps({"object": "list", "data": [{"id": n} for n in names]})


# ---------------------------------------------------------------------------
# 替身一:spy opener(零网络;记录调用/回放结果/抛固定异常)
# ---------------------------------------------------------------------------


class _FakeResponse:
    """urlopen 返回替身:一次性回放 body。"""

    def __init__(self, body: str, status: int = 200):
        self.status = status
        self._data = body.encode("utf-8")

    def read(self, limit: int = -1) -> bytes:
        return self._data if limit is None or limit < 0 else self._data[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _SpyOpener:
    """opener 替身:记录全部调用;固定回放 ``(status, body)`` 或抛固定异常。"""

    def __init__(self, result: tuple[int, str] | None = None, exc: Exception | None = None):
        self.calls: list[dict] = []
        self._result = result
        self._exc = exc

    def open(self, request, timeout=None):
        self.calls.append(
            {"url": request.full_url, "method": request.get_method(), "timeout": timeout}
        )
        if self._exc is not None:
            raise self._exc
        status, body = self._result if self._result is not None else (200, "{}")
        return _FakeResponse(body, status)

    @property
    def urls(self) -> list[str]:
        return [call["url"] for call in self.calls]


# ---------------------------------------------------------------------------
# 替身二:127.0.0.1 随机端口真实 mock 服务(红线 32:仅回环)
# ---------------------------------------------------------------------------


class _SilentServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # noqa: ANN001, N802
        return  # 客户端超时提前断开等噪音静默,不污染测试输出


class _MockLocalService:
    """127.0.0.1 随机端口 mock:/v1/models 按 status/body 回放,可延迟/重定向。"""

    def __init__(
        self,
        *,
        status: int = 200,
        body: str = "",
        sleep_s: float = 0.0,
        location: str | None = None,
    ):
        self.requests: list[str] = []
        outer = self
        config = {"status": status, "body": body, "sleep_s": sleep_s, "location": location}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002
                return

            def do_GET(self):  # noqa: N802
                outer.requests.append(self.path)
                time.sleep(config["sleep_s"])
                redirect = config["location"]
                if redirect is not None:  # 3xx:验证探测绝不跟随(红线 32)
                    self.send_response(302)
                    self.send_header("Location", redirect)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = config["body"].encode("utf-8")
                self.send_response(config["status"])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = _SilentServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True, name="a143-mock"
        )
        self.thread.start()

    @property
    def port(self) -> str:
        return str(self.httpd.server_address[1])

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# ---------------------------------------------------------------------------
# 真实回环 mock:核心清点路径
# ---------------------------------------------------------------------------


def test_scan_filters_vision_from_mixed_models():
    """视觉+文本混合名单 → 只留视觉模型(过滤委托 A150),七键形态齐整。"""
    with _MockLocalService(body=_models_body(*_MIXED_MODELS)) as svc:
        rows = LocalVisionScanner(timeout=1.0).scan(ports=[svc.port])
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == _ROW_KEYS
    assert row["ok"] is True
    assert row["error"] == ""
    assert row["unfiltered"] is False
    assert row["models"] == _MIXED_VISION
    assert row["port"] == svc.port
    assert row["provider"] == "openai-compat"  # 随机端口不在映射目录
    assert row["base_url"] == f"http://127.0.0.1:{svc.port}/v1"
    assert svc.requests == ["/v1/models"]


def test_scan_online_but_empty_data_is_ok_with_no_models():
    """data 为空列表:服务在线但无模型 → ok=True、models=[],同样是有效清点。"""
    with _MockLocalService(body=_models_body()) as svc:
        rows = LocalVisionScanner().scan(ports=[svc.port])
    row = rows[0]
    assert row["ok"] is True
    assert row["models"] == []
    assert row["error"] == ""
    assert row["unfiltered"] is False


def test_scan_http_404_returns_chinese_error():
    with _MockLocalService(status=404, body=json.dumps({"error": "not found"})) as svc:
        row = LocalVisionScanner().scan(ports=[svc.port])[0]
    assert row["ok"] is False
    assert "404" in row["error"]
    assert row["models"] == []
    assert row["unfiltered"] is False


def test_scan_small_timeout_window_returns_chinese_error():
    """超时窗 0.2s vs 服务延迟 1.0s → 中文超时错误(超时参数真实生效)。"""
    with _MockLocalService(body=_models_body("llava"), sleep_s=1.0) as svc:
        row = LocalVisionScanner(timeout=0.2).scan(ports=[svc.port])[0]
    assert row["ok"] is False
    assert "超时" in row["error"]
    assert row["models"] == []


def test_scan_bad_json_returns_chinese_error():
    with _MockLocalService(body="这不是 JSON{{") as svc:
        row = LocalVisionScanner().scan(ports=[svc.port])[0]
    assert row["ok"] is False
    assert "JSON" in row["error"]


def test_scan_missing_data_field_returns_chinese_error():
    with _MockLocalService(body=json.dumps({"object": "list"})) as svc:
        row = LocalVisionScanner().scan(ports=[svc.port])[0]
    assert row["ok"] is False
    assert "data" in row["error"]


def test_oversized_body_is_rejected():
    huge = json.dumps({"data": [{"id": "llava"}], "pad": "x" * ((1 << 20) + 64)})
    with _MockLocalService(body=huge) as svc:
        row = LocalVisionScanner(timeout=5.0).scan(ports=[svc.port])[0]
    assert row["ok"] is False
    assert "上限" in row["error"]


def test_redirect_is_not_followed_red_line_32():
    """红线 32:3xx 重定向(哪怕指向外部地址)一律按错误回报,绝不跟随。"""
    with _MockLocalService(location="http://example.invalid/v1/models") as svc:
        row = LocalVisionScanner().scan(ports=[svc.port])[0]
    assert row["ok"] is False
    assert "302" in row["error"]
    assert row["models"] == []


# ---------------------------------------------------------------------------
# 端口表 / provider 映射(spy opener:零网络)
# ---------------------------------------------------------------------------


def test_default_ports_provider_mapping_and_probe_order(monkeypatch):
    """缺省端口表按序逐个探测,provider 按端口映射目录回报,URL 只打 /v1/models。"""
    spy = _SpyOpener(result=(200, _models_body("llava")))
    monkeypatch.setattr(local_probe, "_OPENER", spy)
    rows = LocalVisionScanner().scan()
    assert [row["provider"] for row in rows] == ["ollama", "lmstudio", "vllm", "xinference"]
    assert [row["port"] for row in rows] == ["11434", "1234", "8000", "9997"]
    assert spy.urls == [
        f"http://127.0.0.1:{p}/v1/models" for p in ["11434", "1234", "8000", "9997"]
    ]
    assert spy.calls[0]["method"] == "GET"
    assert all(row["ok"] and row["models"] == ["llava"] for row in rows)


def test_port_provider_catalog_matches_contract():
    assert local_probe.PORT_PROVIDERS == {
        "11434": "ollama",
        "1234": "lmstudio",
        "8000": "vllm",
        "9997": "xinference",
    }


def test_default_ports_match_contracts_config():
    """缺省端口表与 contracts.Config.local_probe_ports 同源(配置即权威)。"""
    assert DEFAULT_PORTS == ["11434", "1234", "8000", "9997"]
    assert Config().local_probe_ports == DEFAULT_PORTS


def test_only_requested_ports_are_probed():
    """只打配置端口:ports=[mock] 时恰好 1 次请求,缺省 4 端口一个都不碰。"""
    with _MockLocalService(body=_models_body("llava")) as svc:
        scanner = LocalVisionScanner(ports=[svc.port])
        rows = scanner.scan()
    assert len(rows) == 1
    assert rows[0]["ok"] is True
    assert svc.requests == ["/v1/models"]  # 计数:1 次,且只此端口


def test_scan_ports_argument_overrides_constructor_once():
    with _MockLocalService(body=_models_body("llava")) as svc:
        scanner = LocalVisionScanner(ports=["12345"])
        rows = scanner.scan(ports=[svc.port])
        assert [row["port"] for row in rows] == [svc.port]
        assert svc.requests == ["/v1/models"]  # 构造端口 12345 未被打
        assert scanner.ports == ["12345"]  # 临时覆盖不改构造端口表


def test_constructor_snapshots_ports_list(monkeypatch):
    mutable = ["11434"]
    scanner = LocalVisionScanner(ports=mutable)
    mutable.append("9997")  # 构造后再改原列表不影响扫描器
    spy = _SpyOpener(result=(200, _models_body("llava")))
    monkeypatch.setattr(local_probe, "_OPENER", spy)
    scanner.scan()
    assert [row["port"] for row in scanner.scan()] == ["11434"]
    assert spy.urls == ["http://127.0.0.1:11434/v1/models"] * 2


def test_duplicate_ports_are_probed_once(monkeypatch):
    spy = _SpyOpener(result=(200, _models_body("llava")))
    monkeypatch.setattr(local_probe, "_OPENER", spy)
    rows = LocalVisionScanner(ports=["11434", "11434", "1234"]).scan()
    assert spy.urls == [
        "http://127.0.0.1:11434/v1/models",
        "http://127.0.0.1:1234/v1/models",
    ]
    assert [row["port"] for row in rows] == ["11434", "1234"]


def test_invalid_ports_error_rows_without_network(monkeypatch):
    """非法端口 → 中文报错行且零网络(红线 32:不碰任何端口)。"""
    spy = _SpyOpener(result=(200, _models_body("llava")))
    monkeypatch.setattr(local_probe, "_OPENER", spy)
    rows = LocalVisionScanner(ports=["abc", "0", "70000", "-1", "  "]).scan()
    assert len(rows) == 4  # 空白项在规整时丢弃
    assert spy.calls == []
    for row in rows:
        assert row["ok"] is False
        assert "端口" in row["error"]
        assert row["models"] == []
        assert set(row) == _ROW_KEYS


def test_scan_empty_ports_returns_empty_list():
    assert LocalVisionScanner(ports=[]).scan() == []
    assert LocalVisionScanner(ports=[]).scan(ports=[]) == []


def test_constructor_timeout_validation():
    assert LocalVisionScanner().timeout == 1.5 == local_probe.DEFAULT_TIMEOUT_S
    assert LocalVisionScanner(timeout=0.2).timeout == 0.2
    for bad in (0, -1, "abc", None):
        with pytest.raises(ValueError, match="超时"):
            LocalVisionScanner(timeout=bad)


# ---------------------------------------------------------------------------
# 红线 32:host 恒 127.0.0.1(源码断言 + 运行时 URL 断言)
# ---------------------------------------------------------------------------


def test_source_pins_loopback_host_only_red_line_32():
    """源码断言:探测主机字面量唯一来源为 127.0.0.1,无任何其他主机写法。"""
    src = inspect.getsource(local_probe)
    assert '"127.0.0.1"' in src  # _PROBE_HOST 常量
    assert local_probe._PROBE_HOST == "127.0.0.1"
    for forbidden in ("localhost", "0.0.0.0", "192.168.", "169.254.", "10.0."):
        assert forbidden not in src


def test_runtime_probe_urls_are_loopback_only(monkeypatch):
    """运行时断言:所有请求 URL 均为 http://127.0.0.1:{port}/v1/models。"""
    spy = _SpyOpener(result=(200, _models_body("llava")))
    monkeypatch.setattr(local_probe, "_OPENER", spy)
    LocalVisionScanner(ports=["11434", "54321"]).scan()
    assert len(spy.calls) == 2
    for call in spy.calls:
        assert call["url"].startswith("http://127.0.0.1:")
        assert call["url"].endswith("/v1/models")
        assert "://" in call["url"] and call["url"].count("127.0.0.1") == 1
    assert spy.calls[1]["timeout"] == 1.5  # 缺省超时确实传到 urllib


# ---------------------------------------------------------------------------
# 失败分级转中文(spy opener 注入异常形态)
# ---------------------------------------------------------------------------


def test_connection_refused_returns_chinese_error(monkeypatch):
    spy = _SpyOpener(exc=ConnectionRefusedError(10061, "connect refused"))
    monkeypatch.setattr(local_probe, "_OPENER", spy)
    row = LocalVisionScanner(ports=["11434"]).scan()[0]
    assert row["ok"] is False
    assert "无法连接" in row["error"]
    assert "11434" in row["error"]


def test_urlerror_wrapped_timeout_classified_as_timeout(monkeypatch):
    """urllib 把套接字超时包进 URLError.reason:同样按"超时"分级转中文。"""
    spy = _SpyOpener(exc=urllib.error.URLError(TimeoutError("timed out")))
    monkeypatch.setattr(local_probe, "_OPENER", spy)
    row = LocalVisionScanner(ports=["11434"]).scan()[0]
    assert row["ok"] is False
    assert "超时" in row["error"]


def test_unexpected_exception_never_raises_returns_chinese(monkeypatch):
    spy = _SpyOpener(exc=RuntimeError("boom"))
    monkeypatch.setattr(local_probe, "_OPENER", spy)
    row = LocalVisionScanner(ports=["11434"]).scan()[0]
    assert row["ok"] is False
    assert "探测失败" in row["error"]
    assert "RuntimeError" in row["error"]


# ---------------------------------------------------------------------------
# unfiltered 降级(A150 capability 缺席 / 接口异常)
# ---------------------------------------------------------------------------


def test_capability_missing_degrades_unfiltered(monkeypatch):
    """capability 缺席(sys.modules 注入 None)→ 全保留 + unfiltered=True。"""
    monkeypatch.setitem(sys.modules, "netsentinel.vision.capability", None)
    with _MockLocalService(body=_models_body(*_MIXED_MODELS)) as svc:
        row = LocalVisionScanner().scan(ports=[svc.port])[0]
    assert row["ok"] is True
    assert row["unfiltered"] is True
    assert row["models"] == _MIXED_MODELS  # 视觉+文本全保留


def test_filter_vision_failure_degrades_unfiltered(monkeypatch):
    """capability 在场但 filter_vision 抛异常 → 同样降级全保留,不向上抛。"""
    capability = importlib.import_module("netsentinel.vision.capability")

    def _boom(models):
        raise RuntimeError("capability exploded")

    monkeypatch.setattr(capability, "filter_vision", _boom)
    with _MockLocalService(body=_models_body(*_MIXED_MODELS)) as svc:
        row = LocalVisionScanner().scan(ports=[svc.port])[0]
    assert row["ok"] is True
    assert row["unfiltered"] is True
    assert row["models"] == _MIXED_MODELS


# ---------------------------------------------------------------------------
# 决定性 / telemetry
# ---------------------------------------------------------------------------


def test_scan_is_deterministic():
    """同一输入两次 scan 结果逐键相等,且 JSON 可序列化往返不变。"""
    with _MockLocalService(body=_models_body(*_MIXED_MODELS)) as svc:
        scanner = LocalVisionScanner(ports=["0", svc.port])  # "0" 非法 → 稳定报错行
        first = scanner.scan()
        second = scanner.scan()
    assert [row["port"] for row in first] == ["0", svc.port]
    assert first == second
    assert json.loads(json.dumps(first, ensure_ascii=False)) == first
    assert first[0]["ok"] is False  # 非法端口行
    assert first[1]["ok"] is True
    assert first[1]["models"] == _MIXED_VISION


def test_telemetry_hit_and_scan_counters():
    telemetry.reset()
    with (
        _MockLocalService(body=_models_body("llava", "llama3")) as hit_svc,
        _MockLocalService(body=_models_body("llama3")) as text_only_svc,
        _MockLocalService(status=404, body="{}") as down_svc,
    ):
        hit_row = LocalVisionScanner().scan(ports=[hit_svc.port])[0]
        text_row = LocalVisionScanner().scan(ports=[text_only_svc.port])[0]
        down_row = LocalVisionScanner().scan(ports=[down_svc.port])[0]
        counters = telemetry.snapshot()["counters"]
    assert hit_row["ok"] is True and hit_row["models"] == ["llava"]
    assert text_row["ok"] is True and text_row["models"] == []  # 在线但无视觉模型
    assert down_row["ok"] is False
    assert counters.get("local_probe.hit") == 1  # 仅视觉命中计 1
    assert counters.get("local_probe.scan") == 3
