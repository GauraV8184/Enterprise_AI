"""Request/response schemas for the supervisor workflow endpoint."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.graph.state import WorkflowRoute
from app.llm.schemas import TaskIntent
from app.schemas.chat import MAX_MESSAGES, ChatMessage, TokenUsage
from app.schemas.documents import MAX_QUESTION_CHARS, SourceCitation


class WorkflowRequest(BaseModel):
    """Body of ``POST /api/workflow/run``.

    Conversation history follows the chat contract: user and assistant turns
    only, and unknown fields are rejected rather than silently ignored.
    """

    model_config = ConfigDict(extra="forbid")

    message: str = Field(
        min_length=1,
        max_length=MAX_QUESTION_CHARS,
        description="The request to classify and answer.",
    )
    history: list[ChatMessage] = Field(
        default_factory=list,
        max_length=MAX_MESSAGES,
        description="Earlier turns, oldest first. Used by the conversational route.",
    )
    document_ids: list[str] | None = Field(
        default=None,
        description="Restrict a document-grounded answer to these documents.",
    )

    @field_validator("message")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("The message must not be blank.")
        return value


class WorkflowResponse(BaseModel):
    """Body of ``POST /api/workflow/run``: the answer and the decision behind it."""

    answer: str
    route: WorkflowRoute = Field(
        description=(
            "Which capability answered: 'chat' for a conversational answer, "
            "'documents' for an answer grounded in retrieved passages."
        )
    )
    intent: TaskIntent
    grounded: bool | None = Field(
        default=None,
        description=(
            "Documents route only: True when passages were retrieved, False when "
            "nothing relevant exists (then the model was not asked to answer). "
            "None for the chat route, where grounding does not apply."
        ),
    )
    sources: list[SourceCitation] = Field(default_factory=list)
    searched_chunks: int = 0
    provider: str
    model: str
    usage: TokenUsage | None = None
    request_id: str
