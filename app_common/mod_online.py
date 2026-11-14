# -*- coding: utf-8 -*-
"""联网核对模组身份：用 Modrinth / CurseForge 的**项目文件列表**校验旧版判定。

为什么还需要联网
----------------
`mod_identity.mod_key` 只依赖文件名归一化，仍有三类问题：
1. 作者换名 / 仓库里上传的文件名完全变样时，归一化对不上 → 旧版漏检；
2. 两个不同模组的文件名基名恰好相同 → 可能误判成同一模组，删错文件；
3. 只能看出「文件名不同」，看不出服务端那份其实**比本地更新**（例如服务端 2.1.6、
   本地还是 1.2.4），直接删掉反而是降级。

本模块把本地模组的显示名 / modid / MC 百科里的 slug 解析到发布渠道项目，取回该项目
**全部版本的文件名列表**，于是可以：
- 用「服务端文件名是否精确出现在项目文件列表里」确认它确实是这个模组；
- 用发布时间（优先）或版本号比较新旧，区分「旧版」与「服务端更新」；
- 发现文件名归一化漏掉的旧版（渠道文件列表里有、且本地已有同一项目）。

渠道（都不需要用户填 key）
--------------------------
- Modrinth 官方 API：`/project/{slug}/version` 返回所有版本及每个版本的文件名；
- CurseForge：走 cfwidget 只读接口（官方 API 需要 key），作为 Modrinth 查不到时的补充。

不联网也能用
------------
任何网络异常都只是让结果退化为「未核对」，绝不会把文件自动标成可删除；
`verify()` 有整体时间预算和条数上限，界面侧在后台线程调用，不会卡住。

缓存
----
项目文件列表按项目缓存，默认 7 天（`config_dir()/mod_online_cache.json`）。
同一批模组第二次核对基本零请求；缓存的 `local` 映射失效后会自动重查。
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from .constants import config_dir
from .logger import get_logger
from .mod_identity import strip_file_suffixes
from .mod_version import compare_versions

log = get_logger("mod_online")

MODRINTH_API = "https://api.modrinth.com/v2"
CFWIDGET_API = "https://api.cfwidget.com/minecraft/mc-mods"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 与 mod_env / mod_version 一致的「三重保护」：条数上限 / 整体预算 / 并发数
DEFAULT_TIMEOUT = 240.0
DEFAULT_MAX_ITEMS = 400
DEFAULT_WORKERS = 8
PER_REQUEST_TIMEOUT = 8.0
CACHE_TTL_DAYS = 7
CACHE_NAME = "mod_online_cache.json"
CACHE_VERSION = 2

# 结果判定
OLDER = "older"       # 服务端文件比本地旧 → 确认可删
NEWER = "newer"       # 服务端文件比本地新 → 不要删（删了是降级）
SAME = "same"         # 同一项目，版本相同 / 无法比较
UNKNOWN = "unknown"   # 渠道文件列表里没有这个文件名（无法核对）

VERDICT_LABELS = {
    OLDER: "确认旧版",
    NEWER: "服端更新",
    SAME: "同项目",
    UNKNOWN: "未收录",
}

_LOADER_RE = re.compile(r"^(?:neoforge|forge|fabric|quilt)$", re.IGNORECASE)


class _Abort(Exception):
    """整体超时 / 网络不可用：提前结束，保留已完成的结果。"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _fetch_json(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


# 有些整合包/启动器会给 jar 加中文名前缀：`[机械动力：伪装方块+] copycats-3.0.11.jar`。
# 这类前缀不在发布渠道的文件名里，比较前先剥掉。
_NAME_PREFIX_RE = re.compile(r"^\s*[\[【][^\]】]{1,80}[\]】]\s*")


def _strip_name_prefix(name: str) -> str:
    return _NAME_PREFIX_RE.sub("", name or "", count=1)


def _file_stem(name: str) -> str:
    """比较用的文件名键：剥掉中文名前缀与 .jar/.disabled 等后缀，小写。"""
    return strip_file_suffixes(_strip_name_prefix(name)).lower()


def _norm_name(text: str) -> str:
    """名称归一化：只留字母数字并小写（中文/标点会被去掉）。"""
    return re.sub(r"[^0-9a-z]", "", str(text or "").lower())


