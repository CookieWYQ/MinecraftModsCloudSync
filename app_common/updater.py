# -*- coding: utf-8 -*-
"""软件本体自动更新：从 GitHub Release 检测最新版本、下载 Windows 安装程序并启动安装。

- 检测：GitHub API `releases/latest`（网络操作必须在线程中调用）
- 下载：Release 资产中的安装程序（MinecraftModsCloudSync_Setup_<版本>.exe）
- 安装：启动下载的安装程序（Inno Setup 自行请求管理员权限，用户在向导中完成安装）

「已提示过的版本」记录在本地 update_state.json，避免每次启动静默检查时反复打扰。
"""
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .constants import APP_NAME, APP_VERSION, appdata_dir
from .logger import get_logger

log = get_logger("updater")

GITHUB_REPO = "CookieWYQ/MinecraftModsCloudSync"
LATEST_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
SETUP_PREFIX = "MinecraftModsCloudSync_Setup_"
_CHUNK = 64 * 1024

# 常见 GitHub 下载加速镜像（按顺序尝试；仅用于安装包下载，版本查询仍走官方 API）。
# 直连失败时自动降级到镜像，任一通道成功后即完成下载。
DOWNLOAD_MIRRORS = (
    "https://ghproxy.net/",
    "https://gh-proxy.com/",
    "https://ghfast.top/",
)

# 国内更新源（Gitee 发行版）：GitHub 不可达时自动切换。
# 需在 Gitee 创建同名仓库并发布发行版（附件名同样为 MinecraftModsCloudSync_Setup_*.exe）。
GITEE_REPO = "CookieWYQ/MinecraftModsCloudSync"
GITEE_LATEST_API_URL = f"https://gitee.com/api/v5/repos/{GITEE_REPO}/releases/latest"
# 每台服务器每轮自动检查前两次请求的最小间隔（秒），避免高频轮询 Gitee/GitHub API 触发限流
UPDATE_SOURCE_ORDER = ("github", "gitee")


def release_page_url() -> str:
    """最新版本 Release 页面地址（供手动下载引导）。"""
    return f"https://github.com/{GITHUB_REPO}/releases/latest"


def release_page_url_gitee() -> str:
    """Gitee 发行版页面地址（供手动下载引导）。"""
    return f"https://gitee.com/{GITEE_REPO}/releases"


class ReleaseInfo:
    """最新 Release 的安装包信息（source 标注来源：github / gitee）。"""

    def __init__(self, tag: str, name: str, published_at: str, body: str,
                 setup_name: str, setup_url: str, setup_size: int,
                 source: str = "github"):
        self.tag = tag            # 如 v1.0.0
        self.name = name          # Release 标题
        self.published_at = published_at
        self.body = body or ""    # 更新日志（Release 描述文本）
        self.setup_name = setup_name
        self.setup_url = setup_url
        self.setup_size = setup_size  # 字节
        self.source = source

    @property
    def setup_size_mb(self) -> float:
        return self.setup_size / 1024 / 1024 if self.setup_size else 0.0

    @property
    def source_label(self) -> str:
        return {"github": "GitHub", "gitee": "Gitee"}.get(self.source, self.source)


def parse_version(text: str) -> tuple | None:
    """'v1.0.0' → (1, 0, 0)；无法解析出数字版本返回 None。"""
    m = re.search(r"v?(\d+(?:\.\d+)*)", text or "")
    if not m:
        return None
    return tuple(int(x) for x in m.group(1).split("."))


def compare_versions(a: tuple, b: tuple) -> int:
    """比较两个版本元组，a > b 返回 1，相等返回 0，a < b 返回 -1。"""
    if a == b:
        return 0
    return 1 if a > b else -1


