# -*- coding: utf-8 -*-
"""时间源单源化守门(V10.21 席位2,对应 netsentinel/timesource.py)。

四个对账段(仿 doctest 收编 / contracts_sync 的显式登记制):

1. **函数语义**:utc_now / now_local / now_iso / now_naive_local /
   fromtimestamp_local 的 tzinfo 语义与输出格式(ISO 秒级带本机偏移等);
2. **等价性硬证(冻结时钟)**:把 ``timesource._dt`` 替换为固定时钟代理,
   断言"同一时刻"下新式与旧惯用式的输出**逐字符相同**——覆盖目录名
   strftime(packager/merge_bundles)、显示时间戳 strftime(report 双件
   /packager generated_at)、run-id strftime(finishflow)、ISO ts
   (telemetry/cost_meter/finishflow meta)、fromtimestamp 显示
   (modelmgr)五类迁移形态;另有真实时钟活体对照(容忍秒边界重试)
   证明非冻结环境下两式同样一致;
3. **迁移收编登记(正向)**:_MIGRATED 登记本席位已迁移的源文件,断言
   每个文件(a)确实 import timesource,(b)不再含裸 ``datetime.now(``
   调用(增删迁移点要同步改登记表);
4. **AST 反向扫描守门**:扫描 netsentinel/ 全部 .py,断言 aware 惯用式
   ``datetime.now(timezone.utc).astimezone()``(任意 import 别名写法)
   只许出现在 timesource.py 本体与显式豁免/登记白名单内:

   - **冻结豁免**:contracts.py(全队规范文件,仅负责人可改);
   - **他席领地**:storage/kernel.py、intel/graph.py、
     intel/site_memory.py(V10.21 席位3 协同:**已完成迁移**,登记自
     _EXEMPT_OTHER_SEAT 移入 _MIGRATED_FILES,白名单已清空);
   - **现存未收编登记**:**已清零**(V10.24 创世纪遗留清偿波 · 席位3
     收编了最后三处 holdout:telemetry_trace.py、notify/hub.py、
     vision/vlm_cache.py,登记移入 _MIGRATED_FILES,白名单置空仅保留
     结构供后续登记复用);
   - **naive 契约永久豁免**:submit/rate_limit.py 与 ops/adaptive.py
     是自洽 naive 本地时间契约(其 ``astimezone()`` 是入参归一,非
     ``now()`` 惯用式,本就不触发 aware 守门,在此注记语义)。

   任何**未登记**文件新出现该惯用式 → 测试失败(收编回归);登记文件
   清零该式 → 测试提醒删登记(增删都要改本文件,漂移无处藏身)。

全部测试离线、零网络;冻结时钟经 monkeypatch 作用域隔离,测试后自动还原。
"""
from __future__ import annotations

import ast
import datetime as _dt
import re
import types
from pathlib import Path

import pytest

from netsentinel import timesource

_ROOT = Path(__file__).resolve().parents[1]
_PKG = _ROOT / "netsentinel"
_TIMESOURCE_PY = _PKG / "timesource.py"

# ---------------------------------------------------------------------------
# 冻结时钟:同一时刻两式对照的硬证设施
# ---------------------------------------------------------------------------

#: 冻结的 UTC 时刻(微秒位非零,验证 timespec="seconds" 的截断口径一致)。
_FROZEN_UTC = _dt.datetime(2026, 10, 13, 4, 5, 6, 789012, tzinfo=_dt.timezone.utc)


class _FrozenDatetime(_dt.datetime):
    """固定时钟的 datetime 替身:now() 恒返回 :data:`_FROZEN_UTC`。

    带 tz 参数 → aware UTC 冻结值(对应 ``datetime.now(timezone.utc)``);
    无参 → 冻结值的本地 naive 墙钟(对应 ``datetime.now()``)。其余类方法
    (fromtimestamp 等)继承原语义(确定性输入,本就无竞态)。
    """

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        if tz is not None:
            return _FROZEN_UTC
        return _FROZEN_UTC.astimezone().replace(tzinfo=None)


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> _dt.datetime:
    """把 timesource 内部的 ``_dt`` 绑定替换为冻结代理,返回冻结时刻。

    只改 ``netsentinel.timesource`` 模块命名空间里的 ``_dt`` 名字绑定,
    绝不动标准库 datetime 模块本身(零全局影响,monkeypatch 自动还原)。
    """
    proxy = types.SimpleNamespace(datetime=_FrozenDatetime, timezone=_dt.timezone)
    monkeypatch.setattr(timesource, "_dt", proxy)
    return _FROZEN_UTC


