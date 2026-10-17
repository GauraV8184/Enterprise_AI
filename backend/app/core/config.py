"""Application configuration.

This module is the single source of truth for runtime settings. Values are read
from environment variables and, for local development, the ``.env`` file at the
repository root. Using ``pydantic-settings`` means every setting is typed and
validated at startup: if a value is malformed the app fails fast with a clear
error instead of misbehaving later.

Add new settings here as each phase introduces a dependency that needs one. Do
not add placeholders for services the code does not use yet - that makes it hard
to tell which settings are real.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["development", "staging", "production", "test"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LlmProvider = Literal["openai"]

# ``backend/app/core/config.py`` -> repo root is three parents up. Resolving the
# .env path absolutely means the app finds the same file no matter which
# directory uvicorn is launched from.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ENV_FILE = _REPO_ROOT / ".env"


class Settings(BaseSettings):
    """Validated application settings.

    Field names map to environment variables case-insensitively, e.g. the
    ``app_name`` field is read from the ``APP_NAME`` environment variable.
    """

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # tolerate unrelated vars in the environment
    )

    # --- Application identity ------------------------------------------------
    app_name: str = "Enterprise AI Intelligence Platform"
    app_version: str = "0.1.0"
    environment: Environment = "development"
    debug: bool = False
    api_prefix: str = "/api"

    # --- Logging -------------------------------------------------------------
    log_level: LogLevel = "INFO"
    log_json: bool = True

    # --- CORS ----------------------------------------------------------------
    # Browser origins allowed to call the API. Defaults to the Vite dev server.
    # ``NoDecode`` stops pydantic-settings from trying to JSON-decode this field
    # before our validator sees it, which is what allows the friendlier
    # comma-separated form (``a,b``) as well as a JSON array.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    # --- LLM provider --------------------------------------------------------
    # ``llm_provider`` selects which client the factory builds. Only "openai" is
    # implemented today; adding a provider means adding a branch in
    # ``app/llm/factory.py``, not redesigning the application.
    llm_provider: LlmProvider = "openai"

    # SecretStr keeps the key out of logs, tracebacks and ``repr(settings)``.
    # Read the real value with ``settings.openai_api_key.get_secret_value()``.
    openai_api_key: SecretStr | None = None

    # Optional: point at an OpenAI-compatible gateway (Azure proxy, LiteLLM,
    # local vLLM, ...) without changing code.
    openai_base_url: str | None = None

    # Model names change over time. This is deliberately configurable: set
    # OPENAI_MODEL in .env to any model your key can access. Verify with
    # ``python scripts/check_llm.py``.
    openai_model: str = "gpt-4o-mini"

    llm_temperature: float = 0.0
    llm_max_tokens: int | None = None
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2

    # --- Validators ----------------------------------------------------------
    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, value: object) -> object:
        """Accept either a JSON array or a comma-separated string.

        ``pydantic-settings`` normally expects JSON for list fields, which is
        awkward to type by hand, so we also accept ``a,b,c``.
        """
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith("["):
                return json.loads(text)
            return [item.strip() for item in text.split(",") if item.strip()]
        return value

    @field_validator("openai_api_key", "openai_base_url", mode="before")
    @classmethod
    def _blank_string_to_none(cls, value: object) -> object:
        """Treat ``KEY=`` (empty) in .env as "not set" rather than ""."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # --- Convenience properties ---------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_test(self) -> bool:
        return self.environment == "test"

    @property
    def llm_configured(self) -> bool:
        """True when enough settings exist to build an LLM client."""
        if self.llm_provider == "openai":
            return self.openai_api_key is not None
        return False


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so the ``.env`` file is parsed once. Tests clear the cache via
    ``get_settings.cache_clear()`` when they need to override the environment.
    """
    return Settings()
