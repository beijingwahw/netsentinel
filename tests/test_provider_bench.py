# -*- coding: utf-8 -*-
"""A77 单元测试:跨平台响应解析基准(全程离线,只写 tmp_path)。

覆盖点:
- FIXTURES 完整性:≥24 条、四家族齐且分布均匀、字段齐、name 唯一、负样本存在;
- run(小语料 6 条):家族分组统计正确(openai/other 各 3 条,2 成功 1 失败 →
  成功率与合规率均 2/3)、平均修复深度按成功样本均值、失败清单口径:
  should_parse=False 的样例恰好计入失败清单(不多不少);
- run(内置 FIXTURES):期望口径零偏差(实际解析 == should_parse)、
  解析失败集合 == 负样本集合、schema 合规 ≤ 解析成功、四家族分组齐全;
- 报告落盘:providers_report.md / providers_report.json 两文件落盘、
  json 可解析且与 run 返回值一致、md 含中文表头与“模拟语料”“vlmctl ping”声明;
- repair_depth 量尺:0/1/2 阶梯与截断/空文本返回 None;
- 修复器后端优先级:sys.modules 注入 response_repair 替身后 run 优先走 A73
  的 repair(并行开发期则为内置同构实现);
- fixtures 非法(空列表 / 缺 text 字段)→ ProviderBenchError 中文;
- CLI:--out 生效、退出码 0、报告两文件存在、stdout 摘要中文。
"""
from __future__ import annotations

import json
import sys
import types
from collections import Counter
from pathlib import Path

import pytest

from benchmarks.providers import (
    FAMILY_ORDER,
    FIXTURES,
    ProviderBenchError,
    main,
    repair_depth,
    run,
)

# ---------------------------------------------------------------------------
# 小语料:6 条(openai 3 条:2 成功 1 失败;other 3 条:2 成功 1 失败)
# ---------------------------------------------------------------------------

SMALL: list[dict[str, object]] = [
    {
        "name": "s_openai_pure",
        "family": "openai",
        "should_parse": True,
        "text": '{"nsfw_prob": 0.91, "categories": ["色情"], "reasoning": "明显成人内容。", "confidence": 0.93}',
    },
    {
        "name": "s_openai_fenced",
        "family": "openai",
        "should_parse": True,
        "text": '```json\n{"nsfw_prob": 0.05, "categories": ["正常"], "reasoning": "风景照。", "confidence": 0.9}\n```',
    },
    {
        "name": "s_openai_truncated",
        "family": "openai",
        "should_parse": False,
        "text": '{"nsfw_prob": 0.4, "categories": ["低俗"], "reasoning": "截',
    },
    {
        "name": "s_other_single_quote",
        "family": "other",
        "should_parse": True,
        "text": "{'nsfw_prob': 0.32, 'categories': ['性感但正常'], 'reasoning': '泳装写真。', 'confidence': 0.7}",
    },
    {
        "name": "s_other_chinese_quote",
        "family": "other",
        "should_parse": True,
        "text": "{“nsfw_prob”: 0.86, “categories”: [“色情”], “reasoning”: “明显违规。”, “confidence”: 0.95}",
    },
    {
        "name": "s_other_no_json",
        "family": "other",
        "should_parse": False,
        "text": "模型未返回结构化内容,仅一句普通描述。",
    },
]


# ---------------------------------------------------------------------------
# FIXTURES 完整性
# ---------------------------------------------------------------------------


def test_fixtures_integrity() -> None:
    assert len(FIXTURES) >= 24
    counts = Counter(str(item["family"]) for item in FIXTURES)
    assert set(counts) == set(FAMILY_ORDER)
    for family in FAMILY_ORDER:
        assert counts[family] >= 5, f"家族 {family} 样本过少:{counts[family]}"
    names: set[str] = set()
    for item in FIXTURES:
        assert {"family", "name", "text", "should_parse"} <= set(item)
        assert isinstance(item["text"], str)
        assert isinstance(item["should_parse"], bool)
        assert isinstance(item["name"], str) and item["name"]
        assert item["name"] not in names
        names.add(str(item["name"]))
    # 负样本(截断 / 拒答 / 空文本 / 纯文本)分布在多个家族
    negatives = [item for item in FIXTURES if not item["should_parse"]]
    assert len(negatives) >= 4
    assert len({item["family"] for item in negatives}) >= 3


# ---------------------------------------------------------------------------
# run:小语料统计口径
# ---------------------------------------------------------------------------


