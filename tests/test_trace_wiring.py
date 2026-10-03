"""A203 trace 贯通接线测试(service/app.py + netsentinel/ops/pool.py)。

第三波 telemetry_trace(new_trace/span/propagate/restore/export_trace_json/
configure)在服务层与线程池边界的接线行为,全部离线、确定性:

- 服务全链:POST /scan 响应头 ``X-Trace-Id`` → 任务 dict 记 trace_id(经
  GET /trace/{job_id} 读出证明)→ /trace 端点返回嵌套 span 树
  (scan.submit → scan.execute → 业务子 span);
- worker 内上下文:propagate/restore 生效——fake 扫描执行时的
  current_trace_id/current_span_id 与主链一致,子 span 正确挂树;
- 开关关闭=现状:响应无 X-Trace-Id 头、响应体/任务端点字段不变、worker
  无 trace 上下文、零 span 计数(模块常量与 create_app 参数两种关法);
- GET /trace 分支:任务不存在 404;任务无 trace(开关关闭)空树 200;
- audit_sink 惰性落盘:首个 span 才建 cfg.audit_path 的 JSONL,事件七键
  + ts、trace_id 与响应头一致、parent 链逐级正确;
- 失败任务:scan.execute span 记 error("类型: 消息")后任务照常收敛 failed;
- 线程池(ops.pool):propagate_trace 默认开——活跃 trace 下提交
  _scan_one_traced、worker 首行 restore 挂树;无活跃 trace / 开关关闭提交
  _scan_one 原样(经记录式 executor 断言),零行为变化;
- batchflow(A231):run_flow 收官消费 intel["trace_id"]——批级清单 +
  runs/<站点>/trace.json + 批次末 drain_to_sink 长窗口转存审计 JSONL;
  开关关闭注册表原样、零文件(现状)。

fastapi / httpx 缺失时服务组整体 skip(importorskip);池组不依赖它们。
"""
from __future__ import annotations

import json
import pathlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, Callable

import pytest

fastapi = pytest.importorskip("fastapi")  # noqa: F841 - 服务组整体 skip 哨兵
pytest.importorskip("httpx")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

import service.app as service_app  # noqa: E402
import netsentinel.ops.pool as pool_mod  # noqa: E402
from netsentinel import telemetry  # noqa: E402
from netsentinel import telemetry_trace as tt  # noqa: E402
from netsentinel.contracts import Config, Verdict  # noqa: E402
from netsentinel.ops.pool import run_pool  # noqa: E402

SITE = "http://localhost/trace/a"
POOL_SITE = "https://pool-a.example.com/"


@pytest.fixture(autouse=True)
def _isolate():
    """测试隔离:清 telemetry 指标与 trace 注册表,回默认 configure。

    注意在 client(创建 app → 接 audit_sink)**之前**执行 setup,teardown
    再还原,避免模块级 sink 跨用例串写。
    """
    telemetry.reset()
    tt.reset()
    tt.configure()  # 回默认:audit_sink=None(零副作用)+ 默认 id 工厂
    yield
    tt.reset()
    tt.configure()


# ---------------------------------------------------------------------------
# 共用 fakes / 小工具
# ---------------------------------------------------------------------------
class TracedFakeScan:
    """记录执行时 trace/span 上下文并开一个业务子 span 的同步 fake。

    fake 在线程池 worker 内被调用:记录到的上下文即 restore 之后的上下文,
    是"propagate/restore 贯通线程边界"的直接证据。业务子 span ``scan.mock``
    仅在上下文确有 trace 时开(开关关闭的链路上开 span 会另起隐式 trace,
    恰好用来断言"关=现状:零 span")。``error`` 非空时在子 span 体内抛
    RuntimeError(验证 span 记 error 后异常照常收敛 job failed)。
    """

    def __init__(self, *, error: str | None = None) -> None:
        self.error = error
        self.trace_ids: list[str | None] = []
        self.span_ids: list[str | None] = []
        self.calls: list[str] = []

    def __call__(self, url: str, cfg: Config) -> Any:
        self.calls.append(url)
        self.trace_ids.append(tt.current_trace_id())
        self.span_ids.append(tt.current_span_id())
        if tt.current_trace_id() is not None:
            with tt.span("scan.mock", attrs={"stage": "mock"}):
                if self.error is not None:
                    raise RuntimeError(self.error)
        elif self.error is not None:
            raise RuntimeError(self.error)
        return SimpleNamespace(
            site_url=url,
            agg_nsw_prob=0.42,
            nsw_image_count=1,
            verdict=Verdict.SUSPECT,
            needs_review=False,  # 不写复核队列,测试零落盘依赖
        )


