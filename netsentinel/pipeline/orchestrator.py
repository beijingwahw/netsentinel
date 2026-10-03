"""端到端编排器(NetSentinel · A18):scan / submit 两条主流程的汇合点。

- :func:`run_scan`:链接发现 → 逐页采样 → 多成员分类器评分 → 集成 → 站点判定;
  判定需人工复核(含 NSFW)时生成证据包、写入复核队列并记审计日志。
- :func:`run_submit`:把一条**已人工确认(approved)**的复核记录提交到举报门户;
  强制频控(最小间隔 / 每日上限),执行成功后才回写队列并记录提交时间戳。

安全红线(必须遵守,违反即缺陷):
1. ``run_submit`` 前置校验 entry 状态必须为 ``approved``,人工确认不可绕过;
2. 频控由本模块强制接入,被拒时直接返回 ``ok=False``,不进入执行器;
3. ``auto_confirm`` 仅作为参数透传给执行器(供测试 fake 使用),人工门
   (HUMAN_GATE)由执行器落实,本模块绝不绕过;
4. 兄弟模块一律**函数内惰性导入**:缺失时抛中文 ``RuntimeError``
   ("模块 netsentinel.xxx 未就位"),保证并行开发期本模块可独立导入与测试。

测试约定:所有外部依赖都收敛到模块级 ``_default_*()`` 工厂函数(内部经
``_load()`` 惰性导入兄弟模块),测试通过 ``monkeypatch`` 替换这些工厂即可
全离线运行,无需兄弟模块就位。

V5:关键入口接入 ``netsentinel.telemetry``(scan.total / scan.verdict.<档位> /
scan.members_seconds / submit.total 等,只记名称与数字,不记 URL 内容)。

校准驱动的动态集成(可选增强,默认关闭 = 等权,旧行为):``cfg`` 附加属性
``ensemble_reliability_weights`` 为 True(经 ``netsentinel.config`` 的 YAML
开关注入)且本地可靠性跟踪器样本充足(>= ``decision.reliability.MIN_N``)时,
主链路按 Brier 反比权重 ``w = 1/(brier+ε)`` 对各成员加权集成——报得准的
成员更有话语权、离群成员被降权;样本不足 / 内核未就位 / 读反馈失败一律
自动回退等权,绝不阻断扫描。详见 :func:`_reliability_ensemble_weights`。

贝叶斯权重回流(可选增强,默认关闭 = 现状):``cfg.bayes_reliability``
(V14 收录为一等 Config 字段,缺省 False;getattr 读取兼容收录前的实例
属性注入惯例)为 True 时,ensemble 权重改用
``decision.reliability.BayesianReliabilityTracker``
(V13)的分层收缩后验权重——**同一份** ``cfg.data_dir/reliability.jsonl`` 运营者
反馈流(Brier record 流)逐行重放为 correctness 事件(映射 ``correct =
(p >= 0.5) 与 outcome 一致``,见 :func:`_default_bayes_reliability_tracker`)。
**优先级高于旧 Brier 开关 ``ensemble_reliability_weights``**:两开关同开时
贝叶斯优先生效(Brier 开关被忽略);两开关全关 = 等权现状逐字节不变。
``cfg.bayes_half_life``(V14 一等字段,单位天;None = 关闭遗忘 = 收录前
现状)为正有限数时重开**指数遗忘**——事件时刻取 jsonl 行内可选 ``ts``
字段(V14 record 写侧落盘,Unix epoch 秒),缺 ts 旧行回退确定性滴答。
零反馈记录回退等权(计数 ``scan.ensemble_weights.bayes_fallback``);全员
冷启动(n_eff < MIN_N)由 V13 收缩吸收、自然退化为等权数值。计数
``scan.ensemble_weights.bayes`` / ``bayes_fallback`` / ``equal_fallback``
分别对应"贝叶斯生效 / 贝叶斯零记录回退 / Brier 样本不足回退",互不污染。
详见 :func:`_bayes_ensemble_weights`。

站群图谱通电(A194 可选增强,默认关闭 = 现状):``cfg`` 附加属性
``graph_wire`` 为 True 时,扫描批末经 :mod:`netsentinel.pipeline.kernel_wire`
把本站图片 sha256(``add_image`` + ``link_shared_images``)、页面模板指纹
(``add_template`` + ``link_templates``)与 LSH 近重复(``phash_near`` 边)
写入 A46 站点关联图谱;写图失败安全降级(telemetry 计数),扫描结论与
复核 / 审计主流程完全不受影响。详见 :func:`_default_graph_wire`。

全链追踪通电(可选增强,默认关闭 = 现状):``cfg`` 附加属性
``trace_enabled`` 为 True 时,``run_scan`` 经 :mod:`netsentinel.telemetry_trace`
开一条新 trace 包住 fetch → vlm → fusion → review 全链(``scan.fetch`` /
``scan.vlm`` / ``scan.fusion`` / ``scan.review`` 四个关键 span),span 结束
双写 telemetry timer 并经 ``configure(audit_sink=...)`` 把七键 span 载荷
转投本扫描的 JSONL 审计日志(``logging_util.JsonlAuditLogger.log_event``,
惰性注入,扫描结束复位);trace_id 写进 ``report.intel["trace_id"]`` 供上层
:func:`telemetry_trace.export_trace_json` 导出 span 树。计数
``scan.trace.enabled`` / ``scan.trace.disabled``。详见 :func:`run_scan`。

分歧弃权接线(可选增强,默认关闭 = 现状):``cfg`` 附加属性 ``abstain_enabled``
为 True 时,ensemble 计分区的成员分数按图片分组经
:func:`netsentinel.decision.abstain.batch_decide` 判定(阈值经
``cfg.abstain_threshold`` 可调,缺省 0.35 对齐 arbiter ``DISAGREE_GAP``);
任一图片成员分歧达到阈值即**弃权**:置 ``needs_review=True``(clean 站点也
直送人工复核)并把 ``to_triage_hint`` 的 ``priority_weight`` 写进
``report.intel["abstain"]``、随入队传给复核队列提权。**verdict 三档判定
公式(clean/suspect/nsfw)零改动**——弃权是复核优先信号,不是第四档。
详见 :func:`_wire_abstain`。

用法示例::

    from netsentinel.contracts import Config
    from netsentinel.pipeline.orchestrator import run_scan

    report = run_scan("http://localhost/a", Config())  # 判定需复核时自动入列
    print(report.verdict, report.needs_review)
"""
from __future__ import annotations

import contextlib
import importlib
import json
import logging
import math
import pathlib
import re
import struct
import time
import urllib.parse
from typing import Any, Callable, Protocol

