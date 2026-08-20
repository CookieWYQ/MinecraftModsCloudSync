# -*- coding: utf-8 -*-
"""通用后台任务线程（QThread），避免网络操作阻塞界面。"""
from PySide6.QtCore import QThread, Signal

from .logger import get_logger

log = get_logger("worker")


class Worker(QThread):
    """在线程中执行 fn(*args, progress_cb=...)，完成后通过信号回调。"""

    done = Signal(bool, str)          # (是否成功, 结果/错误信息)
    progress = Signal(int, int, str)  # (当前, 总数, 消息)

    def __init__(self, fn, *args, parent=None, **kwargs):
        super().__init__(parent)
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:
        try:
            result = self._fn(*self._args, progress_cb=self._emit_progress)
            self.done.emit(True, result or "")
        except Exception as exc:
            log.exception("后台任务执行失败")
            self.done.emit(False, str(exc))

    def _emit_progress(self, current: int, total: int, message: str) -> None:
        self.progress.emit(current, total, message)
