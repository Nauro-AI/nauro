"""Test-only seeding helpers that wrap the kernel write path.

The pre-cutover ``nauro.store.writer`` module exposed
``append_decision`` / ``supersede_decision`` / ``update_decision`` as the
canonical write API for fixture seeding across the test suite. PR 10
moves the canonical write path into ``nauro_core.operations``; these
wrappers stay test-only so existing fixtures don't need to be reshaped.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from nauro_core.constants import DECISIONS_DIR, OPEN_QUESTIONS_MD
from nauro_core.decision_model import DecisionStatus, format_decision, parse_decision
from nauro_core.operations.propose_decision import _write_decision_direct
from nauro_core.questions import OpenQuestionsFile, ResolveResult

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
    """Seed a decision file. Mirrors the pre-cutover ``writer.append_decision``."""
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
    """Seed a supersede: write the new decision, then flip the old.

    Mirrors the pre-cutover writer's two-write sequence for test fixture
    seeding only.
    """
    new_path = append_decision(
        project_path,
        title=new_proposal.get("title", "Untitled"),
        rationale=new_proposal.get("rationale"),
        rejected=new_proposal.get("rejected"),
        confidence=new_proposal.get("confidence", "medium"),
        decision_type=new_proposal.get("decision_type"),
        reversibility=new_proposal.get("reversibility"),
        files_affected=new_proposal.get("files_affected"),
        source=new_proposal.get("source"),
    )
    new_decision_id = new_path.stem

    new_body = new_path.read_text()
    new_decision = parse_decision(new_body, new_path.name)
    new_num = new_decision.num

    old_path: Path | None = None
    for f in (project_path / DECISIONS_DIR).glob("*.md"):
        if f.stem == old_decision_id:
            old_path = f
            break

    if old_path is not None:
        old_num_int = _extract_num(old_decision_id)
        if old_num_int is not None:
            new_path.write_text(
                format_decision(new_decision.model_copy(update={"supersedes": str(old_num_int)}))
            )
        old_decision = parse_decision(old_path.read_text(), old_path.name)
        old_path.write_text(
            format_decision(
                old_decision.model_copy(
                    update={
                        "status": DecisionStatus.superseded,
                        "superseded_by": str(new_num),
                    }
                )
            )
        )
    return new_decision_id


def update_decision(
    decision_id: str,
    additional_rationale: str,
    project_path: Path,
) -> str:
    """Seed an update: append a dated paragraph to the target decision's body."""
    decisions_dir = project_path / DECISIONS_DIR
    target_path: Path | None = None
    for f in decisions_dir.glob("*.md"):
        if f.stem == decision_id:
            target_path = f
            break
    if target_path is None or not target_path.exists():
        return decision_id

    decision = parse_decision(target_path.read_text(), target_path.name)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    appended = (
        f"{decision.rationale.strip()}\n\n"
        f"*Update (v{decision.version + 1}) — {date}:* {additional_rationale.strip()}"
    )
    updated = decision.model_copy(update={"version": decision.version + 1, "rationale": appended})
    target_path.write_text(format_decision(updated))
    return decision_id


def resolve_questions_in_file(
    store_path: Path,
    ids: list[str],
    decision_num: int,
    decision_date,
) -> ResolveResult:
    """Move named open questions under ``## Resolved`` in open-questions.md."""
    if not ids:
        return ResolveResult(file=OpenQuestionsFile(), moved_ids=(), unknown_ids=())
    oq_path = store_path / OPEN_QUESTIONS_MD
    content = oq_path.read_text() if oq_path.exists() else ""
    file = OpenQuestionsFile.parse(content)
    result = file.resolve(ids, decision_num, decision_date)
    oq_path.write_text(result.file.format())
    return result


def _extract_num(decision_id: str) -> int | None:
    """Pull the leading int off a decision id stem or synthetic shape."""
    from nauro_core.parsing import extract_decision_number

    return extract_decision_number(decision_id)
