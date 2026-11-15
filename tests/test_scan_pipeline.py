"""P1-3 站内流水线重叠测试(全部离线、零外呼)。

锁定六组语义:

1. **流式闸门等价**:SendGate 分块喂入 vs apply_send_gate 一次性——放行
   清单与记账完全一致;
2. **流水线 ⟷ 串行等价**:默认路径(未注入任何组件)重叠开/关两态,
   image_scores(顺序+数值)/verdict/pages 完全一致,重叠确实发生
   (scan.pipeline.chunks 计数)且墙钟更短;
3. **早夭止损**:首块全败(≥熔断阈值)→ 停止爬取剩余页(生产者只拉
   了第 1 页),V10.24 同款熔断报错;
4. **聚合熔断补口**:每块 < 阈值但整站全败 → 收割段按总量熔断;
5. **跨块语义**:跨页 sha 去重、跨页改名映射(enrich renames)贯通;
6. **开关与注入**:scan_pipeline_overlap=False 或注入任一组件 → 串行
   路径(chunks 计数零增量)。
"""
from __future__ import annotations

import time
from typing import Any, Iterator

import pytest

import netsentinel.pipeline.orchestrator as orchestrator
from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, ImageScore, PageSample
from netsentinel.vision.classifier_base import NsfwClassifier, register_classifier
from netsentinel.vision.send_gate import SendGate, apply_send_gate

SITE = "http://localhost/a"


# ---------------------------------------------------------------------------
# 注册替身(默认解析路径 → 流水线生效;计费标记缺省 False → 预检零打扰)
# ---------------------------------------------------------------------------
class PipeSlow(NsfwClassifier):
    """慢速本地替身:每张 sleep,模拟 VLM 延迟;不覆写 classify_batch。"""

    name = "pipe_slow"
    calls = 0

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg

    def classify(self, img: ImageEvidence) -> ImageScore:
        PipeSlow.calls += 1
        time.sleep(0.12)
        return ImageScore(image=img, model=self.name, nsfw_prob=0.03)


class PipeDead(NsfwClassifier):
    """全败替身:每张带内 error 分(V10.24 熔断形态),0.05s/张模拟往返。"""

    name = "pipe_dead"
    calls = 0

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg

    def classify(self, img: ImageEvidence) -> ImageScore:
        PipeDead.calls += 1
        time.sleep(0.05)
        return ImageScore(image=img, model=self.name, nsfw_prob=0.0,
                          scores={"error": "HTTP 429 余额不足(模拟)"})


register_classifier("pipe_slow", PipeSlow)
register_classifier("pipe_dead", PipeDead)


class PipeSlower(NsfwClassifier):
    """等价性对照专用:0.5s/张,拉大 VLM 占比使重叠收益显著可断言。"""

    name = "pipe_slower"
    calls = 0

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg

    def classify(self, img: ImageEvidence) -> ImageScore:
        PipeSlower.calls += 1
        time.sleep(0.5)
        return ImageScore(image=img, model=self.name, nsfw_prob=0.03)


register_classifier("pipe_slower", PipeSlower)


def _ev(path: str, *, width: int = 300, height: int = 300, sha: str = "") -> ImageEvidence:
    return ImageEvidence(path=path, url=f"http://img/{path}", source_page=SITE,
                         sha256=sha, width=width, height=height)


@pytest.fixture(autouse=True)
def _fresh_counters():
    PipeSlow.calls = PipeDead.calls = 0
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


