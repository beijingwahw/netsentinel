"""发现层测试(V6.5,全离线:mock 引擎/注入 transport,零外呼)。"""
from __future__ import annotations

import sqlite3

import pytest

from netsentinel.contracts import Config
from netsentinel.discovery import (
    DiscoveryConfigError,
    MockEngine,
    expand_queries,
    load_keywords,
)
from netsentinel.discovery.engines import get_engine, list_engines
from netsentinel.discovery.pipeline import (
    DEFAULT_EXCLUDE_DOMAINS,
    LEADS_HEADER,
    _DiscoveryCache,
    discover,
    main,
    write_leads,
)

YANDEX_OK_XML = """<?xml version="1.0" encoding="utf-8"?>
<yandexsearch version="1.0">
 <response>
  <results>
   <grouping>
    <group>
     <doc>
      <url>http://alpha.example.com/page1</url>
      <title>Alpha</title>
      <domain>alpha.example.com</domain>
     </doc>
     <doc>
      <url>http://beta.example.org/</url>
      <title>Beta</title>
      <domain>beta.example.org</domain>
     </doc>
   </group>
   </grouping>
  </results>
 </response>
</yandexsearch>"""

YANDEX_ERR_XML = '<?xml version="1.0"?><yandexsearch><response><error code="32">key error</error></response></yandexsearch>'


def cfg(**kw):
    c = Config()
    c.discovery_engine = "mock"
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# ---------------- keywords ----------------

def test_keywords_from_list_and_dedup():
    assert load_keywords(["a", " b ", "a", "#注释", ""]) == ["a", "b"]


def test_keywords_txt_file(tmp_path):
    f = tmp_path / "k.txt"
    f.write_text("# 注释\n词一\n 词二 \n词一\n", encoding="utf-8")
    assert load_keywords(f) == ["词一", "词二"]


def test_keywords_yaml_list_and_mapping(tmp_path):
    f = tmp_path / "k.yaml"
    f.write_text("[甲, 乙]\n", encoding="utf-8")
    assert load_keywords(f) == ["甲", "乙"]
    f2 = tmp_path / "k2.yaml"
    f2.write_text("keywords: [x, y]\n", encoding="utf-8")
    assert load_keywords(f2) == ["x", "y"]


def test_keywords_empty_and_missing(tmp_path):
    with pytest.raises(ValueError, match="不内置"):
        load_keywords(["", "  "])
    with pytest.raises(ValueError):
        load_keywords(tmp_path / "none.txt")


def test_expand_queries_default_and_templates():
    assert expand_queries(["a", "b"]) == ["a", "b"]
    out = expand_queries(["a"], ["{kw} site:cn", "免费 {kw}"])
    assert out == ["a site:cn", "免费 a"]


# ---------------- engines ----------------

def test_list_and_get_engines():
    assert {"yandex", "searxng", "mock"} <= set(list_engines())
    assert isinstance(get_engine("mock", cfg()), MockEngine)
    with pytest.raises(DiscoveryConfigError):
        get_engine("nope", cfg())


def test_yandex_offline_and_missing_creds():
    from netsentinel.discovery.yandex import YandexXmlEngine

    e = YandexXmlEngine(cfg())  # discovery_online=False
    with pytest.raises(DiscoveryConfigError, match="discovery_online"):
        e.search("q", 5)
    c = cfg(discovery_online=True)  # 无凭据
    with pytest.raises(DiscoveryConfigError, match="凭据缺失"):
        YandexXmlEngine(c).search("q", 5)


def test_yandex_parse_and_request_shape(monkeypatch):
    from netsentinel.discovery.yandex import YandexXmlEngine

    captured = {}

    def fake_transport(url, timeout):
        captured["url"] = url
        return 200, YANDEX_OK_XML

    c = cfg(discovery_online=True, yandex_xml_user="u1", yandex_xml_key="k1")
    e = YandexXmlEngine(c, transport=fake_transport)
    hits = e.search("词A", 10)
    assert [h.url for h in hits] == [
        "http://alpha.example.com/page1",
        "http://beta.example.org/",
    ]
    assert hits[0].engine == "yandex" and hits[0].domain == "alpha.example.com"
    assert "user=u1" in captured["url"] and "key=k1" in captured["url"]
    assert "%E8%AF%8DA" in captured["url"] or "query=" in captured["url"]


def test_yandex_api_error_and_http_error():
    from netsentinel.discovery.yandex import YandexXmlEngine

    c = cfg(discovery_online=True, yandex_xml_user="u", yandex_xml_key="k")
    with pytest.raises(RuntimeError, match="key error"):
        YandexXmlEngine(c, transport=lambda u, t: (200, YANDEX_ERR_XML)).search("q", 5)
    with pytest.raises(RuntimeError, match="HTTP 403"):
        YandexXmlEngine(c, transport=lambda u, t: (403, "forbidden")).search("q", 5)


def test_searxng_gates_and_parse():
    from netsentinel.discovery.searxng import SearxngEngine

    with pytest.raises(DiscoveryConfigError, match="discovery_online"):
        SearxngEngine(cfg()).search("q", 5)
    c = cfg(discovery_online=True)
    with pytest.raises(DiscoveryConfigError, match="searxng_base_url"):
        SearxngEngine(c, base_url="").search("q", 5)
    ok = '{"results": [{"url": "http://s.example.net/", "title": "S", "hostname": "s.example.net"}, {"url": ""}]}'
    e = SearxngEngine(c, base_url="http://127.0.0.1:8888", transport=lambda u, t: (200, ok))
    hits = e.search("q", 10)
    assert len(hits) == 1 and hits[0].engine == "searxng"


# ---------------- pipeline ----------------

