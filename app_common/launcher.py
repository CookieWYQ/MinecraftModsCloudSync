# -*- coding: utf-8 -*-
"""启动器解析与已知版本库。

支持：拖入版本/整合包目录、拖入 .minecraft 根目录、拖入启动器快捷方式(.lnk)或启动器 exe，
自动解析出 Minecraft 根目录并扫描 versions 下的所有版本，持久化到已知版本库。
"""
import json
import os
import subprocess
from pathlib import Path

from .config import JsonStore
from .constants import config_dir
from .logger import get_logger

log = get_logger("launcher")

APPDATA = os.environ.get("APPDATA", "")
DEFAULT_MC_ROOT = os.path.join(APPDATA, ".minecraft")


# ---------------- 快捷方式解析 ----------------
def _item_name(item: bytes) -> str:
    """从 IDList 的文件系统项中提取文件名（offset 0x0E 处，ANSI 或 UTF-16LE）。"""
    start = 14
    if len(item) <= start:
        return ""
    aend = item.find(b"\x00", start)
    ansi = item[start:aend].decode("mbcs", errors="replace") if aend > start else None
    # 全 ASCII 的名字以 ANSI 存储，直接采用
    if ansi and all(b < 0x80 for b in item[start:aend]):
        return ansi
    uend = item.find(b"\x00\x00", start)
    if uend > start and (uend - start) % 2 == 0:
        uni = item[start:uend].decode("utf-16-le", errors="ignore")
        if uni and "\ufffd" not in uni \
                and not any(0xD800 <= ord(c) <= 0xDFFF for c in uni):
            return uni
    if ansi and "\ufffd" not in ansi:
        return ansi
    return ""


def _idlist_path(data: bytes) -> str | None:
    """从 .lnk 的 LinkTargetIDList 中解析完整路径。

    用于不含 LinkInfo 的 .lnk（Windows 对含非 ASCII 路径的快捷方式常省略
    LinkInfo，路径只存在 IDList 中）。
    """
    if len(data) < 78:
        return None
    idlist_size = int.from_bytes(data[76:78], "little")
    pos = 78
    end = 78 + idlist_size
    if end > len(data):
        return None
    parts: list[str] = []
    drive = ""
    while pos < end:
        size = int.from_bytes(data[pos:pos + 2], "little")
        if size == 0:
            break
        if size < 4 or pos + size > end:
            return None
        item = data[pos:pos + size]
        pos += size
        typ = item[2] if len(item) > 2 else 0
        if typ == 0x1F:
            continue  # 桌面 CLSID，无路径信息
        if typ == 0x2F and not drive:
            # 卷/驱动器项：offset 3 处为 ANSI "D:\"
            if len(item) >= 6 and item[4:5] == b":" and item[5:6] == b"\\":
                drive = item[3:6].decode("ascii", errors="replace")
            continue
        if typ in (0x31, 0x32, 0x35):
            name = _item_name(item)
            if name:
                parts.append(name)
    if not parts:
        return None
    path = os.path.join(*(parts if drive else parts))
    if drive:
        path = drive + path
    return os.path.normpath(path)


