"""Diagnostic: prove that live web search actually works.

The research route and web-sourced reports call a real search provider. This
script calls it directly, because a key that exists but is invalid, revoked, or
out of quota fails at request time - and from inside the application that looks
exactly like a network problem.

Without TAVILY_API_KEY the script exits 1 and says so. The research route
answers a typed 503 in that state, and a check that "passed" without searching
anything would be a lie.

Run it from ``backend``:

    .\\.venv\\Scripts\\python.exe scripts\\check_web_search.py

Exit codes: 0 = a live search returned results, 1 = not configured or failed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.tools.web_search import (  # noqa: E402
    WebSearchFailedError,
    WebSearchNotConfiguredError,
    build_web_search,
)

QUERY = "What is the current UK statutory annual leave entitlement?"


def _line(label: str, value: object) -> None:
    print(f"  {label:<20} {value}")


async def main() -> int:
    settings = Settings()

    print("\nWeb search configuration")
    print("-" * 60)
    _line("provider", "tavily")
    _line("configured", settings.web_search_configured)
    _line("max results", settings.web_search_max_results)
    _line("timeout", f"{settings.web_search_timeout_seconds}s")

    search = build_web_search(settings)
    if search is None:
        print("\nRESULT: not configured - no TAVILY_API_KEY in .env.")
        print("  Get a key at https://app.tavily.com, put TAVILY_API_KEY=... in the")
        print("  repository-root .env file, and re-run this script.")
        print("  Until then the research route answers 503; nothing else is affected.")
        return 1

    try:
        print("\nLive search")
        print("-" * 60)
        print(f"  Q: {QUERY}")
        try:
            results = await search.search(
                QUERY, max_results=settings.web_search_max_results
            )
        except (WebSearchNotConfiguredError, WebSearchFailedError) as exc:
            print(f"\nRESULT: FAILED - {exc.message}")
            return 1

        _line("results", len(results))
        for index, result in enumerate(results, start=1):
            print(f"\n  W{index}: {result.title}")
            print(f"      {result.url}")
            print(f"      {result.snippet[:120]}")

        if not results:
            print("\nRESULT: FAILED - the provider answered with no results for a query")
            print("  that should match something. Check the key's plan and quota.")
            return 1

        print(f"\nRESULT: OK - {len(results)} results returned by a live search.")
        return 0
    finally:
        close = getattr(search, "close", None)
        if close is not None:
            close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
