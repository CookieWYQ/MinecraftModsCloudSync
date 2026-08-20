# -*- coding: utf-8 -*-
"""程序图标（完全内嵌，单文件运行不依赖任何外部素材文件）。

图标以 base64 内嵌于 icons_data.py（构建时由 tools/gen_icons_data.py 生成），
运行时解码为 QIcon，并保留 .ico 内的多尺寸帧（窗口、任务栏、托盘均清晰）。
"""
import base64

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QIcon, QImageReader, QPixmap

from .icons_data import CLIENT_ICO_B64, SERVER_ICO_B64


def _decode_b64(text: str) -> bytes:
    return base64.b64decode("".join(text.split()))


def _icon_from_b64(b64: str) -> QIcon:
    icon = QIcon()
    data = _decode_b64(b64)
    buffer = QBuffer()
    buffer.setData(data)
    buffer.open(QIODevice.ReadOnly)
    reader = QImageReader(buffer)
    reader.setAutoTransform(False)
    while True:
        image = reader.read()
        if image.isNull():
            break
        icon.addPixmap(QPixmap.fromImage(image))
        if not reader.jumpToNextImage():
            break
    buffer.close()
    return icon


def get_app_icon(role: str) -> QIcon:
    """获取程序图标。role: 'client' | 'server'。"""
    b64 = CLIENT_ICO_B64 if role == "client" else SERVER_ICO_B64
    icon = _icon_from_b64(b64)
    if icon.isNull():
        return QIcon()
    return icon
