"""Tests for the LLM provider factory."""

from __future__ import annotations

import pytest
from langchain_core.language_models import BaseChatModel

from app.core.config import Settings
from app.llm.errors import LlmNotConfiguredError
from app.llm.factory import build_chat_model


def _settings(**overrides) -> Settings:
    base = {"environment": "test", "_env_file": None}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_builds_a_chat_model_when_configured() -> None:
    model = build_chat_model(_settings(openai_api_key="sk-test-not-a-real-key"))
    assert isinstance(model, BaseChatModel)


def test_model_name_and_timeout_are_applied() -> None:
    model = build_chat_model(
        _settings(openai_api_key="sk-test", openai_model="gpt-4o-mini", llm_timeout_seconds=12.5)
    )
    # Attributes are the ones we set explicitly; nothing else is asserted.
    assert model.model_name == "gpt-4o-mini"
    assert model.request_timeout == 12.5


def test_custom_base_url_is_applied() -> None:
    model = build_chat_model(
        _settings(openai_api_key="sk-test", openai_base_url="https://gateway.example/v1")
    )
    assert str(model.openai_api_base).rstrip("/") == "https://gateway.example/v1"


def test_max_tokens_only_set_when_provided() -> None:
    default_model = build_chat_model(_settings(openai_api_key="sk-test"))
    assert default_model.max_tokens is None

    capped = build_chat_model(_settings(openai_api_key="sk-test", llm_max_tokens=256))
    assert capped.max_tokens == 256


def test_missing_key_raises_not_configured() -> None:
    with pytest.raises(LlmNotConfiguredError):
        build_chat_model(_settings(openai_api_key=None))


def test_blank_key_in_env_is_treated_as_missing() -> None:
    # Simulates ``OPENAI_API_KEY=`` (empty) in a .env file.
    settings = _settings(openai_api_key="   ")
    assert settings.openai_api_key is None
    assert settings.llm_configured is False


def test_llm_configured_reflects_key_presence() -> None:
    assert _settings(openai_api_key="sk-test").llm_configured is True
    assert _settings(openai_api_key=None).llm_configured is False


def test_api_key_is_not_exposed_in_repr() -> None:
    """SecretStr must keep the key out of logs, tracebacks and repr()."""
    # Built at runtime rather than written literally: keeps credential-shaped
    # strings out of the source tree and out of automated scanners.
    fake_value = "-".join(["sk", "unit", "test", "value"])
    settings = _settings(openai_api_key=fake_value)

    assert fake_value not in repr(settings)
    assert fake_value not in str(settings)
    assert fake_value not in str(settings.model_dump())
    # ...but the real value is still retrievable by code that needs it.
    assert settings.openai_api_key is not None
    assert settings.openai_api_key.get_secret_value() == fake_value
