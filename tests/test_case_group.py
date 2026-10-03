"""A104 netsentinel.intel.case_group 案件分组引擎测试。

纯离线、零外呼。覆盖:三域混合(www 镜像 / 端口变体 / 子域 / 多段后缀)
归并 3 组、verdict 档位取最严重、agg_max、aliases 去重小写、三键排序、
reports 缺失容错(空 dict 纯条目分组)、空输入、canonical(A103)未就位
内置兜底(monkeypatch 注入 fake / None 阻断)、图片 sha 并集、确定性
(同输入同输出 + 乱序输入分组不变)。

双世界兼容:不强制兜底的用例只断言 A103 契约与内置兜底语义一致的行为;
涉及兜底专属口径(如截断长度)的用例一律先阻断 canonical 再断言。
"""
from __future__ import annotations

import random
import sys
import types

import pytest

from netsentinel import telemetry
from netsentinel.contracts import ImageEvidence, PageSample, SiteReport, Verdict
from netsentinel.intel import case_group
from netsentinel.intel.case_group import CaseGroup, group_entries

# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def _entry(eid: int, url: str, verdict: str = "clean", evidence_zip: str = "") -> object:
    """SimpleNamespace 条目(鸭子:verdict 也可传 Verdict 枚举)。"""
    return types.SimpleNamespace(
        id=eid, site_url=url, verdict=verdict, evidence_zip=evidence_zip
    )


def _img(sha: str) -> ImageEvidence:
    return ImageEvidence(
        path=f"img_{sha[:8]}.png",
        url=f"https://example.com/static/{sha[:8]}.png",
        source_page="https://example.com/",
        sha256=sha,
        width=320,
        height=320,
    )


def _report(
    agg: float = 0.0,
    shas: list[str] | None = None,
    site: str = "https://example.com/",
    verdict: Verdict = Verdict.CLEAN,
) -> SiteReport:
    """带一页图片证据的 SiteReport(shas 里空串模拟缺失 sha)。"""
    evidences = [_img(s) for s in (shas or []) if s]
    # 空串 sha 也构造进去,验证会被跳过:用 path 占位、sha256 为空。
    for _ in [s for s in (shas or []) if not s]:
        evidences.append(ImageEvidence(path="blank.png", url="u", source_page="s"))
    pages = [PageSample(url=site, image_evidences=evidences)]
    return SiteReport(site_url=site, pages=pages, agg_nsw_prob=agg, verdict=verdict)


def _three_domain_entries() -> list[object]:
    """三域混合:www 镜像、端口变体、子域、com.cn 多段后缀、完全重复 URL。"""
    return [
        _entry(1, "https://www.example.com/a", "nsfw", "z1.zip"),
        _entry(2, "http://example.com:8080/b", "clean"),
        _entry(3, "https://img.example.com/c", "suspect"),
        _entry(8, "https://www.example.com/a", "clean"),  # 与 1 完全同 URL
        _entry(4, "https://shop.bad-site.net/x", "suspect"),
        _entry(5, "https://www.bad-site.net/", "clean"),
        _entry(6, "https://a.portal.example.com.cn/", "clean"),
        _entry(7, "http://www.example.com.cn:8443/login", "clean"),
    ]


def _block_canonical(monkeypatch: pytest.MonkeyPatch) -> None:
    """阻断 A103 canonical(sys.modules 置 None → import 必败)→ 强制内置兜底。"""
    monkeypatch.setitem(sys.modules, "netsentinel.intel.canonical", None)


# ---------------------------------------------------------------------------
# CaseGroup 数据结构与档位辅助
# ---------------------------------------------------------------------------


def test_case_group_field_defaults_and_construction_order() -> None:
    """默认构造:空列表/空集/agg 0/verdict clean/created_at 为时间戳。"""
    group = CaseGroup()
    assert group.name == ""
    assert group.aliases == [] and group.entry_ids == [] and group.site_urls == []
    assert group.agg_max == 0.0
    assert group.verdict == "clean"
    assert group.image_sha_set == set()
    assert isinstance(group.created_at, str) and group.created_at
    # 位置参数顺序与契约 §4 一致。
    positioned = CaseGroup("n", ["a"], [1], ["u"], 0.5, "nsfw", {"s"}, "2026-10-02")
    assert (positioned.name, positioned.aliases, positioned.entry_ids) == ("n", ["a"], [1])
    assert (positioned.agg_max, positioned.verdict, positioned.image_sha_set) == (
        0.5,
        "nsfw",
        {"s"},
    )
    assert positioned.created_at == "2026-10-02"


