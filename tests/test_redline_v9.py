# -*- coding: utf-8 -*-
"""V9 红线专项测试(NetSentinel · A181 · tests/test_redline_v9.py)。

依据 CONTRACTS-V9 §0 三条新红线(35/36/37),对已就位的 V9 模块做
**源码断言 + 行为断言**双保险;兄弟模块(A163-A175)并行开发中,
未就位者经 ``pytest.importorskip`` 跳过并在 reason 中标注所属红线组别。

三组专项(对应契约 §2 A181 行):

- **红线 35(压榨边界)**:高档并发只作用于本地计算与本地回环 IO;
  对外网络的礼貌间隔(``cfg.fetch_delay_s``)、引擎限速、举报频控
  **一概不放宽**;进程池仅传模块级纯函数的普通数据,不 pickle 实例;
- **红线 36(结案代理无自主提交权)**:SummaryAgent 源码不得出现
  ``run_batch`` / ``executor_playwright`` / ``plan_12377`` /
  ``auto_confirm``;SequentialReportAgent 源码不得出现
  ``auto_confirm=True``(允许 ``auto_confirm=False`` 或缺省不传),
  两者都不得有代写 attest 的路径;
- **红线 37(资源治理)**:任一执行器 workers ≤ cpu 核数;
  ``cpu_workers ≤ io_workers ≤ cores``;CPU 过载让位保护
  (:class:`LoadGuard`)在高利用率下真实睡眠让位(有界 0.05s、锁外
  可中断)。

全部用例离线、零网络、零真实 sleep(睡眠/时钟/执行器均可注入)。
"""
from __future__ import annotations

import ast
import inspect
import os
import re
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

from netsentinel.contracts import Config, ImageEvidence, ImageScore

# ---------------------------------------------------------------------------
# 通用小工具:源码/AST 提取
# ---------------------------------------------------------------------------
_CONCURRENCY_IDS: frozenset[str] = frozenset(
    {
        "concurrency_tier",
        "concurrency_auto",
        "io_workers",
        "cpu_workers",
        "cpu_reserve",
        "tier",
        "tier_workers",
        "tier_once",
        "thread_pool",
        "process_pool",
        "cpu_count",
    }
)

#: 对外礼貌/频控字段(红线 35:任何执行编排层不得改写)
_POLITENESS_FIELDS: frozenset[str] = frozenset(
    {
        "fetch_delay_s",
        "submit_min_interval_s",
        "submit_max_per_day",
        "batch_item_interval_s",
        "discovery_query_delay_s",
    }
)


def _module_source(module: object) -> str:
    """读模块源码全文(含 docstring;按 utf-8)。"""
    path = Path(inspect.getfile(module))  # type: ignore[arg-type]
    return path.read_text(encoding="utf-8")


def _names_in(node: ast.AST) -> set[str]:
    """收集 AST 子树里的全部裸标识符名(ast.Name)。"""
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _attrs_in(node: ast.AST) -> set[str]:
    """收集 AST 子树里的全部属性名(ast.Attribute.attr)。"""
    return {a.attr for a in ast.walk(node) if isinstance(a, ast.Attribute)}


