"""Service layer: turns LangChain's model interface into predictable app behaviour.

Responsibilities:

* build the message list from an API request,
* normalise the model's response (content shape and token usage vary by
  provider/version),
* translate provider exceptions into :class:`AppError` subclasses with sensible
  HTTP statuses and client-safe messages,
* run structured-output calls.

Keeping this here means routers stay thin and the error contract is consistent
everywhere the LLM is used.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
)

from app.core.errors import AppError
from app.core.logging import get_logger, safe_extra
from app.llm.errors import (
    LlmAuthenticationError,
    LlmConnectionError,
    LlmProviderError,
    LlmQuotaExhaustedError,
    LlmRateLimitError,
    LlmStructuredOutputError,
    LlmTimeoutError,
)
from app.llm.prompts import DEFAULT_SYSTEM_PROMPT
from app.llm.schemas import TaskIntent

logger = get_logger(__name__)

_ROLE_TO_MESSAGE = {"user": HumanMessage, "assistant": AIMessage, "system": SystemMessage}


@dataclass(frozen=True)
class TokenUsage:
    """Token accounting, when the provider reports it."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class LlmAnswer:
    """Normalised result of one chat completion."""

    text: str
    usage: TokenUsage | None = None
    finish_reason: str | None = None
    model_name: str | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)


def build_messages(
    history: list[dict[str, str]],
    system_prompt: str | None = None,
) -> list[BaseMessage]:
    """Convert API-style ``[{"role", "content"}]`` dicts into LangChain messages.

    The default system prompt is prepended unless the caller supplies one or the
    history already begins with a system message.
    """
    messages: list[BaseMessage] = []

    has_system = any(item.get("role") == "system" for item in history)
    if not has_system:
        messages.append(SystemMessage(content=system_prompt or DEFAULT_SYSTEM_PROMPT))

    for item in history:
        role = item.get("role", "user")
        factory = _ROLE_TO_MESSAGE.get(role)
        if factory is None:
            raise ValueError(f"Unsupported message role: {role!r}")
        messages.append(factory(content=item.get("content", "")))

    return messages


def _content_to_text(content: Any) -> str:
    """Flatten message content to plain text.

    Newer LangChain versions may return a list of content blocks
    (``[{"type": "text", "text": "..."}]``) instead of a plain string.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return "" if content is None else str(content)


def _extract_usage(message: BaseMessage) -> TokenUsage | None:
    """Read token usage from a response, tolerating providers that omit it."""
    metadata = getattr(message, "usage_metadata", None)
    if not isinstance(metadata, dict):
        return None
    return TokenUsage(
        input_tokens=metadata.get("input_tokens"),
        output_tokens=metadata.get("output_tokens"),
        total_tokens=metadata.get("total_tokens"),
    )


# Provider codes that mean "out of credits/quota" rather than "slow down".
# OpenAI reports both as HTTP 429, which is why the body must be inspected.
_QUOTA_ERROR_CODES = frozenset(
    {
        "insufficient_quota",
        "credit_balance_exhausted",
        "billing_hard_limit_reached",
        "quota_exceeded",
        "exceeded_quota",
    }
)


def _provider_error_fields(exc: BaseException) -> tuple[str, str]:
    """Extract the provider's ``(code, type)`` regardless of SDK version.

    The openai SDK exposes these as attributes, but they are only populated when
    the error originates from a real HTTP response. Reading the parsed body as a
    fallback keeps classification working for errors constructed in tests and for
    providers that populate only one of the two.
    """
    code = str(getattr(exc, "code", "") or "").lower()
    error_type = str(getattr(exc, "type", "") or "").lower()

    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            code = code or str(error.get("code") or "").lower()
            error_type = error_type or str(error.get("type") or "").lower()

    return code, error_type


def _classify_rate_limit(exc: RateLimitError) -> AppError:
    """Split HTTP 429 into "out of credits" vs "slow down".

    Retrying is futile for the first and correct for the second, so conflating
    them produces actively misleading advice. Providers that expose neither
    field fall back to the rate-limit interpretation.
    """
    code, error_type = _provider_error_fields(exc)

    if code in _QUOTA_ERROR_CODES or error_type == "insufficient_quota":
        return LlmQuotaExhaustedError(details={"provider_code": code or error_type or "unknown"})
    return LlmRateLimitError(details={"provider_code": code or error_type or "unknown"})


# Specific provider errors worth recovering from a wrapper's cause chain.
# The generic APIError is deliberately excluded: it carries no extra detail, so
# digging for it would not improve the classification.
_KNOWN_PROVIDER_ERRORS = (
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
    APITimeoutError,
    APIConnectionError,
    APIStatusError,
    OutputParserException,
)


def _find_provider_cause(exc: BaseException) -> BaseException | None:
    """Walk the exception chain looking for the original provider error.

    Wrappers can hide a specific provider error behind a generic one. LangChain's
    streaming accumulator, for example, replaces a failed request with
    ``RuntimeError("v2 stream finished without producing a message")``, which
    would otherwise be reported as a vague provider error. The original is still
    reachable through ``__cause__``/``__context__``.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, _KNOWN_PROVIDER_ERRORS):
            return current
        current = current.__cause__ or current.__context__
    return None


