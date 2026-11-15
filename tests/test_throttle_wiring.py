# -*- coding: utf-8 -*-
"""A74 接线测试:provider_throttle 挂进 vlm_client 真实外呼路径。

项目红线:新接线默认关闭——缺省(未 set_default_rpm / cfg 未挂附加属性
provider_rpm)时传输层行为与接线前逐字节一致,零限速调用;本文件同时锁定:

- 注入点 ``set_default_rpm`` 的开关语义(None/<1/不可数值化 → 禁用);
- cfg 附加属性 ``provider_rpm`` 的惰性建桶与同 rpm 复用(不每请求重建);
- 启用后**每次真实外呼尝试(含重试跳)**前都取令牌(记录型替身零等待);
- provider_throttle 新构造参数 ``default_rpm`` 的缺省不变 / 覆盖语义。
"""
from __future__ import annotations

import logging
import urllib.error

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.vision import provider_throttle, vlm_client
from netsentinel.vision.vlm_client import UniversalVLMClient

from tests.test_vlm_client import FakeResolved, FakeTransport, make_cfg, ok_openai

PROVIDER = "openai"


class RecordingThrottle:
    """记录型限速器替身:set_default_rpm 经惰性导入拿到它,acquire 零等待。"""

    instances: list["RecordingThrottle"] = []

    def __init__(self, rpm_hints=None, *, clock=None, default_rpm=None) -> None:
        self.default_rpm = default_rpm
        self.acquired: list[str] = []
        RecordingThrottle.instances.append(self)

    def acquire(self, provider: str) -> float:
        self.acquired.append(str(provider))
        return 0.0


@pytest.fixture(autouse=True)
def _restore_throttle_state(monkeypatch):
    """每条用例前清空记录、隔离模块级限速状态,用例后还原为缺省(禁用)。"""
    RecordingThrottle.instances = []
    vlm_client._THROTTLE = None
    vlm_client._CFG_THROTTLE = None
    vlm_client._CFG_THROTTLE_RPM = None
    yield
    RecordingThrottle.instances = []
    vlm_client._THROTTLE = None
    vlm_client._CFG_THROTTLE = None
    vlm_client._CFG_THROTTLE_RPM = None


# ---------------------------------------------------------------------------
# 缺省关闭(项目红线:主线行为逐字节不变)
# ---------------------------------------------------------------------------


def test_default_disabled_resolve_returns_none() -> None:
    """缺省:进程级未设置且 cfg 无附加属性 → 不限速。"""
    assert vlm_client._THROTTLE is None
    assert vlm_client._resolve_throttle(Config()) is None
    assert vlm_client._resolve_throttle(None) is None


def test_default_disabled_chat_json_never_acquires(monkeypatch) -> None:
    """缺省状态下完整走一次 chat_json:限速器零构造、零取令牌。"""
    monkeypatch.setattr(provider_throttle, "ProviderThrottle", RecordingThrottle)
    client, transport = _make_client([(200, ok_openai('{"a": 1}'))])
    assert client.chat_json([{"role": "user", "content": "ping"}]) == {"a": 1}
    assert len(transport.calls) == 1
    assert RecordingThrottle.instances == []


# ---------------------------------------------------------------------------
# set_default_rpm 注入点
# ---------------------------------------------------------------------------


def test_set_default_rpm_enable_and_disable() -> None:
    """启用:构建进程级限速器,未登记提供方按 default_rpm 建桶;None → 禁用。"""
    vlm_client.set_default_rpm(120)
    assert isinstance(vlm_client._THROTTLE, provider_throttle.ProviderThrottle)
    # state 只读观测不消费令牌、不等待
    assert vlm_client._THROTTLE.state(PROVIDER)["rpm"] == 120
    vlm_client.set_default_rpm(None)
    assert vlm_client._THROTTLE is None


def test_set_default_rpm_invalid_values_disable() -> None:
    """非法入参(<1 / 不可数值化)一律禁用,绝不抛出。"""
    for bad in (0, -3, "abc", object()):
        vlm_client.set_default_rpm(bad)  # type: ignore[arg-type]
        assert vlm_client._THROTTLE is None


def test_enabled_throttle_acquires_before_each_attempt(monkeypatch) -> None:
    """启用后:首发与 URLError 重试跳各取一次令牌(共 2 次,provider 正确)。"""
    monkeypatch.setattr(provider_throttle, "ProviderThrottle", RecordingThrottle)
    monkeypatch.setattr(vlm_client, "_sleep", lambda _s: None)  # 退避不真等
    vlm_client.set_default_rpm(60)
    client, transport = _make_client(
        [urllib.error.URLError("boom"), (200, ok_openai('{"a": 1}'))]
    )
    assert client.chat_json([{"role": "user", "content": "ping"}]) == {"a": 1}
    assert len(transport.calls) == 2  # 首发 + 1 次重试
    assert [inst.acquired for inst in RecordingThrottle.instances] == [
        [PROVIDER, PROVIDER]
    ]


# ---------------------------------------------------------------------------
# cfg 附加属性 provider_rpm(不新增 Config 字段,getattr 动态读取)
# ---------------------------------------------------------------------------


