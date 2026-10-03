"""A117 netsentinel.intel.group_stats 分组战况统计测试。

纯离线、零外呼、零网络。覆盖:

- stats 全字段:三组构造(单站/多站/共享别名域)逐键断言,键集合恰为
  契约七键;Top5 截断(7 组只留 5)与排序(URL 数降序、agg_max 降序、
  组名升序兜底);verdict_dist(Verdict 枚举 / 字符串兼容、未知判定
  不计入但组数计入、三键恒在);空组列表 / None 全空战况;鸭子容错
  (缺 aliases / agg_max / verdict / name、site_urls 为 tuple);乱序
  输入结果不变(确定性);
- alias_top5:跨组复现域名排行(计数降序、域名升序、仅 ≥2 组、Top5
  截断)、无复现为空;
- export_csv:写盘可读回——BOM(utf-8-sig)、表头列序、数据行数=返回值、
  逐单元格内容(站点数=别名域数、URL 数、判定、agg_max、aliases 分号
  拼接)、空组仅表头返回 0、csv 模块 utf-8-sig 读回全矩阵一致;
- repeat_offenders:note 含 [rejected] 与 status=rejected 两种命中、
  中文 reason、(site_url, group_name) 去重、无驳回 / 无组 / canonical
  不落组为空、鸭子容错(缺 note/status、note=None、site_url 缺失、
  组缺 site_urls);
- canonical(A103)惰性导入:sys.modules 置 None 强制内置兜底后,www
  子域仍折叠到同一可注册域命中分组(双态确定);
- 与 A104 真实 CaseGroup / group_entries 产物的流水线兼容(组鸭子)。
"""
from __future__ import annotations

import csv
import sys
import types
from types import SimpleNamespace

import pytest

from netsentinel.contracts import Verdict
from netsentinel.intel.case_group import CaseGroup, group_entries
from netsentinel.intel.group_stats import (
    CSV_HEADER,
    export_csv,
    repeat_offenders,
    stats,
)

STATS_KEYS = {
    "group_count",
    "single_site_groups",
    "multi_site_groups",
    "total_urls",
    "max_group_top5",
    "verdict_dist",
    "alias_top5",
}


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def _duck(
    name: str = "",
    urls: list[str] | None = None,
    aliases: list[str] | None = None,
    agg: float = 0.0,
    verdict: object = "clean",
    **extra: object,
) -> SimpleNamespace:
    """鸭子版案件组:只按属性名访问,可注入任意额外字段或省略字段。"""
    group = SimpleNamespace(
        name=name,
        site_urls=list(urls or []),
        aliases=list(aliases if aliases is not None else []),
        agg_max=agg,
        verdict=verdict,
    )
    for key, value in extra.items():
        setattr(group, key, value)
    return group


def _case(
    name: str,
    urls: list[str],
    aliases: list[str],
    agg: float,
    verdict: str,
    ids: list[int] | None = None,
) -> CaseGroup:
    """真实 CaseGroup(A104 兄弟模块,只读使用)。"""
    return CaseGroup(
        name=name,
        aliases=list(aliases),
        entry_ids=list(ids or []),
        site_urls=list(urls),
        agg_max=agg,
        verdict=verdict,
    )


def _entry(
    eid: int,
    url: str,
    *,
    verdict: str = "nsfw",
    note: str = "",
    status: str = "",
) -> SimpleNamespace:
    """鸭子版复核条目:note / status 均可缺省(模拟属性不存在则不传)。"""
    entry = SimpleNamespace(id=eid, site_url=url, verdict=verdict, note=note)
    if status:
        entry.status = status  # 仅在显式给定时装上 status 属性
    return entry


def _three_groups() -> list[CaseGroup]:
    """三组战况:g1 大组(nsfw)、g2 单站组(clean)、g3 中组(suspect)。

    g1 与 g3 共享别名域 shared.cdn.net(跨组复现的"重复线索域")。
    """
    return [
        _case(
            "a.example.com",
            urls=[
                "https://a.example.com/",
                "https://www.a.example.com/x",
                "http://cdn.a.example.com:8080/y",
            ],
            aliases=["a.example.com", "www.a.example.com", "shared.cdn.net"],
            agg=0.92,
            verdict="nsfw",
            ids=[1, 2, 3],
        ),
        _case(
            "b.example.net",
            urls=["https://b.example.net/"],
            aliases=["b.example.net"],
            agg=0.1,
            verdict="clean",
            ids=[4],
        ),
        _case(
            "c.example.org",
            urls=["https://c.example.org/", "https://mirror.c.example.org/z"],
            aliases=["c.example.org", "mirror.c.example.org", "shared.cdn.net"],
            agg=0.55,
            verdict="suspect",
            ids=[5, 6],
        ),
    ]


