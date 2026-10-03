# -*- coding: utf-8 -*-
"""A119 批量总流程 CLI(batchflow)单元测试:全部离线、全注入。

覆盖:run_flow 全链 fake(3 域 8 URL → 3 组)、中文摘要 capsys、永不提交
(dry_run=False 也不提交,红线 24)、拒绝条目、空清单短路、清单缺失、
兄弟模块缺失中文 RuntimeError、注入参数透传(scanner/grouping/memory/
run_scan/queue/packager)、最大组统计、扫描失败行;resume_batch 新建批次
与续批分派、ready 合并补 portal、dry_run 缺省 cfg 与显式 False、无条目
零值、超上限透传 A112 ValueError(红线 26)、BatchState 对接约定
(resume/new_batch/bound 缺失的中文错误)、模块缺失、行形态兼容;CLI
main 的互斥校验 / 模式分派 / 错误码 / 入口。

零网络、零真实门户、零真实提交;兄弟全 fake 或 monkeypatch sys.modules。
"""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import subprocess
import sys
import types
from typing import Any

import pytest

from netsentinel import telemetry
from netsentinel import telemetry_trace as tt
from netsentinel.batchflow import resume_batch, run_flow, main
from netsentinel.contracts import Config

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 3 域 × 8 URL:a.com(3)/b.com(2)/c.com.cn(3);canonical 去重后待扫 3
URLS_3DOM_8: list[str] = [
    "https://a.com/",
    "https://www.a.com/x",
    "http://a.com:8080/y",
    "https://b.com/",
    "https://www.b.com/",
    "https://c.com.cn/",
    "https://www.c.com.cn/",
    "https://c.com.cn:8443/z",
]


# ---------------------------------------------------------------------------
# Fake 组件(离线注入)
# ---------------------------------------------------------------------------
class FakeIntake:
    """load_bulk 替身:记录路径,返回预置 (合法, 拒绝)。"""

    def __init__(self, valid: list[str] | None = None, rejected=None) -> None:
        self.valid = list(valid or [])
        self.rejected = list(rejected or [])
        self.calls: list[str] = []

    def __call__(self, path: str):
        self.calls.append(path)
        return list(self.valid), list(self.rejected)


class FakeScanner:
    """batch_scan 替身:记录 (urls, cfg, kwargs),返回预置 reports/summary。"""

    def __init__(self, reports=None, errors=None) -> None:
        self.reports = dict(reports or {})
        self.errors = list(errors or [])
        self.calls: list[dict[str, Any]] = []

    def __call__(self, urls, cfg, **kwargs):
        self.calls.append({"urls": list(urls), "cfg": cfg, "kwargs": kwargs})
        reports = {u: self.reports.get(u, f"report:{u}") for u in urls}
        return {"reports": reports, "summary": {"errors": list(self.errors)}}

    @property
    def n(self) -> int:
        return len(self.calls)


@dataclasses.dataclass
class FakeGroup:
    """CaseGroup 鸭子替身(组名/别名/成员 URL/伪 entry id)。"""

    name: str
    site_urls: list[str]
    entry_ids: list[int]
    verdict: str = "nsfw"
    agg_max: float = 0.9


class FakeGrouping:
    """group_and_enqueue 替身:记录 (cfg, kwargs),返回预置归组结果。"""

    def __init__(self, groups=None, enqueued=None, skipped_clean=0) -> None:
        self.groups = list(groups or [])
        self.enqueued = enqueued  # None → 缺省取组数
        self.skipped_clean = int(skipped_clean)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, cfg, *, reports=None, **kwargs):
        self.calls.append({"cfg": cfg, "reports": dict(reports or {}), "kwargs": kwargs})
        enqueued = len(self.groups) if self.enqueued is None else self.enqueued
        return {"groups": list(self.groups), "enqueued": enqueued,
                "skipped_clean": self.skipped_clean}


class FakeMemory:
    """指纹记忆替身:无历史指纹 → 全部按需扫(不跳过)。"""

    def last_fingerprint(self, url: str) -> str:
        return ""

    def should_rescan(self, url: str, fp: str) -> bool:
        return True


class FakeSubmit:
    """提交函数替身:任何调用都视为违反"run_flow 永不提交"。"""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("run_flow 不得触发任何提交动作(红线 24)")


class FakeReady:
    """ready_entries 替身:记录 cfg,返回预置就绪条目。"""

    def __init__(self, items=None) -> None:
        self.items = [dict(i) for i in (items or [])]
        self.calls: list[Any] = []

    def __call__(self, cfg, **kwargs):
        self.calls.append(cfg)
        return [dict(i) for i in self.items]


class BoundFake:
    """BatchState.bound(batch_id) 返回的状态器鸭子替身。"""

    def __init__(self, batch_id: int) -> None:
        self.batch_id = batch_id
        self.marks: list[tuple[Any, str, str]] = []

    def mark(self, entry_id, status, error=""):
        self.marks.append((entry_id, status, error))


