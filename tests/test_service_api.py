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

import dataclasses
import datetime
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
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
                     "/plan/{entry_id}", "/health",
                     # V10.26 净网助手统一工作台五端点
                     "/discover", "/leads", "/scan-lead", "/captcha/session",
                     "/assistant/status"):
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
        """记录 submit 调用后转交真实线程池执行的替身。

        V10.35:调用点经 _get_scan_pool(scan_workers) 传档位化大小,
        替身构造随之接受(并忽略)该参数。
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

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


# ---------------------------------------------------------------------------
# A233 门户动态化:动态门户进入 REST /plan;清单失败回退内置
# ---------------------------------------------------------------------------
def _write_dynamic_portals(tmp_path: Any) -> None:
    """在 tmp_path 下写两份动态门户定义:一份 enabled:true,一份 false。"""
    (tmp_path / "dyn.yaml").write_text(
        "name: 动态测试门户\n"
        "entry_url_key: portal_shdf_base\n"
        "category: 动态类目\n"
        "enabled: true\n",
        encoding="utf-8",
    )
    (tmp_path / "off.yaml").write_text(
        "name: 未启用门户\n"
        "entry_url_key: portal_shdf_base\n"
        "category: 动态类目\n"
        "enabled: false\n",
        encoding="utf-8",
    )


def test_a233_list_valid_portals_dynamic_and_fallback(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """/plan 门户清单:动态 enabled:true 进入;portal_defs 崩溃回退内置。"""
    from netsentinel.submit import portal_defs

    _write_dynamic_portals(tmp_path)
    real_list = portal_defs.list_portals
    monkeypatch.setattr(
        portal_defs, "list_portals",
        lambda include_disabled=False, directory=None: real_list(
            include_disabled, str(tmp_path)
        ),
    )
    names = service_app._list_valid_portals()
    assert {"12377", "shdf", "dyn"} <= set(names)
    assert "off" not in names  # 红线:未显式 enabled:true 不进清单

    # 清单异常 → 回退内置两门户,不抛错
    def _boom(*args, **kwargs):
        raise RuntimeError("门户模块损坏(测试注入)")

    monkeypatch.setattr(portal_defs, "list_portals", _boom)
    assert service_app._list_valid_portals() == ["12377", "shdf"]


def test_a233_plan_endpoint_accepts_dynamic_portal(
    client: Any, cfg: Config, tmp_path: Any, monkeypatch: Any
) -> None:
    """动态门户(dyn)可经 POST /plan 生成计划:类目/入口/人工门全链生效。"""
    from netsentinel.submit import portal_defs

    _write_dynamic_portals(tmp_path)
    real_list = portal_defs.list_portals
    real_get = portal_defs.get_portal
    monkeypatch.setattr(
        portal_defs, "list_portals",
        lambda include_disabled=False, directory=None: real_list(
            include_disabled, str(tmp_path)
        ),
    )
    monkeypatch.setattr(
        portal_defs, "get_portal",
        lambda name, directory=None: real_get(name, str(tmp_path)),
    )

    entry_id = make_entry(cfg)
    assert client.post(f"/queue/{entry_id}/approve").status_code == 200

    resp = client.post(f"/plan/{entry_id}", json={"portal": "dyn"})
    assert resp.status_code == 200
    body = resp.json()
    plan = body["plan"]
    assert plan["portal"] == "dyn"
    assert plan["entry_url"] == Config().portal_shdf_base  # 入口取 cfg 字段
    assert plan["payload"]["category"] == "动态类目"
    assert plan["steps"][0]["meta"]["portal"] == "动态测试门户"
    assert "human_gate" in {s["action"] for s in plan["steps"]}  # 红线:人工门
    assert "举报 Playbook" in body["playbook_md"]

    # 未启用的动态门户:清单不含 → 422
    resp = client.post(f"/plan/{entry_id}", json={"portal": "off"})
    assert resp.status_code == 422
    assert "portal" in resp.json()["detail"]


def test_a233_plan_invalid_portal_message_lists_available(client: Any, cfg: Config) -> None:
    """422 文案随动态清单变化(可用门户如实列出,不再硬编码 12377/shdf)。"""
    entry_id = make_entry(cfg)
    assert client.post(f"/queue/{entry_id}/approve").status_code == 200
    resp = client.post(f"/plan/{entry_id}", json={"portal": "999"})
    assert resp.status_code == 422
    assert "12377" in resp.json()["detail"] and "shdf" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# V10.18 性能:_find_entry_id 只读 SQL MAX 与旧全量回查的三态等价
# ---------------------------------------------------------------------------
def _legacy_find_entry_id(cfg: Config, site: str) -> int | None:
    """改造前口径的参照实现:全量 queue.list() 后 Python 侧取同站点 max(id)。"""
    queue = ReviewQueue(cfg.db_path)
    try:
        matches = [e for e in queue.list() if e.site_url == site]
    finally:
        queue.close()
    return max(int(e.id) for e in matches) if matches else None


def test_v10_find_entry_id_empty_db_returns_none(cfg: Config) -> None:
    """三态之一:空库(表已建、零条目)→ None,与旧口径一致。"""
    ReviewQueue(cfg.db_path).close()  # 建库建表(零条目)后即关
    assert service_app._find_entry_id(SITE, cfg) is None
    assert _legacy_find_entry_id(cfg, SITE) is None


def test_v10_find_entry_id_normal_and_out_of_order(cfg: Config) -> None:
    """三态之二/之三:正常回查取同站点 id;乱序插入(同站点夹插他站)取最大。"""
    a1 = make_entry(cfg, "http://localhost/aa")
    make_entry(cfg, "http://localhost/bb")  # 乱序:夹插他站点条目
    a2 = make_entry(cfg, "http://localhost/aa")

    found = service_app._find_entry_id("http://localhost/aa", cfg)
    assert found == a2  # 同站点取最大 id,不是首条 a1
    assert found == max(a1, a2)
    # 等价性证明:与改造前全量回查口径在相同库状态下恒等
    assert found == _legacy_find_entry_id(cfg, "http://localhost/aa")
    assert service_app._find_entry_id("http://localhost/bb", cfg) \
        == _legacy_find_entry_id(cfg, "http://localhost/bb")
    assert service_app._find_entry_id("http://localhost/none", cfg) is None


def test_v10_find_entry_id_missing_db_falls_back_to_legacy(tmp_path: Any) -> None:
    """兜底等价:库文件缺席时只读口打不开 → 回落旧路径(建空库)→ None。"""
    c = Config()
    c.db_path = str(tmp_path / "sub" / "none.db")
    assert service_app._find_entry_id(SITE, c) is None
    assert (tmp_path / "sub" / "none.db").is_file()  # 旧路径的建库副作用保持不变


def test_v10_max_entry_id_sqlite_matches_legacy_for_every_site(cfg: Config) -> None:
    """逐站点全量等价:多站点混插后,SQL 口径与旧口径对每个站点都一致。"""
    sites = [f"http://localhost/s{i}" for i in range(5)]
    for i, site in enumerate(sites):
        for _ in range((i % 3) + 1):  # 各站点 1~3 条,数量不均
            make_entry(cfg, site)
    for site in sites:
        assert service_app._max_entry_id_sqlite(cfg.db_path, site) \
            == (_legacy_find_entry_id(cfg, site) or 0)


# ---------------------------------------------------------------------------
# V10.20 入口层修复:main(argv) 参数解析(Seat D 移交)
# ---------------------------------------------------------------------------
def _fake_uvicorn(monkeypatch: Any) -> list[tuple[Any, ...]]:
    """注入假 uvicorn 模块(记录 run 调用;绝不起真实服务/占端口)。"""
    import sys
    import types

    calls: list[tuple[Any, ...]] = []
    mod = types.ModuleType("uvicorn")

    def _run(app: Any, host: Any = None, port: Any = None) -> None:
        calls.append((host, port))

    mod.run = _run
    monkeypatch.setitem(sys.modules, "uvicorn", mod)
    return calls


def _fake_cfg_loader(monkeypatch: Any, tmp_path: Any) -> Config:
    """把 load_config 替换为指向 tmp 的配置(不读 ./config.yaml,零落盘真实区)。"""
    from netsentinel import config as config_mod

    c = Config()
    c.data_dir = str(tmp_path / "data")
    c.evidence_dir = str(tmp_path / "data" / "evidence")
    c.db_path = str(tmp_path / "data" / "review_queue.db")
    c.audit_path = str(tmp_path / "data" / "audit.jsonl")
    c.log_path = str(tmp_path / "data" / "logs" / "netsentinel.log")
    monkeypatch.setattr(config_mod, "load_config", lambda: c)
    return c


def test_v1020_main_help_exits_zero_without_server(
    monkeypatch: Any, capsys: Any
) -> None:
    """--help:打印用法退出 0,绝不起 uvicorn(V10.20 前会直接绑 8765)。"""
    calls = _fake_uvicorn(monkeypatch)
    with pytest.raises(SystemExit) as ei:
        service_app.main(["--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "--host" in out and "--port" in out
    assert "service.app" in out
    assert calls == []  # 未起服务、未占端口


def test_v1020_main_invalid_args_exit_two(monkeypatch: Any) -> None:
    """非法参数(未知选项 / --port 非整数)退出码 2,不起服务。"""
    calls = _fake_uvicorn(monkeypatch)
    for bad in (["--bogus"], ["--port", "abc"], ["--host"]):
        with pytest.raises(SystemExit) as ei:
            service_app.main(bad)
        assert ei.value.code == 2
    assert calls == []


def test_v1020_main_defaults_to_config_host_port(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """缺省(无参)监听地址取 cfg.service_host/service_port(旧语义不变)。"""
    calls = _fake_uvicorn(monkeypatch)
    cfg = _fake_cfg_loader(monkeypatch, tmp_path)
    assert cfg.service_host == "127.0.0.1" and cfg.service_port == 8765
    assert service_app.main([]) == 0
    assert calls == [("127.0.0.1", 8765)]


def test_v1020_main_explicit_host_port_override_config(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """--host/--port 显式覆盖配置(缺省取配置,覆盖优先)。"""
    calls = _fake_uvicorn(monkeypatch)
    _fake_cfg_loader(monkeypatch, tmp_path)
    assert service_app.main(["--host", "localhost", "--port", "9911"]) == 0
    assert calls == [("localhost", 9911)]


# ---------------------------------------------------------------------------
# V10.20:GET /queue 可选分页(缺省全量完全向后兼容)
# ---------------------------------------------------------------------------
def test_v1020_queue_default_returns_all_backward_compatible(
    client: Any, cfg: Config
) -> None:
    """缺省全量:响应体仍是旧口径两键(逐字节兼容)。"""
    for i in range(3):
        make_entry(cfg, f"http://localhost/p{i}")
    resp = client.get("/queue")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"entries", "summary"}
    assert [e["id"] for e in body["entries"]] == [1, 2, 3]


def test_v1020_queue_pagination_slice_with_total(client: Any, cfg: Config) -> None:
    """limit/offset 切片:id 升序全量的 [offset:offset+limit],响应附 total。"""
    for i in range(5):
        make_entry(cfg, f"http://localhost/p{i}")
    resp = client.get("/queue?limit=2&offset=1")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"entries", "summary", "total"}
    assert [e["id"] for e in body["entries"]] == [2, 3]
    assert body["total"] == 5
    assert body["summary"]["pending"] == 5  # summary 恒为全队列口径

    # offset 单独在场同样进入分页语义(到尾段)
    body2 = client.get("/queue?offset=4").json()
    assert [e["id"] for e in body2["entries"]] == [5]
    assert body2["total"] == 5


def test_v1020_queue_pagination_out_of_range_returns_empty_200(
    client: Any, cfg: Config
) -> None:
    """offset 越界:空 entries + 200(不报错),total 如实。"""
    for i in range(2):
        make_entry(cfg, f"http://localhost/q{i}")
    resp = client.get("/queue?limit=10&offset=50")
    assert resp.status_code == 200
    body = resp.json()
    assert body["entries"] == []
    assert body["total"] == 2


def test_v1020_queue_pagination_invalid_values_422_chinese(
    client: Any, cfg: Config
) -> None:
    """非法值(非整数 / limit<1 / offset<0 / 空串)统一 422 中文。"""
    make_entry(cfg, "http://localhost/one")
    for bad_limit in ("abc", "0", "-3", ""):
        resp = client.get("/queue", params={"limit": bad_limit})
        assert resp.status_code == 422, bad_limit
        assert "limit" in resp.json()["detail"]
        assert "不合法" in resp.json()["detail"]
    for bad_offset in ("xyz", "-1", ""):
        resp = client.get("/queue", params={"offset": bad_offset})
        assert resp.status_code == 422, bad_offset
        assert "offset" in resp.json()["detail"]
    # 合法极值不受扰:limit=1 / offset=0
    ok = client.get("/queue?limit=1&offset=0").json()
    assert len(ok["entries"]) == 1 and ok["total"] == 1


# ---------------------------------------------------------------------------
# V10.26 净网助手统一工作台(席位A):五个新端点(全离线,零外呼/零弹窗)
# ---------------------------------------------------------------------------
def wait_terminal(client: Any, job_id: str, timeout: float = 10.0) -> dict[str, Any]:
    """轮询任务直到终态 done/failed(queued/running 继续等;带超时)。"""
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if last.get("status") in ("done", "failed"):
            return last
        time.sleep(0.02)
    pytest.fail(f"任务 {job_id} 在 {timeout}s 内未到终态,最后状态:{last}")


class FakeDiscover:
    """同步 fake 发现函数(经 _get_discover 缝注入,零外呼)。

    记录调用参数与 worker 内的进程环境(无头断言用);``error`` 非空时抛
    中文 RuntimeError(验证失败传播路径)。
    """

    def __init__(
        self,
        *,
        urls: list[str] | None = None,
        queries: int = 2,
        errors: list[tuple[str, str]] | None = None,
        error: str | None = None,
    ) -> None:
        self.urls = list(urls or [])
        self.queries = queries
        self.errors = list(errors or [])
        self.error = error
        self.calls: list[tuple[list[str], str]] = []
        self.seen_env: dict[str, str | None] = {}

    def __call__(self, keywords: list[str], cfg: Config, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((list(keywords), str(cfg.discovery_engine)))
        self.seen_env = {
            "headed": os.environ.get("NETSENTINEL_YANDEX_HTML_HEADED"),
            "data_dir": os.environ.get("NETSENTINEL_DATA_DIR"),
        }
        if self.error is not None:
            raise RuntimeError(self.error)
        return {
            "urls": list(self.urls),
            "queries": self.queries,
            "cache_hits": 0,
            "errors": list(self.errors),
        }


class FakeSubprocessModule:
    """subprocess 模块替身(经 _get_subprocess 缝注入,零弹窗/零真进程)。

    记录 run 调用(cmd/env/timeout 全量),返回预置的 CompletedProcess 形态;
    ``error`` 非空时抛出(启动失败/超时传播路径)。
    """

    def __init__(
        self, *, returncode: int = 0, stdout: str = "", error: Exception | None = None
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def run(self, cmd: list[str], **kwargs: Any) -> Any:
        self.calls.append({"cmd": list(cmd), **kwargs})
        if self.error is not None:
            raise self.error
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr="")


def _inject_discover(monkeypatch: Any, fake: FakeDiscover) -> FakeDiscover:
    monkeypatch.setattr(service_app, "_get_discover", lambda: fake)
    return fake


def _inject_subprocess(monkeypatch: Any, fake: FakeSubprocessModule) -> FakeSubprocessModule:
    monkeypatch.setattr(service_app, "_get_subprocess", lambda: fake)
    return fake


# ---- POST /discover -------------------------------------------------------
def test_v1026_discover_flow_writes_leads_and_done(
    client: Any, cfg: Config, monkeypatch: Any
) -> None:
    """正常流:202 queued → done;result 四键齐全,线索真实落 <data>/leads.txt。"""
    fake = _inject_discover(
        monkeypatch,
        FakeDiscover(
            urls=["http://a.example/1", "http://b.example/2"],
            queries=3,
            errors=[("词A", "验证码拦截")],
        ),
    )
    resp = client.post("/discover", json={"keywords": ["词A", "词B"]})
    assert resp.status_code == 202
    body = resp.json()
    assert set(body) == {"job_id", "status"}
    assert body["status"] == "queued"

    job = wait_terminal(client, body["job_id"])
    assert job["status"] == "done"
    result = job["result"]
    assert set(result) == {"urls", "queries", "errors", "out"}
    assert result["urls"] == 2
    assert result["queries"] == 3
    assert result["errors"] == ["词A —— 验证码拦截"]
    assert result["out"] == str(Path(cfg.data_dir) / "leads.txt")
    text = Path(result["out"]).read_text(encoding="utf-8")  # 真实 write_leads 落盘
    assert "http://a.example/1" in text and "http://b.example/2" in text
    assert fake.calls[0][0] == ["词A", "词B"]


def test_v1026_discover_default_keywords_from_active_file(
    client: Any, cfg: Config, monkeypatch: Any
) -> None:
    """keywords 缺省:读 <data>/keywords/active.txt,同关键词页口径解析。"""
    fake = _inject_discover(monkeypatch, FakeDiscover(urls=[]))
    kw_dir = Path(cfg.data_dir) / "keywords"
    kw_dir.mkdir(parents=True)
    (kw_dir / "active.txt").write_text(
        "# 注释行\n词A\n词B\n词A\n\n  词C  \n", encoding="utf-8"
    )
    resp = client.post("/discover", json={})
    assert resp.status_code == 202
    job = wait_terminal(client, resp.json()["job_id"])
    assert job["status"] == "done"
    assert fake.calls[0][0] == ["词A", "词B", "词C"]  # strip/跳注释/去重保序
    assert fake.calls[0][1] == cfg.discovery_engine  # engine 缺省取配置


def test_v1026_discover_engine_override(
    client: Any, monkeypatch: Any
) -> None:
    """engine 显式覆盖:scoped 配置生效(fake 收到的 cfg 已改 discovery_engine)。"""
    fake = _inject_discover(monkeypatch, FakeDiscover(urls=[]))
    resp = client.post("/discover", json={"keywords": ["词A"], "engine": "yandex-html"})
    assert resp.status_code == 202
    wait_terminal(client, resp.json()["job_id"])
    assert fake.calls[0][1] == "yandex-html"


def test_v1026_discover_missing_keywords_422_chinese(client: Any, cfg: Config) -> None:
    """缺参:body 无 keywords 且活文件缺失 → 422 中文(不建 job)。"""
    resp = client.post("/discover", json={})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "keywords" in detail and "不合法" in detail


def test_v1026_discover_invalid_keywords_and_engine_422(client: Any) -> None:
    """非法参数统一 422 中文:keywords 非列表/元素非串/全注释;engine 非法。"""
    for bad in ({"keywords": "词A"}, {"keywords": [1, 2]}, {"keywords": ["#注释", ""]}):
        resp = client.post("/discover", json=bad)
        assert resp.status_code == 422, bad
        assert "keywords" in resp.json()["detail"]
    for bad_engine in ("", 7, []):
        resp = client.post("/discover", json={"keywords": ["词A"], "engine": bad_engine})
        assert resp.status_code == 422, bad_engine
        assert "engine" in resp.json()["detail"]


def test_v1026_discover_failure_marks_job_failed(client: Any, monkeypatch: Any) -> None:
    """后台失败传播:discover 抛中文 RuntimeError(如验证码)→ failed.error。"""
    _inject_discover(
        monkeypatch,
        FakeDiscover(error="yandex-html 检测到验证码,无头模式下已停止(请先完成验证会话)"),
    )
    job_id = client.post("/discover", json={"keywords": ["词A"]}).json()["job_id"]
    job = wait_terminal(client, job_id)
    assert job["status"] == "failed"
    assert "发现失败" in job["error"] and "验证码" in job["error"]
    assert "result" not in job


def test_v1026_discover_worker_runs_headless_even_if_env_set(
    client: Any, monkeypatch: Any
) -> None:
    """无头保证:服务进程环境带有头变量,发现 worker 体内也必须移除,且事后恢复。"""
    monkeypatch.setenv("NETSENTINEL_YANDEX_HTML_HEADED", "1")
    fake = _inject_discover(monkeypatch, FakeDiscover(urls=["http://x.example/"]))
    job_id = client.post("/discover", json={"keywords": ["词A"]}).json()["job_id"]
    wait_terminal(client, job_id)
    assert fake.seen_env["headed"] is None  # 子线程内不得设有头变量
    assert os.environ.get("NETSENTINEL_YANDEX_HTML_HEADED") == "1"  # 退出恢复


def test_v1026_discover_worker_aligns_data_dir_env(
    client: Any, cfg: Config, monkeypatch: Any
) -> None:
    """数据目录对齐:worker 体内 NETSENTINEL_DATA_DIR = cfg.data_dir 绝对路径。"""
    fake = _inject_discover(monkeypatch, FakeDiscover(urls=[]))
    job_id = client.post("/discover", json={"keywords": ["词A"]}).json()["job_id"]
    wait_terminal(client, job_id)
    assert fake.seen_env["data_dir"] == str(Path(cfg.data_dir).resolve())


# ---- GET /leads -----------------------------------------------------------
def test_v1026_leads_missing_file_returns_empty(client: Any, cfg: Config) -> None:
    """文件缺失 → 空表 + mtime null;只读端点绝不创建文件。"""
    resp = client.get("/leads")
    assert resp.status_code == 200
    assert resp.json() == {"leads": [], "count": 0, "mtime": None}
    assert not (Path(cfg.data_dir) / "leads.txt").exists()


def test_v1026_leads_parses_urls_with_line_numbers(client: Any, cfg: Config) -> None:
    """正常流:跳 # 注释行,行号为文件内 1 起始真实行号,mtime 可解析。"""
    leads_path = Path(cfg.data_dir) / "leads.txt"
    leads_path.parent.mkdir(parents=True, exist_ok=True)
    leads_path.write_text(
        "# NetSentinel 线索清单(搜索引擎发现)\n"
        "# 引擎:mock | 生成:2026-10-13T00:00:00\n"
        "http://a.example/1\n"
        "\n"
        "http://b.example/2\n",
        encoding="utf-8",
    )
    resp = client.get("/leads")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"leads", "count", "mtime"}
    assert body["leads"] == [
        {"url": "http://a.example/1", "line": 3},
        {"url": "http://b.example/2", "line": 5},
    ]
    assert body["count"] == 2
    assert body["mtime"] is not None
    datetime.datetime.fromisoformat(body["mtime"])  # iso 可解析


