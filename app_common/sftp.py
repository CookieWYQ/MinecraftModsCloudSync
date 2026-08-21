# -*- coding: utf-8 -*-
"""SFTP 客户端封装：连接、目录、上传下载、删除，异常统一转换为 SFTPError。

连接策略（针对免费/小主机等并发连接受限的服务器）：
- 全局并发上限：同一时刻最多 _CONN_MAX 个 SFTP 会话（连接后占用名额、close 时释放），
  避免页面自动检测/自检/并行扫描同时开太多连接被拒
- 连接失败自动重试（短暂退避），应对冷启动/限流的瞬时超时
- 目录扫描缓存：按（服务器+根目录）缓存最近一次完整扫描结果（内存，短 TTL），
  避免反复打开页面/重复点击扫描时重复读取同一棵目录树
"""
import queue as _queue
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

import paramiko

from .logger import get_logger

log = get_logger("sftp")

# 全局并发连接上限：主会话 1 + 扫描工作线程若干，扫描总占用不超过该值
_CONN_MAX = 5
_conn_sem = threading.BoundedSemaphore(_CONN_MAX)

# 目录扫描缓存：key=(host,port,username,root,excludes,skip_system) → (时间, 结果列表)
_scan_cache: dict[tuple, tuple] = {}
_SCAN_CACHE_TTL = 60.0  # 秒；写入型操作会主动清空缓存


def _invalidate_scan_cache() -> None:
    """任何修改服务端文件的操作后调用，保证缓存不返回过期数据。"""
    _scan_cache.clear()

