"""A18 编排器(orchestrator)与 CLI(__main__)单元测试。

全部离线、依赖注入 fake,不依赖兄弟模块是否就位:
- run_scan 的外部依赖走参数注入(capture / classifier / fetch_page)或
  模块级 ``_default_*`` 工厂(monkeypatch 替换);
- 集成(ensemble)与判定(verdict)使用真实实现(均为纯标准库离线模块),
  分类器优先用真实 stub,缺失时退化为同规则 fake;
- run_submit 的队列 / 频控 / 计划 / 执行器 / 审计全部注入 fake;
- 绝不访问真实举报门户,不发起任何网络请求。

V11 接线锁定(trace_enabled / abstain_enabled 两开关):默认关 = 行为与
现状逐字节一致(入队调用口径、intel 键集、遥测计数均无扰动);开 = trace
全链 span 树可导出 + 审计收到 span 事件;abstain 高分歧置 needs_review +
priority_weight 提权入队,而三档判定输出(verdict / agg / count)分毫不动。
"""
from __future__ import annotations

import json
import os
from typing import Any

import pytest

import netsentinel.__main__ as cli
import netsentinel.pipeline.orchestrator as orchestrator
from netsentinel.contracts import (
    Config,
    EvidenceBundle,
    ExecutionResult,
    ImageEvidence,
    ImageScore,
    PageSample,
    Portal,
    SiteReport,
    Verdict,
)

SITE = "http://localhost/a"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class FakeCapture:
    """页面采样 fake:返回含 N 张指定命名前缀图片的 PageSample。"""

    def __init__(self, count: int = 4, prefix: str = "nsfw_hi") -> None:
        self.count = count
        self.prefix = prefix
        self.calls: list[str] = []
        self.fetch_seen: Any = None

    def __call__(self, url: str, cfg: Config, *, fetch_page: Any = None) -> PageSample:
        self.calls.append(url)
        self.fetch_seen = fetch_page
        imgs = [
            ImageEvidence(
                path=os.path.join("fake", "imgs", f"{self.prefix}_{i}.jpg"),
                url=f"http://img/{self.prefix}/{i}",
                source_page=url,
                width=300,
                height=300,
            )
            for i in range(self.count)
        ]
        return PageSample(url=url, screenshot_path="", image_evidences=imgs)


class _FallbackStub:
    """与 vision.stub_classifier 同规则的兜底 fake(兄弟模块缺失时使用)。"""

    name = "stub"

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg

    def classify(self, img: ImageEvidence) -> ImageScore:
        filename = os.path.basename(img.path).lower()
        if "nsfw_hi" in filename:
            prob = 0.97
        elif "nsfw_mid" in filename:
            prob = 0.72
        else:
            prob = 0.02
        return ImageScore(image=img, model=self.name, nsfw_prob=prob, scores={})

    def classify_batch(self, imgs: list[ImageEvidence]) -> list[ImageScore]:
        return [self.classify(i) for i in imgs]


def make_stub_classifier(cfg: Config) -> Any:
    """优先取真实 stub 分类器(importorskip 容错:缺则用同规则 fake)。"""
    try:
        stub = pytest.importorskip("netsentinel.vision.stub_classifier")
    except pytest.skip.Exception:  # pragma: no cover - 兄弟模块未就位时走 fake
        return _FallbackStub(cfg)
    return stub.StubClassifier(cfg)


class FakeBundleFactory:
    """build_bundle fake:记录调用并返回固定证据包。"""

    def __init__(self) -> None:
        self.calls: list[SiteReport] = []

    def __call__(self, report: SiteReport, cfg: Config) -> EvidenceBundle:
        self.calls.append(report)
        return EvidenceBundle(
            site_url=report.site_url,
            dir_path="fake/evidence/dir",
            manifest_path="fake/evidence/dir/manifest.json",
            zip_path="fake/evidence/bundle.zip",
        )


class FakeEntry:
    """复核条目 fake(鸭子类型:status / site_url / evidence_zip)。"""

    def __init__(
        self,
        id: int,
        site_url: str = "http://localhost/x",
        status: str = "approved",
        evidence_zip: str = "fake/evidence/bundle.zip",
    ) -> None:
        self.id = id
        self.site_url = site_url
        self.status = status
        self.evidence_zip = evidence_zip


class FakeQueue:
    """ReviewQueue fake:记录 add / mark_submitted 调用。"""

    def __init__(self, entries: dict[int, FakeEntry] | None = None) -> None:
        self.entries: dict[int, FakeEntry] = dict(entries or {})
        self.added: list[tuple[SiteReport, str]] = []
        self.marked: list[int] = []

    def add(self, report: SiteReport, evidence_zip: str = "") -> int:
        self.added.append((report, evidence_zip))
        new_id = max(self.entries, default=0) + 1
        self.entries[new_id] = FakeEntry(new_id, report.site_url, "pending", evidence_zip)
        return 77

    def get(self, entry_id: int) -> FakeEntry | None:
        return self.entries.get(entry_id)

    def mark_submitted(self, entry_id: int) -> FakeEntry:
        self.marked.append(entry_id)
        return self.entries[entry_id]

    def list(self, status: str | None = None) -> list[FakeEntry]:
        return [
            e for e in self.entries.values() if status is None or e.status == status
        ]


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log_event(self, event: str, **fields: Any) -> None:
        self.events.append({"event": event, **fields})


class FakeAuditFactory:
    """JsonlAuditLogger fake 工厂。"""

    def __init__(self) -> None:
        self.audits: list[FakeAudit] = []

    def __call__(self, path: str) -> FakeAudit:
        audit = FakeAudit()
        self.audits.append(audit)
        return audit


class FakeRateLimiter:
    def __init__(self, allowed: bool = True, reason: str = "") -> None:
        self.allowed = allowed
        self.reason = reason
        self.recorded = 0

    def can_submit(self, now: Any = None) -> tuple[bool, str]:
        return (self.allowed, self.reason)

    def record(self, now: Any = None) -> None:
        self.recorded += 1


class FakeRateLimiterFactory:
    def __init__(self, limiter: FakeRateLimiter) -> None:
        self.limiter = limiter
        self.calls: list[tuple[str, int, int]] = []

    def __call__(self, state_path: str, min_interval_s: int, max_per_day: int) -> FakeRateLimiter:
        self.calls.append((state_path, min_interval_s, max_per_day))
        return self.limiter


class FakePlanFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, Portal]] = []

    def __call__(self, entry_like: Any, portal: Portal, cfg: Config) -> Any:
        self.calls.append((entry_like, portal))
        return "FAKE_PLAN"


class FakeExecutor:
    def __init__(self, result: ExecutionResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, plan: Any, cfg: Config, *, auto_confirm: bool = False, dry_run: bool | None = None
    ) -> ExecutionResult:
        self.calls.append({"plan": plan, "auto_confirm": auto_confirm, "dry_run": dry_run})
        return self.result


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def cfg(tmp_path) -> Config:
    return Config(
        data_dir=str(tmp_path / "data"),
        evidence_dir=str(tmp_path / "evidence"),
        db_path=str(tmp_path / "data" / "review_queue.db"),
        audit_path=str(tmp_path / "data" / "audit.jsonl"),
        log_path=str(tmp_path / "data" / "logs" / "netsentinel.log"),
    )


def _patch_scan_side_effects(
    monkeypatch: pytest.MonkeyPatch, queue: FakeQueue
) -> tuple[FakeBundleFactory, FakeAuditFactory]:
    """替换 run_scan 的 discover / build_bundle / queue / audit 默认工厂。"""
    monkeypatch.setattr(
        orchestrator, "_default_discover", lambda url, c, fetch_page=None: [url]
    )
    bundles = FakeBundleFactory()
    monkeypatch.setattr(orchestrator, "_default_build_bundle", bundles)
    monkeypatch.setattr(orchestrator, "_default_queue", lambda db_path: queue)
    audits = FakeAuditFactory()
    monkeypatch.setattr(orchestrator, "_default_audit_logger", audits)
    return bundles, audits


