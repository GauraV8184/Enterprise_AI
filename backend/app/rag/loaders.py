"""Extract text from supported file types.

Every parser returns ``TextBlock`` objects that carry **provenance** (page number,
section heading). Chunking happens later and inherits that provenance, which is
what makes citations like "handbook.pdf, page 7" possible instead of just
"handbook.pdf".

Design notes:

* For a page-based format (PDF) one block is one page, so page numbers stay
  accurate through chunking.
* For row-based formats (CSV/XLSX) the header row is repeated in every emitted
  block. Without that, a chunk describing row 4,000 would be a list of values
  with no column names and would match very little.
* Nothing here guesses. A scanned PDF has no text layer, and OCR is not
  implemented - that is reported as a specific failure rather than an empty
  document that silently answers nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.core.errors import AppError
from app.core.logging import get_logger, safe_extra
from app.security.file_validation import SUPPORTED_TYPES

logger = get_logger(__name__)

#: Rows per emitted block for tabular files. Each block re-states the header.
ROWS_PER_TABLE_BLOCK = 50

#: Hard ceiling on rows read from one sheet/file. A malformed "CSV" that is
#: really 20 million rows would otherwise be read entirely into memory.
MAX_TABLE_ROWS = 200_000

#: Text shorter than this from a whole PDF page is treated as "no text layer".
MIN_PDF_TEXT_PER_PAGE = 8


class DocumentParseError(AppError):
    """The file is a valid type but its contents could not be extracted."""

    status_code = 422
    code = "document_parse_failed"
    public_message = "That document could not be read. It may be damaged or password protected."


class NoExtractableTextError(AppError):
    """The file parsed cleanly but contained no usable text."""

    status_code = 422
    code = "no_extractable_text"
    public_message = "No text could be extracted from that document."


@dataclass(frozen=True)
class TextBlock:
    """A contiguous run of text with its origin."""

    text: str
    #: 1-based page number, when the format has pages.
    page: int | None = None
    #: Nearest preceding heading or sheet name, when the format has one.
    section: str | None = None


@dataclass(frozen=True)
class LoadedDocument:
    """Extracted content plus a summary of what was read."""

    blocks: list[TextBlock] = field(default_factory=list)
    page_count: int | None = None

    @property
    def char_count(self) -> int:
        return sum(len(block.text) for block in self.blocks)


class _HeadingStack:
    """Tracks nested headings so sections keep their ancestry.

    "Incident response" on its own is ambiguous; "Operations Guide > Incident
    response" is not. Keeping the full path in the section metadata gives
    retrieval something meaningful to show and the model something meaningful to
    cite, especially when two documents both have a "Overview" heading.
    """

    def __init__(self) -> None:
        self._entries: list[tuple[int, str]] = []

    def push(self, level: int, text: str) -> None:
        # A heading at the same or shallower level closes everything deeper.
        while self._entries and self._entries[-1][0] >= level:
            self._entries.pop()
        self._entries.append((level, text))

    @property
    def section(self) -> str | None:
        if not self._entries:
            return None
        return " > ".join(text for _, text in self._entries)


def _decode_text(data: bytes) -> str:
    """Decode text bytes, preferring UTF-8 then common Windows encodings.

    ``latin-1`` is the final fallback because it cannot fail - it maps every byte.
    That guarantees a text file always yields *something* rather than raising on
    a stray byte.
    """
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def _load_pdf(path: Path) -> LoadedDocument:
    import pymupdf

    try:
        document = pymupdf.open(path)
    except Exception as exc:  # pymupdf raises a variety of low-level errors
        raise DocumentParseError(f"Could not open the PDF: {exc}") from exc

    try:
        if document.needs_pass:
            raise DocumentParseError("That PDF is password protected.")

        blocks: list[TextBlock] = []
        for page_index in range(document.page_count):
            text = document.load_page(page_index).get_text("text").strip()
            if len(text) >= MIN_PDF_TEXT_PER_PAGE:
                blocks.append(TextBlock(text=text, page=page_index + 1))

        return LoadedDocument(blocks=blocks, page_count=document.page_count)
    finally:
        document.close()


def _iter_docx_blocks(document: object):
    """Yield paragraphs and tables in document order.

    ``document.paragraphs`` and ``document.tables`` are separate collections, so
    reading them separately loses the interleaving - a table would always appear
    after all prose. Walking the XML body preserves the real order.
    """
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def _load_docx(path: Path) -> LoadedDocument:
    from docx import Document
    from docx.table import Table

    try:
        document = Document(str(path))
    except Exception as exc:
        raise DocumentParseError(f"Could not open the DOCX file: {exc}") from exc

    blocks: list[TextBlock] = []
    headings = _HeadingStack()
    buffer: list[str] = []

    def flush() -> None:
        text = "\n".join(buffer).strip()
        if text:
            blocks.append(TextBlock(text=text, section=headings.section))
        buffer.clear()

    for item in _iter_docx_blocks(document):
        if isinstance(item, Table):
            rows = [" | ".join(cell.text.strip() for cell in row.cells) for row in item.rows]
            table_text = "\n".join(row for row in rows if row.strip())
            if table_text:
                buffer.append(table_text)
            continue

        text = item.text.strip()
        if not text:
            continue

        style_name = getattr(item.style, "name", "") or ""
        level = _docx_heading_level(style_name)
        if level is not None:
            # A new heading closes the previous section.
            flush()
            headings.push(level, text)
            continue

        buffer.append(text)

    flush()
    return LoadedDocument(blocks=blocks, page_count=None)


def _docx_heading_level(style_name: str) -> int | None:
    """Map a Word paragraph style to a heading level, or ``None`` if not a heading."""
    if style_name == "Title":
        return 0
    if style_name.startswith("Heading"):
        suffix = style_name.removeprefix("Heading").strip()
        # Word styles are "Heading 1".."Heading 9"; custom names may not carry a number.
        return int(suffix) if suffix.isdigit() else 1
    return None


def _load_markdown(path: Path) -> LoadedDocument:
    """Plain-text read, with ``#`` headings tracked as nested sections."""
    raw = _decode_text(path.read_bytes())
    blocks: list[TextBlock] = []
    headings = _HeadingStack()
    buffer: list[str] = []

    def flush() -> None:
        text = "\n".join(buffer).strip()
        if text:
            blocks.append(TextBlock(text=text, section=headings.section))
        buffer.clear()

    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            marker, _, title = stripped.partition(" ")
            if set(marker) == {"#"} and title.strip():
                flush()
                headings.push(len(marker), title.strip())
                continue
        buffer.append(line)

    flush()
    return LoadedDocument(blocks=blocks, page_count=None)


