"""Tests for the chat HTTP API: success paths, validation, and honest failures."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.llm.schemas import TaskIntent
from app.main import create_app
from tests.fakes import FakeChatModel, make_rate_limit_error

# --------------------------------------------------------------------------
# GET /api/chat/status
# --------------------------------------------------------------------------


def test_status_reports_not_configured(client: TestClient) -> None:
    body = client.get("/api/chat/status").json()
    assert body["configured"] is False
    assert body["provider"] == "openai"
    assert "OPENAI_API_KEY" in body["detail"]


def test_status_never_leaks_the_api_key(llm_client: TestClient) -> None:
    response = llm_client.get("/api/chat/status")
    assert "sk-" not in response.text
    assert "api_key" not in response.text.lower().replace("openai_api_key", "")


def test_llm_endpoints_require_authentication_when_supabase_auth_is_enabled() -> None:
    """Anonymous callers must not be able to spend the deployment's LLM quota."""
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
        requests = (
            api.get("/api/chat/status"),
            api.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]}),
            api.post("/api/chat/intent", json={"text": "hello"}),
        )

        for response in requests:
            assert response.status_code == 401
            assert response.json()["code"] == "not_authenticated"

        malformed = api.post(
            "/api/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer not-a-jwt"},
        )
        assert malformed.status_code == 401
        assert malformed.json()["code"] == "invalid_token"


# --------------------------------------------------------------------------
# POST /api/chat
# --------------------------------------------------------------------------


def test_chat_returns_answer(llm_client: TestClient) -> None:
    response = llm_client.post("/api/chat", json={"messages": [{"role": "user", "content": "Hi"}]})
    assert response.status_code == 200

    body = response.json()
    assert body["answer"] == "Hello from the fake model."
    assert body["provider"] == "openai"
    assert body["model"] == "fake-model"  # taken from provider metadata
    assert body["request_id"]
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_chat_returns_token_usage_when_available(llm_client: TestClient, fake_model) -> None:
    fake_model.usage = {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}
    body = llm_client.post(
        "/api/chat", json={"messages": [{"role": "user", "content": "Hi"}]}
    ).json()
    assert body["usage"] == {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}


def test_chat_usage_is_null_when_provider_omits_it(llm_client: TestClient) -> None:
    body = llm_client.post(
        "/api/chat", json={"messages": [{"role": "user", "content": "Hi"}]}
    ).json()
    assert body["usage"] is None


def test_chat_sends_the_whole_conversation(
    llm_client: TestClient, fake_model: FakeChatModel
) -> None:
    llm_client.post(
        "/api/chat",
        json={
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
                {"role": "user", "content": "third"},
            ]
        },
    )
    contents = [message.content for message in fake_model.received[0]]
    assert "first" in contents and "second" in contents and "third" in contents


def test_chat_honours_custom_system_prompt(llm_client: TestClient, fake_model) -> None:
    llm_client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "Hi"}], "system_prompt": "Be brief."},
    )
    assert fake_model.received[0][0].content == "Be brief."


def test_chat_passes_temperature_override(llm_client: TestClient, fake_model) -> None:
    llm_client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "Hi"}], "temperature": 1.2},
    )
    assert fake_model.bound_kwargs["temperature"] == 1.2


def test_chat_without_configuration_returns_503(client: TestClient) -> None:
    """No key configured must fail loudly, never fabricate an answer."""
    response = client.post("/api/chat", json={"messages": [{"role": "user", "content": "Hi"}]})
    assert response.status_code == 503

    body = response.json()
    assert body["code"] == "llm_not_configured"
    assert "OPENAI_API_KEY" in body["detail"]
    assert body["request_id"]


def test_provider_rate_limit_becomes_429(llm_client: TestClient, fake_model) -> None:
    fake_model.error = make_rate_limit_error()
    response = llm_client.post("/api/chat", json={"messages": [{"role": "user", "content": "Hi"}]})
    assert response.status_code == 429
    assert response.json()["code"] == "llm_rate_limited"


