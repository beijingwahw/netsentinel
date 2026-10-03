# -*- coding: utf-8 -*-
"""净网哨兵(NetSentinel)多平台统一接入演示脚本 —— A78(V4)。

在**完全离线(零外呼)**的环境中演示"目录里任何提供方的视觉模型都能用
``classifier: 提供方:模型`` 语法接入跑通识别":

1. 目录清单:打印 ``providers.PROVIDERS`` 全部 20 家
   (方言 / 默认模型(提示值)/ 本地 / 密钥是否已配置——只显示已配置/未配置,绝不回显);
2. 三平台评分对比:openai / anthropic / gemini 对同一张演示图打分,
   三种 API 方言各走各的请求构造,响应全部由本地回放传输层生成;
3. 故障转移:链 ``[openai:gpt-4o-mini, ollama:llava]`` 第一家模拟传输故障,
   自动切换到本地 ollama,胜者标注 ``failover→ollama:llava``;
4. 跨平台一致性:``provider_agreement.analyze`` 对三家在两张图上的分数出
   一致性报告(逐图分歧 / 系统性偏离 outlier 中文提示 / 两两一致率);
5. 成本估算示例:A75 ``cost_meter`` 就位时用其提示价估算并记账,
   未就位时打印内置示例表(提示值,以各平台账单为准);
6. 收尾声明:演示全程零外呼,所有平台响应均为本地模拟。

安全红线(V4 §0 第 16/17/19/20 条):
- 全部传输层为本地回放(``vlm_client._http_post_json`` 整体替换),**零真实外呼**,
  即使脚本有 bug 也不可能发出真实请求;
- 密钥只以"已配置/未配置"出现,认证头只在 DEBUG 日志中打码打印,绝不回显;
- 所有临时产物(评分缓存库 / 记账文件)写入系统临时目录,不污染仓库。

用法:python scripts/demo_multi_provider.py
退出码:0=演示成功;2=核心兄弟模块未就位;1=其他错误(如夹具缺失)。
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import logging
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

#: 项目根目录(脚本位于 <root>/scripts/)
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from netsentinel import telemetry  # noqa: E402
from netsentinel.contracts import Config, ImageEvidence, ImageScore  # noqa: E402

#: 演示站点夹具目录(与端到端测试共用同一批图)
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "demo_site"

#: 核心依赖(任一缺失都以退出码 2 结束;模块名 → 中文角色说明)
REQUIRED_SIBLING_MODULES: list[tuple[str, str]] = [
    ("netsentinel.vision.providers", "提供方目录(A61)"),
    ("netsentinel.vision.vlm_client", "统一传输层(A62)"),
    ("netsentinel.vision.multi_provider", "统一分类器(A63)"),
    ("netsentinel.vision.vlm_cache", "评分缓存与预算(A23)"),
    ("netsentinel.vision.failover", "故障转移路由(A68)"),
    ("netsentinel.vision.provider_agreement", "跨平台一致性(A69)"),
]

#: 可选依赖(缺失时对应环节优雅降级,不阻塞演示)
_OPTIONAL_MODULES: dict[str, str] = {
    "netsentinel.security.keys": "多平台密钥环(A70)",
    "netsentinel.vision.cost_meter": "成本计量(A75)",
}

#: 三平台对比环节用的演示图(高置信色情夹具)
DEMO_IMAGE = "nsfw_hi_1.png"
#: 一致性分析第二张图(正常夹具,用来制造平台间分歧)
DEMO_IMAGE_NORMAL = "normal_1.png"

#: 各家注入的演示密钥(仅存在于本进程 cfg,绝不外发、绝不打印)
_DEMO_KEYS: dict[str, str] = {
    "openai": "sk-demo-openai",
    "anthropic": "sk-demo-anthropic",
    "gemini": "sk-demo-gemini",
}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _ensure_utf8_stdio() -> None:
    """Windows 管道/终端非 UTF-8 时切换标准输出编码,避免中文打印报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and stream.encoding and stream.encoding.lower() not in (
                "utf-8",
                "utf8",
            ):
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 重新配置失败不影响主流程
            pass


