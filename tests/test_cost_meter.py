"""tests/test_cost_meter.py —— A75 成本计量器单元测试。

离线:仅使用 tmp_path 临时账本,零网络、零真实计费数据;
所有金额断言均针对"提示值口径"(以账单为准),不涉及真实价格。
"""
from __future__ import annotations

import datetime as _dt
import json
import logging

from netsentinel.vision.cost_meter import LOCAL_FREE, PRICE_HINTS, CostMeter


# ---------------------------------------------------------------------------
# 价格提示表本身:口径红线(提示值、数量级、以账单为准)
# ---------------------------------------------------------------------------
def test_price_hints_calibration() -> None:
    """≥8 条;键形如 provider:model;值非负且为整数数量级(不写小数级精确)。"""
    assert len(PRICE_HINTS) >= 8
    for key, price in PRICE_HINTS.items():
        assert isinstance(key, str) and ":" in key, f"键必须是 provider:model 形态:{key}"
        assert isinstance(price, (int, float))
        assert price >= 0, f"提示价不应为负:{key}"
        assert float(price) == int(price), f"提示价只保留数量级,不应带小数:{key}={price}"
    # glm-4v-flash 档历史免费(以官方为准)
    assert PRICE_HINTS["glm:glm-4v-flash"] == 0.0


def test_local_free_members() -> None:
    """本地提供方目录与契约 §2 的 local=True 四家一致。"""
    assert LOCAL_FREE == {"ollama", "vllm", "lmstudio", "xinference"}


# ---------------------------------------------------------------------------
# estimate 三分支:本地免费 / 有价 / 无价
# ---------------------------------------------------------------------------
def test_estimate_local_free(tmp_path) -> None:
    """本地提供方(任意模型)一律 0.0:成本是本地算力,不出本机。"""
    meter = CostMeter(tmp_path / "costs.jsonl")
    for provider in sorted(LOCAL_FREE):
        assert meter.estimate(provider, "whatever-model", 500) == 0.0


def test_estimate_priced(tmp_path) -> None:
    """有精确键:单价 × images / 1000。"""
    meter = CostMeter(tmp_path / "costs.jsonl")
    assert meter.estimate("openai", "gpt-4o-mini", 1000) == 10.0
    assert meter.estimate("openai", "gpt-4o-mini", 500) == 5.0
    assert meter.estimate("glm", "glm-4v-flash", 1000) == 0.0


def test_estimate_unknown_returns_none(tmp_path) -> None:
    """无价格提示 → None(不猜价,以账单为准)。"""
    meter = CostMeter(tmp_path / "costs.jsonl")
    assert meter.estimate("groq", "meta-llama/llama-4-scout", 100) is None
    assert meter.estimate("acme", "model-x", 1) is None  # 未知提供方
    assert meter.estimate("openai", "gpt-4o-mini-2024-07-18", 10) is None  # 键须精确


# ---------------------------------------------------------------------------
# 单位口径与金额两位小数
# ---------------------------------------------------------------------------
def test_unit_caliber_per_1000_calls(tmp_path) -> None:
    """单位口径:1000 次调用 ≈ 表中单价(元/千次图像调用)。"""
    meter = CostMeter(tmp_path / "costs.jsonl")
    for key, price in PRICE_HINTS.items():
        provider, model = key.split(":", 1)
        assert meter.estimate(provider, model, 1000) == float(price)
        assert meter.estimate(provider, model, 100) == _r2(price / 10)


def test_amounts_two_decimals(tmp_path) -> None:
    """金额输出保留两位小数。"""
    meter = CostMeter(tmp_path / "costs.jsonl")
    est = meter.estimate("qwen", "qwen-vl-max", 33)
    assert est == 0.66  # 20 元/千次 × 33 / 1000
    est2 = meter.estimate("openai", "gpt-4o", 3)
    assert est2 == 0.45  # 150 元/千次 × 3 / 1000
    for e in (est, est2):
        assert e == round(e, 2)


def _r2(v: float) -> float:
    return round(float(v), 2)


