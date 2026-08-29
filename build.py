#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键构建脚本：生成图标 → PyInstaller 打包（单文件、无控制台）→ 汇总发布目录 → Inno Setup 编译安装程序。

用法：
    python build.py                # 完整构建（图标 + 双端 exe + 汇总 + 安装程序）
    python build.py --skip-icons   # 跳过图标生成（已有图标时更快）
    python build.py --skip-setup   # 跳过安装程序编译（未安装 Inno Setup 时）
    python build.py --gitee        # 构建完成后自动同步 Gitee（需环境变量 GITEE_TOKEN）
    python build.py --gitee --gitee-body-file release_note.txt  # 附版本介绍

前置条件：
    - Python 3 + .venv（PyInstaller、PySide6、paramiko 等）
    - Inno Setup 6（用于编译 Windows 安装程序，自动定位 ISCC.exe；
      未安装时可通过 winget install JRSoftware.InnoSetup 安装）
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = sys.executable

# Inno Setup 编译器（ISCC.exe）常见安装位置
ISCC_CANDIDATES = (
    r"D:\Inno Setup 7\ISCC.exe",
    r"D:\Inno Setup 6\ISCC.exe",
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
    r"C:\Program Files (x86)\Inno Setup 5\ISCC.exe",
    r"C:\Program Files\Inno Setup 7\ISCC.exe",
)


def run(cmd, **kw):
    print(">>", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=kw.pop("cwd", ROOT), **kw)


def read_app_version() -> str:
    """从 app_common/constants.py 读取当前版本号（如 1.0.0）。"""
    try:
        text = (ROOT / "app_common" / "constants.py").read_text(encoding="utf-8")
        m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', text)
        if m:
            return m.group(1).strip()
    except OSError:
        pass
    return "1.0.0"


def find_iscc() -> str | None:
    """定位 Inno Setup 编译器 ISCC.exe（环境变量 → 常见路径 → 注册表 → PATH）。"""
    env = os.environ.get("ISCC") or os.environ.get("INNO_SETUP_HOME")
    if env and os.path.isfile(env):
        return env
    for cand in ISCC_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    # 注册表查询安装位置（32/64 位注册表视图）
    if sys.platform == "win32":
        import winreg
        for sub in ("6", "5"):
            try:
                for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                    try:
                        with winreg.OpenKey(
                                winreg.HKEY_LOCAL_MACHINE,
                                rf"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup {sub}_is1",
                                0, winreg.KEY_READ | view) as key:
                            loc, _ = winreg.QueryValueEx(key, "InstallLocation")
                        iscc = os.path.join(str(loc), "ISCC.exe")
                        if os.path.isfile(iscc):
                            return iscc
                    except OSError:
                        continue
            except OSError:
                pass
    shim = shutil.which("ISCC")
    if shim:
        return shim
    return None


def build_icons() -> tuple[str, str]:
    print("=== 1/4 生成图标 ===")
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
    print(f"=== 2/4 打包 {name} ===")
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
    print("=== 3/4 汇总发布目录 ===")
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


def gitee_sync(version: str, body_file: str = "") -> int:
    """调用 tools/gitee_sync.py：同步代码到 Gitee + 创建/更新发行版 + 上传 3 个附件。

    需要设置环境变量 GITEE_TOKEN（Gitee 私人令牌），否则仅打印提示。
    """
    print("=== 5/5 同步 Gitee ===")
    release = ROOT / "release"
    setup = release / f"MinecraftModsCloudSync_Setup_{version}.exe"
    tag = f"v{version}" if not str(version).startswith("v") else str(version)
    cmd = [
        VENV_PY, str(ROOT / "tools" / "gitee_sync.py"),
        "--repo", "MinecraftModsCloudSync",
        "--tag", tag,
        "--name", tag,
        "--assets",
        str(release / "MinecraftSyncClient" / "MinecraftSyncClient.exe"),
        str(release / "MinecraftSyncServer" / "MinecraftSyncServer.exe"),
        str(setup),
    ]
    if body_file:
        cmd += ["--body-file", str(body_file)]
    if not os.environ.get("GITEE_TOKEN"):
        print("!! 未设置环境变量 GITEE_TOKEN，跳过 Gitee 同步。")
        print("   设置方法：set GITEE_TOKEN=你的私人令牌，然后重新运行 python build.py --gitee")
        return 1
    run(cmd)
    return 0


def compile_setup(version: str) -> Path | None:
    """调用 Inno Setup 编译器（ISCC.exe）生成 Windows 安装程序（输出到 release\\）。

    版本号通过 /D 预处理器覆盖 setup.iss 中的 MyAppVersion，与代码版本保持同步。
    返回安装程序路径；找不到 ISCC 或编译失败返回 None。
    """
    print(f"=== 4/4 编译安装程序（版本 {version}）===")
    iscc = find_iscc()
    if not iscc:
        print("!! 未找到 Inno Setup 编译器（ISCC.exe）。")
        print("   请安装 Inno Setup（winget install JRSoftware.InnoSetup），")
        print("   或在系统环境变量中设置 ISCC 指向 ISCC.exe 的完整路径。")
        print("   安装后重新运行 python build.py 即可自动生成安装程序。")
        return None
    print(f"ISCC: {iscc}")
    run([iscc, f"/DMyAppVersion={version}", str(ROOT / "installer" / "setup.iss")])
    setup = ROOT / "release" / f"MinecraftModsCloudSync_Setup_{version}.exe"
    if setup.exists():
        print("安装程序: ", setup)
        return setup
    print("!! ISCC 编译完成但未找到预期安装程序，请检查 installer/setup.iss 的输出配置。")
    return None


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
    parser.add_argument("--skip-setup", action="store_true",
                        help="跳过安装程序编译（Inno Setup 未安装时）")
    parser.add_argument("--gitee", action="store_true",
                        help="构建完成后自动同步 Gitee（代码 + 发行版 + 附件，需 GITEE_TOKEN）")
    parser.add_argument("--gitee-body-file", default="",
                        help="Gitee 发行版版本介绍文本文件（UTF-8），配合 --gitee 使用")
    args = parser.parse_args()

    version = read_app_version()
    print(f"当前版本: {version}")

    client_ico = str(ROOT / "assets" / "icon_client.ico")
    server_ico = str(ROOT / "assets" / "icon_server.ico")
    if not args.skip_icons or not (Path(client_ico).exists() and Path(server_ico).exists()):
        client_ico, server_ico = build_icons()
    build_exe("entry_server.py", "MinecraftSyncServer", server_ico, admin=False)
    build_exe("entry_client.py", "MinecraftSyncClient", client_ico, admin=False)
    assemble(client_ico, server_ico)
    if args.skip_setup:
        print("\n已跳过安装程序编译。如需生成安装程序，请安装 Inno Setup 后运行：")
        print(f"  python build.py")
        return 0
    setup = compile_setup(version)
    if setup is None:
        return 1
    print("\n构建完成。发布产物（release 目录）：")
    print(f"  {ROOT / 'release' / 'MinecraftSyncClient' / 'MinecraftSyncClient.exe'}")
    print(f"  {ROOT / 'release' / 'MinecraftSyncServer' / 'MinecraftSyncServer.exe'}")
    print(f"  {setup}")
    if args.gitee:
        gitee_sync(version, args.gitee_body_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
