# -*- coding: utf-8 -*-
"""服务端 - 待办任务页：编辑任务清单并发布到 SFTP。"""
import os
from datetime import datetime
from pathlib import PurePosixPath

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app_common import winutil
from app_common.logger import get_logger
from app_common.sftp import SFTPManager
from app_common.tasks import (
    ACTION_LABELS,
    CATEGORY_LABELS,
    TaskItem,
    TodoManifest,
)
from app_common.worker import Worker

log = get_logger("server.todo_page")

COLUMNS = ["操作", "分类", "目标文件", "来源文件", "说明"]


class _TaskDialog(QDialog):
    """新增/编辑任务对话框（安装/替换 或 删除）。"""

    def __init__(self, parent=None, action="install", task: TaskItem | None = None):
        super().__init__(parent)
        self.action = action
        self.task = task
        self.setWindowTitle("编辑任务" if task else ("添加安装/替换任务" if action == "install" else "添加删除任务"))
        self.setMinimumWidth(520)
        self._build()
        if task:
            self._load(task)

    def _build(self):
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(10)

        self.cb_category = QComboBox()
        for key, label in CATEGORY_LABELS.items():
            self.cb_category.addItem(label, key)

        if self.action == "install":
            self.ed_file = QLineEdit()
            self.ed_file.setReadOnly(True)
            btn_browse = QPushButton("浏览…")
            btn_browse.clicked.connect(self._browse)
            file_row = QHBoxLayout()
            file_row.addWidget(self.ed_file, 1)
            file_row.addWidget(btn_browse)
            form.addRow("本地文件", file_row)

        self.ed_path = QLineEdit()
        self.ed_path.setPlaceholderText("例如 foo.jar（other 分类可填完整相对路径）")
        form.addRow("目标文件", self.ed_path)

        self.ed_desc = QLineEdit()
        self.ed_desc.setPlaceholderText("可选，说明任务用途")
        form.addRow("说明", self.ed_desc)

        layout.addLayout(form)
        hint = QLabel("提示：目标为游戏目录内的相对路径。")
        hint.setObjectName("muted")
        layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("确定")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择要发布的文件")
        if path:
            self.ed_file.setText(path)

    def _load(self, task: TaskItem):
        idx = self.cb_category.findData(task.category)
        if idx >= 0:
            self.cb_category.setCurrentIndex(idx)
        if self.action == "install":
            self.ed_file.setText(task.local_file)
            self.ed_path.setText(os.path.basename(task.target.replace("\\", "/")))
        else:
            self.ed_path.setText(task.target)
        self.ed_desc.setText(task.description)

    def result_task(self) -> TaskItem | None:
        category = self.cb_category.currentData()
        path = self.ed_path.text().strip()
        if not path:
            return None
        if category == "other":
            target = "/".join(p for p in PurePosixPath(path.replace("\\", "/")).parts if p not in ("", ".", ".."))
        else:
            name = path.replace("\\", "/").rstrip("/").split("/")[-1]
            if not name:
                return None
            target = f"{category}/{name}"
        if self.action == "delete":
            return TaskItem(action="delete", category=category, target=target,
                            description=self.ed_desc.text().strip())
        local = self.ed_file.text().strip()
        if not local or not os.path.isfile(local):
            return None
        return TaskItem(action="install", category=category, target=target,
                        source=target, description=self.ed_desc.text().strip(),
                        local_file=local)


