# -*- coding: utf-8 -*-
"""C2S 旧版 / 临时文件清理 + 历史版本（回收站）回归。

覆盖：
1. `mod_key` / `is_junk_filename`：版本号带字母、加载器后缀、大小写/下划线差异归一；
2. 服务端 /mods 与 /client_files/mods 的旧版（改名、换版本）都能被识别为「旧版残留」；
3. .bak / .temp / .disabled.<时间戳> 识别为「临时/备份」，本地同名临时文件不参与同步；
4. 开启历史版本后，删除/替换先归档到 .mc-sync-history，活动目录变干净；
5. 历史条目可还原；按保留数量清理；历史目录不参与扫描。

运行：.venv/Scripts/python.exe tests/test_c2s_cleanup.py
"""
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
logging.getLogger("paramiko").setLevel(logging.CRITICAL)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app_common.app_config import ServerConfig  # noqa: E402
from app_common.history import (  # noqa: E402
    HISTORY_SUBDIR,
    HistoryStore,
    history_root,
    list_history,
    load_index,
    restore_entries,
)
from app_common.mod_identity import is_junk_filename, mod_key  # noqa: E402
from app_common.sftp import SFTPManager  # noqa: E402
from tests.fake_sftp import FakeSFTPServer  # noqa: E402

import app_common.constants as constants_mod  # noqa: E402
import app_common.snapshot as snapshot_mod  # noqa: E402
import server_app.ui.todo_page as todo_mod  # noqa: E402
import server_app.ui.remote_page as remote_mod  # noqa: E402
from server_app.ui.remote_page import RemoteFilePage  # noqa: E402

APP = QApplication.instance() or QApplication([])

DATA = tempfile.mkdtemp(prefix="mc_cleanup_data_")
REMOTE = tempfile.mkdtemp(prefix="mc_cleanup_remote_")
LOCAL = tempfile.mkdtemp(prefix="mc_cleanup_local_")
constants_mod.config_dir = lambda: Path(DATA)
snapshot_mod.config_dir = lambda: Path(DATA)
todo_mod.config_dir = lambda: Path(DATA)

SERVER_ID = "srv-cleanup-0001"
SFTP_USER, SFTP_PASS = "tester", "secret"


def remote(*parts):
    return os.path.join(REMOTE, *parts)


