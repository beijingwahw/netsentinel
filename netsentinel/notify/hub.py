# -*- coding: utf-8 -*-
"""运营者通知中枢(NetSentinel V2 · A32)。

发现待复核站点时向运营者推送提醒,支持企业微信(WeCom)/ 钉钉 / 飞书 / 通用 webhook。
与 CONTRACTS-V2 §3 A32 及团队任务书逐条一致:

- :data:`PLATFORMS`:URL 子串 → 平台标识(qyapi.weixin→wecom、dingtalk→dingtalk、
  feishu/open.feishu→feishu,其余→generic)。
- :func:`_build_payload`:按平台构造消息载荷。wecom/dingtalk 与飞书为文本消息
  (最终文本自动附 ``[NetSentinel][事件] 正文 (字段拼接) @本地时间`` 时间戳);
  generic 为裸 JSON ``{"event", "text", **fields}``。
- :func:`notify`:统一入口。``cfg.notify_webhook`` 为空直接返回 False(info 日志);
  否则 POST JSON(application/json,超时 10 秒,仅标准库 urllib/json/datetime)。
  HTTPError / URLError / 超时 / 非 2xx 一律记中文 warning 后返回 False,绝不向上抛。

**绝不重试**:提醒类消息宁可单次漏发,也不对目标群造成轰炸(V2 任务书红线)。

可观测性(V5):发送成功累加 ``telemetry.inc("notify.sent")``,任何失败
(HTTP / 网络 / 非 2xx)累加 ``telemetry.inc("notify.fail")``,未配置
webhook 直接跳过时累加 ``telemetry.inc("notify.skip")``——只存名称与数字。

用法示例::

    from netsentinel.contracts import Config
    from netsentinel.notify.hub import notify

    cfg = Config(notify_webhook="https://qyapi.weixin.qq.com/.../send?key=...")
    notify(cfg, "pending_review", "example.com verdict=nsfw agg=0.97")  # 单发
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config

__all__ = ["PLATFORMS", "GENERIC", "detect_platform", "notify"]

logger = logging.getLogger(__name__)

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
    """本地时区可读时间戳(秒级)。"""
    return _dt.datetime.now(_dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


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


def notify(cfg: Config, event: str, text: str, **fields: Any) -> bool:
    """推送一条运营者提醒;成功返回 True,任何失败返回 False(不抛异常)。

    - ``cfg.notify_webhook`` 为空:记 info 日志"未配置 notify_webhook,跳过通知"
      并返回 False,不发出任何请求(遥测计数 ``notify.skip``);
    - 请求:POST JSON(content-type application/json,超时 10 秒,标准库),
      载荷只构建一次、只发送一次;
    - HTTPError / URLError / 超时 / 非 2xx:记中文 warning(含状态码)后返回
      False(遥测计数 ``notify.fail``);
    - 仅尝试一次,绝不重试(发送成功遥测计数 ``notify.sent``)。
    """
    webhook = (cfg.notify_webhook or "").strip()
    if not webhook:
        logger.info("未配置 notify_webhook,跳过通知(event=%s)", event)
        telemetry.inc("notify.skip")
        return False

    platform = detect_platform(webhook)
    payload = _build_payload(platform, event, text, fields)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as resp:
            status = int(getattr(resp, "status", 0) or resp.getcode() or 0)
    except urllib.error.HTTPError as exc:  # 必须先于 URLError(HTTPError 是其子类)
        logger.warning(
            "通知发送失败:webhook 返回 HTTP %s(平台=%s,event=%s):%s",
            exc.code, platform, event, exc,
        )
        telemetry.inc("notify.fail")
        return False
    except (urllib.error.URLError, TimeoutError) as exc:
        logger.warning(
            "通知发送失败:网络异常或超时 %.0fs(平台=%s,event=%s):%s",
            REQUEST_TIMEOUT_S, platform, event, exc,
        )
        telemetry.inc("notify.fail")
        return False

    if 200 <= status < 300:
        logger.info("通知已发送(平台=%s,event=%s,HTTP %s)", platform, event, status)
        telemetry.inc("notify.sent")
        return True
    logger.warning(
        "通知发送失败:HTTP %s 非成功状态(平台=%s,event=%s)",
        status, platform, event,
    )
    telemetry.inc("notify.fail")
    return False