def test_cfg_provider_rpm_creates_shared_throttle_once() -> None:
    """cfg 附加属性开启:惰性建桶一次,同 rpm 后续复用同一实例。"""
    cfg = make_cfg()
    cfg.provider_rpm = 30
    first = vlm_client._resolve_throttle(cfg)
    assert isinstance(first, provider_throttle.ProviderThrottle)
    assert first.state(PROVIDER)["rpm"] == 30
    assert vlm_client._resolve_throttle(cfg) is first


def test_cfg_provider_rpm_invalid_returns_none() -> None:
    """cfg 附加属性非法(<1 / 不可数值化)→ 不限速,绝不抛出。"""
    for bad in (0, -1, "abc", object()):
        cfg = make_cfg()
        cfg.provider_rpm = bad
        assert vlm_client._resolve_throttle(cfg) is None


def test_process_level_throttle_takes_precedence_over_cfg() -> None:
    """进程级 set_default_rpm 优先于 cfg 附加属性。"""
    vlm_client.set_default_rpm(120)
    cfg = make_cfg()
    cfg.provider_rpm = 30
    assert vlm_client._resolve_throttle(cfg) is vlm_client._THROTTLE


# ---------------------------------------------------------------------------
# provider_throttle.default_rpm 构造参数(向后兼容)
# ---------------------------------------------------------------------------


def test_provider_throttle_default_rpm_none_keeps_default() -> None:
    """不传 default_rpm:未登记提供方仍按 DEFAULT_RPM(既有行为不变)。"""
    throttle = provider_throttle.ProviderThrottle()
    assert throttle.state("zzz")["rpm"] == provider_throttle.DEFAULT_RPM


def test_provider_throttle_default_rpm_overrides() -> None:
    """default_rpm=2:未登记提供方按 2 建桶;已登记提供方仍按 rpm_hints。"""
    throttle = provider_throttle.ProviderThrottle({"known": 30}, default_rpm=2)
    assert throttle.state("zzz")["rpm"] == 2
    assert throttle.state("known")["rpm"] == 30


def test_provider_throttle_default_rpm_invalid_warns_and_falls_back(caplog) -> None:
    """default_rpm 非法:告警后回落 DEFAULT_RPM。"""
    caplog.set_level(logging.WARNING, logger="netsentinel.vision.provider_throttle")
    throttle = provider_throttle.ProviderThrottle(default_rpm=0)
    assert throttle.state("zzz")["rpm"] == provider_throttle.DEFAULT_RPM
    assert any("默认 rpm 非法" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# V10.18 可观测补齐:vision.throttle.wait(纯遥测,零行为变化)
# ---------------------------------------------------------------------------


class WaitingThrottle:
    """记录型替身:acquire 恒返回固定等待秒数(模拟真实限速排队)。"""

    def __init__(self, waited: float) -> None:
        self.waited = waited
        self.acquired: list[str] = []

    def acquire(self, provider: str) -> float:
        self.acquired.append(str(provider))
        return self.waited


def test_v10_throttle_wait_telemetry_counts_real_waits() -> None:
    """真实等待(acquire 返回 >0)时计 vision.throttle.wait 并 observe 同名时长。"""
    throttle = WaitingThrottle(2.5)
    vlm_client._THROTTLE = throttle
    before = telemetry.snapshot()
    count_before = before["counters"].get("vision.throttle.wait", 0.0)
    timer_before = before["timers"].get("vision.throttle.wait", {"count": 0})

    vlm_client._throttle_acquire(PROVIDER, make_cfg())

    after = telemetry.snapshot()
    assert throttle.acquired == [PROVIDER]
    assert after["counters"]["vision.throttle.wait"] == count_before + 1.0
    timer = after["timers"]["vision.throttle.wait"]
    assert timer["count"] == timer_before["count"] + 1
    # 新样本 2.5s 并入后,均值恰为 (旧均值*旧数 + 2500ms) / 新数
    expected_avg = (
        timer_before.get("avg_ms", 0.0) * timer_before["count"] + 2500.0
    ) / timer["count"]
    assert timer["avg_ms"] == pytest.approx(expected_avg)


def test_v10_throttle_wait_telemetry_zero_wait_not_counted() -> None:
    """立即获得令牌(返回 0.0)不计 vision.throttle.wait(零等待零计数)。"""
    throttle = WaitingThrottle(0.0)
    vlm_client._THROTTLE = throttle
    before = telemetry.snapshot()["counters"].get("vision.throttle.wait", 0.0)

    vlm_client._throttle_acquire(PROVIDER, make_cfg())

    after = telemetry.snapshot()["counters"].get("vision.throttle.wait", 0.0)
    assert after == before


def test_v10_throttle_wait_telemetry_acquire_error_not_counted() -> None:
    """取令牌异常照旧放行(既不抛出也不计数,降级哲学不变)。"""
    class BrokenThrottle:
        def acquire(self, provider: str) -> float:
            raise RuntimeError("限速器内部异常")

    vlm_client._THROTTLE = BrokenThrottle()
    before = telemetry.snapshot()["counters"].get("vision.throttle.wait", 0.0)

    vlm_client._throttle_acquire(PROVIDER, make_cfg())  # 不抛

    after = telemetry.snapshot()["counters"].get("vision.throttle.wait", 0.0)
    assert after == before


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _make_client(script):
    """构造 openai 方言客户端 + 脚本回放传输层(零网络外呼)。"""
    transport = FakeTransport(script)
    client = UniversalVLMClient(FakeResolved(), make_cfg(), transport=transport)
    return client, transport
