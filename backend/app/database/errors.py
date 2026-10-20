"""Failures raised by a document store.

These are deliberately separate from :mod:`app.core.errors`: a store failure is
an operational fact about the deployment ("the database is not reachable", "the
migrations were never applied"), and each one maps to a distinct status code so
an operator can tell them apart from a client bug.
"""

from __future__ import annotations

from app.core.errors import AppError


class DocumentStoreUnavailableError(AppError):
    """The store could not be reached or returned a server error.

    A 503, not a 500: the request was fine and retrying later may work.
    """

    status_code = 503
    code = "document_store_unavailable"
    public_message = "The document store is temporarily unavailable. Try again shortly."


class DocumentStoreNotReadyError(AppError):
    """The store is reachable but its schema is missing.

    This is a deployment error, not a request error, so it is loud in the log.
    It maps to 503 rather than 500 because the service is genuinely unable to
    serve until someone runs the migrations - and it is not the caller's fault.
    """

    status_code = 503
    code = "document_store_not_ready"
    public_message = (
        "The document store is not ready: its schema has not been applied. "
        "Run the migrations in supabase/migrations."
    )


class DocumentStoreError(AppError):
    """The store rejected a request in a way the application did not expect.

    The database's own message is logged but never returned: it can contain
    column names, constraint names and fragments of other tenants' data.
    """

    status_code = 500
    code = "document_store_error"
    public_message = "The document store rejected the request. The server log has the detail."


class EmbeddingDimensionMismatchError(AppError):
    """Stored vectors and the current embedding model disagree.

    This happens when ``EMBEDDING_MODEL`` changes after documents were indexed.
    Comparing the two would be meaningless, so the request fails with an
    explanation instead of returning silently bad search results.
    """

    status_code = 409
    code = "embedding_dimension_mismatch"

    def __init__(self, *, stored_dimensions: int, query_dimensions: int) -> None:
        super().__init__(
            f"Stored chunks were embedded with a {stored_dimensions}-dimensional model, but the "
            f"configured model produces {query_dimensions} dimensions. Re-upload the documents, or "
            "restore the previous EMBEDDING_MODEL."
        )
        self.public_message = self.message
