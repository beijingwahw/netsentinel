"""A37 单元测试:benchmarks 离线基准测试框架(全程离线,只写 tmp_path)。

覆盖点:
- 载入语料:PNG 头嗅探宽高 / sha256 回填 / 排序 / labels.json 两种格式兼容 / 缺失提示;
- run(stub):report.md 与 report.json 落盘、混淆矩阵数值正确
  (stub 对 nsfw_hi=0.97 > 0.9 全中)、PR 曲线 19 点、strict 口径差异;
- evaluate 除零容错(全预测负 / 全误报 / 全漏报);
- pr_curve 默认网格与单调性抽查(tp/fp/fn 单调不增、tn 单调不减、召回不增);
- suggest_threshold:F1 并列取更保守高阈值(默认口径 0.95、strict 口径 0.70);
- make_corpus:12/6/12 配比、标注映射、README 声明、尺寸混合;
- CLI:未知分类器与未配置 glm 均退出码 2(中文提示);--make-corpus 全流程退出码 0。

V5 新增(test_v5_*):批量评分协议与回退、PR 曲线"一次排序多阈值扫描"与
逐阈值 evaluate 的数值等价、遥测(benchmark.run/images/errors)、
make_png 的 zlib IDAT 回读与 CLI 中文校验。

V10.4 新增(adversarial 子命令):``main(["adversarial", ...])`` 把余下参数
原样透传给对抗基准 CLI(--gate/--update-golden/--golden 直达),返回码
0/1/2 原样回传;不带子命令的原有 CLI 路径零改动。
"""
from __future__ import annotations

import importlib.util
import json
import struct
import zlib
from pathlib import Path

import pytest

from netsentinel import telemetry
from netsentinel.contracts import ImageScore
from benchmarks import run_benchmark as rb
from benchmarks.run_benchmark import (
    BenchmarkError,
    default_thresholds,
    evaluate,
    load_corpus,
    main,
    make_corpus,
    pr_curve,
    run,
    sniff_png_size,
    suggest_threshold,
    write_png,
)

# ---------------------------------------------------------------------------
# 临时小语料:6 张图(2 hi / 1 mid / 3 normal),尺寸混合
# ---------------------------------------------------------------------------

_SMALL_PLAN: list[tuple[str, int, int, str]] = [
    ("nsfw_hi_001.png", 200, 200, "nsfw"),
    ("nsfw_hi_002.png", 400, 300, "nsfw"),
    ("nsfw_mid_001.png", 400, 300, "borderline"),
    ("normal_001.png", 200, 200, "clean"),
    ("normal_002.png", 400, 300, "clean"),
    ("normal_003.png", 200, 200, "clean"),
]


