"""融合内核·提供方可靠性追踪(V7 · A126,CONTRACTS-V7.md §2)。

学习型融合的证据端:把**运营者本地反馈**(人工复核结论——某提供方当时报了
概率 p,事实是 outcome)按提供方累计为 Brier 分数::

    brier = mean((p - outcome)²)      # 0 完美校准,越大越不可靠

可靠性权重取 Brier 反比并平滑::

    w = 1 / (brier + ε)               # ε = 0.05(见 :data:`EPSILON`)

ε 保证完美提供方(brier=0)权重有限(20)、全错提供方(brier=1)权重不为零
(≈0.952),好坏权重比上限 21:1——既让报得准的平台有话语权,也不让任何
单一提供方被清零或垄断。权重在**样本充足**(n >= min_n,默认 5)的提供方
之间归一(Σ = 1);样本不足的提供方值为 ``None``,由调用方决定回退策略
(:func:`netsentinel.decision.fusion_reliable.fuse_reliable` 中回退等权 = 旧行为)。

数据面红线(V7 §0 红线 30,零外呼):

- 绝不联网取数、绝不调用 VLM 训练;只消费调用方传入的
  (provider, p, outcome) 三元组与本地 jsonl 持久化文件;
- 线程安全:单一 ``threading.Lock`` 同时保护内存累计与 jsonl 追加,
  多线程并发 record 不会丢样本、不会写坏行;
- 健壮性:损坏 / 缺字段 / 值非法的 jsonl 行跳过并记 debug 日志,
  单行脏数据不拖垮整个反馈环(与 intel.active_learn.ReviewFeedback 同款口径);
- ``jsonl_path`` 为空时纯内存(适合一次性分析 / 测试),非空时构造读回 +
  逐条追加,重复打开同一文件可跨批次累积。

用法::

    from netsentinel.decision.reliability import ReliabilityTracker

    tracker = ReliabilityTracker("data/reliability.jsonl")
    tracker.record("glm", 0.9, True)    # 复核确认:glm 报 0.9,确为色情
    tracker.record("stub", 0.9, False)  # 复核驳回:stub 报 0.9,实为正常图
    tracker.stats()     # {"glm": {"n": 1, "brier": 0.01}, ...}
    tracker.weights()   # 样本充足后 {"glm": 0.66, "stub": 0.34}(Σ=1)

本模块为新增文件(V7 红线 29),不改任何既有模块;离线回退 = 固定等权
(不建 tracker 即等权,行为与旧融合完全一致)。

=======================================================================
V12 增量(A216 贝叶斯分层可靠性跟踪,与 Brier 路径并存、opt-in)
=======================================================================

Brier 反比是**点估计**:n=5 的提供方靠 4/5 的幸运命中率即可拿到与
n=500 的稳定提供方同量级的话语权,且历史一视同仁——分布漂移(模型
换版、平台策略调整)发生后,几百条陈旧样本会拖住权重的调整步伐
(V10.4 波报告点名)。:class:`BayesianReliabilityTracker` 用在线贝叶斯
ensemble 的标准三板斧补上这两块短板:

1. **Beta 后验 on correctness**:每成员维护 (α, β) 共轭后验
   (均匀先验 Beta(1,1)),记录 (member, correct, ts) 事件流——
   α = 1 + Σ对, β = 1 + Σ错;
2. **指数遗忘**:obs(t) = exp(-ln2·Δt/half_life) 的事件权重计入
   等效 α/β(α += obs·1[correct]),等效样本数 n_eff = Σobs——
   推导:Beta-Bernoulli 对数似然 Σ obs_i·(y_i·lnθ + (1-y_i)·ln(1-θ))
   与伪计数为 obs_i 的加权 Beta-Bernoulli 同形,故"权重 0.5 的旧对错"
   数学上恰等价于"0.5 个伪样本",遗忘前后皆为共轭闭式;
3. **分层收缩**:成员后验均值向全局池后验收缩,强度 λ = κ/(κ+n_eff)
   (κ 默认 10,可配)——n_eff 大→成员数据主导,n_eff 小→向池均值退让;
   **冷启动**:n_eff < MIN_N 时 λ 直接取 1(完全收缩到全局),3/3 的
   完美新成员不再被幸运主导(V10.4 痛点)。

weights() 直接取收缩后后验均值 θ*(correctness 越大越好,天然单调,
无需 Brier 那样的反比失真映射)归一 Σ=1;weights_with_ci() 附
Beta 95% 置信区间(正则化不完全 Beta 逆:连分数 + 二分,纯 stdlib、
确定性迭代)。全模块零墙钟:as-of 时刻默认取事件流最大 ts,时钟由
调用方注入(测试用固定时钟)。

向后兼容红线:Brier 路径(:class:`ReliabilityTracker`、:data:`EPSILON`、
:data:`MIN_N`、:func:`kernel_selfcheck`)一字未动;贝叶斯路径仅经
``fuse_reliable(..., bayes_tracker=...)`` 显式 opt-in 注入。

=======================================================================
V14 增量(record 流可选 ``ts`` 字段:为贝叶斯遗忘提供真实时刻)
=======================================================================

:meth:`ReliabilityTracker.record` 增可选 ``ts`` 形参(Unix epoch 秒,与
``BayesianReliabilityTracker.half_life`` 的时间量纲同构——调用方喂 epoch
秒,半衰期就按秒算):

- **写侧**:``ts=None``(缺省)落盘行与历史**逐字节一致**(三键
  ``{"provider", "p", "outcome"}``,旧读取方零感知);显式传入时经
  :func:`_coerce_ts` 规整(拒布尔/非数字/NaN/inf,中文错误)后随行落盘,
  供贝叶斯重放路径按真实时刻做指数遗忘;
- **读侧**(:meth:`ReliabilityTracker._load`):``ts`` 键可选——旧行(无
  ts)照常读回,带 ts 的行 ts 仅供校验(Brier 累计不消费时刻),ts 存在
  但非法的行按坏行跳过(与 p/outcome 非法同口径,保证两条读取路径对
  同一行的一致裁决);
- **Brier 语义零变化**:ts 是纯元数据,stats/weights 的分子分母与收录前
  逐位一致;:class:`BayesianReliabilityTracker` 本身(A216 领地遗产)API
  一字未动,重放侧(orchestrator)从 ts 字段读时刻、缺 ts 行回退滴答。

=======================================================================
V15 增量(A242 贝叶斯事件流检查点化:O(n) 每查询重放 → 增量 + 时间校正)
=======================================================================

A216 交付报告风险项::class:`BayesianReliabilityTracker` 每次查询都全量
重放事件流(as_of 一动,每条事件的遗忘权重都变),单次 ``weights()`` 为
O(事件数)——长流(> 1e5)下每次集成加权都要付一次全量扫描。
:class:`CheckpointedBayesianReliabilityTracker`(子类,与基类并存、
接口同形)对标流式系统 checkpoint/快照惯例,把"重放求和"改为:

- **写入时增量折叠**:record 把事件压进待折叠缓冲,缓冲满
  ``fold_every``(缺省 256)条时整体折叠进检查点——各成员维护以锚点
  ``checkpoint_ts`` 为基准的等效 (α_eff, β_eff, n),摊还 record O(1);
- **读取时惰性重衰减**:查询 = 检查点 × 单一衰减因子
  exp(-ln2·(as_of - checkpoint_ts)/half_life) + 缓冲内新事件因果扫描,
  单次查询 O(成员数 + 缓冲),与流长无关。

指数遗忘核的泛函方程 f(a+b) = f(a)·f(b) 保证"检查点 + 校正"与全量重放
**恰等价**(精确性条件与推导见类 docstring;无遗忘路径连 exp 都不评估,
整数计数逐位相等)。快照 :meth:`save_snapshot` /
:meth:`CheckpointedBayesianReliabilityTracker.load_snapshot`
(JSON:``{checkpoint_ts, half_life, kappa, members:[{member, alpha, beta, n}]}``)
支持跨进程状态压缩恢复。基类与 Brier 路径零改动(并存红线)。
"""
from __future__ import annotations

