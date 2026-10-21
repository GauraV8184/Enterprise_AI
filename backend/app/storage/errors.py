"""Failures raised by a file store.

Separate from :mod:`app.database.errors` for the same reason those are separate
from :mod:`app.core.errors`: "the object store is unreachable" is a fact about the
deployment, not about the request, and an operator needs to recognise it quickly.

The messages follow the same rule as everywhere else: the storage provider's own
text is logged but never returned, because it can name buckets, policies or keys.
"""

from __future__ import annotations

from app.core.errors import AppError


class FileStoreUnavailableError(AppError):
    """The object store could not be reached or returned a server error."""

    status_code = 503
    code = "file_store_unavailable"
    public_message = "File storage is temporarily unavailable. Try again shortly."


class FileStoreNotReadyError(AppError):
    """The bucket is missing, so the deployment is incomplete.

    Distinct from "unavailable": retrying will never help until someone applies
    ``supabase/migrations/0002_storage_bucket.sql``.
    """

    status_code = 503
    code = "file_store_not_ready"
    public_message = (
        "File storage is not ready: the documents bucket has not been created. "
        "Run the migrations in supabase/migrations."
    )


class FileStoreObjectMissingError(AppError):
    """Metadata points at an object that is not there.

    This is a data-integrity problem, not a 404 for the client: the document row
    is owned by the caller, so the missing part is the bytes. Re-uploading is the
    only recovery, and the message says so.
    """

    status_code = 500
    code = "file_store_object_missing"
    public_message = (
        "The stored original for that document is missing. Delete the document and upload it again."
    )


class FileStoreError(AppError):
    """The store rejected an operation in an unexpected way."""

    status_code = 500
    code = "file_store_error"
    public_message = "The file store rejected the request. The server log has the detail."
