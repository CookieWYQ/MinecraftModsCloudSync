# -*- coding: utf-8 -*-
"""服务端 - 文件管理页：直接管理服务器上的模组 / 资源包 / 光影包等文件。

用户需求：
- 在服务端工具里对服务器文件做手动操作（启用 / 禁用、重命名、删除、新建目录）；
- 分门别类展示（模组 / 资源包 / 光影包 / 插件 / 配置 / 数据包 / 其他）；
- 支持拖拽上传：把本地文件直接拖到分类树上就上传到服务器；
- **不把服务端文件下载到本地**：所有操作都在 SFTP 上完成。

「启用 / 禁用」= 在服务器上给文件名加/去 ``.disabled`` 后缀（与 PCL 等启动器一致），
不需要下载再上传。删除文件时若开启了回收站，会先归档到服务器上的历史目录，可还原。

另外提供「映射为本地磁盘（给 PCL 用）」帮助窗口：检测 SSHFS-Win / rclone 并给出
可直接复制的映射命令，把服务器目录假装成本地盘符，PCL 就能直接把整合包下载进去。
"""
import json
import os
import posixpath
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app_common import winutil
from app_common.file_manager import (
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    OTHER_KEY,
    collect_local_items,
    delete_remote,
    find_conflicts,
    format_size,
    is_disabled,
    is_root_top,
    is_toggleable,
    list_dir,
    new_folder,
    rename_remote,
    scan_overview,
    target_rels,
    toggle_remote,
    upload_items,
)
from app_common.history import HistoryStore
from app_common.logger import get_logger
from app_common.sftp import SFTPManager
from app_common.sftp_mount import mount_help
from app_common.worker import Worker, fmt_progress

log = get_logger("server.file_manager_page")

ROLE_DATA = Qt.UserRole


def _fmt_time(ts) -> str:
    try:
        value = int(ts or 0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")


class _FileTree(QTreeWidget):
    """文件树：支持把本地文件/文件夹拖进来（发出 dropped 信号，由页面决定上传位置）。"""

    dropped = Signal(list, object)  # (本地路径列表, 命中的 QTreeWidgetItem 或 None)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls()
                 if u.isLocalFile()]
        paths = [p for p in paths if p]
        if not paths:
            return
        item = self.itemAt(event.position().toPoint())
        self.dropped.emit(paths, item)
        event.acceptProposedAction()


