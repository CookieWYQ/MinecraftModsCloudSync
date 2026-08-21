# -*- coding: utf-8 -*-
"""模组身份识别：判断两个 jar 是否属于同一模组（用于旧版残留检测）。

策略（两者结合）：
1. 优先读取 jar 内的模组元数据得到真实 modid：
   - META-INF/mods.toml / META-INF/neoforge.mods.toml（Forge / NeoForge）
   - fabric.mod.json（Fabric）
   - mcmod.info（旧版 Forge）
2. 读不到（或元数据被作者写错/改名）时退回文件名相似度：
   - 去掉扩展名与尾部版本段（如 a-1.0.jar → a；a-1.20.1-1.0.jar → a），
     只要版本不同而"基名"一致即视为同一模组。

注意：本模块只解析本地 jar；服务端 jar 因无法低成本读取元数据，
对比时服务端一侧仅使用文件名相似度（filename_base）。
"""
import json
import re
import zipfile

from .logger import get_logger

log = get_logger("mod_identity")

# 元数据条目 → 解析函数（按优先级）
_META_READERS = (
    ("META-INF/neoforge.mods.toml", "toml"),
    ("META-INF/mods.toml", "toml"),
    ("fabric.mod.json", "fabric"),
    ("mcmod.info", "mcmod"),
)


def filename_base(name: str) -> str:
    """文件名基名：去扩展名，并循环去掉尾部版本段（以 -/_/. 分隔、以数字或 v 数字开头）。

    例：
      a-1.0.jar        → a
      a-1.20.1-1.0.jar → a
      3dskinlayers-1.0 → 3dskinlayers
    """
    stem = name.rsplit(".", 1)[0] if "." in name else name
    while True:
        new = re.sub(r"[-_.](?:v)?\d[\d._-]*$", "", stem)
        if new == stem:
            break
        stem = new
    return stem.strip().lower()


def _parse_toml_modid(text: bytes) -> str:
    """mods.toml / neoforge.mods.toml：取第一个 modId="..."。"""
    m = re.search(rb'modId\s*=\s*"([^"]+)"', text)
    return m.group(1).decode("utf-8", "ignore").strip() if m else ""


def _parse_fabric_modid(text: bytes) -> str:
    """fabric.mod.json：取顶层 id 字段。"""
    data = json.loads(text.decode("utf-8", "ignore"))
    return str(data.get("id", "")).strip()


def _parse_mcmod_modid(text: bytes) -> str:
    """mcmod.info：json 数组，取第一项的 modid。"""
    data = json.loads(text.decode("utf-8", "ignore"))
    if isinstance(data, list) and data:
        return str(data[0].get("modid", "")).strip()
    if isinstance(data, dict):
        return str(data.get("modid", "")).strip()
    return ""


_READERS = {
    "toml": _parse_toml_modid,
    "fabric": _parse_fabric_modid,
    "mcmod": _parse_mcmod_modid,
}


def _jar_modid(path: str) -> str:
    """读取本地 jar 的 modid（失败返回空串）。"""
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            for entry, kind in _META_READERS:
                if entry in names:
                    try:
                        modid = _READERS[kind](zf.read(entry))
                    except Exception:
                        continue
                    if modid:
                        return modid.lower()
    except Exception as exc:
        log.debug("读取模组元数据失败 %s: %s", path, exc)
    return ""


def jar_identifiers(path: str) -> list[str]:
    """本地 jar 的模组标识集合（小写、去重）：[文件名基名, 元数据 modid]。"""
    ids: list[str] = []
    base = filename_base(path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1])
    if base:
        ids.append(base)
    modid = _jar_modid(path)
    if modid and modid not in ids:
        ids.append(modid)
    return ids


# ---------- 模组显示名（PCL CE 移植：jar 元数据 → 百科关联用） ----------

def _parse_toml_display_name(text: bytes) -> str:
    """mods.toml / neoforge.mods.toml：取第一个 displayName="..."（PCL CE ModLocalComp.cs L1477）。"""
    m = re.search(rb'displayName\s*=\s*"([^"]+)"', text)
    return m.group(1).decode("utf-8", "ignore").strip() if m else ""


def _parse_json_name(text: bytes) -> str:
    """fabric.mod.json / quilt.mod.json：取顶层 name 字段（PCL CE ModLocalComp.cs L1251/L1320）。"""
    data = json.loads(text.decode("utf-8", "ignore"))
    return str(data.get("name", "")).strip()


def _parse_mcmod_name(text: bytes) -> str:
    """mcmod.info：json 数组，取第一项的 name（PCL CE ModLocalComp.cs L1157）。"""
    data = json.loads(text.decode("utf-8", "ignore"))
    if isinstance(data, list) and data:
        return str(data[0].get("name", "")).strip()
    if isinstance(data, dict):
        return str(data.get("name", "")).strip()
    return ""


def mod_display_name(path: str) -> str:
    """读取本地 jar 的模组显示名（不含版本号/加载器信息），供 MC 百科关联使用。

    参考 PCL CE（Plain Craft Launcher 2/Modules/Minecraft/ModLocalComp.cs）：
    mcmod.info 的 name / fabric.mod.json 的 name / quilt.mod.json 的 name /
    mods.toml（neoforge.mods.toml）的 displayName；读取失败返回空串。
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if "fabric.mod.json" in names:
                return _parse_json_name(zf.read("fabric.mod.json"))
            if "quilt.mod.json" in names:
                return _parse_json_name(zf.read("quilt.mod.json"))
            if "META-INF/mods.toml" in names:
                return _parse_toml_display_name(zf.read("META-INF/mods.toml"))
            if "META-INF/neoforge.mods.toml" in names:
                return _parse_toml_display_name(zf.read("META-INF/neoforge.mods.toml"))
            if "mcmod.info" in names:
                return _parse_mcmod_name(zf.read("mcmod.info"))
    except Exception as exc:
        log.debug("读取模组显示名失败 %s: %s", path, exc)
    return ""
