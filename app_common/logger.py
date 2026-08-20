# -*- coding: utf-8 -*-
"""统一日志：写入 %APPDATA%\\MinecraftModsCloudSync\\logs\\，按天滚动，无控制台输出。"""
import logging
import logging.handlers
from datetime import datetime

from .constants import logs_dir

_registry: dict = {}


def get_logger(name: str = "app") -> logging.Logger:
    if name in _registry:
        return _registry[name]
    logger = logging.getLogger(name)
    if logger.handlers:
        _registry[name] = logger
        return logger
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    day = datetime.now().strftime("%Y%m%d")
    fh = logging.handlers.RotatingFileHandler(
        logs_dir() / f"{day}.log",
        maxBytes=2_000_000,
        backupCount=10,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    _registry[name] = logger
    return logger
