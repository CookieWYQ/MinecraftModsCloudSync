# -*- coding: utf-8 -*-
"""服务端 - 日志页：按天查看日志、按时间段导出、配置管理（配置与日志均隐藏保存）。"""
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app_common import winutil
from app_common.config_manager import ConfigManagerDialog
from app_common.log_manager import LogManagerDialog
from app_common.logger import (
    active_logs_dir,
    available_log_days,
    get_logger,
    log_files_for_date,
)

log = get_logger("server.log_page")


class LogPage(QWidget):
    def __init__(self, config=None, parent=None):
        super().__init__(parent)
        self.config = config
        self._days: list[tuple[str, str]] = []  # [(日期 YYYYMMDD, 首个文件)]
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
        top.addWidget(self.lbl_info, 1)

        top.addWidget(QLabel("查看日期："))
        self.cb_day = QComboBox()
        self.cb_day.setMinimumWidth(150)
        self.cb_day.setToolTip("选择查看哪一天的日志")
        self.cb_day.currentIndexChanged.connect(self._load_selected)
        top.addWidget(self.cb_day)

        btn_export = QPushButton("导出日志…")
        btn_export.clicked.connect(lambda: LogManagerDialog(self, title="日志导出").exec())
        btn_cfg = QPushButton("配置管理…")
        btn_cfg.clicked.connect(self._open_config_manager)
        btn_open = QPushButton("打开日志目录")
        btn_refresh = QPushButton("刷新")
        top.addWidget(btn_export)
        top.addWidget(btn_cfg)
        top.addWidget(btn_refresh)
        top.addWidget(btn_open)
        layout.addLayout(top)

        self.editor = QPlainTextEdit()
        self.editor.setReadOnly(True)
        layout.addWidget(self.editor, 1)

        btn_open.clicked.connect(
            lambda: winutil.open_in_explorer(active_logs_dir()))
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

    def reload(self):
        """切换服务器/上下文后刷新日期列表并加载日志。"""
        self.refresh()

    def _refresh_days(self):
        """刷新日期下拉框（保持当前选择；无选择时默认选最新一天）。"""
        current = self.cb_day.currentData()
        days = available_log_days()
        self.cb_day.blockSignals(True)
        self.cb_day.clear()
        for date, path in days:
            self.cb_day.addItem(f"{date[:4]}-{date[4:6]}-{date[6:]}", date)
        if days:
            idx = self.cb_day.findData(current)
            if idx < 0:
                idx = 0  # 最新一天（列表已按日期倒序）
            self.cb_day.setCurrentIndex(idx)
        self.cb_day.blockSignals(False)
        self._days = days

    def _load_selected(self, _index=None):
        """用户切换日期时立即加载该天的日志。"""
        if self.editor.textCursor().hasSelection():
            return
        self._load_content()

    def _load_content(self):
        date = self.cb_day.currentData()
        if not date:
            content, files = "", []
        else:
            files = log_files_for_date(date)
            parts = []
            for path in files:
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        parts.append(f.read())
                except OSError as exc:
                    log.warning("读取日志失败 %s: %s", path, exc)
            content = "".join(parts)
        if self.editor.toPlainText() != content:
            self.editor.setPlainText(content)
            self.editor.verticalScrollBar().setValue(
                self.editor.verticalScrollBar().maximum())
        if files:
            self.lbl_info.setText(f"日志文件（隐藏目录）：{files[-1]}")
        else:
            self.lbl_info.setText("暂无日志")

    def refresh(self):
        # 若用户正选中文本（准备复制），暂停刷新，避免打断选择
        if self.editor.textCursor().hasSelection():
            return
        self._refresh_days()
        self._load_content()
