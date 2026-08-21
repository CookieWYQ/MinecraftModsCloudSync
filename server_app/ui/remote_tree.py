# -*- coding: utf-8 -*-
"""服务端 - 远程文件树组件：懒加载浏览 SFTP 文件仓库。

- 懒加载：展开文件夹时才读取远程子目录；可展开的文件夹带三角标注（空文件夹自动收起三角）
- 排序：按名称 / 大小 / 类型 / 时间（文件夹在前，与差异审核一致），支持正序 / 倒序切换
- 搜索：glob 通配过滤已加载条目
- 双击文件发出 file_activated(相对路径)
"""
import fnmatch
import json
from datetime import datetime

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app_common.logger import get_logger
from app_common.mcmod_link import add_mcmod_menu_actions, mod_search_name
from app_common.sftp import SFTPManager
from app_common.worker import Worker

log = get_logger("server.remote_tree")


def fmt_size(size: int) -> str:
    if size < 0:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


class SelectTreeWidget(QTreeWidget):
    """支持 Ctrl/Shift 多选与 Ctrl+Shift+A 反选的树控件（改动树 / 审核树 / 文件浏览器通用）。

    - Ctrl/Shift + 点击：多选 / 连续多选（ExtendedSelection）
    - Ctrl+Shift+A：反选（只翻转可见条目，受搜索过滤影响）
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)

    def keyPressEvent(self, event):
        if (event.key() == Qt.Key_A
                and event.modifiers() == (Qt.ControlModifier | Qt.ShiftModifier)):
            self._invert_selection()
            event.accept()
            return
        super().keyPressEvent(event)

    def _invert_selection(self):
        """反选：翻转所有可见条目的选中状态。"""
        items = []

        def walk(item):
            items.append(item)
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self.topLevelItemCount()):
            walk(self.topLevelItem(i))
        for item in items:
            if not item.isHidden():
                item.setSelected(not item.isSelected())


class RemoteTreeWidget(QWidget):
    """远程文件仓库树形浏览器。"""

    file_activated = Signal(str)  # 双击文件 → files 目录下相对路径

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self._worker = None
        self._browse_root = ""     # 当前浏览的完整远程根目录
        self._browse_label = ""    # 根节点显示名称
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        toolbar = QHBoxLayout()
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索名称（支持 * 通配）")
        self.ed_search.setClearButtonEnabled(True)
        toolbar.addWidget(self.ed_search, 1)
        toolbar.addWidget(QLabel("排序："))
        self.cb_sort = QComboBox()
        self.cb_sort.addItem("按名称", "name")
        self.cb_sort.addItem("按大小", "size")
        self.cb_sort.addItem("按类型", "type")
        self.cb_sort.addItem("按时间", "time")
        toolbar.addWidget(self.cb_sort)
        self.btn_dir = QPushButton("↓ 正序")
        self.btn_dir.setCheckable(True)
        self.btn_dir.setToolTip("切换正序 / 倒序")
        toolbar.addWidget(self.btn_dir)
        self.btn_refresh = QPushButton("刷新")
        toolbar.addWidget(self.btn_refresh)
        lay.addLayout(toolbar)

        self.tree = SelectTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["文件 / 文件夹", "大小", "类型", "时间"])
        self.tree.setRootIsDecorated(True)  # 保留展开三角标注
        # 名称列拉伸占满宽度，完整显示长文件名；其余列按内容自适应
        header = self.tree.header()
        header.setSectionsMovable(True)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2, 3):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.tree.setMinimumHeight(200)
        self.tree.setSortingEnabled(False)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_menu)
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.itemDoubleClicked.connect(self._on_double_clicked)
        lay.addWidget(self.tree, 1)

        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("muted")
        lay.addWidget(self.lbl_status)

        self.ed_search.textChanged.connect(lambda _: self._apply_filter())
        self.cb_sort.currentIndexChanged.connect(self._re_sort_loaded)
        self.btn_dir.toggled.connect(self._re_sort_loaded)
        self.btn_refresh.clicked.connect(self.reload)
        self._set_busy(False)

    # ---------- 加载 ----------
    def _set_busy(self, busy: bool):
        self.btn_refresh.setEnabled(not busy)
        if busy:
            self.lbl_status.setText("正在读取远程目录…")

    def reload(self, remote_dir=None, label=None):
        """重新加载浏览根目录（默认服务端文件仓库；remote_dir 为完整远程路径）。"""
        self._browse_root = remote_dir if remote_dir is not None else self.config.files_dir
        self._browse_label = label or self._browse_root
        self.tree.clear()
        self._set_busy(False)
        if not self.config.host():
            self.lbl_status.setText("未配置 SFTP 服务器（请先在「SFTP 设置」页填写）。")
            return
        self._set_busy(True)
        root_item = QTreeWidgetItem([self._browse_label, "", "", ""])
        root_item.setData(0, Qt.UserRole,
                          {"rel": "", "is_dir": True, "size": 0, "loaded": False})
        root_item.setToolTip(0, self._browse_root)
        self.tree.addTopLevelItem(root_item)
        root_item.setExpanded(True)  # 触发 _on_expanded → 加载顶层
        self._apply_filter()

    def _fetch_entries(self, remote_dir, progress_cb=None) -> str:
        """读取远程目录条目，返回 JSON {"entries": [[名称, 是否目录, 大小, 修改时间], ...]}。
        目录不存在时返回 {"error": "..."}，避免误报为「空文件夹」。"""
        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            if not sftp.exists(remote_dir):
                return json.dumps({"error": f"目录不存在：{remote_dir}"},
                                  ensure_ascii=False)
            entries = sftp.list_entries(remote_dir)
        return json.dumps({"entries": entries}, ensure_ascii=False)

    def _on_expanded(self, item):
        data = item.data(0, Qt.UserRole) or {}
        if not data.get("is_dir") or data.get("loaded"):
            return
        self._set_busy(True)
        worker = Worker(self._fetch_entries, self._remote_path(item))
        worker.done.connect(lambda ok, msg: self._on_entries(ok, msg, item))
        self._worker = worker
        worker.start()

    def _on_entries(self, ok: bool, msg: str, item):
        self._set_busy(False)
        try:
            data = dict(item.data(0, Qt.UserRole) or {})
        except RuntimeError:
            # 树已被清空/重建（如切换服务器时 reload），丢弃过期结果
            return
        data["loaded"] = True
        item.setData(0, Qt.UserRole, data)
        item.takeChildren()
        if not ok:
            self.lbl_status.setText(f"读取失败：{msg}")
            return
        try:
            payload = json.loads(msg or "{}")
        except Exception:
            payload = {}
        if payload.get("error"):
            self.lbl_status.setText(payload["error"])
            return
        entries = []
        for e in payload.get("entries", []):
            try:
                fname = str(e[0])
                is_dir = bool(e[1])
                size = int(e[2])
                mtime = int(e[3] or 0)
            except (IndexError, TypeError, ValueError):
                continue
            ftype = fname.rsplit(".", 1)[-1].lower() if "." in fname else "文件"
            entries.append({"n": fname, "d": is_dir, "s": size,
                            "t": mtime, "ftype": ftype})
        self._populate(item, entries)
        self.lbl_status.setText(f"共 {len(entries)} 项" if entries else "（空文件夹）")

    def _populate(self, item, entries):
        parent_rel = (item.data(0, Qt.UserRole) or {}).get("rel", "")
        for e in self._sorted_entries(entries):
            rel = f"{parent_rel}/{e['n']}" if parent_rel else e["n"]
            if e["d"]:
                child = QTreeWidgetItem([e["n"], "", "", ""])
            else:
                time_text = (datetime.fromtimestamp(e["t"]).strftime("%Y-%m-%d %H:%M")
                             if e.get("t") else "—")
                child = QTreeWidgetItem([e["n"], fmt_size(e["s"]),
                                         e.get("ftype", "文件"), time_text])
            child.setData(0, Qt.UserRole,
                          {"rel": rel, "is_dir": e["d"], "size": e["s"],
                           "mtime": e.get("t", 0), "ftype": e.get("ftype", ""),
                           "loaded": False})
            child.setToolTip(0, rel)
            item.addChild(child)
            if e["d"]:
                # 占位子项：保证有三角展开标注，展开时再读取真实内容
                QTreeWidgetItem(child, ["", "", "", ""])
        self._apply_filter()

    # ---------- 排序 / 搜索 ----------
    def _sorted_entries(self, entries):
        """与差异审核一致：文件夹在前（按名称），文件按所选依据（名称/大小/类型/时间）排序。"""
        mode = self.cb_sort.currentData()
        dirs = [e for e in entries if e["d"]]
        files = [e for e in entries if not e["d"]]
        dirs.sort(key=lambda e: e["n"].lower())
        if mode == "size":
            files.sort(key=lambda e: e.get("s", 0))
        elif mode == "type":
            files.sort(key=lambda e: e.get("ftype", ""))
        elif mode == "time":
            files.sort(key=lambda e: e.get("t", 0))
        else:
            files.sort(key=lambda e: e["n"].lower())
        if self.btn_dir.isChecked():
            dirs.reverse()
            files.reverse()
        return dirs + files

    def _re_sort_loaded(self):
        """排序/方向变化时，对已加载的子树重新排序并保留展开状态。"""
        root = self.tree.topLevelItem(0)
        if root is None:
            return
        self._re_sort_node(root)

    def _re_sort_node(self, item):
        data = item.data(0, Qt.UserRole) or {}
        if not data.get("loaded"):
            return
        real = [item.child(i) for i in range(item.childCount())
                if item.child(i).data(0, Qt.UserRole) is not None]
        if not real:
            return
        expanded = {c.data(0, Qt.UserRole).get("rel")
                    for c in real if c.isExpanded()}
        entries = []
        for c in real:
            d = c.data(0, Qt.UserRole) or {}
            entries.append({"n": c.text(0), "d": d.get("is_dir", False),
                            "s": d.get("size", 0), "t": d.get("mtime", 0),
                            "ftype": d.get("ftype", "")})
        item.takeChildren()
        self._populate(item, entries)
        for i in range(item.childCount()):
            c = item.child(i)
            d = c.data(0, Qt.UserRole)
            if d and d.get("rel") in expanded:
                c.setExpanded(True)

    def _apply_filter(self):
        text = self.ed_search.text().strip()
        root = self.tree.topLevelItem(0)
        if root is None:
            return
        if not text:
            self._set_visible_all(root, True)
            return
        self._filter_node(root, text)

    def _filter_node(self, item, text) -> bool:
        if fnmatch.fnmatch(item.text(0).lower(), text.lower()):
            self._set_visible_all(item, True)
            return True
        found = False
        for i in range(item.childCount()):
            if self._filter_node(item.child(i), text):
                found = True
        item.setHidden(not found)
        return found

    @staticmethod
    def _set_visible_all(item, visible: bool):
        item.setHidden(not visible)
        for i in range(item.childCount()):
            RemoteTreeWidget._set_visible_all(item.child(i), visible)

    # ---------- 交互 ----------
    def _remote_path(self, item) -> str:
        """条目对应的完整远程路径（根条目即当前浏览根目录）。"""
        rel = (item.data(0, Qt.UserRole) or {}).get("rel", "")
        if rel:
            return SFTPManager.join(self._browse_root, rel)
        return self._browse_root

    def _on_double_clicked(self, item, column):
        if item is None:
            return
        data = item.data(0, Qt.UserRole)
        if data is None:  # 占位子项
            return
        if data.get("is_dir"):
            item.setExpanded(not item.isExpanded())
        else:
            self.file_activated.emit(data.get("rel", ""))

    def _on_menu(self, pos):
        """右键菜单：复制名称 / 复制相对路径（支持多选）；模组（.jar）提供 MC 百科搜索（PCL CE 移植）。"""
        item = self.tree.itemAt(pos)
        if item is None:
            return
        selected = self.tree.selectedItems()
        if item not in selected:
            selected = [item]
        data = item.data(0, Qt.UserRole) or {}
        single = len(selected) == 1
        menu = QMenu(self)
        act_name = menu.addAction(
            "复制名称" if single else f"复制名称（{len(selected)} 项）")
        act_path = menu.addAction(
            "复制相对路径" if single else f"复制相对路径（{len(selected)} 项）")
        # 模组文件：关联 MC 百科链接（详情页/搜索，功能移植自 PCL CE，署名 PCL CE）
        search_name = ""
        act_mc_view = act_mc_search = act_mc_copy = None
        view_url = search_url = ""
        if single and not data.get("is_dir") and item.text(0).lower().endswith(".jar"):
            search_name = mod_search_name(item.text(0))
            menu.addSeparator()
            act_mc_view, act_mc_search, act_mc_copy, view_url, search_url = \
                add_mcmod_menu_actions(menu, search_name)
        act = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if act is None:
            return
        if act is act_name:
            QApplication.clipboard().setText(
                item.text(0) if single else "\n".join(it.text(0) for it in selected))
        elif act is act_path:
            QApplication.clipboard().setText(
                data.get("rel", "") if single else
                "\n".join((it.data(0, Qt.UserRole) or {}).get("rel", "")
                          for it in selected))
        elif search_name:
            if act is act_mc_view:
                QDesktopServices.openUrl(QUrl(view_url))
                self.lbl_status.setText(f"已打开 MC 百科：{search_name}")
            elif act is act_mc_search:
                QDesktopServices.openUrl(QUrl(search_url))
                self.lbl_status.setText(f"已打开 MC 百科搜索：{search_name}")
            elif act is act_mc_copy:
                QApplication.clipboard().setText(view_url)
                self.lbl_status.setText("已复制 MC 百科链接")
