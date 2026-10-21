"""Prove, against the live project, that one tenant cannot reach another's data.

Reading the policies in a migration tells you what they are *supposed* to do.
This script signs in as two real users and tries to break the isolation on
purpose, because the interesting failures - a policy that evaluates to true for
everyone, a grant that survived the migration, a function that runs as its owner
- all look fine in a diff.

What it checks, end to end:

  1. two throwaway users can be created and signed in
  2. user A can store a document and two vectors, and search them
  3. user B sees none of it: not in a listing, not by id, not by similarity
  4. user B cannot delete A's document
  5. user B cannot write a row *as* A
  6. user B cannot attach a chunk to A's document
  7. the public anon role reaches nothing at all
  8. the storage bucket refuses B a file uploaded by A

Every table row it creates is removed at the end, including on failure: the users
are deleted through the admin API, and the foreign keys cascade.

Run it from ``backend``:

    .\\.venv\\Scripts\\python.exe scripts\\check_rls.py

Exit codes: 0 = isolation holds, 1 = something leaked or errored.
"""

from __future__ import annotations

import json
import math
import random
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.core.config import Settings  # noqa: E402

TIMEOUT = 30.0
EMBEDDING_DIMS = 768


@dataclass
class Report:
    passed: int = 0
    failed: list[str] = field(default_factory=list)

    def ok(self, message: str) -> None:
        self.passed += 1
        print(f"  [pass] {message}")

    def fail(self, message: str, detail: str = "") -> None:
        self.failed.append(message)
        print(f"  [FAIL] {message}")
        if detail:
            print(f"         {detail[:200]}")

    def check(self, condition: bool, message: str, detail: str = "") -> bool:
        if condition:
            self.ok(message)
        else:
            self.fail(message, detail)
        return condition


class Client:
    """Thin wrapper over the three Supabase sub-services.

    The key in ``apikey`` and the token in ``Authorization`` are kept separate on
    purpose: it mirrors what the application does, and it means a test can act as
    a user while still presenting the public key, exactly like a browser would.
    """

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

    def _headers(self, token: str | None, *, prefer: str | None = None) -> dict[str, str]:
        headers = {
            "apikey": self._anon,
            "Authorization": f"Bearer {token or self._anon}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def create_user(self, email: str, password: str) -> str:
        response = self._http.post(
            self._url("auth", "/admin/users"),
            headers={"apikey": self._service, "Authorization": f"Bearer {self._service}"},
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
            headers={"apikey": self._service, "Authorization": f"Bearer {self._service}"},
        )
        response.raise_for_status()

    def rest(
        self,
        method: str,
        path: str,
        token: str | None = None,
        *,
        body: Any = None,
        prefer: str | None = None,
    ) -> httpx.Response:
        return self._http.request(
            method,
            self._url("rest", path),
            headers=self._headers(token, prefer=prefer),
            json=body,
        )

    def rpc(self, name: str, token: str | None, body: dict[str, Any]) -> httpx.Response:
        return self.rest("POST", f"/rpc/{name}", token, body=body)

    def upload(self, path: str, token: str, content: bytes, content_type: str) -> httpx.Response:
        return self._http.post(
            self._url("storage", f"/object/documents/{path}"),
            headers={
                "apikey": self._anon,
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type,
            },
            content=content,
        )

    def download(self, path: str, token: str) -> httpx.Response:
        return self._http.get(
            self._url("storage", f"/object/documents/{path}"),
            headers={"apikey": self._anon, "Authorization": f"Bearer {token}"},
        )

    def delete_object(self, path: str, token: str | None = None) -> httpx.Response:
        """Delete with the service key: used only for cleanup.

        Storage objects are not covered by ``on delete cascade`` from
        ``auth.users``, so a user deletion leaves their files behind. Tests that
        upload must remove the key explicitly.

        No ``Content-Type`` here: Storage's gateway rejects a DELETE that declares
        a JSON body and then sends none ("Body cannot be empty").
        """
        return self._http.delete(
            self._url("storage", f"/object/documents/{path}"),
            headers={
                "apikey": self._anon,
                "Authorization": f"Bearer {token or self._service}",
            },
        )


def unit_vector(seed: int, dims: int = EMBEDDING_DIMS) -> list[float]:
    """A deterministic unit-length vector, so similarity is predictable."""
    rng = random.Random(seed)
    raw = [rng.uniform(-1.0, 1.0) for _ in range(dims)]
    norm = math.sqrt(sum(value * value for value in raw))
    return [value / norm for value in raw]


def blend(
    primary: list[float], secondary: list[float], primary_weight: float, secondary_weight: float
) -> list[float]:
    """Combine two vectors and renormalise, giving a predictable cosine.

    Test data has to look like *real* embeddings or it measures the wrong thing.
    Two independent random 768-dimensional vectors are almost exactly orthogonal,
    so their cosine lands near zero and often just below the 0.0 relevance floor -
    which would make this test pass or fail on a coin flip driven by the floor
    rather than by tenant isolation.

    Once normalised, the cosine against ``primary`` is
    ``primary_weight / sqrt(primary_weight**2 + secondary_weight**2)``, so pass
    (0.8, 0.6) for ~0.8 - a real hit that still ranks below an exact match.
    """
    mixed = [
        primary_weight * a + secondary_weight * b for a, b in zip(primary, secondary, strict=True)
    ]
    norm = math.sqrt(sum(value * value for value in mixed))
    return [value / norm for value in mixed]


def as_pgvector(vector: list[float]) -> str:
    """Render a vector the way pgvector's text input expects it.

    Sending a JSON array and hoping for an implicit cast is the kind of thing
    that works in one client and not another; the text form is unambiguous.
    """
    return json.dumps(vector)


def error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("code", "")) if isinstance(body, dict) else ""


