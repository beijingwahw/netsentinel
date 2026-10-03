"""A103 netsentinel.intel.canonical 可注册域归一测试。

纯离线、零网络、纯函数断言(不落盘、无 fixture 依赖)。覆盖:
MULTI_SUFFIXES 契约固定表(整表冻结 + 12 后缀逐项)、
a.b.example.com.cn 三级子域(契约 §2 原例)、www 归并、
端口/查询/片段/scheme/路径差异同键、IPv4/IPv6 直连(v6 规范化压缩)、
localhost 等单标签原样、大小写/尾点/认证信息、解析失败空串、
裸域名(无 scheme)空串、非法 TLD 整串兜底、canonical_name(=key /
失败截断 80 字)、is_same_site 正反例(含 com.cn 与 com 不混并)、
alias_label 各形态(主站/www 前缀/子域/端口/带路径)与组合拼接、
属性:决定性(重复调用恒等)、恒小写、可注册域上幂等。
"""
from __future__ import annotations

import pytest

from netsentinel.intel.canonical import (
    MULTI_SUFFIXES,
    alias_label,
    canonical_key,
    canonical_name,
    is_same_site,
)

# 契约 §4 A103 固定的 12 个多段后缀(禁增删)
EXPECTED_MULTI_SUFFIXES = {
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
    "com.hk", "com.tw", "com.sg", "co.jp", "co.uk", "com.au",
}

# 同一站点的常见 URL 变体(端口/查询/片段/scheme/路径全差异)
EXAMPLE_VARIANTS = [
    "http://example.com/",
    "https://example.com",
    "https://www.example.com/#top",
    "http://example.com:8080/a/b/c?x=1&y=2#frag",
    "http://user:secret@www.example.com:90/",
    "ftp://shop.example.com/download",
]

SAMPLE_URLS = [
    "http://a.b.example.com.cn:8080/x?y",
    "HTTPS://WWW.Example.COM/",
    "http://[2001:0DB8::0001]:443/x",
    "http://192.168.10.7:8000/a",
    "http://localhost:8080/app",
    "https://mall.co.uk/p?q=1",
    "https://x.gov.cn./a",
    "http://xn--fiqs8s.com/",
]


# ---------------------------------------------------------------------------
# MULTI_SUFFIXES 契约固定表
# ---------------------------------------------------------------------------

def test_multi_suffixes_table_frozen() -> None:
    """MULTI_SUFFIXES 与契约 §4 A103 的 12 后缀整表一致(禁增删)。"""
    assert MULTI_SUFFIXES == EXPECTED_MULTI_SUFFIXES
    assert len(MULTI_SUFFIXES) == 12


@pytest.mark.parametrize("suffix", sorted(EXPECTED_MULTI_SUFFIXES))
def test_multi_suffix_each(suffix: str) -> None:
    """12 个多段后缀逐项:mall.<后缀> 本身即完整可注册域。"""
    url = f"https://mall.{suffix}/path?q=1"
    assert canonical_key(url) == f"mall.{suffix}"


def test_multi_suffix_exact_host_is_registrable() -> None:
    """host 恰好等于多段后缀(http://com.cn/):整串即键,不误切。"""
    assert canonical_key("http://com.cn/") == "com.cn"
    assert canonical_key("http://www.gov.cn/") == "www.gov.cn"


# ---------------------------------------------------------------------------
# canonical_key:子域归并 / www 归并 / 变体同键
# ---------------------------------------------------------------------------

def test_contract_example_three_level_subdomain() -> None:
    """契约 §2 原例:http://a.b.example.com.cn:8080/x?y → example.com.cn。"""
    assert canonical_key("http://a.b.example.com.cn:8080/x?y") == "example.com.cn"


def test_deep_subdomain_chain() -> None:
    """更深的子域链同样归到可注册域(a.b.c.example.com.cn)。"""
    assert canonical_key("http://a.b.c.example.com.cn/") == "example.com.cn"
    assert canonical_key("https://x.y.z.mall.co.jp/a") == "mall.co.jp"


def test_www_prefix_merged() -> None:
    """www 前缀归并到可注册域(通用 TLD 与多段后缀两类)。"""
    assert canonical_key("https://www.example.com/") == "example.com"
    assert canonical_key("http://www.example.com.cn/x") == "example.com.cn"
    assert canonical_key("http://www.mall.co.uk/a") == "mall.co.uk"


def test_variant_urls_share_key() -> None:
    """端口/查询/片段/scheme/路径/认证信息差异 → 同一 canonical_key。"""
    keys = {canonical_key(u) for u in EXAMPLE_VARIANTS}
    assert keys == {"example.com"}


