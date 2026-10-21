"""Exercise Supabase Storage with the real client and two real signed-in users.

``tests/test_supabase_storage.py`` proves the client *builds* the right request.
This proves the bucket and its policies actually behave that way: the request
shapes are accepted, the caller's own token is sufficient, and a second user
cannot read, overwrite or delete the first user's object.

It also cleans up after itself. Storage objects have no foreign key to
``auth.users``, so deleting a throwaway user does **not** delete their files -
this script removes each key explicitly and then lists both prefixes to prove
nothing was left behind.

Run it from ``backend``:

    .\\.venv\\Scripts\\python.exe scripts\\check_storage.py

Exit codes: 0 = storage works and stays tenant-scoped, 1 = a check failed.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_rls import Client, Report  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.database.repositories import new_id  # noqa: E402
from app.security.errors import AccessDeniedError  # noqa: E402
from app.storage.base import storage_filename  # noqa: E402
from app.storage.errors import (  # noqa: E402
    FileStoreNotReadyError,
    FileStoreObjectMissingError,
)
from app.storage.supabase import SupabaseStorage  # noqa: E402

TIMEOUT = 30.0
PASSWORD = "throwaway-password-not-used-elsewhere"
# A space and a "#" on purpose: both must survive the URL, and "#" truncates a
# path into a fragment if the client forgets to encode it.
FILENAME = "q3 report #1.txt"
PAYLOAD = b"the vault code is seven-two-one"


def store_for(settings: Settings, token: str, http: httpx.Client) -> SupabaseStorage:
    return SupabaseStorage(
        base_url=settings.supabase_service_url("storage"),
        api_key=settings.supabase_anon_key.get_secret_value(),  # type: ignore[union-attr]
        access_token=token,
        client=http,
        bucket=settings.supabase_documents_bucket,
        timeout=TIMEOUT,
    )


def service_headers(settings: Settings) -> dict[str, str]:
    service = settings.supabase_service_role_key.get_secret_value()  # type: ignore[union-attr]
    return {
        "apikey": settings.supabase_anon_key.get_secret_value(),  # type: ignore[union-attr]
        "Authorization": f"Bearer {service}",
    }


def list_objects(settings: Settings, http: httpx.Client, prefix: str) -> list[dict]:
    response = http.post(
        f"{settings.supabase_service_url('storage')}/object/list/"
        f"{settings.supabase_documents_bucket}",
        headers=service_headers(settings),
        json={"prefix": prefix, "limit": 100, "offset": 0},
    )
    response.raise_for_status()
    body = response.json()
    return body if isinstance(body, list) else []


def main() -> int:
    settings = Settings()
    setup_logging(settings)
    print("=" * 70)
    print("Supabase Storage - live check")
    print("=" * 70)

    if not settings.supabase_configured:
        print("FAILED: SUPABASE_URL and SUPABASE_ANON_KEY must be set in .env")
        return 1
    if not settings.supabase_admin_configured:
        print("FAILED: SUPABASE_SERVICE_ROLE_KEY is needed to create throwaway users")
        return 1

    report = Report()
    client = Client(settings)
    http = httpx.Client(timeout=TIMEOUT)

    alice_id = bob_id = ""
    alice_token = bob_token = ""
    object_key: str | None = None
    document_id = new_id()

    try:
        print("\n1) Two throwaway users")
        print("-" * 70)
        suffix = uuid.uuid4().hex[:10]
        alice_id = client.create_user(f"storage-alice-{suffix}@example.com", PASSWORD)
        bob_id = client.create_user(f"storage-bob-{suffix}@example.com", PASSWORD)
        alice_token = client.sign_in(f"storage-alice-{suffix}@example.com", PASSWORD)
        bob_token = client.sign_in(f"storage-bob-{suffix}@example.com", PASSWORD)
        report.check(bool(alice_id and bob_id), "both users exist and can sign in")

        alice_store = store_for(settings, alice_token, http)
        bob_store = store_for(settings, bob_token, http)

        print("\n2) A uploads and reads back")
        print("-" * 70)
        try:
            object_key = alice_store.put(
                owner_id=alice_id,
                document_id=document_id,
                filename=FILENAME,
                data=PAYLOAD,
                content_type="text/plain",
            )
            report.ok(f"upload accepted and keyed at {object_key}")
        except FileStoreNotReadyError:
            report.fail("the documents bucket does not exist")
            report.fail("apply supabase/migrations/0002_storage_bucket.sql")
            return 1
        except Exception as exc:  # noqa: BLE001 - report, do not traceback
            report.fail("A could not upload the object", repr(exc))
            return 1

        report.check(
            object_key == f"{alice_id}/{document_id}/{storage_filename(FILENAME)}",
            "the key is <owner>/<document>/<sanitised filename>",
            str(object_key),
        )

        try:
            stored = alice_store.read(owner_id=alice_id, document_id=document_id, filename=FILENAME)
            report.check(stored == PAYLOAD, "A reads back exactly the bytes uploaded")
        except Exception as exc:  # noqa: BLE001
            report.fail("A could not read their own object", repr(exc))

        print("\n3) B is refused")
        print("-" * 70)
        try:
            bob_store.read(owner_id=alice_id, document_id=document_id, filename=FILENAME)
            report.fail("B read A's object")
        except FileStoreObjectMissingError:
            # RLS makes the row invisible, so it is indistinguishable from
            # absence. That is the intended answer, not a leakage of existence.
            report.ok("B cannot read A's object (RLS hides it: not found)")
        except AccessDeniedError:
            report.ok("B cannot read A's object (403 from the policy)")
        except Exception as exc:  # noqa: BLE001
            report.fail("B's read failed in an unexpected way", repr(exc))

        try:
            bob_store.put(
                owner_id=alice_id,
                document_id=document_id,
                filename="planted.txt",
                data=b"overwritten",
                content_type="text/plain",
            )
            report.fail("B uploaded into A's folder")
        except AccessDeniedError:
            report.ok("B cannot write into A's folder")
        except Exception as exc:  # noqa: BLE001
            report.fail("B's write failed in an unexpected way", repr(exc))

        # Supabase may answer a foreign DELETE with 200 (zero rows removed) or
        # with a refusal. Either is safe only if the object is still there; the
        # read below is the assertion that matters.
        try:
            bob_store.delete(owner_id=alice_id, document_id=document_id, filename=FILENAME)
        except (AccessDeniedError, FileStoreObjectMissingError):
            pass
        except Exception as exc:  # noqa: BLE001
            report.fail("B's delete failed in an unexpected way", repr(exc))

        try:
            still_there = alice_store.read(
                owner_id=alice_id, document_id=document_id, filename=FILENAME
            )
            report.check(still_there == PAYLOAD, "B's delete did not remove A's object")
        except Exception as exc:  # noqa: BLE001
            report.fail("A's object disappeared after B tried to delete it", repr(exc))

        print("\n4) A deletes, and the object is gone")
        print("-" * 70)
        try:
            alice_store.delete(owner_id=alice_id, document_id=document_id, filename=FILENAME)
            report.ok("A deleted the object")
        except Exception as exc:  # noqa: BLE001
            report.fail("A could not delete their object", repr(exc))

        try:
            alice_store.read(owner_id=alice_id, document_id=document_id, filename=FILENAME)
            report.fail("the object is still readable after deletion")
        except FileStoreObjectMissingError:
            report.ok("reading it afterwards reports a missing object")
        except Exception as exc:  # noqa: BLE001
            report.fail("the post-delete read failed in an unexpected way", repr(exc))

        try:
            alice_store.delete(owner_id=alice_id, document_id=document_id, filename=FILENAME)
            report.ok("deleting it again is a no-op, not an error")
        except Exception as exc:  # noqa: BLE001
            report.fail("a repeated delete failed", repr(exc))

        print("\n5) The public anon key reaches nothing")
        print("-" * 70)
        anon_store = store_for(
            settings,
            settings.supabase_anon_key.get_secret_value(),
            http,  # type: ignore[union-attr]
        )
        try:
            anon_store.read(owner_id=alice_id, document_id=document_id, filename=FILENAME)
            report.fail("the anon key read an object")
        except (AccessDeniedError, FileStoreObjectMissingError):
            report.ok("the anon key cannot read the object")

    except Exception as exc:  # noqa: BLE001 - report, do not traceback at the user
        report.fail("the check could not finish", repr(exc))

    finally:
        print("\n6) Cleanup")
        print("-" * 70)
        if object_key:
            # Explicit, because deleting the user does not touch storage.
            http.delete(
                f"{settings.supabase_service_url('storage')}/object/"
                f"{quote(settings.supabase_documents_bucket, safe='')}/"
                f"{quote(object_key, safe='/')}",
                headers=service_headers(settings),
            )
        for label, user_id in (("A", alice_id), ("B", bob_id)):
            if not user_id:
                continue
            try:
                client.delete_user(user_id)
                print(f"  deleted user {label} ({user_id[:8]}...)")
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask the result
                print(f"  [warn] could not delete user {label}: {exc!r}")

        for label, owner in (("A", alice_id), ("B", bob_id)):
            if not owner:
                continue
            try:
                leftover = list_objects(settings, http, owner)
            except Exception as exc:  # noqa: BLE001
                report.fail(f"could not list {label}'s prefix to verify cleanup", repr(exc))
                continue
            report.check(
                not leftover,
                f"no objects remain under {label}'s prefix",
                str(leftover)[:200],
            )

        http.close()
        client.close()

    print()
    if report.failed:
        print(
            f"RESULT: FAILED - {len(report.failed)} of {report.passed + len(report.failed)} checks"
        )
        for message in report.failed:
            print(f"  - {message}")
        return 1
    print(f"RESULT: OK - {report.passed} checks passed. Storage is live and tenant-scoped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
