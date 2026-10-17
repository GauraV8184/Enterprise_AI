"""Shared pytest fixtures.

Tests build their own app instance with an explicit ``Settings`` object so they
never depend on a developer's local ``.env`` file, and never hit the network.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from tests.fakes import FakeChatModel


@pytest.fixture
def settings() -> Settings:
    """Deterministic settings for tests (no .env, no credentials, quiet logs)."""
    return Settings(
        environment="test",
        debug=False,
        log_json=False,
        log_level="WARNING",
        cors_origins=["http://localhost:5173"],
        openai_api_key=None,
        _env_file=None,  # type: ignore[call-arg]  # ignore any developer .env
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """A fresh app instance (no LLM configured)."""
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """TestClient with no chat model configured."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def fake_model() -> FakeChatModel:
    return FakeChatModel(replies=["Hello from the fake model."])


@pytest.fixture
def llm_client(app: FastAPI, fake_model: FakeChatModel) -> Iterator[TestClient]:
    """TestClient whose app has a fake chat model installed.

    ``app.state.chat_model`` is the same slot the real lifespan fills, and it is
    set after the client starts so the lifespan cannot overwrite it.
    """
    with TestClient(app) as test_client:
        app.state.chat_model = fake_model
        yield test_client