def test_multi_suffix_not_collapsed_with_generic() -> None:
    """example.com.cn 与 example.com 是两个不同站点,不得混并。"""
    assert canonical_key("http://example.com.cn/") == "example.com.cn"
    assert canonical_key("http://example.com/") == "example.com"
    assert not is_same_site("http://example.com.cn/", "http://example.com/")


# ---------------------------------------------------------------------------
# canonical_key:IP 直连 / localhost / 大小写 / 解析失败 / 兜底
# ---------------------------------------------------------------------------

def test_ip_v4_direct() -> None:
    """IPv4 直连:返回 IP 串(端口/路径不影响)。"""
    assert canonical_key("http://192.168.10.7:8000/a") == "192.168.10.7"
    assert canonical_key("https://8.8.8.8/dns") == "8.8.8.8"


def test_ip_v6_direct_normalized() -> None:
    """IPv6 直连:去括号、压缩、小写规范化后返回。"""
    assert canonical_key("http://[2001:0DB8::0001]:443/x") == "2001:db8::1"
    assert canonical_key("http://[::1]/") == "::1"


def test_ip_same_site_across_variants() -> None:
    """IP 直连的变体(端口/查询/片段/路径/scheme)同站。"""
    assert is_same_site("http://1.2.3.4/", "https://1.2.3.4:8443/x?q=1#f")
    assert not is_same_site("http://1.2.3.4/", "http://1.2.3.5/")


def test_single_label_host_as_is() -> None:
    """单标签主机(localhost 等内网名)原样返回,端口/路径不影响。"""
    assert canonical_key("http://localhost:8080/app") == "localhost"
    assert canonical_key("https://intranet/a/b") == "intranet"


def test_case_trailing_dot_and_auth() -> None:
    """大小写混合、尾部圆点、认证信息均不影响 host 归一结果。"""
    assert canonical_key("HTTP://WWW.Example.COM./a") == "example.com"
    assert canonical_key("http://user:secret@www.EXAMPLE.com.cn.") == "example.com.cn"
    assert canonical_key("HTTP://WWW.EXAMPLE.COM/") == "example.com"


def test_parse_failures_return_empty() -> None:
    """解析失败/无 host(空串、纯空白、裸路径、空 netloc、非法端口)→ ""。"""
    for bad in ["", "   ", "not a url at all", "http:///path", "http://:9000/",
                "http://example.com:notaport/", "http://example.com:70000/"]:
        assert canonical_key(bad) == "", bad


def test_bare_domain_without_scheme_empty() -> None:
    """裸域名(无 scheme,如 "example.com")按契约口径无 host → ""。

    补 scheme 是 A107 bulk_intake 的职责;本模块不猜测输入。
    """
    assert canonical_key("example.com") == ""
    assert canonical_key("www.example.com.cn") == ""


def test_invalid_tld_falls_back_to_whole_host() -> None:
    """尾标签不是合法 TLD(数字/单字母/非 ASCII/连续圆点)→ 整串兜底。"""
    assert canonical_key("http://a.example.x/") == "a.example.x"
    assert canonical_key("http://host.123/") == "host.123"
    assert canonical_key("http://a..example.com/") == "a..example.com"
    # 兜底键仍决定:同 host 同站
    assert is_same_site("http://a.example.x/", "https://a.example.x:8080/p")


# ---------------------------------------------------------------------------
# canonical_name
# ---------------------------------------------------------------------------

def test_canonical_name_equals_key() -> None:
    """解析成功时 canonical_name = canonical_key(展示主名)。"""
    assert canonical_name("http://a.b.example.com.cn/x") == "example.com.cn"
    assert canonical_name("https://www.example.com/") == "example.com"
    assert canonical_name("http://192.168.10.7:8000/a") == "192.168.10.7"
    assert canonical_name("http://localhost/") == "localhost"


def test_canonical_name_fallback_truncated_to_80() -> None:
    """解析失败时回退为原 URL 截断 80 字(保证有非空可展示名称)。"""
    long_bad = "无法解析的输入" + "x" * 100
    assert canonical_name(long_bad) == long_bad[:80]
    assert len(canonical_name(long_bad)) == 80
    assert canonical_name("") == ""


# ---------------------------------------------------------------------------
# is_same_site
# ---------------------------------------------------------------------------

def test_is_same_site_true_matrix() -> None:
    """www/子域/端口/路径/scheme 差异均判同站(V6 镜像归并语义)。"""
    base = "https://example.com/"
    for other in EXAMPLE_VARIANTS:
        assert is_same_site(base, other), other
    assert is_same_site("http://a.b.example.com.cn/", "https://x.example.com.cn:9/p")


