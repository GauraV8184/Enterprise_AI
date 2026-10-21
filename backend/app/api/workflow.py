"""The supervisor workflow endpoint.

One endpoint decides how a request should be answered - conversationally, or
from the caller's own documents - and answers with the same code the dedicated
endpoints use. The decision travels back with the answer, so a caller can
always see which capability ran and whether grounding applies.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import ChatModelDep, EmbeddingsDep, RepositoryDep, SettingsDep
from app.core.logging import get_logger, request_id_ctx, safe_extra
from app.graph.workflow import run_workflow
from app.schemas.chat import TokenUsage
from app.schemas.documents import SourceCitation
from app.schemas.workflow import WorkflowRequest, WorkflowResponse
from app.security.identity import CurrentUserDep

logger = get_logger(__name__)

router = APIRouter(prefix="/workflow", tags=["workflow"])


@router.post(
    "/run",
    response_model=WorkflowResponse,
    summary="Classify a request and answer it with the right capability",
)
async def run_supervisor(
    payload: WorkflowRequest,
    model: ChatModelDep,
    repository: RepositoryDep,
    embeddings: EmbeddingsDep,
    settings: SettingsDep,
    user: CurrentUserDep,
) -> WorkflowResponse:
    """Run one request through the supervisor graph.

    A classification or provider failure is reported as an error; the graph
    has no fallback that would answer with a guess.
    """
    state = await run_workflow(
        model,
        repository,
        embeddings,
        owner_id=user.user_id,
        question=payload.message,
        history=[message.model_dump() for message in payload.history],
        document_ids=payload.document_ids,
        top_k=settings.retrieval_top_k,
        max_context_chars=settings.max_context_chars,
        min_score=settings.retrieval_min_score,
    )

    sources = state.get("sources", [])
    logger.info(
        "workflow_completed",
        extra=safe_extra(
            route=state["route"],
            intent=state["intent"].intent,
            grounded=state.get("grounded"),
            sources=len(sources),
            searched_chunks=state.get("searched_chunks", 0),
        ),
    )

    usage = state.get("usage")
    return WorkflowResponse(
        answer=state["answer"],
        route=state["route"],
        intent=state["intent"],
        grounded=state.get("grounded"),
        sources=[SourceCitation.from_match(index, match) for index, match in enumerate(sources, 1)],
        searched_chunks=state.get("searched_chunks", 0),
        provider=settings.llm_provider,
        model=settings.openai_model,
        usage=TokenUsage(**usage.__dict__) if usage else None,
        request_id=request_id_ctx.get(),
    )