def _plausible(idx: dict, name: str, modid: str, filename: str) -> bool:
    """渠道项目是否「像」这个本地模组（用于接受 weak 关联，避免关联到搜索误命中）。

    只在项目标题/slug 与 modid、显示名、文件名主干之一相等（或 slug 以 modid 结尾）时接受。
    """
    keys = {_norm_name(modid), _norm_name(name), _norm_name(_name_hint(filename))}
    keys.discard("")
    if not keys:
        return False
    cands = {_norm_name(idx.get("project", "")), _norm_name(idx.get("title", ""))}
    cands.discard("")
    for c in cands:
        if c in keys:
            return True
        # 项目 slug 常带作者/系列前缀（create-fluidlogistics），允许「项目以模组名结尾」，
        # 反向不成立：短 slug（create）不能把长模组名（createfluidlogistics）吞掉。
        for k in keys:
            if len(k) >= 5 and c.endswith(k):
                return True
    return False


def _display_version(raw, game_versions=(), loaders=()) -> str:
    """渠道版本串 → 模组自身版本号（剥掉 MC 版本 / 加载器标记后取数字段）。

    与 mod_version._version_of 同样的思路，放在这里避免依赖私有函数：
      "3.0.11+mc.1.21.1-neoforge"（gv=1.21.1, loader=neoforge）→ "3.0.11"
    """
    s = str(raw or "").lower()
    for gv in game_versions or ():
        gv = str(gv).lower().strip()
        if gv:
            s = s.replace(gv, " ")
    for ld in loaders or ():
        ld = str(ld).lower().strip()
        if ld:
            s = re.sub(rf"(?<![0-9a-z]){re.escape(ld)}(?![0-9a-z])", " ", s)
    if s.endswith(".jar"):
        s = s[:-4]
    dotted = re.findall(r"\d+(?:\.\d+)+", s)
    if dotted:
        return dotted[-1]
    ints = re.findall(r"\d+", s)
    return ints[-1] if ints else str(raw or "").strip()


_LOADER_WORD_RE = re.compile(
    r"^(?:neoforge|forge|fabric|quilt|neo|mc(?:\d.*)?)$", re.IGNORECASE)


def _loose_stem(name: str) -> str:
    """再退一步的比较键：去掉加载器/游戏版本词后只留字母数字。

    createnuclear-1.3.2-beta.3.jar 与 createnuclear-1.3.2-beta.3-neoforge.jar
      → createnuclear132beta3
    用于「服务端文件被手动改名、少了加载器后缀」的情况（发布渠道不会收录这种名字）。
    """
    stem = _file_stem(name)
    # 先整体去掉 mc1.21.1 这类「MC 版本」标记：按标点切词会把它切成 mc1/21/1，
    # 单个词再判断加载器就漏掉了。
    stem = re.sub(r"(?<![0-9a-z])mc\d+(?:\.\d+)*", " ", stem)
    parts = [p for p in re.split(r"[-_.+ ]+", stem)
             if p and not _LOADER_WORD_RE.match(p)]
    return re.sub(r"[^0-9a-z]", "", "".join(parts))


def _name_hint(name: str) -> str:
    """文件名 → 模组名提示（供 Modrinth 搜索）：去掉版本段 / 加载器词。

    copycats-3.0.9+mc.1.21.1-neoforge.jar → "copycats"
    create-central-kitchen-2.6.2b.jar      → "create central kitchen"
    """
    stem = strip_file_suffixes(_strip_name_prefix(name))
    parts: list[str] = []
    for token in re.split(r"[-_.+ ]+", stem):
        if not token:
            continue
        if _LOADER_RE.match(token) or token.lower() in ("neo", "mc"):
            continue
        if re.match(r"^[vV]?\d", token) and parts:
            break
        parts.append(token)
    return " ".join(parts)


# ---------- 渠道 → 统一的项目文件索引 ----------