def _disp_width(text: str) -> int:
    """中文等东亚宽字符按 2 列计的显示宽度(纯标准库近似)。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    """按显示宽度右补空格,用于中文表格对齐。"""
    text = str(text)
    gap = width - _disp_width(text)
    return text + " " * max(0, gap)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    """渲染一张按显示宽度对齐的中文表格(无第三方依赖)。"""
    widths = [_disp_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _disp_width(str(cell)))
    lines = [
        "  ".join(_pad(h, widths[i]) for i, h in enumerate(headers)).rstrip(),
        "  ".join("-" * w for w in widths),
    ]
    for row in rows:
        lines.append("  ".join(_pad(str(c), widths[i]) for i, c in enumerate(row)).rstrip())
    return "\n".join(lines)


def _print_telemetry_tail() -> None:
    """打印 telemetry.snapshot() 中文尾节(V5 可观测性演示)。

    展示本进程累计的关键计数(计数器/仪表)与各演示环节耗时统计
    (次数 / 平均 / p95 / 最大,单位毫秒);零依赖、只含名称与数字。
    """
    snap = telemetry.snapshot()
    print("=" * 78)
    print("遥测快照(telemetry.snapshot,V5 可观测性:关键计数与各环节耗时 p95)")
    counters = snap.get("counters") or {}
    gauges = snap.get("gauges") or {}
    timers = snap.get("timers") or {}
    if not (counters or gauges or timers):
        print("  本进程尚未记录任何遥测指标。")
        return
    if counters or gauges:
        rows = [[name, f"{value:g}"] for name, value in {**counters, **gauges}.items()]
        print("计数器 / 仪表:")
        print(_table(["指标", "值"], rows))
    if timers:
        rows = [
            [
                name,
                f"{stats['count']}",
                f"{stats['avg_ms']:.1f}",
                f"{stats['p95_ms']:.1f}",
                f"{stats['max_ms']:.1f}",
            ]
            for name, stats in timers.items()
        ]
        print("耗时统计(毫秒):")
        print(_table(["环节(timer)", "次数", "平均", "p95", "最大"], rows))


def check_sibling_modules() -> list[str]:
    """逐一导入核心依赖,返回未就位模块的中文描述列表。"""
    missing: list[str] = []
    for name, role in REQUIRED_SIBLING_MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - 并行开发中未就位/依赖缺失均视为未就位
            missing.append(f"{name} —— {role}(导入失败:{exc})")
    return missing


def _import_optional(name: str) -> Any | None:
    """导入可选兄弟模块;未就位返回 None(对应环节降级)。"""
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001
        return None


def _fixture_evidence(name: str) -> ImageEvidence:
    """把演示站点夹具图包成 ImageEvidence(宽高 200x200,sha256 按内容计算)。"""
    path = FIXTURE_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"缺少演示站点夹具图片:{path}")
    return ImageEvidence(
        path=str(path),
        url=f"https://demo-multi.invalid/{name}",
        source_page="https://demo-multi.invalid/index.html",
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        width=200,
        height=200,
    )


# ---------------------------------------------------------------------------
# 本地回放传输层(红线 20:零外呼的根基)
# ---------------------------------------------------------------------------


class QueueTransport:
    """按调用顺序出队的本地回放传输层:替代 ``vlm_client._http_post_json``。

    - ``enqueue(body=...)`` / ``enqueue(exc=...)``:预排下一次"外呼"的结果,
      评分与故障都由本地脚本导演,绝无真实网络;
    - 每次调用记录 (url, headers, payload, timeout),供演示讲解接线细节;
    - 未预排时抛 AssertionError——宁可失败也绝不落回真实网络。
    """

    def __init__(self) -> None:
        self.queue: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    def enqueue(self, body: str | None = None, exc: Exception | None = None) -> None:
        self.queue.append({"body": body, "exc": exc})

    def __call__(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float = 90.0,
        **_: Any,
    ) -> tuple[int, str]:
        self.calls.append(
            {"url": url, "headers": dict(headers), "payload": payload, "timeout": timeout}
        )
        if not self.queue:
            raise AssertionError(
                f"演示传输层未预排响应却被调用:{url}(演示绝不发起真实网络请求)"
            )
        item = self.queue.pop(0)
        if item["exc"] is not None:
            raise item["exc"]
        assert item["body"] is not None
        return 200, str(item["body"])


def dialect_body(style: str, content: dict[str, Any]) -> str:
    """按 API 方言构造一次"合法响应"(三模板,本地生成)。"""
    text = json.dumps(content, ensure_ascii=False)
    if style == "anthropic":
        return json.dumps({"content": [{"type": "text", "text": text}]}, ensure_ascii=False)
    if style == "gemini":
        return json.dumps(
            {"candidates": [{"content": {"parts": [{"text": text}]}}]}, ensure_ascii=False
        )
    return json.dumps({"choices": [{"message": {"content": text}}]}, ensure_ascii=False)


def _install_replay_transport() -> QueueTransport:
    """整体替换 vlm_client 缺省传输为本地回放(此后本进程不可能真实外呼)。"""
    vlm_client = importlib.import_module("netsentinel.vision.vlm_client")
    transport = QueueTransport()
    setattr(vlm_client, "_http_post_json", transport)  # 演示脚本专用注入点
    return transport


# ---------------------------------------------------------------------------
# 演示环节
# ---------------------------------------------------------------------------


def demo_catalog(cfg: Config) -> None:
    """环节 1:提供方目录清单(20 家表;密钥只显示已配置/未配置)。"""
    providers = importlib.import_module("netsentinel.vision.providers")
    keys_mod = _import_optional("netsentinel.security.keys")
    configured: dict[str, bool] = keys_mod.configured(cfg) if keys_mod else {}

    print("=" * 78)
    print(f"一、提供方目录清单(providers.PROVIDERS,共 {len(providers.PROVIDERS)} 家)")
    print("模型名与端点均为提示值,以各平台官方文档为准,上线前请核验一次(红线 18)")
    rows: list[list[str]] = []
    for name, spec in providers.PROVIDERS.items():
        if spec.local:
            key_state = "免密钥"
        elif configured:
            key_state = "已配置" if configured.get(name) else "未配置"
        else:
            key_state = "未配置(注:密钥环 A70 未就位,此处不判断)"
        rows.append(
            [
                name,
                spec.style,
                spec.default_model or "(必填模型)",
                "本地" if spec.local else "云端",
                key_state,
            ]
        )
    print(_table(["提供方", "方言", "默认模型(提示值)", "部署", "密钥"], rows))
    print()


def _mock_content(prob: float, reasoning: str) -> dict[str, Any]:
    """构造一份 mock 评分内容(categories/confidence 按 prob 粗分档)。"""
    return {
        "nsfw_prob": prob,
        "categories": ["色情"] if prob >= 0.5 else ["正常"],
        "reasoning": reasoning,
        "confidence": 0.9 if prob >= 0.5 else 0.6,
    }


def _classify_with_mock(
    cfg: Config,
    spec: str,
    transport: QueueTransport,
    content: dict[str, Any] | Exception,
    img: ImageEvidence,
) -> ImageScore:
    """build_classifier(spec) → 预排一次 mock 响应 → classify(零外呼)。

    评分缓存真实生效:若该(模型, 图)已命中缓存未发起传输,则把预排响应
    出队,避免串场到后续环节;分类器自建的缓存连接用毕即关(Windows 句柄)。
    """
    multi_provider = importlib.import_module("netsentinel.vision.multi_provider")
    providers = importlib.import_module("netsentinel.vision.providers")
    provider_name = spec.split(":", 1)[0]
    style = providers.PROVIDERS[provider_name].style
    if isinstance(content, Exception):
        transport.enqueue(exc=content)
    else:
        transport.enqueue(body=dialect_body(style, content))
    clf = multi_provider.build_classifier(spec, cfg)
    before = len(transport.calls)
    try:
        return clf.classify(img)
    finally:
        if len(transport.calls) == before and transport.queue:
            transport.queue.pop(0)  # 缓存命中未消耗预排响应:出队防串场
        _close_classifier_cache(clf)


def _close_classifier_cache(clf: Any) -> None:
    """关闭分类器(及其故障转移成员)自建的 VlmCache 连接(便于临时目录清理)。"""
    members = list(getattr(clf, "_members", {}).values()) or [clf]
    for member in members:
        cache = getattr(member, "_cache", None)
        with contextlib.suppress(Exception):
            if callable(getattr(cache, "close", None)):
                cache.close()


def demo_three_platforms(cfg: Config, transport: QueueTransport) -> list[ImageScore]:
    """环节 2:openai / anthropic / gemini 三方言对同一张图评分并出对比表。"""
    print("=" * 78)
    print(f"二、三平台评分对比(同一张图:{DEMO_IMAGE},响应均为本地模拟)")
    img = _fixture_evidence(DEMO_IMAGE)
    plan: list[tuple[str, float, str]] = [
        ("openai:gpt-4o-mini", 0.97, "画面含明显裸露与性暗示内容(openai 方言模拟)"),
        ("anthropic:claude-sonnet-4", 0.98, "画面为直白色情内容(anthropic 方言模拟)"),
        ("gemini:gemini-2.0-flash", 0.93, "画面含高置信色情内容(gemini 方言模拟)"),
    ]
    scores: list[ImageScore] = []
    for spec, prob, reasoning in plan:
        score = _classify_with_mock(
            cfg, spec, transport, _mock_content(prob, reasoning), img
        )
        scores.append(score)
        call = transport.calls[-1]
        provider = spec.split(":", 1)[0]
        print(
            f"  [{provider:>9}] 请求端点 {call['url']}  "
            f"载荷模型 {call['payload'].get('model', '(在 URL 中)')}"
        )
    rows = [
        [
            s.scores.get("provider", "?"),
            s.model,
            f"{s.nsfw_prob:.3f}",
            str(s.scores.get("reasoning", "")),
        ]
        for s in scores
    ]
    print(_table(["提供方", "模型", "nsfw_prob(校准后)", "评分依据"], rows))
    print("三家均判高置信色情(nsfw_prob > 0.9),口径一致。")
    print()
    return scores


def demo_failover(cfg: Config, transport: QueueTransport) -> None:
    """环节 3:故障转移——第一家模拟传输故障,自动切本地 ollama。

    用第二张夹具图(nsfw_hi_2.png)避开上一环节的评分缓存,确保两家成员
    都真实走过传输层(回放),完整演示"失败 → 切换 → 胜者标注"。
    """
    print("=" * 78)
    print("三、故障转移演示(链:openai:gpt-4o-mini → ollama:llava)")
    failover = importlib.import_module("netsentinel.vision.failover")
    cfg.vlm_fallback_chain = ["openai:gpt-4o-mini", "ollama:llava"]
    img = _fixture_evidence("nsfw_hi_2.png")

    print("  送审图:nsfw_hi_2.png(未评分过,避开缓存,两家成员都真实走传输层)")
    print("  第 1 家 openai:gpt-4o-mini:传输层即将抛出模拟故障(RuntimeError)……")
    fault = RuntimeError("模拟 openai 平台传输层故障(演示注入,零外呼)")
    transport.enqueue(exc=fault)
    transport.enqueue(
        body=dialect_body("openai", _mock_content(0.96, "本地 ollama 兜底评分(模拟)"))
    )
    clf = failover.FailoverClassifier(cfg)
    try:
        score = clf.classify(img)
    finally:
        _close_classifier_cache(clf)
    assert score.model == "failover→ollama:llava", f"故障转移胜者标注错误:{score.model}"
    print("  第 1 家失败 → 自动切换;第 2 家 ollama:llava 评分成功。")
    print(f"  胜者标注 model={score.model!s}  nsfw_prob={score.nsfw_prob:.3f}")
    print(f"  fallback_from(先前失败成员)={score.scores.get('fallback_from')}")
    print("  说明:日志里的 WARNING 为第一家失败的预期降级记录;预算一本账(红线 19)")
    print("  由各成员自行 spend_one 记账,故障转移不绕账。")
    print()


def demo_agreement(scores_hi: list[ImageScore], cfg: Config, transport: QueueTransport) -> None:
    """环节 4:provider_agreement 一致性报告(outlier 中文提示)。"""
    print("=" * 78)
    print("四、跨平台一致性分析(provider_agreement.analyze,两张图 × 三家)")
    # 第二张图用 normal_1:openai/anthropic 判正常,gemini 模拟一次误报(偏高),
    # 让 outlier 检测有东西可报——这正是多平台互检的价值。
    img_normal = _fixture_evidence(DEMO_IMAGE_NORMAL)
    extra: list[ImageScore] = []
    for spec, prob, reasoning in (
        ("openai:gpt-4o-mini", 0.03, "画面为普通风景图(openai 方言模拟)"),
        ("anthropic:claude-sonnet-4", 0.04, "无色情内容(anthropic 方言模拟)"),
        ("gemini:gemini-2.0-flash", 0.55, "画面疑似擦边(gemini 方言模拟,刻意偏高)"),
    ):
        extra.append(
            _classify_with_mock(cfg, spec, transport, _mock_content(prob, reasoning), img_normal)
        )
    agreement = importlib.import_module("netsentinel.vision.provider_agreement")
    analysis = agreement.analyze(scores_hi + extra)

    print(f"  参与提供方:{'、'.join(analysis['providers'])}")
    spread_rows = [
        [
            Path(r["image"]).name,
            f"{r['max']:.3f}",
            f"{r['min']:.3f}",
            f"{r['spread']:.3f}",
        ]
        for r in analysis["per_image_spread"]
    ]
    print(_table(["图片(分歧降序)", "最高分", "最低分", "极差"], spread_rows))
    bias_rows = [[p, f"{v:+.4f}", "偏松" if v > 0 else "偏严"] for p, v in analysis["bias"].items()]
    print("  各家系统性偏差(bias = 均值 - 逐图共识;正=偏松,负=偏严):")
    print(_table(["提供方", "bias", "倾向"], bias_rows))
    if analysis["outliers"]:
        print("  离群提供方(|bias| > 0.15,建议人工抽检):")
        for out in analysis["outliers"]:
            print(f"    - {out['provider']}:delta={out['delta']:+.4f}  {out['note']}")
    else:
        print("  无离群提供方。")
    pair_rows = [
        [f"{a} × {b}", f"{rate:.0%}"] for (a, b), rate in analysis["pair_agreement"].items()
    ]
    print("  两两一致率(同图分差 ≤ 0.2 视为一致):")
    print(_table(["提供方对", "一致率"], pair_rows))
    print()


def demo_cost(cfg: Config, transport: QueueTransport) -> None:
    """环节 5:成本估算示例(A75 就位则真记账,否则内置提示价示例)。"""
    print("=" * 78)
    print("五、成本估算示例(提示值,以各平台实际账单为准;本地推理计为 0 元)")
    vlm_cache = importlib.import_module("netsentinel.vision.vlm_cache")
    cache = vlm_cache.VlmCache(cfg.vlm_cache_db)
    try:
        state = cache.budget_state()
    finally:
        cache.close()
    print(
        f"  本次演示经 vlm_cache.spend_one 预算记账 {state['used']} 次"
        f"(红线 19:跨平台共用一本账,日限 {state['limit']};全部为本地模拟调用)"
    )
    cost_meter = _import_optional("netsentinel.vision.cost_meter")
    if cost_meter is not None:
        print("  成本计量模块 cost_meter(A75)已就位:按其提示价真估算并落账。")
        meter = cost_meter.CostMeter(str(Path(cfg.data_dir) / "cost.jsonl"))
        samples = [
            ("openai", "gpt-4o-mini", 8),
            ("glm", "glm-5.3-flash", 8),
            ("gemini", "gemini-2.0-flash", 8),
            ("ollama", "llava", 8),
        ]
        rows: list[list[str]] = []
        for provider, model, images in samples:
            est = meter.estimate(provider, model, images)
            meter.record(provider, model, images)
            rows.append(
                [
                    provider,
                    model,
                    str(images),
                    "无提示价(以账单为准)" if est is None else f"{est:.4f} 元",
                ]
            )
        print(_table(["提供方", "模型", "图片数", "估算成本(提示值)"], rows))
        summary = meter.summary()
        by_provider = summary.get("by_provider") if isinstance(summary, dict) else None
        if isinstance(by_provider, dict) and by_provider:
            print("  记账汇总(summary,单位:人民币元):")
            for provider, info in by_provider.items():
                if not isinstance(info, dict):
                    continue
                calls = info.get("calls", 0)
                images = info.get("images", 0)
                cost = info.get("est_cost")
                cost_text = "无提示价" if cost is None else f"{cost:.4f}"
                print(f"    - {provider}:调用 {calls} 次 / 图片 {images} 张 / 估算 {cost_text}")
            total = summary.get("total_est")
            if total is not None:
                print(f"    合计估算:{total:.4f} 元(无提示价记录按 0 参与求和)")
        else:
            print(f"  记账汇总:{summary}")
    else:
        print("  成本计量模块 cost_meter(A75)未就位,以下为内置提示价示例:")
        rows = [
            ["openai", "gpt-4o-mini", "8", "约 0.01 美元/千次级(示例)"],
            ["glm", "glm-5.3-flash", "8", "约 0.01 元人民币/次级(示例)"],
            ["gemini", "gemini-2.0-flash", "8", "按 token 计费,flash 档低价(示例)"],
            ["ollama", "llava", "8", "本地算力,0 外呼成本"],
        ]
        print(_table(["提供方", "模型", "图片数", "说明"], rows))
    print()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def build_demo_config(work_dir: Path) -> Config:
    """演示配置:密钥只进本进程 cfg;缓存库与数据目录全部指向临时目录。"""
    return Config(
        vlm_online=True,
        vlm_api_keys=dict(_DEMO_KEYS),
        vlm_cache_db=str(work_dir / "vlm_cache.db"),
        vlm_daily_budget=200,
        data_dir=str(work_dir / "data"),
        evidence_dir=str(work_dir / "data" / "evidence"),
        db_path=str(work_dir / "data" / "review_queue.db"),
        audit_path=str(work_dir / "data" / "audit.jsonl"),
        log_path=str(work_dir / "data" / "logs" / "netsentinel.log"),
    )


def main() -> int:
    """演示主流程;返回进程退出码(0=成功,2=模块未就位,1=其他错误)。"""
    _ensure_utf8_stdio()
    missing = check_sibling_modules()
    if missing:
        print("核心兄弟模块未就位,演示中止(退出码 2):")
        for line in missing:
            print(f"  - {line}")
        return 2

    # 日志并入 stdout:既保留 WARNING 级降级记录(故障转移环节的预期旁白),
    # 又避免 Windows 下 stderr 无缓冲导致输出乱序;密钥绝不进日志(红线 17)。
    logging.basicConfig(stream=sys.stdout, level=logging.WARNING, format="[日志] %(message)s")

    print("净网哨兵 NetSentinel —— 多平台统一接入演示(V4 · A78)")
    print("安全声明:本演示替换了 VLM 传输层为本地回放,全程零外呼;密钥不回显。")
    print()
    # V5 可观测:遥测从演示起点清零,尾节 snapshot 只含本轮各环节数据
    telemetry.reset()
    # ignore_cleanup_errors:Windows 下个别 sqlite 句柄可能晚于目录回收,残留交由系统清理
    with tempfile.TemporaryDirectory(prefix="netsentinel-demo-", ignore_cleanup_errors=True) as tmp:
        work_dir = Path(tmp)
        cfg = build_demo_config(work_dir)
        transport = _install_replay_transport()
        with telemetry.timer("demo.catalog"):
            demo_catalog(cfg)
        with telemetry.timer("demo.three_platforms"):
            scores = demo_three_platforms(cfg, transport)
        with telemetry.timer("demo.failover"):
            demo_failover(cfg, transport)
        with telemetry.timer("demo.agreement"):
            demo_agreement(scores, cfg, transport)
        with telemetry.timer("demo.cost"):
            demo_cost(cfg, transport)
        _print_telemetry_tail()

    print("=" * 78)
    print("演示全程零外呼:所有平台响应均为本地模拟。")
    print("正式使用:vlmctl list/ping/doctor 诊断配置(仅 ping 为人工显式外呼),")
    print("再以 classifier: 提供方:模型 接入任意目录内平台(见 docs/PROVIDERS.md)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
