# -*- coding: utf-8 -*-
"""pHash 指纹对抗鲁棒性红队基准测试(benchmarks/phash_redteam.py)。

全程离线、确定性(固定种子)、只写 tmp_path。覆盖:

- **确定性**:合成语料同 ``(count, seed)`` 字节级一致、异种子可区分;
  攻击变体同种子同输出字节(逐族逐档);噪声变体种子敏感(异图序 →
  异字节);``variant_seed`` 稳定且区分非法输入;
- **召回矩阵数学正确性**:恒等控制组(无攻击重存)任何指纹 × 距离档
  (含两索引)召回恒 = 1.0;矩阵 cells 与 details 逐格对账(独立重算
  一致);轻攻击锚点(缩放 / JPEG p64@8 = 1.0);极端攻击已知低召回
  (镜像 / 中心裁剪 @p64 d8 ≈ 0);
- **真值对账**:LSH 召回 ≤ 暴力全扫真值(逐族 × 参数 × d);
- **两索引对比**:多表 multi-probe ≥ 单表(逐 d 聚合 + 水印族数据点),
  d16 优势 ≥ 5pp,多表 ≤ 暴力真值;
- **结构**:最脆弱 Top3(不含恒等族、按最小召回升序)、
  min_effective_attack 字段语义、报告 JSON+markdown 落盘含关键节、
  payload 默认无 "gate" 键、details 完整性与值域;
- **管线级确定性**:两次 run 的矩阵 / 对比 / 明细逐位一致;
- **金标门禁**:指纹稳定与敏感(compute_fingerprint)、update→gate
  往返通过、篡改基线 → 违例 + 退出码 2、金标损坏 / 结构非法 → 1、
  缺基线 warn 0 / fail 2、容差可配(tol_ratio 分支);
- **CLI 冒烟**:退出码三态(0 正常 / 1 金标输入错误 / 2 违例或参数
  非法);PIL 缺席:importorskip 整文件跳过 + monkeypatch 屏蔽后中文
  错误、CLI 退出码 2。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from benchmarks.phash_redteam import (
    ATTACK_FAMILIES,
    ATTACK_META,
    ATTACK_VERSION,
    DEFAULT_SEED,
    DISTANCES256,
    DISTANCES64,
    GATE_METRICS,
    GOLDEN_SCHEMA,
    IDENTITY_FAMILY,
    RECALL_FLOOR,
    TOL_VALUE_RATIO,
    RedteamError,
    RedteamGoldenError,
    apply_attack,
    build_golden_entry,
    compute_fingerprint,
    evaluate_gate,
    generate_variants,
    load_golden,
    main,
    make_synthetic_corpus,
    metric_tolerance,
    run,
    save_golden,
    variant_seed,
)

# 攻击变换与语料生成依赖 Pillow;环境缺失时整文件跳过(无 PIL 分支用
# monkeypatch 单独覆盖)。
PIL = pytest.importorskip("PIL", reason="需要 Pillow 才能测试 pHash 红队基准")
from PIL import Image as PILImage  # noqa: E402

#: 模块级共享跑批张数(覆盖全部 6 类构图;33 变体/图 → 198 条明细)。
COUNT = 6

#: 每图变体数 = 恒等 1 + 9 族 32 档。
VARIANTS_PER_IMAGE = 1 + sum(len(ATTACK_META[f]["params"]) for f in ATTACK_FAMILIES)


@pytest.fixture(scope="module")
def payload(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """模块级共享的一次完整跑批(count=6,默认种子),报告落 tmp。"""
    out = tmp_path_factory.mktemp("redteam_run")
    return run(out, count=COUNT)


# ---------------------------------------------------------------------------
# 确定性:合成语料 / 变体生成 / 种子派生
# ---------------------------------------------------------------------------


def test_synthetic_corpus_deterministic_bytes(tmp_path: Path) -> None:
    """同 (count, seed) 两次生成逐文件字节一致;异种子可区分。"""
    first = make_synthetic_corpus(tmp_path / "a", COUNT, DEFAULT_SEED)
    second = make_synthetic_corpus(tmp_path / "b", COUNT, DEFAULT_SEED)
    assert len(first) == COUNT
    for item_a, item_b in zip(first, second):
        assert item_a["name"] == item_b["name"]
        assert Path(item_a["path"]).read_bytes() == Path(item_b["path"]).read_bytes()
    other = make_synthetic_corpus(tmp_path / "c", COUNT, DEFAULT_SEED + 1)
    assert any(
        Path(x["path"]).read_bytes() != Path(y["path"]).read_bytes()
        for x, y in zip(first, other)
    )


def test_synthetic_corpus_count_validation(tmp_path: Path) -> None:
    """count 越界 / 非法 → 中文 ValueError;run 层转 RedteamError。"""
    for bad in (0, -1, 129):
        with pytest.raises(ValueError, match="count 无效"):
            make_synthetic_corpus(tmp_path / "bad", bad, DEFAULT_SEED)
    with pytest.raises(RedteamError, match="count 无效"):
        run(tmp_path / "out", count=0)


def test_variant_seed_stable_distinct_and_invalid() -> None:
    """variant_seed:同参恒等、异参区分、非法族/参数中文 ValueError。"""
    a = variant_seed(DEFAULT_SEED, 0, "noise", "8")
    assert a == variant_seed(DEFAULT_SEED, 0, "noise", "8")
    assert a != variant_seed(DEFAULT_SEED, 1, "noise", "8")
    assert a != variant_seed(DEFAULT_SEED, 0, "noise", "16")
    assert a != variant_seed(DEFAULT_SEED + 1, 0, "noise", "8")
    with pytest.raises(ValueError, match="非法攻击族/参数"):
        variant_seed(DEFAULT_SEED, 0, "no_such_family", "8")
    with pytest.raises(ValueError, match="非法攻击族/参数"):
        variant_seed(DEFAULT_SEED, 0, "noise", "999")


def test_generate_variants_deterministic_bytes(tmp_path: Path) -> None:
    """同种子两次生成:逐变体字节一致;恒等变体 = 源图字节;噪声种子敏感。"""
    corpus = make_synthetic_corpus(tmp_path / "corpus", 2, DEFAULT_SEED)
    src = Path(corpus[0]["path"])
    first = generate_variants(src, tmp_path / "v1", seed=DEFAULT_SEED, image_index=0)
    second = generate_variants(src, tmp_path / "v2", seed=DEFAULT_SEED, image_index=0)
    assert len(first) == VARIANTS_PER_IMAGE
    assert [r["family"] for r in first][0] == IDENTITY_FAMILY
    for rec_a, rec_b in zip(first, second):
        assert rec_a["family"] == rec_b["family"] and rec_a["param"] == rec_b["param"]
        assert Path(rec_a["path"]).read_bytes() == Path(rec_b["path"]).read_bytes()

    identity = next(r for r in first if r["family"] == IDENTITY_FAMILY)
    assert Path(identity["path"]).read_bytes() == src.read_bytes()

    # 噪声族种子敏感:换图序(即换派生种子)→ 字节变化;其余族与图序无关
    # (纯确定性几何 / 光度变换,不消费种子)。
    shifted = generate_variants(
        src, tmp_path / "v3", seed=DEFAULT_SEED, image_index=1, families=("noise",),
        include_identity=False,
    )
    noise_first = next(r for r in first if r["family"] == "noise" and r["param"] == "8")
    assert Path(shifted[0]["path"]).read_bytes() != Path(noise_first["path"]).read_bytes()


def test_apply_attack_validation_and_purity(tmp_path: Path) -> None:
    """非法族 / 参数中文 ValueError;攻击不修改入参图(纯函数语义)。"""
    corpus = make_synthetic_corpus(tmp_path / "corpus", 1, DEFAULT_SEED)
    with PILImage.open(corpus[0]["path"]) as opened:
        opened.load()
        im = opened.convert("L")
    getter = getattr(im, "get_flattened_data", None) or im.getdata
    before = list(getter())
    with pytest.raises(ValueError, match="非法攻击族"):
        apply_attack(im, "no_such_family", "1.0")
    with pytest.raises(ValueError, match="非法攻击参数"):
        apply_attack(im, "noise", "7")
    for family in ATTACK_FAMILIES:
        param = ATTACK_META[family]["params"][0]
        out = apply_attack(im, family, param, seed=42)
        # 除 resize 族(攻击即改变分辨率)外,各族变体保持原始宽高。
        if family != "resize":
            assert out.size == im.size, family
        assert out is not im
    assert list(getter()) == before  # 入参未被原地修改


def test_generate_variants_rejects_bad_family(tmp_path: Path) -> None:
    """generate_variants 传入未知攻击族 → 中文 ValueError。"""
    corpus = make_synthetic_corpus(tmp_path / "corpus", 1, DEFAULT_SEED)
    with pytest.raises(ValueError, match="非法攻击族"):
        apply_attack(PILImage.new("L", (8, 8)), "unknown", "x")


# ---------------------------------------------------------------------------
# 召回矩阵:恒等控制组 / 对账 / 已知锚点
# ---------------------------------------------------------------------------


def test_identity_control_group_all_ones(payload: dict) -> None:
    """无攻击(恒等重存)在任何指纹 × 距离档(含两索引)召回恒 = 1.0。"""
    row = next(r for r in payload["families"] if r["type"] == IDENTITY_FAMILY)
    for fp in ("phash64", "phash256", "lsh_single", "lsh_multi"):
        for value in row["recall"][fp].values():
            assert value == 1.0
    for cell in row["cells"].values():
        for fp in cell:
            for value in cell[fp].values():
                assert value == 1.0


def test_matrix_recount_matches_details(payload: dict) -> None:
    """真值对账(重算):矩阵 cells / 族均值与 details 明细独立重算一致。"""
    details = payload["details"]
    count = payload["corpus"]["count"]
    for row in payload["families"]:
        family = row["type"]
        for param in row["params"]:
            bucket = [r for r in details if r["family"] == family and r["param"] == param]
            assert len(bucket) == count
            cell = row["cells"][param]
            for d in DISTANCES64:
                expect = round(sum(1 for r in bucket if r["dist64"] <= d) / count, 4)
                assert cell["phash64"][str(d)] == expect
                expect_single = round(
                    sum(1 for r in bucket if r["lsh_single_in_d16"] and r["dist64"] <= d)
                    / count, 4,
                )
                assert cell["lsh_single"][str(d)] == expect_single
                expect_multi = round(
                    sum(1 for r in bucket if r["lsh_multi_in_d16"] and r["dist64"] <= d)
                    / count, 4,
                )
                assert cell["lsh_multi"][str(d)] == expect_multi
            for big_d in DISTANCES256:
                expect = round(sum(1 for r in bucket if r["dist256"] <= big_d) / count, 4)
                assert cell["phash256"][str(big_d)] == expect
        # 族均值 = 全部参数档命中的总平均(与逐格均值等价)。
        n_total = count * len(row["params"])
        for d in DISTANCES64:
            expect = round(
                sum(
                    1
                    for r in details
                    if r["family"] == family and r["dist64"] <= d
                )
                / n_total, 4,
            )
            assert row["recall"]["phash64"][str(d)] == expect


def test_lsh_recall_never_exceeds_bruteforce(payload: dict) -> None:
    """真值对账(结构性):LSH 召回 ≤ 暴力全扫真值,逐族 × 参数 × d。"""
    for row in payload["families"]:
        for cell in row["cells"].values():
            for d in DISTANCES64:
                assert cell["lsh_single"][str(d)] <= cell["phash64"][str(d)] + 1e-9
                assert cell["lsh_multi"][str(d)] <= cell["phash64"][str(d)] + 1e-9
        for d in DISTANCES64:
            assert row["recall"]["lsh_single"][str(d)] <= row["recall"]["phash64"][str(d)] + 1e-9
            assert row["recall"]["lsh_multi"][str(d)] <= row["recall"]["phash64"][str(d)] + 1e-9


def test_known_recall_anchors(payload: dict) -> None:
    """已知锚点:轻攻击(缩放/JPEG/亮度)满召回;强攻击(镜像/裁剪)低召回。"""
    by_type = {row["type"]: row for row in payload["families"]}
    # 轻攻击:64bit 工作点 d=8 召回 1.0(DCT 中位阈值对缩放 / 重编码 /
    # 线性光度缩放的数学稳定性)。
    assert by_type["resize"]["recall"]["phash64"]["8"] == 1.0
    assert by_type["jpeg"]["recall"]["phash64"]["8"] == 1.0
    assert by_type["brightness"]["recall"]["phash64"]["8"] == 1.0
    # 极端攻击:镜像翻转改变奇次频率系数符号(约半数位翻转),中心裁剪
    # 破坏全局频率结构 —— 两个最强攻击族在工作点召回 ≈ 0。
    assert by_type["flip"]["recall"]["phash64"]["8"] <= 0.05
    assert by_type["crop"]["recall"]["phash64"]["8"] <= 0.05
    assert by_type["crop"]["recall"]["phash256"]["64"] <= 0.05
    # 旋转为梯级攻击:即使最小 3° 档也已跌破召回地板(分块 256bit 更敏感)。
    assert by_type["rotate"]["recall"]["phash64"]["8"] <= 0.5
    assert by_type["rotate"]["recall"]["phash256"]["32"] <= 0.05


# ---------------------------------------------------------------------------
# 两索引对比(单表 vs 多表)
# ---------------------------------------------------------------------------


def test_index_compare_multi_advantage(payload: dict) -> None:
    """多表 multi-probe 逐 d 不低于单表、不超暴力真值;d16 优势 ≥ 5pp。"""
    cmp_data = payload["index_compare"]
    assert cmp_data["n_variants"] == COUNT * (VARIANTS_PER_IMAGE - 1)
    for d in cmp_data["d_values"]:
        row = cmp_data[str(d)]
        assert row["lsh_multi"] <= row["brute"] + 1e-9
        assert row["lsh_single"] <= row["brute"] + 1e-9
        assert row["lsh_multi"] >= row["lsh_single"] - 1e-9
    at16 = cmp_data["16"]
    assert at16["lsh_multi"] - at16["lsh_single"] >= 0.05
    assert at16["mt_minus_single"] == round(
        at16["lsh_multi"] - at16["lsh_single"], 4
    )


def test_index_compare_watermark_datapoint(payload: dict) -> None:
    """水印族数据点:单表 d≥bands 后漏检,多表把召回拉回暴力水平。"""
    row = next(r for r in payload["families"] if r["type"] == "watermark")
    recall = row["recall"]
    assert recall["lsh_multi"]["8"] > recall["lsh_single"]["8"]
    assert recall["lsh_multi"]["8"] == recall["phash64"]["8"]


# ---------------------------------------------------------------------------
# 结构:Top3 / min_effective_attack / 报告 / payload 形状
# ---------------------------------------------------------------------------


def test_top3_fragile_structure(payload: dict) -> None:
    """Top3:恰 3 项、不含恒等族、按最小召回升序、已知强攻击居前。"""
    top3 = payload["top3_fragile"]
    assert len(top3) == 3
    assert IDENTITY_FAMILY not in [item["type"] for item in top3]
    recalls = [item["min_recall"] for item in top3]
    assert recalls == sorted(recalls)
    assert top3[0]["type"] in {"crop", "flip"}
    assert {"crop", "flip"} <= {item["type"] for item in top3}
    for item in top3:
        assert item["worst_param"] in ATTACK_META[item["type"]]["params"]
        assert "@" in item["worst_metric"]


def test_min_effective_attack_semantics(payload: dict) -> None:
    """min_effective:恒等族不跌破;crop 首档 0.1 / flip 首档 h 即跌破;
    跌破档召回 < 地板,且此前的档位全部 ≥ 地板(沿强度递增序)。"""
    by_type = {row["type"]: row for row in payload["families"]}
    none_row = by_type[IDENTITY_FAMILY]
    assert none_row["min_effective_attack"]["brute_d8"] is None
    assert none_row["min_effective_attack"]["lsh_mt_d8"] is None

    for family, first_drop in (("crop", "0.1"), ("flip", "h")):
        row = by_type[family]
        drop = row["min_effective_attack"]["brute_d8"]
        assert drop is not None and drop["param"] == first_drop
        assert drop["recall"] < RECALL_FLOOR
        params = row["params"]
        before = params[: params.index(first_drop)]
        assert before == []  # 首档即跌破
    rotate_drop = by_type["rotate"]["min_effective_attack"]["brute_d8"]
    assert rotate_drop is not None and rotate_drop["param"] == "3"


def test_details_completeness(payload: dict) -> None:
    """明细:条数 = 图 × 33;字段齐全且值域合法;基图两两指纹互异。"""
    details = payload["details"]
    assert len(details) == COUNT * VARIANTS_PER_IMAGE
    for record in details:
        assert 0 <= record["dist64"] <= 64
        assert 0 <= record["dist256"] <= 256
        assert isinstance(record["lsh_single_in_d16"], bool)
        assert isinstance(record["lsh_multi_in_d16"], bool)
        assert record["family"] in set(ATTACK_FAMILIES) | {IDENTITY_FAMILY}
    sep = payload["corpus"]["separability"]
    assert sep["phash64_min_pair_dist"] >= 1
    assert sep["phash256_min_pair_dist"] >= 1


def test_reports_written_and_parseable(tmp_path_factory: pytest.TempPathFactory) -> None:
    """报告 JSON+markdown 落盘 tmp、可解析、含关键节;默认无 gate 键。"""
    out = tmp_path_factory.mktemp("redteam_report")
    result = run(out, count=2)
    assert "gate" not in result  # gate=False 默认纯报告(对齐 adversarial 惯例)

    report_json = out / "phash_redteam_report.json"
    report_md = out / "phash_redteam_report.md"
    assert report_json.is_file() and report_md.is_file()
    loaded = json.loads(report_json.read_text(encoding="utf-8"))
    assert loaded["benchmark"] == "phash_redteam"
    assert loaded["attacks"]["version"] == ATTACK_VERSION
    assert len(loaded["families"]) == 1 + len(ATTACK_FAMILIES)

    markdown = report_md.read_text(encoding="utf-8")
    for section in (
        "召回矩阵",
        "最脆弱攻击 Top3",
        "min_effective_attack",
        "索引对比",
        "逐变体明细",
        "无攻击(恒等重存)",
    ):
        assert section in markdown


def test_pipeline_deterministic_rerun(
    payload: dict, tmp_path: Path
) -> None:
    """管线级确定性:两次 run 的矩阵 / 索引对比 / 明细逐位一致。"""
    again = run(tmp_path / "out", count=COUNT)
    assert again["families"] == payload["families"]
    assert again["index_compare"] == payload["index_compare"]
    assert again["details"] == payload["details"]
    assert again["corpus"]["digest"] == payload["corpus"]["digest"]


# ---------------------------------------------------------------------------
# 金标门禁
# ---------------------------------------------------------------------------


def test_compute_fingerprint_stable_and_sensitive() -> None:
    """金标指纹:同 (count, seed) 稳定;异种子 / 异张数 → 换键。"""
    assert compute_fingerprint(2, DEFAULT_SEED) == compute_fingerprint(2, DEFAULT_SEED)
    assert compute_fingerprint(2, DEFAULT_SEED) != compute_fingerprint(2, DEFAULT_SEED + 1)
    assert compute_fingerprint(2, DEFAULT_SEED) != compute_fingerprint(3, DEFAULT_SEED)


def test_metric_tolerance_branches() -> None:
    """容差两分支与校验:比例分支、CI 半宽分支、非法输入中文错误。"""
    # 比例分支主导:|0.8| × 0.1 = 0.08 > CI 半宽 0。
    assert metric_tolerance("recall_phash64_d8", 0.8, [0.8, 0.8]) == pytest.approx(0.08)
    # CI 半宽分支主导:零值比例分支为 0,取 Wilson 半宽。
    assert metric_tolerance("recall_phash64_d8", 0.0, [0.0, 0.2]) == pytest.approx(0.1)
    # 比例可配。
    assert metric_tolerance(
        "recall_phash64_d8", 0.8, [0.8, 0.8], tol_ratio=0.5
    ) == pytest.approx(0.4)
    with pytest.raises(ValueError, match="非门禁指标"):
        metric_tolerance("not_a_metric", 0.5, [0.0, 0.1])
    with pytest.raises(ValueError, match="tol_ratio 无效"):
        metric_tolerance("recall_phash64_d8", 0.5, [0.0, 0.1], tol_ratio=-0.1)


def test_golden_roundtrip_gate_pass(tmp_path: Path) -> None:
    """--update-golden → --gate 往返:重建后门禁通过(payload + CLI 双口径)。"""
    out = tmp_path / "out"
    golden = tmp_path / "golden.json"
    updated = run(out, count=2, update_golden=True, golden_path=golden)
    assert updated["gate"]["status"] == "updated"
    assert updated["gate"]["metrics_recorded"] == 10 * len(GATE_METRICS)
    assert golden.is_file()

    checked = run(out, count=2, gate=True, golden_path=golden)
    assert checked["gate"]["status"] == "pass"
    assert checked["gate"]["violations"] == []

    raw = load_golden(golden)
    assert raw["_schema"] == GOLDEN_SCHEMA
    assert main(["--out", str(out), "--count", "2", "--golden", str(golden), "--gate"]) == 0


def test_golden_tamper_violation_exit2(tmp_path: Path) -> None:
    """篡改基线(偏差超容差)→ 中文违例清单 + CLI 退出码 2。"""
    out = tmp_path / "out"
    golden = tmp_path / "golden.json"
    run(out, count=2, update_golden=True, golden_path=golden)

    raw = json.loads(golden.read_text(encoding="utf-8"))
    fingerprint = next(key for key in raw if not key.startswith("_"))
    raw[fingerprint]["none/recall_phash64_d8"]["value"] = 0.0
    raw[fingerprint]["none/recall_phash64_d8"]["tol"] = 0.1
    golden.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    checked = run(out, count=2, gate=True, golden_path=golden)
    assert checked["gate"]["status"] == "violations"
    violation = checked["gate"]["violations"][0]
    assert violation["kind"] == "metric_violation"
    assert "超出容差" in violation["message"]
    assert (
        main(["--out", str(out), "--count", "2", "--golden", str(golden), "--gate"]) == 2
    )


def test_golden_corrupt_and_invalid_exit1(tmp_path: Path) -> None:
    """金标损坏 / 坏 JSON / 结构非法 → RedteamGoldenError / CLI 退出码 1。"""
    out = tmp_path / "out"
    golden = tmp_path / "golden.json"
    golden.write_text("{bad json", encoding="utf-8")
    with pytest.raises(RedteamGoldenError, match="无法解析"):
        run(out, count=2, gate=True, golden_path=golden)
    assert main(["--out", str(out), "--count", "2", "--golden", str(golden), "--gate"]) == 1

    # 顶层非对象。
    golden.write_text("[]", encoding="utf-8")
    with pytest.raises(RedteamGoldenError, match="顶层必须是对象"):
        load_golden(golden)
    assert main(["--out", str(out), "--count", "2", "--golden", str(golden), "--gate"]) == 1

    # 指标表结构非法(缺 tol / 非数值)。
    save_golden(golden, "ff" * 32, {"crop/recall_phash64_d8": {"value": 1.0}})
    with pytest.raises(RedteamGoldenError, match="结构非法"):
        load_golden(golden)
    assert main(["--out", str(out), "--count", "2", "--golden", str(golden), "--gate"]) == 1

    # 版本不兼容。
    raw = json.loads(golden.read_text(encoding="utf-8"))
    raw["_schema"] = "netsentinel-phash-redteam-golden/999"
    golden.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(RedteamGoldenError, match="版本不兼容"):
        load_golden(golden)


def test_golden_missing_baseline_warn_and_fail(tmp_path: Path) -> None:
    """指纹未命中金标(换种子):warn 跳过退出码 0;fail 按违例退出码 2。"""
    out = tmp_path / "out"
    golden = tmp_path / "golden.json"
    run(out, count=2, update_golden=True, golden_path=golden)  # 基线:默认种子

    warned = run(
        out, count=2, seed=DEFAULT_SEED + 7, gate=True, golden_path=golden
    )
    assert warned["gate"]["status"] == "no_baseline"
    assert warned["gate"]["warnings"]
    rc = main(
        [
            "--out", str(out), "--count", "2", "--seed", str(DEFAULT_SEED + 7),
            "--golden", str(golden), "--gate",
        ]
    )
    assert rc == 0

    failed = run(
        out, count=2, seed=DEFAULT_SEED + 7, gate=True, golden_path=golden,
        missing_baseline="fail",
    )
    assert failed["gate"]["status"] == "violations"
    assert failed["gate"]["violations"][0]["kind"] == "missing_baseline"
    rc = main(
        [
            "--out", str(out), "--count", "2", "--seed", str(DEFAULT_SEED + 7),
            "--golden", str(golden), "--gate", "--missing-baseline", "fail",
        ]
    )
    assert rc == 2

    with pytest.raises(RedteamError, match="missing_baseline 取值非法"):
        run(out, count=2, gate=True, golden_path=golden, missing_baseline="boom")


def test_build_golden_entry_tol_configurable(payload: dict) -> None:
    """build_golden_entry:键 = 族/指标、值带 4 位舍入;tol_ratio 可配生效。"""
    entry = build_golden_entry(payload["families"], tol_ratio=TOL_VALUE_RATIO)
    assert len(entry) == len(payload["families"]) * len(GATE_METRICS)
    loose = build_golden_entry(payload["families"], tol_ratio=0.9)
    for key, spec in entry.items():
        assert "/" in key and key.split("/")[0] in ATTACK_META
        assert loose[key]["tol"] >= spec["tol"]
        assert spec["tol"] >= abs(spec["value"]) * TOL_VALUE_RATIO - 1e-9


def test_evaluate_gate_missing_metric_violation(payload: dict) -> None:
    """金标缺个别指标键(统计版本过旧)→ 按违例处理,不静默跳过。"""
    entry = build_golden_entry(payload["families"])
    entry.pop("crop/recall_phash64_d8")
    violations = evaluate_gate(payload["families"], entry)
    missing = [v for v in violations if v["kind"] == "missing_metric"]
    assert len(missing) == 1
    assert missing[0]["metric"] == "crop/recall_phash64_d8"
    assert "缺该指标基线" in missing[0]["message"]


# ---------------------------------------------------------------------------
# CLI 冒烟与无 Pillow 分支
# ---------------------------------------------------------------------------


def test_cli_smoke_exit0(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """CLI 成功路径:退出码 0、中文摘要、报告双输出(tmp 目录)。"""
    out = tmp_path / "out"
    rc = main(["--out", str(out), "--count", "3"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "pHash 红队基准完成" in captured.out
    assert "索引对比" in captured.out
    assert (out / "phash_redteam_report.md").is_file()
    assert (out / "phash_redteam_report.json").is_file()


def test_cli_invalid_count_and_tol_exit2(tmp_path: Path) -> None:
    """CLI 参数非法(count=0 / 负容差比例)→ 中文错误 + 退出码 2。"""
    assert main(["--out", str(tmp_path / "o1"), "--count", "0"]) == 2
    assert main(
        ["--out", str(tmp_path / "o2"), "--count", "2", "--tol-ratio", "-0.5"]
    ) == 2


def test_run_without_pil_raises_and_cli_exit2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """屏蔽 PIL 后:run / 语料生成 / 变体生成中文错误;CLI 退出码 2。

    先用真 PIL 造好一张图再屏蔽(屏蔽态下无法编码落盘)。
    """
    corpus = make_synthetic_corpus(tmp_path / "corpus", 1, DEFAULT_SEED)
    monkeypatch.setitem(sys.modules, "PIL", None)
    with pytest.raises(RedteamError, match="未安装 Pillow"):
        run(tmp_path / "out", count=1)
    with pytest.raises(RedteamError, match="未安装 Pillow"):
        make_synthetic_corpus(tmp_path / "corpus2", 1, DEFAULT_SEED)
    with pytest.raises(RedteamError, match="未安装 Pillow"):
        generate_variants(corpus[0]["path"], tmp_path / "v", seed=DEFAULT_SEED)
    assert main(["--out", str(tmp_path / "out"), "--count", "1"]) == 2
