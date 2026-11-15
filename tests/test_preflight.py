"""P1-5 VLM 扫描预检测试(全部离线、零外呼、零真实门户)。

锁定五组语义:

1. **计量检测**:`consumes_vlm_budget` 标记(vlm/glm/cascade/failover=True,
   stub/基类缺省 False);`metered_members` 只返回计量成员、构造失败不抢答、
   同配置进程内记忆;
2. **预算闸**:计量扫描 + 当日预算耗尽 → 爬取前抛中文 RuntimeError,
   discover/capture/分类**零调用**(2026-10-13 事故形态的爬前拦截);
   非计量扫描(stub)零预算也照常;注入 classifier 整体绕过预检;
3. **健康账本**:评分系统性抛错记 dead → TTL 内下一站预检跳过;成功
   返回清除 dead;TTL 过期自动放行;零送审站不扰动既有状态;
4. **开关**:`vlm_budget_precheck=False` 逐字节回旧行为(预检零调用);
5. **集成**:run_scan 顶层接线(预算耗尽时 capture 间谍未被调用)。
"""
from __future__ import annotations

from typing import Any

import pytest

import netsentinel.pipeline.orchestrator as orchestrator
from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, ImageScore, PageSample
from netsentinel.vision import preflight
from netsentinel.vision.classifier_base import NsfwClassifier, register_classifier
from netsentinel.vision.preflight import (
    PREFLIGHT_SKIP_BUDGET,
    PREFLIGHT_SKIP_DEAD,
    metered_members,
    preflight_scan,
    record_scan_health,
    reset_preflight_state,
)

SITE = "http://localhost/a"


# ---------------------------------------------------------------------------
# fakes:注册名进注册表(走默认解析路径 → 预检生效),零外呼
# ---------------------------------------------------------------------------
class LocalFake(NsfwClassifier):
    """本地(非计量)替身:不消耗预算。"""

    name = "preflight_local"
    consumes_vlm_budget = False
    calls = 0

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg

    def classify(self, img: ImageEvidence) -> ImageScore:
        LocalFake.calls += 1
        return ImageScore(image=img, model=self.name, nsfw_prob=0.02)


class MeteredFake(NsfwClassifier):
    """计量替身:声明消耗预算,评分永远成功。"""

    name = "preflight_metered"
    consumes_vlm_budget = True
    calls = 0

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg

    def classify(self, img: ImageEvidence) -> ImageScore:
        MeteredFake.calls += 1
        return ImageScore(image=img, model=self.name, nsfw_prob=0.03)


class DeadMeteredFake(NsfwClassifier):
    """计量替身:批量评分抛系统性异常(模拟 V10.24 全败中止形态)。"""

    name = "preflight_dead"
    consumes_vlm_budget = True
    calls = 0

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg

    def classify(self, img: ImageEvidence) -> ImageScore:
        DeadMeteredFake.calls += 1
        raise RuntimeError("模拟:提供方 glm 接口调用失败 HTTP 429 余额不足")


register_classifier("preflight_local", LocalFake)
register_classifier("preflight_metered", MeteredFake)
register_classifier("preflight_dead", DeadMeteredFake)


def _ev(path: str, *, width: int = 300, height: int = 300, sha: str = "") -> ImageEvidence:
    return ImageEvidence(path=path, url=f"http://img/{path}", source_page=SITE,
                         sha256=sha, width=width, height=height)


@pytest.fixture(autouse=True)
def _fresh_state():
    reset_preflight_state()
    LocalFake.calls = MeteredFake.calls = DeadMeteredFake.calls = 0
    yield
    reset_preflight_state()


@pytest.fixture
def cfg(tmp_path) -> Config:
    c = Config(
        data_dir=str(tmp_path / "data"),
        evidence_dir=str(tmp_path / "evidence"),
        db_path=str(tmp_path / "data" / "review_queue.db"),
        audit_path=str(tmp_path / "data" / "audit.jsonl"),
        log_path=str(tmp_path / "data" / "logs" / "netsentinel.log"),
        vlm_cache_db=str(tmp_path / "vlm_cache.db"),
    )
    c.use_fusion = False
    c.ensemble_members = []
    return c


class SpyCapture:
    """记录是否被调用(预检生效时必须为 0 次)。"""

    def __init__(self, evidences: list[ImageEvidence]) -> None:
        self.evidences = evidences
        self.calls = 0

    def __call__(self, url: str, cfg: Config, *, fetch_page: Any = None) -> PageSample:
        self.calls += 1
        return PageSample(url=url, screenshot_path="", image_evidences=list(self.evidences))


def _patch_scan_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        orchestrator, "_default_discover", lambda url, c, fetch_page=None: [url]
    )
    monkeypatch.setattr(
        orchestrator, "_default_build_bundle",
        lambda report, c: type("B", (), {"zip_path": "fake/bundle.zip"})(),
    )

    class _Queue:
        def add(self, report, zip_path, **kw):
            return 1

        def close(self):
            pass

    monkeypatch.setattr(orchestrator, "_default_queue", lambda db_path: _Queue())

    class _Audit:
        def log_event(self, event, **fields):
            pass

    monkeypatch.setattr(orchestrator, "_default_audit_logger", lambda p: _Audit())


