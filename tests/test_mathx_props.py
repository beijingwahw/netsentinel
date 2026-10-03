# -*- coding: utf-8 -*-
"""数学内核性质测试(property-based testing,双引擎:hypothesis 优先 / stdlib 兜底)。

对标 2026 前沿的 property-based testing 实践:不为单个手算例子写断言,
而是为 :mod:`netsentinel.mathx` 的公开函数声明**数学不变量**,由测试
引擎批量生成输入去攻击;再以汉明距离的度量公理(非负 / 对称 / 三角
不等式)约束 :func:`netsentinel.vision.phash2.hamming_hex`(只读引用,
不改实现)。

双引擎纪律(红线:核心零必装依赖,hypothesis 仅 dev 可选):

- **hypothesis 引擎**:pyproject 的 dev extra 已声明 ``hypothesis>=6.100``;
  本机未安装时经由 :func:`pytest.importorskip` 的哨卫测试**显式记录
  跳过原因**(见 :func:`test_hypothesis_engine_available`),性质断言
  **不停摆**——立即降级到 stdlib 引擎;
- **stdlib 引擎**::class:`random.Random(42)` 固定种子,每条性质随机
  采样 200 组输入,失败可精确复现(种子写在测试里,不依赖墙钟/外部
  服务,符合红线 31 的可复现精神)。

浮点现实主义(预研发现的两处"文档口径 vs 浮点真相",断言按真相收紧):

- softmax 极端悬殊输入下 ``exp(v-peak)`` 下溢为 0.0,输出可含精确
  0.0——故值域断言用**闭区间** ``[0, 1]``(docstring 的 ``(0,1]``
  对下溢场景过度承诺);和恒为 1 用 fsum 容差;
- standardize 对"近邻浮点对"(间隔仅若干 ulp)存在均值舍入伪影,
  输出均值可偏离 0 达 O(1)——不变量仅在**良分离**输入上断言
  (:func:`_well_spread` 判定),常数列另走 σ=0 → 全 0 的确定性分支。
"""
from __future__ import annotations

import math
import random

import pytest

from netsentinel.mathx import clamp, dot, matmul, matmul_ops, sigmoid, softmax, standardize
from netsentinel.vision.phash2 import hamming_hex

# ---------------------------------------------------------------------------
# 引擎选择:hypothesis 可用则用,不可用则 stdlib 兜底(核心零必装依赖)
# ---------------------------------------------------------------------------

HAVE_HYPOTHESIS = False
try:  # pragma: no cover - 两个分支在不同环境各跑一次,覆盖统计无意义
    import hypothesis as _hypothesis
    from hypothesis import assume, given, settings
    from hypothesis import strategies as st

    HAVE_HYPOTHESIS = True
except ImportError:
    pass

#: stdlib 兜底引擎每条性质的随机采样组数(种子固定,结果可复现)。
_N_FALLBACK = 200
#: stdlib 引擎的固定种子(测试名即文档:Random(42))。
_SEED = 42


def test_hypothesis_engine_available() -> None:
    """hypothesis 引擎哨卫:可用则记录版本并放行;缺失则 importorskip 显式跳过。

  跳过不等于停摆:全部性质断言已由同文件的 stdlib ``Random(42)``
  兜底路径接管(见各 ``test_*`` 的 else 分支)。
    """
    pytest.importorskip(
        "hypothesis",
        reason="hypothesis 未安装(仅 dev extra 可选,核心零必装依赖);"
        "性质断言已由 stdlib Random(42) 兜底引擎接管",
    )
    assert _hypothesis.__version__  # pragma: no cover - 装了 hypothesis 才走到这


# ---------------------------------------------------------------------------
# 性质检查器(纯断言,与引擎无关:hypothesis 与 stdlib 共用同一份不变量)
# ---------------------------------------------------------------------------


def _well_spread(vals: list[float]) -> bool:
    """输入是否"良分离":极差 ≥ 量级的 1e-6 倍(近邻浮点对有舍入伪影,不适用)。

    standardize 的两遍算法里 ``(a+b)/2`` 可舍入到端点,使间隔仅数个 ulp
    的近邻对输出均值偏离 0 达 O(1)——这是浮点本性而非实现缺陷;均值≈0 /
    σ≈1 的不变量只在良分离输入上具备数学保证。
    """
    if len(vals) < 2:
        return False
    spread = max(vals) - min(vals)
    scale = max(1.0, max(abs(v) for v in vals))
    return spread >= 1e-6 * scale


