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

GLM 第三方分歧仲裁接线(V10.21 可选增强,默认关闭 = 现状):``cfg`` 附加
属性 ``arbiter_enabled`` 为 True 时,**ensemble 聚合之后、verdict 判定
之前的**,若存在成员分歧度(同图各成员 nsfw_prob 的 max−min)≥
:data:`netsentinel.vision.arbiter.DISAGREE_GAP`(0.35)的图片,经
:func:`netsentinel.vision.arbiter.arbitrate` 送 GLM 视觉大模型第三方仲裁,
被仲裁图的 ensemble 条目替换为 ``model="vlm-arbiter"`` 的新条目后再进
判定;无分歧 / 离线 / 预算尽 / 内核未就位一律原样返回,主线不受影响。
开启方式:``cfg.arbiter_enabled = True``(附加实例属性,仿
``enable_text_fusion`` 先例);代价:每站最多
``min(3, cfg.vlm_max_images_per_site)`` 张仲裁外呼(缓存命中不外呼不扣
预算)。计数 ``scan.arbiter.enabled`` / ``scan.arbiter.skipped``(门控双侧)
与 ``scan.arbiter.errors``(优雅降级)。详见 :func:`_wire_arbiter`。

识别失败诚信缺口清偿(V10.24,默认开启):V10.23 实弹事故——GLM 密钥
中途余额耗尽(HTTP 429 code 1113),评分失败的图片被静默跳过 / 降级为
0 分,站点以零分集合产出 verdict=clean。"证据范围内未见违规"不等于
"识别做成",这是诚信缺口。本编排层按**成员×图片**粒度记账失败
(静默缺评 = 入批数 − 返回数;带内错误分 = ``scores["error"]`` 非空,
A63/GLM/failover 同一降级语义),随后两段处置:

1. **诚信升级**(``cfg.escalate_on_classify_error`` 附加属性,缺省 True =
   开启):本站存在任何图片级评分失败时,``needs_review`` 强制 True
   (verdict 三档保持 assess 原档不动,与 fusion"只升不降"同哲学)、入列
   note 追加 ``[识别失败 N 张,判定置信受损]``、verdict=clean 时 CLI 输出
   与日志各加一行醒目中文警示;计数 ``scan.classify.failed_images``。
   开关关闭回旧行为(零失败主线两侧逐字节不变)。
2. **快速中止**(``cfg.classify_abort_threshold`` 附加属性,缺省 5):首笔
   完成的成员批量评分**全部失败**且失败数 ≥ 阈值(疑似余额耗尽 / 密钥
   失效 / 网络中断等系统性故障)时,不再对其余成员盲打,直接抛中文
   ``RuntimeError``(附 ``vlmctl ping`` 诊断指引),整次 scan 以错误收场。
   计数 ``scan.classify.aborted``(不受诚信开关门控)。

详见 :func:`_collect_member_scores` 与 :func:`_escalate_classify_failures`。

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
from typing import Any, Callable, Iterator, Protocol

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


def _default_capture_many(urls: list[str], cfg: Config) -> Iterator[PageSample]:
    """批量采样(v1 缺省链路):一次浏览器会话逐页复用(V10.13 性能升级)。

    - v1 引擎:``browser.capture_pages_iter`` —— 一次 playwright 启动 +
      launch,逐页 new_page **流式产出**(P1-3:调用方可边爬边消费;
      物化用法 ``list(_default_capture_many(...))`` 与旧版 list 语义
      逐字节一致);每页采样口径与逐页 ``capture_page`` 一致(缺 playwright
      时自动逐页退化,行为不变);
    - v2 引擎:``capture_v2.capture_pages_v2_iter``(同款流式内核);
      旧实例缺 iter 变体时回退逐页 list,行为不变;
    - 仅在调用方未注入 ``capture`` / ``fetch_page`` 时启用(注入路径的
      行为与旧版逐字节一致)。
    """
    if getattr(cfg, "capture_engine", "v1") == "v2":
        capture_v2 = _load("netsentinel.crawler.capture_v2")
        # V10.15(创世纪 G1):v2 引擎同样接入会话复用批量口;P1-3 起优先
        # 流式内核(逐页产出),旧实例回退逐页物化,行为不变。
        batch_iter = getattr(capture_v2, "capture_pages_v2_iter", None)
        if callable(batch_iter):
            return batch_iter(urls, cfg)
        return iter(
            [
                capture_v2.capture_page_v2(u, cfg, fetch_page=None)
                for u in urls
            ]
        )
    browser = _load("netsentinel.crawler.browser")
    return browser.capture_pages_iter(urls, cfg)


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


def _default_arbiter_module() -> Any:
    """GLM 第三方分歧仲裁内核(``netsentinel.vision.arbiter``,惰性导入;测试可替换)。"""
    return _load("netsentinel.vision.arbiter")


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


def _wire_arbiter(
    ensemble: list[ImageScore], raw_scores: list[ImageScore], cfg: Config
) -> list[ImageScore]:
    """GLM 第三方分歧仲裁接线(``cfg.arbiter_enabled`` 开关门控,默认 False = 现状)。

    运行位次:**ensemble 聚合之后、verdict 判定之前**(A25 仲裁语义:被仲裁
    图的 ensemble 条目被替换为 ``model="vlm-arbiter"`` 的新条目,再交给
    :func:`_default_assess` 消费)。门控与前置检查:

    - ``getattr(cfg, "arbiter_enabled", False)`` 为假 → 计
      ``scan.arbiter.skipped`` 并原样返回 ensemble(缺省主线逐字节不变,
      连仲裁内核都不导入);
    - 开启后先把成员原始评分按图片分组(排除 ensemble / vlm-arbiter 已聚合
      条目,同成员同图后值覆盖前值,与 :func:`vision.ensemble` 同口径),
      **仅当存在分歧度 max−min ≥ ``vision.arbiter.DISAGREE_GAP`` 的图片**
      才调用 ``arbiter.arbitrate(ensemble, raw_scores, cfg)``——无分歧不
      触发任何仲裁路径(不构造 client / cache,零外呼零预算);
    - 预算尽 / 离线等整体性放弃由 arbiter 自身优雅中止(原样返回集成
      结果);本函数只兜底捕获接线层异常(内核未就位 / 评分非法等)——
      记 ``scan.arbiter.errors`` + warning 后原样返回,绝不阻断扫描;
    - 计数:门控开 ``scan.arbiter.enabled`` / 门控关 ``scan.arbiter.skipped``
      (仿 V10.18 text_fusion 双侧口径);仲裁内部另有 ``arbiter.disputes`` /
      ``arbiter.escalated``(arbiter 自带)。

    开启方式:``cfg.arbiter_enabled = True``(附加实例属性,不新增 Config
    字段)。代价:每站最多 ``min(3, cfg.vlm_max_images_per_site)`` 张仲裁
    外呼(缓存命中不外呼不扣预算;预算尽停止后续仲裁,已完成的保留)。

    :param ensemble: 集成评分结果(model="ensemble",每图一条)。
    :param raw_scores: 各成员模型的逐图原始评分(与 ``_collect_member_scores``
        返回的原始流同源)。
    :return: 仲裁生效时为替换后的新列表;任何不满足时为 ensemble 原列表。
    """
    if not getattr(cfg, "arbiter_enabled", False):
        telemetry.inc("scan.arbiter.skipped")
        return ensemble
    telemetry.inc("scan.arbiter.enabled")
    try:
        arbiter = _default_arbiter_module()
        gap = float(getattr(arbiter, "DISAGREE_GAP", 0.35))
        arbiter_model = str(getattr(arbiter, "ARBITER_MODEL", "vlm-arbiter"))
        grouped: dict[str, dict[str, float]] = {}
        for s in raw_scores:
            model = str(s.model)
            if model in ("ensemble", arbiter_model):
                continue  # 已聚合 / 已仲裁条目不是成员分,防误传自我重复
            grouped.setdefault(s.image.path, {})[model] = float(s.nsfw_prob)
        has_disagreement = any(
            len(probs) >= 2 and (max(probs.values()) - min(probs.values())) >= gap
            for probs in grouped.values()
        )
        if not has_disagreement:
            logger.debug(
                "成员分歧均低于 %.2f(arbiter.DISAGREE_GAP),本轮无图送第三方仲裁", gap
            )
            return ensemble
        return list(arbiter.arbitrate(ensemble, raw_scores, cfg))
    except Exception as exc:  # noqa: BLE001 - 仲裁是增强项,失败不阻断扫描
        telemetry.inc("scan.arbiter.errors")
        logger.warning("GLM 第三方仲裁已跳过(集成结果与判定不受影响):%s", exc)
        return ensemble


