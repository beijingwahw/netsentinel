"""tests/test_provider_agreement.py —— A69 跨平台一致性分析单元测试。

离线:仅构造 ImageEvidence / ImageScore,不需要真实图片文件,也不访问网络。
"""
from __future__ import annotations

import pytest

from netsentinel.contracts import ImageEvidence, ImageScore
from netsentinel.vision.provider_agreement import analyze, provider_of


def make_evidence(path: str) -> ImageEvidence:
    """构造无需真实落盘文件的图片证据。"""
    return ImageEvidence(
        path=path,
        url=f"https://example.invalid/{path}",
        source_page="https://example.invalid/index.html",
        sha256="0" * 64,
        width=800,
        height=600,
    )


def make_score(path: str, model: str, prob: float) -> ImageScore:
    """快捷构造一条成员评分。"""
    return ImageScore(image=make_evidence(path), model=model, nsfw_prob=prob)


# ---------------------------------------------------------------------------
# provider_of:各命名形态
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("model_name", "expected"),
    [
        ("openai:gpt-4o-mini", "openai"),          # 提供方:模型
        ("glm:glm-5.3-flash", "glm"),
        ("qwen:qwen-vl-max", "qwen"),
        ("ollama:llava", "ollama"),
        ("anthropic", "anthropic"),                # 目录提供方名,不带模型
        ("stub", "stub"),                          # 无冒号 → 原名
        ("ensemble", "ensemble"),                  # 原样
        ("vlm-arbiter", "vlm-arbiter"),            # 原样
        ("nudenet", "nudenet"),
        ("failover→glm:glm-5.3-flash", "failover"),   # 故障转移标注先按 "→" 拆
        ("failover→openai:gpt-4o-mini", "failover"),
        ("", ""),                                  # 空字符串原样返回
    ],
)
def test_provider_of_forms(model_name: str, expected: str) -> None:
    """provider_of 对各种 model 命名形态的还原。"""
    assert provider_of(model_name) == expected


# ---------------------------------------------------------------------------
# 三方完全一致
# ---------------------------------------------------------------------------

def test_three_providers_full_agreement() -> None:
    """三方逐图打分一致:spread=0、bias=0、无离群、每对一致率 1.0。"""
    scores = [
        # a.jpg:三方都 0.8
        make_score("a.jpg", "glm:glm-5.3-flash", 0.8),
        make_score("a.jpg", "openai:gpt-4o-mini", 0.8),
        make_score("a.jpg", "anthropic:claude-sonnet-4", 0.8),
        # b.jpg:三方都 0.3
        make_score("b.jpg", "glm:glm-5.3-flash", 0.3),
        make_score("b.jpg", "openai:gpt-4o-mini", 0.3),
        make_score("b.jpg", "anthropic:claude-sonnet-4", 0.3),
    ]
    res = analyze(scores)

    assert res["providers"] == ["anthropic", "glm", "openai"]  # 去重排序

    assert len(res["per_image_spread"]) == 2
    for row in res["per_image_spread"]:
        assert row["spread"] == pytest.approx(0.0)
        assert row["max"] == pytest.approx(row["min"])

    assert set(res["bias"]) == {"anthropic", "glm", "openai"}
    assert all(v == pytest.approx(0.0) for v in res["bias"].values())

    assert res["outliers"] == []

    assert set(res["pair_agreement"]) == {
        ("anthropic", "glm"),
        ("anthropic", "openai"),
        ("glm", "openai"),
    }
    assert all(rate == pytest.approx(1.0) for rate in res["pair_agreement"].values())


# ---------------------------------------------------------------------------
# 一方系统性偏高
# ---------------------------------------------------------------------------