class FakeState:
    """A113 BatchState 鸭子替身:resume/new_batch/bound/close 全留痕。

    ``drop`` 中列出的方法**不绑定**(实例级按需挂方法,而非类定义),
    用于模拟注入状态器缺方法的对接错误。
    """

    def __init__(self, unfinished=None, drop: tuple[str, ...] = ()) -> None:
        self.unfinished = list(unfinished or [])
        self.resumed: list[Any] = []
        self.created: list[tuple[list, str]] = []
        self.bound_ids: list[Any] = []
        self.closed = 0
        self._next = 100
        if "resume" not in drop:

            def resume(batch_id):
                self.resumed.append(batch_id)
                return list(self.unfinished)

            self.resume = resume
        if "new_batch" not in drop:

            def new_batch(items, note=""):
                self.created.append((list(items), note))
                self._next += 1
                return self._next

            self.new_batch = new_batch
        if "bound" not in drop:

            def bound(batch_id):
                self.bound_ids.append(batch_id)
                return BoundFake(batch_id)

            self.bound = bound

    def close(self):
        self.closed += 1


class FakeRunBatch:
    """A112 run_batch 替身:记录 (items, cfg, kwargs),返回预置摘要。"""

    def __init__(self, result=None) -> None:
        self.result = dict(
            result
            or {
                "submitted": 0,
                "failed": 0,
                "rate_limited": False,
                "paused": False,
                "results": [],
                "note": "",
            }
        )
        self.calls: list[dict[str, Any]] = []

    def __call__(self, items, cfg, **kwargs):
        self.calls.append(
            {"items": [dict(i) for i in items], "cfg": cfg, "kwargs": dict(kwargs)}
        )
        return dict(self.result)


class RowLike:
    """sqlite3.Row 鸭子(有 keys()/__getitem__,非 Mapping/dataclass)。"""

    def __init__(self, data: dict) -> None:
        self._data = dict(data)

    def keys(self):
        return list(self._data.keys())

    def __getitem__(self, key):
        return self._data[key]


@dataclasses.dataclass
class RowDataclass:
    """dataclass 形态的批次行。"""

    entry_id: int
    group_name: str


def _cfg(tmp_path, **kw) -> Config:
    """离线 Config:data_dir/db_path 全指向 tmp,避免触碰仓库 data/。"""
    return Config(
        data_dir=str(tmp_path / "data"),
        db_path=str(tmp_path / "q.db"),
        audit_path=str(tmp_path / "audit.jsonl"),
        **kw,
    )


def _write_cfg_yaml(tmp_path) -> str:
    """main --config 用的临时配置(data_dir/db_path 指向 tmp)。"""
    path = tmp_path / "config.yaml"
    path.write_text(
        f"data_dir: {(tmp_path / 'data').as_posix()}\n"
        f"db_path: {(tmp_path / 'q.db').as_posix()}\n",
        encoding="utf-8",
    )
    return str(path)


