# -*- coding: utf-8 -*-
"""服务端 - 自动同步策略页：规定客户端可以对比/自动下载哪些目录与文件。

与「发布待办（S2C）」的关系：
- S2C 是「服主发布清单 → 客户端按清单安装」；
- 本页是「客户端按服务器下发的策略，直接对比服务器文件仓库并自动拉取」，
  不需要服主每次发布待办。两条链路互不影响，可同时使用。

本页只负责编辑策略文件 ``{todo_dir}/auto_sync.json``：
- 总开关：关闭时所有客户端都不再自动同步；
- 白名单：允许比较/下载的目录或文件（空 = 不同步任何文件）；
- 黑名单：排除项，优先级高于白名单；
- 客户端界面的「自动同步」开关仍由玩家自己控制，两层开关都打开才生效。
"""
import json
import time
from datetime import datetime

from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app_common import winutil
from app_common.auto_sync import (
    default_policy,
    load_policy,
    new_policy,
    normalize_policy,
    save_policy,
    scan_remote,
)
from app_common.logger import get_logger
from app_common.sftp import SFTPManager
from app_common.worker import Worker

log = get_logger("server.auto_sync_page")

# 一键填写的常用范围（仍可在白名单里手工修改）
INCLUDE_PRESETS = [
    ("常用目录", ["mods", "config", "resourcepacks", "shaderpacks",
                  "defaultconfigs"]),
    ("仅模组", ["mods"]),
    ("仅配置", ["config"]),
    ("资源包与光影", ["resourcepacks", "shaderpacks"]),
]


def _fmt_size(num: int) -> str:
    size = float(max(0, int(num or 0)))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


