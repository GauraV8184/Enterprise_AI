"""The Supabase store, exercised against a mock PostgREST.

No network and no database: ``httpx.MockTransport`` answers every call, so the
tests assert on the exact requests the store would send. That is where the bugs
worth catching live - a filter left out of a query, an id sent in the wrong
shape, an admin key where a user token belongs.

The live counterpart is ``scripts/check_rls.py``, which proves the *policies* are
right against the real project. These two are complementary: this file proves the
application asks for the right rows, that one proves Postgres refuses the wrong
ones.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import numpy as np
import pytest
from fastapi import FastAPI, Request

from app.api.deps import get_repository
from app.core.config import Settings
from app.database.errors import (
    DocumentStoreError,
    DocumentStoreNotReadyError,
    DocumentStoreUnavailableError,
    EmbeddingDimensionMismatchError,
)
from app.database.postgrest import PostgrestClient
from app.database.repositories import ChunkRecord, new_id
from app.database.supabase_repository import SupabaseDocumentRepository
from app.security.errors import AccessDeniedError, InvalidTokenError
from app.security.identity import UserIdentity

USER_ID = "9f8c1d2e-3b4a-4c5d-8e9f-0a1b2c3d4e5f"
DOC_ID = "11111111-1111-1111-1111-111111111111"
DOC_ID_HEX = DOC_ID.replace("-", "")
ANON_KEY = "anon-key-not-a-secret"
USER_TOKEN = "user-access-token"

Handler = Callable[[httpx.Request], httpx.Response]


def make_repository(handler: Handler, *, token: str = USER_TOKEN) -> SupabaseDocumentRepository:
    """Build a repository whose HTTP layer never leaves the process."""
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return SupabaseDocumentRepository(
        PostgrestClient(
            base_url="https://project.supabase.co/rest/v1",
            api_key=ANON_KEY,
            access_token=token,
            client=client,
        )
    )


def document_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": DOC_ID,
        "owner_id": USER_ID,
        "filename": "handbook.pdf",
        "content_type": "application/pdf",
        "extension": ".pdf",
        "size_bytes": 1234,
        "status": "pending",
        "error": None,
        "page_count": None,
        "chunk_count": 0,
        "created_at": "2026-10-20T05:27:00.123456+00:00",
        "indexed_at": None,
    }
    row.update(overrides)
    return row


def ok(request: httpx.Request, payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload, request=request)


def test_every_request_carries_the_caller_token_and_the_public_key() -> None:
    """The service-role key must never be reachable from here.

    ``apikey`` is the anon key, which identifies the project but grants nothing on
    its own; ``Authorization`` is what Postgres turns into ``auth.uid()``.
    """
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return ok(request, [])

    make_repository(handler).list_documents(owner_id=USER_ID)

    assert seen[0]["authorization"] == f"Bearer {USER_TOKEN}"
    assert seen[0]["apikey"] == ANON_KEY


def test_a_repository_cannot_be_built_without_a_caller_token() -> None:
    """Falling back to the anon key would return zero rows and look like an empty
    account, which is a far worse failure than refusing to start."""
    with pytest.raises(ValueError, match="access token"):
        PostgrestClient(
            base_url="https://project.supabase.co/rest/v1",
            api_key=ANON_KEY,
            access_token="",
            client=httpx.Client(transport=httpx.MockTransport(lambda request: ok(request, []))),
        )


def test_create_document_sends_a_canonical_uuid_and_returns_hex() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return ok(request, [{**document_row(), **captured}], status=201)

    record = make_repository(handler).create_document(
        owner_id=USER_ID,
        filename="handbook.pdf",
        content_type="application/pdf",
        extension=".pdf",
        size_bytes=1234,
    )

    sent_id = str(captured["id"])
    assert len(sent_id) == 36, "Postgres expects the dashed form"
    assert record.id == sent_id.replace("-", ""), "the API keeps using 32-char hex"
    assert record.owner_id == USER_ID
    assert record.status == "pending"


def test_get_document_queries_the_canonical_id_and_returns_hex() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["id"] == f"eq.{DOC_ID}"
        assert request.url.params["owner_id"] == f"eq.{USER_ID}"
        return ok(request, [document_row()])

    record = make_repository(handler).get_document(owner_id=USER_ID, document_id=DOC_ID_HEX)

    assert record is not None
    assert record.id == DOC_ID_HEX


def test_a_malformed_id_is_a_miss_not_a_database_error() -> None:
    """Sending junk to a ``uuid`` column produces a 400 about input syntax. The
    caller asked about a document that does not exist, and 404 is the honest
    answer - so the request is never made."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return ok(request, [])

    repository = make_repository(handler)

    assert repository.get_document(owner_id=USER_ID, document_id="not-a-uuid") is None
    assert repository.delete_document(owner_id=USER_ID, document_id="not-a-uuid") is False
    assert calls == []


