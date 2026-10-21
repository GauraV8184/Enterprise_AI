"""Where original uploads live.

``DocumentFileStore`` is the contract; the local filesystem and Supabase Storage
are the two implementations. Routers depend on the protocol, never on either
class, so the SQLite development path and the Postgres deployment path run the
same code.
"""

from app.storage.base import (
    DocumentFileStore,
    InvalidObjectKeyError,
    object_key,
    storage_filename,
)
from app.storage.local import LocalFileStore
from app.storage.supabase import SupabaseStorage

__all__ = [
    "DocumentFileStore",
    "InvalidObjectKeyError",
    "LocalFileStore",
    "SupabaseStorage",
    "object_key",
    "storage_filename",
]
