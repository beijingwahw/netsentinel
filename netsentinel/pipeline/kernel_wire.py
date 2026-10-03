"""内核装配线(V7 · A139,CONTRACTS-V7.md §2)。

把 V7 八大内核按 ``Config`` 开关**装配**成一个 :class:`KernelBox`,并提供
``run_scan_v7``——对冻结的 :func:`netsentinel.pipeline.orchestrator.run_scan`
的**外层包装**(orchestrator 一字不改,红线 29):

- :func:`assemble`:按开关解析 SPRT 实例 / 融合函数(可靠性加权 or 既有
  fusion.fuse)/ 执行器类(会话复用 or executor_playwright),``flags`` 记录
  装配时刻各开关的**实际生效状态**(内核缺失降级时为 False);
- :func:`wrap_classifier`:可选 LRU 缓存包装工厂
  (:class:`~netsentinel.vision.cache2.CachedClassifier`),命中零内层调用;
- :func:`run_scan_v7`::

      report = run_scan_v7(url, cfg, fetch_page=..., capture=...,
                           classifier=..., lru=MemoLRU(256))

  内部透传 ``orchestrator.run_scan(url, cfg, **deps)``,并按开关做三件事:

  1. ``classifier`` 依赖注入且 ``lru`` 在席 → 用 :func:`wrap_classifier`
     包装(仅当传入 lru 存在;不传 lru 即 v6 原样透传);
  2. ``cfg.use_sprt`` → run_scan 返回后取 ``report.image_scores`` 的
     ensemble 条目做 :func:`~netsentinel.decision.sprt.next_images` 送审
     演算,结果写入 ``report.intel["sprt"] = {"verdict","n_used",
     "budget_saved"}``——**纯预算参考,绝不改动 verdict / needs_review /
     agg_nsw_prob 等任何判定字段**(SPRT 是"下一轮该送审几张"的省钱演算,
     不是站点判定;红线:开启 SPRT 后报告的判定结论与关闭时完全一致);
  3. ``cfg.use_reliability_fusion`` 且 ``report.intel`` 已有 "fusion"(即
     orchestrator 的 use_fusion 融合已成功)→ 用
     :class:`~netsentinel.decision.reliability.ReliabilityTracker`
     (``cfg.data_dir/reliability.jsonl``,惰性创建)补算
     :func:`~netsentinel.decision.fusion_reliable.fuse_reliable` 覆写
     ``intel["fusion"]``;fuse_reliable 自带**只升不降兜底**(verdict 档位
     与 needs_review 单向恒真,绝不洗白),覆写时保留 fusion 之外的既有
     intel 键(如 ② 写入的 "sprt")。

v6 完全一致(断言锁定,见 tests/test_kernel_wire.py):开关全关
(use_sprt=False / use_reliability_fusion=False)且未传 lru 时,deps 逐参
透传、报告对象原样返回、**零 intel 侵入**——与直接调用 run_scan 无任何
差异。

安全红线(V7 §0):

- **红线 29(零 API 破坏)**:纯新增文件;orchestrator 及全部既有模块
  一字不改;开关默认值 = 旧行为(use_sprt / use_reliability_fusion 默认
  False;LRU 只在调用方显式传 lru 时生效);
- **红线 30(零外呼)**:SPRT 演算与可靠性加权均只消费本地分数与本地
  jsonl 反馈,不联网、不调 VLM;
- **红线 31(基准可复现)**:``tests/test_kernel_wire.py`` 的
  ``test_v7_bench_*`` 以**操作计数**断言(LRU 重复评分内层调用 5→1;
  SPRT 早停送审 2/8 张省 6 张),零墙钟;
- 内核缺失(惰性导入失败)一律**优雅降级 + 中文日志**:装配时开关回退
  False,运行时跳过该步后处理并保留既有结论,scan 主流程绝不中断。

SPRT 校准假设(承 decision/sprt.py):送审概率须经校准(GLM calibrate
后近似);stub 未校准分数不满足 α/β 担保,故 ``cfg.use_sprt`` 默认 False,
由调用方/装配线把关。

用法示例::

    from netsentinel.contracts import Config
    from netsentinel.pipeline.kernel_wire import assemble, run_scan_v7

    box = assemble(Config())          # 按开关装配(SPRT/融合/执行器/LRU)
    report = run_scan_v7("http://127.0.0.1/a", Config())   # = run_scan(v6)

A194 站群图谱通电(默认关闭 = 现状,经 ``cfg.graph_wire`` 实例开关门控,
contracts 冻结不改,消费方 ``getattr(cfg, "graph_wire", False)`` 读):

- :func:`wire_graph_from_scan` —— 扫描批末把本站素材指纹灌入 A46
  ``EvidenceGraph``:``add_site`` / 图片 sha256 ``add_image`` / 页面模板
  simhash ``add_template``,再 ``link_shared_images`` / ``link_templates``
  折叠关联边;经 :mod:`netsentinel.intel.phash` + ``phash_lsh`` LSH 近邻
  查询跨站近重复图,写 ``phash_near`` 边(A204 起走 A46 公开 ``add_edge``
  写口;A194 时代旧私有 ``_upsert_site_edge`` 探测回退已按 A204→A214
  兼容期承诺移除);A204 登记步把本批 pHash 写入 ``PhashRegistry`` +
  持久 ``MultiTableLSH``,后续批次的近邻查询即命中本批指纹(跨批次
  闭环);A214 起查询步按库规模自适应:**注册库登记数超过可配阈值
  (缺省 5000)时跳过每批 O(N) 全量重灌、只查持久多表 LSH(fastpath)**,
  小库保持"注册库重灌 ∪ 多表 LSH"双源并查不变(slowpath);任一步
  失败**安全降级**(中文日志 + telemetry 计数,绝不中断扫描主流程);
- :func:`resolve_gangs` / :func:`resolve_gangs_from_config` —— 团伙判定:
  ``connectivity`` 模式(默认)经 :class:`~netsentinel.intel.graph_kernel.
  UnionFindKernel` 纯连通并团(一条弱边即并;A244 起唯一例外:
  ``mirror_near`` 镜像候选边默认**不**并团,见下);``community`` 模式
  按边权重过滤 + ``shared_template`` / ``mirror_near`` 降权后走
  :func:`~netsentinel.intel.graph_kernel.louvain_communities` 加权社区
  检测(强关联成团、建站工具弱边与镜像候选边不再误并)。

A229 多哈希生产接线(CONTRACTS-V14 §2 指纹纵深第 3/4 项;默认关闭 =
现状单哈希,经 ``cfg.graph_wire_multihash`` 附加属性门控,``getattr``
缺省 False,contracts 冻结不改):

- 登记步 :func:`_register_phash` 对每图增算 mirror_hash(64bit 镜像不变
  规范形)与 pyramid_hash(108bit 多尺度),``PhashRegistry.register``
  一次写三列(A219 建好的多哈希列自此被生产链路消费);mirror 另建
  **独立持久多表 LSH 实例**(文件 ``<phash_db>.mtlsh.mirror``,实例标识
  ``name="mirror"``,A229 泛化)供跨批次翻转近邻检索;pyramid 27 hex
  非定长 64bit、查询语义是三层取 min,**不适合** MT-LSH 汉明桶——只入
  Registry 列供 ``find_similar(hash_kind="pyramid")`` 精确查(取舍详见
  :func:`_register_phash` docstring);
- 建边步 :func:`_wire_phash_near` 增 mirror 源:mirror 索引近邻命中
  (距离 ≤ ``cfg.graph_wire_mirror_distance``,缺省 12 = A219 建议距)
  并入建边;A244 起镜像命中落**独立边种类 ``mirror_near``**(此前挂
  ``phash_near``、图上无法区分来源——A229 交付报告点名),phash 源
  命中仍落 ``phash_near``,两通道互不混流(phash 已命中同 sha 的镜像
  命中去重、只计一次);**候选语义(红线 48)**:多哈希命中是候选
  生成而非判定——mirror 丢弃翻转方向信息(互为翻转的异图不可分),
  命中只生成 ``mirror_near`` 候选关联边,须经既有 phash256 / 人工
  复核确认;消费侧 :func:`resolve_gangs` 对该来源降权并默认不直接
  并团(红线 48 的强化);权重语义沿用"近重复占比"(该站命中次数 ÷
  本批成功哈希图数,0~1);telemetry 计 ``graph_wire.mirror_hits``
  (命中)与 ``graph_wire.mirror_edge``(成边);
- 三哈希批内惰性计算(开关关 = 零额外图片解码、零额外落盘,与
  A204/A214 现状逐字节一致);逐图失败降级计数
  (``graph_wire.multihash_skipped``,该图仍按 phash 登记);vision/phash2
  内核缺失时整批退回单哈希现状(中文告警,不中断扫描)。

零外呼(红线 30):只读写本地 SQLite 图谱 / 哈希库与本地图片文件;操作
全部记 telemetry 计数(红线 17:只存名称与数字)。
"""
from __future__ import annotations

