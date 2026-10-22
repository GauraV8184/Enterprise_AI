"""Web search, behind a provider-agnostic interface.

**Why an interface.** The research agent needs *a* search tool, not *Tavily*.
Tests implement :class:`WebSearch` with a fake and exercise the real agent code;
``build_web_search`` returns ``None`` when no key is configured, so the rest of
the platform keeps working without one.

**Why not a LangChain package.** A Tavily client is one POST request. Keeping it
here means one fewer dependency to track, and the request/response handling
(including the failure modes) is reviewable in this module.

**The key never leaves the process.** It is sent in the request body as Tavily's
API requires, and it is never logged or echoed in an error message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger, safe_extra

logger = get_logger(__name__)

TAVILY_SEARCH_URL = "https://api.tavily.com/search"

MAX_RESULTS_HARD_LIMIT = 10

SNIPPET_CHARS = 400


@dataclass(frozen=True)
class WebResult:
    """One search hit. ``content`` is a provider-extracted excerpt, not the page."""

    title: str
    url: str
    content: str
    score: float | None = None

    @property
    def snippet(self) -> str:
        text = " ".join(self.content.split())
        if len(text) <= SNIPPET_CHARS:
            return text
        return text[:SNIPPET_CHARS].rsplit(" ", 1)[0] + "..."


class WebSearchNotConfiguredError(AppError):
    """No search provider is configured. Surfaced as 503, never faked.

    A chat or documents request must keep working without a search key, which is
    why this is raised where a search is actually attempted rather than at
    startup.
    """

    status_code = 503
    code = "web_search_not_configured"
    public_message = (
        "Web search is not configured. Set TAVILY_API_KEY in the repository-root .env "
        "file (see .env.example, 'Tools'). Verify with: python scripts/check_web_search.py"
    )


class WebSearchFailedError(AppError):
    """The provider could not be reached or returned an unusable response."""

    status_code = 502
    code = "web_search_failed"
    public_message = "The web search provider request failed. Try again shortly."


class WebSearch(Protocol):
    """Contract the research agent depends on."""

    async def search(self, query: str, *, max_results: int) -> list[WebResult]: ...


class TavilySearch:
    """Tavily-backed search over a shared HTTP client.

    ``search`` is async but the HTTP call is synchronous: it runs in a worker
    thread, so a slow provider never blocks the event loop and the retry and
    timeout semantics stay those of a plain ``httpx`` request.
    """

    def __init__(
        self,
        *,
        api_key: str,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout, connect=10.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    def close(self) -> None:
        """Close the HTTP client, unless the caller supplied their own."""
        if self._owns_client:
            self._client.close()

    async def search(self, query: str, *, max_results: int) -> list[WebResult]:
        return await run_in_threadpool(self._search, query, max_results)

    def _search(self, query: str, max_results: int) -> list[WebResult]:
        cleaned = query.strip()
        if not cleaned:
            raise WebSearchFailedError("The search query must not be blank.")

        payload: dict[str, Any] = {
            "api_key": self._api_key,
            "query": cleaned,
            "max_results": max(1, min(int(max_results), MAX_RESULTS_HARD_LIMIT)),
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
        }

        try:
            response = self._client.post(TAVILY_SEARCH_URL, json=payload, timeout=self._timeout)
        except httpx.HTTPError as exc:
            logger.warning(
                "web_search_unreachable",
                extra=safe_extra(error=type(exc).__name__),
            )
            raise WebSearchFailedError("The web search provider could not be reached.") from exc

        if response.status_code >= 400:
            logger.warning(
                "web_search_rejected",
                extra=safe_extra(status=response.status_code),
            )
            raise WebSearchFailedError()

        try:
            body = response.json()
        except ValueError:
            raise WebSearchFailedError("The provider returned a non-JSON response.") from None

        raw_results = body.get("results") if isinstance(body, dict) else None
        results = [self._to_result(item) for item in raw_results or [] if isinstance(item, dict)]
        parsed = [result for result in results if result is not None]

        logger.info(
            "web_search_completed",
            extra=safe_extra(results=len(parsed), max_results=max_results),
        )
        return parsed

    @staticmethod
    def _to_result(item: dict[str, Any]) -> WebResult | None:
        """Map one provider result; drop anything without a URL.

        A result with no URL cannot be cited, and an uncitable result is worse
        than a missing one - the model would have to describe it as if it were
        verifiable.
        """
        url = str(item.get("url") or "").strip()
        if not url:
            return None
        title = str(item.get("title") or "").strip() or url
        score = item.get("score")
        return WebResult(
            title=title,
            url=url,
            content=str(item.get("content") or "").strip(),
            score=float(score) if isinstance(score, (int, float)) else None,
        )


def build_web_search(settings: Settings) -> WebSearch | None:
    """Build the configured search provider, or ``None`` when no key exists.

    Returning ``None`` rather than raising keeps this a startup-safe call: the
    lifespan attaches whatever it gets, and only a route that actually needs to
    search turns the absence into a typed 503.
    """
    if settings.tavily_api_key is None:
        return None

    logger.info(
        "web_search_client_built",
        extra=safe_extra(
            provider="tavily",
            timeout_seconds=settings.web_search_timeout_seconds,
        ),
    )
    return TavilySearch(
        api_key=settings.tavily_api_key.get_secret_value(),
        timeout=settings.web_search_timeout_seconds,
    )