def test_worst_verdict_basic_unknown_and_enum() -> None:
    """最严重档竞选:常规组合 / 空参 / 未知值忽略 / Verdict 枚举兼容。"""
    assert CaseGroup.worst_verdict("clean", "suspect", "nsfw") == "nsfw"
    assert CaseGroup.worst_verdict("suspect", "clean") == "suspect"
    assert CaseGroup.worst_verdict("clean", "clean") == "clean"
    assert CaseGroup.worst_verdict() == "clean"
    assert CaseGroup.worst_verdict("", "非法值", "clean") == "clean"
    assert CaseGroup.worst_verdict(Verdict.CLEAN, "nsfw") == "nsfw"
    assert CaseGroup.worst_verdict("suspect", Verdict.NSFW) == "nsfw"


def test_verdict_rank_strict_ordering() -> None:
    """档位序数 clean=0 < suspect=1 < nsfw=2;未知/空 → -1。"""
    assert CaseGroup.verdict_rank("clean") < CaseGroup.verdict_rank("suspect")
    assert CaseGroup.verdict_rank("suspect") < CaseGroup.verdict_rank("nsfw")
    assert CaseGroup.verdict_rank("") == -1
    assert CaseGroup.verdict_rank("unknown") == -1
    assert CaseGroup.verdict_rank(Verdict.NSFW) == 2


def test_sort_key_descending_three_keys() -> None:
    """sort_key 三键降序编码 + 组名升序兜底。"""
    big = CaseGroup("b.com", entry_ids=[1, 2], verdict="clean", agg_max=0.0)
    small = CaseGroup("a.com", entry_ids=[1], verdict="clean", agg_max=0.0)
    assert big.sort_key() < small.sort_key()  # 组规模降序(取负后比较)
    assert CaseGroup("z.com", verdict="nsfw").sort_key() < CaseGroup(
        "a.com", verdict="clean"
    ).sort_key()
    assert CaseGroup("z.com", verdict="clean", agg_max=0.9).sort_key() < CaseGroup(
        "a.com", verdict="clean", agg_max=0.1
    ).sort_key()


# ---------------------------------------------------------------------------
# 空输入 / 纯条目分组 / reports 容错
# ---------------------------------------------------------------------------


def test_empty_entries_returns_empty_list() -> None:
    """空输入 → [](entries 为空列表 / None / 含多余 reports 均然)。"""
    assert group_entries([], {}) == []
    assert group_entries(None, None) == []
    assert group_entries([], {1: _report(agg=0.9)}) == []


def test_extra_report_ids_ignored() -> None:
    """reports 含未出现于 entries 的 id → 不影响结果。"""
    entries = [_entry(1, "https://www.example.com/", "nsfw")]
    reports = {1: _report(agg=0.4), 99: _report(agg=1.0), 100: _report(agg=0.8)}
    groups = group_entries(entries, reports)
    assert len(groups) == 1
    assert groups[0].agg_max == 0.4


def test_reports_empty_dict_pure_grouping() -> None:
    """reports 为空 dict → 纯条目分组仍成立(agg 0、sha 空)。"""
    groups = group_entries(_three_domain_entries(), {})
    assert len(groups) == 3
    for group in groups:
        assert group.agg_max == 0.0
        assert group.image_sha_set == set()


def test_reports_none_tolerated() -> None:
    """reports 传 None 与空 dict 等价(容错)。"""
    assert group_entries(_three_domain_entries(), None) == group_entries(
        _three_domain_entries(), {}
    )


# ---------------------------------------------------------------------------
# 三域混合归并
# ---------------------------------------------------------------------------


def test_three_domains_merge_into_three_groups() -> None:
    """8 条(www 镜像/端口/子域/com.cn/重复 URL)→ 3 组,成员正确。"""
    groups = group_entries(_three_domain_entries(), {})
    assert len(groups) == 3
    by_name = {g.name: g for g in groups}
    assert set(by_name) == {"example.com", "bad-site.net", "example.com.cn"}
    # www / 无 www / 端口 / 子域 / 完全重复 URL 全部归并进同一组。
    assert sorted(by_name["example.com"].entry_ids) == [1, 2, 3, 8]
    assert sorted(by_name["bad-site.net"].entry_ids) == [4, 5]
    assert sorted(by_name["example.com.cn"].entry_ids) == [6, 7]


def test_group_name_is_canonical_registered_domain() -> None:
    """组名 = canonical 主名:通用 TLD 取末两段,com.cn 取末三段。"""
    groups = group_entries(_three_domain_entries(), {})
    names = {g.name for g in groups}
    assert "example.com" in names
    assert "example.com.cn" in names  # a.portal.example.com.cn → example.com.cn
    assert all("www" not in n and "portal" not in n for n in names)


