"""NetSentinel(净网哨兵)并行图像分类调度器(vision.parallel_classify,A166)。

把一批 :class:`ImageEvidence` 分发给并发执行器逐张 ``classifier.classify``,
**保序**返回 :class:`ImageScore` 列表;单条失败逐条容错为 0 分 + 中文
``scores={"error": ...}``,绝不影响其余条目、不中断整批。

两种形态(契约 §2 A166 行):

- **线程模式(默认,``use_processes=False``)**:视觉模型调用属 IO 等待型
  (本地回环网关 / VLM API),线程池即可压榨等待时间。执行器三级解析:
  ①调用方注入 ``executor``(用完**不关闭**,归属调用方);
  ②缺省惰性复用 A164 ``ops.concurrency.thread_pool(cfg, label="classify")``
  (兄弟模块并行开发期可能缺席,缺席静默跳过);
  ③兜底自建 ``ThreadPoolExecutor(max_workers=io_workers)``,档位公式与
  契约 §1 / A163 / A164 逐条一致(low=cores//4 / mid=cores//2 /
  high=cores-reserve)。
- **进程模式(``use_processes=True``)**:纯本地 CPU 计算(如 ``skin``
  启发式内核)可走进程池,**注入的 ``executor`` 在该模式下被忽略**。
  **红线 35:绝不 pickle 分类器实例/连接**——主进程只把
  ``(分类器名, 图片路径, URL)`` 纯字符串三元组提交给模块级纯函数
  :func:`_proc_classify`,子进程内按名经 ``classifier_base.get_classifier``
  惰性构造一次性实例,返回普通 dict;主进程按位对齐**重组**
  :class:`ImageScore`(``image`` 复用原始 evidence 对象,保序)。子进程
  内任何异常就地转 ``{"error": ...}`` dict,不让异常跨进程边界传播。
  ``ProcessPoolExecutor(max_workers=cpu_workers)`` 构造失败不静默降级,
  统一转中文 :class:`RuntimeError`(原始异常保留为 ``__cause__``)。

安全红线:

35. 压榨边界——本模块只加速**本地计算与本地回环 IO**(并行分类);对外
    抓取的礼貌间隔(``fetch_delay_s``)、引擎限速、举报频控一概不在本
    模块触碰;进程池仅经模块级纯函数传递 ``(name, path, url)``,
    不 pickle 分类器实例。
37. 资源治理——缺省执行器 workers 一律 ≤ 物理核数(线程池经 §1 公式
    天然 ≤ cores;进程池 ``min(tier_workers, cores)`` 双重钳制;注入显式
    执行器不受限,归属调用方);单批总量上限 :data:`MAX_BATCH`、单任务
    取结果超时 :data:`PROC_RESULT_TIMEOUT_S`;缺省线程池随 ``with``
    关闭、缺省进程池 ``finally`` 关闭并不留排队任务,失控不养孤儿。

遥测:``classify.parallel`` 计时整批;``classify.errors`` 按失败条数
逐条累加(线程异常 / 子进程 error dict / 取结果失败三处同源计数)。

测试约定:``tests/test_parallel_classify.py`` 全离线(核数 monkeypatch
固定;进程路径用 fake ``ProcessPoolExecutor`` 同步执行 + 一例真 spawn
可用性测试,不可用环境 skip)。
"""
from __future__ import annotations

import importlib
import logging
import os
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, ImageScore
from netsentinel.vision.classifier_base import NsfwClassifier

__all__ = ["classify_parallel", "MAX_BATCH", "PROC_RESULT_TIMEOUT_S"]

logger = logging.getLogger(__name__)

#: 单批证据总量上限(红线 37「所有并行任务有总量与超时上限」);
#: 超出抛中文 ValueError,调用方应自行分片。
MAX_BATCH = 4096

#: 进程模式单条任务取结果超时(秒);超时按该条失败容错,不拖垮整批。
PROC_RESULT_TIMEOUT_S = 300.0

#: 核数探测兜底值(契约 §1:``os.cpu_count()`` 返回 None 时按 2)
_FALLBACK_CORES = 2

#: 进程模式子进程**二级惰性导入表**:``get_classifier`` 未命中时,先导入
#: 对应自注册模块再重试一次。背景:``classifier_base`` 的惰性导入映射
#: (stub/nudenet/clip/glm)随基座冻结,后落地的本地内核(如 V7 ``skin``
#: 启发式,导入即自注册)不在其中——子进程是全新解释器,注册表为空,
#: 若无此表 "skin" 永远无法跨进程按名构造。仅导入模块(不传实例,
#: 红线 35);导入失败仍走原 ValueError → error dict。
_PROC_SECOND_CHANCE_MODULES: dict[str, str] = {
    "skin": "netsentinel.vision.heuristic_kernel",
}


