"""P0-1 VLM 送审闸门测试(全部离线、零外呼、零兄弟模块硬依赖)。

锁定四组语义:

1. **判定口径镜像(机器对账)**:对 (宽,高,min_px) 全网格,断言
   "闸门放行 ⟺ :func:`netsentinel.decision.verdict.assess` 候选"。
   vision 层不得 import decision(import-linter 分层),口径一致性
   不靠共享导入而靠本测试锁死——任何人改动 assess 的候选过滤或
   闸门谓词而未同步另一方,这里立即失败。
2. **三层拦截各自语义**:已测得小图拦下 / 同内容(sha256,缺省回退
   path)去重 / 未测得 fail-open 放行(显式开启才拦);关闭开关
   原清单原样返回、记账 None(回旧行为)。
3. **run_scan 集成**:送审清单只含闸门放行项;判定结果与闸门关闭时
   完全一致;零拦截主线 intel 键集与审计载荷零侵入;全小图站点
   零送审调用、判 CLEAN、不炸。
4. **嗅探升级回归**:WebP(VP8X/VP8/VP8L)与 AVIF/HEIF 宽高解析
   (修复这两类图"宽高恒 0 → 从不参与判定"的漏报洞)+ 存量
   PNG/GIF/JPEG 口径不变 + 畸形输入安全返回 (0, 0)。
"""
from __future__ import annotations

import struct
from typing import Any

import pytest

import netsentinel.pipeline.orchestrator as orchestrator
from netsentinel import telemetry
from netsentinel.contracts import (
    Config,
    ImageEvidence,
    ImageScore,
    PageSample,
    Verdict,
)
from netsentinel.decision.verdict import assess
from netsentinel.vision.send_gate import SendGateStats, apply_send_gate

SITE = "http://localhost/a"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
def _ev(
    path: str,
    *,
    width: int = 300,
    height: int = 300,
    sha: str = "",
    url: str | None = None,
) -> ImageEvidence:
    return ImageEvidence(
        path=path,
        url=url or f"http://img/{path}",
        source_page=SITE,
        sha256=sha,
        width=width,
        height=height,
    )


class FakeCapture:
    """按预设证据清单返回单页采样(路径与尺寸完全由测试控制)。"""

    def __init__(self, evidences: list[ImageEvidence]) -> None:
        self.evidences = evidences

    def __call__(self, url: str, cfg: Config, *, fetch_page: Any = None) -> PageSample:
        return PageSample(url=url, screenshot_path="", image_evidences=list(self.evidences))


class CountingClassifier:
    """替身分类器:记录收到的每张送审图,返回固定低分。"""

    def __init__(self, prob: float = 0.03) -> None:
        self.name = "counting"
        self.prob = prob
        self.seen: list[str] = []

    def classify_batch(self, imgs: list[ImageEvidence]) -> list[ImageScore]:
        self.seen.extend(i.path for i in imgs)
        return [
            ImageScore(image=i, model=self.name, nsfw_prob=self.prob, scores={})
            for i in imgs
        ]


@pytest.fixture
def cfg(tmp_path) -> Config:
    cfg = Config(
        data_dir=str(tmp_path / "data"),
        evidence_dir=str(tmp_path / "evidence"),
        db_path=str(tmp_path / "data" / "review_queue.db"),
        audit_path=str(tmp_path / "data" / "audit.jsonl"),
        log_path=str(tmp_path / "data" / "logs" / "netsentinel.log"),
    )
    cfg.use_fusion = False
    cfg.ensemble_members = []
    return cfg


def _patch_scan_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """替换 run_scan 的 discover / build_bundle / queue / audit 默认工厂。"""
    monkeypatch.setattr(
        orchestrator, "_default_discover", lambda url, c, fetch_page=None: [url]
    )
    monkeypatch.setattr(
        orchestrator,
        "_default_build_bundle",
        lambda report, c: type("B", (), {"zip_path": "fake/bundle.zip"})(),
    )

    class _Queue:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        def add(self, report: Any, zip_path: str, **kwargs: Any) -> int:
            self.calls.append((report, zip_path, kwargs))
            return 1

        def close(self) -> None:  # pragma: no cover - 空实现
            pass

    queue = _Queue()
    monkeypatch.setattr(orchestrator, "_default_queue", lambda db_path: queue)

    class _Audit:
        def __init__(self) -> None:
            self.events: list[dict] = []

        def log_event(self, event: str, **fields: Any) -> None:
            self.events.append({"event": event, **fields})

    audit = _Audit()
    monkeypatch.setattr(orchestrator, "_default_audit_logger", lambda p: audit)
    return queue, audit


