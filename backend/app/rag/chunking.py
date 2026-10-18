"""Split extracted text into retrieval-sized chunks.

Chunking is where retrieval quality is won or lost. Two decisions matter here:

1. **Split per block, not per document.** A PDF page is a block, so a chunk never
   spans two pages and its ``page`` metadata is exact. Splitting the whole
   document as one string would give better-boundary chunks but make citations
   approximate, and citations are the point of a document assistant.
2. **Carry provenance forward.** Page and section travel with each chunk into the
   vector store, so an answer can say *which* page it came from.

``RecursiveCharacterTextSplitter`` tries paragraph, then line, then sentence, then
word boundaries before cutting mid-word. That ordering is why chunks read like
prose instead of fragments.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.core.logging import get_logger, safe_extra
from app.rag.loaders import TextBlock

logger = get_logger(__name__)

#: Separators in priority order. The empty string is the final fallback: a hard
#: character cut for pathological input such as one enormous unbroken token.
_SEPARATORS = ["\n\n", "\n", ". ", "! ", "? ", "; ", ", ", " ", ""]


@dataclass(frozen=True)
class PreparedChunk:
    """A chunk ready to be embedded, with its provenance intact."""

    content: str
    page: int | None
    section: str | None

    @property
    def char_count(self) -> int:
        return len(self.content)


def build_splitter(*, chunk_size: int, chunk_overlap: int) -> RecursiveCharacterTextSplitter:
    """Create the splitter used for every document.

    ``length_function=len`` measures characters rather than tokens. That keeps
    chunk sizes predictable across providers and needs no tokeniser download; the
    trade-off is that a chunk of dense text holds more tokens than a chunk of
    whitespace, so ``chunk_size`` is a budget, not a guarantee.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if chunk_overlap < 0:
        raise ValueError("chunk_overlap cannot be negative")
    if chunk_overlap >= chunk_size:
        # Overlap at or above the chunk size means a window that never advances,
        # producing overlapping duplicates forever on long input.
        raise ValueError("chunk_overlap must be smaller than chunk_size")

    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=_SEPARATORS,
        length_function=len,
        keep_separator=True,
    )


def chunk_blocks(
    blocks: Sequence[TextBlock],
    *,
    chunk_size: int,
    chunk_overlap: int,
) -> list[PreparedChunk]:
    """Split text blocks into chunks, preserving page and section.

    Whitespace-only chunks are dropped: they carry no meaning, but a local
    embedding model would happily spend time vectorising them and they would
    occupy slots in the top-k results.
    """
    splitter = build_splitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    chunks: list[PreparedChunk] = []
    for block in blocks:
        text = block.text.strip()
        if not text:
            continue

        for piece in splitter.split_text(text):
            cleaned = piece.strip()
            if not cleaned:
                continue
            chunks.append(
                PreparedChunk(
                    content=cleaned,
                    page=block.page,
                    section=block.section,
                )
            )

    logger.info(
        "document_chunked",
        extra=safe_extra(
            blocks=len(blocks),
            chunks=len(chunks),
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            mean_chars=round(sum(c.char_count for c in chunks) / len(chunks), 1) if chunks else 0,
        ),
    )
    return chunks
