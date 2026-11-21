# -*- coding: utf-8 -*-
"""模组网页链接：MC 百科 / Modrinth / CurseForge。

用途
----
服务端「文件管理（手动）」等页面里，右键一个模组 jar 就能直接打开它对应的网页：

- **MC 百科（mcmod.cn）**：命中本地 ``mcmod.buf`` 数据库 → 打开详情页
  （``/class/{wiki_id}.html``）；未命中 → 回退到百科搜索页；
- **Modrinth**：数据库里有 Modrinth Slug → 打开 ``modrinth.com/mod/{slug}``；
  没有 → 回退到 Modrinth 搜索页；
- **CurseForge**：数据库里有 CurseForge Slug → 打开项目页；
  没有 → 回退到 CurseForge 搜索页。

这样「所有模组」都能打开一个相关网页（用户明确要的兜底行为）：能确认项目时
直达详情页，确认不了时打开对应网站的搜索页。链接解析全部离线完成
（``app_common.mcmod_db`` 读 PCL CE 的 mcmod.buf），不联网、不阻塞界面。

署名：WikiId 数据库与百科链接规则移植自 PCL CE（Plain Craft Launcher Community
Edition），见 ``app_common/mcmod_link.py`` 的说明。
"""
import re
import urllib.parse

from .mcmod_db import slugs_for
from .mcmod_link import mcmod_class_url, mcmod_search_url
from .mod_identity import strip_file_suffixes

MODRINTH_PROJECT = "https://modrinth.com/mod/{slug}"
MODRINTH_SEARCH = "https://modrinth.com/mods?q={query}"
CURSEFORGE_PROJECT = "https://www.curseforge.com/minecraft/mc-mods/{slug}"
CURSEFORGE_SEARCH = ("https://www.curseforge.com/minecraft/search"
                     "?page=1&pageSize=20&sortBy=relevancy&class=mc-mods"
                     "&search={query}")

# 文件名 → 展示名：反复剥掉尾部的版本段 / MC 版本段 / 加载器后缀
_VERSION_TAIL = re.compile(r"[-_ .](?:v?\d[\d.]*)$")
_MC_TAIL = re.compile(r"[-_ .+]mc\d[\d.]*$", re.IGNORECASE)
_LOADER_TAIL = re.compile(
    r"[-_ .](?:fabric|forge|neoforge|quilt|fml|common|loader)$", re.IGNORECASE)
# 整合包/启动器可能给 jar 加中文名前缀：`[机械动力：伪装方块+] copycats-3.0.11.jar`
_NAME_PREFIX = re.compile(r"^\s*[\[【][^\]】]{1,80}[\]】]\s*")


def _display_name(filename: str) -> str:
    """由 jar 文件名得到比较干净的模组名（保留中文与原大小写）。

    例：``sodium-fabric-0.5.8+mc1.20.1.jar`` → ``sodium``；
    ``appleskin-forge-mc1.20.1-2.5.1.jar`` → ``appleskin``；
    ``Mekanism-1.20.1-10.4.1.15.jar`` → ``Mekanism``；
    ``OptiFine_1.20.1_HD_U_I6.jar`` → 原样保留（版本段不是纯数字，不能乱删）。
    """
    text = _NAME_PREFIX.sub(
        "", strip_file_suffixes(filename or "").strip()).strip()
    for _ in range(8):
        new = _VERSION_TAIL.sub("", text)
        new = _MC_TAIL.sub("", new)
        new = _LOADER_TAIL.sub("", new)
        if new == text:
            break
        text = new
    return text.strip() or strip_file_suffixes(filename or "")


def _quote(text: str) -> str:
    return urllib.parse.quote_plus(str(text or "").strip())


def mod_links(filename: str, display_name: str = "") -> dict:
    """解析模组文件（或名称）对应的网页链接。

    返回字典（值为 URL 字符串，无法确认直接链接时为 None）：

    - ``name``：用于显示 / 搜索的名称（优先调用方给的 display_name，
      否则由文件名去掉版本段得到）；
    - ``wiki_id`` / ``mcmod_url`` / ``mcmod_search_url``；
    - ``modrinth_slug`` / ``modrinth_url`` / ``modrinth_search_url``；
    - ``curseforge_slug`` / ``curseforge_url`` / ``curseforge_search_url``。

    注意：文件名应传原始 jar 文件名（可带 ``.disabled`` 后缀），不要预先截断
    版本号——``mcmod_db`` 自带「去版本号 / 加载器后缀」的候选匹配，直接传原名
    命中率更高；``name`` 才会做版本段剥离，用于搜索页与菜单文字。
    """
    raw_stem = strip_file_suffixes(filename or "")
    search_name = (display_name or "").strip() or _display_name(filename)
    wiki_id, cf_slug, mr_slug = slugs_for(raw_stem)
    if not wiki_id and not (cf_slug or mr_slug):
        # 原始文件名匹配不到时，再用「去掉版本段」的名字试一次（例如
        # OptiFine_1.20.1_HD_U_I6 → 仍匹配不到，但部分模组的版本段写法
        # 会让数据库候选失效，多一条兜底路径）。
        alt = _display_name(filename)
        if alt and alt != raw_stem:
            wiki_id, cf_slug, mr_slug = slugs_for(alt)

    return {
        "name": search_name,
        "wiki_id": wiki_id,
        "mcmod_url": mcmod_class_url(wiki_id) if wiki_id else None,
        "mcmod_search_url": mcmod_search_url(search_name),
        "modrinth_slug": mr_slug,
        "modrinth_url": (MODRINTH_PROJECT.format(slug=mr_slug)
                         if mr_slug else None),
        "modrinth_search_url": MODRINTH_SEARCH.format(query=_quote(search_name)),
        "curseforge_slug": cf_slug,
        "curseforge_url": (CURSEFORGE_PROJECT.format(slug=cf_slug)
                           if cf_slug else None),
        "curseforge_search_url": CURSEFORGE_SEARCH.format(
            query=_quote(search_name)),
    }