import hashlib
import importlib
import logging
import pathlib
import re
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config, SiteReport

__all__ = [
    "KernelBox",
    "assemble",
    "resolve_gangs",
    "resolve_gangs_from_config",
    "run_scan_v7",
    "wire_graph_from_scan",
    "wrap_classifier",
]

logger = logging.getLogger(__name__)

#: 可靠性反馈 jsonl 文件名(挂接在 ``cfg.data_dir`` 下;A126 数据面)
RELIABILITY_JSONL_NAME = "reliability.jsonl"


def _load(module_name: str) -> Any:
    """惰性导入兄弟内核模块;缺失时抛中文 ``RuntimeError``(契约 §2 口径)。"""
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(f"模块 {module_name} 未就位:{exc}") from exc


# ---------------------------------------------------------------------------
# 缺省依赖工厂(模块级,便于测试 monkeypatch 稳定替换;orchestrator 冻结不改)
# ---------------------------------------------------------------------------
def _default_run_scan(url: str, cfg: Config, **deps: Any) -> SiteReport:
    """缺省 scan 入口:透传 orchestrator.run_scan(**deps)(一字不改)。"""
    from netsentinel.pipeline import orchestrator  # 兄弟模块惰性导入(契约 §2)

    return orchestrator.run_scan(url, cfg, **deps)


class _LazyFuse:
    """惰性融合函数:首次调用才解析目标内核,避免装配期耦合兄弟模块。

    ``reliable=True`` → :func:`~netsentinel.decision.fusion_reliable.fuse_reliable`
    (接受 ``tracker=`` 关键字);``reliable=False`` → 既有
    :func:`netsentinel.decision.fusion.fuse`(v6 行为,不识别 tracker)。
    两者 ``(report, url_feat, text_feat, page_vlm, cfg)`` 前五位同构。
    """

    __slots__ = ("_fn", "reliable")

    def __init__(self, reliable: bool) -> None:
        self.reliable = bool(reliable)
        self._fn: Callable[..., SiteReport] | None = None

    def __call__(
        self,
        report: SiteReport,
        url_feat: dict[str, Any],
        text_feat: dict[str, Any],
        page_vlm: dict[str, Any],
        cfg: Config,
        **kwargs: Any,
    ) -> SiteReport:
        if self._fn is None:
            if self.reliable:
                self._fn = _load("netsentinel.decision.fusion_reliable").fuse_reliable
            else:
                self._fn = _load("netsentinel.decision.fusion").fuse
        return self._fn(report, url_feat, text_feat, page_vlm, cfg, **kwargs)

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"_LazyFuse(reliable={self.reliable!r}, resolved={self._fn is not None})"


# ---------------------------------------------------------------------------
# LRU 缓存包装工厂
# ---------------------------------------------------------------------------
def wrap_classifier(inner: Any, lru: Any | None = None) -> Any:
    """LRU 缓存包装工厂:``lru`` 在席时返回 ``CachedClassifier(inner, lru)``。

    - ``lru is None`` → **原样返回 inner**(不缓存,v6 行为;可选包装的"关");
    - ``lru`` 在席 → 惰性导入 ``netsentinel.vision.cache2`` 构造
      :class:`~netsentinel.vision.cache2.CachedClassifier`(命中零内层调用,
      model 字段透传,下游 ensemble 看到的仍是真实评分来源);
    - cache2 未就位 → 抛中文 ``RuntimeError``,由调用方决定降级
      (:func:`run_scan_v7` 会捕获并按 v6 原样透传 inner + 中文告警)。
    """
    if lru is None:
        return inner
    cache2 = _load("netsentinel.vision.cache2")
    return cache2.CachedClassifier(inner, lru=lru)


# ---------------------------------------------------------------------------
# 装配线
# ---------------------------------------------------------------------------
@dataclass
class KernelBox:
    """按 ``Config`` 开关装配出的内核集合(纯数据容器,无隐藏状态)。

    :param sprt:         SPRT 实例(use_sprt 开且内核在席;否则 None);
    :param fuse_fn:      融合函数(可靠性加权 or 既有 fusion.fuse 的惰性封装,
                         见 :class:`_LazyFuse`;始终非 None);
    :param executor_cls: ``SessionExecutor`` 类(browser_session_reuse 开且
                         内核在席;None 表示继续用 executor_playwright.execute,
                         即 orchestrator.run_submit 的 v6 缺省路径);
    :param lru:          LRU 实例(**不由 cfg 开关控制**,默认 None;调用方按需
                         ``box.lru = MemoLRU(256)`` 注入后
                         :meth:`wrap_classifier` 即生效);
    :param flags:        装配时刻各开关的**实际生效**快照
                         (``{"sprt","reliability_fusion","browser_session_reuse",
                         "lru_cache"}`` → bool;内核缺失降级后为 False)。
    """

    sprt: object | None = None
    fuse_fn: Callable[..., SiteReport] | None = None
    executor_cls: object | None = None
    lru: object | None = None
    flags: dict[str, bool] = field(default_factory=dict)

    def wrap_classifier(self, inner: Any) -> Any:
        """用 ``box.lru`` 包装分类器;lru 未注入时原样返回 inner(v6 行为)。"""
        return wrap_classifier(inner, self.lru)


def assemble(cfg: Config) -> KernelBox:
    """按 ``Config`` 开关装配内核集合;内核缺失时逐项优雅降级(中文日志)。

    装配规则(契约 §2 A139):

    - ``cfg.use_sprt`` → ``SPRT(cfg.sprt_alpha, cfg.sprt_beta)`` 实例;
    - ``cfg.use_reliability_fusion`` → fuse_reliable(装配期仅就位性探测);
      否则(或内核缺失降级)→ 既有 ``fusion.fuse`` 的惰性封装(v6 口径);
    - ``cfg.browser_session_reuse`` → ``SessionExecutor`` 类;否则 None
      (= executor_playwright.execute,v6 缺省执行路径);
    - LRU 不受 cfg 控制:``box.lru`` 恒为 None,由调用方按需注入。

    降级语义:任一内核惰性导入失败 → 对应字段回退关闭态(v6 行为)、
    ``flags`` 记 False、记中文 warning;**绝不向调用方抛异常**——装配线
    的职责是"能装多少装多少,装不上的回到 v6"。

    :param cfg: 全局配置(读 use_sprt / sprt_alpha / sprt_beta /
                use_reliability_fusion / browser_session_reuse)。
    :return: :class:`KernelBox`;``flags`` 为实际生效开关快照。
    """
    flags: dict[str, bool] = {
        "sprt": False,
        "reliability_fusion": False,
        "browser_session_reuse": False,
        "lru_cache": False,
    }

    sprt: object | None = None
    if cfg.use_sprt:
        try:
            sprt = _load("netsentinel.decision.sprt").SPRT(
                cfg.sprt_alpha, cfg.sprt_beta
            )
            flags["sprt"] = True
        except Exception as exc:  # noqa: BLE001 - 内核缺失/参数非法均降级为关闭
            logger.warning("SPRT 内核未就位,use_sprt 已降级为关闭(v6 行为):%s", exc)

    reliable = bool(cfg.use_reliability_fusion)
    if reliable:
        try:
            _load("netsentinel.decision.fusion_reliable")  # 就位性探测(惰性)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "可靠性融合内核未就位,use_reliability_fusion 已降级为既有"
                " fusion.fuse(v6 行为):%s",
                exc,
            )
            reliable = False
    flags["reliability_fusion"] = reliable

    executor_cls: object | None = None
    if cfg.browser_session_reuse:
        try:
            executor_cls = _load("netsentinel.submit.executor_session").SessionExecutor
            flags["browser_session_reuse"] = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "会话复用执行内核未就位,已降级为 executor_playwright(v6 行为):%s", exc
            )

    return KernelBox(
        sprt=sprt,
        fuse_fn=_LazyFuse(reliable=reliable),
        executor_cls=executor_cls,
        lru=None,
        flags=flags,
    )


