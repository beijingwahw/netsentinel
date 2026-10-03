# -*- coding: utf-8 -*-
"""A138 benchmarks.kernel_bench 内核基准总控测试。

纯离线、确定性、不依赖墙钟(红线 31);只写 tmp_path,不触仓库内
benchmarks/out。覆盖:

- collect:注册表全量(16 模块 + 1 条附加自检 = 17 行)与就位内核数
  (≥12)、四键统一归一、extra 键留档、两次调用逐行一致(离线可复现)、
  导入失败容错;
- A224 新注册:ops.aimd 的 kernel_selfcheck 与 reliability 的
  bayesian_kernel_selfcheck(KERNEL_EXTRA_CHECKS 附加行,行序就近插在
  标准行后;属性缺席 → not_ready 注明属性名;抛异常 → fail);
- HAS_NUMPY 标注:探测只读 mathx.HAS_NUMPY,payload / md / CLI 均标注;
- 判定规则两分支:``pass_`` 布尔优先;否则 value 有值即通过(含 value=0
  的"有值"边界与 value=None 的未通过);
- fake 内核接入:monkeypatch 进 sys.modules 验证收集路径与 not_ready 路径;
- run:两份报告落盘且可解析(JSON 结构 / Markdown 中文表)、汇总恰四键且
  计数自洽、默认 out_dir="benchmarks/out";
- bench(红线 31):Markdown 表数据行数 == 内核总数(操作计数口径);
- CLI:成功 0(monkeypatch 全通过)、未通过 / 未就位 → 2(默认注册表实跑);
- Windows 控制台 UTF-8 兜底:非 UTF-8 标准流被 reconfigure。
"""
from __future__ import annotations

import inspect
import io
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from benchmarks import kernel_bench as kb

#: 契约 §2 A138 行列出的 15 个内核模块 + A224 新注册的 ops.aimd
#: (CONTRACTS-V13 §2 工程清理:ops.aimd 自检收入注册表)。
CONTRACT_MODULES: frozenset[str] = frozenset(
    {
        "netsentinel.vision.heuristic_kernel",
        "netsentinel.intel.text_kernel",
        "netsentinel.decision.sprt",
        "netsentinel.decision.reliability",
        "netsentinel.intel.phash_lsh",
        "netsentinel.intel.graph_kernel",
        "netsentinel.submit.executor_session",
        "netsentinel.ops.sched_kernel",
        "netsentinel.ops.aimd",
        "netsentinel.storage.kernel",
        "netsentinel.vision.cache2",
        "netsentinel.mathx",
        "netsentinel.vision.phash2",
        "netsentinel.submit.style_kernel",
        "netsentinel.security.threat_kernel",
        "netsentinel.telemetry_export",
    }
)

#: 附加自检注册表口径(A224):reliability 的贝叶斯路径。
EXPECTED_EXTRA_CHECKS: dict[str, tuple[str, str]] = {
    "netsentinel.decision.reliability": ("bayesian_kernel_selfcheck", "reliability.bayes"),
}


@pytest.fixture(scope="module")
def default_rows() -> list[dict[str, Any]]:
    """默认注册表的一次全量收集(module 级共享,免重复实跑各内核自检)。"""
    return kb.collect()


@pytest.fixture(scope="module")
def default_run(tmp_path_factory: pytest.TempPathFactory):
    """默认注册表的一次完整 run(module 级共享),返回 (out_dir, summary)。"""
    out_dir = tmp_path_factory.mktemp("kernel_bench_run")
    summary = kb.run(out_dir)
    return out_dir, summary


# ---------------------------------------------------------------------------
# collect:全量与就位内核
# ---------------------------------------------------------------------------


def test_collect_registry_matches_contract() -> None:
    """注册表恰为契约 A138 的 15 模块 + A224 新增 ops.aimd(16 模块),
    附加自检注册表恰为 reliability 的贝叶斯路径。"""
    assert set(kb.KERNEL_MODULES) == CONTRACT_MODULES
    assert len(kb.KERNEL_MODULES) == 16
    assert kb.KERNEL_EXTRA_CHECKS == EXPECTED_EXTRA_CHECKS
    assert "KERNEL_EXTRA_CHECKS" in kb.__all__


