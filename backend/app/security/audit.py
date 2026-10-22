"""The audit trail: durable records of the actions that matter.

Every request already produces a structured log line (``request_completed``
with method, path, status and request id). That is enough to debug a request,
but it is not an audit trail: log retention is an infrastructure setting, and a
user cannot read their own history from it. Actions that change data or produce
a lasting artefact are therefore also appended to ``audit_events``.

**Failures never change the outcome.** The action has already happened by the
time it is recorded. If the insert fails, turning the request into a 500 would
not undo it; it would only add an outage to a logging problem. The failure is
logged loudly with the request id, and the response is returned unchanged. The
trade-off is stated rather than hidden: the write path is best-effort, and an
``audit_event_not_recorded`` line is the signal that the trail has a gap.

**What is recorded.** The actor, the action verb, the resource type and id, the
request id, and a small detail object: counts, route names, grounded flags, and
the requested sources. Never document contents, prompts, answers, or
credentials. ``request_id`` ties the record back to the log line for the same
request, which is where the non-audited detail lives.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger, request_id_ctx, safe_extra
from app.database.repositories import DocumentStore
from app.security.identity import UserIdentity

logger = get_logger(__name__)


def record_event(
    repository: DocumentStore,
    *,
    user: UserIdentity,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one audit event, never raising.

    The store is passed in rather than resolved here so the caller uses the
    same per-request, caller-scoped store every other operation uses. On
    Supabase that token is what lets ``record_audit_event()`` attribute the
    event to the right actor; nothing about the identity is taken on trust.
    """
    try:
        repository.record_audit_event(
            owner_id=user.user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=request_id_ctx.get(),
            detail=detail or {},
        )
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        logger.warning(
            "audit_event_not_recorded",
            extra=safe_extra(action=action, error_type=type(exc).__name__),
        )
