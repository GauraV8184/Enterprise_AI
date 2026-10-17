"""Integration tests that talk to the real LLM provider.

Skipped by default: they cost money and need credentials. Run explicitly with

    $env:RUN_LIVE_LLM_TESTS="1"; python -m pytest tests/integration -v

Requires a valid OPENAI_API_KEY in the repository-root .env file.
"""

from __future__ import annotations

import os

import pytest

from app.core.config import Settings
from app.llm.errors import LlmNotConfiguredError
from app.llm.factory import build_chat_model
from app.llm.schemas import TaskIntent
from app.llm.service import classify_intent, generate_answer

pytestmark = pytest.mark.integration

_RUN_LIVE = os.getenv("RUN_LIVE_LLM_TESTS") == "1"

skip_reason = (
    "Set RUN_LIVE_LLM_TESTS=1 to run live LLM tests "
    "(requires a valid OPENAI_API_KEY in the repo-root .env)."
)


@pytest.fixture(scope="module")
def live_settings() -> Settings:
    settings = Settings()
    if not settings.llm_configured:
        pytest.skip("OPENAI_API_KEY is not configured in .env")
    return settings


@pytest.fixture(scope="module")
def live_model(live_settings: Settings):
    try:
        return build_chat_model(live_settings)
    except LlmNotConfiguredError:  # pragma: no cover - defensive
        pytest.skip("LLM provider is not configured")


@pytest.mark.skipif(not _RUN_LIVE, reason=skip_reason)
async def test_live_chat_completion(live_model) -> None:
    """A real completion request must return non-empty text."""
    answer = await generate_answer(
        live_model,
        history=[{"role": "user", "content": "Reply with exactly the word: pong"}],
        system_prompt="You are a test harness. Follow the instruction literally.",
        temperature=0.0,
    )
    assert answer.text.strip(), "provider returned an empty completion"
    assert answer.usage is not None, "provider did not report token usage"


@pytest.mark.skipif(not _RUN_LIVE, reason=skip_reason)
async def test_live_structured_output(live_model) -> None:
    """The provider must honour the structured-output schema."""
    result = await classify_intent(
        live_model,
        text=(
            "Analyze our Q3 sales performance, compare the results with our internal "
            "sales strategy documents, research relevant market trends, identify "
            "possible reasons for changes in sales, and generate a management report."
        ),
    )
    assert isinstance(result, TaskIntent)
    assert result.intent in {"report", "data_analysis"}
    assert result.complexity == "multi_step"
