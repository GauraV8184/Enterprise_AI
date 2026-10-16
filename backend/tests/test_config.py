"""Tests for settings parsing and validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings


def test_defaults_are_sane() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.app_name == "Enterprise AI Intelligence Platform"
    assert settings.environment == "development"
    assert settings.api_prefix == "/api"
    assert settings.is_production is False
    assert settings.is_test is False


def test_cors_origins_accepts_comma_separated_string() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, cors_origins="http://a.test, http://b.test"
    )
    assert settings.cors_origins == ["http://a.test", "http://b.test"]


def test_cors_origins_accepts_json_array() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, cors_origins='["http://a.test"]'
    )
    assert settings.cors_origins == ["http://a.test"]


def test_invalid_environment_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="not-a-real-env")  # type: ignore[call-arg]


def test_is_test_and_is_production_flags() -> None:
    assert Settings(_env_file=None, environment="test").is_test is True  # type: ignore[call-arg]
    assert Settings(_env_file=None, environment="production").is_production is True  # type: ignore[call-arg]


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    assert get_settings() is get_settings()
    get_settings.cache_clear()
