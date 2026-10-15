"""Test-only seeding helpers that write decisions through the kernel write path."""

from __future__ import annotations

from pathlib import Path

from nauro_core.constants import DECISIONS_DIR
from nauro_core.operations.propose_decision import _do_supersede, _write_decision_direct

from nauro.store.filesystem_store import FilesystemStore


def append_decision(
    store_path: Path,
    title: str,
    rationale: str | None = None,
    rejected=None,
    confidence: str = "medium",
    decision_type: str | None = None,
    reversibility: str | None = None,
    files_affected: list[str] | None = None,
    source: str | None = None,
) -> Path:
    """Seed a decision file and return its path."""
    decision_id = _write_decision_direct(
        FilesystemStore(store_path),
        {
            "title": title,
            "rationale": rationale,
            "rejected": rejected,
            "confidence": confidence,
            "decision_type": decision_type,
            "reversibility": reversibility,
            "files_affected": files_affected,
            "source": source,
        },
    )
    return store_path / DECISIONS_DIR / f"{decision_id}.md"


def supersede_decision(
    old_decision_id: str,
    new_proposal: dict,
    project_path: Path,
) -> str:
    """Seed a supersede through the kernel and return the new decision id."""
    new_id, _op, _touched, _resolve, error = _do_supersede(
        FilesystemStore(project_path), new_proposal, old_decision_id
    )
    assert error is None, error
    return new_id
