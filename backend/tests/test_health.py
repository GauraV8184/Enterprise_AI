"""Tests for health and service-metadata endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.logging import request_id_ctx


def test_root_returns_service_metadata(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == "0.1.0"
    assert body["environment"] == "test"
    assert body["docs_url"] == "/docs"


def test_health_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_under_api_prefix(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_request_id_header_is_returned(client: TestClient) -> None:
    response = client.get("/health")
    assert "X-Request-ID" in response.headers
    assert len(response.headers["X-Request-ID"]) == 32


def test_request_id_is_echoed_back_when_supplied(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "abc123"})
    assert response.headers["X-Request-ID"] == "abc123"


def test_invalid_request_id_is_replaced(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "bad id with spaces"})

    assert response.headers["X-Request-ID"] != "bad id with spaces"
    assert len(response.headers["X-Request-ID"]) == 32


def test_completion_log_is_written_before_the_request_context_is_reset(
    client: TestClient, monkeypatch
) -> None:
    captured: list[str] = []

    def fake_info(message: str, *args: object, **kwargs: object) -> None:
        if message == "request_completed":
            captured.append(request_id_ctx.get())

    monkeypatch.setattr("app.main.logger.info", fake_info)

    response = client.get("/health", headers={"X-Request-ID": "trace-abc"})

    assert response.headers["X-Request-ID"] == "trace-abc"
    assert captured == ["trace-abc"]


def test_openapi_schema_is_available(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["info"]["version"] == "0.1.0"


def test_unknown_route_returns_404(client: TestClient) -> None:
    response = client.get("/does-not-exist")
    assert response.status_code == 404
