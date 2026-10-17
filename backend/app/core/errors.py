"""Application-level error types.

Every error the API intentionally surfaces subclasses :class:`AppError`, which
carries the HTTP status and a *safe* public message. A single exception handler
(``app/api/error_handlers.py``) turns these into JSON responses, so adding a new
error type never requires touching the routers.

Security rule: ``public_message`` is shown to the client, so it must never
contain credentials, stack traces, or internal paths. Full detail belongs in the
logs, not the response.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for expected, client-safe application errors."""

    status_code: int = 500
    code: str = "internal_error"
    public_message: str = "An internal error occurred."

    def __init__(self, message: str | None = None, *, details: dict[str, Any] | None = None):
        self.message = message or self.public_message
        self.details = details or {}
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        """Shape the error for a JSON response body."""
        payload: dict[str, Any] = {"detail": self.message, "code": self.code}
        if self.details:
            payload["details"] = self.details
        return payload
