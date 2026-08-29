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
    QProgressDialog,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app_common import winutil
from app_common.c2c import abs_c2c_dir, local_files, send_worker
from app_common.logger import get_logger
from app_common.sftp import SFTPManager
from app_common.upload_files import (
    cleanup_expired,
    collect_upload_items,
    delete_files,
    list_remote_files,
    upload_files,
)
from app_common.worker import Worker, fmt_progress

log = get_logger("server.c2c_page")

# 共享文件保存时限选项：(显示文本, 小时数；0=永久)
TTL_OPTIONS = (
    ("永久保存", 0),
    ("1 小时", 1),
    ("6 小时", 6),
    ("1 天", 24),
    ("3 天", 72),
    ("7 天", 168),
    ("30 天", 720),
)


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
        self._shared_files: list[dict] = []
        self._upload_progress = None
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

        # ---- 共享文件区（.upload_files 临时下载区） ----
        shared_box = QGroupBox("共享文件（.upload_files 临时下载区：上传共享 / 设置密钥 / 设置保存时限）")
        sl = QVBoxLayout(shared_box)
        sl.setSpacing(6)
        op_row = QHBoxLayout()
        btn_up_file = QPushButton("上传文件…")
        btn_up_file.clicked.connect(lambda: self._browse_shared_upload(folder=False))
        btn_up_dir = QPushButton("上传文件夹…")
        btn_up_dir.clicked.connect(lambda: self._browse_shared_upload(folder=True))
        op_row.addWidget(btn_up_file)
        op_row.addWidget(btn_up_dir)
        op_row.addSpacing(8)
        op_row.addWidget(QLabel("下载密钥"))
        self.ed_shared_key = QLineEdit()
        self.ed_shared_key.setPlaceholderText("留空=不加密；填写后客户端需输入相同密钥才能下载")
        self.ed_shared_key.setMaximumWidth(180)
        op_row.addWidget(self.ed_shared_key)
        op_row.addWidget(QLabel("保存时限"))
        self.cb_ttl = QComboBox()
        for text, hours in TTL_OPTIONS:
            self.cb_ttl.addItem(text, hours)
        self.cb_ttl.setMaximumWidth(110)
        op_row.addWidget(self.cb_ttl)
        op_row.addStretch(1)
        btn_cleanup = QPushButton("清理过期")
        btn_cleanup.setToolTip("删除已到保存时限的共享文件（也可在「服务器设置」页配置自动清理）")
        btn_cleanup.clicked.connect(self._cleanup_shared_expired)
        op_row.addWidget(btn_cleanup)
        btn_refresh = QPushButton("刷新")
        btn_refresh.clicked.connect(self._refresh_shared)
        op_row.addWidget(btn_refresh)
        sl.addLayout(op_row)

        self.tree_shared = QTreeWidget()
        self.tree_shared.setColumnCount(4)
        self.tree_shared.setHeaderLabels(["文件（保留相对结构）", "大小", "密钥", "过期时间"])
        self.tree_shared.setAlternatingRowColors(True)
        self.tree_shared.setSelectionMode(QTreeWidget.ExtendedSelection)
        sh = self.tree_shared.header()
        sh.setSectionResizeMode(0, QHeaderView.Stretch)
        sh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        sh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        sh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        sl.addWidget(self.tree_shared, 1)

        del_row = QHBoxLayout()
        self.lbl_shared_status = QLabel("")
        self.lbl_shared_status.setObjectName("muted")
        del_row.addWidget(self.lbl_shared_status, 1)
        btn_del = QPushButton("删除选中")
        btn_del.clicked.connect(self._delete_shared_selected)
        del_row.addWidget(btn_del)
        sl.addLayout(del_row)
        layout.addWidget(shared_box)

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
        if self.config.host():
            self._refresh_shared()

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

    # ---------- 共享文件区（.upload_files 临时下载区） ----------
    def _require_sftp_config(self) -> bool:
        if not self.config.host():
            winutil.warn(self, "提示", "请先在「服务器设置」页填写服务器信息。")
            return False
        return True

    def _shared_dir(self) -> str:
        return abs_c2c_dir(self.config.c2c_dir)

    def _browse_shared_upload(self, folder: bool):
        if not self._require_sftp_config():
            return
        if folder:
            path = QFileDialog.getExistingDirectory(self, "选择要上传的文件夹（保留相对结构）")
            paths = [path] if path else []
        else:
            paths, _ = QFileDialog.getOpenFileNames(
                self, "选择要上传的文件（可多选）")
        if not paths:
            return
        items = collect_upload_items(paths)
        if not items:
            winutil.warn(self, "提示", "所选内容为空或不可读。")
            return
        total_bytes = sum(os.path.getsize(a) for _, a in items)
        key = self.ed_shared_key.text().strip()
        ttl_hours = int(self.cb_ttl.currentData() or 0)
        key_tip = f"（加密：下载需输入密钥）" if key else ""
        ttl_tip = "永久保存" if ttl_hours <= 0 else f"{ttl_hours} 小时后过期"
        if not winutil.confirm(
                self, "确认上传共享文件",
                f"将上传 {len(items)} 个文件（共 {_fmt_size(total_bytes)}）到服务器的共享下载区：\n"
                f"路径：{self._shared_dir()}/.upload_files\n"
                f"保存时限：{ttl_tip}\n"
                f"密钥：{key_tip or '无（明文）'}\n\n"
                "上传内容保留相对文件结构，客户端可在「共享文件」中下载。"):
            return
        self._run_shared_upload(items, key, ttl_hours)

    def _run_shared_upload(self, items, key, ttl_hours):
        total_bytes = sum(os.path.getsize(a) for _, a in items)
        progress = QProgressDialog("正在上传共享文件…", "取消", 0,
                                   max(total_bytes, 1), self)
        progress.setWindowTitle("上传共享文件")
        progress.setWindowModality(Qt.NonModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        self._upload_progress = progress
        worker = Worker(self._shared_upload_worker, items, key, ttl_hours)
        worker.progress.connect(self._on_shared_upload_progress)
        worker.done.connect(lambda ok, msg: self._on_shared_upload_done(
            ok, msg, progress))
        self._worker = worker
        worker.start()

    def _shared_upload_worker(self, items, key, ttl_hours, progress_cb=None):
        state = {"total": sum(os.path.getsize(a) for _, a in items)}
        def byte_cb(transferred, total):
            if progress_cb:
                progress_cb(transferred, max(state["total"], 1), "")
        def file_cb(i, n, msg):
            if progress_cb:
                progress_cb(state["total"], max(state["total"], 1), msg)
        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            cleanup_expired(sftp, self._shared_dir())  # 上传前先清理过期
            result = upload_files(sftp, self._shared_dir(), items,
                                  key=key, ttl_hours=ttl_hours,
                                  progress_cb=file_cb, byte_progress_cb=byte_cb)
        expires = result["expires_at"]
        tip = f"，{_fmt_expires(expires)}" if expires else ""
        return f"已上传 {result['uploaded']} 个共享文件{tip}。"

    def _on_shared_upload_progress(self, cur, total, msg):
        progress = self._upload_progress
        if progress is None:
            return
        progress.setRange(0, max(total, 1))
        progress.setValue(cur)
        if msg:
            progress.setLabelText(f"{msg}（{cur / max(total, 1):.0%}）")

    def _on_shared_upload_done(self, ok: bool, msg: str, progress):
        progress.close()
        self._upload_progress = None
        if ok:
            winutil.info(self, "上传成功", msg)
            self._refresh_shared()
        else:
            winutil.error(self, "上传失败", f"共享文件上传失败：\n{msg}")

    def _refresh_shared(self):
        if not self._require_sftp_config():
            return
        self.lbl_shared_status.setText("正在读取共享文件列表…")
        worker = Worker(self._shared_refresh_worker)
        worker.done.connect(self._on_shared_refresh_done)
        self._worker = worker
        worker.start()

    def _shared_refresh_worker(self, progress_cb=None):
        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            cleanup_expired(sftp, self._shared_dir())  # 读取前顺带清理过期
            return list_remote_files(sftp, self._shared_dir())

    def _on_shared_refresh_done(self, ok: bool, result):
        if not ok:
            self.lbl_shared_status.setText("读取失败 ✘")
            winutil.error(self, "读取失败", f"无法读取共享文件列表：\n{result}")
            return
        self._shared_files = result or []
        self._rebuild_shared_tree()
        expired = sum(1 for f in self._shared_files if f["expired"])
        self.lbl_shared_status.setText(
            f"共 {len(self._shared_files)} 个共享文件，其中 {expired} 个已过期（可在服务器设置中开启自动清理）")

    def _rebuild_shared_tree(self):
        self.tree_shared.clear()
        for f in self._shared_files:
            name = f["rel"]
            if f["expired"]:
                name = f"{name}（已过期）"
            item = QTreeWidgetItem([
                name, _fmt_size(f["size"]),
                "是" if f["encrypted"] else "—",
                _fmt_expires(f["expires_at"]) or "永久",
            ])
            item.setData(0, Qt.UserRole, f["rel"])
            self.tree_shared.addTopLevelItem(item)

    def _selected_shared_rels(self) -> list[str]:
        rels = []
        for item in self.tree_shared.selectedItems():
            rel = item.data(0, Qt.UserRole)
            if rel:
                rels.append(rel)
        return rels

    def _delete_shared_selected(self):
        rels = self._selected_shared_rels()
        if not rels:
            winutil.warn(self, "提示", "请先在共享文件列表中选择要删除的文件。")
            return
        if not winutil.confirm(
                self, "确认删除",
                f"将从服务器删除 {len(rels)} 个共享文件：\n"
                + "\n".join(f"  · {r}" for r in rels[:10])
                + ("\n  …" if len(rels) > 10 else "")):
            return
        self._run_shared_misc(self._shared_delete_worker, rels)

    def _shared_delete_worker(self, rels, progress_cb=None):
        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            n = delete_files(sftp, self._shared_dir(), rels)
        return f"已删除 {n} 个共享文件。"

    def _cleanup_shared_expired(self):
        if not winutil.confirm(
                self, "确认清理",
                "将删除所有已到保存时限的过期共享文件，是否继续？"):
            return
        self._run_shared_misc(self._shared_cleanup_worker)

    def _shared_cleanup_worker(self, progress_cb=None):
        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            n = cleanup_expired(sftp, self._shared_dir())
        return f"已清理 {n} 个过期共享文件。" if n else "没有过期共享文件。"

    def _run_shared_misc(self, worker_fn, *args):
        worker = Worker(worker_fn, *args)
        worker.done.connect(self._on_shared_misc_done)
        self._worker = worker
        worker.start()

    def _on_shared_misc_done(self, ok: bool, msg: str):
        if ok:
            self.lbl_shared_status.setText("操作完成 ✔")
            winutil.info(self, "完成", msg)
        else:
            self.lbl_shared_status.setText("操作失败 ✘")
            winutil.error(self, "操作失败", msg)
        self._refresh_shared()


def _fmt_expires(expires_at: str) -> str:
    """ISO 时间 → 显示文本；空 → 永久。"""
    if not expires_at:
        return "永久"
    from datetime import datetime
    try:
        return datetime.fromisoformat(expires_at).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return expires_at


def _fmt_size(n) -> str:
    n = int(n or 0)
    if n < 1024:
        return f"{n} B"
    kb = n / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    return f"{kb / 1024:.2f} MB"
