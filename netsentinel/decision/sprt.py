"""决策内核·序贯概率比检验 SPRT(A125,CONTRACTS-V7.md §2)。

Wald 序贯概率比检验:不再等全部图片送审完才判定,而是**每送审一张就检验
一次**累计证据,一旦足够偏 NSFW / 偏 CLEAN 立即停审,把省下的 VLM 送审
配额留给下一个站点——"证据够了就收手"。

- :class:`SPRT`:伯努利对数似然比检验器。零假设 H0:单张图片色情概率
  ``p0``(默认 0.5,即"掷硬币");备择 H1:``p1``(默认 0.9,即"色情站点
  图片的典型概率")。每次 :meth:`SPRT.update` 输入一张图片的 ``nsfw_prob``
  (软概率,作为单次伯努利试验的期望),累计对数似然比 LLR 越过上界 A 判
  ``"nsfw"`、跌破下界 B 判 ``"clean"``、否则 ``"continue"``;
- :func:`next_images`:接在 ``rank_for_vlm`` 之后的送审序列生成器
  (不确定度 |p-0.5| 升序逐张送审,判停即截断);
- :func:`kernel_selfcheck`:V7 内核自检(A138 kernel_bench 统一调用)。

**校准假设(重要)**:SPRT 的数学有效性**依赖校准概率**——输入的
``nsfw_prob`` 必须近似真实频率(经 GLM calibrate 校准后近似满足),错误率
α/β 的担保才是真的。stub 分类器的未校准概率不满足该假设,**不建议开启**
(因此 ``cfg.use_sprt`` 默认 ``False``,由调用方/装配线把关;
真实接线由 ``pipeline/kernel_wire.py``(A139)装配,本模块不外呼任何
VLM/网络,:func:`next_images` 只是用已算好的分数做"逐张送审、判停即截断"
的省预算演示/离线模拟语义)。

默认常数下的关键数字(p0=0.5, p1=0.9, α=β=0.05):

- A = ln((1-β)/α) = ln(19) ≈ 2.9444;B = ln(β/(1-α)) = -ln(19) ≈ -2.9444;
- 单张证据上限 = ln(p1/p0) = ln(1.8) ≈ 0.5878(p=1 时),故判 NSFW 最少
  需 6 张(ceil(ln19/ln1.8));判 CLEAN 单张证据达 ln(1/p1 的补)…
  实测全 0.02 序列 2 张即停(p<0.9 时 LLR 为负,越"干净"证据越偏 B)。

e-process / anytime 置信序列内核(2026 前沿对标,A160+,纯增量):

- :class:`MixtureSPRT`:Robbins 混合 SPRT(mixture-SPRT)——以 Beta(a,b)
  (默认 Jeffreys (1/2,1/2))混合似然比对 H0:``p=p0`` 做**复合备择**检验,
  输出累计 e-值 ``E_n``(对数域 lgamma 闭式,防溢出)。``E_n`` 是 H0 下
  期望恒为 1 的非负上鞅,Ville 不等式给出**任意时刻**错误率担保:
  ``P(∃n: E_n ≥ 1/α) ≤ α``(对标 Ramdas 等 *Safe anytime-valid
  inference* 综述与 Howard 等的 betting/e-process 框架);越界方向由
  运行均值 ``s/n`` 与 ``p0`` 的大小给出(nsfw / clean 三态,吸收态语义
  与 :class:`SPRT` 同款);
- :func:`log_evalue`:闭式 log e-值(Beta-Binomial 边缘对原似然之比,
  纯 lgamma/log1p,软概率=分数次计数);
- :func:`stitched_radius` / :func:`anytime_ci` / :func:`confidence_sequence`
  / :class:`ConfidenceSequence`:Howard et al. 2021 线性 stitching 风格的
  Hoeffding-U 型指数边界——样本数按 2 的幂分段,段内逐 n 联合(union)
  Hoeffding、段间几何权重分摊 δ,得到对站点 NSFW 率 p 的**置信序列**:
  任意时刻覆盖率 ≥ 1-δ(纯 math);
- :func:`ci_band_stop` / :func:`ci_band_stop_sequence`:anytime CI 整体
  离开决策带 ``[clean_below, nsfw_above]`` 即停的辅助判定(三态语义)。

以上全部为**新增**类/函数:Wald :class:`SPRT`、:func:`next_images`、
:func:`kernel_selfcheck` 一字未动,旧调用方零感知;依旧零外呼、零第三方
依赖(仅 math)。

安全红线(V7 §0):纯离线数学,零外呼;全部为新增文件,不改任何既有
模块;开关默认关(use_sprt=False),旧调用方零感知。
"""
from __future__ import annotations

import math
from typing import Any, Iterable

from netsentinel.contracts import Config, ImageScore

