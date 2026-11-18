"""本机工作台账号查看码与短时解锁。"""
from __future__ import annotations

import asyncio
import hmac
import secrets
import time
from pathlib import Path
from tempfile import NamedTemporaryFile

from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from solo_auto.errors import ApiError

SESSION_SECONDS = 300


def _digest(code: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(code.encode())


class AccountAccess:
    def __init__(self, home: Path) -> None:
        self.path = home / 'account-view-code'
        self.lock = asyncio.Lock()
        self.failures: list[float] = []
        self.sessions: dict[str, tuple[float, str]] = {}

    @property
    def configured(self) -> bool:
        return self.path.exists()

    def allows(self, token: str) -> bool:
        return not self.configured or self.unlocked(token, settings=True)

    def unlocked(self, token: str, *, settings: bool = False) -> bool:
        expiry, purpose = self.sessions.get(token, (0, ""))
        return expiry > time.monotonic() and (not settings or purpose == "settings")

    async def _verify(self, code: str) -> None:
        now = time.monotonic()
        self.failures = [t for t in self.failures if now - t < 60]
        if len(self.failures) >= 5:
            raise ApiError(429, 'VIEW_CODE_THROTTLED', '尝试次数过多，请在一分钟后重试')
        if not self.configured:
            raise ApiError(409, 'VIEW_CODE_UNSET', '请先设置查看码')
        record = self.path.read_bytes()
        digest = await asyncio.to_thread(_digest, code, record[:16])
        if not hmac.compare_digest(digest, record[16:]):
            self.failures.append(now)
            raise ApiError(403, 'VIEW_CODE_INCORRECT', '查看码不正确')
        self.failures.clear()

    async def configure(self, current: str, new: str) -> None:
        async with self.lock:
            if self.configured:
                await self._verify(current)
            salt = secrets.token_bytes(16)
            digest = await asyncio.to_thread(_digest, new, salt)
            with NamedTemporaryFile(dir=self.path.parent, delete=False) as f:
                f.write(salt + digest)
            Path(f.name).replace(self.path)
            self.sessions.clear()

    async def unlock(self, code: str, *, purpose: str) -> str | None:
        async with self.lock:
            await self._verify(code)
            if purpose == "dispatch":
                return None
            now = time.monotonic()
            self.sessions = {k: v for k, v in self.sessions.items() if v[0] > now}
            token = secrets.token_urlsafe(32)
            self.sessions[token] = (now + SESSION_SECONDS, purpose)
            return token
