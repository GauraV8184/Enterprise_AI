"""The supervisor workflow.

One graph, one decision, five ways to answer:

``classify`` -> ``answer_directly``     (conversational requests)
             -> ``answer_from_documents`` (the caller's own corpus)
             -> ``answer_from_research``  (live web search)
             -> ``answer_from_data``      (SQL over the demo sales dataset)
             -> ``write_report``          (whichever sources it is told to use)

The routing decision is a pure function of the structured intent, so it is
testable without a model. Each terminal node calls the real Phase 2/3/6 code
path - the graph adds the decision, not a parallel implementation.

Why a route is only added with its tool: routing a request to a capability that
cannot answer it would be a lie. Each node below therefore depends on real,
tested code, and a route that is missing a dependency fails loudly (503) rather
than degrading into a guess.
"""

from __future__ import annotations

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.analyst import answer_with_data
from app.agents.report import ReportSource, generate_report
from app.agents.research import answer_with_research
from app.core.logging import get_logger, safe_extra
from app.database.repositories import DocumentStore
from app.graph.state import WorkflowRoute, WorkflowState
from app.llm.schemas import TaskIntent
from app.llm.service import classify_intent, generate_answer
from app.rag.retriever import answer_question
from app.tools.web_search import WebSearch, WebSearchNotConfiguredError

logger = get_logger(__name__)


def route_for_intent(intent: TaskIntent) -> WorkflowRoute:
    """Pick the capability that can answer this request.

    Priority, and why it is this order:

    1. ``report`` wins outright. It is the only route that is *asked* to combine
       sources, and it gathers whichever ones the classification flags say the
       request needs.
    2. ``documents`` before research and analytics: a request that names the
       internal corpus is answered from it. Combining a corpus answer with live
       web or data is exactly what the report route is for.
    3. ``research`` and ``analytics`` follow their explicit intent or flags.
    4. Everything else is conversational.
    """
    if intent.intent == "report":
        return "report"
    if intent.intent == "document_question" or intent.requires_internal_documents:
        return "documents"
    if intent.intent == "research" or intent.requires_web_research:
        return "research"
    if intent.intent == "data_analysis" or intent.requires_business_data:
        return "analytics"
    return "chat"


def sources_for_intent(intent: TaskIntent) -> list[ReportSource]:
    """Which evidence a report gathers, from the classification flags.

    Defaulting to documents when the classifier flagged nothing keeps the report
    honest in the common case: it either uses the internal corpus or says the
    corpus had nothing, rather than writing from general knowledge.
    """
    sources: list[ReportSource] = []
    if intent.requires_internal_documents or intent.intent == "document_question":
        sources.append("documents")
    if intent.requires_business_data or intent.intent == "data_analysis":
        sources.append("analytics")
    if intent.requires_web_research or intent.intent == "research":
        sources.append("web")
    return sources or ["documents"]


def _require_web_search(web_search: WebSearch | None) -> WebSearch:
    if web_search is None:
        raise WebSearchNotConfiguredError()
    return web_search


