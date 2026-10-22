"""Chat and LLM endpoints.

Phase 2 scope: prove the LLM integration works end to end with real error
handling. Conversation persistence arrives in Phase 4 (Supabase), retrieval
grounding in Phase 3, and agent routing in Phase 5.

Nothing here fakes a model call. If the provider is not configured, the request
fails with 503 and a message explaining exactly which setting is missing.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import ChatModelDep, SettingsDep
from app.core.logging import get_logger, request_id_ctx, safe_extra
from app.llm.service import classify_intent, generate_answer
from app.schemas.chat import (
    ChatRequest,
    ChatResponse,
    IntentRequest,
    IntentResponse,
    LlmStatusResponse,
    TokenUsage,
)
from app.security.identity import CurrentUserDep
from app.security.rate_limit import model_rate_limit, standard_rate_limit

logger = get_logger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


@router.get(
    "/status",
    response_model=LlmStatusResponse,
    summary="LLM configuration status",
    dependencies=[Depends(standard_rate_limit)],
)
def llm_status(settings: SettingsDep, user: CurrentUserDep) -> LlmStatusResponse:
    """Report whether the LLM is configured, without exposing any secret.

    Authenticated like the generation endpoints: provider, model and timeout are
    deployment details, and ``/health`` is the anonymous liveness probe.
    """
    configured = settings.llm_configured
    return LlmStatusResponse(
        configured=configured,
        provider=settings.llm_provider,
        model=settings.openai_model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        detail=None if configured else "Set OPENAI_API_KEY in .env, then restart the server.",
    )


@router.post(
    "",
    response_model=ChatResponse,
    summary="Send a chat completion request",
    dependencies=[Depends(model_rate_limit)],
)
async def chat(
    payload: ChatRequest,
    model: ChatModelDep,
    settings: SettingsDep,
    user: CurrentUserDep,
) -> ChatResponse:
    """Send the conversation to the configured model and return its reply.

    Authentication is required even though no tenant data is read: without it an
    anonymous caller can spend the deployment's provider quota. The identity is
    not otherwise used - chat history is not persisted here.
    """
    answer = await generate_answer(
        model,
        history=[message.model_dump() for message in payload.messages],
        temperature=payload.temperature,
    )

    logger.info(
        "chat_completed",
        extra=safe_extra(
            model=settings.openai_model,
            message_count=len(payload.messages),
            output_chars=len(answer.text),
            total_tokens=answer.usage.total_tokens if answer.usage else None,
        ),
    )

    return ChatResponse(
        answer=answer.text,
        provider=settings.llm_provider,
        model=answer.model_name or settings.openai_model,
        usage=TokenUsage(**answer.usage.__dict__) if answer.usage else None,
        finish_reason=answer.finish_reason,
        request_id=request_id_ctx.get(),
    )


@router.post(
    "/intent",
    response_model=IntentResponse,
    summary="Classify a request (structured output)",
    dependencies=[Depends(model_rate_limit)],
)
async def intent(
    payload: IntentRequest,
    model: ChatModelDep,
    settings: SettingsDep,
    user: CurrentUserDep,
) -> IntentResponse:
    """Classify a request into a typed :class:`TaskIntent`.

    Demonstrates structured LLM output, and is the routing input the LangGraph
    supervisor will consume in Phase 5.
    """
    result = await classify_intent(model, text=payload.text)

    logger.info(
        "intent_classified",
        extra=safe_extra(intent=result.intent, complexity=result.complexity),
    )

    return IntentResponse(
        intent=result,
        provider=settings.llm_provider,
        model=settings.openai_model,
        request_id=request_id_ctx.get(),
    )
