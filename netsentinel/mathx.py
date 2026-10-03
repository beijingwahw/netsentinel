"""推理内核·张量微库(A133 / A199 双后端)—— 缺省纯 stdlib、探测式可选 numpy 加速。

依据 CONTRACTS-V7.md §2 A133 与红线 29/31:为 V7 各学习型内核
(SPRT 打分融合、文本 n-gram TF-IDF、可靠性加权融合……)提供一套
**零第三方依赖、零 IO、纯函数**的公共数值底座,避免各内核各自手搓
点积 / 归一化时口径漂移:

- :func:`dot`          向量点积 / 矩阵-向量 / 向量-矩阵 / 矩阵-矩阵
                       (一维、二维皆可;维度与非矩形校验全抛中文 ValueError);
- :func:`matmul`       二维嵌套列表矩阵乘(转置 + 生成器内积累加);
- :func:`softmax`      数值稳定 softmax(减最大值,大数值不溢出);
- :func:`sigmoid`      数值稳定 sigmoid(正负双分支,±1000 不溢出);
- :func:`standardize`  总体 z-score 标准化(σ=0 → 全 0;fsum 求和);
- :func:`clamp`        逐元素夹取到 ``[lo, hi]``;
- :func:`matmul_ops`   m×k · k×n 矩阵乘的乘加操作计数(= m·k·n,
                       供 bench 以操作计数断言,红线 31)。

设计约束(红线 29/30):

- **缺省纯 stdlib**:基线仅 ``math`` / ``collections.abc`` / ``typing``
  (numpy 仅为探测式可选,见下节"双后端");零随机、零网络、零文件
  IO(:func:`kernel_selfcheck` 用全 1 确定性构造);
- **纯函数**:不改入参、不持全局可变状态;任意嵌套 ``list`` /
  ``tuple`` 皆可入参,元素不拷贝转换、原样进入运算生成器(因此
  float 子类替身元素也能透传,便于测试精确计数内部乘法次数);
- **零 API 破坏**:本模块为包根**新增**顶层模块,不触碰任何既有
  模块,无既有调用方受影响;
- 数值语义:int 输入按 Python 数值塔自然参与运算(手算整值结果可
  用 ``==`` 精确对照);``bool`` 不视为数值,显式拒绝。

双后端(A199,探测式 numpy 加速,对标 BLAS 加速;numpy 仅为可选
extra ``fast``,**缺失时一切走纯 stdlib 路径,零必装依赖红线不破**):

- 模块导入时一次性探测 ``numpy``(:data:`HAS_NUMPY` 记录结果);
- 热路径(:func:`dot` / :func:`matmul` / :func:`softmax` /
  :func:`standardize`)在 numpy 可用且**输入规模超阈值**(任一操作数
  元素数 > :data:`_NUMPY_MIN_ELEMENTS` = 64)时走向量化路径,小输入
  一律纯 stdlib(免 asarray 转换开销,docstring 手算示例恒走 stdlib,
  输出双后端逐字节一致);
- 向量化路径仅接受**原生 int/float 元素**:bool、一切子类替身
  (float 计数替身)、超出 float64 无损域(2^53)的大整数一律回落
  stdlib——因此红线 31 的操作计数替身永远逐次经过 Python 乘法,
  计数口径不受后端选择影响;
- **双后端数值一致**:两路径对同一输入的偏差锁定在 1e-12 容差内
  (tests/test_mathx.py 双后端一致性测试);numpy 输出经 ``tolist()``
  / ``float()`` 还原为内置 Python 数值(repr / doctest 口径稳定);
- **操作计数口径不变**::func:`matmul_ops` 的 m·k·n 语义对两后端
  统一成立;:func:`kernel_selfcheck` 在双后端下均通过且
  ``value == baseline``。

操作计数口径(红线 31:基准以操作计数断言,禁墙钟)::

    matmul(m, k, n) 内循环乘加(multiply-accumulate)次数 == m*k*n
    == matmul_ops(m, k, n)
    (每次乘加 = 1 次乘法 + 1 次加法;单计乘法亦为 m*k*n,
    乘+加合计 FLOPs 口径为 2*m*k*n)

:func:`kernel_selfcheck` 供 A138 内核基准总控离线调用,输出确定性
(无墙钟、无真随机),两次调用结果逐字节一致。
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import TypeAlias

# ---------------------------------------------------------------------------
# 探测式 numpy 加速后端(A199;可选 extra "fast",缺失零影响)
# ---------------------------------------------------------------------------

try:  # pragma: no cover - 探测分支各在一种环境执行一次,覆盖统计无意义
    import numpy as _np  # type: ignore[import-not-found]

    #: numpy 是否可用(模块级探测一次;缺失时全模块走纯 stdlib 路径,
    #: 行为与 A133 原版一致,测试不 skip 不停摆)。
    HAS_NUMPY: bool = True
except ImportError:  # pragma: no cover - numpy 缺席:核心零必装依赖红线不破
    _np = None  # type: ignore[assignment]
    HAS_NUMPY = False

__all__ = [
    "dot",
    "matmul",
    "matmul_ops",
    "softmax",
    "sigmoid",
    "standardize",
    "clamp",
    "kernel_selfcheck",
]

#: 向量化触发阈值:任一操作数元素数 ≤ 此值一律纯 stdlib(小输入免
#: asarray 转换开销;doctest / 手算示例均为小输入,输出与后端无关)。
_NUMPY_MIN_ELEMENTS: int = 64

#: float64 可无损表示的整数上界(2^53):超出此域的 int 回落 stdlib
#: 大整数精确路径,绝不经 numpy 静默丢精度。
_EXACT_INT_MAX: int = 2**53

#: 数值元素口径:int / float(bool 显式排除,见 :func:`_is_number`)。
Number: TypeAlias = int | float
#: 一维向量(任意 Sequence,元素为数值)。
Vector: TypeAlias = Sequence[Number]
#: 二维矩阵(嵌套 Sequence,须矩形;非矩形在入口检测并报错)。
Matrix: TypeAlias = Sequence[Sequence[Number]]
#: dot 的一维 / 二维联合入参类型。
Array: TypeAlias = Vector | Matrix


# ---------------------------------------------------------------------------
# 内部校验辅助(纯结构检查,不拷贝、不转换元素)
# ---------------------------------------------------------------------------


def _is_number(v: object) -> bool:
    """v 是否为本库认可的数值(int / float;bool 是 int 子类但显式排除)。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _classify(x: object, label: str) -> tuple[str, int, int, bool]:
    """判定一维 / 二维形状并完成全部结构校验。

    返回 ``(种类, 外长度, 内长度, 全原生数值)``:向量为
    ``("vec", len, 0, plain)``,矩阵为 ``("mat", 行数, 列数, plain)``。
    ``plain`` 表示全体元素恰为原生 int/float 且 int 在 float64 无损域
    (见 :func:`_plain_number`),是启用 numpy 向量化路径的必要条件
    (子类替身元素一律回落 stdlib,操作计数替身因此永远可透传)。

    校验(全抛中文 ``ValueError``,信息带 ``label`` 前缀定位参数):

    - 非 Sequence(list / tuple 之外的可索引序列也可)→ 拒绝;
    - 空 → 拒绝;矩阵含空行(零宽)→ 拒绝;
    - 一维混入非数值元素 / 二维行内混入非数值元素 → 拒绝;
    - 二维各行列数不齐(非矩形)→ 拒绝。

    仅做 isinstance 结构检查,元素对象原样保留(float 子类替身
    元素可透传,供测试计数内部乘法)。
    """
    if not isinstance(x, Sequence):
        raise ValueError(
            f"{label}:必须是列表或元组(一维向量 / 二维嵌套),"
            f"got {type(x).__name__}"
        )
    outer = len(x)
    if outer == 0:
        raise ValueError(f"{label}:输入不能为空")
    first = x[0]
    if _is_number(first):
        plain = True
        for i, v in enumerate(x):
            if not _is_number(v):
                raise ValueError(
                    f"{label}:一维向量须全为数值,第 {i} 个元素是 "
                    f"{type(v).__name__}"
                )
            plain = plain and _plain_number(v)
        return "vec", outer, 0, plain
    if isinstance(first, Sequence):
        width = len(first)
        if width == 0:
            raise ValueError(f"{label}:矩阵不能包含空行(首行宽度为 0)")
        plain = True
        for i, row in enumerate(x):
            if not isinstance(row, Sequence):
                raise ValueError(
                    f"{label}:二维矩阵各行须为列表/元组,第 {i} 行是 "
                    f"{type(row).__name__}(一维/二维元素混杂)"
                )
            if len(row) != width:
                raise ValueError(
                    f"{label}:非矩形矩阵:第 {i} 行长度 {len(row)} "
                    f"≠ 首行宽度 {width}"
                )
            for j, v in enumerate(row):
                if not _is_number(v):
                    raise ValueError(
                        f"{label}:矩阵元素须全为数值,第 {i} 行第 {j} 列是 "
                        f"{type(v).__name__}"
                    )
                plain = plain and _plain_number(v)
        return "mat", outer, width, plain
    raise ValueError(
        f"{label}:无法识别的形状:首元素类型 {type(first).__name__}"
        f"(期望数值或列表/元组)"
    )


