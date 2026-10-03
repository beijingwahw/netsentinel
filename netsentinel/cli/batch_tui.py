"""交互式批量复核 TUI —— 终端里看分组、逐组声明、预览批量队列(NetSentinel V6 · A111)。

在 A57 复核 TUI(逐**条**拍板)之上面向**案件组**的批量工作台:把
"approved 且备注带 ``[组:名]`` 标记"的复核条目按组聚合展示,运营者在终端
逐组完成证据核验声明(红线 25),再预览批量举报队列。纯标准库实现
(无 curses),逐行读取 stdin 命令、向 stdout 打印结果,stdin/stdout
均可注入以便测试脚本化全流程::

    from netsentinel.cli.batch_tui import BatchTUI
    tui = BatchTUI(db, cfg, stdin=io.StringIO("groups\\nquit\\n"), stdout=buf)
    rc = tui.run()  # -> 0

命令集(输入 help 可见)::

    groups                               分组总览(组名/条数/判定徽章/agg 最大/已声明 ✓✗)
    show <组名>                          组内逐条明细(站点/判定/agg/证据包路径)
    attest <组名> --reviewer <名字>      批量确认声明(先 show,再答“我已逐站人工核实(Y/N)”)
    ready                                待批量清单(A110 ready_entries)
    queue-batch [--portal 12377|shdf]    批量队列预览(仅 dry_run 演练与打印真实命令)
    help / quit

安全红线(V6 契约 §0 第 24–25 条):
  * 本 TUI **绝不执行真实提交**:queue-batch 确认后只打印 dry_run 演练调用
    示例与真实执行命令行,不调用任何执行器;批量举报仍是"逐条人工门",
    每一条提交的验证码输入与最终确认由人工在执行器 HUMAN_GATE 完成;
  * attest 必须经过"我已逐站人工核实(Y/N)":仅接受 Y/y;N、空回车、乱码、
    EOF 一律视为取消,**绝不落声明**;
  * 声明文案为固定模板且必含"人工核实"四字(文案校验在 A110 收口),
    声明写入审计留痕(含组名/条数/审核人)。

依赖关系:仅依赖既有模块(review_queue / contracts / telemetry / config);
兄弟模块 A110(:mod:`netsentinel.decision.batch_review`)采用**惰性导入**
(并行开发期可能尚未合入,缺失时 groups 声明列降级为"?"、attest/ready/
queue-batch 给出中文错误);A112(:mod:`netsentinel.submit.batch_submit`)
只以**命令文本**形式出现在演练示例里,本模块从不 import 它、更不执行。
"""
from __future__ import annotations

import argparse
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO

from netsentinel import telemetry
from netsentinel.config import load_config
from netsentinel.contracts import Config
from netsentinel.decision.review_queue import (
    DEFAULT_DB_PATH,
    STATUS_CN,
    ReviewQueue,
)

__all__ = ["BatchTUI", "main"]

# ---------------------------------------------------------------------------
# ANSI 颜色徽章(与 A57 review_tui 同风格:纯文本 + ANSI SGR)
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

#: 判定严重档(组判定取最严重档,与 A104 case_group 语义一致)。
_VERDICT_RANK: dict[str, int] = {"clean": 0, "suspect": 1, "nsfw": 2}

#: 人工确认问题(红线 25:声明前必答,仅 Y/y 通过;一字不可改)。
_CONFIRM_QUESTION = "我已逐站人工核实(Y/N)"

#: 组内证据包提示语(任务书 §4 A111 原文)。
_EVIDENCE_HINT = "请人工核验内容"

#: 批量提交提示(ready 命令固定输出;任务书原文)。
_READY_HINT = "批量提交需在 CLI/TUI 外经 run_batch 逐条人工门执行"

#: 声明固定文案模板(必含"人工核实"四字;文案校验在 A110 收口)。
ATTEST_TEXT_TMPL = (
    "审核人 {reviewer} 已逐站打开证据包完成人工核实,确认组“{name}”共 {n} 条"
    "站点证据真实、判定无误,同意该组进入批量举报队列(批量确认声明 · 红线 25 留痕)。"
)

