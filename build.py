#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键构建脚本：生成图标 → PyInstaller 打包（单文件、无控制台）→ 汇总发布目录。

用法：
    python build.py
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = sys.executable


def run(cmd, **kw):
    print(">>", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=kw.pop("cwd", ROOT), **kw)


def build_icons() -> tuple[str, str]:
    print("=== 1/3 生成图标 ===")
    assets = ROOT / "assets"
    assets.mkdir(exist_ok=True)
    client_ico = assets / "icon_client.ico"
    server_ico = assets / "icon_server.ico"
    run([VENV_PY, "tools/make_icon.py", "client", str(assets)])
    run([VENV_PY, "tools/make_icon.py", "server", str(assets)])
    # 图标以 base64 内嵌进代码，运行时无需任何外部素材文件
    run([VENV_PY, "tools/gen_icons_data.py"])
    return str(client_ico), str(server_ico)


def build_exe(entry: str, name: str, icon: str, admin: bool):
    print(f"=== 2/3 打包 {name} ===")
    cmd = [
        VENV_PY, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--noconsole",
        "--name", name,
        "--icon", icon,
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        str(ROOT / entry),
    ]
    if admin:
        # 客户端请求管理员权限（exe 图标带盾牌）
        cmd.insert(cmd.index("--onefile") + 1, "--uac-admin")
    run(cmd)


def assemble(client_ico: str, server_ico: str):
    print("=== 3/3 汇总发布目录 ===")
    dist = ROOT / "dist"
    release = ROOT / "release"
    shutil.rmtree(release, ignore_errors=True)
    release.mkdir(exist_ok=True)

    server_dir = release / "MinecraftSyncServer"
    client_dir = release / "MinecraftSyncClient"
    server_dir.mkdir()
    client_dir.mkdir()

    # 单文件 exe
    shutil.copy(dist / "MinecraftSyncServer.exe", server_dir / "MinecraftSyncServer.exe")
    shutil.copy(dist / "MinecraftSyncClient.exe", client_dir / "MinecraftSyncClient.exe")

    # 图标（供 Inno Setup 引用）
    icons_dir = release / "installer_assets"
    icons_dir.mkdir()
    shutil.copy(client_ico, icons_dir / "icon_client.ico")
    shutil.copy(server_ico, icons_dir / "icon_server.ico")
    print("发布目录: ", release)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-icons", action="store_true", help="跳过图标生成")
    args = parser.parse_args()

    client_ico = str(ROOT / "assets" / "icon_client.ico")
    server_ico = str(ROOT / "assets" / "icon_server.ico")
    if not args.skip_icons or not (Path(client_ico).exists() and Path(server_ico).exists()):
        client_ico, server_ico = build_icons()
    build_exe("entry_server.py", "MinecraftSyncServer", server_ico, admin=False)
    build_exe("entry_client.py", "MinecraftSyncClient", client_ico, admin=True)
    assemble(client_ico, server_ico)
    print("\n构建完成。使用 Inno Setup 编译 installer/setup.iss 生成安装程序。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
