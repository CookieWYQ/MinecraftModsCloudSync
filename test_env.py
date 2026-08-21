# -*- coding: utf-8 -*-
"""验证「分析模组环境」完整流程：jar 列表分析 → 结果 → 应用/取消标注。"""
import json
import os
import tempfile
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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


make_jar("sodium-fabric-0.5.11.jar", "fabric.mod.json",
         json.dumps({"id": "sodium", "name": "Sodium", "environment": "client"}))
make_jar("create-1.20.1-0.5.1.jar", "META-INF/mods.toml",
         b'mods=[{modId="create", displayName="Create", version="0.5.1"}]')
make_jar("servercore-1.0.jar", "fabric.mod.json",
         json.dumps({"id": "servercore", "name": "ServerCore", "environment": "server"}))

cfg = ServerConfig(os.path.join(tempfile.gettempdir(), "env_cfg.json"))
cfg.store.set("servers", [])
cfg.local_mc_dir = root
page = RemoteFilePage(cfg)
page.show()
app.processEvents()

# 1) 分析给定 jar 列表（与审核树中列出的本地模组一致）
jars = [os.path.join(mods, f) for f in os.listdir(mods)]
results = json.loads(page._analyze_env_worker(jars))
by_env = {}
for r in results:
    print(r["file"], "->", r["env"], "|", r["name"], "|", r["source"])
    by_env[r["file"]] = r["env"]
assert by_env["sodium-fabric-0.5.11.jar"] == "client"
assert by_env["create-1.20.1-0.5.1.jar"] == "both"  # Forge 元数据无环境字段 → MC 百科标注补充
assert by_env["servercore-1.0.jar"] == "server"

# 2) 模拟分析完成后：「分析结果」列显示结果，目标列不变（未应用）
page._rows = [
    {"rel": "mods/sodium-fabric-0.5.11.jar", "status": "new", "size": 0,
     "remote_size": 0, "mtime": 0, "target": "both", "manual": False},
    {"rel": "mods/create-1.20.1-0.5.1.jar", "status": "new", "size": 0,
     "remote_size": 0, "mtime": 0, "target": "both", "manual": False},
    {"rel": "mods/servercore-1.0.jar", "status": "new", "size": 0,
     "remote_size": 0, "mtime": 0, "target": "both", "manual": False},
]
page._env_results = {r["file"].lower(): r["env"] for r in results}
page._env_sources = {r["file"].lower(): r["source"] for r in results}
# 目标列保持原样（未应用）
for r in page._rows:
    assert r["target"] == "both"
# 「分析结果」列显示判定结果
assert page._env_cell("mods/sodium-fabric-0.5.11.jar")[0] == "客户端"
assert page._env_cell("mods/create-1.20.1-0.5.1.jar")[0] == "双端"
assert page._env_cell("mods/servercore-1.0.jar")[0] == "服务端"

# 3) 全部应用 → 写入持久化标注，目标列更新
page._apply_env_results()
ov = page.config.target_overrides
assert ov.get("mods/sodium-fabric-0.5.11.jar") == "client", ov
assert ov.get("mods/create-1.20.1-0.5.1.jar") == "both", ov
assert ov.get("mods/servercore-1.0.jar") == "server", ov
assert page._rows[0]["target"] == "client" and page._rows[0]["manual"] is True
# 分析结果列保留（区分「真双端」与「未检测到」）
assert page._env_cell("mods/create-1.20.1-0.5.1.jar")[0] == "双端"

# 4) 取消 → 清空分析结果列，已应用的标注保留
page._cancel_env_results()
assert not page._env_results and not page._env_sources
assert page._env_cell("mods/sodium-fabric-0.5.11.jar") == ("", "")
assert page._rows[0]["target"] == "client"  # 已应用标注不受取消影响
page.close()
print("ANALYZE OK")
