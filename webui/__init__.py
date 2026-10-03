"""净网哨兵 Web 复核台包(A30)。

只包含 Streamlit 复核台 ``webui/app.py``。``import webui`` 本身不触发
streamlit 导入(app 模块内部惰性处理),以便在无 UI 依赖的环境里复用
``webui.app`` 的纯逻辑层(卡片组装 / 过滤 / 证据路径筛选等)。
"""
from __future__ import annotations

__all__ = ["app"]
