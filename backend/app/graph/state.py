"""The state object every node of the workflow graph reads and updates.

``total=False`` because nodes return partial updates: the classifier adds
``intent`` and ``route``, an answer node adds ``answer`` and the evidence
behind it. LangGraph merges each returned mapping into the state, so a node
never has to echo back fields it did not touch.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from app.llm.schemas import TaskIntent
from app.llm.service import TokenUsage
from app.rag.retriever import RetrievedChunk
from app.tools.analytics import AnalyticsResult
from app.tools.web_search import WebResult

WorkflowRoute = Literal["chat", "documents", "research", "analytics", "report"]


class WorkflowState(TypedDict, total=False):
    """Everything one request through the graph carries."""

    question: str
    history: list[dict[str, str]]
    owner_id: str
    document_ids: list[str] | None

    intent: TaskIntent
    route: WorkflowRoute

    answer: str
    grounded: bool | None
    sources: list[RetrievedChunk]
    searched_chunks: int
    web_sources: list[WebResult]
    web_queries: list[str]
    analytics: AnalyticsResult | None
    usage: TokenUsage | None
