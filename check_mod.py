import sys
import os
import zipfile
import json
import re
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QLabel, QVBoxLayout,
    QWidget, QPushButton, QFileDialog, QTextEdit
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent


# ================== 核心分析逻辑（增强版） ==================
def analyze_mod_jar(file_path):
    """返回 (状态描述, 详细原因)"""
    if not os.path.isfile(file_path):
        return "错误", "文件不存在"

    # 初始化特征
    has_registry = False
    has_client_ref = False
    has_server_ref = False
    has_isolation = False
    has_assets = False
    has_data = False

    # 更全面的注册表关键词
    registry_patterns = [
        rb'Registry\.register',
        rb'DeferredRegister',
        rb'Registry\.BLOCK',
        rb'Registry\.ITEM',
        rb'Registry\.ENTITY',
        rb'Registry\.BIOME',
        rb'GameRegistry\.register',
        rb'registerBlock',
        rb'registerItem',
        rb'registerEntity',
        rb'register\s*\([^)]*Registry\.',   # register(Registry.BLOCK ...)
    ]

    # 客户端类关键词（扩展）
    client_keywords = [
        b'net/minecraft/client',
        b'client/renderer',
        b'client/gui',
        b'client/entity',
        b'client/particle',
        b'client/audio',
        b'client/shader',
    ]

    # 服务端类关键词（扩展）
    server_keywords = [
        b'net/minecraft/server/dedicated',
        b'ServerPlayerEntity',
        b'ServerWorld',
        b'net/minecraft/server/MinecraftServer',
        b'net/minecraft/server/management',
        b'dedicated/DedicatedServer',
    ]

    # 隔离检测关键词（包括运行时判断）
    isolation_patterns = [
        b'OnlyIn',
        b'Environment',
        b'DistExecutor',
        b'FMLEnvironment',
        b'Dist.isClient',
        b'Dist.isServer',
        b'@Environment',
    ]

    # 读取元数据
    side_hint = None  # 'client', 'server', 'both', None
    try:
        with zipfile.ZipFile(file_path, 'r') as jar:
            # 1. Fabric 元数据
            try:
                data = jar.read('fabric.mod.json')
                mod_json = json.loads(data)
                env = mod_json.get('environment')
                if env in ('client', 'server', 'both'):
                    side_hint = env
            except KeyError:
                pass

            # 2. Forge mods.toml（简单提取 side）
            try:
                data = jar.read('META-INF/mods.toml')
                # 用正则查找 side = "client" 或 side = "server"
                for line in data.decode('utf-8', errors='ignore').splitlines():
                    m = re.match(r'\s*side\s*=\s*"(\w+)"', line)
                    if m:
                        side_hint = m.group(1)
                        break
            except KeyError:
                pass

            # 扫描 .class 文件
            for info in jar.infolist():
                name = info.filename
                if name.startswith('assets/') and not name.endswith('/'):
                    has_assets = True
                if name.startswith('data/') and not name.endswith('/'):
                    has_data = True
                if not name.endswith('.class'):
                    continue
                try:
                    data = jar.read(name)
                except:
                    continue

                # 注册表检测
                if not has_registry:
                    for pat in registry_patterns:
                        if re.search(pat, data, re.IGNORECASE):
                            has_registry = True
                            break

                # 客户端引用
                if not has_client_ref:
                    for kw in client_keywords:
                        if kw in data:
                            has_client_ref = True
                            break

                # 服务端引用
                if not has_server_ref:
                    for kw in server_keywords:
                        if kw in data:
                            has_server_ref = True
                            break

                # 隔离检测
                if not has_isolation:
                    for pat in isolation_patterns:
                        if re.search(pat, data, re.IGNORECASE):
                            has_isolation = True
                            break

                # 所有特征都找到则提前结束扫描（但不能跳出资源检查，所以只标记）
                if has_registry and has_client_ref and has_server_ref and has_isolation:
                    # 继续但要跳出循环？这里为了简洁，不跳出，因为大多模组不大。
                    pass
    except zipfile.BadZipFile:
        return "错误", "无效的 JAR 文件（无法解压）"

    # ---------- 分类决策 ----------
    # 判断是否有“危险”引用（未隔离）
    server_danger = has_client_ref and not has_isolation
    client_danger = has_server_ref and not has_isolation

    # 1. 优先信任元数据（若明确且未被危险引用矛盾）
    if side_hint:
        # 如果元数据说是 client，但服务器有危险，则服务器无效；如果服务端无危险，则服务器可选
        if side_hint == 'client':
            # 客户端倾向：客户端可能必装（功能上），服务端看是否有危险
            client_status = "必装" if (has_registry or has_client_ref) else "可选"
            server_status = "无效" if server_danger else "可选"
            # 如果有注册表，则客户端为协议必装，服务端也为必装（但服务端危险则无效）
            if has_registry:
                client_status = "必装"
                server_status = "必装" if not server_danger else "无效"
            reason = f"元数据标注为 client，且{'检测到注册表' if has_registry else '未检测到注册表'}"
            return f"服务端{server_status}，客户端{client_status}", reason
        elif side_hint == 'server':
            server_status = "必装" if (has_registry or has_server_ref) else "可选"
            client_status = "无效" if client_danger else "可选"
            if has_registry:
                server_status = "必装"
                client_status = "必装" if not client_danger else "无效"
            reason = f"元数据标注为 server，且{'检测到注册表' if has_registry else '未检测到注册表'}"
            return f"服务端{server_status}，客户端{client_status}", reason
        # 'both' 则按通用逻辑

    # 2. 通用逻辑（无元数据或元数据为 both）
    if has_registry:
        # 有注册表 -> 协议上双端必装
        server_status = "必装"
        client_status = "必装"
        if server_danger:
            server_status = "无效"
        if client_danger:
            client_status = "无效"
        reason = "检测到注册表写入（方块/物品/实体等），需双端同步"
        if server_status == "无效":
            reason += "；且存在未隔离的纯客户端类引用，服务端加载会崩溃"
        if client_status == "无效":
            reason += "；且存在未隔离的纯服务端类引用，客户端加载会崩溃"
        return f"服务端{server_status}，客户端{client_status}", reason
    else:
        # 无注册表 -> 功能型模组
        # 依据引用倾向推断“必装”（功能上）
        # 如果主要引用客户端且无服务端引用，则客户端“必装”（要功能得装），服务端“无效”或“可选”
        if has_client_ref and not has_server_ref:
            # 纯客户端增强
            if server_danger:
                server_status = "无效"
            else:
                server_status = "可选"  # 服务端装了也没用
            client_status = "必装" if has_client_ref else "可选"  # 客户端装了才有功能
            reason = "未检测到注册表，但大量引用客户端渲染/UI类，客户端功能必须；服务端"
            if server_danger:
                reason += "因含客户端类且未隔离，加载会崩溃"
            else:
                reason += "可挂载但不提供功能"
            return f"服务端{server_status}，客户端{client_status}", reason

        elif has_server_ref and not has_client_ref:
            # 纯服务端增强
            if client_danger:
                client_status = "无效"
            else:
                client_status = "可选"
            server_status = "必装" if has_server_ref else "可选"
            reason = "未检测到注册表，但大量引用服务端逻辑类，服务端功能必须；客户端"
            if client_danger:
                reason += "因含服务端类且未隔离，加载会崩溃"
            else:
                reason += "可挂载但不提供功能"
            return f"服务端{server_status}，客户端{client_status}", reason

        else:
            # 无明确倾向或两者都有但均未危险，或全无引用
            server_status = "可选"
            client_status = "可选"
            if server_danger:
                server_status = "无效"
            if client_danger:
                client_status = "无效"
            reason = "未检测到注册表，且无明显的客户端/服务端功能依赖"
            if server_danger and client_danger:
                reason += "；双端都存在互斥代码引用且无隔离，双端均无效"
            elif server_danger:
                reason += "；但含有未隔离的客户端渲染/UI代码，服务端无法加载"
            elif client_danger:
                reason += "；但含有未隔离的服务端专用逻辑，客户端无法加载"
            else:
                reason += "；双端挂载均安全，但不提供实际功能"
            return f"服务端{server_status}，客户端{client_status}", reason


