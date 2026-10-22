"""Per-identity request budgets, unit and endpoint level.

The window is 60 seconds, so these tests never sleep: they set the limits low
and make one request more than the budget. Time-based expiry is not tested by
waiting; the pruning logic is visible in the implementation and the retry hint
is asserted from the oldest hit.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.security.rate_limit import (
    MODEL_TIER,
    STANDARD_TIER,
    RateLimitedError,
    RateLimiter,
)


def limiter(*, standard: int = 10, model: int = 10, enabled: bool = True) -> RateLimiter:
    return RateLimiter(
        enabled=enabled,
        standard_per_minute=standard,
        model_per_minute=model,
    )


def test_the_budget_is_exact_then_rejected() -> None:
    rate_limiter = limiter(standard=2)

    rate_limiter.check(identity="user-1", tier=STANDARD_TIER)
    rate_limiter.check(identity="user-1", tier=STANDARD_TIER)

    with pytest.raises(RateLimitedError) as caught:
        rate_limiter.check(identity="user-1", tier=STANDARD_TIER)

    error = caught.value
    assert error.status_code == 429
    assert error.code == "rate_limited"
    assert 1 <= int(error.headers["Retry-After"]) <= 60
    assert error.details["tier"] == STANDARD_TIER
    assert error.details["limit"] == 2


def test_users_and_tiers_have_separate_budgets() -> None:
    rate_limiter = limiter(standard=1, model=1)

    rate_limiter.check(identity="user-1", tier=MODEL_TIER)
    rate_limiter.check(identity="user-2", tier=MODEL_TIER)
    rate_limiter.check(identity="user-1", tier=STANDARD_TIER)

    with pytest.raises(RateLimitedError):
        rate_limiter.check(identity="user-1", tier=MODEL_TIER)


def test_a_disabled_limiter_never_rejects() -> None:
    rate_limiter = limiter(standard=1, model=1, enabled=False)

    for _ in range(5):
        rate_limiter.check(identity="user-1", tier=MODEL_TIER)


def test_a_standard_endpoint_returns_a_429_with_a_retry_hint(
    app: FastAPI, client: TestClient
) -> None:
    app.state.rate_limiter = limiter(standard=2)

    assert client.get("/api/chat/status").status_code == 200
    assert client.get("/api/chat/status").status_code == 200

    response = client.get("/api/chat/status")

    assert response.status_code == 429
    assert response.headers["Retry-After"].isdigit()
    body = response.json()
    assert body["code"] == "rate_limited"
    assert body["details"]["tier"] == STANDARD_TIER
    assert body["request_id"]


def test_a_model_endpoint_charges_the_model_budget(app: FastAPI, llm_client: TestClient) -> None:
    app.state.rate_limiter = limiter(standard=10, model=1)
    payload = {"messages": [{"role": "user", "content": "hello"}]}

    assert llm_client.post("/api/chat", json=payload).status_code == 200
    response = llm_client.post("/api/chat", json=payload)

    assert response.status_code == 429
    assert response.json()["details"]["tier"] == MODEL_TIER


def test_one_tier_does_not_consume_the_other(app: FastAPI, llm_client: TestClient) -> None:
    app.state.rate_limiter = limiter(standard=1, model=1)
    payload = {"messages": [{"role": "user", "content": "hello"}]}

    assert llm_client.get("/api/chat/status").status_code == 200
    assert llm_client.get("/api/chat/status").status_code == 429
    assert llm_client.post("/api/chat", json=payload).status_code == 200


def test_budgets_are_per_user(app: FastAPI, client: TestClient) -> None:
    app.state.rate_limiter = limiter(standard=1)

    alice = {"X-User-Id": "alice"}
    bob = {"X-User-Id": "bob"}

    assert client.get("/api/chat/status", headers=alice).status_code == 200
    assert client.get("/api/chat/status", headers=alice).status_code == 429
    assert client.get("/api/chat/status", headers=bob).status_code == 200