from netsentinel import telemetry
from netsentinel.contracts import (
    Config,
    ExecutionResult,
    ImageEvidence,
    ImageScore,
    PageSample,
    Portal,
    SiteReport,
)

__all__ = ["run_scan", "run_submit"]

logger = logging.getLogger(__name__)

#: 可注入的页面抓取函数签名:(url, cfg) -> (status, html, final_url)
FetchPageFn = Callable[[str, Config], tuple[int, str, str]]

#: 可注入的页面采样函数签名:capture_page(url, cfg, *, fetch_page=None) -> PageSample
CaptureFn = Callable[..., PageSample]

#: 可注入的执行器签名:execute(plan, cfg, *, auto_confirm, dry_run) -> ExecutionResult
ExecutorFn = Callable[..., ExecutionResult]


class ClassifierLike(Protocol):
    """注入用分类器最小协议(与 vision.classifier_base.NsfwClassifier 兼容)。"""

    name: str

    def classify_batch(self, imgs: list[ImageEvidence]) -> list[ImageScore]: ...


def _load(module_name: str) -> Any:
    """惰性导入兄弟模块;缺失时抛中文 RuntimeError。"""
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(f"模块 {module_name} 未就位:{exc}") from exc


# ---------------------------------------------------------------------------
# 默认实现工厂(模块级,便于测试 monkeypatch 稳定替换)
# ---------------------------------------------------------------------------
def _default_discover(
    url: str, cfg: Config, fetch_page: FetchPageFn | None = None
) -> list[str]:
    """链接发现:同 host BFS,取前 max_pages 个页面(含起点)。"""
    site_map = _load("netsentinel.crawler.site_map")
    return site_map.discover_links(url, cfg, fetch_page)


def _default_capture(
    url: str, cfg: Config, *, fetch_page: FetchPageFn | None = None
) -> PageSample:
    """单页采样:截图 + 图片证据下载 + 文本线索命中。

    V2:``cfg.capture_engine == "v2"`` 时改用增强采集(滚动触发懒加载)。
    """
    if getattr(cfg, "capture_engine", "v1") == "v2":
        capture_v2 = _load("netsentinel.crawler.capture_v2")
        return capture_v2.capture_page_v2(url, cfg, fetch_page=fetch_page)
    browser = _load("netsentinel.crawler.browser")
    return browser.capture_page(url, cfg, fetch_page=fetch_page)


def _default_classifier(name: str, cfg: Config) -> Any:
    """按名称从分类器注册表取实例(未安装的三方成员会抛异常,由调用方跳过)。"""
    classifier_base = _load("netsentinel.vision.classifier_base")
    return classifier_base.get_classifier(name, cfg)


def _default_ensemble(
    scores: list[ImageScore], weights: dict[str, float] | None = None
) -> list[ImageScore]:
    """多成员评分 → 逐图集成评分(model='ensemble')。

    :param weights: 模型名 -> 权重映射(校准驱动动态集成开启时传入);
        ``None`` = 等权(与旧版调用口径完全一致)。
    """
    ensemble = _load("netsentinel.vision.ensemble")
    return ensemble.ensemble_scores(scores, weights=weights)


#: 可靠性反馈 jsonl 文件名(挂接 ``cfg.data_dir`` 下;与 pipeline.kernel_wire
#: 的 V7 融合内核同一约定,保证集成权重与融合加权读到同一份运营者反馈)。
RELIABILITY_JSONL_NAME = "reliability.jsonl"


def _default_reliability_tracker(cfg: Config) -> Any:
    """打开可靠性跟踪器(只读 ``cfg.data_dir/reliability.jsonl`` 的本地反馈)。

    文件缺失 = 无样本 → :func:`_ensemble_weights_from_tracker` 回退等权;
    绝不联网取数、绝不写回(零外呼,与 V7 数据面红线同口径)。
    """
    reliability = _load("netsentinel.decision.reliability")
    tracker_path = str(pathlib.Path(cfg.data_dir) / RELIABILITY_JSONL_NAME)
    return reliability.ReliabilityTracker(tracker_path)


def _bayes_correct(p: Any, outcome: Any) -> bool | None:
    """单条 record 流字段 → correctness 布尔;字段缺失/类型非法返回 None(跳过该行)。

    jsonl 现有结构(:meth:`ReliabilityTracker.record` 落盘的三键行,先读
    ``decision/reliability.py`` 的 ``_ENTRY_KEYS`` 后定的映射;V14 起行内
    可携带可选 ``ts`` 时刻键,与本映射无关)::

        {"provider": "glm", "p": 0.9, "outcome": true}

    贝叶斯追踪器消费 ``(member, correct)`` 事件流,correctness 映射为
    **成员二值判定与事实的一致性**::

        correct = (p >= 0.5) == outcome

    - ``p >= 0.5`` 计为成员判"违规"(0.5 为闭边界的二值判定线),与
      ``outcome``(复核事实:True = 确为违规)一致即"对";
    - 低分报正常图(p=0.3, outcome=false)与高分报违规图(p=0.9,
      outcome=true)同为"对";误报与漏报同为"错"——与 Brier 口径
      (p 距 outcome 的平方距离)同源、但压缩为对/错二值,供 Beta 后验计数。
    """
    if isinstance(p, bool) or not isinstance(p, (int, float)):
        return None
    if isinstance(outcome, bool):
        flag = outcome
    elif isinstance(outcome, int) and outcome in (0, 1):  # bool 已被上面截走
        flag = bool(outcome)
    else:
        return None
    q = float(p)
    if math.isnan(q) or math.isinf(q):
        return None
    return (q >= 0.5) == flag


def _bayes_ts(ts: Any) -> float | None:
    """record 流可选 ``ts`` 字段 → 事件时刻(Unix epoch 秒)。

    - 缺字段(``None``)→ 返回 ``None``:调用方回退 tracker 缺省滴答
      (兼容收录前对无 ts 流的重放语义);
    - 存在但非法(布尔/非数字/NaN/inf)→ 抛中文 ``ValueError``,调用方
      按坏行整行跳过(与 p/outcome 非法同口径)。
    """
    if ts is None:
        return None
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        raise ValueError(f"ts 必须是数字,当前值:{ts!r}")
    q = float(ts)
    if math.isnan(q) or math.isinf(q):
        raise ValueError("ts 必须是有限数(非 NaN/inf)")
    return q


