# -*- coding: utf-8 -*-
"""跨平台原子文件替换微工具(V10.12 优化升级波)。

背景:``os.replace`` 在 Windows 上当目标文件被其他进程/线程短暂持有
(典型:webui 正读旧档、杀毒软件扫描、两个 CLI 进程并发写同一状态文件)
时抛 ``PermissionError``——POSIX 上替换是原子的、该失败模式不存在。
``ops/tier_state.py`` 早已为档位账本实现了"有限重试"的同一语义;本模块
把它提为公开工具,供全部状态文件/存档写回点统一使用:

- 成功路径与裸 ``os.replace`` 完全一致(一次成功立即返回);
- 仅 ``PermissionError`` 重试(总预算 ≤ 2 秒),其余异常原样上抛;
- POSIX 平台重试永不触发,行为逐字节不变。

用法::

    from netsentinel.atomic import replace_with_retry

    replace_with_retry(tmp_path, target_path)
"""
from __future__ import annotations

import os
import time
from pathlib import Path

__all__ = ["REPLACE_ATTEMPTS", "REPLACE_WAIT_S", "replace_with_retry"]

#: 重试次数与间隔(与 ops.tier_state 同参:40 × 0.05s ≈ 2s 总预算)
REPLACE_ATTEMPTS: int = 40
REPLACE_WAIT_S: float = 0.05


def replace_with_retry(
    src: str | os.PathLike[str] | Path,
    dst: str | os.PathLike[str] | Path,
    *,
    attempts: int = REPLACE_ATTEMPTS,
    wait_s: float = REPLACE_WAIT_S,
    sleep: "time.sleep" = time.sleep,  # type: ignore[valid-type]
) -> None:
    """``os.replace(src, dst)``;Windows 目标被短暂占用时有限重试后放弃。

    - 仅捕获 :class:`PermissionError`(Windows 共享冲突的稳定信号);
      重试耗尽后把最后一次异常原样上抛(绝不静默丢状态);
    - 其余 ``OSError``(如磁盘满、路径非法)不重试,首错即抛;
    - ``sleep`` 仅供测试注入(缺省 :func:`time.sleep`)。
    """
    for attempt in range(max(1, int(attempts))):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt >= int(attempts) - 1:
                raise
            sleep(float(wait_s))
