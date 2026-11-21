# -*- coding: utf-8 -*-
"""服务端文件管理（手动）核心逻辑：分类、启用/禁用、上传、重命名、删除。

设计目标（用户需求）：
- 在服务端工具里直接管理服务器上的 mods / resourcepacks / shaderpacks / plugins
  等目录，**不需要把服务端文件下载到本地**；
- 「启用 / 禁用」= 在服务器上把文件名加/去 ``.disabled`` 后缀（PCL 等启动器的
  禁用方式一致），客户端 / 服务端加载时就不会读到它；
- 分类展示、拖拽上传、手动重命名/删除/新建目录；
- 删除文件优先走「服务端回收站」（history.HistoryStore），可按保留策略还原。

本模块只负责「对 SFTP 的操作 + 结果汇总」，不依赖 Qt，便于单测；
界面在 ``server_app/ui/file_manager_page.py``。
"""
import os
import posixpath
import stat as _stat
import time

from .logger import get_logger
from .sftp import SYSTEM_ROOT_DIRS, SFTPError, SFTPManager
from .tasks import safe_target

log = get_logger("file_manager")

DISABLED_SUFFIX = ".disabled"

# 固定展示的分类（目录名与显示名）。不存在时也展示，方便拖拽上传时自动创建。
CATEGORY_ORDER = ["mods", "resourcepacks", "shaderpacks", "plugins",
                  "datapacks", "config", "defaultconfigs", "scripts"]
CATEGORY_LABELS = {
    "mods": "模组",
    "resourcepacks": "资源包",
    "shaderpacks": "光影包",
    "plugins": "服务端插件",
    "datapacks": "数据包",
    "config": "配置",
    "defaultconfigs": "默认配置",
    "scripts": "脚本",
}
TOGGLE_CATEGORIES = {"mods", "resourcepacks", "shaderpacks", "plugins", "datapacks"}

OTHER_KEY = "__other__"
OTHER_LABEL = "其他文件 / 目录"

_JUNK_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}
_SKIP_SUFFIXES = (".tmp", ".temp", ".part", ".partial", ".crdownload",
                  ".download", "~")


# ---------------- 文件名状态 ----------------
def is_disabled(name: str) -> bool:
    """文件名是否以 .disabled 结尾（被禁用）。"""
    return str(name or "").lower().endswith(DISABLED_SUFFIX)


def disabled_name(name: str) -> str:
    """返回「禁用后」的文件名（已是禁用状态则原样返回）。"""
    name = str(name or "")
    return name if is_disabled(name) else name + DISABLED_SUFFIX


def enabled_name(name: str) -> str:
    """返回「启用后」的文件名（去掉 .disabled 后缀）。"""
    name = str(name or "")
    return name[:-len(DISABLED_SUFFIX)] if is_disabled(name) else name


def toggle_name(name: str, enable=None) -> str:
    """切换 / 指定启用状态。enable=None 表示翻转；True 启用；False 禁用。"""
    if enable is None:
        return enabled_name(name) if is_disabled(name) else disabled_name(name)
    return enabled_name(name) if enable else disabled_name(name)


def category_of(rel: str) -> str:
    """相对路径所属分类（第一段目录名，小写）。"""
    rel = str(rel or "").replace("\\", "/").strip("/")
    return rel.split("/", 1)[0].lower() if "/" in rel else ""


def category_label(key: str) -> str:
    if key == OTHER_KEY:
        return OTHER_LABEL
    return CATEGORY_LABELS.get(str(key or "").lower(), str(key or ""))


def is_toggleable(rel: str, is_dir: bool = False) -> bool:
    """该路径是否支持「启用 / 禁用」（只看分类；目录不支持改名禁用）。"""
    return (not is_dir) and category_of(rel) in TOGGLE_CATEGORIES


def is_root_top(rel: str) -> bool:
    """是否根目录下的顶层条目（服务端根目录顶层文件受保护，禁止删除）。"""
    rel = str(rel or "").replace("\\", "/").strip("/")
    return bool(rel) and "/" not in rel


def format_size(size) -> str:
    try:
        value = float(max(0, int(size or 0)))
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return ""


# ---------------- 远程目录读取 ----------------
def _rel_join(base: str, name: str) -> str:
    return f"{base}/{name}".strip("/") if base else str(name)


def _safe_rel(rel: str) -> str:
    """校验相对路径（不允许 .. / 绝对路径），失败抛 ValueError。"""
    return safe_target(str(rel or "").replace("\\", "/"))