__all__ = [
    "P0",
    "P1",
    "CLAMP_LO",
    "CLAMP_HI",
    "STATES",
    "SPRT",
    "next_images",
    "kernel_selfcheck",
    "MixtureSPRT",
    "log_evalue",
    "stitched_radius",
    "anytime_ci",
    "confidence_sequence",
    "ConfidenceSequence",
    "ci_band_stop",
    "ci_band_stop_sequence",
]

#: 零假设 H0 的单张色情概率("干净/硬币面")。导出可调:构造 SPRT 时以
#: ``p0=`` 覆盖,无需改模块全局。
P0: float = 0.5

#: 备择假设 H1 的单张色情概率("色情站点典型面")。导出可调(同上)。
P1: float = 0.9

#: 送审概率的下/上钳制边界:stub 或数值噪声给出的 0/1 极值会放大
#: 对数似然比的数值误差(且 p=0/1 使 (1-p) 项退化),统一钳进
#: [0.01, 0.99] 防除零/防退化(契约 §2 A125)。
CLAMP_LO: float = 0.01
CLAMP_HI: float = 0.99

#: update/decide_sequence 可能返回的三种状态。
STATES: tuple[str, ...] = ("continue", "nsfw", "clean")


def _clamp_prob(p: object) -> float:
    """概率规整:转 float、拒 NaN、钳进 [CLAMP_LO, CLAMP_HI]。

    :raises ValueError: 非数值 / NaN 时(中文消息)。
    """
    try:
        q = float(p)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"nsfw_prob 必须是数字,当前值:{p!r}") from exc
    if math.isnan(q):
        raise ValueError("nsfw_prob 不能是 NaN")
    return min(max(q, CLAMP_LO), CLAMP_HI)


class SPRT:
    """伯努利序贯概率比检验器(Wald SPRT,软概率版)。

    每张图片的 ``nsfw_prob`` 被当作一次伯努利试验的期望取值,其对数似然比::

        LLR(p) = p*ln(p1/p0) + (1-p)*ln((1-p1)/(1-p0))

    逐张累加;判决边界::

        A = ln((1-β)/α)   # 上界:证据足够偏 NSFW
        B = ln(β/(1-α))   # 下界:证据足够偏 CLEAN

    - ``LLR ≥ A`` → ``"nsfw"``;``LLR ≤ B`` → ``"clean"``;否则 ``"continue"``;
    - **吸收态**:一旦越界终判,后续 :meth:`update` 直接返回该终态,不再
      消耗样本、不再移动 LLR(decide_sequence 同理立即返回);
    - ``p0``/``p1`` 默认取模块常量 :data:`P0`/:data:`P1`(0.5/0.9),
      可在构造时覆盖(如对称假设 p0=0.1, p1=0.9,证据更"锋利")。

    **校准假设**::meth:`update` 的输入应为**校准概率**(GLM calibrate 后
    近似);stub 未校准分数不满足假设,不建议在生产开启
    (``cfg.use_sprt`` 默认 False)。

    用法::

        s = SPRT()                       # alpha=0.05, beta=0.05
        for p in probs:                  # 逐张送审
            if s.update(p) != "continue":
                break                    # 早停:省下剩余 VLM 配额
        verdict, n_used = SPRT().decide_sequence(probs)
    """

    def __init__(
        self,
        alpha: float = 0.05,
        beta: float = 0.05,
        *,
        p0: float = P0,
        p1: float = P1,
    ) -> None:
        """以第一/二类错误率上限 alpha/beta 构造检验器。

        :param alpha: 第一类错误上限(把 CLEAN 站点误判 NSFW),∈(0, 0.5);
        :param beta:  第二类错误上限(把 NSFW 站点漏判 CLEAN),∈(0, 0.5);
        :param p0:    H0 单张色情概率,∈(0, 1) 且 < p1;
        :param p1:    H1 单张色情概率,∈(0, 1) 且 > p0;
        :raises ValueError: 参数越界时(中文消息,与 config 校验一致)。
        """
        a, b = float(alpha), float(beta)
        if not (0 < a < 0.5) or not (0 < b < 0.5):
            raise ValueError(
                f"alpha/beta={alpha}/{beta} 无效:错误率须在 (0, 0.5) 区间"
            )
        h0, h1 = float(p0), float(p1)
        if not (0 < h0 < 1) or not (0 < h1 < 1) or h0 >= h1:
            raise ValueError(
                f"p0/p1={p0}/{p1} 无效:均须在 (0, 1) 内且 p0 < p1"
            )
        self.alpha = a
        self.beta = b
        self.p0 = h0
        self.p1 = h1
        #: 上界 A = ln((1-β)/α):LLR ≥ A 判 nsfw。
        self.upper = math.log((1.0 - b) / a)
        #: 下界 B = ln(β/(1-α)):LLR ≤ B 判 clean。
        self.lower = math.log(b / (1.0 - a))
        #: 累计对数似然比(吸收后不再变化)。
        self.total_llr = 0.0
        #: 已消耗的送审样本数(吸收后不再增加)。
        self.n = 0
        self._state = "continue"

    # ------------------------------------------------------------------
    # 单步证据与状态
    # ------------------------------------------------------------------

    def llr(self, p: float) -> float:
        """单伯努利对数似然比 LLR(p)(先钳进 [0.01, 0.99])。

        p=0.5(与 H0 一致)给出负值、p=p1 给出正值是本参数化的固有形状;
        LLR 恒随 p 单调递增,零点位于 p≈0.73(介于 p0 与 p1 之间)。
        """
        q = _clamp_prob(p)
        return q * math.log(self.p1 / self.p0) + (1.0 - q) * math.log(
            (1.0 - self.p1) / (1.0 - self.p0)
        )

    def state(self) -> str:
        """当前状态("continue" / "nsfw" / "clean"),不消耗样本。"""
        return self._state

    def _decide(self) -> str:
        """按当前 total_llr 对照边界判决(≥A 为 nsfw、≤B 为 clean,含等值)。"""
        if self.total_llr >= self.upper:
            return "nsfw"
        if self.total_llr <= self.lower:
            return "clean"
        return "continue"

    def update(self, p: float) -> str:
        """送审一张图片(软概率 p),返回最新状态。

        边界穿越即终判;终判后本检验器进入**吸收态**:再 update 任何值都
        直接返回原终态,n / total_llr 均不变(证据已足,后续样本无意义)。
        """
        if self._state != "continue":
            return self._state  # 吸收态:直接返回终态
        self.total_llr += self.llr(p)
        self.n += 1
        self._state = self._decide()
        return self._state

    def decide_sequence(self, probs: Iterable[float]) -> tuple[str, int]:
        """逐张送审 probs,中途判停即返回 ``(verdict, n_used)``。

        :param probs: 送审概率序列(列表/生成器均可,逐个消费);
        :return: ``(终态或 "continue", 实际消耗的张数)``——序列用尽仍未
                 越界则 ``("continue", len(probs))``;已处于吸收态的实例
                 直接返回 ``(终态, 0)``(不再消耗任何样本)。
        """
        if self._state != "continue":
            return self._state, 0
        used = 0
        for p in probs:
            used += 1
            if self.update(p) != "continue":
                return self._state, used
        return "continue", used

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return (
            f"SPRT(alpha={self.alpha}, beta={self.beta}, p0={self.p0}, "
            f"p1={self.p1}, n={self.n}, total_llr={self.total_llr:.4f}, "
            f"state={self._state!r})"
        )


