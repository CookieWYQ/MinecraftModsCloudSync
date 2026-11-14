# -*- coding: utf-8 -*-
"""客户端核心引擎：多服务器更新检查与任务应用（网络操作在线程中执行，日志脱敏）。

本模块不记录任何 SFTP 服务器地址、端口、账号、密码等敏感信息；
所有网络异常在客户端模式（sanitize_log=True）下仅给出脱敏后的提示。
"""
import os
import shutil
import tempfile
from datetime import datetime

from PySide6.QtCore import QThread, Signal

from app_common import winutil
from app_common.app_config import ClientConfig
from app_common.auto_sync import apply_sync, load_policy, plan_sync
from app_common.c2c import files_root as c2c_files_root
from app_common.c2c import load_client_manifest
from app_common.constants import (
    DEFAULT_REMOTE_C2C_DIR,
    DEFAULT_REMOTE_FILES_DIR,
    DEFAULT_REMOTE_TODO_DIR,
)
from app_common.license import AuthError, verify
from app_common.logger import get_logger
from app_common.profile import client_identity, parse_profile_content
from app_common.sftp import SFTPError, SFTPManager
from app_common.tasks import TodoManifest, manifest_signature, safe_target
from app_common.upload_files import upload_files_dir

log = get_logger("client.engine")


def default_minecraft_dir() -> str:
    return os.path.join(os.environ.get("APPDATA", ""), ".minecraft")


def profile_sftp_info(profile: dict) -> dict:
    """从服务器档案（配置文件原文）解析并解密 SFTP 连接信息。"""
    data = parse_profile_content(profile.get("content", ""))
    sftp = data["sftp"]
    return {
        "host": sftp["host"],
        "port": int(sftp.get("port", 22)),
        "username": sftp.get("username", ""),
        "password": sftp.get("password", ""),
        "todo_dir": _abs_remote(sftp.get("todo_dir"), DEFAULT_REMOTE_TODO_DIR),
        "files_dir": _abs_remote(sftp.get("files_dir"), DEFAULT_REMOTE_FILES_DIR),
        "c2c_dir": _abs_remote(sftp.get("c2c_dir"), DEFAULT_REMOTE_C2C_DIR),
    }


def _abs_remote(value, default: str) -> str:
    """远程目录统一为绝对路径（以 / 开头）；旧版相对路径配置自动补前缀。"""
    p = (value or "").strip().replace("\\", "/")
    if not p:
        return default
    if not p.startswith("/"):
        p = "/" + p.lstrip("/")
    return p


def _connect(info: dict) -> SFTPManager:
    """客户端统一以脱敏模式建立连接（日志/异常不出现地址与凭据）。"""
    return SFTPManager(info["host"], info["port"], info["username"],
                       info["password"], sanitize_log=True)


# ---------------- 更新检查 ----------------
class CheckAllThread(QThread):
    """检查所有已导入服务器。"""

    done = Signal(bool, dict, str)  # (成功, {server_id: result}, 整体错误)
    progress = Signal(int, int, str)

    def __init__(self, config: ClientConfig, parent=None):
        super().__init__(parent)
        self.config = config

    def run(self):
        try:
            results = check_all(self.config, self._progress)
            self.done.emit(True, results, "")
        except Exception as exc:
            log.exception("批量检查更新失败")
            self.done.emit(False, {}, str(exc))

    def _progress(self, cur: int, total: int, msg: str):
        self.progress.emit(cur, total, msg)