def fetch_latest(timeout: int = 15) -> ReleaseInfo | None:
    """依次尝试 GitHub → Gitee，返回第一个可用源的最新版本信息。

    - 某源可用但没有安装包资产 → 返回 None（说明源已发布但缺附件）
    - 全部源都不可达 → 抛出最后一个错误（调用方可提示用户）
    """
    last_err: Exception | None = None
    for source in UPDATE_SOURCE_ORDER:
        try:
            info = _fetch_github(timeout) if source == "github" \
                else _fetch_gitee(timeout)
            if info is not None and info.setup_url:
                return info
            if info is None:
                log.warning("更新源 %s 可用但未找到安装包资产", source)
                last_err = RuntimeError(f"更新源 {source} 未发布安装包")
        except Exception as exc:
            log.warning("更新源 %s 不可用: %s", source, exc)
            last_err = exc
    if last_err:
        raise last_err
    return None


def _fetch_github(timeout: int = 15) -> ReleaseInfo | None:
    """查询 GitHub 最新 Release（网络请求）。找不到安装程序资产时返回 None。"""
    req = urllib.request.Request(
        LATEST_API_URL,
        headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}",
                 "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    tag = data.get("tag_name", "")
    assets = data.get("assets") or []
    setup = next((a for a in assets
                  if a.get("name", "").startswith(SETUP_PREFIX)
                  and a.get("name", "").lower().endswith(".exe")), None)
    if not setup:
        return None
    return ReleaseInfo(
        tag=tag,
        name=data.get("name", ""),
        published_at=str(data.get("published_at", "")).replace("T", " ")[:16],
        body=data.get("body", ""),
        setup_name=setup.get("name", ""),
        setup_url=setup.get("browser_download_url", ""),
        setup_size=int(setup.get("size", 0) or 0),
        source="github",
    )


def _fetch_gitee(timeout: int = 15) -> ReleaseInfo | None:
    """查询 Gitee 最新发行版（网络请求）。找不到安装程序附件时返回 None。"""
    req = urllib.request.Request(
        GITEE_LATEST_API_URL,
        headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}",
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    tag = data.get("tag_name", "")
    # Gitee 发行版附件字段兼容 assets / attach_files 两种命名
    assets = data.get("assets") or data.get("attach_files") or []
    setup = next((a for a in assets
                  if a.get("name", "").startswith(SETUP_PREFIX)
                  and a.get("name", "").lower().endswith(".exe")), None)
    if not setup:
        return None
    url = (setup.get("browser_download_url")
           or setup.get("url") or "")
    return ReleaseInfo(
        tag=tag,
        name=data.get("name", ""),
        published_at=str(data.get("created_at", "")).replace("T", " ")[:16],
        body=data.get("body", ""),
        setup_name=setup.get("name", ""),
        setup_url=url,
        setup_size=int(setup.get("size", 0) or 0),
        source="gitee",
    )


def fetch_releases(timeout: int = 15) -> list[dict]:
    """获取版本历史列表 [{tag, published_at}]（按时间倒序）。多源依次尝试，全部失败返回空列表。"""
    for source in UPDATE_SOURCE_ORDER:
        try:
            items = (_fetch_releases_github(timeout) if source == "github"
                     else _fetch_releases_gitee(timeout))
            if items:
                return items
        except Exception as exc:
            log.warning("版本列表源 %s 不可用: %s", source, exc)
    return []


def _fetch_releases_github(timeout: int = 15) -> list[dict]:
    url = (f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=30")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}",
                 "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return [{
        "tag": r.get("tag_name", ""),
        "published_at": str(r.get("published_at", "")).replace("T", " ")[:16],
        "body": r.get("body", ""),
    } for r in data if isinstance(r, dict)]


def _fetch_releases_gitee(timeout: int = 15) -> list[dict]:
    url = f"https://gitee.com/api/v5/repos/{GITEE_REPO}/releases?per_page=30"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}",
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return [{
        "tag": r.get("tag_name", ""),
        "published_at": str(r.get("created_at", "")).replace("T", " ")[:16],
        "body": r.get("body", ""),
    } for r in data if isinstance(r, dict)]


