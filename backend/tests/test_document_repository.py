"""Document store: lifecycle, owner isolation, and vector bookkeeping."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.database.repositories import (
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_READY,
    ChunkRecord,
    DocumentRepository,
    new_id,
)

ALICE = "alice"
BOB = "bob"


@pytest.fixture
def repository(tmp_path: Path) -> DocumentRepository:
    return DocumentRepository(tmp_path / "store" / "app.db")


def make_document(repository: DocumentRepository, *, owner: str = ALICE, name: str = "notes.txt"):
    return repository.create_document(
        owner_id=owner,
        filename=name,
        content_type="text/plain",
        extension=".txt",
        size_bytes=42,
    )


def make_chunk(
    *,
    document_id: str,
    owner: str = ALICE,
    index: int = 0,
    content: str = "some content",
    embedding: list[float] | None = None,
) -> ChunkRecord:
    return ChunkRecord(
        id=new_id(),
        document_id=document_id,
        owner_id=owner,
        chunk_index=index,
        content=content,
        page=1,
        section=None,
        embedding=np.array(
            embedding if embedding is not None else [1.0, 0.0, 0.0], dtype=np.float32
        ),
    )


def test_created_document_starts_pending(repository: DocumentRepository) -> None:
    record = make_document(repository)

    assert record.status == "pending"
    assert record.chunk_count == 0
    assert record.indexed_at is None
    assert repository.get_document(owner_id=ALICE, document_id=record.id) == record


def test_status_transitions_to_ready(repository: DocumentRepository) -> None:
    record = make_document(repository)

    updated = repository.update_status(
        owner_id=ALICE,
        document_id=record.id,
        status=STATUS_READY,
        page_count=3,
        chunk_count=7,
        mark_indexed=True,
    )

    assert updated is True
    stored = repository.get_document(owner_id=ALICE, document_id=record.id)
    assert stored is not None
    assert stored.status == STATUS_READY
    assert stored.page_count == 3
    assert stored.chunk_count == 7
    assert stored.indexed_at is not None
    assert stored.error is None


def test_failure_records_the_reason(repository: DocumentRepository) -> None:
    record = make_document(repository)

    repository.update_status(
        owner_id=ALICE,
        document_id=record.id,
        status=STATUS_FAILED,
        error="No text layer was found.",
    )

    stored = repository.get_document(owner_id=ALICE, document_id=record.id)
    assert stored is not None
    assert stored.status == STATUS_FAILED
    assert stored.error == "No text layer was found."


def test_error_text_is_truncated(repository: DocumentRepository) -> None:
    """Provider errors can embed whole request bodies; the column must not grow forever."""
    record = make_document(repository)

    repository.update_status(
        owner_id=ALICE, document_id=record.id, status=STATUS_FAILED, error="x" * 5000
    )

    stored = repository.get_document(owner_id=ALICE, document_id=record.id)
    assert stored is not None
    assert stored.error is not None
    assert len(stored.error) == 1000


def test_status_update_clears_a_previous_error(repository: DocumentRepository) -> None:
    """A retried document must not keep displaying its old failure."""
    record = make_document(repository)
    repository.update_status(
        owner_id=ALICE, document_id=record.id, status=STATUS_FAILED, error="boom"
    )

    repository.update_status(owner_id=ALICE, document_id=record.id, status=STATUS_READY)

    stored = repository.get_document(owner_id=ALICE, document_id=record.id)
    assert stored is not None
    assert stored.error is None


def test_listing_is_newest_first(repository: DocumentRepository) -> None:
    first = make_document(repository, name="first.txt")
    second = make_document(repository, name="second.txt")

    listed = repository.list_documents(owner_id=ALICE)

    assert [record.id for record in listed] == [second.id, first.id]


def test_document_is_invisible_to_another_owner(repository: DocumentRepository) -> None:
    record = make_document(repository, owner=ALICE)

    assert repository.get_document(owner_id=BOB, document_id=record.id) is None


def test_listing_only_returns_your_own_documents(repository: DocumentRepository) -> None:
    make_document(repository, owner=ALICE, name="alice.txt")
    make_document(repository, owner=BOB, name="bob.txt")

    assert [record.filename for record in repository.list_documents(owner_id=ALICE)] == [
        "alice.txt"
    ]
    assert [record.filename for record in repository.list_documents(owner_id=BOB)] == ["bob.txt"]


def test_another_owner_cannot_update_your_document(repository: DocumentRepository) -> None:
    record = make_document(repository, owner=ALICE)

    updated = repository.update_status(
        owner_id=BOB, document_id=record.id, status=STATUS_PROCESSING
    )

    assert updated is False
    stored = repository.get_document(owner_id=ALICE, document_id=record.id)
    assert stored is not None
    assert stored.status == "pending"


def test_another_owner_cannot_delete_your_document(repository: DocumentRepository) -> None:
    record = make_document(repository, owner=ALICE)

    assert repository.delete_document(owner_id=BOB, document_id=record.id) is False
    assert repository.get_document(owner_id=ALICE, document_id=record.id) is not None


def test_vectors_are_never_returned_to_another_owner(repository: DocumentRepository) -> None:
    """The filter must be in the query, not applied afterwards."""
    record = make_document(repository, owner=ALICE)
    repository.add_chunks([make_chunk(document_id=record.id, owner=ALICE)])

    chunks, matrix = repository.fetch_chunks(owner_id=BOB)

    assert chunks == []
    assert matrix is not None
    assert matrix.size == 0


def test_chunks_round_trip_with_embeddings(repository: DocumentRepository) -> None:
    record = make_document(repository)
    vector = [0.1, 0.2, 0.3, 0.4]
    repository.add_chunks(
        [make_chunk(document_id=record.id, content="hello world", embedding=vector)]
    )

    chunks, matrix = repository.fetch_chunks(owner_id=ALICE)

    assert len(chunks) == 1
    assert chunks[0].content == "hello world"
    assert chunks[0].filename == record.filename
    assert matrix is not None
    assert matrix.shape == (1, 4)
    assert np.allclose(matrix[0], np.array(vector, dtype=np.float32))


def test_multiple_chunks_form_a_matrix(repository: DocumentRepository) -> None:
    record = make_document(repository)
    repository.add_chunks(
        [
            make_chunk(document_id=record.id, index=0, embedding=[1.0, 0.0]),
            make_chunk(document_id=record.id, index=1, embedding=[0.0, 1.0]),
            make_chunk(document_id=record.id, index=2, embedding=[0.5, 0.5]),
        ]
    )

    chunks, matrix = repository.fetch_chunks(owner_id=ALICE)

    assert len(chunks) == 3
    assert matrix is not None
    assert matrix.shape == (2, 3) or matrix.shape == (3, 2)


def test_embeddings_can_be_skipped(repository: DocumentRepository) -> None:
    """Listing passages should not pay to deserialise every vector."""
    record = make_document(repository)
    repository.add_chunks([make_chunk(document_id=record.id)])

    chunks, matrix = repository.fetch_chunks(owner_id=ALICE, embeddings=False)

    assert len(chunks) == 1
    assert matrix is None


def test_fetch_can_be_restricted_to_specific_documents(repository: DocumentRepository) -> None:
    first = make_document(repository, name="first.txt")
    second = make_document(repository, name="second.txt")
    repository.add_chunks([make_chunk(document_id=first.id, content="from first")])
    repository.add_chunks([make_chunk(document_id=second.id, content="from second")])

    chunks, _ = repository.fetch_chunks(owner_id=ALICE, document_ids=[first.id])

    assert [chunk.content for chunk in chunks] == ["from first"]


def test_fetch_with_an_unknown_document_id_returns_nothing(repository: DocumentRepository) -> None:
    record = make_document(repository)
    repository.add_chunks([make_chunk(document_id=record.id)])

    chunks, matrix = repository.fetch_chunks(owner_id=ALICE, document_ids=["does-not-exist"])

    assert chunks == []
    assert matrix is not None
    assert matrix.size == 0


def test_counting_chunks(repository: DocumentRepository) -> None:
    record = make_document(repository)
    repository.add_chunks([make_chunk(document_id=record.id, index=index) for index in range(5)])

    assert repository.count_chunks(owner_id=ALICE) == 5
    assert repository.count_chunks(owner_id=ALICE, document_id=record.id) == 5
    assert repository.count_chunks(owner_id=BOB) == 0


def test_deleting_a_document_removes_its_vectors(repository: DocumentRepository) -> None:
    """Orphaned vectors would keep answering from a deleted document."""
    record = make_document(repository)
    repository.add_chunks([make_chunk(document_id=record.id, index=index) for index in range(4)])

    assert repository.delete_document(owner_id=ALICE, document_id=record.id) is True

    assert repository.get_document(owner_id=ALICE, document_id=record.id) is None
    assert repository.count_chunks(owner_id=ALICE) == 0


def test_deleting_one_document_leaves_others_intact(repository: DocumentRepository) -> None:
    keep = make_document(repository, name="keep.txt")
    remove = make_document(repository, name="remove.txt")
    repository.add_chunks([make_chunk(document_id=keep.id, content="keep me")])
    repository.add_chunks([make_chunk(document_id=remove.id, content="remove me")])

    repository.delete_document(owner_id=ALICE, document_id=remove.id)

    chunks, _ = repository.fetch_chunks(owner_id=ALICE)
    assert [chunk.content for chunk in chunks] == ["keep me"]


def test_delete_document_chunks_only_removes_vectors(repository: DocumentRepository) -> None:
    """Used when indexing fails: keep the record, drop the partial vectors."""
    record = make_document(repository)
    repository.add_chunks([make_chunk(document_id=record.id)])

    removed = repository.delete_document_chunks(owner_id=ALICE, document_id=record.id)

    assert removed == 1
    assert repository.get_document(owner_id=ALICE, document_id=record.id) is not None
    assert repository.count_chunks(owner_id=ALICE) == 0


def test_store_is_persistent_across_instances(tmp_path: Path) -> None:
    """Data must outlive the process, which is what separates this from an in-memory store."""
    database_path = tmp_path / "persist" / "app.db"
    first = DocumentRepository(database_path)
    record = make_document(first)
    first.add_chunks([make_chunk(document_id=record.id, content="durable")])

    second = DocumentRepository(database_path)

    stored = second.get_document(owner_id=ALICE, document_id=record.id)
    assert stored is not None
    assert stored.id == record.id
    chunks, _ = second.fetch_chunks(owner_id=ALICE)
    assert [chunk.content for chunk in chunks] == ["durable"]


def test_database_directory_is_created(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c" / "app.db"
    DocumentRepository(nested)
    assert nested.exists()