# ===========================================================================
# 红线 35:压榨边界——档位/workers 不影响礼貌间隔与频控;进程池不传实例
# ===========================================================================
def test_redline35_pool_module_free_of_concurrency_and_politeness_ids():
    """ops/pool.py 整模块源码不含并发档位/礼貌频控标识符(A58 扫描池)。

    池的并发只来自 ``workers``(线程池规模 + 背压阈值),礼貌停顿只用
    模块级固定常量;模块不读取也不改写 ``cfg.fetch_delay_s`` 等任何
    礼貌/频控字段——这是红线 35「高档并发不放宽对外礼貌间隔」的
    源码级证明。
    """
    pool = pytest.importorskip(
        "netsentinel.ops.pool", reason="A58 ops.pool 未就位(红线 35 组)"
    )
    src = _module_source(pool)
    assert src.strip(), "红线 35 断言失效:ops/pool.py 源码为空,无法做源码断言"
    tree = ast.parse(src)
    ids = _names_in(tree) | _attrs_in(tree)
    leaked = sorted(ids & _CONCURRENCY_IDS)
    assert not leaked, (
        f"红线 35 违例:ops/pool.py 源码出现并发档位相关标识符 {leaked},"
        "扫描池的礼貌停顿/频控路径不得与 concurrency_tier/workers 档位耦合"
    )
    politeness = sorted(ids & set(_POLITENESS_FIELDS))
    assert not politeness, (
        f"红线 35 违例:ops/pool.py 源码触碰礼貌/频控字段 {politeness},"
        "扫描池无权读取或改写对外礼貌参数(它们由 crawler 链按 cfg 执行)"
    )
    # 礼貌常量必须是模块级纯数字字面量(不得由 cfg/档位推导),且未低于
    # A39 scheduler 同款基准(1.0s + 0~0.5s 抖动)——即"未被放宽"。
    module_consts = {
        t.id: stmt.value
        for stmt in tree.body
        if isinstance(stmt, ast.Assign)
        for t in stmt.targets
        if isinstance(t, ast.Name)
    }
    for name in ("PAUSE_BASE_S", "PAUSE_JITTER_S"):
        value_node = module_consts.get(name)
        assert isinstance(
            value_node, ast.Constant
        ), f"红线 35 违例:{name} 不是模块级常量字面量(疑似随 cfg/档位推导)"
        assert isinstance(value_node.value, (int, float)) and not isinstance(
            value_node.value, bool
        ), f"红线 35 违例:{name} 不是纯数字常量:{value_node.value!r}"
    assert pool.PAUSE_BASE_S >= 1.0, (
        f"红线 35 违例:礼貌基准间隔 PAUSE_BASE_S={pool.PAUSE_BASE_S} "
        "低于 1.0 秒基准,对外礼貌间隔被放宽"
    )
    assert 0.0 <= pool.PAUSE_JITTER_S <= 1.0, (
        f"红线 35 违例:礼貌抖动 PAUSE_JITTER_S={pool.PAUSE_JITTER_S} 越界"
    )


def test_redline35_pool_pause_path_no_concurrency_identifiers():
    """ops/pool.py 的礼貌 sleep 代码路径(pause 调用)不含任何并发标识符。

    每一处礼貌停顿必须且只能由固定常量 ``PAUSE_BASE_S``/``PAUSE_JITTER_S``
    与抖动源 ``jitter_fn`` 组成——``workers``/``tier``/``io_workers`` 等
    一概不得进入停顿时长表达式(即:提高并发绝不缩短礼貌间隔)。
    """
    pool = pytest.importorskip(
        "netsentinel.ops.pool", reason="A58 ops.pool 未就位(红线 35 组)"
    )
    tree = ast.parse(_module_source(pool))
    pause_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "pause"
    ]
    assert len(pause_calls) >= 2, (
        "红线 35 断言失效:ops/pool.py 中礼貌停顿 pause() 调用点不足 2 处"
        "(应含提交循环与背压停顿),源码断言未覆盖频控路径"
    )
    allowed = {"PAUSE_BASE_S", "PAUSE_JITTER_S", "jitter_fn"}
    banned_in_pause = _CONCURRENCY_IDS | {"workers", "n_workers", "max_workers"}
    for call in pause_calls:
        # 只检查停顿**参数表达式**(排除 pause 函数名自身)
        names: set[str] = set()
        for arg in call.args:
            names |= _names_in(arg)
        assert "PAUSE_BASE_S" in names and "PAUSE_JITTER_S" in names, (
            "红线 35 违例:礼貌停顿时长未锚定固定常量 PAUSE_BASE_S/"
            f"PAUSE_JITTER_S,实际表达式标识符:{sorted(names)}"
        )
        leaked = sorted(names & banned_in_pause)
        assert not leaked, (
            f"红线 35 违例:礼貌停顿表达式混入并发相关标识符 {leaked},"
            "停顿时长随 workers/档位变化即构成对外频控放宽"
        )
        extra = sorted(names - allowed)
        assert not extra, (
            f"红线 35 违例:礼貌停顿表达式出现非常量来源标识符 {extra},"
            "停顿时长只能由固定常量与抖动源组成"
        )