# ---------------------------------------------------------------------------
# stats:全字段 / Top5 / verdict_dist / 空输入
# ---------------------------------------------------------------------------


def test_stats_three_groups_all_fields() -> None:
    """三组构造:七个键全量断言(计数 / 总 URL / Top5 / 分布 / 域排行)。"""
    report = stats(_three_groups())
    assert set(report) == STATS_KEYS  # 键集合恰为契约七键,不多不少
    assert report["group_count"] == 3
    assert report["single_site_groups"] == 1  # 仅 b.example.net(1 个别名域)
    assert report["multi_site_groups"] == 2
    assert report["total_urls"] == 6  # 3 + 1 + 2
    assert report["max_group_top5"] == [
        {"name": "a.example.com", "urls": 3, "agg_max": 0.92},
        {"name": "c.example.org", "urls": 2, "agg_max": 0.55},
        {"name": "b.example.net", "urls": 1, "agg_max": 0.1},
    ]
    assert report["verdict_dist"] == {"clean": 1, "suspect": 1, "nsfw": 1}
    assert report["alias_top5"] == [{"host": "shared.cdn.net", "groups": 2}]


def test_stats_top5_truncation_with_seven_groups() -> None:
    """7 个组(1..7 条 URL)→ max_group_top5 截断为 5,按 URL 数降序。"""
    groups = [
        _duck(name=f"g{i}.test", urls=[f"https://g{i}.test/p{j}" for j in range(i)])
        for i in range(1, 8)
    ]
    top5 = stats(groups)["max_group_top5"]
    assert len(top5) == 5
    assert [row["urls"] for row in top5] == [7, 6, 5, 4, 3]
    assert [row["name"] for row in top5] == ["g7.test", "g6.test", "g5.test", "g4.test", "g3.test"]
    # 每行结构固定三键。
    assert all(set(row) == {"name", "urls", "agg_max"} for row in top5)


def test_stats_top5_tiebreaks_agg_then_name() -> None:
    """URL 数并列 → agg_max 降序;再并列 → 组名升序(结果确定)。"""
    groups = [
        _duck(name="b.com", urls=["https://b.com/1", "https://b.com/2"], agg=0.5),
        _duck(name="c.com", urls=["https://c.com/1", "https://c.com/2"], agg=0.9),
        _duck(name="a.com", urls=["https://a.com/1", "https://a.com/2"], agg=0.9),
    ]
    top = stats(groups)["max_group_top5"]
    assert [row["name"] for row in top] == ["a.com", "c.com", "b.com"]
    assert [row["agg_max"] for row in top] == [0.9, 0.9, 0.5]


def test_stats_verdict_dist_enum_compatible_and_unknown_skipped() -> None:
    """verdict 兼容 Verdict 枚举;未知判定不计入分布但组数照计。"""
    groups = [
        _duck("a.com", ["https://a.com/"], verdict=Verdict.NSFW),
        _duck("b.com", ["https://b.com/"], verdict=Verdict.SUSPECT),
        _duck("c.com", ["https://c.com/"], verdict=Verdict.CLEAN),
        _duck("d.com", ["https://d.com/"], verdict="非法档位"),
        _duck("e.com", ["https://e.com/"], verdict="NSFW"),  # 大小写归一
    ]
    report = stats(groups)
    assert report["group_count"] == 5
    assert report["verdict_dist"] == {"clean": 1, "suspect": 1, "nsfw": 2}


def test_stats_empty_and_none_inputs() -> None:
    """空组列表 / None:零战况;verdict_dist 三键恒在且全 0。"""
    for empty in ([], None):
        report = stats(empty)
        assert report["group_count"] == 0
        assert report["single_site_groups"] == 0
        assert report["multi_site_groups"] == 0
        assert report["total_urls"] == 0
        assert report["max_group_top5"] == []
        assert report["verdict_dist"] == {"clean": 0, "suspect": 0, "nsfw": 0}
        assert report["alias_top5"] == []


