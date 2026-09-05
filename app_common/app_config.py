# -*- coding: utf-8 -*-
"""服务端 / 客户端专属配置模型。所有配置项持久化保存。"""
from .config import JsonStore
from .constants import (
    CLIENT_CONFIG_PATH,
    DEFAULT_CLIENT_ONLY_KEYWORDS,
    DEFAULT_REMOTE_C2C_DIR,
    DEFAULT_REMOTE_FILES_DIR,
    DEFAULT_REMOTE_TODO_DIR,
    DEFAULT_SYNC_EXCLUDE_PATTERNS,
    SERVER_CONFIG_PATH,
    SYNC_CATEGORIES,
)
from .logger import get_logger
from .profile import new_server_id
from .tasks import safe_target

log = get_logger("app_config")


def _abs_remote_dir(value, default: str) -> str:
    """远程目录统一为绝对路径（以 / 开头）；旧版相对路径配置自动补前缀。"""
    p = (value or "").strip().replace("\\", "/")
    if not p:
        return default
    if not p.startswith("/"):
        p = "/" + p.lstrip("/")
    return p


class ServerConfig:
    """服务端工具配置：可同时管理多台服务器（每台独立的 SFTP / 远程目录 / 本地客户端目录）。

    所有现有属性（sftp / remote / local_mc_dir / sync / export 等）读写时均作用于
    「当前选中的服务器」，因此各页面无需改动即可跟随切换；旧版单服务器配置会自动迁移。
    """

    def __init__(self, path=None):
        self.store = JsonStore(path or SERVER_CONFIG_PATH, defaults={
            "servers": [],
            "current_id": "",
            "window": {"width": 980, "height": 680, "tab": 0},
            "auto_update_check": True,
        })
        self._migrate_legacy()

    # ---------- 多服务器管理 ----------
    @property
    def servers(self) -> list[dict]:
        servers = self.store.get("servers", [])
        return list(servers) if isinstance(servers, list) else []

    def _save_servers(self, servers: list[dict]) -> None:
        self.store.set("servers", servers)

    def _ensure_server(self) -> dict:
        """确保存在至少一个服务器，返回当前选中项（无则创建默认并选中）。"""
        servers = self.servers
        if not servers:
            sid = new_server_id()
            servers = [{
                "id": sid, "name": "默认服务器",
                "sftp": {}, "remote": {}, "local_mc_dir": "",
                "sync": {}, "export": {},
            }]
            self._save_servers(servers)
            self.store.set("current_id", sid)
        cur_id = self.store.get("current_id", "")
        cur = next((s for s in servers if s.get("id") == cur_id), None)
        if cur is None:
            cur = servers[0]
            self.store.set("current_id", cur.get("id", ""))
        return cur

    def _set_current_field(self, key: str, value) -> None:
        servers = self.servers
        cur_id = self._ensure_server().get("id", "")
        for s in servers:
            if s.get("id") == cur_id:
                s[key] = value
                break
        self._save_servers(servers)

    def _migrate_legacy(self) -> None:
        """旧版单服务器配置（顶层 sftp/remote/local_mc_dir/sync/export）迁移为 servers 列表。"""
        if self.store.get("servers"):
            return
        servers = [{
            "id": new_server_id(),
            "name": self.store.get("sftp", {}).get("host") or "默认服务器",
            "sftp": self.store.get("sftp", {}),
            "remote": self.store.get("remote", {}),
            "local_mc_dir": self.store.get("local_mc_dir", ""),
            "sync": self.store.get("sync", {}),
            "export": self.store.get("export", {}),
        }]
        self._save_servers(servers)
        self.store.set("current_id", servers[0]["id"])

    def current_id(self) -> str:
        return self._ensure_server().get("id", "")

    def set_current(self, server_id: str) -> None:
        if any(s.get("id") == server_id for s in self.servers):
            self.store.set("current_id", server_id)

    def server_by_id(self, server_id: str) -> dict | None:
        return next((s for s in self.servers if s.get("id") == server_id), None)

    def add_server(self, name: str, local_mc_dir: str = "") -> str:
        """新增服务器并设为当前。返回服务器 id。"""
        sid = new_server_id()
        servers = self.servers
        servers.append({
            "id": sid, "name": name or "默认服务器",
            "sftp": {}, "remote": {}, "local_mc_dir": local_mc_dir,
            "sync": {}, "export": {},
        })
        self._save_servers(servers)
        self.set_current(sid)
        return sid

    def remove_server(self, server_id: str) -> None:
        servers = [s for s in self.servers if s.get("id") != server_id]
        self._save_servers(servers)
        if self.store.get("current_id") == server_id:
            self.store.set("current_id", servers[0].get("id", "") if servers else "")

    def rename_server(self, server_id: str, name: str) -> None:
        servers = self.servers
        for s in servers:
            if s.get("id") == server_id:
                s["name"] = name or s.get("name", "")
                break
        self._save_servers(servers)

    # ---- 当前服务器的字段（兼容旧接口，作用于选中服务器） ----
    @property
    def sftp(self) -> dict:
        return dict(self._ensure_server().get("sftp", {}))

    @sftp.setter
    def sftp(self, value: dict) -> None:
        self._set_current_field("sftp", value)

    @property
    def remote(self) -> dict:
        return dict(self._ensure_server().get("remote", {}))

    @remote.setter
    def remote(self, value: dict) -> None:
        self._set_current_field("remote", value)

    @property
    def local_mc_dir(self) -> str:
        return self._ensure_server().get("local_mc_dir", "")

    @local_mc_dir.setter
    def local_mc_dir(self, value: str) -> None:
        self._set_current_field("local_mc_dir", value)

    @property
    def sync(self) -> dict:
        return dict(self._ensure_server().get("sync", {}))

    @sync.setter
    def sync(self, value: dict) -> None:
        self._set_current_field("sync", value)

    @property
    def export(self) -> dict:
        return dict(self._ensure_server().get("export", {}))

    @export.setter
    def export(self, value: dict) -> None:
        self._set_current_field("export", value)

    # ---- 全局配置 ----
    @property
    def window(self) -> dict:
        return dict(self.store.get("window", {}))

    @window.setter
    def window(self, value: dict) -> None:
        self.store.set("window", value)

    # ---- 主题 ----
    @property
    def theme(self) -> str:
        value = self.store.get("theme", "system")
        return value if value in ("system", "light", "dark") else "system"

    @theme.setter
    def theme(self, value: str) -> None:
        if value not in ("system", "light", "dark"):
            value = "system"
        self.store.set("theme", value)

    # ---- 软件本体更新 ----
    @property
    def auto_update_check(self) -> bool:
        """是否定时自动检查软件本体更新（GitHub / Gitee）。"""
        return bool(self.store.get("auto_update_check", True))

    @auto_update_check.setter
    def auto_update_check(self, value: bool) -> None:
        self.store.set("auto_update_check", bool(value))

    def host(self) -> str:
        return self.sftp.get("host", "")

    def port(self) -> int:
        try:
            return int(self.sftp.get("port", 22))
        except (TypeError, ValueError):
            return 22

    def username(self) -> str:
        return self.sftp.get("username", "")

    def password(self) -> str:
        return self.sftp.get("password", "")

    @property
    def todo_dir(self) -> str:
        return _abs_remote_dir(self.remote.get("todo_dir"), DEFAULT_REMOTE_TODO_DIR)

    @property
    def files_dir(self) -> str:
        return _abs_remote_dir(self.remote.get("files_dir"), DEFAULT_REMOTE_FILES_DIR)

    @property
    def c2c_dir(self) -> str:
        """C2C（本地对本地）发布目录（服务器上的绝对路径）。"""
        return _abs_remote_dir(self.remote.get("c2c_dir"), DEFAULT_REMOTE_C2C_DIR)

    @property
    def server_code(self) -> str:
        """服务端（本机工具）代号，用于 C2C 名单去重。"""
        return (self.remote.get("server_code") or "").strip()

    @server_code.setter
    def server_code(self, value: str) -> None:
        self.remote = {**self.remote, "server_code": (value or "").strip()}

    @property
    def c2c_local_dir(self) -> str:
        """C2C 发送的本地源目录（保持文件结构发布）。"""
        return (self.remote.get("c2c_local_dir") or "").strip()

    @c2c_local_dir.setter
    def c2c_local_dir(self, value: str) -> None:
        self.remote = {**self.remote, "c2c_local_dir": (value or "").strip()}

    @property
    def server_root(self) -> str:
        """服务端文件仓库的实际根目录（服务端工具读写/对比用），默认 / 表示服务器根目录。"""
        root = self.remote.get("server_root", "")
        return root if root and root.strip("/") else "/"

    @server_root.setter
    def server_root(self, value: str) -> None:
        val = (value or "").strip()
        if val and not val.startswith("/"):
            val = "/" + val.lstrip("/")
        self.remote = {**self.remote, "server_root": val or "/"}

    @property
    def exclude_names(self) -> list[str]:
        """差异检测时排除的顶层文件夹/文件名（如 logs、cache）。"""
        return list(self.remote.get("exclude", []))

    @exclude_names.setter
    def exclude_names(self, value) -> None:
        self.remote = {**self.remote, "exclude": list(value or [])}

    @property
    def client_mods(self) -> list[str]:
        """标注为「客户端模组」的关键词（文件名/模组名子串匹配，只发客户端）。"""
        mods = self.remote.get("client_mods", [])
        return list(mods) if isinstance(mods, list) else []

    @client_mods.setter
    def client_mods(self, value) -> None:
        self.remote = {**self.remote, "client_mods": list(value or [])}

    @property
    def push_settings(self) -> dict:
        """随发布待办下发的客户端软件默认设置（每台服务器独立）。"""
        return dict(self.remote.get("push_settings", {}))

    @push_settings.setter
    def push_settings(self, value) -> None:
        self.remote = {**self.remote, "push_settings": dict(value or {})}

    @property
    def target_overrides(self) -> dict:
        """手动标注的发送目标（持久化）：{相对路径 或 文件夹前缀: target}。

        文件路径精确匹配优先；文件夹前缀（以该目录开头的所有文件）次之。
        用于「更新服务端」差异树右键标注后，下次检测时依然生效。
        """
        overrides = self.remote.get("target_overrides", {})
        return dict(overrides) if isinstance(overrides, dict) else {}

    @target_overrides.setter
    def target_overrides(self, value) -> None:
        self.remote = {**self.remote, "target_overrides": dict(value or {})}

    @property
    def ignore_overrides(self) -> dict:
        """「忽略」列表（持久化）：{相对路径 或 文件夹前缀: True}。

        被忽略的文件/文件夹不出现在差异审查中（本地、服务端独有、旧版残留都不显示），
        可在差异审核树中勾选「显示已忽略项」后右键恢复。
        """
        ignored = self.remote.get("ignore_overrides", {})
        return dict(ignored) if isinstance(ignored, dict) else {}

    @ignore_overrides.setter
    def ignore_overrides(self, value) -> None:
        self.remote = {**self.remote, "ignore_overrides": dict(value or {})}

    # ---- 导出客户端配置（当前服务器的名称 + 唯一编号 + 授权列表） ----
    def export_name(self) -> str:
        return self.export.get("name", "")

    def export_server_id(self) -> str:
        return self.export.get("server_id", "")

    def export_output_dir(self) -> str:
        return self.export.get("output_dir", "")

    def export_ids(self) -> list[dict]:
        ids = self.export.get("ids", [])
        return list(ids) if isinstance(ids, list) else []


