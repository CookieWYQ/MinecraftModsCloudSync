# -*- coding: utf-8 -*-
"""客户端入口：普通权限运行、无控制台、启动后进入托盘。"""
import sys

from app_common.logger import set_app_scope

# 必须先于任何 get_logger 调用：客户端日志写入 logs\client 子目录，
# 与服务端（logs\server）隔离，客户端日志管理不会出现服务端的 SFTP 信息。
set_app_scope("client")

from PySide6.QtWidgets import QApplication

from app_common.app_config import ClientConfig
from app_common.app_icon import get_app_icon
from app_common.constants import APP_DISPLAY_NAME
from app_common.logger import get_logger
from app_common.style import apply_style, theme_is_dark

from .ui.main_window import ClientMainWindow

log = get_logger("client.main")


def main() -> int:
    # 普通权限运行：客户端写入的是用户目录（.minecraft）与 HKCU 自启注册表，
    # 均不需要管理员；提权反而会因 UIPI 隔离导致无法从资源管理器拖放文件/文件夹。
    app = QApplication(sys.argv)
    app.setApplicationName(APP_DISPLAY_NAME)
    app.setQuitOnLastWindowClosed(False)

    # 单实例：已有客户端在运行时自动将其窗口激活到前台，本实例退出
    from app_common.single_instance import ensure_single_instance
    if not ensure_single_instance("client", silent="--tray" in sys.argv):
        return 0

    config = ClientConfig()
    apply_style(app, theme_is_dark(config.theme))
    app.setWindowIcon(get_app_icon("client"))

    window = ClientMainWindow(config)
    if "--tray" not in sys.argv:
        window.showMaximized()
    else:
        log.info("以 --tray 方式启动，直接进入托盘")
    log.info("客户端启动")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
