# -*- coding: utf-8 -*-
"""客户端导入服务器配置文件回归测试（v1.3.0 导入失败修复）。

覆盖：
1. 未导入任何配置时，右侧显示「请先导入」引导页，而不是空的服务器详情；
2. 按钮导入路径：保存档案并写入 client_id，界面切换到详情页；
3. 拖入导入路径：.mcscf 直接拖入、扩展名被改写的文件（内容嗅探）都能导入；
4. 取消导入 / 取消覆盖时不写入档案；
5. 导入过程写入日志（用户在「日志管理…」里能看到全部记录）。

运行：.venv/Scripts/python.exe tests/test_client_import.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="client_import_")

from app_common import logger as logger_mod  # noqa: E402

# 测试日志写临时目录，避免污染本机真实客户端日志
logger_mod.logs_dir = lambda: Path(TMP) / "logs"

from app_common.logger import get_logger, set_app_scope  # noqa: E402

set_app_scope("client")
logger_mod._reset_handlers()

from PySide6.QtWidgets import QApplication  # noqa: E402

from app_common import log_manager, winutil  # noqa: E402
from app_common.app_config import ClientConfig  # noqa: E402
from app_common.profile import (  # noqa: E402
    build_profile_content,
    new_server_id,
    parse_profile_content,
)

APP = QApplication.instance() or QApplication(sys.argv)

from client_app.ui.main_window import ClientMainWindow  # noqa: E402

SFTP = {"host": "127.0.0.1", "port": 22, "username": "u", "password": "p"}
log = get_logger("test.client_import")


def _patch_dialogs(confirm: bool = True) -> None:
    winutil.confirm = lambda *a, **k: confirm
    winutil.info = lambda *a, **k: None
    winutil.error = lambda *a, **k: None
    winutil.warn = lambda *a, **k: None


def _write_profile(path: str, name: str) -> str:
    sid = new_server_id()
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_profile_content(name, sid, dict(SFTP)))
    return sid


def _profiles(cfg: ClientConfig) -> list[dict]:
    return ClientConfig(cfg.store.path).profiles()


def main() -> None:
    _patch_dialogs(True)
    cfg_path = os.path.join(TMP, "client.json")
    cfg = ClientConfig(cfg_path)
    win = ClientMainWindow(cfg)

    # 1) 空状态：右侧是引导页，不是服务器详情
    assert win.right_stack.currentWidget() is win.empty_page, "未导入配置时应显示引导页"
    assert win.detail_page.isHidden(), "未导入配置时详情页不应显示"
    print("空状态：右侧显示「请先导入」引导页正确")

    # 2) 按钮导入路径：走 _import_profile（用假的文件选择框直接返回路径）
    path_btn = os.path.join(TMP, "按钮导入服.mcscf")
    sid_btn = _write_profile(path_btn, "按钮导入服")
    with open(path_btn, "r", encoding="utf-8") as f:
        parsed_btn = parse_profile_content(f.read())

    class _FakeFileDialog:
        @staticmethod
        def getOpenFileName(parent=None, title="", directory="", filters=""):
            return path_btn, filters

    import client_app.ui.main_window as mw
    mw.QFileDialog = _FakeFileDialog
    win._import_profile()
    saved = ClientConfig(cfg_path).profile_by_id(sid_btn)
    assert saved is not None, "按钮导入应写入档案"
    assert saved.get("client_id") == parsed_btn["client_id"], saved
    assert win.right_stack.currentWidget() is win.detail_page, "导入后应切换到详情页"
    assert win.lbl_name.text() == "按钮导入服"
    print("按钮导入：档案写入 + client_id 保存 + 详情页切换正确")

    # 3) 拖入 .mcscf 配置文件
    path_mcscf = os.path.join(TMP, "拖入服.mcscf")
    sid_drop = _write_profile(path_mcscf, "拖入服")
    win._confirm_import_dropped([path_mcscf])
    assert ClientConfig(cfg_path).profile_by_id(sid_drop) is not None, "拖入 .mcscf 应导入"
    print("拖入导入：.mcscf 文件导入正确")

    # 4) 扩展名被改写的配置文件：内容嗅探后仍能导入
    path_txt = os.path.join(TMP, "改名的配置.txt")
    sid_sniff = _write_profile(path_txt, "嗅探服")
    assert win._looks_like_profile_file(path_txt), "应按内容识别出配置文件"
    win._handle_dropped([path_txt])
    assert ClientConfig(cfg_path).profile_by_id(sid_sniff) is not None, "嗅探出的配置应导入"
    print("内容嗅探：扩展名被改写的配置文件也能导入")

    # 5) 取消导入 / 取消覆盖不写档案
    _patch_dialogs(False)
    path_cancel = os.path.join(TMP, "取消服.mcscf")
    sid_cancel = _write_profile(path_cancel, "取消服")
    win._confirm_import_dropped([path_cancel])
    assert ClientConfig(cfg_path).profile_by_id(sid_cancel) is None, "取消后不应写入档案"
    before = len(_profiles(cfg))
    win._confirm_import_dropped([path_mcscf])  # 已存在同编号：取消覆盖
    assert len(_profiles(cfg)) == before, "取消覆盖后档案数量不应变化"
    print("取消导入 / 取消覆盖：不写入档案正确")

    # 6) 导入日志可在「日志管理…」中看到
    _patch_dialogs(True)
    lines = log_manager.collect_log_lines(
        datetime.now() - timedelta(minutes=5), datetime.now() + timedelta(minutes=5))
    joined = "\n".join(lines)
    assert "开始导入服务器配置文件" in joined, joined
    assert "导入服务器档案成功: 按钮导入服" in joined, joined
    assert "通过拖入导入服务器档案成功: 拖入服" in joined, joined
    assert "通过拖入导入服务器档案成功: 嗅探服" in joined, joined
    assert "用户取消导入" in joined, joined
    print(f"导入日志：共记录 {len(lines)} 条，导入/取消均有记录")

    # 7) 移除全部服务器后回到空状态引导页
    for p in _profiles(cfg):
        cfg.remove_profile(p["server_id"])
    win.refresh_list()
    assert win.right_stack.currentWidget() is win.empty_page, "移除后应回到引导页"
    print("移除全部服务器：右侧回到引导页正确")

    win.tray.hide()
    win.close()
    win.deleteLater()
    APP.processEvents()
    print("\nCLIENT IMPORT OK：空状态/按钮导入/拖入导入/内容嗅探/取消/日志全部通过")


if __name__ == "__main__":
    main()
