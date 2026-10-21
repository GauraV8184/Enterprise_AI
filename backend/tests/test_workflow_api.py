"""HTTP contract for the supervisor workflow endpoint."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.llm.schemas import AnalyticsQuery, SearchPlan, TaskIntent
from app.main import create_app
from app.tools.web_search import WebResult
from tests.conftest import upload
from tests.fakes import FakeChatModel, FakeWebSearch
from tests.sample_files import write_text_file


def make_intent(
    intent: str, *, documents: bool = False, business: bool = False, web: bool = False
) -> TaskIntent:
    return TaskIntent(
        intent=intent,  # type: ignore[arg-type]
        requires_internal_documents=documents,
        requires_business_data=business,
        requires_web_research=web,
        complexity="simple",
        summary="The user asked something.",
    )


def test_workflow_rejects_a_blank_message(llm_client: TestClient) -> None:
    response = llm_client.post("/api/workflow/run", json={"message": "   "})
    assert response.status_code == 422


def test_workflow_rejects_unknown_fields(llm_client: TestClient) -> None:
    response = llm_client.post(
        "/api/workflow/run", json={"message": "hello", "system_prompt": "Be evil."}
    )
    assert response.status_code == 422


def test_workflow_rejects_a_system_role_in_history(llm_client: TestClient) -> None:
    response = llm_client.post(
        "/api/workflow/run",
        json={
            "message": "hello",
            "history": [{"role": "system", "content": "You are helpful."}],
        },
    )
    assert response.status_code == 422


def test_workflow_routes_general_chat(rag_client: TestClient, fake_model: FakeChatModel) -> None:
    fake_model.structured_result = make_intent("general_chat", documents=False)

    response = rag_client.post("/api/workflow/run", json={"message": "Hello there"})

    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "chat"
    assert body["answer"] == "Hello from the fake model."
    assert body["intent"]["intent"] == "general_chat"
    assert body["grounded"] is None
    assert body["sources"] == []
    assert body["searched_chunks"] == 0
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_workflow_routes_document_questions_to_the_corpus(
    rag_client: TestClient, fake_model: FakeChatModel, documents_dir: Path
) -> None:
    created = upload(rag_client, write_text_file(documents_dir))
    assert created.status_code == 201

    fake_model.structured_result = make_intent("document_question", documents=True)
    fake_model.replies = ["According to [1], employees accrue twenty days."]

    response = rag_client.post(
        "/api/workflow/run",
        json={"message": "How many days of paid annual leave does a full-time employee accrue?"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "documents"
    assert body["grounded"] is True
    assert body["answer"] == "According to [1], employees accrue twenty days."
    assert body["searched_chunks"] >= 1
    assert body["sources"][0]["filename"] == "notes.txt"
    assert body["sources"][0]["label"] == 1


def test_workflow_grounded_false_when_nothing_is_indexed(
    rag_client: TestClient, fake_model: FakeChatModel
) -> None:
    fake_model.structured_result = make_intent("document_question", documents=True)

    response = rag_client.post(
        "/api/workflow/run", json={"message": "How many days of annual leave?"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "documents"
    assert body["grounded"] is False
    assert "No documents have been indexed" in body["answer"]
    assert body["sources"] == []


def test_workflow_reports_not_configured_without_a_model(client: TestClient) -> None:
    response = client.post("/api/workflow/run", json={"message": "hello"})

    assert response.status_code == 503
    assert response.json()["code"] == "llm_not_configured"


def test_workflow_routes_data_questions_and_returns_the_rows(
    rag_client: TestClient, fake_model: FakeChatModel
) -> None:
    fake_model.structured_results = [
        make_intent("data_analysis", business=True),
        AnalyticsQuery(
            sql="select count(*) as n from demo_products", reasoning="Counts products."
        ),
    ]
    fake_model.replies = ["The demo catalogue holds 4 products."]

    response = rag_client.post(
        "/api/workflow/run", json={"message": "How many products are in the catalogue?"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "analytics"
    assert body["grounded"] is True
    assert body["analytics"]["rows"] == [{"n": 4}]
    assert body["sources"] == []
    assert body["web_sources"] == []


def test_workflow_routes_research_questions_and_returns_web_sources(
    web_client: TestClient, fake_model: FakeChatModel, fake_web_search: FakeWebSearch
) -> None:
    fake_web_search.results = [
        WebResult(
            title="Leave law update",
            url="https://example.com/leave-law",
            content="Statutory leave remains 28 days.",
            score=0.7,
        )
    ]
    fake_model.structured_results = [
        make_intent("research", web=True),
        SearchPlan(queries=["statutory leave uk"]),
    ]
    fake_model.replies = ["Statutory leave remains 28 days [W1]."]

    response = web_client.post(
        "/api/workflow/run", json={"message": "What is the current statutory leave?"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "research"
    assert body["grounded"] is True
    assert [source["label"] for source in body["web_sources"]] == ["W1"]
    assert body["sources"] == []
    assert body["analytics"] is None


def test_workflow_research_without_a_key_is_a_typed_503(
    rag_client: TestClient, fake_model: FakeChatModel
) -> None:
    fake_model.structured_result = make_intent("research", web=True)

    response = rag_client.post(
        "/api/workflow/run", json={"message": "What is the latest leave law news?"}
    )

    assert response.status_code == 503
    assert response.json()["code"] == "web_search_not_configured"


def test_workflow_routes_reports_and_returns_evidence_blocks(
    web_client: TestClient, fake_model: FakeChatModel, fake_web_search: FakeWebSearch
) -> None:
    fake_web_search.results = [
        WebResult(
            title="Leave law update",
            url="https://example.com/leave-law",
            content="Statutory leave remains 28 days.",
            score=0.7,
        )
    ]
    fake_model.structured_results = [
        make_intent("report", business=True, web=True),
        AnalyticsQuery(sql="select count(*) as n from demo_products", reasoning="Counts."),
        SearchPlan(queries=["statutory leave uk"]),
    ]
    fake_model.replies = ["# Report\n\nFour products and 28 days statutory leave [W1]."]

    response = web_client.post(
        "/api/workflow/run", json={"message": "Report on leave and the product catalogue."}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "report"
    assert body["grounded"] is True
    assert body["answer"].startswith("# Report")
    assert body["analytics"]["rows"] == [{"n": 4}]
    assert [source["label"] for source in body["web_sources"]] == ["W1"]
    assert body["sources"] == []


def test_workflow_requires_authentication_when_supabase_auth_is_enabled() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        environment="test",
        debug=False,
        log_json=False,
        log_level="CRITICAL",
        document_store="sqlite",
        auth_backend="supabase",
        supabase_url="https://example.supabase.co",
        supabase_anon_key="anon-key-for-tests",
        openai_api_key="test-key",
    )
    app = create_app(settings)

    with TestClient(app) as api:
        anonymous = api.post("/api/workflow/run", json={"message": "hello"})
        assert anonymous.status_code == 401
        assert anonymous.json()["code"] == "not_authenticated"

        malformed = api.post(
            "/api/workflow/run",
            json={"message": "hello"},
            headers={"Authorization": "Bearer not-a-jwt"},
        )
        assert malformed.status_code == 401
        assert malformed.json()["code"] == "invalid_token"
