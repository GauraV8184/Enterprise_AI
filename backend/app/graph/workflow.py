"""The supervisor workflow.

One graph, three nodes:

``classify`` -> ``answer_directly`` (conversational requests)
             -> ``answer_from_documents`` (anything that needs the corpus)

The routing decision is a pure function of the structured intent, so it is
testable without a model. Each terminal node calls the real Phase 2/3 code
path - the graph adds the decision, not a parallel implementation.

Phase 6 adds specialist agents and tools; the state and routing contract stay
the same, and the tool-backed routes only appear once a tool actually exists.
Routing a request to a capability that cannot answer it would be a lie.
"""

from __future__ import annotations

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.core.logging import get_logger, safe_extra
from app.database.repositories import DocumentStore
from app.graph.state import WorkflowRoute, WorkflowState
from app.llm.schemas import TaskIntent
from app.llm.service import classify_intent, generate_answer
from app.rag.retriever import answer_question

logger = get_logger(__name__)


def route_for_intent(intent: TaskIntent) -> WorkflowRoute:
    """Pick the capability that can answer this request today.

    Documents win whenever the answer is meant to come from the corpus - either
    because the request is a document question or because the classification
    says internal documents are required. Everything else is a conversational
    answer. Business-data, web-research and report routes arrive with their
    tools in Phase 6.
    """
    if intent.intent == "document_question" or intent.requires_internal_documents:
        return "documents"
    return "chat"


def build_workflow(
    *,
    model: BaseChatModel,
    repository: DocumentStore,
    embeddings: Embeddings,
    top_k: int,
    max_context_chars: int,
    min_score: float,
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

    def next_node(state: WorkflowState) -> str:
        return state["route"]

    builder: StateGraph = StateGraph(WorkflowState)
    builder.add_node("classify", classify)
    builder.add_node("answer_directly", answer_directly)
    builder.add_node("answer_from_documents", answer_from_documents)
    builder.add_edge(START, "classify")
    builder.add_conditional_edges(
        "classify",
        next_node,
        {"chat": "answer_directly", "documents": "answer_from_documents"},
    )
    builder.add_edge("answer_directly", END)
    builder.add_edge("answer_from_documents", END)
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
    history: list[dict[str, str]] | None = None,
    document_ids: list[str] | None = None,
) -> WorkflowState:
    """Run one request through the supervisor and return the final state.

    Raises:
        AppError: classification or answering failed. The graph does not catch
            these - a failed request is reported, never answered with a guess.
    """
    workflow = build_workflow(
        model=model,
        repository=repository,
        embeddings=embeddings,
        top_k=top_k,
        max_context_chars=max_context_chars,
        min_score=min_score,
    )
    initial: WorkflowState = {
        "question": question,
        "history": history or [],
        "owner_id": owner_id,
        "document_ids": document_ids,
    }
    return await workflow.ainvoke(initial)
