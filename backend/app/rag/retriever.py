"""Retrieval-augmented question answering.

Two steps, deliberately separate:

1. :func:`search` - embed the query, compare it against the caller's chunks, rank.
2. :func:`answer_question` - feed the top chunks to the model and require it to
   answer only from them.

The separation matters because retrieval can be tested and inspected without an
LLM. When an answer is wrong, the first question is always "were the right chunks
retrieved?" - and this structure makes that answerable in one API call.

**When there is nothing to retrieve, the model is not called at all.** Asking a
model to answer from an empty context is an invitation to hallucinate, and the
honest answer is already known: nothing was found.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from starlette.concurrency import run_in_threadpool

from app.core.errors import AppError
from app.core.logging import get_logger, safe_extra
from app.database.repositories import ChunkMatch, DocumentStore
from app.llm.prompts import RAG_SYSTEM_PROMPT, build_grounded_answer_prompt
from app.llm.service import TokenUsage, generate_answer, map_exception

logger = get_logger(__name__)

DEFAULT_MIN_SCORE = 0.1

SNIPPET_CHARS = 280


@dataclass(frozen=True)
class RetrievedChunk:
    """One search hit."""

    chunk_id: str
    document_id: str
    filename: str
    content: str
    page: int | None
    section: str | None
    score: float

    @property
    def citation(self) -> str:
        """Human-readable source label, e.g. ``handbook.pdf, page 7``."""
        label = self.filename
        if self.page is not None:
            label = f"{label}, page {self.page}"
        if self.section:
            label = f"{label} ({self.section})"
        return label

    @property
    def snippet(self) -> str:
        text = self.content.strip()
        if len(text) <= SNIPPET_CHARS:
            return text
        return text[:SNIPPET_CHARS].rsplit(" ", 1)[0] + "..."


@dataclass(frozen=True)
class SearchResult:
    """Outcome of a retrieval call."""

    query: str
    matches: list[RetrievedChunk] = field(default_factory=list)
    searched_chunks: int = 0

    @property
    def found(self) -> bool:
        return bool(self.matches)


@dataclass(frozen=True)
class RagAnswer:
    """A grounded answer plus the evidence behind it."""

    question: str
    answer: str
    sources: list[RetrievedChunk]
    searched_chunks: int
    grounded: bool
    """False when no context was found, so ``answer`` is a statement about the
    corpus rather than a model output."""
    usage: TokenUsage | None = None


class EmptyQueryError(AppError):
    """A blank query reached the retrieval layer."""

    status_code = 400
    code = "empty_query"
    public_message = "The query must not be blank."


def _to_retrieved(match: ChunkMatch) -> RetrievedChunk:
    """Store row -> API shape.

    One conversion point, so the citations a caller sees and the passages the
    model was given can never drift apart.
    """
    return RetrievedChunk(
        chunk_id=match.id,
        document_id=match.document_id,
        filename=match.filename,
        content=match.content,
        page=match.page,
        section=match.section,
        score=match.score,
    )


def search(
    repository: DocumentStore,
    embeddings: Embeddings,
    *,
    owner_id: str,
    query: str,
    top_k: int,
    document_ids: list[str] | None = None,
    min_score: float = DEFAULT_MIN_SCORE,
) -> SearchResult:
    """Semantic search over the caller's own chunks.

    Ranking is delegated to the store: NumPy for SQLite, pgvector for Postgres.
    ``owner_id`` is part of that call rather than a post-filter, so another
    tenant's vectors are excluded before any comparison happens - by the SQL in
    one backend and by Row Level Security in the other.
    """
    cleaned = query.strip()
    if not cleaned:
        raise EmptyQueryError()

    try:
        query_vector = np.asarray(embeddings.embed_query(cleaned), dtype=np.float32)
    except Exception as exc:  # noqa: BLE001 - translated to a typed provider error
        mapped = map_exception(exc)
        logger.warning(
            "retrieval_embedding_failed",
            extra=safe_extra(error_type=type(exc).__name__, mapped_error=mapped.code),
        )
        raise mapped from exc

    result = repository.search_chunks(
        owner_id=owner_id,
        query_embedding=query_vector,
        top_k=top_k,
        document_ids=document_ids,
        min_score=min_score,
    )
    matches = [_to_retrieved(match) for match in result.matches]

    logger.info(
        "retrieval_completed",
        extra=safe_extra(
            searched_chunks=result.searched_chunks,
            returned=len(matches),
            top_k=top_k,
            best_score=round(matches[0].score, 4) if matches else None,
        ),
    )
    return SearchResult(query=cleaned, matches=matches, searched_chunks=result.searched_chunks)


def format_context(
    matches: list[RetrievedChunk], *, max_chars: int
) -> tuple[str, list[RetrievedChunk]]:
    """Render matches as numbered context, dropping any that do not fit.

    Returns the text and the matches that actually made it in, so citations and
    context can never disagree - the model is never given a label that the
    response omits, and vice versa.
    """
    blocks: list[str] = []
    included: list[RetrievedChunk] = []
    used = 0

    for match in matches:
        label = len(included) + 1
        block = f"[{label}] Source: {match.citation}\n{match.content}"

        if used + len(block) > max_chars and included:
            break

        blocks.append(block)
        included.append(match)
        used += len(block)

    return "\n\n".join(blocks), included


async def answer_question(
    repository: DocumentStore,
    embeddings: Embeddings,
    model: BaseChatModel,
    *,
    owner_id: str,
    question: str,
    top_k: int,
    max_context_chars: int,
    document_ids: list[str] | None = None,
    min_score: float = DEFAULT_MIN_SCORE,
) -> RagAnswer:
    """Answer a question strictly from the caller's documents.

    Raises:
        AppError: retrieval or provider failures. A no-match outcome is *not* an
            error - it returns ``grounded=False`` with an explanatory answer.
    """
    cleaned = question.strip()
    if not cleaned:
        raise EmptyQueryError("The question must not be blank.")

    query_vector = np.asarray(await embeddings.aembed_query(cleaned), dtype=np.float32)
    result = await run_in_threadpool(
        repository.search_chunks,
        owner_id=owner_id,
        query_embedding=query_vector,
        top_k=top_k,
        document_ids=document_ids,
        min_score=min_score,
    )
    candidates = [_to_retrieved(match) for match in result.matches]

    if not candidates:
        if result.searched_chunks == 0:
            answer = (
                "No documents have been indexed yet, so this cannot be answered from your "
                "documents. Upload a document and try again."
            )
        else:
            answer = (
                "No relevant content was found in your documents for that question. "
                "Nothing has been guessed - try rephrasing, or upload a document that covers it."
            )
        return RagAnswer(
            question=cleaned,
            answer=answer,
            sources=[],
            searched_chunks=result.searched_chunks,
            grounded=False,
        )

    context, included = format_context(candidates, max_chars=max_context_chars)

    prompt = build_grounded_answer_prompt(cleaned, context)
    completion = await generate_answer(
        model,
        history=[{"role": "user", "content": prompt}],
        system_prompt=RAG_SYSTEM_PROMPT,
    )

    logger.info(
        "rag_answered",
        extra=safe_extra(
            searched_chunks=result.searched_chunks,
            context_chunks=len(included),
            context_chars=len(context),
            citations=len(included),
        ),
    )

    return RagAnswer(
        question=cleaned,
        answer=completion.text,
        sources=included,
        searched_chunks=result.searched_chunks,
        grounded=True,
        usage=completion.usage,
    )
