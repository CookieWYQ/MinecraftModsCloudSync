# -*- coding: utf-8 -*-
"""模组运行环境分析：分别判定模组在「客户端侧」和「服务端侧」的安装要求。

**运行环境 ≠ 发给谁**，这是两个维度：
- 运行环境是模组的固有属性：客户端需装 / 可选 / 无需，服务端同理，两侧独立；
- 「发给谁」由运行环境 + 两个「可选也发」开关共同决定（见 env_target()）。

旧实现把两侧压成一个 client/server/both，会丢信息：「客户端可选 + 服务端需装」
被压成「仅服务端」，客户端就永远拿不到，玩家客户端之间无法保持一致。

判断依据（按优先级）：
1. jar 元数据（本地，最快）：
   - fabric.mod.json / quilt.mod.json 的 environment 字段（client / server / *）
   - mcmod.info 的 clientSideOnly / serverSideOnly（Forge 1.12 及更早）
   - Forge / NeoForge 的 mods.toml 不含运行环境字段 → 需参考百科
2. MC 百科（网络，可选）：抓取 class 页面的「运行环境」标注，如「客户端需装, 服务端可选」

两侧状态约定：required（需装）/ optional（可选）/ none（无需）/ unknown（未判定）。
元数据没写运行环境时两侧都是 unknown（元数据无标注 ≠ 确定双端）。

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

# ---------- 两侧安装要求 ----------
SIDE_REQUIRED = "required"
SIDE_OPTIONAL = "optional"
SIDE_NONE = "none"
SIDE_UNKNOWN = "unknown"

SIDE_LABELS = {
    SIDE_REQUIRED: "需装",
    SIDE_OPTIONAL: "可选",
    SIDE_NONE: "无需",
    SIDE_UNKNOWN: "未判定",
}


def side_label(state: str) -> str:
    """单个状态的显示文字（未知值原样返回，便于排查）。"""
    return SIDE_LABELS.get(state, state)


def side_summary(c_state: str, s_state: str) -> str:
    """两侧状态的紧凑摘要，如「客户端：需装 ｜ 服务端：可选」。"""
    return f"客户端：{side_label(c_state)} ｜ 服务端：{side_label(s_state)}"


def env_target(c_state: str, s_state: str, *,
               client_optional: bool = False,
               server_optional: bool = False) -> str:
    """两侧安装要求 + 两个「可选也发」开关 → 发送目标。

    返回 client / server / both / none / unknown：
    - 「需装」一律要发；
    - 「可选」默认不发，开关打开后按「需装」处理（腐竹想让玩家客户端内容一致时打开）；
    - 两侧都未判定 → unknown（交由腐竹判断，不瞎猜成双端）；
    - 两侧都不必发 → none。

    例：「客户端可选 + 服务端需装」在开关关闭时 → server（只发服务端），
    开关打开时 → both（客户端那份也发出去）。
    """
    if c_state == SIDE_UNKNOWN and s_state == SIDE_UNKNOWN:
        return "unknown"

    def need(state: str, optional_ok: bool) -> bool:
        return state == SIDE_REQUIRED or (state == SIDE_OPTIONAL and optional_ok)

    c = need(c_state, client_optional)
    s = need(s_state, server_optional)
    if c and s:
        return "both"
    if c:
        return "client"
    if s:
        return "server"
    return "none"


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
        return SIDE_NONE
    if any(k in seg for k in _ENV_REQUIRED):
        return SIDE_REQUIRED
    if any(k in seg for k in _ENV_OPTIONAL):
        return SIDE_OPTIONAL
    return SIDE_UNKNOWN


def _parse_env_sides(text: str) -> tuple[str, str]:
    """解析百科「运行环境」标注文本 → (客户端状态, 服务端状态)。

    按「客户端 / 服务端」各自的安装要求分别判定，不把整段文本当成一个信号：
    例：客户端需装, 服务端可选 → (required, optional)
        客户端可选, 服务端需装 → (optional, required)
        双端需装             → (required, required)
    只提到一侧时另一侧保持 unknown（没写 ≠ 无需）。
    """
    t = text or ""
    if "双端" in t:
        return SIDE_REQUIRED, SIDE_REQUIRED
    c = s = SIDE_UNKNOWN
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
    return c, s


# ---------- 本地 jar 元数据 ----------
def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("true", "1", "yes")


def _env_from_fabric(zf: zipfile.ZipFile, entry: str) -> tuple[str, str, str]:
    """Fabric / Quilt 元数据的 environment 字段 → (客户端状态, 服务端状态, 依据)。

    environment 是「加载器在哪个侧加载这个模组」：client → 仅客户端加载
    （服务端无需装），server → 仅服务端加载，* → 两侧都加载。
    """
    try:
        data = json.loads(zf.read(entry).decode("utf-8", "ignore"))
    except Exception as exc:
        return SIDE_UNKNOWN, SIDE_UNKNOWN, f"{entry} 解析失败（{type(exc).__name__}）"
    nested = data.get("jars")
    tail = (f"（另含 {len(nested)} 个内嵌模组，未单独判定）"
            if isinstance(nested, list) and nested else "")
    env = data.get("environment")
    if env == "client":
        return SIDE_REQUIRED, SIDE_NONE, f"{entry} 标注 environment=client（仅客户端）"
    if env == "server":
        return SIDE_NONE, SIDE_REQUIRED, f"{entry} 标注 environment=server（仅服务端）"
    if env == "*":
        return SIDE_REQUIRED, SIDE_REQUIRED, f"{entry} 标注 environment=*（双端通用）"
    # 字段缺失：加载器规范按 * 加载，但「没标注」不等于「确定双端」，按未判定处理
    return SIDE_UNKNOWN, SIDE_UNKNOWN, f"{entry} 没有 environment 字段{tail}"


def _env_from_mcmod_info(raw: bytes) -> tuple[str, str, str] | None:
    """Forge 1.12 及更早的 mcmod.info：clientSideOnly / serverSideOnly → 两侧状态。

    支持 `[{...}]`、`{"modList": [...]}`、`{"modid": {...}}` 三种格式；
    没有任何一侧标记为 true 时返回 None，交由后续步骤判定。
    """
    try:
        data = json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return None
    if isinstance(data, list):
        entries = [e for e in data if isinstance(e, dict)]
    elif isinstance(data, dict):
        if isinstance(data.get("modList"), list):
            entries = [e for e in data["modList"] if isinstance(e, dict)]
        else:
            entries = [v for v in data.values() if isinstance(v, dict)]
    else:
        return None
    for e in entries:
        c = _truthy(e.get("clientSideOnly"))
        s = _truthy(e.get("serverSideOnly"))
        if c and not s:
            return SIDE_REQUIRED, SIDE_NONE, "mcmod.info 标注 clientSideOnly=true（仅客户端）"
        if s and not c:
            return SIDE_NONE, SIDE_REQUIRED, "mcmod.info 标注 serverSideOnly=true（仅服务端）"
    return None


def jar_environment(path: str) -> tuple[str, str, str]:
    """本地 jar 元数据分析运行环境 → (客户端状态, 服务端状态, 判断依据)。

    两侧状态 ∈ required / optional / none / unknown；依据为判断说明，供提示里展示。
    元数据没有运行环境字段时两侧都是 unknown（元数据无标注 ≠ 确定双端）。
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            for entry in ("fabric.mod.json", "quilt.mod.json"):
                if entry in names:
                    return _env_from_fabric(zf, entry)
            if "mcmod.info" in names:
                got = _env_from_mcmod_info(zf.read("mcmod.info"))
                if got:
                    return got
            if "META-INF/neoforge.mods.toml" in names:
                return SIDE_UNKNOWN, SIDE_UNKNOWN, "NeoForge 元数据不含运行环境字段"
            if "META-INF/mods.toml" in names or "mods.toml" in names:
                return SIDE_UNKNOWN, SIDE_UNKNOWN, "Forge 元数据不含运行环境字段"
            return SIDE_UNKNOWN, SIDE_UNKNOWN, "jar 内没有模组元数据"
    except zipfile.BadZipFile:
        return SIDE_UNKNOWN, SIDE_UNKNOWN, "不是有效的 jar / zip（文件可能已损坏）"
    except Exception as exc:
        log.debug("读取模组环境失败 %s: %s", path, exc)
        return SIDE_UNKNOWN, SIDE_UNKNOWN, f"读取失败：{type(exc).__name__}"


