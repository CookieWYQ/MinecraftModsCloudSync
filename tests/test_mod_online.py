# -*- coding: utf-8 -*-
"""联网核对模组身份（Modrinth / CurseForge 文件列表）离线回归。

用注入的 fetcher 模拟渠道 API，验证：
1. 项目文件列表解析（Modrinth / CurseForge 两种数据）；
2. 判定：服务端旧版 / 服务端更新 / 同版本 / 未收录；
3. 服务端文件被手动改名（少了 -neoforge / -mc1.21.1）时，能在同一项目内模糊匹配；
4. 「联网发现」文件名归一化漏掉的旧版（服务端独有但属于本地已有项目）；
5. 缓存：第二次核对零请求；网络不可用时不抛异常、只标未收录。

运行：.venv/Scripts/python.exe tests/test_mod_online.py
"""
import json
import os
import sys
import tempfile
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app_common import mod_online  # noqa: E402

TMP = tempfile.mkdtemp(prefix="mc_online_test_")
CACHE = os.path.join(TMP, "cache.json")

MR = mod_online.MODRINTH_API
CF = mod_online.CFWIDGET_API


def _ver(number, gv, loader, date, filename):
    return {"version_number": number, "game_versions": [gv], "loaders": [loader],
            "date_published": date, "files": [{"filename": filename}]}


COPYCATS = {
    "slug": "copycats", "title": "Create: Copycats+",
    "versions": [
        _ver("3.0.11+mc.1.21.1-neoforge", "1.21.1", "neoforge",
             "2026-11-08T04:06:32Z", "copycats-3.0.11+mc.1.21.1-neoforge.jar"),
        _ver("3.0.9+mc.1.21.1-neoforge", "1.21.1", "neoforge",
             "2026-09-06T13:37:40Z", "copycats-3.0.9+mc.1.21.1-neoforge.jar"),
    ],
}

FLUID = {
    "slug": "createfluidlogistic", "title": "Create: FluidLogistics",
    "versions": [
        _ver("1.3.2", "1.21.1", "neoforge", "2026-10-01T00:00:00Z",
             "fluidlogistics-1.3.2-mc1.21.1.jar"),
        _ver("1.3.2", "1.20.1", "forge", "2026-10-01T00:00:00Z",
             "fluidlogistics-1.3.2-mc1.20.1.jar"),
        _ver("1.3.0", "1.21.1", "neoforge", "2026-08-01T00:00:00Z",
             "fluidlogistics-1.3.0-mc1.21.1.jar"),
        _ver("1.3.0", "1.20.1", "forge", "2026-08-01T00:00:00Z",
             "fluidlogistics-1.3.0-mc1.20.1.jar"),
    ],
}

PACKSLUGS = {
    "Create: Copycats+": (1, "", "copycats"),
    "Create FluidLogistics": (2, "", ""),  # 百科没有 Modrinth slug，走搜索
}


def fake_fetcher(url, timeout):
    path = urllib.parse.urlparse(url).path
    if "/project/" in path and path.endswith("/version"):
        slug = path.split("/project/", 1)[1].split("/", 1)[0]
        for proj in (COPYCATS, FLUID):
            if proj["slug"] == slug:
                return proj["versions"]
        return []
    if "/search?" in url:
        query = urllib.parse.unquote(
            urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["query"][0])
        hits = []
        if "copycats" in query.lower():
            hits.append({"slug": "copycats", "title": "Create: Copycats+"})
        if "fluid" in query.lower():
            hits.append({"slug": "createfluidlogistic", "title": "Create: FluidLogistics"})
        return {"hits": hits}
    raise AssertionError(f"意外的网络请求：{url}")


def install_stubs():
    """MC 百科 slug 查询打桩：不依赖真实百科库。"""
    import app_common.mcmod_db as db
    db.slugs_for = lambda name: PACKSLUGS.get(name, (None, "", ""))


install_stubs()

local_items = [
    {"file": "copycats-3.0.11+mc.1.21.1-neoforge.jar", "name": "Create: Copycats+",
     "modid": "copycats", "version": "3.0.11", "loader": "neoforge"},
    {"file": "fluidlogistics-1.3.2-mc1.21.1.jar", "name": "Create FluidLogistics",
     "modid": "fluidlogistics", "version": "1.3.2", "loader": "neoforge"},
]
server_names = [
    "copycats-3.0.9+mc.1.21.1-neoforge.jar",   # 精确命中项目文件列表
    "fluidlogistics-1.3.0.jar",                # 被手动改名（少了 -mc1.21.1）
    "unknown-mod-1.0.jar",                     # 渠道未收录
]