def _counter(name: str) -> float:
    return telemetry.snapshot()["counters"].get(name, 0.0)


# ---------------------------------------------------------------------------
# 1. 机器对账:闸门谓词 ⟺ assess 候选过滤(全网格)
# ---------------------------------------------------------------------------
#: 网格覆盖:未测得(全 0/部分 0)/ 远小于 / 边界两侧 / 一边大一边小 / 常见分辨率
_GRID = [
    (0, 0), (0, 100), (100, 0), (0, 500), (500, 0), (1, 1), (100, 100),
    (199, 199), (199, 200), (200, 199), (200, 200), (201, 199), (199, 201),
    (1280, 720), (72, 4000), (1920, 1080),
]
_MIN_PX_VARIANTS = [200, 120]


@pytest.mark.parametrize("min_px", _MIN_PX_VARIANTS)
@pytest.mark.parametrize("w,h", _GRID)
def test_gate_parity_with_assess(w: int, h: int, min_px: int) -> None:
    """闸门放行 ⟺ assess 把该图计为候选(判定翻档 ⟺ 送审),双模式锁定。

    构造:待测图 (w,h) 高分 0.97 + 哨兵大图 4096×4096 低分 0.02。
    assess 翻离 CLEAN 当且仅当待测图是候选(agg 被抬到复核线以上)。

    - 已测得,或未测得但一边已达标(如 0×500——assess 今日就采信):
      **缺省与 drop_unmeasured 两态都必须放行** ⟺ 候选;
    - 完全未测得且非候选(如 0×0):assess 永不采信,缺省 fail-open
      放行(安全方向多花不丢证),drop 模式拦下——两态都不动判定。
    """
    local = Config()
    local.min_image_px = min_px

    probe = ImageScore(image=_ev(f"probe_{w}x{h}.jpg", width=w, height=h),
                       model="m", nsfw_prob=0.97)
    sentinel = ImageScore(image=_ev("sentinel.jpg", width=4096, height=4096),
                          model="m", nsfw_prob=0.02)
    report = assess(SITE, [], [probe, sentinel], local)
    assess_uses_it = report.verdict is not Verdict.CLEAN  # 候选 → agg=0.97 → 翻档

    send_default, _ = apply_send_gate([probe.image, sentinel.image], local)
    sends_default = probe.image in send_default

    local_drop = Config()
    local_drop.min_image_px = min_px
    local_drop.vlm_send_gate_drop_unmeasured = True
    send_drop, _ = apply_send_gate([probe.image, sentinel.image], local_drop)
    sends_drop = probe.image in send_drop

    if assess_uses_it:
        # 判定参与者:任何模式都不得拦(含"部分未测得但一边达标")
        assert sends_default, (
            f"闸门拦下了判定参与者:(w={w}, h={h}, min_px={min_px})"
        )
        assert sends_drop, (
            f"drop 模式拦下了判定参与者:(w={w}, h={h}, min_px={min_px})"
        )
    else:
        # 非候选:assess 永不采信
        if w > 0 and h > 0:
            # 已测得小图:两态都拦(纯浪费外呼,拦截零判定影响)
            assert not sends_default, (
                f"缺省模式应拦下已测得小图:(w={w}, h={h}, min_px={min_px})"
            )
            assert not sends_drop
        else:
            # 未测得非候选:缺省 fail-open 放行(多花不丢证),drop 模式拦下
            assert sends_default, (
                f"缺省模式必须 fail-open 放行:(w={w}, h={h}, min_px={min_px})"
            )
            assert not sends_drop, (
                f"drop 模式应拦下非候选:(w={w}, h={h}, min_px={min_px})"
            )


