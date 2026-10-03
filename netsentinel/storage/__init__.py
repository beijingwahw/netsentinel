"""netsentinel.storage:统一存储子包(V7 · A131,SQLiteKernel 统一存储底座)。

惰性重导出 ``SQLiteKernel`` / ``Repo``(子模块 :mod:`netsentinel.storage.kernel`
按需导入),保持与 decision / intel 等兄弟子包一致的最小 ``__init__`` 风格,
对既有调用方零影响(红线 29:纯新增)。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查期导入
    from netsentinel.storage.kernel import Repo, SQLiteKernel

__all__ = ["SQLiteKernel", "Repo", "kernel"]


def __getattr__(name: str):  # PEP 562:惰性导入,导入子包零开销
    if name in ("SQLiteKernel", "Repo"):
        from netsentinel.storage import kernel as _kernel

        return getattr(_kernel, name)
    if name == "kernel":
        from netsentinel.storage import kernel as _kernel

        return _kernel
    raise AttributeError(f"module {__name__!r} 没有属性 {name!r}")
