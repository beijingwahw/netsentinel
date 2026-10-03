"""NetSentinel 命令行交互组件(纯标准库,无 curses)。

当前提供 A57 的交互式终端复核 TUI::

    python -m netsentinel.cli.review_tui --db data/review_queue.db --four-eyes

刻意不在包级 ``__init__`` 里重导出子模块,避免 ``import netsentinel.cli``
时连带建立 SQLite 连接;按需直接导入 ``netsentinel.cli.review_tui``。
"""
from __future__ import annotations

__all__ = ["review_tui"]
