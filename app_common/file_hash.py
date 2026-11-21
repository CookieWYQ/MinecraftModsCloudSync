# -*- coding: utf-8 -*-
"""文件哈希工具：用于判断两个文件是否「内容相同」。

单纯比较文件大小可能漏判（内容被替换但大小恰好相同），
S2C / C2S / C2C 三个模块统一用「大小 + 哈希」双条件判定：
- 大小不同 → 必然不同
- 大小相同 → 再比对哈希（默认 MD5）确认内容是否一致
"""
import hashlib
import os
import threading

from .logger import get_logger
from .sftp import MAX_SCAN_WORKERS, SFTPManager

log = get_logger("file_hash")

_CHUNK = 1 << 20  # 1 MB

# 默认并行哈希连接数（软件设置里可调 1~8）；多连接是慢网络/大模组包场景的关键
DEFAULT_HASH_WORKERS = 6

# 本地哈希缓存：path → (大小, 修改时间, 哈希)。
# 重复扫描（每次发送后都重建快照）时，绝大多数文件没变，直接复用上次结果，
# 避免把整个模组包重新读一遍（几百 MB ~ 几 GB 的磁盘 IO）。
# 仅在「大小 + 修改时间」都没变时复用，文件一改动即失效。
_LOCAL_CACHE: dict[str, tuple[int, float, str]] = {}
_LOCAL_CACHE_MAX = 50000
_local_cache_lock = threading.Lock()


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


def hash_file_cached(path: str, algo: str = "md5") -> str:
    """本地文件哈希（带缓存）。

    仅当「大小 + 修改时间」与上次一致时复用缓存值（文件内容变了必然改变大小或时间），
    否则重新读取计算。用于重复扫描本地模组目录，避免每次重扫都把整个包读一遍。
    """
    if algo != "md5":
        return hash_file(path, algo)
    st = os.stat(path)  # 失败时抛 OSError，由调用方决定跳过
    with _local_cache_lock:
        hit = _LOCAL_CACHE.get(path)
        if hit is not None and hit[0] == st.st_size and hit[1] == st.st_mtime:
            return hit[2]
    value = hash_file(path, algo)
    with _local_cache_lock:
        if len(_LOCAL_CACHE) >= _LOCAL_CACHE_MAX:
            _LOCAL_CACHE.clear()
        _LOCAL_CACHE[path] = (st.st_size, st.st_mtime, value)
    return value


def hash_remote(sftp: SFTPManager, remote_path: str, algo: str = "md5") -> str | None:
    """流式读取远程文件并计算哈希（不落临时文件）。

    失败（文件不存在 / 网络 / 读取错误）返回 None，由调用方按「无法确认」保守处理。
    服务器不支持 ``check-file`` 扩展时走这里：直接 ``open`` 后边读边算，
    省掉「下载到临时文件 → 再从磁盘读一遍」的双重 IO；多连接时每个连接独立流式读取。
    """
    h = hashlib.new(algo)
    try:
        with sftp.open(remote_path, "rb") as f:
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception as exc:
        log.warning("计算远程文件哈希失败: %s（%s）", remote_path, exc)
        return None


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


def _hash_tasks_one_conn(conn: SFTPManager, tasks: list[tuple[str, str]],
                         progress_cb=None) -> dict[str, str]:
    """在一条连接上完成一批哈希任务：先流水线批量算，取不到的再逐文件回退。

    「流水线」指一次发出多个 check-file 请求再依次收响应，把逐文件的网络往返
    摊销掉（模组包上千个文件时这是主要耗时）。服务器不支持该扩展时
    remote_hash_many 会返回空，这里自动回退为 hash_remote_smart（必要时下载）。
    """
    keys_of: dict[str, list[str]] = {}
    for k, remote in tasks:
        keys_of.setdefault(remote, []).append(k)

    with_remote = conn.remote_hash_many(list(keys_of))
    # 整批一个都没算出来 → 服务器不支持 check-file，后面直接下载计算，
    # 不再对每个文件重试一次注定失败的远程哈希
    fallback = hash_remote if not with_remote else hash_remote_smart
    result: dict[str, str] = {}
    done = 0
    total = len(tasks)
    for remote, keys in keys_of.items():
        h = with_remote.get(remote)
        if h is None:
            h = fallback(conn, remote)
        if h:
            for k in keys:
                result[k] = h
        done += len(keys)
        if progress_cb:
            try:
                progress_cb(done, total, keys[0])
            except Exception:
                pass
    return result


