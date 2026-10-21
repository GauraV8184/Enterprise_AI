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


# ---------------------------------------------------------------------------
# File storage
# ---------------------------------------------------------------------------


def test_file_storage_auto_follows_the_document_store() -> None:
    local = Settings(_env_file=None)  # type: ignore[call-arg]
    assert local.use_supabase_storage is False

    supabase = Settings(  # type: ignore[call-arg]
        _env_file=None,
        supabase_url="https://example.supabase.co",
        supabase_anon_key="public-anon-key",
    )
    assert supabase.use_supabase_store is True
    assert supabase.use_supabase_storage is True


def test_local_file_storage_can_be_pinned_with_a_supabase_store() -> None:
    """Allowed outside production, so a developer can keep originals on disk."""
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        document_store="supabase",
        auth_backend="supabase",
        file_storage="local",
        supabase_url="https://example.supabase.co",
        supabase_anon_key="public-anon-key",
    )

    assert settings.use_supabase_storage is False
    settings.validate_supabase_expectations()


def test_supabase_storage_without_credentials_is_refused() -> None:
    settings = Settings(_env_file=None, file_storage="supabase")  # type: ignore[call-arg]

    with pytest.raises(ValueError, match="FILE_STORAGE=supabase requires SUPABASE_URL"):
        settings.validate_supabase_expectations()


def test_supabase_storage_with_the_development_stub_is_refused() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        document_store="sqlite",
        auth_backend="dev",
        file_storage="supabase",
        supabase_url="https://example.supabase.co",
        supabase_anon_key="public-anon-key",
    )

    with pytest.raises(ValueError, match="FILE_STORAGE=supabase requires AUTH_BACKEND=supabase"):
        settings.validate_supabase_expectations()


def test_production_refuses_local_file_storage() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        environment="production",
        document_store="supabase",
        auth_backend="supabase",
        file_storage="local",
        supabase_url="https://example.supabase.co",
        supabase_anon_key="public-anon-key",
    )

    with pytest.raises(ValueError, match="requires Supabase Storage"):
        settings.validate_supabase_expectations()
