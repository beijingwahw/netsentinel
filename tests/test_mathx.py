"""推理内核·张量微库测试(V7 · A133;见 CONTRACTS-V7.md §2 / 红线 29、31)。

覆盖::

- ``dot``:1D·1D / 2D·1D / 1D·2D / 2D·2D 手算精确对照、非共形与
  非矩形中文 ``ValueError``、空输入 / 混杂元素类型拒绝;
- ``matmul``:2×3·3×2 手算精确、单位阵恒等(seeded 随机阵)、
  1×1、非共形、左/右非矩形、一维拒绝、空矩阵拒绝;
- ``softmax``:和为 1 + 单调、手算精确、±1000 大数值稳定不溢出、
  空输入报错;
- ``sigmoid``:0 → 0.5、与 ``math.exp`` 互证、奇对称、±1000 稳定;
- ``standardize``:均值 0 / 总体 σ=1 手算互证、常数列 σ=0 → 全 0、
  空输入报错;
- ``clamp``:越界夹取 / 区间内原样、lo > hi 报错;
- 生成器等任意可迭代入参;
- ``matmul_ops``:m·k·n 公式与负数 / 非整数拒绝;
- **红线 31 基准**::func:`test_v7_bench_matmul_op_count` 以
  64×64×64 操作计数断言 == 262144(公式 + 全 1 矩阵元素总和 +
  float 子类替身元素逐次计数内部乘法三路互证),并以 seeded 随机
  阵 × 单位阵的精确恒等互证结果正确性;
- ``kernel_selfcheck``:确定性(两次调用逐字节一致);
- **A199 双后端**:探测式 numpy 加速(可选 extra ``fast``)——
  dot/matmul/softmax/standardize 在 numpy / stdlib 两路径下逐值
  一致(容差 1e-12)、小输入与子类替身 / 超大整数恒走 stdlib 的
  分支探针断言、同形状操作计数双路径统一(红线 31)、
  kernel_selfcheck 双后端哨卫、numpy 后端 500×500 matmul 性能
  冒烟(< 1s 宽松上界);numpy 缺席时相关断言 importorskip 显式跳过。

全部测试离线,不发起任何网络请求;除性能冒烟一条(显式允许墙钟的
宽松上界)外,全部断言不依赖墙钟。
"""
from __future__ import annotations

import importlib.util
import math
import random
import time

import pytest

from netsentinel import mathx as mathx_module
from netsentinel.mathx import (
    HAS_NUMPY,
    clamp,
    dot,
    kernel_selfcheck,
    matmul,
    matmul_ops,
    sigmoid,
    softmax,
    standardize,
)

# ---------------------------------------------------------------------------
# dot:手算精确对照与维度校验
# ---------------------------------------------------------------------------


def test_dot_1d_1d_hand_exact() -> None:
    """一维点积手算精确对照(int 整值结果可 == 精确比较)。"""
    assert dot([1, 2, 3], [4, 5, 6]) == 32
    assert dot([-1, 2, 0], [3, 0, 5]) == -3
    assert dot([1.5, 2.5], [4.0, 4.0]) == pytest.approx(16.0)


def test_dot_2d_1d_hand_exact() -> None:
    """矩阵-向量手算精确:[[1,2],[3,4]]·[5,6] = [17, 39];非方阵亦然。"""
    assert dot([[1, 2], [3, 4]], [5, 6]) == [17, 39]
    assert dot([[1, 2, 3], [4, 5, 6]], [1, 1, 1]) == [6, 15]


def test_dot_1d_2d_hand_exact() -> None:
    """向量-矩阵手算精确:[1,2]·[[3,4],[5,6]] = [1·3+2·5, 1·4+2·6] = [13, 16]。"""
    assert dot([1, 2], [[3, 4], [5, 6]]) == [13, 16]


def test_dot_2d_2d_matches_matmul_hand() -> None:
    """二维·二维与 matmul 同构,并与手算 [[19,22],[43,50]] 精确一致。"""
    a, b = [[1, 2], [3, 4]], [[5, 6], [7, 8]]
    assert dot(a, b) == matmul(a, b) == [[19, 22], [43, 50]]


