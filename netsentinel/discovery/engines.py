"""搜索引擎抽象与注册表(V6.5)。

发现层只做一件事:把运营者的自定义关键词变成"候选 URL 线索清单"。
本模块定义引擎协议;Yandex/SearXNG/Mock 实现见同包各模块。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

from netsentinel.contracts import Config

__all__ = [
    "DiscoveryConfigError",
    "SearchHit",
    "SearchEngine",
    "MockEngine",
    "register_engine",
    "get_engine",
    "list_engines",
]

logger = logging.getLogger(__name__)


class DiscoveryConfigError(RuntimeError):
    """发现层配置不满足(缺密钥/未开启 discovery_online/未知引擎),中文消息。"""


@dataclass(frozen=True)
class SearchHit:
    """一条搜索结果(线索)。"""

    url: str
    title: str = ""
    domain: str = ""
    engine: str = ""

    @classmethod
    def from_url(cls, url: str, *, title: str = "", engine: str = "") -> "SearchHit":
        host = (urlsplit(url).hostname or "").lower()
        return cls(url=url, title=title, domain=host, engine=engine)


class SearchEngine(ABC):
    """搜索引擎协议:``search(query, limit) -> list[SearchHit]``。

    实现约束:
    - 只读公开检索结果,不模拟登录、不绕过验证码/反爬;
    - 失败抛中文异常(DiscoveryConfigError / RuntimeError),不静默吞;
    - 每次调用代表一次真实外呼(引擎侧自查配置与开关)。
    """

    name: str = "abstract"

    @abstractmethod
    def search(self, query: str, limit: int) -> list[SearchHit]:
        """执行一次检索,返回至多 limit 条线索。"""


_REGISTRY: dict[str, type[SearchEngine]] = {}


def register_engine(name: str, cls: type[SearchEngine]) -> None:
    _REGISTRY[name.lower()] = cls


def list_engines() -> list[str]:
    """可用引擎名(含惰性内置三家)。"""
    builtin = {"yandex", "searxng", "mock"}
    return sorted(set(_REGISTRY) | builtin)


def get_engine(name: str, cfg: Config, **kwargs: Any) -> SearchEngine:
    """按名构造引擎;yandex/searxng/mock 缺省惰性导入,未知名中文报错。"""
    key = (name or "").strip().lower()
    if key == "mock":
        from netsentinel.discovery.engines import MockEngine as _M

        return _M(**kwargs)
    if key == "yandex":
        from netsentinel.discovery.yandex import YandexXmlEngine

        return YandexXmlEngine(cfg, **kwargs)
    if key == "searxng":
        from netsentinel.discovery.searxng import SearxngEngine

        return SearxngEngine(cfg, **kwargs)
    cls = _REGISTRY.get(key)
    if cls is not None:
        return cls(**kwargs)
    raise DiscoveryConfigError(
        f"未知搜索引擎 '{name}';可用引擎:{', '.join(list_engines())}"
    )


@dataclass
class MockEngine(SearchEngine):
    """离线测试引擎:可注入结果提供器,或按查询哈希生成确定性结果。"""

    name: str = "mock"
    provider: Callable[[str], list[str]] | None = None
    calls: list[str] = field(default_factory=list)

    def search(self, query: str, limit: int) -> list[SearchHit]:
        self.calls.append(query)
        if self.provider is not None:
            urls = self.provider(query)
        else:
            digest = hex(abs(hash(query)) % 1000)[2:]
            urls = [f"http://site{digest}-{i}.example.com/" for i in range(3)]
        hits = [SearchHit.from_url(u, engine=self.name) for u in urls]
        return hits[: max(0, limit)]
