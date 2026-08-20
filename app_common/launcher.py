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
def resolve_shortcut(lnk_path: str) -> str | None:
    """通过 WScript.Shell 解析 .lnk，返回目标程序路径（无控制台窗口）。"""
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
def find_mc_root_from_launcher(exe_path: str, work_dir: str = "") -> str | None:
    """根据启动器位置与常见配置推断 Minecraft 根目录。"""
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

    # 2) 便携式 .minecraft
    for base in (exe_dir, work_dir):
        if base:
            candidates.append(os.path.join(base, ".minecraft"))

    # 3) 默认 %APPDATA%\\.minecraft
    candidates.append(DEFAULT_MC_ROOT)

    for cand in candidates:
        if cand and os.path.isdir(os.path.join(cand, "versions")):
            return cand
    return None


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
        if os.path.isdir(os.path.join(path, "mods")) or \
                os.path.isdir(os.path.join(path, "config")) or \
                os.path.isdir(os.path.join(path, "resourcepacks")):
            return {"kind": "version_dir", "mc_root": "", "version_dir": path}
        return {}

    if p.is_file():
        if p.suffix.lower() == ".lnk":
            target = resolve_shortcut(path)
            if target and Path(target).suffix.lower() == ".exe":
                mc_root = find_mc_root_from_launcher(target)
                if mc_root:
                    return {"kind": "launcher", "mc_root": mc_root, "version_dir": "",
                            "launcher": target}
        elif p.suffix.lower() == ".exe":
            mc_root = find_mc_root_from_launcher(path)
            if mc_root:
                return {"kind": "launcher", "mc_root": mc_root, "version_dir": "",
                        "launcher": path}
    return {}