@pytest.fixture()
def cfg(tmp_path: Any) -> Config:
    """数据路径全部指向 tmp 的配置(audit 落盘可查,不碰真实 data/)。"""
    data = tmp_path / "data"
    c = Config()
    c.data_dir = str(data)
    c.evidence_dir = str(data / "evidence")
    c.db_path = str(data / "review_queue.db")
    c.audit_path = str(data / "audit.jsonl")
    c.log_path = str(data / "logs" / "netsentinel.log")
    c.vlm_cache_db = str(data / "vlm_cache.db")
    return c


@pytest.fixture()
def client(cfg: Config) -> Any:
    return TestClient(service_app.create_app(cfg))


@pytest.fixture()
def traced_scan(monkeypatch: Any) -> TracedFakeScan:
    """把扫描执行函数替换为带上下文记录的同步 fake(经 _get_run_scan 缝)。"""
    fake = TracedFakeScan()
    monkeypatch.setattr(service_app, "_get_run_scan", lambda: fake)
    return fake


def wait_job(client: Any, job_id: str, timeout: float = 10.0) -> dict[str, Any]:
    """轮询任务直到 done / failed(带超时),返回最后一次响应体。"""
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if last.get("status") != "running":
            return last
        time.sleep(0.02)
    pytest.fail(f"扫描任务 {job_id} 在 {timeout}s 内未结束,最后状态:{last}")