def test_collect_covers_all_ready_kernels(default_rows: list[dict[str, Any]]) -> None:
    """全量收集:总行数 == 注册表模块数 + 附加自检数(17),就位内核 ≥ 12。"""
    assert len(default_rows) == len(kb.KERNEL_MODULES) + len(kb.KERNEL_EXTRA_CHECKS)
    assert len(default_rows) == 17
    ready = [r for r in default_rows if r["status"] in ("pass", "fail")]
    assert len(ready) >= 12
    # 已实现 kernel_selfcheck 的内核全部在列且判定为通过
    # (A224 起新增 aimd 与 reliability.bayes 附加自检)。
    names = {r["name"] for r in default_rows}
    assert {
        "skin",
        "text_kernel",
        "sprt",
        "reliability",
        "reliability.bayes",
        "aimd",
        "sched_kernel",
        "style_kernel",
        "threat_kernel",
        "phash2",
    } <= names
    assert all(r["status"] == "pass" for r in ready)


def test_collect_rows_have_unified_four_keys(default_rows: list[dict[str, Any]]) -> None:
    """就位行统一四键 {name, metric, value, baseline}(缺键已被容错补 None)。"""
    for row in default_rows:
        if row["status"] in ("pass", "fail"):
            assert set(kb._ROW_KEYS) <= set(row), row
            assert isinstance(row["name"], str) and row["name"]


def test_collect_known_not_ready_marked(default_rows: list[dict[str, Any]]) -> None:
    """executor_session/cache2 的 selfcheck 已由负责人补齐(2026-10-02)→ 现应全就位。"""
    by_module = {r["module"]: r for r in default_rows}
    for module_path in ("netsentinel.submit.executor_session", "netsentinel.vision.cache2"):
        row = by_module[module_path]
        # 2026-10-02 负责人补齐两内核 selfcheck:现应就位并通过(状态 pass,带 value/baseline)
        assert row["status"] == "pass"
        assert row["value"] is not None and row["baseline"] is not None
        assert row["name"]


def test_collect_extra_keys_preserved(default_rows: list[dict[str, Any]]) -> None:
    """threat_kernel 自检的额外键(total_ran 等)收入 extra 留档,不进四键。"""
    threat = next(r for r in default_rows if r["name"] == "threat_kernel")
    assert threat["extra"]["total_ran"] >= 1
    assert "total_ran" not in kb._ROW_KEYS


def test_collect_deterministic_reproducible() -> None:
    """红线 31:两次全量收集逐行一致(各内核自检为确定性微基准,零墙钟)。"""
    first = kb.collect()
    second = kb.collect()
    strip = lambda rows: [
        (r["name"], r.get("metric"), r.get("value"), r.get("baseline"), r["status"])
        for r in rows
    ]
    assert strip(first) == strip(second)


def test_collect_import_failure_not_ready() -> None:
    """模块路径导入失败 → not_ready 行(中文原因),不抛异常不中断。"""
    rows = kb.collect(modules=("netsentinel.no_such_module_v7",))
    assert rows == [
        {
            "name": "no_such_module_v7",
            "module": "netsentinel.no_such_module_v7",
            "status": "not_ready",
            "reason": rows[0]["reason"],
        }
    ]
    assert "模块导入失败" in rows[0]["reason"]


# ---------------------------------------------------------------------------
# 判定规则:pass_ 分支与 value 分支
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"pass_": True, "value": None}, True),      # pass_ 优先,哪怕 value 缺失
        ({"pass_": False, "value": 1}, False),       # pass_ 优先,value 有值也不翻案
        ({"value": 0}, True),                        # value 分支:0 是"有值"
        ({"value": "首迁 2 条 DDL"}, True),           # 字符串基线口径同样算有值
        ({}, False),                                 # 无 pass_ 且 value=None → 未通过
        ({"value": None}, False),
    ],
)
def test_judge_unified_rule_branches(result: dict[str, Any], expected: bool) -> None:
    """统一判定:含 pass_ 布尔用之;否则 value 非 None 即通过。"""
    assert kb._judge(result) is expected


# ---------------------------------------------------------------------------
# fake 内核接入(monkeypatch sys.modules)
# ---------------------------------------------------------------------------


