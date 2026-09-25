# -*- coding: utf-8 -*-
"""模组运行环境分析：判断模组是客户端 / 服务端 / 双端。

判断依据（按优先级）：
1. jar 元数据（本地，最快）：
   - fabric.mod.json / quilt.mod.json 的 environment 字段（client / server / *）
   - mcmod.info 的 clientSideOnly / serverSideOnly（Forge 1.12 及更早）
   - Forge / NeoForge 的 mods.toml 不含运行环境字段 → 需参考百科
2. MC 百科（网络，可选）：抓取 class 页面的「运行环境」标注，如「客户端需装, 服务端可选」

结果约定：client（客户端模组）/ server（服务端模组）/ both（双端）/ unknown（未标注，留用户判断）。
元数据没写运行环境时（Forge / NeoForge，或 fabric 没写 environment）返回 unknown，
不会按「双端」呈现——避免把「不知道」包装成「确定双端」。

联网补齐统一走 enrich_online()：带整体时间预算、条数上限，断网时提前放弃，
不会出现「点了发送后界面卡住十几分钟」。
"""
import json
import re
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError

from .logger import get_logger

log = get_logger("mod_env")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 百科查询批量上限 / 整体时间预算（断网时最多等这么久就放弃，避免卡住界面）
ONLINE_TIMEOUT = 45.0
ONLINE_MAX_ITEMS = 60
ONLINE_PER_REQUEST = 8.0
ONLINE_WORKERS = 4