# ---------------------------------------------------------------------------
# run_scan_v7:orchestrator.run_scan 的开关化包装(orchestrator 冻结不改)
# ---------------------------------------------------------------------------
def _sprt_intel(report: SiteReport, cfg: Config) -> dict[str, Any]:
    """SPRT 送审演算 → intel["sprt"] 三键(纯预算参考,不改任何判定字段)。

    取 ``report.image_scores`` 的 ensemble 条目,经
    :func:`~netsentinel.decision.sprt.next_images` 按"不确定度升序逐张送审、
    判停即截断"演算::

        verdict       SPRT 自身状态("nsfw" / "clean" / "continue")——
                      是**送审序列的统计结论**,与站点 verdict 无关、不回写;
        n_used        实际"送审"张数(早停即截断);
        budget_saved  省下的送审张数 = min(vlm_max_images_per_site,
                      ensemble 条目数) - n_used(无早停基线则全花光 = 0)。
    """
    sprt_mod = _load("netsentinel.decision.sprt")
    sent, sprt = sprt_mod.next_images(report.image_scores, cfg)
    n_used = len(sent)
    ensemble_n = sum(1 for s in report.image_scores if s.model == "ensemble")
    budget = max(0, min(int(cfg.vlm_max_images_per_site), ensemble_n))
    return {
        "verdict": sprt.state(),
        "n_used": n_used,
        "budget_saved": max(0, budget - n_used),
    }


def _apply_reliable_fusion(report: SiteReport, cfg: Config, box: KernelBox) -> None:
    """补算可靠性加权融合,覆写 ``intel["fusion"]``(只升不降兜底)。

    - tracker 惰性构造:``ReliabilityTracker(cfg.data_dir/reliability.jsonl)``
      (零外呼:只读本地运营者反馈;文件缺失 = 无权重 → 成员等权 = 旧行为);
    - 特征取自 orchestrator 融合写入的原三路情报(``intel["url"/"text"/
      "page_vlm"]``,空 dict 视为缺失,与 fusion 同口径);
    - ``fuse_reliable`` 就地覆写 intel 四键并按只升不降重算 verdict /
      needs_review(档位/复核单向恒真,绝不洗白——兜底即内核自带);
    - 覆写后**回填 fusion 之外的既有 intel 键**(如先写入的 "sprt"),
      保证后处理之间互不覆盖。
    """
    reliability = _load("netsentinel.decision.reliability")
    tracker_path = str(pathlib.Path(cfg.data_dir) / RELIABILITY_JSONL_NAME)
    tracker = reliability.ReliabilityTracker(tracker_path)

    kept = dict(report.intel)
    url_feat = kept.get("url") or {}
    text_feat = kept.get("text") or {}
    page_vlm = kept.get("page_vlm") or {}
    box.fuse_fn(report, url_feat, text_feat, page_vlm, cfg, tracker=tracker)

    for key, value in kept.items():
        if key not in report.intel:
            report.intel[key] = value


def run_scan_v7(
    url: str,
    cfg: Config,
    *,
    lru: Any | None = None,
    **deps: Any,
) -> SiteReport:
    """开关化扫描入口:透传 ``orchestrator.run_scan`` 并按开关套 V7 内核。

    与 v6 的关系(红线 29):本函数是 orchestrator.run_scan 的**外层包装**,
    orchestrator 一字不改;开关全关(use_sprt=False /
    use_reliability_fusion=False)且未传 ``lru`` 时,deps 逐参透传、报告
    对象原样返回、零 intel 侵入——与直接调用 run_scan 完全一致(测试断言
    锁定)。

    :param url:  目标站点 URL(透传)。
    :param cfg:  全局配置(读 use_sprt / use_reliability_fusion /
                 sprt_* / vlm_max_images_per_site / data_dir)。
    :param lru:  可选 LRU(通常 ``assemble(cfg).lru`` 或自建 ``MemoLRU``);
                 **仅当它与 ``classifier`` 依赖同时注入**时才包装缓存,
                 单独传入不产生任何效果(orchestrator 无可包装对象)。
    :param deps: 透传给 ``orchestrator.run_scan`` 的依赖
                 (``fetch_page`` / ``capture`` / ``classifier``);
                 ``classifier`` 在 lru 在席时会被替换为
                 :class:`~netsentinel.vision.cache2.CachedClassifier` 包装
                 (``.inner`` 即原分类器)。
    :return:     orchestrator 返回的同一 :class:`SiteReport` 对象(id 不变),
                 视开关附加 ``intel["sprt"]`` / 覆写 ``intel["fusion"]``。
    :raises RuntimeError: 透传 orchestrator 的失败(如无分类器成员可用);
                          V7 后处理自身失败一律**降级跳过**,绝不中断主流程。
    """
    box = assemble(cfg)

    # ① 分类器依赖注入 + lru 在席 → LRU 缓存包装(失败降级为 v6 原样透传)
    classifier = deps.get("classifier")
    effective_lru = lru if lru is not None else box.lru
    if classifier is not None and effective_lru is not None:
        try:
            deps["classifier"] = wrap_classifier(classifier, effective_lru)
        except Exception as exc:  # noqa: BLE001 - 缓存是增强项,失败不阻断扫描
            logger.warning("LRU 缓存包装失败,分类器按 v6 原样透传:%s", exc)

    report = _default_run_scan(url, cfg, **deps)

    # ② SPRT 送审演算 → intel["sprt"](纯预算参考;绝不改动 verdict 等判定字段)
    if box.flags.get("sprt"):
        try:
            report.intel["sprt"] = _sprt_intel(report, cfg)
        except Exception as exc:  # noqa: BLE001 - 预算参考缺失不影响判定
            logger.warning("SPRT 送审演算已跳过(判定结论不受影响):%s", exc)

    # ③ 可靠性加权融合补算 → 覆写 intel["fusion"](只升不降兜底由内核自带)
    if box.flags.get("reliability_fusion") and isinstance(
        report.intel.get("fusion"), dict
    ):
        try:
            _apply_reliable_fusion(report, cfg, box)
        except Exception as exc:  # noqa: BLE001 - 融合是增强项,保留既有结论
            logger.warning("可靠性融合补算已跳过(保留既有融合结论):%s", exc)

    return report


