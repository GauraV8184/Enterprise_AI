"""Shared pytest fixtures.

Tests build their own app instance with an explicit ``Settings`` object so they
never depend on a developer's local ``.env`` file, and never hit the network.

``data_dir`` is redirected to a temporary directory for every test. Without that,
the suite would write uploads and vectors into the developer's real
``data/`` folder and tests could see each other's leftovers.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from tests.fakes import FakeChatModel, HashingEmbeddings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Deterministic settings for tests (no .env, no credentials, quiet logs)."""
    return Settings(
        environment="test",
        debug=False,
        log_json=False,
        log_level="WARNING",
        cors_origins=["http://localhost:5173"],
        openai_api_key=None,
        data_dir=tmp_path / "data",
        chunk_size=400,
        chunk_overlap=80,
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
def fake_embeddings() -> HashingEmbeddings:
    return HashingEmbeddings()


@pytest.fixture
def llm_client(app: FastAPI, fake_model: FakeChatModel) -> Iterator[TestClient]:
    """TestClient whose app has a fake chat model installed.

    ``app.state.chat_model`` is the same slot the real lifespan fills, and it is
    set after the client starts so the lifespan cannot overwrite it.
    """
    with TestClient(app) as test_client:
        app.state.chat_model = fake_model
        yield test_client


@pytest.fixture
def rag_client(
    app: FastAPI,
    fake_model: FakeChatModel,
    fake_embeddings: HashingEmbeddings,
) -> Iterator[TestClient]:
    """TestClient with both a fake chat model and fake embeddings installed.

    A test that also requests ``fake_model`` receives this same instance, so it can
    set ``fake_model.replies`` to control the grounded answer text.
    """
    with TestClient(app) as test_client:
        app.state.chat_model = fake_model
        app.state.embeddings = fake_embeddings
        yield test_client


@pytest.fixture
def documents_dir(tmp_path: Path) -> Path:
    """Scratch directory for generated sample documents."""
    directory = tmp_path / "samples"
    directory.mkdir()
    return directory


def upload(
    test_client: TestClient, path: Path, *, filename: str | None = None, user: str | None = None
):
    """POST a file to the documents endpoint.

    Returns the raw response so tests can assert on both success and failure
    without the helper swallowing status codes.
    """
    headers = {"X-User-Id": user} if user else {}
    with path.open("rb") as handle:
        return test_client.post(
            "/api/documents",
            files={"file": (filename or path.name, handle, "application/octet-stream")},
            headers=headers,
        )