def _load_text(path: Path) -> LoadedDocument:
    text = _decode_text(path.read_bytes()).strip()
    if not text:
        return LoadedDocument(blocks=[], page_count=None)
    return LoadedDocument(blocks=[TextBlock(text=text)], page_count=None)


def _table_to_blocks(
    frame, *, section: str | None, rows_per_block: int = ROWS_PER_TABLE_BLOCK
) -> list[TextBlock]:
    """Render a dataframe as header-prefixed row groups.

    ``frame`` is a pandas DataFrame (typed loosely to keep pandas off the import
    path for non-tabular uploads).
    """
    columns = [str(column) for column in frame.columns]
    header = " | ".join(columns)

    rows = frame.head(MAX_TABLE_ROWS).astype(object).where(frame.notna(), "")
    rendered: list[str] = []
    for record in rows.itertuples(index=False, name=None):
        cells = [str(value).strip() for value in record]
        rendered.append(" | ".join(cells))

    blocks: list[TextBlock] = []
    for start in range(0, len(rendered), rows_per_block):
        group = rendered[start : start + rows_per_block]
        if not group:
            continue
        blocks.append(
            TextBlock(
                text="\n".join([header, *group]),
                section=section,
            )
        )
    return blocks


def _read_csv_best_effort(path: Path):
    """Read a CSV, detecting the separator when the default guess is wrong.

    ``pandas.read_csv`` assumes a comma. Given ``region;units`` headers - which
    Excel produces under many European locale settings - it does not fail, it
    returns a single column literally named ``region;units``. The document then
    indexes as one unsearchable string. So a one-column frame whose header contains
    another common separator is re-read with that separator.
    """
    import pandas as pd

    frame = pd.read_csv(path)

    if frame.shape[1] == 1:
        header = str(frame.columns[0])
        for separator in (";", "\t", "|"):
            if separator in header:
                retry = pd.read_csv(path, sep=separator)
                if retry.shape[1] > 1:
                    return retry

    return frame


def _load_csv(path: Path) -> LoadedDocument:
    import pandas as pd

    try:
        frame = _read_csv_best_effort(path)
    except Exception:
        # Fall back to delimiter sniffing for quoting/encoding styles pandas' C
        # parser rejects but its Python parser accepts.
        try:
            frame = pd.read_csv(path, sep=None, engine="python")
        except Exception as exc:
            raise DocumentParseError(f"Could not read the CSV file: {exc}") from exc

    return LoadedDocument(blocks=_table_to_blocks(frame, section=None), page_count=None)


