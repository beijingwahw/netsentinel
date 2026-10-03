"""举报理由自动生成(V10)—— ≤39 字,自动填入描述首行。

**红线 39**:
- 理由只基于**已核实证据字段**(域名/抽查页数/达标图数/判定),禁止编造细节;
  GLM 路径仅做"措辞压缩",输入即事实清单,输出超限/为空一律回退确定性模板;
- 长度硬保证 :func:`fit_39`(≤39 字,含标点);
- 全链路离线可用:无密钥/预算尽/任何异常 → 模板,绝不因理由生成失败阻断举报。

用法::

    from netsentinel.submit.reason_gen import generate_reason
    reason = generate_reason(entry_like, cfg)      # "xx.com抽查2页,3图涉黄,已人工核实"
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from netsentinel.contracts import Config
from netsentinel import telemetry

__all__ = ["REASON_LIMIT", "count_chars", "fit_39", "generate_reason", "template_reason"]

logger = logging.getLogger(__name__)

#: 理由长度硬上限(字,含标点)
REASON_LIMIT = 39


def count_chars(text: str) -> int:
    """字数(中文 1 字计 1;英文按字符计)——与表单计数口径一致的朴素 len。"""
    return len(text or "")


def fit_39(text: str) -> str:
    """裁入 ≤39 字:优先在标点/空格边界收尾,硬裁后补"…";空串原样。"""
    s = (text or "").strip().replace("\n", " ")
    s = " ".join(s.split())  # 折叠空白
    if not s:
        return ""
    if count_chars(s) <= REASON_LIMIT:
        return s
    window = s[: REASON_LIMIT - 1]
    for stop in ("。", ",", ",", ";", ";", " ", "、"):
        idx = window.rfind(stop)
        if idx >= 12:  # 至少保留 12 字,避免裁成残句
            return window[:idx]
    return window


def _host_of(url: str, limit: int = 18) -> str:
    try:
        host = (urlsplit(url).hostname or str(url)).lower()
    except ValueError:
        host = str(url)
    host = host.strip(".")
    return host[:limit]


def _facts_of(entry_like: Any) -> dict[str, Any]:
    """从条目鸭子取事实字段(容错缺省);仅这些字段可进入理由(红线 39)。"""

    def g(name: str, default: Any) -> Any:
        for attr in (name,):
            v = getattr(entry_like, attr, None)
            if v not in (None, ""):
                return v
        v = entry_like.get(name) if isinstance(entry_like, dict) else None
        return v if v not in (None, "") else default

    return {
        "host": _host_of(str(g("site_url", ""))),
        "pages": g("pages", 0),
        "count": g("nsw_image_count", 0),
        "verdict": str(g("verdict", "nsfw")),
    }


def template_reason(entry_like: Any) -> str:
    """确定性模板理由(离线保底):域名+抽查页数+达标图数+人工核实。"""
    f = _facts_of(entry_like)
    pages = f["pages"]
    pages_n = len(pages) if isinstance(pages, (list, tuple)) else (int(pages) if str(pages).isdigit() else 0)
    count = int(f["count"]) if str(f["count"]).replace(".", "", 1).isdigit() else 0
    host = f["host"] or "该站点"
    if pages_n and count:
        raw = f"{host}抽查{pages_n}页,{count}张图涉色情内容,已人工核实"
    elif count:
        raw = f"{host}{count}张图涉色情内容,经人工核实"
    else:
        raw = f"{host}涉嫌传播色情内容,已经人工核实"
    return fit_39(raw)


def _glm_compress(facts: dict[str, Any], cfg: Config, client: Any) -> str | None:
    """GLM 措辞压缩:输入=事实清单,要求 ≤39 字中文理由;任何失败返回 None。

    红线 39:提示词明确"只用给定事实,不得添加任何新信息"。
    """
    if client is None:
        return None
    try:
        result = client.chat_json([{
            "role": "user",
            "content": (
                "把以下举报事实压缩成不超过39个汉字的中文举报理由一句话。"
                "只能使用给定事实,禁止添加任何新信息或推测。"
                '只输出 JSON:{"reason":"..."}。事实:' + ", ".join(
                    f"{k}={v}" for k, v in facts.items()
                )
            ),
        }])
        from netsentinel.vision.vlm_prompts import parse_json_response  # 惰性

        data = parse_json_response(result) if isinstance(result, str) else (
            result if isinstance(result, dict) else None
        )
        if isinstance(data, dict):
            reason = str(data.get("reason", "")).strip()
            if reason:
                return fit_39(reason)
    except Exception as exc:  # noqa: BLE001 - 增强路径绝不阻断
        logger.debug("GLM 理由压缩失败(回退模板):%s", exc)
    return None


def generate_reason(
    entry_like: Any,
    cfg: Config | None = None,
    *,
    client: Any = None,
    use_glm: bool = True,
) -> str:
    """生成 ≤39 字举报理由:GLM 压缩(可选)→ 失败回退确定性模板。"""
    cfg = cfg or Config()
    reason = ""
    if use_glm and client is not None:
        reason = _glm_compress(_facts_of(entry_like), cfg, client) or ""
        if reason:
            telemetry.inc("reason_gen.glm_ok")
    if not reason:
        reason = template_reason(entry_like)
        telemetry.inc("reason_gen.template")
    telemetry.inc("reason_gen.generated")
    final = fit_39(reason)
    if count_chars(final) > REASON_LIMIT:  # 双保险(理论不可达)
        final = final[:REASON_LIMIT]
    return final