def test_aliases_dedup_lowercase_first_seen_order() -> None:
    """aliases = 去重 host 列表(小写、不含端口/路径,首见顺序)。"""
    entries = [
        _entry(1, "https://WWW.Example.COM/a"),
        _entry(2, "http://example.com:8080/b"),
        _entry(3, "https://img.example.com/c"),
        _entry(4, "https://www.example.com/d"),  # host 已见过 → 去重
    ]
    groups = group_entries(entries, {})
    assert len(groups) == 1
    assert groups[0].aliases == ["www.example.com", "example.com", "img.example.com"]


def test_entry_ids_keep_input_order_and_site_urls_dedup() -> None:
    """组内 entry_ids 保持输入顺序;site_urls 去重且保持首见顺序。"""
    groups = group_entries(_three_domain_entries(), {})
    main = next(g for g in groups if g.name == "example.com")
    assert main.entry_ids == [1, 2, 3, 8]
    assert main.site_urls == [
        "https://www.example.com/a",
        "http://example.com:8080/b",
        "https://img.example.com/c",
    ]


# ---------------------------------------------------------------------------
# verdict / agg_max / sha 并集
# ---------------------------------------------------------------------------


def test_verdict_takes_most_severe_across_group() -> None:
    """clean + suspect + nsfw 同组 → 组判定 nsfw(最严重档)。"""
    entries = [
        _entry(1, "https://www.example.com/", "clean"),
        _entry(2, "https://example.com/", "suspect"),
        _entry(3, "https://img.example.com/", "nsfw"),
    ]
    groups = group_entries(entries, {})
    assert len(groups) == 1 and groups[0].verdict == "nsfw"


def test_verdict_clean_plus_suspect_is_suspect() -> None:
    """clean + suspect → suspect。"""
    entries = [
        _entry(1, "https://a.example.com/", "clean"),
        _entry(2, "https://b.example.com/", "suspect"),
    ]
    assert group_entries(entries, {})[0].verdict == "suspect"


def test_verdict_enum_instance_accepted() -> None:
    """条目 verdict 为 Verdict 枚举实例时同样参与档位竞选。"""
    entries = [
        _entry(1, "https://www.example.com/", Verdict.CLEAN),
        _entry(2, "https://img.example.com/", Verdict.NSFW),
    ]
    assert group_entries(entries, {})[0].verdict == "nsfw"


def test_verdict_missing_or_unknown_defaults_clean() -> None:
    """条目缺 verdict 属性 / 未知值 → 组判定回退 clean。"""
    no_verdict = types.SimpleNamespace(id=1, site_url="https://www.example.com/")
    entries = [no_verdict, _entry(2, "https://img.example.com/", "什么鬼")]
    groups = group_entries(entries, {})
    assert len(groups) == 1 and groups[0].verdict == "clean"


def test_agg_max_takes_maximum_across_reports() -> None:
    """agg_max 取组内 report 的 agg_nsw_prob 最大值。"""
    entries = [
        _entry(1, "https://www.example.com/", "nsfw"),
        _entry(2, "https://img.example.com/", "nsfw"),
    ]
    reports = {1: _report(agg=0.31), 2: _report(agg=0.95)}
    assert group_entries(entries, reports)[0].agg_max == pytest.approx(0.95)


def test_agg_max_zero_when_no_report_for_group() -> None:
    """组内条目在 reports 中全部缺失 → agg_max == 0.0。"""
    entries = [_entry(1, "https://www.example.com/", "suspect")]
    assert group_entries(entries, {2: _report(agg=0.9)})[0].agg_max == 0.0


def test_image_sha_union_from_reports_dedup() -> None:
    """image_sha_set = 组内各 report 页面图片 sha 并集(跨条目重复 sha 去重)。"""
    sha_a, sha_b, sha_c = "a" * 64, "b" * 64, "c" * 64
    entries = [
        _entry(1, "https://www.example.com/", "nsfw"),
        _entry(2, "https://img.example.com/", "nsfw"),
    ]
    reports = {
        1: _report(agg=0.9, shas=[sha_a, sha_b]),
        2: _report(agg=0.8, shas=[sha_b, sha_c, ""]),  # 空 sha 应被跳过
    }
    groups = group_entries(entries, reports)
    assert groups[0].image_sha_set == {sha_a, sha_b, sha_c}