def test_stats_alias_top5_ranking_cap_and_single_excluded() -> None:
    """重复线索域:h×3 组排首位,2 组的按域名升序,单组出现的不入榜,Top5 截断。"""
    groups = [
        _duck("g1.test", ["https://g1.test/"], aliases=["h1.net", "h2.net", "h7.net", "h8.net"]),
        _duck("g2.test", ["https://g2.test/"], aliases=["h1.net", "h2.net", "h3.net", "h8.net"]),
        _duck("g3.test", ["https://g3.test/"], aliases=["h1.net", "h4.net", "h5.net"]),
        _duck("g4.test", ["https://g4.test/"], aliases=["h3.net", "h4.net", "h5.net", "h6.net"]),
    ]
    top5 = stats(groups)["alias_top5"]
    # h1×3 组第一;2 组者按域名升序:h2,h3,h4,h5,h8;h6/h7 仅 1 组不入榜。
    assert top5 == [
        {"host": "h1.net", "groups": 3},
        {"host": "h2.net", "groups": 2},
        {"host": "h3.net", "groups": 2},
        {"host": "h4.net", "groups": 2},
        {"host": "h5.net", "groups": 2},
    ]
    assert len(top5) == 5  # 恰好截断(h8 同为 2 组被挤出)


def test_stats_alias_top5_empty_without_cross_group_repeat() -> None:
    """各组别名域互不重叠 → 无重复线索,alias_top5 为空。"""
    groups = [
        _duck("a.com", ["https://a.com/"], aliases=["a.com", "www.a.com"]),
        _duck("b.net", ["https://b.net/"], aliases=["b.net"]),
    ]
    assert stats(groups)["alias_top5"] == []


def test_stats_duck_tolerance_missing_fields() -> None:
    """缺字段 / 类型变体:缺 aliases 按单站计、缺 agg_max 按 0.0、
    缺 verdict 不入分布、site_urls 为 tuple 可统计。"""
    bare = SimpleNamespace(name="bare.test", site_urls=("https://bare.test/1", "https://bare.test/2"))
    noname = SimpleNamespace(
        site_urls=["https://x.test/"], aliases=["x.test", "www.x.test"], verdict=Verdict.NSFW
    )
    report = stats([bare, noname])
    assert report["group_count"] == 2
    assert report["single_site_groups"] == 1  # bare 无 aliases(0 域)→ 单站
    assert report["multi_site_groups"] == 1  # noname 两个别名域
    assert report["total_urls"] == 3
    assert report["verdict_dist"] == {"clean": 0, "suspect": 0, "nsfw": 1}
    top = report["max_group_top5"]
    assert top[0] == {"name": "bare.test", "urls": 2, "agg_max": 0.0}  # tuple URLs 可统计
    assert top[1] == {"name": "", "urls": 1, "agg_max": 0.0}  # noname 缺名 / 缺 agg


def test_stats_determinism_under_reordering() -> None:
    """乱序输入(各键互异时)→ 战况字典完全一致。"""
    groups = _three_groups()
    assert stats(groups) == stats(list(reversed(groups)))


# ---------------------------------------------------------------------------
# export_csv:写盘可读回(BOM / 列序 / 行数)
# ---------------------------------------------------------------------------


def test_export_csv_bom_and_header(tmp_path: object) -> None:
    """utf-8-sig:BOM 字节开头;表头列序与契约一致。"""
    path = tmp_path / "groups.csv"  # type: ignore[operator]
    export_csv(_three_groups(), path)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # Excel 双击不乱码的 BOM
    text = raw.decode("utf-8-sig")
    rows = list(csv.reader(text.splitlines()))
    assert rows[0] == list(CSV_HEADER) == ["组名", "站点数", "URL数", "判定", "agg_max", "aliases"]


def test_export_csv_row_count_return_and_content(tmp_path: object) -> None:
    """返回值=数据行数(不含表头);逐单元格:站点数=别名域数、URL 数、
    判定、agg_max、aliases 分号拼接。"""
    groups = [
        _case(
            "a.example.com",
            urls=["https://a.example.com/", "https://a.example.com/p2", "https://www.a.example.com/x"],
            aliases=["a.example.com", "www.a.example.com"],
            agg=0.92,
            verdict="nsfw",
        ),
        _case("b.example.net", ["https://b.example.net/"], ["b.example.net"], 0.1, "clean"),
    ]
    path = tmp_path / "out.csv"  # type: ignore[operator]
    written = export_csv(groups, path)
    assert written == 2
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert len(rows) == 3  # 表头 + 2 行
    assert rows[1] == ["a.example.com", "2", "3", "nsfw", "0.92", "a.example.com;www.a.example.com"]
    assert rows[2] == ["b.example.net", "1", "1", "clean", "0.1", "b.example.net"]


