# -*- coding: utf-8 -*-
"""服务端 - 服务器设置页：每台服务器独立的 SFTP 连接、远程目录、本地/服务端根目录、差异规则与下发设置。

「SFTP 设置」与「服务器设置」合并为一个板块：连接信息、远程目录、本地与服务端根目录、
差异排除、客户端模组、客户端软件下发设置与服务端工具自启全部收拢在这里，
「发布待办（S2C）」与「更新服务端（C2S）」页面只保留目录树与操作。
软件开启或切换服务器时，主窗口会调用 auto_check() 自动检测当前服务器的 SFTP 连接。
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app_common import winutil
from app_common.c2c import abs_c2c_dir, register_code
from app_common.launcher import KnownVersions
from app_common.logger import get_logger
from app_common.sftp import SFTPManager
from app_common.version_dialog import VersionPickerDialog
from app_common.worker import Worker

from .remote_page import DEFAULT_EXCLUDE

log = get_logger("server.settings_page")


def _abs_dir(value: str, default: str) -> str:
    """远程目录统一为绝对路径（以 / 开头）。"""
    p = (value or "").strip().replace("\\", "/")
    if not p:
        return default
    if not p.startswith("/"):
        p = "/" + p.lstrip("/")
    return p


class ServerSettingsPage(QWidget):
    """服务器设置：SFTP 连接 / 远程目录 / 本地与服务端根目录 / 差异排除 / 客户端模组 / 下发设置。

    所有字段均作用于当前选中的服务器（config 自动跟随切换）；保存后发出 saved 信号，
    主窗口据此刷新各工作流页面；auto_check() 供主窗口启动/切换服务器时自动检测连接。
    """

    saved = Signal()

    def __init__(self, config, main_window=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.main_window = main_window
        self._worker = None
        self._auto_worker = None
        self._build()
        self.reload()

    def _build(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        container = QWidget()
        # 滚动区域与容器跟随全局主题背景，避免使用系统默认底色（深浅色主题下发白）
        scroll.viewport().setAutoFillBackground(False)
        container.setAutoFillBackground(False)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # ---- SFTP 服务器连接 ----
        conn_box = QGroupBox("SFTP 服务器连接（开启软件时自动检测连通性）")
        form = QFormLayout(conn_box)
        form.setSpacing(8)
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
        conn_btn_row = QHBoxLayout()
        self.btn_test = QPushButton("测试连接")
        conn_btn_row.addWidget(self.btn_test)
        conn_btn_row.addStretch(1)
        self.lbl_conn = QLabel("")
        self.lbl_conn.setObjectName("muted")
        conn_btn_row.addWidget(self.lbl_conn)
        form.addRow(conn_btn_row)
        layout.addWidget(conn_box)

        # ---- 远程目录（合并：服务端文件仓库根目录 + 待办目录 + 客户端文件目录，均为绝对路径） ----
        remote_box = QGroupBox("远程目录（服务器上的绝对路径，连接时自动建立）")
        remote_form = QFormLayout(remote_box)
        remote_form.setSpacing(8)
        self.ed_server_root = QLineEdit()
        self.ed_server_root.setPlaceholderText("例如 / 或 /files")
        self.ed_server_root.setToolTip("服务端文件仓库的实际根目录（服务端文件所在位置）")
        self.ed_todo_dir = QLineEdit()
        self.ed_todo_dir.setPlaceholderText("例如 /todo")
        self.ed_files_dir = QLineEdit()
        self.ed_files_dir.setPlaceholderText("例如 /client_files")
        remote_form.addRow("服务端文件仓库根目录", self.ed_server_root)
        remote_form.addRow("待办任务目录", self.ed_todo_dir)
        remote_form.addRow("客户端文件目录", self.ed_files_dir)
        dir_hint = QLabel("以上目录均为服务器上的绝对路径，保存后自动建立。\n"
                          "客户端文件目录可自由指定（不再固定在服务端根目录之下）。")
        dir_hint.setObjectName("muted")
        dir_hint.setWordWrap(True)
        remote_form.addRow(dir_hint)
        layout.addWidget(remote_box)

        # ---- 本地新客户端根目录 ----
        local_box = QGroupBox("本地新客户端根目录（制作好的客户端，用于对比并上传服务端）")
        lr = QHBoxLayout(local_box)
        self.ed_local = QLineEdit()
        self.ed_local.setReadOnly(True)
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._browse)
        btn_versions = QPushButton("选择已知版本…")
        btn_versions.clicked.connect(self._choose_known_version)
        lr.addWidget(self.ed_local, 1)
        lr.addWidget(btn_versions)
        lr.addWidget(btn_browse)
        layout.addWidget(local_box)

        # ---- C2C（本地对本地）发布设置 ----
        c2c_box = QGroupBox("C2C（本地对本地）发布设置")
        c2c_form = QFormLayout(c2c_box)
        c2c_form.setSpacing(8)
        code_row = QHBoxLayout()
        self.ed_server_code = QLineEdit()
        self.ed_server_code.setPlaceholderText("本服务端唯一的代号，如 admin-01")
        self.ed_server_code.setToolTip("代号用于在 C2C 名单中区分各服务端，登记后全局唯一")
        code_row.addWidget(self.ed_server_code, 1)
        self.btn_register_code = QPushButton("登记代号并上传名单")
        self.btn_register_code.setToolTip("把代号写入服务器 C2C 目录下的名单（roster.json），重复代号会被拒绝")
        self.btn_register_code.clicked.connect(self._register_code)
        code_row.addWidget(self.btn_register_code)
        c2c_form.addRow("服务端代号", code_row)
        self.lbl_code_status = QLabel("")
        self.lbl_code_status.setObjectName("muted")
        self.lbl_code_status.setWordWrap(True)
        c2c_form.addRow(self.lbl_code_status)
        self.ed_c2c_dir = QLineEdit()
        self.ed_c2c_dir.setPlaceholderText("例如 /c2c")
        self.ed_c2c_dir.setToolTip("C2C 名单、客户端文件与清单都单独存放在该目录下")
        c2c_form.addRow("C2C 远程目录", self.ed_c2c_dir)
        c2c_hint = QLabel("说明：代号在所有服务端中必须唯一。登记后写入 C2C 远程目录下的名单文件"
                          "（roster.json，名单单独存放），相同代号会被拒绝；"
                          "客户端文件与清单也按客户端编号单独存放在该目录下。")
        c2c_hint.setObjectName("muted")
        c2c_hint.setWordWrap(True)
        c2c_form.addRow(c2c_hint)
        layout.addWidget(c2c_box)

        # ---- 差异排除 ----
        excl_box = QGroupBox("差异审核排除（logs、cache 等硬性跳过，每行一个，忽略大小写）")
        er = QVBoxLayout(excl_box)
        er.setSpacing(4)
        self.ed_exclude = QPlainTextEdit()
        self.ed_exclude.setPlaceholderText("每行一个，如：\nlogs\ncache")
        self.ed_exclude.setMaximumHeight(90)
        er.addWidget(self.ed_exclude)
        layout.addWidget(excl_box)

        # ---- 客户端模组关键词 ----
        mod_box = QGroupBox("客户端模组关键词（文件名包含即自动标注为「客户端」，只发 client_files）")
        mr = QVBoxLayout(mod_box)
        mr.setSpacing(4)
        self.ed_client_mods = QPlainTextEdit()
        self.ed_client_mods.setPlaceholderText("每行一个，如：\n3dskinlayers\nxaeros")
        self.ed_client_mods.setMaximumHeight(80)
        mr.addWidget(self.ed_client_mods)
        layout.addWidget(mod_box)

        # ---- 客户端软件设置（随待办下发） ----
        push_box = QGroupBox("客户端软件设置（随「发布待办」下发到客户端）")
        pv = QHBoxLayout(push_box)
        self.cb_push = QCheckBox("下发客户端软件设置")
        pv.addWidget(self.cb_push)
        pv.addSpacing(8)
        pv.addWidget(QLabel("自动检查间隔"))
        self.sp_interval = QSpinBox()
        self.sp_interval.setRange(5, 1440)
        self.sp_interval.setValue(60)
        self.sp_interval.setSuffix(" 分钟")
        pv.addWidget(self.sp_interval)
        pv.addSpacing(8)
        self.cb_push_autostart = QCheckBox("开机自启")
        self.cb_push_autostart.setToolTip("下发后，客户端将写入/清除开机自启动注册表")
        pv.addWidget(self.cb_push_autostart)
        self.cb_notify = QCheckBox("系统通知")
        pv.addWidget(self.cb_notify)
        pv.addStretch(1)
        layout.addWidget(push_box)

        # ---- 服务端工具选项 ----
        tool_box = QGroupBox("服务端工具选项")
        tr = QHBoxLayout(tool_box)
        self.cb_self_autostart = QCheckBox("开机自启动（最小化到托盘）")
        tr.addWidget(self.cb_self_autostart)
        tr.addStretch(1)
        layout.addWidget(tool_box)

        # ---- 保存 ----
        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self.btn_save = QPushButton("保存设置")
        self.btn_save.setObjectName("primary")
        self.btn_save.setToolTip("保存后，「发布待办」「更新服务端」页将立即使用新配置；\n"
                                 "未填写服务器地址时无法保存")
        self.btn_save.clicked.connect(self._save)
        save_row.addWidget(self.btn_save)
        self.lbl_hint = QLabel("")
        self.lbl_hint.setObjectName("muted")
        save_row.addWidget(self.lbl_hint)
        layout.addLayout(save_row)
        layout.addStretch(1)

        self.cb_show_pwd.stateChanged.connect(
            lambda s: self.ed_password.setEchoMode(
                QLineEdit.Normal if s else QLineEdit.Password))
        self.btn_test.clicked.connect(self._on_test)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        scroll.setWidget(container)

    # ---------- 加载 / 保存 ----------
    def reload(self):
        """切换服务器后加载当前服务器配置。"""
        info = self.config.sftp
        self.ed_host.setText(info.get("host", ""))
        self.ed_port.setValue(self.config.port())
        self.ed_user.setText(info.get("username", ""))
        self.ed_password.setText(info.get("password", ""))
        self.ed_todo_dir.setText(self.config.todo_dir)
        self.ed_files_dir.setText(self.config.files_dir)
        self.ed_server_code.setText(self.config.server_code)
        self.ed_c2c_dir.setText(self.config.c2c_dir)
        self.lbl_code_status.setText("")
        self.cb_self_autostart.setChecked(winutil.is_autostart_enabled())
        self.ed_local.setText(self.config.local_mc_dir)
        self.ed_server_root.setText(self.config.server_root)
        self.ed_exclude.setPlainText(
            "\n".join(self.config.exclude_names or sorted(DEFAULT_EXCLUDE)))
        self.ed_client_mods.setPlainText("\n".join(self.config.client_mods))
        ps = self.config.push_settings
        self.cb_push.setChecked(bool(ps.get("enabled", False)))
        self.sp_interval.setValue(int(ps.get("check_interval_min", 60)))
        self.cb_push_autostart.setChecked(bool(ps.get("autostart", False)))
        self.cb_notify.setChecked(bool(ps.get("notify", True)))
        self.lbl_hint.setText("")
        self.lbl_conn.setText("")

    def _save(self):
        if not self.ed_host.text().strip():
            winutil.warn(self, "提示",
                         "请先填写服务器地址（SFTP 连接信息）。\n\n"
                         "保存服务器设置前需要先配置连接信息，否则客户端无法连接。")
            return
        self.config.local_mc_dir = self.ed_local.text().strip()
        root = self.ed_server_root.text().strip()
        if root and not root.startswith("/"):
            root = "/" + root.lstrip("/")
        self.config.server_root = root or "/"
        self.ed_server_root.setText(self.config.server_root)
        excludes = sorted({line.strip().lower()
                           for line in self.ed_exclude.toPlainText().splitlines()
                           if line.strip()})
        self.config.exclude_names = excludes or sorted(DEFAULT_EXCLUDE)
        self.config.client_mods = [line.strip()
                                   for line in self.ed_client_mods.toPlainText().splitlines()
                                   if line.strip()]
        self.config.push_settings = {
            "enabled": self.cb_push.isChecked(),
            "check_interval_min": self.sp_interval.value(),
            "autostart": self.cb_push_autostart.isChecked(),
            "notify": self.cb_notify.isChecked(),
        }
        data = self._collect_sftp()
        self.config.sftp = data["sftp"]
        # 合并而非整体替换：保留 server_root、排除规则、模组关键词等其它 remote 字段
        self.config.remote = {**self.config.remote, **data["remote"]}
        # C2C 设置立即持久化（含服务端代号，登记后回填）
        self.config.server_code = self.ed_server_code.text().strip()
        self.ed_c2c_dir.setText(self.config.c2c_dir)
        if self.cb_self_autostart.isChecked() != winutil.is_autostart_enabled():
            winutil.set_autostart(self.cb_self_autostart.isChecked())
        log.info("服务器设置已保存: host=%s root=%s",
                 self.config.host(), self.config.server_root)
        self.lbl_hint.setText("已保存 ✔（工作流页面已刷新）")
        self.saved.emit()
        # 保存后立即自动检测连接，状态栏实时反馈
        self.auto_check()

    # ---------- SFTP 连接 ----------
    def _collect_sftp(self) -> dict:
        return {
            "sftp": {
                "host": self.ed_host.text().strip(),
                "port": self.ed_port.value(),
                "username": self.ed_user.text().strip(),
                "password": self.ed_password.text(),
            },
            "remote": {
                "todo_dir": _abs_dir(self.ed_todo_dir.text(), "/todo"),
                "files_dir": _abs_dir(self.ed_files_dir.text(), "/client_files"),
                "c2c_dir": abs_c2c_dir(self.ed_c2c_dir.text()),
            },
        }

    # ---------- C2C 代号登记 ----------
    def _register_code(self):
        host = self.config.host()
        if not host:
            winutil.warn(self, "提示", "请先填写服务器地址（SFTP 连接信息）。")
            return
        code = self.ed_server_code.text().strip()
        if not code:
            winutil.warn(self, "提示", "请填写服务端代号。")
            return
        c2c_dir = abs_c2c_dir(self.ed_c2c_dir.text())
        server_name = self.config.export_name() or (
            next((s.get("name", "") for s in self.config.servers
                  if s.get("id") == self.config.current_id()), ""))
        self.btn_register_code.setEnabled(False)
        self.lbl_code_status.setText("正在登记代号…")
        worker = Worker(self._register_worker, code, c2c_dir, server_name)
        worker.done.connect(self._on_register_done)
        self._code_worker = worker
        worker.start()

    def _register_worker(self, code: str, c2c_dir: str, server_name: str,
                         progress_cb=None):
        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            return register_code(sftp, c2c_dir, code, server_name)

    def _on_register_done(self, ok: bool, msg: str):
        self.btn_register_code.setEnabled(True)
        if not ok:
            self.lbl_code_status.setText("登记失败 ✘")
            winutil.error(self, "登记失败", f"无法登记代号到服务器名单：\n{msg}")
            return
        if msg:
            # register_code 返回非空错误消息 = 代号重复被拒绝
            self.lbl_code_status.setText("代号重复 ✘")
            winutil.warn(self, "代号重复", msg)
            return
        self.config.server_code = self.ed_server_code.text().strip()
        self.lbl_code_status.setText(f"代号已登记并上传名单 ✔（{self.config.c2c_dir}/roster.json）")
        winutil.info(self, "登记成功",
                     f"服务端代号「{self.ed_server_code.text().strip()}」已登记并写入名单：\n"
                     f"{self.config.c2c_dir}/roster.json\n\n"
                     f"该代号全局唯一，其他服务端将无法重复使用。")
        if self.main_window is not None:
            self.main_window.set_sftp_status(True)

    def _test_worker(self, data, progress_cb=None) -> str:
        todo = data["remote"]["todo_dir"]
        files = data["remote"]["files_dir"]
        with SFTPManager(**data["sftp"]) as sftp:
            sftp.mkdirs(todo)
            sftp.mkdirs(files)
            exists = sftp.exists(todo) and sftp.exists(files)
        return ("连接成功，待办目录与文件目录可用" if exists
                else "连接成功，但远程目录不可用")

    def auto_check(self) -> bool:
        """自动检测当前服务器的连接（不保存字段）。返回是否发起了检测。"""
        host = self.config.host()
        if not host:
            self.lbl_conn.setText("未配置服务器地址，无法检测")
            return False
        data = {
            "sftp": {"host": host, "port": self.config.port(),
                     "username": self.config.username(),
                     "password": self.config.password()},
            "remote": {"todo_dir": self.config.todo_dir,
                       "files_dir": self.config.files_dir},
        }
        self.lbl_conn.setText("正在自动检测连接…")
        worker = Worker(self._test_worker, data)
        worker.done.connect(self._on_auto_check_done)
        self._auto_worker = worker
        worker.start()
        return True

    def _on_auto_check_done(self, ok: bool, msg: str):
        self._auto_worker = None
        self._update_conn_status(ok)
        if self.main_window is not None:
            self.main_window.set_sftp_status(ok)

    def _on_test(self):
        data = self._collect_sftp()
        host = data["sftp"]["host"]
        if not host:
            winutil.warn(self, "提示", "请先填写服务器地址。")
            return
        if not winutil.confirm(self, "确认操作",
                               f"即将测试连接 {host}:{data['sftp']['port']}，是否继续？"):
            return
        self.btn_test.setEnabled(False)
        self.lbl_conn.setText("正在测试连接…")
        worker = Worker(self._test_worker, data)
        worker.done.connect(self._on_test_done)
        self._worker = worker
        worker.start()

    def _on_test_done(self, ok: bool, msg: str):
        self.btn_test.setEnabled(True)
        self._update_conn_status(ok)
        if ok:
            winutil.info(self, "测试结果", msg)
        else:
            winutil.error(self, "测试结果", f"SFTP 连接失败：\n{msg}")

    def _update_conn_status(self, ok: bool):
        self.lbl_conn.setText("连接成功 ✔" if ok else "连接失败 ✘")

    # ---------- 本地根目录 ----------
    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "选择客户端根目录",
                                                self.ed_local.text())
        if path:
            self._set_local_dir(path)

    def _choose_known_version(self):
        dlg = VersionPickerDialog(self, KnownVersions(), title="选择已知版本",
                                  hint="从 versions 下选择一个版本文件夹作为客户端根目录。")
        if dlg.exec() == VersionPickerDialog.Accepted:
            entry = dlg.selected_entry()
            if entry:
                self._set_local_dir(entry["version_dir"])

    def _set_local_dir(self, path: str):
        """设置本地新客户端根目录并立即持久化（无需再点「保存设置」）。"""
        path = (path or "").strip()
        self.ed_local.setText(path)
        self.config.local_mc_dir = path