def _parse_lnk(lnk_path: str) -> str | None:
    """纯 Python 解析 .lnk 的目标路径（不启动任何外部进程）。

    优先读取 LinkInfo 的 LocalBasePath；不含 LinkInfo 时回退解析
    LinkTargetIDList（覆盖含中文路径的快捷方式）。均失败返回 None。
    """
    try:
        with open(lnk_path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if len(data) < 76 or data[:4] != b"L\x00\x00\x00":
        return None
    link_flags = int.from_bytes(data[20:24], "little")
    has_idlist = bool(link_flags & 0x00000001)
    has_linkinfo = bool(link_flags & 0x00000002)
    if has_linkinfo:
        # 定位 LinkInfo：76 字节头之后，若含 IDList 则需跳过其大小（含 2 字节大小字段自身）
        linkinfo_off = 76
        if has_idlist:
            if len(data) < 78:
                return None
            idlist_size = int.from_bytes(data[76:78], "little")
            linkinfo_off += 2 + idlist_size
        # LinkInfo：LinkInfoSize(4) + HeaderSize(4) + LinkInfoFlags(4) + ...
        if linkinfo_off + 28 > len(data):
            return None
        base_off = int.from_bytes(data[linkinfo_off + 16: linkinfo_off + 20], "little")
        unicode_off = int.from_bytes(data[linkinfo_off + 20: linkinfo_off + 24], "little")
        if base_off > 0:
            abs_off = linkinfo_off + base_off
            end = data.find(b"\x00", abs_off)
            if end >= abs_off:
                path = data[abs_off:end].decode("mbcs", errors="replace")
                # 优先使用 Unicode 版本的 LocalBasePath（对中文路径更准确）
                if unicode_off > 0:
                    uoff = linkinfo_off + unicode_off
                    uend = data.find(b"\x00\x00", uoff)
                    if uend > uoff:
                        uraw = data[uoff:uend]
                        if len(uraw) % 2 == 0:
                            try:
                                upath = uraw.decode("utf-16-le").rstrip("\x00")
                                if upath:
                                    path = upath
                            except UnicodeDecodeError:
                                pass
                if path and path != "\\\\":
                    return path
    if has_idlist:
        path = _idlist_path(data)
        if path:
            return path
    return None


def resolve_shortcut(lnk_path: str) -> str | None:
    """解析 .lnk，返回目标程序路径。

    优先纯 Python 解析（不启动进程、不触发安全软件提示）；
    仅当 .lnk 不含 LinkInfo 结构时回退到 PowerShell（隐藏窗口、无控制台）。
    """
    target = _parse_lnk(lnk_path)
    if target:
        return target
    try:
        ps = (
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{0}');"
            "$s.TargetPath; $s.WorkingDirectory"
        ).format(str(lnk_path).replace("'", "''"))
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-Command", ps],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
        return lines[0] if lines else None
    except Exception as exc:
        log.warning("解析快捷方式失败: %s", exc)
        return None


# ---------------- 启动器 → .minecraft 根目录 ----------------
def find_mc_roots_from_launcher(exe_path: str, work_dir: str = "") -> list[str]:
    """根据启动器位置与常见配置推断其可能对应的**所有** Minecraft 根目录。

    单独适配：
    - HMCL：读取 hmcl.json 的 gameDir（exe 同目录 / 工作目录 / %APPDATA%\\hmcl）
    - PCL2 / PCL CE：便携式，.minecraft 一般在 exe 同目录、同目录子目录或上级目录
    - 兜底：默认 %APPDATA%\\.minecraft
    """
    exe_dir = str(Path(exe_path).parent) if exe_path else ""
    candidates: list[str] = []

    # 1) HMCL：hmcl.json（exe 同目录 / 工作目录 / %APPDATA%\\hmcl）
    for base in (exe_dir, work_dir, os.path.join(APPDATA, "hmcl")):
        cfg = os.path.join(base, "hmcl.json") if base else ""
        if cfg and os.path.isfile(cfg):
            try:
                with open(cfg, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for key in ("gameDir", "gameDirectory", "game.directory"):
                    val = data.get(key)
                    if isinstance(val, str) and val:
                        if val.lower() == "default":
                            candidates.append(DEFAULT_MC_ROOT)
                        else:
                            candidates.append(os.path.abspath(val.replace("\\", "/")))
            except Exception as exc:
                log.warning("解析 HMCL 配置失败: %s", exc)

    # 2) PCL2 / PCL CE / HMCL 便携版：exe 同目录 .minecraft、
    #    同目录下每个子目录里的 .minecraft、上级目录里的 .minecraft
    for base in (exe_dir, work_dir):
        if not base:
            continue
        candidates.append(os.path.join(base, ".minecraft"))
        try:
            for sub in os.listdir(base):
                sub_dir = os.path.join(base, sub)
                if os.path.isdir(sub_dir):
                    candidates.append(os.path.join(sub_dir, ".minecraft"))
        except OSError:
            pass
    if exe_dir:
        candidates.append(os.path.join(str(Path(exe_dir).parent), ".minecraft"))

    # 3) 默认 %APPDATA%\\.minecraft
    candidates.append(DEFAULT_MC_ROOT)

    roots: list[str] = []
    for cand in candidates:
        if cand and os.path.isdir(os.path.join(cand, "versions")) and cand not in roots:
            roots.append(cand)
    return roots


# ---------------- 版本扫描 ----------------
def scan_versions(mc_root: str) -> list[str]:
    """扫描 .minecraft\\versions 下所有有效版本目录名。"""
    versions_dir = os.path.join(mc_root, "versions")
    if not os.path.isdir(versions_dir):
        return []
    result = []
    try:
        for name in os.listdir(versions_dir):
            vdir = os.path.join(versions_dir, name)
            if os.path.isdir(vdir) and os.path.isfile(os.path.join(vdir, f"{name}.json")):
                result.append(name)
    except OSError as exc:
        log.warning("扫描版本失败: %s", exc)
    return sorted(result)


# ---------------- 已知版本库 ----------------
class KnownVersions:
    """持久化已知版本（供客户端/服务端选择版本根目录）。"""

    def __init__(self, path: str | None = None):
        self.store = JsonStore(Path(path) if path else config_dir() / "known_versions.json",
                               defaults={"entries": []})

    def entries(self) -> list[dict]:
        es = self.store.get("entries", [])
        return list(es) if isinstance(es, list) else []

    def add_entry(self, name: str, mc_root: str, version: str, version_dir: str) -> None:
        entries = [e for e in self.entries()
                   if e.get("version_dir") != version_dir]
        entries.append({"name": name, "mc_root": mc_root,
                        "version": version, "version_dir": version_dir})
        self.store.set("entries", entries)

    def add_from_mc_root(self, mc_root: str) -> int:
        """扫描 mc_root 的 versions 并加入库，返回新增数量。"""
        versions = scan_versions(mc_root)
        before = len(self.entries())
        root_name = Path(mc_root).name or mc_root
        for version in versions:
            self.add_entry(f"{root_name} / {version}", mc_root, version,
                           os.path.join(mc_root, "versions", version))
        return len(self.entries()) - before

    def add_version_dir(self, version_dir: str) -> None:
        """将独立的版本/整合包目录加入库（mc_root 未知时留空）。"""
        version = Path(version_dir).name or version_dir
        self.add_entry(version, "", version, version_dir)

    def remove(self, version_dir: str) -> None:
        entries = [e for e in self.entries() if e.get("version_dir") != version_dir]
        self.store.set("entries", entries)


# ---------------- 拖拽解析统一入口 ----------------
def resolve_dropped(path: str) -> dict:
    """解析拖入路径，返回 {kind, mc_root, version_dir, launcher}。

    kind:
      - "mc_root": .minecraft 根目录（含 versions/）
      - "version_dir": 版本/整合包实例目录（含 mods 或 config）
      - "launcher": 启动器 exe / 快捷方式（需再扫描版本）
    """
    path = str(path).strip().strip('"')
    if not path:
        return {}
    p = Path(path)

    if p.is_dir():
        if os.path.isdir(os.path.join(path, "versions")):
            return {"kind": "mc_root", "mc_root": path, "version_dir": ""}
        # 直接拖入 versions 文件夹本身：其父目录即 Minecraft 根目录，读取其中所有版本
        if os.path.basename(os.path.normpath(path)).lower() == "versions":
            parent = os.path.dirname(os.path.normpath(path))
            if parent:
                return {"kind": "mc_root", "mc_root": parent, "version_dir": ""}
        if os.path.isdir(os.path.join(path, "mods")) or \
                os.path.isdir(os.path.join(path, "config")) or \
                os.path.isdir(os.path.join(path, "resourcepacks")):
            return {"kind": "version_dir", "mc_root": "", "version_dir": path}
        return {}

    if p.is_file():
        if p.suffix.lower() == ".lnk":
            target = resolve_shortcut(path)
            if target and Path(target).suffix.lower() == ".exe":
                mc_roots = find_mc_roots_from_launcher(target)
                if mc_roots:
                    return {"kind": "launcher", "mc_root": mc_roots[0], "version_dir": "",
                            "launcher": target, "mc_roots": mc_roots}
        elif p.suffix.lower() == ".exe":
            mc_roots = find_mc_roots_from_launcher(path)
            if mc_roots:
                return {"kind": "launcher", "mc_root": mc_roots[0], "version_dir": "",
                        "launcher": path, "mc_roots": mc_roots}
    return {}


# ---------------- 多路径拖入：解析 + 入库（供后台线程调用） ----------------
def collect_dropped(paths) -> dict:
    """解析多个拖入路径，汇总为 {version_dirs: [...], mc_roots: [...]}。

    - 版本/整合包文件夹 → version_dirs
    - .minecraft / 启动器(exe/快捷方式) → mc_roots（可能一个启动器对应多个 .minecraft）
    """
    version_dirs: list[str] = []
    mc_roots: list[str] = []
    for path in paths:
        info = resolve_dropped(path)
        if not info:
            continue
        if info["kind"] == "version_dir":
            version_dirs.append(os.path.abspath(info["version_dir"]))
        elif info.get("mc_roots"):
            mc_roots.extend(os.path.abspath(r) for r in info["mc_roots"])
        elif info.get("mc_root"):
            mc_roots.append(os.path.abspath(info["mc_root"]))
    return {"version_dirs": version_dirs,
            "mc_roots": list(dict.fromkeys(mc_roots))}


def parse_and_store(paths, progress_cb=None) -> str:
    """后台线程用：解析拖入内容并把识别到的版本写入已知版本库，返回 JSON 字符串。

    结果结构：{"version_dirs": [...], "mc_roots": [...], "added": 新增版本数}
    """
    data = collect_dropped(paths)
    known = KnownVersions()
    added = 0
    for root in data["mc_roots"]:
        added += known.add_from_mc_root(root)
    for vd in data["version_dirs"]:
        known.add_version_dir(vd)
    return json.dumps({**data, "added": added}, ensure_ascii=False)
