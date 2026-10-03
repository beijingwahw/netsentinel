# -*- coding: utf-8 -*-
"""doctest 激活机:把散落在纯逻辑模块 docstring 里的可执行示例变成测试资产。

背景(对标 property-based testing / docs-as-tests 的"示例即契约"一翼):
仓库中多个纯 stdlib 模块的 docstring 写有 ``>>>`` 示例,但 pytest 默认
不执行它们——示例一旦与实现漂移,文档会安静地撒谎。本文件用
:func:`doctest.testmod` 程序化执行**零 IO 副作用**模块的全部 doctest,
失败即断言失败,让"文档示例"获得与单元测试同等的回归保护。

选模块的三条纪律:

1. **纯逻辑**:模块导入与 doctest 执行都不产生文件 / 网络 / 数据库
   副作用(mathx 是零 IO 数值库;adaptive/sched_kernel 是纯函数内核,
   telemetry 仅内存计数;review_tui 的 ``_display_width`` 与 vlmctl 的
   ``_render_table`` 是纯字符串渲染,包级 ``__init__`` 已声明不连带建连);
2. **自包含**:示例引用的一切名字都在模块命名空间或示例自身内
   (name_suggest 的 ``suggest_name(group)`` 引用未定义的夹具变量,
   属于"非自包含示例",显式排除并在 :data:`_EXCLUDED` 留档,而非静默);
3. **守门**::func:`test_every_doctest_module_is_triaged` 反向扫描包内
   全部 ``*.py`` 源码——任何新出现 ``>>>`` 的模块必须显式归入
   收编清单或排除清单,防止未来的 doctest 无人认领。

phash2 与 conformal 经查不含 ``>>>`` 示例(phash2 的用法示例为非
doctest 的叙述式代码块),故不在收编清单中,由扫描测试自动放行。
"""
from __future__ import annotations

import doctest
import importlib
import pytest

#: 收编清单:纳入 doctest 回归的纯逻辑模块(导入与示例执行均零 IO 副作用)。
_DOCTEST_MODULES: list[str] = [
    "netsentinel.mathx",            # 张量微库:dot/matmul/softmax/sigmoid/... 10 例
    "netsentinel.ops.adaptive",     # 自适应重扫内核:volatility/suggest_interval 3 例
    "netsentinel.ops.sched_kernel", # 调度内核:priority/select_round 5 例
    "netsentinel.cli.review_tui",   # 复核 TUI:_display_width(CJK/ANSI 宽度)2 例
    "netsentinel.vision.vlmctl",    # VLM 诊断 CLI:_render_table(表格渲染)1 例
]

#: 排除清单:含 ``>>>`` 但不可收编的模块 → 排除原因(留档,非静默跳过)。
_EXCLUDED: dict[str, str] = {
    "netsentinel.intel.name_suggest": (
        "docstring 示例引用未定义的夹具变量 group / group3,"
        "非自包含示例(doctest 执行必 NameError);修复示例后可移入收编清单"
    ),
}


@pytest.mark.parametrize("modname", _DOCTEST_MODULES)
def test_module_doctests(modname: str) -> None:
    """程序化执行单个模块的全部 doctest:有失败或零示例即断言失败。"""
    module = importlib.import_module(modname)  # 缺失/导入崩 → 测试失败而非跳过
    results = doctest.testmod(
        module,
        verbose=False,
        optionflags=doctest.ELLIPSIS,
    )
    assert results.attempted > 0, (
        f"{modname}:预期含可执行 doctest,实际执行 0 例——"
        f"示例被删/格式破坏,或模块被错误加入收编清单"
    )
    assert results.failed == 0, (
        f"{modname}:{results.failed}/{results.attempted} 个 doctest 失败——"
        f"docstring 示例已与实现漂移,文档在撒谎"
    )


def test_every_doctest_module_is_triaged() -> None:
    """守门:包内任何含 ``>>>`` 的模块必须显式归入收编或排除清单。

    反向扫描 ``netsentinel/`` 下全部 ``*.py`` 源码(跳过 ``__pycache__``),
    出现新的含 doctest 模块而未分诊 → 失败并打印模块名,提醒维护者
    二选一:加进 :data:`_DOCTEST_MODULES`(推荐,零副作用纯逻辑模块)
    或在 :data:`_EXCLUDED` 登记排除原因。
    """
    import pathlib
    import sys

    root = pathlib.Path(__file__).resolve().parents[1] / "netsentinel"
    assert root.is_dir(), f"未找到包根目录:{root}"
    triaged = set(_DOCTEST_MODULES) | set(_EXCLUDED)
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root.parent).with_suffix("")
        modname = ".".join(rel.parts)
        if ">>>" in path.read_text(encoding="utf-8") and modname not in triaged:
            offenders.append(modname)
    assert not offenders, (
        "以下模块含 >>> doctest 但未分诊(收编进 _DOCTEST_MODULES,"
        "或在 _EXCLUDED 登记排除原因):"
        + ";".join(offenders)
        + f"(sys.path 首段={sys.path[0]!r} 仅供诊断)"
    )