class AutoSyncPage(QWidget):
    """自动同步策略编辑页（每台服务器一份，保存在服务器待办目录）。"""

    def __init__(self, config, status_cb=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.status_cb = status_cb
        self._policy = default_policy()
        self._load_worker = None
        self._save_worker = None
        self._preview_worker = None
        self._last_load = 0.0
        self._build()
        self._set_status("打开本页时读取服务器上的自动同步策略。")

    # ---------- 生命周期 ----------
    def showEvent(self, event):
        """每次打开本页自动读取一次服务器策略（5 秒内不重复）。"""
        super().showEvent(event)
        self._auto_load()

    def _auto_load(self, force: bool = False):
        if not self._has_sftp():
            self._set_status("尚未配置 SFTP 连接信息，请先在「服务器设置」中填写。")
            return
        if self._load_worker is not None:
            return
        now = time.time()
        if not force and now - self._last_load < 5:
            return
        self.reload()

    # ---------- 界面 ----------
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.viewport().setAutoFillBackground(False)
        container = QWidget()
        container.setAutoFillBackground(False)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        scroll.setWidget(container)
        outer.addWidget(scroll)

        intro = QLabel(
            "客户端开启「自动同步」后，会按这里的策略直接对比服务器文件仓库"
            "（客户端文件目录）与本地游戏目录，并只下载新增/变化的文件。\n"
            "本页是服务端规定：哪些目录/文件允许被比较和下载；玩家自己还能在客户端"
            "界面关掉自动同步。客户端只下载、不删除本地文件，本地多出来的文件只提示。")
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # ---- 总开关 ----
        switch_box = QGroupBox("总开关")
        sl = QVBoxLayout(switch_box)
        sl.setSpacing(6)
        self.cb_enabled = QCheckBox(
            "启用自动同步（下发后，所有打开自动同步开关的客户端都会开始拉取）")
        self.cb_enabled.setToolTip(
            "关闭时客户端即使本地开关打开也不会同步；\n"
            "关闭不会删除客户端已下载的文件。")
        sl.addWidget(self.cb_enabled)
        self.lbl_switch_hint = QLabel(
            "说明：这是服务端总开关；客户端还需要玩家在界面上打开「自动同步」才会生效。")
        self.lbl_switch_hint.setObjectName("muted")
        self.lbl_switch_hint.setWordWrap(True)
        sl.addWidget(self.lbl_switch_hint)
        layout.addWidget(switch_box)

        # ---- 白名单 ----
        include_box = QGroupBox("同步范围（白名单，每行一个；空 = 不同步任何文件）")
        il = QVBoxLayout(include_box)
        il.setSpacing(6)
        self.ed_include = QPlainTextEdit()
        self.ed_include.setPlaceholderText(
            "mods\nconfig\nresourcepacks\n# 也可以写文件或通配符：\n"
            "options.txt\nconfig/*.toml\nmods/**/*.jar")
        self.ed_include.setMaximumHeight(130)
        il.addWidget(self.ed_include)
        preset_row = QHBoxLayout()
        preset_row.setSpacing(6)
        preset_row.addWidget(QLabel("快速填写："))
        for label, items in INCLUDE_PRESETS:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, v=items: self._apply_include_preset(v))
            preset_row.addWidget(btn)
        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(lambda: self.ed_include.setPlainText(""))
        preset_row.addWidget(btn_clear)
        preset_row.addStretch(1)
        il.addLayout(preset_row)
        include_hint = QLabel(
            "路径相对「客户端文件目录」与游戏根目录，例如 mods 表示整个模组文件夹"
            "（含子目录）；config/*.toml 只匹配 config 下一层的 toml；"
            "** 可跨多层目录。以 # 开头的行会被忽略。")
        include_hint.setObjectName("muted")
        include_hint.setWordWrap(True)
        il.addWidget(include_hint)
        layout.addWidget(include_box)

        # ---- 黑名单 ----
        exclude_box = QGroupBox("排除（黑名单，优先级高于白名单，每行一个）")
        el = QVBoxLayout(exclude_box)
        el.setSpacing(6)
        self.ed_exclude = QPlainTextEdit()
        self.ed_exclude.setPlaceholderText("*.disabled\n*.bak\nconfig/private.toml")
        self.ed_exclude.setMaximumHeight(100)
        el.addWidget(self.ed_exclude)
        exclude_row = QHBoxLayout()
        btn_default_exclude = QPushButton("恢复默认排除")
        btn_default_exclude.clicked.connect(
            lambda: self.ed_exclude.setPlainText(
                "\n".join(default_policy()["exclude"])))
        exclude_row.addWidget(btn_default_exclude)
        exclude_row.addStretch(1)
        el.addLayout(exclude_row)
        exclude_hint = QLabel(
            "适合排除玩家本地设置、私密配置、被禁用的模组（.disabled）等；"
            "目录命中黑名单时整棵子树都不会被扫描。")
        exclude_hint.setObjectName("muted")
        exclude_hint.setWordWrap(True)
        el.addWidget(exclude_hint)
        layout.addWidget(exclude_box)

        # ---- 备注 ----
        note_box = QGroupBox("备注（可选，只随策略一起记录，方便以后回看）")
        nl = QHBoxLayout(note_box)
        self.ed_note = QLineEdit()
        self.ed_note.setPlaceholderText("例如：2026-11-14 调整同步范围，排除玩家按键配置")
        nl.addWidget(self.ed_note)
        layout.addWidget(note_box)

        # ---- 预览 ----
        preview_box = QGroupBox("预览（按当前填写的策略扫描服务器文件仓库，不保存）")
        pl = QVBoxLayout(preview_box)
        pl.setSpacing(6)
        btn_row = QHBoxLayout()
        self.btn_reload = QPushButton("从服务器重新读取策略")
        self.btn_reload.clicked.connect(self.reload)
        self.btn_preview = QPushButton("扫描并预览同步范围")
        self.btn_preview.clicked.connect(self._preview)
        btn_row.addWidget(self.btn_reload)
        btn_row.addWidget(self.btn_preview)
        btn_row.addStretch(1)
        pl.addLayout(btn_row)
        self.txt_preview = QPlainTextEdit()
        self.txt_preview.setReadOnly(True)
        self.txt_preview.setMaximumHeight(150)
        self.txt_preview.setPlaceholderText(
            "点击「扫描并预览同步范围」后，这里会列出将参与自动同步的文件统计。")
        pl.addWidget(self.txt_preview)
        layout.addWidget(preview_box)

        # ---- 保存 ----
        action_row = QHBoxLayout()
        self.btn_save = QPushButton("保存并下发策略")
        self.btn_save.setObjectName("primary")
        self.btn_save.clicked.connect(self._save)
        action_row.addWidget(self.btn_save)
        action_row.addStretch(1)
        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("muted")
        self.lbl_status.setWordWrap(True)
        action_row.addWidget(self.lbl_status)
        layout.addLayout(action_row)
        layout.addStretch(1)

    # ---------- 状态 ----------
    def _set_status(self, text: str, warn: bool = False):
        self.lbl_status.setText(text)
        if warn:
            log.warning("自动同步策略页: %s", text)

    def _set_busy(self, busy: bool):
        for btn in (self.btn_reload, self.btn_preview, self.btn_save):
            btn.setEnabled(not busy)

    def _has_sftp(self) -> bool:
        return bool(self.config.host())

    def _sftp_args(self) -> dict:
        """在发起后台任务前固定连接参数：切换服务器后旧任务结果不会串台。"""
        return {
            "host": self.config.host(),
            "port": self.config.port(),
            "username": self.config.username(),
            "password": self.config.password(),
            "todo_dir": self.config.todo_dir,
            "files_dir": self.config.files_dir,
        }

    # ---------- 读取 ----------
    def reload(self):
        """从服务器读取当前策略并回填表单（切换服务器/打开页面/保存设置后调用）。"""
        if self._load_worker is not None:
            return
        if not self._has_sftp():
            self._set_status("尚未配置 SFTP 连接信息，请先在「服务器设置」中填写。",
                             warn=True)
            return
        self._last_load = time.time()
        self._set_busy(True)
        self._set_status("正在读取服务器上的自动同步策略…")
        sid = self.config.current_id()
        worker = Worker(self._do_load, self._sftp_args())
        worker.done.connect(
            lambda ok, msg, s=sid: self._on_load_done(ok, msg, s))
        self._load_worker = worker
        worker.start()

    def _do_load(self, info: dict, progress_cb=None) -> str:
        with SFTPManager(info["host"], info["port"],
                         info["username"], info["password"]) as sftp:
            policy = load_policy(sftp, info["todo_dir"])
        return json.dumps({"policy": policy,
                           "todo_dir": info["todo_dir"]},
                          ensure_ascii=False)

    def _on_load_done(self, ok: bool, msg: str, sid: str = ""):
        self._load_worker = None
        self._set_busy(False)
        if sid and sid != self.config.current_id():
            # 读取期间切换了服务器：丢弃旧结果，重新读取当前服务器
            self.reload()
            return
        if not ok:
            self._set_status("读取自动同步策略失败 ✘（可检查 SFTP 连接后重试）",
                             warn=True)
            log.warning("读取自动同步策略失败: %s", msg)
            return
        try:
            data = json.loads(msg)
        except Exception:
            data = {}
        policy = data.get("policy")
        if policy is None:
            self._policy = default_policy()
            self._fill_form(self._policy)
            self._set_status(
                f"服务器尚未下发自动同步策略（{data.get('todo_dir', '')}/"
                f"auto_sync.json 不存在）。当前显示默认值，保存后生效。")
            return
        self._policy = policy
        self._fill_form(policy)
        updated = policy.get("updated_at") or "未知时间"
        enabled = "已启用" if policy.get("enabled") else "已关闭"
        self._set_status(f"已读取服务器策略（{enabled}，更新于 {updated}），"
                         f"共 {len(policy.get('include') or [])} 条白名单、"
                         f"{len(policy.get('exclude') or [])} 条排除。")

    def _fill_form(self, policy: dict):
        policy = normalize_policy(policy)
        self.cb_enabled.setChecked(bool(policy["enabled"]))
        self.ed_include.setPlainText("\n".join(policy["include"]))
        self.ed_exclude.setPlainText("\n".join(policy["exclude"]))
        self.ed_note.setText(policy.get("note") or "")
        self.txt_preview.clear()

    # ---------- 表单 → 策略 ----------
    @staticmethod
    def _lines(edit: QPlainTextEdit) -> list[str]:
        return [line.strip() for line in edit.toPlainText().splitlines()
                if line.strip() and not line.strip().startswith("#")]

    def _collect_policy(self) -> dict:
        return new_policy(
            enabled=self.cb_enabled.isChecked(),
            include=self._lines(self.ed_include),
            exclude=self._lines(self.ed_exclude),
            note=self.ed_note.text().strip(),
        )

    def _apply_include_preset(self, items: list[str]):
        current = self._lines(self.ed_include)
        merged = list(current)
        low = {item.lower() for item in current}
        for item in items:
            if item.lower() not in low:
                merged.append(item)
                low.add(item.lower())
        self.ed_include.setPlainText("\n".join(merged))

    # ---------- 保存 ----------
    def _save(self):
        if not self._has_sftp():
            winutil.warn(self, "提示", "请先在「服务器设置」中填写 SFTP 连接信息。")
            return
        policy = self._collect_policy()
        if policy["enabled"] and not policy["include"]:
            winutil.warn(
                self, "同步范围为空",
                "启用自动同步时，白名单至少需要填写一个目录或文件。\n\n"
                "例如：mods、config、resourcepacks。\n"
                "（白名单为空表示不比较、不下载任何文件。）")
            return
        if not policy["include"]:
            if not winutil.confirm(
                    self, "同步范围为空",
                    "当前白名单为空，客户端即使打开自动同步也不会下载任何文件。\n\n"
                    "仍要保存吗？", default_yes=False):
                return
        self._set_busy(True)
        self._set_status("正在下发策略到服务器…")
        sid = self.config.current_id()
        worker = Worker(self._do_save, self._sftp_args(),
                        json.dumps(policy, ensure_ascii=False))
        worker.done.connect(
            lambda ok, msg, s=sid: self._on_save_done(ok, msg, s))
        self._save_worker = worker
        worker.start()

    def _do_save(self, info: dict, policy_json: str, progress_cb=None) -> str:
        policy = json.loads(policy_json)
        with SFTPManager(info["host"], info["port"],
                         info["username"], info["password"]) as sftp:
            saved = save_policy(sftp, info["todo_dir"], policy)
        return json.dumps(saved, ensure_ascii=False)

    def _on_save_done(self, ok: bool, msg: str, sid: str = ""):
        self._save_worker = None
        self._set_busy(False)
        if sid and sid != self.config.current_id():
            self._set_status("策略已写入切换前的服务器（当前页面已切换到其他服务器）。")
            log.info("自动同步策略已下发到切换前的服务器: %s", sid)
            return
        if not ok:
            self._set_status("下发策略失败 ✘", warn=True)
            winutil.error(self, "保存失败",
                          f"无法把自动同步策略写入服务器：\n{msg}")
            return
        try:
            saved = json.loads(msg)
        except Exception:
            saved = {}
        self._policy = saved or self._collect_policy()
        updated = saved.get("updated_at") or datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S")
        state = "已启用" if saved.get("enabled") else "已关闭"
        self._set_status(f"策略已下发 ✔（{state}，{updated}）")
        if self.status_cb is not None:
            self.status_cb(True)
        lines = [
            f"写入位置：{self.config.todo_dir}/auto_sync.json",
            f"总开关：{state}",
            f"白名单：{len(saved.get('include') or [])} 条",
            f"排除：{len(saved.get('exclude') or [])} 条",
            "",
            "客户端还需在界面上打开「自动同步」开关，并按自己的间隔检测。",
        ]
        if not saved.get("enabled"):
            lines.append("注意：总开关当前为「关闭」，客户端不会自动下载。")
        winutil.info_list(self, "策略已保存", lines)
        log.info("自动同步策略已下发: 白名单 %s 条 / 排除 %s 条 / 启用=%s",
                 len(saved.get("include") or []),
                 len(saved.get("exclude") or []), saved.get("enabled"))

    # ---------- 预览 ----------
    def _preview(self):
        if not self._has_sftp():
            winutil.warn(self, "提示", "请先在「服务器设置」中填写 SFTP 连接信息。")
            return
        policy = self._collect_policy()
        if not policy["include"]:
            winutil.warn(self, "提示",
                         "请先填写同步范围（白名单）再预览。")
            return
        self._set_busy(True)
        self._set_status("正在扫描服务器文件仓库，预览同步范围…")
        self.txt_preview.setPlainText("正在扫描…")
        sid = self.config.current_id()
        worker = Worker(self._do_preview, self._sftp_args(),
                        json.dumps(policy, ensure_ascii=False))
        worker.progress.connect(self._on_preview_progress)
        worker.done.connect(
            lambda ok, msg, s=sid: self._on_preview_done(ok, msg, s))
        self._preview_worker = worker
        worker.start()

    def _on_preview_progress(self, cur: int, total: int, msg: str):
        self.txt_preview.setPlainText(msg)

    def _do_preview(self, info: dict, policy_json: str,
                    progress_cb=None) -> str:
        policy = normalize_policy(json.loads(policy_json))
        with SFTPManager(info["host"], info["port"],
                         info["username"], info["password"]) as sftp:
            if not sftp.exists(info["files_dir"]):
                raise RuntimeError(
                    f"客户端文件目录不存在：{info['files_dir']}\n"
                    f"请先在「服务器设置」中检查「客户端文件目录」。")
            files = scan_remote(sftp, info["files_dir"], policy, progress_cb)
        by_top: dict[str, list[int]] = {}
        for rel, meta in files.items():
            top = rel.split("/", 1)[0] if "/" in rel else "(根目录文件)"
            agg = by_top.setdefault(top, [0, 0])
            agg[0] += 1
            agg[1] += int(meta.get("size") or 0)
        total_bytes = sum(int(meta.get("size") or 0) for meta in files.values())
        return json.dumps({
            "count": len(files),
            "bytes": total_bytes,
            "by_top": by_top,
            "samples": sorted(files, key=str.lower)[:40],
        }, ensure_ascii=False)

    def _on_preview_done(self, ok: bool, msg: str, sid: str = ""):
        self._preview_worker = None
        self._set_busy(False)
        if sid and sid != self.config.current_id():
            self.txt_preview.clear()
            self._set_status("预览期间切换了服务器，结果已丢弃。")
            return
        if not ok:
            self.txt_preview.clear()
            self._set_status("扫描预览失败 ✘", warn=True)
            winutil.error(self, "预览失败", f"无法扫描服务器文件仓库：\n{msg}")
            return
        try:
            data = json.loads(msg)
        except Exception:
            data = {}
        count = int(data.get("count") or 0)
        total_bytes = int(data.get("bytes") or 0)
        lines = [f"同步范围内共 {count} 个文件，合计 {_fmt_size(total_bytes)}。"]
        by_top = data.get("by_top") or {}
        if by_top:
            lines.append("")
            lines.append("按顶层目录：")
            for top in sorted(by_top, key=str.lower):
                n, size = by_top[top]
                label = top if top == "(根目录文件)" else f"{top}/"
                lines.append(f"  {label}    {n} 个文件（{_fmt_size(size)}）")
        samples = data.get("samples") or []
        if samples:
            lines.append("")
            lines.append("示例文件（最多 40 个）：")
            lines.extend(f"  {rel}" for rel in samples)
        self.txt_preview.setPlainText("\n".join(lines))
        self._set_status(f"预览完成：{count} 个文件（{_fmt_size(total_bytes)}）")