def next_images(
    candidates: list[ImageScore],
    cfg: Config,
    *,
    max_send: int | None = None,
) -> tuple[list[ImageScore], SPRT]:
    """按不确定度升序逐张"送审",SPRT 判停即截断(省预算演示语义)。

    排序复用 :func:`netsentinel.intel.active_learn.rank_for_vlm` 的语义
    (惰性导入兄弟模块,零外呼):只取 ``model == "ensemble"`` 条目,按
    ``|nsfw_prob - 0.5|`` 升序——最"边缘"的图片信息量最大,优先送审;
    高置信图片留到后面,往往还没轮到就已判停,配额即省下。

    每取一张就 ``update(nsfw_prob)`` 一次,返回 ``"nsfw"/"clean"`` 即停止
    取样,已送审列表到此截断;``"continue"`` 则继续,直至取满
    ``max_send``(默认 ``cfg.vlm_max_images_per_site``)。

    **真实接线说明**:本函数不调用任何 VLM——它假设候选分数已经算好,
    模拟"逐张送审、判停即截断"的**省预算演示/离线语义**;生产环境的真实
    送审(每张实际外呼 VLM 并用其返回概率 update)由
    ``pipeline/kernel_wire.py``(A139)装配。**校准假设**:输入概率须经
    校准(GLM calibrate 后近似),stub 未校准分数不满足 SPRT 担保,
    不建议开启(``cfg.use_sprt`` 默认 False,开关由调用方/装配线把关,
    本函数本身是无条件的纯排序+判停原语)。

    :param candidates: 图片评分列表(通常即 ``report.image_scores``);
    :param cfg:        配置(读 sprt_alpha / sprt_beta / vlm_max_images_per_site);
    :param max_send:   本次最多送审张数;None 取 cfg.vlm_max_images_per_site;
    :return: ``(实际送审的 ImageScore 列表, 该轮使用的 SPRT 实例)``——
             SPRT 实例携带终态/消耗张数,调用方可继续读取
             (``state()`` / ``n`` / ``total_llr``)。
    """
    from netsentinel.intel.active_learn import rank_for_vlm  # 惰性导入(契约 §2)

    budget = cfg.vlm_max_images_per_site if max_send is None else int(max_send)
    sprt = SPRT(cfg.sprt_alpha, cfg.sprt_beta)
    sent: list[ImageScore] = []
    if budget <= 0:
        return sent, sprt
    for score in rank_for_vlm(candidates, budget):
        sent.append(score)
        if sprt.update(float(score.nsfw_prob)) != "continue":
            break  # 判停即截断:后续图片不再"送审",预算省下
    return sent, sprt


