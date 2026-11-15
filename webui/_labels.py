"""净网哨兵 · Web 页面共享标签口径(单一权威定义)。

纯常量模块:不导入 streamlit、不依赖任何兄弟模块,任意页面顶层导入
不影响"页面可独立运行"的约定(各页仍可用
``try: from webui._labels import ... / except ImportError: from _labels import ...``
双路导入兜底脚本直跑场景)。

背景(本次统一):此前各页中文口径不一致——app.py 把 clean 译"未发现",
groups_page.py 译"无风险",runs_page.py 译"未发现"。统一为更准确的措辞:

- clean → **未见违规**:机器初筛"未发现违规内容"是**证据范围内的否定
  结论**,不等于"无风险"(站点未被扫描到的部分无从谈起),措辞如实;
- suspect → 疑似;nsfw → 高置信(两档历史各页一致,原样保留)。

各页历史常量名(``VERDICT_CN_MAP`` / ``VERDICT_LABELS_CN`` / ``_STATUS_CN``
等)以赋值别名方式指向本模块同一字典对象,对外行为兼容。
"""
from __future__ import annotations

__all__ = ["STATUS_CN", "VERDICT_CN"]

#: 复核状态 → 中文(四态,与 decision.review_queue 状态机一致)。
STATUS_CN: dict[str, str] = {
    "pending": "待复核",
    "approved": "已批准",
    "rejected": "已驳回",
    "submitted": "已提交",
}

#: 判定档位 → 中文(统一口径:未见违规 / 疑似 / 高置信)。
VERDICT_CN: dict[str, str] = {
    "clean": "未见违规",
    "suspect": "疑似",
    "nsfw": "高置信",
}
