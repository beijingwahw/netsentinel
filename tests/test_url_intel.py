"""A27 netsentinel.intel.url_intel URL 静态情报测试。

纯离线字符串分析,零网络、零文件 IO。覆盖:正常 https 大站零风险、
每个特征逐一命中、多特征加权求和与 clamp、解析失败降级、端口白名单、
risk 输出区间、explain 与 features 命中数一一对应、路径/熵段边界。
"""
from __future__ import annotations

import pytest

from netsentinel.intel import url_intel
from netsentinel.intel.url_intel import SUSPICIOUS_TLDS, url_features

CLEAN = "https://www.example.com/page"

# 覆盖面不同的样本(供区间/对应关系批量断言)
SAMPLES = [
    CLEAN,
    "https://www.google.com/search?q=netsentinel",
    "http://xn--80ak6aa92e.com/login",
    "https://例え.jp/おわり",
    "https://shop.example.xyz/item?id=9",
    "https://a.b.c.d.example.com/",
    "https://a-b-c-d-e-f-g-h-i-j-k-l-m-n-o-p.example.com/",
    "https://example.com:8080/admin",
    "https://[2001:db8::1]/index.html",
    "http://192.168.1.1/",
    "http://www-paypal.example.com/login",
    "https://example.com/" + "a" * 130 + "?t=" + "0" * 40,
]


# ---------------------------------------------------------------------------
# 常量与正常 URL
# ---------------------------------------------------------------------------


def test_suspicious_tlds_constant_matches_contract() -> None:
    """可疑顶级域集合与契约给定完全一致(15 个,防误改)。"""
    assert SUSPICIOUS_TLDS == {
        "xyz", "top", "info", "su", "ru", "cc", "tk", "ml", "ga", "cf", "gq",
        "biz", "click", "link", "loan",
    }


def test_normal_https_site_is_clean() -> None:
    """正常 https 大站:全部特征未命中,risk=0,explain 为空。"""
    r = url_features(CLEAN)
    assert set(r) == {"features", "risk", "explain"}
    assert r["features"] == {key: False for key in url_intel.FEATURE_WEIGHTS}
    assert r["risk"] == pytest.approx(0.0)
    assert r["explain"] == []


# ---------------------------------------------------------------------------
# 逐特征命中
# ---------------------------------------------------------------------------


def test_punycode_host_hit() -> None:
    """xn-- 开头标签 → punycode_host 命中(权重 0.20)。"""
    r = url_features("https://xn--80ak6aa92e.com/login")
    assert r["features"]["punycode_host"] is True
    assert r["risk"] == pytest.approx(0.20)
    assert r["explain"] == ["域名含 punycode 混写(xn--),常见于仿冒/规避封锁"]


def test_idn_non_ascii_hit() -> None:
    """host 含非 ASCII(IDN 原文)→ idn_non_ascii 命中(权重 0.10)。"""
    r = url_features("https://例え.jp/おわり")
    assert r["features"]["idn_non_ascii"] is True
    assert r["features"]["punycode_host"] is False  # 原文与 punycode 编码互斥
    assert r["risk"] == pytest.approx(0.10)


def test_suspicious_tld_hit() -> None:
    """.xyz 可疑顶级域 → suspicious_tld 命中(权重 0.12)。"""
    r = url_features("https://shop.example.xyz/item?id=9")
    assert r["features"]["suspicious_tld"] is True
    assert r["risk"] == pytest.approx(0.12)


def test_subdomain_depth_hit() -> None:
    """host 点分超过 3 段 → subdomain_depth 命中(权重 0.08);恰好 3 段不计。"""
    r = url_features("https://a.b.c.d.example.com/")
    assert r["features"]["subdomain_depth"] is True
    assert r["risk"] == pytest.approx(0.08)
    assert url_features(CLEAN)["features"]["subdomain_depth"] is False


def test_hyphen_ratio_hit() -> None:
    """host 连字符占比 > 0.3 → hyphen_ratio 命中(权重 0.06)。"""
    # 15 个 '-' / 43 字符 ≈ 0.35,且仅 3 个标签、正常 tld,不影响其他特征
    r = url_features("https://a-b-c-d-e-f-g-h-i-j-k-l-m-n-o-p.example.com/")
    assert r["features"]["hyphen_ratio"] is True
    assert r["risk"] == pytest.approx(0.06)


def test_nonstd_port_hit() -> None:
    """非标准端口 8080 → nonstd_port 命中(权重 0.05)。"""
    r = url_features("https://example.com:8080/admin")
    assert r["features"]["nonstd_port"] is True
    assert r["risk"] == pytest.approx(0.05)


