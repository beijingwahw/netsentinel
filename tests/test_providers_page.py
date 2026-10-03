"""webui/providers_page.py 纯逻辑层测试(A76,离线,零外呼,不依赖 streamlit)。

- 只测纯函数(provider_rows / ping_row / agreement_rows / cost_rows /
  render_status_badge):本文件顶部成功 import 即证明纯逻辑层可独立导入
  (本机未装 streamlit 时同样成立);
- 密钥隔离:monkeypatch 清空全部提供方环境变量 + 把 ``pathlib.Path.home``
  指向 tmp 目录(隔离 ``~/.netsentinel/keys``),保证结论不受宿主机污染;
  再向 ``cfg.vlm_api_keys`` 注入密钥,断言行 dict 里**绝不回显密钥值**(红线 17);
- agreement_rows / cost_rows 用真实兄弟模块(A69 analyze / A75 CostMeter)
  在内存 / tmp 中构造输入,做小型集成校验;
- A220 追加段:weight_ci_rows / weight_point_rows / bayes_tracker_from_jsonl
  (贝叶斯权重区间行构造、点值回退、reliability.jsonl 确定性重建,注入
  固定事件流的 V13 tracker,全程离线);
- A232 追加段:batch_cost_rows(runs/*/cost.jsonl 批次成本快照行构造、
  目录名降序排序、空态/坏目录/坏文件回退,全程离线);
- 全程不联网、不访问真实门户、不启动 streamlit 服务。
"""
from __future__ import annotations

import json
import pathlib

import pytest

import webui.providers_page as providers_page
from webui.providers_page import (
    AGREE_NORMAL_NOTE,
    BADGE_FAIL,
    BADGE_OK,
    agreement_rows,
    cost_rows,
    ping_row,
    provider_rows,
    render_status_badge,
)

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, ImageScore

# ---------------------------------------------------------------------------
# 公共辅助:环境与家目录隔离
# ---------------------------------------------------------------------------

_ALL_PROVIDERS = (
    "glm", "openai", "anthropic", "gemini", "qwen", "doubao", "hunyuan",
    "moonshot", "minimax", "stepfun", "siliconflow", "ernie", "openrouter",
    "groq", "together", "xai", "ollama", "vllm", "lmstudio", "xinference",
)


@pytest.fixture()
def home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """把 pathlib.Path.home() 指向 tmp 目录,隔离真实家目录与密钥文件。"""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home_dir))
    return home_dir


