"""Structured logging configuration.

Two formatters are provided:

* :class:`JsonFormatter` - one JSON object per line. This is what production log
  aggregators expect, and it is the default when ``LOG_JSON=true``.
* :class:`HumanFormatter` - readable text for local development.

Every record can carry a ``request_id`` so a single request can be traced
through all of its log lines. The id lives in a :class:`contextvars.ContextVar`
so it is correctly isolated per request/task, even under async concurrency.

Never log secrets. The redaction helpers below are a safety net for the most
common mistake (logging a settings object or an auth header verbatim), not a
license to log credentials.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings

# Correlation id for the current request; "-" when outside a request context.
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

# Attribute names that are part of every LogRecord and must not be treated as
# user-supplied "extra" fields when building the JSON payload.
_RESERVED_LOG_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()) | {
    "message",
    "asctime",
    "taskName",
}

# A key is sensitive when one of its underscore/dash separated words is in this
# set. Matching whole words (rather than substrings) avoids redacting legitimate
# fields such as ``total_tokens`` or ``max_tokens`` while still catching
# ``access_token`` and ``openai_api_key``.
_SENSITIVE_WORDS = frozenset(
    {
        "key",
        "keys",
        "secret",
        "secrets",
        "password",
        "passwd",
        "token",
        "tokens",
        "credential",
        "credentials",
        "authorization",
        "cookie",
        "signature",
        "private",
    }
)

_WORD_SPLIT = re.compile(r"[^a-z0-9]+")


def is_sensitive_key(name: str) -> bool:
    """True when a field name looks like it holds a secret."""
    words = [word for word in _WORD_SPLIT.split(str(name).lower()) if word]
    return any(word in _SENSITIVE_WORDS for word in words)


def redact_secrets(value: Any) -> Any:
    """Recursively redact values held under suspicious-looking keys."""
    if isinstance(value, dict):
        return {
            k: ("***REDACTED***" if is_sensitive_key(str(k)) else redact_secrets(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_secrets(item) for item in value]
    return value


def safe_extra(**fields: Any) -> dict[str, Any]:
    """Prepare keyword fields for ``logger.*(..., extra=...)``.

    Python's logging raises ``KeyError`` when ``extra`` contains a name that
    collides with a ``LogRecord`` attribute. Because that happens *inside* the
    logging call, it can turn an already-handled error into an unhandled crash.
    This helper renames collisions with a ``ctx_`` prefix (keeping the data
    rather than dropping it) and redacts anything that looks sensitive.

    Prefer it over a bare dict for every structured log call::

        logger.info("thing_happened", extra=safe_extra(user_id=1, message="hi"))
    """
    cleaned: dict[str, Any] = {}
    for name, value in fields.items():
        key = f"ctx_{name}" if name in _RESERVED_LOG_ATTRS else name
        cleaned[key] = value
    return redact_secrets(cleaned)


class JsonFormatter(logging.Formatter):
    """Render each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_ctx.get(),
        }

        # Merge any structured fields passed via ``logger.info(..., extra={...})``.
        for key, value in record.__dict__.items():
            if key not in _RESERVED_LOG_ATTRS and not key.startswith("_"):
                payload[key] = redact_secrets(value)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    """Readable single-line format for local development."""

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{datetime.fromtimestamp(record.created, tz=UTC).strftime('%H:%M:%S')} "
            f"{record.levelname:<8} {record.name} "
            f"[{request_id_ctx.get()}] {record.getMessage()}"
        )
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def setup_logging(settings: Settings) -> None:
    """Configure the root logger. Safe to call more than once."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if settings.log_json else HumanFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # Align uvicorn's own loggers with our level and stop them double-logging
    # through their default handlers.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger."""
    return logging.getLogger(name)
