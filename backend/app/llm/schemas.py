"""Structured-output schemas produced by the LLM.

These are Pydantic models, which means the same definition validates the LLM's
response *and* documents the contract for the API. ``TaskIntent`` tells the
LangGraph supervisor which capability a request needs; ``AnalyticsQuery`` and
``SearchPlan`` are the planning outputs of the Phase 6 specialist agents."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

IntentKind = Literal[
    "general_chat",
    "document_question",
    "data_analysis",
    "research",
    "report",
]


class TaskIntent(BaseModel):
    """What the user is asking for, and which capabilities it needs."""

    intent: IntentKind = Field(description="The single best-fitting category for the request.")
    requires_internal_documents: bool = Field(
        description="True if answering needs internal documents, policies, or uploaded files."
    )
    requires_business_data: bool = Field(
        description="True if answering needs structured business data (sales, revenue, customers)."
    )
    requires_web_research: bool = Field(
        description="True if answering needs external/current information from the public web."
    )
    complexity: Literal["simple", "multi_step"] = Field(
        description="'simple' for one-step answers, 'multi_step' when several tools must be combined."
    )
    summary: str = Field(
        description="One sentence restating the request in neutral terms, with no invented details."
    )


class AnalyticsQuery(BaseModel):
    """One SQL query proposed by the model for the analytics tool.

    Validated by the analyst agent before execution - the schema only fixes the
    shape, not the safety of the statement.
    """

    sql: str = Field(
        description=(
            "A single read-only SELECT statement (WITH ... SELECT is fine). "
            "No semicolon, no INSERT/UPDATE/DELETE/DDL, no comments."
        )
    )
    reasoning: str = Field(
        description="One sentence describing what the query computes, in neutral terms."
    )


class SearchPlan(BaseModel):
    """The web searches the research agent will run."""

    queries: list[str] = Field(
        min_length=1,
        max_length=3,
        description="One to three short keyword phrases a search engine expects.",
    )