def test_standard_ports_whitelisted() -> None:
    """端口白名单:443/80 不计 nonstd_port;80 上的明文 http 仍计 0.04。"""
    r443 = url_features("https://example.com:443/page")
    assert r443["features"]["nonstd_port"] is False
    assert r443["risk"] == pytest.approx(0.0)
    assert r443["explain"] == []

    r80 = url_features("http://example.com:80/page")
    assert r80["features"]["nonstd_port"] is False
    assert r80["features"]["plain_http"] is True
    assert r80["risk"] == pytest.approx(0.04)


def test_ip_host_ipv6_hit() -> None:
    """IPv6 直连(单标签)→ 仅 ip_host 命中(权重 0.15)。"""
    r = url_features("https://[2001:db8::1]/index.html")
    assert r["features"]["ip_host"] is True
    assert r["risk"] == pytest.approx(0.15)


def test_ip_host_ipv4_stacked_sum() -> None:
    """IPv4 直连:ip_host 0.15 + 4 段标签 subdomain_depth 0.08 + 明文 http 0.04。"""
    r = url_features("http://192.168.1.1/")
    assert r["features"]["ip_host"] is True
    assert r["features"]["subdomain_depth"] is True
    assert r["features"]["plain_http"] is True
    assert r["risk"] == pytest.approx(0.15 + 0.08 + 0.04)
    assert len(r["explain"]) == 3


def test_long_obfuscated_path_hit_and_boundary() -> None:
    """路径 > 120 字符命中(0.05);恰好 120 为边界不计。"""
    hit = url_features("https://example.com/" + "a" * 120)  # path 长 121
    assert hit["features"]["long_obfuscated_path"] is True
    assert hit["risk"] == pytest.approx(0.05)

    edge = url_features("https://example.com/" + "a" * 119)  # path 长 120
    assert edge["features"]["long_obfuscated_path"] is False
    assert edge["risk"] == pytest.approx(0.0)


def test_high_entropy_query_hit_and_boundary() -> None:
    """查询串 ≥32 位连续 hex/base64 样式段命中(0.10);31 位不计。"""
    hit = url_features("https://example.com/s?q=" + "0123456789abcdef" * 2)
    assert hit["features"]["high_entropy_query"] is True
    assert hit["risk"] == pytest.approx(0.10)

    edge = url_features("https://example.com/s?q=" + "0123456789abcdef0123456789abcde")
    assert edge["features"]["high_entropy_query"] is False
    assert edge["risk"] == pytest.approx(0.0)


def test_plain_http_hit() -> None:
    """scheme==http → plain_http 命中(权重 0.04)。"""
    r = url_features("http://example.com/")
    assert r["features"]["plain_http"] is True
    assert r["risk"] == pytest.approx(0.04)


def test_deceptive_prefix_hit() -> None:
    """www- 伪装前缀(www-paypal 拼接样式)→ deceptive_prefix 命中(0.15)。"""
    r = url_features("https://www-paypal.example.com/login")
    assert r["features"]["deceptive_prefix"] is True
    assert r["risk"] == pytest.approx(0.15)
    # 正常 www. 前缀不误报
    assert url_features(CLEAN)["features"]["deceptive_prefix"] is False
    # 伪装前缀出现在任意标签(非开头)同样命中
    deep = url_features("https://secure.www-verify.example.com/")
    assert deep["features"]["deceptive_prefix"] is True


# ---------------------------------------------------------------------------
# 多特征叠加、clamp 与解析失败
# ---------------------------------------------------------------------------


def test_multi_feature_weighted_sum() -> None:
    """8 个特征叠加:风险 = 权重精确求和 0.79,explain 同为 8 条。"""
    url = (
        "http://www-secure.a.b.c.xn--80ak6aa92e.top:8080/"
        + "a" * 121
        + "?t="
        + "0" * 32
    )
    r = url_features(url)
    f = r["features"]
    assert (
        f["plain_http"]
        and f["deceptive_prefix"]
        and f["subdomain_depth"]
        and f["punycode_host"]
        and f["suspicious_tld"]
        and f["nonstd_port"]
        and f["long_obfuscated_path"]
        and f["high_entropy_query"]
    )
    # hyphen_ratio 3/35≈0.09、ip/idn 均未命中
    assert not f["hyphen_ratio"] and not f["ip_host"] and not f["idn_non_ascii"]
    assert r["risk"] == pytest.approx(
        0.04 + 0.15 + 0.08 + 0.20 + 0.12 + 0.05 + 0.05 + 0.10
    )
    assert len(r["explain"]) == 8


