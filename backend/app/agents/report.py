"""The report agent: gather the requested evidence, then write Markdown from it.

A report is the one deliverable that combines capabilities, so the agent is
built from the other two: documents come from Phase 3 retrieval, analytics from
the analyst's query pipeline, web results from the research agent's search
pipeline. There is no third implementation of any of them here - that is what
keeps a report's figures identical to what the dedicated routes would return.

**No evidence, no report.** If every requested source comes back empty, the
model is not called at all: it would have nothing to write from, and a
plausible-looking report built from nothing is the single most damaging output
this platform could produce. The caller gets an explicit statement instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from starlette.concurrency import run_in_threadpool

from app.agents.analyst import generate_and_run_query
from app.agents.research import format_web_context, gather_web_evidence
from app.core.logging import get_logger, safe_extra
from app.database.repositories import DocumentStore
from app.llm.prompts import REPORT_SYSTEM_PROMPT, build_report_prompt
from app.llm.service import TokenUsage, generate_answer
from app.rag.retriever import RetrievedChunk, format_context, search
from app.security.prompt_injection import prepare_untrusted_text
from app.tools.analytics import AnalyticsResult, render_rows
from app.tools.web_search import WebResult, WebSearch, WebSearchNotConfiguredError

logger = get_logger(__name__)

ReportSource = Literal["documents", "analytics", "web"]

MAX_TITLE_CHARS = 120


@dataclass(frozen=True)
class ReportEvidence:
    """Everything the report was written from, in the order it was gathered."""

    requested: list[str] = field(default_factory=list)
    documents: list[RetrievedChunk] = field(default_factory=list)
    searched_chunks: int = 0
    analytics: AnalyticsResult | None = None
    web: list[WebResult] = field(default_factory=list)
    web_queries: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReportOutcome:
    """The Markdown report, its evidence, and whether any evidence existed."""

    report: str
    evidence: ReportEvidence
    grounded: bool
    usage: TokenUsage | None = None


def _title(topic: str) -> str:
    """One safe, single-line title: collapse whitespace and cap the length.

    The topic comes from the caller, so newlines are collapsed rather than
    echoed - a topic cannot inject extra Markdown sections into the report
    header that the model never wrote.
    """
    collapsed = " ".join(topic.split())
    if len(collapsed) <= MAX_TITLE_CHARS:
        return collapsed
    return collapsed[:MAX_TITLE_CHARS].rsplit(" ", 1)[0] + "..."


def no_evidence_report(*, topic: str, gaps: list[str]) -> str:
    """The honest answer when no source produced anything to write from."""
    missing = ", ".join(gaps) if gaps else "the requested sources"
    return (
        f"# Report: {_title(topic)}\n\n"
        "## Summary\n\n"
        f"No evidence was found for this request: {missing} returned nothing. The report "
        "was not written, because a report without evidence would have to be invented.\n\n"
        "## Limitations\n\n"
        "- No matching documents or data rows were found, and web results were either "
        "empty or not requested.\n"
        "- Upload or index the relevant documents, broaden the request, or check the "
        "configured sources, then try again.\n"
    )


async def generate_report(
    store: DocumentStore,
    embeddings: Embeddings,
    model: BaseChatModel,
    *,
    web_search: WebSearch | None,
    owner_id: str,
    topic: str,
    sources: list[ReportSource],
    top_k: int,
    max_context_chars: int,
    min_score: float,
    max_rows: int,
    web_max_results: int,
    document_ids: list[str] | None = None,
) -> ReportOutcome:
    """Gather the requested evidence and compose a Markdown report.

    Raises:
        WebSearchNotConfiguredError: web evidence was requested but no search
            provider is configured. Requesting less is better than silently
            delivering a report that claims to have searched.
        AppError: retrieval, analytics, search, or provider failures.
    """
    requested = list(dict.fromkeys(sources))
    blocks: list[str] = []
    gaps: list[str] = []

    documents: list[RetrievedChunk] = []
    searched_chunks = 0
    analytics: AnalyticsResult | None = None
    web_results: list[WebResult] = []
    web_queries: list[str] = []

    if "documents" in requested:
        result = await run_in_threadpool(
            search,
            store,
            embeddings,
            owner_id=owner_id,
            query=topic,
            top_k=top_k,
            document_ids=document_ids,
            min_score=min_score,
        )
        searched_chunks = result.searched_chunks
        if result.matches:
            context, documents = format_context(result.matches, max_chars=max_context_chars)
            safe_context = prepare_untrusted_text(context, source="documents")
            blocks.append(f"<documents>\n{safe_context}\n</documents>")
        else:
            gaps.append("internal documents")

    if "analytics" in requested:
        analytics = await generate_and_run_query(store, model, question=topic, max_rows=max_rows)
        if analytics.rows:
            rows_text, shown = render_rows(analytics.rows)
            body = (
                f"SQL: {analytics.sql}\n"
                f"Rows ({shown} of {analytics.row_count} shown, as JSON):\n{rows_text}"
            )
            safe_body = prepare_untrusted_text(body, source="analytics_rows")
            blocks.append(f"<analytics>\n{safe_body}\n</analytics>")
        else:
            gaps.append("business data (the query ran and matched no rows)")

    if "web" in requested:
        if web_search is None:
            raise WebSearchNotConfiguredError()
        gathering = await gather_web_evidence(
            web_search, model, topic=topic, max_results=web_max_results
        )
        web_queries = gathering.queries
        if gathering.results:
            context, web_results = format_web_context(gathering.results)
            safe_context = prepare_untrusted_text(context, source="web_search")
            blocks.append(f"<web_results>\n{safe_context}\n</web_results>")
        else:
            gaps.append("web search")

    evidence = ReportEvidence(
        requested=requested,
        documents=documents,
        searched_chunks=searched_chunks,
        analytics=analytics,
        web=web_results,
        web_queries=web_queries,
    )

    if not blocks:
        logger.warning(
            "report_without_evidence",
            extra=safe_extra(requested=requested, gaps=len(gaps)),
        )
        return ReportOutcome(
            report=no_evidence_report(topic=topic, gaps=gaps),
            evidence=evidence,
            grounded=False,
        )

    prompt = build_report_prompt(topic=topic, evidence="\n\n".join(blocks), gaps=gaps)
    completion = await generate_answer(
        model,
        history=[{"role": "user", "content": prompt}],
        system_prompt=REPORT_SYSTEM_PROMPT,
    )

    logger.info(
        "report_generated",
        extra=safe_extra(
            requested=requested,
            documents=len(documents),
            analytics_rows=analytics.row_count if analytics else 0,
            web=len(web_results),
            gaps=len(gaps),
        ),
    )
    return ReportOutcome(
        report=completion.text,
        evidence=evidence,
        grounded=True,
        usage=completion.usage,
    )