def test_systematic_high_bias_provider_flagged() -> None:
    """glm 每图都比另两方高 0.3:bias 精确、outlier 附中文建议、其配对一致率低。"""
    scores = [
        # a.jpg:共识 (0.9+0.6+0.6)/3 = 0.7 → glm 偏 +0.2,其余各 -0.1
        make_score("a.jpg", "glm:glm-5.3-flash", 0.9),
        make_score("a.jpg", "openai:gpt-4o-mini", 0.6),
        make_score("a.jpg", "qwen:qwen-vl-max", 0.6),
        # b.jpg:共识 0.6 → 同上偏离
        make_score("b.jpg", "glm:glm-5.3-flash", 0.8),
        make_score("b.jpg", "openai:gpt-4o-mini", 0.5),
        make_score("b.jpg", "qwen:qwen-vl-max", 0.5),
    ]
    res = analyze(scores)

    assert res["bias"]["glm"] == pytest.approx(0.2)      # 偏高 0.2 = 0.3 * (2/3)
    assert res["bias"]["openai"] == pytest.approx(-0.1)
    assert res["bias"]["qwen"] == pytest.approx(-0.1)

    # |bias|>0.15 的只有 glm,且给出中文处置建议
    assert len(res["outliers"]) == 1
    out = res["outliers"][0]
    assert out["provider"] == "glm"
    assert out["delta"] == pytest.approx(0.2)
    assert "偏高" in out["note"]
    assert "抽检" in out["note"]

    # glm 与另两方差 0.3 > 0.2 → 一致率 0;另两方差 0 → 一致率 1
    assert res["pair_agreement"][("glm", "openai")] == pytest.approx(0.0)
    assert res["pair_agreement"][("glm", "qwen")] == pytest.approx(0.0)
    assert res["pair_agreement"][("openai", "qwen")] == pytest.approx(1.0)

    # 争议最大的图是 a.jpg(spread 0.3)
    assert res["per_image_spread"][0]["image"] == "a.jpg"
    assert res["per_image_spread"][0]["spread"] == pytest.approx(0.3)
    assert res["per_image_spread"][0]["max"] == pytest.approx(0.9)
    assert res["per_image_spread"][0]["min"] == pytest.approx(0.6)


def test_systematic_low_bias_provider_flagged() -> None:
    """对称情形:一方系统性偏低 → outlier 注明"偏低"。"""
    scores = [
        make_score("a.jpg", "glm:glm-5.3-flash", 0.1),   # 另两方 0.4 → 共识 0.3,偏离 -0.2
        make_score("a.jpg", "openai:gpt-4o-mini", 0.4),
        make_score("a.jpg", "qwen:qwen-vl-max", 0.4),
    ]
    res = analyze(scores)
    assert res["bias"]["glm"] == pytest.approx(-0.2)
    assert len(res["outliers"]) == 1
    assert res["outliers"][0]["provider"] == "glm"
    assert res["outliers"][0]["delta"] == pytest.approx(-0.2)
    assert "偏低" in res["outliers"][0]["note"]


def test_bias_at_threshold_not_flagged() -> None:
    """|bias| 恰为 0.15(未严格超过)→ 不算离群。"""
    scores = [
        # 共识 0.25 → openai 偏 +0.15,stub 偏 -0.15
        make_score("a.jpg", "openai:gpt-4o-mini", 0.4),
        make_score("a.jpg", "stub", 0.1),
    ]
    res = analyze(scores)
    assert res["bias"]["openai"] == pytest.approx(0.15)
    assert res["outliers"] == []


# ---------------------------------------------------------------------------
# 单提供方组不参与
# ---------------------------------------------------------------------------

def test_single_provider_group_excluded() -> None:
    """仅一个提供方评分的图不参与统计;提供方名单也不含它。"""
    scores = [
        # 有效组:两提供方
        make_score("both.jpg", "glm:glm-5.3-flash", 0.7),
        make_score("both.jpg", "openai:gpt-4o-mini", 0.5),
        # 无效组:单提供方(即便有多条评分)
        make_score("solo.jpg", "stub", 0.9),
        make_score("solo.jpg", "stub", 0.8),
    ]
    res = analyze(scores)

    assert res["providers"] == ["glm", "openai"]          # stub 不在列
    assert [r["image"] for r in res["per_image_spread"]] == ["both.jpg"]
    assert "stub" not in res["bias"]
    assert set(res["pair_agreement"]) == {("glm", "openai")}


def test_same_provider_different_models_excluded() -> None:
    """同一提供方的两个模型(如 glm 两个版本)仍只算一个提供方,组不参与。"""
    scores = [
        make_score("twoglm.jpg", "glm:glm-5.3-flash", 0.9),
        make_score("twoglm.jpg", "glm:glm-4.5v", 0.2),
    ]
    res = analyze(scores)
    assert res == {
        "providers": [],
        "per_image_spread": [],
        "bias": {},
        "outliers": [],
        "pair_agreement": {},
    }


# ---------------------------------------------------------------------------
# spread 排序与截断
# ---------------------------------------------------------------------------

