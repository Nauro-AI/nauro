"""Store writer — write operations for the project store.

All writes to ~/.nauro/projects/<name>/ go through this module.
"""

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock
from nauro_core import extract_decision_number
from nauro_core.state import migrate_legacy_state, prepare_state_update

from nauro.constants import (
    DECISIONS_DIR,
    OPEN_QUESTIONS_MD,
    SLUG_MAX_LENGTH,
    STATE_CURRENT_FILENAME,
    STATE_HISTORY_FILENAME,
    STATE_MD,
)


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

    Reads existing decision files to determine the next number.

    Args:
        store_path: Path to the project store directory.
        title: Decision title.
        rationale: Why this decision was made.
        rejected: Rejected alternatives. Each item is either a string (legacy)
            or a dict with "alternative" and "reason" keys.
        confidence: "high", "medium", or "low".
        decision_type: Classification (architecture, library_choice, pattern, etc.).
        reversibility: "easy", "moderate", or "hard".
        files_affected: List of key file paths affected by this decision.
        source: Where this decision was extracted from (e.g., "commit", "compaction (session abc)").

    Returns:
        Path to the newly created decision file.
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

        date = datetime.now(UTC).strftime("%Y-%m-%d")

        # Build metadata lines
        metadata_lines = [
            f"**Date:** {date}",
            "**Version:** 1",
            "**Status:** active",
            f"**Confidence:** {confidence}",
        ]
        if decision_type:
            metadata_lines.append(f"**Type:** {decision_type}")
        if reversibility:
            metadata_lines.append(f"**Reversibility:** {reversibility}")
        if source:
            metadata_lines.append(f"**Source:** {source}")
        # Defensive: MCP transport may pass arrays as JSON strings
        if isinstance(files_affected, str):
            try:
                files_affected = json.loads(files_affected)
            except (json.JSONDecodeError, ValueError):
                files_affected = [files_affected]
        if files_affected:
            metadata_lines.append(f"**Files affected:** {', '.join(files_affected)}")

        metadata_block = "\n".join(metadata_lines)

        # Build decision section
        decision_section = "## Decision\n\n"
        if rationale:
            decision_section += f"{rationale}\n"
        else:
            decision_section += f"{title}\n"

        # Build rejected alternatives section
        rejected_section = ""
        if isinstance(rejected, str):
            try:
                rejected = json.loads(rejected)
            except (json.JSONDecodeError, ValueError):
                rejected = None
        if rejected:
            rejected_section = "\n## Rejected Alternatives\n"
            for item in rejected:
                if isinstance(item, dict):
                    alt_name = item.get("alternative", "Unknown")
                    alt_reason = item.get("reason", "")
                    rejected_section += f"\n### {alt_name}\n{alt_reason}\n"
                elif isinstance(item, str):
                    rejected_section += f"\n### {item}\n"

        content = (
            f"# {next_num:03d} — {title}\n\n"
            f"{metadata_block}\n\n"
            f"{decision_section}{rejected_section}"
        )

        filepath.write_text(content)

    return filepath


def supersede_decision(
    old_decision_id: str,
    new_proposal: dict,
    project_path: Path,
) -> str:
    """Supersede an old decision with a new one.

    Marks the old decision as superseded and creates the new decision
    with a Supersedes pointer.

    Args:
        old_decision_id: Stem of the old decision file (e.g. "019-use-cloudflare").
        new_proposal: Dict with title, rationale, etc.
        project_path: Path to the project store.

    Returns:
        The new decision's file stem (decision ID).
    """
    decisions_dir = project_path / DECISIONS_DIR

    # Find and update the old decision file
    old_path = None
    for f in decisions_dir.glob("*.md"):
        if f.stem == old_decision_id:
            old_path = f
            break

    # Write the new decision first to get its ID
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

    # Add Supersedes pointer to the new decision
    new_content = new_path.read_text()
    new_content = new_content.replace(
        "**Status:** active",
        f"**Status:** active\n**Supersedes:** {old_decision_id}",
    )
    new_path.write_text(new_content)

    # Mark old decision as superseded
    if old_path and old_path.exists():
        old_content = old_path.read_text()
        if "**Status:** active" in old_content:
            old_content = old_content.replace(
                "**Status:** active",
                f"**Status:** superseded\n**Superseded by:** {new_decision_id}",
            )
        elif "**Status:**" not in old_content:
            # Old format without Status field — add it after Date line
            old_content = re.sub(
                r"(\*\*Date:\*\* \S+)",
                rf"\1\n**Status:** superseded\n**Superseded by:** {new_decision_id}",
                old_content,
            )
        old_path.write_text(old_content)

    return new_decision_id


