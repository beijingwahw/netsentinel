"""NetSentinel 举报计划执行器(Playwright 驱动)。[A15;V5 可观测/健壮性升级]

按 :class:`~netsentinel.contracts.SubmissionPlan` 逐步驱动浏览器,完成举报表单的
填写、截图与提交;同一份计划未来也可由 computer-use 执行器复用。

安全红线(CONTRACTS §0,违反即缺陷):
1. 绝不自动识别/绕过验证码:验证码环节只能以 ``HUMAN_GATE`` 步骤出现,本模块
   只负责暂停等待人工处理,不包含任何验证码识别或自动填写逻辑。
2. 真实模式下 ``HUMAN_GATE`` 必须交互等待人工确认(``input()``);仅
   ``auto_confirm=True``(测试模式,只应配合本地 file:///127.0.0.1 mock 页面
   使用)可跳过交互。
3. ``dry_run=True`` 干跑路径只依赖 Python 标准库,绝不导入 playwright 等
   三文库,保证 CI 可离线测试。
4. 开发与测试期间只允许 ``file://`` 或 ``127.0.0.1`` 本地页面,绝不访问
   www.12377.cn / www.shdf.gov.cn 真实门户。

Playwright 采用函数内惰性导入:未安装时抛出带中文安装提示的 ``ImportError``。

V5 升级(契约 §1):
- 可观测性:真实模式整体计时 ``telemetry.timer("executor.run")``;提交成功
  (``submitted=True``)记 ``telemetry.inc("executor.submitted")``;浏览器
  启动失败与步骤异常记 ``telemetry.inc("executor.errors")``;截图保存失败记
  ``telemetry.inc("executor.screenshot_failures")``。干跑路径不产生任何
  executor 指标(纯文本,零三方依赖)。
- 健壮性:截图步骤的保存失败(OSError / Playwright Error)窄捕获为
  warning + notes 记录,不中断后续步骤(截图是佐证性 IO,不应阻断举报);
  ``HUMAN_GATE`` 的交互语义与文案一字未动(红线)。

反脆弱事件驱动等待(V12,对标 self-healing / anti-fragile 等待策略):
- WAIT 步缺省保持固定盲睡(旧计划 JSON 语义不变);声明
  ``Step.meta["wait_until"]``(selector_visible / text_present / requests_idle,
  任一满足即过)时切换为事件驱动;
- 每个声明条件超时后按指数退避(×2)重试一次,仍失败则进入分级兜底链:
  networkidle 超时 → 降级 selector_visible → 最终兜底盲睡(延续旧语义,
  对齐 crawler/browser.py 的 networkidle 容错先例);
- 每一级等待结果全部写入 step notes(审计可见);干跑路径行为不变(只记 notes)。

自愈选择器候选链(V13,对标 Healenium / Selenium AI 式 self-healing selector):
- ``Step.meta["selector_chain"]`` 声明有序候选链(候选类型:css /
  role+name / label / placeholder / text);未声明(或链长为 1)时走 v1
  快速路径——直接用 ``Step.selector`` 驱动页面,零探测、notes 逐字不变;
- 链长 ≥ 2 时由 :func:`_resolve_selector` 逐候选即时探测(css→
  query_selector;role+name→get_by_role;label/placeholder/text→对应
  get_by_* 定位器),命中即用,胜者索引与候选类型写入 step notes(审计
  可见),并把胜者索引记入进程内运行时缓存(下次同字段解析先试胜者;
  绝不回写门户 YAML);
- 全候选失败抛中文 :class:`LookupError` —— 沿既有错误路径进 notes、
  ``stopped_at`` 中止,绝不静默降级;
- 验证码红线不受影响:候选链只出现在 FILL/SELECT/CLICK 步骤,加载期与
  计划自检期已对候选链逐候选拒绝任何指向验证码字段的候选;
- 干跑路径行为不变(候选链只存在于 meta,不进入干跑 notes);回退命中
  记 ``telemetry.inc("executor.selfheal")``。

用法示例(干跑,纯标准库)::

    from netsentinel.submit.executor_playwright import execute
    result = execute(plan, cfg, dry_run=True)   # ok=True, submitted=False
"""
from __future__ import annotations

