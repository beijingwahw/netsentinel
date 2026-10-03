# -*- coding: utf-8 -*-
"""A173 benchmarks.tier_bench 三档并发基准测试。

纯离线、零外呼、零网络;**计数式断言,零墙钟比较**(红线 31:峰值是
Barrier 到达计数,本文件不断言任何耗时)。覆盖:

- peak_concurrency 核心语义:workers=4 × n=8(两轮循环 Barrier)→ 4;
  workers=1 → 1;n=workers 单轮 → n;n < workers 部分等待超时 → 实际
  到达数;task 抛异常(未到达 Barrier)→ 峰值按**安全到达数**计、
  超时兜底不挂死;放行后再抛异常 → 峰值仍为满额(到达已计数);
  n=12 三轮循环复用 → 峰值稳定 4;
- 标准任务 ``_barrier_task``:单任务打 2 方栏 → 超时破栏就地吞掉返回 0,
  到达计数 peak=1、parties=2;
- 入参校验:n=0/负、workers=0/负、timeout_s≤0/NaN、task_fn 不可调用、
  n 折不成整数 → 中文 ValueError(含「必须」);
- run 三档表与 A163 公式一致:cores=8 显式注入 → low/mid/high = 2/4/7,
  且逐档与 ``cpu_profile.tier_workers`` 交叉对表、peak==workers_expected;
  cores=None 缺省走 detect(monkeypatch detect → 2/4/7,cores_source=
  "detect");``NETSENTINEL_FAKE_CORES`` 环境钩子 → 2/4/7;A163 缺席
  (sys.modules 置 None + 包属性置 None + os.cpu_count=8)→ 内置 §1
  公式兜底同为 2/4/7(并行双态一致);
- 报告落盘:tier_report.md(中文表 + 口径说明含「峰值=Barrier 实测并发度,
  确定性计数非墙钟」;数据行数 == 3,计数断言)与 tier_report.json
  (benchmark/cores/cores_source/tiers 与返回表同源);同参数两次运行
  报告**逐字节一致**(无时间戳,红线 31);out_dir 缺省 "benchmarks/out";
- CLI:--out/--cores → 退出码 0、双报告落盘、stdout 三档「理论/实测」;
  实测与理论不一致(monkeypatch peak_concurrency)→ 退出码 2;
- 红线 31 专项(test_v9_bench_*):连续多批峰值计数完全一致 + 模块源码
  零计时原语(perf_counter / monotonic / time.time / 墙钟等待 / timeit);
- 红线 37 专项:cores∈{1,2,4,8,16,32} × 三档公式恒 1 ≤ workers ≤ cores;
  cores=4 实跑三档 peak == workers_expected ≤ 4。
"""
from __future__ import annotations

import inspect
import itertools
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from benchmarks import tier_bench as tb
from netsentinel.ops import cpu_profile as cpu_profile_mod
from netsentinel.ops.cpu_profile import ENV_FAKE_CORES
from netsentinel.ops.cpu_profile import tier_workers as a163_tier_workers

#: 红线 31:模块源码不得出现的计时原语字样(计数口径,非墙钟)
CLOCK_TOKENS = ("perf_counter", "monotonic", "time.time", "sleep", "timeit")


