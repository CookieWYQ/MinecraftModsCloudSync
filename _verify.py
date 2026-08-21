# -*- coding: utf-8 -*-
"""临时验证（跑完删除）。"""
import os
import sys
import tempfile

from app_common.tasks import TodoManifest, TaskItem

# 1) manifest settings 往返
m = TodoManifest.new("hotfix-2.0")
m.tasks = [TaskItem(action="delete", category="mods", target="mods/a.jar")]
m.settings = {"check_interval_min": 30, "autostart": True, "notify": False}
m2 = TodoManifest.from_dict(m.to_dict())
assert m2.settings == m.settings, m2.settings
assert "自动检查间隔(分钟)=30" in m.settings_summary()
assert "开机自启=开" in m.settings_summary()
assert "系统通知=关" in m.settings_summary()
print("manifest settings OK:", m.settings_summary())

# 2) 图标内嵌解码（多尺寸）—— 需先创建 QApplication（QPixmap 依赖 GUI 环境）
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)

from app_common.app_icon import get_app_icon  # noqa: E402

icon = get_app_icon("client")
assert not icon.isNull(), "client icon should decode"
icon2 = get_app_icon("server")
assert not icon2.isNull(), "server icon should decode"
print("embedded icons OK (client/server)")

# 3) 日志收集（构造假日志目录）
from app_common import log_manager  # noqa: E402
from app_common.logger import set_app_scope  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

set_app_scope("")  # 无 scope：直接使用 logs_dir
tmp = tempfile.mkdtemp()
fake_log = os.path.join(tmp, "20260821.log")
with open(fake_log, "w", encoding="utf-8") as f:
    f.write("2026-08-21 10:00:00 [INFO] app | 启动\n")
    f.write("2026-08-21 11:30:00 [ERROR] app | 出错 something\n")
    f.write("bad line without timestamp\n")
orig = log_manager.active_logs_dir
log_manager.active_logs_dir = lambda: __import__("pathlib").Path(tmp)
lines = log_manager.collect_log_lines(
    datetime(2026, 8, 21, 10, 30), datetime(2026, 8, 21, 12, 0), "出错")
assert len(lines) == 1 and "出错" in lines[0], lines
log_manager.active_logs_dir = orig
print("log collection OK:", lines)

# 4) GUI 构造冒烟 + 主题切换
# 空配置的服务端窗口会在 600ms 后弹「SFTP 未配置」模态框，压测时静默掉
from app_common import winutil  # noqa: E402

winutil.warn = lambda *a, **k: None
from PySide6.QtCore import QTimer  # noqa: E402

from app_common.app_config import ClientConfig, ServerConfig  # noqa: E402
from app_common.style import apply_style  # noqa: E402
from app_common.profile import build_profile_content, new_server_id  # noqa: E402
from server_app.ui.main_window import ServerMainWindow  # noqa: E402
from client_app.ui.main_window import ClientMainWindow  # noqa: E402

apply_style(app, dark=True)
apply_style(app, dark=False)
# 使用临时配置：避免读取真实配置后触发后台 SFTP 连接线程拖住进程退出
sw = ServerMainWindow(ServerConfig(path=os.path.join(tempfile.mkdtemp(), "s.json")))
sw.show()
cfg = ClientConfig(path=os.path.join(tempfile.mkdtemp(), "c.json"))
sid = new_server_id()
cfg.add_profile("测试服", sid, build_profile_content("测试服", sid,
                 {"host": "127.0.0.1", "port": 22, "username": "u", "password": "p"}))
cw = ClientMainWindow(cfg)
cw.show()
QTimer.singleShot(1500, app.quit)
rc = app.exec()
print("GUI construct OK rc=", rc)
sys.exit(rc)
