# -*- coding: utf-8 -*-
"""自动同步（免待办直连拉取）：服务端同步策略 + 客户端对比/下载。

「发布待办（S2C）」要求服主先检测改动并生成清单，客户端再按清单安装；
本模块提供另一条更直接的链路：

    服务器 {files_dir}  ──按策略扫描──▶ 与本地游戏目录逐文件对比 ──▶ 只下载新增/变化的文件

适合「服务器上的文件就是最新版，玩家不必等服主发布待办」的场景。两条链路互不影响。

职责划分：
- 服务端：编辑并下发策略文件 ``{todo_dir}/auto_sync.json``（总开关 + 白名单 + 黑名单）；
- 客户端：读取策略，只比较/下载策略允许的路径；**绝不删除本地文件**（本地多余文件只报告）。

安全边界：
1. 策略总开关默认关闭，客户端界面的自动同步开关默认关闭，两层都打开才生效；
2. 白名单为空 = 不同步任何文件（不采用「空 = 全部」的宽松语义）；
3. 黑名单优先级高于白名单，命中即排除，且目录命中黑名单时整棵子树都不进入；
4. 下载目标经过 ``safe_target()`` 与「必须位于游戏目录内」双重校验，拒绝路径穿越；
5. 跳过隐藏项与临时文件（``.tmp`` / ``.part`` / ``.crdownload`` 等），避免同步到半成品。
"""
import fnmatch
import hashlib
import json
import os
import tempfile
from datetime import datetime
from functools import lru_cache

from .constants import DEFAULT_SYNC_EXCLUDE_PATTERNS
from .file_hash import hash_file_cached, hash_remote_parallel
from .logger import get_logger
from .sftp import DEFAULT_SCAN_WORKERS, SFTPError, SFTPManager
from .tasks import safe_target

log = get_logger("auto_sync")

POLICY_FILENAME = "auto_sync.json"
POLICY_SCHEMA = 1

# 任何策略下都不参与同步的垃圾/临时文件（避免把半成品同步给玩家）
_JUNK_NAMES = {
    "desktop.ini", "thumbs.db", ".ds_store", "session.lock",
    "ehthumbs.db", "iconcache.db",
}
_TEMP_SUFFIXES = (".tmp", ".part", ".partial", ".crdownload", ".download", "~")
_GLOB_CHARS = "*?["


# ---------------- 策略模型 ----------------
def policy_path(todo_dir: str) -> str:
    """策略文件的远程路径（放在待办目录，客户端已能读取该目录）。"""
    return SFTPManager.join(todo_dir, POLICY_FILENAME)


def new_policy(enabled: bool = False, include=None, exclude=None,
               note: str = "") -> dict:
    """构造一份规范化的策略。include 为空表示不同步任何文件。"""
    return normalize_policy({
        "enabled": bool(enabled),
        "include": list(include or []),
        "exclude": list(exclude if exclude is not None
                         else DEFAULT_SYNC_EXCLUDE_PATTERNS),
        "note": note,
        "updated_at": _now_iso(),
    })


def default_policy() -> dict:
    """界面「恢复默认」用的策略：关闭状态，黑名单取默认排除模式，白名单为空。"""
    return new_policy(enabled=False, include=[],
                      exclude=list(DEFAULT_SYNC_EXCLUDE_PATTERNS))


def normalize_policy(raw) -> dict:
    """把任意来源（JSON / 表单）的策略规范化为固定结构。

    - 未知字段忽略；缺失字段取安全默认值（关闭、空白名单）；
    - 白名单/黑名单条目统一为 POSIX 相对路径；拒绝 ``..`` / 绝对路径；
    - 条目去重（忽略大小写），保留填写顺序。
    """
    raw = raw if isinstance(raw, dict) else {}
    return {
        "schema": POLICY_SCHEMA,
        "enabled": bool(raw.get("enabled", False)),
        "include": _clean_entries(raw.get("include")),
        "exclude": _clean_entries(raw.get("exclude")),
        "note": str(raw.get("note") or ""),
        "updated_at": str(raw.get("updated_at") or ""),
    }