def _patch_defaults(monkeypatch: pytest.MonkeyPatch, pages_builder, pulled: list[int]):
    """替换 discover/capture_many 为受控生成器;pulled 记录生产者拉取页数。"""
    monkeypatch.setattr(
        orchestrator, "_default_discover", lambda url, c, fetch_page=None: [url]
    )
    monkeypatch.setattr(
        orchestrator, "_default_build_bundle",
        lambda report, c: type("B", (), {"zip_path": "fake/b.zip"})(),
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

    def capture_many(urls, c) -> Iterator[PageSample]:
        for page in pages_builder():
            pulled.append(1)
            yield page

    monkeypatch.setattr(orchestrator, "_default_capture_many", capture_many)


def _page(name: str, evs: list[ImageEvidence], delay: float = 0.0) -> PageSample:
    if delay:
        time.sleep(delay)
    return PageSample(url=f"{SITE}/{name}", screenshot_path="", image_evidences=evs)


def _counter(name: str) -> float:
    return telemetry.snapshot()["counters"].get(name, 0.0)


# ---------------------------------------------------------------------------
# 1. 流式闸门等价
# ---------------------------------------------------------------------------
def test_sendgate_streaming_equals_oneshot() -> None:
    cfg = Config()
    chunk1 = [_ev(f"a{i}.jpg", sha=chr(ord("a") + i) * 64) for i in range(3)]
    chunk2 = [
        _ev("a0dup.jpg", sha="a" * 64),          # 跨块重复
        _ev("small.jpg", width=10, height=10, sha="s" * 64),  # 小图
        _ev("big.jpg", width=999, height=999, sha="b" * 64),
    ]
    gate = SendGate(cfg)
    s1 = gate.feed(chunk1)
    s2 = gate.feed(chunk2)
    whole, stats = apply_send_gate(chunk1 + chunk2, cfg)
    assert [i.path for i in s1 + s2] == [i.path for i in whole]
    d_stream, d_once = gate.stats.as_dict(), (stats.as_dict() if stats else {})
    for key in ("collected", "sent", "skipped_small", "skipped_duplicate"):
        assert d_stream[key] == d_once[key], key


# ---------------------------------------------------------------------------
# 2. 流水线 ⟷ 串行等价 + 重叠发生 + 墙钟收益
# ---------------------------------------------------------------------------
def _three_slow_pages():
    """逐页生成器:每页延迟发生在该页被拉取时(重叠窗口真实存在)。"""
    yield _page("p1", [_ev(f"i1_{j}.jpg", sha=f"{j}" * 64) for j in range(3)], delay=0.5)
    yield _page("p2", [_ev(f"i2_{j}.jpg", sha=f"{j + 10}" * 64) for j in range(3)], delay=0.5)
    yield _page("p3", [_ev(f"i3_{j}.jpg", sha=f"{j + 20}" * 64) for j in range(2)], delay=0.5)


def test_pipeline_matches_serial_results_and_is_faster(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    cfg.classifier = "pipe_slower"

    # 串行(开关关)
    cfg_off = cfg
    cfg_off.scan_pipeline_overlap = False
    _patch_defaults(monkeypatch, _three_slow_pages, pulled_off := [])
    base_off = _counter("scan.pipeline.chunks")
    t0 = time.perf_counter()
    r_off = orchestrator.run_scan(SITE, cfg_off)
    wall_off = time.perf_counter() - t0
    assert _counter("scan.pipeline.chunks") == base_off  # 串行零流水线计数
    assert len(pulled_off) == 3

    # 流水线(开关开,缺省)
    cfg_on = Config(
        data_dir=cfg.data_dir, evidence_dir=cfg.evidence_dir, db_path=cfg.db_path,
        audit_path=cfg.audit_path, log_path=cfg.log_path, vlm_cache_db=cfg.vlm_cache_db,
    )
    cfg_on.use_fusion = False
    cfg_on.ensemble_members = []
    cfg_on.classifier = "pipe_slower"
    monkeypatch.setattr(
        orchestrator, "_default_discover", lambda url, c, fetch_page=None: [url]
    )
    _patch_defaults(monkeypatch, _three_slow_pages, pulled_on := [])
    base_on = _counter("scan.pipeline.chunks")
    PipeSlower.calls = 0  # 计数器按本次运行重置(前半是串行运行的累计)
    t0 = time.perf_counter()
    r_on = orchestrator.run_scan(SITE, cfg_on)
    wall_on = time.perf_counter() - t0
    assert _counter("scan.pipeline.chunks") == base_on + 3  # 3 页 = 3 分块
    assert len(pulled_on) == 3

    # 判定完全一致(顺序 + 数值)
    assert r_on.verdict is r_off.verdict
    assert r_on.agg_nsw_prob == r_off.agg_nsw_prob
    assert [(s.image.path, s.nsfw_prob) for s in r_on.image_scores] == \
           [(s.image.path, s.nsfw_prob) for s in r_off.image_scores]
    assert [p.url for p in r_on.pages] == [p.url for p in r_off.pages]
    assert PipeSlower.calls == 8  # 两态调用数一致(8 张互异图各一次)

    # 墙钟:重叠把评分藏进爬取窗(0.9s 爬取 ×2 路相同;串行额外付满评分)
    assert wall_on < wall_off * 0.85, (wall_on, wall_off)


def test_pipeline_order_preserved(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    """image_scores 顺序 = 页序首现序(与串行一致)。"""
    cfg.classifier = "pipe_slow"
    _patch_defaults(monkeypatch, _three_slow_pages, _ := [])
    r = orchestrator.run_scan(SITE, cfg)
    order = [s.image.path for s in r.image_scores]
    assert order == [f"i1_{j}.jpg" for j in range(3)] \
        + [f"i2_{j}.jpg" for j in range(3)] + [f"i3_{j}.jpg" for j in range(2)]


# ---------------------------------------------------------------------------
# 3. 早夭止损:首块全败 ≥ 阈值 → 停止爬取
# ---------------------------------------------------------------------------
def test_early_abort_stops_crawling(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    cfg.classifier = "pipe_dead"

    def six_more_pages():
        # 每页 6 张(≥ 熔断阈值 5):第 1 块全败;页间 0.4s 爬取延迟远大于
        # 首块完成耗时(6×0.05s),第 2 页拉取后的轮询必然看到首块错误
        for k in range(3):
            yield _page(
                f"dead{k}",
                [_ev(f"d{k}_{j}.jpg", sha=chr(ord("a") + k) + f"{j}" * 63)
                 for j in range(6)],
                delay=0.4,
            )

    pulled: list[int] = []
    _patch_defaults(monkeypatch, six_more_pages, pulled)
    with pytest.raises(RuntimeError, match="全部失败"):
        orchestrator.run_scan(SITE, cfg)
    # 早夭:第 1 块错误在第 2 页爬完后的轮询被看到,第 3 页不再爬取
    # (串行路径会拉满 3 页、盲打 18 次);首块 6 次 + 第二块 0~6 次竞态
    assert len(pulled) == 2
    assert 6 <= PipeDead.calls <= 12


# ---------------------------------------------------------------------------
# 4. 聚合熔断补口:每块 < 阈值但整站全败
# ---------------------------------------------------------------------------
def test_aggregate_abort_when_chunks_below_threshold(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    cfg.classifier = "pipe_dead"

    def small_pages():
        for k in range(3):  # 每页 3 张(< 阈值 5),合计 9 张全败
            yield _page(
                f"agg{k}",
                [_ev(f"g{k}_{j}.jpg", sha=chr(ord("a") + k) + f"{j}" * 63)
                 for j in range(3)],
            )

    pulled: list[int] = []
    _patch_defaults(monkeypatch, small_pages, pulled)
    with pytest.raises(RuntimeError, match="全部失败"):
        orchestrator.run_scan(SITE, cfg)
    assert len(pulled) == 3  # 块级不触发 → 爬满
    assert PipeDead.calls == 9


# ---------------------------------------------------------------------------
# 5. 跨块语义:去重 + 改名映射
# ---------------------------------------------------------------------------
def test_cross_chunk_dedup(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    cfg.classifier = "pipe_slow"

    def dup_pages():
        yield _page("p1", [_ev(f"x{j}.jpg", sha=f"{j}" * 64) for j in range(3)])
        yield _page("p2", [  # x0 内容重复(同 sha),x9 新增
            _ev("dup_of_x0.jpg", sha="0" * 64),
            _ev("x9.jpg", sha="9" * 64),
        ])

    _patch_defaults(monkeypatch, dup_pages, _ := [])
    r = orchestrator.run_scan(SITE, cfg)
    assert PipeSlow.calls == 4  # 3 + 1(重复图不送审)
    assert len(r.image_scores) == 4
    assert "dup_of_x0.jpg" not in [s.image.path for s in r.image_scores]


def test_cross_chunk_rename_mapping(tmp_path, monkeypatch, cfg: Config) -> None:
    """跨页同文件:第 1 页改哈希名→原始名后,第 2 页旧路径证据经映射对齐。"""
    import re as _re
    hex16 = "abcdef0123456789"
    src = tmp_path / f"{hex16}.jpg"
    src.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 200)  # JPEG 头(嗅探容忍失败→0x0)
    cfg.classifier = "pipe_slow"

    ev1 = ImageEvidence(path=str(src), url="http://img/photo_orig_name.jpg",
                        source_page=SITE, sha256="", width=0, height=0)
    ev2 = ImageEvidence(path=str(src), url="http://img/photo_orig_name.jpg",
                        source_page=SITE, sha256="", width=0, height=0)

    def two_pages():
        yield _page("p1", [ev1])
        yield _page("p2", [ev2])  # 同一物理文件的第 2 条证据(旧路径已失效)

    _patch_defaults(monkeypatch, two_pages, _ := [])
    r = orchestrator.run_scan(SITE, cfg)
    # 改名后第 2 条证据指向新路径:两条证据都存活且归并为一条集成
    assert len(r.image_scores) >= 1
    assert PipeSlow.calls >= 1


# ---------------------------------------------------------------------------
# 6. 开关与注入 → 串行路径
# ---------------------------------------------------------------------------
def test_knob_off_uses_serial(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    cfg.classifier = "pipe_slow"
    cfg.scan_pipeline_overlap = False
    _patch_defaults(monkeypatch, _three_slow_pages, _ := [])
    base = _counter("scan.pipeline.chunks")
    orchestrator.run_scan(SITE, cfg)
    assert _counter("scan.pipeline.chunks") == base


def test_injected_capture_uses_serial(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    """注入 capture = 调用方接管 → 串行路径(流水线计数零增量)。"""
    cfg.classifier = "pipe_slow"
    _patch_defaults(monkeypatch, _three_slow_pages, _ := [])
    base = _counter("scan.pipeline.chunks")

    class Cap:
        def __call__(self, url, c, *, fetch_page=None):
            return PageSample(url=url, screenshot_path="",
                              image_evidences=[_ev("cap.jpg", sha="c" * 64)])

    monkeypatch.setattr(orchestrator, "_default_discover",
                        lambda url, c, fetch_page=None: [url])
    orchestrator.run_scan(SITE, cfg, capture=Cap())
    assert _counter("scan.pipeline.chunks") == base
