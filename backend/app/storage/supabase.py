"""Supabase Storage transport bound to one caller's access token.

The same two decisions as :mod:`app.database.postgrest`, for the same reasons:

* **A request-scoped object.** ``supabase-py`` keeps the session on the client,
  so a process-wide instance plus ``.auth(token)`` per request lets concurrent
  requests act as each other. Here the token is an immutable constructor
  argument and the object is rebuilt per request.
* **The caller's token, never the service-role key.** The bucket is private and
  its policies compare the first key segment to ``auth.uid()``. With the
  service-role key those policies would still exist but would never run, leaving
  ownership enforcement to this application's code alone.

Uploads use ``x-upsert: true``. The key contains a server-generated document id,
so a retry can only overwrite the same document's own object - never another
user's file - and making retries idempotent is worth more than the stricter
``false`` that would turn a network retry into a 409.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from app.core.logging import get_logger, safe_extra
from app.security.errors import AccessDeniedError, InvalidTokenError
from app.storage.base import object_key
from app.storage.errors import (
    FileStoreError,
    FileStoreNotReadyError,
    FileStoreObjectMissingError,
    FileStoreUnavailableError,
)

logger = get_logger(__name__)


class SupabaseStorage:
    """One caller's view of one private bucket."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        access_token: str,
        client: httpx.Client,
        bucket: str,
        timeout: float = 120.0,
    ) -> None:
        """``base_url`` is the Storage endpoint, e.g. ``https://<ref>.supabase.co/storage/v1``.

        Not the project URL: appending ``/storage/v1`` here as well would produce
        the same doubled path that PostgREST answers with a bare 404.
        """
        if not access_token:
            raise ValueError(
                "A caller access token is required. The anon key alone cannot satisfy "
                "the storage policies, and uploading with it would be rejected."
            )
        if not bucket:
            raise ValueError("A bucket name is required.")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._access_token = access_token
        self._client = client
        self._bucket = bucket
        self._timeout = timeout

    # -- contract ----------------------------------------------------------

    def put(
        self,
        *,
        owner_id: str,
        document_id: str,
        filename: str,
        data: bytes,
        content_type: str,
    ) -> str:
        key = object_key(owner_id=owner_id, document_id=document_id, filename=filename)
        self._request(
            "POST",
            self._object_path(key),
            content=data,
            headers={
                "Content-Type": content_type,
                "x-upsert": "true",
            },
        )
        return key

    def read(self, *, owner_id: str, document_id: str, filename: str) -> bytes:
        key = object_key(owner_id=owner_id, document_id=document_id, filename=filename)
        response = self._request("GET", self._object_path(key), headers={"Accept": "*/*"})
        return response.content

    def delete(self, *, owner_id: str, document_id: str, filename: str) -> None:
        key = object_key(owner_id=owner_id, document_id=document_id, filename=filename)
        try:
            self._request("DELETE", self._object_path(key))
        except FileStoreObjectMissingError:
            # Idempotent by contract: the object the caller wanted gone is gone.
            logger.info("storage_object_already_absent", extra=safe_extra(document_id=document_id))

    # -- plumbing ----------------------------------------------------------

    def _object_path(self, key: str) -> str:
        # Each segment is percent-encoded; "/" stays because it separates the
        # owner, document and filename segments. Without this, a filename such as
        # "q3 #1?.pdf" would truncate the path at the fragment/query marker.
        return f"/object/{quote(self._bucket, safe='')}/{quote(key, safe='/')}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        content: bytes | None = None,
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
                content=content,
                headers=merged,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "storage_unreachable",
                extra=safe_extra(method=method, error=type(exc).__name__),
            )
            raise FileStoreUnavailableError() from exc

        if response.status_code >= 400:
            self._raise_for_status(response, method=method)
        return response

    def _raise_for_status(self, response: httpx.Response, *, method: str) -> None:
        body = _json_body(response)
        message = str(body.get("message") or body.get("error") or response.text[:400])
        lowered = message.lower()

        logger.warning(
            "storage_error",
            extra=safe_extra(
                method=method,
                status=response.status_code,
                detail=message[:300],
            ),
        )

        # Verified locally before the call, so a 401 here means the token was
        # revoked or expired in between; either way the caller needs a new one.
        if response.status_code == 401:
            raise InvalidTokenError()
        if response.status_code == 403:
            # The policies refused the key. The database is doing its job.
            raise AccessDeniedError()
        if "bucket not found" in lowered:
            raise FileStoreNotReadyError()
        # Unlike PostgREST, Storage reports policy violations as 400, with one of
        # these messages. Treating them as a server error would turn a correct
        # refusal into a 500 and hide the fact that isolation is working.
        if (
            "row-level security" in lowered
            or lowered == "access denied"
            or "unauthorized" in lowered
        ):
            raise AccessDeniedError()
        if response.status_code == 404 or "not found" in lowered:
            raise FileStoreObjectMissingError()
        if response.status_code >= 500:
            raise FileStoreUnavailableError()
        raise FileStoreError()


def _json_body(response: httpx.Response) -> dict[str, Any]:
    """Storage error bodies are JSON objects; anything else is ignored."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
