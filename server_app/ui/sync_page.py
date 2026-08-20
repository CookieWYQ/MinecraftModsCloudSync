# -*- coding: utf-8 -*-
"""服务端 - 文件同步页：本地游戏目录 → SFTP，带客户端专用过滤。"""
import fnmatch
import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app_common import winutil
from app_common.launcher import KnownVersions, resolve_dropped
from app_common.logger import get_logger
from app_common.sftp import SFTPManager
from app_common.tasks import TodoManifest
from app_common.version_dialog import VersionPickerDialog
from app_common.worker import Worker

log = get_logger("server.sync_page")


class SyncPage(QWidget):
    def __init__(self, config, status_cb=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.status_cb = status_cb
        self._worker = None
        self._preview_items: list[tuple[str, str]] = []  # (本地绝对路径, 远程相对路径)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        local_box = QGroupBox("本地 Minecraft 客户端根目录（可拖入版本文件夹 / .minecraft / 启动器快捷方式）")
        local_row = QHBoxLayout(local_box)
        self.ed_local = QLineEdit()
        self.ed_local.setPlaceholderText("选择制作完成的客户端根目录（含 mods/resourcepacks/config 等）")
        self.ed_local.setReadOnly(True)
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._browse)
        btn_versions = QPushButton("选择已知版本…")
        btn_versions.clicked.connect(self._choose_known_version)
        local_row.addWidget(self.ed_local, 1)
        local_row.addWidget(btn_versions)
        local_row.addWidget(btn_browse)
        layout.addWidget(local_box)

        filter_box = QGroupBox("同步范围与过滤")
        fv = QVBoxLayout(filter_box)
        cat_row = QHBoxLayout()
        self.cb_mods = QCheckBox("模组 mods")
        self.cb_res = QCheckBox("资源包 resourcepacks")
        self.cb_cfg = QCheckBox("配置 config")
        for cb in (self.cb_mods, self.cb_res, self.cb_cfg):
            cb.setChecked(True)
            cat_row.addWidget(cb)
        cat_row.addStretch(1)
        fv.addLayout(cat_row)

        self.cb_filter = QCheckBox("过滤仅限客户端模组（同步到服务端时排除以下关键词文件）")
        fv.addWidget(self.cb_filter)

        kw_row = QHBoxLayout()
        kw_row.addWidget(QLabel("客户端专用关键词（每行一个，小写子串匹配）"))
        kw_row.addStretch(1)
        fv.addLayout(kw_row)
        self.ed_keywords = QPlainTextEdit()
        self.ed_keywords.setFixedHeight(88)
        fv.addWidget(self.ed_keywords)

        ex_row = QHBoxLayout()
        ex_row.addWidget(QLabel("排除模式（fnmatch 通配，每行一个）"))
        ex_row.addStretch(1)
        fv.addLayout(ex_row)
        self.ed_excludes = QPlainTextEdit()
        self.ed_excludes.setFixedHeight(72)
        fv.addWidget(self.ed_excludes)
        layout.addWidget(filter_box)

        btn_row = QHBoxLayout()
        self.btn_preview = QPushButton("预览待同步文件")
        self.btn_sync = QPushButton("执行同步")
        self.btn_sync.setObjectName("primary")
        self.btn_sync.setEnabled(False)
        btn_row.addWidget(self.btn_preview)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_sync)
        layout.addLayout(btn_row)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["√", "远程路径", "本地大小"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.MultiSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        layout.addWidget(self.table, 1)

        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("muted")
        layout.addWidget(self.lbl_status)

        self.btn_preview.clicked.connect(self._preview)
        self.btn_sync.clicked.connect(self._sync)

        self._load_config()

    def _load_config(self):
        sync = self.config.sync
        self.ed_local.setText(self.config.local_mc_dir)
        cats = sync.get("categories", [])
        self.cb_mods.setChecked("mods" in cats)
        self.cb_res.setChecked("resourcepacks" in cats)
        self.cb_cfg.setChecked("config" in cats)
        self.cb_filter.setChecked(sync.get("filter_client_only", True))
        self.ed_keywords.setPlainText("\n".join(sync.get("client_only_keywords", [])))
        self.ed_excludes.setPlainText("\n".join(sync.get("exclude_patterns", [])))

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "选择客户端根目录", self.ed_local.text())
        if path:
            self._set_local(path)

    def _set_local(self, path: str):
        self.ed_local.setText(path)
        self.config.local_mc_dir = path

    def _choose_known_version(self):
        dlg = VersionPickerDialog(self, KnownVersions(), title="选择已知版本",
                                  hint="从 versions 下选择一个版本文件夹作为客户端根目录。")
        if dlg.exec() == VersionPickerDialog.Accepted:
            entry = dlg.selected_entry()
            if entry:
                self._set_local(entry["version_dir"])
                winutil.info(self, "已填充",
                             f"已选择版本：{entry.get('version')}\n"
                             f"客户端根目录：{entry.get('version_dir')}")

    def handle_dropped(self, path: str):
        """处理从主窗口拖入的路径（版本文件夹 / .minecraft / 启动器快捷方式）。"""
        info = resolve_dropped(path)
        if not info:
            winutil.warn(self, "无法识别",
                         "无法识别拖入的内容。\n\n支持：\n"
                         "· versions 下的版本/整合包文件夹（内含 mods 等）\n"
                         "· .minecraft 文件夹\n"
                         "· 启动器快捷方式(.lnk) 或启动器程序")
            return
        known = KnownVersions()
        if info["kind"] == "version_dir":
            known.add_version_dir(info["version_dir"])
            self._set_local(info["version_dir"])
            winutil.info(self, "已填充",
                         f"已将版本目录设为客户端根目录：\n{info['version_dir']}")
            return
        if info.get("mc_root"):
            added = known.add_from_mc_root(info["mc_root"])
            hint = (f"已识别 Minecraft 根目录：{info['mc_root']}"
                    + (f"\n新增 {added} 个版本。" if added else ""))
            dlg = VersionPickerDialog(self, known, title="选择版本目录", hint=hint)
            if dlg.exec() == VersionPickerDialog.Accepted:
                entry = dlg.selected_entry()
                if entry:
                    self._set_local(entry["version_dir"])
                    winutil.info(self, "已填充",
                                 f"已选择版本：{entry.get('version')}\n"
                                 f"客户端根目录：{entry.get('version_dir')}")

    def _collect_sync(self) -> dict:
        categories = []
        if self.cb_mods.isChecked():
            categories.append("mods")
        if self.cb_res.isChecked():
            categories.append("resourcepacks")
        if self.cb_cfg.isChecked():
            categories.append("config")
        keywords = [k.strip().lower() for k in self.ed_keywords.toPlainText().splitlines() if k.strip()]
        excludes = [p.strip().lower() for p in self.ed_excludes.toPlainText().splitlines() if p.strip()]
        return {
            "local_dir": self.ed_local.text().strip(),
            "categories": categories,
            "filter_client_only": self.cb_filter.isChecked(),
            "keywords": keywords,
            "excludes": excludes,
        }

    def _save_sync_settings(self, data: dict):
        self.config.local_mc_dir = data["local_dir"]
        self.config.sync = {
            "categories": data["categories"],
            "filter_client_only": data["filter_client_only"],
            "client_only_keywords": data["keywords"],
            "exclude_patterns": data["excludes"],
        }

    # ---------- 预览 ----------
    def _preview(self):
        data = self._collect_sync()
        if not data["local_dir"] or not os.path.isdir(data["local_dir"]):
            winutil.warn(self, "提示", "请先选择有效的客户端根目录。")
            return
        if not data["categories"]:
            winutil.warn(self, "提示", "请至少勾选一个同步分类。")
            return
        self._save_sync_settings(data)
        self.btn_preview.setEnabled(False)
        self.lbl_status.setText("正在扫描本地文件…")
        worker = Worker(self._scan, data)
        worker.done.connect(self._on_scan_done)
        self._worker = worker
        worker.start()

    @staticmethod
    def _scan(data: dict, progress_cb=None) -> list:
        """扫描本地文件并应用过滤，返回 (本地绝对路径, 远程相对路径, 大小)。"""
        root = Path(data["local_dir"])
        keywords = data["keywords"]
        excludes = data["excludes"]
        filter_client = data["filter_client_only"]
        result = []
        for category in data["categories"]:
            cat_dir = root / category
            if not cat_dir.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(cat_dir):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    rel = os.path.relpath(os.path.join(dirpath, name), root)
                    rel_posix = rel.replace("\\", "/")
                    lower = rel_posix.lower()
                    if any(fnmatch.fnmatch(lower, p) for p in excludes):
                        continue
                    if filter_client and category == "mods" and any(k in lower for k in keywords):
                        continue
                    if name.startswith("."):
                        continue
                    size = os.path.getsize(os.path.join(dirpath, name))
                    result.append((os.path.join(dirpath, name), rel_posix, size))
        result.sort(key=lambda x: x[1])
        return result

    def _on_scan_done(self, ok: bool, result):
        self.btn_preview.setEnabled(True)
        if not ok:
            self.lbl_status.setText("扫描失败 ✘")
            winutil.error(self, "扫描失败", str(result))
            return
        items = [(a, b, s) for a, b, s in result]
        self._preview_items = items
        self.table.setRowCount(len(items))
        for row, (local, rel, size) in enumerate(items):
            check = QTableWidgetItem("☑")
            check.setFlags(check.flags() | Qt.ItemIsUserCheckable)
            check.setCheckState(Qt.Checked)
            self.table.setItem(row, 0, check)
            self.table.setItem(row, 1, QTableWidgetItem(rel))
            self.table.setItem(row, 2, QTableWidgetItem(_fmt_size(size)))
        self.btn_sync.setEnabled(True)
        self.lbl_status.setText(f"扫描完成：{len(items)} 个文件待同步（可取消勾选）")

    # ---------- 同步 ----------
    def _sync(self):
        if not self._preview_items:
            return
        if not winutil.confirm(
                self, "确认同步",
                f"即将把 {self.table.rowCount()} 个文件同步（上传）到 SFTP 服务器，是否继续？"):
            return
        self.btn_sync.setEnabled(False)
        self.btn_preview.setEnabled(False)
        worker = Worker(self._sync_worker, list(self._preview_items))
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_sync_done)
        self._worker = worker
        worker.start()

    def _sync_worker(self, items, progress_cb=None):
        selected = []
        for row, (local, rel, _size) in enumerate(items):
            item = self.table.item(row, 0)
            if item is not None and item.checkState() == Qt.Checked:
                selected.append((local, rel))
        total = len(selected)
        if total == 0:
            return "没有勾选任何文件，未执行同步。"
        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            files_dir = self.config.files_dir
            for i, (local, rel) in enumerate(selected):
                remote = TodoManifest.remote_source_path(sftp, files_dir, rel)
                sftp.upload(local, remote)
                if progress_cb:
                    progress_cb(i + 1, total, rel)
        return f"同步完成：共上传 {total} 个文件。"

    def _on_progress(self, current, total, message):
        self.lbl_status.setText(f"同步进度 {current}/{total}：{message}")

    def _on_sync_done(self, ok: bool, msg: str):
        self.btn_sync.setEnabled(True)
        self.btn_preview.setEnabled(True)
        if self.status_cb:
            self.status_cb(ok)
        if ok:
            self.lbl_status.setText("同步完成 ✔")
            winutil.info(self, "同步完成", msg)
            log.info("同步完成: %s", msg)
        else:
            self.lbl_status.setText("同步失败 ✘")
            winutil.error(self, "同步失败", f"同步过程中发生错误：\n{msg}")


def _fmt_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    return f"{size:.1f} TB"
