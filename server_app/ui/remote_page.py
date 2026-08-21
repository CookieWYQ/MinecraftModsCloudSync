# -*- coding: utf-8 -*-
"""服务端 - 更新服务端页（C2S）：把服主制作的新客户端内容同步到服务端文件仓库。

流程：
1. 启动程序与每次上传后，自动读取服务端目录树（server_root）创建快照（本地保存，作为对比基线）；
2. 自动对比本地新客户端 vs 快照，展示「客户端有而服务端没有/不一致」的差异；
3. 差异树支持：排除规则（logs/cache 等）、搜索、排序（名称/大小/类型/时间）、右键复制与设置发送目标；
4. 发送目标：双端（服务端+客户端）/ 服务端 / 客户端 / 跳过；
   标注为「客户端模组」的文件默认只发客户端；
5. 人工追加需要上传的文件（从本地浏览 / 双击远程文件树）；
6. 发送：按目标执行 增加/替换/删除，完成后自动重建快照。
"""
import json
import os
from datetime import datetime

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app_common import winutil
from app_common.logger import get_logger
from app_common.mcmod_db import wiki_id_for
from app_common.mcmod_link import add_mcmod_menu_actions, mod_display_name
from app_common.mod_env import jar_environment, mcmod_env_online
from app_common.mod_identity import filename_base, jar_identifiers
from app_common.sftp import SFTPManager
from app_common.snapshot import load_snapshot, update_snapshot
from app_common.tasks import TodoManifest
from app_common.worker import Worker, fmt_progress

from .remote_tree import RemoteTreeWidget, SelectTreeWidget, fmt_size

log = get_logger("server.remote_page")

STATUS_LABELS = {"new": "新增", "update": "更新", "server": "仅服务器",
                 "obsolete": "旧版残留", "same": "已一致"}
TARGET_LABELS = {"both": "双端", "client": "客户端", "server": "服务端", "skip": "跳过"}
# 默认排除的顶层文件夹（非分发内容，可在「规则…」中修改）
DEFAULT_EXCLUDE = {"logs", "cache", "crash-reports", "backups"}


def _overlap_prefix(child: str, base: str) -> str | None:
    """child 相对 base 的子路径前缀（POSIX）；不在其下或相同返回 None。"""
    base = base.rstrip("/") or "/"
    child = child.rstrip("/")
    if child == base:
        return None
    if base == "/":
        return child.lstrip("/") or None
    if child.startswith(base + "/"):
        return child[len(base) + 1:]
    return None


