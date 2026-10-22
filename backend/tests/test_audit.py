"""The audit trail, written by the real endpoints into the real SQLite store.

The point of these tests is that the event is produced by the same code path a
user takes, not by calling the store directly. The live counterpart is
``scripts/check_audit.py``, which proves the Postgres policies and the
token-derived actor.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.llm.schemas import TaskIntent
from tests.conftest import upload
from tests.fakes import FakeChatModel
from tests.sample_files import write_text_file


def audit_rows(settings: Settings) -> list[sqlite3.Row]:
    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT owner_id, action, resource_type, resource_id, request_id, detail "
            "FROM audit_events ORDER BY created_at, rowid"
        ).fetchall()


def test_upload_and_delete_are_recorded(
    rag_client: TestClient, settings: Settings, documents_dir: Path
) -> None:
    path = write_text_file(
        documents_dir,
        "leave.txt",
        "Every full-time employee accrues twenty days of paid annual leave.",
    )

    uploaded = upload(rag_client, path)
    assert uploaded.status_code == 201
    document_id = uploaded.json()["id"]

    assert rag_client.delete(f"/api/documents/{document_id}").status_code == 204

    rows = audit_rows(settings)
    assert [row["action"] for row in rows] == ["document.uploaded", "document.deleted"]
    assert all(row["owner_id"] == "dev-user" for row in rows)
    assert rows[0]["resource_id"] == document_id
    assert rows[0]["request_id"]

    uploaded_detail = json.loads(rows[0]["detail"])
    assert uploaded_detail["extension"] == ".txt"
    assert uploaded_detail["chunk_count"] >= 1
    assert uploaded_detail["size_bytes"] == path.stat().st_size

    assert json.loads(rows[1]["detail"]) == {"filename": "leave.txt"}


def test_asking_a_document_question_is_recorded(
    rag_client: TestClient, settings: Settings, documents_dir: Path
) -> None:
    path = write_text_file(documents_dir, "leave.txt")
    assert upload(rag_client, path).status_code == 201

    response = rag_client.post(
        "/api/documents/ask",
        json={"question": "How many days of annual leave do employees accrue?"},
    )
    assert response.status_code == 200

    rows = audit_rows(settings)
    assert [row["action"] for row in rows] == ["document.uploaded", "documents.asked"]
    assert rows[1]["request_id"]
    detail = json.loads(rows[1]["detail"])
    assert set(detail) == {"grounded", "sources", "searched_chunks"}
    assert isinstance(detail["grounded"], bool)


def test_a_workflow_run_is_recorded(
    rag_client: TestClient, settings: Settings, fake_model: FakeChatModel
) -> None:
    fake_model.structured_result = TaskIntent(
        intent="general_chat",
        requires_internal_documents=False,
        requires_business_data=False,
        requires_web_research=False,
        complexity="simple",
        summary="A greeting.",
    )

    assert rag_client.post("/api/workflow/run", json={"message": "Hello there"}).status_code == 200

    rows = audit_rows(settings)
    assert [row["action"] for row in rows] == ["workflow.ran"]
    assert rows[0]["request_id"]
    detail = json.loads(rows[0]["detail"])
    assert detail["route"] == "chat"
    assert detail["intent"] == "general_chat"
    assert detail["grounded"] is None


def test_a_generated_report_is_recorded(rag_client: TestClient, settings: Settings) -> None:
    response = rag_client.post("/api/reports", json={"topic": "annual leave"})
    assert response.status_code == 200

    rows = audit_rows(settings)
    assert [row["action"] for row in rows] == ["report.generated"]
    assert rows[0]["resource_type"] == "report"
    assert rows[0]["request_id"]
    detail = json.loads(rows[0]["detail"])
    assert set(detail) == {
        "grounded",
        "sources_requested",
        "documents",
        "web_sources",
        "analytics_rows",
    }
    assert "topic" not in detail
    assert detail["sources_requested"] == ["documents"]
    assert detail["grounded"] is False


def test_an_audit_failure_does_not_fail_the_action(
    app: FastAPI,
    rag_client: TestClient,
    settings: Settings,
    documents_dir: Path,
) -> None:
    """The upload has already happened; a logging failure must not turn it into
    a 500 the caller would retry (and thereby upload twice)."""

    def boom(**kwargs: object) -> None:
        raise RuntimeError("audit store is down")

    app.state.repository.record_audit_event = boom  # type: ignore[method-assign]

    uploaded = upload(rag_client, write_text_file(documents_dir, "notes.txt"))

    assert uploaded.status_code == 201
    assert audit_rows(settings) == []