def test_unmeasured_drop_mode_never_flips_verdict_either_way() -> None:
    """未测得图高分也不影响判定(assess 非候选);拦下与放行两态判定一致。"""
    local = Config()
    unmeasured = ImageScore(image=_ev("zero.jpg", width=0, height=0),
                            model="m", nsfw_prob=0.99)
    report = assess(SITE, [], [unmeasured], local)
    assert report.verdict is Verdict.CLEAN  # 0x0 永不参与判定
    assert report.agg_nsw_prob == 0.0


# ---------------------------------------------------------------------------
# 2. 三层拦截单元语义
# ---------------------------------------------------------------------------
def test_gate_drops_measured_small_keeps_large() -> None:
    small = _ev("s.jpg", width=199, height=199, sha="s" * 64)
    large = _ev("l.jpg", width=200, height=200, sha="l" * 64)
    wide = _ev("w.jpg", width=150, height=1000, sha="w" * 64)
    send, stats = apply_send_gate([small, large, wide], Config())
    assert [i.path for i in send] == ["l.jpg", "w.jpg"]
    assert stats is not None
    assert stats.collected == 3 and stats.sent == 2
    assert stats.skipped_small == 1 and stats.saved_calls == 1


def test_gate_dedups_by_sha256_then_path() -> None:
    a1 = _ev("a1.jpg", sha="a" * 64)
    a2 = _ev("a2.jpg", sha="a" * 64)  # 同内容异路径 → 去重
    b1 = _ev("b1.jpg", sha="")  # sha 缺失 → 回退 path
    b2 = _ev("b1.jpg", sha="")  # 同路径 → 去重
    send, stats = apply_send_gate([a1, a2, b1, b2], Config())
    assert [i.path for i in send] == ["a1.jpg", "b1.jpg"]
    assert stats is not None
    assert stats.skipped_duplicate == 2


def test_gate_unmeasured_failopen_by_default_drop_on_optin() -> None:
    zero = _ev("z.jpg", width=0, height=0, sha="z" * 64)

    send, stats = apply_send_gate([zero], Config())  # 缺省 fail-open
    assert [i.path for i in send] == ["z.jpg"]
    assert stats is not None and stats.kept_unmeasured == 1

    cfg = Config()
    cfg.vlm_send_gate_drop_unmeasured = True  # 显式开启才拦
    send2, stats2 = apply_send_gate([zero], cfg)
    assert send2 == []
    assert stats2 is not None and stats2.skipped_unmeasured == 1


def test_gate_disabled_returns_original_and_none() -> None:
    imgs = [_ev("a.jpg", width=10, height=10, sha="a" * 64)]
    cfg = Config()
    cfg.vlm_send_gate = False
    send, stats = apply_send_gate(imgs, cfg)
    assert send is imgs  # 原清单原样(逐字节旧行为)
    assert stats is None


def test_gate_stats_as_dict_and_telemetry() -> None:
    base_dup = _counter("sendgate.skipped.duplicate")
    base_small = _counter("sendgate.skipped.small")
    base_pass = _counter("sendgate.passed")
    small = _ev("s.jpg", width=1, height=1, sha="s" * 64)
    dup = _ev("d.jpg", width=300, height=300, sha="d" * 64)
    dup2 = _ev("d2.jpg", width=300, height=300, sha="d" * 64)
    send, stats = apply_send_gate([small, dup, dup2], Config())
    d = stats.as_dict() if stats else {}
    assert d["collected"] == 3 and d["sent"] == 1
    assert d["skipped_small"] == 1 and d["skipped_duplicate"] == 1
    assert d["saved_calls"] == 2
    assert _counter("sendgate.skipped.duplicate") == base_dup + 1
    assert _counter("sendgate.skipped.small") == base_small + 1
    assert _counter("sendgate.passed") == base_pass + 1


def test_gate_empty_input() -> None:
    send, stats = apply_send_gate([], Config())
    assert send == []
    assert stats is not None and stats.collected == 0 and stats.sent == 0