def test_redline35_pool_pause_identical_across_workers_and_tiers(tmp_path):
    """行为断言:workers 与 concurrency_tier 变化时,礼貌停顿序列逐毫秒不变。

    同步假执行器(future 即交即完)使背压判定确定化:每次运行恰产生
    ``len(targets)`` 次停顿;workers∈{1,2,4,8} × tier∈{low,mid,high}
    × 抖动∈{0.0,0.7} 全组合下,停顿值必须恒等于
    ``PAUSE_BASE_S + PAUSE_JITTER_S × jitter``。
    """
    pool = pytest.importorskip(
        "netsentinel.ops.pool", reason="A58 ops.pool 未就位(红线 35 组)"
    )

    class _SyncExecutor:
        """submit 即同步执行并返回已完成 Future(背压确定化为零积压)。"""

        def submit(self, fn, *args, **kwargs):
            fut: Future = Future()
            try:
                fut.set_result(fn(*args, **kwargs))
            except BaseException as exc:  # noqa: BLE001 - 与真实 Future 同语义
                fut.set_exception(exc)
            return fut

        def shutdown(self, wait=True):  # pragma: no cover - 注入执行器不会被关
            return None

    class _NoopMemory:
        def last_fingerprint(self, url):
            return ""

        def fingerprint(self, report):
            return ""

        def remember(self, url, fp):
            return None

    fake_report = SimpleNamespace(verdict="clean", needs_review=False, agg_nsw_prob=0.1)
    urls = [
        "https://a.example.com/",
        "https://b.example.com/",
        "https://c.example.com/",
    ]
    baseline: dict[float, list[float]] = {}
    for workers in (1, 2, 4, 8):
        for tier in ("low", "mid", "high"):
            for jitter in (0.0, 0.7):
                cfg = Config(data_dir=str(tmp_path), concurrency_tier=tier)
                pauses: list[float] = []
                summary = pool.run_pool(
                    cfg,
                    urls,
                    executor=_SyncExecutor(),
                    run_scan=lambda url, cfg_: fake_report,
                    memory=_NoopMemory(),
                    workers=workers,
                    sleep=pauses.append,
                    jitter=lambda: jitter,
                )
                assert summary["done"] == len(urls), (
                    f"红线 35 前置失败:workers={workers} tier={tier} 时扫描未全部完成,"
                    f"汇总={summary}(无法据此校验礼貌停顿)"
                )
                expected = [
                    pool.PAUSE_BASE_S + pool.PAUSE_JITTER_S * jitter
                ] * len(urls)
                assert pauses == expected, (
                    f"红线 35 违例:workers={workers} tier={tier} jitter={jitter} 时"
                    f"礼貌停顿序列 {pauses} ≠ 期望 {expected}——停顿随并发档位漂移,"
                    "对外礼貌间隔被放宽"
                )
                baseline.setdefault(jitter, expected)
                assert pauses == baseline[jitter], (
                    f"红线 35 违例:jitter={jitter} 下 workers={workers}/tier={tier} "
                    f"的停顿序列 {pauses} 与基准 {baseline[jitter]} 不一致"
                )


def test_redline35_crawler_politeness_sleep_binds_fetch_delay_s_only():
    """crawler 链的礼貌 sleep 直接绑定 ``cfg.fetch_delay_s``,与并发无关。

    fetcher / site_map / redirect 三处对外礼貌休眠(引擎限速同族)的
    sleep 表达式只允许引用 ``cfg.fetch_delay_s``(可包 max/float 防御),
    不得混入任何并发档位标识符,也不得对间隔做缩放乘除。
    """
    banned = _CONCURRENCY_IDS | {"workers", "max_workers"}
    total_sleeps = 0
    for mod_name in (
        "netsentinel.crawler.fetcher",
        "netsentinel.crawler.site_map",
        "netsentinel.crawler.redirect",
    ):
        module = pytest.importorskip(
            mod_name, reason=f"{mod_name} 未就位(红线 35 组:crawler 礼貌链)"
        )
        tree = ast.parse(_module_source(module))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "sleep"
            ):
                continue
            for arg in node.args:
                refs = _names_in(arg) | _attrs_in(arg)
                if "fetch_delay_s" not in refs:
                    continue  # 非对外礼貌 sleep(如测试辅助),不属本断言
                total_sleeps += 1
                leaked = sorted(refs & banned)
                assert not leaked, (
                    f"红线 35 违例:{mod_name} 的礼貌休眠 time.sleep 表达式"
                    f"混入并发标识符 {leaked}——抓取礼貌间隔不得随档位/workers 变化"
                )
                # 间隔不得被缩放(只允许 max/float 防御性包装)
                assert not isinstance(arg, ast.BinOp), (
                    f"红线 35 违例:{mod_name} 的礼貌休眠参数是运算表达式"
                    "(疑似对 fetch_delay_s 缩放宽限),只允许原值或防御性包装"
                )
    assert total_sleeps >= 3, (
        "红线 35 断言失效:crawler 三模块中绑定 cfg.fetch_delay_s 的礼貌"
        f"sleep 仅找到 {total_sleeps} 处(期望 ≥3:fetcher/site_map/redirect)"
    )


