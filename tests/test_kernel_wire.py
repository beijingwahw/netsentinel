"""内核装配线测试(V7 · A139 / A194)—— netsentinel/pipeline/kernel_wire.py。

覆盖(契约 §2 A139 行 + 红线 29/31):

1. ``assemble``:各开关组合装配(SPRT 参数接线 / 融合选择 / 执行器选择 /
   flags 快照),内核缺失的优雅降级(中文日志 + 回退 v6);
2. ``wrap_classifier``:无 lru 原样透传 / 有 lru 得到 CachedClassifier /
   box.wrap_classifier 使用 box.lru;
3. ``run_scan_v7``:
   - **开关全关 = v6 透传**(deps 逐参原样、报告对象原样返回、零 intel 侵入,
     与真实 orchestrator.run_scan 的报告逐字段一致——离线 e2e 对照);
   - SPRT 开启写 ``intel["sprt"]`` 三键且**判定字段一字不动**;
   - 可靠性融合开启补算覆写 ``intel["fusion"]``(rule/member_weights/agg
     数值确定性断言,只升不降兜底);
   - LRU 与 classifier 同时注入才包装;lru 缺失 / cache2 缺失优雅降级;
   - 两后处理并存时 intel 键互不覆盖;
4. ``test_v7_bench_*``(红线 31,操作计数、零墙钟):
   - LRU 重复评分 5 次 → 内层分类器恰调用 1 次;
   - SPRT 早停:20 张强 clean 序列送审 2/8 张、省 6 张(计数守恒断言)。

A194 站群图谱通电追加覆盖:

5. ``graph_wire`` 开关默认关:orchestrator 批末**零调用**写口(快照断言,
   Config 无该属性);开启时 ``_default_graph_wire`` 被调用且判定不动;
   写图异常安全降级(扫描不中断 + telemetry 计数);
6. ``wire_graph_from_scan``:真实 A46 EvidenceGraph 落库(shared_image /
   shared_template 边 + 权重)、mock 图谱写口逐方法断言、phash LSH 近邻
   端到端建 phash_near 边(真实 PNG + 真实 Pillow,缺 Pillow 自动跳过);
   A204 追加:phash 登记跨批次闭环(批 1 登记 → 批 2 同图新站 →
   phash_near 边,PhashRegistry + 持久 MultiTableLSH 双写)、登记步缺失
   内核安全降级;A214 追加:_graph_add_phash_near 公开 add_edge 直调
   (私有口回退移除,缺公开口中文快失败)、查询步库规模阈值切换
   fastpath/slowpath(召回等价 + 边界 + telemetry);
6d. A229 多哈希生产接线:翻转站群跨批端到端(批 1 三哈希登记 + 镜像
   索引落盘 → 批 2 水平翻转图经 mirror 源建 **mirror_near 独立候选边**
   (A244,红线级验收场景)+ 团伙默认不并团断言)、开关默认关快照一致
   (单哈希现状逐项不变)、fastpath 与多哈希共存、phash/mirror 双通道
   分离与同 sha 去重、逐图降级计数 / phash2 内核缺失整批回退、
   mirror 距离配置与 _fp_parts 旧口径兼容;
7. ``resolve_gangs``:connectivity(默认,一条弱边并团 = 现状;A244 起
   mirror_near 候选边默认**不**并团,显式开关才纳入)vs community
   (Louvain 加权社区,弱桥不误并;阈值 / 模板降权 / **镜像降权数学
   对照** / 分辨率),``resolve_gangs_from_config`` 开关读取与缺省兼容。

全部离线:orchestrator 经 ``_default_run_scan`` 替身或注入依赖,零网络、
零浏览器、零 VLM。
"""
from __future__ import annotations

import logging
import math
import pathlib
import sqlite3
import struct
import zlib

import pytest

from netsentinel import telemetry
from netsentinel.contracts import (
    Config,
    ImageEvidence,
    ImageScore,
    PageSample,
    SiteReport,
    Verdict,
)
from netsentinel.decision.sprt import SPRT
from netsentinel.pipeline import kernel_wire
from netsentinel.pipeline.kernel_wire import (
    KernelBox,
    assemble,
    resolve_gangs,
    resolve_gangs_from_config,
    run_scan_v7,
    wire_graph_from_scan,
    wrap_classifier,
)
from netsentinel.submit.executor_session import SessionExecutor
from netsentinel.vision.cache2 import CachedClassifier, MemoLRU

# ---------------------------------------------------------------------------
# 构造辅助(全部确定性,零 IO)
# ---------------------------------------------------------------------------
_URL = "http://127.0.0.1:8000/"