def test_v1026_leads_after_discover_roundtrip(
    client: Any, cfg: Config, monkeypatch: Any
) -> None:
    """与 /discover 串联:后台发现落盘后,/leads 如实可见(后台化闭环)。"""
    _inject_discover(monkeypatch, FakeDiscover(urls=["http://c.example/9"]))
    resp = client.post("/discover", json={"keywords": ["词A"]})
    wait_terminal(client, resp.json()["job_id"])
    body = client.get("/leads").json()
    assert body["count"] == 1
    assert body["leads"][0]["url"] == "http://c.example/9"
    assert body["leads"][0]["line"] >= 1


# ---- POST /scan-lead ------------------------------------------------------
def test_v1026_scan_lead_flow_same_mechanism_as_scan(
    client: Any, fake_scan: FakeRunScan
) -> None:
    """正常流:与 /scan 同机制(202 running → done,入复核队列可查)。"""
    resp = client.post("/scan-lead", json={"url": SITE, "line": 7})
    assert resp.status_code == 202
    body = resp.json()
    assert set(body) == {"job_id", "status"}
    assert body["status"] == "running"
    job = wait_job(client, body["job_id"])
    assert job["status"] == "done"
    assert job["result"]["verdict"] == "nsfw"
    assert job["result"]["entry_id"] == fake_scan.entry_ids[SITE]
    assert fake_scan.calls == [SITE]
    entries = client.get("/queue").json()["entries"]  # 同 /scan:入列待复核
    assert [e["id"] for e in entries] == [fake_scan.entry_ids[SITE]]


