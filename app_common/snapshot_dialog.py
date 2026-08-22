# -*- coding: utf-8 -*-
"""快照历史管理：查看每个历史快照的时间戳与文件树状结构，并支持「撤销到此状态」。

- 左侧：历史快照列表（时间戳 + 文件数），可切换「发布基线 / 仓库快照」两类。
- 右侧：选中快照的树状结构（目录 + 文件大小）。
- 「设为发布基线（撤销到此状态）」：把选中快照的内容设为发布基线，
  之后到「发布待办」页重新检测并发布，客户端应用后即回滚/恢复到该快照状态。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from . import winutil
from .snapshot import list_snapshots, load_snapshot_at, save_snapshot

SNAP_LABELS = {"publish": "发布基线", "repo": "仓库快照"}


def _fmt_size(n) -> str:
    n = int(n or 0)
    if n < 1024:
        return f"{n} B"
    kb = n / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    return f"{kb / 1024:.2f} MB"


class SnapshotHistoryDialog(QDialog):
    """快照历史查看 / 撤销对话框。"""

    def __init__(self, parent=None, config=None):
        super().__init__(parent)
        self.config = config
        self._entries: list[dict] = []
        self.setWindowTitle("快照历史")
        self.setMinimumSize(720, 480)
        self._build()
        self._reload()

    # ---------- 界面 ----------
    def _build(self):
        layout = QVBoxLayout(self)

        head = QHBoxLayout()
        head.addWidget(QLabel("快照类型："))
        self.cb_key = QComboBox()
        self.cb_key.addItem("发布基线（已下发给客户端的版本）", "publish")
        self.cb_key.addItem("仓库快照（服务端文件仓库状态）", "repo")
        self.cb_key.currentIndexChanged.connect(self._reload)
        head.addWidget(self.cb_key)
        self.lbl_info = QLabel("")
        self.lbl_info.setObjectName("muted")
        head.addWidget(self.lbl_info, 1)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self._reload)
        head.addWidget(self.btn_refresh)
        layout.addLayout(head)

        splitter = QSplitter(Qt.Horizontal)

        # 左侧：快照列表（时间戳）
        self.list_snapshots = QListWidget()
        self.list_snapshots.currentItemChanged.connect(self._on_select)
        splitter.addWidget(self.list_snapshots)

        # 右侧：树状结构
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["文件 / 文件夹", "大小"])
        self.tree.setAlternatingRowColors(True)
        header = self.tree.header()
        header.setSectionsMovable(True)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        splitter.addWidget(self.tree)
        splitter.setSizes([240, 480])
        layout.addWidget(splitter, 1)

        tip = QLabel("提示：选中任意历史快照可查看其树状结构。点击「设为发布基线」后，"
                     "到「发布待办」页重新检测并发布，客户端应用后即回滚/恢复到该快照状态。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        row = QHBoxLayout()
        self.btn_rollback = QPushButton("设为发布基线（撤销到此状态）")
        self.btn_rollback.setObjectName("primary")
        self.btn_rollback.clicked.connect(self._set_as_baseline)
        row.addWidget(self.btn_rollback)
        row.addStretch(1)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        row.addWidget(btn_close)
        layout.addLayout(row)

    # ---------- 数据 ----------
    def _current_key(self) -> str:
        return self.cb_key.currentData() or "publish"

    def _reload(self):
        server_id = self.config.current_id() if self.config else ""
        key = self._current_key()
        self._entries = list_snapshots(server_id, key)
        self.list_snapshots.blockSignals(True)
        self.list_snapshots.clear()
        for entry in self._entries:
            item = QListWidgetItem(
                f"{entry['saved_at']}  ·  {entry['count']} 个文件")
            item.setData(Qt.UserRole, entry["ts"])
            item.setToolTip(f"快照时间：{entry['saved_at']}")
            self.list_snapshots.addItem(item)
        self.list_snapshots.blockSignals(False)
        if self._entries:
            self.list_snapshots.setCurrentRow(0)
            self._show_tree(self._entries[0])
        else:
            self._show_tree(None)
        self.lbl_info.setText(
            f"{SNAP_LABELS.get(key, key)} 共 {len(self._entries)} 条历史快照")

    def _selected_ts(self) -> str | None:
        item = self.list_snapshots.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _on_select(self, current, _previous):
        if current is None:
            self._show_tree(None)
            return
        for entry in self._entries:
            if entry["ts"] == current.data(Qt.UserRole):
                self._show_tree(entry)
                break

    def _show_tree(self, entry: dict | None):
        self.tree.clear()
        if entry is None:
            return
        files = load_snapshot_at(self.config.current_id(),
                                 self._current_key(), entry["ts"]).get("files", {})
        nodes: dict[str, QTreeWidgetItem] = {}
        for rel in sorted(files):
            size = files[rel]
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
            item = QTreeWidgetItem([parts[-1], _fmt_size(size)])
            if parent is None:
                self.tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
        self.tree.expandAll()

    # ---------- 撤销 ----------
    def _set_as_baseline(self):
        if self.config is None:
            return
        ts = self._selected_ts()
        if ts is None:
            winutil.warn(self, "提示", "请先选择一个历史快照。")
            return
        server_id = self.config.current_id()
        snap = load_snapshot_at(server_id, self._current_key(), ts)
        files = snap.get("files", {})
        saved_at = save_snapshot(server_id, files, "publish")
        if not saved_at:
            winutil.error(self, "操作失败", "无法写入发布基线。")
            return
        winutil.info(
            self, "已设为发布基线",
            f"已将 {snap.get('saved_at', '')} 的快照设为发布基线"
            f"（{len(files)} 个文件）。\n\n"
            f"接下来请到「发布待办」页重新检测并发布：\n"
            f"· 服务端文件仓库与该快照不一致的改动会重新列出；\n"
            f"· 发布后客户端应用新版本即回滚/恢复到该快照状态。")
