"""HTTP contract for the report endpoint.

The interesting assertions are not that a 200 comes back: they are that the
response carries the same evidence the model was given, that labels line up with
what the report cites, and that a request which cannot be answered honestly
fails or explains itself instead of producing a plausible-looking document.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.llm.schemas import AnalyticsQuery, SearchPlan
from app.tools.web_search import WebResult
from tests.conftest import upload
from tests.fakes import FakeChatModel, FakeWebSearch, HashingEmbeddings, make_connection_error
from tests.sample_files import write_text_file

LEAVE_TOPIC = "How much paid annual leave does a full-time employee accrue?"

WEB_RESULT = WebResult(
    title="Leave law update",
    url="https://example.com/leave-law",
    content="Statutory leave remains 28 days.",
    score=0.7,
)


def valid_query() -> AnalyticsQuery:
    return AnalyticsQuery(
        sql="select count(*) as n from demo_products", reasoning="Counts products."
    )


def test_reports_reject_a_blank_topic(llm_client: TestClient) -> None:
    assert llm_client.post("/api/reports", json={"topic": "   "}).status_code == 422


def test_reports_reject_unknown_fields(llm_client: TestClient) -> None:
    response = llm_client.post("/api/reports", json={"topic": "leave", "temperature": 0.9})
    assert response.status_code == 422


def test_reports_reject_an_empty_source_list(llm_client: TestClient) -> None:
    assert (
        llm_client.post("/api/reports", json={"topic": "leave", "sources": []}).status_code == 422
    )


def test_reports_reject_an_unknown_source(llm_client: TestClient) -> None:
    response = llm_client.post(
        "/api/reports", json={"topic": "leave", "sources": ["documents", "warehouse"]}
    )
    assert response.status_code == 422


def test_reports_default_to_the_corpus_only(
    rag_client: TestClient, fake_model: FakeChatModel
) -> None:
    response = rag_client.post("/api/reports", json={"topic": LEAVE_TOPIC})

    assert response.status_code == 200
    body = response.json()
    assert body["sources_requested"] == ["documents"]
    assert body["grounded"] is False
    assert body["web_sources"] == []
    assert body["analytics"] is None
    assert "No evidence was found" in body["report"]
    assert fake_model.received == []


def test_report_maps_an_embedding_outage_to_a_typed_error(
    rag_client: TestClient, fake_model: FakeChatModel
) -> None:
    """A dead embedding endpoint is a provider error, not an internal error."""

    class UnreachableEmbeddings(HashingEmbeddings):
        def embed_query(self, text: str) -> list[float]:
            raise make_connection_error()

    rag_client.app.state.embeddings = UnreachableEmbeddings()

    response = rag_client.post(
        "/api/reports", json={"topic": LEAVE_TOPIC, "sources": ["documents"]}
    )

    assert response.status_code == 502
    body = response.json()
    assert body["code"] == "llm_connection_failed"
    assert "Could not reach" in body["detail"]
    assert fake_model.received == []


def test_reports_deduplicate_repeated_sources(
    rag_client: TestClient, fake_model: FakeChatModel
) -> None:
    response = rag_client.post(
        "/api/reports", json={"topic": LEAVE_TOPIC, "sources": ["documents", "documents"]}
    )

    assert response.status_code == 200
    assert response.json()["sources_requested"] == ["documents"]


def test_document_report_carries_matching_citation_labels(
    rag_client: TestClient, fake_model: FakeChatModel, documents_dir: Path
) -> None:
    created = upload(rag_client, write_text_file(documents_dir))
    assert created.status_code == 201

    fake_model.replies = ["# Report\n\nEmployees accrue twenty days of leave [1]."]

    response = rag_client.post("/api/reports", json={"topic": LEAVE_TOPIC})

    assert response.status_code == 200
    body = response.json()
    assert body["grounded"] is True
    assert body["report"].startswith("# Report")
    assert [source["filename"] for source in body["sources"]] == ["notes.txt"]
    assert body["sources"][0]["label"] == 1
    assert "[1] Source: notes.txt" in str(fake_model.received[-1][-1].content)


def test_analytics_report_returns_the_query_and_rows(
    rag_client: TestClient, fake_model: FakeChatModel
) -> None:
    fake_model.structured_results = [valid_query()]
    fake_model.replies = ["# Report\n\nThe catalogue holds 4 products."]

    response = rag_client.post(
        "/api/reports", json={"topic": "Product catalogue size", "sources": ["analytics"]}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["grounded"] is True
    assert body["analytics"]["rows"] == [{"n": 4}]
    assert body["analytics"]["truncated"] is False
    assert body["sources"] == []
    assert '"n": 4' in str(fake_model.received[-1][-1].content)


def test_web_report_without_a_key_is_a_typed_503(
    rag_client: TestClient, fake_model: FakeChatModel
) -> None:
    response = rag_client.post(
        "/api/reports", json={"topic": "Statutory leave law", "sources": ["web"]}
    )

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "web_search_not_configured"
    assert "TAVILY_API_KEY" in body["detail"]
    assert fake_model.received == []


def test_web_report_labels_results_with_w_prefixes(
    web_client: TestClient,
    fake_model: FakeChatModel,
    fake_web_search: FakeWebSearch,
) -> None:
    fake_web_search.results = [WEB_RESULT]
    fake_model.structured_results = [SearchPlan(queries=["statutory leave uk"])]
    fake_model.replies = ["# Report\n\nStatutory leave is 28 days [W1]."]

    response = web_client.post(
        "/api/reports", json={"topic": "Statutory leave law", "sources": ["web"]}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["grounded"] is True
    assert [source["label"] for source in body["web_sources"]] == ["W1"]
    assert body["web_sources"][0]["url"] == WEB_RESULT.url
    assert fake_web_search.queries == ["statutory leave uk"]


def test_a_report_that_cannot_query_the_data_fails_with_the_reason(
    rag_client: TestClient, fake_model: FakeChatModel
) -> None:
    blocked = AnalyticsQuery(sql="drop table demo_products", reasoning="Not allowed.")
    fake_model.structured_results = [blocked, blocked]

    response = rag_client.post(
        "/api/reports", json={"topic": "Product catalogue size", "sources": ["analytics"]}
    )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "analytics_query_rejected"
    assert "SELECT" in body["detail"]
    assert len(fake_model.structured_received) == 2


def test_reports_report_not_configured_without_a_model(client: TestClient) -> None:
    response = client.post("/api/reports", json={"topic": "leave"})

    assert response.status_code == 503
    assert response.json()["code"] == "llm_not_configured"