def test_v1026_scan_lead_without_line_ok(client: Any, fake_scan: FakeRunScan) -> None:
    """line 可选:只给 url 也能走通(与 /scan 完全等价)。"""
    resp = client.post("/scan-lead", json={"url": "http://localhost/lead-only"})
    assert resp.status_code == 202
    job = wait_job(client, resp.json()["job_id"])
    assert job["status"] == "done"


def test_v1026_scan_lead_missing_url_422_chinese(client: Any) -> None:
    """缺参:无 body / 缺 url / 非法 url → 422 中文。"""
    for bad_body in (None, {}, {"line": 3}):
        resp = client.post("/scan-lead", json=bad_body)
        assert resp.status_code == 422, bad_body
        assert "url" in resp.json()["detail"]
    for bad_url in ("not-a-url", "ftp://localhost/x", ""):
        resp = client.post("/scan-lead", json={"url": bad_url, "line": 3})
        assert resp.status_code == 422, bad_url
        assert "url" in resp.json()["detail"]


def test_v1026_scan_lead_invalid_line_422_chinese(client: Any) -> None:
    """line 非法(字符串/0/负数/浮点/布尔)→ 422 中文。"""
    for bad_line in ("3", 0, -1, 1.5, True):
        resp = client.post("/scan-lead", json={"url": SITE, "line": bad_line})
        assert resp.status_code == 422, bad_line
        assert "line" in resp.json()["detail"]
        assert "不合法" in resp.json()["detail"]


