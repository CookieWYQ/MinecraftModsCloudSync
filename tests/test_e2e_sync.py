# -*- coding: utf-8 -*-
"""端到端回归：C2S 上传 → 快照 → S2C 发布待办 → 客户端检查/应用。

用本地 paramiko SFTP 服务器（tests/fake_sftp.py）模拟真实服务器，覆盖：

1. C2S：仅客户端模组只进 client_files，双端模组两边各一份，服务端模组不进 client_files；
2. S2C：以上次发布基线对比服务器**实时** client_files，列出新增并生成待办清单；
3. 客户端：授权校验 → 读取待办 → 下载安装/删除；
4. 服主绕过本工具、直接往服务器 client_files 里加/改/删文件，S2C 也能发现；
5. 同一版本号二次发布，客户端仍能识别为「有新更新」。

运行：.venv/Scripts/python.exe tests/test_e2e_sync.py
"""
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 测试用 SFTP 服务器在连接关闭时会打大量 paramiko socket 日志，压掉
logging.getLogger("paramiko").setLevel(logging.CRITICAL)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PySide6.QtWidgets import QApplication  # noqa: E402

from app_common.app_config import ClientConfig, ServerConfig  # noqa: E402
from app_common.license import save_ids  # noqa: E402
from app_common.profile import build_profile_content, new_client_id  # noqa: E402
from app_common.sftp import SFTPManager  # noqa: E402
from app_common.snapshot import load_snapshot  # noqa: E402
from app_common.tasks import manifest_signature  # noqa: E402
from client_app.engine import apply_update, check_all, is_manifest_new  # noqa: E402
from server_app.ui.remote_page import RemoteFilePage  # noqa: E402
from server_app.ui.todo_page import TodoPage  # noqa: E402
from tests.fake_sftp import FakeSFTPServer  # noqa: E402

APP = QApplication.instance() or QApplication([])

DATA = tempfile.mkdtemp(prefix="mc_sync_e2e_data_")
REMOTE = tempfile.mkdtemp(prefix="mc_sync_e2e_remote_")
LOCAL = tempfile.mkdtemp(prefix="mc_sync_e2e_local_")
GAME = tempfile.mkdtemp(prefix="mc_sync_e2e_game_")

# 快照 / 待办状态 / 哈希缓存都写到临时目录，绝不碰用户真实配置
import app_common.constants as constants_mod  # noqa: E402
import app_common.snapshot as snapshot_mod  # noqa: E402
import server_app.ui.todo_page as todo_mod  # noqa: E402
from pathlib import Path  # noqa: E402

constants_mod.config_dir = lambda: Path(DATA)
snapshot_mod.config_dir = lambda: Path(DATA)
todo_mod.config_dir = lambda: Path(DATA)

SERVER_ID = "srv-e2e-0001"
SFTP_USER, SFTP_PASS = "tester", "secret"


def log_step(title):
    print(f"\n=== {title} ===")


def remote(*parts):
    return os.path.join(REMOTE, *parts)


