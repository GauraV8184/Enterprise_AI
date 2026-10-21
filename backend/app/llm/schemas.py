"""Structured-output schemas produced by the LLM.

These are Pydantic models, which means the same definition validates the LLM's
response *and* documents the contract for the API. ``TaskIntent`` is the first
of these: classifying what a user is actually asking for is the input the
LangGraph supervisor needs in Phase 5 to decide which specialist agents to run.
"""

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
