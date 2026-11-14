# -*- coding: utf-8 -*-
"""「历史版本」对话框：查看 / 还原 / 清理更新服务端时备份下来的旧文件。

对应 `app_common/history.py`：
- 历史文件存放在服务器 `{files_dir}/.mc-sync-history`（隐藏目录，扫描自动跳过）；
- 开关与保留策略（每条路径保留几个版本、历史目录最多占多少 MB）保存在当前服务器配置里；
- 还原会把文件移回原路径；原路径已有文件时先自动备份现有文件再覆盖。
"""
import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from . import winutil
from .history import (
    KIND_LABELS,
    clear_history,
    format_size,
    history_root,
    list_history,
    restore_entries,
    HistoryStore,
)
from .logger import get_logger
from .sftp import SFTPManager
from .worker import Worker

log = get_logger("history_dialog")


class HistoryDialog(QDialog):
    """历史版本列表 + 备份策略设置 + 还原 / 删除 / 清空。"""

    def __init__(self, parent=None, config=None):
        super().__init__(parent)
        self.config = config
        self.changed = False          # 是否执行过还原（调用方据此重新扫描差异）
        self._entries: list[dict] = []
        self._worker = None
        self.setWindowTitle("历史版本（删除 / 替换前的旧文件）")
        self.setMinimumSize(900, 560)
        self._build()
        self._reload()
        self._load_settings()

    # ---------- 界面 ----------
    def _build(self):
        layout = QVBoxLayout(self)

        self.cb_enable = QCheckBox("删除 / 替换前，先把旧文件备份到服务器历史目录")
        self.cb_enable.setToolTip(
            "开启后，「更新服务端」删除文件或覆盖同名文件前，会先把旧文件移动到\n"
            "{客户端文件目录}/.mc-sync-history。\n"
            "该目录以 . 开头，不会被扫描 / 发布 / 同步；可随时在这里还原或清理。")
        self.cb_enable.toggled.connect(self._save_settings)
        layout.addWidget(self.cb_enable)

        row = QHBoxLayout()
        row.addWidget(QLabel("每条路径保留最近"))
        self.sp_keep = QSpinBox()
        self.sp_keep.setRange(1, 50)
        self.sp_keep.setSuffix(" 个版本")
        self.sp_keep.setToolTip("同一文件（同一路径）最多保留几个旧版本，更旧的自动清理")
        self.sp_keep.valueChanged.connect(self._save_settings)
        row.addWidget(self.sp_keep)
        row.addWidget(QLabel("历史目录最多占用"))
        self.sp_max = QSpinBox()
        self.sp_max.setRange(0, 20480)
        self.sp_max.setSingleStep(64)
        self.sp_max.setSuffix(" MB")
        self.sp_max.setSpecialValueText("不限")
        self.sp_max.setToolTip("所有历史文件的总大小上限；超出时从最旧的开始清理（0 = 不限）")
        self.sp_max.valueChanged.connect(self._save_settings)
        row.addWidget(self.sp_max)
        self.btn_prune = QPushButton("立即按策略清理")
        self.btn_prune.setToolTip("按上面的保留数量 / 总大小上限清理历史（不会动活动文件）")
        self.btn_prune.clicked.connect(self._prune_now)
        row.addWidget(self.btn_prune)
        row.addStretch(1)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self._reload)
        row.addWidget(self.btn_refresh)
        layout.addLayout(row)

        self.lbl_path = QLabel("")
        self.lbl_path.setObjectName("muted")
        self.lbl_path.setWordWrap(True)
        layout.addWidget(self.lbl_path)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["归档时间", "位置", "原路径", "大小", "原因"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionsMovable(True)
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        layout.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("muted")
        self.lbl_status.setWordWrap(True)
        bottom.addWidget(self.lbl_status, 1)
        self.btn_restore = QPushButton("还原所选")
        self.btn_restore.setObjectName("primary")
        self.btn_restore.setToolTip(
            "把选中的旧文件移回原路径。\n"
            "原路径已有文件时：先自动备份现有文件，再还原旧版。")
        self.btn_restore.clicked.connect(self._restore_selected)
        bottom.addWidget(self.btn_restore)
        self.btn_delete = QPushButton("删除所选")
        self.btn_delete.clicked.connect(self._delete_selected)
        bottom.addWidget(self.btn_delete)
        self.btn_clear = QPushButton("清空全部")
        self.btn_clear.clicked.connect(self._clear_all)
        bottom.addWidget(self.btn_clear)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        bottom.addWidget(btn_close)
        layout.addLayout(bottom)
        self._set_busy(False)

    def _load_settings(self):
        cfg = self.config
        if cfg is None:
            self.lbl_path.setText("未配置服务器。")
            return
        self.cb_enable.blockSignals(True)
        self.sp_keep.blockSignals(True)
        self.sp_max.blockSignals(True)
        self.cb_enable.setChecked(bool(cfg.history_enabled))
        self.sp_keep.setValue(int(cfg.history_keep))
        self.sp_max.setValue(int(cfg.history_max_mb))
        self.cb_enable.blockSignals(False)
        self.sp_keep.blockSignals(False)
        self.sp_max.blockSignals(False)
        self.lbl_path.setText(
            f"历史目录：{history_root(cfg.files_dir)}（隐藏目录，不参与同步 / 发布）")

    def _save_settings(self):
        cfg = self.config
        if cfg is None:
            return
        cfg.history_enabled = bool(self.cb_enable.isChecked())
        cfg.history_keep = int(self.sp_keep.value())
        cfg.history_max_mb = int(self.sp_max.value())
        self.lbl_status.setText("备份设置已保存（下次发送时生效）。")

    def _set_busy(self, busy: bool):
        for w in (self.btn_refresh, self.btn_prune, self.btn_restore,
                  self.btn_delete, self.btn_clear):
            w.setEnabled(not busy)
        self.table.setEnabled(not busy)

    # ---------- 读取 ----------
    def _reload(self):
        cfg = self.config
        if cfg is None or not cfg.host():
            self.lbl_status.setText("未配置 SFTP（请先在「服务器设置」页填写）。")
            return
        self._set_busy(True)
        self.lbl_status.setText("正在读取历史版本…")
        worker = Worker(self._load_worker)
        worker.done.connect(self._on_load_done)
        self._worker = worker
        worker.start()

    def _load_worker(self, progress_cb=None) -> str:
        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            data = list_history(sftp, self.config.files_dir)
        return json.dumps(data, ensure_ascii=False)

    def _on_load_done(self, ok: bool, msg: str):
        self._set_busy(False)
        if not ok:
            self.lbl_status.setText("读取历史版本失败 ✘")
            winutil.error(self, "读取失败", f"无法读取历史版本：\n{msg}")
            return
        try:
            data = json.loads(msg or "{}")
        except Exception:
            data = {}
        self._entries = data.get("entries") or []
        usage = data.get("usage") or {}
        self._fill_table()
        suffix = ""
        if not self.cb_enable.isChecked():
            suffix = "（当前未开启「删除前备份」，不会再产生新的历史）"
        self.lbl_status.setText(
            f"共 {usage.get('count', len(self._entries))} 个历史文件，"
            f"占用 {format_size(usage.get('bytes', 0))}。{suffix}")

    def _fill_table(self):
        self.table.setRowCount(0)
        for e in self._entries:
            r = self.table.rowCount()
            self.table.insertRow(r)
            vals = [
                str(e.get("archived_at", "")).replace("T", " "),
                KIND_LABELS.get(e.get("kind", ""), e.get("kind", "")),
                e.get("rel", ""),
                format_size(e.get("size", 0)),
                e.get("reason", "") or "删除/替换",
            ]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                item.setData(Qt.UserRole, e.get("id", ""))
                if c in (0, 1, 3, 4):
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(r, c, item)
        if not self._entries:
            self.lbl_status.setText(
                "暂无历史版本。开启上方「删除前备份」后，"
                "「更新服务端」删除 / 替换文件时会自动生成。")

    def _selected_ids(self) -> list[str]:
        ids: list[str] = []
        for idx in self.table.selectionModel().selectedRows():
            item = self.table.item(idx.row(), 0)
            if item is not None:
                eid = item.data(Qt.UserRole)
                if eid:
                    ids.append(eid)
        return ids

    # ---------- 操作 ----------
    def _restore_selected(self):
        ids = self._selected_ids()
        if not ids:
            winutil.warn(self, "提示", "请先在列表里选中要还原的历史文件。")
            return
        chosen = [e for e in self._entries if e.get("id") in set(ids)]
        items = [f"{KIND_LABELS.get(e.get('kind'), '')}　{e.get('rel')}"
                 for e in chosen[:30]]
        text = ("即将把选中的旧文件还原回原路径。\n\n"
                "· 原路径已有文件时：会先把现有文件备份到历史，再还原旧版；\n"
                "· 还原后建议回到「更新服务端」页重新扫描，确认差异。")
        if not winutil.confirm_list(self, "确认还原历史版本", text, items,
                                    ok_label="开始还原"):
            return
        self._set_busy(True)
        worker = Worker(self._restore_worker, ids)
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_restore_done)
        self._worker = worker
        worker.start()

    def _restore_worker(self, ids, progress_cb=None) -> str:
        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            result = restore_entries(sftp, self.config.files_dir,
                                     self.config.server_root, ids, progress_cb)
        return json.dumps(result, ensure_ascii=False)

    def _on_restore_done(self, ok: bool, msg: str):
        self._set_busy(False)
        if not ok:
            self.lbl_status.setText("还原失败 ✘")
            winutil.error(self, "还原失败", f"无法还原历史版本：\n{msg}")
            return
        try:
            data = json.loads(msg or "{}")
        except Exception:
            data = {}
        restored = data.get("restored") or []
        failed = data.get("failed") or []
        self.changed = bool(restored)
        text = f"已还原 {len(restored)} 个旧文件。"
        if data.get("backed_up"):
            text += f"\n还原前已备份现有文件 {data['backed_up']} 个到历史。"
        if failed:
            text += "\n失败：\n" + "\n".join(failed[:20])
        self.lbl_status.setText(text.splitlines()[0])
        if failed:
            winutil.warn(self, "还原完成（部分失败）", text)
        else:
            winutil.info(self, "还原完成", text)
        self._reload()

    def _delete_selected(self):
        ids = self._selected_ids()
        if not ids:
            winutil.warn(self, "提示", "请先在列表里选中要删除的历史文件。")
            return
        if not winutil.confirm(
                self, "确认删除",
                f"即将永久删除选中的 {len(ids)} 个历史文件。\n\n"
                "删除后无法再还原这些旧版本，是否继续？"):
            return
        self._set_busy(True)
        worker = Worker(self._delete_worker, ids)
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_simple_done)
        self._worker = worker
        worker.start()

    def _delete_worker(self, ids, progress_cb=None) -> str:
        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            store = HistoryStore(sftp, self.config.files_dir)
            removed = store.remove_entries(ids)
            store.save()
        return f"已删除 {len(removed)} 个历史文件。"

    def _clear_all(self):
        if not self._entries:
            winutil.info(self, "提示", "当前没有历史文件。")
            return
        usage = sum(int(e.get("size") or 0) for e in self._entries)
        if not winutil.confirm(
                self, "确认清空",
                f"即将清空全部历史版本（{len(self._entries)} 个文件，"
                f"{format_size(usage)}）。\n\n清空后无法再还原这些旧版本，是否继续？"):
            return
        self._set_busy(True)
        worker = Worker(self._clear_worker)
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_simple_done)
        self._worker = worker
        worker.start()

    def _clear_worker(self, progress_cb=None) -> str:
        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            n = clear_history(sftp, self.config.files_dir)
        return f"已清空 {n} 个历史文件。"

    def _prune_now(self):
        self._set_busy(True)
        worker = Worker(self._prune_worker, int(self.sp_keep.value()),
                        int(self.sp_max.value()))
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_simple_done)
        self._worker = worker
        worker.start()

    def _prune_worker(self, keep, max_mb, progress_cb=None) -> str:
        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            store = HistoryStore(sftp, self.config.files_dir)
            removed = store.prune(keep, max_mb)
            store.save()
        return f"按策略清理了 {len(removed)} 个历史文件。"

    def _on_simple_done(self, ok: bool, msg: str):
        self._set_busy(False)
        if not ok:
            self.lbl_status.setText("操作失败 ✘")
            winutil.error(self, "操作失败", f"执行失败：\n{msg}")
            return
        self.lbl_status.setText(str(msg))
        self._reload()

    def _on_progress(self, current, total, message):
        self.lbl_status.setText(message)
