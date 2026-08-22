# -*- coding: utf-8 -*-
"""服务端 - C2C（本地对本地）页：向已接入的客户端（按 UUID）发布保持文件结构的文件。

- 客户端名单：列出所有接受本服务器配置的客户端（授权编号），支持搜索（UUID/名称）、排序（名称/加入时间）。
- 本地文件树：选择本地发送目录后以树状结构展示（保持目录结构），可搜索。
- 发送：可向选中的单个客户端点对点发送，也可一键发送到全部客户端。
  文件上传到服务器上的 C2C 目录（默认 /c2c），并写入该客户端的 C2C 清单供客户端检查应用。
"""
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app_common import winutil
from app_common.c2c import local_files, send_worker
from app_common.logger import get_logger
from app_common.worker import Worker, fmt_progress

log = get_logger("server.c2c_page")


class _ClientItem(QWidget):
    """客户端名单列表项：名称 + UUID + 加入时间（两行小字）。"""

    def __init__(self, name: str, uid: str, created_at: str, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(0)
        self.lbl_name = QLabel(name or "未命名")
        self.lbl_name.setObjectName("list-name")
        self.lbl_meta = QLabel(f"{uid}  ·  加入：{created_at or '—'}")
        self.lbl_meta.setObjectName("list-status")
        lay.addWidget(self.lbl_name)
        lay.addWidget(self.lbl_meta)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)