def _default_bayes_reliability_tracker(cfg: Config) -> Any:
    """打开贝叶斯分层可靠性跟踪器(同一份 reliability.jsonl 数据源适配)。

    与 :func:`_default_reliability_tracker` 读**同一份** Brier record 流
    (``cfg.data_dir/reliability.jsonl``),逐行按 :func:`_bayes_correct` 映射
    重放为 correctness 事件::

        {"provider": "good", "p": 1.0, "outcome": true}
          → record("good", correct=True)    # 报 1.0 且确为违规:对
        {"provider": "calm", "p": 0.3, "outcome": false}
          → record("calm",  correct=True)   # 低报正常图:对
        {"provider": "wolf", "p": 0.9, "outcome": false}
          → record("wolf",  correct=False)  # 误报:错

    - **半衰期(V14 遗忘重开)**:经 ``cfg.bayes_half_life``(一等字段,
      getattr 兼容旧注入;单位**天**)读取——``None``(缺省)= 关闭遗忘
      (现状:每条事件权重恰为 1.0,α/β 即对/错计数 + Beta(1,1) 先验,
      手算可复核);正有限数 = 构造带半衰期的 tracker,天→秒换算
      (``days × 86400``)——:class:`BayesianReliabilityTracker` 的
      half_life 量纲与 ts 同构,record 流 ts 字段为 Unix epoch 秒,
      两者必须同单位才能做 ``exp(-ln2·Δt/half_life)`` 衰减。分层收缩
      κ 恒取缺省 ``DEFAULT_KAPPA`` = 10.0;
    - **事件时刻(V14)**:取自行内可选 ``ts`` 字段(Unix epoch 秒,
      :meth:`ReliabilityTracker.record` 写侧落盘)——有 ts 的行按真实时刻
      重放;缺 ts 的旧行回退 tracker 缺省滴答(上一条 + 1.0,首条 1.0,
      零墙钟)并计数(重放完成日志给出回退条数;滴答时刻 ≈ 0,在
      epoch 秒量纲下距当前千万半衰期级,遗忘开启时旧行自然衰减殆尽);
      ts 存在但非法的行按坏行跳过;
    - 损坏 / 缺字段 / 值非法的行跳过并记 debug 日志(与 Brier ``_load``
      同口径),单行脏数据不拖垮整条反馈环;
    - 文件缺失 = 零事件 → :func:`_ensemble_weights_from_tracker` 回退等权;
      绝不联网取数、绝不写回(零外呼,与 V7 数据面红线同口径)。
    """
    reliability = _load("netsentinel.decision.reliability")
    half_life_days = getattr(cfg, "bayes_half_life", None)
    half_life_seconds: float | None = (
        None if half_life_days is None else float(half_life_days) * 86400.0
    )
    tracker = reliability.BayesianReliabilityTracker(half_life=half_life_seconds)
    path = pathlib.Path(cfg.data_dir) / RELIABILITY_JSONL_NAME
    replayed = 0
    tick_rows = 0
    if path.is_file():
        with path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                raw = line.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                    if not isinstance(obj, dict):
                        raise ValueError("非对象行")
                    provider = obj.get("provider")
                    if not isinstance(provider, str) or not provider.strip():
                        raise ValueError("provider 非法")
                    correct = _bayes_correct(obj.get("p"), obj.get("outcome"))
                    if correct is None:
                        raise ValueError("p/outcome 非法")
                    when = _bayes_ts(obj.get("ts"))
                except (ValueError, TypeError, json.JSONDecodeError):
                    logger.debug(
                        "跳过无法映射为 correctness 事件的反馈行:%s 第 %d 行",
                        path,
                        lineno,
                    )
                    continue
                if when is None:
                    # 缺 ts 旧行:回退确定性滴答(兼容收录前的重放语义)。
                    tracker.record(provider.strip(), correct)
                    tick_rows += 1
                else:
                    tracker.record(provider.strip(), correct, ts=when)
                replayed += 1
    logger.debug(
        "贝叶斯可靠性事件重放完成:%d 条(缺 ts 回退滴答 %d 条,"
        "半衰期 %s 秒,源 %s)",
        replayed,
        tick_rows,
        half_life_seconds,
        path,
    )
    return tracker


def _ensemble_weights_from_tracker(
    ok_members: list[str], tracker: Any
) -> dict[str, float] | None:
    """可靠性权重适配层:``tracker.weights()`` → 覆盖本轮成功成员的权重映射。

    ``decision.reliability.ReliabilityTracker`` 与 ``vision.ensemble`` 均为
    冻结不改的兄弟模块,这里只读 ``weights()`` 现有接口并做两步适配:

    1. **均值兜底**:样本充足(n >= ``reliability.MIN_N``,``weights()`` 返回
       非 None)的成员直接取其 Brier 反比归一权重;缺失 / 样本不足(值为
       ``None``)的成员按已知权重的算术均值兜底——不奖不罚的中性待遇;
    2. **重归一**:按本轮成功成员全集重新归一,**Σ w = 1**(不变量;已知
       成员之间的权重比值在重归一下保持不变)。

    :return: 权重映射;tracker 无任何样本充足提供方(全 ``None`` / 空)或
        成员列表为空时返回 ``None``,由调用方回退等权(旧行为)。
    """
    raw: dict[str, Any] = dict(tracker.weights())
    known: dict[str, float] = {
        str(m): float(w) for m, w in raw.items() if w is not None
    }
    if not known:
        return None
    members = list(dict.fromkeys(str(m) for m in ok_members))  # 去重保序
    if not members:
        return None
    fallback = sum(known.values()) / len(known)
    merged = {m: known.get(m, fallback) for m in members}
    total = sum(merged.values())
    if total <= 0.0:  # 理论不可达(known 值恒正),防御性回退等权
        return None
    return {m: w / total for m, w in merged.items()}


