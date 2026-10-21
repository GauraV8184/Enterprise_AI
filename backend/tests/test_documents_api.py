"""End-to-end document API tests against a real app, real parsers, real store.

Only the two model calls are faked (chat model and embeddings). Everything else -
file validation, parsing, chunking, SQLite storage, ranking - runs for real.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.storage.errors import FileStoreUnavailableError
from tests.conftest import upload
from tests.fakes import FakeChatModel
from tests.sample_files import (
    LAPTOP_SECURITY_TEXT,
    REVENUE_REPORT_TEXT,
    write_csv_file,
    write_docx_file,
    write_fake_pdf,
    write_pdf_file,
    write_scanned_style_pdf,
    write_text_file,
)

ALICE = "alice"
BOB = "bob"


# --------------------------------------------------------------------------
# Upload and parse
# --------------------------------------------------------------------------


def test_upload_text_file_is_indexed_immediately(
    rag_client: TestClient, documents_dir: Path
) -> None:
    """A 201 means searchable, not merely received."""
    response = upload(rag_client, write_text_file(documents_dir))

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "ready"
    assert body["chunk_count"] >= 1
    assert body["indexed_at"] is not None
    assert body["error"] is None
    assert body["filename"] == "notes.txt"


def test_upload_pdf_reports_page_count(rag_client: TestClient, documents_dir: Path) -> None:
    response = upload(rag_client, write_pdf_file(documents_dir, pages=2))

    assert response.status_code == 201
    body = response.json()
    assert body["page_count"] == 2
    assert body["status"] == "ready"


def test_upload_csv_header_is_indexed(rag_client: TestClient, documents_dir: Path) -> None:
    response = upload(rag_client, write_csv_file(documents_dir))
    assert response.status_code == 201

    found = rag_client.post(
        "/api/documents/search",
        json={"query": "North Widget units revenue"},
        headers={"X-User-Id": "dev-user"},
    )
    assert found.status_code == 200
    assert "region | product | units | revenue" in found.json()["matches"][0]["snippet"]


def test_upload_docx_is_indexed(rag_client: TestClient, documents_dir: Path) -> None:
    response = upload(rag_client, write_docx_file(documents_dir))
    assert response.status_code == 201
    assert response.json()["chunk_count"] >= 1


def test_uploaded_document_is_listed(rag_client: TestClient, documents_dir: Path) -> None:
    created = upload(rag_client, write_text_file(documents_dir)).json()

    listing = rag_client.get("/api/documents")

    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    assert body["documents"][0]["id"] == created["id"]


def test_document_can_be_fetched_by_id(rag_client: TestClient, documents_dir: Path) -> None:
    created = upload(rag_client, write_text_file(documents_dir)).json()

    response = rag_client.get(f"/api/documents/{created['id']}")

    assert response.status_code == 200
    assert response.json() == created


def test_unknown_document_is_404(rag_client: TestClient) -> None:
    response = rag_client.get("/api/documents/does-not-exist")

    assert response.status_code == 404
    assert response.json()["code"] == "document_not_found"


def test_response_never_exposes_the_owner_or_storage_path(
    rag_client: TestClient, documents_dir: Path
) -> None:
    """Owner ids and internal paths are not the client's business."""
    body = upload(rag_client, write_text_file(documents_dir)).json()

    serialized = str(body)
    assert "owner_id" not in body
    assert "dev-user" not in serialized
    assert str(documents_dir) not in serialized


def test_path_traversal_in_the_filename_is_neutralised(
    rag_client: TestClient, documents_dir: Path
) -> None:
    path = write_text_file(documents_dir, name="inner.txt")

    response = upload(rag_client, path, filename="../../../windows/system32/evil.txt")

    assert response.status_code == 201
    assert response.json()["filename"] == "evil.txt"


# --------------------------------------------------------------------------
# Failed indexing
# --------------------------------------------------------------------------