def hash_remote_parallel(sftp: SFTPManager | None,
                         host: str, port: int, username: str, password: str,
                         tasks: list[tuple[str, str]],
                         progress_cb=None, workers: int | None = None) -> dict[str, str]:
    """并行计算多个远程文件哈希（快速哈希优先：SFTP check-file，不下载文件）。

    - tasks: [(key, remote_path), ...]，key 仅用于回填与进度显示
    - 返回 {key: md5}；计算失败的条目不包含在内
    - 每条连接内部再走「批量流水线」，往返次数由 文件数 降为 文件数/batch，
      多文件（模组包）场景比逐文件串行快一个数量级
    - workers>1 时每个工作线程一个独立连接（受 sftp 模块全局并发名额限制），
      默认 DEFAULT_HASH_WORKERS（软件设置里可调）；workers<=1 且传入 sftp 时复用该连接
    - 传入的 sftp 主连接会直接承担一片任务，不额外再建一条连接；某片线程建连失败
      （小主机限制并发）时，剩余任务会自动落到单连接重试，避免整批哈希缺失
    - progress_cb(done, total, key) 可选
    """
    total = len(tasks)
    if total == 0:
        return {}
    try:
        wanted = int(workers) if workers is not None else DEFAULT_HASH_WORKERS
    except (TypeError, ValueError):
        wanted = DEFAULT_HASH_WORKERS
    n = min(max(wanted, 1), MAX_SCAN_WORKERS, total)
    if n <= 1:
        if sftp is not None:
            return _hash_tasks_one_conn(sftp, tasks, progress_cb)
        with SFTPManager(host, port, username, password) as conn:
            return _hash_tasks_one_conn(conn, tasks, progress_cb)

    lock = threading.Lock()
    counter = {"done": 0}

    def on_progress(_local_done: int, _local_total: int, key: str):
        if not progress_cb:
            return
        with lock:
            counter["done"] += 1
            done = min(counter["done"], total)
        try:
            progress_cb(done, total, key)
        except Exception:
            pass

    result: dict[str, str] = {}
    failed: list[list[tuple[str, str]]] = []

    def hash_on_conn(conn, slice_):
        try:
            part = _hash_tasks_one_conn(conn, slice_, on_progress)
        except Exception as exc:
            log.warning("哈希工作线程失败: %s", exc)
            return None
        with lock:
            result.update(part)
        return part

    def worker(slice_):
        try:
            with SFTPManager(host, port, username, password) as conn:
                part = hash_on_conn(conn, slice_)
        except Exception as exc:
            log.warning("哈希工作连接建立失败: %s", exc)
            part = None
        if part is None:
            with lock:
                failed.append(slice_)

    threads = [threading.Thread(target=worker, args=(tasks[i::n],), daemon=True)
               for i in range(1, n)]

    # 主连接直接跑第 0 片（不额外占一个连接名额），其余并行
    if sftp is not None:
        for t in threads:
            t.start()
        part = hash_on_conn(sftp, tasks[0::n])
        if part is None:
            failed.append(tasks[0::n])
    else:
        threads.insert(0, threading.Thread(target=worker, args=(tasks[0::n],),
                                           daemon=True))
        for t in threads:
            t.start()
    for t in threads:
        t.join()

    # 有切片整片失败（通常是并发连接被服务器拒绝）→ 剩余任务落到单连接重试
    if failed:
        retry = [item for slice_ in failed for item in slice_
                 if item[0] not in result]
        if retry:
            log.warning("并行哈希有 %d 个任务失败，改用单连接重试", len(retry))
            try:
                if sftp is not None and sftp.is_alive():
                    part = _hash_tasks_one_conn(sftp, retry, on_progress)
                else:
                    with SFTPManager(host, port, username, password) as conn:
                        part = _hash_tasks_one_conn(conn, retry, on_progress)
                result.update(part)
            except Exception as exc:
                log.warning("哈希单连接重试失败: %s", exc)
    return result
