# -*- coding: utf-8 -*-
"""服务端 - 导出客户端配置页：生成可分发、可导入的服务器配置文件（.mcscf）。

一个客户端主程序可通过导入多个配置文件同时接入多台服务器；
本页负责维护本服务器的名称、唯一编号与授权编号列表，并导出配置文件。
"""
import json
import os
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app_common import winutil
from app_common.app_config import ServerConfig
from app_common.license import save_ids
from app_common.logger import get_logger
from app_common.profile import PROFILE_FILTER, PROFILE_SUFFIX, build_profile_content, new_server_id
from app_common.sftp import SFTPManager
from app_common.worker import Worker, fmt_progress

log = get_logger("server.export_page")


class ExportPage(QWidget):
    def __init__(self, config: ServerConfig, status_cb=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.status_cb = status_cb
        self._worker = None
        self._build()
        self._load()

    # ---------- 界面 ----------
    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        box = QGroupBox("本服务器信息（写入客户端配置文件）")
        form = QVBoxLayout(box)
        form.setSpacing(8)

        row_name = QHBoxLayout()
        row_name.addWidget(QLabel("服务器名称"))
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("人类可读名称，例如 CreateEncore 服务器")
        row_name.addWidget(self.ed_name, 1)
        form.addLayout(row_name)

        row_id = QHBoxLayout()
        row_id.addWidget(QLabel("唯一编号"))
        self.ed_uid = QLineEdit()
        self.ed_uid.setReadOnly(True)
        self.ed_uid.setPlaceholderText("自动生成（UUID，机器可读，用于授权校验）")
        row_id.addWidget(self.ed_uid, 1)
        form.addLayout(row_id)

        hint = QLabel("说明：名称给人看、编号给程序读。客户端导入配置文件后，以「名称 + 唯一编号」识别本服务器；"
                      "编号必须同步到服务器授权清单（license.json），客户端连接时才会通过校验。"
                      "\n编号由系统自动生成且不可更改：客户端已按该编号授权，重新生成会使所有客户端失效。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        form.addWidget(hint)
        layout.addWidget(box)

        # 授权编号
        auth_box = QGroupBox("已授权客户端编号（同步到服务器 license.json）")
        av = QVBoxLayout(auth_box)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["唯一编号", "服务器名称", "创建时间", "说明"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionsMovable(True)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        av.addWidget(self.table)
        auth_btn_row = QHBoxLayout()
        btn_remove = QPushButton("移除选中编号")
        btn_remove.clicked.connect(self._remove_id)
        auth_btn_row.addWidget(btn_remove)
        auth_btn_row.addStretch(1)
        av.addLayout(auth_btn_row)
        layout.addWidget(auth_box, 1)

        # 操作
        act_row = QHBoxLayout()
        self.btn_save = QPushButton("保存信息")
        self.btn_sync = QPushButton("同步编号到服务器")
        self.btn_export = QPushButton("导出客户端配置文件")
        self.btn_export.setObjectName("primary")
        act_row.addWidget(self.btn_save)
        act_row.addWidget(self.btn_sync)
        act_row.addStretch(1)
        act_row.addWidget(self.btn_export)
        layout.addLayout(act_row)

        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("muted")
        self.lbl_status.setWordWrap(True)
        layout.addWidget(self.lbl_status)

        self.btn_save.clicked.connect(self._save)
        self.btn_sync.clicked.connect(self._sync_ids)
        self.btn_export.clicked.connect(self._export)
        self.ed_name.textChanged.connect(lambda _: self._persist_info())

    def reload(self):
        """切换服务器后刷新界面。"""
        self._load()

    def _load(self):
        """载入名称与唯一编号；编号由系统生成后立即持久化，不可手动重新生成。"""
        name = self.config.export_name()
        sid = self.config.export_server_id()
        if not name:
            cur = next((s for s in self.config.servers
                        if s.get("id") == self.config.current_id()), None)
            name = (cur or {}).get("name", "") or "默认服务器"
        if not sid:
            sid = new_server_id()
        self.ed_uid.setText(sid)
        self.ed_name.setText(name)
        self._persist_info()
        self._refresh_auth_table()

    def _persist_info(self):
        """名称与唯一编号即时持久化（不依赖「保存信息」按钮，关闭/切换后依然保留）。"""
        self.config.export = {
            **self.config.export,
            "name": self.ed_name.text().strip(),
            "server_id": self.ed_uid.text().strip(),
        }

    def _save_state(self):
        self.config.export = {
            "name": self.ed_name.text().strip(),
            "server_id": self.ed_uid.text().strip(),
            "ids": [
                {"id": self.table.item(r, 0).text(),
                 "name": self.table.item(r, 1).text(),
                 "created_at": self.table.item(r, 2).text(),
                 "note": self.table.item(r, 3).text()}
                for r in range(self.table.rowCount())
            ],
        }

    def _refresh_auth_table(self):
        ids = self.config.export_ids()
        self.table.setRowCount(len(ids))
        for row, item in enumerate(ids):
            self.table.setItem(row, 0, QTableWidgetItem(item.get("id", "")))
            self.table.setItem(row, 1, QTableWidgetItem(item.get("name", "")))
            self.table.setItem(row, 2, QTableWidgetItem(item.get("created_at", "")))
            self.table.setItem(row, 3, QTableWidgetItem(item.get("note", "")))

    def _remove_id(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            winutil.warn(self, "提示", "请先在列表中选择一个编号。")
            return
        item = self.table.item(rows[0].row(), 0)
        uid = item.text() if item else ""
        if not winutil.confirm(self, "确认操作",
                               f"确定移除编号 {uid} 吗？移除后该客户端将无法连接。\n\n注意：还需点击「同步编号到服务器」使变更生效。"):
            return
        ids = [i for i in self.config.export_ids() if i.get("id") != uid]
        self.config.export = {**self.config.export, "ids": ids}
        self._refresh_auth_table()

    # ---------- 保存 ----------
    def _save(self):
        if not self.ed_name.text().strip():
            winutil.warn(self, "提示", "请填写服务器名称。")
            return
        if not winutil.confirm(self, "确认操作", "即将保存本服务器信息，是否继续？"):
            return
        self._save_state()
        self.lbl_status.setText("服务器信息已保存 ✔")

    # ---------- 同步编号 ----------
    def _sync_ids(self):
        host = self.config.host()
        if not host:
            winutil.warn(self, "提示", "请先在「SFTP 设置」页填写服务器信息。")
            return
        ids = [i.get("id", "") for i in self.config.export_ids()]
        if not ids:
            winutil.warn(self, "提示", "授权列表为空，无需同步。")
            return
        if not winutil.confirm(self, "确认同步",
                               f"即将把 {len(ids)} 个授权编号同步到服务器 {host}（写入 license.json）。\n是否继续？"):
            return
        self.btn_sync.setEnabled(False)
        self.lbl_status.setText("正在同步编号…")
        worker = Worker(self._sync_worker, ids)
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_sync_done)
        self._worker = worker
        worker.start()

    def _on_progress(self, current, total, message):
        self.lbl_status.setText(fmt_progress(current, total, message, "同步编号"))

    def _sync_worker(self, ids, progress_cb=None):
        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            if progress_cb:
                progress_cb(1, 1, "写入 license.json")
            save_ids(sftp, ids)
        return f"已同步 {len(ids)} 个编号到服务器根目录 license.json。"

    def _on_sync_done(self, ok: bool, msg: str):
        self.btn_sync.setEnabled(True)
        if ok:
            self.lbl_status.setText("编号同步完成 ✔")
            winutil.info(self, "同步完成", msg)
            if self.status_cb:
                self.status_cb(True)
        else:
            self.lbl_status.setText("编号同步失败 ✘")
            winutil.error(self, "同步失败", f"无法同步编号到服务器：\n{msg}")
            if self.status_cb:
                self.status_cb(False)

    # ---------- 导出 ----------
    def _export(self):
        name = self.ed_name.text().strip()
        server_id = self.ed_uid.text().strip()
        if not name:
            winutil.warn(self, "提示", "请填写服务器名称。")
            return
        if not server_id:
            winutil.warn(self, "提示", "请先生成唯一编号。")
            return
        host = self.config.host()
        if not host:
            winutil.warn(self, "提示", "请先在「SFTP 设置」页填写服务器信息（将打包进配置文件）。")
            return

        safe_name = "".join(c for c in name if c not in '\\/:*?"<>|').strip() or "服务器"
        default_path = os.path.join(os.path.expanduser("~"), "Desktop",
                                    f"客户端配置_{safe_name}{PROFILE_SUFFIX}")
        path, _ = QFileDialog.getSaveFileName(self, "导出客户端配置文件", default_path, PROFILE_FILTER)
        if not path:
            return
        if not path.lower().endswith(PROFILE_SUFFIX):
            path += PROFILE_SUFFIX

        detail = (f"服务器名称：{name}\n"
                  f"唯一编号：{server_id}\n"
                  f"服务器地址：{host}:{self.config.port()}\n"
                  f"待办目录：{self.config.todo_dir}\n"
                  f"文件目录：{self.config.files_dir}\n"
                  f"导出路径：{path}\n\n"
                  f"导出后该编号将登记并同步到服务器授权清单。")
        if not winutil.confirm(self, "确认导出", f"即将导出客户端配置文件：\n\n{detail}\n是否继续？"):
            return

        self._save_state()
        self.btn_export.setEnabled(False)
        self.lbl_status.setText("正在导出…")
        worker = Worker(self._export_worker, name, server_id, path)
        worker.progress.connect(self._on_export_progress)
        worker.done.connect(self._on_export_done)
        self._worker = worker
        worker.start()

    def _on_export_progress(self, current, total, message):
        self.lbl_status.setText(fmt_progress(current, total, message, "导出"))

    def _export_worker(self, name, server_id, output_path, progress_cb=None):
        def step(cur: int, total: int, msg: str):
            if progress_cb:
                progress_cb(cur, total, msg)

        step(1, 3, "生成客户端配置文件")
        info = {
            "host": self.config.host(),
            "port": self.config.port(),
            "username": self.config.username(),
            "password": self.config.password(),
            "todo_dir": self.config.todo_dir,
            "files_dir": self.config.files_dir,
        }
        content = build_profile_content(name, server_id, info)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)

        # 登记编号并同步授权清单
        step(2, 3, "登记授权编号")
        ids = self.config.export_ids()
        if server_id not in [i.get("id") for i in ids]:
            ids.append({"id": server_id, "name": name,
                        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "note": "由导出客户端配置生成"})
            self.config.export = {**self.config.export, "ids": ids}
        step(3, 3, "同步授权清单到服务器")
        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            save_ids(sftp, [i.get("id", "") for i in ids])
        return f"客户端配置文件已导出：\n{output_path}\n\n服务器：{name}（{server_id}）\n授权编号已同步到服务器。"

    def _on_export_done(self, ok: bool, msg: str):
        self.btn_export.setEnabled(True)
        if ok:
            self.lbl_status.setText("导出完成 ✔")
            self._refresh_auth_table()
            winutil.info(self, "导出成功", msg)
            if self.status_cb:
                self.status_cb(True)
        else:
            self.lbl_status.setText("导出失败 ✘")
            winutil.error(self, "导出失败", f"导出过程中发生错误：\n{msg}")
            if self.status_cb:
                self.status_cb(False)
