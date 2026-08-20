# -*- coding: utf-8 -*-
"""服务器配置文件：服务端导出、客户端导入（.mcscf）。

每个服务器对应一份档案：人类可读名称 + 机器可读唯一编号(UUID) + 加密的 SFTP 信息。
配置文件采用随机密钥加密，客户端导入后即可解密使用；唯一编号用于连接时的授权校验。
"""
import json
import uuid
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken

from .logger import get_logger

log = get_logger("profile")

PROFILE_TYPE = "mc_cloud_sync_profile"
PROFILE_SCHEMA = 1
PROFILE_SUFFIX = ".mcscf"
PROFILE_FILTER = f"服务器配置文件 (*{PROFILE_SUFFIX})"


def new_server_id() -> str:
    """生成机器可读唯一编号（UUID）。"""
    return str(uuid.uuid4())


def build_profile_content(name: str, server_id: str, sftp_info: dict) -> str:
    """将服务器信息打包为可分发、可导入的配置文件内容（随机密钥加密）。"""
    key = Fernet.generate_key().decode("ascii")
    fernet = Fernet(key.encode("ascii"))
    payload = json.dumps({"sftp": sftp_info}, ensure_ascii=False)
    encrypted = fernet.encrypt(payload.encode("utf-8")).decode("ascii")
    data = {
        "type": PROFILE_TYPE,
        "schema": PROFILE_SCHEMA,
        "name": name,
        "server_id": server_id,
        "created_at": datetime.now().astimezone().isoformat(),
        "key": key,
        "encrypted_sftp": encrypted,
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def parse_profile_content(content: str) -> dict:
    """解析并解密客户端导入的配置文件，返回 {name, server_id, created_at, sftp}。"""
    try:
        data = json.loads(content)
    except Exception as exc:
        raise ValueError("文件不是有效的 JSON 配置") from exc

    if data.get("type") != PROFILE_TYPE:
        raise ValueError("不是有效的服务器配置文件")
    if int(data.get("schema", 0)) != PROFILE_SCHEMA:
        raise ValueError("配置文件版本不受支持")
    name = str(data.get("name", "")).strip()
    server_id = str(data.get("server_id", "")).strip()
    key = data.get("key")
    encrypted = data.get("encrypted_sftp")
    if not name:
        raise ValueError("配置缺少服务器名称")
    if not server_id:
        raise ValueError("配置缺少唯一编号")
    if not key or not encrypted:
        raise ValueError("配置缺少加密数据")
    try:
        payload = json.loads(
            Fernet(key.encode("ascii")).decrypt(encrypted.encode("ascii")).decode("utf-8")
        )
    except (InvalidToken, ValueError, TypeError) as exc:
        raise ValueError("配置解密失败，文件可能已损坏或被篡改") from exc
    sftp = payload.get("sftp")
    if not isinstance(sftp, dict) or not sftp.get("host"):
        raise ValueError("配置缺少服务器地址")
    return {
        "name": name,
        "server_id": server_id,
        "created_at": data.get("created_at", ""),
        "sftp": sftp,
    }
