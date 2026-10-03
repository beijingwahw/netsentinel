"""交互式终端复核 TUI —— 键盘流复核工作台,内置四眼原则(NetSentinel V3 · A57)。

纯标准库实现(无 curses / 无第三方依赖),逐行读取 stdin 命令、向 stdout
打印结果,stdin/stdout 均可注入以便测试脚本化全流程::

    from netsentinel.cli.review_tui import ReviewTUI
    tui = ReviewTUI(db, required_four_eyes=True,
                    stdin=io.StringIO("list\\nquit\\n"), stdout=buf)
    rc = tui.run()  # -> 0

命令集(输入 help 可见)::

    list [--status pending|approved|rejected|submitted]
    show <编号>
    approve <编号> --reviewer <名字>     # 四眼启用时需两名不同审核人
    reject <编号> --note <理由>          # 理由必填
    help / quit

安全红线(契约 §0 第 11 条 + §3 A57):
  * 批准前必须回答人工确认问题"我已人工核实(Y/N)":
    仅接受 Y/y;N、空回车、乱码、EOF 一律视为取消,**绝不批准**;
  * 四眼 required=True 时单人永远只能留下"等待第二审核人"的痕迹;
  * 本 TUI 绝不自动批准(必须有 Y 确认),也绝不自动提交
    (没有任何 mark_submitted 调用路径)。

组合(而非修改)A50 的 FourEyesQueue;queue 参数可注入任何鸭子类型
队列(list/get/approve/reject,可选 summary/status/close)用于测试。

V5 升级(A101):
- 性能:``_print_table`` 单遍宽度计算——每个单元格的可见宽度只算一次并
  缓存复用(原先打印阶段经 _pad 重复计算一遍,宽度计算与 ANSI 剥离
  扫描次数减半);
- 可观测性:仅**成功**的 approve / reject 操作计数 ``telemetry.inc
  ("tui.approve")`` / ``telemetry.inc("tui.reject")``(取消、参数错误、
  队列层 ValueError 一律不计数);
- 质量:``_display_width`` 的 CJK 感知(> 0x2E7F 计 2 列)与 ``_strip_ansi``
  保持不变(既有测试锁定);``_status_label`` / ``_is_int`` 补类型注解;
  人工确认红线 ``_CONFIRM_QUESTION`` 一字不动,另有 test_v5_* 用例锁定。

A234 升级(事件账本接线,默认关闭):``ReviewTUI.__init__`` 新增仅关键字
参数 ``event_log``(默认 ``None`` = 现状逐字节不变);CLI 侧以
``--event-ledger <路径>`` 注入。启用后自建队列把账本透传给
:class:`~netsentinel.decision.four_eyes.FourEyesQueue`(A222),approve /
reject / 双人确认自动双写(先账本后状态,可离线重放对账);本 TUI 只持有
账本引用不拥有其生命周期(注入时由调用方管理,CLI 入口随 TUI 退出关闭)。
"""
from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path
from typing import TextIO

from netsentinel import telemetry
from netsentinel.decision.four_eyes import FourEyesQueue
from netsentinel.decision.review_queue import (
    DEFAULT_DB_PATH,
    STATUS_CN,
    VALID_STATUSES,
)
from netsentinel.storage.event_log import EventLog

__all__ = ["ReviewTUI", "main"]

# ---------------------------------------------------------------------------
# ANSI 颜色徽章(纯文本 + ANSI SGR,不依赖终端扩展)
# ---------------------------------------------------------------------------

ANSI_RESET = "\x1b[0m"
ANSI_RED = "\x1b[31m"      # 🔴 nsfw:高置信色情
ANSI_YELLOW = "\x1b[33m"   # 🟡 suspect:疑似
ANSI_GREEN = "\x1b[32m"    # 🟢 clean:无风险(clean 一般不入列,防御性渲染)

#: 判定 → (ANSI 颜色码, 徽章可见文本)。
_VERDICT_BADGES: dict[str, tuple[str, str]] = {
    "nsfw": (ANSI_RED, "🔴 高置信色情(nsfw)"),
    "suspect": (ANSI_YELLOW, "🟡 疑似(suspect)"),
    "clean": (ANSI_GREEN, "🟢 无风险(clean)"),
}

#: 判定中文名(兜底展示用,与 A10 VERDICT_CN 语义一致)。
_VERDICT_CN: dict[str, str] = {
    "clean": "无风险",
    "suspect": "疑似",
    "nsfw": "高置信色情",
}

