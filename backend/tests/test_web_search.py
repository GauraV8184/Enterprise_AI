"""The web search tool: request shape, response mapping, and failure modes.

Tavily is faked at the HTTP layer with ``httpx.MockTransport``, so the real
client code (the one production runs) handles every response in these tests.
No network call is made, and the API key used is a fake string.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.tools.web_search import (
    MAX_RESULTS_HARD_LIMIT,
    TAVILY_SEARCH_URL,
    TavilySearch,
    WebResult,
    WebSearchFailedError,
    build_web_search,
)

TEST_KEY = "tvly-test-key-not-real"


def make_search(handler: httpx.MockTransport) -> tuple[TavilySearch, httpx.Client]:
    client = httpx.Client(transport=handler)
    return TavilySearch(api_key=TEST_KEY, timeout=5.0, client=client), client


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "environment": "test",
        "log_json": False,
        "log_level": "CRITICAL",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


async def test_search_maps_provider_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == TAVILY_SEARCH_URL
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "SMB laptop market 2025",
                        "url": "https://example.com/report",
                        "content": "Spending grew 12% in 2025.",
                        "score": 0.91,
                    },
                    {"title": "", "url": "https://example.com/no-title", "content": ""},
                ]
            },
        )

    search, client = make_search(httpx.MockTransport(handler))
    try:
        results = await search.search("smb laptop market", max_results=3)
    finally:
        client.close()

    assert [result.url for result in results] == [
        "https://example.com/report",
        "https://example.com/no-title",
    ]
    assert results[0].title == "SMB laptop market 2025"
    assert results[0].score == 0.91
    assert results[1].title == "https://example.com/no-title"
    assert results[1].score is None


async def test_search_sends_the_expected_request_body() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"results": []})

    search, client = make_search(httpx.MockTransport(handler))
    try:
        await search.search("  quarterly revenue trend  ", max_results=99)
    finally:
        client.close()

    assert captured[0]["query"] == "quarterly revenue trend"
    assert captured[0]["max_results"] == MAX_RESULTS_HARD_LIMIT
    assert captured[0]["search_depth"] == "basic"
    assert captured[0]["api_key"] == TEST_KEY


async def test_search_drops_results_without_a_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [{"title": "x", "content": "y"}, {"title": "z", "url": "  "}]},
        )

    search, client = make_search(httpx.MockTransport(handler))
    try:
        results = await search.search("anything", max_results=5)
    finally:
        client.close()

    assert results == []


async def test_search_surfaces_provider_failures_as_502() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": {"error": "rate limited"}})

    search, client = make_search(httpx.MockTransport(handler))
    try:
        with pytest.raises(WebSearchFailedError) as caught:
            await search.search("anything", max_results=5)
    finally:
        client.close()

    assert caught.value.status_code == 502
    assert caught.value.code == "web_search_failed"


async def test_search_rejects_a_non_json_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    search, client = make_search(httpx.MockTransport(handler))
    try:
        with pytest.raises(WebSearchFailedError):
            await search.search("anything", max_results=5)
    finally:
        client.close()


async def test_search_maps_a_transport_error_to_502() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    search, client = make_search(httpx.MockTransport(handler))
    try:
        with pytest.raises(WebSearchFailedError):
            await search.search("anything", max_results=5)
    finally:
        client.close()


async def test_search_rejects_a_blank_query_without_calling_the_provider() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"results": []})

    search, client = make_search(httpx.MockTransport(handler))
    try:
        with pytest.raises(WebSearchFailedError):
            await search.search("   ", max_results=5)
    finally:
        client.close()

    assert calls == 0


def test_snippet_collapses_whitespace_and_truncates() -> None:
    words = " ".join(f"word{index}" for index in range(200))
    result = WebResult(title="t", url="https://example.com", content=f"a\n\n b\tc {words}")

    assert result.snippet.startswith("a b c word0 word1")
    assert result.snippet.endswith("...")
    assert len(result.snippet) <= 403


def test_build_web_search_returns_none_without_a_key() -> None:
    assert build_web_search(make_settings()) is None


def test_build_web_search_builds_a_provider_that_owns_its_client() -> None:
    search = build_web_search(make_settings(tavily_api_key=TEST_KEY))

    assert isinstance(search, TavilySearch)
    search.close()
