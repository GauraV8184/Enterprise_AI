"""Upload validation: extensions, size limits, spoofed content, unsafe names."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.security.file_validation import (
    SUPPORTED_TYPES,
    FileTooLargeError,
    InvalidFileError,
    UnsupportedFileTypeError,
    detect_extension,
    normalize_filename,
    validate_upload,
)
from tests.sample_files import (
    write_csv_file,
    write_fake_docx,
    write_fake_pdf,
    write_pdf_file,
    write_text_file,
    write_xlsx_file,
)

ONE_MB = 1024 * 1024


def test_path_components_are_discarded() -> None:
    """A traversal attempt must not survive into the display name."""
    assert normalize_filename("../../../etc/passwd") == "passwd"
    assert normalize_filename(r"..\..\Windows\System32\config.sys") == "config.sys"
    assert normalize_filename("/absolute/path/report.pdf") == "report.pdf"


def test_hidden_file_dots_are_stripped() -> None:
    assert normalize_filename("...secret.pdf") == "secret.pdf"


def test_control_characters_are_removed() -> None:
    assert normalize_filename("re\x00po\x07rt.pdf") == "report.pdf"


@pytest.mark.parametrize("bad", ["", "   ", None, "/", "..", "///", "./"])
def test_unusable_filenames_are_rejected(bad: str | None) -> None:
    with pytest.raises(InvalidFileError):
        normalize_filename(bad)


def test_directory_like_name_keeps_only_the_last_segment() -> None:
    """``/tmp/`` is a path, and its basename is a legitimate name.

    It is not rejected here - it has no extension, so ``detect_extension`` rejects
    it a moment later with a more accurate message.
    """
    assert normalize_filename("/tmp/") == "tmp"
    with pytest.raises(UnsupportedFileTypeError):
        detect_extension("/tmp/")


def test_overlong_filename_is_rejected() -> None:
    with pytest.raises(InvalidFileError):
        normalize_filename("a" * 300 + ".pdf")


@pytest.mark.parametrize("extension", sorted(SUPPORTED_TYPES))
def test_supported_extensions_are_recognised(extension: str) -> None:
    assert detect_extension(f"file{extension}") == extension


def test_extension_matching_is_case_insensitive() -> None:
    assert detect_extension("REPORT.PDF") == ".pdf"


def test_unknown_extension_lists_what_is_supported() -> None:
    with pytest.raises(UnsupportedFileTypeError) as excinfo:
        detect_extension("malware.exe")
    message = str(excinfo.value)
    assert ".exe" in message
    assert ".pdf" in message


def test_missing_extension_is_rejected() -> None:
    with pytest.raises(UnsupportedFileTypeError):
        detect_extension("README")


def test_oversized_upload_is_rejected() -> None:
    with pytest.raises(FileTooLargeError):
        validate_upload(filename="big.txt", data=b"x" * (ONE_MB + 1), max_bytes=ONE_MB)


def test_upload_at_the_limit_is_accepted() -> None:
    file_type = validate_upload(filename="exact.txt", data=b"x" * ONE_MB, max_bytes=ONE_MB)
    assert file_type.extension == ".txt"


def test_empty_upload_is_rejected() -> None:
    with pytest.raises(InvalidFileError):
        validate_upload(filename="empty.txt", data=b"", max_bytes=ONE_MB)


def test_genuine_files_pass_validation(documents_dir: Path) -> None:
    for path in (
        write_text_file(documents_dir),
        write_csv_file(documents_dir),
        write_xlsx_file(documents_dir),
        write_pdf_file(documents_dir),
    ):
        file_type = validate_upload(
            filename=path.name, data=path.read_bytes(), max_bytes=10 * ONE_MB
        )
        assert file_type.extension == path.suffix


def test_pdf_extension_with_text_content_is_rejected(documents_dir: Path) -> None:
    """The mislabelled file is the whole point: extensions prove nothing."""
    path = write_fake_pdf(documents_dir)
    with pytest.raises(InvalidFileError) as excinfo:
        validate_upload(filename=path.name, data=path.read_bytes(), max_bytes=10 * ONE_MB)
    assert "not a PDF" in str(excinfo.value)


def test_zip_that_is_not_a_docx_is_rejected(documents_dir: Path) -> None:
    """A real ZIP archive passes the signature check but is not a Word file."""
    path = write_fake_docx(documents_dir)
    with pytest.raises(InvalidFileError) as excinfo:
        validate_upload(filename=path.name, data=path.read_bytes(), max_bytes=10 * ONE_MB)
    assert "not a valid Word document" in str(excinfo.value)


def test_binary_content_with_text_extension_is_rejected() -> None:
    with pytest.raises(InvalidFileError) as excinfo:
        validate_upload(filename="data.txt", data=b"\x00\x01\x02binary", max_bytes=ONE_MB)
    assert "binary" in str(excinfo.value)


def test_text_with_high_bytes_is_still_accepted() -> None:
    """cp1252 bytes are not valid UTF-8 but are legitimate text."""
    with pytest.raises(InvalidFileError):
        validate_upload(filename="a.txt", data=b"", max_bytes=ONE_MB)
    assert (
        validate_upload(filename="a.txt", data=b"caf\xe9 na\xefve", max_bytes=ONE_MB).extension
        == ".txt"
    )
