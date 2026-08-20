# -*- coding: utf-8 -*-
"""版本选择对话框：列出已知版本（versions 下的客户端根目录），供用户选择。"""
import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .launcher import KnownVersions
from .logger import get_logger

log = get_logger("version_dialog")


class VersionPickerDialog(QDialog):
    """选择 versions 下某个版本文件夹（该文件夹即客户端根目录）。"""

    def __init__(self, parent=None, known: KnownVersions | None = None,
                 title: str = "选择版本目录", hint: str = ""):
        super().__init__(parent)
        self.known = known or KnownVersions()
        self._selected: dict | None = None
        self.setWindowTitle(title)
        self.setMinimumSize(560, 360)
        self._build(hint)

    def _build(self, hint: str):
        layout = QVBoxLayout(self)
        if hint:
            tip = QLabel(hint)
            tip.setObjectName("muted")
            tip.setWordWrap(True)
            layout.addWidget(tip)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["版本名称", "所在根目录", "完整路径"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.itemDoubleClicked.connect(lambda _: self._accept_selected())
        layout.addWidget(self.table, 1)

        btn_row = QHBoxLayout()
        btn_rescan = QPushButton("重新扫描已识别目录")
        btn_rescan.clicked.connect(self._rescan)
        btn_remove = QPushButton("移除选中项")
        btn_remove.clicked.connect(self._remove)
        btn_row.addWidget(btn_rescan)
        btn_row.addWidget(btn_remove)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("选择")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._accept_selected)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh()

    def _refresh(self):
        entries = self.known.entries()
        self.table.setRowCount(len(entries))
        for row, e in enumerate(entries):
            root = Path(e.get("mc_root") or "").name or e.get("mc_root") or "—"
            self.table.setItem(row, 0, QTableWidgetItem(e.get("version", "")))
            self.table.setItem(row, 1, QTableWidgetItem(root))
            self.table.setItem(row, 2, QTableWidgetItem(e.get("version_dir", "")))
        if not entries:
            self.table.setRowCount(1)
            self.table.setItem(0, 0, QTableWidgetItem("（暂无已识别版本）"))
            self.table.setItem(0, 2, QTableWidgetItem("请先拖入 .minecraft 文件夹或启动器快捷方式"))

    def _rescan(self):
        scanned = 0
        for root_dir in {e.get("mc_root", "") for e in self.known.entries()}:
            if root_dir:
                scanned += self.known.add_from_mc_root(root_dir)
        for e in self.known.entries():
            if e.get("version_dir") and os.path.isdir(e["version_dir"]) \
                    and not e.get("version"):
                pass
        self._refresh()
        if scanned:
            log.info("重新扫描新增 %d 个版本", scanned)

    def _remove(self):
        row = self._current_row()
        if row < 0:
            return
        entry = self.known.entries()[row]
        self.known.remove(entry.get("version_dir", ""))
        self._refresh()

    def _current_row(self) -> int:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _accept_selected(self):
        row = self._current_row()
        entries = self.known.entries()
        if row < 0 or row >= len(entries):
            return
        self._selected = entries[row]
        self.accept()

    def selected_entry(self) -> dict | None:
        return self._selected
