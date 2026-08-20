# -*- coding: utf-8 -*-
"""服务端 - SFTP 设置页：连接信息与远程目录，全部持久化保存。"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app_common import winutil
from app_common.logger import get_logger
from app_common.sftp import SFTPError, SFTPManager
from app_common.style import apply_style
from app_common.worker import Worker

log = get_logger("server.sftp_page")


class SFTPPage(QWidget):
    def __init__(self, config, main_window, parent=None):
        super().__init__(parent)
        self.config = config
        self.main_window = main_window
        self._worker = None
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        box = QGroupBox("SFTP 服务器连接")
        form = QFormLayout(box)
        form.setSpacing(10)

        self.ed_host = QLineEdit()
        self.ed_host.setPlaceholderText("例如 sftp.example.com 或 192.168.1.10")
        self.ed_port = QSpinBox()
        self.ed_port.setRange(1, 65535)
        self.ed_port.setValue(22)
        self.ed_user = QLineEdit()
        self.ed_password = QLineEdit()
        self.ed_password.setEchoMode(QLineEdit.Password)
        self.cb_show_pwd = QCheckBox("显示密码")

        form.addRow("服务器地址", self.ed_host)
        form.addRow("端口", self.ed_port)
        form.addRow("用户名", self.ed_user)
        pwd_row = QHBoxLayout()
        pwd_row.addWidget(self.ed_password)
        pwd_row.addWidget(self.cb_show_pwd)
        form.addRow("密码", pwd_row)

        remote_box = QGroupBox("远程目录（待办任务与客户端文件仓库）")
        remote_form = QFormLayout(remote_box)
        remote_form.setSpacing(10)
        self.ed_todo_dir = QLineEdit()
        self.ed_todo_dir.setPlaceholderText("例如 todo")
        self.ed_files_dir = QLineEdit()
        self.ed_files_dir.setPlaceholderText("例如 client_files")
        remote_form.addRow("待办任务目录", self.ed_todo_dir)
        remote_form.addRow("客户端文件目录", self.ed_files_dir)
        self.lbl_dir_hint = QLabel("目录均为 SFTP 根目录下的相对路径，创建后将自动建立。")
        self.lbl_dir_hint.setObjectName("muted")
        self.lbl_dir_hint.setWordWrap(True)
        remote_form.addRow(self.lbl_dir_hint)

        options_box = QGroupBox("启动选项")
        options_form = QFormLayout(options_box)
        self.cb_autostart = QCheckBox("开机自启动（最小化到托盘）")
        options_form.addRow(self.cb_autostart)

        btn_row = QHBoxLayout()
        self.btn_test = QPushButton("测试连接")
        self.btn_save = QPushButton("保存设置")
        self.btn_save.setObjectName("primary")
        btn_row.addWidget(self.btn_test)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_save)

        self.lbl_result = QLabel("")
        self.lbl_result.setObjectName("muted")
        self.lbl_result.setWordWrap(True)

        layout.addWidget(box)
        layout.addWidget(remote_box)
        layout.addWidget(options_box)
        layout.addLayout(btn_row)
        layout.addWidget(self.lbl_result)
        layout.addStretch(1)

        self.cb_show_pwd.stateChanged.connect(
            lambda s: self.ed_password.setEchoMode(
                QLineEdit.Normal if s else QLineEdit.Password
            ))
        self.btn_test.clicked.connect(self._on_test)
        self.btn_save.clicked.connect(self._on_save)

        self._load_from_config()

    def _load_from_config(self):
        info = self.config.sftp
        self.ed_host.setText(info.get("host", ""))
        self.ed_port.setValue(self.config.port())
        self.ed_user.setText(info.get("username", ""))
        self.ed_password.setText(info.get("password", ""))
        self.ed_todo_dir.setText(self.config.todo_dir)
        self.ed_files_dir.setText(self.config.files_dir)
        self.cb_autostart.setChecked(winutil.is_autostart_enabled())
        self.cb_dark.setChecked(self.config.theme == "dark")

    def _on_theme_toggled(self, checked: bool):
        self.config.theme = "dark" if checked else "light"
        app = QApplication.instance()
        if app is not None:
            apply_style(app, checked)

    def _collect(self) -> dict:
        return {
            "sftp": {
                "host": self.ed_host.text().strip(),
                "port": self.ed_port.value(),
                "username": self.ed_user.text().strip(),
                "password": self.ed_password.text(),
            },
            "remote": {
                "todo_dir": self.ed_todo_dir.text().strip() or "todo",
                "files_dir": self.ed_files_dir.text().strip() or "client_files",
            },
        }

    def _on_test(self):
        data = self._collect()
        host = data["sftp"]["host"]
        if not host:
            winutil.warn(self, "提示", "请先填写服务器地址。")
            return
        if not winutil.confirm(self, "确认操作",
                               f"即将测试连接 {host}:{data['sftp']['port']}，是否继续？"):
            return
        self.btn_test.setEnabled(False)
        self.lbl_result.setText("正在测试连接…")
        worker = Worker(self._test_conn, data)
        worker.done.connect(self._on_test_done)
        self._worker = worker
        worker.start()

    def _test_conn(self, data, progress_cb=None):
        with SFTPManager(**data["sftp"]) as sftp:
            todo = data["remote"]["todo_dir"]
            files = data["remote"]["files_dir"]
            sftp.mkdirs(todo)
            sftp.mkdirs(files)
            exists = sftp.exists(todo) and sftp.exists(files)
        return ("连接成功，待办目录与文件目录可用" if exists
                else "连接成功，但远程目录不可用")

    def _on_test_done(self, ok: bool, msg: str):
        self.btn_test.setEnabled(True)
        self.main_window.set_sftp_status(ok)
        if ok:
            self.lbl_result.setText("连接成功 ✔")
            winutil.info(self, "测试结果", msg)
        else:
            self.lbl_result.setText("连接失败 ✘")
            winutil.error(self, "测试结果", f"SFTP 连接失败：\n{msg}")

    def _on_save(self):
        data = self._collect()
        if not data["sftp"]["host"]:
            winutil.warn(self, "提示", "服务器地址不能为空。")
            return
        if not winutil.confirm(self, "确认操作",
                               "即将保存 SFTP 服务器设置（含密码），是否继续？"):
            return
        self.config.sftp = data["sftp"]
        self.config.remote = data["remote"]
        self.config.window = self.main_window.window_state()
        # 开机自启
        if self.cb_autostart.isChecked() != winutil.is_autostart_enabled():
            winutil.set_autostart(self.cb_autostart.isChecked())
        self.lbl_result.setText("设置已保存 ✔")
        winutil.info(self, "保存成功", "SFTP 设置已持久化保存。")
        log.info("SFTP 设置已保存")