def check_all(config: ClientConfig, progress_cb=None) -> dict:
    """依次检查每个服务器：授权校验 → 读取待办清单（S2C）与 C2C 清单。

    返回 {server_id: {"ok": bool, "manifest": TodoManifest|None,
                      "c2c_manifest": TodoManifest|None, "error": str}}
    """
    profiles = config.profiles()
    results: dict = {}
    for i, profile in enumerate(profiles):
        sid = profile.get("server_id", "")
        ident = client_identity(profile)  # 客户端身份：client_id，旧配置回退 server_id
        name = profile.get("name", "")
        try:
            info = profile_sftp_info(profile)
            with _connect(info) as sftp:
                verify(sftp, ident)
                manifest = TodoManifest.load_from_sftp(sftp, info["todo_dir"])
                c2c_manifest = None
                try:
                    c2c_manifest = load_client_manifest(
                        sftp, info["c2c_dir"], ident)
                except SFTPError as exc:
                    # C2C 为可选功能：读取失败（如未配置目录）不阻塞整体检查
                    log.warning("读取 C2C 清单失败: %s（%s）", name, exc)
                    c2c_manifest = None
            if manifest is not None:
                config.mark_seen(sid, manifest.version,
                                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                 sig=manifest_signature(manifest))
            if c2c_manifest is not None:
                config.mark_seen_c2c(sid, c2c_manifest.version,
                                     datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            results[sid] = {"ok": True, "manifest": manifest,
                            "c2c_manifest": c2c_manifest, "error": ""}
            log.info("检查完成: %s", name)
        except AuthError as exc:
            log.warning("授权校验未通过: %s（%s）", name, exc)
            results[sid] = {"ok": False, "manifest": None,
                            "c2c_manifest": None, "error": f"授权:{exc}"}
        except Exception as exc:
            log.exception("检查更新失败: %s", name)
            results[sid] = {"ok": False, "manifest": None,
                            "c2c_manifest": None, "error": str(exc)}
        if progress_cb:
            progress_cb(i + 1, len(profiles), name)
    return results


def is_manifest_new(profile: dict, manifest: TodoManifest | None,
                    c2c_manifest: TodoManifest | None = None) -> bool:
    """该服务器的清单是否算「有新更新」（客户端决定提醒/允许应用）。

    判定：版本号变化，或版本号相同但清单内容指纹变化。
    后者用于「服务器当天二次发布、版本号重复」的情况——只比版本号会漏掉。
    """
    profile = profile or {}
    if manifest is not None:
        if manifest.version != profile.get("last_applied_version", ""):
            return True
        prev_sig = profile.get("last_applied_sig") or ""
        if prev_sig and manifest_signature(manifest) != prev_sig:
            return True
    if c2c_manifest is not None and \
            c2c_manifest.version != profile.get("last_applied_c2c", ""):
        return True
    return False


def is_silent_batch(*manifests: TodoManifest | None) -> bool:
    """这批清单是否全部由「静默 + 非可选」任务组成（客户端可后台自动应用）。

    服务端「发布待办」页勾选「静默下载应用」时，会给该批全部任务打 silent 标记。
    """
    tasks = [t for m in manifests if m is not None for t in m.tasks]
    return bool(tasks) and all(t.silent and not t.optional for t in tasks)


# ---------------- 应用更新 ----------------
class ApplyThread(QThread):
    """应用指定服务器的待办清单（S2C + C2C）。

    skip_optional_ids: 用户取消勾选的可选任务 id 集合（这些任务将被跳过）。
    """

    done = Signal(bool, object, str)  # (成功, summary dict, 错误信息)
    progress = Signal(int, int, str)

    def __init__(self, config: ClientConfig, profile: dict, manifest: TodoManifest,
                 c2c_manifest: TodoManifest | None = None,
                 skip_optional_ids: set[str] | None = None, parent=None):
        super().__init__(parent)
        self.config = config
        self.profile = profile
        self.manifest = manifest
        self.c2c_manifest = c2c_manifest
        self.skip_optional_ids = skip_optional_ids or set()

    def run(self):
        try:
            summary = apply_update(self.config, self.profile, self.manifest,
                                   self.c2c_manifest, self._progress,
                                   self.skip_optional_ids)
            self.done.emit(True, summary, "")
        except AuthError as exc:
            log.warning("授权校验未通过: %s", exc)
            self.done.emit(False, None, f"授权:{exc}")
        except Exception as exc:
            log.exception("应用更新失败")
            self.done.emit(False, None, str(exc))

    def _progress(self, cur: int, total: int, msg: str):
        self.progress.emit(cur, total, msg)


def apply_update(config: ClientConfig, profile: dict, manifest: TodoManifest,
                 c2c_manifest: TodoManifest | None = None,
                 progress_cb=None, skip_optional_ids: set[str] | None = None) -> dict:
    """校验编号后应用清单：下载/替换文件、删除文件、下载共享文件（S2C 与 C2C）。

    - download 任务：从 .upload_files 共享区下载到游戏目录（未加密共享文件）；
    - optional 任务：id 在 skip_optional_ids 中时跳过（用户未勾选）；
    - silent 任务：不影响执行，仅由界面层决定是否静默展示。
    """
    info = profile_sftp_info(profile)
    ident = client_identity(profile)  # 客户端身份：client_id，旧配置回退 server_id
    game_dir = config.local_mc_dir
    if not game_dir or not os.path.isdir(game_dir):
        raise RuntimeError("游戏目录无效，请先在主界面选择客户端根目录。")

    skip_ids = skip_optional_ids or set()
    summary = {"installed": [], "deleted": [], "skipped": [], "errors": [],
               "settings": []}
    s2c_tasks = list(manifest.tasks) if manifest else []
    c2c_tasks = list(c2c_manifest.tasks) if c2c_manifest else []
    total = len(s2c_tasks) + len(c2c_tasks)
    shared_root = upload_files_dir(info["c2c_dir"])
    with _connect(info) as sftp:
        verify(sftp, ident)
        with tempfile.TemporaryDirectory(prefix="mc_sync_") as tmp:
            count = [0]

            def apply_tasks(tasks, source_root):
                """按任务应用：install 从 source_root 下载，download 从共享区下载，delete 删除。"""
                for task in tasks:
                    try:
                        if task.optional and task.id in skip_ids:
                            summary["skipped"].append(
                                f"{task.target}（可选任务未勾选，已跳过）")
                            count[0] += 1
                            if progress_cb:
                                progress_cb(count[0], total, task.summary())
                            continue
                        target = safe_target(task.target)
                        abs_target = os.path.join(game_dir, target)
                        if task.action == "delete":
                            if os.path.exists(abs_target):
                                os.remove(abs_target)
                                summary["deleted"].append(target)
                                log.info("已删除: %s", target)
                            else:
                                summary["skipped"].append(f"{target}（不存在）")
                        elif task.action == "install":
                            remote = TodoManifest.remote_source_path(
                                sftp, source_root, task.source or target)
                            if not sftp.exists(remote):
                                summary["errors"].append(
                                    f"{target}（服务器缺少源文件）")
                            else:
                                local_tmp = os.path.join(tmp, f"c{count[0]}")
                                sftp.download(remote, local_tmp)
                                os.makedirs(os.path.dirname(abs_target),
                                            exist_ok=True)
                                shutil.move(local_tmp, abs_target)
                                summary["installed"].append(target)
                                log.info("已安装: %s", target)
                        elif task.action == "download":
                            # 从 .upload_files 共享区下载到游戏目录（保留相对结构）
                            remote = SFTPManager.join(shared_root,
                                                      task.source or target)
                            if not sftp.exists(remote):
                                summary["errors"].append(
                                    f"{target}（共享文件不存在或已过期）")
                            else:
                                local_tmp = os.path.join(tmp, f"c{count[0]}")
                                sftp.download(remote, local_tmp)
                                os.makedirs(os.path.dirname(abs_target),
                                            exist_ok=True)
                                shutil.move(local_tmp, abs_target)
                                summary["installed"].append(target)
                                log.info("已下载安装: %s", target)
                        else:
                            summary["skipped"].append(
                                f"{target}（未知操作 {task.action}）")
                    except Exception as exc:
                        log.exception("任务执行失败: %s", task.target)
                        summary["errors"].append(f"{task.target}（{exc}）")
                    count[0] += 1
                    if progress_cb:
                        progress_cb(count[0], total, task.summary())

            apply_tasks(s2c_tasks, info["files_dir"])
            if c2c_tasks:
                apply_tasks(c2c_tasks, c2c_files_root(info["c2c_dir"], ident))

    # 应用客户端软件设置（随待办下发）
    for key, value in ((manifest.settings or {}) if manifest else {}).items():
        try:
            if key == "check_interval_min":
                config.check_interval_min = max(5, min(1440, int(value)))
                summary["settings"].append(f"自动检查间隔={config.check_interval_min}分钟")
            elif key == "autostart":
                val = bool(value)
                config.autostart = val
                winutil.set_autostart(val)
                summary["settings"].append(f"开机自启={'开' if val else '关'}")
            elif key == "notify":
                val = bool(value)
                config.notify = val
                summary["settings"].append(f"系统通知={'开' if val else '关'}")
            else:
                log.warning("未知的客户端设置项: %s", key)
        except Exception as exc:
            log.exception("应用客户端设置失败: %s", key)
            summary["errors"].append(f"设置 {key}（{exc}）")

    return summary


# ---------------- 自动同步（免待办直连拉取） ----------------
AUTO_SYNC_SKIP_LABELS = {
    "policy_missing": "服务器尚未下发自动同步策略",
    "policy_disabled": "服务器已关闭自动同步",
    "policy_empty": "服务器的同步范围为空白名单",
}


class AutoSyncThread(QThread):
    """按服务端策略对比服务器文件仓库与本地游戏目录，并自动下载差异。

    与 S2C 待办互不影响；只下载新增/变化文件，不删除本地文件。
    """

    done = Signal(bool, object, str)  # (成功, result dict, 错误信息)
    progress = Signal(int, int, str)

    def __init__(self, config: ClientConfig, profile: dict,
                 apply: bool = True, parent=None):
        super().__init__(parent)
        self.config = config
        self.profile = profile
        self.apply = apply

    def run(self):
        try:
            result = auto_sync_run(self.config, self.profile, self.apply,
                                   self._progress)
            self.done.emit(True, result, "")
        except AuthError as exc:
            log.warning("自动同步授权校验未通过: %s", exc)
            self.done.emit(False, None, f"授权:{exc}")
        except Exception as exc:
            log.exception("自动同步失败")
            self.done.emit(False, None, str(exc))

    def _progress(self, cur: int, total: int, msg: str):
        self.progress.emit(cur, total, msg)


def auto_sync_run(config: ClientConfig, profile: dict, apply: bool = True,
                  progress_cb=None) -> dict:
    """读取服务端策略 → 扫描对比 → （可选）下载差异文件。

    返回字段：
    - ``skipped``：非空表示本次没有执行（policy_missing / policy_disabled /
      policy_empty），其余字段为空；
    - ``downloads`` / ``installed``：计划下载、实际下载成功的相对路径；
    - ``extras``：本地有、服务器没有的文件（只报告，不删除）；
    - ``errors`` / ``warnings``：下载失败项与提示（如文件名大小写冲突）。
    """
    profile = profile or {}
    info = profile_sftp_info(profile)
    ident = client_identity(profile)  # 客户端身份：client_id，旧配置回退 server_id
    game_dir = config.local_mc_dir
    if not game_dir or not os.path.isdir(game_dir):
        raise RuntimeError("游戏目录无效，请先在主界面选择客户端根目录。")

    result = {
        "skipped": "", "policy": {},
        "remote_files": 0, "local_files": 0,
        "downloads": [], "installed": [], "errors": [], "extras": [],
        "total_bytes": 0, "bytes": 0, "warnings": [], "scanned_at": "",
    }
    with _connect(info) as sftp:
        verify(sftp, ident)
        policy = load_policy(sftp, info["todo_dir"])
        if policy is None:
            result["skipped"] = "policy_missing"
            return result
        result["policy"] = policy
        if not policy.get("enabled"):
            result["skipped"] = "policy_disabled"
            return result
        if not policy.get("include"):
            result["skipped"] = "policy_empty"
            return result
        plan = plan_sync(sftp, info["files_dir"], game_dir, policy,
                         progress_cb)
        for key in ("remote_files", "local_files", "downloads", "extras",
                    "total_bytes", "warnings", "scanned_at"):
            result[key] = plan.get(key, result.get(key))
        if apply and plan["downloads"]:
            summary = apply_sync(sftp, info["files_dir"], game_dir,
                                 plan["downloads"], progress_cb)
            result["installed"] = summary["installed"]
            result["errors"] = summary["errors"]
            result["bytes"] = summary["bytes"]
        elif apply and progress_cb:
            progress_cb(0, 0, "已是最新，无需下载")
    return result
