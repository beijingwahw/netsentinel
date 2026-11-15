# -*- coding: utf-8 -*-
"""A32 netsentinel.notify.hub 单元测试:运营者通知中枢。

全程离线:monkeypatch ``urllib.request.urlopen`` 捕获请求(断言 URL / data /
headers / 超时),零网络外呼,不访问任何真实企微 / 钉钉 / 飞书接口。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.notify.hub import (
    GENERIC,
    PLATFORMS,
    _build_payload,
    detect_platform,
    notify,
)

#: 最终文本末尾的本地时间戳(秒级)
_TS_RE = re.compile(r"@\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

WECOM_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key"
DINGTALK_URL = "https://oapi.dingtalk.com/robot/send?access_token=test-token"
FEISHU_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/test-hook"
GENERIC_URL = "https://alerts.example.internal/hook/netsentinel"


class FakeResponse:
    """假 urlopen 响应:上下文管理器 + status/read。"""

    def __init__(self, status: int = 200, body: str = '{"ok": true}') -> None:
        self.status = status
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args) -> bool:
        return False


class Recorder:
    """monkeypatch 用假 urlopen:记录每次请求(含 timeout),可选回放异常/响应。"""

    def __init__(self, response=None, exc: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.response = response or FakeResponse()
        self.exc = exc

    def __call__(self, request, timeout=None):
        self.calls.append({"request": request, "timeout": timeout})
        if self.exc is not None:
            raise self.exc
        return self.response


def _capture(monkeypatch, url: str, response=None, exc=None) -> tuple[Recorder, Config]:
    """装好假 urlopen 并返回(记录器, 指向该 webhook 的 Config)。"""
    rec = Recorder(response=response, exc=exc)
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    return rec, Config(notify_webhook=url)


def _last_request(rec: Recorder):
    """取唯一一次请求并断言"只发一次、绝不重试"。"""
    assert len(rec.calls) == 1, f"应只发一次请求(绝不重试),实际 {len(rec.calls)} 次"
    return rec.calls[0]


def _headers_lower(request) -> dict[str, str]:
    return {k.lower(): v for k, v in request.header_items()}


# ---------------------------------------------------------------------------
# 平台识别
# ---------------------------------------------------------------------------

def test_platforms_mapping_and_detect() -> None:
    """PLATFORMS 子串映射与 detect_platform 的识别/兜底。"""
    assert PLATFORMS["qyapi.weixin"] == "wecom"
    assert PLATFORMS["dingtalk"] == "dingtalk"
    assert PLATFORMS["feishu"] == "feishu"
    assert PLATFORMS["open.feishu"] == "feishu"
    assert detect_platform(WECOM_URL) == "wecom"
    assert detect_platform(DINGTALK_URL) == "dingtalk"
    assert detect_platform(FEISHU_URL) == "feishu"
    assert detect_platform("https://open.feishu.cn/any") == "feishu"
    # 大小写不敏感
    assert detect_platform("https://QYAPI.Weixin.QQ.COM/x") == "wecom"


def test_unknown_substring_falls_back_to_generic() -> None:
    """未知平台子串 → generic(识别层 + 载荷层在下方全链路用例覆盖)。"""
    assert detect_platform("https://hooks.slack-like.example/x") == GENERIC
    assert detect_platform("") == GENERIC


def test_unknown_platform_payload_is_generic(monkeypatch) -> None:
    """未知平台走全链路:载荷为含 event / fields 的裸 JSON。"""
    rec, cfg = _capture(monkeypatch, "https://hooks.unknown.example/services/T1/B2")
    assert notify(cfg, "pending_review", "发现待复核站点", agg=0.93, site="example.net") is True
    call = _last_request(rec)
    payload = json.loads(call["request"].data.decode("utf-8"))
    assert payload == {
        "event": "pending_review",
        "text": "发现待复核站点",
        "agg": 0.93,
        "site": "example.net",
    }
    assert "msgtype" not in payload and "msg_type" not in payload


# ---------------------------------------------------------------------------
# 未配置 / 请求基本形态
# ---------------------------------------------------------------------------

def test_empty_webhook_returns_false_without_request(monkeypatch) -> None:
    """未配置 notify_webhook → False,且不发出任何请求。"""
    rec = Recorder()
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    cfg = Config(notify_webhook="")
    assert notify(cfg, "pending_review", "发现待复核站点") is False
    assert rec.calls == []


def test_request_shape_url_headers_timeout_method(monkeypatch) -> None:
    """POST JSON:URL、content-type、超时 10s、方法 POST 逐一断言。"""
    rec, cfg = _capture(monkeypatch, DINGTALK_URL)
    assert notify(cfg, "pending_review", "发现待复核站点", agg=0.6) is True
    call = _last_request(rec)
    req = call["request"]
    assert req.full_url == DINGTALK_URL
    assert req.method == "POST" or req.get_method() == "POST"
    headers = _headers_lower(req)
    assert headers.get("content-type") == "application/json"
    assert call["timeout"] == 10
    # 请求体本身是合法 UTF-8 JSON
    json.loads(req.data.decode("utf-8"))


# ---------------------------------------------------------------------------
# 四种平台载荷格式
# ---------------------------------------------------------------------------

def test_wecom_and_dingtalk_payload_same_shape(monkeypatch) -> None:
    """企微与钉钉同构:{"msgtype": "text", "text": {"content": 最终文本}}。"""
    payloads = []
    for url in (WECOM_URL, DINGTALK_URL):
        rec, cfg = _capture(monkeypatch, url)
        assert notify(cfg, "pending_review", "发现待复核站点", agg=0.87) is True
        req = _last_request(rec)["request"]
        assert req.full_url == url
        payloads.append(json.loads(req.data.decode("utf-8")))
    assert set(payloads[0]) == {"msgtype", "text"}
    assert payloads[0]["msgtype"] == "text"
    assert payloads[0] == payloads[1], "企微与钉钉载荷应同构(时间戳秒级内一致)"


def test_feishu_payload_shape(monkeypatch) -> None:
    """飞书:{"msg_type": "text", "content": {"text": 最终文本}}。"""
    rec, cfg = _capture(monkeypatch, FEISHU_URL)
    assert notify(cfg, "pending_review", "发现待复核站点", agg=0.91) is True
    payload = json.loads(_last_request(rec)["request"].data.decode("utf-8"))
    assert set(payload) == {"msg_type", "content"}
    assert payload["msg_type"] == "text"
    assert set(payload["content"]) == {"text"}
    content = payload["content"]["text"]
    assert content.startswith("[NetSentinel][pending_review] 发现待复核站点")
    assert "agg=0.91" in content
    assert _TS_RE.search(content), f"最终文本应附本地时间戳: {content!r}"


def test_generic_payload_shape(monkeypatch) -> None:
    """通用 webhook:裸 JSON,含 event / 原文 text / 展开的 fields。"""
    rec, cfg = _capture(monkeypatch, GENERIC_URL)
    assert notify(cfg, "scan_failed", "抓取失败", url="http://x.example", reason="超时") is True
    payload = json.loads(_last_request(rec)["request"].data.decode("utf-8"))
    assert payload["event"] == "scan_failed"
    assert payload["text"] == "抓取失败"
    assert payload["url"] == "http://x.example"
    assert payload["reason"] == "超时"


# ---------------------------------------------------------------------------
# 最终文本格式
# ---------------------------------------------------------------------------

def test_final_text_contains_tag_fields_and_timestamp() -> None:
    """文本格式:抬头、事件、正文、字段拼接、@本地时间。"""
    content = _build_payload(
        "wecom", "pending_review", "发现待复核站点", {"agg": 0.93, "verdict": "nsfw"}
    )["text"]["content"]
    assert content.startswith("[NetSentinel][pending_review] 发现待复核站点")
    assert "(agg=0.93, verdict=nsfw)" in content
    assert _TS_RE.search(content)


def test_final_text_without_fields_has_no_empty_parens() -> None:
    """无字段时不出现空括号,时间戳仍在。"""
    content = _build_payload("feishu", "test_event", "纯文本提醒", {})["content"]["text"]
    assert content.startswith("[NetSentinel][test_event] 纯文本提醒")
    assert "()" not in content
    assert _TS_RE.search(content)


def test_generic_payload_keeps_raw_text_without_timestamp() -> None:
    """generic 的 text 为原文(时间戳只附加在 IM 平台最终文本里)。"""
    payload = _build_payload(GENERIC, "e1", "原始正文", {"k": "v"})
    assert payload == {"event": "e1", "text": "原始正文", "k": "v"}


# ---------------------------------------------------------------------------
# 失败路径:不抛异常、返回 False
# ---------------------------------------------------------------------------

def test_http_error_500_returns_false(monkeypatch) -> None:
    """HTTPError(500) → False,不抛。"""
    rec, cfg = _capture(
        monkeypatch, WECOM_URL,
        exc=urllib.error.HTTPError(WECOM_URL, 500, "Internal Server Error", None, None),
    )
    assert notify(cfg, "pending_review", "发现待复核站点") is False
    assert len(rec.calls) == 1, "HTTP 失败后不得重试"


def test_urlerror_returns_false(monkeypatch) -> None:
    """URLError(含超时包装) → False,不抛。"""
    rec, cfg = _capture(
        monkeypatch, DINGTALK_URL,
        exc=urllib.error.URLError(TimeoutError("timed out")),
    )
    assert notify(cfg, "pending_review", "发现待复核站点") is False
    assert len(rec.calls) == 1


def test_timeout_error_returns_false(monkeypatch) -> None:
    """裸 TimeoutError(部分路径不包 URLError) → False,不抛。"""
    rec, cfg = _capture(monkeypatch, FEISHU_URL, exc=TimeoutError("connect timeout"))
    assert notify(cfg, "pending_review", "发现待复核站点") is False


def test_non_2xx_status_returns_false(monkeypatch) -> None:
    """非 2xx(响应体直接返回 500,不抛异常) → False。"""
    rec, cfg = _capture(monkeypatch, GENERIC_URL, response=FakeResponse(status=500, body="boom"))
    assert notify(cfg, "pending_review", "发现待复核站点") is False


def test_success_statuses(monkeypatch) -> None:
    """2xx(200 / 204) → True。"""
    for status in (200, 204):
        rec, cfg = _capture(monkeypatch, WECOM_URL, response=FakeResponse(status=status))
        assert notify(cfg, "pending_review", "发现待复核站点") is True
        assert len(rec.calls) == 1


# ---------------------------------------------------------------------------
# V5 升级:notify.sent / notify.fail / notify.skip 遥测;单遍文本构建行为锁定
# ---------------------------------------------------------------------------
def test_v5_notify_telemetry_sent(monkeypatch) -> None:
    """发送成功:notify.sent 计 1,且不产生 fail/skip。"""
    telemetry.reset()
    rec, cfg = _capture(monkeypatch, WECOM_URL)
    assert notify(cfg, "pending_review", "发现待复核站点") is True
    counters = telemetry.snapshot()["counters"]
    assert counters["notify.sent"] == 1
    assert counters.get("notify.fail", 0) == 0
    assert counters.get("notify.skip", 0) == 0
    assert len(rec.calls) == 1  # 单发不重试


def test_v5_notify_telemetry_fail_on_http_error_and_non_2xx(monkeypatch) -> None:
    """HTTPError 与非 2xx 响应两条失败路径都计 notify.fail。"""
    telemetry.reset()
    _, cfg1 = _capture(
        monkeypatch, WECOM_URL,
        exc=urllib.error.HTTPError(WECOM_URL, 502, "Bad Gateway", None, None),
    )
    assert notify(cfg1, "pending_review", "文本") is False
    _, cfg2 = _capture(
        monkeypatch, GENERIC_URL, response=FakeResponse(status=503, body="err")
    )
    assert notify(cfg2, "pending_review", "文本") is False
    counters = telemetry.snapshot()["counters"]
    assert counters["notify.fail"] == 2
    assert counters.get("notify.sent", 0) == 0


def test_v5_notify_telemetry_skip_when_unconfigured(monkeypatch) -> None:
    """未配置 webhook:notify.skip 计 1,不产生任何请求。"""
    telemetry.reset()
    rec = Recorder()
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    assert notify(Config(notify_webhook="   "), "pending_review", "文本") is False
    counters = telemetry.snapshot()["counters"]
    assert counters["notify.skip"] == 1
    assert counters.get("notify.sent", 0) == 0
    assert counters.get("notify.fail", 0) == 0
    assert rec.calls == []


def test_v5_final_text_single_pass_preserves_order_and_raw_values() -> None:
    """单遍拼装不改语义:字段保持插入序、值不转义、无字段时无空括号。"""
    content = _build_payload(
        "wecom", "scan_failed", "抓取失败", {"reason": "超时=10s", "n": 3}
    )["text"]["content"]
    assert "[NetSentinel][scan_failed] 抓取失败 (reason=超时=10s, n=3) @" in content
    assert content.count("(") == 1 and content.count(")") == 1  # 恰一对括号
    bare = _build_payload("feishu", "e", "纯文本", {})["content"]["text"]
    assert "()" not in bare and _TS_RE.search(bare)


# ---------------------------------------------------------------------------
# V10.35(P2-8):有界重试(opt-in;缺省 0 = 单发不重试逐字节旧行为)
# ---------------------------------------------------------------------------
class FlakyRecorder:
    """前 N 次抛 URLError、之后成功的假 urlopen;记录每次调用与休眠。"""

    def __init__(self, fail_times: int = 1) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.sleeps: list[float] = []


def _flaky(monkeypatch, fail_times: int) -> FlakyRecorder:
    import netsentinel.notify.hub as hub

    rec = FlakyRecorder(fail_times)

    def fake_urlopen(request, timeout=10):  # noqa: ANN001
        rec.calls += 1
        if rec.calls <= rec.fail_times:
            raise urllib.error.URLError("模拟网络抖动")
        return FakeResponse()

    def fake_sleep(seconds: float) -> None:
        rec.sleeps.append(float(seconds))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(hub, "_sleep", fake_sleep)
    return rec


def test_retry_default_zero_single_shot_byte_identical(monkeypatch) -> None:
    telemetry.reset()
    rec = _flaky(monkeypatch, fail_times=1)
    cfg = Config(notify_webhook=GENERIC_URL)  # notify_retry_max 缺省 0
    assert notify(cfg, "pending_review", "x") is False
    assert rec.calls == 1                 # 只发一次,绝不重试(V2 红线缺省)
    assert rec.sleeps == []               # 零退避等待
    snap = telemetry.snapshot()["counters"]
    assert snap.get("notify.fail") == 1.0
    assert snap.get("notify.retry") is None
    telemetry.reset()


def test_retry_success_on_second_attempt(monkeypatch) -> None:
    telemetry.reset()
    rec = _flaky(monkeypatch, fail_times=1)
    cfg = Config(
        notify_webhook=GENERIC_URL, notify_retry_max=2, notify_retry_backoff_s=5.0
    )
    assert notify(cfg, "pending_review", "x") is True
    assert rec.calls == 2                 # 首败 + 重试成功
    assert rec.sleeps == [5.0]            # 基础退避起步
    snap = telemetry.snapshot()["counters"]
    assert snap.get("notify.sent") == 1.0
    assert snap.get("notify.retry") == 1.0
    assert snap.get("notify.fail") is None  # 终态恰一次:成功不算失败
    telemetry.reset()


def test_retry_exhausted_counts_fail_once(monkeypatch) -> None:
    telemetry.reset()
    rec = _flaky(monkeypatch, fail_times=99)  # 永远失败
    cfg = Config(
        notify_webhook=GENERIC_URL, notify_retry_max=2, notify_retry_backoff_s=1.0
    )
    assert notify(cfg, "pending_review", "x") is False
    assert rec.calls == 3                 # 1 次首发 + 2 次重试,恰为上界
    assert rec.sleeps == [1.0, 2.0]       # 指数翻倍
    snap = telemetry.snapshot()["counters"]
    assert snap.get("notify.fail") == 1.0 # 终态计数恰一次
    assert snap.get("notify.retry") == 2.0
    telemetry.reset()


def test_retry_backoff_doubles_each_time(monkeypatch) -> None:
    telemetry.reset()
    rec = _flaky(monkeypatch, fail_times=2)
    cfg = Config(
        notify_webhook=GENERIC_URL, notify_retry_max=3, notify_retry_backoff_s=2.0
    )
    assert notify(cfg, "ev", "y") is True
    assert rec.sleeps == [2.0, 4.0]       # 两次重试的退避序列
    telemetry.reset()


def test_retry_zero_backoff_immediate(monkeypatch) -> None:
    telemetry.reset()
    rec = _flaky(monkeypatch, fail_times=1)
    cfg = Config(
        notify_webhook=GENERIC_URL, notify_retry_max=1, notify_retry_backoff_s=0.0
    )
    assert notify(cfg, "ev", "y") is True
    assert rec.sleeps == [0.0]            # 0 退避 = 立即重试(本机测试形态)
    telemetry.reset()


def test_retry_payload_identical_each_attempt(monkeypatch) -> None:
    """重发的是同一载荷(只重发通知,绝不重发动作;字节一致)。"""
    telemetry.reset()
    import netsentinel.notify.hub as hub

    monkeypatch.setattr(hub, "_sleep", lambda s: None)
    bodies: list[bytes] = []

    def urlopen_grab(request, timeout=10):  # noqa: ANN001
        bodies.append(request.data)
        if len(bodies) <= 2:
            raise urllib.error.URLError("fail")
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen_grab)
    cfg = Config(
        notify_webhook=GENERIC_URL, notify_retry_max=2, notify_retry_backoff_s=0.0
    )
    assert notify(cfg, "ev", "z", k=1) is True
    assert len(bodies) == 3
    assert bodies[0] == bodies[1] == bodies[2]
    telemetry.reset()


def test_retry_negative_backoff_clamped_to_zero(monkeypatch) -> None:
    telemetry.reset()
    rec = _flaky(monkeypatch, fail_times=1)
    cfg = Config(
        notify_webhook=GENERIC_URL, notify_retry_max=1, notify_retry_backoff_s=-5.0
    )
    assert notify(cfg, "ev", "y") is True
    assert rec.sleeps == [0.0]            # 负退避钳到 0(绝不倒贴时间)
    telemetry.reset()
