"""Tests for Supabase access-token verification.

No network and no mocks around ``jwt.decode``: each test generates a real ES256
key pair, signs real tokens with it, and hands the verifier a JWKS built from the
matching public key. That means the signature check actually runs - the code path
production uses is the code path under test. Mocking the decode call instead
would assert that our own stub was called, which proves nothing.

The JWKS endpoint is replaced through ``JwksCache(fetch=...)``, which is the one
seam the design deliberately leaves open for exactly this reason.
"""

from __future__ import annotations

import base64
import contextlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwt.algorithms import ECAlgorithm
from starlette.requests import Request as StarletteRequest

from app.core.config import Settings
from app.main import create_app
from app.security.errors import (
    AuthServiceUnavailableError,
    ExpiredTokenError,
    InvalidTokenError,
    NotAuthenticatedError,
)
from app.security.identity import extract_bearer_token, get_current_user
from app.security.supabase_auth import JwksCache, SupabaseTokenVerifier

PROJECT_URL = "https://test-project.supabase.co"
ISSUER = f"{PROJECT_URL}/auth/v1"
AUDR = "authenticated"
SUBJECT = "8f14e45f-ceea-467a-9b2b-1c0f9a4d2e77"


# ---------------------------------------------------------------------------
# Building tokens
# ---------------------------------------------------------------------------


@dataclass
class SigningKeyPair:
    """A private key plus the public JWK a Supabase project would publish."""

    kid: str
    private_key: Any
    jwk: dict[str, Any]


def make_keypair(kid: str = "test-kid") -> SigningKeyPair:
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk: dict[str, Any] = json.loads(ECAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": kid, "alg": "ES256", "use": "sig"})
    return SigningKeyPair(kid=kid, private_key=private_key, jwk=jwk)


def make_claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": SUBJECT,
        "aud": AUDR,
        "iss": ISSUER,
        "iat": now,
        "exp": now + 900,
        "email": "person@example.test",
        "role": "authenticated",
    }
    claims.update(overrides)
    return claims


def sign(
    claims: dict[str, Any],
    keypair: SigningKeyPair,
    *,
    algorithm: str = "ES256",
    headers: dict[str, Any] | None = None,
) -> str:
    """Sign ``claims``. Pass ``headers={}`` to emit a token with no ``kid``."""
    final_headers = {"kid": keypair.kid} if headers is None else headers
    return jwt.encode(claims, keypair.private_key, algorithm=algorithm, headers=final_headers)


def unsigned_token(claims: dict[str, Any]) -> str:
    """A token declaring ``alg: none``, assembled by hand.

    Built directly rather than through ``jwt.encode`` because PyJWT has changed
    how willingly it will produce an unsigned token across versions, and this
    test should not depend on which way that went.
    """

    def segment(payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{segment({'alg': 'none', 'typ': 'JWT'})}.{segment(claims)}."


class FakeJwks:
    """A stand-in for the project's JWKS endpoint, with fault injection."""

    def __init__(self, *keypairs: SigningKeyPair) -> None:
        self.keypairs = list(keypairs)
        self.calls = 0
        self.error: Exception | None = None

    def __call__(self) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"keys": [keypair.jwk for keypair in self.keypairs]}


def make_verifier(*keypairs: SigningKeyPair, ttl_seconds: float = 600.0):
    """A verifier wired to a fake JWKS endpoint. Returns ``(verifier, fake)``."""
    fake = FakeJwks(*keypairs)
    cache = JwksCache(f"{ISSUER}/.well-known/jwks.json", ttl_seconds=ttl_seconds, fetch=fake)
    return SupabaseTokenVerifier(make_settings(), jwks=cache), fake


def make_settings(**overrides: Any) -> Settings:
    return Settings(
        environment="test",
        debug=False,
        log_json=False,
        log_level="CRITICAL",
        supabase_url=PROJECT_URL,
        supabase_anon_key="anon-key-for-tests",
        _env_file=None,
        **overrides,
    )


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_valid_token_produces_a_verified_identity() -> None:
    keypair = make_keypair()
    verifier, _ = make_verifier(keypair)

    identity = verifier.verify(sign(make_claims(), keypair))

    assert identity.user_id == SUBJECT
    assert identity.is_verified is True
    assert identity.source == "supabase_auth"


