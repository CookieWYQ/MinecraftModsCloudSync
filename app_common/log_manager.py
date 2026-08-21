# -*- coding: utf-8 -*-
"""日志管理器：按时间段/关键词查看并导出日志（配置与日志已隐藏保存）。"""
import re
from datetime import datetime, timedelta

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtWidgets import (
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from . import winutil
from .logger import active_logs_dir, get_logger

log = get_logger("log_manager")

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def collect_log_lines(start: datetime, end: datetime, keyword: str = "") -> list[str]:
    """扫描当前应用的日志目录（含轮转备份），按时间段与关键词过滤。

    客户端 / 服务端日志已按子目录隔离（logs\\client、logs\\server），
    这里只读取当前应用自己的日志，不会混入对方（含 SFTP 连接细节）的记录。
    """
    keyword = keyword.strip().lower()
    lines: list[str] = []
    for path in sorted(active_logs_dir().glob("*.log*")):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for raw in f:
                    line = raw.rstrip("\n")
                    m = _TS_RE.match(line)
                    if not m:
                        continue
                    try:
                        ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
                    if not (start <= ts <= end):
                        continue
                    if keyword and keyword not in line.lower():
                        continue
                    lines.append(line)
        except OSError as exc:
            log.warning("读取日志文件失败 %s: %s", path, exc)
    return lines


class LogManagerDialog(QDialog):
    """查看与导出日志（可选择时间范围与关键词）。"""

    def __init__(self, parent=None, title: str = "日志管理"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(720, 460)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        top = QHBoxLayout()

        now = QDateTime.currentDateTime()
        self.ed_from = QDateTimeEdit(now.date().startOfDay())
        self.ed_from.setCalendarPopup(True)
        self.ed_to = QDateTimeEdit(now)
        self.ed_to.setCalendarPopup(True)
        self.ed_keyword = QLineEdit()
        self.ed_keyword.setPlaceholderText("关键词过滤（可选）")
        self.ed_keyword.setFixedWidth(180)

        top.addWidget(QLabel("从"))
        top.addWidget(self.ed_from)
        top.addWidget(QLabel("到"))
        top.addWidget(self.ed_to)
        top.addWidget(self.ed_keyword)
        top.addStretch(1)
        btn_preview = QPushButton("刷新预览")
        btn_preview.clicked.connect(self._preview)
        top.addWidget(btn_preview)
        layout.addLayout(top)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        layout.addWidget(self.preview, 1)

        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("muted")
        layout.addWidget(self.lbl_count)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_export = QPushButton("导出所选日志…")
        btn_export.setObjectName("primary")
        btn_export.clicked.connect(self._export)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        btn_row.addWidget(btn_export)
        btn_row.addWidget(buttons)
        layout.addLayout(btn_row)

        self._preview()

    def _range(self) -> tuple[datetime, datetime]:
        return (self.ed_from.dateTime().toPython(),
                self.ed_to.dateTime().toPython())

    def _preview(self):
        start, end = self._range()
        lines = collect_log_lines(start, end, self.ed_keyword.text())
        self.preview.setPlainText("\n".join(lines))
        self.lbl_count.setText(f"共 {len(lines)} 条日志（时间范围：{start:%Y-%m-%d %H:%M:%S} ~ {end:%Y-%m-%d %H:%M:%S}）")

    def _export(self):
        start, end = self._range()
        lines = collect_log_lines(start, end, self.ed_keyword.text())
        if not lines:
            winutil.warn(self, "提示", "当前时间段内没有匹配的日志。")
            return
        default_name = f"客户端日志_{datetime.now():%Y%m%d_%H%M}.log"
        path, _ = QFileDialog.getSaveFileName(self, "导出日志", default_name, "日志文件 (*.log *.txt)")
        if not path:
            return
        if not (path.lower().endswith(".log") or path.lower().endswith(".txt")):
            path += ".log"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError as exc:
            winutil.error(self, "导出失败", f"写入文件失败：\n{exc}")
            return
        winutil.info(self, "导出完成",
                     f"已导出 {len(lines)} 条日志到：\n{path}\n\n时间范围：{start:%Y-%m-%d %H:%M:%S} ~ {end:%Y-%m-%d %H:%M:%S}")