def download_setup(url: str, dest: str, progress_cb=None,
                   cancelled_cb=None, timeout: int = 30) -> str:
    """下载安装程序到 dest（返回 dest）。progress_cb(已下载, 总字节, 消息)。

    - Gitee 直链国内可直接下载，不走加速镜像；
    - GitHub 直链依次尝试：直连 → 加速镜像列表；某一通道成功后即返回。
    """
    if "gitee.com" in url:
        attempts = [url]
    else:
        attempts = [url] + [f"{m}{url}" for m in DOWNLOAD_MIRRORS]
    last_err: Exception | None = None
    for i, target in enumerate(attempts):
        if cancelled_cb and cancelled_cb():
            raise RuntimeError("已取消下载")
        try:
            _download_one(target, dest, progress_cb, cancelled_cb, timeout)
            return dest
        except Exception as exc:  # 单个通道失败 → 尝试下一个
            last_err = exc
            if progress_cb:
                mb = i + 1
                total = len(attempts)
                progress_cb(0, 0, f"通道 {mb}/{total} 不可用，切换下一通道…")
    raise last_err if last_err else RuntimeError("下载失败")


def _download_one(url: str, dest: str, progress_cb=None,
                  cancelled_cb=None, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        if progress_cb:
            progress_cb(0, total, "开始下载…")
        downloaded = 0
        with open(dest, "wb") as f:
            while True:
                if cancelled_cb and cancelled_cb():
                    raise RuntimeError("已取消下载")
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb:
                    mb = downloaded / 1024 / 1024
                    progress_cb(downloaded, total, f"已下载 {mb:.1f} MB")
    if progress_cb:
        progress_cb(downloaded, total, "下载完成")
    return dest


def launch_installer(path: str) -> None:
    """启动 Windows 安装程序（Inno Setup 自行弹出管理员授权，用户继续完成安装）。"""
    if os.name == "nt":
        os.startfile(path)  # noqa: S606
    else:
        raise RuntimeError("当前仅支持 Windows 安装程序")


def update_dir() -> Path:
    d = appdata_dir() / "updates"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- 本地状态：上次提示过的版本 + 上次检查时间 ----------
def _state_path() -> Path:
    return appdata_dir() / "update_state.json"


def _read_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_state(data: dict) -> None:
    try:
        with open(_state_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError as exc:
        log.warning("保存更新状态失败: %s", exc)


def last_prompted_version() -> str:
    return str(_read_state().get("last_prompted", ""))


def set_last_prompted_version(version: str) -> None:
    data = _read_state()
    data["last_prompted"] = version
    _write_state(data)


def last_check_time() -> str:
    """上次检查软件更新的时间（手动 / 自动检查都会更新）。"""
    return str(_read_state().get("last_check", ""))


def set_last_check_time(value: str) -> None:
    data = _read_state()
    data["last_check"] = value
    _write_state(data)


# ---------- 界面辅助（客户端 / 服务端共用） ----------
def show_update_result(parent, latest, releases: list[dict],
                       checked_at: str, has_update: bool) -> bool:
    """展示检查更新结果：当前版本、上次检查时间、更新日志、版本历史列表。

    - has_update=True：展示新版本信息并询问是否下载，返回 True=下载安装。
    - has_update=False：提示已是最新版本，仅查看（返回 False）。
    """
    from PySide6.QtWidgets import (
        QDialog,
        QDialogButtonBox,
        QLabel,
        QListWidget,
        QPlainTextEdit,
        QVBoxLayout,
    )
    dlg = QDialog(parent)
    dlg.setWindowTitle("发现软件新版本" if has_update else "检查更新结果")
    dlg.setMinimumSize(600, 560)
    layout = QVBoxLayout(dlg)

    if has_update and latest is not None:
        tip = QLabel(
            f"发现新版本：\n\n"
            f"来源：{latest.source_label}\n"
            f"当前版本：{APP_VERSION}\n"
            f"最新版本：{latest.tag}\n"
            f"发布时间：{latest.published_at}\n"
            f"安装包大小：{latest.setup_size_mb:.1f} MB\n"
            f"上次检查时间：{checked_at or '—'}\n\n"
            f"将后台下载 Windows 安装程序（不中断当前操作），下载完成后提示安装。")
    else:
        tip = QLabel(
            f"当前已是最新版本（{APP_VERSION}）。\n"
            f"上次检查时间：{checked_at or '—'}")
    tip.setWordWrap(True)
    layout.addWidget(tip)

    if has_update and latest is not None and latest.body.strip():
        layout.addWidget(QLabel("更新日志："))
        txt = QPlainTextEdit()
        txt.setReadOnly(True)
        txt.setPlainText(latest.body.strip())
        txt.setMaximumHeight(200)
        layout.addWidget(txt)

    layout.addWidget(QLabel("版本历史："))
    lst = QListWidget()
    cur_tag = f"v{APP_VERSION}" if not APP_VERSION.startswith("v") else APP_VERSION
    for i, r in enumerate(releases):
        tag = r.get("tag", "")
        mark = "  （当前版本）" if tag == cur_tag else ("  （最新）" if i == 0 else "")
        lst.addItem(f"{tag}  ·  {r.get('published_at', '')}{mark}")
    if not releases:
        lst.addItem("（无法获取版本历史列表）")
    lst.setMaximumHeight(200)
    layout.addWidget(lst, 1)

    buttons = QDialogButtonBox()
    if has_update and latest is not None:
        btn_download = buttons.addButton("下载并安装", QDialogButtonBox.AcceptRole)
        buttons.addButton("稍后", QDialogButtonBox.RejectRole)
    else:
        btn_ok = buttons.addButton("确定", QDialogButtonBox.AcceptRole)
        btn_ok.setDefault(True)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)
    return dlg.exec() == QDialog.Accepted


def show_check_failed(parent, error: str) -> bool:
    """检查更新失败提示；返回 True 表示用户选择重试。"""
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QMessageBox
    box = QMessageBox(parent)
    box.setWindowTitle("检查更新失败")
    box.setIcon(QMessageBox.Warning)
    box.setText("无法连接 GitHub / Gitee 获取最新版本信息。\n\n"
                "可能原因：网络不可用或平台访问受限。\n"
                "您可以稍后重试，或打开平台页面手动下载安装包。")
    box.setInformativeText(f"错误详情：{error}")
    retry = box.addButton("重试", QMessageBox.AcceptRole)
    gh = box.addButton("打开 GitHub Release", QMessageBox.ActionRole)
    ge = box.addButton("打开 Gitee 发行版", QMessageBox.ActionRole)
    box.addButton("关闭", QMessageBox.RejectRole)
    box.exec()
    clicked = box.clickedButton()
    if clicked is gh:
        QDesktopServices.openUrl(QUrl(release_page_url()))
        return False
    if clicked is ge:
        QDesktopServices.openUrl(QUrl(release_page_url_gitee()))
        return False
    return clicked is retry


# ---------- 后台线程（避免网络/大文件下载阻塞界面） ----------
class UpdateCheckThread(QThread):
    """后台查询最新版本 + 版本历史列表（并记录本次检查时间）。"""

    done = Signal(bool, object, str)  # (成功, {"latest","releases","checked_at"}|None, 错误信息)

    def __init__(self, parent=None):
        super().__init__(parent)

    def run(self):
        try:
            latest = fetch_latest()
            releases = fetch_releases()
            checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            set_last_check_time(checked_at)
            self.done.emit(True, {
                "latest": latest,
                "releases": releases,
                "checked_at": checked_at,
            }, "")
        except Exception as exc:
            log.warning("查询最新版本失败: %s", exc)
            self.done.emit(False, None, str(exc))


class DownloadThread(QThread):
    """后台下载安装程序（支持取消）。"""

    progress = Signal(int, int, str)  # (已下载, 总字节, 消息)
    done = Signal(bool, str, str)     # (成功, 本地路径, 错误信息)

    def __init__(self, url: str, dest: str, parent=None):
        super().__init__(parent)
        self._url = url
        self._dest = dest
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            download_setup(self._url, self._dest,
                           progress_cb=self._emit_progress,
                           cancelled_cb=lambda: self._cancelled)
            self.done.emit(True, self._dest, "")
        except Exception as exc:
            log.warning("下载安装程序失败: %s", exc)
            self.done.emit(False, "", str(exc))

    def _emit_progress(self, current: int, total: int, message: str):
        self.progress.emit(current, total, message)
