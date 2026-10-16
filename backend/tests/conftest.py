"""Shared pytest fixtures.

Tests build their own app instance with an explicit ``Settings`` object so they
never depend on a developer's local ``.env`` file.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    """Deterministic settings for tests (no .env, quiet logs)."""
    return Settings(
        environment="test",
        debug=False,
        log_json=False,
        log_level="WARNING",
        cors_origins=["http://localhost:5173"],
        _env_file=None,  # type: ignore[call-arg]  # ignore any developer .env
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """A TestClient bound to a fresh app instance."""
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
