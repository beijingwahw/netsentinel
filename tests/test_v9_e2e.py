# -*- coding: utf-8 -*-
"""A176:V9 端到端测试(离线)—— 三档并发退化 · 真扫描收官 · 依次举报干跑。

依据 CONTRACTS-V9.md §3 A176 行与 §0 红线 35/36,离线覆盖四条主链
(全部只打本地回环,零外呼、零真实提交):

1. **档位退化**:``NETSENTINEL_FAKE_CORES=2``(A163 测试钩子)或
   monkeypatch ``os.cpu_count=2`` → ``tier=high`` 且 ``cpu_reserve=1`` →
   ``ops.concurrency.io_workers(cfg) == 1``(§1 公式 ``max(1, 2-1)``,
   单核机高档退化正确,红线 37 恒 ≥1);经 ``finishflow.finish``(注入
   scan + **真** A168 SummaryAgent)验证 outcome ``workers==1`` 且
   workers 以关键字透传给扫描函数;
2. **真扫描收官**:本地 2 站(127.0.0.1 / 127.0.0.2 各 2 张 ``nsfw_hi``
   PNG,200x200,纯标准库 PNG 编码器 ``scripts/make_png.py`` 生成,
   构造手法参照 test_v8_e2e 的回环 ThreadingHTTPServer)→
   ``finishflow.finish(urls, cfg)``(tier=mid,真扫描池 → 真编排器 →
   真桩分类器 → 真判定 → 真归组入列 → 真结案汇总)→ 断言 outcome
   ``groups>=1``、收官报告 md/html 双件落盘、``ready_count`` /
   ``attest_pending`` 结构正确、**汇总文件含"结案代理生成"与"人工声明"
   字样**(A172 固定结论句,红线 36);
3. **依次举报(干跑)**:真复核队列(approve)→ 真逐组声明(attest)→
   真 ``ready_entries`` → ``SequentialReportAgent().run(items, cfg,
   dry_run=True)`` 与 ``finishflow.run_report`` 干跑——run_report 缺省走
   **真 run_batch → 真 plan_12377 → 真 executor_playwright dry_run 路径**
   (纯文本无浏览器),断言逐条执行(on_item 1..n 保序)、
   ``submitted==0``(干跑绝不真实提交)且 ``results`` 长度==条目数;
4. **红线 35**:finish 与 run_report 全链前后对 cfg 做**全字段快照**
   (dataclasses.asdict)比对相等——礼貌间隔 ``fetch_delay_s`` 与提交频控
   字段(``submit_min_interval_s`` / ``submit_max_per_day`` /
   ``batch_item_interval_s`` 等)一概不被改动;CLI ``--tier high`` 覆盖
   只经 ``dataclasses.replace`` 生成新对象(输出明示"礼貌间隔与举报频控
   不变")。

并行未就位标注:A163–A175/A169/A181 本轮已全部就位(真模块直跑);
对兄弟模块仍保留用例级 ``pytest.importorskip`` 门控——未就位时 skip 并在
reason 标注,不误报失败。

隔离纪律:每条用例独立 tmp(data / db / 审计 / 日志 / 证据);模块级
autouse fixture 负责 ``NETSENTINEL_FAKE_CORES`` 清零、A165 会话幂等标记
复位、浏览器采样强制退化(无截图,纯 HTML+图片下载,离线确定性)、以及
**全部服务器与线程的收尾停止**。
"""
from __future__ import annotations

import dataclasses
import functools
import importlib
import importlib.util
import pathlib
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from netsentinel.contracts import Config, SiteReport, Verdict

# ---------------------------------------------------------------------------
# 路径 / 兄弟模块清单(importorskip 门控)
# ---------------------------------------------------------------------------
ROOT = pathlib.Path(__file__).resolve().parents[1]
MAKE_PNG_PATH = ROOT / "scripts" / "make_png.py"

#: 本文件直连的 V9/既有兄弟模块(未就位 → importorskip skip 标注)
MOD_CPU_PROFILE = "netsentinel.ops.cpu_profile"        # A163
MOD_CONCURRENCY = "netsentinel.ops.concurrency"        # A164
MOD_TIER_STATE = "netsentinel.ops.tier_state"          # A165
MOD_BATCH_SCAN = "netsentinel.ops.batch_scan"          # A108
MOD_SUMMARY_AGENT = "netsentinel.agent.summary_agent"  # A168
MOD_FINISHFLOW = "netsentinel.finishflow"              # A169
MOD_SEQUENTIAL = "netsentinel.agent.sequential_report" # A170
MOD_RUN_SUMMARY = "netsentinel.report.run_summary"     # A172
MOD_BATCH_REVIEW = "netsentinel.decision.batch_review" # A110
MOD_REVIEW_QUEUE = "netsentinel.decision.review_queue" # A9
MOD_BATCH_STATE = "netsentinel.submit.batch_state"     # A113
MOD_BATCH_SUBMIT = "netsentinel.submit.batch_submit"   # A112
MOD_PORTAL_12377 = "netsentinel.submit.portal_12377"   # A10
MOD_EXECUTOR = "netsentinel.submit.executor_playwright"# A13
MOD_BROWSER = "netsentinel.crawler.browser"            # 采样(强制退化)