def _index_from_modrinth(slug: str, versions) -> dict:
    files: dict[str, dict] = {}
    for v in versions or ():
        if not isinstance(v, dict):
            continue
        gv = [str(x) for x in (v.get("game_versions") or ())]
        ld = [str(x).lower() for x in (v.get("loaders") or ())]
        disp = _display_version(v.get("version_number"), gv, ld)
        date = str(v.get("date_published") or "")
        for f in v.get("files") or ():
            fn = str((f or {}).get("filename") or "")
            if not fn:
                continue
            files[_file_stem(fn)] = {
                "file": fn, "ver": disp, "date": date,
                "gv": gv, "ld": ld,
            }
    return {"channel": "Modrinth", "project": slug, "title": slug,
            "url": f"https://modrinth.com/mod/{slug}", "files": files}


def _index_from_curseforge(slug: str, data: dict) -> dict:
    files: dict[str, dict] = {}
    groups = (data or {}).get("versions") or {}
    if isinstance(groups, dict):
        for bucket, arr in groups.items():
            if not isinstance(arr, list):
                continue
            for f in arr:
                if not isinstance(f, dict):
                    continue
                fn = str(f.get("name") or "")
                if not fn:
                    continue
                files[_file_stem(fn)] = {
                    "file": fn,
                    "ver": _display_version(fn, [str(bucket)]),
                    "date": str(f.get("uploaded_at") or ""),
                    "gv": [str(bucket)], "ld": [],
                }
    title = str((data or {}).get("title") or slug)
    return {"channel": "CurseForge", "project": slug, "title": title,
            "url": f"https://www.curseforge.com/minecraft/mc-mods/{slug}",
            "files": files}


def _loose_compatible(entry: dict, local_item: dict, idx: dict) -> bool:
    """模糊匹配候选是否与本地模组同一构建：加载器/游戏版本至少一项对得上。

    只在同一项目内使用；对不上就放弃（宁可不匹配也不误删别的构建）。
    """
    local_entry = (idx.get("files") or {}).get(_file_stem(local_item.get("file", "")))
    ld = {str(x).lower() for x in ((local_entry or {}).get("ld") or ())}
    gv = {str(x).lower() for x in ((local_entry or {}).get("gv") or ())}
    if not ld and local_item.get("loader"):
        ld = {str(local_item["loader"]).lower()}
    e_ld = {str(x).lower() for x in (entry.get("ld") or ())}
    e_gv = {str(x).lower() for x in (entry.get("gv") or ())}
    if ld and e_ld and not (ld & e_ld):
        return False
    if gv and e_gv and not (gv & e_gv):
        return False
    return True


