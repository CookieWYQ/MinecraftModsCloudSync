# -*- coding: utf-8 -*-
"""服务端 - 日志页：查看日志、按时间段导出、配置管理（配置与日志均隐藏保存）。"""
from datetime import datetime

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from app_common import winutil
from app_common.config_manager import ConfigManagerDialog
from app_common.constants import logs_dir
from app_common.log_manager import LogManagerDialog
from app_common.logger import get_logger

log = get_logger("server.log_page")


class LogPage(QWidget):
    def __init__(self, config=None, parent=None):
        super().__init__(parent)
        self.config = config
        self._build()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(3000)
        self.refresh()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        top = QHBoxLayout()
        self.lbl_info = QLabel("")
        self.lbl_info.setObjectName("muted")
        btn_export = QPushButton("导出日志…")
        btn_export.clicked.connect(lambda: LogManagerDialog(self, title="日志导出").exec())
        btn_cfg = QPushButton("配置管理…")
        btn_cfg.clicked.connect(self._open_config_manager)
        btn_open = QPushButton("打开日志目录")
        btn_refresh = QPushButton("刷新")
        top.addWidget(self.lbl_info, 1)
        top.addWidget(btn_export)
        top.addWidget(btn_cfg)
        top.addWidget(btn_refresh)
        top.addWidget(btn_open)
        layout.addLayout(top)

        self.editor = QPlainTextEdit()
        self.editor.setReadOnly(True)
        layout.addWidget(self.editor, 1)

        btn_open.clicked.connect(lambda: winutil.open_in_explorer(logs_dir()))
        btn_refresh.clicked.connect(self.refresh)

    def _open_config_manager(self):
        if self.config is None:
            winutil.warn(self, "提示", "当前环境未提供配置对象。")
            return
        dlg = ConfigManagerDialog(self, store=self.config.store,
                                  title="服务端配置管理",
                                  on_restored=self._on_config_restored,
                                  note="配置包括：SFTP 服务器信息、远程目录、同步过滤规则、授权编号等。")
        dlg.exec()

    def _on_config_restored(self):
        winutil.info(self, "恢复完成", "服务端配置已从备份恢复。\n部分设置将在重启后完全生效。")

    def refresh(self):
        path = logs_dir() / f"{datetime.now().strftime('%Y%m%d')}.log"
        try:
            if path.exists():
                content = path.read_text(encoding="utf-8", errors="replace")
                self.editor.setPlainText(content)
                self.editor.verticalScrollBar().setValue(
                    self.editor.verticalScrollBar().maximum())
                self.lbl_info.setText(f"日志文件（隐藏目录）：{path}")
            else:
                self.lbl_info.setText("暂无日志")
        except Exception as exc:
            log.warning("读取日志失败: %s", exc)
