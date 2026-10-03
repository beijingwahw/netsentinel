"""A31 本机 REST 服务(service/app.py)离线单元测试。

全部离线,零网络:
- fastapi / httpx 为可选依赖,缺失时整体 skip(importorskip);
  uvicorn 不需要安装(仅 ``main`` 用到,本文件不测真实启动);
- 扫描执行体经 ``service.app._get_run_scan`` 缝替换为同步 fake,
  fake 在后台线程内直接完成(需要时模拟 orchestrator 写入复核队列);
- 复核队列 / 计划生成使用真实第一轮模块,但数据库与数据路径全部指向
  ``tmp_path``,绝不访问真实举报门户,不发起任何网络请求。
"""
from __future__ import annotations

import time
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")
testclient_mod = pytest.importorskip("fastapi.testclient")
TestClient = testclient_mod.TestClient

import service.app as service_app  # noqa: E402
from netsentinel import telemetry  # noqa: E402
from netsentinel.contracts import Config, SiteReport, Verdict  # noqa: E402
from netsentinel.decision.review_queue import ReviewQueue  # noqa: E402

SITE = "http://localhost/demo/a"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def cfg(tmp_path: Any) -> Config:
    """数据路径全部指向 tmp 的配置(不落任何真实 data/ 目录)。"""
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


class FakeRunScan:
    """同步 fake 扫描:模拟 orchestrator(needs_review 时写入复核队列)。

    ``error`` 非空时抛 RuntimeError,用于验证失败任务路径。
    """

    def __init__(
        self,
        *,
        verdict: Verdict = Verdict.NSFW,
        agg: float = 0.97,
        nsw_count: int = 4,
        needs_review: bool = True,
        error: str | None = None,
    ) -> None:
        self.verdict = verdict
        self.agg = agg
        self.nsw_count = nsw_count
        self.needs_review = needs_review
        self.error = error
        self.calls: list[str] = []
        self.entry_ids: dict[str, int] = {}

    def __call__(self, url: str, cfg: Config) -> SiteReport:
        self.calls.append(url)
        if self.error is not None:
            raise RuntimeError(self.error)
        report = SiteReport(
            site_url=url,
            agg_nsw_prob=self.agg,
            nsw_image_count=self.nsw_count,
            verdict=self.verdict,
            needs_review=self.needs_review,
        )
        if self.needs_review:
            queue = ReviewQueue(cfg.db_path)
            try:
                self.entry_ids[url] = queue.add(report, "fake/evidence/bundle.zip")
            finally:
                queue.close()
        return report


@pytest.fixture()
def fake_scan(monkeypatch: Any) -> FakeRunScan:
    """把扫描执行函数替换为同步 fake(经 _get_run_scan 缝,零网络)。"""
    fake = FakeRunScan()
    monkeypatch.setattr(service_app, "_get_run_scan", lambda: fake)
    return fake


def make_entry(cfg: Config, site: str = SITE, verdict: str = "nsfw") -> int:
    """直接向复核队列塞一条 pending 条目,返回 id。"""
    queue = ReviewQueue(cfg.db_path)
    try:
        return queue.add(
            SiteReport(site_url=site, verdict=Verdict(verdict)), "fake/evidence/b.zip"
        )
    finally:
        queue.close()


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


# ---------------------------------------------------------------------------
# 健康检查与工厂
# ---------------------------------------------------------------------------
def test_health(client: Any) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "version": "v2"}


def test_create_app_returns_fastapi_with_routes() -> None:
    app = service_app.create_app()
    paths = {getattr(r, "path", "") for r in app.routes}
    for expected in ("/scan", "/jobs/{job_id}", "/trace/{job_id}", "/queue",
                     "/queue/{entry_id}/approve", "/queue/{entry_id}/reject",
                     "/plan/{entry_id}", "/health"):
        assert expected in paths


def test_plan_notice_constant_mentions_no_submit() -> None:
    # 红线:计划响应的 notice 必须声明本服务不执行提交
    assert "不执行提交" in service_app.PLAN_NOTICE
    assert "人工" in service_app.PLAN_NOTICE


# ---------------------------------------------------------------------------
# POST /scan + GET /jobs
# ---------------------------------------------------------------------------
def test_scan_needs_review_flow(client: Any, fake_scan: FakeRunScan) -> None:
    resp = client.post("/scan", json={"url": SITE})
    assert resp.status_code == 202
    body = resp.json()
    assert set(body) == {"job_id", "status"}
    assert body["status"] == "running"
    job_id = body["job_id"]

    job = wait_job(client, job_id)
    assert job["status"] == "done"
    result = job["result"]
    for key in ("verdict", "agg", "nsw_image_count", "entry_id", "needs_review"):
        assert key in result
    assert result["verdict"] == "nsfw"
    assert result["agg"] == pytest.approx(0.97)
    assert result["nsw_image_count"] == 4
    assert result["needs_review"] is True
    assert result["entry_id"] == fake_scan.entry_ids[SITE]
    assert fake_scan.calls == [SITE]

    # 入列的条目可经队列端点看到(pending,等人工拍板)
    entries = client.get("/queue").json()["entries"]
    assert [e["id"] for e in entries] == [fake_scan.entry_ids[SITE]]
    assert entries[0]["status"] == "pending"