def test_valid_token_carries_email_and_role() -> None:
    keypair = make_keypair()
    verifier, _ = make_verifier(keypair)

    identity = verifier.verify(sign(make_claims(email="a@b.test", role="authenticated"), keypair))

    assert identity.email == "a@b.test"
    assert identity.role == "authenticated"


def test_tokens_without_email_still_verify() -> None:
    """Phone-only and anonymous sign-ins carry no email claim."""
    keypair = make_keypair()
    verifier, _ = make_verifier(keypair)
    claims = make_claims()
    del claims["email"]

    identity = verifier.verify(sign(claims, keypair))

    assert identity.user_id == SUBJECT
    assert identity.email is None


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


def test_expired_token_reports_expiry_specifically() -> None:
    """A client needs to tell "refresh and retry" from "something is wrong"."""
    keypair = make_keypair()
    verifier, _ = make_verifier(keypair)
    expired = make_claims(exp=int(time.time()) - 3600)

    with pytest.raises(ExpiredTokenError) as caught:
        verifier.verify(sign(expired, keypair))

    assert caught.value.code == "token_expired"
    assert caught.value.status_code == 401


def test_small_clock_skew_is_tolerated() -> None:
    """A token that expired one second ago is accepted, within the leeway.

    Without leeway, a machine whose clock is a moment behind the auth server
    rejects freshly minted tokens and the failure looks random.
    """
    keypair = make_keypair()
    verifier, _ = make_verifier(keypair)

    identity = verifier.verify(sign(make_claims(exp=int(time.time()) - 1), keypair))

    assert identity.user_id == SUBJECT


def test_token_signed_by_a_different_key_is_rejected() -> None:
    published = make_keypair(kid="published")
    attacker = make_keypair(kid="published")  # same kid, different private key
    verifier, _ = make_verifier(published)

    with pytest.raises(InvalidTokenError):
        verifier.verify(sign(make_claims(), attacker))


def test_tampered_payload_is_rejected() -> None:
    """Re-encoding the payload must invalidate the signature."""
    keypair = make_keypair()
    verifier, _ = make_verifier(keypair)
    token = sign(make_claims(), keypair)

    header, payload, signature = token.split(".")
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    decoded["sub"] = "00000000-0000-0000-0000-000000000000"
    forged_payload = (
        base64.urlsafe_b64encode(json.dumps(decoded, separators=(",", ":")).encode())
        .rstrip(b"=")
        .decode()
    )

    with pytest.raises(InvalidTokenError):
        verifier.verify(f"{header}.{forged_payload}.{signature}")


def test_wrong_issuer_is_rejected() -> None:
    """A token minted by a *different* Supabase project must not be accepted."""
    keypair = make_keypair()
    verifier, _ = make_verifier(keypair)
    foreign = make_claims(iss="https://someone-else.supabase.co/auth/v1")

    with pytest.raises(InvalidTokenError):
        verifier.verify(sign(foreign, keypair))


def test_wrong_audience_is_rejected() -> None:
    keypair = make_keypair()
    verifier, _ = make_verifier(keypair)

    with pytest.raises(InvalidTokenError):
        verifier.verify(sign(make_claims(aud="service_role"), keypair))


def test_unsigned_token_is_rejected() -> None:
    """The classic `alg: none` downgrade."""
    keypair = make_keypair()
    verifier, _ = make_verifier(keypair)

    with pytest.raises(InvalidTokenError):
        verifier.verify(unsigned_token(make_claims()))


def test_hs256_token_is_rejected_even_when_signed_with_the_public_key() -> None:
    """The algorithm-confusion attack, explicitly.

    A naive verifier that accepts whatever ``alg`` the token names, and uses the
    published *public* key as an HMAC secret, will accept this token. The verifier
    must refuse on the algorithm before any key is consulted.
    """
    keypair = make_keypair()
    verifier, fake = make_verifier(keypair)
    # The attacker's "secret" is the project's published public key material.
    public_pem = keypair.jwk["x"] + keypair.jwk["y"]
    forged = jwt.encode(make_claims(), public_pem, algorithm="HS256", headers={"kid": keypair.kid})

    with pytest.raises(InvalidTokenError):
        verifier.verify(forged)

    assert fake.calls == 0, "the algorithm check must run before any key lookup"


def test_token_with_an_unknown_kid_is_rejected() -> None:
    keypair = make_keypair(kid="real")
    verifier, _ = make_verifier(keypair)

    with pytest.raises(InvalidTokenError):
        verifier.verify(sign(make_claims(), keypair, headers={"kid": "invented"}))