def _check_softmax(xs: list[float]) -> None:
    """softmax 不变量:和恒为 1(fsum 容差)、逐项落在 [0, 1]、最大项得分最高。"""
    out = softmax(xs)
    assert len(out) == len(xs), "softmax 必须保长"
    assert all(0.0 <= p <= 1.0 for p in out), f"softmax 输出越出 [0,1]:{out!r}"
    assert math.isclose(math.fsum(out), 1.0, rel_tol=1e-9, abs_tol=1e-12), (
        f"softmax 概率和 != 1:fsum={math.fsum(out)!r},输入={xs!r}"
    )
    # 最大输入对应输出中的最大概率(减最大值稳定的直接推论)
    assert out[max(range(len(xs)), key=xs.__getitem__)] == max(out)


def _check_sigmoid(x: float, y: float) -> None:
    """sigmoid 不变量:值域 [0,1]、中心对称 σ(-x)=1-σ(x)、单调不减。"""
    sx, sy = sigmoid(x), sigmoid(y)
    for s, v in ((sx, x), (sy, y)):
        assert 0.0 <= s <= 1.0, f"sigmoid({v!r}) = {s!r} 越出 [0,1]"
        assert math.isclose(sigmoid(-v) + s, 1.0, rel_tol=1e-12, abs_tol=1e-12), (
            f"中心对称破损:sigmoid({-v!r}) + sigmoid({v!r}) = "
            f"{sigmoid(-v) + s!r} != 1"
        )
    if x < y:
        assert sx <= sy, f"单调性破损:sigmoid({x!r})={sx!r} > sigmoid({y!r})={sy!r}"


def _check_standardize(xs: list[float]) -> None:
    """standardize 不变量:保长;常数列 → 全 0;良分离列 → 均值≈0、总体 σ≈1。"""
    out = standardize(xs)
    assert len(out) == len(xs), "standardize 必须保长"
    if max(xs) == min(xs):
        assert out == [0.0] * len(xs), f"常数列应返回全 0,实得 {out!r}"
        return
    if not _well_spread(xs):
        return  # 近邻浮点对存在均值舍入伪影,均值/σ 不变量不适用(见模块 docstring)
    mean = math.fsum(out) / len(out)
    sigma = math.sqrt(math.fsum((o - mean) ** 2 for o in out) / len(out))
    assert abs(mean) <= 1e-6, f"标准化后均值 {mean!r} 偏离 0(输入 {xs!r})"
    assert math.isclose(sigma, 1.0, rel_tol=1e-6), (
        f"标准化后总体 σ {sigma!r} 偏离 1(输入 {xs!r})"
    )


def _check_matmul_associativity(
    a: list[list[int]], b: list[list[int]], c: list[list[int]]
) -> None:
    """矩阵乘结合律 (A·B)·C == A·(B·C);int 输入下精确相等,无容差。"""
    left = matmul(matmul(a, b), c)
    right = matmul(a, matmul(b, c))
    assert left == right, (
        f"结合律破损:\n(A·B)·C={left!r}\nA·(B·C)={right!r}\nA={a!r} B={b!r} C={c!r}"
    )
    # dot 的二维·二维分支与 matmul 严格同构(契约承诺,顺带守恒)
    assert dot(a, b) == matmul(a, b)


def _check_clamp_idempotent(xs: list[float], lo: float, hi: float) -> None:
    """clamp 不变量:幂等 clamp∘clamp == clamp,且输出全部落进 [lo, hi]。"""
    once = clamp(xs, lo, hi)
    twice = clamp(once, lo, hi)
    assert once == twice, f"clamp 幂等破损:一次 {once!r} != 两次 {twice!r}"
    assert all(lo <= v <= hi for v in once), f"clamp 越界:{once!r} ∉ [{lo!r}, {hi!r}]"


def _check_hamming_metric(a: str, b: str, c: str) -> None:
    """hamming_hex 度量公理:非负、自距为 0、对称、三角不等式。"""
    assert hamming_hex(a, a) == 0
    d_ab, d_ba = hamming_hex(a, b), hamming_hex(b, a)
    assert d_ab >= 0, "汉明距离必须非负"
    assert d_ab == d_ba, f"对称性破损:d(a,b)={d_ab} != d(b,a)={d_ba}"
    assert hamming_hex(a, c) <= d_ab + hamming_hex(b, c), (
        f"三角不等式破损:d(a,c)={hamming_hex(a, c)} > "
        f"d(a,b)={d_ab} + d(b,c)={hamming_hex(b, c)}"
    )


