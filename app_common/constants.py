"""全局常量与路径定义"""
import os
import sys
from pathlib import Path

from .hide import set_hidden

APP_NAME = "MinecraftModsCloudSync"
APP_DISPLAY_NAME = "Minecraft 模组云端同步"
APP_VERSION = "1.0.0"

# 客户端加密密钥派生盐值（构建时可通过环境变量 MC_SYNC_SECRET 覆盖密钥本体）
CRYPTO_SALT = "mc-mods-cloud-sync-7f3a9c2e5b1d"

# SFTP 默认远程目录
DEFAULT_REMOTE_TODO_DIR = "todo"          # 待办任务目录
DEFAULT_REMOTE_FILES_DIR = "client_files"  # 客户端文件仓库目录
MANIFEST_FILENAME = "manifest.json"

# 同步分类（对应 Minecraft 游戏目录子文件夹）
SYNC_CATEGORIES = ("mods", "resourcepacks", "config")

# 默认“仅限客户端模组”过滤关键词（小写子串匹配，可在界面修改）
DEFAULT_CLIENT_ONLY_KEYWORDS = [
    "ambientsounds", "betterfps", "damageindicator", "xaeros", "minimap",
    "modmenu", "rei", "inventoryprofiles", "sodium", "iris", "ferritecore",
    "zoom", "hud", "keybinds", "languagereload",
]

# 同步时默认排除的文件模式（fnmatch，小写）
DEFAULT_SYNC_EXCLUDE_PATTERNS = [
    "*.disabled", "*.bak", "*.old", "*backup*", "*备份*", "*.tmp",
    "crash-*", "*.log",
]


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def app_dir() -> Path:
    """程序运行目录（打包后为 exe 所在目录）"""
    if is_frozen():
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def appdata_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    # 隐藏整个数据目录（配置与日志都在其中），避免用户误删/误改
    set_hidden(d)
    return d


def logs_dir() -> Path:
    d = appdata_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_dir() -> Path:
    d = appdata_dir() / "config"
    d.mkdir(parents=True, exist_ok=True)
    return d


SERVER_CONFIG_PATH = config_dir() / "server_config.json"
CLIENT_CONFIG_PATH = config_dir() / "client_config.json"
