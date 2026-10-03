"""A36 单元测试:netsentinel.submit.llm_describer(全离线,FakeClient / 假模块注入)。

覆盖点:
- 在线路径:FakeClient 返回 description → 带 “[AI 草拟” 前缀、短文不截断;
  messages 组装(system=DESCRIBER_SYSTEM、user 含事实清单)、纯文本调用
  (image_paths=None,不出图片);
- 长度红线:300 字 description → 连前缀整体截断到 240;
- 回退路径:description 缺失 / 空串 / 非字符串 / 返回非 JSON → 确定性模板;
- chat_json 抛异常 → 回退模板(含站点与“人工核实”)并记 warning;
- 离线分支:client=None 且 glm_adapter 缺失 / VlmOfflineError / 无密钥 → 回退;
- 字符串 JSON 返回(```json 围栏)→ 正常解析出 description;
- vlm_prompts 注入点:假模块的 DESCRIBER_SYSTEM / build_user_prompt 生效;
  vlm_prompts 缺失 → 内置精简系统提示词兜底;
- _collect_facts:数值两位小数、intel 要点入清单、行数 ≤12、缺失字段容错;
- 安全红线:两条路径都含 “[AI 草拟” 前缀与 “人工核实” 字样。

零外呼:所有客户端均为注入的假对象或缺省离线分支,不触碰 urllib / 网络。
"""
from __future__ import annotations

import logging
import sys
import types

import pytest

from netsentinel.contracts import Config, PageSample, SiteReport, Verdict
from netsentinel.submit.llm_describer import (
    AI_DRAFT_PREFIX,
    MAX_DESCRIPTION_CHARS,
    MAX_FACT_LINES,
    _collect_facts,
    draft_description,
)

vlm_prompts = pytest.importorskip("netsentinel.vision.vlm_prompts")

_ADAPTER_NAME = "netsentinel.vision.glm_adapter"
_PROMPTS_NAME = "netsentinel.vision.vlm_prompts"
_ENV_KEY = "NETSENTINEL_GLM_API_KEY"
_SITE = "https://bad.example.com/zone"