def test_redline35_finishflow_finish_forwards_cfg_verbatim(
    tmp_path, monkeypatch, capsys
):
    """finishflow.finish 把 cfg **原对象**原样转发给 scan/summary,绝不改写。

    注入 fake scan/summary 捕获入参:①捕获的 cfg 必须与传入对象同一
    (``is``);②跑完后 ``fetch_delay_s`` 与举报频控字段逐一未变;
    ③finishflow 源码不存在对任何礼貌/频控字段的赋值(含 --tier 覆盖,
    它经 dataclasses.replace 生成新对象,不改原 cfg)。
    """
    ff = pytest.importorskip(
        "netsentinel.finishflow", reason="A169 finishflow 未就位(红线 35 组)"
    )
    pytest.importorskip(
        "netsentinel.ops.concurrency",
        reason="A164 ops.concurrency 未就位(红线 35 组:finish 前置)",
    )
    tier_state = pytest.importorskip(
        "netsentinel.ops.tier_state",
        reason="A165 ops.tier_state 未就位(红线 35 组:finish 前置)",
    )
    # 隔离 A165 会话幂等标记:finish 前置的 tier_once 打桩为零副作用
    monkeypatch.setattr(tier_state, "tier_once", lambda cfg, **kw: {"action": "disabled"})

    politeness_snapshot = {
        "fetch_delay_s": 2.5,
        "submit_min_interval_s": 77,
        "submit_max_per_day": 3,
        "batch_item_interval_s": 95,
    }
    captured: dict[str, object] = {}

    def fake_scan(urls, cfg_, *, workers):
        captured["scan_urls"] = list(urls)
        captured["scan_cfg"] = cfg_
        captured["scan_workers"] = workers
        return {
            "reports": {},
            "summary": {"done": 0, "failed": 0, "skipped": 0, "errors": []},
        }

    class FakeSummary:
        def run(self, scan_result, cfg_):
            captured["summary_result"] = scan_result
            captured["summary_cfg"] = cfg_
            return {
                "groups": [],
                "attest_pending": [],
                "ready_count": 0,
                "tier": "mid",
                "workers": 2,
            }

    for tier in ("low", "mid", "high"):
        captured.clear()
        cfg = Config(
            data_dir=str(tmp_path),
            db_path=str(tmp_path / "q.db"),
            audit_path=str(tmp_path / "a.jsonl"),
            concurrency_tier=tier,
            **politeness_snapshot,
        )
        ff.finish(["https://a.example.com/"], cfg, scan=fake_scan, summary=FakeSummary())
        assert captured.get("scan_cfg") is cfg, (
            f"红线 35 违例:tier={tier} 时 finish 未把 cfg 原对象透传给 scan"
            f"(收到 {type(captured.get('scan_cfg')).__name__}),疑似复制/改写后转发"
        )
        assert captured.get("summary_cfg") is cfg, (
            f"红线 35 违例:tier={tier} 时 finish 传给 SummaryAgent 的 cfg "
            "不是原对象(礼貌参数可能在转发途中被改)"
        )
        for field, value in politeness_snapshot.items():
            assert getattr(cfg, field) == value, (
                f"红线 35 违例:tier={tier} 时 finish 改写了礼貌/频控字段 "
                f"{field}:{value} → {getattr(cfg, field)}"
            )
        assert isinstance(captured.get("scan_workers"), int) and captured[
            "scan_workers"
        ] >= 1, (
            "红线 35 前置失败:finish 未经 workers= 关键字把并发规模传给 scan,"
            f"捕获值:{captured.get('scan_workers')!r}"
        )
        assert captured.get("summary_result") == {
            "reports": {},
            "summary": {"done": 0, "failed": 0, "skipped": 0, "errors": []},
        }, "红线 35 断言失败:finish 未把 scan 结果原样转发给 SummaryAgent"

    # 源码级:finishflow 任何位置都不得给礼貌/频控字段赋值
    tree = ast.parse(_module_source(ff))
    offenders: list[str] = []
    for node in ast.walk(tree):
        targets: list[ast.AST]
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        else:
            continue
        for t in targets:
            if isinstance(t, ast.Attribute) and t.attr in _POLITENESS_FIELDS:
                offenders.append(t.attr)
    assert not offenders, (
        f"红线 35 违例:finishflow.py 源码存在对礼貌/频控字段的赋值 {sorted(set(offenders))},"
        "收官流程无权放宽对外礼貌间隔与举报频控"
    )