def test_list_documents_is_owner_scoped_and_newest_first() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["owner_id"] == f"eq.{USER_ID}"
        assert request.url.params["order"] == "created_at.desc"
        return ok(request, [document_row(filename="newer.txt"), document_row(filename="older.txt")])

    records = make_repository(handler).list_documents(owner_id=USER_ID)

    assert [record.filename for record in records] == ["newer.txt", "older.txt"]


def test_delete_document_reports_whether_a_row_existed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        return ok(request, [document_row()])

    assert make_repository(handler).delete_document(owner_id=USER_ID, document_id=DOC_ID) is True

    def missing(request: httpx.Request) -> httpx.Response:
        return ok(request, [])

    assert make_repository(missing).delete_document(owner_id=USER_ID, document_id=DOC_ID) is False


def test_update_status_sends_only_what_changed_but_always_clears_the_error() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        assert request.method == "PATCH"
        return ok(request, [document_row(status="ready")])

    repository = make_repository(handler)
    repository.update_status(owner_id=USER_ID, document_id=DOC_ID, status="ready")

    assert bodies[0] == {"status": "ready", "error": None}
    assert "page_count" not in bodies[0], "unset fields must not be overwritten with null"

    repository.update_status(
        owner_id=USER_ID,
        document_id=DOC_ID,
        status="ready",
        chunk_count=12,
        mark_indexed=True,
    )
    assert bodies[1]["chunk_count"] == 12
    assert "indexed_at" in bodies[1]


def test_update_status_returns_false_when_the_row_does_not_exist() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return ok(request, [])

    repository = make_repository(handler)
    assert repository.update_status(owner_id=USER_ID, document_id=DOC_ID, status="ready") is False


def test_add_chunks_posts_one_array_with_pgvector_text() -> None:
    captured: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(201, request=request)

    chunk = ChunkRecord(
        id=new_id(),
        document_id=DOC_ID_HEX,
        owner_id=USER_ID,
        chunk_index=0,
        content="Employees accrue twenty days of leave.",
        page=4,
        section="Leave",
        embedding=np.array([0.5, -0.25], dtype=np.float32),
    )

    count = make_repository(handler).add_chunks([chunk])

    assert count == 1
    assert isinstance(captured[0], list), "one bulk insert, not one request per chunk"
    row = captured[0][0]  # type: ignore[index]
    assert row["embedding"] == "[0.5,-0.25]"
    assert row["document_id"] == DOC_ID
    assert row["page"] == 4


def test_count_chunks_reads_the_total_from_content_range() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["prefer"] == "count=exact"
        assert request.url.params["limit"] == "1"
        assert "documents.status" not in request.url.params
        return httpx.Response(200, json=[], headers={"Content-Range": "*/42"}, request=request)

    assert make_repository(handler).count_chunks(owner_id=USER_ID) == 42


def test_search_uses_the_rpc_and_translates_the_result() -> None:
    payloads: list[dict[str, object]] = []
    count_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rpc/match_document_chunks"):
            payloads.append(json.loads(request.content))
            return ok(
                request,
                [
                    {
                        "id": "22222222-2222-2222-2222-222222222222",
                        "document_id": DOC_ID,
                        "filename": "handbook.pdf",
                        "chunk_index": 3,
                        "content": "twenty days",
                        "page": 4,
                        "section": "Leave",
                        "similarity": 0.87,
                    }
                ],
            )
        count_requests.append(request)
        return httpx.Response(200, json=[], headers={"Content-Range": "*/7"}, request=request)

    result = make_repository(handler).search_chunks(
        owner_id=USER_ID,
        query_embedding=[0.1, 0.2, 0.3],
        top_k=5,
        document_ids=[DOC_ID_HEX],
        min_score=0.25,
    )

    payload = payloads[0]
    assert payload["query_embedding"] == "[0.1,0.2,0.3]"
    assert payload["match_count"] == 5
    assert payload["min_similarity"] == 0.25
    assert payload["filter_document_ids"] == [DOC_ID]

    count_params = count_requests[0].url.params
    assert count_params["documents.status"] == "eq.ready"
    assert count_params["select"] == "id,documents!inner(id)"

    match = result.matches[0]
    assert match.id == "22222222222222222222222222222222"
    assert match.document_id == DOC_ID_HEX
    assert match.score == 0.87
    assert match.page == 4
    assert result.searched_chunks == 7


