# -*- coding: utf-8 -*-
"""日志导出回归测试：导出时异常 traceback 不能被丢掉。

背景：线上日志里出现过只有一行
    2026-11-21 22:07:54 [ERROR] server.main | 未捕获异常导致程序退出
而紧随其后的 Traceback / RuntimeError 行在「导出日志」时被过滤掉，用户导出的
日志文件里看不到崩溃原因。本测试确保 collect_log_lines() 把无时间戳的续行
（traceback）当作上一条日志的一部分一起导出，关键词过滤也覆盖整段记录。

用法（仓库根目录）：
    python tests/test_log_export.py
"""
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app_common import log_manager

LOG_TEXT = """\
2026-11-21 22:07:54 [ERROR] server.main | 未捕获异常导致程序退出
Traceback (most recent call last):
  File "server_app\\ui\\main_window.py", line 228, in <lambda>
  File "server_app\\ui\\main_window.py", line 528, in _check_update
RuntimeError: Internal C++ object (UpdateCheckThread) already deleted.
2026-11-21 22:08:44 [INFO] sftp | SFTP 连接成功 sfe4-connect.simpfun.cn:2010
2026-11-21 23:59:59 [ERROR] other | OUTSIDE_MARKER 不在范围内
Traceback (most recent call last):
  File "x.py", line 1, in <module>
ValueError: OUTSIDE_MARKER
"""


def _collect(start, end, keyword=""):
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "server_20261121.log").write_text(LOG_TEXT, encoding="utf-8")
        old = log_manager.active_logs_dir
        log_manager.active_logs_dir = lambda: Path(tmp)
        try:
            return log_manager.collect_log_lines(start, end, keyword)
        finally:
            log_manager.active_logs_dir = old


def check_traceback_kept():
    start = datetime(2026, 11, 21, 0, 0, 0)
    end = datetime(2026, 11, 21, 23, 0, 0)
    lines = _collect(start, end)
    joined = "\n".join(lines)
    assert "未捕获异常导致程序退出" in joined, lines
    assert "Traceback (most recent call last):" in joined, joined
    assert "RuntimeError: Internal C++ object (UpdateCheckThread) already deleted." in joined, joined
    assert "SFTP 连接成功" in joined, joined
    # 时间范围外的记录（含其 traceback）整体不导出
    assert "OUTSIDE_MARKER" not in joined, joined
    # traceback 续行必须紧跟在报错行后面
    idx = lines.index("2026-11-21 22:07:54 [ERROR] server.main | 未捕获异常导致程序退出")
    assert lines[idx + 1].startswith("Traceback"), lines[idx:idx + 2]
    print("正常导出：traceback 续行完整保留，范围外记录不泄漏")


def check_keyword_matches_continuation():
    start = datetime(2026, 11, 21, 0, 0, 0)
    end = datetime(2026, 11, 21, 23, 0, 0)
    lines = _collect(start, end, "runtimeerror")
    joined = "\n".join(lines)
    assert "RuntimeError: Internal C++" in joined, joined
    # 关键词命中续行时，整条记录（含开头报错行与 traceback）一起输出
    assert "未捕获异常导致程序退出" in joined, joined
    assert "Traceback" in joined, joined
    assert "SFTP" not in joined, joined
    print("关键词过滤：命中 traceback 时整条记录一起导出")


def check_out_of_range_not_leaked():
    start = datetime(2026, 11, 21, 0, 0, 0)
    end = datetime(2026, 11, 21, 12, 0, 0)
    lines = _collect(start, end)
    joined = "\n".join(lines)
    assert "OUTSIDE_MARKER" not in joined, joined
    assert "未捕获异常导致程序退出" not in joined, joined
    print("时间范围：范围外记录的续行不会被错误拼接进来")


def main() -> None:
    check_traceback_kept()
    check_keyword_matches_continuation()
    check_out_of_range_not_leaked()
    print("\nLOG EXPORT OK")


if __name__ == "__main__":
    main()