class _Catalog:
    """一个核对任务内的渠道查询器：负责缓存、限速、失败提前放弃。"""

    def __init__(self, cache_path: str, *, ttl_days: float = CACHE_TTL_DAYS,
                 fetcher=None, timeout: float = DEFAULT_TIMEOUT,
                 max_items: int = DEFAULT_MAX_ITEMS, workers: int = DEFAULT_WORKERS):
        self.cache_path = cache_path
        self.ttl = max(0.0, float(ttl_days)) * 86400
        self._fetcher = fetcher
        self.deadline = time.monotonic() + max(1.0, float(timeout))
        self.max_items = max(1, int(max_items))
        self.workers = max(1, int(workers))
        self._cache = self._load_cache()
        self._lock = threading.Lock()
        self._throttle_lock = threading.Lock()
        self._last_request = 0.0
        self._fail_streak = 0
        self._requests = 0
        self._cache_hits = 0
        self.aborted = ""
        self.save_needed = False

    # ---- 缓存 ----
    def _load_cache(self) -> dict:
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("v") == CACHE_VERSION:
                return {"projects": data.get("projects") or {},
                        "local": data.get("local") or {},
                        "misses": data.get("misses") or {}}
        except (OSError, ValueError):
            pass
        return {"projects": {}, "local": {}, "misses": {}}

    def save(self) -> None:
        if not self.save_needed:
            return
        data = {"v": CACHE_VERSION, "saved_at": _now(),
                "projects": self._cache["projects"], "local": self._cache["local"],
                "misses": self._cache["misses"]}
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            tmp = f"{self.cache_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.cache_path)
        except OSError as exc:
            log.debug("写模组联网缓存失败：%s", exc)

    def _fresh(self, at: float) -> bool:
        return bool(at) and (time.time() - float(at)) < self.ttl

    def _cached_index(self, key: str):
        with self._lock:
            entry = self._cache["projects"].get(key)
        if not entry or not isinstance(entry.get("index"), dict):
            return None
        if not self._fresh(entry.get("at", 0)):
            return None
        idx = entry["index"]
        idx["stale"] = False
        self._cache_hits += 1
        return idx

    def _stale_index(self, key: str):
        """过期缓存：网络失败时兜底用（标 stale=True）。"""
        with self._lock:
            entry = self._cache["projects"].get(key)
        idx = (entry or {}).get("index")
        if isinstance(idx, dict):
            idx = dict(idx)
            idx["stale"] = True
            return idx
        return None

    def _store_index(self, idx: dict) -> None:
        # 缓存键要记在索引里：过滤查询的键带 :gv=/:ld= 后缀，本地映射必须复用它，
        # 否则每次都会当作「缓存里没有」重新联网。
        key = idx.get("cache_key") or f"{idx['channel'].lower()}:{idx['project']}"
        idx["cache_key"] = key
        with self._lock:
            self._cache["projects"][key] = {"at": time.time(), "index": idx}
            self.save_needed = True

    # ---- HTTP ----
    def _throttle(self) -> None:
        if self._fetcher is not None:
            return  # 测试注入的 fetcher 不限速
        with self._throttle_lock:
            wait = 0.26 - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def _json(self, url: str):
        if time.monotonic() > self.deadline:
            raise _Abort("整体时间预算已用完")
        self._throttle()
        fetch = self._fetcher or _fetch_json
        try:
            data = fetch(url, PER_REQUEST_TIMEOUT)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:  # 限流：按 Retry-After 等一次再试
                try:
                    delay = min(5.0, float(exc.headers.get("Retry-After") or 1.0))
                except (TypeError, ValueError):
                    delay = 1.0
                time.sleep(delay)
                data = fetch(url, PER_REQUEST_TIMEOUT)
            else:
                self._fail()
                raise
        except Exception:
            self._fail()
            raise
        self._requests += 1
        self._fail_streak = 0
        return data

    def _fail(self) -> None:
        self._fail_streak += 1
        if self._fail_streak >= 6 and self._requests == 0:
            self.aborted = "网络不可用，已停止联网核对"
            raise _Abort(self.aborted)

    # ---- 项目解析 ----
    def _modrinth(self, slug: str, gv: str = "", ld: str = ""):
        """取项目全部版本文件列表；给了 MC 版本/加载器时让服务端先过滤。

        过滤能显著缩小响应（大型模组 900 个版本时尤其明显），也少踩 429；
        缓存键带上过滤条件，避免把「只含部分版本」的索引当成完整列表复用。
        """
        gv = str(gv or "").strip()
        ld = str(ld or "").strip().lower()
        key = f"modrinth:{slug.lower()}"
        if gv:
            key += f":gv={gv}"
        if ld:
            key += f":ld={ld}"
        idx = self._cached_index(key)
        if idx is not None:
            return idx
        url = f"{MODRINTH_API}/project/{urllib.parse.quote(slug)}/version"
        params = []
        if gv:
            params.append(("game_versions", json.dumps([gv])))
        if ld:
            params.append(("loaders", json.dumps([ld])))
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            versions = self._json(url)
        except _Abort:
            raise
        except Exception as exc:
            log.debug("Modrinth 项目查询失败 %s: %s", slug, exc)
            return self._stale_index(key)
        idx = _index_from_modrinth(slug, versions)
        if idx["files"]:
            self._store_index(idx)
        return idx

    def _curseforge(self, slug: str):
        key = f"curseforge:{slug.lower()}"
        idx = self._cached_index(key)
        if idx is not None:
            return idx
        try:
            data = self._json(f"{CFWIDGET_API}/{urllib.parse.quote(slug)}")
        except _Abort:
            raise
        except Exception as exc:
            log.debug("CurseForge 项目查询失败 %s: %s", slug, exc)
            return self._stale_index(key)
        idx = _index_from_curseforge(slug, data)
        if idx["files"]:
            self._store_index(idx)
        return idx

    def search_modrinth(self, query: str, limit: int = 5) -> list[dict]:
        facets = urllib.parse.quote('[["project_type:mod"]]')
        url = (f"{MODRINTH_API}/search?query={urllib.parse.quote(query)}"
               f"&limit={limit}&facets={facets}")
        try:
            data = self._json(url)
        except _Abort:
            raise
        except Exception as exc:
            log.debug("Modrinth 搜索失败 %s: %s", query, exc)
            return []
        hits = data.get("hits") if isinstance(data, dict) else None
        return [h for h in (hits or []) if isinstance(h, dict)]

    # ---- 本地模组 → 项目 ----
    def resolve(self, item: dict):
        """本地模组文件 → (项目索引, 置信度)。查不到返回 (None, "")。

        置信度 exact：项目文件列表里精确存在本地这个文件名（身份基本无歧义）；
        weak：只按名称/百科 slug 关联上项目，本地文件不在渠道列表里（自制/改名构建）。
        """
        stem = _file_stem(item.get("file", ""))
        with self._lock:
            loc = self._cache["local"].get(stem)
            miss = self._cache["misses"].get(stem)
        if loc and self._fresh(loc.get("at", 0)):
            idx = self._cached_index(loc.get("key", ""))
            if idx is not None:
                return idx, loc.get("confidence", "weak")
        if miss and self._fresh(miss.get("at", 0)):
            return None, ""  # 上次查不到：负缓存期内不再重复浪费请求
        idx, conf = self._find(item, stem)
        if idx is None:
            with self._lock:
                self._cache["misses"][stem] = {"at": time.time()}
                self.save_needed = True
        if idx is not None:
            key = idx.get("cache_key") or (
                f"{idx['channel'].lower()}:{idx['project'].lower()}")
            with self._lock:
                self._cache["local"][stem] = {
                    "at": time.time(), "key": key,
                    "channel": idx["channel"], "project": idx["project"],
                    "confidence": conf,
                }
                self.save_needed = True
        return idx, conf

    def _find(self, item: dict, stem: str):
        """本地模组 → 项目索引：百科 slug（Modrinth → CurseForge）→ 搜索 → 兜底。

        很多国内整合包的模组只在 CurseForge 有完整文件列表，所以百科给了 CF slug 时
        先查 CF（一次请求即可覆盖全部版本），比 Modrinth 搜索更省请求也更准。
        """
        name = str(item.get("name") or "").strip()
        modid = str(item.get("modid") or "").strip()
        mr_slug = cf_slug = ""
        if name:
            try:
                from .mcmod_db import slugs_for  # 延迟导入，离线也可用
                _, cf_slug, mr_slug = slugs_for(name)
            except Exception as exc:  # 百科库缺失/损坏时不影响其它渠道
                log.debug("MC 百科 slug 查询失败 %s: %s", name, exc)
        tried: set[str] = set()
        fallback = None
        # 只用加载器过滤：从文件名猜 MC 版本经常把模组版本误当成游戏版本
        # （如 aeroworks-1.5.0 → 1.5），过滤后反而查不到本地文件；加载器来自 jar
        # 元数据更可靠，且能把 fabric/forge/neoforge 构建分开。
        ld = str(item.get("loader") or "").strip().lower()
        if ld in ("neo",):
            ld = "neoforge"

        def remember(idx):
            nonlocal fallback
            if idx is None or not idx.get("files"):
                return None
            if self._has_local(idx, stem):
                return idx, "exact"
            if fallback is None and _plausible(idx, name, modid, item.get("file", "")):
                fallback = (idx, "weak")
            return None

        if mr_slug:
            idx = self._modrinth(mr_slug, "", ld)
            got = remember(idx)
            if got:
                return got
            if idx is not None and ld and not idx.get("files"):
                # 加载器过滤后为空（项目没有这个加载器的构建？）→ 退回完整列表核对一次
                got = remember(self._modrinth(mr_slug))
                if got:
                    return got
            tried.add(f"modrinth:{mr_slug.lower()}")
        if cf_slug:
            got = remember(self._curseforge(cf_slug))
            if got:
                return got
            tried.add(f"curseforge:{cf_slug.lower()}")
        queries: list[str] = []
        for q in (modid, name, _name_hint(item.get("file", ""))):
            q = str(q or "").strip()
            if q and q not in queries:
                queries.append(q)
        for q in queries[:2]:
            hits = self.search_modrinth(q)
            # 标题/slug 与本地模组名相符的命中优先查；最多再试 3 个项目
            hits.sort(key=lambda h: 0 if _plausible(
                {"project": h.get("slug", ""), "title": h.get("title", "")},
                name, modid, item.get("file", "")) else 1)
            for h in hits[:3]:
                slug = str(h.get("slug") or "")
                if not slug or f"modrinth:{slug.lower()}" in tried:
                    continue
                tried.add(f"modrinth:{slug.lower()}")
                got = remember(self._modrinth(slug, "", ld))
                if got:
                    return got
            if fallback:
                break
        if fallback:
            return fallback
        return None, ""

    @staticmethod
    def _has_local(idx: dict, stem: str) -> bool:
        return stem in (idx.get("files") or {})

    def discover(self, server_name: str, local_items: list[dict], stem: str):
        """服务端独有文件 → 若它属于某个本地已有模组的项目，返回 (本地项, 索引, 文件项)。

        供「文件名归一化漏掉的旧版」发现用；本地模组集合里没有同项目文件就返回 None。
        """
        hint = _name_hint(server_name)
        if not hint:
            return None
        for hit in self.search_modrinth(hint, limit=3):
            slug = str(hit.get("slug") or "")
            if not slug:
                continue
            idx = self._modrinth(slug)
            if idx is None or stem not in idx["files"]:
                continue
            for item in local_items:
                if self._has_local(idx, _file_stem(item.get("file", ""))):
                    return item, idx, idx["files"][stem]
        return None