def test_v1026_scan_lead_failure_marks_job_failed(client: Any, monkeypatch: Any) -> None:
    """后台失败传播:扫描函数抛错 → failed(中文 error,与 /scan 同口径)。"""
    fake = FakeRunScan(error="没有任何分类器成员成功完成评分")
    monkeypatch.setattr(service_app, "_get_run_scan", lambda: fake)
    job_id = client.post("/scan-lead", json={"url": SITE, "line": 2}).json()["job_id"]
    job = wait_job(client, job_id)
    assert job["status"] == "failed"
    assert "扫描失败" in job["error"] and "分类器" in job["error"]
    assert "result" not in job


# ---- POST /captcha/session ------------------------------------------------
def test_v1026_captcha_session_command_env_and_done(
    client: Any, cfg: Config, monkeypatch: Any
) -> None:
    """正常流:命令行/env 契约精确匹配;returncode 0 → done(result 两键)。"""
    fake = _inject_subprocess(
        monkeypatch,
        FakeSubprocessModule(
            returncode=0,
            stdout="关键词 2 个;引擎 yandex-html\n发现完成:查询 2 次,线索 5 条\n",
        ),
    )
    monkeypatch.setenv("NETSENTINEL_YANDEX_HTML_HEADED", "0")  # 父进程环境哨兵
    resp = client.post("/captcha/session")
    assert resp.status_code == 202
    assert set(resp.json()) == {"job_id", "status"}
    assert resp.json()["status"] == "queued"
    job = wait_terminal(client, resp.json()["job_id"])
    assert job["status"] == "done"
    assert job["result"]["returncode"] == 0
    assert "发现完成" in job["result"]["stdout_tail"]

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["cmd"] == [
        sys.executable,
        "-m",
        "netsentinel.discovery",
        "--keywords-file",
        str(Path(cfg.data_dir) / "keywords" / "active.txt"),
        "--engine",
        "yandex-html",
        "--out",
        str(Path(cfg.data_dir) / "leads.txt"),
    ]
    assert call["env"]["NETSENTINEL_YANDEX_HTML_HEADED"] == "1"
    assert call["env"]["NETSENTINEL_DATA_DIR"] == str(Path(cfg.data_dir).resolve())
    assert call["timeout"] == service_app.CAPTCHA_SESSION_TIMEOUT_S
    assert call["capture_output"] is True and call["text"] is True
    # env 显式传入,父进程环境不被污染(后台无头承诺不受影响)
    assert os.environ.get("NETSENTINEL_YANDEX_HTML_HEADED") == "0"


