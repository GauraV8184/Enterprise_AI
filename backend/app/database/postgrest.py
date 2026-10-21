"""Minimal PostgREST transport bound to one caller's access token.

**Why not ``supabase-py``.** Its client keeps the session on the instance, so a
process-wide client plus ``.auth(token)`` per request would let two concurrent
requests act as each other: request A sets the token, request B overwrites it, and
A's query then runs as B. Here the token is an immutable constructor argument and
a fresh instance is built per request, so there is no shared mutable state to race
on.

**Why PostgREST at all.** It is the only path the application takes to the
database, and every call carries the caller's own JWT. The service-role key is
never used here, which means every query is subject to Row Level Security: the
database, not this module, is what keeps tenants apart.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from app.core.logging import get_logger, safe_extra
from app.database.errors import (
    AnalyticsQueryFailedError,
    AnalyticsQueryRejectedError,
    DocumentStoreError,
    DocumentStoreNotReadyError,
    DocumentStoreUnavailableError,
)
from app.security.errors import AccessDeniedError, InvalidTokenError

logger = get_logger(__name__)

_SCHEMA_MISSING_CODES = frozenset({"PGRST202", "PGRST205"})

_ANALYTICS_RPC_PATH = "/rpc/run_demo_query"

_STATEMENT_TIMEOUT_SQLSTATE = "57014"

_DIMENSION_PATTERNS = (
    re.compile(r"expected (\d+) dimensions?,?\s+not (\d+)", re.IGNORECASE),
    re.compile(r"different vector dimensions (\d+) and (\d+)", re.IGNORECASE),
)


class VectorDimensionError(Exception):
    """Postgres rejected a vector because it has the wrong number of dimensions.

    Internal to the store layer. The transport cannot build the user-facing
    error on its own because the message alone does not say which side is the
    configured model; the repository knows the query width and translates this.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def dimensions(self) -> tuple[int, int] | None:
        """``(stored, supplied)`` when pgvector's message can be parsed."""
        for pattern in _DIMENSION_PATTERNS:
            match = pattern.search(self.message)
            if match:
                return int(match.group(1)), int(match.group(2))
        return None


class PostgrestClient:
    """A request-scoped view of one PostgREST endpoint.

    Instances are cheap and must not be shared between requests: the access token
    they carry identifies one caller.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        access_token: str,
        client: httpx.Client,
        timeout: float = 30.0,
    ) -> None:
        """``base_url`` is the REST endpoint, e.g. ``https://<ref>.supabase.co/rest/v1``.

        It is *not* the project URL: appending ``/rest/v1`` here as well would
        produce ``/rest/v1/rest/v1/...``, which PostgREST answers with a 404 whose
        message ("Invalid path specified in request URL") says nothing about why.
        """
        if not access_token:
            raise ValueError(
                "A caller access token is required. The anon key alone cannot satisfy "
                "Row Level Security, and sending it would silently return zero rows."
            )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._access_token = access_token
        self._client = client
        self._timeout = timeout

    def get(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return self._request("GET", path, params=params, headers=headers)

    def post(
        self,
        path: str,
        *,
        json: Any = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return self._request("POST", path, params=params, json=json, headers=headers)

    def patch(
        self,
        path: str,
        *,
        json: Any = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return self._request("PATCH", path, params=params, json=json, headers=headers)

    def delete(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return self._request("DELETE", path, params=params, headers=headers)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        merged: dict[str, str] = {
            "apikey": self._api_key,
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
        }
        if headers:
            merged.update(headers)

        url = f"{self._base_url}{path}"
        try:
            response = self._client.request(
                method,
                url,
                params=params,
                json=json,
                headers=merged,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "postgrest_unreachable",
                extra=safe_extra(method=method, path=path, error=type(exc).__name__),
            )
            raise DocumentStoreUnavailableError() from exc

        if response.status_code >= 400:
            self._raise_for_status(response, method=method, path=path)
        return response

    def _raise_for_status(self, response: httpx.Response, *, method: str, path: str) -> None:
        body = _json_body(response)
        message = str(body.get("message") or response.text[:400])
        pg_code = str(body.get("code") or "")

        logger.warning(
            "postgrest_error",
            extra=safe_extra(
                method=method,
                path=path,
                status=response.status_code,
                pg_code=pg_code,
                detail=message[:300],
            ),
        )

        if response.status_code == 401:
            raise InvalidTokenError()
        if response.status_code == 403:
            raise AccessDeniedError()
        if pg_code in _SCHEMA_MISSING_CODES:
            raise DocumentStoreNotReadyError()
        if path.startswith(_ANALYTICS_RPC_PATH):
            if pg_code == "P0001":
                raise AnalyticsQueryRejectedError(message)
            if response.status_code == 400 or pg_code == _STATEMENT_TIMEOUT_SQLSTATE:
                raise AnalyticsQueryFailedError(repair_hint=message)
        if any(pattern.search(message) for pattern in _DIMENSION_PATTERNS):
            raise VectorDimensionError(message)
        if response.status_code >= 500:
            raise DocumentStoreUnavailableError()
        raise DocumentStoreError()


def _json_body(response: httpx.Response) -> dict[str, Any]:
    """PostgREST error bodies are JSON objects; anything else is ignored."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
