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

from app_common.app_config import ClientConfig
from app_common.constants import DEFAULT_REMOTE_FILES_DIR, DEFAULT_REMOTE_TODO_DIR
from app_common import winutil
from app_common.license import AuthError, verify
from app_common.logger import get_logger
from app_common.profile import parse_profile_content
from app_common.sftp import SFTPManager
from app_common.tasks import TodoManifest, safe_target

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
        "todo_dir": sftp.get("todo_dir") or DEFAULT_REMOTE_TODO_DIR,
        "files_dir": sftp.get("files_dir") or DEFAULT_REMOTE_FILES_DIR,
    }


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
    """依次检查每个服务器：授权校验 → 读取待办清单。

    返回 {server_id: {"ok": bool, "manifest": TodoManifest|None, "error": str}}
    """
    profiles = config.profiles()
    results: dict = {}
    for i, profile in enumerate(profiles):
        sid = profile.get("server_id", "")
        name = profile.get("name", "")
        try:
            info = profile_sftp_info(profile)
            with _connect(info) as sftp:
                verify(sftp, sid)
                manifest = TodoManifest.load_from_sftp(sftp, info["todo_dir"])
            if manifest is not None:
                config.mark_seen(sid, manifest.version,
                                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            results[sid] = {"ok": True, "manifest": manifest, "error": ""}
            log.info("检查完成: %s", name)
        except AuthError as exc:
            log.warning("授权校验未通过: %s（%s）", name, exc)
            results[sid] = {"ok": False, "manifest": None, "error": f"授权:{exc}"}
        except Exception as exc:
            log.exception("检查更新失败: %s", name)
            results[sid] = {"ok": False, "manifest": None, "error": str(exc)}
        if progress_cb:
            progress_cb(i + 1, len(profiles), name)
    return results


# ---------------- 应用更新 ----------------
class ApplyThread(QThread):
    """应用指定服务器的待办清单。"""

    done = Signal(bool, object, str)  # (成功, summary dict, 错误信息)
    progress = Signal(int, int, str)

    def __init__(self, config: ClientConfig, profile: dict, manifest: TodoManifest,
                 parent=None):
        super().__init__(parent)
        self.config = config
        self.profile = profile
        self.manifest = manifest

    def run(self):
        try:
            summary = apply_update(self.config, self.profile, self.manifest, self._progress)
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
                 progress_cb=None) -> dict:
    """校验编号后应用清单：下载/替换文件、删除文件。"""
    info = profile_sftp_info(profile)
    sid = profile.get("server_id", "")
    game_dir = config.local_mc_dir
    if not game_dir or not os.path.isdir(game_dir):
        raise RuntimeError("游戏目录无效，请先在主界面选择客户端根目录。")

    summary = {"installed": [], "deleted": [], "skipped": [], "errors": [],
               "settings": []}
    total = len(manifest.tasks)
    with _connect(info) as sftp:
        verify(sftp, sid)
        with tempfile.TemporaryDirectory(prefix="mc_sync_") as tmp:
            for i, task in enumerate(manifest.tasks):
                try:
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
                            sftp, info["files_dir"], task.source or target)
                        if not sftp.exists(remote):
                            summary["errors"].append(f"{target}（服务器缺少源文件）")
                        else:
                            local_tmp = os.path.join(tmp, f"f{i}")
                            sftp.download(remote, local_tmp)
                            os.makedirs(os.path.dirname(abs_target), exist_ok=True)
                            shutil.move(local_tmp, abs_target)
                            summary["installed"].append(target)
                            log.info("已安装: %s", target)
                    else:
                        summary["skipped"].append(f"{target}（未知操作 {task.action}）")
                except Exception as exc:
                    log.exception("任务执行失败: %s", task.target)
                    summary["errors"].append(f"{task.target}（{exc}）")
                if progress_cb:
                    progress_cb(i + 1, total, task.summary())

    # 应用客户端软件设置（随待办下发）
    for key, value in (manifest.settings or {}).items():
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