class TodoPage(QWidget):
    def __init__(self, config, status_cb=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.status_cb = status_cb
        self.tasks: list[TaskItem] = []
        self._worker = None
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        head = QHBoxLayout()
        head.addWidget(QLabel("版本号："))
        self.ed_version = QLineEdit()
        self.ed_version.setPlaceholderText("自定义版本号，如 hotfix-1.0.2")
        self.ed_version.setText(f"hotfix-{datetime.now().strftime('%m%d')}")
        head.addWidget(self.ed_version, 1)
        self.lbl_time = QLabel("创建时间：—")
        self.lbl_time.setObjectName("muted")
        head.addWidget(self.lbl_time)
        layout.addLayout(head)

        # 客户端软件设置（随待办下发）
        settings_box = QGroupBox("客户端软件设置（随待办下发）")
        sv = QHBoxLayout(settings_box)
        self.cb_push_settings = QCheckBox("下发客户端软件设置")
        self.cb_push_settings.toggled.connect(self._on_push_toggled)
        sv.addWidget(self.cb_push_settings)
        sv.addSpacing(8)
        sv.addWidget(QLabel("自动检查间隔"))
        self.sp_interval = QSpinBox()
        self.sp_interval.setRange(5, 1440)
        self.sp_interval.setValue(60)
        self.sp_interval.setSuffix(" 分钟")
        sv.addWidget(self.sp_interval)
        sv.addSpacing(8)
        self.cb_autostart = QCheckBox("开机自启")
        self.cb_autostart.setToolTip("下发后，客户端将写入/清除开机自启动注册表")
        sv.addWidget(self.cb_autostart)
        self.cb_notify = QCheckBox("系统通知")
        sv.addWidget(self.cb_notify)
        sv.addStretch(1)
        layout.addWidget(settings_box)
        self._on_push_toggled(False)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        btn_row = QHBoxLayout()
        self.btn_add_install = QPushButton("添加安装/替换")
        self.btn_add_delete = QPushButton("添加删除")
        self.btn_edit = QPushButton("修改")
        self.btn_delete = QPushButton("移除")
        self.btn_up = QPushButton("上移")
        self.btn_down = QPushButton("下移")
        self.btn_clear = QPushButton("清空")
        for b in (self.btn_add_install, self.btn_add_delete, self.btn_edit,
                  self.btn_delete, self.btn_up, self.btn_down, self.btn_clear):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        pub_row = QHBoxLayout()
        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("muted")
        pub_row.addWidget(self.lbl_status, 1)
        self.btn_publish = QPushButton("发布待办到服务器")
        self.btn_publish.setObjectName("primary")
        pub_row.addWidget(self.btn_publish)
        layout.addLayout(pub_row)

        self.btn_add_install.clicked.connect(lambda: self._add_task("install"))
        self.btn_add_delete.clicked.connect(lambda: self._add_task("delete"))
        self.btn_edit.clicked.connect(self._edit_task)
        self.btn_delete.clicked.connect(self._remove_task)
        self.btn_up.clicked.connect(lambda: self._move(-1))
        self.btn_down.clicked.connect(lambda: self._move(1))
        self.btn_clear.clicked.connect(self._clear)
        self.btn_publish.clicked.connect(self._publish)

    def _on_push_toggled(self, checked: bool):
        for w in (self.sp_interval, self.cb_autostart, self.cb_notify):
            w.setEnabled(checked)

    def _collect_settings(self) -> dict:
        """收集要下发的客户端软件设置。"""
        if not self.cb_push_settings.isChecked():
            return {}
        return {
            "check_interval_min": self.sp_interval.value(),
            "autostart": self.cb_autostart.isChecked(),
            "notify": self.cb_notify.isChecked(),
        }

    # ---------- 表格 ----------
    def _refresh(self):
        self.table.setRowCount(len(self.tasks))
        for row, task in enumerate(self.tasks):
            self.table.setItem(row, 0, QTableWidgetItem(ACTION_LABELS.get(task.action, task.action)))
            self.table.setItem(row, 1, QTableWidgetItem(CATEGORY_LABELS.get(task.category, task.category)))
            self.table.setItem(row, 2, QTableWidgetItem(task.target))
            self.table.setItem(row, 3, QTableWidgetItem(task.source))
            self.table.setItem(row, 4, QTableWidgetItem(task.description))
        self.lbl_time.setText(f"创建时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.lbl_status.setText(f"共 {len(self.tasks)} 个任务")

    def _selected_index(self) -> int:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    # ---------- 任务编辑 ----------
    def _add_task(self, action: str):
        dlg = _TaskDialog(self, action=action)
        if dlg.exec() != QDialog.Accepted:
            return
        task = dlg.result_task()
        if task is None:
            winutil.warn(self, "提示", "任务信息不完整：请选择本地文件并填写目标文件名。")
            return
        self.tasks.append(task)
        self._refresh()
        self.table.selectRow(len(self.tasks) - 1)

    def _edit_task(self):
        row = self._selected_index()
        if row < 0:
            winutil.warn(self, "提示", "请先选中一个任务。")
            return
        task = self.tasks[row]
        action = task.action
        dlg = _TaskDialog(self, action=action, task=task)
        if dlg.exec() != QDialog.Accepted:
            return
        new_task = dlg.result_task()
        if new_task is None:
            winutil.warn(self, "提示", "任务信息不完整。")
            return
        new_task.id = task.id
        self.tasks[row] = new_task
        self._refresh()
        self.table.selectRow(row)

    def _remove_task(self):
        row = self._selected_index()
        if row < 0:
            winutil.warn(self, "提示", "请先选中一个任务。")
            return
        task = self.tasks[row]
        if not winutil.confirm(self, "确认操作", f"确定移除任务：\n{task.summary()}？"):
            return
        self.tasks.pop(row)
        self._refresh()

    def _move(self, direction: int):
        row = self._selected_index()
        new = row + direction
        if row < 0 or not (0 <= new < len(self.tasks)):
            return
        self.tasks[row], self.tasks[new] = self.tasks[new], self.tasks[row]
        self._refresh()
        self.table.selectRow(new)

    def _clear(self):
        if not self.tasks:
            return
        if not winutil.confirm(self, "确认操作", "确定清空全部任务吗？"):
            return
        self.tasks.clear()
        self._refresh()

    # ---------- 发布 ----------
    def _publish(self):
        version = self.ed_version.text().strip()
        if not version:
            winutil.warn(self, "提示", "请填写版本号。")
            return
        if not self.tasks:
            winutil.warn(self, "提示", "任务列表为空，无法发布。")
            return
        host = self.config.host()
        if not host:
            winutil.warn(self, "提示", "请先在「SFTP 设置」页填写服务器信息。")
            return
        detail = "\n".join(f"  · {t.summary()}" for t in self.tasks)
        settings = self._collect_settings()
        if settings:
            labels = {"check_interval_min": f"自动检查间隔={settings['check_interval_min']}分钟",
                      "autostart": f"开机自启={'开' if settings['autostart'] else '关'}",
                      "notify": f"系统通知={'开' if settings['notify'] else '关'}"}
            detail += "\n客户端软件设置：" + "、".join(labels.values())
        if not winutil.confirm(
                self, "确认发布",
                f"即将发布待办任务到服务器 {host}：\n"
                f"版本号：{version}\n{detail}\n\n是否继续？"):
            return

        self.btn_publish.setEnabled(False)
        self.lbl_status.setText("正在发布…")
        worker = Worker(self._publish_worker, version, list(self.tasks), settings)
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_publish_done)
        self._worker = worker
        worker.start()

    def _publish_worker(self, version, tasks, settings, progress_cb=None):
        manifest = TodoManifest.new(version)
        manifest.tasks = [TaskItem.from_dict(t.to_dict()) for t in tasks]
        manifest.settings = dict(settings)
        total = len(tasks) + 1
        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            files_dir = self.config.files_dir
            for i, task in enumerate(manifest.tasks):
                if task.action == "install" and task.local_file:
                    remote = TodoManifest.remote_source_path(sftp, files_dir, task.source)
                    sftp.upload(task.local_file, remote)
                if progress_cb:
                    progress_cb(i + 1, total, f"上传 {task.target}")
            manifest.save_to_sftp(sftp, self.config.todo_dir)
            if progress_cb:
                progress_cb(total, total, "写入清单")
        return (f"待办已发布：{manifest.version}（{len(manifest.tasks)} 项）\n"
                f"时间：{manifest.formatted_time()}")

    def _on_progress(self, current, total, message):
        self.lbl_status.setText(f"发布进度 {current}/{total}：{message}")

    def _on_publish_done(self, ok: bool, msg: str):
        self.btn_publish.setEnabled(True)
        if self.status_cb:
            self.status_cb(ok)
        if ok:
            self.lbl_status.setText("发布完成 ✔")
            winutil.info(self, "发布成功", msg)
            log.info("待办发布成功: %s", msg)
        else:
            self.lbl_status.setText("发布失败 ✘")
            winutil.error(self, "发布失败", f"发布过程中发生错误：\n{msg}")