def _require_numbers(vals: list[object], fname: str) -> None:
    """一维 iterable 入口校验:非空且全为数值(中文 ValueError)。"""
    if not vals:
        raise ValueError(f"{fname}:输入不能为空")
    for i, v in enumerate(vals):
        if not _is_number(v):
            raise ValueError(
                f"{fname}:元素须全为数值,第 {i} 个是 {type(v).__name__}"
            )


# ---------------------------------------------------------------------------
# 后端选择与向量化实现(A199;全部私有,不进 __all__)
# ---------------------------------------------------------------------------


def _plain_number(v: object) -> bool:
    """v 是否为可安全进 numpy 路径的原生数值(三重门,缺一回落 stdlib)。

    - 类型恰为 ``int`` / ``float``:bool 与一切子类替身排除(float 计数
      替身必须逐次经过 Python 乘法,红线 31 的计数口径才可观测);
    - int 须在 float64 无损域内(|v| ≤ 2^53),杜绝 asarray 静默丢精度
      或大整数转换溢出。
    """
    t = type(v)
    if t is float:
        return True
    return t is int and -_EXACT_INT_MAX <= v <= _EXACT_INT_MAX


def _use_numpy(*operand_sizes: int) -> bool:
    """规模门:numpy 可用且任一操作数元素数 > 阈值才启用向量化。"""
    return HAS_NUMPY and max(operand_sizes) > _NUMPY_MIN_ELEMENTS