#: 参与批量分组的条目状态(待复核 + 已确认;rejected/submitted 不参与)。
_GROUP_STATUSES: tuple[str, ...] = ("pending", "approved")

#: 备注中的组标记前缀:"[组:example.com] 其余说明"(容忍全角冒号与空白)。
_GROUP_TAG_RE = re.compile(r"^\s*\[组[:：]\s*(?P<name>[^\]]+?)\s*\]")

#: queue-batch --portal 合法取值(A110 ready 条目 portal 同域)。
VALID_PORTALS: tuple[str, ...] = ("12377", "shdf")


def _badge(verdict: object) -> str:
    """判定 → ANSI 彩色徽章(未知判定做灰色兜底,绝不抛错)。"""
    v = str(getattr(verdict, "value", verdict) or "").strip().lower()
    hit = _VERDICT_BADGES.get(v)
    if hit is not None:
        code, text = hit
        return f"{code}{text}{ANSI_RESET}"
    return f"○ 未知判定({v or '-'})(防御性渲染)"


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
    """近似显示宽度:CJK/全角/emoji 按 2 列,ANSI 转义不计宽。"""
    visible = _strip_ansi(text)
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in visible)


def _pad(text: str, width: int) -> str:
    """按可见宽度右侧补空格(ANSI 彩色单元格同样对齐)。"""
    return text + " " * max(0, width - _display_width(text))


def _status_label(status: object) -> str:
    """状态值 → 中文标签(未知状态兜底"未知",原值随括号展示)。"""
    s = str(getattr(status, "value", status) or "")
    return f"{STATUS_CN.get(s, '未知')}({s or '-'})"


def _agg_value(entry: object) -> float | None:
    """防御性读取条目聚合分(agg_nsw_prob 或别名 agg);无/非数 → None。"""
    v = getattr(entry, "agg_nsw_prob", None)
    if v is None:
        v = getattr(entry, "agg", None)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _fmt_agg(entry: object) -> str:
    """聚合分渲染:两位小数;缺失显示"-"。"""
    v = _agg_value(entry)
    return "-" if v is None else f"{v:.2f}"


def _verdict_key(entry: object) -> str:
    """条目判定归一为小写字符串(兼容 Verdict 枚举与纯字符串)。"""
    raw = getattr(entry, "verdict", "")
    return str(getattr(raw, "value", raw) or "").strip().lower()


def _parse_group_name(note: object) -> str | None:
    """备注 → 组名;无 ``[组:名]`` 标记返回 None。"""
    m = _GROUP_TAG_RE.match(str(note or ""))
    if m is None:
        return None
    name = m.group("name").strip()
    return name or None


@dataclass
class _GroupInfo:
    """TUI 内部的分组视图(按备注标记把 pending/approved 条目聚到一起)。"""

    name: str
    entries: list

    @property
    def size(self) -> int:
        return len(self.entries)

    @property
    def verdict(self) -> str:
        """组判定:取组内最严重档(nsfw > suspect > clean;未知档垫底)。"""
        best, best_rank = "", -1
        for e in self.entries:
            rank = _VERDICT_RANK.get(_verdict_key(e), -1)
            if rank > best_rank:
                best, best_rank = _verdict_key(e), rank
        return best

    @property
    def agg_max(self) -> str:
        """组内聚合分最大值(两位小数);全部缺失显示"-"。"""
        values = [v for v in (_agg_value(e) for e in self.entries) if v is not None]
        return "-" if not values else f"{max(values):.2f}"


# ---------------------------------------------------------------------------
# TUI 主体
# ---------------------------------------------------------------------------


