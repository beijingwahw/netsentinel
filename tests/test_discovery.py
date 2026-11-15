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


def test_http_get_urlerror_converted_to_chinese_runtime_error(monkeypatch):
    """回归:_http_get 遇 URLError(DNS 失败/断网)转中文 RuntimeError,不再原样上抛。

    URLError 是 OSError 子类,若原样上抛会击穿 discover 整轮(打桩 urlopen,
    零真实外呼;yandex 与 searxng 共用该传输层)。
    """
    import urllib.error
    import urllib.request

    from netsentinel.discovery.yandex import _http_get

    def fake_urlopen(req, timeout):
        raise urllib.error.URLError(OSError("name or service not known"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="连接失败"):
        _http_get("https://yandex.invalid/search", 5.0)


def test_discover_survives_raw_urlerror_from_engine():
    """回归:engine.search 抛原始 URLError(OSError 子类)时,整轮 discover 优雅返回。

    即使第三方引擎未做 RuntimeError 转换,pipeline 层的 OSError 兜底也要把
    失败记入 errors 列表继续跑完,绝不击穿。
    """
    import urllib.error

    class NetDown(MockEngine):
        def search(self, query, limit):
            raise urllib.error.URLError(OSError("dns -3"))

    r = discover(
        ["k"], cfg(), engine=NetDown(), queries=["q1", "q2"],
        sleep=lambda s: None, cache=_off(),
    )
    assert r["urls"] == []
    assert len(r["errors"]) == 2 and "dns -3" in r["errors"][0][1]


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


# ---------------------------------------------------------------------------
# V10.18 _DiscoveryCache 时区防御:naive created_at 不退化、aware 判定等价
# ---------------------------------------------------------------------------

def test_cache_naive_created_at_fresh_row_hit(tmp_path):
    """防御:历史/外部写入的 naive(无 tz)created_at 新鲜行仍命中——
    naive 按本地时区 aware 化后与 aware 当前时刻比较,不误判过期。"""
    import datetime as dt
    cache = _DiscoveryCache(str(tmp_path / "c.db"), ttl_h=1)
    naive_now = dt.datetime.now().isoformat(timespec="seconds")  # 无 tz 后缀
    with cache._lock:
        cache._conn.execute(
            "INSERT INTO queries(engine, query, limit_n, urls, created_at)"
            " VALUES('mock','q',5,?,?)", ("http://n.example.com/", naive_now),
        )
        cache._conn.commit()
    assert cache.get("mock", "q", 5) == ["http://n.example.com/"]
    cache.close()


def test_cache_naive_created_at_expired_row_miss(tmp_path):
    """防御:naive 过期行(超 TTL)按未命中处理,触发刷新重查。"""
    import datetime as dt
    cache = _DiscoveryCache(str(tmp_path / "c.db"), ttl_h=1)
    old_naive = (dt.datetime.now() - dt.timedelta(hours=3)).isoformat(timespec="seconds")
    with cache._lock:
        cache._conn.execute(
            "INSERT INTO queries(engine, query, limit_n, urls, created_at)"
            " VALUES('mock','q',5,?,?)", ("http://old.example.com/", old_naive),
        )
        cache._conn.commit()
    assert cache.get("mock", "q", 5) is None
    cache.close()


def test_cache_naive_vs_aware_same_instant_same_verdict(tmp_path):
    """等价性:同一时刻的 naive(本地墙钟)与 aware(put 原生写法)两种
    打点,在同一 TTL 下判定结论一致(aware 化只补语义,不改判定)。"""
    import datetime as dt
    cache = _DiscoveryCache(str(tmp_path / "c.db"), ttl_h=1)
    with cache._lock:
        now_local = dt.datetime.now().astimezone()
        cache._conn.execute(
            "INSERT INTO queries(engine, query, limit_n, urls, created_at)"
            " VALUES('mock','aware',5,?,?)",
            ("http://a.example.com/", now_local.isoformat(timespec="seconds")),
        )
        cache._conn.execute(
            "INSERT INTO queries(engine, query, limit_n, urls, created_at)"
            " VALUES('mock','naive',5,?,?)",
            ("http://b.example.com/", now_local.replace(tzinfo=None).isoformat(timespec="seconds")),
        )
        cache._conn.commit()
    assert cache.get("mock", "aware", 5) == ["http://a.example.com/"]
    assert cache.get("mock", "naive", 5) == ["http://b.example.com/"]
    cache.close()


def test_cache_corrupt_or_null_created_at_treated_as_miss(tmp_path):
    """兜底:created_at 为 NULL 或畸形串时按未命中处理(不抛 TypeError)。"""
    cache = _DiscoveryCache(str(tmp_path / "c.db"), ttl_h=1)
    with cache._lock:
        cache._conn.execute(
            "INSERT INTO queries(engine, query, limit_n, urls, created_at)"
            " VALUES('mock','null',5,'http://x.example.com/',NULL)"
        )
        cache._conn.execute(
            "INSERT INTO queries(engine, query, limit_n, urls, created_at)"
            " VALUES('mock','bad',5,'http://y.example.com/','not-a-date')"
        )
        cache._conn.commit()
    assert cache.get("mock", "null", 5) is None
    assert cache.get("mock", "bad", 5) is None
    cache.close()


# ---------------- V10.20:模板文件装载 --template-file ----------------

def test_v10_load_templates_txt_comments_blank_and_dedup(tmp_path):
    """.txt 模板文件:每行一模板,# 注释与空行跳过,去重保序。"""
    from netsentinel.discovery.keywords import load_templates

    f = tmp_path / "templates.txt"
    f.write_text(
        "{kw} site:cn\n"
        "\n"
        "# 运营者注释行\n"
        "报告 {kw}\n"
        "{kw} site:cn\n",  # 重复模板:去重
        encoding="utf-8",
    )
    assert load_templates(f) == ["{kw} site:cn", "报告 {kw}"]


def test_v10_load_templates_list_and_missing_and_empty(tmp_path):
    """列表直取;文件不存在/全空 → 中文 ValueError(与 load_keywords 同纪律)。"""
    from netsentinel.discovery.keywords import load_templates

    assert load_templates(["a {kw}", "a {kw}", ""]) == ["a {kw}"]
    with pytest.raises(ValueError, match="模板文件不存在"):
        load_templates(tmp_path / "none.txt")
    bad = tmp_path / "empty.txt"
    bad.write_text("# 只有注释\n\n", encoding="utf-8")
    with pytest.raises(ValueError, match="模板为空"):
        load_templates(bad)


def test_v10_load_templates_feeds_expand_queries(tmp_path):
    """load_templates 结果可直接喂 expand_queries({kw} 占位符展开)。"""
    from netsentinel.discovery.keywords import load_templates

    f = tmp_path / "templates.txt"
    f.write_text("{kw} site:cn\n报告 {kw}\n", encoding="utf-8")
    queries = expand_queries(["涉诈"], load_templates(f))
    assert queries == ["涉诈 site:cn", "报告 涉诈"]


def test_v10_cli_template_file_offline_check(tmp_path, capsys):
    """CLI --template-file:离线检查同样装载校验模板(零外呼),打印模板数。"""
    k = tmp_path / "k.txt"
    k.write_text("关键词甲\n", encoding="utf-8")
    t = tmp_path / "templates.txt"
    t.write_text("{kw} site:cn\n", encoding="utf-8")
    rc = main(["--keywords-file", str(k), "--template-file", str(t), "--offline-check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "查询模板 1 个" in out
    assert "未发出任何查询" in out


def test_v10_cli_template_file_bad_path_returns_2(tmp_path, capsys):
    """CLI --template-file 指向不存在文件:中文报错,退出码 2(装载错误)。"""
    k = tmp_path / "k.txt"
    k.write_text("关键词甲\n", encoding="utf-8")
    rc = main(
        ["--keywords-file", str(k), "--template-file", str(tmp_path / "nope.txt"),
         "--offline-check"]
    )
    assert rc == 2
    assert "模板文件不存在" in capsys.readouterr().out


def test_v10_cli_template_file_merges_with_inline_and_expands(tmp_path, capsys):
    """CLI 端到端(mock 引擎):文件模板在前、内联模板叠加去重,查询数 = 词 × 模板。"""
    k = tmp_path / "k.txt"
    k.write_text("词甲\n词乙\n", encoding="utf-8")
    t = tmp_path / "templates.txt"
    t.write_text("{kw} site:cn\n", encoding="utf-8")
    out = tmp_path / "leads.txt"
    rc = main(
        ["--keywords-file", str(k), "--engine", "mock", "--out", str(out),
         "--template-file", str(t), "--template", "报告 {kw}",
         "--template", "{kw} site:cn"]  # 与文件重复:去重
    )
    assert rc == 0
    text = capsys.readouterr().out
    assert "查询模板 2 个" in text          # 文件 1 + 内联去重后 1
    assert "查询 4 次" in text              # 2 词 × 2 模板
    assert out.is_file()


def test_v10_cli_without_template_file_behavior_unchanged(tmp_path, capsys):
    """不传 --template-file:输出与既有行为一致(无模板行,原样查询)。"""
    k = tmp_path / "k.txt"
    k.write_text("词甲\n", encoding="utf-8")
    rc = main(["--keywords-file", str(k), "--engine", "mock",
               "--out", str(tmp_path / "leads.txt")])
    assert rc == 0
    assert "查询模板" not in capsys.readouterr().out
