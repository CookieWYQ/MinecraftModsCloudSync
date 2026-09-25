#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Gitee 同步工具：把 GitHub 仓库镜像到 Gitee，并同步代码 + 发行版（Release）+ 安装包附件。

背景：本项目 Gitee 仓库由网页「从 GitHub 克隆导入」创建，GitHub 每次发布后 Gitee 不会
自动同步，且 Gitee 的发行版附件需要手动上传。本工具把这一整套手动流程自动化：

模式一（默认，推荐）：Gitee API 模式
  - 仓库不存在 → 调 API 以 import_url 从 GitHub 导入创建（等价网页「从 GitHub 克隆导入」）；
  - 仓库已存在 → 调 API 触发同步（等价网页「同步更新」按钮，仅对从 GitHub 导入的仓库有效）；
  - 创建/更新发行版（含版本介绍 body），并上传安装包附件（等价网页手动发布 + 传附件）。

模式二（--browser）：浏览器模拟模式（Playwright）
  - 用 Playwright 驱动浏览器模拟网页操作（「同步更新」/「从 GitHub 克隆导入」）。
    需要先安装：pip install playwright && playwright install chromium
    首次运行会在弹出的浏览器中登录一次 Gitee，登录态保存在本地，之后免登录。
    该模式用于没有/不想配置 API 令牌的场景；页面结构变化可能导致失败，失败时请手动操作。

用法：
  # API 模式
  set GITEE_TOKEN=你的私人令牌
  python tools/gitee_sync.py --owner CookieWYQ --repo MinecraftModsCloudSync ^
      --gh-repo CookieWYQ/MinecraftModsCloudSync ^
      --tag v1.1.1 --name v1.1.1 --body "版本介绍文本" ^
      --assets release\\MinecraftSyncClient\\MinecraftSyncClient.exe ^
               release\\MinecraftSyncServer\\MinecraftSyncServer.exe ^
               release\\MinecraftModsCloudSync_Setup_1.1.1.exe

  # 浏览器模拟模式
  python tools/gitee_sync.py --browser --owner CookieWYQ --repo MinecraftModsCloudSync

  # 也可以直接以 JSON 文件提供发行版信息（--release-json release.json）
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://gitee.com/api/v5"

# Gitee 私人令牌：优先命令行 --token，其次环境变量 GITEE_TOKEN
GITHUB_IMPORT_HINT = (
    "提示：Gitee 的「同步」仅对从 GitHub 导入（镜像）的仓库有效。\n"
    "若你的 Gitee 仓库不是从 GitHub 导入创建的，请到网页删除后重新用\n"
    "「从 GitHub 克隆导入」创建，或手动在仓库「管理」里配置 GitHub 镜像。"
)

# ---------- Gitee 令牌的安全存取（Windows 凭据管理器） ----------
# 私人令牌不建议写进环境变量或脚本明文；优先存入 Windows 凭据管理器
# （Credential Manager，由系统 DPAPI 加密，仅当前 Windows 用户可读）。
_CRED_TARGET = "MinecraftModsCloudSync/GiteeToken"
_CRED_TYPE_GENERIC = 1


def _wincred_read(target: str = _CRED_TARGET) -> str:
    """从 Windows 凭据管理器读取令牌；非 Windows / 不存在返回空字符串。"""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.c_void_p),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        advapi32 = ctypes.WinDLL("advapi32")
        advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
        advapi32.CredReadW.restype = wintypes.BOOL
        advapi32.CredFree.argtypes = [ctypes.c_void_p]
        advapi32.CredFree.restype = None
        pcred = ctypes.POINTER(CREDENTIALW)()
        if not advapi32.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
            return ""
        try:
            cred = pcred.contents
            if cred.CredentialBlob and cred.CredentialBlobSize:
                raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
                return raw.decode("utf-16-le").rstrip("\x00")
        finally:
            advapi32.CredFree(pcred)
    except Exception:
        return ""
    return ""


