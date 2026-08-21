#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键构建脚本：生成图标 → PyInstaller 打包（单文件、无控制台）→ 汇总发布目录。

用法：
    python build.py
"""
import argparse
import os
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
    # 打包前结束同名进程：PyInstaller 需覆盖 dist 下的旧 exe，
    # 若旧实例仍在运行（文件被占用）会报 PermissionError [WinError 5] 拒绝访问。
    subprocess.run(["taskkill", "/F", "/IM", f"{name}.exe"],
                   capture_output=True, check=False)
    cmd = [
        VENV_PY, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--noconsole",
        "--name", name,
        "--icon", icon,
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        # 强制收集本地包的全部子模块：PyInstaller 对 app_common 的
        # 静态分析会漏掉 app_config 等模块，导致打包后 exe 运行报
        # "No module named 'app_common.app_config'"。
        "--collect-submodules", "app_common",
        "--collect-submodules", "client_app",
        "--collect-submodules", "server_app",
        # MCMod（MC 百科）数据库：mcmod.buf（PCL CE 数据，621KB）
        "--add-data",
        f"{ROOT / 'app_common' / 'data' / 'mcmod.buf'};app_common/data",
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
    server_dir.mkdir(exist_ok=True)
    client_dir.mkdir(exist_ok=True)

    # 单文件 exe
    shutil.copy(dist / "MinecraftSyncServer.exe", server_dir / "MinecraftSyncServer.exe")
    shutil.copy(dist / "MinecraftSyncClient.exe", client_dir / "MinecraftSyncClient.exe")

    # 图标（供 Inno Setup 引用）
    icons_dir = release / "installer_assets"
    icons_dir.mkdir()
    shutil.copy(client_ico, icons_dir / "icon_client.ico")
    shutil.copy(server_ico, icons_dir / "icon_server.ico")
    print("发布目录: ", release)


def _ensure_venv() -> None:
    """强制使用项目 .venv 解释器打包。

    系统 Python（尤其 3.13）下 PyInstaller 收集模块异常，会漏掉
    app_common 子模块，导致打包出的 exe 运行时报
    "No module named 'app_common.app_config'"。这里自动切换到 .venv。
    """
    venv_py = ROOT / ".venv" / "Scripts" / "python.exe"
    if not venv_py.exists():
        return
    if Path(sys.executable).resolve() == venv_py.resolve():
        return
    print(f">> 当前解释器不是 .venv（{sys.executable}），自动切换到 .venv\\Scripts\\python.exe 重新执行")
    os.execv(str(venv_py), [str(venv_py), str(ROOT / "build.py")] + sys.argv[1:])


def main() -> int:
    _ensure_venv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-icons", action="store_true", help="跳过图标生成")
    args = parser.parse_args()

    client_ico = str(ROOT / "assets" / "icon_client.ico")
    server_ico = str(ROOT / "assets" / "icon_server.ico")
    if not args.skip_icons or not (Path(client_ico).exists() and Path(server_ico).exists()):
        client_ico, server_ico = build_icons()
    build_exe("entry_server.py", "MinecraftSyncServer", server_ico, admin=False)
    build_exe("entry_client.py", "MinecraftSyncClient", client_ico, admin=False)
    assemble(client_ico, server_ico)
    print("\n构建完成。使用 Inno Setup 编译 installer/setup.iss 生成安装程序。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
