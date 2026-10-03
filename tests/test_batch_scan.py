"""A108 ``ops.batch_scan`` 单元测试(全 fake 注入,离线/零外呼)。

覆盖契约要点:
- ``batch_scan``:注入 pool_runner 的成功/部分失败透传 summary、run_scan
  透传、空列表、缺省依赖(orchestrator / ops.pool)未就位的中文
  RuntimeError、memory 透传;
- ``group_and_enqueue``:3 域 8 URL(含镜像)→ 3 组、note 前缀
  ``[组:组名]``、enqueued / skipped_clean 计数、CLEAN 组不入列、
  linker 注入合并两组 → 2 组、伪 entry 负数 id、合并报告字段
  (agg_max / 最严重 verdict / pages 并集)、bundle 调用次数 == 组数、
  空 reports 短路、兄弟模块(case_group / group_linker / packager /
  review_queue)缺失的中文 RuntimeError、旧式队列 add 不支持 note 的回退。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from netsentinel.contracts import Config, PageSample, SiteReport, Verdict
from netsentinel.ops import batch_scan as ns

# ---------------------------------------------------------------------------
# 公共 fake
# ---------------------------------------------------------------------------


def make_report(
    url: str,
    verdict: str = "nsfw",
    agg: float = 0.95,
    pages: int = 1,
) -> SiteReport:
    """构造最小可用 SiteReport(纯内存,不落盘)。"""
    page_list = [
        PageSample(url=f"{url}/p{i}", screenshot_path="") for i in range(pages)
    ]
    return SiteReport(
        site_url=url,
        pages=page_list,
        agg_nsw_prob=agg,
        nsw_image_count=0,
        verdict=Verdict(verdict),
        needs_review=verdict != "clean",
    )


@dataclass
class FakeGroup:
    """A104 CaseGroup 的鸭子替身(字段按契约同名同义)。"""

    name: str
    aliases: list[str] = field(default_factory=list)
    entry_ids: list[int] = field(default_factory=list)
    site_urls: list[str] = field(default_factory=list)
    agg_max: float = 0.0
    verdict: str = "clean"
    image_sha_set: set[str] = field(default_factory=set)
    created_at: str = ""


def host_key(url: str) -> str:
    """测试用"可注册域"近似:hostname 去端口后取最后两段。"""
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def fake_grouper(entries: list[Any], reports_by_id: dict[int, Any]) -> list[FakeGroup]:
    """默认 fake grouper:按可注册域把伪 entries 聚成基础组。"""
    by_key: dict[str, FakeGroup] = {}
    ordered: list[FakeGroup] = []
    for entry in entries:
        key = host_key(entry.site_url)
        group = by_key.get(key)
        if group is None:
            group = FakeGroup(name=key)
            by_key[key] = group
            ordered.append(group)
        group.entry_ids.append(entry.id)
        group.site_urls.append(entry.site_url)
        group.aliases.append(entry.site_url)
        report = reports_by_id.get(entry.id)
        if ns._severity(entry.verdict) > ns._severity(group.verdict):
            group.verdict = entry.verdict
        group.agg_max = max(group.agg_max, float(getattr(report, "agg_nsw_prob", 0.0)))
    return ordered


class FakePackager:
    """记录调用并返回带 zip_path 的伪 bundle。"""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def __call__(self, report: Any, cfg: Any) -> Any:
        self.calls.append(report)
        return SimpleNamespace(zip_path=f"evidence/group_{len(self.calls)}.zip")


class FakeQueue:
    """支持 note 关键字的伪复核队列。"""

    def __init__(self) -> None:
        self.added: list[tuple[Any, str, Any]] = []

    def add(self, report: Any, evidence_zip: str = "", note: Any = None) -> int:
        self.added.append((report, evidence_zip, note))
        return len(self.added)


class LegacyQueue:
    """旧式 add 签名(无 note 参数),触发回退分支。"""

    def __init__(self) -> None:
        self.added: list[tuple[Any, str]] = []

    def add(self, report: Any, evidence_zip: str = "") -> int:
        self.added.append((report, evidence_zip))
        return len(self.added)


def make_pool_runner(
    scan_table: dict[str, Any] | None = None,
    fail_urls: set[str] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """造一个复刻 run_pool 汇总语义的 fake 池,返回 (runner, 记录器)。"""
    records: dict[str, Any] = {"calls": [], "cfg": None, "memory": None,
                               "run_scan": None}

    def runner(cfg, items, *, run_scan=None, memory=None, **kwargs):
        records["calls"].append(list(items))
        records["cfg"] = cfg
        records["memory"] = memory
        records["run_scan"] = run_scan
        summary: dict[str, Any] = {
            "total": len(items), "done": 0, "failed": 0, "skipped": 0,
            "results": {}, "errors": [],
        }
        for url in items:
            if fail_urls and url in fail_urls:
                summary["failed"] += 1
                summary["errors"].append((url, f"扫描失败(测试注入):{url}"))
                continue
            report = run_scan(url, cfg)
            summary["done"] += 1
            summary["results"][url] = ns._verdict_str(getattr(report, "verdict", ""))
        return summary

    return runner, records


#: 3 域 8 URL 场景(example.com 主站+www+手机站;b2.net 主站+www;
#: c3.org 主站+镜像+8080 端口变体)——镜像应归并进同一基础组。
URLS_3D8U = [
    "https://a1.example.com/",
    "https://www.a1.example.com/",
    "https://m.a1.example.com/x",
    "https://b2.net/",
    "https://www.b2.net/",
    "https://c3.org/",
    "https://mirror.c3.org/",
    "https://c3.org:8080/y",
]


def reports_for_urls(urls: list[str], verdict: str = "nsfw", agg: float = 0.9) -> dict:
    return {url: make_report(url, verdict=verdict, agg=agg) for url in urls}


# ---------------------------------------------------------------------------
# batch_scan
# ---------------------------------------------------------------------------


def test_batch_scan_success_all_urls_in_reports() -> None:
    """全部成功:每个 URL 的报告对象都进 reports(对象同一性)。"""
    reports_src = reports_for_urls(URLS_3D8U[:3])
    runner, _ = make_pool_runner()
    cfg = Config()
    result = ns.batch_scan(URLS_3D8U[:3], cfg, run_scan=lambda u, c: reports_src[u],
                           pool_runner=runner)
    assert set(result["reports"]) == set(URLS_3D8U[:3])
    for url, report in result["reports"].items():
        assert report is reports_src[url]
    assert result["summary"]["done"] == 3
    assert result["summary"]["failed"] == 0


def test_batch_scan_summary_object_passthrough() -> None:
    """summary 是 pool 摘要 dict 的原样透传(同一对象)。"""
    runner, _ = make_pool_runner()
    cfg = Config()
    scan = lambda u, c: make_report(u)  # noqa: E731
    result = ns.batch_scan(["https://x.example.com/"], cfg, run_scan=scan,
                           pool_runner=runner)
    assert result["summary"]["total"] == 1
    assert result["summary"]["results"] == {"https://x.example.com/": "nsfw"}


def test_batch_scan_run_scan_passthrough_called_with_urls_and_cfg() -> None:
    """run_scan 被透传:(url, cfg) 逐 URL 调用,cfg 是同一对象。"""
    seen: list[tuple[str, Any]] = []

    def scan(url: str, cfg: Any) -> SiteReport:
        seen.append((url, cfg))
        return make_report(url)

    runner, _ = make_pool_runner()
    cfg = Config()
    urls = ["https://a.example.com/", "https://b.example.com/"]
    ns.batch_scan(urls, cfg, run_scan=scan, pool_runner=runner)
    assert [u for u, _ in seen] == urls
    assert all(c is cfg for _, c in seen)


def test_batch_scan_memory_and_urls_passthrough_to_runner() -> None:
    """memory 与 URL 列表原样到达 pool_runner;run_scan 已被包裹成可调用。"""
    runner, records = make_pool_runner()
    cfg = Config()
    memory = SimpleNamespace(label="mem")
    ns.batch_scan(["https://a.example.com/"], cfg,
                  run_scan=lambda u, c: make_report(u),
                  memory=memory, pool_runner=runner)
    assert records["memory"] is memory
    assert records["cfg"] is cfg
    assert records["calls"] == [["https://a.example.com/"]]
    assert callable(records["run_scan"])


def test_batch_scan_partial_failure_not_in_reports_but_in_errors() -> None:
    """部分失败:失败 URL 不在 reports,但出现在 summary.errors。"""
    good = "https://good.example.com/"
    bad = "https://bad.example.com/"
    runner, _ = make_pool_runner(fail_urls={bad})
    cfg = Config()
    result = ns.batch_scan([good, bad], cfg,
                           run_scan=lambda u, c: make_report(u),
                           pool_runner=runner)
    assert set(result["reports"]) == {good}
    assert result["summary"]["failed"] == 1
    assert result["summary"]["errors"] == [(bad, f"扫描失败(测试注入):{bad}")]
    assert result["summary"]["done"] == 1


def test_batch_scan_empty_urls() -> None:
    """空 URL 列表:reports 为空,summary 透传 fake 池汇总。"""
    runner, _ = make_pool_runner()
    result = ns.batch_scan([], Config(), run_scan=lambda u, c: make_report(u),
                           pool_runner=runner)
    assert result["reports"] == {}
    assert result["summary"]["total"] == 0


def test_batch_scan_pool_module_missing_cn_runtimeerror(monkeypatch) -> None:
    """缺省 pool_runner 依赖 ops.pool 未就位 → 中文 RuntimeError 指明模块。"""
    monkeypatch.setitem(sys.modules, "netsentinel.ops.pool", None)
    with pytest.raises(RuntimeError) as ei:
        ns.batch_scan(["https://a.example.com/"], Config(),
                      run_scan=lambda u, c: make_report(u))
    assert "netsentinel.ops.pool" in str(ei.value)
    assert "未就位" in str(ei.value)


def test_batch_scan_orchestrator_missing_cn_runtimeerror(monkeypatch) -> None:
    """run_scan 未注入且 orchestrator 未就位 → 中文 RuntimeError 指明模块。"""
    monkeypatch.setitem(sys.modules, "netsentinel.pipeline.orchestrator", None)
    with pytest.raises(RuntimeError) as ei:
        ns.batch_scan(["https://a.example.com/"], Config(),
                      pool_runner=make_pool_runner()[0])
    assert "netsentinel.pipeline.orchestrator" in str(ei.value)
    assert "未就位" in str(ei.value)


# ---------------------------------------------------------------------------
# group_and_enqueue:归组 / 入列主链路
# ---------------------------------------------------------------------------


def test_three_domains_eight_urls_into_three_groups() -> None:
    """3 域 8 URL(镜像)→ 3 组;组内 URL 数 3/2/3;全部入列。"""
    reports = {
        "https://a1.example.com/": make_report("https://a1.example.com/", agg=0.95),
        "https://www.a1.example.com/": make_report("https://www.a1.example.com/", agg=0.90),
        "https://m.a1.example.com/x": make_report("https://m.a1.example.com/x", agg=0.80),
        "https://b2.net/": make_report("https://b2.net/", agg=0.70),
        "https://www.b2.net/": make_report("https://www.b2.net/", agg=0.60),
        "https://c3.org/": make_report("https://c3.org/", agg=0.88),
        "https://mirror.c3.org/": make_report("https://mirror.c3.org/", agg=0.85),
        "https://c3.org:8080/y": make_report("https://c3.org:8080/y", agg=0.82),
    }
    queue = FakeQueue()
    packager = FakePackager()
    result = ns.group_and_enqueue(Config(), reports=reports, queue=queue,
                                  packager=packager, grouper=fake_grouper,
                                  linker=lambda groups, **kw: list(groups))
    names = {g.name for g in result["groups"]}
    assert names == {"example.com", "b2.net", "c3.org"}
    sizes = sorted(len(g.site_urls) for g in result["groups"])
    assert sizes == [2, 3, 3]
    assert result["enqueued"] == 3
    assert result["skipped_clean"] == 0
    assert len(queue.added) == 3


def test_note_prefix_contains_group_name() -> None:
    """入列 note 前缀必须是 "[组:组名]"。"""
    reports = reports_for_urls(["https://x.example.com/", "https://www.x.example.com/"])
    queue = FakeQueue()
    result = ns.group_and_enqueue(Config(), reports=reports, queue=queue,
                                  packager=FakePackager(), grouper=fake_grouper,
                                  linker=lambda groups, **kw: list(groups))
    assert result["enqueued"] == 1
    _, zip_path, note = queue.added[0]
    assert note == "[组:example.com]"
    assert zip_path  # 证据包路径非空


def test_queue_receives_merged_report_and_zip() -> None:
    """队列收到的是合并报告(SiteReport)与 packager 返回的 zip 路径。"""
    reports = reports_for_urls(["https://x.example.com/"])
    queue = FakeQueue()
    packager = FakePackager()
    ns.group_and_enqueue(Config(), reports=reports, queue=queue,
                         packager=packager, grouper=fake_grouper,
                         linker=lambda groups, **kw: list(groups))
    report, zip_path, _ = queue.added[0]
    assert isinstance(report, SiteReport)
    assert zip_path == "evidence/group_1.zip"
    assert report.needs_review is True


def test_clean_group_not_enqueued_counted_skipped() -> None:
    """CLEAN 组(agg 低于复核阈值)不入列、不打包,计入 skipped_clean。"""
    reports = {
        "https://bad.example.com/": make_report("https://bad.example.com/", "nsfw", 0.95),
        "https://ok.example.net/": make_report("https://ok.example.net/", "clean", 0.10),
    }
    queue = FakeQueue()
    packager = FakePackager()
    result = ns.group_and_enqueue(Config(), reports=reports, queue=queue,
                                  packager=packager, grouper=fake_grouper,
                                  linker=lambda groups, **kw: list(groups))
    assert result["enqueued"] == 1
    assert result["skipped_clean"] == 1
    assert len(result["groups"]) == 2
    assert len(packager.calls) == 1  # clean 组不打包
    enqueued_urls = {r.site_url for r, _, _ in queue.added}
    assert enqueued_urls == {"https://bad.example.com/"}


def test_clean_verdict_with_high_agg_still_enqueued() -> None:
    """verdict=clean 但 agg ≥ review_threshold 的组仍需人工复核。"""
    cfg = Config(review_threshold=0.5)
    reports = {"https://edge.example.com/": make_report("https://edge.example.com/",
                                                        "clean", 0.66)}
    queue = FakeQueue()
    result = ns.group_and_enqueue(cfg, reports=reports, queue=queue,
                                  packager=FakePackager(), grouper=fake_grouper,
                                  linker=lambda groups, **kw: list(groups))
    assert result["enqueued"] == 1
    assert result["skipped_clean"] == 0


def test_linker_injection_merges_two_groups() -> None:
    """linker 注入:把两个基础组合并 → 总组数 2,enqueued 2。"""

    def merging_linker(groups: list[Any], **kwargs: Any) -> list[Any]:
        merged = FakeGroup(
            name=groups[0].name,
            aliases=groups[0].aliases + groups[1].aliases,
            entry_ids=groups[0].entry_ids + groups[1].entry_ids,
            site_urls=groups[0].site_urls + groups[1].site_urls,
            agg_max=max(groups[0].agg_max, groups[1].agg_max),
            verdict="nsfw" if "nsfw" in (groups[0].verdict, groups[1].verdict)
            else groups[0].verdict,
        )
        return [merged] + list(groups[2:])

    reports = reports_for_urls(URLS_3D8U)
    queue = FakeQueue()
    result = ns.group_and_enqueue(Config(), reports=reports, queue=queue,
                                  packager=FakePackager(), grouper=fake_grouper,
                                  linker=merging_linker)
    assert len(result["groups"]) == 2
    assert result["enqueued"] == 2
    big = max(result["groups"], key=lambda g: len(g.site_urls))
    assert len(big.site_urls) == 5  # example.com + b2.net 两组并成一组
    assert result["groups"][0].name in {"example.com", "b2.net"}


def test_linker_receives_base_groups_and_cfg_thresholds() -> None:
    """linker 收到基础组列表与 cfg 的 V6 归并参数。"""
    seen: dict[str, Any] = {}

    def spy_linker(groups: list[Any], **kwargs: Any) -> list[Any]:
        seen["n_base"] = len(groups)
        seen.update(kwargs)
        return list(groups)

    cfg = Config(group_merge_phash_overlap=0.42, group_merge_template=False)
    ns.group_and_enqueue(cfg, reports=reports_for_urls(URLS_3D8U),
                         queue=FakeQueue(), packager=FakePackager(),
                         grouper=fake_grouper, linker=spy_linker)
    assert seen["n_base"] == 3
    assert seen["overlap_threshold"] == pytest.approx(0.42)
    assert seen["merge_template"] is False


def test_duck_cfg_without_v6_fields_uses_defaults() -> None:
    """鸭子 cfg 缺 V6 字段:linker 参数按缺省 0.3 / True 兜底。"""
    seen: dict[str, Any] = {}

    def spy_linker(groups: list[Any], **kwargs: Any) -> list[Any]:
        seen.update(kwargs)
        return list(groups)

    duck_cfg = SimpleNamespace(review_threshold=0.5)
    ns.group_and_enqueue(duck_cfg, reports=reports_for_urls(["https://a.example.com/"]),
                         queue=FakeQueue(), packager=FakePackager(),
                         grouper=fake_grouper, linker=spy_linker)
    assert seen["overlap_threshold"] == pytest.approx(0.3)
    assert seen["merge_template"] is True


def test_bundle_call_count_equals_group_count() -> None:
    """全部需复核时,packager 被调次数 == 组数。"""
    reports = reports_for_urls(URLS_3D8U)
    packager = FakePackager()
    result = ns.group_and_enqueue(Config(), reports=reports, queue=FakeQueue(),
                                  packager=packager, grouper=fake_grouper,
                                  linker=lambda groups, **kw: list(groups))
    assert len(packager.calls) == len(result["groups"]) == 3


def test_merged_report_fields_agg_verdict_pages() -> None:
    """合并报告:agg 取组内最大、verdict 取最严重、pages 取全体成员并集、
    主站取组内最严重成员。"""
    reports = {
        "https://a.example.com/": make_report("https://a.example.com/", "suspect", 0.55, pages=2),
        "https://b.example.com/": make_report("https://b.example.com/", "nsfw", 0.97, pages=3),
        "https://c.example.com/": make_report("https://c.example.com/", "clean", 0.10, pages=1),
    }
    queue = FakeQueue()
    ns.group_and_enqueue(Config(), reports=reports, queue=queue,
                         packager=FakePackager(), grouper=fake_grouper,
                         linker=lambda groups, **kw: list(groups))
    merged = queue.added[0][0]
    assert isinstance(merged, SiteReport)
    assert merged.site_url == "https://b.example.com/"  # 最严重成员作主站
    assert merged.verdict is Verdict.NSFW
    assert merged.agg_nsw_prob == pytest.approx(0.97)
    assert len(merged.pages) == 6  # 2 + 3 + 1 全并集
    assert merged.intel["group_name"] == "example.com"
    assert merged.intel["queue_note"] == "[组:example.com]"


def test_pseudo_entries_negative_ids_and_report_mapping() -> None:
    """grouper 收到的伪 entries:id 全负且唯一、verdict 为字符串档位,
    reports_by_id 按 id 映射回原报告对象。"""
    seen: dict[str, Any] = {}

    def spy_grouper(entries: list[Any], reports_by_id: dict[int, Any]) -> list[Any]:
        seen["entries"] = entries
        seen["by_id"] = reports_by_id
        return fake_grouper(entries, reports_by_id)

    reports = reports_for_urls(["https://a.example.com/", "https://b.example.net/"])
    ns.group_and_enqueue(Config(), reports=reports, queue=FakeQueue(),
                         packager=FakePackager(), grouper=spy_grouper,
                         linker=lambda groups, **kw: list(groups))
    entries = seen["entries"]
    assert [e.id for e in entries] == [-1, -2]
    assert {e.site_url for e in entries} == set(reports)
    assert all(isinstance(e.verdict, str) and e.verdict == "nsfw" for e in entries)
    for entry in entries:
        assert seen["by_id"][entry.id] is reports[entry.site_url]


def test_empty_reports_short_circuits() -> None:
    """空 reports:返回空结果,不触发 grouper(不触碰兄弟模块)。"""
    calls: list[int] = []

    def boom_grouper(entries: list[Any], by_id: dict[int, Any]) -> list[Any]:
        calls.append(1)
        return []

    result = ns.group_and_enqueue(Config(), reports={}, queue=FakeQueue(),
                                  packager=FakePackager(), grouper=boom_grouper,
                                  linker=lambda groups, **kw: list(groups))
    assert result == {"groups": [], "enqueued": 0, "skipped_clean": 0}
    assert calls == []


def test_reports_none_defaults_to_empty() -> None:
    """reports=None 与空字典同义。"""
    result = ns.group_and_enqueue(Config(), queue=FakeQueue(),
                                  packager=FakePackager(), grouper=fake_grouper,
                                  linker=lambda groups, **kw: list(groups))
    assert result["groups"] == []
    assert result["enqueued"] == 0


def test_legacy_queue_without_note_kwarg_falls_back() -> None:
    """队列 add 不支持 note 关键字:回退两参调用,仍成功入列。"""
    reports = reports_for_urls(["https://x.example.com/"])
    queue = LegacyQueue()
    result = ns.group_and_enqueue(Config(), reports=reports, queue=queue,
                                  packager=FakePackager(), grouper=fake_grouper,
                                  linker=lambda groups, **kw: list(groups))
    assert result["enqueued"] == 1
    assert len(queue.added) == 1
    report, zip_path = queue.added[0]
    assert report.intel["queue_note"] == "[组:example.com]"  # 组名仍留痕
    assert zip_path


def test_groups_returned_are_linker_output_objects() -> None:
    """返回的 groups 就是 linker 输出的原对象(同一性)。"""
    linker_groups: list[Any] = []

    def identity_linker(groups: list[Any], **kwargs: Any) -> list[Any]:
        linker_groups.extend(groups)
        return list(groups)

    reports = reports_for_urls(["https://a.example.com/"])
    result = ns.group_and_enqueue(Config(), reports=reports, queue=FakeQueue(),
                                  packager=FakePackager(), grouper=fake_grouper,
                                  linker=identity_linker)
    assert result["groups"][0] is linker_groups[0]


# ---------------------------------------------------------------------------
# group_and_enqueue:兄弟模块未就位(中文 RuntimeError)
# ---------------------------------------------------------------------------


def test_grouper_sibling_missing_cn_runtimeerror(monkeypatch) -> None:
    """A104 case_group 未就位 → 中文 RuntimeError 指明模块。"""
    monkeypatch.setitem(sys.modules, "netsentinel.intel.case_group", None)
    with pytest.raises(RuntimeError) as ei:
        ns.group_and_enqueue(Config(), reports=reports_for_urls(["https://a.example.com/"]),
                             queue=FakeQueue(), packager=FakePackager(),
                             linker=lambda groups, **kw: list(groups))
    assert "netsentinel.intel.case_group" in str(ei.value)
    assert "未就位" in str(ei.value)


def test_linker_sibling_missing_cn_runtimeerror(monkeypatch) -> None:
    """A105 group_linker 未就位 → 中文 RuntimeError 指明模块。"""
    monkeypatch.setitem(sys.modules, "netsentinel.intel.group_linker", None)
    with pytest.raises(RuntimeError) as ei:
        ns.group_and_enqueue(Config(), reports=reports_for_urls(["https://a.example.com/"]),
                             queue=FakeQueue(), packager=FakePackager(),
                             grouper=fake_grouper)
    assert "netsentinel.intel.group_linker" in str(ei.value)
    assert "未就位" in str(ei.value)


def test_packager_sibling_missing_cn_runtimeerror(monkeypatch) -> None:
    """A11 evidence.packager 未就位 → 中文 RuntimeError 指明模块。"""
    monkeypatch.setitem(sys.modules, "netsentinel.evidence.packager", None)
    with pytest.raises(RuntimeError) as ei:
        ns.group_and_enqueue(Config(), reports=reports_for_urls(["https://a.example.com/"]),
                             queue=FakeQueue(), grouper=fake_grouper,
                             linker=lambda groups, **kw: list(groups))
    assert "netsentinel.evidence.packager" in str(ei.value)
    assert "未就位" in str(ei.value)


def test_queue_sibling_missing_cn_runtimeerror(monkeypatch) -> None:
    """decision.review_queue 未就位 → 中文 RuntimeError 指明模块。"""
    monkeypatch.setitem(sys.modules, "netsentinel.decision.review_queue", None)
    with pytest.raises(RuntimeError) as ei:
        ns.group_and_enqueue(Config(), reports=reports_for_urls(["https://a.example.com/"]),
                             packager=FakePackager(), grouper=fake_grouper,
                             linker=lambda groups, **kw: list(groups))
    assert "netsentinel.decision.review_queue" in str(ei.value)
    assert "未就位" in str(ei.value)


def test_queue_without_add_method_cn_runtimeerror() -> None:
    """注入的队列没有 add 方法 → 中文 RuntimeError(鸭子契约校验)。"""
    reports = reports_for_urls(["https://x.example.com/"])
    with pytest.raises(RuntimeError) as ei:
        ns.group_and_enqueue(Config(), reports=reports, queue=object(),
                             packager=FakePackager(), grouper=fake_grouper,
                             linker=lambda groups, **kw: list(groups))
    assert "add" in str(ei.value)


def test_group_without_members_still_enqueues_with_fallback_url() -> None:
    """组内 site_urls 与 reports 对不上(空组):仍可入列,主站回退组名。"""
    lonely = FakeGroup(name="ghost.example.com", verdict="suspect", agg_max=0.6)
    result = ns.group_and_enqueue(Config(), reports=reports_for_urls(["https://a.example.com/"]),
                                  queue=FakeQueue(), packager=FakePackager(),
                                  grouper=lambda e, b: [lonely],
                                  linker=lambda groups, **kw: list(groups))
    assert result["enqueued"] == 1
