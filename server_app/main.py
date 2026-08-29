# -*- coding: utf-8 -*-
"""服务端工具入口。"""
import sys

from app_common.logger import set_app_scope

# 必须先于任何 get_logger 调用：服务端日志写入 logs\server 子目录，
# 与客户端（logs\client）隔离，避免日志管理中混入对方的记录。
set_app_scope("server")

from PySide6.QtWidgets import QApplication

from app_common.app_config import ServerConfig
from app_common.app_icon import get_app_icon
from app_common.constants import APP_DISPLAY_NAME
from app_common.logger import get_logger
from app_common.style import apply_style, theme_is_dark

from .ui.main_window import ServerMainWindow

log = get_logger("server.main")


def _install_excepthook():
    """打包为无控制台 exe 后，未捕获异常无处可见；记录到日志文件便于排查。"""

    def _hook(exc_type, exc_value, exc_tb):
        log.error("未捕获异常导致程序退出", exc_info=(exc_type, exc_value, exc_tb))
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook


def main() -> int:
    _install_excepthook()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_DISPLAY_NAME)
    app.setQuitOnLastWindowClosed(False)  # 关闭窗口仅最小化到托盘

    # 单实例：已有服务端在运行时自动将其窗口激活到前台，本实例退出
    from app_common.single_instance import ensure_single_instance
    if not ensure_single_instance("server", silent="--tray" in sys.argv):
        return 0

    config = ServerConfig()
    apply_style(app, theme_is_dark(config.theme))
    app.setWindowIcon(get_app_icon("server"))

    window = ServerMainWindow(config)
    if "--tray" not in sys.argv:
        window.showMaximized()
    log.info("服务端工具启动 (--tray=%s)", "--tray" in sys.argv)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
