# -*- coding: utf-8 -*-
"""客户端唯一编号授权：编号清单（license.json）存于 SFTP 根目录。"""
import json

from .logger import get_logger
from .sftp import SFTPError, SFTPManager

log = get_logger("license")

LICENSE_FILENAME = "license.json"
# 授权清单固定存放于 SFTP 根目录，便于两端定位
LICENSE_REMOTE_PATH = "/" + LICENSE_FILENAME


class AuthError(Exception):
    """客户端唯一编号未获授权。"""


def load_ids(sftp: SFTPManager) -> list[str]:
    """读取服务器上已授权的唯一编号列表。"""
    if not sftp.exists(LICENSE_REMOTE_PATH):
        return []
    try:
        data = json.loads(sftp.read_text(LICENSE_REMOTE_PATH))
        ids = data.get("ids", [])
        return [str(i) for i in ids] if isinstance(ids, list) else []
    except SFTPError:
        raise
    except Exception as exc:
        raise SFTPError(f"解析授权清单失败: {exc}") from exc


def save_ids(sftp: SFTPManager, ids: list[str]) -> None:
    """将唯一编号列表同步到 SFTP 根目录。"""
    data = {"ids": list(ids)}
    sftp.write_text(LICENSE_REMOTE_PATH, json.dumps(data, ensure_ascii=False, indent=2))


def verify(sftp: SFTPManager, unique_id: str) -> None:
    """校验客户端唯一编号：不在服务器授权清单中则抛出 AuthError。"""
    ids = load_ids(sftp)
    if not unique_id:
        raise AuthError("客户端缺少唯一编号，无法连接。")
    if not ids:
        raise AuthError("服务器尚未同步任何客户端编号，请先在服务端执行「同步编号到服务器」。")
    if unique_id not in ids:
        raise AuthError("客户端唯一编号未获授权，连接被拒绝。")
    log.info("客户端编号授权校验通过")
