# -*- coding: utf-8 -*-
"""「文件管理（手动）」模组标记回归测试。

覆盖三类手动标记（与其它页面共用同一份数据）：

1. 发布待办：本次不发布 —— 写入 ``todo_<server>.json``，发布待办检测会跳过；
2. 自动同步：排除 —— 写入服务器上的 ``{todo_dir}/auto_sync.json`` 黑名单；
3. 差异审核：跳过 —— 写入 ``target_overrides``（rel / client_files/rel → skip）。

同时检查「标记」列文案、右键菜单勾选状态、自动同步策略文件的读写与取消标记。

用法（仓库根目录）：
    python tests/test_file_manager_marks.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app_common import auto_sync  # noqa: E402
from app_common import file_manager as fm  # noqa: E402
from app_common.app_config import ServerConfig  # noqa: E402
from app_common.sftp import SFTPManager  # noqa: E402
from tests.fake_sftp import FakeSFTPServer  # noqa: E402

import server_app.ui.todo_page as todo_mod  # noqa: E402
from server_app.ui.file_manager_page import (  # noqa: E402
    MARK_DIFF,
    MARK_SYNC,
    MARK_TODO,
    ROLE_DATA,
    FileManagerPage,
)

DATA = tempfile.mkdtemp(prefix="fm_marks_data_")
REMOTE = tempfile.mkdtemp(prefix="fm_marks_remote_")
todo_mod.config_dir = lambda: Path(DATA)

HOST_USER = "tester"
HOST_PASS = "secret"

MOD_A = "mods/sodium-fabric-0.5.8+mc1.20.1.jar"
MOD_B = "mods/xyz-unknown-mod-1.2.3.jar"


def _write(rel: str, data: bytes) -> None:
    path = os.path.join(REMOTE, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _find_item(page: FileManagerPage, rel: str):
    stack = [page.tree.topLevelItem(i)
             for i in range(page.tree.topLevelItemCount())]
    while stack:
        node = stack.pop()
        if (node.data(0, ROLE_DATA) or {}).get("rel") == rel:
            return node
        stack.extend(node.child(i) for i in range(node.childCount()))
    return None


def _menu_labels(page: FileManagerPage, items) -> dict:
    """构造一次「标记 / 排除」子菜单，返回 {动作文字: 是否勾选}。"""
    from PySide6.QtWidgets import QMenu
    menu = QMenu()
    page._add_marks_menu(menu, items)
    sub_action = menu.actions()[0]
    sub = sub_action.menu()
    return {act.text(): act.isChecked() for act in sub.actions()}


def check_publish_mark(page: FileManagerPage, item) -> None:
    print("=== 1. 发布待办：本次不发布 ===")
    assert page._mark_labels(MOD_A, False) == [], page._mark_labels(MOD_A, False)
    page._toggle_publish_marks([item], True)
    assert MOD_A in page._marks[MARK_TODO], page._marks
    assert "发布排除" in page._mark_labels(MOD_A, False), page._mark_labels(MOD_A, False)
    assert page.tree is not None
    assert "发布排除" in item.text(4), item.text(4)
    state_file = os.path.join(DATA, "snapshots",
                              f"todo_{page.config.current_id()}.json")
    assert os.path.isfile(state_file), state_file
    with open(state_file, encoding="utf-8") as f:
        assert MOD_A in json.load(f)["disabled"]
    labels = _menu_labels(page, [item])
    publish = [v for k, v in labels.items() if "发布待办" in k]
    assert publish == [True], labels
    page._toggle_publish_marks([item], False)
    assert MOD_A not in page._marks[MARK_TODO]
    assert page._mark_labels(MOD_A, False) == []
    print("发布待办标记写入 / 取消 / 菜单回显正确")


def check_sync_mark(page: FileManagerPage, item, info: dict) -> None:
    print("=== 2. 自动同步：排除（服务器策略黑名单）===")
    raw = page._mark_sync_worker(info, [MOD_A], True)
    page._on_mark_sync_done(True, raw)
    assert MOD_A in page._marks[MARK_SYNC], page._marks[MARK_SYNC]
    assert "同步排除" in item.text(4), item.text(4)
    with SFTPManager(info["host"], info["port"],
                     info["username"], info["password"]) as sftp:
        policy = auto_sync.load_policy(sftp, info["todo_dir"])
    assert policy is not None, "服务器上应已写入 auto_sync.json"
    assert MOD_A in policy["exclude"], policy["exclude"]
    assert policy["enabled"] is False, "标记不能偷偷打开自动同步总开关"
    labels = _menu_labels(page, [item])
    sync_on = [v for k, v in labels.items() if "自动同步" in k]
    assert sync_on == [True], labels

    # 「发布待办」和「差异审核」不因自动同步标记而勾选
    assert "发布排除" not in page._mark_labels(MOD_A, False)
    raw = page._mark_sync_worker(info, [MOD_A], False)
    page._on_mark_sync_done(True, raw)
    with SFTPManager(info["host"], info["port"],
                     info["username"], info["password"]) as sftp:
        policy = auto_sync.load_policy(sftp, info["todo_dir"])
    assert MOD_A not in policy["exclude"], policy["exclude"]
    print("自动同步黑名单写入 / 读回 / 取消正确")


def check_diff_mark(page: FileManagerPage, item, path: str) -> None:
    print("=== 3. 差异审核：跳过 ===")
    page._toggle_diff_marks([item], True)
    assert MOD_A in page._marks[MARK_DIFF], page._marks[MARK_DIFF]
    assert "审核跳过" in item.text(4), item.text(4)
    cfg2 = ServerConfig(path)
    overrides = cfg2.target_overrides
    assert overrides.get(MOD_A) == "skip", overrides
    assert overrides.get(f"client_files/{MOD_A}") == "skip", overrides
    labels = _menu_labels(page, [item])
    diff_on = [v for k, v in labels.items() if "差异审核" in k]
    assert diff_on == [True], labels
    page._toggle_diff_marks([item], False)
    assert page._mark_labels(MOD_A, False) == []
    cfg3 = ServerConfig(path)
    assert cfg3.target_overrides.get(MOD_A) is None, cfg3.target_overrides
    print("差异审核跳过写入 / 取消 / client_files 副本同步标记正确")


def check_mod_web_menu(page: FileManagerPage) -> None:
    print("=== 4. 模组网页子菜单 ===")
    from PySide6.QtWidgets import QMenu
    menu = QMenu()
    page._add_mod_web_menu(menu, "sodium-fabric-0.5.8+mc1.20.1.jar")
    sub_action = menu.actions()[0]
    sub = sub_action.menu()
    acts = sub.actions()
    payloads = [act.data() for act in acts]
    links = [p[1] for p in payloads if isinstance(p, tuple) and p[0] == "link"
             and "modrinth" in p[1]]
    assert "https://modrinth.com/mod/sodium" in links, payloads
    mc = [p[1] for p in payloads if isinstance(p, tuple) and p[0] == "link"
          and "mcmod.cn/class/" in p[1]]
    assert mc and mc[0].endswith(".html"), payloads
    texts = [act.text() for act in acts]
    assert any("查看" in t for t in texts), texts
    print("模组网页菜单：MC 百科详情页 / Modrinth / CurseForge 链接已挂上")


def check_menu_dispatch(page: FileManagerPage) -> None:
    """右键菜单动作分发：mark / copy / 普通动作。"""
    print("=== 5. 右键菜单动作分发 ===")
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QApplication

    item = _find_item(page, MOD_B)
    assert item is not None
    page.tree.clearSelection()
    item.setSelected(True)

    act = QAction()
    act.setCheckable(True)
    act.setChecked(True)
    act.setData(("mark", MARK_TODO))
    assert page._handle_menu_action(act) is True
    assert MOD_B in page._marks[MARK_TODO], page._marks[MARK_TODO]
    act2 = QAction()
    act2.setCheckable(True)
    act2.setChecked(False)
    act2.setData(("mark", MARK_TODO))
    assert page._handle_menu_action(act2) is True
    assert MOD_B not in page._marks[MARK_TODO]

    copy = QAction()
    copy.setData(("copy", "https://modrinth.com/mod/example", "Modrinth"))
    assert page._handle_menu_action(copy) is True
    assert QApplication.clipboard().text() == "https://modrinth.com/mod/example"

    plain = QAction("刷新")
    assert page._handle_menu_action(plain) is False
    print("mark / copy / 普通动作分发正确")


def check_reload_flow(app, cfg, host, port, path) -> None:
    """真实 reload（Worker 线程 + 读取服务器策略）后标记列应回显黑名单条目。"""
    print("=== 6. 页面 reload：读取服务器策略并回显标记 ===")
    with SFTPManager(host, port, HOST_USER, HOST_PASS) as sftp:
        auto_sync.save_policy(
            sftp, "/todo",
            auto_sync.new_policy(exclude=[MOD_B]))
    page = FileManagerPage(cfg)
    page.reload()
    import time
    start = time.time()
    while time.time() - start < 20:
        app.processEvents()
        if page._loaded_key and not page._busy:
            break
        time.sleep(0.02)
    assert page._loaded_key, "文件管理页 reload 超时"
    item = _find_item(page, MOD_B)
    assert item is not None, f"reload 后树里找不到 {MOD_B}"
    assert "同步排除" in item.text(4), item.text(4)
    assert MOD_B in page._marks[MARK_SYNC], page._marks[MARK_SYNC]
    page.deleteLater()
    app.processEvents()
    print("reload 后「同步排除」标记回显正确（策略来自服务器）")


def main() -> None:
    _write(MOD_A, b"sodium")
    _write(MOD_B, b"unknown")
    _write("mods/broken.jar.disabled", b"disabled")
    srv = FakeSFTPServer(REMOTE)
    host, port = srv.start()
    app = QApplication.instance() or QApplication([])
    try:
        path = os.path.join(DATA, "server_config.json")
        cfg = ServerConfig(path)
        cfg.sftp = {"host": host, "port": port,
                    "username": HOST_USER, "password": HOST_PASS}
        cfg.remote = {"todo_dir": "/todo", "files_dir": "/client_files",
                      "server_root": "/"}
        page = FileManagerPage(cfg)
        page._load_local_marks()
        with SFTPManager(host, port, HOST_USER, HOST_PASS) as sftp:
            data = fm.scan_overview(sftp, "/")
        page._overview = data
        page._fill_tree(data)
        item = _find_item(page, MOD_A)
        assert item is not None, "树里找不到测试模组"
        assert item.text(4) == "", item.text(4)
        count = page.tree.columnCount()
        assert count == 5, count
        assert page.tree.headerItem().text(4) == "标记"

        info = page._sftp_info()
        assert info["todo_dir"] == "/todo", info

        check_publish_mark(page, item)
        check_sync_mark(page, item, info)
        check_diff_mark(page, item, path)
        check_mod_web_menu(page)
        check_menu_dispatch(page)
        check_reload_flow(app, cfg, host, port, path)

        page.deleteLater()
        app.processEvents()
        print("\nFILE MANAGER MARKS OK")
    finally:
        srv.stop()


if __name__ == "__main__":
    main()
