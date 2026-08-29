# -*- coding: utf-8 -*-
"""服务端 - 发布待办页：检测服务端改动（新增/替换/删除），选择是否发布到客户端。

- 服务端改动树即任务清单：全部默认启用（发布），右键可禁用（变灰）/重新启用/批注。
- 每次打开本页自动检测一遍服务端改动（带统一进度格式）。
- 发布时只把「启用」的改动写入清单；禁用项保持原基线，下次仍会检测出来。
"""
import fnmatch
import json
import os
import re
import time
from datetime import datetime

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app_common import winutil
from app_common.c2c import abs_c2c_dir
from app_common.constants import config_dir
from app_common.file_hash import hash_remote_smart
from app_common.logger import get_logger
from app_common.mcmod_link import add_mcmod_menu_actions, mod_search_name
from app_common.sftp import SFTPManager
from app_common.snapshot import load_snapshot, normalize_files, save_snapshot
from app_common.snapshot_dialog import SnapshotHistoryDialog
from app_common.tasks import CATEGORY_LABELS, TaskItem, TodoManifest, safe_target
from app_common.upload_files import list_remote_files
from app_common.worker import Worker, fmt_progress

from .remote_tree import SelectTreeWidget

log = get_logger("server.todo_page")

# 服务端改动状态：new=新增（绿）、update=替换、rename=改名（蓝）、deleted=服务端已移除、obsolete=旧版残留（后两者均为删除，红）
CHANGE_LABELS = {"new": "新增", "update": "替换", "rename": "改名",
                 "deleted": "删除", "obsolete": "删除"}
CHANGE_COLORS = {"new": "#4caf50", "update": "#ff9800",
                 "rename": "#2196f3",
                 "deleted": "#ef5350", "obsolete": "#ef5350"}
_STATUS_ORDER = {"new": 0, "update": 1, "rename": 2, "deleted": 3, "obsolete": 4}


# 末尾版本段：-1.0 / -1.20.1 / _2.0 / .1.0 等（不含连字符连接的更多段）
_VERSION_SEG = re.compile(r"[-_.](v?\d[\d.]*)$")


def _mod_group_key(filename: str) -> str:
    """模组分组键：去扩展名并去掉最后一个版本段（a-1.20.1-1.0.jar → a-1.20.1）。"""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return _VERSION_SEG.sub("", stem).strip().lower()


def _mod_version_key(filename: str) -> tuple:
    """模组版本数字元组（比较新旧，如 2.0 > 1.0）；无版本段返回 (0,)。"""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    m = _VERSION_SEG.search(stem)
    if not m:
        return (0,)
    return tuple(int(x) for x in re.findall(r"\d+", m.group(1)))


# ---------- 本地持久化：禁用项 + 批注（每台服务器独立） ----------
def _todo_state_path(server_id: str) -> str:
    d = config_dir() / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / f"todo_{server_id}.json")


def _load_todo_state(server_id: str) -> dict:
    path = _todo_state_path(server_id)
    if not os.path.exists(path):
        return {"disabled": set(), "notes": {}}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {
            "disabled": set(data.get("disabled") or []),
            "notes": {k: v for k, v in (data.get("notes") or {}).items() if v},
        }
    except Exception as exc:
        log.warning("读取待办状态失败: %s", exc)
        return {"disabled": set(), "notes": {}}


def _save_todo_state(server_id: str, state: dict) -> None:
    try:
        with open(_todo_state_path(server_id), "w", encoding="utf-8") as f:
            json.dump({"disabled": sorted(state["disabled"]),
                       "notes": state["notes"]}, f, ensure_ascii=False)
    except OSError as exc:
        log.warning("保存待办状态失败: %s", exc)


# ---------- 检测哈希缓存（避免每次检测都全量下载确认「大小相同」的文件） ----------
def _hash_cache_path(server_id: str) -> str:
    d = config_dir() / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / f"hash_cache_{server_id}.json")


