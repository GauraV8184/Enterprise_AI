"""Prove, against the live project, that the audit trail behaves.

The offline tests prove the application calls the store. Only the live project
can prove that the database attributes the event to the token rather than to a
payload, that one user cannot read another user's events, and that no
application role can edit or delete a recorded event.

What it checks, end to end:

  1. two throwaway users can be created and signed in
  2. ``record_audit_event()`` records the event with the caller's id as actor
  3. the caller can read their own event, with the detail intact
  4. a second user cannot read it, even with the exact request id
  5. a direct INSERT is refused (no grant, no insert policy)
  6. an UPDATE and a DELETE are refused, and the event is unchanged
  7. an anonymous caller cannot execute the function
  8. cleanup removes the events and the users

Run it from ``backend`` (migration 0007 must already be applied):

    .\\.venv\\Scripts\\python.exe scripts\\check_audit.py

Exit codes: 0 = every check passed, 1 = something failed or is not configured.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.database.postgrest import PostgrestClient  # noqa: E402
from app.database.supabase_repository import SupabaseDocumentRepository  # noqa: E402
from scripts.check_analytics import Report, error_body  # noqa: E402

TIMEOUT = 30.0
ACTION = "security.check"
TABLE = "audit_events"


class Client:
    """Admin, auth and REST calls, with the same header split the app uses."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._anon = settings.supabase_anon_key.get_secret_value()
        self._service = (
            settings.supabase_service_role_key.get_secret_value()
            if settings.supabase_service_role_key
            else ""
        )
        self._http = httpx.Client(timeout=TIMEOUT)

    def close(self) -> None:
        self._http.close()

    def _url(self, service: str, path: str) -> str:
        return f"{self._settings.supabase_service_url(service)}{path}"  # type: ignore[arg-type]

    def _headers(self, token: str | None) -> dict[str, str]:
        return {
            "apikey": self._anon,
            "Authorization": f"Bearer {token or self._anon}",
            "Content-Type": "application/json",
        }

    def _service_headers(self) -> dict[str, str]:
        return {
            "apikey": self._service,
            "Authorization": f"Bearer {self._service}",
            "Content-Type": "application/json",
        }

    def create_user(self, email: str, password: str) -> str:
        response = self._http.post(
            self._url("auth", "/admin/users"),
            headers=self._service_headers(),
            json={"email": email, "password": password, "email_confirm": True},
        )
        response.raise_for_status()
        return response.json()["id"]

    def sign_in(self, email: str, password: str) -> str:
        response = self._http.post(
            self._url("auth", "/token?grant_type=password"),
            headers=self._headers(None),
            json={"email": email, "password": password},
        )
        response.raise_for_status()
        return response.json()["access_token"]

    def delete_user(self, user_id: str) -> None:
        response = self._http.delete(
            self._url("auth", f"/admin/users/{user_id}"),
            headers=self._service_headers(),
        )
        response.raise_for_status()

    def rpc(self, name: str, token: str | None, body: dict[str, Any]) -> httpx.Response:
        return self._http.post(
            self._url("rest", f"/rpc/{name}"),
            headers=self._headers(token),
            json=body,
        )

    def select(self, table: str, token: str | None, params: dict[str, str]) -> httpx.Response:
        return self._http.get(
            self._url("rest", f"/{table}"), headers=self._headers(token), params=params
        )

    def insert(self, table: str, token: str, row: dict[str, Any]) -> httpx.Response:
        return self._http.post(
            self._url("rest", f"/{table}"),
            headers={**self._headers(token), "Prefer": "return=representation"},
            json=row,
        )

    def patch(
        self, table: str, token: str, params: dict[str, str], body: dict[str, Any]
    ) -> httpx.Response:
        return self._http.patch(
            self._url("rest", f"/{table}"), headers=self._headers(token), params=params, json=body
        )

    def delete(self, table: str, token: str, params: dict[str, str]) -> httpx.Response:
        return self._http.delete(
            self._url("rest", f"/{table}"), headers=self._headers(token), params=params
        )

    def delete_service(self, table: str, params: dict[str, str]) -> None:
        response = self._http.delete(
            self._url("rest", f"/{table}"), headers=self._service_headers(), params=params
        )
        response.raise_for_status()

    def repository(self, token: str) -> SupabaseDocumentRepository:
        return SupabaseDocumentRepository(
            PostgrestClient(
                base_url=self._settings.supabase_service_url("rest"),
                api_key=self._anon,
                access_token=token,
                client=self._http,
            )
        )


def rows(response: httpx.Response) -> list[dict[str, Any]]:
    try:
        body = response.json()
    except ValueError:
        return []
    return body if isinstance(body, list) else []


