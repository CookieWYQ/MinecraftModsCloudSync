# -*- coding: utf-8 -*-
"""双主题（暗色/浅色）全局样式（QSS），所有子窗口/对话框统一生效。"""

DARK_QSS = """
* {
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 13px;
    color: #e4e6eb;
    outline: none;
}
QMainWindow, QDialog, QWidget#root, QProgressDialog {
    background-color: #171a21;
}
QTabWidget::pane {
    border: 1px solid #2a2f3a;
    background: #1b1f27;
    border-radius: 6px;
    top: -1px;
}
QTabBar::tab {
    background: transparent;
    padding: 9px 22px;
    margin-right: 2px;
    color: #9aa4b2;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    font-size: 13px;
}
QTabBar::tab:selected {
    background: #232a36;
    color: #ffffff;
    border-bottom: 2px solid #4f8cff;
}
QTabBar::tab:hover:!selected {
    color: #d6dbe3;
}
QGroupBox {
    border: 1px solid #2a2f3a;
    border-radius: 8px;
    margin-top: 12px;
    padding: 12px 10px 10px 10px;
    background: #1e232c;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: #aeb6c2;
    font-size: 12px;
}
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {
    background: #10141a;
    border: 1px solid #2f3644;
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: #2f6bff;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {
    border: 1px solid #4f8cff;
}
QLineEdit:disabled, QSpinBox:disabled {
    color: #6b7280;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QComboBox QAbstractItemView {
    background: #1b1f27;
    border: 1px solid #2f3644;
    selection-background-color: #2f6bff;
}
QPushButton {
    background: #2b3240;
    border: 1px solid #39415a;
    border-radius: 6px;
    padding: 7px 16px;
    color: #e4e6eb;
}
QPushButton:hover {
    background: #35405a;
    border-color: #4f8cff;
}
QPushButton:pressed {
    background: #222838;
}
QPushButton:disabled {
    background: #20242e;
    color: #6b7280;
    border-color: #2a2f3a;
}
QPushButton#primary {
    background: #2f6bff;
    border: 1px solid #4f8cff;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#primary:hover {
    background: #3d7aff;
}
QPushButton#primary:disabled {
    background: #27408c;
    color: #9aa4b2;
}
QPushButton#danger {
    background: #7a1f2b;
    border: 1px solid #a83242;
    color: #ffd9de;
}
QPushButton#danger:hover {
    background: #942a3a;
}
QCheckBox {
    spacing: 6px;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid #4a5468;
    background: #10141a;
}
QCheckBox::indicator:checked {
    background: #2f6bff;
    border-color: #4f8cff;
}
QTableWidget {
    background: #10141a;
    alternate-background-color: #151a22;
    border: 1px solid #2a2f3a;
    border-radius: 6px;
    gridline-color: #232a36;
}
QTableWidget::item {
    padding: 4px 6px;
}
QTableWidget::item:selected {
    background: #2f6bff;
    color: #ffffff;
}
QHeaderView::section {
    background: #1e232c;
    color: #aeb6c2;
    border: none;
    border-bottom: 1px solid #2f3644;
    padding: 6px 8px;
    font-size: 12px;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #39415a;
    border-radius: 5px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover {
    background: #4a5468;
}
QScrollBar::add-line, QScrollBar::sub-line {
    height: 0;
}
QStatusBar {
    background: #14171d;
    color: #9aa4b2;
    border-top: 1px solid #232a36;
}
QLabel#muted {
    color: #9aa4b2;
}
QLabel#title {
    font-size: 20px;
    font-weight: 700;
    color: #ffffff;
}
QLabel#card-title {
    font-size: 14px;
    font-weight: 600;
    color: #e4e6eb;
}
QFrame#card {
    background: #1e232c;
    border: 1px solid #2a2f3a;
    border-radius: 10px;
}
QProgressBar {
    background: #10141a;
    border: 1px solid #2f3644;
    border-radius: 6px;
    text-align: center;
    color: #e4e6eb;
    min-height: 16px;
}
QProgressBar::chunk {
    background: #2f6bff;
    border-radius: 5px;
}
QMessageBox {
    background: #1b1f27;
}
QMenu {
    background: #1b1f27;
    border: 1px solid #2f3644;
    padding: 4px;
}
QMenu::item {
    padding: 6px 18px;
    border-radius: 4px;
}
QMenu::item:selected {
    background: #2f6bff;
}
QToolTip {
    background: #232a36;
    color: #e4e6eb;
    border: 1px solid #39415a;
    padding: 4px 6px;
}
QListWidget {
    background: #10141a;
    border: 1px solid #2a2f3a;
    border-radius: 6px;
    outline: none;
}
QListWidget::item {
    border-radius: 6px;
    margin: 2px 4px;
    background: transparent;
}
QListWidget::item:hover {
    background: #232a36;
}
QListWidget::item:selected {
    background: #2f6bff;
    border: 1px solid #4f8cff;
}
QLabel#list-name {
    color: #e4e6eb;
    font-size: 13px;
    font-weight: 600;
}
QListWidget::item:selected QLabel#list-name {
    color: #ffffff;
}
QLabel#list-status {
    color: #8a93a3;
    font-size: 11px;
}
QListWidget::item:selected QLabel#list-status {
    color: #cfe0ff;
}
QDateEdit, QDateTimeEdit {
    background: #10141a;
    border: 1px solid #2f3644;
    border-radius: 6px;
    padding: 4px 8px;
}
"""

