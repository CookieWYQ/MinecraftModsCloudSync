# -*- coding: utf-8 -*-
"""客户端 - 共享文件下载对话框：浏览 .upload_files 临时共享区并下载。

- 列出共享文件（大小 / 密钥 / 过期时间），可多选；
- 下载到指定目录并保留相对文件结构；
- 加密文件需输入下载密钥（下载进度为字节级）。
"""
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QProgressDialog,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from app_common import winutil
from app_common.c2c import abs_c2c_dir
from app_common.logger import get_logger
from app_common.sftp import SFTPManager
from app_common.upload_files import download_file, list_remote_files
from app_common.worker import Worker

log = get_logger("client.shared_download")


class SharedDownloadDialog(QDialog):
    """共享文件下载窗口（单实例：由主窗口控制只打开一个）。"""

    def __init__(self, config, profile, parent=None):
        super().__init__(parent)
        self.config = config
        self.profile = profile
        self._info = None
        self._files: list[dict] = []
        self._dest_dir = ""
        self._worker = None
        self.setWindowTitle("共享文件下载（.upload_files）")
        self.setMinimumSize(640, 460)
        self._build()
        self._reload()

    # ---------- 界面 ----------
    def _build(self):
        lay = QVBoxLayout(self)
        head = QHBoxLayout()
        head.addWidget(QLabel(f"服务器：{self.profile.get('name', '')}"))
        head.addStretch(1)
        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("muted")
        head.addWidget(self.lbl_count)
        btn_refresh = QPushButton("刷新")
        btn_refresh.clicked.connect(self._reload)
        head.addWidget(btn_refresh)
        lay.addLayout(head)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["文件（保留相对结构）", "大小", "密钥", "过期时间"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        h = self.tree.header()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        lay.addWidget(self.tree, 1)

        bottom = QHBoxLayout()
        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("muted")
        self.lbl_status.setWordWrap(True)
        bottom.addWidget(self.lbl_status, 1)
        btn_browse = QPushButton("下载到…")
        btn_browse.clicked.connect(self._browse_dest)
        bottom.addWidget(btn_browse)
        self.btn_dl = QPushButton("下载选中")
        self.btn_dl.setObjectName("primary")
        self.btn_dl.clicked.connect(self._download_selected)
        bottom.addWidget(self.btn_dl)
        lay.addLayout(bottom)

    # ---------- 数据加载 ----------
    def _reload(self):
        self.lbl_status.setText("正在读取共享文件列表…")
        self.btn_dl.setEnabled(False)
        worker = Worker(self._load_worker)
        worker.done.connect(self._on_loaded)
        self._worker = worker
        worker.start()

    def _load_worker(self, progress_cb=None):
        from app_common.profile import parse_profile_content
        data = parse_profile_content(self.profile.get("content", ""))
        sftp_cfg = data["sftp"]
        self._info = {
            "host": sftp_cfg["host"],
            "port": int(sftp_cfg.get("port", 22)),
            "username": sftp_cfg.get("username", ""),
            "password": sftp_cfg.get("password", ""),
            "c2c_dir": abs_c2c_dir(sftp_cfg.get("c2c_dir", "")),
        }
        with SFTPManager(self._info["host"], self._info["port"],
                         self._info["username"], self._info["password"],
                         sanitize_log=True) as sftp:
            return list_remote_files(sftp, self._info["c2c_dir"])

    def _on_loaded(self, ok: bool, result):
        if not ok:
            self.lbl_status.setText("读取失败 ✘")
            winutil.error(self, "读取失败", f"无法读取共享文件列表：\n{result}")
            self.btn_dl.setEnabled(True)
            return
        self._files = result or []
        self.tree.clear()
        for f in self._files:
            name = f["rel"] + ("（已过期）" if f["expired"] else "")
            item = QTreeWidgetItem([name, _fmt_size(f["size"]),
                                    "是" if f["encrypted"] else "—",
                                    _fmt_expires(f["expires_at"]) or "永久"])
            item.setData(0, Qt.UserRole, f["rel"])
            if f["expired"]:
                item.setForeground(0, item.foreground(0))
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)  # 过期文件不可选
            self.tree.addTopLevelItem(item)
        self.lbl_count.setText(f"共 {len(self._files)} 个文件")
        self.lbl_status.setText("选择文件后点击「下载选中」，下载保留相对文件结构。")
        self.btn_dl.setEnabled(True)

    # ---------- 下载 ----------
    def _browse_dest(self):
        path = QFileDialog.getExistingDirectory(
            self, "选择下载目标目录（保留相对文件结构）",
            self._dest_dir or self.config.local_mc_dir or "")
        if path:
            self._dest_dir = path
            self.lbl_status.setText(f"下载到：{path}")

    def _selected_rels(self) -> list[str]:
        return [it.data(0, Qt.UserRole) for it in self.tree.selectedItems()
                if it.data(0, Qt.UserRole)]

    def _download_selected(self):
        rels = self._selected_rels()
        if not rels:
            winutil.warn(self, "提示", "请先选择要下载的文件。")
            return
        dest_dir = self._dest_dir or self.config.local_mc_dir
        if not dest_dir or not os.path.isdir(dest_dir):
            winutil.warn(self, "提示",
                         "请先点击「下载到…」选择下载目标目录。")
            return
        meta_by_rel = {f["rel"]: f for f in self._files}
        encrypted = [r for r in rels if meta_by_rel.get(r, {}).get("encrypted")]
        key = ""
        if encrypted:
            key, ok = QInputDialog.getText(
                self, "输入下载密钥",
                f"所选 {len(encrypted)} 个文件已加密，请输入下载密钥：",
                echo=QLineEdit.Password)
            if not ok or not key.strip():
                winutil.warn(self, "提示", "已取消：加密文件需要密钥才能下载。")
                return
            key = key.strip()

        total_bytes = sum(int(meta_by_rel.get(r, {}).get("size", 0)) for r in rels)
        progress = QProgressDialog("正在下载共享文件…", "取消", 0,
                                   max(total_bytes, 1), self)
        progress.setWindowTitle("下载共享文件")
        progress.setWindowModality(Qt.NonModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        self._download_progress = progress
        worker = Worker(self._download_worker, rels, dest_dir, key)
        worker.progress.connect(self._on_download_progress)
        worker.done.connect(lambda ok, msg: self._on_download_done(ok, msg, progress))
        self._worker = worker
        worker.start()

    def _download_worker(self, rels, dest_dir, key, progress_cb=None):
        info = self._info
        meta_by_rel = {f["rel"]: f for f in self._files}
        total_bytes = sum(int(meta_by_rel.get(r, {}).get("size", 0)) for r in rels)
        done = [0]
        with SFTPManager(info["host"], info["port"], info["username"],
                         info["password"], sanitize_log=True) as sftp:
            for rel in rels:
                size = int(meta_by_rel.get(rel, {}).get("size", 0))
                dest = os.path.join(dest_dir, rel.replace("/", os.sep))

                def byte_cb(transferred, total):
                    if progress_cb:
                        progress_cb(min(done[0] + transferred, max(total_bytes, 1)),
                                    max(total_bytes, 1), f"下载 {rel}")
                try:
                    download_file(sftp, info["c2c_dir"], rel, dest, key,
                                  byte_progress_cb=byte_cb)
                except Exception as exc:
                    raise RuntimeError(f"下载 {rel} 失败：{exc}") from exc
                done[0] += size
        return f"已下载 {len(rels)} 个共享文件到：\n{dest_dir}"

    def _on_download_progress(self, cur, total, msg):
        progress = self._download_progress
        if progress is None:
            return
        progress.setRange(0, max(total, 1))
        progress.setValue(cur)
        if msg:
            progress.setLabelText(f"{msg}（{cur / max(total, 1):.0%}）")

    def _on_download_done(self, ok: bool, msg: str, progress):
        progress.close()
        self._download_progress = None
        if ok:
            winutil.info(self, "下载完成", msg)
        else:
            winutil.error(self, "下载失败", f"共享文件下载失败：\n{msg}")


def _fmt_expires(expires_at: str) -> str:
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