# ---------------------------------------------------------------------------
# ① 函数语义
# ---------------------------------------------------------------------------


def test_utc_now_is_aware_utc() -> None:
    """utc_now:tzinfo 恰为 timezone.utc,且与标准库同一路径取值。"""
    got = timesource.utc_now()
    assert got.tzinfo is _dt.timezone.utc
    assert abs((got - _dt.datetime.now(_dt.timezone.utc)).total_seconds()) < 5


def test_now_local_is_aware_local() -> None:
    """now_local:aware 且时区等于本机(与 utc_now 同瞬间墙钟一致)。"""
    got = timesource.now_local()
    assert got.tzinfo is not None
    assert got.utcoffset() == _dt.datetime.now().astimezone().utcoffset()
    assert abs((got - timesource.utc_now()).total_seconds()) < 5


def test_now_iso_format_seconds() -> None:
    """now_iso 缺省:ISO8601 秒级、带本机时区偏移。"""
    text = timesource.now_iso()
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}", text
    ), f"now_iso 输出非秒级带偏移格式:{text!r}"
    parsed = _dt.datetime.fromisoformat(text)  # 可回解为 aware 时刻
    assert parsed.tzinfo is not None


def test_now_iso_timespec_variants() -> None:
    """now_iso(timespec=...):微秒/分钟变体格式生效且可回解。"""
    micro = timesource.now_iso(timespec="microseconds")
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}[+-]\d{2}:\d{2}", micro
    ), f"microseconds 变体格式不符:{micro!r}"
    minutes = timesource.now_iso(timespec="minutes")
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+-]\d{2}:\d{2}", minutes
    ), f"minutes 变体格式不符:{minutes!r}"


def test_now_naive_local_is_naive_wallclock() -> None:
    """now_naive_local:无 tzinfo,墙钟与本地时刻一致(documenting 用)。"""
    got = timesource.now_naive_local()
    assert got.tzinfo is None
    assert abs(got.timestamp() - _dt.datetime.now().timestamp()) < 5


def test_fromtimestamp_local_matches_stdlib() -> None:
    """fromtimestamp_local:与标准库 fromtimestamp 逐字符等价(naive 本地)。"""
    ts = 1759500000.0
    got = timesource.fromtimestamp_local(ts)
    want = _dt.datetime.fromtimestamp(ts)
    assert got == want and got.tzinfo is None and type(got) is type(want)


# ---------------------------------------------------------------------------
# ② 等价性硬证:冻结时钟下"同一时刻两式输出逐字符相同"
# ---------------------------------------------------------------------------


def test_now_iso_byte_equal_to_legacy_idiom(frozen_clock: _dt.datetime) -> None:
    """ISO ts 迁移点(telemetry.snapshot / cost_meter._now_iso /
    finishflow meta.generated_at):新式与旧 aware 惯用式逐字符相同。"""
    legacy = (
        _FROZEN_UTC.astimezone().isoformat(timespec="seconds")
    )  # == datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    assert timesource.now_iso() == legacy
    assert timesource.now_iso(timespec="microseconds") == (
        _FROZEN_UTC.astimezone().isoformat(timespec="microseconds")
    )