def test_v1026_captcha_session_nonzero_returncode_failed(
    client: Any, monkeypatch: Any
) -> None:
    """非 0 退出码 → failed(中文 error 附退出码);result 仍随附 stdout_tail。"""
    _inject_subprocess(
        monkeypatch,
        FakeSubprocessModule(returncode=2, stdout="错误:关键词文件不存在\n"),
    )
    job_id = client.post("/captcha/session").json()["job_id"]
    job = wait_terminal(client, job_id)
    assert job["status"] == "failed"
    assert "退出码 2" in job["error"] and "验证会话" in job["error"]
    assert job["result"]["returncode"] == 2
    assert "关键词文件不存在" in job["result"]["stdout_tail"]


def test_v1026_captcha_session_spawn_failure_failed_chinese(
    client: Any, monkeypatch: Any
) -> None:
    """后台失败传播:子进程启动失败(异常)→ failed(中文 error,无 result)。"""
    _inject_subprocess(
        monkeypatch, FakeSubprocessModule(error=RuntimeError("找不到 Python 解释器"))
    )
    job_id = client.post("/captcha/session").json()["job_id"]
    job = wait_terminal(client, job_id)
    assert job["status"] == "failed"
    assert "验证会话执行失败" in job["error"] and "Python" in job["error"]
    assert "result" not in job