# ================== GUI（与之前相同，调用新分析函数） ==================
class ModAnalyzerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.initUI()

    def initUI(self):
        self.setWindowTitle("Minecraft 模组归属分析器 (v2)")
        self.setGeometry(150, 150, 550, 350)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout()
        central.setLayout(layout)

        self.drop_label = QLabel(
            "将模组文件 (.jar) 拖放到此处\n或点击下方按钮选择"
        )
        self.drop_label.setAlignment(Qt.AlignCenter)
        self.drop_label.setStyleSheet(
            "border: 2px dashed #aaa; padding: 30px; font-size: 14px;"
        )
        self.drop_label.setAcceptDrops(True)
        self.drop_label.installEventFilter(self)
        layout.addWidget(self.drop_label)

        btn_open = QPushButton("选择文件...")
        btn_open.clicked.connect(self.open_file)
        layout.addWidget(btn_open)

        self.result_text = QTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setStyleSheet("font-size: 12px;")
        layout.addWidget(self.result_text)

    def eventFilter(self, obj, event):
        if obj == self.drop_label:
            if event.type() == event.Type.DragEnter:
                if event.mimeData().hasUrls():
                    event.accept()
                    return True
                else:
                    event.ignore()
                    return True
            elif event.type() == event.Type.Drop:
                urls = event.mimeData().urls()
                if urls:
                    file_path = urls[0].toLocalFile()
                    if file_path.lower().endswith('.jar'):
                        self.analyze(file_path)
                    else:
                        self.result_text.setText("⚠️ 请拖入 .jar 格式的文件")
                return True
        return super().eventFilter(obj, event)

    def open_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择模组文件", "", "JAR 文件 (*.jar)"
        )
        if file_path:
            self.analyze(file_path)

    def analyze(self, file_path):
        status, reason = analyze_mod_jar(file_path)
        self.result_text.setText(f"📌 分析结果：{status}\n\n🔎 判定依据：{reason}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = ModAnalyzerGUI()
    window.show()
    sys.exit(app.exec())