class RemoteBrowserDialog(QDialog):
    """服务端文件仓库浏览子窗口：目录树展示服务器文件，可切换浏览根目录/服务器根，双击文件加入审核列表。"""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("服务端文件仓库浏览（目录树）")
        self.resize(800, 580)
        self._selected_rel = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)

        root_bar = QHBoxLayout()
        root_bar.addWidget(QLabel("浏览位置："))
        self.cb_root = QComboBox()
        server_root = config.server_root
        files_dir = config.files_dir
        self.cb_root.addItem(f"服务端文件仓库（{server_root}）", server_root)
        self.cb_root.addItem(f"客户端文件目录（{files_dir}）", files_dir)
        if server_root != "/":
            self.cb_root.addItem("服务器根目录（/）", "/")
        self.cb_root.currentIndexChanged.connect(self._on_root_changed)
        root_bar.addWidget(self.cb_root, 1)
        lay.addLayout(root_bar)

        self.tree_widget = RemoteTreeWidget(config)
        self.tree_widget.file_activated.connect(self._on_activated)
        lay.addWidget(self.tree_widget, 1)
        btn_row = QHBoxLayout()
        hint = QLabel("双击文件：本地新客户端中有对应文件时，将加入审核列表（新增/更新）。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        btn_row.addWidget(hint, 1)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        lay.addLayout(btn_row)

        # 初始加载服务端文件仓库根目录
        self.tree_widget.reload(server_root, f"服务端文件仓库（{server_root}）")

    def _on_root_changed(self, _index: int):
        remote_dir = self.cb_root.currentData()
        if remote_dir == "/":
            label = "服务器根目录（/）"
        elif remote_dir == self.config.files_dir:
            label = f"客户端文件目录（{remote_dir}）"
        else:
            label = f"服务端文件仓库（{remote_dir}）"
        self.tree_widget.reload(remote_dir, label)

    def _on_activated(self, rel: str):
        self._selected_rel = rel
        self.accept()

    def selected_rel(self) -> str:
        return self._selected_rel


class RemoteFilePage(QWidget):
    def __init__(self, config, status_cb=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.status_cb = status_cb
        self._worker = None
        self._snapshot_busy = False
        self._rows: list[dict] = []  # {rel, status, size, remote_size, mtime, target, manual, checked}
        self._excludes = set(DEFAULT_EXCLUDE)
        self._client_keywords: list[str] = []
        self._overrides: dict[str, str] = {}  # 手动标注（持久化）：rel/文件夹前缀 → target
        self._ignored: dict[str, bool] = {}   # 忽略列表（持久化）：rel/文件夹前缀 → True
        self._env_results: dict[str, str] = {}   # 本次「分析模组环境」结果：文件名 → client/server/both/unknown
        self._env_sources: dict[str, str] = {}   # 文件名 → 判断来源说明
        self._build()
        self._load_config()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # ---- 远程信息 + 快照时间 ----
        info_row = QHBoxLayout()
        self.lbl_remote = QLabel("")
        self.lbl_remote.setObjectName("muted")
        info_row.addWidget(self.lbl_remote, 1)
        self.lbl_snapshot = QLabel("")
        self.lbl_snapshot.setObjectName("muted")
        info_row.addWidget(self.lbl_snapshot)
        layout.addLayout(info_row)

        btn_row = QHBoxLayout()
        self.btn_scan = QPushButton("手动创建快照")
        self.btn_scan.setObjectName("primary")
        self.btn_scan.setToolTip("重新读取服务端目录树，覆盖更新快照并对比本地差异\n"
                                 "（程序启动与每次发送后会自动创建快照，此处为手动触发）")
        self.btn_add_local = QPushButton("从本地添加文件…")
        self.btn_remove = QPushButton("移除选中")
        self.btn_browse_remote = QPushButton("浏览服务端文件仓库…")
        self.btn_analyze = QPushButton("分析模组环境…")
        self.btn_analyze.setToolTip("分析差异审核列表中本地 mods 下的各模组运行环境\n"
                                    "（客户端 / 服务端 / 双端；依据 jar 元数据与 MC 百科标注，"
                                    "未标注的标为「未知」留给用户自行判断）\n"
                                    "结果显示在「分析结果」列，确认后点「全部应用」生效")
        self.btn_apply_env = QPushButton("全部应用")
        self.btn_apply_env.setObjectName("primary")
        self.btn_apply_env.setToolTip("把本次分析结果写入标注（已手动标注的不覆盖）；\n"
                                      "「分析结果」列保留显示，便于区分「真双端」与「未检测到」")
        self.btn_apply_env.hide()
        self.btn_cancel_env = QPushButton("取消")
        self.btn_cancel_env.hide()
        self.cb_show_server = QCheckBox("显示服务端独有文件（可勾选删除）")
        self.cb_show_server.setToolTip("服务端有而本地没有的文件；勾选后发送时将从服务端删除")
        self.cb_show_ignored = QCheckBox("显示已忽略项")
        self.cb_show_ignored.setToolTip("显示被「忽略」的文件/文件夹（灰色），右键可取消忽略恢复显示")
        btn_row.addWidget(self.btn_scan)
        btn_row.addWidget(self.btn_add_local)
        btn_row.addWidget(self.btn_remove)
        btn_row.addWidget(self.btn_browse_remote)
        btn_row.addWidget(self.btn_analyze)
        btn_row.addWidget(self.btn_apply_env)
        btn_row.addWidget(self.btn_cancel_env)
        btn_row.addStretch(1)
        btn_row.addWidget(self.cb_show_ignored)
        btn_row.addWidget(self.cb_show_server)
        layout.addLayout(btn_row)

        # ---- 差异审核树 ----
        review_box = QGroupBox("差异审核（勾选发送；右键可复制 / 标注为双端、客户端或服务端）")
        rv = QVBoxLayout(review_box)
        rv.setSpacing(6)

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
        self.btn_sort_dir = QPushButton("↓ 正序")
        self.btn_sort_dir.setCheckable(True)
        self.btn_sort_dir.setToolTip("切换正序 / 倒序")
        toolbar.addWidget(self.btn_sort_dir)
        toolbar.addStretch(1)
        rv.addLayout(toolbar)

        self.review_tree = SelectTreeWidget()
        self.review_tree.setColumnCount(7)
        self.review_tree.setHeaderLabels(
            ["文件 / 文件夹", "状态", "目标", "大小", "类型", "时间", "分析结果"])
        self.review_tree.setRootIsDecorated(True)  # 保留展开三角标注
        self.review_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.review_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.review_tree.customContextMenuRequested.connect(self._on_review_menu)
        rh = self.review_tree.header()
        rh.setSectionsMovable(True)
        rh.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2, 3, 4, 5, 6):
            rh.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.review_tree.setMinimumHeight(260)
        self.review_tree.itemChanged.connect(self._on_item_changed)
        rv.addWidget(self.review_tree, 1)
        layout.addWidget(review_box, 1)

        # ---- 差异统计提示（独立一行，避免与目录树区域重叠） ----
        summary_row = QHBoxLayout()
        self.lbl_summary = QLabel("")
        self.lbl_summary.setObjectName("muted")
        self.lbl_summary.setWordWrap(True)
        summary_row.addWidget(self.lbl_summary, 1)
        layout.addLayout(summary_row)

        # ---- 发送 ----
        send_row = QHBoxLayout()
        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("muted")
        send_row.addWidget(self.lbl_status, 1)
        self.btn_send = QPushButton("发送到服务器（上传/删除）")
        self.btn_send.setObjectName("primary")
        send_row.addWidget(self.btn_send)
        layout.addLayout(send_row)

        self.btn_scan.clicked.connect(self._auto_detect)
        self.btn_add_local.clicked.connect(self._add_local_files)
        self.btn_remove.clicked.connect(self._remove_rows)
        self.btn_browse_remote.clicked.connect(self._browse_remote)
        self.btn_analyze.clicked.connect(self._analyze_mods)
        self.btn_apply_env.clicked.connect(self._apply_env_results)
        self.btn_cancel_env.clicked.connect(self._cancel_env_results)
        self.btn_send.clicked.connect(self._send)
        self.cb_show_server.toggled.connect(lambda _: self._refresh_tree())
        self.cb_show_ignored.toggled.connect(lambda _: self._refresh_tree())
        self.ed_search.textChanged.connect(lambda _: self._apply_filter())
        self.cb_sort.currentIndexChanged.connect(lambda _: self._re_sort())
        self.btn_sort_dir.toggled.connect(lambda _: self._re_sort())

    # ---------- 配置加载 ----------
    def _load_config(self):
        self._excludes = set(self.config.exclude_names) or set(DEFAULT_EXCLUDE)
        self._client_keywords = [k.lower() for k in self.config.client_mods]
        self._overrides = dict(self.config.target_overrides)
        self._ignored = dict(self.config.ignore_overrides)
        host = self.config.host()
        if host:
            self.lbl_remote.setText(
                f"服务端文件仓库：{self.config.server_root}"
                f"｜客户端文件：{self.config.files_dir}"
                f"（{host}:{self.config.port()}）")
        else:
            self.lbl_remote.setText("服务端文件仓库：未配置 SFTP（请先在「SFTP 设置」页填写）。")
        snap = load_snapshot(self.config.current_id())
        self.lbl_snapshot.setText(
            f"服务端快照：{snap.get('saved_at') or '无（尚未创建）'}"
            f"（{len(snap.get('files', {}))} 个文件）")
        # 启动/切换服务器时自动创建快照并对比差异
        self._auto_detect()

    def reload(self):
        """切换服务器后刷新界面。"""
        self._rows.clear()
        self._env_results.clear()  # 分析结果针对当前审核列表，切换后作废
        self._env_sources.clear()
        self.btn_apply_env.hide()
        self.btn_cancel_env.hide()
        self.btn_analyze.show()
        self._refresh_tree()
        self._load_config()

    # ---------- 快照 + 差异检测 ----------
    def _auto_target_of(self, rel: str) -> str:
        """默认发送目标：命中「客户端模组」关键词 → 客户端，否则 → 双端。"""
        fname = rel.rsplit("/", 1)[-1].lower()
        return "client" if any(k in fname for k in self._client_keywords) else "both"

    def _is_ignored(self, rel: str) -> bool:
        """是否被「忽略」：文件精确匹配优先，其次文件夹前缀（任一命中即忽略）。"""
        if rel in self._ignored:
            return True
        for key in self._ignored:
            if key and rel.startswith(key.rstrip("/") + "/"):
                return True
        return False

    def _override_target_of(self, rel: str) -> str | None:
        """手动标注的目标：文件精确匹配优先，其次文件夹前缀（最长优先）。"""
        if rel in self._overrides:
            return self._overrides[rel]
        best: str | None = None
        for key, target in self._overrides.items():
            if key and rel.startswith(key.rstrip("/") + "/"):
                if best is None or len(key) > len(best):
                    best = key
        return self._overrides[best] if best else None

    def _effective_target(self, rel: str) -> tuple[str, bool]:
        """实际发送目标与是否手动标注：(target, manual)。

        优先级：手动标注 > 关键词自动判断。
        「分析模组环境」的结果只显示在「分析结果」列，点「全部应用」后才写入标注。
        """
        ov = self._override_target_of(rel)
        if ov:
            return ov, True
        return self._auto_target_of(rel), False

    def _auto_detect(self):
        """创建服务端快照并自动对比本地差异（加载时/上传后/手动触发）。"""
        if self._snapshot_busy:
            return
        if not self.config.host():
            self.lbl_status.setText("未配置 SFTP（请先在「SFTP 设置」页填写）。")
            return
        root = self.config.local_mc_dir
        if not root or not os.path.isdir(root):
            self.lbl_status.setText("未选择本地新客户端根目录，暂不检测差异。")
            return
        self._snapshot_busy = True
        self.btn_scan.setEnabled(False)
        self.btn_add_local.setEnabled(False)
        self.lbl_status.setText("正在读取服务端目录树并创建快照…")
        worker = Worker(self._snapshot_scan_worker)
        worker.progress.connect(self._on_scan_progress)
        worker.done.connect(self._on_snapshot_scan_done)
        self._worker = worker
        worker.start()

    def _snapshot_scan_worker(self, progress_cb=None) -> str:
        """读取服务端文件仓库 + 客户端文件目录（并行、跳过系统目录与排除目录）→ 覆盖式更新快照。

        快照统一以「相对服务端文件仓库根目录」的键保存：
        - 服务端文件 → 相对 server_root 的路径（files_dir 位于其下时自动剔除，避免重复）
        - 客户端文件 → files_dir 的内容统一以 client_files/ 前缀并入
        （files_dir 可以是服务器上任意绝对位置，不再要求位于 server_root 之下）
        """
        excludes = list(self._excludes) or list(DEFAULT_EXCLUDE)
        server_root = self.config.server_root
        files_dir = self.config.files_dir

        def on_scan_progress(dirs: int, entries: int):
            if progress_cb:
                progress_cb(dirs, 0,
                            f"正在读取服务端目录树…（已读 {dirs} 个目录 / {entries} 个条目）")

        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            files: dict[str, int] = {}
            overlap = _overlap_prefix(files_dir, server_root)
            for rel, size in sftp.list_files_recursive_with_size(
                    server_root, excludes=excludes, skip_system=True,
                    progress_cb=on_scan_progress):
                if overlap and (rel == overlap or rel.startswith(overlap + "/")):
                    continue  # 属于客户端文件目录，避免重复计入
                files[rel] = size
            for rel, size in sftp.list_files_recursive_with_size(
                    files_dir, excludes=excludes, skip_system=False,
                    progress_cb=on_scan_progress):
                files[f"client_files/{rel}"] = size
        snap = update_snapshot(self.config.current_id(), files)
        rows = self._diff_local(files)
        return json.dumps({"saved_at": snap.get("saved_at", ""),
                           "changed": snap.get("changed", False),
                           "aged": snap.get("aged", False),
                           "rows": rows, "repo_count": len(files)},
                          ensure_ascii=False)

    def _on_scan_progress(self, current, total, message):
        """快照扫描进度：实时更新状态文本，避免看起来像卡死（目录树扫描无总进度）。"""
        self.lbl_status.setText(fmt_progress(current, total, message, "扫描进度"))

    def _remote_keys_of(self, rel: str, target: str) -> tuple[str, ...]:
        """文件在服务端快照中的可能位置键：
        client → client_files/rel（必须在该位置）；
        server/both → 服务端根 rel 为主，both 的 client_files 副本为可选（任一存在即算已同步）。
        """
        if target == "client":
            return (f"client_files/{rel}",)
        return (rel, f"client_files/{rel}")

    def _diff_status(self, rel: str, target: str, size: int,
                     snapshot_files: dict) -> tuple[str | None, int]:
        """对比文件在快照中的状态：None=已同步 / new=需要上传 / update=需要更新。

        任一约定位置存在且大小一致 → 已同步（不强制双端副本都在）；
        所有约定位置都不存在 → new；存在但大小不同 → update。
        返回 (状态, 远程大小)。
        """
        found = [snapshot_files[k] for k in self._remote_keys_of(rel, target) if k in snapshot_files]
        if not found:
            return "new", 0
        remote_size = found[0]
        if any(s != size for s in found):
            return "update", remote_size
        return None, remote_size

    def _diff_local(self, snapshot_files: dict) -> list:
        """本地文件 vs 服务端快照对比：新增 / 更新 / 旧版残留 / 仅服务器。

        按目标类型找对应位置：客户端模组（client）对比 client_files/ 下，
        服务端/双端以服务端根目录为准（双端副本任一存在即视为已同步），
        避免把服务端已有但 client_files 未补的文件误判为新增。
        """
        root = self.config.local_mc_dir
        excludes = self._excludes or set(DEFAULT_EXCLUDE)
        keywords = [k.lower() for k in self._client_keywords]
        local: dict[str, dict] = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d.lower() not in excludes]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root).replace("\\", "/")
                try:
                    info = {"size": os.path.getsize(full),
                            "mtime": os.path.getmtime(full)}
                except OSError:
                    continue
                local[rel] = info
        rows = []
        for rel in sorted(local):
            info = local[rel]
            target, manual = self._effective_target(rel)
            status, remote_size = self._diff_status(rel, target, info["size"], snapshot_files)
            if status is None:
                continue  # 目标位置均存在且大小一致 → 无需操作
            row = {"rel": rel, "status": status, "size": info["size"],
                   "remote_size": remote_size, "mtime": info["mtime"],
                   "target": target, "manual": manual}
            if self._is_ignored(rel):
                row["ignored"] = True  # 保留原状态，恢复时直接还原
            rows.append(row)
        # 旧版残留：本地 mods 下的 jar 标识集合（modid + 文件名基名），
        # 服务端 mods 下本地不存在的 jar 若命中同一标识 → 视为旧版本残留（默认勾选删除）
        local_jar_ids: set[str] = set()
        for rel in local:
            if rel.split("/", 1)[0] == "mods" and rel.rsplit("/", 1)[-1].lower().endswith(".jar"):
                try:
                    local_jar_ids.update(i for i in jar_identifiers(os.path.join(root, rel)) if i)
                except Exception:
                    continue
        obsolete_rels: set[str] = set()
        for rel in sorted(snapshot_files):
            if rel in local or rel.split("/", 1)[0] != "mods":
                continue
            fname = rel.rsplit("/", 1)[-1].lower()
            if not fname.endswith(".jar"):
                continue
            base = filename_base(fname)
            if base and base in local_jar_ids:
                obsolete_rels.add(rel)
                row = {"rel": rel, "status": "obsolete", "size": 0,
                       "remote_size": snapshot_files[rel], "mtime": 0,
                       "target": "server", "manual": False}
                if self._is_ignored(rel):
                    row["ignored"] = True
                rows.append(row)
        for rel in sorted(snapshot_files):
            if rel in local or rel in obsolete_rels:
                continue
            # client_files/ 下的文件对应本地同路径（去掉前缀），本地存在 → 已同步，不是服务端独有
            if rel.startswith("client_files/"):
                if rel[len("client_files/"):] in local:
                    continue
            row = {"rel": rel, "status": "server", "size": 0,
                   "remote_size": snapshot_files[rel], "mtime": 0,
                   "target": "both", "manual": False}
            if self._is_ignored(rel):
                row["ignored"] = True
            rows.append(row)
        return rows

    def _on_snapshot_scan_done(self, ok: bool, msg: str):
        self._snapshot_busy = False
        self.btn_scan.setEnabled(True)
        self.btn_add_local.setEnabled(True)
        if not ok:
            self.lbl_status.setText("读取服务端快照失败 ✘")
            winutil.error(self, "读取失败", f"无法读取服务端目录树：\n{msg}")
            if self.status_cb:
                self.status_cb(False)
            return
        try:
            data = json.loads(msg or "{}")
        except Exception:
            data = {}
        saved_at = data.get("saved_at", "")
        rows = data.get("rows", [])
        repo_count = data.get("repo_count", 0)
        changed = data.get("changed", False)
        aged = data.get("aged", False)
        self.lbl_snapshot.setText(f"服务端快照：{saved_at or '—'}（{repo_count} 个文件）")
        self._rows = [dict(r, checked=(r["status"] != "server")) for r in rows]
        self._refresh_tree()
        new_n = sum(1 for r in self._rows if r["status"] == "new")
        upd_n = sum(1 for r in self._rows if r["status"] == "update")
        srv_n = sum(1 for r in self._rows if r["status"] == "server")
        obs_n = sum(1 for r in self._rows if r["status"] == "obsolete")
        if changed:
            snap_note = "快照已更新（服务端内容有变化）"
        elif aged:
            snap_note = "快照已自动刷新（距上次超过时限）"
        else:
            snap_note = "快照已刷新（内容未变化，仅更新时间）"
        if repo_count == 0:
            self.lbl_status.setText(
                "警告：未读取到服务端文件仓库（server_root）的任何文件。\n"
                "扫描根目录时已自动跳过系统目录（usr、etc、proc、home 等）"
                "与「规则…」中的排除目录（logs、cache 等）。\n"
                "请检查「SFTP 设置」连接信息，或在本页「服务端版本根目录」处"
                "填写服务器文件实际所在根目录（默认 /）。")
        else:
            self.lbl_status.setText(
                f"对比完成：新增 {new_n}、更新 {upd_n}、"
                f"旧版残留 {obs_n}、服务端独有 {srv_n} ✔"
                f"（服务端仓库共 {repo_count} 个文件，{snap_note}）")
        log.info("更新服务端对比完成: new=%d update=%d obsolete=%d server=%d repo=%d changed=%s aged=%s",
                 new_n, upd_n, obs_n, srv_n, repo_count, changed, aged)

    # ---------- 人工添加（本地浏览） ----------
    def _add_local_files(self):
        root = self.config.local_mc_dir
        if not root or not os.path.isdir(root):
            winutil.warn(self, "提示", "请先选择本地新客户端根目录。")
            return
        if not self.config.host():
            winutil.warn(self, "提示", "请先在「SFTP 设置」页填写服务器信息。")
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "选择要上传到服务器的文件", root)
        if not paths:
            return
        existing = {r["rel"] for r in self._rows}
        rels = []
        for p in paths:
            try:
                rel = os.path.relpath(p, root).replace("\\", "/")
            except ValueError:
                continue
            if rel not in existing:
                rels.append(rel)
                existing.add(rel)
        if not rels:
            winutil.warn(self, "提示", "所选文件均已在审核列表中。")
            return
        self.btn_add_local.setEnabled(False)
        self.lbl_status.setText("正在核对服务器上的对应文件…")
        worker = Worker(self._stat_remote_worker, rels)
        worker.progress.connect(self._on_stat_progress)
        worker.done.connect(self._on_stat_done)
        self._worker = worker
        worker.start()

    def _on_stat_progress(self, current, total, message):
        self.lbl_status.setText(fmt_progress(current, total, message, "核对服务器文件"))

    def _stat_remote_worker(self, rels, progress_cb=None) -> str:
        """批量查询远程文件大小，返回 JSON [[rel, size], ...]（不存在为 -1）。

        按目标类型查对应位置：客户端模组查 client_files/ 下，其余查服务端根目录下。
        """
        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            server_root = self.config.server_root
            client_dir = self.config.files_dir
            out = []
            total = len(rels)
            for i, rel in enumerate(rels):
                target, _ = self._effective_target(rel)
                base = client_dir if target == "client" else server_root
                remote = TodoManifest.remote_source_path(sftp, base, rel)
                out.append([rel, sftp.file_size(remote)])
                if progress_cb:
                    progress_cb(i + 1, total, rel)
        return json.dumps(out, ensure_ascii=False)

    def _on_stat_done(self, ok: bool, msg: str):
        self.btn_add_local.setEnabled(True)
        if not ok:
            self.lbl_status.setText("核对失败 ✘")
            winutil.error(self, "核对失败", str(msg))
            return
        try:
            stats = json.loads(msg or "[]")
        except Exception:
            stats = []
        added = 0
        root = self.config.local_mc_dir
        for rel, rsize in stats:
            if not rel or rel in {r["rel"] for r in self._rows}:
                continue
            local = os.path.join(root, rel)
            try:
                lsize = os.path.getsize(local)
                mtime = os.path.getmtime(local)
            except OSError:
                continue
            if rsize < 0:
                status = "new"
            elif rsize == lsize:
                status = "same"
            else:
                status = "update"
            self._rows.append({"rel": rel, "status": status,
                               "size": lsize, "remote_size": max(rsize, 0),
                               "mtime": mtime, "target": self._auto_target_of(rel),
                               "manual": False,
                               "checked": status != "same"})
            added += 1
        self._refresh_tree()
        self.lbl_status.setText(f"已人工添加 {added} 个文件 ✔" if added
                                else "没有可添加的新文件。")

    # ---------- 人工添加（远程仓库子窗口双击） ----------
    def _browse_remote(self):
        """打开独立子窗口浏览服务端文件仓库；双击文件可加入审核列表。"""
        dlg = RemoteBrowserDialog(self.config, self)
        if dlg.exec() == QDialog.Accepted and dlg.selected_rel():
            self._on_tree_file_activated(dlg.selected_rel())

    def _on_tree_file_activated(self, rel: str):
        root = self.config.local_mc_dir
        local = os.path.join(root, rel) if root else ""
        if not os.path.isfile(local):
            winutil.info(self, "仅服务器文件",
                         f"「{rel}」仅存在于服务器，本地新客户端中没有对应文件。\n\n"
                         "如需删除它，可勾选上方「显示服务端独有文件」后标记删除。")
            return
        for r in self._rows:
            if r["rel"] == rel:
                winutil.info(self, "已在列表中", f"「{rel}」已在审核列表中。")
                return
        try:
            lsize = os.path.getsize(local)
            mtime = os.path.getmtime(local)
        except OSError:
            return
        target, manual = self._effective_target(rel)
        row = {"rel": rel, "status": "update",
               "size": lsize, "remote_size": lsize,
               "mtime": mtime, "target": target,
               "manual": manual, "checked": True}
        if self._is_ignored(rel):
            row["ignored"] = True
        self._rows.append(row)
        self._refresh_tree()
        self.lbl_status.setText(f"已人工加入审核列表：{rel} ✔")

    # ---------- 差异审核树 ----------
    def _refresh_tree(self):
        """按 self._rows 重建差异审核树：目录层级、文件夹三态勾选、目标标注、排序、搜索过滤。"""
        expanded = self._collect_expanded()  # 重建前记录展开状态，标注后保持原样
        self.review_tree.blockSignals(True)
        self.review_tree.clear()
        root_name = os.path.basename(self.config.local_mc_dir.rstrip("/\\")) or "客户端根目录"
        root_item = QTreeWidgetItem([root_name, "", "", "", "", "", ""])
        root_item.setData(0, Qt.UserRole, {"rel": "", "kind": "dir"})
        root_item.setExpanded(True)
        self.review_tree.addTopLevelItem(root_item)

        show_server = self.cb_show_server.isChecked()
        show_ignored = self.cb_show_ignored.isChecked()
        nodes = {"": root_item}
        for r in self._rows:
            if r.get("ignored") and not show_ignored:
                continue  # 被忽略的默认隐藏，勾选「显示已忽略项」后展示以便恢复
            if r["status"] == "server" and not show_server:
                continue  # 服务端独有文件默认不展示，勾选开关后才显示（可删除）
            parts = r["rel"].split("/")
            parent_rel = ""
            parent_item = root_item
            for part in parts[:-1]:
                child_rel = f"{parent_rel}/{part}" if parent_rel else part
                node = nodes.get(child_rel)
                if node is None:
                    node = QTreeWidgetItem([part, "", "", "", "", "", ""])
                    node.setData(0, Qt.UserRole, {"rel": child_rel, "kind": "dir"})
                    parent_item.addChild(node)
                    nodes[child_rel] = node
                parent_rel = child_rel
                parent_item = node
            fname = parts[-1]
            ftype = fname.rsplit(".", 1)[-1].lower() if "." in fname else "文件"
            size = r["size"] if r.get("size") else r.get("remote_size", 0)
            mtime_text = (datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M")
                          if r.get("mtime") else "—")
            env_text, env_tip = self._env_cell(r["rel"])
            ignored = bool(r.get("ignored"))
            status_text = "已忽略" if ignored else STATUS_LABELS.get(r["status"], r["status"])
            node = QTreeWidgetItem([
                fname,
                status_text,
                TARGET_LABELS.get(r["target"], r["target"]),
                fmt_size(size) if size else "—",
                ftype,
                mtime_text,
                env_text,
            ])
            node.setData(0, Qt.UserRole, {
                "rel": r["rel"], "kind": "file", "status": r["status"],
                "target": r.get("target", "both"),
                "size": r.get("size", 0), "mtime": r.get("mtime", 0), "ftype": ftype})
            node.setToolTip(2, self._target_tooltip(r))  # 目标判断依据（手动标注）
            node.setToolTip(6, env_tip)  # 分析结果列：判断依据/未检测到提示
            if ignored:
                node.setToolTip(1, "已忽略：不参与差异审查，右键可取消忽略恢复")
                node.setToolTip(0, "已忽略（右键可取消）")
            parent_item.addChild(node)
            # 新增/更新/旧版残留/仅服务器：可勾选；旧版残留、仅服务器=删除；已忽略不可勾选
            if ignored:
                node.setFlags(node.flags() & ~Qt.ItemIsUserCheckable)
                gray = QBrush(QColor("#888888"))
                for col in range(node.columnCount()):
                    node.setForeground(col, gray)
                font = node.font(0)
                font.setItalic(True)
                node.setFont(0, font)
            elif r["status"] in ("new", "update", "server", "obsolete"):
                node.setFlags(node.flags() | Qt.ItemIsUserCheckable)
                node.setCheckState(0, Qt.Checked if r.get("checked") else Qt.Unchecked)
            else:
                node.setFlags(node.flags() & ~Qt.ItemIsUserCheckable)

        self._apply_dir_checks(root_item)
        self._apply_dir_target(root_item)
        self._sort_tree(root_item)
        # 排序会 removeChild/addChild 重建节点，展开状态需在排序后恢复
        self._restore_expanded(root_item, expanded)
        self.review_tree.blockSignals(False)
        self._apply_filter()
        self._update_summary()

    def _update_summary(self):
        """刷新底部差异统计提示（独立于树重建调用）。"""
        show_server = self.cb_show_server.isChecked()
        new_n = sum(1 for r in self._rows if r["status"] == "new")
        upd_n = sum(1 for r in self._rows if r["status"] == "update")
        checked_n = sum(1 for r in self._rows
                        if r.get("checked") and r["status"] in ("new", "update")
                        and r.get("target") != "skip")
        obs_n = sum(1 for r in self._rows if r["status"] == "obsolete")
        server_n = sum(1 for r in self._rows if r["status"] == "server")
        ignored_n = sum(1 for r in self._rows if r.get("ignored"))
        tip = ("，服务端独有已隐藏" if server_n and not show_server else
               f"，服务端独有 {server_n}（可勾选删除）" if show_server else "")
        ig_tip = f"，已忽略 {ignored_n}（勾选「显示已忽略项」可恢复）" if ignored_n else ""
        self.lbl_summary.setText(
            f"共 {len(self._rows)} 项：新增 {new_n}、更新 {upd_n}"
            f"{f'、旧版残留 {obs_n}（默认勾选删除）' if obs_n else ''}"
            f"、待发送 {checked_n}{tip}{ig_tip}"
            "（目标：双端=服务端+客户端，客户端=仅 client_files）")

    def _update_tree_targets(self):
        """标注后仅刷新目标列与文件夹目标，不重建树（保持展开/滚动状态）。"""
        root = self.review_tree.topLevelItem(0)
        if root is None:
            return
        by_rel = {r["rel"]: r for r in self._rows}
        stack = [root]
        while stack:
            item = stack.pop()
            d = item.data(0, Qt.UserRole) or {}
            if d.get("kind") == "file":
                row = by_rel.get(d.get("rel", ""))
                if row is not None:
                    item.setText(2, TARGET_LABELS.get(row["target"], row["target"]))
                    item.setToolTip(2, self._target_tooltip(row))
                    item.setData(0, Qt.UserRole, {**d, "target": row["target"]})
            for i in range(item.childCount()):
                stack.append(item.child(i))
        self._apply_dir_target(root)
        self._update_summary()

    def _target_tooltip(self, r: dict) -> str:
        """目标列提示：手动标注 / 其他（无额外提示）。"""
        return "手动标注" if r.get("manual") else ""

    def _env_cell(self, rel: str) -> tuple[str, str]:
        """「分析结果」列单元格：(文本, tooltip)。未分析/非模组 → ("", "")。"""
        env_labels = {"client": "客户端", "server": "服务端",
                      "both": "双端", "unknown": "未知（未检测到）"}
        env = self._env_results.get(rel.rsplit("/", 1)[-1].lower())
        if not env:
            return "", ""
        text = env_labels.get(env, env)
        if env == "unknown":
            return text, "jar 元数据与 MC 百科均未标注运行环境，请自行判断"
        src = self._env_sources.get(rel.rsplit("/", 1)[-1].lower(), "")
        return text, f"本次分析：{src}" if src else text

    def _update_env_column(self):
        """仅刷新「分析结果」列，不重建树（保持展开/滚动状态）。"""
        root = self.review_tree.topLevelItem(0)
        if root is None:
            return
        stack = [root]
        while stack:
            item = stack.pop()
            d = item.data(0, Qt.UserRole) or {}
            if d.get("kind") == "file":
                env_text, env_tip = self._env_cell(d.get("rel", ""))
                item.setText(6, env_text)
                item.setToolTip(6, env_tip)
            for i in range(item.childCount()):
                stack.append(item.child(i))

    @staticmethod
    def _apply_dir_checks(item):
        """自底向上为文件夹节点计算三态勾选（√ 全部 / - 部分 / 空）。"""
        states = []
        for i in range(item.childCount()):
            c = item.child(i)
            d = c.data(0, Qt.UserRole) or {}
            if d.get("kind") == "file":
                if c.flags() & Qt.ItemIsUserCheckable:
                    states.append(c.checkState(0))
            elif d.get("kind") == "dir":
                RemoteFilePage._apply_dir_checks(c)
                states.append(c.checkState(0))
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        if states:
            if all(s == Qt.Checked for s in states):
                item.setCheckState(0, Qt.Checked)
            elif all(s == Qt.Unchecked for s in states):
                item.setCheckState(0, Qt.Unchecked)
            else:
                item.setCheckState(0, Qt.PartiallyChecked)
        else:
            item.setCheckState(0, Qt.Unchecked)

    def _apply_dir_target(self, item) -> str:
        """文件夹「目标」列：其下所有文件目标一致时显示统一类型，存在差异时留空。"""
        d = item.data(0, Qt.UserRole) or {}
        if d.get("kind") == "file":
            return d.get("target", "both")
        targets: set[str] = set()
        for i in range(item.childCount()):
            t = self._apply_dir_target(item.child(i))
            if t:
                targets.add(t)
        if len(targets) == 1:
            text = TARGET_LABELS.get(next(iter(targets)), "")
            item.setText(2, text)
            return next(iter(targets))
        item.setText(2, "")
        return ""

    def _collect_expanded(self) -> set[str]:
        """收集当前树中已展开的节点相对路径（供重建后恢复）。"""
        root = self.review_tree.topLevelItem(0)
        if root is None:
            return set()
        expanded: set[str] = set()
        stack = [root]
        while stack:
            item = stack.pop()
            if item.isExpanded():
                d = item.data(0, Qt.UserRole) or {}
                if d.get("rel"):
                    expanded.add(d["rel"])
            for i in range(item.childCount()):
                stack.append(item.child(i))
        return expanded

    def _restore_expanded(self, item, expanded: set[str]):
        """按记录的相对路径恢复子节点展开状态（保持标注前的目录结构）。"""
        for i in range(item.childCount()):
            c = item.child(i)
            d = c.data(0, Qt.UserRole) or {}
            if d.get("rel") in expanded:
                c.setExpanded(True)
            self._restore_expanded(c, expanded)

    def _sort_key(self, item):
        d = item.data(0, Qt.UserRole) or {}
        mode = self.cb_sort.currentData()
        if mode == "size":
            return d.get("size", 0)
        if mode == "type":
            return d.get("ftype", "")
        if mode == "time":
            return d.get("mtime", 0)
        return item.text(0).lower()

    def _sort_tree(self, item):
        """节点内排序：文件夹在前，文件按所选依据排序（名称/大小/类型/时间）。"""
        children = [item.child(i) for i in range(item.childCount())]
        dirs = [c for c in children
                if (c.data(0, Qt.UserRole) or {}).get("kind") == "dir"]
        files = [c for c in children
                 if (c.data(0, Qt.UserRole) or {}).get("kind") == "file"]
        dirs.sort(key=lambda c: c.text(0).lower())
        files.sort(key=self._sort_key)
        if self.btn_sort_dir.isChecked():
            dirs.reverse()
            files.reverse()
        for c in dirs + files:
            item.removeChild(c)
            item.addChild(c)
            if (c.data(0, Qt.UserRole) or {}).get("kind") == "dir":
                self._sort_tree(c)

    def _re_sort(self):
        root = self.review_tree.topLevelItem(0)
        if root is None:
            return
        self._sort_tree(root)

    def _apply_filter(self):
        text = self.ed_search.text().strip()
        root = self.review_tree.topLevelItem(0)
        if root is None:
            return
        if not text:
            self._set_visible_all(root, True)
            return
        self._filter_node(root, text)

    def _filter_node(self, item, text) -> bool:
        import fnmatch
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
            RemoteFilePage._set_visible_all(item.child(i), visible)

    # ---------- 勾选联动 ----------
    def _on_item_changed(self, item, column):
        if column != 0:
            return
        self.review_tree.blockSignals(True)
        try:
            state = item.checkState(0)
            self._set_children_check(item, state)
            self._update_parent_check(item)
        finally:
            self.review_tree.blockSignals(False)

    @staticmethod
    def _set_children_check(item, state):
        for i in range(item.childCount()):
            c = item.child(i)
            if c.flags() & Qt.ItemIsUserCheckable:
                c.setCheckState(0, state)
            RemoteFilePage._set_children_check(c, state)

    def _update_parent_check(self, item):
        parent = item.parent()
        if parent is None:
            return
        states = []
        for i in range(parent.childCount()):
            c = parent.child(i)
            if c.flags() & Qt.ItemIsUserCheckable:
                states.append(c.checkState(0))
        if states:
            if all(s == Qt.Checked for s in states):
                parent.setCheckState(0, Qt.Checked)
            elif all(s == Qt.Unchecked for s in states):
                parent.setCheckState(0, Qt.Unchecked)
            else:
                parent.setCheckState(0, Qt.PartiallyChecked)
        self._update_parent_check(parent)

    # ---------- 右键菜单 ----------
    def _on_review_menu(self, pos):
        item = self.review_tree.itemAt(pos)
        if item is None:
            return
        # 多选：右键落在选中项上时，对全部选中项统一标注
        selected = self.review_tree.selectedItems()
        if item not in selected:
            selected = [item]
        d = item.data(0, Qt.UserRole) or {}
        menu = QMenu(self)
        act_name = menu.addAction("复制名称")
        act_path = menu.addAction("复制相对路径")
        menu.addSeparator()
        keys = []
        for it in selected:
            keys.extend(self._keys_of(it.data(0, Qt.UserRole) or {}))
        # 忽略 / 取消忽略：被忽略的文件/文件夹不参与差异审查（可恢复）
        all_ignored = bool(keys) and all(self._is_ignored(k) for k in keys)
        act_ignore = menu.addAction(
            "取消忽略（恢复显示）" if all_ignored else
            ("忽略（不参与差异审查）" if len(selected) == 1
             else f"忽略（{len(selected)} 项）"))
        act_ignore.setToolTip("被忽略的文件/文件夹不出现在差异审查中（无论新增/更新），\n"
                              "可在按钮行勾选「显示已忽略项」后右键恢复")
        menu.addSeparator()
        mark_menu = menu.addMenu("标注为" if len(selected) == 1
                                 else f"标注为（{len(selected)} 项）")
        for key, label in TARGET_LABELS.items():
            act = mark_menu.addAction(label)
            act.setData(key)
        act_auto = mark_menu.addAction("恢复自动标注")
        act_auto.setToolTip("按「客户端模组」关键词重新判定目标")
        act_auto.setData("auto")
        # 模组文件（.jar）：关联 MC 百科链接（详情页/搜索，功能移植自 PCL CE，署名 PCL CE）
        mcmod_key = ""
        act_mc_view = act_mc_search = act_mc_copy = None
        view_url = search_url = ""
        if d.get("kind") == "file" and item.text(0).lower().endswith(".jar"):
            # 差异审核树中的 jar 均为本地文件：优先读元数据模组名（不含版本号/加载器）
            rel = d.get("rel", "")
            local_path = os.path.join(self.config.local_mc_dir, rel) if rel else ""
            mcmod_key = mod_display_name(local_path, item.text(0))
            menu.addSeparator()
            act_mc_view, act_mc_search, act_mc_copy, view_url, search_url = \
                add_mcmod_menu_actions(menu, mcmod_key)
        act = menu.exec(self.review_tree.viewport().mapToGlobal(pos))
        if act is None:
            return
        if act is act_name:
            QApplication.clipboard().setText(item.text(0))
            self.lbl_status.setText(f"已复制名称：{item.text(0)}")
        elif act is act_path:
            QApplication.clipboard().setText(d.get("rel", ""))
            self.lbl_status.setText(f"已复制相对路径：{d.get('rel', '')}")
        elif act is act_ignore:
            self._toggle_ignore(keys, ignore=not all_ignored)
        elif act.parent() is mark_menu:
            # 标注分支先于百科分支：百科 action 的 parent 是主菜单，不会被误吞
            if act is act_auto:
                self._restore_auto(keys)
            else:
                self._mark_rows(keys, act.data())
        elif mcmod_key:
            if act is act_mc_view:
                QDesktopServices.openUrl(QUrl(view_url))
                self.lbl_status.setText(f"已打开 MC 百科：{mcmod_key}")
            elif act is act_mc_search:
                QDesktopServices.openUrl(QUrl(search_url))
                self.lbl_status.setText(f"已打开 MC 百科搜索：{mcmod_key}")
            elif act is act_mc_copy:
                QApplication.clipboard().setText(view_url)
                self.lbl_status.setText("已复制 MC 百科链接")

    def _keys_of(self, d: dict) -> list[str]:
        """标注键：文件 → 自身相对路径；文件夹 → 文件夹前缀（其下所有文件，含之后的）。"""
        if d.get("kind") == "file":
            return [d["rel"]]
        return [d["rel"]] if d.get("rel") else []

    def _apply_overrides(self) -> int:
        """把手动标注应用到当前差异行（target/manual），返回发生变化的行数。"""
        count = 0
        for r in self._rows:
            target, manual = self._effective_target(r["rel"])
            if r.get("target") != target or r.get("manual") != manual:
                r["target"] = target
                r["manual"] = manual
                count += 1
        return count

    def _mark_rows(self, keys: list[str], target: str):
        """把指定键（文件或文件夹前缀）标注为 target 并持久化，下次检测默认生效。

        只刷新目标列、不重建树，保持用户当前的展开状态。
        """
        for k in keys:
            self._overrides[k] = target
        self.config.target_overrides = dict(self._overrides)
        self._apply_overrides()
        affected = sum(1 for r in self._rows
                       if self._override_target_of(r["rel"]) is not None)
        self._update_tree_targets()
        if affected:
            self.lbl_status.setText(
                f"已标注「{TARGET_LABELS.get(target, target)}」，覆盖 {affected} 个文件"
                "（已保存，下次检测默认生效）")

    def _restore_auto(self, keys: list[str]):
        """移除手动标注并持久化，恢复按「客户端模组」关键词自动判定。

        只刷新目标列、不重建树，保持用户当前的展开状态。
        """
        for k in keys:
            self._overrides.pop(k, None)
        self.config.target_overrides = dict(self._overrides)
        changed = self._apply_overrides()
        self._update_tree_targets()
        if changed:
            self.lbl_status.setText(
                f"已恢复 {changed} 个文件/文件夹的自动标注 ✔（已保存）")

    def _toggle_ignore(self, keys: list[str], ignore: bool):
        """忽略 / 取消忽略指定文件或文件夹（前缀），持久化并刷新差异树。

        被忽略的项保留原始差异状态，取消后直接还原显示。
        采用增量更新（仅刷新受影响行），不重建树、不改变目录树的展开/滚动状态。
        """
        for k in keys:
            if ignore:
                self._ignored[k] = True
            else:
                self._ignored.pop(k, None)
        self.config.ignore_overrides = dict(self._ignored)
        # 同步当前 rows 的 ignored 标记（保留原状态字段，恢复时还原）
        for r in self._rows:
            if self._is_ignored(r["rel"]):
                r["ignored"] = True
            else:
                r.pop("ignored", None)
        self._sync_ignore_rows(self.review_tree.topLevelItem(0))
        self._update_summary()
        n = len(keys)
        if ignore:
            self.lbl_status.setText(
                f"已忽略 {n} 个文件/文件夹 ✔（勾选「显示已忽略项」可查看并恢复）")
        else:
            self.lbl_status.setText(f"已取消忽略 {n} 个文件/文件夹，恢复显示 ✔")

    def _sync_ignore_rows(self, root) -> bool:
        """增量刷新已忽略行的显示/隐藏与样式，返回该节点下是否有可见子项。

        不重建树：只更新受影响行的状态文本、灰色斜体样式与隐藏状态，
        文件夹节点按可见子项自动隐藏/显示。
        """
        if root is None:
            return False
        show_ignored = self.cb_show_ignored.isChecked()
        by_rel = {r["rel"]: r for r in self._rows}
        default_font = QFont(self.review_tree.font())

        def walk(item) -> bool:
            d = item.data(0, Qt.UserRole) or {}
            if d.get("kind") == "file":
                row = by_rel.get(d.get("rel", ""))
                ignored = bool(row and row.get("ignored"))
                if ignored:
                    item.setText(1, "已忽略")
                    gray = QBrush(QColor("#888888"))
                    for col in range(item.columnCount()):
                        item.setForeground(col, gray)
                    f = item.font(0)
                    f.setItalic(True)
                    item.setFont(0, f)
                    item.setToolTip(1, "已忽略：不参与差异审查，右键可取消忽略恢复")
                    item.setToolTip(0, "已忽略（右键可取消）")
                    item.setHidden(not show_ignored)
                    return show_ignored
                # 恢复普通显示
                if row:
                    item.setText(1, STATUS_LABELS.get(row["status"], row["status"]))
                for col in range(item.columnCount()):
                    item.setForeground(col, QBrush())
                item.setFont(0, default_font)
                item.setToolTip(1, "")
                item.setToolTip(0, "")
                item.setHidden(False)
                return True
            # 文件夹：递归后按可见子项决定隐藏
            any_visible = False
            for i in range(item.childCount()):
                if walk(item.child(i)):
                    any_visible = True
            item.setHidden(not any_visible)
            return any_visible

        return walk(root)

    def _collect_actions(self):
        """收集发送动作：uploads=[(rel, target)]，deletes=[rel]，并返回其中旧版残留数量。"""
        root = self.review_tree.topLevelItem(0)
        if root is None:
            return [], [], 0
        uploads: list[tuple[str, str]] = []
        deletes: list[str] = []
        obsolete_n = 0

        def walk(item):
            nonlocal obsolete_n
            d = item.data(0, Qt.UserRole) or {}
            if d.get("kind") == "file":
                if d.get("ignored"):
                    return  # 已忽略项不参与发送
                if item.checkState(0) != Qt.Checked:
                    return
                if d.get("status") in ("new", "update"):
                    if d.get("target") != "skip":
                        uploads.append((d["rel"], d.get("target", "both")))
                elif d.get("status") in ("server", "obsolete"):
                    deletes.append(d["rel"])
                    if d.get("status") == "obsolete":
                        obsolete_n += 1
            else:
                for i in range(item.childCount()):
                    walk(item.child(i))

        walk(root)
        return uploads, deletes, obsolete_n

    def _remove_rows(self):
        items = self.review_tree.selectedItems()
        if not items:
            winutil.warn(self, "提示",
                         "请先选中要移除的文件（或整个文件夹）。\n\n"
                         "· 按住 Ctrl 点击：单独多选\n"
                         "· 按住 Shift 点击：连续多选")
            return
        rels = set()
        for it in items:
            d = it.data(0, Qt.UserRole) or {}
            if d.get("kind") == "file":
                rels.add(d["rel"])
            elif d.get("kind") == "dir":
                prefix = d["rel"] + "/" if d["rel"] else ""
                for r in self._rows:
                    if r["rel"].startswith(prefix):
                        rels.add(r["rel"])
        before = len(self._rows)
        self._rows = [r for r in self._rows if r["rel"] not in rels]
        self._refresh_tree()
        self.lbl_status.setText(f"已移除 {before - len(self._rows)} 项。")

    # ---------- 模组运行环境分析 ----------
    def _analyze_mods(self):
        """分析差异审核列表中本地 mods 下的 .jar 模组运行环境（客户端/服务端/双端）。

        只分析审核树中列出的模组（新增/更新，即本地存在的文件），
        别的模组不在审核列表里选了也没意义。
        """
        root = self.config.local_mc_dir
        if not root or not os.path.isdir(root):
            winutil.warn(self, "提示",
                         "未找到本地新客户端的 mods 目录。\n\n"
                         "请在「服务器设置」页配置本地新客户端根目录。")
            return
        files = []
        for r in self._rows:
            if r["status"] not in ("new", "update"):
                continue  # 仅本地存在的文件可分析
            rel = r["rel"]
            if rel.split("/", 1)[0] != "mods" or not rel.rsplit("/", 1)[-1].lower().endswith(".jar"):
                continue
            p = os.path.join(root, rel)
            if os.path.isfile(p):
                files.append(p)
        if not files:
            winutil.warn(self, "提示",
                         "差异审核列表中没有本地 mods 下的模组（.jar）需要分析。")
            return
        self.btn_analyze.setEnabled(False)
        self.lbl_status.setText("正在分析模组运行环境…")
        worker = Worker(self._analyze_env_worker, files)
        worker.progress.connect(self._on_analyze_progress)
        worker.done.connect(self._on_analyze_done)
        self._worker = worker
        worker.start()

    def _on_analyze_progress(self, current, total, message):
        self.lbl_status.setText(fmt_progress(current, total, message, "分析模组环境"))

    def _analyze_env_worker(self, files, progress_cb=None) -> str:
        """分析给定 jar 列表：jar 元数据分析；Forge 模组（元数据无环境字段）再参考 MC 百科标注。"""
        results = []
        total = len(files)
        for i, path in enumerate(files):
            fname = os.path.basename(path)
            env, source = jar_environment(path)
            name = mod_display_name(path, fname)
            wid = wiki_id_for(name) if name else None
            # 元数据无法判定（如 Forge）→ 参考 MC 百科「运行环境」标注补充
            if wid and env == "unknown":
                env2, source2 = mcmod_env_online(wid)
                if env2 != "unknown":
                    env, source = env2, source2
            results.append({"file": fname, "name": name, "env": env,
                            "source": source, "wiki_id": wid})
            if progress_cb:
                progress_cb(i + 1, total, f"正在分析 {fname}…")
        return json.dumps(results, ensure_ascii=False)

    def _on_analyze_done(self, ok: bool, msg: str):
        self.btn_analyze.setEnabled(True)
        if not ok:
            self.lbl_status.setText("模组环境分析失败 ✘")
            winutil.error(self, "分析失败", str(msg))
            return
        try:
            results = json.loads(msg or "[]")
        except Exception:
            results = []
        # 保存本次分析结果，显示到「分析结果」列（不影响目标列，点「全部应用」才写入标注）
        self._env_results = {r["file"].lower(): r["env"] for r in results}
        self._env_sources = {r["file"].lower(): r["source"] for r in results}
        self._update_env_column()
        # 按钮切换为「全部应用 / 取消」，由用户确认是否采纳本次分析结果
        self.btn_analyze.hide()
        self.btn_apply_env.show()
        self.btn_cancel_env.show()
        unknown = sum(1 for r in results if r.get("env") == "unknown")
        known = len(results) - unknown
        self.lbl_status.setText(
            f"分析完成：{known} 个已判定、{unknown} 个未标注（在「分析结果」列标出，请自行判断）。"
            "确认后点击「全部应用」，或点「取消」放弃。")

    def _apply_env_results(self):
        """全部应用本次分析结果：写入持久化手动标注（右键「恢复自动标注」可撤销）。

        分析结果列保留显示，便于区分「真双端」与「未检测到」。
        """
        env_target = {"client": "client", "server": "server", "both": "both"}
        applied = 0
        for r in self._rows:
            rel = r["rel"]
            if rel.split("/", 1)[0] != "mods" or not rel.rsplit("/", 1)[-1].lower().endswith(".jar"):
                continue
            env = self._env_results.get(rel.rsplit("/", 1)[-1].lower())
            if env not in env_target:
                continue
            if self._override_target_of(rel):
                continue  # 已有手动标注不覆盖
            self._overrides[rel] = env_target[env]
            applied += 1
        self.config.target_overrides = dict(self._overrides)
        # 标注写入后重算目标列（分析结果列保留显示）
        self._rows = [dict(r) for r in self._rows]
        for r in self._rows:
            target, manual = self._effective_target(r["rel"])
            r["target"], r["manual"] = target, manual
        self._update_tree_targets()
        self.btn_apply_env.hide()
        self.btn_cancel_env.hide()
        self.btn_analyze.show()
        self.lbl_status.setText(
            f"已应用 {applied} 个模组的运行环境标注（右键可恢复自动标注）✔")

    def _cancel_env_results(self):
        """放弃本次分析结果：清空「分析结果」列，恢复原目标。"""
        self._env_results.clear()
        self._env_sources.clear()
        self._update_env_column()
        self.btn_apply_env.hide()
        self.btn_cancel_env.hide()
        self.btn_analyze.show()
        self.lbl_status.setText("已取消本次模组环境分析。")

    # ---------- 发送 ----------
    def _send(self):
        root_dir = self.config.local_mc_dir
        uploads, deletes, obsolete_n = self._collect_actions()
        if not uploads and not deletes:
            winutil.warn(self, "提示",
                         "没有勾选任何操作。\n\n勾选「新增/更新」= 上传（按目标）；\n"
                         "勾选「旧版残留/服务端独有」= 从服务端删除。")
            return
        lines = []
        if uploads:
            server_n = sum(1 for _, t in uploads if t in ("server", "both"))
            client_n = sum(1 for _, t in uploads if t in ("client", "both"))
            lines.append(f"【上传】{len(uploads)} 个文件（服务端 {server_n}、客户端 {client_n}）")
        if deletes:
            if obsolete_n:
                lines.append(f"【删除】{len(deletes)} 个文件（其中旧版残留 {obsolete_n} 个）")
            else:
                lines.append(f"【删除】{len(deletes)} 个服务端独有文件")
        detail = "\n".join(lines)
        if not winutil.confirm(self, "确认发送",
                               f"即将把勾选的操作发送到服务器：\n\n{detail}\n\n是否继续？"):
            return
        self.btn_send.setEnabled(False)
        self.btn_scan.setEnabled(False)
        worker = Worker(self._send_worker, uploads, deletes)
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_send_done)
        self._worker = worker
        worker.start()

    def _remote_disp(self, remote: str) -> str:
        """远程路径显示：转为相对服务端根目录，便于区分服务端与 client_files。"""
        root = self.config.server_root.rstrip("/") + "/"
        if remote.startswith(root):
            return remote[len(root):]
        return remote

    def _send_worker(self, uploads, deletes, progress_cb=None) -> str:
        """按目标上传：双端→服务端与 client_files 各一份，服务端→server_root，客户端→client_files。

        进度消息显示远程目标路径（相对服务端根目录），
        客户端的一律带 client_files 前缀，避免与本地相对路径混淆。
        """
        def steps_of(target: str) -> int:
            return 2 if target == "both" else 1

        total = sum(steps_of(t) for _, t in uploads) + len(deletes)
        root = self.config.local_mc_dir
        server_root = self.config.server_root
        client_dir = self.config.files_dir
        done = 0
        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            for rel, target in uploads:
                local = os.path.join(root, rel)
                if target in ("server", "both"):
                    done += 1
                    remote = TodoManifest.remote_source_path(sftp, server_root, rel)
                    sftp.upload(local, remote)
                    if progress_cb:
                        progress_cb(done, total,
                                    f"上传 {self._remote_disp(remote)}"
                                    f"（{TARGET_LABELS.get(target, target)}）")
                if target in ("client", "both"):
                    done += 1
                    remote = TodoManifest.remote_source_path(sftp, client_dir, rel)
                    sftp.upload(local, remote)
                    if progress_cb:
                        progress_cb(done, total,
                                    f"上传 {self._remote_disp(remote)}"
                                    f"（{TARGET_LABELS.get(target, target)}）")
            for rel in deletes:
                done += 1
                remote = TodoManifest.remote_source_path(sftp, server_root, rel)
                if sftp.is_dir(remote):
                    sftp.delete_dir(remote)
                else:
                    sftp.delete(remote)
                if progress_cb:
                    progress_cb(done, total, f"删除 {self._remote_disp(remote)}")
        return f"已上传 {len(uploads)} 个、删除 {len(deletes)} 个。"

    def _on_progress(self, current, total, message):
        self.lbl_status.setText(fmt_progress(current, total, message, "发送进度"))

    def _on_send_done(self, ok: bool, msg: str):
        self.btn_send.setEnabled(True)
        self.btn_scan.setEnabled(True)
        if self.status_cb:
            self.status_cb(ok)
        if not ok:
            self.lbl_status.setText("发送失败 ✘")
            winutil.error(self, "发送失败", f"发送过程中发生错误：\n{msg}")
            return
        self.lbl_status.setText("发送完成 ✔")
        winutil.info(self, "发送完成", msg)
        log.info("更新服务端发送完成: %s", msg)
        # 发送后自动重建快照 + 重新对比
        self._auto_detect()
