"""Who is making this request.

Phase 3 has **no real authentication** - Supabase Auth arrives in Phase 4. This
module exists so that ownership is enforced from the very first endpoint rather
than bolted on later, which is how cross-tenant data leaks happen.

How it works today:

* The caller may identify themselves with an ``X-User-Id`` header. This is a
  development convenience for testing multi-user isolation, **not a security
  control** - anyone can send that header.
* Without the header, ``settings.dev_user_id`` is used.
* In ``ENVIRONMENT=production`` this module refuses to produce an identity at all
  (:class:`AuthenticationNotAvailableError`). Failing closed means an
  accidentally exposed deployment cannot serve data to an unidentified caller.

What this does buy you right now: every storage query is filtered by owner id, so
the *data layer* is already tenant-scoped. Phase 4 only has to replace how the
identity is derived; the enforcement points do not move.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings, get_settings
from app.security.errors import AuthenticationNotAvailableError, NotAuthenticatedError

USER_ID_HEADER = "X-User-Id"
_MAX_USER_ID_LENGTH = 128


@dataclass(frozen=True)
class UserIdentity:
    """The acting user for the current request."""

    user_id: str
    is_verified: bool
    """True only when the identity came from a real authentication mechanism.

    Always False in Phase 3. Callers that need certainty (audit logging, admin
    actions) must check this rather than assuming.
    """
    source: str


def _resolve_user_id(settings: Settings, request: Request) -> str:
    header_value = request.headers.get(USER_ID_HEADER)
    if header_value is not None:
        candidate = header_value.strip()
        if not candidate:
            raise NotAuthenticatedError(f"The {USER_ID_HEADER} header was sent but is empty.")
        if len(candidate) > _MAX_USER_ID_LENGTH:
            raise NotAuthenticatedError(f"The {USER_ID_HEADER} header is too long.")
        return candidate
    return settings.dev_user_id


def get_current_user(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> UserIdentity:
    """Resolve the acting user, refusing to guess in production."""
    if settings.is_production:
        raise AuthenticationNotAvailableError()

    return UserIdentity(
        user_id=_resolve_user_id(settings, request),
        is_verified=False,
        source=USER_ID_HEADER if request.headers.get(USER_ID_HEADER) else "default",
    )


CurrentUserDep = Annotated[UserIdentity, Depends(get_current_user)]