class BatchTUI:
    """批量复核工作台(逐行读命令,EOF/quit 退出并返回 0)。

    Parameters
    ----------
    db_path:
        SQLite 队列库路径(仅默认自建 ReviewQueue / BatchReview 时使用;
        注入 queue / review 时仅用于横幅展示)。
    cfg:
        配置对象(:class:`netsentinel.contracts.Config`),传给 A110
        ``ready_entries`` 等下游。
    stdin / stdout:
        可注入的文本流(缺省 sys.stdin / sys.stdout),测试零真实终端。
    queue:
        可注入的复核队列(ReviewQueue / FourEyesQueue 或结构兼容 fake,
        需 ``list(status)``);注入时生命周期归调用方。
    review:
        可注入的批量确认声明层(A110 BatchReview 或结构兼容 fake,需
        ``attest`` / ``is_attested``,可选 ``ready_entries``);缺省惰性
        构造真实 BatchReview(模块缺失时降级,见模块 docstring)。
    """

    PROMPT = "批量复核> "

    def __init__(
        self,
        db_path: str | Path,
        cfg: Config,
        *,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        queue: ReviewQueue | None = None,
        review: object | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self.cfg = cfg
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        self._owns_queue = queue is None
        self._queue: ReviewQueue = (
            queue if queue is not None else ReviewQueue(self.db_path)
        )
        self._review: object | None = review
        self._owns_review = False  # 惰性自建时置 True
        # 红线 24 测试哨兵:本 TUI 任何代码路径都**不调用**该钩子;测试注入
        # 带计数的 fake 执行器并断言计数恒为 0,证明 queue-batch 只演练不执行。
        self._run_batch_hook: Callable[..., object] | None = None
        # ready_entries 注入点(测试/扩展用);缺省惰性解析 A110 模块函数。
        self._ready_entries_fn: Callable[..., list[dict]] | None = None

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
                    self._print("收到输入结束(EOF),退出批量复核 TUI。")
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
                    self._print(
                        "再见(未完成的声明与批量队列保持原状,绝不自动提交)。"
                    )
                    break
                self._dispatch(cmd, args)
        except KeyboardInterrupt:  # 真实终端 Ctrl+C:安全退出,不声明不提交
            self._print()
            self._print("收到中断(Ctrl+C),退出批量复核 TUI。")
        finally:
            self._teardown()
        return 0

    def _dispatch(self, cmd: str, args: list[str]) -> None:
        """命令分发;队列/声明层 ValueError 一律转中文错误,绝不崩主循环。"""
        try:
            if cmd == "groups":
                self._cmd_groups()
            elif cmd == "show":
                self._cmd_show(args)
            elif cmd == "attest":
                self._cmd_attest(args)
            elif cmd == "ready":
                self._cmd_ready()
            elif cmd == "queue-batch":
                self._cmd_queue_batch(args)
            elif cmd in ("help", "?"):
                self._cmd_help()
            else:
                self._print(f"未知命令:{cmd}(输入 help 查看可用命令)")
        except ValueError as exc:
            self._print(f"错误:{exc}")

    # ------------------------------------------------------------------
    # groups:分组总览
    # ------------------------------------------------------------------

    def _collect_groups(self) -> tuple[list[_GroupInfo], int]:
        """把 pending/approved 条目按备注 ``[组:名]`` 聚组(首现顺序稳定)。

        返回 (组列表, 未带组标记条数);rejected / submitted 不参与批量。
        """
        buckets: dict[str, list] = {}
        order: list[str] = []
        ungrouped = 0
        for status in _GROUP_STATUSES:
            for e in self._queue.list(status):
                name = _parse_group_name(getattr(e, "note", "") or "")
                if name is None:
                    ungrouped += 1
                    continue
                if name not in buckets:
                    buckets[name] = []
                    order.append(name)
                buckets[name].append(e)
        groups = [_GroupInfo(name=n, entries=buckets[n]) for n in order]
        return groups, ungrouped

    def _find_group(self, name: str) -> _GroupInfo | None:
        groups, _ = self._collect_groups()
        for g in groups:
            if g.name == name:
                return g
        return None

    def _attest_mark(self, group_name: str) -> tuple[str, bool]:
        """组 → 声明状态徽章(✓ 已声明 / ✗ 未声明);返回 (徽章, 是否降级)。"""
        review = self._ensure_review()
        if review is None:
            return "?(声明模块未安装)", True
        try:
            ok = bool(review.is_attested(group_name))
        except ValueError:
            return "?(声明状态读取失败)", True
        if ok:
            return f"{ANSI_GREEN}✓ 已声明{ANSI_RESET}", False
        return f"{ANSI_RED}✗ 未声明{ANSI_RESET}", False

    def _cmd_groups(self) -> None:
        groups, ungrouped = self._collect_groups()
        if not groups:
            self._print(
                "当前没有可批量分组的条目"
                "(仅统计待复核/已确认且备注含“[组:名]”标记的条目)。"
            )
            return
        headers = ["组名", "条数", "判定", "agg 最大", "已声明"]
        rows: list[list[str]] = []
        degraded = False
        pending_n = approved_n = 0
        for g in groups:
            mark, is_degraded = self._attest_mark(g.name)
            degraded = degraded or is_degraded
            for e in g.entries:
                status = str(getattr(e, "status", "") or "")
                if status == "approved":
                    approved_n += 1
                elif status == "pending":
                    pending_n += 1
            rows.append(
                [g.name, str(g.size), _badge(g.verdict), g.agg_max, mark]
            )
        self._print_table(headers, rows)
        total = sum(g.size for g in groups)
        self._print()
        self._print(
            f"共 {len(groups)} 组 · {total} 条"
            f"(待复核 {pending_n} · 已确认 {approved_n});"
            f"未带组标记 {ungrouped} 条(不参与批量声明/提交)。"
        )
        if degraded:
            self._print(
                "提示:批量确认声明模块(netsentinel.decision.batch_review,A110)"
                "不可用,声明状态列降级为“?”;attest / ready / queue-batch 暂不可用。"
            )

    # ------------------------------------------------------------------
    # show:组内逐条明细
    # ------------------------------------------------------------------

    def _cmd_show(self, args: list[str]) -> None:
        if len(args) != 1 or not args[0].strip():
            self._print("用法:show <组名>(组名来自 groups 列表,例如 show example.com)")
            return
        group = self._find_group(args[0].strip())
        if group is None:
            self._print(f"错误:未找到组:{args[0].strip()}(输入 groups 查看全部组名)")
            return
        self._print_group_detail(group)

    def _print_group_detail(self, group: _GroupInfo) -> None:
        """组明细渲染(attest 前置流程复用):站点/判定/agg/证据包 + 人工核验提示。"""
        self._print(f"── 组 {group.name}(共 {group.size} 条)──")
        for e in group.entries:
            self._print(f"── 条目 {getattr(e, 'id', '?')} ──")
            self._print(f"站点    : {getattr(e, 'site_url', '-') or '-'}")
            self._print(f"判定    : {_badge(getattr(e, 'verdict', ''))}")
            self._print(f"聚合分  : {_fmt_agg(e)}")
            self._print(f"状态    : {_status_label(getattr(e, 'status', ''))}")
            evidence = str(getattr(e, "evidence_zip", "") or "")
            if evidence:
                self._print(f"证据包  : {evidence}({_EVIDENCE_HINT})")
            else:
                self._print("证据包  : (尚未打包;请人工核验已有材料)")

    # ------------------------------------------------------------------
    # attest:批量确认声明(红线 25:必答"我已逐站人工核实(Y/N)")
    # ------------------------------------------------------------------

    def _cmd_attest(self, args: list[str]) -> None:
        if not args or not args[0].strip():
            self._print(
                "用法:attest <组名> --reviewer <名字>"
                "(例如 attest example.com --reviewer 张三)"
            )
            return
        group_name = args[0].strip()
        reviewer: str | None = None
        i = 1
        while i < len(args):
            tok = args[i]
            if tok == "--reviewer":
                if i + 1 >= len(args):
                    self._print(
                        "错误:--reviewer 需要审核人姓名,"
                        "例如:attest example.com --reviewer 张三"
                    )
                    return
                reviewer = args[i + 1]
                i += 2
            else:
                self._print(
                    f"错误:attest 不认识参数:{tok}(仅支持 --reviewer <名字>)"
                )
                return
        if reviewer is None or not reviewer.strip():
            self._print(
                "错误:缺少 --reviewer:批量确认声明必须指定审核人姓名,"
                "例如 attest example.com --reviewer 张三"
            )
            return
        reviewer = reviewer.strip()

        group = self._find_group(group_name)
        if group is None:
            self._print(f"错误:未找到组:{group_name}(输入 groups 查看全部组名)")
            return
        review = self._ensure_review()
        if review is None:
            self._print(
                "错误:批量确认声明模块(netsentinel.decision.batch_review,A110)"
                "不可用,无法记录声明。"
            )
            return

        # 流程:show 逐条明细 → 证据核验提示 → 人工确认问题(Y/N)。
        self._print_group_detail(group)
        self._print()
        self._print("批准前请逐站打开证据包核实(上方每条“证据包”行即证据包路径)。")
        answer = self._ask(_CONFIRM_QUESTION)
        if answer not in ("Y", "y"):
            # 红线:N / 空回车 / 乱码 / EOF 一律取消,绝不落声明。
            self._print("已取消:人工确认未通过(仅接受 Y),未记录任何声明。")
            return
        text = ATTEST_TEXT_TMPL.format(reviewer=reviewer, name=group_name, n=group.size)
        try:
            result = review.attest(group_name, group.size, reviewer, text)
        except ValueError as exc:
            self._print(f"错误:{exc}")
            return
        telemetry.inc("tui.batch.attest")  # 仅成功声明计数(取消/异常不计数)
        ts = getattr(result, "ts", "") or ""
        ts_tail = f",时间:{ts}" if ts else ""
        self._print(
            f"组 {group_name} 声明已记录(共 {group.size} 条,审核人:{reviewer}{ts_tail});"
            "声明文本已含“人工核实”并写入审计留痕。"
        )
        self._print("声明只代表核验完成;批量提交仍须在 CLI/TUI 外经逐条人工门执行。")

    # ------------------------------------------------------------------
    # ready:待批量清单(A110 ready_entries)
    # ------------------------------------------------------------------

    def _resolve_ready_fn(self) -> Callable[..., list[dict]] | None:
        """解析 ready_entries:注入点优先,其次 A110 模块函数,最后 review 对象。"""
        if self._ready_entries_fn is not None:
            return self._ready_entries_fn
        try:
            from netsentinel.decision.batch_review import ready_entries  # 惰性

            return ready_entries
        except ImportError:
            pass
        review = self._review
        fn = getattr(review, "ready_entries", None)
        return fn if callable(fn) else None

    def _load_ready_items(self) -> list[dict] | None:
        """取待批量条目(每条 dict:entry_id/group_name/site_urls/portal)。"""
        fn = self._resolve_ready_fn()
        if fn is None:
            self._print(
                "错误:待批量清单模块(netsentinel.decision.batch_review,A110)"
                "不可用,无法生成 ready 条目。"
            )
            return None
        items = fn(self.cfg, queue=self._queue, review=self._review)
        if not isinstance(items, list):
            self._print("错误:ready_entries 返回值不是列表(请检查 A110 实现)。")
            return None
        return [dict(item) for item in items]

    def _print_ready_table(self, items: list[dict]) -> None:
        headers = ["序号", "条目", "组名", "站点", "门户"]
        rows = []
        for i, item in enumerate(items, start=1):
            urls = item.get("site_urls") or []
            if isinstance(urls, str):
                urls = [urls]
            rows.append(
                [
                    str(i),
                    str(item.get("entry_id", "?")),
                    str(item.get("group_name", "?")),
                    ", ".join(str(u) for u in urls) or "-",
                    str(item.get("portal", "12377")),
                ]
            )
        self._print_table(headers, rows)

    def _cmd_ready(self) -> None:
        items = self._load_ready_items()
        if items is None:
            return
        if not items:
            self._print(
                "当前没有待批量条目:"
                "仅“已确认(approved)”且“已完成声明”的组进入批量队列"
                "(可先 groups 查看各组声明状态)。"
            )
            return
        self._print(f"待批量清单(共 {len(items)} 条):")
        self._print_ready_table(items)
        self._print()
        self._print(f"{_READY_HINT}(本 TUI 不提交)。")

    # ------------------------------------------------------------------
    # queue-batch:批量队列预览(只演练 / 打印命令,绝不执行)
    # ------------------------------------------------------------------

    def _cmd_queue_batch(self, args: list[str]) -> None:
        portal: str | None = None
        i = 0
        while i < len(args):
            tok = args[i]
            if tok == "--portal":
                if i + 1 >= len(args):
                    self._print("错误:--portal 需要门户名(可选:12377 / shdf)")
                    return
                portal = args[i + 1]
                i += 2
            else:
                self._print(
                    f"错误:queue-batch 不认识参数:{tok}(仅支持 --portal 12377|shdf)"
                )
                return
        if portal is not None and portal not in VALID_PORTALS:
            self._print(
                f"错误:未知门户:{portal}(可选:{' / '.join(VALID_PORTALS)})"
            )
            return

        items = self._load_ready_items()
        if items is None:
            return
        if not items:
            self._print("无待批量条目,无需批量(可用 groups / ready 查看原因)。")
            return
        if portal is not None:
            for item in items:
                item["portal"] = portal

        n = len(items)
        max_items = getattr(self.cfg, "batch_max_items", None)
        interval = getattr(self.cfg, "batch_item_interval_s", None)
        per_day = getattr(self.cfg, "submit_max_per_day", None)
        self._print(f"── ready 预览(共 {n} 条;批量上限 batch_max_items={max_items})──")
        self._print_ready_table(items)
        self._print()
        self._print(
            f"频控:相邻提交间隔 ≥ {interval}s,每日 ≤ {per_day} 条"
            "(红线 26:额度用尽自动挂起,可续批)。"
        )
        answer = self._ask(f"确认将依次批量举报 {n} 条,每条仍需人工输入验证码(Y/N)")
        if answer not in ("Y", "y"):
            self._print("已取消:未确认,未执行任何批量提交(队列与声明状态未变)。")
            return

        # 红线 24:确认后也只**打印** dry_run 演练示例与真实命令,绝不执行。
        self._print()
        self._print("已确认。以下仅为演练示例与命令打印,本 TUI 不执行任何提交。")
        self._print("── dry_run 演练调用示例(不打开浏览器、不提交)──")
        self._print("  from netsentinel.submit.batch_submit import run_batch")
        self._print(
            "  items = ready_entries(cfg, queue=..., review=...)"
            f"  # 即上方 {n} 条(门户 {items[0].get('portal', '12377')})"
        )
        self._print("  result = run_batch(items, cfg, dry_run=True)")
        self._print("── 真实执行命令(需另行人工执行;每条仍需人工输入验证码)──")
        self._print("  python -m netsentinel.batchflow --resume <批次号> --exec")
        self._print(
            f"门户:{items[0].get('portal', '12377')}"
            "(随 ready 条目传入);批次号由 BatchState 新建批次后确定。"
        )
        self._print(
            "安全红线:批量举报仍是逐条人工门(红线 24);"
            "本 TUI 绝不代填验证码、绝不自动确认。"
        )

    # ------------------------------------------------------------------
    # help
    # ------------------------------------------------------------------

    def _cmd_help(self) -> None:
        usage = [
            ("groups", "查看案件分组(组名/条数/判定/agg 最大/声明状态 ✓✗)"),
            ("show <组名>", "查看组内逐条明细(站点/判定/agg/证据包路径)"),
            (
                "attest <组名> --reviewer <名字>",
                "批量确认声明(先 show,再答“我已逐站人工核实(Y/N)”)",
            ),
            ("ready", "待批量清单(approved 且已声明的组)"),
            (
                "queue-batch [--portal 12377|shdf]",
                "批量队列预览:仅 dry_run 演练与打印真实命令,不执行",
            ),
            ("help", "显示本帮助"),
            ("quit", "退出(也可 Ctrl+D / Ctrl+Z+回车)"),
        ]
        width = max(_display_width(cmd) for cmd, _ in usage) + 2
        self._print("可用命令:")
        for cmd, desc in usage:
            self._print("  " + _pad(cmd, width) + desc)
        self._print()
        self._print(
            "安全红线:本 TUI 不执行真实提交;批量举报仍是逐条人工门,"
            "验证码由人工输入;组进入批量队列前必须完成"
            "“我已逐站人工核实(Y/N)”声明(留痕,红线 24/25)。"
        )

    # ------------------------------------------------------------------
    # 声明层(惰性 A110)
    # ------------------------------------------------------------------

    def _ensure_review(self) -> object | None:
        """取声明层:注入优先;缺省惰性构造真实 BatchReview(模块缺失→None)。"""
        if self._review is not None:
            return self._review
        try:
            from netsentinel.decision.batch_review import BatchReview  # 惰性

        except ImportError:
            return None
        self._review = BatchReview(self.db_path)
        self._owns_review = True
        return self._review

    # ------------------------------------------------------------------
    # 渲染辅助
    # ------------------------------------------------------------------

    def _print_banner(self) -> None:
        self._print("=== 净网哨兵 NetSentinel · 批量复核 TUI(V6 · A111)===")
        self._print(f"数据库:{self.db_path}")
        self._print("分组来源:待复核/已确认条目备注中的“[组:名]”标记。")
        self._print(
            "本 TUI 负责看分组、逐组声明、预览批量队列;绝不执行真实提交(红线 24)。"
        )
        self._print("输入 help 查看命令;输入 quit 退出。")
        self._print()

    def _print_table(self, headers: list[str], rows: list[list[str]]) -> None:
        """简易表格:列宽按可见宽度(去 ANSI、CJK/emoji 计 2 列)单遍计算对齐。"""
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

    def _teardown(self) -> None:
        """只关闭自己创建的队列/声明层;注入对象的生命周期归调用方。"""
        if self._owns_queue:
            close_fn = getattr(self._queue, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:  # pragma: no cover - 关闭异常不阻断退出
                    pass
        if self._owns_review and self._review is not None:
            close_fn = getattr(self._review, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:  # pragma: no cover - 关闭异常不阻断退出
                    pass


# ---------------------------------------------------------------------------
# CLI:python -m netsentinel.cli.batch_tui
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m netsentinel.cli.batch_tui",
        description=(
            "净网哨兵 · 批量复核 TUI(V6:分组查看 / 逐组声明 / 批量队列预览;"
            "绝不执行真实提交)"
        ),
    )
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite 队列库路径(默认取配置 db_path:data/review_queue.db)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径(YAML,可选;缺省找 ./config.yaml,再缺省用纯默认配置)",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """CLI 入口:解析 --db / --config 后进入 TUI 主循环,返回退出码。"""
    args = _build_parser().parse_args(argv)
    out = stdout if stdout is not None else sys.stdout
    try:
        cfg = load_config(args.config)
    except ValueError as exc:
        out.write(f"错误:配置加载失败:{exc}\n")
        return 2
    db_path = args.db or cfg.db_path
    tui = BatchTUI(db_path, cfg, stdin=stdin, stdout=stdout)
    return tui.run()


if __name__ == "__main__":
    raise SystemExit(main())
