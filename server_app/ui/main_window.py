# -*- coding: utf-8 -*-
"""服务端 - 主窗口：标签页 + 系统托盘 + 窗口状态持久化。"""
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QTabWidget,
)

from app_common import winutil
from app_common.app_icon import get_app_icon
from app_common.constants import APP_DISPLAY_NAME
from app_common.logger import get_logger
from app_common.settings_dialog import SettingsDialog

from .export_page import ExportPage
from .log_page import LogPage
from .sftp_page import SFTPPage
from .sync_page import SyncPage
from .todo_page import TodoPage

log = get_logger("server.main_window")


class ServerMainWindow(QMainWindow):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self._build()
        self._restore_state()

    def _build(self):
        self.setWindowTitle(f"{APP_DISPLAY_NAME} - 服务端工具")
        self.setMinimumSize(QSize(920, 620))
        self.setAcceptDrops(True)
        self.setWindowIcon(get_app_icon("server"))

        self.tabs = QTabWidget()
        self.sftp_page = SFTPPage(self.config, self)
        self.todo_page = TodoPage(self.config, status_cb=self.set_sftp_status)
        self.sync_page = SyncPage(self.config, status_cb=self.set_sftp_status)
        self.export_page = ExportPage(self.config, status_cb=self.set_sftp_status)
        self.log_page = LogPage(self.config)
        self.tabs.addTab(self.sftp_page, "SFTP 设置")
        self.tabs.addTab(self.todo_page, "待办任务发布")
        self.tabs.addTab(self.sync_page, "文件同步")
        self.tabs.addTab(self.export_page, "导出客户端配置")
        self.tabs.addTab(self.log_page, "日志")
        self.setCentralWidget(self.tabs)

        self.statusBar().showMessage("就绪")
        self.lbl_sftp_status = QLabel("SFTP：未检测")
        self.statusBar().addPermanentWidget(self.lbl_sftp_status)

        # 软件本体设置（主题等），与服务端档案配置区分
        btn_settings = QPushButton("设置…")
        btn_settings.clicked.connect(self._open_settings)
        self.statusBar().addPermanentWidget(btn_settings)

        # 托盘
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(get_app_icon("server"), self)
            self.tray.setToolTip(APP_DISPLAY_NAME)
            menu = QMenu()
            act_show = QAction("显示主界面", self)
            act_show.triggered.connect(self._show_window)
            act_quit = QAction("退出", self)
            act_quit.triggered.connect(self._quit)
            menu.addAction(act_show)
            menu.addSeparator()
            menu.addAction(act_quit)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self._on_tray_activated)
            self.tray.show()

    def set_sftp_status(self, ok: bool):
        """更新状态栏中的 SFTP 连通状态。"""
        self.lbl_sftp_status.setText(f"SFTP：{'已连通' if ok else '连接失败'}")

    def _open_settings(self):
        """打开软件本体设置子窗口（主题等）。"""
        dlg = SettingsDialog(self, config=self.config, is_client=False)
        dlg.exec()

    # ---------- 拖拽（版本文件夹 / .minecraft / 启动器快捷方式） ----------
    def dragEnterEvent(self, event):
        if self._dropped_path(event) is not None:
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if self._dropped_path(event) is not None:
            event.acceptProposedAction()

    def dropEvent(self, event):
        path = self._dropped_path(event)
        if path:
            self.tabs.setCurrentWidget(self.sync_page)
            self.sync_page.handle_dropped(path)
            event.acceptProposedAction()

    @staticmethod
    def _dropped_path(event) -> str | None:
        urls = event.mimeData().urls()
        for url in urls:
            if url.isLocalFile():
                return url.toLocalFile()
        return None

    # ---------- 窗口状态 ----------
    def _restore_state(self):
        win = self.config.window
        if win.get("width"):
            self.resize(win.get("width", 980), win.get("height", 680))
        if win.get("tab") is not None:
            self.tabs.setCurrentIndex(min(int(win.get("tab", 0)),
                                          self.tabs.count() - 1))
        self.tabs.currentChanged.connect(self._on_tab_changed)

    def _on_tab_changed(self, index: int):
        self.config.window = {**self.config.window, "tab": index}

    def window_state(self) -> dict:
        return {
            "width": self.width(),
            "height": self.height(),
            "tab": self.tabs.currentIndex(),
        }

    # ---------- 托盘 ----------
    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._show_window()

    def closeEvent(self, event):
        if self.tray is not None:
            event.ignore()
            self.hide()
            self.tray.showMessage(APP_DISPLAY_NAME, "程序已最小化到托盘，双击图标恢复。",
                                  QSystemTrayIcon.Information, 3000)
        else:
            self._save_state()
            event.accept()

    def _save_state(self):
        self.config.window = self.window_state()

    def _quit(self):
        if not winutil.confirm(self, "确认退出", "确定退出服务端工具吗？"):
            return
        self._save_state()
        self.tray.hide()
        QApplication.quit()