# ---------- 判定 ----------

def judge(server_entry: dict, local_item: dict, idx: dict) -> dict:
    """服务端文件项 vs 本地模组项 → 判定结果 dict（纯函数，便于单测）。"""
    local_stem = _file_stem(local_item.get("file", ""))
    local_entry = (idx.get("files") or {}).get(local_stem)
    server_ver = server_entry.get("ver") or ""
    local_ver = (local_entry or {}).get("ver") or local_item.get("version") or ""
    note = ""

    cmp = None
    server_date = str(server_entry.get("date") or "")
    local_date = str((local_entry or {}).get("date") or "")
    if local_entry and server_date and local_date and server_date != local_date:
        # 同一项目文件列表里两个文件都有发布时间：优先按时间，最可靠
        cmp = 1 if server_date > local_date else -1
        note = "按项目文件列表的发布时间判断"
    elif server_ver and local_ver:
        cmp = compare_versions(server_ver, local_ver)
        note = "按版本号判断" if cmp is not None else ""

    if local_entry and cmp is None:
        verdict = SAME
        note = note or "同一项目，无法比较新旧（视为同一版本）"
    elif not local_entry and cmp is None:
        verdict = SAME
        note = "渠道未收录本地这个文件（自制/改名构建），按项目相同处理"
    elif cmp < 0:
        verdict = OLDER
    elif cmp > 0:
        verdict = NEWER
        note = note or "服务端文件比本地新，删除会导致玩家降级，请确认"
    else:
        verdict = SAME

    return {
        "verdict": verdict,
        "channel": idx.get("channel", ""),
        "project": idx.get("project", ""),
        "title": idx.get("title", "") or idx.get("project", ""),
        "url": idx.get("url", ""),
        "server_version": server_ver,
        "local_version": local_ver,
        "server_date": server_date,
        "local_date": local_date,
        "local_file": local_item.get("file", ""),
        "note": note,
        "stale": bool(idx.get("stale")),
    }