# ---------------------------------------------------------------------------
# 输入生成器:stdlib 引擎(Random(42) 固定种子)
# ---------------------------------------------------------------------------


def _gen_softmax_input(rng: random.Random) -> list[float]:
    n = rng.randint(1, 24)
    return [rng.uniform(-1000.0, 1000.0) for _ in range(n)]


def _gen_sigmoid_pair(rng: random.Random) -> tuple[float, float]:
    x = rng.uniform(-1000.0, 1000.0)
    y = x + rng.uniform(0.0, 50.0)  # y >= x:顺带覆盖单调不减
    return x, y


def _gen_standardize_input(rng: random.Random) -> list[float]:
    n = rng.randint(2, 32)
    style = rng.random()
    if style < 0.5:  # 等差数列:确定性良分离
        base = rng.uniform(-1000.0, 1000.0)
        step = rng.choice([-1.0, 1.0]) * 10.0 ** rng.randint(-3, 3)
        return [base + i * step for i in range(n)]
    # 高斯抖动:宽分离(近邻对由 _well_spread 自然滤除)
    center = rng.uniform(-1000.0, 1000.0)
    scale = 10.0 ** rng.randint(-3, 3)
    return [center + rng.gauss(0.0, scale) for _ in range(n)]


def _gen_matrix_triple(
    rng: random.Random,
) -> tuple[list[list[int]], list[list[int]], list[list[int]]]:
    m, k, p, q = (rng.randint(1, 4) for _ in range(4))
    mk = lambda rows, cols: [[rng.randint(-9, 9) for _ in range(cols)] for _ in range(rows)]
    return mk(m, k), mk(k, p), mk(p, q)


def _gen_clamp_input(rng: random.Random) -> tuple[list[float], float, float]:
    xs = [rng.uniform(-10.0, 10.0) for _ in range(rng.randint(1, 16))]
    lo, hi = sorted((rng.uniform(-5.0, 5.0), rng.uniform(-5.0, 5.0)))
    return xs, lo, hi


_HEX_ALPHABET = "0123456789abcdef"


def _gen_hex(rng: random.Random, width: int) -> str:
    return "".join(rng.choice(_HEX_ALPHABET) for _ in range(width))


# ---------------------------------------------------------------------------
# hypothesis 引擎的策略与运行器(仅 hypothesis 可用时定义)
# ---------------------------------------------------------------------------

if HAVE_HYPOTHESIS:

    @st.composite
    def _mat_triple(draw):  # pragma: no cover - 由下方运行器统一执行
        m, k, p, q = (draw(st.integers(1, 4)) for _ in range(4))
        mat = lambda rows, cols: [
            [draw(st.integers(-9, 9)) for _ in range(cols)] for _ in range(rows)
        ]
        return mat(m, k), mat(k, p), mat(p, q)

    _finite_floats = st.floats(-1e12, 1e12, allow_nan=False, allow_infinity=False)

    @settings(max_examples=150, deadline=None)
    def _soft(xs: list[float]) -> None:
        _check_softmax(xs)

    @settings(max_examples=150, deadline=None)
    def _sig(x: float, y: float) -> None:
        _check_sigmoid(x, y)

    @settings(max_examples=150, deadline=None)
    def _std(xs: list[float]) -> None:
        _check_standardize(xs)

    @settings(max_examples=60, deadline=None)
    def _assoc(triple) -> None:
        _check_matmul_associativity(*triple)

    @settings(max_examples=150, deadline=None)
    def _clamp(xs: list[float], lo: float, hi: float) -> None:
        assume(lo <= hi)
        _check_clamp_idempotent(xs, lo, hi)

    @settings(max_examples=100, deadline=None)
    def _hamming(a: str, b: str, c: str) -> None:
        _check_hamming_metric(a, b, c)

    _run_softmax = given(st.lists(st.floats(allow_nan=False, allow_infinity=False), min_size=1, max_size=32))(_soft)
    _run_sigmoid = given(
        st.floats(allow_nan=False, allow_infinity=False),
        st.floats(allow_nan=False, allow_infinity=False),
    )(_sig)
    _run_standardize = given(st.lists(_finite_floats, min_size=2, max_size=32))(_std)
    _run_associativity = given(_mat_triple())(_assoc)
    _run_clamp = given(
        st.lists(_finite_floats, min_size=1, max_size=16),
        st.floats(-5.0, 5.0, allow_nan=False, allow_infinity=False),
        st.floats(-5.0, 5.0, allow_nan=False, allow_infinity=False),
    )(_clamp)
    _hex64 = st.text(_HEX_ALPHABET, min_size=64, max_size=64)
    _run_hamming = given(_hex64, _hex64, _hex64)(_hamming)


