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


def hash_remote_parallel(sftp: SFTPManager | None,
                         host: str, port: int, username: str, password: str,
                         tasks: list[tuple[str, str]],
                         progress_cb=None, workers: int = 3) -> dict[str, str]:
    """并行计算多个远程文件哈希（快速哈希优先：SFTP check-file，不下载文件）。

    - tasks: [(key, remote_path), ...]，key 仅用于回填与进度显示
    - 返回 {key: md5}；计算失败的条目不包含在内
    - workers>1 时每个工作线程一个独立连接（受 sftp 模块全局并发名额限制），
      显著减少多文件场景下的往返等待；workers<=1 且传入 sftp 时复用该连接串行计算
    - progress_cb(done, total, key) 可选
    """
    import queue
    import threading

    total = len(tasks)
    if total == 0:
        return {}
    n = min(max(workers, 1), 3, total)
    if n <= 1 and sftp is not None:
        result: dict[str, str] = {}
        for i, (k, remote) in enumerate(tasks):
            h = hash_remote_smart(sftp, remote)
            if h:
                result[k] = h
            if progress_cb:
                try:
                    progress_cb(i + 1, total, k)
                except Exception:
                    pass
        return result

    from .sftp import SFTPManager as _SFTPManager

    q: queue.Queue = queue.Queue()
    for t in tasks:
        q.put(t)
    result: dict[str, str] = {}
    lock = threading.Lock()
    done = 0

    def worker():
        nonlocal done
        with _SFTPManager(host, port, username, password) as conn:
            while True:
                try:
                    k, remote = q.get_nowait()
                except queue.Empty:
                    return
                h = hash_remote_smart(conn, remote)
                with lock:
                    if h:
                        result[k] = h
                    done += 1
                    d = done
                if progress_cb:
                    try:
                        progress_cb(d, total, k)
                    except Exception:
                        pass

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return result