def test_run_small_fixture_stats(tmp_path: Path) -> None:
    stats = run(tmp_path, fixtures=SMALL)  # type: ignore[arg-type]

    fam = {row["family"]: row for row in stats["families"]}
    assert set(fam) == {"openai", "other"}
    for family in ("openai", "other"):
        row = fam[family]
        assert row["samples"] == 3
        assert row["parsed"] == 2
        assert row["parse_rate"] == round(2 / 3, 4)
        assert row["schema_ok"] == 2
        assert row["schema_rate"] == round(2 / 3, 4)
        assert len(row["failures"]) == 1
    # 平均修复深度 = 成功样本深度的均值(失败样本不计入)
    assert fam["openai"]["avg_repair_depth"] == 0.5  # (0 + 1) / 2
    assert fam["other"]["avg_repair_depth"] == 2.0  # (2 + 2) / 2

    overall = stats["overall"]
    assert overall["samples"] == 6
    assert overall["parsed"] == 4
    assert overall["schema_ok"] == 4
    assert overall["parse_rate"] == round(4 / 6, 4)
    assert stats["expectation_mismatch"] == []

    # 失败口径一致:should_parse=False 的样例恰好全部计入失败清单
    failed_names = {item["name"] for item in stats["failures"]}
    expected_failures = {item["name"] for item in SMALL if not item["should_parse"]}
    assert failed_names == expected_failures
    for row in stats["details"]:
        if not row["should_parse"]:
            assert not row["parsed"]
            assert row["repair_depth"] is None
            assert row["nsfw_prob"] is None


def test_run_reports_written(tmp_path: Path) -> None:
    stats = run(tmp_path, fixtures=SMALL)  # type: ignore[arg-type]
    md_path = tmp_path / "providers_report.md"
    json_path = tmp_path / "providers_report.json"
    assert md_path.is_file()
    assert json_path.is_file()
    # json 可解析,且与 run 返回的统计 dict 完全一致
    assert json.loads(json_path.read_text(encoding="utf-8")) == stats
    text = md_path.read_text(encoding="utf-8")
    for token in (
        "家族",
        "样本数",
        "解析成功率",
        "schema 合规率",
        "失败样例名",
        "模拟语料",
        "vlmctl ping",
    ):
        assert token in text
    # 家族分组表列出小语料的两个家族
    assert "| openai | 3 |" in text
    assert "| other | 3 |" in text


# ---------------------------------------------------------------------------
# run:内置 FIXTURES 全量(零外呼、毫秒级)
# ---------------------------------------------------------------------------


def test_run_builtin_fixtures(tmp_path: Path) -> None:
    stats = run(tmp_path)
    assert stats["fixtures"]["total"] == len(FIXTURES)
    assert [row["family"] for row in stats["families"]] == list(FAMILY_ORDER)

    expected_parsed = sum(1 for item in FIXTURES if item["should_parse"])
    overall = stats["overall"]
    assert overall["parsed"] == expected_parsed
    assert overall["schema_ok"] <= overall["parsed"]
    assert stats["expectation_mismatch"] == []

    # 解析失败集合 == 负样本集合(should_parse=False 的样例计入失败口径一致)
    parse_failed = {row["name"] for row in stats["details"] if not row["parsed"]}
    negatives = {item["name"] for item in FIXTURES if not item["should_parse"]}
    assert parse_failed == negatives
    listed_failed = {item["name"] for item in stats["failures"]}
    assert parse_failed <= listed_failed  # 失败清单另含 schema 不合规样例
    # 每条家族统计的失败样例名与明细一致
    for row in stats["families"]:
        expected = [
            d["name"]
            for d in stats["details"]
            if d["family"] == row["family"] and not (d["parsed"] and d["schema_ok"])
        ]
        assert row["failures"] == expected


def test_run_family_grouping_custom_family(tmp_path: Path) -> None:
    custom = [
        {
            "name": "c_pure",
            "family": "custom_vendor",
            "should_parse": True,
            "text": '{"nsfw_prob": 0.5}',
        },
        {
            "name": "c_broken",
            "family": "custom_vendor",
            "should_parse": False,
            "text": "no json here",
        },
        {
            "name": "c_openai",
            "family": "openai",
            "should_parse": True,
            "text": '{"nsfw_prob": 0.2}',
        },
    ]
    stats = run(tmp_path, fixtures=custom)  # type: ignore[arg-type]
    fam = {row["family"]: row for row in stats["families"]}
    assert set(fam) == {"openai", "custom_vendor"}
    assert fam["openai"]["parse_rate"] == 1.0
    assert fam["custom_vendor"]["parsed"] == 1
    assert fam["custom_vendor"]["failures"] == ["c_broken"]
    assert fam["custom_vendor"]["label"] == "自定义家族"


# ---------------------------------------------------------------------------
# 修复深度量尺与后端优先级
# ---------------------------------------------------------------------------


def test_repair_depth_ladder() -> None:
    assert repair_depth('{"nsfw_prob": 0.5}') == 0
    assert repair_depth('前缀杂文:\n{"nsfw_prob": 0.5}') == 1
    assert repair_depth('```json\n{"nsfw_prob": 0.5}\n```') == 1
    assert repair_depth("{'nsfw_prob': 0.5,}") == 2
    assert repair_depth('{"nsfw_prob": 0.5, "flag": True}') == 2
    assert repair_depth("{“nsfw_prob”: 0.5}") == 2
    # 截断 / 无 JSON / 空文本 / 非字符串 → None
    assert repair_depth('{"nsfw_prob": 0.5, "reasoning": "截') is None
    assert repair_depth("没有任何 JSON") is None
    assert repair_depth("") is None
    assert repair_depth(None) is None  # type: ignore[arg-type]