def _counter(name: str) -> float:
    return telemetry.snapshot()["counters"].get(name, 0.0)


def _exhaust_budget(c: Config) -> None:
    from netsentinel.vision.vlm_cache import VlmCache

    cache = VlmCache(str(c.vlm_cache_db), daily_limit=int(c.vlm_daily_budget))
    for _ in range(int(c.vlm_daily_budget)):
        cache.spend_one()
    cache.close()


BIG_EV = [_ev(f"b{i}.jpg", sha=chr(ord("a") + i) * 64) for i in range(6)]


# ---------------------------------------------------------------------------
# 1. 计量检测
# ---------------------------------------------------------------------------
def test_metering_flags_on_builtin_families() -> None:
    """远程 VLM 家族标记 True;本地内核与基类缺省 False。"""
    from netsentinel.vision.classifier_base import NsfwClassifier as Base

    assert Base.consumes_vlm_budget is False
    for mod_path, cls_name in [
        ("netsentinel.vision.multi_provider", "UniversalVLMClassifier"),
        ("netsentinel.vision.glm_adapter", "GlmVlmClassifier"),
        ("netsentinel.vision.cascade", "CascadeClassifier"),
        ("netsentinel.vision.failover", "FailoverClassifier"),
    ]:
        import importlib
        cls = getattr(importlib.import_module(mod_path), cls_name)
        assert cls.consumes_vlm_budget is True, f"{cls_name} 应为计量成员"


def test_metered_members_resolution(cfg: Config) -> None:
    cfg.classifier = "preflight_metered"
    cfg.ensemble_members = ["preflight_local"]
    assert metered_members(cfg) == ["preflight_metered"]
    # 同配置进程内记忆:二次调用同结果
    assert metered_members(cfg) == ["preflight_metered"]


def test_metered_members_empty_for_pure_local(cfg: Config) -> None:
    cfg.classifier = "preflight_local"
    cfg.ensemble_members = []
    assert metered_members(cfg) == []


def test_metered_members_construct_failure_not_answered(cfg: Config) -> None:
    """构造失败的成员(未装依赖等)不抢答——扫描主体自会给出准确报错。"""
    cfg.classifier = "no_such_classifier_xyz"
    cfg.ensemble_members = []
    assert metered_members(cfg) == []


