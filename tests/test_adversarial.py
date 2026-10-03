"""A55 单元测试:对抗鲁棒性基准(全程离线,只写 tmp_path)。

覆盖点:
- perturb:四类变体产出、命名保留原 stem、保持原始宽高、遮挡中心为黑、
  变体内容确实与原图不同(棋盘格图,模糊/马赛克会真正改变像素);
- run(stub):adversarial_report.md / adversarial_report.json 落盘、json 可解析、
  stub 按文件名打分对视觉扰动免疫 → 四类降幅恒 ≈ 0(框架验证)、
  报告含 stub 免疫声明与四类中文标签;
- 降幅统计口径:drop = 原始 − 扰动(payload.details 逐图核对);
- 分类器未注册 → AdversarialError(中文指引)/ CLI 退出码 2;
- glm 未配置(vlm_online=False)→ 拒绝评测,CLI 退出码 2;
- 语料缺 labels.json → 透传 A37 的 BenchmarkError,CLI 退出码 2;
- 无 Pillow 分支:monkeypatch 屏蔽 PIL 后 perturb/run 抛中文异常、CLI 退出码 2;
- CLI 成功路径:退出码 0、stdout 摘要、报告文件存在;
- V9 统计推断层(纯 stdlib,确定性):bootstrap_mean_ci95 /
  signflip_permutation_pvalue / wilson_ci95 的同种子确定性、全零降幅 →
  CI=[0,0] 且 p=1、恒正降幅 → p 极小、Wilson 0/N 与 N/N 边界闭式退化、
  空输入 ValueError;run() 级别:payload 新增 ci95/p_value/wilson_ci 字段、
  markdown 新列、两次运行推断结果逐位一致、模拟掉分分类器下的显著性检出。
- V10.4 金标回归门禁:同指纹同结果 → 门禁通过(--update-golden 往返);
  monkeypatch 模拟掉分分类器(指纹不变、指标偏移超容差)→ 中文违例清单 +
  退出码 2;指纹敏感性(换 classifier 名 / 加语料文件 → 缺基线,warn 默认
  跳过、fail 按违例);金标损坏 / 坏 JSON → 中文错误退出码 1;容差 =
  max(10%·|基线|, 对应 CI 半宽) 取值断言;gate=False 默认 payload 不含
  "gate" 键(既有语义零变动)。
- 感知感知(perceptual-aware)扰动预算曲线(grid 模式,对标 2025-26 攻防
  报告从固定 L_p 转 SSIM 约束 ε 网格的范式):grid 默认关闭 payload 无
  curves 键;SSIM 手算对照(恒等=1.0、常量窗闭式、双窗均值、低相似<阈值、
  小图缩窗、尺寸不一致/PIL 缺席中文报错);网格参数确实生效(强弱变体
  字节不同且 SSIM 更低、文件名保留 stem);robustness_auc /
  min_effective_attack 手算对照与非法输入;确定性 _SimGridDropClassifier
  构造单调响应 → 曲线单调性、四类 AUC(0.225/0.42/0.35/0.325)与
  min_effective(含 jpeg q=60 恰 0.2 的严格大于边界);stub 全零曲线;
  两次运行 curves 逐位一致;grid 与金标门禁正交(指纹/门禁结果不变);
  markdown 曲线表与 CLI --grid 摘要。
- V13 感知曲线独立金标门禁:--update-curve-golden → --curve-golden 往返
  通过(stub:auc=0 容差 0.05、min_effective=None);_SimGridDropClassifier
  下基线值与容差(max(10%·|基线|, 0.05))逐项手算对照;模拟掉分分类器
  → AUC 偏移违例 + None→数值 null_flip 违例 + 退出码 2;
  evaluate_curve_gate 单元(边界 delta==tol 不违例 / None↔数值双向违例 /
  同 None 通过 / 缺指标键违例);曲线指纹独立于单点指纹且对网格与曲线
  版本敏感(单点指纹不受牵连);单点与曲线两金标文件字节级互不触碰、
  双门禁并存 rc 0;曲线金标损坏/结构非法 → 退出码 1;curve_gate 默认
  关闭、开启时自动附带 grid;缺基线 warn/fail 策略。
- V13-2 曲线金标三项扩展:shape_digest 形状指纹(平移构造场景——AUC 与
  min_effective 同值但斜率符号翻转 → kind=shape_changed 检出;正常同分布
  复跑(严格 0 容差)确定性不误报;离散容差 = 允许翻转段数,tol=1 放行
  单段翻转 / 拦截两段翻转;函数级手算对照与非法输入;金标结构校验按
  指标名分派——shape_digest 的 value 为 hex 串 tol 为非负整数);版本
  递增 v13-2 后旧曲线金标按缺基线处置(warn / fail);--curve-missing-
  baseline 拆分(缺省继承 --missing-baseline,显式设置则单点/曲线独立
  策略矩阵,CLI 透传)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from benchmarks.adversarial import (
    BOOTSTRAP_B,
    CURVE_GATE_METRICS,
    CURVE_GOLDEN_SCHEMA,
    CURVE_GOLDEN_STATS_VERSION,
    CURVE_SHAPE_FLIP_TOL,
    CURVE_TOL_MIN,
    CURVE_TOL_VALUE_RATIO,
    PERMUTATION_EXACT_MAX_N,
    PERMUTATION_MC_N,
    PERTURB_GRIDS,
    PERTURB_META,
    PERTURB_TYPES,
    STATS_SEED,
    AdversarialError,
    AdversarialGoldenError,
    GATE_METRICS,
    GOLDEN_SCHEMA,
    bootstrap_mean_ci95,
    build_curve_golden_entry,
    build_golden_entry,
    compute_fingerprint,
    compute_curve_fingerprint,
    compute_ssim,
    curve_metric_tolerance,
    curve_shape_digest,
    evaluate_curve_gate,
    evaluate_gate,
    load_curve_golden,
    load_golden,
    main,
    metric_tolerance,
    min_effective_attack,
    perturb,
    render_markdown,
    robustness_auc,
    run,
    signflip_permutation_pvalue,
    wilson_ci95,
)
from benchmarks.run_benchmark import BenchmarkError, write_png

# 扰动生成依赖 Pillow;环境缺失时整文件跳过(其余分支用 monkeypatch 单测)。
PIL = pytest.importorskip("PIL", reason="需要 Pillow 才能测试对抗扰动生成")
from PIL import Image as PILImage  # noqa: E402

# ---------------------------------------------------------------------------
# 临时小语料:3 张图(1 hi / 1 mid / 1 normal)
# ---------------------------------------------------------------------------

_SMALL_PLAN: list[tuple[str, int, int, str]] = [
    ("nsfw_hi_001.png", 240, 200, "nsfw"),
    ("nsfw_mid_001.png", 320, 240, "borderline"),
    ("normal_001.png", 200, 200, "clean"),
]


def _make_small_corpus(root: Path) -> Path:
    """手造 3 图 + labels.json 的小语料(全部落在 tmp_path 下)。"""
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


def _checkerboard_png(path: Path, width: int = 160, height: int = 128, cell: int = 5) -> Path:
    """生成棋盘格 PNG(像素有高频变化,模糊/马赛克才会真正改变内容)。

    格宽取 5:与马赛克的 8 倍降采样因子**不整除对齐**,保证 8x8 马赛克
    采样后内容确实改变(cell=8 时马赛克会原样还原棋盘格,断言会失真)。
    """
    rows = bytearray()
    for y in range(height):
        for x in range(width):
            value = 235 if ((x // cell) + (y // cell)) % 2 == 0 else 20
            rows.extend((value, value // 2, 255 - value))
    image = PILImage.frombytes("RGB", (width, height), bytes(rows))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG")
    return path


# ---------------------------------------------------------------------------
# perturb:四类变体
# ---------------------------------------------------------------------------


def test_perturb_produces_four_variants_named_with_stem(tmp_path: Path) -> None:
    src = _checkerboard_png(tmp_path / "nsfw_hi_001.png")
    out_dir = tmp_path / "variants"
    paths = perturb(src, out_dir)

    assert len(paths) == 4
    assert [Path(p).name for p in paths] == [
        "nsfw_hi_001_blur.png",
        "nsfw_hi_001_mosaic.png",
        "nsfw_hi_001_occlude.png",
        "nsfw_hi_001_jpeg.jpg",
    ]
    source_bytes = src.read_bytes()
    for kind, path in zip(PERTURB_TYPES, paths):
        target = Path(path)
        # 命名保留原 stem(stub 关键词不断链)+ 文件落盘且非空
        assert "nsfw_hi_001" in target.stem
        assert target.parent == out_dir
        assert target.is_file() and target.stat().st_size > 0
        # 棋盘格图经模糊/马赛克/遮挡/重压缩后内容确实改变
        assert target.read_bytes() != source_bytes
        # 四类扰动均保持原始宽高
        with PILImage.open(target) as variant:
            assert variant.size == (160, 128)


def test_perturb_occlude_blacks_center(tmp_path: Path) -> None:
    """中央 25% 面积黑块:中心像素为纯黑,角落像素保持原样(棋盘格亮格)。"""
    src = _checkerboard_png(tmp_path / "normal_001.png")
    paths = perturb(src, tmp_path / "variants")
    occlude = Path(paths[PERTURB_TYPES.index("occlude")])
    with PILImage.open(occlude) as variant:
        assert variant.convert("RGB").getpixel((80, 64)) == (0, 0, 0)
        corner = variant.convert("RGB").getpixel((4, 4))
        assert corner != (0, 0, 0)


def test_perturb_rejects_undecodable_image(tmp_path: Path) -> None:
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"definitely not a png")
    with pytest.raises(AdversarialError, match="无法解码"):
        perturb(bad, tmp_path / "variants")


# ---------------------------------------------------------------------------
# run(stub):报告产出与"stub 免疫 → 降幅≈0"框架验证
# ---------------------------------------------------------------------------


def test_run_stub_writes_reports_with_zero_drop(tmp_path: Path) -> None:
    corpus = _make_small_corpus(tmp_path)
    out = tmp_path / "out"
    payload = run(corpus, out)

    md_path = out / "adversarial_report.md"
    json_path = out / "adversarial_report.json"
    assert md_path.is_file() and json_path.is_file()

    reloaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert reloaded["classifier"] == "stub"
    assert reloaded["corpus"]["total"] == 3
    assert reloaded["corpus"]["labels"] == {"borderline": 1, "clean": 1, "nsfw": 1}
    assert len(reloaded["details"]) == 3

    # stub 按文件名打分,变体保留原 stem → 四类降幅恒 ≈ 0(框架验证)
    for row in reloaded["stats"]:
        assert row["n"] == 3
        assert row["avg_abs_drop"] == pytest.approx(0.0, abs=1e-9)
        assert row["max_drop"] == pytest.approx(0.0, abs=1e-9)
        assert row["drop_gt_0.2_ratio"] == pytest.approx(0.0, abs=1e-9)

    # 逐图核对:变体分 == 原始分,drop = 原始 − 扰动 = 0
    by_file = {row["file"]: row for row in reloaded["details"]}
    for name, _, _, _ in _SMALL_PLAN:
        row = by_file[name]
        assert row["base_prob"] == pytest.approx(
            0.97 if "nsfw_hi" in name else (0.72 if "nsfw_mid" in name else 0.02)
        )
        for kind in PERTURB_TYPES:
            assert row["variants"][kind]["prob"] == pytest.approx(row["base_prob"])
            assert row["variants"][kind]["drop"] == pytest.approx(0.0, abs=1e-9)

    # 报告为中文,含四类扰动标签与 stub 免疫声明
    markdown = md_path.read_text(encoding="utf-8")
    assert "对抗鲁棒性基准报告" in markdown
    for kind in PERTURB_TYPES:
        assert PERTURB_META[kind]["label"] in markdown
    assert "天然免疫" in markdown
    assert "生产模型" in markdown and "重跑" in markdown


def test_run_does_not_touch_corpus_dir(tmp_path: Path) -> None:
    """变体写临时目录:语料目录前后快照一致,目录内不新增任何文件。"""
    corpus = _make_small_corpus(tmp_path)
    before = sorted(p.name for p in corpus.iterdir())
    run(corpus, tmp_path / "out")
    after = sorted(p.name for p in corpus.iterdir())
    assert after == before


# ---------------------------------------------------------------------------
# 错误路径:未注册分类器 / glm 未配置 / 语料缺失
# ---------------------------------------------------------------------------


def test_run_unknown_classifier_raises_chinese_error(tmp_path: Path) -> None:
    corpus = _make_small_corpus(tmp_path)
    with pytest.raises(AdversarialError, match="无法创建分类器 'nope'"):
        run(corpus, tmp_path / "out", classifier="nope")
    assert not (tmp_path / "out" / "adversarial_report.json").exists()


def test_run_glm_without_config_is_rejected(tmp_path: Path) -> None:
    """默认安全态(vlm_online=False)下拒绝 GLM 评测,不产出无效报告。"""
    corpus = _make_small_corpus(tmp_path)
    with pytest.raises(AdversarialError, match="vlm_online"):
        run(corpus, tmp_path / "out", classifier="glm")


def test_run_missing_labels_propagates_benchmark_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty_corpus"
    empty.mkdir()
    with pytest.raises(BenchmarkError, match="labels.json"):
        run(empty, tmp_path / "out")


# ---------------------------------------------------------------------------
# 无 Pillow 分支(monkeypatch 屏蔽;真实缺 PIL 时整文件已 importorskip)
# ---------------------------------------------------------------------------


def test_pil_missing_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # 先用 PIL 造好测试图,再屏蔽:PIL 的 save 会触发插件初始化,
    # 需要父包 "PIL" 可导入,不能在屏蔽状态下造图。
    src = _checkerboard_png(tmp_path / "nsfw_hi_001.png")
    monkeypatch.setitem(sys.modules, "PIL", None)

    with pytest.raises(AdversarialError, match="Pillow"):
        perturb(src, tmp_path / "variants")
    with pytest.raises(AdversarialError, match="Pillow"):
        run(tmp_path, tmp_path / "out")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_end_to_end_stub(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    corpus = _make_small_corpus(tmp_path)
    out = tmp_path / "out"
    code = main(["--corpus", str(corpus), "--out", str(out), "--classifier", "stub"])

    assert code == 0
    captured = capsys.readouterr()
    assert "对抗鲁棒性基准完成" in captured.out
    assert "blur" in captured.out and "mosaic" in captured.out
    assert (out / "adversarial_report.md").is_file()
    assert (out / "adversarial_report.json").is_file()


def test_cli_exit_code_2_on_expected_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = _make_small_corpus(tmp_path)

    code = main(["--corpus", str(corpus), "--out", str(tmp_path / "o1"), "--classifier", "nope"])
    assert code == 2
    err = capsys.readouterr().err
    assert "错误" in err and "nope" in err

    code = main(["--corpus", str(corpus), "--out", str(tmp_path / "o2"), "--classifier", "glm"])
    assert code == 2
    assert "vlm_online" in capsys.readouterr().err


def test_cli_exit_code_2_when_pil_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = _make_small_corpus(tmp_path)
    monkeypatch.setitem(sys.modules, "PIL", None)
    code = main(["--corpus", str(corpus), "--out", str(tmp_path / "out")])
    assert code == 2
    assert "Pillow" in capsys.readouterr().err

# ---------------------------------------------------------------------------
# V5 升级:单次解码 / 生成与评分循环合并(整轮一次批量)/ 遥测
# ---------------------------------------------------------------------------


def test_v5_perturb_decodes_source_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """四变体共享一次源图打开与解码:整个 perturb 过程 PIL.Image.open 恰好 1 次。"""
    src = _checkerboard_png(tmp_path / "nsfw_hi_001.png")
    original_open = PILImage.open
    opened: list[object] = []

    def _counting_open(fp, *args, **kwargs):
        opened.append(fp)
        return original_open(fp, *args, **kwargs)

    monkeypatch.setattr(PILImage, "open", _counting_open)
    paths = perturb(src, tmp_path / "variants")
    assert len(paths) == 4
    assert len(opened) == 1, f"源图应只解码一次,实际打开 {len(opened)} 次"


def test_v5_perturb_deterministic_bytes(tmp_path: Path) -> None:
    """V5 重排(JPEG 先于原地遮挡)后仍确定性:两次 perturb 逐变体字节一致。"""
    src = _checkerboard_png(tmp_path / "normal_001.png")
    first = perturb(src, tmp_path / "v1")
    second = perturb(src, tmp_path / "v2")
    for a, b in zip(first, second):
        assert Path(a).read_bytes() == Path(b).read_bytes()


class _SpyClassifier:
    """探针分类器:按文件名打分(与 stub 同规则),统计批量/逐张调用次数。"""

    name = "spy"

    def __init__(self) -> None:
        self.batch_calls = 0
        self.single_calls = 0
        self.batch_sizes: list[int] = []

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
        from netsentinel.contracts import ImageScore

        return ImageScore(image=img, model="spy", nsfw_prob=self._prob(img))

    def classify_batch(self, imgs):
        from netsentinel.contracts import ImageScore

        self.batch_calls += 1
        self.batch_sizes.append(len(imgs))
        return [ImageScore(image=im, model="spy", nsfw_prob=self._prob(im)) for im in imgs]


def test_v5_run_merges_loops_into_single_batch_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """生成与评分循环合并:整轮(原图+四变体)恰好一次 classify_batch,无逐张调用。"""
    from benchmarks import adversarial as adv

    corpus = _make_small_corpus(tmp_path)
    spy = _SpyClassifier()
    monkeypatch.setattr(adv, "_build_classifier", lambda name, cfg: spy)

    payload = run(corpus, tmp_path / "out")
    # 3 张图 × (1 原图 + 4 变体) = 15 条证据,一次批量评分收拢
    assert spy.batch_calls == 1
    assert spy.batch_sizes == [15]
    assert spy.single_calls == 0
    # 数值口径不变:变体保留原 stem → 探针(与 stub 同规则)降幅恒 0
    for row in payload["stats"]:
        assert row["n"] == 3
        assert row["avg_abs_drop"] == pytest.approx(0.0, abs=1e-9)
    by_file = {row["file"]: row for row in payload["details"]}
    assert by_file["nsfw_hi_001.png"]["base_prob"] == pytest.approx(0.97)
    assert by_file["normal_001.png"]["base_prob"] == pytest.approx(0.02)


def test_v5_run_emits_telemetry(tmp_path: Path) -> None:
    """run 全程计时 adversarial.run,图片数/变体数计入对应计数器。"""
    from netsentinel import telemetry

    telemetry.reset()
    corpus = _make_small_corpus(tmp_path)
    run(corpus, tmp_path / "out")
    snap = telemetry.snapshot()
    assert snap["timers"]["adversarial.run"]["count"] == 1
    assert snap["counters"]["adversarial.images"] == 3
    assert snap["counters"]["adversarial.variants"] == 12  # 3 张 × 4 类扰动


# ===========================================================================
# V9 统计推断层:配对 bootstrap CI + 符号翻转置换检验 + Wilson CI
# (对标 RobustBench / HELM 的 CI 报告规范;全部纯 stdlib、固定种子确定性)
# ===========================================================================

#: Wilson 检验用标准正态 97.5% 分位(与被测模块同一常量值,独立内联防抄袭)。
_Z = 1.959963984540054

#: 函数级测试用的混合降幅样本(含正负与零,非平凡分布)。
_MIXED = [0.31, -0.02, 0.44, 0.12, 0.0, -0.07, 0.22, 0.18]


# ---------------------------------------------------------------------------
# 确定性:同种子同结果(bootstrap 与蒙特卡洛置换都必须逐位可复现)
# ---------------------------------------------------------------------------


def test_v9_bootstrap_deterministic_same_seed() -> None:
    """同种子两次 bootstrap → CI 逐位相等;不同种子 → 重采样序列确实不同。"""
    first = bootstrap_mean_ci95(_MIXED, seed=STATS_SEED)
    second = bootstrap_mean_ci95(_MIXED, seed=STATS_SEED)
    assert first == second
    # 种子真实驱动采样:换种子后区间(连续值)实际改变
    other = bootstrap_mean_ci95(_MIXED, seed=STATS_SEED + 1)
    assert other != first


def test_v9_permutation_montecarlo_deterministic_same_seed() -> None:
    """蒙特卡洛置换(n>exact_max_n 触发 MC 路径)同种子逐位一致。"""
    values = [0.05 * (i % 7) - 0.1 for i in range(13)]  # n=13 → MC
    assert len(values) > PERMUTATION_EXACT_MAX_N
    first = signflip_permutation_pvalue(values, seed=7)
    second = signflip_permutation_pvalue(values, seed=7)
    assert first == second
    assert first[1] == "monte_carlo"


def test_v9_run_inference_deterministic_across_runs(tmp_path: Path) -> None:
    """run() 级别:同一语料两次完整运行,全部推断字段逐位一致。"""
    corpus = _make_small_corpus(tmp_path)
    first = run(corpus, tmp_path / "o1")
    second = run(corpus, tmp_path / "o2")
    assert len(first["stats"]) == len(second["stats"]) == len(PERTURB_TYPES)
    for row_a, row_b in zip(first["stats"], second["stats"]):
        assert row_a["ci95"] == row_b["ci95"]
        assert row_a["p_value"] == row_b["p_value"]
        assert row_a["p_value_method"] == row_b["p_value_method"]
        assert row_a["wilson_ci"] == row_b["wilson_ci"]


# ---------------------------------------------------------------------------
# 统计性质:全零降幅 / 恒正降幅 / 均值恰为零 / CI 覆盖样本均值
# ---------------------------------------------------------------------------


def test_v9_bootstrap_all_zero_ci_collapses_to_zero_covering_true_mean() -> None:
    """全零降幅:bootstrap CI 收缩为 [0, 0](覆盖真均值 0),不是无效区间。"""
    ci = bootstrap_mean_ci95([0.0, 0.0, 0.0, 0.0])
    assert ci == (0.0, 0.0)
    lo, hi = ci
    assert lo <= 0.0 <= hi  # 覆盖 0


def test_v9_bootstrap_ci_covers_sample_mean_symmetric_sample() -> None:
    """对称样本:CI 覆盖样本均值且非退化(lo < mean < hi)。"""
    values = [0.1, 0.2, 0.3, 0.4, 0.5]  # 关于 0.3 对称
    lo, hi = bootstrap_mean_ci95(values)
    assert lo < 0.3 < hi


def test_v9_permutation_all_zero_p_is_one() -> None:
    """全零降幅:精确与蒙特卡洛两条路径的 p 值都恰为 1(零效应不误报)。"""
    p_exact, method = signflip_permutation_pvalue([0.0, 0.0, 0.0])
    assert method == "exact"
    assert p_exact == 1.0
    p_mc, method_mc = signflip_permutation_pvalue([0.0] * 13)
    assert method_mc == "monte_carlo"
    assert p_mc == 1.0  # +1 校正下 (9999+1)/10000


def test_v9_permutation_constant_positive_p_tiny() -> None:
    """恒正且互不相等的降幅(强效应):观测统计量是置换族最大值 → p 极小。"""
    values = [0.25 + 0.01 * i for i in range(16)]  # n=16 → 蒙特卡洛路径
    p, method = signflip_permutation_pvalue(values)
    assert method == "monte_carlo"
    assert p <= 0.001, f"恒正降幅 p 应极小,实际 {p}"
    # 小样本精确路径的对照:n=3 恒正时仅全正/全负两种极端翻转达标 → p=2/8
    p3, method3 = signflip_permutation_pvalue([0.3, 0.4, 0.5])
    assert method3 == "exact"
    assert p3 == pytest.approx(2 / 8)


def test_v9_permutation_zero_mean_symmetric_p_ge_099() -> None:
    """均值恰为 0 的对称样本:每个置换统计量都 ≥ |obs|=0 → p ≥ 0.99。"""
    values = [0.5, -0.5, 0.3, -0.3, 0.1, -0.1]
    p, method = signflip_permutation_pvalue(values)
    assert method == "exact"
    assert p >= 0.99


def test_v9_permutation_method_dispatch_by_n() -> None:
    """n ≤ 12 精确枚举,n ≥ 13 蒙特卡洛(+1 校正保证 p ≥ 1/(n_mc+1))。"""
    p_small, m_small = signflip_permutation_pvalue([0.1] * PERMUTATION_EXACT_MAX_N)
    assert m_small == "exact"
    assert 0.0 < p_small <= 1.0
    p_big, m_big = signflip_permutation_pvalue([0.1] * (PERMUTATION_EXACT_MAX_N + 1))
    assert m_big == "monte_carlo"
    assert p_big >= 1.0 / (PERMUTATION_MC_N + 1)


def test_v9_wilson_boundaries_degenerate_correctly() -> None:
    """Wilson CI 闭式边界:0/N → [0, z²/(n+z²)];N/N → [n/(n+z²), 1]。"""
    z2 = _Z * _Z
    n = 8
    lo0, hi0 = wilson_ci95(0, n)
    assert lo0 == pytest.approx(0.0, abs=1e-12)
    assert hi0 == pytest.approx(z2 / (n + z2), abs=1e-12)
    assert 0.0 < hi0 < 0.5  # 上界仍有信息量,不塌缩为 [0,1]

    lo1, hi1 = wilson_ci95(n, n)
    assert hi1 == pytest.approx(1.0, abs=1e-12)
    assert lo1 == pytest.approx(n / (n + z2), abs=1e-12)
    assert 0.5 < lo1 < 1.0


def test_v9_wilson_covers_point_estimate_and_rejects_bad_input() -> None:
    """中间情形覆盖点估计 k/n;非法输入(n≤0 / k 越界)抛 ValueError。"""
    for k, n in ((3, 8), (1, 4), (2, 7), (10, 13)):
        lo, hi = wilson_ci95(k, n)
        assert 0.0 <= lo <= k / n <= hi <= 1.0
    with pytest.raises(ValueError):
        wilson_ci95(3, 0)
    with pytest.raises(ValueError):
        wilson_ci95(9, 8)
    with pytest.raises(ValueError, match="无法计算置信区间"):
        bootstrap_mean_ci95([])
    with pytest.raises(ValueError, match="无法检验"):
        signflip_permutation_pvalue([])


# ---------------------------------------------------------------------------
# run() 级别:payload 新字段 / markdown 新列 / CLI 摘要 / 模拟掉分信号
# ---------------------------------------------------------------------------


def test_v9_run_stub_payload_has_inference_fields(tmp_path: Path) -> None:
    """stub 全零降幅 → 每类扰动 ci95=[0,0](覆盖 0)、p=1、Wilson=[0, z²/(3+z²)]。"""
    corpus = _make_small_corpus(tmp_path)
    payload = run(corpus, tmp_path / "out")
    z2 = _Z * _Z
    expected_wilson_hi = round(z2 / (3 + z2), 4)  # 0/3 的 Wilson 上界
    for row in payload["stats"]:
        # 新增推断字段(既有字段仍在且语义不变)
        assert row["ci95"] == [0.0, 0.0]
        assert row["p_value"] == 1.0
        assert row["wilson_ci"] == [0.0, expected_wilson_hi]
        assert "exact" in row["p_value_method"]
        assert "n" in row and "avg_drop" in row and "avg_abs_drop" in row
        assert "max_drop" in row and "drop_gt_0.2_ratio" in row
        # CI 覆盖真值 0
        assert row["ci95"][0] <= 0.0 <= row["ci95"][1]
    meta = payload["inference"]
    assert meta["seed"] == STATS_SEED
    assert meta["bootstrap_b"] == BOOTSTRAP_B == 10_000
    assert meta["permutation_mc_n"] == PERMUTATION_MC_N == 9_999
    assert meta["permutation_exact_max_n"] == PERMUTATION_EXACT_MAX_N


def test_v9_markdown_renders_inference_columns_and_notes(tmp_path: Path) -> None:
    """markdown 总表新增三列与三条方法注脚;老 payload(无 ci95)回退旧表。"""
    corpus = _make_small_corpus(tmp_path)
    payload = run(corpus, tmp_path / "out")
    markdown = (tmp_path / "out" / "adversarial_report.md").read_text(encoding="utf-8")
    header = "| 扰动类型 | 变体文件 | 扰动参数 | 平均绝对降幅 | 最大降幅 | 降幅>0.2 占比 | 平均降幅 95% CI | 置换 p 值 | 占比 Wilson 95% CI |"
    assert header in markdown
    assert "| [0.0000, 0.0000] | 1.0000 |" in markdown  # stub 零降幅行
    assert "95% CI" in markdown and "Wilson" in markdown
    assert "bootstrap" in markdown and "置换" in markdown
    # 兼容性:剥离 ci95 的老 payload 仍可渲染(不抛错、不含新列)
    legacy = json.loads(json.dumps(payload))
    for row in legacy["stats"]:
        for key in ("ci95", "ci95_method", "p_value", "p_value_method", "wilson_ci", "wilson_ci_method"):
            row.pop(key)
    legacy_md = render_markdown(legacy)
    assert "平均降幅 95% CI" not in legacy_md
    assert "平均绝对降幅" in legacy_md  # 旧表结构原样


def test_v9_cli_stdout_includes_inference_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 摘要行带上 CI95 / p 值 / Wilson CI(既有措辞不删除)。"""
    corpus = _make_small_corpus(tmp_path)
    code = main(["--corpus", str(corpus), "--out", str(tmp_path / "out"), "--classifier", "stub"])
    assert code == 0
    out = capsys.readouterr().out
    assert "平均降幅CI95[0.0000,0.0000]" in out
    assert "置换p=1.0000" in out
    assert "占比WilsonCI[0.0000,0.5615]" in out