def test_is_same_site_false_matrix() -> None:
    """不同可注册域 / 形近域名 / 任一侧解析失败 → 不同站。"""
    base = "https://example.com/"
    for other in ["https://example.org/", "http://evil-example.com/",
                  "http://example.com.evil.net/", "not a url", ""]:
        assert not is_same_site(base, other), other
    # 双方都解析失败不得因同为 "" 判同站
    assert not is_same_site("not a url", "also not a url")
    assert not is_same_site("", "")


# ---------------------------------------------------------------------------
# alias_label:各形态与组合
# ---------------------------------------------------------------------------

def test_alias_label_main_site() -> None:
    """与 canonical 无差异(无 www/子域/端口/路径)→ "主站"。"""
    assert alias_label("https://example.com/") == "主站"
    assert alias_label("http://example.com") == "主站"
    assert alias_label("https://example.com/?q=1#top") == "主站"  # 查询/片段不计
    assert alias_label("http://localhost/") == "主站"


def test_alias_label_www_prefix() -> None:
    """最左标签恰为 www → "www 前缀"。"""
    assert alias_label("https://www.example.com/") == "www 前缀"
    assert alias_label("http://www.example.com.cn/") == "www 前缀"


def test_alias_label_subdomain() -> None:
    """可注册域之上的子域前缀 → "子域 x"(支持多级)。"""
    assert alias_label("http://shop.example.com/") == "子域 shop"
    assert alias_label("http://a.b.example.com.cn/") == "子域 a.b"
    # www2 不是 www,按子域描述
    assert alias_label("http://www2.example.com/") == "子域 www2"


def test_alias_label_www_plus_subdomain() -> None:
    """www 之下还有子域(www.shop.example.com)→ 两段拼接。"""
    assert alias_label("http://www.shop.example.com/") == "www 前缀/子域 shop"


def test_alias_label_port() -> None:
    """URL 显式端口 → "端口 N"(显式 80/443 也如实描述)。"""
    assert alias_label("http://example.com:8080/") == "端口 8080"
    assert alias_label("https://example.com:443/") == "端口 443"
    assert alias_label("http://example.com:80/a") == "端口 80/带路径"


def test_alias_label_path() -> None:
    """路径非空且非 "/" → "带路径";仅查询/片段不算。"""
    assert alias_label("https://example.com/a") == "带路径"
    assert alias_label("https://example.com/a/b/c?x=1#f") == "带路径"


def test_alias_label_combined() -> None:
    """组合形态:www 前缀 → 子域 → 端口 → 带路径 顺序拼接。"""
    assert alias_label("http://www.example.com:8080/a") == "www 前缀/端口 8080/带路径"
    assert alias_label("http://www.shop.example.com.cn:9000/p") == (
        "www 前缀/子域 shop/端口 9000/带路径"
    )
    assert alias_label("http://a.b.example.com:7070/") == "子域 a.b/端口 7070"


def test_alias_label_ip_and_localhost() -> None:
    """IP 直连 / localhost 无子域概念,仅端口/路径片段。"""
    assert alias_label("http://10.0.0.1:9000/x") == "端口 9000/带路径"
    assert alias_label("http://[2001:db8::1]:8080/p") == "端口 8080/带路径"
    assert alias_label("http://10.0.0.1/") == "主站"
    assert alias_label("http://localhost:3000/") == "端口 3000"


def test_alias_label_parse_failure_empty() -> None:
    """解析失败(无 host / 非法端口)→ 空串。"""
    assert alias_label("not a url") == ""
    assert alias_label("") == ""
    assert alias_label("http://example.com:bad/") == ""


# ---------------------------------------------------------------------------
# 属性:决定性 / 恒小写 / 幂等
# ---------------------------------------------------------------------------

def test_canonical_key_deterministic() -> None:
    """决定性:同一输入重复调用结果恒等(归组键的硬要求)。"""
    for url in SAMPLE_URLS:
        first = canonical_key(url)
        for _ in range(3):
            assert canonical_key(url) == first, url


def test_canonical_key_always_lowercase() -> None:
    """属性:任意输入(含大写 host、大写 v6)的键恒为小写。"""
    for url in SAMPLE_URLS:
        key = canonical_key(url)
        assert key == key.lower(), url


def test_canonical_key_idempotent_on_registrable() -> None:
    """属性:键(域名类)带上 scheme 再归一仍得自身——已是最简形态。"""
    domain_urls = [u for u in SAMPLE_URLS if "192.168" not in u.lower() and "db8" not in u.lower()]
    for url in domain_urls:
        key = canonical_key(url)
        assert key and canonical_key(f"https://{key}/") == key, url