def test_token_without_kid_is_rejected_when_several_keys_are_published() -> None:
    """With more than one candidate the correct key is genuinely ambiguous."""
    first, second = make_keypair("kid-1"), make_keypair("kid-2")
    verifier, _ = make_verifier(first, second)

    with pytest.raises(InvalidTokenError):
        verifier.verify(sign(make_claims(), first, headers={}))


def test_token_without_kid_is_accepted_when_only_one_key_is_published() -> None:
    """Older Supabase tokens omit `kid`; with one key there is no ambiguity."""
    keypair = make_keypair()
    verifier, _ = make_verifier(keypair)

    identity = verifier.verify(sign(make_claims(), keypair, headers={}))

    assert identity.user_id == SUBJECT


def test_garbage_token_is_rejected() -> None:
    verifier, fake = make_verifier(make_keypair())

    with pytest.raises(InvalidTokenError):
        verifier.verify("not-a-jwt-at-all")

    assert fake.calls == 0


def test_blank_token_is_rejected() -> None:
    verifier, _ = make_verifier(make_keypair())

    with pytest.raises(NotAuthenticatedError):
        verifier.verify("   ")


def test_token_without_subject_is_rejected() -> None:
    keypair = make_keypair()
    verifier, _ = make_verifier(keypair)
    claims = make_claims()
    del claims["sub"]

    # `sub` is in the required-claims list, so decode itself refuses this.
    with pytest.raises(InvalidTokenError):
        verifier.verify(sign(claims, keypair))


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------


def test_signing_keys_are_fetched_once_for_many_tokens() -> None:
    """The cache exists so verification does not become a network call."""
    keypair = make_keypair()
    verifier, fake = make_verifier(keypair)

    for _ in range(5):
        verifier.verify(sign(make_claims(), keypair))

    assert fake.calls == 1


def test_a_rotated_key_is_picked_up_automatically() -> None:
    """After rotation the token names a kid we have never seen.

    The cache must refetch rather than reject, otherwise every user is locked out
    at the moment the project rotates its keys.
    """
    old = make_keypair(kid="old")
    verifier, fake = make_verifier(old)

    # Warm the cache with the pre-rotation key set.
    verifier.verify(sign(make_claims(), old))
    assert fake.calls == 1

    new = make_keypair(kid="new")
    fake.keypairs = [new]

    identity = verifier.verify(sign(make_claims(), new))

    assert identity.user_id == SUBJECT
    assert fake.calls == 2


def test_expired_cache_is_refreshed() -> None:
    keypair = make_keypair()
    verifier, fake = make_verifier(keypair, ttl_seconds=0.0)

    verifier.verify(sign(make_claims(), keypair))
    verifier.verify(sign(make_claims(), keypair))

    assert fake.calls == 2


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def test_jwks_outage_with_a_cold_cache_is_a_503_not_a_401() -> None:
    """An outage must not be reported as bad credentials.

    A 401 sends the user to a login page that cannot work either; a 503 tells the
    client to retry.
    """
    keypair = make_keypair()
    verifier, fake = make_verifier(keypair)
    fake.error = RuntimeError("connection refused")

    with pytest.raises(AuthServiceUnavailableError) as caught:
        verifier.verify(sign(make_claims(), keypair))

    assert caught.value.status_code == 503


def test_jwks_outage_with_a_warm_cache_still_verifies() -> None:
    """Stale-but-real keys are better than locking everyone out mid-incident.

    The cached keys still verify correctly; all that is lost is the ability to
    recognise a key rotated in after the cache was filled, which fails closed.
    """
    keypair = make_keypair()
    verifier, fake = make_verifier(keypair, ttl_seconds=0.0)
    verifier.verify(sign(make_claims(), keypair))  # warm the cache

    fake.error = RuntimeError("connection refused")
    identity = verifier.verify(sign(make_claims(), keypair))

    assert identity.user_id == SUBJECT


def test_project_publishing_no_keys_is_an_error_not_a_silent_accept() -> None:
    verifier, fake = make_verifier()
    fake.keypairs = []

    with pytest.raises(AuthServiceUnavailableError):
        verifier.verify(sign(make_claims(), make_keypair()))


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("Bearer abc123", "abc123"),
        ("bearer abc123", "abc123"),
        ("BEARER abc123", "abc123"),
        ("  Bearer   abc123  ", "abc123"),
        ("Basic abc123", None),
        ("abc123", None),
        ("Bearer", None),
        ("Bearer    ", None),
    ],
)
def test_bearer_token_extraction(header: str | None, expected: str | None) -> None:
    assert extract_bearer_token(header) == expected