def write_remote(rel, data: bytes):
    p = remote(*rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


def remote_exists(rel):
    return os.path.isfile(remote(*rel.split("/")))


def write_local(rel, data: bytes):
    p = os.path.join(LOCAL, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


def make_page(cfg):
    page = RemoteFilePage(cfg)
    page._snapshot_busy = True
    page._load_config()
    page._snapshot_busy = False
    return page


def c2s_scan(page):
    payload = json.loads(page._snapshot_scan_worker())
    rows = payload.get("rows") or []
    page._rows = [dict(r, checked=(r["status"] != "server")) for r in rows]
    page._refresh_tree()
    return rows


def c2s_send(page):
    uploads, deletes, deletes_client, _obsolete, renames = page._collect_actions()
    msg = page._send_worker(uploads, deletes, deletes_client, renames)
    page._snapshot_scan_worker()
    return msg


def test_mod_identity():
    pairs = [
        ("create-central-kitchen-2.6.2.jar", "create-central-kitchen-2.6.2b.jar"),
        ("copycats-3.0.9+mc.1.21.1-neoforge.jar",
         "copycats-3.0.11+mc.1.21.1-neoforge.jar"),
        ("ItemPhysic_NEOFORGE_v1.8.14_mc1.21.1.jar",
         "ItemPhysic_NEOFORGE_v1.8.17_mc1.21.1.jar"),
        ("PuzzlesLib-v21.1.60-mc1.21.1-NeoForge.jar",
         "puzzleslib-v21.1.66-mc1.21.1+neoforge.jar"),
        ("fragmentum-5.0.0+1.21.1-neoforge.jar",
         "fragmentum-neoforge-1.21.1-2.4.3.jar"),
        ("CreateCyberGoggles-1.21.1-8.6.4-NeoForge.jar",
         "create_cyber_goggles-1.21.1-8.8.0-neoforge.jar"),
        ("create_afn.jar", "create_afn-1.8.6.jar"),
    ]
    for a, b in pairs:
        assert mod_key(a) == mod_key(b), f"同一模组未归一到同一键: {a} / {b}"
    assert mod_key("copycats-3.0.9+mc.jar") != mod_key("fluidlogistics-1.3.2.jar")
    for junk in ("x.jar.bak", "x.jar.temp", "x.jar.new", "x.jar.old",
                 "x.jar.disabled.1793431851"):
        assert is_junk_filename(junk), f"未识别为临时/备份文件: {junk}"
    assert not is_junk_filename("x.jar.disabled")
    assert not is_junk_filename("x-1.0.jar")
    print("模块身份归一 / 临时文件识别 OK")


def setup_remote():
    # 服务端 mods：旧版（会删除）、新版（本地存在）、无关文件（保留）
    write_remote("mods/[机械动力：中央厨房] create-central-kitchen-2.6.2.jar", b"old-kitchen")
    write_remote("mods/copycats-3.0.9+mc.1.21.1-neoforge.jar", b"old-copycats")
    write_remote("mods/[钠] sodium.jar.bak", b"junk-bak")
    write_remote("mods/sodium.jar.disabled.1793431851", b"junk-temp")
    write_remote("mods/keepmod-1.0.jar", b"server-only")
    # 服务端客户端文件仓库：旧版副本、仅客户端旧版、临时文件
    write_remote("client_files/mods/[机械动力：中央厨房] create-central-kitchen-2.6.2.jar",
                 b"old-kitchen")
    write_remote("client_files/mods/copycats-3.0.9+mc.1.21.1-neoforge.jar", b"old-copycats")
    write_remote("client_files/mods/smoothmovement-1.21.1-2.8.jar", b"old-smooth")
    write_remote("client_files/mods/x.jar.temp", b"junk-client")
    os.makedirs(remote("todo"), exist_ok=True)
    os.makedirs(remote("c2c"), exist_ok=True)


def setup_local():
    write_local("mods/[机械动力：中央厨房] create-central-kitchen-2.6.2b.jar", b"new-kitchen")
    write_local("mods/copycats-3.0.11+mc.1.21.1-neoforge.jar", b"new-copycats")
    write_local("mods/smoothmovement-1.21.1-3.3.jar", b"new-smooth")
    # 本地临时文件：不应参与同步（否则会被当成新增上传）
    write_local("mods/sodium.jar.new", b"local-junk")


def _find_tree_item(page, rel):
    stack = [page.review_tree.topLevelItem(0)]
    while stack:
        item = stack.pop()
        if item is None:
            continue
        d = item.data(0, Qt.UserRole) or {}
        if d.get("kind") == "file" and d.get("rel") == rel:
            return item
        for i in range(item.childCount()):
            stack.append(item.child(i))
    return None


def test_online_ui(page):
    """联网核对结果应用到界面：确认旧版保留勾选、服端更新取消勾选、发现行追加。"""
    rows = page._rows
    newer = {"verdict": "newer", "channel": "Modrinth", "project": "create-central-kitchen",
             "title": "Create: Central Kitchen", "url": "", "server_version": "2.6.2",
             "local_version": "2.6.2b", "server_date": "", "local_date": "",
             "local_file": "create-central-kitchen-2.6.2b.jar", "note": "测试：服端更新"}
    older = {"verdict": "older", "channel": "Modrinth", "project": "copycats",
             "title": "Create: Copycats+", "url": "", "server_version": "3.0.9",
             "local_version": "3.0.11", "server_date": "2026-09-06", "local_date": "2026-11-08",
             "local_file": "copycats-3.0.11+mc.1.21.1-neoforge.jar", "note": "测试：旧版"}
    smooth = dict(older, project="smoothmovement", title="Smooth Movement",
                  server_version="2.8", local_version="3.3")

    def fake_verify(local_items, server_names, discover=(), progress_cb=None, **kw):
        return {
            "matches": {
                "create-central-kitchen-2.6.2": newer,
                "copycats-3.0.9+mc.1.21.1-neoforge": older,
                "smoothmovement-1.21.1-2.8": smooth,
            },
            "discovered": [{"rel": "mods/keepmod-1.0.jar", "name": "keepmod-1.0.jar",
                            "verdict": dict(older, project="keepmod", title="KeepMod")}],
            "resolved": {}, "stats": {"requests": 0, "cache_hits": 0}, "note": "",
        }

    orig = remote_mod.mod_online.verify
    remote_mod.mod_online.verify = fake_verify
    try:
        payload = page._collect_online_payload(rows)
        page._on_online_done(True, page._online_worker(payload))
    finally:
        remote_mod.mod_online.verify = orig

    by_rel = {r["rel"]: r for r in page._rows}
    central = by_rel["mods/[机械动力：中央厨房] create-central-kitchen-2.6.2.jar"]
    assert central["online"]["verdict"] == "newer", central
    assert central["checked"] is False, "服端更新应取消勾选（避免删了降级）"
    copycats = by_rel["mods/copycats-3.0.9+mc.1.21.1-neoforge.jar"]
    assert copycats["online"]["verdict"] == "older" and copycats["checked"] is True
    item = _find_tree_item(page, "mods/copycats-3.0.9+mc.1.21.1-neoforge.jar")
    assert item is not None and "确认旧版" in item.text(8), item.text(8) if item else None
    assert "3.0.9" in item.toolTip(8), item.toolTip(8)
    disc = [r for r in page._rows if r.get("discovered")]
    assert disc and disc[0]["rel"] == "mods/keepmod-1.0.jar", page._rows
    assert disc[0]["status"] == "obsolete" and disc[0]["checked"] is False
    print("联网核对界面 OK：确认旧版保留、服端更新取消、发现行追加")

    # 还原勾选，供后续发送测试（并移除发现行，避免误删 keepmod）
    page._rows = [r for r in page._rows if not r.get("discovered")]
    for r in page._rows:
        if r["status"] in ("new", "update", "rename", "obsolete", "junk"):
            r["checked"] = True
    page._refresh_tree()


def main():
    srv = FakeSFTPServer(REMOTE)
    host, port = srv.start()
    try:
        test_mod_identity()
        setup_remote()
        setup_local()

        cfg = ServerConfig(os.path.join(DATA, "server_config.json"))
        cfg.sftp = {"host": host, "port": port,
                    "username": SFTP_USER, "password": SFTP_PASS}
        cfg.remote = {
            "todo_dir": "/todo", "files_dir": "/client_files",
            "c2c_dir": "/c2c", "server_root": "/",
            "exclude": [], "client_mods": [], "auto_targets": {},
            "env_online": False, "env_version_check": False,
            "target_overrides": {}, "sync_scope": ["mods"],
            "history_enabled": True, "history_keep": 2, "history_max_mb": 0,
        }
        cfg.local_mc_dir = LOCAL

        page = make_page(cfg)
        rows = c2s_scan(page)
        by_rel = {r["rel"]: r for r in rows}
        expected_obsolete = {
            "mods/[机械动力：中央厨房] create-central-kitchen-2.6.2.jar",
            "mods/copycats-3.0.9+mc.1.21.1-neoforge.jar",
            "client_files/mods/[机械动力：中央厨房] create-central-kitchen-2.6.2.jar",
            "client_files/mods/copycats-3.0.9+mc.1.21.1-neoforge.jar",
            "client_files/mods/smoothmovement-1.21.1-2.8.jar",
        }
        for rel in expected_obsolete:
            assert rel in by_rel and by_rel[rel]["status"] == "obsolete", \
                f"未识别为旧版残留: {rel}（实际 {by_rel.get(rel)}）"
        expected_junk = {
            "mods/[钠] sodium.jar.bak",
            "mods/sodium.jar.disabled.1793431851",
            "client_files/mods/x.jar.temp",
        }
        for rel in expected_junk:
            assert rel in by_rel and by_rel[rel]["status"] == "junk", \
                f"未识别为临时/备份: {rel}（实际 {by_rel.get(rel)}）"
        assert by_rel["mods/keepmod-1.0.jar"]["status"] == "server"
        assert "mods/sodium.jar.new" not in by_rel, "本地临时文件不应参与同步"
        print(f"扫描识别 OK：旧版 {len(expected_obsolete)}、临时 {len(expected_junk)}")

        test_online_ui(page)

        msg = c2s_send(page)
        assert "历史版本" in msg, msg
        for rel in expected_obsolete | expected_junk:
            assert not remote_exists(rel), f"发送后仍残留: {rel}"
        assert remote_exists("mods/keepmod-1.0.jar"), "服务端独有文件不应被删除"
        assert remote_exists("client_files/mods/smoothmovement-1.21.1-3.3.jar")
        assert not remote_exists("client_files/mods/smoothmovement-1.21.1-2.8.jar")
        print("发送清理 OK（旧版 / 临时文件已从两个仓库移除；服务端独有保留）")

        # 历史目录内容 + 索引
        with SFTPManager(host, port, SFTP_USER, SFTP_PASS) as sftp:
            data = list_history(sftp, "/client_files")
        n_deleted = len(expected_obsolete) + len(expected_junk)
        assert data["usage"]["count"] >= n_deleted, data["usage"]
        assert data["usage"]["bytes"] > 0
        assert all(".mc-sync-history" not in e.get("rel", "") for e in data["entries"])
        # 清理后重新扫描：历史目录不应出现在差异里
        rows2 = c2s_scan(page)
        assert all(HISTORY_SUBDIR not in r["rel"] for r in rows2), rows2
        print(f"历史版本 OK：已归档 {data['usage']['count']} 个，扫描不读历史目录")

        # 还原：把 copycats 旧版（服务端仓库）放回去
        target = next(e for e in data["entries"]
                      if e["kind"] == "server"
                      and e["rel"].endswith("copycats-3.0.9+mc.1.21.1-neoforge.jar"))
        with SFTPManager(host, port, SFTP_USER, SFTP_PASS) as sftp:
            result = restore_entries(sftp, "/client_files", "/", [target["id"]])
        assert result["restored"], result
        assert remote_exists("mods/copycats-3.0.9+mc.1.21.1-neoforge.jar")
        print("历史还原 OK")

        # 按保留数量清理：同一路径归档两次，keep=1 只留最新
        write_remote("mods/tmp-old.jar", b"v1")
        with SFTPManager(host, port, SFTP_USER, SFTP_PASS) as sftp:
            store = HistoryStore(sftp, "/client_files")
            store.archive("/mods/tmp-old.jar", "server", "tmp-old.jar", "测试")
            store.save()
        write_remote("mods/tmp-old.jar", b"v2")
        with SFTPManager(host, port, SFTP_USER, SFTP_PASS) as sftp:
            store = HistoryStore(sftp, "/client_files")
            store.archive("/mods/tmp-old.jar", "server", "tmp-old.jar", "测试")
            removed = store.prune(1, 0)
            store.save()
            index = load_index(sftp, "/client_files")
        same = [e for e in index["entries"] if e["rel"] == "tmp-old.jar"]
        assert len(same) == 1, same
        assert removed, "keep=1 时应清理掉更旧的那条"
        print("按保留数量清理 OK")

        # 历史目录确实位于 client_files 下且是隐藏目录
        assert os.path.isdir(os.path.join(REMOTE, "client_files", HISTORY_SUBDIR))
        print("全部通过")
    finally:
        srv.stop()
        shutil.rmtree(DATA, ignore_errors=True)


if __name__ == "__main__":
    main()
