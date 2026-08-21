# -*- coding: utf-8 -*-
"""MCMod（MC 百科）模组数据库：解析 PCL CE 的 mcmod.buf 并按 Slug/名称匹配 WikiId。

数据库文件 mcmod.buf 取自 PCL CE 仓库（Plain Craft Launcher 2/Resources/mcmod.buf），
为 gzip 压缩的 protobuf-net 序列化数据，字段结构对应 PCL CE 的 CompDatabaseEntry
（ModComp.cs L827-L858）：
    WikiId=1(varint), ChineseName=2(string), CurseForgeSlug=3(string), ModrinthSlug=4(string)
匹配策略参考 PCL CE 的 GetCompWikiEntryBySlug（ModComp.cs L805-L825）：
    CurseForgeSlug / ModrinthSlug 精确匹配；另补充中文名括号内英文名匹配。
署名：PCL CE。
"""
import gzip
import re
import sys
from pathlib import Path


def _data_file() -> Path:
    """定位 mcmod.buf：PyInstaller 打包后位于 _MEIPASS/app_common/data/，源码运行时位于模块同级 data/。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "app_common" / "data" / "mcmod.buf"
    return Path(__file__).resolve().parent / "data" / "mcmod.buf"


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """读取 protobuf varint，返回 (值, 新位置)。"""
    shift = 0
    result = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _parse_entry(buf: bytes, start: int, end: int) -> tuple[int, str, str, str]:
    """按 PCL CE CompDatabaseEntry 字段解析一条记录 → (wiki_id, chinese_name, cf_slug, mr_slug)。"""
    wiki_id = 0
    cn = cf = mr = ""
    pos = start
    while pos < end:
        key, pos = _read_varint(buf, pos)
        field_no, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(buf, pos)
            if field_no == 1:
                wiki_id = value
        elif wire == 2:
            length, pos = _read_varint(buf, pos)
            chunk = buf[pos:pos + length]
            if field_no == 2:
                cn = chunk.decode("utf-8", "ignore")
            elif field_no == 3:
                cf = chunk.decode("utf-8", "ignore")
            elif field_no == 4:
                mr = chunk.decode("utf-8", "ignore")
            pos += length
        else:
            break
    return wiki_id, cn, cf, mr


def _parse(buf: bytes) -> list[tuple[int, str, str, str]]:
    """protobuf-net 序列化的 List<CompDatabaseEntry>：repeated 字段 1 包裹每条记录。"""
    entries: list[tuple[int, str, str, str]] = []
    pos = 0
    total = len(buf)
    while pos < total:
        key, pos = _read_varint(buf, pos)
        if (key >> 3) != 1 or (key & 7) != 2:
            break
        length, pos = _read_varint(buf, pos)
        entries.append(_parse_entry(buf, pos, pos + length))
        pos += length
    return entries


class McModDb:
    """mcmod.buf 的只读内存索引：Slug / 英文名 → WikiId。"""

    def __init__(self, data_file: Path | str | None = None):
        self._slug: dict[str, int] = {}
        self._en_name: dict[str, int] = {}
        path = Path(data_file) if data_file else _data_file()
        if not path.is_file():
            return
        raw = gzip.decompress(path.read_bytes())
        for wiki_id, cn, cf, mr in _parse(raw):
            if not wiki_id:
                continue
            if cf:
                self._slug.setdefault(cf.lower(), wiki_id)
            if mr:
                self._slug.setdefault(mr.lower(), wiki_id)
            # 中文名括号内的英文名（如 "工业时代2 (Industrial Craft 2)"）
            m = re.search(r"\(([^()]*)\)", cn)
            if m:
                en = m.group(1).strip()
                if en:
                    self._en_name.setdefault(en.lower(), wiki_id)

    def wiki_id_for(self, name: str) -> int | None:
        """按名称（文件名基名 / modid / slug）匹配 MC 百科 WikiId；未命中返回 None。"""
        base = (name or "").strip().lower()
        if not base:
            return None
        if base in self._slug:
            return self._slug[base]
        if base in self._en_name:
            return self._en_name[base]
        # 去除非字母数字后再匹配（容忍 -、_、空格差异）
        compact = re.sub(r"[^a-z0-9]", "", base)
        if compact and compact in self._slug:
            return self._slug[compact]
        if compact and compact in self._en_name:
            return self._en_name[compact]
        # 文件名可能带加载器后缀（如 sodium-fabric）：剥离后再试
        for suffix in ("-fabric", "-forge", "-neoforge", "-quilt", "-common"):
            if base.endswith(suffix):
                stem = base[: -len(suffix)]
                if stem in self._slug:
                    return self._slug[stem]
                stem_compact = re.sub(r"[^a-z0-9]", "", stem)
                if stem_compact in self._slug:
                    return self._slug[stem_compact]
        # 兜底：去掉版本号与加载器标记，按词重建 slug 候选再匹配
        # （如 "xaeros-minimap-23.9.3_Fabric_1.20.1" → "xaeros-minimap"）
        for cand in _slug_candidates(base):
            if cand in self._slug:
                return self._slug[cand]
            cand_compact = re.sub(r"[^a-z0-9]", "", cand)
            if cand_compact and cand_compact in self._slug:
                return self._slug[cand_compact]
        return None


# 已知加载器/无关标记（文件名中出现时忽略）
_LOADER_TOKENS = {"fabric", "forge", "neoforge", "quilt", "fml",
                  "loader", "api", "common", "core", "lib"}


def _slug_candidates(name: str) -> list[str]:
    """从名称生成可能的 slug 候选：去除版本号与加载器标记后按 '-'/紧凑 拼接。

    例：xaeros-minimap-23.9.3_fabric_1.20.1 → [xaeros-minimap, xaerosminimap]
    """
    s = re.sub(r"[-_.,;()+\[\] ]+", " ", (name or "").lower()).strip()
    kept = []
    for t in s.split(" "):
        if not t:
            continue
        if re.fullmatch(r"(?:v)?\d[\d.]*", t):
            continue  # 版本号段，如 1.20.1 / 23.9.3 / 15.2.0.27
        if t in _LOADER_TOKENS:
            continue
        kept.append(t)
    if not kept:
        return []
    candidates = []
    # 从完整词列开始，逐步去掉尾部词（应对文件名中未知的附加标记）
    for i in range(len(kept), 0, -1):
        joined = "-".join(kept[:i])
        if joined and joined not in candidates:
            candidates.append(joined)
    compact = "".join(kept)
    if compact and compact not in candidates:
        candidates.append(compact)
    return candidates


# 模块级懒加载单例（文件缺失/损坏时回退为 None，不影响搜索链接）
_db: McModDb | None = None
_db_failed = False


def wiki_id_for(name: str) -> int | None:
    """查询模组名称对应的 MC 百科 WikiId；失败返回 None（调用方回退到百科搜索链接）。"""
    global _db, _db_failed
    if _db is None and not _db_failed:
        try:
            _db = McModDb()
        except Exception:
            _db_failed = True
    if _db is None:
        return None
    try:
        return _db.wiki_id_for(name)
    except Exception:
        return None