def update_decision(
    decision_id: str,
    additional_rationale: str,
    project_path: Path,
) -> str:
    """Update an existing decision by incrementing its version and appending rationale.

    Args:
        decision_id: Stem of the decision file (e.g. "019-use-cloudflare").
        additional_rationale: New rationale to append.
        project_path: Path to the project store.

    Returns:
        The decision's file stem (same as input).
    """
    decisions_dir = project_path / DECISIONS_DIR

    target_path = None
    for f in decisions_dir.glob("*.md"):
        if f.stem == decision_id:
            target_path = f
            break

    if not target_path or not target_path.exists():
        return decision_id

    content = target_path.read_text()

    # Increment version
    version_match = re.search(r"\*\*Version:\*\*\s*(\d+)", content)
    if version_match:
        old_version = int(version_match.group(1))
        new_version = old_version + 1
        content = content.replace(
            f"**Version:** {old_version}",
            f"**Version:** {new_version}",
        )
    else:
        # Old format — add version field after Date
        new_version = 2
        content = re.sub(
            r"(\*\*Date:\*\* \S+)",
            rf"\1\n**Version:** {new_version}",
            content,
        )

    # Append the update section
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    update_section = f"\n## Update (v{new_version}) — {date}\n\n{additional_rationale}\n"
    content += update_section

    target_path.write_text(content)
    return decision_id


def append_question(store_path: Path, question: str) -> None:
    """Append a question to open-questions.md with timestamp.

    Args:
        store_path: Path to the project store directory.
        question: The question text.
    """
    oq_path = store_path / OPEN_QUESTIONS_MD
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    entry = f"- [{timestamp}] {question}\n"

    if oq_path.exists():
        content = oq_path.read_text()
    else:
        content = "# Open Questions\n"

    # Insert after the header line
    lines = content.split("\n")
    insert_idx = 1
    for i, line in enumerate(lines):
        if line.startswith("# "):
            insert_idx = i + 1
            break

    # Skip blank lines / comment lines right after header
    while insert_idx < len(lines) and (
        lines[insert_idx].strip() == "" or lines[insert_idx].startswith("<!--")
    ):
        insert_idx += 1

    lines.insert(insert_idx, entry.rstrip())
    oq_path.write_text("\n".join(lines))


def update_state(store_path: Path, delta: str) -> None:
    """Replace the current state with *delta*, archiving the previous state.

    Writes state_current.md (replace semantics) and appends to
    state_history.md (append-only archive). On first call after upgrade,
    migrates legacy state.md to state_current.md without deleting state.md.

    Args:
        store_path: Path to the project store directory.
        delta: The new current state description.
    """
    current_path = store_path / STATE_CURRENT_FILENAME
    history_path = store_path / STATE_HISTORY_FILENAME
    legacy_path = store_path / STATE_MD

    # Read existing current state
    current_content: str | None = None
    if current_path.exists():
        current_content = current_path.read_text()
    elif legacy_path.exists():
        # First write after upgrade: migrate legacy state.md
        legacy_content = legacy_path.read_text()
        migrated = migrate_legacy_state(legacy_content)
        current_path.write_text(migrated.current_content)
        current_content = migrated.current_content
        # Do NOT delete state.md — leave as dead file for sync safety
    else:
        # No state files at all — nothing to update
        return

    # Prepare the update
    result = prepare_state_update(delta, current_content)

    # Write state_current.md (full replace)
    current_path.write_text(result.current_content)

    # Append to state_history.md if there's a history entry
    if result.history_entry is not None:
        existing_history = history_path.read_text() if history_path.exists() else ""
        history_path.write_text(existing_history + result.history_entry)