LIGHT_QSS = """
* {
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 13px;
    color: #1f2328;
    outline: none;
}
QMainWindow, QDialog, QWidget#root, QProgressDialog {
    background-color: #f2f4f7;
}
QTabWidget::pane {
    border: 1px solid #d8dde4;
    background: #ffffff;
    border-radius: 6px;
    top: -1px;
}
QTabBar::tab {
    background: transparent;
    padding: 9px 22px;
    margin-right: 2px;
    color: #5f6b7a;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    font-size: 13px;
}
QTabBar::tab:selected {
    background: #ffffff;
    color: #1f2328;
    border-bottom: 2px solid #2f6bff;
    font-weight: 600;
}
QTabBar::tab:hover:!selected {
    color: #1f2328;
}
QGroupBox {
    border: 1px solid #d8dde4;
    border-radius: 8px;
    margin-top: 12px;
    padding: 12px 10px 10px 10px;
    background: #ffffff;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: #5f6b7a;
    font-size: 12px;
}
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {
    background: #ffffff;
    border: 1px solid #c9d0d9;
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: #d5e2ff;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {
    border: 1px solid #2f6bff;
}
QLineEdit:disabled, QSpinBox:disabled {
    color: #a3abb5;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QComboBox QAbstractItemView {
    background: #ffffff;
    border: 1px solid #c9d0d9;
    selection-background-color: #d5e2ff;
    color: #1f2328;
}
QPushButton {
    background: #eef1f5;
    border: 1px solid #c9d0d9;
    border-radius: 6px;
    padding: 7px 16px;
    color: #1f2328;
}
QPushButton:hover {
    background: #e2e7ee;
    border-color: #2f6bff;
}
QPushButton:pressed {
    background: #d5dbe3;
}
QPushButton:disabled {
    background: #f0f2f5;
    color: #a3abb5;
    border-color: #e0e4e9;
}
QPushButton#primary {
    background: #2f6bff;
    border: 1px solid #4f8cff;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#primary:hover {
    background: #3d7aff;
}
QPushButton#primary:disabled {
    background: #a9c1f5;
    color: #f2f6ff;
}
QPushButton#danger {
    background: #fdecee;
    border: 1px solid #f0b3bc;
    color: #b02a3c;
}
QPushButton#danger:hover {
    background: #f9d7dc;
}
QCheckBox {
    spacing: 6px;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid #b9c2ce;
    background: #ffffff;
}
QCheckBox::indicator:checked {
    background: #2f6bff;
    border-color: #2f6bff;
}
QTableWidget {
    background: #ffffff;
    alternate-background-color: #f7f9fb;
    border: 1px solid #d8dde4;
    border-radius: 6px;
    gridline-color: #e6eaf0;
}
QTableWidget::item {
    padding: 4px 6px;
}
QTableWidget::item:selected {
    background: #d5e2ff;
    color: #1f2328;
}
QHeaderView::section {
    background: #eef1f5;
    color: #5f6b7a;
    border: none;
    border-bottom: 1px solid #d8dde4;
    padding: 6px 8px;
    font-size: 12px;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #c4ccd6;
    border-radius: 5px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover {
    background: #aab4c1;
}
QScrollBar::add-line, QScrollBar::sub-line {
    height: 0;
}
QStatusBar {
    background: #ffffff;
    color: #5f6b7a;
    border-top: 1px solid #e0e4e9;
}
QLabel#muted {
    color: #6b7686;
}
QLabel#title {
    font-size: 20px;
    font-weight: 700;
    color: #1f2328;
}
QLabel#card-title {
    font-size: 14px;
    font-weight: 600;
    color: #1f2328;
}
QFrame#card {
    background: #ffffff;
    border: 1px solid #d8dde4;
    border-radius: 10px;
}
QProgressBar {
    background: #ffffff;
    border: 1px solid #c9d0d9;
    border-radius: 6px;
    text-align: center;
    color: #1f2328;
    min-height: 16px;
}
QProgressBar::chunk {
    background: #2f6bff;
    border-radius: 5px;
}
QMessageBox {
    background: #ffffff;
}
QMenu {
    background: #ffffff;
    border: 1px solid #c9d0d9;
    padding: 4px;
}
QMenu::item {
    padding: 6px 18px;
    border-radius: 4px;
}
QMenu::item:selected {
    background: #d5e2ff;
    color: #1f2328;
}
QToolTip {
    background: #ffffff;
    color: #1f2328;
    border: 1px solid #c9d0d9;
    padding: 4px 6px;
}
QListWidget {
    background: #ffffff;
    border: 1px solid #d8dde4;
    border-radius: 6px;
    outline: none;
}
QListWidget::item {
    border-radius: 6px;
    margin: 2px 4px;
    background: transparent;
}
QListWidget::item:hover {
    background: #eef2f7;
}
QListWidget::item:selected {
    background: #d5e2ff;
    border: 1px solid #2f6bff;
}
QLabel#list-name {
    color: #1f2328;
    font-size: 13px;
    font-weight: 600;
}
QListWidget::item:selected QLabel#list-name {
    color: #123c9e;
}
QLabel#list-status {
    color: #6b7686;
    font-size: 11px;
}
QListWidget::item:selected QLabel#list-status {
    color: #2f5fbf;
}
QDateEdit, QDateTimeEdit {
    background: #ffffff;
    border: 1px solid #c9d0d9;
    border-radius: 6px;
    padding: 4px 8px;
}
"""


def apply_style(app, dark: bool = True) -> None:
    app.setStyleSheet(DARK_QSS if dark else LIGHT_QSS)


def theme_is_dark(theme: str) -> bool:
    """主题是否使用深色：system 跟随 Windows 系统设置。"""
    if theme == "light":
        return False
    if theme == "dark":
        return True
    # system
    try:
        from .winutil import system_dark_mode

        return system_dark_mode()
    except Exception:
        return True