# ---------------------------------------------------------------------------
# record 落盘
# ---------------------------------------------------------------------------
def test_record_persists_jsonl(tmp_path) -> None:
    """record 追加一行完整 jsonl,est_cost 一并落盘,ts 可解析且为当日。"""
    path = tmp_path / "costs.jsonl"
    meter = CostMeter(path)
    entry = meter.record("openai", "gpt-4o-mini", 1000)
    assert entry["est_cost"] == 10.0
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert set(data) == {"ts", "provider", "model", "images", "est_cost"}
    assert data["provider"] == "openai"
    assert data["model"] == "gpt-4o-mini"
    assert data["images"] == 1000
    assert data["est_cost"] == 10.0
    assert _dt.datetime.fromisoformat(data["ts"]).date() == _dt.date.today()


def test_record_unpriced_writes_null(tmp_path) -> None:
    """无价格提示也照常记账,est_cost 落为 null。"""
    path = tmp_path / "costs.jsonl"
    meter = CostMeter(path)
    entry = meter.record("groq", "meta-llama/llama-4-scout", 7)
    assert entry["est_cost"] is None
    data = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert data["est_cost"] is None  # json null
    assert '"est_cost": null' in path.read_text(encoding="utf-8")


def test_record_local_zero(tmp_path) -> None:
    """本地提供方记账 est_cost=0.0(免费但次数照记)。"""
    meter = CostMeter(tmp_path / "costs.jsonl")
    assert meter.record("ollama", "llava", 64)["est_cost"] == 0.0


# ---------------------------------------------------------------------------
# summary 汇总:两家混合、unpriced 计数
# ---------------------------------------------------------------------------
def test_summary_mixed_providers(tmp_path) -> None:
    """多家混合汇总;null 成本按 0 求和并计入 unpriced_calls。"""
    meter = CostMeter(tmp_path / "costs.jsonl")
    meter.record("glm", "glm-4v-flash", 100)      # 0.0(免费档,有价不是 unpriced)
    meter.record("openai", "gpt-4o-mini", 1000)   # 10.0
    meter.record("openai", "gpt-4o-mini", 500)    # 5.0
    meter.record("qwen", "qwen-vl-max", 1000)     # 20.0
    meter.record("together", "Llama-4-Scout", 300)  # None(无提示)
    s = meter.summary()
    assert set(s) == {"by_provider", "total_est", "unpriced_calls"}
    assert set(s["by_provider"]) == {"glm", "openai", "qwen", "together"}
    assert s["by_provider"]["openai"] == {
        "calls": 2, "images": 1500, "est_cost": 15.0, "unpriced_calls": 0,
    }
    assert s["by_provider"]["glm"] == {
        "calls": 1, "images": 100, "est_cost": 0.0, "unpriced_calls": 0,
    }
    assert s["by_provider"]["together"] == {
        "calls": 1, "images": 300, "est_cost": 0.0, "unpriced_calls": 1,
    }
    assert s["by_provider"]["qwen"]["est_cost"] == 20.0
    assert s["total_est"] == 35.0
    assert s["unpriced_calls"] == 1


def test_summary_empty_when_no_file(tmp_path) -> None:
    """账本尚不存在 → 空汇总而非报错。"""
    meter = CostMeter(tmp_path / "absent.jsonl")
    assert meter.summary() == {"by_provider": {}, "total_est": 0.0, "unpriced_calls": 0}


def test_summary_amounts_two_decimals(tmp_path) -> None:
    """汇总金额同样保留两位小数。"""
    meter = CostMeter(tmp_path / "costs.jsonl")
    meter.record("qwen", "qwen-vl-max", 33)   # 0.66
    meter.record("qwen", "qwen-vl-max", 33)   # 0.66
    s = meter.summary()
    assert s["by_provider"]["qwen"]["est_cost"] == 1.32
    assert s["total_est"] == 1.32


