# -*- coding: utf-8 -*-
"""客户端 - 主窗口：多服务器管理、导入配置文件、更新检查与任务应用、托盘与自启。"""
import json
import os
from datetime import datetime

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QDragEnterEvent, QDropEvent, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QProgressDialog,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app_common import winutil
from app_common.app_config import ClientConfig
from app_common.app_icon import get_app_icon
from app_common.config_manager import ConfigManagerDialog
from app_common.constants import APP_DISPLAY_NAME, APP_VERSION
from app_common.launcher import KnownVersions, parse_and_store
from app_common.log_manager import LogManagerDialog
from app_common.logger import get_logger, set_log_context
from app_common.notifications import notify
from app_common.profile import (
    PROFILE_FILTER,
    PROFILE_SUFFIX,
    client_identity,
    parse_profile_content,
)
from app_common.settings_dialog import SettingsDialog
from app_common.single_instance import _activate_msg_id
from app_common.tasks import (
    ACTION_LABELS,
    CATEGORY_LABELS,
    TodoManifest,
    manifest_signature,
)
from app_common.updater import (
    DownloadThread,
    TrayProgressDialog,
    UpdateCheckThread,
    launch_installer,
    last_prompted_version,
    parse_version,
    release_page_url,
    set_last_prompted_version,
    show_check_failed,
    show_update_result,
    track_thread,
    update_dir,
)
from app_common.version_dialog import VersionPickerDialog
from app_common.worker import Worker
from .shared_download import SharedDownloadDialog

from ..engine import (
    AUTO_SYNC_SKIP_LABELS,
    ApplyThread,
    AutoSyncThread,
    CheckAllThread,
    default_minecraft_dir,
    is_manifest_new,
    is_silent_batch,
)

log = get_logger("client.main_window")

COLUMNS = ["操作", "分类", "目标文件", "说明"]

CONN_UNKNOWN = "未检测"
CONN_OK = "连接正常"
CONN_FAIL = "连接失败"
CONN_AUTH = "未授权"


