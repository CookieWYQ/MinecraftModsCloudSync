# -*- coding: utf-8 -*-
"""本地测试用 SFTP 服务器（纯 paramiko 实现，无外部服务依赖）。

把本机一个临时目录映射成 SFTP 根目录（`/` = 该目录），支持本项目用到的全部操作：
列目录、stat、上传/下载、删除、建目录、rename / posix-rename。
不支持 OpenSSH 的 check-file 扩展（返回 OP_UNSUPPORTED），正好可以顺带验证
`file_hash.hash_remote_*` 的「远程哈希不可用 → 回退下载计算」路径。

用法：
    srv = FakeSFTPServer(root)
    host, port = srv.start()
    ...
    srv.stop()

仅供测试；不要用于生产。
"""
import os
import posixpath
import socket
import sys
import threading
import time

import paramiko
from paramiko import (
    AUTH_SUCCESSFUL,
    OPEN_SUCCEEDED,
    SFTPAttributes,
    SFTPHandle,
    SFTPServer,
    SFTPServerInterface,
)
from paramiko.sftp import SFTP_OK

# 支持 `python tests/fake_sftp.py` 从仓库根目录直接自检（sys.path[0] 是 tests/）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 允许的 Windows 文件名，无需特殊处理；所有远程路径按 POSIX 处理。


class _TestHandle(SFTPHandle):
    """打开文件的句柄：read/write 用 paramiko 默认实现（依赖 readfile/writefile）。"""

    def stat(self):
        try:
            return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)

    def chattr(self, attr):
        try:
            if getattr(attr, "st_size", None) is not None and self.readfile is not None:
                os.truncate(self.readfile.fileno(), attr.st_size)
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        return SFTP_OK


class _LocalSFTPServer(SFTPServerInterface):
    """把 SFTP 根目录映射到本机目录的实现。"""

    def __init__(self, server, *args, root=None, **kwargs):
        super().__init__(server, *args, **kwargs)
        self.root = os.path.abspath(root or ".")

    # ---------- 路径 ----------
    @staticmethod
    def _norm(path: str) -> str:
        p = posixpath.normpath("/" + str(path or "").replace("\\", "/").lstrip("/"))
        return p

    def _local(self, path: str) -> str:
        rel = self._norm(path).lstrip("/")
        parts = [p for p in rel.split("/") if p and p != "."]
        return os.path.join(self.root, *parts)

    def canonicalize(self, path):
        return self._norm(path)

    # ---------- 目录 ----------
    def list_folder(self, path):
        local = self._local(path)
        try:
            names = os.listdir(local)
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        out = []
        for name in names:
            try:
                attr = SFTPAttributes.from_stat(os.stat(os.path.join(local, name)))
            except OSError:
                continue
            attr.filename = name
            out.append(attr)
        return out

    def mkdir(self, path, attr):
        try:
            os.mkdir(self._local(path))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        return SFTP_OK

    def rmdir(self, path):
        try:
            os.rmdir(self._local(path))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        return SFTP_OK

    # ---------- 文件 ----------
    def stat(self, path):
        try:
            attr = SFTPAttributes.from_stat(os.stat(self._local(path)))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        attr.filename = posixpath.basename(self._norm(path)) or "/"
        return attr

    def lstat(self, path):
        return self.stat(path)

    def open(self, path, flags, attr):
        local = self._local(path)
        writing = bool(flags & (os.O_WRONLY | os.O_RDWR))
        if writing:
            if flags & os.O_APPEND:
                mode = "ab"
            elif flags & os.O_TRUNC:
                mode = "wb"
            elif os.path.exists(local):
                mode = "r+b"
            else:
                mode = "wb"
        else:
            mode = "rb"
        try:
            f = open(local, mode)
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        handle = _TestHandle(flags)
        handle.filename = path
        handle.readfile = f
        handle.writefile = f
        return handle

    def remove(self, path):
        try:
            os.remove(self._local(path))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        return SFTP_OK

    def rename(self, oldpath, newpath):
        try:
            os.replace(self._local(oldpath), self._local(newpath))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        return SFTP_OK

    def posix_rename(self, oldpath, newpath):
        return self.rename(oldpath, newpath)

    def chattr(self, path, attr):
        return SFTP_OK


