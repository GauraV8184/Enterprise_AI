"""Shared store records plus the SQLite implementation.

This module holds three things that are not SQLite-specific:

* the dataclasses every store returns (:class:`DocumentRecord`,
  :class:`ChunkRecord`, :class:`ChunkMatch`, ...),
* the :class:`DocumentStore` protocol - the contract
  :class:`~app.database.supabase_repository.SupabaseDocumentRepository` also
  satisfies,
* and the SQLite store itself, which is still what runs on a developer machine.

**Why SQLite still exists.** It needs no account, no network and no migration, so
the whole pipeline stays testable offline. It is a development store, not a
production one.

**Why ranking is a store operation.** ``search_chunks`` takes an embedding and
returns ranked matches. On SQLite that means loading the corpus and comparing in
NumPy: fine for a local file, unacceptable over a network. On Postgres the same
call becomes one pgvector query. That is why the contract does not expose
``fetch_chunks`` - downloading every vector to rank it in Python would throw away
the entire point of the index.

**Tenant isolation.** Every method takes ``owner_id`` and filters on it in the
SQL. Cross-tenant reads return ``None``/empty rather than raising, so endpoints
can return 404 without revealing that a document exists for someone else. In the
Supabase store this filter is a *second* line of defence: Row Level Security
already decides which rows exist.

**Embeddings** are stored as raw float32 bytes. That keeps the schema portable
(Postgres becomes ``vector(768)``) and avoids JSON rounding noise.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import numpy as np

from app.core.logging import get_logger, safe_extra
from app.core.vectors import cosine_similarity_matrix
from app.database.errors import EmbeddingDimensionMismatchError

logger = get_logger(__name__)

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id            TEXT PRIMARY KEY,
    owner_id      TEXT NOT NULL,
    filename      TEXT NOT NULL,
    content_type  TEXT NOT NULL,
    extension     TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    status        TEXT NOT NULL,
    error         TEXT,
    page_count    INTEGER,
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    indexed_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_documents_owner
    ON documents (owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS chunks (
    id             TEXT PRIMARY KEY,
    document_id    TEXT NOT NULL,
    owner_id       TEXT NOT NULL,
    chunk_index    INTEGER NOT NULL,
    content        TEXT NOT NULL,
    page           INTEGER,
    section        TEXT,
    embedding      BLOB NOT NULL,
    embedding_dim  INTEGER NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chunks_owner
    ON chunks (owner_id, document_id);
"""


@dataclass(frozen=True)
class DocumentRecord:
    """A stored document's metadata. Never contains file contents."""

    id: str
    owner_id: str
    filename: str
    content_type: str
    extension: str
    size_bytes: int
    status: str
    error: str | None
    page_count: int | None
    chunk_count: int
    created_at: str
    indexed_at: str | None


@dataclass(frozen=True)
class ChunkRecord:
    """A chunk ready to be written (embedding included)."""

    id: str
    document_id: str
    owner_id: str
    chunk_index: int
    content: str
    page: int | None
    section: str | None
    embedding: np.ndarray


@dataclass(frozen=True)
class StoredChunk:
    """A chunk read back for retrieval, with its parent document's name."""

    id: str
    document_id: str
    filename: str
    content: str
    page: int | None
    section: str | None
    chunk_index: int


@dataclass(frozen=True)
class ChunkMatch:
    """One ranked passage returned by a similarity search."""

    id: str
    document_id: str
    filename: str
    content: str
    page: int | None
    section: str | None
    chunk_index: int
    score: float


@dataclass(frozen=True)
class ChunkSearchResult:
    """Ranked matches, plus how much of the corpus was in scope.

    ``searched_chunks`` is the number of the caller's chunks the query was run
    against, not the number of vectors an index happened to touch. pgvector's
    HNSW index is approximate, so those two numbers are not the same thing and
    reporting the internal one would imply an exactness search does not have.
    """

    matches: list[ChunkMatch]
    searched_chunks: int


def new_id() -> str:
    return uuid.uuid4().hex


def _utc_now() -> str:
    """Timestamps are stored as ISO-8601 UTC strings, matching Postgres ``timestamptz``."""
    return datetime.now(UTC).isoformat()


def _to_blob(embedding: np.ndarray) -> bytes:
    return np.asarray(embedding, dtype=np.float32).tobytes()


def _from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