def list_dir(sftp: SFTPManager, root: str, rel_dir: str = "",
             show_hidden: bool = False) -> list[dict]:
    """读取 root 下某个相对目录的直接子项（不递归）。

    返回 ``[{name, rel, is_dir, size, mtime, disabled}, ...]``，按目录在前、
    名称排序。隐藏项默认不显示（避免误操作 .mc-sync-history 等）。
    """
    rel_dir = _safe_rel(rel_dir) if rel_dir else ""
    remote = SFTPManager.join(root, rel_dir) if rel_dir else root
    try:
        attrs = sftp.listdir_attr(remote)
    except (FileNotFoundError, IOError):
        return []
    except Exception as exc:
        raise SFTPError(f"读取目录失败：{remote}（{exc}）") from exc
    out = []
    for attr in attrs:
        name = attr.filename
        if not show_hidden and name.startswith("."):
            continue
        is_dir = _stat.S_ISDIR(attr.st_mode)
        rel = _rel_join(rel_dir, name)
        out.append({
            "name": name,
            "rel": rel,
            "is_dir": bool(is_dir),
            "size": int(attr.st_size or 0),
            "mtime": int(attr.st_mtime or 0),
            "disabled": (not is_dir) and is_disabled(name),
        })
    out.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return out


def scan_overview(sftp: SFTPManager, root: str,
                  show_hidden: bool = False) -> dict:
    """读取文件管理总览：固定分类 + 其他顶层条目。

    返回 ``{"root": root, "categories": [...]}``。每个分类：
    ``{key, label, dir, toggle, exists, entries}``。
    分类目录不存在时 exists=False（界面仍显示，拖拽上传会自动创建）。
    """
    top = list_dir(sftp, root, "", show_hidden=show_hidden)
    dirs = {e["name"].lower(): e for e in top if e["is_dir"]}
    used: set[str] = set()
    categories = []
    for key in CATEGORY_ORDER:
        hit = dirs.get(key)
        exists = hit is not None
        if exists:
            used.add(key)
        entries = list_dir(sftp, root, key, show_hidden=show_hidden) if exists else []
        categories.append({
            "key": key,
            "label": CATEGORY_LABELS[key],
            "dir": key,
            "toggle": key in TOGGLE_CATEGORIES,
            "exists": exists,
            "entries": entries,
        })
    others = [e for e in top
              if e["name"].lower() not in used and not e["name"].startswith(".")]
    if not str(root or "").strip("/"):
        # 服务端根目录为 / 时不把 /usr、/etc 等系统目录列进来（与差异扫描口径一致）
        others = [e for e in others if e["name"] not in SYSTEM_ROOT_DIRS]
    if others:
        categories.append({
            "key": OTHER_KEY,
            "label": OTHER_LABEL,
            "dir": "",
            "toggle": False,
            "exists": True,
            "entries": others,
        })
    return {"root": root, "show_hidden": bool(show_hidden),
            "categories": categories}


# ---------------- 启用 / 禁用 ----------------
def toggle_remote(sftp: SFTPManager, root: str, rels, enable=None) -> dict:
    """批量启用 / 禁用远程文件（改为 .disabled 后缀或去掉该后缀）。

    - enable=None：逐个翻转当前状态；True：全部启用；False：全部禁用
    - 只处理文件；目录、根目录顶层、非法路径会跳过并记录
    - 目标文件名已存在（例如同名 .jar 与 .jar.disabled 并存）记为冲突，不覆盖
    返回 ``{ok: [新rel...], skipped: [rel...], conflicts: [rel...],
    errors: [(rel, msg)...]}``
    """
    summary = {"ok": [], "skipped": [], "conflicts": [], "errors": []}
    for raw in rels:
        try:
            rel = _safe_rel(raw)
        except ValueError as exc:
            summary["errors"].append((str(raw), f"路径不合法：{exc}"))
            continue
        if not rel or is_root_top(rel):
            summary["errors"].append((rel, "拒绝修改根目录顶层条目"))
            continue
        src = SFTPManager.join(root, rel)
        try:
            if sftp.is_dir(src):
                summary["skipped"].append(rel)
                continue
        except Exception:
            pass
        parent, name = posixpath.dirname(rel), posixpath.basename(rel)
        new_name = toggle_name(name, enable)
        if new_name == name:
            summary["skipped"].append(rel)
            continue
        new_rel = _rel_join(parent, new_name)
        dst = SFTPManager.join(root, new_rel)
        try:
            if sftp.exists(dst):
                summary["conflicts"].append(rel)
                continue
            sftp.move(src, dst)
        except Exception as exc:
            summary["errors"].append((rel, str(exc)))
            continue
        summary["ok"].append(new_rel)
        log.info("文件管理：%s → %s", rel, new_rel)
    return summary


