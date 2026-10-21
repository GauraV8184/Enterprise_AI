"""Embedding client construction and provider selection."""

from __future__ import annotations

import pytest
from langchain_core.embeddings import Embeddings

from app.core.config import Settings
from app.llm.errors import LlmNotConfiguredError
from app.llm.factory import build_embeddings


def _settings(**overrides) -> Settings:
    base = {
        "environment": "test",
        "log_json": False,
        "log_level": "WARNING",
        "openai_api_key": None,
        "_env_file": None,
    }
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


def test_ollama_embeddings_need_no_api_key() -> None:
    model = build_embeddings(
        _settings(embedding_provider="ollama", embedding_model="nomic-embed-text")
    )

    assert isinstance(model, Embeddings)
    assert model.model == "nomic-embed-text"
    assert str(model.openai_api_base).rstrip("/") == "http://127.0.0.1:11434/v1"


def test_openai_embeddings_without_a_key_fail_clearly() -> None:
    """The error must name the setting, not just report a missing argument."""
    with pytest.raises(LlmNotConfiguredError) as excinfo:
        build_embeddings(_settings(embedding_provider="openai", openai_api_key=None))

    assert "OPENAI_API_KEY" in excinfo.value.message
    assert "EMBEDDING_PROVIDER=ollama" in excinfo.value.message


def test_openai_embeddings_are_built_when_a_key_exists() -> None:
    model = build_embeddings(
        _settings(
            embedding_provider="openai",
            openai_api_key="sk-test",
            embedding_model="text-embedding-3-small",
        )
    )

    assert model.model == "text-embedding-3-small"


def test_embedding_endpoint_is_independent_of_the_chat_endpoint() -> None:
    """Local embeddings plus a hosted chat model is a normal, supported combination."""
    settings = _settings(
        llm_provider="openai",
        openai_api_key="sk-test",
        openai_base_url="https://gateway.internal/v1",
        embedding_provider="ollama",
        embedding_base_url="http://192.168.1.50:11434/v1",
    )

    assert settings.effective_base_url == "https://gateway.internal/v1"
    assert settings.effective_embedding_base_url == "http://192.168.1.50:11434/v1"


def test_embedding_endpoint_falls_back_to_the_chat_base_url() -> None:
    """A single gateway setting must configure both clients, not just chat."""
    settings = _settings(
        llm_provider="openai",
        openai_api_key="sk-test",
        openai_base_url="https://gateway.internal/v1",
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
    )

    assert settings.effective_embedding_base_url == "https://gateway.internal/v1"

    model = build_embeddings(settings)
    assert str(model.openai_api_base).rstrip("/") == "https://gateway.internal/v1"


def test_local_models_skip_the_openai_token_length_check() -> None:
    """``check_embedding_ctx_length`` uses OpenAI's tokeniser and rejects other models."""
    model = build_embeddings(_settings(embedding_provider="ollama"))

    assert model.check_embedding_ctx_length is False


def test_batch_size_is_applied() -> None:
    model = build_embeddings(_settings(embedding_provider="ollama", embedding_batch_size=16))
    assert model.chunk_size == 16


def test_embeddings_configured_reflects_the_provider() -> None:
    assert _settings(embedding_provider="ollama").embeddings_configured is True
    assert (
        _settings(embedding_provider="openai", openai_api_key=None).embeddings_configured is False
    )
    assert (
        _settings(embedding_provider="openai", openai_api_key="sk-test").embeddings_configured
        is True
    )