def flatten_nodes(spans: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """把 /trace 返回的嵌套树按 name 摊平(name 在本文件用例内互异)。"""
    found: dict[str, dict[str, Any]] = {}

    def _walk(node: dict[str, Any]) -> None:
        found[node["name"]] = node
        for child in node["children"]:
            _walk(child)

    for root in spans:
        _walk(root)
    return found


# ---------------------------------------------------------------------------
# 服务全链:X-Trace-Id → 任务 dict → /trace 树
# ---------------------------------------------------------------------------
def test_scan_trace_full_chain_header_jobdict_to_tree(
    client: Any, traced_scan: TracedFakeScan
) -> None:
    resp = client.post("/scan", json={"url": SITE})
    assert resp.status_code == 202
    header_id = resp.headers.get("X-Trace-Id")
    assert header_id is not None, "POST /scan 必须返回 X-Trace-Id 响应头"
    assert re.fullmatch(r"[0-9a-f]{16}", header_id)
    body = resp.json()
    assert set(body) == {"job_id", "status"}  # 响应体字段不因接线而扩大
    job_id = body["job_id"]

    job = wait_job(client, job_id)
    assert job["status"] == "done"

    # worker 内上下文:restore 生效(fake 执行时就挂在同一条 trace 上,
    # 且当前 span 即 scan.execute——span 包装了 run_scan 调用)
    assert traced_scan.calls == [SITE]
    assert traced_scan.trace_ids == [header_id]
    execute_span_id = traced_scan.span_ids[0]
    assert execute_span_id is not None

    # /trace 端点:从任务 dict 读 trace_id(任务存在→非 404→必为 dict 记录值),
    # 返回嵌套树 scan.submit → scan.execute → scan.mock
    tree_resp = client.get(f"/trace/{job_id}")
    assert tree_resp.status_code == 200
    tree = tree_resp.json()
    assert tree["trace_id"] == header_id
    assert isinstance(tree["started_ts"], str) and "T" in tree["started_ts"]
    assert len(tree["spans"]) == 1, "同 trace 内首个 span 应为唯一根"
    root = tree["spans"][0]
    assert root["name"] == "scan.submit"
    assert root["parent_span_id"] is None
    assert root["attrs"] == {"stage": "submit"}
    assert root["error"] is None
    assert root["duration_ms"] is not None and root["duration_ms"] >= 0.0
    (execute,) = root["children"]
    assert execute["name"] == "scan.execute"
    assert execute["span_id"] == execute_span_id
    assert execute["parent_span_id"] == root["span_id"]
    assert execute["attrs"] == {"stage": "execute"}
    (mock,) = execute["children"]
    assert mock["name"] == "scan.mock"
    assert mock["parent_span_id"] == execute["span_id"]
    # 任务终态在 span 关闭后写回:done 时全树必为终态(无进行中 None)
    for node in (root, execute, mock):
        assert node["duration_ms"] is not None and node["duration_ms"] >= 0.0


def test_trace_endpoint_404_for_unknown_job(client: Any) -> None:
    resp = client.get("/trace/does-not-exist")
    assert resp.status_code == 404
    assert "不存在" in resp.json()["detail"]


def test_trace_disabled_empty_tree_200(
    cfg: Config, traced_scan: TracedFakeScan
) -> None:
    """任务存在但无 trace(开关关闭):空树 200,而非误读环境 trace。"""
    with TestClient(service_app.create_app(cfg, trace_enabled=False)) as off:
        job_id = off.post("/scan", json={"url": SITE}).json()["job_id"]
        assert wait_job(off, job_id)["status"] == "done"
        tree = off.get(f"/trace/{job_id}").json()
    assert tree == {"trace_id": None, "started_ts": None, "spans": []}


# ---------------------------------------------------------------------------
# 开关关闭=现状(无 X-Trace-Id 头、行为快照一致)
# ---------------------------------------------------------------------------
def test_trace_disabled_via_param_matches_status_quo(
    cfg: Config, traced_scan: TracedFakeScan
) -> None:
    telemetry.reset()
    with TestClient(service_app.create_app(cfg, trace_enabled=False)) as off:
        resp = off.post("/scan", json={"url": SITE})
        assert resp.status_code == 202
        assert "x-trace-id" not in {k.lower() for k in resp.headers.keys()}
        body = resp.json()
        assert set(body) == {"job_id", "status"} and body["status"] == "running"

        job = wait_job(off, body["job_id"])
        assert set(job) == {"status", "result"}  # /jobs 响应字段不因接线扩大
        assert job["result"]["verdict"] == "suspect"
        assert job["result"]["entry_id"] is None

    # 零 trace 副作用:worker 无上下文、全进程零 span 计数、零审计落盘
    assert traced_scan.trace_ids == [None]
    assert telemetry.snapshot()["counters"].get("trace.span", 0.0) == 0.0
    assert not pathlib.Path(cfg.audit_path).exists()


def test_trace_disabled_via_module_flag(
    cfg: Config, traced_scan: TracedFakeScan, monkeypatch: Any
) -> None:
    """monkeypatch 模块常量 TRACE_ENABLED=False 与参数关法等效。"""
    monkeypatch.setattr(service_app, "TRACE_ENABLED", False)
    with TestClient(service_app.create_app(cfg)) as off:
        resp = off.post("/scan", json={"url": SITE})
        assert "x-trace-id" not in {k.lower() for k in resp.headers.keys()}
        assert wait_job(off, resp.json()["job_id"])["status"] == "done"
    assert traced_scan.trace_ids == [None]


def test_health_untouched_by_wiring(client: Any) -> None:
    # /health 不接线:无响应头、响应体恒定
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "version": "v2"}
    assert "x-trace-id" not in {k.lower() for k in resp.headers.keys()}