def test_dot_1d_1d_length_mismatch_raises() -> None:
    """向量点积长度不等 → 中文 ValueError(不共形)。"""
    with pytest.raises(ValueError, match="不共形"):
        dot([1, 2], [1, 2, 3])


def test_dot_2d_1d_mismatch_raises() -> None:
    """矩阵列数 ≠ 向量长度 → 中文 ValueError。"""
    with pytest.raises(ValueError, match="不共形"):
        dot([[1, 2], [3, 4]], [1, 2, 3])


def test_dot_1d_2d_mismatch_raises() -> None:
    """向量长度 ≠ 矩阵行数 → 中文 ValueError。"""
    with pytest.raises(ValueError, match="不共形"):
        dot([1, 2, 3], [[1, 2], [3, 4]])


def test_dot_2d_2d_nonconformal_raises() -> None:
    """2×3 · 2×2 列行不共形 → 中文 ValueError。"""
    with pytest.raises(ValueError, match="不共形"):
        dot([[1, 2, 3]], [[1, 2], [3, 4]])


def test_dot_ragged_matrix_raises() -> None:
    """dot 入参非矩形([[1,2],[3]])→ 中文 ValueError。"""
    with pytest.raises(ValueError, match="非矩形"):
        dot([[1, 2], [3]], [1, 2])


def test_dot_mixed_element_types_raises() -> None:
    """一维向量混入非数值元素([1, [2]])→ 中文 ValueError。"""
    with pytest.raises(ValueError, match="不是数值|须全为数值|全为数值"):
        dot([1, [2]], [1, 2])


def test_dot_empty_input_raises() -> None:
    """空输入 → 中文 ValueError。"""
    with pytest.raises(ValueError, match="不能为空"):
        dot([], [])


# ---------------------------------------------------------------------------
# matmul:手算精确、恒等、1×1 与结构校验
# ---------------------------------------------------------------------------


def test_matmul_2x3_3x2_hand_exact() -> None:
    """2×3 · 3×2 手算逐元素精确对照。"""
    a = [[1, 2, 3], [4, 5, 6]]
    b = [[7, 8], [9, 10], [11, 12]]
    assert matmul(a, b) == [[58, 64], [139, 154]]


def test_matmul_identity_exact() -> None:
    """seeded 随机 5×5 阵 × 单位阵 = 原阵(逐元素精确,乘 0/1 无舍入)。"""
    rng = random.Random(1_331_33)
    r5 = [[rng.uniform(-1.0, 1.0) for _ in range(5)] for _ in range(5)]
    i5 = [[1.0 if i == j else 0.0 for j in range(5)] for i in range(5)]
    assert matmul(r5, i5) == r5
    assert matmul(i5, r5) == r5


def test_matmul_1x1_hand_exact() -> None:
    """1×1 退化情形:[[3]]·[[-7]] = [[-21]]。"""
    assert matmul([[3]], [[-7]]) == [[-21]]


def test_matmul_nonconformal_raises() -> None:
    """2×3 · 2×2 非共形 → 中文 ValueError。"""
    with pytest.raises(ValueError, match="不共形"):
        matmul([[1, 2, 3], [4, 5, 6]], [[1, 2], [3, 4]])


def test_matmul_ragged_left_raises() -> None:
    """左阵非矩形([[1,2],[3]])→ 中文 ValueError。"""
    with pytest.raises(ValueError, match="非矩形"):
        matmul([[1, 2], [3]], [[1, 2], [3, 4]])


def test_matmul_ragged_right_raises() -> None:
    """右阵非矩形([[1,2],[3]])→ 中文 ValueError。"""
    with pytest.raises(ValueError, match="非矩形"):
        matmul([[1, 2], [3, 4]], [[1, 2], [3]])


def test_matmul_rejects_1d_operands() -> None:
    """matmul 拒绝一维向量(提示改用 dot)。"""
    with pytest.raises(ValueError, match="二维"):
        matmul([1, 2], [[3, 4], [5, 6]])


def test_matmul_empty_matrix_raises() -> None:
    """空矩阵 → 中文 ValueError。"""
    with pytest.raises(ValueError, match="不能为空"):
        matmul([], [])


def test_matmul_zero_width_row_raises() -> None:
    """零宽行([[]])→ 中文 ValueError(空行)。"""
    with pytest.raises(ValueError, match="空行"):
        matmul([[]], [[1]])


