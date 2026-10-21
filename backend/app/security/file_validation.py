"""Validation for uploaded files.

Uploads are the most exposed part of the API, so nothing about them is taken on
trust. Three independent checks are applied, because each catches a different
attack:

1. **Extension allowlist** - cheap rejection of obviously unsupported types.
2. **Magic-byte / structure sniffing** - a file called ``report.pdf`` that is
   actually a Windows executable is rejected. Extensions and ``Content-Type``
   headers are attacker-controlled; the bytes are not.
3. **Size limit** - enforced by reading at most ``max_bytes + 1`` bytes, so a
   huge upload cannot exhaust memory before we notice.

The filename is reduced to a single printable component and is then only the last
segment of a storage key whose first two segments are server-generated ids. A
client string therefore cannot choose where anything is written: path traversal
is removed as a category rather than filtered for.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath

from app.core.errors import AppError


class UnsupportedFileTypeError(AppError):
    status_code = 415
    code = "unsupported_file_type"
    public_message = "That file type is not supported."


class FileTooLargeError(AppError):
    status_code = 413
    code = "file_too_large"
    public_message = "That file is larger than the configured upload limit."


class InvalidFileError(AppError):
    status_code = 400
    code = "invalid_file"
    public_message = "That file appears to be damaged or is not the type it claims to be."


@dataclass(frozen=True)
class FileType:
    """A supported upload type."""

    extension: str
    label: str
    sniff: str
    mime_type: str


SUPPORTED_TYPES: dict[str, FileType] = {
    ".pdf": FileType(".pdf", "PDF document", "pdf", "application/pdf"),
    ".docx": FileType(
        ".docx",
        "Word document",
        "zip:word/document.xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    ".txt": FileType(".txt", "Plain text", "text", "text/plain"),
    ".md": FileType(".md", "Markdown", "text", "text/markdown"),
    ".markdown": FileType(".markdown", "Markdown", "text", "text/markdown"),
    ".csv": FileType(".csv", "CSV spreadsheet", "text", "text/csv"),
    ".xlsx": FileType(
        ".xlsx",
        "Excel workbook",
        "zip:xl/workbook.xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
}

MAX_EXTENSION_LENGTH = 12
PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"
MAX_FILENAME_LENGTH = 255


def normalize_filename(raw: str | None) -> str:
    """Reduce a client-supplied filename to a safe display name.

    Only the final path component is kept, so ``../../../etc/passwd`` becomes
    ``passwd``. This value is used for display and for messages; it is never used
    to construct a storage path.
    """
    if raw is None or not raw.strip():
        raise InvalidFileError("The upload is missing a filename.")

    normalized = raw.replace("\\", "/").strip()
    name = PurePosixPath(normalized).name

    name = "".join(char for char in name if char.isprintable()).strip().lstrip(".")

    if not name:
        raise InvalidFileError("The upload has no usable filename.")
    if len(name) > MAX_FILENAME_LENGTH:
        raise InvalidFileError(f"The filename is longer than {MAX_FILENAME_LENGTH} characters.")

    return name


def detect_extension(filename: str) -> str:
    """Return the lower-cased extension, validating its shape."""
    name = normalize_filename(filename)
    suffix = PurePosixPath(name).suffix.lower()

    if not suffix:
        supported = ", ".join(sorted(SUPPORTED_TYPES))
        raise UnsupportedFileTypeError(
            f"'{name}' has no file extension. Supported types: {supported}."
        )
    if len(suffix) > MAX_EXTENSION_LENGTH:
        raise UnsupportedFileTypeError(f"'{suffix}' is not a valid file extension.")

    if suffix not in SUPPORTED_TYPES:
        supported = ", ".join(sorted(SUPPORTED_TYPES))
        raise UnsupportedFileTypeError(
            f"'{suffix}' files are not supported. Supported types: {supported}."
        )
    return suffix


def _looks_like_text(data: bytes) -> bool:
    """True when the bytes plausibly are text rather than a binary blob.

    NUL bytes essentially never appear in text files but are everywhere in
    binaries. Checking the head of a large file is enough - a binary disguising
    its first 8 KB as text would still fail to parse later, and the parsers
    report that as a normal processing failure.
    """
    return b"\x00" not in data[:8192]


def _looks_like_zip_member(data: bytes, member: str) -> bool:
    """True when ``data`` is a ZIP archive containing ``member``.

    Trusting the ``PK`` signature alone would accept any ZIP - a renamed ``.jar``
    or an archive bomb. Requiring the OOXML part that the parser needs means the
    file is genuinely the type it claims.
    """
    if not data.startswith(ZIP_MAGIC):
        return False
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            return member in archive.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


def validate_upload(*, filename: str | None, data: bytes, max_bytes: int) -> FileType:
    """Validate an upload's name, size and contents.

    Returns the matched :class:`FileType`.

    Raises:
        InvalidFileError: empty, oversized-in-name, or content does not match.
        UnsupportedFileTypeError: extension is not allowlisted.
        FileTooLargeError: exceeds ``max_bytes``.
    """
    safe_name = normalize_filename(filename)
    file_type = SUPPORTED_TYPES[detect_extension(safe_name)]

    if not data:
        raise InvalidFileError("The uploaded file is empty.")

    if len(data) > max_bytes:
        limit_mb = max_bytes / (1024 * 1024)
        raise FileTooLargeError(f"The file is larger than the {limit_mb:.0f} MB limit.")

    if file_type.sniff == "pdf":
        if PDF_MAGIC not in data[:1024]:
            raise InvalidFileError(f"'{safe_name}' has a .pdf extension but is not a PDF file.")

    elif file_type.sniff.startswith("zip:"):
        member = file_type.sniff.split(":", 1)[1]
        if not _looks_like_zip_member(data, member):
            raise InvalidFileError(
                f"'{safe_name}' has a {file_type.extension} extension but is not a valid "
                f"{file_type.label}."
            )

    elif file_type.sniff == "text" and not _looks_like_text(data):
        raise InvalidFileError(
            f"'{safe_name}' has a {file_type.extension} extension but contains binary data."
        )

    return file_type