def test_scan_clean_has_no_entry(client: Any, fake_scan: FakeRunScan) -> None:
    fake_scan.verdict = Verdict.CLEAN
    fake_scan.agg = 0.01
    fake_scan.nsw_count = 0
    fake_scan.needs_review = False

    job_id = client.post("/scan", json={"url": SITE}).json()["job_id"]
    job = wait_job(client, job_id)
    assert job["status"] == "done"
    assert job["result"]["needs_review"] is False
    assert job["result"]["entry_id"] is None
    assert client.get("/queue").json()["summary"]["pending"] == 0


def test_scan_failure_marks_job_failed(client: Any, monkeypatch: Any) -> None:
    fake = FakeRunScan(error="没有任何分类器成员成功完成评分")
    monkeypatch.setattr(service_app, "_get_run_scan", lambda: fake)

    job_id = client.post("/scan", json={"url": SITE}).json()["job_id"]
    job = wait_job(client, job_id)
    assert job["status"] == "failed"
    assert "扫描失败" in job["error"]
    assert "分类器" in job["error"]
    assert "result" not in job


def test_scan_invalid_url_returns_422_chinese(client: Any) -> None:
    for bad in ("not-a-url", "ftp://localhost/x", "http://", ""):
        resp = client.post("/scan", json={"url": bad})
        assert resp.status_code == 422, bad
        assert "url" in resp.json()["detail"]


def test_jobs_unknown_id_404(client: Any) -> None:
    resp = client.get("/jobs/does-not-exist")
    assert resp.status_code == 404
    assert "不存在" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /queue
# ---------------------------------------------------------------------------
def test_queue_list_and_summary(client: Any, cfg: Config) -> None:
    make_entry(cfg, "http://localhost/1")
    make_entry(cfg, "http://localhost/2")

    resp = client.get("/queue")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"entries", "summary"}
    assert len(body["entries"]) == 2
    entry = body["entries"][0]
    for key in ("id", "site_url", "verdict", "status", "evidence_zip",
                "created_at", "updated_at", "note"):
        assert key in entry
    assert body["summary"] == {
        "pending": 2, "approved": 0, "rejected": 0, "submitted": 0,
    }


# ---------------------------------------------------------------------------
# approve / reject
# ---------------------------------------------------------------------------
def test_queue_approve_then_conflict_409(client: Any, cfg: Config) -> None:
    entry_id = make_entry(cfg)

    resp = client.post(f"/queue/{entry_id}/approve")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"ok", "entry"}
    assert body["ok"] is True
    assert body["entry"]["id"] == entry_id
    assert body["entry"]["status"] == "approved"

    # 已 approved 再 approve:非法迁移 → 409 中文错误
    resp = client.post(f"/queue/{entry_id}/approve")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "复核条目" in detail or "人工确认" in detail


