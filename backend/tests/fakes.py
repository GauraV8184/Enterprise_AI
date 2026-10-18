"""Test doubles and helpers.

The fake chat model implements LangChain's ``BaseChatModel`` interface for real
(``_generate`` / ``_agenerate`` / ``with_structured_output``), so the code under
test runs its genuine logic - only the network call is replaced. That keeps the
tests honest: nothing in the app knows it is talking to a fake.

Note: openai 3.x uses ``httpx2`` internally, so provider exceptions are built
with that library to keep the fakes faithful to what production sees.
"""

from __future__ import annotations

import zlib
from typing import Any

import httpx2
import openai
from langchain_core.embeddings import Embeddings
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
# Fake embeddings
# --------------------------------------------------------------------------


class HashingEmbeddings(Embeddings):
    """A real ``Embeddings`` implementation with no model behind it.

    Each token is hashed into one of ``dimensions`` buckets and the result is
    L2-normalised, so cosine similarity is dominated by **shared vocabulary**. That
    lets retrieval tests assert real ranking behaviour ("the passage about revenue
    ranks above the passage about laptops") rather than just checking a list came
    back.

    ``dimensions`` is large because collisions are the failure mode: with a small
    vector, unrelated documents share buckets by accident and can outscore a
    genuinely related one. At 1024 buckets a stray collision contributes roughly
    0.07 cosine, well below the ~0.25 from three genuinely shared terms.

    ``zlib.crc32`` is used rather than the builtin ``hash()`` because string hashing
    is salted per process - a test that passed in one run could fail in the next,
    and stored vectors would not be reproducible.
    """

    dimensions: int = 1024

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in text.lower().split():
            bucket = zlib.crc32(token.encode("utf-8")) % self.dimensions
            vector[bucket] += 1.0

        norm = sum(value * value for value in vector) ** 0.5
        if norm == 0.0:
            # An empty string would otherwise be an all-zero vector, which has no
            # direction and cannot be compared with anything.
            vector[0] = 1.0
            return vector
        return [value / norm for value in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


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