# ---------- 批量核对入口 ----------

def verify(local_items: list[dict], server_names: list[str], *, discover=(),
           enabled: bool = True, cache_path: str = "", timeout: float = DEFAULT_TIMEOUT,
           max_items: int = DEFAULT_MAX_ITEMS, workers: int = DEFAULT_WORKERS,
           ttl_days: float = CACHE_TTL_DAYS, fetcher=None,
           progress_cb=None) -> dict:
    """批量核对服务端旧版文件。

    参数
    ----
    local_items: 本地模组项 [{file, name, modid, version, loader, game_version}]；
    server_names: 待核对的服务端文件名（通常是差异列表里的「旧版残留」）；
    discover: 额外的服务端独有文件 [{"rel": 相对路径, "name": 文件名}]，
              用于发现文件名归一化漏掉的旧版（找到同项目时给出发现结果）。
    fetcher: 可注入的 HTTP 函数 (url, timeout) -> 解析后的 JSON，便于离线测试。

    返回
    ----
    {"matches": {文件名键: 判定}, "discovered": [{rel, name, verdict}],
     "resolved": {本地文件: {channel, project, title, url, confidence}},
     "stats": {...}, "note": 整体说明}
    """
    out = {"matches": {}, "discovered": [], "resolved": {}, "stats": {}, "note": ""}
    if not enabled:
        out["note"] = "已关闭联网核对"
        return out
    local_items = [it for it in (local_items or [])
                   if it.get("file") and it.get("file", "").lower().endswith(
                       (".jar", ".jar.disabled"))]
    # 去重（同一文件只查一次；.disabled 与 .jar 并存时优先启用中的那份）
    uniq: dict[str, dict] = {}
    for it in local_items:
        stem = _file_stem(it["file"])
        old = uniq.get(stem)
        if old is None or (old["file"].lower().endswith(".disabled")
                           and not it["file"].lower().endswith(".disabled")):
            uniq[stem] = it
    local_items = list(uniq.values())[: max_items]
    stems = list(dict.fromkeys(_file_stem(n) for n in (server_names or ()) if n))

    cat = _Catalog(cache_path or os.path.join(str(config_dir()), CACHE_NAME),
                   ttl_days=ttl_days, fetcher=fetcher, timeout=timeout,
                   max_items=max_items, workers=workers)

    resolved: dict[str, tuple[dict, dict]] = {}  # stem -> (item, index)
    total = len(local_items)
    done = failed = 0
    try:
        pool = ThreadPoolExecutor(max_workers=cat.workers)
        try:
            futs = {pool.submit(cat.resolve, it): it for it in local_items}
            for fut in as_completed(futs):
                it = futs[fut]
                try:
                    idx, conf = fut.result()
                except _Abort:
                    idx, conf = None, ""
                except Exception as exc:
                    log.debug("模组身份核对失败 %s: %s", it.get("file"), exc)
                    idx, conf = None, ""
                done += 1
                if idx is None:
                    failed += 1
                else:
                    resolved[_file_stem(it["file"])] = (it, idx)
                if progress_cb:
                    progress_cb(done, total, f"联网核对 {it.get('file', '')}…")
                if done >= 6 and failed == done and cat.aborted:
                    break
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    except _Abort:
        pass
    finally:
        cat.save()

    # 1) 匹配差异列表里的旧版文件：先精确文件名，再按项目内模糊匹配（手动改名的情况）。
    #    模糊表只建一次（按 _loose_stem 倒排），避免「每个候选 × 每个项目的每个文件」重复算正则。
    stem_names: dict[str, str] = {}
    for n in (server_names or ()):
        if n:
            stem_names.setdefault(_file_stem(n), n)
    exact: dict[str, list[tuple[dict, dict, dict]]] = {}
    loose_map: dict[str, list[tuple[dict, dict, dict]]] = {}
    for it, idx in resolved.values():
        for s, entry in (idx.get("files") or {}).items():
            exact.setdefault(s, []).append((it, idx, entry))
            loose_map.setdefault(_loose_stem(s), []).append((it, idx, entry))
    for stem in stems:
        cands = exact.get(stem)
        if cands:
            # 同一文件同时出现在多个项目里时，优先「本地也存在同名文件」的那个
            cands.sort(key=lambda p: _file_stem(p[0].get("file", "")) != stem)
            it, idx, entry = cands[0]
            out["matches"][stem] = judge(entry, it, idx)
            continue
        server_name = stem_names.get(stem, stem)
        pool = loose_map.get(_loose_stem(server_name), ())
        hit = None
        for it, idx, entry in pool:
            if _loose_compatible(entry, it, idx):
                hit = (it, idx, entry)
                break
        if hit:
            it, idx, entry = hit
            out["matches"][stem] = judge(entry, it, idx)
            out["matches"][stem]["renamed_match"] = True
    # 没有出现在任何项目文件列表里的旧版 → 明确标「未核对」
    for stem in stems:
        out["matches"].setdefault(stem, {
            "verdict": UNKNOWN, "channel": "", "project": "", "title": "",
            "url": "", "server_version": "", "local_version": "",
            "server_date": "", "local_date": "", "local_file": "",
            "note": "发布渠道的项目文件列表里没有这个文件名（可能未上传/已删除/私有 mod）",
        })

    # 2) 发现归一化漏掉的旧版（服务端独有文件）
    if discover and not cat.aborted:
        local_items_only = [it for it, _ in resolved.values()]
        for d in list(discover)[:20]:
            rel = str(d.get("rel") or "")
            name = str(d.get("name") or rel.rsplit("/", 1)[-1])
            stem = _file_stem(name)
            if not rel or (stem in out["matches"]
                           and out["matches"][stem]["verdict"] != UNKNOWN):
                continue
            try:
                hit = cat.discover(name, local_items_only, stem)
            except _Abort:
                break
            except Exception as exc:
                log.debug("旧版发现失败 %s: %s", name, exc)
                continue
            if not hit:
                continue
            it, idx, entry = hit
            out["discovered"].append({"rel": rel, "name": name,
                                      "verdict": judge(entry, it, idx)})

    # 汇总
    verdicts = [m.get("verdict") for m in out["matches"].values()]
    out["stats"] = {
        "local_total": len(local_items),
        "local_ok": len(resolved),
        "local_fail": failed,
        "candidates": len(stems),
        "older": verdicts.count(OLDER),
        "newer": verdicts.count(NEWER),
        "same": verdicts.count(SAME),
        "unknown": verdicts.count(UNKNOWN),
        "discovered": len(out["discovered"]),
        "requests": cat._requests,
        "cache_hits": cat._cache_hits,
        "aborted": cat.aborted,
    }
    out["note"] = cat.aborted or ""
    if not cat.aborted and not resolved and stems:
        out["note"] = "没有查到任何本地模组的发布渠道项目（网络或通道不可用）"
    for stem, (it, idx) in resolved.items():
        out["resolved"][it["file"]] = {
            "channel": idx.get("channel", ""), "project": idx.get("project", ""),
            "title": idx.get("title", ""), "url": idx.get("url", ""),
            "confidence": "exact" if _file_stem(it["file"]) in (idx.get("files") or {})
                           else "weak",
        }
    log.info("联网核对：本地 %d（成功 %d）、候选 %d → 确认旧版 %d / 服端更新 %d / "
             "未收录 %d / 发现 %d，请求 %d 次（缓存命中 %d）",
             len(local_items), len(resolved), len(stems), out["stats"]["older"],
             out["stats"]["newer"], out["stats"]["unknown"],
             out["stats"]["discovered"], cat._requests, cat._cache_hits)
    return out