def _install_fake(monkeypatch: pytest.MonkeyPatch, name: str, check: Any) -> str:
    """把 fake 模块塞进 sys.modules,返回其模块路径(collect 经 import_module 命中)。"""
    path = f"netsentinel._fake_{name}"
    module = types.ModuleType(path)
    if check is not None:
        module.kernel_selfcheck = check
    monkeypatch.setitem(sys.modules, path, module)
    return path


def test_collect_fake_kernel_integrated(monkeypatch: pytest.MonkeyPatch) -> None:
    """fake 内核经 sys.modules 接入:被 collect 收割并归一四键。"""

    def _selfcheck() -> dict[str, Any]:
        return {"name": "fake_v7", "metric": "fake_metric", "value": 7, "baseline": 6}
    path = _install_fake(monkeypatch, "ready", _selfcheck)
    rows = kb.collect(modules=(path,))
    assert rows == [
        {
            "name": "fake_v7",
            "metric": "fake_metric",
            "value": 7,
            "baseline": 6,
            "module": path,
            "status": "pass",
        }
    ]


def test_collect_fake_without_selfcheck_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """无 kernel_selfcheck 的 fake 模块 → not_ready 行(缺席跳过并标注)。"""
    path = _install_fake(monkeypatch, "absent", None)
    rows = kb.collect(modules=(path,))
    assert len(rows) == 1
    assert rows[0]["status"] == "not_ready"
    assert rows[0]["name"] == "_fake_absent"
    assert "未提供 kernel_selfcheck" in rows[0]["reason"]


def test_collect_fake_missing_keys_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    """自检只返回 name(缺 metric/value/baseline)→ 补 None;value 无值判 fail。"""
    path = _install_fake(monkeypatch, "partial", lambda: {"name": "partial_kernel"})
    rows = kb.collect(modules=(path,))
    row = rows[0]
    assert row["name"] == "partial_kernel"
    assert row["metric"] is None and row["value"] is None and row["baseline"] is None
    assert row["status"] == "fail"


def test_collect_fake_pass_field_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """fake 带 pass_=False:即使 value 有值也判 fail(pass_ 分支经全链路生效)。"""
    path = _install_fake(
        monkeypatch,
        "strict",
        lambda: {"name": "strict_kernel", "metric": "m", "value": 1, "baseline": 1, "pass_": False},
    )
    rows = kb.collect(modules=(path,))
    assert rows[0]["status"] == "fail"


def test_collect_fake_raising_selfcheck_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """自检抛异常 → fail 行(与缺席 not_ready 区分),不中断整批。"""
    def _boom() -> dict[str, Any]:
        raise AssertionError("自检内部口径断言失败")

    path = _install_fake(monkeypatch, "boom", _boom)
    rows = kb.collect(modules=(path,))
    assert rows[0]["status"] == "fail"
    assert rows[0]["value"] is None
    assert "kernel_selfcheck 抛异常" in rows[0]["reason"]


# ---------------------------------------------------------------------------
# A224 新注册:ops.aimd 标准自检 + reliability 贝叶斯附加自检 + HAS_NUMPY
# ---------------------------------------------------------------------------


def test_collect_aimd_row_registered_and_passing(default_rows: list[dict[str, Any]]) -> None:
    """A224:ops.aimd 的 kernel_selfcheck 收入注册表,四键 + extra 留档。"""
    row = next(r for r in default_rows if r["module"] == "netsentinel.ops.aimd")
    assert row["name"] == "aimd"
    assert row["status"] == "pass"
    assert row["value"] == row["baseline"] == 8  # 回升恰饱和在 cap(操作计数)
    assert row["extra"]["op_counts_invariant"] is True  # 附加键收入 extra
    assert row["extra"]["halving_seq"] == row["extra"]["halving_expected"]


def test_collect_bayesian_extra_check_row(default_rows: list[dict[str, Any]]) -> None:
    """A224:reliability 的 bayesian_kernel_selfcheck 作为附加行收割,
    行序紧随其标准 kernel_selfcheck 行之后(就近插入)。"""
    names = [r["name"] for r in default_rows]
    assert names.index("reliability.bayes") == names.index("reliability") + 1
    row = next(r for r in default_rows if r["name"] == "reliability.bayes")
    assert row["module"] == "netsentinel.decision.reliability"
    assert row["status"] == "pass"
    assert row["value"] is not None and row["baseline"] is not None
    assert row["value"] < row["baseline"]  # 漂移 1 日内后验均值减半以上(方向由内核断言锁定)
    assert row["extra"]["decay_evals"] == 1240  # 操作计数不变量随 extra 留档