# ---------------------------------------------------------------------------
# run_scan
# ---------------------------------------------------------------------------
def test_run_scan_nsfw_needs_review_flow(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    """nsfw_hi 图片 → NSFW、needs_review;证据包/入列/审计均被调用。"""
    queue = FakeQueue()
    bundles, audits = _patch_scan_side_effects(monkeypatch, queue)
    fetch = lambda url, c: (200, "<html></html>", url)  # noqa: E731

    report = orchestrator.run_scan(
        SITE, cfg, fetch_page=fetch, capture=FakeCapture(), classifier=make_stub_classifier(cfg)
    )

    assert report.verdict is Verdict.NSFW
    assert report.needs_review is True
    assert report.agg_nsw_prob >= cfg.nsfw_threshold
    assert report.nsw_image_count >= cfg.min_nsw_images
    assert report.site_url == SITE
    # 证据包构建被调用,且 zip 路径透传给队列
    assert len(bundles.calls) == 1
    assert queue.added and queue.added[0][0] is report
    assert queue.added[0][1] == "fake/evidence/bundle.zip"
    # 审计事件:scan + site/verdict/agg
    assert audits.audits and audits.audits[0].events
    event = audits.audits[0].events[0]
    assert event["event"] == "scan"
    assert event["site"] == SITE
    assert event["verdict"] == "nsfw"
    assert event["agg"] == pytest.approx(0.97, abs=1e-6)


def test_run_scan_passes_fetch_page_through(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    """注入的 fetch_page 同时透传给链接发现与页面采样。"""
    queue = FakeQueue()
    _patch_scan_side_effects(monkeypatch, queue)
    seen: dict[str, Any] = {}

    def fake_discover(url: str, c: Config, fetch_page: Any = None) -> list[str]:
        seen["discover"] = fetch_page
        return [url]

    monkeypatch.setattr(orchestrator, "_default_discover", fake_discover)
    capture = FakeCapture(count=1, prefix="plain")
    my_fetch = lambda url, c: (200, "<html></html>", url)  # noqa: E731

    orchestrator.run_scan(
        SITE, cfg, fetch_page=my_fetch, capture=capture, classifier=make_stub_classifier(cfg)
    )
    assert seen["discover"] is my_fetch
    assert capture.fetch_seen is my_fetch


def test_run_scan_clean_skips_bundle_and_queue(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    """CLEAN 判定:不打证据包、不入列,但仍记录 scan 审计。"""
    queue = FakeQueue()
    bundles, audits = _patch_scan_side_effects(monkeypatch, queue)

    report = orchestrator.run_scan(
        SITE, cfg, capture=FakeCapture(prefix="normal"), classifier=make_stub_classifier(cfg)
    )

    assert report.verdict is Verdict.CLEAN
    assert report.needs_review is False
    assert bundles.calls == []
    assert queue.added == []
    assert audits.audits[0].events[0]["event"] == "scan"
    assert audits.audits[0].events[0]["verdict"] == "clean"


def test_run_scan_requires_at_least_one_member(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    """没有任何分类器成员成功评分 → RuntimeError(中文)。"""
    queue = FakeQueue()
    _patch_scan_side_effects(monkeypatch, queue)

    def broken_factory(name: str, c: Config) -> Any:
        raise ValueError(f"未注册的分类器 '{name}'")

    monkeypatch.setattr(orchestrator, "_default_classifier", broken_factory)

    with pytest.raises(RuntimeError, match="分类器成员"):
        orchestrator.run_scan(SITE, cfg, capture=FakeCapture())  # classifier 缺省走工厂


def test_load_missing_module_raises_chinese_runtimeerror() -> None:
    """兄弟模块缺失 → 中文 RuntimeError 指明模块未就位。"""
    with pytest.raises(RuntimeError, match="未就位"):
        orchestrator._load("netsentinel.no_such_module_xyz")


# ---------------------------------------------------------------------------
# run_submit
# ---------------------------------------------------------------------------
def _patch_submit_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    queue: FakeQueue,
    limiter: FakeRateLimiter,
) -> tuple[FakePlanFactory, FakeAuditFactory, FakeRateLimiterFactory]:
    monkeypatch.setattr(orchestrator, "_default_queue", lambda db_path: queue)
    plans = FakePlanFactory()
    monkeypatch.setattr(orchestrator, "_default_plan", plans)
    audits = FakeAuditFactory()
    monkeypatch.setattr(orchestrator, "_default_audit_logger", audits)
    rl_factory = FakeRateLimiterFactory(limiter)
    monkeypatch.setattr(orchestrator, "_default_rate_limiter", rl_factory)
    return plans, audits, rl_factory


def test_run_submit_success_marks_and_records(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    """approved 条目 + 频控通过 + 执行器 submitted=True → 回写队列/频控/审计。"""
    entry = FakeEntry(id=5, site_url=SITE, status="approved")
    queue = FakeQueue({5: entry})
    limiter = FakeRateLimiter(allowed=True)
    plans, audits, rl_factory = _patch_submit_side_effects(monkeypatch, queue, limiter)
    executor = FakeExecutor(
        ExecutionResult(ok=True, portal="12377", submitted=True, notes=["已提交(fake)"])
    )

    result = orchestrator.run_submit(5, Portal.P12377, cfg, dry_run=False, executor=executor)

    assert result.ok is True and result.submitted is True
    assert queue.marked == [5]
    assert limiter.recorded == 1
    assert plans.calls and plans.calls[0][1] is Portal.P12377
    assert plans.calls[0][0] is entry
    assert executor.calls[0]["dry_run"] is False
    assert executor.calls[0]["auto_confirm"] is False  # 人工确认门不可旁路
    # 频控以配置的间隔/每日上限构造,状态文件落在 data_dir 下
    assert len(rl_factory.calls) == 1
    state_path, min_interval, max_per_day = rl_factory.calls[0]
    assert state_path.endswith("rate_limit.json")
    assert (min_interval, max_per_day) == (
        cfg.submit_min_interval_s,
        cfg.submit_max_per_day,
    )
    # 审计:submit 事件
    event = audits.audits[0].events[0]
    assert event["event"] == "submit" and event["entry"] == 5
    assert event["portal"] == "12377" and event["submitted"] is True


def test_run_submit_rejects_non_approved(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    """非 approved(如 pending)→ RuntimeError,要求先人工审核批准。"""
    queue = FakeQueue({3: FakeEntry(id=3, status="pending")})
    limiter = FakeRateLimiter(allowed=True)
    _patch_submit_side_effects(monkeypatch, queue, limiter)
    executor = FakeExecutor(ExecutionResult(ok=True, submitted=True))

    with pytest.raises(RuntimeError, match="approved"):
        orchestrator.run_submit(3, Portal.SHDF, cfg, executor=executor)
    assert executor.calls == []
    assert queue.marked == []


def test_run_submit_missing_entry(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    """条目不存在 → 中文 RuntimeError。"""
    queue = FakeQueue({})
    _patch_submit_side_effects(monkeypatch, queue, FakeRateLimiter())
    with pytest.raises(RuntimeError, match="不存在"):
        orchestrator.run_submit(99, Portal.P12377, cfg)


def test_run_submit_rate_limited_returns_not_ok(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """频控拒绝 → ok=False、notes 含中文原因,不构建计划、不进执行器。"""
    reason = "当日(本地时区)已提交 5 次,达到每日上限 5 次,请明日再提交"
    queue = FakeQueue({5: FakeEntry(id=5, status="approved")})
    limiter = FakeRateLimiter(allowed=False, reason=reason)
    plans, _audits, rl_factory = _patch_submit_side_effects(monkeypatch, queue, limiter)
    executor = FakeExecutor(ExecutionResult(ok=True, submitted=True))

    result = orchestrator.run_submit(5, Portal.P12377, cfg, executor=executor)

    assert result.ok is False
    assert result.submitted is False
    assert result.notes == [reason]
    assert executor.calls == []  # 频控在执行器之前强制拦截
    assert plans.calls == []
    assert queue.marked == [] and limiter.recorded == 0
    assert rl_factory.calls[0][1:] == (cfg.submit_min_interval_s, cfg.submit_max_per_day)


def test_run_submit_not_submitted_skips_side_effects(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """执行器 submitted=False(如干跑)→ 不回写队列、不计频控、不记 submit 审计。"""
    queue = FakeQueue({5: FakeEntry(id=5, status="approved")})
    limiter = FakeRateLimiter(allowed=True)
    _plans, audits, _rl = _patch_submit_side_effects(monkeypatch, queue, limiter)
    executor = FakeExecutor(
        ExecutionResult(ok=True, portal="shdf", submitted=False, notes=["干跑完成"])
    )

    result = orchestrator.run_submit(5, Portal.SHDF, cfg, dry_run=True, executor=executor)

    assert result.ok is True and result.submitted is False
    assert executor.calls[0]["dry_run"] is True
    assert queue.marked == [] and limiter.recorded == 0
    assert audits.audits == []  # 未真实提交:审计器根本不会被构造


# ---------------------------------------------------------------------------
# CLI(__main__)
# ---------------------------------------------------------------------------
def test_cli_scan_prints_chinese_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture
) -> None:
    """scan 子命令:monkeypatch run_scan 后返回 0 并输出中文摘要。"""
    monkeypatch.chdir(tmp_path)  # 隔离 cwd:默认配置与日志都落在 tmp
    report = SiteReport(
        site_url="http://localhost/scan",
        agg_nsw_prob=0.97,
        nsw_image_count=4,
        verdict=Verdict.NSFW,
        needs_review=True,
    )
    monkeypatch.setattr(cli, "_default_run_scan", lambda url, c: report)
    monkeypatch.setattr(
        cli, "_find_entry_info", lambda c, r: (7, "data/evidence/fake.zip")
    )

    rc = cli.main(["scan", "--url", "http://localhost/scan"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "高置信色情" in out
    assert "0.97" in out
    assert "达标图片数" in out
    assert "data/evidence/fake.zip" in out
    assert "复核队列编号" in out and "7" in out


def test_cli_queue_forwards_args(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """queue 子命令:剩余参数前插 --db cfg.db_path 后转交 review_queue.main。"""
    monkeypatch.chdir(tmp_path)
    recorded: dict[str, Any] = {}

    def fake_queue_main(argv: list[str]) -> int:
        recorded["argv"] = argv
        return 0

    monkeypatch.setattr(cli, "_default_queue_main", fake_queue_main)

    rc = cli.main(["queue", "list", "--status", "pending"])

    assert rc == 0
    argv = recorded["argv"]
    assert argv[:2] == ["--db", "data/review_queue.db"]  # Config 默认 db_path
    assert argv[2:] == ["list", "--status", "pending"]


def test_cli_submit_flag_parsing(monkeypatch: pytest.MonkeyPatch, tmp_path, capsys) -> None:
    """submit 子命令:--dry-run/--exec/缺省 分别映射 dry_run True/False/None。"""
    monkeypatch.chdir(tmp_path)
    calls: dict[str, Any] = {}

    def fake_run_submit(
        entry_id: int, portal: Portal, cfg: Config, *, auto_confirm: bool = False,
        dry_run: bool | None = None,
    ) -> ExecutionResult:
        calls.update(
            entry_id=entry_id, portal=portal, auto_confirm=auto_confirm, dry_run=dry_run
        )
        return ExecutionResult(ok=True, portal=str(portal), submitted=False, notes=["干跑(测试)"])

    monkeypatch.setattr(cli, "_default_run_submit", fake_run_submit)

    assert cli.main(["submit", "--id", "3", "--portal", "12377", "--dry-run"]) == 0
    assert calls["entry_id"] == 3
    assert calls["portal"] is Portal.P12377
    assert calls["dry_run"] is True
    assert calls["auto_confirm"] is False  # CLI 永不自动确认
    assert "干跑(测试)" in capsys.readouterr().out

    assert cli.main(["submit", "--id", "3", "--portal", "shdf", "--exec"]) == 0
    assert calls["dry_run"] is False and calls["portal"] is Portal.SHDF

    assert cli.main(["submit", "--id", "3", "--portal", "shdf"]) == 0
    assert calls["dry_run"] is None  # 缺省交给 cfg.dry_run_default

    # 运行失败(ok=False)→ 退出码 1
    monkeypatch.setattr(
        cli,
        "_default_run_submit",
        lambda *a, **k: ExecutionResult(ok=False, notes=["频控拒绝"]),
    )
    assert cli.main(["submit", "--id", "3", "--portal", "12377"]) == 1


def test_cli_submit_has_no_auto_confirm_bypass(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    """不存在 --yes 之类的自动确认旁路参数:未知参数报用法错误(退出码 2)。"""
    monkeypatch.chdir(tmp_path)
    called = {"n": 0}

    def fake_run_submit(*a: Any, **k: Any) -> ExecutionResult:
        called["n"] += 1
        return ExecutionResult(ok=True)

    monkeypatch.setattr(cli, "_default_run_submit", fake_run_submit)
    rc = cli.main(["submit", "--id", "1", "--portal", "12377", "--yes"])
    assert rc == 2
    assert called["n"] == 0
    assert "无法识别" in capsys.readouterr().err


def test_cli_find_entry_info_with_fake_queue(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    """_find_entry_info:从队列倒序找同站点最新条目;CLEAN 直接返回 (None, "")。"""

    class E:
        def __init__(self, id: int, site_url: str, zip: str) -> None:
            self.id, self.site_url, self.evidence_zip = id, site_url, zip

    class Q:
        def list(self, status: str | None = None) -> list[E]:
            return [E(3, "http://other/", "old.zip"), E(9, SITE, "new.zip")]

    monkeypatch.setattr(cli, "_default_queue", lambda db_path: Q())

    suspect = SiteReport(site_url=SITE, verdict=Verdict.SUSPECT, needs_review=True)
    assert cli._find_entry_info(cfg, suspect) == (9, "new.zip")

    clean = SiteReport(site_url=SITE, verdict=Verdict.CLEAN, needs_review=False)
    assert cli._find_entry_info(cfg, clean) == (None, "")


def test_cli_scan_runtime_error_returns_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    """run_scan 抛 RuntimeError(如兄弟模块未就位)→ 中文错误 + 退出码 1。"""
    monkeypatch.chdir(tmp_path)

    def broken(url: str, cfg: Config) -> SiteReport:
        raise RuntimeError("模块 netsentinel.crawler.site_map 未就位:模拟缺失")

    monkeypatch.setattr(cli, "_default_run_scan", broken)
    rc = cli.main(["scan", "--url", "http://localhost/x"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "未就位" in err


# ---------------------------------------------------------------------------
# V5 升级锁定:telemetry(scan / submit / cli)+ 图片增强 stat 消重
# ---------------------------------------------------------------------------
from netsentinel import telemetry


def test_v5_run_scan_telemetry_verdict_gauges_and_total(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """run_scan:整体计时 scan.total;按档位计数 scan.verdict.<档位>;
    规模 gauge(scan.pages / scan.images)与成员循环耗时 gauge 落盘。"""
    queue = FakeQueue()
    _patch_scan_side_effects(monkeypatch, queue)
    snap0 = telemetry.snapshot()
    base_nsfw = snap0["counters"].get("scan.verdict.nsfw", 0.0)
    base_enqueued = snap0["counters"].get("scan.enqueued", 0.0)
    base_total = snap0["timers"].get("scan.total", {}).get("count", 0)

    report = orchestrator.run_scan(
        SITE, cfg, capture=FakeCapture(), classifier=make_stub_classifier(cfg)
    )

    assert report.verdict is Verdict.NSFW
    snap1 = telemetry.snapshot()
    assert snap1["counters"].get("scan.verdict.nsfw", 0.0) == base_nsfw + 1.0
    assert snap1["counters"].get("scan.enqueued", 0.0) == base_enqueued + 1.0
    assert snap1["timers"]["scan.total"]["count"] == base_total + 1
    assert snap1["gauges"]["scan.pages"] == 1.0  # _default_discover 只返回起点
    assert snap1["gauges"]["scan.images"] == 4.0  # FakeCapture 默认 4 张
    # 成员循环耗时 gauge:非负数(存在即代表计时挂钩生效)
    assert snap1["gauges"]["scan.members_seconds"] >= 0.0


def test_v5_run_scan_clean_verdict_counter(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    """CLEAN 判定同样计数(scan.verdict.clean),且不产生 scan.enqueued。"""
    queue = FakeQueue()
    _patch_scan_side_effects(monkeypatch, queue)
    base_clean = telemetry.snapshot()["counters"].get("scan.verdict.clean", 0.0)
    base_enq = telemetry.snapshot()["counters"].get("scan.enqueued", 0.0)

    orchestrator.run_scan(
        SITE, cfg, capture=FakeCapture(prefix="normal"), classifier=make_stub_classifier(cfg)
    )

    counters = telemetry.snapshot()["counters"]
    assert counters.get("scan.verdict.clean", 0.0) == base_clean + 1.0
    assert counters.get("scan.enqueued", 0.0) == base_enq  # 无入列


def test_v5_run_scan_error_counter_on_no_members(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """没有任何成员成功评分 → RuntimeError 前记一次 scan.errors。"""
    queue = FakeQueue()
    _patch_scan_side_effects(monkeypatch, queue)
    monkeypatch.setattr(
        orchestrator, "_default_classifier", lambda name, c: (_ for _ in ()).throw(
            ValueError(f"未注册的分类器 '{name}'")
        )
    )
    base = telemetry.snapshot()["counters"].get("scan.errors", 0.0)

    with pytest.raises(RuntimeError, match="分类器成员"):
        orchestrator.run_scan(SITE, cfg, capture=FakeCapture())

    assert telemetry.snapshot()["counters"].get("scan.errors", 0.0) == base + 1.0


def test_v5_enrich_images_dedupes_missing_file_stats(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """批量增强:同一(不存在的)路径只 stat 一次;单条调用保持旧行为。"""
    import pathlib

    real_is_file = pathlib.Path.is_file
    stat_paths: list[str] = []

    def counting_is_file(self: pathlib.Path, *args: Any, **kwargs: Any) -> bool:
        stat_paths.append(str(self))
        return real_is_file(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "is_file", counting_is_file)

    def img(path: str) -> ImageEvidence:
        return ImageEvidence(path=path, url=f"http://img/{path}", source_page=SITE)

    shared = os.path.join("no", "such", "a.jpg")  # 两条证据共享同一缺失路径
    other = os.path.join("no", "such", "b.jpg")
    imgs = [img(shared), img(other), img(shared)]  # 故意交错,验证缓存生效

    # 批量入口:3 条证据、2 个不同缺失路径 → 恰好 2 次 stat(旧版为 3 次)
    orchestrator._enrich_images(imgs)
    assert sorted(stat_paths) == sorted([shared, other])
    assert len(stat_paths) == 2

    # 单条入口(无缓存参数):行为与旧版一致,逐条各 stat 一次
    stat_paths.clear()
    orchestrator._enrich_image_evidence(img(shared))
    orchestrator._enrich_image_evidence(img(shared))
    assert len(stat_paths) == 2


def test_v5_run_submit_telemetry_counts_outcomes(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """run_submit:整体计时 submit.total;真实提交计 submit.submitted,
    频控拒绝计 submit.rate_limited(不进执行器)。"""
    entry = FakeEntry(id=5, site_url=SITE, status="approved")
    queue = FakeQueue({5: entry})
    limiter = FakeRateLimiter(allowed=True)
    _patch_submit_side_effects(monkeypatch, queue, limiter)
    executor = FakeExecutor(ExecutionResult(ok=True, portal="12377", submitted=True))
    snap0 = telemetry.snapshot()
    base_sub = snap0["counters"].get("submit.submitted", 0.0)
    base_rl = snap0["counters"].get("submit.rate_limited", 0.0)
    base_t = snap0["timers"].get("submit.total", {}).get("count", 0)

    orchestrator.run_submit(5, Portal.P12377, cfg, dry_run=False, executor=executor)

    snap1 = telemetry.snapshot()
    assert snap1["counters"].get("submit.submitted", 0.0) == base_sub + 1.0
    assert snap1["counters"].get("submit.rate_limited", 0.0) == base_rl
    assert snap1["timers"]["submit.total"]["count"] == base_t + 1

    # 频控拒绝路径
    monkeypatch.setattr(
        orchestrator, "_default_rate_limiter", FakeRateLimiterFactory(
            FakeRateLimiter(allowed=False, reason="每日上限")
        )
    )
    orchestrator.run_submit(5, Portal.P12377, cfg, executor=executor)
    snap2 = telemetry.snapshot()
    assert snap2["counters"].get("submit.rate_limited", 0.0) == base_rl + 1.0
    assert snap2["counters"].get("submit.submitted", 0.0) == base_sub + 1.0  # 未新增


def test_v5_cli_subcommand_telemetry(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """CLI 子命令:cli.scan 计数 + 计时;执行异常计 cli.errors。"""
    import netsentinel.__main__ as cli_mod

    monkeypatch.chdir(tmp_path)
    report = SiteReport(
        site_url="http://localhost/scan", verdict=Verdict.CLEAN, needs_review=False
    )
    monkeypatch.setattr(cli_mod, "_default_run_scan", lambda url, c: report)
    snap0 = telemetry.snapshot()
    base_scan = snap0["counters"].get("cli.scan", 0.0)
    base_errors = snap0["counters"].get("cli.errors", 0.0)
    base_t = snap0["timers"].get("cli.scan", {}).get("count", 0)

    assert cli_mod.main(["scan", "--url", "http://localhost/scan"]) == 0
    snap1 = telemetry.snapshot()
    assert snap1["counters"].get("cli.scan", 0.0) == base_scan + 1.0
    assert snap1["timers"]["cli.scan"]["count"] == base_t + 1
    assert snap1["counters"].get("cli.errors", 0.0) == base_errors

    def broken(url: str, cfg: Config) -> SiteReport:
        raise RuntimeError("模块 netsentinel.crawler.site_map 未就位:模拟缺失")

    monkeypatch.setattr(cli_mod, "_default_run_scan", broken)
    assert cli_mod.main(["scan", "--url", "http://localhost/x"]) == 1
    snap2 = telemetry.snapshot()
    assert snap2["counters"].get("cli.errors", 0.0) == base_errors + 1.0


# ---------------------------------------------------------------------------
# V11 接线:全链追踪(trace_enabled)+ 分歧弃权(abstain_enabled)
# 两开关均为 cfg 附加属性、getattr 缺省 False;默认关 = 与现状逐字节一致
# ---------------------------------------------------------------------------
class RecordingQueue:
    """复核队列 fake:记录 add 调用(含关键字实参),用于锁定入队口径。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def add(self, report: SiteReport, evidence_zip: str = "", **kwargs: Any) -> int:
        self.calls.append(
            {"report": report, "zip": evidence_zip, "kwargs": dict(kwargs)}
        )
        return 77


class FixedScoreClassifier:
    """固定分值成员 fake:每张图都给同一 nsfw_prob(用于构造成员分歧)。"""

    def __init__(self, name: str, prob: float) -> None:
        self.name = name
        self.prob = prob

    def classify_batch(self, imgs: list[ImageEvidence]) -> list[ImageScore]:
        return [
            ImageScore(image=i, model=self.name, nsfw_prob=self.prob, scores={})
            for i in imgs
        ]


@pytest.fixture()
def trace_kernel():
    """telemetry_trace 内核隔离夹具:前后清注册表与 audit_sink(全局状态)。"""
    tt = pytest.importorskip("netsentinel.telemetry_trace")
    tt.configure(audit_sink=None)
    tt.reset()
    yield tt
    tt.configure(audit_sink=None)
    tt.reset()


def _flat_cfg(cfg: Config) -> Config:
    """新接线测试的统一配置:关掉 fusion、双成员集成(分歧可控)。"""
    cfg.use_fusion = False
    cfg.ensemble_members = ["mA", "mB"]
    return cfg


def _run_two_member(
    monkeypatch: pytest.MonkeyPatch, cfg: Config, p_a: float, p_b: float
) -> tuple[SiteReport, RecordingQueue]:
    """两成员扫描:mA=注入分类器(固定 p_a)/ mB=经工厂(固定 p_b)。"""
    queue = RecordingQueue()
    _patch_scan_side_effects(monkeypatch, queue)
    monkeypatch.setattr(
        orchestrator,
        "_default_classifier",
        lambda name, c: FixedScoreClassifier("mB", p_b),
    )
    report = orchestrator.run_scan(
        SITE, cfg, capture=FakeCapture(), classifier=FixedScoreClassifier("mA", p_a)
    )
    return report, queue


def _collect_events(audits: FakeAuditFactory, event: str) -> list[dict[str, Any]]:
    return [
        e for a in audits.audits for e in a.events if e.get("event") == event
    ]


# ---------------------------------------------------------------------------
# 1. 两开关默认关:行为与现状逐字节一致(快照断言)
# ---------------------------------------------------------------------------
def test_v11_switches_absent_by_default(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    """V12 升格追认:trace_enabled / abstain_enabled 已为 Config 一等字段,
    纯默认值为 False(与升格前附加属性 getattr 缺省口径逐字一致)。"""
    default_cfg = Config()
    assert default_cfg.trace_enabled is False    # A210 V12 收录后:字段默认值断言
    assert default_cfg.abstain_enabled is False  # (原 hasattr 缺席断言随升格失效)
    assert getattr(default_cfg, "trace_enabled", False) is False
    assert getattr(default_cfg, "abstain_enabled", False) is False


def test_v11_switches_off_snapshot_identical_to_legacy(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """默认关:CLEAN 扫描 intel 无接线键、不入队、只计 scan.trace.disabled。"""
    queue = RecordingQueue()
    _patch_scan_side_effects(monkeypatch, queue)
    cfg.use_fusion = False
    snap0 = telemetry.snapshot()
    base_dis = snap0["counters"].get("scan.trace.disabled", 0.0)
    base_en = snap0["counters"].get("scan.trace.enabled", 0.0)
    base_ev = snap0["counters"].get("scan.abstain.evaluated", 0.0)
    base_tr = snap0["counters"].get("scan.abstain.triggered", 0.0)

    report = orchestrator.run_scan(
        SITE, cfg, capture=FakeCapture(prefix="normal"),
        classifier=make_stub_classifier(cfg),
    )

    assert report.verdict is Verdict.CLEAN
    assert "trace_id" not in report.intel and "abstain" not in report.intel
    assert report.needs_review is False and queue.calls == []
    counters = telemetry.snapshot()["counters"]
    assert counters.get("scan.trace.disabled", 0.0) == base_dis + 1.0
    assert counters.get("scan.trace.enabled", 0.0) == base_en
    assert counters.get("scan.abstain.evaluated", 0.0) == base_ev
    assert counters.get("scan.abstain.triggered", 0.0) == base_tr


def test_v11_switches_off_enqueue_call_signature_unchanged(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """默认关:入队调用口径与旧版逐字一致(仅位置实参,零关键字)。"""
    queue = RecordingQueue()
    _patch_scan_side_effects(monkeypatch, queue)
    cfg.use_fusion = False

    orchestrator.run_scan(
        SITE, cfg, capture=FakeCapture(), classifier=make_stub_classifier(cfg)
    )

    assert len(queue.calls) == 1
    assert queue.calls[0]["kwargs"] == {}  # 旧版:不带任何附加关键字
    assert queue.calls[0]["zip"] == "fake/evidence/bundle.zip"
    assert queue.calls[0]["report"].verdict is Verdict.NSFW


def test_v11_explicit_false_attributes_behave_identically(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """显式置 False 与不设置完全等价(向后兼容口径)。"""
    cfg.trace_enabled = False
    cfg.abstain_enabled = False
    cfg.use_fusion = False
    queue = RecordingQueue()
    _patch_scan_side_effects(monkeypatch, queue)
    base_dis = telemetry.snapshot()["counters"].get("scan.trace.disabled", 0.0)

    report = orchestrator.run_scan(
        SITE, cfg, capture=FakeCapture(prefix="normal"),
        classifier=make_stub_classifier(cfg),
    )

    assert report.verdict is Verdict.CLEAN
    assert "trace_id" not in report.intel and "abstain" not in report.intel
    assert queue.calls == []
    assert telemetry.snapshot()["counters"].get(
        "scan.trace.disabled", 0.0
    ) == base_dis + 1.0


# ---------------------------------------------------------------------------
# 2. trace 开:trace_id / span 树 / 审计旁路 / 异常 span / sink 复位
# ---------------------------------------------------------------------------
def test_v11_trace_enabled_span_tree_and_audit(
    monkeypatch: pytest.MonkeyPatch, cfg: Config, trace_kernel
) -> None:
    """trace 开:run 结果含 16-hex trace_id;四个阶段 span 成树可导出;
    审计旁路收到七键 span 事件;scan 审计事件携带 trace_id。"""
    tt = trace_kernel
    queue = RecordingQueue()
    bundles, audits = _patch_scan_side_effects(monkeypatch, queue)
    cfg.trace_enabled = True
    cfg.use_fusion = False
    base_en = telemetry.snapshot()["counters"].get("scan.trace.enabled", 0.0)

    report = orchestrator.run_scan(
        SITE, cfg, capture=FakeCapture(), classifier=make_stub_classifier(cfg)
    )

    assert telemetry.snapshot()["counters"].get(
        "scan.trace.enabled", 0.0
    ) == base_en + 1.0

    trace_id = report.intel.get("trace_id")
    assert isinstance(trace_id, str) and len(trace_id) == 16
    int(trace_id, 16)  # 合法 16 位 hex

    # span 树:四个根 span(顺序 = 开始序),全部终态、无错误
    tree = tt.export_trace_json(trace_id)
    assert tree["trace_id"] == trace_id
    assert [n["name"] for n in tree["spans"]] == [
        "scan.fetch", "scan.vlm", "scan.fusion", "scan.review",
    ]
    for node in tree["spans"]:
        assert node["parent_span_id"] is None  # 顺序块 = 根 span
        assert node["duration_ms"] is not None and node["duration_ms"] >= 0.0
        assert node["error"] is None
    json.dumps(tree)  # 可直接序列化导出

    # 审计旁路:七键 span 事件,trace_id 对齐,mock sink(FakeAudit)收到
    span_events = _collect_events(audits, "span")
    assert {ev["name"] for ev in span_events} >= {
        "scan.fetch", "scan.vlm", "scan.fusion", "scan.review",
    }
    for ev in span_events:
        assert ev["trace_id"] == trace_id
        assert set(ev) == {
            "event", "trace_id", "span_id", "parent", "name",
            "duration_ms", "error",
        }
    scan_events = _collect_events(audits, "scan")
    assert scan_events and scan_events[0]["trace_id"] == trace_id

    # 扫描结束 audit_sink 复位:之后的裸 span 不再进本扫描审计(零泄漏)
    sink_audit = audits.audits[0]  # _wire_trace_audit 最先创建的日志器
    events_before = len(sink_audit.events)
    with tt.span("scan.外部探针"):
        pass
    assert len(sink_audit.events) == events_before


def test_v11_trace_enabled_error_span_recorded_and_reraised(
    monkeypatch: pytest.MonkeyPatch, cfg: Config, trace_kernel
) -> None:
    """trace 开 + 扫描失败:异常 span 记录错误后原样重抛,绝不吞。"""
    tt = trace_kernel
    queue = RecordingQueue()
    bundles, audits = _patch_scan_side_effects(monkeypatch, queue)
    cfg.trace_enabled = True

    def broken_factory(name: str, c: Config) -> Any:
        raise ValueError(f"未注册的分类器 '{name}'")

    monkeypatch.setattr(orchestrator, "_default_classifier", broken_factory)

    with pytest.raises(RuntimeError, match="分类器成员"):
        orchestrator.run_scan(SITE, cfg, capture=FakeCapture())

    span_events = _collect_events(audits, "span")
    trace_ids = {ev["trace_id"] for ev in span_events}
    assert len(trace_ids) == 1  # 一链一 trace
    tree = tt.export_trace_json(trace_ids.pop())
    by_name = {n["name"]: n for n in tree["spans"]}
    assert "RuntimeError" in by_name["scan.vlm"]["error"]  # 异常落在 vlm span
    assert by_name["scan.fetch"]["error"] is None


def test_v11_trace_disabled_leaves_no_global_trace_state(
    monkeypatch: pytest.MonkeyPatch, cfg: Config, trace_kernel
) -> None:
    """默认关:全程不开 trace,run 结果无 trace_id,注册表零新增。"""
    tt = trace_kernel
    queue = RecordingQueue()
    _patch_scan_side_effects(monkeypatch, queue)
    cfg.use_fusion = False

    report = orchestrator.run_scan(
        SITE, cfg, capture=FakeCapture(prefix="normal"),
        classifier=make_stub_classifier(cfg),
    )

    assert "trace_id" not in report.intel
    assert tt.current_trace_id() is None
    assert tt.export_trace_json()["spans"] == []  # 无隐式 trace 产生


# ---------------------------------------------------------------------------
# 3. abstain 开:高分歧 needs_review + 提权;低分差异零扰动;判定不变
# ---------------------------------------------------------------------------
def test_v11_abstain_high_disagreement_clean_site_sent_to_review(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """高分歧(0.48 ≥ 0.35):CLEAN 站点也置 needs_review 直送人工复核,
    hint 写进 intel、priority_weight 随入队传给复核队列。"""
    _flat_cfg(cfg)
    cfg.abstain_enabled = True
    base_ev = telemetry.snapshot()["counters"].get("scan.abstain.evaluated", 0.0)
    base_tr = telemetry.snapshot()["counters"].get("scan.abstain.triggered", 0.0)

    report, queue = _run_two_member(monkeypatch, cfg, 0.02, 0.5)

    assert report.verdict is Verdict.CLEAN  # 判定档位不变(均值 0.26 < 0.5)
    assert report.agg_nsw_prob == pytest.approx(0.26, abs=1e-12)
    assert report.needs_review is True  # 弃权:复核优先信号,不是第四档
    hint = report.intel["abstain"]
    assert hint["abstain"] is True
    assert hint["source"] == "decision.abstain"
    assert hint["disagreement"] == pytest.approx(0.48)
    assert hint["threshold"] == pytest.approx(0.35)
    assert hint["priority_weight"] == pytest.approx(0.48)
    assert hint["p_nsfw"] == pytest.approx(0.26)
    assert "人工" in hint["reason"]
    # 入队:唯一一次调用,附带提权加项(其余口径与旧版一致)
    assert len(queue.calls) == 1
    assert queue.calls[0]["kwargs"] == {"priority_weight": pytest.approx(0.48)}
    counters = telemetry.snapshot()["counters"]
    assert counters.get("scan.abstain.evaluated", 0.0) == base_ev + 1.0
    assert counters.get("scan.abstain.triggered", 0.0) == base_tr + 1.0


def test_v11_abstain_never_changes_three_tier_verdict(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """重点红线断言:三档判定输出(档位/聚合分/达标计数/逐图集成分)在
    开关两侧逐项一致;弃权只增不减 needs_review,绝不改 verdict。"""
    _flat_cfg(cfg)
    for p_a, p_b, expected in (
        (0.02, 0.5, Verdict.CLEAN),    # 高分歧 + 低均值 → 基线 CLEAN
        (0.9, 0.1, Verdict.SUSPECT),   # 高分歧 + 均值 0.5 → 基线 SUSPECT
        (0.97, 0.6, Verdict.SUSPECT),  # 分歧 0.37 恰过阈 → 基线 SUSPECT
    ):
        cfg.abstain_enabled = False
        base, base_queue = _run_two_member(monkeypatch, cfg, p_a, p_b)
        cfg.abstain_enabled = True
        wired, wired_queue = _run_two_member(monkeypatch, cfg, p_a, p_b)

        # 三档判定公式输出零改动
        assert base.verdict is wired.verdict is expected
        assert wired.agg_nsw_prob == pytest.approx(base.agg_nsw_prob, abs=1e-12)
        assert wired.nsw_image_count == base.nsw_image_count
        assert [s.nsfw_prob for s in wired.image_scores] == [
            s.nsfw_prob for s in base.image_scores
        ]
        # needs_review:弃权后恒 True;基线 = (verdict != CLEAN)
        assert wired.needs_review is True
        assert base.needs_review is (expected != Verdict.CLEAN)
        # 弃权侧入队且带提权;基线侧口径与现状一致(CLEAN 不入队,其余原样)
        assert wired_queue.calls[0]["kwargs"].get("priority_weight") == \
            pytest.approx(max(p_a, p_b) - min(p_a, p_b))
        if expected is Verdict.CLEAN:
            assert base_queue.calls == []
        else:
            assert base_queue.calls[0]["kwargs"] == {}


def test_v11_abstain_low_disagreement_untouched(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """低分歧(0.03 < 0.35):不弃权,行为与现状一致(不入队/无 intel 键)。"""
    _flat_cfg(cfg)
    cfg.abstain_enabled = True
    base_ev = telemetry.snapshot()["counters"].get("scan.abstain.evaluated", 0.0)
    base_tr = telemetry.snapshot()["counters"].get("scan.abstain.triggered", 0.0)

    report, queue = _run_two_member(monkeypatch, cfg, 0.02, 0.05)

    assert report.verdict is Verdict.CLEAN
    assert report.needs_review is False
    assert "abstain" not in report.intel
    assert queue.calls == []
    counters = telemetry.snapshot()["counters"]
    assert counters.get("scan.abstain.evaluated", 0.0) == base_ev + 1.0
    assert counters.get("scan.abstain.triggered", 0.0) == base_tr  # 未触发


def test_v11_abstain_threshold_configurable(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """阈值经 cfg.abstain_threshold 覆盖:0.9 时 0.48 不弃权;
    恰等于 0.48 时闭边界弃权(对齐 arbiter 的 ≥ 语义)。"""
    _flat_cfg(cfg)
    cfg.abstain_enabled = True
    cfg.abstain_threshold = 0.9

    strict, strict_queue = _run_two_member(monkeypatch, cfg, 0.02, 0.5)
    assert strict.needs_review is False and strict_queue.calls == []

    cfg.abstain_threshold = 0.48  # 闭边界:分歧恰等于阈值 → 弃权
    edge, edge_queue = _run_two_member(monkeypatch, cfg, 0.02, 0.5)
    assert edge.needs_review is True
    assert edge.intel["abstain"]["threshold"] == pytest.approx(0.48)
    assert edge_queue.calls[0]["kwargs"]["priority_weight"] == pytest.approx(0.48)


def test_v11_abstain_single_member_never_triggers(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """单成员:分歧恒 0,永不弃权(即使阈值 0 也不弃权)。"""
    cfg.abstain_enabled = True
    cfg.use_fusion = False
    cfg.abstain_threshold = 0.0  # 极端阈值下单成员依旧安全
    queue = RecordingQueue()
    _patch_scan_side_effects(monkeypatch, queue)

    report = orchestrator.run_scan(
        SITE, cfg, capture=FakeCapture(prefix="normal"),
        classifier=make_stub_classifier(cfg),
    )

    assert report.verdict is Verdict.CLEAN
    assert report.needs_review is False
    assert "abstain" not in report.intel
    assert queue.calls == []


def test_v11_abstain_kernel_failure_degrades_safely(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """弃权内核未就位/异常:记 scan.abstain.skipped 后安全跳过,
    判定照常、无任何 needs_review / intel 副作用。"""
    _flat_cfg(cfg)
    cfg.abstain_enabled = True

    def broken_kernel() -> Any:
        raise RuntimeError("模块 netsentinel.decision.abstain 未就位:模拟缺失")

    monkeypatch.setattr(orchestrator, "_default_abstain_module", broken_kernel)
    base_skip = telemetry.snapshot()["counters"].get("scan.abstain.skipped", 0.0)

    report, queue = _run_two_member(monkeypatch, cfg, 0.02, 0.5)

    assert report.verdict is Verdict.CLEAN  # 扫描结论不受增强项失败影响
    assert report.needs_review is False
    assert "abstain" not in report.intel
    assert queue.calls == []
    assert telemetry.snapshot()["counters"].get(
        "scan.abstain.skipped", 0.0
    ) == base_skip + 1.0


def test_v11_trace_and_abstain_combined(
    monkeypatch: pytest.MonkeyPatch, cfg: Config, trace_kernel
) -> None:
    """两开关同时开:trace 与弃权互不干扰,intel 同时携带 trace_id 与
    abstain 提示;span 树完整可导出。"""
    tt = trace_kernel
    _flat_cfg(cfg)
    cfg.trace_enabled = True
    cfg.abstain_enabled = True

    report, queue = _run_two_member(monkeypatch, cfg, 0.02, 0.5)

    assert report.needs_review is True
    assert len(report.intel["trace_id"]) == 16
    assert report.intel["abstain"]["priority_weight"] == pytest.approx(0.48)
    assert queue.calls[0]["kwargs"] == {"priority_weight": pytest.approx(0.48)}
    tree = tt.export_trace_json(report.intel["trace_id"])
    assert [n["name"] for n in tree["spans"]] == [
        "scan.fetch", "scan.vlm", "scan.fusion", "scan.review",
    ]
