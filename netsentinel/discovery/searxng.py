"""SearXNG 引擎(V6.5,备选:自托管元搜索,JSON API,无第三方 ToS 顾虑)。

要求运营者自建 SearXNG 实例并在其配置中开启 ``search.formats`` 包含 ``json``
(官方默认关闭,自托管自行开启)。端点:``GET {base}/search?q=<query>&format=json``。
仅当 ``cfg.discovery_online=True`` 时外呼。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable
from urllib.parse import quote

from netsentinel.contracts import Config
from netsentinel.discovery.engines import DiscoveryConfigError, SearchEngine, SearchHit
from netsentinel.discovery.yandex import _http_get

__all__ = ["SearxngEngine"]

logger = logging.getLogger(__name__)


class SearxngEngine(SearchEngine):
    name = "searxng"

    def __init__(
        self,
        cfg: Config,
        *,
        transport: Callable[[str, float], tuple[int, str]] | None = None,
        base_url: str | None = None,
    ) -> None:
        self.cfg = cfg
        self._transport = transport or _http_get
        self._base = (
            base_url
            if base_url is not None
            else getattr(cfg, "searxng_base_url", "")
        ).rstrip("/")

    def search(self, query: str, limit: int) -> list[SearchHit]:
        if not getattr(self.cfg, "discovery_online", False):
            raise DiscoveryConfigError(
                "SearXNG 发现层未开启:需在配置中显式设置 discovery_online: true"
            )
        if not self._base:
            raise DiscoveryConfigError(
                "SearXNG 端点未配置:请设置 searxng_base_url(自托管实例)"
            )
        url = f"{self._base}/search?q={quote(query)}&format=json"
        status, body = self._transport(url, 30.0)
        if status != 200:
            raise RuntimeError(
                f"SearXNG 返回 HTTP {status}(请确认实例已开启 json 输出格式)"
            )
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"SearXNG 返回内容无法解析为 JSON:{exc}") from exc
        hits: list[SearchHit] = []
        for item in data.get("results", []):
            u = (item.get("url") or "").strip()
            if not u:
                continue
            hits.append(
                SearchHit(
                    url=u,
                    title=(item.get("title") or "").strip(),
                    domain=(item.get("hostname") or "").lower(),
                    engine=self.name,
                )
            )
            if len(hits) >= max(1, limit):
                break
        return hits
