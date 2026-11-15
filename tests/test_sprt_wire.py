"""V10.37 SPRT 实时早停接线测试(orchestrator 默认流水线路径,零外呼)。

锁定七组语义:

1. **强 clean 站早停**:门禁放行 + use_sprt → 分块评分喂检验器,统计
   结论已足即停爬停送(判 clean);判定与全量送审一致(CLEAN/零复核);
   n_used = 检验器吸收点(2 张),非整块;
2. **强 nsfw 站早停**:判 nsfw 即停,verdict/needs_review 与全量一致,
   调用数严格少于全量送审;
3. **站级预算上界**:vlm_max_images_per_site 首次成为真实送审上界
   (超出截下、超满停爬,stop_reason=budget);
4. **门禁未过降级**:use_sprt=True 但无报告/批准(或批准指纹过期)→
   响亮降级(全量送审旧行为 + intel inactive 标记 + sprt.gate.refused
   计数),绝不阻塞扫描;
5. **注入/串行路径不启用**:classifier 注入或 overlap 关 → inactive 标记;
6. **错误分不喂**:带内 error 分(哨兵 0.0)不是证据,n_used=0;
7. **use_sprt 缺省关**:intel 零 sprt 键,行为逐字节旧行为(用例 1 A 态)。
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterator

import pytest

import netsentinel.pipeline.orchestrator as orchestrator
from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, ImageScore, PageSample, Verdict
from netsentinel.vision import calibration as calib
from netsentinel.vision import kernel_gate as kg
from netsentinel.vision.classifier_base import NsfwClassifier, register_classifier

SITE = "http://localhost/sprt"


# ---------------------------------------------------------------------------
# 注册替身(默认解析路径 → 流水线生效;计费标记缺省 False → 预检零打扰;
# 均不覆写 classify_batch → 走基类逐张路径,与生产形态一致)
# ---------------------------------------------------------------------------
class SprtClean(NsfwClassifier):
    """校准良好的强 clean 替身:恒 0.02(真实 GLM 对合成图实测值域)。"""

    name = "sprt_clean"
    calls = 0

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg

    def classify(self, img: ImageEvidence) -> ImageScore:
        SprtClean.calls += 1
        return ImageScore(image=img, model=self.name, nsfw_prob=0.02)


class SprtHot(NsfwClassifier):
    """强 nsfw 替身:恒 0.99(Wald LLR 6 张过 A 界,停审点=6)。"""

    name = "sprt_hot"
    calls = 0

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg

    def classify(self, img: ImageEvidence) -> ImageScore:
        SprtHot.calls += 1
        return ImageScore(image=img, model=self.name, nsfw_prob=0.99)


class SprtMid(NsfwClassifier):
    """不确定替身:恒 0.73(LLR≈-0.005/张,永不判停——预算上界的探针)。"""

    name = "sprt_mid"
    calls = 0

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg

    def classify(self, img: ImageEvidence) -> ImageScore:
        SprtMid.calls += 1
        return ImageScore(image=img, model=self.name, nsfw_prob=0.73)


class SprtErr(NsfwClassifier):
    """全错替身:带内 error 分(A63 降级语义;3 张 < 熔断阈值 5,不中止)。"""

    name = "sprt_err"
    calls = 0

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg

    def classify(self, img: ImageEvidence) -> ImageScore:
        SprtErr.calls += 1
        return ImageScore(image=img, model=self.name, nsfw_prob=0.0,
                          scores={"error": "HTTP 429 余额不足(模拟)"})


for _n, _c in (("sprt_clean", SprtClean), ("sprt_hot", SprtHot),
               ("sprt_mid", SprtMid), ("sprt_err", SprtErr)):
    register_classifier(_n, _c)


@pytest.fixture(autouse=True)
def _fresh_counters():
    SprtClean.calls = SprtHot.calls = SprtMid.calls = SprtErr.calls = 0
    yield


@pytest.fixture
def cfg(tmp_path) -> Config:
    c = Config(
        data_dir=str(tmp_path / "data"),
        evidence_dir=str(tmp_path / "evidence"),
        db_path=str(tmp_path / "data" / "review_queue.db"),
        audit_path=str(tmp_path / "data" / "audit.jsonl"),
        log_path=str(tmp_path / "data" / "logs" / "netsentinel.log"),
        vlm_cache_db=str(tmp_path / "vlm.db"),
    )
    c.use_fusion = False
    c.ensemble_members = []
    return c


def _grant_gate(c: Config, model: str = "") -> None:
    """在 cfg.data_dir 写一份通过的机读报告 + 批准(三道锁全过)。"""
    m = model or c.classifier
    gate = {
        "criteria": [
            {"criterion": "C1 样本量", "pass": True,
             "detail": "人标正样本 25/20,负样本 30/20"},
            {"criterion": "C2 校准 ECE", "pass": True, "detail": "ECE=0.03(≤ 0.1 为过)"},
        ],
        "passed": True,
        "verdict": "门禁通过",
    }
    report = {"labeled_total": 55, "scored_ok": 55, "coverage": 1.0,
              "score_errors": 0,
              "by_source": {"human": {"n": 55}, "seed_or_site": {"n": 0}}}
    calib.save_report_json(c.data_dir, m, report, gate)
    kg.write_approval(c.data_dir, "sprt", m, "测试批准")


def _patch_defaults(monkeypatch: pytest.MonkeyPatch, pages_builder, pulled: list[int]):
    """替换 discover/capture_many/入列/审计为受控替身(家规同 test_scan_pipeline)。"""
    monkeypatch.setattr(
        orchestrator, "_default_discover", lambda url, c, fetch_page=None: [url]
    )
    monkeypatch.setattr(
        orchestrator, "_default_build_bundle",
        lambda report, c: type("B", (), {"zip_path": "fake/b.zip"})(),
    )

    class _Queue:
        added: list[Any] = []

        def add(self, report, zip_path, **kw):
            _Queue.added.append(report)
            return 1

        def close(self):
            pass

    monkeypatch.setattr(orchestrator, "_default_queue", lambda db_path: _Queue())

    class _Audit:
        def log_event(self, event, **fields):
            pass

    monkeypatch.setattr(orchestrator, "_default_audit_logger", lambda p: _Audit())

    def capture_many(urls, c) -> Iterator[PageSample]:
        for page in pages_builder():
            pulled.append(1)
            yield page

    monkeypatch.setattr(orchestrator, "_default_capture_many", capture_many)


def _ev(path: str, *, width: int = 300, height: int = 300, sha: str = "") -> ImageEvidence:
    return ImageEvidence(path=path, url=f"http://img/{path}", source_page=SITE,
                         sha256=sha, width=width, height=height)


def _page(name: str, n: int, *, sha_base: str, delay_before: float = 0.0) -> PageSample:
    if delay_before:
        time.sleep(delay_before)  # 模拟慢页:给在飞分块留完成窗口(判停可见)
    return PageSample(
        url=f"{SITE}/{name}", screenshot_path="",
        image_evidences=[_ev(f"{name}_{j}.jpg", sha=sha_base + f"{j:02d}" * 30)
                         for j in range(n)],
    )


def _counter(name: str) -> float:
    return telemetry.snapshot()["counters"].get(name, 0.0)


def _clone(c: Config, **kw) -> Config:
    """同目录新 Config(两态 A/B 互不串扰;data_dir 共享门禁文件)。"""
    d = Config(
        data_dir=c.data_dir, evidence_dir=c.evidence_dir, db_path=c.db_path,
        audit_path=c.audit_path, log_path=c.log_path, vlm_cache_db=c.vlm_cache_db,
        **kw,
    )
    d.use_fusion = False
    d.ensemble_members = []
    return d


# ---------------------------------------------------------------------------
# 1. 强 clean 站:早停 + 判定一致 + use_sprt 缺省关零 intel 键
# ---------------------------------------------------------------------------
def _clean_pages():
    yield _page("p1", 3, sha_base="a", delay_before=0.3)
    yield _page("p2", 3, sha_base="b", delay_before=0.3)
    yield _page("p3", 3, sha_base="c", delay_before=0.3)


def test_clean_site_early_stop_matches_full_send_verdict(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    # A 态:SPRT 关(全量送审;use_sprt 缺省 False = 逐字节旧行为)
    cfg_off = _clone(cfg, classifier="sprt_clean")
    _patch_defaults(monkeypatch, _clean_pages, pulled_off := [])
    base_refused = _counter("sprt.gate.refused")
    r_off = orchestrator.run_scan(SITE, cfg_off)
    assert SprtClean.calls == 9            # 全量送审
    assert "sprt" not in r_off.intel       # use_sprt 关:零 intel 键(旧行为)
    assert r_off.verdict is Verdict.CLEAN and r_off.needs_review is False

    # B 态:SPRT 开(门禁三锁全过)
    cfg_on = _clone(cfg, classifier="sprt_clean", use_sprt=True)
    _grant_gate(cfg_on, "sprt_clean")
    _patch_defaults(monkeypatch, _clean_pages, pulled_on := [])
    SprtClean.calls = 0
    r_on = orchestrator.run_scan(SITE, cfg_on)

    # 早停:p1 分块在 p2 爬取窗内完成 → 喂检验器 → 判 clean → 停爬
    assert len(pulled_on) == 2             # 第 3 页从未拉取
    assert 3 <= SprtClean.calls < 9        # 送审严格少于全量
    # 判定与全量送审逐项一致(红线:早停只省调用,不改结论)
    assert r_on.verdict is r_off.verdict
    assert r_on.needs_review == r_off.needs_review
    assert r_on.agg_nsw_prob == pytest.approx(r_off.agg_nsw_prob)
    # intel 记账:active 真数;n_used=检验器吸收点(2),非整块 3
    intel = r_on.intel["sprt"]
    assert intel["active"] is True and intel["allowed"] is True
    assert intel["verdict"] == "clean"
    assert intel["n_used"] == 2
    assert intel["stopped_early"] is True
    assert intel["stop_reason"] == "sprt:clean"
    assert intel["images_sent"] == SprtClean.calls   # 口径=实际送审
    assert _counter("sprt.gate.refused") == base_refused  # 本路径零拒开计数


# ---------------------------------------------------------------------------
# 2. 强 nsfw 站:早停 + 判定一致
# ---------------------------------------------------------------------------
def _hot_pages():
    yield _page("p1", 7, sha_base="a", delay_before=0.3)   # 7×0.99:6 张过 A 界
    yield _page("p2", 3, sha_base="b", delay_before=0.3)
    yield _page("p3", 3, sha_base="c", delay_before=0.3)


def test_hot_site_early_stop_keeps_nsfw_verdict(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    cfg_off = _clone(cfg, classifier="sprt_hot")
    _patch_defaults(monkeypatch, _hot_pages, pulled_off := [])
    r_off = orchestrator.run_scan(SITE, cfg_off)
    assert SprtHot.calls == 13
    assert r_off.verdict is Verdict.NSFW and r_off.needs_review is True

    cfg_on = _clone(cfg, classifier="sprt_hot", use_sprt=True)
    _grant_gate(cfg_on, "sprt_hot")
    _patch_defaults(monkeypatch, _hot_pages, pulled_on := [])
    SprtHot.calls = 0
    r_on = orchestrator.run_scan(SITE, cfg_on)

    assert len(pulled_on) == 2             # 第 3 页从未拉取
    assert SprtHot.calls < 13              # 送审严格少于全量
    assert r_on.verdict is Verdict.NSFW    # 判定与全量一致
    assert r_on.needs_review is True
    intel = r_on.intel["sprt"]
    assert intel["verdict"] == "nsfw"
    assert intel["n_used"] == 6            # Wald 界:0.99 序列 6 张过 A
    assert intel["stop_reason"] == "sprt:nsfw"
    assert intel["images_sent"] == SprtHot.calls


# ---------------------------------------------------------------------------
# 3. 站级预算上界(不确定站:SPRT 永不判停,预算截断接管)
# ---------------------------------------------------------------------------
def _mid_pages():
    yield _page("p1", 3, sha_base="a", delay_before=0.05)
    yield _page("p2", 3, sha_base="b", delay_before=0.05)
    yield _page("p3", 3, sha_base="c", delay_before=0.05)


def test_budget_cap_limits_sends(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    cfg_on = _clone(cfg, classifier="sprt_mid", use_sprt=True,
                    vlm_max_images_per_site=4)
    _grant_gate(cfg_on, "sprt_mid")
    _patch_defaults(monkeypatch, _mid_pages, pulled := [])
    r = orchestrator.run_scan(SITE, cfg_on)

    assert SprtMid.calls == 4              # 上界=4:p1 全送 3,p2 送 1 截 2
    assert len(pulled) == 2                # 预算满 → 停爬
    intel = r.intel["sprt"]
    assert intel["budget_cap"] == 4
    assert intel["images_sent"] == 4
    assert intel["budget_saved"] == 2      # p2 被截下的 2 张
    assert intel["stopped_early"] is True
    assert intel["stop_reason"] == "budget"
    assert intel["verdict"] == "continue"  # 0.73 永不判停,预算收口
    assert r.verdict is Verdict.SUSPECT    # agg=0.73 ≥ 0.5 复核线(判定机器未动)


# ---------------------------------------------------------------------------
# 4. 门禁未过:响亮降级(全量送审旧行为,绝不阻塞)
# ---------------------------------------------------------------------------
def test_gate_refused_degrades_to_full_send(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    cfg_on = _clone(cfg, classifier="sprt_clean", use_sprt=True)  # 无报告/无批准
    _patch_defaults(monkeypatch, _clean_pages, pulled := [])
    base = _counter("sprt.gate.refused")
    r = orchestrator.run_scan(SITE, cfg_on)

    assert len(pulled) == 3                # 全量爬取
    assert SprtClean.calls == 9            # 全量送审(旧行为)
    assert r.verdict is Verdict.CLEAN
    intel = r.intel["sprt"]
    assert intel["active"] is False and intel["allowed"] is False
    assert "校准报告缺失" in intel["reason"]
    assert _counter("sprt.gate.refused") == base + 1


def test_gate_digest_stale_degrades(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """报告重测后旧批准失效(指纹锁):运行时同样响亮降级。"""
    cfg_on = _clone(cfg, classifier="sprt_clean", use_sprt=True)
    _grant_gate(cfg_on, "sprt_clean")
    # 篡改批准 digest(模拟"报告已重测,批准还是旧的")
    p = kg.approval_path(cfg_on.data_dir)
    obj = json.loads(p.read_text(encoding="utf-8"))
    obj["digest"] = "0" * 64
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    _patch_defaults(monkeypatch, _clean_pages, pulled := [])
    r = orchestrator.run_scan(SITE, cfg_on)
    assert SprtClean.calls == 9            # 降级=全量送审
    assert r.intel["sprt"]["active"] is False
    assert "指纹与当前报告不一致" in r.intel["sprt"]["reason"]


# ---------------------------------------------------------------------------
# 5. 注入 / 串行路径:不启用,留 inactive 标记
# ---------------------------------------------------------------------------
def test_injected_classifier_disables_sprt(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    cfg_on = _clone(cfg, classifier="sprt_clean", use_sprt=True)
    _grant_gate(cfg_on, "sprt_clean")
    _patch_defaults(monkeypatch, _clean_pages, pulled := [])
    r = orchestrator.run_scan(SITE, cfg_on, classifier=SprtClean(cfg_on))
    assert SprtClean.calls == 9            # 串行全量(注入=调用方接管)
    assert r.intel["sprt"]["active"] is False
    assert "注入" in r.intel["sprt"]["reason"]


def test_serial_path_disables_sprt(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    cfg_on = _clone(cfg, classifier="sprt_clean", use_sprt=True,
                    scan_pipeline_overlap=False)
    _grant_gate(cfg_on, "sprt_clean")
    _patch_defaults(monkeypatch, _clean_pages, pulled := [])
    r = orchestrator.run_scan(SITE, cfg_on)
    assert SprtClean.calls == 9
    assert r.intel["sprt"]["active"] is False
    assert "串行路径" in r.intel["sprt"]["reason"]


# ---------------------------------------------------------------------------
# 6. 错误分不喂检验器(失败不是证据)
# ---------------------------------------------------------------------------
def test_error_scores_never_feed_sprt(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    def one_page():
        yield _page("p1", 3, sha_base="a")   # 3 张 < 熔断阈值 5:不中止

    cfg_on = _clone(cfg, classifier="sprt_err", use_sprt=True)
    _grant_gate(cfg_on, "sprt_err")
    _patch_defaults(monkeypatch, one_page, _pulled := [])
    r = orchestrator.run_scan(SITE, cfg_on)

    assert SprtErr.calls == 3
    intel = r.intel["sprt"]
    assert intel["n_used"] == 0             # 全部带内 error:0 张入检验器
    assert intel["verdict"] == "continue"
    assert r.verdict is Verdict.CLEAN       # 判定机器未动(agg=0)
    assert r.needs_review is True           # V10.24 诚信升级照常生效
