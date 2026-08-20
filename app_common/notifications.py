# -*- coding: utf-8 -*-
"""Windows 通知：优先 winotify 原生 Toast，失败时回退到系统托盘气泡。"""
from .logger import get_logger

log = get_logger("notify")


def notify(title: str, message: str, tray=None) -> None:
    """发送 Windows 消息弹窗提示。tray 为可选的 QSystemTrayIcon。"""
    try:
        from winotify import Notification, audio

        toast = Notification(app_id="MinecraftModsCloudSync", title=title, msg=message)
        toast.set_audio(audio.Default, loop=False)
        toast.show()
        log.info("已发送系统通知: %s - %s", title, message)
        return
    except Exception as exc:
        log.warning("winotify 通知失败，回退托盘气泡: %s", exc)

    if tray is not None:
        try:
            tray.showMessage(title, message, tray.MessageIcon.Information, 5000)
            return
        except Exception as exc:
            log.warning("托盘气泡通知失败: %s", exc)
    log.warning("所有通知方式均不可用，仅记录日志: %s - %s", title, message)