# ---------------------------------------------------------------------------
# audit_sink 惰性接线:首个 span 才建 JSONL,七键载荷 + ts
# ---------------------------------------------------------------------------
def test_audit_sink_lazy_jsonl_and_payload(
    cfg: Config, traced_scan: TracedFakeScan
) -> None:
    audit_path = pathlib.Path(cfg.audit_path)
    with TestClient(service_app.create_app(cfg)) as c:
        assert not audit_path.exists(), "惰性接线:未扫描前不得建审计文件"
        resp = c.post("/scan", json={"url": SITE})
        header_id = resp.headers["X-Trace-Id"]
        assert wait_job(c, resp.json()["job_id"])["status"] == "done"
    assert audit_path.exists(), "首个 span 落点应已建 cfg.audit_path"

    records = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    spans = [rec for rec in records if rec.get("event") == "span"]
    # span 结束顺序存在调度竞态(submit 与 worker 并行),只断言集合与链
    assert sorted(rec["name"] for rec in spans) == [
        "scan.execute", "scan.mock", "scan.submit",
    ]
    for rec in spans:
        assert rec["trace_id"] == header_id
        assert set(rec) == {
            "event", "trace_id", "span_id", "parent",
            "name", "duration_ms", "error", "ts",
        }
        assert rec["error"] is None
        assert rec["duration_ms"] is not None and rec["duration_ms"] >= 0.0
    by_name = {rec["name"]: rec for rec in spans}
    assert by_name["scan.submit"]["parent"] is None
    assert by_name["scan.execute"]["parent"] == by_name["scan.submit"]["span_id"]
    assert by_name["scan.mock"]["parent"] == by_name["scan.execute"]["span_id"]


# ---------------------------------------------------------------------------
# 失败任务:span 记 error 后照常收敛 failed
# ---------------------------------------------------------------------------
def test_failed_scan_span_records_error(client: Any, monkeypatch: Any) -> None:
    fake = TracedFakeScan(error="分类器全军覆没")
    monkeypatch.setattr(service_app, "_get_run_scan", lambda: fake)

    job_id = client.post("/scan", json={"url": SITE}).json()["job_id"]
    job = wait_job(client, job_id)
    assert job["status"] == "failed"
    assert "扫描失败" in job["error"] and "分类器" in job["error"]

    tree = client.get(f"/trace/{job_id}").json()
    nodes = flatten_nodes(tree["spans"])
    assert set(nodes) == {"scan.submit", "scan.execute", "scan.mock"}
    assert nodes["scan.execute"]["error"] == "RuntimeError: 分类器全军覆没"
    assert nodes["scan.mock"]["error"] == "RuntimeError: 分类器全军覆没"
    assert nodes["scan.submit"]["error"] is None  # 受理段本身无异常


# ---------------------------------------------------------------------------
# 线程池边界(ops.pool):propagate 快照 → worker 首行 restore
# ---------------------------------------------------------------------------
class RecordingExecutor:
    """记录每次 submit 的任务函数名,转交真实单线程池执行(注入形态)。"""

    def __init__(self) -> None:
        self._ex = ThreadPoolExecutor(max_workers=1)
        self.submitted: list[str] = []

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        self.submitted.append(getattr(fn, "__name__", str(fn)))
        return self._ex.submit(fn, *args, **kwargs)

    def shutdown(self) -> None:
        self._ex.shutdown(wait=True)


class PlainMemory:
    """A33 SiteMemory 契约最小 fake(去重关闭:指纹恒空 → 全部按必扫)。"""

    def last_fingerprint(self, site_url: str) -> str:
        return ""

    def fingerprint(self, report: Any) -> str:
        return ""

    def remember(self, site_url: str, fp: str) -> None:
        return None


def pool_cfg(tmp_path: Any) -> Config:
    data = tmp_path / "pooldata"
    return Config(
        data_dir=str(data),
        db_path=str(data / "review_queue.db"),
        audit_path=str(data / "audit.jsonl"),
        log_path=str(data / "logs" / "netsentinel.log"),
    )


def make_pool_report(url: str) -> SimpleNamespace:
    return SimpleNamespace(
        site_url=url, verdict="clean", agg_nsw_prob=0.0, needs_review=False
    )


