"""Response hardening: defensive headers and, in production, host validation."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_api_responses_carry_defensive_headers(client: TestClient) -> None:
    response = client.get("/health")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"


def test_hsts_is_reserved_for_production(client: TestClient) -> None:
    """Sending HSTS in development would pin localhost to HTTPS in the browser."""

    assert "Strict-Transport-Security" not in client.get("/health").headers


def test_production_sends_hsts_and_rejects_unknown_hosts(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(Settings, "is_production", property(lambda self: True))
    production = settings.model_copy(update={"allowed_hosts": ["api.example.com"]})

    # No context manager: the lifespan's Supabase validation is not what this
    # test is about, and the middleware is registered in create_app.
    client = TestClient(create_app(production))

    allowed = client.get("/health", headers={"Host": "api.example.com"})
    assert allowed.status_code == 200
    assert allowed.headers["Strict-Transport-Security"].startswith("max-age=31536000")

    refused = client.get("/health", headers={"Host": "evil.example.com"})
    assert refused.status_code == 400
