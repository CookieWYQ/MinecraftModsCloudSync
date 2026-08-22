# -*- coding: utf-8 -*-
"""服务端 - 主窗口：左侧服务器列表（多服务器管理）+ 标签页 + 系统托盘 + 窗口状态持久化。"""
import json
import os

from PySide6.QtCore import QItemSelection, QItemSelectionModel, QSize, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QProgressDialog,
    QPushButton,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app_common import winutil
from app_common.app_icon import get_app_icon
from app_common.constants import APP_DISPLAY_NAME, APP_VERSION
from app_common.launcher import parse_and_store
from app_common.logger import get_logger, set_log_context
from app_common.settings_dialog import SettingsDialog
from app_common.updater import (
    DownloadThread,
    UpdateCheckThread,
    launch_installer,
    last_prompted_version,
    parse_version,
    release_page_url,
    set_last_prompted_version,
    show_check_failed,
    show_update_result,
    update_dir,
)
from app_common.worker import Worker

from .export_page import ExportPage
from .log_page import LogPage
from .remote_page import RemoteFilePage
from .server_settings_page import ServerSettingsPage
from .todo_page import TodoPage

log = get_logger("server.main_window")


class _ServerItem(QWidget):
    """服务器列表项：名称 + 本地客户端目录（两行小字）。"""

    def __init__(self, name: str, path: str, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(0)
        self.lbl_name = QLabel(name)
        self.lbl_name.setObjectName("list-name")
        self.lbl_path = QLabel(path or "（未选择本地目录）")
        self.lbl_path.setObjectName("list-status")
        lay.addWidget(self.lbl_name)
        lay.addWidget(self.lbl_path)
        # 点击事件穿透到列表项，由 QListWidget 处理选中高亮
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_data(self, name: str, path: str):
        self.lbl_name.setText(name)
        self.lbl_path.setText(path or "（未选择本地目录）")


class _ServerList(QListWidget):
    """服务器列表：Ctrl/Shift 多选（同 Windows 资源管理器），Ctrl+Shift+A 反选。"""

    def keyPressEvent(self, event):
        if (event.key() == Qt.Key_A
                and event.modifiers() == (Qt.ControlModifier | Qt.ShiftModifier)):
            self._invert_selection()
            event.accept()
            return
        super().keyPressEvent(event)

    def _invert_selection(self):
        model = self.model()
        sel_rows = {i for i in range(self.count()) if self.item(i).isSelected()}
        selection = QItemSelection()
        for i in range(self.count()):
            if i not in sel_rows:
                idx = model.index(i, 0)
                selection.select(idx, idx)
        self.clearSelection()
        self.selectionModel().select(selection, QItemSelectionModel.Select)


class ServerMainWindow(QMainWindow):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self._update_thread = None
        self._download_thread = None
        self._update_manual = False
        self._build()
        self._restore_state()

    def _build(self):
        self.setWindowTitle(f"{APP_DISPLAY_NAME} - 服务端工具")
        self.setMinimumSize(QSize(960, 640))
        self.setAcceptDrops(True)
        self.setWindowIcon(get_app_icon("server"))

        # 根布局：左侧服务器列表 + 右侧标签页
        root = QWidget()
        root.setObjectName("root")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(8)

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
        root_layout.addLayout(top_row)

        # ---- 主体：左侧服务器列表（可折叠）+ 右侧标签页 ----
        hbox = QHBoxLayout()
        hbox.setSpacing(12)
        self.left_panel = QWidget()
        pl = QVBoxLayout(self.left_panel)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(8)

        self.ed_search_servers = QLineEdit()
        self.ed_search_servers.setPlaceholderText("搜索服务器（支持 * 通配）")
        self.ed_search_servers.setClearButtonEnabled(True)
        self.ed_search_servers.textChanged.connect(lambda _: self._filter_server_list())
        pl.addWidget(self.ed_search_servers)

        self.list_servers = _ServerList()
        self.list_servers.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list_servers.currentItemChanged.connect(self._on_server_changed)
        self.list_servers.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_servers.customContextMenuRequested.connect(self._on_server_list_menu)
        pl.addWidget(self.list_servers, 1)

        btn_add = QPushButton("添加服务器…")
        btn_add.clicked.connect(self._add_server)
        pl.addWidget(btn_add)
        self._server_panel_widgets = (self.ed_search_servers, self.list_servers, btn_add)
        self._left_w, self._left_w_min = 200, 28
        self.left_panel.setFixedWidth(self._left_w)
        hbox.addWidget(self.left_panel)

        # ---- 右侧：标签页 ----
        self.tabs = QTabWidget()
        self.todo_page = TodoPage(self.config, status_cb=self.set_sftp_status)
        self.remote_page = RemoteFilePage(self.config, status_cb=self.set_sftp_status)
        self.export_page = ExportPage(self.config, status_cb=self.set_sftp_status)
        self.settings_page = ServerSettingsPage(self.config, main_window=self)
        self.log_page = LogPage(self.config)
        self.settings_page.saved.connect(self._on_settings_saved)
        self.tabs.addTab(self.remote_page, "更新服务端（C2S）")
        self.tabs.addTab(self.todo_page, "发布待办（S2C）")
        self.tabs.addTab(self.export_page, "导出客户端配置")
        self.tabs.addTab(self.settings_page, "服务器设置")
        self.tabs.addTab(self.log_page, "日志")
        hbox.addWidget(self.tabs, 4)
        root_layout.addLayout(hbox, 1)
        self.setCentralWidget(root)

        self.statusBar().showMessage("就绪")
        self.lbl_sftp_status = QLabel("SFTP：未检测")
        self.statusBar().addPermanentWidget(self.lbl_sftp_status)

        # 软件本体设置（主题等），与服务端档案配置区分
        btn_settings = QPushButton("设置…")
        btn_settings.clicked.connect(self._open_settings)
        self.statusBar().addPermanentWidget(btn_settings)

        # 软件本体更新（GitHub）
        self.btn_update = QPushButton("检查更新…")
        self.btn_update.setToolTip(f"当前版本：{APP_VERSION}\n检查 GitHub 上的软件新版本")
        self.btn_update.clicked.connect(lambda: self._check_update(manual=True))
        self.statusBar().addPermanentWidget(self.btn_update)

        self._refresh_servers()

        # 启动后自动检测当前服务器的 SFTP 连接（等窗口就绪后执行）
        QTimer.singleShot(600, self._auto_check_sftp)

        # 软件本体更新：启动数秒后检查一次，之后每 6 小时自动检查（可在「设置」中关闭）
        QTimer.singleShot(5000, lambda: self._check_update(manual=False))
        self.timer_update = QTimer(self)
        self.timer_update.timeout.connect(lambda: self._check_update(manual=False))
        self.timer_update.start(6 * 60 * 60 * 1000)

        # 托盘
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(get_app_icon("server"), self)
            self.tray.setToolTip(APP_DISPLAY_NAME)
            menu = QMenu()
            act_show = QAction("显示主界面", self)
            act_show.triggered.connect(self._show_window)
            act_quit = QAction("退出", self)
            act_quit.triggered.connect(self._quit)
            menu.addAction(act_show)
            menu.addSeparator()
            menu.addAction(act_quit)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self._on_tray_activated)
            self.tray.show()

    # ---------- 服务器列表 ----------
    def _toggle_server_list(self, expanded: bool):
        """折叠 / 展开左侧服务器列表：收起时整个面板向左变窄，右侧内容区自动变宽。"""
        for w in self._server_panel_widgets:
            w.setVisible(expanded)
        self.left_panel.setFixedWidth(self._left_w if expanded else self._left_w_min)
        self.btn_toggle_list.setText("◀" if expanded else "▶")
        self.btn_toggle_list.setToolTip(
            "收起服务器列表（面板变窄，右侧变宽）" if expanded
            else "展开服务器列表（恢复宽度）")

    def _refresh_servers(self):
        """按配置重建服务器列表，保持当前选中项。"""
        current = self.config.current_id()
        self.list_servers.blockSignals(True)
        self.list_servers.clear()
        for s in self.config.servers:
            sid = s.get("id", "")
            item = QListWidgetItem(self.list_servers)
            item.setData(Qt.UserRole, sid)
            widget = _ServerItem(s.get("name", "未命名"), s.get("local_mc_dir", ""))
            item.setSizeHint(widget.sizeHint())
            self.list_servers.setItemWidget(item, widget)
        self.list_servers.blockSignals(False)
        if self.list_servers.count():
            for i in range(self.list_servers.count()):
                if self.list_servers.item(i).data(Qt.UserRole) == current:
                    self.list_servers.setCurrentRow(i)
                    break
            else:
                self.list_servers.setCurrentRow(0)
        self._filter_server_list()

    def _filter_server_list(self):
        """按搜索关键词过滤服务器列表（支持 * 通配，匹配名称与本地目录）。"""
        import fnmatch

        text = self.ed_search_servers.text().strip().lower()
        for i in range(self.list_servers.count()):
            item = self.list_servers.item(i)
            widget = self.list_servers.itemWidget(item)
            name = widget.lbl_name.text().lower() if widget is not None else ""
            path = widget.lbl_path.text().lower() if widget is not None else ""
            hay = f"{name} {path}"
            item.setHidden(bool(text)
                           and not fnmatch.fnmatch(hay, f"*{text}*"))

    def _current_server_id(self) -> str | None:
        item = self.list_servers.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _on_server_changed(self, current, _previous):
        if current is None:
            return
        sid = current.data(Qt.UserRole)
        if sid == self.config.current_id():
            return  # 仅列表刷新（如重命名服务器），服务器未变化，不重复读取服务端
        self.config.set_current(sid)
        # 每台服务器独立日志文件
        set_log_context(sid)
        self._reload_pages()

    def _reload_pages(self):
        """切换服务器后刷新各数据页面。"""
        try:
            self.todo_page.reload()
        except Exception as exc:
            log.warning("刷新发布待办页失败: %s", exc)
        try:
            self.remote_page.reload()
        except Exception as exc:
            log.warning("刷新更新服务端页失败: %s", exc)
        try:
            self.export_page.reload()
        except Exception as exc:
            log.warning("刷新导出页失败: %s", exc)
        try:
            self.settings_page.reload()
        except Exception as exc:
            log.warning("刷新服务器设置页失败: %s", exc)
        try:
            self.log_page.reload()
        except Exception as exc:
            log.warning("刷新日志页失败: %s", exc)
        # 切换服务器后自动检测其 SFTP 连接（状态栏反馈，不弹窗）
        try:
            self.settings_page.auto_check()
        except Exception as exc:
            log.warning("自动检测 SFTP 失败: %s", exc)

    def _on_settings_saved(self):
        """服务器设置保存后，刷新两个工作流页面使其立即生效。"""
        try:
            self.remote_page.reload()
        except Exception as exc:
            log.warning("设置保存后刷新更新服务端页失败: %s", exc)
        try:
            self.todo_page.reload()
        except Exception as exc:
            log.warning("设置保存后刷新发布待办页失败: %s", exc)

    def _add_server(self):
        path = QFileDialog.getExistingDirectory(
            self, "选择客户端根目录（.minecraft 或版本文件夹）")
        if path:
            self._append_server(path, announce=True)

    def _rename_server(self):
        sid = self._current_server_id()
        if not sid:
            winutil.warn(self, "提示", "请先选择一个服务器。")
            return
        server = self.config.server_by_id(sid)
        if server is None:
            return
        name, ok = QInputDialog.getText(self, "重命名服务器", "服务器名称：",
                                        text=server.get("name", ""))
        if ok and name.strip():
            self.config.rename_server(sid, name.strip())
            self._refresh_servers()

    def _selected_servers(self) -> list[QListWidgetItem]:
        sm = self.list_servers.selectionModel()
        model = self.list_servers.model()
        return [self.list_servers.item(i)
                for i in range(self.list_servers.count())
                if sm.isSelected(model.index(i, 0))]

    def _on_server_list_menu(self, pos):
        """服务器列表右键菜单：重命名 / 移除选中 / 添加服务器。

        重命名作用于右键悬停项；移除作用于当前选中项（右键未选中项时只移除悬停项）。
        """
        item = self.list_servers.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        act_rename = menu.addAction("重命名")
        act_remove = menu.addAction("移除选中…")
        menu.addSeparator()
        act_add = menu.addAction("添加服务器…")
        act = menu.exec(self.list_servers.viewport().mapToGlobal(pos))
        if act is None:
            return
        if act is act_rename:
            self.list_servers.setCurrentItem(item)
            self._rename_server()
        elif act is act_remove:
            if item not in self._selected_servers():
                self.list_servers.setCurrentItem(item)
            self._remove_server()
        elif act is act_add:
            self._add_server()

    def _remove_server(self):
        items = self._selected_servers()
        if not items:
            winutil.warn(self, "提示",
                         "请先选择要移除的服务器。\n\n"
                         "· 按住 Ctrl 点击：单独多选\n"
                         "· 按住 Shift 点击：连续多选\n"
                         "· Ctrl+Shift+A：反选")
            return
        names = []
        for item in items:
            sid = item.data(Qt.UserRole)
            server = self.config.server_by_id(sid)
            names.append(server.get("name", "未命名") if server else "?")
        if len(items) == 1:
            if not winutil.confirm(
                    self, "确认操作",
                    f"确定移除服务器「{names[0]}」吗？\n其 SFTP 与本地目录配置将一并删除。"):
                return
        else:
            # 批量移除：条目在可滚动列表中展示，避免弹窗超高按不到按钮
            if not winutil.confirm_list(
                    self, "确认操作",
                    f"确定移除选中的 {len(items)} 个服务器吗？\n"
                    f"其 SFTP 与本地目录配置将一并删除。", names):
                return
        for item in items:
            self.config.remove_server(item.data(Qt.UserRole))
        self._refresh_servers()
        self._reload_pages()

    def set_sftp_status(self, ok: bool):
        """更新状态栏中的 SFTP 连通状态。"""
        self.lbl_sftp_status.setText(f"SFTP：{'已连通' if ok else '连接失败'}")

    def _auto_check_sftp(self):
        """开启软件时自动检测当前服务器的 SFTP 连接；未填写信息时要求先到设置页填写。"""
        if not self.config.host():
            self.lbl_sftp_status.setText("SFTP：未配置")
            winutil.warn(self, "SFTP 未配置",
                         "尚未填写 SFTP 服务器连接信息。\n\n"
                         "请先到「服务器设置」页填写服务器地址、用户名与密码，\n"
                         "再点击「保存设置」完成配置。")
            self.tabs.setCurrentWidget(self.settings_page)
            return
        self.lbl_sftp_status.setText("SFTP：正在检测…")
        self.settings_page.auto_check()

    def _open_settings(self):
        """打开软件本体设置子窗口（主题等）。"""
        dlg = SettingsDialog(self, config=self.config, is_client=False)
        dlg.exec()

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
        progress = QProgressDialog("正在后台下载安装程序…", "取消", 0,
                                   max(int(latest.setup_size) or 1, 1), self)
        progress.setWindowTitle("下载安装程序")
        progress.setWindowModality(Qt.NonModal)  # 后台静默下载：不阻塞界面操作
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)

        thread = DownloadThread(latest.setup_url, str(dest), self)
        progress.canceled.connect(thread.cancel)
        thread.progress.connect(lambda cur, total, msg: (
            progress.setRange(0, max(total or 1, 1)),
            progress.setValue(cur),
            progress.setLabelText(f"{msg}（{latest.setup_name}）"),
        ))
        thread.done.connect(lambda ok, path, err: self._on_update_downloaded(
            ok, path, err, progress, latest))
        thread.finished.connect(thread.deleteLater)
        self._download_thread = thread
        thread.start()

    def _on_update_downloaded(self, ok: bool, path: str, err: str, progress, latest):
        progress.close()
        if not ok:
            winutil.error(self, "下载失败",
                          f"安装程序下载失败（已尝试直连与多个加速镜像）：\n{err}\n\n"
                          f"可手动下载安装包：\n{release_page_url()}")
            return
        if not winutil.confirm(
                self, "下载完成",
                f"安装程序已下载：\n{path}\n\n"
                f"即将启动安装程序并关闭本程序。\n"
                f"请在弹出的安装向导中完成安装。"):
            return
        try:
            launch_installer(path)
        except Exception as exc:
            winutil.error(self, "启动失败", f"无法启动安装程序：\n{exc}")
            return
        QApplication.quit()

    # ---------- 拖拽（后台解析，版本入库备选，用户选择后加入服务器列表） ----------
    def dragEnterEvent(self, event):
        if self._dropped_paths(event):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if self._dropped_paths(event):
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = self._dropped_paths(event)
        if paths:
            self._append_server(paths, announce=True)
            event.acceptProposedAction()

    @staticmethod
    def _dropped_paths(event) -> list[str]:
        urls = event.mimeData().urls()
        out = []
        for url in urls:
            if url.isLocalFile():
                out.append(url.toLocalFile())
        return out

    def _append_server(self, paths, announce: bool = False):
        """把拖入/选择的路径追加为服务器（后台线程解析，拖入 .minecraft 不卡界面）。

        - 独立版本文件夹 → 直接加入服务器列表（同路径去重，重名标注绝对路径）
        - .minecraft / 启动器(PCL2/PCL CE/HMCL 等) → 识别到的版本全部写入已知版本库
          （仅提示导入结果，不弹选择；需要时在「服务器设置」页用「选择已知版本…」）。
        """
        if isinstance(paths, str):
            paths = [paths]
        self.list_servers.setEnabled(False)
        self.statusBar().showMessage("正在解析拖入内容…")
        worker = Worker(parse_and_store, [os.path.abspath(p) for p in paths])
        worker.done.connect(lambda ok, msg: self._on_drop_parsed(ok, msg, announce))
        self._drop_worker = worker
        worker.start()

    def _on_drop_parsed(self, ok: bool, msg: str, announce: bool):
        self.list_servers.setEnabled(True)
        self.statusBar().showMessage("就绪")
        if not ok:
            if announce:
                winutil.error(self, "解析失败", str(msg))
            return
        try:
            data = json.loads(msg)
        except Exception:
            data = {}
        version_dirs = data.get("version_dirs") or []
        mc_roots = data.get("mc_roots") or []

        if version_dirs:
            # 独立版本/整合包文件夹 → 直接加为服务器条目
            if len(version_dirs) == 1:
                self._add_one_server(version_dirs[0], announce=True)
                self._reload_pages()
            else:
                added = sum(1 for vd in version_dirs
                            if self._add_one_server(vd, announce=False))
                self._refresh_servers()
                self._reload_pages()
                if announce:
                    winutil.info(self, "已添加服务器", f"已添加 {added} 个服务器条目。")
            return

        if mc_roots:
            # .minecraft / 启动器 / versions 文件夹 → 识别到的版本全部写入已知版本库，
            # 只提示导入了什么（不弹选择；需要时在「服务器设置」页用「选择已知版本…」）。
            lines = ["已识别 Minecraft 根目录："]
            lines += [f"  · {r}" for r in mc_roots]
            added = data.get("added") or 0
            if added:
                lines.append(f"共识别并保存 {added} 个版本。")
            self.statusBar().showMessage("已导入到已知版本库")
            if announce:
                winutil.info(self, "已导入",
                             "\n".join(lines) + "\n\n可在「服务器设置」页的「选择已知版本…」中选用。")
            return

        if announce:
            winutil.warn(self, "无法识别",
                         "无法识别拖入的内容。\n\n支持：\n"
                         "· versions 下的版本/整合包文件夹（内含 mods 等）\n"
                         "· .minecraft 文件夹\n"
                         "· 启动器快捷方式(.lnk) 或启动器程序")

    def _add_one_server(self, target_dir: str, announce: bool) -> bool:
        """追加单个目录为服务器（去重 / 重名标注）。返回是否新增。"""
        target_dir = os.path.normpath(target_dir)
        # 去重：已存在同一本地目录的服务器 → 直接选中它
        for s in self.config.servers:
            if s.get("local_mc_dir") and os.path.normpath(s["local_mc_dir"]) == target_dir:
                self.config.set_current(s.get("id", ""))
                self._refresh_servers()
                self.tabs.setCurrentWidget(self.remote_page)
                if announce:
                    winutil.info(self, "已选中",
                                 f"该目录已存在，已切换到服务器「{s.get('name')}」。")
                return False
        # 追加为新服务器（名称 = 版本文件夹名，重名时标注绝对路径）
        name = os.path.basename(target_dir.rstrip("/\\")) or "默认服务器"
        if any(s.get("name") == name for s in self.config.servers):
            name = f"{name}（{target_dir}）"
        self.config.add_server(name, target_dir)
        self._refresh_servers()
        self.tabs.setCurrentWidget(self.remote_page)
        log.info("追加服务器: %s -> %s", name, target_dir)
        if announce:
            winutil.info(self, "已添加服务器",
                         f"服务器：{name}\n本地目录：{target_dir}\n\n"
                         f"可在「服务器设置」页配置该服务器的连接信息。")
        return True

    # ---------- 窗口状态 ----------
    def _restore_state(self):
        win = self.config.window
        if win.get("width"):
            self.resize(win.get("width", 980), win.get("height", 680))
        if win.get("tab") is not None:
            self.tabs.setCurrentIndex(min(int(win.get("tab", 0)),
                                          self.tabs.count() - 1))
        self.tabs.currentChanged.connect(self._on_tab_changed)

    def _on_tab_changed(self, index: int):
        self.config.window = {**self.config.window, "tab": index}

    def window_state(self) -> dict:
        return {
            "width": self.width(),
            "height": self.height(),
            "tab": self.tabs.currentIndex(),
        }

    # ---------- 托盘 ----------
    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._show_window()

    def closeEvent(self, event):
        if self.tray is not None:
            event.ignore()
            self.hide()
            self.tray.showMessage(APP_DISPLAY_NAME, "程序已最小化到托盘，双击图标恢复。",
                                  QSystemTrayIcon.Information, 3000)
        else:
            self._save_state()
            event.accept()

    def _save_state(self):
        self.config.window = self.window_state()

    def _quit(self):
        if not winutil.confirm(self, "确认退出", "确定退出服务端工具吗？"):
            return
        self._save_state()
        self.tray.hide()
        QApplication.quit()