def run(settings: Settings) -> int:
    report = Report()
    client = Client(settings)
    suffix = uuid.uuid4().hex[:10]
    marker = f"check-{suffix}"
    request_id = f"req-{marker}"
    password = f"audit-check-{suffix}"
    users: dict[str, str] = {}

    print("\nAudit configuration")
    print("-" * 62)
    print(f"  {'project':<22} {settings.supabase_url}")
    print(f"  {'table':<22} public.{TABLE}")
    print(f"  {'probe request id':<22} {request_id}")

    try:
        print("\n1) Create two throwaway users")
        print("-" * 62)
        for label in ("a", "b"):
            email = f"audit-check-{label}-{suffix}@example.com"
            users[label] = client.create_user(email, password)
            report.ok(f"created user {label} ({users[label][:8]}...)")

        tokens = {
            label: client.sign_in(f"audit-check-{label}-{suffix}@example.com", password)
            for label in users
        }
        for label in tokens:
            report.check(bool(tokens[label]), f"user {label} signed in and received a token")

        alice_token = tokens["a"]
        bob_token = tokens["b"]

        print("\n2) The RPC records the caller as the actor")
        print("-" * 62)
        try:
            # Deliberately pass the *other* user's id. The RPC must ignore it
            # and take the actor from the token; the application always passes
            # the caller's id, so this is the adversarial version of the call.
            client.repository(alice_token).record_audit_event(
                owner_id=users["b"],
                action=ACTION,
                resource_type="check",
                resource_id=marker,
                request_id=request_id,
                detail={"probe": marker, "purpose": "live audit check"},
            )
        except Exception as exc:  # noqa: BLE001 - a missing migration is a finding
            report.fail(
                "record_audit_event() failed (is migration 0007 applied and the schema cache reloaded?)",
                repr(exc),
            )
            return _finish(report)

        report.ok("recorded one event through the application's repository")

        print("\n3) The caller reads their own event")
        print("-" * 62)
        own = client.select(
            TABLE,
            alice_token,
            {
                "request_id": f"eq.{request_id}",
                "select": "actor_id,action,resource_type,resource_id,detail,created_at",
            },
        )
        own_rows = rows(own)
        report.check(
            own.status_code == 200 and len(own_rows) == 1,
            "the caller sees exactly the event they recorded",
            f"HTTP {own.status_code} {own.text[:200]}",
        )
        if own_rows:
            row = own_rows[0]
            report.check(
                row.get("actor_id") == users["a"],
                "the actor is the caller's user id, not a value from the payload",
                str(row.get("actor_id")),
            )
            report.check(
                row.get("action") == ACTION
                and row.get("resource_id") == marker
                and row.get("detail", {}).get("probe") == marker,
                "the action, resource and detail were stored intact",
                str(row),
            )
            report.check(
                bool(row.get("created_at")),
                "the event carries a server-side timestamp",
                str(row.get("created_at")),
            )

        print("\n4) Another user cannot read it")
        print("-" * 62)
        other = client.select(TABLE, bob_token, {"request_id": f"eq.{request_id}", "select": "id"})
        report.check(
            other.status_code == 200 and rows(other) == [],
            "a second user sees zero rows, even with the exact request id",
            f"HTTP {other.status_code} {other.text[:200]}",
        )

        print("\n5) A direct INSERT is refused")
        print("-" * 62)
        forged = client.insert(
            TABLE,
            alice_token,
            {
                "actor_id": users["a"],
                "action": "security.forged",
                "detail": {"probe": marker},
            },
        )
        report.check(
            forged.status_code >= 400,
            f"the audit table is not directly writable (HTTP {forged.status_code})",
            forged.text[:200],
        )

        print("\n6) The recorded event cannot be edited or deleted")
        print("-" * 62)
        updated = client.patch(
            TABLE,
            alice_token,
            {"request_id": f"eq.{request_id}"},
            {"detail": {"probe": "tampered"}},
        )
        report.check(
            updated.status_code >= 400,
            f"an UPDATE is refused (HTTP {updated.status_code})",
            updated.text[:200],
        )

        deleted = client.delete(TABLE, alice_token, {"request_id": f"eq.{request_id}"})
        report.check(
            deleted.status_code >= 400,
            f"a DELETE is refused (HTTP {deleted.status_code})",
            deleted.text[:200],
        )

        after = client.select(
            TABLE, alice_token, {"request_id": f"eq.{request_id}", "select": "detail"}
        )
        after_rows = rows(after)
        report.check(
            len(after_rows) == 1 and after_rows[0].get("detail", {}).get("probe") == marker,
            "the event is unchanged after both attempts",
            str(after_rows),
        )

        print("\n7) An anonymous caller cannot record an event")
        print("-" * 62)
        anon = client.rpc(
            "record_audit_event",
            None,
            {"action": ACTION, "request_id": f"anon-{marker}"},
        )
        report.check(
            anon.status_code >= 400,
            f"anon is refused (HTTP {anon.status_code})",
            str(error_body(anon).get("message", anon.text[:160])),
        )
    finally:
        print("\n8) Cleanup")
        print("-" * 62)
        for label, user_id in users.items():
            try:
                client.delete_service(TABLE, {"actor_id": f"eq.{user_id}"})
                print(f"  deleted audit rows for user {label}")
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask the result
                report.fail(f"could not delete audit rows for user {label}", repr(exc))
            try:
                client.delete_user(user_id)
                print(f"  deleted user {label} ({user_id[:8]}...)")
            except Exception as exc:  # noqa: BLE001
                report.fail(f"could not delete user {label} during cleanup", repr(exc))
        client.close()

    return _finish(report)


def _finish(report: Report) -> int:
    print("\n" + "=" * 62)
    if report.failed:
        print(f"RESULT: FAILED - {report.passed} passed, {len(report.failed)} failed")
        for failure in report.failed:
            print(f"  - {failure}")
        return 1
    print(f"RESULT: OK - {report.passed} checks passed. The audit trail holds.")
    return 0


def main() -> int:
    settings = Settings()
    if not settings.supabase_configured or not settings.supabase_admin_configured:
        print("This check needs SUPABASE_URL, SUPABASE_ANON_KEY and SUPABASE_SERVICE_ROLE_KEY.")
        return 1
    return run(settings)


if __name__ == "__main__":
    raise SystemExit(main())