def _has_cjk(text: str) -> bool:
    """判断文案是否含中文字符。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


class FakeClient:
    """可编程假客户端:记录 (messages, image_paths),按配置返回 payload 或抛异常。"""

    def __init__(self, payload=None, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls: list[tuple[list[dict], object]] = []

    def chat_json(self, messages, *, image_paths=None):
        self.calls.append((messages, image_paths))
        if self.error is not None:
            raise self.error
        return self.payload


def _make_entry(**overrides) -> SiteReport:
    """构造典型 NSFW 站点报告(3 个抽样页面)。"""
    report = SiteReport(
        site_url=_SITE,
        pages=[PageSample(url=f"{_SITE}/p{i}") for i in range(3)],
        agg_nsw_prob=0.966,
        nsw_image_count=5,
        verdict=Verdict.NSFW,
    )
    for key, value in overrides.items():
        setattr(report, key, value)
    return report


def _make_intel() -> dict:
    """构造 A29 fusion 写入形态的 intel(url/text explain + fusion.prob)。"""
    return {
        "url": {
            "risk": 0.8,
            "explain": ["域名使用可疑顶级域 xyz", "子域深度超过 3 层", "第三条要点", "第四条要点"],
        },
        "text": {
            "risk": 0.7,
            "explain": ["命中色情关键词 6 处", "页面含诱导性短语"],
        },
        "page_vlm": {"page_nsfw_prob": 0.81},
        "fusion": {"prob": 0.934, "rule": "只升不降"},
    }


# ---------------------------------------------------------------------------
# 在线路径(FakeClient 注入)
# ---------------------------------------------------------------------------
def test_draft_with_fake_client_prefix_and_body() -> None:
    body = "该站点多张抽样图片含色情低俗内容,证据材料见附件。以上情况本人已人工核实。"
    client = FakeClient(payload={"description": body})
    result = draft_description(_make_entry(), _make_intel(), Config(), client=client)

    assert result.startswith(AI_DRAFT_PREFIX)
    assert body in result  # 短文不截断
    assert len(result) <= MAX_DESCRIPTION_CHARS
    assert len(client.calls) == 1

    messages, image_paths = client.calls[0]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == vlm_prompts.DESCRIBER_SYSTEM  # A22 系统提示词
    # 用户消息由 build_user_prompt("describer", facts=...) 生成,含事实清单
    assert "站点:" + _SITE in messages[1]["content"]
    assert "0.97" in messages[1]["content"]  # agg 0.966 → 两位小数
    assert "URL 风险要点:域名使用可疑顶级域 xyz" in messages[1]["content"]
    assert image_paths is None  # 纯文本调用,不外发图片


def test_long_description_truncated_to_240() -> None:
    body = "举" * 300
    client = FakeClient(payload={"description": body})
    result = draft_description(_make_entry(), {}, Config(), client=client)

    assert len(result) == MAX_DESCRIPTION_CHARS == 240
    assert result == (AI_DRAFT_PREFIX + body)[:MAX_DESCRIPTION_CHARS]
    assert result.startswith(AI_DRAFT_PREFIX)


def test_string_json_payload_parsed() -> None:
    """client 返回 JSON 字符串(带 ```json 围栏)→ 也能取出 description。"""
    payload = '```json\n{"description": "字符串返回的草拟正文。"}\n```'
    client = FakeClient(payload=payload)
    result = draft_description(_make_entry(), {}, Config(), client=client)
    assert "字符串返回的草拟正文。" in result
    assert result.startswith(AI_DRAFT_PREFIX)


def test_builtin_prompt_when_vlm_prompts_missing(monkeypatch) -> None:
    """vlm_prompts 未就位(sys.modules 置 None)→ 内置精简系统提示词兜底。"""
    monkeypatch.setitem(sys.modules, _PROMPTS_NAME, None)
    client = FakeClient(payload={"description": "内置提示词路径的正文。"})
    result = draft_description(_make_entry(), {}, Config(), client=client)

    assert "内置提示词路径的正文。" in result
    messages, _ = client.calls[0]
    assert "草拟" in messages[0]["content"]  # 内置系统提示词
    assert "站点:" + _SITE in messages[1]["content"]  # 内置用户提示词仍带事实清单


def test_fake_vlm_prompts_injection_points(monkeypatch) -> None:
    """注入假 vlm_prompts:DESCRIBER_SYSTEM 与 build_user_prompt 均生效。"""
    mod = types.ModuleType(_PROMPTS_NAME)
    mod.DESCRIBER_SYSTEM = "SYS-MARK-描述器系统提示"

    def _build_user_prompt(kind, **ctx):
        return f"USER-MARK:{kind}:{ctx.get('facts')}"

    mod.build_user_prompt = _build_user_prompt
    monkeypatch.setitem(sys.modules, _PROMPTS_NAME, mod)

    client = FakeClient(payload={"description": "正文"})
    draft_description(_make_entry(), {}, Config(), client=client)
    messages, _ = client.calls[0]
    assert messages[0]["content"] == "SYS-MARK-描述器系统提示"
    assert messages[1]["content"].startswith("USER-MARK:describer:")
    assert "站点:" + _SITE in messages[1]["content"]


# ---------------------------------------------------------------------------
# 回退路径:description 缺失 / 非法 → 确定性模板
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        {"foo": "bar"},            # 缺 description
        {"description": ""},       # 空串
        {"description": "   "},    # 纯空白
        {"description": 123},      # 非字符串
        None,                      # 非 dict 非 str
        "这完全不是 JSON",          # 字符串但解析失败
    ],
    ids=["missing", "empty", "blank", "non-str", "none", "garbage"],
)
def test_invalid_payload_falls_back_to_template(payload) -> None:
    client = FakeClient(payload=payload)
    result = draft_description(_make_entry(), _make_intel(), Config(), client=client)

    assert result.startswith(AI_DRAFT_PREFIX)
    assert _SITE in result                       # 模板含站点
    assert "高置信色情" in result                  # 模板含判定
    assert result.endswith("以上情况本人已人工核实。")
    assert len(result) <= MAX_DESCRIPTION_CHARS
    assert _has_cjk(result)


def test_client_exception_falls_back(caplog) -> None:
    client = FakeClient(error=RuntimeError("连接超时"))
    with caplog.at_level(logging.WARNING, logger="netsentinel.submit.llm_describer"):
        result = draft_description(_make_entry(), _make_intel(), Config(), client=client)

    assert _SITE in result
    assert "人工核实" in result
    assert "[AI 草拟" in result
    assert result.endswith("以上情况本人已人工核实。")
    assert any("调用失败" in r.getMessage() for r in caplog.records)


def test_fallback_template_contents() -> None:
    """回退模板完整要素:站点/判定/agg/达标图数/页面数/URL 与文本风险/人工核实。"""
    result = draft_description(
        _make_entry(), _make_intel(), Config(), client=FakeClient(error=RuntimeError("离线"))
    )
    assert "举报站点:" + _SITE in result
    assert "系统初筛判定:高置信色情(nsfw)" in result
    assert "0.97" in result                 # agg 0.966 → 两位小数
    assert "达标图片 5 张" in result
    assert "抽样页面 3 个" in result
    assert "URL 风险:" in result and "域名使用可疑顶级域" in result
    assert "文本风险:" in result and "命中色情关键词" in result
    assert result.endswith("以上情况本人已人工核实。")
    assert len(result) <= MAX_DESCRIPTION_CHARS


def test_fallback_without_intel_omits_risk_segments() -> None:
    result = draft_description(
        _make_entry(), {}, Config(), client=FakeClient(payload={"description": ""})
    )
    assert "URL 风险" not in result
    assert "文本风险" not in result
    assert "以上情况本人已人工核实。" in result


def test_fallback_truncates_long_site_url() -> None:
    entry = _make_entry(site_url="https://" + "a" * 300 + ".com")
    result = draft_description(entry, {}, Config(), client=FakeClient(error=RuntimeError("x")))
    assert len(result) <= MAX_DESCRIPTION_CHARS
    assert result.startswith(AI_DRAFT_PREFIX)


# ---------------------------------------------------------------------------
# 离线分支(client=None 的缺省客户端路径)
# ---------------------------------------------------------------------------
def test_offline_adapter_missing_falls_back(monkeypatch) -> None:
    """glm_adapter 模块缺失(sys.modules 置 None)→ 回退确定性模板,不抛出。"""
    monkeypatch.setitem(sys.modules, _ADAPTER_NAME, None)
    result = draft_description(_make_entry(), _make_intel(), Config())

    assert result.startswith(AI_DRAFT_PREFIX)
    assert _SITE in result
    assert "人工核实" in result
    assert result.endswith("以上情况本人已人工核实。")


def test_offline_vlm_offline_error_falls_back(monkeypatch) -> None:
    """注入假 glm_adapter:构造抛 VlmOfflineError → 回退模板。"""
    monkeypatch.setenv(_ENV_KEY, "fake-key-1234567890")  # 有密钥,确保走到构造一步

    mod = types.ModuleType(_ADAPTER_NAME)

    class VlmOfflineError(RuntimeError):
        pass

    class GlmVlmClient:
        def __init__(self, cfg):
            raise VlmOfflineError("vlm_online=False,图像数据不出本机")

    mod.VlmOfflineError = VlmOfflineError
    mod.GlmVlmClient = GlmVlmClient
    monkeypatch.setitem(sys.modules, _ADAPTER_NAME, mod)

    result = draft_description(_make_entry(), {}, Config())
    assert result.startswith(AI_DRAFT_PREFIX)
    assert _SITE in result
    assert "人工核实" in result


def test_offline_no_key_falls_back(monkeypatch) -> None:
    """无密钥(cfg.glm_api_key 与环境变量均为空)→ 直接回退模板。"""
    monkeypatch.delenv(_ENV_KEY, raising=False)
    cfg = Config()  # glm_api_key 默认 ""
    assert not cfg.glm_api_key
    result = draft_description(_make_entry(), {}, cfg)

    assert result.startswith(AI_DRAFT_PREFIX)
    assert _SITE in result
    assert "以上情况本人已人工核实。" in result


# ---------------------------------------------------------------------------
# _collect_facts
# ---------------------------------------------------------------------------
def test_collect_facts_numeric_formatting_and_intel_points() -> None:
    facts = _collect_facts(_make_entry(), _make_intel())
    lines = facts.split("\n")

    assert 1 <= len(lines) <= MAX_FACT_LINES
    assert all(line.strip() for line in lines)
    assert lines[0] == "站点:" + _SITE
    assert "判定:高置信色情(nsfw)" in lines
    assert "站点聚合最高图像分:0.97" in lines      # 0.966 → 两位小数
    assert "达标图片数:5" in lines
    assert "抽样页面数:3" in lines
    assert "URL 风险要点:域名使用可疑顶级域 xyz" in lines
    assert "URL 风险要点:子域深度超过 3 层" in lines
    assert "URL 风险要点:第三条要点" in lines
    assert "URL 风险要点:第四条要点" not in lines  # 每类要点至多 3 条
    assert "文本风险要点:命中色情关键词 6 处" in lines
    assert "融合特征分:0.93" in lines              # 0.934 → 两位小数


def test_collect_facts_two_decimal_places() -> None:
    facts = _collect_facts(_make_entry(agg_nsw_prob=0.9), {"fusion": {"prob": 1.0}})
    lines = facts.split("\n")
    assert "站点聚合最高图像分:0.90" in lines
    assert "融合特征分:1.00" in lines


def test_collect_facts_line_cap() -> None:
    """要点超量时行数封顶 12:5 基础 + 3 URL + 3 文本 + 1 融合。"""
    intel = {
        "url": {"explain": [f"要点{i}" for i in range(6)]},
        "text": {"explain": [f"文本{i}" for i in range(6)]},
        "fusion": {"prob": 0.5},
    }
    lines = _collect_facts(_make_entry(), intel).split("\n")
    assert len(lines) == MAX_FACT_LINES
    assert sum(1 for l in lines if l.startswith("URL 风险要点:")) == 3
    assert sum(1 for l in lines if l.startswith("文本风险要点:")) == 3
    assert "融合特征分:0.50" in lines  # 恰好第 12 行,不被裁掉


def test_collect_facts_tolerates_missing_fields() -> None:
    class Bare:
        site_url = "https://only.example.com"

    facts = _collect_facts(Bare(), {})
    lines = facts.split("\n")
    assert lines[0] == "站点:https://only.example.com"
    assert "判定:未知" in lines
    assert "站点聚合最高图像分:0.00" in lines
    assert "达标图片数:0" in lines
    assert "抽样页面数:0" in lines
    assert "URL 风险要点" not in facts
    assert "文本风险要点" not in facts
    assert "融合特征分" not in facts


def test_collect_facts_tolerates_bad_values() -> None:
    class Odd:
        site_url = None
        verdict = "weird"
        agg_nsw_prob = "not-a-number"
        nsw_image_count = "7"
        pages = 4  # int 形态的 pages

    facts = _collect_facts(Odd(), {"url": "不是dict", "fusion": {"prob": "x"}})
    lines = facts.split("\n")
    assert lines[0] == "站点:"
    assert "判定:weird" in lines        # 未知取值原样透传
    assert "站点聚合最高图像分:0.00" in lines
    assert "达标图片数:7" in lines       # 字符串数字容错
    assert "抽样页面数:4" in lines       # int 形态 pages
    assert "URL 风险要点" not in facts   # 非 dict 的 intel["url"] 容错
    assert "融合特征分" not in facts      # 非法 fusion.prob 容错


def test_collect_facts_accepts_none_intel() -> None:
    facts = _collect_facts(_make_entry(), None)
    assert facts.startswith("站点:" + _SITE)
    assert "URL 风险要点" not in facts


# ---------------------------------------------------------------------------
# 安全红线:两条路径都必须有 AI 声明前缀与“人工核实”
# ---------------------------------------------------------------------------
def test_redline_prefix_and_human_confirm_on_all_paths(monkeypatch) -> None:
    entry, intel = _make_entry(), _make_intel()
    cfg = Config()

    online = draft_description(
        entry, intel, cfg, client=FakeClient(payload={"description": "模型草拟的正文。"})
    )
    on_error = draft_description(
        entry, intel, cfg, client=FakeClient(error=RuntimeError("调用失败"))
    )
    monkeypatch.setitem(sys.modules, _ADAPTER_NAME, None)
    offline_default = draft_description(entry, intel, cfg)

    for result in (online, on_error, offline_default):
        assert "[AI 草拟" in result          # AI 草拟声明前缀
        assert "人工核实" in result           # 人工核实字样
        assert len(result) <= MAX_DESCRIPTION_CHARS


# ---------------------------------------------------------------------------
# V5 升级:可观测性(describer.draft 计时 / describer.fallback 计数)
# + 回退模板单次 format 重构后的逐字节回归
# ---------------------------------------------------------------------------
from netsentinel import telemetry  # noqa: E402


def test_v5_draft_timer_and_fallback_counter_on_offline(monkeypatch) -> None:
    """离线回退(无密钥):计时 1 次、fallback 计数 1。"""
    telemetry.reset()
    monkeypatch.delenv(_ENV_KEY, raising=False)
    draft_description(_make_entry(), _make_intel(), Config())

    snap = telemetry.snapshot()
    assert snap["timers"]["describer.draft"]["count"] == 1
    assert snap["counters"].get("describer.fallback") == 1.0


def test_v5_fallback_counter_on_client_error() -> None:
    telemetry.reset()
    draft_description(
        _make_entry(), {}, Config(), client=FakeClient(error=RuntimeError("连接超时"))
    )
    assert telemetry.snapshot()["counters"].get("describer.fallback") == 1.0


def test_v5_no_fallback_counter_on_online_success() -> None:
    """在线成功路径:只计时,不计 fallback。"""
    telemetry.reset()
    result = draft_description(
        _make_entry(), {}, Config(), client=FakeClient(payload={"description": "正文。"})
    )
    snap = telemetry.snapshot()
    assert snap["timers"]["describer.draft"]["count"] == 1
    assert "describer.fallback" not in snap["counters"]
    assert result.startswith(AI_DRAFT_PREFIX)


def test_v5_fallback_template_exact_bytes() -> None:
    """回退模板单次 format 重构:输出与旧版多段拼接逐字节一致。"""
    entry = _make_entry()  # site/agg 0.966/5 张/3 页/nsfw
    intel = _make_intel()  # URL 要点 2 条 + 文本要点 1 条
    expected_core = (
        "举报站点:" + _SITE + ";"
        "系统初筛判定:高置信色情(nsfw);"
        "图像初筛:站点聚合最高分 0.97、达标图片 5 张、抽样页面 3 个"
    )
    expected = (
        AI_DRAFT_PREFIX
        + expected_core
        + ";URL 风险:域名使用可疑顶级域 xyz;子域深度超过 3 层"
        + ";文本风险:命中色情关键词 6 处;页面含诱导性短语"
        + "。以上情况本人已人工核实。"
    )
    result = draft_description(entry, intel, Config(), client=FakeClient(error=RuntimeError("x")))
    assert result == expected
    assert len(result) <= MAX_DESCRIPTION_CHARS


def test_v5_fallback_template_constant_is_single_skeleton() -> None:
    """模板骨架为模块常量(占位符一次 format,无分段 f-string)。"""
    from netsentinel.submit import llm_describer

    template = llm_describer._FALLBACK_CORE_TEMPLATE
    assert template.count("{site}") == 1
    assert "{agg:.2f}" in template
    filled = template.format(
        site="s.example", verdict="疑似色情低俗(suspect)", agg=0.5, nsw=2, pages=1
    )
    assert filled.startswith("举报站点:s.example;")
    assert "0.50" in filled and "2 张" in filled and "1 个" in filled
