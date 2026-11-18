"""Identities shared by account settings, task routing and the skill bridge."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from solo_auto.context import AppContext
    from solo_auto.settings import Solo2Settings


def origin(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.scheme}://{parts.netloc.lower()}"


def account_key(url: str, username: str) -> str:
    return hashlib.sha256(f"{origin(url)}/{username.strip()}".encode()).hexdigest()


def standby_key(primary: Solo2Settings) -> str:
    return f"solo2.standby.{account_key(primary.base_url, primary.username)}"


def standby_record(ctx: AppContext) -> dict[str, Any] | None:
    primary = ctx.settings.solo2
    record = ctx.secrets.get_json(standby_key(primary)) if primary.username.strip() else None
    return record if isinstance(record, dict) else None


def submission_quota_directory(ctx: AppContext, username: str) -> str:
    return str(ctx.paths.run / "solo2-daily-quota" / account_key(ctx.settings.solo2.base_url, username))
