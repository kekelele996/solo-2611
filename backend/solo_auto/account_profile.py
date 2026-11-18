"""Portable account settings. Secret values occur only inside authenticated encryption."""
from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

FORMAT = "solo-auto-accounts-v1"


def cipher(password: str, salt: bytes) -> AESGCM:
    key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())
    return AESGCM(key)


def pack(profile: dict[str, Any], password: str) -> dict[str, Any]:
    if not password:
        return {"format": FORMAT, "profile": profile}
    salt, nonce = os.urandom(16), os.urandom(12)
    encrypted = cipher(password, salt).encrypt(nonce, json.dumps(profile).encode(), FORMAT.encode())
    return {"format": FORMAT, "encrypted": base64.b64encode(salt + nonce + encrypted).decode()}


def unpack(bundle: dict[str, Any], password: str) -> dict[str, Any]:
    if not isinstance(bundle, dict) or not isinstance(password, str) or bundle.get("format") != FORMAT:
        raise ValueError("配置文件版本不支持")
    if "encrypted" in bundle:
        raw = base64.b64decode(bundle["encrypted"], validate=True)
        plain = cipher(password, raw[:16]).decrypt(raw[16:28], raw[28:], FORMAT.encode())
        profile = json.loads(plain)
        if not isinstance(profile, dict):
            raise ValueError("配置内容无效")
        return profile
    profile = bundle["profile"]
    if not isinstance(profile, dict) or "secrets" in profile:
        raise ValueError("凭据需要加密备份")
    return profile