def test_pool_default_on_restores_snapshot_into_worker(tmp_path: Any) -> None:
    """默认开 + 调用方有活跃 trace:提交 _scan_one_traced,worker 挂同一树。"""
    cfg = pool_cfg(tmp_path)
    ex = RecordingExecutor()
    seen: list[str | None] = []

    def scan(url: str, cfg_: Config) -> Any:
        seen.append(tt.current_trace_id())
        with tt.span("池.单站", attrs={"stage": "scan"}):
            pass
        return make_pool_report(url)

    with tt.new_trace() as tid:
        with tt.span("池.批次") as batch_span:
            summary = run_pool(
                cfg, [POOL_SITE], run_scan=scan, memory=PlainMemory(),
                executor=ex, sleep=lambda seconds: None, jitter=lambda: 0.0,
            )

    assert summary["done"] == 1 and summary["failed"] == 0
    assert ex.submitted == ["_scan_one_traced"]
    assert seen == [tid]  # worker 首行 restore 后与主线程同一条 trace
    tree = tt.export_trace_json(tid)
    assert len(tree["spans"]) == 1
    root = tree["spans"][0]
    assert root["name"] == "池.批次"
    (child,) = root["children"]
    assert child["name"] == "池.单站"
    assert child["parent_span_id"] == batch_span.span_id  # 快照时刻 span 为父
    ex.shutdown()


def test_pool_no_active_trace_submits_plain_scan_one(tmp_path: Any) -> None:
    """默认开但调用方无活跃 trace:无快照 → 提交 _scan_one 原样,零行为变化。"""
    cfg = pool_cfg(tmp_path)
    ex = RecordingExecutor()
    seen: list[str | None] = []

    def scan(url: str, cfg_: Config) -> Any:
        seen.append(tt.current_trace_id())
        assert tt.current_span_id() is None  # worker 从空上下文起步(现状)
        return make_pool_report(url)

    telemetry.reset()
    summary = run_pool(
        cfg, [POOL_SITE], run_scan=scan, memory=PlainMemory(),
        executor=ex, sleep=lambda seconds: None, jitter=lambda: 0.0,
    )

    assert summary["done"] == 1
    assert ex.submitted == ["_scan_one"]  # 未包装:提交物与接线前逐字一致
    assert seen == [None]
    assert telemetry.snapshot()["counters"].get("trace.span", 0.0) == 0.0
    ex.shutdown()


def test_pool_switch_off_keeps_status_quo_with_active_trace(tmp_path: Any) -> None:
    """propagate_trace=False:即使调用方有活跃 trace 也不注入快照(现状)。"""
    cfg = pool_cfg(tmp_path)
    ex = RecordingExecutor()
    seen: list[str | None] = []

    def scan(url: str, cfg_: Config) -> Any:
        seen.append(tt.current_trace_id())
        return make_pool_report(url)

    with tt.new_trace() as tid:
        summary = run_pool(
            cfg, [POOL_SITE], run_scan=scan, memory=PlainMemory(),
            executor=ex, sleep=lambda seconds: None, jitter=lambda: 0.0,
            propagate_trace=False,
        )

    assert summary["done"] == 1
    assert ex.submitted == ["_scan_one"]
    assert seen == [None]  # worker 未被接入主线程 trace
    assert tt.export_trace_json(tid)["spans"] == []  # 主 trace 上零挂载
    ex.shutdown()


def test_pool_default_on_is_default_parameter(tmp_path: Any) -> None:
    """run_pool 签名默认值即 True(开关存在且默认开,调用方可显式关闭)。"""
    import inspect

    sig = inspect.signature(pool_mod.run_pool)
    assert sig.parameters["propagate_trace"].default is True


# ---------------------------------------------------------------------------
# batchflow 侧接线(A231):run_flow 收官消费 intel["trace_id"] + 长窗口转存
# ---------------------------------------------------------------------------
class TracedSiteReport:
    """携带 intel 的报告鸭子替身(trace_id 由调用方注入)。"""

    def __init__(self, site_url: str, intel: dict[str, Any]) -> None:
        self.site_url = site_url
        self.intel = dict(intel)


def batchflow_cfg(tmp_path: Any) -> Config:
    """batchflow 用例的离线配置(数据路径全指向 tmp)。"""
    data = tmp_path / "bfdata"
    return Config(
        data_dir=str(data),
        db_path=str(data / "q.db"),
        audit_path=str(data / "audit.jsonl"),
    )