import datetime as _dt
import logging
import pathlib
import time
from typing import Any, Callable

from netsentinel import telemetry
from netsentinel.contracts import (
    Config,
    ExecutionResult,
    Step,
    StepAction,
    SubmissionPlan,
)

__all__ = ["execute", "SUBMIT_SELECTOR", "DRY_RUN_BANNER"]

logger = logging.getLogger(__name__)

#: 提交按钮选择器(CONTRACTS §3 SELECTORS["submit"];此处硬编码以避免
#: 对并行开发中的 submit.form_models 模块产生依赖)
SUBMIT_SELECTOR = "#report-submit"

#: 干跑模式 notes 首行固定文案
DRY_RUN_BANNER = "DRY-RUN:未驱动浏览器"

#: 运行目录时间戳格式
_RUN_TS_FORMAT = "%Y%m%d_%H%M%S"

#: 遥测指标名(V5 可观测性,命名规范 <模块>.<动作>)
_METRIC_RUN = "executor.run"
_METRIC_SUBMITTED = "executor.submitted"
_METRIC_ERRORS = "executor.errors"
_METRIC_SHOT_FAILURES = "executor.screenshot_failures"
#: 自愈回退遥测指标(V13):候选链命中非首位候选(含缓存命中的胜者)时计数。
_METRIC_SELFHEAL = "executor.selfheal"

#: 事件等待超时的指数退避因子:首次超时后重试一次,重试超时 = 首次 × 该值。
_WAIT_BACKOFF_FACTOR: float = 2.0
#: text_present 轮询 page.content() 的起始间隔(秒),逐次指数翻倍。
_POLL_BASE_S: float = 0.1
#: text_present 轮询间隔上限(秒);封顶避免长间隔漏检突发渲染。
_POLL_MAX_S: float = 0.8


# ---------------------------------------------------------------------------
# 反脆弱事件驱动等待(V12)
# ---------------------------------------------------------------------------
def _step_wait_until_or_none(step: Step) -> dict[str, Any] | None:
    """提取并校验 ``Step.meta["wait_until"]``;未声明返回 None(旧盲睡语义)。

    惰性导入 form_models:与模块既有惰性导入风格一致,未声明 wait_until 的
    旧路径不新增任何模块级依赖。声明了但内容非法时抛中文 ValueError
    (由步骤级异常捕获记录为错误并中止该计划 —— 错误的等待计划必须暴露,
    绝不静默降级)。
    """
    if not (isinstance(step.meta, dict) and "wait_until" in step.meta):
        return None
    from netsentinel.submit.form_models import step_wait_until

    return step_wait_until(step)


def _make_idle_runner(page: Any) -> Callable[[float], None]:
    """requests_idle 条件执行体:等待网络空闲(networkidle)。"""

    def run(timeout_s: float) -> None:
        page.wait_for_load_state("networkidle", timeout=int(timeout_s * 1000))

    return run


def _make_selector_runner(page: Any, selector: str) -> Callable[[float], None]:
    """selector_visible 条件执行体:等待 CSS 元素可见。"""

    def run(timeout_s: float) -> None:
        page.wait_for_selector(selector, state="visible", timeout=int(timeout_s * 1000))

    return run


def _make_text_runner(page: Any, text: str) -> Callable[[float], None]:
    """text_present 条件执行体:轮询 page.content(),间隔指数退避。"""

    def run(timeout_s: float) -> None:
        deadline = time.monotonic() + max(float(timeout_s), 0.001)
        interval = _POLL_BASE_S
        while True:
            if text in (page.content() or ""):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"轮询 {float(timeout_s):.2f}s 内页面未出现文本:{text!r}"
                )
            time.sleep(min(interval, remaining))
            interval = min(interval * 2.0, _POLL_MAX_S)

    return run