def _bayes_ensemble_weights(
    cfg: Config, ok_members: list[str]
) -> dict[str, float] | None:
    """贝叶斯可靠性权重回流(``cfg.bayes_reliability`` 开关门控,默认关 = 现状)。

    与 Brier 路径(:func:`_reliability_ensemble_weights`)共用 V10.4 适配层
    :func:`_ensemble_weights_from_tracker`——缺失成员均值兜底 + 按本轮成功
    成员重归一(Σ = 1)的不变量原样沿用;bayes ``weights()`` 恒为数值
    (V13 冷启动由分层收缩吸收,无 None 形状),仅**零事件流**(空 jsonl /
    文件缺失)使适配层返回 ``None`` → 回退等权。全员冷启动(n_eff <
    ``MIN_N``)时收缩把各成员拉平到全局池均值,权重自然退化为等权数值。

    遥测语义(与 Brier 路径分列,互不污染):

    - ``scan.ensemble_weights.bayes``:贝叶斯加权集成生效(含冷启动等权值);
    - ``scan.ensemble_weights.bayes_fallback``:开关开启但零可用权重
      (反馈零记录)→ 回退等权;
    - ``scan.ensemble_weights.skipped``:解析异常(内核未就位 / 文件不可读)
      → 告警 + 回退等权,绝不阻断扫描(与 Brier 路径同语义共用计数)。
    """
    try:
        tracker = _default_bayes_reliability_tracker(cfg)
        weights = _ensemble_weights_from_tracker(ok_members, tracker)
    except Exception as exc:  # noqa: BLE001 - 增强项,失败不阻断扫描
        telemetry.inc("scan.ensemble_weights.skipped")
        logger.warning("贝叶斯可靠性权重解析失败,本轮集成回退等权:%s", exc)
        return None
    if weights is None:
        telemetry.inc("scan.ensemble_weights.bayes_fallback")
        logger.info("贝叶斯反馈零记录(无 correctness 事件),本轮集成回退等权")
    else:
        telemetry.inc("scan.ensemble_weights.bayes")
        logger.info("贝叶斯可靠性加权集成生效:%s", weights)
    return weights


def _reliability_ensemble_weights(
    cfg: Config, ok_members: list[str]
) -> dict[str, float] | None:
    """按配置开关解析集成的可靠性权重;任何不满足都回退等权(``None``)。

    **开关优先级(V14 贝叶斯回流)**:``cfg.bayes_reliability``(V14 收录为
    Config 一等字段,缺省 False;getattr 读取兼容收录前的实例属性注入)
    开启时**优先生效**——转交 :func:`_bayes_ensemble_weights`,旧 Brier 开关
    ``ensemble_reliability_weights`` 同开时被忽略;仅 bayes 关闭且 Brier 开启
    才走 Brier 路径;两开关全关 → ``None`` = 等权,调用口径与旧版完全一致
    (向后兼容)。

    - Brier 开关开启:经 :func:`_default_reliability_tracker` 读本地反馈,
      样本充足时产出权重映射;样本不足自动回退等权;
    - 兄弟模块未就位 / 反馈文件损坏等任何异常:记 telemetry 与告警后降级
      等权,绝不阻断主链路(增强项失败不拖垮扫描)。
    """
    if getattr(cfg, "bayes_reliability", False):
        # 贝叶斯优先于 Brier(两开关同开时只走贝叶斯,不重复计数)。
        return _bayes_ensemble_weights(cfg, ok_members)
    if not getattr(cfg, "ensemble_reliability_weights", False):
        return None
    try:
        tracker = _default_reliability_tracker(cfg)
        weights = _ensemble_weights_from_tracker(ok_members, tracker)
    except Exception as exc:  # noqa: BLE001 - 增强项,失败不阻断扫描
        telemetry.inc("scan.ensemble_weights.skipped")
        logger.warning("可靠性权重解析失败,本轮集成回退等权:%s", exc)
        return None
    if weights is None:
        telemetry.inc("scan.ensemble_weights.equal_fallback")
        logger.info("可靠性样本不足(低于 MIN_N),本轮集成回退等权")
    else:
        telemetry.inc("scan.ensemble_weights.reliability_weighted")
        logger.info("校准驱动的可靠性加权集成生效:%s", weights)
    return weights


def _default_assess(
    site_url: str,
    pages: list[PageSample],
    ensemble: list[ImageScore],
    cfg: Config,
) -> SiteReport:
    """按 CONTRACTS §4 公式做站点级判定。"""
    verdict = _load("netsentinel.decision.verdict")
    return verdict.assess(site_url, pages, ensemble, cfg)


def _default_build_bundle(report: SiteReport, cfg: Config) -> Any:
    """生成证据包(目录 + manifest + zip)。"""
    packager = _load("netsentinel.evidence.packager")
    return packager.build_bundle(report, cfg)


def _default_queue(db_path: str) -> Any:
    """打开人工复核队列(sqlite,标准库)。"""
    review_queue = _load("netsentinel.decision.review_queue")
    return review_queue.ReviewQueue(db_path)


def _default_audit_logger(path: str) -> Any:
    """打开 JSONL 审计日志器。"""
    logging_util = _load("netsentinel.logging_util")
    return logging_util.JsonlAuditLogger(path)


def _default_rate_limiter(state_path: str, min_interval_s: int, max_per_day: int) -> Any:
    """构造提交频控器。"""
    rate_limit = _load("netsentinel.submit.rate_limit")
    return rate_limit.RateLimiter(state_path, min_interval_s, max_per_day)


def _default_graph_wire(url: str, cfg: Config, pages: list[PageSample]) -> dict[str, Any]:
    """批末站群图谱通电:经 kernel_wire 装配层写 A46 图谱(惰性导入)。

    把本站图片 sha256 / 页面模板指纹灌入 ``EvidenceGraph`` 并折叠
    ``shared_image`` / ``shared_template`` / ``phash_near`` 关联边;由
    ``cfg.graph_wire`` 开关门控(附加实例属性,缺省 False = 现状零通电)。
    本工厂仅做转发,写图细节与降级语义见
    :func:`netsentinel.pipeline.kernel_wire.wire_graph_from_scan`。
    """
    kernel_wire = _load("netsentinel.pipeline.kernel_wire")
    return kernel_wire.wire_graph_from_scan(url, cfg, pages)


def _default_trace_module() -> Any:
    """全链追踪内核(``netsentinel.telemetry_trace``,惰性导入;测试可替换)。"""
    return _load("netsentinel.telemetry_trace")


def _default_abstain_module() -> Any:
    """分歧弃权内核(``netsentinel.decision.abstain``,惰性导入;测试可替换)。"""
    return _load("netsentinel.decision.abstain")


def _span(trace: Any | None, name: str, **attrs: Any) -> Any:
    """阶段 span 的零开销直通包装:trace 内核为 None(开关关)时直通。

    ``attrs`` 只放阶段标签与计数数字(红线 17:不放 URL/密钥/路径等内容
    字段);span 体内异常由内核记录后原样重抛,本包装绝不吞。
    """
    if trace is None:
        return contextlib.nullcontext()
    return trace.span(name, attrs=attrs)