res = mod_online.verify(local_items, server_names, cache_path=CACHE,
                        fetcher=fake_fetcher, ttl_days=7)
ms = res["matches"]
assert ms["copycats-3.0.9+mc.1.21.1-neoforge"]["verdict"] == mod_online.OLDER, ms
assert ms["fluidlogistics-1.3.0"]["verdict"] == mod_online.OLDER, ms
assert ms["fluidlogistics-1.3.0"].get("renamed_match") is True, ms["fluidlogistics-1.3.0"]
assert ms["unknown-mod-1.0"]["verdict"] == mod_online.UNKNOWN, ms
assert res["stats"]["local_ok"] == 2 and res["stats"]["older"] == 2, res["stats"]
print("判定 OK：旧版 2（含手动改名 1）、未收录 1")

# 服务端反而更新的情况：不能被当成旧版删除
newer = mod_online.verify(
    [{"file": "copycats-3.0.9+mc.1.21.1-neoforge.jar", "name": "Create: Copycats+",
      "modid": "copycats", "version": "3.0.9", "loader": "neoforge"}],
    ["copycats-3.0.11+mc.1.21.1-neoforge.jar"],
    cache_path=os.path.join(TMP, "cache_newer.json"), fetcher=fake_fetcher)
assert newer["matches"]["copycats-3.0.11+mc.1.21.1-neoforge"]["verdict"] == mod_online.NEWER, newer
print("服端更新判定 OK（删除会导致降级）")

# 同一版本 / 无法比较 → same，不当作旧版
same = mod_online.verify(
    [{"file": "fluidlogistics-1.3.2-mc1.21.1.jar", "name": "Create FluidLogistics",
      "modid": "fluidlogistics", "version": "1.3.2", "loader": "neoforge"}],
    ["fluidlogistics-1.3.2-mc1.21.1.jar"],
    cache_path=os.path.join(TMP, "cache_same.json"), fetcher=fake_fetcher)
assert same["matches"]["fluidlogistics-1.3.2-mc1.21.1"]["verdict"] == mod_online.SAME, same
print("同版本判定 OK")

# 联网发现：服务端独有文件其实是本地已有项目的旧版
disc = mod_online.verify(
    local_items, [],
    discover=[{"rel": "mods/copycats-3.0.9+mc.1.21.1-neoforge.jar",
               "name": "copycats-3.0.9+mc.1.21.1-neoforge.jar"}],
    cache_path=os.path.join(TMP, "cache_disc.json"), fetcher=fake_fetcher)
assert len(disc["discovered"]) == 1, disc
assert disc["discovered"][0]["verdict"]["verdict"] == mod_online.OLDER, disc["discovered"][0]
print("联网发现 OK：文件名规则漏掉的旧版也能找到")

# 缓存：第二次核对不应再发请求
def no_network(url, timeout):
    raise AssertionError(f"缓存命中时不应联网：{url}")


again = mod_online.verify(local_items, server_names, cache_path=CACHE,
                          fetcher=no_network)
assert again["stats"]["requests"] == 0, again["stats"]
assert again["stats"]["cache_hits"] >= 2, again["stats"]
assert again["matches"]["fluidlogistics-1.3.0"]["verdict"] == mod_online.OLDER
print("缓存 OK：第二次核对零请求")

# 断网：不抛异常，结果退化为未收录
def offline(url, timeout):
    raise OSError("no network")


off = mod_online.verify(local_items, server_names,
                        cache_path=os.path.join(TMP, "cache_off.json"),
                        fetcher=offline, timeout=5)
assert off["matches"]["copycats-3.0.9+mc.1.21.1-neoforge"]["verdict"] == mod_online.UNKNOWN
assert off["stats"]["requests"] == 0, off["stats"]
assert "网络" in (off["note"] or ""), off["note"]
assert off["resolved"] == {}
print("断网降级 OK：只标未收录，不影响原有勾选")

# 解析辅助函数
assert mod_online._file_stem("[机械动力：伪装方块+] copycats-3.0.11+mc.1.21.1-neoforge.jar") \
    == "copycats-3.0.11+mc.1.21.1-neoforge"
assert mod_online._loose_stem("createnuclear-1.3.2-beta.3.jar") == \
    mod_online._loose_stem("createnuclear-1.3.2-beta.3-neoforge.jar")
print("文件名归一 OK")

print("全部通过")