# ---------------------------------------------------------------------------
# 2. 预算闸(爬取前拦截)
# ---------------------------------------------------------------------------
def test_budget_exhausted_blocks_before_crawl(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    _patch_scan_side_effects(monkeypatch)
    cfg.classifier = "preflight_metered"
    _exhaust_budget(cfg)
    spy = SpyCapture(BIG_EV)
    base = _counter("scan.skipped.no_budget")

    with pytest.raises(RuntimeError, match="预算已耗尽"):
        orchestrator.run_scan(SITE, cfg, capture=spy)

    assert spy.calls == 0  # 爬取零调用(10/13 事故形态的爬前拦截)
    assert MeteredFake.calls == 0  # 评分零调用
    assert _counter("scan.skipped.no_budget") == base + 1


def test_budget_ok_metered_scan_proceeds(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    _patch_scan_side_effects(monkeypatch)
    cfg.classifier = "preflight_metered"
    spy = SpyCapture(BIG_EV)
    report = orchestrator.run_scan(SITE, cfg, capture=spy)
    assert spy.calls == 1
    assert MeteredFake.calls == len(BIG_EV)
    assert report.verdict.value == "clean"


def test_local_scan_unaffected_by_exhausted_budget(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """纯本地扫描(stub 家族)零预算照常——预检零打扰。"""
    _patch_scan_side_effects(monkeypatch)
    cfg.classifier = "preflight_local"
    _exhaust_budget(cfg)
    spy = SpyCapture(BIG_EV)
    report = orchestrator.run_scan(SITE, cfg, capture=spy)
    assert spy.calls == 1
    assert LocalFake.calls == len(BIG_EV)


def test_injected_classifier_bypasses_preflight(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """classifier 注入 = 调用方接管识别链路:预检整体跳过(测试语义)。"""
    _patch_scan_side_effects(monkeypatch)
    cfg.classifier = "preflight_metered"
    _exhaust_budget(cfg)
    spy = SpyCapture(BIG_EV)
    clf = LocalFake()  # 注入本地替身(即便配置写计量名)
    report = orchestrator.run_scan(SITE, cfg, capture=spy, classifier=clf)
    assert spy.calls == 1  # 预检未拦截
    assert clf.calls == len(BIG_EV)


def test_preflight_knob_off_restores_old_behavior(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """开关关闭:预检零调用零拦截,行为与 P1-5 之前一致(替身照常评分)。"""
    _patch_scan_side_effects(monkeypatch)
    cfg.classifier = "preflight_metered"
    cfg.vlm_budget_precheck = False
    _exhaust_budget(cfg)  # 即便预算耗尽:旧路径由评分自身语义决定,预检不插手
    spy = SpyCapture(BIG_EV)
    report = orchestrator.run_scan(SITE, cfg, capture=spy)
    assert spy.calls == 1
    assert MeteredFake.calls == len(BIG_EV)
    assert report.verdict.value == "clean"


# ---------------------------------------------------------------------------
# 3. 健康账本
# ---------------------------------------------------------------------------
def test_dead_member_blocks_next_scan_within_ttl(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    _patch_scan_side_effects(monkeypatch)
    cfg.classifier = "preflight_dead"
    spy = SpyCapture(BIG_EV)
    # 第一站:评分阶段系统性抛错 → V10.24 无可用成员/中止语义 → 记 dead
    with pytest.raises(RuntimeError):
        orchestrator.run_scan(SITE, cfg, capture=spy)
    assert spy.calls == 1  # 第一站照常爬(冷启动无先验)
    assert DeadMeteredFake.calls == len(BIG_EV)

    # 第二站:预检读账本 → 爬取前跳过
    base_dead = _counter("scan.skipped.provider_dead")
    spy2 = SpyCapture(BIG_EV)
    DeadMeteredFake.calls = 0
    with pytest.raises(RuntimeError, match="系统性识别失败"):
        orchestrator.run_scan(SITE, cfg, capture=spy2)
    assert spy2.calls == 0  # 爬取零调用——空烧被拦
    assert DeadMeteredFake.calls == 0  # 评分零调用
    assert _counter("scan.skipped.provider_dead") == base_dead + 1


def test_dead_cleared_by_ok_record(cfg: Config) -> None:
    cfg.classifier = "preflight_metered"
    record_scan_health(["preflight_metered"], ok=False)
    with pytest.raises(RuntimeError, match="系统性识别失败"):
        preflight_scan(cfg, SITE)
    record_scan_health(["preflight_metered"], ok=True)  # 成功扫描清除 dead
    meta = preflight_scan(cfg, SITE)  # 不再抛
    assert meta is not None and meta["metered"] == ["preflight_metered"]


def test_dead_expires_after_ttl(cfg: Config) -> None:
    cfg.classifier = "preflight_metered"
    cfg.vlm_health_ttl_s = 0  # TTL=0:dead 立即过期
    record_scan_health(["preflight_metered"], ok=False)
    meta = preflight_scan(cfg, SITE)  # 过期 → 放行
    assert meta is not None


def test_empty_send_list_does_not_touch_health(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """全小图站(零送审)不回报健康——不扰动既有健康状态(债务留痕)。"""
    _patch_scan_side_effects(monkeypatch)
    cfg.classifier = "preflight_metered"
    calls: list[bool] = []
    monkeypatch.setattr(
        preflight, "record_scan_outcome",
        lambda meta, ok: calls.append(ok) if meta else None,
    )
    # 全小图站:零送审 → 不回报
    tiny = [_ev(f"t{i}.jpg", width=64, height=64, sha=chr(ord("a") + i) * 64)
            for i in range(3)]
    report = orchestrator.run_scan(SITE, cfg, capture=SpyCapture(tiny))
    assert report.verdict.value == "clean"
    assert calls == []  # 未回报(既有 dead/ok 状态原样保留)

    # 正常站:有送审 → 回报 ok
    orchestrator.run_scan(SITE, cfg, capture=SpyCapture(BIG_EV))
    assert calls == [True]


# ---------------------------------------------------------------------------
# 4. 预检返回上下文与直接调用语义
# ---------------------------------------------------------------------------
def test_preflight_returns_none_for_local(cfg: Config) -> None:
    cfg.classifier = "preflight_local"
    assert preflight_scan(cfg, SITE) is None


def test_preflight_low_budget_warns_not_blocks(cfg: Config, caplog) -> None:
    """余量偏小只警告不拦截(部分预算耗尽属正常运营)。"""
    from netsentinel.vision.vlm_cache import VlmCache

    cache = VlmCache(str(cfg.vlm_cache_db), daily_limit=int(cfg.vlm_daily_budget))
    for _ in range(int(cfg.vlm_daily_budget) - 3):  # 只剩 3(< 成员数×5)
        cache.spend_one()
    cache.close()
    cfg.classifier = "preflight_metered"
    with caplog.at_level("WARNING"):
        meta = preflight_scan(cfg, SITE)  # 不抛
    assert meta is not None
    assert any("余量偏小" in r.message for r in caplog.records)


def test_record_scan_outcome_none_meta_is_noop() -> None:
    preflight.record_scan_outcome(None, ok=False)  # 零副作用,不抛即可
