# -*- coding: utf-8 -*-
"""通用后台任务线程（QThread），避免网络操作阻塞界面。"""
from PySide6.QtCore import QThread, Signal

from .logger import get_logger

log = get_logger("worker")

# 保持对所有已启动 Worker 的强引用，直到其线程结束。
# 若 Worker 的 Python 包装对象被提前回收（例如 self._worker 被下一个任务覆盖），
# 而 QThread 仍在运行，Qt 会触发 "QThread: Destroyed while thread is still running"
# 的 qFatal（异常 0xc0000409），导致程序静默崩溃。
_live_workers: set = set()


def fmt_progress(current: int, total: int, message: str = "", prefix: str = "") -> str:
    """统一进度文本：`前缀 当前/总（xx%）消息`。

    无法计算百分比（total<=0，如目录树扫描）时只显示消息。
    例：fmt_progress(3, 5, "正在分析 create.jar…", "分析模组环境")
        → "分析模组环境 3/5（60%）正在分析 create.jar…"
    """
    if total > 0:
        pct = int(current * 100 / total)
        text = f"{current}/{total}（{pct}%）"
        if prefix:
            text = f"{prefix} {text}"
        return f"{text} {message}".rstrip()
    return f"{prefix} {message}".rstrip()


class Worker(QThread):
    """在线程中执行 fn(*args, progress_cb=...)，完成后通过信号回调。"""

    done = Signal(bool, str)          # (是否成功, 结果/错误信息)
    progress = Signal(int, int, str)  # (当前, 总数, 消息)

    def __init__(self, fn, *args, parent=None, **kwargs):
        super().__init__(parent)
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def start(self, priority=QThread.InheritPriority):
        _live_workers.add(self)
        self.finished.connect(self._on_finished)
        super().start(priority)

    def _on_finished(self):
        """线程结束后释放引用，允许对象正常回收。"""
        _live_workers.discard(self)

    def run(self) -> None:
        try:
            result = self._fn(*self._args, progress_cb=self._emit_progress)
            self.done.emit(True, result or "")
        except Exception as exc:
            log.exception("后台任务执行失败")
            self.done.emit(False, str(exc))

    def _emit_progress(self, current: int, total: int, message: str) -> None:
        self.progress.emit(current, total, message)
