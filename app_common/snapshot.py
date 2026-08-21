# -*- coding: utf-8 -*-
"""服务端 - 文件仓库快照：记录服务端 files 目录树，作为「更新服务端 / 发布待办」的对比基线。

- 仓库快照（repo）：服务端文件仓库当前状态（{rel: size}），用于「更新服务端」对比本地新客户端。
- 发布基线（publish）：上次发布待办时服务端文件仓库状态，用于「发布待办」检测新改动。

快照保存在本地数据目录 snapshots\\{key}_{server_id}.json，每台服务器独立。
"""
import json
import os
from datetime import datetime

from .constants import config_dir
from .logger import get_logger

log = get_logger("snapshot")

_KEYS = {"repo", "publish"}


def _snapshot_path(server_id: str, key: str = "repo") -> str:
    if key not in _KEYS:
        key = "repo"
    d = config_dir() / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / f"{key}_{server_id}.json")


def save_snapshot(server_id: str, files: dict, key: str = "repo") -> str:
    """保存文件仓库快照（{rel: size}），返回保存时间字符串（失败返回空）。"""
    data = {
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "files": dict(files) if isinstance(files, dict) else {},
    }
    path = _snapshot_path(server_id, key)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError as exc:
        log.warning("保存快照失败: %s", exc)
        return ""
    return data["saved_at"]


def load_snapshot(server_id: str, key: str = "repo") -> dict:
    """读取快照，返回 {"saved_at": str, "files": {rel: size}}；不存在或损坏时 files 为空。"""
    path = _snapshot_path(server_id, key)
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


def update_snapshot(server_id: str, files: dict, key: str = "repo",
                    max_age_min: int = 30) -> dict:
    """覆盖式更新文件仓库快照（始终只保留一个快照文件，不累积）。

    返回 {"saved_at": 保存时间, "changed": 内容是否发生变化, "aged": 是否因超时刷新}。
    - 无旧快照 → 创建
    - 内容与旧快照不同 → 重写（saved_at=现在）
    - 内容相同 → 仅刷新 saved_at（保持时效性）
    - 距上次快照超过 max_age_min 分钟 → 无论内容是否相同均视为需要更新（aged=True）
    """
    old = load_snapshot(server_id, key)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changed = bool(old.get("files")) and old.get("files") != files
    aged = False
    if old.get("saved_at"):
        try:
            dt = datetime.strptime(old["saved_at"], "%Y-%m-%d %H:%M:%S")
            aged = (datetime.now() - dt).total_seconds() > max_age_min * 60
        except ValueError:
            aged = True
    data = {"saved_at": now_str, "files": dict(files) if isinstance(files, dict) else {}}
    path = _snapshot_path(server_id, key)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError as exc:
        log.warning("保存快照失败: %s", exc)
        return {"saved_at": "", "changed": changed, "aged": aged}
    return {"saved_at": now_str, "changed": changed, "aged": aged}