def _make_small_corpus(root: Path) -> Path:
    """手造 6 图 + labels.json 的小语料(全部落在 tmp_path 下)。"""
    corpus = root / "corpus"
    corpus.mkdir(parents=True)
    labels: dict[str, str] = {}
    for name, width, height, label in _SMALL_PLAN:
        write_png(corpus / name, width, height, (0x11, 0x22, 0x33))
        labels[name] = label
    (corpus / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return corpus


# ---------------------------------------------------------------------------
# load_corpus / sniff_png_size
# ---------------------------------------------------------------------------


def test_sniff_png_size_roundtrip(tmp_path: Path) -> None:
    path = write_png(tmp_path / "a.png", 400, 300, (1, 2, 3))
    width, height = sniff_png_size(path.read_bytes())
    assert (width, height) == (400, 300)
    with pytest.raises(BenchmarkError):
        sniff_png_size(b"not a png at all........")


def test_load_corpus_sniffs_png_and_sorts(tmp_path: Path) -> None:
    corpus = _make_small_corpus(tmp_path)
    pairs = load_corpus(corpus)
    assert [p[0].path for p in pairs] == [
        str(corpus / name) for name, _, _, _ in sorted(_SMALL_PLAN)
    ]
    by_name = {Path(ev.path).name: (ev, label) for ev, label in pairs}
    for name, width, height, label in _SMALL_PLAN:
        ev, got_label = by_name[name]
        assert got_label == label
        assert (ev.width, ev.height) == (width, height)
        assert len(ev.sha256) == 64
        assert ev.url == f"benchmark://corpus/{name}"


def test_load_corpus_accepts_record_list_format(tmp_path: Path) -> None:
    """labels.json 兼容 [{"file":..., "label":...}] 数组格式。"""
    corpus = _make_small_corpus(tmp_path)
    records = [{"file": name, "label": label} for name, _, _, label in _SMALL_PLAN]
    (corpus / "labels.json").write_text(
        json.dumps(records, ensure_ascii=False), encoding="utf-8"
    )
    pairs = load_corpus(corpus)
    assert len(pairs) == 6
    assert {label for _, label in pairs} == {"nsfw", "borderline", "clean"}


def test_load_corpus_missing_labels_gives_make_corpus_hint(tmp_path: Path) -> None:
    empty = tmp_path / "nope"
    empty.mkdir()
    with pytest.raises(BenchmarkError, match="make-corpus"):
        load_corpus(empty)


def test_load_corpus_rejects_bad_label_and_missing_image(tmp_path: Path) -> None:
    corpus = _make_small_corpus(tmp_path)
    labels = json.loads((corpus / "labels.json").read_text(encoding="utf-8"))
    labels["normal_001.png"] = "weird"
    (corpus / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(BenchmarkError, match="合法取值"):
        load_corpus(corpus)

    labels["normal_001.png"] = "clean"
    labels["ghost.png"] = "clean"  # 引用不存在的图片
    (corpus / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(BenchmarkError, match="不存在"):
        load_corpus(corpus)


# ---------------------------------------------------------------------------
# evaluate:混淆矩阵与除零容错
# ---------------------------------------------------------------------------


def test_evaluate_confusion_matrix_values() -> None:
    scores = [
        ("nsfw", 0.97), ("nsfw", 0.91),      # 双正类命中
        ("borderline", 0.72),                 # 默认不计正类 → tn
        ("clean", 0.02), ("clean", 0.01), ("clean", 0.03),
    ]
    result = evaluate(scores, 0.90)
    assert (result["tp"], result["fp"], result["fn"], result["tn"]) == (2, 0, 0, 4)
    assert result["precision"] == pytest.approx(1.0)
    assert result["recall"] == pytest.approx(1.0)
    assert result["f1"] == pytest.approx(1.0)
    assert result["positive_labels"] == ["nsfw"]


def test_evaluate_zero_division_tolerant() -> None:
    # 全预测为负且无正类:precision/recall/f1 均为 0.0,不抛 ZeroDivisionError
    result = evaluate([("clean", 0.02), ("clean", 0.01)], 0.90)
    assert (result["tp"], result["fp"], result["fn"], result["tn"]) == (0, 0, 0, 2)
    assert (result["precision"], result["recall"], result["f1"]) == (0.0, 0.0, 0.0)

    # 全预测为正但无真实正类:precision=0(0/0 容错)
    result = evaluate([("clean", 0.99)], 0.50)
    assert (result["tp"], result["fp"]) == (0, 1)
    assert (result["precision"], result["recall"], result["f1"]) == (0.0, 0.0, 0.0)

    # 只有正类且全部漏报:recall=0
    result = evaluate([("nsfw", 0.10)], 0.90)
    assert (result["tp"], result["fn"]) == (0, 1)
    assert (result["precision"], result["recall"], result["f1"]) == (0.0, 0.0, 0.0)


def test_evaluate_strict_positive_set() -> None:
    scores = [("nsfw", 0.97), ("borderline", 0.72), ("clean", 0.02)]
    result = evaluate(scores, 0.90, positive={"nsfw", "borderline"})
    assert (result["tp"], result["fp"], result["fn"], result["tn"]) == (1, 0, 1, 1)
    assert result["precision"] == pytest.approx(1.0)
    assert result["recall"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# pr_curve / suggest_threshold
# ---------------------------------------------------------------------------


def test_pr_curve_default_grid_and_custom_thresholds() -> None:
    scores = [("nsfw", 0.97), ("nsfw", 0.50), ("clean", 0.02)]
    curve = pr_curve(scores)
    assert [point["threshold"] for point in curve] == default_thresholds()
    assert default_thresholds()[0] == pytest.approx(0.05)
    assert default_thresholds()[-1] == pytest.approx(0.95)
    assert len(curve) == 19

    custom = pr_curve(scores, thresholds=[0.40, 0.60])
    assert [point["threshold"] for point in custom] == [0.40, 0.60]


def test_pr_curve_monotonicity() -> None:
    """阈值升序:tp/fp/fn 单调不增、tn 单调不减、召回单调不增(抽查)。"""
    scores = [
        ("nsfw", 0.97), ("nsfw", 0.93), ("nsfw", 0.61),
        ("borderline", 0.88), ("borderline", 0.42),
        ("clean", 0.55), ("clean", 0.02),
    ]
    curve = pr_curve(scores)
    for prev, cur in zip(curve, curve[1:]):
        assert cur["threshold"] > prev["threshold"]
        assert cur["tp"] <= prev["tp"]
        assert cur["fp"] <= prev["fp"]
        assert cur["fn"] >= prev["fn"]
        assert cur["tn"] >= prev["tn"]
        assert cur["recall"] <= prev["recall"] + 1e-12
    # 两端点抽查
    assert curve[0]["tp"] == 3 and curve[0]["fp"] == 3   # t=0.05:除 0.02 外全预测正
    assert curve[-1]["tp"] == 1 and curve[-1]["fp"] == 0  # t=0.95:仅 0.97 预测正


def test_suggest_threshold_prefers_conservative_higher_on_tie() -> None:
    scores: list[tuple[str, float]] = [
        ("nsfw", 0.97), ("nsfw", 0.97), ("borderline", 0.72),
        ("clean", 0.02), ("clean", 0.02), ("clean", 0.02),
    ]
    # 默认口径:t>=0.75 起 F1=1.0 并列 → 取最高网格阈值 0.95
    assert suggest_threshold(pr_curve(scores)) == pytest.approx(0.95)
    # strict 口径:t<=0.70 时 F1=1.0 并列 → 取该并列段的最高阈值 0.70
    strict_curve = pr_curve(scores, positive={"nsfw", "borderline"})
    assert suggest_threshold(strict_curve) == pytest.approx(0.70)


def test_suggest_threshold_empty_curve_raises() -> None:
    with pytest.raises(BenchmarkError):
        suggest_threshold([])


# ---------------------------------------------------------------------------
# make_corpus:配比 / 标注 / README / 尺寸
# ---------------------------------------------------------------------------


def test_make_corpus_counts_labels_and_readme(tmp_path: Path) -> None:
    corpus_dir = tmp_path / "corpus"
    summary = make_corpus(corpus_dir)
    assert summary["counts"] == {"nsfw": 12, "borderline": 6, "clean": 12}
    assert summary["total"] == 30

    labels = json.loads((corpus_dir / "labels.json").read_text(encoding="utf-8"))
    assert len(labels) == 30
    assert all(labels[f"nsfw_hi_{i:03d}.png"] == "nsfw" for i in range(1, 13))
    assert all(labels[f"nsfw_mid_{i:03d}.png"] == "borderline" for i in range(1, 7))
    assert all(labels[f"normal_{i:03d}.png"] == "clean" for i in range(1, 13))

    readme = (corpus_dir / "README.md").read_text(encoding="utf-8")
    assert "合成语料" in readme and "不含真实违规内容" in readme

    pairs = load_corpus(corpus_dir)  # 生成物可直接被装载
    widths = {ev.width for ev, _ in pairs}
    heights = {ev.height for ev, _ in pairs}
    assert widths == {200, 400} and heights == {200, 300}  # 尺寸混合


def test_make_corpus_deterministic(tmp_path: Path) -> None:
    first, second = tmp_path / "c1", tmp_path / "c2"
    make_corpus(first)
    make_corpus(second)
    for name in ("nsfw_hi_001.png", "nsfw_mid_006.png", "normal_012.png"):
        assert (first / name).read_bytes() == (second / name).read_bytes()


# ---------------------------------------------------------------------------
# run(stub):报告落盘与指标
# ---------------------------------------------------------------------------


def test_run_stub_writes_reports_with_correct_confusion(tmp_path: Path) -> None:
    corpus = _make_small_corpus(tmp_path)
    out = tmp_path / "out"
    payload = run(corpus, out)

    assert (out / "report.md").is_file()
    assert (out / "report.json").is_file()

    data = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert data == payload  # 落盘 JSON 与返回值一致
    assert data["classifier"] == "stub"
    assert data["eval"]["threshold"] == pytest.approx(0.90)
    # stub 对 nsfw_hi=0.97 > 0.9 全中:borderline 默认不计正类
    assert data["eval"]["confusion"] == {"tp": 2, "fp": 0, "fn": 0, "tn": 4}
    assert data["eval"]["precision"] == pytest.approx(1.0)
    assert data["eval"]["recall"] == pytest.approx(1.0)
    assert data["eval"]["f1"] == pytest.approx(1.0)
    assert len(data["pr_curve"]) == 19
    assert len(data["details"]) == 6
    assert data["eval"]["strict"] is False
    assert "borderline" in data["eval"]["borderline_note"]

    markdown = (out / "report.md").read_text(encoding="utf-8")
    for key in ("混淆矩阵", "PR 表", "建议阈值", "逐图明细", "结论"):
        assert key in markdown
    assert "stub 仅为管线基准锚点,生产请用 glm/nudenet 集成" in markdown
    assert "TP = 2" in markdown and "TN = 4" in markdown


def test_run_stub_strict_counts_borderline(tmp_path: Path) -> None:
    corpus = _make_small_corpus(tmp_path)
    payload = run(corpus, tmp_path / "out_strict", strict=True)
    assert payload["eval"]["positive_labels"] == ["borderline", "nsfw"]
    # borderline(0.72 < 0.9)计入正类后成为漏报
    assert payload["eval"]["confusion"] == {"tp": 2, "fp": 0, "fn": 1, "tn": 3}
    assert payload["eval"]["precision"] == pytest.approx(1.0)
    assert payload["eval"]["recall"] == pytest.approx(2 / 3)
    assert payload["eval"]["f1"] == pytest.approx(0.8)


def test_run_empty_corpus_raises(tmp_path: Path) -> None:
    corpus = tmp_path / "empty"
    corpus.mkdir()
    (corpus / "labels.json").write_text("{}", encoding="utf-8")
    with pytest.raises(BenchmarkError, match="语料为空"):
        run(corpus, tmp_path / "out")


# ---------------------------------------------------------------------------
# CLI:退出码与中文提示
# ---------------------------------------------------------------------------


def test_main_stub_flow_exit_0(tmp_path: Path) -> None:
    corpus = _make_small_corpus(tmp_path)
    out = tmp_path / "cli_out"
    rc = main(["--corpus", str(corpus), "--out", str(out)])
    assert rc == 0
    assert (out / "report.md").is_file()
    assert (out / "report.json").is_file()


def test_main_make_corpus_then_run_exit_0(tmp_path: Path) -> None:
    corpus = tmp_path / "gen"
    out = tmp_path / "gen_out"
    rc = main(["--make-corpus", "--corpus", str(corpus), "--out", str(out)])
    assert rc == 0
    payload = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert payload["corpus"]["total"] == 30
    assert payload["eval"]["confusion"]["tp"] == 12


def test_main_unknown_classifier_exit_2(tmp_path: Path, capsys) -> None:
    corpus = _make_small_corpus(tmp_path)
    rc = main(
        ["--corpus", str(corpus), "--out", str(tmp_path / "o"), "--classifier", "no_such"]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "no_such" in err
    assert "无法创建分类器" in err or "未注册" in err


def test_main_glm_unconfigured_exit_2(tmp_path: Path, capsys) -> None:
    """glm 未配置(vlm_online=False / 无密钥)时:中文明确提示 + 退出码 2,不出报告。"""
    corpus = _make_small_corpus(tmp_path)
    out = tmp_path / "glm_out"
    rc = main(["--corpus", str(corpus), "--out", str(out), "--classifier", "glm"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "GLM" in err or "glm" in err
    assert "vlm_online" in err or "未注册" in err
    assert not (out / "report.md").exists()  # 未静默产出无效报告

# ---------------------------------------------------------------------------
# V5 升级:批量评分协议 / PR 曲线单趟扫描 / 遥测 / make_png CLI 中文校验
# ---------------------------------------------------------------------------


class _SpyClassifier:
    """探针分类器:按文件名打分(与 stub 同规则),分别统计批量/逐张调用次数。"""

    name = "spy"

    def __init__(self) -> None:
        self.batch_calls = 0
        self.single_calls = 0

    @staticmethod
    def _prob(img) -> float:
        fname = Path(img.path).name
        if "nsfw_hi" in fname:
            return 0.97
        if "nsfw_mid" in fname:
            return 0.72
        return 0.02

    def classify(self, img):
        self.single_calls += 1
        return ImageScore(image=img, model="spy", nsfw_prob=self._prob(img))

    def classify_batch(self, imgs):
        self.batch_calls += 1
        return [ImageScore(image=im, model="spy", nsfw_prob=self._prob(im)) for im in imgs]


class _SpyNoBatch(_SpyClassifier):
    """旧式分类器:没有批量接口(抹掉 classify_batch),必须走逐张回退。"""

    classify_batch = None  # type: ignore[assignment]


class _SpyBadBatch(_SpyClassifier):
    """批量接口返回长度不符:必须回退逐张,不得产出错位结果。"""

    def classify_batch(self, imgs):  # type: ignore[override]
        self.batch_calls += 1
        return [ImageScore(image=imgs[0], model="spy", nsfw_prob=0.5)]


@pytest.mark.parametrize(
    "spy,expect_batch,expect_single",
    [
        (_SpyClassifier, 1, 0),
        (_SpyNoBatch, 0, 6),
        (_SpyBadBatch, 1, 6),
    ],
    ids=["batch", "no-batch-fallback", "bad-length-fallback"],
)
def test_v5_run_classify_batch_protocol_and_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spy,
    expect_batch: int,
    expect_single: int,
) -> None:
    """评分循环走 classify_batch(一次批量);无批量接口/返回长度不符时回退逐张。"""
    corpus = _make_small_corpus(tmp_path)
    instance = spy()
    monkeypatch.setattr(rb, "_build_classifier", lambda name, cfg: instance)

    payload = run(corpus, tmp_path / "out")
    assert instance.batch_calls == expect_batch
    assert instance.single_calls == expect_single
    # 三条路径的数值结果完全一致(与 stub 口径相同:2 hi 全中,borderline 不计正类)
    assert payload["eval"]["confusion"] == {"tp": 2, "fp": 0, "fn": 0, "tn": 4}
    assert len(payload["details"]) == 6


def test_v5_pr_curve_single_sweep_matches_naive_evaluate() -> None:
    """PR 曲线"一次排序多阈值扫描"与逐阈值 evaluate 数值完全等价(含并列/空表)。"""
    datasets: list[list[tuple[str, float]]] = [
        [],
        [("nsfw", 0.5)],
        [
            ("nsfw", 0.97), ("nsfw", 0.97), ("borderline", 0.72),
            ("clean", 0.02), ("clean", 0.02),
        ],
        [
            ("nsfw", 0.05), ("nsfw", 0.25), ("nsfw", 0.45), ("nsfw", 0.65),
            ("borderline", 0.85), ("borderline", 0.95), ("clean", 0.15),
            ("clean", 0.35), ("clean", 0.55), ("clean", 0.75),
        ],
    ]
    grids: list[list[float]] = [
        default_thresholds(),
        [0.60, 0.40, 0.60, 0.30],   # 乱序且含重复阈值
        [0.123456, 0.5, 0.999],     # 非网格步长的任意阈值(输出按 4 位舍入)
    ]
    for scores in datasets:
        for positive in ({"nsfw"}, {"nsfw", "borderline"}):
            for grid in grids:
                assert pr_curve(scores, grid, positive) == [
                    evaluate(scores, t, positive) for t in grid
                ], (scores, positive, grid)


def test_v5_pr_curve_preserves_grid_order() -> None:
    """输出顺序与传入网格一致(乱序网格不重排,重复阈值逐位保留)。"""
    scores = [("nsfw", 0.9), ("clean", 0.1)]
    out = pr_curve(scores, thresholds=[0.60, 0.40, 0.60])
    assert [p["threshold"] for p in out] == [0.60, 0.40, 0.60]
    assert out[0] == out[2]


def test_v5_run_emits_telemetry(tmp_path: Path) -> None:
    """run 全程计时 benchmark.run,语料张数计入 benchmark.images(零依赖遥测)。"""
    telemetry.reset()
    corpus = _make_small_corpus(tmp_path)
    run(corpus, tmp_path / "out")
    snap = telemetry.snapshot()
    assert snap["timers"]["benchmark.run"]["count"] == 1
    assert snap["counters"]["benchmark.images"] == 6


def test_v5_main_counts_expected_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 可预期失败(未知分类器 / glm 未配置)计入 benchmark.errors。"""
    telemetry.reset()
    corpus = _make_small_corpus(tmp_path)
    assert main(["--corpus", str(corpus), "--out", str(tmp_path / "o1"), "--classifier", "no_such"]) == 2
    assert main(["--corpus", str(corpus), "--out", str(tmp_path / "o2"), "--classifier", "glm"]) == 2
    assert telemetry.snapshot()["counters"]["benchmark.errors"] == 2


def _load_make_png_module():
    """独立加载 scripts/make_png.py(与 run_benchmark 的缓存装载互不干扰)。"""
    spec = importlib.util.spec_from_file_location(
        "test_v5_make_png_module", rb._ROOT / "scripts" / "make_png.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_v5_make_png_idat_single_zlib_roundtrip() -> None:
    """IDAT 生成确认为 zlib:单块、可解压还原为 filter=0 的纯色扫描线。"""
    module = _load_make_png_module()
    width, height, rgb = 13, 7, (0x2E, 0x54, 0x8A)
    data = module.make_png(width, height, rgb)

    chunks = []
    pos = 8  # 跳过签名
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        chunks.append((data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + length]))
        pos += 12 + length
    idat_chunks = [payload for tag, payload in chunks if tag == b"IDAT"]
    assert len(idat_chunks) == 1  # 单块 IDAT(整幅一次 zlib 压缩)

    scanline = b"\x00" + bytes(rgb) * width
    assert zlib.decompress(idat_chunks[0]) == scanline * height  # bytes 整体构造可回读


def test_v5_make_png_cli_chinese_validation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 宽/高/颜色取值非法:中文提示 + 退出码 2 + 不落盘(与 A19 语义一致)。"""
    module = _load_make_png_module()
    out = tmp_path / "nested" / "pic.png"
    cases = [
        (["0", "4", "112233", str(out)], "宽度"),
        (["6", "abc", "112233", str(out)], "高度"),
        (["6", "4", "XYZ12A", str(out)], "颜色"),
        (["6", "4", "12345", str(out)], "颜色"),
    ]
    for argv, keyword in cases:
        assert module.main(argv) == 2, argv
        err = capsys.readouterr().err
        assert "错误" in err and keyword in err, (argv, err)
        assert not out.exists()


# ---------------------------------------------------------------------------
# V10.4:adversarial 子命令透传(金标门禁参数直达,返回码原样回传)
# ---------------------------------------------------------------------------


def _gate_cli_args(corpus: Path, out: Path, golden: Path, *extra: str) -> list[str]:
    """adversarial 子命令参数拼装(子命令名 + corpus/out/golden + 余项透传)。"""
    return ["--corpus", str(corpus), "--out", str(out), "--golden", str(golden), *extra]


class _DropClassifier:
    """固定掉分分类器:文件名带扰动后缀即 0.2,原图 0.5(掉分 0.3,构造门禁回归)。"""

    name = "dropreg"

    _SUFFIXES = ("_blur.png", "_mosaic.png", "_occlude.png", "_jpeg.jpg")

    def classify(self, img):
        fname = Path(img.path).name
        prob = 0.2 if any(fname.endswith(suffix) for suffix in self._SUFFIXES) else 0.5
        return ImageScore(image=img, model=self.name, nsfw_prob=prob)


def test_main_adversarial_subcommand_passthrough_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """adversarial 子命令:--update-golden / --gate 透传,违例时主 CLI 退出码 2。"""
    corpus = _make_small_corpus(tmp_path)
    golden = tmp_path / "adversarial_golden.json"
    out = tmp_path / "adv_out"

    # 透传 --update-golden:重建金标(返回码 0)
    rc = main(
        ["adversarial", *_gate_cli_args(corpus, out, golden, "--update-golden")]
    )
    assert rc == 0
    assert golden.is_file()
    assert (out / "adversarial_report.json").is_file()
    capsys.readouterr()

    # 透传 --gate:同指纹同结果 → 通过(返回码 0)
    rc = main(["adversarial", *_gate_cli_args(corpus, out, golden, "--gate")])
    assert rc == 0
    assert "金标门禁通过" in capsys.readouterr().out

    # 构造回归:注册名仍是 stub(指纹不变),评分口径 monkeypatch 退化 → 退出码 2
    monkeypatch.setattr(
        "benchmarks.adversarial._build_classifier", lambda name, cfg: _DropClassifier()
    )
    rc = main(["adversarial", *_gate_cli_args(corpus, out, golden, "--gate")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "金标违例" in err and "退出码 2" in err
    assert "基线" in err and "容差" in err


def test_main_adversarial_subcommand_input_error_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """adversarial 子命令:金标坏 JSON → 中文错误 + 主 CLI 退出码 1(输入错误)。"""
    corpus = _make_small_corpus(tmp_path)
    golden = tmp_path / "adversarial_golden.json"
    golden.write_text("{oops: bad json", encoding="utf-8")
    rc = main(
        ["adversarial", *_gate_cli_args(corpus, tmp_path / "o", golden, "--gate")]
    )
    assert rc == 1
    assert "金标文件无法解析" in capsys.readouterr().err


def test_main_without_subcommand_cli_unchanged(tmp_path: Path) -> None:
    """不带子命令的原有 CLI 路径零改动:仍走 A37 主基准并落 report.json。"""
    corpus = _make_small_corpus(tmp_path)
    rc = main(["--corpus", str(corpus), "--out", str(tmp_path / "o")])
    assert rc == 0
    assert (tmp_path / "o" / "report.json").is_file()
    assert not (tmp_path / "o" / "adversarial_report.json").exists()