def _wincred_save(token: str, target: str = _CRED_TARGET) -> None:
    """把令牌写入 Windows 凭据管理器（DPAPI 加密，仅当前用户可读）。"""
    if os.name != "nt":
        raise RuntimeError("Windows 凭据管理器仅适用于 Windows。")
    if not token:
        raise ValueError("令牌不能为空。")
    import ctypes
    from ctypes import wintypes

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.c_void_p),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.WinDLL("advapi32")
    advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    advapi32.CredWriteW.restype = wintypes.BOOL
    buf = ctypes.create_unicode_buffer(token)
    cred = CREDENTIALW()
    cred.Type = _CRED_TYPE_GENERIC
    cred.TargetName = target
    cred.UserName = "gitee"
    cred.CredentialBlobSize = len(token) * 2
    cred.CredentialBlob = ctypes.cast(buf, ctypes.c_void_p)
    cred.Persist = 3  # CRED_PERSIST_ENTERPRISE：本机持久保存
    if not advapi32.CredWriteW(ctypes.byref(cred), 0):
        raise ctypes.WinError()


# ---------- HTTP 基础 ----------
class _FileBody:
    """惰性 multipart/form-data 请求体：字段与文件头先按序输出，文件内容流式读取。

    避免 100MB 安装包整体读入内存；中间绝不返回空串（http.client 会把空串当作
    body 结束，导致连接被提前截断）。
    """

    def __init__(self, params: dict, files: list[str], boundary: str):
        self._queue: list = []
        for k, v in params.items():
            if v is None:
                continue
            self._queue.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        for fp in files:
            name = os.path.basename(fp)
            header = (f'--{boundary}\r\nContent-Disposition: form-data; '
                      f'name="file"; filename="{name}"\r\n'
                      f"Content-Type: application/octet-stream\r\n\r\n").encode()
            self._queue.append(header)
            self._queue.append(fp)        # 文件路径（读取时打开）
            self._queue.append(b"\r\n")
        self._queue.append(f"--{boundary}--\r\n".encode())
        self._qi = 0
        self._fh = None

    def read(self, n: int = -1) -> bytes:
        if self._fh is not None:
            chunk = self._fh.read(n)
            if chunk:
                return chunk
            self._fh.close()
            self._fh = None
            # 文件读完：落到下方队列继续读取（文件分隔符 \r\n 已在队列中预置，
            # 此处绝不能返回 b"\r\n" —— 会与队列里的 \r\n 叠加成两个，
            # 被服务器把多余的 \r\n 解析进文件内容导致安装包损坏）。
            # 同时也不能返回 b""：http.client 会把空串当作 body 结束而截断上传。
        while self._qi < len(self._queue):
            item = self._queue[self._qi]
            self._qi += 1
            if isinstance(item, str):    # 文件路径 → 打开并立即读第一块
                self._fh = open(item, "rb")
                return self._fh.read(n)
            return item                  # bytes 段直接返回
        return b""


def _request(method: str, path: str, token: str,
             params: dict | None = None, files: list[str] | None = None,
             timeout: int = 60) -> dict:
    """调用 Gitee API。返回 JSON dict；非 2xx 抛 RuntimeError（含 Gitee 错误信息）。"""
    import http.client

    if params is None:
        params = {}
    params = dict(params)
    params["access_token"] = token
    query = urllib.parse.urlencode(params)
    parsed = urllib.parse.urlsplit(API + path)

    headers = {"User-Agent": "MinecraftModsCloudSync/gitee-sync",
               "Accept": "application/json"}

    if files:
        boundary = "----gitee-sync-%08x" % int(time.time() * 1000)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        body = _FileBody(params, files, boundary)
        query_in_url = query          # 上传接口：参数走 query（Gitee 要求），同时进 form
    elif method in ("POST", "PATCH", "PUT", "DELETE"):
        # 参数只放 body：放进 URL 时，长文本（如版本介绍正文）会撑爆 URL 报 HTTP 414
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = query.encode()
        query_in_url = ""
    else:
        body = None
        query_in_url = query

    path_and_query = parsed.path + ("?" + query_in_url if query_in_url else "")
    conn = http.client.HTTPSConnection(parsed.netloc, timeout=timeout)
    try:
        conn.request(method, path_and_query, body=body, headers=headers)
        resp = conn.getresponse()
        text = resp.read().decode("utf-8", "replace")
    finally:
        conn.close()

    try:
        result = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        result = {"_raw": text[:500]}
    if not (200 <= resp.status < 300):
        msg = result.get("message") or result.get("error") or text[:300]
        raise RuntimeError(f"Gitee API {method} {path} → HTTP {resp.status}: {msg}")
    return result