def test_spread_ordering_and_truncation() -> None:
    """25 张图:按 spread 降序、最多 20 条,最小 spread 的图被截断。"""
    scores: list[ImageScore] = []
    for i in range(25):
        half = i * 0.01                                   # spread_i = i * 0.02
        scores.append(make_score(f"img_{i:02d}.jpg", "openai:gpt-4o-mini", 0.5 - half))
        scores.append(make_score(f"img_{i:02d}.jpg", "gemini:gemini-2.0-flash", 0.5 + half))
    res = analyze(scores)

    rows = res["per_image_spread"]
    assert len(rows) == 20                                # 截断到 MAX_SPREAD_ROWS
    spreads = [r["spread"] for r in rows]
    assert spreads == sorted(spreads, reverse=True)       # 降序
    assert rows[0]["image"] == "img_24.jpg"               # 争议最大
    assert rows[0]["max"] == pytest.approx(0.74)
    assert rows[0]["min"] == pytest.approx(0.26)
    assert rows[0]["spread"] == pytest.approx(0.48)
    listed = {r["image"] for r in rows}
    assert "img_00.jpg" not in listed                     # spread 最小的 5 张被截掉
    assert "img_04.jpg" not in listed
    assert "img_05.jpg" in listed
    # 每行字段齐全
    assert all(set(r) == {"image", "max", "min", "spread"} for r in rows)


# ---------------------------------------------------------------------------
# ensemble 条目与提供方条目混合
# ---------------------------------------------------------------------------

def test_ensemble_entries_mixed_with_provider_entries() -> None:
    """ensemble / vlm-arbiter / stub 原样视为一个"提供方",可与真提供方成组。"""
    assert provider_of("ensemble") == "ensemble"

    scores = [
        make_score("m.jpg", "ensemble", 0.6),
        make_score("m.jpg", "openai:gpt-4o-mini", 0.4),
    ]
    res = analyze(scores)

    assert res["providers"] == ["ensemble", "openai"]
    assert len(res["per_image_spread"]) == 1
    assert res["per_image_spread"][0]["spread"] == pytest.approx(0.2)
    # 分差恰为 0.2(含边界)→ 一致
    assert res["pair_agreement"] == {("ensemble", "openai"): pytest.approx(1.0)}


# ---------------------------------------------------------------------------
# 空输入与全单提供方
# ---------------------------------------------------------------------------

def test_empty_input_returns_empty_structure() -> None:
    """空输入 → 全空结构。"""
    assert analyze([]) == {
        "providers": [],
        "per_image_spread": [],
        "bias": {},
        "outliers": [],
        "pair_agreement": {},
    }


def test_all_single_provider_groups_returns_empty_structure() -> None:
    """全部图片都只有单一提供方 → 同样返回全空结构。"""
    scores = [
        make_score("a.jpg", "stub", 0.9),
        make_score("b.jpg", "nudenet", 0.1),
        make_score("c.jpg", "glm:glm-5.3-flash", 0.5),
    ]
    assert analyze(scores) == {
        "providers": [],
        "per_image_spread": [],
        "bias": {},
        "outliers": [],
        "pair_agreement": {},
    }


# ---------------------------------------------------------------------------
# V5:单遍流式等价性 + telemetry 计时
# ---------------------------------------------------------------------------