def test_scanned_pdf_fails_loudly_but_is_still_recorded(
    rag_client: TestClient, documents_dir: Path
) -> None:
    """The upload succeeds and the record survives, so the user can see and remove it."""
    response = upload(rag_client, write_scanned_style_pdf(documents_dir))

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "no_extractable_text"
    assert body["details"]["status"] == "failed"
    document_id = body["details"]["document_id"]

    listed = rag_client.get("/api/documents").json()["documents"]
    stored = next(item for item in listed if item["id"] == document_id)
    assert stored["status"] == "failed"
    assert "no text layer" in stored["error"].lower()
    assert stored["chunk_count"] == 0

    # A failed document must not contribute searchable content.
    searched = rag_client.post("/api/documents/search", json={"query": "anything"}).json()
    assert searched["searched_chunks"] == 0


# --------------------------------------------------------------------------
# Rejected uploads
# --------------------------------------------------------------------------


def test_unsupported_extension_is_rejected(rag_client: TestClient, documents_dir: Path) -> None:
    path = documents_dir / "payload.exe"
    path.write_bytes(b"MZ\x90\x00")

    response = upload(rag_client, path)

    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_file_type"


def test_spoofed_pdf_is_rejected(rag_client: TestClient, documents_dir: Path) -> None:
    response = upload(rag_client, write_fake_pdf(documents_dir))

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_file"


def test_empty_upload_is_rejected(rag_client: TestClient, documents_dir: Path) -> None:
    path = documents_dir / "empty.txt"
    path.write_bytes(b"")

    response = upload(rag_client, path)

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_file"


def test_oversized_upload_is_rejected(tmp_path: Path, fake_embeddings) -> None:
    """A dedicated app is used so the 1 MB limit does not affect other tests."""
    settings = Settings(
        environment="test",
        log_json=False,
        log_level="WARNING",
        openai_api_key=None,
        data_dir=tmp_path / "limited",
        max_upload_mb=1,
        _env_file=None,  # type: ignore[call-arg]
    )
    app: FastAPI = create_app(settings)

    with TestClient(app) as client:
        app.state.embeddings = fake_embeddings
        response = client.post(
            "/api/documents",
            files={"file": ("big.txt", b"x" * (1024 * 1024 + 1), "text/plain")},
        )

    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"


def test_rejected_upload_creates_no_record(rag_client: TestClient, documents_dir: Path) -> None:
    """Validation must happen before storage, or junk accumulates as empty documents."""
    path = documents_dir / "payload.exe"
    path.write_bytes(b"MZ")

    upload(rag_client, path)

    assert rag_client.get("/api/documents").json()["total"] == 0


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


