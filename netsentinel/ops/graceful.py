# -*- coding: utf-8 -*-
"""优雅退出协调器(NetSentinel V10.35 · P2-7)。

批量扫描(站点可能上百、单站墙钟数十秒到数分钟)此前没有任何信号处理:
Ctrl-C 在提交循环 / 聚合循环里硬抛 :class:`KeyboardInterrupt`,在飞站点的
VLM 外呼被拦腰打断(预算已扣、报告丢失),批内进度只剩 vlm_cache 的图级
幂等兜底。本模块把"中断"变成一次**可排空的收尾**:

- 第一次 Ctrl-C / SIGTERM → 置位内部标志(:meth:`GracefulExit.request`),
  扫描池(:mod:`netsentinel.ops.pool`)的提交循环在**下一个提交间隙**读到
  标志即停止提交新站,已提交的在飞任务**全部排空**(等待上界 = 单站在飞
  时长),批末摘要如实登记 ``interrupted=True`` 与未提交清单;
- 第二次 Ctrl-C / SIGTERM → 立即硬中断(信号处理器直接抛
  :class:`KeyboardInterrupt`),不再等待在飞任务——两段式是无人值守韧性
  的标准形态("先礼后兵"),等待与否永远由操作者按下几次决定。

设计约束:

- 信号处理器**只能在主线程注册**(:func:`signal.signal` 在非主线程抛
  ``ValueError``)——非主线程安装只置标志不注册处理器,并记中文告警
  (REST / WebUI 等以线程形态嵌入扫描的宿主不受影响,行为与接线前一致);
- 安装即记忆原处理器,:meth:`GracefulExit.restore` 原样交还(exit 语义
  干净,不污染宿主进程的信号语义);
- ``cfg.graceful_exit_enabled``(一等字段,缺省 True)关闭时宿主可不安装,
  批量链路回到"硬中断"旧行为;
- **绝不涉及**举报提交链路(人工门/频控红线不动);排空只针对扫描侧
  在飞任务,与对外礼貌间隔无关(红线 35)。

可观测性:每次收到信号累加 ``telemetry.inc("graceful.signal")``;首次
置位累加 ``graceful.requested``。只存名称与数字(红线 17)。

用法示例(批量宿主,离线)::

    from netsentinel.ops.graceful import graceful_exit

    with graceful_exit() as ex:
        summary = run_pool(cfg, items, stop_check=ex.should_stop)
    # Ctrl-C:池停接新站→排空→返回带 interrupted 的摘要;再次 Ctrl-C 硬退
"""
from __future__ import annotations

import logging
import signal
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from netsentinel import telemetry

__all__ = ["GracefulExit", "graceful_exit"]

logger = logging.getLogger(__name__)

#: 参与优雅退出的信号(Windows 下 SIGTERM 仅 os.kill 能发,注册无害)。
_SIGNALS: tuple[int, ...] = (signal.SIGINT, signal.SIGTERM)


class GracefulExit:
    """两段式中断协调器(线程安全的标志位 + 信号处理器安装/交还)。

    - :attr:`requested`:是否已收到(至少一次)退出请求;
    - :meth:`should_stop`:给扫描池提交循环的谓词(``stop_check`` 注入口);
    - :meth:`request`:置位(幂等);**第二次**调用抛
      :class:`KeyboardInterrupt`(硬中断,由信号回调或调用方直接触发);
    - :meth:`install` / :meth:`restore`:注册/交还 SIGINT+SIGTERM 处理器
      (仅主线程;非主线程 ValueError 捕获后仅告警,标志位照常可用)。
    """

    __slots__ = ("_lock", "_requested", "_count", "_orig", "_installed")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requested = False
        self._count = 0
        self._orig: dict[int, Any] = {}
        self._installed = False

    @property
    def requested(self) -> bool:
        """是否已收到退出请求(只读视图)。"""
        with self._lock:
            return self._requested

    @property
    def installed(self) -> bool:
        """信号处理器是否已注册(非主线程安装 = False)。"""
        return self._installed

    def should_stop(self) -> bool:
        """扫描池 ``stop_check`` 谓词:已请求退出即应停接新目标。"""
        return self.requested

    def request(self, reason: str = "") -> None:
        """登记一次退出请求;第二次起直接硬中断。

        - 首次:置位 ``requested``、中文 info 日志说明"停接新目标,
          排空在飞任务后退出";
        - 之后每次:记 warning 并抛 :class:`KeyboardInterrupt`——操作者
          连按两次即"不再等待在飞任务",等待权在人不在我。
        """
        with self._lock:
            self._count += 1
            first = not self._requested
            hard = self._count >= 2
            if first:
                self._requested = True
        telemetry.inc("graceful.signal")
        if first:
            telemetry.inc("graceful.requested")
            logger.info(
                "收到退出请求(%s):停止提交新目标,排空在飞任务后收尾",
                reason or "中断",
            )
            return
        logger.warning("再次收到退出请求(%s):立即硬中断,不再等待在飞任务", reason or "中断")
        raise KeyboardInterrupt(reason or "graceful hard interrupt")

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        """信号回调:转 :meth:`request`(第二次信号在处理器内抛硬中断)。"""
        self.request(f"signal {signum}")

    def install(self) -> "GracefulExit":
        """注册 SIGINT/SIGTERM 处理器并记忆原值(幂等;仅主线程生效)。

        非主线程(REST worker / 测试线程)调用时 :func:`signal.signal`
        抛 ``ValueError``——捕获后仅中文告警,不置 ``installed``,
        标志位与 :meth:`request` / :meth:`should_stop` 照常可用
        (宿主自行调用 ``request()`` 仍能触发优雅排空)。
        """
        if self._installed:
            return self
        try:
            for sig in _SIGNALS:
                self._orig[sig] = signal.signal(sig, self._handle_signal)
            self._installed = True
            logger.debug("优雅退出:SIGINT/SIGTERM 处理器已注册(主线程)")
        except ValueError:
            logger.warning(
                "当前线程非主线程,无法注册信号处理器:优雅退出仅保留内部"
                "标志位(宿主可自行调用 request() 触发排空)"
            )
        return self

    def restore(self) -> None:
        """把信号处理器原样交还给宿主进程(未安装时为空操作)。"""
        for sig, handler in self._orig.items():
            try:
                signal.signal(sig, handler)
            except ValueError:  # 非主线程交还:无害,处理器本就没装上
                pass
        self._orig.clear()
        self._installed = False

    def __enter__(self) -> "GracefulExit":
        return self.install()

    def __exit__(self, *_exc: Any) -> None:
        self.restore()


@contextmanager
def graceful_exit(install_signals: bool = True) -> Iterator[GracefulExit]:
    """批量宿主的便捷上下文:进入注册处理器,退出原样交还。

    ``install_signals=False`` 供测试/嵌入式宿主使用:只拿到协调器对象
    (``should_stop`` / ``request``),不碰进程信号表——两段式语义与
    安装形态完全一致。
    """
    ex = GracefulExit()
    if install_signals:
        ex.install()
    try:
        yield ex
    finally:
        ex.restore()