# ---------------------------------------------------------------------------
# A194 站群图谱通电:扫描批末写 A46 EvidenceGraph(默认关,zero-侵入)
# ---------------------------------------------------------------------------
#: ``phash_near`` 边种类字面量(与 A46 ``graph.EDGE_PHASH_NEAR`` 同值;
#: 为避免对兄弟模块建立导入期耦合,此处本地定义)。
_EDGE_PHASH_NEAR = "phash_near"
_EDGE_SHARED_TEMPLATE = "shared_template"
#: ``mirror_near`` 边种类字面量(A244;与 A46 ``graph.EDGE_MIRROR_NEAR``
#: 同值,同款本地定义):A229 mirror 源建边的独立通道——此前挂
#: ``phash_near`` 无法区分来源(A46 边表无 payload 字段),独立种类后
#: 消费侧 resolve_gangs 可按来源降权 / 默认不直接并团。
_EDGE_MIRROR_NEAR = "mirror_near"

#: URL 形状归一:连续数字折叠为 "0"(``/list_12.html`` 与 ``/list_99.html``
#: 同形——同模板建站工具的典型路径分页形态)。
_DIGITS_RE = re.compile(r"\d+")


def _url_shape_tokens(url: object) -> list[str]:
    """URL → 结构形状 token 列表(host 抹平、路径数字归零、保留扩展名)。"""
    text = str(url or "").strip()
    if not text:
        return []
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:  # pragma: no cover - 非法端口等脏输入
        return []
    tokens: list[str] = []
    for segment in (parts.path or "/").split("/"):
        shape = _DIGITS_RE.sub("0", segment.strip().lower())
        if shape:
            tokens.append(f"seg:{shape}")
    if parts.query:
        tokens.append("seg:?query")  # 有无查询串也是模板结构信号
    return tokens


def _page_template_key(page: Any) -> str:
    """PageSample → 64bit 页面模板 simhash(16 位 hex;纯函数,零 IO)。

    指纹源(全部归一化后取 token):页面 URL 路径形状、各图片 URL 路径
    形状(数字折叠,分页序号不影响指纹)、文本线索命中表。同模板建站
    工具产出的站点(路径结构 / 资源布局一致)指纹相同或极近,A46
    ``link_templates`` 按键折叠即得 ``shared_template`` 边;无任何 token
    (空 URL 且无图片无线索)返回空串,调用方跳过登记。
    """
    tokens: list[str] = []
    urls = [str(getattr(page, "url", "") or "")]
    for img in getattr(page, "image_evidences", None) or []:
        urls.append(str(getattr(img, "url", "") or ""))
    for url in urls:
        tokens.extend(_url_shape_tokens(url))
    for hit in sorted(str(h) for h in getattr(page, "text_hint_hits", None) or []):
        tokens.append(f"hint:{hit.lower()}")
    if not tokens:
        return ""
    bits = [0] * 64
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        value = int.from_bytes(digest[:8], "big")
        for i in range(64):
            bits[i] += 1 if (value >> i) & 1 else -1
    fingerprint = 0
    for i in range(64):
        if bits[i] > 0:
            fingerprint |= 1 << i
    return f"{fingerprint:016x}"


def _graph_commit(graph: Any) -> None:
    """尽力提交图谱连接的挂起事务(duck 访问,失败静默降级)。"""
    conn = getattr(graph, "_conn", None)
    if conn is None:
        return
    try:
        conn.commit()
    except Exception:  # noqa: BLE001 - 提交失败由上层计数降级,不上抛
        logger.debug("图谱事务提交失败(按降级处理)", exc_info=True)


def _graph_add_phash_near(
    graph: Any, url_a: str, url_b: str, weight: float, kind: str = _EDGE_PHASH_NEAR
) -> bool:
    """向图谱补一条近重复家族边(A214 起只走 A46 公开写口 ``add_edge``)。

    - 唯一写口 = ``graph.add_edge(url_a, url_b, kind=..., weight=...)``
      (A204 起在席、V12 落定的公开通用边通道,kind 白名单含全部五种
      契约边种类);A194 时代 A46 尚无公开写口时的私有
      ``_upsert_site_edge`` 探测回退已按 A204→A214 兼容期承诺**移除**——
      极端旧库实例缺 ``add_edge`` 时中文快失败,由调用方
      :func:`_wire_phash_near` 统一降级(告警 + telemetry + 返回 0);
    - ``kind`` 缺省 ``phash_near``(phash 源,既有口径不变);A244 起
      mirror 源传 ``mirror_near``——两来源分通道落边,消费侧可区分
      (函数名沿用历史,语义已泛化为"近重复家族边写入");
    - 两端站点节点用公开幂等口 ``add_site`` 补建;写后尽力 commit。
    - 单条边写入失败(写入异常)容错返回 ``False``(调用方计数降级,
      不中断整批写图)。
    """
    if not callable(getattr(graph, "add_edge", None)):
        raise RuntimeError(
            "EvidenceGraph 缺少公开写口 add_edge(A204/V12 起在席):"
            "旧私有 _upsert_site_edge 探测回退已于 A214 移除,"
            "请升级 netsentinel.intel.graph 后重试"
        )
    try:
        for url in (url_a, url_b):
            add_site = getattr(graph, "add_site", None)
            if callable(add_site):
                add_site(url)
        graph.add_edge(url_a, url_b, kind=kind, weight=weight)
        _graph_commit(graph)
        return True
    except Exception as exc:  # noqa: BLE001 - 单条边失败不阻断整批写图
        logger.debug(
            "%s 边写入失败,已容错跳过:%s -> %s", kind, url_a, url_b, exc_info=True
        )
    return False


def _phash_fingerprints(
    pages: list[Any], multihash: bool = False
) -> tuple[list[tuple[str, str, str, str]], int]:
    """本批页面图片 → ``[(sha256, phash, mirror, pyramid)…]`` 与跳过计数。

    哈希:A43 ``intel/phash.phash(path)``(64bit DCT;需 Pillow / 文件
    在席,任一缺失即该图跳过并计数——增强项,不影响其余流程)。phash
    内核未就位时抛中文 ``RuntimeError``,由调用方整体降级(登记与近邻
    建边一并跳过)。收集结果同时喂给登记步(:func:`_register_phash`)
    与查询步(:func:`_wire_phash_near`),每张图**只哈希一次**。

    A229 多哈希(``multihash=True``,经 ``cfg.graph_wire_multihash`` 门控,
    缺省 False = 零额外解码的现状):每图**批内惰性**增算
    ``vision.phash2.mirror_hash``(64bit 镜像不变规范形,16 hex)与
    ``pyramid_hash``(108bit 多尺度,27 hex)——算力预算可控:每图三次
    PIL 解码变换(phash / mirror / pyramid 各一),未开启时一次都不多算;
    逐图失败(文件损坏 / 单图解码异常)**逐图降级**:该图仍按 phash
    登记、对应列补空串并计 ``graph_wire.multihash_skipped``,不中断批;
    ``vision/phash2`` 内核整体缺失时退回单哈希现状(中文告警 + 计数)。
    """
    phash_mod = _load("netsentinel.intel.phash")
    phash2 = None
    if multihash:
        try:
            phash2 = _load("netsentinel.vision.phash2")
        except Exception as exc:  # noqa: BLE001 - 内核缺失退回单哈希现状
            telemetry.inc("graph_wire.multihash_skipped")
            logger.warning(
                "多哈希内核 vision/phash2 未就位,本批退回单哈希口径"
                "(登记与建边不受影响):%s",
                exc,
            )
    items: list[tuple[str, str, str, str]] = []
    skipped = 0
    seen_paths: set[str] = set()
    for page in pages:
        for img in getattr(page, "image_evidences", None) or []:
            path = str(getattr(img, "path", "") or "")
            if not path or path in seen_paths:
                continue
            seen_paths.add(path)
            sha = str(getattr(img, "sha256", "") or "").strip().lower()
            try:
                fingerprint = phash_mod.phash(path)
            except Exception:  # noqa: BLE001 - Pillow 缺失/文件不可解码
                skipped += 1
                continue
            mirror = ""
            pyramid = ""
            if phash2 is not None:
                try:
                    mirror = phash2.mirror_hash(path)
                except Exception:  # noqa: BLE001 - 逐图降级:该图 mirror 列空
                    telemetry.inc("graph_wire.multihash_skipped")
                try:
                    pyramid = phash2.pyramid_hash(path)
                except Exception:  # noqa: BLE001 - 逐图降级:该图 pyramid 列空
                    telemetry.inc("graph_wire.multihash_skipped")
            items.append((sha, fingerprint, mirror, pyramid))
    return items, skipped