def test_redline35_parallel_classify_process_payload_pure_str_triple_source():
    """parallel_classify 进程模式源码断言:submit 负载是 (str,str,str) 元组。

    ``_classify_processes`` 只能把 ``(name, ev.path, ev.url)`` 纯字符串
    三元组提交给模块级纯函数 ``_proc_classify``;分类器实例/cfg/evidence
    对象绝不出现在 submit 参数里(红线 35:不 pickle 实例/连接)。
    """
    pc = pytest.importorskip(
        "netsentinel.vision.parallel_classify",
        reason="A166 vision/parallel_classify 未就位(红线 35 组)",
    )
    src = inspect.getsource(pc._classify_processes)
    tree = ast.parse(src)
    submits = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "submit"
    ]
    assert submits, (
        "红线 35 断言失效:_classify_processes 源码中找不到 pool.submit 调用,"
        "进程模式源码断言未覆盖提交路径"
    )
    for call in submits:
        assert call.args, "红线 35 违例:submit() 无参数(必须提交工作函数)"
        first = call.args[0]
        assert isinstance(first, ast.Name) and first.id == "_proc_classify", (
            "红线 35 违例:进程池 submit 的工作函数不是模块级纯函数 "
            f"_proc_classify(实际:{ast.dump(first)})"
        )
        for extra in call.args[1:]:
            assert isinstance(extra, ast.Name), (
                "红线 35 违例:submit 负载不是纯变量(payload),"
                f"出现内联表达式:{ast.dump(extra)}——疑似把实例对象塞进了进程池"
            )
            assert extra.id not in {"classifier", "ev", "evidences", "cfg"}, (
                f"红线 35 违例:submit 直接传递了标识符 {extra.id!r},"
                "分类器实例/证据对象/cfg 不得跨进程 pickle"
            )
        assert "classifier" not in _names_in(call) | _attrs_in(call), (
            "红线 35 违例:submit 参数引用了 classifier——进程池不得 pickle 分类器实例"
        )
    # payload 构造:恰为 (name, ev.path, ev.url) 三元组,元素全为字符串来源
    found_triple = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Tuple):
            continue
        for elt in node.elts:
            assert not (isinstance(elt, ast.Name) and elt.id == "classifier"), (
                "红线 35 违例:payload 元组里出现 classifier 标识符(实例被 pickle)"
            )
        if len(node.elts) == 3:
            first, second, third = node.elts
            if (
                isinstance(first, ast.Name)
                and first.id == "name"
                and isinstance(second, ast.Attribute)
                and second.attr == "path"
                and isinstance(second.value, ast.Name)
                and second.value.id == "ev"
                and isinstance(third, ast.Attribute)
                and third.attr == "url"
                and isinstance(third.value, ast.Name)
                and third.value.id == "ev"
            ):
                found_triple = True
    assert found_triple, (
        "红线 35 违例:_classify_processes 未按 (name, ev.path, ev.url) "
        "构造纯字符串三元组负载(红线 35:进程池只传普通数据)"
    )
    # 工作函数签名与模块级可 pickle 性
    assert pc._proc_classify.__qualname__ == "_proc_classify", (
        "红线 35 违例:_proc_classify 不是模块级顶层函数(嵌套函数无法被 "
        "spawn 子进程按名定位,且易携带闭包实例)"
    )
    annotation = str(inspect.signature(pc._proc_classify).parameters["payload"].annotation)
    assert "tuple" in annotation and annotation.count("str") >= 1, (
        f"红线 35 违例:_proc_classify 负载注解不是 (str, str, str) 三元组:{annotation}"
    )


def test_redline35_parallel_classify_process_submits_str_triples_behavior(
    tmp_path, monkeypatch
):
    """行为断言:进程模式实际提交的每条负载都是 (str,str,str),无实例对象。

    用记录型假 ProcessPoolExecutor 拦截 submit:负载必须为长度 3 的纯
    字符串元组;返回 dict 在主进程按位重组 ImageScore(image 复用原始
    evidence 对象,保序)。
    """
    pc = pytest.importorskip(
        "netsentinel.vision.parallel_classify",
        reason="A166 vision/parallel_classify 未就位(红线 35 组)",
    )

    recorded: list[tuple[object, object]] = []
    pool_sizes: list[int] = []

    class _FakeFuture:
        def __init__(self, value):
            self._value = value

        def result(self, timeout=None):
            return self._value

    class _FakeProcPool:
        def __init__(self, *, max_workers=None, mp_context=None):
            pool_sizes.append(int(max_workers or 0))

        def submit(self, fn, payload):
            recorded.append((fn, payload))
            name, path, url = payload
            return _FakeFuture(
                {
                    "model": name,
                    "nsfw_prob": 0.25,
                    "scores": {"stub": 0.25},
                    "path": path,
                    "url": url,
                }
            )

        def shutdown(self, wait=True, cancel_futures=False):
            return None

    monkeypatch.setattr(pc, "ProcessPoolExecutor", _FakeProcPool)
    evidences = [
        ImageEvidence(
            path=str(tmp_path / f"{i}.jpg"),
            url=f"https://x.test/{i}.jpg",
            source_page="https://x.test/",
        )
        for i in range(3)
    ]
    classifier = SimpleNamespace(name="stub")
    cfg = Config(classifier="stub", data_dir=str(tmp_path))
    scores = pc.classify_parallel(classifier, evidences, cfg, use_processes=True)

    assert recorded, "红线 35 断言失效:进程模式未提交任何任务(负载断言空跑)"
    cores = os.cpu_count() or 2
    for fn, payload in recorded:
        assert fn is pc._proc_classify, (
            f"红线 35 违例:进程池提交的工作函数是 {fn!r},不是模块级纯函数 _proc_classify"
        )
        assert isinstance(payload, tuple) and len(payload) == 3, (
            f"红线 35 违例:进程负载不是三元组:{payload!r}(类型 {type(payload).__name__})"
        )
        assert all(isinstance(item, str) for item in payload), (
            f"红线 35 违例:进程负载含非字符串元素(疑似 pickle 实例):{payload!r}"
        )
        assert not isinstance(payload, ImageEvidence), (
            "红线 35 违例:进程负载是 ImageEvidence 实例(证据对象被 pickle)"
        )
    assert [s.image for s in scores] == evidences, (
        "红线 35 违例:进程模式结果未保序/未复用原始 evidence 对象重组 ImageScore"
    )
    assert all(isinstance(s, ImageScore) for s in scores) and all(
        s.nsfw_prob == 0.25 for s in scores
    ), f"红线 35 前置失败:进程模式返回评分异常:{scores!r}"
    assert pool_sizes and all(1 <= n <= cores for n in pool_sizes), (
        f"红线 37/35 违例:进程池 max_workers={pool_sizes} 超出 [1, {cores}]"
    )


