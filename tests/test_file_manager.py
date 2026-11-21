# -*- coding: utf-8 -*-
"""文件管理（服务端手动管理）核心逻辑回归测试。

覆盖：
1. 分类总览：固定分类 / 不存在分类 / 其他顶层条目；
2. 启用 / 禁用：加去 .disabled 后缀、冲突检测、目录与根目录顶层保护；
3. 重命名 / 新建目录 / 删除（含服务端回收站归档）；
4. 拖拽上传：本地文件/文件夹展开、目标路径映射、覆盖 / 跳过；
5. SSHFS-Win / rclone 映射命令生成。

用法（仓库根目录）：
    python tests/test_file_manager.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app_common import file_manager as fm
from app_common import sftp_mount
from app_common.history import HistoryStore, load_index
from app_common.sftp import SFTPManager
from tests.fake_sftp import FakeSFTPServer

HOST_USER = "tester"
HOST_PASS = "secret"


def _write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _make_tree(root: str) -> None:
    _write(os.path.join(root, "mods", "create-1.0.jar"), b"create")
    _write(os.path.join(root, "mods", "jei-1.0.jar.disabled"), b"jei")
    _write(os.path.join(root, "mods", "dup.jar"), b"dup-a")
    _write(os.path.join(root, "mods", "dup.jar.disabled"), b"dup-b")
    _write(os.path.join(root, "resourcepacks", "faithful.zip"), b"pack")
    _write(os.path.join(root, "shaderpacks", "bsl.zip"), b"shader")
    _write(os.path.join(root, "plugins", "essentials.jar"), b"plugin")
    _write(os.path.join(root, "config", "server.toml"), b"[server]\n")
    _write(os.path.join(root, "start.sh"), b"#!/bin/sh\n")
    os.makedirs(os.path.join(root, "world", "region"), exist_ok=True)


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="file_mgr_")
    _make_tree(tmp)
    srv = FakeSFTPServer(tmp)
    host, port = srv.start()
    try:
        with SFTPManager(host, port, HOST_USER, HOST_PASS) as sftp:
            print("=== 1. 分类总览 ===")
            data = fm.scan_overview(sftp, "/")
            by_key = {c["key"]: c for c in data["categories"]}
            assert by_key["mods"]["exists"] is True
            assert len(by_key["mods"]["entries"]) == 4
            assert by_key["resourcepacks"]["exists"] is True
            assert by_key["shaderpacks"]["exists"] is True
            assert by_key["plugins"]["exists"] is True
            assert by_key["datapacks"]["exists"] is False
            others = {e["name"] for e in
                      (by_key[fm.OTHER_KEY]["entries"])}
            assert {"start.sh", "world"} <= others, others
            print(f"分类总览正确：{len(data['categories'])} 个分类，"
                  f"其他 {len(others)} 项")

            print("=== 2. 启用 / 禁用 ===")
            summary = fm.toggle_remote(sftp, "/", ["mods/create-1.0.jar"], False)
            assert summary["ok"] == ["mods/create-1.0.jar.disabled"], summary
            assert os.path.isfile(os.path.join(tmp, "mods",
                                               "create-1.0.jar.disabled"))
            summary = fm.toggle_remote(sftp, "/",
                                       ["mods/jei-1.0.jar.disabled"], True)
            assert summary["ok"] == ["mods/jei-1.0.jar"], summary
            assert os.path.isfile(os.path.join(tmp, "mods", "jei-1.0.jar"))
            # 同名启用版与禁用版并存 → 冲突，不做覆盖
            summary = fm.toggle_remote(sftp, "/", ["mods/dup.jar"], False)
            assert summary["conflicts"] == ["mods/dup.jar"], summary
            assert os.path.isfile(os.path.join(tmp, "mods", "dup.jar"))
            # 目录 / 根目录顶层
            summary = fm.toggle_remote(sftp, "/",
                                       ["world/region", "start.sh"], False)
            assert summary["skipped"] == ["world/region"], summary
            assert summary["errors"], summary
            assert os.path.isfile(os.path.join(tmp, "start.sh"))
            print("启用/禁用、冲突与保护规则正确")

            print("=== 3. 重命名 / 新建目录 ===")
            new_rel = fm.rename_remote(sftp, "/", "mods/jei-1.0.jar",
                                       "jei-2.0.jar")
            assert new_rel == "mods/jei-2.0.jar"
            assert os.path.isfile(os.path.join(tmp, "mods", "jei-2.0.jar"))
            try:
                fm.rename_remote(sftp, "/", "mods/jei-2.0.jar", "dup.jar")
                raise AssertionError("同名冲突应报错")
            except ValueError:
                pass
            new_dir = fm.new_folder(sftp, "/", "mods", "newpack")
            assert new_dir == "mods/newpack"
            assert os.path.isdir(os.path.join(tmp, "mods", "newpack"))
            print("重命名 / 新建目录正确")

            print("=== 4. 删除 + 回收站归档 ===")
            store = HistoryStore(sftp, "/")
            summary = fm.delete_remote(sftp, "/", ["mods/jei-2.0.jar"],
                                       store, "server")
            assert summary["ok"] == ["mods/jei-2.0.jar"], summary
            assert summary["archived"] == 1
            assert not os.path.exists(os.path.join(tmp, "mods", "jei-2.0.jar"))
            store.save()
            index = load_index(sftp, "/")
            assert len(index["entries"]) == 1, index
            archived = index["entries"][0]
            assert os.path.isfile(os.path.join(
                tmp, ".mc-sync-history", archived["id"], "server",
                "mods", "jei-2.0.jar"))
            # 目录：直接递归删除
            summary = fm.delete_remote(sftp, "/", ["mods/newpack"])
            assert summary["ok"] == ["mods/newpack"], summary
            assert not os.path.exists(os.path.join(tmp, "mods", "newpack"))
            # 根目录顶层保护
            summary = fm.delete_remote(sftp, "/", ["start.sh"])
            assert summary["errors"], summary
            assert os.path.isfile(os.path.join(tmp, "start.sh"))
            print("删除 / 回收站归档 / 顶层保护正确")

            print("=== 5. 拖拽上传 ===")
            local_dir = os.path.join(tmp, "local_upload")
            _write(os.path.join(local_dir, "packA", "a.jar"), b"AAA")
            _write(os.path.join(local_dir, "packA", "sub", "b.jar"), b"BBB")
            _write(os.path.join(local_dir, "packA", ".hidden"), b"x")
            _write(os.path.join(local_dir, "packA", "x.tmp"), b"x")
            items = fm.collect_local_items([os.path.join(local_dir, "packA")])
            rels = sorted(rel for _f, rel, _s in items)
            assert rels == ["packA/a.jar", "packA/sub/b.jar"], rels
            mapped = fm.target_rels("resourcepacks", items)
            assert {rel for _f, rel, _s in mapped} == {
                "resourcepacks/packA/a.jar",
                "resourcepacks/packA/sub/b.jar"}
            conflicts = fm.find_conflicts(
                sftp, "/", ["resourcepacks/packA/a.jar"])
            assert conflicts == []
            summary = fm.upload_items(sftp, "/", mapped, overwrite=True)
            assert len(summary["uploaded"]) == 2, summary
            assert os.path.isfile(os.path.join(
                tmp, "resourcepacks", "packA", "sub", "b.jar"))
            # 再传一次：skip 模式应跳过、overwrite 模式应成功
            summary = fm.upload_items(sftp, "/", mapped, overwrite=False)
            assert len(summary["skipped"]) == 2, summary
            summary = fm.upload_items(sftp, "/", mapped, overwrite=True)
            assert len(summary["uploaded"]) == 2, summary
            print("拖拽上传展开 / 路径映射 / 覆盖跳过正确")

        print("=== 6. 映射为本地磁盘命令 ===")
        cmd = sftp_mount.sshfs_net_use("example.com", 22, "mc", "/srv/mc")
        assert cmd.startswith('net use Z: "\\\\sshfs.r\\mc@example.com'), cmd
        cmd2 = sftp_mount.sshfs_net_use("example.com", 2222, "mc", "/srv/mc")
        assert "!2222" in cmd2, cmd2
        help_data = sftp_mount.mount_help("example.com", 22, "mc", "/srv/mc")
        assert len(help_data["commands"]) == 2
        assert any("PCL" in note for note in help_data["notes"])
        print("SSHFS-Win / rclone 命令生成正确")
    finally:
        srv.stop()
    print("\nFILE MANAGER OK：分类/启用禁用/重命名/删除回收站/上传/映射全部通过")


if __name__ == "__main__":
    main()
