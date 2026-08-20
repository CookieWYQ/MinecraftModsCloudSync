# -*- coding: utf-8 -*-
"""对称加密工具（Fernet），用于客户端隐秘携带 SFTP 凭据。"""
import base64
import hashlib
import uuid
import winreg

from cryptography.fernet import Fernet, InvalidToken

from .constants import CRYPTO_SALT


def get_machine_id() -> str:
    """读取 Windows MachineGuid 作为机器唯一标识，失败时回退为随机 UUID。"""
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
        ) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value)
    except Exception:
        return str(uuid.uuid4())


def _derive_key(secret: str) -> bytes:
    raw = hashlib.sha256((secret + CRYPTO_SALT).encode("utf-8")).digest()
    return base64.urlsafe_b64encode(raw)


class CryptoBox:
    """使用给定密钥进行加解密。密钥相同才能互相解密。"""

    def __init__(self, secret: str):
        if not secret:
            secret = get_machine_id()
        self._fernet = Fernet(_derive_key(secret))

    def encrypt(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise ValueError("解密失败：密钥不匹配或数据被篡改") from exc
