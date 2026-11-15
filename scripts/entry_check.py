# -*- coding: utf-8 -*-
"""NetSentinel 全入口离线功能体检器(V10.20 · Seat D)。

定位
----
``golive check`` 管的是**配置**(生产开关/密钥/依赖就绪),本工具管的是
**功能**:把 USAGE §1 登记的全部 CLI 入口(``python -m netsentinel`` 三
子命令、batchflow/finishflow/sessionflow、discovery、review_tui /
batch_tui、modelmgr、vlmctl、profile、golive、storage.replay、
ops.audit_verify、ops.scheduler、form_scanner、service.app)与 WebUI
8 页的**导入层**,逐条以子进程真实跑一遍离线最小命令,汇总成一张
可重复的体检表——"跑一遍整个项目"从人工试错变成一条命令。

红线(违反任何一条即设计失败)
----
- **零真实外呼**:所有子命令只触达 ``127.0.0.1``;扫描类条目由体检器
  自起的临时 ``http.server``(随机端口,伺服 ``tests/fixtures/demo_site``,
  跑完即关)供页,绝无对 12377/shdf/搜索引擎/云模型的任何请求;
- **不改真实 data/**:每次运行创建 ``tempfile.TemporaryDirectory`` 作为
  隔离沙箱,生成指向沙箱的 ``config.yaml``(全部库路径入沙箱、
  ``takeover_auto: false`` 杜绝连接向导、``vlm_online/discovery_online:
  false``)并以 ``NETSENTINEL_DATA_DIR`` 注入子进程;真实 ``data/`` 一字
  不动(沙箱随运行结束整体删除);
- **不启动常驻服务**:不占用 8501/8502/8765/8766/8899 等端口;唯一监听
  是体检器自己的临时回环静态服务;子进程环境剥离全部 ``NETSENTINEL_*``
  变量后再注入体检自己的覆盖,避免继承本机密钥/向导配置。

用法::

    python scripts/entry_check.py                 # 跑全量体检
    python scripts/entry_check.py --list          # 只列清单不执行
    python scripts/entry_check.py --only help     # 名称子串过滤(可多次)
    python scripts/entry_check.py --json 报告.json  # 附加导出机器报告

退出码(与项目门禁语义一致):0=全部通过;1=有失败条目;2=自身用法/
配置错误。

已知入口层差异(如实登记,不替入口修 bug):
- USAGE §1 的 ``python -m netsentinel.profile`` 实际模块是
  ``netsentinel.submit.profile``(文档漂移,体检按可运行形式收录);
- ``service.app`` 的 ``main()`` 忽略 argv(``--help`` 会直接起 uvicorn
  绑 8765),体检改用导入层探针(``create_app``)覆盖同一代码路径,
  绝不触碰真实端口。
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence

#: 项目根目录(脚本位于 <root>/scripts/)
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 供沙箱工件构造复用项目自身模块(只读调用)
    sys.path.insert(0, str(ROOT))

#: 隔离沙箱里自建的演示站点词表(体检器自有,绝不读真实 <data>/keywords/)
_KEYWORDS_TEXT = "体检示例词甲\n体检示例词乙\n体检示例词丙\n"

#: 单条目默认超时秒数(总时长上限保护:逐条限时,超时按失败收账)
DEFAULT_TIMEOUT_S = 120.0

#: 扫描类条目较重,单独放宽
SCAN_TIMEOUT_S = 240.0

#: WebUI 页面模块名(USAGE §1:streamlit 9 页;体检用导入层探针)
_WEBUI_PAGES: tuple[str, ...] = (
    "app",
    "assistant_page",
    "dashboard",
    "draft_flow_page",
    "groups_page",
    "keywords_page",
    "label_page",
    "models_page",
    "providers_page",
    "runs_page",
)

#: 退出码语义(与项目门禁一致)
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


# ---------------------------------------------------------------------------
# 体检清单(硬编码 curated,与 docs/USAGE.md §1 入口表对齐)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CheckEntry:
    """单条体检条目。

    - ``argv``:命令模板(以 ``sys.executable`` 为 ``python``);占位符在
      运行时格式化,可用键见 :class:`Sandbox`;
    - ``expect``:可接受的退出码集合(超出一律判败);
    - ``stdin``:喂给子进程的标准输入(TUI 类条目用命令脚本驱动后退出);
    - ``needs_http``:需要体检器自起的临时 fixtures 静态服务;
    - ``note``:一行用途/红线说明(进 ``--list`` 与 JSON 报告)。
    """

    name: str
    argv: tuple[str, ...]
    expect: frozenset[int] = frozenset({0})
    timeout: float = DEFAULT_TIMEOUT_S
    stdin: str | None = None
    needs_http: bool = False
    note: str = ""


def _cli_entries() -> list[CheckEntry]:
    """CLI 入口清单(USAGE §1 主表 + 低频维护入口)。

    注意:``--config`` 一律独立成 token(且在须前置的入口放子命令之前),
    否则子进程 argparse 收到的是单个 "--config 路径" 串,必判用法错误。
    """
    return [
        CheckEntry(
            name="main_help",
            argv=("-m", "netsentinel", "--help"),
            note="主入口 argparse 组装(三子命令总览,--help 不触网不落盘)",
        ),
        CheckEntry(
            name="scan_local_fixture",
            argv=(
                "-m", "netsentinel", "--config", "{config}", "scan",
                "--url", "http://127.0.0.1:{port}/index.html",
            ),
            timeout=SCAN_TIMEOUT_S,
            needs_http=True,
            note="端到端扫描:临时服务伺服 tests/fixtures/demo_site,stub 分类器,零外呼",
        ),
        CheckEntry(
            name="queue_list",
            argv=("-m", "netsentinel", "--config", "{config}", "queue", "list"),
            note="复核队列 CLI(空库列出,--config 须在子命令之前)",
        ),
        CheckEntry(
            name="submit_help",
            argv=("-m", "netsentinel", "submit", "--help"),
            note="提交举报参数面(人工门旗标;--help 在配置加载前退出)",
        ),
        CheckEntry(
            name="batchflow_help",
            argv=("-m", "netsentinel.batchflow", "--help"),
            note="批量筛选入口 argparse 组装",
        ),
        CheckEntry(
            name="finishflow_help",
            argv=("-m", "netsentinel.finishflow", "--help"),
            note="收官汇总入口 argparse 组装",
        ),
        CheckEntry(
            name="sessionflow_status",
            argv=("-m", "netsentinel.sessionflow", "--status", "--config", "{config}"),
            note="会话累积 --status(空累积现状,零扫描零提交)",
        ),
        CheckEntry(
            name="discovery_offline_check",
            argv=(
                "-m", "netsentinel.discovery",
                "--engine", "mock", "--offline-check",
                "--keywords-file", "{keywords}",
                "--out", "{leads}",
                "--config", "{config}",
            ),
            note="线索发现离线体检(词表为体检器自建 tmp 文件,零查询)",
        ),
        CheckEntry(
            name="review_tui_list",
            argv=("-m", "netsentinel.cli.review_tui", "--db", "{db}"),
            stdin="list\nquit\n",
            note="复核 TUI:list 后 quit(空库;EOF/quit 恒退 0,绝不自动批准)",
        ),
        CheckEntry(
            name="batch_tui_groups",
            argv=("-m", "netsentinel.cli.batch_tui", "--db", "{db}", "--config", "{config}"),
            stdin="groups\nquit\n",
            note="批量 TUI:groups 后 quit(分组查看,绝不真实提交)",
        ),
        CheckEntry(
            name="modelmgr_status",
            argv=("-m", "netsentinel.modelmgr", "--config", "{config}", "status"),
            note="模型管理 status(仅回环探测 local_probe_ports,零外呼)",
        ),
        CheckEntry(
            name="vlmctl_list",
            argv=("-m", "netsentinel.vision.vlmctl", "--config", "{config}", "list"),
            note="VLM 提供方总表(零外呼命令,密钥只显已配置/未配置)",
        ),
        CheckEntry(
            name="profile_show",
            argv=("-m", "netsentinel.submit.profile", "show"),
            note="个人模板 show(只读;实际模块名 netsentinel.submit.profile,见文件头说明)",
        ),
        CheckEntry(
            name="golive_check_offline",
            argv=("-m", "netsentinel.golive", "check", "--config", "{config}"),
            note="上线体检离线模式(不带 --net,不触外网)",
        ),
        CheckEntry(
            name="storage_replay_empty",
            argv=("-m", "netsentinel.storage.replay", "{ledger}", "{queue}"),
            note="事件账本重放对账(沙箱独立空账本 vs 空队列,应判一致)",
        ),
        CheckEntry(
            name="audit_verify_minimal",
            argv=("-m", "netsentinel.ops.audit_verify", "{audit}"),
            note="审计哈希链校验(沙箱自建最小合法审计卷,应整卷通过)",
        ),
        CheckEntry(
            name="scheduler_once_empty",
            argv=("-m", "netsentinel.ops.scheduler", "--once", "--config", "{config}"),
            note="定期巡查 --once(沙箱空 watchlist,零目标零扫描)",
        ),
        CheckEntry(
            name="form_scanner_local_fixture",
            argv=(
                "-m", "netsentinel.submit.form_scanner",
                "{root}/tests/fixtures/mini_form.html",
                "--out", "{draft}",
            ),
            note="门户表单扫描(离线解析本地 fixture,草稿只写沙箱)",
        ),
    ]


def _webui_entries() -> list[CheckEntry]:
    """WebUI 9 页导入层探针(streamlit 页面不依赖会话即可 import)。"""
    return [
        CheckEntry(
            name=f"webui_import_{page}",
            argv=("-c", f"import webui.{page}; print('webui.{page} 导入成功')"),
            note=f"WebUI 页面导入层:webui.{page}(不渲染不监听)",
        )
        for page in _WEBUI_PAGES
    ]


def _service_entry() -> list[CheckEntry]:
    """REST 服务入口探针。

    ``service.app:main`` 忽略 argv(``--help`` 会真起 uvicorn 绑 8765,
    与本机 LIVE 服务冲突),故体检用 ``create_app`` 导入层探针覆盖同一
    应用工厂代码路径(含模型路由挂载),绝不绑定任何端口。
    """
    return [
        CheckEntry(
            name="service_app_factory",
            argv=(
                "-c",
                "from service.app import create_app; "
                "from netsentinel.contracts import Config; "
                "app = create_app(Config()); "
                "print('create_app 装配成功, 路由数=', len(app.routes))",
            ),
            note="REST 服务应用工厂(create_app 离线装配,--help 会起服务故不直跑 main)",
        ),
    ]


def build_checks() -> list[CheckEntry]:
    """全量 curated 清单(CLI 18 条 + WebUI 9 条 + service 1 条)。"""
    return _cli_entries() + _webui_entries() + _service_entry()


# ---------------------------------------------------------------------------
# 隔离沙箱
# ---------------------------------------------------------------------------
@dataclass
class Sandbox:
    """一次性隔离沙箱:tmp 数据目录 + 隔离 config.yaml + 各条目所需工件。

    占位符键(供 :attr:`CheckEntry.argv` 格式化):``config`` / ``data`` /
    ``db`` / ``audit`` / ``ledger`` / ``queue`` / ``keywords`` / ``leads`` /
    ``draft`` / ``root`` / ``port``。
    """

    root: Path  # 项目根(子进程 cwd)
    dir: Path = field(init=False)  # 沙箱目录
    placeholders: dict[str, str] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="netsentinel_entrycheck_"))
        data = self.dir / "data"
        (data / "logs").mkdir(parents=True, exist_ok=True)
        self._write_config(data)
        self._seed_model_runtime(data)
        self._write_watchlist()
        self._write_keywords()
        self._write_leads_target()
        self._build_artifacts(data)

    # -- 工件构造 ----------------------------------------------------------

    def _write_config(self, data: Path) -> None:
        """生成隔离 config.yaml:全部落盘路径入沙箱,危险开关全关。

        - ``takeover_auto/onboarding_auto_open`` 关:杜绝自动接管与连接
          向导(向导会绑 8766,违反"不启动常驻服务"红线);
        - ``vlm_online/discovery_online/allow_network`` 关 + ``dry_run_default``
          开:与项目安全默认一致,双保险零外呼。
        """
        cfg = {
            "data_dir": str(data),
            "evidence_dir": str(data / "evidence"),
            "db_path": str(data / "review_queue.db"),
            "audit_path": str(data / "audit.jsonl"),
            "log_path": str(data / "logs" / "netsentinel.log"),
            "vlm_cache_db": str(data / "vlm_cache.db"),
            "phash_db": str(data / "phash.db"),
            "graph_db": str(data / "graph.db"),
            "discovery_cache_db": str(data / "discovery_cache.db"),
            "agent_task_db": str(data / "agent_tasks.db"),
            "model_runtime_path": str(data / "model_runtime.json"),
            "watchlist_path": str(self.dir / "watchlist.yaml"),
            "policy_path": str(self.dir / "policy.yaml"),
            "takeover_auto": False,
            "onboarding_auto_open": False,
            "vlm_online": False,
            "discovery_online": False,
            "allow_network": False,
            "dry_run_default": True,
        }
        path = self.dir / "config.yaml"
        try:
            import yaml  # PyYAML(项目既有依赖,零新增)

            path.write_text(
                yaml.safe_dump(cfg, allow_unicode=True, sort_keys=True),
                encoding="utf-8",
            )
        except ImportError:  # pragma: no cover - PyYAML 缺席时退 JSON 兼容形态
            path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        self.placeholders["config"] = str(path)

    def _seed_model_runtime(self, data: Path) -> None:
        """预置活动模型 spec(stub):即使某入口漏读隔离配置,接管四级
        决策也停在 kept(零探测、零向导),兜底保护 LIVE 端口。"""
        target = data / "model_runtime.json"
        target.write_text(
            json.dumps({"spec": "stub"}, ensure_ascii=False), encoding="utf-8"
        )
        self.placeholders["model_runtime"] = str(target)

    def _write_watchlist(self) -> None:
        """空 watchlist(空文件 = 零巡查目标,scheduler --once 走空轮)。"""
        path = self.dir / "watchlist.yaml"
        path.write_text("", encoding="utf-8")
        self.placeholders["watchlist"] = str(path)

    def _write_keywords(self) -> None:
        """体检器自建 tmp 词表(绝不读真实 <data>/keywords/ 活文件)。"""
        path = self.dir / "keywords.txt"
        path.write_text(_KEYWORDS_TEXT, encoding="utf-8")
        self.placeholders["keywords"] = str(path)

    def _write_leads_target(self) -> None:
        """discovery --out 落点(沙箱内,不写项目根)。"""
        self.placeholders["leads"] = str(self.dir / "leads.txt")

    def _build_artifacts(self, data: Path) -> None:
        """借项目自身 API 构造最小合法工件(空事件账本/空队列/最小审计卷)。

        ``replay`` 条目使用**独立的**空账本+空队列对(不与 ``db`` 共用:
        scan 条目会向共用队列入列,混用会让重放对账出现"账本缺失"差异,
        那是体检器自伤而不是入口缺陷)。
        """
        db = data / "review_queue.db"
        replay_dir = self.dir / "replay"
        replay_dir.mkdir(parents=True, exist_ok=True)
        replay_ledger = replay_dir / "ledger.db"
        replay_queue = replay_dir / "queue.db"
        audit = data / "audit.jsonl"
        try:
            from netsentinel.decision.review_queue import ReviewQueue
            from netsentinel.logging_util import JsonlAuditLogger
            from netsentinel.storage.event_log import EventLog

            for db_path in (db, replay_queue):
                queue = ReviewQueue(str(db_path))
                close = getattr(queue, "close", None)
                if callable(close):
                    close()
            log = EventLog(str(replay_ledger))
            log.close()
            audit_logger = JsonlAuditLogger(str(audit))
            audit_logger.log_event("entry_check", source="sandbox")
            audit_logger.close()
        except Exception as exc:  # pragma: no cover - 兄弟缺席时如实暴露
            raise RuntimeError(f"沙箱工件构造失败(项目模块异常):{exc}") from exc
        self.placeholders.update(
            db=str(db),
            queue=str(replay_queue),
            ledger=str(replay_ledger),
            audit=str(audit),
        )

    # -- 公共 --------------------------------------------------------------

    def base_env(self) -> dict[str, str]:
        """子进程环境:剥离全部 NETSENTINEL_*(本机密钥/向导残留)后注入
        体检自己的覆盖(数据目录隔离 + 禁弹浏览器 + UTF-8 输出)。"""
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("NETSENTINEL_")
        }
        env.update(
            # V10.21 起 load_config 全链路认 NETSENTINEL_DATA_DIR(env>文件>默认):
            # 注入 <沙箱>/data 与沙箱 config.yaml 的 data_dir 布局完全对齐
            # (此前 env 直指沙箱根,产物会从 <沙箱>/data/ 平移到 <沙箱>/)。
            NETSENTINEL_DATA_DIR=str(Path(self.dir) / "data"),
            NETSENTINEL_NO_BROWSER="1",          # 双保险:任何路径都不弹浏览器
            PYTHONIOENCODING="utf-8",
            PYTHONDONTWRITEBYTECODE="1",
        )
        return env

    def expand(self, argv: Sequence[str], port: int | None) -> list[str]:
        """把 argv 模板格式化为可执行命令(注入沙箱占位符与端口)。"""
        mapping = dict(self.placeholders)
        mapping.update(
            data=str(self.dir / "data"),
            draft=str(self.dir / "portal_draft.yaml"),
            root=str(self.root),
            port=str(port if port is not None else 0),
        )
        return [part.format(**mapping) for part in argv]

    def cleanup(self) -> None:
        """删除整个沙箱目录(失败容忍:残留 tmp 目录不阻断报告)。"""
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 临时 fixtures 静态服务(唯一监听,回环 + 随机端口,跑完即关)
# ---------------------------------------------------------------------------
class FixtureServer:
    """伺服 ``tests/fixtures/demo_site`` 的临时 HTTP 服务(127.0.0.1 随机端口)。"""

    def __init__(self, root: Path) -> None:
        handler = partial(
            SimpleHTTPRequestHandler, directory=str(root / "tests" / "fixtures" / "demo_site")
        )
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port: int = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 执行与报告
# ---------------------------------------------------------------------------
@dataclass
class RunResult:
    """单条目执行结果(报告表与 JSON 报告共源)。"""

    name: str
    ok: bool
    exit_code: int | None
    expected: list[int]
    timeout: bool
    seconds: float
    tail: list[str]
    argv: list[str]
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _tail_lines(stdout: str, stderr: str, count: int = 3, width: int = 160) -> list[str]:
    """合并 stdout+stderr 取尾部 ``count`` 行(每行截断,便于表格排版)。"""
    lines = [ln for ln in (stdout + ("\n" + stderr if stderr else "")).splitlines() if ln.strip()]
    return [ln[:width] for ln in lines[-count:]]


def run_entry(entry: CheckEntry, sandbox: Sandbox, port: int | None) -> RunResult:
    """跑单条条目:子进程真实执行,按退出码集合 + 超时分类。"""
    argv = [sys.executable, *sandbox.expand(entry.argv, port)]
    started = time.perf_counter()
    timed_out = False
    try:
        proc = subprocess.run(
            argv,
            cwd=str(sandbox.root),
            env=sandbox.base_env(),
            input=entry.stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=entry.timeout,
        )
        code: int | None = int(proc.returncode)
        tail = _tail_lines(proc.stdout or "", proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        code = None
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        tail = [f"[超时] 超过 {entry.timeout:.0f}s 被终止", *_tail_lines(stdout, stderr, count=2)]
    seconds = time.perf_counter() - started
    ok = (not timed_out) and code in set(entry.expect)
    return RunResult(
        name=entry.name,
        ok=bool(ok),
        exit_code=code,
        expected=sorted(set(entry.expect)),
        timeout=timed_out,
        seconds=round(seconds, 2),
        tail=tail,
        argv=argv,
        note=entry.note,
    )


def _render_table(results: list[RunResult]) -> str:
    """中文汇总表:名称/结果/耗时/退出码/尾部输出。"""
    name_w = max([len(r.name) for r in results] + [12])
    rows = [
        f"  {'名称'.ljust(name_w)}  结果   耗时(s)  退出码  尾部输出",
        f"  {'-' * name_w}  -----  -------  ------  " + "-" * 60,
    ]
    for r in results:
        mark = "✅" if r.ok else "❌"
        code = "超时" if r.timeout else (str(r.exit_code) if r.exit_code is not None else "?")
        tail = " | ".join(r.tail) if r.tail else "(无输出)"
        rows.append(
            f"  {r.name.ljust(name_w)}  {mark}   {r.seconds:7.2f}  {code:>4}   {tail}"
        )
    return "\n".join(rows)


def _render_report(results: list[RunResult], total_s: float, sandbox_dir: str) -> str:
    """完整中文报告(头部红线提示 + 汇总表 + 失败明细)。"""
    passed = sum(1 for r in results if r.ok)
    failed = len(results) - passed
    head = [
        "NetSentinel 全入口离线功能体检",
        "  红线:零真实外呼(仅 127.0.0.1)/ 不改真实 data/(隔离沙箱:"
        f"{sandbox_dir})/ 不启动常驻服务(唯一监听为体检器临时静态服务)",
        f"  条目 {len(results)} 项:通过 {passed},失败 {failed},总耗时 {total_s:.1f}s",
        "",
    ]
    body = [_render_table(results)]
    if failed:
        detail = ["", "失败条目明细:"]
        for r in results:
            if not r.ok:
                expect = "/".join(str(c) for c in r.expected)
                got = "超时" if r.timeout else str(r.exit_code)
                detail.append(f"  ❌ {r.name}:期望退出码 {expect},实际 {got}")
                detail.append(f"     命令:{' '.join(r.argv)}")
                for ln in r.tail:
                    detail.append(f"     输出:{ln}")
        body.extend(detail)
    return "\n".join(head + body)


def _write_json_report(path: str, results: list[RunResult], total_s: float) -> int:
    """导出机器可读 JSON 报告;返回 0 成功 / 2 写入失败(自身错误)。"""
    payload = {
        "tool": "netsentinel.entry_check",
        "version": 1,
        "finished_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "total_seconds": round(total_s, 2),
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r.ok),
            "failed": sum(1 for r in results if not r.ok),
        },
        "results": [r.as_dict() for r in results],
    }
    try:
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        print(f"[entry_check] JSON 报告写入失败({path}):{exc}", file=sys.stderr)
        return EXIT_USAGE
    print(f"JSON 报告已写入:{path}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# CLI
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


def build_parser() -> argparse.ArgumentParser:
    """体检器自身的 argparse(中文帮助)。"""
    parser = argparse.ArgumentParser(
        prog="python scripts/entry_check.py",
        description=(
            "NetSentinel 全入口离线功能体检器:对 USAGE §1 全部 CLI 入口与"
            " WebUI 8 页导入层逐条跑离线最小命令(隔离沙箱 + 临时本地静态"
            "服务,零真实外呼、不改真实 data/、不启动常驻服务)"
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="只打印体检清单(名称/命令模板/预期退出码/超时/说明),不执行",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="子串",
        help="只跑名称含该子串的条目(可多次给出,并集;大小写不敏感)",
    )
    parser.add_argument(
        "--json",
        default=None,
        metavar="路径",
        help="附加导出机器可读 JSON 报告(不影响人读报告与退出码)",
    )
    return parser


def _filter_checks(checks: list[CheckEntry], needles: list[str]) -> list[CheckEntry]:
    """按名称子串并集过滤;一个都不命中视为用法错误(由调用方转退出码 2)。"""
    low = [n.lower() for n in needles]
    return [c for c in checks if any(n in c.name.lower() for n in low)]


def main(argv: Sequence[str] | None = None, *, checks: list[CheckEntry] | None = None) -> int:
    """体检器入口:返回 0=全部通过 / 1=有失败 / 2=自身用法或配置错误。

    ``checks`` 仅供测试注入假清单;缺省用 :func:`build_checks` 全量清单。
    """
    _ensure_utf8_stdio()
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    all_checks = build_checks() if checks is None else checks

    names = [c.name for c in all_checks]
    if len(names) != len(set(names)):
        dup = sorted({n for n in names if names.count(n) > 1})
        print(f"[entry_check] 自身配置错误:清单存在重复名称:{dup}", file=sys.stderr)
        return EXIT_USAGE

    if args.list:
        print(f"体检清单(共 {len(all_checks)} 条;单条默认超时 {DEFAULT_TIMEOUT_S:.0f}s):")
        for c in all_checks:
            expect = "/".join(str(x) for x in sorted(set(c.expect)))
            flags = []
            if c.needs_http:
                flags.append("需本地服务")
            if c.stdin:
                flags.append("喂 stdin")
            suffix = f"({','.join(flags)})" if flags else ""
            print(f"  - {c.name:<28} 期望退出码 {expect:<6} 超时 {c.timeout:.0f}s {suffix}")
            print(f"      命令:python {' '.join(c.argv)}")
            if c.note:
                print(f"      说明:{c.note}")
        return EXIT_OK

    selected = _filter_checks(all_checks, args.only) if args.only else all_checks
    if not selected:
        print(
            f"[entry_check] 用法错误:--only {args.only!r} 未命中任何条目"
            f"(可先用 --list 查看全部名称)",
            file=sys.stderr,
        )
        return EXIT_USAGE

    sandbox = Sandbox(ROOT)
    server: FixtureServer | None = None
    if any(c.needs_http for c in selected):
        server = FixtureServer(ROOT)
        server.start()
    results: list[RunResult] = []
    started = time.perf_counter()
    try:
        for entry in selected:
            print(f"[entry_check] 运行:{entry.name} ...", flush=True)
            results.append(run_entry(entry, sandbox, server.port if server else None))
    finally:
        if server is not None:
            server.close()
        sandbox.cleanup()
    total_s = time.perf_counter() - started

    print()
    print(_render_report(results, total_s, str(sandbox.dir)))

    rc = EXIT_OK if all(r.ok for r in results) else EXIT_FAIL
    if args.json:
        rc_json = _write_json_report(args.json, results, total_s)
        if rc_json != EXIT_OK:
            return rc_json
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