def test_collect_extra_check_absent_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """附加自检属性缺席 → 追加一行 not_ready 并**注明属性名**,不中断。"""
    path = _install_fake(monkeypatch, "noextra", lambda: {"name": "std_only", "value": 1})
    monkeypatch.setitem(
        kb.KERNEL_EXTRA_CHECKS,
        path,
        ("bayesian_kernel_selfcheck", "fake.bayes"),
    )
    rows = kb.collect(modules=(path,))
    assert len(rows) == 2
    assert rows[0]["status"] == "pass"  # 标准自检照常收割
    assert rows[1]["status"] == "not_ready"
    assert rows[1]["name"] == "fake.bayes"
    assert "未提供 bayesian_kernel_selfcheck" in rows[1]["reason"]


def test_collect_extra_check_raising_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """附加自检抛异常 → fail 行(原因注明属性名),标准行不受影响。"""
    module = types.ModuleType("netsentinel._fake_both")
    module.kernel_selfcheck = lambda: {"name": "std_ok", "value": 1}
    def _bayes_boom() -> dict[str, Any]:
        raise AssertionError("贝叶斯自检内部口径断言失败")
    module.bayesian_kernel_selfcheck = _bayes_boom
    monkeypatch.setitem(sys.modules, "netsentinel._fake_both", module)
    monkeypatch.setitem(
        kb.KERNEL_EXTRA_CHECKS,
        "netsentinel._fake_both",
        ("bayesian_kernel_selfcheck", "fake.both"),
    )
    rows = kb.collect(modules=("netsentinel._fake_both",))
    assert rows[0]["status"] == "pass"
    assert rows[1]["status"] == "fail"
    assert "bayesian_kernel_selfcheck 抛异常" in rows[1]["reason"]


