"""End-to-end check of the supervisor workflow against the real providers.

The graph classifies each request, routes it, and answers with the capability
the routing decision selected. Two requests exercise both routes that exist
today:

1. a conversational message -> the chat route
2. a question about an uploaded document -> the documents route, with citation

It uses a temporary data directory, so running it never touches your real
``data/`` folder and never leaves documents behind. The store is local SQLite;
the point of this check is the graph and the model, not Supabase.

Usage (from the ``backend`` directory):

    .\\.venv\\Scripts\\python.exe scripts\\check_workflow.py

Exit codes: 0 = both routes worked, 1 = something is not configured or failed.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.database.repositories import DocumentRepository  # noqa: E402
from app.graph.workflow import run_workflow  # noqa: E402
from app.llm.errors import LlmNotConfiguredError  # noqa: E402
from app.llm.factory import build_chat_model, build_embeddings  # noqa: E402
from app.rag.ingest import ingest_document  # noqa: E402

OWNER = "check-workflow"

SAMPLE = (
    "Employee Handbook\n\n"
    "Section 1: Annual leave\n"
    "Every full-time employee accrues twenty days of paid annual leave per calendar "
    "year. Unused leave may be carried over, up to a maximum of five days."
)

GREETING = "Hello! Who are you?"

QUESTION = "How many days of paid annual leave does a full-time employee accrue?"


def _line(label: str, value: object) -> None:
    print(f"  {label:<28} {value}")


def _shorten(text: str, limit: int = 160) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "..."


async def main() -> int:
    settings = Settings()

    print("\nWorkflow configuration")
    print("-" * 60)
    _line("provider", settings.llm_provider)
    _line("model", settings.openai_model)
    _line("embedding model", settings.embedding_model)
    _line("retrieval top-k", settings.retrieval_top_k)
    _line("relevance floor", settings.retrieval_min_score)

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

    checks: list[tuple[str, bool]] = []

    with tempfile.TemporaryDirectory() as directory:
        workdir = Path(directory)
        document_path = workdir / "handbook.txt"
        document_path.write_text(SAMPLE, encoding="utf-8")

        repository = DocumentRepository(workdir / "app.db")
        record = repository.create_document(
            owner_id=OWNER,
            filename="handbook.txt",
            content_type="text/plain",
            extension=".txt",
            size_bytes=document_path.stat().st_size,
        )
        ingest_document(
            repository=repository,
            embeddings=embeddings,
            owner_id=OWNER,
            document_id=record.id,
            data=document_path.read_bytes(),
            filename="handbook.txt",
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
        )

        print("\n1) Conversational request (expect the chat route)")
        print("-" * 60)
        chat_state = await run_workflow(
            model,
            repository,
            embeddings,
            owner_id=OWNER,
            question=GREETING,
            top_k=settings.retrieval_top_k,
            max_context_chars=settings.max_context_chars,
            min_score=settings.retrieval_min_score,
        )
        _line("intent", chat_state["intent"].intent)
        _line("route", chat_state["route"])
        print(f"  A: {_shorten(chat_state['answer'])}")

        checks.append(("a greeting is routed to chat", chat_state["route"] == "chat"))
        checks.append(("the chat answer is non-empty", bool(chat_state["answer"].strip())))

        print("\n2) Document question (expect the documents route)")
        print("-" * 60)
        document_state = await run_workflow(
            model,
            repository,
            embeddings,
            owner_id=OWNER,
            question=QUESTION,
            top_k=settings.retrieval_top_k,
            max_context_chars=settings.max_context_chars,
            min_score=settings.retrieval_min_score,
        )
        _line("intent", document_state["intent"].intent)
        _line("route", document_state["route"])
        _line("grounded", document_state["grounded"])
        _line("citations", len(document_state["sources"]))
        print(f"\n  Q: {QUESTION}")
        print(f"  A: {document_state['answer']}")

        answer_text = document_state["answer"].lower()
        checks.append(
            ("the question is routed to documents", document_state["route"] == "documents")
        )
        checks.append(("the answer is grounded", document_state["grounded"] is True))
        checks.append(("at least one source is attached", len(document_state["sources"]) >= 1))
        checks.append(
            ("the answer contains the figure", "twenty" in answer_text or "20" in answer_text)
        )
        checks.append(("the answer cites its source", "[1]" in document_state["answer"]))

    print("\nChecks")
    print("-" * 60)
    for label, passed in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")

    if all(passed for _, passed in checks):
        print("\nRESULT: OK - classification, routing and both answer paths work.")
        return 0

    print("\nRESULT: FAILED - see the failing checks above.")
    print("  A small local model may classify ambiguously. Try a larger model via OPENAI_MODEL,")
    print("  or inspect the intent printed for each request.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
