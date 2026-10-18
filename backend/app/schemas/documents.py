"""Request/response schemas for the document and retrieval APIs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.database.repositories import DocumentRecord
from app.rag.retriever import RetrievedChunk
from app.schemas.chat import TokenUsage

MAX_QUESTION_CHARS = 4000
MAX_SEARCH_QUERY_CHARS = 2000

DocumentStatus = Literal["pending", "processing", "ready", "failed"]

DEFAULT_TOP_K = 6
MAX_TOP_K = 25


class DocumentResponse(BaseModel):
    """Public view of a stored document.

    Note what is absent: file contents, storage paths, and owner ids. The owner is
    implied by the authenticated request and never needs to be echoed back.
    """

    id: str
    filename: str
    content_type: str
    extension: str
    size_bytes: int
    status: DocumentStatus
    error: str | None = Field(
        default=None,
        description="Why indexing failed, when ``status`` is ``failed``.",
    )
    page_count: int | None = Field(
        default=None, description="Pages read, for formats that have pages (PDF)."
    )
    chunk_count: int = Field(description="Number of stored vectors for this document.")
    created_at: str
    indexed_at: str | None = None

    @classmethod
    def from_record(cls, record: DocumentRecord) -> DocumentResponse:
        return cls(
            id=record.id,
            filename=record.filename,
            content_type=record.content_type,
            extension=record.extension,
            size_bytes=record.size_bytes,
            status=record.status,  # type: ignore[arg-type]
            error=record.error,
            page_count=record.page_count,
            chunk_count=record.chunk_count,
            created_at=record.created_at,
            indexed_at=record.indexed_at,
        )


class DocumentListResponse(BaseModel):
    """Body of ``GET /api/documents``.

    Scoped to the caller: this model can only be built from records the
    repository returned for the requesting owner.
    """

    documents: list[DocumentResponse]
    total: int


class SourceCitation(BaseModel):
    """One retrieved passage backing an answer or a search result.

    ``label`` is the bracketed number the model was told to cite, so a client can
    match ``[2]`` in the answer text to a concrete file and page.
    """

    label: int
    document_id: str
    filename: str
    page: int | None = None
    section: str | None = None
    score: float = Field(description="Cosine similarity between the query and this passage.")
    snippet: str

    @classmethod
    def from_match(cls, label: int, match: RetrievedChunk) -> SourceCitation:
        return cls(
            label=label,
            document_id=match.document_id,
            filename=match.filename,
            page=match.page,
            section=match.section,
            score=round(match.score, 4),
            snippet=match.snippet,
        )


class SearchRequest(BaseModel):
    """Body of ``POST /api/documents/search``."""

    query: str = Field(
        min_length=1,
        max_length=MAX_SEARCH_QUERY_CHARS,
        description="Natural-language query. Matched semantically, not by keyword.",
    )
    top_k: int | None = Field(
        default=None,
        ge=1,
        le=MAX_TOP_K,
        description=f"Number of passages to return. Defaults to {DEFAULT_TOP_K}.",
    )
    document_ids: list[str] | None = Field(
        default=None,
        description="Restrict the search to these documents. Omit to search everything you own.",
    )

    @field_validator("query")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("The query must not be blank.")
        return value


class SearchResponse(BaseModel):
    """Body of a search response. Contains no model-generated text."""

    query: str
    searched_chunks: int = Field(
        description="How many of your passages were compared. Zero means nothing is indexed yet."
    )
    matches: list[SourceCitation]


class AskRequest(BaseModel):
    """Body of ``POST /api/documents/ask``."""

    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    top_k: int | None = Field(default=None, ge=1, le=MAX_TOP_K)
    document_ids: list[str] | None = Field(
        default=None, description="Restrict the answer to these documents."
    )

    @field_validator("question")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("The question must not be blank.")
        return value


class AskResponse(BaseModel):
    """A grounded answer with the evidence it was built from."""

    question: str
    answer: str
    grounded: bool = Field(
        description=(
            "True when relevant passages were found and the model answered from them. "
            "False when nothing was retrieved - in that case ``answer`` states that no "
            "relevant content exists and the model was not called."
        )
    )
    sources: list[SourceCitation]
    searched_chunks: int
    provider: str
    model: str
    usage: TokenUsage | None = None
    request_id: str