def test_dir_stamp_byte_equal_naive_and_aware(frozen_clock: _dt.datetime) -> None:
    """目录名 strftime 迁移点(packager:430 / merge_bundles:193 /
    executor_session:191 / executor_playwright:474 / browser:220):
    新式(aware).strftime == 旧 naive ``datetime.now().strftime``
    == 旧 aware ``now().astimezone().strftime`` 三方逐字符相同。"""
    fmt = "%Y%m%d_%H%M%S"
    new = timesource.now_local().strftime(fmt)
    legacy_naive = _FROZEN_UTC.astimezone().replace(tzinfo=None).strftime(fmt)
    legacy_aware = _FROZEN_UTC.astimezone().strftime(fmt)
    assert new == legacy_naive == legacy_aware


def test_display_stamp_byte_equal(frozen_clock: _dt.datetime) -> None:
    """显示时间戳 strftime 迁移点(packager:458 generated_at /
    batch_report:270 / run_summary:255 ``_TS_FORMAT``):三方逐字符相同。"""
    fmt = "%Y-%m-%d %H:%M:%S"
    new = timesource.now_local().strftime(fmt)
    legacy_naive = _FROZEN_UTC.astimezone().replace(tzinfo=None).strftime(fmt)
    legacy_aware = _FROZEN_UTC.astimezone().strftime(fmt)
    assert new == legacy_naive == legacy_aware


def test_run_id_stamp_byte_equal(frozen_clock: _dt.datetime) -> None:
    """finishflow._default_run_id:旧式是 ``now().astimezone().strftime``,
    新式 now_local().strftime 逐字符相同(含 "run-" 前缀整体对照)。"""
    fmt = "%Y%m%d-%H%M%S"
    legacy = "run-" + _FROZEN_UTC.astimezone().strftime(fmt)
    assert "run-" + timesource.now_local().strftime(fmt) == legacy


def test_fromtimestamp_display_byte_equal() -> None:
    """modelmgr._fmt_time 的 fromtimestamp 显示式:确定性输入逐字符相同
    (无需冻结:fromtimestamp 输入相同输出必相同)。"""
    ts = 1759500000.0
    assert timesource.fromtimestamp_local(ts).strftime("%Y-%m-%d %H:%M:%S") == (
        _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    )


def test_live_clock_agreement_with_legacy() -> None:
    """真实时钟活体对照:now_iso 与旧惯用式(及 contracts.now_iso)
    同秒逐字符相同;两式间隔微秒级,重试上限内必命中同一秒。"""
    from netsentinel.contracts import now_iso as contracts_now_iso

    for _ in range(50):
        got = timesource.now_iso()
        legacy = (
            _dt.datetime.now(_dt.timezone.utc)
            .astimezone()
            .isoformat(timespec="seconds")
        )
        frozen_contract = contracts_now_iso()
        if got == legacy == frozen_contract:
            return
        # 秒边界翻转(两式跨秒各取一侧)→ 重试
    pytest.fail(
        "真实时钟下 50 次重试仍未出现两式同秒一致:"
        f"timesource={got!r} legacy={legacy!r} contracts={frozen_contract!r}"
    )


# ---------------------------------------------------------------------------
# ③ 迁移收编登记(正向):已迁移文件必须 import timesource 且无裸 now
# ---------------------------------------------------------------------------

#: 已迁移的源文件(相对 netsentinel/ 的 posix 路径)。
#: 增删迁移点必须同步本登记表——收编留痕,防回退。
#: (V10.21 席位3 协同:storage/intel 三文件由席位3 按移交完成迁移,
#:  自 _EXEMPT_OTHER_SEAT 他席领地白名单移入本表;
#:  V10.24 创世纪遗留清偿波 · 席位3:收编最后三处 aware 惯用式
#:  holdout——telemetry_trace / notify/hub / vision/vlm_cache,
#:  自 _REGISTERED_HOLDOUTS 移入本表,holdout 登记清零。)
_MIGRATED_FILES: tuple[str, ...] = (
    "evidence/merge_bundles.py",
    "evidence/packager.py",
    "report/batch_report.py",
    "report/run_summary.py",
    "crawler/browser.py",
    "submit/executor_session.py",
    "submit/executor_playwright.py",
    "finishflow.py",
    "modelmgr.py",
    "telemetry.py",
    "vision/cost_meter.py",
    "storage/kernel.py",
    "intel/graph.py",
    "intel/site_memory.py",
    "telemetry_trace.py",
    "notify/hub.py",
    "vision/vlm_cache.py",
)

