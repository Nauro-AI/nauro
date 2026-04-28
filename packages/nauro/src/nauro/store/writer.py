"""Store writer — write operations for the project store.

All writes to ~/.nauro/projects/<name>/ go through this module.

As of nauro-core 0.2.0, decision files are emitted via the v2 pydantic
model. ``append_decision`` / ``supersede_decision`` / ``update_decision``
build a ``Decision`` and serialize with ``format_decision``. String
templating is gone; the one source of truth for the on-disk format is
``nauro_core.decision_model``.
"""

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock
from nauro_core import extract_decision_number, parse_decision
from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionSource,
    DecisionStatus,
    DecisionType,
    RejectedAlternative,
    Reversibility,
    format_decision,
)
from nauro_core.state import migrate_legacy_state, prepare_state_update

from nauro.constants import (
    DECISIONS_DIR,
    OPEN_QUESTIONS_MD,
    SLUG_MAX_LENGTH,
    STATE_CURRENT_FILENAME,
    STATE_HISTORY_FILENAME,
    STATE_MD,
)


def _coerce_rejected(rejected: Sequence[dict | str] | str | None) -> list[RejectedAlternative]:
    """Accept the legacy proposal-dict shapes and coerce to RejectedAlternative."""
    if rejected is None:
        return []
    if isinstance(rejected, str):
        # Defensive: MCP transport may send arrays as JSON strings.
        try:
            rejected = json.loads(rejected)
        except (json.JSONDecodeError, ValueError):
            return []
    if not rejected:
        return []
    out: list[RejectedAlternative] = []
    for item in rejected:
        if isinstance(item, RejectedAlternative):
            out.append(item)
        elif isinstance(item, dict):
            name = item.get("alternative") or item.get("name") or "Unknown"
            reason = item.get("reason")
            out.append(RejectedAlternative(name=str(name), reason=reason or None))
        elif isinstance(item, str):
            out.append(RejectedAlternative(name=item, reason=None))
    return out


def _coerce_files_affected(files_affected: list[str] | str | None) -> list[str]:
    """Accept both list and JSON-string shapes from the MCP transport."""
    if files_affected is None:
        return []
    if isinstance(files_affected, str):
        try:
            decoded = json.loads(files_affected)
            if isinstance(decoded, list):
                return [str(x) for x in decoded]
            return [files_affected]
        except (json.JSONDecodeError, ValueError):
            return [files_affected]
    return list(files_affected)


def _optional_enum(raw, enum_cls):
    if raw is None:
        return None
    if isinstance(raw, enum_cls):
        return raw
    s = str(raw).strip()
    if not s:
        return None
    return enum_cls(s)


def append_decision(
    store_path: Path,
    title: str,
    rationale: str | None = None,
    rejected: Sequence[dict | str] | None = None,
    confidence: str = "medium",
    decision_type: str | None = None,
    reversibility: str | None = None,
    files_affected: list[str] | None = None,
    source: str | None = None,
) -> Path:
    """Create the next sequential decision file in decisions/.

    Builds a v2 ``Decision`` and serializes via ``format_decision``.
    Proposal-dict input shape is unchanged; only the on-disk format moves.
    """
    decisions_dir = store_path / DECISIONS_DIR
    decisions_dir.mkdir(parents=True, exist_ok=True)

    lock_path = decisions_dir / ".lock"
    with FileLock(lock_path):
        existing = sorted(decisions_dir.glob("*.md"))
        next_num = 1
        for f in existing:
            n = extract_decision_number(f.name)
            if n is not None:
                next_num = max(next_num, n + 1)

        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        if len(slug) > SLUG_MAX_LENGTH:
            slug = slug[:SLUG_MAX_LENGTH].rsplit("-", 1)[0]
        filename = f"{next_num:03d}-{slug}.md"
        filepath = decisions_dir / filename

        decision = Decision(
            date=datetime.now(UTC).date(),
            version=1,
            status=DecisionStatus.active,
            confidence=DecisionConfidence(confidence),
            decision_type=_optional_enum(decision_type, DecisionType),
            reversibility=_optional_enum(reversibility, Reversibility),
            source=_optional_enum(source, DecisionSource),
            files_affected=_coerce_files_affected(files_affected),
            rejected=_coerce_rejected(rejected),
            num=next_num,
            title=title,
            rationale=rationale or title,
        )

        filepath.write_text(format_decision(decision))

    return filepath