def test_exhausted_credits_return_402_with_billing_advice(
    llm_client: TestClient, fake_model
) -> None:
    """An empty balance must not be reported as something retrying would fix."""
    from tests.fakes import make_quota_error

    fake_model.error = make_quota_error()
    response = llm_client.post("/api/chat", json={"messages": [{"role": "user", "content": "Hi"}]})
    assert response.status_code == 402

    body = response.json()
    assert body["code"] == "llm_quota_exhausted"
    assert "billing" in body["detail"]
    assert body["details"]["provider_code"] == "credit_balance_exhausted"


def test_provider_auth_error_is_reported_clearly(llm_client: TestClient, fake_model) -> None:
    from tests.fakes import make_authentication_error

    fake_model.error = make_authentication_error()
    response = llm_client.post("/api/chat", json={"messages": [{"role": "user", "content": "Hi"}]})
    assert response.status_code == 502
    body = response.json()
    assert body["code"] == "llm_authentication_failed"
    assert "OPENAI_API_KEY" in body["detail"]
    # The provider's raw body must never be echoed to the client.
    assert "provider error" not in response.text


# --------------------------------------------------------------------------
# Request validation
# --------------------------------------------------------------------------


def test_empty_messages_are_rejected(llm_client: TestClient) -> None:
    assert llm_client.post("/api/chat", json={"messages": []}).status_code == 422


def test_blank_content_is_rejected(llm_client: TestClient) -> None:
    response = llm_client.post("/api/chat", json={"messages": [{"role": "user", "content": "   "}]})
    assert response.status_code == 422


def test_system_only_conversation_is_rejected(llm_client: TestClient) -> None:
    response = llm_client.post(
        "/api/chat", json={"messages": [{"role": "system", "content": "You are helpful."}]}
    )
    assert response.status_code == 422


def test_invalid_role_is_rejected(llm_client: TestClient) -> None:
    response = llm_client.post("/api/chat", json={"messages": [{"role": "robot", "content": "Hi"}]})
    assert response.status_code == 422


def test_out_of_range_temperature_is_rejected(llm_client: TestClient) -> None:
    response = llm_client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "Hi"}], "temperature": 5.0},
    )
    assert response.status_code == 422


def test_overlong_message_is_rejected(llm_client: TestClient) -> None:
    response = llm_client.post(
        "/api/chat", json={"messages": [{"role": "user", "content": "x" * 8001}]}
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# POST /api/chat/intent  (structured output)
# --------------------------------------------------------------------------


def _sample_intent() -> TaskIntent:
    return TaskIntent(
        intent="report",
        requires_internal_documents=True,
        requires_business_data=True,
        requires_web_research=True,
        complexity="multi_step",
        summary="Analyse Q3 sales, compare with strategy documents and market trends.",
    )


def test_intent_endpoint_returns_structured_result(llm_client: TestClient, fake_model) -> None:
    fake_model.structured_result = _sample_intent()
    response = llm_client.post("/api/chat/intent", json={"text": "Analyze our Q3 sales"})
    assert response.status_code == 200

    body = response.json()
    assert body["intent"]["intent"] == "report"
    assert body["intent"]["requires_business_data"] is True
    assert body["intent"]["complexity"] == "multi_step"
    assert body["request_id"]


def test_intent_endpoint_requires_non_blank_text(llm_client: TestClient) -> None:
    assert llm_client.post("/api/chat/intent", json={"text": ""}).status_code == 422
    assert llm_client.post("/api/chat/intent", json={"text": "  "}).status_code == 422


def test_intent_endpoint_without_configuration_returns_503(client: TestClient) -> None:
    response = client.post("/api/chat/intent", json={"text": "Analyze our Q3 sales"})
    assert response.status_code == 503
    assert response.json()["code"] == "llm_not_configured"


# --------------------------------------------------------------------------
# Startup wiring
# --------------------------------------------------------------------------


def test_lifespan_builds_a_model_when_configured() -> None:
    """With a key present the app builds a real client (no network call is made)."""
    from fastapi.testclient import TestClient as _TestClient

    from app.main import create_app

    settings = Settings(environment="test", openai_api_key="sk-test-value", _env_file=None)  # type: ignore[call-arg]
    app = create_app(settings)
    with _TestClient(app):
        assert app.state.chat_model is not None
        assert type(app.state.chat_model).__name__ == "ChatOpenAI"


def test_lifespan_leaves_model_none_when_unconfigured(client: TestClient) -> None:
    assert client.app.state.chat_model is None  # type: ignore[attr-defined]
