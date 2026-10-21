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
from app.graph.workflow import route_for_intent, run_workflow
from app.llm.errors import LlmProviderError
from app.llm.prompts import DEFAULT_SYSTEM_PROMPT
from app.llm.schemas import TaskIntent
from tests.fakes import FakeChatModel, HashingEmbeddings, make_status_error

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


def test_route_for_intent_sends_document_needs_to_the_corpus() -> None:
    assert route_for_intent(make_intent("document_question", documents=True)) == "documents"
    assert route_for_intent(make_intent("report", documents=True)) == "documents"
    assert route_for_intent(make_intent("general_chat")) == "chat"
    assert route_for_intent(make_intent("research")) == "chat"


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


async def test_internal_documents_flag_wins_over_the_intent_label(
    repository: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    seed_document(repository, embeddings)
    model = make_model(
        make_intent("report", documents=True, business=True, complexity="multi_step")
    )
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
