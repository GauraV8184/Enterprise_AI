"""Detect and neutralise instructions hidden in untrusted text.

Prompts already label untrusted content (``<context>``, ``<evidence>``,
``<web_results>``, ``<rows>``) and the system prompts state that labelled
content is data. That structure is the primary defence. This module adds two
supporting pieces:

* **Neutralisation.** A document can contain the literal closing tag of the block
  it is embedded in (``</context>``). That ends the data block early and promotes
  the rest of the document to instructions. The block tags this application
  formats with are HTML-escaped inside untrusted text, so a block cannot be
  closed from within.

* **Detection.** A small, conservative set of phrases that are hard to explain as
  ordinary business prose. A hit is logged as ``possible_prompt_injection`` with
  the source and the marker names - never the text - and does not block the
  request. Blocking on a phrase is easy to evade and would silently drop
  legitimate documents; the log is an operator signal, and the structural
  controls (permissions, the SQL allowlist) are what bound the damage.
"""

from __future__ import annotations

import re

from app.core.logging import get_logger, safe_extra

logger = get_logger(__name__)

DELIMITER_TAGS = (
    "context",
    "documents",
    "analytics",
    "evidence",
    "web_results",
    "rows",
    "schema",
    "query",
)

_DELIMITER_PATTERN = re.compile(
    r"</?(?:" + "|".join(DELIMITER_TAGS) + r")\s*>",
    re.IGNORECASE,
)

#: ``(name, pattern)`` pairs. Names are logged; patterns never leave the process.
_INJECTION_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore_prior_instructions",
        re.compile(
            r"\b(?:ignore|disregard|forget)\b[^.\n]{0,40}"
            r"\b(?:previous|prior|above|earlier|preceding)\b[^.\n]{0,20}"
            r"\b(?:instructions?|rules?|directions?|prompts?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "reveal_prompt",
        re.compile(
            r"\b(?:reveal|show|print|repeat|leak|output)\b[^.\n]{0,30}"
            r"\b(?:system\s+prompt|hidden\s+prompt|instructions?|prompt)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role_change",
        re.compile(
            r"\b(?:you\s+are\s+now|from\s+now\s+on,?\s+you\s+(?:are|will|must))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role_spoof",
        re.compile(r"</?\s*(?:system|assistant|developer|prompt)\b", re.IGNORECASE),
    ),
    (
        "credential_exfiltration",
        re.compile(
            r"\b(?:send|post|upload|email|forward|include)\b[^.\n]{0,40}"
            r"\b(?:api[\s_-]?key|access[\s_-]?token|password|secret|credentials?)\b",
            re.IGNORECASE,
        ),
    ),
    ("delimiter_escape", _DELIMITER_PATTERN),
)


def scan_for_injection(text: str, *, source: str) -> list[str]:
    """Log any known injection markers in ``text`` and return their names.

    The text itself is never logged: an injected document may also contain
    tenant data, and the point of the signal is the pattern, not the payload.
    """
    if not text:
        return []

    markers = [name for name, pattern in _INJECTION_MARKERS if pattern.search(text)]
    if markers:
        logger.warning(
            "possible_prompt_injection",
            extra=safe_extra(source=source, markers=markers, characters=len(text)),
        )
    return markers


def neutralize_delimiters(text: str) -> str:
    """Escape this application's own block tags inside untrusted text."""

    def escape(match: re.Match[str]) -> str:
        return match.group(0).replace("<", "&lt;").replace(">", "&gt;")

    return _DELIMITER_PATTERN.sub(escape, text)


def prepare_untrusted_text(text: str, *, source: str) -> str:
    """Scan untrusted text for injection, then make its delimiters inert.

    Call this at the point text is embedded in a prompt: the scan sees the raw
    source, and the model sees a block it cannot be talked out of.
    """
    scan_for_injection(text, source=source)
    return neutralize_delimiters(text)