# ---------- MC 百科联网补齐 ----------
def mcmod_env_online(wiki_id: int, timeout: float = 10.0) -> tuple[str, str, str]:
    """抓取 MC 百科 class 页面的「运行环境」标注 → (客户端状态, 服务端状态, 依据)。

    失败 / 未标注 → 两侧 unknown + 原因。
    """
    try:
        html = _fetch(f"https://www.mcmod.cn/class/{int(wiki_id)}.html", timeout=timeout)
        m = re.search(r"运行环境\s*[:：]\s*([^<]{0,80})", html)
        if m:
            text = re.sub(r"\s+", " ", m.group(1)).strip()
            c, s = _parse_env_sides(text)
            return c, s, f"MC 百科标注：{text}"
    except Exception as exc:
        log.debug("抓取 MC 百科环境失败 wiki_id=%s: %s", wiki_id, exc)
        return SIDE_UNKNOWN, SIDE_UNKNOWN, f"MC 百科抓取失败（{type(exc).__name__}）"
    return SIDE_UNKNOWN, SIDE_UNKNOWN, "MC 百科未标注运行环境"


def enrich_online(names: list[str], *, enabled: bool = True,
                  timeout: float = ONLINE_TIMEOUT,
                  max_items: int = ONLINE_MAX_ITEMS,
                  per_request: float = ONLINE_PER_REQUEST,
                  workers: int = ONLINE_WORKERS) -> dict[str, tuple[str, str, str]]:
    """并行用 MC 百科补齐一批模组的运行环境 → {名称: (客户端状态, 服务端状态, 依据)}。

    三重保护，避免断网 / 百科不可用时卡住界面：
    - 条数上限 max_items，超出的直接返回「已跳过」；
    - 整体时间预算 timeout，超时后放弃未完成的查询；
    - 前 6 个查询全部是网络失败（没网 / 百科不可用）时提前放弃其余查询。
    enabled=False 表示用户关闭了联网补齐，全部直接返回未查询。
    """
    names = list(dict.fromkeys(n for n in names if n))
    if not enabled:
        return {n: (SIDE_UNKNOWN, SIDE_UNKNOWN, "已关闭联网查询（仅用本地元数据）")
                for n in names}
    head, rest = names[:max_items], names[max_items:]
    out: dict[str, tuple[str, str, str]] = {
        n: (SIDE_UNKNOWN, SIDE_UNKNOWN, "模组较多，已跳过联网查询") for n in rest}
    if not head:
        return out
    from .mcmod_db import wiki_id_for  # 延迟导入，避免顶层循环依赖

    def lookup_one(name: str):
        wid = wiki_id_for(name)
        if not wid:
            return SIDE_UNKNOWN, SIDE_UNKNOWN, "MC 百科数据库里没有这个模组"
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
                    out[name] = (SIDE_UNKNOWN, SIDE_UNKNOWN,
                                 f"查询失败（{type(exc).__name__}）")
                done += 1
                if out[name][2].startswith("MC 百科抓取失败"):
                    net_fail += 1
                if done >= 6 and net_fail == done:
                    log.info("MC 百科连续 %d 次抓取失败，判定为无网络，跳过其余查询", done)
                    break
        except FuturesTimeoutError:
            log.info("MC 百科查询整体超时（%.0fs），已跳过未完成的查询", timeout)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    for n in head:
        out.setdefault(n, (SIDE_UNKNOWN, SIDE_UNKNOWN, "MC 百科查询未完成（超时或已跳过）"))
    return out