#: datetime 模块的常见本地别名(裸 now 检测只认这些根名;
#: ``timesource.now_*`` 的根名是 timesource,天然不触发)。
_DATETIME_ROOTS = frozenset({"datetime", "_dt"})


def _root_name(expr: ast.expr) -> str | None:
    """沿属性链下取最左根名(``_dt.datetime.now`` → ``_dt``;Name → 自身)。"""
    while isinstance(expr, ast.Attribute):
        expr = expr.value
    return expr.id if isinstance(expr, ast.Name) else None


def _find_bare_now_calls(tree: ast.Module) -> list[int]:
    """AST 定位裸 ``datetime.now(...)`` 调用行号(import 别名无关)。

    只认真实调用表达式:``datetime.now(`` / ``_dt.datetime.now(``;
    docstring、注释、``timesource.now_local()`` 均不触发。
    """
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "now":
            root = _root_name(func)
            if root in _DATETIME_ROOTS:
                hits.append(node.lineno)
    return hits


def _iter_pkg_files() -> list[Path]:
    """netsentinel/ 包内全部 .py(存在性哨卫:包失踪即显式失败)。"""
    if not _PKG.is_dir():
        pytest.fail(f"未找到 {_PKG}:包目录失踪,守门拒绝静默")
    return sorted(_PKG.rglob("*.py"))


def _ast_of(path: Path) -> ast.Module:
    """读入并解析 .py;语法损坏显式失败(守门要求代码可解析)。"""
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:  # pragma: no cover - 防御:源码语法损坏
        pytest.fail(f"{path} 语法解析失败:{exc}(守门要求可解析)")


def test_migrated_files_import_timesource() -> None:
    """每个登记的迁移文件都真实 import 了 timesource。"""
    for rel in _MIGRATED_FILES:
        path = _PKG / rel
        assert path.is_file(), f"迁移登记指向不存在的文件:{rel}(登记表需复核)"
        tree = _ast_of(path)
        imported = any(
            (
                isinstance(node, ast.ImportFrom)
                and node.module == "netsentinel"
                and any(a.name == "timesource" for a in node.names)
            )
            or (
                isinstance(node, ast.Import)
                and any(a.name == "netsentinel.timesource" for a in node.names)
            )
            for node in ast.walk(tree)
        )
        assert imported, f"{rel} 已登记迁移但未 import timesource(回退或漏迁)"


def test_migrated_files_have_no_bare_datetime_now() -> None:
    """迁移文件里不再有裸 ``datetime.now(...)`` 调用(时间取值全走
    timesource);AST 检测,docstring/注释里的文字提及不误报。"""
    offenders: list[str] = []
    for rel in _MIGRATED_FILES:
        path = _PKG / rel
        if not path.is_file():
            continue  # 存在性已由 import 断言把守
        for lineno in _find_bare_now_calls(_ast_of(path)):
            offenders.append(f"{rel}:{lineno}")
    assert not offenders, (
        "已迁移文件残留裸 datetime.now 调用(应改走 netsentinel.timesource):"
        f"{offenders}"
    )


def test_timesource_zero_internal_dependencies() -> None:
    """L0 基座约束:timesource.py 零内部依赖(只许标准库 import)。"""
    tree = _ast_of(_TIMESOURCE_PY)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("netsentinel"), (
                    f"timesource 违反 L0 基座约束:import {alias.name}"
                )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            level_prefix = "." * (node.level or 0)
            assert not (mod.startswith("netsentinel") or node.level), (
                f"timesource 违反 L0 基座约束:from {level_prefix}{mod} import"
            )


# ---------------------------------------------------------------------------
# ④ AST 反向扫描守门:aware 惯用式只许在 timesource + 白名单
# ---------------------------------------------------------------------------

