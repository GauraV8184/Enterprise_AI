"""The Supabase Storage transport, without a network.

``MockTransport`` is enough because the interesting behaviour is the request the
client builds and the error it chooses: URL shape, percent-encoding, the caller's
token, and the difference between "denied" and "gone".
"""

from __future__ import annotations

import httpx
import pytest

from app.security.errors import AccessDeniedError, InvalidTokenError
from app.storage.errors import (
    FileStoreError,
    FileStoreNotReadyError,
    FileStoreObjectMissingError,
    FileStoreUnavailableError,
)
from app.storage.supabase import SupabaseStorage

OWNER = "8f14e45fceea167a5a36dedd4bea2543"
DOCUMENT = "3c9a7b21f0e14f4b9c0d0e9f1a2b3c4d"
BASE = "https://example.supabase.co/storage/v1"
TOKEN = "user-access-token"
ANON = "public-anon-key"


def build(handler, *, token: str = TOKEN, bucket: str = "documents") -> SupabaseStorage:
    return SupabaseStorage(
        base_url=BASE,
        api_key=ANON,
        access_token=token,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        bucket=bucket,
    )


def test_upload_posts_under_the_owner_document_key() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.content
        seen["authorization"] = request.headers["authorization"]
        seen["apikey"] = request.headers["apikey"]
        seen["content_type"] = request.headers["content-type"]
        seen["upsert"] = request.headers["x-upsert"]
        return httpx.Response(200, json={"Key": "documents/ignored", "Id": "ignored"})

    key = build(handler).put(
        owner_id=OWNER,
        document_id=DOCUMENT,
        filename="notes.txt",
        data=b"hello",
        content_type="text/plain",
    )

    assert key == f"{OWNER}/{DOCUMENT}/notes.txt"
    assert seen["method"] == "POST"
    assert seen["url"] == f"{BASE}/object/documents/{OWNER}/{DOCUMENT}/notes.txt"
    assert seen["body"] == b"hello"
    # The caller's JWT, not the service-role key: this is what makes the
    # bucket's policies run at all.
    assert seen["authorization"] == f"Bearer {TOKEN}"
    assert seen["apikey"] == ANON
    assert seen["content_type"] == "text/plain"
    assert seen["upsert"] == "true"


def test_a_hostile_filename_is_sanitised_before_it_reaches_the_url() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={})

    key = build(handler).put(
        owner_id=OWNER,
        document_id=DOCUMENT,
        filename="Q3 report #1?.txt",
        data=b"x",
        content_type="text/plain",
    )

    assert key == f"{OWNER}/{DOCUMENT}/Q3-report-1.txt"
    assert seen["url"] == f"{BASE}/object/documents/{OWNER}/{DOCUMENT}/Q3-report-1.txt"


def test_object_path_percent_encodes_anything_that_reaches_it() -> None:
    """Second line of defence for a key that somehow gained a URL metacharacter.

    ``#`` would otherwise start a fragment and ``?`` a query string, so both the
    bucket the server sees and the key would be silently truncated.
    """
    store = build(lambda request: httpx.Response(200))

    assert store._object_path("a b/c#d?.txt") == "/object/documents/a%20b/c%23d%3F.txt"


def test_read_returns_the_bytes() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["accept"] = request.headers["accept"]
        return httpx.Response(200, content=b"stored bytes")

    result = build(handler).read(owner_id=OWNER, document_id=DOCUMENT, filename="a.txt")

    assert result == b"stored bytes"
    assert seen["method"] == "GET"
    assert seen["accept"] == "*/*"


def test_delete_treats_an_absent_object_as_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"statusCode": "404", "error": "not_found", "message": "Object not found"},
        )

    build(handler).delete(owner_id=OWNER, document_id=DOCUMENT, filename="a.txt")


def test_delete_does_not_swallow_a_denial() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"statusCode": "403", "error": "Unauthorized", "message": "denied"},
        )

    with pytest.raises(AccessDeniedError):
        build(handler).delete(owner_id=OWNER, document_id=DOCUMENT, filename="a.txt")


def test_reading_a_missing_object_is_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"statusCode": "404", "error": "not_found", "message": "Object not found"},
        )

    with pytest.raises(FileStoreObjectMissingError):
        build(handler).read(owner_id=OWNER, document_id=DOCUMENT, filename="a.txt")


def test_an_expired_token_is_an_authentication_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "JWT expired"})

    with pytest.raises(InvalidTokenError):
        build(handler).read(owner_id=OWNER, document_id=DOCUMENT, filename="a.txt")


def test_a_foreign_key_is_an_access_denial() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "new row violates row-level security policy"})

    with pytest.raises(AccessDeniedError):
        build(handler, token="another-users-token").read(
            owner_id=OWNER, document_id=DOCUMENT, filename="a.txt"
        )


def test_a_missing_bucket_is_not_ready_not_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"statusCode": "404", "error": "Bucket not found", "message": "Bucket not found"},
        )

    with pytest.raises(FileStoreNotReadyError):
        build(handler).put(
            owner_id=OWNER,
            document_id=DOCUMENT,
            filename="a.txt",
            data=b"x",
            content_type="text/plain",
        )


def test_a_row_level_security_rejection_is_an_access_denial() -> None:
    """Storage reports policy violations as 400, not 403."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "new row violates row-level security policy"})

    with pytest.raises(AccessDeniedError):
        build(handler).put(
            owner_id=OWNER,
            document_id=DOCUMENT,
            filename="a.txt",
            data=b"x",
            content_type="text/plain",
        )


def test_an_access_denied_message_is_an_access_denial() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "Access denied"})

    with pytest.raises(AccessDeniedError):
        build(handler).delete(owner_id=OWNER, document_id=DOCUMENT, filename="a.txt")


def test_a_server_error_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"message": "storage-api is restarting"})

    with pytest.raises(FileStoreUnavailableError):
        build(handler).read(owner_id=OWNER, document_id=DOCUMENT, filename="a.txt")


def test_a_connection_failure_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(FileStoreUnavailableError):
        build(handler).read(owner_id=OWNER, document_id=DOCUMENT, filename="a.txt")


def test_an_unexpected_rejection_is_a_generic_store_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "The resource already exists"})

    with pytest.raises(FileStoreError):
        build(handler).put(
            owner_id=OWNER,
            document_id=DOCUMENT,
            filename="a.txt",
            data=b"x",
            content_type="text/plain",
        )


def test_an_empty_access_token_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="access token"):
        SupabaseStorage(
            base_url=BASE,
            api_key=ANON,
            access_token="",
            client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
            bucket="documents",
        )
