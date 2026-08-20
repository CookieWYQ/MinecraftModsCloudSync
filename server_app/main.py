# -*- coding: utf-8 -*-
"""服务端工具入口。"""
import sys

from PySide6.QtWidgets import QApplication

from app_common.app_config import ServerConfig
from app_common.app_icon import get_app_icon
from app_common.constants import APP_DISPLAY_NAME
from app_common.logger import get_logger
from app_common.style import apply_style, theme_is_dark

from .ui.main_window import ServerMainWindow

log = get_logger("server.main")


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_DISPLAY_NAME)
    app.setQuitOnLastWindowClosed(False)  # 关闭窗口仅最小化到托盘

    config = ServerConfig()
    apply_style(app, theme_is_dark(config.theme))
    app.setWindowIcon(get_app_icon("server"))

    window = ServerMainWindow(config)
    if "--tray" not in sys.argv:
        window.show()
    log.info("服务端工具启动 (--tray=%s)", "--tray" in sys.argv)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