class C2CPage(QWidget):
    def __init__(self, config, status_cb=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.status_cb = status_cb
        self._worker = None
        self._entries: list[dict] = []
        self._local_files: list[tuple[str, str]] = []
        self._build()
        self.reload()

    # ---------- 界面 ----------
    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # 发送源
        src_box = QGroupBox("本地发送目录（保持文件结构发布给客户端）")
        src_row = QHBoxLayout(src_box)
        self.ed_local = QLineEdit()
        self.ed_local.setReadOnly(True)
        self.ed_local.setPlaceholderText("选择一个本地文件夹，其内容将原样发布到客户端")
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._browse_local)
        btn_refresh = QPushButton("刷新文件树")
        btn_refresh.clicked.connect(self._refresh_tree)
        src_row.addWidget(self.ed_local, 1)
        src_row.addWidget(btn_browse)
        src_row.addWidget(btn_refresh)
        layout.addWidget(src_box)

        splitter = QSplitter(Qt.Horizontal)

        # ---- 左：客户端名单 ----
        list_box = QWidget()
        ll = QVBoxLayout(list_box)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)
        ll.addWidget(QLabel("客户端名单（所有接受本服务器配置的客户端）"))

        search_row = QHBoxLayout()
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索名称 / UUID（支持 * 通配）")
        self.ed_search.setClearButtonEnabled(True)
        self.ed_search.textChanged.connect(lambda _: self._apply_filter())
        search_row.addWidget(self.ed_search, 1)
        self.cb_sort = QComboBox()
        self.cb_sort.addItem("按名称", "name")
        self.cb_sort.addItem("按加入时间", "created_at")
        self.cb_sort.addItem("按 UUID", "id")
        self.cb_sort.currentIndexChanged.connect(lambda _: self._re_sort())
        search_row.addWidget(self.cb_sort)
        self.btn_sort_dir = QPushButton("↓ 正序")
        self.btn_sort_dir.setCheckable(True)
        self.btn_sort_dir.setToolTip("切换正序 / 倒序")
        self.btn_sort_dir.toggled.connect(lambda _: self._re_sort())
        search_row.addWidget(self.btn_sort_dir)
        ll.addLayout(search_row)

        self.list_clients = QListWidget()
        self.list_clients.setSelectionMode(QListWidget.SingleSelection)
        ll.addWidget(self.list_clients, 1)
        splitter.addWidget(list_box)

        # ---- 右：本地文件树 ----
        tree_box = QWidget()
        tl = QVBoxLayout(tree_box)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(6)
        tsearch = QHBoxLayout()
        tl.addWidget(QLabel("本地文件（保持目录结构）"))
        self.ed_tree_search = QLineEdit()
        self.ed_tree_search.setPlaceholderText("搜索文件 / 文件夹（支持 * 通配）")
        self.ed_tree_search.setClearButtonEnabled(True)
        self.ed_tree_search.textChanged.connect(lambda _: self._apply_tree_filter())
        tsearch.addWidget(self.ed_tree_search, 1)
        self.lbl_tree_count = QLabel("")
        self.lbl_tree_count.setObjectName("muted")
        tsearch.addWidget(self.lbl_tree_count)
        tl.addLayout(tsearch)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["文件 / 文件夹", "大小"])
        self.tree.setAlternatingRowColors(True)
        header = self.tree.header()
        header.setSectionsMovable(True)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        tl.addWidget(self.tree, 1)
        splitter.addWidget(tree_box)

        splitter.setSizes([300, 400])
        layout.addWidget(splitter, 1)

        # 发送区
        send_row = QHBoxLayout()
        send_row.addWidget(QLabel("版本号"))
        self.ed_version = QLineEdit()
        self.ed_version.setPlaceholderText("自定义版本号，如 c2c-1.0")
        self.ed_version.setMaximumWidth(200)
        send_row.addWidget(self.ed_version)
        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("muted")
        self.lbl_status.setWordWrap(True)
        send_row.addWidget(self.lbl_status, 1)
        self.btn_send_one = QPushButton("发送到选中客户端")
        self.btn_send_one.setObjectName("primary")
        self.btn_send_one.clicked.connect(self._send_selected)
        self.btn_send_all = QPushButton("发送到全部客户端")
        self.btn_send_all.clicked.connect(self._send_all)
        send_row.addWidget(self.btn_send_one)
        send_row.addWidget(self.btn_send_all)
        layout.addLayout(send_row)

        tip = QLabel("说明：C2C 用于把任意保持结构的文件从本机（服务端工具）发布给指定客户端。"
                     "发送时自动对比上次发布内容，新增 / 更新 / 改名（如 foo.jar → foo.jar.disabled 用于禁用）/ "
                     "删除的文件都会同步到客户端（保持文件结构）。"
                     "文件上传到服务器 C2C 目录（默认 /c2c），客户端检查更新时会读取自己 UUID 对应的清单并应用。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        layout.addWidget(tip)

    # ---------- 数据 ----------
    def reload(self):
        """切换服务器后刷新名单。"""
        self._entries = list(self.config.export_ids())
        self.ed_local.setText(self.config.c2c_local_dir)
        self._refresh_list()
        self._refresh_tree()

    def _refresh_list(self):
        self._re_sort()

    def _apply_filter(self):
        self._re_sort()

    def _re_sort(self):
        """按搜索关键词过滤并按所选键排序后重建列表。

        注意：QListWidget.takeItem 会移除关联的 itemWidget，因此不能原地交换，
        这里直接从数据源重建并重新绑定 widget。
        """
        import fnmatch

        key = self.cb_sort.currentData() or "name"
        reverse = self.btn_sort_dir.isChecked()
        text = self.ed_search.text().strip().lower()

        def sort_key(entry):
            if key == "name":
                return (entry.get("name", "") or "").lower()
            if key == "created_at":
                return (entry.get("created_at", "") or "").lower()
            return entry.get("id", "") or ""

        rows = []
        for entry in self._entries:
            uid = entry.get("id", "") or ""
            name = (entry.get("name", "") or "").lower()
            meta = f"{uid}  ·  加入：{entry.get('created_at') or '—'}".lower()
            if text and not fnmatch.fnmatch(f"{name} {meta}", f"*{text}*"):
                continue
            rows.append(entry)
        rows.sort(key=sort_key, reverse=reverse)

        self.list_clients.blockSignals(True)
        self.list_clients.clear()
        for entry in rows:
            uid = entry.get("id", "") or ""
            item = QListWidgetItem(self.list_clients)
            item.setData(Qt.UserRole, uid)
            widget = _ClientItem(entry.get("name", ""), uid,
                                 entry.get("created_at", ""))
            item.setSizeHint(widget.sizeHint())
            self.list_clients.setItemWidget(item, widget)
        self.list_clients.blockSignals(False)

    # ---------- 本地文件树 ----------
    def _browse_local(self):
        path = QFileDialog.getExistingDirectory(self, "选择本地发送目录",
                                                self.ed_local.text())
        if path:
            self.ed_local.setText(path)
            self.config.c2c_local_dir = path
            self._refresh_tree()

    def _refresh_tree(self):
        self.tree.clear()
        self._local_files = []
        local_dir = self.ed_local.text().strip()
        if not local_dir or not os.path.isdir(local_dir):
            self.lbl_tree_count.setText("未选择目录")
            return
        try:
            self._local_files = local_files(local_dir)
        except OSError as exc:
            self.lbl_tree_count.setText(f"读取失败：{exc}")
            return
        nodes: dict[str, QTreeWidgetItem] = {}
        for rel, abs_path in self._local_files:
            parts = rel.split("/")
            parent = None
            parent_rel = ""
            for part in parts[:-1]:
                child_rel = f"{parent_rel}/{part}" if parent_rel else part
                node = nodes.get(child_rel)
                if node is None:
                    node = QTreeWidgetItem([part, ""])
                    node.setData(0, Qt.UserRole, {"kind": "dir"})
                    if parent is None:
                        self.tree.addTopLevelItem(node)
                    else:
                        parent.addChild(node)
                    nodes[child_rel] = node
                parent = node
                parent_rel = child_rel
            item = QTreeWidgetItem([parts[-1], _fmt_size(
                os.path.getsize(abs_path) if os.path.isfile(abs_path) else 0)])
            if parent is None:
                self.tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
        self.tree.expandAll()
        self.lbl_tree_count.setText(f"共 {len(self._local_files)} 个文件")
        self._apply_tree_filter()

    def _apply_tree_filter(self):
        import fnmatch
        text = self.ed_tree_search.text().strip()

        def walk(item) -> bool:
            if not text or fnmatch.fnmatch(item.text(0).lower(), text.lower()):
                item.setHidden(False)
                return True
            found = False
            for i in range(item.childCount()):
                if walk(item.child(i)):
                    found = True
            item.setHidden(not found)
            return found

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))

    # ---------- 发送 ----------
    def _selected_uuid(self) -> str | None:
        item = self.list_clients.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _version(self) -> str:
        return self.ed_version.text().strip()

    def _send_selected(self):
        uid = self._selected_uuid()
        if not uid:
            winutil.warn(self, "提示", "请先在客户端名单中选择一个客户端。")
            return
        if not self.config.host():
            winutil.warn(self, "提示", "请先在「服务器设置」页填写服务器信息。")
            return
        name = ""
        for entry in self._entries:
            if entry.get("id") == uid:
                name = entry.get("name", "")
                break
        if not winutil.confirm(
                self, "确认发送",
                f"将向客户端「{name or uid}」发布本地目录的全部改动：\n"
                f"目录：{self.ed_local.text()}\n"
                f"版本：{self._version() or '（自动）'}\n\n"
                "发送时自动对比上次发布内容：新增 / 更新 / 改名（禁用）/ 删除的文件"
                "都会同步到客户端（保持文件结构）。是否继续？"):
            return
        self._run_send(self._send_one_worker, uid)

    def _send_all(self):
        if not self.config.host():
            winutil.warn(self, "提示", "请先在「服务器设置」页填写服务器信息。")
            return
        count = len(self.config.export_ids())
        if count == 0:
            winutil.warn(self, "提示",
                         "客户端名单为空。请先在「导出客户端配置」页导出并授权客户端。")
            return
        if not winutil.confirm(
                self, "确认发送",
                f"将把本地目录的全部改动同时发布给全部 {count} 个已授权客户端。\n"
                f"目录：{self.ed_local.text()}\n"
                f"版本：{self._version() or '（自动）'}\n\n"
                "发送时自动对比上次发布内容：新增 / 更新 / 改名（禁用）/ 删除的文件"
                "都会同步到客户端（保持文件结构）。是否继续？"):
            return
        self._run_send(self._send_all_worker)

    def _run_send(self, worker_fn, *args):
        self.btn_send_one.setEnabled(False)
        self.btn_send_all.setEnabled(False)
        self.lbl_status.setText("正在发送…")
        worker = Worker(worker_fn, *args)
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_send_done)
        self._worker = worker
        worker.start()

    def _send_one_worker(self, uid, progress_cb=None):
        return send_worker(self.config, uid, self._version(), progress_cb)

    def _send_all_worker(self, progress_cb=None):
        uuids = [e.get("id", "") for e in self.config.export_ids()]
        uuids = [u for u in uuids if u]
        if not uuids:
            raise RuntimeError("客户端名单为空。")
        msgs = []
        total = len(uuids)
        for i, uid in enumerate(uuids):
            if progress_cb:
                progress_cb(i, total, f"正在发送到 {uid}")
            msgs.append(send_worker(self.config, uid, self._version()))
            if progress_cb:
                progress_cb(i + 1, total, f"已完成 {uid}")
        return f"已向全部 {total} 个客户端发布完成：\n" + "\n".join(msgs)

    def _on_progress(self, current, total, message):
        self.lbl_status.setText(fmt_progress(current, total, message, "C2C 发送"))

    def _on_send_done(self, ok: bool, msg: str):
        self.btn_send_one.setEnabled(True)
        self.btn_send_all.setEnabled(True)
        if ok:
            self.lbl_status.setText("发送完成 ✔")
            winutil.info(self, "发送成功", msg)
            if self.status_cb:
                self.status_cb(True)
        else:
            self.lbl_status.setText("发送失败 ✘")
            winutil.error(self, "发送失败", f"C2C 发送过程中发生错误：\n{msg}")
            if self.status_cb:
                self.status_cb(False)


def _fmt_size(n) -> str:
    n = int(n or 0)
    if n < 1024:
        return f"{n} B"
    kb = n / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    return f"{kb / 1024:.2f} MB"
