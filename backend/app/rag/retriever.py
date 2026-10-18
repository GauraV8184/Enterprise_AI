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

from app.core.errors import AppError
from app.core.logging import get_logger, safe_extra
from app.database.repositories import DocumentRepository
from app.llm.prompts import RAG_SYSTEM_PROMPT, build_grounded_answer_prompt
from app.llm.service import TokenUsage, generate_answer
from app.rag.loaders import cosine_similarity_matrix

logger = get_logger(__name__)

#: Chunks below this cosine similarity are almost always unrelated. Small local
#: embedding models put unrelated text around 0.5-0.65 and related text above
#: ~0.7, so a low floor removes obvious noise without discarding real hits.
DEFAULT_MIN_SCORE = 0.0

#: Characters of a chunk shown in a citation snippet.
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


def _require_dimension_match(matrix: np.ndarray, query_vector: np.ndarray) -> None:
    if matrix.size and matrix.shape[1] != query_vector.shape[0]:
        raise EmbeddingDimensionMismatchError(
            stored_dimensions=int(matrix.shape[1]),
            query_dimensions=int(query_vector.shape[0]),
        )


class EmbeddingDimensionMismatchError(AppError):
    """Stored vectors and the current embedding model disagree.

    This happens when ``EMBEDDING_MODEL`` changes after documents were indexed.
    Comparing the two would be meaningless, so the request fails with an
    explanation instead of returning silently bad search results.
    """

    status_code = 409
    code = "embedding_dimension_mismatch"

    def __init__(self, *, stored_dimensions: int, query_dimensions: int) -> None:
        super().__init__(
            f"Stored chunks were embedded with a {stored_dimensions}-dimensional model, but the "
            f"configured model produces {query_dimensions} dimensions. Re-upload the documents, or "
            "restore the previous EMBEDDING_MODEL."
        )
        self.public_message = self.message


def search(
    repository: DocumentRepository,
    embeddings: Embeddings,
    *,
    owner_id: str,
    query: str,
    top_k: int,
    document_ids: list[str] | None = None,
    min_score: float = DEFAULT_MIN_SCORE,
) -> SearchResult:
    """Semantic search over the caller's own chunks.

    ``owner_id`` is threaded into the query rather than applied after the fact,
    so another tenant's vectors are never loaded into memory in the first place.
    """
    cleaned = query.strip()
    if not cleaned:
        raise EmptyQueryError()

    chunks, matrix = repository.fetch_chunks(owner_id=owner_id, document_ids=document_ids)
    if not chunks:
        return SearchResult(query=cleaned, matches=[], searched_chunks=0)

    query_vector = np.asarray(embeddings.embed_query(cleaned), dtype=np.float32)
    _require_dimension_match(matrix, query_vector)

    scores = cosine_similarity_matrix(matrix, query_vector)

    # Rank by score, then by chunk id. Ids never change for a stored corpus, so
    # results are stable across identical requests even when scores tie exactly.
    ranked = sorted(
        zip(chunks, scores, strict=True),
        key=lambda pair: (-float(pair[1]), pair[0].id),
    )

    matches: list[RetrievedChunk] = []
    for chunk, score in ranked:
        value = float(score)
        if value < min_score:
            continue
        matches.append(
            RetrievedChunk(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                filename=chunk.filename,
                content=chunk.content,
                page=chunk.page,
                section=chunk.section,
                score=value,
            )
        )
        if len(matches) >= top_k:
            break

    logger.info(
        "retrieval_completed",
        extra=safe_extra(
            searched_chunks=len(chunks),
            returned=len(matches),
            top_k=top_k,
            best_score=round(matches[0].score, 4) if matches else None,
        ),
    )
    return SearchResult(query=cleaned, matches=matches, searched_chunks=len(chunks))


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
    repository: DocumentRepository,
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

    chunks, matrix = repository.fetch_chunks(owner_id=owner_id, document_ids=document_ids)
    if not chunks:
        return RagAnswer(
            question=cleaned,
            answer=(
                "No documents have been indexed yet, so this cannot be answered from your "
                "documents. Upload a document and try again."
            ),
            sources=[],
            searched_chunks=0,
            grounded=False,
        )

    query_vector = np.asarray(await embeddings.aembed_query(cleaned), dtype=np.float32)
    _require_dimension_match(matrix, query_vector)
    scores = cosine_similarity_matrix(matrix, query_vector)

    ranked = sorted(
        zip(chunks, scores, strict=True),
        key=lambda pair: (-float(pair[1]), pair[0].id),
    )

    candidates = [
        RetrievedChunk(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            filename=chunk.filename,
            content=chunk.content,
            page=chunk.page,
            section=chunk.section,
            score=float(score),
        )
        for chunk, score in ranked
        if float(score) >= min_score
    ][:top_k]

    context, included = format_context(candidates, max_chars=max_context_chars)

    if not included:
        return RagAnswer(
            question=cleaned,
            answer=(
                "No relevant content was found in your documents for that question. "
                "Nothing has been guessed - try rephrasing, or upload a document that covers it."
            ),
            sources=[],
            searched_chunks=len(chunks),
            grounded=False,
        )

    prompt = build_grounded_answer_prompt(cleaned, context)
    completion = await generate_answer(
        model,
        history=[{"role": "user", "content": prompt}],
        system_prompt=RAG_SYSTEM_PROMPT,
    )

    logger.info(
        "rag_answered",
        extra=safe_extra(
            searched_chunks=len(chunks),
            context_chunks=len(included),
            context_chars=len(context),
            citations=len(included),
        ),
    )

    return RagAnswer(
        question=cleaned,
        answer=completion.text,
        sources=included,
        searched_chunks=len(chunks),
        grounded=True,
        usage=completion.usage,
    )
