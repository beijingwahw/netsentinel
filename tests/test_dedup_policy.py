"""A106 netsentinel.decision.dedup_policy 组间去重策略测试。

纯离线、零网络(策略层为纯函数)。覆盖:
- DedupRules 默认值与 from_config 映射(SimpleNamespace 鸭子配置 + 真实 Config 默认);
- 四类合并依据各自触发:同站(canonical/host)、图片指纹 Jaccard、图谱
  shared_image / phash_near、图谱 shared_template;
- 规则开关:same_site=False 时同站不并、template=False 时模板边不并;
- 阈值边界:恰好等于阈值合并、低于阈值不并、阈值 0、空指纹集合(0/0 不判并);
- 判定优先级:同站 > 指纹 > 图谱;
- 不合并返回 (False, "") 空串;
- dedup_report_url:内容格式 / host 去重 / aliases 纳入 / 无镜像写"无" /
  200 字边界(恰好不截断)与超限截断加"等";
- canonical 惰性导入:未就位(ImportError)按 host 全等兜底、就位后折叠
  www 子域(monkeypatch 注入假 canonical,双态确定);
- 鸭子类型:缺 image_sha_set / aliases 属性的组对象安全降级;
  graph 查询口抛错按无证据处理。

注:未 monkeypatch canonical 的用例只断言"host 兜底与真实 canonical 结论
一致"的样本(同 host 必并、跨可注册域必不并),不依赖 A103 是否已落盘。
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from types import SimpleNamespace

from netsentinel.contracts import Config
from netsentinel.decision import dedup_policy
from netsentinel.decision.dedup_policy import (
    DedupRules,
    dedup_report_url,
    from_config,
    should_merge_groups,
)

# 注意:A/B 取**不同可注册域**(.com/.org)。A103 canonical 已落盘后,
# a.example.com 与 b.example.com 会折叠为同一可注册域(→ 同站合并),
# 样例必须跨可注册域才能稳定表达"不同站点"。
A = "https://a.example.com/"
B = "https://b.example.org/"
C = "https://c.example.net/"
H1, H2, H3, H4 = "1" * 64, "2" * 64, "3" * 64, "4" * 64  # 图片 sha256(形状即可)


@dataclass
class G:
    """鸭子版 CaseGroup:策略层只按属性名访问。"""

    name: str = "g"
    site_urls: list[str] = field(default_factory=list)
    image_sha_set: set[str] = field(default_factory=set)
    aliases: list[str] = field(default_factory=list)


def mk(urls: list[str], shas: set[str] | None = None, aliases: list[str] | None = None) -> G:
    return G(name="g", site_urls=list(urls), image_sha_set=set(shas or ()), aliases=list(aliases or ()))


class FakeGraph:
    """A46 EvidenceGraph.related_sites 的鸭子替身(无向边表)。"""

    def __init__(self, pairs: list[tuple[str, str, list[str]]]) -> None:
        self.adj: dict[str, list[tuple[str, list[str]]]] = {}
        for ua, ub, via in pairs:
            self.adj.setdefault(ua, []).append((ub, list(via)))
            self.adj.setdefault(ub, []).append((ua, list(via)))

    def related_sites(self, url: str, depth: int = 1) -> list[dict[str, object]]:
        return [{"site": site, "via": via, "weight": 1.0} for site, via in self.adj.get(url, [])]


class ExplodingGraph:
    """查询口抛错的坏图:策略层须按无证据处理、不崩溃。"""

    def related_sites(self, url: str, depth: int = 1) -> list[dict[str, object]]:
        raise RuntimeError("图谱不可用")


# ---------------------------------------------------------------------------
# DedupRules / from_config
# ---------------------------------------------------------------------------


def test_rules_defaults() -> None:
    """默认规则:阈值 0.3、模板边启用、同站合并启用。"""
    rules = DedupRules()
    assert rules.phash_overlap == 0.3
    assert rules.template is True
    assert rules.same_site is True


def test_from_config_mapping() -> None:
    """from_config 读 group_merge_* 两键;same_site 无配置项保持默认开。"""
    cfg = SimpleNamespace(group_merge_phash_overlap=0.55, group_merge_template=False)
    rules = from_config(cfg)
    assert rules == DedupRules(phash_overlap=0.55, template=False, same_site=True)


def test_from_config_real_config_defaults() -> None:
    """真实 contracts.Config 默认值(0.3 / True)映射正确。"""
    rules = from_config(Config())
    assert rules.phash_overlap == 0.3
    assert rules.template is True
    assert rules.same_site is True


# ---------------------------------------------------------------------------
# 依据一:同站(canonical / host 兜底)
# ---------------------------------------------------------------------------


def test_same_site_same_host_merges() -> None:
    """同 host(路径不同)→ 合并,文案"同一站点(可注册域相同)"。"""
    a = mk([A + "x"])
    b = mk([A + "y"])
    ok, reason = should_merge_groups(a, b, DedupRules())
    assert (ok, reason) == (True, "同一站点(可注册域相同)")


def test_same_site_rule_disabled() -> None:
    """same_site=False:同 host、无其他证据 → 不合并。"""
    a = mk([A])
    b = mk([A + "y"])
    rules = DedupRules(same_site=False)
    assert should_merge_groups(a, b, rules) == (False, "")


def test_canonical_lazy_fallback_host_equality(monkeypatch) -> None:
    """canonical 未就位(canonical_key=None)→ 按 host 全等兜底。"""
    monkeypatch.setattr(dedup_policy, "_canonical_key_fn", lambda: None)
    # 同 host:路径差异不影响合并
    ok, reason = should_merge_groups(mk(["https://x.example.com/a"]), mk(["https://x.example.com/b"]), DedupRules())
    assert (ok, reason) == (True, "同一站点(可注册域相同)")
    # host 兜底是全等:子域与裸域不算同站(且无其他证据)
    assert should_merge_groups(
        mk(["https://sub.x.example.com/"]), mk(["https://x.example.com/"]), DedupRules()
    ) == (False, "")


def test_canonical_present_folds_www(monkeypatch) -> None:
    """canonical 就位(注入假实现)→ www 子域与裸域折叠为同站。"""
    def fake_canonical_key(url: str) -> str:
        host = urllib.parse.urlsplit(url).hostname or ""
        return host[4:] if host.startswith("www.") else host

    monkeypatch.setattr(dedup_policy, "_canonical_key_fn", lambda: fake_canonical_key)
    ok, reason = should_merge_groups(
        mk(["https://www.a.example.com/"]), mk(["https://a.example.com/"]), DedupRules()
    )
    assert (ok, reason) == (True, "同一站点(可注册域相同)")


# ---------------------------------------------------------------------------
# 依据二:图片指纹 Jaccard
# ---------------------------------------------------------------------------


def test_phash_overlap_merges() -> None:
    """指纹 Jaccard=0.5 ≥ 0.3 → 合并,文案含百分比。"""
    a = mk([A], {H1, H2, H3, H4})
    b = mk([B], {H1, H2})
    ok, reason = should_merge_groups(a, b, DedupRules())
    assert ok is True
    assert reason == "图片指纹重叠 50%"


def test_phash_boundary_exact_threshold() -> None:
    """恰好等于阈值(0.25 == 0.25)→ 合并(>= 语义)。"""
    a = mk([A], {H1, H2, H3, H4})
    b = mk([B], {H1})
    ok, reason = should_merge_groups(a, b, DedupRules(phash_overlap=0.25))
    assert (ok, reason) == (True, "图片指纹重叠 25%")


def test_phash_below_threshold_no_merge() -> None:
    """低于阈值(0.25 < 0.3)→ 不合并,返回空串。"""
    a = mk([A], {H1, H2, H3, H4})
    b = mk([B], {H1})
    assert should_merge_groups(a, b, DedupRules()) == (False, "")


def test_phash_threshold_zero_merges_any_nonempty() -> None:
    """阈值 0:任意非空指纹并集(即使零交集)按字面语义判并(0% ≥ 0)。"""
    ok, reason = should_merge_groups(mk([A], {H1}), mk([B], {H2}), DedupRules(phash_overlap=0.0))
    assert (ok, reason) == (True, "图片指纹重叠 0%")


def test_phash_empty_sets_never_merge() -> None:
    """两组指纹均为空(0/0 未定义)→ 不构成证据。"""
    assert should_merge_groups(mk([A]), mk([B]), DedupRules()) == (False, "")


def test_phash_one_side_empty_no_merge() -> None:
    """一侧指纹为空 → 交集为 0,低于默认阈值不并。"""
    assert should_merge_groups(mk([A], {H1, H2}), mk([B]), DedupRules()) == (False, "")


# ---------------------------------------------------------------------------
# 依据三 / 四:检索图谱边
# ---------------------------------------------------------------------------


def test_graph_shared_image_edge() -> None:
    """图谱 shared_image 直接边 → "共享图片证据"。"""
    graph = FakeGraph([(A, B, ["shared_image"])])
    ok, reason = should_merge_groups(mk([A]), mk([B]), DedupRules(), graph=graph)
    assert (ok, reason) == (True, "共享图片证据")


def test_graph_phash_near_edge() -> None:
    """图谱 phash_near 边同样计入"共享图片证据"。"""
    graph = FakeGraph([(A, B, ["phash_near"])])
    ok, reason = should_merge_groups(mk([A]), mk([B]), DedupRules(), graph=graph)
    assert (ok, reason) == (True, "共享图片证据")


def test_graph_shared_template_merges_when_enabled() -> None:
    """shared_template 边 + rules.template=True → "共享页面模板"。"""
    graph = FakeGraph([(A, B, ["shared_template"])])
    ok, reason = should_merge_groups(mk([A]), mk([B]), DedupRules(), graph=graph)
    assert (ok, reason) == (True, "共享页面模板")


def test_graph_shared_template_disabled() -> None:
    """shared_template 边 + rules.template=False → 不合并(建站工具误并防线)。"""
    graph = FakeGraph([(A, B, ["shared_template"])])
    assert should_merge_groups(mk([A]), mk([B]), DedupRules(template=False), graph=graph) == (False, "")


def test_graph_none_no_evidence() -> None:
    """graph=None(空图/离线场景)→ 无图谱证据。"""
    assert should_merge_groups(mk([A]), mk([B]), DedupRules(), graph=None) == (False, "")


def test_graph_url_normalization_match() -> None:
    """图谱边记录的 URL 与组内 URL 仅尾部斜杠之差 → 按站点键仍命中。"""
    graph = FakeGraph([(A, B + "page/", ["shared_image"])])
    ok, reason = should_merge_groups(mk([A]), mk([B + "page"]), DedupRules(), graph=graph)
    assert (ok, reason) == (True, "共享图片证据")


def test_graph_query_error_tolerated() -> None:
    """graph.related_sites 抛错 → 按无证据处理,不崩溃(安全方向:漏并不误并)。"""
    assert should_merge_groups(mk([A]), mk([B]), DedupRules(), graph=ExplodingGraph()) == (False, "")


# ---------------------------------------------------------------------------
# 优先级与不合并
# ---------------------------------------------------------------------------


def test_priority_same_site_over_phash_and_graph() -> None:
    """同站 + 指纹重叠 + 图谱边同时成立 → 依据取优先级最高的"同站"。"""
    graph = FakeGraph([(A, B, ["shared_image"])])
    a = mk([A + "x"], {H1, H2})
    b = mk([A + "y"], {H1, H2})
    ok, reason = should_merge_groups(a, b, DedupRules(), graph=graph)
    assert (ok, reason) == (True, "同一站点(可注册域相同)")


def test_priority_phash_over_graph() -> None:
    """指纹重叠 + 图谱边同时成立 → 依据取"图片指纹重叠"(指纹优先于图谱)。"""
    graph = FakeGraph([(A, B, ["shared_image", "shared_template"])])
    a = mk([A], {H1, H2})
    b = mk([B], {H1, H2})
    ok, reason = should_merge_groups(a, b, DedupRules(), graph=graph)
    assert ok is True
    assert reason == "图片指纹重叠 100%"


def test_no_merge_returns_empty_reason() -> None:
    """跨可注册域、无指纹交集、无图谱 → (False, "")。"""
    assert should_merge_groups(mk([A], {H1}), mk([B], {H2}), DedupRules()) == (False, "")


# ---------------------------------------------------------------------------
# dedup_report_url
# ---------------------------------------------------------------------------


def test_report_url_basic_content() -> None:
    """主站取第一个 site_url 原样;镜像为去重 host 的逗号拼接。"""
    group = mk([A, B + "x", C])
    text = dedup_report_url(group)
    assert text == "主站:https://a.example.com/;镜像/关联:b.example.org,c.example.net"
    assert "等" not in text  # 短文本不截断


def test_report_url_host_dedup() -> None:
    """同 host 多条 URL / alias 只列一次。"""
    group = mk(
        [A, B + "1", B + "2", C + "a"],
        aliases=[B, C + "b"],
    )
    text = dedup_report_url(group)
    assert text == "主站:https://a.example.com/;镜像/关联:b.example.org,c.example.net"


def test_report_url_aliases_included() -> None:
    """aliases 中的裸域名与带路径 URL 均折算 host 纳入清单(剔除主站)。"""
    group = mk([A], aliases=["d.example.com", "https://e.example.com/page"])
    assert dedup_report_url(group) == (
        "主站:https://a.example.com/;镜像/关联:d.example.com,e.example.com"
    )


def test_report_url_no_mirrors() -> None:
    """单站点组:镜像/关联写"无"。"""
    assert dedup_report_url(mk([A])) == "主站:https://a.example.com/;镜像/关联:无"


def test_report_url_empty_group_falls_back_to_name() -> None:
    """无任何 URL/别名:主站退回组名,镜像写"无"。"""
    assert dedup_report_url(G(name="无名组")) == "主站:无名组;镜像/关联:无"


def _report_with_mirror_lengths(n1: int, n2: int) -> str:
    """构造两个指定长度 host 的镜像组,返回生成文本(便于精确卡 200 边界)。"""
    def host(length: int) -> str:
        return "h" * (length - len(".example.com")) + ".example.com"

    h1, h2 = host(n1), host(n2)
    group = mk(["https://a.example.com/", f"https://{h1}/", f"https://{h2}/"])
    return dedup_report_url(group)


def test_report_url_exactly_limit_not_truncated() -> None:
    """恰好 200 字(固定头 32 + 镜像 168)→ 不截断、无"等"。"""
    text = _report_with_mirror_lengths(83, 84)  # 83 + 1(逗号) + 84 = 168
    assert len(text) == 200
    assert not text.endswith("等")


def test_report_url_over_limit_truncated_with_suffix() -> None:
    """超 1 字(201)→ 截断至 200 并以"等"收尾。"""
    text = _report_with_mirror_lengths(83, 85)
    assert len(text) == 200
    assert text.endswith("等")
    assert text.startswith("主站:https://a.example.com/;镜像/关联:")


# ---------------------------------------------------------------------------
# 鸭子类型降级
# ---------------------------------------------------------------------------


class BareGroup:
    """只有 site_urls 的最小组对象:验证缺字段时的安全降级。"""

    def __init__(self, urls: list[str]) -> None:
        self.site_urls = urls


def test_duck_group_without_optional_fields() -> None:
    """缺 image_sha_set / aliases 属性:判定与文案均不崩溃。"""
    a, b = BareGroup([A]), BareGroup([A + "x"])
    ok, reason = should_merge_groups(a, b, DedupRules())
    assert (ok, reason) == (True, "同一站点(可注册域相同)")
    assert should_merge_groups(BareGroup([A]), BareGroup([B]), DedupRules()) == (False, "")
    assert dedup_report_url(BareGroup([A])) == "主站:https://a.example.com/;镜像/关联:无"