# ---------------------------------------------------------------------------
# 3. run_scan 集成
# ---------------------------------------------------------------------------
def _mixed_evidences() -> list[ImageEvidence]:
    return [
        _ev("big1.jpg", width=300, height=300, sha="a" * 64),
        _ev("big2.jpg", width=400, height=200, sha="b" * 64),
        _ev("small.jpg", width=100, height=100, sha="c" * 64),
        _ev("dup_big1.jpg", width=300, height=300, sha="a" * 64),  # 同内容重复
        _ev("unknown.jpg", width=0, height=0, sha="e" * 64),  # 未测得(fail-open)
    ]


def test_run_scan_gate_filters_and_preserves_verdict(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """闸门开(缺省):只送审 3 张(2 大图 + 1 未测得);判定与闸门关完全一致。"""
    _patch_scan_side_effects(monkeypatch)
    evidences = _mixed_evidences()

    clf_on = CountingClassifier(prob=0.03)
    report_on = orchestrator.run_scan(
        SITE, cfg, capture=FakeCapture(evidences), classifier=clf_on
    )
    assert clf_on.seen == ["big1.jpg", "big2.jpg", "unknown.jpg"]  # 小图+重复被拦
    assert report_on.verdict is Verdict.CLEAN
    gate = report_on.intel.get("sendgate")
    assert gate is not None
    assert gate["collected"] == 5 and gate["sent"] == 3
    assert gate["skipped_small"] == 1 and gate["skipped_duplicate"] == 1

    cfg_off = Config(
        data_dir=cfg.data_dir, evidence_dir=cfg.evidence_dir,
        db_path=cfg.db_path, audit_path=cfg.audit_path, log_path=cfg.log_path,
    )
    cfg_off.use_fusion = False
    cfg_off.ensemble_members = []
    cfg_off.vlm_send_gate = False
    clf_off = CountingClassifier(prob=0.03)
    report_off = orchestrator.run_scan(
        SITE, cfg_off, capture=FakeCapture(evidences), classifier=clf_off
    )
    assert clf_off.seen == [e.path for e in evidences]  # 关闸:全量送审(旧行为)
    assert report_off.verdict is report_on.verdict  # 判定不变
    assert report_off.agg_nsw_prob == report_on.agg_nsw_prob
    assert "sendgate" not in report_off.intel


def test_run_scan_zero_skip_mainline_intel_untouched(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """零拦截主线:intel 键集与旧版完全一致(sendgate 不出现)。"""
    _patch_scan_side_effects(monkeypatch)
    evidences = [_ev(f"ok{i}.jpg", width=300, height=300, sha=chr(ord("a") + i) * 64)
                 for i in range(3)]
    clf = CountingClassifier(prob=0.02)
    report = orchestrator.run_scan(
        SITE, cfg, capture=FakeCapture(evidences), classifier=clf
    )
    assert report.verdict is Verdict.CLEAN
    assert report.intel == {}  # 零拦截:零 intel 侵入(旧版逐字节一致)


def test_run_scan_all_small_site_zero_calls_clean(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """全小图站点:零送审调用、判 CLEAN、不抛(与旧行为判定一致,省全部调用)。"""
    _patch_scan_side_effects(monkeypatch)
    evidences = [_ev(f"tiny{i}.jpg", width=64, height=64, sha=chr(ord("a") + i) * 64)
                 for i in range(3)]
    clf = CountingClassifier(prob=0.97)  # 即便高分也轮不到送审
    report = orchestrator.run_scan(
        SITE, cfg, capture=FakeCapture(evidences), classifier=clf
    )
    assert clf.seen == []
    assert report.verdict is Verdict.CLEAN  # 无候选 → agg=0(与旧版同款结论)
    gate = report.intel["sendgate"]
    assert gate["sent"] == 0 and gate["skipped_small"] == 3


# ---------------------------------------------------------------------------
# 4. 嗅探升级:WebP / AVIF(HEIF)
# ---------------------------------------------------------------------------
def _box(box_type: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body) + 8) + box_type + body


def _write(tmp_path, name: str, data: bytes) -> str:
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def _webp_vp8x(w: int, h: int) -> bytes:
    payload = (b"\x00" * 4 + (w - 1).to_bytes(3, "little")
               + (h - 1).to_bytes(3, "little"))
    riff = b"WEBP" + b"VP8X" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(riff)) + riff


def _webp_vp8(w: int, h: int) -> bytes:
    payload = b"\x00\x00\x00\x9d\x01\x2a" + struct.pack("<HH", w, h)
    riff = b"WEBP" + b"VP8 " + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(riff)) + riff


