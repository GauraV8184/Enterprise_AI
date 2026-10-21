"""Logging: secret redaction and request-id propagation."""

from __future__ import annotations

import json
import logging
import sys

from app.core.logging import HumanFormatter, JsonFormatter, redact_secrets, redact_text


def _record(message: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("test.logger", logging.INFO, __file__, 1, message, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_sensitive_keys_are_redacted_recursively() -> None:
    redacted = redact_secrets(
        {
            "supabase_db_url": "postgres://user:secret@host/db",
            "nested": {"api_key": "sk-not-a-real-key", "safe": "value"},
            "items": [{"token": "abc123"}],
        }
    )

    assert redacted["supabase_db_url"] == "***REDACTED***"
    assert redacted["nested"]["api_key"] == "***REDACTED***"
    assert redacted["nested"]["safe"] == "value"
    assert redacted["items"][0]["token"] == "***REDACTED***"


def test_credentials_embedded_in_strings_are_redacted() -> None:
    text = redact_text(
        "Authorization: Bearer abc123 token=def456 sk-abcdefghijklmnop "
        "eyJabcdefgh.abcdefgh.abcdefgh"
    )

    assert "abc123" not in text
    assert "def456" not in text
    assert "sk-abcdefghijklmnop" not in text
    assert "eyJabcdefgh" not in text


def test_json_formatter_redacts_message_extras_and_exceptions() -> None:
    try:
        raise RuntimeError("failed with Authorization: Bearer sk-secret12345")
    except RuntimeError:
        exc_info = sys.exc_info()

    record = _record(
        "provider call failed: api_key=sk-secret12345",
        url="https://gateway.example/v1?token=abc123",
        exc_info=exc_info,
    )

    payload = json.loads(JsonFormatter().format(record))
    serialized = json.dumps(payload)

    assert "sk-secret12345" not in serialized
    assert "abc123" not in serialized
    assert "***REDACTED***" in payload["message"]
    assert "***REDACTED***" in payload["url"]
    assert "***REDACTED***" in payload["exception"]


def test_human_formatter_redacts_too() -> None:
    record = _record("provider call failed: token=abc123")

    text = HumanFormatter().format(record)

    assert "abc123" not in text
    assert "***REDACTED***" in text