def test_image_sha_only_from_entries_with_report() -> None:
    """组内一半条目缺 report → sha 并集只来自有 report 的条目。"""
    sha_x = "x" * 64
    entries = [
        _entry(1, "https://www.example.com/", "nsfw"),
        _entry(2, "https://img.example.com/", "nsfw"),
    ]
    groups = group_entries(entries, {1: _report(agg=0.9, shas=[sha_x])})
    assert groups[0].image_sha_set == {sha_x}


# ---------------------------------------------------------------------------
# 排序三键
# ---------------------------------------------------------------------------


def test_sort_verdict_tier_descending() -> None:
    """第一键:verdict 档降序(nsfw → suspect → clean)。"""
    entries = [
        _entry(1, "https://www.clean-site.org/", "clean"),
        _entry(2, "https://www.nsfw-site.net/", "nsfw"),
        _entry(3, "https://www.suspect-site.com/", "suspect"),
    ]
    groups = group_entries(entries, {})
    assert [g.name for g in groups] == [
        "nsfw-site.net",
        "suspect-site.com",
        "clean-site.org",
    ]


def test_sort_agg_max_tiebreak_on_equal_verdict() -> None:
    """第二键:verdict 同档时按 agg_max 降序。"""
    entries = [
        _entry(1, "https://www.low-site.org/", "suspect"),
        _entry(2, "https://www.high-site.net/", "suspect"),
    ]
    reports = {1: _report(agg=0.2), 2: _report(agg=0.88)}
    groups = group_entries(entries, reports)
    assert [g.name for g in groups] == ["high-site.net", "low-site.org"]


def test_sort_group_size_tiebreak_on_equal_verdict_and_agg() -> None:
    """第三键:verdict 与 agg 都相同时组规模大者在前。"""
    entries = [
        _entry(1, "https://www.small-site.org/", "suspect"),
        _entry(2, "https://a.big-site.net/", "suspect"),
        _entry(3, "https://b.big-site.net/", "suspect"),
    ]
    groups = group_entries(entries, {})
    assert [len(g.entry_ids) for g in groups] == [2, 1]
    assert groups[0].name == "big-site.net"


def test_sort_deterministic_when_all_three_keys_tie() -> None:
    """三键全同 → 组名升序兜底,输出确定。"""
    entries = [
        _entry(1, "https://www.beta-site.org/", "clean"),
        _entry(2, "https://www.alpha-site.net/", "clean"),
        _entry(3, "https://www.gamma-site.com/", "clean"),
    ]
    groups = group_entries(entries, {})
    assert [g.name for g in groups] == [
        "alpha-site.net",
        "beta-site.org",
        "gamma-site.com",
    ]


# ---------------------------------------------------------------------------
# canonical(A103)惰性导入:未就位兜底 / fake 注入
# ---------------------------------------------------------------------------


def test_canonical_not_ready_fallback_still_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    """阻断 canonical → 内置兜底接管:三域归并、组名、档位全部不受影响。"""
    _block_canonical(monkeypatch)
    groups = group_entries(_three_domain_entries(), {})
    assert {g.name for g in groups} == {"example.com", "bad-site.net", "example.com.cn"}
    top = next(g for g in groups if g.name == "example.com")
    assert top.verdict == "nsfw"
    assert sorted(top.entry_ids) == [1, 2, 3, 8]


def test_fallback_multi_suffix_and_port_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底口径:多段后缀取末三段;端口/子域/www 变体同组(与 A103 同语义)。"""
    _block_canonical(monkeypatch)
    entries = [
        _entry(1, "http://a.b.example.com.cn:8080/x?y", "nsfw"),
        _entry(2, "https://www.example.com.cn/", "clean"),
    ]
    groups = group_entries(entries, {})
    assert len(groups) == 1
    assert groups[0].name == "example.com.cn"
    assert groups[0].aliases == ["a.b.example.com.cn", "www.example.com.cn"]


def test_fallback_ip_direct_urls_group_by_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底口径:IP 直连 → IP 串为键(不同 IP 不同组)。"""
    _block_canonical(monkeypatch)
    entries = [
        _entry(1, "http://192.168.1.7:8080/x", "nsfw"),
        _entry(2, "https://192.168.1.7/y", "clean"),
        _entry(3, "http://10.0.0.2/", "clean"),
    ]
    groups = group_entries(entries, {})
    assert len(groups) == 2
    assert groups[0].name == "192.168.1.7"
    assert sorted(groups[0].entry_ids) == [1, 2]
    assert any(g.name == "10.0.0.2" for g in groups)


