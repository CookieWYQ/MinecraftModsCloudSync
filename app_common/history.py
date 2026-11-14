# -*- coding: utf-8 -*-
"""C2S 文件历史（服务端回收站）：删除 / 替换前把旧文件挪进历史目录。

为什么要它：
- 「更新服务端」的删除是真删除，「替换」是直接覆盖；升级模组、改名、清理旧版后
  就再也拿不回上一版文件。快照（`snapshot.py`）只记录 `{路径: 大小/哈希}`，
  不保存文件内容，所以回滚只能靠「本地恰好还有旧文件」。
- 本模块把要删除/被覆盖的文件先**移动**到服务器上的历史目录，再执行删除/上传，
  这样旧版本随时可以还原，且不额外占用「活动文件」的空间。

远程结构（默认 `{files_dir}/.mc-sync-history`，隐藏目录，扫描自动跳过）：
  index.json                      — 条目索引（JSON）
  {id}/{kind}/{rel}               — 归档文件本体（kind: server=服务端仓库，client=客户端文件目录）

占用空间的控制（这是「历史会不会撑爆服务器」的答案）：
- 只归档「被删除 / 被覆盖」的文件，纯新增不产生历史；
- 按「每条路径保留最近 N 个版本」清理（默认 3）；
- 再按「历史目录总大小上限」清理最旧的条目（默认 512 MB，0 = 不限）；
- 归档用服务器内 rename/move（不传输内容，几乎无额外流量）；
- 可在界面里手动删除单条 / 清空全部 / 立即按策略清理。
"""
import json
import os
import tempfile
from datetime import datetime

from .logger import get_logger
from .sftp import SFTPError, SFTPManager
from .tasks import safe_target

log = get_logger("history")

HISTORY_SUBDIR = ".mc-sync-history"
INDEX_FILENAME = "index.json"
KIND_LABELS = {"server": "服务端", "client": "客户端"}

DEFAULT_KEEP = 3
DEFAULT_MAX_MB = 512
MAX_KEEP = 50
MAX_MAX_MB = 20480


def history_root(files_dir: str) -> str:
    """历史目录的远程根（位于客户端文件目录下的隐藏目录）。"""
    return SFTPManager.join(files_dir, HISTORY_SUBDIR)


def index_path(files_dir: str) -> str:
    return SFTPManager.join(history_root(files_dir), INDEX_FILENAME)


def _entry_dir(files_dir: str, entry: dict) -> str:
    return SFTPManager.join(history_root(files_dir), str(entry.get("id", "")))


def entry_remote_path(files_dir: str, entry: dict) -> str:
    """归档文件在服务器上的绝对路径。"""
    return SFTPManager.join(history_root(files_dir), str(entry.get("id", "")),
                            str(entry.get("kind", "server")), str(entry.get("rel", "")))


def load_index(sftp: SFTPManager, files_dir: str) -> dict:
    """读取历史索引；不存在或损坏时返回空索引（不让历史功能阻塞发送）。"""
    path = index_path(files_dir)
    if not sftp.exists(path):
        return {"version": 1, "entries": []}
    try:
        data = json.loads(sftp.read_text(path))
    except Exception as exc:
        log.warning("读取历史索引失败，按空索引处理: %s", exc)
        return {"version": 1, "entries": []}
    entries = data.get("entries") if isinstance(data, dict) else None
    return {"version": 1,
            "entries": [e for e in (entries or []) if isinstance(e, dict) and e.get("id")]}


def save_index(sftp: SFTPManager, files_dir: str, index: dict) -> None:
    sftp.mkdirs(history_root(files_dir))
    sftp.write_text(index_path(files_dir),
                    json.dumps(index, ensure_ascii=False, indent=2))


def usage(index: dict) -> dict:
    """返回 {"count": 条目数, "bytes": 总大小}。"""
    entries = index.get("entries") or []
    return {"count": len(entries),
            "bytes": sum(int(e.get("size") or 0) for e in entries)}


def format_size(n) -> str:
    n = int(n or 0)
    if n < 1024:
        return f"{n} B"
    kb = n / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    return f"{kb / 1024:.2f} MB"


