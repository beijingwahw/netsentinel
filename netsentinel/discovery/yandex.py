"""Yandex 官方 XML 检索引擎(V6.5,优先引擎)。

使用 Yandex Search XML API(官方付费接口,凭 user/key 鉴权):
``https://yandex.com/search/xml?user=<user>&key=<key>&query=<query>&lr=...``
返回 XML(results/grouping/group/doc:url/title/domain)。

安全与合规:
- 密钥来源:cfg.yandex_xml_user/yandex_xml_key → 环境变量
  NETSENTINEL_YANDEX_USER / NETSENTINEL_YANDEX_KEY;缺失即拒发(不盲试);
- 仅在 ``cfg.discovery_online=True`` 时外呼(红线 28);
- 不解析/不绕过任何验证码与反爬页面;接口报错按官方错误信息转述;
- 密钥绝不写入日志/异常(telemetry 只存名称与数字)。

端点与参数以官方文档为准,可用 cfg 覆盖 ``discovery`` 相关路径;
如需对接新版 Yandex Cloud Search API,可经子类覆盖 ``_request_params``。
"""
from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
from typing import Any, Callable
from urllib.parse import quote

from netsentinel.contracts import Config
from netsentinel.discovery.engines import DiscoveryConfigError, SearchEngine, SearchHit

__all__ = ["YandexXmlEngine", "YANDEX_XML_ENDPOINT"]

logger = logging.getLogger(__name__)

#: 官方 XML 端点(可经子类/测试替换)
YANDEX_XML_ENDPOINT = "https://yandex.com/search/xml"


def _http_get(url: str, timeout: float) -> tuple[int, str]:
    """GET 并返回 (status, body);HTTPError 转 (code, body) 不抛,URLError 上抛。"""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "NetSentinel/0.6"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200) or 200), resp.read().decode(
                "utf-8", errors="replace"
            )
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", errors="replace")


class YandexXmlEngine(SearchEngine):
    """Yandex XML API 引擎(官方接口;离线/无密钥时构造即拒)。"""

    name = "yandex"

    def __init__(
        self,
        cfg: Config,
        *,
        transport: Callable[[str, float], tuple[int, str]] | None = None,
        endpoint: str = YANDEX_XML_ENDPOINT,
    ) -> None:
        self.cfg = cfg
        self._transport = transport or _http_get
        self._endpoint = endpoint
        self._user = (getattr(cfg, "yandex_xml_user", "") or "").strip() or os.environ.get(
            "NETSENTINEL_YANDEX_USER", ""
        )
        self._key = (getattr(cfg, "yandex_xml_key", "") or "").strip() or os.environ.get(
            "NETSENTINEL_YANDEX_KEY", ""
        )

    # -- 协议实现 ---------------------------------------------------------
    def search(self, query: str, limit: int) -> list[SearchHit]:
        if not getattr(self.cfg, "discovery_online", False):
            raise DiscoveryConfigError(
                "Yandex 发现层未开启:需在配置中显式设置 discovery_online: true"
                "(搜索外呼须运营者显式授权,默认零外呼)"
            )
        if not self._user or not self._key:
            missing = [n for n, v in (("user", self._user), ("key", self._key)) if not v]
            raise DiscoveryConfigError(
                f"Yandex XML API 凭据缺失:{' 与 '.join(missing)};"
                "请在配置 yandex_xml_user/yandex_xml_key 或环境变量 "
                "NETSENTINEL_YANDEX_USER / NETSENTINEL_YANDEX_KEY 提供(以官方控制台为准)"
            )
        url = self._build_url(query, limit)
        status, body = self._transport(url, 30.0)
        if status != 200:
            raise RuntimeError(
                f"Yandex XML API 返回 HTTP {status}:"
                f"{self._error_text(body) or '详见官方错误码文档'}"
            )
        return self._parse(body)

    # -- 内部 -------------------------------------------------------------
    def _build_url(self, query: str, limit: int) -> str:
        limit = max(1, min(int(limit), 100))
        return (
            f"{self._endpoint}?user={quote(self._user)}&key={quote(self._key)}"
            f"&query={quote(query)}&lr=&l10n=zh"
            f"&groupby=attr%3D%22%22.mode%3Dflat.groups%3D1.doc%3D{limit}"
        )

    def _parse(self, xml_text: str) -> list[SearchHit]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise RuntimeError(f"Yandex 返回内容无法解析为 XML:{exc}") from exc
        error = root.find(".//error")
        if error is not None:
            raise RuntimeError(f"Yandex XML API 报错:{(error.text or '').strip()}")
        hits: list[SearchHit] = []
        for doc in root.iter("doc"):
            url_el = doc.find("url")
            if url_el is None or not (url_el.text or "").strip():
                continue
            title_el = doc.find("title")
            domain_el = doc.find("domain")
            hits.append(
                SearchHit(
                    url=(url_el.text or "").strip(),
                    title=(title_el.text or "").strip() if title_el is not None else "",
                    domain=(domain_el.text or "").strip().lower()
                    if domain_el is not None
                    else "",
                    engine=self.name,
                )
            )
        return hits

    @staticmethod
    def _error_text(body: str) -> str:
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return body[:120]
        err = root.find(".//error")
        return (err.text or "").strip() if err is not None else body[:120]