#: 模拟分类器的"按扰动类型掉分比例"(模块级常量,避免可变类属性)。
_SIM_RATIO: dict[str, float] = {
    "_blur.png": 0.05,
    "_mosaic.png": 0.35,
    "_occlude.png": 0.55,
    "_jpeg.jpg": 0.02,
}


class _SimDropClassifier:
    """确定性"按扰动类型掉分"模拟分类器:base = 0.5+0.04·i,变体按类型比例掉分。

    用于给统计推断层注入真实信号:occlude 恒掉 55%(全部 > 0.2)、
    mosaic 掉 35%(部分 > 0.2)、blur 掉 5%、jpeg 掉 2%(均恒正且互异)。
    """

    name = "simdrop"

    def classify(self, img):
        from netsentinel.contracts import ImageScore

        fname = Path(img.path).name
        stem, ratio = fname, 0.0
        for suffix, r in _SIM_RATIO.items():
            if fname.endswith(suffix):
                stem, ratio = fname[: -len(suffix)], r
                break
        else:
            stem = fname.rsplit(".", 1)[0]  # 原图:sim_00.png → sim_00
        base = 0.5 + 0.04 * int(stem.rsplit("_", 1)[1])
        return ImageScore(
            image=img, model=self.name, nsfw_prob=max(0.0, base * (1.0 - ratio))
        )