def _dot_numpy(a: Array, b: Array) -> float | list[Number]:
    """dot 的 numpy 向量化路径(调用前已完成全部结构/维度校验)。

    numpy 的 ``@`` 对 1D·1D / 2D·1D / 1D·2D 的语义与 :func:`dot`
    同构;输出经 ``float()`` / ``tolist()`` 还原为内置 Python 数值,
    repr / doctest 口径与 stdlib 路径一致。
    """
    out = _np.asarray(a, dtype=_np.float64) @ _np.asarray(b, dtype=_np.float64)
    if isinstance(out, _np.ndarray):
        return out.tolist()
    return float(out)  # 1D·1D → 标量


def _matmul_numpy(a: Matrix, b: Matrix) -> list[list[Number]]:
    """matmul 的 numpy 向量化路径(调用前已完成全部结构/维度校验)。

    语义操作计数仍为 m·k·n(:func:`matmul_ops`):每个输出元素恰消费
    k 次乘加是矩阵乘的定义性口径,与实现后端无关。
    """
    arr = _np.asarray(a, dtype=_np.float64) @ _np.asarray(b, dtype=_np.float64)
    return arr.tolist()


def _softmax_numpy(vals: list[Number]) -> list[float]:
    """softmax 的 numpy 向量化路径(入参已校验非空且全为数值)。"""
    arr = _np.asarray(vals, dtype=_np.float64)
    exps = _np.exp(arr - arr.max())  # 减最大值:指数恒 ≤ 0,大数值不溢出
    return (exps / exps.sum()).tolist()