def verdict_label(verdict: dict) -> str:
    """「联网核对」列的紧凑文本。"""
    v = (verdict or {}).get("verdict", UNKNOWN)
    label = VERDICT_LABELS.get(v, v)
    if v == OLDER and verdict.get("server_version"):
        label += f" {verdict['server_version']}"
    return label


def verdict_tooltip(verdict: dict) -> str:
    """「联网核对」列的悬停说明：渠道项目、版本、发布时间、依据与链接。"""
    d = verdict or {}
    if d.get("verdict") == UNKNOWN:
        return (d.get("note") or "未在发布渠道的项目文件列表里找到这个文件")
    lines = [f"联网核对（{d.get('channel') or '未知渠道'}）",
             f"项目：{d.get('title') or d.get('project') or '—'}",
             f"服务端版本：{d.get('server_version') or '—'}"
             + (f"（{d['server_date'][:10]}）" if d.get("server_date") else ""),
             f"本地版本：{d.get('local_version') or '—'}"
             + (f"（{d['local_date'][:10]}）" if d.get("local_date") else ""),
             f"判定：{VERDICT_LABELS.get(d.get('verdict'), d.get('verdict'))}"]
    if d.get("local_file"):
        lines.append(f"对应本地文件：{d['local_file']}")
    if d.get("note"):
        lines.append(d["note"])
    if d.get("stale"):
        lines.append("（使用过期缓存）")
    if d.get("url"):
        lines.append(d["url"])
    return "\n".join(lines)
