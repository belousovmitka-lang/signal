"""Small numpy helpers for embeddings stored as JSON lists."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def unit_matrix(vectors: Sequence[Sequence[float]]) -> np.ndarray:
    """Rows scaled to length 1, so a dot product is a cosine similarity."""
    m = np.asarray(vectors, dtype=np.float32)
    if m.ndim != 2 or not len(m):
        return m.reshape(0, 0)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.where(norms == 0, 1, norms)


def centroid(unit_rows: np.ndarray) -> np.ndarray:
    """Mean direction of unit rows, itself scaled to length 1."""
    c = unit_rows.mean(axis=0)
    n = np.linalg.norm(c)
    return c / n if n else c


def cosine(a: Sequence[float] | np.ndarray, b: Sequence[float] | np.ndarray) -> float:
    a, b = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return float(a @ b / (na * nb)) if na and nb else 0.0
