# -*- coding: utf-8 -*-
"""验证「模组运行环境 + 版本检查」流程：
jar 分析 → 两侧状态 → 分析结果列 → 应用/取消标注 → 最新版本列。

不联网（只读本地 jar 元数据、版本解析与展示均为纯函数），可离线运行。
"""
import json
import os
import tempfile
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app_common.app_config import ServerConfig
from server_app.ui.remote_page import RemoteFilePage

app = QApplication([])
root = tempfile.mkdtemp()
mods = os.path.join(root, "mods")
os.makedirs(mods)


def make_jar(fname, meta_entry, meta_bytes):
    p = os.path.join(mods, fname)
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr(meta_entry, meta_bytes)
    return p


# 仅客户端 / 双端 / 仅服务端 三种本地可判定的情形
make_jar("sodium-fabric-0.5.11.jar", "fabric.mod.json",
         json.dumps({"id": "sodium", "name": "Sodium",
                     "version": "0.5.11", "environment": "client"}))
make_jar("lithium-fabric-0.14.2.jar", "fabric.mod.json",
         json.dumps({"id": "lithium", "name": "Lithium", "environment": "*"}))
make_jar("servercore-1.0.jar", "fabric.mod.json",
         json.dumps({"id": "servercore", "name": "ServerCore", "environment": "server"}))
# Forge 元数据不含运行环境字段 → 两侧都未判定（离线时不瞎猜成双端）
make_jar("create-1.20.1-0.5.1.jar", "META-INF/mods.toml",
         b'mods=[{modId="create", displayName="Create", version="0.5.1"}]')

# 新配置的默认值：版本检查默认开启（可在设置页关闭）
_fresh = ServerConfig(tempfile.mkstemp(suffix=".json")[1])
assert _fresh.env_version_check is True, "版本检查默认开启"
assert _fresh.env_client_optional is False and _fresh.env_server_optional is False

cfg = ServerConfig(os.path.join(tempfile.gettempdir(), "env_cfg.json"))
cfg.store.set("servers", [])
cfg.remote = {**cfg.remote, "env_online": False, "env_version_check": False}  # 离线
cfg.local_mc_dir = root
page = RemoteFilePage(cfg)
page.show()
app.processEvents()

# 1) 分析给定 jar 列表（与审核树中列出的本地模组一致）
jars = [os.path.join(mods, f) for f in sorted(os.listdir(mods))]
results = json.loads(page._analyze_env_worker(jars))
by_file = {}
for r in results:
    print(f"{r['file']} -> {r['env']} | {r['name']} | {r['source']}")
    by_file[r["file"]] = r

assert by_file["sodium-fabric-0.5.11.jar"]["env"] == "client"
assert by_file["lithium-fabric-0.14.2.jar"]["env"] == "both"
assert by_file["servercore-1.0.jar"]["env"] == "server"
assert by_file["create-1.20.1-0.5.1.jar"]["env"] == "unknown"
# 两侧状态分别保存，「客户端可选 + 服务端需装」这类组合不会被压成一个值
assert (by_file["sodium-fabric-0.5.11.jar"]["c"],
        by_file["sodium-fabric-0.5.11.jar"]["s"]) == ("required", "none")
assert (by_file["servercore-1.0.jar"]["c"],
        by_file["servercore-1.0.jar"]["s"]) == ("none", "required")

# 2) 模拟分析完成后：「分析结果」列显示两侧状态，目标列不变（未应用）
page._rows = [
    {"rel": f"mods/{f}", "status": "new", "size": 0,
     "remote_size": 0, "mtime": 0, "target": "both", "manual": False}
    for f in sorted(os.listdir(mods))
]
page._env_results = {r["file"].lower(): r["env"] for r in results}
page._env_sources = {r["file"].lower(): r["source"] for r in results}
page._env_summaries = {r["file"].lower(): f"客户端：{r['c']} ｜ 服务端：{r['s']}"
                       for r in results}
for r in page._rows:
    assert r["target"] == "both"  # 目标列保持原样（未应用）

assert page._env_cell("mods/sodium-fabric-0.5.11.jar")[0] == "客户端：required ｜ 服务端：none"
assert page._env_cell("mods/lithium-fabric-0.14.2.jar")[0] == "客户端：required ｜ 服务端：required"
assert page._env_cell("mods/servercore-1.0.jar")[0] == "客户端：none ｜ 服务端：required"
# tooltip 给出「按当前设置发往哪里」与判断依据
sodium_tip = page._env_cell("mods/sodium-fabric-0.5.11.jar")[1]
assert "仅客户端" in sodium_tip and "environment=client" in sodium_tip, sodium_tip

