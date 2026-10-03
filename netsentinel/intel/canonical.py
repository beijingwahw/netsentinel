"""可注册域归一(A103)—— V6「归纳同名」的判定地基。

依据 CONTRACTS-V6.md §2 核心概念(canonical/镜像)与 §4 A103 条目:
大批量筛选后要按「同一名称」归纳镜像,首先必须把 www/子域/端口/路径/
scheme 各异的 URL 归一到**可注册域**(registrable domain)::

    http://a.b.example.com.cn:8080/x?y  →  example.com.cn

归一规则(canonical_key):
  1. 解析 host:小写、去端口/认证信息/IPv6 方括号/尾部圆点;
  2. host 是 IP 字面量(v4/v6,``ipaddress`` 校验)→ 返回规范化 IP 串;
  3. 单标签主机(localhost 等内网名)→ 原样返回;
  4. 其余逐级去掉最左标签,直到剩余部分 ∈ MULTI_SUFFIXES ∪ 通用 TLD
     (≥2 位纯 ASCII 字母),再向左补一个标签即为可注册域
     (首个命中即最长后缀,PSL 语义);
  5. 解析失败 / 无 host → ``""``;尾标签不是合法 TLD 的畸形域名 → 整串兜底。

同 canonical_key 的所有 URL 视为同一站点(V6 镜像归并的基础组),
www/子域/端口/路径差异由 alias_label 记为中文变体描述。
纯标准库(urllib.parse / ipaddress),零网络、零落盘;结果决定且恒为小写。
A107 bulk_intake 负责给裸域名补 scheme,本模块不猜测、按契约口径返回 ""。
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

__all__ = [
    "MULTI_SUFFIXES",
    "canonical_key",
    "canonical_name",
    "is_same_site",
    "alias_label",
]

#: 常见多段(二级)公共后缀(契约 §4 A103 固定表,禁增删):此类后缀
#: 本身不可单独注册,可注册域需再向左取一个标签
#: (www.example.co.uk → example.co.uk;a.b.example.com.cn → example.com.cn)。
MULTI_SUFFIXES = {
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
    "com.hk", "com.tw", "com.sg", "co.jp", "co.uk", "com.au",
}

#: canonical_name 解析失败回退展示时的最大长度(契约:原 URL 截断 80 字)。
_NAME_FALLBACK_LIMIT = 80


def _is_generic_tld(label: str) -> bool:
    """单标签是否属于通用 TLD:≥2 位纯 ASCII 字母(com/cn/io…)。

    含点号的串天然不通过(多段后缀只查 MULTI_SUFFIXES),数字尾标签、
    单字母、非 ASCII(如 IDN 原文 ``中国``)均不算通用 TLD。
    """
    return len(label) >= 2 and label.isascii() and label.isalpha()


def _split_host(url: str) -> tuple[str, int | None, str]:
    """解析 URL 提取 ``(host, 显式端口, 路径)``。

    host 已小写、去认证信息(user:pass@)与 IPv6 方括号、去尾部圆点及
    前后空白;解析失败(空串 / 无 host / 非法端口 / 括号不闭合的 IPv6)
    统一返回 ``("", None, "")``,与契约「解析失败→空串」口径一致
    (同 url_intel 的降级口径)。
    """
    try:
        raw = (url or "").strip()
        if not raw:
            return "", None, ""
        parts = urlsplit(raw)
        host = (parts.hostname or "").strip().rstrip(".")
        port = parts.port  # 端口非数字或超 0-65535 时抛 ValueError
        path = parts.path or ""
    except ValueError:
        return "", None, ""
    if not host:
        return "", None, ""
    return host, port, path


def canonical_key(url: str) -> str:
    """把任意 URL 归一为可注册域小写键;IP 直连返回 IP 串;失败返回 ""。

    - ``http://a.b.example.com.cn:8080/x?y`` → ``example.com.cn``
      (逐级去最左标签,``com.cn`` 命中 MULTI_SUFFIXES,向左补一标签);
    - ``https://www.example.com/`` → ``example.com``(www 归并);
    - ``http://192.168.10.7:8000/a`` → ``192.168.10.7``(v4 直连);
    - ``http://[2001:0DB8::0001]/x`` → ``2001:db8::1``(v6 压缩小写);
    - ``http://localhost:8080/app`` → ``localhost``(单标签原样);
    - ``"not a url"`` / 空串 / 无 host → ``""``。

    端口/查询/片段/scheme/路径差异不影响结果;函数纯本地、决定、恒小写。
    """
    host, _port, _path = _split_host(url)
    if not host:
        return ""
    # IP 直连:返回 ipaddress 规范化串(v4 原样;v6 压缩、小写)
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) == 1:
        return host  # 单标签主机(localhost / 内网主机名):原样即可注册
    if not all(labels):
        return host  # 防御连续圆点等畸形域名:整串兜底,保守同 host 同站
    # 逐级去最左标签直到剩余部分命中多段后缀或通用 TLD;
    # 首个命中即最长后缀,可注册域 = 命中后缀再向左取一个标签。
    for cut in range(1, len(labels)):
        suffix = ".".join(labels[cut:])
        if suffix in MULTI_SUFFIXES or _is_generic_tld(suffix):
            return ".".join(labels[cut - 1:])
    # 尾标签不是合法 TLD(数字/单字母/非 ASCII):无公共后缀可言,
    # 整串视为键(非解析失败:host 结构完整,仅 TLD 罕见)。
    return host


def canonical_name(url: str) -> str:
    """展示主名:成功时 = canonical_key;解析失败时回退为原 URL 截断。

    契约 §4 A103:canonical_name = 可注册域(展示主名);失败 → 原 URL
    截断 80 字,保证任何输入都有非空可展示的名称。
    """
    key = canonical_key(url)
    if key:
        return key
    return (url or "")[:_NAME_FALLBACK_LIMIT]


def is_same_site(a: str, b: str) -> bool:
    """两个 URL 是否归属同一可注册域(www/子域/端口/路径/scheme 差异忽略)。

    任一侧解析失败(键为空)即 False——两个「解析不了」的输入不能因为
    同为空串而判为同站。
    """
    key_a = canonical_key(a)
    if not key_a:
        return False
    return key_a == canonical_key(b)


def alias_label(url: str) -> str:
    """相对 canonical_key 的中文变体描述(用于镜像组 aliases 展示)。

    可能的组成片段依次拼接(``/`` 分隔):
      - ``www 前缀``:最左标签恰为 www(www.a.b 形如 www 前缀+子域 a.b);
      - ``子域 x``:可注册域之上的其余子域前缀(如 ``子域 a.b``);
      - ``端口 N``:URL 显式写了端口(含显式 80/443);
      - ``带路径``:路径非空且不是 ``/``(查询/片段不计)。
    与 canonical 完全无差异 → ``主站``;解析失败 → ``""``。
    IP 直连 / 单标签主机没有子域概念,只可能出现端口/路径片段。
    """
    host, port, path = _split_host(url)
    if not host:
        return ""
    key = canonical_key(url)
    parts: list[str] = []
    prefix = ""
    if host != key and host.endswith("." + key):
        prefix = host[: -(len(key) + 1)]
    if prefix == "www" or prefix.startswith("www."):
        parts.append("www 前缀")
        prefix = prefix[4:]  # 去掉 "www."(恰为 www 时剩空串)
    if prefix:
        parts.append(f"子域 {prefix}")
    if port is not None:
        parts.append(f"端口 {port}")
    if path not in ("", "/"):
        parts.append("带路径")
    return "/".join(parts) if parts else "主站"
