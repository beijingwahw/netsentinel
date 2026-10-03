"""A28 text_intel 测试(离线纯函数)。"""
from __future__ import annotations

from netsentinel.intel.text_intel import extract_visible_text, text_features


NORMAL_HTML = """<html><head><title>某某新闻网-科技频道</title>
<meta name="description" content="每日科技新闻"></head>
<body><script>var a="色情";</script>
<p>今日发布新款芯片,性能提升百分之三十。</p>
<style>.x{color:red}</style>
<div>记者采访了多位业内人士。</div></body></html>"""

BAD_HTML = """<html><head><title>免费观看 激情电影</title></head><body>
<p>本站每日更新 无码专区 资源,深夜福利不断,手机看片 一步到位。</p>
<a>博彩 试玩 送彩金</a><p>裸聊 援交 一夜情 都有</p>
<div>AAAA++++////CCCCddddEEEEFFFFGGGGhhhhIIIIjjjjKKKKllllMMMMnnnn</div>
</body></html>"""


def test_extract_normal_text():
    title, body = extract_visible_text(NORMAL_HTML)
    assert title == "某某新闻网-科技频道"
    assert "芯片" in body and "记者" in body
    assert "color" not in body          # style 已剥
    assert "var a" not in body          # script 已剥(其中的词不计入)
    assert "色情" not in body


def test_extract_entity_decode():
    title, body = extract_visible_text(
        "<title>a &amp; b</title><p>x &lt;y&gt; z</p>")
    assert title == "a & b" and "<y>" in body


def test_normal_page_low_risk():
    r = text_features(NORMAL_HTML)
    assert r["risk"] <= 0.05
    assert r["features"]["porn_hits"] == 0
    assert r["explain"] == []


def test_empty_html_zero_risk():
    r = text_features("")
    assert r["risk"] == 0.0 and r["explain"] == []


def test_bad_page_risky():
    r = text_features(BAD_HTML)
    f = r["explain"]
    assert r["risk"] >= 0.5
    assert r["features"]["porn_hits"] >= 4
    assert r["features"]["lure_hits"] >= 3
    assert r["features"]["gambling_hits"] >= 1
    assert r["features"]["title_suspect"] == 1
    assert r["features"]["obfuscated_blobs"] >= 1
    assert any("标题" in x for x in f)
    assert any("诱导短语" in x for x in f)
    assert any("博彩" in x for x in f)


def test_risk_clamped_to_unit():
    html = ("<title>色情</title><body>" +
            "色情 淫秽 裸聊 援交 卖淫 无码 激情电影 黄色电影 ".lower() * 50 +
            "免费观看 无码专区 深夜福利 博彩 赌球 六合彩 ".lower() * 50 + "</body>")
    assert 0.0 <= text_features(html)["risk"] <= 1.0


def test_explain_matches_hits():
    r = text_features(BAD_HTML)
    n = sum(1 for k in ("porn_hits", "lure_hits", "gambling_hits",
                        "obfuscated_blobs", "title_suspect") if r["features"][k])
    # 密度达标会追加一行,故 explain 行数 >= 非零特征类别数
    assert len(r["explain"]) >= n >= 5


def test_single_category_incremental():
    one = text_features("<body><p>裸聊</p></body>")
    two = text_features("<body><p>裸聊 色情</p></body>")
    assert two["risk"] > one["risk"] > 0.0


# ---------------------------------------------------------------------------
# V5 升级锁定:词表计数语义冻结 / 遥测 / 错误计数 / 导出面
# ---------------------------------------------------------------------------


def test_v5_keyword_count_semantics_frozen():
    """跨词重叠与前缀词的计数语义锁定为逐词 str.count(防未来"优化"漂移)。

    - "色情色情":色情(2 次)+ 情色(1 次)→ uniq=2 / total=3;
    - "淫秽物品":淫秽 与 淫秽物品 同位各计 1 → uniq=2 / total=2。
    拼接词表的正则 findall 在这两类输入上会少计(V5 量化否决的方案)。
    """
    from netsentinel.intel import text_intel

    porn = text_intel.PORN_KEYWORDS
    assert text_intel._count_hits("色情色情", porn) == (2, 3)
    assert text_intel._count_hits("淫秽物品", porn) == (2, 2)


def test_v5_no_word_self_overlap_invariant():
    """词表任何词都不自重叠:逐词计数的"全出现=非重叠出现"等价性成立。"""
    from netsentinel.intel import text_intel

    for group in (text_intel.PORN_KEYWORDS, text_intel.LURE_PHRASES,
                  text_intel.GAMBLING_KEYWORDS):
        for w in group:
            for k in range(1, len(w)):
                assert w[:k] != w[-k:], f"词表出现自重叠词,语义需复核:{w}"


def test_v5_telemetry_timer_recorded():
    """text_features 记 telemetry.timer("text_intel.features")。"""
    from netsentinel import telemetry

    telemetry.reset()
    try:
        text_features(BAD_HTML)
        text_features("")
        snap = telemetry.snapshot()
        assert snap["timers"]["text_intel.features"]["count"] == 1  # 空 HTML 短路不计时
    finally:
        telemetry.reset()


def test_v5_error_counter_on_parser_failure(monkeypatch):
    """HTML 解析抛错时降级不中断,并记 telemetry.inc("text_intel.errors")。"""
    from netsentinel import telemetry
    from netsentinel.intel import text_intel

    def boom(self, data):
        raise ValueError("malformed")

    monkeypatch.setattr(text_intel._TextExtractor, "feed", boom)
    telemetry.reset()
    try:
        title, body = text_intel.extract_visible_text("<html><body><p>x</p></body></html>")
        assert (title, body) == ("", "")
        assert telemetry.snapshot()["counters"]["text_intel.errors"] == 1
    finally:
        telemetry.reset()


def test_v5_module_exports():
    """__all__ 覆盖词表数据与两个公共函数(V5 补全导出面)。"""
    from netsentinel.intel import text_intel

    assert set(text_intel.__all__) == {
        "PORN_KEYWORDS", "LURE_PHRASES", "GAMBLING_KEYWORDS",
        "extract_visible_text", "text_features",
    }