# ---------------------------------------------------------------------------
# Wiring into the request pipeline
# ---------------------------------------------------------------------------


@pytest.fixture
def supabase_settings(tmp_path: Path) -> Settings:
    """Supabase Auth enabled, but documents still on the local SQLite store.

    Keeping the store local isolates this test to the identity decision, which is
    the only thing under test here.
    """
    return Settings(
        environment="test",
        debug=False,
        log_json=False,
        log_level="CRITICAL",
        data_dir=tmp_path / "data",
        supabase_url=PROJECT_URL,
        supabase_anon_key="anon-key-for-tests",
        document_store="sqlite",
        _env_file=None,
    )


@pytest.fixture
def supabase_app(supabase_settings: Settings) -> FastAPI:
    return create_app(supabase_settings)


@contextlib.contextmanager
def client_with_verifier(app: FastAPI, verifier: SupabaseTokenVerifier):
    """A started TestClient whose app verifies tokens with ``verifier``.

    The verifier is attached *after* startup, the same way the existing fixtures
    install fake models, so whatever the real lifespan builds cannot overwrite it.
    """
    with TestClient(app) as client:
        app.state.token_verifier = verifier
        yield client


def test_request_without_a_token_is_refused(supabase_app: FastAPI) -> None:
    verifier, _ = make_verifier(make_keypair())
    with client_with_verifier(supabase_app, verifier) as client:
        response = client.get("/api/documents")

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


def test_request_with_an_invalid_token_is_refused(supabase_app: FastAPI) -> None:
    verifier, _ = make_verifier(make_keypair())
    with client_with_verifier(supabase_app, verifier) as client:
        response = client.get(
            "/api/documents", headers={"Authorization": f"Bearer {unsigned_token(make_claims())}"}
        )

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


def test_request_with_a_valid_token_is_scoped_to_that_user(supabase_app: FastAPI) -> None:
    keypair = make_keypair()
    verifier, _ = make_verifier(keypair)
    token = sign(make_claims(), keypair)

    with client_with_verifier(supabase_app, verifier) as client:
        response = client.get("/api/documents", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"documents": [], "total": 0}


def test_the_header_stub_is_ignored_once_supabase_auth_is_on(supabase_app: FastAPI) -> None:
    """X-User-Id must not be a way in when real authentication is configured."""
    verifier, _ = make_verifier(make_keypair())
    with client_with_verifier(supabase_app, verifier) as client:
        response = client.get("/api/documents", headers={"X-User-Id": "somebody-else"})

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


def test_missing_verifier_fails_closed_rather_than_falling_back(
    supabase_app: FastAPI,
) -> None:
    """Configured for Supabase Auth but nothing to verify with.

    Degrading to the header stub here would silently trust an unverified header
    in an environment that explicitly asked for real authentication.
    """
    with TestClient(supabase_app) as client:
        supabase_app.state.token_verifier = None
        response = client.get("/api/documents", headers={"X-User-Id": "somebody-else"})

    assert response.status_code == 503
    assert response.json()["code"] == "authentication_not_configured"


def test_dev_stub_is_used_when_supabase_is_not_configured(tmp_path: Path) -> None:
    """The Phase 3 behaviour must survive for a checkout with no credentials."""
    app = create_app(dev_settings(tmp_path))

    with TestClient(app) as client:
        response = client.get("/api/documents", headers={"X-User-Id": "alice"})

    assert response.status_code == 200


def test_dev_stub_marks_the_identity_as_unverified(tmp_path: Path) -> None:
    app = create_app(dev_settings(tmp_path))
    request = starlette_request(app, {"X-User-Id": "alice"})

    identity = get_current_user(request)

    assert identity.user_id == "alice"
    assert identity.is_verified is False
    assert identity.source == "dev_header"


def dev_settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        log_json=False,
        log_level="CRITICAL",
        data_dir=tmp_path / "data",
        _env_file=None,
    )


def starlette_request(app: FastAPI, headers: dict[str, str]) -> StarletteRequest:
    """A real request object, so the dependency is exercised as FastAPI calls it."""
    return StarletteRequest(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/documents",
            "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
            "app": app,
        }
    )