class HistoryStore:
    """一次发送过程中的历史操作会话（索引读一次、最后统一写回）。

    典型用法：
        store = HistoryStore(sftp, files_dir)
        store.archive(remote_path, "server", rel, "替换旧版")
        store.prune(keep, max_mb)
        store.save()
    """

    def __init__(self, sftp: SFTPManager, files_dir: str):
        self.sftp = sftp
        self.files_dir = files_dir
        self.index = load_index(sftp, files_dir)
        self.added: list[dict] = []
        self.failures: list[str] = []
        self.dirty = False

    # ---------- 归档 ----------
    def archive(self, src: str, kind: str, rel: str, reason: str = "") -> dict | None:
        """把远程文件 `src` 移动到历史目录；失败返回 None 并记录原因。

        优先用服务器内 move（等同 rename，不传内容）；跨文件系统/不支持时
        退化为「下载 → 上传 → 删除原文件」。两种方式都失败时调用方应放弃删除/覆盖，
        避免旧版本丢失。
        """
        try:
            rel = safe_target(rel)
        except ValueError as exc:
            self.failures.append(f"{rel}（路径不合法：{exc}）")
            return None
        kind = kind if kind in KIND_LABELS else "server"
        now = datetime.now()
        entry_id = now.strftime("%Y%m%d_%H%M%S_%f")
        dest = SFTPManager.join(history_root(self.files_dir), entry_id, kind, rel)
        try:
            self.sftp.mkdirs(SFTPManager.join(history_root(self.files_dir),
                                              entry_id, kind,
                                              os.path.dirname(rel)))
        except Exception as exc:
            self.failures.append(f"{rel}（创建历史目录失败：{exc}）")
            return None
        moved = False
        try:
            self.sftp.move(src, dest)
            moved = True
        except SFTPError as exc:
            log.info("历史归档用 move 失败，改为下载/上传: %s（%s）", rel, exc)
        except Exception as exc:
            log.info("历史归档用 move 失败，改为下载/上传: %s（%s）", rel, exc)
        if not moved:
            try:
                with tempfile.TemporaryDirectory(prefix="mc_hist_") as tmp:
                    local = os.path.join(tmp, "file")
                    self.sftp.download(src, local)
                    self.sftp.upload(local, dest)
                self.sftp.delete(src)
            except Exception as exc:
                self.failures.append(f"{rel}（归档失败：{exc}）")
                log.warning("历史归档失败: %s（%s）", rel, exc)
                return None
        try:
            size = max(0, int(self.sftp.file_size(dest)))
        except Exception:
            size = 0
        entry = {
            "id": entry_id,
            "kind": kind,
            "rel": rel,
            "size": size,
            "reason": (reason or "").strip(),
            "archived_at": now.isoformat(timespec="seconds"),
            "server_path": src,
        }
        self.index["entries"].append(entry)
        self.added.append(entry)
        self.dirty = True
        log.info("已归档旧文件: %s（%s，%s，%s B）",
                 rel, KIND_LABELS[kind], entry["reason"] or "删除/替换", size)
        return entry

    # ---------- 清理 ----------
    def prune(self, keep: int = DEFAULT_KEEP,
              max_mb: int = DEFAULT_MAX_MB) -> list[dict]:
        """按策略清理历史：每条路径保留最近 keep 个，再按总大小上限删最旧的。"""
        try:
            keep = max(1, min(MAX_KEEP, int(keep)))
        except (TypeError, ValueError):
            keep = DEFAULT_KEEP
        try:
            max_mb = max(0, min(MAX_MAX_MB, int(max_mb)))
        except (TypeError, ValueError):
            max_mb = DEFAULT_MAX_MB

        groups: dict[tuple, list[dict]] = {}
        for e in self.index.get("entries", []):
            groups.setdefault((e.get("kind", "server"), e.get("rel", "")), []).append(e)
        kept: list[dict] = []
        removed: list[dict] = []
        for group in groups.values():
            group.sort(key=lambda e: str(e.get("archived_at", "")), reverse=True)
            kept.extend(group[:keep])
            removed.extend(group[keep:])

        max_bytes = max_mb * 1024 * 1024
        if max_bytes > 0:
            kept.sort(key=lambda e: str(e.get("archived_at", "")))
            total = sum(int(e.get("size") or 0) for e in kept)
            while kept and total > max_bytes:
                e = kept.pop(0)
                removed.append(e)
                total -= int(e.get("size") or 0)

        still: list[dict] = []
        for e in removed:
            if not self._delete_entry_file(e):
                still.append(e)
        self.index["entries"] = kept + still
        self.dirty = True
        if removed:
            log.info("历史清理：删除 %d 条（保留 %d 条）",
                     len(removed) - len(still), len(self.index["entries"]))
        return removed

    def _delete_entry_file(self, entry: dict) -> bool:
        """删除一条历史记录对应的文件；成功返回 True。"""
        remote = entry_remote_path(self.files_dir, entry)
        try:
            if self.sftp.is_dir(remote):
                self.sftp.delete_dir(remote)
            else:
                self.sftp.delete(remote)
        except Exception as exc:
            log.warning("删除历史文件失败: %s（%s）", remote, exc)
            return False
        # 顺手清掉空的 {id}/{kind} 与 {id} 目录（delete_dir 只对空目录生效前会先列目录，
        # 这里目录里只剩本文件，删除文件后再删目录是安全的）。
        for d in (SFTPManager.join(history_root(self.files_dir),
                                   str(entry.get("id", "")),
                                   str(entry.get("kind", ""))),
                  _entry_dir(self.files_dir, entry)):
            try:
                if self.sftp.exists(d) and not self.sftp.list_dir(d):
                    self.sftp.delete_dir(d)
            except Exception:
                pass
        return True

    def remove_entries(self, ids: list[str]) -> list[dict]:
        """删除指定 id 的历史条目（界面「删除所选」）。返回成功删除的条目。"""
        wanted = set(ids)
        removed: list[dict] = []
        rest: list[dict] = []
        for e in self.index.get("entries", []):
            if e.get("id") in wanted:
                if self._delete_entry_file(e):
                    removed.append(e)
                else:
                    rest.append(e)
            else:
                rest.append(e)
        self.index["entries"] = rest
        self.dirty = True
        return removed

    def save(self) -> None:
        if self.dirty:
            save_index(self.sftp, self.files_dir, self.index)
            self.dirty = False


