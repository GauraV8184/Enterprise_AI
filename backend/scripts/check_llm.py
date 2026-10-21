"""Diagnostic: check that the configured LLM provider actually works.

Run this before starting the server when you add or rotate a key:

    cd backend
    .\\.venv\\Scripts\\python.exe scripts\\check_llm.py

It exercises the same code path the API uses (``app.llm.service``), so a green
result means the chat endpoint will work. It never prints the API key.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.llm.errors import LlmNotConfiguredError  # noqa: E402
from app.llm.factory import build_chat_model  # noqa: E402
from app.llm.service import classify_intent, generate_answer  # noqa: E402


def _line(label: str, value: object) -> None:
    print(f"  {label:<26} {value}")


async def main() -> int:
    settings = get_settings()

    print("\nLLM configuration")
    print("-" * 60)
    _line("provider", settings.llm_provider)
    _line("model", settings.openai_model)
    if settings.llm_provider == "ollama":
        _line("api key", "not required for a local server")
    else:
        _line("api key present", settings.openai_api_key is not None)
    _line("endpoint", settings.effective_base_url or "(provider default: api.openai.com)")
    _line("timeout (s)", settings.llm_timeout_seconds)
    _line("max retries", settings.llm_max_retries)
    _line("environment", settings.environment)

    if not settings.llm_configured:
        print("\nRESULT: not configured.")
        print("  Add OPENAI_API_KEY to the .env file in the repository root, then retry.")
        print("  For a local Ollama server, use LLM_PROVIDER=ollama (no key needed).")
        return 1

    try:
        model = build_chat_model(settings)
    except LlmNotConfiguredError as exc:
        print(f"\nRESULT: cannot build client - {exc.message}")
        return 1

    print("\n1) Plain chat completion")
    print("-" * 60)
    try:
        answer = await generate_answer(
            model,
            history=[{"role": "user", "content": "Reply with exactly the word: pong"}],
            system_prompt="Follow the instruction literally. Reply with one word.",
            temperature=0.0,
        )
        _line("response", repr(answer.text[:120]))
        _line("finish reason", answer.finish_reason)
        _line("model reported", answer.model_name)
        _line("usage", answer.usage)
    except AppError as exc:
        print(f"\nRESULT: FAILED - {exc.code} (HTTP {exc.status_code})")
        print(f"  {exc.message}")
        return 1
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        print(f"\nRESULT: FAILED - unexpected {type(exc).__name__}: {exc}")
        return 1

    print("\n2) Structured output (intent classification)")
    print("-" * 60)
    try:
        intent = await classify_intent(
            model,
            text="Analyze our Q3 sales performance and generate a management report with charts.",
        )
        for field, value in intent.model_dump().items():
            _line(field, value)
    except AppError as exc:
        print(f"\nRESULT: FAILED - {exc.code} (HTTP {exc.status_code})")
        print(f"  {exc.message}")
        return 1
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        print(f"\nRESULT: FAILED - unexpected {type(exc).__name__}: {exc}")
        return 1

    print("\nRESULT: OK - the LLM provider is reachable and responding.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
