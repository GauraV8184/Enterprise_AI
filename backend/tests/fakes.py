"""Test doubles and helpers.

The fake chat model implements LangChain's ``BaseChatModel`` interface for real
(``_generate`` / ``_agenerate`` / ``with_structured_output``), so the code under
test runs its genuine logic - only the network call is replaced. That keeps the
tests honest: nothing in the app knows it is talking to a fake.

Note: openai 3.x uses ``httpx2`` internally, so provider exceptions are built
with that library to keep the fakes faithful to what production sees.
"""

from __future__ import annotations

from typing import Any

import httpx2
import openai
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import Field


class FakeChatModel(BaseChatModel):
    """Deterministic stand-in for a real chat model."""

    replies: list[str] = Field(default_factory=list)
    """Canned replies, consumed in order; the last one repeats."""

    usage: dict[str, int] = Field(default_factory=dict)
    error: Any = None
    """Raised from generation when set - used to test error translation."""

    structured_result: Any = None
    structured_error: Any = None
    bound_kwargs: dict[str, Any] = Field(default_factory=dict)
    received: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "fake-chat-model"

    def _reply_for(self, index: int) -> str:
        if not self.replies:
            return ""
        return self.replies[min(index, len(self.replies) - 1)]

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.received.append(list(messages))
        if self.error is not None:
            raise self.error

        message = AIMessage(
            content=self._reply_for(len(self.received) - 1),
            response_metadata={"finish_reason": "stop", "model_name": "fake-model"},
            usage_metadata=self.usage or None,
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def bind(self, **kwargs: Any) -> FakeChatModel:
        self.bound_kwargs.update(kwargs)
        return self

    def with_structured_output(self, schema: Any, **kwargs: Any) -> RunnableLambda:
        def produce(_input: Any) -> Any:
            if self.structured_error is not None:
                raise self.structured_error
            return self.structured_result

        return RunnableLambda(produce)


# --------------------------------------------------------------------------
# Provider exception builders (mirror what the openai SDK raises in production)
# --------------------------------------------------------------------------

_API_URL = "https://api.openai.com/v1/chat/completions"


def _response(status_code: int, body: dict | None = None) -> httpx2.Response:
    return httpx2.Response(
        status_code,
        request=httpx2.Request("POST", _API_URL),
        json=body or {"error": {"message": "provider error"}},
    )


def make_authentication_error() -> Exception:
    return openai.AuthenticationError(
        "Incorrect API key provided",
        response=_response(401),
        body={"error": {"message": "Incorrect API key provided"}},
    )


def make_rate_limit_error() -> Exception:
    """Genuine throttling: retrying later is the right response."""
    return openai.RateLimitError(
        "Rate limit reached",
        response=_response(429),
        body={"error": {"message": "Rate limit reached", "type": "rate_limit_error"}},
    )


def make_quota_error() -> Exception:
    """HTTP 429 caused by an empty credit balance, not throttling.

    Captured from a real OpenAI response so the fake matches production.
    """
    body = {
        "error": {
            "message": "You have no credits remaining.",
            "type": "insufficient_quota",
            "param": None,
            "code": "credit_balance_exhausted",
        }
    }
    return openai.RateLimitError("no credits remaining", response=_response(429, body), body=body)


def make_timeout_error() -> Exception:
    return openai.APITimeoutError(request=httpx2.Request("POST", _API_URL))


def make_connection_error() -> Exception:
    return openai.APIConnectionError(request=httpx2.Request("POST", _API_URL))


def make_status_error(status_code: int = 503) -> Exception:
    return openai.APIStatusError(
        "service unavailable",
        response=_response(status_code),
        body={"error": {"message": "service unavailable"}},
    )
