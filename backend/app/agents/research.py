"""The research agent: plan searches -> run them -> synthesise with citations.

Two model calls at most, and the second never happens without evidence: a
request whose searches return nothing is answered with a statement about the
searches, not by asking a model to fill the silence.

Citations use ``[W1]``-style labels. They are deliberately distinct from the
documents route's ``[1]`` labels so a mixed-evidence report cannot confuse a
document passage with a web result.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from langchain_core.language_models import BaseChatModel

from app.core.logging import get_logger, safe_extra
from app.llm.prompts import (
    RESEARCH_SYSTEM_PROMPT,
    SEARCH_PLAN_SYSTEM_PROMPT,
    build_research_prompt,
)
from app.llm.schemas import SearchPlan
from app.llm.service import TokenUsage, generate_answer, structured_completion
from app.tools.web_search import WebResult, WebSearch

logger = get_logger(__name__)

MAX_WEB_CONTEXT_CHARS = 8000

MAX_SEARCH_QUERIES = 3


@dataclass(frozen=True)
class WebGathering:
    """What the searches were and what came back."""

    queries: list[str] = field(default_factory=list)
    results: list[WebResult] = field(default_factory=list)


@dataclass(frozen=True)
class ResearchAnswer:
    """A synthesised answer plus the searches and sources behind it."""

    answer: str
    queries: list[str]
    results: list[WebResult]
    grounded: bool
    """False when the searches returned nothing; then ``answer`` states that and
    the model was not called."""
    usage: TokenUsage | None = None


def format_web_context(
    results: list[WebResult],
    *,
    max_chars: int = MAX_WEB_CONTEXT_CHARS,
) -> tuple[str, list[WebResult]]:
    """Render results as numbered ``[W1]`` blocks, dropping any that do not fit.

    Returns the text and the results that made it in, so the labels the model
    was given and the citations returned to the caller cannot disagree - the
    same invariant the documents route keeps.
    """
    blocks: list[str] = []
    included: list[WebResult] = []
    used = 0

    for result in results:
        label = f"W{len(included) + 1}"
        block = f"[{label}] {result.title}\nURL: {result.url}\n{result.content}"

        if used + len(block) > max_chars and included:
            break

        blocks.append(block)
        included.append(result)
        used += len(block)

    return "\n\n".join(blocks), included


async def plan_searches(model: BaseChatModel, *, topic: str) -> list[str]:
    """Ask the model which searches to run."""
    plan = await structured_completion(
        model,
        SearchPlan,
        prompt=f"Research request: {topic}",
        system_prompt=SEARCH_PLAN_SYSTEM_PROMPT,
    )
    queries = [query.strip() for query in plan.queries if query.strip()]
    return queries[:MAX_SEARCH_QUERIES]


async def gather_web_evidence(
    web_search: WebSearch,
    model: BaseChatModel,
    *,
    topic: str,
    max_results: int,
) -> WebGathering:
    """Plan searches, run them, and return deduplicated results.

    ``max_results`` bounds the merged result list, so three queries cannot
    multiply the prompt size: the first results win, later duplicates are
    dropped by URL.
    """
    queries = await plan_searches(model, topic=topic)
    results: list[WebResult] = []
    seen: set[str] = set()

    for query in queries:
        for result in await web_search.search(query, max_results=max_results):
            if result.url in seen:
                continue
            seen.add(result.url)
            results.append(result)
            if len(results) >= max_results:
                break
        if len(results) >= max_results:
            break

    logger.info(
        "web_evidence_gathered",
        extra=safe_extra(queries=len(queries), results=len(results)),
    )
    return WebGathering(queries=queries, results=results)


async def answer_with_research(
    web_search: WebSearch,
    model: BaseChatModel,
    *,
    question: str,
    max_results: int,
) -> ResearchAnswer:
    """Answer a question from live web search results.

    Raises:
        AppError: provider failures (search or model). An empty search outcome
            is not an error - it returns ``grounded=False`` with an explanatory
            answer and no model call.
    """
    gathering = await gather_web_evidence(
        web_search, model, topic=question, max_results=max_results
    )

    if not gathering.results:
        return ResearchAnswer(
            answer=(
                "Web search returned no results for that request, so there is nothing to "
                "report from the web. Nothing has been guessed - try rephrasing, or check "
                "the search provider's status."
            ),
            queries=gathering.queries,
            results=[],
            grounded=False,
        )

    context, included = format_web_context(gathering.results)
    completion = await generate_answer(
        model,
        history=[
            {"role": "user", "content": build_research_prompt(question=question, context=context)}
        ],
        system_prompt=RESEARCH_SYSTEM_PROMPT,
    )

    logger.info(
        "research_answered",
        extra=safe_extra(results=len(included), queries=len(gathering.queries)),
    )
    return ResearchAnswer(
        answer=completion.text,
        queries=gathering.queries,
        results=included,
        grounded=True,
        usage=completion.usage,
    )