def _load_hash_cache(server_id: str) -> dict:
    path = _hash_cache_path(server_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("读取检测哈希缓存失败: %s", exc)
        return {}


def _save_hash_cache(server_id: str, cache: dict) -> None:
    try:
        with open(_hash_cache_path(server_id), "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except OSError as exc:
        log.warning("保存检测哈希缓存失败: %s", exc)


class TodoPage(QWidget):
    def __init__(self, config, status_cb=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.status_cb = status_cb
        self._worker = None
        self._detecting = False
        self._auto_mode = False
        self._last_auto_detect = 0.0
        self._rows: list[dict] = []
        self._state = _load_todo_state(self.config.current_id())
        self._shared_tasks: list[dict] = []  # 共享文件下载任务 [{rel, target, category}]
        self._build()
        self._refresh_pub_snap()

    # ---------- 界面 ----------
    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        head = QHBoxLayout()
        head.addWidget(QLabel("版本号："))
        self.ed_version = QLineEdit()
        self.ed_version.setPlaceholderText("自定义版本号，如 hotfix-1.0.2")
        self.ed_version.setText(f"hotfix-{datetime.now().strftime('%m%d')}")
        head.addWidget(self.ed_version, 1)
        self.lbl_time = QLabel("创建时间：—")
        self.lbl_time.setObjectName("muted")
        head.addWidget(self.lbl_time)
        layout.addLayout(head)

        # 发布设置：可选 / 静默 / 发布说明 / 共享文件下载任务
        opt_row = QHBoxLayout()
        self.chk_optional = QCheckBox("本批待办为可选（客户端可勾选跳过）")
        self.chk_silent = QCheckBox("静默下载应用（客户端后台执行，不弹窗打扰）")
        opt_row.addWidget(self.chk_optional)
        opt_row.addWidget(self.chk_silent)
        opt_row.addSpacing(10)
        opt_row.addWidget(QLabel("发布说明："))
        self.ed_note = QLineEdit()
        self.ed_note.setPlaceholderText("可选：本次发布的说明（时间自动记录，随待办下发展示给客户端）")
        opt_row.addWidget(self.ed_note, 1)
        layout.addLayout(opt_row)

        shared_row = QHBoxLayout()
        self.btn_add_shared = QPushButton("添加共享下载任务…")
        self.btn_add_shared.setToolTip("从 .upload_files 共享区选择文件，作为「下载安装」待办发布给客户端")
        self.btn_add_shared.clicked.connect(self._add_shared_task)
        self.btn_remove_shared = QPushButton("移除选中")
        self.btn_remove_shared.setToolTip("移除选中的共享下载任务")
        self.btn_remove_shared.clicked.connect(self._remove_shared_task)
        shared_row.addWidget(self.btn_add_shared)
        shared_row.addWidget(self.btn_remove_shared)
        self.list_shared_tasks = QListWidget()
        self.list_shared_tasks.setMaximumHeight(84)
        self.list_shared_tasks.setSelectionMode(QListWidget.ExtendedSelection)
        shared_row.addWidget(self.list_shared_tasks, 1)
        layout.addLayout(shared_row)

        # 服务端改动检测：改动树即任务清单，全部默认发布，右键可禁用/批注
        detect_box = QGroupBox(
            "服务端改动检测（新增/替换/删除，全部默认发布；右键可禁用 / 批注，"
            "Ctrl/Shift 多选、Ctrl+Shift+A 反选）")
        dv = QVBoxLayout(detect_box)
        dv.setSpacing(6)

        head_row = QHBoxLayout()
        self.lbl_pub_snap = QLabel("上次发布快照：无")
        self.lbl_pub_snap.setObjectName("muted")
        head_row.addWidget(self.lbl_pub_snap, 1)
        self.btn_snapshots = QPushButton("快照历史…")
        self.btn_snapshots.setToolTip(
            "查看全部历史快照（时间戳 / 文件树）\n"
            "可将任意历史快照设为发布基线，从而撤销 / 回滚客户端更新")
        self.btn_snapshots.clicked.connect(self._open_snapshots)
        head_row.addWidget(self.btn_snapshots)
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索名称（支持 * 通配）")
        self.ed_search.setClearButtonEnabled(True)  # 一键清空搜索
        self.ed_search.setMaximumWidth(240)
        head_row.addWidget(self.ed_search)
        head_row.addWidget(QLabel("排序："))
        self.cb_sort = QComboBox()
        self.cb_sort.addItem("按名称", "name")
        self.cb_sort.addItem("按状态", "status")
        head_row.addWidget(self.cb_sort)
        self.btn_sort_dir = QPushButton("↓ 正序")
        self.btn_sort_dir.setCheckable(True)
        self.btn_sort_dir.setToolTip("切换正序 / 倒序")
        head_row.addWidget(self.btn_sort_dir)
        self.btn_detect = QPushButton("重新检测")
        head_row.addWidget(self.btn_detect)
        dv.addLayout(head_row)

        self.changes_tree = SelectTreeWidget()
        self.changes_tree.setColumnCount(3)
        self.changes_tree.setHeaderLabels(["文件 / 文件夹", "状态", "说明"])
        self.changes_tree.setRootIsDecorated(True)  # 保留展开三角标注
        self.changes_tree.setMinimumHeight(300)
        ch = self.changes_tree.header()
        ch.setSectionsMovable(True)
        ch.setSectionResizeMode(0, QHeaderView.Stretch)
        ch.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        ch.setSectionResizeMode(2, QHeaderView.Stretch)
        self.changes_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.changes_tree.customContextMenuRequested.connect(self._on_changes_menu)
        dv.addWidget(self.changes_tree, 1)
        layout.addWidget(detect_box, 1)

        pub_row = QHBoxLayout()
        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("muted")
        self.lbl_status.setWordWrap(True)
        pub_row.addWidget(self.lbl_status, 1)
        self.btn_ignore = QPushButton("忽略本次快照更新")
        self.btn_ignore.setToolTip("跳过本次检测到的所有改动（本次不发布）\n"
                                   "已忽略的改动可随时通过「重新发布已忽略」恢复")
        self.btn_ignore.clicked.connect(self._ignore_all)
        self.btn_republish = QPushButton("重新发布已忽略")
        self.btn_republish.setToolTip("把已忽略的改动恢复为可发布状态")
        self.btn_republish.clicked.connect(self._republish_all)
        self.btn_publish = QPushButton("发布到服务器")
        self.btn_publish.setObjectName("primary")
        pub_row.addWidget(self.btn_ignore)
        pub_row.addWidget(self.btn_republish)
        pub_row.addWidget(self.btn_publish)
        layout.addLayout(pub_row)

        self.btn_detect.clicked.connect(lambda: self._detect_changes())
        self.btn_publish.clicked.connect(self._publish)
        self.ed_search.textChanged.connect(lambda _: self._apply_filter())
        self.cb_sort.currentIndexChanged.connect(lambda _: self._re_sort())
        self.btn_sort_dir.toggled.connect(lambda _: self._re_sort())

        self._default_font = QFont(self.changes_tree.font())

    def showEvent(self, event):
        """每次打开本页自动检测一遍服务端改动。"""
        super().showEvent(event)
        self._auto_detect()

    def reload(self):
        """切换服务器后刷新界面并重新检测。"""
        self._refresh_pub_snap()
        self.changes_tree.clear()
        self._rows = []
        self._state = _load_todo_state(self.config.current_id())
        self._auto_detect(force=True)

    def _auto_detect(self, force: bool = False):
        """自动检测：页面打开 / 切换服务器时调用；5 秒内不重复触发。"""
        if self._detecting:
            return
        if not self.config.host():
            return  # 未配置 SFTP 时静默跳过
        now = time.time()
        if not force and now - self._last_auto_detect < 5:
            return
        self._last_auto_detect = now
        self._detect_changes(auto=True)

    # ---------- 服务端改动检测 ----------
    def _refresh_pub_snap(self):
        """显示上次发布时保存的基线时间（作为改动检测基线）。"""
        snap = load_snapshot(self.config.current_id(), "publish")
        self.lbl_pub_snap.setText(
            f"上次发布快照：{snap.get('saved_at') or '无（首次检测将对比空基线）'}")

    def _open_snapshots(self):
        """打开快照历史对话框（查看时间戳 / 文件树，可设为发布基线以撤销更新）。"""
        dlg = SnapshotHistoryDialog(self, config=self.config)
        dlg.exec()
        self._refresh_pub_snap()  # 可能被设为新基线，刷新显示

    def _detect_changes(self, auto: bool = False):
        if self._detecting:
            return
        if not self.config.host():
            if not auto:
                winutil.warn(self, "提示", "请先在「SFTP 设置」页填写服务器信息。")
            else:
                self.lbl_status.setText("未配置 SFTP，跳过自动检测")
            return
        self._auto_mode = auto
        self._detecting = True
        self.btn_detect.setEnabled(False)
        self.lbl_status.setText("正在检测服务端改动…")
        worker = Worker(self._detect_worker)
        worker.progress.connect(self._on_detect_progress)
        worker.done.connect(self._on_detect_done)
        self._worker = worker
        worker.start()

    def _on_detect_progress(self, current, total, message):
        self.lbl_status.setText(fmt_progress(current, total, message, "检测服务端改动"))

    def _detect_worker(self, progress_cb=None) -> str:
        """当前客户端文件夹（client_files） vs 上次发布基线，返回 JSON 行列表。

        每行：{"rel", "status": new|update|rename|deleted|obsolete, "size", "old_rel"?}
        判定条件组合（大小 + 哈希）：
        - 基线无 → 新增；
        - 大小不同，或大小相同但哈希不同 → 替换；
        - 当前新增与基线已移除的文件内容相同（大小 + 哈希一致）→ 改名；
        - 基线有、当前无且未配对 → 删除。
        """

        def on_scan(dirs: int, entries: int):
            if progress_cb:
                progress_cb(dirs, 0,
                            f"正在读取服务端文件仓库…（已读 {dirs} 个目录 / {entries} 个条目）")

        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            # client_files 目录树较小：串行扫描，复用当前连接，不额外开连接
            current = dict(sftp.list_files_recursive_with_size(
                self.config.files_dir, max_workers=1,
                progress_cb=on_scan))
            base = normalize_files(
                load_snapshot(self.config.current_id(), "publish").get("files", {}))
            # 需要计算当前文件哈希（快速哈希：SFTP 远程计算，不下载内容）：
            #  1) 当前新增（基线无）→ 与「服务端已移除」配对判断改名；
            #  2) 基线存在、大小相同 → 总是远程计算当前哈希确认内容是否一致
            #     （即使基线已存哈希也重新确认：服务端文件被替换但大小不变时同样能检出）。
            #     （大小不同 → 直接判定替换，无需哈希，最快。）
            cache = _load_hash_cache(self.config.current_id())
            need_hash = []
            for rel, rsize in current.items():
                bmeta = base.get(rel)
                if bmeta is None or bmeta["size"] == rsize:
                    need_hash.append(rel)
            cur_hash: dict[str, str] = {}
            total = len(need_hash)
            for i, rel in enumerate(need_hash):
                remote = TodoManifest.remote_source_path(
                    sftp, self.config.files_dir, rel)
                h = hash_remote_smart(sftp, remote)
                if h:
                    cur_hash[rel] = h
                if progress_cb:
                    progress_cb(i + 1, max(total, 1), f"计算哈希 {rel}")
            # 更新哈希缓存：仅记录与基线一致（或无法确认但大小相同）的文件，
            # 供下次检测跳过重复下载；大小变化 / 已移除的文件从缓存剔除。
            new_cache = dict(cache)
            for rel, rsize in current.items():
                bmeta = base.get(rel)
                if bmeta is None or bmeta["size"] != rsize:
                    new_cache.pop(rel, None)
                    continue
                h = cur_hash.get(rel)
                if h:
                    new_cache[rel] = {"size": rsize, "hash": h}
                elif rel not in new_cache:
                    new_cache[rel] = {"size": rsize, "hash": bmeta.get("hash")}
            for rel in list(new_cache):
                if rel not in current:
                    new_cache.pop(rel, None)
            _save_hash_cache(self.config.current_id(), new_cache)

        rows = []
        for rel in sorted(current):
            rsize = current[rel]
            bmeta = base.get(rel)
            if bmeta is None:
                rows.append({"rel": rel, "status": "new", "size": rsize,
                             "hash": cur_hash.get(rel)})
            elif bmeta["size"] != rsize:
                rows.append({"rel": rel, "status": "update", "size": rsize})
            else:
                # 大小相同：基线哈希优先；基线无哈希（旧数据）时用缓存中上次确认的哈希
                bh = bmeta.get("hash")
                if not bh:
                    cc = cache.get(rel)
                    if cc and cc.get("size") == rsize and cc.get("hash"):
                        bh = cc["hash"]
                ch = cur_hash.get(rel)
                if bh and ch and bh != ch:
                    # 大小相同但内容不同 → 替换
                    rows.append({"rel": rel, "status": "update", "size": rsize})
                # 否则视为一致，不出行（当前哈希已写入缓存，下次可直接比对）

        # 服务端已移除（基线里有、当前没有）→ 客户端应删除；
        # 内容与当前新增文件相同（大小 + 哈希一致）→ 判定为改名。
        deleted_rels = [rel for rel in sorted(base) if rel not in current]
        matched = self._match_rename_pairs(rows, base, deleted_rels)
        final = []
        for r in rows:
            if r["rel"] in matched:
                final.append({**r, "status": "rename",
                              "old_rel": matched[r["rel"]]})
            else:
                final.append(r)
        for rel in deleted_rels:
            if rel in matched.values():
                continue  # 已作为改名的旧文件，不再单独删除
            final.append({"rel": rel, "status": "deleted",
                          "size": base[rel]["size"]})
        rows = final

        # 旧版残留：client_files/mods 下同模组（去末尾版本段）存在多个版本时，
        # 保留版本号最大的一个，其余标记为「旧版残留」（删除任务）。
        # 旧版残留优先于快照差异（即使同时是新增/更新，也视为待删除的旧版）。
        groups: dict[str, list[str]] = {}
        for rel in sorted(current):
            if rel.split("/", 1)[0] == "mods" and rel.rsplit("/", 1)[-1].lower().endswith(".jar"):
                key = _mod_group_key(rel.rsplit("/", 1)[-1])
                if key:
                    groups.setdefault(key, []).append(rel)
        obsolete_rels: set[str] = set()
        for rels in groups.values():
            if len(rels) < 2:
                continue
            rels.sort(key=_mod_version_key, reverse=True)  # 版本大在前
            obsolete_rels.update(rels[1:])
        rows = [r for r in rows if r["rel"] not in obsolete_rels]
        for rel in sorted(obsolete_rels):
            rows.append({"rel": rel, "status": "obsolete", "size": current[rel]})
        return json.dumps(rows, ensure_ascii=False)

    @staticmethod
    def _match_rename_pairs(new_rows: list[dict], base: dict,
                            deleted_rels: list[str]) -> dict:
        """匹配改名对：{当前新增 Y: 基线移除 X}（大小 + 哈希都相同 → 判定为改名）。

        只匹配基线中哈希已知的文件（发布时记录）；旧基线无哈希则按删除处理。
        """
        new_by_hash: dict[tuple[int, str], list[dict]] = {}
        for r in new_rows:
            h = r.get("hash")
            if h:
                new_by_hash.setdefault((r["size"], h), []).append(r)
        matched: dict[str, str] = {}
        used: set[str] = set()
        for rel in deleted_rels:
            bmeta = base.get(rel) or {}
            if not bmeta.get("hash"):
                continue
            for r in new_by_hash.get((bmeta["size"], bmeta["hash"]), []):
                if r["rel"] not in used:
                    matched[r["rel"]] = rel
                    used.add(r["rel"])
                    break
        return matched

    def _on_detect_done(self, ok: bool, msg: str):
        self._detecting = False
        self.btn_detect.setEnabled(True)
        if not ok:
            self.lbl_status.setText("检测失败 ✘")
            if not self._auto_mode:  # 自动检测失败只提示状态，不弹窗
                winutil.error(self, "检测失败", f"无法读取服务端文件仓库：\n{msg}")
            if self.status_cb:
                self.status_cb(False)
            return
        try:
            rows = json.loads(msg or "[]")
        except Exception:
            rows = []
        self._rows = rows
        self._build_changes_tree(rows)
        # 清理已不再出现的禁用项（保留批注）
        current_rels = {r["rel"] for r in rows}
        stale = self._state["disabled"] - current_rels
        if stale:
            self._state["disabled"] -= stale
            _save_todo_state(self.config.current_id(), self._state)
        new_n = sum(1 for r in rows if r["status"] == "new")
        upd_n = sum(1 for r in rows if r["status"] == "update")
        rnm_n = sum(1 for r in rows if r["status"] == "rename")
        del_n = sum(1 for r in rows if r["status"] in ("deleted", "obsolete"))
        if not rows:
            self.lbl_status.setText(
                "未检测到服务端改动：client_files 与上次发布基线一致。")
        else:
            self.lbl_status.setText(
                f"服务端改动：新增 {new_n}、替换 {upd_n}"
                f"{f'、改名 {rnm_n}' if rnm_n else ''}、删除 {del_n}，共 {len(rows)} 项 ✔"
                "（全部默认发布，右键可禁用）")
        log.info("服务端改动检测完成: new=%d update=%d rename=%d delete=%d",
                 new_n, upd_n, rnm_n, del_n)

    # ---------- 改动树 ----------
    def _build_changes_tree(self, rows):
        """按检测结果重建改动树（无顶层根，目录层级、文件夹在前）。"""
        self.changes_tree.blockSignals(True)
        self.changes_tree.clear()
        nodes: dict[str, QTreeWidgetItem] = {}
        for r in rows:
            parts = r["rel"].split("/")
            parent_rel = ""
            parent_item: QTreeWidgetItem | None = None
            for part in parts[:-1]:
                child_rel = f"{parent_rel}/{part}" if parent_rel else part
                node = nodes.get(child_rel)
                if node is None:
                    node = QTreeWidgetItem([part, "", ""])
                    node.setData(0, Qt.UserRole, {"rel": child_rel, "kind": "dir"})
                    if parent_item is None:
                        self.changes_tree.addTopLevelItem(node)
                    else:
                        parent_item.addChild(node)
                    nodes[child_rel] = node
                parent_rel = child_rel
                parent_item = node
            desc = f"原名：{r['old_rel']}" if r.get("old_rel") else ""
            node = QTreeWidgetItem(
                [parts[-1], CHANGE_LABELS.get(r["status"], r["status"]), desc])
            node.setData(0, Qt.UserRole,
                         {"rel": r["rel"], "kind": "file",
                          "status": r["status"], "size": r.get("size", 0),
                          "old_rel": r.get("old_rel", "")})
            if parent_item is None:
                self.changes_tree.addTopLevelItem(node)
            else:
                parent_item.addChild(node)

        self._apply_state_recursive_top()  # 恢复禁用状态与批注
        self._re_sort()                    # 排序 + 搜索过滤
        self.changes_tree.blockSignals(False)

    # ---- 状态（禁用/批注）样式 ----
    def _apply_state_recursive_top(self):
        for i in range(self.changes_tree.topLevelItemCount()):
            self._apply_state_item(self.changes_tree.topLevelItem(i))

    def _apply_state_item(self, item):
        d = item.data(0, Qt.UserRole) or {}
        note = self._state["notes"].get(d.get("rel", ""))
        if note:
            item.setText(2, note)
        if d.get("kind") == "file":
            self._style_file_item(item)
        for i in range(item.childCount()):
            self._apply_state_item(item.child(i))

    def _style_file_item(self, item):
        """按启用状态着色：禁用 → 灰色斜体 + 状态「已禁用」；否则按新增/替换/删除着色。"""
        d = item.data(0, Qt.UserRole) or {}
        if d.get("rel") in self._state["disabled"]:
            gray = QBrush(QColor("#8a8a8a"))
            font = QFont(self._default_font)
            font.setItalic(True)
            for col in range(3):
                item.setForeground(col, gray)
                item.setFont(col, font)
            item.setText(1, "已禁用")
        else:
            color = CHANGE_COLORS.get(d.get("status"))
            for col in range(3):
                item.setFont(col, QFont(self._default_font))
            brush = QBrush(QColor(color)) if color else QBrush()
            item.setForeground(0, brush)
            item.setForeground(1, brush)
            item.setForeground(2, QBrush(self.changes_tree.palette().text().color()))
            item.setText(1, CHANGE_LABELS.get(d.get("status"), d.get("status", "")))

    def _dir_file_rels(self, item) -> list[str]:
        rels: list[str] = []

        def walk(it):
            d = it.data(0, Qt.UserRole) or {}
            if d.get("kind") == "file":
                rels.append(d["rel"])
            else:
                for i in range(it.childCount()):
                    walk(it.child(i))

        walk(item)
        return rels

    def _selected_items(self) -> list[QTreeWidgetItem]:
        """当前选中的全部条目（含子层级的选中项）。"""
        out: list[QTreeWidgetItem] = []

        def walk(item):
            if item.isSelected():
                out.append(item)
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self.changes_tree.topLevelItemCount()):
            walk(self.changes_tree.topLevelItem(i))
        return out

    def _file_rels_of_items(self, items) -> list[str]:
        """选区涉及的所有文件相对路径（目录展开到其下所有文件），去重保序。"""
        rels: list[str] = []
        for it in items:
            d = it.data(0, Qt.UserRole) or {}
            if d.get("kind") == "dir":
                rels.extend(self._dir_file_rels(it))
            elif d.get("rel"):
                rels.append(d["rel"])
        return list(dict.fromkeys(rels))

    def _toggle_items(self, items, disable: bool):
        """禁用 / 启用选区（目录作用于其下所有文件），并持久化。"""
        rels = set(self._file_rels_of_items(items))
        if not rels:
            return
        if disable:
            self._state["disabled"].update(rels)
        else:
            self._state["disabled"] -= rels
        _save_todo_state(self.config.current_id(), self._state)
        for it in items:
            self._apply_state_item(it)  # 重新应用样式（目录含子树）
        self.lbl_status.setText(f"已{'禁用' if disable else '启用'} {len(rels)} 项")

    def _ignore_all(self):
        """忽略本次快照更新：把当前检测到的全部改动标记为不发布（可随时恢复）。"""
        rels = [r["rel"] for r in self._rows]
        if not rels:
            winutil.warn(self, "提示", "当前没有检测到的服务端改动，无需忽略。")
            return
        if not winutil.confirm(
                self, "忽略本次快照更新",
                f"将忽略当前检测到的 {len(rels)} 项改动（本次不发布）。\n\n"
                "已忽略的改动可随时通过「重新发布已忽略」恢复。是否继续？",
                default_yes=False):
            return
        self._state["disabled"].update(rels)
        _save_todo_state(self.config.current_id(), self._state)
        self._apply_state_recursive_top()
        self.lbl_status.setText(f"已忽略本次快照更新：{len(rels)} 项本次不发布")

    def _republish_all(self):
        """重新发布已忽略：把全部已忽略的改动恢复为可发布状态。"""
        if not self._state["disabled"]:
            winutil.warn(self, "提示", "当前没有已忽略的改动。")
            return
        count = len(self._state["disabled"])
        if not winutil.confirm(
                self, "重新发布已忽略",
                f"将恢复 {count} 项已忽略的改动为可发布状态。\n\n"
                "恢复后它们会随下一次发布一起发送给客户端。是否继续？",
                default_yes=False):
            return
        self._state["disabled"] = set()
        _save_todo_state(self.config.current_id(), self._state)
        self._apply_state_recursive_top()
        self.lbl_status.setText(f"已恢复 {count} 项改动为可发布状态")

    def _annotate_items(self, items):
        """批注选区：一次输入，应用到全部选中项（目录直接作用于目录行）。"""
        rels = [(it, (it.data(0, Qt.UserRole) or {}).get("rel", ""))
                for it in items]
        rels = [(it, r) for it, r in rels if r]
        if not rels:
            return
        existing = [self._state["notes"].get(r, "") for _, r in rels]
        default = (existing[0] if len(rels) == 1
                   else existing[0] if len(set(existing)) == 1 else "")
        text, ok = QInputDialog.getMultiLineText(
            self, "批注",
            f"为选中的 {len(rels)} 项添加说明（随本次发布下发给客户端）：", default)
        if not ok:
            return
        text = text.strip()
        for it, r in rels:
            if text:
                self._state["notes"][r] = text
            else:
                self._state["notes"].pop(r, None)
            it.setText(2, text)
        _save_todo_state(self.config.current_id(), self._state)

    # ---- 排序 / 搜索 ----
    def _sort_key(self, item):
        d = item.data(0, Qt.UserRole) or {}
        name = item.text(0).lower()
        if d.get("kind") == "dir":
            return (0, name)
        if self.cb_sort.currentData() == "status":
            return (1, _STATUS_ORDER.get(d.get("status"), 99), name)
        return (1, name)

    def _sort_children(self, item):
        children = [item.child(i) for i in range(item.childCount())]
        children.sort(key=self._sort_key)
        for c in children:
            item.removeChild(c)
            item.addChild(c)
            if (c.data(0, Qt.UserRole) or {}).get("kind") == "dir":
                self._sort_children(c)

    def _re_sort(self):
        items = [self.changes_tree.topLevelItem(i)
                 for i in range(self.changes_tree.topLevelItemCount())]
        items.sort(key=self._sort_key)
        for c in items:
            idx = self.changes_tree.indexOfTopLevelItem(c)
            if idx >= 0:
                self.changes_tree.takeTopLevelItem(idx)
            self.changes_tree.addTopLevelItem(c)
            if (c.data(0, Qt.UserRole) or {}).get("kind") == "dir":
                self._sort_children(c)
        self._apply_filter()  # 排序后保持搜索过滤结果一致

    def _apply_filter(self):
        text = self.ed_search.text().strip()
        for i in range(self.changes_tree.topLevelItemCount()):
            item = self.changes_tree.topLevelItem(i)
            if not text:
                self._set_visible_all(item, True)
            else:
                self._filter_node(item, text)

    def _filter_node(self, item, text) -> bool:
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
            TodoPage._set_visible_all(item.child(i), visible)

    # ---- 右键菜单（支持多选 / 反选后统一操作） ----
    def _on_changes_menu(self, pos):
        item = self.changes_tree.itemAt(pos)
        if item is None:
            return
        selected = self._selected_items()
        if item not in selected:  # 右键落在未选中项上 → 只作用于该项
            self.changes_tree.clearSelection()
            item.setSelected(True)
            selected = [item]
        if not selected:
            return

        file_rels = self._file_rels_of_items(selected)
        all_disabled = bool(file_rels) and all(
            r in self._state["disabled"] for r in file_rels)
        n = len(selected)
        single = n == 1
        single_dir = single and (selected[0].data(0, Qt.UserRole) or {}).get("kind") == "dir"

        menu = QMenu(self)
        if all_disabled:
            if single_dir:
                act_enable = menu.addAction("启用整目录（含子项）")
            elif single:
                act_enable = menu.addAction("启用（重新发布）")
            else:
                act_enable = menu.addAction(f"启用（{n} 项）")
        else:
            if single_dir:
                act_disable = menu.addAction("禁用整目录（含子项）")
            elif single:
                act_disable = menu.addAction("禁用（本次不发布）")
            else:
                act_disable = menu.addAction(f"禁用（{n} 项）")
        act_note = menu.addAction("批注…" if single else f"批注…（{n} 项）")
        menu.addSeparator()
        act_name = menu.addAction("复制名称" if single else f"复制名称（{n} 项）")
        act_path = menu.addAction("复制相对路径" if single else f"复制相对路径（{n} 项）")
        # 模组文件（.jar）：仅单选时提供 MC 百科链接（详情页/搜索，功能移植自 PCL CE，署名 PCL CE）
        mcmod_key = ""
        act_mc_view = act_mc_search = act_mc_copy = None
        view_url = search_url = ""
        if single and not single_dir and selected[0].text(0).lower().endswith(".jar"):
            mcmod_key = mod_search_name(selected[0].text(0))
            menu.addSeparator()
            act_mc_view, act_mc_search, act_mc_copy, view_url, search_url = \
                add_mcmod_menu_actions(menu, mcmod_key)
        act = menu.exec(self.changes_tree.viewport().mapToGlobal(pos))
        if act is None:
            return
        if all_disabled and act is act_enable:
            self._toggle_items(selected, False)
        elif not all_disabled and act is act_disable:
            self._toggle_items(selected, True)
        elif act is act_note:
            self._annotate_items(selected)
        elif act is act_name:
            QApplication.clipboard().setText(
                selected[0].text(0) if single
                else "\n".join(it.text(0) for it in selected))
        elif act is act_path:
            QApplication.clipboard().setText(
                (selected[0].data(0, Qt.UserRole) or {}).get("rel", "") if single
                else "\n".join((it.data(0, Qt.UserRole) or {}).get("rel", "")
                               for it in selected))
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

    # ---------- 发布 ----------
    def _collect_settings(self) -> dict:
        """收集要下发的客户端软件设置（来自「服务器设置」页，每台服务器独立）。"""
        ps = self.config.push_settings
        if not ps.get("enabled"):
            return {}
        return {
            "check_interval_min": int(ps.get("check_interval_min", 60)),
            "autostart": bool(ps.get("autostart", False)),
            "notify": bool(ps.get("notify", True)),
        }

    def _collect_publish_items(self) -> list[dict]:
        """收集改动树中「启用」的文件行 [{rel, status, size, old_rel?}]（禁用的跳过）。"""
        items: list[dict] = []

        def walk(item):
            d = item.data(0, Qt.UserRole) or {}
            if d.get("kind") == "file":
                if d["rel"] not in self._state["disabled"]:
                    items.append({"rel": d["rel"], "status": d["status"],
                                  "size": d.get("size", 0),
                                  "old_rel": d.get("old_rel", "")})
            else:
                for i in range(item.childCount()):
                    walk(item.child(i))

        for i in range(self.changes_tree.topLevelItemCount()):
            walk(self.changes_tree.topLevelItem(i))
        return items

    def _publish(self):
        version = self.ed_version.text().strip()
        if not version:
            winutil.warn(self, "提示", "请填写版本号。")
            return
        host = self.config.host()
        if not host:
            winutil.warn(self, "提示", "请先在「SFTP 设置」页填写服务器信息。")
            return
        items = self._collect_publish_items()
        if not items:
            if self._rows:
                winutil.warn(self, "提示",
                             "所有改动均已禁用。\n右键点击条目可重新启用后再发布。")
            else:
                winutil.warn(self, "提示", "未检测到服务端改动，无需发布。")
            return

        detail = []
        for it in items:
            rel = it["rel"]
            if it["status"] == "rename":
                line = f"改名 {it.get('old_rel', '')} → {rel}"
            else:
                line = f"安装 {rel}" if it["status"] in ("new", "update") else f"删除 {rel}"
            note = self._state["notes"].get(rel, "")
            if note:
                line += f"（{note}）"
            detail.append(line)
        settings = self._collect_settings()
        if settings:
            labels = {"check_interval_min": f"自动检查间隔={settings['check_interval_min']}分钟",
                      "autostart": f"开机自启={'开' if settings['autostart'] else '关'}",
                      "notify": f"系统通知={'开' if settings['notify'] else '关'}"}
            detail.append("客户端软件设置：" + "、".join(labels.values()))
        note = self.ed_note.text().strip()
        if note:
            detail.append(f"发布说明：{note}")
        flags = []
        if self.chk_optional.isChecked():
            flags.append("本批可选（客户端可勾选跳过）")
        if self.chk_silent.isChecked():
            flags.append("静默下载应用（后台执行）")
        if flags:
            detail.append("任务标记：" + "、".join(flags))
        for t in self._shared_tasks:
            detail.append(f"下载任务：{self._task_line(t)}")
        detail.append(f"发布时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        if not winutil.confirm_list(
                self, "确认发布",
                f"即将发布 {len(items)} 项改动 + {len(self._shared_tasks)} 个共享下载任务"
                f"到服务器 {host}，版本号：{version}。是否继续？",
                detail, ok_label="发布", cancel_label="取消"):
            return

        self.btn_publish.setEnabled(False)
        self.lbl_status.setText("正在发布…")
        worker = Worker(self._publish_worker, version, items,
                        dict(self._state["notes"]), settings, note,
                        self.chk_optional.isChecked(),
                        self.chk_silent.isChecked(),
                        list(self._shared_tasks))
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_publish_done)
        self._worker = worker
        worker.start()

    def _publish_worker(self, version, items, notes, settings,
                        note="", optional=False, silent=False,
                        shared_tasks=None, progress_cb=None):
        manifest = TodoManifest.new(version)
        manifest.note = note
        tasks = []
        for it in items:
            rel = it["rel"]
            note_desc = notes.get(rel, "")
            if it["status"] == "rename":
                # 改名 = 删除旧文件 + 安装新文件（客户端旧文件移除、新文件落地）
                old = it.get("old_rel", "")
                old_parts = old.split("/")
                old_cat = (old_parts[0] if len(old_parts) > 1
                           and old_parts[0] in CATEGORY_LABELS else "other")
                tasks.append(TaskItem(
                    action="delete", category=old_cat, target=old,
                    description=note_desc or "改名（删除旧文件）",
                    optional=optional, silent=silent))
                parts = rel.split("/")
                category = (parts[0] if len(parts) > 1
                            and parts[0] in CATEGORY_LABELS else "other")
                tasks.append(TaskItem(
                    action="install", category=category, target=rel, source=rel,
                    description=note_desc or "改名（安装新文件）",
                    optional=optional, silent=silent))
                continue
            parts = rel.split("/")
            category = parts[0] if len(parts) > 1 and parts[0] in CATEGORY_LABELS \
                else "other"
            if it["status"] in ("new", "update"):
                tasks.append(TaskItem(
                    action="install", category=category, target=rel, source=rel,
                    description=note_desc or "来自服务端改动检测",
                    optional=optional, silent=silent))
            else:
                desc = note_desc or ("旧版残留自动检测" if it["status"] == "obsolete"
                                     else "服务端已移除")
                tasks.append(TaskItem(
                    action="delete", category=category, target=rel,
                    description=desc, optional=optional, silent=silent))
        # 共享文件下载任务（action=download：客户端从 .upload_files 下载到游戏目录）
        for st in shared_tasks or []:
            tasks.append(TaskItem(
                action="download", category=st.get("category", "other"),
                target=safe_target(st.get("target", "")),
                source=safe_target(st.get("rel", "")),
                description="共享文件下载任务",
                optional=optional, silent=silent))
        manifest.tasks = tasks
        manifest.settings = dict(settings)
        total = len(tasks) + 1
        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            client_dir = self.config.files_dir
            for i, task in enumerate(tasks):
                if task.action == "install" and task.local_file:
                    remote = TodoManifest.remote_source_path(sftp, client_dir, task.source)
                    sftp.upload(task.local_file, remote)
                if progress_cb:
                    progress_cb(i + 1, total, f"处理 {task.target}")
            manifest.save_to_sftp(sftp, self.config.todo_dir)
            if progress_cb:
                progress_cb(total, total, "写入清单")
            # 更新发布基线：上一轮基线 + 本次「已发布」的改动（记录大小 + 哈希，
            # 供下次检测判断「大小相同但内容不同」与改名配对）。
            # 禁用项保持旧状态 → 下次仍会检测出来（可重新启用后再发布）。
            base = normalize_files(
                load_snapshot(self.config.current_id(), "publish").get("files", {}))
            for it in items:
                rel = it["rel"]
                if it["status"] in ("new", "update", "rename"):
                    if it["status"] == "rename" and it.get("old_rel"):
                        base.pop(it["old_rel"], None)
                    remote = TodoManifest.remote_source_path(sftp, client_dir, rel)
                    base[rel] = {"size": it["size"],
                                 "hash": hash_remote_smart(sftp, remote)}
                else:
                    base.pop(rel, None)
            saved_at = save_snapshot(self.config.current_id(), base, "publish")
        if saved_at:
            self._pub_snap_time = saved_at
        return (f"待办已发布：{manifest.version}（{len(tasks)} 项）\n"
                f"时间：{manifest.formatted_time()}\n"
                f"发布快照：{saved_at or '保存失败'}")

    def _on_progress(self, current, total, message):
        self.lbl_status.setText(fmt_progress(current, total, message, "发布进度"))

    def _on_publish_done(self, ok: bool, msg: str):
        self.btn_publish.setEnabled(True)
        if self.status_cb:
            self.status_cb(ok)
        if ok:
            self.lbl_time.setText(f"创建时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            self._refresh_pub_snap()
            winutil.info(self, "发布成功", msg)
            log.info("待办发布成功: %s", msg)
            self._shared_tasks = []
            self._update_shared_tasks_ui()
            self._auto_detect(force=True)  # 发布后立即重新检测，刷新剩余改动
        else:
            self.lbl_status.setText("发布失败 ✘")
            winutil.error(self, "发布失败", f"发布过程中发生错误：\n{msg}")

    # ---------- 共享文件下载任务（download 待办） ----------
    def _add_shared_task(self):
        if not self.config.host():
            winutil.warn(self, "提示", "请先在「SFTP 设置」页填写服务器信息。")
            return
        self.btn_add_shared.setEnabled(False)
        worker = Worker(self._load_shared_entries)
        worker.done.connect(self._on_shared_entries_loaded)
        self._worker = worker
        worker.start()

    def _load_shared_entries(self, progress_cb=None):
        c2c_dir = abs_c2c_dir(self.config.c2c_dir)
        with SFTPManager(self.config.host(), self.config.port(),
                         self.config.username(), self.config.password()) as sftp:
            return list_remote_files(sftp, c2c_dir)

    def _on_shared_entries_loaded(self, ok: bool, result):
        self.btn_add_shared.setEnabled(True)
        if not ok:
            winutil.error(self, "读取失败", f"无法读取共享文件列表：\n{result}")
            return
        dlg = _SharedDownloadDialog(result or [], self)
        if dlg.exec() == QDialog.Accepted:
            for t in dlg.selected_tasks():
                if any(x.get("rel") == t["rel"] and x.get("target") == t["target"]
                       for x in self._shared_tasks):
                    continue
                self._shared_tasks.append(t)
            self._update_shared_tasks_ui()

    def _remove_shared_task(self):
        selected = {it.text() for it in self.list_shared_tasks.selectedItems()}
        if not selected:
            winutil.warn(self, "提示", "请先在共享下载任务列表中选择要移除的任务。")
            return
        self._shared_tasks = [t for t in self._shared_tasks
                              if self._task_line(t) not in selected]
        self._update_shared_tasks_ui()

    def _task_line(self, t: dict) -> str:
        cat = CATEGORY_LABELS.get(t.get("category", ""), t.get("category", ""))
        return f"{t.get('rel', '')} → 安装到{cat}"

    def _update_shared_tasks_ui(self):
        self.list_shared_tasks.clear()
        for t in self._shared_tasks:
            self.list_shared_tasks.addItem(self._task_line(t))


class _SharedDownloadDialog(QDialog):
    """选择 .upload_files 共享文件，生成「下载安装」待办任务。加密文件不可选。"""

    def __init__(self, entries: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加共享下载任务")
        self.setMinimumSize(560, 420)
        lay = QVBoxLayout(self)
        tip = QLabel("选择共享区文件作为「下载安装」待办：客户端应用待办时会自动下载到游戏目录。\n"
                     "已加密的文件无法作为待办自动下载（需客户端输入密钥手动下载），已置灰。")
        tip.setWordWrap(True)
        tip.setObjectName("muted")
        lay.addWidget(tip)
        self.list_files = QListWidget()
        self.list_files.setSelectionMode(QListWidget.ExtendedSelection)
        for e in entries:
            line = f"{e.get('rel', '')}（{_fmt_size(e.get('size', 0))}）"
            if e.get("expired"):
                line += "  [已过期]"
            item = QListWidgetItem(line)
            item.setData(Qt.UserRole, e)
            if e.get("encrypted") or e.get("expired"):
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
                item.setToolTip("已加密 / 已过期：需客户端手动处理，不能作为待办")
            else:
                item.setToolTip("未加密，可作为待办自动下载")
            self.list_files.addItem(item)
        lay.addWidget(self.list_files, 1)
        cat_row = QHBoxLayout()
        cat_row.addWidget(QLabel("安装到游戏目录："))
        self.cb_cat = QComboBox()
        for key, label in CATEGORY_LABELS.items():
            self.cb_cat.addItem(label, key)
        cat_row.addWidget(self.cb_cat)
        cat_row.addStretch(1)
        lay.addLayout(cat_row)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("添加任务")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def selected_tasks(self) -> list[dict]:
        """返回 [{rel, target, category}]；target 保留共享文件相对结构。"""
        tasks = []
        cat = self.cb_cat.currentData() or "other"
        for item in self.list_files.selectedItems():
            e = item.data(Qt.UserRole)
            if e and not e.get("encrypted") and not e.get("expired"):
                rel = e.get("rel", "")
                tasks.append({"rel": rel, "target": f"{cat}/{rel}", "category": cat})
        return tasks


def _fmt_size(n) -> str:
    n = int(n or 0)
    if n < 1024:
        return f"{n} B"
    kb = n / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    return f"{kb / 1024:.2f} MB"