# ---------------------------------------------------------------------------
# softmax:和为 1、单调、大数值稳定
# ---------------------------------------------------------------------------


def test_softmax_sums_to_one_and_monotone() -> None:
    """常规输入:各元素 ∈ (0,1)、和为 1(浮点容差)、保序单调。"""
    out = softmax([0.1, 1.0, 2.0, 5.0])
    assert all(0.0 < p < 1.0 for p in out)
    assert math.fsum(out) == pytest.approx(1.0)
    assert out == sorted(out)


def test_softmax_hand_exact() -> None:
    """手算精确:softmax([0,0]) = [0.5,0.5];softmax([0]) = [1.0]。"""
    assert softmax([0, 0]) == [0.5, 0.5]
    assert softmax([0.0]) == [1.0]


def test_softmax_large_values_stable() -> None:
    """大数值 1000 不溢出:等值 → 均分;悬殊 → [1.0, 0.0] 且和为 1。"""
    out = softmax([1000.0, 1000.0])
    assert out == [0.5, 0.5]
    dominant = softmax([1000.0, 0.0])
    assert all(math.isfinite(p) for p in dominant)
    assert dominant[0] == pytest.approx(1.0)
    assert dominant[1] == pytest.approx(0.0, abs=1e-300)
    assert math.fsum(dominant) == pytest.approx(1.0)


def test_softmax_extreme_negative_stable() -> None:
    """全负大数(-1000)经减最大值后同样稳定 → [0.5, 0.5]。"""
    assert softmax([-1000.0, -1000.0]) == [0.5, 0.5]


def test_softmax_empty_raises() -> None:
    """空输入 → 中文 ValueError(和为 1 的不变量无从成立)。"""
    with pytest.raises(ValueError, match="不能为空"):
        softmax([])


# ---------------------------------------------------------------------------
# sigmoid:中心值、互证、对称、极端稳定
# ---------------------------------------------------------------------------


def test_sigmoid_center_and_exp_crosscheck() -> None:
    """sigmoid(0) = 0.5 精确;与 math.exp 手工公式互证。"""
    assert sigmoid(0) == 0.5
    assert sigmoid(1.0) == pytest.approx(1.0 / (1.0 + math.exp(-1.0)))
    assert sigmoid(-2.5) == pytest.approx(math.exp(-2.5) / (1.0 + math.exp(-2.5)))


def test_sigmoid_symmetry() -> None:
    """奇对称:sigmoid(x) + sigmoid(-x) = 1。"""
    for x in (-5.0, -0.3, 0.7, 3.0):
        assert sigmoid(x) + sigmoid(-x) == pytest.approx(1.0)


def test_sigmoid_extreme_values_stable() -> None:
    """±1000 双分支稳定:恰为 1.0 / 0.0,绝不 OverflowError,且单调。"""
    assert sigmoid(1000.0) == 1.0
    assert sigmoid(-1000.0) == 0.0
    assert sigmoid(-100.0) < sigmoid(0.0) < sigmoid(100.0)


def test_sigmoid_non_numeric_raises() -> None:
    """非数值输入 → 中文 ValueError。"""
    with pytest.raises(ValueError, match="数值"):
        sigmoid("0")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# standardize:均值 0、σ=1、常数列
# ---------------------------------------------------------------------------


def test_standardize_mean_zero_std_one() -> None:
    """z-score 后均值 ≈ 0、总体标准差 ≈ 1;与手算公式逐项互证。"""
    data = [1.0, 2.0, 3.0, 4.0]
    out = standardize(data)
    assert math.fsum(out) == pytest.approx(0.0, abs=1e-12)
    mean = math.fsum(data) / len(data)
    sigma = math.sqrt(math.fsum((v - mean) ** 2 for v in data) / len(data))
    assert out == pytest.approx([(v - mean) / sigma for v in data])
    out_std = math.sqrt(math.fsum(v * v for v in out) / len(out))
    assert out_std == pytest.approx(1.0)


def test_standardize_constant_returns_zeros() -> None:
    """σ = 0(常数列)→ 全 0,而非除零 NaN。"""
    assert standardize([7, 7, 7, 7]) == [0.0, 0.0, 0.0, 0.0]