def _add_site_links(sub, name: str, sites) -> list:
    """往子菜单里逐个网站加「查看 / 搜索」动作，返回 [(QAction, url, 站点), ...]。"""
    items = []
    for site, direct_url, search_url in sites:
        # 中文站点名（MC 百科）后面不需要空格，英文站点名（Modrinth）前后留空格更好看
        fmt = "在{site}{verb}「{name}」" if site and not site.isascii() \
            else "在 {site} {verb}「{name}」"
        if direct_url:
            action = sub.addAction(fmt.format(site=site, verb="查看", name=name))
            items.append((action, direct_url, site))
        else:
            action = sub.addAction(fmt.format(site=site, verb="搜索", name=name))
            items.append((action, search_url, site))
    return items


def _add_copy_menu(sub, info: dict) -> list:
    """加「复制链接」子菜单（只要有任意一个详情页直链就显示）。"""
    direct = [(site, info[key]) for site, key in (
        ("MC 百科", "mcmod_url"),
        ("Modrinth", "modrinth_url"),
        ("CurseForge", "curseforge_url")) if info.get(key)]
    if not direct:
        return []
    sub.addSeparator()
    copy = sub.addMenu("复制链接")
    return [(copy.addAction(f"复制 {site} 链接"), url, site)
            for site, url in direct]


def add_mod_web_menu(menu, filename: str, display_name: str = "",
                     menu_title: str = "模组网页（MC 百科 / Modrinth / CurseForge）"):
    """向右键菜单添加「模组网页」子菜单（不 import Qt，菜单对象由调用方传入）。

    每个网站一项：能确认项目 →「查看」详情页；确认不了 →「搜索」页兜底。
    另加「复制链接」子菜单。返回::

        {
          "menu": 子菜单,
          "info": mod_links(...) 的结果,
          "open": [(QAction, url, 站点名), ...],
          "copy": [(QAction, url, 站点名), ...],
        }

    调用方在菜单 exec 返回后按 QAction 打开 URL / 复制链接即可。
    """
    info = mod_links(filename, display_name)
    name = info["name"]
    sub = menu.addMenu(menu_title)
    open_items = _add_site_links(sub, name, (
        ("MC 百科", info["mcmod_url"], info["mcmod_search_url"]),
        ("Modrinth", info["modrinth_url"], info["modrinth_search_url"]),
        ("CurseForge", info["curseforge_url"], info["curseforge_search_url"]),
    ))
    copy_items = _add_copy_menu(sub, info)
    return {"menu": sub, "info": info, "open": open_items, "copy": copy_items}


def add_store_pages_menu(menu, filename: str, display_name: str = "",
                         menu_title: str = "Modrinth / CurseForge 网页"):
    """只加 Modrinth / CurseForge 的「模组网页」子菜单（给已有 MC 百科菜单的页面用）。

    返回结构与 :func:`add_mod_web_menu` 相同（没有 MC 百科项）。
    """
    info = mod_links(filename, display_name)
    name = info["name"]
    sub = menu.addMenu(menu_title)
    open_items = _add_site_links(sub, name, (
        ("Modrinth", info["modrinth_url"], info["modrinth_search_url"]),
        ("CurseForge", info["curseforge_url"], info["curseforge_search_url"]),
    ))
    return {"menu": sub, "info": info, "open": open_items, "copy": []}


def preferred_mod_url(filename: str, display_name: str = "") -> str:
    """模组首选网页：MC 百科详情页 > Modrinth 项目页 > CurseForge 项目页 > 百科搜索页。"""
    info = mod_links(filename, display_name)
    return (info["mcmod_url"] or info["modrinth_url"]
            or info["curseforge_url"] or info["mcmod_search_url"])