def _make_sim_corpus(root: Path, count: int = 13) -> Path:
    """13 张 sim_XX 小图语料(n=13 → 置换检验走蒙特卡洛路径)。"""
    corpus = root / "sim_corpus"
    corpus.mkdir(parents=True)
    labels: dict[str, str] = {}
    for i in range(count):
        name = f"sim_{i:02d}.png"
        write_png(corpus / name, 48, 32, (0x40, 0x80, 0xC0))
        labels[name] = "nsfw" if i % 2 else "borderline"
    (corpus / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return corpus


def test_v9_run_with_real_signal_detects_significance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模拟掉分分类器:恒正降幅 → p≤0.001 且 CI 下界>0;全数>0.2 → Wilson 上界=1。"""
    from benchmarks import adversarial as adv

    corpus = _make_sim_corpus(tmp_path)  # n=13 → 蒙特卡洛置换
    monkeypatch.setattr(adv, "_build_classifier", lambda name, cfg: _SimDropClassifier())
    payload = run(corpus, tmp_path / "out")

    by_type = {row["type"]: row for row in payload["stats"]}
    z2 = _Z * _Z
    n = payload["corpus"]["total"]
    assert n == 13

    # 四类降幅全部恒正且互异 → p 值极小(观测统计量为置换族最大值)
    for kind in PERTURB_TYPES:
        row = by_type[kind]
        assert row["p_value"] <= 0.001, f"{kind} 恒正降幅 p 应极小:{row}"
        assert "monte carlo" in row["p_value_method"]
        assert row["ci95"][0] > 0.0  # bootstrap CI 下界离零(效应方向确定)
        assert row["avg_drop"] > 0.0

    # occlude:base×0.55 ∈ [0.275, 0.539] 全部 > 0.2 → k=n → Wilson [n/(n+z²), 1]
    occlude = by_type["occlude"]
    assert occlude["drop_gt_0.2_ratio"] == 1.0
    assert occlude["wilson_ci"][1] == 1.0
    assert occlude["wilson_ci"][0] == pytest.approx(round(n / (n + z2), 4), abs=5e-5)

    # blur:base×0.05 ∈ [0.025, 0.049] 无一 > 0.2 → k=0 → Wilson [0, z²/(n+z²)]
    blur = by_type["blur"]
    assert blur["drop_gt_0.2_ratio"] == 0.0
    assert blur["wilson_ci"][0] == 0.0
    assert blur["wilson_ci"][1] == pytest.approx(round(z2 / (n + z2), 4), abs=5e-5)

    # mosaic:base×0.35 > 0.2 ⇔ base > 4/7 ⇔ i ≥ 2 → k=11;Wilson 覆盖点估计
    mosaic = by_type["mosaic"]
    assert mosaic["drop_gt_0.2_ratio"] == pytest.approx(11 / 13, abs=5e-5)  # 4 位舍入
    lo, hi = mosaic["wilson_ci"]
    assert lo <= 11 / 13 <= hi


# ===========================================================================
# V10.4 对抗基准金标回归门禁:指纹 / 容差 / 往返 / 违例 / 缺基线 / 坏金标
# (对标 regression-based benchmark gates;全程 tmp_path,不落仓库输出)
# ===========================================================================


def _golden_fingerprint_keys(data: dict) -> list[str]:
    """金标 JSON 的指纹键(排除 ``_`` 前缀元数据)。"""
    return [key for key in data if not key.startswith("_")]


def _gate_cli_args(corpus: Path, out: Path, golden: Path, *extra: str) -> list[str]:
    """金标门禁 CLI 参数拼装(corpus/out/golden 三固定项 + 余项)。"""
    return ["--corpus", str(corpus), "--out", str(out), "--golden", str(golden), *extra]


def test_gate_update_then_gate_roundtrip_passes(tmp_path: Path) -> None:
    """--update-golden 往返:重建 → 同指纹复跑 → 门禁通过(payload + CLI 双口径)。"""
    from benchmarks import adversarial as adv

    corpus = _make_small_corpus(tmp_path)
    golden = tmp_path / "adversarial_golden.json"
    assert not golden.exists()

    # 1) run() API:显式重建金标
    first = run(corpus, tmp_path / "o1", update_golden=True, golden_path=golden)
    assert first["gate"]["mode"] == "update"
    assert first["gate"]["status"] == "updated"
    assert first["gate"]["metrics_recorded"] == len(PERTURB_TYPES) * len(GATE_METRICS) == 12
    data = json.loads(golden.read_text(encoding="utf-8"))
    assert data["_schema"] == GOLDEN_SCHEMA
    assert data["_stats_version"] == adv.GOLDEN_STATS_VERSION
    fps = _golden_fingerprint_keys(data)
    assert fps == [first["gate"]["fingerprint"]]
    entry = data[fps[0]]
    expected_keys = [f"{kind}/{metric}" for kind in PERTURB_TYPES for metric in GATE_METRICS]
    assert sorted(entry) == sorted(expected_keys)
    for spec in entry.values():
        assert set(spec) == {"value", "tol"} and spec["tol"] >= 0.0

    # 2) 同指纹复跑(stub 确定性 → 指标逐位一致)→ 门禁通过
    second = run(corpus, tmp_path / "o2", gate=True, golden_path=golden)
    assert second["gate"]["mode"] == "check"
    assert second["gate"]["status"] == "pass"
    assert second["gate"]["violations"] == []
    assert second["gate"]["fingerprint"] == fps[0]
    assert second["gate"]["metrics_total"] == 12

    # 3) CLI 往返:重建 → 门禁通过(退出码 0;diff 摘要另见下方 CLI 专项测试)
    rc = main(_gate_cli_args(corpus, tmp_path / "o3", golden, "--update-golden"))
    assert rc == 0
    rc = main(_gate_cli_args(corpus, tmp_path / "o4", golden, "--gate"))
    assert rc == 0


def test_gate_cli_prints_update_diff_and_pass_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 摘要:首次重建"新增基线",再次重建"项持平",门禁通过各就各位。"""
    corpus = _make_small_corpus(tmp_path)
    golden = tmp_path / "adversarial_golden.json"

    assert main(_gate_cli_args(corpus, tmp_path / "o1", golden, "--update-golden")) == 0
    out = capsys.readouterr().out
    assert "金标已重建" in out and "新增基线" in out and "容差=max(10%·|基线|, CI 半宽)" in out

    assert main(_gate_cli_args(corpus, tmp_path / "o2", golden, "--update-golden")) == 0
    out = capsys.readouterr().out
    assert "基线刷新" in out and "12 项持平" in out

    assert main(_gate_cli_args(corpus, tmp_path / "o3", golden, "--gate")) == 0
    out = capsys.readouterr().out
    assert "金标门禁通过" in out and "12 项指标全部在容差内" in out


def test_gate_detects_regression_violations_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """构造回归:同指纹(stub 名不变)但 monkeypatch 分类器指标偏移超容差 → 违例 + 退出码 2。"""
    corpus = _make_small_corpus(tmp_path)
    golden = tmp_path / "adversarial_golden.json"
    assert main(_gate_cli_args(corpus, tmp_path / "o1", golden, "--update-golden")) == 0
    capsys.readouterr()

    # 模拟掉分分类器:注册名仍是 "stub"(指纹不变),评分口径退化(指标偏移)
    monkeypatch.setattr(
        "benchmarks.adversarial._build_classifier", lambda name, cfg: _SimDropClassifier()
    )
    rc = main(_gate_cli_args(corpus, tmp_path / "o2", golden, "--gate"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "金标违例" in err and "退出码 2" in err

    payload = run(corpus, tmp_path / "o3", gate=True, golden_path=golden)
    gate = payload["gate"]
    assert gate["status"] == "violations"
    by_metric = {v["metric"]: v for v in gate["violations"]}
    # stub 全零基线下 avg_drop 容差为 0 → 任何系统性掉分都拦截
    assert "blur/avg_drop" in by_metric and "jpeg/avg_drop" in by_metric
    # occlude 掉分 0.297 → 占比从 0 升到 1.0,超出 Wilson 半宽容差 ≈ 0.2807
    occlude = by_metric["occlude/drop_gt_0.2_ratio"]
    assert occlude["kind"] == "metric_violation"
    assert occlude["baseline"] == pytest.approx(0.0, abs=1e-9)
    assert occlude["current"] == pytest.approx(1.0)
    assert occlude["tol"] == pytest.approx(0.5615 / 2, abs=1e-4)
    assert occlude["delta"] == pytest.approx(1.0)
    # 每条违例 message 含指标名 / 基线 / 当前 / 容差四要素(中文)
    for violation in gate["violations"]:
        message = violation["message"]
        assert "基线" in message and "当前" in message and "容差" in message
        assert violation["perturb"] in PERTURB_TYPES


def test_gate_fingerprint_sensitivity_missing_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """指纹敏感性:换 classifier 名 / 加语料文件 → 指纹变 → 缺基线(warn 跳过 / fail 违例)。"""
    from benchmarks import adversarial as adv

    corpus = _make_small_corpus(tmp_path)
    golden = tmp_path / "adversarial_golden.json"
    run(corpus, tmp_path / "o1", update_golden=True, golden_path=golden)

    # (a) 换 classifier 注册名(评分口径不变,仅名字变)→ 新指纹 → 缺基线
    monkeypatch.setattr(adv, "_build_classifier", lambda name, cfg: _SpyClassifier())
    renamed = run(corpus, tmp_path / "o2", classifier="stub_renamed", gate=True, golden_path=golden)
    assert renamed["gate"]["status"] == "no_baseline"
    stored_fps = _golden_fingerprint_keys(json.loads(golden.read_text(encoding="utf-8")))
    assert renamed["gate"]["fingerprint"] not in stored_fps  # 旧金标未命中
    assert any("--update-golden" in w for w in renamed["gate"]["warnings"])

    # (b) CLI 默认 warn:中文警告 + 退出码 0,金标不被改写
    before = golden.read_text(encoding="utf-8")
    rc = main(
        _gate_cli_args(corpus, tmp_path / "o3", golden, "--gate", "--classifier", "stub_renamed")
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "缺基线" in err and "--update-golden" in err
    assert golden.read_text(encoding="utf-8") == before  # warn 不改写金标

    # (c) --missing-baseline fail:按回归违例处理 → 退出码 2
    rc = main(
        _gate_cli_args(
            corpus, tmp_path / "o4", golden, "--gate", "--classifier", "stub_renamed",
            "--missing-baseline", "fail",
        )
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "无基线" in err and "missing_baseline=fail" in err
    failed = run(
        corpus, tmp_path / "o5", classifier="stub_renamed",
        gate=True, golden_path=golden, missing_baseline="fail",
    )
    assert failed["gate"]["status"] == "violations"
    assert failed["gate"]["violations"][0]["kind"] == "missing_baseline"

    # (d) 加一个语料文件(内容新)→ 指纹变 → 缺基线(classifier 名不变)
    write_png(corpus / "nsfw_hi_002.png", 200, 200, (0x77, 0x11, 0x22))
    labels = json.loads((corpus / "labels.json").read_text(encoding="utf-8"))
    labels["nsfw_hi_002.png"] = "nsfw"
    (corpus / "labels.json").write_text(json.dumps(labels), encoding="utf-8")
    grown = run(corpus, tmp_path / "o6", gate=True, golden_path=golden)
    assert grown["gate"]["status"] == "no_baseline"
    assert grown["gate"]["fingerprint"] != renamed["gate"]["fingerprint"]
    assert json.loads(golden.read_text(encoding="utf-8")) == json.loads(before)  # 仍未改写


def test_gate_default_off_keeps_payload_shape(tmp_path: Path) -> None:
    """gate=False(默认)payload 不含 "gate" 键:既有字段与语义零变动。"""
    corpus = _make_small_corpus(tmp_path)
    payload = run(corpus, tmp_path / "plain_out")
    assert "gate" not in payload
    report_json = tmp_path / "plain_out" / "adversarial_report.json"
    reloaded = json.loads(report_json.read_text(encoding="utf-8"))
    assert "gate" not in reloaded
    assert reloaded["classifier"] == "stub"
    # gate 开启后仅新增 "gate" 键,既有指标字段原样
    gated = run(corpus, tmp_path / "gate_out", gate=True, golden_path=tmp_path / "g.json")
    assert set(gated) - set(payload) == {"gate"}
    for row_new, row_old in zip(gated["stats"], payload["stats"]):
        assert row_new == row_old


def test_gate_corrupt_golden_exit_code_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """金标损坏 / 坏 JSON / 结构非法 → 中文错误 + 退出码 1(输入错误惯例)。"""
    corpus = _make_small_corpus(tmp_path)
    golden = tmp_path / "adversarial_golden.json"

    golden.write_text("{oops: 不是合法 JSON", encoding="utf-8")
    rc = main(_gate_cli_args(corpus, tmp_path / "o1", golden, "--gate"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "金标文件无法解析" in err and "--update-golden" in err

    # 顶层非对象
    golden.write_text("[1, 2, 3]", encoding="utf-8")
    rc = main(_gate_cli_args(corpus, tmp_path / "o2", golden, "--gate"))
    assert rc == 1
    assert "顶层必须是对象" in capsys.readouterr().err

    # 指标表结构非法
    golden.write_text(json.dumps({"a" * 64: 42}), encoding="utf-8")
    rc = main(_gate_cli_args(corpus, tmp_path / "o3", golden, "--gate"))
    assert rc == 1
    assert "结构非法" in capsys.readouterr().err

    # 指标条目缺 tol
    golden.write_text(
        json.dumps({"a" * 64: {"blur/avg_drop": {"value": 0.1}}}), encoding="utf-8"
    )
    rc = main(_gate_cli_args(corpus, tmp_path / "o4", golden, "--gate"))
    assert rc == 1
    assert "结构非法" in capsys.readouterr().err

    # 版本不兼容
    golden.write_text(json.dumps({"_schema": "alien/9"}), encoding="utf-8")
    rc = main(_gate_cli_args(corpus, tmp_path / "o5", golden, "--gate"))
    assert rc == 1
    assert "版本不兼容" in capsys.readouterr().err

    # update 模式同样拒绝损坏金标(须显式修复/删除,而非静默覆盖)
    golden.write_text("{broken", encoding="utf-8")
    with pytest.raises(AdversarialGoldenError, match="金标文件无法解析"):
        run(corpus, tmp_path / "o6", update_golden=True, golden_path=golden)
    assert load_golden(tmp_path / "not_exists.json") == {}  # 不存在 → 空金标


def test_metric_tolerance_formula() -> None:
    """容差 = max(10%·|基线|, 对应 CI 半宽):两分支取值与非法指标名。"""
    row = {"ci95": [0.1, 0.3], "wilson_ci": [0.3, 0.5]}
    # CI 半宽 0.1 支配(10%·|value| 更小)
    assert metric_tolerance("avg_drop", 0.2, row) == pytest.approx(0.1)
    assert metric_tolerance("avg_abs_drop", 0.03, row) == pytest.approx(0.1)
    # 占比指标用 Wilson 半宽
    assert metric_tolerance("drop_gt_0.2_ratio", 0.4, row) == pytest.approx(0.1)
    # 10%·|value| 支配(CI 收得很窄)
    assert metric_tolerance("avg_drop", 0.9, {"ci95": [0.85, 0.95]}) == pytest.approx(0.09)
    row_ratio = {"wilson_ci": [0.99, 1.0]}
    assert metric_tolerance("drop_gt_0.2_ratio", 1.0, row_ratio) == pytest.approx(0.1)
    # 非门禁指标 → ValueError(中文)
    with pytest.raises(ValueError, match="非门禁指标"):
        metric_tolerance("max_drop", 0.5, row)


def test_metric_tolerance_recorded_in_golden_from_ci(tmp_path: Path) -> None:
    """stub 实跑:金标容差确实取自 V9 推断字段(avg_drop 用 ci95 半宽,占比用 Wilson 半宽)。"""
    corpus = _make_small_corpus(tmp_path)
    golden = tmp_path / "adversarial_golden.json"
    payload = run(corpus, tmp_path / "out", update_golden=True, golden_path=golden)
    data = json.loads(golden.read_text(encoding="utf-8"))
    entry = data[payload["gate"]["fingerprint"]]

    for row in payload["stats"]:
        ci_lo, ci_hi = row["ci95"]
        w_lo, w_hi = row["wilson_ci"]
        # stub 全零:avg_drop 基线 0 → 容差 = ci95 半宽 = 0(确定性数据零容差)
        assert entry[f"{row['type']}/avg_drop"] == {"value": 0.0, "tol": 0.0}
        assert entry[f"{row['type']}/avg_abs_drop"] == {"value": 0.0, "tol": 0.0}
        # 占比基线 0 → 容差 = Wilson 半宽(0/N 上界一半,边界不塌缩)
        assert entry[f"{row['type']}/drop_gt_0.2_ratio"]["value"] == 0.0
        assert entry[f"{row['type']}/drop_gt_0.2_ratio"]["tol"] == pytest.approx(
            round((w_hi - w_lo) / 2.0, 6), abs=1e-9
        )
        assert (w_hi - w_lo) / 2.0 == pytest.approx(round((w_hi - w_lo) / 2.0, 6))
    # build_golden_entry 与落盘条目逐项一致(独立重算)
    assert build_golden_entry(payload["stats"]) == entry


def test_evaluate_gate_boundary_and_missing_metric() -> None:
    """evaluate_gate 单元:偏差恰等于容差不违例;超出违例;缺指标键按缺基线违例。"""
    stats = [
        {
            "type": "blur",
            "label": "高斯模糊",
            "avg_drop": 0.1,       # |0.1 − 0| = 0.1 == tol → 不违例(边界)
            "avg_abs_drop": 0.11,  # 0.11 > 0.1 → 违例
            "drop_gt_0.2_ratio": 0.0,
        }
    ]
    entry = {
        "blur/avg_drop": {"value": 0.0, "tol": 0.1},
        "blur/avg_abs_drop": {"value": 0.0, "tol": 0.1},
        # blur/drop_gt_0.2_ratio 故意缺失 → 指标缺基线违例
    }
    violations = evaluate_gate(stats, entry)
    by_metric = {v["metric"]: v for v in violations}
    assert set(by_metric) == {"blur/avg_abs_drop", "blur/drop_gt_0.2_ratio"}

    violation = by_metric["blur/avg_abs_drop"]
    assert violation["kind"] == "metric_violation"
    assert violation["baseline"] == pytest.approx(0.0)
    assert violation["current"] == pytest.approx(0.11)
    assert violation["tol"] == pytest.approx(0.1)
    assert violation["delta"] == pytest.approx(0.11)
    assert "高斯模糊" in violation["message"] and "avg_abs_drop" in violation["message"]

    missing = by_metric["blur/drop_gt_0.2_ratio"]
    assert missing["kind"] == "missing_metric"
    assert missing["baseline"] is None and missing["tol"] is None
    assert "--update-golden" in missing["message"]

    # 完整条目且全部在容差内(基线与当前一致)→ 空清单
    full = {
        "blur/avg_drop": {"value": 0.1, "tol": 0.05},
        "blur/avg_abs_drop": {"value": 0.11, "tol": 0.1},
        "blur/drop_gt_0.2_ratio": {"value": 0.0, "tol": 0.0},
    }
    assert evaluate_gate(stats, full) == []


def test_fingerprint_deterministic_and_sensitive(tmp_path: Path) -> None:
    """指纹确定性 + 敏感性:分类器名 / 语料文件增删 / 重命名任一变化即换键。"""
    corpus = _make_small_corpus(tmp_path)
    fp = compute_fingerprint("stub", corpus)
    assert len(fp) == 64  # sha256 hex
    assert compute_fingerprint("stub", corpus) == fp  # 同输入逐位一致
    assert compute_fingerprint("nudenet", corpus) != fp  # 分类器名变 → 新键

    # 加一个语料文件(内容新)→ 新键
    write_png(corpus / "nsfw_hi_002.png", 240, 200, (0x50, 0x60, 0x70))
    labels = json.loads((corpus / "labels.json").read_text(encoding="utf-8"))
    labels["nsfw_hi_002.png"] = "nsfw"
    (corpus / "labels.json").write_text(json.dumps(labels), encoding="utf-8")
    fp_grown = compute_fingerprint("stub", corpus)
    assert fp_grown != fp

    # 仅改标注(labels.json 内容变)→ 新键(评测口径变化)
    labels["nsfw_hi_002.png"] = "borderline"
    (corpus / "labels.json").write_text(json.dumps(labels), encoding="utf-8")
    assert compute_fingerprint("stub", corpus) != fp_grown

    # 同字节不同文件名 → 新键(stub 按文件名打分,重命名即口径变化)
    left, right = tmp_path / "c_left", tmp_path / "c_right"
    left.mkdir()
    right.mkdir()
    write_png(left / "nsfw_hi_001.png", 200, 200, (0x11, 0x22, 0x33))
    write_png(right / "normal_001.png", 200, 200, (0x11, 0x22, 0x33))
    for root, name in ((left, "nsfw_hi_001.png"), (right, "normal_001.png")):
        (root / "labels.json").write_text(json.dumps({name: "nsfw"}), encoding="utf-8")
    assert compute_fingerprint("stub", left) != compute_fingerprint("stub", right)


# ===========================================================================
# 感知感知(perceptual-aware)扰动预算曲线:ε 网格 / SSIM / AUC / min_effective
# (对标 2025-26 攻防报告从固定 L_p 参数转向 SSIM 约束 ε 网格的范式;
#   全部确定性、零随机源,只写 tmp_path;与金标门禁正交)
# ===========================================================================


def _make_grid_corpus(root: Path, count: int = 3) -> Path:
    """count 张 48x32 棋盘格 sim_XX 语料(高频内容 → SSIM 对扰动强度敏感)。"""
    corpus = root / "grid_corpus"
    corpus.mkdir(parents=True)
    labels: dict[str, str] = {}
    for i in range(count):
        name = f"sim_{i:02d}.png"
        _checkerboard_png(corpus / name, width=48, height=32, cell=3)
        labels[name] = "nsfw" if i % 2 else "borderline"
    (corpus / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return corpus


class _SimGridDropClassifier:
    """确定性"随攻击强度单调掉分"模拟分类器(曲线单调性 / AUC 手算对照用)。

    原图 → 1.0;网格变体按 (kind, param) 公式掉分:blur 0.05·r、mosaic
    0.03·b、occlude 面积占比、jpeg 0.5·(1 − q/100)——四类公式都随 ε 单调
    不减,且 jpeg 在 q=60 处恰为 0.2(检验 min_effective 的严格大于边界)。
    单点变体文件名(如 ``sim_00_blur.png``)不含 ``_blur_`` 标记,按原图
    1.0 处理 → 单点 stats 恒零,曲线与单点互不污染。
    """

    name = "simgrid"

    @staticmethod
    def _drop(fname: str) -> float:
        for kind in PERTURB_TYPES:
            marker = f"_{kind}_"
            if marker in fname:
                tag = int(fname.rsplit(marker, 1)[1].split(".")[0])
                if kind == "blur":
                    return 0.05 * tag
                if kind == "mosaic":
                    return 0.03 * tag
                if kind == "occlude":
                    return tag / 100.0
                return 0.5 * (1.0 - tag / 100.0)
        return 0.0

    def classify(self, img):
        from netsentinel.contracts import ImageScore

        return ImageScore(
            image=img, model=self.name, nsfw_prob=1.0 - self._drop(Path(img.path).name)
        )


#: 四类扰动的手算期望(ε / 平均降幅 / AUC / 首个显著掉分参数),与
#: _SimGridDropClassifier 的掉分公式一一对应(3 张图同分 → 均值即公式值)。
_GRID_EXPECT: dict[str, dict[str, object]] = {
    "blur": {
        "params": [1, 2, 3, 5, 8],
        "eps": [1.0, 2.0, 3.0, 5.0, 8.0],
        "drops": [0.05, 0.10, 0.15, 0.25, 0.40],
        "auc": 0.225,      # 梯形:(.075+.125+.40+.975)/7
        "mea_param": 5,
    },
    "mosaic": {
        "params": [4, 8, 16, 24],
        "eps": [4.0, 8.0, 16.0, 24.0],
        "drops": [0.12, 0.24, 0.48, 0.72],
        "auc": 0.42,       # (0.72+2.88+4.80)/20
        "mea_param": 8,
    },
    "occlude": {
        "params": [0.10, 0.25, 0.40, 0.60],
        "eps": [0.1, 0.25, 0.4, 0.6],
        "drops": [0.10, 0.25, 0.40, 0.60],
        "auc": 0.35,       # (0.02625+0.04875+0.10)/0.5
        "mea_param": 0.25,
    },
    "jpeg": {
        "params": [60, 40, 30, 20, 10],
        "eps": [0.4, 0.6, 0.7, 0.8, 0.9],
        "drops": [0.20, 0.30, 0.35, 0.40, 0.45],
        "auc": 0.325,      # (0.05+0.0325+0.0375+0.0425)/0.5
        "mea_param": 40,   # q=60 恰 0.2,严格大于不触发 → 首个显著掉分是 q=40
    },
}


# ---------------------------------------------------------------------------
# 网格常量与默认关闭语义
# ---------------------------------------------------------------------------


def test_perturb_meta_grid_field_matches_constants() -> None:
    """PERTURB_META 新增 grid 字段 = PERTURB_GRIDS;单点默认参数均在网格内。"""
    assert set(PERTURB_GRIDS) == set(PERTURB_TYPES)
    defaults = {"blur": 3, "mosaic": 8, "occlude": 0.25, "jpeg": 30}
    for kind in PERTURB_TYPES:
        assert PERTURB_META[kind]["grid"] == PERTURB_GRIDS[kind]
        assert defaults[kind] in PERTURB_GRIDS[kind]
        assert PERTURB_META[kind]["param_name"]
    # ε(强度)单调性:网格按强度递增排列(jpeg 的 quality 递减 = 强度递增)
    for kind in PERTURB_TYPES:
        params = list(PERTURB_GRIDS[kind])
        eps = [1.0 - p / 100.0 if kind == "jpeg" else float(p) for p in params]
        assert eps == sorted(eps), f"{kind} 网格应按强度递增:{eps}"


def test_grid_default_off_keeps_payload_without_curves(tmp_path: Path) -> None:
    """grid=False(默认)payload / 落盘 json / markdown 均无 curves 曲线段;
    grid=True 仅新增 "curves" 键,既有 stats 逐字段不变。"""
    corpus = _make_grid_corpus(tmp_path)
    plain = run(corpus, tmp_path / "plain_out")
    assert "curves" not in plain
    reloaded = json.loads(
        (tmp_path / "plain_out" / "adversarial_report.json").read_text(encoding="utf-8")
    )
    assert "curves" not in reloaded
    assert "感知扰动预算曲线" not in (
        tmp_path / "plain_out" / "adversarial_report.md"
    ).read_text(encoding="utf-8")

    gridded = run(corpus, tmp_path / "grid_out", grid=True)
    assert set(gridded) - set(plain) == {"curves"}
    assert gridded["curves"]["mode"] == "grid"
    for row_new, row_old in zip(gridded["stats"], plain["stats"]):
        assert row_new == row_old  # 单点 stats 与曲线互不污染


# ---------------------------------------------------------------------------
# SSIM:恒等 / 闭式手算 / 双窗均值 / 低相似 / 小图 / 非法输入 / PIL 缺席
# ---------------------------------------------------------------------------


def _gray_png(path: Path, value: int, width: int = 8, height: int = 8) -> Path:
    """落盘一张常量灰度 PNG(闭式 SSIM 手算对照用)。"""
    PILImage.new("L", (width, height), value).save(path, format="PNG")
    return path


def test_compute_ssim_identical_images_equal_one(tmp_path: Path) -> None:
    """恒等输入 → SSIM 恰为 1.0(浮点上分子分母逐项相等,无容差漂移)。"""
    src = _checkerboard_png(tmp_path / "nsfw_hi_001.png")
    assert compute_ssim(src, src) == pytest.approx(1.0, abs=1e-15)
    same_bytes = tmp_path / "copy.png"
    same_bytes.write_bytes(src.read_bytes())
    assert compute_ssim(src, same_bytes) == pytest.approx(1.0, abs=1e-15)
    # 小于窗口的图(4x4):窗口收缩到 4,恒等仍为 1.0
    tiny_a = _gray_png(tmp_path / "tiny_a.png", 77, width=4, height=4)
    tiny_b = _gray_png(tmp_path / "tiny_b.png", 77, width=4, height=4)
    assert compute_ssim(tiny_a, tiny_b) == pytest.approx(1.0, abs=1e-15)


def test_compute_ssim_constant_window_closed_formula(tmp_path: Path) -> None:
    """8x8 常量窗手算:var=cov=0 时 SSIM 闭式退化
    = (2·μx·μy + C1)/(μx² + μy² + C1)(C1 = (0.01·255)²,C2 消去)。"""
    left = _gray_png(tmp_path / "a.png", 100)
    right = _gray_png(tmp_path / "b.png", 110)
    c1 = (0.01 * 255.0) ** 2
    expected = (2 * 100 * 110 + c1) / (100**2 + 110**2 + c1)
    assert compute_ssim(left, right) == pytest.approx(expected, abs=1e-12)


def test_compute_ssim_two_windows_averaged(tmp_path: Path) -> None:
    """16x8 双窗手算:左半恒等窗 = 1.0,右半常量窗 = 闭式值,全图 = 二者均值。"""
    left = PILImage.new("L", (16, 8), 100)
    right = PILImage.new("L", (16, 8), 100)
    for x in range(8, 16):
        for y in range(8):
            right.putpixel((x, y), 110)
    path_l, path_r = tmp_path / "l.png", tmp_path / "r.png"
    left.save(path_l, format="PNG")
    right.save(path_r, format="PNG")

    c1 = (0.01 * 255.0) ** 2
    shifted = (2 * 100 * 110 + c1) / (100**2 + 110**2 + c1)
    assert compute_ssim(path_l, path_r) == pytest.approx((1.0 + shifted) / 2.0, abs=1e-12)


def test_compute_ssim_low_similarity_below_threshold(tmp_path: Path) -> None:
    """构造低相似对(高频棋盘 vs 平坦灰)→ SSIM < 0.05,远离 1.0。"""
    board = _checkerboard_png(tmp_path / "board.png", width=64, height=64, cell=4)
    flat = tmp_path / "flat.png"
    PILImage.new("RGB", (64, 64), (128, 128, 128)).save(flat, format="PNG")
    value = compute_ssim(board, flat)
    assert value < 0.05
    assert 0.0 <= value <= 1.0


def test_compute_ssim_rejects_bad_inputs_and_missing_pil(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """尺寸不一致 → ValueError(中文);PIL 缺席 → AdversarialError(中文)。"""
    left = _gray_png(tmp_path / "a.png", 100, width=8, height=8)
    right = _gray_png(tmp_path / "b.png", 100, width=16, height=8)
    with pytest.raises(ValueError, match="尺寸不一致"):
        compute_ssim(left, right)
    with pytest.raises(ValueError, match="窗口边长"):
        compute_ssim(left, right, win=0)

    monkeypatch.setitem(sys.modules, "PIL", None)
    with pytest.raises(AdversarialError, match="Pillow"):
        compute_ssim(left, left)


# ---------------------------------------------------------------------------
# 网格变体参数化:参数确实生效、文件名保留 stem、未知类型拒绝
# ---------------------------------------------------------------------------


def test_grid_variant_params_take_effect_and_keep_stem(tmp_path: Path) -> None:
    """强弱两档参数产出不同字节且强档 SSIM 更低;文件名保留原 stem(stub 不断链)。"""
    from benchmarks import adversarial as adv

    src = _checkerboard_png(tmp_path / "nsfw_hi_001.png")
    cases = [
        ("blur", 1, 8, "nsfw_hi_001_blur_1.png", "nsfw_hi_001_blur_8.png"),
        ("mosaic", 4, 24, "nsfw_hi_001_mosaic_4.png", "nsfw_hi_001_mosaic_24.png"),
        ("occlude", 0.10, 0.60, "nsfw_hi_001_occlude_10.png", "nsfw_hi_001_occlude_60.png"),
        ("jpeg", 60, 10, "nsfw_hi_001_jpeg_60.jpg", "nsfw_hi_001_jpeg_10.jpg"),
    ]
    for kind, weak_p, strong_p, weak_name, strong_name in cases:
        weak = adv._perturb_grid_variant(src, tmp_path / "v", kind, weak_p)
        strong = adv._perturb_grid_variant(src, tmp_path / "v", kind, strong_p)
        assert weak.name == weak_name and strong.name == strong_name
        assert weak.read_bytes() != strong.read_bytes()
        with PILImage.open(str(strong)) as opened:  # 保持原始宽高
            assert opened.size == (160, 128)
        # 感知强度单调:更强扰动 → 更低 SSIM
        assert compute_ssim(src, strong) < compute_ssim(src, weak)


def test_grid_variant_rejects_unknown_kind(tmp_path: Path) -> None:
    from benchmarks import adversarial as adv

    src = _checkerboard_png(tmp_path / "normal_001.png")
    with pytest.raises(ValueError, match="未知扰动类型"):
        adv._perturb_grid_variant(src, tmp_path / "v", "posterize", 3)
    with pytest.raises(AdversarialError, match="写出失败"):
        adv._perturb_grid_variant(src, tmp_path / "v", "blur", -1)  # 越界参数


# ---------------------------------------------------------------------------
# robustness_auc / min_effective_attack:函数级手算对照与非法输入
# ---------------------------------------------------------------------------


def test_robustness_auc_hand_values_and_validation() -> None:
    """梯形归一化面积手算:([1,2,3],[.1,.3,.5]) → (0.2+0.4)/2 = 0.3。"""
    assert robustness_auc([1.0, 2.0, 3.0], [0.1, 0.3, 0.5]) == pytest.approx(0.3)
    # 非均匀 ε:([0,5,10],[0,1,1]) → (0.5·5 + 1·5)/10 = 0.75
    assert robustness_auc([0.0, 5.0, 10.0], [0.0, 1.0, 1.0]) == pytest.approx(0.75)
    # 单点无法构成区间 → 0;带符号降幅可抵减面积
    assert robustness_auc([2.0], [0.9]) == 0.0
    assert robustness_auc([0.0, 1.0], [-0.4, 0.6]) == pytest.approx(0.1)
    with pytest.raises(ValueError, match="等长且非空"):
        robustness_auc([1.0, 2.0], [0.1])
    with pytest.raises(ValueError, match="严格递增"):
        robustness_auc([2.0, 2.0], [0.1, 0.2])
    with pytest.raises(ValueError, match="严格递增"):
        robustness_auc([3.0, 2.0], [0.1, 0.2])


def test_min_effective_attack_scan_and_strict_boundary() -> None:
    """ε 升序扫描取首个 |降幅|>0.2;恰等于阈值不计入(严格大于);无则 None。"""
    points = [
        {"param": 8, "eps": 8.0, "avg_drop": 0.40, "avg_ssim": 0.02},
        {"param": 1, "eps": 1.0, "avg_drop": 0.05, "avg_ssim": 0.55},
        {"param": 5, "eps": 5.0, "avg_drop": 0.25, "avg_ssim": 0.03},
        {"param": 3, "eps": 3.0, "avg_drop": 0.15, "avg_ssim": 0.20},
    ]
    attack = min_effective_attack(points)
    assert attack == {"param": 5, "eps": 5.0, "ssim": 0.03, "avg_drop": 0.25}
    # 乱序输入同样按 ε 升序取**最小**有效 ε(而非列表首个)
    assert min_effective_attack(list(reversed(points)))["eps"] == 5.0
    # 恰等于阈值(0.2)不触发;负向大幅掉分(|drop| 口径)触发
    boundary = [
        {"param": 60, "eps": 0.4, "avg_drop": 0.2, "avg_ssim": 0.97},
        {"param": 40, "eps": 0.6, "avg_drop": 0.3, "avg_ssim": 0.96},
    ]
    assert min_effective_attack(boundary)["param"] == 40
    negative = [{"param": 2, "eps": 2.0, "avg_drop": -0.7, "avg_ssim": 0.5}]
    assert min_effective_attack(negative)["avg_drop"] == -0.7
    # 全部低于阈值 / 空输入 → None
    assert min_effective_attack(boundary[:1]) is None
    assert min_effective_attack([]) is None


# ---------------------------------------------------------------------------
# run(grid=True):单调曲线 / AUC / min_effective / 变体三元组 / 确定性 / 正交
# ---------------------------------------------------------------------------


def test_grid_curves_monotone_auc_and_min_effective(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """确定性单调分类器:降幅随 ε 单调不减;四类 AUC / min_effective 与
    手算逐项一致(含 jpeg q=60 恰 0.2 的严格大于边界);变体附三元组。"""
    from benchmarks import adversarial as adv

    corpus = _make_grid_corpus(tmp_path)
    monkeypatch.setattr(
        adv, "_build_classifier", lambda name, cfg: _SimGridDropClassifier()
    )
    payload = run(corpus, tmp_path / "out", grid=True)
    curves = payload["curves"]

    assert sorted(curves["kinds"]) == sorted(PERTURB_TYPES)
    assert curves["ssim"]["window"] == 8
    assert "正交" in curves["note"]  # docstring 注明的门禁正交声明落在 payload
    for kind in PERTURB_TYPES:
        expect = _GRID_EXPECT[kind]
        curve = curves["kinds"][kind]
        points = curve["points"]
        assert curve["grid"] == list(expect["params"])
        assert [p["param"] for p in points] == list(expect["params"])
        assert [p["eps"] for p in points] == list(expect["eps"])
        # 单调性:平均降幅随 ε 单调不减(分类器构造的确定性响应)
        drops = [p["avg_drop"] for p in points]
        assert drops == list(expect["drops"])
        assert all(drops[i + 1] >= drops[i] - 1e-9 for i in range(len(drops) - 1))
        # AUC 与 min_effective 手算对照
        assert curve["auc"] == pytest.approx(float(expect["auc"]), abs=1e-6)
        attack = curve["min_effective_attack"]
        assert attack is not None and attack["param"] == expect["mea_param"]
        assert attack["eps"] == points[list(expect["params"]).index(attack["param"])]["eps"]
        assert attack["ssim"] == points[list(expect["params"]).index(attack["param"])]["avg_ssim"]
        # score-vs-SSIM 曲线:同一点列按 SSIM 降序的 [SSIM, 平均分] 对
        by_ssim = sorted(points, key=lambda p: p["avg_ssim"], reverse=True)
        assert curve["score_vs_ssim"] == [[p["avg_ssim"], p["avg_score"]] for p in by_ssim]
        # 每变体附 (perturb_kind, param, ssim) 三元组(+ 文件名 / 分 / 降幅)
        for point in points:
            assert point["n"] == 3
            assert len(point["variants"]) == 3
            for variant in point["variants"]:
                assert variant["perturb_kind"] == kind
                assert variant["param"] == point["param"]
                assert 0.0 <= variant["ssim"] <= 1.0
        # 感知强度方向:更强扰动 → 更低平均 SSIM(occlude/jpeg 全程严格递减)
        ssims = [p["avg_ssim"] for p in points]
        if kind in ("occlude", "jpeg"):
            assert ssims == sorted(ssims, reverse=True)
        else:  # blur/mosaic 在极强档趋于饱和,只断言首弱档 > 末强档
            assert ssims[0] > ssims[-1]
        # 单点 stats 不受曲线影响(单点变体不含 _kind_ 标记 → 掉分恒 0)
        single = {row["type"]: row for row in payload["stats"]}
        assert single[kind]["avg_drop"] == pytest.approx(0.0, abs=1e-9)


def test_grid_curves_stub_all_zero(tmp_path: Path) -> None:
    """stub 免疫在网格上同样成立:四类曲线降幅恒 0、AUC=0、无显著掉分。"""
    corpus = _make_grid_corpus(tmp_path)
    payload = run(corpus, tmp_path / "out", grid=True)
    for kind in PERTURB_TYPES:
        curve = payload["curves"]["kinds"][kind]
        assert all(point["avg_drop"] == pytest.approx(0.0, abs=1e-9) for point in curve["points"])
        assert all(point["avg_score"] == pytest.approx(0.02) for point in curve["points"])
        assert curve["auc"] == pytest.approx(0.0, abs=1e-12)
        assert curve["min_effective_attack"] is None


def test_grid_curves_deterministic_across_runs(tmp_path: Path) -> None:
    """同一语料两次 grid 运行:curves 段逐位一致(零随机源)。"""
    corpus = _make_grid_corpus(tmp_path)
    first = run(corpus, tmp_path / "o1", grid=True)
    second = run(corpus, tmp_path / "o2", grid=True)
    assert first["curves"] == second["curves"]


def test_grid_mode_orthogonal_to_golden_gate(tmp_path: Path) -> None:
    """网格与门禁正交:单点建金标 → grid+gate 复跑通过、指纹与指标数不变。"""
    corpus = _make_grid_corpus(tmp_path)
    golden = tmp_path / "adversarial_golden.json"
    baseline = run(corpus, tmp_path / "o1", update_golden=True, golden_path=golden)

    gridded = run(corpus, tmp_path / "o2", grid=True, gate=True, golden_path=golden)
    assert gridded["gate"]["mode"] == "check"
    assert gridded["gate"]["status"] == "pass"
    assert gridded["gate"]["violations"] == []
    # 指纹只覆盖单点默认参数:grid 开关不换键;门禁指标数恒 4×3
    assert gridded["gate"]["fingerprint"] == baseline["gate"]["fingerprint"]
    assert gridded["gate"]["metrics_total"] == len(PERTURB_TYPES) * len(GATE_METRICS) == 12
    assert gridded["gate"]["tolerance_rule"] == baseline["gate"]["tolerance_rule"]


def test_grid_mode_pil_missing_raises_chinese(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PIL 缺席时 grid 模式与单点路径同样中文报错(不产出无效报告)。"""
    corpus = _make_grid_corpus(tmp_path)
    monkeypatch.setitem(sys.modules, "PIL", None)
    with pytest.raises(AdversarialError, match="Pillow"):
        run(corpus, tmp_path / "out", grid=True)
    assert not (tmp_path / "out" / "adversarial_report.json").exists()


def test_grid_markdown_renders_curve_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """markdown 新增曲线表:ε/平均分/降幅/SSIM 列、AUC 与首个显著掉分行。"""
    from benchmarks import adversarial as adv

    corpus = _make_grid_corpus(tmp_path)
    monkeypatch.setattr(
        adv, "_build_classifier", lambda name, cfg: _SimGridDropClassifier()
    )
    run(corpus, tmp_path / "out", grid=True)
    markdown = (tmp_path / "out" / "adversarial_report.md").read_text(encoding="utf-8")
    assert "## 感知扰动预算曲线(ε 网格 · grid 模式)" in markdown
    assert "| 参数 | ε(强度) | 平均分 | 平均降幅 | 平均 SSIM |" in markdown
    assert "鲁棒 AUC = 0.2250" in markdown
    assert "首个显著掉分:ε = 5.0000(radius = 5" in markdown
    assert "正交" in markdown and "SSIM" in markdown
    for kind in PERTURB_TYPES:
        assert PERTURB_META[kind]["label"] in markdown
    # 单点报告(无 curves)不渲染该段 —— 上文 default_off 测试已覆盖


def test_cli_grid_flag_prints_curve_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI --grid:退出码 0,stdout 打印四类曲线摘要(类型/AUC/首个显著掉分)。"""
    corpus = _make_grid_corpus(tmp_path)
    code = main(["--corpus", str(corpus), "--out", str(tmp_path / "out"), "--grid"])
    assert code == 0
    out = capsys.readouterr().out
    assert "感知扰动预算曲线(grid 模式)" in out
    for kind in PERTURB_TYPES:
        assert kind in out
    assert "鲁棒AUC=0.0000" in out and "网格内无显著掉分" in out  # stub 免疫
    reloaded = json.loads(
        (tmp_path / "out" / "adversarial_report.json").read_text(encoding="utf-8")
    )
    assert reloaded["curves"]["mode"] == "grid"


# ===========================================================================
# V13 感知曲线独立金标门禁:往返 / 容差 / None↔数值违例 / 指纹独立 / 坏文件
# (与单点金标两文件、两指纹、两段输出,互不干扰;全程 tmp_path)
# ===========================================================================


def _curve_cli_args(
    corpus: Path, out: Path, curve_golden: Path, *extra: str
) -> list[str]:
    """曲线金标门禁 CLI 参数拼装(--curve-golden 固定项 + 余项)。"""
    return ["--corpus", str(corpus), "--out", str(out), "--curve-golden", str(curve_golden), *extra]


def test_curve_golden_update_then_gate_roundtrip_passes(tmp_path: Path) -> None:
    """--update-curve-golden → --curve-gate 往返:重建 → 同指纹复跑通过。"""
    from benchmarks import adversarial as adv

    corpus = _make_grid_corpus(tmp_path)
    golden = tmp_path / "adversarial_curve_golden.json"
    assert not golden.exists()

    # 1) run() API:显式重建曲线金标(grid 未开 → 自动附带网格评测)
    first = run(corpus, tmp_path / "o1", update_curve_golden=True, curve_golden_path=golden)
    assert "curves" in first  # 曲线金标的比对对象是曲线 → 自动附带 ε 网格
    gate = first["curve_gate"]
    assert gate["mode"] == "update" and gate["status"] == "updated"
    assert gate["metrics_recorded"] == len(PERTURB_TYPES) * len(CURVE_GATE_METRICS) == 12
    assert gate["tolerance_rule"] == adv._CURVE_TOL_RULE_TEXT
    data = json.loads(golden.read_text(encoding="utf-8"))
    assert data["_schema"] == CURVE_GOLDEN_SCHEMA
    assert data["_stats_version"] == CURVE_GOLDEN_STATS_VERSION
    assert data["_basis"]["metrics"] == list(CURVE_GATE_METRICS)
    assert data["_basis"]["grid"] == {k: list(PERTURB_GRIDS[k]) for k in PERTURB_TYPES}
    fps = _golden_fingerprint_keys(data)
    assert fps == [gate["fingerprint"]]
    entry = data[fps[0]]
    assert sorted(entry) == sorted(
        f"{kind}/{metric}" for kind in PERTURB_TYPES for metric in CURVE_GATE_METRICS
    )
    # stub 免疫:AUC 基线 0(容差 = max(10%·0, 0.05) = 0.05);min_effective 未跌破;
    # shape_digest = 全平坦段("0"×(网格点数−1),严格 0 容差)
    for kind in PERTURB_TYPES:
        assert entry[f"{kind}/auc"] == {"value": 0.0, "tol": 0.05}
        assert entry[f"{kind}/min_effective_eps"] == {"value": None, "tol": None}
        assert entry[f"{kind}/shape_digest"] == {
            "value": "0" * (len(PERTURB_GRIDS[kind]) - 1),
            "tol": CURVE_SHAPE_FLIP_TOL,
        }
        assert CURVE_SHAPE_FLIP_TOL == 0  # 默认严格:任何一段翻转都拦截

    # 2) 同指纹复跑(stub 确定性)→ 曲线门禁通过
    second = run(corpus, tmp_path / "o2", grid=True, curve_gate=True, curve_golden_path=golden)
    assert second["curve_gate"]["mode"] == "check"
    assert second["curve_gate"]["status"] == "pass"
    assert second["curve_gate"]["violations"] == []
    assert second["curve_gate"]["metrics_total"] == 12
    assert second["curve_gate"]["fingerprint"] == fps[0]

    # 3) CLI 往返:重建 → 门禁通过(退出码 0)
    rc = main(_curve_cli_args(corpus, tmp_path / "o3", golden, "--update-curve-golden"))
    assert rc == 0
    rc = main(_curve_cli_args(corpus, tmp_path / "o4", golden, "--curve-gate"))
    assert rc == 0


def test_curve_golden_cli_prints_update_diff_and_pass_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 摘要:首次重建"新增曲线基线",再次重建"持平",门禁通过措辞齐全。"""
    corpus = _make_grid_corpus(tmp_path)
    golden = tmp_path / "adversarial_curve_golden.json"

    assert main(_curve_cli_args(corpus, tmp_path / "o1", golden, "--update-curve-golden")) == 0
    out = capsys.readouterr().out
    assert "曲线金标已重建" in out and "新增曲线基线" in out
    assert "max(10%·|基线|, 0.05)" in out and "None↔数值互变即违例" in out

    assert main(_curve_cli_args(corpus, tmp_path / "o2", golden, "--update-curve-golden")) == 0
    out = capsys.readouterr().out
    assert "曲线基线刷新" in out and "12 项持平" in out

    assert main(_curve_cli_args(corpus, tmp_path / "o3", golden, "--curve-gate")) == 0
    out = capsys.readouterr().out
    assert "曲线金标门禁通过" in out and "12 项曲线指标全部在容差内" in out


def test_curve_golden_expected_values_and_tolerances_with_sim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模拟掉分分类器:曲线金标基线值与容差逐项手算对照
    (AUC / min_effective ε 来自 _GRID_EXPECT;容差 = max(10%·|基线|, 0.05))。"""
    from benchmarks import adversarial as adv

    corpus = _make_grid_corpus(tmp_path)
    monkeypatch.setattr(
        adv, "_build_classifier", lambda name, cfg: _SimGridDropClassifier()
    )
    payload = run(
        corpus, tmp_path / "out", update_curve_golden=True,
        curve_golden_path=tmp_path / "g.json",
    )
    entry = build_curve_golden_entry(payload["curves"])
    # 期望 ε:blur/mosaic 为参数原值,occlude 为面积占比,jpeg 为 1 − q/100
    expected_mea = {"blur": 5.0, "mosaic": 8.0, "occlude": 0.25, "jpeg": 0.6}
    # 期望形状指纹:四类公式都随 ε 单调不减 → 全 "1"(V13-2 第三指标)
    expected_digest = {"blur": "1111", "mosaic": "111", "occlude": "111", "jpeg": "1111"}
    for kind in PERTURB_TYPES:
        auc = float(_GRID_EXPECT[kind]["auc"])
        assert entry[f"{kind}/auc"]["value"] == pytest.approx(auc, abs=1e-6)
        assert entry[f"{kind}/auc"]["tol"] == pytest.approx(
            curve_metric_tolerance(auc), abs=1e-9
        )
        eps = expected_mea[kind]
        assert entry[f"{kind}/min_effective_eps"]["value"] == pytest.approx(eps, abs=1e-9)
        assert entry[f"{kind}/min_effective_eps"]["tol"] == pytest.approx(
            curve_metric_tolerance(eps), abs=1e-9
        )
        assert entry[f"{kind}/shape_digest"] == {
            "value": expected_digest[kind], "tol": CURVE_SHAPE_FLIP_TOL,
        }
        # payload 的 curves 段与金标条目同源(指纹由同一点列计算)
        assert payload["curves"]["kinds"][kind]["shape_digest"] == expected_digest[kind]
    # 容差公式两分支:10%·|基线| 支配大基线(blur ε=5 → 0.5),0.05 下限
    # 支配小基线(occlude ε=0.25 → 0.05;jpeg ε=0.6 → 0.06 恰过下限)
    assert curve_metric_tolerance(5.0) == pytest.approx(0.5)
    assert curve_metric_tolerance(0.25) == pytest.approx(0.05)
    assert curve_metric_tolerance(0.6) == pytest.approx(0.06)
    assert curve_metric_tolerance(0.0) == pytest.approx(CURVE_TOL_MIN)
    assert CURVE_TOL_VALUE_RATIO == 0.10 and CURVE_TOL_MIN == 0.05
    with pytest.raises(ValueError, match="有限数值"):
        curve_metric_tolerance(float("nan"))


def test_curve_gate_detects_regression_violations_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """构造回归:stub 建曲线基线 → 模拟掉分分类器(注册名不变,指纹不变)
    → 四类 AUC 偏移超容差 + min_effective None→数值 → 违例 + 退出码 2。"""
    from benchmarks import adversarial as adv

    corpus = _make_grid_corpus(tmp_path)
    golden = tmp_path / "adversarial_curve_golden.json"
    assert main(_curve_cli_args(corpus, tmp_path / "o1", golden, "--update-curve-golden")) == 0
    capsys.readouterr()

    monkeypatch.setattr(
        adv, "_build_classifier", lambda name, cfg: _SimGridDropClassifier()
    )
    rc = main(_curve_cli_args(corpus, tmp_path / "o2", golden, "--curve-gate"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "曲线金标违例" in err and "退出码 2" in err

    payload = run(
        corpus, tmp_path / "o3", grid=True, curve_gate=True, curve_golden_path=golden
    )
    gate = payload["curve_gate"]
    assert gate["status"] == "violations"
    by_metric = {v["metric"]: v for v in gate["violations"]}
    # stub 全零基线(AUC 容差 0.05)→ 任何真实掉分曲线都拦截
    blur_auc = by_metric["blur/auc"]
    assert blur_auc["kind"] == "metric_violation"
    assert blur_auc["baseline"] == pytest.approx(0.0)
    assert blur_auc["current"] == pytest.approx(0.225)
    assert blur_auc["tol"] == pytest.approx(0.05)
    assert blur_auc["delta"] == pytest.approx(0.225)
    # min_effective:基线 None → 当前数值 = null_flip 违例(下降方向)
    blur_mea = by_metric["blur/min_effective_eps"]
    assert blur_mea["kind"] == "null_flip"
    assert blur_mea["baseline"] is None and blur_mea["current"] == pytest.approx(5.0)
    assert "None↔数值互变即违例" in blur_mea["message"]
    assert "鲁棒性下降方向" in blur_mea["message"]
    # shape_digest:stub 基线全 "0" → sim 单调曲线全 "1" → 斜率符号全翻转违例
    blur_shape = by_metric["blur/shape_digest"]
    assert blur_shape["kind"] == "shape_changed"
    assert blur_shape["baseline"] == "0000" and blur_shape["current"] == "1111"
    assert blur_shape["tol"] == 0 and blur_shape["delta"] == 4
    assert "斜率符号翻转" in blur_shape["message"]
    # 四类全部:AUC 违例 ×4 + null_flip ×4 + shape_changed ×4 = 12 项;
    # 每条 message 四要素齐全
    assert len(gate["violations"]) == 12
    for violation in gate["violations"]:
        assert violation["perturb"] in PERTURB_TYPES
        assert "基线" in violation["message"] and "当前" in violation["message"]


def _kinds_payload(
    auc: float, mea_eps: float | None, shape_digest: str = "1111"
) -> dict:
    """手造单类(blur)curves.kinds 载荷(evaluate_curve_gate 单元测试用)。"""
    attack = (
        None if mea_eps is None
        else {"param": mea_eps, "eps": mea_eps, "avg_drop": 0.3}
    )
    return {
        "blur": {
            "label": "高斯模糊",
            "auc": auc,
            "min_effective_attack": attack,
            "shape_digest": shape_digest,
        }
    }


def test_evaluate_curve_gate_unit_semantics() -> None:
    """evaluate_curve_gate 单元:边界 / None↔数值双向 / 同 None 通过 /
    数值容差比对 / 缺指标键违例 / 形状指纹离散容差(与 evaluate_gate 同形态)。"""
    # (a) AUC 数值比对:delta == tol 不违例(边界),超出违例;
    # shape_digest 精确匹配(载荷 "1111" == 基线 "1111")一并通过
    entry = {"blur/auc": {"value": 0.2, "tol": 0.05},
             "blur/min_effective_eps": {"value": None, "tol": None},
             "blur/shape_digest": {"value": "1111", "tol": 0}}
    assert evaluate_curve_gate(_kinds_payload(0.25, None), entry) == []  # 0.05 == tol
    violated = evaluate_curve_gate(_kinds_payload(0.26, None), entry)
    assert [v["kind"] for v in violated] == ["metric_violation"]
    assert violated[0]["metric"] == "blur/auc"
    assert "容差" in violated[0]["message"]

    # (b) None↔数值互变即违例:基线 None → 当前数值(下降方向)
    flipped = evaluate_curve_gate(_kinds_payload(0.2, 5.0), entry)
    assert [v["kind"] for v in flipped] == ["null_flip"]
    assert flipped[0]["baseline"] is None and flipped[0]["current"] == pytest.approx(5.0)
    assert "鲁棒性下降方向" in flipped[0]["message"]

    # (c) 反向:基线数值 → 当前 None(变好也须人工复核,同样拦截)
    entry_rev = {"blur/auc": {"value": 0.2, "tol": 0.05},
                 "blur/min_effective_eps": {"value": 5.0, "tol": 0.5},
                 "blur/shape_digest": {"value": "1111", "tol": 0}}
    rev = evaluate_curve_gate(_kinds_payload(0.2, None), entry_rev)
    assert [v["kind"] for v in rev] == ["null_flip"]
    assert rev[0]["baseline"] == pytest.approx(5.0) and rev[0]["current"] is None
    assert "人工复核" in rev[0]["message"]

    # (d) 两侧同为数值:容差内通过(delta 0.4 ≤ tol 0.5),超出违例
    assert evaluate_curve_gate(_kinds_payload(0.2, 5.4), entry_rev) == []
    beyond = evaluate_curve_gate(_kinds_payload(0.2, 5.6), entry_rev)
    assert [v["kind"] for v in beyond] == ["metric_violation"]
    assert beyond[0]["metric"] == "blur/min_effective_eps"
    assert beyond[0]["delta"] == pytest.approx(0.6)

    # (e) 缺指标键 → missing_metric(fail-safe 方向)
    partial = {"blur/auc": {"value": 0.2, "tol": 0.05},
               "blur/shape_digest": {"value": "1111", "tol": 0}}
    missing = evaluate_curve_gate(_kinds_payload(0.2, None), partial)
    assert [v["kind"] for v in missing] == ["missing_metric"]
    assert missing[0]["baseline"] is None and missing[0]["tol"] is None
    assert "--update-curve-golden" in missing[0]["message"]

    # (f) 形状指纹离散容差:基线 "1202" vs 当前 "1212"(1 段翻转)——
    # tol=0 严格 → shape_changed;tol=1 → 放行;当前 "1211"(2 段翻转)
    # 在 tol=1 下仍拦截;载荷缺 shape_digest 字段 → missing_metric
    def _shape_entry(tol: int) -> dict:
        return {
            "blur/auc": {"value": 0.2, "tol": 0.05},
            "blur/min_effective_eps": {"value": None, "tol": None},
            "blur/shape_digest": {"value": "1202", "tol": tol},
        }

    strict = evaluate_curve_gate(_kinds_payload(0.2, None, "1212"), _shape_entry(0))
    assert [v["kind"] for v in strict] == ["shape_changed"]
    assert strict[0]["baseline"] == "1202" and strict[0]["current"] == "1212"
    assert strict[0]["tol"] == 0 and strict[0]["delta"] == 1
    assert "斜率符号翻转" in strict[0]["message"] and "容差" in strict[0]["message"]
    assert evaluate_curve_gate(_kinds_payload(0.2, None, "1212"), _shape_entry(1)) == []
    assert evaluate_curve_gate(_kinds_payload(0.2, None, "1202"), _shape_entry(1)) == []
    two_flips = evaluate_curve_gate(_kinds_payload(0.2, None, "1211"), _shape_entry(1))
    assert [v["kind"] for v in two_flips] == ["shape_changed"]
    assert two_flips[0]["delta"] == 2 and two_flips[0]["tol"] == 1
    # 载荷缺 shape_digest 字段(统计版本过旧)→ 缺指标违例(当前侧缺失)
    legacy_payload = {"blur": {
        "label": "高斯模糊", "auc": 0.2, "min_effective_attack": None,
    }}
    absent = evaluate_curve_gate(legacy_payload, _shape_entry(0))
    assert [v["kind"] for v in absent] == ["missing_metric"]
    assert absent[0]["current"] is None

    # (g) 违例项形态与 evaluate_gate 一致(五要素键齐全)
    for violation in flipped + rev + beyond + missing + strict + two_flips:
        assert set(violation) == {
            "kind", "metric", "perturb", "metric_name",
            "baseline", "current", "tol", "delta", "message",
        }


def test_curve_fingerprint_independent_and_sensitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """曲线指纹 = 单点指纹 + 网格 + 曲线版本:独立于单点键,且对网格 /
    曲线版本敏感(单点指纹不受这两者牵连)。"""
    from benchmarks import adversarial as adv

    corpus = _make_grid_corpus(tmp_path)
    single = compute_fingerprint("stub", corpus)
    curve = compute_curve_fingerprint("stub", corpus)
    assert len(curve) == 64 and curve != single  # 独立键
    assert compute_curve_fingerprint("stub", corpus) == curve  # 确定性
    assert compute_curve_fingerprint("nudenet", corpus) != curve  # 分类器换键

    # 网格变化 → 曲线键换;单点键不动(网格不进单点指纹)
    with monkeypatch.context() as m:
        m.setitem(adv.PERTURB_GRIDS, "blur", (1, 2, 3, 5, 8, 12))
        assert compute_curve_fingerprint("stub", corpus) != curve
        assert compute_fingerprint("stub", corpus) == single

    # 曲线统计版本变化 → 曲线键换;单点键不动(两版本语义互不牵连)
    with monkeypatch.context() as m:
        m.setattr(adv, "CURVE_GOLDEN_STATS_VERSION", "v99-test")
        assert compute_curve_fingerprint("stub", corpus) != curve
        assert compute_fingerprint("stub", corpus) == single


def test_curve_and_single_golden_files_isolated(tmp_path: Path) -> None:
    """两金标文件字节级互不触碰:单点重建不动曲线文件,曲线重建不动单点
    文件;双门禁并存通过(退出码 0)。"""
    corpus = _make_grid_corpus(tmp_path)
    single_g = tmp_path / "adversarial_golden.json"
    curve_g = tmp_path / "adversarial_curve_golden.json"
    base = [
        "--corpus", str(corpus),
        "--golden", str(single_g), "--curve-golden", str(curve_g),
    ]

    assert main([*base, "--out", str(tmp_path / "o1"), "--update-golden"]) == 0
    single_bytes = single_g.read_bytes()
    assert not curve_g.exists()  # 单点重建不产生曲线文件

    assert main([*base, "--out", str(tmp_path / "o2"), "--update-curve-golden"]) == 0
    assert single_g.read_bytes() == single_bytes  # 单点文件未被触碰
    curve_bytes = curve_g.read_bytes()

    assert main([*base, "--out", str(tmp_path / "o3"), "--update-golden"]) == 0
    assert curve_g.read_bytes() == curve_bytes  # 曲线文件未被触碰

    # 双门禁并存:单点 + 曲线同时通过 → 退出码 0
    rc = main([*base, "--out", str(tmp_path / "o4"), "--gate", "--curve-gate"])
    assert rc == 0


def test_curve_gate_corrupt_file_exit_code_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """曲线金标损坏 / 坏 JSON / 结构非法 / 版本不兼容 → 中文错误 + 退出码 1。"""
    corpus = _make_grid_corpus(tmp_path)
    golden = tmp_path / "adversarial_curve_golden.json"

    golden.write_text("{oops: 不是合法 JSON", encoding="utf-8")
    rc = main(_curve_cli_args(corpus, tmp_path / "o1", golden, "--curve-gate"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "曲线金标文件无法解析" in err and "--update-curve-golden" in err

    # 顶层非对象
    golden.write_text("[1, 2, 3]", encoding="utf-8")
    assert main(_curve_cli_args(corpus, tmp_path / "o2", golden, "--curve-gate")) == 1
    assert "顶层必须是对象" in capsys.readouterr().err

    # value=None 但 tol 为数值(None 无容差概念,tol=None 当且仅当 value=None)
    golden.write_text(
        json.dumps({"a" * 64: {"blur/auc": {"value": None, "tol": 0.05}}}),
        encoding="utf-8",
    )
    assert main(_curve_cli_args(corpus, tmp_path / "o3", golden, "--curve-gate")) == 1
    assert "结构非法" in capsys.readouterr().err

    # shape_digest 结构非法(V13-2):value 须为 '012' 组成的指纹串而非数值,
    # tol 须为非负整数(允许斜率翻转段数)
    golden.write_text(
        json.dumps({"a" * 64: {"blur/shape_digest": {"value": 1202, "tol": 0}}}),
        encoding="utf-8",
    )
    assert main(_curve_cli_args(corpus, tmp_path / "o3b", golden, "--curve-gate")) == 1
    assert "结构非法" in capsys.readouterr().err
    golden.write_text(
        json.dumps({"a" * 64: {"blur/shape_digest": {"value": "1202", "tol": -1}}}),
        encoding="utf-8",
    )
    assert main(_curve_cli_args(corpus, tmp_path / "o3c", golden, "--curve-gate")) == 1
    assert "结构非法" in capsys.readouterr().err

    # 指标表缺失 / 空表
    golden.write_text(json.dumps({"a" * 64: 42}), encoding="utf-8")
    assert main(_curve_cli_args(corpus, tmp_path / "o4", golden, "--curve-gate")) == 1
    assert "结构非法" in capsys.readouterr().err

    # 版本不兼容
    golden.write_text(json.dumps({"_schema": "alien/9"}), encoding="utf-8")
    assert main(_curve_cli_args(corpus, tmp_path / "o5", golden, "--curve-gate")) == 1
    assert "版本不兼容" in capsys.readouterr().err

    # update 模式同样拒绝损坏曲线金标(须显式修复/删除,而非静默覆盖)
    golden.write_text("{broken", encoding="utf-8")
    with pytest.raises(AdversarialGoldenError, match="曲线金标文件无法解析"):
        run(corpus, tmp_path / "o6", update_curve_golden=True, curve_golden_path=golden)
    assert load_curve_golden(tmp_path / "not_exists.json") == {}  # 不存在 → 空金标


def test_curve_gate_default_off_and_auto_enables_grid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """curve_gate 默认关闭 payload 无 "curve_gate" 键;开启时自动附带
    curves(比对对象就是曲线);缺基线默认 warn 跳过(退出码 0)。"""
    corpus = _make_grid_corpus(tmp_path)
    plain = run(corpus, tmp_path / "plain_out")
    assert "curve_gate" not in plain and "curves" not in plain

    gated = run(
        corpus, tmp_path / "gate_out", curve_gate=True,
        curve_golden_path=tmp_path / "g.json",
    )
    assert set(gated) - set(plain) == {"curves", "curve_gate"}
    assert gated["curve_gate"]["mode"] == "check"
    assert gated["curve_gate"]["status"] == "no_baseline"  # 金标文件不存在
    assert any("--update-curve-golden" in w for w in gated["curve_gate"]["warnings"])
    assert "violations" not in gated["curve_gate"]
    for row_new, row_old in zip(gated["stats"], plain["stats"]):
        assert row_new == row_old  # 单点 stats 不受曲线门禁影响

    # CLI:--curve-gate(未开 --grid)缺基线 warn → 中文警告 + 退出码 0
    rc = main(_curve_cli_args(corpus, tmp_path / "o1", tmp_path / "g.json", "--curve-gate"))
    assert rc == 0
    captured = capsys.readouterr()
    assert "缺基线" in captured.err and "曲线金标" in captured.err
    assert "曲线金标门禁:缺基线,已按警告跳过 —— 退出码 0" in captured.out
    reloaded = json.loads(
        (tmp_path / "o1" / "adversarial_report.json").read_text(encoding="utf-8")
    )
    assert "curves" in reloaded and "curve_gate" in reloaded  # 落盘 payload 同源


def test_curve_gate_missing_baseline_fail_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """曲线金标缺基线处置:默认 warn 跳过(金标不被改写);fail 按回归
    违例处理(kind=missing_baseline)→ 退出码 2(与单点门禁共用策略参数)。"""
    corpus = _make_grid_corpus(tmp_path)
    golden = tmp_path / "adversarial_curve_golden.json"
    assert not golden.exists()

    # warn(默认):状态 no_baseline,不写金标文件
    warned = run(
        corpus, tmp_path / "o1", curve_gate=True, curve_golden_path=golden
    )
    assert warned["curve_gate"]["status"] == "no_baseline"
    assert not golden.exists()

    # fail:missing_baseline 违例
    failed = run(
        corpus, tmp_path / "o2", curve_gate=True, curve_golden_path=golden,
        missing_baseline="fail",
    )
    assert failed["curve_gate"]["status"] == "violations"
    violation = failed["curve_gate"]["violations"][0]
    assert violation["kind"] == "missing_baseline"
    assert "--update-curve-golden" in violation["message"]
    # CLI 同口径:退出码 2
    rc = main(_curve_cli_args(corpus, tmp_path / "o3", golden, "--curve-gate",
                              "--missing-baseline", "fail"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "曲线金标违例" in err and "退出码 2" in err


# ===========================================================================
# V13-2 评测增强批:shape_digest 形状指纹 / 版本递增缺基线 / 缺基线策略拆分
# (平移构造场景:保持 AUC 与首越阈点不变、只重排曲线形状 → shape_changed)
# ===========================================================================


def test_curve_shape_digest_function_unit() -> None:
    """curve_shape_digest 函数级手算对照:斜率符号量化(0 平坦/1 升/2 降)、
    平移盲区构造对(AUC 与首越阈点同值、指纹不同)、确定性、非法输入。"""
    eps = [1.0, 2.0, 3.0, 5.0, 8.0]
    assert curve_shape_digest(eps, [0.05, 0.45, 0.15, 0.15, 0.05]) == "1202"
    assert curve_shape_digest(eps, [0.05, 0.45, 0.10, 0.18, 0.05]) == "1212"
    assert curve_shape_digest(eps, [0.05, 0.93, 0.02, 0.036, 0.05]) == "1211"
    # 规范化端点:全零曲线(stub 免疫)全平坦;单调不降曲线(sim 类)全上升
    assert curve_shape_digest(eps, [0.0] * 5) == "0000"
    assert curve_shape_digest(eps, [0.05, 0.10, 0.15, 0.25, 0.40]) == "1111"
    assert curve_shape_digest([0.10, 0.25, 0.40, 0.60], [0.1, 0.25, 0.4, 0.6]) == "111"
    assert curve_shape_digest([2.0], [0.9]) == ""  # 单点无段(理论边界)
    # 确定性:同输入两次调用逐位一致(零随机源)
    assert curve_shape_digest(eps, eps) == curve_shape_digest(eps, eps)

    # 平移盲区构造对的存在性证明:AUC 逐位同值(1.15/7)且首越阈点同为 ε=2,
    # 但逐段斜率符号序列不同——AUC/端点指标对该形状重排全盲
    base = [0.05, 0.45, 0.15, 0.15, 0.05]
    shifted = [0.05, 0.93, 0.02, 0.036, 0.05]
    assert robustness_auc(eps, base) == pytest.approx(1.15 / 7, abs=1e-12)
    assert robustness_auc(eps, shifted) == pytest.approx(1.15 / 7, abs=1e-12)

    def _points(drops: list[float]) -> list[dict]:
        return [
            {"param": i + 1, "eps": e, "avg_drop": d}
            for i, (e, d) in enumerate(zip(eps, drops))
        ]

    assert min_effective_attack(_points(base))["eps"] == pytest.approx(2.0)
    assert min_effective_attack(_points(shifted))["eps"] == pytest.approx(2.0)
    assert curve_shape_digest(eps, base) != curve_shape_digest(eps, shifted)

    # 非法输入:长度失配 / ε 非严格递增 / flat_tol 负数 → ValueError(中文)
    with pytest.raises(ValueError, match="等长且非空"):
        curve_shape_digest([1.0, 2.0], [0.1])
    with pytest.raises(ValueError, match="严格递增"):
        curve_shape_digest([2.0, 2.0], [0.1, 0.2])
    with pytest.raises(ValueError, match="flat_tol"):
        curve_shape_digest(eps, base, flat_tol=-1.0)


#: 平移构造的 blur 网格掉分曲线(ε=[1,2,3,5,8];AUC 系数
#: c=[0.5,1,1.5,2.5,1.5]/7 下三者 Σcᵢdᵢ 恒为 1.15/7,首越阈点同为 ε=2):
#: baseline "1202" / one_flip "1212"(单段翻转)/ two_flip "1211"(两段翻转)。
_BUMP_DROPS: dict[str, dict[int, float]] = {
    "baseline": {1: 0.05, 2: 0.45, 3: 0.15, 5: 0.15, 8: 0.05},
    "one_flip": {1: 0.05, 2: 0.45, 3: 0.10, 5: 0.18, 8: 0.05},
    "two_flip": {1: 0.05, 2: 0.93, 3: 0.02, 5: 0.036, 8: 0.05},
}


class _BumpShiftClassifier:
    """确定性"平移构造"分类器:blur 网格变体按 _BUMP_DROPS[mode] 掉分,
    其余类型与全部单点变体不掉分。

    三种 mode 的 AUC 与 min_effective_eps 完全一致(数值指标盲区),
    唯一差异是曲线形状指纹——shape_changed 违例的单一差异源。
    """

    name = "bumpshift"

    def __init__(self, mode: str = "baseline") -> None:
        self._mode = mode

    def classify(self, img):
        from netsentinel.contracts import ImageScore

        drop = 0.0
        fname = Path(img.path).name
        if "_blur_" in fname:  # 网格变体标记:<stem>_blur_<tag>.png(单点无标记)
            tag = int(fname.rsplit("_blur_", 1)[1].split(".")[0])
            drop = _BUMP_DROPS[self._mode].get(tag, 0.0)
        return ImageScore(image=img, model=self.name, nsfw_prob=1.0 - drop)


def test_curve_shape_changed_detects_shift_with_same_auc_and_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """平移构造场景:AUC 与首越阈点都在容差内、曲线形状重排 →
    kind=shape_changed 检出(严格 0 容差);正常复跑不误报;容差 1 段
    放行单段翻转、拦截两段翻转。"""
    from benchmarks import adversarial as adv

    corpus = _make_grid_corpus(tmp_path)
    golden = tmp_path / "adversarial_curve_golden.json"
    with monkeypatch.context() as m:
        m.setattr(
            adv, "_build_classifier",
            lambda name, cfg: _BumpShiftClassifier("baseline"),
        )
        base = run(
            corpus, tmp_path / "o1", update_curve_golden=True, curve_golden_path=golden
        )
    fp = base["curve_gate"]["fingerprint"]
    entry = json.loads(golden.read_text(encoding="utf-8"))[fp]
    # 基线曲线:blur "1202" + AUC 0.1643 + 首越阈 ε=2;其余三类全零
    assert entry["blur/shape_digest"] == {"value": "1202", "tol": 0}
    assert entry["blur/auc"]["value"] == pytest.approx(0.1643, abs=1e-4)
    assert entry["blur/min_effective_eps"]["value"] == pytest.approx(2.0)
    for kind in ("mosaic", "occlude", "jpeg"):
        assert entry[f"{kind}/shape_digest"]["value"] == "0" * (
            len(PERTURB_GRIDS[kind]) - 1
        )
    assert base["curves"]["kinds"]["blur"]["shape_digest"] == "1202"

    # 正常复跑(同分类器同分布):严格 0 容差下不误报 → 门禁通过
    with monkeypatch.context() as m:
        m.setattr(
            adv, "_build_classifier",
            lambda name, cfg: _BumpShiftClassifier("baseline"),
        )
        same = run(corpus, tmp_path / "o2", curve_gate=True, curve_golden_path=golden)
    assert same["curve_gate"]["status"] == "pass"
    assert same["curve_gate"]["violations"] == []

    # 平移构造(one_flip):AUC / min_effective_eps 均在容差内(数值指标全盲),
    # 形状指纹单段翻转 > 0 → 唯一违例 kind=shape_changed
    with monkeypatch.context() as m:
        m.setattr(
            adv, "_build_classifier",
            lambda name, cfg: _BumpShiftClassifier("one_flip"),
        )
        shifted = run(corpus, tmp_path / "o3", curve_gate=True, curve_golden_path=golden)
    assert shifted["curve_gate"]["status"] == "violations"
    violations = shifted["curve_gate"]["violations"]
    assert len(violations) == 1  # AUC 与首越阈点无任何违例——盲区的直接证据
    violation = violations[0]
    assert violation["kind"] == "shape_changed"
    assert violation["metric"] == "blur/shape_digest"
    assert violation["baseline"] == "1202" and violation["current"] == "1212"
    assert violation["tol"] == 0 and violation["delta"] == 1
    assert "斜率符号翻转" in violation["message"]
    assert shifted["curves"]["kinds"]["blur"]["auc"] == pytest.approx(
        base["curves"]["kinds"]["blur"]["auc"], abs=1e-6
    )
    assert shifted["curves"]["kinds"]["blur"]["min_effective_attack"]["eps"] == (
        pytest.approx(2.0)
    )

    # 离散容差:金标 tol 调到 1 段 → 单段翻转放行、两段翻转仍拦截
    data = json.loads(golden.read_text(encoding="utf-8"))
    data[fp]["blur/shape_digest"]["tol"] = 1
    golden.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    with monkeypatch.context() as m:
        m.setattr(
            adv, "_build_classifier",
            lambda name, cfg: _BumpShiftClassifier("one_flip"),
        )
        tolerated = run(
            corpus, tmp_path / "o4", curve_gate=True, curve_golden_path=golden
        )
    assert tolerated["curve_gate"]["status"] == "pass"  # 1 段翻转 ≤ 容差 1
    with monkeypatch.context() as m:
        m.setattr(
            adv, "_build_classifier",
            lambda name, cfg: _BumpShiftClassifier("two_flip"),
        )
        harsh = run(corpus, tmp_path / "o5", curve_gate=True, curve_golden_path=golden)
    assert harsh["curve_gate"]["status"] == "violations"
    two = harsh["curve_gate"]["violations"][0]
    assert two["kind"] == "shape_changed" and two["delta"] == 2 and two["tol"] == 1


def test_curve_stats_version_bump_invalidates_old_golden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CURVE_GOLDEN_STATS_VERSION 递增(v13-1 旧口径 → v13-2)后:旧版本写入的
    曲线金标指纹不再命中 → 缺基线(warn 默认跳过 / fail 按违例),金标文件
    不被改写;新版本重建后恢复命中。"""
    from benchmarks import adversarial as adv

    assert adv.CURVE_GOLDEN_STATS_VERSION == "v13-2"  # 版本号确实递增
    corpus = _make_grid_corpus(tmp_path)
    golden = tmp_path / "adversarial_curve_golden.json"
    with monkeypatch.context() as m:
        m.setattr(adv, "CURVE_GOLDEN_STATS_VERSION", "v13-1")  # 旧口径
        old = run(
            corpus, tmp_path / "o1", update_curve_golden=True, curve_golden_path=golden
        )
    old_fp = old["curve_gate"]["fingerprint"]
    before = golden.read_text(encoding="utf-8")

    # 当前口径指纹不同 → warn(默认):no_baseline 跳过,金标原样未改写
    warned = run(corpus, tmp_path / "o2", curve_gate=True, curve_golden_path=golden)
    assert warned["curve_gate"]["status"] == "no_baseline"
    assert warned["curve_gate"]["fingerprint"] != old_fp
    assert any("--update-curve-golden" in w for w in warned["curve_gate"]["warnings"])
    assert golden.read_text(encoding="utf-8") == before

    # fail:按违例处理(kind=missing_baseline)
    failed = run(
        corpus, tmp_path / "o3", curve_gate=True, curve_golden_path=golden,
        missing_baseline="fail",
    )
    assert failed["curve_gate"]["status"] == "violations"
    assert failed["curve_gate"]["violations"][0]["kind"] == "missing_baseline"

    # 新口径重建 → 同口径复跑恢复命中(缺基线机制闭环)
    rebuilt = run(
        corpus, tmp_path / "o4", update_curve_golden=True, curve_golden_path=golden
    )
    passed = run(corpus, tmp_path / "o5", curve_gate=True, curve_golden_path=golden)
    assert passed["curve_gate"]["status"] == "pass"
    assert passed["curve_gate"]["fingerprint"] == rebuilt["curve_gate"]["fingerprint"]


def test_curve_missing_baseline_split_policy_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--curve-missing-baseline 拆分:缺省(None)继承单点策略;显式设置则
    单点/曲线独立(策略矩阵 fail×warn 与 warn×fail 各自只拦一侧);CLI 透传。"""
    from benchmarks import adversarial as adv

    corpus = _make_grid_corpus(tmp_path)
    single_g = tmp_path / "single.json"
    curve_g = tmp_path / "curve.json"
    # stub 名下重建两套金标;换注册名 → 两指纹同时缺基线(策略分流的输入)
    run(corpus, tmp_path / "o0", update_golden=True, golden_path=single_g)
    run(corpus, tmp_path / "o0b", update_curve_golden=True, curve_golden_path=curve_g)
    monkeypatch.setattr(adv, "_build_classifier", lambda name, cfg: _SpyClassifier())

    def _both(missing: str, curve_missing: str | None) -> dict:
        return run(
            corpus,
            tmp_path / f"out_{missing}_{curve_missing}",
            classifier="stub_renamed",
            gate=True,
            golden_path=single_g,
            curve_gate=True,
            curve_golden_path=curve_g,
            missing_baseline=missing,
            curve_missing_baseline=curve_missing,
        )

    # (a) 缺省(None)继承单点值:warn → 双 no_baseline;fail → 双 violations
    warn_both = _both("warn", None)
    assert warn_both["gate"]["status"] == "no_baseline"
    assert warn_both["curve_gate"]["status"] == "no_baseline"
    fail_both = _both("fail", None)
    assert fail_both["gate"]["status"] == "violations"
    assert fail_both["curve_gate"]["status"] == "violations"
    assert fail_both["curve_gate"]["missing_baseline"] == "fail"  # 段内记录生效值

    # (b) 单点 fail + 曲线显式 warn:单点违例、曲线跳过(两策略独立)
    mixed = _both("fail", "warn")
    assert mixed["gate"]["status"] == "violations"
    assert mixed["curve_gate"]["status"] == "no_baseline"
    assert mixed["curve_gate"]["missing_baseline"] == "warn"

    # (c) 单点 warn + 曲线显式 fail:单点跳过、曲线违例
    mixed_rev = _both("warn", "fail")
    assert mixed_rev["gate"]["status"] == "no_baseline"
    assert mixed_rev["curve_gate"]["status"] == "violations"

    # (d) CLI 透传:显式 --curve-missing-baseline 覆盖 --missing-baseline
    base_args = [
        "--corpus", str(corpus),
        "--golden", str(single_g), "--curve-golden", str(curve_g),
        "--classifier", "stub_renamed", "--gate", "--curve-gate",
    ]
    rc = main([*base_args, "--out", str(tmp_path / "c1"),
               "--missing-baseline", "fail", "--curve-missing-baseline", "warn"])
    assert rc == 2  # 单点 fail 拦截;曲线 warn 不拦截
    err = capsys.readouterr().err
    assert "金标违例" in err and "无基线" in err

    rc = main([*base_args, "--out", str(tmp_path / "c2"),
               "--missing-baseline", "warn", "--curve-missing-baseline", "fail"])
    assert rc == 2  # 曲线 fail 拦截;单点 warn 不拦截
    err = capsys.readouterr().err
    assert "曲线金标违例" in err and "退出码 2" in err

    # 双 warn:两门禁都跳过 → 退出码 0(缺省继承等价于显式同值)
    rc = main([*base_args, "--out", str(tmp_path / "c3"),
               "--missing-baseline", "warn", "--curve-missing-baseline", "warn"])
    assert rc == 0
    rc = main([*base_args, "--out", str(tmp_path / "c4"), "--missing-baseline", "warn"])
    assert rc == 0  # 未显式设置 → 曲线继承 warn

    # 非法取值由曲线门禁段校验(中文报错,CLI 退出码 2)
    with pytest.raises(AdversarialError, match="missing_baseline 取值非法"):
        run(
            corpus, tmp_path / "c5", classifier="stub_renamed",
            curve_gate=True, curve_golden_path=curve_g,
            curve_missing_baseline="ignore",
        )
