# -*- coding: utf-8 -*-
"""模组版本检查：从发布渠道查最新版本，判断本地文件是不是旧版本。

用途：更新模组时确认「这个文件到底该不该删」。差异审核里「旧版残留」默认勾选删除，
万一判定错（把已是最新的文件当成旧版残留删掉、或漏掉真正该删的旧版）就是删错模组，
所以这里额外给出「本地版本 vs 渠道最新版本」的对照。

**与运行环境无关**：运行环境（两侧是否需装）是模组固有属性，见 mod_env；
本模块只管「版本新旧」这一个维度。

渠道（都不需要用户填 key）：
- Modrinth 官方 API：每个版本都带 game_versions / loaders / version_type / 发布时间；
- CurseForge：走第三方只读接口 cfwidget（官方 API 需要 key）。

**不需要预先知道 MC 版本**：先在项目版本列表里定位「本地这一版」，
用它的 game_versions 圈定同一游戏版本的发布，再看其中有没有更新的。
这样不会拿别的 MC 版本的新版本误报「有新版本」。

结果状态：latest（已是最新）/ outdated（有新版本）/ ahead（本地更新）/
unknown（未查到或无法比较）。
"""
import json
import re
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError

from .logger import get_logger

log = get_logger("mod_version")

MODRINTH_API = "https://api.modrinth.com/v2"
CFWIDGET_API = "https://api.cfwidget.com/minecraft/mc-mods"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 与 mod_env.enrich_online 一致的三重保护参数：条数上限 / 整体预算 / 并发数
ONLINE_TIMEOUT = 30.0
ONLINE_MAX_ITEMS = 60
ONLINE_PER_REQUEST = 8.0
ONLINE_WORKERS = 4

VER_LATEST = "latest"
VER_OUTDATED = "outdated"
VER_AHEAD = "ahead"
VER_UNKNOWN = "unknown"

VER_LABELS = {
    VER_LATEST: "已是最新",
    VER_OUTDATED: "有新版本",
    VER_AHEAD: "本地更新",
    VER_UNKNOWN: "未查到",
}


# ---------- 版本串解析 / 比较 ----------
def parse_version(text) -> tuple[int, ...]:
    """版本串 → 可比较的数字元组：取最后一个「点分数字」段。

    取最后一段是为了跳过版本号里的 MC 版本前缀（1.20.1），并先剥掉 mcNN.N 标记：
      0.5.8                 → (0, 5, 8)
      mc1.20.1-0.5.13-fabric → (0, 5, 13)
      1.20.1-1.0.0          → (1, 0, 0)
      v2                    → (2,)
    解析不出数字 → ()（调用方据此判定「无法比较」）。
    """
    s = str(text or "").lower()
    s = re.sub(r"(?:^|[^0-9a-z])mc\s*\d+(?:\.\d+)*", " ", s)
    if s.endswith(".jar"):
        s = s[:-4]
    dotted = re.findall(r"\d+(?:\.\d+)+", s)
    if dotted:
        return tuple(int(x) for x in dotted[-1].split("."))
    ints = re.findall(r"\d+", s)
    return (int(ints[-1]),) if ints else ()