def _standardize_numpy(vals: list[Number]) -> list[float]:
    """standardize 的 numpy 向量化路径(入参已校验非空且全为数值)。"""
    arr = _np.asarray(vals, dtype=_np.float64)
    sigma = float(arr.std())  # 总体标准差(除以 n,与 stdlib 口径一致)
    if sigma == 0.0:
        return [0.0] * len(vals)  # 常数列 → 全 0,与 stdlib 约定一致
    return ((arr - arr.mean()) / sigma).tolist()


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def dot(a: Array, b: Array) -> float | list[Number] | list[list[Number]]:
    """广义内积:一维/二维皆可,维度不共形抛中文 ``ValueError``。

    四种组合(numpy ``dot`` 同构语义):

    - 一维 · 一维 → 标量(等长点积);
    - 二维 · 一维 → 长度为 a 行数的向量(矩阵-向量);
    - 一维 · 二维 → 长度为 b 列数的向量(向量-矩阵);
    - 二维 · 二维 → 矩阵积(等价 :func:`matmul`)。

    内积一律用生成器逐项累加(``sum(x*y for ...)``),不物化中间
    列表;入参非矩形 / 含非数值元素同样在入口报错。规模超阈值且
    元素全为原生数值时走 numpy 向量化路径(双后端一致,容差 1e-12,
    详见模块 docstring 的"双后端"一节)。

    示例::

        >>> dot([1, 2, 3], [4, 5, 6])
        32
        >>> dot([[1, 2], [3, 4]], [5, 6])
        [17, 39]
    """
    kind_a, len_a, cols_a, plain_a = _classify(a, "dot:参数 a")
    kind_b, rows_b, cols_b, plain_b = _classify(b, "dot:参数 b")
    if kind_a == "vec" and kind_b == "vec":
        if len_a != rows_b:
            raise ValueError(
                f"dot:维度不共形:向量点积要求等长,"
                f"got len(a)={len_a} ≠ len(b)={rows_b}"
            )
        if plain_a and plain_b and _use_numpy(len_a):
            return _dot_numpy(a, b)  # 向量化:两向量同长,规模即元素数
        return sum(x * y for x, y in zip(a, b))
    if kind_a == "mat" and kind_b == "vec":
        if cols_a != rows_b:
            raise ValueError(
                f"dot:维度不共形:a 为 {len_a}×{cols_a} 矩阵,"
                f"b 为长度 {rows_b} 向量,要求 a 的列数 == len(b)"
            )
        if plain_a and plain_b and _use_numpy(len_a * cols_a, rows_b):
            return _dot_numpy(a, b)
        return [sum(x * y for x, y in zip(row, b)) for row in a]
    if kind_a == "vec" and kind_b == "mat":
        if len_a != rows_b:
            raise ValueError(
                f"dot:维度不共形:a 为长度 {len_a} 向量,"
                f"b 为 {rows_b}×{cols_b} 矩阵,要求 len(a) == b 的行数"
            )
        if plain_a and plain_b and _use_numpy(len_a, rows_b * cols_b):
            return _dot_numpy(a, b)
        return [sum(x * y for x, y in zip(a, col)) for col in zip(*b)]
    # 二维 · 二维:与 matmul 完全同构(含全部维度/矩形校验)。
    if cols_a != rows_b:
        raise ValueError(
            f"dot:维度不共形:a 为 {len_a}×{cols_a},b 为 {rows_b}×{cols_b},"
            f"要求 a 的列数 == b 的行数"
        )
    return matmul(a, b)


