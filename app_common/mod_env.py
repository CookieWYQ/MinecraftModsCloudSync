# -*- coding: utf-8 -*-
"""模组运行环境分析：判断模组是客户端 / 服务端 / 双端。

判断依据（按优先级）：
1. jar 元数据（本地，最快）：
   - fabric.mod.json / quilt.mod.json 的 environment 字段（client / server / *）
   - Forge（mods.toml / mcmod.info）元数据不含运行环境字段 → 需参考百科
2. MC 百科（网络）：抓取 class 页面的「运行环境」标注，如「客户端需装, 服务端可选」
   （页面结构见 https://www.mcmod.cn/class/459.html 的运行环境行）

结果约定：client（客户端模组）/ server（服务端模组）/ both（双端）/ unknown（未标注，留用户判断）。
"""
import json
import re
import urllib.request
import zipfile

from .logger import get_logger

log = get_logger("mod_env")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _fetch(url: str, timeout: float = 10.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def _parse_env_text(text: str) -> str:
    """解析百科「运行环境」标注文本 → client / server / both / unknown。

    例：客户端需装 → client；服务端需装 → server；
        客户端需装, 服务端可选 → client；双端需装 → both。
    """
    t = text or ""
    if "双端" in t:
        return "both"  # 双端需装/双端通用
    client_need = any(k in t for k in ("客户端需装", "客户端必装", "客户端必须"))
    server_need = any(k in t for k in ("服务端需装", "服务端必装", "服务端必须"))
    if client_need and server_need:
        return "both"
    if client_need:
        return "client"
    if server_need:
        return "server"
    if "客户端" in t and "服务端" in t:
        return "both"
    if "客户端" in t:
        return "client"
    if "服务端" in t:
        return "server"
    return "unknown"


def jar_environment(path: str) -> tuple[str, str]:
    """本地 jar 元数据分析运行环境，返回 (env, source)。

    env ∈ client / server / both / unknown；source 为判断依据说明。
    fabric/quilt 的 environment 字段：client→客户端，server→服务端，*或缺省→双端通用。
    Forge（mods.toml / mcmod.info）无环境字段 → unknown（需百科补充）。
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            for entry in ("fabric.mod.json", "quilt.mod.json"):
                if entry in names:
                    try:
                        data = json.loads(zf.read(entry).decode("utf-8", "ignore"))
                    except Exception:
                        continue
                    env = data.get("environment", "*")
                    if env == "client":
                        return "client", f"{entry} 标注 environment=client"
                    if env == "server":
                        return "server", f"{entry} 标注 environment=server"
                    return "both", f"{entry} 标注 environment=*（双端通用）"
            return "unknown", "Forge 元数据不含运行环境字段"
    except Exception as exc:
        log.debug("读取模组环境失败 %s: %s", path, exc)
        return "unknown", f"读取失败：{type(exc).__name__}"


def mcmod_env_online(wiki_id: int) -> tuple[str, str]:
    """抓取 MC 百科 class 页面的「运行环境」标注，返回 (env, source)。失败/未标注 → unknown。"""
    try:
        html = _fetch(f"https://www.mcmod.cn/class/{int(wiki_id)}.html")
        m = re.search(r"运行环境\s*[:：]\s*([^<]{0,80})", html)
        if m:
            text = re.sub(r"\s+", " ", m.group(1)).strip()
            return _parse_env_text(text), f"MC 百科标注：{text}"
    except Exception as exc:
        log.debug("抓取 MC 百科环境失败 wiki_id=%s: %s", wiki_id, exc)
        return "unknown", f"MC 百科抓取失败（{type(exc).__name__}）"
    return "unknown", "MC 百科未标注运行环境"
