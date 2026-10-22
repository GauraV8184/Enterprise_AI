"""The report endpoint.

A report is the deliverable that combines capabilities: documents, SQL results,
and web research, assembled into one Markdown document with every claim tied to
the evidence it came from. The response returns that evidence alongside the
report, so a reader can verify it instead of trusting it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from app.agents.report import generate_report
from app.api.deps import (
    ChatModelDep,
    EmbeddingsDep,
    RepositoryDep,
    SettingsDep,
    WebSearchDep,
)
from app.core.logging import get_logger, request_id_ctx, safe_extra
from app.schemas.chat import TokenUsage
from app.schemas.documents import SourceCitation
from app.schemas.evidence import AnalyticsEvidence, WebSource
from app.schemas.reports import ReportRequest, ReportResponse
from app.security.audit import record_event
from app.security.identity import CurrentUserDep
from app.security.rate_limit import model_rate_limit

logger = get_logger(__name__)

router = APIRouter(prefix="/reports", tags=["reports"])


@router.post(
    "",
    response_model=ReportResponse,
    summary="Generate an evidence-backed Markdown report",
    dependencies=[Depends(model_rate_limit)],
)
async def create_report(
    payload: ReportRequest,
    model: ChatModelDep,
    repository: RepositoryDep,
    embeddings: EmbeddingsDep,
    settings: SettingsDep,
    web_search: WebSearchDep,
    user: CurrentUserDep,
) -> ReportResponse:
    """Gather the requested evidence and write the report from it.

    A request whose sources all come back empty is not an error: the response
    explains that with ``grounded=false`` and a report body that says so, and
    the model is never asked to write from nothing. Requesting ``web`` without a
    configured key is a 503, because quietly skipping a requested source would
    misrepresent what the report is based on.
    """
    outcome = await generate_report(
        repository,
        embeddings,
        model,
        web_search=web_search,
        owner_id=user.user_id,
        topic=payload.topic,
        sources=list(payload.sources),
        top_k=settings.retrieval_top_k,
        max_context_chars=settings.max_context_chars,
        min_score=settings.retrieval_min_score,
        max_rows=settings.analytics_max_rows,
        web_max_results=settings.web_search_max_results,
    )

    evidence = outcome.evidence
    analytics = evidence.analytics
    logger.info(
        "report_completed",
        extra=safe_extra(
            grounded=outcome.grounded,
            requested=evidence.requested,
            documents=len(evidence.documents),
            web_sources=len(evidence.web),
            analytics_rows=analytics.row_count if analytics else 0,
        ),
    )
    await run_in_threadpool(
        record_event,
        repository,
        user=user,
        action="report.generated",
        resource_type="report",
        detail={
            "grounded": outcome.grounded,
            "sources_requested": evidence.requested,
            "documents": len(evidence.documents),
            "web_sources": len(evidence.web),
            "analytics_rows": analytics.row_count if analytics else 0,
        },
    )

    usage = outcome.usage
    return ReportResponse(
        report=outcome.report,
        topic=payload.topic,
        sources_requested=evidence.requested,
        grounded=outcome.grounded,
        sources=[
            SourceCitation.from_match(index, match)
            for index, match in enumerate(evidence.documents, 1)
        ],
        web_sources=[
            WebSource.from_result(f"W{index}", result)
            for index, result in enumerate(evidence.web, 1)
        ],
        analytics=AnalyticsEvidence.from_result(analytics) if analytics else None,
        provider=settings.llm_provider,
        model=settings.openai_model,
        usage=TokenUsage(**usage.__dict__) if usage else None,
        request_id=request_id_ctx.get(),
    )