# 3) 全部应用 → 写入持久化标注，目标列更新
page._apply_env_results()
ov = page.config.target_overrides
assert ov.get("mods/sodium-fabric-0.5.11.jar") == "client", ov
assert ov.get("mods/lithium-fabric-0.14.2.jar") == "both", ov
assert ov.get("mods/servercore-1.0.jar") == "server", ov
assert "mods/create-1.20.1-0.5.1.jar" not in ov, "未判定的不应写入标注"
targets = {r["rel"]: (r["target"], r["manual"]) for r in page._rows}
assert targets["mods/sodium-fabric-0.5.11.jar"] == ("client", True)
# 分析结果列保留（区分「真双端」与「未检测到」）
assert page._env_cell("mods/lithium-fabric-0.14.2.jar")[0].startswith("客户端：required")

# 4) 取消 → 清空分析结果列，已应用的标注保留
page._cancel_env_results()
assert not page._env_results and not page._env_sources and not page._env_summaries
assert page._env_cell("mods/sodium-fabric-0.5.11.jar") == ("", "")
after = {r["rel"]: r["target"] for r in page._rows}
assert after["mods/sodium-fabric-0.5.11.jar"] == "client"  # 已应用标注不受取消影响

# 5) 开关：把「客户端可选」也当作客户端需装 → 目标变「双端」
from app_common.mod_env import env_target

assert env_target("optional", "required") == "server"
assert env_target("optional", "required", client_optional=True) == "both"

# 6) 模组版本检查（离线：只验证解析/比较/本地读取/展示，联网查询不参与）
#    这部分保证「本地是不是旧版」的判定逻辑正确，是「防止更新时删错模组」的基础。
from app_common.mod_version import (
    VER_LATEST, VER_OUTDATED, VER_UNKNOWN,
    compare_versions, guess_game_version, jar_meta, parse_version,
    status_text, tooltip_text,
)

assert parse_version("0.5.8") == (0, 5, 8)
assert parse_version("mc1.20.1-0.5.13-fabric") == (0, 5, 13), "要跳过 mc 版本前缀"
assert parse_version("1.20.1-1.0.0") == (1, 0, 0)
assert parse_version("v2") == (2,)
assert parse_version("") == ()
assert compare_versions("0.5.8", "0.5.13") == -1
assert compare_versions("1.0", "1.0.0") == 0, "1.0 与 1.0.0 视为同一版"
assert compare_versions("2.0", "1.9.9") == 1
assert compare_versions("abc", "1.0") is None, "解析不出数字 → 无法比较"

# 文件名里的 MC 版本提示：有则用于消歧，无则留空（不瞎猜）
assert guess_game_version("journeymap-1.20.1-5.9.0.jar") == "1.20.1"
assert guess_game_version("sodium-fabric-0.5.11.jar") == ""
assert guess_game_version("create-1.20.1-0.5.1f.jar") == "1.20.1"

# 本地 jar → (版本号, 加载器)：版本检查的输入
assert jar_meta(os.path.join(mods, "sodium-fabric-0.5.11.jar")) == ("0.5.11", "fabric")
assert jar_meta(os.path.join(mods, "create-1.20.1-0.5.1.jar")) == ("0.5.1", "forge")

# 列文案：有新版本时附上渠道版本号；未查到不编造版本号
info = {"status": VER_OUTDATED, "latest": "0.6.0", "local": "0.5.11",
        "source": "Modrinth", "url": "https://modrinth.com/mod/sodium",
        "note": "与本地同一游戏版本（1.20.1）"}
assert status_text(info) == "有新版本 0.6.0"
assert status_text({"status": VER_LATEST}) == "已是最新"
assert status_text({"status": VER_UNKNOWN, "latest": "9.9"}) == "未查到", "未查到不得显示版本号"
tip = tooltip_text(info)
assert "本地版本：0.5.11" in tip and "渠道最新：0.6.0" in tip and "Modrinth" in tip

# 「最新版本」列渲染：结果写进第 8 列（不联网，直接喂结果）
page._version_results = {"sodium-fabric-0.5.11.jar": info}
page._refresh_tree()
tree = page.review_tree
assert tree.columnCount() == 8, "新增「最新版本」列"
node = None
for i in range(tree.topLevelItem(0).childCount()):
    d = tree.topLevelItem(0).child(i)
    if (d.data(0, Qt.UserRole) or {}).get("rel") == "mods":
        for j in range(d.childCount()):
            if d.child(j).text(0) == "sodium-fabric-0.5.11.jar":
                node = d.child(j)
assert node is not None, "未能定位到 sodium 行"
assert node.text(7) == "有新版本 0.6.0", node.text(7)
assert "Modrinth" in node.toolTip(7)

# 取消分析 → 「分析结果」「最新版本」两列一起清空
page._cancel_env_results()
assert not page._version_results
assert node.text(7) == "" and node.text(6) == ""
page.close()
print("ANALYZE OK")