def test_standardize_empty_raises() -> None:
    """空输入 → 中文 ValueError。"""
    with pytest.raises(ValueError, match="不能为空"):
        standardize([])


# ---------------------------------------------------------------------------
# clamp:夹取语义
# ---------------------------------------------------------------------------


def test_clamp_basic() -> None:
    """低于下界取 lo、高于上界取 hi、区间内原样保留。"""
    assert clamp([-2, 0.5, 9], 0, 1) == [0, 0.5, 1]
    assert clamp([-3.5, -1.0], -2.0, -1.0) == [-2.0, -1.0]


def test_clamp_in_range_untouched() -> None:
    """全部落在区间内 → 值与类型原样返回(int 不被强转 float)。"""
    vals = [1, 2, 3]
    assert clamp(vals, 0, 10) == [1, 2, 3]
    assert all(isinstance(v, int) for v in clamp(vals, 0, 10))


def test_clamp_lo_greater_than_hi_raises() -> None:
    """lo > hi → 中文 ValueError。"""
    with pytest.raises(ValueError, match="下界不能大于上界"):
        clamp([1.0], 2.0, 1.0)


# ---------------------------------------------------------------------------
# 任意可迭代入参(生成器实现约定)
# ---------------------------------------------------------------------------


def test_iterable_generator_inputs_supported() -> None:
    """softmax / standardize / clamp 接受生成器等任意可迭代入参。"""
    gen = (float(i) for i in range(4))
    assert math.fsum(softmax(gen)) == pytest.approx(1.0)
    out = standardize(x * x for x in (-1, 0, 1))
    assert math.fsum(out) == pytest.approx(0.0, abs=1e-12)
    assert clamp((v for v in [-1.0, 0.5, 2.0]), 0.0, 1.0) == [0.0, 0.5, 1.0]


def test_matmul_accepts_tuple_rows() -> None:
    """矩阵行可为 tuple(嵌套 Sequence 皆可),结果与 list 入参一致。"""
    assert matmul(((1, 2), (3, 4)), ((5, 6), (7, 8))) == [[19, 22], [43, 50]]


# ---------------------------------------------------------------------------
# matmul_ops:操作计数公式
# ---------------------------------------------------------------------------


def test_matmul_ops_formula() -> None:
    """matmul_ops(m,k,n) == m·k·n:含 64³ = 262144 基准口径。"""
    assert matmul_ops(2, 3, 2) == 12
    assert matmul_ops(1, 1, 1) == 1
    assert matmul_ops(64, 64, 64) == 262144 == 64**3


def test_matmul_ops_rejects_invalid_shapes() -> None:
    """负数 / 非整数维度 → 中文 ValueError。"""
    with pytest.raises(ValueError, match="非负整数"):
        matmul_ops(-1, 2, 3)
    with pytest.raises(ValueError, match="非负整数"):
        matmul_ops(2, "3", 4)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 红线 31 基准:64×64×64 操作计数 == 262144(禁墙钟,离线可复现)
# ---------------------------------------------------------------------------


class _CountingFloat(float):
    """乘法计数浮点替身:matmul 元素原样透传,精确记录内循环乘法次数。

    matmul 的每次乘加恰发生一次 ``x * y``;替身元素经 zip 转置原样
    进入运算路径(实现承诺零拷贝转换),故 ``__mul__`` 调用总数即
    内循环乘法操作计数。
    """

    mul_count = 0

    def __mul__(self, other: object) -> "_CountingFloat":
        _CountingFloat.mul_count += 1
        return _CountingFloat(float(self) * float(other))  # type: ignore[arg-type]

    def __rmul__(self, other: object) -> "_CountingFloat":
        _CountingFloat.mul_count += 1
        return _CountingFloat(float(other) * float(self))  # type: ignore[arg-type]