class DocumentStore(Protocol):
    """The contract every store implements.

    Declared structurally, like :class:`app.security.identity.TokenVerifier`, so
    the retrieval and ingest layers depend on the behaviour without importing
    either implementation. ``fetch_chunks`` is deliberately absent: see the module
    docstring for why ranking is a store operation.
    """

    def create_document(
        self,
        *,
        owner_id: str,
        filename: str,
        content_type: str,
        extension: str,
        size_bytes: int,
        status: str = STATUS_PENDING,
    ) -> DocumentRecord: ...

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
    ) -> bool: ...

    def get_document(self, *, owner_id: str, document_id: str) -> DocumentRecord | None: ...

    def list_documents(self, *, owner_id: str) -> list[DocumentRecord]: ...

    def delete_document(self, *, owner_id: str, document_id: str) -> bool: ...

    def add_chunks(self, chunks: Sequence[ChunkRecord]) -> int: ...

    def delete_document_chunks(self, *, owner_id: str, document_id: str) -> int: ...

    def count_chunks(self, *, owner_id: str, document_id: str | None = None) -> int: ...

    def search_chunks(
        self,
        *,
        owner_id: str,
        query_embedding: Sequence[float] | np.ndarray,
        top_k: int,
        document_ids: Sequence[str] | None = None,
        min_score: float = 0.1,
    ) -> ChunkSearchResult: ...


