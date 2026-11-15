"""净网哨兵 NetSentinel 本机 REST 服务层(A31)。

把第一轮能力 API 化:扫描(后台任务)、人工复核队列读写、举报计划生成。
fastapi / uvicorn 均为**惰性导入**(可选依赖 ``pip install '.[api]'``),
兄弟模块(orchestrator / review_queue / portal_* / playbook_gen)一律函数内导入。

安全红线(违反即缺陷,必须遵守):
1. 本服务**只生成举报计划与预览,绝不提供任何真实提交端点**;
   实际提交只能走 CLI(``netsentinel submit --id ...``),在人工门
   (HUMAN_GATE:人工核对 / 人工上传证据包 / 人工输入验证码)之下完成。
2. 默认绑定 ``127.0.0.1``(取 ``cfg.service_host``),**无鉴权,仅限本机使用**,
   严禁暴露到不可信网络;绑定非回环地址时启动告警。
3. 扫描任务在后台线程执行,任务状态仅存内存(dict + Lock),
   服务重启即失,不做持久化承诺。

启动方式::

    python -m service.app            # 读取 ./config.yaml(缺省全默认),监听 127.0.0.1:8765

V5 升级(A101):
- 性能:后台扫描从"每请求新建 daemon 线程"改为**进程内复用线程池**
  (:data:`SCAN_MAX_WORKERS` = 2,经 :func:`_get_scan_pool` 惰性单例获取);
  jobs 的完成语义不变——POST /scan 立即返回 running,执行体结束后写回
  done / failed;超出 2 路的并发扫描在池内排队,状态照常先 running;
- 可观测性:``telemetry.inc("service.scan_requested")``(请求受理)、
  ``telemetry.inc("service.plan_generated")``(计划生成成功)、
  ``telemetry.inc("service.scan_errors")``(任务失败)、
  ``telemetry.timer("service.scan.duration")``(单任务耗时);
- 质量:线程池大小提为模块常量 :data:`SCAN_MAX_WORKERS`,补 docstring 示例。

A203 trace 贯通(telemetry_trace 第三波接线,只增不改既有语义):
- POST /scan 入口 ``telemetry_trace.new_trace()`` 生成 trace_id:**HTTP 响应头
  ``X-Trace-Id``** 返回给调用方、任务 dict 记 ``trace_id`` 字段;提交线程
  池任务时经 ``propagate()`` 拍快照,worker 首步 ``restore()`` 把 trace
  上下文带进线程池(显式 API,无隐式线程注入);
- 阶段 span:``scan.submit``(受理:登记任务 + 池提交)→ ``scan.execute``
  (worker 执行体,异常记入 span 后照常收敛 job failed);任务终态在 span
  关闭**之后**写回——/jobs 见到 done/failed 时 /trace 树已是终态;
- 新增只读端点 ``GET /trace/{job_id}``:按任务 dict 记录的 trace_id 返回
  ``export_trace_json`` 的嵌套 span 树;任务不存在 404,任务无 trace 时
  返回空树 200(绝不误读当前环境的 trace);
- audit_sink 惰性接线:服务启动时 ``configure(audit_sink=...)`` 把 span
  双写接到 ``JsonlAuditLogger(cfg.audit_path)`` 的 ``log_event``——
  logging_util **惰性导入**、审计器**首个 span 落点才构造**(不扫描不建
  目录不碰盘);写入异常已被 telemetry_trace 安全吞掉(计数
  ``trace.audit.error``),扫描业务不受影响;
- 总开关 :data:`TRACE_ENABLED`(默认开;或 ``create_app(..., trace_enabled=
  False)`` / monkeypatch 模块常量):关闭时不开 trace、不加响应头、不接
  audit_sink,与接线前行为完全一致(向后兼容红线)。

A233 收口(门户动态化):POST /plan 的门户清单改为惰性动态发现
(:func:`_list_valid_portals` → ``portal_defs.list_portals``;失败回退内置
12377/shdf 并 warning),计划生成切换 ``portal_defs.build_plan_from_def``
(:func:`_build_report_plan`;与 v1 planner 语义等价由 tests/test_portal_defs.py
一致性用例锁定)——动态门户(portals/*.yaml 显式 enabled: true)自动进入
REST /plan,服务仍**只生成计划与预览,不提供任何真实提交端点**。

V10.26 净网助手统一工作台(席位A:REST 服务端后台化)——支撑"所有操作
后台进行,只看净网助手页面":
- ``POST /discover``:后台**无头**整轮线索发现(worker 内经
  :func:`_patch_environ` 移除 ``NETSENTINEL_YANDEX_HTML_HEADED``),线索
  统一落 ``<data>/leads.txt``;整轮带 errors 也算 done(result 如实上抛),
  异常收敛 failed(中文 error,验证码失败即走此路);
- ``GET /leads``:只读线索清单(跳 ``#`` 注释行,附 1 起始文件行号);
- ``POST /scan-lead``:按线索提交扫描(与 /scan 完全同机制,复用
  :func:`_submit_scan_job` 受理/线程池/trace;可选 line 留痕);
- ``POST /captcha/session``:**用户显式点击才触发**的一次性可见验证窗口:
  后台 subprocess 有头跑一次发现 CLI(env 显式置 HEADED=1 +
  DATA_DIR 绝对化,**父进程环境不动**),result 含 returncode/stdout_tail,
  0 = 验证完成;
- ``GET /assistant/status``:工作台首屏状态总览(密钥文件/会话目录/关键词
  数/队列统计/三配置项),全本地只读、零外呼、零副作用(队列统计走
  只读连接,库缺席绝不建库)。
发现/验证码任务复用既有 jobs 字典/锁/线程池机制(受理即 ``queued``,
worker 起步置 ``running``,终态 done/failed 与 /scan 同语义)。

测试约定:模块级 :func:`_get_run_scan` 是扫描执行函数的获取缝,
``monkeypatch.setattr(service.app, "_get_run_scan", lambda: fake)`` 即可
离线替换 orchestrator.run_scan,不发起任何网络请求;
:func:`_get_scan_pool` 是线程池的获取缝,同理可注入替身。
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import dataclasses
import datetime as _dt
import logging
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from netsentinel import telemetry
from netsentinel import telemetry_trace as tt
from netsentinel.contracts import Config, SiteReport
from netsentinel.storage.kernel import connect as kernel_connect

__all__ = ["create_app", "main", "SCAN_MAX_WORKERS", "TRACE_ENABLED"]

logger = logging.getLogger(__name__)

#: 服务版本(A31 属 V2 升级批次)。
SERVICE_VERSION = "v2"

#: 计划预览统一声明(红线:本服务不执行提交)。
PLAN_NOTICE = "本服务不执行提交;请用 CLI 在人工门下执行"

#: 举报门户清单兜底(A233:动态发现失败时的回退,与 contracts.Portal 一致;
#: 可用清单以 :func:`_list_valid_portals` 惰性动态发现为准)。
_FALLBACK_PORTALS: tuple[str, ...] = ("12377", "shdf")

#: 本机回环地址集合(绑定非回环时告警)。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: 后台扫描线程池大小(V5:每请求新线程 → 进程内复用池)。
#: 2 路并发上限既复用线程,又天然限制同时扫描的站点数(资源保护)。
#: V10.35(P2-9)档位化后作为**回退缺省**:实际大小经
#: :func:`scan_pool_size(cfg)` 按 CPU 档位取 ``min(io_workers, 上界)``,
#: 档位解析失败或无 cfg 时回到本常量(既有 monkeypatch 语义不变)。
SCAN_MAX_WORKERS: int = 2

#: 扫描池档位化上界(资源保护:服务侧与批量池共用一台机,留余量;
#: mid 档 20 核 = 10,恰好等于提案目标值,再高的档位也钳在此)。
SCAN_POOL_CAP: int = 10

#: A203 trace 贯通总开关(默认开):False 时 /scan 不开 trace、不加
#: X-Trace-Id 响应头、不接 audit_sink——与接线前行为完全一致。
#: 经 ``create_app(..., trace_enabled=False)`` 或 monkeypatch 本常量关闭。
TRACE_ENABLED: bool = True

#: yandex-html 有头模式环境变量名(字面量与 netsentinel.discovery.yandex_html
#: 一致;本模块不 import 该模块,改名以那边测试为准,同 _GUARD_FAMILIES 惯例)。
YANDEX_HTML_HEADED_ENV: str = "NETSENTINEL_YANDEX_HTML_HEADED"

#: 数据目录环境变量名(字面量与 netsentinel.config.DATA_DIR_ENV / webui 一致;
#: 同上,字面量单源在此,不 import 配置层)。
DATA_DIR_ENV: str = "NETSENTINEL_DATA_DIR"

#: 验证码会话子进程超时(秒):人工门内真人输入验证码,给足 30 分钟;
#: 超时按 job failed 收敛(中文 error),子进程由 subprocess.run 的 timeout
#: 参数负责终止。
CAPTCHA_SESSION_TIMEOUT_S: int = 1800

#: /captcha/session 结果里 stdout 尾部保留的字符数(响应有界)。
_STDOUT_TAIL_CHARS: int = 2000

#: 进程环境变量临时增删的互斥锁(V10.26:发现 worker 的无头保证与数据目录
#: 对齐都在共享线程池线程里做,串行化避免互相覆盖恢复值)。
_environ_lock = threading.Lock()

#: 进程内共享的扫描线程池(经 :func:`_get_scan_pool` 惰性创建)。
_scan_pool: concurrent.futures.ThreadPoolExecutor | None = None

#: 供 :func:`_get_scan_pool` 单例构造用的双检锁(V10.18 线程安全审计:
#: FastAPI sync 端点跑在 anyio 线程池里,多个 POST /scan 可并发进入
#: check-then-act 构造序列;加锁后并发下也只构造一个池实例,行为等价)。
_scan_pool_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 兄弟模块惰性获取缝(测试经这些函数 monkeypatch,全离线)
# ---------------------------------------------------------------------------
def _get_run_scan() -> Callable[[str, Config], SiteReport]:
    """获取扫描执行函数(惰性导入 orchestrator;测试经此缝替换)。

    :raises RuntimeError: orchestrator 未就位时给出中文提示。
    """
    try:
        from netsentinel.pipeline import orchestrator
    except Exception as exc:  # noqa: BLE001 - 并行开发期兄弟模块可能未就位
        raise RuntimeError(
            f"扫描依赖 netsentinel.pipeline.orchestrator(A18),当前不可用:{exc}"
        ) from exc
    return orchestrator.run_scan  # type: ignore[return-value]


def _get_queue_cls() -> Any:
    """惰性导入人工复核队列类(标准库 sqlite3 实现)。"""
    try:
        from netsentinel.decision import review_queue
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"复核队列依赖 netsentinel.decision.review_queue(A10),当前不可用:{exc}"
        ) from exc
    return review_queue.ReviewQueue


def _get_discovery_pipeline() -> Any:
    """惰性导入发现流水线模块(V10.26;测试经 :func:`_get_discover` 缝替换)。"""
    try:
        from netsentinel.discovery import pipeline as discovery_pipeline
    except Exception as exc:  # noqa: BLE001 - 并行开发期兄弟模块可能未就位
        raise RuntimeError(
            f"发现依赖 netsentinel.discovery.pipeline,当前不可用:{exc}"
        ) from exc
    return discovery_pipeline


def _get_discover() -> Callable[..., dict[str, Any]]:
    """获取发现执行函数(惰性;测试经此缝注入 fake,零外呼)。"""
    return _get_discovery_pipeline().discover  # type: ignore[no-any-return]


def _get_write_leads() -> Callable[..., int]:
    """获取线索写盘函数(惰性;与 :func:`_get_discover` 同源)。"""
    return _get_discovery_pipeline().write_leads  # type: ignore[no-any-return]


def _get_subprocess() -> Any:
    """获取 subprocess 模块(标准库;测试注入假模块实现零弹窗)。"""
    import subprocess

    return subprocess


def scan_pool_size(cfg: Config | None = None) -> int:
    """V10.35(P2-9)服务扫描池并发档位化:``min(io_workers(cfg), CAP)``。

    - 提案口径:服务池固定 2 与三档并发体系脱节(mid 档批量池 10 路,
      服务池 2 路排队)——现按 ``cfg.concurrency_tier`` 对齐批量档位;
    - 恒钳在 :data:`SCAN_POOL_CAP`(10):服务与批量共用一台机,资源
      保护语义保留(红线 37 精神);
    - ``cfg`` 缺失 / 档位解析失败 / concurrency 模块缺席:回退
      :data:`SCAN_MAX_WORKERS`(=2),行为与档位化前逐字节一致。
    """
    if cfg is None:
        return SCAN_MAX_WORKERS
    try:
        from netsentinel.ops.concurrency import io_workers

        return max(1, min(int(io_workers(cfg)), SCAN_POOL_CAP))
    except Exception as exc:  # noqa: BLE001 - 档位解析失败回退固定缺省
        logger.warning("扫描池档位解析失败,回退 %d 路:%s", SCAN_MAX_WORKERS, exc)
        return SCAN_MAX_WORKERS


def _get_scan_pool(
    max_workers: int | None = None,
) -> concurrent.futures.ThreadPoolExecutor:
    """获取进程内共享的扫描线程池(惰性单例;测试可 monkeypatch 本缝)。

    V5 性能:此前每个 POST /scan 都新建一个 daemon 线程(创建/销毁开销
    与线程数都不设限);现在复用共享池,线程只建一次,超额请求在池内
    排队——jobs 的完成语义完全不变。

    V10.18 线程安全:单例构造加 :data:`_scan_pool_lock` 双检锁(锁内只做
    一次判空与构造);此前并发首扫会各建一个池、后写者胜出(败者泄漏一个
    空闲 executor 对象),加锁后并发与串行路径拿到的是同一个实例,外部
    行为不变(测试仍可 monkeypatch 本缝整体替换)。

    V10.35(P2-9):``max_workers`` 由 :func:`create_app` 按
    :func:`scan_pool_size(cfg)`(CPU 档位)传入;缺省 None = 旧行为
    (:data:`SCAN_MAX_WORKERS` = 2)——无参调用(既有测试/直接调用方)
    与档位化前逐字节一致。
    """
    global _scan_pool
    if _scan_pool is None:
        with _scan_pool_lock:
            if _scan_pool is None:
                _scan_pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=max(1, int(max_workers if max_workers is not None else SCAN_MAX_WORKERS)),
                    thread_name_prefix="netsentinel-scan",
                )
    return _scan_pool


def _open_queue(cfg: Config) -> Any:
    """打开复核队列连接(调用方负责 close,避免跨线程复用同一连接)。"""
    return _get_queue_cls()(cfg.db_path)


def _list_valid_portals() -> list[str]:
    """惰性列出可用的举报门户短名(A233 动态闭环收口)。

    调 ``netsentinel.submit.portal_defs.list_portals()``:内置门户
    (12377/shdf)在前,动态门户(portals/*.yaml 显式 ``enabled: true``)
    按短名排序在后——动态门户由此进入 REST /plan;任何失败(模块/PyYAML
    缺席、定义文件损坏、清单为空)回退 :data:`_FALLBACK_PORTALS` 并记
    warning,绝不抛错(清单问题只降级可用门户,不让端点 500)。
    """
    try:
        from netsentinel.submit import portal_defs

        names = list(portal_defs.list_portals())
    except Exception as exc:  # noqa: BLE001 - 清单失败只降级,不让端点崩溃
        logger.warning("动态门户清单不可用,回退内置门户 %s:%s", _FALLBACK_PORTALS, exc)
        return list(_FALLBACK_PORTALS)
    if not names:
        logger.warning("门户清单为空(portals/ 目录异常),回退内置门户 %s", _FALLBACK_PORTALS)
        return list(_FALLBACK_PORTALS)
    return names


def _build_report_plan(entry_like: Any, portal: str, cfg: Config) -> Any:
    """按门户定义生成举报步骤计划(A233;只生成计划,不执行、不联网)。

    - 门户定义经 ``portal_defs.get_portal`` 取:内置门户豁免加载,动态门户
      须显式 ``enabled: true``(红线:动态发现绝不自动生效未确认门户);
    - payload 由 ``form_models.build_payload`` 构造(与 v1 planner 同源),
      内置门户映射 :class:`Portal` 枚举,动态门户以短名字符串承载
      (Portal 为 str 枚举,playbook/JSON 序列化对两种形态同义);
    - 步骤生成交给 ``portal_defs.build_plan_from_def``——与 v1
      portal_12377 / portal_shdf 的产出语义等价已由 tests/test_portal_defs.py
      的一致性用例锁定(步序/标签/类目/首步 meta/验证码红线全部一致)。

    :raises ValueError: 门户未知或未启用(中文消息,端点转 422)。
    """
    from netsentinel.contracts import Portal
    from netsentinel.submit import form_models, portal_defs

    key = str(portal).strip()
    defn = portal_defs.get_portal(key)  # 内置 → 动态 enabled;未知 → ValueError
    portal_key: Any = {"12377": Portal.P12377, "shdf": Portal.SHDF}.get(key, key)
    payload = form_models.build_payload(entry_like, portal_key, cfg)
    return portal_defs.build_plan_from_def(defn, payload, cfg)


def _render_plan(plan: Any) -> tuple[dict[str, Any], str]:
    """计划 → (JSON dict, 中文 playbook markdown)。"""
    from netsentinel.submit import playbook_gen

    return playbook_gen.plan_to_json(plan), playbook_gen.plan_to_markdown(plan)


def _entry_to_dict(entry_like: Any) -> dict[str, Any]:
    """复核条目 → 可 JSON 序列化 dict(dataclass asdict / 手工兜底)。"""
    if dataclasses.is_dataclass(entry_like) and not isinstance(entry_like, type):
        return dataclasses.asdict(entry_like)
    return {
        "id": int(getattr(entry_like, "id", 0)),
        "site_url": str(getattr(entry_like, "site_url", "")),
        "verdict": str(getattr(entry_like, "verdict", "")),
        "status": str(getattr(entry_like, "status", "")),
        "evidence_zip": str(getattr(entry_like, "evidence_zip", "")),
        "created_at": str(getattr(entry_like, "created_at", "")),
        "updated_at": str(getattr(entry_like, "updated_at", "")),
        "note": str(getattr(entry_like, "note", "")),
    }


def _verdict_value(verdict: Any) -> str:
    """Verdict 枚举或字符串统一为字符串。"""
    value = getattr(verdict, "value", None)
    return str(value) if value is not None else str(verdict or "")


def _max_entry_id_sqlite(db_path: str, site_url: str) -> int:
    """只读 SQLite 连接直查同站点最大条目 id(无匹配 → 0;V10.18 性能)。

    - 只读连接经 :func:`netsentinel.storage.kernel.connect(path,
      readonly=True)`(V10.21 全仓唯一 ``sqlite3.connect`` 工厂;V10.24
      席位3 收编本处 URI 直连):``file:…?mode=ro`` URI,绝不建库/建表/
      写盘,复刻 ``ReviewQueue.list()`` 全量回查的取数语义
      (``site_url`` 精确等值过滤后取 ``MAX(id)``),但把 O(N) 全表物化
      降为单条聚合查询;
    - WAL 一致性:ro 连接读取最近已提交快照(写方连接在场时经 -shm 读
      -wal,干净关闭后 -wal 已 checkpoint 进主库),与旧实现"新开一条
      ReviewQueue 连接再 list()"的快照口径一致;
    - 忙等口径:工厂内置 ``PRAGMA busy_timeout=BUSY_TIMEOUT_MS``(5000,
      与 :data:`netsentinel.decision.review_queue.BUSY_TIMEOUT_MS` 同一
      常量单源),等价于原直连的 ``timeout=5.0``;只读口打不开(库缺席 /
      WAL 待恢复 / entries 表未建)抛 :class:`sqlite3.Error`,由调用方
      回退旧路径。

    :raises sqlite3.Error: 只读连接打开或查询失败(调用方负责回退)。
    """
    conn = kernel_connect(Path(str(db_path)), readonly=True)
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM entries WHERE site_url = ?",
            (site_url,),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0]) if row is not None else 0


def _find_entry_id(site_url: str, cfg: Config) -> int | None:
    """扫描结束后在复核队列里找该站点最新条目的 id(取同站点最大 id)。

    orchestrator.run_scan 不在返回值里携带 entry_id(needs_review 时它把
    条目写进了 cfg.db_path 指向的队列),这里按站点地址回查最新一条;
    队列不可用或查不到时返回 None 并记 warning,不影响扫描结果本身。

    V10.18 性能:回查从"全量 ``queue.list()`` 后 Python 侧取 max(id)"
    改为 :func:`_max_entry_id_sqlite` 只读 SQL ``SELECT COALESCE(MAX(id), 0)``
    (等价性:``site_url`` 精确等值过滤 + MAX 聚合,与旧全量扫描的
    ``max(e.id for e in matches)`` 在任意已提交库状态下结果恒等——
    空表 / 无匹配均得 None,由 tests/test_service_api.py 三态用例锁定);
    只读口打不开的兜底场景(库缺席 / WAL 崩溃残留待恢复 / 表未建)回落
    旧的全量回查路径,行为逐字节等价。
    """
    try:
        try:
            found = _max_entry_id_sqlite(str(cfg.db_path), site_url)
        except sqlite3.Error:
            # 只读口不可用:回落旧全量回查(它会建库/建表并恢复 WAL,
            # 与改造前行为完全一致),其异常统一由外层 except 收敛。
            queue = _open_queue(cfg)
            try:
                matches = [e for e in queue.list() if e.site_url == site_url]
            finally:
                queue.close()
            return max(int(e.id) for e in matches) if matches else None
        return found if found > 0 else None
    except Exception as exc:  # noqa: BLE001 - 回查失败只影响 entry_id 字段
        logger.warning("回查复核队列条目编号失败(site=%s):%s", site_url, exc)
        return None


def _is_http_url(url: str) -> bool:
    """url 必须是 http(s):// 开头且带主机名的完整链接。"""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


# ---------------------------------------------------------------------------
# V10.26 净网助手工作台助手(全本地只读;契约路径与 webui 布局同口径)
# ---------------------------------------------------------------------------
def _data_dir(cfg: Config) -> Path:
    """cfg.data_dir → Path(空值回退默认 ``data``)。"""
    return Path(str(cfg.data_dir or "data"))


def _active_keywords_path(cfg: Config) -> Path:
    """关键词活文件路径 ``<data>/keywords/active.txt``(与 webui 关键词页同布局)。"""
    return _data_dir(cfg) / "keywords" / "active.txt"


def _leads_path(cfg: Config) -> Path:
    """线索清单路径 ``<data>/leads.txt``(发现轮与验证码会话的统一落点)。"""
    return _data_dir(cfg) / "leads.txt"


def _yandex_profile_path(cfg: Config) -> Path:
    """yandex-html 浏览器会话目录 ``<data>/browser_profiles/yandex_html``。"""
    return _data_dir(cfg) / "browser_profiles" / "yandex_html"


def _glm_key_path() -> Path:
    """GLM 密钥文件路径 ``~/.netsentinel/keys/glm``(只查存在性,零外呼)。"""
    return Path.home() / ".netsentinel" / "keys" / "glm"


def _parse_keyword_lines(lines: Iterable[str]) -> list[str]:
    """逐行解析关键词:strip、跳空行/#注释、去重保序。

    与 :func:`netsentinel.discovery.keywords.load_keywords` 的列表口径一致
    (该函数是唯一的装载纪律单源;此处仅按行同口径解析,**不 import webui**,
    等价于 webui.keywords_page.read_active + load_keywords 的组合)。
    """
    kept: list[str] = []
    for ln in lines:
        word = ln.strip()
        if not word or word.startswith("#") or word in kept:
            continue
        kept.append(word)
    return kept


def _read_active_keywords(cfg: Config) -> list[str]:
    """读关键词活文件 → 有效词列表;缺失/坏文件 → ``[]``。

    与 webui.keywords_page.read_active 同口径:不存在 → 空,读失败(OSError)
    → 空;文本按行解析(:func:`_parse_keyword_lines`)。
    """
    path = _active_keywords_path(cfg)
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:  # noqa: BLE001 - 坏文件按未配置处理
        return []
    return _parse_keyword_lines(text.splitlines())


@contextlib.contextmanager
def _patch_environ(overrides: dict[str, str | None]) -> Iterator[None]:
    """临时设置/删除进程环境变量(值 None = 删除),退出恢复原值。

    V10.26 发现 worker 的无头保证:进入前移除 ``NETSENTINEL_YANDEX_HTML_HEADED``
    (即使操作者在服务进程环境里设置了它,后台发现也一律无头——可见验证
    窗口只允许出现在 ``POST /captcha/session`` 的子进程里),并把
    ``NETSENTINEL_DATA_DIR`` 对齐到 cfg.data_dir 的绝对路径(yandex-html 的
    会话目录按该环境变量取位);退出时恢复,不影响后续请求与父进程环境。
    """
    with _environ_lock:
        saved: dict[str, str | None] = {}
        try:
            for key, value in overrides.items():
                saved[key] = os.environ.get(key)
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            yield
        finally:
            for key, old in saved.items():
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old


def _stdout_tail(text: Any, limit: int = _STDOUT_TAIL_CHARS) -> str:
    """子进程 stdout 的尾部截断(None 安全,响应有界)。"""
    tail = str(text or "").strip()
    return tail[-limit:]


def _format_discovery_errors(errors: Any) -> list[str]:
    """discover() 的 errors((查询词, 中文原因) 元组表)→ 中文字符串表。"""
    out: list[str] = []
    for item in errors or []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            out.append(f"{item[0]} —— {item[1]}")
        else:
            out.append(str(item))
    return out


def _queue_summary_readonly(cfg: Config) -> dict[str, int]:
    """只读统计复核队列四状态计数(与 GET /queue 的 summary 同款口径)。

    经 :func:`kernel_connect(readonly=True)` 的 ``mode=ro`` URI 连接——库
    文件缺席/打不开时**不会误建空库**(与 /queue 的读写连接不同,本函数
    承诺全本地只读零副作用,供零外呼的 /assistant/status 使用);任何
    sqlite3 异常一律收敛为全 0 计数,绝不让端点 500。
    """
    counts = {"pending": 0, "approved": 0, "rejected": 0, "submitted": 0}
    try:
        conn = kernel_connect(Path(str(cfg.db_path)), readonly=True)
    except sqlite3.Error:
        return counts
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM entries GROUP BY status"
        ).fetchall()
    except sqlite3.Error:
        return counts
    finally:
        conn.close()
    for row in rows:
        status = row["status"] if isinstance(row, dict) else row[0]
        count = int(row["n"] if isinstance(row, dict) else row[1])
        if status in counts:
            counts[status] = count
    return counts


def _wire_trace_audit(cfg: Config) -> None:
    """A203:把 span 结束的 audit_sink 惰性接到 ``JsonlAuditLogger(cfg.audit_path)``。

    - 两段惰性:``netsentinel.logging_util`` 惰性导入(可选依赖形态与本模块
      其他兄弟模块一致);审计器在**首个 span 落点**才构造——不扫描则不建
      目录、不碰盘(logging_util API 只读使用,不改其源);
    - 构造加双检锁:span 落点可能来自多个线程(请求线程 + 池 worker),
      不加锁会构造出多个实例、各持各的文件锁,同文件追加将出现撕裂行;
    - 载荷七键 ``(**payload)`` 直接进 ``log_event(event="span", ...)``,
      行内由 logging_util 自动补 ``ts``;
    - 构造/写入的任何异常都会被 telemetry_trace 安全吞掉(计数
      ``trace.audit.error``),扫描业务不受影响——本函数因此无需自设兜底。
    """
    state: dict[str, Any] = {"audit": None}
    state_lock = threading.Lock()

    def _sink(payload: dict[str, Any]) -> None:
        audit = state["audit"]
        if audit is None:
            with state_lock:
                audit = state["audit"]
                if audit is None:
                    from netsentinel.logging_util import JsonlAuditLogger  # 惰性导入

                    audit = JsonlAuditLogger(str(cfg.audit_path))
                    state["audit"] = audit
        audit.log_event(**payload)

    tt.configure(audit_sink=_sink)


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------
def create_app(
    cfg: Config | None = None, *, trace_enabled: bool | None = None
) -> "FastAPI":
    """构建本机 REST 服务(FastAPI 可选依赖,函数内惰性导入)。

    :param cfg: 全局配置;缺省 ``load_config()``(默认找 ``./config.yaml``)。
    :param trace_enabled: A203 trace 贯通开关;None(缺省)取模块常量
        :data:`TRACE_ENABLED`。关闭时不开 trace、不加 ``X-Trace-Id`` 响应头、
        不接 audit_sink,与接线前行为完全一致。
    :return: FastAPI 实例(未启动;由 uvicorn 或 TestClient 驱动)。

    任务存储为进程内 ``dict + threading.Lock``;扫描在共享线程池
    (:func:`_get_scan_pool`,:data:`SCAN_MAX_WORKERS` = 2)中执行,
    经模块级 :func:`_get_run_scan` 取 orchestrator.run_scan(便于测试替换)。

    示例::

        app = create_app(cfg)            # 或 create_app() → load_config()
        with TestClient(app) as client:  # pip install '.[api]'
            assert client.get("/health").json()["ok"] is True
    """
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse

    if trace_enabled is None:
        trace_enabled = TRACE_ENABLED

    if cfg is None:
        from netsentinel.config import load_config

        cfg = load_config()

    if trace_enabled:
        _wire_trace_audit(cfg)  # A203:span 双写 → cfg.audit_path JSONL(惰性)

    def _body_str(body: dict[str, Any] | None, key: str, err: str) -> str:
        """从 JSON 请求体取字符串字段;缺失或类型不符 → 422 中文错误。

        不用函数内定义的 Pydantic 模型:模块启用了 ``from __future__ import
        annotations``,FastAPI 解析端点注解时看不到闭包内的局部类名;
        手工校验还能保证 422 错误文案为中文。
        """
        value = (body or {}).get(key)
        if not isinstance(value, str):
            raise HTTPException(status_code=422, detail=err)
        return value.strip()

    # 内存任务存储:job_id -> {"status", "url", "created_at", "result"?, "error"?}
    jobs: dict[str, dict[str, Any]] = {}
    jobs_lock = threading.Lock()

    # V10.35(P2-9):扫描池按 CPU 档位取宽(惰性单例首建时生效;
    # 无参 _get_scan_pool() 旧行为不受影响)
    scan_workers = scan_pool_size(cfg)

    # V10.35(P2-7b):服务关闭时把遥测快照追加落盘(未配置路径=空操作);
    # 失败只告警,绝不影响关闭流程。lifespan 形态(on_event 已弃用)。
    @contextlib.asynccontextmanager
    async def _lifespan(_app: Any) -> Any:
        yield
        path = (getattr(cfg, "telemetry_jsonl_path", "") or "").strip()
        if not path:
            return
        try:
            telemetry.export_jsonl(path)
            telemetry.inc("telemetry.export")
        except Exception as exc:  # noqa: BLE001 - 观测失败不拖垮关闭
            telemetry.inc("telemetry.export.failed")
            logger.warning("服务关闭遥测落盘失败:%s", exc)

    app = FastAPI(
        title="净网哨兵 NetSentinel 本机辅助服务",
        version=SERVICE_VERSION,
        lifespan=_lifespan,
        description=(
            "机器初筛 → 人工复核 → 辅助举报的本机 REST 层。"
            "**仅限本机使用(默认绑定 127.0.0.1,无鉴权)**;"
            "本服务只生成举报计划与预览,**不提供任何真实提交端点**;"
            "实际提交请用 CLI,在人工门(验证码人工输入)之下执行。"
        ),
    )

    # ------------------------------------------------------------------
    # 后台扫描执行体:任何异常都收敛为 job failed(中文 error)
    # ------------------------------------------------------------------
    def _execute_scan(
        job_id: str, url: str, snapshot: tt.TraceSnapshot | None = None
    ) -> None:
        """线程池 worker 执行体(A203:可选 restore 快照后开 scan.execute span)。

        - ``snapshot`` 在场:worker 首步 :func:`tt.restore` 接回该任务的
          trace,再以 ``scan.execute`` span 包住整个执行体(体内异常先由
          span 记 error 再上抛,由 except 收敛为 job failed);
        - ``snapshot`` 为 None(trace 开关关闭):不开 span;并显式把 worker
          上下文清空(restore 空 快照)——共享池线程可能残留早前任务的
          trace 上下文,清空保证**每个无快照任务从干净上下文起步**
          (进程内开关恒定时为幂等空操作,生产行为与接线前一致);
        - 任务终态(done/failed)在 span 关闭**之后**写回:GET /jobs 看到
          终态时 GET /trace 的树必为终态(无半更新窗口)。
        """
        execute_span: contextlib.AbstractContextManager[Any] = contextlib.nullcontext()
        if snapshot is not None:
            try:
                tt.restore(snapshot)
            except Exception as exc:  # noqa: BLE001 - 恢复失败退化为现状路径
                logger.warning(
                    "trace 快照恢复失败,本次任务不开执行段 span:job=%s %s", job_id, exc
                )
            else:
                execute_span = tt.span("scan.execute", attrs={"stage": "execute"})
        else:
            try:
                tt.restore(tt.TraceSnapshot(trace_id=None, span_id=None))
            except Exception:  # noqa: BLE001 - 清空失败不影响扫描(仅上下文卫生)
                logger.debug("清空 worker 残留 trace 上下文失败(忽略):job=%s", job_id)
        result: dict[str, Any]
        try:
            with execute_span:
                run_scan = _get_run_scan()
                with telemetry.timer("service.scan.duration"):
                    report = run_scan(url, cfg)
                needs_review = bool(getattr(report, "needs_review", False))
                entry_id: int | None = (
                    _find_entry_id(url, cfg) if needs_review else None
                )
                result = {
                    "site_url": str(getattr(report, "site_url", url)),
                    "verdict": _verdict_value(getattr(report, "verdict", "")),
                    "agg": float(getattr(report, "agg_nsw_prob", 0.0)),
                    "nsw_image_count": int(getattr(report, "nsw_image_count", 0)),
                    "entry_id": entry_id,
                    "needs_review": needs_review,
                }
        except Exception as exc:  # noqa: BLE001 - 单任务失败不影响服务
            telemetry.inc("service.scan_errors")
            logger.warning("扫描任务失败:job=%s site=%s:%s", job_id, url, exc)
            with jobs_lock:
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["error"] = f"扫描失败:{exc}"
            return
        with jobs_lock:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["result"] = result
        logger.info(
            "扫描任务完成:job=%s site=%s verdict=%s needs_review=%s",
            job_id,
            url,
            result["verdict"],
            result["needs_review"],
        )

    # ------------------------------------------------------------------
    # V10.26 后台发现执行体(净网助手席位A):无头整轮 discover + 线索落盘
    # ------------------------------------------------------------------
    def _execute_discover(
        job_id: str, keywords: list[str], engine: str | None
    ) -> None:
        """线程池 worker:后台**无头**跑一轮 discover(),线索写 ``<data>/leads.txt``。

        - 整轮**带 errors 也算 done**(result.errors 如实上抛给页面提示:
          验证码拦截等单查询失败属业务结果,不是任务失败),只有
          discover()/写盘本身抛异常才收敛 failed(中文 error);
        - 无头保证::func:`_patch_environ` 在体内移除
          ``NETSENTINEL_YANDEX_HTML_HEADED``——可见验证窗口只允许出现在
          ``POST /captcha/session`` 的子进程里(用户显式点击触发);
          ``NETSENTINEL_DATA_DIR`` 同步对齐 cfg.data_dir 绝对路径,
          yandex-html 的会话目录/缓存随 cfg 走;
        - ``engine`` 覆盖经 :func:`dataclasses.replace` 生成 scoped 配置
          (discovery_engine 单字段替换),引擎构造仍由 discover() 内部
          统一完成(缺省 cfg.discovery_engine 时零拷贝)。
        """
        with jobs_lock:
            jobs[job_id]["status"] = "running"
        result: dict[str, Any]
        try:
            scan_cfg = cfg
            if engine is not None and engine != str(cfg.discovery_engine):
                scan_cfg = dataclasses.replace(cfg, discovery_engine=engine)
            effective_engine = str(engine or cfg.discovery_engine)
            with _patch_environ(
                {
                    YANDEX_HTML_HEADED_ENV: None,
                    DATA_DIR_ENV: str(_data_dir(cfg).resolve()),
                }
            ):
                outcome = _get_discover()(list(keywords), scan_cfg)
                out_path = str(_leads_path(cfg))
                urls = [str(u) for u in (outcome.get("urls") or [])]
                _get_write_leads()(urls, out_path, engine_name=effective_engine)
            result = {
                "urls": len(urls),
                "queries": int(outcome.get("queries") or 0),
                "errors": _format_discovery_errors(outcome.get("errors")),
                "out": out_path,
            }
        except Exception as exc:  # noqa: BLE001 - 单任务失败不影响服务
            telemetry.inc("service.discover_errors")
            logger.warning("发现任务失败:job=%s:%s", job_id, exc)
            with jobs_lock:
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["error"] = f"发现失败:{exc}"
            return
        with jobs_lock:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["result"] = result
        logger.info(
            "发现任务完成:job=%s 线索 %d 条 → %s",
            job_id, result["urls"], result["out"],
        )

    # ------------------------------------------------------------------
    # V10.26 验证码会话执行体:一次性可见窗口(用户显式点击才触发)
    # ------------------------------------------------------------------
    def _execute_captcha_session(job_id: str) -> None:
        """线程池 worker:subprocess **有头**跑一次发现 CLI(唯一可见窗口)。

        - 命令行固定 ``[sys.executable, "-m", "netsentinel.discovery",
          "--keywords-file", <data>/keywords/active.txt, "--engine",
          "yandex-html", "--out", <data>/leads.txt]``;
        - 子进程 env **显式传入**(不污染父进程环境,后台任务的无头承诺
          不受影响):``NETSENTINEL_YANDEX_HTML_HEADED=1`` +
          ``NETSENTINEL_DATA_DIR=<cfg.data_dir 绝对化>``;
        - 完成语义:result 含 ``{"returncode": n, "stdout_tail": "..."}``,
          returncode 0 = 验证完成(done);非 0 → failed(中文 error 附
          退出码,result 仍随附供页面展示输出尾部);子进程启动失败/超时
          → failed(中文 error,无 result)。
        """
        with jobs_lock:
            jobs[job_id]["status"] = "running"
        cmd = [
            sys.executable,
            "-m",
            "netsentinel.discovery",
            "--keywords-file",
            str(_active_keywords_path(cfg)),
            "--engine",
            "yandex-html",
            "--out",
            str(_leads_path(cfg)),
        ]
        env = dict(os.environ)
        env[YANDEX_HTML_HEADED_ENV] = "1"
        env[DATA_DIR_ENV] = str(_data_dir(cfg).resolve())
        try:
            proc = _get_subprocess().run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=CAPTCHA_SESSION_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 - 启动失败/超时统一收敛 failed
            telemetry.inc("service.captcha_session_errors")
            if "Timeout" in type(exc).__name__:
                error = (
                    f"验证会话超时:超过 {CAPTCHA_SESSION_TIMEOUT_S // 60} 分钟"
                    "未完成,子进程已终止"
                )
            else:
                error = f"验证会话执行失败:{exc}"
            logger.warning("验证会话失败:job=%s:%s", job_id, error)
            with jobs_lock:
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["error"] = error
            return
        returncode = int(getattr(proc, "returncode", -1))
        stdout_tail = _stdout_tail(getattr(proc, "stdout", ""))
        job_result: dict[str, Any] = {
            "returncode": returncode,
            "stdout_tail": stdout_tail,
        }
        with jobs_lock:
            if returncode == 0:
                jobs[job_id]["status"] = "done"
                jobs[job_id]["result"] = job_result
            else:
                telemetry.inc("service.captcha_session_errors")
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["error"] = f"验证会话未完成:子进程退出码 {returncode}"
                jobs[job_id]["result"] = job_result
        logger.info("验证会话结束:job=%s returncode=%d", job_id, returncode)

    # ------------------------------------------------------------------
    # 扫描受理核心(/scan 与 V10.26 /scan-lead 共用:受理 + trace + 池提交)
    # ------------------------------------------------------------------
    def _submit_scan_job(
        url: str, extra_fields: dict[str, Any] | None = None
    ) -> Any:
        """登记扫描任务并提交线程池;返回 202 响应体(trace 开启时带头)。

        - ``extra_fields`` 并入任务 dict 留痕(如 /scan-lead 的线索行号
          ``line``;不影响 GET /jobs 的 status/result/error 出参口径);
        - 受理即 ``running``(与历史语义逐字节一致),trace 处理同 A203:
          开启时响应附 ``X-Trace-Id``,worker 首步接回同一 trace 树。
        """
        telemetry.inc("service.scan_requested")
        job_id = uuid.uuid4().hex

        def _register_and_submit(trace_id: str | None) -> None:
            """登记任务并提交线程池(在当前 trace/span 上下文内调用)。

            ``trace_id`` 非 None 时:任务 dict 记 trace_id,并在当前上下文
            (scan.submit span 内)拍 :func:`tt.propagate` 快照随任务提交,
            worker 首步 restore 即接回同一条 trace 树。
            """
            with jobs_lock:
                job: dict[str, Any] = {
                    "status": "running",
                    "url": url,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                if trace_id is not None:
                    job["trace_id"] = trace_id
                if extra_fields:
                    job.update(extra_fields)
                jobs[job_id] = job
            if trace_id is None:
                _get_scan_pool(scan_workers).submit(_execute_scan, job_id, url)
            else:
                _get_scan_pool(scan_workers).submit(
                    _execute_scan, job_id, url, tt.propagate()
                )

        if not trace_enabled:
            # A203 开关关闭:不开 trace、不加响应头,与接线前完全一致
            _register_and_submit(None)
            return {"job_id": job_id, "status": "running"}

        with tt.new_trace() as trace_id:
            with tt.span("scan.submit", attrs={"stage": "submit"}):
                _register_and_submit(trace_id)
            return JSONResponse(
                {"job_id": job_id, "status": "running"},
                status_code=202,
                headers={"X-Trace-Id": trace_id},
            )

    # ------------------------------------------------------------------
    # 端点:扫描任务
    # ------------------------------------------------------------------
    @app.post("/scan", status_code=202, summary="提交站点扫描(后台异步执行)")
    def scan(request: dict[str, Any] | None = None) -> dict[str, Any]:
        url = _body_str(
            request, "url", "url 不合法:请求体必须含字符串字段 url"
        )
        if not _is_http_url(url):
            raise HTTPException(
                status_code=422,
                detail="url 不合法:必须是 http(s):// 开头且带主机名的完整链接",
            )
        return _submit_scan_job(url)

    @app.get("/jobs/{job_id}", summary="查询扫描任务状态")
    def get_job(job_id: str) -> dict[str, Any]:
        with jobs_lock:
            job = jobs.get(job_id)
            snapshot = dict(job) if job is not None else None
        if snapshot is None:
            raise HTTPException(
                status_code=404, detail=f"扫描任务不存在:job_id={job_id}"
            )
        resp: dict[str, Any] = {"status": snapshot["status"]}
        if "result" in snapshot:
            resp["result"] = snapshot["result"]
        if "error" in snapshot:
            resp["error"] = snapshot["error"]
        return resp

    @app.get("/trace/{job_id}", summary="查询扫描任务的追踪 span 树(只读)")
    def get_trace(job_id: str) -> dict[str, Any]:
        """任务 dict 记录的 trace_id → ``export_trace_json`` 嵌套 span 树。

        只读端点:自身不开 span、不建 trace;任务不存在 404;任务无
        ``trace_id``(开关关闭时的任务)返回空树 200——**显式判 None**,
        绝不误读当前环境可能残留的 trace(export 的 None 语义是"当前上下文")。

        V10.18 线程安全:``trace_id`` 的读取收进 jobs_lock 锁内(与
        ``get_job`` 的快照口径一致)——``trace_id`` 建后不变,旧代码锁外
        读取在 CPython 下本也安全,此举纯属防御性收紧,响应逐字节等价。
        """
        with jobs_lock:
            job = jobs.get(job_id)
            trace_id = job.get("trace_id") if job is not None else None
        if job is None:
            raise HTTPException(
                status_code=404, detail=f"扫描任务不存在:job_id={job_id}"
            )
        if trace_id is None:
            return {"trace_id": None, "started_ts": None, "spans": []}
        return tt.export_trace_json(trace_id)

    # ------------------------------------------------------------------
    # 端点:人工复核队列(机器初筛,人工拍板)
    # ------------------------------------------------------------------
    @app.get("/queue", summary="列出复核队列条目与统计(可选 limit/offset 分页)")
    def list_queue(
        limit: str | None = None, offset: str | None = None
    ) -> dict[str, Any]:
        """列出复核队列条目与统计(V10.20:可选 ``?limit=&offset=`` 分页)。

        - **缺省全量返回,完全向后兼容**:不带任何参数时响应体仍是
          ``{"entries": [...], "summary": {...}}`` 逐字节旧口径;
        - 分页:``limit``(≥1 的整数)与 ``offset``(≥0 的整数)任一在场
          即进入分页语义——entries 取 ``全量[offset:offset+limit]`` 切片
          (按 id 升序的原有顺序),并在响应附加 ``total``(全量条数,
          供客户端计算页数);summary 恒为**全队列**统计(分页只切
          entries,不切统计口径);offset 越界返回空表(200,不报错);
        - 参数手工校验(不以 int 注解交给 FastAPI):非法值(非整数 /
          超界)统一 **422 中文**,与 /scan 的中文错误风格一致。
        """
        parsed_limit: int | None = None
        parsed_offset: int = 0
        if limit is not None:
            try:
                parsed_limit = int(str(limit).strip())
            except ValueError:
                parsed_limit = -1  # 落入下方的非法值分支
            if parsed_limit < 1:
                raise HTTPException(
                    status_code=422,
                    detail="limit 不合法:须为 ≥1 的整数(如 ?limit=20)",
                )
        if offset is not None:
            try:
                parsed_offset = int(str(offset).strip())
            except ValueError:
                parsed_offset = -1
            if parsed_offset < 0:
                raise HTTPException(
                    status_code=422,
                    detail="offset 不合法:须为 ≥0 的整数(如 ?offset=40)",
                )

        queue = _open_queue(cfg)
        try:
            entries = [_entry_to_dict(e) for e in queue.list()]
            summary = dict(queue.summary())
        finally:
            queue.close()
        if parsed_limit is None and offset is None:
            return {"entries": entries, "summary": summary}
        end = parsed_offset + (parsed_limit if parsed_limit is not None else len(entries))
        return {
            "entries": entries[parsed_offset:end],
            "summary": summary,
            "total": len(entries),
        }

    @app.post("/queue/{entry_id}/approve", summary="人工确认(pending → approved)")
    def approve(entry_id: int, request: dict[str, Any] | None = None) -> dict[str, Any]:
        note = _body_str(request, "note", "note 不合法:如需备注请传字符串字段 note") \
            if (request or {}).get("note") is not None else ""
        return _transition(entry_id, "approve", note)

    @app.post("/queue/{entry_id}/reject", summary="人工驳回(pending → rejected)")
    def reject(entry_id: int, request: dict[str, Any] | None = None) -> dict[str, Any]:
        note = _body_str(request, "note", "note 不合法:驳回原因请传字符串字段 note") \
            if (request or {}).get("note") is not None else ""
        return _transition(entry_id, "reject", note)

    def _transition(entry_id: int, action: str, note: str) -> dict[str, Any]:
        """approve / reject 共用:先查存在性(404),再做状态迁移(409)。"""
        queue = _open_queue(cfg)
        try:
            if queue.get(entry_id) is None:
                raise HTTPException(
                    status_code=404, detail=f"复核条目不存在:id={entry_id}"
                )
            try:
                entry = queue.approve(entry_id, note) if action == "approve" \
                    else queue.reject(entry_id, note)
            except ValueError as exc:
                # 非法状态迁移(如已 approved 再 approve):409 中文错误
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            queue.close()
        return {"ok": True, "entry": _entry_to_dict(entry)}

    # ------------------------------------------------------------------
    # 端点:举报计划预览(绝不执行提交)
    # ------------------------------------------------------------------
    @app.post("/plan/{entry_id}", summary="生成举报计划预览(不执行提交)")
    def make_plan(entry_id: int, request: dict[str, Any] | None = None) -> dict[str, Any]:
        valid_portals = _list_valid_portals()  # A233:内置 ∪ 动态 enabled 门户
        portal = _body_str(
            request, "portal", "portal 不合法:请求体必须含字符串字段 portal(如 12377 / shdf)"
        )
        if portal not in valid_portals:
            raise HTTPException(
                status_code=422,
                detail=f"portal 不合法:{portal!r}(可用:{' / '.join(valid_portals)})",
            )
        queue = _open_queue(cfg)
        try:
            entry = queue.get(entry_id)
            if entry is None:
                raise HTTPException(
                    status_code=404, detail=f"复核条目不存在:id={entry_id}"
                )
            if str(getattr(entry, "status", "")) != "approved":
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"复核条目 {entry_id} 当前状态为 {getattr(entry, 'status', '')},"
                        f"只有 approved(已人工确认)的条目才能生成举报计划"
                    ),
                )
        finally:
            queue.close()

        try:
            plan = _build_report_plan(entry, portal, cfg)
        except ValueError as exc:
            # 门户在清单校验后仍取不到定义(动态门户刚被改坏/禁用等竞态)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        plan_json, playbook_md = _render_plan(plan)
        telemetry.inc("service.plan_generated")
        return {"plan": plan_json, "playbook_md": playbook_md, "notice": PLAN_NOTICE}

    # ------------------------------------------------------------------
    # 端点:净网助手统一工作台(V10.26 席位A:REST 服务端后台化)
    # ------------------------------------------------------------------
    @app.post(
        "/discover",
        status_code=202,
        summary="提交线索发现(后台无头执行,线索落 <data>/leads.txt)",
    )
    def discover_leads(request: dict[str, Any] | None = None) -> dict[str, Any]:
        """后台整轮线索发现(关键词 → 搜索引擎 → 线索清单),不阻塞页面。

        - body ``{"keywords": ["词", ...]}`` 可选:缺省读关键词活文件
          ``<data>/keywords/active.txt``(按行解析同关键词页口径:strip、
          跳空行/#注释、去重保序;缺失或解析为空 → 422 中文);
        - body ``{"engine": "yandex-html"}`` 可选:缺省取
          ``cfg.discovery_engine``;引擎构造/查询失败一律进 job 的
          errors/failed(中文),本端点不做引擎白名单(与 CLI 同宽);
        - 受理即 ``queued``,worker 起步置 ``running``;整轮带 errors 也算
          ``done``——result 含 ``{"urls": n, "queries": n, "errors":
          ["查询词 —— 中文原因", ...], "out": "<data>/leads.txt 路径"}``;
          discover()/写盘异常 → ``failed``(中文 error,验证码失败即此路)。
        """
        body = request or {}
        raw_keywords = body.get("keywords")
        if raw_keywords is None:
            keywords = _read_active_keywords(cfg)
            if not keywords:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "keywords 不合法:请求体未提供 keywords,且关键词活文件"
                        f"({_active_keywords_path(cfg)})缺失或为空"
                    ),
                )
        elif isinstance(raw_keywords, list) and all(
            isinstance(k, str) for k in raw_keywords
        ):
            keywords = _parse_keyword_lines(raw_keywords)
            if not keywords:
                raise HTTPException(
                    status_code=422,
                    detail="keywords 不合法:列表内没有有效搜索词(空串/#注释行不计)",
                )
        else:
            raise HTTPException(
                status_code=422,
                detail='keywords 不合法:必须为字符串列表(如 ["词A", "词B"])',
            )
        raw_engine = body.get("engine")
        engine: str | None = None
        if raw_engine is not None:
            if not isinstance(raw_engine, str) or not raw_engine.strip():
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "engine 不合法:必须为非空字符串(如 yandex-html;"
                        "缺省取配置 discovery_engine)"
                    ),
                )
            engine = raw_engine.strip()

        telemetry.inc("service.discover_requested")
        job_id = uuid.uuid4().hex
        with jobs_lock:
            jobs[job_id] = {
                "status": "queued",
                "kind": "discover",
                "keywords": len(keywords),
                "engine": str(engine or cfg.discovery_engine),
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        _get_scan_pool(scan_workers).submit(_execute_discover, job_id, keywords, engine)
        return {"job_id": job_id, "status": "queued"}

    @app.get("/leads", summary="读线索清单(只读)")
    def list_leads() -> dict[str, Any]:
        """读 ``<data>/leads.txt``:跳 ``#`` 注释行,附 1 起始的文件行号。

        只读端点:文件缺失 → 空表(mtime null);绝不写盘/建目录;读失败
        (IO 异常)按空表处理并记 warning,不让工作台页面 500。``line`` 与
        ``POST /scan-lead`` 的 line 同源(文件内真实行号,注释行占号)。
        """
        path = _leads_path(cfg)
        leads: list[dict[str, Any]] = []
        mtime: str | None = None
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8-sig")
                mtime = _dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat()
            except OSError as exc:  # noqa: BLE001 - 读失败按空表返回
                logger.warning("线索文件读取失败(按空表返回):%s:%s", path, exc)
                return {"leads": [], "count": 0, "mtime": None}
            for lineno, line in enumerate(text.splitlines(), start=1):
                url = line.strip()
                if not url or url.startswith("#"):
                    continue
                leads.append({"url": url, "line": lineno})
        return {"leads": leads, "count": len(leads), "mtime": mtime}

    @app.post(
        "/scan-lead",
        status_code=202,
        summary="按线索提交扫描(与 /scan 同机制,后台异步执行)",
    )
    def scan_lead(request: dict[str, Any] | None = None) -> dict[str, Any]:
        """按 GET /leads 的线索提交扫描:受理/线程池/trace 与 POST /scan 完全同机制。

        - ``url`` 必填(字符串 + http(s):// 完整链接,缺参/非法 422 中文);
        - ``line`` 可选(线索行号,须为 ≥1 的整数,非法 422 中文),仅随
          任务 dict 留痕,不参与扫描语义。
        """
        body = request or {}
        url = _body_str(
            body, "url", "url 不合法:请求体必须含字符串字段 url"
        )
        if not _is_http_url(url):
            raise HTTPException(
                status_code=422,
                detail="url 不合法:必须是 http(s):// 开头且带主机名的完整链接",
            )
        raw_line = body.get("line")
        line: int | None = None
        if raw_line is not None:
            if (
                isinstance(raw_line, bool)
                or not isinstance(raw_line, int)
                or raw_line < 1
            ):
                raise HTTPException(
                    status_code=422,
                    detail="line 不合法:线索行号须为 ≥1 的整数(可选,来自 GET /leads)",
                )
            line = int(raw_line)
        extra: dict[str, Any] = {"kind": "scan-lead"}
        if line is not None:
            extra["line"] = line
        return _submit_scan_job(url, extra)

    @app.post(
        "/captcha/session",
        status_code=202,
        summary="启动一次性可见验证会话(用户显式点击才触发)",
    )
    def captcha_session() -> dict[str, Any]:
        """可见验证窗口(**唯一有头路径**):后台 subprocess 有头跑一次发现 CLI。

        只应在用户于净网助手页面显式点击"验证会话"时调用——子进程会弹出
        真实浏览器窗口等待人工完成验证码(人工门,机器只等待);验证通过
        的会话 cookie 落 ``<data>/browser_profiles/yandex_html``,后续后台
        无头发现即可复用。job result 含 ``{"returncode", "stdout_tail"}``,
        returncode 0 = 验证完成;非 0/超时/启动失败 → failed(中文 error)。
        """
        telemetry.inc("service.captcha_session_requested")
        job_id = uuid.uuid4().hex
        with jobs_lock:
            jobs[job_id] = {
                "status": "queued",
                "kind": "captcha-session",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        _get_scan_pool(scan_workers).submit(_execute_captcha_session, job_id)
        return {"job_id": job_id, "status": "queued"}

    @app.get(
        "/assistant/status",
        summary="净网助手工作台状态总览(全本地只读,零外呼)",
    )
    def assistant_status() -> dict[str, Any]:
        """工作台首屏状态总览:五路本地探测拼装,绝不发起任何网络/模型外呼。

        - ``glm_key``:``~/.netsentinel/keys/glm`` 密钥文件存在性(只查文件);
        - ``yandex_profile``:``<data>/browser_profiles/yandex_html`` 会话
          目录存在性(验证会话是否已就绪);
        - ``keywords_count``:关键词活文件有效词数(坏文件 → 0);
        - ``queue``:复核队列四状态计数(与 GET /queue 的 summary 同款;
          只读连接,库缺席 → 全 0,绝不建库);
        - ``config``:``vlm_online`` / ``discovery_online`` /
          ``classifier`` 三项配置原样透传(布尔/字符串)。
        """
        return {
            "glm_key": _glm_key_path().is_file(),
            "yandex_profile": _yandex_profile_path(cfg).exists(),
            "keywords_count": len(_read_active_keywords(cfg)),
            "queue": _queue_summary_readonly(cfg),
            "config": {
                "vlm_online": bool(cfg.vlm_online),
                "discovery_online": bool(cfg.discovery_online),
                "classifier": str(cfg.classifier),
            },
        }

    # ------------------------------------------------------------------
    # 端点:健康检查
    # ------------------------------------------------------------------
    @app.get("/health", summary="健康检查")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": SERVICE_VERSION}

    # V8:挂载视觉模型管理路由(切换/探测/测试;缺席只告警不阻断)。
    try:
        from netsentinel.service.model_api import create_model_router

        app.include_router(create_model_router(cfg))
    except Exception as _exc:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning("模型路由挂载失败(忽略):%s", _exc)

    return app


# ---------------------------------------------------------------------------
# CLI 入口:python -m service.app
# ---------------------------------------------------------------------------
def _build_arg_parser() -> Any:
    """构建入口参数解析器(V10.20:--host/--port/--help;标准库 argparse)。

    - 选项全部可选且缺省 None:实际监听地址仍取 ``cfg.service_host`` /
      ``cfg.service_port``(config.yaml 语义不变),CLI 只做**显式覆盖**;
    - ``--help`` 由 argparse 原生处理:打印用法后 ``SystemExit(0)``,
      **绝不起服务、不碰端口**(V10.20 前的缺陷:main 忽略 argv,
      ``python -m service.app --help`` 会直接绑 8765 起 uvicorn,
      与 LIVE 服务端口冲突);
    - 非法选项(如 ``--bogus`` / ``--port abc``)由 argparse 报中文补充
      说明后以退出码 2 结束——解析在 load_config / uvicorn 导入**之前**,
      坏参数零副作用。
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m service.app",
        description=(
            "净网哨兵本机 REST 服务(无鉴权,仅限本机;只生成举报计划与预览,"
            "不提供真实提交端点)。监听地址缺省取 config.yaml 的 "
            "service_host/service_port(默认 127.0.0.1:8765)。"
        ),
    )
    parser.add_argument(
        "--host",
        default=None,
        help="监听地址(缺省取 config.yaml service_host,默认 127.0.0.1;请保持回环地址)",
    )
    parser.add_argument(
        "--port",
        default=None,
        type=int,
        help="监听端口(缺省取 config.yaml service_port,默认 8765)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """启动本机 REST 服务(uvicorn,惰性导入;阻塞运行)。

    V10.20 入口层修复:此前本函数**忽略 argv**(参数仅是摆设),
    ``python -m service.app --help`` 会直接绑 8765 起 uvicorn——与
    LIVE 服务端口冲突。现在:

    - 先经 :func:`_build_arg_parser` 解析 ``argv``(缺省 ``sys.argv[1:]``):
      ``--help`` 正常打印用法并退出 0(不起服务),非法参数退出 2;
    - ``--host`` / ``--port`` 为显式覆盖,缺省仍取 ``cfg.service_host`` /
      ``cfg.service_port``(默认 ``127.0.0.1:8765``),既有启动语义
      逐字不变(向后兼容红线)。

    **本服务无鉴权,仅限本机使用**;绑定非回环地址时打印告警并要求
    确认环境可信。

    :param argv: 命令行参数(缺省 ``sys.argv[1:]``;测试可注入)。
    :return: 进程退出码(uvicorn 正常退出后返回 0)。
    """
    args = _build_arg_parser().parse_args(argv)

    from netsentinel.config import load_config

    cfg = load_config()
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            "启动本服务需要 uvicorn,请先安装可选依赖:pip install 'netsentinel[api]'"
        ) from exc

    host = str(args.host) if args.host is not None else str(cfg.service_host)
    port = int(args.port) if args.port is not None else int(cfg.service_port)
    if host not in _LOOPBACK_HOSTS:
        logger.warning(
            "service_host=%s 不是本机回环地址:本服务无鉴权,请确保仅暴露在可信网络!"
            " 建议保持默认 127.0.0.1。运行期间请自行确认环境安全。",
            host,
        )

    app = create_app(cfg)
    logger.info("净网哨兵本机服务启动:%s:%d(无鉴权,仅限本机;不提供提交端点)", host, port)
    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