#: 两站的图片清单(文件名含 nsfw_hi → 桩分类器 0.97;200x200 过小图过滤)
SITE_A_IMAGES = ("nsfw_hi_a1.png", "nsfw_hi_a2.png")
SITE_B_IMAGES = ("nsfw_hi_b1.png", "nsfw_hi_b2.png")

#: 批量声明合法文本(必须含"人工核实"四字,红线 25)
VALID_ATTEST_TEXT = "我已逐站人工核实全部证据,同意批量举报"


# ---------------------------------------------------------------------------
# 服务器注册表与模块级 fixture(起停 / 隔离 / 收尾)
# ---------------------------------------------------------------------------
#: 本模块启动的全部 (server, thread),autouse fixture teardown 统一停止
_ACTIVE_SERVERS: list[tuple[Any, threading.Thread]] = []


class _QuietHandler(SimpleHTTPRequestHandler):
    """静态文件服务(回环限定):关访问日志,避免污染 pytest 输出。"""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


def _stop_all_servers() -> None:
    """停止并清空注册表里的全部服务器(幂等;线程 join 限时防挂死)。"""
    while _ACTIVE_SERVERS:
        srv, thread = _ACTIVE_SERVERS.pop()
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:  # noqa: BLE001 - 收尾清理绝不抛出掩盖测试结果
            pass
        thread.join(timeout=5)


def _reset_tier_once_session() -> None:
    """复位 A165 会话幂等标记(tier_once 的模块级 _ONCE_RESULT)。"""
    try:
        tier_state = importlib.import_module(MOD_TIER_STATE)
    except Exception:  # noqa: BLE001 - 未就位属并行常态
        return
    reset = getattr(tier_state, "_reset_once_guard", None)
    if callable(reset):
        reset()


@pytest.fixture(autouse=True)
def _e2e_env(monkeypatch: pytest.MonkeyPatch):
    """每条用例的统一隔离环境 + 收尾。

    - ``NETSENTINEL_FAKE_CORES`` 清零(缺省不伪造核数;档位用例显式 setenv);
    - 复位 A165 ``tier_once`` 会话幂等标记(模块级可变状态,不复位会跨用例串扰);
    - 采样强制无截图退化(monkeypatch ``browser._import_sync_playwright`` →
      None:纯标准库 urllib 抓 HTML + 下载图片,离线确定性;发现/下载/分类/
      判定/打包/入列仍是真链);
    - teardown:停止本用例注册的全部服务器并再次复位会话标记。
    """
    monkeypatch.delenv("NETSENTINEL_FAKE_CORES", raising=False)
    _reset_tier_once_session()
    try:
        browser = importlib.import_module(MOD_BROWSER)
    except Exception:  # noqa: BLE001 - 未就位属并行常态,扫描用例自行 skip
        browser = None
    if browser is not None:
        monkeypatch.setattr(browser, "_import_sync_playwright", lambda: None)
    yield
    _stop_all_servers()
    _reset_tier_once_session()


# ---------------------------------------------------------------------------
# 小工具:PNG 生成 / 站点构造 / 配置工厂 / 服务器
# ---------------------------------------------------------------------------
def _load_make_png() -> Any:
    """按文件路径加载 scripts/make_png.py(scripts 不是包;test_e2e_stub 同款)。"""
    spec = importlib.util.spec_from_file_location(
        "netsentinel_scripts_make_png_v9e2e", MAKE_PNG_PATH
    )
    assert spec is not None and spec.loader is not None, "无法加载 scripts/make_png.py"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_site(site_dir: pathlib.Path, images: tuple[str, ...]) -> pathlib.Path:
    """在 site_dir 生成演示站点:index.html + 若干 200x200 nsfw_hi PNG。

    200x200 恰过 ``cfg.min_image_px`` 小图过滤;文件名含 ``nsfw_hi`` →
    桩分类器固定 0.97(两张未达 min_nsw_images=3 → 站点判 SUSPECT,
    agg=0.97 ≥ review_threshold,必须人工复核并入列)。
    """
    site_dir.mkdir(parents=True, exist_ok=True)
    make_png = _load_make_png().make_png
    img_tags = "\n".join(f'    <img src="{name}" alt="pic">' for name in images)
    (site_dir / "index.html").write_text(
        "<!DOCTYPE html>\n"
        "<html><head><meta charset='utf-8'><title>演示站点</title></head>\n"
        f"<body>\n{img_tags}\n</body></html>\n",
        encoding="utf-8",
    )
    for idx, name in enumerate(images):
        (site_dir / name).write_bytes(make_png(200, 200, (0x80 + idx * 8, 0x40, 0xC0)))
    return site_dir


def _start_site(site_dir: pathlib.Path, host: str) -> str:
    """在指定回环 host 随机端口服务 site_dir,返回首页 URL(注册收尾)。"""
    handler = functools.partial(_QuietHandler, directory=str(site_dir))
    server = ThreadingHTTPServer((host, 0), handler)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        daemon=True,
        name=f"v9e2e-http-{host}",
    )
    thread.start()
    _ACTIVE_SERVERS.append((server, thread))
    return f"http://{host}:{server.server_address[1]}/index.html"


