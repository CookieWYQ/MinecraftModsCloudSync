# -*- coding: utf-8 -*-
"""C2C 临时共享下载区（.upload_files）：服务端上传共享文件，客户端凭密钥/时限下载。

远程目录：{c2c_dir}/.upload_files/
  {c2c_dir}/.upload_files/<相对路径>       — 共享文件（设置密钥时内容已加密存储）
  {c2c_dir}/.upload_files/manifest.json    — 元数据清单

清单结构：
{
  "version": 1,
  "files": {
    "相对路径": {
      "size": 123, "hash": "md5",
      "encrypted": true, "salt": "base64(16B)",
      "created_at": "ISO 时间", "expires_at": "ISO 时间或空(永久)"
    }
  }
}

- 保存时限：expires_at 到期的文件由服务端软件定时清理（cleanup_expired）；
- 密钥：上传时可设置密码，内容经「密码 + 随机盐 → PBKDF2 → Fernet」加密，下载需输入同一密码。
"""
import base64
import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from .crypto import decrypt_data, encrypt_data, new_salt
from .logger import get_logger
from .sftp import SFTPError, SFTPManager

log = get_logger("upload_files")

UPLOAD_SUBDIR = ".upload_files"
UPLOAD_MANIFEST_FILENAME = "manifest.json"


def upload_files_dir(c2c_dir: str) -> str:
    return SFTPManager.join(c2c_dir, UPLOAD_SUBDIR)


def upload_manifest_path(c2c_dir: str) -> str:
    return SFTPManager.join(c2c_dir, UPLOAD_SUBDIR, UPLOAD_MANIFEST_FILENAME)


# ---------- 清单 ----------
def load_upload_manifest(sftp: SFTPManager, c2c_dir: str) -> dict:
    """读取共享区清单 {"files": {rel: meta}}；不存在返回空结构。"""
    path = upload_manifest_path(c2c_dir)
    if not sftp.exists(path):
        return {"files": {}}
    try:
        data = json.loads(sftp.read_text(path))
        files = data.get("files", {})
        return {"files": files if isinstance(files, dict) else {}}
    except Exception as exc:
        raise SFTPError(f"解析共享文件清单失败: {exc}") from exc


def save_upload_manifest(sftp: SFTPManager, c2c_dir: str, manifest: dict) -> None:
    sftp.mkdirs(upload_files_dir(c2c_dir))
    sftp.write_text(upload_manifest_path(c2c_dir),
                    json.dumps({"version": 1, "files": manifest["files"]},
                               ensure_ascii=False, indent=2))


def is_expired(meta: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    exp = meta.get("expires_at")
    if not exp:
        return False
    try:
        return now > datetime.fromisoformat(exp)
    except Exception:
        return False


# ---------- 上传 ----------
def collect_upload_items(local_paths: list[str], rel_prefix: str = "") -> list[tuple[str, str]]:
    """收集要上传的本地路径 → [(相对路径, 绝对路径)]，保留相对文件结构。

    - 单个文件 → 相对路径为其文件名（指定 rel_prefix 时拼接前缀）；
    - 文件夹 → 递归收集，相对路径从文件夹本身开始（含文件夹名）。
    """
    items: list[tuple[str, str]] = []
    for p in local_paths:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            base_name = os.path.basename(p.rstrip("/\\"))
            for root, dirs, files in os.walk(p):
                dirs.sort()
                for name in sorted(files):
                    abs_path = os.path.join(root, name)
                    rel = os.path.relpath(abs_path, p).replace("\\", "/")
                    rel = f"{base_name}/{rel}"
                    if rel_prefix:
                        rel = f"{rel_prefix}/{rel}"
                    items.append((rel, abs_path))
        elif os.path.isfile(p):
            name = os.path.basename(p)
            rel = f"{rel_prefix}/{name}" if rel_prefix else name
            items.append((rel, p))
    items.sort(key=lambda x: x[0])
    return items


def upload_files(sftp: SFTPManager, c2c_dir: str, items: list[tuple[str, str]],
                 key: str = "", ttl_hours: int = 0, progress_cb=None,
                 byte_progress_cb=None) -> dict:
    """上传共享文件（保留相对结构），返回 {"uploaded": n, "expires_at": str}。

    - key 非空 → 内容加密后上传（下载需同一密码）；
    - ttl_hours > 0 → 到期由服务端清理（expires_at 记录进清单）；
    - progress_cb(文件序号, 文件总数, 消息) 按文件回调；
    - byte_progress_cb(已传字节, 总字节) 按字节回调（单个文件内）。
    """
    now = datetime.now()
    expires_at = (now + timedelta(hours=ttl_hours)).isoformat() if ttl_hours > 0 else ""
    manifest = load_upload_manifest(sftp, c2c_dir)
    files = manifest["files"]
    root = upload_files_dir(c2c_dir)

    total = len(items)
    for i, (rel, abs_path) in enumerate(items):
        with open(abs_path, "rb") as f:
            data = f.read()
        entry = {
            "size": len(data),
            "hash": hashlib.md5(data).hexdigest(),
            "encrypted": bool(key),
            "created_at": now.isoformat(),
            "expires_at": expires_at,
        }
        remote = SFTPManager.join(root, rel)
        if key:
            salt = new_salt()
            enc = encrypt_data(data, key, salt)
            # 加密内容写入临时文件再上传，避免大文件整体常驻内存与 SFTP 冲突
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".enc")
            try:
                tmp.write(enc)
                tmp.close()
                sftp.upload(tmp.name, remote, byte_progress_cb)
            finally:
                try:
                    os.remove(tmp.name)
                except OSError:
                    pass
            entry["salt"] = base64.b64encode(salt).decode("ascii")
        else:
            sftp.upload(abs_path, remote, byte_progress_cb)
        files[rel] = entry
        if progress_cb:
            progress_cb(i + 1, max(total, 1), f"上传 {rel}")

    save_upload_manifest(sftp, c2c_dir, manifest)
    return {"uploaded": total, "expires_at": expires_at}