# ===========================================================================
# 红线 36:结案代理无自主提交权
# ===========================================================================
_SUMMARY_FORBIDDEN_TOKENS = ("run_batch", "executor_playwright", "plan_12377", "auto_confirm")
_ATTEST_WRITE_TOKENS = (
    "set_attested",
    "attest_group",
    "add_attestation",
    "mark_attested",
    "write_attest",
    "auto_attest",
)


def test_redline36_summary_agent_source_has_no_submit_paths():
    """summary_agent.py 源码不得出现任何提交路径标识(红线 36)。

    结案代理只做汇总与举报**准备**:不编排 run_batch、不驱动
    executor_playwright、不构造 plan_12377、不存在任何 auto_confirm。
    """
    sa = pytest.importorskip(
        "netsentinel.agent.summary_agent",
        reason="A168 agent/summary_agent 未就位(红线 36 组)",
    )
    src = _module_source(sa)
    assert src.strip(), "红线 36 断言失效:summary_agent.py 源码为空"
    found = [token for token in _SUMMARY_FORBIDDEN_TOKENS if token in src]
    assert not found, (
        f"红线 36 违例:summary_agent.py 源码出现提交路径标识 {found};"
        "结案代理无自主提交权——举报必须经 run_batch(逐条 HUMAN_GATE)"
        "之外的人工链路:batch_tui 逐组声明 → finishflow --report"
    )


def test_redline36_summary_agent_no_attest_write_paths():
    """summary_agent.py 不得有代写/绕过 attest 的路径(红线 36)。

    允许**只读**判定(如 ``is_attested`` 查询);任何写入/代答式
    声明调用都构成绕过人工声明门。
    """
    sa = pytest.importorskip(
        "netsentinel.agent.summary_agent",
        reason="A168 agent/summary_agent 未就位(红线 36 组)",
    )
    src = _module_source(sa)
    found = [token for token in _ATTEST_WRITE_TOKENS if token in src]
    assert not found, (
        f"红线 36 违例:summary_agent.py 源码出现代写声明标识 {found};"
        "组声明只能由运营者在 batch_tui 人工完成,结案代理不得代答 attest"
    )


def test_redline36_sequential_report_no_auto_confirm_true():
    """sequential_report.py 源码不得出现 auto_confirm=True(红线 36)。

    SequentialReportAgent 编排 run_batch 时必须恒 auto_confirm=False
    或缺省不传;允许 ``auto_confirm=False`` 字样存在。
    """
    mod = pytest.importorskip(
        "netsentinel.agent.sequential_report",
        reason="A170 agent/sequential_report 未就位(红线 36 组)",
    )
    src = _module_source(mod)
    assert src.strip(), "红线 36 断言失效:sequential_report.py 源码为空"
    offender = re.search(r"auto_confirm\s*=\s*True", src)
    assert offender is None, (
        "红线 36 违例:sequential_report.py 源码出现 auto_confirm=True"
        f"(上下文:{src[max(0, offender.start() - 40):offender.end() + 40]!r});"
        "顺序批量举报必须逐条 HUMAN_GATE,禁止任何自动确认"
    )