def test_risk_clamped_to_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """权重总和超 1 时 clamp 到上限 1.0(注入放大权重模拟极端配置)。"""
    monkeypatch.setattr(
        url_intel, "FEATURE_WEIGHTS", {"plain_http": 0.8, "suspicious_tld": 0.7}
    )
    r = url_features("http://example.top/")
    assert r["features"]["plain_http"] is True
    assert r["features"]["suspicious_tld"] is True
    assert r["risk"] == 1.0


@pytest.mark.parametrize(
    "bad",
    [
        "",              # 空串
        "   ",           # 空白
        "not-a-url",     # 无 scheme/host
        "http://example.com:bad/",  # 非数字端口
        "http://[::1/",  # IPv6 方括号不闭合
    ],
)
def test_unparseable_urls_return_fixed_shape(bad: str) -> None:
    """解析失败统一降级:空特征、0 风险、固定中文提示。"""
    r = url_features(bad)
    assert r == {"features": {}, "risk": 0.0, "explain": ["URL 无法解析"]}


# ---------------------------------------------------------------------------
# 区间与 explain/features 对应关系
# ---------------------------------------------------------------------------


def test_risk_always_within_unit_interval() -> None:
    """所有样本的 risk 恒在 [0, 1]。"""
    for u in SAMPLES:
        r = url_features(u)
        assert isinstance(r["risk"], float)
        assert 0.0 <= r["risk"] <= 1.0, u


def test_explain_matches_feature_hits() -> None:
    """每个样本:命中特征数 == len(explain),且键集合与权重表一致。"""
    for u in SAMPLES:
        r = url_features(u)
        hits = [v for v in r["features"].values() if v]
        assert set(r["features"]) == set(url_intel.FEATURE_WEIGHTS), u
        assert len(hits) == len(r["explain"]), u
        assert all(isinstance(msg, str) and msg for msg in r["explain"]), u


# ---------------------------------------------------------------------------
# V5 升级锁定:单遍判定 / 快路径等价 / 阈值常量化 / 遥测
# ---------------------------------------------------------------------------


def test_v5_module_exports_and_threshold_constants() -> None:
    """__all__ 齐备;判定阈值魔法数字已提为模块常量且与契约口径一致。"""
    assert set(url_intel.__all__) >= {"SUSPICIOUS_TLDS", "FEATURE_WEIGHTS", "url_features"}
    assert url_intel._HYPHEN_RATIO_LIMIT == 0.3
    assert url_intel._LONG_PATH_LIMIT == 120
    assert url_intel._ENTROPY_RUN_MIN == 32
    # 高熵正则按常量拼装:32 位命中 / 31 位不命中(与契约边界一致)
    assert url_intel._HIGH_ENTROPY_RUN.search("0" * 32)
    assert url_intel._HIGH_ENTROPY_RUN.search("0" * 31) is None


def test_v5_ip_literal_fast_path_equivalence() -> None:
    """V5 快路径(非数字开头且无冒号直接非 IP)与 ipaddress 判定等价。"""
    import ipaddress

    samples = [
        "192.168.1.1", "8.8.8.8", "2001:db8::1", "::1",          # 真 IP
        "example.com", "shop.example.xyz", "123.example.com",     # 域名(含数字开头)
        "xn--80ak6aa92e.com", "a-b-c.example.com", "www-paypal.example.com",
    ]
    for host in samples:
        try:
            expected = ipaddress.ip_address(host) is not None
        except ValueError:
            expected = False
        assert url_intel._is_ip_literal(host) is expected, host


def test_v5_idn_feature_uses_isascii() -> None:
    """idn_non_ascii 换用 str.isascii 后语义不变:仅非 ASCII host 命中。"""
    assert url_features("https://例え.jp/おわり")["features"]["idn_non_ascii"] is True
    for ascii_url in (CLEAN, "https://xn--80ak6aa92e.com/", "http://192.168.1.1/"):
        assert url_features(ascii_url)["features"]["idn_non_ascii"] is False, ascii_url


def test_v5_telemetry_timer_and_unparseable_counter() -> None:
    """url_features 记 telemetry.timer("url_intel.features");解析失败计数。"""
    from netsentinel import telemetry

    telemetry.reset()
    try:
        for _ in range(3):
            url_features(CLEAN)
        for bad in ("", "http://example.com:bad/", "http://[::1/"):
            url_features(bad)
        snap = telemetry.snapshot()
        assert snap["timers"]["url_intel.features"]["count"] == 3
        assert snap["counters"]["url_intel.unparseable"] == 3
    finally:
        telemetry.reset()
