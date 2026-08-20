# -*- coding: utf-8 -*-
"""Windows 文件/目录隐藏（无第三方依赖，轻量）。"""

FILE_ATTRIBUTE_HIDDEN = 0x2


def set_hidden(path) -> None:
    """为路径设置隐藏属性（目录隐藏后，资源管理器中其内容默认不可见）。"""
    try:
        import ctypes

        ctypes.windll.kernel32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_HIDDEN)
    except Exception:
        pass
