"""Chunking: size limits, overlap, and provenance survival."""

from __future__ import annotations

import pytest

from app.rag.chunking import build_splitter, chunk_blocks
from app.rag.loaders import TextBlock

LONG_PARAGRAPHS = "\n\n".join(
    f"Paragraph {index} discusses operational procedure number {index} in detail, "
    f"covering the steps, the responsible team, and the expected timeframe."
    for index in range(1, 12)
)


def test_long_text_becomes_multiple_chunks() -> None:
    chunks = chunk_blocks(
        [TextBlock(text=LONG_PARAGRAPHS)],
        chunk_size=400,
        chunk_overlap=80,
    )

    assert len(chunks) > 1
    assert all(chunk.char_count <= 400 for chunk in chunks)


def test_chunks_do_not_lose_text() -> None:
    """Every word of the source must appear somewhere.

    Overlap means chunks are not a partition, so this checks coverage rather than
    counts - the failure it guards against is silent content loss at a boundary.
    """
    chunks = chunk_blocks([TextBlock(text=LONG_PARAGRAPHS)], chunk_size=400, chunk_overlap=80)
    combined = " ".join(chunk.content for chunk in chunks)

    assert combined.count("operational procedure") == LONG_PARAGRAPHS.count("operational procedure")


def test_overlap_makes_chunks_share_text() -> None:
    """Without overlap a sentence split across a boundary is unfindable."""
    body = " ".join(f"token{index}" for index in range(400))
    chunks = chunk_blocks([TextBlock(text=body)], chunk_size=200, chunk_overlap=50)

    assert len(chunks) >= 2
    tail = set(chunks[0].content.split()[-10:])
    head = set(chunks[1].content.split()[:60])
    assert tail & head, "consecutive chunks share no tokens, so they are not overlapping"


def test_page_number_is_preserved() -> None:
    chunks = chunk_blocks(
        [
            TextBlock(text="Alpha content about leave policy.", page=1),
            TextBlock(text="Beta content about remote work.", page=2),
        ],
        chunk_size=400,
        chunk_overlap=80,
    )

    assert [chunk.page for chunk in chunks] == [1, 2]
    assert "leave policy" in chunks[0].content
    assert "remote work" in chunks[1].content


def test_page_number_survives_splitting() -> None:
    """A page that splits into several chunks must label all of them with that page."""
    chunks = chunk_blocks(
        [TextBlock(text=LONG_PARAGRAPHS, page=7)],
        chunk_size=400,
        chunk_overlap=80,
    )

    assert len(chunks) > 1
    assert {chunk.page for chunk in chunks} == {7}


def test_section_is_preserved() -> None:
    chunks = chunk_blocks(
        [TextBlock(text="Some body text.", section="Handbook > Leave")],
        chunk_size=400,
        chunk_overlap=80,
    )

    assert chunks[0].section == "Handbook > Leave"


def test_blank_blocks_are_skipped() -> None:
    chunks = chunk_blocks(
        [TextBlock(text="   \n\n  "), TextBlock(text="real content")],
        chunk_size=400,
        chunk_overlap=80,
    )

    assert len(chunks) == 1
    assert chunks[0].content == "real content"


def test_chunks_are_stripped() -> None:
    chunks = chunk_blocks(
        [TextBlock(text="\n\n   padded text   \n\n")],
        chunk_size=400,
        chunk_overlap=80,
    )

    assert chunks[0].content == "padded text"


def test_empty_input_produces_no_chunks() -> None:
    assert chunk_blocks([], chunk_size=400, chunk_overlap=80) == []


def test_unbroken_token_is_hard_split() -> None:
    """Pathological input (one huge token, e.g. base64) must not produce one giant chunk."""
    chunks = chunk_blocks([TextBlock(text="x" * 2000)], chunk_size=400, chunk_overlap=80)

    assert len(chunks) > 1
    assert all(chunk.char_count <= 400 for chunk in chunks)


def test_table_header_is_repeated_in_every_split_chunk() -> None:
    """A row without its column names is unreadable and unsearchable."""
    header = "region | product | units | revenue"
    rows = "\n".join(f"R{index} | Widget | {index} | {index * 10}" for index in range(1, 200))

    chunks = chunk_blocks(
        [TextBlock(text=f"{header}\n{rows}", table_header=header)],
        chunk_size=200,
        chunk_overlap=20,
    )

    assert len(chunks) > 1
    assert all(chunk.content.startswith(header) for chunk in chunks)


def test_table_chunks_stay_within_the_size_budget() -> None:
    header = "name | value"
    rows = "\n".join(f"row-{index} | {index}" for index in range(1, 100))

    chunks = chunk_blocks(
        [TextBlock(text=f"{header}\n{rows}", table_header=header)],
        chunk_size=200,
        chunk_overlap=20,
    )

    assert len(chunks) > 1
    assert all(chunk.char_count <= 200 for chunk in chunks)


def test_overlap_larger_than_chunk_size_is_rejected() -> None:
    """This combination never advances the window, so splitting would not terminate."""
    with pytest.raises(ValueError, match="chunk_overlap must be smaller"):
        build_splitter(chunk_size=100, chunk_overlap=100)


def test_negative_overlap_is_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        build_splitter(chunk_size=100, chunk_overlap=-1)


def test_zero_chunk_size_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        build_splitter(chunk_size=0, chunk_overlap=0)
