"""Retrieval and grounded answering, without an LLM for the ranking tests.

These tests use :class:`HashingEmbeddings`, whose similarity genuinely tracks shared
vocabulary. That makes the ranking assertions meaningful: a passage about leave
policy really does score above one about device encryption.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.core.errors import AppError
from app.database.errors import EmbeddingDimensionMismatchError
from app.database.repositories import (
    ChunkMatch,
    ChunkRecord,
    ChunkSearchResult,
    DocumentRepository,
    new_id,
)
from app.llm.errors import LlmConnectionError
from app.rag.retriever import (
    DEFAULT_MIN_SCORE,
    answer_question,
    format_context,
    search,
)
from tests.fakes import FakeChatModel, HashingEmbeddings, make_connection_error

LEAVE_TEXT = (
    "Every full-time employee accrues twenty days of paid annual leave per calendar "
    "year, and unused leave carries over up to five days."
)
ENCRYPTION_TEXT = (
    "All laptops must have full-disk encryption enabled before they are issued to staff."
)
REVENUE_TEXT = (
    "Total revenue for the third quarter reached 4,820,000 dollars, an increase of "
    "twelve percent over the previous quarter."
)

ALICE = "alice"
BOB = "bob"


def seed_chunk(
    repository: DocumentRepository,
    embeddings: HashingEmbeddings,
    *,
    filename: str,
    content: str,
    owner: str = ALICE,
    page: int | None = None,
    section: str | None = None,
    chunk_index: int = 0,
) -> str:
    """Store one chunk and return its document id."""
    document = repository.create_document(
        owner_id=owner,
        filename=filename,
        content_type="text/plain",
        extension=".txt",
        size_bytes=len(content),
    )
    vector = embeddings.embed_query(content)
    repository.add_chunks(
        [
            ChunkRecord(
                id=new_id(),
                document_id=document.id,
                owner_id=owner,
                chunk_index=chunk_index,
                content=content,
                page=page,
                section=section,
                embedding=np.array(vector, dtype=np.float32),
            )
        ]
    )
    repository.update_status(
        owner_id=owner, document_id=document.id, status="ready", chunk_count=1, mark_indexed=True
    )
    return document.id


@pytest.fixture
def repository(tmp_path: Path) -> DocumentRepository:
    return DocumentRepository(tmp_path / "rag" / "app.db")


@pytest.fixture
def embeddings() -> HashingEmbeddings:
    return HashingEmbeddings()


@pytest.fixture
def corpus(repository: DocumentRepository, embeddings: HashingEmbeddings) -> DocumentRepository:
    """Three unrelated documents, so ranking has something to get wrong."""
    seed_chunk(
        repository,
        embeddings,
        filename="handbook.pdf",
        content=LEAVE_TEXT,
        page=4,
        section="Leave",
    )
    seed_chunk(repository, embeddings, filename="laptops.txt", content=ENCRYPTION_TEXT)
    seed_chunk(repository, embeddings, filename="revenue.txt", content=REVENUE_TEXT)
    return repository


def test_relevant_passage_ranks_first(
    corpus: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    result = search(corpus, embeddings, owner_id=ALICE, query="annual leave allowance", top_k=3)

    assert result.found
    assert result.matches[0].filename == "handbook.pdf"
    assert result.searched_chunks == 3


def test_ranking_follows_shared_vocabulary(
    corpus: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    """Swapping the query swaps the winner - proving similarity drives the order."""
    result = search(corpus, embeddings, owner_id=ALICE, query="full-disk encryption", top_k=3)

    assert result.matches[0].filename == "laptops.txt"


def test_page_and_section_travel_with_the_match(
    corpus: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    result = search(corpus, embeddings, owner_id=ALICE, query="paid annual leave", top_k=1)

    match = result.matches[0]
    assert match.page == 4
    assert match.section == "Leave"
    assert match.citation == "handbook.pdf, page 4 (Leave)"


def test_top_k_limits_results(corpus: DocumentRepository, embeddings: HashingEmbeddings) -> None:
    result = search(corpus, embeddings, owner_id=ALICE, query="revenue leave", top_k=2)
    assert len(result.matches) == 2


def test_scores_are_ordered_descending(
    corpus: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    result = search(corpus, embeddings, owner_id=ALICE, query="revenue and leave", top_k=3)
    scores = [match.score for match in result.matches]
    assert scores == sorted(scores, reverse=True)


def test_min_score_filters_weak_matches(
    corpus: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    """A high floor should drop unrelated passages rather than pad the results."""
    everything = search(corpus, embeddings, owner_id=ALICE, query="annual leave", top_k=3)
    strict = search(
        corpus, embeddings, owner_id=ALICE, query="annual leave", top_k=3, min_score=0.5
    )

    assert len(strict.matches) < len(everything.matches)
    assert all(match.score >= 0.5 for match in strict.matches)


def test_empty_corpus_is_not_an_error(
    repository: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    """A new user searching before uploading anything gets an answer, not a 500."""
    result = search(repository, embeddings, owner_id=ALICE, query="anything", top_k=3)

    assert result.found is False
    assert result.searched_chunks == 0


def test_blank_query_is_rejected(
    repository: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    with pytest.raises(AppError, match="must not be blank"):
        search(repository, embeddings, owner_id=ALICE, query="   ", top_k=3)


def test_embedding_outage_is_a_typed_provider_error(repository: DocumentRepository) -> None:
    """A dead embedding endpoint must not escape as an unhandled 500."""

    class UnreachableEmbeddings(HashingEmbeddings):
        def embed_query(self, text: str) -> list[float]:
            raise make_connection_error()

    with pytest.raises(LlmConnectionError):
        search(repository, UnreachableEmbeddings(), owner_id=ALICE, query="leave", top_k=3)


def test_search_delegates_ranking_to_the_store() -> None:
    """The retriever must not assume the vectors are local.

    This is what lets the Supabase store rank inside pgvector instead of
    downloading every embedding to Python. If ``search`` reached for
    ``fetch_chunks`` again, the Postgres path would quietly turn into a full scan
    over the network - still correct, and useless at scale.
    """

    class StoreWithoutFetch:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def search_chunks(self, **kwargs: object) -> ChunkSearchResult:
            self.calls.append(kwargs)
            return ChunkSearchResult(
                matches=[
                    ChunkMatch(
                        id="c1",
                        document_id="d1",
                        filename="handbook.pdf",
                        content="twenty days",
                        page=4,
                        section="Leave",
                        chunk_index=0,
                        score=0.9,
                    )
                ],
                searched_chunks=4,
            )

    store = StoreWithoutFetch()
    result = search(  # type: ignore[arg-type]
        store, HashingEmbeddings(), owner_id=ALICE, query="leave", top_k=2
    )

    assert store.calls[0]["top_k"] == 2
    assert store.calls[0]["owner_id"] == ALICE
    assert store.calls[0]["min_score"] == DEFAULT_MIN_SCORE
    assert DEFAULT_MIN_SCORE > 0.0
    assert result.searched_chunks == 4
    assert result.matches[0].chunk_id == "c1"
    assert result.matches[0].citation == "handbook.pdf, page 4 (Leave)"


def test_search_only_covers_your_own_documents(
    corpus: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    seed_chunk(
        corpus,
        embeddings,
        filename="bob-secrets.txt",
        content="Bob's confidential annual leave arrangements for the board.",
        owner=BOB,
    )

    alice = search(corpus, embeddings, owner_id=ALICE, query="annual leave", top_k=10)

    assert all(match.filename != "bob-secrets.txt" for match in alice.matches)
    assert alice.searched_chunks == 3


def test_search_can_be_limited_to_chosen_documents(
    corpus: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    revenue_id = next(
        record.id
        for record in corpus.list_documents(owner_id=ALICE)
        if record.filename == "revenue.txt"
    )

    result = search(
        corpus,
        embeddings,
        owner_id=ALICE,
        query="annual leave and revenue",
        top_k=5,
        document_ids=[revenue_id],
    )

    assert {match.filename for match in result.matches} == {"revenue.txt"}
    assert result.searched_chunks == 1


def test_changing_embedding_model_fails_instead_of_returning_noise(
    corpus: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    """Comparing stored vectors with differently-sized queries is meaningless.

    Without this guard the mismatch would surface as a NumPy broadcast error, or
    worse as a silently wrong result, neither of which tells the user that their
    embedding model changed.
    """

    class DifferentDimensions(HashingEmbeddings):
        dimensions: int = 8

    with pytest.raises(EmbeddingDimensionMismatchError) as excinfo:
        search(corpus, DifferentDimensions(), owner_id=ALICE, query="annual leave", top_k=3)

    assert excinfo.value.status_code == 409
    message = excinfo.value.message
    assert str(embeddings.dimensions) in message
    assert str(DifferentDimensions.dimensions) in message
    assert "EMBEDDING_MODEL" in message


def _match(index: int, content: str, score: float = 0.9):
    from app.rag.retriever import RetrievedChunk

    return RetrievedChunk(
        chunk_id=f"chunk-{index}",
        document_id=f"doc-{index}",
        filename=f"file{index}.pdf",
        content=content,
        page=index,
        section=None,
        score=score,
    )


def test_context_labels_start_at_one() -> None:
    context, included = format_context([_match(1, "alpha"), _match(2, "beta")], max_chars=10_000)

    assert "[1] Source: file1.pdf, page 1" in context
    assert "[2] Source: file2.pdf, page 2" in context
    assert len(included) == 2


def test_sources_never_include_what_the_context_left_out() -> None:
    """Citations and context must agree, or the model cites a missing source."""
    context, included = format_context([_match(1, "a" * 200), _match(2, "b" * 200)], max_chars=250)

    assert len(included) == 1
    assert "[2]" not in context
    assert included[0].chunk_id == "chunk-1"


def test_a_single_oversized_match_is_still_included() -> None:
    """Dropping everything would turn a long passage into a false 'nothing found'."""
    context, included = format_context([_match(1, "x" * 5000)], max_chars=100)

    assert len(included) == 1
    assert context


def test_empty_matches_produce_empty_context() -> None:
    context, included = format_context([], max_chars=1000)
    assert context == ""
    assert included == []


async def test_answer_is_grounded_in_retrieved_context(
    corpus: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = FakeChatModel(replies=["Employees accrue twenty days of annual leave [1]."])

    result = await answer_question(
        corpus,
        embeddings,
        model,
        owner_id=ALICE,
        question="How much annual leave do employees get?",
        top_k=3,
        max_context_chars=8000,
    )

    assert result.grounded is True
    assert result.sources
    assert result.sources[0].filename == "handbook.pdf"

    sent = "\n".join(str(message.content) for message in model.received[0])
    assert "twenty days of paid annual leave" in sent
    assert "[1]" in sent


def test_answer_uses_the_rag_system_prompt(
    corpus: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    """The grounding rules must reach the model, not just the user message."""
    import asyncio

    model = FakeChatModel(replies=["answer"])

    asyncio.run(
        answer_question(
            corpus,
            embeddings,
            model,
            owner_id=ALICE,
            question="What is the leave policy?",
            top_k=2,
            max_context_chars=8000,
        )
    )

    system_messages = [m for m in model.received[0] if m.type == "system"]
    assert len(system_messages) == 1
    assert "Answer ONLY from the numbered context blocks" in str(system_messages[0].content)


async def test_model_is_not_called_when_nothing_is_retrieved(
    repository: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    """Asking a model to answer from an empty context is how hallucinations start."""
    model = FakeChatModel(replies=["invented answer"])

    result = await answer_question(
        repository,
        embeddings,
        model,
        owner_id=ALICE,
        question="What is our leave policy?",
        top_k=3,
        max_context_chars=8000,
    )

    assert result.grounded is False
    assert result.sources == []
    assert model.received == [], "the LLM was called with no context"
    assert "No documents have been indexed yet" in result.answer


async def test_the_store_call_runs_off_the_event_loop(
    embeddings: HashingEmbeddings,
) -> None:
    """The stores block (SQLite, psycopg, PostgREST); the loop must stay free.

    Without ``run_in_threadpool`` this coroutine would hold the event loop for
    the whole query, stalling every other request handled by the worker.
    """
    import threading

    loop_thread = threading.get_ident()
    seen: list[int] = []

    class RecordingStore:
        def search_chunks(self, **_kwargs: object) -> ChunkSearchResult:
            seen.append(threading.get_ident())
            return ChunkSearchResult(matches=[], searched_chunks=0)

    result = await answer_question(
        RecordingStore(),  # type: ignore[arg-type]
        embeddings,
        FakeChatModel(replies=[]),
        owner_id=ALICE,
        question="anything",
        top_k=3,
        max_context_chars=8000,
    )

    assert result.grounded is False
    assert seen, "the store was never called"
    assert seen[0] != loop_thread


async def test_no_match_above_threshold_explains_itself(
    corpus: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = FakeChatModel(replies=["invented answer"])

    result = await answer_question(
        corpus,
        embeddings,
        model,
        owner_id=ALICE,
        question="What is the leave policy?",
        top_k=3,
        max_context_chars=8000,
        min_score=0.999,
    )

    assert result.grounded is False
    assert model.received == []
    assert "No relevant content was found" in result.answer
    assert result.searched_chunks == 3


async def test_answer_question_rejects_a_blank_question(
    corpus: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    with pytest.raises(AppError, match="must not be blank"):
        await answer_question(
            corpus,
            embeddings,
            FakeChatModel(),
            owner_id=ALICE,
            question="  ",
            top_k=3,
            max_context_chars=8000,
        )
