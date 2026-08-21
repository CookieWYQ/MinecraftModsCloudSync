# -*- coding: utf-8 -*-
"""Windows 集成辅助：管理员权限、开机自启、打开目录、常见确认框。"""
import ctypes
import os
import subprocess
import sys
import winreg
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from .constants import APP_NAME
from .logger import get_logger

log = get_logger("winutil")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def system_dark_mode() -> bool:
    """检测 Windows 系统是否处于深色模式（AppsUseLightTheme）。"""
    try:
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(value) == 0
    except Exception:
        return True


def relaunch_as_admin() -> bool:
    """以管理员权限重启自身（运行时发现权限不足时调用）。"""
    try:
        params = " ".join(f'"{a}"' for a in sys.argv[1:])
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1
        )
        return True
    except Exception as exc:
        log.exception("提权重启失败: %s", exc)
        return False


def is_autostart_enabled(exe_path: str | None = None) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except FileNotFoundError:
        return False
    except Exception as exc:
        log.warning("查询自启失败: %s", exc)
        return False


def set_autostart(enabled: bool) -> bool:
    """写入/删除 HKCU 开机自启（隐藏窗口启动）。"""
    exe = _current_exe()
    if not exe:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ,
                                  f'"{exe}" --tray')
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception as exc:
        log.exception("设置自启失败: %s", exc)
        return False


def _current_exe() -> str | None:
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable))
    return None


def open_in_explorer(path: str | Path) -> None:
    try:
        os.startfile(str(path))  # noqa: S606
    except Exception as exc:
        log.exception("打开目录失败: %s", exc)


def confirm(parent, title: str, text: str, default_yes: bool = False) -> bool:
    """统一二次确认弹窗。"""
    buttons = QMessageBox.Yes | QMessageBox.No
    default_btn = QMessageBox.Yes if default_yes else QMessageBox.No
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Question)
    box.setStandardButtons(buttons)
    box.setDefaultButton(default_btn)
    return box.exec() == QMessageBox.Yes


def confirm_list(parent, title: str, text: str, items: list[str],
                 ok_label: str = "确定", cancel_label: str = "取消") -> bool:
    """带可滚动条目列表的二次确认弹窗。

    条目较多（批量操作）时使用，避免大量文本把弹窗撑得超出屏幕、按不到按钮。
    """
    from PySide6.QtWidgets import (
        QDialog,
        QDialogButtonBox,
        QLabel,
        QListWidget,
        QVBoxLayout,
    )
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumSize(480, 380)
    layout = QVBoxLayout(dlg)
    if text:
        tip = QLabel(text)
        tip.setWordWrap(True)
        layout.addWidget(tip)
    lst = QListWidget()
    lst.addItems(items)
    layout.addWidget(lst, 1)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.button(QDialogButtonBox.Ok).setText(ok_label)
    buttons.button(QDialogButtonBox.Ok).setDefault(True)
    buttons.button(QDialogButtonBox.Cancel).setText(cancel_label)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)
    return dlg.exec() == QDialog.Accepted


def info(parent, title: str, text: str) -> None:
    QMessageBox.information(parent, title, text)


def info_list(parent, title: str, items: list[str], text: str = "") -> None:
    """带可滚动条目列表的信息弹窗（内容多时使用，避免撑出屏幕按不到按钮）。"""
    from PySide6.QtWidgets import (
        QDialog,
        QDialogButtonBox,
        QLabel,
        QListWidget,
        QVBoxLayout,
    )
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumSize(520, 420)
    layout = QVBoxLayout(dlg)
    if text:
        tip = QLabel(text)
        tip.setWordWrap(True)
        layout.addWidget(tip)
    lst = QListWidget()
    lst.addItems(items)
    layout.addWidget(lst, 1)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok)
    buttons.button(QDialogButtonBox.Ok).setText("确定")
    buttons.accepted.connect(dlg.accept)
    layout.addWidget(buttons)
    dlg.exec()


def warn(parent, title: str, text: str) -> None:
    QMessageBox.warning(parent, title, text)


def error(parent, title: str, text: str) -> None:
    QMessageBox.critical(parent, title, text)