def _start_two_sites(tmp_path: pathlib.Path) -> list[str]:
    """起本地 2 站:127.0.0.1 与 127.0.0.2(回环 /8 段,不离开本机)。"""
    dir_a = _build_site(tmp_path / "site_a", SITE_A_IMAGES)
    dir_b = _build_site(tmp_path / "site_b", SITE_B_IMAGES)
    return [_start_site(dir_a, "127.0.0.1"), _start_site(dir_b, "127.0.0.2")]


def _make_cfg(
    tmp_path: pathlib.Path,
    *,
    tier: str = "mid",
    reserve: int = 1,
    **kw: Any,
) -> Config:
    """端到端 Config:runtime / data / db / 审计 / 日志全部落 tmp。

    - ``fetch_delay_s=0``:本地回环演示无抓取间隔(礼貌参数只对真实外网);
    - ``concurrency_auto=False``:tier_once 零副作用(不写持久档);
    - ``browser_session_reuse=False``:干跑举报走 run_batch 缺省
      executor_playwright(纯文本路径),不构造 V7 会话执行器;
    - ``dry_run_default=True``:任何漏传 dry_run 的入口都绝不真实提交。
    """
    data_dir = tmp_path / "data"
    values: dict[str, Any] = dict(
        data_dir=str(data_dir),
        evidence_dir=str(data_dir / "evidence"),
        db_path=str(data_dir / "review_queue.db"),
        audit_path=str(data_dir / "audit.jsonl"),
        log_path=str(data_dir / "logs" / "netsentinel.log"),
        model_runtime_path=str(data_dir / "model_runtime.json"),
        fetch_delay_s=0.0,
        concurrency_tier=tier,
        concurrency_auto=False,
        cpu_reserve=reserve,
        browser_session_reuse=False,
        dry_run_default=True,
    )
    values.update(kw)
    return Config(**values)


class _RecordingScan:
    """finish 注入的扫描替身:记录 (urls, cfg, workers),返回预置结果。"""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.result = result or {"reports": {}, "summary": {"done": 0, "failed": 0}}
        self.calls: list[dict[str, Any]] = []

    def __call__(self, urls: list[str], cfg: Config, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"urls": list(urls), "cfg": cfg, "kwargs": dict(kwargs)})
        return dict(self.result)


def _queue_add_group_entry(
    queue: Any, url: str, group: str, entry_zip: str = "out/evidence.zip"
) -> int:
    """向真复核队列入列一条带 ``[组:名]`` note 的 NSFW 条目,返回 id。"""
    report = SiteReport(
        site_url=url,
        verdict=Verdict.NSFW,
        agg_nsw_prob=0.97,
        nsw_image_count=3,
        needs_review=True,
    )
    return int(queue.add(report, entry_zip, note=f"[组:{group}]"))