def _cmp_keys(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    """比较两个版本元组，长度不同时右侧补 0（1.0 与 1.0.0 视为同一版）。"""
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def compare_versions(a, b) -> int | None:
    """比较两个版本串：a 旧 → -1，相同 → 0，a 新 → 1；任一侧解析不出 → None。"""
    ta, tb = parse_version(a), parse_version(b)
    if not ta or not tb:
        return None
    return _cmp_keys(ta, tb)


def _clean_version(raw, game_versions=(), loaders=()) -> str:
    """剥掉渠道版本串里的 MC 版本与加载器标记，留下模组自身版本。

    Modrinth 的 version_number 形如 "mc1.20.1-0.5.13-fabric"，
    只按「最后一个点分段」解析并不可靠，这里用渠道给出的
    game_versions / loaders 精确剔除后再解析。
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
    return s.strip(" -_+.~")


def _version_of(raw, game_versions=(), loaders=()) -> str:
    """渠道版本串 → 可显示的模组版本号（剥掉 MC 版本/加载器后取数字段）。

    例："mc1.20.1-0.5.13-fabric"（gv=1.20.1，loader=fabric）→ "0.5.13"；
        "2.5.1+mc1.20.1" → "2.5.1"。
    """
    cleaned = _clean_version(raw, game_versions, loaders)
    cleaned = re.sub(r"\+?\s*mc\s*[\d.]*", " ", cleaned)  # 去掉残留的 "+mc1.20.1" 标记
    m = re.search(r"\d+(?:\.\d+)*[0-9a-z.+_-]*", cleaned)
    return (m.group(0).strip(" -_+.") if m else cleaned.strip()) or str(raw or "").strip()


# ---------- 本地 jar 版本 / 加载器 ----------
_META_ENTRIES = (
    ("fabric.mod.json", "fabric"),
    ("quilt.mod.json", "quilt"),
    ("META-INF/neoforge.mods.toml", "neoforge"),
    ("META-INF/mods.toml", "forge"),
    ("mcmod.info", "forge"),
)


def _json_version(raw: bytes) -> str:
    """fabric/Quilt 的 fabric.mod.json 或旧版 Forge 的 mcmod.info → version 字段。"""
    data = json.loads(raw.decode("utf-8", "ignore"))
    entries = data if isinstance(data, list) else (
        data.get("modList") if isinstance(data, dict) and isinstance(data.get("modList"), list)
        else [data])
    for e in entries:
        if isinstance(e, dict) and str(e.get("version") or "").strip():
            return str(e["version"]).strip()
    return ""


def _toml_version(raw: bytes) -> str:
    """mods.toml / neoforge.mods.toml：取第一个 version="..."（跳过 ${变量} 模板）。"""
    for m in re.finditer(rb'version\s*=\s*"([^"]*)"', raw):
        val = m.group(1).decode("utf-8", "ignore").strip()
        if val and not val.startswith("$"):
            return val
    return ""


def jar_meta(path: str) -> tuple[str, str]:
    """读本地 jar 的 (版本号, 加载器)；读不到返回 ("", "")。

    加载器用于 Modrinth 的 loaders 过滤（避免把 Forge 版当 NeoForge 版比较）。
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            for entry, loader in _META_ENTRIES:
                if entry not in names:
                    continue
                try:
                    raw = zf.read(entry)
                    version = _json_version(raw) if entry.endswith(".json") else _toml_version(raw)
                except Exception as exc:
                    log.debug("解析 %s 失败 %s: %s", entry, path, exc)
                    continue
                if version:
                    return version, loader
    except zipfile.BadZipFile:
        log.debug("不是有效的 jar/zip：%s", path)
    except Exception as exc:
        log.debug("读取模组版本失败 %s: %s", path, exc)
    return "", ""


# ---------- 渠道查询 ----------
def _http_json(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                              "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def _norm(text) -> str:
    return re.sub(r"[^0-9a-z]", "", str(text or "").lower())


def guess_game_version(text) -> str:
    """从文件名里猜 Minecraft 版本（如 journeymap-1.20.1-5.9.0.jar → "1.20.1"）。

    只作提示用：同一个模组版本号常同时存在于多个 MC 版本（如 sodium 的
    0.5.8 有 1.20.1 与 1.20.6 两份构建），有提示时优先按它挑，避免拿错游戏版本的版本。
    猜不出返回空串（此时退回原有判定）。
    """
    m = re.search(r"(?:^|[^0-9])1\.(\d{1,2})(?:\.(\d{1,2}))?(?![0-9])", str(text or ""))
    if not m:
        return ""
    return f"1.{m.group(1)}" + (f".{m.group(2)}" if m.group(2) else "")


def _unknown(local_version: str, note: str, source: str = "", url: str = "") -> dict:
    return {"status": VER_UNKNOWN, "latest": "", "local": local_version,
            "source": source, "url": url, "note": note}


def _status_of(local_version: str, latest_version: str) -> tuple[str, str]:
    """比较本地与渠道最新 → (状态, 说明)。任一解析不出 → unknown。"""
    lv, best = parse_version(local_version), parse_version(latest_version)
    if not lv:
        return VER_UNKNOWN, "本地 jar 未标注版本号，无法比较"
    if not best:
        return VER_UNKNOWN, "渠道版本号解析不出数字，无法比较"
    cmp = _cmp_keys(lv, best)
    if cmp == 0:
        return VER_LATEST, ""
    if cmp < 0:
        return VER_OUTDATED, ""
    return VER_AHEAD, "本地版本比渠道最新版本更高（自制/测试版？）"


def _modrinth_slug(slug: str, name: str, timeout: float) -> str:
    """定位 Modrinth 项目 slug：优先用百科库里的精确 slug，否则搜索后精确匹配。

    只在 slug 或标题与模组名**完全一致**时才采用，避免关联到同名/相似模组。
    """
    if slug:
        try:
            data = _http_json(f"{MODRINTH_API}/project/{urllib.parse.quote(slug)}", timeout)
            if data.get("slug"):
                return str(data["slug"])
        except Exception as exc:
            log.debug("Modrinth 项目查询失败 slug=%s: %s", slug, exc)
    target = _norm(name)
    if not target:
        return ""
    facets = urllib.parse.quote('[["project_type:mod"]]')
    data = _http_json(
        f"{MODRINTH_API}/search?query={urllib.parse.quote(name)}&limit=5&facets={facets}",
        timeout)
    for hit in data.get("hits") or ():
        if _norm(hit.get("slug")) == target or _norm(hit.get("title")) == target:
            return str(hit.get("slug") or "")
    return ""


def _modrinth_check(slug: str, name: str, local_version: str, loader: str,
                    game_version: str, timeout: float) -> dict | None:
    """Modrinth：定位项目 → 圈定本地版本所属的游戏版本 → 取其中最新发布。"""
    project = _modrinth_slug(slug, name, timeout)
    if not project:
        return None
    versions = _http_json(f"{MODRINTH_API}/project/{urllib.parse.quote(project)}/version",
                          timeout)
    if not isinstance(versions, list) or not versions:
        return None
    url = f"https://modrinth.com/mod/{project}"
    lv = parse_version(local_version)

    def game_versions_of(v) -> list[str]:
        return [str(g) for g in (v.get("game_versions") or ())]

    def matching(entries: list) -> list:
        """有游戏版本提示时，优先保留提示命中的条目。"""
        if not game_version:
            return entries
        return [v for v in entries if game_version in game_versions_of(v)]

    # 锚点：先按版本元组精确相等，再退化为「版本串包含本地版本」
    anchor_cands: list = []
    if lv:
        anchor_cands = [v for v in versions
                        if parse_version(v.get("version_number")) == lv]
        if not anchor_cands:
            local_tok = _norm(local_version)
            anchor_cands = [v for v in versions
                            if local_tok and local_tok in _norm(v.get("version_number"))]
    hinted = matching(anchor_cands)
    anchor = hinted[0] if hinted else (anchor_cands[0] if anchor_cands else None)

    pool = versions
    if anchor is not None:
        same_gv = [v for v in versions
                   if set(v.get("game_versions") or ()) & set(anchor.get("game_versions") or ())]
        if same_gv:
            pool = same_gv
    else:
        pool = matching(versions) or versions
    if loader:
        same_loader = [v for v in pool
                       if loader in [str(x).lower() for x in (v.get("loaders") or ())]]
        if same_loader:
            pool = same_loader
    releases = [v for v in pool if v.get("version_type") == "release"]
    best = max(releases or pool, key=lambda v: str(v.get("date_published") or ""))
    latest = _version_of(best.get("version_number"), best.get("game_versions"),
                         best.get("loaders"))
    status, note = _status_of(local_version, latest)
    if not note:
        note = f"与本地同一游戏版本（{'、'.join(best.get('game_versions') or []) or '未标注'}）"
    return {"status": status, "latest": latest, "local": local_version,
            "source": "Modrinth", "url": url, "note": note}


def _curseforge_check(slug: str, local_version: str, game_version: str,
                      timeout: float) -> dict | None:
    """CurseForge（cfwidget）：按本地版本定位 MC 版本分组 → 取该分组内最新文件。

    定位不到本地这一版就不猜（避免拿别的 MC 版本的文件误报「有新版本」）。
    有游戏版本提示时优先在提示对应的分组里找。
    """
    if not slug:
        return None
    data = _http_json(f"{CFWIDGET_API}/{urllib.parse.quote(slug)}", timeout)
    groups = data.get("versions") or {}
    if not isinstance(groups, dict) or not groups:
        return None
    url = f"https://www.curseforge.com/minecraft/mc-mods/{slug}"
    lv = parse_version(local_version)
    bucket = ""
    if lv:
        keys = [k for k in groups if game_version and k == game_version]
        keys += [k for k in groups if k not in keys]
        for key in keys:
            files = groups.get(key) or ()
            if any(parse_version(f.get("name")) == lv for f in files if isinstance(f, dict)):
                bucket = key
                break
    if not bucket:
        return _unknown(local_version,
                        "CurseForge 文件列表里找不到本地这一版，无法确定对应的游戏版本",
                        "CurseForge", url)
    files = [f for f in groups.get(bucket) or () if isinstance(f, dict)]
    if not files:
        return _unknown(local_version, "CurseForge 该游戏版本下没有文件", "CurseForge", url)
    releases = [f for f in files if f.get("type") == "release"]
    best = max(releases or files, key=lambda f: str(f.get("uploaded_at") or ""))
    latest = _version_of(best.get("name"), [bucket])
    status, note = _status_of(local_version, latest)
    if not note:
        note = f"CurseForge 游戏版本分组：{bucket}"
    return {"status": status, "latest": latest, "local": local_version,
            "source": "CurseForge", "url": url, "note": note}


def check_latest(name: str, local_version: str, *, loader: str = "",
                 game_version: str = "", slug: str = "", cf_slug: str = "",
                 timeout: float = ONLINE_PER_REQUEST) -> dict:
    """查单个模组的最新版本 → {status, latest, local, source, url, note}。

    game_version 是可选的 MC 版本提示（见 guess_game_version），用于消歧；
    依次尝试 Modrinth → CurseForge，任一给出可比较的结果即返回；
    都拿不到就返回 status=unknown 的结果（不抛异常）。
    """
    for source, call in (
            ("Modrinth", lambda: _modrinth_check(slug, name, local_version, loader,
                                                 game_version, timeout)),
            ("CurseForge", lambda: _curseforge_check(cf_slug, local_version,
                                                     game_version, timeout))):
        try:
            got = call()
        except Exception as exc:
            log.debug("%s 查询失败 %s: %s", source, name or slug, exc)
            continue
        if not got:
            continue
        if got.get("status") != VER_UNKNOWN:
            return got
    return _unknown(local_version, "未在 Modrinth / CurseForge 找到可比较的版本")


def check_versions(items, *, enabled: bool = True,
                   timeout: float = ONLINE_TIMEOUT,
                   max_items: int = ONLINE_MAX_ITEMS,
                   per_request: float = ONLINE_PER_REQUEST,
                   workers: int = ONLINE_WORKERS) -> dict[str, dict]:
    """并行检查一批模组的版本 → {key: info}。

    items 每项含 key（唯一标识）/ name（模组显示名）/ version（本地版本）/ loader。
    三重保护与 mod_env.enrich_online 相同：条数上限、整体时间预算、连续网络失败提前放弃，
    断网时不会把界面卡住。enabled=False → 全部返回「已关闭版本检查」。
    """
    items = [it for it in items if it.get("key")]
    if not items:
        return {}
    if not enabled:
        return {it["key"]: _unknown(it.get("version") or "", "已在设置里关闭模组版本检查")
                for it in items}
    head, rest = items[:max_items], items[max_items:]
    out = {it["key"]: _unknown(it.get("version") or "", "模组较多，已跳过版本检查")
           for it in rest}
    if not head:
        return out
    from .mcmod_db import slugs_for  # 延迟导入，避免顶层循环依赖

    def lookup_one(item: dict) -> dict:
        _, cf_slug, mr_slug = slugs_for(item.get("name") or item["key"])
        hint = item.get("game_version") or guess_game_version(item["key"])
        return check_latest(item.get("name") or "", item.get("version") or "",
                            loader=item.get("loader") or "", game_version=hint,
                            slug=mr_slug, cf_slug=cf_slug, timeout=per_request)

    pool = ThreadPoolExecutor(max_workers=workers)
    done = failed = 0
    try:
        futs = {pool.submit(lookup_one, it): it for it in head}
        try:
            for fut in as_completed(futs, timeout=timeout):
                it = futs[fut]
                try:
                    out[it["key"]] = fut.result()
                except Exception as exc:
                    out[it["key"]] = _unknown(it.get("version") or "",
                                              f"查询失败（{type(exc).__name__}）")
                done += 1
                if out[it["key"]].get("status") == VER_UNKNOWN:
                    failed += 1
                if done >= 6 and failed == done:
                    log.info("版本查询连续 %d 次无果，判定为网络不可用，跳过其余查询", done)
                    break
        except FuturesTimeoutError:
            log.info("版本查询整体超时（%.0fs），已跳过未完成的查询", timeout)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    for it in head:
        out.setdefault(it["key"], _unknown(it.get("version") or "",
                                           "查询未完成（超时或已跳过）"))
    return out


# ---------- 界面展示用文案（放在这里便于离线单测） ----------
def status_text(info: dict) -> str:
    """「最新版本」列的紧凑文本：状态（有新版本时附上渠道版本号）。"""
    status = (info or {}).get("status", VER_UNKNOWN)
    text = VER_LABELS.get(status, status)
    if status == VER_OUTDATED and (info or {}).get("latest"):
        text += f" {info['latest']}"
    return text


def tooltip_text(info: dict) -> str:
    """「最新版本」列的悬停说明：本地版本 / 渠道最新 / 依据与链接。"""
    info = info or {}
    lines = [f"版本检查（{info.get('source') or '无可用渠道'}）",
             f"本地版本：{info.get('local') or '未标注'}",
             f"渠道最新：{info.get('latest') or '—'}"]
    if info.get("note"):
        lines.append(info["note"])
    if info.get("url"):
        lines.append(info["url"])
    return "\n".join(lines)
