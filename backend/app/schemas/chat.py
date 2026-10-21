"""Request/response schemas for the chat API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.llm.schemas import TaskIntent

ChatRole = Literal["user", "assistant"]

MAX_MESSAGES = 50
MAX_CONTENT_CHARS = 8000


class ChatMessage(BaseModel):
    """One turn in the conversation."""

    role: ChatRole
    content: str = Field(
        min_length=1,
        max_length=MAX_CONTENT_CHARS,
        description="Message text. Empty messages are rejected.",
    )

    @field_validator("content")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Message content must not be blank.")
        return value


class ChatRequest(BaseModel):
    """Body of ``POST /api/chat``.

    The system prompt is a server-side contract: callers may only supply user
    and assistant turns. ``extra="forbid"`` makes that explicit instead of
    silently ignoring a caller-supplied ``system_prompt`` field.
    """

    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage] = Field(
        min_length=1,
        max_length=MAX_MESSAGES,
        description="Conversation so far, oldest first, using only user/assistant roles.",
    )
    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description="Optional per-request sampling temperature.",
    )


class TokenUsage(BaseModel):
    """Token accounting reported by the provider, when available."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class ChatResponse(BaseModel):
    """Body of a successful ``POST /api/chat`` response."""

    answer: str = Field(description="The model's reply, as plain text/Markdown.")
    provider: str
    model: str
    usage: TokenUsage | None = Field(
        default=None, description="Null when the provider does not report usage."
    )
    finish_reason: str | None = None
    request_id: str = Field(description="Correlation id, also returned in the X-Request-ID header.")


class IntentRequest(BaseModel):
    """Body of ``POST /api/chat/intent``."""

    text: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)

    @field_validator("text")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank.")
        return value


class IntentResponse(BaseModel):
    """Structured classification result."""

    intent: TaskIntent
    provider: str
    model: str
    request_id: str


class LlmStatusResponse(BaseModel):
    """Reports whether the LLM is usable. Never includes credentials."""

    configured: bool
    provider: str
    model: str
    timeout_seconds: float
    max_retries: int
    detail: str | None = Field(
        default=None, description="Actionable hint when ``configured`` is false."
    )
