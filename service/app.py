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

测试约定:模块级 :func:`_get_run_scan` 是扫描执行函数的获取缝,
``monkeypatch.setattr(service.app, "_get_run_scan", lambda: fake)`` 即可
离线替换 orchestrator.run_scan,不发起任何网络请求;
:func:`_get_scan_pool` 是线程池的获取缝,同理可注入替身。
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import dataclasses
import logging
import threading
import time
import urllib.parse
import uuid
from typing import Any, Callable

from netsentinel import telemetry
from netsentinel import telemetry_trace as tt
from netsentinel.contracts import Config, SiteReport

__all__ = ["create_app", "main", "SCAN_MAX_WORKERS", "TRACE_ENABLED"]

logger = logging.getLogger(__name__)

#: 服务版本(A31 属 V2 升级批次)。
SERVICE_VERSION = "v2"

#: 计划预览统一声明(红线:本服务不执行提交)。
PLAN_NOTICE = "本服务不执行提交;请用 CLI 在人工门下执行"

#: 举报门户合法取值(与 contracts.Portal 一致)。
_VALID_PORTALS = ("12377", "shdf")

#: 本机回环地址集合(绑定非回环时告警)。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: 后台扫描线程池大小(V5:每请求新线程 → 进程内复用池)。
#: 2 路并发上限既复用线程,又天然限制同时扫描的站点数(资源保护)。
SCAN_MAX_WORKERS: int = 2

#: A203 trace 贯通总开关(默认开):False 时 /scan 不开 trace、不加
#: X-Trace-Id 响应头、不接 audit_sink——与接线前行为完全一致。
#: 经 ``create_app(..., trace_enabled=False)`` 或 monkeypatch 本常量关闭。
TRACE_ENABLED: bool = True

#: 进程内共享的扫描线程池(经 :func:`_get_scan_pool` 惰性创建)。
_scan_pool: concurrent.futures.ThreadPoolExecutor | None = None


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


def _get_scan_pool() -> concurrent.futures.ThreadPoolExecutor:
    """获取进程内共享的扫描线程池(惰性单例;测试可 monkeypatch 本缝)。

    V5 性能:此前每个 POST /scan 都新建一个 daemon 线程(创建/销毁开销
    与线程数都不设限);现在复用 :data:`SCAN_MAX_WORKERS` 大小的池,
    线程只建一次,超额请求在池内排队——jobs 的完成语义完全不变。
    """
    global _scan_pool
    if _scan_pool is None:
        _scan_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=SCAN_MAX_WORKERS,
            thread_name_prefix="netsentinel-scan",
        )
    return _scan_pool


def _open_queue(cfg: Config) -> Any:
    """打开复核队列连接(调用方负责 close,避免跨线程复用同一连接)。"""
    return _get_queue_cls()(cfg.db_path)


def _build_report_plan(entry_like: Any, portal: str, cfg: Config) -> Any:
    """按门户分派生成举报步骤计划(只生成计划,不执行、不联网)。"""
    if portal == "12377":
        from netsentinel.submit import portal_12377

        return portal_12377.plan_12377(entry_like, cfg)
    if portal == "shdf":
        from netsentinel.submit import portal_shdf

        return portal_shdf.plan_shdf(entry_like, cfg)
    raise ValueError(f"不支持的举报门户:{portal!r}(仅支持 12377 / shdf)")


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


def _find_entry_id(site_url: str, cfg: Config) -> int | None:
    """扫描结束后在复核队列里找该站点最新条目的 id(取同站点最大 id)。

    orchestrator.run_scan 不在返回值里携带 entry_id(needs_review 时它把
    条目写进了 cfg.db_path 指向的队列),这里按站点地址回查最新一条;
    队列不可用或查不到时返回 None 并记 warning,不影响扫描结果本身。
    """
    try:
        queue = _open_queue(cfg)
        try:
            matches = [e for e in queue.list() if e.site_url == site_url]
        finally:
            queue.close()
    except Exception as exc:  # noqa: BLE001 - 回查失败只影响 entry_id 字段
        logger.warning("回查复核队列条目编号失败(site=%s):%s", site_url, exc)
        return None
    return max(int(e.id) for e in matches) if matches else None


def _is_http_url(url: str) -> bool:
    """url 必须是 http(s):// 开头且带主机名的完整链接。"""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


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

    app = FastAPI(
        title="净网哨兵 NetSentinel 本机辅助服务",
        version=SERVICE_VERSION,
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
                jobs[job_id] = job
            if trace_id is None:
                _get_scan_pool().submit(_execute_scan, job_id, url)
            else:
                _get_scan_pool().submit(
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
        """
        with jobs_lock:
            job = jobs.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=404, detail=f"扫描任务不存在:job_id={job_id}"
            )
        trace_id = job.get("trace_id")
        if trace_id is None:
            return {"trace_id": None, "started_ts": None, "spans": []}
        return tt.export_trace_json(trace_id)

    # ------------------------------------------------------------------
    # 端点:人工复核队列(机器初筛,人工拍板)
    # ------------------------------------------------------------------
    @app.get("/queue", summary="列出复核队列条目与统计")
    def list_queue() -> dict[str, Any]:
        queue = _open_queue(cfg)
        try:
            entries = [_entry_to_dict(e) for e in queue.list()]
            summary = dict(queue.summary())
        finally:
            queue.close()
        return {"entries": entries, "summary": summary}

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
        portal = _body_str(
            request, "portal", "portal 不合法:请求体必须含字符串字段 portal(12377 / shdf)"
        )
        if portal not in _VALID_PORTALS:
            raise HTTPException(
                status_code=422,
                detail=f"portal 不合法:{portal!r}(仅支持 12377 / shdf)",
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

        plan = _build_report_plan(entry, portal, cfg)
        plan_json, playbook_md = _render_plan(plan)
        telemetry.inc("service.plan_generated")
        return {"plan": plan_json, "playbook_md": playbook_md, "notice": PLAN_NOTICE}

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
def main(argv: list[str] | None = None) -> int:
    """启动本机 REST 服务(uvicorn,惰性导入;阻塞运行)。

    监听地址与端口取 ``cfg.service_host`` / ``cfg.service_port``
    (默认 ``127.0.0.1:8765``)。**本服务无鉴权,仅限本机使用**;
    绑定非回环地址时打印告警并要求确认环境可信。

    :param argv: 预留参数(当前无选项,配置一律走 config.yaml)。
    :return: 进程退出码(uvicorn 正常退出后返回 0)。
    """
    from netsentinel.config import load_config

    cfg = load_config()
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            "启动本服务需要 uvicorn,请先安装可选依赖:pip install 'netsentinel[api]'"
        ) from exc

    host = str(cfg.service_host)
    port = int(cfg.service_port)
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