def write_remote(rel, data: bytes):
    p = remote(*rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


def read_remote(rel) -> bytes:
    with open(remote(*rel.split("/")), "rb") as f:
        return f.read()


def remote_exists(rel) -> bool:
    return os.path.isfile(remote(*rel.split("/")))


def write_local(rel, data: bytes):
    p = os.path.join(LOCAL, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


def setup_remote():
    # 服务端根目录：模拟真实服务器（顶层文件受保护，服务端模组）
    write_remote("mods/servercore-1.0.jar", b"S" * 64)
    write_remote("start.sh", b"#!/bin/sh\n")
    write_remote("server.properties", b"motd=test\n")
    os.makedirs(remote("client_files", "mods"), exist_ok=True)
    os.makedirs(remote("client_files", "resourcepacks"), exist_ok=True)
    os.makedirs(remote("todo"), exist_ok=True)
    os.makedirs(remote("c2c"), exist_ok=True)


def setup_local():
    write_local("mods/both-mod-1.0.jar", b"B" * 100)
    write_local("mods/client-only-1.0.jar", b"C" * 120)
    write_local("mods/server-only-1.0.jar", b"D" * 140)
    write_local("resourcepacks/MyPack.zip", b"P" * 200)
    write_local("config/mymod.toml", b"key = 1\n")


def make_server_config() -> ServerConfig:
    return ServerConfig(os.path.join(DATA, "server_config.json"))


def apply_server_config(cfg: ServerConfig, host, port) -> None:
    cfg.sftp = {"host": host, "port": port,
                "username": SFTP_USER, "password": SFTP_PASS}
    cfg.remote = {
        "todo_dir": "/todo", "files_dir": "/client_files",
        "c2c_dir": "/c2c", "server_root": "/",
        "exclude": [], "client_mods": [], "auto_targets": {},
        "env_online": False, "env_version_check": False,
        "target_overrides": {
            "mods/client-only-1.0.jar": "client",
            "mods/server-only-1.0.jar": "server",
        },
        "sync_scope": [],
    }
    cfg.local_mc_dir = LOCAL


def c2s_scan(page) -> list[dict]:
    """执行一次「更新服务端」扫描并返回差异行（等价于点「重新扫描」）。"""
    payload = json.loads(page._snapshot_scan_worker())
    rows = payload.get("rows") or []
    page._rows = [dict(r, checked=(r["status"] != "server")) for r in rows]
    page._refresh_tree()
    return rows


def c2s_send(page):
    """勾选全部差异并发送；随后重建服务端快照（与真实流程一致）。"""
    uploads, deletes, deletes_client, obsolete_n, renames = page._collect_actions()
    page._send_worker(uploads, deletes, deletes_client, renames)
    page._snapshot_scan_worker()  # 发送完成后自动重建快照
    return uploads, deletes, deletes_client, renames


def s2c_detect(page) -> dict:
    payload = json.loads(page._detect_worker())
    page._rows = payload.get("rows") or []
    page._build_changes_tree(page._rows)
    return payload


def s2c_publish(page, version="v1"):
    items = page._collect_publish_items()
    assert items, "没有可发布的改动"
    msg = page._publish_worker(version, items, {}, {}, "e2e", False, False, [])
    return items, msg


def manifest_remote() -> dict:
    with SFTPManager("127.0.0.1", srv.port, SFTP_USER, SFTP_PASS) as sftp:
        return json.loads(sftp.read_text("/todo/manifest.json"))


def client_check(cfg: ClientConfig) -> dict:
    results = check_all(cfg)
    result = results[SERVER_ID]
    assert result["ok"], f"客户端检查失败: {result}"
    return result


def client_apply(cfg: ClientConfig, manifest) -> dict:
    summary = apply_update(cfg, cfg.profile_by_id(SERVER_ID), manifest)
    assert not summary["errors"], summary["errors"]
    # 模拟客户端界面：应用成功后记录版本 + 内容指纹
    cfg.mark_applied(SERVER_ID, manifest.version,
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     sig=manifest_signature(manifest))
    return summary


def game_file(rel: str) -> str:
    return os.path.join(GAME, *rel.split("/"))


def assert_game_has(rel: str, data: bytes):
    path = game_file(rel)
    assert os.path.isfile(path), f"客户端缺少文件: {rel}"
    with open(path, "rb") as f:
        assert f.read() == data, f"客户端文件内容不符: {rel}"


srv = FakeSFTPServer(REMOTE)
host, port = srv.start()
try:
    setup_remote()
    setup_local()
    server_cfg = make_server_config()

    # ---------- 1. C2S：上传到服务器 ----------
    log_step("1. C2S 上传（仅客户端 / 双端 / 服务端）")
    page = RemoteFilePage(server_cfg)  # 此时无 SFTP/本地目录，不会启动自动检测
    apply_server_config(server_cfg, host, port)
    page._snapshot_busy = True
    page._load_config()
    page._snapshot_busy = False

    rows = c2s_scan(page)
    by_rel = {r["rel"]: r for r in rows}
    for rel in ("mods/both-mod-1.0.jar", "mods/client-only-1.0.jar",
                "mods/server-only-1.0.jar", "resourcepacks/MyPack.zip",
                "config/mymod.toml"):
        assert rel in by_rel, f"C2S 未检测到新增: {rel}（实际 {sorted(by_rel)}）"
    assert by_rel["mods/server-only-1.0.jar"]["target"] == "server"
    assert by_rel["mods/client-only-1.0.jar"]["target"] == "client"
    assert by_rel["mods/both-mod-1.0.jar"]["target"] == "both"

    c2s_send(page)
    assert remote_exists("mods/both-mod-1.0.jar")
    assert remote_exists("client_files/mods/both-mod-1.0.jar")
    assert remote_exists("mods/server-only-1.0.jar")
    assert not remote_exists("client_files/mods/server-only-1.0.jar"), \
        "仅服务端模组绝不能进 client_files（会导致客户端多装模组）"
    assert not remote_exists("mods/client-only-1.0.jar"), \
        "仅客户端模组绝不能进服务端 mods（会导致服务器启动失败）"
    assert remote_exists("client_files/mods/client-only-1.0.jar")
    assert remote_exists("client_files/resourcepacks/MyPack.zip")
    assert remote_exists("client_files/config/mymod.toml")
    print("C2S 目标分流正确")

    # ---------- 2. S2C：对比发布基线，生成待办 ----------
    log_step("2. S2C 首次检测 + 发布")
    todo = TodoPage(server_cfg)
    payload = s2c_detect(todo)
    rows = payload["rows"]
    rels = {r["rel"] for r in rows}
    expected = {"mods/both-mod-1.0.jar", "mods/client-only-1.0.jar",
                "resourcepacks/MyPack.zip", "config/mymod.toml"}
    assert rels == expected, f"S2C 首轮应列出全部客户端文件，实际 {sorted(rels)}"
    assert all(r["status"] == "new" for r in rows)

    items, _ = s2c_publish(todo, "v1")
    manifest = manifest_remote()
    tasks = {(t["action"], t["target"], t["source"]) for t in manifest["tasks"]}
    for rel in expected:
        assert ("install", rel, rel) in tasks, f"待办缺少 install 任务: {rel}"
    publish_snap = load_snapshot(server_cfg.current_id(), "publish")
    assert len(publish_snap["files"]) == 4, publish_snap

    payload2 = s2c_detect(todo)
    assert not payload2["rows"], f"发布后不应再有改动，实际 {payload2['rows']}"

    # ---------- 3. 客户端：授权 → 检查 → 应用 ----------
    log_step("3. 客户端检查/应用")
    client_id = new_client_id()
    with SFTPManager(host, port, SFTP_USER, SFTP_PASS) as sftp:
        save_ids(sftp, [client_id])
    content = build_profile_content("E2E 测试服", SERVER_ID, {
        "host": host, "port": port, "username": SFTP_USER,
        "password": SFTP_PASS, "todo_dir": "/todo",
        "files_dir": "/client_files", "c2c_dir": "/c2c",
    }, client_id=client_id)
    client_cfg = ClientConfig(os.path.join(DATA, "client_config.json"))
    client_cfg.add_profile("E2E 测试服", SERVER_ID, content, client_id=client_id)
    client_cfg.local_mc_dir = GAME

    result = client_check(client_cfg)
    assert result["manifest"] is not None, "客户端读不到待办清单"
    assert is_manifest_new(client_cfg.profile_by_id(SERVER_ID),
                           result["manifest"]) is True
    summary = client_apply(client_cfg, result["manifest"])
    assert len(summary["installed"]) == 4, summary
    assert_game_has("mods/both-mod-1.0.jar", b"B" * 100)
    assert_game_has("mods/client-only-1.0.jar", b"C" * 120)
    assert_game_has("resourcepacks/MyPack.zip", b"P" * 200)
    assert_game_has("config/mymod.toml", b"key = 1\n")
    assert not os.path.exists(game_file("mods/server-only-1.0.jar")), \
        "客户端不应安装仅服务端模组"
    print("客户端下载安装成功（4 个文件，无错误）")

    # ---------- 4. 同一版本号二次发布，客户端仍能识别 ----------
    log_step("4. 同版本号二次发布（客户端通过内容指纹识别）")
    # 先做纯逻辑校验：版本号相同、任务不同 → 必须视为新更新
    from app_common.tasks import TaskItem, TodoManifest

    m_a = TodoManifest(version="same", tasks=[
        TaskItem(action="install", category="mods", target="mods/a.jar",
                 source="mods/a.jar")])
    m_b = TodoManifest(version="same", tasks=[
        TaskItem(action="install", category="mods", target="mods/b.jar",
                 source="mods/b.jar")])
    fake_profile = {"last_applied_version": "same",
                    "last_applied_sig": manifest_signature(m_a)}
    assert is_manifest_new(fake_profile, m_a) is False
    assert is_manifest_new(fake_profile, m_b) is True

    write_local("mods/extra-mod-1.0.jar", b"E" * 160)
    c2s_scan(page)
    c2s_send(page)
    payload = s2c_detect(todo)
    assert any(r["rel"] == "mods/extra-mod-1.0.jar" for r in payload["rows"]), payload
    s2c_publish(todo, "v1")  # 故意沿用同一版本号（服务端会自动补时间后缀）
    result2 = client_check(client_cfg)
    assert result2["manifest"].version.startswith("v1"), result2["manifest"].version
    assert is_manifest_new(client_cfg.profile_by_id(SERVER_ID),
                           result2["manifest"]) is True, \
        "版本号相同但内容变化时，客户端必须仍认为有新更新"
    client_apply(client_cfg, result2["manifest"])
    assert_game_has("mods/extra-mod-1.0.jar", b"E" * 160)
    print("同版本号二次发布：客户端已识别并应用")

    # ---------- 5. 绕过工具直接往服务器加文件 ----------
    log_step("5. 外部途径新增文件（未走 C2S）")
    write_remote("client_files/mods/external-mod-1.0.jar", b"X" * 180)
    payload = s2c_detect(todo)
    assert any(r["rel"] == "mods/external-mod-1.0.jar" for r in payload["rows"]), \
        f"外部新增文件漏检: {payload['rows']}"
    s2c_publish(todo, "v1")
    result3 = client_check(client_cfg)
    client_apply(client_cfg, result3["manifest"])
    assert_game_has("mods/external-mod-1.0.jar", b"X" * 180)
    print("外部新增文件已被检测、发布、安装")

    # ---------- 6. 绕过工具替换同名同大小文件 ----------
    log_step("6. 外部替换同名同大小文件（靠哈希发现）")
    target_rel = "mods/both-mod-1.0.jar"
    old = read_remote("client_files/" + target_rel)
    new_content = b"Z" * len(old)
    assert new_content != old
    time.sleep(1.1)  # 保证修改时间变化
    write_remote("client_files/" + target_rel, new_content)
    payload = s2c_detect(todo)
    upd = [r for r in payload["rows"] if r["rel"] == target_rel]
    assert upd and upd[0]["status"] == "update", \
        f"同名同大小内容替换漏检: {payload['rows']}"
    s2c_publish(todo, "v1")
    result4 = client_check(client_cfg)
    client_apply(client_cfg, result4["manifest"])
    assert_game_has(target_rel, new_content)
    print("同大小内容替换已被哈希识别并同步")

    # ---------- 7. 绕过工具删除文件 ----------
    log_step("7. 外部途径删除文件")
    os.remove(remote("client_files", "resourcepacks", "MyPack.zip"))
    payload = s2c_detect(todo)
    deleted = [r for r in payload["rows"] if r["rel"] == "resourcepacks/MyPack.zip"]
    assert deleted and deleted[0]["status"] == "deleted", payload["rows"]
    s2c_publish(todo, "v1")
    result5 = client_check(client_cfg)
    summary = client_apply(client_cfg, result5["manifest"])
    assert "resourcepacks/MyPack.zip" in summary["deleted"], summary
    assert not os.path.exists(game_file("resourcepacks/MyPack.zip")), \
        "客户端应删除服务端已移除的文件"
    print("外部删除已同步到客户端")

    # ---------- 8. 发布前的源文件核对 ----------
    log_step("8. 发布前核对源文件（防止发布坏任务）")
    write_local("mods/guard-mod-1.0.jar", b"G" * 90)
    c2s_scan(page)
    c2s_send(page)
    s2c_detect(todo)
    items = todo._collect_publish_items()
    assert items
    os.remove(remote("client_files", "mods", "guard-mod-1.0.jar"))  # 检测完又被删
    try:
        todo._publish_worker("v1", items, {}, {}, "e2e", False, False, [])
        raise AssertionError("源文件缺失时发布本应中止")
    except RuntimeError as exc:
        assert "发布已中止" in str(exc), exc
    print("源文件缺失时发布会中止并提示重新检测")

    # ---------- 9. 静默批次：客户端能识别为可自动应用 ----------
    log_step("9. 静默批次识别（服务端勾选「静默下载应用」）")
    from client_app.engine import is_silent_batch

    write_local("mods/silent-mod-1.0.jar", b"Q" * 70)
    c2s_scan(page)
    c2s_send(page)
    s2c_detect(todo)
    items = todo._collect_publish_items()
    assert items
    todo._publish_worker("v-silent", items, {}, {}, "silent e2e", False, True, [])
    silent_manifest = TodoManifest.from_dict(manifest_remote())
    assert is_silent_batch(silent_manifest) is True, silent_manifest.tasks
    result_silent = client_check(client_cfg)
    assert is_silent_batch(result_silent["manifest"]) is True
    client_apply(client_cfg, result_silent["manifest"])
    assert_game_has("mods/silent-mod-1.0.jar", b"Q" * 70)
    print("静默批次可被客户端识别并应用")

    # ---------- 10. 目录配置错误保护 ----------
    log_step("10. 客户端文件目录不存在时中止检测（防误删）")
    server_cfg.remote = {**server_cfg.remote,
                         "files_dir": "/no_such_client_files_dir"}
    try:
        todo._detect_worker()
        raise AssertionError("目录不存在且已有发布基线时，检测本应中止")
    except RuntimeError as exc:
        assert "不存在" in str(exc), exc
    finally:
        server_cfg.remote = {**server_cfg.remote, "files_dir": "/client_files"}
    print("目录异常时中止检测，不会把已发布文件误判为删除")

    print("\nE2E OK：C2S / S2C / 客户端全流程通过")
finally:
    srv.stop()
    for d in (DATA, REMOTE, LOCAL, GAME):
        shutil.rmtree(d, ignore_errors=True)
