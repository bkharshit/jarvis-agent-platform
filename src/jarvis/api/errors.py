"""API error envelope (plan §5): `{"error": {kind, message, details}}`.

Routes raise `ApiError`; `app.py` maps it (plus FastAPI's validation errors
and unexpected exceptions) onto the envelope, so every error response has
one shape."""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """Domain-shaped error raised by routes; handled by the app factory."""

    def __init__(
        self,
        status_code: int,
        kind: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind
        self.message = message
        self.details = details


def envelope(
    kind: str, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"kind": kind, "message": message}
    if details:
        error["details"] = details
    return {"error": error}


__all__ = ["ApiError", "envelope"]
