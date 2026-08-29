# -*- coding: utf-8 -*-
"""待办任务模型与清单（manifest）读写。"""
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import PurePosixPath

from .constants import MANIFEST_FILENAME
from .logger import get_logger
from .sftp import SFTPError, SFTPManager

log = get_logger("tasks")

# 动作：install=安装/替换文件，delete=删除文件，download=从共享区下载到游戏目录
ACTION_LABELS = {"install": "安装/替换", "delete": "删除", "download": "下载安装"}
# 分类：对应游戏目录下的子目录
CATEGORY_LABELS = {"mods": "模组", "resourcepacks": "资源包", "config": "配置", "other": "其他"}


@dataclass
class TaskItem:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    action: str = "delete"            # install | delete | download
    category: str = "mods"            # mods | resourcepacks | config | other
    target: str = ""                  # 游戏目录内相对路径，如 mods/foo.jar
    source: str = ""                  # install/download 时的源相对路径
    description: str = ""             # 说明
    optional: bool = False            # 可选任务：客户端应用时可勾选跳过
    silent: bool = False              # 静默任务：客户端后台应用，不弹窗打扰
    local_file: str = ""              # 仅界面使用：本地源文件路径，不入库

    @classmethod
    def from_dict(cls, d: dict) -> "TaskItem":
        return cls(
            id=d.get("id") or uuid.uuid4().hex,
            action=d.get("action", "delete"),
            category=d.get("category", "mods"),
            target=d.get("target", ""),
            source=d.get("source", ""),
            description=d.get("description", ""),
            optional=bool(d.get("optional", False)),
            silent=bool(d.get("silent", False)),
        )

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("local_file", None)
        return data

    def summary(self) -> str:
        label = ACTION_LABELS.get(self.action, self.action)
        cat = CATEGORY_LABELS.get(self.category, self.category)
        if self.action == "delete":
            return f"删除{cat}：{self.target}"
        if self.action == "download":
            return f"下载{cat}：{self.target}"
        return f"安装{cat}：{self.target}"

    @property
    def tags(self) -> list[str]:
        """任务标签（界面展示用）。"""
        out = []
        if self.optional:
            out.append("可选")
        if self.silent:
            out.append("静默")
        return out


@dataclass
class TodoManifest:
    version: str = ""                 # 版本号，可自定义，如 hotfix-1.0
    created_at: str = ""              # ISO 8601 时间戳
    note: str = ""                    # 发布说明（可选，随待办下发展示）
    tasks: list[TaskItem] = field(default_factory=list)
    settings: dict = field(default_factory=dict)  # 客户端软件设置（随待办下发）
    meta: dict = field(default_factory=dict)      # 附加元数据（如 C2C 文件哈希表 {"files": {rel: md5}}）

    @classmethod
    def new(cls, version: str) -> "TodoManifest":
        return cls(version=version, created_at=datetime.now().astimezone().isoformat())

    @classmethod
    def from_dict(cls, d: dict) -> "TodoManifest":
        settings = d.get("settings", {})
        meta = d.get("meta", {})
        return cls(
            version=d.get("version", ""),
            created_at=d.get("created_at", ""),
            note=d.get("note", ""),
            tasks=[TaskItem.from_dict(t) for t in d.get("tasks", [])],
            settings=settings if isinstance(settings, dict) else {},
            meta=meta if isinstance(meta, dict) else {},
        )

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "note": self.note,
            "tasks": [t.to_dict() for t in self.tasks],
            "settings": dict(self.settings),
            "meta": dict(self.meta),
        }

    def settings_summary(self) -> str:
        labels = {
            "check_interval_min": "自动检查间隔(分钟)",
            "autostart": "开机自启",
            "notify": "系统通知",
        }
        if not self.settings:
            return ""
        parts = []
        for key, value in self.settings.items():
            label = labels.get(key, key)
            if key == "autostart" or key == "notify":
                value = "开" if value else "关"
            parts.append(f"{label}={value}")
        return "、".join(parts)

    def summary(self) -> str:
        if not self.tasks:
            return "（无任务）"
        return "; ".join(t.summary() for t in self.tasks)

    def formatted_time(self) -> str:
        """将 ISO 时间戳格式化为 2026-08-20 14:30:05。"""
        try:
            dt = datetime.fromisoformat(self.created_at)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return self.created_at or "未知"

    # ---------- SFTP 读写 ----------
    @staticmethod
    def _manifest_path(todo_dir: str) -> str:
        return SFTPManager.join(todo_dir, MANIFEST_FILENAME)

    def save_to_sftp(self, sftp: SFTPManager, todo_dir: str) -> None:
        sftp.mkdirs(todo_dir)
        sftp.write_text(self._manifest_path(todo_dir),
                        json.dumps(self.to_dict(), ensure_ascii=False, indent=2))

    @staticmethod
    def load_from_sftp(sftp: SFTPManager, todo_dir: str) -> "TodoManifest | None":
        path = TodoManifest._manifest_path(todo_dir)
        if not sftp.exists(path):
            return None
        try:
            data = json.loads(sftp.read_text(path))
            return TodoManifest.from_dict(data)
        except SFTPError:
            raise
        except Exception as exc:
            raise SFTPError(f"解析待办清单失败: {exc}") from exc

    @staticmethod
    def remote_source_path(sftp: SFTPManager, files_dir: str, source: str) -> str:
        """将 source 相对路径规范化为 files 目录下的绝对远程路径。"""
        source = source.replace("\\", "/").strip("/")
        return SFTPManager.join(files_dir, source)


def make_task(action: str, category: str, filename: str, description: str = "",
              source: str = "") -> TaskItem:
    """按分类与文件名构造任务，target 自动拼接分类子目录。"""
    if action == "delete":
        return TaskItem(action="delete", category=category,
                        target=f"{category}/{filename}".strip("/"), description=description)
    return TaskItem(action="install", category=category,
                    target=f"{category}/{filename}".strip("/"),
                    source=source or f"{category}/{filename}".strip("/"),
                    description=description)


def safe_target(rel: str) -> str:
    """规范化目标相对路径，防止路径穿越。"""
    rel = rel.replace("\\", "/").strip("/")
    p = PurePosixPath(rel)
    if any(part in ("..", ".") for part in p.parts):
        raise ValueError("路径不合法（不允许 .. 或 .）")
    return p.as_posix()
