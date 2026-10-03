# -*- coding: utf-8 -*-
"""净网哨兵(NetSentinel)V7 内核演练场(离线)—— A140。

依次演示 V7 八大内核中七个可直接离线观察的代际升级(全中文分节;
存储内核 SQLiteKernel 属持久化基建,由 A131/A138 的自检覆盖,不在本演练场):

①  识别内核(vision/heuristic_kernel,A123):合成"肤色椭圆占主体"图 vs
    风景/文本图各 4 张,经 stdlib zlib 写成真 PNG 后走公开 API
    ``SkinHeuristicClassifier.classify``,输出 skin 评分对比表;
②  决策内核(decision/sprt,A125):20 张序列(0.97×5 + 0.5×15)逐张送审,
    打印 LLR 游走表,早停于第 N 张并打印"省 N' 张送审预算";
    附 next_images(不确定度升序送审)的互补口径;
③  检索内核(intel/phash_lsh,A127):1000 条合成 64bit 指纹建分带 LSH 索引,
    单查询 compare_calls(汉明比较次数)vs 全表扫描 1000 次;
④  执行内核(submit/executor_session,A129):FakeLauncher/FakeContext
    内存替身跑 3 个极小计划,launch 计数 1 vs 旧执行方式 3;
⑤  调度内核(ops/sched_kernel,A130):10 项候选、预算 4 的优先级轮选表;
⑥  观测内核(telemetry_export,A137):telemetry 打点后 render_metrics()
    输出前 8 行(Prometheus exposition 片段);
⑦  融合内核(decision/reliability + fusion_reliable,A126):同一分值集合,
    等权 vs 喂本地反馈后可靠性加权的 agg 对比。

安全红线(与 CONTRACTS-V7.md §0 一致):

- **零外呼**:不联网、不调用任何 VLM/模型 API;皮肤图/指纹/反馈全部为
  本脚本确定性构造的合成数据;
- **零真实提交**:执行内核演示注入内存替身(FakeLauncher/FakeContext/
  FakePage),绝不启动 chromium;演示计划不含提交按钮与人工门步骤,
  ``submitted`` 恒为 False;页面地址仅用本地 ``file://`` 夹具
  (tests/fixtures/mini_form.html),绝不访问 www.12377.cn / www.shdf.gov.cn;
- **纯新增**:本脚本不改任何既有模块;V7 内核开关默认关,生产行为不变。

用法:python scripts/demo_kernel_evolution.py
退出码:0 = 演示成功;2 = V7 兄弟内核模块未就位;1 = 其他错误。
"""
from __future__ import annotations

import random
import struct
import sys
import tempfile
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: 项目根目录(脚本位于 <root>/scripts/)
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from netsentinel import telemetry  # noqa: E402
from netsentinel.contracts import (  # noqa: E402
    Config,
    ImageEvidence,
    ImageScore,
    Portal,
    SiteReport,
    Step,
    StepAction,
    SubmissionPayload,
    SubmissionPlan,
    Verdict,
)

#: 演示依赖的 V7 兄弟内核(模块名 → 中文角色说明);任一缺失都以退出码 2 结束
REQUIRED_SIBLING_MODULES: list[tuple[str, str]] = [
    ("netsentinel.vision.heuristic_kernel", "识别内核·肤色启发式(A123)"),
    ("netsentinel.decision.sprt", "决策内核·序贯检验(A125)"),
    ("netsentinel.decision.reliability", "融合内核·可靠性追踪(A126)"),
    ("netsentinel.decision.fusion_reliable", "融合内核·可靠性加权(A126)"),
    ("netsentinel.intel.phash_lsh", "检索内核·分带 LSH(A127)"),
    ("netsentinel.submit.executor_session", "执行内核·会话复用(A129)"),
    ("netsentinel.ops.sched_kernel", "调度内核·优先级预算(A130)"),
    ("netsentinel.telemetry_export", "观测内核·指标导出(A137)"),
    ("netsentinel.decision.fusion", "既有特征融合(A29,V6 基线)"),
]