def map_exception(exc: BaseException) -> AppError:
    """Translate a provider exception into a client-safe application error.

    Order matters: ``AuthenticationError`` and ``RateLimitError`` are
    ``APIStatusError`` subclasses, and ``APITimeoutError`` is an
    ``APIConnectionError`` subclass, so the specific cases must be checked first.
    """
    if isinstance(exc, AppError):
        return exc
    if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
        return LlmAuthenticationError()
    if isinstance(exc, RateLimitError):
        return _classify_rate_limit(exc)
    if isinstance(exc, (APITimeoutError, asyncio.TimeoutError, TimeoutError)):
        return LlmTimeoutError()
    if isinstance(exc, APIConnectionError):
        return LlmConnectionError()
    if isinstance(exc, OutputParserException):
        return LlmStructuredOutputError()
    if isinstance(exc, APIStatusError):
        # Record the provider's HTTP status (a number, safe to expose) but never
        # the response body, which may echo request content.
        return LlmProviderError(details={"provider_status": exc.status_code})
    if isinstance(exc, APIError):
        return LlmProviderError()

    # A wrapper may have hidden the real provider error - look for it.
    cause = _find_provider_cause(exc)
    if cause is not None:
        return map_exception(cause)

    return LlmProviderError()


async def generate_answer(
    model: BaseChatModel,
    *,
    history: list[dict[str, str]],
    system_prompt: str | None = None,
    temperature: float | None = None,
) -> LlmAnswer:
    """Run a chat completion and return a normalised answer.

    Raises:
        AppError: any provider failure, translated by :func:`map_exception`.
    """
    messages = build_messages(history, system_prompt)

    runnable: Any = model
    if temperature is not None:
        # ``bind`` passes the override to the provider without mutating the
        # shared client instance held on app.state.
        runnable = model.bind(temperature=temperature)

    try:
        response = await runnable.ainvoke(messages)
    except Exception as exc:  # noqa: BLE001 - deliberately translated below
        mapped = map_exception(exc)
        logger.warning(
            "llm_call_failed",
            extra=safe_extra(
                error_type=type(exc).__name__,
                mapped_error=mapped.code,
                message_count=len(messages),
            ),
        )
        raise mapped from exc

    metadata = getattr(response, "response_metadata", None) or {}
    return LlmAnswer(
        text=_content_to_text(response.content).strip(),
        usage=_extract_usage(response),
        finish_reason=metadata.get("finish_reason"),
        model_name=metadata.get("model_name") or metadata.get("model"),
        raw_metadata={"id": metadata.get("id")} if metadata.get("id") else {},
    )


async def classify_intent(
    model: BaseChatModel,
    *,
    text: str,
    system_prompt: str | None = None,
) -> TaskIntent:
    """Classify a request into a :class:`TaskIntent` using structured output.

    ``with_structured_output`` makes the model emit JSON matching the schema and
    validates it with Pydantic, so callers receive a typed object rather than
    text they have to parse.

    Raises:
        AppError: provider failures, or a schema mismatch
            (:class:`LlmStructuredOutputError`).
    """
    from app.llm.prompts import INTENT_SYSTEM_PROMPT

    messages = build_messages(
        [{"role": "user", "content": text}],
        system_prompt or INTENT_SYSTEM_PROMPT,
    )

    try:
        structured = model.with_structured_output(TaskIntent)
        result = await structured.ainvoke(messages)
    except Exception as exc:  # noqa: BLE001 - deliberately translated below
        mapped = map_exception(exc)
        logger.warning(
            "llm_structured_call_failed",
            extra=safe_extra(error_type=type(exc).__name__, mapped_error=mapped.code),
        )
        raise mapped from exc

    if not isinstance(result, TaskIntent):
        # Providers using a "tool calling" strategy can return a dict.
        try:
            return TaskIntent.model_validate(result)
        except Exception as exc:  # noqa: BLE001
            raise LlmStructuredOutputError() from exc
    return result
