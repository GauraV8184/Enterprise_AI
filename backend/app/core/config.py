"""Application configuration.

This module is the single source of truth for runtime settings. Values are read
from environment variables and, for local development, an optional ``.env``
file. Using ``pydantic-settings`` means every setting is typed and validated at
startup: if a value is malformed the app fails fast with a clear error instead
of misbehaving later.

Add new settings here as each phase introduces a dependency that needs one. Do
not add placeholders for services the code does not use yet - that makes it hard
to tell which settings are real.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production", "test"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Validated application settings.

    Field names map to environment variables case-insensitively, e.g. the
    ``app_name`` field is read from the ``APP_NAME`` environment variable.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
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
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

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

    # --- Convenience properties ---------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_test(self) -> bool:
        return self.environment == "test"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so the ``.env`` file is parsed once. Tests clear the cache via
    ``get_settings.cache_clear()`` when they need to override the environment.
    """
    return Settings()
