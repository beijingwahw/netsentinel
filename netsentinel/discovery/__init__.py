"""搜索引擎线索发现层(V6.5)—— 优先 Yandex,支持运营者自定义关键词。

安全红线(27/28):
27. **发现即线索**:搜索得到的目标只是待筛查线索,必须走完整
    扫描 → 判定 → 人工复核 → 声明 → 举报 流程;发现本身不构成任何处置依据。
28. **关键词零内置 + 默认离线**:代码不内置任何敏感搜索词,关键词必须由
    运营者显式提供;``discovery_online`` 默认 False(零外呼),查询带间隔限速
    与单轮线索预算。

引擎:``yandex``(官方 XML API,优先)/ ``searxng``(自托管)/ ``mock``(离线测试)。
"""
from netsentinel.discovery.engines import (
    DiscoveryConfigError,
    MockEngine,
    SearchEngine,
    SearchHit,
    get_engine,
    list_engines,
)
from netsentinel.discovery.keywords import expand_queries, load_keywords
from netsentinel.discovery.pipeline import discover, write_leads

__all__ = [
    "DiscoveryConfigError",
    "MockEngine",
    "SearchEngine",
    "SearchHit",
    "get_engine",
    "list_engines",
    "expand_queries",
    "load_keywords",
    "discover",
    "write_leads",
]