# ---------------- 重命名 / 新建目录 ----------------
def _check_new_name(name: str) -> str:
    name = str(name or "").strip()
    if not name or name in (".", ".."):
        raise ValueError("名称不能为空")
    if "/" in name or "\\" in name:
        raise ValueError("名称不能包含路径分隔符")
    if len(name) > 200:
        raise ValueError("名称过长")
    return name


def rename_remote(sftp: SFTPManager, root: str, rel: str, new_name: str) -> str:
    """重命名远程文件 / 目录，返回新相对路径；冲突或失败抛 ValueError/SFTPError。"""
    rel = _safe_rel(rel)
    if not rel or is_root_top(rel):
        raise ValueError("拒绝重命名根目录顶层条目")
    new_name = _check_new_name(new_name)
    parent = posixpath.dirname(rel)
    new_rel = _rel_join(parent, new_name)
    if new_rel == rel:
        return rel
    src = SFTPManager.join(root, rel)
    dst = SFTPManager.join(root, new_rel)
    if not sftp.exists(src):
        raise ValueError(f"文件不存在：{rel}")
    if sftp.exists(dst):
        raise ValueError(f"目标名称已存在：{new_rel}")
    sftp.move(src, dst)
    log.info("文件管理：重命名 %s → %s", rel, new_rel)
    return new_rel


def new_folder(sftp: SFTPManager, root: str, rel_dir: str, name: str) -> str:
    """在 rel_dir 下新建目录，返回新目录相对路径。"""
    rel_dir = _safe_rel(rel_dir) if rel_dir else ""
    name = _check_new_name(name)
    new_rel = _rel_join(rel_dir, name)
    remote = SFTPManager.join(root, new_rel)
    if sftp.exists(remote):
        raise ValueError(f"目录已存在：{new_rel}")
    sftp.mkdirs(remote)
    log.info("文件管理：新建目录 %s", new_rel)
    return new_rel


# ---------------- 删除（优先进回收站） ----------------
def delete_remote(sftp: SFTPManager, root: str, rels, store=None,
                  kind: str = "server") -> dict:
    """删除远程文件 / 目录。

    - 文件：传入 HistoryStore 时先归档进服务端回收站（可还原），否则真删除；
    - 目录：直接递归删除（回收站只支持文件，界面需二次确认）；
    - 服务端根目录顶层条目一律拒绝删除（与「更新服务端」的硬防护一致）。
    返回 ``{ok, archived, skipped, errors}``。
    """
    summary = {"ok": [], "archived": 0, "skipped": [], "errors": []}
    for raw in rels:
        try:
            rel = _safe_rel(raw)
        except ValueError as exc:
            summary["errors"].append((str(raw), f"路径不合法：{exc}"))
            continue
        if not rel or is_root_top(rel):
            summary["errors"].append((rel, "拒绝删除根目录顶层条目（start.sh 等受保护）"))
            continue
        remote = SFTPManager.join(root, rel)
        try:
            if sftp.is_dir(remote):
                sftp.delete_dir(remote)
                summary["ok"].append(rel)
                log.info("文件管理：删除目录 %s", rel)
                continue
            if store is not None:
                entry = store.archive(remote, kind, rel, "文件管理手动删除")
                if entry is None:
                    summary["errors"].append((rel, "归档到回收站失败，已跳过删除"))
                    continue
                summary["archived"] += 1
            else:
                sftp.delete(remote)
            summary["ok"].append(rel)
            log.info("文件管理：删除文件 %s（回收站=%s）", rel, store is not None)
        except Exception as exc:
            summary["errors"].append((rel, str(exc)))
    return summary


# ---------------- 上传 ----------------
def _skip_local(name: str) -> bool:
    low = str(name or "").lower()
    if low.startswith(".") or low in _JUNK_NAMES:
        return True
    return low.endswith(_SKIP_SUFFIXES)