# ---------- 界面用的独立操作（各自建连接，供历史对话框调用） ----------
def list_history(sftp: SFTPManager, files_dir: str) -> dict:
    """返回 {"entries": [...]（按时间倒序）, "usage": {...}}。"""
    index = load_index(sftp, files_dir)
    entries = sorted(index.get("entries", []),
                     key=lambda e: str(e.get("archived_at", "")), reverse=True)
    return {"entries": entries, "usage": usage(index)}


def restore_entries(sftp: SFTPManager, files_dir: str, server_root: str,
                    ids: list[str], progress_cb=None) -> dict:
    """把历史条目还原回原路径。

    - 原路径已存在时，先把现有文件归档（理由「还原前自动备份」），再还原旧版；
    - 还原后该条目从索引移除（它已回到活动目录）。
    返回 {"restored": [rel...], "failed": [msg...], "backed_up": n}。
    """
    wanted = set(ids)
    store = HistoryStore(sftp, files_dir)
    entries = [e for e in store.index.get("entries", []) if e.get("id") in wanted]
    restored: list[str] = []
    failed: list[str] = []
    backed_up = 0
    total = len(entries)
    for i, entry in enumerate(entries):
        rel = entry.get("rel", "")
        kind = entry.get("kind", "server")
        if progress_cb:
            progress_cb(i, total, f"还原 {rel}")
        src = entry_remote_path(files_dir, entry)
        if not sftp.exists(src):
            failed.append(f"{rel}（历史文件已不存在）")
            continue
        dest_root = server_root if kind == "server" else files_dir
        dest = SFTPManager.join(dest_root, rel)
        try:
            if sftp.exists(dest):
                if store.archive(dest, kind, rel, "还原前自动备份"):
                    backed_up += 1
                else:
                    failed.append(f"{rel}（现有文件备份失败，已跳过还原）")
                    continue
            sftp.mkdirs(SFTPManager.join(dest_root, os.path.dirname(rel)))
            sftp.move(src, dest)
        except Exception as exc:
            failed.append(f"{rel}（{exc}）")
            log.warning("还原历史文件失败: %s（%s）", rel, exc)
            continue
        store.index["entries"] = [e for e in store.index["entries"]
                                  if e.get("id") != entry.get("id")]
        store.dirty = True
        restored.append(f"{KIND_LABELS.get(kind, kind)}/{rel}")
    try:
        store.save()
    except Exception as exc:
        failed.append(f"保存历史索引失败：{exc}")
    return {"restored": restored, "failed": failed, "backed_up": backed_up}


def clear_history(sftp: SFTPManager, files_dir: str) -> int:
    """清空整个历史目录，返回清理的条目数。"""
    index = load_index(sftp, files_dir)
    count = len(index.get("entries", []))
    root = history_root(files_dir)
    try:
        if sftp.exists(root):
            sftp.delete_dir(root)
    except Exception as exc:
        raise SFTPError(f"清空历史目录失败：{exc}") from exc
    return count