def kernel_selfcheck() -> dict[str, Any]:
    """V7 内核自检(A138 kernel_bench 统一调用;确定性、零墙钟)。

    20 张强 clean(全 0.02)序列:SPRT 第 2 张即判停,送审数 2 vs
    无早停基线 20——以**操作计数**(送审张数)证明代差,非计时断言。
    """
    seq = [0.02] * 20
    verdict, n_used = SPRT(0.05, 0.05).decide_sequence(seq)
    return {
        "name": "sprt",
        "metric": "20 张强 clean 序列的 VLM 送审张数(早停)",
        "value": n_used,
        "baseline": len(seq),
        "verdict": verdict,
    }


# ===========================================================================
# e-process / anytime 置信序列内核(A160+,增量;上方的 Wald SPRT 不动)
#
# 数学对标:Ramdas, Grünwald, Vovk, Shafer (2023) "Game-theoretic statistics
# and safe anytime-valid inference" 综述的 e-process / 非负上鞅框架;
# Robbins 的 mixture-SPRT;Howard, Ramdas, McAuliffe, Sekhon (2021) 的
# 时间一致 Chernoff/stitching 置信序列。全部闭式、纯 math、零外呼。
# ===========================================================================

#: ln(最大有限 double)≈709.78:e 值 exp() 上限,超过即以 inf 报告(对数域
#: 累计天然防溢出,本常数只保护最后一步 exp)。
_LN_MAX_DOUBLE: float = math.log(1.7976931348623157e308)

#: 混合备择的默认先验:Beta(1/2, 1/2)(Jeffreys 无先验信息先验)。
JEFFREYS_A: float = 0.5
JEFFREYS_B: float = 0.5


def _as_float(value: object, name: str) -> float:
    """数值规整:转 float,非数值/NaN 抛中文 ValueError。"""
    try:
        q = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字,当前值:{value!r}") from exc
    if math.isnan(q):
        raise ValueError(f"{name} 不能是 NaN")
    return q


def log_evalue(
    s: float,
    n: float,
    *,
    p0: float = P0,
    prior_a: float = JEFFREYS_A,
    prior_b: float = JEFFREYS_B,
) -> float:
    """Robbins 混合 e-值的**对数**闭式(Beta-Binomial 边缘 ÷ 原似然)。

    以 Beta(``prior_a``, ``prior_b``)(默认 Jeffreys)先验把复合备择
    "p ~ Beta(a,b)" 积分掉,得混合似然比(e-值)::

        E_n = ∫₀¹ ∏ᵢ f_θ(xᵢ) dπ_{a,b}(θ) / ∏ᵢ f_{p0}(xᵢ)
            = [B(a+S, b+n−S) / B(a,b)] · p0^(−S) · (1−p0)^(−(n−S))

    其中 ``S`` 为 NSFW 计数(**软概率送审时取期望计数**——各张钳制后概率
    之和,与 Wald SPRT 对软概率取期望 LLR 的处理同构;硬 0/1 标签时严格
    回退为经典 Robbins mixture-SPRT)。对数域闭式::

        log E_n = lnB(a+S, b+n−S) − lnB(a, b)
                  − S·ln(p0) − (n−S)·ln(1−p0)

    全程 :func:`math.lgamma` / :func:`math.log1p`,无幂次连乘,**天然防
    上/下溢**(n 数万、S 极端时 log E 仍是有限数)。

    **鞅性质**(anytime-valid 的根):H0(真值 p=p0)下 ``E_n`` 是期望
    恒为 1 的非负上鞅——任意 θ 的似然比是鞅,Beta 混合(权重积分和为 1)
    由 Fubini 仍是鞅;故 Ville 不等式给 ``P(∃n: E_n ≥ 1/α) ≤ α``,
    **停时任意**也成立(这是对 Wald SPRT 固定边界逼近的关键升级)。

    :param s:   NSFW 期望计数 ∑pᵢ(钳制后),须在 [0, n] 内(±1e-9 容差,
                越界抛错防把非法聚合作 e-值用);
    :param n:   样本数,≥ 0;n=0 时 E₀=1(log 为 0);
    :param p0:  H0 的单张色情概率,∈(0, 1);
    :param prior_a/prior_b: 备择混合先验 Beta(a,b) 两参数,>0(默认 0.5/0.5);
    :return:    log E_n(自然对数;E_n<精度下限时为负大数,不炸);
    :raises ValueError: 参数越界/非数值时(中文消息)。
    """
    n_f = _as_float(n, "n")
    s_f = _as_float(s, "s")
    if n_f < 0.0:
        raise ValueError(f"n={n} 无效:须为 ≥0 的数")
    if not (-1e-9 <= s_f <= n_f + 1e-9):
        raise ValueError(f"s={s} 无效:须在 [0, n={n}] 内(期望计数不得越界)")
    s_c = min(max(s_f, 0.0), n_f)
    h0 = _as_float(p0, "p0")
    if not (0.0 < h0 < 1.0):
        raise ValueError(f"p0={p0} 无效:须在 (0, 1) 内")
    pa = _as_float(prior_a, "prior_a")
    pb = _as_float(prior_b, "prior_b")
    if not (math.isfinite(pa) and pa > 0.0) or not (
        math.isfinite(pb) and pb > 0.0
    ):
        raise ValueError(
            f"prior_a/prior_b={prior_a}/{prior_b} 无效:均须为正有限数"
        )
    if n_f == 0.0:
        return 0.0  # E₀ = 1(空积)
    # lnB(a+s, b+n−s) − lnB(a, b),全 lgamma,防大数幂连乘溢出。
    ln_marginal = (
        math.lgamma(pa + s_c)
        + math.lgamma(pb + n_f - s_c)
        - math.lgamma(pa + pb + n_f)
    ) - (math.lgamma(pa) + math.lgamma(pb) - math.lgamma(pa + pb))
    return ln_marginal - s_c * math.log(h0) - (n_f - s_c) * math.log1p(-h0)


