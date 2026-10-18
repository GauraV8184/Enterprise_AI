"""LLM-specific error types.

These subclass :class:`AppError`, so the generic handler in
``app/api/error_handlers.py`` turns them into JSON responses with the right
status code. None of the public messages contain credentials or provider
response bodies - those details stay in the logs.
"""

from __future__ import annotations

from app.core.errors import AppError


class LlmError(AppError):
    """Base class for LLM failures."""

    status_code = 502
    code = "llm_error"
    public_message = "The language model request failed."


class LlmNotConfiguredError(LlmError):
    """No API key / provider configured. Surfaced as 503, never faked."""

    status_code = 503
    code = "llm_not_configured"
    public_message = (
        "The language model is not configured. Set OPENAI_API_KEY in your .env file "
        "(optionally LLM_PROVIDER and OPENAI_MODEL)."
    )


class LlmAuthenticationError(LlmError):
    """The provider rejected our credentials (bad, revoked, or wrong-project key)."""

    status_code = 502
    code = "llm_authentication_failed"
    public_message = (
        "The language model provider rejected the API key. Verify that OPENAI_API_KEY "
        "is valid, active, and belongs to the intended project."
    )


class LlmRateLimitError(LlmError):
    """Provider throttled us. Retrying later can help."""

    status_code = 429
    code = "llm_rate_limited"
    public_message = "The language model provider rate-limited this request. Retry shortly."


class LlmQuotaExhaustedError(LlmError):
    """The account has no credits / quota left.

    Providers report this as HTTP 429, the same status as rate limiting, but the
    remedy is completely different - retrying never helps. Keeping it a separate
    error type is what stops the platform from giving useless advice.
    """

    status_code = 402
    code = "llm_quota_exhausted"
    public_message = (
        "The language model account has no credits or quota remaining, so requests "
        "cannot be served. Add credits to the provider account (for OpenAI: "
        "https://platform.openai.com/settings/organization/billing/). Retrying will not help."
    )


class LlmTimeoutError(LlmError):
    """The provider did not answer within the configured timeout."""

    status_code = 504
    code = "llm_timeout"
    public_message = (
        "The language model did not respond in time. Try again or simplify the request."
    )


class LlmConnectionError(LlmError):
    """Network-level failure reaching the provider."""

    status_code = 502
    code = "llm_connection_failed"
    public_message = "Could not reach the language model provider. Check network access and the configured base URL."


class LlmStructuredOutputError(LlmError):
    """The model did not return output matching the requested schema."""

    status_code = 502
    code = "llm_structured_output_failed"
    public_message = (
        "The language model returned a response that did not match the required schema."
    )


class LlmProviderError(LlmError):
    """Any other provider-side failure (5xx, unexpected status, unknown error)."""

    status_code = 502
    code = "llm_provider_error"
    public_message = (
        "The language model provider returned an error. This is usually temporary; retry shortly."
    )
