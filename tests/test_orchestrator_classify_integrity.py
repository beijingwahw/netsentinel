"""V10.24 识别失败诚信缺口清偿 · 编排器单测(席位1,全部离线)。

锁定三条新行为的开关两侧语义(背景:V10.23 实弹事故——GLM 密钥中途
余额耗尽,HTTP 429 code 1113,评分失败图片被静默跳过,站点以零分集合
产出 verdict=clean):

1. **失败感知**:`_collect_member_scores` 在成员收货口按"参与评分图片数
   vs 成功入集成图片数"记账图片级失败(静默缺评 = 入批数 − 返回数;
   带内错误分 = ``scores["error"]`` 非空的 0 分降级条目),零失败主线
   逐字节不变(计数 / note / 警示行全不出现);
2. **诚信升级**(默认开,``cfg.escalate_on_classify_error`` 可关):部分
   失败 → needs_review 强制 True(verdict 原档不动)+ 入列 note
   ``[识别失败 N 张,判定置信受损]`` + clean 时 CLI/日志警示行 + 遥测
   ``scan.classify.failed_images``;开关关闭回旧行为;
3. **快速中止**(``cfg.classify_abort_threshold``,缺省 5):首笔完成的
   成员批量评分全部失败且失败数 ≥ 阈值 → 中文 RuntimeError(含
   ``vlmctl ping`` 诊断指引),不再调用后续成员(计数断言);4 张不中止、
   5 张中止(阈值边界)。

测试约定:全部替身注入(fake capture / fake 分类器 / fake 队列),
零外呼、零兄弟模块硬依赖,与 tests/test_orchestrator.py 同口径。
"""
from __future__ import annotations

import os
from typing import Any

import pytest

import netsentinel.pipeline.orchestrator as orchestrator
from netsentinel import telemetry
from netsentinel.contracts import (
    Config,
    EvidenceBundle,
    ImageEvidence,
    ImageScore,
    PageSample,
    SiteReport,
    Verdict,
)

SITE = "http://localhost/a"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class FakeCapture:
    """页面采样 fake:返回含 N 张指定命名前缀图片的单页 PageSample。"""

    def __init__(self, count: int = 4, prefix: str = "normal") -> None:
        self.count = count
        self.prefix = prefix

    def __call__(self, url: str, cfg: Config, *, fetch_page: Any = None) -> PageSample:
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


class GoodClassifier:
    """正常成员替身:每张图都给固定低分(全成功,零失败)。"""

    def __init__(self, name: str = "good", prob: float = 0.02) -> None:
        self.name = name
        self.prob = prob
        self.batch_calls = 0

    def classify_batch(self, imgs: list[ImageEvidence]) -> list[ImageScore]:
        self.batch_calls += 1
        return [
            ImageScore(image=i, model=self.name, nsfw_prob=self.prob, scores={})
            for i in imgs
        ]


class FlakyClassifier:
    """失败成员替身:前 fail_count 张模拟识别失败,其余正常。

    - mode="error":失败图返回 ``nsfw_prob=0.0 + scores={"error": ...}``
      带内降级条目(A63/GLM/parallel_classify 语义:进了集成但是 0 分);
    - mode="drop":失败图直接不返回(V10.23 事故形态:静默缺评,
      入批 N 张只返回 N-fail 张)。
    """

    def __init__(
        self, fail_count: int, mode: str = "error", name: str = "vlm", prob: float = 0.02
    ) -> None:
        self.name = name
        self.fail_count = fail_count
        self.mode = mode
        self.prob = prob
        self.batch_calls = 0

    def classify_batch(self, imgs: list[ImageEvidence]) -> list[ImageScore]:
        self.batch_calls += 1
        out: list[ImageScore] = []
        for idx, img in enumerate(imgs):
            if idx < self.fail_count:  # 前 fail_count 张识别失败
                if self.mode == "drop":
                    continue
                out.append(
                    ImageScore(
                        image=img,
                        model=self.name,
                        nsfw_prob=0.0,
                        scores={"error": "GLM 识别失败:HTTP 429;code=1113 余额耗尽(模拟)"},
                    )
                )
                continue
            out.append(
                ImageScore(image=img, model=self.name, nsfw_prob=self.prob, scores={})
            )
        return out