def _mt_lsh_db_path(cfg: Config) -> str:
    """持久 ``MultiTableLSH`` 库路径:优先 ``cfg.phash_mt_lsh_db`` 附加属性
    (契约冻结不改,与 ``graph_wire`` 同款注入方式),缺省挂接在 phash_db
    旁(``<phash_db>.mtlsh``,同目录、同生命周期、同损坏重建语义)。"""
    override = str(getattr(cfg, "phash_mt_lsh_db", "") or "").strip()
    return override or f"{cfg.phash_db}.mtlsh"


#: mirror 持久多表 LSH 的文件后缀(挂接在主 phash mtlsh 路径之后,如
#: ``<phash_db>.mtlsh.mirror`` / ``<自定义>.mtlsh.mirror``;与主索引同
#: 目录、同生命周期、同损坏重建语义——A229 多哈希接线)。
_MIRROR_LSH_SUFFIX = ".mirror"


def _mirror_lsh_db_path(cfg: Config) -> str:
    """mirror 持久多表 LSH 库路径:沿 ``.mtlsh`` 惯例自主索引路径派生
    (``<phash_db>.mtlsh.mirror``;``phash_mt_lsh_db`` 覆盖时为
    ``<覆盖路径>.mirror``),保证主 / 镜像两实例各持独立文件、互不污染。"""
    return f"{_mt_lsh_db_path(cfg)}{_MIRROR_LSH_SUFFIX}"


def _multihash_enabled(cfg: Config) -> bool:
    """读多哈希接线开关(契约冻结不改,附加实例属性,``getattr`` 防御式
    读取);缺省 False = 现状单哈希(phash 单列 + 单 mtlsh 索引)。"""
    return bool(getattr(cfg, "graph_wire_multihash", False))


#: mirror 源建边的缺省汉明距离上限(64bit 域):取 A219
#: ``vision.phash2.MIRROR_SUGGESTED_MAX_DISTANCE``(实测翻转距离恒 0、
#: 异构图最小距 ≥ 16,12 居中留裕量);经 ``cfg.graph_wire_mirror_distance``
#: 附加属性覆盖(getattr 防御式读取)。
_MIRROR_DISTANCE_DEFAULT = 12


def _mirror_distance(cfg: Config) -> int:
    """读 mirror 源建边距离上限;缺失 / 非法(负数 / 超 64 / 不可解析)
    回缺省 12(A219 建议距,与 ``_phash_rebuild_limit`` 同款容错面)。"""
    try:
        value = int(
            getattr(cfg, "graph_wire_mirror_distance", _MIRROR_DISTANCE_DEFAULT)
        )
    except (TypeError, ValueError):
        return _MIRROR_DISTANCE_DEFAULT
    return value if 0 <= value <= 64 else _MIRROR_DISTANCE_DEFAULT


def _fp_parts(item: tuple[str, ...]) -> tuple[str, str, str, str]:
    """指纹条目 → ``(sha256, phash, mirror, pyramid)`` 四元组(归一化)。

    兼容 A204/A214 时代的 2 元组 ``(sha256, phash)``(mirror/pyramid 补
    空串 = 未登记,单哈希现状口径)与 A229 的 4 元组——登记步 / 建边步
    内部统一按四元组消费,旧调用方与既有测试零改动。
    """
    sha = str(item[0] or "").strip().lower()
    phash_hex = str(item[1] or "")
    mirror = str(item[2]) if len(item) > 2 and item[2] else ""
    pyramid = str(item[3]) if len(item) > 3 and item[3] else ""
    return sha, phash_hex, mirror, pyramid


def _hit_field(payload: Any, key: str) -> str:
    """LSH 负载安全取字符串字段(非 dict / 缺键 / 空值 → "",不抛错)。"""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get(key) or "")


def _register_phash(
    site_url: str, cfg: Config, fingerprints: list[tuple[str, ...]]
) -> int:
    """登记步(A204 跨批次闭环):本批指纹灌入 PhashRegistry + 持久多表 LSH。

    - ``PhashRegistry.register(sha256, phash_hex, site_url)``(A43 公开
      UPSERT 口,同 sha 重登覆盖):哈希库是**权威存档**——近邻查询的
      全量重灌索引(:func:`_wire_phash_near`)与复核台 find_similar 都以
      它为准,登记即让后续批次的查询命中本批指纹;
    - ``MultiTableLSH.insert + flush``(A195,只读 import,持久库挂
      ``<phash_db>.mtlsh``):**增量 ANN 索引**——懒加载历史批 + 本批
      追加,免每批全表重灌;同 sha 已在索引(公开 ``query`` 精确探针)
      时不重复插入,防跨批次重复登记膨胀权重;
    - 无 sha 的图片证据跳过登记(哈希库以 sha256 为主键),近邻查询
      不受影响;登记失败由调用方降级计数,不中断扫描。

    A229 多哈希登记(``cfg.graph_wire_multihash`` 开启时;缺省关 = 上方
    单哈希口径逐字节不变):

    - ``register`` 一次写三列——phash 主列 + mirror_hash(16 hex 镜像
      不变规范形)+ pyramid_hash(27 hex 多尺度),消费 A219 建好的
      ``PhashRegistry`` 多哈希列与 ``find_similar(hash_kind=...)`` 复核口;
    - mirror 另建**独立持久多表 LSH 实例**(``_mirror_lsh_db_path`` =
      ``<phash_db>.mtlsh.mirror``,实例标识 ``name="mirror"``):mirror 是
      定长 64bit 汉明域,与主 phash 同规格可入 MT-LSH 桶;条目负载
      ``{"sha256", "site", "kind": "mirror"}`` 注明来源。同 sha 已在
      镜像索引时不重复插入;惰性建实例(本批无 mirror 哈希则不落盘,
      开关关时不产生任何新文件);
    - **pyramid 只入 Registry 列、不入 LSH**(取舍):27 hex = 108bit
      三层串联,查询语义是"三层汉明取最小"(pyramid_distance),不是
      定长 64bit 汉明球——MT-LSH 的桶键 / 探针 / 距离过滤均按 64bit
      设计,装 108bit 变长哈希会破坏桶边界与召回论证;故 pyramid 走
      ``PhashRegistry.find_similar(hash_kind="pyramid")`` 全表精确查
      (复核台场景,本地库规模可接受)。
    """
    phash_mod = _load("netsentinel.intel.phash")
    phash_lsh = _load("netsentinel.intel.phash_lsh")
    multihash = _multihash_enabled(cfg)
    registry = phash_mod.PhashRegistry(cfg.phash_db)
    index = phash_lsh.MultiTableLSH(db_path=_mt_lsh_db_path(cfg))
    mirror_index: Any | None = None  # 惰性:首条 mirror 哈希在席才建实例
    mirror_registered = 0
    registered = 0
    try:
        for item in fingerprints:
            sha, fingerprint, mirror, pyramid = _fp_parts(item)
            if not sha:
                continue
            if multihash:
                registry.register(
                    sha,
                    fingerprint,
                    site_url,
                    mirror_hash=mirror or None,
                    pyramid_hash=pyramid or None,
                )
            else:
                registry.register(sha, fingerprint, site_url)
            registered += 1
            if not any(
                _hit_field(payload, "sha256") == sha
                for payload in index.query(fingerprint, 0)
            ):
                index.insert(fingerprint, {"sha256": sha, "site": site_url})
            if multihash and mirror:
                if mirror_index is None:
                    mirror_index = phash_lsh.MultiTableLSH(
                        db_path=_mirror_lsh_db_path(cfg), name="mirror"
                    )
                if not any(
                    _hit_field(payload, "sha256") == sha
                    for payload in mirror_index.query(mirror, 0)
                ):
                    mirror_index.insert(
                        mirror,
                        {"sha256": sha, "site": site_url, "kind": "mirror"},
                    )
                    mirror_registered += 1
        if registered:
            index.flush()
        if mirror_index is not None and mirror_registered:
            mirror_index.flush()
    finally:
        registry.close()
        index.close()
        if mirror_index is not None:
            mirror_index.close()
    if registered:
        telemetry.inc("graph_wire.register", registered)
    if mirror_registered:
        telemetry.inc("graph_wire.mirror_registered", mirror_registered)
    logger.debug(
        "phash 登记完成:%s(本批登记 %d 条,哈希库 + 持久多表 LSH 双写;"
        "多哈希 %s)",
        site_url,
        registered,
        f"开:mirror 索引新增 {mirror_registered} 条" if multihash else "关",
    )
    return registered


