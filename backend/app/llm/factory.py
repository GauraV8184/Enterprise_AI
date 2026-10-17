"""Factory that builds a chat model from settings.

Why a factory instead of constructing ``ChatOpenAI`` inline? Two reasons:

* **Provider swap without redesign.** All provider-specific knowledge lives in
  :func:`build_chat_model`. Adding Azure/Bedrock/Anthropic later is a new branch
  here; nothing else in the app changes.
* **Single validation point.** If the provider is not configured we raise
  :class:`LlmNotConfiguredError` at startup rather than failing on a user's first
  request with a confusing stack trace.

Field names are version-sensitive - ``langchain-openai`` uses ``model_name`` and
``openai_api_base``, not ``model``/``base_url``. They were verified against
langchain-openai 1.7.2.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel

from app.core.config import Settings
from app.core.logging import get_logger, safe_extra
from app.llm.errors import LlmNotConfiguredError

logger = get_logger(__name__)


def build_chat_model(settings: Settings) -> BaseChatModel:
    """Build a provider client from validated settings.

    Raises:
        LlmNotConfiguredError: the selected provider has no credentials.
    """
    if settings.llm_provider == "openai":
        return _build_openai(settings)

    # Defensive: ``llm_provider`` is a Literal, so this is unreachable unless a
    # new provider value is added without a corresponding branch.
    raise LlmNotConfiguredError(f"Unsupported LLM provider: {settings.llm_provider!r}")


def _build_openai(settings: Settings) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    if settings.openai_api_key is None:
        raise LlmNotConfiguredError()

    kwargs: dict[str, Any] = {
        "model_name": settings.openai_model,
        # Passing the SecretStr straight through keeps the plaintext value out
        # of local variables where it could end up in a traceback.
        "openai_api_key": settings.openai_api_key,
        "temperature": settings.llm_temperature,
        "request_timeout": settings.llm_timeout_seconds,
        "max_retries": settings.llm_max_retries,
    }
    if settings.openai_base_url:
        kwargs["openai_api_base"] = settings.openai_base_url
    if settings.llm_max_tokens is not None:
        kwargs["max_tokens"] = settings.llm_max_tokens

    logger.info(
        "llm_client_built",
        extra=safe_extra(
            provider=settings.llm_provider,
            model=settings.openai_model,
            timeout_seconds=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            custom_base_url=bool(settings.openai_base_url),
        ),
    )
    return ChatOpenAI(**kwargs)