def _wire_trace_audit(trace: Any, cfg: Config) -> None:
    """把 span 审计旁路接到本扫描的 JSONL 审计日志器(惰性,只读 API)。

    经 :func:`telemetry_trace.configure(audit_sink=...)` 注入:七键 span
    载荷原样展开交给 ``logging_util.JsonlAuditLogger.log_event``(只调用
    其既有 API,不改其源);sink 抛出的任何异常由追踪内核安全吞掉并计数,
    绝不拖垮扫描。注意:configure 是模块级注入点,本扫描期间会覆盖此前
    配置的 sink,扫描结束由 :func:`_unwire_trace_audit` 复位。
    """
    audit = _default_audit_logger(cfg.audit_path)
    trace.configure(audit_sink=lambda payload: audit.log_event(**payload))


def _unwire_trace_audit(trace: Any) -> None:
    """扫描结束后复位模块级 audit_sink(不把本扫描的日志器泄漏给后续 trace)。

    复位等价于 ``configure(audit_sink=None)``——按上游 configure 语义,
    该调用同时把 id 工厂复位为默认 ``secrets.token_hex``(telemetry_trace
    冻结不改,此处如实遵守)。
    """
    trace.configure(audit_sink=None)


def _wire_abstain(
    raw_scores: list[ImageScore], report: SiteReport, cfg: Config
) -> dict[str, Any] | None:
    """分歧弃权接线(``cfg.abstain_enabled`` 开关门控,默认 False = 现状)。

    把 ensemble 计分区的**成员**原始评分按图片分组(路径首现序,同成员
    同图后值覆盖前值,与 ``vision.ensemble`` 同口径),逐图经
    :func:`netsentinel.decision.abstain.batch_decide` 判定分歧(阈值经
    ``cfg.abstain_threshold`` 覆盖,缺省 0.35);取分歧最大的图片判定作为
    站点代表:

    - 达到阈值(闭边界,单成员 / 空输入永不弃权)→ **弃权**:
      ``report.needs_review = True``(clean 站点也直送人工复核,证据包 /
      入列 / 提醒走既有分支),``to_triage_hint`` 的完整提示(含
      ``priority_weight`` = 分歧度)写进 ``report.intel["abstain"]``;
    - 未达阈值 → 无任何改动(与开关关闭的行为一致)。

    红线:**verdict 三档判定公式零改动**——本函数在判定完成之后才运行,
    只动 ``needs_review`` 标记与 ``intel`` 记录,``verdict`` / ``agg_nsw_prob``
    / ``nsw_image_count`` 原样保留;任何异常(内核未就位 / 分数非法等)
    记 telemetry 后安全跳过,绝不阻断扫描。

    :return: 弃权触发时返回 ``to_triage_hint`` 提示 dict,否则 None。
    """
    if not getattr(cfg, "abstain_enabled", False):
        return None
    try:
        abstain = _default_abstain_module()
        threshold = getattr(cfg, "abstain_threshold", None)
        if threshold is None:
            threshold = abstain.DEFAULT_ABSTAIN_THRESHOLD
        grouped: dict[str, dict[str, float]] = {}
        for s in raw_scores:
            model = str(s.model)
            if model == "ensemble":  # 集成结果不是成员分,防误传自我重复
                continue
            grouped.setdefault(s.image.path, {})[model] = float(s.nsfw_prob)
        decisions = abstain.batch_decide(
            [list(per_image.values()) for per_image in grouped.values()],
            threshold=threshold,
        )
        telemetry.inc("scan.abstain.evaluated")
        worst = max(decisions, key=lambda d: d.disagreement, default=None)
        if worst is None or not worst.abstain:
            return None
        report.needs_review = True
        hint = abstain.to_triage_hint(worst, p_nsfw=report.agg_nsw_prob)
        report.intel["abstain"] = hint
        telemetry.inc("scan.abstain.triggered")
        logger.info(
            "成员分歧 %.4f ≥ 阈值 %g:自动判定弃权,站点直送人工复核"
            "(判定档位 %s 保持不变)",
            worst.disagreement,
            worst.threshold,
            report.verdict.value,
        )
        return hint
    except Exception as exc:  # noqa: BLE001 - 弃权是增强项,失败不阻断扫描
        telemetry.inc("scan.abstain.skipped")
        logger.warning("分歧弃权接线已跳过(三档判定不受影响):%s", exc)
        return None


def _default_plan(entry_like: Any, portal: Portal, cfg: Config) -> Any:
    """按门户分派构建举报步骤计划(不含任何自动填验证码的步骤)。"""
    if portal == Portal.P12377:
        portal_12377 = _load("netsentinel.submit.portal_12377")
        return portal_12377.plan_12377(entry_like, cfg)
    if portal == Portal.SHDF:
        portal_shdf = _load("netsentinel.submit.portal_shdf")
        return portal_shdf.plan_shdf(entry_like, cfg)
    raise RuntimeError(f"不支持的举报门户:{portal!r}(仅支持 12377 / shdf)")


def _default_executor(
    plan: Any,
    cfg: Config,
    *,
    auto_confirm: bool = False,
    dry_run: bool | None = None,
) -> ExecutionResult:
    """用 playwright 执行器驱动计划(dry_run / 人工门由执行器落实)。"""
    executor_playwright = _load("netsentinel.submit.executor_playwright")
    return executor_playwright.execute(
        plan, cfg, auto_confirm=auto_confirm, dry_run=dry_run
    )


# ---------------------------------------------------------------------------
# scan 主流程
# ---------------------------------------------------------------------------
def _collect_member_scores(
    imgs: list[ImageEvidence],
    cfg: Config,
    classifier: ClassifierLike | None = None,
) -> tuple[list[ImageScore], list[str]]:
    """收集各成员分类器的原始评分。

    - ``classifier`` 注入时优先使用;缺省经 ``get_classifier(cfg.classifier, cfg)`` 获取;
    - 再遍历 ``cfg.ensemble_members`` 逐个取分类器(与已就位成员重名的跳过);
    - 未安装 / 构造失败 / 批量评分失败的成员记 warning 后跳过;
    - 至少要有一个成员成功完成评分,否则由调用方抛 RuntimeError。

    :return: (全部原始评分, 成功成员名列表)
    """
    instances: list[ClassifierLike] = []
    seen_names: set[str] = set()

    if classifier is not None:
        instances.append(classifier)
        seen_names.add(str(getattr(classifier, "name", "")) or cfg.classifier)
    else:
        try:
            inst = _default_classifier(cfg.classifier, cfg)
            instances.append(inst)
            seen_names.add(str(getattr(inst, "name", "")) or cfg.classifier)
        except Exception as exc:  # noqa: BLE001 - 主分类器不可用时交给成员循环兜底
            logger.warning("主分类器 %r 不可用:%s", cfg.classifier, exc)

    if cfg.ensemble_members:
        requested: list[str] = list(cfg.ensemble_members)
    elif classifier is None and cfg.classifier:
        requested = [cfg.classifier]
    else:
        requested = []

    for member in requested:
        if member in seen_names:
            logger.debug("集成成员 %r 与已就位分类器重名,跳过重复评分", member)
            continue
        try:
            inst = _default_classifier(member, cfg)
            instances.append(inst)
            seen_names.add(member)
        except Exception as exc:  # noqa: BLE001 - 成员库未安装属正常,跳过并告警
            logger.warning("集成成员分类器 %r 未就位,已跳过:%s", member, exc)

    raw_scores: list[ImageScore] = []
    ok_members: list[str] = []
    for inst in instances:
        name = str(getattr(inst, "name", "?"))
        try:
            raw_scores.extend(inst.classify_batch(imgs))
            ok_members.append(name)
            logger.info("分类器 %r 完成 %d 张图片评分", name, len(imgs))
        except Exception as exc:  # noqa: BLE001 - 单成员评分失败不拖垮整轮扫描
            logger.warning("分类器 %r 批量评分失败,已跳过:%s", name, exc)
    return raw_scores, ok_members