def test_batchflow_run_flow_consumes_trace_and_drains_long_window(
    tmp_path: Any,
) -> None:
    """真实内核端到端:扫描替身携带 trace_id → 批级清单 + 逐站 trace.json
    (span 树嵌套结构)+ 批次末长窗口转存审计 JSONL 后注册表清空。"""
    tt.reset()
    telemetry.reset()
    from netsentinel.batchflow import run_flow

    cfg = batchflow_cfg(tmp_path)
    cfg.trace_enabled = True
    with tt.new_trace() as tid:
        with tt.span("scan.fetch", attrs={"stage": "fetch"}):
            with tt.span("fetch.page"):
                pass

    url = "https://wiring.example.com/"
    leads = tmp_path / "leads.txt"
    leads.write_text(url + "\n", encoding="utf-8")
    reports = {url: TracedSiteReport(url, {"trace_id": tid})}
    scanner = lambda urls, cfg_, **kw: {  # noqa: E731 - 离线替身
        "reports": dict(reports),
        "summary": {"errors": []},
    }
    grouping = lambda cfg_, *, reports=None, **kw: {  # noqa: E731
        "groups": [], "enqueued": 0, "skipped_clean": 0,
    }

    result = run_flow(
        str(leads), cfg, scanner=scanner, grouping=grouping, memory=PlainMemory()
    )

    assert result["trace_ids"] == [tid]
    # 站点标识 = canonical 键(wiring.example.com 归并为 example.com)
    expected = pathlib.Path(cfg.data_dir) / "runs" / "example.com" / "trace.json"
    assert result["trace_json_paths"] == [str(expected)]
    tree = json.loads(expected.read_text(encoding="utf-8"))
    assert tree["trace_id"] == tid
    assert tree["spans"][0]["name"] == "scan.fetch"
    assert tree["spans"][0]["children"][0]["name"] == "fetch.page"
    # 长窗口转存:审计 JSONL 的 trace_export 事件携带同一棵树,注册表清空
    audit_lines = pathlib.Path(cfg.audit_path).read_text(encoding="utf-8").splitlines()
    exports = [
        json.loads(line)
        for line in audit_lines
        if line.strip() and json.loads(line).get("event") == "trace_export"
    ]
    assert [e["trace_id"] for e in exports] == [tid]
    assert exports[0]["spans"][0]["name"] == "scan.fetch"
    assert tt.export_trace_json(tid)["spans"] == []


def test_batchflow_trace_disabled_keeps_registry_and_zero_files(
    tmp_path: Any,
) -> None:
    """开关关闭 = 现状:批次不消费 trace_id、不转存(注册表原样)、零文件。"""
    tt.reset()
    telemetry.reset()
    from netsentinel.batchflow import run_flow

    with tt.new_trace() as tid:
        with tt.span("现状.批次"):
            pass
    cfg = batchflow_cfg(tmp_path)
    cfg.trace_enabled = False
    url = "https://wiring.example.com/"
    leads = tmp_path / "leads.txt"
    leads.write_text(url + "\n", encoding="utf-8")
    reports = {url: TracedSiteReport(url, {"trace_id": tid})}
    scanner = lambda urls, cfg_, **kw: {  # noqa: E731 - 离线替身
        "reports": dict(reports),
        "summary": {"errors": []},
    }
    grouping = lambda cfg_, *, reports=None, **kw: {  # noqa: E731
        "groups": [], "enqueued": 0, "skipped_clean": 0,
    }

    result = run_flow(
        str(leads), cfg, scanner=scanner, grouping=grouping, memory=PlainMemory()
    )

    assert "trace_ids" not in result and "trace_json_paths" not in result
    assert not (pathlib.Path(cfg.data_dir) / "runs").exists()
    assert not pathlib.Path(cfg.audit_path).exists()
    assert tt.export_trace_json(tid)["spans"], "开关关闭注册表原样保留"
    counters = telemetry.snapshot()["counters"]
    assert counters.get("batchflow.trace_export.saved", 0.0) == 0.0
    assert counters.get("batchflow.trace_export.failed", 0.0) == 0.0
