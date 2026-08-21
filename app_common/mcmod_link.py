# -*- coding: utf-8 -*-
"""模组 ↔ MCMod（MC 百科）链接关联工具。

功能移植自 PCL CE（Plain Craft Launcher Community Edition），相关代码署名 PCL CE：
- 搜索词构造 / 百科搜索链接：PCL-CE 仓库
  `Plain Craft Launcher 2/Pages/PageInstance/PageInstanceCompResource.xaml.cs`
  L2547-L2591（右键模组 →「百科搜索」）
- 百科详情页链接（WikiId → class 页面）：PCL-CE 仓库
  `Plain Craft Launcher 2/Modules/Minecraft/ModComp.cs` L1435-L1438
- WikiId 数据库（mcmod.buf）解析与 Slug 匹配：PCL-CE 仓库
  `Plain Craft Launcher 2/Modules/Minecraft/ModComp.cs` L805-L858（CompDatabaseEntry）
源码仓库：https://github.com/PCL-Community/PCL-CE
"""
import re
import urllib.parse

from .mcmod_db import wiki_id_for
from .mod_identity import mod_display_name as _meta_display_name

_MC_BASE = "https://www.mcmod.cn"


def build_mcmod_search_key(name: str) -> str:
    """构造 MC 百科搜索词（PCL CE 移植）。

    PCL CE 的原始逻辑（PageInstanceCompResource.xaml.cs）：
    - 空格 → '+'
    - 驼峰边界（上一个字母小写、这一个字母大写）处插入 '+'：OptiFine → Opti+Fine
    - 合并连续 '+'，并修正特例 "pti+Fine" → "ptiFine"
    """
    raw = (name or "").replace(" ", "+")
    if not raw:
        return ""
    out = raw[0]
    for i in range(1, len(raw)):
        prev, cur = raw[i - 1], raw[i]
        # 仅在两侧都是字母时才判断驼峰边界，避免 "a1B" 这类被错误拆分
        if prev.isalpha() and cur.isalpha() and prev.islower() and cur.isupper():
            out += "+"
        out += cur
    return out.replace("++", "+").replace("pti+Fine", "ptiFine")


def mcmod_search_url(name: str) -> str:
    """MC 百科搜索链接（PCL CE 移植：https://www.mcmod.cn/s?key=...&site=all&filter=0）。"""
    key = build_mcmod_search_key(name)
    return f"{_MC_BASE}/s?key={urllib.parse.quote(key, safe='+')}&site=all&filter=0"


def mcmod_class_url(wiki_id) -> str:
    """MC 百科模组详情页链接（PCL CE 移植：WikiId → https://www.mcmod.cn/class/{id}.html）。"""
    return f"{_MC_BASE}/class/{int(wiki_id)}.html"


def mcmod_view_url(name: str) -> str:
    """模组对应的百科链接：命中本地数据库 → 详情页；否则回退 → 搜索页（PCL CE 移植）。"""
    wid = wiki_id_for(name)
    return mcmod_class_url(wid) if wid else mcmod_search_url(name)


def add_mcmod_menu_actions(menu, search_name: str):
    """向菜单添加 MC 百科相关操作（功能移植自 PCL CE，署名 PCL CE）。

    - 命中 WikiId 数据库：加「在 MC 百科查看」→ 详情页链接
    - 始终提供「在 MC 百科搜索」与「复制百科链接」
    返回 (act_view, act_search, act_copy, view_url, search_url)，
    act_view 在未命中数据库时为 None。
    """
    wid = wiki_id_for(search_name)
    search_url = mcmod_search_url(search_name)
    view_url = mcmod_class_url(wid) if wid else search_url
    act_view = menu.addAction(f"在 MC 百科查看「{search_name}」") if wid else None
    act_search = menu.addAction(f"在 MC 百科搜索「{search_name}」")
    act_copy = menu.addAction("复制百科链接")
    return act_view, act_search, act_copy, view_url, search_url


def mod_search_name(filename: str) -> str:
    """由模组文件名生成百科搜索名：去 .jar 扩展名并去掉尾部版本段（保留原大小写）。

    说明：PCL CE 直接使用去扩展名的文件名；这里额外去掉尾部版本段
    （如 OptiFine_1.20.1.jar → OptiFine），可显著提高搜索准确率，
    驼峰转换逻辑仍与 PCL CE 保持一致。
    """
    stem = filename[:-4] if filename.lower().endswith(".jar") else filename
    stem = re.sub(r"[-_ .](?:v)?\d[\d._-]*$", "", stem)
    return stem.strip()


def mod_display_name(path: str, filename: str = "") -> str:
    """模组显示名：本地 jar 优先读取元数据中的名称（PCL CE 移植，ModLocalComp.cs），
    读不到时退回文件名基名。远程 jar（无法读元数据）直接传 filename。"""
    if path:
        name = _meta_display_name(path)
        if name:
            return name
    return mod_search_name(filename or path or "")