def test_repair_backend_prefers_response_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = types.ModuleType("netsentinel.vision.response_repair")
    seen: list[str] = []

    def _fake_repair(text: str) -> dict:
        seen.append(text)
        return {"nsfw_prob": 0.5, "categories": ["正常"], "reasoning": "替身", "confidence": 0.6}

    stub.repair = _fake_repair  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "netsentinel.vision.response_repair", stub)

    stats = run(tmp_path, fixtures=SMALL)  # type: ignore[arg-type]
    assert stats["repair_backend"] == "response_repair(A73)"
    assert len(seen) == len(SMALL)  # 每条都确实走了替身(含负样本)
    assert stats["overall"]["parsed"] == len(SMALL)
    assert stats["overall"]["schema_ok"] == len(SMALL)


# ---------------------------------------------------------------------------
# 非法输入与 CLI
# ---------------------------------------------------------------------------


def test_run_invalid_fixtures(tmp_path: Path) -> None:
    with pytest.raises(ProviderBenchError):
        run(tmp_path, fixtures=[])
    with pytest.raises(ProviderBenchError):
        run(tmp_path, fixtures=[{"family": "openai", "should_parse": True}])  # 缺 text


def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--out", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / "providers_report.md").is_file()
    assert (tmp_path / "providers_report.json").is_file()
    out = capsys.readouterr().out
    assert "基准完成" in out
    assert "报告已写出" in out
    assert "vlmctl ping" in out

# ---------------------------------------------------------------------------
# V5 升级:单遍统计口径不变 + 遥测(provider_bench.run / fixtures / errors)
# ---------------------------------------------------------------------------


def test_v5_run_emits_telemetry(tmp_path: Path) -> None:
    """run 全程计时 provider_bench.run,样本数计入 provider_bench.fixtures。"""
    from netsentinel import telemetry

    telemetry.reset()
    run(tmp_path, fixtures=SMALL)  # type: ignore[arg-type]
    snap = telemetry.snapshot()
    assert snap["timers"]["provider_bench.run"]["count"] == 1
    assert snap["counters"]["provider_bench.fixtures"] == 6


def test_v5_run_stats_single_pass_same_as_before(tmp_path: Path) -> None:
    """单遍累积后的家族/总体统计与既有口径逐字段一致(含自定义家族排序)。"""
    stats = run(tmp_path, fixtures=SMALL)  # type: ignore[arg-type]
    fam = {row["family"]: row for row in stats["families"]}
    assert list(fam) == ["openai", "other"]  # FAMILY_ORDER 顺序
    assert fam["openai"]["failures"] == ["s_openai_truncated"]
    assert fam["other"]["failures"] == ["s_other_no_json"]
    assert fam["openai"]["avg_repair_depth"] == 0.5
    assert fam["other"]["avg_repair_depth"] == 2.0
    overall = stats["overall"]
    assert (overall["samples"], overall["parsed"], overall["schema_ok"]) == (6, 4, 4)
    assert overall["parse_rate"] == round(4 / 6, 4)
    assert stats["fixtures"]["by_family"] == {"openai": 3, "other": 3}
    # 失败清单与期望口径校对按明细顺序生成(单遍累积不改变顺序)
    assert [item["name"] for item in stats["failures"]] == [
        "s_openai_truncated",
        "s_other_no_json",
    ]
    assert stats["expectation_mismatch"] == []


def test_v5_repair_backend_exception_counts_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """修复器后端抛异常:该样本按失败计且计入 provider_bench.errors(不中断基准)。"""
    from netsentinel import telemetry

    boom_text = SMALL[1]["text"]  # 对第 2 条样本注入异常,其余正常解析

    def _flaky_repair(text: str):
        if text == boom_text:
            raise RuntimeError("模拟修复器故障")
        return {"nsfw_prob": 0.5}

    stub = types.ModuleType("netsentinel.vision.response_repair")
    stub.repair = _flaky_repair  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "netsentinel.vision.response_repair", stub)

    telemetry.reset()
    stats = run(tmp_path, fixtures=SMALL)  # type: ignore[arg-type]
    assert stats["overall"]["parsed"] == 5  # 6 条中 1 条按解析失败计
    failed = {item["name"] for item in stats["failures"]}
    assert "s_openai_fenced" in failed  # 注入异常的那条(第 2 条样本)
    reason = next(item["reason"] for item in stats["failures"] if item["name"] == "s_openai_fenced")
    assert "修复器异常" in reason
    assert telemetry.snapshot()["counters"]["provider_bench.errors"] == 1