class _ServerItem(QWidget):
    """服务器列表项：显示用户可读名称 + 连接状态（暗色高区分）。"""

    def __init__(self, name: str, status: str, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(0)
        self.lbl_name = QLabel(name)
        self.lbl_name.setObjectName("list-name")
        self.lbl_status = QLabel(status)
        self.lbl_status.setObjectName("list-status")
        lay.addWidget(self.lbl_name)
        lay.addWidget(self.lbl_status)
        # 点击事件穿透到列表项，由 QListWidget 处理选中高亮
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_status(self, status: str):
        self.lbl_status.setText(status)


class ClientMainWindow(QWidget):
    def __init__(self, config: ClientConfig):
        super().__init__()
        self.config = config
        self._status: dict[str, str] = {}       # server_id -> 连接状态文本
        self._manifests: dict[str, TodoManifest] = {}
        self._c2c_manifests: dict[str, TodoManifest] = {}
        self._has_new: dict[str, bool] = {}
        self._current_sid: str | None = None
        self._threads = []
        self._update_thread = None
        self._download_thread = None
        self._download_progress = None
        self._update_manual = False
        self._auto_applying = False  # 是否正在自动应用「静默」更新
        self._auto_sync_thread = None  # 正在执行的自动同步线程
        self._auto_sync_running = False
        self._apply_busy = False  # 是否正在应用服务器待办（避免与自动同步同时写游戏目录）
        self._build()
        self._load_state()

    def nativeEvent(self, eventType, message):
        # 单实例「激活已运行窗口」：收到跨进程自定义消息 → 自行恢复显示并置前
        try:
            from ctypes import wintypes
            msg = wintypes.MSG.from_address(int(message))
        except (TypeError, ValueError):
            return super().nativeEvent(eventType, message)
        if msg.message == _activate_msg_id("client"):
            self.showNormal()
            self.raise_()
            self.activateWindow()
            return True, 0
        return super().nativeEvent(eventType, message)

    # ---------- 界面 ----------
    def _build(self):
        self.setWindowTitle(f"{APP_DISPLAY_NAME} - 客户端")
        self.setMinimumSize(QSize(900, 640))
        self.setObjectName("root")
        self.setAcceptDrops(True)
        self.setWindowIcon(get_app_icon("client"))

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(8)

        # ---- 顶部固定行：收起按钮 + 标题（位于可折叠面板之外，收起/展开时位置完全不变） ----
        top_row = QHBoxLayout()
        top_row.setSpacing(4)
        self.btn_toggle_list = QPushButton("◀")
        self.btn_toggle_list.setCheckable(True)
        self.btn_toggle_list.setChecked(True)
        self.btn_toggle_list.setFixedSize(28, 28)
        self.btn_toggle_list.setToolTip("收起服务器列表（面板变窄，右侧变宽）")
        self.btn_toggle_list.clicked.connect(self._toggle_server_list)
        top_row.addWidget(self.btn_toggle_list)
        self.lbl_title = QLabel("服务器")
        self.lbl_title.setObjectName("card-title")
        top_row.addWidget(self.lbl_title)
        top_row.addStretch(1)
        root.addLayout(top_row)

        # ---- 主体：左侧服务器列表（可折叠）+ 右侧详情 ----
        body = QHBoxLayout()
        body.setSpacing(14)
        self.left_panel = QWidget()
        pl = QVBoxLayout(self.left_panel)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(8)

        self.ed_search_servers = QLineEdit()
        self.ed_search_servers.setPlaceholderText("搜索服务器（支持 * 通配）")
        self.ed_search_servers.setClearButtonEnabled(True)
        self.ed_search_servers.textChanged.connect(lambda _: self._filter_server_list())
        pl.addWidget(self.ed_search_servers)

        self.list_servers = QListWidget()
        self.list_servers.currentItemChanged.connect(self._on_select)
        pl.addWidget(self.list_servers, 1)

        self.btn_import = QPushButton("导入配置文件…")
        self.btn_import.clicked.connect(self._import_profile)
        self.btn_remove = QPushButton("移除选中服务器")
        self.btn_remove.clicked.connect(self._remove_profile)
        pl.addWidget(self.btn_import)
        pl.addWidget(self.btn_remove)
        self._server_panel_widgets = (self.ed_search_servers, self.list_servers,
                                      self.btn_import, self.btn_remove)
        self._left_w, self._left_w_min = 210, 28
        self.left_panel.setFixedWidth(self._left_w)
        body.addWidget(self.left_panel)

        # ---- 右侧：详情（仅在已导入服务器配置时显示） ----
        self.detail_page = QWidget()
        right = QVBoxLayout(self.detail_page)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(12)

        # 标题：服务器名称 + 唯一编号
        title_row = QHBoxLayout()
        self.lbl_name = QLabel("未选择服务器")
        self.lbl_name.setObjectName("title")
        self.lbl_uid = QLabel("")
        self.lbl_uid.setObjectName("muted")
        title_row.addWidget(self.lbl_name)
        title_row.addWidget(self.lbl_uid)
        title_row.addStretch(1)
        right.addLayout(title_row)

        # 状态卡片
        card = QFrame()
        card.setObjectName("card")
        grid = QGridLayout(card)
        grid.setContentsMargins(16, 14, 16, 14)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(10)

        def _label(obj="muted"):
            lab = QLabel("—")
            lab.setObjectName(obj)
            lab.setWordWrap(True)
            return lab

        grid.addWidget(QLabel("连接状态"), 0, 0)
        self.lbl_conn = _label()
        grid.addWidget(self.lbl_conn, 0, 1)
        grid.addWidget(QLabel("服务器最新版本"), 0, 2)
        self.lbl_latest = _label()
        grid.addWidget(self.lbl_latest, 0, 3)
        grid.addWidget(QLabel("当前已应用版本"), 1, 0)
        self.lbl_applied = _label()
        grid.addWidget(self.lbl_applied, 1, 1)
        grid.addWidget(QLabel("最后应用时间"), 1, 2)
        self.lbl_applied_time = _label()
        grid.addWidget(self.lbl_applied_time, 1, 3)
        right.addWidget(card)

        # 游戏目录
        dir_box = QGroupBox("本地 Minecraft 客户端根目录（可拖入版本文件夹 / .minecraft / 启动器快捷方式）")
        dir_row = QHBoxLayout(dir_box)
        self.ed_game_dir = QLineEdit()
        self.ed_game_dir.setReadOnly(True)
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._browse_dir)
        btn_versions = QPushButton("选择已知版本…")
        btn_versions.clicked.connect(self._choose_known_version)
        dir_row.addWidget(self.ed_game_dir, 1)
        dir_row.addWidget(btn_versions)
        dir_row.addWidget(btn_browse)
        right.addWidget(dir_box)

        # 自动同步（免待办直连拉取）：服务端规定范围，玩家只控制开关与间隔
        auto_box = QGroupBox("自动同步（直接对比服务器文件并拉取，不需要服主发布待办）")
        auto_lay = QVBoxLayout(auto_box)
        auto_lay.setSpacing(6)
        auto_row = QHBoxLayout()
        self.cb_auto_sync = QCheckBox("启用自动同步")
        self.cb_auto_sync.setToolTip(
            "开启后按下面的间隔检测服务器文件仓库，与本地游戏目录对比并自动下载新增/变化的文件。\n"
            "服务器必须在「自动同步（免待办）」页下发并启用策略；\n"
            "客户端只下载、不删除本地文件，本地多出来的文件只提示。")
        self.sp_auto_interval = QSpinBox()
        self.sp_auto_interval.setRange(5, 1440)
        self.sp_auto_interval.setSuffix(" 分钟")
        self.sp_auto_interval.setToolTip("自动检测服务器文件差异的间隔（5–1440 分钟）")
        self.btn_auto_now = QPushButton("立即检测并拉取")
        self.btn_auto_now.setToolTip("立即对比服务器与本地文件，并下载差异")
        auto_row.addWidget(self.cb_auto_sync)
        auto_row.addSpacing(12)
        auto_row.addWidget(QLabel("检测间隔"))
        auto_row.addWidget(self.sp_auto_interval)
        auto_row.addSpacing(12)
        auto_row.addWidget(self.btn_auto_now)
        auto_row.addStretch(1)
        auto_lay.addLayout(auto_row)
        self.lbl_auto_status = QLabel("")
        self.lbl_auto_status.setObjectName("muted")
        self.lbl_auto_status.setWordWrap(True)
        auto_lay.addWidget(self.lbl_auto_status)
        right.addWidget(auto_box)

        # 操作按钮
        op_row = QHBoxLayout()
        self.btn_check = QPushButton("立即检查更新")
        self.btn_check.setObjectName("primary")
        self.btn_apply = QPushButton("应用更新")
        self.btn_apply.setEnabled(False)
        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("muted")
        op_row.addWidget(self.btn_check)
        op_row.addWidget(self.btn_apply)
        op_row.addStretch(1)
        op_row.addWidget(self.lbl_status)
        right.addLayout(op_row)

        # 任务表格
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionsMovable(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        right.addWidget(self.table, 1)

        # ---- 右侧外层：上方为「未导入引导页 / 服务器详情」，下方全局工具条常驻 ----
        self.right_stack = QStackedWidget()
        self.empty_page = self._build_empty_page()
        self.right_stack.addWidget(self.empty_page)
        self.right_stack.addWidget(self.detail_page)
        right_col = QVBoxLayout()
        right_col.setSpacing(12)
        right_col.addWidget(self.right_stack, 1)

        # 软件设置与数据管理
        opt_box = QGroupBox("软件设置与数据管理")
        opt_row = QHBoxLayout(opt_box)
        btn_settings = QPushButton("设置…")
        btn_settings.clicked.connect(self._open_settings)
        btn_logs = QPushButton("日志管理…")
        btn_logs.clicked.connect(lambda: LogManagerDialog(self).exec())
        btn_cfg = QPushButton("配置管理…")
        btn_cfg.clicked.connect(self._open_config_manager)
        self.btn_update = QPushButton("检查更新…")
        self.btn_update.setToolTip(f"当前版本：{APP_VERSION}\n检查 GitHub 上的软件新版本")
        self.btn_update.clicked.connect(lambda: self._check_update(manual=True))
        btn_shared = QPushButton("共享文件…")
        btn_shared.setToolTip("浏览并下载服务器的 .upload_files 临时共享文件\n（上传方可能设置下载密钥与保存时限）")
        btn_shared.clicked.connect(self._open_shared_download)
        opt_row.addWidget(btn_settings)
        opt_row.addWidget(btn_logs)
        opt_row.addWidget(btn_cfg)
        opt_row.addStretch(1)
        opt_row.addWidget(btn_shared)
        opt_row.addWidget(self.btn_update)
        right_col.addWidget(opt_box)

        body.addLayout(right_col, 3)
        root.addLayout(body, 1)

        self.btn_check.clicked.connect(lambda: self._check(manual=True))
        self.btn_apply.clicked.connect(self._apply)

        # 自动同步（免待办）：开关 / 间隔 / 立即检测
        self.cb_auto_sync.toggled.connect(self._on_auto_toggle)
        self.sp_auto_interval.valueChanged.connect(self._on_auto_interval_changed)
        self.btn_auto_now.clicked.connect(lambda: self._auto_sync(manual=True))
        self.timer_auto = QTimer(self)
        self.timer_auto.timeout.connect(lambda: self._auto_sync(manual=False))

        # 定时检查
        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self._check(manual=False))
        self.timer.start(self.config.check_interval_min * 60_000)

        # 软件本体更新：启动数秒后检查一次，之后每 6 小时自动检查（可在「设置」中关闭）
        QTimer.singleShot(5000, lambda: self._check_update(manual=False))
        self.timer_update = QTimer(self)
        self.timer_update.timeout.connect(lambda: self._check_update(manual=False))
        self.timer_update.start(6 * 60 * 60 * 1000)

        self._setup_tray()

    def _build_empty_page(self) -> QWidget:
        """未导入任何服务器配置时的引导页：右侧不再显示无意义的空详情。"""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(32, 32, 32, 32)
        lay.addStretch(1)

        title = QLabel("尚未导入服务器配置文件")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignCenter)
        hint = QLabel(
            "请先在左侧点击「导入配置文件…」选择服务器导出的 .mcscf 文件，\n"
            "或直接把 .mcscf 配置文件拖入本窗口。\n\n"
            "导入成功后会在这里显示服务器连接状态、更新版本与任务列表。")
        hint.setObjectName("muted")
        hint.setAlignment(Qt.AlignCenter)
        hint.setWordWrap(True)

        btn_import = QPushButton("导入配置文件…")
        btn_import.setObjectName("primary")
        btn_import.setFixedWidth(180)
        btn_import.clicked.connect(self._import_profile)

        lay.addWidget(title)
        lay.addSpacing(10)
        lay.addWidget(hint)
        lay.addSpacing(18)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(btn_import)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)
        lay.addStretch(1)
        return page

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(get_app_icon("client"), self)
        self.tray.setToolTip(APP_DISPLAY_NAME)
        menu = QMenu(self)
        act_show = QAction("显示主界面", self)
        act_show.triggered.connect(self._show_window)
        act_check = QAction("立即检查更新", self)
        act_check.triggered.connect(lambda: self._check(manual=True))
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self._quit)
        menu.addAction(act_show)
        menu.addAction(act_check)
        menu.addSeparator()
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    # ---------- 状态 ----------
    def _toggle_server_list(self, expanded: bool):
        """折叠 / 展开左侧服务器列表：收起时整个面板向左变窄，右侧内容区自动变宽。"""
        for w in self._server_panel_widgets:
            w.setVisible(expanded)
        self.left_panel.setFixedWidth(self._left_w if expanded else self._left_w_min)
        self.btn_toggle_list.setText("◀" if expanded else "▶")
        self.btn_toggle_list.setToolTip(
            "收起服务器列表（面板变窄，右侧变宽）" if expanded
            else "展开服务器列表（恢复宽度）")

    def _load_state(self):
        self.ed_game_dir.setText(self.config.local_mc_dir or default_minecraft_dir())
        self.cb_auto_sync.blockSignals(True)
        self.cb_auto_sync.setChecked(self.config.auto_sync_enabled)
        self.cb_auto_sync.blockSignals(False)
        self.btn_auto_now.setEnabled(self.config.auto_sync_enabled)
        self.sp_auto_interval.blockSignals(True)
        self.sp_auto_interval.setValue(self.config.auto_sync_interval_min)
        self.sp_auto_interval.blockSignals(False)
        self._restart_auto_timer()
        self.refresh_list()
        self._refresh_auto_status()
        if not self.config.profiles():
            log.info("客户端未导入任何服务器配置文件，右侧显示导入引导")

    def refresh_list(self):
        current = self._current_sid
        self.list_servers.blockSignals(True)
        self.list_servers.clear()
        profiles = self.config.profiles()
        for profile in profiles:
            sid = profile.get("server_id", "")
            status = self._status.get(sid, CONN_UNKNOWN)
            item = QListWidgetItem(self.list_servers)
            item.setData(Qt.UserRole, sid)
            widget = _ServerItem(profile.get("name", "未命名"), status)
            item.setSizeHint(widget.sizeHint())
            self.list_servers.setItemWidget(item, widget)
        self.list_servers.blockSignals(False)
        if profiles:
            if current and self.config.profile_by_id(current):
                for i in range(self.list_servers.count()):
                    if self.list_servers.item(i).data(Qt.UserRole) == current:
                        self.list_servers.setCurrentRow(i)
                        break
            else:
                self.list_servers.setCurrentRow(0)
        else:
            self._current_sid = None
            self._show_placeholder()
        self.right_stack.setCurrentWidget(self.detail_page if profiles else self.empty_page)
        self._filter_server_list()

    def _filter_server_list(self):
        """按搜索关键词过滤服务器列表（支持 * 通配，匹配名称与唯一编号）。"""
        import fnmatch

        text = self.ed_search_servers.text().strip().lower()
        for i in range(self.list_servers.count()):
            item = self.list_servers.item(i)
            sid = item.data(Qt.UserRole) or ""
            widget = self.list_servers.itemWidget(item)
            name = widget.lbl_name.text().lower() if widget is not None else ""
            hay = f"{name} {sid}"
            item.setHidden(bool(text)
                           and not fnmatch.fnmatch(hay, f"*{text}*"))

    def _show_placeholder(self):
        self.lbl_name.setText("未选择服务器")
        self.lbl_uid.setText("")
        for lab in (self.lbl_conn, self.lbl_latest, self.lbl_applied, self.lbl_applied_time):
            lab.setText("—")
        self.table.setRowCount(0)
        self.btn_apply.setEnabled(False)
        self.lbl_status.setText("请点击「导入配置文件…」接入服务器更新。")

    def _on_select(self, current, _previous):
        if current is None:
            return
        sid = current.data(Qt.UserRole)
        self._current_sid = sid
        # 每台服务器独立日志文件
        set_log_context(sid)
        self._refresh_detail()

    def _refresh_detail(self):
        sid = self._current_sid
        profile = self.config.profile_by_id(sid) if sid else None
        if profile is None:
            return
        self.lbl_name.setText(profile.get("name", "未命名"))
        self.lbl_uid.setText(f"唯一编号：{profile.get('server_id', '')}")
        self.lbl_conn.setText(self._status.get(sid, CONN_UNKNOWN))
        self.lbl_applied.setText(profile.get("last_applied_version") or "—")
        self.lbl_applied_time.setText(profile.get("last_applied_at") or "—")
        manifest = self._manifests.get(sid)
        c2c = self._c2c_manifests.get(sid)
        versions = []
        tasks = []
        if manifest is not None:
            versions.append(f"S2C {manifest.version}（{manifest.formatted_time()}）")
            tasks += list(manifest.tasks)
        if c2c is not None:
            versions.append(f"C2C {c2c.version}（{c2c.formatted_time()}）")
            tasks += list(c2c.tasks)
        if versions:
            self.lbl_latest.setText("\n".join(versions))
            self._fill_tasks(tasks)
        else:
            self.lbl_latest.setText(profile.get("last_seen_version") or "—")
            self.table.setRowCount(0)
        self.btn_apply.setEnabled(bool(self._has_new.get(sid)))
        self._refresh_auto_status()

    def _fill_tasks(self, tasks: list):
        self.table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            self.table.setItem(row, 0, QTableWidgetItem(ACTION_LABELS.get(task.action, task.action)))
            self.table.setItem(row, 1, QTableWidgetItem(CATEGORY_LABELS.get(task.category, task.category)))
            self.table.setItem(row, 2, QTableWidgetItem(task.target))
            desc = task.description or ""
            tags = task.tags
            if tags:
                desc = f"{desc}  [{'、'.join(tags)}]" if desc else f"[{'、'.join(tags)}]"
            self.table.setItem(row, 3, QTableWidgetItem(desc))

    def _update_list_status(self):
        """刷新列表中每台服务器的连接状态（仅更新状态小字，名称保持用户可读名称）。"""
        for i in range(self.list_servers.count()):
            item = self.list_servers.item(i)
            sid = item.data(Qt.UserRole)
            status = self._status.get(sid, CONN_UNKNOWN)
            widget = self.list_servers.itemWidget(item)
            if widget is not None:
                widget.set_status(status)

    # ---------- 服务器档案管理 ----------
    def _apply_import(self, content: str, name: str, sid: str, created_at: str,
                      client_id: str = "") -> None:
        """写入服务器档案并选中该服务器（界面状态同步）。"""
        self.config.add_profile(name, sid, content, created_at, client_id)
        self._status.pop(sid, None)
        self._manifests.pop(sid, None)
        self._c2c_manifests.pop(sid, None)
        self._has_new.pop(sid, None)
        self.refresh_list()
        self._current_sid = sid
        for i in range(self.list_servers.count()):
            if self.list_servers.item(i).data(Qt.UserRole) == sid:
                self.list_servers.setCurrentRow(i)
                break
        self._refresh_detail()

    def _import_profile(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入服务器配置文件", "", PROFILE_FILTER)
        if not path:
            log.info("导入服务器配置文件：用户取消了文件选择")
            return
        log.info("开始导入服务器配置文件: %s", path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            data = parse_profile_content(content)
        except Exception as exc:
            log.error("导入服务器配置文件失败 %s: %s", path, exc, exc_info=True)
            winutil.error(self, "导入失败",
                          f"无法导入该配置文件：\n{path}\n\n原因：{exc}")
            return
        name, sid = data["name"], data["server_id"]
        cid = data.get("client_id") or sid
        existing = self.config.profile_by_id(sid)
        if existing is not None:
            if not winutil.confirm(
                    self, "确认覆盖",
                    f"服务器「{existing.get('name')}」已存在（唯一编号 {sid}）。\n\n"
                    f"导入的配置文件：\n{path}\n\n"
                    f"导入后将覆盖该服务器的现有配置，是否覆盖？",
                    default_yes=False):
                log.info("导入服务器配置文件：用户取消覆盖「%s」(%s)", name, sid)
                return
        try:
            self._apply_import(content, name, sid, data.get("created_at", ""), cid)
        except Exception as exc:
            log.error("保存服务器档案失败 %s: %s", path, exc, exc_info=True)
            winutil.error(self, "导入失败",
                          f"配置文件解析成功，但保存服务器档案失败：\n{exc}")
            return
        log.info("导入服务器档案成功: %s（服务器编号 %s，客户端编号 %s）", name, sid, cid)
        winutil.info(self, "导入成功",
                     f"已接入服务器：{name}\n客户端编号：{cid}\n\n点击「立即检查更新」验证连接。")

    def _confirm_import_dropped(self, paths: list[str]):
        """拖入的 .mcscf 服务器配置文件：逐份确认后自动导入；已存在同编号服务器时提示覆盖。"""
        if not paths:
            return
        log.info("拖入服务器配置文件 %d 份: %s", len(paths), "、".join(paths))
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                data = parse_profile_content(content)
            except Exception as exc:
                log.error("拖入的服务器配置文件解析失败 %s: %s", path, exc, exc_info=True)
                winutil.error(self, "导入失败",
                              f"无法导入该配置文件：\n{path}\n\n原因：{exc}")
                continue
            name, sid = data["name"], data["server_id"]
            cid = data.get("client_id") or sid
            existing = self.config.profile_by_id(sid)
            if existing is not None:
                if not winutil.confirm(
                        self, "确认覆盖",
                        f"服务器「{existing.get('name')}」已存在（服务器编号 {sid}）。\n\n"
                        f"拖入的配置文件：\n{path}\n\n"
                        f"导入后将覆盖该服务器的现有配置，是否覆盖？",
                        default_yes=False):
                    log.info("拖入服务器配置文件：用户取消覆盖「%s」(%s)", name, sid)
                    continue
            elif not winutil.confirm(
                    self, "确认导入服务器",
                    f"检测到拖入的服务器配置文件：\n{path}\n\n"
                    f"服务器名称：{name}\n客户端编号：{cid}\n\n是否导入并接入该服务器？"):
                log.info("拖入服务器配置文件：用户取消导入「%s」(%s)", name, sid)
                continue
            try:
                self._apply_import(content, name, sid, data.get("created_at", ""), cid)
            except Exception as exc:
                log.error("保存拖入的服务器档案失败 %s: %s", path, exc, exc_info=True)
                winutil.error(self, "导入失败",
                              f"配置文件解析成功，但保存服务器档案失败：\n{exc}")
                continue
            log.info("通过拖入导入服务器档案成功: %s（服务器编号 %s，客户端编号 %s）",
                     name, sid, cid)
            winutil.info(self, "导入成功",
                         f"已接入服务器：{name}\n客户端编号：{cid}\n\n点击「立即检查更新」验证连接。")

    def _remove_profile(self):
        sid = self._current_sid
        if not sid:
            winutil.warn(self, "提示", "请先选择一个服务器。")
            return
        profile = self.config.profile_by_id(sid)
        if not winutil.confirm(self, "确认操作",
                               f"确定移除服务器「{profile.get('name')}」吗？\n移除后将不再接收该服务器的更新。", default_yes=False):
            return
        self.config.remove_profile(sid)
        self._status.pop(sid, None)
        self._manifests.pop(sid, None)
        self._c2c_manifests.pop(sid, None)
        self._has_new.pop(sid, None)
        self._current_sid = None
        self.refresh_list()

    # ---------- 共享文件下载 ----------
    def _open_shared_download(self):
        sid = self._current_sid
        profile = self.config.profile_by_id(sid) if sid else None
        if profile is None:
            winutil.warn(self, "提示", "请先在列表中选择一台服务器，再打开共享文件。")
            return
        dlg = SharedDownloadDialog(self.config, profile, self)
        dlg.exec()

    # ---------- 更新检查 ----------
    def _check(self, manual: bool = False):
        if not self.config.profiles():
            if manual:
                winutil.warn(self, "提示", "尚未导入任何服务器配置文件。")
            return
        if not self.config.local_mc_dir or not self.ed_game_dir.text():
            if manual:
                winutil.warn(self, "提示", "请先选择客户端根目录。")
            return
        self.config.local_mc_dir = self.ed_game_dir.text()
        self.btn_check.setEnabled(False)
        if manual:
            self.lbl_status.setText("正在检查所有服务器…")
        thread = CheckAllThread(self.config)
        thread.done.connect(lambda ok, results, err: self._on_check_done(ok, results, err, manual))
        thread.finished.connect(thread.deleteLater)
        self._threads.append(thread)
        thread.start()

    def _on_check_done(self, ok: bool, results: dict, err: str, manual: bool):
        self.btn_check.setEnabled(True)
        if not ok:
            self.lbl_status.setText("检查失败 ✘")
            log.error("批量检查失败: %s", err)
            if manual:
                winutil.error(self, "检查失败", str(err))
            return

        new_count = 0
        for sid, result in results.items():
            if result.get("ok"):
                manifest = result.get("manifest")
                c2c = result.get("c2c_manifest")
                self._manifests[sid] = manifest
                self._c2c_manifests[sid] = c2c
                profile = self.config.profile_by_id(sid) or {}
                is_new = bool(is_manifest_new(profile, manifest, c2c))
                self._has_new[sid] = is_new
                self._status[sid] = CONN_OK
                if is_new:
                    new_count += 1
                    # 多次更新未应用提醒：每发现一次未应用累计，达阈值时托盘提示
                    n = self.config.bump_unapplied(sid)
                    if n in (3, 5, 10):
                        self.tray.showMessage(
                            APP_DISPLAY_NAME,
                            f"「{profile.get('name', sid)}」已有 {n} 次更新未应用，"
                            f"请尽快检查并应用更新。",
                            QSystemTrayIcon.Information, 5000)
            else:
                self._status[sid] = CONN_AUTH if result.get("error", "").startswith("授权:") else CONN_FAIL
                self._has_new[sid] = False
        self._update_list_status()
        self._refresh_detail()

        if manual:
            profile = self.config.profile_by_id(self._current_sid) if self._current_sid else None
            if profile:
                sid = self._current_sid
                res = results.get(sid)
                if res and not res.get("ok"):
                    error_text = res.get("error", "")
                    if error_text.startswith("授权:"):
                        winutil.error(self, "授权未通过", error_text[3:])
                    else:
                        winutil.error(self, "检查失败", f"无法连接服务器「{profile.get('name')}」：\n{error_text}")
                elif self._has_new.get(sid):
                    manifest = self._manifests.get(sid)
                    c2c = self._c2c_manifests.get(sid)
                    lines = [f"服务器「{profile.get('name')}」"]
                    if manifest:
                        lines += [f"S2C 版本号：{manifest.version}",
                                  f"S2C 发布时间：{manifest.formatted_time()}",
                                  f"S2C 待办任务：{len(manifest.tasks)} 项"]
                    if c2c:
                        lines += [f"C2C 版本号：{c2c.version}",
                                  f"C2C 发布时间：{c2c.formatted_time()}",
                                  f"C2C 待办任务：{len(c2c.tasks)} 项"]
                    winutil.info(self, "发现新更新", "\n".join(lines))
                else:
                    self.lbl_status.setText("检查完成 ✔")
            if new_count == 0:
                self.lbl_status.setText("所有服务器均为最新 ✔")
            else:
                self.lbl_status.setText(f"发现 {new_count} 台服务器有新更新 ✔")
        elif new_count:
            # 服务端把整批待办标记为「静默」时，客户端在后台自动应用，不打扰玩家
            if self._auto_apply_if_possible(results):
                pass
            elif self.config.notify:
                names = []
                for sid in self._manifests:
                    if self._has_new.get(sid):
                        p = self.config.profile_by_id(sid)
                        if p:
                            names.append(p.get("name", ""))
                notify(f"{APP_DISPLAY_NAME} - 发现新更新",
                       "、".join(names) + " 有待应用的更新，请打开客户端查看。", self.tray)

    # ---------- 自动同步（免待办直连拉取） ----------
    def _restart_auto_timer(self):
        """按本地开关与间隔启动/停止自动同步定时器。"""
        if not hasattr(self, "timer_auto"):
            return
        if self.config.auto_sync_enabled:
            self.timer_auto.start(self.config.auto_sync_interval_min * 60_000)
        else:
            self.timer_auto.stop()

    def _on_auto_toggle(self, checked: bool):
        self.config.auto_sync_enabled = bool(checked)
        self.btn_auto_now.setEnabled(bool(checked))
        self._restart_auto_timer()
        if not checked:
            self._refresh_auto_status()
            return
        self.lbl_auto_status.setText(
            f"自动同步已开启（每 {self.config.auto_sync_interval_min} 分钟检测）。"
            f"正在检测服务器文件差异…")
        # 打开开关后立刻检测一次，让玩家马上看到效果
        QTimer.singleShot(
            800, lambda: (self._auto_sync(manual=True)
                          if self.config.auto_sync_enabled else None))

    def _on_auto_interval_changed(self, value: int):
        self.config.auto_sync_interval_min = int(value)
        self._restart_auto_timer()
        self._refresh_auto_status()

    def _refresh_auto_status(self):
        if self._auto_sync_running:
            return
        if not self.config.auto_sync_enabled:
            self.lbl_auto_status.setText(
                "自动同步已关闭。打开后按间隔检测服务器文件并自动下载差异"
                "（服务器需先启用自动同步策略）。")
            return
        sid = self._current_sid
        profile = self.config.profile_by_id(sid) if sid else None
        interval = self.config.auto_sync_interval_min
        if profile is None:
            self.lbl_auto_status.setText(
                f"自动同步已开启（每 {interval} 分钟检测），请先选择服务器。")
            return
        at = profile.get("auto_sync_at") or ""
        files = int(profile.get("auto_sync_files") or 0)
        if at:
            self.lbl_auto_status.setText(
                f"自动同步已开启（每 {interval} 分钟检测）。"
                f"上次同步：{at}，{files} 个文件。")
        else:
            self.lbl_auto_status.setText(
                f"自动同步已开启（每 {interval} 分钟检测）。尚未执行过自动同步。")

    def _auto_sync(self, manual: bool = False):
        if self._auto_sync_thread is not None:
            return
        if not manual and not self.config.auto_sync_enabled:
            return
        if not self.config.profiles():
            if manual:
                winutil.warn(self, "提示", "尚未导入任何服务器配置文件。")
            return
        if not self.config.local_mc_dir or not self.ed_game_dir.text():
            if manual:
                winutil.warn(self, "提示", "请先选择客户端根目录。")
            return
        if self._apply_busy:
            if manual:
                winutil.warn(self, "请稍候",
                             "正在应用服务器待办更新，等更新完成后再检测自动同步。")
            return
        sid = self._current_sid or self.config.profiles()[0].get("server_id")
        profile = self.config.profile_by_id(sid)
        if profile is None:
            return
        self.config.local_mc_dir = self.ed_game_dir.text()
        self._auto_sync_running = True
        self.btn_auto_now.setEnabled(False)
        if manual:
            self.lbl_auto_status.setText("正在检测服务器与本地文件差异…")
        thread = AutoSyncThread(self.config, profile, apply=True)
        thread.progress.connect(self._on_auto_sync_progress)
        thread.done.connect(
            lambda ok, result, err: self._on_auto_sync_done(
                ok, result, err, profile, manual))
        thread.finished.connect(thread.deleteLater)
        self._auto_sync_thread = thread
        self._threads.append(thread)
        thread.start()

    def _on_auto_sync_progress(self, cur: int, total: int, msg: str):
        if msg:
            self.lbl_auto_status.setText(msg)

    def _on_auto_sync_done(self, ok: bool, result, err: str,
                           profile, manual: bool):
        thread = self._auto_sync_thread
        self._auto_sync_thread = None
        if thread is not None:
            try:
                self._threads.remove(thread)
            except ValueError:
                pass
        self._auto_sync_running = False
        self.btn_auto_now.setEnabled(self.config.auto_sync_enabled)
        name = profile.get("name", "")
        sid = profile.get("server_id", "")
        if not ok:
            log.error("自动同步失败: %s（%s）", name, err)
            self.lbl_auto_status.setText(f"自动同步失败 ✘：{err}")
            if manual:
                if str(err).startswith("授权:"):
                    winutil.error(self, "授权未通过", str(err)[3:])
                else:
                    winutil.error(self, "自动同步失败",
                                  f"无法完成自动同步：\n{err}")
            elif self.config.notify:
                notify(f"{APP_DISPLAY_NAME} - 自动同步失败",
                       f"「{name}」自动同步失败，请打开客户端查看日志。",
                       self.tray)
            return
        result = result or {}
        skipped = result.get("skipped") or ""
        if skipped:
            label = AUTO_SYNC_SKIP_LABELS.get(skipped, skipped)
            hint = {
                "policy_missing":
                    "请让服主在服务端工具打开「自动同步（免待办）」页并保存策略。",
                "policy_disabled":
                    "请让服主在服务端工具把自动同步总开关打开。",
                "policy_empty":
                    "服务端策略的白名单为空，未允许任何目录。",
            }.get(skipped, "")
            self.lbl_auto_status.setText(label + ("。" + hint if hint else ""))
            if manual:
                winutil.info(self, "暂未同步",
                             label + (f"\n\n{hint}" if hint else ""))
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        installed = list(result.get("installed") or [])
        downloads = list(result.get("downloads") or [])
        errors = list(result.get("errors") or [])
        extras = list(result.get("extras") or [])
        remote_files = int(result.get("remote_files") or 0)
        downloaded_bytes = int(result.get("bytes") or 0)
        total_bytes = int(result.get("total_bytes") or 0)
        self.config.mark_auto_sync(sid, now, len(installed),
                                   "ok" if not errors else "partial")
        if not downloads:
            self.lbl_auto_status.setText(
                f"自动同步：已是最新（对比 {remote_files} 个文件，{now}）。")
        elif errors:
            self.lbl_auto_status.setText(
                f"自动同步：下载 {len(installed)}/{len(downloads)} 个文件，"
                f"失败 {len(errors)} 个（{now}）。")
        else:
            self.lbl_auto_status.setText(
                f"自动同步：已下载 {len(installed)} 个文件"
                f"（{_fmt_size(downloaded_bytes)}，{now}）。")
        if result.get("warnings"):
            log.warning("自动同步提示: %s",
                        "；".join(result["warnings"][:5]))
        if manual:
            lines = [
                f"服务器：{name}",
                f"检测时间：{now}",
                f"服务器范围内文件：{remote_files} 个",
                f"本次需下载：{len(downloads)} 个（{_fmt_size(total_bytes)}）",
                f"已下载：{len(installed)} 个（{_fmt_size(downloaded_bytes)}）",
                f"本地多余（不会删除）：{len(extras)} 个",
            ]
            if errors:
                lines.append(f"失败：{len(errors)} 个")
                lines += errors
            if extras:
                lines.append("")
                lines.append("本地多余文件（服务器上没有，未自动删除）：")
                lines += [f"  · {item['rel']}" for item in extras[:50]]
                if len(extras) > 50:
                    lines.append(f"  … 其余 {len(extras) - 50} 个")
            title = "自动同步：已是最新" if not downloads else "自动同步完成"
            winutil.info_list(self, title, lines)
        elif installed and self.config.notify:
            notify(f"{APP_DISPLAY_NAME} - 自动同步",
                   f"「{name}」已自动下载 {len(installed)} 个文件。",
                   self.tray)
        log.info("自动同步完成: %s - 下载 %d/%d 个文件（%d 个失败）",
                 name, len(installed), len(downloads), len(errors))

    # ---------- 应用更新 ----------
    def _apply(self):
        if self._auto_sync_running:
            winutil.warn(self, "请稍候",
                         "正在自动同步服务器文件，完成后再应用待办更新。")
            return
        sid = self._current_sid
        if not sid or not self._has_new.get(sid):
            return
        profile = self.config.profile_by_id(sid)
        manifest = self._manifests.get(sid)
        c2c = self._c2c_manifests.get(sid)
        if profile is None or (manifest is None and c2c is None):
            return
        lines = []
        if manifest:
            installs = [t for t in manifest.tasks if t.action == "install"]
            downloads = [t for t in manifest.tasks if t.action == "download"]
            deletes = [t for t in manifest.tasks if t.action == "delete"]
            if installs:
                lines.append("【S2C 将下载并安装/替换】")
                lines += [f"  · {t.target}{_task_tags(t)}" for t in installs]
            if downloads:
                lines.append("【S2C 将从共享区下载安装】")
                lines += [f"  · {t.target}{_task_tags(t)}" for t in downloads]
            if deletes:
                lines.append("【S2C 将删除】")
                lines += [f"  · {t.target}{_task_tags(t)}" for t in deletes]
            if manifest.note:
                lines.append(f"【发布说明】{manifest.note}")
            settings_text = manifest.settings_summary()
            if settings_text:
                lines.append("【客户端软件设置（随更新下发）】")
                lines += [f"  · {s.strip()}" for s in settings_text.split("、")]
        if c2c:
            c2c_installs = [t for t in c2c.tasks if t.action == "install"]
            c2c_downloads = [t for t in c2c.tasks if t.action == "download"]
            c2c_deletes = [t for t in c2c.tasks if t.action == "delete"]
            if c2c_installs:
                lines.append("【C2C 将下载并安装/替换（本地对本地发布）】")
                lines += [f"  · {t.target}{_task_tags(t)}" for t in c2c_installs]
            if c2c_downloads:
                lines.append("【C2C 将从共享区下载安装】")
                lines += [f"  · {t.target}{_task_tags(t)}" for t in c2c_downloads]
            if c2c_deletes:
                lines.append("【C2C 将删除】")
                lines += [f"  · {t.target}{_task_tags(t)}" for t in c2c_deletes]
            if c2c.note:
                lines.append(f"【C2C 发布说明】{c2c.note}")
        if not lines:
            lines = ["（无任务）"]
        head = f"服务器「{profile.get('name')}」即将应用更新。"
        if manifest:
            head += f"\nS2C 版本 {manifest.version}（{manifest.formatted_time()}）"
        if c2c:
            head += f"\nC2C 版本 {c2c.version}（{c2c.formatted_time()}）"
        if not winutil.confirm_list(
                self, "确认应用更新", head + "\n是否继续？",
                lines, ok_label="应用更新", cancel_label="取消"):
            return

        # 可选任务：弹窗让用户勾选（默认全选，取消勾选 = 跳过）
        optional_tasks = [t for t in ((manifest.tasks if manifest else [])
                                      + (c2c.tasks if c2c else [])) if t.optional]
        skip_ids: set[str] = set()
        if optional_tasks:
            dlg = _OptionalTasksDialog(optional_tasks, self)
            if dlg.exec() != QDialog.Accepted:
                return
            skip_ids = dlg.skipped_ids()

        self._start_apply(profile, manifest, c2c, skip_ids, auto=False)

    def _start_apply(self, profile, manifest, c2c, skip_ids: set[str],
                     auto: bool = False):
        """启动应用线程；auto=True 表示静默自动应用（不弹进度/结果对话框）。"""
        self._apply_busy = True
        total = len(manifest.tasks) if manifest else 0
        total += len(c2c.tasks) if c2c else 0
        progress = None
        if not auto:
            progress = QProgressDialog("正在应用更新…", None, 0, max(total, 1), self)
            progress.setWindowTitle("应用更新")
            progress.setWindowModality(Qt.WindowModal)
            progress.setMinimumDuration(0)
            progress.setAutoClose(False)

        thread = ApplyThread(self.config, profile, manifest, c2c, skip_ids)
        if progress is not None:
            thread.progress.connect(lambda cur, total, msg: (
                progress.setValue(cur),
                progress.setLabelText(f"[{cur}/{total}] {msg}"),
            ))
        thread.done.connect(lambda ok, summary, err: self._on_apply_done(
            ok, summary, err, profile, manifest, c2c, progress, auto))
        thread.finished.connect(thread.deleteLater)
        self._threads.append(thread)
        thread.start()

    def _auto_apply_if_possible(self, results: dict) -> bool:
        """整批待办都被服务端标记为「静默」且没有可选任务时，后台自动应用。

        只在定时检查（非手动）时尝试，且一次只自动应用一台服务器，
        避免多台服务器同时写同一个游戏目录。返回是否已开始自动应用。
        """
        if self._auto_applying:
            return True
        candidates = []
        for sid, result in results.items():
            if not result.get("ok"):
                continue
            manifest = result.get("manifest")
            c2c = result.get("c2c_manifest")
            profile = self.config.profile_by_id(sid)
            if profile is None or not is_manifest_new(profile, manifest, c2c):
                continue
            # 有任一任务不是静默、或带可选（需要玩家选择）→ 不自动应用
            if not is_silent_batch(manifest, c2c):
                continue
            candidates.append((sid, profile, manifest, c2c))
        if len(candidates) != 1:
            return False
        sid, profile, manifest, c2c = candidates[0]
        self._auto_applying = True
        name = profile.get("name", sid)
        self.lbl_status.setText(f"检测到「{name}」的静默更新，正在后台自动应用…")
        log.info("静默更新自动应用开始: %s", name)
        self._start_apply(profile, manifest, c2c, set(), auto=True)
        return True

    def _on_apply_done(self, ok: bool, summary, err: str, profile, manifest,
                       c2c, progress, auto: bool = False):
        self._auto_applying = False
        self._apply_busy = False
        if progress is not None:
            progress.close()
        if not ok:
            self.lbl_status.setText("应用更新失败 ✘")
            if auto:
                log.error("静默更新自动应用失败: %s（%s）", profile.get("name"), err)
                if self.config.notify:
                    notify(f"{APP_DISPLAY_NAME} - 自动更新失败",
                           f"「{profile.get('name')}」更新失败，请打开客户端查看日志。",
                           self.tray)
            else:
                winutil.error(self, "应用失败", f"应用更新过程中发生错误：\n{err}")
            return
        sid = profile.get("server_id")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if manifest is not None:
            self.config.mark_applied(sid, manifest.version, now,
                                     sig=manifest_signature(manifest))
        if c2c is not None:
            self.config.mark_applied_c2c(sid, c2c.version)
        self.config.reset_unapplied(sid)
        self._has_new[sid] = False
        self._status[sid] = CONN_OK
        if manifest is not None:
            self._apply_settings_ui(manifest.settings or {})
        self._update_list_status()
        self._refresh_detail()
        versions = []
        if manifest is not None:
            versions.append(f"S2C {manifest.version}")
        if c2c is not None:
            versions.append(f"C2C {c2c.version}")
        lines = [
            f"服务器：{profile.get('name')}",
            f"已应用版本：{'、'.join(versions)}",
            f"时间：{now}",
            f"安装/替换：{len(summary['installed'])} 项",
            f"删除：{len(summary['deleted'])} 项",
            f"跳过：{len(summary['skipped'])} 项",
            f"失败：{len(summary['errors'])} 项",
        ]
        if summary.get("settings"):
            lines.append("客户端软件设置：" + "、".join(summary["settings"]))
        if summary["errors"]:
            lines.append("")
            lines.append("失败详情：")
            lines += summary["errors"]
        self.lbl_status.setText(f"已应用版本 {'、'.join(versions)} ✔")
        if self.config.notify:
            notify(f"{APP_DISPLAY_NAME} - 更新完成",
                   f"已应用版本 {'、'.join(versions)}", self.tray)
        if not auto:
            winutil.info_list(self, "更新完成", lines)
        log.info("应用更新完成: %s - %s%s", profile.get("name"),
                 "、".join(versions), "（静默自动）" if auto else "")

    # ---------- 设置 ----------
    def _browse_dir(self):
        path = QFileDialog.getExistingDirectory(
            self, "选择客户端根目录（含 .minecraft）", self.ed_game_dir.text())
        if path:
            self._set_game_dir(path)

    def _set_game_dir(self, path: str):
        self.ed_game_dir.setText(path)
        self.config.local_mc_dir = path

    # ---------- 拖拽 / 已知版本 ----------
    def dragEnterEvent(self, event: QDragEnterEvent):
        if self._dropped_paths(event):
            event.acceptProposedAction()

    def dragMoveEvent(self, event: QDragEnterEvent):
        if self._dropped_paths(event):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        paths = self._dropped_paths(event)
        if paths:
            self._handle_dropped(paths)
            event.acceptProposedAction()

    @staticmethod
    def _dropped_paths(event) -> list[str]:
        urls = event.mimeData().urls()
        out = []
        for url in urls:
            if url.isLocalFile():
                out.append(url.toLocalFile())
        return out

    @staticmethod
    def _looks_like_profile_file(path: str) -> bool:
        """内容嗅探：扩展名被改过的配置文件也能识别（只读文件头部 4KB）。"""
        try:
            if not os.path.isfile(path):
                return False
            with open(path, "rb") as f:
                head = f.read(4096)
        except OSError:
            return False
        return b"mc_cloud_sync_profile" in head

    def _handle_dropped(self, paths):
        """后台线程解析拖入内容（避免拖入 .minecraft / 启动器快捷方式时卡顿界面）。"""
        if isinstance(paths, str):
            paths = [paths]
        if paths:
            log.info("拖入 %d 个本地路径: %s", len(paths), "、".join(paths))
        # 拖入服务器配置文件（.mcscf）→ 确认后自动导入（无需后台解析）
        profile_paths = [p for p in paths
                         if os.path.splitext(p)[1].lower() == PROFILE_SUFFIX]
        # 扩展名被改过的配置文件：按内容嗅探识别，避免拖入后毫无反应
        profile_paths += [p for p in paths
                          if p not in profile_paths
                          and self._looks_like_profile_file(p)]
        if profile_paths:
            self._confirm_import_dropped([os.path.abspath(p) for p in profile_paths])
            return
        self.lbl_status.setText("正在解析拖入内容…")
        worker = Worker(parse_and_store, [os.path.abspath(p) for p in paths])
        worker.done.connect(self._on_drop_parsed)
        self._drop_worker = worker
        worker.start()

    def _on_drop_parsed(self, ok: bool, msg: str):
        self.lbl_status.setText("")
        if not ok:
            winutil.error(self, "解析失败", str(msg))
            return
        try:
            data = json.loads(msg)
        except Exception:
            data = {}
        version_dirs = data.get("version_dirs") or []
        mc_roots = data.get("mc_roots") or []

        if version_dirs and len(version_dirs) == 1:
            # 单个版本/整合包文件夹 → 直接设为客户端根目录
            self._set_game_dir(version_dirs[0])
            winutil.info(self, "已填充",
                         f"已将版本目录设为客户端根目录：\n{version_dirs[0]}")
            return

        # .minecraft / 启动器 / versions 文件夹 / 多个版本目录 → 识别到的版本全部
        # 写入已知版本库，只提示导入了什么（不弹选择；需要时用「选择已知版本…」）。
        lines = []
        if version_dirs:
            lines.append(f"已导入 {len(version_dirs)} 个版本文件夹。")
        if mc_roots:
            lines.append("已识别 Minecraft 根目录：")
            lines += [f"  · {r}" for r in mc_roots]
        if data.get("added"):
            lines.append(f"共新增 {data['added']} 个版本到已知版本库。")
        if not lines:
            lines = ["未识别到可用的版本内容。"]
        self.lbl_status.setText("已导入到已知版本库")
        winutil.info(self, "已导入",
                     "\n".join(lines) + "\n\n可在「选择已知版本…」中选用。")

    def _choose_known_version(self):
        dlg = VersionPickerDialog(self, KnownVersions(), title="选择已知版本",
                                  hint="从 versions 下选择一个版本文件夹作为客户端根目录。")
        if dlg.exec() == VersionPickerDialog.Accepted:
            entry = dlg.selected_entry()
            if entry:
                self._set_game_dir(entry["version_dir"])
                winutil.info(self, "已填充",
                             f"已选择版本：{entry.get('version')}\n"
                             f"客户端根目录：{entry.get('version_dir')}")

    # ---------- 软件设置 ----------
    def _open_settings(self):
        """打开软件本体设置子窗口（主题、开机自启、自动检查、系统通知）。"""
        dlg = SettingsDialog(self, config=self.config, is_client=True,
                             on_changed=self._on_settings_changed)
        dlg.exec()

    def _on_settings_changed(self, interval_changed: bool):
        """设置保存后的回调：检查间隔变化时重启定时器。"""
        if interval_changed:
            self.timer.start(self.config.check_interval_min * 60_000)
        self.lbl_status.setText("软件设置已保存。")

    # ---------- 软件本体更新（GitHub / Gitee） ----------
    def _check_update(self, manual: bool = True):
        """检查软件本体更新。manual=False 为定时/启动静默检查（尊重设置开关）。"""
        if not manual and not self.config.auto_update_check:
            return
        if self._update_thread is not None and self._update_thread.isRunning():
            return
        self._update_manual = manual
        self._update_thread = UpdateCheckThread(self)
        self._update_thread.done.connect(self._on_update_checked)
        self._update_thread.finished.connect(self._update_thread.deleteLater)
        self._update_thread.start()

    def _on_update_checked(self, ok: bool, result, err: str):
        if not ok:
            if self._update_manual and show_check_failed(self, err):
                self._check_update(manual=True)
            return
        latest = (result or {}).get("latest")
        releases = (result or {}).get("releases") or []
        checked_at = (result or {}).get("checked_at") or ""
        has_update = False
        if latest is not None and latest.setup_url:
            new_ver = parse_version(latest.tag)
            cur_ver = parse_version(APP_VERSION)
            has_update = bool(new_ver and cur_ver and new_ver > cur_ver)
        if not has_update:
            # 手动检查 → 展示结果（当前版本 / 检查时间 / 版本历史）；自动检查 → 静默
            if self._update_manual:
                show_update_result(self, latest, releases, checked_at,
                                   has_update=False)
            return
        # 静默自动检查：已提示过该版本则不再打扰
        if not self._update_manual and last_prompted_version() == latest.tag:
            return
        if not show_update_result(self, latest, releases, checked_at,
                                  has_update=True):
            return
        set_last_prompted_version(latest.tag)
        self._start_download(latest)

    def _start_download(self, latest):
        dest = update_dir() / latest.setup_name
        progress = TrayProgressDialog(
            "下载安装程序", "正在后台下载安装程序…", "取消",
            0, max(int(latest.setup_size) or 1, 1),
            tray=self.tray, parent=self)
        self._download_progress = progress

        thread = DownloadThread(latest.setup_url, str(dest), self)
        progress.canceled.connect(thread.cancel)
        thread.progress.connect(lambda cur, total, msg: (
            progress.setRange(0, max(total or 1, 1)),
            progress.setValue(cur),
            progress.setLabelText(f"{msg}（{latest.setup_name}）"),
        ))
        thread.done.connect(lambda ok, path, err: self._on_update_downloaded(
            ok, path, err, progress, latest))
        track_thread(self, "_download_thread", thread)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _on_update_downloaded(self, ok: bool, path: str, err: str, progress, latest):
        progress.close()
        self._download_progress = None
        if not ok:
            winutil.error(self, "下载失败",
                          f"安装程序下载失败（已尝试 Gitee 与 GitHub 直连）：\n{err}\n\n"
                          f"可手动下载安装包：\n{release_page_url()}")
            return
        # 完整性兜底校验：下载文件大小与远程声明不一致 → 视为损坏，提示重新下载
        try:
            actual = os.path.getsize(path)
        except OSError:
            actual = 0
        if latest.setup_size and actual and actual != latest.setup_size:
            winutil.error(
                self, "安装包损坏",
                f"下载的安装程序不完整（{actual / 1048576:.1f} MB，"
                f"应为 {latest.setup_size / 1048576:.1f} MB）。\n\n"
                f"请删除后重新下载，或手动下载安装包：\n{release_page_url()}")
            try:
                os.remove(path)
            except OSError:
                pass
            return
        # 下载完成 → 直接关闭本程序并启动安装程序（不再弹出确认）
        try:
            launch_installer(path)
        except Exception as exc:
            winutil.error(self, "启动失败", f"无法启动安装程序：\n{exc}")
            return
        QApplication.quit()

    # ---------- 配置/日志管理 ----------
    def _open_config_manager(self):
        dlg = ConfigManagerDialog(self, store=self.config.store,
                                  title="客户端配置管理",
                                  on_restored=self._on_config_restored,
                                  note="配置包括：服务器列表、游戏目录、更新间隔等。\n"
                                       "如需清空全部数据可删除服务器后再导出，不建议直接编辑文件。")
        dlg.exec()

    def _on_config_restored(self):
        """配置恢复后刷新界面（部分设置重启后完全生效）。"""
        self._status.clear()
        self._manifests.clear()
        self._c2c_manifests.clear()
        self._has_new.clear()
        self._current_sid = None
        self._load_state()
        self.timer.start(self.config.check_interval_min * 60_000)
        self.lbl_status.setText("配置已恢复，界面已刷新。")

    def _apply_settings_ui(self, settings: dict):
        """服务端下发的软件设置已由引擎写入配置，这里仅同步界面状态。"""
        if not settings:
            return
        if "check_interval_min" in settings:
            self.timer.start(self.config.check_interval_min * 60_000)
        self.lbl_status.setText("已应用服务端下发的软件设置。")

    # ---------- 托盘/退出 ----------
    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason):
        if reason not in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            return
        # 若下载进度窗被最小化隐藏到托盘，点击托盘优先恢复它
        progress = getattr(self, "_download_progress", None)
        if (progress is not None and progress.is_background_hidden()
                and self._download_thread is not None
                and self._download_thread.isRunning()):
            progress.showNormal()
            progress.raise_()
            progress.activateWindow()
            return
        self._show_window()

    def closeEvent(self, event):
        event.ignore()
        self.hide()
        self.tray.showMessage(APP_DISPLAY_NAME, "客户端已最小化到托盘，双击图标恢复。",
                              QSystemTrayIcon.Information, 3000)

    def _quit(self):
        if not winutil.confirm(self, "确认退出", "确定退出客户端吗？退出后停止自动同步。", default_yes=False):
            return
        self.tray.hide()
        QApplication.quit()


def _fmt_size(num: int) -> str:
    """字节数格式化（界面提示用）。"""
    size = float(max(0, int(num or 0)))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _task_tags(task) -> str:
    """任务标签文本：可选/静默。"""
    tags = task.tags
    return f"（{'、'.join(tags)}）" if tags else ""


class _OptionalTasksDialog(QDialog):
    """选择要应用的可选任务（默认全部勾选，取消勾选 = 跳过）。"""

    def __init__(self, tasks: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("可选任务（可跳过）")
        self.setMinimumSize(520, 400)
        lay = QVBoxLayout(self)
        tip = QLabel("以下任务被标记为「可选」待办：取消勾选将跳过该任务。")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        self._checks: list[tuple] = []
        for t in tasks:
            text = t.summary()
            tags = t.tags
            if tags:
                text = f"{text}  （{'、'.join(tags)}）"
            cb = QCheckBox(text)
            cb.setChecked(True)
            self._checks.append((t, cb))
            lay.addWidget(cb)
        lay.addStretch(1)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("应用勾选的任务")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def skipped_ids(self) -> set[str]:
        return {t.id for t, cb in self._checks if not cb.isChecked()}
