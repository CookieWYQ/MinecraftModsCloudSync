# -*- coding: utf-8 -*-
"""检查更新线程生命周期回归测试（Qt，无需联网、无需显示窗口）。

复现线上崩溃：
  第一次「检查更新」结束后 QThread.deleteLater() 销毁了 C++ 对象，但主窗口属性
  self._update_thread 仍指向失效的 Python 包装对象；第二次点「检查更新」时
  isRunning() 抛出
      RuntimeError: Internal C++ object (UpdateCheckThread) already deleted.
  异常冒泡到 sys.excepthook，表现为程序日志里连续出现「未捕获异常导致程序退出」。

修复后 _check_update() 在旧线程已结束时清空引用，并用 track_thread() 在 finished
时自动清空，因此可以反复检查更新。

用法（仓库根目录）：
    python tests/test_update_thread_lifecycle.py
"""
import os
import sys
from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QThread, Signal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import server_app.ui.main_window as server_mw
import client_app.ui.main_window as client_mw


class _FastCheckThread(QThread):
    """替代 UpdateCheckThread：立即结束，不联网。"""

    done = Signal(bool, object, str)
    runs = 0

    def __init__(self, parent=None):
        super().__init__(parent)

    def run(self):
        type(self).runs += 1
        self.done.emit(True, {"latest": None, "releases": [], "checked_at": ""}, "")


class _FakeWindow(QObject):
    def __init__(self):
        super().__init__()
        self.config = SimpleNamespace(auto_update_check=True)
        self._update_thread = None
        self._update_manual = False
        self.checked = 0

    def _on_update_checked(self, *_args):
        self.checked += 1


def _pump(app: QCoreApplication, times: int = 6) -> None:
    for _ in range(times):
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, int(QEvent.DeferredDelete))


def _exercise(app: QCoreApplication, module, label: str) -> None:
    old = module.UpdateCheckThread
    module.UpdateCheckThread = _FastCheckThread
    _FastCheckThread.runs = 0
    try:
        win = _FakeWindow()
        for i in range(3):
            module.ServerMainWindow._check_update(win, manual=False) \
                if module is server_mw else \
                module.ClientMainWindow._check_update(win, manual=False)
            thread = win._update_thread
            assert thread is not None, f"{label}: 第 {i + 1} 次未启动线程"
            assert thread.wait(3000), f"{label}: 第 {i + 1} 次线程未结束"
            _pump(app)
            # 线程结束后引用应已被 track_thread 清空（或至少不再可访问 C++ 对象）
            assert win._update_thread is None, \
                f"{label}: 第 {i + 1} 次结束后引用未清空"
        assert _FastCheckThread.runs == 3, _FastCheckThread.runs
        print(f"{label}: 连续检查 3 次均正常，引用已清空，无 RuntimeError")
    finally:
        module.UpdateCheckThread = old


def main() -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    _exercise(app, server_mw, "服务端")
    _exercise(app, client_mw, "客户端")
    print("\nUPDATE THREAD LIFECYCLE OK")


if __name__ == "__main__":
    main()