def test_redline36_sequential_report_no_attest_bypass():
    """sequential_report.py 不得代写/绕过 attest(红线 36)。"""
    mod = pytest.importorskip(
        "netsentinel.agent.sequential_report",
        reason="A170 agent/sequential_report 未就位(红线 36 组)",
    )
    src = _module_source(mod)
    found = [token for token in _ATTEST_WRITE_TOKENS if token in src]
    assert not found, (
        f"红线 36 违例:sequential_report.py 源码出现代写声明标识 {found};"
        "举报前置声明必须由人工完成,代理不得代答 attest"
    )


# ===========================================================================
# 红线 37:资源治理——workers ≤ cores;CPU 过载让位保护真实睡眠
# ===========================================================================
@pytest.mark.parametrize("cores", list(range(1, 34)), ids=lambda n: f"cores={n}")
def test_redline37_tier_workers_all_tiers_within_cores(cores):
    """cpu_profile.tier_workers:N=1..33 全档位换算恒满足 1 ≤ workers ≤ N。

    覆盖 low/mid/high × reserve∈{0,1,3}(reserve≥N 时 high 退化 1,
    绝不返回 0 或负数,也绝不越过物理核数)。
    """
    cp = pytest.importorskip(
        "netsentinel.ops.cpu_profile",
        reason="A163 ops/cpu_profile 未就位(红线 37 组)",
    )
    for tier in ("low", "mid", "high"):
        for reserve in (0, 1, 3):
            workers = cp.tier_workers(tier, reserve=reserve, cores=cores)
            assert isinstance(workers, int) and 1 <= workers <= cores, (
                f"红线 37 违例:tier={tier} reserve={reserve} cores={cores} "
                f"换算出 workers={workers},超出 [1, {cores}] 边界——"
                "任一执行器 workers 不得超过 cpu 核数"
            )


def test_redline37_cpu_workers_within_io_workers_within_cores(monkeypatch):
    """concurrency:cores=8 注入下,cpu_workers ≤ io_workers ≤ cores 全档成立。"""
    cp = pytest.importorskip(
        "netsentinel.ops.cpu_profile",
        reason="A163 ops/cpu_profile 未就位(红线 37 组)",
    )
    conc = pytest.importorskip(
        "netsentinel.ops.concurrency",
        reason="A164 ops/concurrency 未就位(红线 37 组)",
    )
    monkeypatch.setenv(cp.ENV_FAKE_CORES, "8")
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    for tier in ("low", "mid", "high"):
        for reserve in (0, 1, 3):
            cfg = Config(concurrency_tier=tier, cpu_reserve=reserve)
            io = conc.io_workers(cfg)
            cpu = conc.cpu_workers(cfg)
            assert 1 <= io <= 8, (
                f"红线 37 违例:tier={tier} reserve={reserve} 时 io_workers={io} "
                "超过注入核数 8(线程池规模越界)"
            )
            assert 1 <= cpu <= io, (
                f"红线 37 违例:tier={tier} reserve={reserve} 时 cpu_workers={cpu} "
                f"超过 io_workers={io}(进程池必须 ≤ 线程池规模)"
            )
            assert cpu <= 8, (
                f"红线 37 违例:tier={tier} reserve={reserve} 时 cpu_workers={cpu} "
                "超过物理核数 8(进程池永不超过核数)"
            )
    # 核数注入生效性自检(避免环境钩子失效导致上断言空转)
    assert conc.io_workers(Config(concurrency_tier="high", cpu_reserve=0)) == 8, (
        "红线 37 断言失效:cores=8 注入未生效(high/reserve=0 应恰为 8),"
        "上组边界断言不可信"
    )
    assert conc.cpu_workers(Config(concurrency_tier="high", cpu_reserve=0)) == 8
    assert conc.io_workers(Config(concurrency_tier="low")) == 2, (
        "红线 37 断言失效:cores=8 注入未生效(low 应为 8//4=2)"
    )


class _FakeClock:
    """可手拨的墙钟(单调递增由测试显式 advance 控制)。"""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _FakeTimes:
    """可手拨的进程 CPU 时间采样(返回 os.times 形态五元组)。"""

    def __init__(self):
        self.cpu = 0.0

    def __call__(self):
        return (self.cpu, 0.0, 0.0, 0.0, 0.0)

    def burn(self, seconds):
        self.cpu += seconds


