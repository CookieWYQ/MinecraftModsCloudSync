# -*- coding: utf-8 -*-
"""把服务器文件「假装成本地」的辅助：检测 SSHFS-Win / rclone 并生成映射命令。

**为什么需要它**：PCL 等启动器只认本地磁盘上的游戏目录，没法直接往 SFTP 里下载。
如果本机装了 SSHFS-Win（基于 WinFsp）或 rclone，可以把服务器上的
``server_root``（或 ``files_dir``）映射成一个盘符，PCL 选择该盘符下的目录即可
把模组 / 资源包直接下载到服务器，服务器就充当了中转站。

**限制（会在界面上说明）**：
- 需要额外安装 SSHFS-Win + WinFsp（或 rclone），本工具不自动安装；
- 网络盘延迟高，PCL 扫描大目录会慢；只建议用来往 mods / resourcepacks 里放文件，
  不建议把整个游戏目录都放在网络盘上跑启动器；
- 映射的盘符在断线后会失效，重新连接需要重新映射。
"""
import os
import shutil

SSHFS_WIN_PATHS = [
    r"C:\Program Files\SSHFS-Win\bin\sshfs.exe",
    r"C:\Program Files (x86)\SSHFS-Win\bin\sshfs.exe",
]
WINFSP_PATHS = [
    r"C:\Program Files (x86)\WinFsp\bin\winfsp-x64.dll",
    r"C:\Program Files\WinFsp\bin\winfsp-x64.dll",
]


def _find_file(paths) -> str:
    for path in paths:
        if os.path.isfile(path):
            return path
    return ""


def detect_tools() -> dict:
    """检测本机的 SSHFS-Win / rclone / WinFsp。返回路径与可用状态。"""
    sshfs = shutil.which("sshfs") or _find_file(SSHFS_WIN_PATHS)
    rclone = shutil.which("rclone")
    winfsp = _find_file(WINFSP_PATHS)
    return {
        "sshfs": sshfs or "",
        "rclone": rclone or "",
        "winfsp": winfsp or "",
        "sshfs_ready": bool(sshfs and winfsp),
    }


def _clean_root(root: str) -> str:
    return str(root or "").replace("\\", "/").strip("/")


def sshfs_net_use(host: str, port: int, username: str, remote_root: str,
                  drive: str = "Z:") -> str:
    """生成 SSHFS-Win 的 net use 命令（映射服务器绝对路径到盘符）。

    SSHFS-Win 的 UNC 规则：``\\\\sshfs.r\\用户@主机!端口\\绝对路径``，
    ``.r`` 表示以服务器根目录为基准（否则相对于用户 home）。
    """
    path = _clean_root(remote_root)
    try:
        port = int(port or 22)
    except (TypeError, ValueError):
        port = 22
    host_part = f"{username}@{host}" if username else str(host)
    if port != 22:
        host_part += f"!{port}"
    unc = f"\\\\sshfs.r\\{host_part}"
    if path:
        unc += "\\" + path.replace("/", "\\")
    return f'net use {drive} "{unc}"'


def rclone_mount(host: str, port: int, username: str, remote_root: str,
                 drive: str = "Z:") -> str:
    """生成 rclone 的按需连接串 mount 命令（需要本机已配置过 rclone 或使用连接串）。"""
    try:
        port = int(port or 22)
    except (TypeError, ValueError):
        port = 22
    path = _clean_root(remote_root)
    connection = f":sftp,host={host},port={port},user={username}:"
    if path:
        connection += "/" + path
    return (f'rclone mount "{connection}" {drive} '
            f'--vfs-cache-mode writes --network-mode')


def mount_help(host: str, port: int, username: str, remote_root: str,
               drive: str = "Z:") -> dict:
    """返回界面要展示的检测结果与建议命令。"""
    tools = detect_tools()
    commands = []
    if host:
        commands.append({
            "title": "SSHFS-Win（推荐，装好后在 cmd 里执行；会提示输入密码）",
            "command": sshfs_net_use(host, port, username, remote_root, drive),
            "available": tools["sshfs_ready"],
        })
        commands.append({
            "title": "rclone（需要先 rclone config 配置好服务器，或自行补密码参数）",
            "command": rclone_mount(host, port, username, remote_root, drive),
            "available": bool(tools["rclone"]),
        })
    notes = [
        "PCL 只能操作本地目录：映射出盘符后，在 PCL 里把「游戏目录」选到该盘符即可"
        "把整合包 / 模组下载到服务器（服务器作为中转站）。",
        "需要先安装 SSHFS-Win（自带依赖 WinFsp）或 rclone；本工具不自动安装。",
        "SSHFS-Win 下载：https://github.com/winfsp/sshfs-win/releases"
        "（安装时一并装 WinFsp；或 https://winfsp.dev/rel/）",
        "rclone 下载：https://rclone.org/downloads/",
        "网络盘有延迟，PCL 扫描大目录会偏慢；建议只把 mods / resourcepacks / "
        "shaderpacks 放在网络盘上，启动游戏仍用本地目录。",
        "断线后盘符会失效，重新执行一次上面的命令即可；映射期间服务器上的文件"
        "就是「真实文件」，本工具的文件管理页可以直接看到它们。",
        "不想装额外软件时，直接用本页的拖拽上传 / 启用禁用 / 删除功能即可，"
        "所有操作都在服务器上完成，不会把服务端文件下载到本地。",
        "这里默认映射「客户端文件仓库」（client_files），因为它的 mods / "
        "resourcepacks 结构正好是 PCL 需要的下载目标；若确实要映射服务端自己的目录，"
        "把命令里的路径换成 server_root 即可（不建议让 PCL 把客户端模组下载进服务端 "
        "mods，服务端会加载到客户端模组而报错）。",
    ]
    return {"tools": tools, "commands": commands, "notes": notes,
            "drive": drive, "remote_root": remote_root}