def _write_txt(tmp_path, lines: list[str], name: str = "leads.txt") -> str:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# run_flow:全链 fake
# ---------------------------------------------------------------------------
class TestRunFlow:
    def test_full_chain_fake_3dom_8url_3groups(self, tmp_path, capsys):
        """3 域 8 URL:真 load_bulk+plan_scan(canonical 去重)→ fake 扫描/归组。"""
        path = _write_txt(tmp_path, URLS_3DOM_8)
        scanner = FakeScanner()
        grouping = FakeGrouping(
            groups=[
                FakeGroup("a.com", ["https://a.com/"], [-1]),
                FakeGroup("b.com", ["https://b.com/"], [-2]),
                FakeGroup("c.com.cn", ["https://c.com.cn/"], [-3]),
            ]
        )
        cfg = _cfg(tmp_path)
        result = run_flow(
            path, cfg, scanner=scanner, grouping=grouping,
            memory=FakeMemory(), submit=FakeSubmit(), dry_run=True,
        )
        assert result["groups"] == 3
        assert result["enqueued"] == 3
        assert result["to_scan"] == [
            "https://a.com/", "https://b.com/", "https://c.com.cn/"
        ]
        s = result["summary"]
        assert s["valid"] == 8 and s["rejected"] == 0
        assert s["to_scan"] == 3 and s["duplicate_urls"] == 5
        assert s["skipped_unchanged"] == 0
        assert s["groups"] == 3 and s["enqueued"] == 3
        assert s["max_group_size"] == 1 and s["skipped_clean"] == 0
        assert s["dry_run"] is True
        assert scanner.n == 1
        assert scanner.calls[0]["urls"] == result["to_scan"]
        # reports 原样透传给 grouping:待扫 URL 一一对应
        assert set(grouping.calls[0]["reports"].keys()) == set(result["to_scan"])

    def test_summary_printed_chinese(self, tmp_path, capsys):
        """中文摘要七要素 + 下一步指引(TUI 声明 → batchflow --resume)。"""
        path = _write_txt(tmp_path, URLS_3DOM_8[:1])
        run_flow(
            path, _cfg(tmp_path), scanner=FakeScanner(),
            grouping=FakeGrouping(groups=[FakeGroup("a.com", ["https://a.com/"], [-1])]),
            memory=FakeMemory(),
        )
        out = capsys.readouterr().out
        for token in ("合法", "拒绝", "待扫", "跳过", "组数", "入列数", "最大组"):
            assert token in out, f"摘要缺少字段:{token}"
        assert "下一步:python -m netsentinel.cli.batch_tui 声明" in out
        assert "python -m netsentinel.batchflow --resume <批次号> --exec" in out
        assert "红线 24" in out and "红线 25" in out

    def test_never_submits_even_dry_run_false(self, tmp_path, capsys):
        """run_flow 永不提交:submit 替身零调用;dry_run=False 也只改提示。"""
        path = _write_txt(tmp_path, ["https://a.com/"])
        spy = FakeSubmit()
        result = run_flow(
            path, _cfg(tmp_path), scanner=FakeScanner(),
            grouping=FakeGrouping(groups=[FakeGroup("a.com", ["https://a.com/"], [-1])]),
            memory=FakeMemory(), submit=spy, dry_run=False,
        )
        assert spy.calls == []
        assert result["summary"]["dry_run"] is False
        assert "未执行任何提交" in capsys.readouterr().out

    def test_rejected_entries_counted(self, tmp_path, capsys):
        """坏行不中断整批:ftp 协议 / 非法主机 → 拒绝清单计数与打印。"""
        path = _write_txt(
            tmp_path,
            ["ftp://bad.example.com/x", "hello world.com", "https://good.com/"],
        )
        result = run_flow(
            path, _cfg(tmp_path), scanner=FakeScanner(),
            grouping=FakeGrouping(groups=[FakeGroup("good.com", ["https://good.com/"], [-1])]),
            memory=FakeMemory(),
        )
        assert result["summary"]["valid"] == 1
        assert result["summary"]["rejected"] == 2
        assert "拒绝条目  : 2" in capsys.readouterr().out

    def test_empty_input_short_circuit(self, tmp_path, capsys):
        """空清单:零值摘要,不触碰扫描/归组,打印中文提示。"""
        path = _write_txt(tmp_path, ["# 只有注释", ""])
        scanner, grouping = FakeScanner(), FakeGrouping()
        result = run_flow(path, _cfg(tmp_path), scanner=scanner, grouping=grouping,
                          memory=FakeMemory())
        assert result["groups"] == 0 and result["enqueued"] == 0
        assert result["to_scan"] == []
        assert result["summary"]["valid"] == 0
        assert scanner.n == 0 and grouping.calls == []
        assert "待扫描清单为空" in capsys.readouterr().out

    def test_missing_input_file_raises_chinese(self, tmp_path):
        with pytest.raises(ValueError, match="不存在"):
            run_flow(str(tmp_path / "nope.txt"), _cfg(tmp_path))

    def test_sibling_bulk_intake_missing(self, tmp_path, monkeypatch):
        """A107 缺失 → 中文 RuntimeError(模块未就位)。"""
        monkeypatch.setitem(sys.modules, "netsentinel.ops.bulk_intake", None)
        with pytest.raises(RuntimeError, match="未就位"):
            run_flow("whatever.txt", _cfg(tmp_path))

    def test_sibling_batch_scan_missing(self, tmp_path, monkeypatch):
        """A108 缺失:清单可加载,扫描阶段中文 RuntimeError。"""
        monkeypatch.setitem(sys.modules, "netsentinel.ops.batch_scan", None)
        path = _write_txt(tmp_path, ["https://a.com/"])
        with pytest.raises(RuntimeError, match="未就位"):
            run_flow(path, _cfg(tmp_path), memory=FakeMemory())

    def test_injection_kwargs_passthrough(self, tmp_path):
        """run_scan/pool_runner/memory → scanner;queue/packager → grouping。"""
        path = _write_txt(tmp_path, ["https://a.com/"])
        scanner, grouping = FakeScanner(), FakeGrouping(groups=[])
        run_scan, pool, queue, packager = object(), object(), object(), object()
        run_flow(
            path, _cfg(tmp_path), scanner=scanner, grouping=grouping,
            memory=FakeMemory(), run_scan=run_scan, pool_runner=pool,
            queue=queue, packager=packager,
        )
        assert scanner.calls[0]["kwargs"]["run_scan"] is run_scan
        assert scanner.calls[0]["kwargs"]["pool_runner"] is pool
        assert isinstance(scanner.calls[0]["kwargs"]["memory"], FakeMemory)
        assert grouping.calls[0]["kwargs"] == {"queue": queue, "packager": packager}

    def test_max_group_size_and_clean_skipped(self, tmp_path):
        """最大组取组内站点数最大值;clean 跳过单独计数。"""
        path = _write_txt(tmp_path, URLS_3DOM_8)
        grouping = FakeGrouping(
            groups=[
                FakeGroup("a.com", ["u1", "u2", "u3"], [-1, -2, -3]),
                FakeGroup("b.com", ["u4"], [-4]),
            ],
            skipped_clean=2,
        )
        result = run_flow(path, _cfg(tmp_path), scanner=FakeScanner(),
                          grouping=grouping, memory=FakeMemory())
        assert result["summary"]["max_group_size"] == 3
        assert result["summary"]["skipped_clean"] == 2

    def test_scan_errors_printed(self, tmp_path, capsys):
        """扫描失败条数在摘要中单列(失败不中断整批)。"""
        path = _write_txt(tmp_path, ["https://a.com/", "https://b.com/"])
        scanner = FakeScanner(errors=[("https://b.com/", "连接超时")])
        run_flow(path, _cfg(tmp_path), scanner=scanner,
                 grouping=FakeGrouping(groups=[]), memory=FakeMemory())
        out = capsys.readouterr().out
        assert "扫描失败" in out and "1" in out


