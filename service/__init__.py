"""净网哨兵本机 REST 服务包(A31)。

只包含 ``service/app.py``(FastAPI 可选依赖,惰性导入)。
``import service`` 不触发 fastapi 导入;未安装 ``netsentinel[api]`` extra 时
本包仍可被安全导入(例如只复用 ``service.app`` 里的纯辅助函数)。

安全说明:该服务无鉴权,仅限绑定 127.0.0.1 本机使用;只生成举报计划与
预览,不提供任何真实提交端点。
"""
from __future__ import annotations

__all__ = ["app"]