def supersede_decision(
    old_decision_id: str,
    new_proposal: dict,
    project_path: Path,
) -> str:
    """Supersede an old decision with a new one.

    Writes the new decision first (via ``append_decision``), then parses the
    old file with the v2 parser and re-emits it with
    ``status=superseded`` + ``superseded_by=<new_id>``. The new decision is
    updated to carry ``supersedes=<old_id>``.
    """
    decisions_dir = project_path / DECISIONS_DIR

    old_path: Path | None = None
    for f in decisions_dir.glob("*.md"):
        if f.stem == old_decision_id:
            old_path = f
            break

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

    # Canonicalize stem-formatted IDs to plain integer strings to match the
    # nauro-core supersession-ref convention ("70", not "070-some-slug").
    old_ref = _canonical_supersession_ref(old_decision_id)
    new_ref = _canonical_supersession_ref(new_decision_id)

    # Rewrite the new decision with the Supersedes backref.
    new_decision = parse_decision(new_path.read_text(), new_path.name)
    new_decision_rewritten = new_decision.model_copy(update={"supersedes": old_ref})
    new_path.write_text(format_decision(new_decision_rewritten))

    # Mark the old decision as superseded.
    if old_path and old_path.exists():
        old_decision = parse_decision(old_path.read_text(), old_path.name)
        old_rewritten = old_decision.model_copy(
            update={
                "status": DecisionStatus.superseded,
                "superseded_by": new_ref,
            }
        )
        old_path.write_text(format_decision(old_rewritten))

    return new_decision_id


def _canonical_supersession_ref(decision_id: str) -> str:
    num = extract_decision_number(decision_id)
    if num is None:
        raise ValueError(
            f"Cannot derive supersession ref from decision id {decision_id!r}: "
            "expected leading number prefix like '042-some-title'."
        )
    return str(num)


def update_decision(
    decision_id: str,
    additional_rationale: str,
    project_path: Path,
) -> str:
    """Update an existing decision by incrementing its version and appending rationale.

    The update appends a dated paragraph to the decision's rationale rather
    than adding a new ``## Update`` section; v2's parser collapses everything
    under ``## Decision`` and we don't want content that round-trips
    asymmetrically.
    """
    decisions_dir = project_path / DECISIONS_DIR

    target_path: Path | None = None
    for f in decisions_dir.glob("*.md"):
        if f.stem == decision_id:
            target_path = f
            break

    if not target_path or not target_path.exists():
        return decision_id

    decision = parse_decision(target_path.read_text(), target_path.name)
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    appended_rationale = (
        f"{decision.rationale.strip()}\n\n"
        f"*Update (v{decision.version + 1}) — {date}:* {additional_rationale.strip()}"
    )
    updated = decision.model_copy(
        update={
            "version": decision.version + 1,
            "rationale": appended_rationale,
        }
    )
    target_path.write_text(format_decision(updated))
    return decision_id


def append_question(store_path: Path, question: str) -> None:
    """Append a question to open-questions.md with timestamp."""
    oq_path = store_path / OPEN_QUESTIONS_MD
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    entry = f"- [{timestamp}] {question}\n"

    if oq_path.exists():
        content = oq_path.read_text()
    else:
        content = "# Open Questions\n"

    lines = content.split("\n")
    insert_idx = 1
    for i, line in enumerate(lines):
        if line.startswith("# "):
            insert_idx = i + 1
            break

    while insert_idx < len(lines) and (
        lines[insert_idx].strip() == "" or lines[insert_idx].startswith("<!--")
    ):
        insert_idx += 1

    lines.insert(insert_idx, entry.rstrip())
    oq_path.write_text("\n".join(lines))


def update_state(store_path: Path, delta: str) -> None:
    """Replace the current state with *delta*, archiving the previous state."""
    current_path = store_path / STATE_CURRENT_FILENAME
    history_path = store_path / STATE_HISTORY_FILENAME
    legacy_path = store_path / STATE_MD

    current_content: str | None = None
    if current_path.exists():
        current_content = current_path.read_text()
    elif legacy_path.exists():
        legacy_content = legacy_path.read_text()
        migrated = migrate_legacy_state(legacy_content)
        current_path.write_text(migrated.current_content)
        current_content = migrated.current_content
    else:
        return

    result = prepare_state_update(delta, current_content)
    current_path.write_text(result.current_content)

    if result.history_entry is not None:
        existing_history = history_path.read_text() if history_path.exists() else ""
        history_path.write_text(existing_history + result.history_entry)
