# -*- coding: utf-8 -*-
"""客户端入口：管理员权限、无控制台、启动后进入托盘。"""
import sys

from PySide6.QtWidgets import QApplication

from app_common import winutil
from app_common.app_config import ClientConfig
from app_common.constants import APP_DISPLAY_NAME
from app_common.logger import get_logger
from app_common.style import apply_style

from .ui.main_window import ClientMainWindow

log = get_logger("client.main")


def main() -> int:
    # 打包后 exe 自带管理员清单（图标带盾牌）；源码运行时尝试提权
    if not winutil.is_admin() and "--no-admin" not in sys.argv:
        if winutil.relaunch_as_admin():
            return 0
        log.warning("提权被拒绝，以普通权限继续运行")

    app = QApplication(sys.argv)
    app.setApplicationName(APP_DISPLAY_NAME)
    app.setQuitOnLastWindowClosed(False)
    config = ClientConfig()
    apply_style(app, theme_is_dark(config.theme))
    app.setWindowIcon(get_app_icon("client"))

    window = ClientMainWindow(config)
    if "--tray" not in sys.argv:
        window.show()
    else:
        log.info("以 --tray 方式启动，直接进入托盘")
    log.info("客户端启动，管理员权限: %s", winutil.is_admin())
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