class RecordingQueue:
    """复核队列 fake:记录 add 调用(含关键字实参),锁定入列口径。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def add(self, report: SiteReport, evidence_zip: str = "", **kwargs: Any) -> int:
        self.calls.append(
            {"report": report, "zip": evidence_zip, "kwargs": dict(kwargs)}
        )
        return 77

    def close(self) -> None:  # 对齐真队列:连接用毕即关(getattr 容错亦支持)
        pass


class FakeBundleFactory:
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


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log_event(self, event: str, **fields: Any) -> None:
        self.events.append({"event": event, **fields})


class FakeAuditFactory:
    def __init__(self) -> None:
        self.audits: list[FakeAudit] = []

    def __call__(self, path: str) -> FakeAudit:
        audit = FakeAudit()
        self.audits.append(audit)
        return audit


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def cfg(tmp_path) -> Config:
    cfg = Config(
        data_dir=str(tmp_path / "data"),
        evidence_dir=str(tmp_path / "evidence"),
        db_path=str(tmp_path / "data" / "review_queue.db"),
        audit_path=str(tmp_path / "data" / "audit.jsonl"),
        log_path=str(tmp_path / "data" / "logs" / "netsentinel.log"),
    )
    # 单成员 + 关融合:失败场景完全由注入的替身分类器决定(确定性);
    # 多成员场景在各测试内显式设置 ensemble_members。
    cfg.use_fusion = False
    cfg.ensemble_members = []
    return cfg


def _patch_scan_side_effects(
    monkeypatch: pytest.MonkeyPatch, queue: RecordingQueue
) -> FakeAuditFactory:
    """替换 run_scan 的 discover / build_bundle / queue / audit 默认工厂。"""
    monkeypatch.setattr(
        orchestrator, "_default_discover", lambda url, c, fetch_page=None: [url]
    )
    monkeypatch.setattr(orchestrator, "_default_build_bundle", FakeBundleFactory())
    monkeypatch.setattr(orchestrator, "_default_queue", lambda db_path: queue)
    audits = FakeAuditFactory()
    monkeypatch.setattr(orchestrator, "_default_audit_logger", audits)
    return audits


def _counter(name: str) -> float:
    return telemetry.snapshot()["counters"].get(name, 0.0)


# ---------------------------------------------------------------------------
# 1. 零失败主线:行为与 V10.24 之前逐字节一致
# ---------------------------------------------------------------------------
def test_zero_failure_mainline_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    cfg: Config,
    capsys: pytest.CaptureFixture,
) -> None:
    """零失败主线:不入列、无 note、无警示行、新计数一概不出现。"""
    queue = RecordingQueue()
    _patch_scan_side_effects(monkeypatch, queue)
    base_failed = _counter("scan.classify.failed_images")
    base_aborted = _counter("scan.classify.aborted")

    report = orchestrator.run_scan(
        SITE, cfg, capture=FakeCapture(count=4, prefix="normal"),
        classifier=GoodClassifier(prob=0.02),
    )

    assert report.verdict is Verdict.CLEAN
    assert report.needs_review is False
    assert queue.calls == []  # clean 且零失败:不入列(旧行为)
    assert _counter("scan.classify.failed_images") == base_failed
    assert _counter("scan.classify.aborted") == base_aborted
    assert "识别失败" not in capsys.readouterr().out  # 无警示行


def test_zero_failure_suspect_enqueue_signature_unchanged(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """零失败但需复核(高分 suspect):入列调用口径与旧版逐字一致(零 kwargs)。"""
    queue = RecordingQueue()
    _patch_scan_side_effects(monkeypatch, queue)

    report = orchestrator.run_scan(
        SITE, cfg, capture=FakeCapture(count=4, prefix="nsfw_hi"),
        classifier=GoodClassifier(prob=0.72),
    )

    assert report.verdict is Verdict.SUSPECT
    assert report.needs_review is True
    assert len(queue.calls) == 1
    assert queue.calls[0]["kwargs"] == {}  # 旧版:不带任何附加关键字
    assert queue.calls[0]["zip"] == "fake/evidence/bundle.zip"


# ---------------------------------------------------------------------------
# 2. 部分失败 → 诚信升级(默认开启)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["error", "drop"])
def test_partial_failure_forces_review_with_note(
    monkeypatch: pytest.MonkeyPatch, cfg: Config, mode: str
) -> None:
    """部分失败(3/8 张,两种失败形态):verdict 保持 clean 原档不动,
    needs_review 强制 True,入列 note 带 [识别失败 3 张,判定置信受损],
    遥测 scan.classify.failed_images 按张数累加。"""
    queue = RecordingQueue()
    _patch_scan_side_effects(monkeypatch, queue)
    base_failed = _counter("scan.classify.failed_images")

    report = orchestrator.run_scan(
        SITE, cfg,
        capture=FakeCapture(count=8, prefix="normal"),
        classifier=FlakyClassifier(fail_count=3, mode=mode),
    )

    # 诚信升级:只升复核倾向,三档判定输出不动
    assert report.verdict is Verdict.CLEAN  # 好图全 0.02 → 本应 clean
    assert report.needs_review is True  # 强制复核(V10.23 缺口闭合)
    assert report.agg_nsw_prob == pytest.approx(0.02, abs=1e-9)  # 聚合分不动
    assert report.nsw_image_count == 0
    # 入列:note 中文标注随 add 传给复核队列
    assert len(queue.calls) == 1
    assert queue.calls[0]["kwargs"] == {"note": "[识别失败 3 张,判定置信受损]"}
    # 遥测:按失败张数累加
    assert _counter("scan.classify.failed_images") == base_failed + 3.0


def test_partial_failure_suspect_verdict_unchanged_note_appended(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """非 clean 档(suspect)+ 部分失败:verdict/聚合分不动,note 照标。"""
    queue = RecordingQueue()
    _patch_scan_side_effects(monkeypatch, queue)

    report = orchestrator.run_scan(
        SITE, cfg,
        capture=FakeCapture(count=6, prefix="nsfw_hi"),
        classifier=FlakyClassifier(fail_count=2, mode="error", prob=0.72),
    )

    assert report.verdict is Verdict.SUSPECT  # 好图 0.72 → suspect 原档不动
    assert report.agg_nsw_prob == pytest.approx(0.72, abs=1e-9)
    assert report.needs_review is True
    assert queue.calls[0]["kwargs"] == {"note": "[识别失败 2 张,判定置信受损]"}


def test_switch_off_restores_legacy_behavior(
    monkeypatch: pytest.MonkeyPatch,
    cfg: Config,
    capsys: pytest.CaptureFixture,
) -> None:
    """escalate_on_classify_error=False + 部分失败:回旧行为——clean 保持
    needs_review False、不入列、无计数、无警示行(V10.24 之前逐字节一致)。"""
    queue = RecordingQueue()
    _patch_scan_side_effects(monkeypatch, queue)
    cfg.escalate_on_classify_error = False
    base_failed = _counter("scan.classify.failed_images")

    report = orchestrator.run_scan(
        SITE, cfg,
        capture=FakeCapture(count=8, prefix="normal"),
        classifier=FlakyClassifier(fail_count=3, mode="error"),
    )

    assert report.verdict is Verdict.CLEAN
    assert report.needs_review is False  # 旧行为:静默放过
    assert queue.calls == []
    assert _counter("scan.classify.failed_images") == base_failed
    assert "识别失败" not in capsys.readouterr().out


def test_switch_off_below_threshold_full_failure_still_legacy(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """开关关闭 + 全失败但低于熔断阈值(4 < 5):同样回旧行为(不中止、
    不升级)——中止与升级是两条独立防线,关闭诚信升级不影响熔断阈值判定,
    而 4 张全败未达阈值本就不熔断。"""
    queue = RecordingQueue()
    _patch_scan_side_effects(monkeypatch, queue)
    cfg.escalate_on_classify_error = False

    report = orchestrator.run_scan(
        SITE, cfg,
        capture=FakeCapture(count=4, prefix="normal"),
        classifier=FlakyClassifier(fail_count=4, mode="error"),
    )

    assert report.verdict is Verdict.CLEAN  # 全 0 分集合:旧行为照判 clean
    assert report.needs_review is False
    assert queue.calls == []


# ---------------------------------------------------------------------------
# 3. 全失败 ≥ 阈值 → 快速中止(熔断)
# ---------------------------------------------------------------------------
def test_full_failure_first_batch_aborts_and_skips_remaining_members(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """首笔批量评分 6/6 全部失败(≥ 阈值 5):中文 RuntimeError(含根因
    提示与 vlmctl ping 指引),后续成员不再被调用(计数断言),计数
    scan.classify.aborted / scan.errors / scan.classify.failed_images。"""
    queue = RecordingQueue()
    _patch_scan_side_effects(monkeypatch, queue)
    cfg.ensemble_members = ["stub"]  # 熔断后本应轮到的后续成员(不再盲打)
    flaky = FlakyClassifier(fail_count=6, mode="error")  # 6/6 全失败
    fallback = GoodClassifier(name="stub")
    monkeypatch.setattr(
        orchestrator, "_default_classifier", lambda name, c: fallback
    )
    base_failed = _counter("scan.classify.failed_images")
    base_aborted = _counter("scan.classify.aborted")
    base_errors = _counter("scan.errors")

    with pytest.raises(RuntimeError) as excinfo:
        orchestrator.run_scan(
            SITE, cfg,
            capture=FakeCapture(count=6, prefix="normal"),
            classifier=flaky,
        )

    msg = str(excinfo.value)
    assert "全部失败" in msg
    assert "余额耗尽" in msg and "密钥失效" in msg and "网络中断" in msg
    assert "vlmctl ping" in msg  # 诊断指引
    assert "6/6" in msg  # 失败计数进错误信息
    # 快速中止核心语义:不再对剩余成员盲打(集成成员一次都没被调用)
    assert flaky.batch_calls == 1
    assert fallback.batch_calls == 0
    assert queue.calls == []  # 未走到入列
    assert _counter("scan.classify.aborted") == base_aborted + 1.0
    assert _counter("scan.errors") == base_errors + 1.0
    assert _counter("scan.classify.failed_images") == base_failed + 6.0


def test_abort_threshold_boundary_four_vs_five(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """阈值边界(缺省 5):4 张全败不中止(转诚信升级);5 张全败熔断。"""
    queue = RecordingQueue()
    _patch_scan_side_effects(monkeypatch, queue)
    base_aborted = _counter("scan.classify.aborted")

    # 4 张全败:4 < 5 → 不中止,走诚信升级(needs_review 强制 True)
    report4 = orchestrator.run_scan(
        SITE, cfg,
        capture=FakeCapture(count=4, prefix="normal"),
        classifier=FlakyClassifier(fail_count=4, mode="error"),
    )
    assert report4.verdict is Verdict.CLEAN
    assert report4.needs_review is True
    assert queue.calls[0]["kwargs"]["note"] == "[识别失败 4 张,判定置信受损]"
    assert _counter("scan.classify.aborted") == base_aborted  # 未熔断

    # 5 张全败:5 ≥ 5 → 熔断中止
    with pytest.raises(RuntimeError, match="全部失败"):
        orchestrator.run_scan(
            SITE, cfg,
            capture=FakeCapture(count=5, prefix="normal"),
            classifier=FlakyClassifier(fail_count=5, mode="error"),
        )
    assert _counter("scan.classify.aborted") == base_aborted + 1.0


def test_abort_threshold_configurable_via_cfg(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """classify_abort_threshold 可调:阈值 10 时 5 张全败不熔断(转升级);
    阈值 3 时 3 张全败即熔断。"""
    queue = RecordingQueue()
    _patch_scan_side_effects(monkeypatch, queue)
    cfg.classify_abort_threshold = 10
    base_aborted = _counter("scan.classify.aborted")

    report = orchestrator.run_scan(
        SITE, cfg,
        capture=FakeCapture(count=5, prefix="normal"),
        classifier=FlakyClassifier(fail_count=5, mode="drop"),  # 事故原形态:静默缺评
    )
    assert report.needs_review is True  # 全败但未达阈值:诚信升级兜底
    assert _counter("scan.classify.aborted") == base_aborted

    cfg.classify_abort_threshold = 3
    with pytest.raises(RuntimeError, match="熔断阈值 3"):
        orchestrator.run_scan(
            SITE, cfg,
            capture=FakeCapture(count=3, prefix="normal"),
            classifier=FlakyClassifier(fail_count=3, mode="error"),
        )


def test_abort_not_triggered_when_first_member_succeeds(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """首笔批量评分部分成功(非"全部失败"):不熔断;第二成员全失败也不
    熔断(熔断只看首笔),由诚信升级按合计失败张数处置。"""
    queue = RecordingQueue()
    _patch_scan_side_effects(monkeypatch, queue)
    cfg.ensemble_members = ["mB"]  # 注入 mA 之后再评 mB(两成员)
    first = FlakyClassifier(fail_count=2, mode="error", name="mA")  # 2/4 失败
    second = FlakyClassifier(fail_count=4, mode="error", name="mB")  # 4/4 全失败
    monkeypatch.setattr(orchestrator, "_default_classifier", lambda name, c: second)
    base_aborted = _counter("scan.classify.aborted")

    report = orchestrator.run_scan(
        SITE, cfg,
        capture=FakeCapture(count=4, prefix="normal"),
        classifier=first,
    )

    assert first.batch_calls == 1 and second.batch_calls == 1  # 两个成员都跑了
    assert report.verdict is Verdict.CLEAN
    assert report.needs_review is True  # 合计 6 张失败 → 诚信升级
    assert queue.calls[0]["kwargs"]["note"] == "[识别失败 6 张,判定置信受损]"
    assert _counter("scan.classify.aborted") == base_aborted  # 从未熔断


def test_classifier_batch_raising_member_still_skipped_not_abort(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """成员 classify_batch 整体抛异常(旧语义):记 warning 跳过、由后续成员
    兜底,不触发熔断(熔断针对"静默全失败",不是显式异常)。"""
    queue = RecordingQueue()
    _patch_scan_side_effects(monkeypatch, queue)
    cfg.ensemble_members = ["stub"]  # 异常成员跳过后由该成员兜底(旧语义)

    class RaisingClassifier:
        name = "boom"

        def classify_batch(self, imgs: list[ImageEvidence]) -> list[ImageScore]:
            raise ConnectionError("GLM 接口调用失败:HTTP 429(模拟)")

    fallback = GoodClassifier(name="stub")
    monkeypatch.setattr(orchestrator, "_default_classifier", lambda name, c: fallback)
    base_aborted = _counter("scan.classify.aborted")

    report = orchestrator.run_scan(
        SITE, cfg, capture=FakeCapture(count=6, prefix="normal"),
        classifier=RaisingClassifier(),
    )

    assert fallback.batch_calls == 1  # 异常成员被跳过后兜底成员照常评分
    assert report.verdict is Verdict.CLEAN
    assert report.needs_review is False  # 整批异常走旧语义:不算图片级失败
    assert queue.calls == []
    assert _counter("scan.classify.aborted") == base_aborted


# ---------------------------------------------------------------------------
# 4. clean + 部分失败:CLI 警示行(stdout)与日志(warning)各一行
# ---------------------------------------------------------------------------
def test_clean_partial_failure_prints_prominent_line(
    monkeypatch: pytest.MonkeyPatch,
    cfg: Config,
    capsys: pytest.CaptureFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """verdict=clean 且有失败:stdout 一行醒目中文警示 + 同文日志 warning;
    非 clean 档不打印 stdout 警示行(入列 note 已承载)。"""
    queue = RecordingQueue()
    _patch_scan_side_effects(monkeypatch, queue)

    with caplog.at_level("WARNING", logger="netsentinel.pipeline.orchestrator"):
        report = orchestrator.run_scan(
            SITE, cfg,
            capture=FakeCapture(count=7, prefix="normal"),
            classifier=FlakyClassifier(fail_count=5, mode="error"),
        )

    assert report.verdict is Verdict.CLEAN
    out = capsys.readouterr().out
    assert "警示" in out and "识别失败" in out and "人工复核" in out
    assert out.count("警示") == 1  # 恰一行
    warnings = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "识别失败" in r.getMessage()
    ]
    assert warnings, "日志侧也应有识别失败 warning"
    assert any("识别失败诚信升级" in r.getMessage() for r in warnings)


def test_suspect_partial_failure_no_stdout_banner(
    monkeypatch: pytest.MonkeyPatch,
    cfg: Config,
    capsys: pytest.CaptureFixture,
) -> None:
    """非 clean 档(suspect)+ 部分失败:不打印 stdout 警示行(该行专属于
    clean 的诚信缺口),note 照常入列。"""
    queue = RecordingQueue()
    _patch_scan_side_effects(monkeypatch, queue)

    report = orchestrator.run_scan(
        SITE, cfg,
        capture=FakeCapture(count=6, prefix="nsfw_hi"),
        classifier=FlakyClassifier(fail_count=2, mode="error", prob=0.72),
    )

    assert report.verdict is Verdict.SUSPECT
    assert "警示" not in capsys.readouterr().out
    assert queue.calls[0]["kwargs"]["note"] == "[识别失败 2 张,判定置信受损]"


# ---------------------------------------------------------------------------
# 5. 失败感知单元口径:_count_image_failures / 阈值解析
# ---------------------------------------------------------------------------
def _img(i: int) -> ImageEvidence:
    return ImageEvidence(
        path=f"fake/imgs/x_{i}.jpg", url=f"http://img/x/{i}", source_page=SITE,
        width=300, height=300,
    )


def test_count_image_failures_semantics() -> None:
    """记账口径:dropped = 入批数 − 返回数(负值记 0);errored 只数
    scores["error"] 非空的条目(与 failover 带内失败同语义)。"""
    imgs = [_img(i) for i in range(6)]
    ok = ImageScore(image=imgs[0], model="m", nsfw_prob=0.1, scores={})
    err = ImageScore(
        image=imgs[1], model="m", nsfw_prob=0.0, scores={"error": "GLM 识别失败:x"}
    )
    empty_err = ImageScore(  # 空串 error 不算失败(与 failover 真值语义一致)
        image=imgs[2], model="m", nsfw_prob=0.1, scores={"error": ""}
    )
    default_scores = ImageScore(  # 缺省 scores={}(dataclass 工厂):同样不算
        image=imgs[3], model="m", nsfw_prob=0.1
    )

    assert orchestrator._count_image_failures([], []) == (0, 0)
    assert orchestrator._count_image_failures(imgs, [ok] * 6) == (0, 0)
    # 6 入 2 回 → dropped 4;其中 1 条带内错误 → errored 1
    assert orchestrator._count_image_failures(imgs, [ok, err]) == (4, 1)
    assert orchestrator._count_image_failures(imgs, [err] * 6) == (0, 6)
    assert orchestrator._count_image_failures(
        imgs, [ok, empty_err, default_scores]
    ) == (3, 0)
    # 返回多于入批(异常成员):不惩罚,dropped 记 0
    assert orchestrator._count_image_failures(imgs[:2], [ok] * 5) == (0, 0)


def test_classify_abort_threshold_resolution(cfg: Config) -> None:
    """阈值解析:缺省 5;附加属性可调;非法值回退缺省(不因配置笔误炸扫描)。"""
    assert orchestrator._classify_abort_threshold(cfg) == 5
    cfg.classify_abort_threshold = 10
    assert orchestrator._classify_abort_threshold(cfg) == 10
    cfg.classify_abort_threshold = "bad"
    assert orchestrator._classify_abort_threshold(cfg) == 5
    cfg.classify_abort_threshold = float("nan")
    assert orchestrator._classify_abort_threshold(cfg) == 5
