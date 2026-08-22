# -*- coding: utf-8 -*-
"""单实例互斥：确保客户端 / 服务端程序同时只运行一个 GUI 实例。

- 通过 QSharedMemory 互斥（键名区分客户端 / 服务端），并在共享内存中记录持有者 PID。
- 重复启动时：提示用户是否关闭旧版本进程后继续（按 PID 精确结束旧实例，不误杀本实例）；
  用户选择不关闭则退出。--tray 静默启动时冲突直接退出，不弹窗打扰。
"""
import os
import struct
import subprocess
import time

from PySide6.QtCore import QSharedMemory

from . import winutil
from .logger import get_logger

log = get_logger("single_instance")

# 持有共享内存对象的强引用，防止被回收导致互斥锁失效
_live: list = []


def _acquire(key: str) -> QSharedMemory | None:
    shm = QSharedMemory(key)
    if shm.create(4):
        try:
            shm.lock()
            struct.pack_into("I", shm.data(), 0, os.getpid())
        except (TypeError, ValueError):
            pass
        finally:
            shm.unlock()
        _live.append(shm)
        return shm
    return None


def _owner_pid(key: str) -> int | None:
    shm = QSharedMemory(key)
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


def _kill_pid(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, check=False)
    except OSError:
        pass


def ensure_single_instance(scope: str, parent=None, silent: bool = False) -> bool:
    """确保单实例。返回 True=本实例获得独占可继续；False=已有实例（已处理提示）。

    - scope: 'client' / 'server'（区分两端，互不干扰）
    - silent: True 时（如 --tray 自启）冲突直接退出，不弹窗打扰
    """
    key = f"mc_sync_single_v1_{scope}"
    if _acquire(key):
        return True
    old_pid = _owner_pid(key)
    log.info("检测到已有实例在运行（scope=%s, pid=%s），%s",
             scope, old_pid, "静默退出" if silent else "询问用户")
    if silent:
        return False
    if winutil.confirm(
            parent, "程序已在运行",
            "检测到「旧版本」程序正在运行。\n\n"
            "是否先关闭旧版本进程，再启动当前程序？\n\n"
            "（选择「否」将退出；请手动关闭旧版本后再启动）",
            default_yes=False):
        if old_pid:
            _kill_pid(old_pid)
            time.sleep(0.6)
            if _acquire(key):
                return True
        else:
            winutil.warn(parent, "无法自动关闭",
                         "未能定位旧版本进程，请手动关闭旧版本后再启动。")
            return False
    return False