def test_v1026_captcha_session_timeout_failed_chinese(
    client: Any, monkeypatch: Any
) -> None:
    """超时路径:Timeout 形异常 → failed(中文,含超时分钟数)。"""
    _inject_subprocess(
        monkeypatch, FakeSubprocessModule(error=TimeoutError("subprocess timed out"))
    )
    job_id = client.post("/captcha/session").json()["job_id"]
    job = wait_terminal(client, job_id)
    assert job["status"] == "failed"
    assert "超时" in job["error"]
    assert str(service_app.CAPTCHA_SESSION_TIMEOUT_S // 60) in job["error"]


# ---- GET /assistant/status ------------------------------------------------
def test_v1026_assistant_status_full_picture(
    client: Any, cfg: Config, monkeypatch: Any, tmp_path: Any
) -> None:
    """正常流:五键齐全,各路本地探测如实(密钥文件/会话目录/词数/队列/配置)。"""
    key_file = tmp_path / "keys" / "glm"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text("sk-test\n", encoding="utf-8")
    monkeypatch.setattr(service_app, "_glm_key_path", lambda: key_file)

    (Path(cfg.data_dir) / "browser_profiles" / "yandex_html").mkdir(parents=True)
    kw_dir = Path(cfg.data_dir) / "keywords"
    kw_dir.mkdir(parents=True)
    (kw_dir / "active.txt").write_text("# 注释\n词A\n词B\n词A\n\n", encoding="utf-8")

    make_entry(cfg, "http://localhost/s1")
    make_entry(cfg, "http://localhost/s2")
    entry_id = make_entry(cfg)
    assert client.post(f"/queue/{entry_id}/approve").status_code == 200

    cfg.vlm_online = True
    cfg.discovery_online = True
    cfg.classifier = "glm"

    resp = client.get("/assistant/status")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"glm_key", "yandex_profile", "keywords_count", "queue", "config"}
    assert body["glm_key"] is True
    assert body["yandex_profile"] is True
    assert body["keywords_count"] == 2  # 注释/重复/空行不计
    assert body["queue"] == {"pending": 2, "approved": 1, "rejected": 0, "submitted": 0}
    assert body["config"] == {
        "vlm_online": True,
        "discovery_online": True,
        "classifier": "glm",
    }


