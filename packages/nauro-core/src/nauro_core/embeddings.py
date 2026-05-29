"""Optional embedding augmenter for retrieval.

Isolated from ``search.py`` so the BM25 path carries no embedding imports.
The dependency is an optional extra (``nauro-core[embeddings]``); when it is
absent the augmenter returns nothing and callers fall back to BM25-only.

Model: ``potion-retrieval-32M`` (Model2Vec static embeddings, numpy-only —
no torch, no ONNX runtime). Encoding is per-call; no persisted cache lives
here. Static encode of a few hundred short documents is a sub-second numpy
matmul, so there is no latency reason to cache at prototype scale.
"""

from __future__ import annotations

import logging

from nauro_core.decision_model import Decision

logger = logging.getLogger("nauro_core.embeddings")

EMBEDDING_MODEL = "minishlab/potion-retrieval-32M"

# Cache the loaded model across calls within a process. The model load is the
# one non-trivial cost (~30MB read + numpy array setup); encoding itself is
# cheap. This is in-process memoization only, not a persisted artifact.
_model = None
_load_failed = False


def embeddings_available() -> bool:
    """Return whether the optional embedding dependency can be imported."""
    try:
        import model2vec  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


def _get_model():
    """Load and memoize the Model2Vec model, or return None if unavailable.

    A failed load (missing dependency or model fetch failure) is recorded so
    the import/instantiation is attempted at most once per process; subsequent
    calls short-circuit to None without re-raising.
    """
    global _model, _load_failed
    if _model is not None:
        return _model
    if _load_failed:
        return None
    try:
        from model2vec import StaticModel

        _model = StaticModel.from_pretrained(EMBEDDING_MODEL)
        return _model
    except Exception:
        _load_failed = True
        logger.warning(
            "embedding model %s could not be loaded; falling back to BM25-only",
            EMBEDDING_MODEL,
            exc_info=True,
        )
        return None


def embedding_pool(
    decisions: list[Decision],
    query: str,
    top_k: int,
) -> list[int]:
    """Return the decision numbers of the top-k embedding matches for ``query``.

    Encodes the ``title + rationale`` of each decision and the query with the
    static model, ranks by cosine similarity (vectors are L2-normalized so a
    dot product is cosine), and returns the ``top_k`` decision numbers in
    descending similarity order.

    Returns an empty list when the dependency is absent, the model fails to
    load, or there is nothing to rank. Never raises — the caller treats an
    empty result as "no embedding contribution" and proceeds with BM25 only.
    """
    if not decisions or not query or not query.strip() or top_k <= 0:
        return []

    model = _get_model()
    if model is None:
        return []

    import numpy as np

    corpus = [f"{d.title} {d.rationale}" for d in decisions]
    doc_vectors = model.encode(corpus)
    query_vector = model.encode([query])[0]

    doc_vectors = _l2_normalize(np.asarray(doc_vectors, dtype=np.float32))
    query_vector = _l2_normalize_vector(np.asarray(query_vector, dtype=np.float32))

    similarities = doc_vectors @ query_vector
    k = min(top_k, len(decisions))
    # argsort ascending; take the last k and reverse for descending order.
    top_indices = np.argsort(similarities)[-k:][::-1]
    return [decisions[int(i)].num for i in top_indices]


def _l2_normalize(matrix):
    """L2-normalize each row of ``matrix``; zero rows stay zero."""
    import numpy as np

    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def _l2_normalize_vector(vector):
    """L2-normalize a single vector; a zero vector stays zero."""
    import numpy as np

    norm = np.linalg.norm(vector)
    if norm == 0:
        return vector
    return vector / norm
