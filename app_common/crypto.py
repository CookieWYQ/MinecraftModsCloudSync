# -*- coding: utf-8 -*-
"""对称加密工具（Fernet），用于客户端隐秘携带 SFTP 凭据，以及共享文件的密钥加密。"""
import base64
import hashlib
import os
import uuid
import winreg

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

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


# ---------- 共享文件密钥加密（密码 → 随机盐 → Fernet） ----------
_SALT_SIZE = 16
_PBKDF2_ITERATIONS = 200_000


def new_salt() -> bytes:
    """为每个加密共享文件生成随机盐（随清单记录，下载时用于派生密钥）。"""
    return os.urandom(_SALT_SIZE)


def derive_file_key(password: str, salt: bytes) -> bytes:
    """由用户密码 + 盐派生 32 字节 Fernet 密钥（PBKDF2-HMAC-SHA256）。"""
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=salt, iterations=_PBKDF2_ITERATIONS)
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def encrypt_data(data: bytes, password: str, salt: bytes) -> bytes:
    """用密码加密文件内容（Fernet）。"""
    return Fernet(derive_file_key(password, salt)).encrypt(data)


def decrypt_data(data: bytes, password: str, salt: bytes) -> bytes:
    """用密码解密文件内容；密钥错误抛 ValueError。"""
    try:
        return Fernet(derive_file_key(password, salt)).decrypt(data)
    except (InvalidToken, ValueError) as exc:
        raise ValueError("解密失败：密钥不正确或文件已损坏") from exc