# ---------------------------------------------------------------------------
# 图片证据增强:补齐 fetcher 契约留白的宽高,并把哈希文件名还原为原始文件名
# ---------------------------------------------------------------------------
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

#: 还原后的原始文件名最大长度(超出视为异常名,保留哈希名)
_MAX_ORIGINAL_NAME_LEN = 120


def _sniff_image_size(path: pathlib.Path) -> tuple[int, int]:
    """从 PNG/GIF/JPEG 文件头嗅探像素宽高;无法识别返回 (0, 0)。"""
    try:
        with path.open("rb") as fh:
            head = fh.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
                w, h = struct.unpack(">II", head[16:24])
                return int(w), int(h)
            if head[:6] in (b"GIF87a", b"GIF89a"):
                w, h = struct.unpack("<HH", head[6:10])
                return int(w), int(h)
            if head[:2] == b"\xff\xd8":  # JPEG:逐段找 SOFn
                fh.seek(2)
                while True:
                    marker = fh.read(2)
                    if len(marker) < 2 or marker[0] != 0xFF:
                        return 0, 0
                    if marker[1] in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6,
                                     0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        data = fh.read(7)
                        if len(data) < 7:
                            return 0, 0
                        h, w = struct.unpack(">HH", data[3:7])
                        return int(w), int(h)
                    length = fh.read(2)
                    if len(length) < 2:
                        return 0, 0
                        # noqa: E501
                    seglen = struct.unpack(">H", length)[0]
                    if seglen < 2:
                        return 0, 0
                    fh.seek(seglen - 2, 1)
    except (OSError, struct.error):
        pass
    return 0, 0


def _enrich_image_evidence(
    img: ImageEvidence,
    *,
    _known_missing: set[str] | None = None,
    _renames: dict[str, str] | None = None,
) -> ImageEvidence:
    """就地增强一条图片证据:嗅探宽高 + 哈希文件名还原为 URL 原始文件名。

    fetcher 按契约以 sha256 前 16 位命名且宽高置 0;而判定公式按
    min_image_px 过滤、stub 分类器按文件名关键词打分,证据可读性也依赖
    原始名,这里统一补齐。文件不存在或改名失败一律静默保留原状。

    :param _known_missing: 批量调用方传入的"已知不存在路径"集合(V5 性能
        优化:同一轮扫描里重复路径只 stat 一次);缺省 None 表示单条调用,
        行为与旧版完全一致。
    """
    p = pathlib.Path(img.path)
    path_str = str(img.path)
    if _known_missing is not None and path_str in _known_missing:
        return img  # 本轮已确认缺失的路径:跳过重复 stat(V5 消重)
    if not p.is_file():
        # 同一物理文件被多条证据引用时,首条已完成"哈希名→原始名"重命名,
        # 其余证据的旧路径随之失效:经映射改指新路径(修 V7 演习发现的搁浅路径缺陷)。
        mapped = (_renames or {}).get(path_str)
        if mapped and pathlib.Path(mapped).is_file():
            img.path = mapped
            p = pathlib.Path(mapped)
        else:
            if _known_missing is not None:
                _known_missing.add(path_str)
            return img
    if not img.width or not img.height:
        w, h = _sniff_image_size(p)
        if w or h:
            img.width, img.height = w, h
    stem = p.stem.lower()
    if not _HEX16.fullmatch(stem):
        return img
    raw_base = urllib.parse.urlparse(img.url).path.rsplit("/", 1)[-1]
    base = _UNSAFE_NAME.sub("_", raw_base)
    if not base or base == p.name or "." not in base or len(base) > _MAX_ORIGINAL_NAME_LEN:
        return img
    target = p.with_name(base)
    if target.exists():
        return img
    try:
        p.rename(target)
        img.path = str(target)
        if _renames is not None:
            _renames[path_str] = str(target)
    except OSError:
        pass
    return img


def _enrich_images(imgs: list[ImageEvidence]) -> None:
    """批量增强图片证据(V5):对不存在文件的 stat 调用按路径消重。

    同一轮扫描里,多条证据可能指向同一本地路径(如重复下载/重复引用);
    缺失路径只需 stat 一次即可全部跳过。文件确实存在的路径不缓存,
    保证每条证据的宽高嗅探 / 改名逻辑与逐条调用完全一致。
    """
    known_missing: set[str] = set()
    renames: dict[str, str] = {}
    for img in imgs:
        _enrich_image_evidence(img, _known_missing=known_missing, _renames=renames)