class MixtureSPRT:
    """混合 e-process 序贯检验器(mixture-SPRT,anytime-valid)。

    与 :class:`SPRT`(Wald,简单假设 p0 vs p1)的差别:**备择是复合的**——
    "p 服从 Beta(a,b)"(默认 Jeffreys),检验统计量是上面积分掉的混合
    似然比 e-值 ``E_n``(:func:`log_evalue` 闭式,对数域累计防溢出)。

    - **判决**:``log E_n ≥ ln(1/α)`` 即拒绝 H0(p=p0,硬币面);方向由
      运行均值 ``s/n`` 读出——高于 p0 → ``"nsfw"``,低于(或恰等于,平局
      归 clean,保守不诬指)→ ``"clean"``;否则 ``"continue"``;
    - **anytime 担保**:``E_n`` 是 H0 下期望 1 的非负上鞅,Ville 不等式
      ⇒ ``P(∃n: E_n ≥ 1/α) ≤ α``——**不论何时停**、停时 unplanned 也
      成立(Wald SPRT 的 A/B 边界只是其渐近近似,且不抗 optional
      stopping 的连续 peeking;e-process 是 2020s 前沿的正解);
    - **吸收态**:与 :class:`SPRT` 同款——终判后 update 直接返回终态,
      ``n`` / ``s`` / ``total_log_e`` 全部冻结,decide_sequence 返回
      ``(终态, 0)``;
    - **校准假设**:与 Wald SPRT 相同——输入应为校准概率(GLM
      calibrate 后近似);stub 未校准分数不满足鞅假设,不建议生产开启。

    用法::

        m = MixtureSPRT(0.05)                 # Jeffreys 备择,p0=0.5
        for p in probs:
            if m.update(p) != "continue":
                break                          # anytime 早停
        verdict, n_used = MixtureSPRT(0.05).decide_sequence(probs)
    """

    def __init__(
        self,
        alpha: float = 0.05,
        *,
        p0: float = P0,
        prior_a: float = JEFFREYS_A,
        prior_b: float = JEFFREYS_B,
    ) -> None:
        """以第一类错误率上限 alpha 构造混合检验器(单侧 e 过 1/α)。

        :param alpha: 第一类错误上限(把 CLEAN 站点误拒 H0),∈(0, 0.5);
        :param p0:    H0 单张色情概率,∈(0, 1);
        :param prior_a/prior_b: 复合备择 Beta(a,b) 先验参数,>0;
        :raises ValueError: 参数越界时(中文消息)。
        """
        a = _as_float(alpha, "alpha")
        if not (0.0 < a < 0.5):
            raise ValueError(
                f"alpha={alpha} 无效:错误率须在 (0, 0.5) 区间"
            )
        h0 = _as_float(p0, "p0")
        if not (0.0 < h0 < 1.0):
            raise ValueError(f"p0={p0} 无效:须在 (0, 1) 内")
        pa = _as_float(prior_a, "prior_a")
        pb = _as_float(prior_b, "prior_b")
        if not (math.isfinite(pa) and pa > 0.0) or not (
            math.isfinite(pb) and pb > 0.0
        ):
            raise ValueError(
                f"prior_a/prior_b={prior_a}/{prior_b} 无效:均须为正有限数"
            )
        self.alpha = a
        self.p0 = h0
        self.prior_a = pa
        self.prior_b = pb
        #: 上界 ln(1/α):log E ≥ 此值即拒 H0(Ville 阈值,对标 Wald 的 A)。
        self.upper = -math.log(a)
        #: 已消耗样本数(吸收后不再增加)。
        self.n = 0
        #: NSFW 期望计数 ∑钳制后 pᵢ(吸收后冻结)。
        self.s = 0.0
        #: 累计 log e-值(吸收后冻结;E₀=1 ⇒ 初值 0)。
        self.total_log_e = 0.0
        self._state = "continue"

    # ------------------------------------------------------------------
    # 单步证据与状态
    # ------------------------------------------------------------------

    def log_evalue(self) -> float:
        """当前 log e-值(= :func:`log_evalue(self.s, self.n, ...)` 闭式)。"""
        return log_evalue(
            self.s,
            self.n,
            p0=self.p0,
            prior_a=self.prior_a,
            prior_b=self.prior_b,
        )

    def evalue(self) -> float:
        """当前 e-值 E_n = exp(total_log_e);对数域防溢出,超出 double
        上限时返回 ``inf`` 而非抛 OverflowError。"""
        if self.total_log_e > _LN_MAX_DOUBLE:
            return math.inf
        return math.exp(self.total_log_e)

    def state(self) -> str:
        """当前状态("continue" / "nsfw" / "clean"),不消耗样本。"""
        return self._state

    def _decide(self) -> str:
        """log E ≥ ln(1/α) 拒 H0,方向按 s/n vs p0(平局归 clean)。"""
        if self.n > 0 and self.total_log_e >= self.upper:
            return "nsfw" if self.s / self.n > self.p0 else "clean"
        return "continue"

    def update(self, p: float) -> str:
        """送审一张图片(软概率 p,先钳 [0.01,0.99]),返回最新状态。

        越过 Ville 阈值即终判;终判后进入**吸收态**:再 update 任何值都
        直接返回原终态,``n`` / ``s`` / ``total_log_e`` 均不变。
        """
        if self._state != "continue":
            return self._state  # 吸收态:直接返回终态
        q = _clamp_prob(p)
        self.n += 1
        self.s += q
        self.total_log_e = self.log_evalue()  # 闭式重算,无增量漂移
        self._state = self._decide()
        return self._state

    def decide_sequence(self, probs: Iterable[float]) -> tuple[str, int]:
        """逐张送审 probs,中途判停即返回 ``(verdict, n_used)``。

        语义与 :meth:`SPRT.decide_sequence` 完全一致:序列用尽仍未越界则
        ``("continue", len(probs))``;已吸收的实例直接 ``(终态, 0)``。
        """
        if self._state != "continue":
            return self._state, 0
        used = 0
        for p in probs:
            used += 1
            if self.update(p) != "continue":
                return self._state, used
        return "continue", used

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return (
            f"MixtureSPRT(alpha={self.alpha}, p0={self.p0}, "
            f"prior=Beta({self.prior_a}, {self.prior_b}), n={self.n}, "
            f"s={self.s:.4f}, total_log_e={self.total_log_e:.4f}, "
            f"state={self._state!r})"
        )