def test_v7_bench_matmul_op_count() -> None:
    """红线 31:64×64×64 matmul 操作计数 == 262144,三路互证 + 正确性互证。

    1. 公式口:``matmul_ops(64,64,64) == 64**3 == 262144``;
    2. 全 1 构造:每个输出恰消费 k=64 次 ``1*1`` 乘加 → 单元素 = 64、
       4096 个输出元素总和 = m·n·k = 262144(结果自身就是计数器);
    3. 替身元素口:float 子类逐次计数内部乘法,恰 262144 次;
    正确性互证:同一代码路径对 seeded 随机阵 × 单位阵给出逐元素精确
    恒等 —— 计数与正确性出自同一次实现,代差可离线复现(零墙钟)。
    """
    m = k = n = 64
    expected_ops = matmul_ops(m, k, n)
    assert expected_ops == 262144 == 64**3

    # 口 2:全 1 矩阵 —— 元素总和 = 乘加总次数。
    ones_a = [[1.0] * k for _ in range(m)]
    ones_b = [[1.0] * n for _ in range(k)]
    product = matmul(ones_a, ones_b)
    assert len(product) == m and all(len(row) == n for row in product)
    assert all(cell == 64.0 for row in product for cell in row)
    total = sum(sum(row) for row in product)
    assert total == 262144 == expected_ops

    # 口 3:替身元素逐次计数内部乘法。
    _CountingFloat.mul_count = 0
    counting_a = [[_CountingFloat(1.0)] * k for _ in range(m)]
    counting_b = [[_CountingFloat(1.0)] * n for _ in range(k)]
    counted = matmul(counting_a, counting_b)
    assert _CountingFloat.mul_count == expected_ops == 262144
    assert all(cell == 64.0 for row in counted for cell in row)

    # 正确性互证:seeded 随机阵 × 单位阵逐元素精确恒等。
    rng = random.Random(2_026_100_2)
    r8 = [[rng.uniform(-2.0, 2.0) for _ in range(8)] for _ in range(8)]
    i8 = [[1.0 if i == j else 0.0 for j in range(8)] for i in range(8)]
    assert matmul(r8, i8) == r8
    assert matmul(i8, r8) == r8


def test_kernel_selfcheck_deterministic() -> None:
    """kernel_selfcheck 输出确定性:两次调用逐字段一致,value == baseline。"""
    first = kernel_selfcheck()
    second = kernel_selfcheck()
    assert first == second
    assert first["name"] == "mathx.matmul"
    assert first["value"] == first["baseline"] == 262144


def test_module_all_exports_complete() -> None:
    """__all__ 恰导出契约要求的七个 API + kernel_selfcheck,无遗漏。"""
    required = {
        "dot", "matmul", "softmax", "sigmoid",
        "standardize", "clamp", "matmul_ops", "kernel_selfcheck",
    }
    assert required == set(mathx_module.__all__)


# ---------------------------------------------------------------------------
# A199 双后端:探测式 numpy 加速(可选 extra "fast";缺失时全 stdlib)
# ---------------------------------------------------------------------------
#
# 纪律(与 test_mathx_props.py 的双引擎纪律同源:核心零必装依赖):
#
# - numpy 仅为 fast extra 可选;本机未安装时,一致性 / 性能 / 分支接通
#   断言经 :func:`pytest.importorskip` **显式记录跳过原因**,不停摆;
# - 一致性容差 1e-12(实测两路径偏差 ~1e-14,留两个数量级余量);
# - "小输入走 stdlib / 替身走 stdlib"的分支断言不依赖 numpy 存在
#   (探针在 numpy 缺席时恒零调用,断言照样成立);
# - 操作计数口径(红线 31)双路径统一:同形状下替身计数(强制 stdlib)
#   == matmul_ops 公式 == numpy 路径的语义计数(结果一致性互证)。
#: 双后端一致性容差:两路径对同一输入的偏差上界(实测 ~1e-14)。
_TOL = 1e-12
#: 双后端测试的固定种子(可复现,不依赖墙钟 / 网络)。
_DUAL_SEED = 2_026_100_3


def _assert_matrices_close(
    actual: list[list[float]], expected: list[list[float]], tol: float = _TOL
) -> None:
    """矩阵逐元素一致性:|a−b| ≤ tol·max(1, |a|, |b|)(pytest.approx 不支持嵌套)。

    违约时报告最大偏差位置与数值,便于离线复现定位。
    """
    assert len(actual) == len(expected), f"行数不一致:{len(actual)} != {len(expected)}"
    for i, (ra, rb) in enumerate(zip(actual, expected)):
        assert len(ra) == len(rb), f"第 {i} 行长度不一致"
        for j, (a, b) in enumerate(zip(ra, rb)):
            diff = abs(a - b)
            assert diff <= tol * max(1.0, abs(a), abs(b)), (
                f"双后端不一致:({i},{j}) 偏差 {diff!r} 超出容差 "
                f"{tol}(numpy={a!r}, stdlib={b!r})"
            )


