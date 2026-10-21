"""Document upload, listing, retrieval and grounded question answering.

**Threading.** Upload, list, delete and search are declared ``def``, not
``async def``. Parsing a PDF and calling an embedding model are blocking
operations, so FastAPI runs these in a worker thread and the event loop stays
responsive. Only ``/ask`` is async, because it also awaits the chat model.

**Ownership.** Every endpoint resolves the acting user and passes that id into the
store. Nothing is filtered after the fact, and a document belonging to someone else
returns 404 rather than 403 - a 403 would confirm that the id exists.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Response, UploadFile, status

from app.api.deps import ChatModelDep, EmbeddingsDep, FileStoreDep, RepositoryDep, SettingsDep
from app.core.errors import AppError
from app.core.logging import get_logger, request_id_ctx, safe_extra
from app.database.repositories import STATUS_FAILED, DocumentRecord, DocumentStore
from app.rag.ingest import ingest_document
from app.rag.retriever import answer_question, search
from app.schemas.chat import TokenUsage
from app.schemas.documents import (
    AskRequest,
    AskResponse,
    DocumentListResponse,
    DocumentResponse,
    SearchRequest,
    SearchResponse,
    SourceCitation,
)
from app.security.file_validation import normalize_filename, validate_upload
from app.security.identity import CurrentUserDep

logger = get_logger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])


class DocumentNotFoundError(AppError):
    status_code = 404
    code = "document_not_found"
    public_message = "No such document."


def _mark_failed(
    repository: DocumentStore,
    *,
    owner_id: str,
    document_id: str,
    error: AppError,
) -> None:
    """Record a storage failure against the document, best effort.

    The document row is created before the bytes are stored, so a storage outage
    would otherwise leave it ``pending`` forever with no explanation. Marking it
    ``failed`` makes it visible and deletable.

    A failure to write the status must not replace the original error: the caller
    is about to receive the storage error, and a second one would only obscure it.
    """
    try:
        repository.update_status(
            owner_id=owner_id,
            document_id=document_id,
            status=STATUS_FAILED,
            error=error.message,
            chunk_count=0,
        )
    except AppError:
        logger.warning(
            "document_failure_status_not_recorded",
            extra=safe_extra(document_id=document_id, error=error.code),
        )
    error.details = {**error.details, "document_id": document_id, "status": STATUS_FAILED}


def _read_capped(file: UploadFile, max_bytes: int) -> bytes:
    """Read at most ``max_bytes + 1`` bytes from an upload.

    Starlette spools large uploads to a temporary file, so this does not hold the
    whole request body in memory. Reading one byte past the limit is what makes an
    oversized file detectable without reading all of it.
    """
    return file.file.read(max_bytes + 1)


def _require_document(repository: RepositoryDep, owner_id: str, document_id: str) -> DocumentRecord:
    record = repository.get_document(owner_id=owner_id, document_id=document_id)
    if record is None:
        raise DocumentNotFoundError(
            "No document with that id. It may have been deleted, or it belongs to another user."
        )
    return record


@router.post(
    "",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload and index a document",
)
def upload_document(
    file: Annotated[UploadFile, File(description="The file to index. See the supported types.")],
    *,
    settings: SettingsDep,
    repository: RepositoryDep,
    file_store: FileStoreDep,
    embeddings: EmbeddingsDep,
    user: CurrentUserDep,
) -> DocumentResponse:
    """Validate, store and index an uploaded file.

    The original goes to the file store; the searchable text and vectors go to the
    document store. Indexing runs inline, so a successful response means the
    document is already searchable. A document that fails either step is still
    recorded with ``status="failed"`` and the reason, and the error response
    carries its id so it can be inspected or removed.

    Repository note: processing a large file inline means a slow request. Moving
    this to a background worker is the natural next step and needs no API change,
    because ``status`` already models the transition.
    """
    data = _read_capped(file, settings.max_upload_bytes)
    safe_name = normalize_filename(file.filename)
    file_type = validate_upload(filename=safe_name, data=data, max_bytes=settings.max_upload_bytes)

    record = repository.create_document(
        owner_id=user.user_id,
        filename=safe_name,
        content_type=file_type.mime_type,
        extension=file_type.extension,
        size_bytes=len(data),
    )

    try:
        file_store.put(
            owner_id=user.user_id,
            document_id=record.id,
            filename=safe_name,
            data=data,
            content_type=file_type.mime_type,
        )
        stored = file_store.read(
            owner_id=user.user_id,
            document_id=record.id,
            filename=safe_name,
        )
    except AppError as exc:
        _mark_failed(repository, owner_id=user.user_id, document_id=record.id, error=exc)
        raise

    logger.info(
        "document_uploaded",
        extra=safe_extra(
            document_id=record.id,
            extension=file_type.extension,
            size_bytes=len(data),
        ),
    )

    try:
        result = ingest_document(
            repository=repository,
            embeddings=embeddings,
            owner_id=user.user_id,
            document_id=record.id,
            data=stored,
            filename=record.filename,
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
        )
    except AppError as exc:
        exc.details = {**exc.details, "document_id": record.id, "status": STATUS_FAILED}
        raise

    return DocumentResponse.from_record(result.document)


@router.get("", response_model=DocumentListResponse, summary="List your documents")
def list_documents(
    repository: RepositoryDep,
    user: CurrentUserDep,
) -> DocumentListResponse:
    """List documents owned by the caller, newest first."""
    records = repository.list_documents(owner_id=user.user_id)
    documents = [DocumentResponse.from_record(record) for record in records]
    return DocumentListResponse(documents=documents, total=len(documents))


@router.get("/{document_id}", response_model=DocumentResponse, summary="Get one document")
def get_document(
    document_id: str,
    repository: RepositoryDep,
    user: CurrentUserDep,
) -> DocumentResponse:
    """Return metadata and processing status for one of your documents."""
    return DocumentResponse.from_record(_require_document(repository, user.user_id, document_id))


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document and its vectors",
)
def delete_document(
    document_id: str,
    repository: RepositoryDep,
    file_store: FileStoreDep,
    user: CurrentUserDep,
) -> Response:
    """Delete a document, every vector derived from it, and the stored original.

    The object is deleted before the rows. If storage refuses, the request fails
    and nothing is deleted, so the caller can simply retry; deleting the rows
    first would leave bytes that no row points at and nothing would retry.
    Deleting an already-missing object is a no-op, which makes a retry after a
    partial failure converge.

    Vectors are removed in the same transaction as the metadata, so a deleted
    document can never keep answering questions from orphaned chunks.
    """
    record = _require_document(repository, user.user_id, document_id)

    file_store.delete(
        owner_id=user.user_id,
        document_id=record.id,
        filename=record.filename,
    )

    repository.delete_document(owner_id=user.user_id, document_id=document_id)

    logger.info("document_deleted", extra=safe_extra(document_id=document_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/search", response_model=SearchResponse, summary="Semantic search over your documents"
)
def search_documents(
    payload: SearchRequest,
    repository: RepositoryDep,
    embeddings: EmbeddingsDep,
    settings: SettingsDep,
    user: CurrentUserDep,
) -> SearchResponse:
    """Return the passages most similar to a query. No model writes any text here.

    Use this to inspect retrieval on its own. When an answer looks wrong, this
    endpoint shows whether the right passages were found in the first place.
    """
    result = search(
        repository,
        embeddings,
        owner_id=user.user_id,
        query=payload.query,
        top_k=payload.top_k or settings.retrieval_top_k,
        document_ids=payload.document_ids,
    )

    return SearchResponse(
        query=result.query,
        searched_chunks=result.searched_chunks,
        matches=[
            SourceCitation.from_match(index, match) for index, match in enumerate(result.matches, 1)
        ],
    )


@router.post(
    "/ask",
    response_model=AskResponse,
    summary="Answer a question from your documents",
)
async def ask_documents(
    payload: AskRequest,
    repository: RepositoryDep,
    embeddings: EmbeddingsDep,
    model: ChatModelDep,
    settings: SettingsDep,
    user: CurrentUserDep,
) -> AskResponse:
    """Retrieve relevant passages and answer strictly from them.

    When nothing relevant is retrieved the model is not called at all: the
    response has ``grounded=false`` and explains that no matching content exists.
    That is a deliberate refusal to let the model improvise.
    """
    result = await answer_question(
        repository,
        embeddings,
        model,
        owner_id=user.user_id,
        question=payload.question,
        top_k=payload.top_k or settings.retrieval_top_k,
        max_context_chars=settings.max_context_chars,
        document_ids=payload.document_ids,
    )

    logger.info(
        "document_question_answered",
        extra=safe_extra(
            grounded=result.grounded,
            sources=len(result.sources),
            searched_chunks=result.searched_chunks,
        ),
    )

    return AskResponse(
        question=result.question,
        answer=result.answer,
        grounded=result.grounded,
        sources=[
            SourceCitation.from_match(index, match) for index, match in enumerate(result.sources, 1)
        ],
        searched_chunks=result.searched_chunks,
        provider=settings.llm_provider,
        model=settings.openai_model,
        usage=TokenUsage(**result.usage.__dict__) if result.usage else None,
        request_id=request_id_ctx.get(),
    )
