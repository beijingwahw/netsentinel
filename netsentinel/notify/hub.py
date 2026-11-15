# -*- coding: utf-8 -*-
"""运营者通知中枢(NetSentinel V2 · A32;V10.35 增补有界重试)。

发现待复核站点时向运营者推送提醒,支持企业微信(WeCom)/ 钉钉 / 飞书 / 通用 webhook。
与 CONTRACTS-V2 §3 A32 及团队任务书逐条一致:

- :data:`PLATFORMS`:URL 子串 → 平台标识(qyapi.weixin→wecom、dingtalk→dingtalk、
  feishu/open.feishu→feishu,其余→generic)。
- :func:`_build_payload`:按平台构造消息载荷。wecom/dingtalk 与飞书为文本消息
  (最终文本自动附 ``[NetSentinel][事件] 正文 (字段拼接) @本地时间`` 时间戳);
  generic 为裸 JSON ``{"event", "text", **fields}``。
- :func:`notify`:统一入口。``cfg.notify_webhook`` 为空直接返回 False(info 日志);
  否则 POST JSON(application/json,超时 10 秒,发送仅标准库 urllib/json;
  时间戳取值经 :mod:`netsentinel.timesource` 单源)。
  HTTPError / URLError / 超时 / 非 2xx 一律记中文 warning 后返回 False,绝不向上抛。

**缺省绝不重试**:提醒类消息宁可单次漏发,也不对目标群造成轰炸(V2 任务书
红线;``notify_retry_max`` 缺省 0 = 逐字节旧语义)。V10.35(P2-8)起运营者
可**显式**配置 ``cfg.notify_retry_max > 0`` 开启有界重试:指数退避
(``notify_retry_backoff_s`` 起步、每次翻倍)、**只重发通知,绝不重发动作**
(本模块本来就只发通知)——无人值守时"告警必达"与"不轰炸"的折中由操作者
自己定,默认值永远站在红线上。

可观测性(V5):发送成功累加 ``telemetry.inc("notify.sent")``,任何失败
(HTTP / 网络 / 非 2xx)累加 ``telemetry.inc("notify.fail")``,未配置
webhook 直接跳过时累加 ``telemetry.inc("notify.skip")``;V10.35 重试启用时
每次重发前累加 ``telemetry.inc("notify.retry")``——只存名称与数字。

用法示例::

    from netsentinel.contracts import Config
    from netsentinel.notify.hub import notify

    cfg = Config(notify_webhook="https://qyapi.weixin.qq.com/.../send?key=...")
    notify(cfg, "pending_review", "example.com verdict=nsfw agg=0.97")  # 单发
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from netsentinel import telemetry, timesource
from netsentinel.contracts import Config

__all__ = ["PLATFORMS", "GENERIC", "detect_platform", "notify"]

logger = logging.getLogger(__name__)

#: 重试间隔注入缝(测试 monkeypatch 用;生产恒 time.sleep)
_sleep = time.sleep

#: 未知平台(无法识别的 webhook URL)统一走裸 JSON 格式
GENERIC = "generic"

#: URL 子串 → 平台标识(按插入顺序匹配;检测时 URL 统一转小写)
PLATFORMS: dict[str, str] = {
    "qyapi.weixin": "wecom",       # 企业微信机器人 https://qyapi.weixin.qq.com/...
    "dingtalk": "dingtalk",        # 钉钉机器人 https://oapi.dingtalk.com/...
    "feishu": "feishu",            # 飞书机器人 https://open.feishu.cn/...
    "open.feishu": "feishu",       # 显式列出,便于查阅("feishu" 已覆盖)
}

#: 单次请求超时(秒)
REQUEST_TIMEOUT_S = 10

#: 消息抬头标记
_TAG = "NetSentinel"


def detect_platform(webhook_url: str) -> str:
    """按 URL 子串识别平台;识别不出返回 ``generic``。"""
    url = (webhook_url or "").lower()
    for marker, platform in PLATFORMS.items():
        if marker in url:
            return platform
    return GENERIC


def _local_timestamp() -> str:
    """本地时区可读时间戳(秒级;取值经 :mod:`netsentinel.timesource` 单源)。

    V10.24 收编:原 aware 惯用式 ``datetime.now(timezone.utc).astimezone()
    .strftime("%Y-%m-%d %H:%M:%S")`` 逐字符等价于 ``timesource.now_local()
    .strftime(...)``(strftime 只读墙钟字段,now_local 即该 aware 惯用式)。
    """
    return timesource.now_local().strftime("%Y-%m-%d %H:%M:%S")


def _final_text(event: str, text: str, fields: dict[str, Any]) -> str:
    """拼 IM 平台最终文本:``[NetSentinel][事件] 正文 (k=v, ...) @本地时间``。

    ``fields`` 为空时省略括号段,避免出现空 ``()``。单遍构建(V5):
    fields 只遍历一次,整句由**一次** f-string 拼装完成(旧实现做三次
    字符串级联、留下两个中间对象),字段顺序保持调用方插入序。
    """
    joined = (
        " (" + ", ".join(f"{k}={v}" for k, v in fields.items()) + ")"
        if fields
        else ""
    )
    return f"[{_TAG}][{event}] {text}{joined} @{_local_timestamp()}"


def _build_payload(platform: str, event: str, text: str, fields: dict[str, Any]) -> dict[str, Any]:
    """按平台构造 POST JSON 载荷。

    - wecom / dingtalk:``{"msgtype": "text", "text": {"content": 最终文本}}``
    - feishu:``{"msg_type": "text", "content": {"text": 最终文本}}``
    - generic:裸 JSON ``{"event": 事件, "text": 原文, **fields}``
    """
    if platform in ("wecom", "dingtalk"):
        return {"msgtype": "text", "text": {"content": _final_text(event, text, fields)}}
    if platform == "feishu":
        return {"msg_type": "text", "content": {"text": _final_text(event, text, fields)}}
    return {"event": event, "text": text, **fields}


def _send_once(request: Any, platform: str, event: str) -> tuple[bool, str]:
    """单次 POST;返回 ``(是否成功, 中文失败原因)``——只发不判重试。"""
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as resp:
            status = int(getattr(resp, "status", 0) or resp.getcode() or 0)
    except urllib.error.HTTPError as exc:  # 必须先于 URLError(HTTPError 是其子类)
        return False, f"webhook 返回 HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return False, f"网络异常或超时 {REQUEST_TIMEOUT_S:.0f}s"

    if 200 <= status < 300:
        logger.info("通知已发送(平台=%s,event=%s,HTTP %s)", platform, event, status)
        return True, ""
    return False, f"HTTP {status} 非成功状态"


def notify(cfg: Config, event: str, text: str, **fields: Any) -> bool:
    """推送一条运营者提醒;成功返回 True,任何失败返回 False(不抛异常)。

    - ``cfg.notify_webhook`` 为空:记 info 日志"未配置 notify_webhook,跳过通知"
      并返回 False,不发出任何请求(遥测计数 ``notify.skip``);
    - 请求:POST JSON(content-type application/json,超时 10 秒,标准库),
      载荷只构建一次、每次重发同一载荷(**只重发通知,绝不重发任何动作**);
    - HTTPError / URLError / 超时 / 非 2xx:记中文 warning(含状态码)后返回
      False(遥测计数 ``notify.fail``);
    - **重试口径(V10.35 P2-8)**:``cfg.notify_retry_max`` 缺省 0 = 仅尝试
      一次,绝不重试(V2 红线缺省不变);显式 >0 时失败后最多再发
      ``notify_retry_max`` 次,间隔 ``notify_retry_backoff_s`` 起步指数翻倍
      (5s/10s/20s…),每次重发前计 ``notify.retry``;**任意一次成功立即停**,
      终态计数(sent/fail)每次调用恰一次(缺省 0 时与旧版逐字节一致)。
    """
    webhook = (cfg.notify_webhook or "").strip()
    if not webhook:
        logger.info("未配置 notify_webhook,跳过通知(event=%s)", event)
        telemetry.inc("notify.skip")
        return False

    try:
        retry_max = max(0, int(getattr(cfg, "notify_retry_max", 0) or 0))
    except (TypeError, ValueError):
        retry_max = 0
    try:
        backoff_base = float(getattr(cfg, "notify_retry_backoff_s", 5.0) or 0.0)
    except (TypeError, ValueError):
        backoff_base = 5.0
    if backoff_base < 0:
        backoff_base = 0.0

    platform = detect_platform(webhook)
    payload = _build_payload(platform, event, text, fields)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    delay = backoff_base
    ok, reason = False, ""
    for attempt in range(retry_max + 1):
        request = urllib.request.Request(  # 每次新建(Request 对象不可重用)
            webhook,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        ok, reason = _send_once(request, platform, event)
        if ok:
            telemetry.inc("notify.sent")
            return True
        if attempt < retry_max:
            telemetry.inc("notify.retry")
            logger.info(
                "通知第 %d/%d 次发送失败(%s;平台=%s,event=%s):%.1fs 后重试",
                attempt + 1, retry_max + 1, reason, platform, event, delay,
            )
            _sleep(delay)
            delay *= 2

    logger.warning(
        "通知发送失败:%s(平台=%s,event=%s,共尝试 %d 次)",
        reason, platform, event, retry_max + 1,
    )
    telemetry.inc("notify.fail")
    return False
