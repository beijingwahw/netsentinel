"""webui/runs_page.py 纯逻辑层测试(A175,离线,零外呼,不依赖 streamlit)。

- 只测纯函数(run_rows / tier_badge / cpu_advice / advice_tier / dist_line)
  及展示常量:本文件顶部成功 import 即证明纯逻辑层可独立导入(本机未装
  streamlit 时同样成立);
- run_rows 容错专项:空列表 / None / 非列表入参、坏行(None / 字面量 /
  无批次标识)、鸭子对象(SimpleNamespace)与 batch_id / ready_count 回退;
- dist_line 中文标签与缺省:固定三档顺序(与入参键序无关)、计数 0 如实
  展示、未知档位原样保留、空分布 / 坏输入回退"无数据";
- tier_badge 三态 + 未知;cpu_advice 三档阈值(≤2 / ≤8 / >8)与坏画像
  容错(一律按 cores=2 落低档建议);
- UI 守卫:streamlit 缺失时纯逻辑层可导入、main() 打印中文安装提示并
  返回退出码 1、render() 抛 RuntimeError;
- 全程不联网、不读真实 data/ 目录、不启动 streamlit 服务、不写任何文件。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from webui import runs_page
from webui.runs_page import (
    ADVICE_HIGH,
    ADVICE_LOW,
    ADVICE_MID,
    BADGE_HIGH,
    BADGE_LOW,
    BADGE_MID,
    BADGE_UNKNOWN,
    MAX_RUNS_SHOWN,
    VERDICT_LABELS_CN,
    advice_tier,
    cpu_advice,
    dist_line,
    run_rows,
    tier_badge,
)

_ROW_KEYS = {
    "batch",
    "sites",
    "verdict_cn",
    "groups",
    "tier_badge",
    "ready",
    "generated_at",
}


def _summary(**overrides: object) -> dict:
    """构造一份"最近运行汇总"形态的 dict(A168/A172 收官统计口径)。"""
    base: dict = {
        "batch": "session_20261002_133347",
        "sites": 12,
        "verdict_dist": {"clean": 7, "suspect": 2, "nsfw": 3},
        "groups": 4,
        "tier": "mid",
        "ready": 5,
        "generated_at": "2026-10-02 13:33:47",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# run_rows:字段 / 顺序 / 计数形态 / 回退 / 容错
# ---------------------------------------------------------------------------


def test_run_rows_exact_keys_and_field_values() -> None:
    """行固定七键;数值 int 化、徽章与判定串换算、时间原样保留。"""
    rows = run_rows([_summary()])
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == _ROW_KEYS
    assert row["batch"] == "session_20261002_133347"
    assert row["sites"] == 12
    assert isinstance(row["sites"], int)
    assert row["verdict_cn"] == "未发现 7 / 疑似 2 / 高置信 3"
    assert row["groups"] == 4
    assert row["tier_badge"] == BADGE_MID == "🟡 中(默认)"
    assert row["ready"] == 5
    assert row["generated_at"] == "2026-10-02 13:33:47"


def test_run_rows_groups_accepts_int_list_and_bad() -> None:
    """groups 三形态:数值直取、列表/字典取长度、折不动按 0。"""
    assert run_rows([_summary(groups=9)])[0]["groups"] == 9
    assert run_rows([_summary(groups=[{"name": "g"}, {"name": "h"}])])[0]["groups"] == 2
    assert run_rows([_summary(groups={"a": 1, "b": 2})])[0]["groups"] == 2
    assert run_rows([_summary(groups="很多")])[0]["groups"] == 0
    assert run_rows([_summary(groups=None)])[0]["groups"] == 0


def test_run_rows_duck_typed_with_batch_id_and_ready_count() -> None:
    """鸭子对象行 + batch 缺失回退 batch_id + ready 缺失回退 ready_count。"""
    duck = SimpleNamespace(
        batch_id="  B-77  ",
        sites="6",
        verdict_dist={"nsfw": 6},
        groups=[1, 2, 3],
        tier="high",
        ready_count=2,
        generated_at=None,
    )
    rows = run_rows([duck])  # type: ignore[arg-type]
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == _ROW_KEYS
    assert row["batch"] == "B-77"  # 去空白
    assert row["sites"] == 6  # 字符串数字宽容转 int
    assert row["verdict_cn"] == "高置信 6"
    assert row["groups"] == 3
    assert row["tier_badge"] == BADGE_HIGH
    assert row["ready"] == 2
    assert row["generated_at"] == ""  # None → 空串(UI 显示 —)


def test_run_rows_tier_missing_defaults_to_mid_badge() -> None:
    """tier 缺失按默认 mid 档;未知档位落 ❓ 徽章。"""
    assert run_rows([{"batch": "B1"}])[0]["tier_badge"] == BADGE_MID
    assert run_rows([_summary(tier="turbo")])[0]["tier_badge"] == BADGE_UNKNOWN


def test_run_rows_skips_rows_without_batch_identifier() -> None:
    """无批次标识(batch 与 batch_id 均空)的行整行跳过,不抛错。"""
    assert run_rows([{}]) == []
    assert run_rows([{"sites": 3, "groups": 1}]) == []
    assert run_rows([{"batch": "   ", "batch_id": ""}]) == []
    # batch 空白但 batch_id 有值仍保留
    assert run_rows([{"batch": " ", "batch_id": "B9"}])[0]["batch"] == "B9"


def test_run_rows_empty_none_and_non_list_inputs() -> None:
    """空列表 / None / 非列表入参一律 []。"""
    assert run_rows([]) == []
    assert run_rows(None) == []
    assert run_rows("不是列表") == []  # type: ignore[arg-type]
    assert run_rows({"batch": "B1"}) == []  # type: ignore[arg-type]


def test_run_rows_bad_entries_skipped_good_kept() -> None:
    """坏行(None / 数字 / 字符串 / 列表)逐条跳过,好行照常保留。"""
    summaries: list[object] = [
        None,
        42,
        "junk",
        ["假装是汇总"],
        _summary(batch="B2", sites=1),
    ]
    rows = run_rows(summaries)  # type: ignore[arg-type]
    assert len(rows) == 1
    assert rows[0]["batch"] == "B2"


def test_run_rows_preserves_input_order_and_missing_fields_default() -> None:
    """多行保输入序;缺字段按保守缺省(verdict_cn 无数据 / ready 0)。"""
    rows = run_rows(
        [
            _summary(batch="second", generated_at=""),
            _summary(batch="first", verdict_dist=None, ready=None, ready_count=None),
        ]
    )
    assert [r["batch"] for r in rows] == ["second", "first"]
    assert rows[0]["generated_at"] == ""
    assert rows[1]["verdict_cn"] == "无数据"
    assert rows[1]["ready"] == 0


# ---------------------------------------------------------------------------
# tier_badge:三态 + 未知
# ---------------------------------------------------------------------------


def test_tier_badge_three_known_tiers() -> None:
    """三档权威文案(CONTRACTS-V9 §1):低🟢 中🟡 高🔴 + 中文注记。"""
    assert tier_badge("low") == BADGE_LOW == "🟢 低(后台)"
    assert tier_badge("mid") == BADGE_MID == "🟡 中(默认)"
    assert tier_badge("high") == BADGE_HIGH == "🔴 高(全压榨)"


def test_tier_badge_tolerant_and_unknown() -> None:
    """空白/大小写规整后匹配;未知/空/None/非字符串 → ❓ 未知档位。"""
    assert tier_badge(" High ") == BADGE_HIGH
    assert tier_badge("LOW") == BADGE_LOW
    assert tier_badge("turbo") == BADGE_UNKNOWN == "❓ 未知档位"
    assert tier_badge("") == BADGE_UNKNOWN
    assert tier_badge(None) == BADGE_UNKNOWN
    assert tier_badge(123) == BADGE_UNKNOWN  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# cpu_advice / advice_tier:三档阈值 + 坏画像容错
# ---------------------------------------------------------------------------


def test_cpu_advice_low_for_small_cores() -> None:
    """cores ≤ 2 → 低档建议(避免卡顿)。"""
    assert cpu_advice({"cores": 1}) == ADVICE_LOW == "核数较少,建议低档避免卡顿"
    assert cpu_advice({"cores": 2}) == ADVICE_LOW


def test_cpu_advice_mid_for_medium_cores() -> None:
    """2 < cores ≤ 8 → 中档均衡(边界 3 与 8 都命中)。"""
    assert cpu_advice({"cores": 3}) == ADVICE_MID == "中档均衡"
    assert cpu_advice({"cores": 8}) == ADVICE_MID
    assert cpu_advice({"cores": 4}) == ADVICE_MID


def test_cpu_advice_high_for_many_cores() -> None:
    """cores > 8 → 高档压榨本地计算,且文案明示对外频控不变(红线 35)。"""
    assert cpu_advice({"cores": 9}) == ADVICE_HIGH
    assert cpu_advice({"cores": 16}) == ADVICE_HIGH
    assert "对外频控不变" in cpu_advice({"cores": 32})
    assert cpu_advice({"cores": 32}) == "可开高档压榨本地计算(对外频控不变)"


def test_cpu_advice_bad_profile_falls_back_to_low() -> None:
    """坏画像(None/非 dict/缺键/非整数/<1/奇异对象)按 cores=2 落低档。"""
    assert cpu_advice(None) == ADVICE_LOW
    assert cpu_advice({}) == ADVICE_LOW
    assert cpu_advice({"arch": "AMD64"}) == ADVICE_LOW
    assert cpu_advice({"cores": "八核"}) == ADVICE_LOW
    assert cpu_advice({"cores": 0}) == ADVICE_LOW
    assert cpu_advice(12345) == ADVICE_LOW  # type: ignore[arg-type]


def test_advice_tier_matches_advice_thresholds_and_duck_profile() -> None:
    """advice_tier 与 cpu_advice 同阈值;鸭子画像(SimpleNamespace)兼容。"""
    assert advice_tier({"cores": 2}) == "low"
    assert advice_tier({"cores": 8}) == "mid"
    assert advice_tier({"cores": 9}) == "high"
    assert advice_tier(None) == "low"
    assert advice_tier(SimpleNamespace(cores=16)) == "high"
    # 徽章组合:advice_tier 的产出可直接喂 tier_badge
    assert tier_badge(advice_tier({"cores": 1})) == BADGE_LOW


# ---------------------------------------------------------------------------
# dist_line:中文标签 / 固定顺序 / 缺省
# ---------------------------------------------------------------------------


def test_dist_line_chinese_labels_full_three() -> None:
    """三键齐全 → "未发现 2 / 疑似 1 / 高置信 3"(中文标签 + " / " 连接)。"""
    assert dist_line({"clean": 2, "suspect": 1, "nsfw": 3}) == "未发现 2 / 疑似 1 / 高置信 3"
    assert VERDICT_LABELS_CN == {"clean": "未发现", "suspect": "疑似", "nsfw": "高置信"}


def test_dist_line_fixed_order_regardless_of_input_order() -> None:
    """输出顺序固定 clean→suspect→nsfw(由轻到重),与入参键序无关。"""
    assert dist_line({"nsfw": 3, "clean": 2, "suspect": 1}) == "未发现 2 / 疑似 1 / 高置信 3"
    assert dist_line({"suspect": 1}) == "疑似 1"
    assert dist_line({"nsfw": 1, "clean": 1}) == "未发现 1 / 高置信 1"


def test_dist_line_keeps_zeros_and_unknown_keys() -> None:
    """计数 0 如实展示;未知档位键原样保留(追加在已知三档之后)。"""
    assert dist_line({"clean": 0, "suspect": 0, "nsfw": 0}) == "未发现 0 / 疑似 0 / 高置信 0"
    assert dist_line({"nsfw": 2, "weird": 1}) == "高置信 2 / weird 1"
    assert dist_line({"other": 5}) == "other 5"


def test_dist_line_defaults_for_empty_or_bad_input() -> None:
    """空分布 / None / 非字典 → "无数据";坏计数按 0。"""
    assert dist_line({}) == "无数据"
    assert dist_line(None) == "无数据"
    assert dist_line(42) == "无数据"  # type: ignore[arg-type]
    assert dist_line("clean=1") == "无数据"  # type: ignore[arg-type]
    assert dist_line({"clean": "许多"}) == "未发现 0"


# ---------------------------------------------------------------------------
# 展示常量
# ---------------------------------------------------------------------------


def test_display_constants() -> None:
    """表容量上限与徽章常量:页面文案固定口径的回归锚点。"""
    assert isinstance(MAX_RUNS_SHOWN, int) and MAX_RUNS_SHOWN >= 1
    assert len({BADGE_LOW, BADGE_MID, BADGE_HIGH, BADGE_UNKNOWN}) == 4
    assert "全压榨" in BADGE_HIGH  # 红线 35:高档=压榨本地计算,页面如实标注


# ---------------------------------------------------------------------------
# UI 守卫:streamlit 缺失时可导入,缺失时 main 打印中文提示退出码 1
# ---------------------------------------------------------------------------


def test_module_importable_without_streamlit() -> None:
    # 顶部已成功 import:即证明纯逻辑层不依赖 streamlit。
    assert isinstance(runs_page._HAS_ST, bool)
    for name in ("advice_tier", "cpu_advice", "dist_line", "main", "render", "run_rows", "tier_badge"):
        assert callable(getattr(runs_page, name)), name
    # __all__ 全部可解析:常量为既定类型、函数全部可调用
    assert set(runs_page.__all__) == {
        "ADVICE_HIGH", "ADVICE_LOW", "ADVICE_MID",
        "BADGE_HIGH", "BADGE_LOW", "BADGE_MID", "BADGE_UNKNOWN",
        "MAX_RUNS_SHOWN", "VERDICT_LABELS_CN",
        "advice_tier", "cpu_advice", "dist_line", "main",
        "render", "run_rows", "tier_badge",
    }


def test_main_without_streamlit_prints_hint_and_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    if runs_page._HAS_ST:  # pragma: no cover - 已装 streamlit 的环境跳过
        pytest.skip("本环境已安装 streamlit,缺失分支不可测")
    assert runs_page.main() == 1
    err = capsys.readouterr().err
    assert "Streamlit" in err and "pip install" in err
    assert "runs_page" in err  # 提示里给出本页面的启动命令
    assert not capsys.readouterr().out  # 提示只走 stderr


def test_render_without_streamlit_raises() -> None:
    if runs_page._HAS_ST:  # pragma: no cover - 已装 streamlit 的环境跳过
        pytest.skip("本环境已安装 streamlit,缺失分支不可测")
    with pytest.raises(RuntimeError, match="streamlit"):
        runs_page.render()


def test_streamlit_ui_guard() -> None:
    pytest.importorskip("streamlit")
    assert runs_page._HAS_ST is True
    assert callable(runs_page.render)