def run(settings: Settings) -> int:
    report = Report()
    client = Client(settings)
    suffix = uuid.uuid4().hex[:10]
    users: dict[str, str] = {}
    object_path: str | None = None
    password = f"rls-check-{suffix}"

    try:
        print("\n1) Create two throwaway users")
        print("-" * 62)
        for label in ("a", "b"):
            email = f"rls-check-{label}-{suffix}@example.com"
            users[label] = client.create_user(email, password)
            report.ok(f"created user {label} ({users[label][:8]}...)")

        tokens = {
            label: client.sign_in(f"rls-check-{label}-{suffix}@example.com", password)
            for label in users
        }
        for label in tokens:
            report.check(bool(tokens[label]), f"user {label} signed in and received a token")

        alice, bob = users["a"], users["b"]
        alice_token, bob_token = tokens["a"], tokens["b"]

        print("\n2) User A stores a document and two vectors")
        print("-" * 62)
        document_response = client.rest(
            "POST",
            "/documents",
            alice_token,
            body={
                "owner_id": alice,
                "filename": "alices-private-notes.txt",
                "content_type": "text/plain",
                "extension": ".txt",
                "size_bytes": 128,
                "status": "ready",
            },
            prefer="return=representation",
        )
        if not report.check(
            document_response.status_code == 201, "A created a document", document_response.text
        ):
            return _finish(report)
        document = document_response.json()[0]
        document_id = document["id"]

        near_vector = unit_vector(1)
        related_vector = blend(near_vector, unit_vector(2), 0.8, 0.6)
        chunks_response = client.rest(
            "POST",
            "/chunks",
            alice_token,
            body=[
                {
                    "document_id": document_id,
                    "owner_id": alice,
                    "chunk_index": 0,
                    "content": "The vault code is seven-two-one.",
                    "page": 1,
                    "embedding": as_pgvector(near_vector),
                },
                {
                    "document_id": document_id,
                    "owner_id": alice,
                    "chunk_index": 1,
                    "content": "Unrelated filler about the weather.",
                    "page": 2,
                    "embedding": as_pgvector(related_vector),
                },
            ],
            prefer="return=representation",
        )
        report.check(
            chunks_response.status_code == 201,
            "A stored two embedded chunks",
            chunks_response.text,
        )

        search = client.rpc(
            "match_document_chunks",
            alice_token,
            {"query_embedding": near_vector, "match_count": 5},
        )
        own_results = search.json() if search.status_code == 200 else []
        report.check(
            search.status_code == 200 and len(own_results) == 2,
            "A's own similarity search returns both chunks",
            search.text,
        )
        if own_results and isinstance(own_results, list):
            top = own_results[0]
            report.check(
                top["content"] == "The vault code is seven-two-one.",
                "the nearest chunk is the one the query vector matches",
                str(top),
            )
            report.check(
                float(top["similarity"]) > 0.999,
                f"self-similarity is ~1.0 (got {float(top['similarity']):.4f})",
            )
            report.check(
                top["filename"] == "alices-private-notes.txt",
                "the search result joined the parent filename",
            )
            if len(own_results) > 1:
                second = own_results[1]
                report.check(
                    0.7 < float(second["similarity"]) < 0.9,
                    "the second passage ranked below the first, as its vector implies "
                    f"(got {float(second['similarity']):.4f})",
                )

        print("\n3) User B tries to read A's data")
        print("-" * 62)
        listing = client.rest("GET", "/documents?select=*", bob_token)
        report.check(
            listing.status_code == 200 and listing.json() == [],
            "B's document listing is empty",
            listing.text,
        )

        by_id = client.rest("GET", f"/documents?id=eq.{document_id}&select=*", bob_token)
        report.check(
            by_id.status_code == 200 and by_id.json() == [],
            "B cannot fetch A's document by id",
            by_id.text,
        )

        chunks_by_id = client.rest(
            "GET", f"/chunks?document_id=eq.{document_id}&select=id", bob_token
        )
        report.check(
            chunks_by_id.status_code == 200 and chunks_by_id.json() == [],
            "B cannot read A's chunks",
            chunks_by_id.text,
        )

        search_as_b = client.rpc(
            "match_document_chunks",
            bob_token,
            {"query_embedding": near_vector, "match_count": 5},
        )
        report.check(
            search_as_b.status_code == 200 and search_as_b.json() == [],
            "B's similarity search returns nothing, even with A's exact query vector",
            search_as_b.text,
        )

        print("\n4) User B tries to destroy A's data")
        print("-" * 62)
        deleted = client.rest(
            "DELETE",
            f"/documents?id=eq.{document_id}",
            bob_token,
            prefer="return=representation",
        )
        report.check(
            deleted.status_code in (200, 204) and deleted.json() in ([], None),
            "B's delete matched zero rows",
            deleted.text,
        )
        still_there = client.rest("GET", f"/documents?id=eq.{document_id}&select=id", alice_token)
        report.check(
            still_there.status_code == 200 and len(still_there.json()) == 1,
            "A's document survived B's delete attempt",
            still_there.text,
        )

        print("\n5) User B tries to write rows attributed to A")
        print("-" * 62)
        impostor = client.rest(
            "POST",
            "/documents",
            bob_token,
            body={
                "owner_id": alice,
                "filename": "planted.txt",
                "content_type": "text/plain",
                "extension": ".txt",
                "size_bytes": 10,
            },
        )
        report.check(
            impostor.status_code == 403,
            "inserting a document owned by A is refused (403)",
            impostor.text,
        )

        print("\n6) User B tries to attach a chunk to A's document")
        print("-" * 62)
        hijack = client.rest(
            "POST",
            "/chunks",
            bob_token,
            body={
                "document_id": document_id,
                "owner_id": bob,
                "chunk_index": 0,
                "content": "injected by B",
                "embedding": as_pgvector(unit_vector(3)),
            },
        )
        report.check(
            hijack.status_code == 403,
            "attaching a chunk to someone else's document is refused (403)",
            hijack.text,
        )

        print("\n7) The public anon role gets nothing")
        print("-" * 62)
        for table in ("documents", "chunks"):
            anonymous = client.rest("GET", f"/{table}?select=*")
            report.check(
                anonymous.status_code in (401, 403) or error_code(anonymous) == "42501",
                f"anon cannot read '{table}'",
                anonymous.text,
            )

        print("\n8) Storage bucket isolation")
        print("-" * 62)
        object_path = f"{alice}/{document_id}/alices-private-notes.txt"
        payload = b"the vault code is seven-two-one"
        upload = client.upload(object_path, alice_token, payload, "text/plain")
        report.check(upload.status_code == 200, "A uploaded a file to the bucket", upload.text)

        own_download = client.download(object_path, alice_token)
        report.check(
            own_download.status_code == 200 and own_download.content == payload,
            "A can read their own file back",
            own_download.text[:200],
        )

        foreign_download = client.download(object_path, bob_token)
        report.check(
            foreign_download.status_code in (400, 403, 404),
            "B cannot download A's file",
            f"HTTP {foreign_download.status_code} {foreign_download.text[:160]}",
        )

    finally:
        print("\n9) Cleanup")
        print("-" * 62)
        if object_path:
            try:
                removed = client.delete_object(object_path)
                if removed.status_code < 400:
                    print(f"  deleted storage object {object_path}")
                else:
                    report.fail(
                        "could not delete the storage object during cleanup",
                        f"HTTP {removed.status_code} {removed.text[:160]}",
                    )
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask the result
                report.fail("could not delete the storage object during cleanup", repr(exc))
        for label, user_id in users.items():
            try:
                client.delete_user(user_id)
                print(f"  deleted user {label} ({user_id[:8]}...) and its rows")
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask the result
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
    print(f"RESULT: OK - {report.passed} checks passed. Tenant isolation holds.")
    return 0


def main() -> int:
    settings = Settings()
    if not settings.supabase_configured or not settings.supabase_admin_configured:
        print("This check needs SUPABASE_URL, SUPABASE_ANON_KEY and SUPABASE_SERVICE_ROLE_KEY.")
        return 1
    if settings.supabase_service_role_key is None:
        return 1
    return run(settings)


if __name__ == "__main__":
    raise SystemExit(main())
