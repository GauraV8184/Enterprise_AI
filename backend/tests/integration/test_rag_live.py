"""Integration tests that talk to the real embedding and chat providers.

Skipped by default. Run explicitly with

    $env:RUN_LIVE_LLM_TESTS="1"; python -m pytest tests/integration -v

For a local Ollama setup this needs:

    ollama pull llama3.1:8b
    ollama pull nomic-embed-text

These tests exist to catch things the fake embeddings cannot: a model that is not
pulled, a dimension change between model versions, and whether a small local model
actually obeys the instruction to cite its sources.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.config import Settings
from app.database.repositories import DocumentRepository
from app.llm.errors import LlmNotConfiguredError
from app.llm.factory import build_chat_model, build_embeddings
from app.rag.ingest import ingest_document
from app.rag.retriever import answer_question, search
from tests.sample_files import write_text_file

pytestmark = pytest.mark.integration

_RUN_LIVE = os.getenv("RUN_LIVE_LLM_TESTS") == "1"

skip_reason = (
    "Set RUN_LIVE_LLM_TESTS=1 to run live RAG tests "
    "(requires a reachable provider and a pulled embedding model)."
)


@pytest.fixture(scope="module")
def live_settings() -> Settings:
    settings = Settings()
    if not settings.llm_configured:
        pytest.skip("The LLM provider is not configured in the repo-root .env")
    if not settings.embeddings_configured:
        pytest.skip("The embedding provider is not configured in the repo-root .env")
    return settings


@pytest.fixture(scope="module")
def live_embeddings(live_settings: Settings):
    try:
        return build_embeddings(live_settings)
    except LlmNotConfiguredError:  # pragma: no cover - defensive
        pytest.skip("Embedding provider is not configured")


@pytest.mark.skipif(not _RUN_LIVE, reason=skip_reason)
def test_live_embedding_produces_a_consistent_dimension(live_embeddings) -> None:
    """Two calls must agree on length, or stored vectors cannot be compared."""
    vectors = live_embeddings.embed_documents(["annual leave policy", "device encryption"])

    assert len(vectors) == 2
    assert len(vectors[0]) == len(vectors[1])
    assert len(vectors[0]) > 0
    assert any(value != 0.0 for value in vectors[0]), "embedding is all zeros"


@pytest.mark.skipif(not _RUN_LIVE, reason=skip_reason)
def test_live_semantic_search_prefers_the_related_passage(
    live_settings: Settings, live_embeddings, tmp_path: Path
) -> None:
    """A real embedding model must rank by meaning, not by word overlap.

    The query shares no distinctive noun with the target text, so a keyword match
    would not find it - only a genuinely semantic model will.
    """
    repository = DocumentRepository(tmp_path / "live" / "app.db")
    samples = tmp_path / "samples"
    samples.mkdir()

    path = write_text_file(samples)
    record = repository.create_document(
        owner_id="live-user",
        filename="notes.txt",
        content_type="text/plain",
        extension=".txt",
        size_bytes=path.stat().st_size,
    )

    result = ingest_document(
        repository=repository,
        embeddings=live_embeddings,
        owner_id="live-user",
        document_id=record.id,
        data=path.read_bytes(),
        filename="notes.txt",
        chunk_size=live_settings.chunk_size,
        chunk_overlap=live_settings.chunk_overlap,
    )
    assert result.succeeded
    assert result.chunk_count > 0

    found = search(
        repository,
        live_embeddings,
        owner_id="live-user",
        query="How many days off am I entitled to each year?",
        top_k=1,
    )

    assert found.found
    assert "leave" in found.matches[0].content.lower()


@pytest.mark.skipif(not _RUN_LIVE, reason=skip_reason)
async def test_live_grounded_answer_cites_its_source(
    live_settings: Settings, live_embeddings, tmp_path: Path
) -> None:
    """The end-to-end path: upload -> index -> retrieve -> answer with a citation."""
    try:
        model = build_chat_model(live_settings)
    except LlmNotConfiguredError:  # pragma: no cover - defensive
        pytest.skip("LLM provider is not configured")

    repository = DocumentRepository(tmp_path / "live" / "answer.db")
    samples = tmp_path / "samples"
    samples.mkdir()

    path = write_text_file(samples)
    record = repository.create_document(
        owner_id="live-user",
        filename="notes.txt",
        content_type="text/plain",
        extension=".txt",
        size_bytes=path.stat().st_size,
    )
    ingest_document(
        repository=repository,
        embeddings=live_embeddings,
        owner_id="live-user",
        document_id=record.id,
        data=path.read_bytes(),
        filename="notes.txt",
        chunk_size=live_settings.chunk_size,
        chunk_overlap=live_settings.chunk_overlap,
    )

    answer = await answer_question(
        repository,
        live_embeddings,
        model,
        owner_id="live-user",
        question="How many days of annual leave does a full-time employee accrue?",
        top_k=live_settings.retrieval_top_k,
        max_context_chars=live_settings.max_context_chars,
    )

    assert answer.grounded is True
    assert answer.sources, "no citation was returned"
    # The correct figure is twenty days; a model answering from memory would not
    # know this, so finding it here shows the answer came from the document.
    assert "twenty" in answer.answer.lower() or "20" in answer.answer
    assert "[1]" in answer.answer, f"the model did not cite its source: {answer.answer!r}"