# ---------------------------------------------------------------------------
# 用例组 1:档位退化(单核机高档 → workers=1;红线 37 恒 ≥1)
# ---------------------------------------------------------------------------
def test_high_tier_degrades_to_one_worker_fake_cores_env(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """退化①:NETSENTINEL_FAKE_CORES=2(A163 测试钩子)→ high/reserve1 → 1。"""
    cpu_profile = pytest.importorskip(
        MOD_CPU_PROFILE, reason="A163 ops.cpu_profile 未就位(并行开发中)"
    )
    concurrency = pytest.importorskip(
        MOD_CONCURRENCY, reason="A164 ops.concurrency 未就位(并行开发中)"
    )
    monkeypatch.setenv("NETSENTINEL_FAKE_CORES", "2")
    assert cpu_profile.detect()["cores"] == 2  # 钩子生效
    cfg = _make_cfg(tmp_path, tier="high", reserve=1)
    assert cfg.concurrency_tier == "high" and cfg.cpu_reserve == 1
    # §1 公式:high = max(1, N - reserve) = max(1, 2-1) = 1(红线 37 恒 ≥1)
    assert cpu_profile.tier_workers("high", reserve=1, cores=2) == 1
    assert concurrency.io_workers(cfg) == 1
    assert concurrency.cpu_workers(cfg) == 1  # min(1, 2) = 1,恒 ≤ 核数


def test_high_tier_degrades_to_one_worker_cpu_count_monkeypatch(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """退化②:monkeypatch os.cpu_count=2(无 env 钩子)→ high/reserve1 → 1。

    A164 核数解析链:cpu_profile.detect() 缺 env → os.cpu_count()(被替身
    固定为 2)→ 同一 §1 公式退化到 1——两条核数注入路径结论恒一致。
    """
    cpu_profile = pytest.importorskip(
        MOD_CPU_PROFILE, reason="A163 ops.cpu_profile 未就位(并行开发中)"
    )
    concurrency = pytest.importorskip(
        MOD_CONCURRENCY, reason="A164 ops.concurrency 未就位(并行开发中)"
    )
    import os

    monkeypatch.setattr(os, "cpu_count", lambda: 2)
    assert "NETSENTINEL_FAKE_CORES" not in __import__("os").environ
    assert cpu_profile.detect()["cores"] == 2
    cfg = _make_cfg(tmp_path, tier="high", reserve=1)
    assert concurrency.io_workers(cfg) == 1
    # 对照档:同一台"单核机"上 low/mid 同样退化到 1(2//4 与 2//2 均 max(1,·))
    low_cfg = _make_cfg(tmp_path, tier="low", reserve=1)
    mid_cfg = _make_cfg(tmp_path, tier="mid", reserve=1)
    assert concurrency.io_workers(low_cfg) == 1
    assert concurrency.io_workers(mid_cfg) == 1


def test_finish_high_tier_single_core_outcome_workers_one(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """退化③:finish(注入 scan + 真 A168 SummaryAgent)在 2 核高档下 workers=1。

    - workers 以 ``workers=`` 关键字透传给注入扫描函数(A169 契约);
    - 真 SummaryOutcome.workers 也解析为 1(A168 经 A164 io_workers);
    - 空批量 outcome 结构齐备(groups/ready/attest 全 0)+ 真收官报告落盘。
    """
    finishflow = pytest.importorskip(
        MOD_FINISHFLOW, reason="A169 finishflow 未就位(并行开发中)"
    )
    pytest.importorskip(
        MOD_SUMMARY_AGENT, reason="A168 agent.summary_agent 未就位(并行开发中)"
    )
    pytest.importorskip(
        MOD_RUN_SUMMARY, reason="A172 report.run_summary 未就位(并行开发中)"
    )
    monkeypatch.setenv("NETSENTINEL_FAKE_CORES", "2")
    cfg = _make_cfg(tmp_path, tier="high", reserve=1)
    scan = _RecordingScan()
    outcome = finishflow.finish(["http://127.0.0.1:9/none"], cfg, scan=scan)

    # workers 透传 + 单核高档退化(outcome 与扫描函数两侧一致为 1)
    assert len(scan.calls) == 1
    assert scan.calls[0]["kwargs"]["workers"] == 1
    assert outcome["workers"] == 1
    assert outcome["tier"] == "high"
    # 空批量结构:groups/ready_count/attest_pending 全 0,真报告落盘(红线 36 字样)
    assert outcome["groups"] == 0
    assert outcome["ready_count"] == 0
    assert outcome["attest_pending"] == 0
    assert outcome["sites"] == 1 and outcome["scanned"] == 0
    md = pathlib.Path(outcome["report_md"])
    html = pathlib.Path(outcome["report_html"])
    assert md.is_file() and html.is_file()
    md_text = md.read_text(encoding="utf-8")
    assert "结案代理生成" in md_text and "人工声明" in md_text
    out = capsys.readouterr().out
    assert "worker 1 个" in out  # 中文收官汇总表明示退化后的 worker 数


# ---------------------------------------------------------------------------
# 用例组 2:真扫描收官(本地 2 站 → finish → outcome + 报告落盘)
# ---------------------------------------------------------------------------
def test_real_two_site_finish_outcome_and_report_files(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """收官①(全真链):2 站 tier=mid 真扫描 → finish → outcome 与报告双件。

    链路:tier_once → io_workers(mid, 4 核伪造 → 2)→ 真 batch_scan →
    真 run_pool → 真 orchestrator(真发现/退化采样/真下载/桩分类/真判定/
    真打包/真入列)→ 真 group_and_enqueue 归组 → 真 SummaryAgent 汇总 →
    真 A172 render_run_summary 落盘。断言:

    - 扫描计数:sites==2 / scanned==2 / failed==0 / skipped==0;
    - outcome:groups>=1(两张 nsfw_hi → SUSPECT 必入列)、workers==2
      (== ops.concurrency.io_workers(cfg),红线 35 只影响本地池并发);
    - 报告:{data_dir}/run_summary.md 与 .html 均落盘,MD 含
      "结案代理生成"与"人工声明"(A172 固定结论句,红线 36)与档位段;
    - 举报准备结构:刚扫完全 pending → ready_count==0、attest_pending==0;
    - 真复核队列确有 ``[组:名]`` 入列条目(供后续声明 → 举报链)。
    """
    finishflow = pytest.importorskip(
        MOD_FINISHFLOW, reason="A169 finishflow 未就位(并行开发中)"
    )
    concurrency = pytest.importorskip(
        MOD_CONCURRENCY, reason="A164 ops.concurrency 未就位(并行开发中)"
    )
    review_queue_mod = pytest.importorskip(
        MOD_REVIEW_QUEUE, reason="A9 decision.review_queue 未就位(并行开发中)"
    )
    monkeypatch.setenv("NETSENTINEL_FAKE_CORES", "4")  # mid → 4//2 = 2 workers
    cfg = _make_cfg(tmp_path, tier="mid")
    urls = _start_two_sites(tmp_path)

    outcome = finishflow.finish(urls, cfg)

    assert outcome["sites"] == 2
    assert outcome["scanned"] == 2, outcome
    assert outcome["failed"] == 0
    assert outcome["skipped"] == 0
    assert outcome["groups"] >= 1  # 真归组:SUSPECT 组必在
    assert outcome["tier"] == "mid"
    assert outcome["workers"] == 2 == concurrency.io_workers(cfg)
    assert outcome["ready_count"] == 0  # 刚扫完全 pending:零就绪
    assert outcome["attest_pending"] == 0  # 无 approved:零待声明

    # 收官报告双件落盘 + 红线 36 固定结论句 + 档位段
    md = pathlib.Path(outcome["report_md"])
    html = pathlib.Path(outcome["report_html"])
    assert md.is_file() and html.is_file()
    assert md.parent == pathlib.Path(cfg.data_dir)
    md_text = md.read_text(encoding="utf-8")
    assert "结案代理生成" in md_text
    assert "人工声明" in md_text
    assert "并发档位" in md_text and "mid" in md_text
    html_text = html.read_text(encoding="utf-8")
    assert "结案代理生成" in html_text and "人工声明" in html_text

    # 真队列:存在带 [组:名] 标记的 pending 条目(红线 24:批量仍逐条人工门)
    queue = review_queue_mod.ReviewQueue(cfg.db_path)
    try:
        group_entries = [
            e for e in queue.list("pending") if (e.note or "").startswith("[组:")
        ]
    finally:
        queue.close()
    assert group_entries, "真归组入列后,队列应有 [组:名] pending 条目"

    out = capsys.readouterr().out
    assert "收官汇总" in out  # 中文收官汇总表已打印
    assert "礼貌间隔与举报频控不随档位放宽" in out  # 红线 35 提示随表输出


def test_real_scan_readiness_ready_and_attest_pending(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """收官②:真扫描 → approve → attest 一个组 → ready/attest_pending 正确。

    链路:真 batch_scan(2 站,真池真编排)→ 真 SummaryAgent(第 1 次:
    真归组入列 + 全 pending → ready 0 / attest [])→ 人工 approve 全部
    ``[组:名]`` 条目 → 只声明(attest)第一个组 → 真 ``ready_entries`` 门控
    (未声明组整组排除,红线 25)→ 真 SummaryAgent(第 2 次,同一 scan_result):
    ``ready_count`` == 已声明组 approved 条数、``attest_pending`` == 未声明
    组名清单(首见序);组摘要行结构 ``{name, sites, urls, verdict, agg_max}``。
    """
    batch_scan_mod = pytest.importorskip(
        MOD_BATCH_SCAN, reason="A108 ops.batch_scan 未就位(并行开发中)"
    )
    summary_mod = pytest.importorskip(
        MOD_SUMMARY_AGENT, reason="A168 agent.summary_agent 未就位(并行开发中)"
    )
    review_queue_mod = pytest.importorskip(
        MOD_REVIEW_QUEUE, reason="A9 decision.review_queue 未就位(并行开发中)"
    )
    batch_review_mod = pytest.importorskip(
        MOD_BATCH_REVIEW, reason="A110 decision.batch_review 未就位(并行开发中)"
    )
    monkeypatch.setenv("NETSENTINEL_FAKE_CORES", "4")
    cfg = _make_cfg(tmp_path, tier="mid")
    urls = _start_two_sites(tmp_path)

    scan_result = batch_scan_mod.batch_scan(urls, cfg)
    assert len(scan_result["reports"]) == 2

    first = summary_mod.SummaryAgent().run(scan_result, cfg)
    assert first.ready_count == 0 and first.attest_pending == []
    rows = first.groups
    assert rows, "真扫描(各 2 张 nsfw_hi → SUSPECT)至少归出 1 组"
    for row in rows:
        assert set(row) == {"name", "sites", "urls", "verdict", "agg_max"}
        assert row["name"] and row["sites"] >= 1 and row["urls"] >= 1
        assert row["verdict"] in ("clean", "suspect", "nsfw")
        assert row["agg_max"] >= 0.9  # nsfw_hi 桩分 0.97

    # 人工链:approve 全部 [组:名] 条目 → 只声明第一个组
    queue = review_queue_mod.ReviewQueue(cfg.db_path)
    try:
        approved_ids: list[tuple[int, str]] = []
        for entry in queue.list("pending"):
            if (entry.note or "").startswith("[组:"):
                queue.approve(entry.id)
                group = batch_review_mod.resolve_group_name(entry.note, entry.site_url)
                approved_ids.append((entry.id, group))
    finally:
        queue.close()
    assert approved_ids, "归组入列的条目应可被人工 approve"

    attested_group = rows[0]["name"]
    review = batch_review_mod.BatchReview(cfg.db_path)
    try:
        review.attest(attested_group, items=1, reviewer="张三", text=VALID_ATTEST_TEXT)
    finally:
        review.close()

    # 期望值(按 A110 语义动态计算):已声明组 → ready;未声明组 → 待声明
    expected_ready = sum(1 for _, g in approved_ids if g == attested_group)
    pending_order: list[str] = []
    for _, g in approved_ids:
        if g != attested_group and g not in pending_order:
            pending_order.append(g)
    assert expected_ready >= 1 or not pending_order  # 至少一组已声明或全部同组

    ready = batch_review_mod.ready_entries(cfg)
    assert all(item["group_name"] == attested_group for item in ready)
    assert len(ready) == expected_ready
    for item in ready:
        assert set(item) == {"entry_id", "group_name", "site_url", "evidence_zip", "portal"}
        assert isinstance(item["entry_id"], int)
        assert item["portal"] == "12377"

    second = summary_mod.SummaryAgent().run(scan_result, cfg)
    assert second.ready_count == expected_ready
    assert list(second.attest_pending) == pending_order  # 首见序、仅未声明组


# ---------------------------------------------------------------------------
# 用例组 3:依次举报干跑(真队列 → 声明 → ready → 真链干跑)
# ---------------------------------------------------------------------------
def test_run_report_dry_run_real_chain_zero_submitted(tmp_path: pathlib.Path) -> None:
    """举报①(全真链干跑):run_report 缺省真 run_batch → 真 executor dry 路径。

    cfg.browser_session_reuse=False(不构造 V7 会话执行器)→
    SequentialReportAgent 缺省编排 A112 run_batch:真 plan_12377 计划 +
    真 executor_playwright ``dry_run`` 路径(纯文本逐 Step 记 notes,
    不启动浏览器、不真实提交)。断言:

    - 新建批次 1(3 条就绪:G1×2 + G2×1);
    - ``submitted == 0``(干跑绝不真实提交,红线 24/36);
    - ``results`` 长度 == 条目数,逐条 ok=True / submitted=False
      (干跑成功未提交 → 计 skipped,不算失败);
    - 批次状态台账(A113)三条全部 skipped;频控未触发。
    """
    finishflow = pytest.importorskip(
        MOD_FINISHFLOW, reason="A169 finishflow 未就位(并行开发中)"
    )
    review_queue_mod = pytest.importorskip(
        MOD_REVIEW_QUEUE, reason="A9 decision.review_queue 未就位(并行开发中)"
    )
    batch_review_mod = pytest.importorskip(
        MOD_BATCH_REVIEW, reason="A110 decision.batch_review 未就位(并行开发中)"
    )
    batch_state_mod = pytest.importorskip(
        MOD_BATCH_STATE, reason="A113 submit.batch_state 未就位(并行开发中)"
    )
    cfg = _make_cfg(tmp_path)

    queue = review_queue_mod.ReviewQueue(cfg.db_path)
    try:
        ids = [
            _queue_add_group_entry(queue, "http://127.0.0.1:8000/a", "G1"),
            _queue_add_group_entry(queue, "http://127.0.0.1:8000/b", "G1"),
            _queue_add_group_entry(queue, "http://127.0.0.2:8000/c", "G2"),
        ]
        for eid in ids:
            queue.approve(eid)
    finally:
        queue.close()
    review = batch_review_mod.BatchReview(cfg.db_path)
    try:
        review.attest("G1", items=2, reviewer="张三", text=VALID_ATTEST_TEXT)
        review.attest("G2", items=1, reviewer="李四", text=VALID_ATTEST_TEXT)
    finally:
        review.close()
    items = batch_review_mod.ready_entries(cfg)
    assert len(items) == 3

    report = finishflow.run_report(cfg, dry_run=True)

    agent_out = report["result"]  # SequentialReportAgent.run 的返回 dict
    batch_summary = agent_out["result"]  # A112 run_batch 摘要
    assert agent_out["submitted"] == 0
    assert batch_summary["submitted"] == 0  # 干跑:submitted==0
    assert batch_summary["failed"] == 0
    assert batch_summary["rate_limited"] is False
    assert batch_summary["paused"] is False
    results = batch_summary["results"]
    assert len(results) == len(items) == 3  # 逐条执行:长度==条目数
    for row in results:
        assert row["ok"] is True
        assert row["submitted"] is False  # 每条都未真实提交
        assert row["portal"] == "12377"
    assert {row["entry_id"] for row in results} == {i["entry_id"] for i in items}

    # 批次台账:新建批次 1,三条全部 skipped(干跑成功未提交)
    assert agent_out["batch_id"] == 1
    st = batch_state_mod.BatchState(cfg.db_path)
    try:
        summary = st.summary(1)
    finally:
        st.close()
    assert summary["total"] == 3
    assert summary["skipped"] == 3
    assert summary["submitted"] == 0


def test_sequential_agent_on_item_order_and_finish_report(tmp_path: pathlib.Path) -> None:
    """举报②:SequentialReportAgent 直调干跑——on_item 逐条 1..n 保序 + 报告。

    真 run_batch(无注入 runner)+ 真 plan_12377(on_item 经计划器包装触发)
    + 真 executor dry 路径;``batch_id`` 给定时结束后经真 A114
    render_batch_report 渲染 ``finish_report_{batch_id}.html`` 落盘。
    """
    sequential_mod = pytest.importorskip(
        MOD_SEQUENTIAL, reason="A170 agent.sequential_report 未就位(并行开发中)"
    )
    review_queue_mod = pytest.importorskip(
        MOD_REVIEW_QUEUE, reason="A9 decision.review_queue 未就位(并行开发中)"
    )
    pytest.importorskip(
        "netsentinel.report.batch_report",
        reason="A114 report.batch_report 未就位(并行开发中)",
    )
    cfg = _make_cfg(tmp_path)

    queue = review_queue_mod.ReviewQueue(cfg.db_path)
    try:
        ids = [
            _queue_add_group_entry(queue, "http://127.0.0.1:8000/x", "GX"),
            _queue_add_group_entry(queue, "http://127.0.0.2:8000/y", "GX"),
        ]
        for eid in ids:
            queue.approve(eid)
    finally:
        queue.close()

    items = [
        {"entry_id": ids[0], "group_name": "GX", "site_url": "http://127.0.0.1:8000/x",
         "evidence_zip": "out/e1.zip", "portal": "12377"},
        {"entry_id": ids[1], "group_name": "GX", "site_url": "http://127.0.0.2:8000/y",
         "evidence_zip": "out/e2.zip", "portal": "12377"},
    ]
    seen: list[tuple[int, int, Any]] = []
    out = sequential_mod.SequentialReportAgent().run(
        items, cfg, dry_run=True,
        on_item=lambda i, n, item: seen.append((i, n, item["entry_id"])),
        batch_id=7,
    )

    # 逐条进度:1 起序、总数正确、按条目顺序(每条实际进入执行前触发)
    assert seen == [(1, 2, ids[0]), (2, 2, ids[1])]
    assert out["submitted"] == 0
    assert out["result"]["submitted"] == 0
    assert len(out["result"]["results"]) == 2
    # 结案报告(A114 真 render)落盘到 cfg.data_dir/finish_report_7.html
    report_path = pathlib.Path(out["report_path"])
    assert report_path.is_file()
    assert report_path.parent == pathlib.Path(cfg.data_dir)
    assert "finish_report_7" in report_path.name


def test_real_planner_and_dry_executor_pure_text(tmp_path: pathlib.Path) -> None:
    """举报③:真 plan_12377 + 真 executor dry_run —— 纯文本、无浏览器、人工门保留。

    这是依次举报干跑链的单条底座:计划含 HUMAN_GATE(验证码仅人工输入),
    dry_run 不启动浏览器、逐 Step 记中文 notes、ok=True / submitted=False。
    """
    portal_mod = pytest.importorskip(
        MOD_PORTAL_12377, reason="A10 submit.portal_12377 未就位(并行开发中)"
    )
    executor_mod = pytest.importorskip(
        MOD_EXECUTOR, reason="A13 submit.executor_playwright 未就位(并行开发中)"
    )
    cfg = _make_cfg(tmp_path)
    item = {
        "entry_id": 1, "group_name": "G", "portal": "12377",
        "site_url": "http://127.0.0.1:8000/z", "evidence_zip": "out/e.zip",
    }
    plan = portal_mod.plan_12377(item, cfg)
    assert any(s.action.value == "human_gate" for s in plan.steps)  # 人工门必须在
    result = executor_mod.execute(plan, cfg, auto_confirm=False, dry_run=True)
    assert result.ok is True
    assert result.submitted is False  # 干跑绝不真实提交
    assert any("干跑跳过" in note for note in result.notes)  # 人工门干跑跳过留痕
    assert any(("人工" in note or "验证码" in note) for note in result.notes)


# ---------------------------------------------------------------------------
# 用例组 4:红线 35 —— cfg 礼貌/频控字段全链前后不变
# ---------------------------------------------------------------------------
def test_redline35_cfg_snapshot_unchanged_after_finish_and_report(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """红线 35:真扫描 finish + 干跑 run_report 全链前后 cfg 全字段快照相等。

    快照覆盖 ``fetch_delay_s`` 与全部提交频控字段(``submit_min_interval_s``
    / ``submit_max_per_day`` / ``batch_item_interval_s`` / ``discovery_query_delay_s``)
    及安全开关(``allow_network`` / ``respect_robots`` / ``human_gate_required``)
    ——用 ``dataclasses.asdict`` 整体比对,档位(mid)与 workers 全程不影响
    任何礼貌/频控参数。
    """
    finishflow = pytest.importorskip(
        MOD_FINISHFLOW, reason="A169 finishflow 未就位(并行开发中)"
    )
    review_queue_mod = pytest.importorskip(
        MOD_REVIEW_QUEUE, reason="A9 decision.review_queue 未就位(并行开发中)"
    )
    batch_review_mod = pytest.importorskip(
        MOD_BATCH_REVIEW, reason="A110 decision.batch_review 未就位(并行开发中)"
    )
    monkeypatch.setenv("NETSENTINEL_FAKE_CORES", "4")
    cfg = _make_cfg(tmp_path, tier="mid")
    snapshot = dataclasses.asdict(cfg)
    politeness = {
        key: snapshot[key]
        for key in (
            "fetch_delay_s", "submit_min_interval_s", "submit_max_per_day",
            "batch_item_interval_s", "discovery_query_delay_s",
        )
    }
    assert politeness["fetch_delay_s"] == 0.0  # 本地回环演示的显式配置
    assert politeness["submit_min_interval_s"] == 60  # 频控缺省不被放宽

    urls = _start_two_sites(tmp_path)
    outcome = finishflow.finish(urls, cfg)
    assert outcome["scanned"] == 2 and outcome["groups"] >= 1
    assert dataclasses.asdict(cfg) == snapshot  # finish 全链零改写

    # 接举报准备干跑:approve + attest 全部组 → run_report(真链干跑)
    queue = review_queue_mod.ReviewQueue(cfg.db_path)
    try:
        groups: list[str] = []
        for entry in queue.list("pending"):
            if (entry.note or "").startswith("[组:"):
                queue.approve(entry.id)
                name = batch_review_mod.resolve_group_name(entry.note, entry.site_url)
                if name not in groups:
                    groups.append(name)
    finally:
        queue.close()
    review = batch_review_mod.BatchReview(cfg.db_path)
    try:
        for name in groups:
            review.attest(name, items=1, reviewer="张三", text=VALID_ATTEST_TEXT)
    finally:
        review.close()

    report = finishflow.run_report(cfg, dry_run=True)
    assert report["result"]["submitted"] == 0  # 干跑链确已真实走通
    assert len(report["result"]["result"]["results"]) >= 1
    assert dataclasses.asdict(cfg) == snapshot  # run_report 全链零改写
    assert cfg.fetch_delay_s == 0.0
    assert cfg.submit_min_interval_s == 60
    assert cfg.submit_max_per_day == 5
    assert cfg.batch_item_interval_s == 90


def test_cli_tier_override_high_single_core(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """红线 35(CLI):--tier high 覆盖只换档位,礼貌/频控不动,零持久档副作用。

    真 ``main`` 入口:tmp 配置文件(真 load_config)+ 批量清单(两个不可达
    回环地址,连接拒绝即失败/落 CLEAN,秒级完成)→ ``--tier high`` 经
    ``dataclasses.replace`` 生成新 Config → 收官流程输出明示
    "礼貌间隔与举报频控不变";2 核伪造下高档退化 worker 1;返回码 0;
    ``concurrency_auto=False`` 全程零写 data/concurrency.json。
    """
    finishflow = pytest.importorskip(
        MOD_FINISHFLOW, reason="A169 finishflow 未就位(并行开发中)"
    )
    pytest.importorskip("yaml", reason="pyyaml 未安装,无法走真 load_config 文件链")
    monkeypatch.setenv("NETSENTINEL_FAKE_CORES", "2")
    data_dir = tmp_path / "data"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "data_dir: " + str(data_dir).replace("\\", "/") + "\n"
        "evidence_dir: " + str(data_dir / "evidence").replace("\\", "/") + "\n"
        "db_path: " + str(data_dir / "review_queue.db").replace("\\", "/") + "\n"
        "audit_path: " + str(data_dir / "audit.jsonl").replace("\\", "/") + "\n"
        "log_path: " + str(data_dir / "logs" / "netsentinel.log").replace("\\", "/") + "\n"
        "fetch_delay_s: 0.0\n"
        "browser_session_reuse: false\n"
        "concurrency_tier: mid\n"
        "concurrency_auto: false\n"
        "classifier: stub\n",
        encoding="utf-8",
    )
    bulk_path = tmp_path / "leads.txt"
    bulk_path.write_text(
        "http://127.0.0.1:1/none-a\nhttp://127.0.0.2:1/none-b\n", encoding="utf-8"
    )

    rc = finishflow.main(
        ["--input", str(bulk_path), "--config", str(config_path), "--tier", "high"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "已覆盖并发档位:high" in out
    assert "礼貌间隔与举报频控不变" in out  # 红线 35 明示
    assert "并发档位: high" in out and "worker 1 个" in out  # 单核高档退化
    # concurrency_auto=False:零持久档副作用(不写 data/concurrency.json)
    assert not (data_dir / "concurrency.json").exists()


# ---------------------------------------------------------------------------
# 用例组 5:站点构造与收尾自证
# ---------------------------------------------------------------------------
def test_two_loopback_sites_serve_distinct_hosts(tmp_path: pathlib.Path) -> None:
    """站点自证:127.0.0.1 与 127.0.0.2 各自可服务,回环互访,PNG 合法。"""
    import struct
    import urllib.request

    urls = _start_two_sites(tmp_path)
    assert {url.split("//", 1)[1].split(":", 1)[0] for url in urls} == {
        "127.0.0.1", "127.0.0.2"
    }
    for url, images in zip(urls, (SITE_A_IMAGES, SITE_B_IMAGES)):
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 仅回环
            assert resp.status == 200
            html = resp.read().decode("utf-8")
        for name in images:
            assert f'src="{name}"' in html
            with urllib.request.urlopen(  # noqa: S310 仅回环
                url.rsplit("/", 1)[0] + "/" + name, timeout=10
            ) as img_resp:
                assert img_resp.status == 200
                head = img_resp.read(24)
        assert head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR"
        width, height = struct.unpack(">II", head[16:24])
        assert (width, height) == (200, 200)  # 过 min_image_px 小图过滤


def test_teardown_stops_servers_and_threads(tmp_path: pathlib.Path) -> None:
    """收尾:注册表内全部服务器可停、端口不再应答、线程退出、注册表清空。"""
    import urllib.error
    import urllib.request

    urls = _start_two_sites(tmp_path)
    servers = [srv for srv, _ in _ACTIVE_SERVERS]
    threads = [t for _, t in _ACTIVE_SERVERS]
    assert len(servers) == 2 and all(t.is_alive() for t in threads)

    _stop_all_servers()  # autouse teardown 同款路径,显式跑一遍以断言可停性
    assert _ACTIVE_SERVERS == []
    assert all(not t.is_alive() for t in threads)
    for url in urls:
        try:
            urllib.request.urlopen(url, timeout=5)  # noqa: S310 仅回环
            raised = False
        except (urllib.error.URLError, OSError):
            raised = True
        assert raised, f"停止后端口不应再应答:{url}"