def _load_xlsx(path: Path) -> LoadedDocument:
    import pandas as pd

    try:
        sheets = pd.read_excel(path, sheet_name=None)
    except Exception as exc:
        raise DocumentParseError(f"Could not read the Excel file: {exc}") from exc

    blocks: list[TextBlock] = []
    for sheet_name, frame in sheets.items():
        blocks.extend(_table_to_blocks(frame, section=str(sheet_name)))

    return LoadedDocument(blocks=blocks, page_count=None)


_PARSERS = {
    ".pdf": _load_pdf,
    ".docx": _load_docx,
    ".md": _load_markdown,
    ".markdown": _load_markdown,
    ".txt": _load_text,
    ".csv": _load_csv,
    ".xlsx": _load_xlsx,
}


def _path_variants(path: Path) -> list[str]:
    """Every textual form the same path can take in a third-party error message.

    Library errors quote the path they were given, and the quoting differs:
    pymupdf escapes backslashes (``C:\\\\dir\\\\file.pdf``), others use forward
    slashes. Longest first so a full path is replaced before any prefix of it.
    """
    raw = str(path)
    variants = {
        raw,
        raw.replace("\\", "/"),
        raw.replace("\\", "\\\\"),
        path.as_posix(),
        repr(raw)[1:-1],  # repr's own escaping, minus the surrounding quotes
    }
    return sorted((variant for variant in variants if variant), key=len, reverse=True)


# Safety net for escaping styles not anticipated above. Requires a drive letter
# followed by a backslash, so it cannot match a URL ("https://...") or prose.
_WINDOWS_ABSOLUTE_PATH = re.compile(r"[A-Za-z]:\\{1,2}(?:[^'\"\s\\/]+[\\/]{1,2})*[^'\"\s\\/]*")


def _sanitize_message(text: str, *, path: Path, display_name: str) -> str:
    """Replace server-side paths in a library error with the display filename.

    Third-party parsers include the path they were given in their error messages.
    That path is the internal storage location (``data/uploads/<uuid>.pdf``), which
    the user neither knows nor needs. Messages become the HTTP ``detail`` and are
    stored on the document record, so they are sanitised before either.
    """
    for variant in _path_variants(path):
        text = text.replace(variant, display_name)

    # Belt and braces: if some escaping style still left an absolute path behind,
    # redact it rather than leak it.
    return _WINDOWS_ABSOLUTE_PATH.sub(display_name, text)


def load_document(path: Path, *, filename: str | None = None) -> LoadedDocument:
    """Parse a stored file into text blocks.

    ``filename`` is used only for the extension and for error messages; the bytes
    always come from ``path``.

    Raises:
        DocumentParseError: the format is fine but extraction failed.
        NoExtractableTextError: extraction succeeded but produced no text.
    """
    display_name = filename or path.name
    extension = Path(display_name).suffix.lower()
    if extension not in SUPPORTED_TYPES:
        supported = ", ".join(sorted(SUPPORTED_TYPES))
        raise DocumentParseError(f"'{extension}' files cannot be read. Supported: {supported}.")

    parser = _PARSERS[extension]
    try:
        loaded = parser(path)
    except DocumentParseError as exc:
        raise DocumentParseError(
            _sanitize_message(exc.message, path=path, display_name=display_name)
        ) from exc

    if not loaded.blocks:
        if extension == ".pdf":
            raise NoExtractableTextError(
                f"No text layer was found in '{display_name}'. It is probably a scanned "
                "image; OCR is not implemented yet."
            )
        raise NoExtractableTextError(f"'{display_name}' contains no readable text.")

    logger.info(
        "document_parsed",
        extra=safe_extra(
            filename=display_name,
            extension=extension,
            blocks=len(loaded.blocks),
            pages=loaded.page_count,
            characters=loaded.char_count,
        ),
    )
    return loaded


def load_document_from_bytes(data: bytes, *, filename: str, extension: str) -> LoadedDocument:
    """Convenience wrapper for callers that hold bytes rather than a path.

    Writes to a temporary file because the parsers are path-based (the Office and
    PDF libraries want seekable input, and some formats need a real filename).
    """
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        temp_path = Path(directory) / f"upload{extension}"
        temp_path.write_bytes(data)
        return load_document(temp_path, filename=filename)
