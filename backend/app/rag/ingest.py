"""The ingest pipeline: file on disk -> parsed text -> chunks -> stored vectors.

Ordering is deliberate. The document is marked ``processing`` before any work
starts, so a crash mid-ingest leaves a visible "processing" record rather than a
document that claims to be ``pending`` forever with no explanation.

Failure handling: a document that fails to index is kept with ``status="failed"``
and a reason, rather than deleted. The user can see what happened and remove it
themselves; a silent disappearance is worse than a visible error.

Embedding is the only slow step and it runs as one batch call. The provider client
handles request-level batching internally (``EMBEDDING_BATCH_SIZE``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from langchain_core.embeddings import Embeddings

from app.core.errors import AppError
from app.core.logging import get_logger, safe_extra
from app.database.repositories import (
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_READY,
    ChunkRecord,
    DocumentRecord,
    DocumentRepository,
    new_id,
)
from app.rag.chunking import chunk_blocks
from app.rag.loaders import NoExtractableTextError, load_document


class DocumentProcessingError(AppError):
    """Unexpected failure while indexing a document."""

    status_code = 500
    code = "document_processing_failed"
    public_message = "That document could not be indexed. See the server logs for details."


@dataclass(frozen=True)
class IngestResult:
    """What happened during ingestion."""

    document: DocumentRecord
    chunk_count: int
    page_count: int | None
    succeeded: bool


logger = get_logger(__name__)


def _safe_failure_message(exc: BaseException) -> str:
    """Message stored on the document when indexing fails.

    ``AppError.message`` is already treated as client-safe elsewhere (it becomes
    the HTTP ``detail``), so it is reused here. Anything else is a bug or an
    infrastructure fault and its text is not shown to the user at all.
    """
    if isinstance(exc, AppError):
        return exc.message
    return "Indexing failed unexpectedly. See the server logs for details."


def ingest_document(
    *,
    repository: DocumentRepository,
    embeddings: Embeddings,
    owner_id: str,
    document_id: str,
    path: Path,
    filename: str,
    chunk_size: int,
    chunk_overlap: int,
) -> IngestResult:
    """Parse, chunk, embed and store one document.

    Returns the final record. On failure the record is marked ``failed`` and the
    original exception propagates so the caller can decide the HTTP status - the
    upload itself did succeed, so this is not necessarily a request error.
    """
    if not repository.update_status(
        owner_id=owner_id,
        document_id=document_id,
        status=STATUS_PROCESSING,
    ):
        # The document was deleted between upload and processing.
        raise DocumentProcessingError("The document no longer exists.")

    try:
        loaded = load_document(path, filename=filename)
        chunks = chunk_blocks(
            loaded.blocks,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        if not chunks:
            raise NoExtractableTextError(f"'{filename}' produced no indexable text.")

        vectors = embeddings.embed_documents([chunk.content for chunk in chunks])
        if len(vectors) != len(chunks):
            # A mismatch would silently associate text with the wrong vector.
            raise DocumentProcessingError(
                f"The embedding provider returned {len(vectors)} vectors for {len(chunks)} chunks."
            )

        repository.add_chunks(
            [
                ChunkRecord(
                    id=new_id(),
                    document_id=document_id,
                    owner_id=owner_id,
                    chunk_index=index,
                    content=chunk.content,
                    page=chunk.page,
                    section=chunk.section,
                    embedding=vector,
                )
                for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
            ]
        )

        repository.update_status(
            owner_id=owner_id,
            document_id=document_id,
            status=STATUS_READY,
            error=None,
            page_count=loaded.page_count,
            chunk_count=len(chunks),
            mark_indexed=True,
        )

    except Exception as exc:
        message = _safe_failure_message(exc)
        repository.update_status(
            owner_id=owner_id,
            document_id=document_id,
            status=STATUS_FAILED,
            error=message,
            chunk_count=0,
        )
        # A half-written document must not leave searchable vectors behind.
        repository.delete_document_chunks(owner_id=owner_id, document_id=document_id)

        if isinstance(exc, AppError):
            logger.warning(
                "document_ingest_failed",
                extra=safe_extra(error=exc.code, filename=filename),
            )
            raise

        logger.exception(
            "document_ingest_crashed",
            extra=safe_extra(filename=filename, error_type=type(exc).__name__),
        )
        raise DocumentProcessingError() from exc

    document = repository.get_document(owner_id=owner_id, document_id=document_id)
    assert document is not None  # noqa: S101 - just written under this owner id

    logger.info(
        "document_ingested",
        extra=safe_extra(
            document_id=document_id,
            filename=filename,
            chunks=len(chunks),
            pages=loaded.page_count,
        ),
    )
    return IngestResult(
        document=document,
        chunk_count=len(chunks),
        page_count=loaded.page_count,
        succeeded=True,
    )
