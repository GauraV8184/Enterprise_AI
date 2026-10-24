"""The data analyst agent: question -> SQL -> rows -> grounded answer.

The loop is deliberately small:

1. the model proposes one query (structured output, so it is typed),
2. the guard and the database either accept it or produce a concrete complaint,
3. the first complaint is handed straight back to the model for one repair
   attempt - a repair driven by the real error, not by "try again",
4. only the rows that actually came back are used to write the answer.

If both attempts fail the error is raised. A request is never answered with
"the data probably shows ...".
"""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel
from starlette.concurrency import run_in_threadpool

from app.core.errors import AppError
from app.core.logging import get_logger, safe_extra
from app.database.errors import AnalyticsQueryRejectedError
from app.database.repositories import DocumentStore
from app.llm.prompts import (
    ANALYST_SYSTEM_PROMPT,
    SQL_GENERATION_SYSTEM_PROMPT,
    build_analyst_prompt,
    build_sql_generation_prompt,
)
from app.llm.schemas import AnalyticsQuery
from app.llm.service import TokenUsage, generate_answer, structured_completion
from app.tools.analytics import (
    DEMO_SCHEMA_DESCRIPTION,
    AnalyticsResult,
    render_rows,
    run_analytics_query,
    strip_unrequested_limit,
)

logger = get_logger(__name__)

MAX_QUERY_ATTEMPTS = 2

_REPAIRABLE_CODES = frozenset({"analytics_query_rejected", "analytics_query_failed"})


@dataclass(frozen=True)
class AnalyticsAnswer:
    """The analyst's answer plus the query and rows behind it."""

    answer: str
    analytics: AnalyticsResult
    grounded: bool
    """False when the query ran but matched no rows; then ``answer`` states that
    and the model was not asked to summarise an empty result."""
    usage: TokenUsage | None = None


async def generate_query(
    model: BaseChatModel,
    *,
    question: str,
    max_rows: int,
    feedback: str | None = None,
) -> AnalyticsQuery:
    """Ask the model for one query, optionally with repair feedback."""
    prompt = build_sql_generation_prompt(
        question=question,
        schema=DEMO_SCHEMA_DESCRIPTION,
        max_rows=max_rows,
        feedback=feedback,
    )
    return await structured_completion(
        model,
        AnalyticsQuery,
        prompt=prompt,
        system_prompt=SQL_GENERATION_SYSTEM_PROMPT,
    )


async def generate_and_run_query(
    store: DocumentStore,
    model: BaseChatModel,
    *,
    question: str,
    max_rows: int,
) -> AnalyticsResult:
    """Generate a query, run it, and repair once if it is refused.

    Raises:
        AppError: the guard, the database, or the provider refused both
            attempts. Only rejections and execution failures are retried;
            anything else (a store outage, a provider failure) propagates
            immediately, because a second model call cannot fix it.
    """
    feedback: str | None = None
    last_error: AppError = AnalyticsQueryRejectedError("No query attempt was made.")

    for attempt in range(1, MAX_QUERY_ATTEMPTS + 1):
        query = await generate_query(model, question=question, max_rows=max_rows, feedback=feedback)
        statement = strip_unrequested_limit(query.sql, question)
        if statement != query.sql:
            logger.info(
                "analytics_limit_removed",
                extra=safe_extra(attempt=attempt, question_chars=len(question)),
            )
        try:
            result = await run_in_threadpool(
                run_analytics_query, store, sql=statement, max_rows=max_rows
            )
        except AppError as exc:
            if exc.code not in _REPAIRABLE_CODES:
                raise
            last_error = exc
            feedback = getattr(exc, "repair_hint", None) or exc.message
            logger.warning(
                "analytics_query_repair",
                extra=safe_extra(attempt=attempt, error_code=exc.code),
            )
            continue

        logger.info(
            "analytics_query_accepted",
            extra=safe_extra(
                attempt=attempt,
                rows=result.row_count,
                truncated=result.truncated,
                reasoning=query.reasoning[:200],
            ),
        )
        return result

    raise last_error


async def answer_with_data(
    store: DocumentStore,
    model: BaseChatModel,
    *,
    question: str,
    max_rows: int,
) -> AnalyticsAnswer:
    """Answer a question from one query against the demo dataset.

    Raises:
        AppError: generation, validation, execution, or provider failures. A
            query that matches nothing is not an error - it returns
            ``grounded=False`` with an explanatory answer and no model call.
    """
    result = await generate_and_run_query(store, model, question=question, max_rows=max_rows)

    if not result.rows:
        return AnalyticsAnswer(
            answer=(
                "The query ran but returned no rows for that question. Nothing has been "
                "guessed - the filters may not match this dataset, so try a broader or "
                "differently worded question."
            ),
            analytics=result,
            grounded=False,
        )

    rows_text, shown = render_rows(result.rows)
    prompt = build_analyst_prompt(
        question=question,
        sql=result.sql,
        rows=rows_text,
        rows_shown=shown,
        omitted=result.truncated or shown < result.row_count,
    )
    completion = await generate_answer(
        model,
        history=[{"role": "user", "content": prompt}],
        system_prompt=ANALYST_SYSTEM_PROMPT,
    )

    logger.info(
        "analytics_answered",
        extra=safe_extra(rows=result.row_count, rows_shown=shown),
    )
    return AnalyticsAnswer(
        answer=completion.text,
        analytics=result,
        grounded=True,
        usage=completion.usage,
    )
