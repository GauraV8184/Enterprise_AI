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


class InvalidTokenError(AppError):
    """The access token failed verification.

    The message is deliberately vague about *which* check failed. Telling a
    caller "correct signature, wrong audience" is only useful to someone probing
    the API, and the person holding a legitimate token has the auth server's own
    error messages to work with.
    """

    status_code = 401
    code = "invalid_token"
    public_message = "Your access token is not valid. Sign in again."


class ExpiredTokenError(AppError):
    """The access token is well-formed and correctly signed, but out of date.

    This one *is* worth being specific about: a client needs to tell "refresh and
    retry" apart from "something is wrong", and an expired token is the single
    most common cause of a 401 in normal operation.
    """

    status_code = 401
    code = "token_expired"
    public_message = "Your access token has expired. Refresh it and try again."


class AuthServiceUnavailableError(AppError):
    """The identity provider could not be reached or did not answer usefully.

    A 503 rather than a 401 on purpose: an outage should not be reported to the
    client as "your credentials are bad", which would send users to a login page
    that cannot work either.
    """

    status_code = 503
    code = "auth_unavailable"
    public_message = "The authentication service is temporarily unavailable. Try again shortly."