class FileManagerPage(QWidget):
    """服务端文件管理页（手动）。"""

    def __init__(self, config, status_cb=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.status_cb = status_cb
        self._worker = None
        self._overview = None
        self._loaded_key = None
        self._busy = False
        self._pending_upload = None
        self._build()

    # ---------------- 界面 ----------------
    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel("手动管理服务器文件")
        font = title.font()
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        title.setFont(font)
        head.addWidget(title)
        head.addSpacing(10)
        self.lbl_hint = QLabel("所有操作直接在服务器上完成（不会下载到本地）；"
                               "禁用 = 文件名加 .disabled 后缀。")
        self.lbl_hint.setObjectName("muted")
        self.lbl_hint.setWordWrap(True)
        head.addWidget(self.lbl_hint, 1)
        self.btn_mount = QPushButton("映射为本地磁盘（给 PCL 用）…")
        self.btn_mount.setToolTip("检测 SSHFS-Win / rclone，生成把服务器目录映射为"
                                  "本地盘符的命令，让 PCL 能直接把文件下载到服务器")
        self.btn_mount.clicked.connect(self._show_mount_help)
        head.addWidget(self.btn_mount)
        lay.addLayout(head)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("管理位置："))
        self.cb_root = QComboBox()
        self.cb_root.setMinimumWidth(340)
        self.cb_root.currentIndexChanged.connect(lambda _i: self.reload())
        bar.addWidget(self.cb_root)
        bar.addSpacing(8)
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索名称（支持 * 通配）")
        self.ed_search.setClearButtonEnabled(True)
        self.ed_search.textChanged.connect(lambda _t: self._apply_filter())
        bar.addWidget(self.ed_search, 1)
        self.cb_only_disabled = QCheckBox("只看已禁用")
        self.cb_only_disabled.setToolTip("只显示带 .disabled 后缀的文件")
        self.cb_only_disabled.toggled.connect(lambda _c: self._apply_filter())
        bar.addWidget(self.cb_only_disabled)
        self.cb_show_hidden = QCheckBox("显示隐藏项")
        self.cb_show_hidden.setToolTip("显示以 . 开头的文件/目录（默认隐藏，避免误删）")
        self.cb_show_hidden.toggled.connect(lambda _c: self.reload())
        bar.addWidget(self.cb_show_hidden)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.reload)
        bar.addWidget(self.btn_refresh)
        lay.addLayout(bar)

        self.tree = _FileTree()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["名称", "状态", "大小", "修改时间"])
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2, 3):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.tree.setRootIsDecorated(True)
        self.tree.setSortingEnabled(False)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_menu)
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.itemDoubleClicked.connect(self._on_double_clicked)
        self.tree.dropped.connect(self._on_dropped)
        lay.addWidget(self.tree, 1)

        actions = QHBoxLayout()
        self.btn_upload = QPushButton("上传文件…")
        self.btn_upload.clicked.connect(self._upload_files)
        actions.addWidget(self.btn_upload)
        self.btn_upload_dir = QPushButton("上传文件夹…")
        self.btn_upload_dir.clicked.connect(self._upload_folder)
        actions.addWidget(self.btn_upload_dir)
        self.btn_new_dir = QPushButton("新建文件夹…")
        self.btn_new_dir.clicked.connect(self._new_folder)
        actions.addWidget(self.btn_new_dir)
        actions.addSpacing(12)
        self.btn_toggle = QPushButton("启用 / 禁用")
        self.btn_toggle.setToolTip("在服务器上重命名（加/去 .disabled），不下载文件")
        self.btn_toggle.clicked.connect(self._toggle_selected)
        actions.addWidget(self.btn_toggle)
        self.btn_rename = QPushButton("重命名…")
        self.btn_rename.clicked.connect(self._rename_selected)
        actions.addWidget(self.btn_rename)
        self.btn_delete = QPushButton("删除…")
        self.btn_delete.clicked.connect(self._delete_selected)
        actions.addWidget(self.btn_delete)
        actions.addStretch(1)
        lay.addLayout(actions)

        self.lbl_status = QLabel("打开本页时读取服务器目录。")
        self.lbl_status.setObjectName("muted")
        self.lbl_status.setWordWrap(True)
        lay.addWidget(self.lbl_status)

        self._action_buttons = [
            self.btn_upload, self.btn_upload_dir, self.btn_new_dir,
            self.btn_toggle, self.btn_rename, self.btn_delete,
            self.btn_refresh, self.btn_mount,
        ]

    # ---------------- 基础信息 ----------------
    def _root_options(self) -> list[tuple[str, str]]:
        """[(显示名, kind)]；服务端目录与客户端文件仓库不同时都提供。"""
        server_root = self.config.server_root
        files_dir = self.config.files_dir
        options = [("服务端目录（%s）" % server_root, "server")]
        if files_dir.strip("/") != server_root.strip("/"):
            options.append(("客户端文件仓库（%s）" % files_dir, "files"))
        return options

    def _root_kind(self) -> str:
        data = self.cb_root.currentData()
        return str(data or "server")

    def _root(self) -> str:
        return self.config.server_root if self._root_kind() == "server" \
            else self.config.files_dir

    def _sftp_info(self) -> dict:
        """固定当前服务器与目录参数，避免切换服务器后旧任务串台。"""
        return {
            "server_id": self.config.current_id(),
            "host": self.config.host(),
            "port": self.config.port(),
            "username": self.config.username(),
            "password": self.config.password(),
            "root": self._root(),
            "kind": self._root_kind(),
            "show_hidden": self.cb_show_hidden.isChecked(),
            "history_enabled": bool(self.config.history_enabled),
            "history_keep": self.config.history_keep,
            "history_max_mb": self.config.history_max_mb,
            "files_dir": self.config.files_dir,
        }

    def _refresh_root_options(self):
        current = self._root_kind() if self.cb_root.count() else "server"
        self.cb_root.blockSignals(True)
        self.cb_root.clear()
        for label, kind in self._root_options():
            self.cb_root.addItem(label, kind)
        idx = self.cb_root.findData(current)
        self.cb_root.setCurrentIndex(idx if idx >= 0 else 0)
        self.cb_root.blockSignals(False)

    # ---------------- 读取 ----------------
    def showEvent(self, event):
        super().showEvent(event)
        key = (self.config.current_id(), self._root_kind())
        if key != self._loaded_key:
            self.reload()

    def reload(self):
        """重新读取当前管理目录（切换服务器/目录、操作完成后调用）。"""
        self._refresh_root_options()
        if self._busy:
            return
        if not self.config.host():
            self.tree.clear()
            self._overview = None
            self._loaded_key = None
            self.lbl_status.setText("未配置 SFTP 服务器（请先在「服务器设置」页填写）。")
            return
        info = self._sftp_info()
        self._set_busy(True)
        self.lbl_status.setText(f"正在读取 {info['root']} …")
        self._start_worker(self._load_worker, info, on_done=self._on_loaded)

    def _load_worker(self, info, progress_cb=None) -> str:
        with SFTPManager(info["host"], info["port"],
                         info["username"], info["password"]) as sftp:
            data = scan_overview(sftp, info["root"],
                                 show_hidden=info["show_hidden"])
        data["kind"] = info["kind"]
        data["server_id"] = info["server_id"]
        return json.dumps(data, ensure_ascii=False)

    def _on_loaded(self, ok: bool, msg: str):
        self._set_busy(False)
        if not ok:
            self._overview = None
            self.tree.clear()
            self.lbl_status.setText(f"读取失败：{msg}")
            if self.status_cb:
                self.status_cb(False)
            return
        try:
            data = json.loads(msg or "{}")
        except Exception:
            data = {}
        if data.get("server_id") != self.config.current_id():
            # 读取期间切换了服务器：丢弃过期结果，并重新加载新服务器的目录
            self.reload()
            return
        self._overview = data
        self._loaded_key = (self.config.current_id(), self._root_kind())
        self._fill_tree(data)
        if self.status_cb:
            self.status_cb(True)
        self.lbl_status.setText(
            f"共 {self._count_entries(data)} 个文件；"
            f"拖拽本地文件到分类上即可上传，右键可启用/禁用、重命名、删除。")

    @staticmethod
    def _count_entries(data) -> int:
        return sum(len(c.get("entries") or [])
                   for c in (data.get("categories") or []))

    def _fill_tree(self, data):
        self.tree.clear()
        root = data.get("root") or ""
        for cat in data.get("categories") or []:
            item = QTreeWidgetItem(self.tree)
            item.setData(0, ROLE_DATA, {
                "kind": "category",
                "key": cat.get("key"),
                "rel": cat.get("dir") if cat.get("key") != OTHER_KEY else "",
                "toggle": bool(cat.get("toggle")),
                "exists": bool(cat.get("exists")),
            })
            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)
            item.setText(0, self._category_text(cat))
            item.setText(1, self._category_status(cat))
            item.setText(2, "")
            item.setText(3, "")
            item.setToolTip(0, cat.get("dir") or root)
            for entry in cat.get("entries") or []:
                self._add_entry(item, entry)
            if cat.get("exists"):
                item.setExpanded(True)
        self._apply_filter()

    def _category_text(self, cat) -> str:
        label = cat.get("label") or cat.get("key") or ""
        if not cat.get("exists"):
            return f"{label}（目录不存在，上传时自动创建）"
        entries = [e for e in (cat.get("entries") or []) if not e.get("is_dir")]
        if cat.get("toggle"):
            disabled = sum(1 for e in entries if e.get("disabled"))
            return f"{label}（{len(entries)}）"
        return f"{label}（{len(entries)}）" if entries else f"{label}"

    @staticmethod
    def _category_status(cat) -> str:
        if not cat.get("toggle") or not cat.get("exists"):
            return ""
        entries = [e for e in (cat.get("entries") or [])
                   if not e.get("is_dir")]
        disabled = sum(1 for e in entries if e.get("disabled"))
        return f"启用 {len(entries) - disabled} / 禁用 {disabled}"

    def _add_entry(self, parent: QTreeWidgetItem, entry: dict) -> QTreeWidgetItem:
        item = QTreeWidgetItem(parent)
        item.setData(0, ROLE_DATA, {
            "kind": "entry",
            "rel": entry.get("rel") or "",
            "is_dir": bool(entry.get("is_dir")),
            "disabled": bool(entry.get("disabled")),
            "toggle": False,
            "loaded": False,
        })
        name = entry.get("name") or ""
        item.setText(0, name)
        if entry.get("is_dir"):
            item.setText(1, "目录")
            item.setText(2, "—")
            # 占位子项：展开时才真正读取，避免大目录一次全拉
            placeholder = QTreeWidgetItem(item)
            placeholder.setText(0, "…")
            placeholder.setData(0, ROLE_DATA, {"kind": "placeholder"})
            item.setToolTip(0, entry.get("rel") or "")
        else:
            disabled = bool(entry.get("disabled"))
            item.setText(1, "已禁用" if disabled else "启用中")
            item.setText(2, format_size(entry.get("size")))
            item.setToolTip(0, f"{entry.get('rel')}\n双击/右键可操作")
        item.setText(3, _fmt_time(entry.get("mtime")))
        if entry.get("disabled"):
            for col in range(4):
                item.setForeground(col, QBrush(QColor("#9e9e9e")))
        return item

    # ---------------- 懒加载子目录 ----------------
    def _on_expanded(self, item):
        data = item.data(0, ROLE_DATA) or {}
        if data.get("kind") != "entry" or not data.get("is_dir"):
            return
        if data.get("loaded"):
            return
        if self._busy:
            return
        info = self._sftp_info()
        info["rel"] = data.get("rel") or ""
        self._set_busy(True)
        self._start_worker(self._load_dir_worker, info,
                           on_done=lambda ok, msg, it=item:
                           self._on_dir_loaded(ok, msg, it))

    def _load_dir_worker(self, info, progress_cb=None) -> str:
        with SFTPManager(info["host"], info["port"],
                         info["username"], info["password"]) as sftp:
            entries = list_dir(sftp, info["root"], info.get("rel") or "",
                               show_hidden=info["show_hidden"])
        return json.dumps({"rel": info.get("rel") or "", "entries": entries},
                          ensure_ascii=False)

    def _on_dir_loaded(self, ok: bool, msg: str, item):
        self._set_busy(False)
        try:
            data = json.loads(msg or "{}") if ok else {}
        except Exception:
            data = {}
        try:
            meta = dict(item.data(0, ROLE_DATA) or {})
        except RuntimeError:
            return  # 树已被重建
        if meta.get("kind") != "entry":
            return
        meta["loaded"] = True
        item.setData(0, ROLE_DATA, meta)
        item.takeChildren()
        if not ok:
            self.lbl_status.setText(f"读取目录失败：{msg}")
            return
        for entry in data.get("entries") or []:
            self._add_entry(item, entry)
        self._apply_filter()

    def _on_double_clicked(self, item, _column):
        data = item.data(0, ROLE_DATA) or {}
        if data.get("kind") == "entry" and data.get("is_dir"):
            item.setExpanded(not item.isExpanded())

    # ---------------- 过滤 ----------------
    def _apply_filter(self):
        text = self.ed_search.text().strip().lower()
        only_disabled = self.cb_only_disabled.isChecked()

        def walk(item):
            data = item.data(0, ROLE_DATA) or {}
            if data.get("kind") == "category":
                visible_any = False
                label_match = not text or text in item.text(0).lower()
                for i in range(item.childCount()):
                    child = item.child(i)
                    if self._filter_entry(child, text, only_disabled,
                                          force=label_match):
                        visible_any = True
                item.setHidden(not (visible_any or label_match))
                return visible_any or label_match
            return True

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))

    def _filter_entry(self, item, text, only_disabled, force=False) -> bool:
        data = item.data(0, ROLE_DATA) or {}
        if data.get("kind") == "placeholder":
            item.setHidden(False)
            return False
        name = item.text(0).lower()
        matched = force or not text or text in name
        if (data.get("kind") == "entry" and not data.get("is_dir")
                and only_disabled and self._in_toggle_category(item)
                and not data.get("disabled")):
            matched = False
        item.setHidden(not matched)
        # 目录的子项也递归过滤
        if data.get("is_dir"):
            for i in range(item.childCount()):
                self._filter_entry(item.child(i), text, only_disabled, force)
        return matched

    @staticmethod
    def _in_toggle_category(item) -> bool:
        """该条目是否位于支持启用/禁用的分类下（模组/资源包/光影包…）。"""
        node = item
        while node is not None:
            data = node.data(0, ROLE_DATA) or {}
            if data.get("kind") == "category":
                return bool(data.get("toggle"))
            node = node.parent()
        return False

    # ---------------- 通用 Worker ----------------
    def _set_busy(self, busy: bool):
        self._busy = busy
        for btn in self._action_buttons:
            btn.setEnabled(not busy)
        self.tree.setEnabled(not busy)

    def _start_worker(self, fn, *args, on_done=None):
        worker = Worker(fn, *args)
        worker.progress.connect(self._on_progress)
        if on_done is not None:
            worker.done.connect(on_done)
        else:
            worker.done.connect(self._on_done_default)
        self._worker = worker
        worker.start()

    def _on_progress(self, cur: int, total: int, message: str):
        self.lbl_status.setText(fmt_progress(cur, total, message, "文件管理"))

    def _on_done_default(self, ok: bool, msg: str):
        self._set_busy(False)
        if not ok:
            self.lbl_status.setText(f"操作失败：{msg}")

    # ---------------- 选择与目标目录 ----------------
    def _selected_items(self) -> list[QTreeWidgetItem]:
        return [item for item in self.tree.selectedItems()
                if (item.data(0, ROLE_DATA) or {}).get("kind") == "entry"]

    def _selected_file_rels(self) -> list[str]:
        rels = []
        for item in self._selected_items():
            data = item.data(0, ROLE_DATA) or {}
            if not data.get("is_dir"):
                rels.append(data.get("rel") or "")
        return [r for r in rels if r]

    def _selected_entry_data(self):
        items = self._selected_items()
        if len(items) != 1:
            return None
        return items[0].data(0, ROLE_DATA) or {}

    def _target_dir_from_item(self, item) -> str | None:
        """根据树上的条目推断上传/新建目录（相对路径）。None = 无法判断。"""
        if item is None:
            return None
        data = item.data(0, ROLE_DATA) or {}
        kind = data.get("kind")
        if kind == "category":
            return data.get("rel") or ""
        if kind == "entry":
            rel = data.get("rel") or ""
            return rel if data.get("is_dir") else posixpath.dirname(rel)
        if kind == "placeholder":
            parent = item.parent()
            return self._target_dir_from_item(parent) if parent is not None else ""
        return None

    def _current_target_dir(self) -> str | None:
        """当前上传/新建目标：选中条目所在分类/目录；未选中时让用户选分类。"""
        item = self.tree.currentItem()
        target = self._target_dir_from_item(item)
        if target is not None:
            return target
        existing = self._existing_category_dirs()
        labels = [CATEGORY_LABELS.get(k, k) for k in CATEGORY_ORDER] + \
                 [d for d in existing if d not in CATEGORY_ORDER]
        keys = list(CATEGORY_ORDER) + [d for d in existing if d not in CATEGORY_ORDER]
        label, ok = QInputDialog.getItem(
            self, "选择上传位置", "要放到哪个分类目录？",
            [CATEGORY_LABELS.get(k, k) if k in CATEGORY_LABELS else k
             for k in keys], 0, False)
        if not ok or not label:
            return None
        idx = [CATEGORY_LABELS.get(k, k) if k in CATEGORY_LABELS else k
               for k in keys].index(label)
        return keys[idx]

    def _existing_category_dirs(self) -> list[str]:
        dirs = []
        for cat in (self._overview or {}).get("categories") or []:
            if cat.get("exists") and cat.get("dir"):
                dirs.append(cat.get("dir"))
        return dirs

    # ---------------- 启用 / 禁用 ----------------
    def _toggle_selected(self):
        rels = self._selected_file_rels()
        category_item = None
        if not rels:
            # 没选具体文件时：选中某个分类 = 批量处理该分类下所有文件
            item = self.tree.currentItem()
            data = item.data(0, ROLE_DATA) if item is not None else {}
            if data.get("kind") == "category" and data.get("toggle"):
                category_item = item
                for i in range(item.childCount()):
                    child = item.child(i)
                    cdata = child.data(0, ROLE_DATA) or {}
                    if (cdata.get("kind") == "entry"
                            and not cdata.get("is_dir")
                            and is_toggleable(cdata.get("rel") or "", False)):
                        rels.append(cdata.get("rel") or "")
        if not rels:
            winutil.warn(self, "提示", "请先选中要启用/禁用的文件，或选中整个"
                                      "「模组 / 资源包 / 光影包」分类批量处理。")
            return
        invalid = [r for r in rels if not is_toggleable(r, False)]
        rels = [r for r in rels if is_toggleable(r, False)]
        if not rels:
            winutil.warn(self, "提示", "选中的文件不在可启用/禁用的分类里"
                                      "（支持：模组 / 资源包 / 光影包 / 插件 / 数据包）。")
            return
        if category_item is not None:
            label = category_item.text(0)
            if not winutil.confirm(self, "批量启用 / 禁用",
                                   f"将对「{label}」下的 {len(rels)} 个文件"
                                   f"逐个切换启用状态（在服务器上重命名，不下载文件）。"
                                   f"\n\n继续吗？"):
                return
        states = [self._is_rel_disabled(r) for r in rels]
        if not rels:
            winutil.warn(self, "提示", "选中的文件不在可启用/禁用的分类里"
                                      "（支持：模组 / 资源包 / 光影包 / 插件 / 数据包）。")
            return
        if all(states):
            enable = True
        elif not any(states):
            enable = False
        else:
            box = QMessageBox(self)
            box.setWindowTitle("启用 / 禁用")
            box.setText(f"选中的 {len(rels)} 个文件状态不一致，要全部启用还是全部禁用？")
            b_enable = box.addButton("全部启用", QMessageBox.YesRole)
            b_disable = box.addButton("全部禁用", QMessageBox.NoRole)
            box.addButton("取消", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() == b_enable:
                enable = True
            elif box.clickedButton() == b_disable:
                enable = False
            else:
                return
        action = "启用" if enable else "禁用"
        info = self._sftp_info()
        self._set_busy(True)
        self.lbl_status.setText(f"正在{action} {len(rels)} 个文件…")
        self._start_worker(
            self._toggle_worker, info, rels, enable,
            on_done=lambda ok, msg: self._on_toggle_done(ok, msg, action, invalid))

    def _is_rel_disabled(self, rel: str) -> bool:
        for item in self._selected_items():
            data = item.data(0, ROLE_DATA) or {}
            if data.get("rel") == rel:
                return bool(data.get("disabled"))
        return is_disabled(posixpath.basename(rel))

    def _toggle_worker(self, info, rels, enable, progress_cb=None) -> str:
        with SFTPManager(info["host"], info["port"],
                         info["username"], info["password"]) as sftp:
            summary = toggle_remote(sftp, info["root"], rels, enable)
        summary["root"] = info["root"]
        return json.dumps(summary, ensure_ascii=False)

    def _on_toggle_done(self, ok: bool, msg: str, action: str, invalid):
        self._set_busy(False)
        if not ok:
            winutil.error(self, f"{action}失败", str(msg))
            return
        try:
            data = json.loads(msg or "{}")
        except Exception:
            data = {}
        lines = [f"已{action} {len(data.get('ok') or [])} 个文件。"]
        if data.get("conflicts"):
            lines.append("以下文件目标名已存在，未改动（请先处理同名文件）："
                         + "、".join(data["conflicts"][:10]))
        if data.get("errors"):
            lines.append("失败：" + "；".join(
                f"{rel}（{why}）" for rel, why in data["errors"][:10]))
        if invalid:
            lines.append("部分选中项不支持启用/禁用，已跳过。")
        self.lbl_status.setText(lines[0])
        if data.get("conflicts") or data.get("errors"):
            winutil.info_list(self, f"{action}结果", lines[1:], lines[0])
        self.reload()

    # ---------------- 重命名 ----------------
    def _rename_selected(self):
        data = self._selected_entry_data()
        if not data:
            winutil.warn(self, "提示", "请先选中一个文件或目录。")
            return
        rel = data.get("rel") or ""
        old_name = posixpath.basename(rel)
        new_name, ok = QInputDialog.getText(self, "重命名", "新名称：",
                                            QLineEdit.Normal, old_name)
        if not ok or not new_name or new_name == old_name:
            return
        info = self._sftp_info()
        self._set_busy(True)
        self.lbl_status.setText(f"正在重命名 {old_name} → {new_name} …")
        self._start_worker(self._rename_worker, info, rel, new_name,
                           on_done=self._on_rename_done)

    def _rename_worker(self, info, rel, new_name, progress_cb=None) -> str:
        with SFTPManager(info["host"], info["port"],
                         info["username"], info["password"]) as sftp:
            new_rel = rename_remote(sftp, info["root"], rel, new_name)
        return json.dumps({"new_rel": new_rel}, ensure_ascii=False)

    def _on_rename_done(self, ok: bool, msg: str):
        self._set_busy(False)
        if not ok:
            winutil.error(self, "重命名失败", str(msg))
            return
        try:
            new_rel = json.loads(msg or "{}").get("new_rel", "")
        except Exception:
            new_rel = ""
        self.lbl_status.setText(f"已重命名为 {new_rel}")
        self.reload()

    # ---------------- 新建文件夹 ----------------
    def _new_folder(self):
        target = self._current_target_dir()
        if target is None:
            return
        name, ok = QInputDialog.getText(self, "新建文件夹", "文件夹名称：")
        if not ok or not name.strip():
            return
        info = self._sftp_info()
        self._set_busy(True)
        self.lbl_status.setText("正在创建文件夹…")
        self._start_worker(self._mkdir_worker, info, target, name.strip(),
                           on_done=self._on_mkdir_done)

    def _mkdir_worker(self, info, target_dir, name, progress_cb=None) -> str:
        with SFTPManager(info["host"], info["port"],
                         info["username"], info["password"]) as sftp:
            new_rel = new_folder(sftp, info["root"], target_dir, name)
        return json.dumps({"new_rel": new_rel}, ensure_ascii=False)

    def _on_mkdir_done(self, ok: bool, msg: str):
        self._set_busy(False)
        if not ok:
            winutil.error(self, "新建文件夹失败", str(msg))
            return
        try:
            new_rel = json.loads(msg or "{}").get("new_rel", "")
        except Exception:
            new_rel = ""
        self.lbl_status.setText(f"已创建 {new_rel}")
        self.reload()

    # ---------------- 删除 ----------------
    def _delete_selected(self):
        items = self._selected_items()
        if not items:
            winutil.warn(self, "提示", "请先选中要删除的文件或目录。")
            return
        rels, dirs, protected = [], [], []
        for item in items:
            data = item.data(0, ROLE_DATA) or {}
            rel = data.get("rel") or ""
            if is_root_top(rel):
                protected.append(rel)
                continue
            rels.append(rel)
            if data.get("is_dir"):
                dirs.append(rel)
        if not rels:
            winutil.warn(self, "提示", "服务端根目录的顶层文件/目录受保护，不能在这里删除。")
            return
        history_on = bool(self.config.history_enabled) and not dirs
        lines = [f"共 {len(rels)} 项："] + rels[:12]
        if len(rels) > 12:
            lines.append(f"…其余 {len(rels) - 12} 项")
        if protected:
            lines.append(f"（根目录顶层 {len(protected)} 项受保护，已跳过："
                         + "、".join(protected[:6])
                         + ("…" if len(protected) > 6 else "") + "）")
        lines.append("")
        if dirs:
            lines.append(f"注意：其中 {len(dirs)} 个目录会连同内部所有文件直接删除，"
                         "不会进回收站。")
        lines.append("文件删除后可在「回收站/历史版本」里还原。" if history_on
                     else "未开启回收站（或包含目录）：文件将被永久删除。")
        if not winutil.confirm_list(self, "确认删除", "\n".join(lines), rels[:12],
                                    ok_label="删除", cancel_label="取消"):
            return
        info = self._sftp_info()
        self._set_busy(True)
        self.lbl_status.setText(f"正在删除 {len(rels)} 项…")
        self._start_worker(self._delete_worker, info, rels,
                           on_done=self._on_delete_done)

    def _delete_worker(self, info, rels, progress_cb=None) -> str:
        store = None
        with SFTPManager(info["host"], info["port"],
                         info["username"], info["password"]) as sftp:
            if info["history_enabled"]:
                store = HistoryStore(sftp, info["files_dir"])
            summary = delete_remote(
                sftp, info["root"], rels, store,
                "server" if info["kind"] == "server" else "client")
            if store is not None:
                summary["pruned"] = len(store.prune(
                    info["history_keep"], info["history_max_mb"]))
                if store.dirty:
                    try:
                        store.save()
                    except Exception as exc:
                        log.warning("保存历史索引失败: %s", exc)
        summary["root"] = info["root"]
        return json.dumps(summary, ensure_ascii=False)

    def _on_delete_done(self, ok: bool, msg: str):
        self._set_busy(False)
        if not ok:
            winutil.error(self, "删除失败", str(msg))
            return
        try:
            data = json.loads(msg or "{}")
        except Exception:
            data = {}
        text = f"已删除 {len(data.get('ok') or [])} 项"
        if data.get("archived"):
            text += f"（{data['archived']} 个文件已移入服务器回收站）"
        self.lbl_status.setText(text)
        if data.get("errors"):
            winutil.info_list(self, "删除结果",
                              [f"{rel}：{why}" for rel, why in data["errors"][:12]],
                              text + "；部分失败")
        self.reload()

    # ---------------- 上传 ----------------
    def _upload_files(self):
        paths, _f = QFileDialog.getOpenFileNames(
            self, "选择要上传的文件（可多选）", "",
            "所有文件 (*.*)")
        if paths:
            self._begin_upload(paths)

    def _upload_folder(self):
        path = QFileDialog.getExistingDirectory(self, "选择要上传的文件夹")
        if path:
            self._begin_upload([path])

    def _on_dropped(self, paths, item):
        if self._busy:
            self.lbl_status.setText("正在执行操作，请稍后再拖入文件。")
            return
        target = self._target_dir_from_item(item)
        if target is None:
            target = self._current_target_dir()
        if target is None:
            return
        self._begin_upload(paths, target)

    def _begin_upload(self, paths, target_dir=None):
        if self._busy:
            return
        if not paths:
            return
        if target_dir is None:
            target_dir = self._current_target_dir()
        if target_dir is None:
            return
        items = collect_local_items(paths)
        if not items:
            winutil.warn(self, "提示", "没有可上传的文件（已跳过隐藏项/临时文件）。")
            return
        total = sum(item[2] for item in items)
        self._pending_upload = {"items": items, "target_dir": target_dir}
        info = self._sftp_info()
        self._set_busy(True)
        self.lbl_status.setText(
            f"准备上传 {len(items)} 个文件（{format_size(total)}）到 "
            f"/{target_dir} …")
        self._start_worker(self._prepare_upload, info, target_dir, items,
                           on_done=self._on_prepared)

    def _prepare_upload(self, info, target_dir, items, progress_cb=None) -> str:
        rels = [rel for _f, rel, _s in target_rels(target_dir, items)]
        with SFTPManager(info["host"], info["port"],
                         info["username"], info["password"]) as sftp:
            conflicts = find_conflicts(sftp, info["root"], rels)
        return json.dumps({"conflicts": conflicts}, ensure_ascii=False)

    def _on_prepared(self, ok: bool, msg: str):
        self._set_busy(False)
        pending = self._pending_upload or {}
        self._pending_upload = None
        if not ok:
            winutil.error(self, "上传准备失败", str(msg))
            return
        try:
            conflicts = json.loads(msg or "{}").get("conflicts") or []
        except Exception:
            conflicts = []
        items = pending.get("items") or []
        target_dir = pending.get("target_dir") or ""
        if not items:
            return
        overwrite = True
        if conflicts:
            box = QMessageBox(self)
            box.setWindowTitle("目标已存在")
            box.setText(f"{len(conflicts)} 个文件在服务器上已存在，如何处理？")
            box.setInformativeText("、".join(conflicts[:8])
                                   + ("…" if len(conflicts) > 8 else ""))
            b_over = box.addButton("覆盖", QMessageBox.YesRole)
            b_skip = box.addButton("跳过已存在的", QMessageBox.NoRole)
            box.addButton("取消", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() == b_over:
                overwrite = True
            elif box.clickedButton() == b_skip:
                overwrite = False
            else:
                return
        info = self._sftp_info()
        self._set_busy(True)
        self.lbl_status.setText(f"正在上传 {len(items)} 个文件…")
        self._start_worker(self._upload_worker, info, target_dir, items,
                           overwrite, on_done=self._on_upload_done)

    def _upload_worker(self, info, target_dir, items, overwrite,
                       progress_cb=None) -> str:
        rels = target_rels(target_dir, items)
        with SFTPManager(info["host"], info["port"],
                         info["username"], info["password"]) as sftp:
            summary = upload_items(sftp, info["root"], rels,
                                   overwrite=overwrite,
                                   progress_cb=progress_cb)
        summary["root"] = info["root"]
        return json.dumps(summary, ensure_ascii=False)

    def _on_upload_done(self, ok: bool, msg: str):
        self._set_busy(False)
        if not ok:
            winutil.error(self, "上传失败", str(msg))
            return
        try:
            data = json.loads(msg or "{}")
        except Exception:
            data = {}
        text = (f"上传完成：成功 {len(data.get('uploaded') or [])} 个"
                f"（{format_size(data.get('bytes'))}）")
        if data.get("skipped"):
            text += f"，跳过 {len(data['skipped'])} 个"
        echo = [text]
        if data.get("errors"):
            echo.append("失败：" + "；".join(
                f"{rel}（{why}）" for rel, why in data["errors"][:10]))
        self.lbl_status.setText(text)
        if data.get("errors"):
            winutil.info_list(self, "上传结果", echo, "部分文件上传失败")
        self.reload()

    # ---------------- 映射为本地磁盘（PCL） ----------------
    def _show_mount_help(self):
        host = self.config.host()
        # 默认映射「客户端文件仓库」（client_files）：它的目录结构与 .minecraft 类似
        # （mods / resourcepacks / config），PCL 把下载目录指到这里正好进入待发布区；
        # 不建议把服务端自己的 mods 目录给 PCL 下载客户端模组（服务端会加载后报错）。
        root = self.config.files_dir
        data = mount_help(host, self.config.port(), self.config.username(),
                          root, "Z:")
        dlg = QDialog(self)
        dlg.setWindowTitle("映射为本地磁盘（给 PCL 用）")
        dlg.resize(720, 520)
        lay = QVBoxLayout(dlg)
        tools = data.get("tools") or {}
        status = []
        status.append("SSHFS-Win：" + ("已安装 " + tools["sshfs"]
                                      if tools.get("sshfs") else "未安装"))
        status.append("WinFsp：" + ("已安装" if tools.get("winfsp") else "未安装"))
        status.append("rclone：" + ("已安装 " + tools["rclone"]
                                    if tools.get("rclone") else "未安装"))
        lbl_tools = QLabel("；".join(status))
        lbl_tools.setObjectName("muted")
        lbl_tools.setWordWrap(True)
        lay.addWidget(lbl_tools)
        editor = QPlainTextEdit()
        editor.setReadOnly(True)
        text = [f"映射目标：{data.get('remote_root')}"
                f"（客户端文件仓库；PCL 下载的 mods / resourcepacks 会直接进入"
                f"「发布待办」的待发布区）", ""]
        text.append("说明：")
        text += ["· " + note for note in data.get("notes") or []]
        text.append("")
        if not host:
            text.append("（未配置 SFTP 服务器，填写连接信息后这里会生成可复制的命令）")
        for cmd in data.get("commands") or []:
            state = "可用" if cmd.get("available") else "未检测到对应软件"
            text.append(f"【{cmd['title']}｜{state}】")
            text.append(cmd["command"])
            text.append("")
        text.append("映射成功后，把 PCL 的「游戏目录」选到该盘符下的目录即可。")
        editor.setPlainText("\n".join(text))
        lay.addWidget(editor, 1)
        row = QHBoxLayout()
        btn_copy = QPushButton("复制全部命令")
        btn_copy.clicked.connect(
            lambda: (self._copy_mount_commands(data),
                     self.lbl_status.setText("映射命令已复制到剪贴板")))
        row.addWidget(btn_copy)
        row.addStretch(1)
        box = QDialogButtonBox(QDialogButtonBox.Close)
        box.rejected.connect(dlg.reject)
        box.accepted.connect(dlg.accept)
        row.addWidget(box)
        lay.addLayout(row)
        dlg.exec()

    @staticmethod
    def _copy_mount_commands(data):
        from PySide6.QtWidgets import QApplication
        lines = [cmd["command"] for cmd in data.get("commands") or []]
        QApplication.clipboard().setText("\n".join(lines))

    # ---------------- 右键菜单 ----------------
    def _on_menu(self, pos):
        item = self.tree.itemAt(pos)
        menu = QMenu(self)
        if item is not None:
            item.setSelected(True)
            self.tree.setCurrentItem(item)
            data = item.data(0, ROLE_DATA) or {}
            if data.get("kind") == "entry":
                is_dir = bool(data.get("is_dir"))
                rel = data.get("rel") or ""
                if not is_dir and is_toggleable(rel, False):
                    disabled = bool(data.get("disabled"))
                    menu.addAction("启用该文件" if disabled else "禁用该文件",
                                   lambda: self._toggle_selected())
                menu.addAction("重命名…", self._rename_selected)
                menu.addAction("删除…", self._delete_selected)
                if is_dir:
                    menu.addAction("上传到这里…", self._upload_here)
                    menu.addAction("在此新建文件夹…", self._new_folder)
            elif data.get("kind") == "category":
                menu.addAction("上传到这里…", self._upload_here)
                menu.addAction("在此新建文件夹…", self._new_folder)
                if data.get("toggle"):
                    menu.addAction("启用 / 禁用该分类全部文件…",
                                   self._toggle_selected)
                menu.addAction("仅显示已禁用文件",
                               lambda: self.cb_only_disabled.setChecked(True))
            menu.addSeparator()
        menu.addAction("刷新", self.reload)
        menu.addAction("展开全部", self.tree.expandAll)
        menu.addAction("折叠全部", self.tree.collapseAll)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _upload_here(self):
        item = self.tree.currentItem()
        target = self._target_dir_from_item(item)
        if target is None:
            return
        self._begin_upload_from_dialog(target)

    def _begin_upload_from_dialog(self, target):
        paths, _f = QFileDialog.getOpenFileNames(
            self, "选择要上传的文件（可多选）", "", "所有文件 (*.*)")
        if paths:
            self._begin_upload(paths, target)