# ---------------------------------------------------------------------------
# 档位公式与 workers 解析(惰性优先 A164,缺席内置兜底——同 §1)
# ---------------------------------------------------------------------------
def _ops_concurrency() -> Any:
    """惰性取 A164 ``netsentinel.ops.concurrency``;缺席/不可用返回 None。

    并行开发期兄弟模块可能未就位或部分写入(非 ImportError 一并按缺席
    处理,只记 debug 日志),此时走本模块内置兜底,结论与契约一致。
    """
    try:
        from netsentinel.ops import concurrency as _mod  # 惰性:A164 并行开发
    except Exception as exc:  # noqa: BLE001 - 并行开发期任何导入问题都按缺席
        logger.debug("ops.concurrency 不可用,按内置公式兜底:%s", exc)
        return None
    return _mod if hasattr(_mod, "thread_pool") else None


def _tier_of(cfg: Config) -> str:
    """读 ``cfg.concurrency_tier``;读不到按缺省 "mid"。"""
    return str(getattr(cfg, "concurrency_tier", "mid") or "mid")


def _reserve_of(cfg: Config) -> int:
    """读 ``cfg.cpu_reserve``(高档保留核数);读不到/非法按缺省 1。"""
    try:
        return max(0, int(getattr(cfg, "cpu_reserve", 1)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1


def _fallback_tier_workers(
    tier: str, *, reserve: int = 1, cores: int | None = None
) -> int:
    """契约 §1 内置兜底公式(与 A163 ``tier_workers`` / A164 同款):

    - low:max(1, N//4) —— 后台/省电;
    - mid:max(1, N//2) —— 默认;
    - high:max(1, N - reserve) —— 极限压榨(reserve=0 即全核);
    - 非法档位:中文 ValueError(不依赖兄弟模块报错文案)。
    """
    n = int(cores) if cores and int(cores) > 0 else (os.cpu_count() or _FALLBACK_CORES)
    try:
        r = max(0, int(reserve))
    except (TypeError, ValueError):
        r = 1
    tier = str(tier).strip().lower()
    if tier == "low":
        return max(1, n // 4)
    if tier == "mid":
        return max(1, n // 2)
    if tier == "high":
        return max(1, n - r)
    raise ValueError(f"未知并发档位:'{tier}'(合法值:low / mid / high)")


def _io_workers(cfg: Config) -> int:
    """线程池 workers:优先 A164 ``io_workers(cfg)`` 委托,缺席走内置公式。"""
    mod = _ops_concurrency()
    if mod is not None and hasattr(mod, "io_workers"):
        try:
            return max(1, int(mod.io_workers(cfg)))
        except Exception as exc:  # noqa: BLE001 - 兄弟异常仅降级到内置公式
            logger.warning("ops.concurrency.io_workers 调用失败,按内置公式兜底:%s", exc)
    return _fallback_tier_workers(_tier_of(cfg), reserve=_reserve_of(cfg))


def _cpu_workers(cfg: Config) -> int:
    """进程池 workers:``min(tier_workers, cores)``——红线 37 双重钳制。

    优先 A164 ``cpu_workers``(其内部已钳),本模块仍以当前核数再钳一次,
    保证任何兄弟态下缺省进程池都不超物理核。
    """
    cores = os.cpu_count() or _FALLBACK_CORES
    mod = _ops_concurrency()
    workers: int | None = None
    if mod is not None and hasattr(mod, "cpu_workers"):
        try:
            workers = int(mod.cpu_workers(cfg))
        except Exception as exc:  # noqa: BLE001 - 兄弟异常仅降级到内置公式
            logger.warning("ops.concurrency.cpu_workers 调用失败,按内置公式兜底:%s", exc)
            workers = None
    if workers is None:
        workers = _fallback_tier_workers(_tier_of(cfg), reserve=_reserve_of(cfg))
    return max(1, min(workers, cores))


def _model_name(classifier: Any, cfg: Config) -> str:
    """分类器显示名:``classifier.name`` 优先,缺席回退 ``cfg.classifier``。"""
    name = str(getattr(classifier, "name", "") or "").strip()
    if name:
        return name
    return str(getattr(cfg, "classifier", "") or "unknown") or "unknown"


# ---------------------------------------------------------------------------
# 进程池工作纯函数(红线 35:入参/出参均为可 pickle 的普通数据)
# ---------------------------------------------------------------------------
def _proc_classify(payload: tuple[str, str, str]) -> dict:
    """进程池工作函数(模块级纯函数):``payload = (分类器名, 路径, URL)``。

    子进程内按名经 ``classifier_base.get_classifier`` **惰性构造**一次性
    分类器实例(用缺省 :class:`Config`;跨进程不传 cfg 定制,需定制的
    IO 型模型请走线程模式注入实例);``get_classifier`` 未命中且名称在
    :data:`_PROC_SECOND_CHANCE_MODULES` 二级惰性表中时(如 "skin"),
    先导入自注册模块再重试一次。对单个路径打分并返回普通 dict;任何
    异常就地转 ``{"error": ...}``,不让异常跨进程边界传播。
    """
    name, path, url = payload
    try:
        from netsentinel.contracts import Config, ImageEvidence
        from netsentinel.vision.classifier_base import get_classifier  # 惰性构造

        try:
            clf = get_classifier(name, Config())
        except ValueError:
            mod_path = _PROC_SECOND_CHANCE_MODULES.get(name)
            if mod_path is None:
                raise  # 非已知本地内核:保留原 ValueError(转 error dict)
            importlib.import_module(mod_path)  # 导入即自注册,再试一次
            clf = get_classifier(name, Config())
        score = clf.classify(ImageEvidence(path=path, url=url, source_page=""))
        return {
            "model": str(score.model),
            "nsfw_prob": float(score.nsfw_prob),
            "scores": dict(score.scores or {}),
            "path": path,
            "url": url,
        }
    except Exception as exc:  # noqa: BLE001 - 子进程异常一律转 error dict
        return {"error": f"子进程分类失败:{exc}", "path": path, "url": url}


def _score_from_proc_dict(
    ev: ImageEvidence, data: Any, fallback_name: str
) -> ImageScore:
    """把 :func:`_proc_classify` 返回的 dict 重组为 :class:`ImageScore`。

    ``image`` 复用**原始** evidence 对象(主进程持有完整 source_page/
    sha256 等字段;子进程只回传打分数值);error dict / 非法返回值均
    容错为 0 分 + 中文 error,并累计 ``classify.errors``。
    """
    if not isinstance(data, dict):
        telemetry.inc("classify.errors")
        return ImageScore(
            image=ev,
            model=fallback_name,
            nsfw_prob=0.0,
            scores={"error": f"子进程返回非法结果:{type(data).__name__}"},
        )
    model = str(data.get("model") or fallback_name)
    if "error" in data:
        telemetry.inc("classify.errors")
        return ImageScore(
            image=ev,
            model=model,
            nsfw_prob=0.0,
            scores={"error": str(data["error"])[:300]},
        )
    try:
        prob = float(data.get("nsfw_prob", 0.0))
    except (TypeError, ValueError):
        prob = 0.0
    raw_scores = data.get("scores")
    scores = dict(raw_scores) if isinstance(raw_scores, dict) else {}
    return ImageScore(
        image=ev,
        model=model,
        scores=scores,
        nsfw_prob=max(0.0, min(1.0, prob)),  # 归一化概率域防御性钳制
    )


# ---------------------------------------------------------------------------
# 两种执行形态
# ---------------------------------------------------------------------------
def _collect_ordered(
    evidences: list[ImageEvidence], futures: list, model_name: str
) -> list[ImageScore]:
    """按提交顺序收集 future 结果(保序);取结果异常逐条容错。"""
    results: list[ImageScore] = []
    for ev, fut in zip(evidences, futures):
        try:
            results.append(fut.result())
        except Exception as exc:  # noqa: BLE001 - 执行器极端故障兜底
            telemetry.inc("classify.errors")
            results.append(
                ImageScore(
                    image=ev,
                    model=model_name,
                    nsfw_prob=0.0,
                    scores={"error": f"取分类结果失败:{exc}"},
                )
            )
    return results


def _classify_threads(
    classifier: NsfwClassifier,
    evidences: list[ImageEvidence],
    cfg: Config,
    executor: Executor | None,
) -> list[ImageScore]:
    """线程模式:注入 executor 或缺省线程池逐张 classify,保序 + 容错。"""
    model_name = _model_name(classifier, cfg)

    def _one(ev: ImageEvidence) -> ImageScore:
        try:
            return classifier.classify(ev)
        except Exception as exc:  # noqa: BLE001 - 单条失败只容错该条
            telemetry.inc("classify.errors")
            return ImageScore(
                image=ev,
                model=model_name,
                nsfw_prob=0.0,
                scores={"error": str(f"分类失败:{exc}")[:300]},
            )

    if executor is not None:
        # 注入执行器:直接使用,不关闭(归属调用方)。
        futures = [executor.submit(_one, ev) for ev in evidences]
        return _collect_ordered(evidences, futures, model_name)

    mod = _ops_concurrency()
    if mod is not None:
        # 缺省 ①:惰性复用 A164 thread_pool(上下文管理器,退出必关闭)。
        with mod.thread_pool(cfg, label="classify") as pool:
            futures = [pool.submit(_one, ev) for ev in evidences]
            return _collect_ordered(evidences, futures, model_name)
    # 缺省 ②:A164 缺席兜底自建(io_workers 内置公式;with 关闭兜底)。
    with ThreadPoolExecutor(
        max_workers=_io_workers(cfg), thread_name_prefix="classify"
    ) as pool:
        futures = [pool.submit(_one, ev) for ev in evidences]
        return _collect_ordered(evidences, futures, model_name)


def _classify_processes(
    classifier: NsfwClassifier,
    evidences: list[ImageEvidence],
    cfg: Config,
) -> list[ImageScore]:
    """进程模式:``(name, path, url)`` 三元组提交纯函数,子进程按名构造。

    红线 35:``classifier`` 实例**绝不经 submit 传递**(不 pickle);主进程
    只传纯字符串 payload,按位对齐重组 ``ImageScore``。
    """
    name = _model_name(classifier, cfg)
    payloads: list[tuple[str, str, str]] = [
        (name, ev.path, ev.url) for ev in evidences
    ]
    workers = _cpu_workers(cfg)
    try:
        pool = ProcessPoolExecutor(max_workers=workers)
    except Exception as exc:  # noqa: BLE001 - 统一转中文 RuntimeError(不吞)
        raise RuntimeError(
            f"进程池 'classify' 创建失败(max_workers={workers}):Windows spawn"
            " 要求程序入口位于 if __name__ == '__main__': 守卫内,或由测试显式"
            f"注入 mp_context;请检查入口守卫。原始错误:{exc}"
        ) from exc
    logger.debug("并行分类进程池创建:max_workers=%d items=%d", workers, len(payloads))
    results: list[ImageScore] = []
    try:
        futures = [pool.submit(_proc_classify, payload) for payload in payloads]
        for ev, fut in zip(evidences, futures):
            try:
                data = fut.result(timeout=PROC_RESULT_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001 - 子进程崩溃/超时逐条容错
                telemetry.inc("classify.errors")
                results.append(
                    ImageScore(
                        image=ev,
                        model=name,
                        nsfw_prob=0.0,
                        scores={"error": str(f"子进程执行失败:{exc}")[:300]},
                    )
                )
                continue
            results.append(_score_from_proc_dict(ev, data, name))
    finally:
        # 红线 37:必关闭且清掉未起跑的排队任务,不留孤儿进程。
        pool.shutdown(wait=False, cancel_futures=True)
    return results


# ---------------------------------------------------------------------------
# 契约入口
# ---------------------------------------------------------------------------
def classify_parallel(
    classifier: NsfwClassifier,
    evidences: list[ImageEvidence],
    cfg: Config,
    *,
    executor: Executor | None = None,
    use_processes: bool = False,
) -> list[ImageScore]:
    """并行对一批证据图片分类,**保序**返回与输入等长的评分列表。

    - 空列表直接返回 ``[]``(不建池、不计时);
    - 线程模式(缺省):注入 ``executor``(不关闭)或缺省线程池
      (A164 ``thread_pool(cfg, label="classify")`` 惰性,缺席内置兜底),
      单条 ``classify`` 异常 → ``ImageScore(nsfw_prob=0.0,
      scores={"error": 中文})``,整批不受影响;
    - 进程模式(``use_processes=True``):**忽略注入的 ``executor``**,经
      模块级纯函数 :func:`_proc_classify` 在子进程按名构造分类器(红线
      35,不 pickle 实例),返回 dict 在主进程重组;进程池构造失败抛中文
      :class:`RuntimeError`;
    - 批量超过 :data:`MAX_BATCH` 抛中文 ValueError(红线 37 总量上限);
    - 遥测:整批 ``classify.parallel`` 计时;失败条数逐条累计
      ``classify.errors``。

    用法示例::

        scores = classify_parallel(clf, evidences, cfg)          # 线程模式
        scores = classify_parallel(clf, evidences, cfg, use_processes=True)
    """
    if not evidences:
        return []
    if len(evidences) > MAX_BATCH:
        raise ValueError(
            f"单批证据数 {len(evidences)} 超过上限 {MAX_BATCH},请分片后重试"
        )
    with telemetry.timer("classify.parallel"):
        if use_processes:
            return _classify_processes(classifier, evidences, cfg)
        return _classify_threads(classifier, evidences, cfg, executor)
