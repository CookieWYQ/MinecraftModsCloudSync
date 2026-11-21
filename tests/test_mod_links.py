# -*- coding: utf-8 -*-
"""模组网页链接解析回归测试（纯离线，不联网、不依赖 Qt）。

覆盖：
1. MC 百科 / Modrinth / CurseForge 直链解析（命中本地 mcmod.buf）；
2. 确认不了项目时回退到各网站搜索页（保证每个 jar 都能打开网页）；
3. ``.disabled`` 后缀、带版本号 / 加载器后缀的文件名；
4. 「模组网页」子菜单的菜单文案（查看 vs 搜索）与返回的链接。

用法（仓库根目录）：
    python tests/test_mod_links.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app_common import mod_links as ml


class _FakeMenu:
    """记录 addMenu / addAction / addSeparator 的假菜单（不依赖 Qt）。"""

    def __init__(self):
        self.items = []

    def addMenu(self, title):
        sub = _FakeMenu()
        self.items.append(("menu", title, sub))
        return sub

    def addAction(self, label):
        action = f"action:{label}"
        self.items.append(("action", action, None))
        return action

    def addSeparator(self):
        self.items.append(("sep", "", None))


def check_known_mods() -> None:
    print("=== 1. 已知模组：直链 ===")
    cases = {
        "sodium-fabric-0.5.8+mc1.20.1.jar": ("sodium", 2785),
        "jei-1.20.1-forge-15.2.0.27.jar": ("jei", 459),
        "Mekanism-1.20.1-10.4.1.15.jar": ("mekanism", 187),
    }
    for filename, (slug, wiki_id) in cases.items():
        info = ml.mod_links(filename)
        assert info["wiki_id"] == wiki_id, (filename, info)
        assert info["mcmod_url"] == f"https://www.mcmod.cn/class/{wiki_id}.html", info
        assert info["modrinth_slug"] == slug, (filename, info)
        assert info["modrinth_url"] == f"https://modrinth.com/mod/{slug}", info
        assert info["curseforge_slug"] == slug, (filename, info)
        assert info["curseforge_url"] == \
            f"https://www.curseforge.com/minecraft/mc-mods/{slug}", info
        assert info["mcmod_search_url"].startswith("https://www.mcmod.cn/s?key=")
        assert info["name"], info
    print(f"已知模组直链正确：{len(cases)} 个（MC 百科 + Modrinth + CurseForge）")


def check_fallback() -> None:
    print("=== 2. 未知模组：搜索页兜底 ===")
    info = ml.mod_links("xyz-unknown-mod-1.2.3.jar")
    assert info["wiki_id"] is None and info["mcmod_url"] is None, info
    assert info["modrinth_url"] is None and info["curseforge_url"] is None, info
    assert info["mcmod_search_url"].startswith("https://www.mcmod.cn/s?key="), info
    assert info["modrinth_search_url"].startswith("https://modrinth.com/mods?q="), info
    assert info["curseforge_search_url"].startswith(
        "https://www.curseforge.com/minecraft/search?"), info
    assert "search=xyz-unknown-mod" in info["curseforge_search_url"], info
    assert ml.preferred_mod_url("xyz-unknown-mod-1.2.3.jar") == \
        info["mcmod_search_url"]
    print("未知模组正确回退到三个网站的搜索页")


def check_disabled_and_names() -> None:
    print("=== 3. 禁用后缀 / 展示名 ===")
    info = ml.mod_links("sodium-fabric-0.5.8+mc1.20.1.jar.disabled")
    assert info["wiki_id"] == 2785, info
    assert info["modrinth_url"] == "https://modrinth.com/mod/sodium", info
    assert info["name"] == "sodium", info
    names = {
        "appleskin-forge-mc1.20.1-2.5.1.jar": "appleskin",
        "Mekanism-1.20.1-10.4.1.15.jar": "Mekanism",
        "jei-1.20.1-forge-15.2.0.27.jar": "jei",
    }
    for filename, expected in names.items():
        got = ml.mod_links(filename)["name"]
        assert got == expected, (filename, got)
    # 版本段不是纯数字时不能乱删（OptiFine_1.20.1_HD_U_I6 保留原样）
    optifine = ml.mod_links("OptiFine_1.20.1_HD_U_I6.jar")
    assert optifine["wiki_id"] is None, optifine
    assert "OptiFine" in optifine["name"], optifine
    print("禁用后缀与展示名处理正确")


def check_menu() -> None:
    print("=== 4. 右键「模组网页」子菜单 ===")
    menu = _FakeMenu()
    bundle = ml.add_mod_web_menu(menu, "sodium-fabric-0.5.8+mc1.20.1.jar")
    assert bundle["info"]["modrinth_url"] == "https://modrinth.com/mod/sodium"
    assert len(bundle["open"]) == 3, bundle["open"]
    sites = [site for _action, _url, site in bundle["open"]]
    assert sites == ["MC 百科", "Modrinth", "CurseForge"], sites
    menu_titles = [item[1] for item in menu.items if item[0] == "menu"]
    assert menu_titles and menu_titles[0].startswith("模组网页"), menu.items
    sub_items = [item for item in menu.items[0][2].items]
    action_labels = [item[1].replace("action:", "")
                     for item in sub_items if item[0] == "action"]
    assert any("查看" in text for text in action_labels), action_labels
    assert not any("搜索" in text for text in action_labels), action_labels
    copy_actions = bundle["copy"]
    assert len(copy_actions) == 3, copy_actions

    # 未知模组：三个网站都变「搜索」
    menu2 = _FakeMenu()
    bundle2 = ml.add_mod_web_menu(menu2, "xyz-unknown-mod-1.2.3.jar")
    sub2 = [item for item in menu2.items[0][2].items if item[0] == "action"]
    labels2 = [item[1].replace("action:", "") for item in sub2]
    assert all("搜索" in text for text in labels2), labels2
    assert not bundle2["copy"], bundle2["copy"]

    # 其它页面（更新服务端 / 发布待办）只追加 Modrinth / CurseForge 两项
    store = ml.add_store_pages_menu(_FakeMenu(),
                                    "sodium-fabric-0.5.8+mc1.20.1.jar")
    assert [site for _a, _u, site in store["open"]] == ["Modrinth", "CurseForge"], \
        store["open"]
    assert store["open"][0][1] == "https://modrinth.com/mod/sodium", store["open"]
    print("菜单文案正确：命中直链 →「查看」，未命中 →「搜索」")


def main() -> None:
    check_known_mods()
    check_fallback()
    check_disabled_and_names()
    check_menu()
    print("\nMOD LINKS OK")


if __name__ == "__main__":
    main()