def test_export_csv_empty_groups_header_only(tmp_path: object) -> None:
    """空组列表:仅写表头,返回 0;文件仍可被 csv 正常读回。"""
    path = tmp_path / "empty.csv"  # type: ignore[operator]
    assert export_csv([], path) == 0
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows == [list(CSV_HEADER)]
    assert export_csv(None, path) == 0  # None 同空列表


def test_export_csv_readback_full_matrix_and_duck(tmp_path: object) -> None:
    """csv 模块 + utf-8-sig 读回全矩阵一致;鸭子缺 aliases → 空串格。"""
    groups = [
        _duck("a.com", ["https://a.com/"], ["a.com", "www.a.com"], 0.5, Verdict.SUSPECT),
        SimpleNamespace(name="b.com", site_urls=["https://b.com/1", "https://b.com/2"], verdict="nsfw"),
    ]
    path = tmp_path / "duck.csv"  # type: ignore[operator]
    assert export_csv(groups, path) == 2
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == list(CSV_HEADER)
    # 枚举判定归一小写;站点数/URL数/agg_max 齐全。
    assert rows[1] == ["a.com", "2", "1", "suspect", "0.5", "a.com;www.a.com"]
    # 缺 aliases / agg_max 的鸭子:站点数 0、agg_max 0.0、aliases 空格。
    assert rows[2] == ["b.com", "0", "2", "nsfw", "0.0", ""]


def test_export_csv_creates_parent_directory(tmp_path: object) -> None:
    """目标父目录不存在时自动创建(str 路径同样可写)。"""
    path = tmp_path / "nested" / "deep" / "g.csv"  # type: ignore[operator]
    assert export_csv(_three_groups(), str(path)) == 3
    assert path.exists()


# ---------------------------------------------------------------------------
# repeat_offenders:命中 / 去重 / 空态 / 鸭子容错
# ---------------------------------------------------------------------------


def _two_groups() -> list[CaseGroup]:
    return [
        _case(
            "a.example.com",
            urls=["https://a.example.com/", "https://www.a.example.com/x"],
            aliases=["a.example.com", "www.a.example.com"],
            agg=0.9,
            verdict="nsfw",
        ),
        _case("b.example.net", urls=["https://b.example.net/"], aliases=["b.example.net"], agg=0.2, verdict="suspect"),
    ]


def test_repeat_offenders_hits_note_and_status() -> None:
    """note 含 [rejected] 与 status=rejected 两条通路都命中,reason 为中文。"""
    entries = [
        _entry(1, "https://www.a.example.com/later", note="证据不足 [rejected] 已驳回"),
        _entry(2, "https://b.example.net/", status="rejected"),
        _entry(3, "https://a.example.com/", note="正常备注", status="approved"),
    ]
    results = repeat_offenders(entries, _two_groups())
    assert len(results) == 2
    assert results[0]["site_url"] == "https://www.a.example.com/later"
    assert results[0]["group_name"] == "a.example.com"
    assert results[1]["site_url"] == "https://b.example.net/"
    assert results[1]["group_name"] == "b.example.net"
    for item in results:
        assert set(item) == {"site_url", "group_name", "reason"}
        assert "驳回" in item["reason"]  # 中文依据必含"驳回"
    # 两种信号来源在 reason 中可区分。
    assert "[rejected]" in results[0]["reason"]
    assert "rejected" in results[1]["reason"]


def test_repeat_offenders_dedupe_same_site_and_group() -> None:
    """(site_url, group_name) 去重:同站同组重复条目只报一次。"""
    dup1 = _entry(1, "https://a.example.com/again", note="x [rejected] y")
    dup2 = _entry(2, "https://a.example.com/again", note="[rejected] 重复线索")
    other = _entry(3, "https://www.a.example.com/another", status="rejected")
    results = repeat_offenders([dup1, dup2, other], _two_groups())
    assert len(results) == 2
    assert {r["site_url"] for r in results} == {
        "https://a.example.com/again",
        "https://www.a.example.com/another",
    }
    assert all(r["group_name"] == "a.example.com" for r in results)


def test_repeat_offenders_empty_when_no_rejection() -> None:
    """无任何驳回信号 / 空条目 / 空组:一律空列表。"""
    groups = _two_groups()
    assert repeat_offenders([], groups) == []
    assert repeat_offenders([_entry(1, "https://a.example.com/", note="ok")], groups) == []
    rejected = _entry(1, "https://a.example.com/", status="rejected")
    assert repeat_offenders([rejected], []) == []
    assert repeat_offenders([rejected], None) == []
    assert repeat_offenders(None, groups) == []


