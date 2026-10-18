"""Authentication and authorisation errors."""

from __future__ import annotations

from app.core.errors import AppError


class NotAuthenticatedError(AppError):
    """No usable identity on the request."""

    status_code = 401
    code = "not_authenticated"
    public_message = "Authentication is required for this endpoint."


class AuthenticationNotAvailableError(AppError):
    """Identity was requested in a mode where it cannot be verified.

    Raised instead of silently trusting a client-supplied identity. Failing
    closed is the only safe behaviour when real authentication is absent.
    """

    status_code = 503
    code = "authentication_not_configured"
    public_message = (
        "This deployment has no authentication configured. Supabase Auth is wired "
        "up in a later phase; until then the API refuses to serve production traffic."
    )


class AccessDeniedError(AppError):
    """The caller is authenticated but not allowed to perform the action."""

    status_code = 403
    code = "access_denied"
    public_message = "You do not have permission to perform this action."