def test_redline37_load_guard_high_utilization_yields_sleep():
    """load_guard:注入 100% 进程 CPU 利用率 → maybe_yield 触发 sleep(计数>0)。"""
    lg = pytest.importorskip(
        "netsentinel.ops.load_guard",
        reason="A167 ops/load_guard 未就位(红线 37 组)",
    )
    clock = _FakeClock()
    times = _FakeTimes()
    sleeps: list[float] = []
    guard = lg.LoadGuard(
        interval_s=0.5,
        times_fn=times,
        clock_fn=clock,
        sleep_fn=sleeps.append,
    )
    # 首调建基线:样本不足,不让位、不睡眠
    assert guard.maybe_yield() is False and not sleeps, (
        "红线 37 前置失败:首采样(仅建基线)不应触发让位睡眠"
    )
    # 墙钟走 1s、进程烧掉 1s CPU → 利用率 1.0 > 0.92 → 必须让位睡眠
    clock.advance(1.0)
    times.burn(1.0)
    assert guard.maybe_yield() is True, (
        "红线 37 违例:进程 CPU 利用率 100% 时 maybe_yield 未让位"
        f"(last_utilization={guard.last_utilization()})"
    )
    assert len(sleeps) > 0 and sleeps[-1] == lg.YIELD_SLEEP_S, (
        f"红线 37 违例:过载让位未执行 sleep(0.05),记录:{sleeps}"
    )
    # interval 内重复判定复用缓存样本:连续过载循环每次都让位
    clock.advance(1.0)
    times.burn(1.0)
    assert guard.maybe_yield() is True
    assert len(sleeps) >= 2 and all(d == lg.YIELD_SLEEP_S for d in sleeps), (
        f"红线 37 违例:持续过载下让位睡眠异常:{sleeps}(应每次恰 0.05s)"
    )
    assert guard.last_utilization() == pytest.approx(1.0)


def test_redline37_load_guard_low_or_equal_utilization_no_sleep():
    """load_guard 负向对照:低负载与恰好等于阈值都不让位(不误伤)。"""
    lg = pytest.importorskip(
        "netsentinel.ops.load_guard",
        reason="A167 ops/load_guard 未就位(红线 37 组)",
    )
    clock = _FakeClock()
    times = _FakeTimes()
    sleeps: list[float] = []
    guard = lg.LoadGuard(
        interval_s=0.5,
        times_fn=times,
        clock_fn=clock,
        sleep_fn=sleeps.append,
    )
    guard.maybe_yield()  # 基线
    # 低负载:1s 墙钟仅烧 0.1s CPU → 10%,不让位
    clock.advance(1.0)
    times.burn(0.1)
    assert guard.maybe_yield() is False, (
        f"红线 37 误报:利用率 10% 不应让位(util={guard.last_utilization()})"
    )
    # 恰好等于阈值 0.92:严格大于才让位
    clock.advance(1.0)
    times.burn(0.92)
    assert guard.maybe_yield() is False, (
        "红线 37 误报:利用率恰等于阈值 0.92 时不应让位(须严格大于)"
    )
    assert sleeps == [], f"红线 37 误报:低载/临界负载发生了让位睡眠:{sleeps}"


def test_redline37_load_guard_yield_bounded_outside_lock():
    """load_guard 让位睡眠有界(恒 0.05s)且发生在锁外(可中断,不阻塞他线程)。"""
    lg = pytest.importorskip(
        "netsentinel.ops.load_guard",
        reason="A167 ops/load_guard 未就位(红线 37 组)",
    )
    assert lg.YIELD_SLEEP_S == 0.05, (
        f"红线 37 违例:让位睡眠时长 YIELD_SLEEP_S={lg.YIELD_SLEEP_S},"
        "偏离有界可中断约定 0.05 秒"
    )
    src = inspect.getsource(lg.LoadGuard.maybe_yield)
    assert "YIELD_SLEEP_S" in src, (
        "红线 37 违例:maybe_yield 未用固定有界常量 YIELD_SLEEP_S 睡眠"
    )
    assert "self._lock" not in src, (
        "红线 37 违例:maybe_yield 源码出现 self._lock——让位睡眠必须发生在"
        "锁外,否则过载让位会阻塞其他线程的判定与让位(不可中断)"
    )
    clock = _FakeClock()
    times = _FakeTimes()
    sleeps: list[float] = []
    guard = lg.LoadGuard(
        interval_s=0.5, times_fn=times, clock_fn=clock, sleep_fn=sleeps.append
    )
    guard.maybe_yield()
    clock.advance(2.0)
    times.burn(2.5)  # 多线程叠加 >1.0,同样按过载处理
    assert guard.maybe_yield() is True and sleeps == [0.05], (
        f"红线 37 违例:利用率 1.25(多核叠加)未按过载让位:{sleeps}"
    )