def _spy_numpy(monkeypatch: pytest.MonkeyPatch, name: str) -> list[int]:
    """给 mathx 的私有向量化实现装上调用计数探针,返回计数盒。

    探针包住原实现转发调用(不改行为),用于断言"该走 / 不该走向量化
    路径"的分支事实——小输入、子类替身、超大整数必须恒走纯 stdlib。
    """
    calls: list[int] = []
    original = getattr(mathx_module, name)

    def _spy(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(mathx_module, name, _spy)
    return calls


def test_numpy_probe_flag_matches_environment() -> None:
    """探测哨卫:mathx.HAS_NUMPY 与解释器环境的 numpy 可用性一致。"""
    assert isinstance(HAS_NUMPY, bool)
    assert HAS_NUMPY == (importlib.util.find_spec("numpy") is not None)


def test_dual_backend_dot_consistent(monkeypatch: pytest.MonkeyPatch) -> None:
    """一致性:dot 三种超阈值组合在 numpy / stdlib 两路径下偏差 ≤ 1e-12。"""
    pytest.importorskip(
        "numpy",
        reason="numpy 未安装(仅 fast extra 可选,核心零必装依赖);"
        "双后端一致性断言仅在 numpy 存在时执行",
    )
    rng = random.Random(_DUAL_SEED)
    vec80 = [rng.uniform(-3.0, 3.0) for _ in range(80)]        # > 64 → 向量化
    mat_a = [[rng.uniform(-3.0, 3.0) for _ in range(80)] for _ in range(10)]
    vec_k = [rng.uniform(-3.0, 3.0) for _ in range(80)]
    mat_b = [[rng.uniform(-3.0, 3.0) for _ in range(3)] for _ in range(80)]

    dot_spied = _spy_numpy(monkeypatch, "_dot_numpy")
    numpy_results = [
        dot(vec80, vec80),      # 1D·1D → 标量
        dot(mat_a, vec_k),      # 2D·1D → 向量
        dot(vec_k, mat_b),      # 1D·2D → 向量
    ]
    assert len(dot_spied) == 3, "三种超阈值组合都应实际接通向量化路径"

    monkeypatch.setattr(mathx_module, "HAS_NUMPY", False)
    std_results = [
        dot(vec80, vec80),
        dot(mat_a, vec_k),
        dot(vec_k, mat_b),
    ]

    assert numpy_results[0] == pytest.approx(std_results[0], rel=_TOL, abs=_TOL)
    assert numpy_results[1] == pytest.approx(std_results[1], rel=_TOL, abs=_TOL)
    assert numpy_results[2] == pytest.approx(std_results[2], rel=_TOL, abs=_TOL)
    # numpy 路径输出必须是内置 Python 数值(repr / doctest 口径稳定)。
    assert type(numpy_results[0]) is float
    assert all(type(v) is float for v in numpy_results[1])


def test_dual_backend_matmul_consistent(monkeypatch: pytest.MonkeyPatch) -> None:
    """一致性:90×70 · 70×80 matmul 两路径逐元素偏差 ≤ 1e-12,且向量化接通。"""
    pytest.importorskip(
        "numpy",
        reason="numpy 未安装(仅 fast extra 可选);一致性断言仅在 numpy 存在时执行",
    )
    rng = random.Random(_DUAL_SEED + 1)
    a = [[rng.uniform(-3.0, 3.0) for _ in range(70)] for _ in range(90)]
    b = [[rng.uniform(-3.0, 3.0) for _ in range(80)] for _ in range(70)]

    spied = _spy_numpy(monkeypatch, "_matmul_numpy")
    result_numpy = matmul(a, b)
    assert len(spied) == 1, "超阈值纯数值矩阵应实际接通向量化路径"

    monkeypatch.setattr(mathx_module, "HAS_NUMPY", False)
    result_std = matmul(a, b)

    assert len(result_numpy) == 90 and all(len(r) == 80 for r in result_numpy)
    _assert_matrices_close(result_numpy, result_std)
    assert all(type(cell) is float for row in result_numpy for cell in row)


def test_dual_backend_softmax_standardize_consistent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一致性:softmax / standardize 两路径偏差 ≤ 1e-12;边界分支同构。"""
    pytest.importorskip(
        "numpy",
        reason="numpy 未安装(仅 fast extra 可选);一致性断言仅在 numpy 存在时执行",
    )
    rng = random.Random(_DUAL_SEED + 2)
    xs = [rng.uniform(-1000.0, 1000.0) for _ in range(100)]  # > 64 → 向量化
    soft_spied = _spy_numpy(monkeypatch, "_softmax_numpy")
    std_spied = _spy_numpy(monkeypatch, "_standardize_numpy")
    soft_np, std_np = softmax(xs), standardize(xs)
    # 边界分支:常数列 → 全 0;大数值 softmax 不溢出(同走向量化路径)。
    const_np = standardize([5.0] * 100)
    extreme_np = softmax([1000.0] * 100)
    assert len(soft_spied) == 2 and len(std_spied) == 2

    monkeypatch.setattr(mathx_module, "HAS_NUMPY", False)
    soft_std, std_std = softmax(xs), standardize(xs)
    const_std = standardize([5.0] * 100)
    extreme_std = softmax([1000.0] * 100)

    assert soft_np == pytest.approx(soft_std, rel=_TOL, abs=_TOL)
    assert std_np == pytest.approx(std_std, rel=_TOL, abs=_TOL)
    assert const_np == const_std == [0.0] * 100
    assert extreme_np == pytest.approx(extreme_std, rel=_TOL, abs=_TOL)
    assert math.fsum(extreme_np) == pytest.approx(1.0)


def test_small_inputs_never_leave_stdlib(monkeypatch: pytest.MonkeyPatch) -> None:
    """分支断言:≤ 64 元素的小输入恒走纯 stdlib(四个向量化探针均零调用)。

    该断言不依赖 numpy 存在(numpy 缺席时门卫短路,探针恒零)——
    锁定"小输入免 asarray 转换开销 + doctest 输出与后端无关"的承诺。
    """
    spied_dot = _spy_numpy(monkeypatch, "_dot_numpy")
    spied_matmul = _spy_numpy(monkeypatch, "_matmul_numpy")
    spied_soft = _spy_numpy(monkeypatch, "_softmax_numpy")
    spied_std = _spy_numpy(monkeypatch, "_standardize_numpy")

    i8 = [[1.0 if i == j else 0.0 for j in range(8)] for i in range(8)]
    rng = random.Random(_DUAL_SEED + 3)
    r8 = [[rng.uniform(-1.0, 1.0) for _ in range(8)] for _ in range(8)]
    v64 = [rng.uniform(-2.0, 2.0) for _ in range(64)]  # 恰 64:不超阈值
    xs32 = [rng.uniform(-9.0, 9.0) for _ in range(32)]

    assert matmul(r8, i8) == r8                       # 64 元素:stdlib 精确恒等
    assert dot(v64, v64) == pytest.approx(
        sum(x * x for x in v64), rel=_TOL
    )
    assert math.fsum(softmax(xs32)) == pytest.approx(1.0)
    assert math.fsum(standardize(xs32)) == pytest.approx(0.0, abs=1e-12)
    assert not spied_dot and not spied_matmul and not spied_soft and not spied_std


def test_subclass_sentinel_and_huge_int_stay_stdlib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """分支断言:子类替身 / 超大整数即便超规模也回落 stdlib。

    - 70×70(向量化规模)float 子类替身:探针零调用,内部乘法恰
      70³ 次(红线 31 的计数替身在双后端世界里永远可透传);
    - 70 元素 10^30 大整数:float64 无损域外,stdlib 大整数精确点积。
    """
    spied_dot = _spy_numpy(monkeypatch, "_dot_numpy")
    spied_matmul = _spy_numpy(monkeypatch, "_matmul_numpy")

    _CountingFloat.mul_count = 0
    counting_a = [[_CountingFloat(2.0)] * 70 for _ in range(70)]
    counting_b = [[_CountingFloat(2.0)] * 70 for _ in range(70)]
    product = matmul(counting_a, counting_b)
    assert _CountingFloat.mul_count == 70**3 == matmul_ops(70, 70, 70)
    assert all(cell == 280.0 for row in product for cell in row)

    huge = [10**30] * 70
    assert dot(huge, huge) == 70 * 10**60  # 精确大整数,无 float64 舍入
    assert not spied_dot and not spied_matmul


def test_op_count_parity_across_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    """红线 31 双路径口径:同形状 70×70×70,语义计数两后端严格一致。

    三路互证:(1) 公式口 matmul_ops == 70³;(2) 替身口(强制 stdlib)
    逐次计数恰 70³;(3) 向量化口(numpy 路径,探针证真实接通)结果
    与 stdlib 路径逐值 1e-12 一致——每个输出元素恰消费 k 次乘加是
    矩阵乘的定义性口径,与实现后端无关。
    """
    pytest.importorskip(
        "numpy",
        reason="numpy 未安装(仅 fast extra 可选);双路径计数互证仅在 numpy 存在时执行",
    )
    rng = random.Random(_DUAL_SEED + 4)
    a = [[rng.uniform(-3.0, 3.0) for _ in range(70)] for _ in range(70)]
    b = [[rng.uniform(-3.0, 3.0) for _ in range(70)] for _ in range(70)]

    spied = _spy_numpy(monkeypatch, "_matmul_numpy")
    result_numpy = matmul(a, b)
    assert len(spied) == 1, "70×70 纯数值矩阵应接通向量化路径"

    monkeypatch.setattr(mathx_module, "HAS_NUMPY", False)
    result_std = matmul(a, b)

    _assert_matrices_close(result_numpy, result_std)
    # 口 2 在 test_subclass_sentinel_and_huge_int_stay_stdlib 中已对同形状
    # 断言(替身计数 == matmul_ops(70,70,70));此处再钉公式口。
    assert matmul_ops(70, 70, 70) == 70**3 == 343_000


def test_kernel_selfcheck_passes_both_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kernel_selfcheck 双后端哨卫:两路径均 value == baseline == 262144。

    64×4096 全 1 输入在 float64 内精确可表,两路径输出逐字节一致
    (A138 内核基准总控在任一后端环境下判定恒为通过)。
    """
    natural = kernel_selfcheck()
    monkeypatch.setattr(mathx_module, "HAS_NUMPY", False)
    forced_stdlib = kernel_selfcheck()
    monkeypatch.undo()
    assert natural == forced_stdlib
    for report in (natural, forced_stdlib):
        assert report["value"] == report["baseline"] == 262144
    assert kernel_selfcheck() == natural  # 确定性:同后端两次逐字节一致

def test_numpy_backend_perf_smoke_500() -> None:
    """性能冒烟:numpy 后端 500×500 matmul < 1s(宽松上界,允许墙钟)。

    红线 31 禁的是**基准断言**依赖墙钟;本冒烟仅验证 BLAS 加速真实
    可感(纯 stdlib 同规模需数十秒)。500³ = 1.25e8 次乘加,BLAS 实测
    数十 ms,1s 上界留 20 倍以上余量,CI 抖动不误伤。
    """
    numpy = pytest.importorskip(
        "numpy",
        reason="numpy 未安装(仅 fast extra 可选);性能冒烟仅在 numpy 存在时执行",
    )
    assert mathx_module.HAS_NUMPY, "numpy 已安装则探测标志必须为真"
    rng = random.Random(_DUAL_SEED + 5)
    a = [[rng.uniform(-1.0, 1.0) for _ in range(500)] for _ in range(500)]
    b = [[rng.uniform(-1.0, 1.0) for _ in range(500)] for _ in range(500)]

    start = time.perf_counter()
    product = matmul(a, b)
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, f"500×500 matmul 耗时 {elapsed:.3f}s 超出冒烟上界"
    assert len(product) == 500 and all(len(row) == 500 for row in product)
    assert all(math.isfinite(cell) for row in product for cell in row)
    assert numpy.__version__  # pragma: no cover - 仅为记录被测后端存在