def test_queue_reject_with_note(client: Any, cfg: Config) -> None:
    entry_id = make_entry(cfg)

    resp = client.post(f"/queue/{entry_id}/reject", json={"note": "误报,内容正常"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["entry"]["status"] == "rejected"
    assert body["entry"]["note"] == "误报,内容正常"


def test_queue_transition_missing_id_404(client: Any) -> None:
    assert client.post("/queue/999/approve").status_code == 404
    assert client.post("/queue/999/reject", json={"note": "x"}).status_code == 404


# ---------------------------------------------------------------------------
# POST /plan/{id}
# ---------------------------------------------------------------------------
def test_plan_requires_approved_409(client: Any, cfg: Config) -> None:
    entry_id = make_entry(cfg)  # pending
    resp = client.post(f"/plan/{entry_id}", json={"portal": "12377"})
    assert resp.status_code == 409
    assert "approved" in resp.json()["detail"]


def test_plan_missing_entry_404(client: Any) -> None:
    resp = client.post("/plan/424242", json={"portal": "12377"})
    assert resp.status_code == 404


def test_plan_12377_full_response(client: Any, cfg: Config) -> None:
    entry_id = make_entry(cfg)
    assert client.post(f"/queue/{entry_id}/approve").status_code == 200

    resp = client.post(f"/plan/{entry_id}", json={"portal": "12377"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"plan", "playbook_md", "notice"}

    plan = body["plan"]
    assert plan["portal"] == "12377"
    assert plan["payload"]["site_url"] == SITE
    assert plan["entry_url"].startswith("https://")
    actions = [s["action"] for s in plan["steps"]]
    assert "goto" in actions
    assert "human_gate" in actions  # 红线:计划必须含人工门
    assert "captcha" not in {s.get("selector") for s in plan["steps"]
                             if s["action"] in ("fill", "select", "click")}

    md = body["playbook_md"]
    assert "举报 Playbook" in md
    assert "人工" in md  # 人工门整行标注
    assert "不执行提交" in body["notice"]
    assert "CLI" in body["notice"]


def test_plan_shdf_and_invalid_portal(client: Any, cfg: Config) -> None:
    entry_id = make_entry(cfg)
    assert client.post(f"/queue/{entry_id}/approve").status_code == 200

    resp = client.post(f"/plan/{entry_id}", json={"portal": "shdf"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan"]["portal"] == "shdf"
    assert body["plan"]["payload"]["category"] == "淫秽色情类"
    assert "举报 Playbook" in body["playbook_md"]

    resp = client.post(f"/plan/{entry_id}", json={"portal": "999"})
    assert resp.status_code == 422
    assert "portal" in resp.json()["detail"]


def test_rejected_entry_cannot_make_plan_409(client: Any, cfg: Config) -> None:
    entry_id = make_entry(cfg)
    assert client.post(f"/queue/{entry_id}/reject", json={"note": "误报"}).status_code == 200
    resp = client.post(f"/plan/{entry_id}", json={"portal": "12377"})
    assert resp.status_code == 409
    assert "approved" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# V5 升级(A101):扫描线程池复用 + service.* 遥测
# ---------------------------------------------------------------------------
def test_v5_scan_pool_constant_is_two_workers() -> None:
    # 池大小提为模块常量(魔法数字上移),并与文档声明一致
    assert service_app.SCAN_MAX_WORKERS == 2


def test_v5_scan_submitted_via_shared_pool(
    client: Any, fake_scan: FakeRunScan, monkeypatch: Any
) -> None:
    submits: list[tuple[Any, ...]] = []
    real_pool = service_app._get_scan_pool()

    class RecordingPool:
        """记录 submit 调用后转交真实线程池执行的替身。"""

        def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
            submits.append(args)
            return real_pool.submit(fn, *args, **kwargs)

    monkeypatch.setattr(service_app, "_get_scan_pool", RecordingPool)

    job_id = client.post("/scan", json={"url": SITE}).json()["job_id"]
    job = wait_job(client, job_id)
    assert job["status"] == "done"
    assert len(submits) == 1            # 每个请求恰一次池提交(不再新建线程)
    assert submits[0][0] == job_id      # 提交参数为 (job_id, url)
    assert submits[0][1] == SITE


def test_v5_concurrent_scans_complete_via_shared_pool(
    client: Any, monkeypatch: Any
) -> None:
    # 3 个并发扫描 > 池大小 2:超出部分在池内排队,jobs 完成语义不变
    fake = FakeRunScan(needs_review=False)  # 不写队列,避免 sqlite 写写竞争
    monkeypatch.setattr(service_app, "_get_run_scan", lambda: fake)

    job_ids = [client.post("/scan", json={"url": SITE}).json()["job_id"] for _ in range(3)]
    assert len(set(job_ids)) == 3
    for job_id in job_ids:
        job = wait_job(client, job_id)
        assert job["status"] == "done"
        assert job["result"]["needs_review"] is False
        assert job["result"]["entry_id"] is None
    assert fake.calls == [SITE] * 3


def test_v5_scan_telemetry_requested_duration_and_errors(
    client: Any, fake_scan: FakeRunScan, monkeypatch: Any
) -> None:
    telemetry.reset()
    job_id = client.post("/scan", json={"url": SITE}).json()["job_id"]
    wait_job(client, job_id)
    counters = telemetry.snapshot()["counters"]
    assert counters.get("service.scan_requested") == 1.0
    assert counters.get("service.scan_errors", 0.0) == 0.0
    assert telemetry.snapshot()["timers"]["service.scan.duration"]["count"] == 1

    # 失败任务:计入 scan_errors;成功计数与计时不再增加
    failing = FakeRunScan(error="没有分类器成员成功")
    monkeypatch.setattr(service_app, "_get_run_scan", lambda: failing)
    fail_id = client.post("/scan", json={"url": SITE}).json()["job_id"]
    wait_job(client, fail_id)
    counters = telemetry.snapshot()["counters"]
    assert counters.get("service.scan_requested") == 2.0
    assert counters.get("service.scan_errors") == 1.0


def test_v5_scan_requested_not_counted_for_invalid_url(client: Any) -> None:
    telemetry.reset()
    assert client.post("/scan", json={"url": "not-a-url"}).status_code == 422
    assert telemetry.snapshot()["counters"].get("service.scan_requested", 0.0) == 0.0


def test_v5_plan_generated_telemetry_only_on_success(client: Any, cfg: Config) -> None:
    telemetry.reset()
    # 404(条目不存在)/ 409(非 approved)路径不计数
    assert client.post("/plan/424242", json={"portal": "12377"}).status_code == 404
    pending_id = make_entry(cfg)
    assert client.post(f"/plan/{pending_id}", json={"portal": "12377"}).status_code == 409
    assert telemetry.snapshot()["counters"].get("service.plan_generated", 0.0) == 0.0

    entry_id = make_entry(cfg)
    assert client.post(f"/queue/{entry_id}/approve").status_code == 200
    assert client.post(f"/plan/{entry_id}", json={"portal": "12377"}).status_code == 200
    assert telemetry.snapshot()["counters"].get("service.plan_generated") == 1.0
