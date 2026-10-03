"""净网哨兵(NetSentinel)命令行入口(NetSentinel · A18)。

子命令::

    python -m netsentinel --config config.yaml scan --url http://localhost/x
    python -m netsentinel queue list --status pending     # 参数转交 review_queue
    python -m netsentinel queue approve 1 --note "已人工核实"
    python -m netsentinel submit --id 1 --portal 12377 --dry-run
    python -m netsentinel submit --id 1 --portal shdf --exec

安全红线:
- ``submit`` 的 ``auto_confirm`` 永远为 False(不存在 --yes 之类的旁路参数),
  人工确认门(HUMAN_GATE)由执行器在真实执行时交互落实;
- ``--exec`` 才真正驱动浏览器,默认跟随 ``cfg.dry_run_default``(安全默认 True);
- 启动即 ``load_config`` + ``setup_logging``;所有配置外的兄弟模块惰性导入。

V5:子命令分发接入 ``netsentinel.telemetry``(cli.scan / cli.queue /
cli.submit 计时与计数,失败计 cli.errors;只记名称与数字)。
"""
from __future__ import annotations

import argparse
import importlib
import logging
import sys
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config, ExecutionResult, Portal, SiteReport

__all__ = ["main"]

logger = logging.getLogger(__name__)

_VERDICT_CN: dict[str, str] = {
    "clean": "无风险",
    "suspect": "疑似",
    "nsfw": "高置信色情",
}

_PORTAL_CN: dict[str, str] = {
    "12377": "中央网信办举报中心(12377)",
    "shdf": "全国扫黄打非办公室(shdf)",
}


def _load(module_name: str) -> Any:
    """惰性导入兄弟模块;缺失时抛中文 RuntimeError。"""
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(f"模块 {module_name} 未就位:{exc}") from exc


# ---------------------------------------------------------------------------
# 默认实现工厂(模块级,便于测试 monkeypatch 稳定替换)
# ---------------------------------------------------------------------------
def _default_run_scan(url: str, cfg: Config) -> SiteReport:
    """执行 scan 主流程(惰性导入编排器)。"""
    orchestrator = _load("netsentinel.pipeline.orchestrator")
    return orchestrator.run_scan(url, cfg)


def _default_run_submit(
    entry_id: int,
    portal: Portal,
    cfg: Config,
    *,
    auto_confirm: bool = False,
    dry_run: bool | None = None,
) -> ExecutionResult:
    """执行 submit 主流程(惰性导入编排器)。"""
    orchestrator = _load("netsentinel.pipeline.orchestrator")
    return orchestrator.run_submit(
        entry_id, portal, cfg, auto_confirm=auto_confirm, dry_run=dry_run
    )


def _default_queue(db_path: str) -> Any:
    """打开人工复核队列。"""
    review_queue = _load("netsentinel.decision.review_queue")
    return review_queue.ReviewQueue(db_path)


def _default_queue_main(argv: list[str]) -> int:
    """转发到复核队列 CLI(list/show/approve/reject)。"""
    review_queue = _load("netsentinel.decision.review_queue")
    return int(review_queue.main(argv))


def _find_entry_info(cfg: Config, report: SiteReport) -> tuple[int | None, str]:
    """在复核队列里查找该站点最近一次入列的条目号与证据包路径。

    用于 scan 摘要展示;needs_review=False 时返回 ``(None, "")``。
    查询失败只告警,不影响 scan 结果。
    """
    if not report.needs_review:
        return (None, "")
    try:
        queue = _default_queue(cfg.db_path)
        for entry in reversed(list(queue.list())):
            if str(getattr(entry, "site_url", "")) == report.site_url:
                return (int(entry.id), str(getattr(entry, "evidence_zip", "") or ""))
    except Exception as exc:  # noqa: BLE001 - 摘要查询失败不阻断主流程
        logger.warning("查询复核队列失败(忽略):%s", exc)
    return (None, "")


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    """构造 CLI 解析器:--config/--verbose 全局可用,子命令 scan/queue/submit。"""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        default="config.yaml",
        help="配置文件路径(默认:./config.yaml,文件可缺省)",
    )
    common.add_argument("--verbose", action="store_true", help="输出 DEBUG 级别日志")

    # 子命令级同名参数:用 SUPPRESS 避免未传参时覆盖顶层已解析的值。
    quiet = argparse.ArgumentParser(add_help=False)
    quiet.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    quiet.add_argument(
        "--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )

    parser = argparse.ArgumentParser(
        prog="python -m netsentinel",
        description="净网哨兵(NetSentinel)—— 疑似色情站点抽样识别、人工复核与举报辅助",
        parents=[common],
    )
    sub = parser.add_subparsers(
        dest="command", required=True, metavar="{scan,queue,submit}"
    )

    p_scan = sub.add_parser(
        "scan", help="扫描站点:抽样 + 图像识别 + 判定,需复核的自动入队", parents=[quiet]
    )
    p_scan.add_argument("--url", required=True, help="目标站点起始 URL")

    sub.add_parser(
        "queue",
        help="人工复核队列(剩余参数转交 review_queue:list/show/approve/reject)",
    )

    p_submit = sub.add_parser(
        "submit", help="提交已人工确认(approved)的条目到举报门户", parents=[quiet]
    )
    p_submit.add_argument(
        "--id", dest="entry_id", type=int, required=True, help="复核队列条目编号"
    )
    p_submit.add_argument(
        "--portal",
        required=True,
        choices=["12377", "shdf"],
        help="举报门户:12377(中央网信办)或 shdf(扫黄打非)",
    )
    dry = p_submit.add_mutually_exclusive_group()
    dry.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_const",
        const=True,
        help="只生成步骤记录,不驱动浏览器(覆盖配置默认)",
    )
    dry.add_argument(
        "--exec",
        dest="dry_run",
        action="store_const",
        const=False,
        help="真正驱动浏览器执行(人工门仍生效,需人工输入验证码)",
    )
    p_submit.set_defaults(dry_run=None)  # 缺省跟随 cfg.dry_run_default
    return parser


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------
def _cmd_scan(args: argparse.Namespace, cfg: Config, extras: list[str]) -> int:
    """scan:执行扫描并打印中文摘要。"""
    if extras:
        print(
            f"错误:scan 无法识别参数:{' '.join(extras)}(--config 请放在子命令之前)",
            file=sys.stderr,
        )
        return 2
    report = _default_run_scan(args.url, cfg)
    entry_id, zip_path = _find_entry_info(cfg, report)
    verdict_cn = _VERDICT_CN.get(report.verdict.value, report.verdict.value)

    print(f"扫描完成:{report.site_url}")
    print(
        f"抽样规模:{len(report.pages)} 页,参与评分图片 {len(report.image_scores)} 张"
    )
    review_tag = "[需人工复核]" if report.needs_review else "[无需复核]"
    print(f"判定:{verdict_cn}({report.verdict.value}){review_tag}")
    print(f"聚合分(最高单图集成分):{report.agg_nsw_prob:.4f}")
    print(f"达标图片数:{report.nsw_image_count}")
    if report.needs_review:
        print(f"证据包:{zip_path or '-'}")
        print(
            f"复核队列编号:{entry_id if entry_id is not None else '-'}"
            "(下一步:queue approve 人工确认后再 submit)"
        )
    else:
        print("证据包:-(判定无需复核,未打包)")
        print("复核队列编号:-(未入列)")
    return 0


