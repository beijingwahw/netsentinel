"""批量预审(V10.1)—— 一次确认,逐条执行;每条唯一人工动作 = 浏览器输入验证码 + 回车。

红线不变:验证码永远由人在浏览器中输入;本模块只是把"逐条复核确认"合并为
"一次总确认",执行时每条仍停在 HUMAN_GATE 等待浏览器验证码完成。

用法::

    from netsentinel.submit.batch_precheck import BatchPrecheck

    pc = BatchPrecheck(cfg)
    result = pc.run(portal="12377", dry_run=False)
"""
from __future__ import annotations

import logging
import types
from typing import Any, Callable

from netsentinel import telemetry
from netsentinel.contracts import Config

__all__ = ["BatchPrecheck", "preview_table"]

logger = logging.getLogger(__name__)


def preview_table(items: list[dict]) -> str:
    """中文预览表:序号/组名/站点/判定/理由(≤39 字截断)/证据包。"""
    if not items:
        return "(无待批量条目)"
    header = f"{'#':<4} {'组名':<22} {'站点':<30} {'判定':<8} {'理由(前39字)':<40}"
    sep = "-" * len(header)
    lines = [header, sep]
    for i, it in enumerate(items, 1):
        site = str(it.get("site_url", ""))[:28]
        group = str(it.get("group_name", ""))[:20]
        verdict = str(it.get("verdict", ""))[:6]
        reason = str(it.get("reason", ""))[:39]
        lines.append(f"{i:<4} {group:<22} {site:<30} {verdict:<8} {reason:<40}")
    lines.append(sep)
    lines.append(f"共 {len(items)} 条;每条执行时仍须在浏览器输入验证码(红线不变)")
    return "\n".join(lines)


class BatchPrecheck:
    """批量预审:合并确认为一次,逐条执行(半自动焦点+浏览器验证码)。"""

    def __init__(self, cfg: Config, *, ready_fn=None, agent=None, stdin=None, stdout=None):
        self.cfg = cfg
        self._ready_fn = ready_fn
        self._agent = agent
        self._stdin = stdin
        self._stdout = stdout

    def _print(self, text: str) -> None:
        if self._stdout:
            print(text, file=self._stdout)
        else:
            print(text)

    def _input(self, prompt: str) -> str:
        import sys as _sys

        dest = self._stdout or _sys.stdout
        print(prompt, file=dest)
        if self._stdin is not None:
            return self._stdin.readline().strip()
        return input()

    def collect_ready(self) -> list[dict]:
        """收集就绪条目(approved + attested);附加自动理由。"""
        if self._ready_fn is not None:
            items = self._ready_fn(self.cfg)
        else:
            from netsentinel.decision.batch_review import ready_entries

            items = ready_entries(self.cfg)
        # 附加自动理由
        try:
            from netsentinel.submit.reason_gen import generate_reason

            for it in items:
                if not it.get("reason"):
                    it["reason"] = generate_reason(it, self.cfg, use_glm=False)
        except Exception as exc:  # noqa: BLE001 - 理由生成不阻断
            logger.debug("理由生成失败(忽略):%s", exc)
            for it in items:
                it.setdefault("reason", "")
        return items

    def run(
        self,
        *,
        portal: str = "12377",
        dry_run: bool | None = None,
        on_item: Callable[[int, int, dict], None] | None = None,
    ) -> dict:
        """预审 → 确认 → 依次举报(每条浏览器验证码 + 回车)。"""
        items = self.collect_ready()
        if not items:
            self._print("无待批量条目(需先 approve + attest)")
            return {"submitted": 0, "failed": 0, "skipped": True}

        # 预审表
        self._print("\n" + "=" * 66)
        self._print("批量预审(一次确认,逐条执行)")
        self._print("=" * 66)
        self._print(preview_table(items))
        self._print("")

        # 一次总确认
        answer = self._input(
            f"确认对以上 {len(items)} 条依次执行举报?\n"
            "每条执行流程:浏览器自动打开→自动填写→光标聚焦验证码框→"
            "你输入验证码→回终端按回车→自动点提交。\n"
            "输入 Y 确认(其他取消): "
        )
        if answer.strip().upper() != "Y":
            self._print("已取消,未执行任何举报。")
            telemetry.inc("batch_precheck.cancelled")
            return {"submitted": 0, "failed": 0, "cancelled": True}

        telemetry.inc("batch_precheck.confirmed", len(items))

        # 依次执行
        from netsentinel.agent.sequential_report import SequentialReportAgent

        agent = self._agent or SequentialReportAgent(self.cfg)
        dr = dry_run if dry_run is not None else self.cfg.dry_run_default
        self._print(f"\n开始依次执行 {len(items)} 条(dry_run={dr})...\n")

        result = agent.run(items, self.cfg, dry_run=dr, on_item=on_item)
        batch_result = result.get("result", {})
        submitted = int(batch_result.get("submitted", 0))
        failed = int(batch_result.get("failed", 0))

        self._print(f"\n批量完成:提交 {submitted} 条 / 失败 {failed} 条")
        if result.get("report_path"):
            self._print(f"结案报告:{result['report_path']}")
        telemetry.inc("batch_precheck.submitted", submitted)
        return {
            "submitted": submitted,
            "failed": failed,
            "result": batch_result,
            "report_path": result.get("report_path"),
        }