# ---------------------------------------------------------------------------
# anytime 置信序列(Howard 线性 stitching,Hoeffding-U 型)
# ---------------------------------------------------------------------------


def _as_positive_int(n: object) -> int:
    """规整为 ≥1 的整数,否则中文 ValueError。"""
    f = _as_float(n, "n")
    if f < 1.0 or f != int(f):
        raise ValueError(f"n={n} 无效:须为 ≥1 的整数(样本数)")
    return int(f)


def _as_delta(delta: object) -> float:
    """规整 δ ∈(0, 1),否则中文 ValueError。"""
    d = _as_float(delta, "delta")
    if not (0.0 < d < 1.0):
        raise ValueError(f"delta={delta} 无效:须在 (0, 1) 内")
    return d


def stitched_radius(n: int, delta: float = 0.05) -> float:
    """Howard 线性 stitching 半径 ε(n)(Hoeffding-U 型指数边界)。

    样本数按 2 的幂分段:第 k 段覆盖 ``n ∈ (2^(k−1), 2^k]``(k 从 0 起,
    即 n=1 | k=0;n=3,4 | k=2;……)。段内逐 n 做 union-Hoeffding、段间
    用几何权重 ``w_k = 2^(−(k−1))…2^(−1)``(Σw_k=1)分摊 δ::

        ε(n) = sqrt( ln(4·m_k/δ_k) / (2n) ),   m_k = 2^k ≥ n,
        δ_k = δ · w_k = δ · 2^(−(k+1))

    **覆盖率证明概要**(对 [0,1] 有界、均值 p 的 iid 观测——硬标签或
    校准软概率均满足):固定段 k 内任一 n,Hoeffding 得
    ``P(|S̄_n − p| ≥ ε(n)) ≤ 2e^(−2nε²) = δ_k/(2m_k)``;段内 union 至多
    m_k 个 n ⇒ 段失败率 ≤ δ_k/2;对所有段再 union,
    ``Σ_k δ_k/2 = δ/2 ≤ δ`` ⇒ **任意时刻** ``|S̄_n − p| ≤ ε(n)`` 同时
    成立的概率 ≥ 1−δ(时间一致置信序列,Ville 式担保的 CI 对偶)。
    (对标 Howard, Ramdas, McAuliffe, Sekhon 2021, "Time-uniform
    Chernoff confidence intervals", stitching 构造;此处取其对工程更
    透明的 Hoeffding+union 变体,常数略松、实现 20 行、纯 math。)

    :param n:     样本数,≥1 的整数;
    :param delta: 总误覆盖水平,∈(0, 1);
    :return:      半径 ε(n) ∈(0, +∞)(n=1、δ=0.05 时 ≈1.59,双倍宽于
                  固定 n Hoeffding——分段与 anytime 的代价);
    :raises ValueError: n/delta 非法时(中文消息)。
    """
    n_i = _as_positive_int(n)
    d = _as_delta(delta)
    k = (n_i - 1).bit_length()  # n=1→0;2→1;3,4→2;5..8→3;…
    m = float(1 << k)  # 段上界 m_k = 2^k ≥ n
    delta_k = d * (0.5 ** (k + 1))  # 段水平 δ_k = δ·2^{-(k+1)}
    return math.sqrt(math.log(4.0 * m / delta_k) / (2.0 * n_i))