def test_v1026_assistant_status_missing_everything_zeros_no_side_effects(
    client: Any, cfg: Config, monkeypatch: Any, tmp_path: Any
) -> None:
    """坏文件/缺文件:全 0 / False;只读红线:绝不建库、不建关键词/会话文件。"""
    monkeypatch.setattr(service_app, "_glm_key_path", lambda: tmp_path / "nope" / "glm")
    resp = client.get("/assistant/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["glm_key"] is False
    assert body["yandex_profile"] is False
    assert body["keywords_count"] == 0
    assert body["queue"] == {"pending": 0, "approved": 0, "rejected": 0, "submitted": 0}
    assert body["config"]["classifier"] == cfg.classifier
    # 零副作用:库/关键词活文件/会话目录都未被创建
    assert not Path(cfg.db_path).exists()
    assert not (Path(cfg.data_dir) / "keywords" / "active.txt").exists()


def test_v1026_assistant_status_queue_uses_readonly_connection(
    client: Any, cfg: Config, monkeypatch: Any
) -> None:
    """零外呼/零副作用证明:队列统计必须走 readonly 连接(与 /queue 口径同款)。"""
    calls: list[bool] = []
    real_connect = service_app.kernel_connect

    def recording_connect(path: Any, *, readonly: bool = False) -> Any:
        calls.append(readonly)
        return real_connect(path, readonly=readonly)

    monkeypatch.setattr(service_app, "kernel_connect", recording_connect)
    make_entry(cfg, "http://localhost/z1")

    body = client.get("/assistant/status").json()
    assert calls and all(calls)  # 只有 readonly=True 的连接被打开
    assert body["queue"]["pending"] == 1


# ---------------------------------------------------------------------------
# V10.35(P2-9):扫描池档位化(scan_pool_size = min(io_workers, CAP))
# ---------------------------------------------------------------------------
def test_v10_35_scan_pool_size_default_fallback_without_cfg() -> None:
    # 无 cfg:回退固定缺省 2(与档位化前行为一致)
    assert service_app.scan_pool_size(None) == service_app.SCAN_MAX_WORKERS


def test_v10_35_scan_pool_size_mid_tier_capped(cfg: Config) -> None:
    size = service_app.scan_pool_size(cfg)
    assert 1 <= size <= service_app.SCAN_POOL_CAP
    # mid 档公式 cores/2;20 核机器 = 10,恰等于 CAP;少核机器按档位收窄
    from netsentinel.ops.concurrency import io_workers

    assert size == min(io_workers(cfg), service_app.SCAN_POOL_CAP)


def test_v10_35_scan_pool_size_high_tier_still_capped(cfg: Config) -> None:
    cfg_high = dataclasses.replace(cfg, concurrency_tier="high")
    size = service_app.scan_pool_size(cfg_high)
    assert 1 <= size <= service_app.SCAN_POOL_CAP  # 资源保护上界恒成立


def test_v10_35_scan_pool_get_uses_default_when_no_arg() -> None:
    # 无参调用(既有测试/直接调用方)= 旧行为,不触发档位化
    assert service_app._get_scan_pool() is service_app._get_scan_pool()  # 单例稳定


def test_v10_35_scan_pool_size_bad_tier_falls_back(cfg: Config) -> None:
    bad = dataclasses.replace(cfg, concurrency_tier="perfekt")  # 非法档位
    assert service_app.scan_pool_size(bad) == service_app.SCAN_MAX_WORKERS