#: 四眼状态中文名(展示用)。
_FOUR_EYES_CN: dict[str, str] = {
    "none": "尚无审核人确认",
    "awaiting_second": "等待第二审核人",
    "approved": "双人确认完成",
}

#: 人工确认问题(红线:批准前必答,仅 Y/y 通过)。
_CONFIRM_QUESTION = "我已人工核实(Y/N)"

#: 证据包提示语(契约 §3 A57 原文)。
_EVIDENCE_HINT = "证据包路径,请人工核验内容"


def _badge(verdict: object) -> str:
    """判定 → ANSI 彩色徽章(未知判定做灰色兜底,绝不抛错)。"""
    v = str(getattr(verdict, "value", verdict) or "").strip().lower()
    hit = _VERDICT_BADGES.get(v)
    if hit is not None:
        code, text = hit
        return f"{code}{text}{ANSI_RESET}"
    label = _VERDICT_CN.get(v, "未知")
    return f"○ {label}({v or '-'})(未知判定,防御性渲染)"


def _strip_ansi(text: str) -> str:
    """去掉 ANSI SGR 转义序列(表格列宽按可见宽度计算,防止错位)。"""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == "\x1b" and i + 1 < n and text[i + 1] == "[":
            j = i + 2
            while j < n and text[j] not in "m":
                j += 1
            i = j + 1 if j < n else n
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _display_width(text: str) -> int:
    """近似显示宽度:CJK/全角/emoji 按 2 列,ANSI 转义不计宽。

    示例::

        >>> _display_width("中文")                     # CJK 每字 2 列
        4
        >>> _display_width(ANSI_RED + "ab" + ANSI_RESET)  # ANSI 不计宽
        2
    """
    visible = _strip_ansi(text)
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in visible)


def _pad(text: str, width: int) -> str:
    """按可见宽度右侧补空格(ANSI 彩色单元格同样对齐)。"""
    return text + " " * max(0, width - _display_width(text))


def _status_label(status: object) -> str:
    """状态值 → 中文标签(未知状态兜底"未知",原值随括号展示)。"""
    s = str(getattr(status, "value", status) or "")
    return f"{STATUS_CN.get(s, '未知')}({s or '-'})"


def _fmt_agg(entry: object) -> str:
    """聚合分(agg_nsw_prob)防御性渲染。

    当前 ReviewQueue 的 Entry 未落库聚合分,列显示 "-";一旦接线层把
    ``agg_nsw_prob``(或别名 ``agg``)带上条目,本列即自动生效。
    """
    v = getattr(entry, "agg_nsw_prob", None)
    if v is None:
        v = getattr(entry, "agg", None)
    if isinstance(v, bool) or v is None:
        return "-"
    if isinstance(v, (int, float)):
        return f"{float(v):.2f}"
    s = str(v).strip()
    return s if s else "-"


def _is_int(text: str) -> bool:
    """字符串是否为合法整数(含正负号;空串与单符号不算)。"""
    return text.lstrip("+-").isdigit() and text.strip() not in ("", "+", "-")


# ---------------------------------------------------------------------------
# TUI 主体
# ---------------------------------------------------------------------------