def _v4_reference_analyze(scores: list[ImageScore]) -> dict:
    """V4 参考实现:先分组收集整条评分,再逐组重算(升级前的两遍式算法)。"""
    from netsentinel.vision.provider_agreement import (
        BIAS_OUTLIER_THRESHOLD,
        MAX_SPREAD_ROWS,
        PAIR_AGREEMENT_TOLERANCE,
        _NOTE_HIGH,
        _NOTE_LOW,
        provider_of as _provider_of,
    )

    _eps = 1e-9
    grouped: dict[str, list[ImageScore]] = {}
    for s in scores:
        grouped.setdefault(s.image.path, []).append(s)

    providers_seen: set[str] = set()
    spread_rows: list[dict] = []
    dev_sum: dict[str, float] = {}
    dev_cnt: dict[str, int] = {}
    pair_ok: dict[tuple[str, str], int] = {}
    pair_total: dict[tuple[str, str], int] = {}

    for path, entries in grouped.items():
        by_provider: dict[str, list[float]] = {}
        for s in entries:
            by_provider.setdefault(_provider_of(s.model), []).append(s.nsfw_prob)
        if len(by_provider) < 2:
            continue
        provider_value = {p: sum(v) / len(v) for p, v in by_provider.items()}
        consensus = sum(s.nsfw_prob for s in entries) / len(entries)
        providers_seen.update(provider_value)
        values = list(provider_value.values())
        hi, lo = max(values), min(values)
        spread_rows.append(
            {"image": path, "max": round(hi, 4), "min": round(lo, 4),
             "spread": round(hi - lo, 4)}
        )
        for p, v in provider_value.items():
            dev_sum[p] = dev_sum.get(p, 0.0) + (v - consensus)
            dev_cnt[p] = dev_cnt.get(p, 0) + 1
        names = sorted(provider_value)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                key = (names[i], names[j])
                pair_total[key] = pair_total.get(key, 0) + 1
                if abs(provider_value[names[i]] - provider_value[names[j]]) <= (
                    PAIR_AGREEMENT_TOLERANCE + _eps
                ):
                    pair_ok[key] = pair_ok.get(key, 0) + 1

    if not providers_seen:
        return {"providers": [], "per_image_spread": [], "bias": {},
                "outliers": [], "pair_agreement": {}}

    bias = {p: round(dev_sum[p] / dev_cnt[p], 4) for p in sorted(dev_cnt)}
    outliers = []
    for p in sorted(bias):
        raw = dev_sum[p] / dev_cnt[p]
        if abs(raw) > BIAS_OUTLIER_THRESHOLD + _eps:
            outliers.append({
                "provider": p, "delta": bias[p],
                "note": _NOTE_HIGH if raw > 0 else _NOTE_LOW,
            })
    outliers.sort(key=lambda o: (-abs(o["delta"]), o["provider"]))
    spread_rows.sort(key=lambda r: -r["spread"])
    return {
        "providers": sorted(providers_seen),
        "per_image_spread": spread_rows[:MAX_SPREAD_ROWS],
        "bias": bias,
        "outliers": outliers,
        "pair_agreement": {
            key: round(pair_ok.get(key, 0) / total, 4)
            for key, total in sorted(pair_total.items())
        },
    }


def test_v5_analyze_equivalent_to_v4_reference_randomized() -> None:
    """随机化等价:多种子随机评分批次上,单遍新版与 V4 参考实现输出逐位一致。"""
    import random

    model_pool = [
        "glm:glm-5.3-flash", "glm:glm-4.5v", "openai:gpt-4o-mini",
        "openai:gpt-4o", "qwen:qwen-vl-max", "anthropic:claude-sonnet-4",
        "gemini:gemini-2.0-flash", "stub", "ensemble",
        "failover→glm:glm-5.3-flash", "doubao:doubao-1.5-vision-pro",
    ]
    for seed in range(12):
        rng = random.Random(seed)
        n_images = rng.randint(0, 25)
        scores: list[ImageScore] = []
        for i in range(n_images):
            # 每图 1~5 条评分(同提供方可重复 → 触发按提供方均值去重路径)
            for _ in range(rng.randint(1, 5)):
                scores.append(
                    make_score(
                        f"img_{i:03d}.jpg",
                        rng.choice(model_pool),
                        round(rng.random(), 6),
                    )
                )
        rng.shuffle(scores)
        assert analyze(scores) == _v4_reference_analyze(scores), f"seed={seed}"


def test_v5_analyze_exact_floats_unchanged() -> None:
    """浮点口径锁定:新旧实现的 bias/共识逐位一致(非近似)。"""
    probs = [0.1, 0.2, 0.3, 0.35, 0.9, 0.07]
    scores = [
        make_score("x.jpg", "openai:gpt-4o-mini", probs[0]),
        make_score("x.jpg", "openai:gpt-4o-mini", probs[1]),   # 同提供方两条 → 均值
        make_score("x.jpg", "glm:glm-5.3-flash", probs[2]),
        make_score("x.jpg", "stub", probs[3]),
    ]
    new = analyze(scores)
    ref = _v4_reference_analyze(scores)
    assert new == ref
    assert new["bias"] == ref["bias"]  # 逐位相等(含 round(…,4) 的位级结果)


def test_v5_analyze_telemetry_timer() -> None:
    """可观测性:每次 analyze 记一个 telemetry 'agreement.analyze' 计时样本。"""
    from netsentinel import telemetry

    telemetry.reset()
    try:
        scores = [
            make_score("a.jpg", "glm:glm-5.3-flash", 0.8),
            make_score("a.jpg", "openai:gpt-4o-mini", 0.4),
        ]
        analyze(scores)
        analyze([])  # 空输入同样计时(入口统一包裹)
        timers = telemetry.snapshot()["timers"]
        assert timers.get("agreement.analyze", {}).get("count") == 2
    finally:
        telemetry.reset()
