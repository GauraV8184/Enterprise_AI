"""Verify Supabase Auth access tokens.

Phase 4 replaces the development header stub with real authentication. This
module answers exactly one question - *which user is making this request?* - and
it answers it without trusting anything the caller typed.

How it works
------------
The API is a confidential backend, so it receives the caller's Supabase access
token as ``Authorization: Bearer <token>``. There are two ways to establish who
that token belongs to:

1. Ask the auth server: ``GET /auth/v1/user`` returns the user for a token.
   Authoritative and secret-free, but it puts a network round trip on every
   request and makes the API fail whenever the auth server is slow.
2. Verify the signature locally against the project's published public keys.

This module does (2). The project signs with **ES256** and publishes a JWKS at
``/auth/v1/.well-known/jwks.json``, so verification is a local public-key
operation: there is no shared secret to store or leak, and no per-request
dependency on the auth server being reachable.

Note what is deliberately absent
--------------------------------
* **No HS256.** Symmetric verification would mean holding the project's JWT
  secret. Worse, accepting HS256 *alongside* a JWKS is the classic algorithm
  confusion hole: an attacker signs a token using the published public key as an
  HMAC secret, and a naive verifier happily accepts it. Only asymmetric
  algorithms from a fixed allow-list are accepted, and ``none`` never is.
* **No ``X-User-Id`` fallback.** If a token is present it must verify. Falling
  back to a client-supplied header on failure would turn every authentication
  error into an impersonation bug.

This does not replace Row Level Security, and it is not supposed to. The token
is *forwarded* to PostgREST on every data query, so Postgres independently
re-derives the caller's identity from the same token. A bug here can deny access
it should have allowed - it cannot grant access that Postgres would refuse.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from app.core.config import Settings
from app.core.logging import get_logger, safe_extra
from app.security.errors import (
    AuthServiceUnavailableError,
    ExpiredTokenError,
    InvalidTokenError,
    NotAuthenticatedError,
)
from app.security.identity import UserIdentity

logger = get_logger(__name__)

JWKS_PATH = "/.well-known/jwks.json"

#: The audience Supabase puts on access tokens issued for a signed-in user.
DEFAULT_AUDIENCE = "authenticated"

#: Asymmetric algorithms only. HS* is excluded on purpose - see the module
#: docstring. ``none`` is not in this set, so an unsigned token is rejected
#: before any key lookup happens.
ASYMMETRIC_ALGORITHMS = frozenset(
    {"ES256", "ES384", "ES512", "RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "EdDSA"}
)

#: How long a fetched key set is trusted before being refetched.
DEFAULT_JWKS_TTL_SECONDS = 600.0

#: Timeout for the JWKS request itself. Short: this is on the request path only
#: when the cache is cold or a key has rotated.
DEFAULT_JWKS_TIMEOUT_SECONDS = 10.0

#: Minimum gap between refetches triggered by an *unknown* key id.
#:
#: A token's ``kid`` is unauthenticated input: the signature has not been checked
#: yet when it is read. Without a floor, anyone can send a well-formed token with
#: a random ``kid`` and force a JWKS fetch per request. Rotation still works - the
#: first token naming a new key triggers a fetch - while a flood is limited to one
#: fetch per window.
DEFAULT_UNKNOWN_KID_COOLDOWN_SECONDS = 30.0

#: Tolerance for clock drift between this machine and the auth server. Without
#: it, a token minted a second in the future is rejected as "not yet valid".
CLOCK_SKEW_LEEWAY_SECONDS = 30


@dataclass(frozen=True)
class _SigningKey:
    """A published key plus the algorithm the project says it belongs to."""

    jwk: dict[str, Any]
    declared_algorithm: str | None


class JwksCache:
    """Thread-safe cache of the project's public signing keys.

    FastAPI runs synchronous endpoints in a threadpool, so several requests can
    verify tokens at once and a shared dict of keys is a race waiting to happen.
    The state lock is held only for dictionary reads and writes, never across the
    network: a slow JWKS endpoint must not stall verification of tokens whose keys
    are already cached. Fetches are serialised by a second lock so one refresh
    populates the cache for everyone waiting.

    Rotation is handled by refetching once when a token names a key id that is
    not in the cached set: the project may have signed the token after our last
    fetch. Refetches for unknown ids are rate-limited, because an unknown id is
    attacker-controlled. A key id that is *still* unknown after a successful
    refetch is a token we cannot vouch for, and it is rejected.
    """

    def __init__(
        self,
        url: str,
        *,
        ttl_seconds: float = DEFAULT_JWKS_TTL_SECONDS,
        timeout_seconds: float = DEFAULT_JWKS_TIMEOUT_SECONDS,
        unknown_kid_cooldown_seconds: float = DEFAULT_UNKNOWN_KID_COOLDOWN_SECONDS,
        fetch: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._url = url
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        self._unknown_kid_cooldown = unknown_kid_cooldown_seconds
        # Injectable so tests can supply keys without a network call.
        self._fetch_override = fetch
        # Guards ``_keys`` and ``_loaded_at``; never held across a fetch.
        self._lock = threading.Lock()
        # Serialises fetches so a cold cache produces one request, not one per
        # waiting thread.
        self._refresh_lock = threading.Lock()
        self._keys: dict[str, _SigningKey] = {}
        self._loaded_at = 0.0
        self._last_unknown_kid_refresh = float("-inf")

    # -- fetching ---------------------------------------------------------

    def _http_get(self) -> dict[str, Any]:
        response = httpx.get(self._url, timeout=self._timeout)
        response.raise_for_status()
        return response.json()

    def _fetch(self) -> dict[str, Any]:
        """Retrieve the key set, translating any transport failure.

        The catch is broad on purpose. DNS failure, TLS rejection, a timeout, a
        captive portal returning HTML, a JSON decode error, or the injected
        stub used by tests - every one of them means the same thing to the
        caller: the identity provider cannot be consulted right now, which is a
        503 about the service rather than a 401 about the caller's credentials.
        """
        try:
            payload = self._fetch_override() if self._fetch_override else self._http_get()
        except AuthServiceUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.warning(
                "jwks_fetch_failed",
                extra=safe_extra(url=self._url, error=type(exc).__name__),
            )
            raise AuthServiceUnavailableError(
                f"Could not fetch the signing keys from {self._url}."
            ) from exc

        if not isinstance(payload, dict):
            raise AuthServiceUnavailableError(
                "The signing-key endpoint returned a body that is not a JSON object."
            )
        return payload

    def _refresh(self) -> None:
        payload = self._fetch()
        raw_keys = payload.get("keys") if isinstance(payload, dict) else None
        if not raw_keys:
            raise AuthServiceUnavailableError(
                "The project published no signing keys. Check that Supabase Auth is enabled for it."
            )

        keys: dict[str, _SigningKey] = {}
        for entry in raw_keys:
            if not isinstance(entry, dict):
                continue
            kid = entry.get("kid")
            if not isinstance(kid, str) or not kid:
                # A key with no id cannot be selected by a token, so it is of no
                # use - skip rather than inventing a placeholder id for it.
                continue
            keys[kid] = _SigningKey(jwk=entry, declared_algorithm=entry.get("alg"))

        if not keys:
            raise AuthServiceUnavailableError("The project published no usable signing keys.")

        with self._lock:
            self._keys = keys
            self._loaded_at = time.monotonic()
        logger.info("jwks_refreshed", extra=safe_extra(key_count=len(keys)))

    def _is_stale(self) -> bool:
        return not self._keys or (time.monotonic() - self._loaded_at) > self._ttl

    def _refresh_gracefully(self) -> None:
        """Refresh, but keep serving the previous keys if the fetch fails.

        A stale-but-real key set still verifies correctly; what it cannot do is
        recognise a key that rotated in *after* it was fetched. Refusing to serve
        in that window would turn a Supabase outage into a total API outage for
        every existing token, which is a strictly worse trade.
        """
        try:
            self._refresh()
        except AuthServiceUnavailableError:
            with self._lock:
                has_keys = bool(self._keys)
            if not has_keys:
                raise
            logger.warning(
                "jwks_refresh_failed_using_cached_keys",
                extra=safe_extra(url=self._url),
            )

    def _refresh_once(self, *, force: bool) -> None:
        """Fetch once, serialised across threads.

        ``force`` distinguishes the two reasons to refresh: a stale cache (skip
        if another thread already refreshed it) and an unknown key id (the cache
        may be fresh but incomplete, so it must actually be refetched).
        """
        with self._refresh_lock:
            if not force:
                with self._lock:
                    if not self._is_stale():
                        return
            self._refresh_gracefully()

    def _claim_unknown_kid_refresh(self) -> bool:
        """Reserve the next unknown-id refetch, or report that it is too soon."""
        with self._lock:
            now = time.monotonic()
            if now - self._last_unknown_kid_refresh < self._unknown_kid_cooldown:
                return False
            self._last_unknown_kid_refresh = now
            return True

    def _only_key(self) -> _SigningKey | None:
        """The sole published key, or ``None`` when the choice is ambiguous."""
        if len(self._keys) == 1:
            return next(iter(self._keys.values()))
        return None

    # -- lookup -----------------------------------------------------------

    def get(self, kid: str | None) -> _SigningKey:
        with self._lock:
            stale = self._is_stale()

        if stale:
            self._refresh_once(force=False)

        with self._lock:
            key = self._keys.get(kid) if kid is not None else self._only_key()

        if key is not None:
            return key

        if kid is None:
            # Older Supabase tokens omit `kid`. That is only unambiguous when
            # the project publishes exactly one key.
            raise InvalidTokenError(
                "The token does not name a key, and the project publishes more than one."
            )

        # Possibly rotated. Refetch before giving up, but at most once per
        # cooldown: an unknown id is unauthenticated input.
        if self._claim_unknown_kid_refresh():
            self._refresh_once(force=True)
            with self._lock:
                key = self._keys.get(kid)
            if key is not None:
                return key

        raise InvalidTokenError("The token was signed with a key this project does not publish.")


class SupabaseTokenVerifier:
    """Turns a Supabase access token into a :class:`UserIdentity`."""

    def __init__(
        self,
        settings: Settings,
        *,
        audience: str = DEFAULT_AUDIENCE,
        jwks: JwksCache | None = None,
    ) -> None:
        if not settings.supabase_configured:
            raise ValueError(
                "SupabaseTokenVerifier needs SUPABASE_URL and SUPABASE_ANON_KEY in .env."
            )
        self._issuer = settings.supabase_service_url("auth")
        self._audience = audience
        self._jwks = jwks or JwksCache(f"{self._issuer}{JWKS_PATH}")

    @property
    def issuer(self) -> str:
        return self._issuer

    def verify(self, token: str) -> UserIdentity:
        """Return the identity behind a token, or raise a client-safe error.

        Raises:
            NotAuthenticatedError: no token was supplied.
            InvalidTokenError: the token is malformed, unsigned, signed with an
                unacceptable algorithm, signed by an unknown key, or carries the
                wrong issuer/audience.
            ExpiredTokenError: correctly signed, but out of date.
            AuthServiceUnavailableError: the signing keys could not be fetched.
        """
        if not token or not token.strip():
            raise NotAuthenticatedError("No access token was supplied.")

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidTokenError("The access token is not a well-formed JWT.") from exc

        algorithm = header.get("alg")
        if not isinstance(algorithm, str) or algorithm not in ASYMMETRIC_ALGORITHMS:
            # Covers `none` and every HMAC variant in one check, before any key
            # is looked up or any signature is attempted.
            logger.warning(
                "token_algorithm_rejected",
                extra=safe_extra(algorithm=str(algorithm)),
            )
            raise InvalidTokenError("The access token uses an unsupported signing algorithm.")

        key = self._jwks.get(header.get("kid"))
        if key.declared_algorithm and key.declared_algorithm != algorithm:
            raise InvalidTokenError(
                "The access token's algorithm does not match the published key."
            )

        try:
            signing_key = jwt.PyJWK(key.jwk).key
        except jwt.PyJWTError as exc:
            raise AuthServiceUnavailableError(
                "A published signing key could not be loaded."
            ) from exc

        try:
            claims = jwt.decode(
                token,
                key=signing_key,
                algorithms=[algorithm],  # the one algorithm we already vetted
                audience=self._audience,
                issuer=self._issuer,
                leeway=CLOCK_SKEW_LEEWAY_SECONDS,
                options={"require": ["exp", "sub", "aud", "iss"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise ExpiredTokenError("The access token has expired.") from exc
        except jwt.InvalidTokenError as exc:
            # Wrong signature, wrong issuer, wrong audience, missing claim - all
            # "this token is not ours" from the caller's point of view.
            logger.warning(
                "token_rejected",
                extra=safe_extra(reason=type(exc).__name__),
            )
            raise InvalidTokenError("The access token failed verification.") from exc

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise InvalidTokenError("The access token carries no subject.")

        email = claims.get("email")
        role = claims.get("role")
        return UserIdentity(
            user_id=subject,
            is_verified=True,
            source="supabase_auth",
            email=email if isinstance(email, str) else None,
            role=role if isinstance(role, str) else None,
        )
