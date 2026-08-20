# -*- coding: utf-8 -*-
"""SFTP 客户端封装：连接、目录、上传下载、删除，异常统一转换为 SFTPError。"""
import stat
import threading
from pathlib import Path, PurePosixPath

import paramiko

from .logger import get_logger

log = get_logger("sftp")


class SFTPError(Exception):
    """SFTP 操作失败。"""


class SFTPManager:
    def __init__(self, host: str, port: int, username: str, password: str,
                 timeout: int = 15, sanitize_log: bool = False):
        self.host = host
        self.port = int(port or 22)
        self.username = username
        self.password = password
        self.timeout = timeout
        # 客户端模式：日志/异常中不出现服务器地址与凭据
        self.sanitize_log = sanitize_log
        self._transport = None
        self._sftp = None
        self._lock = threading.RLock()

    def _log_conn(self, text: str) -> None:
        if self.sanitize_log:
            log.info(text)
        else:
            log.info(text + " %s:%s", self.host, self.port)

    # ---------- 连接管理 ----------
    def connect(self) -> None:
        if self._sftp is not None:
            return
        try:
            transport = paramiko.Transport((self.host, self.port))
            transport.banner_timeout = self.timeout
            transport.connect_timeout = self.timeout
            transport.start_client(timeout=self.timeout)
            transport.auth_password(self.username, self.password)
            if not transport.is_authenticated():
                raise SFTPError("认证失败：用户名或密码错误")
            sftp = paramiko.SFTPClient.from_transport(transport)
            sftp.get_channel().settimeout(self.timeout)
            self._transport, self._sftp = transport, sftp
            self._log_conn("SFTP 连接成功")
        except SFTPError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            log.exception("SFTP 连接失败")
            if self.sanitize_log:
                raise SFTPError("连接失败：网络错误或凭据无效（详情已脱敏）") from exc
            raise SFTPError(f"连接失败: {exc}") from exc

    def close(self) -> None:
        try:
            if self._sftp is not None:
                self._sftp.close()
        except Exception:
            pass
        try:
            if self._transport is not None:
                self._transport.close()
        except Exception:
            pass
        self._sftp = None
        self._transport = None

    def __enter__(self) -> "SFTPManager":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- 路径工具 ----------
    @staticmethod
    def _posix(path: str) -> str:
        path = path.replace("\\", "/")
        return "/" + PurePosixPath(path).as_posix().lstrip("/") if path else "/"

    @staticmethod
    def join(*parts: str) -> str:
        cleaned = [p.strip("/") for p in parts if p.strip("/")]
        return "/" + "/".join(cleaned) if cleaned else "/"

    # ---------- 基本操作 ----------
    def exists(self, path: str) -> bool:
        try:
            self._sftp.stat(self._posix(path))
            return True
        except (FileNotFoundError, IOError):
            return False

    def is_dir(self, path: str) -> bool:
        try:
            mode = self._sftp.stat(self._posix(path)).st_mode
            return stat.S_ISDIR(mode)
        except Exception:
            return False

    def mkdirs(self, path: str) -> None:
        """递归创建远程目录。"""
        parts = [p for p in self._posix(path).split("/") if p]
        cur = ""
        for part in parts:
            cur = self.join(cur, part)
            if not self.exists(cur):
                self._sftp.mkdir(cur)

    def list_dir(self, path: str) -> list[str]:
        try:
            return [e.filename for e in self._sftp.listdir_attr(self._posix(path))]
        except (FileNotFoundError, IOError):
            return []
        except Exception as exc:
            raise SFTPError(f"列出目录失败: {path} ({exc})") from exc

    def list_files_recursive(self, path: str) -> list[str]:
        """返回目录下所有文件相对于 path 的路径列表。"""
        result = []
        stack = [""]
        while stack:
            rel = stack.pop()
            full = self.join(path, rel) if rel else self._posix(path)
            try:
                entries = self._sftp.listdir_attr(full)
            except Exception as exc:
                log.warning("无法列出 %s: %s", full, exc)
                continue
            for e in entries:
                child_rel = f"{rel}/{e.filename}".strip("/") if rel else e.filename
                mode = e.st_mode
                if stat.S_ISDIR(mode):
                    stack.append(child_rel)
                else:
                    result.append(child_rel)
        return result

    def read_text(self, path: str) -> str:
        try:
            with self._sftp.open(self._posix(path), "r") as f:
                return f.read().decode("utf-8")
        except Exception as exc:
            raise SFTPError(f"读取远程文件失败: {path} ({exc})") from exc

    def write_text(self, path: str, text: str) -> None:
        try:
            with self._sftp.open(self._posix(path), "w") as f:
                f.write(text.encode("utf-8"))
        except Exception as exc:
            raise SFTPError(f"写入远程文件失败: {path} ({exc})") from exc

    def upload(self, local_path: str | Path, remote_path: str) -> None:
        local = Path(local_path)
        remote = self._posix(remote_path)
        self.mkdirs(str(PurePosixPath(remote).parent))
        try:
            self._sftp.put(str(local), remote)
        except Exception as exc:
            raise SFTPError(f"上传失败: {local.name} ({exc})") from exc

    def download(self, remote_path: str, local_path: str | Path) -> None:
        local = Path(local_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._sftp.get(self._posix(remote_path), str(local))
        except Exception as exc:
            raise SFTPError(f"下载失败: {remote_path} ({exc})") from exc

    def delete(self, remote_path: str) -> None:
        try:
            self._sftp.remove(self._posix(remote_path))
        except FileNotFoundError:
            pass
        except Exception as exc:
            raise SFTPError(f"删除远程文件失败: {remote_path} ({exc})") from exc

    def delete_dir(self, remote_path: str) -> None:
        """递归删除远程目录。"""
        try:
            stack = [self._posix(remote_path)]
            while stack:
                cur = stack[-1]
                try:
                    entries = self._sftp.listdir_attr(cur)
                except FileNotFoundError:
                    stack.pop()
                    continue
                if not entries:
                    self._sftp.rmdir(cur)
                    stack.pop()
                    continue
                for e in entries:
                    child = f"{cur}/{e.filename}"
                    if stat.S_ISDIR(e.st_mode):
                        stack.append(child)
                    else:
                        self._sftp.remove(child)
        except Exception as exc:
            raise SFTPError(f"删除远程目录失败: {remote_path} ({exc})") from exc
