# -*- coding: utf-8 -*-
"""A118 单元测试:分组质量基准(全程离线,只写 tmp_path)。

覆盖点:
- 合成语料:规模 / 标签结构(镜像同标、团伙对共享标签)/ URL 全部合法
  http(s) 且唯一 / canonical 语义(同站同 canonical、异站异 canonical、
  团伙对跨 canonical)/ seed 决定性 / 自定义参数 / 非法参数中文报错;
- 指纹线索:团伙对 Jaccard = 0.5 ≥ 阈值;异站互斥、同站镜像同指纹;
- evaluate:完美分组 1/1;全并一组(过度合并)purity 低;打散重排
  (组内混标签)purity 与 completeness 双低;单例拆分不惩罚
  (purity = completeness = 1);漏分按单例计;未知 URL 与空组容错;
  空语料全 0;返回键恰为三个;
- run:注入完美 / 全并一组 grouper 产出两份报告文件且指标正确,
  报告含"合成语料仅验证归并逻辑"声明;
- 真实链(importorskip 冒烟):小语料与默认语料 purity = completeness = 1,
  团伙对被指纹并组;CLI 成功退出码 0、链未就位退出码 2(中文提示)。
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from benchmarks import grouping_bench as gb
from benchmarks.grouping_bench import (
    GANG_PAIRS,
    GroupingBenchError,
    evaluate,
    fingerprint_shas,
    make_synthetic,
    run,
)

# ---------------------------------------------------------------------------
# make_synthetic:规模 / 标签结构 / URL 合法性
# ---------------------------------------------------------------------------


def test_make_synthetic_scale_and_label_structure() -> None:
    """默认语料:12 站 ×(主域 + 2 镜像)+ 2 团伙对 = 40 URL / 14 标签。"""
    pairs = make_synthetic()
    assert len(pairs) == 12 * (1 + 2) + GANG_PAIRS * 2
    labels = dict(pairs)
    assert len(labels) == len(pairs)  # 每个 URL 唯一(dict 无覆盖)
    assert len(set(labels.values())) == 12 + GANG_PAIRS  # 真值标签数
    counts = Counter(labels.values())
    for i in range(12):
        assert counts[f"site{i}"] == 3  # 主域 + 2 镜像同标签
    assert counts["gang0"] == 2 and counts["gang1"] == 2  # 团伙对共享标签


def test_make_synthetic_urls_legal_and_unique() -> None:
    """全部 URL 合法 http(s) 且互不重复;镜像变体落在已知变体模式内。"""
    pairs = make_synthetic()
    urls = [url for url, _ in pairs]
    assert len(set(urls)) == len(urls)
    for url in urls:
        parts = urlsplit(url)
        assert parts.scheme in ("http", "https")
        assert parts.netloc and parts.hostname
    for url, label in pairs:
        if label.startswith("site") and url not in (
            f"http://site{label[4:]}.example/com",
            f"http://site{label[4:]}.example/net",
            f"http://site{label[4:]}.example/org",
        ):
            assert (
                url.startswith("http://www.")
                or ":8080" in url
                or url.startswith("http://m.")
                or url.startswith("https://")
            ), f"镜像变体模式不符:{url}"


def test_make_synthetic_gang_pairs_cross_registrable_domain() -> None:
    """团伙对:两个成员 host 不同(跨可注册域),但标签相同。"""
    pairs = make_synthetic()
    for j in range(GANG_PAIRS):
        members = [url for url, label in pairs if label == f"gang{j}"]
        hosts = {urlsplit(url).hostname for url in members}
        assert len(members) == 2 and len(hosts) == 2


def test_make_synthetic_canonical_semantics() -> None:
    """canonical 语义:同站(含镜像)同 canonical;异站 / 团伙对互相独立。"""
    canonical = pytest.importorskip("netsentinel.intel.canonical")
    pairs = make_synthetic()
    by_label: dict[str, set[str]] = {}
    for url, label in pairs:
        key = canonical.canonical_key(url)
        assert key, f"canonical 解析失败:{url}"
        by_label.setdefault(label, set()).add(key)
    for i in range(12):
        assert len(by_label[f"site{i}"]) == 1  # 主域 + 全部镜像同一 canonical
    site_keys = {next(iter(by_label[f"site{i}"])) for i in range(12)}
    assert len(site_keys) == 12  # 站与站互不坍缩
    for j in range(GANG_PAIRS):
        gang_keys = by_label[f"gang{j}"]
        assert len(gang_keys) == 2  # 团伙对跨可注册域
        assert gang_keys.isdisjoint(site_keys)


def test_make_synthetic_deterministic_same_seed() -> None:
    """决定性:同参数(含 seed)两次调用结果完全一致。"""
    assert make_synthetic() == make_synthetic()
    assert make_synthetic(n_sites=5, mirrors_per_site=3, seed=7) == make_synthetic(
        n_sites=5, mirrors_per_site=3, seed=7
    )


def test_make_synthetic_other_seed_same_structure() -> None:
    """换 seed:规模与标签结构不变(仍是合法语料,只是镜像选择可能不同)。"""
    other = make_synthetic(n_sites=12, mirrors_per_site=2, seed=2024)
    assert len(other) == 40
    counts = Counter(label for _, label in other)
    assert len(counts) == 14
    for i in range(12):
        assert counts[f"site{i}"] == 3
    assert counts["gang0"] == 2 and counts["gang1"] == 2


def test_make_synthetic_custom_params() -> None:
    """自定义参数:镜像 4 个吃满变体池;0 镜像只留主域 + 团伙对。"""
    full = make_synthetic(n_sites=5, mirrors_per_site=4)
    assert len(full) == 5 * 5 + GANG_PAIRS * 2
    assert len(dict(full)) == len(full)
    bare = make_synthetic(n_sites=3, mirrors_per_site=0)
    assert len(bare) == 3 + GANG_PAIRS * 2
    assert Counter(label for _, label in bare)[f"site0"] == 1


def test_make_synthetic_invalid_params() -> None:
    """非法参数:负数站点 / 负数镜像 → ValueError(中文消息)。"""
    with pytest.raises(ValueError, match="n_sites"):
        make_synthetic(n_sites=-1)
    with pytest.raises(ValueError, match="mirrors_per_site"):
        make_synthetic(mirrors_per_site=-1)


# ---------------------------------------------------------------------------
# 指纹线索合成
# ---------------------------------------------------------------------------


def _jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def test_fingerprint_gang_pair_overlap_above_threshold() -> None:
    """团伙对指纹重叠:Jaccard = 0.5,高于默认并组阈值 0.3。"""
    a = fingerprint_shas("gang0", "http://gang0a.example/")
    b = fingerprint_shas("gang0", "https://gang0b.example/")
    assert a and b and _jaccard(a, b) == pytest.approx(0.5)
    assert _jaccard(a, b) >= 0.3


def test_fingerprint_disjoint_across_labels() -> None:
    """异站 / 站与团伙 / 团伙之间指纹互斥;同站镜像指纹一致。"""
    s0 = fingerprint_shas("site0", "http://site0.example/com")
    s0_mirror = fingerprint_shas("site0", "http://www.site0.example/")
    s1 = fingerprint_shas("site1", "http://site1.example/net")
    g0a = fingerprint_shas("gang0", "http://gang0a.example/")
    g1a = fingerprint_shas("gang1", "http://gang1a.example/")
    assert s0 == s0_mirror  # 同站主域与镜像同一指纹集合
    assert not (s0 & s1) and not (s0 & g0a) and not (g0a & g1a)
    assert len(s0) == 3 and len(g0a) == 3


# ---------------------------------------------------------------------------
# evaluate:purity / completeness
# ---------------------------------------------------------------------------


def _handmade() -> tuple[dict[str, str], int]:
    """手造小语料:标签 A/B 各 3 条,便于精确核对指标公式。"""
    labels = {
        "http://a1.example/": "A",
        "http://a2.example/": "A",
        "http://a3.example/": "A",
        "http://b1.example/": "B",
        "http://b2.example/": "B",
        "http://b3.example/": "B",
    }
    return labels, 6


def test_evaluate_result_keys() -> None:
    labels, _ = _handmade()
    res = evaluate([list(labels)], labels)
    assert set(res) == {"purity", "completeness", "n_groups"}


def test_evaluate_perfect_grouping() -> None:
    """完美分组(按真值标签归组):purity = completeness = 1。"""
    labels, _ = _handmade()
    grouping = [
        [u for u, l in labels.items() if l == "A"],
        [u for u, l in labels.items() if l == "B"],
    ]
    res = evaluate(grouping, labels)
    assert res == {"purity": 1.0, "completeness": 1.0, "n_groups": 2}


def test_evaluate_overmerge_all_in_one_low_purity() -> None:
    """过度合并(全并一组):purity 与 completeness 均被拉低(3/6 = 0.5)。"""
    labels, _ = _handmade()
    res = evaluate([list(labels)], labels)
    assert res["purity"] == round(3 / 6, 2)
    assert res["completeness"] == round(3 / 6, 2)
    assert res["n_groups"] == 1


def test_evaluate_scatter_mixed_labels_low_metrics() -> None:
    """打散重排(每组混入不同标签):purity 与 completeness 双低。"""
    labels, total = _handmade()
    urls = list(labels)
    grouping = [urls[0:2], urls[2:4], urls[4:6]]  # (A,A)(A,B)(B,B) 型混排
    res = evaluate(grouping, labels)
    # purity:各组最大同标签 2+1+2 = 5 / 6;completeness:4 个纯成员 + 2 个 1/2 成员
    assert res["purity"] == round(5 / total, 2)
    assert res["completeness"] == round(5 / total, 2)  # (1×4 + 0.5×2) / 6
    assert res["n_groups"] == 3


def test_evaluate_singleton_split_not_penalized() -> None:
    """单例拆分(全打散成每组一条):两指标均不惩罚欠合并(恒 1)。"""
    labels, total = _handmade()
    res = evaluate([[url] for url in labels], labels)
    assert res == {"purity": 1.0, "completeness": 1.0, "n_groups": total}


def test_evaluate_missing_urls_counted_as_singletons() -> None:
    """漏分:labels 中未被分组覆盖的 URL 按单例组计(不豁免也不炸)。"""
    labels, _ = _handmade()
    grouping = [[u for u, l in labels.items() if l == "A"]]  # B 全部漏分
    res = evaluate(grouping, labels)
    assert res["n_groups"] == 1 + 3
    assert res["purity"] == 1.0 and res["completeness"] == 1.0


def test_evaluate_tolerates_unknown_urls_and_empty_groups() -> None:
    """容错:未知 URL 丢弃、空组忽略,不参与指标。"""
    labels, _ = _handmade()
    grouping = [
        ["http://a1.example/", "http://ghost.example/", ""],
        [],
        [u for u, l in labels.items() if l == "B"],
    ]
    urls = list(labels)
    res = evaluate(grouping, labels)
    # a2/a3 漏分按单例:purity = (1 + 1 + 1 + 3) / 6 = 1;n_groups = 2 + 2
    assert res["n_groups"] == 4
    assert res["purity"] == 1.0 and res["completeness"] == 1.0
    assert "http://ghost.example/" not in urls


def test_evaluate_empty_labels_and_empty_grouping() -> None:
    """空语料全 0;空分组 + 非空标签 → 全部按单例计。"""
    assert evaluate([["http://a.example/"]], {}) == {
        "purity": 0.0,
        "completeness": 0.0,
        "n_groups": 0,
    }
    labels, total = _handmade()
    res = evaluate([], labels)
    assert res == {"purity": 1.0, "completeness": 1.0, "n_groups": total}


def test_evaluate_accepts_sets_as_groups() -> None:
    """分组容器鸭子兼容:list[set[str]] 亦可(契约 §4 A118 原始形态)。"""
    labels, _ = _handmade()
    grouping = [
        {u for u, l in labels.items() if l == "A"},
        {u for u, l in labels.items() if l == "B"},
    ]
    assert evaluate(grouping, labels)["purity"] == 1.0


# ---------------------------------------------------------------------------
# run:注入 grouper 的对照实验
# ---------------------------------------------------------------------------


def _perfect_grouper(pairs: list[tuple[str, str]]) -> list[set[str]]:
    by_label: dict[str, set[str]] = {}
    for url, label in pairs:
        by_label.setdefault(label, set()).add(url)
    return list(by_label.values())


def test_run_injected_perfect_grouper_writes_reports(tmp_path: Path) -> None:
    """注入完美分组器:两份报告落盘,指标 1/1,json 与返回 payload 一致。"""
    payload = run(tmp_path, grouper=_perfect_grouper)
    md_path = tmp_path / "grouping_report.md"
    json_path = tmp_path / "grouping_report.json"
    assert md_path.is_file() and json_path.is_file()
    assert payload["eval"] == {
        "purity": 1.0,
        "completeness": 1.0,
        "n_groups": payload["corpus"]["n_labels"],
    }
    assert json.loads(json_path.read_text(encoding="utf-8")) == payload
    md = md_path.read_text(encoding="utf-8")
    assert "合成语料仅验证归并逻辑" in md
    assert "purity" in md and "completeness" in md and "语料规模" in md


def test_run_injected_all_in_one_grouper(tmp_path: Path) -> None:
    """注入"全并一组"分组器:过度合并 → purity 低(最大标签 3/40)。"""
    payload = run(tmp_path, grouper=lambda pairs: [[url for url, _ in pairs]])
    assert payload["eval"]["n_groups"] == 1
    assert payload["eval"]["purity"] == round(3 / 40, 2)
    assert payload["eval"]["purity"] < 0.15
    assert payload["chain"]["mode"].startswith("注入")


def test_run_injected_scatter_grouper(tmp_path: Path) -> None:
    """注入"打散重排"分组器:组内混标签 → purity 低。"""
    def scatter(pairs: list[tuple[str, str]]) -> list[list[str]]:
        by_label: dict[str, list[str]] = {}
        for url, label in pairs:
            by_label.setdefault(label, []).append(url)
        labels = sorted(by_label)
        depth = max(len(v) for v in by_label.values())
        return [
            [by_label[lab][k] for lab in labels if k < len(by_label[lab])]
            for k in range(depth)
        ]

    payload = run(tmp_path, grouper=scatter)
    assert payload["eval"]["purity"] < 0.5  # 每组混入多站标签
    assert payload["eval"]["n_groups"] == 3  # 最大标签规模(站 3 条)


def test_run_custom_corpus_params(tmp_path: Path) -> None:
    """自定义语料参数透传:规模与标签数反映在 payload。"""
    payload = run(tmp_path, grouper=_perfect_grouper, n_sites=4, mirrors_per_site=1, seed=9)
    assert payload["corpus"]["n_urls"] == 4 * 2 + GANG_PAIRS * 2
    assert payload["corpus"]["n_labels"] == 4 + GANG_PAIRS
    assert payload["corpus"]["seed"] == 9
    assert payload["eval"]["purity"] == 1.0


# ---------------------------------------------------------------------------
# 真实链(A104 + A105)与 CLI
# ---------------------------------------------------------------------------


def test_load_real_chain_returns_callables() -> None:
    """真实链惰性加载:返回 (group_entries, merge_groups) 两个可调用。"""
    pytest.importorskip("netsentinel.intel.case_group")
    pytest.importorskip("netsentinel.intel.group_linker")
    group_entries, merge_groups = gb._load_real_chain()
    assert callable(group_entries) and callable(merge_groups)


def test_run_real_chain_smoke(tmp_path: Path) -> None:
    """真实链冒烟:小语料 purity = completeness = 1,团伙对被指纹并组。"""
    pytest.importorskip("netsentinel.intel.case_group")
    pytest.importorskip("netsentinel.intel.group_linker")
    payload = run(tmp_path, n_sites=3, mirrors_per_site=2)
    assert payload["chain"]["mode"].startswith("真实链")
    # 基础组 = 3 站 + 4 个团伙单域;指纹并组后 = 3 + 2 = 5 个案件组
    assert payload["chain"]["base_groups"] == 3 + GANG_PAIRS * 2
    assert payload["eval"]["n_groups"] == 3 + GANG_PAIRS
    assert payload["eval"]["purity"] == 1.0
    assert payload["eval"]["completeness"] == 1.0
    assert (tmp_path / "grouping_report.md").is_file()
    assert (tmp_path / "grouping_report.json").is_file()


def test_run_real_chain_default_corpus(tmp_path: Path) -> None:
    """真实链 + 默认语料(12 站):镜像归并与团伙并组全部命中,双指标 1。"""
    pytest.importorskip("netsentinel.intel.case_group")
    pytest.importorskip("netsentinel.intel.group_linker")
    payload = run(tmp_path)
    assert payload["corpus"]["n_urls"] == 40
    assert payload["eval"] == {"purity": 1.0, "completeness": 1.0, "n_groups": 14}


def test_cli_real_chain_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI 实跑:小语料退出码 0,stdout 含中文摘要与报告路径。"""
    pytest.importorskip("netsentinel.intel.case_group")
    pytest.importorskip("netsentinel.intel.group_linker")
    rc = gb.main(["--out", str(tmp_path), "--n-sites", "2", "--mirrors-per-site", "1", "--seed", "7"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "分组基准完成" in out and "grouping_report.md" in out
    assert (tmp_path / "grouping_report.json").is_file()


def test_cli_exit_2_when_chain_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """真实链未就位:中文错误提示 + 退出码 2(不产出报告)。"""

    def _broken() -> tuple[object, object]:
        raise GroupingBenchError("真实分组链未就位:无法导入 netsentinel.intel.case_group")

    monkeypatch.setattr(gb, "_load_real_chain", _broken)
    rc = gb.main(["--out", str(tmp_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "未就位" in err
    assert not (tmp_path / "grouping_report.md").exists()