import json
import logging
import math
import threading
from pathlib import Path
from typing import Any

from netsentinel import telemetry

__all__ = [
    "EPSILON",
    "MIN_N",
    "ReliabilityTracker",
    "kernel_selfcheck",
    "DEFAULT_HALF_LIFE",
    "DEFAULT_KAPPA",
    "PRIOR_A",
    "PRIOR_B",
    "BayesianReliabilityTracker",
    "bayesian_kernel_selfcheck",
    "DEFAULT_FOLD_EVERY",
    "CheckpointedBayesianReliabilityTracker",
]

logger = logging.getLogger(__name__)

#: 权重平滑项:w = 1/(brier + ε)。完美校准 → 20,全错 → ≈0.952,
#: 好坏权重比上限 21:1。
EPSILON: float = 0.05

#: weights() 默认最小样本数:低于该量的提供方统计上不可信,权重记 None。
MIN_N: int = 5

# ---------------------------------------------------------------------------
# V12 贝叶斯分层可靠性(A216)常量
# ---------------------------------------------------------------------------

#: 指数遗忘默认半衰期(时间单位与 ts 同构:调用方用天就是天,用秒就是秒)。
#: 7 = 一周量级的运营节奏;``None`` / ``inf`` = 关闭遗忘(每条事件权重恰为 1)。
DEFAULT_HALF_LIFE: float = 7.0

#: 分层收缩默认伪计数 κ:收缩强度 λ = κ/(κ+n_eff)。κ=10 表示"成员自身
#: 等效样本数攒到 10 之前,全局池均值占一半以上话语权"。
DEFAULT_KAPPA: float = 10.0

#: correctness 后验的均匀先验 Beta(1, 1)(无信息先验;手算基准:
#: 3 对 1 错 → α=4, β=2)。
PRIOR_A: float = 1.0
PRIOR_B: float = 1.0

#: ln 2(半衰期权重 exp(-ln2·Δt/half_life) 的衰减常数)。
_LN2: float = math.log(2.0)

#: jsonl 条目必填字段(持久化与内存统一为这三个键);V14 起行内可携带
#: 可选 ``ts`` 键(Unix epoch 秒,见 ReliabilityTracker.record)。
_ENTRY_KEYS: tuple[str, ...] = ("provider", "p", "outcome")


def _coerce_provider(provider: object) -> str:
    """提供方名规整:转字符串并去首尾空白;空名抛中文错误。"""
    name = str(provider).strip() if provider is not None else ""
    if not name:
        raise ValueError(f"provider 必须是非空字符串,当前值:{provider!r}")
    return name


def _coerce_p(p: object) -> float:
    """概率规整:拒布尔/非数字/NaN(中文错误),数值钳进 [0, 1]。

    越界值按钳制处理(与 fusion._norm 同哲学:非法取值保守归一,不放大);
    类型层面的非法值(字符串 / None / 布尔)直接拒绝,不做任何解释。
    """
    if isinstance(p, bool) or not isinstance(p, (int, float)):
        raise ValueError(f"p 必须是数字,当前值:{p!r}")
    q = float(p)
    if math.isnan(q):
        raise ValueError("p 不能是 NaN")
    if q < 0.0:
        return 0.0
    if q > 1.0:
        return 1.0
    return q


def _coerce_outcome(outcome: object) -> bool:
    """结果规整:布尔原样;整数仅容忍 0/1;其余抛中文错误。"""
    if isinstance(outcome, bool):
        return outcome
    if isinstance(outcome, int) and outcome in (0, 1):  # bool 已被上面截走
        return bool(outcome)
    raise ValueError(f"outcome 必须是布尔值(或 0/1),当前值:{outcome!r}")


def _brier_term(p: float, outcome: bool) -> float:
    """单条记录的 Brier 贡献 (p - outcome)²。"""
    return (p - (1.0 if outcome else 0.0)) ** 2