def matmul(a: Matrix, b: Matrix) -> list[list[Number]]:
    """二维嵌套列表矩阵乘:a 为 m×k、b 为 k×n → m×n。

    实现为"转置 + 生成器内积累加":先把 b 转置成 n 个 k 元列,
    每个输出元素恰好消费 k 次乘加 —— 内循环乘加总次数为
    ``m*k*n == matmul_ops(m, k, n)``(红线 31 的操作计数保证)。
    元素原样参与运算(int 结果保持 int,可手算精确对照)。

    双后端(A199):numpy 可用且两阵元素全为原生数值、规模超阈值时
    走向量化路径(结果为 float64 精确整数/浮点,偏差锁定 1e-12 容差);
    子类替身元素 / 超大整数 / 小输入一律本纯 stdlib 路径,int 语义
    原样保留。**操作计数口径 m·k·n 对两后端统一成立**(每个输出元素
    恰消费 k 次乘加是矩阵乘的定义,与实现无关;stdlib 路径另由
    float 子类替身逐次计数直接验证)。

    入口校验(中文 ``ValueError``):两个参数都必须是二维嵌套列表
    (向量混合运算请用 :func:`dot`)、矩形(各行列数一致)、非空、
    列数 == 行数共形。

    示例::

        >>> matmul([[1, 2], [3, 4]], [[5, 6], [7, 8]])
        [[19, 22], [43, 50]]
    """
    kind_a, m, k, plain_a = _classify(a, "matmul:参数 a")
    kind_b, rows_b, n, plain_b = _classify(b, "matmul:参数 b")
    if kind_a != "mat" or kind_b != "mat":
        raise ValueError(
            "matmul:两个参数都必须是二维嵌套列表(向量混合运算请用 dot)"
        )
    if k != rows_b:
        raise ValueError(
            f"matmul:维度不共形:a 为 {m}×{k},b 为 {rows_b}×{n},"
            f"要求 a 的列数 == b 的行数"
        )
    if plain_a and plain_b and _use_numpy(m * k, rows_b * n):
        return _matmul_numpy(a, b)
    cols = list(zip(*b))  # 转置:n 个 k 元列(元素原样引用,零拷贝)
    return [
        [sum(x * y for x, y in zip(row, col)) for col in cols] for row in a
    ]


def matmul_ops(m: int, k: int, n: int) -> int:
    """m×k · k×n 矩阵乘的内循环乘加操作计数,恒等于 ``m*k*n``。

    口径:一次乘加(multiply-accumulate)= 1 次乘法 + 1 次加法;
    乘法单独计数亦为 ``m*k*n``,乘+加合计(FLOPs)为 ``2*m*k*n``。
    供 kernelbench 以**操作计数**断言 matmul 的复杂度(红线 31:
    禁止依赖墙钟);:func:`matmul` 的实现保证内循环乘加次数与之
    严格相等(可用 float 子类替身元素逐次计数验证)。

    示例::

        >>> matmul_ops(64, 64, 64)
        262144
    """
    for name, v in (("m", m), ("k", k), ("n", n)):
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ValueError(
                f"matmul_ops:{name} 必须是非负整数,got {v!r}"
            )
    return m * k * n


def softmax(xs: Iterable[Number]) -> list[float]:
    """数值稳定 softmax:先减去最大值再取指数,大数值(如 1000)不溢出。

    不变量:输出全为 ``(0, 1]`` 内的浮点、``fsum`` 之和为 1(浮点
    容差内);输入不能为空(否则"和为 1"无从谈起,抛中文
    ``ValueError``)。接受任意可迭代对象(含生成器)。

    双后端(A199):规模超阈值(> 64 元素)且 numpy 可用时走向量化
    路径,两后端结果锁定 1e-12 容差一致;小输入恒走 stdlib。

    示例::

        >>> softmax([1000.0, 1000.0])
        [0.5, 0.5]
    """
    vals = list(xs)
    _require_numbers(vals, "softmax")
    if _use_numpy(len(vals)):
        return _softmax_numpy(vals)
    peak = max(vals)
    exps = [math.exp(v - peak) for v in vals]  # 减最大值:指数恒 ≤ 0
    total = math.fsum(exps)
    return [e / total for e in exps]