class _TestSSHServer(paramiko.ServerInterface):
    def __init__(self):
        self.event = threading.Event()

    def check_auth_password(self, username, password):
        # 测试服务器：任意账号密码都放行
        return AUTH_SUCCESSFUL

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_subsystem_request(self, channel, name):
        return super().check_channel_subsystem_request(channel, name)


class FakeSFTPServer:
    """在随机端口上启动一个本地 SFTP 服务，root 目录映射为 `/`。"""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self.host = "127.0.0.1"
        self.port = 0
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._transports: list[paramiko.Transport] = []
        self._lock = threading.Lock()
        self._running = False
        self._host_key = None

    def start(self) -> tuple[str, int]:
        if self._running:
            return self.host, self.port
        self._host_key = paramiko.RSAKey.generate(2048)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, 0))
        sock.listen(50)
        self._sock = sock
        self.port = sock.getsockname()[1]
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        return self.host, self.port

    def _accept_loop(self):
        while self._running:
            try:
                client, _addr = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._session, args=(client,), daemon=True).start()

    def _session(self, client: socket.socket):
        try:
            transport = paramiko.Transport(client)
            transport.add_server_key(self._host_key)
            transport.set_subsystem_handler(
                "sftp", SFTPServer, _LocalSFTPServer, root=self.root)
            with self._lock:
                self._transports.append(transport)
            transport.start_server(server=_TestSSHServer())
            while transport.is_active():
                time.sleep(0.05)
        except Exception:
            pass

    def stop(self):
        self._running = False
        try:
            if self._sock is not None:
                self._sock.close()
        except OSError:
            pass
        with self._lock:
            transports = list(self._transports)
            self._transports.clear()
        for t in transports:
            try:
                t.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None


def main() -> None:
    """自检：起服务 → 上传/列目录/下载/删除，全部通过即输出 OK。"""
    import tempfile

    tmp = tempfile.mkdtemp(prefix="fakesftp_")
    srv = FakeSFTPServer(tmp)
    host, port = srv.start()
    try:
        from app_common.sftp import SFTPManager

        local_src = os.path.join(tmp, "upload_src.bin")
        with open(local_src, "wb") as f:
            f.write(b"hello sftp" * 100)
        with SFTPManager(host, port, "u", "p") as sftp:
            sftp.mkdirs("/a/b")
            sftp.upload(local_src, "/a/b/file.bin")
            assert sftp.exists("/a/b/file.bin")
            assert sftp.file_size("/a/b/file.bin") == 1000
            assert sftp.list_files_recursive_with_size("/a") == [("b/file.bin", 1000)]
            out = os.path.join(tmp, "download.bin")
            sftp.download("/a/b/file.bin", out)
            assert open(out, "rb").read() == b"hello sftp" * 100
            sftp.mkdirs("/todo")
            sftp.write_text("/todo/manifest.json", '{"x": 1}')
            assert sftp.read_text("/todo/manifest.json") == '{"x": 1}'
            sftp.move("/a/b/file.bin", "/a/b/renamed.bin")
            assert sftp.exists("/a/b/renamed.bin")
            sftp.delete("/a/b/renamed.bin")
            sftp.delete_dir("/a")
            assert not sftp.exists("/a")
            # 远程哈希不可用 → 回退下载计算，结果应为真实 md5
            import hashlib

            with open(local_src, "rb") as f:
                expect = hashlib.md5(f.read()).hexdigest()
            assert sftp.remote_hash("/upload_src.bin") is None
            from app_common.file_hash import hash_remote_smart

            assert hash_remote_smart(sftp, "/upload_src.bin") == expect
        print("FAKE SFTP OK")
    finally:
        srv.stop()


if __name__ == "__main__":
    main()