class ReliabilityTracker:
    """提供方可靠性追踪器(Brier 内存累计 + 可选 jsonl 持久化 + 归一权重)。

    :param jsonl_path: 持久化文件路径;空字符串 = 纯内存。非空时构造即读回
        已有记录(坏行跳过),record 时逐条追加,可跨批次 / 跨进程累积。
    """

    def __init__(self, jsonl_path: str = "") -> None:
        self.jsonl_path = jsonl_path
        self._lock = threading.Lock()
        # provider -> [Σ(p-outcome)², n] 两格累加器:stats/weights 单遍读取,
        # 不缓存均值(避免 record 后的陈旧派生值)。
        self._acc: dict[str, list[float]] = {}
        if jsonl_path:
            self._load()

    # ------------------------------------------------------------------
    # 记录与统计
    # ------------------------------------------------------------------

    def record(
        self, provider: str, p: float, outcome: bool, ts: float | None = None
    ) -> dict[str, Any]:
        """记录一条本地反馈:提供方 ``provider`` 曾报概率 ``p``,事实为 ``outcome``。

        内存累计 Brier;``jsonl_path`` 非空时同步追加一行 JSON(与内存同锁,
        保证文件与内存一致)。返回归一化后的条目副本(改返回值不影响内部)。

        :param ts: 可选事件时刻(Unix epoch 秒,V14)。``None``(缺省)= 不落盘
            ``ts`` 键,行结构与历史逐字节一致(旧读取方零感知);显式传入时经
            :func:`_coerce_ts` 规整(拒布尔/非数字/NaN/inf,中文错误)后随行
            落盘。Brier 累计**不消费** ts(纯元数据,stats/weights 与不传时
            逐位一致),消费方是贝叶斯重放路径(orchestrator 按 ts 做遗忘)。
        :raises ValueError: provider 为空 / p 非数字或 NaN / outcome 非布尔 /
            ts 非法(均为中文消息)。
        """
        entry = {
            "provider": _coerce_provider(provider),
            "p": _coerce_p(p),
            "outcome": _coerce_outcome(outcome),
        }
        if ts is not None:
            entry["ts"] = _coerce_ts(ts)
        with self._lock:
            acc = self._acc.setdefault(entry["provider"], [0.0, 0])
            acc[0] += _brier_term(entry["p"], entry["outcome"])
            acc[1] += 1
            if self.jsonl_path:
                self._append_jsonl(entry)
        telemetry.inc("reliability.records")
        return dict(entry)

    def stats(self) -> dict[str, dict[str, Any]]:
        """各提供方统计快照:``{provider: {"n": int, "brier": float}}``。

        brier = Σ(p-outcome)² / n;n 恒 >= 1(只含已记录的提供方)。
        返回的是新建 dict,改返回值不影响内部状态。
        """
        with self._lock:
            return {
                prov: {"n": int(acc[1]), "brier": acc[0] / acc[1]}
                for prov, acc in self._acc.items()
            }

    def weights(self, min_n: int = MIN_N) -> dict[str, Any]:
        """可靠性权重:``{provider: w 或 None}``,w = 1/(brier+ε) 归一。

        - 样本充足(n >= ``min_n``)的提供方之间归一,Σ w = 1
          (样本不足者不参与归一化分母);
        - 样本不足的提供方值为 ``None``(调用方自行回退,如等权);
        - 无任何样本充足者时全部为 ``None``;从未记录过的提供方不在返回中。
        """
        with self._lock:
            snapshot: dict[str, tuple[float, int]] = {
                prov: (acc[0], int(acc[1])) for prov, acc in self._acc.items()
            }
        result: dict[str, Any] = {prov: None for prov in snapshot}
        raw: dict[str, float] = {
            prov: 1.0 / (sq / n + EPSILON)
            for prov, (sq, n) in snapshot.items()
            if n >= min_n
        }
        if raw:
            total = sum(raw.values())  # raw 值恒正,无需防零
            for prov, w in raw.items():
                result[prov] = w / total
        return result

    # ------------------------------------------------------------------
    # 持久化(jsonl)
    # ------------------------------------------------------------------

    def _append_jsonl(self, entry: dict[str, Any]) -> None:
        """追加一行 JSON(调用方已持锁;UTF-8,ensure_ascii=False 便于人读)。"""
        path = Path(self.jsonl_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _load(self) -> None:
        """读回 jsonl:损坏 / 缺字段 / 值非法的行跳过并记 debug 日志。

        V14:行内 ``ts`` 键可选——旧行(无 ts)照常读回;带 ts 的行 ts 仅供
        校验(Brier 累计不消费时刻);ts 存在但非法的行按坏行跳过,与
        p/outcome 非法同口径(保证 Brier 读侧与贝叶斯重放侧裁决一致)。
        """
        path = Path(self.jsonl_path)
        if not path.is_file():
            return
        with path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                raw = line.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                    if not isinstance(obj, dict) or any(
                        k not in obj for k in _ENTRY_KEYS
                    ):
                        raise ValueError("缺少字段")
                    prov = _coerce_provider(obj["provider"])
                    p = _coerce_p(obj["p"])
                    outcome = _coerce_outcome(obj["outcome"])
                    if "ts" in obj:  # V14 可选时刻:非法即整行跳过
                        _coerce_ts(obj["ts"])
                except (ValueError, TypeError, json.JSONDecodeError):
                    logger.debug(
                        "跳过损坏的可靠性记录:%s 第 %d 行", self.jsonl_path, lineno
                    )
                    continue
                acc = self._acc.setdefault(prov, [0.0, 0])
                acc[0] += _brier_term(p, outcome)
                acc[1] += 1


# ===========================================================================
# V12 贝叶斯分层可靠性(A216):数值层(正则化不完全 Beta 逆,纯 stdlib)
# ===========================================================================


def _log_beta_fn(a: float, b: float) -> float:
    """ln B(a,b) = lgamma(a)+lgamma(b)-lgamma(a+b)(对数域防溢出)。"""
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _betacf(a: float, b: float, x: float, eps: float = 3e-14, itmax: int = 300) -> float:
    """不完全 Beta 连分数(Lentz 修正递推;Numerical Recipes betacf 同构)。

    收敛判据 |delta-1| < eps,300 轮上限对我们关心的量级
    (a,b <= 数百、x∈(0,1))远未触及;纯算术、零随机 → 确定性。
    """
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """正则化不完全 Beta 函数 I_x(a,b) = CDF of Beta(a,b) 在 x 处。

    对称关系 I_x(a,b) = 1 - I_{1-x}(b,a) 保证连分数始终在收敛最快的
    分支上求值;x<=0 → 0、x>=1 → 1(闭式边界)。
    """
    if a <= 0.0 or b <= 0.0:
        raise ValueError(f"Beta 参数必须为正,当前: a={a!r}, b={b!r}")
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        a * math.log(x) + b * math.log(1.0 - x) - _log_beta_fn(a, b)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - math.exp(
        b * math.log(1.0 - x) + a * math.log(x) - _log_beta_fn(a, b)
    ) * _betacf(b, a, 1.0 - x) / b


def _beta_ppf(
    q: float,
    a: float,
    b: float,
    tol: float = 1e-12,
    max_iter: int = 200,
    counter: list[int] | None = None,
) -> float:
    """Beta(a,b) 分位数(PPF):对单调 CDF 二分,确定性收敛。

    bisection 区间 [0,1] 每轮减半,``tol`` 提前停机(约 40 轮达 1e-12);
    上限 200 轮 = 2^-200,理论不可达,防御性封顶。闭式边界:q<=0 → 0、
    q>=1 → 1。``counter`` 传入单格 list 时逐次累加尾概率探针次数
    (操作计数,红线 31)。
    """
    if not 0.0 < q < 1.0:
        return 1.0 if q >= 1.0 else 0.0
    aa = max(a, 1e-12)
    bb = max(b, 1e-12)
    lo, hi = 0.0, 1.0
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        cdf = _betainc(aa, bb, mid)
        if counter is not None:
            counter[0] += 1
        if cdf < q:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


def _beta_ci95(
    a: float, b: float, counter: list[int] | None = None
) -> tuple[float, float]:
    """Beta(a,b) 等尾 95% 置信区间 [q_{0.025}, q_{0.975}](两次 PPF)。"""
    return (
        _beta_ppf(0.025, a, b, counter=counter),
        _beta_ppf(0.975, a, b, counter=counter),
    )


def _coerce_ts(ts: object) -> float:
    """时间戳规整:拒布尔/非数字/NaN/inf(中文错误);int/float 原样转 float。

    ts 只要求与 half_life 同单位,不要求单调、不要求非负(相对时刻即可)。
    """
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        raise ValueError(f"ts 必须是数字,当前值:{ts!r}")
    q = float(ts)
    if math.isnan(q) or math.isinf(q):
        raise ValueError("ts 必须是有限数(非 NaN/inf)")
    return q


class BayesianReliabilityTracker:
    """贝叶斯分层可靠性追踪器(Beta 后验 + 指数遗忘 + 分层收缩,纯内存)。

    与 :class:`ReliabilityTracker`(Brier 反比点估计)并存、互不影响:
    本类记录 **correctness 事件流** ``(member, correct, ts)`` 并在查询时
    按指定 as-of 时刻重放事件流——遗忘使 α/β 依赖时刻,必须保留原始流
    (这是与 Brier 累加器"记完即忘"的结构差异)。

    数学(V10.4 波报告点名的两个短板各有解):

    1. **Beta 后验**(5 样本幸运主导 → 后验均值 + CI 显式量化不确定):
       α_m = 1 + Σ对, β_m = 1 + Σ错(均匀先验 Beta(1,1));
    2. **指数遗忘**(分布漂移感知):事件权重 obs(t) = exp(-ln2·Δt/half_life)
       计入等效 α/β,等效样本数 n_eff = Σobs——推导:加权对数似然
       Σ obs_i·ln Bernoulli(θ; y_i) 与伪计数 obs_i 的 Beta-Bernoulli
       同形,故遗忘保持共轭闭式;旧观测权重指数衰减 → 后验偏向新观测;
    3. **分层收缩**:λ = κ/(κ+n_eff),θ* = (1-λ)·θ_m + λ·μ_G
       (θ_m = α_m/(α_m+β_m),μ_G = 全局池后验均值,池 = 全体成员事件
       同款遗忘累加 + 同款先验);**冷启动**:n_eff < ``min_n`` 时 λ := 1
       (完全收缩到全局),新成员 3/3 不会被幸运主导;
    4. **置信区间**:θ* 的 Beta 区间用收缩后参数 (α*, β*) =
       (θ*·n*, (1-θ*)·n*),n* = α_m+β_m(保留成员自身浓度,收缩只挪
       中心不借池的确定性——宁可区间保守,不虚窄)。

    :param half_life: 遗忘半衰期(与 ts 同单位);``None``/``inf`` = 关闭
        遗忘(每条事件权重恰为 1.0,不评估 exp)。
    :param kappa: 分层收缩伪计数 κ;κ→∞ 收缩到全局均值,n_eff→∞ 成员
        数据主导。

    零墙钟:所有查询以 ``as_of`` 为锚,缺省 = 事件流最大 ts(确定性);
    ``record`` 的 ts 缺省 = 上一条 ts + 1.0(首条为 1.0)——确定性"滴答"
    时刻,不读系统时钟。线程安全:单锁保护事件流(与 Brier tracker 同款)。

    操作计数(红线 31 惯例,供 bench / 测试断言复杂度,零墙钟):

    - :attr:`decay_evals`:exp() 遗忘权重评估次数(每事件每查询一次;
      遗忘关闭时恒 0);
    - :attr:`ci_probes`:CI 二分的尾概率(不完全 Beta)探针次数。
    """

    def __init__(
        self,
        half_life: float | None = DEFAULT_HALF_LIFE,
        kappa: float = DEFAULT_KAPPA,
    ) -> None:
        if half_life is not None:
            if isinstance(half_life, bool) or not isinstance(half_life, (int, float)):
                raise ValueError(f"half_life 必须是数字或 None,当前值:{half_life!r}")
            hl = float(half_life)
            if math.isnan(hl) or hl <= 0.0:
                raise ValueError(f"half_life 必须为正有限数,当前值:{half_life!r}")
            self.half_life: float | None = hl if math.isfinite(hl) else None
        else:
            self.half_life = None
        if isinstance(kappa, bool) or not isinstance(kappa, (int, float)):
            raise ValueError(f"kappa 必须是数字,当前值:{kappa!r}")
        kap = float(kappa)
        if math.isnan(kap) or kap < 0.0:
            raise ValueError(f"kappa 必须为非负有限数,当前值:{kappa!r}")
        self.kappa = kap
        self._lock = threading.Lock()
        # 事件流 (member, correct, ts) 按录入顺序保存:遗忘使后验依赖
        # as-of 时刻,只能保留原流、查询时重放(重放顺序确定 → 输出确定);
        # 成员顺序由重放首现推导(as-of 因果过滤后自然只含当时已见成员)。
        self._events: list[tuple[str, bool, float]] = []
        # 操作计数(累计,单调递增;详见类 docstring)。
        self.decay_evals: int = 0
        self.ci_probes: int = 0

    # ------------------------------------------------------------------
    # 记录
    # ------------------------------------------------------------------

    def record(
        self, member: str, correct: object, ts: float | None = None
    ) -> dict[str, Any]:
        """记录一条 correctness 事件:成员 ``member`` 在 ``ts`` 时刻对/错。

        :param member: 非空成员名(规整同 :class:`ReliabilityTracker`)。
        :param correct: 布尔(容忍 0/1 整数,同 Brier 的 outcome 口径)。
        :param ts: 事件时刻;缺省 = 上一条 ts + 1.0(首条 1.0)——确定性
            滴答,零墙钟;显式传入用于固定时钟测试 / 真实运营时刻。
        :raises ValueError: member 空 / correct 非布尔 / ts 非法(中文消息)。
        :return: 归一化条目副本 ``{"member", "correct", "ts"}``。
        """
        name = _coerce_provider(member)
        flag = _coerce_outcome(correct)
        with self._lock:
            when = (
                _coerce_ts(self._events[-1][2] + 1.0)
                if ts is None and self._events
                else (1.0 if ts is None else _coerce_ts(ts))
            )
            self._events.append((name, flag, when))
        telemetry.inc("reliability.bayes.records")
        return {"member": name, "correct": flag, "ts": when}

    # ------------------------------------------------------------------
    # 内部:事件流重放(遗忘衰减的等效 α/β)
    # ------------------------------------------------------------------

    def _snapshot_events(self) -> list[tuple[str, bool, float]]:
        """持锁拷贝事件流(查询在锁外重放,纯函数、不阻塞录入)。"""
        with self._lock:
            return list(self._events)

    def _decay_sums(
        self, as_of: float | None
    ) -> tuple[dict[str, float], dict[str, float], dict[str, int], list[str], float]:
        """重放事件流,返回 (succ, fail, raw_n, order, as_of_used)。

        - 事件权 obs = exp(-ln2·(as_of - ts)/half_life),ts > as_of 的
          "未来"事件不计(因果性:as-of 时刻尚未发生);
        - 遗忘关闭(half_life None)时 obs 恒 1 且**不**评估 exp
          (操作计数 decay_evals 恒 0,精确手算路径);
        - succ/fail 为各成员的加权对/错累计(不含先验),raw_n 为原始条数。
        """
        events = self._snapshot_events()
        if not events:
            if as_of is None:
                return {}, {}, {}, [], 0.0
            try:
                return {}, {}, {}, [], _coerce_ts(as_of)
            except ValueError as exc:
                raise ValueError(f"as_of 非法:{exc}") from exc
        if as_of is None:
            anchor = max(ts for _, _, ts in events)
        else:
            try:
                anchor = _coerce_ts(as_of)
            except ValueError as exc:
                raise ValueError(f"as_of 非法:{exc}") from exc
        succ: dict[str, float] = {}
        fail: dict[str, float] = {}
        raw_n: dict[str, int] = {}
        order: list[str] = []
        hl = self.half_life
        for name, flag, ts in events:
            if ts > anchor:
                continue  # as-of 时刻尚未发生的事件不参与重放
            if name not in raw_n:
                raw_n[name] = 0
                succ[name] = 0.0
                fail[name] = 0.0
                order.append(name)
            raw_n[name] += 1
            if hl is None:
                obs = 1.0
            else:
                self.decay_evals += 1  # 操作计数:一次 exp 遗忘评估
                obs = math.exp(-_LN2 * (anchor - ts) / hl)
            if flag:
                succ[name] += obs
            else:
                fail[name] += obs
        return succ, fail, raw_n, order, anchor

    # ------------------------------------------------------------------
    # 查询:统计 / 后验 / 权重
    # ------------------------------------------------------------------

    def stats(self, as_of: float | None = None) -> dict[str, dict[str, Any]]:
        """各成员**未收缩**的后验快照:``{member: {n, n_eff, alpha, beta, mean}}``。

        alpha = 1 + Σ加权对, beta = 1 + Σ加权错, mean = α/(α+β),
        n = 原始条数(<= as-of 的事件),n_eff = Σobs = α+β-2。
        与 Brier :meth:`ReliabilityTracker.stats` 同位的"原始统计"口径;
        收缩 / CI 见 :meth:`posterior`。
        """
        succ, fail, raw_n, order, _ = self._decay_sums(as_of)
        return {
            name: {
                "n": raw_n[name],
                "n_eff": succ[name] + fail[name],
                "alpha": PRIOR_A + succ[name],
                "beta": PRIOR_B + fail[name],
                "mean": (PRIOR_A + succ[name])
                / (PRIOR_A + PRIOR_B + succ[name] + fail[name]),
            }
            for name in order
        }

    def posterior(
        self, as_of: float | None = None, min_n: int = MIN_N
    ) -> dict[str, dict[str, Any]]:
        """分层收缩后的成员后验(含 95% CI 与收缩明细,供解释 / 审计)。

        每成员返回::

            {"n", "n_eff",                    # 原始条数 / 等效样本数
             "alpha_raw", "beta_raw", "mean_raw",   # 未收缩后验
             "shrink",                        # λ = κ/(κ+n_eff),冷启动取 1
             "pool_mean",                     # 全局池后验均值 μ_G
             "alpha", "beta", "mean",         # 收缩后 (θ*·n*, (1-θ*)·n*, θ*)
             "ci95"}                          # (lo, hi) Beta 等尾 95%

        全局池 = 全体成员(as-of 内)事件同款遗忘累加 + Beta(1,1) 先验;
        n_eff >= ``min_n`` 走连续收缩,否则 λ=1(冷启动完全收缩)。
        空事件流返回 ``{}``。
        """
        return self._shrunk(as_of=as_of, min_n=min_n, want_ci=True)

    def _shrunk(
        self,
        as_of: float | None,
        min_n: int,
        want_ci: bool,
    ) -> dict[str, dict[str, Any]]:
        """收缩后验的公共内核:``want_ci=False`` 跳过 CI 二分(热路径省算)。"""
        succ, fail, raw_n, order, _ = self._decay_sums(as_of)
        if not order:
            return {}
        pool_succ = sum(succ.values())
        pool_fail = sum(fail.values())
        pool_a = PRIOR_A + pool_succ
        pool_b = PRIOR_B + pool_fail
        pool_mean = pool_a / (pool_a + pool_b)
        out: dict[str, dict[str, Any]] = {}
        for name in order:
            alpha_raw = PRIOR_A + succ[name]
            beta_raw = PRIOR_B + fail[name]
            n_eff = succ[name] + fail[name]
            mean_raw = alpha_raw / (alpha_raw + beta_raw)
            lam = 1.0 if n_eff < min_n else self.kappa / (self.kappa + n_eff)
            mean_star = (1.0 - lam) * mean_raw + lam * pool_mean
            n_star = alpha_raw + beta_raw  # 保留成员自身浓度,只挪中心
            alpha_star = mean_star * n_star
            beta_star = (1.0 - mean_star) * n_star
            entry: dict[str, Any] = {
                "n": raw_n[name],
                "n_eff": n_eff,
                "alpha_raw": alpha_raw,
                "beta_raw": beta_raw,
                "mean_raw": mean_raw,
                "shrink": lam,
                "pool_mean": pool_mean,
                "alpha": alpha_star,
                "beta": beta_star,
                "mean": mean_star,
            }
            if want_ci:
                probe: list[int] = [0]
                entry["ci95"] = _beta_ci95(alpha_star, beta_star, counter=probe)
                self.ci_probes += probe[0]  # 操作计数:CI 尾概率探针(累计)
            out[name] = entry
        return out

    def weights(
        self, as_of: float | None = None, min_n: int = MIN_N
    ) -> dict[str, float]:
        """可靠性权重:``{member: w}``,w = 收缩后后验均值 θ* 归一(Σ=1)。

        与 Brier :meth:`ReliabilityTracker.weights` **同构的消费接口形状**
        (成员名 → 归一权重),差异仅在:correctness 后验均值越大越好,
        无需 Brier 的反比失真映射;且冷启动由收缩吸收(向全局退让),
        **不返回 None**——成员键集恒等于已记录成员集,空流为空 dict。

        排序权重 = 后验均值排序(θ* 单调);消费方
        (fusion_reliable / orchestrator 适配层)的"缺失回退均值"逻辑
        对无 None 形状零改动可用。
        """
        post = self._shrunk(as_of=as_of, min_n=min_n, want_ci=False)
        total = sum(p["mean"] for p in post.values())
        if not post or total <= 0.0:
            return {}  # 理论不可达(θ* ∈ (0,1) 恒正),防御式空回退
        return {name: p["mean"] / total for name, p in post.items()}

    def weights_with_ci(
        self, as_of: float | None = None, min_n: int = MIN_N
    ) -> dict[str, dict[str, Any]]:
        """权重 + 不确定度:``{member: {"weight", "mean", "ci95", "n_eff"}}``。

        weight 为归一权重(Σ=1,与 :meth:`weights` 一致);mean 为未归一的
        收缩后后验均值;ci95 为 Beta(α*, β*) 等尾 95% 区间——CI 覆盖的是
        成员真实 correctness θ(未归一),不是归一份额。
        """
        post = self._shrunk(as_of=as_of, min_n=min_n, want_ci=True)
        total = sum(p["mean"] for p in post.values())
        if not post or total <= 0.0:
            return {}
        return {
            name: {
                "weight": p["mean"] / total,
                "mean": p["mean"],
                "ci95": p["ci95"],
                "n_eff": p["n_eff"],
            }
            for name, p in post.items()
        }


# ===========================================================================
# V15 检查点化增量(A242):O(n) 每查询重放 → 增量折叠 + 单因子时间校正
# ===========================================================================

#: 待折叠缓冲的默认容量:record 只追加 O(1),缓冲满 256 条时一次折叠
#: O(成员数 + 256)——摊还 record O(1)、查询 O(成员数 + 缓冲),与流长无关。
DEFAULT_FOLD_EVERY: int = 256

#: 快照 JSON 的格式标识与版本(不兼容变更时升版本号,load 拒绝旧格式)。
_SNAPSHOT_FORMAT: str = "netsentinel.bayes-checkpoint"
_SNAPSHOT_VERSION: int = 1


def _snapshot_number(value: object, field: str, lower: float) -> float:
    """快照数值字段规整:拒布尔 / 非数字 / 非有限 / 低于下界(中文错误)。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"快照成员 {field} 非法:{value!r}")
    q = float(value)
    if not math.isfinite(q) or q < lower:
        raise ValueError(f"快照成员 {field} 非法:{value!r}(应 >= {lower})")
    return q


class CheckpointedBayesianReliabilityTracker(BayesianReliabilityTracker):
    """检查点化的贝叶斯可靠性追踪器(增量维护等效 (α, β, n),查询免全量重放)。

    **动机(A216 交付报告风险项)**:基类把 correctness 事件流完整留在内存
    (遗忘使 α/β 依赖 as-of 时刻,只能保留原流)、每次查询全量重放,单次
    ``weights()`` 为 O(事件数)——长流(> 1e5)下每次集成加权都要付一次
    全量扫描。本类对标流式系统 checkpoint/快照惯例,改为"写入时增量折叠 +
    读取时惰性重衰减":

    1. **增量折叠(fold)**:record 把事件压进待折叠缓冲,缓冲满
       ``fold_every``(缺省 256)条时整体折叠进检查点——各成员维护以锚点
       ``checkpoint_ts`` 为基准的 (succ_cp, fail_cp, raw_n);乱序到达
       (ts' < 锚点)的事件在折叠时按 exp(-ln2·(锚点 - ts')/half_life)
       直接并入锚点和,无需展开历史;
    2. **惰性时间校正(query)**:查询对检查点和乘**单一**衰减因子
       exp(-ln2·(as_of - checkpoint_ts)/half_life),再因果扫描缓冲中
       ts <= as_of 的少量新事件——单次查询 O(成员数 + 缓冲),与流长无关。

    **精确性推导(检查点 + 校正 = 全量重放,恰等价)**:设事件 i 时刻
    ts_i、查询时刻 t、半衰期 h(遗忘权重 w_i(t) = 2^{-(t - ts_i)/h})。
    检查点锚点 T 满足"已折叠事件均 ts_i <= T"。对任意 t >= T,由指数核的
    泛函方程 f(a+b) = f(a)·f(b)::

        w_i(t) = 2^{-(t - ts_i)/h}
               = 2^{-(t - T)/h} · 2^{-(T - ts_i)/h} = c(t) · w_i(T)

    求和与数乘可交换,故 Σ_i w_i(t)·x_i = c(t) · Σ_i w_i(T)·x_i:锚点处的
    加权和(检查点所存)乘一个标量 = 全量重放。**精确性条件**:

    (i) **t >= T**(锚点不晚于查询时刻):t < T 时,锚点之后已折叠进聚合
        的"未来"事件无法从和中剔除(因果排除不可恢复),只能回退全量
        重放——保留全量事件时(``keep_history=True``,缺省)回退基类
        :meth:`BayesianReliabilityTracker._decay_sums`(精确);快照加载
        后 / ``keep_history=False`` 不再保留,显式中文 ValueError 拒绝
        (宁可拒绝,不可把"未来"事件算进过去);
    (ii) **遗忘核为指数核**:上述泛函方程是指数函数独有性质(线性 / 幂律
        核的 checkpoint+校正 ≠ 全量重放),half_life 有限或 None 均满足
        (None 时权重恒 1,校正因子 = 1,退化为纯计数、免 exp);
    (iii) **浮点复合误差有界**:每次重锚引入 ~ε 相对误差,k 次折叠累计
        ~k·ε(1e5 事件 / 256 每折 ≈ 390 次 ≈ 8.6e-14 << 1e-12 测试容差;
        无遗忘路径不评估 exp,整数计数与基类逐位相等)。

    n(原始条数)不随遗忘变化,折叠时 +1 维护,任意 as_of >= T 精确;
    成员首现顺序 = (检查点顺序 + 缓冲因果首现),与基类按到达序重放的
    首现顺序一致。

    **快照(save/load)**::meth:`save_snapshot` 先冲刷缓冲再落盘 JSON
    ``{"checkpoint_ts", "half_life", "kappa", "members": [{member, alpha,
    beta, n}]}``(members 列表保首现顺序;alpha/beta = 先验 + 锚点处
    加权和,即锚点时刻的收缩前等效后验参数);:meth:`load_snapshot`
    校验格式 / 版本 / 半衰期与 κ 一致(防语义漂移)后重建检查点。加载后
    的实例不保留锚点之前的原始事件(t < 检查点时刻的查询按条件 (i)
    拒绝),后续 record / 查询照常增量进行——"快照 = 状态压缩"的语义。
    加载后 ts 缺省滴答从 checkpoint_ts + 1.0 继续(确定性)。

    **接线方式(orchestrator 侧,本模块零改动)**:本类是基类的子类
    (isinstance 兼容、``weights()``/``posterior()`` 等接口同形),把
    ``pipeline.orchestrator._default_bayes_reliability_tracker`` 中的构造
    ``reliability.BayesianReliabilityTracker(half_life=...)`` 替换为
    ``reliability.CheckpointedBayesianReliabilityTracker(half_life=...,
    keep_history=False)`` 即可(逐行 record 重放语义不变,之后每次
    ``weights()`` 为 O(成员数),内存 O(成员数 + fold_every));跨进程可
    重放一次后 ``save_snapshot``,后续 ``load_snapshot``(half_life /
    kappa 须一致)免重放恢复。``fuse_reliable(..., bayes_tracker=本类
    实例)`` 直接可用(消费方仅调用 ``weights()``)。

    :param fold_every: 待折叠缓冲容量(每满这么多条 record 折叠一次);
        越大查询要扫描的缓冲越长、重锚次数越少,缺省
        :data:`DEFAULT_FOLD_EVERY` = 256;``1`` = 逐条即时折叠。
    :param keep_history: 是否同时保留全量事件流(缺省 ``True``):保留则
        t < 锚点的查询回退基类全量重放(精确,内存 O(事件数) 与基类
        同);``False`` 则只留检查点 + 缓冲(内存 O(成员数 + fold_every),
        长流推荐),t < 锚点的查询显式拒绝。

    操作计数(红线 31 惯例):``decay_evals`` = exp 遗忘评估次数——折叠时
    每事件一次 + 每次重锚一次 + 查询时一次锚点因子 + 缓冲可见事件数次
    (对照基类的"每查询每事件一次",多查询负载下总评估数从 O(Q·n) 降到
    O(n + Q·(成员数 + 缓冲)));``ci_probes`` 与基类同口径。
    """

    def __init__(
        self,
        half_life: float | None = DEFAULT_HALF_LIFE,
        kappa: float = DEFAULT_KAPPA,
        *,
        fold_every: int = DEFAULT_FOLD_EVERY,
        keep_history: bool = True,
    ) -> None:
        super().__init__(half_life=half_life, kappa=kappa)
        if isinstance(fold_every, bool) or not isinstance(fold_every, int):
            raise ValueError(f"fold_every 必须是正整数,当前值:{fold_every!r}")
        if fold_every < 1:
            raise ValueError(f"fold_every 必须是正整数,当前值:{fold_every!r}")
        if not isinstance(keep_history, bool):
            raise ValueError(f"keep_history 必须是布尔值,当前值:{keep_history!r}")
        self.fold_every = fold_every
        self.keep_history = keep_history
        # 是否保留"自建流起"的全量事件(可供 t < 锚点查询回退基类重放);
        # 快照加载重建的实例恒 False(锚点之前的历史已压缩进检查点)。
        self._full_history = keep_history
        # 待折叠缓冲(到达序):查询时因果扫描,满 fold_every 条时折叠。
        self._pending: list[tuple[str, bool, float]] = []
        # 检查点:各成员以 _anchor 为基准的加权对 / 加权错 / 原始条数,
        # 以及首现顺序(与基类重放的首现顺序一致)。
        self._cp_succ: dict[str, float] = {}
        self._cp_fail: dict[str, float] = {}
        self._cp_n: dict[str, int] = {}
        self._cp_order: list[str] = []
        self._anchor: float | None = None
        # 上一条事件的 ts(ts 缺省滴答 = 它 + 1.0;快照加载后 = 锚点)。
        self._last_ts: float | None = None

    # ------------------------------------------------------------------
    # 记录(语义与基类逐字一致 + 增量维护检查点)
    # ------------------------------------------------------------------

    def record(
        self, member: str, correct: object, ts: float | None = None
    ) -> dict[str, Any]:
        """记录一条 correctness 事件(校验 / 返回值与基类逐字一致)。

        事件先入待折叠缓冲;缓冲满 ``fold_every`` 条时折叠进检查点
        (O(成员数 + 缓冲),摊还 record O(1))。``keep_history=True`` 时
        同步保留全量事件流(供 t < 锚点的查询回退基类重放)。
        ts 缺省 = 上一条 ts + 1.0(首条 1.0;快照加载后上一条 =
        checkpoint_ts),零墙钟、确定性。
        """
        name = _coerce_provider(member)
        flag = _coerce_outcome(correct)
        with self._lock:
            if ts is None:
                when = (
                    1.0
                    if self._last_ts is None
                    else _coerce_ts(self._last_ts + 1.0)
                )
            else:
                when = _coerce_ts(ts)
            self._last_ts = when
            self._pending.append((name, flag, when))
            if self.keep_history:
                self._events.append((name, flag, when))
            if len(self._pending) >= self.fold_every:
                self._fold_locked()
        telemetry.inc("reliability.bayes.records")
        return {"member": name, "correct": flag, "ts": when}

    # ------------------------------------------------------------------
    # 内部:增量折叠
    # ------------------------------------------------------------------

    def _fold_locked(self) -> None:
        """把待折叠缓冲并入检查点(调用方已持锁;纯算术、确定性)。

        新锚点 = max(旧锚点, 缓冲最大 ts):先按指数核泛函方程把旧锚点和
        重锚到新锚点(整体乘 exp(-ln2·ΔT/half_life)),再逐条按
        exp(-ln2·(新锚点 - ts)/half_life) 并入(乱序事件 ts < 旧锚点时,
        它在新锚点处的权重直接可算,无需展开历史)。无遗忘时权重恒 1、
        连 exp 都不评估(与基类"关闭遗忘不评估 exp"同口径)。
        """
        if not self._pending:
            return
        new_anchor = max(ts for _, _, ts in self._pending)
        if self._anchor is not None and self._anchor > new_anchor:
            new_anchor = self._anchor  # 全乱序批:锚点不回退
        hl = self.half_life
        if hl is not None and self._anchor is not None and new_anchor > self._anchor:
            self.decay_evals += 1  # 操作计数:一次重锚衰减
            factor = math.exp(-_LN2 * (new_anchor - self._anchor) / hl)
            for name in self._cp_succ:
                self._cp_succ[name] *= factor
                self._cp_fail[name] *= factor
        for name, flag, ts in self._pending:
            if hl is None:
                weight = 1.0
            else:
                self.decay_evals += 1  # 操作计数:一次折叠遗忘评估
                weight = math.exp(-_LN2 * (new_anchor - ts) / hl)
            if name not in self._cp_n:
                self._cp_n[name] = 0
                self._cp_succ[name] = 0.0
                self._cp_fail[name] = 0.0
                self._cp_order.append(name)
            self._cp_n[name] += 1
            if flag:
                self._cp_succ[name] += weight
            else:
                self._cp_fail[name] += weight
        self._anchor = new_anchor
        self._pending.clear()

    # ------------------------------------------------------------------
    # 查询分发:热路径(检查点 + 校正)/ 冷路径(基类全量重放 / 拒绝)
    # ------------------------------------------------------------------

    def _decay_sums(
        self, as_of: float | None
    ) -> tuple[dict[str, float], dict[str, float], dict[str, int], list[str], float]:
        """检查点热路径 + (必要时)基类全量重放冷路径(返回形状与基类一致)。

        - **热路径**(as_of >= 锚点,含缺省 as_of = 全流最大 ts):检查点 ×
          单因子校正 + 缓冲因果扫描,O(成员数 + 缓冲),与全量重放恰等价
          (推导见类 docstring);
        - **冷路径**(显式 as_of < 锚点):保留全量事件流时回退基类
          ``_decay_sums``(精确);否则(快照加载后 / keep_history=False)
          抛中文 ValueError——已折叠进聚合的"未来"事件无法剔除,宁可
          拒绝不可近似。
        """
        with self._lock:
            anchor = self._anchor
            if anchor is None and not self._pending:
                # 空流语义与基类一致:显式 as_of 仍要校验
                if as_of is None:
                    return {}, {}, {}, [], 0.0
                try:
                    return {}, {}, {}, [], _coerce_ts(as_of)
                except ValueError as exc:
                    raise ValueError(f"as_of 非法:{exc}") from exc
            pending = list(self._pending)
            if as_of is None:
                anchor_used = anchor
                for _, _, ts in pending:  # 缺省 = 全流最大 ts(与基类同口径)
                    if anchor_used is None or ts > anchor_used:
                        anchor_used = ts
            else:
                try:
                    anchor_used = _coerce_ts(as_of)
                except ValueError as exc:
                    raise ValueError(f"as_of 非法:{exc}") from exc
            cold = anchor is not None and anchor_used < anchor
            if not cold:
                # 持锁快照检查点与锚点(与 anchor 同一次加锁,免折叠竞态)
                succ = dict(self._cp_succ)
                fail = dict(self._cp_fail)
                raw_n = dict(self._cp_n)
                order = list(self._cp_order)
        if cold:
            if not self._full_history:
                raise ValueError(
                    f"as_of={anchor_used!r} 早于检查点锚点 {anchor!r}:精确"
                    "重放需要锚点之前的全量事件流,本实例不再保留(快照加载"
                    "或 keep_history=False);请改用 as_of >= 检查点时刻,或"
                    "使用保留全量事件的 BayesianReliabilityTracker"
                )
            return super()._decay_sums(as_of)  # 基类全量重放(自带锁,精确)
        hl = self.half_life
        if anchor is not None and hl is not None and anchor_used > anchor:
            self.decay_evals += 1  # 操作计数:一次锚点校正因子
            factor = math.exp(-_LN2 * (anchor_used - anchor) / hl)
            for name in succ:
                succ[name] *= factor
                fail[name] *= factor
        for name, flag, ts in pending:
            if ts > anchor_used:
                continue  # as-of 时刻尚未发生的事件不参与(与基类同口径)
            if hl is None:
                weight = 1.0
            else:
                self.decay_evals += 1  # 操作计数:一次缓冲遗忘评估
                weight = math.exp(-_LN2 * (anchor_used - ts) / hl)
            if name not in raw_n:
                raw_n[name] = 0
                succ[name] = 0.0
                fail[name] = 0.0
                order.append(name)
            raw_n[name] += 1
            if flag:
                succ[name] += weight
            else:
                fail[name] += weight
        return succ, fail, raw_n, order, anchor_used

    # ------------------------------------------------------------------
    # 快照 save / load
    # ------------------------------------------------------------------

    def save_snapshot(self, path: str | Path) -> None:
        """落盘检查点快照(JSON、UTF-8、确定性字节输出)。

        先冲刷待折叠缓冲(快照即完整状态,无信息丢失),再写::

            {"format": "netsentinel.bayes-checkpoint", "version": 1,
             "checkpoint_ts": <锚点 or null>, "half_life": <或 null>,
             "kappa": <κ>,
             "members": [{"member", "alpha", "beta", "n"}, ...]}

        members 列表保首现顺序;alpha/beta = 均匀先验 + 锚点处遗忘加权和
        (锚点时刻的收缩前等效后验参数);n = 原始条数。父目录不存在则创建。
        """
        with self._lock:
            self._fold_locked()
            payload = {
                "format": _SNAPSHOT_FORMAT,
                "version": _SNAPSHOT_VERSION,
                "checkpoint_ts": self._anchor,
                "half_life": self.half_life,
                "kappa": self.kappa,
                "members": [
                    {
                        "member": name,
                        "alpha": PRIOR_A + self._cp_succ[name],
                        "beta": PRIOR_B + self._cp_fail[name],
                        "n": self._cp_n[name],
                    }
                    for name in self._cp_order
                ],
            }
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    @classmethod
    def load_snapshot(
        cls,
        path: str | Path,
        *,
        half_life: float | None = DEFAULT_HALF_LIFE,
        kappa: float = DEFAULT_KAPPA,
        fold_every: int = DEFAULT_FOLD_EVERY,
        keep_history: bool = True,
    ) -> "CheckpointedBayesianReliabilityTracker":
        """从快照重建检查点化追踪器(half_life / kappa 须与快照一致)。

        校验格式 / 版本 / 数值合法性,半衰期或 κ 与构造参数不一致时抛中文
        ValueError(遗忘与收缩语义会漂移,显式拒绝优于静默错算)。重建后
        不保留锚点之前的原始事件(t < checkpoint_ts 的查询将被
        :meth:`_decay_sums` 拒绝),ts 缺省滴答从 checkpoint_ts + 1.0 继续。

        :raises FileNotFoundError: 快照文件不存在。
        :raises ValueError: 非 JSON / 格式或版本不符 / 参数不一致 / 字段非法。
        """
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(obj, dict) or obj.get("format") != _SNAPSHOT_FORMAT:
            raise ValueError(f"快照格式不符:应为 {_SNAPSHOT_FORMAT!r}")
        version = obj.get("version")
        if isinstance(version, bool) or version != _SNAPSHOT_VERSION:
            raise ValueError(
                f"快照版本不支持:{version!r}(当前支持 v{_SNAPSHOT_VERSION})"
            )
        members = obj.get("members")
        if not isinstance(members, list):
            raise ValueError(f"快照缺少 members 列表:{obj!r}")
        tracker = cls(
            half_life=half_life,
            kappa=kappa,
            fold_every=fold_every,
            keep_history=keep_history,
        )
        snap_hl = obj.get("half_life")
        if snap_hl is not None:
            if (
                isinstance(snap_hl, bool)
                or not isinstance(snap_hl, (int, float))
                or not math.isfinite(float(snap_hl))
                or float(snap_hl) <= 0.0
            ):
                raise ValueError(f"快照 half_life 非法:{snap_hl!r}")
            snap_hl = float(snap_hl)
        if snap_hl != tracker.half_life:
            raise ValueError(
                f"快照 half_life={snap_hl!r} 与构造参数 "
                f"half_life={tracker.half_life!r} 不一致(遗忘语义会漂移)"
            )
        snap_kappa = obj.get("kappa")
        if (
            isinstance(snap_kappa, bool)
            or not isinstance(snap_kappa, (int, float))
            or not math.isfinite(float(snap_kappa))
            or float(snap_kappa) < 0.0
        ):
            raise ValueError(f"快照 kappa 非法:{snap_kappa!r}")
        if float(snap_kappa) != tracker.kappa:
            raise ValueError(
                f"快照 kappa={float(snap_kappa)!r} 与构造参数 "
                f"kappa={tracker.kappa!r} 不一致(收缩语义会漂移)"
            )
        ts_raw = obj.get("checkpoint_ts")
        if not members:
            if ts_raw is not None:
                raise ValueError(f"空快照不应携带 checkpoint_ts:{ts_raw!r}")
            return tracker  # 空状态:与新建实例等价
        if (
            isinstance(ts_raw, bool)
            or not isinstance(ts_raw, (int, float))
            or not math.isfinite(float(ts_raw))
        ):
            raise ValueError(f"快照 checkpoint_ts 非法:{ts_raw!r}")
        seen: set[str] = set()
        for entry in members:
            if not isinstance(entry, dict):
                raise ValueError(f"快照成员条目必须是对象:{entry!r}")
            name = _coerce_provider(entry.get("member"))
            if name in seen:
                raise ValueError(f"快照成员重复:{name!r}")
            seen.add(name)
            alpha = _snapshot_number(entry.get("alpha"), "alpha", PRIOR_A)
            beta = _snapshot_number(entry.get("beta"), "beta", PRIOR_B)
            n_raw = entry.get("n")
            if isinstance(n_raw, bool) or not isinstance(n_raw, int) or n_raw < 1:
                raise ValueError(f"快照成员 {name!r} 的 n 非法:{n_raw!r}")
            tracker._cp_succ[name] = alpha - PRIOR_A
            tracker._cp_fail[name] = beta - PRIOR_B
            tracker._cp_n[name] = n_raw
            tracker._cp_order.append(name)
        tracker._anchor = float(ts_raw)
        tracker._last_ts = tracker._anchor
        tracker._full_history = False  # 锚点之前的历史已压缩,不再可重放
        return tracker


# ---------------------------------------------------------------------------
# V7 内核自检(A138 kernel_bench 统一调用;确定性构造数据,零 IO、零墙钟)
# ---------------------------------------------------------------------------


def kernel_selfcheck() -> dict[str, Any]:
    """自检:完美提供方 vs 全错提供方,样本充足后前者权重应压倒性占优。

    构造(确定性):good 报 p=1 且 6 次全对(brier=0,raw=1/0.05=20);
    bad 报 p=1 且 6 次全错(brier=1,raw=1/1.05=20/21)。
    归一:w(good) = 20/(20+20/21) = 21/22 ≈ 0.9545;baseline = 0.5(等权)。
    """
    tracker = ReliabilityTracker()
    for _ in range(6):
        tracker.record("good", 1.0, True)
        tracker.record("bad", 1.0, False)
    w = tracker.weights()
    return {
        "name": "reliability",
        "metric": "good_provider_weight",
        "value": w["good"],
        "baseline": 0.5,  # 等权基线;可靠性学习后好提供方权重应显著高于此
    }


def bayesian_kernel_selfcheck() -> dict[str, Any]:
    """V12 贝叶斯内核自检(操作计数惯例;确定性构造数据,零 IO、零墙钟)。

    漂移仿真(V10.4 波报告痛点):drifted 成员 10 天 × 20 对/天(200 对)
    后在 1 天内连错 20 条;good 成员同窗稳态 80% 正确。half_life=0.5、
    固定时钟注入,查 as_of=10.0(漂移前)与 as_of=10.2(漂移后 0.2 单位,
    "1 日内"主张):

    - drifted 可靠性后验均值应在 1 日内减半以上(指数遗忘压制幸运历史);
    - 同样数据喂 Brier 反比点估计:当日 drifted 份额仍 > 0.5
      (20 错 / 220 条只把 brier 拉到 1/11,滞后可见);
    - 操作计数:decay_evals 恰为三次查询重放的事件数
      (400 + 420 + 420 = 1240,线性复杂度自证,零墙钟)。
    """
    tracker = BayesianReliabilityTracker(half_life=0.5)
    # good:10 天 ×(16 对 + 4 错),每天 20 条(密度保证 n_eff 稳态
    # ≈ r·h/ln2 ≈ 14 > MIN_N,不触冷启动门)
    for d in range(10):
        for k in range(16):
            tracker.record("good", True, ts=d + 0.05 * k)
        for k in range(4):
            tracker.record("good", False, ts=d + 0.8 + 0.05 * k)
    # drifted:前 10 天全对(200 条),密度同款
    for i in range(200):
        tracker.record("drifted", True, ts=0.05 * i)
    mean_before = tracker.posterior(as_of=10.0)["drifted"]["mean"]
    # 漂移:1 天内连错 20 条(t ∈ [10.0, 10.2])
    for i in range(20):
        tracker.record("drifted", False, ts=10.0 + 0.01 * i)
    share_after = tracker.weights(as_of=10.2)["drifted"]
    mean_after = tracker.posterior(as_of=10.2)["drifted"]["mean"]

    # 对照:Brier 反比点估计(同样 200 对 + 20 错 vs good 160 对 40 错,
    # p=1.0 口径:对 = 报 1 实为 1,错 = 报 1 实为 0)
    brier = ReliabilityTracker()
    for _ in range(160):
        brier.record("good", 1.0, True)
    for _ in range(40):
        brier.record("good", 1.0, False)
    for _ in range(200):
        brier.record("drifted", 1.0, True)
    for _ in range(20):
        brier.record("drifted", 1.0, False)
    brier_share = brier.weights()["drifted"]

    assert mean_before > 0.8, "漂移前 drifted 后验均值应 > 0.8(自查)"
    assert mean_after < 0.5 * mean_before, "漂移 1 日内均值应减半以上(自查)"
    assert share_after < 0.45, "漂移后 drifted 权重份额应显著回落(自查)"
    assert brier_share > 0.5, f"Brier 点估计应滞后(份额仍过半),得到 {brier_share}(自查)"
    assert tracker.decay_evals == 1240, (
        f"decay_evals 应为 1240(400+420+420),得到 {tracker.decay_evals}(自查)"
    )
    return {
        "name": "reliability.bayes",
        "metric": "漂移 1 日内 drifted 后验均值(指数遗忘消退)",
        "value": round(mean_after, 6),
        "baseline": round(mean_before, 6),
        "brier_share_same_day": round(brier_share, 6),
        "bayes_share_same_day": round(share_after, 6),
        "decay_evals": tracker.decay_evals,
    }
