"""Filesystem-backed storage for the SQLite development store.

This is the offline backend: no network, no credentials, and the test suite can
assert on real files. It deliberately mirrors the Supabase key layout
(``<owner>/<document>/<filename>``) so a bug in the layout is caught locally
rather than only against a live project.
"""

from __future__ import annotations

from pathlib import Path

from app.core.logging import get_logger, safe_extra
from app.storage.base import object_key
from app.storage.errors import FileStoreError, FileStoreObjectMissingError

logger = get_logger(__name__)


class LocalFileStore:
    """Store uploads under a root directory, one folder per owner and document."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        """Exposed for startup logging and tests; never sent to a client."""
        return self._root

    def path_for(self, *, owner_id: str, document_id: str, filename: str) -> Path:
        """The on-disk path for a key.

        Safe because :func:`object_key` rejects separators and reserved segments,
        and because the owner id comes from a verified token rather than from the
        request body.
        """
        return self._root / object_key(
            owner_id=owner_id, document_id=document_id, filename=filename
        )

    def put(
        self,
        *,
        owner_id: str,
        document_id: str,
        filename: str,
        data: bytes,
        content_type: str,
    ) -> str:
        path = self.path_for(owner_id=owner_id, document_id=document_id, filename=filename)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as exc:
            logger.error(
                "local_file_write_failed",
                extra=safe_extra(document_id=document_id, error=type(exc).__name__),
            )
            raise FileStoreError("The uploaded file could not be written to disk.") from exc

        return object_key(owner_id=owner_id, document_id=document_id, filename=filename)

    def read(self, *, owner_id: str, document_id: str, filename: str) -> bytes:
        path = self.path_for(owner_id=owner_id, document_id=document_id, filename=filename)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            logger.error(
                "local_file_missing",
                extra=safe_extra(document_id=document_id),
            )
            raise FileStoreObjectMissingError() from exc
        except OSError as exc:
            logger.error(
                "local_file_read_failed",
                extra=safe_extra(document_id=document_id, error=type(exc).__name__),
            )
            raise FileStoreError("The stored file could not be read.") from exc

    def delete(self, *, owner_id: str, document_id: str, filename: str) -> None:
        path = self.path_for(owner_id=owner_id, document_id=document_id, filename=filename)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            # The caller (document deletion) treats a failure to remove the
            # original as non-fatal, but it still deserves a log line.
            logger.error(
                "local_file_delete_failed",
                extra=safe_extra(document_id=document_id, error=type(exc).__name__),
            )
            raise FileStoreError("The stored file could not be deleted.") from exc

        # Best effort: an empty document folder is noise, but a non-empty one
        # means something else lives there and must stay.
        for directory in (path.parent, path.parent.parent):
            try:
                directory.rmdir()
            except OSError:
                break