# ---------------------------------------------------------------------------
# run_flow:trace 收官消费(V12 接线收口,A231;开关 cfg.trace_enabled)
# ---------------------------------------------------------------------------
class FakeTraceReport:
    """携带 intel 的报告替身(intel["trace_id"] 由调用方注入;A210 同款)。"""

    def __init__(self, site_url: str, intel: dict[str, Any] | None = None) -> None:
        self.site_url = site_url
        self.intel = dict(intel or {})


class TestRunFlowTraceConsume:
    @pytest.fixture(autouse=True)
    def _isolate_trace(self):
        """trace 组隔离:清遥测指标与 trace 注册表,回默认 configure。"""
        telemetry.reset()
        tt.reset()
        tt.configure()
        yield
        tt.reset()
        tt.configure()

    def test_trace_enabled_manifest_json_and_long_window_drain(
        self, tmp_path, capsys
    ):
        """开关开启:真实内核 trace → (a) 批级 trace_ids 清单;(b) 逐站
        runs/<canonical>/trace.json(span 树);(c) 批次末长窗口转存审计
        JSONL(event=trace_export 四键)后注册表清空。"""
        with tt.new_trace() as tid:
            with tt.span("scan.fetch", attrs={"stage": "fetch"}):
                with tt.span("fetch.page"):
                    pass
        url = "https://www.a.com/x"
        path = _write_txt(tmp_path, [url])
        cfg = _cfg(tmp_path)
        cfg.trace_enabled = True
        scanner = FakeScanner(reports={url: FakeTraceReport(url, {"trace_id": tid})})
        grouping = FakeGrouping(groups=[FakeGroup("a.com", [url], [-1])])

        result = run_flow(path, cfg, scanner=scanner, grouping=grouping,
                          memory=FakeMemory())

        # (a) 批级 trace 清单写入批次结果
        assert result["trace_ids"] == [tid]
        # (b) 与 finishflow A210 同款落盘:runs/<站点 canonical>/trace.json
        expected = pathlib.Path(cfg.data_dir) / "runs" / "a.com" / "trace.json"
        assert result["trace_json_paths"] == [str(expected)]
        tree = json.loads(expected.read_text(encoding="utf-8"))
        assert tree["trace_id"] == tid
        assert [n["name"] for n in tree["spans"]] == ["scan.fetch"]
        assert tree["spans"][0]["children"][0]["name"] == "fetch.page"
        # (c) 长窗口转存:审计 JSONL 收 trace_export 四键载荷,注册表清空
        audit = [
            json.loads(line)
            for line in pathlib.Path(cfg.audit_path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        exports = [r for r in audit if r.get("event") == "trace_export"]
        assert [r["trace_id"] for r in exports] == [tid]
        assert exports[0]["spans"][0]["name"] == "scan.fetch"
        assert "ts" in exports[0]
        assert tt.export_trace_json(tid)["spans"] == []
        counters = telemetry.snapshot()["counters"]
        assert counters.get("batchflow.trace_export.saved") == 1.0
        assert counters.get("batchflow.trace_export.drained") == 1.0
        assert counters.get("batchflow.trace_export.failed", 0.0) == 0.0
        out = capsys.readouterr().out
        assert "trace 落盘" in out and "trace 转存" in out

    def test_trace_disabled_zero_behavior_snapshot(self, tmp_path, capsys):
        """开关关闭(默认):即使报告携带 trace_id 也零行为——结果无 trace
        键、不写任何文件、注册表原样保留、saved/failed 零计数、输出无 trace 行。"""
        with tt.new_trace() as tid:
            with tt.span("现状.段"):
                pass
        url = "https://a.com/"
        path = _write_txt(tmp_path, [url])
        cfg = _cfg(tmp_path)
        assert cfg.trace_enabled is False
        scanner = FakeScanner(reports={url: FakeTraceReport(url, {"trace_id": tid})})

        result = run_flow(path, cfg, scanner=scanner,
                          grouping=FakeGrouping(groups=[]), memory=FakeMemory())

        assert "trace_ids" not in result and "trace_json_paths" not in result
        assert not (pathlib.Path(cfg.data_dir) / "runs").exists()
        assert not pathlib.Path(cfg.audit_path).exists()
        assert tt.export_trace_json(tid)["spans"], "开关关闭不得触碰 trace 注册表"
        counters = telemetry.snapshot()["counters"]
        assert counters.get("batchflow.trace_export.saved", 0.0) == 0.0
        assert counters.get("batchflow.trace_export.failed", 0.0) == 0.0
        assert "trace 落盘" not in capsys.readouterr().out

    def test_trace_enabled_no_trace_ids_zero_writes(self, tmp_path):
        """开关开启但报告不携带 trace_id(如注入替身):无清单、无文件、
        无 failed 计数(零行为门槛同 A210)。"""
        url = "https://a.com/"
        path = _write_txt(tmp_path, [url])
        cfg = _cfg(tmp_path)
        cfg.trace_enabled = True
        scanner = FakeScanner()  # 默认报告为普通字符串,无 intel

        result = run_flow(path, cfg, scanner=scanner,
                          grouping=FakeGrouping(groups=[]), memory=FakeMemory())

        assert "trace_ids" not in result and "trace_json_paths" not in result
        assert not (pathlib.Path(cfg.data_dir) / "runs").exists()
        assert not pathlib.Path(cfg.audit_path).exists()
        assert telemetry.snapshot()["counters"].get(
            "batchflow.trace_export.failed", 0.0
        ) == 0.0

    def test_empty_scan_zero_trace_behavior(self, tmp_path):
        """开关开启但待扫为空:不消费、不转存(注册表原样,审计零落盘)。"""
        with tt.new_trace() as tid:
            with tt.span("空批.段"):
                pass
        path = _write_txt(tmp_path, ["# 空"])
        cfg = _cfg(tmp_path)
        cfg.trace_enabled = True

        result = run_flow(path, cfg, scanner=FakeScanner(),
                          grouping=FakeGrouping(), memory=FakeMemory())

        assert "trace_ids" not in result
        assert tt.export_trace_json(tid)["spans"], "空批不得转存他处 trace"
        assert not pathlib.Path(cfg.audit_path).exists()

    def test_export_failure_warns_and_never_interrupts(
        self, tmp_path, monkeypatch, caplog
    ):
        """导出异常:中文 warning + batchflow.trace_export.failed 计数;批次
        照常完成(绝不中断),批级清单仍留档,不产生任何文件。"""
        seen: list[str] = []

        def fake_export(tid):
            seen.append(tid)
            raise RuntimeError("导出器爆炸")

        mod = types.ModuleType("netsentinel.telemetry_trace")
        mod.export_trace_json = fake_export
        mod.drain_to_sink = lambda **kw: 0
        monkeypatch.setitem(sys.modules, "netsentinel.telemetry_trace", mod)
        url = "https://a.com/"
        path = _write_txt(tmp_path, [url])
        cfg = _cfg(tmp_path)
        cfg.trace_enabled = True
        scanner = FakeScanner(reports={url: FakeTraceReport(url, {"trace_id": "deadbeefdeadbeef"})})

        with caplog.at_level("WARNING", logger="netsentinel.batchflow"):
            result = run_flow(path, cfg, scanner=scanner,
                              grouping=FakeGrouping(
                                  groups=[FakeGroup("a.com", [url], [-1])]),
                              memory=FakeMemory())

        assert seen == ["deadbeefdeadbeef"]  # 按报告携带的 trace_id 调用
        assert result["groups"] == 1  # 批次主流程照常完成
        assert result["trace_ids"] == ["deadbeefdeadbeef"]  # (a) 清单仍留档
        assert "trace_json_paths" not in result
        assert telemetry.snapshot()["counters"].get(
            "batchflow.trace_export.failed"
        ) == 1.0
        assert "trace 树落盘失败" in caplog.text
        assert "不中断批次" in caplog.text
        assert not (pathlib.Path(cfg.data_dir) / "runs").exists()

    def test_trace_module_missing_skips_and_counts(
        self, tmp_path, monkeypatch, caplog
    ):
        """telemetry_trace 缺席:落盘与长窗口转存整体跳过(落盘按待导出份数
        + 转存按 1 计 failed),中文 warning、批次不中断。"""
        monkeypatch.setitem(sys.modules, "netsentinel.telemetry_trace", None)
        urls = ["https://a.com/", "https://b.org/"]
        path = _write_txt(tmp_path, urls)
        cfg = _cfg(tmp_path)
        cfg.trace_enabled = True
        reports = {u: FakeTraceReport(u, {"trace_id": u}) for u in urls}
        scanner = FakeScanner(reports=reports)

        with caplog.at_level("WARNING", logger="netsentinel.batchflow"):
            result = run_flow(path, cfg, scanner=scanner,
                              grouping=FakeGrouping(groups=[]), memory=FakeMemory())

        assert result["groups"] == 0 and result["enqueued"] == 0  # 批次照常
        assert result["trace_ids"] == urls
        assert telemetry.snapshot()["counters"].get(
            "batchflow.trace_export.failed"
        ) == 3.0  # 2 份待落盘 + 1 次长窗口转存
        assert "trace 树导出模块未就位" in caplog.text
        assert "trace 长窗口转存失败" in caplog.text

    def test_drain_failure_warns_and_never_interrupts(
        self, tmp_path, monkeypatch, caplog
    ):
        """长窗口转存失败(审计日志器不可用):warning + failed 计数,
        批次照常完成,不产生任何文件。"""
        monkeypatch.setitem(sys.modules, "netsentinel.logging_util", None)
        url = "https://a.com/"
        path = _write_txt(tmp_path, [url])
        cfg = _cfg(tmp_path)
        cfg.trace_enabled = True

        with caplog.at_level("WARNING", logger="netsentinel.batchflow"):
            result = run_flow(path, cfg, scanner=FakeScanner(),
                              grouping=FakeGrouping(groups=[]), memory=FakeMemory())

        assert result["groups"] == 0  # 批次主流程照常完成
        assert telemetry.snapshot()["counters"].get(
            "batchflow.trace_export.failed"
        ) == 1.0
        assert "trace 长窗口转存失败" in caplog.text
        assert "不中断批次" in caplog.text
        assert not pathlib.Path(cfg.audit_path).exists()


# ---------------------------------------------------------------------------
# resume_batch:新建批次 / 续批分派
# ---------------------------------------------------------------------------
READY_ITEMS = [
    {"entry_id": 1, "group_name": "a.com", "site_url": "https://a.com/",
     "evidence_zip": "data/evidence/a.zip", "portal": "12377"},
    {"entry_id": 2, "group_name": "b.com", "site_url": "https://b.com/",
     "evidence_zip": "data/evidence/b.zip", "portal": "shdf"},
]


class TestResumeNewBatch:
    def test_new_batch_from_ready_when_no_unfinished(self, tmp_path, capsys):
        """批次无未完条目 → 按 ready 新建批次,run_batch 收到 items+绑定状态。"""
        cfg = _cfg(tmp_path)
        st = FakeState(unfinished=[])
        rb = FakeRunBatch()
        executor, p1, p2, rate = object(), object(), object(), object()
        result = resume_batch(
            7, cfg, ready=FakeReady(READY_ITEMS), state=st, submit=rb,
            executor=executor, plan_12377=p1, plan_shdf=p2, rate=rate,
        )
        assert st.resumed == [7]
        assert len(st.created) == 1
        assert st.created[0][0] == READY_ITEMS
        assert "batchflow" in st.created[0][1]
        assert st.bound_ids == [101]
        assert result["batch_id"] == 101
        assert rb.calls[0]["items"] == READY_ITEMS
        assert rb.calls[0]["cfg"] is cfg
        kw = rb.calls[0]["kwargs"]
        assert kw["state"].batch_id == 101  # 对接约定:state=st.bound(bid)
        assert kw["executor"] is executor
        assert kw["plan_12377"] is p1 and kw["plan_shdf"] is p2  # 注入参数名
        assert kw["rate"] is rate
        assert kw["dry_run"] is True  # cfg.dry_run_default 缺省
        assert st.closed == 0  # 注入状态器归调用方,不代关
        assert "新建批次 101" in capsys.readouterr().out

    def test_none_batch_id_creates_new(self, tmp_path):
        """batch_id=None → 无续批语义,直接按 ready 新建。"""
        st = FakeState(unfinished=[])
        result = resume_batch(
            None, _cfg(tmp_path), ready=FakeReady(READY_ITEMS[:1]),
            state=st, submit=FakeRunBatch(),
        )
        assert st.resumed == []  # None 不查 resume
        assert result["batch_id"] == 101


class TestResumeContinue:
    def test_continue_unfinished_merges_ready(self, tmp_path, capsys):
        """有未完条目 → 续批:批次号沿用,ready 按 entry_id 补 portal 等细节。"""
        cfg = _cfg(tmp_path)
        st = FakeState(
            unfinished=[
                {"entry_id": 5, "group_name": "x.com"},
                {"entry_id": 2, "group_name": "b.com"},
            ]
        )
        rb = FakeRunBatch()
        result = resume_batch(
            7, cfg, ready=FakeReady(READY_ITEMS), state=st, submit=rb,
        )
        assert st.created == [] and st.resumed == [7]
        assert result["batch_id"] == 7 and st.bound_ids == [7]
        items = rb.calls[0]["items"]
        assert items[0]["entry_id"] == 5
        assert items[0]["portal"] == "12377"  # ready 无匹配 → 缺省门户
        assert items[1]["entry_id"] == 2
        assert items[1]["portal"] == "shdf"  # ready 命中 → 覆盖补齐
        assert items[1]["evidence_zip"] == "data/evidence/b.zip"
        assert "续批批次 7" in capsys.readouterr().out

    def test_row_shapes_row_and_dataclass(self, tmp_path):
        """未完条目兼容 sqlite Row 形态与 dataclass 形态。"""
        st = FakeState(
            unfinished=[RowLike({"entry_id": 9, "group_name": "r.com"}),
                        RowDataclass(entry_id=10, group_name="d.com")]
        )
        rb = FakeRunBatch()
        resume_batch(3, _cfg(tmp_path), ready=FakeReady([]), state=st, submit=rb)
        items = rb.calls[0]["items"]
        assert [i["entry_id"] for i in items] == [9, 10]
        assert all(i["portal"] == "12377" for i in items)

    def test_missing_group_name_raises_chinese(self, tmp_path):
        st = FakeState(unfinished=[{"entry_id": 9}])
        with pytest.raises(ValueError, match="必填字段"):
            resume_batch(1, _cfg(tmp_path), ready=FakeReady([]), state=st,
                         submit=FakeRunBatch())


class TestResumeDryRun:
    def test_dry_run_none_uses_cfg_default_true(self, tmp_path):
        cfg = _cfg(tmp_path)  # dry_run_default 默认 True
        rb = FakeRunBatch()
        resume_batch(1, cfg, ready=FakeReady(READY_ITEMS[:1]),
                     state=FakeState(unfinished=[]), submit=rb)
        assert rb.calls[0]["kwargs"]["dry_run"] is True

    def test_dry_run_none_uses_cfg_default_false(self, tmp_path):
        cfg = _cfg(tmp_path, dry_run_default=False)
        rb = FakeRunBatch()
        resume_batch(1, cfg, ready=FakeReady(READY_ITEMS[:1]),
                     state=FakeState(unfinished=[]), submit=rb)
        assert rb.calls[0]["kwargs"]["dry_run"] is False

    def test_real_mode_requires_explicit_false(self, tmp_path, capsys):
        """真实执行必须显式 dry_run=False;打印红线 24 人工门提示。"""
        rb = FakeRunBatch()
        resume_batch(
            4, _cfg(tmp_path), ready=FakeReady(READY_ITEMS),
            state=FakeState(unfinished=[]), submit=rb, dry_run=False,
        )
        assert rb.calls[0]["kwargs"]["dry_run"] is False
        out = capsys.readouterr().out
        assert "真实执行模式" in out
        assert "人工输入验证码" in out and "无任何自动确认" in out


class TestResumeIdleAndLimits:
    def test_no_items_returns_zero_summary(self, tmp_path, capsys):
        """无未完条目且就绪为空 → 不新建、不执行,返回零值摘要。"""
        st = FakeState(unfinished=[])
        rb = FakeRunBatch()
        result = resume_batch(9, _cfg(tmp_path), ready=FakeReady([]),
                              state=st, submit=rb)
        assert rb.calls == [] and st.created == []
        assert result["batch_id"] == 9
        assert result["submitted"] == 0 and result["results"] == []
        assert "无待批量条目" in capsys.readouterr().out

    def test_over_cap_passes_through_a112_valueerror(self, tmp_path):
        """超过 batch_max_items → 真 A112 run_batch 抛中文 ValueError(红线 26)。"""
        cfg = _cfg(tmp_path, batch_max_items=2)
        items = [
            {"entry_id": i, "group_name": f"g{i}.com", "portal": "12377"}
            for i in range(1, 4)
        ]
        st = FakeState(unfinished=[])
        with pytest.raises(ValueError, match="超过单批上限"):
            resume_batch(1, cfg, ready=FakeReady(items), state=st)  # submit 缺省真 A112
        assert len(st.created) == 1  # 先建批,后由 A112 前置校验拒绝

    def test_rate_limited_prints_resume_hint(self, tmp_path, capsys):
        rb = FakeRunBatch(
            result={"submitted": 1, "failed": 0, "rate_limited": True,
                    "paused": False, "results": [], "note": "额度/间隔限制,可续批"}
        )
        # 续批路径(有未完条目)→ 批次号沿用 6,挂起提示指向 --resume 6
        resume_batch(6, _cfg(tmp_path), ready=FakeReady(READY_ITEMS[:1]),
                     state=FakeState(unfinished=READY_ITEMS[:1]), submit=rb)
        out = capsys.readouterr().out
        assert "可续批" in out
        assert "--resume 6" in out


class TestResumeStateContract:
    def test_state_missing_resume_method(self, tmp_path):
        st = FakeState(drop=("resume",))
        with pytest.raises(RuntimeError, match="resume"):
            resume_batch(1, _cfg(tmp_path), ready=FakeReady([]), state=st)

    def test_state_missing_new_batch_method(self, tmp_path):
        st = FakeState(unfinished=[], drop=("new_batch",))
        with pytest.raises(RuntimeError, match="new_batch"):
            resume_batch(1, _cfg(tmp_path), ready=FakeReady(READY_ITEMS[:1]),
                         state=st, submit=FakeRunBatch())

    def test_state_missing_bound_method(self, tmp_path):
        st = FakeState(unfinished=READY_ITEMS[:1], drop=("bound",))
        with pytest.raises(RuntimeError, match="bound"):
            resume_batch(2, _cfg(tmp_path), ready=FakeReady([]), state=st,
                         submit=FakeRunBatch())

    def test_batch_state_module_missing(self, tmp_path, monkeypatch):
        """A113 模块缺失(未注入 state)→ 中文 RuntimeError。"""
        monkeypatch.setitem(sys.modules, "netsentinel.submit.batch_state", None)
        with pytest.raises(RuntimeError, match="未就位"):
            resume_batch(1, _cfg(tmp_path), ready=FakeReady([]))


# ---------------------------------------------------------------------------
# main:CLI 参数解析与校验
# ---------------------------------------------------------------------------
class TestMainUsage:
    def test_dry_run_and_exec_exclusive(self, tmp_path, capsys):
        rc = main(["--input", "x.txt", "--dry-run", "--exec"])
        assert rc == 2
        assert "互斥" in capsys.readouterr().out

    def test_input_and_resume_exclusive(self, tmp_path, capsys):
        rc = main(["--input", "x.txt", "--resume", "1"])
        assert rc == 2
        assert "互斥" in capsys.readouterr().out

    def test_exec_requires_resume(self, capsys):
        rc = main(["--input", "x.txt", "--exec"])
        assert rc == 2
        assert "--exec 仅在 --resume" in capsys.readouterr().out

    def test_requires_input_or_resume(self, capsys):
        rc = main([])
        assert rc == 2
        assert "需要 --input" in capsys.readouterr().out


class TestMainFlow:
    def test_flow_ok_prints_summary_and_next_step(self, tmp_path, capsys):
        """空清单走通 main:rc 0,摘要 + 下一步 TUI 声明指引。"""
        cfg_path = _write_cfg_yaml(tmp_path)
        path = _write_txt(tmp_path, ["# 空"])
        rc = main(["--input", path, "--config", cfg_path])
        assert rc == 0
        out = capsys.readouterr().out
        assert "批量筛选流程摘要" in out
        assert "下一步:python -m netsentinel.cli.batch_tui 声明" in out

    def test_flow_missing_input_returns_2(self, tmp_path, capsys):
        cfg_path = _write_cfg_yaml(tmp_path)
        rc = main(["--input", str(tmp_path / "nope.txt"), "--config", cfg_path])
        assert rc == 2
        out = capsys.readouterr().out
        assert "错误" in out and "不存在" in out


class TestMainResume:
    def test_resume_defaults_to_dry_run(self, tmp_path, monkeypatch):
        """无 --exec → 恒干跑(CLI 显式 dry_run=True,不依赖 cfg 缺省)。"""
        cfg_path = _write_cfg_yaml(tmp_path)
        seen: dict[str, Any] = {}

        def fake_resume(batch_id, cfg, *, dry_run=None, **kw):
            seen.update(batch_id=batch_id, cfg=cfg, dry_run=dry_run)
            return {"batch_id": batch_id, "submitted": 0, "failed": 0,
                    "rate_limited": False, "paused": False, "results": []}

        monkeypatch.setattr("netsentinel.batchflow.resume_batch", fake_resume)
        rc = main(["--resume", "7", "--config", cfg_path])
        assert rc == 0
        assert seen["batch_id"] == 7 and seen["dry_run"] is True

    def test_resume_exec_is_real_mode(self, tmp_path, monkeypatch):
        cfg_path = _write_cfg_yaml(tmp_path)
        seen: dict[str, Any] = {}

        def fake_resume(batch_id, cfg, *, dry_run=None, **kw):
            seen.update(batch_id=batch_id, dry_run=dry_run)
            return {"batch_id": batch_id, "submitted": 0, "failed": 0,
                    "rate_limited": False, "paused": False, "results": []}

        monkeypatch.setattr("netsentinel.batchflow.resume_batch", fake_resume)
        rc = main(["--resume", "7", "--exec", "--config", cfg_path])
        assert rc == 0 and seen["dry_run"] is False

    def test_resume_sibling_missing_returns_3(self, tmp_path, monkeypatch, capsys):
        """A113 缺失经 main → rc 3,中文错误(ready 走真 A110 空清单)。"""
        monkeypatch.setitem(sys.modules, "netsentinel.submit.batch_state", None)
        cfg_path = _write_cfg_yaml(tmp_path)
        rc = main(["--resume", "5", "--config", cfg_path])
        assert rc == 3
        out = capsys.readouterr().out
        assert "错误" in out and "未就位" in out


class TestEntrypoint:
    def test_python_m_entrypoint(self):
        """python -m netsentinel.batchflow 可运行:无参 → rc 2 + 中文用法错误。"""
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable, "-m", "netsentinel.batchflow"],
            capture_output=True, cwd=str(ROOT), env=env, timeout=120,
        )
        assert proc.returncode == 2
        out = proc.stdout.decode("utf-8", errors="replace")
        assert "需要 --input" in out