def test_search_finds_the_relevant_document(rag_client: TestClient, documents_dir: Path) -> None:
    """Two documents with disjoint vocabulary, so similarity alone decides the order."""
    upload(rag_client, write_text_file(documents_dir))
    upload(
        rag_client,
        write_pdf_file(
            documents_dir,
            name="security.pdf",
            texts=[REVENUE_REPORT_TEXT, LAPTOP_SECURITY_TEXT],
        ),
    )

    response = rag_client.post(
        "/api/documents/search", json={"query": "annual leave allowance", "top_k": 3}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["searched_chunks"] >= 2
    assert body["matches"][0]["filename"] == "notes.txt"
    assert body["matches"][0]["label"] == 1
    assert "annual leave" in body["matches"][0]["snippet"].lower()


def test_search_returns_the_other_document_for_a_different_query(
    rag_client: TestClient, documents_dir: Path
) -> None:
    upload(rag_client, write_text_file(documents_dir))
    upload(
        rag_client,
        write_pdf_file(
            documents_dir,
            name="security.pdf",
            texts=[REVENUE_REPORT_TEXT, LAPTOP_SECURITY_TEXT],
        ),
    )

    body = rag_client.post(
        "/api/documents/search", json={"query": "full-disk encryption on laptops", "top_k": 1}
    ).json()

    assert body["matches"][0]["filename"] == "security.pdf"
    assert body["matches"][0]["page"] == 2


def test_search_on_an_empty_corpus_returns_no_matches(rag_client: TestClient) -> None:
    response = rag_client.post("/api/documents/search", json={"query": "anything"})

    assert response.status_code == 200
    body = response.json()
    assert body["matches"] == []
    assert body["searched_chunks"] == 0


def test_search_rejects_a_blank_query(rag_client: TestClient) -> None:
    response = rag_client.post("/api/documents/search", json={"query": "   "})

    assert response.status_code == 422


def test_search_top_k_is_bounded(rag_client: TestClient) -> None:
    response = rag_client.post("/api/documents/search", json={"query": "x", "top_k": 999})
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Ask
# --------------------------------------------------------------------------


def test_ask_returns_a_grounded_answer_with_citations(
    rag_client: TestClient, documents_dir: Path, fake_model: FakeChatModel
) -> None:
    upload(rag_client, write_text_file(documents_dir))
    fake_model.replies = ["Full-time employees accrue twenty days of annual leave [1]."]

    response = rag_client.post(
        "/api/documents/ask", json={"question": "How much annual leave do employees get?"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["grounded"] is True
    assert body["sources"][0]["label"] == 1
    assert body["sources"][0]["filename"] == "notes.txt"
    assert body["model"]
    assert body["request_id"]

    # The passage the client is told to cite is the passage the model received.
    # This is the contract that makes the citation verifiable rather than decorative.
    cited = body["sources"][0]["snippet"]
    sent = "\n".join(str(message.content) for message in fake_model.received[0])
    assert cited[:100] in sent
    assert "[1] Source: notes.txt" in sent


def test_ask_without_documents_does_not_call_the_model(
    rag_client: TestClient, fake_model: FakeChatModel
) -> None:
    fake_model.replies = ["invented"]

    response = rag_client.post("/api/documents/ask", json={"question": "What is the policy?"})

    assert response.status_code == 200
    body = response.json()
    assert body["grounded"] is False
    assert body["sources"] == []
    assert "No documents have been indexed yet" in body["answer"]
    assert fake_model.received == [], "the model was invoked with an empty context"


def test_ask_can_be_scoped_to_one_document(
    rag_client: TestClient, documents_dir: Path, fake_model: FakeChatModel
) -> None:
    upload(rag_client, write_text_file(documents_dir))
    revenue = upload(rag_client, write_pdf_file(documents_dir, name="revenue.pdf", pages=3)).json()
    fake_model.replies = ["Revenue reached 4,820,000 dollars [1]."]

    response = rag_client.post(
        "/api/documents/ask",
        json={"question": "What was the total revenue?", "document_ids": [revenue["id"]]},
    )

    assert response.status_code == 200
    body = response.json()
    assert {source["filename"] for source in body["sources"]} == {"revenue.pdf"}
    assert REVENUE_REPORT_TEXT.split()[0] in body["answer"] or "4,820,000" in body["answer"]


def test_ask_rejects_a_blank_question(rag_client: TestClient) -> None:
    response = rag_client.post("/api/documents/ask", json={"question": "   "})
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Delete
# --------------------------------------------------------------------------


def test_delete_removes_the_document_and_its_vectors(
    rag_client: TestClient, documents_dir: Path
) -> None:
    created = upload(rag_client, write_text_file(documents_dir)).json()

    deleted = rag_client.delete(f"/api/documents/{created['id']}")
    assert deleted.status_code == 204

    assert rag_client.get(f"/api/documents/{created['id']}").status_code == 404
    assert rag_client.get("/api/documents").json()["total"] == 0

    # Nothing left to retrieve: orphaned vectors would keep answering.
    searched = rag_client.post("/api/documents/search", json={"query": "annual leave"}).json()
    assert searched["searched_chunks"] == 0
    assert searched["matches"] == []


def test_deleting_an_unknown_document_is_404(rag_client: TestClient) -> None:
    assert rag_client.delete("/api/documents/nope").status_code == 404


# --------------------------------------------------------------------------
# Multi-user isolation
# --------------------------------------------------------------------------


def test_one_user_cannot_see_another_users_documents(
    rag_client: TestClient, documents_dir: Path, fake_model: FakeChatModel
) -> None:
    created = upload(rag_client, write_text_file(documents_dir), user=ALICE).json()

    listing = rag_client.get("/api/documents", headers={"X-User-Id": BOB}).json()
    assert listing["total"] == 0

    assert (
        rag_client.get(f"/api/documents/{created['id']}", headers={"X-User-Id": BOB}).status_code
        == 404
    )
    assert (
        rag_client.delete(f"/api/documents/{created['id']}", headers={"X-User-Id": BOB}).status_code
        == 404
    )

    # Alice's document is untouched by Bob's attempts.
    assert (
        rag_client.get(f"/api/documents/{created['id']}", headers={"X-User-Id": ALICE}).status_code
        == 200
    )


def test_search_and_ask_are_scoped_per_user(
    rag_client: TestClient, documents_dir: Path, fake_model: FakeChatModel
) -> None:
    upload(rag_client, write_text_file(documents_dir), user=ALICE)

    bob_search = rag_client.post(
        "/api/documents/search",
        json={"query": "annual leave"},
        headers={"X-User-Id": BOB},
    ).json()
    assert bob_search["searched_chunks"] == 0
    assert bob_search["matches"] == []

    bob_ask = rag_client.post(
        "/api/documents/ask",
        json={"question": "How much annual leave?"},
        headers={"X-User-Id": BOB},
    ).json()
    assert bob_ask["grounded"] is False


def test_different_users_get_different_default_identities(
    rag_client: TestClient, documents_dir: Path
) -> None:
    """Without a header the configured development user is used."""
    upload(rag_client, write_text_file(documents_dir))

    assert rag_client.get("/api/documents").json()["total"] == 1
    assert rag_client.get("/api/documents", headers={"X-User-Id": BOB}).json()["total"] == 0


def test_empty_user_header_is_rejected(rag_client: TestClient) -> None:
    """An explicit but empty identity is a client bug, not a request to be anonymous."""
    response = rag_client.get("/api/documents", headers={"X-User-Id": "   "})

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


# --------------------------------------------------------------------------
# Original file storage
# --------------------------------------------------------------------------


def test_the_original_is_stored_under_the_owner_and_document(
    rag_client: TestClient, documents_dir: Path
) -> None:
    """The key layout is not cosmetic: the Supabase policies check its first segment."""
    source = write_text_file(documents_dir)
    body = upload(rag_client, source).json()
    settings: Settings = rag_client.app.state.settings

    stored = settings.uploads_dir / settings.dev_user_id / body["id"] / body["filename"]

    assert stored.read_bytes() == source.read_bytes()

    assert rag_client.delete(f"/api/documents/{body['id']}").status_code == 204
    assert not stored.exists()


class _UnavailableFileStore:
    """Every operation fails the way a storage outage would."""

    def put(self, **_kwargs: object) -> str:
        raise FileStoreUnavailableError()

    def read(self, **_kwargs: object) -> bytes:
        raise FileStoreUnavailableError()

    def delete(self, **_kwargs: object) -> None:
        raise FileStoreUnavailableError()


def test_a_storage_failure_leaves_a_visible_failed_document(
    rag_client: TestClient, documents_dir: Path
) -> None:
    """A row created before an outage must not sit at ``pending`` forever."""
    rag_client.app.state.file_store = _UnavailableFileStore()

    response = upload(rag_client, write_text_file(documents_dir))

    assert response.status_code == 503
    assert response.json()["code"] == "file_store_unavailable"

    document_id = response.json()["details"]["document_id"]
    listing = rag_client.get("/api/documents").json()["documents"]
    record = next(item for item in listing if item["id"] == document_id)

    assert record["status"] == "failed"
    assert record["chunk_count"] == 0
    assert record["error"]