class DocumentRepository:
    """Owner-scoped access to documents and vectors.

    Connections are opened per operation rather than held open. FastAPI runs sync
    endpoints in a threadpool, and SQLite connections are thread-bound by default;
    short-lived connections sidestep that entirely. SQLite handles this pattern
    well, and it is what makes the class safe to share across requests.
    """

    def __init__(self, database_path: Path) -> None:
        self._path = Path(database_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)
        logger.info("document_store_ready", extra=safe_extra(path=str(self._path)))

    @staticmethod
    def _row_to_document(row: sqlite3.Row) -> DocumentRecord:
        return DocumentRecord(
            id=row["id"],
            owner_id=row["owner_id"],
            filename=row["filename"],
            content_type=row["content_type"],
            extension=row["extension"],
            size_bytes=row["size_bytes"],
            status=row["status"],
            error=row["error"],
            page_count=row["page_count"],
            chunk_count=row["chunk_count"],
            created_at=row["created_at"],
            indexed_at=row["indexed_at"],
        )

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
        document_id = new_id()
        created_at = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO documents
                    (id, owner_id, filename, content_type, extension,
                     size_bytes, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    owner_id,
                    filename,
                    content_type,
                    extension,
                    size_bytes,
                    status,
                    created_at,
                ),
            )
        return DocumentRecord(
            id=document_id,
            owner_id=owner_id,
            filename=filename,
            content_type=content_type,
            extension=extension,
            size_bytes=size_bytes,
            status=status,
            error=None,
            page_count=None,
            chunk_count=0,
            created_at=created_at,
            indexed_at=None,
        )

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

        ``error`` is truncated because provider messages can embed whole request
        bodies; the status endpoint shows this string to the user.
        """
        assignments = ["status = ?", "error = ?"]
        params: list[object] = [status, (error or None) and error[:1000]]
        if page_count is not None:
            assignments.append("page_count = ?")
            params.append(page_count)
        if chunk_count is not None:
            assignments.append("chunk_count = ?")
            params.append(chunk_count)
        if mark_indexed:
            assignments.append("indexed_at = ?")
            params.append(_utc_now())
        params.extend([document_id, owner_id])

        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE documents SET {', '.join(assignments)} WHERE id = ? AND owner_id = ?",
                params,
            )
            return cursor.rowcount > 0

    def get_document(self, *, owner_id: str, document_id: str) -> DocumentRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE id = ? AND owner_id = ?",
                (document_id, owner_id),
            ).fetchone()
        return self._row_to_document(row) if row else None

    def list_documents(self, *, owner_id: str) -> list[DocumentRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM documents WHERE owner_id = ? ORDER BY created_at DESC",
                (owner_id,),
            ).fetchall()
        return [self._row_to_document(row) for row in rows]

    def delete_document(self, *, owner_id: str, document_id: str) -> bool:
        """Remove a document and every vector derived from it.

        Chunks are deleted explicitly rather than relying on ``ON DELETE
        CASCADE``, because ``PRAGMA foreign_keys`` is connection-scoped: if it
        were ever off, a cascade would silently leave orphaned vectors behind and
        those would keep surfacing in other users' search results.
        """
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM documents WHERE id = ? AND owner_id = ?",
                (document_id, owner_id),
            ).fetchone()
            if exists is None:
                return False

            connection.execute(
                "DELETE FROM chunks WHERE document_id = ? AND owner_id = ?",
                (document_id, owner_id),
            )
            connection.execute(
                "DELETE FROM documents WHERE id = ? AND owner_id = ?",
                (document_id, owner_id),
            )
        return True

    def add_chunks(self, chunks: Sequence[ChunkRecord]) -> int:
        """Insert chunks in one transaction.

        A single transaction matters: a partially indexed document would look
        ``ready`` while answering from only some of its content.
        """
        if not chunks:
            return 0

        rows = [
            (
                chunk.id,
                chunk.document_id,
                chunk.owner_id,
                chunk.chunk_index,
                chunk.content,
                chunk.page,
                chunk.section,
                _to_blob(chunk.embedding),
                int(np.asarray(chunk.embedding).shape[-1]),
            )
            for chunk in chunks
        ]
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO chunks
                    (id, document_id, owner_id, chunk_index, content,
                     page, section, embedding, embedding_dim)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def delete_document_chunks(self, *, owner_id: str, document_id: str) -> int:
        """Delete only the vectors for a document. Returns rows removed.

        Used when indexing fails partway: a document with some of its chunks
        stored would answer from an incomplete corpus while appearing ``ready``.
        """
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM chunks WHERE document_id = ? AND owner_id = ?",
                (document_id, owner_id),
            )
            return cursor.rowcount

    def count_chunks(self, *, owner_id: str, document_id: str | None = None) -> int:
        query = "SELECT COUNT(*) AS n FROM chunks WHERE owner_id = ?"
        params: list[object] = [owner_id]
        if document_id is not None:
            query += " AND document_id = ?"
            params.append(document_id)
        with self._connect() as connection:
            return int(connection.execute(query, params).fetchone()["n"])

    def fetch_chunks(
        self,
        *,
        owner_id: str,
        document_ids: Sequence[str] | None = None,
        embeddings: bool = True,
        ready_only: bool = False,
    ) -> tuple[list[StoredChunk], np.ndarray | None]:
        """Read an owner's chunks, optionally filtered to specific documents.

        Returns ``(chunks, matrix)`` where ``matrix`` is ``(n_chunks, dim)``
        float32, or ``None`` when ``embeddings=False``. One query plus one array
        build - not one query per chunk. ``ready_only`` restricts the result to
        chunks whose parent document finished indexing.
        """
        columns = (
            "c.id, c.document_id, c.content, c.page, c.section, c.chunk_index, "
            "c.embedding, d.filename"
        )
        query = f"SELECT {columns} FROM chunks c JOIN documents d ON d.id = c.document_id WHERE c.owner_id = ?"
        params: list[object] = [owner_id]

        if ready_only:
            query += " AND d.status = ?"
            params.append(STATUS_READY)

        if document_ids:
            placeholders = ", ".join("?" for _ in document_ids)
            query += f" AND c.document_id IN ({placeholders})"
            params.extend(document_ids)

        query += " ORDER BY c.document_id, c.chunk_index"

        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
            blobs = [row["embedding"] for row in rows]

        chunks = [
            StoredChunk(
                id=row["id"],
                document_id=row["document_id"],
                filename=row["filename"],
                content=row["content"],
                page=row["page"],
                section=row["section"],
                chunk_index=row["chunk_index"],
            )
            for row in rows
        ]

        if not embeddings:
            return chunks, None
        if not rows:
            return chunks, np.empty((0, 0), dtype=np.float32)

        matrix = np.vstack([_from_blob(blob) for blob in blobs]).astype(np.float32)
        return chunks, matrix

    def search_chunks(
        self,
        *,
        owner_id: str,
        query_embedding: Sequence[float] | np.ndarray,
        top_k: int,
        document_ids: Sequence[str] | None = None,
        min_score: float = 0.1,
    ) -> ChunkSearchResult:
        """Rank this owner's chunks against ``query_embedding`` in NumPy.

        A full scan: every chunk in scope is loaded and compared. That is the
        honest cost of a store with no vector index, and it is why the Postgres
        store implements this method against pgvector rather than reusing it.

        Ties are broken by chunk id so identical requests return identical order.
        """
        chunks, matrix = self.fetch_chunks(
            owner_id=owner_id, document_ids=document_ids, ready_only=True
        )
        if not chunks or matrix is None or matrix.size == 0:
            return ChunkSearchResult(matches=[], searched_chunks=len(chunks))

        vector = np.asarray(query_embedding, dtype=np.float32).reshape(-1)
        if matrix.shape[1] != vector.shape[0]:
            raise EmbeddingDimensionMismatchError(
                stored_dimensions=int(matrix.shape[1]),
                query_dimensions=int(vector.shape[0]),
            )

        if top_k <= 0:
            return ChunkSearchResult(matches=[], searched_chunks=len(chunks))

        scores = cosine_similarity_matrix(matrix, vector)
        ranked = sorted(
            zip(chunks, scores, strict=True),
            key=lambda pair: (-float(pair[1]), pair[0].id),
        )

        matches: list[ChunkMatch] = []
        for chunk, score in ranked:
            value = float(score)
            if value < min_score:
                continue
            matches.append(
                ChunkMatch(
                    id=chunk.id,
                    document_id=chunk.document_id,
                    filename=chunk.filename,
                    content=chunk.content,
                    page=chunk.page,
                    section=chunk.section,
                    chunk_index=chunk.chunk_index,
                    score=value,
                )
            )
            if len(matches) >= top_k:
                break

        return ChunkSearchResult(matches=matches, searched_chunks=len(chunks))
