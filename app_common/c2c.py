# -*- coding: utf-8 -*-
"""C2C（本地对本地）数据层：服务端工具向指定客户端（按 UUID）发布保持文件结构的文件。

远程目录结构（默认 /c2c，可在「服务器设置」中配置）：
  {c2c_dir}/roster.json                 — 服务端代号名单（代号全局唯一，用于去重）
  {c2c_dir}/files/{client_uuid}/...     — 每个客户端的文件（保持本地目录结构）
  {c2c_dir}/manifests/{client_uuid}.json— 每个客户端的 C2C 待办清单（复用 TodoManifest）

设计原则与 C2S / S2C 一致：任务驱动、树状结构、文件保持相对路径。
"""
import json
import os
from datetime import datetime

from .constants import DEFAULT_REMOTE_C2C_DIR
from .logger import get_logger
from .sftp import SFTPError, SFTPManager
from .tasks import TaskItem, TodoManifest

log = get_logger("c2c")

ROSTER_FILENAME = "roster.json"
FILES_SUBDIR = "files"
MANIFESTS_SUBDIR = "manifests"


def abs_c2c_dir(value: str) -> str:
    """C2C 目录统一为绝对路径（以 / 开头）。"""
    p = (value or "").strip().replace("\\", "/")
    if not p:
        return DEFAULT_REMOTE_C2C_DIR
    if not p.startswith("/"):
        p = "/" + p.lstrip("/")
    return p


# ---------- 服务端代号名单（roster） ----------
def roster_path(c2c_dir: str) -> str:
    return SFTPManager.join(c2c_dir, ROSTER_FILENAME)


def load_roster(sftp: SFTPManager, c2c_dir: str) -> list[dict]:
    """读取服务端代号名单 [{code, server_name, joined_at}]；不存在或损坏返回 []。"""
    path = roster_path(c2c_dir)
    if not sftp.exists(path):
        return []
    try:
        data = json.loads(sftp.read_text(path))
        entries = data.get("entries", [])
        return entries if isinstance(entries, list) else []
    except SFTPError:
        raise
    except Exception as exc:
        raise SFTPError(f"解析服务端代号名单失败: {exc}") from exc


def save_roster(sftp: SFTPManager, c2c_dir: str, entries: list[dict]) -> None:
    sftp.mkdirs(c2c_dir)
    sftp.write_text(roster_path(c2c_dir),
                    json.dumps({"entries": entries}, ensure_ascii=False, indent=2))