@pytest.fixture()
def clean_env(home: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """清空全部提供方密钥环境变量(约定两形态 + 目录 key_envs 全覆盖)。"""
    for name in _ALL_PROVIDERS:
        upper = name.upper()
        monkeypatch.delenv(f"NETSENTINEL_{upper}_API_KEY", raising=False)
        monkeypatch.delenv(f"{upper}_API_KEY", raising=False)
    # 目录里额外的第二形态(DASHSCOPE / ARK / QIANFAN 等)
    for extra in ("DASHSCOPE_API_KEY", "ARK_API_KEY", "QIANFAN_API_KEY"):
        monkeypatch.delenv(extra, raising=False)


def _secret(tag: str) -> str:
    """构造形似真实密钥的注入用密钥值(只用于断言"绝不回显")。"""
    return f"sk-A76-{tag}-0000000000000000"


# ---------------------------------------------------------------------------
# provider_rows:20 行 / 布尔化 / 无密钥字面量 / 覆盖 / 缺席降级
# ---------------------------------------------------------------------------

def test_provider_rows_twenty_rows_with_exact_keys(clean_env) -> None:
    rows = provider_rows(Config())
    assert len(rows) == 20
    expect_keys = {"provider", "style", "default_model", "local", "key_configured", "base_url"}
    for row in rows:
        assert set(row) == expect_keys
        assert isinstance(row["provider"], str) and row["provider"]
        assert isinstance(row["style"], str) and row["style"]
        assert isinstance(row["default_model"], str)
        assert isinstance(row["base_url"], str) and row["base_url"]
        assert isinstance(row["local"], bool)          # 布尔化
        assert isinstance(row["key_configured"], bool)  # 布尔化,绝非密钥字符串
    # 顺序与目录一致
    from netsentinel.vision.providers import PROVIDERS

    assert [r["provider"] for r in rows] == list(PROVIDERS)


def test_provider_rows_values_match_catalog(clean_env) -> None:
    rows = {r["provider"]: r for r in provider_rows(Config())}
    assert rows["glm"]["style"] == "openai"
    assert rows["glm"]["default_model"] == "glm-5.3-flash"
    assert rows["glm"]["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert rows["anthropic"]["style"] == "anthropic"
    assert rows["gemini"]["style"] == "gemini"
    # 本地提供方:数据不出本机;vllm 无默认模型 = 必须显式指定
    assert rows["ollama"]["local"] is True
    assert rows["ollama"]["default_model"] == "llava"
    assert rows["vllm"]["default_model"] == ""
    # 本地标记恰好四家,云端全部 False
    assert {r["provider"] for r in rows.values() if r["local"]} == {
        "ollama", "vllm", "lmstudio", "xinference",
    }


def test_provider_rows_no_key_configured_in_clean_env(clean_env) -> None:
    rows = provider_rows(Config())
    assert all(r["key_configured"] is False for r in rows)  # 环境与家目录均已隔离


def test_provider_rows_key_state_via_env_and_no_leak(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NETSENTINEL_QWEN_API_KEY", _secret("ENV"))
    rows = {r["provider"]: r for r in provider_rows(Config())}
    assert rows["qwen"]["key_configured"] is True
    # 其余提供方仍为 False(优先级:cfg > 目录 key_envs > 文件)
    assert rows["glm"]["key_configured"] is False
    # 密钥值绝不回显
    assert _secret("ENV") not in json.dumps(rows, ensure_ascii=False)


def test_provider_rows_cfg_key_injection_never_echoes(clean_env) -> None:
    cfg = Config()
    cfg.vlm_api_keys = {
        "glm": _secret("CFG1"),
        "openai": _secret("CFG2"),
        "qwen": "   ",  # 纯空白视为未配置
    }
    rows = {r["provider"]: r for r in provider_rows(cfg)}
    assert rows["glm"]["key_configured"] is True
    assert rows["openai"]["key_configured"] is True
    assert rows["qwen"]["key_configured"] is False
    serialized = json.dumps(rows, ensure_ascii=False)
    assert _secret("CFG1") not in serialized
    assert _secret("CFG2") not in serialized
    assert "sk-A76-" not in serialized  # 任何注入密钥的字面前缀都不出现


def test_provider_rows_honor_cfg_overrides(clean_env) -> None:
    cfg = Config()
    cfg.vlm_provider_models = {"glm": "glm-4v-plus"}
    cfg.vlm_provider_base_urls = {"openai": "https://proxy.internal/v1"}
    rows = {r["provider"]: r for r in provider_rows(cfg)}
    assert rows["glm"]["default_model"] == "glm-4v-plus"
    assert rows["openai"]["base_url"] == "https://proxy.internal/v1"
    assert rows["qwen"]["default_model"] == "qwen-vl-max"  # 未覆盖项保持目录值


def test_provider_rows_tolerates_none_cfg(clean_env) -> None:
    assert len(provider_rows(None)) == 20


def test_provider_rows_empty_without_catalog(clean_env, monkeypatch) -> None:
    # 模拟 A61 目录缺席:包属性置空后降级返回 [](页面显示中文提示,不抛错)
    import netsentinel.vision

    monkeypatch.setattr(netsentinel.vision, "providers", None, raising=False)
    assert provider_rows(Config()) == []


def test_provider_rows_fallback_when_keys_module_absent(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 模拟 A70 缺席:降级为 cfg.vlm_api_keys + key_envs 环境变量兜底(仍只出布尔)
    import netsentinel.security

    monkeypatch.setattr(netsentinel.security, "keys", None, raising=False)
    monkeypatch.setenv("NETSENTINEL_GLM_API_KEY", _secret("FB"))
    cfg = Config()
    cfg.vlm_api_keys = {"together": _secret("CFG")}
    rows = {r["provider"]: r for r in provider_rows(cfg)}
    assert rows["glm"]["key_configured"] is True       # 环境变量兜底路径
    assert rows["together"]["key_configured"] is True  # cfg 注入兜底路径
    assert rows["openai"]["key_configured"] is False
    serialized = json.dumps(list(rows.values()), ensure_ascii=False)
    assert _secret("FB") not in serialized and _secret("CFG") not in serialized


# ---------------------------------------------------------------------------
# ping_row:各形态
# ---------------------------------------------------------------------------

def test_ping_row_success_form() -> None:
    raw = {
        "provider": "glm",
        "latency_ms": 812.34,
        "model": "glm-5.3-flash",
        "nsfw_prob": 0.0123,
        "parsed": True,
        "error": "",
        "reasoning": "无明显违规",
    }
    row = ping_row(raw)
    assert row["provider"] == "glm"
    assert row["ok"] is True
    assert row["latency_ms"] == pytest.approx(812.3)
    assert row["model"] == "glm-5.3-flash"
    assert "链路可用" in row["note"]
    assert "812.3" in row["note"] and "0.0123" in row["note"]


def test_ping_row_parse_failure_form() -> None:
    raw = {
        "provider": "openai",
        "latency_ms": 1200.0,
        "model": "gpt-4o-mini",
        "nsfw_prob": 0.0,
        "parsed": False,
        "error": "响应缺少 nsfw_prob 字段",
    }
    row = ping_row(raw)
    assert row["ok"] is False
    assert row["latency_ms"] == pytest.approx(1200.0)
    assert "响应缺少 nsfw_prob 字段" in row["note"]


def test_ping_row_exception_form() -> None:
    raw = {"provider": "qwen", "error": "调用失败(qwen:qwen-vl-max):无法连接对端"}
    row = ping_row(raw)
    assert row["provider"] == "qwen"
    assert row["ok"] is False
    assert row["latency_ms"] is None
    assert row["model"] == ""
    assert row["note"].startswith("调用失败")


def test_ping_row_explicit_ok_flag_wins() -> None:
    row = ping_row({"provider": "ollama", "ok": True, "latency_ms": 45})
    assert row["ok"] is True
    assert row["latency_ms"] == pytest.approx(45.0)
    row2 = ping_row({"provider": "ollama", "ok": False, "parsed": True})
    assert row2["ok"] is False  # 显式 ok 优先于 parsed


def test_ping_row_latency_garbage_becomes_none() -> None:
    assert ping_row({"provider": "x", "ok": True, "latency_ms": "很快"})["latency_ms"] is None
    assert ping_row({"provider": "x", "ok": True, "latency_ms": True})["latency_ms"] is None


def test_ping_row_empty_and_malformed_inputs() -> None:
    for raw in (None, {}, "不是字典", 123):
        row = ping_row(raw)  # type: ignore[arg-type]
        assert row["provider"] == ""
        assert row["ok"] is False
        assert row["latency_ms"] is None
        assert row["model"] == ""
        assert isinstance(row["note"], str) and row["note"]


# ---------------------------------------------------------------------------
# agreement_rows:A69 analyze 输出表格化(含离群注与空)
# ---------------------------------------------------------------------------

def _score(path: str, model: str, prob: float) -> ImageScore:
    return ImageScore(
        image=ImageEvidence(path=path, url=f"https://x.example/{path}", source_page="https://x.example/"),
        model=model,
        nsfw_prob=prob,
    )


def test_agreement_rows_from_real_analyze_with_outlier() -> None:
    from netsentinel.vision.provider_agreement import analyze

    scores = [
        _score("a.png", "glm:glm-5.3-flash", 0.20),
        _score("b.png", "glm:glm-5.3-flash", 0.20),
        _score("a.png", "openai:gpt-4o-mini", 0.25),
        _score("b.png", "openai:gpt-4o-mini", 0.25),
        _score("a.png", "qwen:qwen-vl-max", 0.55),
        _score("b.png", "qwen:qwen-vl-max", 0.55),
    ]
    rows = agreement_rows(analyze(scores))
    assert len(rows) == 3
    assert {r["provider"] for r in rows} == {"glm", "openai", "qwen"}
    # 行序按 |平均偏差| 降序:离群的 qwen 排最前
    assert rows[0]["provider"] == "qwen"
    assert rows[0]["outlier"] is True
    assert rows[0]["bias"] == pytest.approx(0.2167, abs=1e-3)
    assert "建议人工抽检" in rows[0]["note"]
    for row in rows[1:]:
        assert row["outlier"] is False
        assert row["note"] == AGREE_NORMAL_NOTE
        assert isinstance(row["bias"], float)


def test_agreement_rows_empty_analysis() -> None:
    from netsentinel.vision.provider_agreement import analyze

    assert agreement_rows(analyze([])) == []  # A69 空输入 → 全空结构


def test_agreement_rows_tolerant_of_bad_input() -> None:
    assert agreement_rows(None) == []
    assert agreement_rows({}) == []
    assert agreement_rows({"providers": ["a"], "outliers": []}) == []  # 无 bias
    # bias 存在但值非法 → 按 0 处理,行仍产出(不抛错)
    rows = agreement_rows({"bias": {"a": "bad", "b": 0.05}, "outliers": []})
    assert [r["provider"] for r in rows] == ["b", "a"]  # |0.05| > |0|
    assert rows[1]["bias"] == 0.0
    # outliers 形态异常不影响主行
    rows2 = agreement_rows({"bias": {"a": 0.3}, "outliers": [{"provider": "a", "note": "系统性偏高"}]})
    assert rows2[0]["outlier"] is True
    assert rows2[0]["note"] == "系统性偏高"


# ---------------------------------------------------------------------------
# cost_rows:A75 summary.by_provider → 行(数值格式)
# ---------------------------------------------------------------------------

def test_cost_rows_nested_by_provider_form() -> None:
    summary = {
        "by_provider": {
            "openai": {"calls": 1, "images": 2, "est_cost": None, "unpriced_calls": 1},
            "glm": {"calls": 3, "images": 10, "est_cost": 0.05, "unpriced_calls": 0},
        },
        "total_est": 0.05,
        "unpriced_calls": 1,
    }
    rows = cost_rows(summary)
    assert [r["provider"] for r in rows] == ["glm", "openai"]  # 按名称排序
    glm, openai = rows
    assert glm["calls"] == 3 and isinstance(glm["calls"], int)
    assert glm["images"] == 10 and isinstance(glm["images"], int)
    assert glm["est_cost"] == pytest.approx(0.05)
    assert isinstance(glm["est_cost"], float)
    assert glm["unpriced_calls"] == 0
    assert openai["est_cost"] is None  # 无价格提示保持 None,绝不猜价
    assert openai["unpriced_calls"] == 1


def test_cost_rows_flat_contract_form_and_coercion() -> None:
    flat = {
        "glm": {"calls": 2.0, "images": 7.9, "est_cost": 0.02, "unpriced_calls": 0},
        "weird": {"calls": "三次", "images": True, "est_cost": False},
    }
    rows = cost_rows(flat)
    by = {r["provider"]: r for r in rows}
    assert by["glm"]["calls"] == 2 and by["glm"]["images"] == 7  # 数值取整
    assert by["weird"]["calls"] == 0     # 非数值按 0
    assert by["weird"]["images"] == 0    # bool 不算数值
    assert by["weird"]["est_cost"] is None  # bool est_cost → None(非数值)
    # 非法槽(非 dict)整行跳过
    assert cost_rows({"by_provider": {"x": None, "glm": {"calls": 1}}}) == [
        {"provider": "glm", "calls": 1, "images": 0, "est_cost": None, "unpriced_calls": 0}
    ]


def test_cost_rows_empty_inputs() -> None:
    assert cost_rows(None) == []
    assert cost_rows({}) == []
    assert cost_rows({"by_provider": {}}) == []
    assert cost_rows({"total_est": 0.0}) == []


def test_cost_rows_against_real_cost_meter(tmp_path: pathlib.Path) -> None:
    from netsentinel.vision.cost_meter import CostMeter

    meter = CostMeter(tmp_path / "vlm_cost.jsonl")
    meter.record("glm", "glm-5.3-flash", 4)        # 有价格提示:5.0 × 4 / 1000 = 0.02
    meter.record("ollama", "llava", 3)             # 本地提供方:0.0
    meter.record("xai", "grok-2-vision-1212", 2)   # 无价格提示:None + unpriced
    rows = {r["provider"]: r for r in cost_rows(meter.summary())}
    assert rows["glm"]["est_cost"] == pytest.approx(0.02)
    assert rows["glm"]["unpriced_calls"] == 0
    assert rows["ollama"]["est_cost"] == pytest.approx(0.0)
    # 无价格提示的调用:汇总口径 est_cost 按 0 计,但单独计入 unpriced_calls
    assert rows["xai"]["est_cost"] == pytest.approx(0.0)
    assert rows["xai"]["unpriced_calls"] == 1


# ---------------------------------------------------------------------------
# render_status_badge:徽章文案
# ---------------------------------------------------------------------------

def test_render_status_badge_text() -> None:
    assert render_status_badge(True) == BADGE_OK == "✅ 正常"
    assert render_status_badge(False) == BADGE_FAIL == "❌ 异常"
    assert render_status_badge(1) == "✅ 正常"   # 真值强制布尔化
    assert render_status_badge(0) == "❌ 异常"
    assert render_status_badge("") == "❌ 异常"


# ---------------------------------------------------------------------------
# UI 守卫:streamlit 缺失时可导入,缺失时 main 打印中文提示退出码 1
# ---------------------------------------------------------------------------

def test_module_importable_without_streamlit() -> None:
    # 顶部已成功 import:即证明纯逻辑层不依赖 streamlit。
    assert isinstance(providers_page._HAS_ST, bool)
    assert callable(providers_page.render)
    assert callable(providers_page.main)


def test_main_without_streamlit_prints_hint_and_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    if providers_page._HAS_ST:  # pragma: no cover - 已装 streamlit 的环境跳过
        pytest.skip("本环境已安装 streamlit,缺失分支不可测")
    assert providers_page.main() == 1
    err = capsys.readouterr().err
    assert "Streamlit" in err and "pip install" in err
    assert "providers_page" in err  # 提示里给出本页面的启动命令


def test_render_without_streamlit_raises() -> None:
    if providers_page._HAS_ST:  # pragma: no cover - 已装 streamlit 的环境跳过
        pytest.skip("本环境已安装 streamlit,缺失分支不可测")
    with pytest.raises(RuntimeError, match="streamlit"):
        providers_page.render()


def test_streamlit_ui_guard() -> None:
    pytest.importorskip("streamlit")
    assert providers_page._HAS_ST is True
    assert callable(providers_page.render)


# ---------------------------------------------------------------------------
# V5 升级(A101):ping_row 的人工诊断遥测(providers_page.ping)
# ---------------------------------------------------------------------------

def test_v5_ping_row_counts_telemetry() -> None:
    telemetry.reset()
    ping_row({"provider": "glm", "ok": True, "latency_ms": 45})
    ping_row(None)  # 形态不完整同样经 ping_row 规整(页面不崩),照常计数
    assert telemetry.snapshot()["counters"].get("providers_page.ping") == 2.0


# ---------------------------------------------------------------------------
# A220 增强:贝叶斯权重区间行(weight_ci_rows / weight_point_rows /
# bayes_tracker_from_jsonl)——注入 tracker 与 dict 双形态、点值回退、空态
# ---------------------------------------------------------------------------

from netsentinel.decision.reliability import (  # noqa: E402 追加段统一在此导入
    BayesianReliabilityTracker,
    ReliabilityTracker,
)

from webui.providers_page import (  # noqa: E402
    bayes_tracker_from_jsonl,
    weight_ci_rows,
    weight_point_rows,
)


def _bayes_tracker() -> BayesianReliabilityTracker:
    """确定性贝叶斯 tracker:glm 六连对、stub 六连错(无遗忘,ts 滴答)。"""
    tracker = BayesianReliabilityTracker(half_life=None)
    for i in range(6):
        tracker.record("glm", True, ts=float(2 * i + 1))
        tracker.record("stub", False, ts=float(2 * i + 2))
    return tracker


def test_weight_ci_rows_from_injected_bayes_tracker() -> None:
    tracker = _bayes_tracker()
    rows = weight_ci_rows(tracker)
    assert len(rows) == 2
    for row in rows:
        assert set(row) == {"member", "weight", "ci_lo", "ci_hi"}
    # glm 全对 → 权重点值压倒性占优,排最前;权重归一 Σ=1
    assert rows[0]["member"] == "glm"
    assert rows[1]["member"] == "stub"
    total = rows[0]["weight"] + rows[1]["weight"]
    assert total == pytest.approx(1.0)
    assert rows[0]["weight"] > 0.5 > rows[1]["weight"]
    # 与 V13 weights_with_ci 的原始输出一致(4 位小数展示规整)
    with_ci = tracker.weights_with_ci()
    assert rows[0]["weight"] == pytest.approx(with_ci["glm"]["weight"], abs=5e-5)
    assert rows[0]["ci_lo"] == pytest.approx(with_ci["glm"]["ci95"][0], abs=5e-5)
    assert rows[0]["ci_hi"] == pytest.approx(with_ci["glm"]["ci95"][1], abs=5e-5)
    # 区间覆盖合理性:lo < hi,且包含未归一后验均值
    for row in rows:
        assert 0.0 <= row["ci_lo"] < row["ci_hi"] <= 1.0
        assert row["ci_lo"] < with_ci[row["member"]]["mean"] < row["ci_hi"]


def test_weight_ci_rows_from_dict_shape_matches_tracker() -> None:
    with_ci = _bayes_tracker().weights_with_ci()
    assert weight_ci_rows(dict(with_ci)) == weight_ci_rows(_bayes_tracker())


def test_weight_ci_rows_point_fallback_when_ci_missing() -> None:
    # 零区间数据(权重在、ci95 缺席/形态不对)→ 点值保留,区间列 None(UI 显示"-")
    rows = weight_ci_rows({"glm": {"weight": 0.625, "n_eff": 8}})
    assert rows == [{"member": "glm", "weight": 0.625, "ci_lo": None, "ci_hi": None}]
    rows2 = weight_ci_rows({"a": {"weight": 0.5, "ci95": "bad"}, "b": {"weight": 0.5, "ci95": [0.1]}})
    assert all(r["ci_lo"] is None and r["ci_hi"] is None for r in rows2)


def test_weight_ci_rows_sorting_and_tolerance() -> None:
    # 行序:权重降序,同值按成员名(确定性)
    rows = weight_ci_rows(
        {"b": {"weight": 0.5, "ci95": [0.1, 0.9]}, "a": {"weight": 0.5, "ci95": [0.2, 0.8]},
         "c": {"weight": 0.9, "ci95": [0.7, 0.99]}}
    )
    assert [r["member"] for r in rows] == ["c", "a", "b"]
    # 空态 / 形态不符 / 坏条目:绝不抛错
    assert weight_ci_rows(None) == []
    assert weight_ci_rows(123) == []
    assert weight_ci_rows({}) == []
    assert weight_ci_rows({"glm": "bad"}) == []            # 槽非 dict → 跳过
    assert weight_ci_rows({"glm": {"weight": None}}) == []  # 无点值 → 整行跳过
    assert weight_ci_rows({"": {"weight": 0.5}}) == []      # 空成员名 → 跳过
    # tracker 调用异常按数据缺席处理
    class _Boom:
        def weights_with_ci(self):  # noqa: N802 - 鸭子接口
            raise RuntimeError("boom")

    assert weight_ci_rows(_Boom()) == []


def test_weight_point_rows_fallback_brier_and_bayes() -> None:
    # Brier 点值回退:样本充足两家出两行;None(样本不足)行跳过
    brier = ReliabilityTracker()  # 纯内存
    for _ in range(6):
        brier.record("good", 1.0, True)
        brier.record("bad", 1.0, False)
    brier.record("tiny", 1.0, True)  # n=1 < MIN_N → weights() 为 None
    rows = weight_point_rows(brier)
    assert [r["member"] for r in rows] == ["good", "bad"]
    assert all(set(r) == {"member", "weight"} for r in rows)
    assert rows[0]["weight"] == pytest.approx(21 / 22, abs=5e-5)  # 20 vs 20/21 归一
    # 贝叶斯 tracker 同接口可用(weights() 恒数值)
    bayes_rows = weight_point_rows(_bayes_tracker())
    assert [r["member"] for r in bayes_rows] == ["glm", "stub"]
    # dict 形态 / 容错
    assert weight_point_rows({"a": 0.4, "b": 0.6})[0]["member"] == "b"
    assert weight_point_rows(None) == []
    assert weight_point_rows({"a": None}) == []
    assert weight_point_rows({"a": "bad"}) == []


def test_bayes_tracker_from_jsonl(tmp_path: pathlib.Path) -> None:
    lines = [
        json.dumps({"provider": "glm", "p": 0.9, "outcome": True}, ensure_ascii=False),
        json.dumps({"provider": "glm", "p": 0.8, "outcome": 1}, ensure_ascii=False),  # 0/1 容忍
        "",
        "不是 json 的坏行",
        json.dumps({"provider": "stub", "p": 0.9, "outcome": False}, ensure_ascii=False),
        json.dumps({"p": 0.5, "outcome": True}, ensure_ascii=False),  # 缺 provider → 跳过
        json.dumps({"provider": "x", "p": "高", "outcome": True}, ensure_ascii=False),  # p 非法
    ]
    # 每成员补足 ≥ MIN_N 条有效记录(冷启动收缩会把样本不足的成员拉平到池均值)
    lines += [json.dumps({"provider": "glm", "p": 0.7, "outcome": True}, ensure_ascii=False)] * 4
    lines += [json.dumps({"provider": "stub", "p": 0.85, "outcome": False}, ensure_ascii=False)] * 5
    path = tmp_path / "reliability.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tracker = bayes_tracker_from_jsonl(str(path))
    assert tracker is not None
    rows = weight_ci_rows(tracker)
    # glm 六条全对(p≥0.5 且确为违规),stub 六条全错(p≥0.5 实为正常)→ glm 权重更高
    assert [r["member"] for r in rows] == ["glm", "stub"]
    assert rows[0]["weight"] > 0.5 > rows[1]["weight"]
    # 与手工重建(同样事件流)一致:换算口径 (p>=0.5)==outcome、无遗忘、ts 逐条滴答
    manual = BayesianReliabilityTracker(half_life=None)
    for i in range(6):
        manual.record("glm", True, ts=float(i + 1))
    for i in range(6):
        manual.record("stub", False, ts=float(7 + i))
    assert rows == weight_ci_rows(manual)


def test_bayes_tracker_from_jsonl_absent_inputs(tmp_path: pathlib.Path) -> None:
    assert bayes_tracker_from_jsonl(str(tmp_path / "nope.jsonl")) is None  # 文件缺失
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert bayes_tracker_from_jsonl(str(empty)) is None  # 无有效行
    corrupt = tmp_path / "corrupt.jsonl"
    corrupt.write_text("垃圾行\n还是垃圾\n", encoding="utf-8")
    assert bayes_tracker_from_jsonl(str(corrupt)) is None  # 全坏行
    assert bayes_tracker_from_jsonl(str(tmp_path)) is None  # 路径是目录(读失败)


def test_weight_ci_rows_counts_telemetry() -> None:
    telemetry.reset()
    weight_ci_rows(None)
    weight_ci_rows({"a": {"weight": 0.5}})
    assert telemetry.snapshot()["counters"].get("providers_page.weight_ci") == 2.0


# ---------------------------------------------------------------------------
# A230 升级段:bayes_tracker_from_jsonl 重建口径(行内 ts 优先 + 半衰期对齐
# cfg.bayes_half_life;缺省关闭遗忘的展示近似保留;行构造层形状不变)
# ---------------------------------------------------------------------------


def test_bayes_tracker_consumes_inline_ts_and_half_life(
    tmp_path: pathlib.Path,
) -> None:
    """带 ts 流 + cfg.bayes_half_life:与手工重建逐位一致;遗忘真实参与。"""
    ts0 = 1_700_000_000.0
    recent = ts0 + 30 * 86400.0  # 30 天后(半衰期 1 天 → 早期事件衰减殆尽)
    lines = [
        # glm 早期两次误报(可被遗忘衰减),近期六连对。
        *([json.dumps({"provider": "glm", "p": 0.9, "outcome": False, "ts": ts0})] * 2),
        json.dumps({"provider": "glm", "p": 0.9, "outcome": True, "ts": recent}),
        # stub 新近六连错(不衰减)。
        *([json.dumps({"provider": "stub", "p": 0.9, "outcome": False, "ts": recent})] * 6),
        *([json.dumps({"provider": "glm", "p": 0.8, "outcome": True, "ts": recent})] * 5),
    ]
    path = tmp_path / "reliability.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    cfg = Config()
    cfg.bayes_half_life = 1.0  # 天
    tracker = bayes_tracker_from_jsonl(str(path), cfg)
    assert tracker is not None
    assert tracker.half_life == pytest.approx(86400.0)  # 天 → 秒,与 ts 同量纲

    manual = BayesianReliabilityTracker(half_life=86400.0)
    for _ in range(2):
        manual.record("glm", False, ts=ts0)
    for _ in range(6):
        manual.record("stub", False, ts=recent)
    for _ in range(6):
        manual.record("glm", True, ts=recent)
    assert weight_ci_rows(tracker) == weight_ci_rows(manual)

    # 半衰期真实参与:遗忘开启后 glm 的早期误报衰减,权重高于关闭遗忘的重建。
    no_forget = bayes_tracker_from_jsonl(str(path))  # cfg 缺省 → 展示近似
    assert no_forget.half_life is None
    rows_forget = {r["member"]: r for r in weight_ci_rows(tracker)}
    rows_none = {r["member"]: r for r in weight_ci_rows(no_forget)}
    assert rows_forget["glm"]["weight"] > rows_none["glm"]["weight"]
    assert rows_forget["stub"]["weight"] < rows_none["stub"]["weight"]


def test_bayes_tracker_ts_fallback_counted(tmp_path: pathlib.Path) -> None:
    """缺 ts 行滴答回退并计数(混合流与手工重建一致,滴答序 = 缺省口径)。"""
    telemetry.reset()
    lines = [
        json.dumps({"provider": "glm", "p": 0.9, "outcome": True}),          # 缺 ts
        json.dumps({"provider": "glm", "p": 0.9, "outcome": True}),          # 缺 ts
        json.dumps({"provider": "glm", "p": 0.9, "outcome": True}),          # 缺 ts
        json.dumps({"provider": "glm", "p": 0.9, "outcome": True, "ts": 123.0}),
    ]
    path = tmp_path / "reliability.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tracker = bayes_tracker_from_jsonl(str(path))
    assert tracker is not None
    counters = telemetry.snapshot()["counters"]
    assert counters.get("providers_page.bayes.ts_fallback") == 3.0  # 三行回退
    manual = BayesianReliabilityTracker(half_life=None)
    manual.record("glm", True)  # ts=1(滴答)
    manual.record("glm", True)  # ts=2
    manual.record("glm", True)  # ts=3
    manual.record("glm", True, ts=123.0)
    assert weight_ci_rows(tracker) == weight_ci_rows(manual)
    telemetry.reset()


def test_bayes_tracker_invalid_ts_rows_skipped(tmp_path: pathlib.Path) -> None:
    """ts 存在但非法(字符串 / bool / NaN)→ 整行跳过,不拖垮重建。"""
    lines = [
        json.dumps({"provider": "glm", "p": 0.9, "outcome": True, "ts": "bad"}),
        json.dumps({"provider": "glm", "p": 0.9, "outcome": True, "ts": True}),
        json.dumps({"provider": "glm", "p": 0.9, "outcome": True, "ts": float("nan")}),
        json.dumps({"provider": "glm", "p": 0.9, "outcome": True, "ts": 100.0}),
    ]
    path = tmp_path / "reliability.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tracker = bayes_tracker_from_jsonl(str(path), Config())
    assert tracker is not None
    manual = BayesianReliabilityTracker(half_life=None)
    manual.record("glm", True, ts=100.0)  # 仅合法行生效
    assert weight_ci_rows(tracker) == weight_ci_rows(manual)


def test_bayes_tracker_cfg_defaults_keep_approximation(
    tmp_path: pathlib.Path,
) -> None:
    """getattr 防御:cfg None / 缺字段 / None / 非正值 → half_life None(展示近似)。"""
    path = tmp_path / "reliability.jsonl"
    path.write_text(
        json.dumps({"provider": "glm", "p": 0.9, "outcome": True}) + "\n",
        encoding="utf-8",
    )
    assert bayes_tracker_from_jsonl(str(path)).half_life is None          # cfg 缺省
    assert bayes_tracker_from_jsonl(str(path), Config()).half_life is None  # 字段缺省 None

    class _NoAttr:  # 旧注入对象缺字段:getattr 防御
        pass

    assert bayes_tracker_from_jsonl(str(path), _NoAttr()).half_life is None
    cfg_bad = Config()
    cfg_bad.bayes_half_life = 0.0  # 非正值 → 按关闭遗忘处理
    assert bayes_tracker_from_jsonl(str(path), cfg_bad).half_life is None


# ---------------------------------------------------------------------------
# A232 追加段:batch_cost_rows(runs/*/cost.jsonl 批次成本快照 → 批次行)
# ---------------------------------------------------------------------------
from webui.providers_page import (  # noqa: E402
    BATCH_COST_HEADERS_ZH,
    batch_cost_rows,
)


def _write_cost_snapshot(runs_dir: pathlib.Path, run_id: str) -> None:
    """按 finishflow 批次成本归集的落盘格式写一份快照(meta + agg + totals)。"""
    import json as _json

    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        {"kind": "meta", "run_id": run_id,
         "generated_at": "2026-10-03T12:00:00+08:00",
         "ledger": "vlm_cost.jsonl", "records": 3},
        {"kind": "agg", "by": "model", "model": "openai:gpt-4o-mini",
         "provider": "openai", "calls": 2, "images": 1500, "tokens": 1024,
         "duration_s": 2.5, "est_cost": 15.0, "unpriced_calls": 0},
        {"kind": "agg", "by": "model", "model": "together:Llama-4-Scout",
         "provider": "together", "calls": 1, "images": 300, "tokens": 0,
         "duration_s": 0.0, "est_cost": 0.0, "unpriced_calls": 1},
        {"kind": "agg", "by": "run_id", "run_id": "(未标记批次)",
         "calls": 1, "images": 1000, "tokens": 0, "duration_s": 0.0,
         "est_cost": 10.0, "unpriced_calls": 0},
        {"kind": "agg", "by": "run_id", "run_id": run_id,
         "calls": 2, "images": 800, "tokens": 1024, "duration_s": 2.5,
         "est_cost": 5.0, "unpriced_calls": 1},
        {"kind": "totals", "by": "model", "calls": 3, "images": 2800,
         "tokens": 1024, "duration_s": 2.5, "est_cost": 15.0,
         "unpriced_calls": 1},
        {"kind": "totals", "by": "run_id", "calls": 3, "images": 2800,
         "tokens": 1024, "duration_s": 2.5, "est_cost": 15.0,
         "unpriced_calls": 1},
    ]
    with (run_dir / "cost.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for obj in lines:
            fh.write(_json.dumps(obj, ensure_ascii=False) + "\n")


def test_batch_cost_rows_builds_rows_from_snapshots(tmp_path: pathlib.Path) -> None:
    """行构造:meta 定批次;模型数 = by=model 明细行数;总调用/总费用/
    unpriced = 该批(by=run_id)那行(批次专属口径,手算对照)。"""
    runs = tmp_path / "runs"
    _write_cost_snapshot(runs, "run-x")
    rows = batch_cost_rows(runs)
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == set(BATCH_COST_HEADERS_ZH)   # 行字段与中文表头一一对应
    assert row["run_id"] == "run-x"
    assert row["models"] == 2          # by=model 明细两行(openai / together)
    assert row["calls"] == 2           # run-x 那行(未标记批的 1 次不计入)
    assert row["est_cost"] == pytest.approx(5.0)
    assert row["unpriced_calls"] == 1


def test_batch_cost_rows_order_newest_first_and_headers(tmp_path: pathlib.Path) -> None:
    """多批:目录名降序(时间戳标识最新在前);中文表头五列齐备。"""
    runs = tmp_path / "runs"
    _write_cost_snapshot(runs, "run-20261002-090000")
    _write_cost_snapshot(runs, "run-20261003-120000")
    rows = batch_cost_rows(runs)
    assert [r["run_id"] for r in rows] == [
        "run-20261003-120000", "run-20261002-090000",
    ]
    assert BATCH_COST_HEADERS_ZH == {
        "run_id": "批次", "models": "模型数", "calls": "总调用",
        "est_cost": "总费用(元)", "unpriced_calls": "无价格提示调用",
    }


def test_batch_cost_rows_empty_and_missing_states(tmp_path: pathlib.Path) -> None:
    """空态与回退:目录缺席 / 空目录 / 无 cost.jsonl / 入参 None → []。"""
    assert batch_cost_rows(tmp_path / "nope") == []      # 目录缺席
    assert batch_cost_rows(None) == []
    empty = tmp_path / "runs"
    empty.mkdir()
    assert batch_cost_rows(empty) == []                 # 空目录
    (empty / "run-y").mkdir()                           # 有子目录但无快照
    assert batch_cost_rows(empty) == []


def test_batch_cost_rows_bad_directory_is_file(tmp_path: pathlib.Path) -> None:
    """坏目录:runs_dir 是文件而非目录 → 空态回退,绝不抛错。"""
    target = tmp_path / "not-a-dir"
    target.write_text("我是文件\n", encoding="utf-8")
    assert batch_cost_rows(target) == []


def test_batch_cost_rows_corrupt_snapshot_falls_back(tmp_path: pathlib.Path) -> None:
    """坏文件容错:垃圾行跳过、meta 缺 run_id 回退目录名、缺批明细按 0 诚实。"""
    import json as _json

    runs = tmp_path / "runs" / "run-broken"
    runs.mkdir(parents=True)
    lines = [
        "不是 json 的坏行",
        _json.dumps({"kind": "meta", "ledger": "vlm_cost.jsonl"}),  # 无 run_id
        _json.dumps(["数组不是对象"]),
        _json.dumps({"kind": "agg", "by": "model", "model": "glm:glm-4v-flash",
                     "calls": 1, "est_cost": 0.0}),                 # 仅模型行
    ]
    with (runs / "cost.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    rows = batch_cost_rows(tmp_path / "runs")
    assert rows == [{"run_id": "run-broken", "models": 1, "calls": 0,
                     "est_cost": 0.0, "unpriced_calls": 0}]