def _fetch(url: str, timeout: float = 10.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


# ---------- 百科「运行环境」文本解析 ----------
# 先判「无需」再判「需装」：「无需装」里含「需装」，顺序不能反
_ENV_NONE = ("无需", "不需", "不用", "不需要", "不可装", "无效")
_ENV_REQUIRED = ("需装", "必装", "必须", "需要")
_ENV_OPTIONAL = ("可选", "可装", "可以装")


def _state_of(seg: str) -> str:
    """单个分句里某一侧的安装要求：required / optional / none / unknown。"""
    if any(k in seg for k in _ENV_NONE):
        return "none"
    if any(k in seg for k in _ENV_REQUIRED):
        return "required"
    if any(k in seg for k in _ENV_OPTIONAL):
        return "optional"
    return "unknown"


def _parse_env_text(text: str) -> str:
    """解析百科「运行环境」标注文本 → client / server / both / unknown。

    按「客户端 / 服务端」各自的安装要求分别判定，不再把整段文本当成一个信号。
    例：客户端需装, 服务端可选 → client（服务端只是「可选」，默认不放服务端；
        百科原文会显示在提示里，需要服务端也装时右键改「双端」）
    """
    t = text or ""
    if "双端" in t:
        return "both"  # 双端需装 / 双端通用
    c = s = "unknown"
    for seg in re.split(r"[,，、;；/|]+", t):
        seg = seg.strip()
        if not seg:
            continue
        state = _state_of(seg)
        has_c, has_s = "客户端" in seg, "服务端" in seg
        if has_c and has_s:
            c = s = state
        elif has_c:
            c = state
        elif has_s:
            s = state
    if c == "required" and s == "required":
        return "both"
    if c == "required":
        return "client"
    if s == "required":
        return "server"
    if c == "optional" and s == "optional":
        return "both"
    if c == "optional":
        return "client"
    if s == "optional":
        return "server"
    # 只提到一侧、且该侧状态不明 → 按该侧处理
    if c != "unknown" and s == "unknown":
        return "client"
    if s != "unknown" and c == "unknown":
        return "server"
    return "unknown"


# ---------- 本地 jar 元数据 ----------
def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("true", "1", "yes")


def _env_from_fabric(zf: zipfile.ZipFile, entry: str) -> tuple[str, str]:
    """Fabric / Quilt 元数据的 environment 字段 → (env, source)。"""
    try:
        data = json.loads(zf.read(entry).decode("utf-8", "ignore"))
    except Exception as exc:
        return "unknown", f"{entry} 解析失败（{type(exc).__name__}）"
    nested = data.get("jars")
    tail = (f"（另含 {len(nested)} 个内嵌模组，未单独判定）"
            if isinstance(nested, list) and nested else "")
    env = data.get("environment")
    if env == "client":
        return "client", f"{entry} 标注 environment=client"
    if env == "server":
        return "server", f"{entry} 标注 environment=server"
    if env == "*":
        return "both", f"{entry} 标注 environment=*（双端通用）"
    # 字段缺失：加载器规范按 * 加载，但「没标注」不等于「确定双端」，按未判定处理
    return "unknown", f"{entry} 没有 environment 字段{tail}"


def _env_from_mcmod_info(raw: bytes) -> tuple[str | None, str]:
    """Forge 1.12 及更早的 mcmod.info：clientSideOnly / serverSideOnly → (env, source)。

    支持 `[{...}]`、`{"modList": [...]}`、`{"modid": {...}}` 三种格式；
    没有任何一侧标记为 true 时返回 (None, "")，交由后续步骤判定。
    """
    try:
        data = json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return None, ""
    if isinstance(data, list):
        entries = [e for e in data if isinstance(e, dict)]
    elif isinstance(data, dict):
        if isinstance(data.get("modList"), list):
            entries = [e for e in data["modList"] if isinstance(e, dict)]
        else:
            entries = [v for v in data.values() if isinstance(v, dict)]
    else:
        return None, ""
    for e in entries:
        c = _truthy(e.get("clientSideOnly"))
        s = _truthy(e.get("serverSideOnly"))
        if c and not s:
            return "client", "mcmod.info 标注 clientSideOnly=true"
        if s and not c:
            return "server", "mcmod.info 标注 serverSideOnly=true"
    return None, ""


def jar_environment(path: str) -> tuple[str, str]:
    """本地 jar 元数据分析运行环境 → (env, source)。

    env ∈ client / server / both / unknown；source 为判断依据说明。
    元数据没有运行环境字段时返回 unknown（元数据无标注 ≠ 确定双端）。
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            for entry in ("fabric.mod.json", "quilt.mod.json"):
                if entry in names:
                    return _env_from_fabric(zf, entry)
            if "mcmod.info" in names:
                env, note = _env_from_mcmod_info(zf.read("mcmod.info"))
                if env:
                    return env, note
            if "META-INF/neoforge.mods.toml" in names:
                return "unknown", "NeoForge 元数据不含运行环境字段"
            if "META-INF/mods.toml" in names or "mods.toml" in names:
                return "unknown", "Forge 元数据不含运行环境字段"
            return "unknown", "jar 内没有模组元数据"
    except zipfile.BadZipFile:
        return "unknown", "不是有效的 jar / zip（文件可能已损坏）"
    except Exception as exc:
        log.debug("读取模组环境失败 %s: %s", path, exc)
        return "unknown", f"读取失败：{type(exc).__name__}"


# ---------- MC 百科联网补齐 ----------
def mcmod_env_online(wiki_id: int, timeout: float = 10.0) -> tuple[str, str]:
    """抓取 MC 百科 class 页面的「运行环境」标注 → (env, source)。

    失败 / 未标注 → ("unknown", 原因)。
    """
    try:
        html = _fetch(f"https://www.mcmod.cn/class/{int(wiki_id)}.html", timeout=timeout)
        m = re.search(r"运行环境\s*[:：]\s*([^<]{0,80})", html)
        if m:
            text = re.sub(r"\s+", " ", m.group(1)).strip()
            return _parse_env_text(text), f"MC 百科标注：{text}"
    except Exception as exc:
        log.debug("抓取 MC 百科环境失败 wiki_id=%s: %s", wiki_id, exc)
        return "unknown", f"MC 百科抓取失败（{type(exc).__name__}）"
    return "unknown", "MC 百科未标注运行环境"


def enrich_online(names: list[str], *, enabled: bool = True,
                  timeout: float = ONLINE_TIMEOUT,
                  max_items: int = ONLINE_MAX_ITEMS,
                  per_request: float = ONLINE_PER_REQUEST,
                  workers: int = ONLINE_WORKERS) -> dict[str, tuple[str, str]]:
    """并行用 MC 百科补齐一批模组的运行环境 → {名称: (env, source)}（查不到则为 unknown）。

    三重保护，避免断网 / 百科不可用时卡住界面：
    - 条数上限 max_items，超出的直接返回「已跳过」；
    - 整体时间预算 timeout，超时后放弃未完成的查询；
    - 前 6 个查询全部是网络失败（没网 / 百科不可用）时提前放弃其余查询。
    enabled=False 表示用户关闭了联网补齐，全部直接返回未查询。
    """
    names = list(dict.fromkeys(n for n in names if n))
    if not enabled:
        return {n: ("unknown", "已关闭联网查询（仅用本地元数据）") for n in names}
    head, rest = names[:max_items], names[max_items:]
    out: dict[str, tuple[str, str]] = {
        n: ("unknown", "模组较多，已跳过联网查询") for n in rest}
    if not head:
        return out
    from .mcmod_db import wiki_id_for  # 延迟导入，避免顶层循环依赖

    def lookup_one(name: str):
        wid = wiki_id_for(name)
        if not wid:
            return "unknown", "MC 百科数据库里没有这个模组"
        return mcmod_env_online(wid, timeout=per_request)

    pool = ThreadPoolExecutor(max_workers=workers)
    done = 0
    net_fail = 0
    try:
        futs = {pool.submit(lookup_one, n): n for n in head}
        try:
            for fut in as_completed(futs, timeout=timeout):
                name = futs[fut]
                try:
                    out[name] = fut.result()
                except Exception as exc:
                    out[name] = ("unknown", f"查询失败（{type(exc).__name__}）")
                done += 1
                if out[name][1].startswith("MC 百科抓取失败"):
                    net_fail += 1
                if done >= 6 and net_fail == done:
                    log.info("MC 百科连续 %d 次抓取失败，判定为无网络，跳过其余查询", done)
                    break
        except FuturesTimeoutError:
            log.info("MC 百科查询整体超时（%.0fs），已跳过未完成的查询", timeout)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    for n in head:
        out.setdefault(n, ("unknown", "MC 百科查询未完成（超时或已跳过）"))
    return out
