"""P1-4 同站近重复拦下测试(全部离线、零外呼)。

两层验证:

- **受控指纹层**(monkeypatch ``SendGate._phash_of`` 返回定距哈希):
  阈值边界(≤4 拦 / >4 放)、阈值 0 仅合并同指纹、fail-open(指纹
  失败放行)、关闭开关零指纹调用、sha 去重优先于近重复;
- **真实图像层**(PIL 生成,skipif 缺 PIL):base 图与其重编码/缩放
  变体(字节不同、phash 距 ≤4)只送代表图,无关噪声图照常送审;
  run_scan 默认链路(流式闸门)跨页近重复生效。

实测锚点(2026-10-14):tests/fixtures/demo_site 五张夹具最小两两距离 6
(阈值 4 下零误伤);data/evidence 45 张真实语料近重复对 7(15%)。
"""
from __future__ import annotations

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, ImageScore, PageSample
from netsentinel.vision.classifier_base import NsfwClassifier, register_classifier
from netsentinel.vision.send_gate import SendGate, apply_send_gate

SITE = "http://localhost/a"


def _ev(path: str, *, width: int = 300, height: int = 300, sha: str = "") -> ImageEvidence:
    return ImageEvidence(path=path, url=f"http://img/{path}", source_page=SITE,
                         sha256=sha, width=width, height=height)


# ---------------------------------------------------------------------------
# 受控指纹层:定距哈希(hex64);d(h1,h2)=2、d(h1,h3)=5
# ---------------------------------------------------------------------------
H_BASE = "f" * 16
H_D2 = "f" * 15 + "c"  # 末位 f(1111) vs c(1100):汉明距 2
H_D5 = "f" * 14 + "c" + "0"  # 末两位 f vs c(2bit)+ f vs 0(4bit)= 距 6(>4 放行)


def _counter(name: str) -> float:
    return telemetry.snapshot()["counters"].get(name, 0.0)


def test_neardup_threshold_default_drops_close_keeps_far() -> None:
    gate = SendGate(Config())
    assert gate.neardup is True and gate.neardup_threshold == 4
    stub = {0: H_BASE, 1: H_D2, 2: H_D5}
    gate._phash_of = lambda img: stub[int(img.path.split("_")[1].split(".")[0])]

    out = gate.feed([_ev(f"i_{i}.jpg", sha=chr(ord("a") + i) * 64) for i in range(3)])
    assert [i.path for i in out] == ["i_0.jpg", "i_2.jpg"]  # 距 2 拦 / 距 6 放
    assert gate.stats.skipped_neardup == 1
    assert gate.stats.sent == 2 and gate.stats.saved_calls == 1


def test_neardup_threshold_zero_merges_only_identical() -> None:
    cfg = Config()
    cfg.vlm_neardup_threshold_bits = 0
    gate = SendGate(cfg)
    stub = {0: H_BASE, 1: H_D2, 2: H_BASE}
    gate._phash_of = lambda img: stub[int(img.path.split("_")[1].split(".")[0])]
    out = gate.feed([_ev(f"i_{i}.jpg", sha=chr(ord("a") + i) * 64) for i in range(3)])
    assert [i.path for i in out] == ["i_0.jpg", "i_1.jpg"]  # 距 2 不拦;同指纹(距 0)拦
    assert gate.stats.skipped_neardup == 1


def test_neardup_failopen_on_hash_failure() -> None:
    gate = SendGate(Config())
    gate._phash_of = lambda img: None  # 模块缺席/文件损坏
    base = _counter("sendgate.neardup.hash_failed")
    out = gate.feed([_ev("a.jpg", sha="a" * 64), _ev("b.jpg", sha="b" * 64)])
    assert [i.path for i in out] == ["a.jpg", "b.jpg"]  # fail-open 全放
    assert gate.stats.skipped_neardup == 0