#: 冻结豁免(全队规范文件,仅负责人可改;语义上与 timesource.now_iso 等价)。
_EXEMPT_FROZEN = ("contracts.py",)

#: 他席领地(V10.21 席位3 接收 storage/intel 的时间源迁移;**席位3 已完成
#: 迁移并移入 _MIGRATED_FILES 正向登记,本白名单清空**,仅保留结构供后续
#: 席位协同复用——"席位3 协同"注记)。
_EXEMPT_OTHER_SEAT: tuple[str, ...] = ()

#: 现存未收编登记(非本席位领地的 aware 惯用式持有文件,后续席位收口;
#: 收编完成后应从此移除——增删都要改本文件)。
#: V10.24 创世纪遗留清偿波 · 席位3:telemetry_trace.py / notify/hub.py /
#: vision/vlm_cache.py 三处 holdout 已收编(移入 _MIGRATED_FILES),
#: 本登记**清零**,仅保留结构供后续可能的登记复用。
_REGISTERED_HOLDOUTS: tuple[str, ...] = ()

#: aware 惯用式合法出现文件全集(timesource.py 本体 + 各豁免/登记)。
_AWARE_IDIOM_ALLOWLIST = frozenset(
    {"timesource.py"}
    | set(_EXEMPT_FROZEN)
    | set(_EXEMPT_OTHER_SEAT)
    | set(_REGISTERED_HOLDOUTS)
)


def _find_aware_now_astimezone(tree: ast.Module) -> list[int]:
    """AST 定位 ``<...>.now(timezone.utc).astimezone()`` 惯用式行号。

    匹配形态(与 import 别名无关):外层调用 ``X.astimezone(...)``,其接收
    者是内层调用 ``Y.now(Z)``,且 Z 是 ``*.utc`` 属性(timezone.utc)。
    只认 now 带参 ``utc`` 的 aware 变体;naive ``now().astimezone()`` 链
    与入参归一式的 ``dt.astimezone()`` 不触发(见模块 docstring 豁免注记)。
    """
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "astimezone"):
            continue
        inner = func.value
        if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)):
            continue
        if inner.func.attr != "now":
            continue
        has_utc_arg = any(
            isinstance(a, ast.Attribute) and a.attr == "utc" for a in inner.args
        )
        if has_utc_arg:
            hits.append(node.lineno)
    return hits


def test_aware_idiom_confined_to_allowlist() -> None:
    """反向扫描:netsentinel/ 全部 .py 中 aware 惯用式只许出现在白名单。"""
    offenders: list[str] = []
    for path in _iter_pkg_files():
        rel = path.relative_to(_PKG).as_posix()
        hits = _find_aware_now_astimezone(_ast_of(path))
        if hits and rel not in _AWARE_IDIOM_ALLOWLIST:
            offenders.append(f"{rel}:{hits}")
    assert not offenders, (
        "未登记文件出现 aware 惯用式 datetime.now(timezone.utc).astimezone():"
        f"{offenders};新代码必须改走 netsentinel.timesource(now_local/"
        "now_iso);确属豁免场景请在本测试白名单显式登记并注明理由"
    )


def test_allowlist_entries_still_hold_the_idiom() -> None:
    """双向对账:白名单条目(除 timesource 本体)必须仍持有该惯用式。

    条目清零该式后不删登记 → 本测试失败,提醒把文件移出白名单
    (登记表与代码现状双向不许漂移;timesource.py 本体是实现处,恒豁免)。
    """
    stale: list[str] = []
    for rel in sorted(_AWARE_IDIOM_ALLOWLIST - {"timesource.py"}):
        path = _PKG / rel
        if not path.is_file():
            stale.append(f"{rel}(文件已不存在)")
            continue
        if not _find_aware_now_astimezone(_ast_of(path)):
            stale.append(f"{rel}(该文件已无 aware 惯用式,应从白名单移除)")
    assert not stale, (
        f"白名单出现失效条目:{stale};请同步增删 _EXEMPT_*/"
        "_REGISTERED_HOLDOUTS 登记(增删都要改本测试,仿 doctest 收编模式)"
    )
