# -*- coding: utf-8 -*-
"""统一日志：写入数据目录 logs\\，按天滚动，无控制台输出。

支持按「服务器上下文」拆分日志文件：调用 set_log_context(key) 后，
所有 logger 写入 logs\\{scope}\\{key}_{date}.log（key 为空时写 {date}.log），
确保服务端/客户端的每个服务器都有独立日志。

scope 隔离：客户端与服务端写入不同子目录（logs\\client、logs\\server），
避免客户端日志管理里混入服务端的 SFTP 连接信息。
"""
import logging
import logging.handlers
from datetime import datetime

from .constants import logs_dir

_registry: dict = {}
_current_key: str = ""
_scope: str = ""


class _ClientSftpFilter(logging.Filter):
    """客户端日志不记录任何 SFTP 服务器连接细节（连接信息仅由服务端日志保留）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if _scope == "client" and record.name == "sftp":
            return False
        return True


def current_log_key() -> str:
    return _current_key


def set_app_scope(scope: str = "") -> None:
    """设置日志所属应用（client / server），用于日志目录隔离。"""
    global _scope
    scope = (scope or "").strip()
    if scope == _scope:
        return
    _scope = scope
    _reset_handlers()


def active_logs_dir():
    """当前应用自己的日志目录（client/server 隔离）。"""
    d = logs_dir()
    return d / _scope if _scope else d


def available_log_days() -> list[tuple[str, str]]:
    """返回当前上下文可用的日志 [(日期 YYYYMMDD, 该日首个日志文件)]，按日期倒序（最新在前）。

    同一日期的轮转备份（.log.1、.log.2…）合并为该日期一项。
    """
    base = active_logs_dir()
    key = _current_key
    days: dict[str, str] = {}
    try:
        for p in base.glob("*.log*"):
            stem = p.name.split(".log", 1)[0]
            date = stem[len(f"{key}_"):] if key else stem
            if len(date) == 8 and date.isdigit():
                days.setdefault(date, str(p))
    except OSError:
        pass
    return sorted(days.items(), reverse=True)


def log_files_for_date(date: str) -> list[str]:
    """返回指定日期（YYYYMMDD）在当前上下文下按时间顺序排列的日志文件路径。

    轮转备份的编号越大越旧（.log.2 早于 .log.1 早于 .log），读取时按 旧→新 排序。
    """
    base = active_logs_dir()
    prefix = f"{_current_key}_{date}" if _current_key else date
    matches = []
    try:
        for p in base.glob("*.log*"):
            if p.name.split(".log", 1)[0] == prefix:
                matches.append(p)
    except OSError:
        pass

    def _backup_index(p):
        try:
            return int(p.name.rsplit(".", 1)[-1])
        except ValueError:
            return 0

    # (是否主文件, -备份编号)：主文件(1,0)排最后；备份按编号倒序（旧在前）
    matches.sort(key=lambda p: (p.suffix == ".log", -_backup_index(p)))
    return [str(p) for p in matches]


def current_log_path():
    """当前上下文对应的日志文件路径。"""
    day = datetime.now().strftime("%Y%m%d")
    base = active_logs_dir()
    base.mkdir(parents=True, exist_ok=True)
    if _current_key:
        return base / f"{_current_key}_{day}.log"
    return base / f"{day}.log"


def set_log_context(key: str = "") -> None:
    """切换日志上下文（如服务器 id），此后所有日志写入对应服务器的独立文件。"""
    global _current_key
    key = key or ""
    if key == _current_key:
        return
    _current_key = key
    _reset_handlers()


def _make_handler():
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.handlers.RotatingFileHandler(
        current_log_path(),
        maxBytes=2_000_000,
        backupCount=10,
        encoding="utf-8",
    )
    if _scope == "client":
        fh.addFilter(_ClientSftpFilter())
    fh.setFormatter(fmt)
    return fh


def _reset_handlers() -> None:
    for logger in list(_registry.values()):
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        logger.addHandler(_make_handler())


def get_logger(name: str = "app") -> logging.Logger:
    if name in _registry:
        return _registry[name]
    logger = logging.getLogger(name)
    if logger.handlers:
        _registry[name] = logger
        return logger
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(_make_handler())
    _registry[name] = logger
    return logger