# 根目录下的系统/临时目录，与 Minecraft 服务端内容无关。
# 扫描根目录（server_root=/）时直接跳过，避免把整个文件系统翻一遍拖慢快照。
SYSTEM_ROOT_DIRS = {
    "proc", "sys", "dev", "run", "tmp", "boot", "lost+found",
    "etc", "usr", "var", "bin", "sbin", "lib", "lib64",
    "root", "home", "opt", "srv", "media", "mnt", "snap",
}


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
        self._holds_conn_slot = False
        self._lock = threading.RLock()

    def _log_conn(self, text: str) -> None:
        if self.sanitize_log:
            log.info(text)
        else:
            log.info(text + " %s:%s", self.host, self.port)

    # ---------- 连接管理 ----------
    def connect(self, retries: int = 2) -> None:
        """建立连接；成功后占用一个全局并发名额直到 close()。

        - 认证错误等确定性失败不重试
        - 网络超时/被拒等瞬时错误重试 retries 次（每次递增退避）
        """
        if self._sftp is not None:
            return
        if not self._holds_conn_slot:
            _conn_sem.acquire()
            self._holds_conn_slot = True
        try:
            self._connect_attempts(retries)
        except BaseException:
            self.close()  # close 会释放全局名额
            raise

    def _connect_attempts(self, retries: int) -> None:
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                self._connect_once()
                return
            except SFTPError:
                raise  # 确定性错误（如认证失败），重试无意义
            except Exception as exc:
                last = exc
                if attempt < retries:
                    log.warning("SFTP 连接第 %d/%d 次失败，稍后重试: %s",
                                attempt + 1, retries,
                                "网络错误（详情已脱敏）" if self.sanitize_log else exc)
                    time.sleep(1.5 * (attempt + 1))
        log.error("SFTP 连接失败: %s",
                  "网络错误或凭据无效（详情已脱敏）" if self.sanitize_log else last)
        if self.sanitize_log:
            raise SFTPError("连接失败：网络错误或凭据无效（详情已脱敏）") from last
        raise SFTPError(f"连接失败: {last}") from last

    def _connect_once(self) -> None:
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

    def close(self) -> None:
        """关闭传输并释放全局并发名额（会话结束）。"""
        self._close_inner()
        if self._holds_conn_slot:
            self._holds_conn_slot = False
            try:
                _conn_sem.release()
            except (RuntimeError, ValueError):
                pass

    def _close_inner(self) -> None:
        """只关闭传输，不释放全局名额（供会话内掉线重连使用）。"""
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

    def is_alive(self) -> bool:
        """传输层是否仍存活（会话内掉线检测）。"""
        try:
            return bool(self._transport is not None and self._transport.is_active())
        except Exception:
            return False

    def reconnect(self) -> None:
        """会话内掉线后重建连接（保持已占用的全局并发名额）。"""
        self._close_inner()
        self.connect()

    def _is_conn_error(self, exc: Exception) -> bool:
        """异常是否表示 SFTP 连接已断开（可通过重连恢复），而非普通的目录不存在等。"""
        if isinstance(exc, (paramiko.SSHException, EOFError,
                            ConnectionError, TimeoutError)):
            return True
        if isinstance(exc, OSError):
            text = str(exc).lower()
            if ("socket is closed" in text or "connection dropped" in text
                    or "channel" in text or "getaddrinfo" in text):
                return True
            if not self.is_alive():
                return True
        return False

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

    def file_size(self, path: str) -> int:
        """返回远程文件大小（字节），不存在或读取失败时返回 -1。"""
        try:
            return self._sftp.stat(self._posix(path)).st_size
        except Exception:
            return -1

    def mkdirs(self, path: str) -> None:
        """递归创建远程目录。"""
        _invalidate_scan_cache()
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

    def listdir_attr(self, path: str) -> list:
        """列出远程目录条目（含属性），供内部并行遍历使用。"""
        return self._sftp.listdir_attr(self._posix(path))

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

    def list_files_recursive_with_size(self, path: str, excludes=None, skip_system: bool = False,
                                       max_workers: int = 3,
                                       progress_cb=None) -> list[tuple[str, int]]:
        """返回目录下所有文件 (相对路径, 大小) 列表，跳过隐藏项。

        - 内存缓存：按（服务器+根目录）缓存 60 秒内的完整扫描结果，重复打开页面 /
          重复点击扫描时直接复用，不再重复读取同一棵目录树；上传/删除等写入操作会自动清空缓存
        - 并行：max_workers>1 时每个工作线程一个独立连接并行列目录（受全局并发名额限制）；
          <=1 退化为串行、复用调用方连接
        - 掉线自动重连：连接被服务器断开时重连后继续，反复掉线才中止报错
        - excludes: 跳过的目录名集合（忽略大小写，任意层级命中即跳过该目录）
        - skip_system: 为真且扫描的是根目录时，跳过与 MC 无关的系统目录（/usr、/etc 等）
        - progress_cb: 进度回调 (已处理目录数, 已发现文件数)
        """
        excludes = {str(e).lower() for e in (excludes or [])}
        root = self._posix(path)
        cache_key = (self.host, self.port, self.username, root,
                     tuple(sorted(excludes)), bool(skip_system))
        now = time.time()
        hit = _scan_cache.get(cache_key)
        if hit is not None and now - hit[0] < _SCAN_CACHE_TTL:
            return list(hit[1])

        def _should_skip_dir(rel: str, name: str) -> bool:
            if name.startswith("."):
                return True
            if name.lower() in excludes:
                return True
            if skip_system and not rel and name in SYSTEM_ROOT_DIRS:
                return True
            return False

        if max_workers > 1:
            result = self._scan_parallel(path, root, _should_skip_dir, max_workers, progress_cb)
        else:
            result = self._scan_serial(path, root, _should_skip_dir, progress_cb)
        _scan_cache[cache_key] = (time.time(), list(result))
        return result

    def _scan_serial(self, path: str, root: str, _should_skip_dir, progress_cb=None):
        """串行遍历（复用调用方连接），掉线自动重连。"""
        result = []
        stack = [""]
        dirs_done = 0
        files_found = 0
        reconnects = 0
        max_reconnects = 5
        while stack:
            rel = stack.pop()
            full = self.join(path, rel) if rel else root
            while True:
                try:
                    entries = self.listdir_attr(full)
                    break
                except Exception as exc:
                    # 连接被服务器断开（Socket is closed / connection dropped）→ 重连续扫
                    if self._is_conn_error(exc) and reconnects < max_reconnects:
                        reconnects += 1
                        log.warning("连接中断，重连后继续扫描 %s…（第 %d/%d 次）",
                                    full, reconnects, max_reconnects)
                        try:
                            self.reconnect()
                        except Exception as exc2:
                            raise SFTPError(f"重连失败，扫描中止：{exc2}") from exc2
                        continue
                    if self._is_conn_error(exc):
                        # 反复掉线：宁可中止报错，也不要带残缺结果跑完
                        raise SFTPError(f"连接反复中断，扫描中止：{exc}") from exc
                    log.warning("无法列出 %s: %s", full, exc)
                    entries = []
                    break
            for e in entries:
                child_rel = f"{rel}/{e.filename}".strip("/") if rel else e.filename
                if stat.S_ISDIR(e.st_mode):
                    if _should_skip_dir(rel, e.filename):
                        continue
                    stack.append(child_rel)
                else:
                    result.append((child_rel, e.st_size))
            dirs_done += 1
            files_found += len(entries)
            if dirs_done % 25 == 0 and progress_cb:
                try:
                    progress_cb(dirs_done, files_found)
                except Exception:
                    pass
        if progress_cb:
            try:
                progress_cb(dirs_done, files_found)
            except Exception:
                pass
        return result

    def _scan_parallel(self, path: str, root: str, _should_skip_dir, max_workers: int,
                       progress_cb=None):
        """并行遍历：共享任务队列 + 每线程独立连接（受全局并发名额限制），掉线自动重连。"""
        q = _queue.Queue()
        results = []
        result_lock = threading.Lock()
        cond = threading.Condition()
        pending = 1            # 队列中 + 处理中的目录任务数
        errors: list[str] = []
        dirs_done = 0
        files_found = 0
        q.put("")              # 根目录任务

        def _emit():
            if progress_cb:
                try:
                    progress_cb(dirs_done, files_found)
                except Exception:
                    pass

        def worker():
            nonlocal pending, dirs_done, files_found
            conn = SFTPManager(self.host, self.port, self.username,
                               self.password, self.timeout, self.sanitize_log)
            try:
                conn.connect()
            except Exception as exc:
                with cond:
                    errors.append(str(exc))
                    if pending == 0:
                        cond.notify_all()
                return
            try:
                while True:
                    try:
                        rel = q.get(timeout=0.5)
                    except _queue.Empty:
                        with cond:
                            if pending == 0 and q.empty():
                                return
                        continue
                    with cond:
                        pending -= 1
                    full = self.join(path, rel) if rel else root
                    # 掉线重连续读（重连失败则中止该工作线程）
                    while True:
                        try:
                            entries = conn.listdir_attr(full)
                            break
                        except Exception as exc:
                            if conn._is_conn_error(exc):
                                try:
                                    conn.reconnect()
                                except Exception as exc2:
                                    with cond:
                                        errors.append(f"重连失败：{exc2}")
                                        if pending == 0:
                                            cond.notify_all()
                                    return
                                continue
                            log.warning("无法列出 %s: %s", full, exc)
                            entries = []
                            break
                    for e in entries:
                        child_rel = f"{rel}/{e.filename}".strip("/") if rel else e.filename
                        if stat.S_ISDIR(e.st_mode):
                            if _should_skip_dir(rel, e.filename):
                                continue
                            with cond:
                                q.put(child_rel)
                                pending += 1
                        else:
                            with result_lock:
                                results.append((child_rel, e.st_size))
                    with cond:
                        dirs_done += 1
                        files_found += len(entries)
                        if pending == 0:
                            cond.notify_all()
                    if dirs_done % 25 == 0:
                        _emit()
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(worker) for _ in range(max_workers)]
            for fut in futures:
                fut.result()
        _emit()
        if not results and errors:
            raise SFTPError(f"读取服务端目录失败：{errors[0]}")
        return results

    def list_entries(self, path: str) -> list[tuple[str, bool, int, int]]:
        """返回目录下的条目 [(名称, 是否目录, 大小, 修改时间戳)]，跳过隐藏项，按名称排序。"""
        try:
            entries = self._sftp.listdir_attr(self._posix(path))
        except (FileNotFoundError, IOError):
            return []
        except Exception as exc:
            raise SFTPError(f"列出目录失败: {path} ({exc})") from exc
        result = []
        for e in entries:
            if e.filename.startswith("."):
                continue
            result.append((e.filename, stat.S_ISDIR(e.st_mode), e.st_size,
                           int(e.st_mtime or 0)))
        result.sort(key=lambda x: x[0].lower())
        return result

    def read_text(self, path: str) -> str:
        try:
            with self._sftp.open(self._posix(path), "r") as f:
                return f.read().decode("utf-8")
        except Exception as exc:
            raise SFTPError(f"读取远程文件失败: {path} ({exc})") from exc

    def write_text(self, path: str, text: str) -> None:
        _invalidate_scan_cache()
        try:
            with self._sftp.open(self._posix(path), "w") as f:
                f.write(text.encode("utf-8"))
        except Exception as exc:
            raise SFTPError(f"写入远程文件失败: {path} ({exc})") from exc

    def upload(self, local_path: str | Path, remote_path: str) -> None:
        _invalidate_scan_cache()
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
        _invalidate_scan_cache()
        try:
            self._sftp.remove(self._posix(remote_path))
        except FileNotFoundError:
            pass
        except Exception as exc:
            raise SFTPError(f"删除远程文件失败: {remote_path} ({exc})") from exc

    def delete_dir(self, remote_path: str) -> None:
        """递归删除远程目录。"""
        _invalidate_scan_cache()
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
