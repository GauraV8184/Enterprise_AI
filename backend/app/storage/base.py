"""The contract every file store implements, and the key layout they share.

**Byte-oriented on purpose.** Postgres never downloads a vector just to rank it;
similarly, an object store is asked for bytes, not for a path. Keeping the
interface at ``put``/``read``/``delete`` means the Supabase backend has no reason
to expose a local filesystem path, and the local backend cannot quietly become
the only one that works.

**One key layout for both backends.** ``<owner_id>/<document_id>/<filename>`` is
not just a Supabase convention: the storage policies in
``supabase/migrations/0002_storage_bucket.sql`` compare the first path segment to
``auth.uid()``. If the local backend used a different shape, the layout would only
be exercised in production, which is exactly where a mistake is most expensive.

The owner segment is written by the server from the verified token. The filename
is reduced to its final component by
:func:`app.security.file_validation.normalize_filename`, then to characters both
backends accept by :func:`storage_filename` - Supabase rejects a key containing
``#`` outright. Validation here is a second line of defence, not the primary one.
"""

from __future__ import annotations

import re
from typing import Protocol

_BAD_SEGMENTS = frozenset({"", ".", ".."})


class InvalidObjectKeyError(ValueError):
    """A key segment could escape its prefix or corrupt the layout."""


_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _checked_segment(value: str, *, label: str) -> str:
    if value in _BAD_SEGMENTS:
        raise InvalidObjectKeyError(f"The {label} is empty or a reserved path segment.")
    if "/" in value or "\\" in value or "\x00" in value:
        raise InvalidObjectKeyError(f"The {label} contains a path separator.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise InvalidObjectKeyError(f"The {label} contains control characters.")
    return value


def storage_filename(filename: str) -> str:
    """Reduce a display name to a key segment both backends accept.

    The database keeps the original name; only the object key is normalised. That
    split matters: ``q3 report #1.txt`` stays readable in the UI while the key
    becomes ``q3-report-1.txt``.

    The extension is preserved because the parsers use it to choose a reader, and
    the result is deterministic so the same name produces the same key on upload
    and on delete.
    """
    stem, dot, suffix = filename.rpartition(".")
    if not dot:
        stem, suffix = filename, ""

    cleaned_stem = _UNSAFE_KEY_CHARS.sub("-", stem).strip("._-") or "file"
    cleaned_suffix = _UNSAFE_KEY_CHARS.sub("", suffix)
    return f"{cleaned_stem}.{cleaned_suffix}" if cleaned_suffix else cleaned_stem


def object_key(*, owner_id: str, document_id: str, filename: str) -> str:
    """Build the single storage key used by every backend.

    The filename component is sanitised (see :func:`storage_filename`); the two id
    segments must already be safe. Callers pass server-generated ids, so a
    rejection means an invariant broke - it is a bug, not user input.

    Raises:
        InvalidObjectKeyError: a segment would escape its prefix.
    """
    return "/".join(
        (
            _checked_segment(owner_id, label="owner id"),
            _checked_segment(document_id, label="document id"),
            _checked_segment(storage_filename(filename), label="filename"),
        )
    )


class DocumentFileStore(Protocol):
    """Where original uploads live.

    Implementations must be safe to use concurrently: the local store is
    stateless, and the Supabase store carries one caller's token.
    """

    def put(
        self,
        *,
        owner_id: str,
        document_id: str,
        filename: str,
        data: bytes,
        content_type: str,
    ) -> str:
        """Store ``data`` and return its key.

        Overwriting the same key is allowed and must be idempotent: the key is
        derived from a server-generated document id, so a retry can only target
        the same document's own object.
        """
        ...

    def read(self, *, owner_id: str, document_id: str, filename: str) -> bytes:
        """Return the stored bytes.

        Raises:
            FileStoreObjectMissingError: nothing is stored at that key.
        """
        ...

    def delete(self, *, owner_id: str, document_id: str, filename: str) -> None:
        """Remove the object. Deleting a key that is already gone is not an error."""
        ...