def _webp_vp8l(w: int, h: int) -> bytes:
    bits = (w - 1) | ((h - 1) << 14)
    payload = b"\x2f" + bits.to_bytes(4, "little")
    riff = b"WEBP" + b"VP8L" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(riff)) + riff


def _avif(w: int, h: int, brand: bytes = b"avif") -> bytes:
    ftyp = _box(b"ftyp", brand + struct.pack(">I", 0) + brand + b"MIAF")
    meta = _box(b"meta", struct.pack(">I", 0) + _box(b"iprp", _box(b"ipco",
        _box(b"ispe", struct.pack(">I", 0) + struct.pack(">II", w, h)))))
    return ftyp + meta + _box(b"mdat", b"\x00" * 16)


@pytest.mark.parametrize("stream,args", [
    ("vp8x", (1920, 1080)), ("vp8x", (64, 64)),
    ("vp8", (800, 600)), ("vp8l", (640, 480)),
])
def test_sniff_webp(tmp_path, stream: str, args: tuple[int, int]) -> None:
    builders = {"vp8x": _webp_vp8x, "vp8": _webp_vp8, "vp8l": _webp_vp8l}
    path = _write(tmp_path, f"{stream}.webp", builders[stream](*args))
    import pathlib
    assert orchestrator._sniff_image_size(pathlib.Path(path)) == args


@pytest.mark.parametrize("brand,args", [
    (b"avif", (4000, 3000)), (b"avif", (100, 80)), (b"heic", (4032, 3024)),
])
def test_sniff_avif_heif(tmp_path, brand: bytes, args: tuple[int, int]) -> None:
    path = _write(tmp_path, "img.avif", _avif(*args, brand=brand))
    import pathlib
    assert orchestrator._sniff_image_size(pathlib.Path(path)) == args


@pytest.mark.parametrize("data", [
    b"RIFF\x00\x00\x00\x00WEBPjunk",                    # WebP 头损坏
    _avif(500, 500)[:40],                                # AVIF 截断
    _avif(1, 1, brand=b"isom"),                          # 非 HEIF 品牌 mp4
    b"hello world this is not an image at all!!",        # 非图片
])
def test_sniff_malformed_returns_zero(tmp_path, data: bytes) -> None:
    import pathlib
    path = _write(tmp_path, "bad.bin", data)
    assert orchestrator._sniff_image_size(pathlib.Path(path)) == (0, 0)


def test_sniff_png_regression(tmp_path) -> None:
    """存量 PNG 口径不变(升级不伤老格式)。"""
    import pathlib
    png = (b"\x89PNG\r\n\x1a\n" + _box(b"IHDR",
            struct.pack(">IIBBBBB", 300, 200, 8, 0, 0, 0, 0)) + b"\x00" * 8)
    path = _write(tmp_path, "ok.png", png)
    assert orchestrator._sniff_image_size(pathlib.Path(path)) == (300, 200)


def test_webp_evidence_now_participates_in_decision(tmp_path) -> None:
    """修复漏报洞:WebP 证据经 _enrich_images 获得宽高 → 恢复判定参与权。"""
    import pathlib
    path = pathlib.Path(_write(tmp_path, "photo.webp", _webp_vp8x(1920, 1080)))
    ev = ImageEvidence(path=str(path), url="http://img/photo.webp",
                       source_page=SITE, sha256="", width=0, height=0)
    orchestrator._enrich_images([ev])
    assert (ev.width, ev.height) == (1920, 1080)
    # 宽高就位后,闸门按普通大图放行(此前 0x0 = 送审却从不参与判定)
    send, stats = apply_send_gate([ev], Config())
    assert [i.path for i in send] == [str(path)]
    assert stats is not None and stats.sent == 1


def test_send_gate_stats_dataclass_defaults() -> None:
    stats = SendGateStats(collected=7, sent=7)
    assert stats.saved_calls == 0
    assert stats.as_dict()["saved_calls"] == 0
