# -*- coding: utf-8 -*-
"""服务端 - 文件同步页：树状勾选本地内容 → SFTP，带排除模式。"""
import fnmatch
import os
import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app_common import winutil
from app_common.launcher import KnownVersions, resolve_dropped
from app_common.logger import get_logger
from app_common.sftp import SFTPManager
from app_common.tasks import TodoManifest
from app_common.version_dialog import VersionPickerDialog
from app_common.worker import Worker, fmt_progress

log = get_logger("server.sync_page")

# 树节点说明标注（实时显示在右侧列）
CATEGORY_HINTS = {
    "mods": "模组文件夹",
    "resourcepacks": "资源包文件夹",
    "config": "配置文件",
    "kubejs": "KubeJS 魔改脚本",
    "datapacks": "数据包",
    "shaderpacks": "光影包",
    "saves": "存档",
    "logs": "日志",
}


class SyncPage(QWidget):
    def __init__(self, config, status_cb=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.status_cb = status_cb
        self._worker = None
        self._preview_items: list[tuple[str, str]] = []  # (本地绝对路径, 远程相对路径)
        self._exclude_patterns: list[str] = []
        self._custom_hints: dict[str, str] = {}
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

        tree_box = QGroupBox("同步内容")
        tv = QVBoxLayout(tree_box)
        head = QHBoxLayout()
        head.addWidget(QLabel("树状勾选：整文件夹勾选，或展开勾选/排除具体文件（√ 全部 / - 部分）"))
        head.addStretch(1)
        btn_excludes = QPushButton("排除模式…")
        btn_excludes.setToolTip("按 fnmatch 通配排除不需要同步的文件（每行一个）")
        btn_excludes.clicked.connect(self._edit_excludes)
        head.addWidget(btn_excludes)
        tv.addLayout(head)

        # 搜索 + 排序
        toolbar = QHBoxLayout()
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索名称（支持 * 通配）")
        self.ed_search.setClearButtonEnabled(True)
        self.cb_regex = QCheckBox("正则")
        self.cb_regex.setToolTip("启用后按正则表达式搜索")
        toolbar.addWidget(self.ed_search, 1)
        toolbar.addWidget(self.cb_regex)
        toolbar.addWidget(QLabel("排序："))
        self.cb_sort = QComboBox()
        self.cb_sort.addItem("按名称", "name")
        self.cb_sort.addItem("按创建时间", "ctime")
        self.cb_sort.addItem("按类型分组（文件夹在前）", "group")
        toolbar.addWidget(self.cb_sort)
        self.btn_sort_dir = QPushButton("↓ 正序")
        self.btn_sort_dir.setCheckable(True)
        self.btn_sort_dir.setChecked(False)
        self.btn_sort_dir.setToolTip("切换正序 / 倒序")
        toolbar.addWidget(self.btn_sort_dir)
        tv.addLayout(toolbar)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["文件 / 文件夹", "说明"])
        self.tree.setRootIsDecorated(True)
        self.tree.setSelectionMode(QAbstractItemView.NoSelection)
        self.tree.setMinimumHeight(360)
        # 名称列拉伸占满宽度，完整显示长文件名；说明列按内容自适应
        tree_header = self.tree.header()
        tree_header.setSectionResizeMode(0, QHeaderView.Stretch)
        tree_header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.setSortingEnabled(False)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        tv.addWidget(self.tree, 1)
        layout.addWidget(tree_box, 2)

        self.ed_search.textChanged.connect(lambda _: self._apply_filter())
        self.cb_regex.toggled.connect(lambda _: self._apply_filter())
        self.cb_sort.currentIndexChanged.connect(lambda _: self._reload_tree())
        self.btn_sort_dir.toggled.connect(lambda _: self._reload_tree())

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

    # ---------- 树状选择 ----------
    def _reload_tree(self):
        """以本地根目录重建勾选树（懒加载：展开文件夹时才加载其子项）。"""
        self.tree.blockSignals(True)
        self.tree.clear()
        root_dir = self.ed_local.text().strip() or self.config.local_mc_dir
        if root_dir and os.path.isdir(root_dir):
            root_item = QTreeWidgetItem([os.path.basename(root_dir.rstrip("/\\")) or root_dir,
                                         "客户端根目录"])
            root_item.setData(0, Qt.UserRole, os.path.abspath(root_dir))
            root_item.setCheckState(0, Qt.Checked)
            self.tree.addTopLevelItem(root_item)
            root_item.setExpanded(True)
            self._populate_children(root_item, root_dir, lazy=True)
            self._apply_config_checks()
        self.tree.blockSignals(False)
        self._apply_filter()

    def _sorted_names(self, path: str) -> list[str]:
        try:
            names = [n for n in os.listdir(path) if not n.startswith(".")]
        except OSError:
            return []
        mode = self.cb_sort.currentData()

        def _key(n):
            if mode == "ctime":
                try:
                    return os.path.getctime(os.path.join(path, n))
                except OSError:
                    return 0.0
            return n.lower()

        names.sort(key=_key)
        if self.btn_sort_dir.isChecked():  # 倒序
            names.reverse()
        if mode == "group":
            # 文件夹一组、文件一组（文件夹固定在前）
            dirs = [n for n in names if os.path.isdir(os.path.join(path, n))]
            files = [n for n in names if os.path.isfile(os.path.join(path, n))]
            names = dirs + files
        return names

    def _hint_for(self, name: str) -> str:
        if name in CATEGORY_HINTS:
            return CATEGORY_HINTS[name]
        return self._custom_hints.get(name, "")

    def _populate_children(self, item: QTreeWidgetItem, path: str, lazy: bool):
        item.takeChildren()
        parent_state = item.checkState(0)
        for name in self._sorted_names(path):
            full = os.path.join(path, name)
            hint = self._hint_for(name)
            if os.path.isdir(full):
                child = QTreeWidgetItem([name, hint])
                child.setData(0, Qt.UserRole, full)
                child.setCheckState(0, parent_state)
                child.setToolTip(0, full)
                child.setToolTip(1, hint)
                item.addChild(child)
                if lazy:
                    # 占位子项，展开时再加载真实内容
                    QTreeWidgetItem(child, ["", ""])
                else:
                    self._populate_children(child, full, lazy=True)
            elif os.path.isfile(full):
                child = QTreeWidgetItem([name, hint])
                child.setData(0, Qt.UserRole, full)
                child.setCheckState(0, parent_state)
                child.setToolTip(0, full)
                child.setToolTip(1, hint)
                item.addChild(child)

    def _on_expanded(self, item: QTreeWidgetItem):
        path = item.data(0, Qt.UserRole)
        if not path or not os.path.isdir(path):
            return
        if item.childCount() == 1 and item.child(0).data(0, Qt.UserRole) is None:
            self._populate_children(item, path, lazy=True)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int):
        """双击「说明」列直接编辑该文件夹/文件的说明。"""
        if column != 1 or item is None:
            return
        name = item.text(0)
        if not name:
            return
        new_hint, ok = QInputDialog.getText(
            self, "编辑说明", f"「{name}」的说明：", text=item.text(1))
        if not ok:
            return
        item.setText(1, new_hint)
        item.setToolTip(1, new_hint)
        hints = dict(self.config.sync.get("category_hints", {}) or {})
        hints[name] = new_hint
        self.config.sync = {**self.config.sync, "category_hints": hints}
        self._custom_hints = hints

    # ---------- 搜索（glob / 可选正则） ----------
    def _apply_filter(self):
        text = self.ed_search.text().strip()
        root = self.tree.topLevelItem(0)
        if root is None:
            return
        if not text:
            self._set_visible_all(root, True)
            return
        self.tree.expandAll()
        use_re = self.cb_regex.isChecked()
        self._filter_node(root, text, use_re)

    def _filter_node(self, item: QTreeWidgetItem, text: str, use_re: bool) -> bool:
        if self._match(item.text(0), text, use_re):
            self._set_visible_all(item, True)
            return True
        found = False
        for i in range(item.childCount()):
            if self._filter_node(item.child(i), text, use_re):
                found = True
        item.setHidden(not found)
        return found

    @staticmethod
    def _match(name: str, text: str, use_re: bool) -> bool:
        if use_re:
            try:
                return re.search(text, name, re.IGNORECASE) is not None
            except re.error:
                return False
        return fnmatch.fnmatch(name.lower(), text.lower())

    def _set_visible_all(self, item: QTreeWidgetItem, visible: bool):
        item.setHidden(not visible)
        for i in range(item.childCount()):
            self._set_visible_all(item.child(i), visible)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int):
        if column != 0:
            return
        self.tree.blockSignals(True)
        try:
            state = item.checkState(0)
            self._set_children_state(item, state)
            self._update_parent_state(item)
        finally:
            self.tree.blockSignals(False)

    def _set_children_state(self, item: QTreeWidgetItem, state):
        for i in range(item.childCount()):
            child = item.child(i)
            child.setCheckState(0, state)
            self._set_children_state(child, state)

    def _update_parent_state(self, item: QTreeWidgetItem):
        parent = item.parent()
        if parent is None:
            return
        real = [parent.child(i) for i in range(parent.childCount())
                if parent.child(i).data(0, Qt.UserRole) is not None]
        if not real:
            return
        states = [c.checkState(0) for c in real]
        if all(s == Qt.Checked for s in states):
            parent.setCheckState(0, Qt.Checked)
        elif all(s == Qt.Unchecked for s in states):
            parent.setCheckState(0, Qt.Unchecked)
        else:
            parent.setCheckState(0, Qt.PartiallyChecked)
        self._update_parent_state(parent)

    def _apply_config_checks(self):
        """按持久化的分类恢复顶层文件夹勾选（无历史记录时默认全选）。"""
        root = self.tree.topLevelItem(0)
        if root is None:
            return
        cats = self.config.sync.get("categories")
        if cats is None:
            return
        cat_set = set(cats)
        self._set_children_state(root, Qt.Unchecked)
        for i in range(root.childCount()):
            child = root.child(i)
            if child.text(0) in cat_set:
                child.setCheckState(0, Qt.Checked)

    def _collect_checked_files(self) -> list[str]:
        """遍历树，收集勾选文件的绝对路径（未展开的全选文件夹按整目录收录）。"""
        root = self.tree.topLevelItem(0)
        if root is None:
            return []
        files: list[str] = []

        def walk(item: QTreeWidgetItem, path: str):
            state = item.checkState(0)
            if state == Qt.Unchecked:
                return
            if os.path.isfile(path):
                files.append(path)
                return
            # 文件夹：是否已展开加载真实子项
            has_real_children = any(
                item.child(i).data(0, Qt.UserRole) is not None
                for i in range(item.childCount()))
            if not has_real_children:
                # 整目录收录（懒加载未展开）
                for dirpath, dirnames, filenames in os.walk(path):
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                    for fn in filenames:
                        files.append(os.path.join(dirpath, fn))
                return
            for i in range(item.childCount()):
                child = item.child(i)
                cpath = child.data(0, Qt.UserRole)
                if cpath:
                    walk(child, cpath)

        walk(root, root.data(0, Qt.UserRole))
        return files

    def _checked_categories(self) -> list[str]:
        """勾选中的顶层分类文件夹名（用于持久化）。"""
        root = self.tree.topLevelItem(0)
        if root is None:
            return []
        cats = []
        for i in range(root.childCount()):
            child = root.child(i)
            if child.checkState(0) == Qt.Checked:
                cats.append(child.text(0))
        return cats

    def reload(self):
        """切换服务器后刷新界面。"""
        self._load_config()

    def _load_config(self):
        sync = self.config.sync
        self._exclude_patterns = [p for p in sync.get("exclude_patterns", [])]
        self._custom_hints = dict(sync.get("category_hints", {}) or {})
        self.ed_local.setText(self.config.local_mc_dir)
        self._reload_tree()

    def _edit_excludes(self):
        """弹窗编辑排除模式（fnmatch 通配，每行一个）。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("排除模式")
        dlg.setMinimumSize(520, 320)
        lay = QVBoxLayout(dlg)
        tip = QLabel("以下匹配的文件将不会被同步。每行一个通配模式，不区分大小写。\n"
                     "例如：*.disabled、crash-*、*backup*、*.tmp")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        ed = QPlainTextEdit()
        ed.setPlainText("\n".join(self._exclude_patterns))
        lay.addWidget(ed, 1)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("确定")
        btns.button(QDialogButtonBox.Ok).setDefault(True)
        btns.button(QDialogButtonBox.Cancel).setText("取消")
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        if dlg.exec() == QDialog.Accepted:
            self._exclude_patterns = [
                p.strip().lower() for p in ed.toPlainText().splitlines() if p.strip()]

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "选择客户端根目录", self.ed_local.text())
        if path:
            self._set_local(path)

    def _set_local(self, path: str):
        self.ed_local.setText(path)
        self.config.local_mc_dir = path
        self._reload_tree()

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
        return {
            "local_dir": self.ed_local.text().strip(),
            "categories": self._checked_categories(),
            "exclude_patterns": list(self._exclude_patterns),
        }

    def _save_sync_settings(self, data: dict):
        self.config.local_mc_dir = data["local_dir"]
        self.config.sync = {
            "categories": data["categories"],
            "exclude_patterns": data["excludes"],
            "category_hints": self._custom_hints,
        }

    # ---------- 预览 ----------
    def _preview(self):
        data = self._collect_sync()
        if not data["local_dir"] or not os.path.isdir(data["local_dir"]):
            winutil.warn(self, "提示", "请先选择有效的客户端根目录。")
            return
        files = self._collect_checked_files()
        if not files:
            winutil.warn(self, "提示", "请先在树中勾选要同步的文件/文件夹。")
            return
        self._save_sync_settings(data)
        self.btn_preview.setEnabled(False)
        self.lbl_status.setText("正在扫描本地文件…")
        worker = Worker(self._scan, files, data)
        worker.progress.connect(self._on_scan_progress)
        worker.done.connect(self._on_scan_done)
        self._worker = worker
        worker.start()

    def _on_scan_progress(self, current, total, message):
        self.lbl_status.setText(fmt_progress(current, total, message, "扫描本地文件"))

    @staticmethod
    def _scan(files: list[str], data: dict, progress_cb=None) -> list:
        """按勾选文件列表扫描并应用排除模式，返回 (本地绝对路径, 远程相对路径, 大小)。"""
        root = Path(data["local_dir"])
        excludes = data["excludes"]
        result = []
        total = len(files)
        for i, local in enumerate(files):
            rel = os.path.relpath(local, root)
            rel_posix = rel.replace("\\", "/")
            if any(fnmatch.fnmatch(rel_posix.lower(), p) for p in excludes):
                if progress_cb:
                    progress_cb(i + 1, total, f"跳过 {rel_posix}…")
                continue
            try:
                size = os.path.getsize(local)
            except OSError:
                continue
            result.append((local, rel_posix, size))
            if progress_cb:
                progress_cb(i + 1, total, rel_posix)
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
        self.lbl_status.setText(fmt_progress(current, total, message, "同步进度"))

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
