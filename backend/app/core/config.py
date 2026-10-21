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

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["development", "staging", "production", "test"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LlmProvider = Literal["openai", "ollama"]

SUPPORTED_LLM_PROVIDERS = frozenset({"openai", "ollama"})

OLLAMA_DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"

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
        extra="ignore",
    )

    app_name: str = Field(default="Enterprise AI Intelligence Platform", min_length=1)
    app_version: str = Field(default="0.1.0", min_length=1)
    environment: Environment = "development"
    debug: bool = False
    api_prefix: str = Field(default="/api", min_length=1, pattern=r"^/")

    log_level: LogLevel = "INFO"
    log_json: bool = True

    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    llm_provider: LlmProvider = "openai"

    openai_api_key: SecretStr | None = None

    openai_base_url: str | None = None

    openai_model: str = Field(default="gpt-4o-mini", min_length=1)

    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    llm_timeout_seconds: float = Field(default=60.0, gt=0.0, le=600.0)
    llm_max_retries: int = Field(default=2, ge=0, le=10)

    embedding_provider: Literal["ollama", "openai"] = "ollama"
    embedding_model: str = Field(default="nomic-embed-text", min_length=1)
    embedding_batch_size: int = Field(default=64, ge=1, le=2048)
    embedding_timeout_seconds: float = Field(default=120.0, gt=0.0, le=600.0)
    embedding_base_url: str | None = None

    data_dir: Path = Field(default_factory=lambda: _REPO_ROOT / "data")
    max_upload_mb: int = Field(default=25, ge=1, le=100)

    chunk_size: int = Field(default=1000, ge=50, le=8000)
    chunk_overlap: int = Field(default=150, ge=0, le=7999)
    retrieval_top_k: int = Field(default=6, ge=1, le=25)
    retrieval_min_score: float = Field(default=0.1, ge=-1.0, le=1.0)
    max_context_chars: int = Field(default=12000, ge=100, le=100_000)

    dev_user_id: str = Field(default="dev-user", min_length=1)

    supabase_url: str | None = None
    supabase_anon_key: SecretStr | None = None
    supabase_service_role_key: SecretStr | None = None
    supabase_documents_bucket: str = Field(default="documents", min_length=1)
    storage_timeout_seconds: float = Field(default=120.0, gt=0.0, le=600.0)

    supabase_db_url: SecretStr | None = None

    document_store: Literal["auto", "sqlite", "supabase"] = "auto"

    file_storage: Literal["auto", "local", "supabase"] = "auto"

    auth_backend: Literal["auto", "dev", "supabase"] = "auto"

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

    @field_validator("openai_api_key", "openai_base_url", "embedding_base_url", mode="before")
    @classmethod
    def _blank_string_to_none(cls, value: object) -> object:
        """Treat ``KEY=`` (empty) in .env as "not set" rather than ""."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("data_dir", mode="after")
    @classmethod
    def _anchor_data_dir(cls, value: Path) -> Path:
        """Resolve a relative ``DATA_DIR`` against the repository, not the CWD."""
        path = value.expanduser()
        if not path.is_absolute():
            path = _REPO_ROOT / path
        return path.resolve()

    @model_validator(mode="after")
    def _validate_runtime_safety(self) -> Settings:
        if self.is_production and self.debug:
            raise ValueError(
                "DEBUG must be false when ENVIRONMENT=production. Debug mode exposes "
                "stack traces and interactive error details to clients."
            )
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError(
                f"CHUNK_OVERLAP ({self.chunk_overlap}) must be smaller than "
                f"CHUNK_SIZE ({self.chunk_size})."
            )
        return self

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

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_test(self) -> bool:
        return self.environment == "test"

    @property
    def allows_dev_auth(self) -> bool:
        """Whether the unauthenticated ``X-User-Id`` stub may be trusted.

        Only the two local environments. Staging is a deployed environment with
        real users; treating it like development would let anyone name themselves
        into another tenant by sending a header.
        """
        return self.environment in {"development", "test"}

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
        """Endpoint for embedding requests.

        ``EMBEDDING_BASE_URL`` is the explicit override. When it is unset the
        chat endpoint is reused if provided, so one gateway setting configures
        both clients; otherwise the provider default applies.
        """
        if self.embedding_base_url:
            return self.embedding_base_url
        if self.openai_base_url:
            return self.openai_base_url
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

    @property
    def supabase_configured(self) -> bool:
        """True when the URL and public key are present."""
        return bool(self.supabase_url and self.supabase_anon_key)

    @property
    def supabase_admin_configured(self) -> bool:
        """True when the privileged key is present.

        Only admin scripts need this. The application deliberately does not use
        it for user traffic, because it bypasses Row Level Security.
        """
        return bool(self.supabase_url and self.supabase_service_role_key)

    @property
    def supabase_base(self) -> str:
        """Project base URL, without a trailing slash."""
        if not self.supabase_url:
            raise ValueError("SUPABASE_URL is not set.")
        return self.supabase_url.rstrip("/")

    def supabase_service_url(self, service: Literal["rest", "auth", "storage"]) -> str:
        """Base URL for one Supabase sub-service."""
        return f"{self.supabase_base}/{service}/v1"

    @property
    def use_supabase_store(self) -> bool:
        """Whether documents and vectors live in Supabase."""
        if self.document_store == "supabase":
            return True
        if self.document_store == "sqlite":
            return False
        return self.supabase_configured

    @property
    def use_supabase_auth(self) -> bool:
        """Whether identity comes from Supabase Auth.

        ``auto`` prefers Supabase whenever it is configured, because the
        development header stub is not a security control.
        """
        if self.auth_backend == "supabase":
            return True
        if self.auth_backend == "dev":
            return False
        return self.supabase_configured

    @property
    def use_supabase_storage(self) -> bool:
        """Whether original uploads live in Supabase Storage.

        ``auto`` follows the document store: keeping rows in Postgres and bytes
        on one machine's disk is the inconsistency this backend exists to remove.
        """
        if self.file_storage == "supabase":
            return True
        if self.file_storage == "local":
            return False
        return self.use_supabase_store

    def validate_supabase_expectations(self) -> None:
        """Fail fast on combinations that cannot work.

        Raises:
            ValueError: an explicit backend was requested without credentials.
        """
        if self.document_store == "supabase" and not self.supabase_configured:
            raise ValueError("DOCUMENT_STORE=supabase requires SUPABASE_URL and SUPABASE_ANON_KEY.")
        if self.auth_backend == "supabase" and not self.supabase_configured:
            raise ValueError("AUTH_BACKEND=supabase requires SUPABASE_URL and SUPABASE_ANON_KEY.")
        if self.file_storage == "supabase" and not self.supabase_configured:
            raise ValueError("FILE_STORAGE=supabase requires SUPABASE_URL and SUPABASE_ANON_KEY.")
        if self.is_production and not self.supabase_configured:
            raise ValueError(
                "ENVIRONMENT=production requires Supabase: without it there is no way to "
                "authenticate callers, and refusing to serve is safer than trusting a header."
            )
        if not self.allows_dev_auth and not self.use_supabase_auth:
            raise ValueError(
                f"ENVIRONMENT={self.environment} requires Supabase Auth. The development "
                "X-User-Id stub is only allowed in development and test; configure "
                "SUPABASE_URL and SUPABASE_ANON_KEY (or set AUTH_BACKEND=supabase)."
            )
        if self.use_supabase_store and not self.use_supabase_auth:
            raise ValueError(
                "DOCUMENT_STORE=supabase requires AUTH_BACKEND=supabase. Every query is sent "
                "with the caller's own access token so that Row Level Security decides what it "
                "can see, and the development header stub supplies no token. Set "
                "DOCUMENT_STORE=sqlite as well if you want to use the stub."
            )
        if self.use_supabase_storage and not self.use_supabase_auth:
            raise ValueError(
                "FILE_STORAGE=supabase requires AUTH_BACKEND=supabase. Storage policies compare "
                "the object key to the caller's user id, which means a verified token must be "
                "forwarded. Set FILE_STORAGE=local for the development stub."
            )
        if self.is_production and not self.use_supabase_storage:
            raise ValueError(
                "ENVIRONMENT=production requires Supabase Storage. Uploads written to the local "
                "filesystem are lost on redeploy and are invisible to other instances. Remove "
                "FILE_STORAGE=local."
            )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so the ``.env`` file is parsed once. Tests clear the cache via
    ``get_settings.cache_clear()`` when they need to override the environment.
    """
    return Settings()
