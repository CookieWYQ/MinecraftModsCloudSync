# -*- coding: utf-8 -*-
"""配置管理器：隐藏保存的配置文件，通过本窗口安全备份/恢复，避免误删误改。"""
import json
from datetime import datetime

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from . import winutil
from .config import JsonStore
from .logger import get_logger

log = get_logger("config_manager")


class ConfigManagerDialog(QDialog):
    """对隐藏的配置文件进行备份与恢复（GUI 内安全编辑，用户无需直接接触文件）。"""

    def __init__(self, parent=None, store: JsonStore | None = None,
                 title: str = "配置管理", on_restored=None, note: str = ""):
        super().__init__(parent)
        self.store = store
        self.on_restored = on_restored
        self.setWindowTitle(title)
        self.setMinimumWidth(520)
        self._build(note)

    def _build(self, note: str):
        layout = QVBoxLayout(self)

        desc = QLabel(
            "程序的所有配置与日志均保存在隐藏目录中，用户无需也不应直接修改文件，以防损坏。\n"
            "本窗口提供安全备份与恢复：导出会生成一份完整备份，恢复可从备份还原，"
            "全程由程序校验，不会产生损坏或误删。"
        )
        desc.setObjectName("muted")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        if note:
            tip = QLabel(note)
            tip.setObjectName("muted")
            tip.setWordWrap(True)
            layout.addWidget(tip)

        if self.store is not None:
            path_lbl = QLabel(f"配置文件位置（隐藏）：\n{self.store.path}")
            path_lbl.setObjectName("muted")
            path_lbl.setWordWrap(True)
            layout.addWidget(path_lbl)

        layout.addSpacing(6)
        btn_row = QHBoxLayout()
        btn_backup = QPushButton("导出配置备份…")
        btn_backup.clicked.connect(self._backup)
        btn_restore = QPushButton("从备份恢复…")
        btn_restore.clicked.connect(self._restore)
        btn_row.addWidget(btn_backup)
        btn_row.addWidget(btn_restore)
        btn_row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        btn_row.addWidget(buttons)
        layout.addLayout(btn_row)

    def _backup(self):
        if self.store is None:
            return
        default_name = f"配置备份_{datetime.now():%Y%m%d_%H%M}.json"
        path, _ = QFileDialog.getSaveFileName(self, "导出配置备份", default_name, "JSON 文件 (*.json)")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.store.all(), f, ensure_ascii=False, indent=2)
        except OSError as exc:
            winutil.error(self, "导出失败", f"写入文件失败：\n{exc}")
            return
        winutil.info(self, "备份完成", f"配置备份已导出到：\n{path}")

    def _restore(self):
        if self.store is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择配置备份", "", "JSON 文件 (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.store.replace_all(data)
        except Exception as exc:
            winutil.error(self, "恢复失败", f"无法恢复该备份：\n{exc}")
            return
        log.info("配置已从备份恢复: %s", path)
        if self.on_restored is not None:
            try:
                self.on_restored()
            except Exception as exc:
                log.warning("恢复后刷新界面失败: %s", exc)
        winutil.info(self, "恢复完成", "配置已从备份恢复。\n部分设置将在程序重启后完全生效。")
