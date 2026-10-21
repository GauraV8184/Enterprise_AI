"""Request/response schemas for ``POST /api/reports``.

The request names the topic and the evidence sources; the response carries the
Markdown report *and* everything it was written from, so a reader can check the
report against the same evidence the model saw rather than trusting it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agents.report import ReportSource
from app.schemas.chat import TokenUsage
from app.schemas.documents import MAX_QUESTION_CHARS, SourceCitation
from app.schemas.evidence import AnalyticsEvidence, WebSource


class ReportRequest(BaseModel):
    """Body of ``POST /api/reports``."""

    model_config = ConfigDict(extra="forbid")

    topic: str = Field(
        min_length=1,
        max_length=MAX_QUESTION_CHARS,
        description="What the report should cover.",
    )
    sources: list[ReportSource] = Field(
        default_factory=lambda: ["documents"],
        min_length=1,
        max_length=3,
        description=(
            "Evidence to gather: 'documents' (the caller's own corpus), 'analytics' "
            "(one SQL query over the demo dataset), 'web' (live web search; requires "
            "TAVILY_API_KEY, otherwise the request fails with 503). Defaults to "
            "'documents' only - the report never silently widens its sources."
        ),
    )

    @field_validator("topic")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("The topic must not be blank.")
        return value

    @field_validator("sources")
    @classmethod
    def _deduplicate(cls, value: list[ReportSource]) -> list[ReportSource]:
        deduplicated = list(dict.fromkeys(value))
        if not deduplicated:
            raise ValueError("At least one source must be requested.")
        return deduplicated


class ReportResponse(BaseModel):
    """Body of ``POST /api/reports``.

    ``grounded`` is False when no requested source returned evidence; then
    ``report`` states that plainly and no model was asked to write anything.
    """

    report: str = Field(description="The Markdown report.")
    topic: str
    sources_requested: list[str]
    grounded: bool
    sources: list[SourceCitation] = Field(
        default_factory=list,
        description="Document passages used, labelled [1], [2] as the report cites them.",
    )
    web_sources: list[WebSource] = Field(
        default_factory=list,
        description="Web results used, labelled W1, W2 as the report cites them.",
    )
    analytics: AnalyticsEvidence | None = Field(
        default=None,
        description="The query and rows used, when analytics evidence was requested.",
    )
    provider: str
    model: str
    usage: TokenUsage | None = None
    request_id: str
