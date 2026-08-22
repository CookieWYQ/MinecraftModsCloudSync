# -*- coding: utf-8 -*-
"""文件哈希工具：用于判断两个文件是否「内容相同」。

单纯比较文件大小可能漏判（内容被替换但大小恰好相同），
S2C / C2S / C2C 三个模块统一用「大小 + 哈希」双条件判定：
- 大小不同 → 必然不同
- 大小相同 → 再比对哈希（默认 MD5）确认内容是否一致
"""
import hashlib
import os
import tempfile

from .logger import get_logger
from .sftp import SFTPManager

log = get_logger("file_hash")

_CHUNK = 1 << 20  # 1 MB


def hash_file(path: str, algo: str = "md5") -> str:
    """计算本地文件哈希（默认 MD5）。文件不存在/读取失败时抛出 OSError。"""
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def hash_remote(sftp: SFTPManager, remote_path: str, algo: str = "md5") -> str | None:
    """下载远程文件到临时文件并计算哈希，计算后删除临时文件。

    失败（文件不存在 / 下载 / 读取错误）返回 None，由调用方按「无法确认」保守处理。
    """
    h = hashlib.new(algo)
    tmp = tempfile.mktemp(prefix="mc_sync_hash_")
    try:
        sftp.download(remote_path, tmp)
        with open(tmp, "rb") as f:
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception as exc:
        log.warning("计算远程文件哈希失败: %s（%s）", remote_path, exc)
        return None
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def hash_remote_smart(sftp: SFTPManager, remote_path: str,
                      algo: str = "md5") -> str | None:
    """优先在远程直接计算哈希（SFTP check-file 扩展，OpenSSH 8+），
    服务器不支持时回退为下载计算。网络慢时远程计算只传输一个字符串，检测不会超时。
    """
    try:
        h = sftp.remote_hash(remote_path, algo)
    except Exception:
        h = None
    if h:
        return h
    return hash_remote(sftp, remote_path, algo)