class ReviewTUI:
    """键盘流复核工作台(逐行读命令,EOF/quit 退出并返回 0)。

    Parameters
    ----------
    db_path:
        SQLite 队列库路径(仅默认自建 FourEyesQueue 时使用;注入 queue 时
        仅用于横幅展示)。
    required_four_eyes:
        四眼原则是否启用(对应 ``cfg.four_eyes_required``)。
    stdin / stdout:
        可注入的文本流(缺省 sys.stdin / sys.stdout),测试零真实终端。
    queue:
        可注入的复核队列(FourEyesQueue 或结构兼容的 fake);注入时
        生命周期归调用方,TUI 不会关闭它。
    event_log:
        事件账本(A234 接线,默认 ``None`` = 现状逐字节不变)。仅对自建
        队列生效:透传给 FourEyesQueue 后 approve / reject / 双人确认自动
        双写(先账本后状态);注入 queue 时账本由调用方在 queue 侧自行
        装配,本参数不参与。TUI 只持有引用,**不拥有其生命周期**
        (close 不会关闭账本,由调用方管理)。
    """

    PROMPT = "复核> "

    def __init__(
        self,
        db_path: str | Path,
        required_four_eyes: bool,
        *,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        queue: FourEyesQueue | None = None,
        event_log: EventLog | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self.required_four_eyes = bool(required_four_eyes)
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        #: 事件账本(None = 不接线,行为与旧版逐字节一致;A234)。
        self.event_log = event_log
        self._owns_queue = queue is None
        self._queue: FourEyesQueue = (
            queue
            if queue is not None
            else FourEyesQueue(
                db_path, self.required_four_eyes, event_log=event_log
            )
        )

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run(self) -> int:
        """主循环:逐行读取并执行命令;EOF/quit 退出,恒返回 0。"""
        self._setup_stream()
        self._print_banner()
        try:
            while True:
                self.stdout.write(self.PROMPT)
                self.stdout.flush()
                line = self.stdin.readline()
                if line == "":  # EOF(Ctrl+D / Ctrl+Z+回车 / 注入流读尽)
                    self._print()
                    self._print("收到输入结束(EOF),退出复核 TUI。")
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    tokens = shlex.split(line)
                except ValueError as exc:
                    self._print(f"命令解析失败:{exc}(请检查引号是否成对)")
                    continue
                if not tokens:
                    continue
                cmd, args = tokens[0], tokens[1:]
                if cmd in ("quit", "exit"):
                    self._print("再见(未完成的条目保持原状态,绝不自动批准或提交)。")
                    break
                self._dispatch(cmd, args)
        except KeyboardInterrupt:  # 真实终端 Ctrl+C:安全退出,不批不放
            self._print()
            self._print("收到中断(Ctrl+C),退出复核 TUI。")
        finally:
            self._teardown_queue()
        return 0

    def _dispatch(self, cmd: str, args: list[str]) -> None:
        """命令分发;队列层 ValueError 一律转中文错误打印,绝不崩主循环。"""
        try:
            if cmd == "list":
                self._cmd_list(args)
            elif cmd == "show":
                self._cmd_show(args)
            elif cmd == "approve":
                self._cmd_approve(args)
            elif cmd == "reject":
                self._cmd_reject(args)
            elif cmd in ("help", "?"):
                self._cmd_help()
            else:
                self._print(f"未知命令:{cmd}(输入 help 查看可用命令)")
        except ValueError as exc:
            self._print(f"错误:{exc}")

    # ------------------------------------------------------------------
    # list / show
    # ------------------------------------------------------------------

    def _cmd_list(self, args: list[str]) -> None:
        status: str | None = None
        i = 0
        while i < len(args):
            tok = args[i]
            if tok == "--status":
                if i + 1 >= len(args):
                    self._print(
                        "错误:--status 需要一个状态值"
                        "(pending/approved/rejected/submitted)"
                    )
                    return
                status = args[i + 1]
                i += 2
            else:
                self._print(f"错误:list 不认识参数:{tok}(仅支持 --status <状态>)")
                return
        if status is not None and status not in VALID_STATUSES:
            self._print(
                f"错误:未知状态:{status}(可选:pending/approved/rejected/submitted)"
            )
            return

        entries = self._queue.list(status)
        if not entries:
            if status:
                self._print(f"按状态 {_status_label(status)} 筛选:共 0 条。")
            else:
                self._print("复核队列为空,暂无条目。")
            return

        headers = ["编号", "判定", "站点", "聚合分", "更新时间", "状态"]
        rows = [
            [
                str(e.id),
                _badge(e.verdict),
                e.site_url,
                _fmt_agg(e),
                getattr(e, "updated_at", "") or getattr(e, "created_at", "") or "-",
                _status_label(e.status),
            ]
            for e in entries
        ]
        self._print_table(headers, rows)
        summary_fn = getattr(self._queue, "summary", None)
        if callable(summary_fn):
            stats = summary_fn()
            if isinstance(stats, dict):
                self._print()
                self._print(
                    f"统计(共 {sum(stats.values())} 条):"
                    f"待复核 {stats.get('pending', 0)} · "
                    f"已确认 {stats.get('approved', 0)} · "
                    f"已驳回 {stats.get('rejected', 0)} · "
                    f"已提交 {stats.get('submitted', 0)}"
                )

    def _cmd_show(self, args: list[str]) -> None:
        if len(args) != 1 or not _is_int(args[0]):
            self._print("用法:show <编号>(编号为整数,例如 show 1)")
            return
        entry_id = int(args[0])
        entry = self._queue.get(entry_id)
        if entry is None:
            self._print(f"错误:复核条目不存在:id={entry_id}")
            return
        self._print_entry(entry, detail=True)

    # ------------------------------------------------------------------
    # approve(人工确认红线:必答 Y/N)
    # ------------------------------------------------------------------

    def _cmd_approve(self, args: list[str]) -> None:
        if not args or not _is_int(args[0]):
            self._print(
                "用法:approve <编号> --reviewer <名字>"
                "(编号为整数,例如 approve 1 --reviewer 张三)"
            )
            return
        entry_id = int(args[0])
        reviewer: str | None = None
        i = 1
        while i < len(args):
            tok = args[i]
            if tok == "--reviewer":
                if i + 1 >= len(args):
                    self._print(
                        "错误:--reviewer 需要审核人姓名,例如:approve 1 --reviewer 张三"
                    )
                    return
                reviewer = args[i + 1]
                i += 2
            else:
                self._print(f"错误:approve 不认识参数:{tok}(仅支持 --reviewer <名字>)")
                return
        if reviewer is None or not reviewer.strip():
            self._print(
                "错误:缺少 --reviewer:批准必须指定审核人姓名,"
                "例如 approve 1 --reviewer 张三"
            )
            return

        entry = self._queue.get(entry_id)
        if entry is None:
            self._print(f"错误:复核条目不存在:id={entry_id}")
            return

        # 流程:摘要 → 证据包提示 → 人工确认问题(Y/N)。
        self._print(f"── 条目 {entry_id} 摘要 ──")
        self._print_entry(entry, detail=False)
        self._print()
        self._print("批准前请打开证据包人工核实(上方“证据包”一行即证据包路径)。")
        answer = self._ask(_CONFIRM_QUESTION)
        if answer not in ("Y", "y"):
            # 红线:N / 空回车 / 乱码 / EOF 一律取消,绝不批准。
            self._print("已取消:人工确认未通过(仅接受 Y),条目未被批准。")
            return
        try:
            result = self._queue.approve(entry_id, reviewer)
        except ValueError as exc:
            self._print(f"错误:{exc}")
            return
        telemetry.inc("tui.approve")  # V5:仅成功操作计数(取消/异常均不计数)
        self._print_approve_result(entry_id, result)

    def _print_approve_result(self, entry_id: int, result: object) -> None:
        """按队列返回的 state 打印中文结果(dict 之外的返回做防御兜底)。"""
        state = result.get("state") if isinstance(result, dict) else None
        if state == "awaiting_second":
            first = ""
            if isinstance(result, dict):
                first = str(result.get("first") or "")
                if not first:
                    reviewers = result.get("reviewers") or []
                    first = str(reviewers[0]) if reviewers else ""
            self._print(
                f"条目 {entry_id} 等待第二审核人(第一人:{first}):"
                "四眼原则下单人不能放行,需另一名审核人再次确认。"
            )
        elif state == "approved":
            reviewers: list[str] = []
            if isinstance(result, dict):
                reviewers = [str(r) for r in (result.get("reviewers") or [])]
            tail = "(审核人:" + "、".join(reviewers) + ")" if reviewers else ""
            self._print(f"条目 {entry_id} 已批准{tail}。")
            self._print("举报提交仍须走人工门,本 TUI 不会自动提交。")
        else:
            self._print(f"条目 {entry_id} 审批返回未知状态:{state}(请检查队列实现)")

    # ------------------------------------------------------------------
    # reject(理由必填)
    # ------------------------------------------------------------------

    def _cmd_reject(self, args: list[str]) -> None:
        if not args or not _is_int(args[0]):
            self._print(
                "用法:reject <编号> --note <理由>"
                "(编号为整数,例如 reject 1 --note 证据不足)"
            )
            return
        entry_id = int(args[0])
        note: str | None = None
        i = 1
        while i < len(args):
            tok = args[i]
            if tok == "--note":
                if i + 1 >= len(args):
                    self._print(
                        "错误:--note 需要驳回理由(必填),例如:reject 1 --note 证据不足"
                    )
                    return
                note = args[i + 1]
                i += 2
            else:
                self._print(f"错误:reject 不认识参数:{tok}(仅支持 --note <理由>)")
                return
        if note is None or not note.strip():
            self._print(
                "错误:驳回必须填写理由:请补 --note \"理由\" 后重试"
                "(空理由或纯空白不被接受)"
            )
            return
        try:
            entry = self._queue.reject(entry_id, note=note)
        except ValueError as exc:
            self._print(f"错误:{exc}")
            return
        telemetry.inc("tui.reject")  # V5:仅成功操作计数(缺理由/异常均不计数)
        new_status = getattr(entry, "status", "rejected")
        self._print(
            f"条目 {entry_id} 已驳回:{_status_label('pending')} → {_status_label(new_status)}"
        )
        self._print(f"驳回理由:{note}")

    # ------------------------------------------------------------------
    # help
    # ------------------------------------------------------------------

    def _cmd_help(self) -> None:
        four_eyes_line = (
            "(四眼原则已启用:approve 需两名不同审核人先后确认;第一人确认后条目保持待复核)"
            if self.required_four_eyes
            else "(四眼原则未启用:单人 approve 即可确认,但仍须回答人工确认问题)"
        )
        usage = [
            ("list [--status pending|approved|rejected|submitted]", "列出复核条目"),
            ("show <编号>", "查看条目详情(含证据包路径)"),
            ("approve <编号> --reviewer <名字>", "批准(先答“我已人工核实(Y/N)”)"),
            ("reject <编号> --note <理由>", "驳回(理由必填)"),
            ("help", "显示本帮助"),
            ("quit", "退出(也可 Ctrl+D / Ctrl+Z+回车)"),
        ]
        width = max(_display_width(cmd) for cmd, _ in usage) + 2
        self._print("可用命令:")
        for cmd, desc in usage:
            self._print("  " + _pad(cmd, width) + desc)
        self._print(f"  {four_eyes_line}")
        self._print("安全红线:批准前必须人工核实证据包;本 TUI 绝不自动批准、绝不自动提交。")

    # ------------------------------------------------------------------
    # 渲染辅助
    # ------------------------------------------------------------------

    def _print_banner(self) -> None:
        mode = "已启用(双人复核)" if self.required_four_eyes else "未启用(单人复核)"
        self._print("=== 净网哨兵 NetSentinel · 交互式终端复核 TUI ===")
        self._print(f"数据库:{self.db_path}")
        self._print(f"四眼原则:{mode}")
        if self._owns_queue and self.event_log is not None:
            # A234:账本接线可见性(仅自建队列才由本 TUI 透传;注入队列的
            # 账本装配归调用方,横幅不做无据声明)。
            ledger_path = str(getattr(self.event_log, "db_path", "") or "?")
            self._print(f"事件账本:已接线({ledger_path},复核操作先账本后状态双写)")
        self._print("输入 help 查看命令;输入 quit 退出。本 TUI 绝不自动批准、绝不自动提交。")
        self._print()

    def _print_entry(self, entry: object, *, detail: bool) -> None:
        """条目渲染(list 之外的摘要/详情);字段全部防御性读取。"""
        entry_id = getattr(entry, "id", "?")
        evidence = str(getattr(entry, "evidence_zip", "") or "")
        evidence_line = (
            f"{evidence}({_EVIDENCE_HINT})" if evidence else "(尚未打包;批准前仍须人工核实已有材料)"
        )
        self._print(f"编号    : {entry_id}")
        self._print(f"站点    : {getattr(entry, 'site_url', '-') or '-'}")
        self._print(f"判定    : {_badge(getattr(entry, 'verdict', ''))}")
        self._print(f"聚合分  : {_fmt_agg(entry)}")
        self._print(f"状态    : {_status_label(getattr(entry, 'status', ''))}")
        self._print(f"证据包  : {evidence_line}")
        if detail:
            self._print(f"创建时间: {getattr(entry, 'created_at', '') or '-'}")
            self._print(f"更新时间: {getattr(entry, 'updated_at', '') or '-'}")
            self._print(f"备注    : {getattr(entry, 'note', '') or '-'}")
            four_eyes = self._four_eyes_line(int(entry_id) if _is_int(str(entry_id)) else -1)
            if four_eyes:
                self._print(f"四眼状态: {four_eyes}")

    def _four_eyes_line(self, entry_id: int) -> str | None:
        """四眼状态行(队列没有 status 能力时安静跳过,报错不上抛)。"""
        status_fn = getattr(self._queue, "status", None)
        if not callable(status_fn):
            return None
        try:
            st = status_fn(entry_id)
        except ValueError:
            return None
        if not isinstance(st, dict):
            return None
        reviewers = [str(r) for r in (st.get("reviewers") or [])]
        state = st.get("state")
        if state == "awaiting_second":
            first = reviewers[0] if reviewers else str(st.get("first", ""))
            return f"等待第二审核人(第一人:{first})"
        if state == "approved":
            tail = "(审核人:" + "、".join(reviewers) + ")" if reviewers else ""
            return f"{_FOUR_EYES_CN['approved']}{tail}"
        if state == "none":
            if reviewers:
                return "已有留痕:" + "、".join(reviewers) + "(当前无待完成的确认流程)"
            return _FOUR_EYES_CN["none"]
        return f"{state}"

    def _print_table(self, headers: list[str], rows: list[list[str]]) -> None:
        """简易表格:列宽按可见宽度(去 ANSI、CJK/emoji 计 2 列)对齐。

        V5 性能:宽度**单遍**计算——每个单元格(含表头)的可见宽度只算
        一次并缓存,打印阶段直接按缓存宽度补空格;原先打印阶段经
        :func:`_pad` 对每个单元格再算一遍宽度(内含 ANSI 剥离整串扫描),
        现在宽度计算与剥离扫描次数减半,输出与原先逐字节一致。
        """
        header_widths = [_display_width(h) for h in headers]
        widths = list(header_widths)
        cell_widths: list[list[int]] = []
        for row in rows:
            row_widths: list[int] = []
            for i, cell in enumerate(row):
                w = _display_width(cell)
                row_widths.append(w)
                if w > widths[i]:
                    widths[i] = w
            cell_widths.append(row_widths)
        self._print(
            "  ".join(
                h + " " * max(0, widths[i] - header_widths[i])
                for i, h in enumerate(headers)
            ).rstrip()
        )
        self._print("  ".join("-" * w for w in widths))
        for row, row_widths in zip(rows, cell_widths):
            self._print(
                "  ".join(
                    cell + " " * max(0, widths[i] - row_widths[i])
                    for i, cell in enumerate(row)
                ).rstrip()
            )

    # ------------------------------------------------------------------
    # IO 与生命周期
    # ------------------------------------------------------------------

    def _print(self, text: str = "") -> None:
        self.stdout.write(text + "\n")

    def _ask(self, question: str) -> str:
        """提问并读取一行回答(真实终端上回答显示在同一行;EOF 返回空串)。"""
        self.stdout.write(f"{question}: ")
        self.stdout.flush()
        return self.stdin.readline().strip()

    def _setup_stream(self) -> None:
        """真实终端兜底:不可编码字符(如 🔴)以替换符输出而非崩溃。"""
        try:
            self.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass  # 注入流(StringIO 等)没有 reconfigure,直接跳过

    def _teardown_queue(self) -> None:
        """只关闭自己创建的队列;注入队列的生命周期归调用方。"""
        if not self._owns_queue:
            return
        close_fn = getattr(self._queue, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:  # pragma: no cover - 关闭异常不阻断退出
                pass


# ---------------------------------------------------------------------------
# CLI:python -m netsentinel.cli.review_tui
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m netsentinel.cli.review_tui",
        description="净网哨兵 · 交互式终端复核 TUI(纯标准库,无 curses;绝不自动批准/提交)",
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help="SQLite 队列数据库路径(默认:%(default)s)",
    )
    parser.add_argument(
        "--four-eyes",
        action="store_true",
        help="启用四眼原则(双人复核),对应配置 four_eyes_required",
    )
    parser.add_argument(
        "--event-ledger",
        default=None,
        metavar="路径",
        help=(
            "事件账本 SQLite 路径(A234 接线,默认关闭=不接账本):启用后"
            " approve / reject / 双人确认先账本后状态双写,可离线重放对账"
            " (python -m netsentinel.storage.replay)"
        ),
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """CLI 入口:解析 --db / --four-eyes / --event-ledger 后进入 TUI 主循环。

    ``--event-ledger`` 给出账本路径时,CLI 侧打开 :class:`EventLog` 注入
    TUI(自建队列透传,复核动作自动双写),并在 TUI 退出后负责关闭
    (账本生命周期归本入口);缺省不接账本 = 现状行为逐字节不变。
    """
    args = _build_parser().parse_args(argv)
    ledger: EventLog | None = None
    if args.event_ledger:
        ledger = EventLog(args.event_ledger)
    try:
        tui = ReviewTUI(
            args.db,
            args.four_eyes,
            stdin=stdin,
            stdout=stdout,
            event_log=ledger,
        )
        return tui.run()
    finally:
        if ledger is not None:
            try:
                ledger.close()
            except Exception:  # pragma: no cover - 关闭异常不阻断退出码
                pass


if __name__ == "__main__":
    raise SystemExit(main())