# ---------------------------------------------------------------------------
# today 过滤(注入旧 ts)
# ---------------------------------------------------------------------------
def _append_line(path, entry: dict) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def test_today_filters_old_ts(tmp_path) -> None:
    """summary 全量;today 只统计当日,旧 ts 与不可解析 ts 均被过滤。"""
    path = tmp_path / "costs.jsonl"
    meter = CostMeter(path)
    meter.record("openai", "gpt-4o-mini", 200)  # 今日 → 2.0
    _append_line(path, {  # 2020 年的旧账
        "ts": "2020-01-01T08:00:00+08:00",
        "provider": "openai", "model": "gpt-4o-mini",
        "images": 800, "est_cost": 8.0,
    })
    _append_line(path, {  # ts 不可解析:summary 收,today 不收
        "ts": "not-a-date",
        "provider": "qwen", "model": "qwen-vl-max",
        "images": 1000, "est_cost": 20.0,
    })
    s = meter.summary()
    assert s["by_provider"]["openai"] == {
        "calls": 2, "images": 1000, "est_cost": 10.0, "unpriced_calls": 0,
    }
    assert s["total_est"] == 30.0
    t = meter.today()
    assert set(t["by_provider"]) == {"openai"}
    assert t["by_provider"]["openai"] == {
        "calls": 1, "images": 200, "est_cost": 2.0, "unpriced_calls": 0,
    }
    assert t["total_est"] == 2.0
    assert t["unpriced_calls"] == 0


def test_today_includes_unpriced(tmp_path) -> None:
    """当日无价记录也进入 today,并计入 unpriced_calls。"""
    meter = CostMeter(tmp_path / "costs.jsonl")
    meter.record("groq", "meta-llama/llama-4-scout", 5)
    t = meter.today()
    assert t["by_provider"]["groq"]["unpriced_calls"] == 1
    assert t["unpriced_calls"] == 1
    assert t["total_est"] == 0.0