class ClientConfig:
    """客户端配置：可同时管理多个服务器（每个服务器一份导入的配置文件）。

    服务器 SFTP 信息保存在各自配置文件中（随机密钥加密，客户端不展示任何 SFTP 细节），
    客户端仅通过服务器名称与唯一编号识别；其余设置（游戏目录、间隔等）明文保存。
    """

    def __init__(self, path=None):
        self.store = JsonStore(path or CLIENT_CONFIG_PATH, defaults={
            "local_mc_dir": "",
            "check_interval_min": 60,
            "autostart": False,
            "notify": True,
            "auto_update_check": True,
            "profiles": [],
        })

    # ---------- 服务器档案 ----------
    def profiles(self) -> list[dict]:
        ps = self.store.get("profiles", [])
        return list(ps) if isinstance(ps, list) else []

    def profile_by_id(self, server_id: str) -> dict | None:
        for p in self.profiles():
            if p.get("server_id") == server_id:
                return dict(p)
        return None

    def add_profile(self, name: str, server_id: str, content: str,
                    created_at: str = "", client_id: str = "") -> None:
        """导入/更新服务器档案（同 server_id 覆盖）。content 为配置文件原文。"""
        profiles = [p for p in self.profiles() if p.get("server_id") != server_id]
        profiles.append({
            "name": name,
            "server_id": server_id,
            "client_id": client_id,
            "created_at": created_at,
            "content": content,
            "last_applied_version": "",
            "last_applied_at": "",
            "last_seen_version": "",
            "last_seen_at": "",
            "last_applied_c2c": "",
            "last_seen_c2c": "",
        })
        self.store.set("profiles", profiles)

    def remove_profile(self, server_id: str) -> None:
        profiles = [p for p in self.profiles() if p.get("server_id") != server_id]
        self.store.set("profiles", profiles)

    def _update_profile(self, server_id: str, **fields) -> None:
        profiles = self.profiles()
        for p in profiles:
            if p.get("server_id") == server_id:
                p.update(fields)
                break
        self.store.set("profiles", profiles)

    def mark_applied(self, server_id: str, version: str, applied_at: str) -> None:
        self._update_profile(server_id,
                             last_applied_version=version,
                             last_applied_at=applied_at)

    def mark_seen(self, server_id: str, version: str, seen_at: str) -> None:
        self._update_profile(server_id,
                             last_seen_version=version,
                             last_seen_at=seen_at)

    def mark_applied_c2c(self, server_id: str, version: str) -> None:
        self._update_profile(server_id, last_applied_c2c=version)

    def mark_seen_c2c(self, server_id: str, version: str, seen_at: str) -> None:
        self._update_profile(server_id, last_seen_c2c=version)

    # ---------- 多次更新未应用提醒（客户端） ----------
    def unapplied_count(self, server_id: str) -> int:
        p = self.profile_by_id(server_id)
        try:
            return max(0, int((p or {}).get("unapplied_count", 0)))
        except (TypeError, ValueError):
            return 0

    def bump_unapplied(self, server_id: str) -> int:
        """客户端发现新更新但未应用时累计次数，返回累计值。"""
        n = self.unapplied_count(server_id) + 1
        self._update_profile(server_id, unapplied_count=n)
        return n

    def reset_unapplied(self, server_id: str) -> None:
        """应用成功后清零未应用次数。"""
        self._update_profile(server_id, unapplied_count=0)

    # ---------- 明文设置 ----------
    @property
    def local_mc_dir(self) -> str:
        return self.store.get("local_mc_dir", "")

    @local_mc_dir.setter
    def local_mc_dir(self, value: str) -> None:
        self.store.set("local_mc_dir", value)

    @property
    def check_interval_min(self) -> int:
        try:
            return max(5, int(self.store.get("check_interval_min", 60)))
        except (TypeError, ValueError):
            return 60

    @check_interval_min.setter
    def check_interval_min(self, value: int) -> None:
        self.store.set("check_interval_min", int(value))

    @property
    def autostart(self) -> bool:
        return bool(self.store.get("autostart", False))

    @autostart.setter
    def autostart(self, value: bool) -> None:
        self.store.set("autostart", bool(value))

    @property
    def notify(self) -> bool:
        return bool(self.store.get("notify", True))

    @notify.setter
    def notify(self, value: bool) -> None:
        self.store.set("notify", bool(value))

    # ---- 软件本体更新 ----
    @property
    def auto_update_check(self) -> bool:
        """是否定时自动检查软件本体更新（GitHub / Gitee）。"""
        return bool(self.store.get("auto_update_check", True))

    @auto_update_check.setter
    def auto_update_check(self, value: bool) -> None:
        self.store.set("auto_update_check", bool(value))

    # ---- 主题 ----
    @property
    def theme(self) -> str:
        value = self.store.get("theme", "system")
        return value if value in ("system", "light", "dark") else "system"

    @theme.setter
    def theme(self, value: str) -> None:
        if value not in ("system", "light", "dark"):
            value = "system"
        self.store.set("theme", value)


def validate_target(rel: str) -> str:
    return safe_target(rel)
