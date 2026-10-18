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
LlmProvider = Literal["openai", "ollama"]

# Values accepted for LLM_PROVIDER. "ollama" is a convenience alias: Ollama
# exposes an OpenAI-compatible API, so it reuses the same client and the same
# OPENAI_* settings, but needs no API key and defaults to a local base URL.
SUPPORTED_LLM_PROVIDERS = frozenset({"openai", "ollama"})

# Where a local Ollama server listens by default.
OLLAMA_DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"

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

    # --- Embeddings (Phase 3) ------------------------------------------------
    # Embeddings are separate from the chat model because a provider may be good
    # at one and not the other. Defaults to a local Ollama model, which costs
    # nothing and keeps documents on the machine.
    embedding_provider: Literal["ollama", "openai"] = "ollama"
    embedding_model: str = "nomic-embed-text"
    embedding_batch_size: int = 64
    embedding_timeout_seconds: float = 120.0
    # Overrides the LLM endpoint/key when set. Left unset, the embedding client
    # follows embedding_provider's defaults.
    embedding_base_url: str | None = None

    # --- Document storage (Phase 3) ------------------------------------------
    # Local SQLite for development. Phase 4 adds Supabase/Postgres; the storage
    # contract is documented in app/database/repositories.py.
    data_dir: Path = Field(default_factory=lambda: _REPO_ROOT / "data")
    max_upload_mb: int = 25

    # --- Retrieval (Phase 3) -------------------------------------------------
    chunk_size: int = 1000
    chunk_overlap: int = 150
    retrieval_top_k: int = 6
    max_context_chars: int = 12000

    # --- Development identity ------------------------------------------------
    # Phase 3 has no authentication yet (Supabase Auth arrives in Phase 4).
    # Requests may name a user via the X-User-Id header for local multi-user
    # testing; without it this id is used. Refused outright in production.
    dev_user_id: str = "dev-user"

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

    @field_validator("llm_provider", mode="before")
    @classmethod
    def _validate_llm_provider(cls, value: object) -> object:
        """Normalise the provider name and explain bad input clearly.

        Pydantic's built-in Literal error ("Input should be 'openai'") does not
        tell you how to talk to a local model, which is a confusing dead end.
        """
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in SUPPORTED_LLM_PROVIDERS:
                return normalized
            raise ValueError(
                f"Unsupported LLM_PROVIDER {value!r}. "
                f"Supported values: {', '.join(sorted(SUPPORTED_LLM_PROVIDERS))}. "
                "For a local Ollama server use LLM_PROVIDER=ollama; for any other "
                "OpenAI-compatible endpoint use LLM_PROVIDER=openai together with "
                "OPENAI_BASE_URL."
            )
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
        """True when enough settings exist to build an LLM client.

        For ``ollama`` this is always true: a local server needs no API key, and
        mismatches (server down, model not pulled) are reported with specific
        errors on the first call instead.
        """
        if self.llm_provider == "ollama":
            return True
        if self.llm_provider == "openai":
            return self.openai_api_key is not None
        return False

    @property
    def effective_base_url(self) -> str | None:
        """The endpoint the client will actually call.

        ``None`` means the provider library default (api.openai.com).
        """
        if self.openai_base_url:
            return self.openai_base_url
        if self.llm_provider == "ollama":
            return OLLAMA_DEFAULT_BASE_URL
        return None

    @property
    def effective_api_key(self) -> str | None:
        """The credential to send, if any.

        Ollama ignores the value, so a placeholder is supplied when none is set -
        this keeps the client happy without inventing a real credential.
        """
        if self.openai_api_key is not None:
            return self.openai_api_key.get_secret_value()
        if self.llm_provider == "ollama":
            return "ollama"
        return None

    @property
    def effective_embedding_base_url(self) -> str | None:
        """Endpoint for embedding requests."""
        if self.embedding_base_url:
            return self.embedding_base_url
        if self.embedding_provider == "ollama":
            return OLLAMA_DEFAULT_BASE_URL
        return None

    @property
    def effective_embedding_api_key(self) -> str | None:
        """Credential for embedding requests (placeholder for local servers)."""
        if self.openai_api_key is not None:
            return self.openai_api_key.get_secret_value()
        if self.embedding_provider == "ollama":
            return "ollama"
        return None

    @property
    def embeddings_configured(self) -> bool:
        if self.embedding_provider == "ollama":
            return True
        if self.embedding_provider == "openai":
            return self.openai_api_key is not None
        return False

    @property
    def uploads_dir(self) -> Path:
        """Where uploaded originals are written."""
        return self.data_dir / "uploads"

    @property
    def database_path(self) -> Path:
        """SQLite file used by the development document store."""
        return self.data_dir / "app.db"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so the ``.env`` file is parsed once. Tests clear the cache via
    ``get_settings.cache_clear()`` when they need to override the environment.
    """
    return Settings()
