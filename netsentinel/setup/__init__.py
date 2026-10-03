# -*- coding: utf-8 -*-
"""netsentinel.setup:连接向导子包(A145–A146、A153–A154,A143–A162 并行开发)。

V8 的自托管"连接向导"家:纯标准库实现(不依赖 fastapi / streamlit),
只绑定 ``127.0.0.1``(红线 32:本地探测/服务仅限回环;红线 33:密钥只进不显)。

- A145 ``server``::class:`SetupServer` —— ThreadingHTTPServer 向导服务
  (状态/扫描/启用/密钥/连通性测试五个 JSON 接口 + 单页 HTML);
- A146 ``page``(兄弟模块,可缺席)——单文件中文向导页;
- A153 ``flow`` / A154 ``daemon``(兄弟模块)——状态机与常驻单例。

顶层保持零导入副作用(与 security / vision 子包一致),``SetupServer``
经 :pep:`562` ``__getattr__`` 惰性暴露,避免包导入即拉起 http.server 相关符号。
"""
from __future__ import annotations

from typing import Any

__all__ = ["SetupServer"]


def __getattr__(name: str) -> Any:
    """惰性暴露 :class:`SetupServer`(仅此一个符号触发真实导入)。"""
    if name == "SetupServer":
        from netsentinel.setup.server import SetupServer  # noqa: PLC0415 惰性导入

        return SetupServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