def build_workflow(
    *,
    model: BaseChatModel,
    repository: DocumentStore,
    embeddings: Embeddings,
    web_search: WebSearch | None,
    top_k: int,
    max_context_chars: int,
    min_score: float,
    analytics_max_rows: int,
    web_max_results: int,
) -> CompiledStateGraph:
    """Compile the graph with its dependencies bound.

    The repository is caller-scoped - in Supabase mode it carries the request's
    access token - so the graph is built per request. Compiling is in-memory
    work only; nothing here performs I/O.
    """

    async def classify(state: WorkflowState) -> WorkflowState:
        intent = await classify_intent(model, text=state["question"])
        route = route_for_intent(intent)
        logger.info(
            "workflow_routed",
            extra=safe_extra(intent=intent.intent, route=route, complexity=intent.complexity),
        )
        return {"intent": intent, "route": route}

    async def answer_directly(state: WorkflowState) -> WorkflowState:
        history = [*state.get("history", []), {"role": "user", "content": state["question"]}]
        completion = await generate_answer(model, history=history)
        return {
            "answer": completion.text,
            "grounded": None,
            "sources": [],
            "searched_chunks": 0,
            "usage": completion.usage,
        }

    async def answer_from_documents(state: WorkflowState) -> WorkflowState:
        result = await answer_question(
            repository,
            embeddings,
            model,
            owner_id=state["owner_id"],
            question=state["question"],
            top_k=top_k,
            max_context_chars=max_context_chars,
            document_ids=state.get("document_ids"),
            min_score=min_score,
        )
        return {
            "answer": result.answer,
            "grounded": result.grounded,
            "sources": result.sources,
            "searched_chunks": result.searched_chunks,
            "usage": result.usage,
        }

    async def answer_from_research(state: WorkflowState) -> WorkflowState:
        outcome = await answer_with_research(
            _require_web_search(web_search),
            model,
            question=state["question"],
            max_results=web_max_results,
        )
        return {
            "answer": outcome.answer,
            "grounded": outcome.grounded,
            "sources": [],
            "searched_chunks": 0,
            "web_sources": outcome.results,
            "web_queries": outcome.queries,
            "usage": outcome.usage,
        }

    async def answer_from_data(state: WorkflowState) -> WorkflowState:
        outcome = await answer_with_data(
            repository,
            model,
            question=state["question"],
            max_rows=analytics_max_rows,
        )
        return {
            "answer": outcome.answer,
            "grounded": outcome.grounded,
            "sources": [],
            "searched_chunks": 0,
            "analytics": outcome.analytics,
            "usage": outcome.usage,
        }

    async def write_report(state: WorkflowState) -> WorkflowState:
        outcome = await generate_report(
            repository,
            embeddings,
            model,
            web_search=web_search,
            owner_id=state["owner_id"],
            topic=state["question"],
            sources=sources_for_intent(state["intent"]),
            top_k=top_k,
            max_context_chars=max_context_chars,
            min_score=min_score,
            max_rows=analytics_max_rows,
            web_max_results=web_max_results,
            document_ids=state.get("document_ids"),
        )
        return {
            "answer": outcome.report,
            "grounded": outcome.grounded,
            "sources": outcome.evidence.documents,
            "searched_chunks": outcome.evidence.searched_chunks,
            "web_sources": outcome.evidence.web,
            "web_queries": outcome.evidence.web_queries,
            "analytics": outcome.evidence.analytics,
            "usage": outcome.usage,
        }

    def next_node(state: WorkflowState) -> str:
        return state["route"]

    builder: StateGraph = StateGraph(WorkflowState)
    builder.add_node("classify", classify)
    builder.add_node("answer_directly", answer_directly)
    builder.add_node("answer_from_documents", answer_from_documents)
    builder.add_node("answer_from_research", answer_from_research)
    builder.add_node("answer_from_data", answer_from_data)
    builder.add_node("write_report", write_report)
    builder.add_edge(START, "classify")
    builder.add_conditional_edges(
        "classify",
        next_node,
        {
            "chat": "answer_directly",
            "documents": "answer_from_documents",
            "research": "answer_from_research",
            "analytics": "answer_from_data",
            "report": "write_report",
        },
    )
    builder.add_edge("answer_directly", END)
    builder.add_edge("answer_from_documents", END)
    builder.add_edge("answer_from_research", END)
    builder.add_edge("answer_from_data", END)
    builder.add_edge("write_report", END)
    return builder.compile()


async def run_workflow(
    model: BaseChatModel,
    repository: DocumentStore,
    embeddings: Embeddings,
    *,
    owner_id: str,
    question: str,
    top_k: int,
    max_context_chars: int,
    min_score: float,
    web_search: WebSearch | None = None,
    analytics_max_rows: int = 100,
    web_max_results: int = 5,
    history: list[dict[str, str]] | None = None,
    document_ids: list[str] | None = None,
) -> WorkflowState:
    """Run one request through the supervisor and return the final state.

    Raises:
        AppError: classification, retrieval, search, SQL, or answering failed.
            The graph does not catch these - a failed request is reported, never
            answered with a guess.
    """
    workflow = build_workflow(
        model=model,
        repository=repository,
        embeddings=embeddings,
        web_search=web_search,
        top_k=top_k,
        max_context_chars=max_context_chars,
        min_score=min_score,
        analytics_max_rows=analytics_max_rows,
        web_max_results=web_max_results,
    )
    initial: WorkflowState = {
        "question": question,
        "history": history or [],
        "owner_id": owner_id,
        "document_ids": document_ids,
    }
    return await workflow.ainvoke(initial)