def _attempt_with_retry(
    run: Callable[[float], None], timeout_s: float
) -> tuple[bool, str]:
    """尝试一个等待条件;超时(或任何页面异常)按指数退避重试一次。

    对齐 crawler/browser.py 的 networkidle 容错先例:条件失败不外抛,
    由调用方决定降级。:return: (是否成功, 审计备注)。
    """
    first_s = max(float(timeout_s), 0.001)
    try:
        run(first_s)
        return True, ""
    except Exception as first_exc:  # noqa: BLE001 - 任何条件失败都进入降级,不外抛
        retry_s = first_s * _WAIT_BACKOFF_FACTOR
        try:
            run(retry_s)
            return True, (
                f"(首次失败退避重试成功,重试超时 {retry_s:.2f}s;首次异常:{first_exc})"
            )
        except Exception as second_exc:  # noqa: BLE001
            return False, (
                f"首次:{first_exc};退避重试(×{_WAIT_BACKOFF_FACTOR:g})仍失败:{second_exc}"
            )


def _blind_sleep_seconds(step: Step) -> float:
    """兜底盲睡秒数:优先 step.value,为空/非法时回退 step.timeout_s(旧语义)。"""
    raw = step.value
    if raw not in (None, ""):
        try:
            return max(float(raw), 0.0)
        except (TypeError, ValueError):
            pass
    try:
        return max(float(step.timeout_s), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _event_driven_wait(
    step: Step,
    wait_until: dict[str, Any],
    *,
    page: Any,
    idx: int,
    result: ExecutionResult,
) -> None:
    """反脆弱事件驱动等待:声明条件(Any-of)→ 退避重试一次 → 分级兜底链。

    分级兜底链(每一级都写 notes,审计可见):
    1. 声明条件按规范顺序尝试(requests_idle → selector_visible → text_present),
       每个条件超时后按指数退避(×2)重试一次;任一满足即通过;
    2. 全部声明条件失败 → 降级 selector_visible:等待 step.selector 可见
       (单次尝试;若 selector_visible 本身是声明条件则不重复尝试);
    3. 最终兜底:固定盲睡(与旧 WAIT 语义一致)—— 等待步绝不因事件条件
       不可满足而中断举报主流程。
    """
    started = time.monotonic()

    def elapsed() -> str:
        return f"(已耗时 {time.monotonic() - started:.2f}s)"

    attempts: list[tuple[str, float, Callable[[float], None]]] = []
    if "requests_idle" in wait_until:
        attempts.append(
            ("requests_idle", float(wait_until["requests_idle"]), _make_idle_runner(page))
        )
    if "selector_visible" in wait_until:
        attempts.append(
            (
                "selector_visible",
                float(step.timeout_s),
                _make_selector_runner(page, str(wait_until["selector_visible"])),
            )
        )
    if "text_present" in wait_until:
        attempts.append(
            (
                "text_present",
                float(step.timeout_s),
                _make_text_runner(page, str(wait_until["text_present"])),
            )
        )

    # 第一级:声明条件(任一满足即过)
    for name, timeout_s, run in attempts:
        ok, detail = _attempt_with_retry(run, timeout_s)
        if ok:
            result.notes.append(
                f"[OK] 第{idx}步 wait {step.label} 事件驱动:条件 {name} 满足{detail}"
                f"{elapsed()}"
            )
            return
        result.notes.append(
            f"[等待超时] 第{idx}步 条件 {name} 超时,指数退避重试一次后仍失败,"
            f"进入降级链:{detail}{elapsed()}"
        )

    # 第二级:降级 selector_visible
    if "selector_visible" in wait_until:
        result.notes.append(
            f"[等待降级] 第{idx}步 selector_visible 已作为声明条件尝试失败,"
            f"跳过同级降级{elapsed()}"
        )
    elif step.selector:
        try:
            page.wait_for_selector(
                step.selector, state="visible", timeout=int(step.timeout_s * 1000)
            )
            result.notes.append(
                f"[等待降级] 第{idx}步 降级 selector_visible 成功:"
                f"{step.selector} 已可见{elapsed()}"
            )
            return
        except Exception as exc:  # noqa: BLE001 - 降级失败继续向下一级
            result.notes.append(
                f"[等待降级] 第{idx}步 降级 selector_visible 失败"
                f"({step.selector}):{exc}{elapsed()}"
            )
    else:
        result.notes.append(
            f"[等待降级] 第{idx}步 无可用降级选择器(步骤未带 selector),"
            f"直接进入盲睡兜底{elapsed()}"
        )

    # 第三级:最终兜底——固定盲睡(延续旧 WAIT 语义)
    sleep_s = _blind_sleep_seconds(step)
    page.wait_for_timeout(int(sleep_s * 1000))
    result.notes.append(
        f"[OK] 第{idx}步 wait {step.label} 兜底盲睡 {sleep_s:g}s"
        f"(全部事件条件未满足){elapsed()}"
    )


# ---------------------------------------------------------------------------
# 自愈选择器候选链(V13)
# ---------------------------------------------------------------------------
#: 运行时解析缓存:主选择器(css 字符串,字段身份)→ 胜者候选索引。
#: 进程内字典,绝不落盘、绝不回写门户 YAML;页面再次改版导致胜者失效时
#: 由 :func:`_resolve_step_target` 自动弹出并全链重扫。
_SELECTOR_WINNERS: dict[str, int] = {}


def _describe_candidate(cand: Any) -> str:
    """候选的审计描述:候选类型 + 取值(如 ``label=举报链接``)。"""
    if not isinstance(cand, dict):
        return "未知候选(非映射)"
    if "css" in cand:
        return f"css={cand['css']}"
    if "role" in cand:
        return f"role+name={cand.get('role', '')}/{cand.get('name', '')}"
    for key in ("label", "placeholder", "text"):
        if key in cand:
            return f"{key}={cand[key]}"
    return "未知候选(无类型键)"


def _candidate_locator(page: Any, cand: dict[str, Any]) -> tuple[str, Any] | None:
    """探测单个候选;命中返回 ``(类型, 目标)``,未命中返回 None。

    - ``("css", 选择器字符串)``:走 page.fill / select_option / click 旧路径;
    - ``("locator", Playwright Locator)``:已取 ``.first``,走 locator 同名方法;
    - role+name 候选依赖 ``page.get_by_role``;页面对象不具备该能力
      (旧版 Playwright / 测试桩)时按未命中处理,链上后续 css/label 候选
      自然兜底。
    探测全部即时返回(``query_selector`` / ``Locator.count`` 不做隐式等待),
    任何异常按未命中处理 —— 单候选探测失败绝不中断候选链。
    """
    try:
        if "css" in cand:
            if page.query_selector(cand["css"]) is not None:
                return "css", cand["css"]
            return None
        if "role" in cand and hasattr(page, "get_by_role"):
            locator = page.get_by_role(cand["role"], name=cand.get("name", ""))
            if locator.count() > 0:
                return "locator", locator.first
            return None
        if "label" in cand and hasattr(page, "get_by_label"):
            locator = page.get_by_label(cand["label"])
            if locator.count() > 0:
                return "locator", locator.first
            return None
        if "placeholder" in cand and hasattr(page, "get_by_placeholder"):
            locator = page.get_by_placeholder(cand["placeholder"])
            if locator.count() > 0:
                return "locator", locator.first
            return None
        if "text" in cand and hasattr(page, "get_by_text"):
            locator = page.get_by_text(cand["text"])
            if locator.count() > 0:
                return "locator", locator.first
            return None
    except Exception:  # noqa: BLE001 - 单候选探测失败 = 未命中,继续下一候选
        return None
    return None


def _resolve_selector(
    page: Any, candidates: list[Any], *, skip: int = -1
) -> tuple[int, str, Any]:
    """逐候选探测自愈选择器候选链,返回 ``(胜者索引, 类型, 目标)``。

    按声明顺序探测 css → role+name → label → placeholder / text 候选;
    ``skip`` 跳过已单独尝试过的候选序号(缓存胜者未命中后的全链重扫)。
    全部未命中抛中文 :class:`LookupError`(附逐候选失败清单)—— 由步骤级
    异常捕获进 notes 并以 ``stopped_at`` 中止,绝不静默降级。
    """
    tried: list[str] = []
    for i, cand in enumerate(candidates):
        if i == skip:
            tried.append(f"#{i + 1}(缓存胜者已单独试过)")
            continue
        hit = _candidate_locator(page, cand if isinstance(cand, dict) else {})
        if hit is not None:
            return i, hit[0], hit[1]
        tried.append(f"#{i + 1} {_describe_candidate(cand)}")
    raise LookupError(
        f"自愈候选链全部未命中(共 {len(candidates)} 个候选):"
        + " ; ".join(tried)
    )


def _resolve_step_target(
    step: Step, page: Any, idx: int, result: ExecutionResult
) -> tuple[str, Any]:
    """解析步骤目标元素:v1 单选择器快速直通,候选链则逐候选自愈探测。

    - v1 兼容:meta 未声明候选链(或链长为 1)时直接返回
      ``("css", step.selector)``,**不做任何页面探测** —— 行为与旧版
      逐字一致(调用序、notes 均不变);
    - 链长 ≥ 2:先查进程内胜者缓存(键 = 主选择器;命中即用并写审计
      notes),缓存未命中或胜者失效则全链探测;胜者索引(含主候选)
      回写缓存 —— 只进运行时缓存与 step notes,绝不回写 YAML;
    - 全候选失败:抛中文 :class:`LookupError`,由调用方按既有错误路径
      记 notes 并以 ``stopped_at`` 中止。

    :return: ``(类型, 目标)`` —— ``css`` 时目标为选择器字符串,
        ``locator`` 时目标为 Playwright Locator(调用方按类型分派)。
    """
    chain = step.meta.get("selector_chain") if isinstance(step.meta, dict) else None
    if not isinstance(chain, list) or len(chain) < 2:
        return "css", step.selector  # v1 快速路径:零探测,notes 不变
    cached = _SELECTOR_WINNERS.get(step.selector, -1)
    if 0 <= cached < len(chain):
        hit = _candidate_locator(page, chain[cached] if isinstance(chain[cached], dict) else {})
        if hit is not None:
            result.notes.append(
                f"[自愈] 第{idx}步 {step.label} 候选链命中缓存胜者:候选#{cached + 1}"
                f"({_describe_candidate(chain[cached])})(主选择器 {step.selector})"
            )
            if cached > 0:
                telemetry.inc(_METRIC_SELFHEAL)
            return hit[0], hit[1]
        # 缓存胜者已失效(门户又改版):弹出缓存,全链重扫。
        _SELECTOR_WINNERS.pop(step.selector, None)
        result.notes.append(
            f"[自愈] 第{idx}步 {step.label} 缓存胜者候选#{cached + 1}未命中,"
            f"重新全链探测(主选择器 {step.selector})"
        )
    winner, kind, target = _resolve_selector(page, chain, skip=cached if 0 <= cached < len(chain) else -1)
    _SELECTOR_WINNERS[step.selector] = winner
    if winner > 0:
        telemetry.inc(_METRIC_SELFHEAL)
    outcome = " 未命中,已回退" if winner > 0 else " 直接命中"
    result.notes.append(
        f"[自愈] 第{idx}步 {step.label} 候选链命中:胜者索引 {winner},"
        f"候选#{winner + 1}({_describe_candidate(chain[winner])})"
        f"(主选择器 {step.selector}{outcome})"
    )
    return kind, target


def execute(
    plan: SubmissionPlan,
    cfg: Config,
    *,
    auto_confirm: bool = False,
    dry_run: bool | None = None,
    headless: bool = True,
    out_dir: str | None = None,
) -> ExecutionResult:
    """执行一份举报计划,返回 :class:`ExecutionResult`。

    - ``dry_run`` 缺省取 ``cfg.dry_run_default``;干跑不启动浏览器,逐 Step
      记录 notes,``ok=True``、``submitted=False``。
    - ``out_dir`` 缺省为 ``Path(cfg.data_dir)/"runs"/<时间戳>``,自动创建。
    - 真实模式惰性导入 playwright 并驱动 chromium;每步异常都会被捕获并记入
      notes,``stopped_at`` 指向中止步骤的 label,``ok=False``;已收集的截图
      仍随结果返回。
    - ``auto_confirm=True`` 仅用于测试:把 ``HUMAN_GATE`` 的交互等待替换为
      notes 记录,绝不用于绕过真实提交前的人工确认红线。
    """
    if dry_run is None:
        dry_run = bool(cfg.dry_run_default)

    if out_dir is not None:
        out_path = pathlib.Path(out_dir)
    else:
        out_path = pathlib.Path(cfg.data_dir) / "runs" / _dt.datetime.now().strftime(
            _RUN_TS_FORMAT
        )
    try:
        out_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # 目录不可建时继续执行,截图步骤会自行报错
        logger.warning("创建运行输出目录失败:%s(%s)", out_path, exc)

    result = ExecutionResult(portal=plan.portal.value)
    if dry_run:
        return _dry_run(plan, result)
    # V5:真实模式整体计时;提交成功(仅 submitted=True)计数
    with telemetry.timer(_METRIC_RUN):
        executed = _run_with_browser(
            plan, result, out_path, auto_confirm=auto_confirm, headless=headless
        )
    if executed.submitted:
        telemetry.inc(_METRIC_SUBMITTED)
    return executed


# ---------------------------------------------------------------------------
# 干跑(纯标准库,CI 可测)
# ---------------------------------------------------------------------------

def _dry_run(plan: SubmissionPlan, result: ExecutionResult) -> ExecutionResult:
    """干跑:不导入任何三方库,逐 Step 记录 notes。"""
    result.notes.append(DRY_RUN_BANNER)
    for i, step in enumerate(plan.steps, start=1):
        if step.action is StepAction.HUMAN_GATE:
            # 红线:人工门在干跑下同样不执行、更不提交,只记录"干跑跳过"
            line = (
                f"[DRY] {i}. {step.action.value} {step.label} "
                "干跑跳过(等待人工确认的环节未执行)"
            )
        else:
            line = (
                f"[DRY] {i}. {step.action.value} {step.label} "
                f"{step.selector or step.value}"
            ).rstrip()
        result.notes.append(line)
    result.ok = True
    result.submitted = False
    logger.info(
        "干跑完成:portal=%s steps=%d notes=%d",
        plan.portal.value,
        len(plan.steps),
        len(result.notes),
    )
    return result


# ---------------------------------------------------------------------------
# 真实模式(playwright 惰性导入)
# ---------------------------------------------------------------------------

def _run_with_browser(
    plan: SubmissionPlan,
    result: ExecutionResult,
    out_path: pathlib.Path,
    *,
    auto_confirm: bool,
    headless: bool,
) -> ExecutionResult:
    """驱动 chromium 逐步执行计划;异常捕获为 notes + stopped_at,不向外抛。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise ImportError(
            "真实执行模式需要 Playwright,请先安装:pip install playwright,"
            "并运行 playwright install chromium 下载浏览器内核"
        ) from exc

    result.notes.append(f"真实模式:计划共 {len(plan.steps)} 步,输出目录 {out_path}")
    logger.info(
        "真实模式执行:portal=%s steps=%d out_dir=%s headless=%s",
        plan.portal.value,
        len(plan.steps),
        out_path,
        headless,
    )

    playwright_inst: Any = None
    browser: Any = None
    try:
        playwright_inst = sync_playwright().start()
        try:
            browser = playwright_inst.chromium.launch(headless=headless)
        except Exception as exc:
            # chromium 未安装/启动失败:返回失败结果而非崩溃,便于上层 CLI 提示
            telemetry.inc(_METRIC_ERRORS)
            result.ok = False
            result.stopped_at = "browser.launch"
            result.notes.append(f"chromium 启动失败:{exc}")
            logger.warning("chromium 启动失败:%s", exc)
            return result

        page = browser.new_page()
        shot_errors = _screenshot_error_types()
        completed = True
        for idx, step in enumerate(plan.steps, start=1):
            stopped_label = step.label or f"step{idx}:{step.action.value}"
            try:
                _perform_step(
                    step,
                    page=page,
                    out_path=out_path,
                    idx=idx,
                    result=result,
                    auto_confirm=auto_confirm,
                    shot_errors=shot_errors,
                )
            except KeyboardInterrupt:
                # 人工门处 Ctrl+C = 人工主动取消
                result.ok = False
                result.stopped_at = stopped_label
                result.notes.append(
                    f"[取消] 第{idx}步 人工取消(Ctrl+C):{stopped_label}"
                )
                logger.info("人工取消:portal=%s stopped_at=%s", plan.portal.value, stopped_label)
                completed = False
                break
            except Exception as exc:
                telemetry.inc(_METRIC_ERRORS)
                result.ok = False
                result.stopped_at = stopped_label
                result.notes.append(
                    f"[错误] 第{idx}步({step.action.value} {step.label}):{exc}"
                )
                logger.warning("步骤失败(%s):%s", stopped_label, exc)
                completed = False
                break

        if completed:
            result.ok = True
            result.notes.append(f"计划执行完毕:共 {len(plan.steps)} 步")
            logger.info(
                "计划执行完毕:portal=%s submitted=%s screenshots=%d",
                plan.portal.value,
                result.submitted,
                len(result.screenshots),
            )
        return result
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:  # pragma: no cover - 清理失败不影响结果
                logger.warning("关闭浏览器失败", exc_info=True)
        if playwright_inst is not None:
            try:
                playwright_inst.stop()
            except Exception:  # pragma: no cover - 清理失败不影响结果
                logger.warning("停止 Playwright 失败", exc_info=True)


def _screenshot_error_types() -> tuple[type[BaseException], ...]:
    """截图步骤的窄捕获异常类型:``(OSError, playwright.sync_api.Error)``。

    Playwright 的截图 IO 失败 / 超时统一抛其 ``Error`` 子类;取不到时仅用
    ``OSError`` 兜底(调用方已确认 playwright 可导入,此分支仅防御)。
    """
    try:
        from playwright.sync_api import Error as _PlaywrightError
    except ImportError:  # pragma: no cover - 与调用方导入结果一致,仅防御
        return (OSError,)
    return (OSError, _PlaywrightError)


def _perform_step(
    step: Step,
    *,
    page: Any,
    out_path: pathlib.Path,
    idx: int,
    result: ExecutionResult,
    auto_confirm: bool,
    shot_errors: tuple[type[BaseException], ...] = (OSError,),
) -> None:
    """解释并执行单个 Step;抛出的异常由调用方统一捕获。

    ``shot_errors`` 为截图步骤的窄捕获异常类型(V5:截图保存失败不中断,
    见 :func:`_screenshot_error_types`)。
    """
    action = step.action

    if action is StepAction.GOTO:
        page.goto(step.value or step.text, timeout=int(step.timeout_s * 1000))
        result.notes.append(f"[OK] 第{idx}步 goto {step.label}".rstrip())

    elif action is StepAction.WAIT:
        # V12 反脆弱等待:未声明 wait_until → 固定盲睡(与历史行为逐字一致);
        # 声明了 → 事件驱动等待(超时退避重试一次 + 分级兜底链,全程写 notes)。
        wait_until = _step_wait_until_or_none(step)
        if wait_until is None:
            page.wait_for_timeout(int(float(step.value or step.timeout_s) * 1000))
            result.notes.append(f"[OK] 第{idx}步 wait {step.label}".rstrip())
        else:
            _event_driven_wait(step, wait_until, page=page, idx=idx, result=result)

    elif action is StepAction.SELECT:
        # V13 自愈:候选链回退(链长 <2 时零探测直通,v1 行为逐字不变)。
        kind, target = _resolve_step_target(step, page, idx, result)
        if kind == "css":
            page.select_option(target, step.value)
        else:
            target.select_option(step.value)
        result.notes.append(f"[OK] 第{idx}步 select {step.label}".rstrip())

    elif action is StepAction.FILL:
        if not step.value and step.meta.get("skippable"):
            result.notes.append(
                f"[跳过] 第{idx}步 可选字段为空,跳过填写:{step.label or step.selector}"
            )
            return
        kind, target = _resolve_step_target(step, page, idx, result)
        if kind == "css":
            page.fill(target, step.value)
        else:
            target.fill(step.value)
        result.notes.append(f"[OK] 第{idx}步 fill {step.label}".rstrip())

    elif action is StepAction.CLICK:
        if step.selector or (
            isinstance(step.meta, dict)
            and isinstance(step.meta.get("selector_chain"), list)
        ):
            kind, target = _resolve_step_target(step, page, idx, result)
        else:
            # 旧兜底:既无选择器也无候选链时按文本点击(行为与 v1 一致)。
            kind, target = "css", f"text={step.text}"
        if kind == "css":
            page.click(target)
        else:
            target.click()
        if _is_submit_click(step):
            # 提交按钮点击成功即置位;后续步骤异常不会撤销 submitted
            result.submitted = True
            result.notes.append(
                f"[提交] 第{idx}步 已点击提交按钮:{step.label or step.selector}"
            )
        else:
            result.notes.append(f"[OK] 第{idx}步 click {step.label}".rstrip())

    elif action is StepAction.SCREENSHOT:
        shot_path = out_path / f"step{idx:02d}_{action.value}.png"
        try:
            page.screenshot(path=str(shot_path), full_page=True)
        except shot_errors as exc:
            # V5:截图是佐证性 IO——窄捕获保存失败,记 warning 与计数后继续,
            # 绝不因截图失败中断举报提交主流程
            telemetry.inc(_METRIC_SHOT_FAILURES)
            logger.warning("第%d步截图保存失败,继续执行后续步骤:%s", idx, exc)
            result.notes.append(
                f"[截图失败] 第{idx}步 {shot_path.name} 保存失败,已跳过:{exc}"
            )
            return
        result.screenshots.append(str(shot_path))
        result.notes.append(f"[截图] 第{idx}步 已保存 {shot_path.name}")

    elif action is StepAction.FOCUS:
        # V10.1:半自动焦点——光标聚焦到目标元素(验证码框),用户直接打字
        if step.selector:
            try:
                page.focus(step.selector, timeout=2000)
                result.notes.append(f"[聚焦] 第{idx}步 已聚焦 {step.selector}")
            except Exception as exc:  # noqa: BLE001 - 聚焦失败不影响后续步骤
                result.notes.append(f"[聚焦] 第{idx}步 聚焦失败(忽略):{exc}")

    elif action is StepAction.HUMAN_GATE:
        # 红线:验证码/人工核对环节只等待人工,绝不自动识别或填写
        gate_label = step.label or "人工核对"
        if auto_confirm:
            result.notes.append(f"[人工门] 第{idx}步 {gate_label} 自动确认(测试模式)")
        else:
            prompt = f"【人工确认】{gate_label} 完成后按回车继续,Ctrl+C 取消:"
            input(prompt)
            result.notes.append(f"[人工门] 第{idx}步 {gate_label} 已人工确认")

    else:  # pragma: no cover - 契约枚举之外的防御分支
        raise ValueError(f"未知步骤类型:{action!r}")


def _is_submit_click(step: Step) -> bool:
    """判断一次 CLICK 是否为"提交"动作(用于置位 ``submitted``)。

    依据二选一:Step.meta 显式标记 ``submit``,或选择器等于契约提交按钮
    ``#report-submit``(CONTRACTS §3)。
    """
    if step.meta.get("submit"):
        return True
    return step.selector.strip() == SUBMIT_SELECTOR
