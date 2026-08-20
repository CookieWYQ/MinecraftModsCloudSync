# -*- coding: utf-8 -*-
"""设置窗口：软件本体设置（主题 / 开机自启 / 自动检查间隔 / 系统通知）。

与「配置管理」区分：设置针对软件本体，配置针对每个服务器档案/文件数据。
"""
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QSpinBox,
    QVBoxLayout,
)

from . import winutil
from .style import apply_style, theme_is_dark
from .logger import get_logger

log = get_logger("settings_dialog")


class SettingsDialog(QDialog):
    """编辑软件本体设置。is_client 为 True 时额外提供客户端专属项。"""

    def __init__(self, parent=None, config=None, is_client: bool = True,
                 on_changed=None):
        super().__init__(parent)
        self.config = config
        self.is_client = is_client
        self.on_changed = on_changed
        self.setWindowTitle("设置")
        self.setMinimumWidth(460)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)

        box = QGroupBox("外观")
        form = QFormLayout(box)
        form.setSpacing(10)
        self.cb_theme = QComboBox()
        self.cb_theme.addItem("跟随系统", "system")
        self.cb_theme.addItem("浅色模式", "light")
        self.cb_theme.addItem("深色模式", "dark")
        idx = self.cb_theme.findData(self.config.theme if self.config else "system")
        if idx < 0:
            idx = 0
        self.cb_theme.setCurrentIndex(idx)
        self.cb_theme.currentIndexChanged.connect(self._preview_theme)
        form.addRow("主题", self.cb_theme)
        layout.addWidget(box)

        general = QGroupBox("常规")
        gform = QFormLayout(general)
        gform.setSpacing(10)
        self.cb_autostart = None
        if self.is_client:
            self.cb_autostart = QCheckBox("开机自启动（最小化到托盘）")
            if self.config is not None:
                self.cb_autostart.setChecked(winutil.is_autostart_enabled())
            gform.addRow(self.cb_autostart)

        self.sp_interval = None
        self.cb_notify = None
        if self.is_client:
            self.sp_interval = QSpinBox()
            self.sp_interval.setRange(5, 1440)
            self.sp_interval.setSuffix(" 分钟")
            if self.config is not None:
                self.sp_interval.setValue(self.config.check_interval_min)
            gform.addRow("自动检查间隔", self.sp_interval)

            self.cb_notify = QCheckBox("发现更新/更新完成时弹出系统通知")
            if self.config is not None:
                self.cb_notify.setChecked(self.config.notify)
            gform.addRow(self.cb_notify)
        layout.addWidget(general)

        tip = QLabel("提示：本窗口管理软件本体设置；每个服务器对应的档案与数据请使用「配置管理」。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _preview_theme(self, _index: int):
        if self.config is None:
            return
        theme = self.cb_theme.currentData()
        self.config.theme = theme
        app = QApplication.instance()
        if app is not None:
            apply_style(app, theme_is_dark(theme))

    def _save(self):
        if self.config is None:
            self.accept()
            return
        theme = self.cb_theme.currentData()
        self.config.theme = theme
        # 开机自启（仅客户端支持；服务端走安装器自启项）
        if self.cb_autostart is not None:
            autostart = self.cb_autostart.isChecked()
            self.config.autostart = autostart
            winutil.set_autostart(autostart)
        # 客户端专属
        interval_changed = False
        if self.sp_interval is not None:
            self.config.check_interval_min = self.sp_interval.value()
            interval_changed = True
        if self.cb_notify is not None:
            self.config.notify = self.cb_notify.isChecked()
        if self.on_changed is not None:
            try:
                self.on_changed(interval_changed)
            except Exception as exc:
                log.warning("设置回调执行失败: %s", exc)
        log.info("软件设置已保存（主题=%s, 自启=%s）", theme, autostart)
        self.accept()
