# -*- coding: utf-8 -*-
"""多连接并行扫描 / 哈希回归测试。

验证点：
1. 并行目录扫描（多条独立 SFTP 连接）与串行扫描结果完全一致（含大小、修改时间）；
2. 并行远程哈希在「服务器不支持 check-file」时能自动回退为多连接下载计算，结果正确；
3. 自动同步的 scan_remote 在并行扫描下与原串行实现的策略过滤结果一致；
4. 扫描并发配置的读取 / 范围限制正确。

用法（需在仓库根目录运行）：
    python tests/test_parallel_scan.py
"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app_common import auto_sync
from app_common.app_config import SCAN_CONNS_DEFAULT, SCAN_CONNS_MAX
from app_common.file_hash import hash_remote_parallel
from app_common.sftp import SFTPManager
from tests.fake_sftp import FakeSFTPServer

HOST_USER = "tester"
HOST_PASS = "secret"


def _make_tree(root: str, n: int = 60) -> None:
    """在本地临时目录里造一棵多目录、多文件的树（内容各不相同）。"""
    for d in ("mods", "config", "resourcepacks", "shaderpacks", "deep/a/b"):
        os.makedirs(os.path.join(root, *d.split("/")), exist_ok=True)
    for i in range(n):
        if i % 4 == 0:
            rel = f"mods/mod_{i:03d}.jar"
        elif i % 4 == 1:
            rel = f"config/cfg_{i:03d}.toml"
        elif i % 4 == 2:
            rel = f"resourcepacks/pack_{i:03d}.zip"
        else:
            rel = f"deep/a/b/data_{i:03d}.bin"
        path = os.path.join(root, *rel.split("/"))
        with open(path, "wb") as f:
            f.write((f"file-{i}-" * (i + 1)).encode("utf-8"))


def main() -> None:
    print("=== 1. 并行目录扫描 vs 串行 ===")
    tmp = tempfile.mkdtemp(prefix="par_scan_")
    _make_tree(tmp, 80)
    srv = FakeSFTPServer(tmp)
    host, port = srv.start()
    try:
        with SFTPManager(host, port, HOST_USER, HOST_PASS) as sftp:
            t0 = time.time()
            serial = sftp.list_files_recursive_with_meta(
                "/", max_workers=1, use_cache=False)
            t_serial = time.time() - t0

            t0 = time.time()
            parallel = sftp.list_files_recursive_with_meta(
                "/", max_workers=6, use_cache=False)
            t_parallel = time.time() - t0

        assert len(serial) == 80, f"文件数不对：{len(serial)}"
        assert sorted(serial) == sorted(parallel), "并行扫描结果与串行不一致"
        # 修改时间也要一致（发布待办靠它复用哈希缓存）
        assert ({r[0]: (r[1], r[2]) for r in serial}
                == {r[0]: (r[1], r[2]) for r in parallel}), "大小/修改时间不一致"
        print(f"并行扫描一致：{len(serial)} 个文件，"
              f"串行 {t_serial:.2f}s / 并行 {t_parallel:.2f}s")

        print("=== 2. 多连接并行哈希（服务器不支持 check-file → 多连接下载回退）===")
        with SFTPManager(host, port, HOST_USER, HOST_PASS) as sftp:
            tasks = [(rel, SFTPManager.join("/", rel)) for rel, _s, _m in serial]
            t0 = time.time()
            hashes = hash_remote_parallel(
                sftp, host, port, HOST_USER, HOST_PASS, tasks, workers=6)
            elapsed = time.time() - t0
        assert len(hashes) == len(tasks), \
            f"哈希缺失：{len(hashes)}/{len(tasks)}"
        import hashlib
        for rel, _size, _mtime in serial:
            path = os.path.join(tmp, *rel.split("/"))
            with open(path, "rb") as f:
                expect = hashlib.md5(f.read()).hexdigest()
            assert hashes[rel] == expect, f"哈希错误：{rel}"
        print(f"多连接哈希正确：{len(hashes)} 个文件，{elapsed:.2f}s")

        print("=== 3. 自动同步 scan_remote 并行策略过滤 ===")
        policy = auto_sync.new_policy(
            enabled=True, include=["mods", "config"],
            exclude=["*.zip", "*.disabled"])
        with SFTPManager(host, port, HOST_USER, HOST_PASS) as sftp:
            fast = auto_sync.scan_remote(sftp, "/", policy, workers=6)
            slow = auto_sync.scan_remote(sftp, "/", policy, workers=1)
        assert fast == slow, "并行扫描与串行扫描的策略结果不一致"
        assert fast and all(rel.startswith(("mods/", "config/")) for rel in fast)
        print(f"scan_remote 过滤一致：{len(fast)} 个文件")

        print("=== 4. 扫描并发配置 ===")
        from app_common.app_config import ServerConfig
        cfg = ServerConfig(path=os.path.join(tmp, "server_config.json"))
        assert cfg.scan_conns == SCAN_CONNS_DEFAULT
        cfg.scan_conns = 99
        assert cfg.scan_conns == SCAN_CONNS_MAX
        cfg.scan_conns = 0
        assert cfg.scan_conns == 1
        cfg.scan_conns = 5
        assert cfg.scan_conns == 5
        print("扫描并发配置范围限制正确")
    finally:
        srv.stop()
    print("\nPARALLEL SCAN OK：并行扫描 / 哈希 / 策略过滤 / 配置全部通过")


if __name__ == "__main__":
    main()