def _scan_once(
    url: str,
    cfg: Config,
    *,
    fetch_page: FetchPageFn | None = None,
    capture: CaptureFn | None = None,
    classifier: ClassifierLike | None = None,
    trace: Any | None = None,
) -> SiteReport:
    """run_scan 的主体(拆出以便外层统一计时,行为与旧版逐行一致)。

    :param trace: 追踪内核(``telemetry_trace`` 模块);None(默认,开关
        关)时所有阶段 span 直通,行为与旧版完全一致。
    """
    logger.info("开始扫描站点:%s(max_pages=%d)", url, cfg.max_pages)

    with _span(trace, "scan.fetch", stage="fetch"):
        urls = _default_discover(url, cfg, fetch_page)
        logger.info("链接发现完成,待抽样页面 %d 个", len(urls))

        cap = capture if capture is not None else _default_capture
        pages: list[PageSample] = [cap(u, cfg, fetch_page=fetch_page) for u in urls]

        imgs: list[ImageEvidence] = []
        for page in pages:
            imgs.extend(page.image_evidences)
        _enrich_images(imgs)
        logger.info("页面采样完成:%d 页 / %d 张图片", len(pages), len(imgs))

    with _span(trace, "scan.vlm", stage="vlm", members=len(cfg.ensemble_members or [])):
        members_started = time.perf_counter()
        raw_scores, ok_members = _collect_member_scores(imgs, cfg, classifier)
        telemetry.gauge("scan.members_seconds", time.perf_counter() - members_started)
        if not ok_members:
            members = list(cfg.ensemble_members) or [cfg.classifier]
            telemetry.inc("scan.errors")
            raise RuntimeError(
                f"没有任何分类器成员成功完成评分,无法判定站点:{url}"
                f"(请求的成员:{', '.join(map(str, members))};"
                f"请检查 netsentinel.vision.* 相关模块或三方库是否就位)"
            )

        # 校准驱动的动态集成:开关开启且可靠性样本充足(MIN_N)时按可靠性
        # 权重加权(bayes_reliability 开启走贝叶斯收缩权重并优先于 Brier 开关;
        # 关闭 / 样本不足 / 内核未就位一律回退等权)——等权分支的调用口径与
        # 旧版逐字一致(不传 weights),保证默认行为零变化。
        ensemble_weights = _reliability_ensemble_weights(cfg, ok_members)
        if ensemble_weights is not None:
            ensemble = _default_ensemble(raw_scores, ensemble_weights)
        else:
            ensemble = _default_ensemble(raw_scores)
        report = _default_assess(url, pages, ensemble, cfg)

    # V2:特征融合(URL 情报 + 页面级 VLM 理解,只升不降)。
    # 兄弟模块未就位/VLM 离线时自动跳过,不影响 v1 行为。
    with _span(trace, "scan.fusion", stage="fusion"):
        if getattr(cfg, "use_fusion", False):
            try:
                url_feat = _load("netsentinel.intel.url_intel").url_features(url)
                page_vlm_feat: dict = {"page_nsfw_prob": None}
                shot = next(
                    (p.screenshot_path for p in pages if p.screenshot_path), None
                )
                if shot:
                    page_vlm_feat = _load("netsentinel.vision.page_vlm").assess_page_screenshot(shot, cfg)
                report = _load("netsentinel.decision.fusion").fuse(
                    report, url_feat, {}, page_vlm_feat, cfg
                )
                logger.info(
                    "融合分析完成:fused=%.4f(图像 agg=%.4f 保持不变)",
                    report.intel.get("fusion", {}).get("prob", 0.0),
                    report.agg_nsw_prob,
                )
            except Exception as exc:  # noqa: BLE001 —— 融合是增强项,失败不阻断主流程
                telemetry.inc("scan.fusion.skipped")
                logger.warning("融合分析已跳过:%s", exc)

    # 分歧弃权接线(默认关闭 = 现状):判定完成之后运行,只动 needs_review
    # 标记与 intel 记录,三档判定 / 聚合分 / 计数原样保留(红线)。
    abstain_hint = _wire_abstain(raw_scores, report, cfg)

    # V5 可观测:按最终判定档位计数,并记录本轮抽样规模。
    telemetry.inc(f"scan.verdict.{report.verdict.value}")
    telemetry.gauge("scan.pages", len(pages))
    telemetry.gauge("scan.images", len(imgs))

    logger.info(
        "站点判定完成:%s verdict=%s agg=%.4f count=%d needs_review=%s",
        url,
        report.verdict.value,
        report.agg_nsw_prob,
        report.nsw_image_count,
        report.needs_review,
    )

    with _span(trace, "scan.review", stage="review"):
        entry_id: int | None = None
        bundle_zip = ""
        if report.needs_review:
            bundle = _default_build_bundle(report, cfg)
            bundle_zip = str(getattr(bundle, "zip_path", "") or "")
            queue = _default_queue(cfg.db_path)
            if abstain_hint is not None:
                # 弃权站点入队附带提权加项(分歧度);复核队列据此在
                # triage 排序中提前。默认路径不传该形参,调用口径与旧版一致。
                entry_id = int(
                    queue.add(
                        report,
                        bundle_zip,
                        priority_weight=float(abstain_hint["priority_weight"]),
                    )
                )
            else:
                entry_id = int(queue.add(report, bundle_zip))
            telemetry.inc("scan.enqueued")
            logger.info(
                "已生成证据包并写入人工复核队列:id=%s zip=%s(机器初筛,人工拍板)",
                entry_id,
                bundle_zip,
            )
            # V2:待复核提醒(未配置 webhook 时静默跳过,绝不重试)。
            try:
                _load("netsentinel.notify.hub").notify(
                    cfg,
                    "pending_review",
                    f"{url} 判定={report.verdict.value} 聚合分={report.agg_nsw_prob:.2f}",
                    entry_id=entry_id,
                    zip=bundle_zip,
                )
            except Exception as exc:  # noqa: BLE001
                telemetry.inc("scan.notify.errors")
                logger.warning("复核提醒发送失败(已忽略):%s", exc)

    # 批末站群图谱通电(A194 可选增强,默认关闭):把本站素材指纹灌入
    # A46 图谱(shared_image / shared_template / phash_near 关联边)。
    # ``cfg.graph_wire`` 为契约外附加实例属性,缺省 False → 完全不走此
    # 分支(旧行为零变化);写图任何失败安全降级,扫描结论不受影响。
    if getattr(cfg, "graph_wire", False):
        try:
            _default_graph_wire(url, cfg, pages)
            telemetry.inc("scan.graph_wire.ok")
        except Exception as exc:  # noqa: BLE001 - 通电是增强项,失败绝不中断扫描
            telemetry.inc("scan.graph_wire.skipped")
            logger.warning("站群图谱通电已跳过(扫描结论不受影响):%s", exc)

    audit = _default_audit_logger(cfg.audit_path)
    audit_fields: dict[str, Any] = {}
    if trace is not None:
        # 追踪开启时审计事件附 trace_id(16 hex,非内容字段),供与 span
        # 树对账;关闭时字段缺省,审计载荷与旧版逐字一致。
        audit_fields["trace_id"] = trace.current_trace_id()
    audit.log_event(
        "scan",
        site=url,
        verdict=report.verdict.value,
        agg=report.agg_nsw_prob,
        needs_review=report.needs_review,
        pages=len(pages),
        images=len(imgs),
        entry_id=entry_id,
        **audit_fields,
    )
    return report