@pytest.fixture(autouse=True)
def _clean_fake_cores(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个测试前清掉 FAKE_CORES,避免开发机环境泄漏干扰缺省探测断言。"""
    monkeypatch.delenv(ENV_FAKE_CORES, raising=False)


# ---------------------------------------------------------------------------
# peak_concurrency:Barrier 计数法核心语义(全部确定性计数,零墙钟)
# ---------------------------------------------------------------------------
def test_peak_concurrency_workers4_n8_barrier_success() -> None:
    """契约主用例:workers=4 × n=8(两轮循环 Barrier)→ 峰值恰 4。"""
    peak = tb.peak_concurrency(tb._barrier_task, n=8, workers=4, timeout_s=5.0)
    assert peak == 4
    assert isinstance(peak, int) and peak >= 1


def test_peak_concurrency_single_worker_peak_is_one() -> None:
    """workers=1(1 方栏即到即放)× n=6 → 峰值 1。"""
    assert tb.peak_concurrency(tb._barrier_task, n=6, workers=1, timeout_s=5.0) == 1


def test_peak_concurrency_n_equals_workers_single_round() -> None:
    """n=workers=5:单轮即满栏放行 → 峰值 5。"""
    assert tb.peak_concurrency(tb._barrier_task, n=5, workers=5, timeout_s=5.0) == 5


def test_peak_concurrency_partial_timeout_counts_actual_arrivals() -> None:
    """n=3 < workers=5:栏永不满 → 部分等待超时破栏,峰值=实际到达数 3。"""
    assert tb.peak_concurrency(tb._barrier_task, n=3, workers=5, timeout_s=0.3) == 3


def test_peak_concurrency_task_exception_counts_safe_arrivals_no_hang() -> None:
    """task 抛异常(未到达 Barrier):峰值按安全到达数 3 计;每次等待带
    timeout_s 兜底,破栏必返——用例正常完成即证明不挂死(红线 31:不断言耗时)。"""
    calls = itertools.count()

    def flaky(barrier: tb._CountingBarrier, timeout_s: float) -> int:
        if next(calls) == 0:
            raise RuntimeError("故障注入:首个任务抛异常,未到达 Barrier")
        barrier.wait(timeout_s)  # 其余 3 个安全到达;破栏异常进 future,不干扰计数

    assert tb.peak_concurrency(flaky, n=4, workers=4, timeout_s=0.5) == 3


def test_peak_concurrency_exception_after_release_keeps_full_peak() -> None:
    """放行后再抛异常:到达已计数,峰值仍为满额 4(异常不追溯扣减)。"""

    def wait_then_raise(barrier: tb._CountingBarrier, timeout_s: float) -> int:
        barrier.wait(timeout_s)  # 4 方齐到,栏放行,峰值已记 4
        raise RuntimeError("放行后故障注入")

    assert tb.peak_concurrency(wait_then_raise, n=4, workers=4, timeout_s=5.0) == 4


def test_peak_concurrency_cyclic_barrier_reuse_three_rounds() -> None:
    """n=12 × workers=4:循环 Barrier 三轮放行,峰值稳定 4(不随轮数漂移)。"""
    assert tb.peak_concurrency(tb._barrier_task, n=12, workers=4, timeout_s=5.0) == 4


def test_default_barrier_task_swallows_broken_barrier() -> None:
    """标准任务:单任务打 2 方栏 → 超时破栏就地吞掉返回 0;计数器 peak=1。"""
    barrier = tb._CountingBarrier(2)
    assert barrier.parties == 2
    assert tb._barrier_task(barrier, 0.1) == 0
    assert barrier.peak == 1


# ---------------------------------------------------------------------------
# peak_concurrency:入参校验(中文 ValueError)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs",
    [
        {"n": 0},
        {"n": -3},
        {"workers": 0},
        {"workers": -1},
        {"timeout_s": 0.0},
        {"timeout_s": -0.5},
        {"timeout_s": float("nan")},
    ],
)
def test_peak_concurrency_invalid_args_raise_chinese_valueerror(
    kwargs: dict[str, Any],
) -> None:
    """n/workers/timeout_s 非法 → 中文 ValueError(含「必须」)。"""
    n = kwargs.get("n", 4)
    workers = kwargs.get("workers", 4)
    timeout_s = kwargs.get("timeout_s", 1.0)
    with pytest.raises(ValueError, match="必须"):
        tb.peak_concurrency(tb._barrier_task, n, workers, timeout_s=timeout_s)


def test_peak_concurrency_invalid_task_fn_and_garbage_n() -> None:
    """task_fn 不可调用 / n 折不成整数 → 中文 ValueError。"""
    with pytest.raises(ValueError, match="必须"):
        tb.peak_concurrency(None, 4, 4)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="必须"):
        tb.peak_concurrency(tb._barrier_task, "many", 4)  # type: ignore[arg-type]


def test_run_invalid_cores_raise_chinese_valueerror(tmp_path: Path) -> None:
    """run 的 cores 注入非法(0 / 负 / 折不成整数)→ 中文 ValueError。"""
    for bad in (0, -2, "eight"):  # type: ignore[list-item]
        with pytest.raises(ValueError, match="必须"):
            tb.run(tmp_path, cores=bad)


# ---------------------------------------------------------------------------
# run:三档表与 A163 公式一致(cores=8 → 2/4/7)+ 缺省探测双态
# ---------------------------------------------------------------------------
def test_run_table_matches_a163_formula_cores8(tmp_path: Path) -> None:
    """cores=8 注入:表恰三档、每行恰两键,逐档与 A163 公式交叉对表 2/4/7,
    且 Barrier 实测 peak == 理论 workers_expected。"""
    table = tb.run(tmp_path, cores=8)
    assert set(table) == set(tb.TIERS) == {"low", "mid", "high"}
    for tier in tb.TIERS:
        row = table[tier]
        assert set(row) == {"workers_expected", "peak"}
        assert row["workers_expected"] == a163_tier_workers(tier, reserve=1, cores=8)
        assert row["peak"] == row["workers_expected"]
    assert [table[t]["workers_expected"] for t in tb.TIERS] == [2, 4, 7]
    assert [table[t]["peak"] for t in tb.TIERS] == [2, 4, 7]


def test_run_cores_default_uses_detect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """cores=None 缺省走 A163 detect:monkeypatch 画像 cores=8 → 2/4/7,
    报告 cores_source="detect"。"""
    monkeypatch.setattr(
        cpu_profile_mod,
        "detect",
        lambda: {"cores": 8, "arch": "x86_64", "platform": "测试/离线", "psutil": False},
    )
    table = tb.run(tmp_path)
    assert [table[t]["workers_expected"] for t in tb.TIERS] == [2, 4, 7]
    assert all(table[t]["peak"] == table[t]["workers_expected"] for t in tb.TIERS)
    payload = json.loads((tmp_path / "tier_report.json").read_text(encoding="utf-8"))
    assert payload["cores"] == 8 and payload["cores_source"] == "detect"


def test_run_cores_default_respects_fake_cores_env_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A163 测试钩子 NETSENTINEL_FAKE_CORES=8 → detect 核数被覆盖 → 2/4/7。"""
    monkeypatch.setenv(ENV_FAKE_CORES, "8")
    table = tb.run(tmp_path)
    assert [table[t]["workers_expected"] for t in tb.TIERS] == [2, 4, 7]


def test_run_a163_absent_falls_back_to_builtin_formula(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A163 缺席(sys.modules 置 None + 包属性置 None,os.cpu_count=8):
    内置 §1 公式兜底,三档同为 2/4/7(并行双态结论一致)。"""
    import netsentinel.ops as ops_pkg

    monkeypatch.setitem(sys.modules, "netsentinel.ops.cpu_profile", None)
    monkeypatch.setattr(ops_pkg, "cpu_profile", None, raising=False)
    monkeypatch.setattr(tb.os, "cpu_count", lambda: 8)
    table = tb.run(tmp_path)
    assert [table[t]["workers_expected"] for t in tb.TIERS] == [2, 4, 7]
    assert all(table[t]["peak"] == table[t]["workers_expected"] for t in tb.TIERS)


def test_run_default_out_dir_is_benchmarks_out() -> None:
    """run 的 out_dir 缺省值为 benchmarks/out(契约 A173 口径)。"""
    assert inspect.signature(tb.run).parameters["out_dir"].default == "benchmarks/out"


# ---------------------------------------------------------------------------
# 报告落盘:md 中文表 + 口径说明 + json 同源(计数断言)
# ---------------------------------------------------------------------------
def test_run_writes_md_and_json_reports(tmp_path: Path) -> None:
    """双报告落盘:json 结构与返回表同源;md 中文表头 + 数据行数恰 3(计数
    断言)+ 口径说明含契约原句。"""
    table = tb.run(tmp_path, cores=8)
    md_path = tmp_path / "tier_report.md"
    json_path = tmp_path / "tier_report.json"
    assert md_path.is_file() and json_path.is_file()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["benchmark"] == "tier_bench"
    assert payload["cores"] == 8 and payload["cores_source"] == "inject"
    assert payload["method"] == "barrier-count"
    assert payload["tiers"] == table

    md = md_path.read_text(encoding="utf-8")
    assert "# NetSentinel 三档并发基准报告(A173)" in md
    assert "峰值=Barrier 实测并发度,确定性计数非墙钟" in md
    rows = [ln for ln in md.splitlines() if ln.startswith("| ")]
    assert rows[0] == "| 档位 | 理论 workers | 实测峰值(Barrier) | 判定 |"
    assert len(rows[2:]) == 3  # 去表头与分隔行:数据行数 == 三档
    assert "一致" in md and "不一致" not in md


def test_run_reports_byte_identical_across_runs(tmp_path_factory: pytest.TempPathFactory) -> None:
    """红线 31:同参数两次运行,md/json **逐字节一致**(报告无时间戳,
    确定性计数可复现)。"""
    dir_a = tmp_path_factory.mktemp("tier_a")
    dir_b = tmp_path_factory.mktemp("tier_b")
    tb.run(dir_a, cores=8)
    tb.run(dir_b, cores=8)
    for name in ("tier_report.md", "tier_report.json"):
        assert (dir_a / name).read_bytes() == (dir_b / name).read_bytes()


# ---------------------------------------------------------------------------
# CLI:--out / --cores
# ---------------------------------------------------------------------------
def test_cli_writes_reports_and_exits_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--out/--cores:退出码 0、双报告落盘、stdout 打印三档「理论/实测」。"""
    code = tb.main(["--out", str(tmp_path), "--cores", "8"])
    assert code == 0
    out = capsys.readouterr().out
    assert "cores=8" in out
    assert "low 2/2" in out and "mid 4/4" in out and "high 7/7" in out
    assert "退出码 0" in out
    payload = json.loads((tmp_path / "tier_report.json").read_text(encoding="utf-8"))
    assert payload["cores"] == 8
    assert (tmp_path / "tier_report.md").is_file()


def test_cli_peak_mismatch_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """实测与理论不一致(monkeypatch peak_concurrency 恒返 1)→ 退出码 2。"""
    monkeypatch.setattr(tb, "peak_concurrency", lambda task_fn, n, workers, **kw: 1)
    assert tb.main(["--out", str(tmp_path), "--cores", "8"]) == 2
    assert "退出码 2" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 红线专项
# ---------------------------------------------------------------------------
def test_v9_bench_peak_counting_deterministic_no_wallclock() -> None:
    """红线 31:峰值是**确定性到达计数**——连续三批完全一致;模块源码零
    计时原语(不测耗时、不与任何钟表读数比较)。"""
    peaks = [tb.peak_concurrency(tb._barrier_task, n=8, workers=4, timeout_s=5.0) for _ in range(3)]
    assert peaks == [4, 4, 4]
    src = Path(tb.__file__).read_text(encoding="utf-8")
    for token in CLOCK_TOKENS:
        assert token not in src, f"tier_bench 源码不应出现计时原语:{token}"


@pytest.mark.parametrize("cores", [1, 2, 4, 8, 16, 32])
def test_redline37_formula_workers_never_exceed_cores(cores: int) -> None:
    """红线 37:cores=1..32 × 三档,理论 workers 恒 1 ≤ w ≤ cores。"""
    for tier in tb.TIERS:
        w = tb._tier_workers(tier, cores=cores)
        assert 1 <= w <= cores, (tier, cores, w)


def test_redline37_real_run_peak_within_cores(tmp_path: Path) -> None:
    """红线 37 实跑:cores=4 三档实测峰值 == 理论 workers ≤ 4(执行侧钳制)。"""
    table = tb.run(tmp_path, cores=4)
    for tier, row in table.items():
        assert row["peak"] == row["workers_expected"] <= 4