# ---------- 列表 / 下载 / 删除 ----------
def list_remote_files(sftp: SFTPManager, c2c_dir: str) -> list[dict]:
    """列出共享区全部文件元数据（按路径排序），供客户端与服务端展示。"""
    manifest = load_upload_manifest(sftp, c2c_dir)
    now = datetime.now()
    out = []
    for rel, meta in sorted(manifest["files"].items()):
        out.append({
            "rel": rel,
            "size": int(meta.get("size", 0)),
            "encrypted": bool(meta.get("encrypted")),
            "created_at": meta.get("created_at", ""),
            "expires_at": meta.get("expires_at", ""),
            "expired": is_expired(meta, now),
        })
    return out


def download_file(sftp: SFTPManager, c2c_dir: str, rel: str, dest: str,
                  key: str = "", progress_cb=None, byte_progress_cb=None) -> None:
    """下载共享文件到本地 dest（相对结构由调用方拼入 dest 目录）。

    加密文件必须提供正确密码（key），否则抛 ValueError；已过期文件拒绝下载。
    progress_cb(文件序号,1,消息) / byte_progress_cb(已传字节, 总字节) 可选。
    """
    manifest = load_upload_manifest(sftp, c2c_dir)
    meta = manifest["files"].get(rel)
    if meta is None:
        raise SFTPError(f"共享文件不存在：{rel}")
    if is_expired(meta):
        raise SFTPError(f"共享文件已过期：{rel}")
    root = upload_files_dir(c2c_dir)
    remote = SFTPManager.join(root, rel)
    local = Path(dest)
    local.parent.mkdir(parents=True, exist_ok=True)
    if meta.get("encrypted"):
        if not key:
            raise ValueError(f"文件已加密，需要密钥才能下载：{rel}")
        salt = base64.b64decode(meta["salt"]) if meta.get("salt") else b""
        tmp = local.with_suffix(local.suffix + ".dl")
        try:
            sftp.download(remote, str(tmp), byte_progress_cb)
            dec = decrypt_data(tmp.read_bytes(), key, salt)
            local.write_bytes(dec)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
    else:
        sftp.download(remote, str(local), byte_progress_cb)
    if progress_cb:
        progress_cb(1, 1, f"下载完成 {rel}")


def delete_files(sftp: SFTPManager, c2c_dir: str, rels: list[str]) -> int:
    """服务端手动删除共享文件（同时更新清单、清理空目录）。返回删除数量。"""
    manifest = load_upload_manifest(sftp, c2c_dir)
    files = manifest["files"]
    root = upload_files_dir(c2c_dir)
    removed = 0
    for rel in rels:
        if rel not in files:
            continue
        try:
            sftp.delete(SFTPManager.join(root, rel))
        except SFTPError:
            pass
        del files[rel]
        removed += 1
    if removed:
        save_upload_manifest(sftp, c2c_dir, manifest)
        _prune_empty_dirs(sftp, c2c_dir)
    return removed


# ---------- 过期清理 ----------
def cleanup_expired(sftp: SFTPManager, c2c_dir: str, progress_cb=None) -> int:
    """删除已过期的共享文件并清理空目录。返回删除数量（服务端定时调用）。"""
    manifest = load_upload_manifest(sftp, c2c_dir)
    files = manifest["files"]
    now = datetime.now()
    removed = 0
    for rel, meta in list(files.items()):
        if is_expired(meta, now):
            try:
                sftp.delete(SFTPManager.join(upload_files_dir(c2c_dir), rel))
            except SFTPError:
                pass
            del files[rel]
            removed += 1
            if progress_cb:
                progress_cb(removed, 0, f"已删除过期文件 {rel}")
    if removed:
        save_upload_manifest(sftp, c2c_dir, manifest)
        _prune_empty_dirs(sftp, c2c_dir)
    return removed


def _prune_empty_dirs(sftp: SFTPManager, c2c_dir: str) -> None:
    """递归删除 .upload_files 下的空目录（保留根目录）。"""
    root = upload_files_dir(c2c_dir)
    try:
        def walk_remove(d: str):
            try:
                names = sftp.list_dir(d)
            except SFTPError:
                return
            for name in names:
                sub = SFTPManager.join(d, name)
                if sftp.is_dir(sub):
                    walk_remove(sub)
            try:
                if not sftp.list_dir(d) and d != root:
                    sftp.delete_dir(d)
            except SFTPError:
                pass
        walk_remove(root)
    except Exception as exc:
        log.info("清理共享区空目录失败: %s", exc)
