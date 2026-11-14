# -*- coding: utf-8 -*-
"""自动同步（免待办直连拉取）回归测试。

覆盖：
1. 黑/白名单匹配语义（目录前缀、glob、``**``、黑名单优先、空白名单不同步）；
2. 服务端策略页：按表单生成策略、下发到 ``{todo_dir}/auto_sync.json``、读回、预览；
3. 客户端对比：新增/大小变化/同大小内容变化（远程哈希）/服务器不支持哈希时的
   修改时间回退；范围外与黑名单文件不参与；本地多余文件只报告不删除；
4. 客户端引擎端到端：授权 → 读取策略 → 对比 → 下载；
5. 策略缺失/关闭/白名单为空三种「本次不执行」状态。

运行：.venv/Scripts/python.exe tests/test_auto_sync.py
"""
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
logging.getLogger("paramiko").setLevel(logging.CRITICAL)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PySide6.QtWidgets import QApplication  # noqa: E402

from app_common.app_config import ClientConfig, ServerConfig  # noqa: E402
from app_common.auto_sync import (  # noqa: E402
    apply_sync,
    default_policy,
    dir_allowed,
    glob_match,
    load_policy,
    new_policy,
    normalize_policy,
    path_allowed,
    plan_sync,
    policy_signature,
    save_policy,
)
from app_common.license import save_ids  # noqa: E402
from app_common.profile import build_profile_content, new_client_id  # noqa: E402
from app_common.sftp import SFTPManager  # noqa: E402
from client_app.engine import auto_sync_run  # noqa: E402
from server_app.ui.auto_sync_page import AutoSyncPage  # noqa: E402
from tests.fake_sftp import FakeSFTPServer  # noqa: E402

APP = QApplication.instance() or QApplication([])

DATA = tempfile.mkdtemp(prefix="mc_auto_sync_data_")
REMOTE = tempfile.mkdtemp(prefix="mc_auto_sync_remote_")
GAME = tempfile.mkdtemp(prefix="mc_auto_sync_game_")
GAME_UI = tempfile.mkdtemp(prefix="mc_auto_sync_game_ui_")

SERVER_ID = "srv-auto-0001"
SFTP_USER, SFTP_PASS = "tester", "secret"


def log_step(title):
    print(f"\n=== {title} ===")


def remote_path(rel):
    return os.path.join(REMOTE, *rel.split("/"))


def write_remote(rel, data: bytes, mtime=None):
    p = remote_path(rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))


