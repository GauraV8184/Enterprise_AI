"""Who is making this request.

Two mechanisms live here, and which one runs is decided by configuration:

* **Supabase Auth** (``settings.use_supabase_auth``). The caller sends
  ``Authorization: Bearer <access token>``, and
  :class:`app.security.supabase_auth.SupabaseTokenVerifier` checks the signature
  against the project's published public keys. This is the real thing: the
  identity is cryptographically attested and ``is_verified`` is True.

* **A development stub**. The caller may name themselves with an ``X-User-Id``
  header, or fall back to ``settings.dev_user_id``. This is *not* a security
  control - anyone can send that header - and it exists only so the pipeline can
  be exercised without an account. It is refused outside ``development`` and
  ``test``.

The stub is why this module's history is worth a sentence: ownership filtering
was enforced in the data layer from the very first endpoint, before there was any
real authentication. Phase 4 did not have to add tenant isolation; it only had to
make the identity trustworthy, and the enforcement points never moved.

Note the division of labour. Verifying a token here establishes *who* the caller
is. It does **not** by itself protect any data: the same token is forwarded to
PostgREST on every query, and Postgres re-derives the identity from it and
applies Row Level Security. A flaw in this module can deny a caller access it
should have had; it cannot hand out access Postgres would refuse.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Protocol

from fastapi import Depends, Request

from app.core.config import Settings, get_settings
from app.security.errors import AuthenticationNotAvailableError, NotAuthenticatedError

USER_ID_HEADER = "X-User-Id"
AUTHORIZATION_HEADER = "Authorization"
_MAX_USER_ID_LENGTH = 128
_BEARER_PREFIX = "bearer "


@dataclass(frozen=True)
class UserIdentity:
    """The acting user for the current request."""

    user_id: str
    is_verified: bool
    """True only when the identity came from a real authentication mechanism.

    False for the development stub. Anything with a security consequence - audit
    logging, administrative actions - must check this rather than assume.
    """
    source: str
    email: str | None = None
    role: str | None = None


class TokenVerifier(Protocol):
    """What :func:`get_current_user` needs from an auth backend.

    Declared structurally so this module never has to import the Supabase
    implementation, which in turn imports :class:`UserIdentity` from here.
    """

    def verify(self, token: str) -> UserIdentity: ...


def extract_bearer_token(authorization_header: str | None) -> str | None:
    """Pull the token out of an ``Authorization`` header.

    Returns ``None`` when the header is missing or uses a different scheme, so
    the caller decides whether that is fatal rather than this function guessing.
    """
    if not authorization_header:
        return None
    candidate = authorization_header.strip()
    if not candidate.lower().startswith(_BEARER_PREFIX):
        return None
    token = candidate[len(_BEARER_PREFIX) :].strip()
    return token or None


def _resolve_dev_user_id(settings: Settings, request: Request) -> tuple[str, str]:
    header_value = request.headers.get(USER_ID_HEADER)
    if header_value is not None:
        candidate = header_value.strip()
        if not candidate:
            raise NotAuthenticatedError(f"The {USER_ID_HEADER} header was sent but is empty.")
        if len(candidate) > _MAX_USER_ID_LENGTH:
            raise NotAuthenticatedError(f"The {USER_ID_HEADER} header is too long.")
        return candidate, "dev_header"
    return settings.dev_user_id, "dev_default"


def _settings_for(request: Request) -> Settings:
    """The settings of the *running application*, not the process-wide defaults.

    ``get_settings()`` is ``lru_cache``d and reads the developer's real ``.env``.
    Consulting it here would let the identity decision disagree with the rest of
    the request about which environment is being served - a test app configured
    for development would still see a production ``.env``, or vice versa, and one
    of those two answers means trusting an unverified header.

    Falls back to the global only when there is no application to ask, which is
    the case for a request object built by hand in a unit test.
    """
    application = request.scope.get("app")
    settings = getattr(getattr(application, "state", None), "settings", None)
    return settings if isinstance(settings, Settings) else get_settings()


def get_current_user(request: Request) -> UserIdentity:
    """Resolve the acting user, refusing to guess when real auth is unavailable."""
    settings = _settings_for(request)

    if settings.use_supabase_auth:
        verifier: TokenVerifier | None = getattr(request.app.state, "token_verifier", None)
        if verifier is None:
            # Configured for Supabase Auth but the verifier never got built. Fail
            # closed rather than degrading to the stub, which would mean silently
            # trusting a header in an environment that asked for real auth.
            raise AuthenticationNotAvailableError(
                "Supabase Auth is configured but no token verifier was initialised."
            )

        token = extract_bearer_token(request.headers.get(AUTHORIZATION_HEADER))
        if token is None:
            raise NotAuthenticatedError(
                "This endpoint requires authentication. Send a Supabase access token as "
                "'Authorization: Bearer <token>'."
            )
        return verifier.verify(token)

    if not settings.allows_dev_auth:
        # Reachable only when a deployed environment was configured without
        # Supabase Auth, which startup validation rejects. Kept as a second line
        # of defence: serving an unverified header outside development would be
        # worse than not serving at all.
        raise AuthenticationNotAvailableError()

    user_id, source = _resolve_dev_user_id(settings, request)
    return UserIdentity(user_id=user_id, is_verified=False, source=source)


CurrentUserDep = Annotated[UserIdentity, Depends(get_current_user)]