def _escalate_classify_failures(
    report: SiteReport, cfg: Config, failed_images: int
) -> str | None:
    """识别失败诚信升级(V10.24,``cfg.escalate_on_classify_error`` 门控,
    缺省 True = 开启;V10.23 余额耗尽事故的清偿)。

    当本站存在图片级评分失败(:func:`_collect_member_scores` 记账的
    ``failed_images > 0``,静默缺评 + 带内错误分合计)时:

    1. **needs_review 强制 True**——verdict 三档保持 assess 原档不动,只升
       复核倾向(与 fusion / abstain"只升不降"同哲学):证据范围内
       "未见违规"不等于"识别做成",零分集合撑起的 clean 不可信;
    2. 入列 note 返回 ``[识别失败 N 张,判定置信受损]``(由调用方随
       ``queue.add(note=...)`` 传给复核队列,供人工复核时知情);
    3. verdict=clean 且有失败时,CLI 输出(stdout 一行)与日志
       (warning)各加一行醒目中文警示;
    4. telemetry 计数 ``scan.classify.failed_images``(按失败张数累加)。

    开关关闭(``escalate_on_classify_error = False``)或零失败:返回 None
    且零副作用,主线行为与 V10.24 之前逐字节一致(入列调用口径、遥测、
    intel 键集全不扰动)。快速中止(全失败熔断)在收货口另行处置,不受
    本开关门控。

    :return: 入列 note 中文标注;无需标注时 None。
    """
    if failed_images <= 0:
        return None
    if not getattr(cfg, "escalate_on_classify_error", True):
        return None
    telemetry.inc("scan.classify.failed_images", float(failed_images))
    report.needs_review = True
    note = f"[识别失败 {failed_images} 张,判定置信受损]"
    logger.warning(
        "识别失败诚信升级:%d 张图片评分失败,verdict=%s 保持原档不动,"
        "needs_review 强制 True,入列 note=%s",
        failed_images,
        report.verdict.value,
        note,
    )
    if str(report.verdict.value) == "clean":
        line = (
            f"警示:本站判定为 clean,但有 {failed_images} 张图片识别失败"
            '("未见违规"≠"识别做成",零分集合不可信),已强制转人工复核'
        )
        # V10.24 诚信缺口清偿:CLI 输出一行醒目中文提示(直接打印是本
        # 编排层唯一的对外可见通道;CLI 摘要打印归属 __main__,本模块领地内)。
        print(line)
        logger.warning(line)
    return note


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
#: 快速中止缺省阈值:首笔完成的成员批量评分全部失败且失败数 ≥ 该值时熔断
#: (经 ``cfg.classify_abort_threshold`` 附加属性覆盖;V10.23 事故复盘值)。
CLASSIFY_ABORT_THRESHOLD_DEFAULT = 5


def _classify_abort_threshold(cfg: Config) -> int:
    """解析 ``cfg.classify_abort_threshold``(附加属性,int 钳制,缺省 5)。

    非法值(非数字 / NaN / inf)回退缺省,绝不因配置笔误阻断扫描。
    """
    raw = getattr(cfg, "classify_abort_threshold", CLASSIFY_ABORT_THRESHOLD_DEFAULT)
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError):
        # 非数字 / NaN / inf 等:int() 全数拒绝,配置笔误回退缺省不炸扫描。
        return CLASSIFY_ABORT_THRESHOLD_DEFAULT


def _count_image_failures(
    imgs: list[ImageEvidence], returned: list[ImageScore]
) -> tuple[int, int]:
    """单个成员批量评分的图片级失败记账 → (静默缺评数, 带内错误分数)。

    V10.23 事故形态的两种"识别没做成"信号(V10.24 失败感知研究结论):

    1. **静默缺评**(dropped):``classify_batch(imgs)`` 返回条目数少于入批
       数——评分失败的图片被直接跳过(事故日志 ``完成 27 张图片评分`` vs
       ``13 张图片完成集成评分`` 的 14 张差值即此口径;ensemble 按路径分组
       去重,不能在集成出口反推,必须在成员收货口逐个对数);
    2. **带内错误分**(errored):成员返回了条目但 ``scores["error"]`` 非空
       (A63/GLM/parallel_classify/failover 共用的降级语义:``nsfw_prob=0.0``
       + 中文 error)——识别失败被伪装成"确认干净"的 0 分,同样拉低判定。

    :return: 两个非负计数;成员返回多于入批数时 dropped 记 0(不惩罚超额)。
    """
    dropped = len(imgs) - len(returned)
    if dropped < 0:
        dropped = 0
    errored = 0
    for s in returned:
        sc = getattr(s, "scores", None)
        if isinstance(sc, dict) and sc.get("error"):
            errored += 1
    return dropped, errored


def _classify_abort_runtimeerror(
    name: str, failed: int, total: int, threshold: int
) -> RuntimeError:
    """构造 V10.24 全败快速中止的中文 RuntimeError(消息文本单一事实源)。

    分块流水线(P1-3)的块级熔断与聚合熔断(每块 < 阈值但整站全败的
    补口)复用同一消息,保证任何路径下运营者看到的诊断指引一致。
    """
    return RuntimeError(
        f"分类器 {name!r} 首笔批量评分 {failed}/{total} 张"
        f"图片全部失败(≥ 熔断阈值 {threshold}),已中止本站"
        "剩余评分、不再盲打:最可能根因为 API 余额耗尽 / 密钥失效 /"
        " 网络中断等系统性故障,请先执行 "
        "`python -m netsentinel.vision.vlmctl ping 规格`"
        "(如 ping glm 或 ping openai:gpt-4o-mini)诊断,"
        "恢复后重扫本站"
    )


