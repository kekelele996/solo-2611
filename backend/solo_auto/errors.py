"""Application exceptions with stable machine-readable codes."""

from __future__ import annotations

from typing import Any


class ApiError(RuntimeError):
    """An expected API failure rendered through the common error envelope."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        detail: dict[str, Any] | list[Any] | str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail


class SkillInvocationError(RuntimeError):
    """The skill returned an environment or command failure."""

    def __init__(self, message: str, *, code: int, stdout: str, stderr: str) -> None:
        super().__init__(message)
        self.code = code
        self.stdout = stdout
        self.stderr = stderr


class SourceOfflineError(RuntimeError):
    """Project Hub is unavailable."""


class SourceAuthError(RuntimeError):
    """Project Hub rejected the configured session."""