# ---------------------------------------------------------------------------
# 坏行容错
# ---------------------------------------------------------------------------
def test_corrupt_lines_skipped(tmp_path, caplog) -> None:
    """损坏行(非法 JSON/非对象/空行/字段缺失)跳过并告警,不影响好账。"""
    path = tmp_path / "costs.jsonl"
    meter = CostMeter(path)
    meter.record("openai", "gpt-4o-mini", 100)  # 唯一有效行 → 1.0
    junk = [
        "{oops 不是 JSON",
        "",
        "[1, 2, 3]",
        json.dumps({"provider": "qwen", "images": 10}),          # 缺 model
        json.dumps(["ts", "provider", "model", "images"]),         # JSON 数组
        json.dumps({"provider": "x", "model": "y", "images": "三"}),  # images 非整数
    ]
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(junk) + "\n")
    with caplog.at_level(logging.WARNING, logger="netsentinel.vision.cost_meter"):
        s = meter.summary()
    assert set(s["by_provider"]) == {"openai"}
    assert s["by_provider"]["openai"]["calls"] == 1
    assert s["total_est"] == 1.0
    assert any("跳过" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# V5:流式汇总(不整载)+ cost.record 遥测 + 句柄释放
# ---------------------------------------------------------------------------
def test_v5_record_telemetry_counter(tmp_path) -> None:
    """可观测性:每次成功记账计一次 telemetry 'cost.record'。"""
    from netsentinel import telemetry

    telemetry.reset()
    try:
        meter = CostMeter(tmp_path / "costs.jsonl")
        meter.record("openai", "gpt-4o-mini", 100)
        meter.record("groq", "meta-llama/llama-4-scout", 5)   # 无价也照常记账计数
        meter.record("ollama", "llava", 8)
        assert telemetry.snapshot()["counters"].get("cost.record") == 3.0
    finally:
        telemetry.reset()


def test_v5_summary_streams_without_read_text(tmp_path, monkeypatch) -> None:
    """性能锁定:禁用 Path.read_text/read_bytes(整载式读取)后 summary/today 仍工作。"""
    from pathlib import Path as _Path

    def _boom(self, *args, **kwargs):  # pragma: no cover - 触发即失败
        raise AssertionError("V5 后汇总应为流式逐行读取,不得整载 read_text")

    monkeypatch.setattr(_Path, "read_text", _boom)
    monkeypatch.setattr(_Path, "read_bytes", _boom)

    meter = CostMeter(tmp_path / "costs.jsonl")
    # 先写好账本再禁用整载(monkeypatch 不影响 open/append 写路径)
    meter.record("openai", "gpt-4o-mini", 1000)
    meter.record("qwen", "qwen-vl-max", 1000)
    s = meter.summary()
    assert s["total_est"] == 30.0
    t = meter.today()
    assert t["by_provider"]["openai"]["calls"] == 1


def test_v5_summary_releases_file_handle(tmp_path) -> None:
    """健壮性(Windows 句柄复查):summary/today 后句柄已释放,可立即删除账本。"""
    path = tmp_path / "costs.jsonl"
    meter = CostMeter(path)
    meter.record("openai", "gpt-4o-mini", 100)
    meter.record("glm", "glm-5.3-flash", 100)
    assert meter.summary()["total_est"] == 1.5
    assert meter.today()["total_est"] == 1.5
    path.unlink()  # Windows 上若句柄未释放,这里会 PermissionError


def test_v5_summary_large_file_streamed(tmp_path) -> None:
    """大账本(5 万行)流式汇总:结果精确、耗时与内存受控(不整载)。"""
    import tracemalloc

    path = tmp_path / "big.jsonl"
    n, batch_provider = 50_000, "openai"
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for i in range(n):
            rec = {
                "ts": "2026-10-02T12:00:00+08:00",
                "provider": batch_provider,
                "model": "gpt-4o-mini",
                "images": 10,
                "est_cost": 0.1,
            }
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    meter = CostMeter(path)
    tracemalloc.start()
    try:
        s = meter.summary()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert s["by_provider"][batch_provider]["calls"] == n
    assert s["total_est"] == _r2(n * 0.1)
    # 峰值内存远小于整载(5 万行文本 ≈ 数 MB;聚合流式峰值应 < 1MB)
    assert peak < 1_000_000, f"流式汇总峰值内存过大:{peak} 字节"


# ---------------------------------------------------------------------------
# V13:per-run 成本归集(增维度 record / 纯聚合 aggregate / 缺维度回退)
# ---------------------------------------------------------------------------
def _v13_records() -> list[dict]:
    """手算对照用混合账目:旧形态行(无新维度)+ 新形态行(run_id/tokens/duration)。"""
    return [
        # 旧行:无 run_id / tokens / duration_s(V13 前落盘形态)
        {"ts": "2026-10-01T09:00:00+08:00", "provider": "openai",
         "model": "gpt-4o-mini", "images": 1000, "est_cost": 10.0},
        # 新形态行:run-a 批
        {"ts": "2026-10-02T10:00:00+08:00", "provider": "openai",
         "model": "gpt-4o-mini", "images": 500, "est_cost": 5.0,
         "run_id": "run-a", "tokens": 1024, "duration_s": 2.5},
        {"ts": "2026-10-02T11:00:00+08:00", "provider": "glm",
         "model": "glm-4v-flash", "images": 200, "est_cost": 0.0,
         "run_id": "run-a", "tokens": 512, "duration_s": 0.5},
        # run-b 批 + ts 不可解析 + 无价提示(est_cost=None)
        {"ts": "not-a-date", "provider": "together",
         "model": "Llama-4-Scout", "images": 300, "est_cost": None,
         "run_id": "run-b"},
    ]


def test_record_run_dimensions_written(tmp_path) -> None:
    """显式传 run_id/tokens/duration_s → 行携带新维度;金额口径不变。"""
    path = tmp_path / "costs.jsonl"
    meter = CostMeter(path)
    entry = meter.record("openai", "gpt-4o-mini", 1000, run_id="run-a",
                         tokens=2048, duration_s=1.5)
    assert entry["run_id"] == "run-a"
    assert entry["tokens"] == 2048
    assert entry["duration_s"] == 1.5
    assert entry["est_cost"] == 10.0          # 金额口径与旧版一致
    data = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert set(data) == {"ts", "provider", "model", "images", "est_cost",
                         "run_id", "tokens", "duration_s"}
    assert data["run_id"] == "run-a"


def test_record_without_run_dimensions_old_shape(tmp_path) -> None:
    """不传可选维度 → 行仍为旧五键(不传 run_id 时行为与行形态不变)。"""
    path = tmp_path / "costs.jsonl"
    CostMeter(path).record("openai", "gpt-4o-mini", 1000)
    data = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert set(data) == {"ts", "provider", "model", "images", "est_cost"}
    # 部分传入只写对应键(其余缺省不写)
    CostMeter(path).record("glm", "glm-4v-flash", 100, run_id="run-b")
    data2 = json.loads(path.read_text(encoding="utf-8").splitlines()[1])
    assert set(data2) == {"ts", "provider", "model", "images", "est_cost", "run_id"}


def test_aggregate_by_model_manual() -> None:
    """纯聚合 by=model:混合行手算对照(键 = provider:model,与价格表口径一致)。"""
    from netsentinel.vision.cost_meter import aggregate

    agg = aggregate(_v13_records(), by="model")
    assert agg["by"] == "model"
    assert [r["model"] for r in agg["rows"]] == [   # 键升序(确定性)
        "glm:glm-4v-flash", "openai:gpt-4o-mini", "together:Llama-4-Scout",
    ]
    rows = {r["model"]: r for r in agg["rows"]}
    assert rows["openai:gpt-4o-mini"] == {
        "model": "openai:gpt-4o-mini", "provider": "openai", "calls": 2,
        "images": 1500, "tokens": 1024, "duration_s": 2.5, "est_cost": 15.0,
        "unpriced_calls": 0,
    }
    assert rows["glm:glm-4v-flash"]["duration_s"] == 0.5
    assert rows["glm:glm-4v-flash"]["est_cost"] == 0.0   # 免费档有价,不算 unpriced
    assert rows["together:Llama-4-Scout"]["unpriced_calls"] == 1
    assert rows["together:Llama-4-Scout"]["tokens"] == 0  # 可选维度缺失按 0
    assert agg["totals"] == {
        "calls": 4, "images": 2000, "tokens": 1536, "duration_s": 3.0,
        "est_cost": 15.0, "unpriced_calls": 1,
    }


def test_aggregate_by_run_id_unmarked_fallback() -> None:
    """by=run_id:旧行(无 run_id)回退 "(未标记批次)" 桶(向后兼容读旧行)。"""
    from netsentinel.vision.cost_meter import UNMARKED_RUN, aggregate

    agg = aggregate(_v13_records(), by="run_id")
    rows = {r["run_id"]: r for r in agg["rows"]}
    assert set(rows) == {UNMARKED_RUN, "run-a", "run-b"}
    assert rows[UNMARKED_RUN] == {
        "run_id": UNMARKED_RUN, "calls": 1, "images": 1000, "tokens": 0,
        "duration_s": 0.0, "est_cost": 10.0, "unpriced_calls": 0,
    }
    assert rows["run-a"] == {
        "run_id": "run-a", "calls": 2, "images": 700, "tokens": 1536,
        "duration_s": 3.0, "est_cost": 5.0, "unpriced_calls": 0,
    }
    assert rows["run-b"]["unpriced_calls"] == 1
    assert agg["totals"]["calls"] == 4


def test_aggregate_by_day_unknown_fallback() -> None:
    """by=day:按 ts 本地日期归桶;不可解析回退 "(未知日期)" 桶。"""
    from netsentinel.vision.cost_meter import UNKNOWN_DAY, aggregate

    agg = aggregate(_v13_records(), by="day")
    rows = {r["day"]: r for r in agg["rows"]}
    assert set(rows) == {"2026-10-01", "2026-10-02", UNKNOWN_DAY}
    assert rows["2026-10-01"]["est_cost"] == 10.0
    assert rows["2026-10-02"]["calls"] == 2
    assert rows[UNKNOWN_DAY]["unpriced_calls"] == 1


def test_aggregate_invalid_by_raises_chinese() -> None:
    """by 非法 → 中文 ValueError(不猜维度)。"""
    import pytest
    from netsentinel.vision.cost_meter import aggregate

    with pytest.raises(ValueError, match="聚合维度"):
        aggregate([], by="provider")
    with pytest.raises(ValueError, match="聚合维度"):
        aggregate(_v13_records(), by="")


def test_aggregate_headers_zh_and_skips_invalid() -> None:
    """返回中文表头映射(收官成本表直接可用);非映射/核心字段缺失行整行跳过。"""
    from netsentinel.vision.cost_meter import AGG_HEADERS_ZH, aggregate

    good = {"ts": "2026-01-01T00:00:00+08:00", "provider": "a", "model": "m",
            "images": 1, "est_cost": 0.5}
    agg = aggregate(["junk", {"provider": "x"}, None, good], by="day")
    assert agg["rows"] == [{"day": "2026-01-01", "calls": 1, "images": 1,
                            "tokens": 0, "duration_s": 0.0, "est_cost": 0.5,
                            "unpriced_calls": 0}]
    assert agg["headers_zh"] == AGG_HEADERS_ZH
    for field, header in AGG_HEADERS_ZH.items():
        assert field and header  # 全部非空
    assert AGG_HEADERS_ZH["calls"] == "调用次数"
    assert "模型" in AGG_HEADERS_ZH["model"]


def test_aggregate_non_numeric_optional_dimensions_as_zero() -> None:
    """tokens/duration_s 非数值(坏数据)按 0 累计,不抛错、不影响金额。"""
    from netsentinel.vision.cost_meter import aggregate

    rec = {"ts": "2026-10-02T10:00:00+08:00", "provider": "openai",
           "model": "m", "images": 10, "est_cost": 0.1,
           "tokens": "很多", "duration_s": None, "run_id": 42}
    agg = aggregate([rec], by="model")
    assert agg["rows"][0]["tokens"] == 0
    assert agg["rows"][0]["duration_s"] == 0.0
    assert agg["rows"][0]["est_cost"] == 0.1
    agg_run = aggregate([rec], by="run_id")   # run_id 为 int 也按字符串归桶
    assert agg_run["rows"][0]["run_id"] == "42"


def test_meter_aggregate_reads_old_ledger_lines(tmp_path) -> None:
    """账本级聚合兼容读旧格式行(直接落盘,不经 record):归未标记桶。"""
    from netsentinel.vision.cost_meter import UNMARKED_RUN

    path = tmp_path / "vlm_cost.jsonl"
    _append_line(path, {  # V13 前的旧形态行
        "ts": "2020-01-01T08:00:00+08:00", "provider": "openai",
        "model": "gpt-4o-mini", "images": 800, "est_cost": 8.0,
    })
    meter = CostMeter(path)
    meter.record("glm", "glm-4v-flash", 100, run_id="run-a")
    agg = meter.aggregate(by="run_id")
    rows = {r["run_id"]: r for r in agg["rows"]}
    assert set(rows) == {UNMARKED_RUN, "run-a"}
    assert rows[UNMARKED_RUN]["est_cost"] == 8.0
    assert rows["run-a"]["calls"] == 1


def test_meter_aggregate_run_id_filter(tmp_path) -> None:
    """meter.aggregate(run_id=...) 只统计该批;缺省(不过滤)统计全账本。"""
    path = tmp_path / "costs.jsonl"
    meter = CostMeter(path)
    meter.record("openai", "gpt-4o-mini", 1000, run_id="run-a")   # 10.0
    meter.record("openai", "gpt-4o-mini", 1000, run_id="run-b")   # 10.0
    meter.record("openai", "gpt-4o-mini", 500)                    # 5.0(未标记)
    only_a = meter.aggregate(by="model", run_id="run-a")
    assert only_a["totals"] == {"calls": 1, "images": 1000, "tokens": 0,
                                "duration_s": 0.0, "est_cost": 10.0,
                                "unpriced_calls": 0}
    everything = meter.aggregate(by="model")   # 缺省口径:全账本(现状不变)
    assert everything["totals"]["calls"] == 3
    assert everything["totals"]["est_cost"] == 25.0
    assert meter.summary()["total_est"] == 25.0  # summary 口径不受影响


def test_pyproject_analytics_extra_minimal() -> None:
    """pyproject 仅新增 analytics extra(pyarrow>=15);核心依赖与既有 extras 原样。"""
    import pathlib
    import tomllib

    data = tomllib.loads(
        (pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml")
        .read_text(encoding="utf-8")
    )
    project = data["project"]
    assert project["dependencies"] == ["PyYAML>=6.0"]   # 核心依赖零变化
    extras = project["optional-dependencies"]
    assert extras.get("analytics") == ["pyarrow>=15"]
    for key in ("browser", "vision", "clip", "ui", "api", "fast", "dev"):
        assert extras.get(key), f"既有 extra 缺失:{key}"   # 旧 extras 原样保留


# ---------------------------------------------------------------------------
# A232:批预算熔断 check_budget(纯函数 / None 关闭 / unpriced 诚实计数)
# ---------------------------------------------------------------------------
def _budget_records() -> list[dict]:
    """手算对照用账目:run-a 批(5.0 已定价 + 1 次无价)+ 未标记批 + run-b 批。"""
    ts = "2026-10-03T10:00:00+08:00"
    return [
        {"ts": ts, "provider": "openai", "model": "gpt-4o-mini",
         "images": 1000, "est_cost": 10.0},                       # 未标记批
        {"ts": ts, "provider": "openai", "model": "gpt-4o-mini",
         "images": 500, "est_cost": 5.0, "run_id": "run-a"},      # run-a 已定价
        {"ts": ts, "provider": "together", "model": "Llama-4-Scout",
         "images": 300, "est_cost": None, "run_id": "run-a"},     # run-a 无价提示
        {"ts": ts, "provider": "qwen", "model": "qwen-vl-max",
         "images": 1000, "est_cost": 20.0, "run_id": "run-b"},    # 其他批(不得计入)
    ]


def test_check_budget_manual_over() -> None:
    """手算对照(超限):run-a 花费 5.0(无价提示不计入),预算 4.5 → 超限。"""
    from netsentinel.vision.cost_meter import check_budget

    st = check_budget(_budget_records(), run_id="run-a", max_cost=4.5)
    assert isinstance(st.over, bool)
    assert st.over is True            # 5.00 > 4.50
    assert st.spent == 5.0            # 只算 run-a 的已定价调用
    assert st.max_cost == 4.5
    assert st.unpriced_calls == 1     # 无价提示诚实单独计数
    assert st.calls == 2              # run-a 两次调用(含无价那次)
    # run-b 的 20.0 与未标记批的 10.0 均不得混入本批预算口径
    assert "超批预算" in st.advice and "超出 0.50" in st.advice
    assert "无价格提示" in st.advice   # unpriced 在文案中如实提示


def test_check_budget_manual_within_and_boundary() -> None:
    """手算对照(未超 / 恰好用满):预算 12 → 未超;spent == max 不算超限。"""
    from netsentinel.vision.cost_meter import check_budget

    st = check_budget(_budget_records(), run_id="run-a", max_cost=12.0)
    assert st.over is False
    assert st.spent == 5.0
    assert "未超批预算" in st.advice
    assert "无价格提示" in st.advice and "1 次" in st.advice
    # 边界:恰好用满(5.0 == 5.0)不算超限(严格大于口径)
    boundary = check_budget(_budget_records(), run_id="run-a", max_cost=5.0)
    assert boundary.over is False and boundary.spent == 5.0


def test_check_budget_none_disables() -> None:
    """max_cost=None → 返回 None(关闭检查;finishflow 侧即零行为)。"""
    from netsentinel.vision.cost_meter import check_budget

    assert check_budget(_budget_records(), run_id="run-a", max_cost=None) is None
    assert check_budget([], run_id="run-a", max_cost=None) is None


def test_check_budget_unpriced_honest() -> None:
    """全为无价提示的批:spent=0(不猜价)、unpriced 如实计数、文案提示。"""
    from netsentinel.vision.cost_meter import check_budget

    recs = [
        {"provider": "groq", "model": "m1", "images": 10,
         "est_cost": None, "run_id": "run-c"},
        {"provider": "xai", "model": "m2", "images": 20,
         "est_cost": None, "run_id": "run-c"},
    ]
    st = check_budget(recs, run_id="run-c", max_cost=0.0)
    assert st.spent == 0.0 and st.unpriced_calls == 2 and st.calls == 2
    assert st.over is False           # 0.0 > 0.0 不成立
    assert "无价格提示" in st.advice and "2 次" in st.advice


def test_check_budget_invalid_max_cost_raises_chinese() -> None:
    """max_cost 非法(负数 / 非数值 / bool)→ 中文 ValueError(配置错当场报)。"""
    import pytest
    from netsentinel.vision.cost_meter import check_budget

    for bad in (-1, -0.01, "10元", True, [5.0]):
        with pytest.raises(ValueError, match="批预算"):
            check_budget(_budget_records(), run_id="run-a", max_cost=bad)


def test_check_budget_run_id_none_budgets_unmarked_bucket() -> None:
    """run_id=None → 预算"(未标记批次)"桶(与 aggregate 缺维度回退一致)。"""
    from netsentinel.vision.cost_meter import UNMARKED_RUN, check_budget

    st = check_budget(_budget_records(), run_id=None, max_cost=9.0)
    assert st.over is True and st.spent == 10.0
    assert st.calls == 1 and st.unpriced_calls == 0
    assert UNMARKED_RUN in st.advice


def test_check_budget_skips_invalid_and_empty_batch() -> None:
    """核心字段不合法的行整行跳过;目标批零记录 → 全零未超(诚实空态)。"""
    from netsentinel.vision.cost_meter import check_budget

    junk = ["junk", {"provider": "x"}, None,
            {"ts": "t", "provider": "qwen", "model": "m",
             "images": 1, "est_cost": 20.0, "run_id": "run-b"}]
    st = check_budget(junk, run_id="run-z", max_cost=1.0)
    assert st.over is False and st.spent == 0.0
    assert st.calls == 0 and st.unpriced_calls == 0
    # run-b 只有一条合法行:垃圾行不影响它批
    st_b = check_budget(junk, run_id="run-b", max_cost=1.0)
    assert st_b.spent == 20.0 and st_b.over is True


def test_meter_check_budget_convenience(tmp_path) -> None:
    """CostMeter.check_budget 便捷读侧:与纯函数对同一账本逐字段一致。"""
    from netsentinel.vision.cost_meter import check_budget

    path = tmp_path / "vlm_cost.jsonl"
    for row in _budget_records():
        _append_line(path, row)
    meter = CostMeter(path)
    for budget in (4.5, 12.0, None):
        got = meter.check_budget(run_id="run-a", max_cost=budget)
        want = check_budget(_budget_records(), run_id="run-a", max_cost=budget)
        assert got == want            # BudgetStatus 为值相等 dataclass
    # 账本不存在:零记录 → 未超(便捷读侧不抛错)
    empty = CostMeter(tmp_path / "absent.jsonl").check_budget(
        run_id="run-a", max_cost=1.0
    )
    assert empty is not None and empty.over is False and empty.spent == 0.0


def test_budget_status_frozen_dataclass_shape() -> None:
    """BudgetStatus 冻结 dataclass,六字段齐备(调用方按 .over 分支)。"""
    import dataclasses

    import pytest
    from netsentinel.vision.cost_meter import BudgetStatus, check_budget

    st = check_budget(_budget_records(), run_id="run-a", max_cost=12.0)
    assert dataclasses.is_dataclass(BudgetStatus)
    assert {f.name for f in dataclasses.fields(BudgetStatus)} == {
        "over", "spent", "max_cost", "unpriced_calls", "calls", "advice",
    }
    with pytest.raises(Exception):  # noqa: B017 - 冻结不可变(宽断言:赋值必炸)
        st.over = True  # type: ignore[misc]
