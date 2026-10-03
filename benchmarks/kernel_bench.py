# -*- coding: utf-8 -*-
"""NetSentinel 内核基准总控(A138)—— 统一收割全部 v7 内核自检。

依据 CONTRACTS-V7.md §2 A138 条目:V7 各新内核(A123–A137)在各自模块中
提供 ``kernel_selfcheck() -> {"name", "metric", "value", "baseline"}``
(离线、确定性、操作计数口径,红线 31),本总控负责:

- :func:`collect`:按注册表**惰性导入**各内核模块并逐个调用其
  ``kernel_selfcheck``;统一归一为四键 ``{name, metric, value, baseline}``
  (缺键容错补 ``None``);模块缺席(导入失败或无 ``kernel_selfcheck``)→
  ``{"name": …, "status": "not_ready"}`` 并注明原因,不让单个未就位内核
  中断整批收割;
- :func:`run`:全量收集 → 逐行判定 → 产出中文对比报告
  ``kernel_report.md``(内核 / 指标 / 值 / 基线 / 判定)+ ``kernel_report.json``,
  返回汇总 ``{"total", "passed", "failed", "not_ready"}``;
- CLI(:func:`main`,``--out`` 指定输出目录):任何 ``not_ready`` 或未通过
  内核 → 退出码 2;全部就位且通过 → 0。

A224(CONTRACTS-V13 §2 工程清理)扩展:

- 注册表收入 A212 ``netsentinel.ops.aimd`` 的 ``kernel_selfcheck`` 与
  :data:`KERNEL_EXTRA_CHECKS` 登记的 reliability
  ``bayesian_kernel_selfcheck``(同模块附加自检,行序就近插在其标准行后;
  模块/属性缺席同样 not_ready 跳过并注明);
- 报告(md / json / CLI 汇总)标注 ``HAS_NUMPY``(:func:`_probe_has_numpy`
  只读探测 ``netsentinel.mathx.HAS_NUMPY``,numpy 在席与否只影响 mathx
  加速路径,数值口径双后端一致)。

判定统一约定(方向歧义的消解):各内核的"value 优于 baseline"方向不一致
(有的越大越好、有的越小越好、有的须相等,如 sprt 早停张数 < 基线、
reliability 权重 > 等权、mathx 乘加计数 == 理论值),故总控**不猜测方向**:

1. 自检返回若含 ``"pass_": bool`` 字段 → 直接采用其真值;
2. 否则 ``value`` 有值(非 ``None``)即判通过 —— 各内核自检内部已用
   ``assert`` 锁定自身口径(触发断言即抛异常,归入未通过),能正常返回
   value 即代表该内核自证成立。

安全红线(红线 30 / 31):全程离线、零外呼、零真实提交;不构造任何
墙钟断言;每个自检均为确定性微基准,两次 ``collect()`` 输出逐行一致。
调用自检期间临时重定向 stdout / stderr(个别内核经 logging 打印政策回退
提示等噪声),保证 CLI 汇总输出干净,异常照常向上传递。

命令行::

    python benchmarks/kernel_bench.py --out benchmarks/out
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

# ---------------------------------------------------------------------------
# 直接以脚本运行(python benchmarks/kernel_bench.py)时,保证项目根在
# sys.path 上,使 netsentinel 各内核可导入;经包导入(tests)时为空操作。
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

__all__ = [
    "KERNEL_MODULES",
    "KERNEL_EXTRA_CHECKS",
    "KernelBenchError",
    "collect",
    "render_markdown",
    "run",
    "main",
]

#: 四键统一模式(缺键容错补 None;判定用 ``pass_`` 为保留字段)。
_ROW_KEYS: tuple[str, ...] = ("name", "metric", "value", "baseline")

#: 标准自检属性名(V7 起各内核约定入口)。
_STANDARD_CHECK = "kernel_selfcheck"

#: 内核注册表:模块路径 → 兜底显示名(自检返回自带 ``name`` 时优先用返回值)。
#: 顺序即报告行序。
#:
#: - executor_session(A129)与 cache2(A132)的 ``kernel_selfcheck`` 已由
#:   负责人于 2026-10-02 补齐,现均就位;
#: - A224(CONTRACTS-V13 §2 工程清理)新增 ``netsentinel.ops.aimd``
#:   (A212 AIMD 调速内核自检)。
KERNEL_MODULES: dict[str, str] = {
    "netsentinel.vision.heuristic_kernel": "heuristic_kernel(skin)",
    "netsentinel.intel.text_kernel": "text_kernel",
    "netsentinel.decision.sprt": "sprt",
    "netsentinel.decision.reliability": "reliability",
    "netsentinel.intel.phash_lsh": "phash_lsh",
    "netsentinel.intel.graph_kernel": "graph_kernel",
    "netsentinel.submit.executor_session": "executor_session",
    "netsentinel.ops.sched_kernel": "sched_kernel",
    "netsentinel.ops.aimd": "aimd",
    "netsentinel.storage.kernel": "storage.kernel",
    "netsentinel.vision.cache2": "cache2",
    "netsentinel.mathx": "mathx",
    "netsentinel.vision.phash2": "phash2",
    "netsentinel.submit.style_kernel": "style_kernel",
    "netsentinel.security.threat_kernel": "threat_kernel",
    "netsentinel.telemetry_export": "telemetry_export",
}

#: 附加自检注册表(A224):模块路径 → ``(自检属性名, 兜底显示名)``。
#:
#: 同一模块提供**多个**自检入口时在此登记(标准 ``kernel_selfcheck``
#: 之外的补充口径),报告行紧随该模块的标准行之后;模块缺席(导入失败
#: 或缺该属性)同样按 ``not_ready`` 跳过并注明,不中断整批收割。
#: 当前登记:reliability 的 V12 贝叶斯路径(``bayesian_kernel_selfcheck``,
#: 指数遗忘漂移消解自检)。
KERNEL_EXTRA_CHECKS: dict[str, tuple[str, str]] = {
    "netsentinel.decision.reliability": ("bayesian_kernel_selfcheck", "reliability.bayes"),
}


class KernelBenchError(RuntimeError):
    """基准总控流程中可预期的错误(中文消息;CLI 捕获后以退出码 2 结束)。"""


# ---------------------------------------------------------------------------
# 收集:惰性导入 + 逐内核自检 + 四键归一
# ---------------------------------------------------------------------------


def _judge(result: Mapping[str, Any]) -> bool:
    """统一判定:优先 ``pass_`` 布尔;否则 ``value`` 有值即通过(见模块 docstring)。"""
    if "pass_" in result:
        return bool(result["pass_"])
    return result.get("value") is not None


def _registry_entries(
    registry: Mapping[str, str],
) -> list[tuple[str, str, str]]:
    """把注册表展开为 ``(模块路径, 自检属性名, 兜底显示名)`` 三元组列表。

    每个模块先出标准 :data:`_STANDARD_CHECK` 条目;若该模块在
    :data:`KERNEL_EXTRA_CHECKS` 登记了附加自检(如 reliability 的贝叶斯
    路径),附加条目**紧随其后**——报告行序 = 注册表序,附加行就近插在
    所属模块的标准行之后。
    """
    entries: list[tuple[str, str, str]] = []
    for module_path, fallback_name in registry.items():
        entries.append((module_path, _STANDARD_CHECK, fallback_name))
        extra = KERNEL_EXTRA_CHECKS.get(module_path)
        if extra is not None:
            attr, extra_fallback = extra
            entries.append((module_path, attr, extra_fallback))
    return entries


def collect(modules: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """逐内核惰性调用自检(``kernel_selfcheck`` 及登记的附加自检),返回统一行列表。

    - ``modules``:参与内核的模块路径序列;缺省用 :data:`KERNEL_MODULES`
      全表(测试可注入 fake 模块路径做接入验证;注入路径经
      :data:`KERNEL_EXTRA_CHECKS` 登记后同样收割其附加自检);
    - 每行统一含四键 ``{name, metric, value, baseline}``(自检缺键补
      ``None``)+ ``status``(``"pass"`` / ``"fail"``)与 ``module``;
      自检返回的额外键(如 threat_kernel 的 ``total_ran``)收入 ``extra``
      供 JSON 报告留档;
    - 模块导入失败或未提供对应自检属性 → 该行仅含
      ``{"name", "module", "status": "not_ready", "reason"}``,跳过不中断
      (A224:reliability 的贝叶斯附加自检缺席时同样走此路径并注明属性名);
    - 自检调用抛异常 → 判 ``"fail"`` 并记录中文原因(区别于缺席);
    - 同一模块的多个自检共享一次导入(模块只 import 一遍)。

    调用期间临时捕获 stdout / stderr(个别内核的 logging 噪声不污染总控
    输出);两次调用输出逐行一致(各内核自检均为确定性微基准)。
    """
    registry: Mapping[str, str] = (
        KERNEL_MODULES if modules is None else {m: m.rsplit(".", 1)[-1] for m in modules}
    )
    rows: list[dict[str, Any]] = []
    imported: dict[str, Any] = {}
    for module_path, attr, fallback_name in _registry_entries(registry):
        module = imported.get(module_path)
        if module is None:
            try:
                module = importlib.import_module(module_path)
            except Exception as exc:  # noqa: BLE001 - 守卫宽:任何导入失败按未就位处理
                rows.append(
                    {
                        "name": fallback_name,
                        "module": module_path,
                        "status": "not_ready",
                        "reason": f"模块导入失败:{type(exc).__name__}: {exc}",
                    }
                )
                continue
            imported[module_path] = module

        check = getattr(module, attr, None)
        if not callable(check):
            rows.append(
                {
                    "name": fallback_name,
                    "module": module_path,
                    "status": "not_ready",
                    "reason": f"未提供 {attr}(缺席)",
                }
            )
            continue

        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                raw = check()
        except Exception as exc:  # noqa: BLE001 - 自检抛异常 = 未通过,不中断整批
            rows.append(
                {
                    "name": fallback_name,
                    "module": module_path,
                    "metric": None,
                    "value": None,
                    "baseline": None,
                    "status": "fail",
                    "reason": f"{attr} 抛异常:{type(exc).__name__}: {exc}",
                }
            )
            continue

        if not isinstance(raw, Mapping):
            rows.append(
                {
                    "name": fallback_name,
                    "module": module_path,
                    "metric": None,
                    "value": None,
                    "baseline": None,
                    "status": "fail",
                    "reason": f"{attr} 返回非映射:{type(raw).__name__}",
                }
            )
            continue

        result = dict(raw)
        row: dict[str, Any] = {
            "name": str(result.get("name") or fallback_name),
            "metric": result.get("metric"),
            "value": result.get("value"),
            "baseline": result.get("baseline"),
            "module": module_path,
            "status": "pass" if _judge(result) else "fail",
        }
        extra = {
            key: val
            for key, val in result.items()
            if key not in _ROW_KEYS and key != "pass_"
        }
        if extra:
            row["extra"] = extra
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------


def _fmt_cell(value: Any) -> str:
    """表格单元格格式化:``None`` → ``—``,其余原样字符串化。"""
    if value is None:
        return "—"
    if isinstance(value, float):
        # 整值浮点保留一位小数(比率 1.0 / 0.0),其余用 %g 紧凑表示。
        return f"{value:.1f}" if value.is_integer() else f"{value:g}"
    return str(value)


_VERDICT_TEXT: dict[str, str] = {"pass": "通过", "fail": "未通过", "not_ready": "未就位"}


def _probe_has_numpy() -> bool | None:
    """只读探测 ``netsentinel.mathx.HAS_NUMPY``(numpy 可选后端是否在席)。

    惰性 import(A199 mathx 模块级探测一次,此处只读不复算);缺席 /
    探测失败返回 ``None``(报告标注"未知")——增强性标注绝不中断基准
    主流程。数值口径不受该后端选择影响(mathx 双后端一致性锁定)。
    """
    try:
        from netsentinel import mathx  # 只读:HAS_NUMPY 由 A199 模块自持
    except Exception:  # noqa: BLE001 - 探测失败按未知处理,不中断
        return None
    value = getattr(mathx, "HAS_NUMPY", None)
    return value if isinstance(value, bool) else None


def _has_numpy_text(has_numpy: Any) -> str:
    """HAS_NUMPY 标注文案:True/False/未知 三态(中文,自解释)。"""
    if has_numpy is True:
        return "True(numpy 向量化加速在席)"
    if has_numpy is False:
        return "False(纯 stdlib,行为与 A133 原版一致)"
    return "未知(mathx 探测失败)"


def render_markdown(payload: Mapping[str, Any]) -> str:
    """把基准 payload 渲染为中文 Markdown 报告(单遍拼接,无外部依赖)。"""
    summary = payload["summary"]
    lines: list[str] = []
    lines.append("# NetSentinel 内核基准总控报告(A138)")
    lines.append("")
    lines.append(f"- 生成时间:{payload['generated_at']}")
    lines.append(
        f"- 汇总:总计 {summary['total']} 个内核 · 通过 {summary['passed']} · "
        f"未通过 {summary['failed']} · 未就位 {summary['not_ready']}"
    )
    lines.append(f"- 数值后端 HAS_NUMPY:{_has_numpy_text(payload.get('has_numpy'))}")
    lines.append(
        "- 判定约定:自检返回含 ``pass_`` 布尔则采用之;否则 value 有值即通过"
        "(各内核方向语义不一:有的大为优、有的小为优、有的须相等,总控不猜方向;"
        "口径由各内核自检内部断言锁定)"
    )
    lines.append("")
    lines.append("## 一、逐内核对比")
    lines.append("")
    lines.append("| 内核 | 指标 | 值 | 基线 | 判定 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for row in payload["kernels"]:
        lines.append(
            "| {} | {} | {} | {} | {} |".format(
                _fmt_cell(row.get("name")),
                _fmt_cell(row.get("metric")),
                _fmt_cell(row.get("value")),
                _fmt_cell(row.get("baseline")),
                _VERDICT_TEXT.get(row.get("status", ""), str(row.get("status"))),
            )
        )
    lines.append("")

    flagged = [r for r in payload["kernels"] if r.get("status") != "pass"]
    if flagged:
        lines.append("## 二、未通过 / 未就位说明")
        lines.append("")
        for row in flagged:
            lines.append(f"- **{row.get('name')}**:{row.get('reason', '未通过判定(见上表)')}")
        lines.append("")

    lines.append("## 三、口径说明")
    lines.append("")
    lines.append(
        "- 全部自检为离线确定性微基准(操作计数 / 精确结果断言,红线 31),"
        "两次运行输出逐行一致,不依赖墙钟;"
    )
    lines.append(
        "- 未就位(executor_session / cache2 等)指该内核模块尚未提供 "
        "``kernel_selfcheck``,总控显式标注而非静默跳过;"
    )
    lines.append(
        "- 详细数值口径见各内核模块 ``kernel_selfcheck`` docstring 与 "
        "docs/KERNEL_EVOLUTION.md。"
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run(out_dir: str | Path = "benchmarks/out") -> dict[str, int]:
    """全量收集 → 判定 → 写报告,返回汇总 ``{"total", "passed", "failed", "not_ready"}``。

    产出 ``out_dir/kernel_report.md``(中文对比表)与 ``out_dir/kernel_report.json``
    (同 payload,含各内核行与 extra 留档);目录不存在则创建。A224 起
    payload 附 ``has_numpy``(mathx 探测结果,只读),两份报告均标注。
    """
    rows = collect()
    summary = {
        "total": len(rows),
        "passed": sum(1 for r in rows if r["status"] == "pass"),
        "failed": sum(1 for r in rows if r["status"] == "fail"),
        "not_ready": sum(1 for r in rows if r["status"] == "not_ready"),
    }
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "benchmark": "kernel_bench",
        "summary": summary,
        "has_numpy": _probe_has_numpy(),
        "kernels": rows,
    }

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "kernel_report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out / "kernel_report.md").write_text(render_markdown(payload), encoding="utf-8")
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _ensure_utf8_stdio() -> None:
    """Windows 控制台编码非 UTF-8 时切换标准流编码,避免中文输出报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if (
                stream is not None
                and stream.encoding
                and stream.encoding.lower() not in ("utf-8", "utf8")
            ):
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 重新配置失败不影响主流程
            pass


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    返回码:0 全部就位且通过;2 存在未通过 / 未就位内核,或流程可预期错误
    (中文提示到 stderr)。
    """
    _ensure_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="python benchmarks/kernel_bench.py",
        description=(
            "NetSentinel 内核基准总控(A138):统一调用全部 v7 内核自检,"
            "产出 kernel_report.md / kernel_report.json(全程离线)"
        ),
    )
    parser.add_argument(
        "--out", default="benchmarks/out", help="报告输出目录(默认 benchmarks/out)"
    )
    args = parser.parse_args(argv)

    try:
        summary = run(args.out)
    except (KernelBenchError, OSError) as exc:
        print(f"错误:{exc}", file=sys.stderr)
        return 2

    print(
        "内核基准总控:{} 个内核 → 通过 {} · 未通过 {} · 未就位 {}".format(
            summary["total"], summary["passed"], summary["failed"], summary["not_ready"]
        )
    )
    print(f"数值后端 HAS_NUMPY:{_has_numpy_text(_probe_has_numpy())}")
    out_dir = Path(args.out)
    print(f"报告已写出:{out_dir / 'kernel_report.md'} 与 {out_dir / 'kernel_report.json'}")
    if summary["failed"] or summary["not_ready"]:
        print("结论:存在未通过 / 未就位内核 —— 退出码 2")
        return 2
    print("结论:全部内核就位且通过 —— 退出码 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