def test_discover_mock_basic_dedup_and_exclude():
    c = cfg(discovery_query_delay_s=1.0)
    eng = MockEngine(provider=lambda q: [
        "http://a.example.com/1",
        "http://www.a.example.com/2",   # 同站镜像 → canonical 去重
        "http://yandex.com/search",     # 引擎自身域 → 排除
        "http://b.example.org/x",
    ])
    sleeps = []
    r = discover(["k1"], c, engine=eng, sleep=sleeps.append, cache=None if False else _off())
    assert r["urls"] == ["http://a.example.com/1", "http://b.example.org/x"]
    assert sleeps == []  # 单查询无间隔
    assert r["errors"] == []


class _off:
    def __init__(self):
        pass

    def get(self, *a):
        return None

    def put(self, *a):
        pass

    def close(self):
        pass


def _two_sites(query: str) -> list[str]:
    idx = {"a": 1, "b": 2, "c": 3}[query]
    return [f"http://x{idx}.example{idx}.com/", f"http://y{idx}.sample{idx}.org/"]


def test_discover_delay_between_queries():
    c = cfg(discovery_query_delay_s=2.0, discovery_max_total=10)
    eng = MockEngine(provider=_two_sites)
    sleeps: list[float] = []
    r = discover(["a", "b", "c"], c, engine=eng, sleep=sleeps.append, cache=_off())
    assert len(r["urls"]) == 6  # 3 查询 × 2 条,互不同站
    assert sleeps == [2.0, 2.0]  # 查询间隔注入生效(首查不睡)


def test_discover_total_budget_cap():
    c = cfg(discovery_query_delay_s=1.0, discovery_max_total=2)
    eng = MockEngine(provider=_two_sites)
    sleeps: list[float] = []
    r = discover(["a", "b", "c"], c, engine=eng, sleep=sleeps.append, cache=_off())
    assert len(r["urls"]) == 2  # 总预算生效:达限后剩余查询跳过
    assert sleeps == []  # 第二查询根本未发出


def test_discover_error_isolation():
    class Flaky(MockEngine):
        def search(self, query, limit):
            if "bad" in query:
                raise RuntimeError("引擎临时故障")
            return super().search(query, limit)

    c = cfg()
    r = discover(["good", "bad2"], c, engine=Flaky(), queries=["good one", "bad two"], sleep=lambda s: None, cache=_off())
    assert r["urls"] and r["errors"] and "引擎临时故障" in r["errors"][0][1]


def test_discover_cache_hit_zero_engine_calls(tmp_path):
    c = cfg(discovery_query_delay_s=1.0)
    cache = _DiscoveryCache(str(tmp_path / "c.db"), ttl_h=24)
    eng = MockEngine(provider=lambda q: ["http://cached.example.com/"])
    r1 = discover(["k"], c, engine=eng, sleep=lambda s: None, cache=cache)
    cache2 = _DiscoveryCache(str(tmp_path / "c.db"), ttl_h=24)
    eng2 = MockEngine()
    r2 = discover(["k"], c, engine=eng2, sleep=lambda s: None, cache=cache2)
    assert r1["urls"] == r2["urls"]
    assert r2["cache_hits"] == 1 and eng2.calls == []
    cache.close(); cache2.close()


def test_cache_ttl_expired(tmp_path):
    import datetime as dt
    cache = _DiscoveryCache(str(tmp_path / "c.db"), ttl_h=1)
    cache.put("mock", "q", 5, ["http://t.example.com/"])
    with cache._lock:
        old = (dt.datetime.now(dt.timezone.utc).astimezone() - dt.timedelta(hours=3)).isoformat()
        cache._conn.execute("UPDATE queries SET created_at=?", (old,))
        cache._conn.commit()
    assert cache.get("mock", "q", 5) is None
    cache.close()


def test_write_leads_roundtrip_and_bulk_compat(tmp_path):
    from netsentinel.ops.bulk_intake import load_bulk

    p = tmp_path / "leads.txt"
    n = write_leads(["http://a.example.com/", "http://b.example.org/"], str(p), engine_name="mock")
    assert n == 2 and LEADS_HEADER.splitlines()[0] in p.read_text(encoding="utf-8")
    urls, rejected = load_bulk(str(p))
    assert urls == ["http://a.example.com/", "http://b.example.org/"] and not rejected


def test_cli_offline_check_and_keywords(tmp_path, capsys):
    f = tmp_path / "k.txt"
    f.write_text("词甲\n", encoding="utf-8")
    rc = main(["--keywords-file", str(f), "--offline-check"])
    out = capsys.readouterr().out
    assert rc == 0 and "未发出任何查询" in out and "词甲" not in out


def test_cli_requires_keywords(capsys):
    assert main([]) == 2
    assert "不内置" in capsys.readouterr().out


def test_cli_mock_end_to_end(tmp_path, capsys):
    f = tmp_path / "k.txt"
    f.write_text("词甲\n词乙\n", encoding="utf-8")
    out = tmp_path / "leads.txt"
    rc = main(["--keywords-file", str(f), "--engine", "mock", "--out", str(out)])
    assert rc == 0
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "待筛查线索" in text and "batchflow" in capsys.readouterr().out


def test_default_excludes_static():
    assert "yandex.com" in DEFAULT_EXCLUDE_DOMAINS


def test_sqlite_persist_roundtrip(tmp_path):
    db = str(tmp_path / "c.db")
    c1 = _DiscoveryCache(db, ttl_h=24)
    c1.put("yandex", "q1", 10, ["http://z.example.com/"])
    c1.close()
    c2 = _DiscoveryCache(db, ttl_h=24)
    assert c2.get("yandex", "q1", 10) == ["http://z.example.com/"]
    c2.close()