#: 本地模拟举报页面(红线:只允许 file:// / 127.0.0.1,绝不访问真实门户)
MINI_FORM_URI = (ROOT / "tests" / "fixtures" / "mini_form.html").resolve().as_uri()

#: 判定结果的中文标签
_VERDICT_CN = {"clean": "无风险", "suspect": "疑似", "nsfw": "高置信色情"}


# ---------------------------------------------------------------------------
# 通用小工具
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


def _check_sibling_modules() -> list[str]:
    """逐一探测依赖的兄弟内核;返回缺失模块的中文清单(空 = 全部就位)。"""
    import importlib

    missing: list[str] = []
    for name, role in REQUIRED_SIBLING_MODULES:
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 - 任何导入失败都按"未就位"报告
            missing.append(f"{name}({role})")
    return missing


def _sec(no: str, title: str) -> None:
    """打印分节横幅。"""
    print()
    print("=" * 74)
    print(f"{no} {title}")
    print("=" * 74)


def _dw(text: str) -> int:
    """字符串的终端显示宽度(粗口径:CJK/全角字符按 2 列计)。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def _ljust(text: str, width: int) -> str:
    """按显示宽度左对齐补空格(中文两列,表格对齐用)。"""
    return text + " " * max(0, width - _dw(text))


def _rjust(text: str, width: int) -> str:
    """按显示宽度右对齐补空格(中文两列,表格对齐用)。"""
    return " " * max(0, width - _dw(text)) + text


def _write_png(path: Path, width: int, height: int, rows: list[list[tuple[int, int, int]]]) -> None:
    """纯 stdlib(struct + zlib)把 RGB 像素行写成最小合法 PNG。

    与 scripts/make_png.py 同款块结构(IHDR/IDAT/IEND、filter=0、色型 2),
    但支持逐像素不同颜色(合成肤色椭圆/风景需要),供 skin 内核的
    stdlib PNG 解码路径离线消费。
    """
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    path.parent.mkdir(parents=True, exist_ok=True)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(
        b"\x00" + b"".join(bytes(pixel) for pixel in row) for row in rows
    )
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# ① 识别内核:skin 评分对比表
# ---------------------------------------------------------------------------


def demo_skin_classifier(workdir: Path) -> None:
    """肤色椭圆主体图 vs 风景/文本图各 4 张 → SkinHeuristicClassifier 评分表。"""
    from netsentinel.vision.heuristic_kernel import (
        SkinHeuristicClassifier,
        synthetic_clean_pixels,
        synthetic_skin_pixels,
    )

    _sec("①", "识别内核·肤色启发式(A123):skin 评分对比(合成图,零模型权重)")

    clf = SkinHeuristicClassifier(Config())
    cases: list[tuple[str, str, Callable[[int], tuple[int, int, list[list[tuple[int, int, int]]]]]]] = [
        ("skin_0.png", "肤色椭圆", synthetic_skin_pixels),
        ("clean_0.png", "风景/文本", synthetic_clean_pixels),
        ("skin_1.png", "肤色椭圆", synthetic_skin_pixels),
        ("clean_1.png", "风景/文本", synthetic_clean_pixels),
        ("skin_2.png", "肤色椭圆", synthetic_skin_pixels),
        ("clean_2.png", "风景/文本", synthetic_clean_pixels),
        ("skin_3.png", "肤色椭圆", synthetic_skin_pixels),
        ("clean_3.png", "风景/文本", synthetic_clean_pixels),
    ]

    print("合成图(stdlib zlib 写 PNG)→ 公开 API classify() 打分:")
    print()
    print(f"{_ljust('图像', 16)}{_ljust('类别', 12)}{_rjust('skin_ratio', 11)}"
          f"{_rjust('max_blob', 10)}{_rjust('edge_density', 13)}{_rjust('nsfw_prob', 10)}")
    print("-" * 76)
    skin_probs: list[float] = []
    clean_probs: list[float] = []
    for seq, (fname, kind, factory) in enumerate(cases):
        index = seq // 2  # skin_k / clean_k 各取 0..3
        width, height, rows = factory(index)
        png_path = workdir / fname
        _write_png(png_path, width, height, rows)
        score = clf.classify(
            ImageEvidence(
                path=str(png_path),
                url=f"https://example.invalid/{fname}",
                source_page="https://example.invalid/",
            )
        )
        (skin_probs if kind == "肤色椭圆" else clean_probs).append(score.nsfw_prob)
        cells = (
            f"{score.scores.get('skin_ratio', 0.0):.4f}",
            f"{score.scores.get('max_blob', 0.0):.4f}",
            f"{score.scores.get('edge_density', 0.0):.4f}",
            f"{score.nsfw_prob:.4f}",
        )
        print(
            _ljust(fname, 16) + _ljust(kind, 12)
            + _rjust(cells[0], 11) + _rjust(cells[1], 10)
            + _rjust(cells[2], 13) + _rjust(cells[3], 10)
        )
    print("-" * 76)
    mean_skin = sum(skin_probs) / len(skin_probs)
    mean_clean = sum(clean_probs) / len(clean_probs)
    print(
        f"均值:肤色椭圆主体图 {mean_skin:.4f}  vs  风景/文本图 {mean_clean:.4f}"
        f"(可分间隔 = {mean_skin - mean_clean:.4f},契约要求 > 0.2)"
    )
    print("解读:YCbCr 肤色占比 + 8x8 连通块 + 边缘密度的线性组合,离线即可把")
    print("      人体色调主体图与冷色调风景/文本图完全分开 —— 粗筛零成本前置。")


# ---------------------------------------------------------------------------
# ② 决策内核:SPRT 早停
# ---------------------------------------------------------------------------


def demo_sprt() -> None:
    """20 张序列(0.97×5 + 0.5×15)的 SPRT 逐张送审与判停。"""
    from netsentinel.decision.sprt import SPRT, next_images

    _sec("②", "决策内核·序贯检验 SPRT(A125):证据够了就收手")

    probs = [0.97] * 5 + [0.5] * 15
    total = len(probs)
    probe = SPRT(0.05, 0.05)
    print(f"序列构造:{total} 张待送审 = 0.97×5(前 5 张高置信)+ 0.5×15(后 15 张不确定)")
    print(f"检验设置:alpha=beta=0.05,p0=0.5 / p1=0.9 → 判决边界 [B, A] = "
          f"[{probe.lower:.4f}, {probe.upper:.4f}](累计对数似然比 LLR)")
    print()
    print(_rjust("张次", 6) + _rjust("nsfw_prob", 11) + _rjust("累计LLR", 11) + _rjust("状态", 10))
    print("-" * 44)
    sprt = SPRT(0.05, 0.05)
    for i, p in enumerate(probs, start=1):
        state = sprt.update(p)
        print(f"{i:>6}{p:>11.2f}{sprt.total_llr:>11.4f}" + _rjust(state, 10))
        if state != "continue":
            break
    saved = total - sprt.n
    print("-" * 44)
    print(f"早停于第 {sprt.n} 张(终态 {sprt.state()!r})→ 省 {saved} 张送审预算"
          f"(送审 {sprt.n}/{total},无需再看剩余 {saved} 张)")

    # 互补口径:next_images 按不确定度升序送审(边缘优先),判停即截断
    cfg = Config()
    candidates = [
        ImageScore(
            image=ImageEvidence(path=f"img{i:02d}.png", url="u", source_page="s"),
            model="ensemble",
            nsfw_prob=p,
        )
        for i, p in enumerate(probs)
    ]
    sent, sprt2 = next_images(candidates, cfg)
    print()
    print(f"互补口径 next_images(不确定度 |p-0.5| 升序,边缘图先送审,预算 "
          f"{cfg.vlm_max_images_per_site}):")
    print(f"  实际送审 {len(sent)} 张即判停(终态 {sprt2.state()!r}),相对全量 "
          f"{total} 张省 {total - len(sent)} 张 VLM 配额")

    print("解读:多数图片落在 0.5 中性带时,证据迅速倒向 clean 一侧 —— 序贯检验")
    print("      让送审张数随证据强度自适应,而非固定烧满预算(开关 use_sprt 默认关)。")


# ---------------------------------------------------------------------------
# ③ 检索内核:LSH vs 全表扫描
# ---------------------------------------------------------------------------


def demo_phash_lsh() -> None:
    """1000 条合成指纹建 LSH 索引:单查询比较次数 vs 全表 1000。"""
    from netsentinel.intel.phash_lsh import DEFAULT_BANDS, LSHIndex

    _sec("③", "检索内核·分带 LSH(A127):近邻检索的亚线性代差")

    rng = random.Random(20261002)  # 固定种子,确定性
    index = LSHIndex(bands=DEFAULT_BANDS)
    entries: list[str] = []
    for seq in range(1000):
        hex64 = f"{rng.getrandbits(64):016x}"
        index.insert(hex64, seq)
        entries.append(hex64)
    stats = index.stats()
    print(f"索引规模:{stats['entries']} 条随机 64bit 指纹,{DEFAULT_BANDS} 带 × 16bit 桶键,"
          f"非空桶位 {stats['buckets']}")

    probe = entries[0]
    probe_value = int(probe, 16)
    before = index.compare_calls
    hits = index.query(probe, max_distance=8)
    lsh_compares = index.compare_calls - before

    brute_hits = sorted(
        seq
        for seq, hex64 in enumerate(entries)
        if (probe_value ^ int(hex64, 16)).bit_count() <= 8
    )
    assert sorted(hits) == brute_hits, "LSH 命中必须与全表扫描一致(演练场自检)"

    print()
    print(_ljust("查询", 26) + _rjust("LSH 汉明比较", 14) + _rjust("全表扫描比较", 14) + _rjust("命中", 8))
    print("-" * 64)
    print(_ljust("精确指纹(表内第 0 条)", 26) + f"{lsh_compares:>14}{len(entries):>14}{len(hits):>8}")

    # 近重复指纹:翻转 3 位(d ≤ bands-1 = 3,保证召回)
    near_value = probe_value
    for bit in (0, 9, 40):
        near_value ^= 1 << bit
    near_hex = f"{near_value:016x}"
    before = index.compare_calls
    near_hits = index.query(near_hex, max_distance=8)
    near_compares = index.compare_calls - before
    print(_ljust("近重复(汉明距离 3)", 26) + f"{near_compares:>14}{len(entries):>14}{len(near_hits):>8}")
    print("-" * 64)
    ratio = lsh_compares / len(entries)
    print(f"单查询比较次数 {lsh_compares} vs 全表 {len(entries)}"
          f"(比较比例 {ratio:.1%},契约红线 31 上限 10%)")
    print("解读:任一带同桶才成为候选再做精确汉明过滤 —— d ≤ 3 时保证 100% 召回,")
    print("      比较次数却从 O(N) 降到个位数,跨案件撞图检索不再随库容线性变慢。")


# ---------------------------------------------------------------------------
# ④ 执行内核:会话复用(内存替身)
# ---------------------------------------------------------------------------


class FakePage:
    """离线假页面:只记录调用,截图写假字节;绝不触发真实浏览器。"""

    def __init__(self) -> None:
        self.goto_calls: list[str] = []
        self.wait_calls: list[int] = []
        self.screenshot_calls: list[str] = []
        self.close_calls = 0

    def goto(self, url: str, timeout: int | None = None) -> None:  # noqa: ANN001
        self.goto_calls.append(url)

    def wait_for_timeout(self, ms: int) -> None:
        self.wait_calls.append(ms)

    def select_option(self, selector: str, value: str) -> None:  # noqa: ARG02
        pass

    def fill(self, selector: str, value: str) -> None:  # noqa: ARG02
        pass

    def click(self, selector: str) -> None:
        pass

    def screenshot(self, path: str | None = None, full_page: bool = False) -> None:  # noqa: ARG02
        self.screenshot_calls.append(str(path))
        Path(path).write_bytes(b"fake-png")

    def close(self) -> None:
        self.close_calls += 1


class FakeContext:
    """离线假 BrowserContext:new_page 逐页记录。"""

    def __init__(self) -> None:
        self.pages: list[FakePage] = []
        self.close_calls = 0

    def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.close_calls += 1


class FakeBrowser:
    """离线假浏览器:launch 计数是本节的核心操作计数。"""

    def __init__(self) -> None:
        self.launch_count = 0
        self.contexts: list[FakeContext] = []
        self.close_calls = 0
        self.chromium = FakeChromium(self)

    def new_context(self) -> FakeContext:
        context = FakeContext()
        self.contexts.append(context)
        return context

    def close(self) -> None:
        self.close_calls += 1


class FakeChromium:
    """离线假 chromium:launch(headless) 返回同一假浏览器并计数。"""

    def __init__(self, browser: FakeBrowser) -> None:
        self._browser = browser

    def launch(self, headless: bool = True) -> FakeBrowser:  # noqa: ARG02
        self._browser.launch_count += 1
        return self._browser


class FakePlaywright:
    """离线假 Playwright 对象(.start() 之后的那层)。"""

    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium

    def stop(self) -> None:
        pass


class _FakePWContext:
    """sync_playwright() 的返回:.start() 取得 Playwright 对象。"""

    def __init__(self, pw: FakePlaywright) -> None:
        self._pw = pw

    def start(self) -> FakePlaywright:
        return self._pw


class _FakeSyncPlaywright:
    """语义同 playwright.sync_api.sync_playwright:调用返回带 start() 的上下文。"""

    def __init__(self, pw: FakePlaywright) -> None:
        self._pw = pw

    def __call__(self) -> _FakePWContext:
        return _FakePWContext(self._pw)


class FakeLauncher:
    """注入用 launcher:无参调用返回 pw 对象(同 _import_sync_playwright 语义)。"""

    def __init__(self) -> None:
        self.browser = FakeBrowser()

    def __call__(self) -> _FakeSyncPlaywright:
        return _FakeSyncPlaywright(FakePlaywright(self.browser.chromium))


def _mini_plan(seq: int) -> SubmissionPlan:
    """第 seq 个极小举报计划:goto 本地夹具 + 短等待 + 截图(无提交/无人工门)。"""
    payload = SubmissionPayload(
        portal=Portal.P12377,
        site_url="http://example.invalid/site",
        category="色情低俗信息",
        description="该站点存在大量疑似色情图片,经辅助系统初筛并人工核实,附证据包。",
        evidence_zip="data/evidence/example.zip",
        reporter_name="演示举报人",
        reporter_phone="13800000000",
    )
    steps = [
        Step(StepAction.GOTO, "打开本地模拟举报页面", value=MINI_FORM_URI),
        Step(StepAction.WAIT, "等待页面加载", value="0.05"),
        Step(StepAction.SCREENSHOT, "截图留证"),
    ]
    return SubmissionPlan(
        portal=Portal.P12377,
        entry_url=MINI_FORM_URI,
        payload=payload,
        steps=steps,
    )


def demo_executor_session(data_dir: Path) -> None:
    """3 个极小计划:会话复用 launch 1 次 vs 每计划独立会话 launch 3 次。"""
    from netsentinel.submit.executor_session import SessionExecutor

    _sec("④", "执行内核·会话复用(A129):一次 launch 跑完整批计划")

    plans = [_mini_plan(i) for i in range(3)]
    print("离线替身:FakeLauncher/FakeContext/FakePage(内存对象,绝不启动 chromium);")
    print(f"页面地址:本地夹具 {MINI_FORM_URI}")
    print("计划内容:goto → wait(0.05s)→ screenshot,无提交按钮、无人工门")
    print("(人工门 HUMAN_GATE 语义与旧执行器一字不差:真实模式一律 input() 等人工;")
    print(" 演示计划刻意不含该步骤,保证零阻塞、零真实提交)")
    print()

    cfg = Config(data_dir=str(data_dir), dry_run_default=False)

    # 新模式:一个 SessionExecutor 顺序跑 3 份计划,复用同一 context
    launcher_new = FakeLauncher()
    with SessionExecutor(cfg, launcher=launcher_new) as executor:
        results_new = [executor.run(plan, dry_run=False) for plan in plans]
    launches_new = launcher_new.browser.launch_count
    pages_new = sum(len(ctx.pages) for ctx in launcher_new.browser.contexts)

    # 旧口径:每计划独立会话(等价于逐计划调用 executor_playwright)→ 每计划一次 launch
    launcher_old = FakeLauncher()
    results_old: list[Any] = []
    for plan in plans:
        executor_old = SessionExecutor(cfg, launcher=launcher_old)
        results_old.append(executor_old.run(plan, dry_run=False))
        executor_old.close()
    launches_old = launcher_old.browser.launch_count
    pages_old = sum(len(ctx.pages) for ctx in launcher_old.browser.contexts)

    print(_ljust("模式", 28) + _rjust("计划数", 6) + _rjust("launch 次数", 12) + _rjust("new_page 次数", 14))
    print("-" * 64)
    print(_ljust("旧口径(每计划独立会话)", 28) + f"{len(plans):>6}{launches_old:>12}{pages_old:>14}")
    print(_ljust("会话复用(SessionExecutor)", 28) + f"{len(plans):>6}{launches_new:>12}{pages_new:>14}")
    print("-" * 64)
    print(f"launch 计数 {launches_new} vs 旧执行器 {launches_old}"
          f"(浏览器冷启动摊薄为每会话一次,页面仍逐计划独立、互不串扰)")
    print(f"执行结果:ok = {[r.ok for r in results_new]},"
          f"submitted = {[r.submitted for r in results_new]},"
          f"每计划截图 {[len(r.screenshots) for r in results_new]} 张")


# ---------------------------------------------------------------------------
# ⑤ 调度内核:优先级预算轮选
# ---------------------------------------------------------------------------


def demo_sched_kernel() -> None:
    """10 项候选、预算 4:三因子优先级 + 等成本背包轮选表。"""
    from netsentinel.ops.sched_kernel import (
        STALENESS_WINDOW_H,
        W_RISK,
        W_STALENESS,
        W_VOLATILITY,
        priority,
        select_round,
    )

    _sec("⑤", "调度内核·优先级预算(A130):预算花在最值得扫的目标上")

    print(f"三因子权重:波动度 {W_VOLATILITY} / URL 风险 {W_RISK} / "
          f"陈旧度 {W_STALENESS}(窗口 {STALENESS_WINDOW_H}h 封顶)")
    print()
    items = [
        {
            "url": f"site{i}.example",
            "volatility": (i % 10) / 10,
            "url_risk": ((9 - i) % 10) / 10,
            "staleness_h": i * 24,
        }
        for i in range(10)
    ]
    picked = select_round(items, 4.0)
    picked_ids = {id(item) for item in picked}

    print(_rjust("轮选序", 6) + " " + _ljust("目标", 18) + _rjust("波动度", 8) + _rjust("URL风险", 9)
          + _rjust("陈旧h", 8) + _rjust("优先级", 9) + _rjust("选中", 6))
    print("-" * 68)
    for rank, item in enumerate(sorted(items, key=priority, reverse=True), start=1):
        mark = "是" if id(item) in picked_ids else "－"
        print(
            f"{rank:>6} " + _ljust(item["url"], 18)
            + f"{item['volatility']:>8.1f}"
            + f"{item['url_risk']:>9.1f}"
            + f"{min(item['staleness_h'], STALENESS_WINDOW_H):>8.0f}"
            + f"{priority(item):>9.4f}"
            + _rjust(mark, 6)
        )
    print("-" * 68)
    print(f"预算 4.0 × 间隔 1.0 → 恰选 {len(picked)} 项:"
          f"{' > '.join(it['url'] for it in picked)}(优先级降序)")
    print("解读:纯函数零 IO,同输入恒同输出;由 cfg.sched_priority 开关接入,默认关")
    print("      时仍是旧 FIFO 行为,旧调用方零感知。")


# ---------------------------------------------------------------------------
# ⑥ 观测内核:Prometheus 导出片段
# ---------------------------------------------------------------------------


def demo_telemetry_export() -> None:
    """telemetry 打点 → render_metrics() 输出前 8 行。"""
    from netsentinel.telemetry_export import render_metrics

    _sec("⑥", "观测内核·指标导出(A137):render_metrics() 片段")

    telemetry.reset()  # 演示口径:清场后重新打点,输出短小可读
    telemetry.inc("demo.scan.sites", 3)
    telemetry.inc("demo.scan.pages", 12)
    telemetry.inc("demo.skin.classify", 8)
    telemetry.gauge("demo.queue.pending", 4)
    telemetry.observe("demo.scan.duration", 0.42)

    text = render_metrics()
    lines = text.splitlines()
    for line in lines[:8]:
        print(line)
    if len(lines) > 8:
        print(f"...(共 {len(lines)} 行,计数器→_total、仪表直出、计时器四序列)")
    print()
    print("解读:telemetry 只存名称与数字(红线 17),render_metrics() 把快照转成")
    print("      Prometheus exposition 文本;service 之外的 /metrics 挂载由运营者")
    print("      自由完成(零侵入,见 telemetry_export.render_metrics 文档字符串)。")


# ---------------------------------------------------------------------------
# ⑦ 融合内核:等权 vs 可靠性加权
# ---------------------------------------------------------------------------


def _twin_report(agg: float) -> SiteReport:
    """构造一份带成员分与 ensemble 汇总条的站点报告(同一分值集合的模板)。"""
    image = ImageEvidence(path="img_synthetic.png", url="u", source_page="s")
    member_scores = [
        ImageScore(image=image, model="glm:glm-5.3-flash", nsfw_prob=0.9),
        ImageScore(image=image, model="glm:glm-5.3-flash", nsfw_prob=0.8),
        ImageScore(image=image, model="stub", nsfw_prob=0.2),
        ImageScore(image=image, model="stub", nsfw_prob=0.1),
        ImageScore(image=image, model="ensemble", nsfw_prob=agg),
    ]
    return SiteReport(
        site_url="https://example.invalid/site",
        image_scores=member_scores,
        agg_nsw_prob=agg,
        nsw_image_count=0,
        verdict=Verdict.CLEAN,
    )


def demo_fusion_reliable() -> None:
    """同一分值集合:等权融合 vs 喂本地反馈后的可靠性加权融合。"""
    from netsentinel.decision.fusion_reliable import fuse_reliable
    from netsentinel.decision.reliability import ReliabilityTracker

    _sec("⑦", "融合内核·可靠性加权(A126):报得准的平台话语权大")

    cfg = Config()
    agg_equal = 0.5  # 等权口径:两成员均值 (0.85 + 0.15) / 2

    # 同一分值集合的双胞胎报告:A 走等权(旧行为),B 喂反馈后可靠性加权
    report_equal = _twin_report(agg_equal)
    report_reliable = _twin_report(agg_equal)

    tracker = ReliabilityTracker()  # 纯内存,零外呼(红线 30)
    for _ in range(6):  # MIN_N=5,喂 6 条本地复核反馈即达样本充足
        tracker.record("glm", 1.0, True)    # glm 报 1.0 且复核确认为色情 → Brier 0
        tracker.record("stub", 1.0, False)  # stub 报 1.0 但复核驳回 → Brier 1
    stats = tracker.stats()
    weights = tracker.weights()

    print("成员分值集合(两份报告完全相同):glm:glm-5.3-flash = [0.9, 0.8],stub = [0.2, 0.1]")
    print("本地反馈(内存,离线):各喂 6 条复核结论 →")
    print(f"  glm : n={stats['glm']['n']}, Brier={stats['glm']['brier']:.4f}, 权重={weights['glm']:.4f}")
    print(f"  stub: n={stats['stub']['n']}, Brier={stats['stub']['brier']:.4f}, 权重={weights['stub']:.4f}")
    print()

    fuse_reliable(report_equal, {}, {}, {}, cfg, tracker=None)      # 等权 = 旧行为
    fuse_reliable(report_reliable, {}, {}, {}, cfg, tracker=tracker)

    fusion_equal = report_equal.intel["fusion"]
    fusion_reliable = report_reliable.intel["fusion"]
    print(_ljust("口径", 24) + _rjust("成员权重(glm/stub)", 24) + _rjust("图像侧 agg", 12)
          + _rjust("融合概率", 10) + _rjust("判定", 8))
    print("-" * 80)
    mw_equal = fusion_equal["member_weights"]
    mw_reliable = fusion_reliable["member_weights"]
    mw_equal_text = f"{mw_equal.get('glm', 0):.2f} / {mw_equal.get('stub', 0):.2f}"
    mw_reliable_text = f"{mw_reliable.get('glm', 0):.2f} / {mw_reliable.get('stub', 0):.2f}"
    print(
        _ljust("等权(旧行为)", 24) + _rjust(mw_equal_text, 24)
        + f"{fusion_equal['agg_reliable']:>12.4f}"
        + f"{fusion_equal['prob']:>10.4f}"
        + _rjust(_VERDICT_CN[report_equal.verdict.value], 8)
    )
    print(
        _ljust("可靠性加权(喂反馈后)", 24) + _rjust(mw_reliable_text, 24)
        + f"{fusion_reliable['agg_reliable']:>12.4f}"
        + f"{fusion_reliable['prob']:>10.4f}"
        + _rjust(_VERDICT_CN[report_reliable.verdict.value], 8)
    )
    print("-" * 80)
    print(f"融合规则:{fusion_reliable['rule']}(intel['fusion'] 单列 agg_reliable /")
    print("          member_weights,agg_nsw_prob 等图像侧字段保持原值不动)")
    print("解读:stub 被复核驳回六次后话语权降到 0.05,同一分值集合下图像侧 agg 从")
    print(f"      {fusion_equal['agg_reliable']:.2f} 修正到 {fusion_reliable['agg_reliable']:.4f}"
          " —— 学习只来自运营者本地反馈,零外呼(开关 use_reliability_fusion 默认关)。")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    """中文分节演示七大 V7 内核;返回进程退出码(0 成功)。"""
    _ensure_utf8_stdio()

    missing = _check_sibling_modules()
    if missing:
        print("以下 V7 兄弟内核模块未就位,演练场无法开跑:", file=sys.stderr)
        for name in missing:
            print(f"  - {name}", file=sys.stderr)
        return 2

    print("净网哨兵 NetSentinel · V7 内核演练场(A140)")
    print("全部离线:合成数据 + 内存替身;零外呼、零真实提交。")

    with tempfile.TemporaryDirectory(prefix="netsentinel_demo_") as tmp:
        workdir = Path(tmp)
        demo_skin_classifier(workdir / "skin")          # ① 识别内核
        demo_sprt()                                     # ② 决策内核
        demo_phash_lsh()                                # ③ 检索内核
        demo_executor_session(workdir / "runs")         # ④ 执行内核
        demo_sched_kernel()                             # ⑤ 调度内核
        demo_telemetry_export()                         # ⑥ 观测内核
        demo_fusion_reliable()                          # ⑦ 融合内核

    print()
    print("=" * 74)
    print("内核进化演示完成:全部离线;开关默认关,生产按 config 启用"
          "(见 docs/KERNEL_EVOLUTION.md)")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