def test_neardup_disabled_never_hashes() -> None:
    cfg = Config()
    cfg.vlm_neardup_dedup = False
    gate = SendGate(cfg)

    def _boom(img):  # pragma: no cover - 不应被调用
        raise AssertionError("关闭态不应计算指纹")

    gate._phash_of = _boom
    out = gate.feed([_ev("a.jpg", sha="a" * 64), _ev("b.jpg", sha="b" * 64)])
    assert len(out) == 2


def test_sha_duplicate_takes_precedence_over_neardup() -> None:
    """同 sha 先按字节重复拦(计数归 duplicate,不算近重复)。"""
    gate = SendGate(Config())
    gate._phash_of = lambda img: H_BASE
    out = gate.feed([
        _ev("a1.jpg", sha="a" * 64),
        _ev("a2.jpg", sha="a" * 64),  # 字节重复优先
        _ev("b.jpg", sha="b" * 64),   # 同指纹近重复
    ])
    assert [i.path for i in out] == ["a1.jpg"]
    assert gate.stats.skipped_duplicate == 1
    assert gate.stats.skipped_neardup == 1


def test_neardup_streaming_across_feeds() -> None:
    """跨页(分块喂入)近重复:第 2 页的同指纹变体被拦。"""
    gate = SendGate(Config())
    gate._phash_of = lambda img: H_BASE if "v" not in img.path else H_D2
    out1 = gate.feed([_ev("p1_base.jpg", sha="a" * 64)])
    out2 = gate.feed([_ev("p2_variant.jpg", sha="b" * 64)])
    assert [i.path for i in out1 + out2] == ["p1_base.jpg"]
    assert gate.stats.skipped_neardup == 1


def test_neardup_unmeasured_kept_also_checked() -> None:
    """fail-open 放行的未测得图同样过近重复闸(拟送审即比对)。"""
    gate = SendGate(Config())
    stub = {"big.jpg": H_BASE, "u1.jpg": "0" * 16, "u2.jpg": "0" * 15 + "2"}
    gate._phash_of = lambda img: stub[img.path]
    out = gate.feed([
        _ev("big.jpg", width=300, height=300, sha="a" * 64),
        _ev("u1.jpg", width=0, height=0, sha="b" * 64),   # 未测得,与 big 远 → 放行并注册
        _ev("u2.jpg", width=0, height=0, sha="c" * 64),   # 未测得,与 u1 距 1 → 拦
    ])
    assert [i.path for i in out] == ["big.jpg", "u1.jpg"]
    assert gate.stats.kept_unmeasured == 2
    assert gate.stats.skipped_neardup == 1


def test_stats_dict_carries_neardup() -> None:
    _, stats = apply_send_gate([_ev("a.jpg", sha="a" * 64)], Config())
    assert "skipped_neardup" in (stats.as_dict() if stats else {})


# ---------------------------------------------------------------------------
# 真实图像层(PIL 生成;base/重编码/缩放变体 vs 无关噪声)
# ---------------------------------------------------------------------------
PILImage = pytest.importorskip("PIL.Image", reason="PIL 缺席时 phash 退化,真实图像层跳过")
from netsentinel.intel.phash import hamming, phash  # noqa: E402


def _base_image(seed: int = 7, size: int = 256):
    """结构化渐变+几何块(与噪声图距离远,与自身重编码距离近)。"""
    img = PILImage.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            px[x, y] = ((x * 2 + seed) % 256, (y * 2) % 256, (x + y + seed) % 256)
    for i in range(32, size - 32, 64):  # 几何块增强结构性
        for y in range(i, i + 32):
            for x in range(i, i + 32):
                px[x, y] = (255 - x % 256, y % 256, 200)
    return img


def _write(tmp_path, name: str, img) -> str:
    p = tmp_path / name
    img.save(str(p))
    return str(p)


@pytest.fixture()
def image_set(tmp_path):
    base = _base_image()
    p_base = _write(tmp_path, "base.png", base)
    # 重编码变体:JPEG q85 再存 PNG(字节不同 → sha 不同;phash 距 ≤4)
    jpg = tmp_path / "tmp.jpg"
    base.save(str(jpg), quality=85)
    p_reenc = _write(tmp_path, "reenc.png", PILImage.open(str(jpg)).convert("RGB"))
    # 缩放变体:90% 重采样
    p_resized = _write(tmp_path, "resized.png", base.resize((230, 230)))
    # 无关图:不同种子(远距离)
    p_noise = _write(tmp_path, "other.png", _base_image(seed=123))
    return p_base, p_reenc, p_resized, p_noise