def run_scan(
    url: str,
    cfg: Config,
    *,
    fetch_page: FetchPageFn | None = None,
    capture: CaptureFn | None = None,
    classifier: ClassifierLike | None = None,
) -> SiteReport:
    """对单个站点做端到端抽样识别,返回站点级判定报告。

    流程:discover_links → 逐页 capture_page → 汇总图片证据 →
    各成员分类器 classify_batch → ensemble_scores → assess。
    判定 needs_review(含 NSFW)时:build_bundle → ReviewQueue.add → 审计;
    无需复核(CLEAN)同样记录 "scan" 审计事件。

    校准驱动的动态集成(可选):``cfg.ensemble_reliability_weights`` 开启且
    可靠性样本充足时,ensemble 按各成员 Brier 反比权重加权(离群成员降权);
    ``cfg.bayes_reliability`` 开启时改用贝叶斯收缩权重并**优先于** Brier 开关;
    任何不满足自动回退等权(与旧行为一致),详见
    :func:`_reliability_ensemble_weights`。

    全链追踪(可选,``cfg.trace_enabled`` 缺省 False = 现状零变化):开一条
    新 trace 包住整条扫描链(``scan.fetch`` / ``scan.vlm`` / ``scan.fusion``
    / ``scan.review`` 阶段 span,异常 span 记录后原样重抛),span 载荷经
    audit_sink 转投本扫描的 JSONL 审计日志,trace_id 写进
    ``report.intel["trace_id"]`` 供上层 ``export_trace_json(trace_id)`` 导出
    span 树;计数 ``scan.trace.enabled`` / ``scan.trace.disabled``。

    V5 可观测:整体耗时记 ``telemetry.timer("scan.total")``,判定档位计
    ``scan.verdict.<clean|suspect|nsfw>``,成员评分循环耗时记 gauge
    ``scan.members_seconds``(只记名称与数字,不记 URL 内容)。

    :param fetch_page: 注入的抓取函数(透传给链接发现与页面采样)。
    :param capture: 注入的单页采样函数;缺省用 crawler.browser.capture_page。
    :param classifier: 注入的分类器实例;缺省按 ``cfg.classifier`` 取。
    :raises RuntimeError: 没有任何分类器成员成功完成评分,或兄弟模块未就位。
    """
    with telemetry.timer("scan.total"):
        if not getattr(cfg, "trace_enabled", False):
            telemetry.inc("scan.trace.disabled")
            return _scan_once(
                url, cfg, fetch_page=fetch_page, capture=capture, classifier=classifier
            )
        telemetry.inc("scan.trace.enabled")
        trace = _default_trace_module()
        _wire_trace_audit(trace, cfg)
        try:
            with trace.new_trace() as trace_id:
                report = _scan_once(
                    url,
                    cfg,
                    fetch_page=fetch_page,
                    capture=capture,
                    classifier=classifier,
                    trace=trace,
                )
                # trace_id 写入 run 结果(供上层导出 span 树;非内容字段)。
                report.intel["trace_id"] = trace_id
                return report
        finally:
            _unwire_trace_audit(trace)


# ---------------------------------------------------------------------------
# submit 主流程
# ---------------------------------------------------------------------------
def _submit_once(
    entry_id: int,
    portal: Portal,
    cfg: Config,
    *,
    auto_confirm: bool = False,
    dry_run: bool | None = None,
    executor: ExecutorFn | None = None,
) -> ExecutionResult:
    """run_submit 的主体(拆出以便外层统一计时,行为与旧版逐行一致)。"""
    if not isinstance(portal, Portal):
        portal = Portal(str(portal))

    queue = _default_queue(cfg.db_path)
    entry = queue.get(entry_id)
    if entry is None:
        raise RuntimeError(f"复核队列中不存在编号为 {entry_id} 的条目,无法提交")

    status = str(getattr(entry, "status", ""))
    if status != "approved":
        raise RuntimeError(
            f"仅 approved 记录可提交,请先人工审核批准"
            f"(条目 {entry_id} 当前状态:{status})"
        )

    state_path = str(pathlib.Path(cfg.data_dir) / "rate_limit.json")
    limiter = _default_rate_limiter(
        state_path, cfg.submit_min_interval_s, cfg.submit_max_per_day
    )
    allowed, reason = limiter.can_submit()
    if not allowed:
        telemetry.inc("submit.rate_limited")
        logger.warning("提交被频控拒绝(条目 %s):%s", entry_id, reason)
        return ExecutionResult(ok=False, portal=portal.value, notes=[reason])

    plan = _default_plan(entry, portal, cfg)
    execute = executor if executor is not None else _default_executor
    result = execute(plan, cfg, auto_confirm=auto_confirm, dry_run=dry_run)

    if result.submitted:
        queue.mark_submitted(entry_id)
        limiter.record()
        audit = _default_audit_logger(cfg.audit_path)
        audit.log_event(
            "submit",
            entry=entry_id,
            portal=portal.value,
            site=str(getattr(entry, "site_url", "")),
            ok=result.ok,
            submitted=True,
        )
        telemetry.inc("submit.submitted")
        logger.info("条目 %s 已完成提交并回写队列状态(门户 %s)", entry_id, portal.value)
    else:
        logger.info(
            "条目 %s 本次执行未完成真实提交(submitted=False),不回写队列、不计频控",
            entry_id,
        )
    return result


def run_submit(
    entry_id: int,
    portal: Portal,
    cfg: Config,
    *,
    auto_confirm: bool = False,
    dry_run: bool | None = None,
    executor: ExecutorFn | None = None,
) -> ExecutionResult:
    """把一条已人工确认(approved)的复核记录提交到举报门户。

    安全前置(依次强制):
    1. 条目必须存在且状态为 ``approved``,否则抛 RuntimeError(人工红线);
    2. 频控(最小间隔 / 每日上限)不通过时直接返回 ``ok=False``,
       不构建计划、不进入执行器;
    3. 人工门(HUMAN_GATE)由执行器落实,本函数只透传 ``auto_confirm``
       (CLI 永远传 False)。

    只有当执行器返回 ``submitted=True``(真实提交完成)时才回写:
    ``queue.mark_submitted`` + 频控 ``record`` + "submit" 审计事件。
    V5 可观测:整体耗时记 ``telemetry.timer("submit.total")``,真实提交计
    ``submit.submitted``,频控拒绝计 ``submit.rate_limited``。

    :param executor: 注入的执行器;缺省用 submit.executor_playwright.execute。
    :raises RuntimeError: 条目不存在 / 状态非 approved / 兄弟模块未就位。
    """
    with telemetry.timer("submit.total"):
        return _submit_once(
            entry_id,
            portal,
            cfg,
            auto_confirm=auto_confirm,
            dry_run=dry_run,
            executor=executor,
        )