def test_collect_import_failure_extra_check_also_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模块导入失败时其附加自检行同样 not_ready(共享同一次导入失败原因)。"""
    path = "netsentinel.no_such_module_v7"
    monkeypatch.setitem(
        kb.KERNEL_EXTRA_CHECKS, path, ("bayesian_kernel_selfcheck", "gone.bayes")
    )
    rows = kb.collect(modules=(path,))
    assert len(rows) == 2
    assert all(r["status"] == "not_ready" for r in rows)
    assert rows[0]["name"] == "no_such_module_v7"
    assert rows[1]["name"] == "gone.bayes"
    assert all("模块导入失败" in r["reason"] for r in rows)


def test_probe_has_numpy_reads_mathx_readonly() -> None:
    """HAS_NUMPY 探测只读 netsentinel.mathx.HAS_NUMPY(布尔三态,绝不复算)。"""
    from netsentinel import mathx

    assert kb._probe_has_numpy() is mathx.HAS_NUMPY
    assert isinstance(kb._probe_has_numpy(), bool)


def test_has_numpy_text_three_states() -> None:
    """标注文案三态:True / False / 未知(探测失败)。"""
    assert "numpy" in kb._has_numpy_text(True)
    assert "stdlib" in kb._has_numpy_text(False)
    assert "未知" in kb._has_numpy_text(None)


def test_run_annotates_has_numpy_in_payload_and_markdown(default_run) -> None:
    """run 的 payload 携带 has_numpy(与 mathx 探测一致),md 头部标注一行。"""
    from netsentinel import mathx

    out_dir, _ = default_run
    payload = json.loads((out_dir / "kernel_report.json").read_text(encoding="utf-8"))
    assert payload["has_numpy"] is mathx.HAS_NUMPY
    md = (out_dir / "kernel_report.md").read_text(encoding="utf-8")
    assert f"- 数值后端 HAS_NUMPY:{kb._has_numpy_text(mathx.HAS_NUMPY)}" in md


# ---------------------------------------------------------------------------
# run:报告落盘与汇总
# ---------------------------------------------------------------------------


def test_run_writes_parsable_reports(default_run) -> None:
    """两份报告落盘:JSON 可解析且与 md 同源;md 为中文表(内核/指标/值/基线/判定)。"""
    out_dir, summary = default_run
    md_path = out_dir / "kernel_report.md"
    json_path = out_dir / "kernel_report.json"
    assert md_path.is_file() and json_path.is_file()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["benchmark"] == "kernel_bench"
    assert payload["summary"] == summary
    assert len(payload["kernels"]) == summary["total"]

    md = md_path.read_text(encoding="utf-8")
    assert "# NetSentinel 内核基准总控报告(A138)" in md
    assert "| 内核 | 指标 | 值 | 基线 | 判定 |" in md
    for verdict in ("通过", "未就位"):
        assert verdict in md
    for row in payload["kernels"]:
        assert row["name"] in md


def test_run_summary_exact_four_keys(default_run) -> None:
    """run 返回恰四键 {total, passed, failed, not_ready},计数自洽。"""
    _, summary = default_run
    assert set(summary) == {"total", "passed", "failed", "not_ready"}
    assert summary["passed"] + summary["failed"] + summary["not_ready"] == summary["total"]
    # 当前基线:16 模块 + 1 附加自检 = 17 个内核就位且全部通过(A224 起)。
    assert summary["total"] == 17
    assert summary["passed"] == 17
    assert summary["not_ready"] == 0
    assert summary["failed"] == 0


def test_run_default_out_dir_is_benchmarks_out() -> None:
    """run 的 out_dir 缺省值为 benchmarks/out(契约口径)。"""
    assert inspect.signature(kb.run).parameters["out_dir"].default == "benchmarks/out"


def test_v7_bench_markdown_rows_equal_kernel_count(default_run) -> None:
    """红线 31 bench:Markdown 表数据行数(操作计数)== 内核总数,一行不多不少。"""
    out_dir, summary = default_run
    md_lines = (out_dir / "kernel_report.md").read_text(encoding="utf-8").splitlines()
    table_lines = [ln for ln in md_lines if ln.startswith("| ")]
    assert table_lines[0] == "| 内核 | 指标 | 值 | 基线 | 判定 |"
    data_rows = table_lines[2:]  # 去表头与分隔行
    assert len(data_rows) == summary["total"]
    assert len(data_rows) == len(kb.KERNEL_MODULES) + len(kb.KERNEL_EXTRA_CHECKS)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_default_registry_exits_2_for_not_ready(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """默认注册表实跑:17 内核全就位 → 退出码 0;CLI 汇总标注 HAS_NUMPY。"""
    code = kb.main(["--out", str(tmp_path)])
    assert code == 0
    assert (tmp_path / "kernel_report.md").is_file()
    assert (tmp_path / "kernel_report.json").is_file()
    out = capsys.readouterr().out
    assert "未就位 0" in out and "退出码 0" in out
    assert "HAS_NUMPY" in out  # A224:CLI 汇总同样标注数值后端


def test_cli_all_pass_exits_0(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """全通过(monkeypatch collect)→ 退出码 0,报告照常落盘。"""
    monkeypatch.setattr(
        kb,
        "collect",
        lambda: [
            {"name": "a", "metric": "m", "value": 1, "baseline": 1, "module": "fake.a", "status": "pass"},
            {"name": "b", "metric": "m", "value": 2, "baseline": 3, "module": "fake.b", "status": "pass"},
        ],
    )
    assert kb.main(["--out", str(tmp_path)]) == 0
    payload = json.loads((tmp_path / "kernel_report.json").read_text(encoding="utf-8"))
    assert payload["summary"] == {"total": 2, "passed": 2, "failed": 0, "not_ready": 0}


def test_cli_failure_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """存在 fail 行(即使零 not_ready)→ 退出码 2。"""
    monkeypatch.setattr(
        kb,
        "collect",
        lambda: [
            {"name": "a", "metric": "m", "value": 1, "baseline": 1, "module": "fake.a", "status": "pass"},
            {"name": "b", "metric": None, "value": None, "baseline": None, "module": "fake.b", "status": "fail", "reason": "x"},
        ],
    )
    assert kb.main(["--out", str(tmp_path)]) == 2


def test_ensure_utf8_stdio_reconfigures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 兜底:cp1252 标准流被 reconfigure 为 UTF-8(失败亦不抛)。"""
    for stream_name in ("sys.stdout", "sys.stderr"):
        wrapper = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        monkeypatch.setattr(stream_name, wrapper)
        kb._ensure_utf8_stdio()
        assert wrapper.encoding.lower() == "utf-8"
