# -*- coding: utf-8 -*-
"""发布历史 / 快照管理：把「客户端内容回滚到某个时间点」做成三步向导。

为什么这样设计：
- 「发布基线」只是「客户端目前装的内容」的记录，本身不保存文件内容；
  旧版界面直接把某个历史快照设为基线，既不会删掉新文件、也不会还原旧文件，
  看起来「点了没反应」，因此改为**真正把服务端客户端内容恢复到该时间点**。

使用（三步，界面里也写了）：
1. 左边选一个时间点（那一版发布给客户端的客户端内容）；
2. 右边看「回滚后会怎样」：哪些文件被删除、哪些还原成旧版、哪些需要你自己处理；
3. 点「回滚到此时间点」→ 服务端客户端内容恢复到该时间点 →
   回到「发布待办」页会自动检测出「删除 / 还原」任务 → 点发布，客户端就回到那个状态。

两类记录：
- 发布历史（publish）：每次发布时保存的客户端内容快照 → 可回滚；
- 仓库快照（repo）：服务端文件仓库（mods 等）的状态记录 → 只读查看，不参与回滚。
"""
import json
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from . import winutil
from .file_hash import hash_file_cached, hash_remote_parallel
from .logger import get_logger
from .sftp import SFTPManager
from .snapshot import (
    list_snapshots,
    load_snapshot_at,
    normalize_files,
)
from .worker import Worker, fmt_progress

log = get_logger("snapshot_dialog")

SNAP_LABELS = {"publish": "发布历史", "repo": "仓库快照"}
ACTION_LABELS = {"delete": "删除", "restore": "还原为旧版", "manual": "需人工处理"}
ACTION_COLORS = {"delete": "#c0392b", "restore": "#2d7dd2", "manual": "#b8860b"}


def _fmt_size(n) -> str:
    n = int(n or 0)
    if n < 1024:
        return f"{n} B"
    kb = n / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    return f"{kb / 1024:.2f} MB"