def anytime_ci(n: int, s: float, delta: float = 0.05) -> tuple[float, float]:
    """任意时刻有效的站点 NSFW 率置信区间(stitching 半径版)。

    ``CI_n = [S̄_n − ε(n), S̄_n + ε(n)] ∩ [0, 1]``,其中 ε 取
    :func:`stitched_radius`;对全部 n ≥ 1 **同时**覆盖率 ≥ 1−δ。

    :param n:     样本数,≥1 的整数;
    :param s:     NSFW 期望计数 ∑pᵢ ∈ [0, n](硬标签即 NSFW 张数);
    :param delta: 总误覆盖水平,∈(0, 1);
    :raises ValueError: 参数非法时(中文消息;复用上游校验)。
    """
    n_i = _as_positive_int(n)
    d = _as_delta(delta)
    s_f = _as_float(s, "s")
    if not (-1e-9 <= s_f <= n_i + 1e-9):
        raise ValueError(f"s={s} 无效:须在 [0, n={n}] 内(期望计数不得越界)")
    s_c = min(max(s_f, 0.0), float(n_i))
    center = s_c / n_i
    radius = stitched_radius(n_i, d)
    return (max(0.0, center - radius), min(1.0, center + radius))


def confidence_sequence(
    probs: Iterable[float],
    delta: float = 0.05,
) -> list[tuple[float, float]]:
    """逐张送审 probs,返回**每一步**的 anytime 置信区间序列。

    便捷批处理形式(内部即 :class:`ConfidenceSequence` 流式累计):
    第 i 个元素是送审完前 i+1 张后的 ``CI_{i+1}``(:func:`anytime_ci`
    语义);全部区间**合起来**的覆盖率 ≥ 1−δ,任取一步单看亦然。

    :param probs: 送审概率序列(逐个钳进 [0.01, 0.99],与 SPRT 同规);
    :param delta: 总误覆盖水平,∈(0, 1);
    :raises ValueError: delta/probs 非法时(中文消息)。
    """
    cs = ConfidenceSequence(delta=delta)  # 顺带校验 delta
    return [cs.update(p) for p in probs]