def _cmd_queue(cfg: Config, extras: list[str]) -> int:
    """queue:剩余参数加 --db 后转交复核队列 CLI。"""
    forwarded = ["--db", cfg.db_path] + list(extras)
    logger.debug("转发复核队列 CLI:%s", forwarded)
    try:
        return _default_queue_main(forwarded)
    except SystemExit as exc:  # review_queue 的 argparse 错误以退出码形式抛出
        code = exc.code
        return code if isinstance(code, int) and code else (1 if code else 0)


def _cmd_submit(args: argparse.Namespace, cfg: Config, extras: list[str]) -> int:
    """submit:提交已人工确认的条目(auto_confirm 恒为 False,人工门交给执行器)。"""
    if extras:
        print(
            f"错误:submit 无法识别参数:{' '.join(extras)}"
            "(不存在自动确认参数;--config 请放在子命令之前)",
            file=sys.stderr,
        )
        return 2
    portal = Portal(args.portal)
    # 安全红线:auto_confirm 永远 False,真实执行时由执行器交互等待人工确认。
    result = _default_run_submit(
        args.entry_id, portal, cfg, auto_confirm=False, dry_run=args.dry_run
    )
    portal_cn = _PORTAL_CN.get(portal.value, portal.value)
    print(f"举报门户:{portal_cn}")
    print(f"条目编号:{args.entry_id}")
    ok_cn = "成功" if result.ok else "失败"
    submitted_cn = "是" if result.submitted else "否"
    print(f"执行结果:{ok_cn};是否已真实提交:{submitted_cn}")
    if result.stopped_at:
        print(f"中止步骤:{result.stopped_at}")
    for note in result.notes:
        print(f"  - {note}")
    return 0 if result.ok else 1


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """CLI 主入口:返回进程退出码(0 成功;1 运行错误;2 用法错误)。"""

    parser = _build_parser()
    try:
        args, extras = parser.parse_known_args(argv)
    except SystemExit as exc:  # --help / 用法错误
        code = exc.code
        return code if isinstance(code, int) and code else (0 if not code else 1)

    try:
        config_mod = _load("netsentinel.config")
        logging_util = _load("netsentinel.logging_util")
    except RuntimeError as exc:
        print(f"错误:{exc}", file=sys.stderr)
        return 1

    try:
        cfg = config_mod.load_config(args.config)
    except (ValueError, OSError) as exc:
        print(f"错误:加载配置失败({args.config}):{exc}", file=sys.stderr)
        return 1
    logging_util.setup_logging(cfg, verbose=bool(getattr(args, "verbose", False)))
    # V8:安装后自动接管本地视觉模型(无模型时弹连接向导);失败只告警不阻断。
    try:
        from netsentinel.pipeline.takeover import takeover_once

        takeover_result = takeover_once(cfg)
        if takeover_result.get("action") in ("local", "cloud", "wizard"):
            print(f"[接管] {takeover_result.get('action')}: {takeover_result.get('detail', '')[:80]}")
    except Exception as exc:  # noqa: BLE001 - 接管是增强项
        print(f"[接管] 跳过:{exc}")

    try:
        if args.command == "scan":
            with telemetry.timer("cli.scan"):
                telemetry.inc("cli.scan")
                return _cmd_scan(args, cfg, extras)
        if args.command == "queue":
            with telemetry.timer("cli.queue"):
                telemetry.inc("cli.queue")
                return _cmd_queue(cfg, extras)
        if args.command == "submit":
            with telemetry.timer("cli.submit"):
                telemetry.inc("cli.submit")
                return _cmd_submit(args, cfg, extras)
    except (RuntimeError, ValueError) as exc:
        telemetry.inc("cli.errors")
        print(f"错误:{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI 兜底:任何异常都以中文提示收口
        telemetry.inc("cli.errors")
        logger.error("执行 %s 子命令时出现未预期异常", args.command, exc_info=True)
        print(
            f"错误:执行过程中出现未预期的异常({type(exc).__name__}):{exc}",
            file=sys.stderr,
        )
        return 1

    print(f"错误:未知子命令:{args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
