# -*- coding: utf-8 -*-
"""单实例互斥：确保客户端 / 服务端程序同时只运行一个 GUI 实例。

互斥权威：
- Windows：命名互斥量（CreateMutexW）。进程退出（含崩溃）时由内核自动释放，
  不存在共享内存段残留导致新实例无法启动的问题；
- 其他平台：QSharedMemory（原方案）。

PID 记录：写入 QSharedMemory 小段，仅供「激活已运行实例窗口」使用（尽力而为）。

重复启动行为：定位已运行实例的主窗口并**激活到前台**（还原最小化 / 托盘隐藏），
然后本实例退出——不再弹窗、不再启动第二个主窗口。
"""
import os
import struct
import time

from PySide6.QtCore import QSharedMemory

from .logger import get_logger

log = get_logger("single_instance")

# 持有互斥句柄 / 共享内存对象的强引用，防止被回收导致互斥锁失效
_live: list = []


# ---------- 互斥获取 ----------
def _acquire_win(mutex_key: str) -> int | None:
    """Windows 命名互斥量独占。返回互斥句柄（进程退出自动释放）或 None（已有实例）。"""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    ERROR_ALREADY_EXISTS = 183
    handle = kernel32.CreateMutexW(None, True, mutex_key)
    if not handle:
        return None
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle


def _acquire_shm(key: str) -> QSharedMemory | None:
    """QSharedMemory 独占（非 Windows fallback）。"""
    shm = QSharedMemory(key)
    if shm.create(4):
        _live.append(shm)
        return shm
    return None


# ---------- PID 记录（用于激活已运行实例） ----------
def _write_pid(pid_key: str, pid: int) -> None:
    shm = QSharedMemory(pid_key)
    if not shm.create(4) and not shm.attach():
        return
    try:
        shm.lock()
        struct.pack_into("I", shm.data(), 0, pid)
    except (TypeError, ValueError):
        pass
    finally:
        shm.unlock()
    _live.append(shm)


def _read_pid(pid_key: str) -> int | None:
    shm = QSharedMemory(pid_key)
    if not shm.attach():
        return None
    pid = None
    try:
        shm.lock()
        pid = struct.unpack_from("I", shm.data(), 0)[0]
    except (TypeError, ValueError):
        pid = None
    finally:
        shm.unlock()
    shm.detach()
    return pid if pid else None


# ---------- 激活已运行实例窗口 ----------
# 跨进程恢复 Qt 窗口的机制：
# Qt 6 隐藏（hide() 驻留托盘）的窗口不接受外部 ShowWindow 恢复（内部状态会拒绝），
# 因此改为：向主窗口发送一条注册的 Windows 自定义消息，由程序自身在 nativeEvent
# 中调用 showNormal()/raise_()/activateWindow() 恢复显示。
_WIN_ACTIVATE_MSG_PREFIX = "MCSync.ActivateWindow."


def _activate_msg_id(scope: str) -> int:
    """注册（或取回）激活消息号。RegisterWindowMessage 同一会话内同名返回相同值。"""
    import ctypes
    user32 = ctypes.WinDLL("user32")
    user32.RegisterWindowMessageW.argtypes = [ctypes.c_wchar_p]
    user32.RegisterWindowMessageW.restype = ctypes.c_uint
    return user32.RegisterWindowMessageW(_WIN_ACTIVATE_MSG_PREFIX + scope)


def _activate_existing(pid: int, scope: str = "") -> bool:
    """把已运行实例（按 PID）的主窗口带到前台并还原显示，返回是否定位到窗口。

    - 先 ShowWindow(SW_RESTORE)：兼容「最小化」窗口（对 Qt 隐藏窗口无效但无害）；
    - 再发送注册的自定义消息：Qt 程序在 nativeEvent 中自行恢复显示与置前，
      解决 ShowWindow 无法恢复 Qt 隐藏（托盘驻留）窗口的问题；
    - Windows 会阻止后台进程抢占前台，程序侧 activateWindow 前可配合 Alt 键模拟。
    """
    if not pid or os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    user32.EnumWindows.argtypes = [
        ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
        wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL

    found: list = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum_cb(hwnd, _lparam):
        pid_here = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_here))
        if pid_here.value == pid:
            found.append(hwnd)
        return True

    user32.EnumWindows(_enum_cb, 0)
    if not found:
        return False
    # 优先取可见窗口（主窗口），否则取第一个顶层窗口（可能是托盘驻留的隐藏窗口）
    hwnd = next((h for h in found if user32.IsWindowVisible(h)), found[0])
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    if scope:
        user32.PostMessageW.argtypes = [
            wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.PostMessageW(hwnd, _activate_msg_id(scope), 0, 0)
    log.info("已向已运行实例（pid=%s）发送激活消息", pid)
    return True


# ---------- 入口 ----------
def ensure_single_instance(scope: str, parent=None, silent: bool = False) -> bool:
    """确保单实例。返回 True=本实例获得独占可继续；False=已有实例（已激活其窗口）。

    - scope: 'client' / 'server'（区分两端，互不干扰）
    - parent / silent: 兼容旧调用保留，行为统一为「激活已运行实例后退出」
    """
    pid_key = f"mc_sync_pid_v1_{scope}"
    if os.name == "nt":
        mutex_key = f"Local\\mc_sync_mutex_v1_{scope}"
        handle = _acquire_win(mutex_key)
        if handle is not None:
            _live.append(handle)
            _write_pid(pid_key, os.getpid())
            return True
        old_pid = _read_pid(pid_key)
        log.info("检测到已有实例在运行（scope=%s, pid=%s），激活其窗口", scope, old_pid)
        if old_pid:
            _activate_existing(old_pid)
        time.sleep(0.2)  # 让前台切换完成后再退出，避免用户看到新窗口一闪而过
        return False

    # 非 Windows：QSharedMemory 方案
    key = f"mc_sync_single_v1_{scope}"
    if _acquire_shm(key):
        _write_pid(pid_key, os.getpid())
        return True
    old_pid = _read_pid(pid_key)
    if old_pid:
        _activate_existing(old_pid, scope)
    time.sleep(0.2)
    return False