def register_code(sftp: SFTPManager, c2c_dir: str, code: str,
                  server_name: str) -> str:
    """登记服务端代号（全局唯一，重复则拒绝）。返回错误消息（空串表示成功）。"""
    code = (code or "").strip()
    if not code:
        return "请填写服务端代号。"
    entries = load_roster(sftp, c2c_dir)
    for e in entries:
        if e.get("code") == code:
            return (f"代号「{code}」已被其他服务端占用"
                    f"（{e.get('server_name', '未知')}，{e.get('joined_at', '')}），请更换。")
    entries.append({
        "code": code,
        "server_name": (server_name or "").strip(),
        "joined_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    save_roster(sftp, c2c_dir, entries)
    return ""


# ---------- C2C 清单与文件 ----------
def manifests_dir(c2c_dir: str) -> str:
    return SFTPManager.join(c2c_dir, MANIFESTS_SUBDIR)


def manifest_path(c2c_dir: str, client_uuid: str) -> str:
    return SFTPManager.join(c2c_dir, MANIFESTS_SUBDIR, f"{client_uuid}.json")


def files_root(c2c_dir: str, client_uuid: str) -> str:
    """该客户端专属的文件根目录（保持文件结构）。"""
    return SFTPManager.join(c2c_dir, FILES_SUBDIR, client_uuid)


def load_client_manifest(sftp: SFTPManager, c2c_dir: str,
                         client_uuid: str) -> TodoManifest | None:
    """读取指定客户端的 C2C 清单；不存在返回 None。"""
    path = manifest_path(c2c_dir, client_uuid)
    if not sftp.exists(path):
        return None
    try:
        return TodoManifest.from_dict(json.loads(sftp.read_text(path)))
    except SFTPError:
        raise
    except Exception as exc:
        raise SFTPError(f"解析 C2C 清单失败: {exc}") from exc


def save_client_manifest(sftp: SFTPManager, c2c_dir: str,
                         client_uuid: str, manifest: TodoManifest) -> None:
    sftp.mkdirs(manifests_dir(c2c_dir))
    sftp.write_text(manifest_path(c2c_dir, client_uuid),
                    json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2))


def local_files(local_dir: str) -> list[tuple[str, str]]:
    """收集本地目录下的全部文件 [(相对路径, 绝对路径)]，保持目录结构、按路径排序。"""
    local_dir = os.path.abspath(local_dir)
    out: list[tuple[str, str]] = []
    for root, dirs, files in os.walk(local_dir):
        dirs.sort()
        for name in sorted(files):
            abs_path = os.path.join(root, name)
            rel = os.path.relpath(abs_path, local_dir).replace("\\", "/")
            out.append((rel, abs_path))
    return out


KNOWN_CATEGORIES = ("mods", "resourcepacks", "config")


def _infer_category(rel: str) -> str:
    """按目标文件顶层目录推断任务分类（与 S2C 保持一致），否则归为「其他」。"""
    top = rel.split("/", 1)[0].lower()
    return top if top in KNOWN_CATEGORIES else "other"


def send_worker(config, client_uuid: str, version: str, progress_cb=None) -> str:
    """把本地 C2C 源目录（config.c2c_local_dir）的全部改动发布给指定客户端（保持文件结构）。

    对比上次发布的清单，按差异生成任务：
    - 新增文件 → install；
    - 内容变化（大小相同但哈希不同等）→ 重新 install；
    - 内容未变 → 跳过（不重复上传）；
    - 本地移除且与新增文件内容相同（大小 + 哈希一致）→ 改名（删除旧文件 + 安装新文件）；
    - 本地移除且无匹配 → delete（客户端删除对应文件）。
    清单中记录本次文件的哈希表（meta.files），供下次发送时判断内容是否变化。

    返回完成信息文本；出错抛出异常由 Worker 统一捕获。
    """
    from .file_hash import hash_file
    from .tasks import safe_target

    local_dir = config.c2c_local_dir
    if not local_dir or not os.path.isdir(local_dir):
        raise RuntimeError("请先在 C2C 页选择本地发送目录。")
    files = local_files(local_dir)
    # 目录存在但为空（文件被全部移除）→ 允许：将发布删除全部已发布文件
    c2c_dir = abs_c2c_dir(config.c2c_dir)
    root = files_root(c2c_dir, client_uuid)

    abs_by_rel = {rel: abs_path for rel, abs_path in files}
    local_hashes = {rel: hash_file(abs_path) for rel, abs_path in files}
    tasks: list[TaskItem] = []
    uploads: list[str] = []
    installed_n = updated_n = renamed_n = deleted_n = 0

    with SFTPManager(config.host(), config.port(),
                     config.username(), config.password()) as sftp:
        prev = load_client_manifest(sftp, c2c_dir, client_uuid)
        prev_meta = dict((prev.meta or {}).get("files", {})) if prev else {}
        if prev_meta:
            prev_rels = set(prev_meta)
        elif prev is not None:
            prev_rels = {t.source or t.target for t in prev.tasks
                         if t.action == "install"}
        else:
            prev_rels = set()

        current_rels = set(local_hashes)
        # 改名配对：本地新增 Y ↔ 上次发布后消失 X（内容相同 → 改名而非 新增+删除）
        gone = prev_rels - current_rels
        added = current_rels - prev_rels
        matched: dict[str, str] = {}
        used: set[str] = set()
        for y in sorted(added):
            hy = local_hashes[y]
            for x in sorted(gone):
                if prev_meta.get(x) == hy and x not in used:
                    matched[y] = x
                    used.add(x)
                    break

        def add_task(action, category, target, source="", description=""):
            tasks.append(TaskItem(
                action=action, category=category, target=safe_target(target),
                source=safe_target(source) if source else "",
                description=description))

        for rel, _ in files:
            if rel in matched:
                x = matched[rel]
                add_task("delete", _infer_category(x), x,
                         description="C2C 改名（删除旧文件）")
                add_task("install", _infer_category(rel), rel, rel,
                         description="C2C 改名（安装新文件）")
                uploads.append(rel)
                renamed_n += 1
            elif rel in added:
                add_task("install", _infer_category(rel), rel, rel,
                         description="C2C 文件发布")
                uploads.append(rel)
                installed_n += 1
            elif prev_meta.get(rel) is None or prev_meta.get(rel) != local_hashes[rel]:
                # 旧清单无哈希记录（无法判断）或内容已变化 → 重新上传安装
                add_task("install", _infer_category(rel), rel, rel,
                         description="C2C 文件更新")
                uploads.append(rel)
                updated_n += 1
            # else 内容未变化 → 跳过（不重复上传）

        # 上次发布存在、本次本地已移除且未配对改名 → 客户端删除
        for x in sorted(gone - used):
            add_task("delete", _infer_category(x), x,
                     description="C2C 文件已移除")
            deleted_n += 1

        total = len(uploads)
        for i, rel in enumerate(uploads):
            remote = SFTPManager.join(root, rel)
            sftp.mkdirs(SFTPManager.join(root, os.path.dirname(rel))
                        if os.path.dirname(rel) else root)
            sftp.upload(abs_by_rel[rel], remote)
            if progress_cb:
                progress_cb(i + 1, max(total, 1), f"上传 {rel}")

        manifest = TodoManifest.new(version or f"c2c-{datetime.now().strftime('%m%d%H%M')}")
        manifest.tasks = tasks
        manifest.meta = {"files": local_hashes}
        save_client_manifest(sftp, c2c_dir, client_uuid, manifest)

    parts = [f"已向客户端 {client_uuid} 发布差异完成"]
    if installed_n:
        parts.append(f"新增 {installed_n} 个")
    if updated_n:
        parts.append(f"更新 {updated_n} 个")
    if renamed_n:
        parts.append(f"改名 {renamed_n} 个")
    if deleted_n:
        parts.append(f"删除 {deleted_n} 个")
    if not tasks:
        parts.append("（无变化，客户端已为最新）")
    parts.append(f"版本：{manifest.version}（{manifest.formatted_time()}）")
    return "\n".join(parts)
