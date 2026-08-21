# -*- coding: utf-8 -*-
"""版本选择对话框：列出已知版本（versions 下的客户端根目录），供用户选择。"""
import fnmatch
import os
import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from . import winutil
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

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["版本名称", "完整路径"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionsMovable(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.itemDoubleClicked.connect(lambda _: self._accept_selected())
        layout.addWidget(self.table, 1)

        # 搜索 + 排序
        tool = QHBoxLayout()
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索版本（支持 * 通配）")
        self.ed_search.setClearButtonEnabled(True)
        self.cb_regex = QCheckBox("正则")
        self.cb_regex.setToolTip("启用后按正则表达式搜索")
        tool.addWidget(self.ed_search, 1)
        tool.addWidget(self.cb_regex)
        tool.addWidget(QLabel("排序："))
        self.cb_sort = QComboBox()
        self.cb_sort.addItem("按版本名", "name")
        self.cb_sort.addItem("按创建时间", "ctime")
        tool.addWidget(self.cb_sort)
        self.btn_sort_dir = QPushButton("↓ 正序")
        self.btn_sort_dir.setCheckable(True)
        self.btn_sort_dir.setToolTip("切换正序 / 倒序")
        tool.addWidget(self.btn_sort_dir)
        layout.addLayout(tool)

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

        self.ed_search.textChanged.connect(lambda _: self._refresh())
        self.cb_regex.toggled.connect(lambda _: self._refresh())
        self.cb_sort.currentIndexChanged.connect(lambda _: self._refresh())
        self.btn_sort_dir.toggled.connect(lambda _: self._refresh())

        self._refresh()

    def _view_entries(self) -> list[dict]:
        """按搜索与排序规则返回当前可见条目。"""
        entries = self.known.entries()
        text = self.ed_search.text().strip()
        if text:
            use_re = self.cb_regex.isChecked()

            def _match(e: dict) -> bool:
                name = f"{e.get('version', '')} {e.get('version_dir', '')}"
                if use_re:
                    try:
                        return re.search(text, name, re.IGNORECASE) is not None
                    except re.error:
                        return False
                return fnmatch.fnmatch(name.lower(), f"*{text.lower()}*")

            entries = [e for e in entries if _match(e)]
        if self.cb_sort.currentData() == "ctime":
            def _key(e: dict):
                try:
                    return os.path.getctime(e.get("version_dir", ""))
                except OSError:
                    return 0.0
            entries = sorted(entries, key=_key)
        else:
            entries = sorted(entries, key=lambda e: (e.get("version") or "").lower())
        if self.btn_sort_dir.isChecked():
            entries.reverse()
        return entries

    def _refresh(self):
        entries = self._view_entries()
        self.table.setRowCount(len(entries))
        for row, e in enumerate(entries):
            self.table.setItem(row, 0, QTableWidgetItem(e.get("version", "")))
            self.table.setItem(row, 1, QTableWidgetItem(e.get("version_dir", "")))
        if not entries:
            self.table.setRowCount(1)
            self.table.setItem(0, 0, QTableWidgetItem("（暂无匹配版本）"))
            self.table.setItem(0, 1, QTableWidgetItem("请先拖入 .minecraft 文件夹或启动器快捷方式"))
        elif self.table.currentRow() < 0:
            # 默认选中第一行，保证点「选择」即可生效
            self.table.selectRow(0)

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

    @staticmethod
    def _selected_rows(table) -> list[int]:
        """当前选中的行索引（QTableWidget 无 isRowSelected，须经 selectionModel 取）。"""
        return sorted({idx.row() for idx in table.selectionModel().selectedRows()})

    def _remove(self):
        rows = self._selected_rows(self.table)
        if not rows:
            return
        entries = self._view_entries()
        for row in rows:
            if row < len(entries):
                self.known.remove(entries[row].get("version_dir", ""))
        self._refresh()

    def _accept_selected(self):
        rows = self._selected_rows(self.table)
        if not rows:
            winutil.warn(self, "提示", "请先选择一个版本。")
            return
        entries = self._view_entries()
        row = rows[0]
        if row >= len(entries):
            winutil.warn(self, "提示", "当前没有可选择的版本。")
            return
        self._selected = entries[row]
        self.accept()

    def selected_entry(self) -> dict | None:
        return self._selected
