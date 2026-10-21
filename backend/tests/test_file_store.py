"""The storage key layout and the local backend.

These tests are deliberately about *where* bytes go, not just that they round
trip: the key is what the Supabase policies check, so a layout change is a
security change.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.storage.base import InvalidObjectKeyError, object_key, storage_filename
from app.storage.errors import FileStoreObjectMissingError
from app.storage.local import LocalFileStore

OWNER = "8f14e45fceea167a5a36dedd4bea2543"
DOCUMENT = "3c9a7b21f0e14f4b9c0d0e9f1a2b3c4d"
FILENAME = "q3-report.txt"


def test_key_is_owner_document_filename() -> None:
    assert (
        object_key(owner_id=OWNER, document_id=DOCUMENT, filename=FILENAME)
        == f"{OWNER}/{DOCUMENT}/{FILENAME}"
    )


def test_put_read_delete_round_trip(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)

    key = store.put(
        owner_id=OWNER,
        document_id=DOCUMENT,
        filename=FILENAME,
        data=b"the quarterly numbers",
        content_type="text/plain",
    )

    assert key == f"{OWNER}/{DOCUMENT}/{FILENAME}"
    assert store.read(owner_id=OWNER, document_id=DOCUMENT, filename=FILENAME) == (
        b"the quarterly numbers"
    )
    assert (
        store.path_for(owner_id=OWNER, document_id=DOCUMENT, filename=FILENAME)
        == tmp_path / OWNER / DOCUMENT / FILENAME
    )

    store.delete(owner_id=OWNER, document_id=DOCUMENT, filename=FILENAME)

    assert not (tmp_path / OWNER).exists()


def test_delete_is_idempotent(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    store.put(
        owner_id=OWNER,
        document_id=DOCUMENT,
        filename=FILENAME,
        data=b"x",
        content_type="text/plain",
    )

    store.delete(owner_id=OWNER, document_id=DOCUMENT, filename=FILENAME)
    store.delete(owner_id=OWNER, document_id=DOCUMENT, filename=FILENAME)


def test_reading_a_missing_object_is_a_typed_failure(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)

    with pytest.raises(FileStoreObjectMissingError):
        store.read(owner_id=OWNER, document_id=DOCUMENT, filename=FILENAME)


@pytest.mark.parametrize(
    "filename",
    [
        "../escape.txt",
        "nested/file.txt",
        "nested\\file.txt",
        "..",
        "",
        "bad\x00name.txt",
        "bad\nname.txt",
    ],
)
def test_a_hostile_filename_never_reaches_the_key_verbatim(filename: str) -> None:
    """Separators and reserved segments are removed, not passed through.

    The filename is sanitised rather than rejected because it is only the last
    segment of a key whose first two segments are server-generated ids. The
    property that matters is that it cannot add a segment or climb out.
    """
    key = object_key(owner_id=OWNER, document_id=DOCUMENT, filename=filename)
    owner, document, name = key.split("/")

    assert (owner, document) == (OWNER, DOCUMENT)
    assert "/" not in name
    assert "\\" not in name
    assert name not in {"", ".", ".."}


@pytest.mark.parametrize("owner_id", ["..", "", "a/b", "a\\b"])
def test_an_owner_id_cannot_escape_the_root(owner_id: str) -> None:
    """Owner ids come from a token, so an invalid one is a bug and must be loud."""
    with pytest.raises(InvalidObjectKeyError):
        object_key(owner_id=owner_id, document_id=DOCUMENT, filename=FILENAME)


def test_storage_filename_keeps_the_extension_and_stays_readable() -> None:
    assert storage_filename("Q3 report #1.txt") == "Q3-report-1.txt"
    assert storage_filename("REPORT") == "REPORT"
    assert storage_filename("报告.pdf") == "file.pdf"


def test_an_unusual_filename_still_round_trips(tmp_path: Path) -> None:
    """The display name is the caller's; the key is the sanitised one."""
    store = LocalFileStore(tmp_path)
    name = "Q3 report #1.txt"

    store.put(
        owner_id=OWNER, document_id=DOCUMENT, filename=name, data=b"x", content_type="text/plain"
    )

    assert store.read(owner_id=OWNER, document_id=DOCUMENT, filename=name) == b"x"
    assert store.path_for(owner_id=OWNER, document_id=DOCUMENT, filename=name) == (
        tmp_path / OWNER / DOCUMENT / "Q3-report-1.txt"
    )
    assert not (tmp_path / OWNER / DOCUMENT / name).exists()
