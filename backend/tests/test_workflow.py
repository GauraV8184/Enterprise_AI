"""The supervisor graph: routing, state, and honest failure modes.

The chat model is faked; the repository, embeddings, chunking and ranking are
real. Routing assertions are meaningful because the classifier result is
controlled exactly as the structured-output call would return it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.database.repositories import ChunkRecord, DocumentRepository, new_id
from app.graph.workflow import route_for_intent, run_workflow, sources_for_intent
from app.llm.errors import LlmProviderError
from app.llm.prompts import DEFAULT_SYSTEM_PROMPT
from app.llm.schemas import AnalyticsQuery, SearchPlan, TaskIntent
from app.tools.web_search import WebResult, WebSearchNotConfiguredError
from tests.fakes import FakeChatModel, FakeWebSearch, HashingEmbeddings, make_status_error

LEAVE_TEXT = (
    "Every full-time employee accrues twenty days of paid annual leave per calendar "
    "year, and unused leave carries over up to five days."
)

ALICE = "alice"


def make_intent(
    intent: str,
    *,
    documents: bool = False,
    business: bool = False,
    web: bool = False,
    complexity: str = "simple",
) -> TaskIntent:
    return TaskIntent(
        intent=intent,  # type: ignore[arg-type]
        requires_internal_documents=documents,
        requires_business_data=business,
        requires_web_research=web,
        complexity=complexity,  # type: ignore[arg-type]
        summary="The user asked something.",
    )


def make_model(result: TaskIntent | None) -> FakeChatModel:
    return FakeChatModel(replies=["A conversational answer."], structured_result=result)


def seed_document(repository: DocumentRepository, embeddings: HashingEmbeddings) -> str:
    """Store one ready-to-search document and return its id."""
    document = repository.create_document(
        owner_id=ALICE,
        filename="handbook.txt",
        content_type="text/plain",
        extension=".txt",
        size_bytes=len(LEAVE_TEXT),
    )
    repository.add_chunks(
        [
            ChunkRecord(
                id=new_id(),
                document_id=document.id,
                owner_id=ALICE,
                chunk_index=0,
                content=LEAVE_TEXT,
                page=1,
                section=None,
                embedding=np.array(embeddings.embed_query(LEAVE_TEXT), dtype=np.float32),
            )
        ]
    )
    repository.update_status(
        owner_id=ALICE, document_id=document.id, status="ready", chunk_count=1, mark_indexed=True
    )
    return document.id


@pytest.fixture
def repository(tmp_path: Path) -> DocumentRepository:
    return DocumentRepository(tmp_path / "workflow" / "app.db")


@pytest.fixture
def embeddings() -> HashingEmbeddings:
    return HashingEmbeddings()


def test_route_for_intent_prefers_report_then_documents_then_the_rest() -> None:
    # Report wins outright: it is the route that gathers more than one source.
    assert route_for_intent(make_intent("report")) == "report"
    assert route_for_intent(make_intent("report", documents=True, business=True)) == "report"

    # Documents win over the other single-source routes.
    assert route_for_intent(make_intent("document_question", documents=True)) == "documents"
    assert route_for_intent(make_intent("general_chat", documents=True)) == "documents"

    # Explicit intents and flags map to their capabilities.
    assert route_for_intent(make_intent("research")) == "research"
    assert route_for_intent(make_intent("general_chat", web=True)) == "research"
    assert route_for_intent(make_intent("data_analysis")) == "analytics"
    assert route_for_intent(make_intent("general_chat", business=True)) == "analytics"

    assert route_for_intent(make_intent("general_chat")) == "chat"


def test_sources_for_intent_maps_flags_to_evidence() -> None:
    assert sources_for_intent(make_intent("report")) == ["documents"]
    assert sources_for_intent(make_intent("report", documents=True, business=True)) == [
        "documents",
        "analytics",
    ]
    assert sources_for_intent(make_intent("research", business=True, web=True)) == [
        "analytics",
        "web",
    ]


async def test_general_chat_routes_to_chat(
    repository: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = make_model(make_intent("general_chat"))

    state = await run_workflow(
        model,
        repository,
        embeddings,
        owner_id=ALICE,
        question="Hello there",
        top_k=5,
        max_context_chars=4000,
        min_score=0.1,
    )

    assert state["route"] == "chat"
    assert state["intent"].intent == "general_chat"
    assert state["answer"] == "A conversational answer."
    assert state["grounded"] is None
    assert state["sources"] == []
    assert model.received[0][0].content == DEFAULT_SYSTEM_PROMPT


async def test_chat_history_reaches_the_model_in_order(
    repository: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = make_model(make_intent("general_chat"))

    await run_workflow(
        model,
        repository,
        embeddings,
        owner_id=ALICE,
        question="And the leave policy?",
        history=[
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
        ],
        top_k=5,
        max_context_chars=4000,
        min_score=0.1,
    )

    sent = model.received[0]
    assert [type(message).__name__ for message in sent] == [
        "SystemMessage",
        "HumanMessage",
        "AIMessage",
        "HumanMessage",
    ]
    assert [message.content for message in sent[1:]] == ["Hi", "Hello", "And the leave policy?"]


async def test_document_question_routes_to_documents_with_citations(
    repository: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    seed_document(repository, embeddings)
    model = make_model(make_intent("document_question", documents=True))
    model.replies = ["According to [1], employees accrue twenty days."]

    state = await run_workflow(
        model,
        repository,
        embeddings,
        owner_id=ALICE,
        question="How many days of paid annual leave does a full-time employee accrue?",
        top_k=3,
        max_context_chars=4000,
        min_score=0.1,
    )

    assert state["route"] == "documents"
    assert state["grounded"] is True
    assert state["answer"] == "According to [1], employees accrue twenty days."
    assert [source.filename for source in state["sources"]] == ["handbook.txt"]
    assert state["searched_chunks"] == 1
    assert state["usage"] is None


async def test_internal_documents_flag_wins_over_other_single_source_intents(
    repository: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    seed_document(repository, embeddings)
    model = make_model(make_intent("research", documents=True, web=True, complexity="multi_step"))
    model.replies = ["According to [1], twenty days."]

    state = await run_workflow(
        model,
        repository,
        embeddings,
        owner_id=ALICE,
        question="Summarise the leave policy from the handbook.",
        top_k=3,
        max_context_chars=4000,
        min_score=0.1,
    )

    assert state["route"] == "documents"
    assert state["grounded"] is True


async def test_empty_corpus_is_answered_without_calling_the_model(
    repository: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = make_model(make_intent("document_question", documents=True))

    state = await run_workflow(
        model,
        repository,
        embeddings,
        owner_id=ALICE,
        question="How many days of annual leave?",
        top_k=3,
        max_context_chars=4000,
        min_score=0.1,
    )

    assert state["route"] == "documents"
    assert state["grounded"] is False
    assert "No documents have been indexed" in state["answer"]
    assert state["sources"] == []
    assert state["searched_chunks"] == 0
    assert model.received == []


async def test_classification_failure_is_raised_not_guessed(
    repository: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = make_model(None)
    model.structured_error = make_status_error(503)

    with pytest.raises(LlmProviderError):
        await run_workflow(
            model,
            repository,
            embeddings,
            owner_id=ALICE,
            question="Hello there",
            top_k=5,
            max_context_chars=4000,
            min_score=0.1,
        )


async def test_answer_failure_surfaces_from_the_chat_route(
    repository: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = make_model(make_intent("general_chat"))
    model.error = make_status_error(503)

    with pytest.raises(LlmProviderError):
        await run_workflow(
            model,
            repository,
            embeddings,
            owner_id=ALICE,
            question="Hello there",
            top_k=5,
            max_context_chars=4000,
            min_score=0.1,
        )


WEB_RESULT = WebResult(
    title="Leave law update",
    url="https://example.com/leave-law",
    content="Statutory leave remains 28 days.",
    score=0.8,
)


async def test_data_questions_route_to_sql_and_return_the_rows(
    repository: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = FakeChatModel(
        replies=["The catalogue holds 4 products."],
        structured_results=[
            make_intent("data_analysis", business=True),
            AnalyticsQuery(
                sql="select count(*) as n from demo_products",
                reasoning="Counts catalogue products.",
            ),
        ],
    )

    state = await run_workflow(
        model,
        repository,
        embeddings,
        owner_id=ALICE,
        question="How many products are in the demo catalogue?",
        top_k=5,
        max_context_chars=4000,
        min_score=0.1,
    )

    assert state["route"] == "analytics"
    assert state["grounded"] is True
    assert state["analytics"].rows == [{"n": 4}]
    assert state["sources"] == []
    assert state["answer"] == "The catalogue holds 4 products."


async def test_research_questions_route_to_web_search(
    repository: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = FakeChatModel(
        replies=["Statutory leave remains 28 days [W1]."],
        structured_results=[
            make_intent("research", web=True),
            SearchPlan(queries=["statutory leave uk"]),
        ],
    )

    state = await run_workflow(
        model,
        repository,
        embeddings,
        owner_id=ALICE,
        question="What is the current statutory leave entitlement?",
        top_k=5,
        max_context_chars=4000,
        min_score=0.1,
        web_search=FakeWebSearch(results=[WEB_RESULT]),
    )

    assert state["route"] == "research"
    assert state["grounded"] is True
    assert [source.url for source in state["web_sources"]] == [WEB_RESULT.url]
    assert state["web_queries"] == ["statutory leave uk"]
    assert "[W1]" in state["answer"]


async def test_research_without_a_provider_fails_as_503(
    repository: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = FakeChatModel(structured_results=[make_intent("research", web=True)])

    with pytest.raises(WebSearchNotConfiguredError):
        await run_workflow(
            model,
            repository,
            embeddings,
            owner_id=ALICE,
            question="What is the latest news on leave law?",
            top_k=5,
            max_context_chars=4000,
            min_score=0.1,
            web_search=None,
        )


async def test_report_questions_write_markdown_from_the_flagged_sources(
    repository: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    seed_document(repository, embeddings)
    model = FakeChatModel(
        replies=["# Report\n\nTwenty days [1] and 4 products."],
        structured_results=[
            make_intent("report", documents=True, business=True),
            AnalyticsQuery(
                sql="select count(*) as n from demo_products",
                reasoning="Counts catalogue products.",
            ),
        ],
    )

    state = await run_workflow(
        model,
        repository,
        embeddings,
        owner_id=ALICE,
        question="Report on the leave policy and the product catalogue.",
        top_k=3,
        max_context_chars=4000,
        min_score=0.1,
    )

    assert state["route"] == "report"
    assert state["grounded"] is True
    assert state["answer"].startswith("# Report")
    assert [source.filename for source in state["sources"]] == ["handbook.txt"]
    assert state["analytics"].rows == [{"n": 4}]


async def test_report_without_evidence_is_stated_but_not_invented(
    repository: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = FakeChatModel(
        replies=["This must not be used."],
        structured_results=[make_intent("report")],
    )

    state = await run_workflow(
        model,
        repository,
        embeddings,
        owner_id=ALICE,
        question="Report on the missing leave policy.",
        top_k=3,
        max_context_chars=4000,
        min_score=0.1,
    )

    assert state["route"] == "report"
    assert state["grounded"] is False
    assert "No evidence was found" in state["answer"]
    assert model.received == []