def test_search_with_no_usable_document_ids_returns_nothing_without_a_query() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return ok(request, [])

    result = make_repository(handler).search_chunks(
        owner_id=USER_ID,
        query_embedding=[0.1, 0.2],
        top_k=5,
        document_ids=["not-a-uuid"],
    )

    assert result.matches == []
    assert result.searched_chunks == 0
    assert calls == []


def test_search_without_a_document_filter_sends_null() -> None:
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rpc/match_document_chunks"):
            payloads.append(json.loads(request.content))
            return ok(request, [])
        return httpx.Response(200, json=[], headers={"Content-Range": "*/0"}, request=request)

    make_repository(handler).search_chunks(owner_id=USER_ID, query_embedding=[0.1], top_k=3)

    assert payloads[0]["filter_document_ids"] is None


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, {"message": "JWT expired"}, InvalidTokenError),
        (403, {"message": "new row violates row-level security policy"}, AccessDeniedError),
        (
            404,
            {"code": "PGRST205", "message": "Could not find the table 'public.chunks'"},
            DocumentStoreNotReadyError,
        ),
        (503, {"message": "upstream connect error"}, DocumentStoreUnavailableError),
        (400, {"message": "unexpected parse failure"}, DocumentStoreError),
    ],
)
def test_postgrest_failures_become_client_safe_errors(
    status: int, body: dict[str, str], expected: type[Exception]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return ok(request, body, status=status)

    with pytest.raises(expected):
        make_repository(handler).list_documents(owner_id=USER_ID)


def test_a_transport_failure_is_a_503_not_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name resolution failed", request=request)

    with pytest.raises(DocumentStoreUnavailableError):
        make_repository(handler).list_documents(owner_id=USER_ID)


def test_a_vector_width_mismatch_becomes_a_409_with_both_numbers() -> None:
    """This is what a changed EMBEDDING_MODEL looks like from Postgres. The caller
    needs to know both widths to understand why re-uploading is required."""

    def handler(request: httpx.Request) -> httpx.Response:
        return ok(
            request,
            {"code": "22000", "message": "different vector dimensions 768 and 3"},
            status=400,
        )

    with pytest.raises(EmbeddingDimensionMismatchError) as excinfo:
        make_repository(handler).search_chunks(
            owner_id=USER_ID, query_embedding=[0.1, 0.2, 0.3], top_k=3
        )

    assert "768" in excinfo.value.message
    assert "3" in excinfo.value.message


def test_get_repository_points_at_rest_v1_exactly_once() -> None:
    """Regression: the configured base URL already ends in ``/rest/v1``.

    Appending it a second time produced ``/rest/v1/rest/v1/documents``, which
    PostgREST rejects with a bare 404 ("Invalid path specified in request URL").
    The mock-transport tests above missed it because they matched on the path
    suffix; only the live check caught it.
    """
    settings = Settings(
        environment="test",
        log_level="WARNING",
        log_json=False,
        document_store="supabase",
        auth_backend="supabase",
        supabase_url="https://project.supabase.co",
        supabase_anon_key=ANON_KEY,
        _env_file=None,  # type: ignore[call-arg]
    )
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return ok(request, [])

    app = FastAPI()
    app.state.settings = settings
    app.state.supabase_http = httpx.Client(transport=httpx.MockTransport(handler))

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/documents",
            "headers": [(b"authorization", f"Bearer {USER_TOKEN}".encode())],
            "app": app,
            "query_string": b"",
        }
    )

    repository = get_repository(
        request, UserIdentity(user_id=USER_ID, is_verified=True, source="supabase")
    )
    repository.list_documents(owner_id=USER_ID)

    assert str(seen[0]).startswith("https://project.supabase.co/rest/v1/documents?")
    assert "rest/v1/rest/v1" not in str(seen[0])
    assert seen[0].params["owner_id"] == f"eq.{USER_ID}"