#: 注册库全量重灌的缺省规模上限(条,A214 查询步优化):库内登记数
#: **超过**该值时跳过每批 O(N) 重灌、只查持久多表 LSH(fastpath);
#: 未超过则保持"注册库重灌 ∪ 多表 LSH"双源并查(slowpath,小库行为
#: 与 A194/A204 完全一致)。经 ``cfg.graph_wire_phash_rebuild_limit``
#: 附加属性覆盖(getattr 防御式读取,contracts 冻结不改)。
_PHASH_REBUILD_LIMIT_DEFAULT = 5000


def _phash_rebuild_limit(cfg: Config) -> int:
    """读注册库重灌规模上限;缺失/非法(负数/不可解析)回缺省 5000。"""
    try:
        value = int(
            getattr(cfg, "graph_wire_phash_rebuild_limit", _PHASH_REBUILD_LIMIT_DEFAULT)
        )
    except (TypeError, ValueError):
        return _PHASH_REBUILD_LIMIT_DEFAULT
    return value if value >= 0 else _PHASH_REBUILD_LIMIT_DEFAULT


def _wire_phash_near(
    graph: Any, site_url: str, cfg: Config, fingerprints: list[tuple[str, ...]]
) -> int:
    """LSH 近邻查询 → 跨站 ``phash_near`` 边;返回成功写入的边数。

    - 检索(按命中 sha 去重后计权,多源同图只算一次),A214 起按库规模
      自适应选路(telemetry 各计 ``graph_wire.wire.slowpath`` /
      ``graph_wire.wire.fastpath``):
      **slowpath(小库,缺省阈值 5000 内,行为与 A194/A204 一致)**——
      双源并查:① A127 ``phash_lsh.LSHIndex`` 经 ``build_from(
      PhashRegistry)`` 全量重灌(权威哈希库,覆盖历史登记与第三方写入,
      代价是每批 O(N) 重灌)∪ ② A195 ``MultiTableLSH``(持久库懒加载,
      A204 登记步增量维护;同 sha 换站重传时保留**首见站点**的索引条目);
      **fastpath(库规模超阈值,``cfg.graph_wire_phash_rebuild_limit`` 可配)
      **——跳过 ① 的每批 O(N) 重灌,只查 ② 持久多表 LSH;两源对
      登记闭环(:func:`_register_phash` 双写)覆盖的数据召回等价,
      唯一差别是注册库被第三方**绕过登记步**直接写入的指纹不再
      每批重灌可见(权衡:大规模库下省去全量重灌);
    - 建边:命中的**其他站点**与本站逐对写 ``phash_near`` 边,权重 =
      该站近重复命中次数 ÷ 本批成功哈希图片数(近重复占比,0~1)。

    A229 mirror 源(``cfg.graph_wire_multihash`` 开启;缺省关 = 上方
    单哈希口径逐字节不变):③ 独立持久 ``MultiTableLSH(name="mirror",
    <phash_db>.mtlsh.mirror)`` 按 mirror_hash 查询,距离 ≤
    ``cfg.graph_wire_mirror_distance``(缺省 12,A219 建议距;翻转图
    距离恒 0)的命中并入 mirror 通道——与 ①② 同享"按命中 sha 去重、
    同站过滤"口径,phash 已命中的同 sha 不重复计权。镜像索引自身
    持久懒加载,**与 fastpath/slowpath 正交**:fastpath 只跳过 ① 的
    注册库重灌,③ 照常查询。候选语义(红线 48):多哈希命中 = 候选
    生成而非判定——mirror 丢弃"原图是否翻转"信息(互为翻转的异图
    不可分),命中只生成候选关联边,须经既有 phash256 比对与人工
    复核确认。A244 起镜像命中落**独立边种类 ``mirror_near``**(A229
    时代挂 ``phash_near``、图上无法区分来源):phash 源(①②)命中
    建 ``phash_near`` 边、mirror 源(③)命中建 ``mirror_near`` 边,
    两通道分开计权(分子各算各的命中次数,分母同为本批成功哈希图数,
    权重语义均为近重复占比 0~1)、分开落边(同站对可同时持有两种边,
    各自独立证据);镜像索引负载的 ``kind="mirror"`` 与 telemetry 计数
    ``graph_wire.mirror_hits``(命中)/ ``graph_wire.mirror_edge``(成边)
    注明来源。返回值 = 近重复家族边总数(phash_near + mirror_near,
    与调用方 counts["phash_near_edges"] 的既有口径衔接)。
    """
    try:
        phash_mod = _load("netsentinel.intel.phash")
        phash_lsh = _load("netsentinel.intel.phash_lsh")
        canonical = _load("netsentinel.intel.canonical")

        multihash = _multihash_enabled(cfg)
        max_distance = int(getattr(cfg, "graph_wire_phash_distance", 8))
        # ① 注册库全量重灌的单表 LSH:仅当库规模未超阈值(slowpath);
        #    超阈值跳过重灌(fastpath),免每批 O(N) 全表灌入
        rebuild_limit = _phash_rebuild_limit(cfg)
        lsh = None
        registry = phash_mod.PhashRegistry(cfg.phash_db)
        try:
            registry_total = int(registry.stats().get("total", 0))
            if registry_total <= rebuild_limit:
                lsh = phash_lsh.LSHIndex(
                    bands=int(getattr(cfg, "phash_lsh_bands", 4))
                )
                lsh.build_from(registry)
                telemetry.inc("graph_wire.wire.slowpath")
            else:
                telemetry.inc("graph_wire.wire.fastpath")
                logger.debug(
                    "注册库规模 %d 超过重灌上限 %d,跳过全量重灌只走持久"
                    "多表 LSH(fastpath)",
                    registry_total,
                    rebuild_limit,
                )
        finally:
            registry.close()

        # ② 持久多表 multi-probe LSH(懒加载历史批;A204 登记闭环的近路)
        # ③ A229 mirror 独立持久多表 LSH(开关开且本批有 mirror 哈希才建)
        pair_hits: dict[str, int] = {}  # phash 源(①②)命中计数
        mirror_pair_hits: dict[str, int] = {}  # mirror 源(③)命中计数(A244 分通道)
        mirror_hits = 0
        index = phash_lsh.MultiTableLSH(db_path=_mt_lsh_db_path(cfg))
        mirror_index: Any | None = None
        try:
            if multihash and any(_fp_parts(item)[2] for item in fingerprints):
                mirror_index = phash_lsh.MultiTableLSH(
                    db_path=_mirror_lsh_db_path(cfg), name="mirror"
                )
            mirror_distance = _mirror_distance(cfg)
            for item in fingerprints:
                _sha, fingerprint, mirror, _pyramid = _fp_parts(item)
                seen_shas: set[str] = set()
                sources = (
                    (
                        lsh.query(fingerprint, max_distance),
                        index.query(fingerprint, max_distance),
                    )
                    if lsh is not None
                    else (index.query(fingerprint, max_distance),)
                )
                for hits in sources:
                    for hit in hits:
                        hit_sha = _hit_field(hit, "sha256")
                        if hit_sha:
                            if hit_sha in seen_shas:
                                continue  # 多源同图:去重,只计一次
                            seen_shas.add(hit_sha)
                        other = _hit_field(hit, "site")
                        if not other or canonical.is_same_site(other, site_url):
                            continue
                        pair_hits[other] = pair_hits.get(other, 0) + 1
                if mirror_index is not None and mirror:
                    for hit in mirror_index.query(mirror, mirror_distance):
                        hit_sha = _hit_field(hit, "sha256")
                        if hit_sha:
                            if hit_sha in seen_shas:
                                continue  # phash 已命中同图:去重,只计一次
                            seen_shas.add(hit_sha)
                        other = _hit_field(hit, "site")
                        if not other or canonical.is_same_site(other, site_url):
                            continue
                        mirror_pair_hits[other] = mirror_pair_hits.get(other, 0) + 1
                        mirror_hits += 1  # 来源计数(边种类已可区分,A244)
        finally:
            index.close()
            if mirror_index is not None:
                mirror_index.close()

        if mirror_hits:
            telemetry.inc("graph_wire.mirror_hits", mirror_hits)
        written = 0
        phash_written = 0
        mirror_written = 0
        denominator = max(1, len(fingerprints))
        for other in sorted(pair_hits):
            weight = min(1.0, pair_hits[other] / denominator)
            if _graph_add_phash_near(graph, site_url, other, weight):
                written += 1
                phash_written += 1
        for other in sorted(mirror_pair_hits):
            weight = min(1.0, mirror_pair_hits[other] / denominator)
            if _graph_add_phash_near(
                graph, site_url, other, weight, kind=_EDGE_MIRROR_NEAR
            ):
                written += 1
                mirror_written += 1
        if phash_written:
            telemetry.inc("graph_wire.phash_near_edges", phash_written)
        if mirror_written:
            telemetry.inc("graph_wire.mirror_edge", mirror_written)
        return written
    except Exception as exc:  # noqa: BLE001 - LSH 通电是增强项,失败降级
        telemetry.inc("graph_wire.phash_near_skipped")
        logger.warning(
            "phash LSH 近邻建边已跳过(扫描与图谱其余写入不受影响):%s", exc
        )
        return 0