def test_fallback_invalid_url_name_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底口径:解析失败时 canonical_name = 原 URL 截断(60 字符)。"""
    _block_canonical(monkeypatch)
    # 括号不闭合的 IPv6 → urlsplit 直接抛 ValueError,属解析失败。
    bad_url = "http://[" + "x" * 200
    groups = group_entries([_entry(1, bad_url, "clean")], {})
    assert len(groups) == 1
    assert groups[0].name == bad_url[: case_group._NAME_TRUNCATE]


def test_fake_canonical_module_used_when_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    """monkeypatch 注入 fake canonical → 分组完全跟随 fake 的键与主名。"""
    fake = types.SimpleNamespace(
        canonical_key=lambda url: "one.fake.cn" if "site-one" in url else "two.fake.cn",
        canonical_name=lambda url: "假名:" + ("一" if "site-one" in url else "二"),
    )
    monkeypatch.setitem(sys.modules, "netsentinel.intel.canonical", fake)
    entries = [
        _entry(1, "https://a.site-one.example.com/", "nsfw"),
        _entry(2, "https://b.site-one.example.com/", "clean"),
        _entry(3, "https://whatever.site-two.net/", "suspect"),
    ]
    groups = group_entries(entries, {})
    assert [g.name for g in groups] == ["假名:一", "假名:二"]
    assert [sorted(g.entry_ids) for g in groups] == [[1, 2], [3]]


def test_fake_canonical_raising_falls_back_builtin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fake canonical_key 抛异常 → 单条按内置兜底,分组不中断。"""
    fake = types.SimpleNamespace(
        canonical_key=lambda url: (_ for _ in ()).throw(RuntimeError("炸了")),
        canonical_name=lambda url: "不会被用到",
    )
    monkeypatch.setitem(sys.modules, "netsentinel.intel.canonical", fake)
    entries = [
        _entry(1, "https://www.example.com/", "nsfw"),
        _entry(2, "https://img.example.com/", "clean"),
    ]
    groups = group_entries(entries, {})
    assert len(groups) == 1 and groups[0].name == "example.com"


def test_unparseable_urls_stay_in_separate_groups() -> None:
    """解析失败的两条不同非法 URL 各自成组,不因 key 同为空而被错误并组。"""
    entries = [
        _entry(1, "", "suspect"),
        _entry(2, "not a url at all", "nsfw"),
    ]
    groups = group_entries(entries, {})
    assert len(groups) == 2
    assert [len(g.entry_ids) for g in groups] == [1, 1]


# ---------------------------------------------------------------------------
# 鸭子容错 / 确定性 / telemetry
# ---------------------------------------------------------------------------


def test_entry_missing_id_and_evidence_zip_tolerated() -> None:
    """条目缺 id / evidence_zip → id 按 0,分组照常(getattr 容错)。"""
    bare = types.SimpleNamespace(site_url="https://www.example.com/", verdict="nsfw")
    groups = group_entries([bare], {})
    assert len(groups) == 1
    assert groups[0].entry_ids == [0]
    assert groups[0].verdict == "nsfw"


def test_determinism_same_input_same_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """冻结时钟后,同一输入两次调用输出逐字段一致(含 created_at)。"""
    monkeypatch.setattr(case_group, "now_iso", lambda: "2026-10-02T00:00:00+08:00")
    first = group_entries(_three_domain_entries(), {1: _report(agg=0.9)})
    second = group_entries(_three_domain_entries(), {1: _report(agg=0.9)})
    assert first == second


def test_determinism_shuffled_input_same_partition() -> None:
    """乱序输入:分组划分(entry_ids 集合)与各组 aliases 集合保持不变。"""
    entries = _three_domain_entries()
    baseline = group_entries(entries, {})
    rng = random.Random(42)
    for _ in range(3):
        shuffled = list(entries)
        rng.shuffle(shuffled)
        groups = group_entries(shuffled, {})
        assert {frozenset(g.entry_ids) for g in groups} == {
            frozenset(g.entry_ids) for g in baseline
        }
        assert {frozenset(g.aliases) for g in groups} == {
            frozenset(g.aliases) for g in baseline
        }
        assert {g.name for g in groups} == {g.name for g in baseline}


def test_telemetry_counters_recorded() -> None:
    """telemetry 记录组数与已分组条目数。"""
    telemetry.reset()
    groups = group_entries(_three_domain_entries(), {})
    counters = telemetry.snapshot()["counters"]
    assert counters["case_group.groups"] == len(groups) == 3
    assert counters["case_group.entries_grouped"] == 8
    telemetry.reset()
