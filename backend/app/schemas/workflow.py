"""Request/response schemas for the supervisor workflow endpoint."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.graph.state import WorkflowRoute
from app.llm.schemas import TaskIntent
from app.schemas.chat import MAX_MESSAGES, ChatMessage, TokenUsage
from app.schemas.documents import MAX_QUESTION_CHARS, SourceCitation
from app.schemas.evidence import AnalyticsEvidence, WebSource


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
            "Which capability answered: 'chat' (conversational), 'documents' "
            "(retrieved passages), 'research' (live web search), 'analytics' (SQL "
            "over the demo dataset), or 'report' (Markdown from the requested "
            "sources)."
        )
    )
    intent: TaskIntent
    grounded: bool | None = Field(
        default=None,
        description=(
            "True when the answer was produced from evidence that was found; False "
            "when the source returned nothing (then the model was not asked to "
            "answer). Applies to the documents, research, analytics and report "
            "routes; None for chat, where grounding does not apply."
        ),
    )
    sources: list[SourceCitation] = Field(
        default_factory=list,
        description=(
            "Document passages behind the answer, labelled [1], [2] as the model "
            "was told to cite them."
        ),
    )
    searched_chunks: int = 0
    web_sources: list[WebSource] = Field(
        default_factory=list,
        description="Web results behind a research or report answer, labelled W1, W2.",
    )
    analytics: AnalyticsEvidence | None = Field(
        default=None,
        description="The query and rows behind an analytics or report answer.",
    )
    provider: str
    model: str
    usage: TokenUsage | None = Field(
        default=None,
        description=(
            "Token usage of the answering call. Planning and SQL-generation calls "
            "are not included (they are structured calls, which do not report usage)."
        ),
    )
    request_id: str