def _collect_member_scores(
    imgs: list[ImageEvidence],
    cfg: Config,
    classifier: ClassifierLike | None = None,
) -> tuple[list[ImageScore], list[str], int]:
    """收集各成员分类器的原始评分(含 V10.24 图片级失败记账与快速中止)。

    - ``classifier`` 注入时优先使用;缺省经 ``get_classifier(cfg.classifier, cfg)`` 获取;
    - 再遍历 ``cfg.ensemble_members`` 逐个取分类器(与已就位成员重名的跳过);
    - 未安装 / 构造失败 / 批量评分失败的成员记 warning 后跳过;
    - 至少要有一个成员成功完成评分,否则由调用方抛 RuntimeError。

    V10.24 失败感知:每个成员收货口经 :func:`_count_image_failures` 记账
    "参与评分图片数 vs 成功入集成图片数"的差值(静默缺评 + 带内错误分,
    成员×图片粒度合计为站点级失败数);零失败主线只做纯计数,不产生任何
    日志 / 遥测 / 行为差异(逐字节不变)。

    V10.24 快速中止(``cfg.classify_abort_threshold``,缺省 5):**首笔完成
    的成员批量评分全部失败且失败数 ≥ 阈值**(如 GLM 密钥余额耗尽,HTTP 429
    code 1113 连环盲打)时,直接抛中文 RuntimeError——不再对剩余成员盲打,
    整次 scan 以错误收场(CLI 退出码沿用现有异常路径);提示用
    ``python -m netsentinel.vision.vlmctl ping 规格`` 诊断根因。部分失败
    (还有图片评成)不中止,交给 :func:`_escalate_classify_failures` 诚信
    升级入列。中止计数 ``scan.classify.aborted`` / ``scan.errors``,失败
    图片数计入 ``scan.classify.failed_images``(中止路径不受诚信开关门控)。

    P0.2 受界并发送审(``cfg.vlm_parallel_workers`` 附加属性,缺省自动档):
    成员批量经 :func:`netsentinel.vision.bounded_parallel.classify_bounded`
    调度——仅对沿用基类默认逐张 ``classify_batch`` 的分类器改为有界线程池
    保序执行(串行等价、判定不变);其余(覆写批量/注入替身/单张批量)
    原串行路径。带内 error 分语义两路径一致,均由 V10.24 收货口记账。

    :return: (全部原始评分, 成功成员名列表, 图片级失败合计数)
    :raises RuntimeError: 首笔完成的成员批量评分全部失败且失败数 ≥ 熔断阈值。
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
    failed_images = 0
    abort_threshold = _classify_abort_threshold(cfg)
    first_batch_done = False
    for inst in instances:
        name = str(getattr(inst, "name", "?"))
        try:
            # list() 包装:分类器返回 None/生成器等畸形结果时保持旧语义
            # (try 内抛出 → 记 warning 跳过该成员),不在收货口外裸炸。
            # P0-2 受界并发送审(``vlm_parallel_workers`` 附加属性,缺省
            # 自动档 min(档位 io_workers, 6)):仅对"沿用基类默认逐张
            # classify_batch"的成员分类器把批量改为有界线程池**保序**执行
            # (串行等价;单图异常按 A166 容错为 error 分,由下方 V10.24
            # 收货口记账);覆写批量(failover/nudenet/clip/guard)、注入
            # 替身、批量 <2 张一律原串行路径,逐字节旧行为。
            returned = list(
                _load("netsentinel.vision.bounded_parallel").classify_bounded(
                    inst, imgs, cfg
                )
            )
        except Exception as exc:  # noqa: BLE001 - 单成员评分失败不拖垮整轮扫描
            logger.warning("分类器 %r 批量评分失败,已跳过:%s", name, exc)
            continue
        dropped, errored = _count_image_failures(imgs, returned)
        member_failed = dropped + errored
        failed_images += member_failed
        raw_scores.extend(returned)
        ok_members.append(name)
        logger.info("分类器 %r 完成 %d 张图片评分", name, len(imgs))
        if member_failed > 0:
            logger.warning(
                "分类器 %r 存在图片级评分失败:静默缺评 %d 张 + 带内错误分 %d 张"
                "(参与 %d 张,识别没做成 ≠ 确认干净)",
                name,
                dropped,
                errored,
                len(imgs),
            )
        if not first_batch_done:
            first_batch_done = True
            if (
                len(imgs) > 0
                and member_failed >= len(imgs)
                and len(imgs) >= abort_threshold
            ):
                telemetry.inc("scan.classify.failed_images", float(member_failed))
                telemetry.inc("scan.classify.aborted")
                telemetry.inc("scan.errors")
                raise _classify_abort_runtimeerror(
                    name, member_failed, len(imgs), abort_threshold
                )
    return raw_scores, ok_members, failed_images


# ---------------------------------------------------------------------------
# 图片证据增强:补齐 fetcher 契约留白的宽高,并把哈希文件名还原为原始文件名
# ---------------------------------------------------------------------------
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

#: 还原后的原始文件名最大长度(超出视为异常名,保留哈希名)
_MAX_ORIGINAL_NAME_LEN = 120


def _sniff_webp_size(head: bytes) -> tuple[int, int]:
    """从 WebP 文件头(RIFF/WEBP)解析画布宽高;非 WebP 或头不完整返回 (0, 0)。

    覆盖三种位流:VP8X(扩展格式,画布尺寸在 chunk 载荷固定偏移)、
    VP8(有损,帧头同步码后 2×14bit)、VP8L(无损,签名后 4 字节打包
    2×14bit);chunk 布局为 ``WEBP + fourcc(12:16) + size(16:20) +
    载荷(20+)``,尺寸均按规范减 1 存储,解析后 +1 还原。
    """
    if head[0:4] != b"RIFF" or head[8:12] != b"WEBP":
        return 0, 0
    fourcc = head[12:16]
    try:
        if fourcc == b"VP8X" and len(head) >= 30:
            w = int.from_bytes(head[24:27], "little") + 1
            h = int.from_bytes(head[27:30], "little") + 1
            return w, h
        if fourcc == b"VP8 " and len(head) >= 30 and head[23:26] == b"\x9d\x01\x2a":
            w = struct.unpack("<H", head[26:28])[0] & 0x3FFF
            h = struct.unpack("<H", head[28:30])[0] & 0x3FFF
            return w, h
        if fourcc == b"VP8L" and len(head) >= 25 and head[20] == 0x2F:
            bits = int.from_bytes(head[21:25], "little")
            w = (bits & 0x3FFF) + 1
            h = ((bits >> 14) & 0x3FFF) + 1
            return w, h
    except (struct.error, ValueError):
        pass
    return 0, 0


#: AVIF/HEIF 尺寸解析的结构守卫:层级深度 / 每层盒子数 / 单盒体上限
_BMFF_MAX_DEPTH = 4
_BMFF_MAX_BOXES = 64
_BMFF_MAX_BOX_BODY = 32 * 1024 * 1024
#: 认可的 HEIF major brand(须先命中 ftyp 才进入盒子遍历,防误解析)
_HEIF_BRANDS = (b"avif", b"avis", b"heic", b"heix", b"heim", b"msf1", b"mif1")
#: ispe/meta 均为 FullBox:载荷前有 4 字节版本/flags
_BMFF_FULLBOX_SKIP = 4
#: 尺寸线索所在的盒子链:meta → iprp → ipco → ispe(宽高 u32 BE)
_HEIF_CONTAINERS = (b"meta", b"iprp", b"ipco")


def _bmff_boxes(fh: Any, start: int, end: int) -> list[tuple[bytes, int, int]]:
    """列出 [start, end) 内的盒子 (type, 载荷起点, 载荷终点);结构异常返回已收部分。

    守卫:size==1(64 位 largesize,罕见)整层放弃;size==0 视为占满到
    end;载荷越界或超 _BMFF_MAX_BOX_BODY 停止;每层至多 _BMFF_MAX_BOXES 个。
    """
    boxes: list[tuple[bytes, int, int]] = []
    fh.seek(start)
    for _ in range(_BMFF_MAX_BOXES):
        if fh.tell() + 8 > end:
            break
        header = fh.read(8)
        if len(header) < 8:
            break
        size = struct.unpack(">I", header[:4])[0]
        body_start = fh.tell()
        if size == 1:
            break
        body_end = end if size == 0 else body_start + size - 8
        if body_end > end or body_end <= body_start or body_end - body_start > _BMFF_MAX_BOX_BODY:
            break
        boxes.append((header[4:8], body_start, body_end))
        fh.seek(body_end)
    return boxes


def _find_heif_ispe(fh: Any, start: int, end: int, depth: int) -> tuple[int, int]:
    """在盒子树 [start, end) 内递归找 ispe 尺寸(meta→iprp→ipco→ispe)。"""
    if depth > _BMFF_MAX_DEPTH:
        return 0, 0
    for box_type, body_start, body_end in _bmff_boxes(fh, start, end):
        if box_type == b"ispe":
            fh.seek(body_start + _BMFF_FULLBOX_SKIP)
            payload = fh.read(8)
            if len(payload) < 8:
                continue
            w, h = struct.unpack(">II", payload)
            if w > 0 and h > 0:
                return int(w), int(h)
        elif box_type in _HEIF_CONTAINERS:
            skip = _BMFF_FULLBOX_SKIP if box_type == b"meta" else 0
            found = _find_heif_ispe(fh, body_start + skip, body_end, depth + 1)
            if found != (0, 0):
                return found
    return 0, 0


def _sniff_heif_size(fh: Any) -> tuple[int, int]:
    """解析 AVIF/HEIF(ISOBMFF)宽高:ftyp 品牌核验 → 全文件盒子树找 ispe。

    只读向前遍历 + 三重结构守卫;任何结构异常返回 (0, 0)(调用方
    fail-open:未测得的图照常送审,绝不因解析失败丢证据)。
    """
    fh.seek(0)
    ftyp = fh.read(12)
    if len(ftyp) < 12 or ftyp[4:8] != b"ftyp" or ftyp[8:12] not in _HEIF_BRANDS:
        return 0, 0
    file_end = fh.seek(0, 2)
    return _find_heif_ispe(fh, 0, file_end, 0)


def _sniff_image_size(path: pathlib.Path) -> tuple[int, int]:
    """从 PNG/GIF/JPEG/WebP/AVIF(HEIF)文件头嗅探像素宽高;无法识别返回 (0, 0)。

    P0-1 起补 WebP 与 AVIF/HEIF 两族:此前这两类图宽高恒为 (0, 0),
    在判定公式里**从不作为候选参与 agg/count**(等于系统性无视真实
    证据);补齐后它们恢复完整判定参与权(修复漏报洞,方向只会更严:
    多出的真实证据只可能抬升 agg,needs_review 只升不降)。嗅探失败
    一律 (0, 0),由送审闸门 fail-open 放行,绝不丢证据。
    """
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
                    seglen = struct.unpack(">H", length)[0]
                    if seglen < 2:
                        return 0, 0
                    fh.seek(seglen - 2, 1)
            if head[:4] == b"RIFF":  # WebP(VP8X / VP8 / VP8L)
                return _sniff_webp_size(head)
            if head[4:8] == b"ftyp":  # AVIF / HEIF 家族
                return _sniff_heif_size(fh)
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


def _site_text_features(pages: list[PageSample]) -> dict:
    """把各页 PageSample 的文本线索汇总为站点级文本特征(text_intel 口径)。

    - PageSample 只携带采集命中的线索词(``text_hint_hits``,不携带原始
      HTML),这里把全部页面的命中词拼成一份伪正文,交给
      ``netsentinel.intel.text_intel.text_features`` 复算风险分(词表与
      单页分析同源,风险公式零改动);
    - 零命中返回空 dict(= 该路情报缺失,融合口径与既有行为完全一致);
    - 仅供融合步骤在 ``cfg.enable_text_fusion`` 开启时调用(附加实例属性,
      缺省 False = 现状零变化);任何异常由调用方的融合 try 块统一降级,
      本函数不自行捕获。
    """
    hits: list[str] = []
    for page in pages:
        hits.extend(str(h) for h in (getattr(page, "text_hint_hits", None) or []))
    if not hits:
        return {}
    return _load("netsentinel.intel.text_intel").text_features(" ".join(hits))


def _sprt_feed_chunk(
    sprt: Any,
    chunk_scores: list[ImageScore],
    weights: dict[str, float] | None,
) -> int:
    """把一个已完成分块的评分喂给 SPRT 检验器(V10.37,纯本地数学)。

    - **错误分绝不喂**:带内错误分(``nsfw_prob=0.0`` + ``scores["error"]``,
      A63 降级语义)是"识别没做成"的哨兵而不是"确认干净"的证据——喂进
      检验器会把 LLR 推向 clean,方向性错误与 V10.24 失败感知一致(失败
      不是证据);剔除口径与 :func:`_count_image_failures` 的 errored 同源;
    - 融合口径与站点判定同源(:func:`_default_ensemble`);门禁只放行单
      成员配置(kernel_gate 保守边界),融合=成员分本身,SPRT 消费的概率
      与最终判定消费的概率逐张一致。

    :return: 实际喂入的证据张数(错误分剔除后)。
    """
    ok_scores = [
        s for s in chunk_scores
        if not (
            isinstance(getattr(s, "scores", None), dict)
            and s.scores.get("error")
        )
    ]
    if not ok_scores:
        return 0
    fed = 0
    for fused in _default_ensemble(ok_scores, weights):
        sprt.update(float(fused.nsfw_prob))
        fed += 1
    return fed


def _pipelined_fetch_and_classify(
    url: str,
    cfg: Config,
    trace: Any | None,
    preflight_meta: Any | None,
    *,
    sprt_active: bool = False,
) -> tuple[
    list[PageSample],
    list[ImageScore],
    list[str],
    int,
    Any | None,
    list[ImageEvidence],
    dict[str, Any] | None,
]:
    """P1-3 站内流水线重叠:主线程逐页礼貌爬取(生产者),后台按页分块
    评分(消费者),把 VLM 等待藏进爬取窗口。

    结构与语义(与串行路径对齐,差异如实登记):

    - **生产者 = 主线程**逐页迭代 ``_default_capture_many`` 生成器——
      页加载、懒加载滚动、图片下载与逐张礼貌间隔全部留在主线程,
      对目标站点的请求节奏与串行路径完全一致(红线 35:不放宽);
    - **消费者 = 单工作后台池**:每页的证据经跨页共享受检闸门
      (:class:`~netsentinel.vision.send_gate.SendGate`,去重键跨页累计)
      放行后提交为一个分块任务(内部复用 ``_collect_member_scores``
      → P0-2 受界并发,成员环/失败记账/V10.24 块级熔断原样);
    - **早夭中止**:每提交一页后轮询已完成分块,分块抛系统性错误
      (V10.24 熔断)立即停止爬取并关闭采样生成器(浏览器会话随生成
      器析构)——比串行路径**更早**止损,不浪费剩余页的爬取;
    - **聚合熔断补口**:每页图片数 < 熔断阈值时块级永不触发;收割后按
      整站总量复判(全败且 ≥ 阈值 → 同款中文 RuntimeError),语义与
      串行"首笔全批全败中止"对齐;
    - **顺序**:pages / raw_scores / image 首现序 = 页序,与串行一致;
    - **V10.37 SPRT 实时早停**(``sprt_active``,仅当 ``use_sprt`` 且门禁
      三锁全过,见 :mod:`netsentinel.vision.kernel_gate`):站级 SPRT 随
      已完成分块**增量判停**——判停(统计结论已足)或站级预算
      (``vlm_max_images_per_site``,首次成为真实送审上界)耗尽即停止
      爬取 + 取消未起跑分块;**在飞分块自然完成、结果照收**(已外呼的
      调用弃收=白烧预算,多收的证据只会让判定更稳);错误分(哨兵 0.0)
      绝不喂检验器(:func:`_sprt_feed_chunk`);判定机器零触碰——SPRT
      只决定「还送不送」,不改「怎么判」;
    - 差异登记:trace 开启时 ``scan.vlm`` span 覆盖的是收割段(评分与
      爬取并发,无法整体入 span);``scan.members_seconds`` 记首个分块
      提交到收割完成的墙钟(含重叠等待)。
    """
    send_gate_mod = _load("netsentinel.vision.send_gate")
    gate = send_gate_mod.SendGate(cfg)

    pages: list[PageSample] = []
    send_imgs: list[ImageEvidence] = []
    futures: list[tuple[Any, Any]] = []
    enrich_missing: set[str] = set()
    enrich_renames: dict[str, str] = {}
    early_error: BaseException | None = None

    # V10.37 SPRT 实时早停状态(sprt_active=False 时全部短路,零行为差异):
    # sprt_fed 按 future 对象身份记账(生产者轮询与收割两处喂入共用,防重复)。
    sprt: Any | None = None
    sprt_stop: str | None = None      # SPRT 判停终态("clean" / "nsfw")
    budget_stop = False               # 站级送审预算耗尽
    budget_cap = -1                   # <0 = 不设上界(非 SPRT 模式)
    budget_skipped = 0                # 预算截下(闸门放行但未送审)
    sprt_cancelled = 0                # 判停/预算尽后取消的已排队送审张数
    sprt_fed: set[Any] = set()
    sprt_weights: dict[str, float] | None = None
    sprt_weights_done = False
    if sprt_active:
        sprt = _load("netsentinel.decision.sprt").SPRT(cfg.sprt_alpha, cfg.sprt_beta)
        budget_cap = max(0, int(cfg.vlm_max_images_per_site))

    from concurrent.futures import ThreadPoolExecutor

    chunk_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vlmpipe")
    try:
        with _span(trace, "scan.fetch", stage="fetch", pipeline=True):
            urls = _default_discover(url, cfg, None)
            logger.info("链接发现完成,待抽样页面 %d 个", len(urls))
            crawl_gen = _default_capture_many(urls, cfg)
            try:
                for page in crawl_gen:
                    pages.append(page)
                    chunk = list(page.image_evidences)
                    for img in chunk:
                        # 跨页共享受检闸门前置:补宽高嗅探 + 哈希名还原
                        # (V5 的 known_missing/renames 缓存跨页贯通,同款
                        # 去重语义与一次性 enrich 完全一致)
                        _enrich_image_evidence(
                            img,
                            _known_missing=enrich_missing,
                            _renames=enrich_renames,
                        )
                    send_chunk = gate.feed(chunk)
                    if sprt is not None:
                        # 站级预算上界:闸门放行项超出剩余预算的部分截下不
                        # 送审(gate.stats.sent 同步回扣,保持 sent=实际送审
                        # 的诚实口径;被截图仍计入 collected/去重键,只是
                        # 不再产生 VLM 调用)。剩余配额按**本批放行前**的
                        # 已送量计算(feed 已把整页计入 stats.sent,若按截后
                        # 余量算会错把本批也当已花掉)。
                        sent_before = gate.stats.sent - len(send_chunk)
                        remaining = budget_cap - sent_before
                        drop = len(send_chunk) - max(0, remaining)
                        if drop > 0:
                            budget_skipped += drop
                            gate.stats.sent -= drop
                            telemetry.inc("sprt.budget.skipped", float(drop))
                            send_chunk = send_chunk[: max(0, remaining)]
                    if send_chunk:
                        send_imgs.extend(send_chunk)
                        telemetry.inc("scan.pipeline.chunks")
                        futures.append(
                            (
                                send_chunk,
                                chunk_pool.submit(
                                    _collect_member_scores, send_chunk, cfg, None
                                ),
                            )
                        )
                    # SPRT 增量判停:已完成分块融合喂检验器,统计结论已足
                    # 即收手(错误分块交给早夭/收割路径,不喂)。
                    if sprt is not None and sprt_stop is None and not budget_stop:
                        for _, fut in futures:
                            if fut in sprt_fed or fut.cancelled() or not fut.done():
                                continue
                            if fut.exception() is not None:
                                sprt_fed.add(fut)
                                continue
                            chunk_scores, chunk_members, _cf = fut.result()
                            sprt_fed.add(fut)
                            if not sprt_weights_done:
                                sprt_weights = _reliability_ensemble_weights(
                                    cfg, chunk_members
                                )
                                sprt_weights_done = True
                            if _sprt_feed_chunk(sprt, chunk_scores, sprt_weights):
                                telemetry.inc("sprt.chunks_fed")
                            if sprt.state() != "continue":
                                sprt_stop = sprt.state()
                                logger.info(
                                    "SPRT 判停(%s):累计证据 %d 张,停止爬取与送审",
                                    sprt_stop, sprt.n,
                                )
                                break
                    if sprt is not None and not budget_stop and gate.stats.sent >= budget_cap:
                        budget_stop = True  # 预算已满:再爬已无送审意义
                        logger.info(
                            "站级送审预算已满(%d 张),停止爬取", budget_cap
                        )
                    # 早夭检测:已完成分块抛系统性错误 → 立即停止爬取
                    for _, fut in futures:
                        if fut.done():
                            exc = fut.exception()
                            if exc is not None:
                                early_error = exc
                                break
                    if early_error is not None or sprt_stop is not None or budget_stop:
                        break
            finally:
                if early_error is not None or sprt_stop is not None or budget_stop:
                    crawl_gen.close()  # 放弃剩余页;浏览器会话随生成器关闭
            logger.info(
                "页面采样完成:%d 页 / %d 张图片", len(pages), gate.stats.collected
            )

        # —— 收割段(串行路径的 scan.vlm 阶段等价物)——
        raw_scores: list[ImageScore] = []
        ok_members: list[str] = []
        classify_failed = 0
        members_started = time.perf_counter()
        try:
            with _span(
                trace, "scan.vlm", stage="vlm",
                members=len(cfg.ensemble_members or []), pipeline=True,
            ):
                if early_error is not None:
                    raise early_error
                if sprt is None:
                    # 收割前合并"仍在排队、未起跑"的分块(爬取快于评分的站点,
                    # 分块串行排队会损失整池吞吐):已在跑/已完成的分块保留,
                    # 未起跑的取消并合并为一整批,一次成员环拿满 P0-2 受界并发
                    # ——快站恢复串行路径吞吐,慢站(分块早已在爬取窗内跑完)
                    # 此合并为空操作。FIFO 单工作者保证保留分块是页序前缀,
                    # 合并批是后缀,首现序不变。
                    kept: list[tuple[Any, Any]] = []
                    merged_queued: list[ImageEvidence] = []
                    for send_chunk, fut in futures:
                        if fut.done() or fut.running():
                            kept.append((send_chunk, fut))
                        elif fut.cancel():
                            merged_queued.extend(send_chunk)
                        else:  # 取消失败(刚被调度):按原分块收割
                            kept.append((send_chunk, fut))
                    if merged_queued:
                        telemetry.inc("scan.pipeline.coalesced")
                        logger.debug(
                            "流水线收割:合并 %d 张排队图片为单批(恢复整池吞吐)",
                            len(merged_queued),
                        )
                        # 合并批作为新任务回到同一 FIFO 池(排在保留分块之后,
                        # 页序首现序不变);一次成员环拿满 P0-2 受界并发。
                        kept.append(
                            (
                                merged_queued,
                                chunk_pool.submit(
                                    _collect_member_scores, merged_queued, cfg, None
                                ),
                            )
                        )
                    for _, fut in kept:
                        chunk_scores, chunk_members, chunk_failed = fut.result()
                        raw_scores.extend(chunk_scores)
                        for m in chunk_members:
                            if m not in ok_members:
                                ok_members.append(m)
                        classify_failed += chunk_failed
                else:
                    # V10.37 SPRT 收割:**不合并**(合并=一次性整批送审,与
                    # 早停意志相悖);**判停** → 未起跑分块取消(统计结论已足,
                    # 省预算正是目的);**预算尽** → 已计入 sent 的排队分块
                    # 照常执行(它们在预算内,取消会让账目与事实脱节);在飞/
                    # 已完成分块一律照收(已外呼的调用弃收=白烧,多收的证据
                    # 只会让判定更稳);收割中判停 → 就地取消剩余排队。
                    kept = []
                    for send_chunk, fut in futures:
                        if (
                            sprt_stop is not None
                            and not fut.done()
                            and not fut.running()
                            and fut.cancel()
                        ):
                            sprt_cancelled += len(send_chunk)
                            gate.stats.sent -= len(send_chunk)
                            telemetry.inc("sprt.cancelled", float(len(send_chunk)))
                        else:
                            kept.append((send_chunk, fut))
                    for i, (send_chunk, fut) in enumerate(kept):
                        if fut.cancelled():
                            continue
                        chunk_scores, chunk_members, chunk_failed = fut.result()
                        raw_scores.extend(chunk_scores)
                        for m in chunk_members:
                            if m not in ok_members:
                                ok_members.append(m)
                        classify_failed += chunk_failed
                        if sprt_stop is None and fut not in sprt_fed:
                            sprt_fed.add(fut)
                            if not sprt_weights_done:
                                sprt_weights = _reliability_ensemble_weights(
                                    cfg, chunk_members
                                )
                                sprt_weights_done = True
                            if _sprt_feed_chunk(sprt, chunk_scores, sprt_weights):
                                telemetry.inc("sprt.chunks_fed")
                            if sprt.state() != "continue":
                                sprt_stop = sprt.state()
                                logger.info(
                                    "SPRT 判停(%s):累计证据 %d 张,取消剩余排队分块",
                                    sprt_stop, sprt.n,
                                )
                                for sc2, fut2 in kept[i + 1:]:
                                    if (
                                        not fut2.done()
                                        and not fut2.running()
                                        and fut2.cancel()
                                    ):
                                        sprt_cancelled += len(sc2)
                                        gate.stats.sent -= len(sc2)
                                        telemetry.inc(
                                            "sprt.cancelled", float(len(sc2))
                                        )
                    # 被取消分块的图片从 send_imgs 移除(口径=实际送审;
                    # 遥测 scan.images_sent 与 gate.stats.sent 同源)。
                    send_imgs[:] = [
                        img for sc, fut in kept if not fut.cancelled() for img in sc
                    ]
                # 聚合熔断补口:每块 < 阈值但整站全败 → 与串行同款中止
                total_sent = gate.stats.sent
                abort_threshold = _classify_abort_threshold(cfg)
                if (
                    total_sent > 0
                    and classify_failed >= total_sent
                    and total_sent >= abort_threshold
                ):
                    telemetry.inc("scan.classify.failed_images", float(classify_failed))
                    telemetry.inc("scan.classify.aborted")
                    telemetry.inc("scan.errors")
                    raise _classify_abort_runtimeerror(
                        str(ok_members[0]) if ok_members else cfg.classifier,
                        classify_failed,
                        total_sent,
                        abort_threshold,
                    )
                if not ok_members:
                    # 与串行路径同款:报错留在 vlm span 内(V11 异常 span 契约)
                    members = list(cfg.ensemble_members) or [cfg.classifier]
                    telemetry.inc("scan.errors")
                    raise RuntimeError(
                        f"没有任何分类器成员成功完成评分,无法判定站点:{url}"
                        f"(请求的成员:{', '.join(map(str, members))};"
                        f"请检查 netsentinel.vision.* 相关模块或三方库是否就位)"
                    )
        except Exception:
            # P1-5 健康回报(与串行路径同款):系统性抛错记 dead 后重抛
            _load("netsentinel.vision.preflight").record_scan_outcome(
                preflight_meta, ok=False
            )
            raise
        telemetry.gauge(
            "scan.members_seconds", time.perf_counter() - members_started
        )
        if send_imgs:
            _load("netsentinel.vision.preflight").record_scan_outcome(
                preflight_meta, ok=True
            )
        gate_stats = gate.stats if gate.enabled else None
        # V10.37 SPRT 记账(只含名称/布尔/数字,红线 17):
        # budget_saved = 相对"无早停全量送审"基线省下的送审张数
        # (预算截下 + 判停后取消的排队分块;在飞分块已外呼,不计省)。
        sprt_info: dict[str, Any] | None = None
        if sprt is not None:
            sprt_info = {
                "kernel": "sprt",
                "allowed": True,
                "active": True,
                "verdict": sprt.state(),
                "n_used": int(sprt.n),
                "images_sent": int(gate.stats.sent),
                "budget_cap": int(budget_cap),
                "budget_saved": int(budget_skipped + sprt_cancelled),
                "stopped_early": bool(sprt_stop is not None or budget_stop),
                "stop_reason": (
                    f"sprt:{sprt_stop}" if sprt_stop is not None
                    else "budget" if budget_stop else ""
                ),
            }
            telemetry.gauge("sprt.n_used", float(sprt.n))
            telemetry.gauge("sprt.budget_saved", float(sprt_info["budget_saved"]))
        return (
            pages, raw_scores, ok_members, classify_failed, gate_stats, send_imgs,
            sprt_info,
        )
    finally:
        # 排队分块一律取消;**join 在飞分块后再放行异常**(wait=True):
        # 早夭/中止路径若不等待,在飞分块会在后台继续外呼(VLM 调用跨过
        # 中止边界,既烧预算也可能把调用记到下一站账上——实测复现为
        # 测试间计数串扰);等待上界 = 单个分块时长,确定性收敛,红线 37
        # 不留孤儿。
        chunk_pool.shutdown(wait=True, cancel_futures=True)


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

    # P1-5 扫描预检(``vlm_budget_precheck``,缺省 True;classifier 注入
    # 路径整体跳过——注入即调用方接管识别链路,预检不越权):爬取之前的
    # 三道免费闸门(计量检测 / 健康账本 / 本地预算),把系统性故障拦在
    # 爬取之前,杜绝"爬几分钟→评分全灭"的空烧(2026-10-13 实弹事故形态:
    # 余额耗尽后仍完整爬完 2 站 + 57 次 429 盲打)。
    preflight_meta = None
    if classifier is None:
        preflight_meta = _load("netsentinel.vision.preflight").preflight_scan(cfg, url)

    # P1-3 站内流水线重叠(``cfg.scan_pipeline_overlap``,缺省 True):仅在
    # **全默认路径**(未注入 capture / fetch_page / classifier)启用——
    # 主线程逐页礼貌爬取、后台按页分块并发评分,VLM 等待藏进爬取窗口;
    # 注入任一组件 = 调用方接管,走原串行路径(逐字节旧行为)。
    #
    # V10.37 SPRT 实时早停(P2-6,``cfg.use_sprt`` 缺省 False = 逐字节
    # 旧行为):仅在流水线路径激活,且必须通过内核门禁执行器
    # (:mod:`netsentinel.vision.kernel_gate`)的**三道锁**(测量/批准/
    # 指纹)——早停改变「哪些图被评分」,是全提案唯一动准确率的项,
    # α/β 担保以校准概率为前提,门禁与运营者显式批准是担保成立的制度
    # 前提。门禁未过 = **响亮降级**(全量送审旧行为 + intel 留 inactive
    # 标记 + 遥测计数)——「不开」而非「不许扫」,扫描本身不被阻塞。
    use_overlap = (
        bool(getattr(cfg, "scan_pipeline_overlap", True))
        and capture is None
        and fetch_page is None
        and classifier is None
    )
    sprt_intel: dict[str, Any] | None = None
    sprt_active = False
    if bool(getattr(cfg, "use_sprt", False)):
        if classifier is not None:
            sprt_intel = {
                "kernel": "sprt", "allowed": False, "active": False,
                "reason": "classifier 注入路径不启用 SPRT(调用方接管识别链路)",
            }
        elif not use_overlap:
            sprt_intel = {
                "kernel": "sprt", "allowed": False, "active": False,
                "reason": "scan_pipeline_overlap 关闭(串行路径)不启用 SPRT",
            }
        else:
            try:
                sprt_gate_res = _load(
                    "netsentinel.vision.kernel_gate"
                ).evaluate_kernel_gate(cfg, "sprt")
            except Exception as exc:  # noqa: BLE001 - 门禁自身故障按未过处理
                telemetry.inc("sprt.gate.refused")
                sprt_intel = {
                    "kernel": "sprt", "allowed": False, "active": False,
                    "reason": f"门禁执行器异常:{exc}",
                }
                logger.warning(
                    "SPRT 门禁执行器异常,按未通过处理(本站全量送审旧行为):%s", exc
                )
            else:
                if sprt_gate_res.allowed:
                    sprt_active = True
                    logger.info(
                        "SPRT 实时早停已激活(门禁三锁全过;批准于 %s)",
                        sprt_gate_res.approved_at or "?",
                    )
                else:
                    telemetry.inc("sprt.gate.refused")
                    sprt_intel = dict(sprt_gate_res.as_intel())
                    sprt_intel["active"] = False
                    logger.warning(
                        "SPRT 早停未激活(门禁未过,本站全量送审旧行为):%s",
                        ";".join(sprt_gate_res.reasons),
                    )

    pages: list[PageSample] = []
    send_imgs: list[ImageEvidence] = []
    raw_scores: list[ImageScore] = []
    ok_members: list[str] = []
    classify_failed = 0
    gate_stats: Any | None = None
    imgs_collected = 0

    if use_overlap:
        (
            pages, raw_scores, ok_members, classify_failed, gate_stats, send_imgs,
            sprt_intel_pipe,
        ) = _pipelined_fetch_and_classify(
            url, cfg, trace, preflight_meta, sprt_active=sprt_active
        )
        if sprt_intel_pipe is not None:
            sprt_intel = sprt_intel_pipe
        imgs_collected = int(
            getattr(gate_stats, "collected", 0)
        ) if gate_stats is not None else len(send_imgs)
    else:
        with _span(trace, "scan.fetch", stage="fetch"):
            urls = _default_discover(url, cfg, fetch_page)
            logger.info("链接发现完成,待抽样页面 %d 个", len(urls))

            # V10.13:缺省链路(未注入 capture / fetch_page)走批量会话采样,
            # 一次浏览器启动逐页复用;注入路径保持逐页调用,行为与旧版一致。
            if capture is None and fetch_page is None:
                pages = list(_default_capture_many(urls, cfg))
            else:
                cap = capture if capture is not None else _default_capture
                pages = [cap(u, cfg, fetch_page=fetch_page) for u in urls]

            imgs: list[ImageEvidence] = []
            for page in pages:
                imgs.extend(page.image_evidences)
            _enrich_images(imgs)
            imgs_collected = len(imgs)
            logger.info("页面采样完成:%d 页 / %d 张图片", len(pages), len(imgs))

        with _span(trace, "scan.vlm", stage="vlm", members=len(cfg.ensemble_members or [])):
            # P0-1 送审闸门(``cfg.vlm_send_gate`` 附加属性,缺省 True):判定
            # 口径前置过滤 + 送审级去重。被拦图(已测得小图 / 同内容重复 /
            # 显式开启时的未测得图)在旧行为里也不参与判定聚合——assess 只扫
            # "宽或高 >= min_image_px" 候选、ensemble 按 path 归并重复——判定
            # 输入集不变,省下的是纯外呼成本(实测 mrdsk 30 送 17 用、受控基准
            # 19 送 9 用);口径一致性由 tests/test_send_gate.py 网格对账锁定。
            send_imgs, gate_stats = _load(
                "netsentinel.vision.send_gate"
            ).apply_send_gate(imgs, cfg)
            members_started = time.perf_counter()
            try:
                raw_scores, ok_members, classify_failed = _collect_member_scores(
                    send_imgs, cfg, classifier
                )
            except Exception:
                # P1-5 健康回报:评分阶段系统性抛错(V10.24 全败中止 / 无可用
                # 成员)记 dead,TTL 内后续站点预检直接跳过;记录后原样重抛。
                # KeyboardInterrupt 等非 Exception 不记——人为中断不是提供方的错。
                _load("netsentinel.vision.preflight").record_scan_outcome(
                    preflight_meta, ok=False
                )
                raise
            if send_imgs:
                # 评分完整返回(含部分图片失败——提供方仍活着)记 ok 并清除
                # dead;零送审(全小图站)不回报,不扰动既有健康状态。
                _load("netsentinel.vision.preflight").record_scan_outcome(
                    preflight_meta, ok=True
                )
            telemetry.gauge(
                "scan.members_seconds", time.perf_counter() - members_started
            )
            if not ok_members:
                members = list(cfg.ensemble_members) or [cfg.classifier]
                telemetry.inc("scan.errors")
                raise RuntimeError(
                    f"没有任何分类器成员成功完成评分,无法判定站点:{url}"
                    f"(请求的成员:{', '.join(map(str, members))};"
                    f"请检查 netsentinel.vision.* 相关模块或三方库是否就位)"
                )

    # —— 以下为两路共用尾段(原串行路径 scan.vlm span 的尾段外提;trace
    #    默认关,span 边界变化只影响可选追踪呈现,不影响任何行为;"无可用
    #    成员"报错仍留在各路 vlm span 内——V11 异常 span 契约不变)——

    # 校准驱动的动态集成:开关开启且可靠性样本充足(MIN_N)时按可靠性
    # 权重加权(bayes_reliability 开启走贝叶斯收缩权重并优先于 Brier 开关;
    # 关闭 / 样本不足 / 内核未就位一律回退等权)——等权分支的调用口径与
    # 旧版逐字一致(不传 weights),保证默认行为零变化。
    ensemble_weights = _reliability_ensemble_weights(cfg, ok_members)
    if ensemble_weights is not None:
        ensemble = _default_ensemble(raw_scores, ensemble_weights)
    else:
        ensemble = _default_ensemble(raw_scores)
    # V10.21 GLM 第三方分歧仲裁接线(``cfg.arbiter_enabled`` 附加属性门控,
    # 缺省 False = 现状零变化):ensemble 聚合之后、verdict 判定之前,对
    # 成员分歧达 DISAGREE_GAP 的图送仲裁替换 ensemble 条目;无分歧 /
    # 离线 / 预算尽 / 异常一律原样返回,判定消费的集成结果与旧行为一致。
    ensemble = _wire_arbiter(ensemble, raw_scores, cfg)
    report = _default_assess(url, pages, ensemble, cfg)

    # V2:特征融合(URL 情报 + 页面级 VLM 理解,只升不降)。
    # 兄弟模块未就位/VLM 离线时自动跳过,不影响 v1 行为。
    # 文本融合接线(``cfg.enable_text_fusion`` 附加属性门控,缺省 False =
    # 恒传空 dict 的现状):开启后把各页 PageSample 的文本线索经
    # :func:`_site_text_features`(intel.text_intel 口径)计入融合——文本
    # 命中可能抬升 fused 概率并上调判定档位(只升不降),故默认关闭;
    # 开启方式:``cfg.enable_text_fusion = True``。
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
                # V10.18 可观测补齐:text_fusion 门控开关两侧各记一笔计数
                # (只加 telemetry.inc,零行为变化;门控语义本身不动)。
                if getattr(cfg, "enable_text_fusion", False):
                    telemetry.inc("scan.fusion.text.enabled")
                    text_feat = _site_text_features(pages)
                else:
                    telemetry.inc("scan.fusion.text.skipped")
                    text_feat = {}
                report = _load("netsentinel.decision.fusion").fuse(
                    report, url_feat, text_feat, page_vlm_feat, cfg
                )
                logger.info(
                    "融合分析完成:fused=%.4f(图像 agg=%.4f 保持不变)",
                    report.intel.get("fusion", {}).get("prob", 0.0),
                    report.agg_nsw_prob,
                )
            except Exception as exc:  # noqa: BLE001 —— 融合是增强项,失败不阻断主流程
                telemetry.inc("scan.fusion.skipped")
                logger.warning("融合分析已跳过:%s", exc)

    # P0-1 闸门记账进 intel:只在**确有拦截**时写入(零拦截主线 intel
    # 键集与旧行为逐字节一致——沿用"零失败主线零扰动"哲学);只含名称
    # 与数字,不含 URL/路径内容。注意必须写在融合块**之后**:fusion.fuse
    # 会整体替换 report.intel(decision/fusion.py),放前面会被抹掉。
    if gate_stats is not None and gate_stats.saved_calls > 0:
        report.intel["sendgate"] = gate_stats.as_dict()

    # V10.37 SPRT 实时早停记账进 intel:仅 ``use_sprt`` 开启时出现(开启
    # 但门禁未过 = inactive 标记 + 理由);只含名称/布尔/数字/短句理由
    # (红线 17)。与 sendgate 同位:必须写在融合块**之后**(fusion.fuse
    # 整体替换 report.intel);kernel_wire 的事后演算见到 ``active`` 键
    # 一律不覆写(单一事实源)。
    if sprt_intel is not None:
        report.intel["sprt"] = sprt_intel

    # 分歧弃权接线(默认关闭 = 现状):判定完成之后运行,只动 needs_review
    # 标记与 intel 记录,三档判定 / 聚合分 / 计数原样保留(红线)。
    abstain_hint = _wire_abstain(raw_scores, report, cfg)

    # V10.24 识别失败诚信升级(默认开启):有图片级评分失败时 needs_review
    # 强制 True(verdict 原档不动)+ 入列 note 中文标注 + clean 警示行;
    # 零失败 / 开关关闭返回 None 且零副作用(主线逐字节不变)。
    classify_note = _escalate_classify_failures(report, cfg, classify_failed)

    # V5 可观测:按最终判定档位计数,并记录本轮抽样规模。
    telemetry.inc(f"scan.verdict.{report.verdict.value}")
    telemetry.gauge("scan.pages", len(pages))
    telemetry.gauge("scan.images", imgs_collected)
    telemetry.gauge("scan.images_sent", len(send_imgs))

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
            try:
                # 入列附加实参按触发源逐项合成:abstain 提权加项(分歧度)与
                # V10.24 识别失败 note(诚信标注)可同时在场;零失败主线
                # kwargs 为空 → 调用口径与旧版逐字一致(仅位置实参)。
                enqueue_kwargs: dict[str, Any] = {}
                if abstain_hint is not None:
                    enqueue_kwargs["priority_weight"] = float(
                        abstain_hint["priority_weight"]
                    )
                if classify_note is not None:
                    enqueue_kwargs["note"] = classify_note
                if enqueue_kwargs:
                    entry_id = int(queue.add(report, bundle_zip, **enqueue_kwargs))
                else:
                    entry_id = int(queue.add(report, bundle_zip))
            finally:
                # V10.12:批量池下每站都会走到这里,连接用毕即关(注入的
                # 假队列可能没有 close,getattr 容错跳过)。
                close = getattr(queue, "close", None)
                if callable(close):
                    close()
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
    if gate_stats is not None and gate_stats.saved_calls > 0:
        # P0-1:闸门确有拦截时审计附送审数(零拦截主线审计载荷逐字节
        # 不变);与 intel["sendgate"] 同条件出现,便于对账。
        audit_fields["images_sent"] = len(send_imgs)
    audit.log_event(
        "scan",
        site=url,
        verdict=report.verdict.value,
        agg=report.agg_nsw_prob,
        needs_review=report.needs_review,
        pages=len(pages),
        images=imgs_collected,
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
    :raises RuntimeError: 没有任何分类器成员成功完成评分、首笔成员批量评分
        全部失败且失败数 ≥ ``cfg.classify_abort_threshold``(V10.24 快速中止,
        缺省 5),或兄弟模块未就位。
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
    try:
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
    finally:
        # V10.12 对齐(扫描路径同款修法):submit 路径的队列连接同样用毕
        # 即关——含条目不存在/状态非法/频控拒绝等提前 return/raise 的分支;
        # 注入的假队列可能没有 close,getattr 容错跳过。
        close = getattr(queue, "close", None)
        if callable(close):
            close()


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
