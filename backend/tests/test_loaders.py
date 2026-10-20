"""Document parsers: one real generated file per supported format."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.core.vectors import cosine_similarity_matrix
from app.rag.loaders import (
    DocumentParseError,
    NoExtractableTextError,
    load_document,
)
from tests.sample_files import (
    REVENUE_REPORT_TEXT,
    write_csv_file,
    write_docx_file,
    write_fake_pdf,
    write_markdown_file,
    write_pdf_file,
    write_scanned_style_pdf,
    write_text_file,
    write_xlsx_file,
)

# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------


def test_pdf_pages_keep_their_numbers(documents_dir: Path) -> None:
    """Page provenance is what makes a citation verifiable."""
    path = write_pdf_file(documents_dir, pages=2)
    loaded = load_document(path)

    assert loaded.page_count == 2
    assert [block.page for block in loaded.blocks] == [1, 2]


def test_pdf_text_is_actually_extracted(documents_dir: Path) -> None:
    loaded = load_document(write_pdf_file(documents_dir, pages=3))

    assert "twenty days of paid annual leave" in loaded.blocks[0].text
    assert "work remotely" in loaded.blocks[1].text
    # Page 3 comes from a different source document - proving pages are not copies.
    assert "4,820,000" in loaded.blocks[2].text
    assert REVENUE_REPORT_TEXT.split()[1] in loaded.blocks[2].text


def test_scanned_pdf_is_reported_as_having_no_text(documents_dir: Path) -> None:
    """A PDF with no text layer must fail loudly, not index as empty."""
    with pytest.raises(NoExtractableTextError) as excinfo:
        load_document(write_scanned_style_pdf(documents_dir))
    assert "no text layer" in str(excinfo.value).lower()


def test_pdf_parser_failure_does_not_leak_the_storage_path(documents_dir: Path) -> None:
    """Library errors embed the path they were handed; users must not see it."""
    path = write_fake_pdf(documents_dir)

    with pytest.raises(DocumentParseError) as excinfo:
        load_document(path, filename="quarterly.pdf")

    message = str(excinfo.value)
    assert str(path) not in message
    assert str(documents_dir) not in message
    assert "quarterly.pdf" in message


# --------------------------------------------------------------------------
# Plain text and Markdown
# --------------------------------------------------------------------------


def test_text_file_is_one_block(documents_dir: Path) -> None:
    loaded = load_document(write_text_file(documents_dir))

    assert len(loaded.blocks) == 1
    assert loaded.page_count is None
    assert "twenty days" in loaded.blocks[0].text


def test_cp1252_text_file_is_decoded(documents_dir: Path) -> None:
    """An accented byte must not turn a readable file into a parse failure."""
    path = documents_dir / "legacy.txt"
    path.write_bytes("Caf\xe9 costs \xa34".encode("latin-1"))

    loaded = load_document(path)
    assert "Caf" in loaded.blocks[0].text


def test_markdown_headings_become_nested_sections(documents_dir: Path) -> None:
    """Section names keep their ancestry so the same heading in two files differs."""
    loaded = load_document(write_markdown_file(documents_dir))

    sections = [block.section for block in loaded.blocks]
    assert sections == [
        "Operations Guide > Incident response",
        "Operations Guide > Change management",
    ]
    assert "fifteen minutes" in loaded.blocks[0].text
    # The heading text belongs to the section label, not to the body.
    assert not loaded.blocks[0].text.startswith("#")


def test_empty_text_file_has_no_blocks(documents_dir: Path) -> None:
    path = documents_dir / "blank.txt"
    path.write_text("   \n\n  ", encoding="utf-8")
    with pytest.raises(NoExtractableTextError):
        load_document(path)


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------


def test_csv_rows_become_text_with_headers(documents_dir: Path) -> None:
    loaded = load_document(write_csv_file(documents_dir))

    assert len(loaded.blocks) == 1
    text = loaded.blocks[0].text
    assert text.startswith("region | product | units | revenue")
    assert "North | Widget | 1200 | 48000" in text


def test_csv_header_repeats_across_blocks(documents_dir: Path) -> None:
    """A chunk without column names is unsearchable, so the header is repeated."""
    path = documents_dir / "wide.csv"
    rows = "\n".join(f"R{index},{index * 10}" for index in range(120))
    path.write_text(f"name,value\n{rows}\n", encoding="utf-8")

    loaded = load_document(path)

    assert len(loaded.blocks) == 3  # 120 rows / 50 per block
    for block in loaded.blocks:
        assert block.text.startswith("name | value")


def test_semicolon_csv_is_detected(documents_dir: Path) -> None:
    """European Excel exports use semicolons; a naive split yields one column."""
    path = documents_dir / "euro.csv"
    path.write_text("region;units\nNorth;1200\nSouth;850\n", encoding="utf-8")

    loaded = load_document(path)

    assert loaded.blocks[0].text.startswith("region | units")
    assert "North | 1200" in loaded.blocks[0].text


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------


def test_xlsx_sheets_become_sections(documents_dir: Path) -> None:
    loaded = load_document(write_xlsx_file(documents_dir))

    sections = {block.section for block in loaded.blocks}
    assert sections == {"Annual Budget", "Spend"}

    budget = next(block for block in loaded.blocks if block.section == "Annual Budget")
    assert budget.text.startswith("department | headcount | budget_usd")
    assert "Engineering | 24 | 1800000" in budget.text

    spend = next(block for block in loaded.blocks if block.section == "Spend")
    assert "Q3 | 705000" in spend.text


def test_xlsx_missing_values_are_blank_not_nan(documents_dir: Path) -> None:
    """``NaN`` in indexed text is pure noise and would match nothing useful."""
    import pandas as pd

    path = documents_dir / "gaps.xlsx"
    with pd.ExcelWriter(path) as writer:
        pd.DataFrame({"name": ["alpha", None], "value": [1, 2]}).to_excel(
            writer, sheet_name="Sheet1", index=False
        )

    loaded = load_document(path)
    assert "nan" not in loaded.blocks[0].text.lower()


# --------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------


def test_docx_headings_become_sections(documents_dir: Path) -> None:
    loaded = load_document(write_docx_file(documents_dir))

    sections = [block.section for block in loaded.blocks]
    assert "Security Policy" in sections
    # A sub-heading keeps its parent so it is unambiguous on its own.
    assert "Security Policy > Device encryption" in sections


def test_docx_tables_are_extracted(documents_dir: Path) -> None:
    loaded = load_document(write_docx_file(documents_dir))
    text = "\n".join(block.text for block in loaded.blocks)

    assert "Access review | IT Security" in text
    assert "Backup testing | Platform Team" in text


def test_docx_preserves_element_order(documents_dir: Path) -> None:
    """Tables must read where they appear, not after all the prose.

    Reading ``document.paragraphs`` and ``document.tables`` separately silently
    reorders the document. This asserts the table lands in its real position.
    """
    loaded = load_document(write_docx_file(documents_dir))

    prose_index = next(
        index for index, block in enumerate(loaded.blocks) if "ninety days" in block.text
    )
    table_index = next(
        index for index, block in enumerate(loaded.blocks) if "Access review" in block.text
    )
    assert table_index > prose_index


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


def test_unsupported_extension_is_rejected_by_the_loader(documents_dir: Path) -> None:
    path = documents_dir / "archive.zip"
    path.write_bytes(b"PK\x03\x04")

    with pytest.raises(DocumentParseError) as excinfo:
        load_document(path)
    assert ".zip" in str(excinfo.value)


# --------------------------------------------------------------------------
# Cosine similarity
# --------------------------------------------------------------------------


def test_cosine_similarity_ranks_by_direction() -> None:
    matrix = np.array([[1.0, 0.0], [0.0, 1.0], [0.9, 0.1]], dtype=np.float32)
    scores = cosine_similarity_matrix(matrix, np.array([1.0, 0.0], dtype=np.float32))

    assert scores[0] > scores[2] > scores[1]


# --------------------------------------------------------------------------
# The fake embedding's own guarantees
# --------------------------------------------------------------------------


def test_fake_embedding_prefers_shared_vocabulary() -> None:
    """Guards the test double, not the app.

    Every ranking assertion in the suite rests on this property. If a future change
    makes the fake less faithful, those tests would keep passing while measuring
    nothing, which is worse than failing.
    """
    from tests.fakes import HashingEmbeddings

    embeddings = HashingEmbeddings()
    related = "All laptops must have full-disk encryption enabled before issue."
    unrelated = "Regional revenue grew by twelve percent in the third quarter."

    query = embeddings.embed_query("full-disk encryption for laptops")
    scores = np.array(
        [
            float(np.dot(query, embeddings.embed_query(related))),
            float(np.dot(query, embeddings.embed_query(unrelated))),
        ]
    )

    assert scores[0] > scores[1]
    # A genuine signal, not a hairline margin: three shared terms should dominate
    # any accidental hash collision.
    assert scores[0] > scores[1] * 2


def test_fake_embedding_is_deterministic_across_instances() -> None:
    """Two instances must agree, or stored vectors would not be comparable."""
    from tests.fakes import HashingEmbeddings

    first = HashingEmbeddings().embed_query("annual leave policy")
    second = HashingEmbeddings().embed_query("annual leave policy")

    assert first == second


def test_cosine_similarity_of_empty_matrix_is_empty() -> None:
    assert cosine_similarity_matrix(np.empty((0, 0), dtype=np.float32), np.array([1.0])).size == 0


def test_zero_norm_rows_score_zero_instead_of_nan() -> None:
    """NaN would sort unpredictably and poison any downstream max()."""
    matrix = np.array([[0.0, 0.0], [1.0, 0.0]], dtype=np.float32)
    scores = cosine_similarity_matrix(matrix, np.array([1.0, 0.0], dtype=np.float32))

    assert not np.isnan(scores).any()
    assert scores[0] == 0.0


def test_dimension_mismatch_raises_a_clear_error() -> None:
    with pytest.raises(ValueError, match="dimension mismatch"):
        cosine_similarity_matrix(
            np.zeros((2, 4), dtype=np.float32), np.array([1.0, 0.0], dtype=np.float32)
        )
