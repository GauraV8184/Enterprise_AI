"""Evidence blocks shared by the workflow and report responses.

These describe *what was found*, not which route found it: the same web result
can back a research answer or a report section, and both callers should see the
same shape for it.

Labels are strings here rather than ints because web citations carry a ``W``
prefix (``W1``). In a report that mixes sources, a document passage cited as
``[1]`` must never be confusable with the first web result - so document
citations keep the Phase 3 integer labels and web citations never use them.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.tools.analytics import AnalyticsResult
from app.tools.web_search import WebResult


class WebSource(BaseModel):
    """One web result as returned to the caller, with its citation label."""

    label: str = Field(description="The bracketed label the model was given, like 'W1'.")
    title: str
    url: str
    snippet: str = Field(description="The provider's excerpt, whitespace-collapsed.")
    score: float | None = Field(
        default=None,
        description="Provider relevance score, when it reports one. Comparable only within one response.",
    )

    @classmethod
    def from_result(cls, label: str, result: WebResult) -> WebSource:
        return cls(
            label=label,
            title=result.title,
            url=result.url,
            snippet=result.snippet,
            score=result.score,
        )


class AnalyticsEvidence(BaseModel):
    """The query that ran and the rows it returned.

    Unlabelled on purpose: a request produces at most one analytics result, so a
    ``[1]``-style label would imply a sibling that does not exist.
    """

    sql: str = Field(description="The validated statement that ran.")
    row_count: int = Field(description="Rows returned.")
    truncated: bool = Field(
        description=(
            "True when the row cap was reached, so more rows may exist than were "
            "returned. The answer states this when it applies."
        )
    )
    rows: list[dict[str, Any]] = Field(default_factory=list)

    @classmethod
    def from_result(cls, result: AnalyticsResult) -> AnalyticsEvidence:
        return cls(
            sql=result.sql,
            row_count=result.row_count,
            truncated=result.truncated,
            rows=result.rows,
        )