def test_real_image_variants_collapse(tmp_path, image_set) -> None:
    p_base, p_reenc, p_resized, p_noise = image_set
    # 前提自证:变体距离 ≤4,无关图距离远
    h = [phash(p) for p in image_set]
    assert hamming(h[0], h[1]) <= 4, hamming(h[0], h[1])
    assert hamming(h[0], h[2]) <= 4, hamming(h[0], h[2])
    assert hamming(h[0], h[3]) >= 10, hamming(h[0], h[3])

    import hashlib
    def sha(p): return hashlib.sha256(open(p, "rb").read()).hexdigest()
    gate = SendGate(Config())
    evs = [
        ImageEvidence(path=p, url=f"http://img/{i}", source_page=SITE, sha256=sha(p),
                      width=256, height=256)
        for i, p in enumerate(image_set)
    ]
    out = gate.feed(evs)
    assert [i.path for i in out] == [p_base, p_noise]  # 变体拦下,代表+无关放行
    assert gate.stats.skipped_neardup == 2


# ---------------------------------------------------------------------------
# run_scan 集成(默认链路:流式闸门跨页近重复)
# ---------------------------------------------------------------------------
class NearCount(NsfwClassifier):
    name = "near_count"
    calls = 0

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg

    def classify(self, img: ImageEvidence) -> ImageScore:
        NearCount.calls += 1
        return ImageScore(image=img, model=self.name, nsfw_prob=0.03)


register_classifier("near_count", NearCount)


def test_run_scan_neardup_across_pages(
    monkeypatch: pytest.MonkeyPatch, tmp_path, cfg_factory, image_set
) -> None:
    import netsentinel.pipeline.orchestrator as orchestrator

    p_base, p_reenc, p_resized, p_noise = image_set
    import hashlib
    def sha(p): return hashlib.sha256(open(p, "rb").read()).hexdigest()
    evs_page1 = [ImageEvidence(path=p_base, url="http://img/base", source_page=SITE,
                               sha256=sha(p_base), width=256, height=256)]
    evs_page2 = [
        ImageEvidence(path=p_reenc, url="http://img/reenc", source_page=SITE,
                      sha256=sha(p_reenc), width=256, height=256),
        ImageEvidence(path=p_noise, url="http://img/noise", source_page=SITE,
                      sha256=sha(p_noise), width=256, height=256),
    ]

    def capture_many(urls, c):
        yield PageSample(url=f"{SITE}/p1", screenshot_path="", image_evidences=evs_page1)
        yield PageSample(url=f"{SITE}/p2", screenshot_path="", image_evidences=evs_page2)

    monkeypatch.setattr(orchestrator, "_default_discover",
                        lambda url, c, fetch_page=None: [url])
    monkeypatch.setattr(orchestrator, "_default_capture_many", capture_many)
    monkeypatch.setattr(
        orchestrator, "_default_build_bundle",
        lambda report, c: type("B", (), {"zip_path": "fake/b.zip"})(),
    )

    class _Q:
        def add(self, report, zip_path, **kw): return 1
        def close(self): pass
    monkeypatch.setattr(orchestrator, "_default_queue", lambda db_path: _Q())

    class _A:
        def log_event(self, event, **fields): pass
    monkeypatch.setattr(orchestrator, "_default_audit_logger", lambda p: _A())

    cfg = cfg_factory
    cfg.classifier = "near_count"
    base = NearCount.calls
    report = orchestrator.run_scan(SITE, cfg)
    assert NearCount.calls - base == 2  # base + noise(重编码变体近重复拦下)
    gate = report.intel.get("sendgate", {})
    assert gate.get("skipped_neardup") == 1


@pytest.fixture
def cfg_factory(tmp_path) -> Config:
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