def wire_graph_from_scan(
    site_url: str, cfg: Config, pages: Iterable[Any]
) -> dict[str, int]:
    """扫描批末通电:把本站素材指纹灌入 A46 图谱并折叠关联边。

    步骤(全部本地 IO,零外呼):

    1. ``add_site(site_url)``;
    2. 逐页 ``add_template(页面模板 simhash, site_url)``、逐图
       ``add_image(sha256, site_url)``(sha 为空的证据跳过);
    3. ``link_shared_images()`` / ``link_templates()`` 把多站点共享素材
       折叠成站点边(权重 = Jaccard;重跑幂等刷新);
    4. :func:`_phash_fingerprints` 逐图计算 pHash(每图只算一次,喂
       ⑤⑥ 两步;``cfg.graph_wire_multihash`` 开启时同批惰性增算
       mirror / pyramid 三哈希,见该函数 docstring);
    5. :func:`_register_phash` 登记步(A204 跨批次闭环):本批指纹写入
       ``PhashRegistry`` + 持久 ``MultiTableLSH``,后续批次的近邻查询
       即可命中本批指纹(此前扫描产出的 phash 只查不写,LSH 近邻仅覆盖
       历史库,跨批次不闭环);A229 多哈希开启时一次登记三列 + mirror
       独立 LSH 实例(``.mtlsh.mirror``),pyramid 只入 Registry 列;
    6. :func:`_wire_phash_near` LSH 近邻查询跨站近重复图建 ``phash_near`` 边
       (A214 起按注册库规模自适应:超阈值只查持久多表 LSH,小库双源不变;
       A229 多哈希开启时增 mirror 源:翻转图经镜像索引命中建
       **``mirror_near`` 独立候选边**(A244 起与 phash_near 分通道)——
       候选语义见该函数 docstring,红线 48;消费侧 resolve_gangs 对该
       来源降权且默认不直接并团)。

    :return: 操作计数 ``{"sites","images","templates","shared_image_edges",
             "shared_template_edges","registered","phash_near_edges"}``
             (telemetry 同步计数;``registered`` 为本批登记进哈希库的
             指纹条数;``phash_near_edges`` 为近重复**家族**边总数 =
             phash_near + mirror_near 两通道之和,telemetry 里两通道
             分别计 ``graph_wire.phash_near_edges`` /
             ``graph_wire.mirror_edge``)。
    :raises RuntimeError: 图谱模块未就位(由 orchestrator 捕获降级);
                         单步写图失败在步内降级计数,不上抛。
    """
    pages = list(pages)
    counts = {
        "sites": 0,
        "images": 0,
        "templates": 0,
        "shared_image_edges": 0,
        "shared_template_edges": 0,
        "registered": 0,
        "phash_near_edges": 0,
    }
    graph_cls = _load("netsentinel.intel.graph").EvidenceGraph
    graph = graph_cls(cfg.graph_db)
    try:
        graph.add_site(site_url)
        counts["sites"] = 1
        seen_shas: set[str] = set()
        seen_templates: set[str] = set()
        for page in pages:
            template = _page_template_key(page)
            if template and template not in seen_templates:
                seen_templates.add(template)
                graph.add_template(template, site_url)
                counts["templates"] += 1
            for img in getattr(page, "image_evidences", None) or []:
                sha = str(getattr(img, "sha256", "") or "").strip().lower()
                if sha and sha not in seen_shas:
                    seen_shas.add(sha)
                    graph.add_image(sha, site_url)
                    counts["images"] += 1
        counts["shared_image_edges"] = int(graph.link_shared_images())
        counts["shared_template_edges"] = int(graph.link_templates())
        # ④ 本批 pHash 指纹收集(单图一次;phash 内核缺失 → 登记/建边整体降级;
        #    multihash 开启时同批惰性增算 mirror/pyramid,A229)
        fingerprints: list[tuple[str, ...]] = []
        try:
            fingerprints, phash_skipped = _phash_fingerprints(
                pages, multihash=_multihash_enabled(cfg)
            )
            if phash_skipped:
                telemetry.inc("graph_wire.phash_skipped", phash_skipped)
            if fingerprints:
                telemetry.inc("graph_wire.phash_hashed", len(fingerprints))
        except Exception as exc:  # noqa: BLE001 - 指纹是增强项,失败不影响写图
            telemetry.inc("graph_wire.phash_near_skipped")
            logger.warning(
                "phash 指纹收集已跳过(登记与近邻建边一并跳过,不影响图谱"
                "其余写入):%s",
                exc,
            )
            fingerprints = []
        # ⑤ 登记步:本批指纹入库,后续批次近邻查询可命中(跨批次闭环)
        if fingerprints:
            try:
                counts["registered"] = _register_phash(site_url, cfg, fingerprints)
            except Exception as exc:  # noqa: BLE001 - 登记失败退回历史库查询
                telemetry.inc("graph_wire.register_skipped")
                logger.warning(
                    "phash 登记已跳过(近邻查询退回历史库覆盖,不影响本批"
                    "扫描):%s",
                    exc,
                )
            # ⑥ 近邻查询建边(登记失败也照常查询:注册库重灌仍覆盖历史)
            counts["phash_near_edges"] = _wire_phash_near(
                graph, site_url, cfg, fingerprints
            )
    finally:
        graph.close()
    telemetry.inc("graph_wire.scans")
    telemetry.inc("graph_wire.images", counts["images"])
    telemetry.inc("graph_wire.templates", counts["templates"])
    telemetry.inc("graph_wire.shared_image_edges", counts["shared_image_edges"])
    telemetry.inc("graph_wire.shared_template_edges", counts["shared_template_edges"])
    logger.info(
        "站群图谱通电完成:%s(图片 %d / 模板 %d / shared_image %d / "
        "shared_template %d / 登记 %d / phash_near %d)",
        site_url,
        counts["images"],
        counts["templates"],
        counts["shared_image_edges"],
        counts["shared_template_edges"],
        counts["registered"],
        counts["phash_near_edges"],
    )
    return counts


