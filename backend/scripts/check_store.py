"""Exercise the Supabase document store against the live project.

``tests/test_supabase_repository.py`` proves the store *asks for* the right rows.
This script proves something different and complementary: that PostgREST and
pgvector actually accept those requests, and that two real signed-in users cannot
see each other's documents through the repository itself.

The unit tests cannot catch the failures this is looking for - a JSON array where
pgvector wants a string, an RPC argument name that does not match the function,
``return=representation`` missing so an update looks like a no-op.

It creates two throwaway users, drives the store with their real access tokens,
and deletes them at the end (the foreign keys cascade, so their rows go too).

Run it from ``backend``:

    .\\.venv\\Scripts\\python.exe scripts\\check_store.py

Exit codes: 0 = the store works and stays tenant-scoped, 1 = a check failed.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import numpy as np  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_rls import Client, Report, blend, unit_vector  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.database.errors import EmbeddingDimensionMismatchError  # noqa: E402
from app.database.postgrest import PostgrestClient  # noqa: E402
from app.database.repositories import ChunkRecord, new_id  # noqa: E402
from app.database.supabase_repository import SupabaseDocumentRepository  # noqa: E402

TIMEOUT = 30.0
EMBEDDING_DIMS = 768
PASSWORD = "throwaway-password-not-used-elsewhere"


def repository_for(
    settings: Settings, token: str, http: httpx.Client
) -> SupabaseDocumentRepository:
    return SupabaseDocumentRepository(
        PostgrestClient(
            base_url=settings.supabase_service_url("rest"),
            api_key=settings.supabase_anon_key.get_secret_value(),  # type: ignore[union-attr]
            access_token=token,
            client=http,
        )
    )


def chunk_for(
    document_id: str, owner_id: str, *, content: str, vector, index: int = 0
) -> ChunkRecord:
    return ChunkRecord(
        id=new_id(),
        document_id=document_id,
        owner_id=owner_id,
        chunk_index=index,
        content=content,
        page=1,
        section=None,
        embedding=np.asarray(vector, dtype=np.float32),
    )


def looks_like_hex_id(value: str) -> bool:
    """32 hex characters: the shape the API promises, whatever the backend."""
    if len(value) != 32:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def run(settings: Settings, report: Report) -> None:
    client = Client(settings)
    http = httpx.Client(timeout=TIMEOUT)
    suffix = uuid.uuid4().hex[:10]
    email_a = f"store-check-a-{suffix}@example.com"
    email_b = f"store-check-b-{suffix}@example.com"
    users: list[str] = []

    try:
        user_a = client.create_user(email_a, PASSWORD)
        users.append(user_a)
        token_a = client.sign_in(email_a, PASSWORD)

        user_b = client.create_user(email_b, PASSWORD)
        users.append(user_b)
        token_b = client.sign_in(email_b, PASSWORD)

        repo_a = repository_for(settings, token_a, http)
        repo_b = repository_for(settings, token_b, http)

        leave_vector = unit_vector(seed=11)
        laptop_vector = blend(leave_vector, unit_vector(seed=12), 0.5, 0.5)

        leave_doc = repo_a.create_document(
            owner_id=user_a,
            filename="handbook.txt",
            content_type="text/plain",
            extension=".txt",
            size_bytes=2048,
        )
        report.check(looks_like_hex_id(leave_doc.id), "A can create a document")
        report.check(leave_doc.status == "pending", "a new document starts pending")

        laptop_doc = repo_a.create_document(
            owner_id=user_a,
            filename="laptops.txt",
            content_type="text/plain",
            extension=".txt",
            size_bytes=512,
        )

        inserted = repo_a.add_chunks(
            [
                chunk_for(
                    leave_doc.id, user_a, content="twenty days of annual leave", vector=leave_vector
                )
            ]
        )
        report.check(inserted == 1, "A can store a chunk")
        repo_a.add_chunks(
            [chunk_for(laptop_doc.id, user_a, content="full-disk encryption", vector=laptop_vector)]
        )
        report.check(
            repo_a.count_chunks(owner_id=user_a) == 2,
            "both chunks are countable",
        )

        try:
            repo_a.add_chunks(
                [
                    chunk_for(
                        leave_doc.id,
                        user_a,
                        content="wrong width",
                        vector=np.zeros(3, dtype=np.float32),
                        index=99,
                    )
                ]
            )
        except EmbeddingDimensionMismatchError as exc:
            report.ok(f"a wrong-width vector is a 409 ({exc.code})")
            report.check(
                "768" in exc.message and "3" in exc.message,
                "the error names both widths",
            )
        except Exception as exc:  # noqa: BLE001 - the point is to see what happened
            report.fail("a wrong-width vector raised the wrong error", repr(exc))
        else:
            report.fail("a 3-dimensional vector was accepted into a vector(768) column")
        report.check(
            repo_a.count_chunks(owner_id=user_a) == 2,
            "the rejected insert stored nothing",
        )

        updated = repo_a.update_status(
            owner_id=user_a,
            document_id=leave_doc.id,
            status="ready",
            chunk_count=1,
            mark_indexed=True,
        )
        record = repo_a.get_document(owner_id=user_a, document_id=leave_doc.id)
        report.check(updated and record is not None, "A can update and re-read a document")
        report.check(
            record is not None and record.status == "ready" and record.indexed_at is not None,
            "status and indexed_at round-trip",
        )
        report.check(
            record is not None and record.chunk_count == 1,
            "chunk_count round-trips",
        )

        pending = repo_a.search_chunks(owner_id=user_a, query_embedding=leave_vector, top_k=5)
        report.check(
            len(pending.matches) == 1 and pending.matches[0].document_id == leave_doc.id,
            "chunks of a document that is not ready are not searchable",
            detail=str([(m.filename, round(m.score, 4)) for m in pending.matches]),
        )
        report.check(
            pending.searched_chunks == 1,
            "searched_chunks counts only fully indexed documents",
        )

        repo_a.update_status(
            owner_id=user_a,
            document_id=laptop_doc.id,
            status="ready",
            chunk_count=1,
            mark_indexed=True,
        )

        listed = repo_a.list_documents(owner_id=user_a)
        report.check(
            {item.id for item in listed} == {leave_doc.id, laptop_doc.id},
            "A lists exactly their own two documents",
        )

        result = repo_a.search_chunks(owner_id=user_a, query_embedding=leave_vector, top_k=5)
        report.check(len(result.matches) == 2, "search returns every passage in scope")
        report.check(
            bool(result.matches) and result.matches[0].document_id == leave_doc.id,
            "the nearest passage is the one embedded from the same vector",
            detail=str([(m.filename, round(m.score, 4)) for m in result.matches]),
        )
        report.check(
            bool(result.matches) and result.matches[0].score > 0.99,
            "an identical vector scores ~1.0",
        )
        report.check(
            result.searched_chunks == 2,
            "searched_chunks reports the corpus size",
        )

        scoped = repo_a.search_chunks(
            owner_id=user_a,
            query_embedding=leave_vector,
            top_k=5,
            document_ids=[laptop_doc.id],
        )
        report.check(
            len(scoped.matches) == 1 and scoped.matches[0].document_id == laptop_doc.id,
            "document_ids narrows the search",
        )
        report.check(scoped.searched_chunks == 1, "the scope count follows the filter")

        report.check(
            repo_b.get_document(owner_id=user_b, document_id=leave_doc.id) is None,
            "B cannot read A's document by id",
        )
        report.check(
            repo_b.list_documents(owner_id=user_b) == [],
            "B's listing is empty",
        )
        b_search = repo_b.search_chunks(owner_id=user_b, query_embedding=leave_vector, top_k=5)
        report.check(
            b_search.matches == [] and b_search.searched_chunks == 0,
            "B's search reaches none of A's vectors",
            detail=f"{len(b_search.matches)} matches, {b_search.searched_chunks} in scope",
        )
        report.check(
            repo_b.delete_document(owner_id=user_b, document_id=leave_doc.id) is False,
            "B cannot delete A's document",
        )
        report.check(
            repo_a.get_document(owner_id=user_a, document_id=leave_doc.id) is not None,
            "A's document survived B's delete",
        )

        report.check(
            repo_a.get_document(owner_id=user_a, document_id="not-a-uuid") is None,
            "a malformed id is a 404, not a database error",
        )

        report.check(
            repo_a.delete_document(owner_id=user_a, document_id=leave_doc.id) is True,
            "A can delete their own document",
        )
        report.check(
            repo_a.count_chunks(owner_id=user_a) == 1,
            "deleting a document cascaded to its chunks",
        )
        report.check(
            repo_a.get_document(owner_id=user_a, document_id=leave_doc.id) is None,
            "the deleted document is gone",
        )

        repo_a.delete_document(owner_id=user_a, document_id=laptop_doc.id)
        report.check(repo_a.count_chunks(owner_id=user_a) == 0, "A's corpus is empty again")

    finally:
        http.close()
        for user_id in users:
            try:
                client.delete_user(user_id)
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask the result
                report.fail(f"could not delete throwaway user {user_id}", repr(exc))
        client.close()


def main() -> int:
    settings = Settings()
    setup_logging(settings)
    print("=" * 78)
    print("Supabase document store - live check")
    print("=" * 78)

    if not settings.supabase_configured:
        print("FAILED: SUPABASE_URL and SUPABASE_ANON_KEY must be set in .env")
        return 1
    if not settings.supabase_admin_configured:
        print("FAILED: SUPABASE_SERVICE_ROLE_KEY is needed to create the throwaway users")
        return 1

    report = Report()
    try:
        run(settings, report)
    except Exception as exc:  # noqa: BLE001 - report, do not traceback at the user
        report.fail("the check could not finish", repr(exc))

    print()
    if report.failed:
        print(
            f"RESULT: FAILED - {len(report.failed)} of {report.passed + len(report.failed)} checks"
        )
        for message in report.failed:
            print(f"  - {message}")
        return 1
    print(f"RESULT: OK - {report.passed} checks passed. The store works and stays scoped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