class SnapshotHistoryDialog(QDialog):
    """发布历史 / 快照：查看历史时间点，并把客户端内容回滚到某个时间点。"""

    def __init__(self, parent=None, config=None):
        super().__init__(parent)
        self.config = config
        self._entries: list[dict] = []
        self._current: dict | None = None   # 当前客户端内容 {rel: {"size","hash"}}
        self._diff_rows: list[dict] = []
        self.rolled_back = False            # 是否执行过回滚（供发布页刷新）
        self._worker = None
        self.setWindowTitle("发布历史 / 快照（回滚客户端内容）")
        self.setMinimumSize(860, 560)
        self._build()
        self._reload()
        self._load_current()

    # ---------- 界面 ----------
    def _build(self):
        layout = QVBoxLayout(self)

        head = QHBoxLayout()
        head.addWidget(QLabel("查看："))
        self.cb_key = QComboBox()
        self.cb_key.addItem("发布历史（每次发布给客户端的客户端内容）", "publish")
        self.cb_key.addItem("仓库快照（服务端文件仓库，只读查看）", "repo")
        self.cb_key.currentIndexChanged.connect(self._on_key_changed)
        head.addWidget(self.cb_key)
        self.lbl_info = QLabel("")
        self.lbl_info.setObjectName("muted")
        head.addWidget(self.lbl_info, 1)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setToolTip("重新读取历史记录与当前客户端内容")
        self.btn_refresh.clicked.connect(self._reload_all)
        head.addWidget(self.btn_refresh)
        layout.addLayout(head)

        guide = QLabel(
            "用法（3 步）：① 左边选一个时间点 → ② 右边看「回滚后会怎样」"
            "（哪些文件会被删除、哪些还原成旧版）→ ③ 点「回滚到此时间点」，"
            "之后到「发布待办」页会检测出对应任务，点发布即可让客户端回到该时间点。")
        guide.setObjectName("muted")
        guide.setWordWrap(True)
        layout.addWidget(guide)

        body = QHBoxLayout()

        self.list_snapshots = QListWidget()
        self.list_snapshots.setMaximumWidth(280)
        self.list_snapshots.currentItemChanged.connect(self._on_select)
        body.addWidget(self.list_snapshots)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["文件 / 文件夹", "回滚动作", "说明"])
        self.tree.setAlternatingRowColors(True)
        header = self.tree.header()
        header.setSectionsMovable(True)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        body.addWidget(self.tree, 1)
        layout.addLayout(body, 1)

        row = QHBoxLayout()
        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("muted")
        self.lbl_status.setWordWrap(True)
        row.addWidget(self.lbl_status, 1)
        self.btn_rollback = QPushButton("回滚到此时间点")
        self.btn_rollback.setObjectName("primary")
        self.btn_rollback.setToolTip(
            "把服务端客户端内容（client_files）恢复到所选时间点：\n"
            "· 该时间点没有、现在有的文件 → 删除（客户端会收到删除任务）\n"
            "· 该时间点有、现在没有或内容不同的文件 → 用本地同内容的文件还原\n"
            "· 本地没有对应旧文件的 → 只能提示你手动处理（工具不保存旧文件内容）")
        self.btn_rollback.clicked.connect(self._rollback)
        self.btn_rollback.setEnabled(False)
        row.addWidget(self.btn_rollback)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        row.addWidget(btn_close)
        layout.addLayout(row)

    # ---------- 数据读取 ----------
    def _current_key(self) -> str:
        return self.cb_key.currentData() or "publish"

    def _reload_all(self):
        self._reload()
        self._load_current()

    def _on_key_changed(self):
        self._reload()

    def _reload(self):
        server_id = self.config.current_id() if self.config else ""
        key = self._current_key()
        self._entries = list_snapshots(server_id, key)
        self.list_snapshots.blockSignals(True)
        self.list_snapshots.clear()
        for i, entry in enumerate(self._entries):
            item = QListWidgetItem(f"{entry['saved_at']}  ·  {entry['count']} 个文件")
            item.setData(Qt.UserRole, entry["ts"])
            item.setToolTip(f"快照时间：{entry['saved_at']}\n文件数：{entry['count']}")
            self.list_snapshots.addItem(item)
        self.list_snapshots.blockSignals(False)
        self._diff_rows = []
        self.tree.clear()
        self.btn_rollback.setEnabled(False)
        label = SNAP_LABELS.get(key, key)
        if self._entries:
            self.list_snapshots.setCurrentRow(0)
            self.lbl_info.setText(f"{label} 共 {len(self._entries)} 条")
        else:
            self.lbl_info.setText(f"{label} 暂无记录")
            self.lbl_status.setText(
                "暂无历史记录：每次「发布到服务器」成功后会自动保存一条。" if key == "publish"
                else "暂无仓库快照记录。")

    def _load_current(self):
        """后台读取当前客户端内容（client_files：大小 + 哈希），用于回滚差异预览。"""
        if self.config is None or not self.config.host():
            self.lbl_status.setText("未配置 SFTP（请先在「服务器设置」页填写）。")
            return
        self.lbl_status.setText("正在读取当前客户端内容…")
        self.btn_rollback.setEnabled(False)
        worker = Worker(self._scan_worker)
        worker.progress.connect(self._on_scan_progress)
        worker.done.connect(self._on_scan_done)
        self._worker = worker
        worker.start()

    def _on_scan_progress(self, current, total, message):
        self.lbl_status.setText(fmt_progress(current, total, message, "读取当前客户端内容"))

    def _scan_worker(self, progress_cb=None) -> str:
        """读取 client_files 当前状态：{rel: {"size", "hash"}}（哈希走远程快速计算，不下载）。"""
        files_dir = self.config.files_dir

        def on_scan(dirs: int, entries: int):
            if progress_cb:
                progress_cb(dirs, 0,
                            f"正在读取 {files_dir}…（已读 {dirs} 个目录 / {entries} 个条目）")

        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            sizes = dict(sftp.list_files_recursive_with_size(
                files_dir, max_workers=2, progress_cb=on_scan))

            def on_hash(done: int, total: int, key: str):
                if progress_cb:
                    progress_cb(done, total, f"计算哈希 {key}")

            hashes = hash_remote_parallel(
                sftp, self.config.host(), self.config.port(),
                self.config.username(), self.config.password(),
                [(rel, SFTPManager.join(files_dir, rel)) for rel in sizes],
                on_hash)
        data = {rel: {"size": size, "hash": hashes.get(rel)} for rel, size in sizes.items()}
        log.info("读取当前客户端内容：%s 下 %d 个文件（已算哈希 %d 个）",
                 files_dir, len(data), len(hashes))
        return json.dumps(data, ensure_ascii=False)

    def _on_scan_done(self, ok: bool, msg: str):
        if not ok:
            self.lbl_status.setText("读取当前客户端内容失败 ✘")
            winutil.error(self, "读取失败", str(msg))
            return
        try:
            self._current = json.loads(msg or "{}")
        except Exception:
            self._current = {}
        self.lbl_status.setText(
            f"当前客户端内容共 {len(self._current or {})} 个文件；选中左侧时间点即可预览回滚结果。")
        self._show_selected()

    # ---------- 差异预览 ----------
    def _selected_entry(self) -> dict | None:
        item = self.list_snapshots.currentItem()
        if item is None:
            return None
        ts = item.data(Qt.UserRole)
        for entry in self._entries:
            if entry["ts"] == ts:
                return entry
        return None

    def _on_select(self, _current, _previous):
        self._show_selected()

    def _show_selected(self):
        entry = self._selected_entry()
        self.tree.clear()
        self._diff_rows = []
        self.btn_rollback.setEnabled(False)
        if entry is None:
            return
        key = self._current_key()
        snap = load_snapshot_at(self.config.current_id(), key, entry["ts"])
        files = normalize_files(snap.get("files", {}))
        if key != "publish":
            self._show_tree(files)
            self.lbl_status.setText(
                "仓库快照仅用于查看服务端文件仓库的历史状态（服务端 mods 等），不参与回滚。")
            return
        if self._current is None:
            self._show_tree(files)
            self.lbl_status.setText("正在读取当前客户端内容…稍后选中即可看到回滚预览。")
            return
        self._build_diff(files)

    def _show_tree(self, files: dict):
        """只读树状结构（目录层级）。"""
        nodes: dict[str, QTreeWidgetItem] = {}
        for rel in sorted(files):
            parts = rel.split("/")
            parent = None
            parent_rel = ""
            for part in parts[:-1]:
                child_rel = f"{parent_rel}/{part}" if parent_rel else part
                node = nodes.get(child_rel)
                if node is None:
                    node = QTreeWidgetItem([part, "", ""])
                    if parent is None:
                        self.tree.addTopLevelItem(node)
                    else:
                        parent.addChild(node)
                    nodes[child_rel] = node
                parent = node
                parent_rel = child_rel
            item = QTreeWidgetItem([parts[-1], "", _fmt_size(files[rel]["size"])])
            if parent is None:
                self.tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
        self.tree.expandAll()

    def _build_diff(self, base: dict):
        """对比「该时间点」与「当前客户端内容」，列出回滚会做什么。"""
        cur = self._current or {}
        local_root = self.config.local_mc_dir or ""
        rows: list[dict] = []
        for rel in sorted(set(base) | set(cur)):
            b, c = base.get(rel), cur.get(rel)
            if b is None:
                rows.append({"rel": rel, "action": "delete",
                             "note": f"当前有、该时间点没有 → 删除（{_fmt_size(c['size'])}）"})
                continue
            same = (c is not None and b["size"] == c["size"]
                    and (not b["hash"] or not c.get("hash") or b["hash"] == c["hash"]))
            if same:
                continue
            path = os.path.join(local_root, *rel.split("/")) if local_root else ""
            restorable = False
            if path and os.path.isfile(path):
                try:
                    restorable = bool(b["hash"]) and hash_file_cached(path) == b["hash"]
                except OSError:
                    restorable = False
            if restorable:
                rows.append({"rel": rel, "action": "restore",
                             "note": f"还原为旧版（{_fmt_size(b['size'])}）→ 用本地同内容文件上传"})
            else:
                rows.append({"rel": rel, "action": "manual",
                             "note": "本地没有这一版文件，无法自动还原（需你手动放回后再上传）"})
        self._diff_rows = rows

        for r in rows:
            item = QTreeWidgetItem([r["rel"], ACTION_LABELS[r["action"]], r["note"]])
            color = QColor(ACTION_COLORS[r["action"]])
            item.setForeground(1, QBrush(color))
            item.setToolTip(0, r["rel"])
            item.setToolTip(2, r["note"])
            self.tree.addTopLevelItem(item)

        doable = [r for r in rows if r["action"] in ("delete", "restore")]
        manual = [r for r in rows if r["action"] == "manual"]
        self.btn_rollback.setEnabled(bool(doable))
        if not rows:
            self.lbl_status.setText("该时间点与当前客户端内容一致，无需回滚 ✔")
        else:
            self.lbl_status.setText(
                f"回滚预览：可自动处理 {len(doable)} 个（删除/还原）"
                + (f"，另有 {len(manual)} 个本地没有旧文件、无法自动还原" if manual else "")
                + "。确认无误后点「回滚到此时间点」。")

    # ---------- 回滚 ----------
    def _rollback(self):
        if self.config is None or not self._diff_rows:
            return
        deletes = [r["rel"] for r in self._diff_rows if r["action"] == "delete"]
        restores = [r["rel"] for r in self._diff_rows if r["action"] == "restore"]
        manual = [r["rel"] for r in self._diff_rows if r["action"] == "manual"]
        text = (f"即将把服务端客户端内容恢复到所选时间点：\n\n"
                f"· 删除 {len(deletes)} 个（该时间点没有的文件）\n"
                f"· 还原 {len(restores)} 个（换成旧版本）")
        if manual:
            text += f"\n· 另有 {len(manual)} 个本地没有旧文件，将跳过（需要你手动处理）"
        text += "\n\n回滚后请到「发布待办」页检测并发布，客户端才会真正回到该状态。"
        items = [f"{ACTION_LABELS[r['action']]}　{r['rel']}" for r in self._diff_rows]
        if not winutil.confirm_list(self, "确认回滚客户端内容", text, items,
                                    ok_label="开始回滚"):
            return
        self.btn_rollback.setEnabled(False)
        worker = Worker(self._rollback_worker, deletes, restores)
        worker.progress.connect(self._on_scan_progress)
        worker.done.connect(self._on_rollback_done)
        self._worker = worker
        worker.start()

    def _rollback_worker(self, deletes, restores, progress_cb=None) -> str:
        """把 client_files 恢复到所选时间点：删除多余文件、用本地同内容文件还原旧版。"""
        files_dir = self.config.files_dir
        local_root = self.config.local_mc_dir or ""
        total = len(deletes) + len(restores)
        done = 0
        deleted: list[str] = []
        restored: list[str] = []
        failed: list[str] = []
        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            for rel in deletes:
                if progress_cb:
                    progress_cb(done, total, f"删除 {rel}")
                try:
                    sftp.delete(SFTPManager.join(files_dir, rel))
                    deleted.append(rel)
                except Exception as exc:
                    log.warning("回滚删除失败 %s: %s", rel, exc)
                    failed.append(f"{rel}：{exc}")
                done += 1
            for rel in restores:
                if progress_cb:
                    progress_cb(done, total, f"还原 {rel}")
                local = os.path.join(local_root, *rel.split("/"))
                try:
                    sftp.upload(local, SFTPManager.join(files_dir, rel))
                    restored.append(rel)
                except Exception as exc:
                    log.warning("回滚还原失败 %s: %s", rel, exc)
                    failed.append(f"{rel}：{exc}")
                done += 1
        log.info("回滚完成：删除 %d 个，还原 %d 个，失败 %d 个",
                 len(deleted), len(restored), len(failed))
        return json.dumps({"deleted": deleted, "restored": restored, "failed": failed},
                          ensure_ascii=False)

    def _on_rollback_done(self, ok: bool, msg: str):
        if not ok:
            self.btn_rollback.setEnabled(True)
            self.lbl_status.setText("回滚失败 ✘")
            winutil.error(self, "回滚失败", str(msg))
            return
        try:
            data = json.loads(msg or "{}")
        except Exception:
            data = {}
        deleted = data.get("deleted") or []
        restored = data.get("restored") or []
        failed = data.get("failed") or []
        self.rolled_back = True
        text = (f"已回滚：删除 {len(deleted)} 个、还原 {len(restored)} 个。\n\n"
                f"下一步：到「发布待办」页（关闭本窗口后会自动检测），"
                f"确认任务后点「发布到服务器」，客户端就会回到所选时间点。")
        if failed:
            text += f"\n\n失败 {len(failed)} 个：\n" + "\n".join(failed[:20])
            if len(failed) > 20:
                text += f"\n…另有 {len(failed) - 20} 个，详见日志。"
        self.lbl_status.setText(text.splitlines()[0])
        if failed:
            winutil.warn(self, "回滚完成（部分失败）", text)
        else:
            winutil.info(self, "回滚完成", text)
        self.accept()  # 关闭对话框，由发布页刷新并重新检测
