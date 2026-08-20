# -*- coding: utf-8 -*-
"""客户端 - 主窗口：多服务器管理、导入配置文件、更新检查与任务应用、托盘与自启。"""
from datetime import datetime

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QDragEnterEvent, QDropEvent, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
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
from app_common.constants import APP_DISPLAY_NAME
from app_common.launcher import KnownVersions, resolve_dropped
from app_common.log_manager import LogManagerDialog
from app_common.logger import get_logger
from app_common.notifications import notify
from app_common.profile import PROFILE_FILTER, parse_profile_content
from app_common.settings_dialog import SettingsDialog
from app_common.tasks import ACTION_LABELS, CATEGORY_LABELS, TodoManifest
from app_common.version_dialog import VersionPickerDialog

from ..engine import (
    ApplyThread,
    CheckAllThread,
    default_minecraft_dir,
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
        self._has_new: dict[str, bool] = {}
        self._current_sid: str | None = None
        self._threads = []
        self._build()
        self._load_state()

    # ---------- 界面 ----------
    def _build(self):
        self.setWindowTitle(f"{APP_DISPLAY_NAME} - 客户端")
        self.setMinimumSize(QSize(900, 640))
        self.setObjectName("root")
        self.setAcceptDrops(True)
        self.setWindowIcon(get_app_icon("client"))

        root = QHBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(14)

        # ---- 左侧：服务器列表 ----
        left = QVBoxLayout()
        left.setSpacing(8)
        left_title = QLabel("服务器")
        left_title.setObjectName("card-title")
        left.addWidget(left_title)

        self.list_servers = QListWidget()
        self.list_servers.currentItemChanged.connect(self._on_select)
        left.addWidget(self.list_servers, 1)

        self.btn_import = QPushButton("导入配置文件…")
        self.btn_import.clicked.connect(self._import_profile)
        self.btn_remove = QPushButton("移除选中服务器")
        self.btn_remove.clicked.connect(self._remove_profile)
        left.addWidget(self.btn_import)
        left.addWidget(self.btn_remove)
        root.addLayout(left, 1)

        # ---- 右侧：详情 ----
        right = QVBoxLayout()
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
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        right.addWidget(self.table, 1)

        # 软件设置与数据管理
        opt_box = QGroupBox("软件设置与数据管理")
        opt_row = QHBoxLayout(opt_box)
        btn_settings = QPushButton("设置…")
        btn_settings.clicked.connect(self._open_settings)
        btn_logs = QPushButton("日志管理…")
        btn_logs.clicked.connect(lambda: LogManagerDialog(self).exec())
        btn_cfg = QPushButton("配置管理…")
        btn_cfg.clicked.connect(self._open_config_manager)
        opt_row.addWidget(btn_settings)
        opt_row.addWidget(btn_logs)
        opt_row.addWidget(btn_cfg)
        opt_row.addStretch(1)
        right.addWidget(opt_box)

        root.addLayout(right, 3)

        self.btn_check.clicked.connect(lambda: self._check(manual=True))
        self.btn_apply.clicked.connect(self._apply)

        # 定时检查
        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self._check(manual=False))
        self.timer.start(self.config.check_interval_min * 60_000)

        self._setup_tray()

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
    def _load_state(self):
        self.ed_game_dir.setText(self.config.local_mc_dir or default_minecraft_dir())
        self.refresh_list()

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
        self.lbl_latest.setText(profile.get("last_seen_version") or "—")
        manifest = self._manifests.get(sid)
        if manifest is not None:
            self.lbl_latest.setText(f"{manifest.version}（{manifest.formatted_time()}）")
            self._fill_table(manifest)
        else:
            self.table.setRowCount(0)
        self.btn_apply.setEnabled(bool(self._has_new.get(sid)))

    def _fill_table(self, manifest: TodoManifest):
        self.table.setRowCount(len(manifest.tasks))
        for row, task in enumerate(manifest.tasks):
            self.table.setItem(row, 0, QTableWidgetItem(ACTION_LABELS.get(task.action, task.action)))
            self.table.setItem(row, 1, QTableWidgetItem(CATEGORY_LABELS.get(task.category, task.category)))
            self.table.setItem(row, 2, QTableWidgetItem(task.target))
            self.table.setItem(row, 3, QTableWidgetItem(task.description))

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
    def _import_profile(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入服务器配置文件", "", PROFILE_FILTER)
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            data = parse_profile_content(content)
        except Exception as exc:
            winutil.error(self, "导入失败", f"无法导入该配置文件：\n{exc}")
            return
        name, sid = data["name"], data["server_id"]
        self.config.add_profile(name, sid, content, data.get("created_at", ""))
        self._status.pop(sid, None)
        self._manifests.pop(sid, None)
        self._has_new.pop(sid, None)
        self.refresh_list()
        self._current_sid = sid
        for i in range(self.list_servers.count()):
            if self.list_servers.item(i).data(Qt.UserRole) == sid:
                self.list_servers.setCurrentRow(i)
                break
        winutil.info(self, "导入成功",
                     f"已接入服务器：{name}\n唯一编号：{sid}\n\n点击「立即检查更新」验证连接。")
        log.info("导入服务器档案: %s", name)

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
        self._has_new.pop(sid, None)
        self._current_sid = None
        self.refresh_list()

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
                self._manifests[sid] = manifest
                is_new = manifest is not None and manifest.version != \
                    self.config.profile_by_id(sid).get("last_applied_version", "")
                self._has_new[sid] = is_new
                self._status[sid] = CONN_OK
                if is_new:
                    new_count += 1
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
                    if manifest:
                        winutil.info(self, "发现新更新",
                                     f"服务器「{profile.get('name')}」\n"
                                     f"版本号：{manifest.version}\n"
                                     f"发布时间：{manifest.formatted_time()}\n"
                                     f"共 {len(manifest.tasks)} 个待办任务。")
                else:
                    self.lbl_status.setText("检查完成 ✔")
            if new_count == 0:
                self.lbl_status.setText("所有服务器均为最新 ✔")
            else:
                self.lbl_status.setText(f"发现 {new_count} 台服务器有新更新 ✔")
        elif new_count and self.config.notify:
            names = []
            for sid in self._manifests:
                if self._has_new.get(sid):
                    p = self.config.profile_by_id(sid)
                    if p:
                        names.append(p.get("name", ""))
            notify(f"{APP_DISPLAY_NAME} - 发现新更新",
                   "、".join(names) + " 有待应用的更新，请打开客户端查看。", self.tray)

    # ---------- 应用更新 ----------
    def _apply(self):
        sid = self._current_sid
        if not sid or not self._has_new.get(sid):
            return
        profile = self.config.profile_by_id(sid)
        manifest = self._manifests.get(sid)
        if profile is None or manifest is None:
            return
        installs = [t for t in manifest.tasks if t.action == "install"]
        deletes = [t for t in manifest.tasks if t.action == "delete"]
        lines = []
        if installs:
            lines.append("【将下载并安装/替换】")
            lines += [f"  · {t.target}" for t in installs]
        if deletes:
            lines.append("【将删除】")
            lines += [f"  · {t.target}" for t in deletes]
        settings_text = manifest.settings_summary()
        if settings_text:
            lines.append("【客户端软件设置（随更新下发）】")
            lines += [f"  · {s.strip()}" for s in settings_text.split("、")]
        if not lines:
            lines = ["（无任务）"]
        detail = "\n".join(lines)
        if not winutil.confirm(
                self, "确认应用更新",
                f"服务器「{profile.get('name')}」即将应用版本 {manifest.version}（{manifest.formatted_time()}）：\n\n"
                f"{detail}\n\n是否继续？"):
            return

        progress = QProgressDialog("正在应用更新…", None, 0, max(len(manifest.tasks), 1), self)
        progress.setWindowTitle("应用更新")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)

        thread = ApplyThread(self.config, profile, manifest)
        thread.progress.connect(lambda cur, total, msg: (
            progress.setValue(cur),
            progress.setLabelText(f"[{cur}/{total}] {msg}"),
        ))
        thread.done.connect(lambda ok, summary, err: self._on_apply_done(
            ok, summary, err, profile, manifest, progress))
        thread.finished.connect(thread.deleteLater)
        self._threads.append(thread)
        thread.start()

    def _on_apply_done(self, ok: bool, summary, err: str, profile, manifest, progress):
        progress.close()
        if not ok:
            winutil.error(self, "应用失败", f"应用更新过程中发生错误：\n{err}")
            return
        sid = profile.get("server_id")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.config.mark_applied(sid, manifest.version, now)
        self._has_new[sid] = False
        self._status[sid] = CONN_OK
        self._apply_settings_ui(manifest.settings or {})
        self._update_list_status()
        self._refresh_detail()
        detail = (
            f"服务器：{profile.get('name')}\n"
            f"已应用版本：{manifest.version}\n"
            f"时间：{now}\n"
            f"安装/替换：{len(summary['installed'])} 项\n"
            f"删除：{len(summary['deleted'])} 项\n"
            f"跳过：{len(summary['skipped'])} 项\n"
            f"失败：{len(summary['errors'])} 项"
        )
        if summary.get("settings"):
            detail += "\n客户端软件设置：" + "、".join(summary["settings"])
        if summary["errors"]:
            detail += "\n\n失败详情：\n" + "\n".join(summary["errors"])
        self.lbl_status.setText(f"已应用版本 {manifest.version} ✔")
        if self.config.notify:
            notify(f"{APP_DISPLAY_NAME} - 更新完成", detail.split("\n\n")[0], self.tray)
        winutil.info(self, "更新完成", detail)
        log.info("应用更新完成: %s - %s", profile.get("name"), manifest.version)

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
        if self._dropped_path(event) is not None:
            event.acceptProposedAction()

    def dragMoveEvent(self, event: QDragEnterEvent):
        if self._dropped_path(event) is not None:
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        path = self._dropped_path(event)
        if path:
            self._handle_dropped(path)
            event.acceptProposedAction()

    @staticmethod
    def _dropped_path(event) -> str | None:
        urls = event.mimeData().urls()
        for url in urls:
            if url.isLocalFile():
                return url.toLocalFile()
        return None

    def _handle_dropped(self, path: str):
        info = resolve_dropped(path)
        if not info:
            winutil.warn(self, "无法识别",
                         "无法识别拖入的内容。\n\n支持：\n"
                         "· versions 下的版本/整合包文件夹（内含 mods 等）\n"
                         "· .minecraft 文件夹\n"
                         "· 启动器快捷方式(.lnk) 或启动器程序")
            return
        known = KnownVersions()
        if info["kind"] == "version_dir":
            known.add_version_dir(info["version_dir"])
            self._set_game_dir(info["version_dir"])
            winutil.info(self, "已填充",
                         f"已将版本目录设为客户端根目录：\n{info['version_dir']}")
            return
        if info.get("mc_root"):
            added = known.add_from_mc_root(info["mc_root"])
            hint = (f"已识别 Minecraft 根目录：{info['mc_root']}"
                    + (f"\n新增 {added} 个版本。" if added else ""))
            dlg = VersionPickerDialog(self, known, title="选择版本目录", hint=hint)
            if dlg.exec() == VersionPickerDialog.Accepted:
                entry = dlg.selected_entry()
                if entry:
                    self._set_game_dir(entry["version_dir"])
                    winutil.info(self, "已填充",
                                 f"已选择版本：{entry.get('version')}\n"
                                 f"客户端根目录：{entry.get('version_dir')}")
            else:
                self.lbl_status.setText("已保存到已知版本库，可通过「选择已知版本…」随时选用。")

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
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
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
