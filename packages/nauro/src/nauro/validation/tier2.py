"""Tier 2 validation — embedding similarity.

Checks the proposal against existing decisions using embeddings.
Falls back to Jaccard text similarity when embedding API is unavailable.
"""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
from typing import Any

from nauro.store.reader import _list_decisions

logger = logging.getLogger("nauro.validation.tier2")

EMBEDDING_INDEX_FILE = ".embedding-index.json"
EMBEDDING_MODEL = "text-embedding-3-small"
SIMILARITY_THRESHOLD = 0.65
JACCARD_THRESHOLD = 0.5
TOP_K = 5


def check_similarity(proposal: dict, project_path: Path) -> tuple[str, list[dict]]:
    """Check proposal similarity against existing decisions.

    Returns:
        (action, similar_decisions) where action is "auto_confirm" or "needs_review".
    """
    index = _load_embedding_index(project_path)
    if not index.get("decisions"):
        return ("auto_confirm", [])

    proposal_text = _proposal_to_text(proposal)

    # Try embedding-based similarity
    try:
        proposal_embedding = _embed_text(proposal_text)
        if proposal_embedding:
            return _compare_embeddings(proposal_embedding, index)
    except Exception as e:
        logger.warning("Embedding unavailable, falling back to Jaccard: %s", e)

    # Fallback: Jaccard similarity
    return _compare_jaccard(proposal_text, project_path)


def update_embedding_index(
    decision_id: str, title: str, rationale: str, project_path: Path
) -> None:
    """Add a new decision's embedding to the index."""
    text = f"{title}. {rationale[:200]}"
    index = _load_embedding_index(project_path)

    try:
        embedding = _embed_text(text)
        if embedding:
            index.setdefault("decisions", {})[decision_id] = {
                "embedding": embedding,
                "title": title,
            }
            index["model"] = EMBEDDING_MODEL
            _save_embedding_index(project_path, index)
            return
    except Exception as e:
        logger.warning("Could not embed decision %s: %s", decision_id, e)

    # Even without embedding, store the title for Jaccard fallback
    index.setdefault("decisions", {})[decision_id] = {
        "embedding": None,
        "title": title,
    }
    _save_embedding_index(project_path, index)


def rebuild_embedding_index(project_path: Path) -> dict:
    """Rebuild the entire embedding index from all decisions.

    Returns:
        Summary dict with count of indexed decisions.
    """
    decisions = _list_decisions(project_path)
    index: dict[str, Any] = {"model": EMBEDDING_MODEL, "decisions": {}}

    indexed = 0
    failed = 0

    for d in decisions:
        decision_id = f"decision-{d['num']:03d}"
        text = f"{d['title']}. {d['rationale'][:200]}"

        try:
            embedding = _embed_text(text)
            index["decisions"][decision_id] = {
                "embedding": embedding,
                "title": d["title"],
            }
            indexed += 1
        except Exception as e:
            logger.warning("Failed to embed %s: %s", decision_id, e)
            index["decisions"][decision_id] = {
                "embedding": None,
                "title": d["title"],
            }
            failed += 1

    _save_embedding_index(project_path, index)
    return {"indexed": indexed, "failed": failed, "model": EMBEDDING_MODEL}


def _proposal_to_text(proposal: dict) -> str:
    """Convert proposal to text for similarity comparison."""
    title = proposal.get("title", "")
    rationale = proposal.get("rationale", "")
    return f"{title}. {rationale[:200]}"


def _embed_text(text: str) -> list[float] | None:
    """Embed text using OpenAI text-embedding-3-small.

    Checks NAURO_EMBEDDING_API_KEY, then OPENAI_API_KEY.
    Returns None if no API key is available.
    """
    api_key = os.environ.get("NAURO_EMBEDDING_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("No embedding API key (NAURO_EMBEDDING_API_KEY or OPENAI_API_KEY)")

    try:
        import openai
    except ImportError:
        raise RuntimeError("openai package required for embeddings: pip install nauro[validation]")

    client = openai.OpenAI(api_key=api_key)
    response = client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return response.data[0].embedding


def _compare_embeddings(proposal_embedding: list[float], index: dict) -> tuple[str, list[dict]]:
    """Compare proposal embedding against indexed decisions."""
    similarities = []

    for decision_id, entry in index.get("decisions", {}).items():
        emb = entry.get("embedding")
        if not emb:
            continue
        sim = _cosine_similarity(proposal_embedding, emb)
        similarities.append(
            {
                "id": decision_id,
                "title": entry.get("title", ""),
                "similarity": round(sim, 3),
                "rationale_preview": "",  # filled by tier 3 if needed
            }
        )

    similarities.sort(key=lambda x: x["similarity"], reverse=True)

    if not similarities or similarities[0]["similarity"] < SIMILARITY_THRESHOLD:
        return ("auto_confirm", [])

    return ("needs_review", similarities[:TOP_K])


def _compare_jaccard(proposal_text: str, project_path: Path) -> tuple[str, list[dict]]:
    """Fallback: compare using Jaccard similarity on word sets."""
    decisions = _list_decisions(project_path)
    if not decisions:
        return ("auto_confirm", [])

    proposal_words = _word_set(proposal_text)
    similarities = []

    for d in decisions:
        decision_text = f"{d['title']}. {d['rationale'][:200]}"
        decision_words = _word_set(decision_text)
        sim = _jaccard_similarity(proposal_words, decision_words)
        decision_id = f"decision-{d['num']:03d}"
        similarities.append(
            {
                "id": decision_id,
                "title": d["title"],
                "similarity": round(sim, 3),
                "rationale_preview": d["rationale"][:100] if d["rationale"] else "",
            }
        )

    similarities.sort(key=lambda x: x["similarity"], reverse=True)

    if not similarities or similarities[0]["similarity"] < JACCARD_THRESHOLD:
        return ("auto_confirm", [])

    return ("needs_review", similarities[:TOP_K])


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _jaccard_similarity(a: set[str], b: set[str]) -> float:
    """Compute Jaccard similarity between two word sets."""
    if not a and not b:
        return 0.0
    intersection = a & b
    union = a | b
    return len(intersection) / len(union)


def _word_set(text: str) -> set[str]:
    """Extract a set of lowercase words from text."""
    return {w.lower().strip(".,;:!?()[]{}\"'") for w in text.split() if len(w) > 2}


def _load_embedding_index(project_path: Path) -> dict:
    """Load the embedding index."""
    path = project_path / EMBEDDING_INDEX_FILE
    if path.exists():
        try:
            return json.loads(path.read_text())  # type: ignore[no-any-return]
        except (json.JSONDecodeError, OSError):
            return {"model": EMBEDDING_MODEL, "decisions": {}}
    return {"model": EMBEDDING_MODEL, "decisions": {}}


def _save_embedding_index(project_path: Path, index: dict) -> None:
    """Save the embedding index."""
    path = project_path / EMBEDDING_INDEX_FILE
    path.write_text(json.dumps(index) + "\n")
