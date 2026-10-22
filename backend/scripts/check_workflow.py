"""End-to-end check of the supervisor workflow against the real providers.

The graph classifies each request, routes it, and answers with the capability
the routing decision selected. The requests below exercise every route that
exists today:

1. a conversational message -> the chat route
2. a question about an uploaded document -> the documents route, with citation
3. a question about sales data -> the analytics route, which generates SQL,
   runs it against the seeded demo dataset, and summarises the rows
4. a report request -> the report route, which gathers evidence and returns
   Markdown
5. a research request -> the research route, with cited web sources. This one is
   skipped honestly when no TAVILY_API_KEY is configured; the route answers 503
   in that state, and a check that "passed" without searching would be a lie.

It uses a temporary data directory, so running it never touches your real
``data/`` folder and never leaves documents behind. The store is local SQLite;
the point of this check is the graph, the tools, and the model, not Supabase.

Usage (from the ``backend`` directory):

    .\\.venv\\Scripts\\python.exe scripts\\check_workflow.py

Exit codes: 0 = every running route worked, 1 = something is not configured or
failed. A skipped research check does not fail the run.
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
from app.tools.web_search import build_web_search  # noqa: E402

OWNER = "check-workflow"

SAMPLE = (
    "Employee Handbook\n\n"
    "Section 1: Annual leave\n"
    "Every full-time employee accrues twenty days of paid annual leave per calendar "
    "year. Unused leave may be carried over, up to a maximum of five days."
)

GREETING = "Hello! Who are you?"

QUESTION = "How many days of paid annual leave does a full-time employee accrue?"

DATA_QUESTION = "How many sales orders are in the demo dataset?"

REPORT_TOPIC = "Write a report on the employee leave policy and the demo sales revenue by region."

RESEARCH_QUESTION = "What is the current UK statutory annual leave entitlement?"


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
    _line("analytics row cap", settings.analytics_max_rows)
    _line("web search", "tavily" if settings.web_search_configured else "not configured")

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

    web_search = build_web_search(settings)
    checks: list[tuple[str, bool]] = []

    try:
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            document_path = workdir / "handbook.txt"
            document_path.write_text(SAMPLE, encoding="utf-8")

            run_kwargs = {
                "top_k": settings.retrieval_top_k,
                "max_context_chars": settings.max_context_chars,
                "min_score": settings.retrieval_min_score,
                "web_search": web_search,
                "analytics_max_rows": settings.analytics_max_rows,
                "web_max_results": settings.web_search_max_results,
            }

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

            await check_chat(model, repository, embeddings, run_kwargs, checks)
            await check_documents(model, repository, embeddings, run_kwargs, checks)
            await check_analytics(model, repository, embeddings, run_kwargs, checks)
            await check_report(model, repository, embeddings, run_kwargs, checks)
            await check_research(model, repository, embeddings, run_kwargs, checks)
    finally:
        if web_search is not None:
            close = getattr(web_search, "close", None)
            if close is not None:
                close()

    print("\nChecks")
    print("-" * 60)
    for label, passed in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")

    if all(passed for _, passed in checks):
        print("\nRESULT: OK - every configured route answered through its own path.")
        return 0

    print("\nRESULT: FAILED - see the failing checks above.")
    print("  A small local model may classify ambiguously or write invalid SQL. Try a")
    print("  larger model via OPENAI_MODEL, or inspect the intent printed for each request.")
    print("  A long report prompt can also exceed LLM_TIMEOUT_SECONDS on a slow local")
    print("  model; raise it for this run, e.g. LLM_TIMEOUT_SECONDS=240.")
    return 1


async def check_chat(
    model: object,
    repository: DocumentRepository,
    embeddings: object,
    run_kwargs: dict[str, object],
    checks: list[tuple[str, bool]],
) -> None:
    print("\n1) Conversational request (expect the chat route)")
    print("-" * 60)
    state = await run_workflow(
        model,  # type: ignore[arg-type]
        repository,
        embeddings,  # type: ignore[arg-type]
        owner_id=OWNER,
        question=GREETING,
        **run_kwargs,  # type: ignore[arg-type]
    )
    _line("intent", state["intent"].intent)
    _line("route", state["route"])
    print(f"  A: {_shorten(state['answer'])}")

    checks.append(("a greeting is routed to chat", state["route"] == "chat"))
    checks.append(("the chat answer is non-empty", bool(state["answer"].strip())))


async def check_documents(
    model: object,
    repository: DocumentRepository,
    embeddings: object,
    run_kwargs: dict[str, object],
    checks: list[tuple[str, bool]],
) -> None:
    print("\n2) Document question (expect the documents route)")
    print("-" * 60)
    state = await run_workflow(
        model,  # type: ignore[arg-type]
        repository,
        embeddings,  # type: ignore[arg-type]
        owner_id=OWNER,
        question=QUESTION,
        **run_kwargs,  # type: ignore[arg-type]
    )
    _line("intent", state["intent"].intent)
    _line("route", state["route"])
    _line("grounded", state["grounded"])
    _line("citations", len(state.get("sources", [])))
    print(f"\n  Q: {QUESTION}")
    print(f"  A: {state['answer']}")

    answer_text = state["answer"].lower()
    checks.append(("the question is routed to documents", state["route"] == "documents"))
    checks.append(("the answer is grounded", state["grounded"] is True))
    checks.append(("at least one source is attached", len(state.get("sources", [])) >= 1))
    checks.append(
        ("the answer contains the figure", "twenty" in answer_text or "20" in answer_text)
    )
    checks.append(("the answer cites its source", "[1]" in state["answer"]))


async def check_analytics(
    model: object,
    repository: DocumentRepository,
    embeddings: object,
    run_kwargs: dict[str, object],
    checks: list[tuple[str, bool]],
) -> None:
    print("\n3) Data question (expect the analytics route)")
    print("-" * 60)
    state = await run_workflow(
        model,  # type: ignore[arg-type]
        repository,
        embeddings,  # type: ignore[arg-type]
        owner_id=OWNER,
        question=DATA_QUESTION,
        **run_kwargs,  # type: ignore[arg-type]
    )
    analytics = state.get("analytics")
    _line("intent", state["intent"].intent)
    _line("route", state["route"])
    _line("grounded", state["grounded"])
    _line("rows", analytics.row_count if analytics else 0)
    _line("truncated", analytics.truncated if analytics else None)
    if analytics:
        _line("sql", _shorten(analytics.sql, 100))
        _line("first row", analytics.rows[0] if analytics.rows else None)
    print(f"\n  Q: {DATA_QUESTION}")
    print(f"  A: {state['answer']}")

    checks.append(("the question is routed to analytics", state["route"] == "analytics"))
    checks.append(("the query returned rows", bool(analytics and analytics.rows)))
    checks.append(("the answer is grounded", state["grounded"] is True))
    checks.append(
        (
            "the SQL is a read-only SELECT",
            bool(analytics and analytics.sql.lower().lstrip().startswith(("select", "with"))),
        )
    )


async def check_report(
    model: object,
    repository: DocumentRepository,
    embeddings: object,
    run_kwargs: dict[str, object],
    checks: list[tuple[str, bool]],
) -> None:
    print("\n4) Report request (expect the report route)")
    print("-" * 60)
    state = await run_workflow(
        model,  # type: ignore[arg-type]
        repository,
        embeddings,  # type: ignore[arg-type]
        owner_id=OWNER,
        question=REPORT_TOPIC,
        **run_kwargs,  # type: ignore[arg-type]
    )
    _line("intent", state["intent"].intent)
    _line("route", state["route"])
    _line("grounded", state["grounded"])
    _line("document sources", len(state.get("sources", [])))
    _line("analytics rows", state["analytics"].row_count if state.get("analytics") else 0)
    print(f"\n  Q: {REPORT_TOPIC}")
    print(f"  A:\n{state['answer']}")

    checks.append(("the request is routed to report", state["route"] == "report"))
    checks.append(("the report is Markdown", state["answer"].lstrip().startswith("#")))
    checks.append(("the report is grounded", state["grounded"] is True))
    checks.append(
        (
            "document evidence is attached",
            len(state.get("sources", [])) >= 1 or state.get("analytics") is not None,
        )
    )


async def check_research(
    model: object,
    repository: DocumentRepository,
    embeddings: object,
    run_kwargs: dict[str, object],
    checks: list[tuple[str, bool]],
) -> None:
    print("\n5) Research question (needs TAVILY_API_KEY)")
    print("-" * 60)

    if run_kwargs.get("web_search") is None:
        print("  SKIP: web search is not configured, so the research route answers 503.")
        print("        Set TAVILY_API_KEY in .env and re-run to exercise it.")
        return

    state = await run_workflow(
        model,  # type: ignore[arg-type]
        repository,
        embeddings,  # type: ignore[arg-type]
        owner_id=OWNER,
        question=RESEARCH_QUESTION,
        **run_kwargs,  # type: ignore[arg-type]
    )
    web_sources = state.get("web_sources", [])
    _line("intent", state["intent"].intent)
    _line("route", state["route"])
    _line("grounded", state["grounded"])
    _line("web sources", len(web_sources))
    if web_sources:
        _line("first source", _shorten(web_sources[0].url, 100))
    print(f"\n  Q: {RESEARCH_QUESTION}")
    print(f"  A: {state['answer']}")

    checks.append(("the question is routed to research", state["route"] == "research"))
    checks.append(("web sources are attached", len(web_sources) >= 1))
    checks.append(("the answer cites a web source", "[W1]" in state["answer"]))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
