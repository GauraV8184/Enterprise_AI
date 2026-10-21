"""The Supabase/Postgres document store.

Same contract as :class:`~app.database.repositories.DocumentRepository`, but the
two halves of the work are split differently on purpose:

* **Metadata** goes through PostgREST's table endpoints.
* **Similarity search** goes through ``match_document_chunks``, a SQL function
  that runs ``<=>`` against the HNSW index. Ranking therefore happens inside
  Postgres, next to the data, instead of downloading every vector to Python.

**Every call carries the caller's access token.** These methods take ``owner_id``
and filter on it, but that filter is a second line of defence: Row Level Security
has already restricted the visible rows, so a mistake here cannot widen access.
That ordering matters - if the two ever disagree, the database wins.

**Id translation.** Postgres uses dashed UUIDs; the API and the SQLite store use
32-character hex. :func:`_canonical_uuid` and :func:`_external_id` convert at the
boundary so callers never see the difference between backends.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import numpy as np

from app.core.errors import AppError
from app.core.logging import get_logger, safe_extra
from app.database.errors import (
    DocumentStoreError,
    EmbeddingDimensionMismatchError,
)
from app.database.postgrest import PostgrestClient, VectorDimensionError
from app.database.repositories import (
    STATUS_PENDING,
    ChunkMatch,
    ChunkRecord,
    ChunkSearchResult,
    DocumentRecord,
    new_id,
)

logger = get_logger(__name__)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_uuid(value: str) -> str | None:
    """Return the dashed form Postgres expects, or ``None`` if this is not a UUID.

    A malformed id has to read as "no such document". Letting it reach Postgres
    would produce a 400 about input syntax, which tells the caller their request
    was wrong when the right answer is simply 404.
    """
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


def _required_uuid(value: str) -> str:
    """Convert an id this module generated; failing loudly if it is not one."""
    canonical = _canonical_uuid(value)
    if canonical is None:
        raise ValueError(f"A store id must be a UUID, got {value!r}")
    return canonical


def _external_id(value: str) -> str:
    """Dashed UUID from PostgREST -> the 32-character hex the API exposes."""
    return str(value).replace("-", "")


def _as_pgvector(embedding: Sequence[float] | np.ndarray) -> str:
    """Serialise a vector in pgvector's text format.

    Sent as a string rather than a JSON array so inserts and the search RPC both
    go through pgvector's input function. One cast, exercised by both paths.

    ``format_float_positional(unique=True)`` emits the shortest text that parses
    back to the identical float32, so a stored vector is bit-for-bit what was
    computed. Fixed-width formatting would either pad it with noise or lose the
    last bit.
    """
    vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
    return (
        "["
        + ",".join(np.format_float_positional(value, unique=True, trim="-") for value in vector)
        + "]"
    )


def _to_document(row: dict[str, Any]) -> DocumentRecord:
    return DocumentRecord(
        id=_external_id(row["id"]),
        owner_id=str(row["owner_id"]),
        filename=row["filename"],
        content_type=row["content_type"],
        extension=row["extension"],
        size_bytes=int(row["size_bytes"]),
        status=row["status"],
        error=row.get("error"),
        page_count=row.get("page_count"),
        chunk_count=int(row.get("chunk_count") or 0),
        created_at=row["created_at"],
        indexed_at=row.get("indexed_at"),
    )


def _to_match(row: dict[str, Any]) -> ChunkMatch:
    return ChunkMatch(
        id=_external_id(row["id"]),
        document_id=_external_id(row["document_id"]),
        filename=row["filename"],
        content=row["content"],
        page=row.get("page"),
        section=row.get("section"),
        chunk_index=int(row["chunk_index"]),
        score=float(row["similarity"]),
    )


def _count_from_content_range(response: httpx.Response) -> int:
    """Read the total out of ``Content-Range: 0-0/42`` (or ``*/0`` when empty)."""
    _, _, total = response.headers.get("content-range", "").partition("/")
    try:
        return int(total)
    except ValueError:
        return 0


class SupabaseDocumentRepository:
    """Documents and vectors in Supabase, reached through PostgREST.

    Instances are request-scoped because they carry one caller's access token.
    """

    def __init__(self, client: PostgrestClient) -> None:
        self._client = client

    def create_document(
        self,
        *,
        owner_id: str,
        filename: str,
        content_type: str,
        extension: str,
        size_bytes: int,
        status: str = STATUS_PENDING,
    ) -> DocumentRecord:
        payload = {
            "id": _required_uuid(new_id()),
            "owner_id": owner_id,
            "filename": filename,
            "content_type": content_type,
            "extension": extension,
            "size_bytes": size_bytes,
            "status": status,
        }
        rows = self._client.post(
            "/documents",
            json=payload,
            headers={"Prefer": "return=representation"},
        ).json()
        return _to_document(rows[0])

    def update_status(
        self,
        *,
        owner_id: str,
        document_id: str,
        status: str,
        error: str | None = None,
        page_count: int | None = None,
        chunk_count: int | None = None,
        mark_indexed: bool = False,
    ) -> bool:
        """Move a document through its lifecycle. Returns False if not found.

        ``error`` is truncated for the same reason as in the SQLite store: provider
        messages can embed whole request bodies, and this string is shown to the
        user by the status endpoint.
        """
        document_uuid = _canonical_uuid(document_id)
        if document_uuid is None:
            return False

        payload: dict[str, Any] = {
            "status": status,
            "error": (error or None) and error[:1000],
        }
        if page_count is not None:
            payload["page_count"] = page_count
        if chunk_count is not None:
            payload["chunk_count"] = chunk_count
        if mark_indexed:
            payload["indexed_at"] = _utc_now()

        rows = self._client.patch(
            "/documents",
            params={"id": f"eq.{document_uuid}", "owner_id": f"eq.{owner_id}"},
            json=payload,
            headers={"Prefer": "return=representation"},
        ).json()
        return bool(rows)

    def get_document(self, *, owner_id: str, document_id: str) -> DocumentRecord | None:
        document_uuid = _canonical_uuid(document_id)
        if document_uuid is None:
            return None
        rows = self._client.get(
            "/documents",
            params={
                "select": "*",
                "id": f"eq.{document_uuid}",
                "owner_id": f"eq.{owner_id}",
                "limit": "1",
            },
        ).json()
        return _to_document(rows[0]) if rows else None

    def list_documents(self, *, owner_id: str) -> list[DocumentRecord]:
        rows = self._client.get(
            "/documents",
            params={
                "select": "*",
                "owner_id": f"eq.{owner_id}",
                "order": "created_at.desc",
            },
        ).json()
        return [_to_document(row) for row in rows]

    def delete_document(self, *, owner_id: str, document_id: str) -> bool:
        """Remove a document. Chunks go with it via ``ON DELETE CASCADE``.

        The cascade is a database constraint here, unlike the SQLite store where
        it depends on a connection pragma - so there is no orphaned-vector risk to
        work around.
        """
        document_uuid = _canonical_uuid(document_id)
        if document_uuid is None:
            return False
        rows = self._client.delete(
            "/documents",
            params={"id": f"eq.{document_uuid}", "owner_id": f"eq.{owner_id}"},
            headers={"Prefer": "return=representation"},
        ).json()
        return bool(rows)

    def add_chunks(self, chunks: Sequence[ChunkRecord]) -> int:
        """Insert chunks in one request, which PostgREST runs as one transaction.

        A partially indexed document would look ``ready`` while answering from only
        part of its content, so this must not be a loop of individual inserts.
        """
        if not chunks:
            return 0

        payload = [
            {
                "id": _required_uuid(chunk.id),
                "document_id": _required_uuid(chunk.document_id),
                "owner_id": chunk.owner_id,
                "chunk_index": chunk.chunk_index,
                "content": chunk.content,
                "page": chunk.page,
                "section": chunk.section,
                "embedding": _as_pgvector(chunk.embedding),
            }
            for chunk in chunks
        ]
        try:
            self._client.post("/chunks", json=payload)
        except VectorDimensionError as exc:
            width = int(np.asarray(chunks[0].embedding, dtype=np.float32).reshape(-1).size)
            raise self._dimension_mismatch(exc, query_dimensions=width) from exc
        return len(payload)

    def delete_document_chunks(self, *, owner_id: str, document_id: str) -> int:
        document_uuid = _canonical_uuid(document_id)
        if document_uuid is None:
            return 0
        rows = self._client.delete(
            "/chunks",
            params={"document_id": f"eq.{document_uuid}", "owner_id": f"eq.{owner_id}"},
            headers={"Prefer": "return=representation"},
        ).json()
        return len(rows)

    def count_chunks(self, *, owner_id: str, document_id: str | None = None) -> int:
        if document_id is not None:
            document_uuid = _canonical_uuid(document_id)
            if document_uuid is None:
                return 0
            return self._count_in_scope(owner_id=owner_id, scope=[document_uuid])
        return self._count_in_scope(owner_id=owner_id, scope=None)

    def search_chunks(
        self,
        *,
        owner_id: str,
        query_embedding: Sequence[float] | np.ndarray,
        top_k: int,
        document_ids: Sequence[str] | None = None,
        min_score: float = 0.1,
    ) -> ChunkSearchResult:
        """Rank the caller's chunks with pgvector.

        ``searched_chunks`` is a separate count rather than part of the RPC: the
        RPC's ``LIMIT`` has already thrown away everything below the cut, so it
        cannot report how large the corpus was.
        """
        vector = np.asarray(query_embedding, dtype=np.float32).reshape(-1)

        scope: list[str] | None = None
        if document_ids:
            scope = [
                canonical
                for canonical in (_canonical_uuid(value) for value in document_ids)
                if canonical is not None
            ]
            if not scope:
                return ChunkSearchResult(matches=[], searched_chunks=0)

        if top_k <= 0:
            return ChunkSearchResult(
                matches=[],
                searched_chunks=self._count_in_scope(
                    owner_id=owner_id, scope=scope, ready_only=True
                ),
            )

        payload = {
            "query_embedding": _as_pgvector(vector),
            "match_count": int(top_k),
            "min_similarity": float(min_score),
            "filter_document_ids": scope,
        }
        try:
            rows = self._client.post("/rpc/match_document_chunks", json=payload).json()
        except VectorDimensionError as exc:
            raise self._dimension_mismatch(exc, query_dimensions=int(vector.size)) from exc

        logger.info(
            "supabase_search_completed",
            extra=safe_extra(returned=len(rows), top_k=top_k, scoped=scope is not None),
        )
        return ChunkSearchResult(
            matches=[_to_match(row) for row in rows],
            searched_chunks=self._count_in_scope(owner_id=owner_id, scope=scope, ready_only=True),
        )

    def _count_in_scope(
        self, *, owner_id: str, scope: list[str] | None, ready_only: bool = False
    ) -> int:
        params = {"select": "id", "owner_id": f"eq.{owner_id}", "limit": "1"}
        if ready_only:
            params["select"] = "id,documents!inner(id)"
            params["documents.status"] = "eq.ready"
        if scope:
            params["document_id"] = f"in.({','.join(scope)})"
        response = self._client.get("/chunks", params=params, headers={"Prefer": "count=exact"})
        return _count_from_content_range(response)

    def _dimension_mismatch(
        self, exc: VectorDimensionError, *, query_dimensions: int
    ) -> EmbeddingDimensionMismatchError:
        """Turn pgvector's rejection into the 409 the caller can act on."""
        parsed = exc.dimensions()
        if parsed is not None:
            stored, supplied = parsed
            return EmbeddingDimensionMismatchError(
                stored_dimensions=stored, query_dimensions=supplied
            )

        stored = self._embedding_dimensions()
        if stored is None:
            raise DocumentStoreError() from exc
        return EmbeddingDimensionMismatchError(
            stored_dimensions=stored, query_dimensions=query_dimensions
        )

    def _embedding_dimensions(self) -> int | None:
        """Ask the database what width its ``embedding`` column is."""
        try:
            value = self._client.post("/rpc/embedding_dimensions").json()
        except AppError:
            return None
        return value if isinstance(value, int) else None