class ConfidenceSequence:
    """流式 anytime 置信序列(站点 NSFW 率 p 的时间一致区间)。

    每张图片 :meth:`update` 一次(软概率钳进 [0.01, 0.99] 后累计期望
    计数),随时 :meth:`interval` 取**当前**区间——任意时刻、任意停时下
    真值 p 落在区间内的概率 ≥ 1−δ(:func:`stitched_radius` 的 Howard
    stitching 担保;校准假设同 SPRT:软概率应为真实频率的近似,硬 0/1
    标签则严格满足)。

    用法::

        cs = ConfidenceSequence(delta=0.05)
        for p in probs:
            lo, hi = cs.update(p)      # 返回更新后的区间
            if lo > 0.5: ...           # 任一时刻可安全判读
    """

    def __init__(self, delta: float = 0.05) -> None:
        """以总误覆盖水平 delta 构造(∈(0,1),默认 0.05)。"""
        #: 总误覆盖水平(全时段合计)。
        self.delta = _as_delta(delta)
        #: 已累计样本数。
        self.n = 0
        #: NSFW 期望计数 ∑钳制后 pᵢ。
        self.sum = 0.0

    def update(self, p: float) -> tuple[float, float]:
        """送审一张(软概率 p),返回更新后的当前区间 ``(lo, hi)``。"""
        q = _clamp_prob(p)
        self.n += 1
        self.sum += q
        return self.interval()

    def interval(self) -> tuple[float, float]:
        """当前区间;n=0 时为无信息区间 ``(0.0, 1.0)``。"""
        if self.n == 0:
            return (0.0, 1.0)
        return anytime_ci(self.n, self.sum, self.delta)

    def radius(self) -> float:
        """当前 stitching 半径 ε(n);n=0 时为 ``inf``(尚无任何收紧)。"""
        if self.n == 0:
            return math.inf
        return stitched_radius(self.n, self.delta)

    def mean(self) -> float:
        """运行均值 S̄_n = sum/n(区间中心,未钳 [0,1] 外)。"""
        if self.n == 0:
            raise ValueError("尚无样本,均值未定义(先 update)")
        return self.sum / self.n

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        lo, hi = self.interval()
        return (
            f"ConfidenceSequence(delta={self.delta}, n={self.n}, "
            f"sum={self.sum:.4f}, ci=({lo:.4f}, {hi:.4f}))"
        )


def _check_band(clean_below: object, nsfw_above: object) -> tuple[float, float]:
    """校验决策带 [clean_below, nsfw_above] ⊆ [0, 1] 且左 ≤ 右。"""
    cb = _as_float(clean_below, "clean_below")
    na = _as_float(nsfw_above, "nsfw_above")
    if not (0.0 <= cb <= 1.0) or not (0.0 <= na <= 1.0) or cb > na:
        raise ValueError(
            f"决策带 [{clean_below}, {nsfw_above}] 无效:"
            "须 0 ≤ clean_below ≤ nsfw_above ≤ 1"
        )
    return cb, na


def ci_band_stop(
    n: int,
    s: float,
    delta: float = 0.05,
    *,
    clean_below: float = P0,
    nsfw_above: float = P0,
) -> str:
    """anytime CI **整体离开决策带**即停的辅助判定(单步纯函数)。

    决策带 ``[clean_below, nsfw_above]`` 默认取退化点 ``[P0, P0]``
    =(0.5, 0.5)(硬币面阈值;加宽成带如 [0.3, 0.7] 即"灰区不下判"):

    - ``lo > nsfw_above``(整个区间在带上方)→ ``"nsfw"``;
    - ``hi < clean_below``(整个区间在带下方)→ ``"clean"``;
    - 区间与带相交 → ``"continue"``(证据不足以 anytime-安全下判)。

    与 :class:`SPRT` 三态同词表,可作管线里的**第二意见**/交叉判定;
    覆盖率担保承 :func:`anytime_ci`(任意时刻 ≥ 1−δ,含判停时刻)。

    :param n:    样本数,≥1 的整数(n<1 时调用方应自行视为 continue);
    :param s:    NSFW 期望计数 ∈ [0, n];
    :param delta: 总误覆盖水平,∈(0, 1);
    :raises ValueError: 参数非法时(中文消息)。
    """
    _check_band(clean_below, nsfw_above)
    lo, hi = anytime_ci(n, s, delta)
    if lo > nsfw_above:
        return "nsfw"
    if hi < clean_below:
        return "clean"
    return "continue"


def ci_band_stop_sequence(
    probs: Iterable[float],
    delta: float = 0.05,
    *,
    clean_below: float = P0,
    nsfw_above: float = P0,
    max_n: int | None = None,
) -> tuple[str, int]:
    """逐张送审,anytime CI 离开决策带即停,返回 ``(verdict, n_used)``。

    语义与 :meth:`SPRT.decide_sequence` 对齐:判停即截断(后续图片不再
    送审);序列用尽或取满 ``max_n`` 仍未离带则 ``("continue", 用量)``;
    无吸收态对象,但同样**零重复消耗**——返回即终判。

    :param probs: 送审概率序列(逐个钳 [0.01, 0.99]);
    :param max_n: 最多送审张数;None 不限(由 probs 长度决定);
    :raises ValueError: delta/决策带/probs 非法时(中文消息)。
    """
    _check_band(clean_below, nsfw_above)
    cs = ConfidenceSequence(delta=delta)  # 校验 delta
    limit = None if max_n is None else _as_positive_int(max_n)
    used = 0
    for p in probs:
        lo, hi = cs.update(p)  # _clamp_prob 校验/钳制
        used += 1
        if lo > nsfw_above:
            return "nsfw", used
        if hi < clean_below:
            return "clean", used
        if limit is not None and used >= limit:
            break
    return "continue", used
