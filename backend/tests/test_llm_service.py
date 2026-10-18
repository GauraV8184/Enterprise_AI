"""Tests for the LLM service layer: message building, normalisation, errors."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.core.errors import AppError
from app.llm.errors import (
    LlmAuthenticationError,
    LlmConnectionError,
    LlmProviderError,
    LlmQuotaExhaustedError,
    LlmRateLimitError,
    LlmStructuredOutputError,
    LlmTimeoutError,
)
from app.llm.prompts import DEFAULT_SYSTEM_PROMPT
from app.llm.schemas import TaskIntent
from app.llm.service import (
    build_messages,
    classify_intent,
    generate_answer,
    map_exception,
)
from tests.fakes import (
    FakeChatModel,
    make_authentication_error,
    make_connection_error,
    make_quota_error,
    make_rate_limit_error,
    make_status_error,
    make_timeout_error,
)

# --------------------------------------------------------------------------
# build_messages
# --------------------------------------------------------------------------


def test_default_system_prompt_is_prepended() -> None:
    messages = build_messages([{"role": "user", "content": "hi"}])
    assert isinstance(messages[0], SystemMessage)
    assert messages[0].content == DEFAULT_SYSTEM_PROMPT
    assert isinstance(messages[1], HumanMessage)


def test_custom_system_prompt_is_used() -> None:
    messages = build_messages([{"role": "user", "content": "hi"}], system_prompt="Be terse.")
    assert messages[0].content == "Be terse."


def test_explicit_system_message_is_not_duplicated() -> None:
    messages = build_messages(
        [{"role": "system", "content": "custom"}, {"role": "user", "content": "hi"}]
    )
    assert len([m for m in messages if isinstance(m, SystemMessage)]) == 1
    assert messages[0].content == "custom"


def test_conversation_order_and_roles_are_preserved() -> None:
    messages = build_messages(
        [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ]
    )
    assert isinstance(messages[1], HumanMessage) and messages[1].content == "one"
    assert isinstance(messages[2], AIMessage) and messages[2].content == "two"
    assert isinstance(messages[3], HumanMessage) and messages[3].content == "three"


def test_unknown_role_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported message role"):
        build_messages([{"role": "robot", "content": "hi"}])


# --------------------------------------------------------------------------
# generate_answer
# --------------------------------------------------------------------------


async def test_generate_answer_returns_text_and_usage() -> None:
    model = FakeChatModel(
        replies=["Q3 revenue rose 12%."],
        usage={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
    )
    answer = await generate_answer(model, history=[{"role": "user", "content": "how was Q3?"}])

    assert answer.text == "Q3 revenue rose 12%."
    assert answer.usage is not None
    assert answer.usage.total_tokens == 18
    assert answer.finish_reason == "stop"
    assert answer.model_name == "fake-model"


async def test_generate_answer_handles_missing_usage() -> None:
    model = FakeChatModel(replies=["ok"])
    answer = await generate_answer(model, history=[{"role": "user", "content": "hi"}])
    assert answer.usage is None


async def test_generate_answer_normalises_block_content() -> None:
    model = FakeChatModel()
    model.replies = ["ignored"]
    # Simulate a provider returning content blocks instead of a plain string.
    original = FakeChatModel._generate

    def patched(self, messages, stop=None, run_manager=None, **kwargs):
        self.received.append(list(messages))
        from langchain_core.outputs import ChatGeneration, ChatResult

        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content=[
                            {"type": "text", "text": "Hello "},
                            {"type": "text", "text": "world"},
                        ]
                    )
                )
            ]
        )

    model._generate = patched.__get__(model, FakeChatModel)  # type: ignore[method-assign]
    answer = await generate_answer(model, history=[{"role": "user", "content": "hi"}])
    assert answer.text == "Hello world"
    assert original is not None


async def test_temperature_override_is_bound_not_mutated_globally() -> None:
    model = FakeChatModel(replies=["ok"])
    await generate_answer(model, history=[{"role": "user", "content": "hi"}], temperature=0.7)
    assert model.bound_kwargs == {"temperature": 0.7}


async def test_system_prompt_reaches_the_model() -> None:
    model = FakeChatModel(replies=["ok"])
    await generate_answer(
        model, history=[{"role": "user", "content": "hi"}], system_prompt="Grounding only."
    )
    assert model.received[0][0].content == "Grounding only."


@pytest.mark.parametrize(
    ("builder", "expected"),
    [
        (make_authentication_error, LlmAuthenticationError),
        (make_rate_limit_error, LlmRateLimitError),
        (make_timeout_error, LlmTimeoutError),
        (make_connection_error, LlmConnectionError),
        (make_status_error, LlmProviderError),
    ],
)
async def test_provider_errors_are_translated(builder, expected) -> None:
    model = FakeChatModel(error=builder())
    with pytest.raises(expected) as excinfo:
        await generate_answer(model, history=[{"role": "user", "content": "hi"}])

    error = excinfo.value
    assert isinstance(error, AppError)
    assert error.status_code == expected.status_code
    # The original exception must be chained for the logs.
    assert error.__cause__ is not None


# --------------------------------------------------------------------------
# map_exception
# --------------------------------------------------------------------------


def test_map_exception_passes_app_errors_through() -> None:
    original = LlmTimeoutError()
    assert map_exception(original) is original


def test_map_exception_checks_specific_subclasses_first() -> None:
    """AuthenticationError is an APIStatusError; it must not become a generic 502."""
    assert isinstance(map_exception(make_authentication_error()), LlmAuthenticationError)
    assert isinstance(map_exception(make_timeout_error()), LlmTimeoutError)


def test_map_exception_records_provider_status_but_not_body() -> None:
    error = map_exception(make_status_error(503))
    assert error.details["provider_status"] == 503
    assert "service unavailable" not in error.message


def test_exhausted_quota_is_not_reported_as_a_rate_limit() -> None:
    """Both arrive as HTTP 429, but retrying only helps one of them."""
    error = map_exception(make_quota_error())
    assert isinstance(error, LlmQuotaExhaustedError)
    assert error.code == "llm_quota_exhausted"
    assert error.details["provider_code"] == "credit_balance_exhausted"
    # The advice must point at billing, not at waiting.
    assert "retrying will not help" in error.message.lower()


def test_genuine_rate_limit_still_reports_as_rate_limited() -> None:
    error = map_exception(make_rate_limit_error())
    assert isinstance(error, LlmRateLimitError)
    assert not isinstance(error, LlmQuotaExhaustedError)
    assert error.code == "llm_rate_limited"


def test_unknown_exception_becomes_provider_error() -> None:
    assert isinstance(map_exception(RuntimeError("boom")), LlmProviderError)


def test_wrapped_provider_error_is_recovered_from_cause_chain() -> None:
    """A wrapper hiding the real provider error must not hide the diagnosis.

    Reproduces the real failure: LangChain's streaming accumulator replaces a
    failed request with "v2 stream finished without producing a message".
    """
    wrapper = RuntimeError("v2 stream finished without producing a message")
    wrapper.__cause__ = make_timeout_error()
    assert isinstance(map_exception(wrapper), LlmTimeoutError)


def test_wrapped_auth_error_is_recovered_from_context_chain() -> None:
    """__context__ works too, not just __cause__."""
    wrapper = RuntimeError("outer")
    wrapper.__context__ = make_authentication_error()
    assert isinstance(map_exception(wrapper), LlmAuthenticationError)


def test_cause_chain_does_not_loop_forever_on_cycles() -> None:
    first = RuntimeError("first")
    second = RuntimeError("second")
    first.__cause__ = second
    second.__cause__ = first
    assert isinstance(map_exception(first), LlmProviderError)


# --------------------------------------------------------------------------
# classify_intent (structured output)
# --------------------------------------------------------------------------


def _intent(**overrides) -> TaskIntent:
    payload = {
        "intent": "report",
        "requires_internal_documents": True,
        "requires_business_data": True,
        "requires_web_research": True,
        "complexity": "multi_step",
        "summary": "Analyse Q3 sales against strategy docs and market trends, then report.",
    }
    payload.update(overrides)
    return TaskIntent(**payload)


async def test_classify_intent_returns_validated_model() -> None:
    model = FakeChatModel(structured_result=_intent())
    result = await classify_intent(model, text="Analyze our Q3 sales performance")
    assert result.intent == "report"
    assert result.complexity == "multi_step"


async def test_classify_intent_accepts_plain_dict_from_tool_calling() -> None:
    """Some providers return a dict instead of a model instance."""
    model = FakeChatModel(structured_result=_intent().model_dump())
    result = await classify_intent(model, text="Analyze our Q3 sales performance")
    assert isinstance(result, TaskIntent)
    assert result.requires_web_research is True


async def test_classify_intent_rejects_invalid_dict() -> None:
    model = FakeChatModel(structured_result={"intent": "not-a-real-intent"})
    with pytest.raises(LlmStructuredOutputError):
        await classify_intent(model, text="hello")


async def test_classify_intent_translates_parser_errors() -> None:
    from langchain_core.exceptions import OutputParserException

    model = FakeChatModel(structured_error=OutputParserException("bad json"))
    with pytest.raises(LlmStructuredOutputError):
        await classify_intent(model, text="hello")


async def test_classify_intent_translates_provider_errors() -> None:
    model = FakeChatModel(structured_error=make_authentication_error())
    with pytest.raises(LlmAuthenticationError):
        await classify_intent(model, text="hello")