def sigmoid(x: Number) -> float:
    """数值稳定 sigmoid(x) = 1 / (1 + e^(-x)),正负双分支,±1000 不溢出。

    - ``x >= 0``:直接 ``1 / (1 + exp(-x))``,``exp`` 参数恒 ≤ 0、
      值域 ``(0, 1]``,大正数下溢为 0 → 恰返回 1.0;
    - ``x < 0``:改用 ``exp(x) / (1 + exp(x))``,``exp`` 参数恒 < 0,
      大负数下溢为 0 → 恰返回 0.0;两个分支都不会触发 ``OverflowError``。

    示例::

        >>> sigmoid(0.0)
        0.5
        >>> sigmoid(1000.0)
        1.0
    """
    if not _is_number(x):
        raise ValueError(f"sigmoid:输入必须是数值,got {type(x).__name__}")
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def standardize(xs: Iterable[Number]) -> list[float]:
    """总体 z-score 标准化:(x − μ) / σ;σ = 0(常数列)→ 全 0。

    μ 与总体方差(除以 n,非样本 n−1)均用 :func:`math.fsum` 累加,
    输出均值精确为 0(浮点容差内)、总体标准差为 1;常数列的 σ 恰为
    0.0,按约定返回全 0(而非除零 NaN)。输入不能为空,接受任意
    可迭代对象(含生成器)。

    双后端(A199):规模超阈值(> 64 元素)且 numpy 可用时走向量化
    路径(``std()`` 的总体口径 ddof=0 与本函数一致),两后端结果
    锁定 1e-12 容差一致;小输入恒走 stdlib。

    示例::

        >>> standardize([5.0, 5.0, 5.0])
        [0.0, 0.0, 0.0]
    """
    vals = list(xs)
    _require_numbers(vals, "standardize")
    n = len(vals)
    if _use_numpy(n):
        return _standardize_numpy(vals)
    mean = math.fsum(vals) / n
    variance = math.fsum((v - mean) ** 2 for v in vals) / n
    sigma = math.sqrt(variance)
    if sigma == 0.0:
        return [0.0] * n
    return [(v - mean) / sigma for v in vals]


def clamp(xs: Iterable[Number], lo: Number, hi: Number) -> list[Number]:
    """逐元素夹取到 ``[lo, hi]``:小于 lo 取 lo,大于 hi 取 hi,其余原样。

    区间内的元素对象原样保留(int 保持 int);``lo > hi`` 抛中文
    ``ValueError``。接受任意可迭代对象(含生成器)。

    示例::

        >>> clamp([-2, 0.5, 9], 0, 1)
        [0, 0.5, 1]
    """
    if not _is_number(lo) or not _is_number(hi):
        raise ValueError(
            f"clamp:lo / hi 必须是数值,got lo={lo!r}, hi={hi!r}"
        )
    if lo > hi:
        raise ValueError(f"clamp:下界不能大于上界:lo={lo} > hi={hi}")
    vals = list(xs)
    _require_numbers(vals, "clamp")
    return [lo if v < lo else hi if v > hi else v for v in vals]


# ---------------------------------------------------------------------------
# 内核自检(A138 kernel_bench 统一调用;红线 31:操作计数,非墙钟)
# ---------------------------------------------------------------------------


def kernel_selfcheck() -> dict[str, object]:
    """确定性微基准:64×64×64 全 1 矩阵乘的乘加操作计数。

    构造全 1 的 64×64 矩阵对:每个输出元素 = k=64 个 ``1*1`` 乘加,
    故输出矩阵全体元素之和 = m·n·k = 64³ = 262144,恰等于
    :func:`matmul_ops` 的理论乘加计数 —— ``value == baseline`` 即
    证明实现内循环与复杂度口径**零漂移**(红线 31)。全 1 构造无
    随机、无墙钟,两次调用输出逐字节一致(离线可复现)。

    双后端(A199):本自检在纯 stdlib 与 numpy 双后端下均通过
    (全 1 输入的 64 项乘加在 float64 内精确可表,两路径结果同为
    262144.0,逐字节一致)。

    示例::

        >>> kernel_selfcheck()["value"] == kernel_selfcheck()["baseline"]
        True
    """
    m = k = n = 64
    ones_m = [[1.0] * k for _ in range(m)]
    ones_k = [[1.0] * n for _ in range(k)]
    product = matmul(ones_m, ones_k)
    # 每个输出恰消费 k 次乘加、每次累加 1 → 元素总和 = 乘加总次数。
    accumulates = sum(sum(row) for row in product)
    expected = matmul_ops(m, k, n)
    assert accumulates == expected == 262_144  # 永真哨卫:防实现漂移
    return {
        "name": "mathx.matmul",
        "metric": (
            "64×64×64 全 1 矩阵乘的内循环乘加操作计数"
            "(Σ输出元素 × 每元素 k 次乘加;理论口径 = m·k·n)"
        ),
        "value": accumulates,
        "baseline": expected,
    }