# ---------------------------------------------------------------------------
# A194 团伙判定增强:纯连通并团(默认,与现状一致) vs 加权社区检测
# ---------------------------------------------------------------------------
def resolve_gangs(
    edges: Iterable[tuple[str, str, str, float]],
    *,
    mode: str = "connectivity",
    weight_threshold: float = 0.0,
    template_weight_factor: float = 1.0,
    mirror_weight_factor: float = 0.5,
    mirror_in_connectivity: bool = False,
    resolution: float = 1.0,
) -> list[list[str]]:
    """把带权关联边解析为团伙分组(输出确定:组内按 id 排序、组按最小成员排序)。

    :param edges: ``(src, dst, kind, weight)`` 四元组序列(kind 如
        ``shared_image`` / ``phash_near`` / ``shared_template`` / ``redirect``
        / ``mirror_near``;weight 为 A46 的 Jaccard / 近重复占比权重);
    :param mode: ``connectivity``(默认)——并查集纯连通并团,**除
        ``mirror_near`` 外**任何边无论权重都合并(与 group_linker 现状
        语义一致;A244 唯一例外见 ``mirror_in_connectivity``);
        ``community``——加权社区检测:``shared_template`` 边乘
        ``template_weight_factor``、``mirror_near`` 边乘
        ``mirror_weight_factor`` 降权后,有效权重 < ``weight_threshold``
        的边剔除,余图经 ``louvain_communities`` 切社区(建站工具弱边
        与镜像候选边不再一条误并整团);
    :param weight_threshold: community 模式的边权重下限(对降权后的有效
        权重判定;connectivity 模式忽略);
    :param template_weight_factor: shared_template 边降权系数(默认 1.0 =
        仅按 A46 自带的 Jaccard 弱权;connectivity 模式忽略);
    :param mirror_weight_factor: mirror_near 边降权系数(默认 0.5,对齐
        shared_template 降权先例 ``gang_template_weight_factor`` 的机制,
        但缺省即半权——A229 交付报告点名 mirror 命中使互为翻转的异图
        不可分,规范形固有取舍,消费侧默认对镜像来源降权;仅 community
        模式生效);
    :param mirror_in_connectivity: 是否允许 ``mirror_near`` 边在
        connectivity 模式下直接并团(默认 **False**——候选语义红线 48
        的强化:mirror 规范形丢弃翻转方向信息,镜像命中只是候选关联
        而非判定,默认须经 phash256 复核确认(或社区检测降权路径)后
        才并团;显式 True = 运营者接受镜像候选直接并团的召回取舍)。
        被排除的镜像边端点仍以单点组出现在输出里(可见但不并);
    :param resolution: Louvain 分辨率(community 模式;>1 偏好更小社区)。
    :return: ``[["site:a", "site:b"], ["site:c"], …]``——团伙列表,组内
        节点升序、组间按每组最小节点升序(确定性);空输入 → []。
    :raises ValueError: mode 非法或参数越界(louvain_communities 校验)。
    """
    raw_edges = [
        (str(a), str(b), str(kind), float(weight)) for a, b, kind, weight in edges
    ]
    # 节点全集取自全部边的非空端点(单端点空值的边仍贡献其有效端点,
    # 保证该端点以单点组出现在输出里,不因边被过滤而消失)。
    nodes = {endpoint for a, b, _k, _w in raw_edges for endpoint in (a, b) if endpoint}
    edge_list = [
        (a, b, kind, weight)
        for a, b, kind, weight in raw_edges
        if a and b and a != b  # 空端点 / 自环不产生合并边
    ]
    mode = str(mode or "connectivity").strip().lower()
    if mode == "connectivity":
        kernel = _load("netsentinel.intel.graph_kernel").UnionFindKernel()
        for node in sorted(nodes):  # 登记全部端点(含无有效边的孤立点)
            kernel.add(node)
        for src, dst, kind, _weight in edge_list:
            if kind == _EDGE_MIRROR_NEAR and not mirror_in_connectivity:
                continue  # 红线 48 强化:镜像候选边默认不直接并团(A244)
            kernel.union(src, dst)
        components = kernel.components()
        groups = [sorted(members) for members in components.values()]
    elif mode == "community":
        graph_kernel = _load("netsentinel.intel.graph_kernel")
        adjacency: dict[str, dict[str, float]] = {node: {} for node in nodes}
        weight_threshold = float(weight_threshold)
        template_weight_factor = float(template_weight_factor)
        mirror_weight_factor = float(mirror_weight_factor)
        for src, dst, kind, weight in edge_list:
            if kind == _EDGE_SHARED_TEMPLATE:
                effective = weight * template_weight_factor
            elif kind == _EDGE_MIRROR_NEAR:
                effective = weight * mirror_weight_factor
            else:
                effective = weight
            if effective < weight_threshold:
                continue
            adjacency[src][dst] = adjacency[src].get(dst, 0.0) + effective
            adjacency[dst][src] = adjacency[dst].get(src, 0.0) + effective
        partition = graph_kernel.louvain_communities(
            adjacency, resolution=resolution
        )
        buckets: dict[int, list[str]] = {}
        for node, label in partition.items():
            buckets.setdefault(int(label), []).append(node)
        groups = [sorted(members) for members in buckets.values()]
    else:
        raise ValueError(
            f"团伙判定模式无效:{mode!r}(仅支持 connectivity / community)"
        )
    groups.sort(key=lambda members: members[0])
    return groups


def resolve_gangs_from_config(
    edges: Iterable[tuple[str, str, str, float]], cfg: Config
) -> list[list[str]]:
    """按 ``cfg`` 的团伙判定开关注解(全部缺省 = connectivity 现状口径)。

    开关为 Config 契约之外的附加实例属性(contracts 冻结不改,与
    ``ensemble_reliability_weights`` 同款注入方式),未设置时全部走缺省:

    - ``gang_mode``:``"connectivity"``(缺省)/ ``"community"``;
    - ``gang_weight_threshold``:边权重下限(缺省 0.0);
    - ``gang_template_weight_factor``:shared_template 降权系数(缺省 1.0);
    - ``gang_mirror_weight_factor``:mirror_near 降权系数(缺省 **0.5**,
      community 模式生效;A244 对齐 shared_template 降权先例的机制,
      缺省半权——镜像命中是候选关联,消费侧默认降权);
    - ``gang_mirror_in_connectivity``:connectivity 模式是否并
      mirror_near 边(缺省 **False**——红线 48 强化:镜像候选边默认
      不直接并团,显式 True 才纳入);
    - ``gang_resolution``:Louvain 分辨率(缺省 1.0)。
    """
    return resolve_gangs(
        edges,
        mode=str(getattr(cfg, "gang_mode", "connectivity") or "connectivity"),
        weight_threshold=float(getattr(cfg, "gang_weight_threshold", 0.0)),
        template_weight_factor=float(getattr(cfg, "gang_template_weight_factor", 1.0)),
        mirror_weight_factor=float(getattr(cfg, "gang_mirror_weight_factor", 0.5)),
        mirror_in_connectivity=bool(
            getattr(cfg, "gang_mirror_in_connectivity", False)
        ),
        resolution=float(getattr(cfg, "gang_resolution", 1.0)),
    )