def write_local(rel, data: bytes):
    p = os.path.join(GAME, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


def local_path(rel):
    return os.path.join(GAME, *rel.split("/"))


def read_local(rel):
    with open(local_path(rel), "rb") as f:
        return f.read()


def setup_remote():
    write_remote("client_files/mods/a.jar", b"A" * 100)
    write_remote("client_files/mods/b.jar", b"B" * 100)
    write_remote("client_files/mods/old.disabled", b"D" * 20)
    write_remote("client_files/mods/sub/nested.jar", b"N" * 80)
    write_remote("client_files/config/x.toml", b"x = 1\n")
    write_remote("client_files/config/private.toml", b"secret = 1\n")
    write_remote("client_files/resourcepacks/p.zip", b"P" * 200)
    write_remote("client_files/shaderpacks/s.zip", b"H" * 60)
    write_remote("client_files/secret.txt", b"S" * 10)
    write_remote("client_files/logs/latest.log", b"log\n")
    os.makedirs(remote_path("todo"), exist_ok=True)


def setup_local():
    write_local("mods/a.jar", b"A" * 100)          # 内容相同
    write_local("mods/b.jar", b"B" * 90)           # 大小不同 → 更新
    write_local("mods/local-only.jar", b"L" * 50)  # 本地多余（不删除）
    write_local("mods/old.disabled", b"LOCAL")     # 黑名单，不参与
    write_local("config/x.toml", b"x = 1\n")       # 内容相同
    write_local("config/private.toml", b"LOCAL SECRET\n")  # 黑名单，不参与
    write_local("secret.txt", b"local custom\n")   # 范围外，不参与
    write_local("logs/latest.log", b"local log\n")  # 范围外，不参与


def make_policy():
    return new_policy(
        enabled=True,
        include=["mods", "config", "resourcepacks"],
        exclude=["*.disabled", "config/private.toml"],
    )


def check_match_semantics():
    policy = make_policy()
    assert path_allowed(policy, "mods/a.jar")
    assert path_allowed(policy, "mods/sub/nested.jar")
    assert path_allowed(policy, "config/x.toml")
    assert not path_allowed(policy, "mods/old.disabled"), "黑名单应优先"
    assert not path_allowed(policy, "config/private.toml")
    assert not path_allowed(policy, "secret.txt"), "白名单外不应参与"
    assert not path_allowed(policy, "logs/latest.log")
    assert not path_allowed(policy, "mods/.hidden"), "隐藏项不应参与"
    assert not path_allowed(policy, "mods/tmp.tmp"), "临时文件不应参与"

    assert dir_allowed(policy, "")
    assert dir_allowed(policy, "mods")
    assert dir_allowed(policy, "mods/sub")
    assert not dir_allowed(policy, "logs")

    empty = normalize_policy({"enabled": True, "include": [],
                              "exclude": []})
    assert not path_allowed(empty, "mods/a.jar")
    assert not dir_allowed(empty, "")

    assert glob_match("mods/*.jar", "mods/a.jar")
    assert not glob_match("mods/*.jar", "mods/sub/nested.jar")
    assert glob_match("mods/**/*.jar", "mods/sub/nested.jar")
    assert glob_match("mods/**", "mods/sub/nested.jar")
    assert glob_match("**/*.toml", "config/x.toml")

    # 黑名单目录：整个子树排除
    policy2 = normalize_policy({"enabled": True,
                                "include": ["mods"],
                                "exclude": ["mods/private"]})
    assert not dir_allowed(policy2, "mods/private")
    assert not path_allowed(policy2, "mods/private/x.jar")
    assert path_allowed(policy2, "mods/public.jar")

    # 非法条目（.. / 绝对路径）被丢弃
    policy3 = normalize_policy({"enabled": True,
                                "include": ["mods", "../etc", "/abs/path"], })
    assert all(".." not in item for item in policy3["include"])
    assert all(not item.startswith("/") for item in policy3["include"])
    print("黑白名单 / glob / 路径安全语义正确")


def check_policy_roundtrip(sftp):
    policy = new_policy(enabled=True, include=["mods", "config"],
                        exclude=["*.disabled"], note="测试策略")
    save_policy(sftp, "/todo", policy)
    assert sftp.exists("/todo/auto_sync.json")
    loaded = load_policy(sftp, "/todo")
    assert loaded is not None
    assert loaded["enabled"] is True
    assert loaded["include"] == ["mods", "config"]
    assert loaded["exclude"] == ["*.disabled"]
    assert policy_signature(loaded) == policy_signature(policy)
    # 策略文件损坏时应明确报错，而不是静默当成「未下发」
    sftp.write_text("/todo/auto_sync.json", "{not json")
    try:
        load_policy(sftp, "/todo")
        raise AssertionError("损坏的策略本应报错")
    except Exception as exc:
        assert "解析" in str(exc), exc

    default = default_policy()
    assert default["enabled"] is False
    assert default["include"] == []
    print("策略下发/读取/损坏检测正确")


def check_plan_and_apply(sftp):
    policy = make_policy()
    plan = plan_sync(sftp, "/client_files", GAME, policy)
    by_rel = {item["rel"]: item for item in plan["downloads"]}

    assert "mods/b.jar" in by_rel and by_rel["mods/b.jar"]["action"] == "update"
    assert "resourcepacks/p.zip" in by_rel
    assert "mods/a.jar" not in by_rel, "内容相同不应下载"
    assert "config/x.toml" not in by_rel
    assert "mods/old.disabled" not in by_rel, "黑名单文件不应下载"
    assert "config/private.toml" not in by_rel
    assert "secret.txt" not in by_rel
    assert "logs/latest.log" not in by_rel

    extras = {item["rel"] for item in plan["extras"]}
    assert "mods/local-only.jar" in extras
    assert "secret.txt" not in extras, "范围外的本地文件不应出现在 extras"
    assert plan["remote_files"] == 5, plan["remote_files"]

    # 同大小但内容被替换：服务器支持远程哈希时靠哈希发现
    write_remote("client_files/mods/a.jar", b"Z" * 100)
    def fake_hashes(paths, algo="md5", batch=32, progress_cb=None):
        out = {}
        for p in paths:
            host_path = os.path.join(REMOTE, *p.lstrip("/").split("/"))
            with open(host_path, "rb") as f:
                out[p] = hashlib.md5(f.read()).hexdigest()
        return out
    original = sftp.remote_hash_many
    sftp.remote_hash_many = fake_hashes
    try:
        plan = plan_sync(sftp, "/client_files", GAME, policy)
        by_rel = {item["rel"]: item for item in plan["downloads"]}
        assert "mods/a.jar" in by_rel, f"同大小内容替换漏检: {plan['downloads']}"
    finally:
        sftp.remote_hash_many = original

    # 服务器不支持远程哈希（假服务器 OP_UNSUPPORTED）→ 退化为修改时间判断
    write_remote("client_files/mods/a.jar", b"A" * 100)  # 恢复内容
    os.remove(local_path("mods/a.jar"))
    write_local("mods/a.jar", b"A" * 100)  # 本地内容相同但 mtime 更新
    plan = plan_sync(sftp, "/client_files", GAME, policy)
    assert "mods/a.jar" not in {i["rel"] for i in plan["downloads"]}, \
        "mtime 相同时不应反复下载"
    import time
    newer = int(time.time()) + 120
    write_remote("client_files/mods/a.jar", b"Q" * 100,
                 mtime=newer)  # 大小相同、mtime 更新
    plan = plan_sync(sftp, "/client_files", GAME, policy)
    assert "mods/a.jar" in {i["rel"] for i in plan["downloads"]}, \
        "服务器不支持哈希时应靠 mtime 发现更新"
    # 恢复为原始内容，再测试下载执行
    old_mtime = int(os.stat(local_path("mods/a.jar")).st_mtime) - 10
    write_remote("client_files/mods/a.jar", b"A" * 100, mtime=old_mtime)

    plan = plan_sync(sftp, "/client_files", GAME, policy)
    summary = apply_sync(sftp, "/client_files", GAME, plan["downloads"])
    assert not summary["errors"], summary["errors"]
    assert read_local("mods/b.jar") == b"B" * 100
    assert read_local("resourcepacks/p.zip") == b"P" * 200
    assert os.path.isfile(local_path("mods/local-only.jar")), \
        "本地多余文件不能被删除"
    assert read_local("config/private.toml") == b"LOCAL SECRET\n", \
        "黑名单文件不能被覆盖"
    assert read_local("secret.txt") == b"local custom\n", \
        "白名单外文件不能被覆盖"
    # 再跑一次应已是最新
    plan2 = plan_sync(sftp, "/client_files", GAME, policy)
    assert not plan2["downloads"], plan2["downloads"]
    print(f"对比/下载正确：首次下载 {len(summary['installed'])} 个，"
          f"复检无差异")


def check_server_page(host, port):
    server_cfg = ServerConfig(os.path.join(DATA, "server_config.json"))
    page = AutoSyncPage(server_cfg)  # 此时无 SFTP 配置，不会启动读取线程
    server_cfg.sftp = {"host": host, "port": port,
                       "username": SFTP_USER, "password": SFTP_PASS}
    server_cfg.remote = {"todo_dir": "/todo", "files_dir": "/client_files"}

    page.cb_enabled.setChecked(True)
    page.ed_include.setPlainText(
        "mods\nconfig\n# 注释行会被忽略\n../etc\nresourcepacks")
    page.ed_exclude.setPlainText("*.disabled")
    policy = page._collect_policy()
    assert policy["include"] == ["mods", "config", "resourcepacks"], \
        policy["include"]
    assert all(".." not in item for item in policy["include"])

    info = page._sftp_args()
    saved = json.loads(page._do_save(
        info, json.dumps(policy, ensure_ascii=False)))
    assert saved["enabled"] is True
    assert sftp_exists("/todo/auto_sync.json")
    loaded = json.loads(page._do_load(info))
    assert loaded["policy"]["enabled"] is True
    assert loaded["policy"]["include"] == ["mods", "config", "resourcepacks"]

    preview = json.loads(page._do_preview(
        info, json.dumps(policy, ensure_ascii=False)))
    assert preview["count"] == 6, preview  # mods(4) + config(2) + resourcepacks(1) - 黑名单 1
    assert "mods" in preview["by_top"]
    print(f"服务端策略页：保存/读回/预览正确（预览 {preview['count']} 个文件）")


def sftp_exists(remote_rel):
    with SFTPManager("127.0.0.1", srv.port, SFTP_USER, SFTP_PASS) as conn:
        return conn.exists(remote_rel)


def wait_until(pred, timeout=10.0) -> bool:
    import time
    start = time.time()
    while time.time() - start < timeout:
        APP.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    return False


def check_page_worker_flow(host, port):
    """走完整 Worker 线程 + 信号回填流程（验证页面实际打开时的读取路径）。"""
    server_cfg = ServerConfig(os.path.join(DATA, "server_config_ui.json"))
    server_cfg.sftp = {"host": host, "port": port,
                       "username": SFTP_USER, "password": SFTP_PASS}
    server_cfg.remote = {"todo_dir": "/todo", "files_dir": "/client_files"}
    page = AutoSyncPage(server_cfg)
    page.reload()  # 模拟打开标签页（页面不在屏幕上时不会自动触发 showEvent）
    assert wait_until(lambda: page._load_worker is None, 15), "读取策略线程超时"
    assert page.cb_enabled.isChecked(), "应回填服务器上已启用的策略"
    assert page.ed_include.toPlainText().strip(), "白名单应回填到表单"
    print("服务端策略页：Worker 线程读取/回填正常")

    from client_app.ui.main_window import ClientMainWindow

    cfg = ClientConfig(os.path.join(DATA, "client_ui_config.json"))
    cfg.local_mc_dir = GAME
    cfg.auto_update_check = False  # 避免测试期间发起软件本体更新检查
    win = ClientMainWindow(cfg)
    assert not win.cb_auto_sync.isChecked()
    assert not win.btn_auto_now.isEnabled()
    cfg.auto_sync_enabled = True
    win._restart_auto_timer()
    assert win.timer_auto.isActive(), "启用后自动同步定时器应启动"
    win.timer_auto.stop()
    win.tray.hide()
    win.deleteLater()
    APP.processEvents()
    print("客户端主界面：自动同步开关/间隔/定时器状态正确")


def check_client_engine(host, port):
    client_id = new_client_id()
    with SFTPManager(host, port, SFTP_USER, SFTP_PASS) as sftp:
        save_ids(sftp, [client_id])
    content = build_profile_content("自动同步测试服", SERVER_ID, {
        "host": host, "port": port, "username": SFTP_USER,
        "password": SFTP_PASS, "todo_dir": "/todo",
        "files_dir": "/client_files", "c2c_dir": "/c2c",
    }, client_id=client_id)
    cfg = ClientConfig(os.path.join(DATA, "client_config.json"))
    cfg.add_profile("自动同步测试服", SERVER_ID, content,
                    client_id=client_id)
    cfg.local_mc_dir = GAME
    profile = cfg.profile_by_id(SERVER_ID)

    # 默认：本地开关关闭、间隔 30 分钟
    assert cfg.auto_sync_enabled is False
    assert cfg.auto_sync_interval_min == 30
    cfg.auto_sync_enabled = True
    cfg.auto_sync_interval_min = 15
    assert cfg.auto_sync_enabled is True
    assert cfg.auto_sync_interval_min == 15

    # 1) 策略缺失
    with SFTPManager(host, port, SFTP_USER, SFTP_PASS) as sftp:
        sftp.delete("/todo/auto_sync.json")
    result = auto_sync_run(cfg, profile, apply=True)
    assert result["skipped"] == "policy_missing", result

    # 2) 策略关闭
    with SFTPManager(host, port, SFTP_USER, SFTP_PASS) as sftp:
        disabled = new_policy(enabled=False, include=["mods"])
        save_policy(sftp, "/todo", disabled)
    result = auto_sync_run(cfg, profile, apply=True)
    assert result["skipped"] == "policy_disabled", result

    # 3) 白名单为空
    with SFTPManager(host, port, SFTP_USER, SFTP_PASS) as sftp:
        empty = new_policy(enabled=True, include=[])
        save_policy(sftp, "/todo", empty)
    result = auto_sync_run(cfg, profile, apply=True)
    assert result["skipped"] == "policy_empty", result

    # 4) 正常启用：只同步 mods，不碰 config（白名单外）
    write_remote("client_files/mods/engine-mod.jar", b"E" * 70)
    with SFTPManager(host, port, SFTP_USER, SFTP_PASS) as sftp:
        save_policy(sftp, "/todo", new_policy(
            enabled=True, include=["mods"], exclude=["*.disabled"]))
    result = auto_sync_run(cfg, profile, apply=True)
    assert not result["skipped"], result
    installed = set(result["installed"])
    assert "mods/engine-mod.jar" in installed, result
    assert os.path.isfile(local_path("mods/engine-mod.jar"))
    assert read_local("mods/engine-mod.jar") == b"E" * 70
    assert not os.path.exists(local_path("shaderpacks/s.zip")), \
        "白名单外文件不应被自动下载"
    # 本地多余文件仍保留
    assert os.path.isfile(local_path("mods/local-only.jar"))
    assert cfg.profile_by_id(SERVER_ID).get("auto_sync_at") == ""  # 由界面层记录

    # 5) 再检查应无差异
    result2 = auto_sync_run(cfg, profile, apply=True)
    assert not result2["downloads"], result2["downloads"]
    assert result2["remote_files"] >= 4, result2["remote_files"]
    print(f"客户端引擎端到端：下载 {len(installed)} 个文件，复检无差异；"
          f"范围外文件未下载")


def check_client_window_auto_sync(host, port):
    """完整走客户端窗口的自动同步线程：检测 → 下载 → 记录状态。"""
    from client_app.ui.main_window import ClientMainWindow

    client_id = new_client_id()
    with SFTPManager(host, port, SFTP_USER, SFTP_PASS) as sftp:
        save_ids(sftp, [client_id])
    content = build_profile_content("自动同步窗口测试服", SERVER_ID, {
        "host": host, "port": port, "username": SFTP_USER,
        "password": SFTP_PASS, "todo_dir": "/todo",
        "files_dir": "/client_files", "c2c_dir": "/c2c",
    }, client_id=client_id)
    cfg = ClientConfig(os.path.join(DATA, "client_ui_config2.json"))
    cfg.add_profile("自动同步窗口测试服", SERVER_ID, content,
                    client_id=client_id)
    cfg.local_mc_dir = GAME_UI
    cfg.auto_sync_enabled = True
    cfg.auto_sync_interval_min = 5
    cfg.notify = False
    cfg.auto_update_check = False
    win = ClientMainWindow(cfg)
    win.timer_auto.stop()
    try:
        assert win.cb_auto_sync.isChecked()
        win._auto_sync(manual=False)
        assert wait_until(lambda: win._auto_sync_thread is None, 30), \
            "自动同步线程超时"
        assert os.path.isfile(os.path.join(GAME_UI, "mods", "a.jar")), \
            "窗口自动同步未下载 mods/a.jar"
        profile = cfg.profile_by_id(SERVER_ID)
        assert profile.get("auto_sync_at"), "未记录自动同步时间"
        assert int(profile.get("auto_sync_files") or 0) > 0
        assert "自动同步" in win.lbl_auto_status.text()
    finally:
        win.tray.hide()
        win.deleteLater()
        APP.processEvents()
    print("客户端窗口自动同步：线程下载/状态记录正常")


srv = FakeSFTPServer(REMOTE)
host, port = srv.start()
try:
    setup_remote()
    setup_local()

    log_step("1. 黑白名单匹配语义")
    check_match_semantics()

    log_step("2. 策略下发/读取")
    with SFTPManager(host, port, SFTP_USER, SFTP_PASS) as sftp:
        check_policy_roundtrip(sftp)

    log_step("3. 对比计划与下载执行")
    with SFTPManager(host, port, SFTP_USER, SFTP_PASS) as sftp:
        check_plan_and_apply(sftp)

    log_step("4. 服务端策略页")
    check_server_page(host, port)

    log_step("4b. 服务端策略页 Worker 流程 + 客户端界面开关")
    check_page_worker_flow(host, port)

    log_step("5. 客户端引擎端到端")
    check_client_engine(host, port)

    log_step("6. 客户端窗口自动同步")
    check_client_window_auto_sync(host, port)

    print("\nAUTO SYNC OK：策略/黑白名单/对比/下载/客户端引擎全部通过")
finally:
    srv.stop()
    for d in (DATA, REMOTE, GAME, GAME_UI):
        shutil.rmtree(d, ignore_errors=True)
