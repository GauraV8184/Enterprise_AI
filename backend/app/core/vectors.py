"""Numerical helpers shared by the vector stores and the retrieval layer.

This lives outside ``app.rag`` on purpose. Ranking is a property of *where the
vectors are stored and compared*, and the SQLite store needs it without the store
depending on the retrieval layer - which would point the dependency arrow the
wrong way.
"""

from __future__ import annotations

import numpy as np


def cosine_similarity_matrix(matrix: np.ndarray, vector: np.ndarray) -> np.ndarray:
    """Cosine similarity of ``vector`` against every row of ``matrix``.

    Guarded against zero-norm rows, which would otherwise divide by zero and
    produce NaN that sorts unpredictably.
    """
    if matrix.size == 0:
        return np.empty((0,), dtype=np.float32)

    query = np.asarray(vector, dtype=np.float32).reshape(-1)
    if query.shape[0] != matrix.shape[1]:
        raise ValueError(
            f"Embedding dimension mismatch: stored chunks are {matrix.shape[1]}-dimensional "
            f"but the query embedding is {query.shape[0]}-dimensional. "
            "This usually means EMBEDDING_MODEL changed after documents were indexed."
        )

    matrix_norms = np.linalg.norm(matrix, axis=1)
    query_norm = float(np.linalg.norm(query))
    denominator = matrix_norms * query_norm
    if denominator.size == 0:
        return np.empty((0,), dtype=np.float32)

    with np.errstate(divide="ignore", invalid="ignore"):
        similarities = (matrix @ query) / denominator
    return np.nan_to_num(similarities, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)