def _cfg(**overrides: object) -> Config:
    """测试配置:默认全关(含 browser_session_reuse),按需覆盖。"""
    cfg = Config(
        use_sprt=False,
        use_reliability_fusion=False,
        browser_session_reuse=False,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _img(i: int, sha: str = "") -> ImageEvidence:
    return ImageEvidence(
        path=f"/nonexistent/img{i}.png",
        url=f"{_URL}img{i}.png",
        source_page=_URL,
        sha256=sha or f"{i:064x}",
        width=300,
        height=300,
    )


def _score(i: int, prob: float, model: str = "ensemble") -> ImageScore:
    return ImageScore(image=_img(i), model=model, nsfw_prob=prob, scores={})


def _report(
    scores: list[ImageScore] | None = None,
    verdict: Verdict = Verdict.CLEAN,
    intel: dict | None = None,
    agg: float = 0.0,
    nsw: int = 0,
    needs_review: bool = False,
) -> SiteReport:
    return SiteReport(
        site_url=_URL,
        image_scores=scores if scores is not None else [],
        verdict=verdict,
        intel=intel if intel is not None else {},
        agg_nsw_prob=agg,
        nsw_image_count=nsw,
        needs_review=needs_review,
    )


class _FakeRunScan:
    """orchestrator.run_scan 替身:记录逐参调用,返回预置报告对象。"""

    def __init__(self, report: SiteReport) -> None:
        self.report = report
        self.calls: list[tuple[str, Config, dict]] = []

    def __call__(self, url: str, cfg: Config, **deps: object) -> SiteReport:
        self.calls.append((url, cfg, deps))
        return self.report


class _CountingClassifier:
    """NsfwClassifier 最小替身:对 classify 调用计数(CachedClassifier 兼容)。"""

    name = "stub"

    def __init__(self) -> None:
        self.calls = 0

    def classify(self, img: ImageEvidence) -> ImageScore:
        self.calls += 1
        return ImageScore(image=img, model="stub", nsfw_prob=0.10, scores={})


def _patch_missing(monkeypatch: pytest.MonkeyPatch, module_name: str) -> None:
    """让 kernel_wire._load 对指定模块抛中文 RuntimeError(模拟内核未就位)。"""
    real_load = kernel_wire._load

    def fake_load(name: str) -> object:
        if name == module_name:
            raise RuntimeError(f"模块 {name} 未就位:No module named '{name}'")
        return real_load(name)

    monkeypatch.setattr(kernel_wire, "_load", fake_load)


def _seed_tracker(tmp_path, glm_p: float = 0.9) -> None:
    """播种本地可靠性反馈:glm 报得准(brier 0.01)、stub 报离谱(brier 0.81)。"""
    from netsentinel.decision.reliability import ReliabilityTracker

    tracker = ReliabilityTracker(str(tmp_path / "reliability.jsonl"))
    for _ in range(5):
        tracker.record("glm", glm_p, True)
        tracker.record("stub", glm_p, False)


# ---------------------------------------------------------------------------
# 1. assemble:开关组合装配
# ---------------------------------------------------------------------------
def test_assemble_all_off_matches_v6() -> None:
    """开关全关:box 各字段均为 v6 关闭态,flags 全 False。"""
    box = assemble(_cfg())
    assert isinstance(box, KernelBox)
    assert box.sprt is None
    assert box.executor_cls is None
    assert box.lru is None
    assert box.flags == {
        "sprt": False,
        "reliability_fusion": False,
        "browser_session_reuse": False,
        "lru_cache": False,
    }
    assert callable(box.fuse_fn)
    assert box.fuse_fn.reliable is False  # 惰性回退既有 fusion.fuse


def test_assemble_sprt_on_wires_cfg_params() -> None:
    """use_sprt=True:得到以 cfg.sprt_alpha/beta 构造的 SPRT 实例。"""
    cfg = _cfg(use_sprt=True, sprt_alpha=0.1, sprt_beta=0.2)
    box = assemble(cfg)
    assert isinstance(box.sprt, SPRT)
    assert box.sprt.alpha == pytest.approx(0.1)
    assert box.sprt.beta == pytest.approx(0.2)
    assert box.sprt.state() == "continue"
    assert box.flags["sprt"] is True


def test_assemble_fuse_fn_reliable_variant() -> None:
    """use_reliability_fusion=True:fuse_fn 走 fuse_reliable(rule/权重键在席)。"""
    cfg = _cfg(use_reliability_fusion=True)
    box = assemble(cfg)
    assert box.flags["reliability_fusion"] is True
    assert box.fuse_fn.reliable is True

    scores = [
        _score(1, 0.9, model="glm:glm-5.3"),
        _score(2, 0.9, model="glm:glm-5.3"),
        _score(3, 0.2, model="stub"),
        _score(4, 0.2, model="stub"),
        _score(5, 0.9, model="ensemble"),
    ]
    report = _report(scores=scores, verdict=Verdict.SUSPECT, agg=0.55)
    out = box.fuse_fn(report, {}, {}, {"page_nsfw_prob": 0.9}, cfg, tracker=None)
    assert out is report  # 就地更新同一对象
    fusion = report.intel["fusion"]
    assert fusion["rule"] == "reliable-weighted 只升不降"
    assert "agg_reliable" in fusion
    assert set(fusion["member_weights"]) == {"glm", "stub"}


def test_assemble_fuse_fn_off_variant_routes_to_fusion_fuse() -> None:
    """use_reliability_fusion=False:fuse_fn 惰性路由到既有 fusion.fuse(v6)。"""
    cfg = _cfg()
    box = assemble(cfg)
    report = _report(scores=[_score(1, 0.6)], verdict=Verdict.SUSPECT, agg=0.6)
    out = box.fuse_fn(report, {"risk": 0.9}, {}, {}, cfg)
    assert out is report
    fusion = report.intel["fusion"]
    assert fusion["rule"] == "只升不降:辅助特征仅加强复核,不降低图像判定"
    assert "agg_reliable" not in fusion  # 旧口径无可靠性新增键


def test_assemble_executor_selection_by_switch() -> None:
    """browser_session_reuse=True → SessionExecutor 类;False → None(v6 路径)。"""
    box_on = assemble(_cfg(browser_session_reuse=True))
    assert box_on.executor_cls is SessionExecutor
    assert box_on.flags["browser_session_reuse"] is True

    box_off = assemble(_cfg(browser_session_reuse=False))
    assert box_off.executor_cls is None
    assert box_off.flags["browser_session_reuse"] is False


def test_assemble_flags_record_requested_switches() -> None:
    """flags 快照逐开关记录实际生效状态(sprt/融合开、会话关)。"""
    box = assemble(_cfg(use_sprt=True, use_reliability_fusion=True))
    assert box.flags == {
        "sprt": True,
        "reliability_fusion": True,
        "browser_session_reuse": False,
        "lru_cache": False,
    }


def test_assemble_missing_sprt_kernel_degrades(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """decision/sprt 缺失:sprt=None、flags 回退 False,中文告警不抛异常。"""
    caplog.set_level(logging.WARNING, logger="netsentinel.pipeline.kernel_wire")
    _patch_missing(monkeypatch, "netsentinel.decision.sprt")
    box = assemble(_cfg(use_sprt=True))
    assert box.sprt is None
    assert box.flags["sprt"] is False
    assert any(
        "SPRT" in r.getMessage() and "降级" in r.getMessage() for r in caplog.records
    )


def test_assemble_missing_session_executor_degrades(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """executor_session 缺失:executor_cls=None(= executor_playwright),中文告警。"""
    caplog.set_level(logging.WARNING, logger="netsentinel.pipeline.kernel_wire")
    _patch_missing(monkeypatch, "netsentinel.submit.executor_session")
    box = assemble(_cfg(browser_session_reuse=True))
    assert box.executor_cls is None
    assert box.flags["browser_session_reuse"] is False
    assert any("会话复用" in r.getMessage() for r in caplog.records)


def test_assemble_missing_fusion_reliable_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """fusion_reliable 缺失:降级为既有 fusion.fuse 且可正常调用。"""
    caplog.set_level(logging.WARNING, logger="netsentinel.pipeline.kernel_wire")
    _patch_missing(monkeypatch, "netsentinel.decision.fusion_reliable")
    cfg = _cfg(use_reliability_fusion=True)
    box = assemble(cfg)
    assert box.flags["reliability_fusion"] is False
    assert box.fuse_fn.reliable is False
    assert any("可靠性融合" in r.getMessage() for r in caplog.records)

    # 降级后 fuse_fn 仍可调用(走 fusion.fuse,v6 口径)
    report = _report(scores=[_score(1, 0.6)], verdict=Verdict.SUSPECT, agg=0.6)
    box.fuse_fn(report, {}, {}, {}, cfg)
    assert report.intel["fusion"]["rule"].startswith("只升不降")


# ---------------------------------------------------------------------------
# 2. wrap_classifier:LRU 包装工厂
# ---------------------------------------------------------------------------
def test_wrap_classifier_without_lru_returns_inner_identity() -> None:
    """lru 缺席 → 原样返回 inner(不缓存,v6 行为;可选包装的"关")。"""
    inner = _CountingClassifier()
    assert wrap_classifier(inner, None) is inner
    assert wrap_classifier(inner) is inner


def test_wrap_classifier_with_lru_returns_cached_classifier() -> None:
    """lru 在席 → CachedClassifier(inner, lru),命名可追溯、lru 引用不变。"""
    lru = MemoLRU(8)
    wrapped = wrap_classifier(_CountingClassifier(), lru)
    assert isinstance(wrapped, CachedClassifier)
    assert wrapped.name == "cached(stub)"
    assert wrapped.lru is lru


def test_box_wrap_classifier_uses_box_lru() -> None:
    """KernelBox.wrap_classifier 用 box.lru;lru 未注入时原样返回。"""
    box = KernelBox()  # 缺省:lru=None
    assert box.sprt is None and box.fuse_fn is None and box.flags == {}
    inner = _CountingClassifier()
    assert box.wrap_classifier(inner) is inner

    box2 = assemble(_cfg())
    box2.lru = MemoLRU(4)
    wrapped = box2.wrap_classifier(inner)
    assert isinstance(wrapped, CachedClassifier)
    assert wrapped.lru is box2.lru


# ---------------------------------------------------------------------------
# 3. run_scan_v7:透传 / SPRT / 可靠性融合 / LRU / 降级
# ---------------------------------------------------------------------------
def test_run_scan_v7_all_off_is_pure_v6_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开关全关 + 未传 lru:deps 逐参透传、报告原样返回、零后处理零 intel 侵入。"""
    cfg = _cfg()
    report = _report(
        scores=[_score(i, 0.02) for i in range(20)],
        verdict=Verdict.CLEAN,
        intel={"origin": 1},
    )
    fake = _FakeRunScan(report)
    monkeypatch.setattr(kernel_wire, "_default_run_scan", fake)

    inner = _CountingClassifier()
    out = run_scan_v7(_URL, cfg, fetch_page=None, classifier=inner)

    assert out is report  # 同一对象原样返回(run_scan 原样返回对象)
    assert len(fake.calls) == 1
    url, got_cfg, deps = fake.calls[0]
    assert (url, got_cfg) == (_URL, cfg)
    assert deps == {"fetch_page": None, "classifier": inner}
    assert deps["classifier"] is inner  # 无 lru → 不包装,原分类器透传
    assert report.intel == {"origin": 1}  # 零 intel 侵入
    assert report.verdict is Verdict.CLEAN  # 判定不动


def test_run_scan_v7_forwards_deps_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetch_page / capture 等依赖以同一函数对象逐参透传给 run_scan。"""
    def fetch_page(url: str, cfg: Config) -> tuple[int, str, str]:
        return 200, "<html></html>", url

    def capture(url: str, cfg: Config, *, fetch_page=None) -> PageSample:
        return PageSample(url=url)

    fake = _FakeRunScan(_report())
    monkeypatch.setattr(kernel_wire, "_default_run_scan", fake)
    run_scan_v7(_URL, _cfg(), fetch_page=fetch_page, capture=capture)

    assert len(fake.calls) == 1
    deps = fake.calls[0][2]
    assert deps == {"fetch_page": fetch_page, "capture": capture}
    assert deps["fetch_page"] is fetch_page and deps["capture"] is capture


def test_run_scan_v7_wraps_injected_classifier_with_lru(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """classifier + lru 同时注入 → orchestrator 收到 CachedClassifier 包装。"""
    fake = _FakeRunScan(_report())
    monkeypatch.setattr(kernel_wire, "_default_run_scan", fake)
    inner = _CountingClassifier()
    lru = MemoLRU(4)

    run_scan_v7(_URL, _cfg(), classifier=inner, lru=lru)

    got = fake.calls[0][2]["classifier"]
    assert isinstance(got, CachedClassifier)
    assert got.inner is inner
    assert got.lru is lru
    assert got.name == "cached(stub)"


def test_run_scan_v7_lru_without_classifier_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只传 lru 不传 classifier:无可包装对象,依赖面不变、不报错。"""
    fake = _FakeRunScan(_report())
    monkeypatch.setattr(kernel_wire, "_default_run_scan", fake)
    out = run_scan_v7(_URL, _cfg(), lru=MemoLRU(4))
    assert out is fake.report
    assert fake.calls[0][2] == {}  # 未注入任何额外依赖


def test_run_scan_v7_sprt_on_writes_intel_and_keeps_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """use_sprt=True:写 intel['sprt'] 三键;判定字段一字不动(红线)。"""
    cfg = _cfg(use_sprt=True, vlm_max_images_per_site=8)
    scores = [_score(i, 0.02) for i in range(20)]  # 强 clean 序列:第 2 张即停
    report = _report(scores=scores, verdict=Verdict.NSFW, agg=0.95, nsw=5)
    fake = _FakeRunScan(report)
    monkeypatch.setattr(kernel_wire, "_default_run_scan", fake)

    run_scan_v7(_URL, cfg)

    assert report.intel["sprt"] == {"verdict": "clean", "n_used": 2, "budget_saved": 6}
    # SPRT 是预算参考:绝不改变站点判定(哪怕 SPRT 说 clean、站点判 NSFW)
    assert report.verdict is Verdict.NSFW
    assert report.agg_nsw_prob == pytest.approx(0.95)
    assert report.nsw_image_count == 5
    assert report.needs_review is False


def test_run_scan_v7_sprt_continue_exhausts_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不判停序列(p≈0.73 零点附近):送审满额 n_used=budget、budget_saved=0。"""
    cfg = _cfg(use_sprt=True, vlm_max_images_per_site=8)
    scores = [_score(i, 0.73) for i in range(8)]  # LLR≈-0.005/张,8 张仍 continue
    report = _report(scores=scores)
    monkeypatch.setattr(kernel_wire, "_default_run_scan", _FakeRunScan(report))

    run_scan_v7(_URL, cfg)

    assert report.intel["sprt"] == {
        "verdict": "continue",
        "n_used": 8,
        "budget_saved": 0,
    }


def test_run_scan_v7_sprt_off_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """use_sprt=False:即使 ensemble 条目在席也不写 intel['sprt']。"""
    report = _report(scores=[_score(i, 0.02) for i in range(20)])
    monkeypatch.setattr(kernel_wire, "_default_run_scan", _FakeRunScan(report))
    run_scan_v7(_URL, _cfg())
    assert "sprt" not in report.intel


def test_run_scan_v7_reliability_on_recomputes_fusion(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """use_reliability_fusion=True 且 intel 已有 fusion:覆写为可靠性加权口径。"""
    _seed_tracker(tmp_path)  # glm 准(brier 0.01)/ stub 离谱(brier 0.81)
    cfg = _cfg(use_reliability_fusion=True, data_dir=str(tmp_path))
    scores = [
        _score(1, 0.9, model="glm:glm-5.3"),
        _score(2, 0.9, model="glm:glm-5.3"),
        _score(3, 0.2, model="stub"),
        _score(4, 0.2, model="stub"),
        _score(5, 0.9, model="ensemble"),
    ]
    old_fusion = {"prob": 0.6, "rule": "只升不降:辅助特征仅加强复核,不降低图像判定"}
    report = _report(
        scores=scores,
        verdict=Verdict.SUSPECT,
        agg=0.55,
        intel={
            "url": {},
            "text": {},
            "page_vlm": {"page_nsfw_prob": 0.5},
            "fusion": old_fusion,
        },
    )
    monkeypatch.setattr(kernel_wire, "_default_run_scan", _FakeRunScan(report))

    run_scan_v7(_URL, cfg)

    fusion = report.intel["fusion"]
    assert fusion is not old_fusion  # 已被覆写为新口径
    assert fusion["rule"] == "reliable-weighted 只升不降"
    # 权重确定性:w_glm=1/0.06、w_stub=1/0.86 → glm 话语权 ≈ 0.9348
    assert fusion["member_weights"]["glm"] == pytest.approx(0.9348, abs=1e-3)
    assert fusion["member_weights"]["stub"] == pytest.approx(0.0652, abs=1e-3)
    # agg_reliable = 0.9348*0.9 + 0.0652*0.2 ≈ 0.8543(报得准的话语权大)
    assert fusion["agg_reliable"] == pytest.approx(0.8543, abs=1e-3)
    # 三路情报键保留
    assert set(report.intel) == {"url", "text", "page_vlm", "fusion"}


def test_run_scan_v7_reliability_only_escalates_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """只升不降兜底:融合分不足时 NSFW/needs_review 也绝不被降级洗白。"""
    _seed_tracker(tmp_path)
    cfg = _cfg(use_reliability_fusion=True, data_dir=str(tmp_path))
    scores = [
        _score(1, 0.9, model="glm:glm-5.3"),
        _score(2, 0.9, model="glm:glm-5.3"),
        _score(3, 0.2, model="stub"),
        _score(4, 0.2, model="stub"),
    ]
    report = _report(
        scores=scores,
        verdict=Verdict.NSFW,
        needs_review=True,
        agg=0.95,
        nsw=5,
        intel={
            "url": {},
            "text": {},
            "page_vlm": {"page_nsfw_prob": 0.1},  # 页面情报偏干净
            "fusion": {"prob": 0.95, "rule": "旧口径"},
        },
    )
    monkeypatch.setattr(kernel_wire, "_default_run_scan", _FakeRunScan(report))

    run_scan_v7(_URL, cfg)

    assert report.verdict is Verdict.NSFW  # 档位只升不降
    assert report.needs_review is True  # 复核单向恒真
    assert report.intel["fusion"]["rule"] == "reliable-weighted 只升不降"


def test_run_scan_v7_reliability_off_leaves_fusion_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """use_reliability_fusion=False:intel 及 fusion 对象原样保留,零后处理。"""
    old_fusion = {"prob": 0.6, "rule": "只升不降:辅助特征仅加强复核,不降低图像判定"}
    intel = {"url": {}, "text": {}, "page_vlm": {}, "fusion": old_fusion}
    report = _report(scores=[_score(1, 0.6)], verdict=Verdict.SUSPECT, intel=intel)
    monkeypatch.setattr(kernel_wire, "_default_run_scan", _FakeRunScan(report))

    out = run_scan_v7(_URL, _cfg())

    assert out is report
    assert report.intel is intel  # intel 字典对象未动
    assert report.intel["fusion"] is old_fusion  # fusion 条目对象未动
    assert report.intel["fusion"]["prob"] == 0.6


def test_run_scan_v7_reliability_skipped_without_fusion_intel(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """开关开但 intel 无 'fusion'(use_fusion 关/融合被跳过):不补算、不注入。"""
    cfg = _cfg(use_reliability_fusion=True, data_dir=str(tmp_path))
    intel = {"url": {}, "note": "fusion-skipped"}
    report = _report(scores=[_score(1, 0.6)], verdict=Verdict.SUSPECT, intel=intel)
    monkeypatch.setattr(kernel_wire, "_default_run_scan", _FakeRunScan(report))

    run_scan_v7(_URL, cfg)

    assert report.intel is intel
    assert report.intel == {"url": {}, "note": "fusion-skipped"}  # 未新增键


def test_run_scan_v7_sprt_intel_survives_fusion_rewrite(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """双开关并存:可靠性融合覆写 intel 后,'sprt' 键被回填保留。"""
    _seed_tracker(tmp_path)
    cfg = _cfg(
        use_sprt=True,
        use_reliability_fusion=True,
        vlm_max_images_per_site=8,
        data_dir=str(tmp_path),
    )
    scores = (
        [_score(i, 0.02) for i in range(20)]  # 20 条 ensemble:SPRT 第 2 张停
        + [_score(21, 0.9, model="glm:glm-5.3"), _score(22, 0.9, model="glm:glm-5.3")]
        + [_score(23, 0.2, model="stub"), _score(24, 0.2, model="stub")]
    )
    report = _report(
        scores=scores,
        verdict=Verdict.SUSPECT,
        agg=0.55,
        intel={
            "url": {},
            "text": {},
            "page_vlm": {"page_nsfw_prob": 0.5},
            "fusion": {"prob": 0.6, "rule": "只升不降:辅助特征仅加强复核,不降低图像判定"},
        },
    )
    monkeypatch.setattr(kernel_wire, "_default_run_scan", _FakeRunScan(report))

    run_scan_v7(_URL, cfg)

    assert report.intel["sprt"] == {"verdict": "clean", "n_used": 2, "budget_saved": 6}
    assert report.intel["fusion"]["rule"] == "reliable-weighted 只升不降"
    assert set(report.intel) == {"url", "text", "page_vlm", "fusion", "sprt"}


def test_run_scan_v7_missing_cache2_degrades_to_passthrough(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """cache2 缺失:分类器按 v6 原样透传,中文告警,扫描不中断。"""
    caplog.set_level(logging.WARNING, logger="netsentinel.pipeline.kernel_wire")
    _patch_missing(monkeypatch, "netsentinel.vision.cache2")
    fake = _FakeRunScan(_report())
    monkeypatch.setattr(kernel_wire, "_default_run_scan", fake)
    inner = _CountingClassifier()

    out = run_scan_v7(_URL, _cfg(), classifier=inner, lru=MemoLRU(4))

    assert out is fake.report
    assert fake.calls[0][2]["classifier"] is inner  # 未包装
    assert any(
        "LRU 缓存包装失败" in r.getMessage() and "未就位" in r.getMessage()
        for r in caplog.records
    )


def test_run_scan_v7_missing_sprt_module_degrades(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """decision/sprt 缺失:报告原样返回、无 intel['sprt'],中文告警。"""
    caplog.set_level(logging.WARNING, logger="netsentinel.pipeline.kernel_wire")
    _patch_missing(monkeypatch, "netsentinel.decision.sprt")
    report = _report(scores=[_score(i, 0.02) for i in range(20)])
    monkeypatch.setattr(kernel_wire, "_default_run_scan", _FakeRunScan(report))

    out = run_scan_v7(_URL, _cfg(use_sprt=True))

    assert out is report
    assert "sprt" not in report.intel
    assert report.verdict is Verdict.CLEAN
    assert any("SPRT" in r.getMessage() and "降级" in r.getMessage() for r in caplog.records)


def test_run_scan_v7_missing_fusion_reliable_degrades(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """fusion_reliable 缺失:保留既有 fusion 结论(对象不动),中文告警。"""
    caplog.set_level(logging.WARNING, logger="netsentinel.pipeline.kernel_wire")
    _patch_missing(monkeypatch, "netsentinel.decision.fusion_reliable")
    old_fusion = {"prob": 0.6, "rule": "只升不降:辅助特征仅加强复核,不降低图像判定"}
    report = _report(
        scores=[_score(1, 0.6)],
        verdict=Verdict.SUSPECT,
        intel={"url": {}, "text": {}, "page_vlm": {}, "fusion": old_fusion},
    )
    monkeypatch.setattr(kernel_wire, "_default_run_scan", _FakeRunScan(report))

    out = run_scan_v7(_URL, _cfg(use_reliability_fusion=True))

    assert out is report
    assert report.intel["fusion"] is old_fusion  # 既有结论原样保留
    assert any("可靠性融合" in r.getMessage() for r in caplog.records)


def test_run_scan_v7_all_off_matches_run_scan_report(tmp_path) -> None:
    """真实 orchestrator 离线对照:开关全关时 run_scan_v7 报告与 run_scan 逐字段一致。

    注入 fetch_page/capture/classifier 全离线依赖,经真实
    discover→capture→enrich→ensemble→assess→audit 链路各跑一遍 v6/v7。
    """
    pytest.importorskip("netsentinel.crawler.site_map")
    pytest.importorskip("netsentinel.vision.ensemble")
    pytest.importorskip("netsentinel.decision.verdict")
    from netsentinel.pipeline.orchestrator import run_scan

    def fetch_page(url: str, cfg: Config) -> tuple[int, str, str]:
        return 200, "<html><body>普通页面</body></html>", url

    def capture(url: str, cfg: Config, *, fetch_page=None) -> PageSample:
        return PageSample(url=url, image_evidences=[_img(1), _img(2)])

    class _Member:
        name = "stub"

        def classify_batch(self, imgs: list[ImageEvidence]) -> list[ImageScore]:
            return [ImageScore(image=i, model="stub", nsfw_prob=0.1) for i in imgs]

    cfg = Config(
        max_pages=1,
        use_fusion=False,
        use_sprt=False,
        use_reliability_fusion=False,
        browser_session_reuse=False,
        data_dir=str(tmp_path),
        audit_path=str(tmp_path / "audit.jsonl"),
        db_path=str(tmp_path / "queue.db"),
        evidence_dir=str(tmp_path / "evidence"),
    )

    report_v6 = run_scan(_URL, cfg, fetch_page=fetch_page, capture=capture, classifier=_Member())
    report_v7 = run_scan_v7(_URL, cfg, fetch_page=fetch_page, capture=capture, classifier=_Member())

    dict_v6 = report_v6.as_dict()
    dict_v7 = report_v7.as_dict()
    dict_v6.pop("created_at", None)  # 时间戳逐次必然不同
    dict_v7.pop("created_at", None)
    assert dict_v7 == dict_v6  # 逐字段一致 = v7 包装零行为差异
    assert report_v7.intel == {}  # 零 intel 侵入
    assert report_v7.verdict is Verdict.CLEAN  # 低分站点判 CLEAN(与 v6 相同)


# ---------------------------------------------------------------------------
# 4. 基准(红线 31:操作计数断言,零墙钟)
# ---------------------------------------------------------------------------
def test_v7_bench_lru_wrap_inner_calls_5_to_1() -> None:
    """LRU 包装:同图重复 classify 5 次 → 内层恰调用 1 次(计数 5→1)。"""
    inner = _CountingClassifier()
    clf = wrap_classifier(inner, MemoLRU(16))
    img = _img(1)
    for _ in range(5):
        clf.classify(img)

    assert inner.calls == 1  # 5 次评分只打穿 1 次内层
    stats = clf.lru.hit_stats()
    assert stats["hits"] == 4 and stats["misses"] == 1

    # 对照(无 LRU 的 v6 直通):内层必须被调用 5 次
    bare = _CountingClassifier()
    for _ in range(5):
        bare.classify(img)
    assert bare.calls == 5


def test_v7_bench_sprt_budget_saved_by_operation_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPRT 早停演算:20 张强 clean 序列送审 2/8 张、省 6 张(计数守恒)。"""
    cfg = _cfg(use_sprt=True, vlm_max_images_per_site=8)

    # 早停序列:全部 0.02,SPRT 第 2 张即判停
    report = _report(scores=[_score(i, 0.02) for i in range(20)])
    monkeypatch.setattr(kernel_wire, "_default_run_scan", _FakeRunScan(report))
    run_scan_v7(_URL, cfg)
    sprt = report.intel["sprt"]
    budget = 8
    assert sprt["n_used"] + sprt["budget_saved"] == budget  # 计数守恒
    assert sprt["n_used"] == 2  # 无早停基线需送审 8 张,实际 2 张
    assert sprt["budget_saved"] == 6  # 省 75% 送审预算

    # 对照(不判停序列):p≈0.73 时送审满额,一张不省
    report2 = _report(scores=[_score(i, 0.73) for i in range(8)])
    monkeypatch.setattr(kernel_wire, "_default_run_scan", _FakeRunScan(report2))
    run_scan_v7(_URL, cfg)
    assert report2.intel["sprt"]["n_used"] == 8
    assert report2.intel["sprt"]["budget_saved"] == 0


# ---------------------------------------------------------------------------
# 5-6. A194 站群图谱通电:graph_wire 开关门控 + wire_graph_from_scan
# ---------------------------------------------------------------------------
_SITE_A = "http://alpha.example.com/"  # 两个**不同可注册域**(canonical 口径)
_SITE_B = "http://beta.example.org/"  # ——同域会被 is_same_site 判同站


def _graph_cfg(tmp_path) -> Config:
    """图谱通电测试配置:库文件全部落在 tmp_path,开关默认关。"""
    cfg = Config(
        use_sprt=False,
        use_reliability_fusion=False,
        browser_session_reuse=False,
        graph_db=str(tmp_path / "graph.db"),
        phash_db=str(tmp_path / "phash.db"),
        data_dir=str(tmp_path),
        audit_path=str(tmp_path / "audit.jsonl"),
        db_path=str(tmp_path / "queue.db"),
        evidence_dir=str(tmp_path / "evidence"),
    )
    cfg.phash_lsh_bands = 4
    return cfg


def _page(
    url: str,
    *,
    shas: list[str] | None = None,
    image_urls: list[str] | None = None,
    paths: list[str] | None = None,
    hints: list[str] | None = None,
) -> PageSample:
    """构造一页采样(图片证据按 sha / url / path 逐项给出)。"""
    shas = shas or []
    image_urls = image_urls or [f"{_URL}img{i}.png" for i in range(len(shas))]
    paths = paths or [f"/nonexistent/img{i}.png" for i in range(len(shas))]
    imgs = [
        ImageEvidence(
            path=path,
            url=image_url,
            source_page=url,
            sha256=sha,
            width=300,
            height=300,
        )
        for sha, image_url, path in zip(shas, image_urls, paths)
    ]
    return PageSample(url=url, image_evidences=imgs, text_hint_hits=hints or [])


def _write_png(path: pathlib.Path, rows: list[list[int]], comment: str = "") -> None:
    """把灰度像素矩阵写为 stdlib 手写 PNG(zlib + CRC,零三方写图依赖)。

    ``comment`` 非空时附加一个 ancillary tEXt 块:解码像素与无注释版
    **逐像素一致**(pHash 相同)、文件字节不同(sha256 不同)——模拟
    "同一张图换站重传时容器级重新打包"。
    """
    size = len(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    parts = [
        b"\x89PNG\r\n\x1a\n",
        chunk(b"IHDR", ihdr),
        chunk(b"IDAT", zlib.compress(raw)),
    ]
    if comment:
        parts.append(chunk(b"tEXt", b"Comment\x00" + comment.encode("ascii")))
    parts.append(chunk(b"IEND", b""))
    path.write_bytes(b"".join(parts))


def _tiny_png(path: pathlib.Path, shade: int = 128, comment: str = "") -> None:
    """stdlib 手写 16x16 灰度 PNG(见 :func:`_write_png`)。

    注:均匀图不同 shade 的 pHash 受 DCT 浮点 epsilon 噪声影响并**不**
    保证相同,故不用 shade 造差异。
    """
    size = 16
    _write_png(path, [[shade] * size for _ in range(size)], comment)


def _structured_png(path: pathlib.Path, *, flip: bool = False) -> None:
    """确定性 32x32 非对称构图 PNG(A229 翻转场景用,见 :func:`_write_png`)。

    像素 = 水平不对称的平滑双向渐变 + 左侧圆块 + 右上矩形块(与
    vision/phash2 的照片式合成构图同族,但左右不对称);``flip=True``
    输出其**水平翻转**(列序反转)。性质(实测锁定,翻转站群场景前提):
    phash 距离 ≈ 30(远超阈值 8,单哈希口径不可关联)、mirror_hash
    逐位相等(镜像规范形在翻转轨道上恒定)。
    """
    size = 32
    rows: list[list[int]] = []
    for y in range(size):
        row: list[int] = []
        for x in range(size):
            u, v = x / size, y / size
            val = (
                96 + 48 * math.sin(2 * math.pi * u + 0.7)
                + 32 * math.cos(2 * math.pi * v + 1.9)
            )
            if (u - 0.3) ** 2 + (v - 0.45) ** 2 < 0.04:
                val = 220.0
            elif 0.6 < u < 0.9 and 0.1 < v < 0.4:
                val = 40.0
            row.append(max(0, min(255, int(val))))
        rows.append(row)
    if flip:
        rows = [row[::-1] for row in rows]
    _write_png(path, rows)


class _RecordingGraphWire:
    """orchestrator._default_graph_wire 替身:记录调用,可配置抛错。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, Config, list]] = []
        self.error = error

    def __call__(self, url: str, cfg: Config, pages: list) -> dict:
        self.calls.append((url, cfg, list(pages)))
        if self.error is not None:
            raise self.error
        return {"sites": 1}


def _offline_scan_deps():
    """真实 run_scan 的全离线依赖(fetch/capture/classifier)。"""

    def fetch_page(url: str, cfg: Config) -> tuple[int, str, str]:
        return 200, "<html><body>普通页面</body></html>", url

    def capture(url: str, cfg: Config, *, fetch_page=None) -> PageSample:
        return _page(url, shas=["ab" * 32, "cd" * 32])

    class _Member:
        name = "stub"

        def classify_batch(self, imgs: list[ImageEvidence]) -> list[ImageScore]:
            return [ImageScore(image=i, model="stub", nsfw_prob=0.1) for i in imgs]

    return fetch_page, capture, _Member()


def _run_offline_scan(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> SiteReport:
    """跑真实 orchestrator.run_scan(离线依赖 + 旁路证据包/队列/审计)。"""
    from netsentinel.pipeline import orchestrator

    monkeypatch.setattr(
        orchestrator, "_default_discover", lambda url, c, fetch_page=None: [url]
    )
    monkeypatch.setattr(orchestrator, "_default_build_bundle", lambda r, c: None)
    monkeypatch.setattr(orchestrator, "_default_queue", lambda db: type(
        "Q", (), {"add": staticmethod(lambda r, z="": 1)}
    )())
    monkeypatch.setattr(
        orchestrator, "_default_audit_logger", lambda p: type(
            "A", (), {"log_event": staticmethod(lambda event, **f: None)}
        )()
    )
    fetch_page, capture, member = _offline_scan_deps()
    return orchestrator.run_scan(
        _URL, cfg, fetch_page=fetch_page, capture=capture, classifier=member
    )


def test_graph_wire_switch_defaults_off_zero_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """默认关闭(快照断言):graph_wire 为 V11 一等字段,默认 False(与升格前
    附加属性缺省口径一致);真实 run_scan 批末**零调用**写图工厂;
    显式 False 行为与缺省完全一致。"""
    from netsentinel.pipeline import orchestrator

    assert Config().graph_wire is False
    assert getattr(Config(), "graph_wire", False) is False

    recorder = _RecordingGraphWire()
    monkeypatch.setattr(orchestrator, "_default_graph_wire", recorder)
    cfg = _graph_cfg(tmp_path)
    report = _run_offline_scan(cfg, monkeypatch)

    assert recorder.calls == []  # 未通电:写图口零调用
    assert report.verdict is Verdict.CLEAN

    cfg_false = _graph_cfg(tmp_path)
    cfg_false.graph_wire = False  # 显式 False = 缺省
    _run_offline_scan(cfg_false, monkeypatch)
    assert recorder.calls == []


def test_graph_wire_on_invokes_writer_and_keeps_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """开启通电:批末恰好调用一次写图(参数逐项核对),判定与报告不受影响,
    telemetry 记 scan.graph_wire.ok。"""
    from netsentinel.pipeline import orchestrator

    recorder = _RecordingGraphWire()
    monkeypatch.setattr(orchestrator, "_default_graph_wire", recorder)
    cfg = _graph_cfg(tmp_path)
    cfg.graph_wire = True
    base = telemetry.snapshot()["counters"].get("scan.graph_wire.ok", 0.0)

    report = _run_offline_scan(cfg, monkeypatch)

    assert len(recorder.calls) == 1
    url, got_cfg, pages = recorder.calls[0]
    assert url == _URL and got_cfg is cfg
    assert len(pages) == 1 and pages[0].url == _URL  # 采样页面透传给写图口
    assert {i.sha256 for i in pages[0].image_evidences} == {"ab" * 32, "cd" * 32}
    assert report.verdict is Verdict.CLEAN  # 判定不动(通电是纯增强)
    snap = telemetry.snapshot()["counters"]
    assert snap.get("scan.graph_wire.ok", 0.0) == base + 1


def test_graph_wire_writer_failure_degrades_safely(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """写图失败安全降级:扫描照常返回报告、不抛异常,中文告警 +
    telemetry 记 scan.graph_wire.skipped(不影响扫描主流程)。"""
    from netsentinel.pipeline import orchestrator

    caplog.set_level(logging.WARNING, logger="netsentinel.pipeline.orchestrator")
    recorder = _RecordingGraphWire(error=RuntimeError("模块 netsentinel.intel.graph 未就位"))
    monkeypatch.setattr(orchestrator, "_default_graph_wire", recorder)
    cfg = _graph_cfg(tmp_path)
    cfg.graph_wire = True
    base = telemetry.snapshot()["counters"].get("scan.graph_wire.skipped", 0.0)

    report = _run_offline_scan(cfg, monkeypatch)

    assert report.verdict is Verdict.CLEAN  # 主流程不受影响
    assert len(recorder.calls) == 1  # 写图口确实被尝试过
    snap = telemetry.snapshot()["counters"]
    assert snap.get("scan.graph_wire.skipped", 0.0) == base + 1
    assert any("站群图谱通电已跳过" in r.getMessage() for r in caplog.records)


def test_wire_graph_from_scan_real_evidence_graph(tmp_path) -> None:
    """真实 A46 落库:本站图片 / 模板入库,与预置站点折叠出 shared_image /
    shared_template 边(权重 = Jaccard);本地文件缺失时 phash 步安全跳过。"""
    cfg = _graph_cfg(tmp_path)
    from netsentinel.intel.graph import EvidenceGraph

    # 预置站点 B:与 A 共享一张图(sha "ef"*32)与一个模板(与 A 首页同形状)
    page_a1 = _page(f"{_SITE_A}video/list_12.html", shas=["ab" * 32, "ef" * 32])
    template_a1 = kernel_wire._page_template_key(page_a1)
    assert template_a1  # 非空模板键(URL 路径形状归一后仍有 token)
    with EvidenceGraph(cfg.graph_db) as seed:
        seed.add_site(_SITE_B)
        seed.add_image("ef" * 32, _SITE_B)
        seed.add_template(template_a1, _SITE_B)

    counts = wire_graph_from_scan(
        _SITE_A,
        cfg,
        [
            page_a1,
            _page(f"{_SITE_A}video/list_99.html", shas=["ef" * 32]),  # 同形分页
        ],
    )
    assert counts["sites"] == 1
    assert counts["images"] == 2  # ab / ef 两个去重 sha
    assert counts["templates"] == 2  # 两页图片集不同 → 两个模板键(均登记)
    assert counts["shared_image_edges"] == 1  # A-B 经 ef 折叠
    assert counts["shared_template_edges"] == 1  # A-B 经同形模板折叠
    assert counts["phash_near_edges"] == 0  # 图片文件不存在 → phash 跳过

    with EvidenceGraph(cfg.graph_db) as check:
        related = {r["site"]: r for r in check.related_sites(_SITE_A, depth=1)}
        assert _SITE_B in related
        assert "shared_image" in related[_SITE_B]["via"]
        assert related[_SITE_B]["weight"] > 0.0
        stats = check.stats()
        assert stats["sites"] == 2 and stats["images"] == 2
    # 幂等:重跑(数据未变)新增边为 0
    counts_again = wire_graph_from_scan(_SITE_A, cfg, [page_a1])
    assert counts_again["shared_image_edges"] == 0
    assert counts_again["shared_template_edges"] == 0


def test_wire_graph_from_scan_mock_write_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """mock 图谱写口:开启通电时 add_site / add_image / add_template /
    link_shared_images / link_templates 逐一被调用;phash 内核缺失降级不中断。"""
    real_load = kernel_wire._load

    class _FakeGraph:
        instances: list["_FakeGraph"] = []

        def __init__(self, db_path: str) -> None:
            self.db_path = db_path
            self.sites: list[str] = []
            self.images: list[tuple[str, str]] = []
            self.templates: list[tuple[str, str]] = []
            self.link_images_calls = 0
            self.link_templates_calls = 0
            self.closed = False
            _FakeGraph.instances.append(self)

        def add_site(self, url: str) -> None:
            self.sites.append(url)

        def add_image(self, sha: str, url: str) -> None:
            self.images.append((sha, url))

        def add_template(self, key: str, url: str) -> None:
            self.templates.append((key, url))

        def link_shared_images(self) -> int:
            self.link_images_calls += 1
            return 2

        def link_templates(self) -> int:
            self.link_templates_calls += 1
            return 3

        def close(self) -> None:
            self.closed = True

    fake_module = type("M", (), {"EvidenceGraph": _FakeGraph})()

    def fake_load(name: str) -> object:
        if name == "netsentinel.intel.graph":
            return fake_module
        if name == "netsentinel.intel.phash":  # 模拟 phash 内核未就位 → 降级
            raise RuntimeError(f"模块 {name} 未就位")
        return real_load(name)

    monkeypatch.setattr(kernel_wire, "_load", fake_load)
    cfg = _graph_cfg(tmp_path)
    base = telemetry.snapshot()["counters"].get("graph_wire.phash_near_skipped", 0.0)

    counts = wire_graph_from_scan(_SITE_A, cfg, [_page(_SITE_A, shas=["ab" * 32])])

    assert counts == {
        "sites": 1,
        "images": 1,
        "templates": 1,
        "shared_image_edges": 2,  # link_shared_images 的返回值透传
        "shared_template_edges": 3,
        "registered": 0,  # phash 内核缺失 → 登记步安全跳过
        "phash_near_edges": 0,  # phash 内核缺失 → 安全跳过
    }
    instance = _FakeGraph.instances[-1]
    assert instance.sites == [_SITE_A]  # add_site 写口被调用
    assert instance.images == [("ab" * 32, _SITE_A)]  # add_image 写口
    assert instance.templates and instance.templates[0][1] == _SITE_A  # add_template
    assert instance.link_images_calls == 1 and instance.link_templates_calls == 1
    assert instance.closed is True  # 连接收尾
    snap = telemetry.snapshot()["counters"]
    assert snap.get("graph_wire.phash_near_skipped", 0.0) == base + 1
    assert snap.get("graph_wire.scans", 0.0) >= 1


def test_wire_graph_phash_near_lsh_end_to_end(tmp_path) -> None:
    """phash LSH 通电端到端:同内容两张 PNG(真实 Pillow 计算 DCT pHash),
    B 站哈希预登记 → A 站扫描批末 LSH 命中并写入 phash_near 边(权重 1.0)。"""
    pytest.importorskip("PIL")  # 未安装 Pillow 的环境按缺依赖跳过
    from netsentinel.intel.graph import EvidenceGraph
    from netsentinel.intel.phash import PhashRegistry, phash

    img_a = tmp_path / "a.png"
    img_b = tmp_path / "b.png"
    _tiny_png(img_a)  # 同 shade:内容逐字节相同 → pHash 距离 0
    _tiny_png(img_b)
    fingerprint = phash(str(img_a))

    cfg = _graph_cfg(tmp_path)
    registry = PhashRegistry(cfg.phash_db)
    try:
        registry.register("ff" * 32, fingerprint, _SITE_B)  # B 站见过同图
    finally:
        registry.close()

    counts = wire_graph_from_scan(
        _SITE_A,
        cfg,
        [_page(_SITE_A, shas=["ab" * 32], paths=[str(img_a)])],
    )

    assert counts["phash_near_edges"] == 1  # LSH 命中 B 站 → 一条 phash_near 边
    with EvidenceGraph(cfg.graph_db) as check:
        edges = {
            (str(e["src"]), str(e["dst"]), str(e["kind"])): float(e["weight"])
            for e in check.export_json()["edges"]
        }
        phash_edges = {k: w for k, w in edges.items() if k[2] == "phash_near"}
        assert len(phash_edges) == 1
        (src, dst, kind), weight = next(iter(phash_edges.items()))
        assert kind == "phash_near"
        assert {src, dst} == {f"site:{_SITE_A}", f"site:{_SITE_B}"}
        assert weight == pytest.approx(1.0)  # 1 命中 / 1 张成功哈希


# ---------------------------------------------------------------------------
# 6b. A204→A214:phash_near 公开 add_edge 直调(A214 移除私有口回退)+ 登记闭环
# ---------------------------------------------------------------------------
def test_graph_add_phash_near_public_add_edge_only() -> None:
    """A214 清理:公开口在席时直调 A46 ``add_edge``(kind/weight 关键字形、
    端点站点补建),旧私有 ``_upsert_site_edge`` 零调用;缺公开口的极端
    旧库实例中文快失败(RuntimeError,由 _wire_phash_near 统一降级);
    单条边写入异常仍容错返回 False。"""
    class _ModernGraph:
        def __init__(self) -> None:
            self.sites: list[str] = []
            self.edge_calls: list[tuple[str, str, str, float]] = []
            self.legacy_calls = 0

        def add_site(self, url: str) -> None:
            self.sites.append(url)

        def add_edge(
            self, src: str, dst: str, *, kind: str, weight: float = 1.0
        ) -> bool:
            self.edge_calls.append((src, dst, kind, weight))
            return True

        def _upsert_site_edge(self, a: str, b: str, kind: str, weight: float) -> bool:
            self.legacy_calls += 1
            return True

    modern = _ModernGraph()
    assert kernel_wire._graph_add_phash_near(modern, _SITE_A, _SITE_B, 0.5) is True
    assert modern.edge_calls == [(_SITE_A, _SITE_B, "phash_near", 0.5)]  # 公开口直调
    assert modern.legacy_calls == 0  # 私有探测口已移除:即使还在也零调用
    assert sorted(modern.sites) == sorted([_SITE_A, _SITE_B])  # 端点站点补建

    class _LegacyGraph:  # 旧版 A46 实例(只有私有口)→ 中文快失败
        def add_site(self, url: str) -> None:
            pass

        def _upsert_site_edge(self, a: str, b: str, kind: str, weight: float) -> bool:
            return True

    with pytest.raises(RuntimeError, match="add_edge"):
        kernel_wire._graph_add_phash_near(_LegacyGraph(), _SITE_A, _SITE_B, 0.5)

    class _BareGraph:  # 连私有口都没有的空鸭子 → 同样中文快失败
        pass

    with pytest.raises(RuntimeError, match="A214"):
        kernel_wire._graph_add_phash_near(_BareGraph(), _SITE_A, _SITE_B, 0.5)

    class _FlakyGraph:  # 公开口在席但写入抛错 → 单条边容错 False(不抛出)
        def add_site(self, url: str) -> None:
            pass

        def add_edge(self, src: str, dst: str, **kwargs: object) -> bool:
            raise TypeError("模拟写入失败")

    assert (
        kernel_wire._graph_add_phash_near(_FlakyGraph(), _SITE_A, _SITE_B, 0.5)
        is False
    )


def test_wire_graph_phash_register_closes_cross_batch_loop(tmp_path) -> None:
    """A204 登记闭环端到端:批 1(A 站)扫描→登记(phash 入哈希库 + 持久
    多表 LSH);批 2(B 站)同内容图(不同 shade → 同 pHash、不同 sha256,
    模拟换站重传)→ 近邻命中 A 批指纹 → phash_near 边建立;两轮
    wire_graph_from_scan 计数、哈希库统计、边权逐项断言。"""
    pytest.importorskip("PIL")  # 未安装 Pillow 的环境按缺依赖跳过
    from netsentinel.intel.graph import EvidenceGraph
    from netsentinel.intel.phash import PhashRegistry, phash

    img_a = tmp_path / "batch1_a.png"
    img_b = tmp_path / "batch2_b.png"
    _tiny_png(img_a, shade=128)
    _tiny_png(img_b, shade=128, comment="reupload")  # 同像素、异封装
    assert img_a.read_bytes() != img_b.read_bytes()  # sha256 必然不同
    fingerprint_a = phash(str(img_a))
    fingerprint_b = phash(str(img_b))
    assert fingerprint_a == fingerprint_b  # 同像素 → pHash 距离 0

    cfg = _graph_cfg(tmp_path)
    register_base = telemetry.snapshot()["counters"].get("graph_wire.register", 0.0)

    # ---- 批 1:A 站扫描 → 本批指纹登记(闭环的"写"半边)----
    counts_1 = wire_graph_from_scan(
        _SITE_A,
        cfg,
        [_page(_SITE_A, shas=["ab" * 32], paths=[str(img_a)])],
    )
    assert counts_1["registered"] == 1  # 本批 1 条指纹登记进哈希库
    assert counts_1["phash_near_edges"] == 0  # 库内尚无其他站点 → 无近邻边
    assert pathlib.Path(f"{cfg.phash_db}.mtlsh").exists()  # 持久多表 LSH 已落盘
    with PhashRegistry(cfg.phash_db) as registry:
        assert registry.stats() == {"total": 1, "sites": 1}  # A 批指纹在库

    # ---- 批 2:B 站同图重传 → 命中批 1 指纹 → phash_near 边(闭环的"读"半边)----
    counts_2 = wire_graph_from_scan(
        _SITE_B,
        cfg,
        [_page(_SITE_B, shas=["cd" * 32], paths=[str(img_b)])],
    )
    assert counts_2["registered"] == 1
    assert counts_2["phash_near_edges"] == 1  # 跨批次命中 A 站 → 建边
    with PhashRegistry(cfg.phash_db) as registry:
        assert registry.stats() == {"total": 2, "sites": 2}  # 两批指纹齐在库

    with EvidenceGraph(cfg.graph_db) as check:
        edges = {
            (str(e["src"]), str(e["dst"]), str(e["kind"])): float(e["weight"])
            for e in check.export_json()["edges"]
        }
        phash_edges = {k: w for k, w in edges.items() if k[2] == "phash_near"}
        assert len(phash_edges) == 1
        (src, dst, kind), weight = next(iter(phash_edges.items()))
        assert {src, dst} == {f"site:{_SITE_A}", f"site:{_SITE_B}"}
        assert weight == pytest.approx(1.0)  # 1 命中 / 1 张成功哈希(双源去重)

    # telemetry:两轮登记各计 1(graph_wire.register)
    assert (
        telemetry.snapshot()["counters"].get("graph_wire.register", 0.0)
        == register_base + 2
    )


def test_wire_graph_register_step_degrades_without_lsh_kernel(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """登记步安全降级:phash_lsh 内核缺失 → 登记跳过(telemetry 记
    graph_wire.register_skipped)、近邻建边同步降级;图谱其余写入
    (site/image/template/link 边)完全不受影响。"""
    pytest.importorskip("PIL")
    caplog.set_level(logging.WARNING, logger="netsentinel.pipeline.kernel_wire")
    real_load = kernel_wire._load

    def fake_load(name: str) -> object:
        if name == "netsentinel.intel.phash_lsh":  # 多表/单表 LSH 内核未就位
            raise RuntimeError(f"模块 {name} 未就位")
        return real_load(name)

    monkeypatch.setattr(kernel_wire, "_load", fake_load)

    img = tmp_path / "plain.png"
    _tiny_png(img)
    cfg = _graph_cfg(tmp_path)
    skipped_base = telemetry.snapshot()["counters"].get(
        "graph_wire.register_skipped", 0.0
    )

    counts = wire_graph_from_scan(
        _SITE_A,
        cfg,
        [_page(_SITE_A, shas=["ab" * 32], paths=[str(img)])],
    )

    assert counts["registered"] == 0  # 登记步整体降级
    assert counts["phash_near_edges"] == 0  # 近邻建边同步降级
    assert counts["sites"] == 1 and counts["images"] == 1  # 图谱写入不受影响
    snap = telemetry.snapshot()["counters"]
    assert snap.get("graph_wire.register_skipped", 0.0) == skipped_base + 1
    assert any("phash 登记已跳过" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 6c. A214:查询步库规模阈值切换(fastpath 只走持久多表 LSH / slowpath 双源)
# ---------------------------------------------------------------------------
#: 召回等价用例的种子指纹(A 站登记,hex64;互相汉明距离远,互不近邻)
_SEED_FINGERPRINTS = [
    "0f1e2d3c4b5a6978",
    "1111222233334444",
    "5555666677778888",
    "9999aaaabbbbcccc",
]


def _seed_registry_and_mtlsh(cfg: Config, shas: list[str], fingerprints: list[str]) -> None:
    """经真实登记步(_register_phash)把指纹双写进注册库 + 持久多表 LSH。"""
    registered = kernel_wire._register_phash(
        _SITE_A, cfg, list(zip(shas, fingerprints))
    )
    assert registered == len(shas)


def test_wire_phash_near_fastpath_slowpath_recall_equivalence(tmp_path) -> None:
    """召回等价:同一数据集(登记步双写注册库 + 持久多表 LSH)下,
    slowpath(注册库重灌 ∪ 多表 LSH)与 fastpath(只走多表 LSH)建出的
    phash_near 边端点/权重完全一致;telemetry 各记一次
    graph_wire.wire.slowpath / graph_wire.wire.fastpath。纯合成指纹,零 Pillow。"""
    from netsentinel.intel.graph import EvidenceGraph
    from netsentinel.intel.phash import hamming

    # 探针:B 站 3 张图——两张分别距 A 站种子 1 bit(必命中),一张无近邻
    probe_near_1 = "0f1e2d3c4b5a6979"  # 与种子 0 …978 距 1
    probe_near_2 = "1111222233334445"  # 与种子 1 …444 距 1
    probe_far = "00ff00ff00ff00ff"
    assert hamming(probe_near_1, _SEED_FINGERPRINTS[0]) == 1
    assert hamming(probe_near_2, _SEED_FINGERPRINTS[1]) == 1
    assert min(hamming(probe_far, f) for f in _SEED_FINGERPRINTS) > 8  # 无近邻前提
    probes = [("ee" * 32, probe_near_1), ("ef" * 32, probe_near_2), ("e0" * 32, probe_far)]

    def _run(root, limit: int) -> tuple[dict, float]:
        """独立库根:种子 4 条 → B 站查询建边;返回 phash_near 边表与边权。"""
        cfg = _graph_cfg(root)
        cfg.graph_wire_phash_rebuild_limit = limit
        _seed_registry_and_mtlsh(
            cfg, [f"{i:064x}" for i in range(4)], list(_SEED_FINGERPRINTS)
        )
        graph = EvidenceGraph(cfg.graph_db)
        try:
            written = kernel_wire._wire_phash_near(graph, _SITE_B, cfg, probes)
        finally:
            graph.close()
        assert written == 1  # 只命中 A 站 → 一条边
        with EvidenceGraph(cfg.graph_db) as check:
            edges = {
                (str(e["src"]), str(e["dst"])): float(e["weight"])
                for e in check.export_json()["edges"]
                if str(e["kind"]) == "phash_near"
            }
        assert set(edges) == {
            (f"site:{_SITE_A}", f"site:{_SITE_B}")
        }  # 端点排序规范化:同一条边
        weight = next(iter(edges.values()))
        return edges, weight

    counters = telemetry.snapshot()["counters"]
    slow_before = counters.get("graph_wire.wire.slowpath", 0.0)
    fast_before = counters.get("graph_wire.wire.fastpath", 0.0)

    edges_slow, weight_slow = _run(tmp_path / "slow", limit=100)  # 4 ≤ 100 → 重灌
    edges_fast, weight_fast = _run(tmp_path / "fast", limit=3)  # 4 > 3 → 跳过重灌

    # 两路径建边结果完全一致(端点 + 权重;2 命中 / 3 张成功哈希图)
    assert edges_fast == edges_slow
    assert weight_fast == weight_slow == pytest.approx(2 / 3)
    snap = telemetry.snapshot()["counters"]
    assert snap.get("graph_wire.wire.slowpath", 0.0) == slow_before + 1
    assert snap.get("graph_wire.wire.fastpath", 0.0) == fast_before + 1


def test_wire_phash_near_threshold_boundary_and_defaults(tmp_path) -> None:
    """阈值边界:库规模 == 阈值 → slowpath(小库双源不变);== 阈值 + 1 →
    fastpath;未配置该属性时缺省 5000(小库恒 slowpath);非法配置回缺省。"""
    from netsentinel.intel.graph import EvidenceGraph

    cfg = _graph_cfg(tmp_path)
    cfg.graph_wire_phash_rebuild_limit = 5
    graph = EvidenceGraph(cfg.graph_db)
    # 无关探针(与种子互不近邻,只验证选路,不建边)
    probe = [("ab" * 32, "00ff00ff00ff00ff")]
    try:
        # 库规模 5 == 阈值 5 → 未"超过",保持 slowpath
        _seed_registry_and_mtlsh(cfg, [f"{i:064x}" for i in range(5)], [
            f"{(0x1111111111111111 * (i + 1)):016x}" for i in range(5)
        ])
        snap = telemetry.snapshot()["counters"]
        slow0 = snap.get("graph_wire.wire.slowpath", 0.0)
        fast0 = snap.get("graph_wire.wire.fastpath", 0.0)
        assert kernel_wire._wire_phash_near(graph, _SITE_B, cfg, probe) == 0
        snap = telemetry.snapshot()["counters"]
        assert snap.get("graph_wire.wire.slowpath", 0.0) == slow0 + 1
        assert snap.get("graph_wire.wire.fastpath", 0.0) == fast0

        # 再登记 1 条 → 库规模 6 > 5 → fastpath
        kernel_wire._register_phash(_SITE_A, cfg, [("f" * 64, "5555666677778888")])
        assert kernel_wire._wire_phash_near(graph, _SITE_B, cfg, probe) == 0
        snap = telemetry.snapshot()["counters"]
        assert snap.get("graph_wire.wire.slowpath", 0.0) == slow0 + 1  # 不再增加
        assert snap.get("graph_wire.wire.fastpath", 0.0) == fast0 + 1
    finally:
        graph.close()

    # 未配置属性 → 缺省 5000:小库(3 条)恒 slowpath
    cfg_default = _graph_cfg(tmp_path / "default")
    graph_default = EvidenceGraph(cfg_default.graph_db)
    try:
        _seed_registry_and_mtlsh(cfg_default, [f"{i:064x}" for i in range(3)], [
            "0f1e2d3c4b5a6978", "1111222233334444", "5555666677778888"
        ])
        assert not hasattr(cfg_default, "graph_wire_phash_rebuild_limit")
        snap = telemetry.snapshot()["counters"]
        slow1 = snap.get("graph_wire.wire.slowpath", 0.0)
        fast1 = snap.get("graph_wire.wire.fastpath", 0.0)
        kernel_wire._wire_phash_near(graph_default, _SITE_B, cfg_default, probe)
        snap = telemetry.snapshot()["counters"]
        assert snap.get("graph_wire.wire.slowpath", 0.0) == slow1 + 1
        assert snap.get("graph_wire.wire.fastpath", 0.0) == fast1
    finally:
        graph_default.close()

    # 非法配置(负数 / 不可解析)回缺省 5000 → 小库仍 slowpath
    for idx, bad in enumerate((-1, "abc", None)):
        cfg_bad = _graph_cfg(tmp_path / f"bad{idx}")
        cfg_bad.graph_wire_phash_rebuild_limit = bad
        graph_bad = EvidenceGraph(cfg_bad.graph_db)
        try:
            _seed_registry_and_mtlsh(cfg_bad, ["0" * 64], ["0f1e2d3c4b5a6978"])
            snap = telemetry.snapshot()["counters"]
            slow2 = snap.get("graph_wire.wire.slowpath", 0.0)
            kernel_wire._wire_phash_near(graph_bad, _SITE_B, cfg_bad, probe)
            assert (
                telemetry.snapshot()["counters"].get(
                    "graph_wire.wire.slowpath", 0.0
                )
                == slow2 + 1
            )
        finally:
            graph_bad.close()


# ---------------------------------------------------------------------------
# 6d. A229 多哈希生产接线:登记三哈希 + mirror 源建边(默认关 = 现状)
# ---------------------------------------------------------------------------

def test_wire_graph_multihash_flip_station_cross_batch_end_to_end(tmp_path) -> None:
    """A229 红线级验收(A244 更新):翻转站群关联端到端。批 1(A 站)登记
    图 X 三哈希(phash/mirror/pyramid 三列 + mirror 多表 LSH 独立落盘);
    批 2(B 站)传入 X 的水平翻转图——phash 距离 30 远超阈值 8(单哈希
    口径的翻转盲区,红队 A218 点名)、mirror 规范形距离 0 → mirror 索引
    命中 → **mirror_near 独立候选边**建立(图上可区分来源,与 phash_near
    分通道);哈希库 find_similar(hash_kind=...) 多哈希列可复核;telemetry
    记 mirror_hits / mirror_edge;团伙判定:镜像候选边默认不并团(红线 48
    强化),显式 gang_mirror_in_connectivity=True 才并团。"""
    pytest.importorskip("PIL")
    import hashlib

    from netsentinel.intel.graph import EvidenceGraph
    from netsentinel.intel.phash import PhashRegistry, hamming, phash
    from netsentinel.intel.phash_lsh import MultiTableLSH
    from netsentinel.vision.phash2 import mirror_hash, pyramid_hash

    img = tmp_path / "origin.png"
    img_flip = tmp_path / "flipped.png"
    _structured_png(img)
    _structured_png(img_flip, flip=True)
    # 场景前提(实测性质锁定):同构图不同字节、主哈希不可关联、镜像恒等
    assert hashlib.sha256(img.read_bytes()).hexdigest() != (
        hashlib.sha256(img_flip.read_bytes()).hexdigest()
    )
    phash_a, phash_b = phash(str(img)), phash(str(img_flip))
    assert hamming(phash_a, phash_b) > 8  # 主哈希翻转盲区(实测 30)
    mirror_a, mirror_b = mirror_hash(str(img)), mirror_hash(str(img_flip))
    assert mirror_a == mirror_b  # 镜像规范形在翻转轨道上恒等
    pyramid_a = pyramid_hash(str(img))
    assert len(pyramid_a) == 27

    cfg = _graph_cfg(tmp_path)
    cfg.graph_wire_multihash = True
    mirror_base = telemetry.snapshot()["counters"].get("graph_wire.mirror_hits", 0.0)
    mirror_edge_base = telemetry.snapshot()["counters"].get(
        "graph_wire.mirror_edge", 0.0
    )
    # 两站页面模板形状刻意不同(图片 URL 形状不同 → 模板键不同)——
    # 保证图中**唯一**关联边是 mirror_near,团伙断言才归因于镜像边本身
    page_a = _page(_SITE_A, shas=["ab" * 32], paths=[str(img)])
    page_b = _page(
        _SITE_B,
        shas=["cd" * 32],
        paths=[str(img_flip)],
        image_urls=[f"{_SITE_B}assets/pic0.jpg"],
    )
    assert kernel_wire._page_template_key(page_a) != kernel_wire._page_template_key(
        page_b
    )

    # ---- 批 1:A 站扫描 → 三哈希登记(三列 + mirror 索引独立落盘)----
    counts_1 = wire_graph_from_scan(_SITE_A, cfg, [page_a])
    assert counts_1["registered"] == 1
    assert counts_1["phash_near_edges"] == 0  # 库内尚无其他站点
    assert pathlib.Path(f"{cfg.phash_db}.mtlsh.mirror").exists()  # 镜像索引落盘
    with sqlite3.connect(cfg.phash_db) as conn:
        row = conn.execute(
            "SELECT phash, mirror_hash, pyramid_hash FROM hashes WHERE sha256 = ?",
            ("ab" * 32,),
        ).fetchone()
    assert row[0] == phash_a and row[1] == mirror_a  # 登记双列(三哈希)
    assert row[2] == pyramid_a
    # 镜像索引条目负载注明来源(kind="mirror")
    with MultiTableLSH(db_path=f"{cfg.phash_db}.mtlsh.mirror", name="mirror") as probe:
        (payload,) = probe.query(mirror_a, 0)
        assert payload == {"sha256": "ab" * 32, "site": _SITE_A, "kind": "mirror"}

    # ---- 批 2:B 站翻转图 → mirror 索引命中 → mirror_near 边(闭环)----
    counts_2 = wire_graph_from_scan(_SITE_B, cfg, [page_b])
    assert counts_2["registered"] == 1
    assert counts_2["phash_near_edges"] == 1  # 近重复家族边总数(mirror 源)
    with EvidenceGraph(cfg.graph_db) as check:
        exported = check.export_json()["edges"]
        mirror_edges = [e for e in exported if str(e["kind"]) == "mirror_near"]
        assert len(mirror_edges) == 1  # mirror 源独建边,种类可区分(A244)
        edge = mirror_edges[0]
        assert {str(edge["src"]), str(edge["dst"])} == {
            f"site:{_SITE_A}", f"site:{_SITE_B}"
        }
        assert float(edge["weight"]) == pytest.approx(1.0)  # 1 命中 / 1 张
        assert not [e for e in exported if str(e["kind"]) == "phash_near"]
        gang_edges = [
            (str(e["src"]), str(e["dst"]), str(e["kind"]), float(e["weight"]))
            for e in exported
        ]
    # 团伙判定:镜像候选边默认不直接并团(红线 48 强化,A244)
    assert resolve_gangs(gang_edges) == [[f"site:{_SITE_A}"], [f"site:{_SITE_B}"]]
    assert resolve_gangs_from_config(gang_edges, cfg) == [
        [f"site:{_SITE_A}"], [f"site:{_SITE_B}"]
    ]  # cfg 未显式开启 → 同默认
    cfg.gang_mirror_in_connectivity = True  # 显式开启才纳入并团
    assert resolve_gangs_from_config(gang_edges, cfg) == [
        sorted([f"site:{_SITE_A}", f"site:{_SITE_B}"])
    ]
    # 来源可观测:telemetry 记 mirror_hits(命中)与 mirror_edge(成边)
    snap = telemetry.snapshot()["counters"]
    assert snap.get("graph_wire.mirror_hits", 0.0) == mirror_base + 1
    assert snap.get("graph_wire.mirror_edge", 0.0) == mirror_edge_base + 1
    # 哈希库多哈希列可复核:B 的镜像哈希按 mirror 列命中两批登记记录
    with PhashRegistry(cfg.phash_db) as registry:
        hits = registry.find_similar(mirror_b, 0, hash_kind="mirror")
        assert {h["sha256"] for h in hits} == {"ab" * 32, "cd" * 32}

    # ---- 对照:开关默认关(未设置属性)= 现状单哈希,同一对图零建边 ----
    cfg_off = _graph_cfg(tmp_path / "control")
    assert not hasattr(cfg_off, "graph_wire_multihash")
    wire_graph_from_scan(
        _SITE_A, cfg_off, [_page(_SITE_A, shas=["ab" * 32], paths=[str(img)])]
    )
    counts_off = wire_graph_from_scan(
        _SITE_B, cfg_off, [_page(_SITE_B, shas=["cd" * 32], paths=[str(img_flip)])]
    )
    assert counts_off["phash_near_edges"] == 0  # 翻转盲区:单哈希口径零关联
    assert not pathlib.Path(f"{cfg_off.phash_db}.mtlsh.mirror").exists()  # 无镜像文件
    with sqlite3.connect(cfg_off.phash_db) as conn:
        mirrors = [
            str(r[0]) for r in conn.execute("SELECT mirror_hash FROM hashes")
        ]
    assert mirrors == ["", ""]  # 登记只写 phash 列(现状)


def test_multihash_switch_off_snapshot_matches_legacy(tmp_path) -> None:
    """开关默认关快照一致:graph_wire_multihash 未设置与显式 False 行为
    完全一致——同内容图(phash 距离 0)照常经 phash 源建边、登记只写
    phash 列、零 .mtlsh.mirror 文件(与 A204/A214 现状逐项一致)。"""
    pytest.importorskip("PIL")

    assert getattr(Config(), "graph_wire_multihash", False) is False  # 缺省关

    img_a = tmp_path / "same_a.png"
    img_b = tmp_path / "same_b.png"
    _tiny_png(img_a)
    _tiny_png(img_b, comment="reupload")  # 同像素、异封装(换站重传)

    def _run(root) -> dict:
        cfg = _graph_cfg(root)
        assert not hasattr(cfg, "graph_wire_multihash")
        wire_graph_from_scan(
            _SITE_A, cfg, [_page(_SITE_A, shas=["ab" * 32], paths=[str(img_a)])]
        )
        return wire_graph_from_scan(
            _SITE_B, cfg, [_page(_SITE_B, shas=["cd" * 32], paths=[str(img_b)])]
        )

    counts_unread = _run(tmp_path / "unset")
    cfg_false_root = tmp_path / "explicit"
    cfg_false = _graph_cfg(cfg_false_root)
    cfg_false.graph_wire_multihash = False  # 显式 False = 缺省
    wire_graph_from_scan(
        _SITE_A, cfg_false, [_page(_SITE_A, shas=["ab" * 32], paths=[str(img_a)])]
    )
    counts_false = wire_graph_from_scan(
        _SITE_B, cfg_false, [_page(_SITE_B, shas=["cd" * 32], paths=[str(img_b)])]
    )

    for root, counts in ((tmp_path / "unset", counts_unread), (cfg_false_root, counts_false)):
        assert counts["registered"] == 1
        assert counts["phash_near_edges"] == 1  # phash 源照常建边(现状)
        assert not pathlib.Path(f"{_graph_cfg(root).phash_db}.mtlsh.mirror").exists()
        with sqlite3.connect(_graph_cfg(root).phash_db) as conn:
            rows = conn.execute(
                "SELECT mirror_hash, pyramid_hash FROM hashes"
            ).fetchall()
        assert rows == [("", ""), ("", "")]  # 多哈希列保持空(单哈希现状)
    assert counts_false == counts_unread


def test_wire_phash_near_fastpath_coexists_with_multihash(tmp_path) -> None:
    """fastpath 与多哈希共存:注册库超重灌阈值只影响 ① 全量重灌源,
    ③ 持久 mirror 索引照常查询——翻转指纹(主哈希距种子 > 8、mirror
    距离 0)在 fastpath 下照样建 **mirror_near 边**(A244 起镜像命中
    落独立边种类);telemetry fastpath +1、mirror_edge +1。"""
    pytest.importorskip("PIL")

    from netsentinel.intel.graph import EvidenceGraph
    from netsentinel.intel.phash import hamming, phash
    from netsentinel.vision.phash2 import mirror_hash, pyramid_hash

    img = tmp_path / "x.png"
    img_flip = tmp_path / "x_flip.png"
    _structured_png(img)
    _structured_png(img_flip, flip=True)
    fp_a, fp_b = phash(str(img)), phash(str(img_flip))
    m_a, m_b = mirror_hash(str(img)), mirror_hash(str(img_flip))
    assert hamming(fp_a, fp_b) > 8  # 主哈希源无贡献(mirror 源独建边)
    pyramid_a = pyramid_hash(str(img))

    cfg = _graph_cfg(tmp_path)
    cfg.graph_wire_multihash = True
    cfg.graph_wire_phash_rebuild_limit = 1  # 库规模 2 > 1 → fastpath
    # 登记 2 条(A 站):目标图 + 一条无关指纹(只为把库规模抬过阈值)
    assert (
        kernel_wire._register_phash(
            _SITE_A,
            cfg,
            [
                ("11" * 32, fp_a, m_a, pyramid_a),
                ("22" * 32, "00ff00ff00ff00ff", "0" * 16, "0" * 27),
            ],
        )
        == 2
    )

    graph = EvidenceGraph(cfg.graph_db)
    fast_before = telemetry.snapshot()["counters"].get("graph_wire.wire.fastpath", 0.0)
    mirror_edge_before = telemetry.snapshot()["counters"].get(
        "graph_wire.mirror_edge", 0.0
    )
    try:
        written = kernel_wire._wire_phash_near(
            graph, _SITE_B, cfg, [("ee" * 32, fp_b, m_b, "")]
        )
    finally:
        graph.close()
    assert written == 1  # mirror 源命中 A 站 → 一条边(fastpath 不挡多哈希)
    assert (
        telemetry.snapshot()["counters"].get("graph_wire.wire.fastpath", 0.0)
        == fast_before + 1
    )
    assert (
        telemetry.snapshot()["counters"].get("graph_wire.mirror_edge", 0.0)
        == mirror_edge_before + 1
    )
    with EvidenceGraph(cfg.graph_db) as check:
        mirror_edges = [
            e for e in check.export_json()["edges"] if str(e["kind"]) == "mirror_near"
        ]
        assert len(mirror_edges) == 1
        assert {str(mirror_edges[0]["src"]), str(mirror_edges[0]["dst"])} == {
            f"site:{_SITE_A}", f"site:{_SITE_B}"
        }
        assert float(mirror_edges[0]["weight"]) == pytest.approx(1.0)


def test_wire_phash_near_splits_phash_and_mirror_channels(tmp_path) -> None:
    """A244 通道分离:同批双源——phash 源命中落 ``phash_near`` 边、mirror
    源命中落 ``mirror_near`` 边(同一站对可同时持有两种边,各自独立
    证据、分开计权);phash 已命中同 sha 的 mirror 命中去重(只计
    phash 一次,不再落 mirror 边);返回值 = 近重复家族边总数;
    telemetry 分别计 phash_near_edges / mirror_edge。纯合成指纹,零 Pillow。"""
    from netsentinel.intel.graph import EvidenceGraph
    from netsentinel.intel.phash import hamming

    fp_p = "0f1e2d3c4b5a6978"  # 与探针主哈希距 1(phash 源可命中)
    fp_probe = "0f1e2d3c4b5a6979"
    fp_far = "00ff00ff00ff00ff"  # 与一切探针距 > 8(只有镜像列可命中)
    m_same = "0" * 16  # 与探针镜像距 0(mirror 源可命中)
    assert hamming(fp_probe, fp_p) == 1
    assert hamming(fp_probe, fp_far) > 8

    cfg = _graph_cfg(tmp_path)
    cfg.graph_wire_multihash = True
    # 两条登记(均属 A 站):一条只可经 phash 源命中、一条只可经 mirror
    # 源命中——探针批双源并发,验证通道分离
    assert (
        kernel_wire._register_phash(
            _SITE_A,
            cfg,
            [
                ("11" * 32, fp_p, "", ""),
                ("22" * 32, fp_far, m_same, "0" * 27),
            ],
        )
        == 2
    )

    phash_base = telemetry.snapshot()["counters"].get(
        "graph_wire.phash_near_edges", 0.0
    )
    mirror_base = telemetry.snapshot()["counters"].get("graph_wire.mirror_edge", 0.0)
    graph = EvidenceGraph(cfg.graph_db)
    try:
        written = kernel_wire._wire_phash_near(
            graph,
            _SITE_B,
            cfg,
            [("ee" * 32, fp_probe, m_same, "")],  # phash 命条 1、mirror 命条 2
        )
    finally:
        graph.close()
    assert written == 2  # phash_near(A-B) + mirror_near(A-B):同站对两种边

    a_id, b_id = f"site:{_SITE_A}", f"site:{_SITE_B}"
    with EvidenceGraph(cfg.graph_db) as check:
        edges = {
            (min(str(e["src"]), str(e["dst"])), max(str(e["src"]), str(e["dst"])),
             str(e["kind"])): float(e["weight"])
            for e in check.export_json()["edges"]
        }
    assert edges.get((a_id, b_id, "phash_near")) == pytest.approx(1.0)  # 1 命中 / 1 张
    assert edges.get((a_id, b_id, "mirror_near")) == pytest.approx(1.0)
    assert len(edges) == 2  # 两条边、两种通道,互不混流
    snap = telemetry.snapshot()["counters"]
    assert snap.get("graph_wire.phash_near_edges", 0.0) == phash_base + 1
    assert snap.get("graph_wire.mirror_edge", 0.0) == mirror_base + 1

    # ---- 同 sha 双源去重:探针的 phash 与 mirror 命中**同一条**登记记录
    # → 只计 phash 一次,不落 mirror 边(A229 去重口径在分通道后保持)----
    root2 = tmp_path / "dedup"
    cfg2 = _graph_cfg(root2)
    cfg2.graph_wire_multihash = True
    assert (
        kernel_wire._register_phash(
            _SITE_A,
            cfg2,
            [("33" * 32, fp_p, m_same, "0" * 27)],  # 同记录双列均可命中
        )
        == 1
    )
    graph2 = EvidenceGraph(cfg2.graph_db)
    try:
        written2 = kernel_wire._wire_phash_near(
            graph2,
            _SITE_B,
            cfg2,
            [("ee" * 32, fp_p, m_same, "")],  # phash 距 0 且 mirror 距 0
        )
    finally:
        graph2.close()
    assert written2 == 1  # 只落 phash_near 一条(同 sha 去重)
    with EvidenceGraph(cfg2.graph_db) as check2:
        kinds2 = sorted(str(e["kind"]) for e in check2.export_json()["edges"])
    assert kinds2 == ["phash_near"]
    snap2 = telemetry.snapshot()["counters"]
    assert snap2.get("graph_wire.phash_near_edges", 0.0) == phash_base + 2
    assert snap2.get("graph_wire.mirror_edge", 0.0) == mirror_base + 1  # 未增加


def test_multihash_per_image_degrade_and_kernel_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """逐图降级计数:mirror 计算对单图失败 → 该图仍按 phash 登记(镜像列
    空)、其余图不受影响,telemetry 记 graph_wire.multihash_skipped;
    vision/phash2 内核整体缺失 → 整批退回单哈希现状(中文告警,不中断)。"""
    pytest.importorskip("PIL")
    from netsentinel.intel.phash import phash
    from netsentinel.vision import phash2

    img_good = tmp_path / "good.png"
    img_bad = tmp_path / "bad.png"
    _structured_png(img_good)
    _structured_png(img_bad, flip=True)
    real_mirror = phash2.mirror_hash

    def flaky_mirror(path):
        if str(path).endswith("bad.png"):
            raise ValueError("模拟单图 mirror 失败")
        return real_mirror(path)

    monkeypatch.setattr(phash2, "mirror_hash", flaky_mirror)
    cfg = _graph_cfg(tmp_path)
    cfg.graph_wire_multihash = True
    skipped_base = telemetry.snapshot()["counters"].get(
        "graph_wire.multihash_skipped", 0.0
    )

    counts = wire_graph_from_scan(
        _SITE_A,
        cfg,
        [_page(_SITE_A, shas=["ab" * 32, "cd" * 32],
               paths=[str(img_good), str(img_bad)])],
    )
    assert counts["registered"] == 2  # 两图都按 phash 正常登记
    assert counts["phash_near_edges"] == 0
    with sqlite3.connect(cfg.phash_db) as conn:
        rows = {
            str(sha): (str(m), str(p))
            for sha, m, p in conn.execute(
                "SELECT sha256, mirror_hash, pyramid_hash FROM hashes"
            )
        }
    good_mirror, good_pyramid = rows["ab" * 32]
    assert good_mirror == real_mirror(str(img_good))  # 好图三列齐全
    assert len(good_pyramid) == 27
    bad_mirror, bad_pyramid = rows["cd" * 32]
    assert bad_mirror == ""  # 坏图镜像列空(逐图降级)
    assert len(bad_pyramid) == 27  # pyramid 不受 mirror 失败影响
    assert (
        telemetry.snapshot()["counters"].get("graph_wire.multihash_skipped", 0.0)
        == skipped_base + 1
    )

    # ---- 内核整体缺失:整批退回单哈希(中文告警),主流程不中断 ----
    caplog.set_level(logging.WARNING, logger="netsentinel.pipeline.kernel_wire")
    real_load = kernel_wire._load

    def fake_load(name: str) -> object:
        if name == "netsentinel.vision.phash2":
            raise RuntimeError(f"模块 {name} 未就位")
        return real_load(name)

    monkeypatch.setattr(kernel_wire, "_load", fake_load)
    cfg2 = _graph_cfg(tmp_path / "no-kernel")
    cfg2.graph_wire_multihash = True
    counts2 = wire_graph_from_scan(
        _SITE_A, cfg2, [_page(_SITE_A, shas=["ab" * 32], paths=[str(img_good)])]
    )
    assert counts2["registered"] == 1  # 登记照常(phash 单哈希)
    with sqlite3.connect(cfg2.phash_db) as conn:
        (mirror_col,) = conn.execute(
            "SELECT mirror_hash FROM hashes"
        ).fetchone()
    assert mirror_col == ""  # 退回单哈希现状
    assert not pathlib.Path(f"{cfg2.phash_db}.mtlsh.mirror").exists()
    assert any("多哈希内核" in r.getMessage() for r in caplog.records)


def test_multihash_mirror_distance_config_and_fp_parts(tmp_path) -> None:
    """_mirror_distance 配置读取:缺省 12(A219 建议距)、附加属性覆盖、
    非法值(负数 / 超 64 / 不可解析)回缺省;_fp_parts 对 2/4 元组归一
    (A204/A214 旧调用兼容)。"""
    cfg = _graph_cfg(tmp_path)
    assert not hasattr(cfg, "graph_wire_mirror_distance")
    assert kernel_wire._mirror_distance(cfg) == 12  # 缺省 = A219 建议距
    cfg.graph_wire_mirror_distance = 6
    assert kernel_wire._mirror_distance(cfg) == 6
    for bad in (-1, 65, "abc", None):
        cfg.graph_wire_mirror_distance = bad
        assert kernel_wire._mirror_distance(cfg) == 12
    cfg.graph_wire_mirror_distance = 0
    assert kernel_wire._mirror_distance(cfg) == 0  # 边界值合法

    # _fp_parts:2 元组(A204/A214 旧口径)补空串、4 元组透传、sha 归一
    assert kernel_wire._fp_parts(("AB" * 32, "0f1e2d3c4b5a6978")) == (
        "ab" * 32, "0f1e2d3c4b5a6978", "", "",
    )
    assert kernel_wire._fp_parts(("cd" * 32, "1111222233334444", "0" * 16, "0" * 27)) == (
        "cd" * 32, "1111222233334444", "0" * 16, "0" * 27,
    )


# ---------------------------------------------------------------------------
# 7. A194 团伙判定增强:resolve_gangs(connectivity vs community)
# ---------------------------------------------------------------------------
def _gang_edges() -> list[tuple[str, str, str, float]]:
    """两个强团伙(两两 0.8 shared_image)+ 一条 0.2 shared_template 弱桥。"""
    clique_a = [f"site:a{i}" for i in range(6)]
    clique_b = [f"site:b{i}" for i in range(6)]
    edges: list[tuple[str, str, str, float]] = []
    for members in (clique_a, clique_b):
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                edges.append((members[i], members[j], "shared_image", 0.8))
    edges.append(("site:a0", "site:b0", "shared_template", 0.2))  # 建站工具弱桥
    return edges


def test_resolve_gangs_connectivity_default_merges_on_any_edge() -> None:
    """connectivity(默认):纯连通一条弱边即并团——与现状语义完全一致;
    空输入 → [];输出组内升序、组间按最小成员升序(确定)。"""
    groups = resolve_gangs(_gang_edges())
    assert groups == [
        sorted(
            [f"site:a{i}" for i in range(6)] + [f"site:b{i}" for i in range(6)]
        )
    ]  # 弱桥照样并:一条边即连通
    assert resolve_gangs([]) == []
    assert resolve_gangs([("", "site:x", "shared_image", 1.0)]) == [["site:x"]]
    # 未知模式中文 ValueError
    with pytest.raises(ValueError, match="团伙判定模式无效"):
        resolve_gangs(_gang_edges(), mode="fuzzy")


def test_resolve_gangs_community_keeps_weak_bridge_apart() -> None:
    """community:Louvain 加权社区检测——0.2 弱桥不再把两个强团伙误并,
    切出两团;组排序确定、两次调用同输出。"""
    edges = _gang_edges()
    groups = resolve_gangs(edges, mode="community")
    assert groups == [[f"site:a{i}" for i in range(6)], [f"site:b{i}" for i in range(6)]]
    assert resolve_gangs(edges, mode="community") == groups  # 确定性
    # 阈值过滤:下限 0.5 时 0.2 的 shared_template 边被剔除,其余 0.8 保留
    assert resolve_gangs(edges, mode="community", weight_threshold=0.5) == groups
    # 模板降权:0.2 × 0.5 = 0.1 有效权重(供更细的阈值组合)
    downweighted = resolve_gangs(
        edges, mode="community", weight_threshold=0.15, template_weight_factor=0.5
    )
    assert downweighted == groups


def test_resolve_gangs_community_threshold_isolates_weak_sites() -> None:
    """高阈值下弱边剔除:两强三角各自成伙、卫星点孤立;connectivity 对照
    一条弱边即全并(现状语义)。"""
    edges = [
        ("site:x", "site:y", "shared_image", 0.9),  # 强三角 1(0.9 × 3)
        ("site:y", "site:w", "shared_image", 0.9),
        ("site:x", "site:w", "shared_image", 0.9),
        ("site:p", "site:q", "shared_image", 0.9),  # 强三角 2(0.9 × 3)
        ("site:q", "site:r", "shared_image", 0.9),
        ("site:p", "site:r", "shared_image", 0.9),
        ("site:x", "site:p", "shared_template", 0.2),  # 建站工具弱桥
        ("site:y", "site:z", "phash_near", 0.2),  # 卫星点弱近重复
    ]
    groups = resolve_gangs(edges, mode="community", weight_threshold=0.5)
    assert groups == [
        ["site:p", "site:q", "site:r"],
        ["site:w", "site:x", "site:y"],
        ["site:z"],  # 弱边被剔除断开 → 孤立
    ]
    # connectivity 对照:同样的边全并一团(现状语义,一条弱边即并)
    assert resolve_gangs(edges) == [
        ["site:p", "site:q", "site:r", "site:w", "site:x", "site:y", "site:z"]
    ]


def test_resolve_gangs_from_config_defaults_and_switches(tmp_path) -> None:
    """配置适配层:缺省(V11 一等字段默认值)= connectivity(现状口径);
    gang_mode=community 时按字段生效;非法模式原样上抛 ValueError。"""
    edges = _gang_edges()
    cfg = _graph_cfg(tmp_path)
    assert cfg.gang_mode == "connectivity"  # V11 一等字段,缺省 = 现状口径
    merged = resolve_gangs_from_config(edges, cfg)  # 缺省:纯连通并团
    assert len(merged) == 1 and len(merged[0]) == 12

    cfg.gang_mode = "community"
    cfg.gang_weight_threshold = 0.5
    cfg.gang_template_weight_factor = 1.0
    split = resolve_gangs_from_config(edges, cfg)
    assert split == [[f"site:a{i}" for i in range(6)], [f"site:b{i}" for i in range(6)]]

    cfg.gang_mode = "nonsense"
    with pytest.raises(ValueError, match="团伙判定模式无效"):
        resolve_gangs_from_config(edges, cfg)


# ---------------------------------------------------------------------------
# 7b. A244:mirror_near 团伙判定降权 / 默认不并团(红线 48 强化)
# ---------------------------------------------------------------------------
def _mirror_gang_edges() -> list[tuple[str, str, str, float]]:
    """A 团(shared_image 0.9 强团)+ B 团(mirror_near 0.8 镜像团),
    各 4 站两两全连——B 团的成团与否完全由镜像边的有效权重决定。"""
    edges: list[tuple[str, str, str, float]] = []
    for prefix, kind, weight in (
        ("a", "shared_image", 0.9),
        ("b", "mirror_near", 0.8),
    ):
        members = [f"site:{prefix}{i}" for i in range(4)]
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                edges.append((members[i], members[j], kind, weight))
    return edges


def _group_of(groups: list[list[str]], node: str) -> list[str]:
    """取 node 所在组(测试辅助)。"""
    return next(g for g in groups if node in g)


def test_resolve_gangs_connectivity_default_excludes_mirror_near() -> None:
    """connectivity(默认)不并 mirror_near 边(红线 48 强化):仅由镜像
    候选桥相连的两团各自成组(镜像边端点仍以成员身份出现在输出里);
    显式 mirror_in_connectivity=True 才纳入并团;同为近重复家族的
    phash_near(确证近重复)默认照常并团——现状语义不变。"""
    edges = [
        ("site:a", "site:b", "shared_image", 0.8),
        ("site:c", "site:d", "shared_image", 0.8),
        ("site:a", "site:c", "mirror_near", 0.9),  # 镜像候选桥
    ]
    assert resolve_gangs(edges) == [["site:a", "site:b"], ["site:c", "site:d"]]
    # 显式开启才纳入(召回取舍由运营者拍板)
    assert resolve_gangs(edges, mirror_in_connectivity=True) == [
        ["site:a", "site:b", "site:c", "site:d"]
    ]
    # 对照:kind 换成 phash_near(确证近重复)→ 默认一条边即并团(现状)
    edges_confirmed = [
        (a, b, "phash_near" if k == "mirror_near" else k, w)
        for a, b, k, w in edges
    ]
    assert resolve_gangs(edges_confirmed) == [
        ["site:a", "site:b", "site:c", "site:d"]
    ]
    # 纯镜像边相连的两站:默认两组单点(可见但不并)
    assert resolve_gangs([("site:x", "site:y", "mirror_near", 1.0)]) == [
        ["site:x"], ["site:y"],
    ]


def test_resolve_gangs_community_mirror_downweight_math() -> None:
    """community 模式 mirror 降权数学对照:镜像边有效权重 = 原权 ×
    mirror_weight_factor,阈值按**有效权重**判定(严格小于才剔除)——
    0.8 镜像边在缺省 factor 0.5 下有效权重恰 0.4:

    - 阈值 0.5:0.4 < 0.5 → 镜像边全部剔除,B 团散为单点;
    - 阈值恰 0.4:0.4 < 0.4 不成立 → 保留,B 团照常成团(边界为 >=);
    - 阈值 0.4000001(刚过有效权重)→ 剔除,全散;
    - factor=1.0:有效权重 0.8,A / B 两团各自成团(两团并存时全局
      2m 足够,两团均成团);阈值刚过 0.8 → 又全散——证明 factor 只乘
      一次、有效权重恰为 0.8。
    """
    edges = _mirror_gang_edges()
    a_team = [f"site:a{i}" for i in range(4)]
    b_team = [f"site:b{i}" for i in range(4)]

    # factor=1.0:镜像边全额 0.8 ≥ 0.5 → A / B 两团各自成团
    assert resolve_gangs(
        edges, mode="community", weight_threshold=0.5, mirror_weight_factor=1.0
    ) == [a_team, b_team]
    # 缺省 factor 0.5:有效权重 0.8 × 0.5 = 0.4 < 0.5 → 镜像边全部剔除
    assert resolve_gangs(edges, mode="community", weight_threshold=0.5) == [
        [n] for n in sorted(a_team + b_team)
    ]
    # 边界锁定(缺省 factor):阈值 == 有效权重 0.4 → 保留(B 团成团);
    # 刚过 0.4 → 剔除(全散)。两侧唯一差异就是乘法后的有效权重。
    kept = resolve_gangs(edges, mode="community", weight_threshold=0.4)
    assert _group_of(kept, "site:b0") == b_team  # 0.4 ≥ 0.4:严格 < 才剔除
    dropped = resolve_gangs(edges, mode="community", weight_threshold=0.4000001)
    assert all(len(g) == 1 for g in dropped)  # 0.4 < 0.4000001 → 剔除
    # 边界锁定(factor=1.0):有效权重恰 0.8,刚过即剔除
    dropped_full = resolve_gangs(
        edges, mode="community", weight_threshold=0.8000001, mirror_weight_factor=1.0
    )
    assert all(len(g) == 1 for g in dropped_full)
    # 交叉验证:mirror factor 不改变其他种类的语义(shared_image 0.9 团
    # 在 factor=0.5 / 1.0 下均不受影响——上式 [a_team, b_team] 已含)


def test_resolve_gangs_from_config_mirror_switches(tmp_path) -> None:
    """配置适配层 A244 附加属性:gang_mirror_weight_factor(getattr 缺省
    0.5)与 gang_mirror_in_connectivity(缺省 False);未设置 = 缺省行为
    (connectivity 不并镜像团 / community 半权)。"""
    edges = _mirror_gang_edges()
    a_team = [f"site:a{i}" for i in range(4)]
    b_team = [f"site:b{i}" for i in range(4)]
    cfg = _graph_cfg(tmp_path)
    assert not hasattr(cfg, "gang_mirror_weight_factor")
    assert not hasattr(cfg, "gang_mirror_in_connectivity")
    # connectivity 缺省:镜像候选边不并团(A 团照常连通,B 各自单点)
    assert resolve_gangs_from_config(edges, cfg) == [a_team] + [[n] for n in b_team]
    cfg.gang_mirror_in_connectivity = True  # 显式开启才纳入(B 团成团)
    assert resolve_gangs_from_config(edges, cfg) == [a_team, b_team]

    # community 模式:缺省半权(0.8 × 0.5 = 0.4 < 0.5 剔除)
    cfg2 = _graph_cfg(tmp_path / "community")
    cfg2.gang_mode = "community"
    cfg2.gang_weight_threshold = 0.5
    assert resolve_gangs_from_config(edges, cfg2) == [
        [n] for n in sorted(a_team + b_team)
    ]
    cfg2.gang_mirror_weight_factor = 1.0  # 显式提权 → 0.8 ≥ 0.5 保留
    assert resolve_gangs_from_config(edges, cfg2) == [a_team, b_team]