# ---------- 代码同步 ----------
def _git_push_code(owner: str, repo: str, token: str) -> str:
    """直接用 git push 把本地仓库代码推到 Gitee（不依赖镜像 sync 接口）。

    返回空字符串表示无法执行（本地无仓库 / push 失败），否则返回成功消息。
    """
    import subprocess
    root = ROOT
    if not (root / ".git").exists():
        return ""
    url = f"https://{owner}:{token}@gitee.com/{owner}/{repo}.git"
    try:
        r = subprocess.run(["git", "-C", str(root), "push", url, "master"],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            return ""
        subprocess.run(["git", "-C", str(root), "push", url, "--tags"],
                       capture_output=True, text=True, timeout=600)
        return f"已通过 git push 同步代码到 Gitee：https://gitee.com/{owner}/{repo}"
    except Exception:
        return ""


def sync_code(token: str, owner: str, repo: str, gh_repo: str,
              description: str = "") -> str:
    """确保 Gitee 仓库存在（不存在则从 GitHub 导入），并触发一次同步。返回消息。

    代码同步优先调 Gitee 官方 sync 接口（仅对「从 GitHub 导入」的镜像仓库有效）；
    若仓库不是镜像（接口返回 405/403 等）→ 自动回退为 git push 直接推送，效果等价。
    """
    try:
        info = _request("GET", f"/repos/{owner}/{repo}", token)
        # 仓库已存在 → 触发同步
        try:
            _request("POST", f"/repos/{owner}/{repo}/sync", token)
            return f"仓库已存在，已触发同步：{info.get('html_url', '')}"
        except RuntimeError as exc:
            if "同步" in str(exc) or "import" in str(exc).lower():
                return (f"仓库已存在：{info.get('html_url', '')}\n"
                        f"但同步失败（{exc}）。\n{GITHUB_IMPORT_HINT}")
            # 非镜像仓库（405 等）→ 回退为 git push 直接推送
            msg = _git_push_code(owner, repo, token)
            if msg:
                return msg
            return (f"仓库已存在：{info.get('html_url', '')}\n"
                    f"但同步接口不可用（{exc}），且本地 git push 也失败。\n"
                    f"{GITHUB_IMPORT_HINT}")
    except RuntimeError as exc:
        if "404" not in str(exc):
            raise
        # 仓库不存在 → 通过 import_url 从 GitHub 导入创建
        _request("POST", "/user/repos", token, params={
            "name": repo,
            "description": description or f"GitHub 仓库 {gh_repo} 的镜像",
            "private": False,
            "auto_init": False,
            "import_url": f"https://github.com/{gh_repo}.git",
        }, timeout=120)
        time.sleep(3)  # 等 Gitee 完成导入
        return f"仓库不存在，已从 GitHub 导入创建：https://gitee.com/{owner}/{repo}"


# ---------- 发行版（Release） ----------
def ensure_release(token: str, owner: str, repo: str,
                   tag: str, name: str, body: str) -> int:
    """确保发行版存在（不存在则创建），返回 release_id。已存在则更新 name/body。

    注意：Gitee 对「tag 不存在」的 GET 返回 HTTP 200 + body `null`（并非 404），
    因此 r 为 None / 无 id 均视为不存在，走创建分支。
    """
    r = _request("GET", f"/repos/{owner}/{repo}/releases/tags/{tag}", token)
    rid = (r or {}).get("id")
    if rid:
        _request("PATCH", f"/repos/{owner}/{repo}/releases/{rid}", token,
                 params={"name": name, "body": body, "tag_name": tag})
        return int(rid)
    r = _request("POST", f"/repos/{owner}/{repo}/releases", token, params={
        "tag_name": tag,
        "name": name,
        "body": body,
        "target_commitish": "master",
        "prerelease": False,
    })
    return int(r["id"])


def upload_assets(token: str, owner: str, repo: str,
                  release_id: int, assets: list[str]) -> list[str]:
    """上传附件到发行版（multipart）。返回已上传文件名列表。

    先查询发行版现有附件，**跳过已存在的同名附件**，避免重复上传。
    """
    existing: set[str] = set()
    try:
        rel = _request("GET", f"/repos/{owner}/{repo}/releases/{release_id}", token)
        existing = {a.get("name") for a in (rel.get("assets") or [])}
    except RuntimeError:
        pass
    uploaded = []
    for path in assets:
        if not os.path.isfile(path):
            continue
        name = os.path.basename(path)
        if name in existing:
            print(f"  附件已存在，跳过：{name}")
            continue
        size = os.path.getsize(path)
        print(f"  上传附件 {name}（{size / 1048576:.1f} MB）…")
        _request("POST", f"/repos/{owner}/{repo}/releases/{release_id}/attach_files",
                 token, files=[path], timeout=600)
        uploaded.append(name)
        existing.add(name)
    return uploaded


# ---------- 浏览器模拟模式 ----------
def sync_via_browser(owner: str, repo: str, gh_repo: str) -> int:
    """用 Playwright 驱动浏览器模拟网页「同步更新」/「从 GitHub 克隆导入」。

    需要：pip install playwright && playwright install chromium
    登录态保存在本地（%LOCALAPPDATA%\\MC-Mod-Sync\\gitee_browser_profile），首次需手动登录。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("未安装 Playwright。请先执行：")
        print("  .venv\\Scripts\\python -m pip install playwright")
        print("  .venv\\Scripts\\python -m playwright install chromium")
        return 1

    data_dir = Path(os.environ.get("LOCALAPPDATA", ".")) / "MC-Mod-Sync" / "gitee_browser_profile"
    data_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(data_dir), headless=False)
        page = ctx.new_page()

        # 1. 检查登录（未登录 → 用户手动登录一次，之后 cookie 持久化）
        page.goto("https://gitee.com/login", wait_until="domcontentloaded", timeout=30000)
        if "登录" in (page.title() or "") or page.locator("text=立即登录").count():
            input(">>> 请在弹出的浏览器中登录 Gitee 账号，登录完成后回到终端按回车继续…")

        # 2. 仓库已存在 → 点「同步更新」
        page.goto(f"https://gitee.com/{owner}/{repo}", wait_until="domcontentloaded", timeout=30000)
        not_found = page.locator("text=仓库不存在").count() or page.locator("text=404").count()
        if not not_found:
            btn = page.locator("text=同步更新").first
            if btn.count():
                btn.click(timeout=8000)
                if page.locator("text=确认同步").count():
                    page.locator("text=确认同步").first.click(timeout=8000)
                print(f"已触发「同步更新」：https://gitee.com/{owner}/{repo}")
                page.wait_for_timeout(4000)
                ctx.close()
                return 0
            print("未找到「同步更新」按钮。请确认该仓库为从 GitHub 导入的镜像仓库。")
            print(f"GitHub 仓库：https://github.com/{gh_repo}")
            input(">>> 如已完成同步请按回车退出；如需操作请直接在浏览器中处理…")
            ctx.close()
            return 0

        # 3. 仓库不存在 → 引导「从 GitHub 克隆导入」
        print("仓库不存在，打开「从 GitHub 克隆导入」页面…")
        page.goto("https://gitee.com/repo/create", wait_until="domcontentloaded", timeout=30000)
        if page.locator("text=从 GitHub 导入").count():
            page.locator("text=从 GitHub 导入").first.click()
        try:
            page.fill('input[name="import_url"]',
                      f"https://github.com/{gh_repo}.git", timeout=5000)
            page.fill('input[name="name"]', repo, timeout=5000)
            page.locator("button:has-text(\"导入\")").first.click(timeout=5000)
            print(f"已提交导入请求：https://github.com/{gh_repo} → Gitee/{owner}/{repo}")
        except Exception:
            print("自动填写失败（页面结构可能已变化），请在浏览器中手动完成导入。")
        input(">>> 完成后回到终端按回车退出…")
        ctx.close()
    return 0


# ---------- 入口 ----------
def main() -> int:
    parser = argparse.ArgumentParser(description="同步 GitHub 仓库/Release 到 Gitee")
    parser.add_argument("--token", default="",
                        help="Gitee 私人令牌（省略时依次读取环境变量 GITEE_TOKEN、"
                             "Windows 凭据管理器）")
    parser.add_argument("--save-token", default="",
                        help="把令牌安全保存到 Windows 凭据管理器后退出"
                             "（以后自动读取，无需再设置环境变量）")
    parser.add_argument("--owner", default="", help="Gitee 用户名（默认取令牌对应用户）")
    parser.add_argument("--repo", default="MinecraftModsCloudSync", help="Gitee 仓库名")
    parser.add_argument("--gh-repo", default="CookieWYQ/MinecraftModsCloudSync",
                        help="GitHub 源仓库 owner/name")
    parser.add_argument("--tag", default="", help="发行版 tag（如 v1.1.1）")
    parser.add_argument("--name", default="", help="发行版标题")
    parser.add_argument("--body", default="", help="发行版版本介绍（Markdown）")
    parser.add_argument("--body-file", default="", help="版本介绍文本文件（UTF-8），优先于 --body")
    parser.add_argument("--assets", nargs="*", default=[], help="要上传的附件路径（exe 等）")
    parser.add_argument("--browser", action="store_true", help="使用浏览器模拟模式（Playwright）")
    args = parser.parse_args()

    if args.save_token:
        _wincred_save(args.save_token)
        print(f"已把 Gitee 令牌安全保存到 Windows 凭据管理器（目标：{_CRED_TARGET}）")
        print("以后运行 python build.py --gitee 会自动读取，无需再设置环境变量。")
        return 0

    gh_owner, _, gh_repo_name = args.gh_repo.partition("/")
    gh_repo = args.gh_repo

    if args.browser:
        return sync_via_browser(args.owner, args.repo, gh_repo)

    # ---- API 模式 ----
    # 令牌解析顺序：--token > 环境变量 GITEE_TOKEN > Windows 凭据管理器
    token = args.token or os.environ.get("GITEE_TOKEN", "") or _wincred_read()
    if not token:
        print("缺少 Gitee 令牌（已检查 --token / 环境变量 GITEE_TOKEN / Windows 凭据管理器）")
        print("获取方式：登录 Gitee → 右上角头像 → 设置 → 安全设置 → 私人令牌 → 生成新令牌")
        print("          权限勾选：projects（仓库读写）、releases（发行版管理）")
        print("安全保存：python tools/gitee_sync.py --save-token 你的令牌")
        return 2
    owner = args.owner or _request("GET", "/user", token).get("login", "")
    if not owner:
        print("无法确定 Gitee 用户名，请用 --owner 指定")
        return 2
    print(f"Gitee 用户：{owner}，仓库：{args.repo}")

    # 1. 代码同步
    print("[1/3] 同步代码…")
    msg = sync_code(token, owner, args.repo, gh_repo,
                    description=f"GitHub 仓库 {gh_repo} 的镜像")
    print(f"  {msg}")

    # 2. 发行版
    print("[2/3] 创建/更新发行版…")
    if not args.tag and not args.name:
        print("  未提供 --tag/--name，跳过发行版操作")
        return 0
    body = ""
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    elif args.body:
        body = args.body
    release_id = ensure_release(token, owner, args.repo,
                                args.tag or args.name, args.name or args.tag, body)
    print(f"  发行版已就绪：https://gitee.com/{owner}/{args.repo}/releases/{args.tag or args.name}")

    # 3. 附件
    if args.assets:
        print("[3/3] 上传附件…")
        uploaded = upload_assets(token, owner, args.repo, release_id, args.assets)
        print(f"  已上传：{', '.join(uploaded) if uploaded else '（无）'}")
    else:
        print("[3/3] 跳过（未指定 --assets）")

    print("\nGitee 同步完成。")
    print(f"  仓库：https://gitee.com/{owner}/{args.repo}")
    print(f"  发行版：https://gitee.com/{owner}/{args.repo}/releases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
