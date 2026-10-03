"""净网哨兵 NetSentinel 服务子包(A151,V8)。

注意与仓库**顶层** ``service/`` 包(A31 本机 REST 服务层,``service/app.py``)
区分:本包是 ``netsentinel`` 包内的子包,当前只承载 V8 的模型管理 REST 路由
(:mod:`netsentinel.service.model_api`),由负责人在 ``service/app.py`` 中
一行 ``include_router`` 挂载(见 CONTRACTS-V8 §4,代理勿改)。

本 ``__init__`` 刻意不导入任何子模块(零副作用),保证导入本包永远不需要
fastapi 等可选依赖。
"""