def collect_local_items(paths) -> list[tuple[str, str, int]]:
    """把用户拖入/选择的本地文件、文件夹展开为 [(本地绝对路径, 相对子路径, 大小)]。

    文件夹会保留相对于其父目录的结构（拖入文件夹 = 在目标分类下新建同名文件夹）；
    隐藏项 / 系统垃圾 / 临时文件自动跳过，``.jar.disabled`` 这类禁用文件会保留。
    """
    items: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    for raw in paths:
        path = os.path.abspath(str(raw))
        if os.path.isdir(path):
            base = os.path.dirname(path)
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fn in filenames:
                    if _skip_local(fn):
                        continue
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, base).replace(os.sep, "/")
                    if rel in seen:
                        continue
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        continue
                    seen.add(rel)
                    items.append((full, rel, int(size)))
        elif os.path.isfile(path):
            if _skip_local(os.path.basename(path)):
                continue
            rel = os.path.basename(path)
            if rel in seen:
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            seen.add(rel)
            items.append((path, rel, int(size)))
    return items


def target_rels(target_dir: str, items) -> list[tuple[str, str, int]]:
    """把本地条目映射到远程相对路径：[(本地路径, 远程相对路径, 大小)]。"""
    target_dir = str(target_dir or "").strip("/")
    out = []
    for full, suffix, size in items:
        suffix = str(suffix or "").replace("\\", "/").lstrip("/")
        rel = f"{target_dir}/{suffix}" if target_dir else suffix
        out.append((full, rel, int(size)))
    return out


def find_conflicts(sftp: SFTPManager, root: str, rels) -> list[str]:
    """返回远程已存在的目标相对路径（上传前用于询问覆盖 / 跳过）。

    按目标目录分组、每目录只列一次，避免几百个文件逐个 exists() 的往返开销。
    """
    by_dir: dict[str, set[str]] = {}
    for rel in rels:
        rel = str(rel or "").replace("\\", "/").strip("/")
        if not rel:
            continue
        parent, name = posixpath.split(rel)
        by_dir.setdefault(parent, set()).add(name)
    hits: list[str] = []
    for parent, names in by_dir.items():
        remote_dir = SFTPManager.join(root, parent) if parent else root
        try:
            existing = {n for n, _isdir, _size, _mtime in
                        sftp.list_entries(remote_dir)}
        except Exception:
            continue
        for name in names:
            if name in existing:
                hits.append(f"{parent}/{name}" if parent else name)
    return hits


def upload_items(sftp: SFTPManager, root: str, items, overwrite: bool = True,
                 progress_cb=None) -> dict:
    """上传 [(本地路径, 远程相对路径, 大小)] 到服务器，返回汇总。

    overwrite=False 时已存在的目标跳过。progress_cb(done, total, message) 可选。
    """
    summary = {"uploaded": [], "skipped": [], "errors": [], "bytes": 0}
    total = len(items)
    total_bytes = sum(int(item[2] or 0) for item in items)
    done_bytes = 0
    last_emit = [0.0]

    def on_file_bytes(sent: int, _total: int, cur_idx: int, cur_rel: str):
        """单文件上传中的字节进度（节流，避免频繁刷 UI）。"""
        if not progress_cb:
            return
        now = time.monotonic()
        if now - last_emit[0] < 0.2 and sent < _total:
            return
        last_emit[0] = now
        try:
            progress_cb(cur_idx, total,
                        f"上传 {cur_rel}（{format_size(done_bytes + sent)} / "
                        f"{format_size(total_bytes)}）")
        except Exception:
            pass

    for idx, (full, rel, size) in enumerate(items, 1):
        rel = str(rel or "").replace("\\", "/").strip("/")
        if not rel:
            continue
        remote = SFTPManager.join(root, rel)
        try:
            if not overwrite and sftp.exists(remote):
                summary["skipped"].append(rel)
            else:
                sftp.upload(full, remote,
                            progress_cb=lambda sent, tot, i=idx, r=rel:
                            on_file_bytes(sent, tot, i, r))
                summary["uploaded"].append(rel)
                summary["bytes"] += int(size or 0)
        except Exception as exc:
            summary["errors"].append((rel, str(exc)))
            log.warning("文件管理：上传失败 %s（%s）", rel, exc)
        done_bytes += int(size or 0)
        if progress_cb:
            try:
                progress_cb(idx, total,
                            f"上传 {rel}（{format_size(done_bytes)} / "
                            f"{format_size(total_bytes)}）")
            except Exception:
                pass
    return summary
