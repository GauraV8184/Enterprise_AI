"""End-to-end RAG check against the real providers.

Unlike ``scripts/check_llm.py`` (which only proves the chat model answers), this
runs the whole document pipeline: parse -> chunk -> embed -> store -> retrieve ->
answer with citations.

It uses a temporary data directory, so running it never touches your real
``data/`` folder and never leaves documents behind.

Usage (from the ``backend`` directory):

    .\\.venv\\Scripts\\python.exe scripts\\check_rag.py

Exit codes: 0 = everything worked, 1 = something is not configured or failed.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.database.repositories import DocumentRepository  # noqa: E402
from app.llm.errors import LlmNotConfiguredError  # noqa: E402
from app.llm.factory import build_chat_model, build_embeddings  # noqa: E402
from app.rag.ingest import ingest_document  # noqa: E402
from app.rag.retriever import answer_question, search  # noqa: E402

SAMPLE = (
    "Employee Handbook\n\n"
    "Section 1: Annual leave\n"
    "Every full-time employee accrues twenty days of paid annual leave per calendar "
    "year. Unused leave may be carried over, up to a maximum of five days."
)

QUESTION = "How many days of paid annual leave does a full-time employee accrue?"


def _line(label: str, value: object) -> None:
    print(f"  {label:<28} {value}")


async def main() -> int:
    settings = Settings()

    print("\nEmbedding configuration")
    print("-" * 60)
    _line("provider", settings.embedding_provider)
    _line("model", settings.embedding_model)
    _line("endpoint", settings.effective_embedding_base_url or "(provider default)")
    _line("chunk size / overlap", f"{settings.chunk_size} / {settings.chunk_overlap}")
    _line("retrieval top-k", settings.retrieval_top_k)

    if not settings.llm_configured or not settings.embeddings_configured:
        print("\nRESULT: not configured.")
        print("  Set LLM_PROVIDER=ollama and EMBEDDING_PROVIDER=ollama in .env,")
        print("  then:  ollama pull llama3.1:8b  &&  ollama pull nomic-embed-text")
        return 1

    try:
        embeddings = build_embeddings(settings)
        model = build_chat_model(settings)
    except LlmNotConfiguredError as exc:
        print(f"\nRESULT: not configured - {exc.message}")
        return 1

    with tempfile.TemporaryDirectory() as directory:
        workdir = Path(directory)
        document_path = workdir / "handbook.txt"
        document_path.write_text(SAMPLE, encoding="utf-8")

        repository = DocumentRepository(workdir / "app.db")
        record = repository.create_document(
            owner_id="check-rag",
            filename="handbook.txt",
            content_type="text/plain",
            extension=".txt",
            size_bytes=document_path.stat().st_size,
        )

        print("\n1) Embedding and indexing")
        print("-" * 60)
        try:
            result = ingest_document(
                repository=repository,
                embeddings=embeddings,
                owner_id="check-rag",
                document_id=record.id,
                data=document_path.read_bytes(),
                filename="handbook.txt",
                chunk_size=settings.chunk_size,
                chunk_overlap=settings.chunk_overlap,
            )
        except Exception as exc:  # surface the real reason rather than a traceback
            print(f"  FAILED: {type(exc).__name__}: {exc}")
            print("\nRESULT: FAILED - the document could not be indexed.")
            print("  Is the embedding model pulled?  ollama pull nomic-embed-text")
            return 1

        _line("status", result.document.status)
        _line("chunks stored", result.chunk_count)
        _line("chunks readable", repository.count_chunks(owner_id="check-rag"))

        print("\n2) Semantic search (no LLM involved)")
        print("-" * 60)
        found = search(
            repository,
            embeddings,
            owner_id="check-rag",
            query=QUESTION,
            top_k=settings.retrieval_top_k,
        )
        _line("passages compared", found.searched_chunks)
        _line("passages returned", len(found.matches))
        if not found.found:
            print("\nRESULT: FAILED - retrieval returned nothing.")
            return 1
        _line("best score", round(found.matches[0].score, 4))
        _line("best source", found.matches[0].citation)
        print(f"  snippet: {found.matches[0].snippet[:120]}")

        print("\n3) Grounded answer (real model, must cite the source)")
        print("-" * 60)
        answer = await answer_question(
            repository,
            embeddings,
            model,
            owner_id="check-rag",
            question=QUESTION,
            top_k=settings.retrieval_top_k,
            max_context_chars=settings.max_context_chars,
        )
        _line("grounded", answer.grounded)
        _line("citations", len(answer.sources))
        print(f"\n  Q: {QUESTION}")
        print(f"  A: {answer.answer}")

        if not answer.grounded:
            print("\nRESULT: FAILED - no context reached the model.")
            return 1

    correct = "twenty" in answer.answer.lower() or "20" in answer.answer
    cited = "[1]" in answer.answer

    print("\nChecks")
    print("-" * 60)
    print(f"  {'PASS' if correct else 'FAIL'}  the answer contains the figure from the document")
    print(f"  {'PASS' if cited else 'FAIL'}  the answer cites its source")

    if correct and cited:
        print("\nRESULT: OK - ingestion, retrieval and grounded answering all work.")
        return 0

    print("\nRESULT: FAILED - the answer was not grounded in the document.")
    print("  A small local model may need a clearer prompt. Try a larger model via OPENAI_MODEL.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
