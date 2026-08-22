# -*- coding: utf-8 -*-
"""服务端 - 文件仓库快照（历史化）：记录服务端文件目录树，作为「更新服务端 / 发布待办」的对比基线。

- 仓库快照（repo）：服务端文件仓库当前状态（{rel: size}），用于「更新服务端」对比本地新客户端。
- 发布基线（publish）：上次发布待办时服务端文件仓库状态，用于「发布待办」检测新改动。

每次保存快照都会保留一条带时间戳的历史记录（snapshots\\{key}_{server_id}_{时间戳}.json），
并维护一个 latest 指针（snapshots\\{key}_{server_id}_latest.json）供现有对比逻辑读取。
历史快照可用于查看每个时间点的文件树，或把某个历史快照设为发布基线来撤销/回滚客户端更新。
"""
import json
import os
import re
from datetime import datetime

from .constants import config_dir
from .logger import get_logger

log = get_logger("snapshot")

_KEYS = {"repo", "publish"}
_MAX_HISTORY = 40  # 每台服务器每个类型的快照最多保留的历史条数（超出删除最旧）

# 历史文件名：{key}_{server_id}_{YYYYMMDD_HHMMSS}.json
_TS_PATTERN = re.compile(r"_(\d{8}_\d{6})\.json$")


def _snapshot_dir() -> str:
    d = config_dir() / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _latest_path(server_id: str, key: str) -> str:
    return os.path.join(_snapshot_dir(), f"{key}_{server_id}_latest.json")


def _history_path(server_id: str, key: str, ts: str) -> str:
    return os.path.join(_snapshot_dir(), f"{key}_{server_id}_{ts}.json")


def _now_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _write(path: str, files: dict) -> str:
    """写入快照文件，返回保存时间字符串（失败返回空）。"""
    saved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data = {"saved_at": saved_at,
            "files": dict(files) if isinstance(files, dict) else {}}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return saved_at
    except OSError as exc:
        log.warning("保存快照失败: %s", exc)
        return ""


def _read(path: str) -> dict:
    """读取快照，返回 {"saved_at": str, "files": {rel: size}}；不存在或损坏时 files 为空。"""
    if not os.path.exists(path):
        return {"saved_at": "", "files": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        files = data.get("files", {})
        return {
            "saved_at": data.get("saved_at", ""),
            "files": files if isinstance(files, dict) else {},
        }
    except Exception as exc:
        log.warning("读取快照失败: %s", exc)
        return {"saved_at": "", "files": {}}


def _prune(server_id: str, key: str) -> None:
    """删除最旧的历史快照，只保留最近 _MAX_HISTORY 条。"""
    entries = list_snapshots(server_id, key)
    for entry in entries[_MAX_HISTORY:]:
        try:
            os.remove(_history_path(server_id, key, entry["ts"]))
        except OSError:
            pass


def save_snapshot(server_id: str, files: dict, key: str = "repo") -> str:
    """保存快照（保留历史）：写入一条带时间戳的历史记录并更新 latest。返回保存时间字符串。"""
    if key not in _KEYS:
        key = "repo"
    saved_at = _write(_history_path(server_id, key, _now_ts()), files)
    latest_saved = _write(_latest_path(server_id, key), files)
    if not saved_at and latest_saved:
        saved_at = latest_saved
    _prune(server_id, key)
    return saved_at or latest_saved


def load_snapshot(server_id: str, key: str = "repo") -> dict:
    """读取最新快照（latest 指针）；旧版单文件快照自动迁移。"""
    if key not in _KEYS:
        key = "repo"
    latest = _read(_latest_path(server_id, key))
    if not latest.get("saved_at") and not latest.get("files"):
        # 兼容旧版单文件快照 {key}_{server_id}.json → 迁移为 latest
        legacy = os.path.join(_snapshot_dir(), f"{key}_{server_id}.json")
        if os.path.exists(legacy):
            latest = _read(legacy)
            if latest.get("saved_at") or latest.get("files"):
                _write(_latest_path(server_id, key), latest.get("files", {}))
    return latest


def update_snapshot(server_id: str, files: dict, key: str = "repo",
                    max_age_min: int = 30) -> dict:
    """覆盖式更新快照（latest 始终为最新；内容发生变化时额外记录一条历史）。

    返回 {"saved_at": 保存时间, "changed": 内容是否发生变化, "aged": 是否因超时刷新}。
    - 无旧快照 → 创建
    - 内容与旧快照不同 → 重写 latest 并记录历史（saved_at=现在）
    - 内容相同 → 仅刷新 saved_at（保持时效性，不记录历史）
    - 距上次快照超过 max_age_min 分钟 → 无论内容是否相同均视为需要更新（aged=True）
    """
    old = load_snapshot(server_id, key)
    changed = bool(old.get("files")) and old.get("files") != files
    aged = False
    if old.get("saved_at"):
        try:
            dt = datetime.strptime(old["saved_at"], "%Y-%m-%d %H:%M:%S")
            aged = (datetime.now() - dt).total_seconds() > max_age_min * 60
        except ValueError:
            aged = True
    if changed:
        # 内容有变化 → 保留一条历史记录，便于查看 / 撤销
        _write(_history_path(server_id, key, _now_ts()), files)
        _prune(server_id, key)
    saved_at = _write(_latest_path(server_id, key), files)
    return {"saved_at": saved_at, "changed": changed, "aged": aged}


def normalize_files(files: dict) -> dict:
    """归一化快照 files 值为 {rel: {"size": int, "hash": str|None}}。

    兼容旧版 int 值快照；新格式额外携带哈希（用于大小相同但内容不同的判定）。
    """
    out: dict = {}
    for rel, v in (files or {}).items():
        if isinstance(v, dict):
            size = v.get("size")
            h = v.get("hash")
            out[rel] = {"size": int(size or 0),
                        "hash": h if isinstance(h, str) and h else None}
        else:
            out[rel] = {"size": int(v or 0), "hash": None}
    return out


def list_snapshots(server_id: str, key: str = "repo") -> list[dict]:
    """列出历史快照（按时间倒序）：[{ts, saved_at, count}]。"""
    if key not in _KEYS:
        key = "repo"
    prefix = f"{key}_{server_id}_"
    entries: list[dict] = []
    try:
        names = os.listdir(_snapshot_dir())
    except OSError:
        return entries
    for name in names:
        m = _TS_PATTERN.search(name)
        if not m or not name.startswith(prefix):
            continue
        ts = m.group(1)
        snap = _read(os.path.join(_snapshot_dir(), name))
        entries.append({
            "ts": ts,
            "saved_at": snap.get("saved_at", ""),
            "count": len(snap.get("files", {})),
        })
    entries.sort(key=lambda e: e["ts"], reverse=True)
    return entries


def load_snapshot_at(server_id: str, key: str, ts: str) -> dict:
    """读取指定时间戳的历史快照。"""
    if key not in _KEYS:
        key = "repo"
    return _read(_history_path(server_id, key, ts))
