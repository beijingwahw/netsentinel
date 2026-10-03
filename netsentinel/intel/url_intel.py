"""URL 侧静态情报(A27)。

依据 CONTRACTS-V2.md §3 A27 条目:对单个 URL 做纯本地启发式分析——
IDN/punycode 混写、可疑顶级域、子域深度、连字符占比、非标准端口、
IP 直连、超长混淆路径、高熵查询串、明文 http、伪装前缀(www- 品牌拼接)等,
输出 {"features", "risk", "explain"} 供 fusion(A29)融合使用。
只用标准库(urllib.parse / re / ipaddress),绝不联网。

用法(V5 单遍判定:解析一次、host/labels 各提取一次复用)::

    from netsentinel.intel.url_intel import url_features

    result = url_features("http://www-paypal.example.xyz:8080/login")
    # {"features": {...}, "risk": 0.31, "explain": ["域名含 www- 伪装前缀…", …]}

关键入口接 ``telemetry.timer("url_intel.features")``;解析失败降级记
``telemetry.inc("url_intel.unparseable")``(只存计数,不存 URL 内容,红线 17/23)。
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from netsentinel import telemetry

__all__ = ["FEATURE_WEIGHTS", "SUSPICIOUS_TLDS", "url_features"]

# 可疑/高风险顶级域(契约给定集合,禁增删)
SUSPICIOUS_TLDS = {
    "xyz", "top", "info", "su", "ru", "cc", "tk", "ml", "ga", "cf", "gq",
    "biz", "click", "link", "loan",
}

# 标准Web 端口白名单(命中白名单不计风险)
_STD_PORTS = {80, 443}

# --- 判定阈值(契约口径,提为常量防魔法数字) -----------------------------
#: 域名连字符占比超过该值视为关键词堆砌。
_HYPHEN_RATIO_LIMIT = 0.3
#: 路径超过该字符数视为混淆路径。
_LONG_PATH_LIMIT = 120
#: 查询串连续 hex/base64 样式段达到该长度视为高熵载荷。
_ENTROPY_RUN_MIN = 32

# 各特征命中权重:risk = 命中项权重求和后 clamp 到 [0, 1]
FEATURE_WEIGHTS: dict[str, float] = {
    "punycode_host": 0.20,
    "idn_non_ascii": 0.10,
    "suspicious_tld": 0.12,
    "subdomain_depth": 0.08,
    "hyphen_ratio": 0.06,
    "nonstd_port": 0.05,
    "ip_host": 0.15,
    "long_obfuscated_path": 0.05,
    "high_entropy_query": 0.10,
    "plain_http": 0.04,
    "deceptive_prefix": 0.15,
}

# 命中特征的中文解释(键与 FEATURE_WEIGHTS 一一对应)
_EXPLAIN: dict[str, str] = {
    "punycode_host": "域名含 punycode 混写(xn--),常见于仿冒/规避封锁",
    "idn_non_ascii": "域名含非 ASCII 字符(IDN 同形异义混写风险)",
    "suspicious_tld": "顶级域属可疑高风险后缀(xyz/top/tk 等)",
    "subdomain_depth": "子域层级过深(点分超过 3 段),疑似拼凑伪装域名",
    "hyphen_ratio": "域名中连字符占比超过 0.3,疑似关键词堆砌",
    "nonstd_port": "使用非 80/443 的非标准端口",
    "ip_host": "以 IP 地址直连访问(无域名),常见于规避域名封禁",
    "long_obfuscated_path": "URL 路径超过 120 字符,疑似混淆或隐藏真实资源",
    "high_entropy_query": "查询串含 ≥32 位连续 hex/base64 样式段,疑似混淆载荷",
    "plain_http": "使用明文 http 协议传输(非 https)",
    "deceptive_prefix": "域名含 www- 伪装前缀(如 www-paypal),疑似仿冒知名站点",
}

# 查询串中 ≥32 位连续 hex/base64 样式字符段(hex 字符集是其子集);
# 阈值取自上方常量,模块加载期拼装并编译一次(无逐调用重编译)。
_HIGH_ENTROPY_RUN = re.compile(rf"[0-9A-Za-z+/_\-]{{{_ENTROPY_RUN_MIN},}}")


def _unparseable() -> dict:
    """解析失败的固定降级结果(每次返回新对象,避免共享可变状态)。"""
    telemetry.inc("url_intel.unparseable")
    return {"features": {}, "risk": 0.0, "explain": ["URL 无法解析"]}


def _is_ip_literal(host: str) -> bool:
    """host 是否为 IPv4/IPv6 地址字面量(urlsplit 已去掉 IPv6 方括号)。

    V5 快路径:合法 IPv4 字面量必以数字开头、IPv6 必含 ``:``;普通域名
    (绝大多数输入)直接短路返回 False,免去 ``ipaddress`` 的异常构造开销。
    """
    if ":" not in host and not host[:1].isdigit():
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def url_features(url: str) -> dict:
    """对单个 URL 做纯本地静态启发式分析,返回特征、加权风险分与中文解释。

    返回结构:
      {"features": {12 个布尔特征, 键名英文小写},
       "risk": float,          # 命中项加权求和后 clamp 到 [0, 1]
       "explain": [str, ...]}  # 每个命中特征一条中文说明,顺序与 features 一致

    解析失败(空串 / 无 host / 非法端口 / 括号不闭合的 IPv6 等)统一降级为
    {"features": {}, "risk": 0.0, "explain": ["URL 无法解析"]}。
    本函数只做字符串与结构分析,不发起任何网络请求。

    V5:单遍判定——``urlsplit`` 解析一次,host/port/labels/tld 各提取一次
    供 11 个特征复用,不逐特征重复解析;整体耗时记
    ``telemetry.timer("url_intel.features")``。
    """
    try:
        raw = (url or "").strip()
        if not raw:
            return _unparseable()
        parts = urlsplit(raw)
        host = parts.hostname  # 已小写、已去 IPv6 方括号;结构非法时抛 ValueError
        port = parts.port      # 端口非数字或超 0-65535 时抛 ValueError
    except ValueError:
        return _unparseable()
    if not host:
        return _unparseable()

    with telemetry.timer("url_intel.features"):
        scheme = parts.scheme.lower()
        path = parts.path or ""
        query = parts.query or ""
        labels = host.split(".")
        tld = labels[-1]

        features: dict[str, bool] = {
            "punycode_host": "xn--" in host,
            "idn_non_ascii": not host.isascii(),
            "suspicious_tld": tld in SUSPICIOUS_TLDS,
            "subdomain_depth": len(labels) > 3,
            "hyphen_ratio": host.count("-") / len(host) > _HYPHEN_RATIO_LIMIT,
            "nonstd_port": port is not None and port not in _STD_PORTS,
            "ip_host": _is_ip_literal(host),
            "long_obfuscated_path": len(path) > _LONG_PATH_LIMIT,
            "high_entropy_query": bool(_HIGH_ENTROPY_RUN.search(query)),
            "plain_http": scheme == "http",
            "deceptive_prefix": any(label.startswith("www-") for label in labels),
        }

        hits = [key for key, hit in features.items() if hit]
        risk = min(1.0, sum((FEATURE_WEIGHTS[key] for key in hits), 0.0))
    return {
        "features": features,
        "risk": round(risk, 4),
        "explain": [_EXPLAIN[key] for key in hits],
    }
