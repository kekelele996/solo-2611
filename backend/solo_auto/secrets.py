"""Keychain-backed secrets with a private-file fallback."""

from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Any

SERVICE = "solo-auto"


class SecretStore:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.fallback_path = home / "secrets.json"
        self.home.mkdir(parents=True, exist_ok=True)

    def _use_keychain(self) -> bool:
        if os.environ.get("SOLO_AUTO_DISABLE_KEYCHAIN") == "1":
            return False
        return platform.system() == "Darwin" and shutil_which("security") is not None

    def get(self, account: str) -> str | None:
        if self._use_keychain():
            proc = subprocess.run(
                ["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0:
                return proc.stdout.rstrip("\n")
            if proc.returncode not in {44, 1}:
                fallback = self._fallback().get(account)
                if fallback is not None:
                    return fallback
                return None
        return self._fallback().get(account)

    def set(self, account: str, value: str) -> None:
        if self._use_keychain():
            proc = subprocess.run(
                [
                    "security",
                    "add-generic-password",
                    "-U",
                    "-s",
                    SERVICE,
                    "-a",
                    account,
                    "-w",
                    value,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0:
                self._remove_fallback(account)
                return
        self._write_fallback(account, value)

    def delete(self, account: str) -> None:
        if self._use_keychain():
            subprocess.run(
                ["security", "delete-generic-password", "-s", SERVICE, "-a", account],
                capture_output=True,
                text=True,
                check=False,
            )
        self._remove_fallback(account)

    def has(self, account: str) -> bool:
        return bool(self.get(account))

    def get_json(self, account: str, default: Any = None) -> Any:
        value = self.get(account)
        if not value:
            return default
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default

    def set_json(self, account: str, value: Any) -> None:
        self.set(account, json.dumps(value, ensure_ascii=False))

    def _fallback(self) -> dict[str, str]:
        if not self.fallback_path.is_file():
            return {}
        try:
            data = json.loads(self.fallback_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _write_fallback(self, account: str, value: str) -> None:
        data = self._fallback()
        data[account] = value
        self.home.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.fallback_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

    def _remove_fallback(self, account: str) -> None:
        data = self._fallback()
        if account not in data:
            return
        data.pop(account, None)
        if not data:
            self.fallback_path.unlink(missing_ok=True)
            return
        self._write_fallback_next(data)

    def _write_fallback_next(self, data: dict[str, str]) -> None:
        fd = os.open(self.fallback_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")


def shutil_which(command: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / command
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None