# ---------------------------------------------------------------------------
# 测试:hypothesis 优先,缺失则 stdlib Random(42) 兜底(同一份不变量)
# ---------------------------------------------------------------------------


def test_softmax_sums_to_one() -> None:
    """性质:softmax 输出构成概率分布(和恒为 1、值域 [0,1]、保序)。"""
    if HAVE_HYPOTHESIS:
        _run_softmax()
        return
    rng = random.Random(_SEED)
    for _ in range(_N_FALLBACK):
        _check_softmax(_gen_softmax_input(rng))


def test_sigmoid_range_symmetry_monotone() -> None:
    """性质:sigmoid 值域 [0,1]、中心对称 σ(-x) = 1 - σ(x)、单调不减。"""
    if HAVE_HYPOTHESIS:
        _run_sigmoid()
        return
    rng = random.Random(_SEED)
    for _ in range(_N_FALLBACK):
        _check_sigmoid(*_gen_sigmoid_pair(rng))


def test_standardize_zero_mean_unit_std() -> None:
    """性质:standardize 良分离输入 → 均值≈0、总体 σ≈1;常数列 → 全 0。"""
    if HAVE_HYPOTHESIS:
        _run_standardize()
    else:
        rng = random.Random(_SEED)
        for _ in range(_N_FALLBACK):
            _check_standardize(_gen_standardize_input(rng))
    # 确定性锚点(两引擎通吃):手算可核
    _check_standardize([5.0, 5.0, 5.0])        # 常数列 → 全 0
    _check_standardize([1.0, 2.0, 3.0, 4.0])   # 等差 → [-1.34..., 0.0, ...]


def test_matmul_dot_associativity() -> None:
    """性质:matmul 结合律 (A·B)·C == A·(B·C);dot 二维分支与 matmul 同构。"""
    if HAVE_HYPOTHESIS:
        _run_associativity()
        return
    rng = random.Random(_SEED)
    for _ in range(_N_FALLBACK):
        _check_matmul_associativity(*_gen_matrix_triple(rng))


def test_clamp_idempotent() -> None:
    """性质:clamp 幂等(clamp∘clamp == clamp)且输出夹进 [lo, hi]。"""
    if HAVE_HYPOTHESIS:
        _run_clamp()
        return
    rng = random.Random(_SEED)
    for _ in range(_N_FALLBACK):
        _check_clamp_idempotent(*_gen_clamp_input(rng))


def test_phash2_hamming_is_metric() -> None:
    """性质:phash2.hamming_hex 满足度量公理(非负/自距 0/对称/三角不等式)。

    256bit(64 hex)与 64bit(16 hex)两种宽度各测一遍;hamming_hex 为
    纯函数(不触 Pillow / 文件 IO),导入 phash2 安全。
    """
    if HAVE_HYPOTHESIS:
        _run_hamming()
    else:
        rng = random.Random(_SEED)
        for width in (64, 16):
            for _ in range(_N_FALLBACK // 2):
                _check_hamming_metric(
                    _gen_hex(rng, width), _gen_hex(rng, width), _gen_hex(rng, width)
                )
    # 确定性锚点:同串距 0、对全零串的距离 = 逐字符置位计数之和、长度不匹配抛中文 ValueError
    h = _gen_hex(random.Random(_SEED), 64)
    assert hamming_hex(h, h) == 0
    assert hamming_hex(h, "0" * 64) == sum(bin(int(ch, 16)).count("1") for ch in h)
    with pytest.raises(ValueError, match="长度不一致"):
        hamming_hex("0" * 64, "0" * 16)


def test_matmul_ops_identity_exact() -> None:
    """性质补充:matmul_ops 恒等式 ops(m,k,n) == m*k*n(操作计数口径)。"""
    rng = random.Random(_SEED)
    for _ in range(50):
        m, k, n = (rng.randint(0, 32) for _ in range(3))
        assert matmul_ops(m, k, n) == m * k * n
    assert matmul_ops(64, 64, 64) == 262_144