def test_repeat_offenders_canonical_not_in_any_group() -> None:
    """驳回条目 canonical 不落任何组(跨可注册域)→ 不产出。"""
    entries = [
        _entry(1, "https://unrelated.example.io/", note="[rejected]"),
        _entry(2, "https://sub.other.example.io/deep", status="rejected"),
    ]
    assert repeat_offenders(entries, _two_groups()) == []


def test_repeat_offenders_duck_tolerance() -> None:
    """鸭子容错:缺 note/status 属性、note=None、site_url 缺失、组缺 site_urls。"""
    plain = SimpleNamespace(id=1, site_url="https://a.example.com/", verdict="nsfw")  # 无 note/status
    note_none = SimpleNamespace(id=2, site_url="https://a.example.com/", verdict="nsfw", note=None)
    no_url = SimpleNamespace(id=3, site_url="", verdict="nsfw", note="[rejected]")
    bare_group = SimpleNamespace(name="bare", aliases=["bare.test"])  # 无 site_urls
    assert repeat_offenders([plain, note_none, no_url], _two_groups()) == []
    assert repeat_offenders([_entry(1, "https://a.example.com/", note="[rejected]")], [bare_group]) == []


def test_repeat_offenders_status_enum_and_case_insensitive() -> None:
    """status 为枚举值 / 大小写混合 → 归一后仍判驳回。"""
    entries = [
        SimpleNamespace(id=1, site_url="https://a.example.com/", verdict="nsfw", note="", status="Rejected"),
    ]
    results = repeat_offenders(entries, _two_groups())
    assert len(results) == 1
    assert results[0]["group_name"] == "a.example.com"


def test_repeat_offenders_fallback_when_canonical_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A103 canonical 未就位(sys.modules 置 None)→ 内置兜底仍把
    www 子域折叠到同一可注册域,命中分组(双态确定)。"""
    monkeypatch.setitem(sys.modules, "netsentinel.intel.canonical", None)
    entries = [
        _entry(1, "https://www.a.example.com/reappear", status="rejected"),
        _entry(2, "http://a.example.com:8080/variant", note="[rejected] 复现"),
    ]
    results = repeat_offenders(entries, _two_groups())
    assert {r["site_url"] for r in results} == {
        "https://www.a.example.com/reappear",
        "http://a.example.com:8080/variant",
    }
    assert {r["group_name"] for r in results} == {"a.example.com"}


# ---------------------------------------------------------------------------
# 与 A104 真实产物 / 全家桶流水线兼容
# ---------------------------------------------------------------------------


def test_stats_pipeline_with_real_group_entries() -> None:
    """A104 group_entries 产物(真实 CaseGroup)直接进 stats:口径自洽。"""
    entries = [
        types.SimpleNamespace(id=1, site_url="https://www.alpha.test/a", verdict="nsfw", evidence_zip=""),
        types.SimpleNamespace(id=2, site_url="http://alpha.test:8080/b", verdict="clean", evidence_zip=""),
        types.SimpleNamespace(id=3, site_url="https://beta.test/", verdict="suspect", evidence_zip=""),
    ]
    groups = group_entries(entries, None)
    assert len(groups) == 2  # alpha.test 镜像归并 + beta.test
    report = stats(groups)
    assert report["group_count"] == 2
    assert report["total_urls"] == 3
    assert report["single_site_groups"] == 1  # beta.test 单别名域
    assert report["multi_site_groups"] == 1  # alpha.test(www + 裸域两个 host)
    assert report["verdict_dist"] == {"clean": 0, "suspect": 1, "nsfw": 1}


def test_stats_and_csv_on_same_groups_consistent(tmp_path: object) -> None:
    """stats 与 export_csv 同源同口径:total_urls == CSV 的 URL数列之和,
    group_count == CSV 数据行数。"""
    groups = _three_groups()
    report = stats(groups)
    path = tmp_path / "consistency.csv"  # type: ignore[operator]
    written = export_csv(groups, path)
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))[1:]
    assert written == report["group_count"] == len(rows)
    assert sum(int(r[2]) for r in rows) == report["total_urls"]
    assert sum(int(r[1]) >= 2 for r in rows) == report["multi_site_groups"]
    # 判定列与 verdict_dist 逐档一致。
    dist: dict[str, int] = {"clean": 0, "suspect": 0, "nsfw": 0}
    for r in rows:
        dist[r[3]] += 1
    assert dist == report["verdict_dist"]