def policy_signature(policy) -> str:
    """策略内容指纹（开关 + 白名单 + 黑名单）；只用于界面提示，不参与安全判断。"""
    policy = normalize_policy(policy)
    payload = {
        "enabled": policy["enabled"],
        "include": policy["include"],
        "exclude": policy["exclude"],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.md5(blob.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _clean_entries(values) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in values or []:
        if isinstance(item, (list, tuple)):
            out.extend(_clean_entries(item))
            continue
        text = str(item or "").replace("\\", "/").strip()
        if not text:
            continue
        text = text.strip("/")
        while "//" in text:
            text = text.replace("//", "/")
        try:
            text = safe_target(text)
        except ValueError:
            log.warning("自动同步策略忽略了不合法路径: %s", item)
            continue
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


# ---------------- SFTP 读写 ----------------
def load_policy(sftp: SFTPManager, todo_dir: str) -> dict | None:
    """读取服务器策略；文件不存在返回 None（表示服主尚未下发）。"""
    path = policy_path(todo_dir)
    if not sftp.exists(path):
        return None
    try:
        data = json.loads(sftp.read_text(path))
    except SFTPError:
        raise
    except Exception as exc:
        raise SFTPError(f"解析自动同步策略失败: {exc}") from exc
    if not isinstance(data, dict):
        raise SFTPError("自动同步策略格式错误：应为 JSON 对象")
    return normalize_policy(data)


def save_policy(sftp: SFTPManager, todo_dir: str, policy) -> dict:
    """下发策略到服务器，返回实际写入的规范化内容。"""
    data = normalize_policy(policy)
    data["schema"] = POLICY_SCHEMA
    sftp.mkdirs(todo_dir)
    sftp.write_text(policy_path(todo_dir),
                    json.dumps(data, ensure_ascii=False, indent=2))
    return data


def update_exclude(policy, *, add=(), remove=()) -> tuple[dict, bool]:
    """在黑名单里加入 / 移除条目，返回 ``(新策略, 是否有变化)``。

    供「文件管理（手动）」页右键「排除出自动同步」使用：

    - 条目按**整条路径**精确比较（忽略大小写），不碰白名单与总开关；
    - ``policy`` 为 None（服务器还没下发策略）时以默认策略为基础（关闭状态），
      这样标记只会写黑名单，不会意外把自动同步打开；
    - 没有实际变化时返回 ``changed=False``，调用方可以不写回服务器。
    """
    base = normalize_policy(policy if policy is not None else default_policy())
    entries = list(base["exclude"])
    existing = {str(item).lower() for item in entries}
    changed = False
    for item in _clean_entries(add):
        if item.lower() in existing:
            continue
        entries.append(item)
        existing.add(item.lower())
        changed = True
    drop = {str(item).lower() for item in _clean_entries(remove)}
    if drop:
        kept = [item for item in entries if str(item).lower() not in drop]
        if len(kept) != len(entries):
            entries = kept
            changed = True
    out = dict(base)
    out["exclude"] = entries
    if changed:
        out["updated_at"] = _now_iso()
    return out, changed


# ---------------- 路径与黑白名单匹配 ----------------
def _split(path) -> list[str]:
    return [p for p in str(path or "").replace("\\", "/").split("/")
            if p and p != "."]


def _has_glob(text: str) -> bool:
    return any(ch in str(text or "") for ch in _GLOB_CHARS)


def _seg_match(pattern: str, name: str) -> bool:
    """单段通配匹配（``*`` / ``?`` / ``[]`` 不跨目录分隔符，忽略大小写）。"""
    return fnmatch.fnmatchcase(str(name).lower(), str(pattern).lower())


@lru_cache(maxsize=16384)
def _match_segments(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    """逐段匹配，支持 ``**`` 跨任意多层目录。"""
    if not pattern:
        return not path
    head = pattern[0]
    if head == "**":
        if _match_segments(pattern[1:], path):
            return True
        return bool(path) and _match_segments(pattern, path[1:])
    if not path:
        return False
    if _seg_match(head, path[0]):
        return _match_segments(pattern[1:], path[1:])
    return False


def glob_match(pattern: str, rel: str) -> bool:
    """整条相对路径的 glob 匹配（例：``config/*.toml``、``mods/**/*.jar``）。"""
    return _match_segments(tuple(_split(pattern)), tuple(_split(rel)))


def entry_matches(entry: str, rel: str) -> bool:
    """策略条目是否命中某条路径。

    - 不含通配符的条目按「文件或目录前缀」匹配：``mods`` 命中 ``mods/a.jar``；
    - 含通配符且**不带 /** 的条目按文件名匹配（任意层级）：``*.disabled`` 命中
      ``mods/a.disabled``；
    - 含通配符且带 / 的条目做整条路径 glob：``mods/*.jar`` 不命中
      ``mods/sub/a.jar``，``mods/**/*.jar`` 才跨层。
    """
    rs = _split(rel)
    if not rs:
        return False
    es = _split(entry)
    if not es:
        return False
    if _has_glob(entry):
        if "/" not in str(entry).replace("\\", "/"):
            return _seg_match(entry, rs[-1])
        return _match_segments(tuple(es), tuple(rs))
    if len(rs) < len(es):
        return False
    return all(_seg_match(e, r) for e, r in zip(es, rs))


def _entry_covers_dir(entry: str, rel: str) -> bool:
    """白名单条目是否可能覆盖目录 ``rel`` 下的文件（决定是否继续往下扫）。"""
    es = _split(entry)
    rs = _split(rel)
    if not es:
        return False
    if _has_glob(entry):
        if "/" not in str(entry).replace("\\", "/"):
            # 只按文件名匹配的条目：任意目录下都可能有命中项
            return True
        i = j = 0
        while j < len(rs):
            if i >= len(es):
                return False
            if es[i] == "**":
                return True
            if not _seg_match(es[i], rs[j]):
                return False
            i += 1
            j += 1
        return True
    # 普通条目：目录在条目之内，或条目在目录之内，都要继续扫
    return all(_seg_match(e, r) for e, r in zip(es, rs))


def _is_ignored_seg(seg: str) -> bool:
    s = str(seg or "").lower()
    return (str(seg).startswith(".") or s in _JUNK_NAMES
            or s.endswith(_TEMP_SUFFIXES))


def is_ignored(rel: str) -> bool:
    """路径是否含隐藏段 / 垃圾文件名 / 临时后缀（任何策略下都跳过）。"""
    return any(_is_ignored_seg(part) for part in _split(rel))


def path_allowed(policy: dict, rel: str) -> bool:
    """文件路径是否参与比较/下载（黑名单优先，白名单为空则全部排除）。"""
    rel_segs = _split(rel)
    if not rel_segs or is_ignored(rel):
        return False
    include = policy.get("include") or []
    exclude = policy.get("exclude") or []
    if not include:
        return False
    if any(entry_matches(e, rel) for e in exclude):
        return False
    return any(entry_matches(e, rel) for e in include)


def dir_allowed(policy: dict, rel: str) -> bool:
    """目录是否继续向下扫描（根目录传空串）。

    - 白名单为空 → 不扫任何内容；
    - 目录命中黑名单（含普通条目前缀）→ 整棵子树跳过；
    - 任一白名单条目可能覆盖该目录（目录在其内 / 条目在其内）→ 继续扫。
    """
    if rel and is_ignored(rel):
        return False
    include = policy.get("include") or []
    exclude = policy.get("exclude") or []
    if not include:
        return False
    if rel and any(entry_matches(e, rel) for e in exclude):
        return False
    return any(_entry_covers_dir(e, rel) for e in include)


# ---------------- 扫描 ----------------
def scan_remote(sftp: SFTPManager, files_dir: str, policy: dict,
                progress_cb=None, workers: int = DEFAULT_SCAN_WORKERS) -> dict[str, dict]:
    """按策略扫描服务器 ``files_dir``，返回 ``{相对路径: {size, mtime}}``。

    使用多条 SFTP 连接并行列目录（workers 条，受全局并发名额限制）：单个会话
    逐目录往返在文件多时很慢，并行后扫描时间通常能降到原来的 1/3 ~ 1/5。
    """
    policy = normalize_policy(policy)
    files_dir = str(files_dir or "")
    if not files_dir:
        raise ValueError("客户端文件目录未配置")
    if not sftp.exists(files_dir):
        raise ValueError(f"客户端文件目录不存在：{files_dir}")

    def on_scan(dirs: int, files: int):
        if progress_cb:
            progress_cb(0, 0, f"正在扫描服务器目录…（已读 {dirs} 个目录 / "
                              f"{files} 个文件）")

    rows = sftp.list_files_recursive_with_meta(
        files_dir,
        max_workers=workers,
        progress_cb=on_scan,
        dir_filter=lambda rel: dir_allowed(policy, rel),
        file_filter=lambda rel: path_allowed(policy, rel),
        use_cache=False)
    result = {rel: {"size": int(size or 0), "mtime": int(mtime or 0)}
              for rel, size, mtime in rows}
    if progress_cb:
        progress_cb(0, 0, f"服务器范围内共 {len(result)} 个文件")
    return result


def scan_local(local_dir: str, policy: dict, progress_cb=None) -> dict[str, dict]:
    """按策略扫描本地游戏目录，返回 ``{相对路径: {size, mtime}}``。"""
    policy = normalize_policy(policy)
    if not local_dir or not os.path.isdir(local_dir):
        raise ValueError("本地游戏目录不存在或未选择")
    result: dict[str, dict] = {}
    dirs = 0
    for root, dirnames, filenames in os.walk(local_dir):
        rel_root = os.path.relpath(root, local_dir)
        rel_root = "" if rel_root == "." else rel_root.replace(os.sep, "/")
        if rel_root:
            dirs += 1
        keep = []
        for name in dirnames:
            child = f"{rel_root}/{name}" if rel_root else name
            if dir_allowed(policy, child):
                keep.append(name)
        dirnames[:] = sorted(keep)
        for name in sorted(filenames):
            child = f"{rel_root}/{name}" if rel_root else name
            if not path_allowed(policy, child):
                continue
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                continue
            result[child] = {"size": int(st.st_size),
                             "mtime": int(st.st_mtime)}
        if progress_cb and dirs and dirs % 50 == 0:
            progress_cb(0, 0, f"已扫描本地 {dirs} 个目录、"
                              f"{len(result)} 个文件…")
    return result


# ---------------- 对比计划 ----------------
def plan_sync(sftp: SFTPManager, files_dir: str, local_dir: str, policy: dict,
              progress_cb=None, workers: int = 4) -> dict:
    """对比服务器与本地，生成「需要下载」的计划（不修改任何文件）。

    判定顺序：
    - 服务器有、本地没有 → ``new``；
    - 两侧都有但大小不同 → ``update``；
    - 大小相同 → 先比远程哈希（``check-file`` 扩展，一次流水线批量算）；
      服务器不支持远程哈希时退化为「远程修改时间明显更新」判断；
    - 本地有、服务器没有 → 只作为 ``extras`` 报告，**不删除**。

    返回：
    ``{scanned_at, policy, policy_signature, remote_files, local_files,
       downloads: [{rel, action, size}], extras: [{rel, size}], total_bytes,
       same, warnings}``
    """
    policy = normalize_policy(policy)
    if not policy["enabled"]:
        raise ValueError("服务端未启用自动同步")
    if not policy["include"]:
        raise ValueError("自动同步策略没有设置同步范围（白名单为空）")

    if progress_cb:
        progress_cb(0, 0, "正在扫描服务器文件…")
    remote = scan_remote(sftp, files_dir, policy, progress_cb, workers=workers)
    if progress_cb:
        progress_cb(0, 0, f"正在扫描本地文件（服务器范围内 {len(remote)} 个）…")
    local = scan_local(local_dir, policy, progress_cb)

    local_by_key: dict[str, tuple[str, dict]] = {}
    for rel, meta in sorted(local.items()):
        local_by_key.setdefault(rel.lower(), (rel, meta))

    downloads: list[dict] = []
    same_keys: list[tuple[str, dict, str, dict]] = []
    for rel, meta in sorted(remote.items()):
        local_hit = local_by_key.get(rel.lower())
        if local_hit is None:
            downloads.append({"rel": rel, "action": "new",
                              "size": meta["size"],
                              "mtime": meta["mtime"]})
        elif local_hit[1]["size"] != meta["size"]:
            downloads.append({"rel": rel, "action": "update",
                              "size": meta["size"],
                              "mtime": meta["mtime"]})
        else:
            same_keys.append((rel, meta, local_hit[0], local_hit[1]))

    warnings: list[str] = []
    seen_case: dict[str, str] = {}
    for rel in sorted(remote):
        key = rel.lower()
        if key in seen_case and seen_case[key] != rel:
            warnings.append(f"服务器上存在仅大小写不同的文件："
                            f"{seen_case[key]} / {rel}")
        else:
            seen_case[key] = rel

    if same_keys:
        if progress_cb:
            progress_cb(0, len(same_keys), "对比大小相同的文件内容…")
        remote_paths = {rel: SFTPManager.join(files_dir, rel)
                        for rel, _meta, _lrel, _lmeta in same_keys}

        def hash_progress(done: int, total: int, rel: str):
            if progress_cb:
                name = str(rel).rsplit("/", 1)[-1]
                progress_cb(done, total,
                            f"对比文件内容 {done}/{total}：{name}")

        # 多连接并行哈希（check-file 流水线；服务器不支持时各自回退下载计算），
        # 比单连接逐文件快得多
        hashes = hash_remote_parallel(
            sftp, sftp.host, sftp.port, sftp.username, sftp.password,
            [(rel, remote_paths[rel]) for rel, _m, _lr, _lm in same_keys],
            hash_progress, workers=workers)
        for idx, (rel, meta, local_rel, local_meta) in enumerate(same_keys, 1):
            remote_hash = hashes.get(rel)
            if remote_hash:
                try:
                    local_hash = hash_file_cached(
                        os.path.join(local_dir, *local_rel.split("/")))
                except OSError:
                    local_hash = None
                changed = local_hash != remote_hash
            else:
                # 服务器不支持 check-file：用修改时间判断（下载后本地 mtime 更新，
                # 因此正常情况下不会反复下载）
                changed = int(meta["mtime"]) > int(local_meta["mtime"]) + 2
            if changed:
                downloads.append({"rel": rel, "action": "update",
                                  "size": meta["size"],
                                  "mtime": meta["mtime"]})
            if progress_cb and idx % 20 == 0:
                progress_cb(idx, len(same_keys), f"对比 {rel}…")

    remote_keys = {rel.lower() for rel in remote}
    extras = sorted(
        ({"rel": rel, "size": meta["size"]}
         for rel, meta in local.items() if rel.lower() not in remote_keys),
        key=lambda item: item["rel"].lower())

    downloads.sort(key=lambda item: item["rel"].lower())
    total_bytes = sum(int(item["size"] or 0) for item in downloads)
    if progress_cb:
        progress_cb(0, 0, f"对比完成：需要下载 {len(downloads)} 个文件"
                          f"（{total_bytes / 1048576:.1f} MB）")
    return {
        "scanned_at": _now_iso(),
        "policy": policy,
        "policy_signature": policy_signature(policy),
        "remote_files": len(remote),
        "local_files": len(local),
        "downloads": downloads,
        "extras": extras,
        "total_bytes": total_bytes,
        "same": max(0, len(remote) - len(downloads)),
        "warnings": warnings,
    }


# ---------------- 执行下载 ----------------
def _local_target(game_dir: str, rel: str) -> str:
    """把相对路径落到游戏目录内；拒绝穿越与跨盘符。"""
    rel = safe_target(rel)
    root = os.path.abspath(game_dir)
    target = os.path.abspath(os.path.join(root, *rel.split("/")))
    try:
        common = os.path.commonpath([root, target])
    except ValueError as exc:
        raise ValueError("目标路径不合法（不在游戏目录内）") from exc
    if os.path.normcase(common) != os.path.normcase(root):
        raise ValueError("目标路径超出游戏目录")
    return target


def apply_sync(sftp: SFTPManager, files_dir: str, local_dir: str,
               downloads: list[dict], progress_cb=None) -> dict:
    """把计划中的文件逐个下载到本地游戏目录（先下到临时文件再原子替换）。

    单个文件失败只记入 ``errors``，不中断其余文件；本地已有文件在下载成功前
    不会被覆盖，因此网络中断不会留下半个文件。
    """
    summary = {"installed": [], "errors": [], "bytes": 0}
    total = len(downloads or [])
    if not total:
        return summary
    with tempfile.TemporaryDirectory(prefix="mc_auto_sync_") as tmp:
        for idx, item in enumerate(downloads, 1):
            rel = str(item.get("rel") or "")
            try:
                target_rel = safe_target(rel)
                remote = SFTPManager.join(files_dir, target_rel)
                target = _local_target(local_dir, target_rel)
                local_tmp = os.path.join(tmp, f"f{idx}")
                sftp.download(remote, local_tmp)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.replace(local_tmp, target)
                # 把本地 mtime 对齐到服务器 mtime：服务器不支持 check-file 时
                # 靠 mtime 判断更新，对齐后不会因两端时钟差反复下载同一文件。
                try:
                    mtime = int(item.get("mtime") or 0)
                    if mtime > 0:
                        os.utime(target, (mtime, mtime))
                except OSError:
                    pass
                summary["installed"].append(target_rel)
                try:
                    summary["bytes"] += os.path.getsize(target)
                except OSError:
                    pass
                log.info("自动同步下载: %s", target_rel)
            except Exception as exc:
                log.warning("自动同步下载失败 %s: %s", rel, exc)
                summary["errors"].append(f"{rel}（{exc}）")
            if progress_cb:
                progress_cb(idx, total, f"下载 {rel}")
    return summary
